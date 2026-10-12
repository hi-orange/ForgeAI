import argparse
from pathlib import Path

from app.benchmarks import load_observations, load_suite, score_benchmark


def main() -> None:
    parser = argparse.ArgumentParser(description="Score captured ForgeAI benchmark observations")
    parser.add_argument("--suite", required=True)
    parser.add_argument("--observations", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    suite, suite_hash = load_suite(args.suite)
    report = score_benchmark(suite, suite_hash, load_observations(args.observations))
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    if any(case.status != "passed" for case in report.cases):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
