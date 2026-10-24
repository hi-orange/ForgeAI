"""Trusted QA executor. Test assets are a bounded HTTP/browser DSL, never eval'ed."""

import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

_spec = importlib.util.spec_from_file_location("qa_check", Path(__file__).with_name("check.py"))
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

VERSION = "forgeai-qa-v3"
_SQL_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_path(path):
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path.startswith("//")
        or "\\" in path
        or any(ord(char) < 32 for char in path)
    ):
        raise ValueError("QA 请求必须指向隔离应用站内路径")
    return path


def assert_json(document, assertion):
    actual = check._json_path(document, assertion["path"])
    op, expected = assertion["operator"], assertion.get("value")
    valid = {
        "equals": lambda: actual == expected,
        "contains": lambda: expected in actual,
        "not_contains": lambda: expected not in actual,
        "length": lambda: len(actual) == expected,
        "exists": lambda: True,
    }[op]()
    if not valid:
        raise AssertionError(f"{assertion['path']} {op}: expected={expected!r}, actual={actual!r}")
    return {"path": assertion["path"], "operator": op, "expected": expected, "actual": actual}


def api_step(step, variables):
    step = check._render_variables(step, variables)
    path = local_path(step["path"])
    headers = {"Accept": "application/json", **step["headers"]}
    if any(key.lower() in {"host", "connection", "proxy-authorization"} for key in headers):
        raise ValueError("禁止覆盖请求目标或代理头")
    body = step.get("json_body")
    payload = json.dumps(body).encode() if body is not None else None
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        "http://127.0.0.1:8000" + path,
        data=payload,
        headers=headers,
        method=step["method"],
    )
    try:
        response = urllib.request.build_opener(NoRedirect).open(request, timeout=10)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        status, raw = response.status, response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("API 响应超过测试预算")
    if status != step["expect_status"]:
        raise AssertionError(
            f"{step['method']} {path}: expected HTTP {step['expect_status']}, actual {status}"
        )
    assertions = []
    if step["assertions"] or step["capture"]:
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AssertionError("预期 JSON 响应，实际不是 JSON") from exc
        for assertion in step["assertions"]:
            try:
                assertions.append(assert_json(document, assertion))
            except (ValueError, TypeError) as exc:
                raise AssertionError(str(exc)) from exc
        for name, path_expr in step["capture"].items():
            variables[name] = check._json_path(document, path_expr)
    return {
        "kind": "api",
        "method": step["method"],
        "path": path,
        "status": status,
        "assertions": assertions,
    }


def run_case(case, script):
    observations, variables = [], {}
    browser_steps = []
    # API setup must precede browser steps; interleaving is rejected, not reordered.
    try:
        for index, step in enumerate(script["steps"]):
            if step["kind"] == "api":
                if browser_steps:
                    raise ValueError("API 步骤须在浏览器步骤之前；拆分场景或使用 UI 完成流程")
                observations.append({"step": index + 1, **api_step(step, variables)})
            else:
                browser_steps.append(check._render_variables(step, variables))
        if browser_steps:
            completed = subprocess.run(
                ["node", "/opt/forgeai/acceptance.mjs"],
                input=json.dumps(browser_steps),
                text=True,
                capture_output=True,
                timeout=120,
                cwd=check.ROOT / "frontend",
            )
            browser = json.loads(completed.stdout)
            observations.extend(browser["observations"])
            if browser["status"] != "passed":
                return {
                    "status": browser["status"],
                    "failure_kind": browser["failure_kind"],
                    "observations": observations,
                    "evidence": browser["evidence"][:3000],
                }
        return {
            "status": "passed",
            "failure_kind": None,
            "observations": observations,
            "evidence": json.dumps(observations, ensure_ascii=False, default=str)[:3500],
        }
    except AssertionError as exc:
        return {
            "status": "failed",
            "failure_kind": "product",
            "observations": observations,
            "evidence": str(exc)[:3000],
        }
    except (urllib.error.URLError, TimeoutError, subprocess.TimeoutExpired) as exc:
        return {
            "status": "blocked",
            "failure_kind": "environment",
            "observations": observations,
            "evidence": str(exc)[:3000],
        }
    except (ValueError, TypeError, KeyError) as exc:
        return {
            "status": "blocked",
            "failure_kind": "script",
            "observations": observations,
            "evidence": str(exc)[:3000],
        }


