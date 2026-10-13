"""Call limits shared by frozen-input creation and engineering execution."""

from typing import Any

DEFAULT_CALL_BUDGET: dict[str, int] = {
    "max_model_turns": 80,
    "max_tool_calls": 120,
    # Cascading first-error failures burn rounds quickly; keep a small hard cap
    # and rely on identical-failure / no-progress stop-loss for the rest.
    "max_repair_rounds": 3,
}


def call_budget(snapshot: dict[str, Any], name: str) -> int:
    configured = snapshot.get("call_budget")
    value = configured.get(name) if isinstance(configured, dict) else None
    if isinstance(value, int) and value > 0:
        return value
    return DEFAULT_CALL_BUDGET[name]
