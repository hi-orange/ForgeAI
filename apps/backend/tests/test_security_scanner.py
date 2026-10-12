import importlib.util
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

CHECK_PATH = Path(__file__).resolve().parents[1] / "sandbox/check.py"
SPEC = importlib.util.spec_from_file_location("forgeai_sandbox_check", CHECK_PATH)
assert SPEC is not None and SPEC.loader is not None
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class SecurityScannerTests(unittest.TestCase):
    def test_safe_sources_pass(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "backend/app").mkdir(parents=True)
            (root / "backend/app/main.py").write_text("value = 1\n", encoding="utf-8")
            report = CHECK.scan_security(root)
        self.assertEqual(report["conclusion"], "passed")
        self.assertEqual(report["findings"], [])

    def test_code_execution_secret_and_floating_dependency_are_blocked(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "backend/app").mkdir(parents=True)
            (root / "frontend").mkdir()
            (root / "backend/app/main.py").write_text(
                "eval(user_input)\nsecret='-----BEGIN PRIVATE KEY-----'\n",
                encoding="utf-8",
            )
            (root / "frontend/package.json").write_text(
                '{"dependencies":{"unsafe":"latest"}}', encoding="utf-8"
            )
            report = CHECK.scan_security(root)
        self.assertEqual(report["conclusion"], "failed")
        rules = {finding["rule_id"] for finding in report["findings"]}
        self.assertTrue({"PY_DYNAMIC_EXECUTION", "PRIVATE_KEY", "UNPINNED_NPM_SOURCE"} <= rules)
        secret = next(
            finding for finding in report["findings"] if finding["rule_id"] == "PRIVATE_KEY"
        )
        self.assertEqual(secret["evidence"], "[REDACTED]")


if __name__ == "__main__":
    unittest.main()
