"""Integration tests for the durable evidence-driven engineering executor."""

import json
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified
from test_engineering_claim import EngineeringClaimTests
from test_product_manager_workflow import valid_spec

from app.agents.code_engineer import CODE_ENGINEER_PROFILE, CODE_ENGINEER_TOOLS, decide_next_action
from app.core.exceptions import BusinessException, ProviderException
from app.generation.delivery import planning_item
from app.generation.engineering.checkpoint import (
    EXECUTION_MODE,
    load_checkpoint,
    new_checkpoint,
    prepare_manual_repair_retry,
    resume_blocked_checkpoint,
)
from app.generation.workspace import default_workspace_path
from app.models.task_execution import TaskExecution
from app.models.workflow_event import WorkflowEvent
from app.orchestration.code_engineer import run_engineering_workflow
from app.schemas.agent_action import ChatWithToolsResult, ToolCall, ToolExecutionResult
from app.schemas.app_spec import AppSpec
from app.services.engineering import read_frozen_input_snapshot
from app.services.engineering.completion import complete_code_engineer_task
from app.tools.checks import CHECK_RUNTIME_VERSION, source_snapshot


def turn(name, **arguments):
    return ChatWithToolsResult(tool_calls=[ToolCall(id="action", name=name, arguments=arguments)])


