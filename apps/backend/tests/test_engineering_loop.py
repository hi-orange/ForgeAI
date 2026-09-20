"""Engineering loop writes workspace files from frozen requirements using tools."""

from __future__ import annotations

import json
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified
from test_architect import valid_design
from test_engineering_claim import EngineeringClaimTests
from test_product_manager_workflow import valid_spec

from app.agents.code_engineer import (
    CODE_ENGINEER_PROFILE,
    CODE_ENGINEER_TOOLS,
    build_code_engineer_messages,
    decide_next_action,
)
from app.generation.delivery import plan_delivery
from app.generation.workspace import default_workspace_path
from app.models.task import Task
from app.models.task_execution import TaskExecution
from app.orchestration.code_engineer import run_engineering_workflow
from app.schemas.agent_action import ChatWithToolsResult, ToolCall, ToolExecutionResult
from app.schemas.app_spec import AppSpec
from app.schemas.system_design import SystemDesign
from app.services.engineering import read_frozen_input_snapshot
from app.tools.checks import source_snapshot
from app.tools.paths import sha256_bytes


class EngineeringLoopTests(EngineeringClaimTests):
    def _set_call_budget(self, execution_id: str, **updates: int) -> None:
        with self.session_factory() as db:
            execution = db.get(TaskExecution, execution_id)
            snapshot = read_frozen_input_snapshot(execution)
            assert snapshot is not None
            snapshot["call_budget"] = {**snapshot["call_budget"], **updates}
            execution.draft = snapshot
            flag_modified(execution, "draft")
            db.commit()

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

    def test_tool_history_includes_assistant_tool_calls(self):
        spec = AppSpec.model_validate(valid_spec())
        messages = build_code_engineer_messages(
            spec=spec,
            work_item=plan_delivery(spec)[0],
            engineering_context={
                "stack": {"frontend": "vue", "backend": "fastapi"},
                "workspace_file_index": {"paths": ["frontend/src/App.vue"]},
            },
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
        self.assertEqual(messages[3]["tool_call_id"], "call_read")
        payload = json.loads(messages[1]["content"])
        self.assertEqual(payload["engineering_context"]["stack"]["frontend"], "vue")
        self.assertTrue(payload["current_work_item"]["acceptance"])
        self.assertIn("architecture_overview", payload["system_design"])

    def test_each_model_turn_executes_only_one_observed_action(self):
        spec = AppSpec.model_validate(valid_spec())
        with patch(
            "app.agents.code_engineer.chat_with_tools",
            return_value=ChatWithToolsResult(
                tool_calls=[
                    ToolCall(id="call_backend", name="read_file", arguments={"path": "a.py"}),
                    ToolCall(id="call_frontend", name="read_file", arguments={"path": "b.vue"}),
                ]
            ),
        ):
            result = decide_next_action(
                spec=spec,
                work_item=plan_delivery(spec)[0],
                engineering_context={"workspace_file_index": {"paths": []}},
                observations=[],
                system_design=SystemDesign.model_validate(valid_design()),
            )
        self.assertEqual([call.id for call in result.tool_calls], ["call_backend"])

    def test_loop_writes_file_then_completes_work_items(self):
        published = self._publish_only()
        delivery = self._assign(published.item_id)
        task, execution = self._claim(delivery.task_id)
        workspace = default_workspace_path(self.workspace_root, self.project.id, self.run.run_id)
        target = workspace / "frontend" / "src" / "App.vue"
        turns = {"n": 0}
        contexts: list[dict] = []

        def decide(
            *, spec, work_item, engineering_context, observations, system_design=None, tools=None
        ):
            self.assertIsNotNone(system_design)
            turns["n"] += 1
            contexts.append(engineering_context)
            if turns["n"] == 1:
                return ChatWithToolsResult(
                    tool_calls=[
                        ToolCall(
                            id="call_read",
                            name="read_file",
                            arguments={"path": "frontend/src/App.vue"},
                        )
                    ]
                )
            if turns["n"] == 2:
                return ChatWithToolsResult(
                    tool_calls=[
                        ToolCall(
                            id="call_write",
                            name="apply_patch",
                            arguments={
                                "path": "frontend/src/App.vue",
                                "expected_hash": sha256_bytes(target.read_bytes()),
                                "content": "<template><h1>FORGEAI_GENERATED</h1></template>\n",
                            },
                        )
                    ]
                )
            if not any(observation.name == "run_check" for observation in observations):
                return ChatWithToolsResult(
                    tool_calls=[
                        ToolCall(
                            id=f"call_check_{work_item.id}",
                            name="run_check",
                            arguments={"check_id": "all"},
                        )
                    ]
                )
            return ChatWithToolsResult(
                tool_calls=[
                    ToolCall(
                        id=f"call_done_{work_item.id}",
                        name="complete_work_item",
                        arguments={"work_item_id": work_item.id, "summary": "written"},
                    )
                ]
            )

        def check(root, *, check_id, tool_call_id):
            return ToolExecutionResult(
                tool_call_id=tool_call_id,
                name="run_check",
                ok=True,
                summary="all 检查通过",
                data={"check_id": check_id, "source_hash": source_snapshot(root)[1]},
            )

        with (
            patch(
                "app.orchestration.code_engineer.decide_next_action",
                side_effect=decide,
            ),
            patch("app.tools.code_engineer.check_tools.run_check", side_effect=check),
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

        self.assertGreaterEqual(turns["n"], 4)
        self.assertEqual(result["outcome"], "generated")
        self.assertIn("FORGEAI_GENERATED", target.read_text(encoding="utf-8"))
        first_context = contexts[0]
        self.assertEqual(first_context["stack"]["frontend"], "vue")
        self.assertIn(
            "frontend/src/App.vue",
            first_context["workspace_file_index"]["paths"],
        )
        self.assertIn(
            "frontend/src/App.vue",
            contexts[-1]["files_modified_in_this_run"],
        )
        progress = self._status()
        self.assertEqual(progress.state, "engineering_generated")
        self.assertTrue(progress.code_ready)
        labels = [item.label for item in progress.activities]
        self.assertIn("Start coding", labels)
        self.assertIn("Call model", labels)
        self.assertIn("Read file", labels)
        self.assertIn("Write file", labels)
        self.assertIn("Run checks", labels)
        self.assertIn("Progress", labels)
        self.assertTrue(
            any(
                item.detail == "计划已获批准，我现在初始化全栈项目模板（Vue 前端 + FastAPI 后端）。"
                for item in progress.activities
            )
        )
        self.assertTrue(any("工程上下文已准备完成" in item.detail for item in progress.activities))
        self.assertTrue(any(item.name == "summary" and item.detail for item in progress.activities))
        self.assertTrue(any("frontend/src/App.vue" in item.detail for item in progress.activities))
        with self.session_factory() as db:
            saved = db.get(TaskExecution, execution.execution_id)
            snapshot = read_frozen_input_snapshot(saved)
            assert snapshot is not None
            self.assertEqual(snapshot["approved_item_id"], published.item_id)
            self.assertEqual(snapshot["checkpoint"]["outcome"], "generated")
            still = db.scalar(select(Task).where(Task.task_id == task.task_id))
            self.assertEqual(still.status, "running")

    def test_loop_stops_at_frozen_model_turn_budget(self):
        published = self._publish_only()
        delivery = self._assign(published.item_id)
        task, execution = self._claim(delivery.task_id)
        self._set_call_budget(execution.execution_id, max_model_turns=2)
        paths = ["frontend/src/App.vue", "frontend/src/main.ts"]
        turns = {"n": 0}

        def decide(
            *, spec, work_item, engineering_context, observations, system_design=None, tools=None
        ):
            self.assertIsNotNone(system_design)
            index = turns["n"]
            turns["n"] += 1
            return ChatWithToolsResult(
                tool_calls=[
                    ToolCall(
                        id=f"call_read_{index}",
                        name="read_file",
                        arguments={"path": paths[index]},
                    )
                ]
            )

        with patch(
            "app.orchestration.code_engineer.decide_next_action",
            side_effect=decide,
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

        self.assertEqual(turns["n"], 2)
        self.assertEqual(result["outcome"], "blocked")
        with self.session_factory() as db:
            saved = db.get(TaskExecution, execution.execution_id)
            snapshot = read_frozen_input_snapshot(saved)
            assert snapshot is not None
            self.assertEqual(snapshot["checkpoint"]["blocked_reason"], "模型调用预算已用尽")

    def test_work_item_cannot_complete_without_current_all_check(self):
        published = self._publish_only()
        delivery = self._assign(published.item_id)
        task, execution = self._claim(delivery.task_id)
        self._set_call_budget(execution.execution_id, max_model_turns=1)

        def decide(
            *, spec, work_item, engineering_context, observations, system_design=None, tools=None
        ):
            self.assertIsNotNone(system_design)
            return ChatWithToolsResult(
                tool_calls=[
                    ToolCall(
                        id="complete_without_check",
                        name="complete_work_item",
                        arguments={"work_item_id": work_item.id},
                    )
                ]
            )

        with patch(
            "app.orchestration.code_engineer.decide_next_action",
            side_effect=decide,
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
        with self.session_factory() as db:
            saved = db.get(TaskExecution, execution.execution_id)
            snapshot = read_frozen_input_snapshot(saved)
            assert snapshot is not None
            observation = snapshot["checkpoint"]["observations"][-1]
            self.assertEqual(observation["error_code"], "CHECK_REQUIRED")
