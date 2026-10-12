from __future__ import annotations

from app.benchmarks.schemas import (
    BenchmarkCaseResult,
    BenchmarkMetric,
    BenchmarkObservation,
    BenchmarkReport,
    BenchmarkSuite,
)


def score_benchmark(
    suite: BenchmarkSuite, suite_hash: str, observations: list[BenchmarkObservation]
) -> BenchmarkReport:
    by_id = {item.case_id: item for item in observations}
    known_ids = {case.case_id for case in suite.cases}
    unknown = sorted(set(by_id) - known_ids)
    if unknown:
        raise ValueError(f"unknown benchmark observations: {', '.join(unknown)}")
    counters = {
        "message_classification_accuracy": [0, 0],
        "routing_accuracy": [0, 0],
        "architecture_decision_accuracy": [0, 0],
        "end_to_end_task_success": [0, 0],
        "security_blocking_accuracy": [0, 0],
        "repair_budget_pass": [0, 0],
        "trajectory_replayable": [0, 0],
    }
    results = []
    repairs, durations, tokens, costs = [], [], [], []
    for case in suite.cases:
        observed = by_id.get(case.case_id)
        if observed is None:
            results.append(
                BenchmarkCaseResult(
                    case_id=case.case_id, status="missing", failed_checks=["observation_missing"]
                )
            )
            for values in counters.values():
                values[1] += 1
            continue
        if observed.suite_version != suite.version:
            raise ValueError(
                f"observation {case.case_id} uses suite version {observed.suite_version}"
            )
        expected = case.expectation
        checks = {
            "message_classification_accuracy": observed.category == expected.category,
            "routing_accuracy": observed.action == expected.action,
            "architecture_decision_accuracy": observed.architect_used
            == expected.architect_required,
            "end_to_end_task_success": observed.end_to_end_success == expected.end_to_end_success,
            "security_blocking_accuracy": observed.security_blocked
            == expected.security_should_block,
            "repair_budget_pass": observed.repair_rounds is not None
            and observed.repair_rounds <= expected.max_repair_rounds,
            "trajectory_replayable": bool(observed.trajectory_id),
        }
        failed = []
        for name, passed in checks.items():
            counters[name][1] += 1
            counters[name][0] += int(passed)
            if not passed:
                failed.append(name)
        if observed.repair_rounds is not None:
            repairs.append(observed.repair_rounds)
        if observed.duration_ms is not None:
            durations.append(observed.duration_ms)
        if observed.total_tokens is not None:
            tokens.append(observed.total_tokens)
        if observed.estimated_cost_microusd is not None:
            costs.append(observed.estimated_cost_microusd)
        results.append(
            BenchmarkCaseResult(
                case_id=case.case_id, status="failed" if failed else "passed", failed_checks=failed
            )
        )
    metrics = [
        BenchmarkMetric(
            name=name,
            numerator=value[0],
            denominator=value[1],
            value=(value[0] / value[1] if value[1] else None),
        )
        for name, value in counters.items()
    ]
    return BenchmarkReport(
        suite_id=suite.suite_id,
        suite_version=suite.version,
        suite_hash=suite_hash,
        metrics=metrics,
        cases=results,
        average_repair_rounds=(sum(repairs) / len(repairs) if repairs else None),
        average_duration_ms=(sum(durations) / len(durations) if durations else None),
        total_tokens=sum(tokens) if tokens else None,
        estimated_cost_microusd=sum(costs) if costs else None,
    )
