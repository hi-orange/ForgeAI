"""The planned-file engineering executor writes real files and repairs failed checks."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.orm.attributes import flag_modified
from test_architect import valid_design
from test_engineering_claim import EngineeringClaimTests
from test_product_manager_workflow import valid_spec

from app.agents.code_engineer import (
    CODE_ENGINEER_PROFILE,
    CODE_ENGINEER_TOOLS,
    PlannedFileBatch,
    build_code_engineer_messages,
    decide_next_action,
    plan_work_item_files,
)
from app.core.exceptions import BusinessException
from app.generation.delivery import FileTask, ImplementationPlan, plan_delivery
from app.generation.workspace import default_workspace_path
from app.models.task_execution import TaskExecution
from app.orchestration.code_engineer import (
    EXECUTION_MODE,
    _load_checkpoint,
    _normalize_plan,
    run_engineering_workflow,
)
from app.schemas.agent_action import ChatWithToolsResult, ToolCall, ToolExecutionResult
from app.schemas.app_spec import AppSpec
from app.schemas.system_design import SystemDesign
from app.services.engineering import read_frozen_input_snapshot
from app.tools.checks import source_snapshot


class EngineeringLoopTests(EngineeringClaimTests):
    def setUp(self) -> None:
        super().setUp()
        self.environment_check = self.enterContext(
            patch(
                "app.orchestration.code_engineer.check_tools.check_environment",
                return_value=ToolExecutionResult(
                    tool_call_id="check_environment",
                    name="check_environment",
                    ok=True,
                    summary="隔离检查环境已就绪",
                    data={"image": "forgeai-checks:fullstack-v1"},
                ),
            )
        )

    def _set_call_budget(self, execution_id: str, **updates: int) -> None:
        with self.session_factory() as db:
            execution = db.get(TaskExecution, execution_id)
            snapshot = read_frozen_input_snapshot(execution)
            assert snapshot is not None
            snapshot["call_budget"] = {**snapshot["call_budget"], **updates}
            execution.draft = snapshot
            flag_modified(execution, "draft")
            db.commit()

    @staticmethod
    def _file_plan(work_item_id: str, *, suffix: str = "") -> PlannedFileBatch:
        return PlannedFileBatch(
            plan=ImplementationPlan(
                work_item_id=work_item_id,
                summary=f"实现 {work_item_id}",
                files=[
                    FileTask(
                        id=f"write_{work_item_id}{suffix}",
                        path=f"generated/{work_item_id}.py",
                        description="实现当前功能的完整模块",
                        context_paths=["backend/app/main.py"],
                    )
                ],
            )
        )

    @staticmethod
    def _passing_check(root: Path, *, check_id: str, tool_call_id: str) -> ToolExecutionResult:
        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            name="run_check",
            ok=True,
            summary="all 检查通过",
            data={"check_id": check_id, "source_hash": source_snapshot(root)[1]},
        )

    def test_plan_delivery_covers_each_feature_once(self):
        spec = AppSpec.model_validate(valid_spec())
        items = plan_delivery(spec)
        self.assertEqual([item.id for item in items], [feature.id for feature in spec.features])

    def test_code_engineer_receives_only_role_allowed_tools(self):
        self.assertEqual(CODE_ENGINEER_PROFILE.display_name, "Code Engineer")
        self.assertEqual(
            {tool.name for tool in CODE_ENGINEER_TOOLS},
            CODE_ENGINEER_PROFILE.allowed_tools,
        )

    def test_legacy_controller_history_remains_parseable_but_is_not_the_executor(self):
        spec = AppSpec.model_validate(valid_spec())
        messages = build_code_engineer_messages(
            spec=spec,
            work_item=plan_delivery(spec)[0],
            engineering_context={"workspace_file_index": {"paths": ["frontend/src/App.vue"]}},
            observations=[
                ToolExecutionResult(
                    tool_call_id="call_read",
                    name="read_file",
                    ok=True,
                    summary="ok",
                    arguments={"path": "frontend/src/App.vue"},
                )
            ],
            system_design=SystemDesign.model_validate(valid_design()),
        )
        self.assertEqual(
            [item["role"] for item in messages],
            ["system", "user", "assistant", "tool"],
        )
        self.assertEqual(messages[2]["tool_calls"][0]["id"], "call_read")

    def test_legacy_controller_still_rejects_unoffered_tools(self):
        spec = AppSpec.model_validate(valid_spec())
        read_only = [tool for tool in CODE_ENGINEER_TOOLS if tool.name == "read_file"]
        with patch(
            "app.agents.code_engineer.chat_with_tools",
            return_value=ChatWithToolsResult(
                tool_calls=[
                    ToolCall(
                        id="write",
                        name="write_new_code",
                        arguments={"path": "backend/app/main.py"},
                    )
                ]
            ),
        ):
            with self.assertRaisesRegex(BusinessException, "无权使用工具"):
                decide_next_action(
                    spec=spec,
                    work_item=plan_delivery(spec)[0],
                    engineering_context={"workspace_file_index": {"paths": []}},
                    observations=[],
                    system_design=None,
                    tools=read_only,
                )

    def test_file_planner_accepts_workspace_root_configuration(self):
        spec = AppSpec.model_validate(valid_spec())
        work_item = plan_delivery(spec)[0]
        payload = {
            "work_item_id": work_item.id,
            "summary": "更新根目录构建配置",
            "files": [
                {
                    "id": "build_config",
                    "path": "build.config.json",
                    "description": "配置应用构建入口",
                    "context_paths": ["frontend/package.json"],
                }
            ],
        }
        with patch("app.agents.code_engineer.chat_completion", return_value=json.dumps(payload)):
            batch = plan_work_item_files(
                spec=spec,
                work_item=work_item,
                workspace_file_index={"paths": ["frontend/package.json"]},
                system_design=None,
            )
        root = default_workspace_path(self.workspace_root, self.project.id, self.run.run_id)
        root.mkdir(parents=True, exist_ok=True)
        normalized = _normalize_plan(root, batch.plan)
        self.assertEqual(normalized.files[0].path, "build.config.json")
        self.assertEqual(batch.deferred_files, ())

    def test_plan_work_item_files_forces_json_and_retries_invalid_output(self):
        spec = AppSpec.model_validate(valid_spec())
        work_item = plan_delivery(spec)[0]
        payload = {
            "work_item_id": work_item.id,
            "summary": "实现职位列表页",
            "files": [
                {
                    "id": "job_list",
                    "path": "frontend/src/views/Jobs.vue",
                    "description": "展示已发布职位",
                    "context_paths": ["frontend/src/App.vue"],
                }
            ],
        }
        calls: list[dict] = []

        def fake_chat(*, messages, json_output=False, **_kwargs):
            calls.append({"json_output": json_output, "messages": messages})
            if len(calls) == 1:
                return "这里先说明一下计划\n```json\nnot-json\n```"
            return "备注：\n" + json.dumps(payload)

        with patch("app.agents.code_engineer.chat_completion", side_effect=fake_chat):
            batch = plan_work_item_files(
                spec=spec,
                work_item=work_item,
                workspace_file_index={"paths": ["frontend/src/App.vue"]},
                system_design=None,
            )
        self.assertEqual(batch.plan.files[0].path, "frontend/src/views/Jobs.vue")
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call["json_output"] is True for call in calls))
        self.assertIn("只重新输出完整 JSON", calls[1]["messages"][-1]["content"])

    def test_plan_work_item_files_accepts_fenced_json_on_first_try(self):
        spec = AppSpec.model_validate(valid_spec())
        work_item = plan_delivery(spec)[0]
        payload = {
            "work_item_id": work_item.id,
            "summary": "实现职位列表页",
            "files": [
                {
                    "id": "job_list",
                    "path": "frontend/src/views/Jobs.vue",
                    "description": "展示已发布职位",
                    "context_paths": [],
                }
            ],
        }
        fenced = f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```"
        with patch("app.agents.code_engineer.chat_completion", return_value=fenced) as chat:
            batch = plan_work_item_files(
                spec=spec,
                work_item=work_item,
                workspace_file_index={"paths": []},
                system_design=None,
            )
        self.assertEqual(batch.plan.summary, "实现职位列表页")
        self.assertTrue(chat.call_args.kwargs["json_output"])

    def test_environment_preflight_blocks_before_planner_or_writer(self):
        published = self._publish_only()
        delivery = self._assign(published.item_id)
        task, execution = self._claim(delivery.task_id)
        unavailable = ToolExecutionResult(
            tool_call_id="check_environment",
            name="check_environment",
            ok=False,
            error_code="CHECK_ENVIRONMENT_UNAVAILABLE",
            summary="隔离检查环境未就绪：Docker daemon 不可用",
        )
        with (
            patch(
                "app.orchestration.code_engineer.check_tools.check_environment",
                return_value=unavailable,
            ),
            patch("app.orchestration.code_engineer.plan_work_item_files") as planner,
            patch("app.orchestration.code_engineer.generate_file_content") as writer,
        ):
            with self.session_factory() as db:
                result = run_engineering_workflow(
                    db,
                    self.owner,
                    self.project.id,
                    self.run.run_id,
                    task.task_id,
                    execution.execution_id,
                    session_factory=self.session_factory,
                )
        self.assertEqual(result["outcome"], "blocked")
        planner.assert_not_called()
        writer.assert_not_called()

    def test_planned_executor_auto_loads_context_writes_files_and_checks(self):
        published = self._publish_only()
        delivery = self._assign(published.item_id)
        task, execution = self._claim(delivery.task_id)
        contexts: list[dict] = []

        def plan(*, work_item, **_kwargs):
            return self._file_plan(work_item.id)

        def write(*, engineering_context, work_item, **_kwargs):
            contexts.append(engineering_context)
            return f"FEATURE = {work_item.id!r}\n"

        with (
            patch("app.orchestration.code_engineer.plan_work_item_files", side_effect=plan),
            patch("app.orchestration.code_engineer.generate_file_content", side_effect=write),
            patch(
                "app.orchestration.code_engineer.check_tools.run_check",
                side_effect=self._passing_check,
            ) as checks,
        ):
            with self.session_factory() as db:
                result = run_engineering_workflow(
                    db,
                    self.owner,
                    self.project.id,
                    self.run.run_id,
                    task.task_id,
                    execution.execution_id,
                    session_factory=self.session_factory,
                )

        self.assertEqual(result["outcome"], "generated")
        self.assertEqual(checks.call_count, len(valid_spec()["features"]))
        self.assertEqual(len(contexts), len(valid_spec()["features"]))
        for context in contexts:
            files = context["platform_file_context"]["files"]
            self.assertEqual(files[0]["role"], "target")
            self.assertFalse(files[0]["exists"])
            self.assertEqual(files[1]["path"], "backend/app/main.py")
            self.assertIn("content", files[1])

        workspace = default_workspace_path(self.workspace_root, self.project.id, self.run.run_id)
        self.assertTrue((workspace / "generated/feat_records.py").is_file())
        self.assertTrue((workspace / "generated/feat_export.py").is_file())
        with self.session_factory() as db:
            saved = db.get(TaskExecution, execution.execution_id)
            snapshot = read_frozen_input_snapshot(saved)
            assert snapshot is not None
            checkpoint = snapshot["checkpoint"]
            self.assertEqual(checkpoint["execution_mode"], EXECUTION_MODE)
            self.assertEqual(checkpoint["planner_model_calls"], 2)
            self.assertEqual(checkpoint["writer_model_calls"], 2)
            self.assertEqual(checkpoint["outcome"], "generated")
            labels = [item["label"] for item in checkpoint["activity"]]
            self.assertIn("Plan files", labels)
            self.assertIn("File plan ready", labels)
            self.assertIn("Generate file", labels)
            self.assertIn("File written", labels)
            self.assertIn("Run checks", labels)
            self.assertIn("Checks passed", labels)

    def test_failed_check_creates_repair_plan_then_rechecks(self):
        published = self._publish_only()
        delivery = self._assign(published.item_id)
        task, execution = self._claim(delivery.task_id)
        repair_contexts: list[dict | None] = []
        check_calls = {"count": 0}

        def plan(*, work_item, repair_context=None, **_kwargs):
            repair_contexts.append(repair_context)
            suffix = "_repair" if repair_context else ""
            return self._file_plan(work_item.id, suffix=suffix)

        def check(root, *, check_id, tool_call_id):
            check_calls["count"] += 1
            if check_calls["count"] == 1:
                return ToolExecutionResult(
                    tool_call_id=tool_call_id,
                    name="run_check",
                    ok=False,
                    error_code="CHECK_FAILED",
                    summary="all 检查失败",
                    data={
                        "check_id": check_id,
                        "source_hash": source_snapshot(root)[1],
                        "output": "generated/feat_records.py:1: syntax error",
                    },
                )
            return self._passing_check(root, check_id=check_id, tool_call_id=tool_call_id)

        with (
            patch("app.orchestration.code_engineer.plan_work_item_files", side_effect=plan),
            patch(
                "app.orchestration.code_engineer.generate_file_content",
                return_value="VALUE = 'fixed'\n",
            ),
            patch("app.orchestration.code_engineer.check_tools.run_check", side_effect=check),
        ):
            with self.session_factory() as db:
                result = run_engineering_workflow(
                    db,
                    self.owner,
                    self.project.id,
                    self.run.run_id,
                    task.task_id,
                    execution.execution_id,
                    session_factory=self.session_factory,
                )

        self.assertEqual(result["outcome"], "generated")
        self.assertIsNone(repair_contexts[0])
        self.assertIsNotNone(repair_contexts[1])
        assert repair_contexts[1] is not None
        self.assertIn("syntax error", repair_contexts[1]["failed_check"]["data"]["output"])
        with self.session_factory() as db:
            saved = db.get(TaskExecution, execution.execution_id)
            snapshot = read_frozen_input_snapshot(saved)
            assert snapshot is not None
            checkpoint = snapshot["checkpoint"]
            self.assertEqual(checkpoint["repair_rounds"]["feat_records"], 1)
            self.assertTrue(any(item["kind"] == "repair" for item in checkpoint["plan_history"]))

    def test_model_budget_counts_planner_and_writer(self):
        published = self._publish_only()
        delivery = self._assign(published.item_id)
        task, execution = self._claim(delivery.task_id)
        self._set_call_budget(execution.execution_id, max_model_turns=1)
        with (
            patch(
                "app.orchestration.code_engineer.plan_work_item_files",
                side_effect=lambda *, work_item, **_kwargs: self._file_plan(work_item.id),
            ),
            patch("app.orchestration.code_engineer.generate_file_content") as writer,
        ):
            with self.session_factory() as db:
                result = run_engineering_workflow(
                    db,
                    self.owner,
                    self.project.id,
                    self.run.run_id,
                    task.task_id,
                    execution.execution_id,
                    session_factory=self.session_factory,
                )
        self.assertEqual(result["outcome"], "blocked")
        writer.assert_not_called()
        with self.session_factory() as db:
            saved = db.get(TaskExecution, execution.execution_id)
            snapshot = read_frozen_input_snapshot(saved)
            assert snapshot is not None
            self.assertEqual(snapshot["checkpoint"]["blocked_reason"], "模型调用预算已用尽")

    def test_controller_checkpoint_migrates_without_replaying_observations(self):
        snapshot = {
            "checkpoint": {
                "kind": "engineering_checkpoint",
                "schema_version": 3,
                "outcome": "blocked",
                "blocked_reason": "工程执行停滞",
                "model_turns": 40,
                "tool_calls": 40,
                "observations": [{"name": "read_file"}],
                "work_items": [],
                "activity": [],
            }
        }
        checkpoint = _load_checkpoint(snapshot)
        self.assertEqual(checkpoint["execution_mode"], EXECUTION_MODE)
        self.assertEqual(checkpoint["outcome"], "running")
        self.assertEqual(checkpoint["model_turns"], 0)
        self.assertEqual(checkpoint["tool_calls"], 0)
        self.assertEqual(checkpoint["observations"], [])
        self.assertTrue(checkpoint["migrated_from_controller"])
