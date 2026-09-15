"""Engineering delivery: handoff from approved intent, claim, then the coding loop."""

from app.services.engineering.claim import (
    DEFAULT_CALL_BUDGET,
    INPUT_SNAPSHOT_KIND,
    INPUT_SNAPSHOT_SCHEMA_VERSION,
    TOOL_STRATEGY_VERSION,
    approved_spec_digest,
    build_frozen_input_snapshot,
    claim_software_engineer_task,
    read_frozen_input_snapshot,
    template_digest,
)
from app.services.engineering.handoff import (
    APPROVAL_VERSION,
    ENGINEERING_TASK_KEY,
    LEGACY_DESIGN_TASK_KEY,
    cancel_legacy_design_task,
    find_claimed_engineering_task,
    find_pending_design_task,
    find_pending_engineering_task,
    load_approved_app_spec,
    load_design_source,
    prepare_requirements_followup,
)
from app.services.engineering.run import is_engineering_active, start_claimed_engineering

__all__ = [
    "APPROVAL_VERSION",
    "DEFAULT_CALL_BUDGET",
    "ENGINEERING_TASK_KEY",
    "INPUT_SNAPSHOT_KIND",
    "INPUT_SNAPSHOT_SCHEMA_VERSION",
    "LEGACY_DESIGN_TASK_KEY",
    "TOOL_STRATEGY_VERSION",
    "approved_spec_digest",
    "build_frozen_input_snapshot",
    "cancel_legacy_design_task",
    "claim_software_engineer_task",
    "find_claimed_engineering_task",
    "find_pending_design_task",
    "find_pending_engineering_task",
    "is_engineering_active",
    "load_approved_app_spec",
    "load_design_source",
    "prepare_requirements_followup",
    "read_frozen_input_snapshot",
    "start_claimed_engineering",
    "template_digest",
]
