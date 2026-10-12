from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.benchmarks.schemas import BenchmarkObservation, BenchmarkSuite


def _json(path: str | Path) -> object:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_suite(path: str | Path) -> tuple[BenchmarkSuite, str]:
    raw = _json(path)
    suite = BenchmarkSuite.model_validate(raw)
    ids = [case.case_id for case in suite.cases]
    if len(ids) != len(set(ids)):
        raise ValueError("benchmark case_id must be unique")
    canonical = json.dumps(raw, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return suite, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_observations(path: str | Path) -> list[BenchmarkObservation]:
    raw = _json(path)
    if not isinstance(raw, list):
        raise ValueError("benchmark observations must be an array")
    observations = [BenchmarkObservation.model_validate(item) for item in raw]
    ids = [item.case_id for item in observations]
    if len(ids) != len(set(ids)):
        raise ValueError("benchmark observation case_id must be unique")
    return observations
