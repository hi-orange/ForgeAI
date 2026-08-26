import unittest

from app.api.v1.projects import router
from app.models.project import Project
from app.schemas.project import ProjectOut


class LegacyStaticPipelineRemovalTests(unittest.TestCase):
    def test_project_router_has_only_project_crud_reads(self) -> None:
        endpoints = {(route.path, frozenset(route.methods or set())) for route in router.routes}
        self.assertEqual(
            endpoints,
            {
                ("/projects", frozenset({"POST"})),
                ("/projects", frozenset({"GET"})),
                ("/projects/{project_id}", frozenset({"GET"})),
            },
        )

    def test_legacy_static_fields_are_not_runtime_models(self) -> None:
        legacy_fields = {
            "prd",
            "approved_spec",
            "generated_files",
            "validation_report",
            "website_revision",
        }
        self.assertTrue(legacy_fields.isdisjoint(Project.__table__.columns.keys()))
        self.assertTrue(legacy_fields.isdisjoint(ProjectOut.model_fields))


if __name__ == "__main__":
    unittest.main()
