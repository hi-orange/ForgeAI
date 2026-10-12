import unittest
from pathlib import Path

from app.benchmarks import load_observations, load_suite, score_benchmark

ROOT = Path(__file__).resolve().parents[3]


class BenchmarkTests(unittest.TestCase):
    def test_fixed_suite_and_golden_scorer_contract(self) -> None:
        suite, suite_hash = load_suite(ROOT / "benchmarks/forgeai-v1.json")
        observations = load_observations(ROOT / "benchmarks/fixtures/forgeai-v1-observations.json")
        report = score_benchmark(suite, suite_hash, observations)
        self.assertEqual(report.suite_version, "1.0.0")
        self.assertEqual(len(report.suite_hash), 64)
        self.assertTrue(all(case.status == "passed" for case in report.cases))
        self.assertTrue(all(metric.value == 1.0 for metric in report.metrics))

    def test_missing_observation_is_failure_not_inferred_success(self) -> None:
        suite, suite_hash = load_suite(ROOT / "benchmarks/forgeai-v1.json")
        report = score_benchmark(suite, suite_hash, [])
        self.assertTrue(all(case.status == "missing" for case in report.cases))
        self.assertTrue(all(metric.numerator == 0 for metric in report.metrics))


if __name__ == "__main__":
    unittest.main()
