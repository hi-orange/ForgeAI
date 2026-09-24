"""Trusted check controller, baked into the image; never copied from generated source."""

import json
import os
import sqlite3
import subprocess
import sys
import time
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path("/tmp/app")
DATABASE = Path("/tmp/check.db")
TEMPLATE_FRONTEND_PACKAGE = Path("/opt/frontend/package.json")
TEMPLATE_FRONTEND_MODULES = Path("/opt/frontend/node_modules")
TEMPLATE_BACKEND_PYPROJECT = Path("/opt/backend/pyproject.toml")


def command(argv, cwd, timeout=90):
    print("检查:", " ".join(str(part) for part in argv), flush=True)
    subprocess.run(argv, cwd=cwd, check=True, timeout=timeout)


def materialize(files):
    for name, content in files.items():
        path = ROOT / name
        if path.is_absolute() and not path.resolve().is_relative_to(ROOT):
            raise ValueError("Invalid source path")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    os.environ["DATABASE_URL"] = f"sqlite:///{DATABASE}"
    os.environ["DEBUG"] = "false"


FRONTEND_BIN_SCRIPTS = {
    "vue-tsc": "vue-tsc/bin/vue-tsc.js",
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


def start_backend():
    process = subprocess.Popen(
        ["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8080"],
        cwd=ROOT / "backend",
    )
    wait_http("http://127.0.0.1:8080/api/v1/health", process, "后端")
    with urllib.request.urlopen("http://127.0.0.1:8080/openapi.json", timeout=2) as response:
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
    # Vite writes node_modules/.vite-temp while loading its config.
    frontend_command("vue-tsc", "-b")
    frontend_command("vite", "build")
    if not (frontend_root / "dist/index.html").is_file():
        raise RuntimeError("构建没有生成 dist/index.html")


def runtime_smoke():
    """Prove that the exact generated revision can start and serve a real page."""

    database()
    backend_process = start_backend()
    preview_process = None
    try:
        frontend()
        preview_process = subprocess.Popen(
            [
                "node",
                str(ROOT / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"),
                "preview",
                "--host",
                "127.0.0.1",
                "--port",
                "4173",
                "--strictPort",
            ],
            cwd=ROOT / "frontend",
        )
        html = wait_http("http://127.0.0.1:4173/", preview_process, "前端预览")
        if b"<html" not in html.lower() and b"<!doctype html" not in html.lower():
            raise RuntimeError("前端预览没有返回 HTML 页面")
        with urllib.request.urlopen("http://127.0.0.1:8080/api/v1/health", timeout=2) as response:
            if response.status != 200:
                raise RuntimeError("运行时后端健康检查未返回 200")
        print("隔离运行冒烟通过：前端页面与后端 API 均可访问。", flush=True)
    finally:
        if preview_process is not None:
            stop_process(preview_process)
        stop_process(backend_process)


def main():
    check = sys.argv[1]
    if check not in {"database", "backend", "frontend", "all"}:
        raise ValueError("Unknown check")
    materialize(json.load(sys.stdin))
    if check == "database":
        database()
    if check == "all":
        runtime_smoke()
    elif check == "backend":
        backend()
    elif check == "frontend":
        frontend()
    print("工程检查通过；持久在线预览发布不在本检查范围内。", flush=True)


if __name__ == "__main__":
    main()