def runtime_evidence(runtime):
    return {
        "database": "isolated_sqlite",
        "startup_env_names": sorted(runtime["env"]),
        "before_upgrade_revision": runtime.get("before_upgrade_revision"),
        "seed_rows": len(runtime["seed_rows"]),
        "migrated_to": "head",
    }


def _case_environment(runtime, database):
    env = os.environ.copy()
    env.update(runtime["env"])
    # The trusted controller owns the database target and must not inherit it
    # from a model-authored scenario.
    env["DATABASE_URL"] = f"sqlite:///{database}"
    env["DEBUG"] = "false"
    return env


def _migration(argv, env, *, capture_output=False):
    return subprocess.run(
        ["alembic", *argv],
        cwd=check.ROOT / "backend",
        env=env,
        check=True,
        timeout=90,
        capture_output=capture_output,
        text=capture_output,
        **check.application_process_options(),
    )


def _seed_old_rows(database, rows):
    with closing(sqlite3.connect(database)) as connection, connection:
        for row in rows:
            table = row["table"]
            if not isinstance(table, str) or not _SQL_IDENTIFIER.fullmatch(table):
                raise ValueError("旧库夹具表名无效")
            if any(
                not isinstance(name, str) or not _SQL_IDENTIFIER.fullmatch(name)
                for name in row["values"]
            ):
                raise ValueError("旧库夹具字段名无效")
            if table.lower().startswith("sqlite_") or table.lower() == "alembic_version":
                raise ValueError("旧库夹具不能修改系统表")
            existing = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            columns = {item[1] for item in existing}
            if not columns or not set(row["values"]) <= columns:
                raise ValueError(f"旧库夹具表或字段不存在：{table}")
            names = list(row["values"])
            quoted = ", ".join(f'"{name}"' for name in names)
            placeholders = ", ".join("?" for _ in names)
            values = [
                int(value) if isinstance(value, bool) else value for value in row["values"].values()
            ]
            connection.execute(f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})', values)


def prepare_case_runtime(runtime, case_index):
    database = Path(f"/tmp/qa-case-{case_index}.db")
    if database.exists():
        raise ValueError("QA 场景数据库已存在")
    env = _case_environment(runtime, database)
    revision = runtime.get("before_upgrade_revision")
    if revision is not None:
        # Reject an invalid revision as a script issue before running generated migrations.
        exists = subprocess.run(
            ["alembic", "show", revision],
            cwd=check.ROOT / "backend",
            env=env,
            check=False,
            timeout=20,
            capture_output=True,
            text=True,
            **check.application_process_options(),
        )
        if exists.returncode:
            raise ValueError(f"旧库修订不存在：{revision}")
        _migration(["upgrade", revision], env)
        _seed_old_rows(database, runtime["seed_rows"])
    _migration(["upgrade", "head"], env)
    _migration(["check"], env)
    return env, runtime_evidence(runtime)


def _runtime_failure(exc):
    if isinstance(exc, (ValueError, TypeError, KeyError, sqlite3.Error)):
        kind = "script"
    elif isinstance(exc, (subprocess.CalledProcessError, RuntimeError, urllib.error.HTTPError)):
        kind = "product"
    else:
        kind = "environment"
    return {
        "status": "failed" if kind == "product" else "blocked",
        "failure_kind": kind,
        "observations": [],
        "evidence": f"场景启动或旧库升级失败：{str(exc)[:1500]}",
    }


