"""Working evidence survives history rollover; observations never restrict tools."""

import json
import tempfile
import unittest
from pathlib import Path

from test_product_manager_workflow import valid_spec

from app.agents.code_engineer import build_code_engineer_messages
from app.generation.delivery import WorkItem
from app.generation.engineering.checkpoint import (
    load_checkpoint,
    new_checkpoint,
    prepare_manual_repair_retry,
)
from app.generation.engineering.work_memory import (
    implementation_stalled,
    inspection_action_required,
    item_memory,
    progress_guidance,
    record_progress,
    record_result,
    suppress_redundant_read,
    working_context,
)
from app.schemas.agent_action import ToolExecutionResult
from app.schemas.app_spec import AppSpec
from app.tools.files import read_file
from app.tools.paths import sha256_bytes


class WorkMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.c = new_checkpoint()
        self.c.update(current_work_item_id="auth", needs_delivery_plan=False)

    def read(self, path="user.py", content="VALUE = 1"):
        (self.root / path).write_text(content, encoding="utf-8")
        result = read_file(self.root, path=path).model_copy(update={"arguments": {"path": path}})
        record_result(self.c, result)
        return result

    def test_actual_coverage_not_requested_range_defines_new_information(self):
        (self.root / "a.py").write_text("a\nb\nc\nd\n", encoding="utf-8")
        for start, end in [(1, 2), (3, 4)]:
            record_progress(
                self.c, read_file(self.root, path="a.py", start_line=start, end_line=end)
            )
            self.assertEqual(self.c["no_progress_steps"], 0)
        for end in [4, 40, 400]:
            record_progress(self.c, read_file(self.root, path="a.py", end_line=end))
        self.assertEqual(self.c["no_progress_steps"], 3)
        self.assertEqual(self.c["read_coverage"]["a.py"]["ranges"], [[1, 4]])
        (self.root / "a.py").write_text("a\nb\nc\nchanged\n", encoding="utf-8")
        record_progress(self.c, read_file(self.root, path="a.py", start_line=4))
        self.assertEqual(self.c["no_progress_steps"], 0)
        self.assertEqual(self.c["steps_without_delivery"], 6)

    def test_text_omitted_from_model_context_is_not_marked_as_already_read(self):
        body = "".join(f"{i:03d}:" + "x" * 95 + "\n" for i in range(200))
        observation = ToolExecutionResult(
            tool_call_id="read",
            name="read_file",
            ok=True,
            summary="read",
            data={
                "path": "large.py",
                "content_hash": "h",
                "start_line": 1,
                "end_line": 200,
                "content": body,
            },
        )
        record_progress(self.c, observation)
        self.assertEqual(self.c["read_coverage"]["large.py"]["ranges"], [[1, 120]])
        tail = observation.model_copy(
            update={
                "data": {
                    **observation.data,
                    "start_line": 121,
                    "content": "".join(body.splitlines(keepends=True)[120:]),
                }
            }
        )
        record_progress(self.c, tail)
        self.assertEqual(self.c["no_progress_steps"], 0)
        self.assertEqual(self.c["read_coverage"]["large.py"]["ranges"], [[1, 200]])
        record_progress(self.c, tail)
        self.assertEqual(self.c["no_progress_steps"], 1)

    def test_medium_file_is_fully_visible_and_repeated_reads_require_action(self):
        body = "\n".join(f"const line{i} = '{'x' * 23}'" for i in range(240))
        self.assertTrue(6000 < len(body) < 12000)
        (self.root / "JobsPage.tsx").write_text(body, encoding="utf-8")
        first = read_file(self.root, path="JobsPage.tsx", start_line=1, end_line=240)
        record_result(self.c, first)
        evidence = working_context(self.c, self.root)["source_evidence"]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["end_line"], 240)
        self.assertFalse(evidence[0]["content_truncated"])

        for _ in range(4):
            repeated = suppress_redundant_read(
                self.c, self.root, read_file(self.root, path="JobsPage.tsx", end_line=240)
            )
            self.assertTrue(repeated.data["already_visible"])
            self.assertNotIn("content", repeated.data)
            record_progress(self.c, repeated)
        self.assertTrue(inspection_action_required(self.c))
        self.assertFalse(implementation_stalled(self.c))

        (self.root / "JobsPage.tsx").write_text(body + "\n// new version", encoding="utf-8")
        changed = suppress_redundant_read(
            self.c, self.root, read_file(self.root, path="JobsPage.tsx", end_line=241)
        )
        self.assertNotIn("already_visible", changed.data)

    def test_evicted_source_rereads_keep_bytes_but_close_read_only_window(self):
        first = self.read("evicted.py", "IMPORTANT = 1\n")
        record_progress(self.c, first)
        for index in range(12):
            self.read(f"other{index}.py", f"VALUE = {index}\n")
        self.assertNotIn("evicted.py", self.c["source_evidence"])

        for count in range(1, 5):
            repeated = suppress_redundant_read(
                self.c, self.root, read_file(self.root, path="evicted.py")
            )
            # An evicted excerpt is not available in the current model context.
            # Keep the source text for safe editing, but count the repeated range.
            self.assertNotIn("already_visible", repeated.data)
            self.assertEqual(repeated.data["content"], "IMPORTANT = 1")
            record_progress(self.c, repeated)
            self.assertEqual(item_memory(self.c)["redundant_read_steps"], count)
        self.assertTrue(inspection_action_required(self.c))

        (self.root / "evicted.py").write_text("IMPORTANT = 2\n", encoding="utf-8")
        changed = suppress_redundant_read(
            self.c, self.root, read_file(self.root, path="evicted.py")
        )
        record_progress(self.c, changed)
        self.assertEqual(item_memory(self.c)["redundant_read_steps"], 0)
        self.assertFalse(inspection_action_required(self.c))

    def test_written_source_keeps_first_explicit_read_result(self):
        path = "backend/app/api/router.py"
        raw = b"from app.api import auth, health\napi_router = auth.router\n"
        target = self.root / path
        target.parent.mkdir(parents=True)
        target.write_bytes(raw)
        record_result(
            self.c,
            ToolExecutionResult(
                tool_call_id="install",
                name="install_capability",
                ok=True,
                summary="installed",
            ),
            updates={path: raw},
        )
        evidence = working_context(self.c, self.root)["source_evidence"]
        self.assertIn(path, [entry["path"] for entry in evidence])
        legacy = load_checkpoint(json.loads(json.dumps({"checkpoint": self.c})))
        for fragment in legacy["source_evidence"][path]["fragments"]:
            fragment.pop("explicit_read", None)
        self.assertNotIn(
            "already_visible",
            suppress_redundant_read(legacy, self.root, read_file(self.root, path=path)).data,
        )

        first = suppress_redundant_read(self.c, self.root, read_file(self.root, path=path))
        self.assertNotIn("already_visible", first.data)
        self.assertEqual(first.data["content"], raw.decode().rstrip("\n"))
        record_progress(self.c, first)
        record_result(self.c, first)
        repeated = suppress_redundant_read(self.c, self.root, read_file(self.root, path=path))
        self.assertTrue(repeated.data["already_visible"])
        self.assertNotIn("content", repeated.data)

    def test_contained_reads_reuse_written_full_source_without_false_read_provenance(self):
        path = "employer_jobs.py"
        body = "".join(f"LINE_{i:03d} = {i}\n" for i in range(1, 190))
        raw = body.encode()
        (self.root / path).write_bytes(raw)
        record_result(
            self.c,
            ToolExecutionResult(tool_call_id="write", name="write_files", ok=True, summary="saved"),
            updates={path: raw},
        )
        first = read_file(self.root, path=path, start_line=1, end_line=40)
        record_progress(self.c, first)
        record_result(self.c, first)
        second = read_file(self.root, path=path, start_line=40, end_line=60)
        record_progress(self.c, second)
        record_result(self.c, second)

        fragments = self.c["source_evidence"][path]["fragments"]
        self.assertEqual(len(fragments), 1)
        self.assertEqual((fragments[0]["start_line"], fragments[0]["end_line"]), (1, 189))
        self.assertFalse(fragments[0]["explicit_read"])
        self.assertEqual(fragments[0]["explicit_read_ranges"], [[1, 60]])
        self.assertEqual(len(working_context(self.c, self.root)["source_evidence"]), 1)
        self.assertTrue(
            suppress_redundant_read(
                self.c, self.root, read_file(self.root, path=path, start_line=1, end_line=40)
            ).data["already_visible"]
        )
        self.assertNotIn(
            "already_visible",
            suppress_redundant_read(self.c, self.root, read_file(self.root, path=path)).data,
        )

        restored = load_checkpoint(json.loads(json.dumps({"checkpoint": self.c})))
        self.assertEqual(len(working_context(restored, self.root)["source_evidence"]), 1)
        tail = read_file(self.root, path=path, start_line=61, end_line=189)
        record_progress(restored, tail)
        record_result(restored, tail)
        self.assertEqual(len(restored["source_evidence"][path]["fragments"]), 1)
        self.assertTrue(restored["source_evidence"][path]["fragments"][0]["explicit_read"])
        self.assertTrue(
            suppress_redundant_read(restored, self.root, read_file(self.root, path=path)).data[
                "already_visible"
            ]
        )

    def test_existing_checkpoint_compacts_overlapping_source_on_projection(self):
        path = "employer_jobs.py"
        body = "".join(f"LINE_{i:03d} = {i}\n" for i in range(1, 190))
        raw = body.encode()
        (self.root / path).write_bytes(raw)
        record_result(
            self.c,
            ToolExecutionResult(tool_call_id="write", name="write_files", ok=True, summary="saved"),
            updates={path: raw},
        )
        full = dict(self.c["source_evidence"][path]["fragments"][0])
        partial = read_file(self.root, path=path, start_line=1, end_line=40)
        self.c["source_evidence"][path]["fragments"] = [
            full,
            {**partial.data, "explicit_read": True, "work_item_id": "auth", "model_turn": 2},
        ]
        projected = working_context(self.c, self.root)["source_evidence"]
        self.assertEqual(len(projected), 1)
        self.assertEqual(projected[0]["explicit_read_ranges"], [[1, 40]])
        self.assertEqual(len(self.c["source_evidence"][path]["fragments"]), 1)

    def test_current_item_read_promotes_cached_file_ahead_of_older_large_source(self):
        self.c["model_turns"] = 5
        self.read("App.tsx", "a" * 4000)
        self.c["model_turns"] = 10
        self.read("forgeai.smoke.json", "s" * 12000)
        self.c["current_work_item_id"] = "browse"
        self.c["model_turns"] = 20
        record_result(self.c, read_file(self.root, path="App.tsx"))
        self.c["model_turns"] = 21
        self.read("jobs.py", "j" * 12000)

        app = self.c["source_evidence"]["App.tsx"]
        self.assertEqual((app["work_item_id"], app["model_turn"]), ("browse", 20))
        context = working_context(self.c, self.root)
        self.assertIn("App.tsx", [row["path"] for row in context["source_evidence"]])
        self.assertIn("forgeai.smoke.json", context["omitted_evidence_paths"])

    def test_large_smoke_does_not_displace_small_current_item_source(self):
        paths = [f"feature{i}.py" for i in range(5)]
        for path in paths:
            self.read(path, "x" * 4000)
        self.read("forgeai.smoke.json", "s" * 12000)

        context = working_context(self.c, self.root)
        projected = {entry["path"] for entry in context["source_evidence"]}
        self.assertTrue(set(paths).issubset(projected))
        self.assertNotIn("forgeai.smoke.json", projected)
        self.assertIn("forgeai.smoke.json", context["omitted_evidence_paths"])
        self.assertTrue(
            any(
                entry["path"] == "forgeai.smoke.json"
                for entry in context["omitted_evidence_ranges"]
            )
        )
        self.assertLessEqual(
            sum(len(entry["content"]) for entry in context["source_evidence"]), 24000
        )
        reread = suppress_redundant_read(
            self.c, self.root, read_file(self.root, path="forgeai.smoke.json")
        )
        self.assertNotIn("already_visible", reread.data)

        (self.root / paths[0]).write_text("changed", encoding="utf-8")
        stale = working_context(self.c, self.root)
        self.assertIn(paths[0], stale["stale_evidence_paths"])
        self.assertNotIn(paths[0], {entry["path"] for entry in stale["source_evidence"]})

    def test_current_edits_outrank_smaller_current_item_dependencies(self):
        changed = {
            "backend/app/api/employer_jobs.py": 9400,
            "frontend/src/pages/employer-jobs.tsx": 8200,
            "frontend/src/router.tsx": 2200,
        }
        for path, size in changed.items():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            raw = b"x" * size
            target.write_bytes(raw)
            record_result(
                self.c,
                ToolExecutionResult(
                    tool_call_id="write", name="write_files", ok=True, summary="written"
                ),
                updates={path: raw},
            )
        for index in range(5):
            self.read(f"ui{index}.tsx", "u" * 1500)
        self.read("forgeai.smoke.json", "s" * 12000)

        context = working_context(self.c, self.root)
        projected = {entry["path"] for entry in context["source_evidence"]}
        self.assertTrue(set(changed).issubset(projected))
        self.assertIn("forgeai.smoke.json", context["omitted_evidence_paths"])
        self.assertLessEqual(
            sum(len(entry["content"]) for entry in context["source_evidence"]), 24000
        )

    def test_legacy_read_replay_keeps_historical_file_priority(self):
        self.c["model_turns"] = 5
        read = self.read("App.tsx", "old source")
        self.c.update(schema_version=15, current_work_item_id="browse", model_turns=100)
        self.c["observations"] = [read.model_copy(update={"model_turn": 7}).model_dump(mode="json")]
        loaded = load_checkpoint(json.loads(json.dumps({"checkpoint": self.c})))
        app = loaded["source_evidence"]["App.tsx"]
        self.assertEqual((app["work_item_id"], app["model_turn"]), ("auth", 7))

        without_cache = json.loads(json.dumps(self.c))
        without_cache["source_evidence"] = {}
        recovered = load_checkpoint({"checkpoint": without_cache})
        app = recovered["source_evidence"]["App.tsx"]
        self.assertIsNone(app["work_item_id"])
        self.assertEqual(app["model_turn"], 7)

    def test_redundant_read_guidance_precedes_action_window(self):
        path = "router.py"
        (self.root / path).write_text("api_router = 1\n", encoding="utf-8")
        first = read_file(self.root, path=path)
        record_progress(self.c, first)
        record_result(self.c, first)
        self.assertIsNone(progress_guidance(self.c))

        for count in (1, 2):
            repeated = suppress_redundant_read(self.c, self.root, read_file(self.root, path=path))
            self.assertTrue(repeated.data["already_visible"])
            record_progress(self.c, repeated)
            guidance = progress_guidance(self.c)
            if count == 1:
                self.assertIsNone(guidance)
            else:
                self.assertEqual(guidance["repeated_source_reads"], 2)
            self.assertFalse(implementation_stalled(self.c))

        for _ in range(2):
            record_progress(self.c, suppress_redundant_read(self.c, self.root, first))
        self.assertTrue(inspection_action_required(self.c))
        self.assertFalse(implementation_stalled(self.c))

    def test_truncated_read_names_next_range_without_claiming_unseen_lines(self):
        body = "".join(f"{i:03d}:" + "x" * 95 + "\n" for i in range(200))
        (self.root / "large.py").write_text(body, encoding="utf-8")
        first = read_file(self.root, path="large.py", start_line=1, end_line=200)
        record_result(self.c, first)
        fragment = working_context(self.c, self.root)["source_evidence"][0]
        self.assertEqual((fragment["end_line"], fragment["requested_end_line"]), (120, 200))
        self.assertEqual(fragment["next_start_line"], 121)

        repeated = suppress_redundant_read(self.c, self.root, first)
        self.assertTrue(repeated.data["already_visible"])
        self.assertEqual(repeated.data["next_start_line"], 121)
        changed_request = read_file(self.root, path="large.py", start_line=1, end_line=199)
        repeated_changed_request = suppress_redundant_read(self.c, self.root, changed_request)
        self.assertTrue(repeated_changed_request.data["already_visible"])
        self.assertEqual(repeated_changed_request.data["next_start_line"], 121)
        tail = read_file(self.root, path="large.py", start_line=121, end_line=200)
        self.assertNotIn("already_visible", suppress_redundant_read(self.c, self.root, tail).data)
        record_result(self.c, tail)
        fully_covered = suppress_redundant_read(self.c, self.root, first)
        self.assertTrue(fully_covered.data["already_visible"])
        self.assertNotIn("next_start_line", fully_covered.data)

        spec = AppSpec.model_validate(valid_spec())
        messages = build_code_engineer_messages(
            spec=spec,
            work_item=WorkItem(id="auth", title="Auth", requirement_ids=[spec.features[0].id]),
            system_design=None,
            engineering_context={"working_memory": working_context(self.c, self.root)},
            observations=[first],
        )
        native_content = next(m["content"] for m in messages if m["role"] == "tool")
        self.assertLessEqual(len(native_content), 20000)
        native = json.loads(native_content)
        self.assertEqual(native["data"]["end_line"], 120)
        self.assertEqual(native["data"]["requested_end_line"], 200)
        self.assertEqual(native["data"]["next_start_line"], 121)
        self.assertTrue(native["data"]["content_truncated"])

    def test_truncated_read_counts_complete_leading_blank_line(self):
        body = "\n" + "x" * 13000
        (self.root / "large.py").write_text(body, encoding="utf-8")
        first = read_file(self.root, path="large.py", start_line=1, end_line=2)
        record_result(self.c, first)
        fragment = working_context(self.c, self.root)["source_evidence"][0]
        self.assertEqual(fragment["content"], "\n")
        self.assertEqual(fragment["end_line"], 1)
        self.assertEqual(fragment["next_start_line"], 2)

    def test_oversized_single_line_explains_range_read_limit(self):
        (self.root / "minified.js").write_text("x" * 13000, encoding="utf-8")
        first = read_file(self.root, path="minified.js", start_line=1, end_line=1)
        record_result(self.c, first)
        fragment = working_context(self.c, self.root)["source_evidence"][0]
        self.assertTrue(fragment["line_too_long"])
        self.assertNotIn("next_start_line", fragment)
        repeated = suppress_redundant_read(self.c, self.root, first)
        self.assertTrue(repeated.data["already_visible"])
        self.assertNotIn("next_start_line", repeated.data)
        self.assertIn("按行重读无法显示剩余内容", repeated.summary)

    def test_escaped_source_keeps_structured_visible_range_in_native_result(self):
        body = "\n".join([r"\"" * 55] * 110)
        (self.root / "escaped.ts").write_text(body, encoding="utf-8")
        first = read_file(self.root, path="escaped.ts", start_line=1, end_line=110)
        record_result(self.c, first)
        fragment = working_context(self.c, self.root)["source_evidence"][0]
        self.assertLess(fragment["end_line"], 110)
        spec = AppSpec.model_validate(valid_spec())
        messages = build_code_engineer_messages(
            spec=spec,
            work_item=WorkItem(id="auth", title="Auth", requirement_ids=[spec.features[0].id]),
            system_design=None,
            engineering_context={"working_memory": working_context(self.c, self.root)},
            observations=[first],
        )
        native_content = next(m["content"] for m in messages if m["role"] == "tool")
        self.assertLessEqual(len(native_content), 20000)
        native = json.loads(native_content)
        self.assertIn("content", native["data"])
        self.assertNotIn("excerpt", native["data"])
        self.assertEqual(native["data"]["end_line"], fragment["end_line"])
        self.assertEqual(native["data"]["next_start_line"], fragment["next_start_line"])

    def test_long_read_path_cannot_collapse_native_result_to_excerpt(self):
        path = "segment/" * 400 + "escaped.ts"
        body = "\n".join([r"\"" * 55] * 110)
        observation = ToolExecutionResult(
            tool_call_id="long_path",
            name="read_file",
            ok=True,
            summary=f"读取 {path} 第 1-110 行",
            data={
                "path": path,
                "content_hash": "h",
                "start_line": 1,
                "end_line": 110,
                "line_count": 110,
                "content": body,
            },
            arguments={"path": path},
        )
        spec = AppSpec.model_validate(valid_spec())
        messages = build_code_engineer_messages(
            spec=spec,
            work_item=WorkItem(id="auth", title="Auth", requirement_ids=[spec.features[0].id]),
            system_design=None,
            engineering_context={},
            observations=[observation],
        )
        native_content = next(m["content"] for m in messages if m["role"] == "tool")
        self.assertLessEqual(len(native_content), 20000)
        native = json.loads(native_content)
        self.assertIn("content", native["data"])
        self.assertIn("end_line", native["data"])
        self.assertNotIn("excerpt", native["data"])
        assistant_call = next(m["tool_calls"] for m in messages if m["role"] == "assistant")
        self.assertEqual(json.loads(assistant_call[0]["function"]["arguments"])["path"], path)

    def test_legacy_truncated_cache_does_not_suppress_new_full_read(self):
        body = "\n".join(f"const line{i} = '{'x' * 23}'" for i in range(240))
        (self.root / "JobsPage.tsx").write_text(body, encoding="utf-8")
        first = read_file(self.root, path="JobsPage.tsx", start_line=1, end_line=240)
        self.c["source_evidence"] = {
            "JobsPage.tsx": {
                "content_hash": first.data["content_hash"],
                "work_item_id": "auth",
                "model_turn": 1,
                "fragments": [
                    {
                        **first.data,
                        "content": body[:6000],
                        "content_truncated": True,
                        "work_item_id": "auth",
                        "model_turn": 1,
                    }
                ],
            }
        }
        restored = load_checkpoint({"checkpoint": self.c})
        visible = working_context(restored, self.root)["source_evidence"][0]
        self.assertLess(visible["end_line"], 240)
        self.assertEqual(visible["requested_end_line"], 240)
        self.assertNotIn(
            "already_visible", suppress_redundant_read(restored, self.root, first).data
        )
        record_result(restored, first)
        self.assertEqual(
            working_context(restored, self.root)["source_evidence"][0]["end_line"], 240
        )
        self.assertTrue(suppress_redundant_read(restored, self.root, first).data["already_visible"])

    def test_guidance_survives_recovery_and_has_grace_before_stopping(self):
        observation = self.read()
        for _ in range(32):
            record_progress(self.c, observation)
        self.assertIsNotNone(progress_guidance(self.c))
        self.assertFalse(implementation_stalled(self.c))
        self.c.update(progress_guidance_turn=10, model_turns=12)
        loaded = load_checkpoint(json.loads(json.dumps({"checkpoint": self.c})))
        self.assertFalse(implementation_stalled(loaded))
        loaded["model_turns"] = 13
        self.assertTrue(implementation_stalled(loaded))
        record_progress(loaded, observation, changed=True)
        self.assertIsNone(progress_guidance(loaded))
        self.assertFalse(implementation_stalled(loaded))

    def test_new_reads_do_not_hide_missing_implementation_and_cached_check_is_not_progress(self):
        for i in range(16):
            self.c["model_turns"] = i + 1
            record_progress(self.c, self.read(path=f"new_{i}.py"))
        self.assertEqual(self.c["no_progress_steps"], 0)
        self.assertTrue(inspection_action_required(self.c))
        self.assertFalse(implementation_stalled(self.c))
        check = ToolExecutionResult(
            tool_call_id="c",
            name="run_check",
            ok=True,
            summary="pass",
            data={"check_id": "quick", "source_hash": "checked"},
        )
        record_progress(self.c, check)
        self.assertIsNone(progress_guidance(self.c))
        self.assertFalse(inspection_action_required(self.c))
        for _ in range(12):
            record_progress(self.c, check)
        self.assertIsNotNone(progress_guidance(self.c))

    def test_inspection_window_counts_model_decisions_not_files_and_resets_on_progress(self):
        self.c["model_turns"] = 1
        for index in range(16):
            record_progress(self.c, self.read(path=f"batch_{index}.py"))
        self.assertEqual(item_memory(self.c)["inspection_model_turns"], 1)
        self.assertFalse(inspection_action_required(self.c))
        for turn in range(2, 17):
            self.c["model_turns"] = turn
            record_progress(self.c, self.read(path=f"next_{turn}.py"))
        self.assertEqual(item_memory(self.c)["inspection_model_turns"], 16)
        self.assertTrue(inspection_action_required(self.c))
        self.assertTrue(progress_guidance(self.c)["inspection_action_required"])

        record_progress(
            self.c,
            ToolExecutionResult(tool_call_id="write", name="write_files", ok=True, summary="saved"),
            changed=True,
        )
        self.assertEqual(item_memory(self.c)["inspection_model_turns"], 0)
        self.assertFalse(inspection_action_required(self.c))
        self.c["model_turns"] = 17
        record_progress(self.c, self.read(path="after_write.py"))
        self.assertEqual(item_memory(self.c)["inspection_model_turns"], 1)
        record_progress(
            self.c,
            ToolExecutionResult(
                tool_call_id="plan", name="update_delivery_plan", ok=True, summary="replanned"
            ),
            plan_changed=True,
        )
        self.assertEqual(item_memory(self.c)["inspection_model_turns"], 0)

    def test_redundant_reads_require_action_before_the_stop_loss(self):
        memory = item_memory(self.c)
        memory["inspection_model_turns"] = 5
        memory["redundant_read_steps"] = 4
        self.c.update(
            model_turns=25,
            no_progress_steps=10,
            steps_without_delivery=10,
            progress_guidance_turn=22,
        )
        self.assertTrue(inspection_action_required(self.c))
        self.assertTrue(progress_guidance(self.c)["inspection_action_required"])
        self.assertFalse(implementation_stalled(self.c))

        # The existing no-progress limit remains available if the next
        # constrained decisions still do not produce a change or check.
        self.c["no_progress_steps"] = 24
        self.assertTrue(implementation_stalled(self.c))

    def test_cache_omitted_reads_require_action_before_generic_stop(self):
        memory = item_memory(self.c)
        memory["inspection_model_turns"] = 8
        memory["redundant_read_steps"] = 0
        self.c.update(no_progress_steps=12, steps_without_delivery=12)
        self.assertTrue(inspection_action_required(self.c))
        self.assertFalse(implementation_stalled(self.c))

    def test_complementary_ranges_survive_alternation_and_history_rollover(self):
        path = "large.py"
        body = "".join(f"LINE_{i:03d} = {i}\n" for i in range(1, 312))
        (self.root / path).write_text(body, encoding="utf-8")
        observations = []
        for start, end in [(1, 120), (240, 311)] * 8:
            result = read_file(self.root, path=path, start_line=start, end_line=end).model_copy(
                update={"arguments": {"path": path, "start_line": start, "end_line": end}}
            )
            record_result(self.c, result)
            observations.append(result)
        context = working_context(self.c, self.root)
        spec = AppSpec.model_validate(valid_spec())
        messages = build_code_engineer_messages(
            spec=spec,
            work_item=WorkItem(id="auth", title="Auth", requirement_ids=[spec.features[0].id]),
            system_design=None,
            engineering_context={"working_memory": context},
            observations=observations,
        )
        native = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        self.assertEqual(len(native), 2)
        self.assertIn("LINE_001", str(native))
        self.assertIn("LINE_311", str(native))
        restored = load_checkpoint(json.loads(json.dumps({"checkpoint": self.c})))
        evidence = working_context(restored, self.root)["source_evidence"]
        self.assertEqual(
            {(e["start_line"], e["end_line"]) for e in evidence}, {(1, 120), (240, 311)}
        )
        self.assertIn("LINE_001", str(evidence))
        self.assertIn("LINE_311", str(evidence))

    def range_read(self, start, end):
        result = read_file(self.root, path="large.py", start_line=start, end_line=end).model_copy(
            update={"arguments": {"path": "large.py", "start_line": start, "end_line": end}}
        )
        record_result(self.c, result)
        return result

    def test_new_source_version_replaces_all_cached_ranges(self):
        (self.root / "large.py").write_text("OLD_HEAD\nmiddle\nOLD_TAIL\n", encoding="utf-8")
        old = [self.range_read(1, 1), self.range_read(3, 3)]
        (self.root / "large.py").write_text("NEW_HEAD\nmiddle\nNEW_TAIL\n", encoding="utf-8")
        # An external change is not silently merged with the prior version.
        new = self.range_read(1, 1)
        context = working_context(self.c, self.root)
        self.assertNotIn("OLD_", str(context["source_evidence"]))
        self.assertNotIn("NEW_TAIL", str(context["source_evidence"]))
        spec = AppSpec.model_validate(valid_spec())
        messages = build_code_engineer_messages(
            spec=spec,
            work_item=WorkItem(id="auth", title="Auth", requirement_ids=[spec.features[0].id]),
            system_design=None,
            engineering_context={"working_memory": context},
            observations=[*old, new],
        )
        native = [m["content"] for m in messages if m["role"] == "tool"]
        self.assertEqual(len(native), 1)
        self.assertNotIn("OLD_", str(native))
        self.assertIn("NEW_HEAD", str(native))

    def test_range_cache_and_model_context_remain_bounded(self):
        body = "".join(f"{i:03d}:" + "x" * 96 + "\n" for i in range(600))
        (self.root / "large.py").write_text(body, encoding="utf-8")
        for start in range(1, 600, 50):
            self.range_read(start, start + 49)
        fragments = self.c["source_evidence"]["large.py"]["fragments"]
        self.assertLessEqual(sum(len(e["content"]) for e in fragments), 24000)
        for i in range(8):
            self.read(f"other{i}.py", "y" * 6000)
        context = working_context(self.c, self.root)
        self.assertLessEqual(sum(len(e["content"]) for e in context["source_evidence"]), 24000)
        self.assertTrue(context["omitted_evidence_ranges"])
        # Version knowledge survives omission from the text budget.
        self.assertIn("large.py", context["source_hashes"])

    def test_legacy_range_migration_recovers_history_and_preserves_stop_state(self):
        (self.root / "large.py").write_text("HEAD\nmiddle\nTAIL\n", encoding="utf-8")
        head, tail = self.range_read(1, 1), self.range_read(3, 3)
        self.c.update(
            schema_version=15,
            outcome="blocked",
            model_turns=72,
            tool_calls=97,
            blocked_reason_code="MODEL_BUDGET_EXHAUSTED",
        )
        # Schema 15 kept only the latest range for each path.
        self.c["source_evidence"] = {"large.py": {**tail.data, "model_turn": 71}}
        self.c["observations"] = [head.model_dump(mode="json"), tail.model_dump(mode="json")]
        loaded = load_checkpoint({"checkpoint": self.c})
        context = working_context(loaded, self.root)
        self.assertEqual(
            {(e["start_line"], e["end_line"]) for e in context["source_evidence"]}, {(1, 1), (3, 3)}
        )
        self.assertEqual(
            (loaded["model_turns"], loaded["tool_calls"], loaded["outcome"]), (72, 97, "blocked")
        )
        self.assertEqual(load_checkpoint({"checkpoint": loaded}), loaded)

    def test_legacy_migration_does_not_revive_reads_before_a_write(self):
        old = self.read("large.py", "OLD_HEAD\nOLD_TAIL\n")
        raw = b"NEW_HEAD\nNEW_TAIL\n"
        (self.root / "large.py").write_bytes(raw)
        self.c.update(
            schema_version=15,
            observations=[old.model_dump(mode="json")],
            observed_hashes={"large.py": sha256_bytes(raw)},
        )
        self.c["source_evidence"] = {
            "large.py": {
                "path": "large.py",
                "content": raw.decode(),
                "content_hash": sha256_bytes(raw),
                "start_line": 1,
                "end_line": 2,
                "line_count": 2,
            }
        }
        loaded = load_checkpoint({"checkpoint": self.c})
        self.assertNotIn("OLD_", str(working_context(loaded, self.root)))
        self.assertIn("NEW_HEAD", str(working_context(loaded, self.root)))

    def test_failed_read_feedback_survives_with_valid_ranges(self):
        success = self.read("large.py", "HEAD\nTAIL\n")
        failure = read_file(self.root, path="large.py", start_line=10, end_line=20)
        spec = AppSpec.model_validate(valid_spec())
        messages = build_code_engineer_messages(
            spec=spec,
            work_item=WorkItem(id="auth", title="Auth", requirement_ids=[spec.features[0].id]),
            system_design=None,
            engineering_context={"working_memory": working_context(self.c, self.root)},
            observations=[success, failure],
        )
        native = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        self.assertEqual(len(native), 2)
        self.assertTrue(any(not e["ok"] for e in native))

    def test_evidence_and_progress_survive_rollover_and_recovery(self):
        self.read()
        result = ToolExecutionResult(
            tool_call_id="write",
            name="write_file",
            ok=True,
            summary="Created auth.py",
            arguments={"path": "auth.py"},
        )
        raw = b"def login(): pass\n"
        (self.root / "auth.py").write_bytes(raw)
        record_result(self.c, result, updates={"auth.py": raw})
        self.c["observations"] = []  # Earlier exchanges no longer fit in rolling history.
        restored = load_checkpoint(json.loads(json.dumps({"checkpoint": self.c})))
        context = working_context(restored, self.root)
        self.assertEqual(context["changed_files"], {"auth.py": sha256_bytes(raw)})
        self.assertEqual({x["path"] for x in context["source_evidence"]}, {"auth.py", "user.py"})
        self.assertEqual(restored["outcome"], "running")

    def test_external_change_or_deletion_never_reuses_stale_source(self):
        for deleted in [False, True]:
            self.read()
            if deleted:
                (self.root / "user.py").unlink()
            else:
                (self.root / "user.py").write_text("VALUE = 2", encoding="utf-8")
            context = working_context(self.c, self.root)
            self.assertEqual(context["source_evidence"], [])
            self.assertEqual(context["stale_evidence_paths"], ["user.py"])

    def test_committed_write_replaces_cached_source_and_hash(self):
        self.read()
        raw = b"VALUE = 2"
        (self.root / "user.py").write_bytes(raw)
        result = ToolExecutionResult(
            tool_call_id="edit", name="edit_file_by_replace", ok=True, summary="Changed user.py"
        )
        record_result(self.c, result, updates={"user.py": raw})
        entry = working_context(self.c, self.root)["source_evidence"][0]
        self.assertEqual(entry["content"], "VALUE = 2")
        self.assertEqual(entry["content_hash"], sha256_bytes(raw))

    def test_failed_write_does_not_record_uncommitted_bytes(self):
        self.read()
        result = ToolExecutionResult(
            tool_call_id="edit",
            name="write_file",
            ok=False,
            error_code="WRITE_CONFLICT",
            summary="stale hash",
        )
        record_result(self.c, result, updates={"user.py": b"WRONG"})
        self.assertEqual(item_memory(self.c)["changed_files"], {})
        self.assertNotIn("WRONG", str(working_context(self.c, self.root)))

    def test_repeated_reads_cannot_evict_written_evidence_and_size_is_bounded(self):
        raw = b"IMPORTANT = 1"
        (self.root / "auth.py").write_bytes(raw)
        record_result(
            self.c,
            ToolExecutionResult(tool_call_id="w", name="write_file", ok=True, summary="written"),
            updates={"auth.py": raw},
        )
        for i in range(30):
            self.read(f"f{i}.py", "x" * 16000)
        self.assertLessEqual(len(self.c["source_evidence"]), 12)
        context = working_context(self.c, self.root)
        self.assertLessEqual(sum(len(x["content"]) for x in context["source_evidence"]), 24000)
        self.assertIn("auth.py", [x["path"] for x in context["source_evidence"]])
        self.assertTrue(context["omitted_evidence_paths"])
        self.assertTrue(any(x["content_truncated"] for x in context["source_evidence"]))

    def test_legacy_focus_upgrade_keeps_audit_and_does_not_resume_or_reset(self):
        for phase in ["awaiting_focus", "acting", "exhausted"]:
            checkpoint = new_checkpoint()
            checkpoint.update(
                schema_version=14,
                current_work_item_id="auth",
                outcome="blocked",
                blocked_reason_code="NO_SOURCE_PROGRESS",
                model_turns=40,
                tool_calls=90,
            )
            item_memory(checkpoint).update(
                correction={"phase": phase, "read_calls_remaining": 0},
                correction_used=True,
            )
            checkpoint["pending_tool"] = {"name": "update_work_item_focus"}
            item_memory(checkpoint)["recent_actions"] = [
                {"tool": "update_work_item_focus", "summary": "old restriction"},
                {"tool": "model_response", "summary": "old refusal"},
                {"tool": "read_file", "summary": "current source"},
            ]
            loaded = load_checkpoint({"checkpoint": checkpoint})
            context = working_context(loaded, self.root)
            self.assertNotIn("correction", context)
            self.assertEqual([a["tool"] for a in context["recent_actions"]], ["read_file"])
            self.assertNotIn("pending_tool", loaded)
            self.assertEqual(loaded["legacy_pending_tool"]["name"], "update_work_item_focus")
            self.assertEqual(item_memory(loaded)["legacy_correction"]["phase"], phase)
            self.assertEqual(loaded["outcome"], "blocked")
            self.assertEqual((loaded["model_turns"], loaded["tool_calls"]), (40, 90))
            self.assertEqual(load_checkpoint({"checkpoint": loaded}), loaded)

    def test_explicit_stall_retry_keeps_evidence_without_forced_focus(self):
        self.read()
        item_memory(self.c)["redundant_read_steps"] = 4
        self.c.update(
            outcome="blocked",
            blocked_reason_code="NO_SOURCE_PROGRESS",
            blocked_reason="Repeated reads",
            model_turns=40,
            tool_calls=90,
        )
        self.assertEqual(
            item_memory(load_checkpoint({"checkpoint": self.c}))["redundant_read_steps"], 4
        )
        retried = prepare_manual_repair_retry({"checkpoint": self.c})["checkpoint"]
        self.assertEqual((retried["model_turns"], retried["tool_calls"]), (40, 90))
        self.assertIn("user.py", retried["source_evidence"])
        self.assertNotIn("correction", working_context(retried, self.root))
        self.assertTrue(retried["recovery_context"]["historical"])
        self.assertEqual(item_memory(retried)["redundant_read_steps"], 0)
        self.assertEqual(retried["outcome"], "running")

    def test_work_item_switch_keeps_memory_separate(self):
        self.read()
        self.c["current_work_item_id"] = "jobs"
        self.assertEqual(item_memory(self.c)["inspection_steps"], 0)
        self.assertEqual(item_memory(self.c)["changed_files"], {})

    def test_messages_supply_evidence_once_and_remove_stale_reads(self):
        result = self.read()
        spec = AppSpec.model_validate(valid_spec())
        item = WorkItem(id="auth", title="Auth", requirement_ids=[spec.features[0].id])
        for stale in [False, True]:
            if stale:
                (self.root / "user.py").unlink()
            context = working_context(self.c, self.root)
            messages = build_code_engineer_messages(
                spec=spec,
                work_item=item,
                system_design=None,
                engineering_context={"working_memory": context},
                observations=[result] * 24,
            )
            self.assertEqual(sum(m["role"] == "tool" for m in messages), 0 if stale else 1)
            payload = json.loads(messages[-1]["content"])
            self.assertEqual(
                len(payload["engineering_context"]["working_memory"]["source_evidence"]),
                0 if stale else 1,
            )

    def test_old_read_exchange_is_not_replayed_after_committed_change(self):
        old = self.read()
        raw = b"VALUE = 2"
        (self.root / "user.py").write_bytes(raw)
        record_result(
            self.c,
            ToolExecutionResult(
                tool_call_id="w", name="write_file", ok=True, summary="Changed user"
            ),
            updates={"user.py": raw},
        )
        spec = AppSpec.model_validate(valid_spec())
        item = WorkItem(id="auth", title="Auth", requirement_ids=[spec.features[0].id])
        messages = build_code_engineer_messages(
            spec=spec,
            work_item=item,
            system_design=None,
            engineering_context={"working_memory": working_context(self.c, self.root)},
            observations=[old],
        )
        self.assertFalse(any(m["role"] == "tool" for m in messages))
        self.assertIn("VALUE = 2", messages[-1]["content"])

    def test_old_checkpoint_upgrade_preserves_terminal_stop_and_budgets(self):
        self.c.update(
            schema_version=13,
            outcome="blocked",
            blocked_reason_code="NO_SOURCE_PROGRESS",
            model_turns=166,
            tool_calls=250,
        )
        del self.c["work_memory"]
        del self.c["source_evidence"]
        loaded = load_checkpoint({"checkpoint": self.c})
        self.assertEqual(loaded["schema_version"], 17)
        self.assertEqual(loaded["outcome"], "blocked")
        self.assertEqual((loaded["model_turns"], loaded["tool_calls"]), (166, 250))


if __name__ == "__main__":
    unittest.main()
