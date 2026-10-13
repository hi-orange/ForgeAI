"""Dependency scheduling is independent of PRD order and protected on replan."""

import unittest
from copy import deepcopy

from test_architect import valid_design

from app.core.exceptions import ConflictException
from app.generation.delivery import (
    PlannedWorkItem,
    WorkItem,
    replace_delivery_plan,
    validate_delivery_plan,
)
from app.generation.engineering.checkpoint import current_item
from app.schemas.agent_action import ToolCall
from app.schemas.app_spec import AppSpec
from app.schemas.system_design import SystemDesign
from app.tools.code_engineer import CODE_ENGINEER_TOOLS, validate_tool_call


def dependency_spec():
    return AppSpec.model_validate(
        {
            "goal": "Private project dashboard",
            "target_users": ["Members"],
            "features": [
                {"id": "dashboard", "text": "Show private projects"},
                {"id": "login", "text": "Sign in"},
            ],
            "data_requirements": [],
            "interface_requirements": ["Use an indigo theme"],
            "constraints": [],
            "open_questions": [],
            "acceptance_criteria": [
                {
                    "id": "private",
                    "text": "Only see own projects",
                    "source_ids": ["dashboard", "login"],
                }
            ],
        }
    )


def dependency_items():
    # Deliberately put consumers BEFORE their prerequisites, just like the PRD.
    return [
        PlannedWorkItem(
            id="projects",
            title="Private projects",
            requirement_ids=["dashboard"],
            depends_on=["auth", "theme"],
        ),
        PlannedWorkItem(
            id="auth", title="Identity and session", requirement_ids=["login"], depends_on=["theme"]
        ),
        PlannedWorkItem(
            id="theme",
            title="Semantic visual tokens",
            kind="visual_foundation",
            requirement_ids=["dashboard", "login"],
        ),
    ]


def dependency_design():
    payload = valid_design()
    payload["implementation_dependencies"] = [
        {
            "prerequisite_requirement_id": "login",
            "dependent_requirement_id": "dashboard",
            "reason": "Private projects need an authenticated identity",
        }
    ]
    return SystemDesign.model_validate(payload)


