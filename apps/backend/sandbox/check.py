"""Trusted check controller, baked into the image; never copied from generated source."""

import base64
import binascii
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path("/tmp/app")
DATABASE = Path("/tmp/check.db")
TEMPLATE_FRONTEND_PACKAGE = Path("/opt/frontend/package.json")
TEMPLATE_FRONTEND_MODULES = Path("/opt/frontend/node_modules")
TEMPLATE_THEME_FILE = Path("/opt/frontend/index.css")
TEMPLATE_BACKEND_PYPROJECT = Path("/opt/backend/pyproject.toml")
SMOKE_MANIFEST = ROOT / "forgeai.smoke.json"
SMOKE_CONTRACT_PATH = Path(__file__).with_name("smoke_contract.json")
SMOKE_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
SMOKE_CONTRACT = json.loads(SMOKE_CONTRACT_PATH.read_text(encoding="utf-8"))
BROWSER_CONTRACT = SMOKE_CONTRACT["browser"]
SMOKE_ACTIONS = BROWSER_CONTRACT["actions"]
VARIABLE_PATTERN = re.compile(r"\$\{([A-Za-z][A-Za-z0-9_]*)\}")
CSS_VARIABLE_PATTERN = re.compile(r"(--[A-Za-z0-9_-]+)\s*:\s*([^;]+);")
REQUIRED_THEME_TOKENS = {"--color-primary", "--color-accent", "--color-muted"}
BINARY_ASSET_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
MAX_BINARY_ASSET_BYTES = 15 * 1024 * 1024


def command(argv, cwd, timeout=90):
    print("检查:", " ".join(str(part) for part in argv), flush=True)
    subprocess.run(argv, cwd=cwd, check=True, timeout=timeout)