def execute(payload):
    suite = payload["suite"]
    results = []
    backend = frontend = None
    default_ready = False
    default_failure = None
    try:
        check.materialize(payload["source"])
        # Model source runs as nobody; it cannot replace the root-owned runner,
        # inspect its stdin or write/forge the protected execution manifest.
        has_browser = any(
            step["kind"] == "browser"
            for script in suite["scripts"].values()
            for step in script["steps"]
        )
        # Prepare dependency links before any generated process can alter source paths.
        if has_browser:
            actual = json.loads((check.ROOT / "frontend/package.json").read_text())
            installed = json.loads(check.TEMPLATE_FRONTEND_PACKAGE.read_text())
            if any(
                actual.get(key) != installed.get(key) for key in ("dependencies", "devDependencies")
            ):
                raise ValueError("QA 离线镜像未包含当前前端依赖；需要匹配依赖的检查镜像")
            check.ensure_frontend_dependencies()
        actual_backend = tomllib.loads((check.ROOT / "backend/pyproject.toml").read_text())
        installed_backend = tomllib.loads(check.TEMPLATE_BACKEND_PYPROJECT.read_text())
        if (
            actual_backend["project"]["dependencies"]
            != installed_backend["project"]["dependencies"]
        ):
            raise ValueError("QA 离线镜像未包含当前后端依赖；需要匹配依赖的检查镜像")
        check.APPLICATION_UID = 65534
        for path in [check.ROOT, *check.ROOT.rglob("*")]:
            os.chown(path, 65534, 65534, follow_symlinks=False)
        check.ensure_backend_dependencies(check.ROOT / "backend")
        if has_browser:
            check.frontend(skip_dependencies=True)
        if has_browser:
            frontend = subprocess.Popen(
                [
                    "node",
                    str(check.ROOT / "frontend/node_modules/vite/bin/vite.js"),
                    "preview",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "5173",
                    "--strictPort",
                ],
                cwd=check.ROOT / "frontend",
                **check.application_process_options(),
            )
            check.wait_http("http://127.0.0.1:5173/", frontend, "QA 前端")
        for index, case in enumerate(suite["scenarios"]):
            script = suite["scripts"].get(case["test_id"])
            if script is None:
                row = {
                    "status": "not_run",
                    "failure_kind": "script",
                    "observations": [],
                    "evidence": "场景尚未实现执行脚本",
                }
            elif case.get("runtime") is not None:
                if backend is not None:
                    check.stop_process(backend)
                    backend = None
                runtime = case["runtime"]
                try:
                    env, setup = prepare_case_runtime(runtime, index)
                    backend = check.start_backend(port=8000, env=env)
                    row = run_case(case, script)
                    row["runtime_evidence"] = setup
                    row["evidence"] = (
                        f"隔离场景启动：{json.dumps(setup, ensure_ascii=False)}；" + row["evidence"]
                    )[:3500]
                except (
                    ValueError,
                    TypeError,
                    KeyError,
                    sqlite3.Error,
                    RuntimeError,
                    subprocess.CalledProcessError,
                    subprocess.TimeoutExpired,
                    OSError,
                ) as exc:
                    row = _runtime_failure(exc)
                finally:
                    if backend is not None:
                        check.stop_process(backend)
                        backend = None
            else:
                if default_failure is not None:
                    row = default_failure.copy()
                else:
                    try:
                        if not default_ready:
                            check.command(["alembic", "upgrade", "head"], check.ROOT / "backend")
                            check.command(["alembic", "check"], check.ROOT / "backend")
                            default_ready = True
                        if backend is None:
                            backend = check.start_backend(port=8000)
                        row = run_case(case, script)
                    except (
                        ValueError,
                        TypeError,
                        KeyError,
                        sqlite3.Error,
                        RuntimeError,
                        subprocess.CalledProcessError,
                        subprocess.TimeoutExpired,
                        OSError,
                    ) as exc:
                        default_failure = _runtime_failure(exc)
                        row = default_failure.copy()
            results.append({"test_id": case["test_id"], **row})
    except Exception as exc:
        results = [
            {
                "test_id": case["test_id"],
                "status": "blocked",
                "failure_kind": "environment",
                "observations": [],
                "evidence": f"应用启动/环境失败：{str(exc)[:1500]}",
            }
            for case in suite["scenarios"]
        ]
    finally:
        for process in (frontend, backend):
            if process is not None:
                check.stop_process(process)
    manifest = {
        key: payload[key] for key in ("execution_id", "code_item_id", "source_hash", "suite_hash")
    }
    manifest.update(runner_version=VERSION, results=results)
    return manifest


if __name__ == "__main__":
    destination = Path("/tmp/evidence")
    destination.mkdir(mode=0o700, exist_ok=False)
    manifest = execute(json.load(sys.stdin))
    (destination / "acceptance.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
