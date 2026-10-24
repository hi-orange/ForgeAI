"""Dependency scheduling is independent of PRD order and protected on replan."""

import json
import unittest
from copy import deepcopy
from pathlib import Path

from test_architect import valid_design

from app.core.exceptions import ConflictException
from app.generation.delivery import (
    DeliveryPlanError,
    PlannedWorkItem,
    WorkItem,
    migrate_implementation_repair_plan,
    replace_delivery_plan,
    suggested_delivery_plan,
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


def visual_prerequisite_plan():
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/visual_prerequisite_plan.json").read_text(
            encoding="utf-8"
        )
    )
    return (
        AppSpec.model_validate(fixture["spec"]),
        SystemDesign.model_validate(
            {**valid_design(), "implementation_dependencies": fixture["dependencies"]}
        ),
        [PlannedWorkItem.model_validate(i) for i in fixture["original"]],
    )


class DeliveryDependencyTests(unittest.TestCase):
    def test_implementation_repair_proposal_only_contains_frozen_defects(self):
        content = (
            "修复这些缺陷：\n"
            "1. 演示账号不能在生产环境启用固定密码。\n"
            "2. 切换招聘方后不能看到前一个账号的候选人。\n"
            "3. 已修改的种子职位重启后不能复制。\n"
            "4. 无效手机号和邮箱应返回字段级 422。"
        )
        spec = dependency_spec()
        proposal = suggested_delivery_plan(spec, dependency_design(), repair_request=content)
        self.assertEqual(len(proposal["items"]), 4)
        self.assertEqual({item["kind"] for item in proposal["items"]}, {"repair"})
        self.assertTrue(all(item["requirement_ids"] == [] for item in proposal["items"]))
        self.assertEqual(
            len(
                replace_delivery_plan(
                    spec,
                    [PlannedWorkItem.model_validate(row) for row in proposal["items"]],
                    [],
                    dependency_design(),
                    repair_request=content,
                )
            ),
            4,
        )
        incomplete = [PlannedWorkItem.model_validate(row) for row in proposal["items"][:-1]]
        with self.assertRaises(DeliveryPlanError) as raised:
            replace_delivery_plan(spec, incomplete, [], dependency_design(), repair_request=content)
        self.assertIn(
            "MISSING_REPAIR_TARGET",
            {issue["code"] for issue in raised.exception.diagnostic.issues},
        )
        original = [
            PlannedWorkItem.model_validate(row)
            for row in suggested_delivery_plan(spec, dependency_design())["items"]
        ]
        with self.assertRaises(DeliveryPlanError) as raised:
            replace_delivery_plan(spec, original, [], dependency_design(), repair_request=content)
        self.assertIn(
            "OUT_OF_SCOPE_REPAIR_ITEMS",
            {issue["code"] for issue in raised.exception.diagnostic.issues},
        )
        with self.assertRaises(DeliveryPlanError):
            replace_delivery_plan(
                spec, [PlannedWorkItem.model_validate(row) for row in proposal["items"]], []
            )

    def test_repair_scope_migration_retains_completed_node_and_spent_budget(self):
        spec = dependency_spec()
        old = replace_delivery_plan(
            spec,
            [PlannedWorkItem.model_validate(row) for row in suggested_delivery_plan(spec)["items"]],
            [],
        )
        old[0]["status"] = "completed"
        old[0]["summary"] = "Existing theme verified"
        checkpoint = {
            "work_items": deepcopy(old),
            "delivery_plan_version": 1,
            "needs_delivery_plan": False,
            "current_work_item_id": old[1]["id"],
            "model_turns": 29,
            "tool_calls": 31,
            "modified_files": [],
            "no_progress_steps": 9,
        }
        request = {"revision_id": "rev_1", "message_id": 19, "content": "修复登录后的私有数据泄漏"}
        self.assertTrue(migrate_implementation_repair_plan(checkpoint, request))
        self.assertEqual(len(checkpoint["work_items"]), 1)
        self.assertEqual(checkpoint["work_items"][0], old[0])
        self.assertTrue(checkpoint["needs_delivery_plan"])
        self.assertEqual(checkpoint["model_turns"], 29)
        self.assertEqual(checkpoint["tool_calls"], 31)
        self.assertEqual(checkpoint["delivery_plan_history"][-1]["items"], old)
        proposal = suggested_delivery_plan(
            spec, repair_request=request["content"], completed=checkpoint["work_items"]
        )
        revised = replace_delivery_plan(
            spec,
            [PlannedWorkItem.model_validate(row) for row in proposal["items"]],
            checkpoint["work_items"],
            repair_request=request["content"],
        )
        self.assertEqual(len(revised), 2)
        self.assertEqual(revised[0]["status"], "completed")
        self.assertEqual(current_item(revised)["kind"], "repair")
        self.assertFalse(migrate_implementation_repair_plan(checkpoint, request))
        with self.assertRaisesRegex(ConflictException, "身份不一致"):
            migrate_implementation_repair_plan(
                checkpoint, {**request, "revision_id": "rev_changed"}
            )

    def test_suggested_plan_uses_explicit_edges_and_keeps_cross_feature_acceptance_late(self):
        spec, design = dependency_spec(), dependency_design()
        proposal = suggested_delivery_plan(spec, design)
        rows = replace_delivery_plan(
            spec, [PlannedWorkItem.model_validate(row) for row in proposal["items"]], [], design
        )
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id["login"]["acceptance"], ["Sign in"])
        self.assertEqual(by_id["dashboard"]["acceptance"], ["Only see own projects"])
        order = []
        while item := current_item(rows):
            order.append(item["id"])
            item["status"] = "completed"
        self.assertEqual(order, ["visual_foundation", "login", "dashboard"])
        self.assertEqual(suggested_delivery_plan(spec, design), proposal)
        self.assertNotEqual(suggested_delivery_plan(spec)["content_hash"], proposal["content_hash"])

    def test_suggested_plan_does_not_infer_auth_and_avoids_reserved_id_collisions(self):
        spec = dependency_spec().model_copy(
            update={
                "features": [
                    dependency_spec()
                    .features[0]
                    .model_copy(
                        update={"id": "visual_foundation", "text": "A public login guide page"}
                    )
                ],
                "acceptance_criteria": [],
            }
        )
        items = suggested_delivery_plan(spec)["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual({row["id"] for row in items}, {"visual_foundation", "visual_foundation_1"})
        self.assertTrue(all(row["requirement_ids"] == ["visual_foundation"] for row in items))

    def test_plan_tool_accepts_exactly_one_plan_source(self):
        digest = suggested_delivery_plan(dependency_spec())["content_hash"]
        valid = validate_tool_call(
            ToolCall(
                id="adopt",
                name="update_delivery_plan",
                arguments={"suggested_plan_hash": digest, "reason": "Matches approved scope"},
            )
        )
        self.assertIsNone(valid.arguments["items"])
        for arguments in (
            {"reason": "missing plan"},
            {"reason": "bad hash", "suggested_plan_hash": "made up"},
            {
                "reason": "ambiguous",
                "suggested_plan_hash": digest,
                "items": [row.model_dump() for row in dependency_items()],
            },
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                validate_tool_call(
                    ToolCall(id="bad", name="update_delivery_plan", arguments=arguments)
                )

    def test_suggestion_handles_maximum_features_and_long_requirement_ids(self):
        payload = dependency_spec().model_dump()
        payload["features"] = [
            {"id": f"f{index:02d}" + "x" * 61, "text": f"Approved feature {index}"}
            for index in range(50)
        ]
        payload["acceptance_criteria"] = [
            {"id": f"ac_{index}", "text": f"Feature {index} works", "source_ids": [feature["id"]]}
            for index, feature in enumerate(payload["features"])
        ]
        spec = AppSpec.model_validate(payload)
        proposed = suggested_delivery_plan(spec)
        self.assertEqual(len(proposed["items"]), 51)
        self.assertEqual(
            {row["id"] for row in proposed["items"] if row["kind"] == "feature"},
            {feature.id for feature in spec.features},
        )

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

    def test_real_visual_prerequisite_plan_has_an_explicit_repair_path(self):
        spec, design, items = visual_prerequisite_plan()
        with self.assertRaises(DeliveryPlanError) as raised:
            replace_delivery_plan(spec, items, [], design)
        issues = raised.exception.diagnostic.issues
        self.assertEqual({i["code"] for i in issues}, {"MISSING_REQUIREMENT_DEPENDENCY"})
        self.assertEqual({i["prerequisite_requirement_id"] for i in issues}, {"feat_visual"})
        for issue in issues:
            self.assertIn("visual_foundation", issue["linked_ancestor_ids"])
            self.assertIn("provides_requirement_ids", issue["suggested_action"])
        visual = next(i for i in items if i.id == "visual_foundation")
        visual.provides_requirement_ids = ["feat_visual"]
        rows = replace_delivery_plan(spec, items, [], design)
        self.assertEqual(current_item(rows)["id"], visual.id)
        self.assertTrue(all(i["status"] == "pending" for i in rows))
        self.assertEqual(rows[0]["provides_requirement_ids"], ["feat_visual"])
        # Planning and evidence declarations grant no completion or check results.
        self.assertTrue(all(i["summary"] is None for i in rows))
        # Real nonvisual dependencies remain mandatory in the same recorded plan.
        next(i for i in items if i.id == "jobs_public_api").depends_on = [visual.id]
        with self.assertRaises(DeliveryPlanError):
            replace_delivery_plan(spec, items, [], design)

    def test_visual_coverage_repair_preserves_both_foundation_and_feature_constraints(self):
        spec, design, items = visual_prerequisite_plan()
        visual = next(item for item in items if item.kind == "visual_foundation")
        visual.provides_requirement_ids = ["feat_visual"]
        for item in items:
            if item.kind == "feature":
                item.requirement_ids = [r for r in item.requirement_ids if r != "feat_visual"]

        # Declaring a real prerequisite still leaves the user-visible feature uncovered.
        with self.assertRaises(DeliveryPlanError) as raised:
            replace_delivery_plan(spec, items, [], design)
        issue = next(
            i for i in raised.exception.diagnostic.issues if i["code"] == "MISSING_FEATURE_COVERAGE"
        )
        self.assertEqual(issue["missing_requirement_ids"], ["feat_visual"])
        self.assertEqual(
            issue["linked_non_feature_items"],
            [
                {
                    "work_item_id": visual.id,
                    "kind": "visual_foundation",
                    "requirement_ids": ["feat_visual"],
                    "provides_requirement_ids": ["feat_visual"],
                }
            ],
        )
        self.assertIn("不计入功能覆盖", issue["expected"])
        self.assertIn("保留", issue["suggested_action"])

        # Retagging the same node repairs coverage but breaks the visual prerequisite gate.
        visual.kind = "feature"
        with self.assertRaises(DeliveryPlanError) as raised:
            replace_delivery_plan(spec, items, [], design)
        issue = next(
            i
            for i in raised.exception.diagnostic.issues
            if i["code"] == "MISSING_VISUAL_FOUNDATION"
        )
        self.assertIn(visual.id, issue["feature_work_item_ids"])
        self.assertIn("同时保留 feature", issue["suggested_action"])

        # A distinct runnable UI slice can cover the requirement while reusing the foundation.
        visual.kind = "visual_foundation"
        items.append(
            PlannedWorkItem(
                id="visual_experience",
                title="职位列表的视觉体验与移动端验证",
                requirement_ids=["feat_visual"],
                depends_on=["web_shell_jobs"],
                acceptance=["职位卡片采用统一主题与字号层级，移动端能够正常浏览"],
                deliverables=["职位列表样式接入与浏览器验证"],
            )
        )
        rows = replace_delivery_plan(spec, items, [], design)
        self.assertEqual(current_item(rows)["id"], visual.id)
        self.assertTrue(all(row["status"] == "pending" for row in rows))
        self.assertEqual(rows[-1]["requirement_ids"], ["feat_visual"])

    def test_coverage_diagnostic_identifies_nonvisual_foundations_without_granting_coverage(self):
        items = dependency_items()
        auth = items[1]
        auth.kind = "foundation"
        auth.provides_requirement_ids = ["login"]
        auth.acceptance = ["Identity and session contract is callable"]
        auth.deliverables = ["User model and session API"]
        with self.assertRaises(DeliveryPlanError) as raised:
            replace_delivery_plan(dependency_spec(), items, [], dependency_design())
        issue = next(
            i for i in raised.exception.diagnostic.issues if i["code"] == "MISSING_FEATURE_COVERAGE"
        )
        linked = {item["work_item_id"]: item for item in issue["linked_non_feature_items"]}
        self.assertEqual(issue["missing_requirement_ids"], ["login"])
        self.assertEqual(linked["auth"]["kind"], "foundation")
        self.assertEqual(linked["auth"]["provides_requirement_ids"], ["login"])
        self.assertEqual(linked["theme"]["kind"], "visual_foundation")
        self.assertEqual(linked["theme"]["provides_requirement_ids"], [])

    def test_provisions_require_linked_scope_and_verifiable_deliverables(self):
        for mutation in ("unlinked", "unknown", "no_acceptance", "no_deliverables"):
            with self.subTest(mutation=mutation):
                items = dependency_items()
                visual = items[-1]
                visual.provides_requirement_ids = ["login"]
                visual.acceptance = ["A real session survives refresh"]
                visual.deliverables = ["Session API and integration probe"]
                if mutation == "unlinked":
                    visual.requirement_ids = ["dashboard"]
                elif mutation == "unknown":
                    visual.provides_requirement_ids = ["payments"]
                elif mutation == "no_acceptance":
                    visual.acceptance = []
                else:
                    visual.deliverables = []
                with self.assertRaises(DeliveryPlanError) as raised:
                    replace_delivery_plan(dependency_spec(), items, [], dependency_design())
                self.assertIn(
                    "INVALID_REQUIREMENT_PROVISION",
                    {i["code"] for i in raised.exception.diagnostic.issues},
                )

    def test_explicit_provision_still_requires_an_ancestor(self):
        items = dependency_items()
        items[0].depends_on = ["theme"]
        items[1].provides_requirement_ids = ["login"]
        items[1].acceptance = ["Session survives refresh"]
        items[1].deliverables = ["Session API"]
        with self.assertRaises(DeliveryPlanError):
            replace_delivery_plan(dependency_spec(), items, [], dependency_design())
        items[0].depends_on = ["auth"]
        rows = replace_delivery_plan(dependency_spec(), items, [], dependency_design())
        rows[1]["status"] = "completed"
        items[1].provides_requirement_ids = []
        with self.assertRaisesRegex(ConflictException, "已完成"):
            replace_delivery_plan(dependency_spec(), items, rows, dependency_design())

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