def materialize(files):
    if not isinstance(files, dict):
        raise ValueError("Invalid source payload")
    for name, content in files.items():
        if not isinstance(name, str):
            raise ValueError("Invalid source path")
        path = ROOT / name
        if path.is_absolute() and not path.resolve().is_relative_to(ROOT):
            raise ValueError("Invalid source path")
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            path.write_text(content, encoding="utf-8")
            continue
        relative = path.resolve().relative_to(ROOT)
        if (
            not isinstance(content, dict)
            or content.get("encoding") != "base64"
            or not isinstance(content.get("content"), str)
            or len(relative.parts) < 3
            or relative.parts[:2] != ("frontend", "public")
            or path.suffix.lower() not in BINARY_ASSET_SUFFIXES
        ):
            raise ValueError("Invalid binary source")
        try:
            payload = base64.b64decode(content["content"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("Invalid binary source") from exc
        if not payload or len(payload) > MAX_BINARY_ASSET_BYTES:
            raise ValueError("Invalid binary source")
        path.write_bytes(payload)
    os.environ["DATABASE_URL"] = f"sqlite:///{DATABASE}"
    os.environ["DEBUG"] = "false"


FRONTEND_BIN_SCRIPTS = {
    "tsc": "typescript/bin/tsc",
    "vite": "vite/bin/vite.js",
}


def _frontend_bin(name: str) -> Path:
    path = ROOT / "frontend" / "node_modules" / ".bin" / name
    if not path.exists():
        raise RuntimeError(f"前端依赖未安装或不完整：缺少 {path.name}")
    return path


def frontend_command(name: str, *args: str, timeout: int = 90):
    """Run a local frontend CLI via node (npm .bin shims may lack +x under nobody)."""

    _frontend_bin(name)
    script = FRONTEND_BIN_SCRIPTS.get(name)
    if script is None:
        raise RuntimeError(f"未配置前端命令入口：{name}")
    entry = ROOT / "frontend" / "node_modules" / script
    if not entry.is_file():
        raise RuntimeError(f"前端依赖未安装或不完整：缺少 {script}")
    command(["node", str(entry), *args], ROOT / "frontend", timeout=timeout)


def _npm_package_specs(actual: dict, expected: dict) -> list[str]:
    """Return name@version specs present in generated manifest but not the template."""

    specs: list[str] = []
    for section in ("dependencies", "devDependencies"):
        wanted = actual.get(section) or {}
        baseline = expected.get(section) or {}
        if not isinstance(wanted, dict):
            raise ValueError(f"frontend/package.json 的 {section} 必须是对象")
        for name, version in wanted.items():
            if not isinstance(name, str) or not name.strip():
                continue
            if baseline.get(name) == version:
                continue
            if isinstance(version, str) and version.strip():
                specs.append(f"{name}@{version.strip()}")
            else:
                specs.append(name.strip())
    return specs


def ensure_frontend_dependencies():
    """Install from generated package.json; reuse template modules when unchanged."""

    frontend_root = ROOT / "frontend"
    package_json = frontend_root / "package.json"
    if not package_json.is_file():
        raise ValueError("缺少 frontend/package.json")
    actual = json.loads(package_json.read_text(encoding="utf-8"))
    expected = json.loads(TEMPLATE_FRONTEND_PACKAGE.read_text(encoding="utf-8"))
    modules = frontend_root / "node_modules"
    same_manifest = actual.get("dependencies") == expected.get("dependencies") and actual.get(
        "devDependencies"
    ) == expected.get("devDependencies")
    if same_manifest:
        modules.mkdir(exist_ok=True)
        for dependency in TEMPLATE_FRONTEND_MODULES.iterdir():
            if dependency.name.startswith(".vite"):
                continue
            link = modules / dependency.name
            if not link.exists():
                link.symlink_to(dependency)
        print("检查: 前端依赖与模板一致，复用预装模块", flush=True)
        return
    extras = _npm_package_specs(actual, expected)
    if not extras:
        raise ValueError("前端依赖清单与模板不一致，但未能解析出需要安装的包")
    print("检查: 按生成清单增量安装前端依赖", flush=True)
    # Seed template node_modules so vite/rollup native binaries stay intact, then
    # install only packages that differ from the template baseline.
    if modules.exists():
        command(["rm", "-rf", str(modules)], frontend_root, timeout=30)
    command(
        ["cp", "-a", str(TEMPLATE_FRONTEND_MODULES), str(modules)],
        frontend_root,
        timeout=60,
    )
    command(
        ["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund", "--no-save", *extras],
        frontend_root,
        timeout=180,
    )


def ensure_backend_dependencies(backend: Path):
    """Install from generated pyproject when it diverges from the template baseline."""

    pyproject = backend / "pyproject.toml"
    if not pyproject.is_file():
        raise ValueError("缺少 backend/pyproject.toml")
    expected = tomllib.loads(TEMPLATE_BACKEND_PYPROJECT.read_text(encoding="utf-8"))
    actual = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    expected_deps = list(expected["project"]["dependencies"])
    actual_deps = list(actual["project"]["dependencies"])
    if actual_deps == expected_deps:
        print("检查: 后端依赖与模板一致，复用预装模块", flush=True)
        return
    if not actual_deps:
        raise ValueError("backend/pyproject.toml 的 project.dependencies 不能为空")
    print("检查: 按生成清单安装后端依赖", flush=True)
    command(
        ["pip", "install", "--no-cache-dir", "--user", *actual_deps],
        backend,
        timeout=180,
    )


def database():
    backend = ROOT / "backend"
    command(["python", "-m", "compileall", "-q", "app", "alembic"], backend)
    ensure_backend_dependencies(backend)
    command(["alembic", "upgrade", "head"], backend)
    command(["alembic", "check"], backend)
    if not DATABASE.is_file():
        raise ValueError("迁移没有创建检查数据库")
    with sqlite3.connect(DATABASE) as db:
        tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        schema = {}
        for (table,) in tables:
            quoted = table.replace('"', '""')
            schema[table] = db.execute(f'PRAGMA table_info("{quoted}")').fetchall()
        print("实际表结构:", json.dumps(schema, ensure_ascii=False), flush=True)


def wait_http(url, process, label):
    for _ in range(80):
        if process.poll() is not None:
            raise RuntimeError(f"{label}启动失败，请查看上方日志")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return response.read()
        except OSError:
            time.sleep(0.25)
    raise RuntimeError(f"{label}健康检查超时")


def stop_process(process):
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _validate_local_path(value, *, api=False):
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
        raise ValueError(f"联调路径必须是站内绝对路径：{value!r}")
    if api and not value.startswith("/api/v1/"):
        raise ValueError(f"API 联调路径必须位于 /api/v1：{value}")
    return value


def _source_path(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"源码路径无效：{value!r}")
    candidate = (ROOT / value).resolve()
    if not candidate.is_relative_to(ROOT) or candidate == ROOT:
        raise ValueError(f"源码路径越界：{value!r}")
    return candidate


def _validate_contract_object(value, fields, label, *, extra_fields=()):
    if not isinstance(value, dict):
        raise ValueError(f"{label} 必须是对象")
    allowed = {*fields, *extra_fields}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{label} 包含契约外字段：{', '.join(unknown)}")
    for name, rule in fields.items():
        if name not in value:
            raise ValueError(f"{label} 缺少字段：{name}")
        item = value[name]
        kind = rule.get("type")
        if kind == "string":
            if not isinstance(item, str) or len(item) < int(rule.get("min_length", 0)):
                raise ValueError(f"{label}.{name} 必须是有效字符串")
            pattern = rule.get("pattern")
            if pattern and re.fullmatch(pattern, item) is None:
                raise ValueError(f"{label}.{name} 格式无效")
        elif kind == "string_array":
            minimum = int(rule.get("min_items", 0))
            maximum = int(rule.get("max_items", len(item) if isinstance(item, list) else 0))
            item_minimum = int(rule.get("item_min_length", 0))
            if (
                not isinstance(item, list)
                or not minimum <= len(item) <= maximum
                or not all(
                    isinstance(candidate, str) and len(candidate) >= item_minimum
                    for candidate in item
                )
            ):
                raise ValueError(f"{label}.{name} 必须是有效字符串数组")
        elif kind == "local_path":
            _validate_local_path(item)
        elif kind == "integer":
            if isinstance(item, bool) or not isinstance(item, int):
                raise ValueError(f"{label}.{name} 必须是整数")
            if item < int(rule.get("minimum", item)) or item > int(rule.get("maximum", item)):
                raise ValueError(f"{label}.{name} 超出允许范围")
        else:
            raise ValueError(f"平台检查契约字段类型无效：{kind}")


def _css_variables(content):
    return {
        name: re.sub(r"\s+", " ", value.strip()).lower()
        for name, value in CSS_VARIABLE_PATTERN.findall(content)
    }


def load_smoke_manifest():
    if not SMOKE_MANIFEST.is_file():
        raise ValueError("缺少 forgeai.smoke.json，无法验证真实前后端联调")
    if SMOKE_MANIFEST.stat().st_size > 256 * 1024:
        raise ValueError("forgeai.smoke.json 超过 256KB")
    manifest = json.loads(SMOKE_MANIFEST.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ValueError("forgeai.smoke.json 必须是 version=1 的对象")
    unknown_top_level = sorted(set(manifest) - set(SMOKE_CONTRACT["top_level_fields"]))
    if unknown_top_level:
        raise ValueError("forgeai.smoke.json 包含未授权顶层字段：" + ", ".join(unknown_top_level))
    api_checks = manifest.get("api_checks")
    browser_checks = manifest.get("browser_checks")
    if not isinstance(api_checks, list) or not 1 <= len(api_checks) <= 50:
        raise ValueError("forgeai.smoke.json 必须包含 1～50 个 api_checks")
    if not isinstance(browser_checks, list) or not 1 <= len(browser_checks) <= 20:
        raise ValueError("forgeai.smoke.json 必须包含 1～20 个 browser_checks")
    visual_contract = manifest.get("visual_contract")
    template_metadata = json.loads((ROOT / "template.json").read_text(encoding="utf-8"))
    if template_metadata.get("template_version") == "fullstack-react-v1":
        if not isinstance(visual_contract, dict):
            raise ValueError("React 项目必须在 forgeai.smoke.json 声明 visual_contract")
        theme_file = visual_contract.get("theme_file")
        if not isinstance(theme_file, str) or not theme_file.startswith("frontend/"):
            raise ValueError("visual_contract.theme_file 必须指向 frontend 内的主题文件")
        _source_path(theme_file)
        required_tokens = visual_contract.get("required_tokens")
        if not isinstance(required_tokens, list) or not all(
            isinstance(token, str) and token.startswith("--") for token in required_tokens
        ):
            raise ValueError("visual_contract.required_tokens 必须是 CSS 变量名数组")
        if not REQUIRED_THEME_TOKENS.issubset(required_tokens):
            raise ValueError("视觉合同必须包含主色、强调色和中性色语义 token")
        theme_mode = visual_contract.get("theme_mode")
        if theme_mode not in {"template", "custom", "preserve"}:
            raise ValueError("visual_contract.theme_mode 必须是 template、custom 或 preserve")
        theme_reason = visual_contract.get("theme_reason")
        if not isinstance(theme_reason, str) or len(theme_reason.strip()) < 8:
            raise ValueError("visual_contract.theme_reason 必须说明当前配色意图")
        local_assets = visual_contract.get("local_assets", [])
        if not isinstance(local_assets, list) or len(local_assets) > 30:
            raise ValueError("visual_contract.local_assets 必须是最多 30 项的数组")
        for asset in local_assets:
            _source_path(asset)
    seen = set()
    for check in api_checks:
        if (
            not isinstance(check, dict)
            or not isinstance(check.get("id"), str)
            or not check["id"].strip()
        ):
            raise ValueError("每个 API 联调项都必须有字符串 id")
        if check["id"] in seen:
            raise ValueError(f"联调项 id 重复：{check['id']}")
        seen.add(check["id"])
        method = str(check.get("method") or "GET").upper()
        if method not in SMOKE_METHODS:
            raise ValueError(f"不支持的 API 联调方法：{method}")
        _validate_local_path(check.get("path"), api=True)
        status = check.get("expect_status", 200)
        if not isinstance(status, int) or not 100 <= status <= 599:
            raise ValueError(f"API 联调状态码无效：{status!r}")
        for field in ("expect_json_paths",):
            value = check.get(field, [])
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ValueError(f"{field} 必须是字符串数组")
        capture = check.get("capture", {})
        if not isinstance(capture, dict) or not all(
            isinstance(key, str)
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key)
            and isinstance(value, str)
            for key, value in capture.items()
        ):
            raise ValueError("capture 必须把变量名映射到 JSON 路径")
        headers = check.get("headers", {})
        if not isinstance(headers, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in headers.items()
        ):
            raise ValueError("headers 必须是字符串到字符串的对象")
    for check in browser_checks:
        if (
            not isinstance(check, dict)
            or not isinstance(check.get("id"), str)
            or not check["id"].strip()
        ):
            raise ValueError("每个浏览器联调项都必须有字符串 id")
        if check["id"] in seen:
            raise ValueError(f"联调项 id 重复：{check['id']}")
        seen.add(check["id"])
        _validate_local_path(check.get("route"))
        viewport = check.get("viewport")
        if viewport is not None:
            _validate_contract_object(viewport, BROWSER_CONTRACT["viewport"], "browser viewport")
        expect_api = check.get("expect_api", [])
        if not isinstance(expect_api, list) or not expect_api:
            raise ValueError(f"浏览器联调项 {check['id']} 必须声明 expect_api")
        for path in expect_api:
            _validate_local_path(path, api=True)
        expect_text = check.get("expect_text", [])
        if not isinstance(expect_text, list) or not all(
            isinstance(item, str) and item for item in expect_text
        ):
            raise ValueError("expect_text 必须是非空字符串数组")
        actions = check.get("actions", [])
        if not isinstance(actions, list) or len(actions) > 20:
            raise ValueError("browser actions 必须是最多 20 项的数组")
        for action in actions:
            if not isinstance(action, dict) or action.get("type") not in SMOKE_ACTIONS:
                raise ValueError(f"浏览器动作无效：{action!r}")
            action_type = action["type"]
            _validate_contract_object(
                action,
                SMOKE_ACTIONS[action_type]["fields"],
                f"浏览器动作 {action_type}",
                extra_fields=("type",),
            )
    return manifest


def validate_visual_contract(manifest):
    contract = manifest.get("visual_contract")
    if not contract:
        return
    theme_path = _source_path(contract["theme_file"])
    if not theme_path.is_file():
        raise ValueError(f"视觉主题文件不存在：{contract['theme_file']}")
    theme_variables = _css_variables(theme_path.read_text(encoding="utf-8"))
    for token in contract["required_tokens"]:
        if token not in theme_variables:
            raise ValueError(f"视觉主题缺少语义 token：{token}")
    for asset in contract.get("local_assets", []):
        asset_path = _source_path(asset)
        if not asset_path.is_file():
            raise ValueError(f"视觉本地资产不存在：{asset}")

    business_api = any(
        check["path"].split("?", 1)[0] != "/api/v1/health" for check in manifest["api_checks"]
    )
    if not business_api:
        return
    theme_mode = contract["theme_mode"]
    if theme_mode == "template":
        raise ValueError(
            "forgeai.smoke.json 的 visual_contract.theme_mode 不能为 template："
            "新视觉使用 custom；用户明确要求保留已批准配色时使用 preserve"
        )
    if theme_mode == "preserve":
        return
    baseline_variables = _css_variables(TEMPLATE_THEME_FILE.read_text(encoding="utf-8"))
    changed = sum(
        theme_variables.get(token) != baseline_variables.get(token)
        for token in REQUIRED_THEME_TOKENS
    )
    if changed < 2:
        raise ValueError("custom 主题仍在沿用模板默认配色；至少重写主色、强调色、中性色中的两项")


def _render_variables(value, variables):
    if isinstance(value, str):

        def replace(match):
            name = match.group(1)
            if name not in variables:
                raise ValueError(f"联调变量尚未捕获：{name}")
            return str(variables[name])

        return VARIABLE_PATTERN.sub(replace, value)
    if isinstance(value, list):
        return [_render_variables(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: _render_variables(item, variables) for key, item in value.items()}
    return value


def _json_path_parts(path):
    value = str(path).strip()
    if not value:
        raise ValueError("JSON 路径不能为空")
    if value == "$":
        return []
    if value.startswith("$"):
        value = value[1:]
    value = re.sub(r"\[(\d+)\]", r".\1", value)
    value = value.removeprefix(".")
    parts = value.split(".") if value else []
    if any(not part for part in parts) or any("[" in part or "]" in part for part in parts):
        raise ValueError(f"JSON 路径格式无效：{path}")
    return parts


def _json_path(document, path):
    current = document
    for part in _json_path_parts(path):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise ValueError(f"响应缺少 JSON 路径：{path}")
    return current


def run_api_smoke(manifest, base_url):
    variables = {}
    for raw_check in manifest["api_checks"]:
        check = _render_variables(raw_check, variables)
        method = str(check.get("method") or "GET").upper()
        path = _validate_local_path(check["path"], api=True)
        body = check.get("json")
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json", **(check.get("headers") or {})}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{base_url}{path}", data=payload, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
                response_body = response.read()
        except urllib.error.HTTPError as response:
            status = response.code
            response_body = response.read()
        expected = int(check.get("expect_status", 200))
        if status != expected:
            detail = response_body.decode("utf-8", errors="replace")[:500]
            raise RuntimeError(
                f"API 联调失败 {check['id']}：期望 HTTP {expected}，实际 {status}；{detail}"
            )
        document = None
        paths = list(check.get("expect_json_paths") or [])
        capture = check.get("capture") or {}
        if paths or capture:
            try:
                document = json.loads(response_body)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"API 联调 {check['id']} 未返回 JSON") from exc
        for json_path in paths:
            _json_path(document, json_path)
        for name, json_path in capture.items():
            variables[name] = _json_path(document, json_path)
        print(f"API 联调通过: {check['id']} {method} {path} -> {status}", flush=True)
    return variables


def run_browser_smoke(manifest, variables, base_url):
    rendered = _render_variables(manifest, variables)
    rendered_path = Path("/tmp/forgeai-smoke-rendered.json")
    rendered_path.write_text(json.dumps(rendered, ensure_ascii=False), encoding="utf-8")
    command(
        ["node", "/opt/forgeai/probe.mjs", base_url, str(rendered_path)],
        ROOT / "frontend",
        timeout=120,
    )


def start_backend(port=8080):
    process = subprocess.Popen(
        ["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT / "backend",
    )
    wait_http(f"http://127.0.0.1:{port}/api/v1/health", process, "后端")
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/openapi.json", timeout=2) as response:
        paths = json.load(response)["paths"]
    print("已注册接口:", json.dumps(paths, ensure_ascii=False)[:12000], flush=True)
    return process


def backend():
    database()
    process = start_backend()
    try:
        return
    finally:
        stop_process(process)


def frontend():
    frontend_root = ROOT / "frontend"
    ensure_frontend_dependencies()
    package = json.loads((frontend_root / "package.json").read_text(encoding="utf-8"))
    dependencies = {
        **(package.get("dependencies") or {}),
        **(package.get("devDependencies") or {}),
    }
    if "typescript" in dependencies:
        build_config = frontend_root / "tsconfig.build.json"
        if build_config.is_file():
            frontend_command("tsc", "-p", build_config.name)
        else:
            frontend_command("tsc", "--noEmit")
    else:
        raise ValueError("frontend/package.json 必须配置 TypeScript 检查器")
    # Vite writes node_modules/.vite-temp while loading its config.
    frontend_command("vite", "build")
    if not (frontend_root / "dist/index.html").is_file():
        raise RuntimeError("构建没有生成 dist/index.html")


def runtime_smoke():
    """Prove real browser journeys call the generated backend with valid contracts."""

    database()
    manifest = load_smoke_manifest()
    validate_visual_contract(manifest)
    backend_process = start_backend(port=8000)
    frontend_process = None
    try:
        frontend()
        variables = run_api_smoke(manifest, "http://127.0.0.1:8000")
        frontend_process = subprocess.Popen(
            [
                "node",
                str(ROOT / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"),
                "--host",
                "127.0.0.1",
                "--port",
                "5173",
                "--strictPort",
            ],
            cwd=ROOT / "frontend",
        )
        html = wait_http("http://127.0.0.1:5173/", frontend_process, "前端联调页面")
        if b"<html" not in html.lower() and b"<!doctype html" not in html.lower():
            raise RuntimeError("前端联调页面没有返回 HTML")
        run_browser_smoke(manifest, variables, "http://127.0.0.1:5173")
        print("真实前后端联调通过：API 契约、页面请求和关键交互均有运行证据。", flush=True)
    finally:
        if frontend_process is not None:
            stop_process(frontend_process)
        stop_process(backend_process)


def visual(route):
    """Render real Chromium screenshots while generated code remains isolated."""

    database()
    backend_process = start_backend(port=8000)
    frontend_process = None
    try:
        frontend()
        frontend_process = subprocess.Popen(
            [
                "node",
                str(ROOT / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"),
                "--host",
                "127.0.0.1",
                "--port",
                "5173",
                "--strictPort",
            ],
            cwd=ROOT / "frontend",
        )
        wait_http("http://127.0.0.1:5173/", frontend_process, "浏览器截图页面")
        command(
            [
                "node",
                "/opt/forgeai/capture.mjs",
                f"http://127.0.0.1:5173{route}",
                "/tmp/evidence",
            ],
            ROOT / "frontend",
            timeout=90,
        )
        print("Chromium 桌面端与移动端截图已生成。", flush=True)
    finally:
        if frontend_process is not None:
            stop_process(frontend_process)
        stop_process(backend_process)


def main():
    check = sys.argv[1]
    if check not in {"database", "backend", "frontend", "all", "visual"}:
        raise ValueError("Unknown check")
    materialize(json.load(sys.stdin))
    if check == "database":
        database()
    if check == "all":
        runtime_smoke()
    elif check == "visual":
        visual(sys.argv[2] if len(sys.argv) > 2 else "/")
    elif check == "backend":
        backend()
    elif check == "frontend":
        frontend()
    print("工程检查通过；持久在线预览发布不在本检查范围内。", flush=True)


if __name__ == "__main__":
    main()