class DeliveryDependencyTests(unittest.TestCase):
    def test_prd_and_plan_order_do_not_override_dependencies(self):
        rows = replace_delivery_plan(dependency_spec(), dependency_items(), [], dependency_design())
        order = []
        while item := current_item(rows):
            order.append(item["id"])
            item["status"] = "completed"
        self.assertEqual(order, ["theme", "auth", "projects"])

    def test_rejects_missing_unknown_cyclic_or_self_dependencies_and_scope(self):
        for mutation in (
            "cycle",
            "unknown",
            "self",
            "missing_feature",
            "unapproved",
            "no_visual",
            "visual_late",
            "duplicate",
        ):
            with self.subTest(mutation=mutation):
                items = dependency_items()
                if mutation == "cycle":
                    items[2].depends_on = ["projects"]
                elif mutation == "unknown":
                    items[0].depends_on = ["missing"]
                elif mutation == "self":
                    items[0].depends_on = ["projects"]
                elif mutation == "missing_feature":
                    items[0].requirement_ids = ["login"]
                elif mutation == "unapproved":
                    items[0].requirement_ids = ["payments"]
                elif mutation == "no_visual":
                    items[2].kind = "foundation"
                elif mutation == "visual_late":
                    items[1].depends_on = []
                else:
                    items.append(items[0])
                with self.assertRaises(ConflictException):
                    replace_delivery_plan(dependency_spec(), items, [])

    def test_frozen_architecture_dependency_cannot_be_omitted(self):
        items = dependency_items()
        items[0].depends_on = ["theme"]
        with self.assertRaisesRegex(ConflictException, "前置依赖"):
            replace_delivery_plan(dependency_spec(), items, [], dependency_design())

    def test_visual_shared_requirement_links_do_not_fake_auth_dependency(self):
        items = dependency_items()
        items[0].depends_on = ["theme"]
        with self.assertRaises(ConflictException):
            replace_delivery_plan(dependency_spec(), items, [], dependency_design())

    def test_completed_nodes_are_immutable_and_pending_can_be_split(self):
        items = dependency_items()
        rows = replace_delivery_plan(dependency_spec(), items, [])
        rows[2]["status"] = "completed"
        rows[2]["summary"] = "Verified existing theme"
        before = deepcopy(rows)
        items.insert(
            1,
            PlannedWorkItem(
                id="auth_data", title="User model", kind="foundation", requirement_ids=["login"]
            ),
        )
        items[2].depends_on.append("auth_data")
        result = replace_delivery_plan(dependency_spec(), items, rows)
        self.assertEqual(current_item(result)["id"], "auth_data")
        self.assertEqual(result[-1]["summary"], "Verified existing theme")
        self.assertEqual(rows, before)
        items[-1].title = "Silently change approved theme"
        with self.assertRaisesRegex(ConflictException, "已完成"):
            replace_delivery_plan(dependency_spec(), items, rows)

    def test_model_cannot_submit_completion_state_or_summary(self):
        items = [item.model_dump() for item in dependency_items()]
        items[0]["status"] = "completed"
        with self.assertRaises(ValueError):
            validate_tool_call(
                ToolCall(
                    id="plan",
                    name="update_delivery_plan",
                    arguments={"reason": "skip", "items": items},
                )
            )

    def test_dependency_schema_has_no_provider_local_references(self):
        import json

        for tool in CODE_ENGINEER_TOOLS:
            self.assertNotIn('"$ref"', json.dumps(tool.parameters))

    def test_invalid_design_edges_rejected_and_legacy_design_readable(self):
        design = dependency_design()
        design.implementation_dependencies[0].prerequisite_requirement_id = "unknown"
        with self.assertRaises(ValueError):
            design.validate_requirement_dependencies(dependency_spec())
        self.assertEqual(
            SystemDesign.model_validate(valid_design()).implementation_dependencies, []
        )

    def test_one_requirement_may_have_multiple_tasks_and_shared_foundations(self):
        items = dependency_items()
        items.append(
            PlannedWorkItem(
                id="settings",
                title="Session settings",
                requirement_ids=["login"],
                depends_on=["auth"],
            )
        )
        validate_delivery_plan(dependency_spec(), [WorkItem(**item.model_dump()) for item in items])

    def test_legacy_completed_work_does_not_have_to_be_redone(self):
        old = WorkItem(
            id="auth", title="Existing sign in", requirement_ids=["login"], status="completed"
        )
        items = dependency_items()
        items[1] = PlannedWorkItem.model_validate(old.model_dump(exclude={"status", "summary"}))
        rows = replace_delivery_plan(dependency_spec(), items, [old.model_dump()])
        self.assertEqual(rows[1]["status"], "completed")
        self.assertEqual(current_item(rows)["id"], "theme")

    def test_qa_repair_preserves_long_defect_ids_and_complete_evidence(self):
        from app.generation.delivery import plan_quality_repair
        from app.schemas.test_report import TestReport

        report = TestReport.model_validate(
            {
                "code_item_id": "code",
                "source_hash": "a" * 64,
                "requirement_results": [
                    {"requirement_id": "private", "status": "failed", "evidence": "Failed"}
                ],
                "check_results": [{"check_id": "all", "status": "failed", "evidence": "Failed"}],
                "defects": [
                    {
                        "defect_id": "d" * 64,
                        "severity": "major",
                        "title": "Missing access check",
                        "description": "d" * 4000,
                        "evidence": "e" * 4000,
                        "related_requirement_ids": ["private"],
                    }
                ],
                "quality_conclusion": "failed",
                "summary": "Repair required",
            }
        )
        item = plan_quality_repair(dependency_spec(), report)[0]
        self.assertEqual(item.id, "repair_" + "d" * 64)
        self.assertEqual(item.acceptance[1], "e" * 4000)
