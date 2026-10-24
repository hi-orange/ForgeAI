"""Trusted QA executor. Test assets are a bounded HTTP/browser DSL, never eval'ed."""

import importlib.util
import json
import os
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

_spec = importlib.util.spec_from_file_location("qa_check", Path(__file__).with_name("check.py"))
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

VERSION = "forgeai-qa-v2"


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


def execute(payload):
    suite = payload["suite"]
    results = []
    backend = frontend = None
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
        check.command(["alembic", "upgrade", "head"], check.ROOT / "backend")
        check.command(["alembic", "check"], check.ROOT / "backend")
        if has_browser:
            check.frontend(skip_dependencies=True)
        backend = check.start_backend(port=8000)
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
        for case in suite["scenarios"]:
            script = suite["scripts"].get(case["test_id"])
            row = (
                run_case(case, script)
                if script
                else {
                    "status": "not_run",
                    "failure_kind": "script",
                    "observations": [],
                    "evidence": "场景尚未实现执行脚本",
                }
            )
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
