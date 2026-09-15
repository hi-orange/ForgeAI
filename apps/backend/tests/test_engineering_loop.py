"""Engineering loop writes workspace files from frozen requirements using tools."""

from __future__ import annotations

from unittest.mock import patch

from sqlalchemy import select
from test_engineering_claim import EngineeringClaimTests
from test_product_manager_workflow import valid_spec

from app.agents.software_engineer import build_software_engineer_messages
from app.generation.delivery import plan_delivery
from app.generation.workspace import default_workspace_path
from app.models.task import Task
from app.models.task_execution import TaskExecution
from app.orchestration.software_engineer import run_engineering_workflow
from app.schemas.agent_action import ChatWithToolsResult, ToolCall, ToolExecutionResult
from app.schemas.app_spec import AppSpec
from app.services.engineering import read_frozen_input_snapshot
from app.tools.paths import sha256_bytes


class EngineeringLoopTests(EngineeringClaimTests):
    def test_plan_delivery_covers_each_feature_once(self):
        spec = AppSpec.model_validate(valid_spec())
        items = plan_delivery(spec)
        self.assertEqual([item.id for item in items], [feature.id for feature in spec.features])

    def test_tool_history_includes_assistant_tool_calls(self):
        spec = AppSpec.model_validate(valid_spec())
        messages = build_software_engineer_messages(
            spec=spec,
            work_item=plan_delivery(spec)[0],
            observations=[
                ToolExecutionResult(
                    tool_call_id="call_read",
                    name="read_file",
                    ok=True,
                    summary="ok",
                    arguments={"path": "frontend/src/App.vue"},
                )
            ],
        )
        self.assertEqual(
            [item["role"] for item in messages],
            ["system", "user", "assistant", "tool"],
        )
        self.assertEqual(messages[2]["tool_calls"][0]["id"], "call_read")
        self.assertEqual(messages[3]["tool_call_id"], "call_read")

    def test_loop_writes_file_then_completes_work_items(self):
        published = self._publish_only()
        delivery = self._assign(published.item_id)
        task, execution = self._claim(delivery.task_id)
        workspace = default_workspace_path(self.workspace_root, self.project.id, self.run.run_id)
        target = workspace / "frontend" / "src" / "App.vue"
        turns = {"n": 0}

        def decide(*, spec, work_item, observations, tools=None):
            turns["n"] += 1
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
            return ChatWithToolsResult(
                tool_calls=[
                    ToolCall(
                        id=f"call_done_{work_item.id}",
                        name="complete_work_item",
                        arguments={"work_item_id": work_item.id, "summary": "written"},
                    )
                ]
            )

        with patch(
            "app.orchestration.software_engineer.decide_next_action",
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

        self.assertGreaterEqual(turns["n"], 4)
        self.assertEqual(result["outcome"], "generated")
        self.assertIn("FORGEAI_GENERATED", target.read_text(encoding="utf-8"))
        progress = self._status()
        self.assertEqual(progress.state, "engineering_generated")
        self.assertTrue(progress.code_ready)
        labels = [item.label for item in progress.activities]
        self.assertIn("Start coding", labels)
        self.assertIn("Call model", labels)
        self.assertIn("Read file", labels)
        self.assertIn("Write file", labels)
        self.assertTrue(any("frontend/src/App.vue" in item.detail for item in progress.activities))
        with self.session_factory() as db:
            saved = db.get(TaskExecution, execution.execution_id)
            snapshot = read_frozen_input_snapshot(saved)
            assert snapshot is not None
            self.assertEqual(snapshot["approved_item_id"], published.item_id)
            self.assertEqual(snapshot["checkpoint"]["outcome"], "generated")
            still = db.scalar(select(Task).where(Task.task_id == task.task_id))
            self.assertEqual(still.status, "running")