class EngineeringLoopTests(EngineeringClaimTests):
    def test_thinking_response_survives_provider_interruption_in_checkpoint(self):
        self._start()
        calls = 0

        def decide(*, work_item, observations, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ProviderException("fixture interruption", retryable=True)
            return self._implement(work_item=work_item, observations=observations).model_copy(
                update={"reasoning_content": "opaque checkpoint fixture"}
            )

        self.model.side_effect = decide
        with self.assertRaises(ProviderException):
            self._run()
        checkpoint = self._checkpoint()
        saved = checkpoint["model_responses"][0]
        self.assertEqual(saved["model_turn"], 1)
        self.assertEqual(saved["result"]["reasoning_content"], "opaque checkpoint fixture")
        self.assertEqual(checkpoint["observations"][-1]["model_turn"], 1)
        self.assertEqual(checkpoint["observations"][-1]["tool_call_id"], "action")
        self.assertEqual(checkpoint["model_turns"], 2)

    def test_read_batches_gather_new_context_without_stopping_before_planning(self):
        self._start()
        for index in range(18):
            (self.root / f"context_{index}.txt").write_text(f"evidence {index}", encoding="utf-8")
        reads = []

        def decide(*, observations, **kwargs):
            reads[:] = [o for o in observations if o.name == "read_file"]
            if len(reads) < 18:
                start = len(reads)
                return ChatWithToolsResult(
                    tool_calls=[
                        ToolCall(
                            id=f"read_{i}", name="read_file", arguments={"path": f"context_{i}.txt"}
                        )
                        for i in range(start, min(start + 9, 18))
                    ]
                )
            return self._implement(observations=observations, **kwargs)

        self.model.side_effect = decide
        self.assertEqual(self._run()["outcome"], "generated")
        self.assertEqual(self._checkpoint()["model_turns"], 9)
        self.assertEqual(len(self._checkpoint()["read_coverage"]), 18)

    def test_repeated_identical_read_is_still_bounded(self):
        self._start()
        # Isolate the read no-progress limit from the earlier planning tool cutoff.
        with self.session_factory() as db:
            row = db.get(TaskExecution, self.execution.execution_id)
            snapshot = read_frozen_input_snapshot(row)
            snapshot["call_budget"]["max_planning_turns"] = 20
            row.draft = snapshot
            flag_modified(row, "draft")
            db.commit()
        (self.root / "repeat.txt").write_text("same", encoding="utf-8")
        self.model.side_effect = None
        self.model.return_value = turn("read_file", path="repeat.txt")
        self.assertEqual(self._run()["outcome"], "blocked")
        self.assertEqual(self._checkpoint()["model_turns"], 13)
        self.assertEqual(self._checkpoint()["no_progress_steps"], 12)

    def test_planning_inspection_closes_then_engineer_can_implement(self):
        self._start()
        for index in range(8):
            (self.root / f"context_{index}.txt").write_text(str(index), encoding="utf-8")
        reads = 0
        constrained = False

        def decide(*, work_item, observations, tools, engineering_context, **kwargs):
            nonlocal reads, constrained
            if work_item.id == "engineering_plan":
                names = {tool.name for tool in tools}
                if "read_file" in names:
                    result = turn("read_file", path=f"context_{reads}.txt")
                    reads += 1
                    return result
                self.assertEqual(names, {"update_delivery_plan", "report_blocked"})
                self.assertEqual(engineering_context["planning_turns_remaining"], 0)
                self.assertEqual(len(engineering_context["observed_files"]), reads)
                constrained = True
            return self._implement(work_item=work_item, observations=observations)

        self.model.side_effect = decide
        with self.assertLogs("forgeai.tools", level="INFO") as logs:
            self.assertEqual(self._run()["outcome"], "generated")
        self.assertEqual(reads, 4)
        self.assertTrue(constrained)
        self.assertEqual(len(self._checkpoint()["modified_files"]), 3)
        self.assertTrue(any("tool=complete_work_item" in row for row in logs.output))
        self.assertTrue(any("tool=read_file" in row and "args_hash=" in row for row in logs.output))

    def test_budget_exhaustion_does_not_reset_on_new_execution_attempt(self):
        self._start()
        with self.session_factory() as db:
            row = db.get(TaskExecution, self.execution.execution_id)
            snapshot = read_frozen_input_snapshot(row)
            snapshot["call_budget"]["max_model_turns"] = 1
            row.draft = snapshot
            flag_modified(row, "draft")
            db.commit()
        self.assertEqual(self._run()["outcome"], "blocked")
        before = self._checkpoint()
        self.assertEqual(before["blocked_reason_code"], "MODEL_BUDGET_EXHAUSTED")
        with self.session_factory() as db:
            row = db.get(TaskExecution, self.execution.execution_id)
            row.attempt = 2
            db.commit()
        self.model.reset_mock()
        self.assertEqual(self._run()["outcome"], "blocked")
        self.model.assert_not_called()
        self.assertEqual(self._checkpoint()["model_turns"], before["model_turns"])
        self.assertEqual(self._checkpoint()["tool_calls"], before["tool_calls"])

    def test_manual_no_progress_recovery_keeps_reason_and_closes_planning_reads(self):
        self._start()
        (self.root / "repeat.txt").write_text("evidence", encoding="utf-8")
        self.model.side_effect = None
        self.model.return_value = turn("read_file", path="repeat.txt")
        self.assertEqual(self._run()["outcome"], "blocked")
        before = self._checkpoint()
        with self.session_factory() as db:
            row = db.get(TaskExecution, self.execution.execution_id)
            row.draft = prepare_manual_repair_retry(read_frozen_input_snapshot(row))
            flag_modified(row, "draft")
            db.commit()
        self.model.side_effect = self._implement
        self.model.reset_mock()
        self.assertEqual(self._run()["outcome"], "generated")
        first = self.model.call_args_list[0].kwargs
        self.assertEqual(
            {tool.name for tool in first["tools"]}, {"update_delivery_plan", "report_blocked"}
        )
        context = first["engineering_context"]
        self.assertEqual(context["recovery_context"]["reason_code"], "REPEATED_TOOL_FAILURE")
        self.assertIn("repeat.txt", context["observed_files"])
        self.assertGreater(self._checkpoint()["model_turns"], before["model_turns"])

    def test_legacy_stop_reason_is_migrated_without_reopening_budget(self):
        for reason, code in [
            ("模型调用预算已用尽", "MODEL_BUDGET_EXHAUSTED"),
            ("工具调用预算已用尽", "TOOL_BUDGET_EXHAUSTED"),
        ]:
            checkpoint = new_checkpoint()
            checkpoint.update(
                outcome="blocked",
                blocked_reason=reason,
                needs_delivery_plan=True,
                model_turns=65,
                tool_calls=72,
            )
            checkpoint.pop("planning_turns")
            loaded = load_checkpoint({"checkpoint": checkpoint})
            self.assertEqual(loaded["blocked_reason_code"], code)
            self.assertEqual(loaded["planning_turns"], 65)
            self.assertFalse(resume_blocked_checkpoint(loaded))
            retried = prepare_manual_repair_retry({"checkpoint": loaded})
            self.assertEqual(retried["checkpoint"]["model_turns"], 65)
            self.assertEqual(retried["checkpoint"]["planning_turns"], 65)
            self.assertEqual(retried["checkpoint"]["recovery_context"]["reason_code"], code)
            self.assertGreater(retried["call_budget"]["max_model_turns"], 65)

    def test_direct_check_keeps_cross_file_diagnostic_outside_observation_window(self):
        from app.orchestration.code_engineer import _EngineeringNodes

        checkpoint = new_checkpoint()
        result = ToolExecutionResult(
            tool_call_id="check",
            name="run_check",
            ok=False,
            summary="typecheck failed",
            error_code="CHECK_FAILED",
            data={
                "source_hash": "checked",
                "output": (
                    "src/routes/jobs.tsx(12,3): error TS2322: "
                    "Type '/my/applications' is not assignable to route type."
                ),
            },
        )
        _EngineeringNodes._remember_check(checkpoint, "quick", result)
        self.assertEqual(checkpoint["repair_context"]["diagnostics"][0]["code"], "TS2322")
        self.assertEqual(
            checkpoint["repair_context"]["failed_check"]["data"]["source_hash"], "checked"
        )
        self.assertEqual(checkpoint["repair_rounds"], {})
        self.assertEqual(checkpoint["work_items"], [])

    def test_provider_failure_preserves_progress_and_retry_disposition(self):
        self._start()
        self.model.side_effect = ProviderException("provider offline", retryable=True)
        with self.assertRaises(ProviderException) as failure:
            self._run()
        self.assertTrue(failure.exception.retryable)
        self.assertEqual(self._checkpoint()["outcome"], "running")
        self.assertEqual(self._checkpoint()["model_turns"], 1)
        self.model.side_effect = self._implement
        self.assertEqual(self._run()["outcome"], "generated")
        self.assertGreater(self._checkpoint()["model_turns"], 1)

    def test_agent_rejects_multi_mutation_batch_instead_of_silently_dropping_calls(self):
        spec = AppSpec.model_validate(valid_spec())
        batch = ChatWithToolsResult(
            tool_calls=[
                ToolCall(
                    id=str(i), name="write_file", arguments={"path": f"{i}.py", "content": "x"}
                )
                for i in range(2)
            ]
        )
        with patch("app.agents.code_engineer.chat_with_tools", return_value=batch):
            with self.assertRaisesRegex(BusinessException, "单独调用"):
                decide_next_action(
                    spec=spec,
                    work_item=planning_item(spec),
                    engineering_context={},
                    observations=[],
                    system_design=None,
                )

    def setUp(self):
        super().setUp()
        self.enterContext(
            patch(
                "app.orchestration.code_engineer.check_tools.check_environment",
                return_value=ToolExecutionResult(
                    tool_call_id="environment", name="check_environment", ok=True, summary="ready"
                ),
            )
        )
        self.model = self.enterContext(
            patch(
                "app.orchestration.code_engineer.decide_next_action",
                side_effect=self._implement,
            )
        )
        self.checks = self.enterContext(
            patch(
                "app.orchestration.code_engineer.check_tools.run_check",
                side_effect=self._passing_check,
            )
        )

    @staticmethod
    def _passing_check(root, *, check_id, tool_call_id):
        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            name="run_check",
            ok=True,
            summary="passed",
            data={
                "source_hash": source_snapshot(root)[1],
                "check_id": check_id,
                "runtime_version": CHECK_RUNTIME_VERSION,
            },
        )

    def test_repeated_reads_keep_tools_and_then_use_completion_gates(self):
        self._start()
        batches = 0
        observed_tools = []

        def decide(*, work_item, observations, engineering_context, tools, **kwargs):
            nonlocal batches
            if work_item.id != "visual":
                return self._implement(work_item=work_item, observations=observations)
            names = {tool.name for tool in tools}
            observed_tools.append(names)
            self.assertEqual(names, set(engineering_context["available_tools"]))
            self.assertNotIn("update_work_item_focus", names)
            if batches < 2:
                batches += 1
                return ChatWithToolsResult(
                    tool_calls=[
                        ToolCall(
                            id=f"r{batches}_{i}", name="read_file", arguments={"path": "README.md"}
                        )
                        for i in range(16)
                    ]
                )
            self.assertEqual(
                engineering_context["working_memory"]["inspection_steps"],
                32 if not any(o.name == "write_file" for o in observations) else 0,
            )
            return self._implement(work_item=work_item, observations=observations)

        (self.root / "README.md").write_text("Source evidence", encoding="utf-8")
        self.model.side_effect = decide
        self.assertEqual(self._run()["outcome"], "generated")
        self.assertTrue(all(names == observed_tools[0] for names in observed_tools))
        self.assertEqual(
            [c.kwargs["check_id"] for c in self.checks.call_args_list],
            ["quick", "all", "all"],
        )
        self.assertEqual(
            len([a for a in self._checkpoint()["activity"] if a["name"] == "read_file"]), 32
        )

    def test_repeated_implementation_reads_stop_at_total_model_budget(self):
        self._start()
        with self.session_factory() as db:
            row = db.get(TaskExecution, self.execution.execution_id)
            snapshot = read_frozen_input_snapshot(row)
            snapshot["call_budget"]["max_model_turns"] = 16
            row.draft = snapshot
            flag_modified(row, "draft")
            db.commit()
        (self.root / "README.md").write_text("evidence", encoding="utf-8")

        def decide(*, work_item, observations, tools, **kwargs):
            if work_item.id == "engineering_plan":
                return self._implement(work_item=work_item, observations=observations)
            self.assertIn("write_files", {t.name for t in tools})
            return turn("read_file", path="README.md")

        self.model.side_effect = decide
        self.assertEqual(self._run()["outcome"], "blocked")
        checkpoint = self._checkpoint()
        self.assertEqual(checkpoint["model_turns"], 16)
        self.assertEqual(checkpoint["work_memory"]["visual"]["inspection_steps"], 15)
        self.assertEqual(checkpoint["blocked_reason_code"], "MODEL_BUDGET_EXHAUSTED")

    def test_nonfinal_feature_requires_real_integration_check(self):
        self._start()

        def fail_integration(root, *, check_id, tool_call_id):
            result = self._passing_check(root, check_id=check_id, tool_call_id=tool_call_id)
            if check_id == "all":
                return result.model_copy(
                    update={
                        "ok": False,
                        "error_code": "CHECK_FAILED",
                        "summary": "business API returned 422",
                        "data": {
                            **result.data,
                            "output": "RuntimeError: API 联调失败：期望 HTTP 201，实际 422",
                        },
                    }
                )
            return result

        self.checks.side_effect = fail_integration
        self.assertEqual(self._run()["outcome"], "blocked")
        items = self._checkpoint()["work_items"]
        self.assertEqual(items[0]["status"], "completed")
        self.assertEqual(items[1]["status"], "pending")
        self.assertEqual(items[2]["status"], "pending")
        self.assertTrue(any(c.kwargs["check_id"] == "all" for c in self.checks.call_args_list))

    def test_verified_visual_provider_releases_dependent_work(self):
        self._start()

        def decide(*, work_item, observations, **kwargs):
            result = self._implement(work_item=work_item, observations=observations)
            if work_item.id == "engineering_plan":
                visual = result.tool_calls[0].arguments["items"][0]
                visual["provides_requirement_ids"] = visual["requirement_ids"][:1]
                visual["acceptance"] = ["Visual contract passes in the running app"]
                visual["deliverables"] = ["Design tokens and smoke visual contract"]
            return result

        self.model.side_effect = decide
        self.assertEqual(self._run()["outcome"], "generated")
        self.assertEqual(
            [c.kwargs["check_id"] for c in self.checks.call_args_list],
            ["all", "all", "all"],
        )
        self.assertTrue(all(i["status"] == "completed" for i in self._checkpoint()["work_items"]))

    def test_visual_prerequisite_is_not_completed_when_integration_fails(self):
        self._start()

        def decide(*, work_item, observations, **kwargs):
            result = self._implement(work_item=work_item, observations=observations)
            if work_item.id == "engineering_plan":
                visual = result.tool_calls[0].arguments["items"][0]
                visual["provides_requirement_ids"] = visual["requirement_ids"][:1]
                visual["acceptance"] = ["Visual contract passes in the running app"]
                visual["deliverables"] = ["Design tokens and smoke visual contract"]
            return result

        def fail_integration(root, *, check_id, tool_call_id):
            result = self._passing_check(root, check_id=check_id, tool_call_id=tool_call_id)
            if check_id == "all":
                return result.model_copy(
                    update={"ok": False, "error_code": "CHECK_FAILED", "summary": "Missing tokens"}
                )
            return result

        self.model.side_effect = decide
        self.checks.side_effect = fail_integration
        self.assertEqual(self._run()["outcome"], "blocked")
        items = self._checkpoint()["work_items"]
        self.assertTrue(all(i["status"] == "pending" for i in items))
        self.assertEqual([c.kwargs["check_id"] for c in self.checks.call_args_list], ["all"])

    def test_repeated_implementation_reads_get_guidance_then_stop_without_tool_ban(self):
        self._start()
        (self.root / "README.md").write_text("same evidence", encoding="utf-8")
        guided = []

        def decide(*, work_item, observations, tools, engineering_context, **kwargs):
            if work_item.id == "engineering_plan":
                return self._implement(work_item=work_item, observations=observations)
            self.assertIn("read_file", {t.name for t in tools})
            self.assertIn("write_files", {t.name for t in tools})
            if engineering_context["progress_guidance"]:
                guided.append(engineering_context["progress_guidance"])
            return turn("read_file", path="README.md", end_line=len(guided) + 10)

        self.model.side_effect = decide
        self.assertEqual(self._run()["outcome"], "blocked")
        self.assertGreaterEqual(len(guided), 3)
        checkpoint = self._checkpoint()
        self.assertEqual(checkpoint["blocked_reason_code"], "NO_SOURCE_PROGRESS")
        self.assertLess(checkpoint["tool_calls"], 120)

    def test_valid_model_call_resolves_protocol_refusal_and_keeps_audit(self):
        self._start()
        attempts = 0

        def decide(*, work_item, observations, engineering_context, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return turn("retired_tool")
            if attempts == 2:
                diagnostic = engineering_context["active_diagnostics"][0]["diagnostic"]
                self.assertIn("available_tools", diagnostic["verification"])
                self.assertNotIn("重新执行 model_response", diagnostic["verification"])
            else:
                self.assertFalse(
                    any(
                        d["scope"] == "tool:model_response:"
                        for d in engineering_context["active_diagnostics"]
                    )
                )
            return self._implement(work_item=work_item, observations=observations)

        self.model.side_effect = decide
        self.assertEqual(self._run()["outcome"], "generated")
        checkpoint = self._checkpoint()
        self.assertEqual(
            checkpoint["diagnostic_state"]["tool:model_response:"]["status"], "resolved"
        )
        self.assertTrue(
            any(h["scope"] == "tool:model_response:" for h in checkpoint["diagnostic_history"])
        )

    @staticmethod
    def _implement(*, work_item, observations, **kwargs):
        if work_item.id == "engineering_plan":
            features = valid_spec()["features"]
            return turn(
                "update_delivery_plan",
                reason="Shared visual foundation precedes feature UI",
                items=[
                    {
                        "id": "visual",
                        "title": "Visual foundation",
                        "kind": "visual_foundation",
                        "requirement_ids": [item["id"] for item in features],
                    },
                    *[
                        {
                            "id": item["id"],
                            "title": item["text"],
                            "requirement_ids": [item["id"]],
                            "depends_on": ["visual"],
                        }
                        for item in features
                    ],
                ],
            )
        path = (
            "frontend/src/visual.ts" if work_item.id == "visual" else f"generated/{work_item.id}.py"
        )
        if not any(
            o.ok and o.name == "write_file" and o.arguments.get("path") == path
            for o in observations
        ):
            return turn(
                "write_file",
                path=path,
                content="export const VALUE = 1\n" if work_item.id == "visual" else "VALUE = 1\n",
            )
        return turn("complete_work_item", work_item_id=work_item.id, summary="Implemented feature")

    def _start(self):
        published = self._publish_only()
        delivery = self._assign(published.item_id)
        self.task, self.execution = self._claim(delivery.task_id)
        self.root = default_workspace_path(self.workspace_root, self.project.id, self.run.run_id)

    def _run(self):
        with self.session_factory() as db:
            return run_engineering_workflow(
                db,
                self.owner,
                self.project.id,
                self.run.run_id,
                self.task.task_id,
                self.execution.execution_id,
                session_factory=self.session_factory,
            )

    def _checkpoint(self):
        with self.session_factory() as db:
            saved = db.get(TaskExecution, self.execution.execution_id)
            return read_frozen_input_snapshot(saved)["checkpoint"]

    def test_decisions_write_sources_and_platform_checks_before_publish(self):
        self._start()
        result = self._run()
        self.assertEqual(result["outcome"], "generated")
        self.assertEqual(
            [c.kwargs["check_id"] for c in self.checks.call_args_list],
            ["quick", "all", "all"],
        )
        self.assertEqual(result["model_turns"], 7)
        for feature in valid_spec()["features"]:
            self.assertEqual(
                (self.root / f"generated/{feature['id']}.py").read_text(), "VALUE = 1\n"
            )
        checkpoint = self._checkpoint()
        self.assertEqual(checkpoint["execution_mode"], EXECUTION_MODE)
        self.assertEqual(checkpoint["completed_source_hash"], source_snapshot(self.root)[1])
        with self.session_factory() as db:
            events = list(
                db.scalars(
                    select(WorkflowEvent).where(
                        WorkflowEvent.build_run_id == self.run.run_id,
                        WorkflowEvent.event_type == "preview_revision_published",
                    )
                )
            )
            self.assertEqual(len(events), 3)
            artifact = complete_code_engineer_task(
                db,
                self.owner,
                self.project.id,
                self.run.run_id,
                self.task.task_id,
                self.execution.execution_id,
            )
            self.assertEqual(artifact.payload["source_hash"], checkpoint["completed_source_hash"])

    def test_login_last_in_prd_runs_before_protected_business_feature(self):
        from test_delivery_dependencies import dependency_design, dependency_items, dependency_spec

        with (
            patch("test_engineering_claim.valid_spec", return_value=dependency_spec().model_dump()),
            patch(
                "test_engineering_claim.valid_design", return_value=dependency_design().model_dump()
            ),
        ):
            self._start()

        def decide(*, work_item, observations, **kwargs):
            if work_item.id == "engineering_plan":
                return turn(
                    "update_delivery_plan",
                    reason="Respect authentication contract",
                    items=[item.model_dump() for item in dependency_items()],
                )
            return self._implement(work_item=work_item, observations=observations)

        self.model.side_effect = decide
        self.assertEqual(self._run()["outcome"], "generated")
        selected = [call.kwargs["work_item"].id for call in self.model.call_args_list]
        self.assertEqual(
            selected, ["engineering_plan", "theme", "theme", "auth", "auth", "projects", "projects"]
        )
        checkpoint = self._checkpoint()
        self.assertEqual(checkpoint["delivery_plan_version"], 1)
        self.assertEqual(len(checkpoint["delivery_plan_history"]), 1)

    def test_no_source_write_before_valid_dependency_plan(self):
        self._start()
        self.model.side_effect = None
        self.model.return_value = turn(
            "write_file", path="generated/premature.py", content="BAD = True"
        )
        self.assertEqual(self._run()["outcome"], "blocked")
        self.assertFalse((self.root / "generated/premature.py").exists())
        self.assertTrue(self._checkpoint()["needs_delivery_plan"])
        self.assertTrue(
            all(
                "update_delivery_plan" in row["summary"]
                for row in self._checkpoint()["observations"]
            )
        )

    def test_replanning_inserts_prerequisite_without_resetting_progress(self):
        self._start()
        revised = False

        def decide(*, work_item, observations, engineering_context, **kwargs):
            nonlocal revised
            if work_item.id == "feat_records" and not revised:
                revised = True
                items = [
                    {key: value for key, value in row.items() if key not in {"status", "summary"}}
                    for row in engineering_context["delivery_plan"]
                ]
                items[1]["depends_on"].append("records_data")
                items.insert(
                    1,
                    {
                        "id": "records_data",
                        "title": "Shared records schema",
                        "kind": "foundation",
                        "requirement_ids": ["feat_records"],
                        "depends_on": ["visual"],
                    },
                )
                return turn(
                    "update_delivery_plan", items=items, reason="Observed missing data prerequisite"
                )
            return self._implement(work_item=work_item, observations=observations)

        self.model.side_effect = decide
        self.assertEqual(self._run()["outcome"], "generated")
        checkpoint = self._checkpoint()
        self.assertEqual(len(checkpoint["delivery_plan_history"]), 2)
        self.assertEqual(checkpoint["model_turns"], 10)
        self.assertEqual(checkpoint["work_items"][0]["status"], "completed")
        ids = [call.kwargs["work_item"].id for call in self.model.call_args_list]
        self.assertEqual(ids.count("visual"), 2)
        self.assertLess(ids.index("records_data"), len(ids) - 4)

    def test_resume_keeps_graph_completed_nodes_and_counters(self):
        self._start()
        with self.session_factory() as db:
            row = db.get(TaskExecution, self.execution.execution_id)
            payload = read_frozen_input_snapshot(row)
            payload["call_budget"]["max_model_turns"] = 3
            row.draft = payload
            flag_modified(row, "draft")
            db.commit()
        self.assertEqual(self._run()["outcome"], "blocked")
        first = self._checkpoint()
        self.assertEqual(first["work_items"][0]["status"], "completed")
        with self.session_factory() as db:
            row = db.get(TaskExecution, self.execution.execution_id)
            payload = read_frozen_input_snapshot(row)
            payload["call_budget"]["max_model_turns"] = 80
            payload["checkpoint"]["outcome"] = "running"
            payload["checkpoint"]["blocked_reason"] = None
            row.draft = payload
            flag_modified(row, "draft")
            db.commit()
        self.model.reset_mock()
        self.assertEqual(self._run()["outcome"], "generated")
        self.assertEqual(self.model.call_args_list[0].kwargs["work_item"].id, "feat_records")
        self.assertEqual(self._checkpoint()["model_turns"], 7)
        self.assertEqual(len(self._checkpoint()["delivery_plan_history"]), 1)

    def test_legacy_feature_queue_must_be_replanned_before_writes(self):
        from app.generation.delivery import WorkItem

        self._start()
        with self.session_factory() as db:
            row = db.get(TaskExecution, self.execution.execution_id)
            payload = read_frozen_input_snapshot(row)
            checkpoint = new_checkpoint()
            checkpoint["model_turns"] = 4
            checkpoint["work_items"] = [
                WorkItem(
                    id=feature["id"], title=feature["text"], requirement_ids=[feature["id"]]
                ).model_dump()
                for feature in valid_spec()["features"]
            ]
            payload["checkpoint"] = checkpoint
            row.draft = payload
            flag_modified(row, "draft")
            db.commit()
        self.assertEqual(self._run()["outcome"], "generated")
        self.assertEqual(self.model.call_args_list[0].kwargs["work_item"].id, "engineering_plan")
        self.assertEqual(self._checkpoint()["model_turns"], 11)

    def test_model_sees_real_write_and_failed_check_then_repairs(self):
        self._start()
        failures = 0
        repaired = False

        def check(root, *, check_id, tool_call_id):
            nonlocal failures
            if failures == 0:
                failures += 1
                return ToolExecutionResult(
                    tool_call_id=tool_call_id,
                    name="run_check",
                    ok=False,
                    summary="invalid feature",
                    error_code="CHECK_FAILED",
                    data={"source_hash": source_snapshot(root)[1], "output": "VALUE must be 2"},
                )
            return self._passing_check(root, check_id=check_id, tool_call_id=tool_call_id)

        def decide(*, work_item, observations, engineering_context, **kwargs):
            nonlocal repaired
            if engineering_context["repair_context"] and not repaired:
                self.assertTrue(
                    any(not o.ok and "invalid feature" in o.summary for o in observations)
                )
                evidence = next(
                    o for o in reversed(observations) if o.name == "write_file" and o.ok
                )
                repaired = True
                return turn(
                    "edit_file_by_replace",
                    path=evidence.arguments["path"],
                    old_text="VALUE = 1",
                    new_text="VALUE = 2",
                    expected_hash=evidence.data["content_hash"],
                )
            return self._implement(work_item=work_item, observations=observations)

        self.checks.side_effect = check
        self.model.side_effect = decide
        self.assertEqual(self._run()["outcome"], "generated")
        self.assertTrue(repaired)
        self.assertIn("VALUE = 2", (self.root / "frontend/src/visual.ts").read_text())

    def test_repeated_failed_completion_is_bounded(self):
        self._start()
        self.checks.side_effect = lambda root, **kwargs: ToolExecutionResult(
            tool_call_id=kwargs["tool_call_id"],
            name="run_check",
            ok=False,
            summary="same failure",
            error_code="CHECK_FAILED",
            data={
                "source_hash": source_snapshot(root)[1],
                "output": "same error",
                "check_id": kwargs["check_id"],
                "runtime_version": CHECK_RUNTIME_VERSION,
            },
        )
        self.assertEqual(self._run()["outcome"], "blocked")
        self.assertEqual(self.checks.call_count, 1)
        self.assertEqual(self._checkpoint()["repair_rounds"]["visual"], 3)
        self.assertEqual(self._checkpoint()["blocked_reason_code"], "REPEATED_CHECK_FAILURE")

    def test_environment_failure_does_not_spend_model_budget(self):
        self._start()
        with patch(
            "app.orchestration.code_engineer.check_tools.check_environment",
            return_value=ToolExecutionResult(
                tool_call_id="environment",
                name="check_environment",
                ok=False,
                summary="Docker unavailable",
                error_code="CHECK_ENVIRONMENT_UNAVAILABLE",
            ),
        ):
            self.assertEqual(self._run()["outcome"], "blocked")
        self.model.assert_not_called()
        self.assertEqual(self._checkpoint()["repair_rounds"], {})

    def test_model_budget_counts_decisions_before_external_call(self):
        self._start()
        with self.session_factory() as db:
            row = db.get(TaskExecution, self.execution.execution_id)
            payload = read_frozen_input_snapshot(row)
            payload["call_budget"]["max_model_turns"] = 1
            row.draft = payload
            flag_modified(row, "draft")
            db.commit()
        self.assertEqual(self._run()["outcome"], "blocked")
        self.assertEqual(self.model.call_count, 1)
        self.assertEqual(self._checkpoint()["model_turns"], 1)

    def test_pause_during_model_call_discards_late_write(self):
        self._start()

        def paused(**kwargs):
            with self.session_factory() as db:
                row = db.get(TaskExecution, self.execution.execution_id)
                row.status = "failed"
                row.active_slot = None
                db.commit()
            return turn("write_file", path="generated/late.py", content="LATE = True\n")

        self.model.side_effect = paused
        self.assertEqual(self._run()["outcome"], "paused")
        self.assertFalse((self.root / "generated/late.py").exists())

    def test_pause_after_tool_staging_discards_prepared_output(self):
        from app.generation.engineering.actions import prepare_action

        self._start()

        def stage_then_pause(call, root, hashes):
            action = prepare_action(call, root, hashes)
            self.assertTrue(action.updates)
            with self.session_factory() as db:
                row = db.get(TaskExecution, self.execution.execution_id)
                row.status = "failed"
                row.active_slot = None
                db.commit()
            return action

        with patch("app.orchestration.code_engineer.prepare_action", side_effect=stage_then_pause):
            self.assertEqual(self._run()["outcome"], "paused")
        self.assertFalse((self.root / "frontend/src/visual.ts").exists())

    def test_listing_files_is_read_only_evidence_not_a_write_result(self):
        self._start()

        def inspect_then_implement(*, observations, **kwargs):
            if not observations:
                return turn("list_files")
            return self._implement(observations=observations, **kwargs)

        self.model.side_effect = inspect_then_implement
        self.assertEqual(self._run()["outcome"], "generated")
        self.assertEqual(len(self._checkpoint()["modified_files"]), 3)

    def test_frozen_acceptance_write_is_rejected_at_runtime(self):
        self._start()

        def decide(*, work_item, observations, **kwargs):
            if work_item.id == "engineering_plan":
                return self._implement(work_item=work_item, observations=observations)
            return turn("write_file", path="frontend/e2e/escape.ts", content="skip()")

        self.model.side_effect = decide
        self.assertEqual(self._run()["outcome"], "blocked")
        self.assertFalse(self._checkpoint()["needs_delivery_plan"])
        self.assertFalse((self.root / "frontend/e2e/escape.ts").exists())
        self.assertEqual(self._checkpoint()["blocked_reason_code"], "REPEATED_TOOL_FAILURE")

    def test_completion_cannot_select_another_work_item(self):
        self._start()

        def decide(*, work_item, observations, **kwargs):
            if work_item.id == "engineering_plan":
                return self._implement(work_item=work_item, observations=observations)
            return turn("complete_work_item", work_item_id="invented", summary="done")

        self.model.side_effect = decide
        self.assertEqual(self._run()["outcome"], "blocked")
        self.checks.assert_not_called()
        self.assertEqual(len(self._checkpoint()["work_items"]), 3)
        self.assertTrue(all(row["status"] == "pending" for row in self._checkpoint()["work_items"]))

    def test_final_hash_mismatch_cannot_mark_generated(self):
        self._start()

        def mismatched(root, *, check_id, tool_call_id):
            result = self._passing_check(root, check_id=check_id, tool_call_id=tool_call_id)
            if check_id == "all":
                result.data["source_hash"] = "wrong-version"
            return result

        self.checks.side_effect = mismatched
        self.assertEqual(self._run()["outcome"], "blocked")
        self.assertNotIn("completed_source_hash", self._checkpoint())

    def test_block_report_never_marks_complete(self):
        self._start()
        self.model.side_effect = None
        self.model.return_value = turn("report_blocked", reason="missing approved API contract")
        self.assertEqual(self._run()["outcome"], "blocked")
        self.assertIn("missing approved", self._checkpoint()["blocked_reason"])

    def test_role_only_offers_implemented_tools(self):
        self.assertEqual(
            {tool.name for tool in CODE_ENGINEER_TOOLS}, CODE_ENGINEER_PROFILE.allowed_tools
        )
        self.assertNotIn("write_new_code", CODE_ENGINEER_PROFILE.allowed_tools)
        self.assertNotIn("write_file", CODE_ENGINEER_PROFILE.allowed_tools)
        self.assertIn("write_files", CODE_ENGINEER_PROFILE.allowed_tools)

    def test_empty_tool_allowlist_is_not_replaced_by_default_permissions(self):
        spec = AppSpec.model_validate(valid_spec())
        with patch(
            "app.agents.code_engineer.chat_with_tools",
            return_value=turn(
                "write_file",
                path="bad.py",
                content="bad",
            ),
        ):
            with self.assertRaisesRegex(BusinessException, "无权"):
                decide_next_action(
                    spec=spec,
                    work_item=planning_item(spec),
                    engineering_context={},
                    observations=[],
                    system_design=None,
                    tools=[],
                )

    def test_old_checkpoint_migrates_without_replaying_writes_or_resetting_stop_loss(self):
        checkpoint = new_checkpoint()
        checkpoint.update(
            execution_mode="planned_files_v7",
            outcome="blocked",
            blocked_reason_code="NO_SOURCE_PROGRESS",
            model_turns=25,
            active_file_plan={"files": [{"path": "old.py"}]},
            work_items=[{"id": "done", "status": "completed"}],
            observations=[{"not": "new protocol"}],
        )
        migrated = load_checkpoint({"checkpoint": checkpoint})
        self.assertEqual(migrated["execution_mode"], EXECUTION_MODE)
        self.assertEqual(migrated["model_turns"], 25)
        self.assertEqual(migrated["outcome"], "blocked")
        self.assertEqual(migrated["observations"], [])
        self.assertIsNone(migrated["active_file_plan"])
        self.assertEqual(migrated["work_items"][0]["status"], "completed")
        self.assertFalse(resume_blocked_checkpoint(migrated))
        retried = prepare_manual_repair_retry({"checkpoint": migrated})
        self.assertEqual(retried["checkpoint"]["outcome"], "running")
        self.assertEqual(migrated["outcome"], "blocked")

    def test_tool_message_json_remains_valid_when_large_results_are_compacted(self):
        from app.agents.code_engineer import build_code_engineer_messages

        spec = AppSpec.model_validate(valid_spec())
        messages = build_code_engineer_messages(
            spec=spec,
            work_item=planning_item(spec),
            engineering_context={},
            system_design=None,
            observations=[
                ToolExecutionResult(
                    tool_call_id="read",
                    name="read_file",
                    ok=True,
                    summary="source",
                    data={"content": "x" * 30000},
                    arguments={"path": "source.py"},
                )
            ],
        )
        self.assertEqual(
            [message["role"] for message in messages],
            ["system", "user", "assistant", "tool", "user"],
        )
        self.assertIn("已截断", json.loads(messages[-2]["content"])["data"]["content"])

    def test_latest_task_and_recovery_state_follow_historical_observations(self):
        from app.agents.code_engineer import build_code_engineer_messages

        spec = AppSpec.model_validate(valid_spec())
        messages = build_code_engineer_messages(
            spec=spec,
            work_item=planning_item(spec),
            system_design=None,
            engineering_context={
                "needs_delivery_plan": True,
                "planning_turns_remaining": 0,
                "observed_files": {"old.py": "hash"},
                "recovery_context": {"reason_code": "NO_SOURCE_PROGRESS"},
            },
            observations=[
                ToolExecutionResult(
                    tool_call_id="read",
                    name="read_file",
                    ok=True,
                    summary="old read",
                    arguments={"path": "old.py"},
                )
            ],
        )
        latest = json.loads(messages[-1]["content"])
        self.assertEqual(messages[-1]["role"], "user")
        self.assertEqual(latest["current_work_item"]["id"], "engineering_plan")
        self.assertEqual(
            latest["engineering_context"]["recovery_context"]["reason_code"], "NO_SOURCE_PROGRESS"
        )
        self.assertNotIn("engineering_context", json.loads(messages[1]["content"]))

    def test_rejected_plan_diagnostic_drives_repair_then_real_source_actions(self):
        self._start()
        rejected = False
        diagnosed = False

        def decide(*, work_item, observations, engineering_context, **kwargs):
            nonlocal rejected, diagnosed
            if work_item.id == "engineering_plan" and not rejected:
                rejected = True
                call = self._implement(work_item=work_item, observations=observations)
                call.tool_calls[0].arguments["items"][1]["depends_on"] = ["missing"]
                return call
            if work_item.id == "engineering_plan":
                issues = engineering_context["active_diagnostics"][0]["diagnostic"]["issues"]
                self.assertEqual(issues[0]["code"], "INVALID_DEPENDENCY_TARGET")
                self.assertEqual(issues[0]["actual"], ["missing"])
                diagnosed = True
            return self._implement(work_item=work_item, observations=observations)

        self.model.side_effect = decide
        self.assertEqual(self._run()["outcome"], "generated")
        self.assertTrue(diagnosed)
        state = self._checkpoint()
        self.assertEqual(state["diagnostic_state"]["plan"]["status"], "resolved")
        self.assertTrue(state["modified_files"])
        self.assertEqual(len(state["diagnostic_history"]), 1)

    def test_identical_invalid_plan_stops_early_and_survives_attempt_recovery(self):
        self._start()
        call = self._implement(
            work_item=planning_item(AppSpec.model_validate(valid_spec())), observations=[]
        )
        call.tool_calls[0].arguments["items"][1]["depends_on"] = ["missing"]
        self.model.side_effect = None
        self.model.return_value = call
        self.assertEqual(self._run()["outcome"], "blocked")
        state = self._checkpoint()
        self.assertEqual(state["model_turns"], 3)
        self.assertEqual(state["blocked_reason_code"], "REPEATED_TOOL_FAILURE")
        self.assertEqual(state["modified_files"], [])
        self.assertEqual(len(state["diagnostic_history"]), 1)
        self.model.reset_mock()
        self.assertEqual(self._run()["outcome"], "blocked")
        self.model.assert_not_called()
