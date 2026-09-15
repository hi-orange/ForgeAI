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


def command(argv, cwd):
    print("检查:", " ".join(argv), flush=True)
    subprocess.run(argv, cwd=cwd, check=True, timeout=90)


def materialize(files):
    for name, content in files.items():
        path = ROOT / name
        if path.is_absolute() and not path.resolve().is_relative_to(ROOT):
            raise ValueError("Invalid source path")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    os.environ["DATABASE_URL"] = f"sqlite:///{DATABASE}"
    os.environ["DEBUG"] = "false"


def database():
    backend = ROOT / "backend"
    command(["python", "-m", "compileall", "-q", "app", "alembic"], backend)
    expected = tomllib.loads(Path("/opt/backend/pyproject.toml").read_text())
    actual = tomllib.loads((backend / "pyproject.toml").read_text())
    if actual["project"]["dependencies"] != expected["project"]["dependencies"]:
        raise ValueError("当前检查镜像仅支持模板中的后端依赖，请恢复依赖清单")
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


def backend():
    database()
    process = subprocess.Popen(
        ["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8080"],
        cwd=ROOT / "backend",
    )
    try:
        for _ in range(60):
            if process.poll() is not None:
                raise RuntimeError("后端启动失败，请查看上方日志")
            try:
                with urllib.request.urlopen("http://127.0.0.1:8080/api/v1/health", timeout=1) as r:
                    if r.status != 200:
                        raise RuntimeError("健康检查未返回 200")
                with urllib.request.urlopen("http://127.0.0.1:8080/openapi.json", timeout=1) as r:
                    paths = json.load(r)["paths"]
                print("已注册接口:", json.dumps(paths, ensure_ascii=False)[:12000], flush=True)
                return
            except OSError:
                time.sleep(0.25)
        raise RuntimeError("后端健康检查超时")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def frontend():
    frontend_root = ROOT / "frontend"
    expected = json.loads(Path("/opt/frontend/package.json").read_text())
    actual = json.loads((frontend_root / "package.json").read_text())
    for key in ("dependencies", "devDependencies"):
        if actual.get(key) != expected.get(key):
            raise ValueError("当前检查镜像仅支持模板中的前端依赖，请恢复依赖清单")
    # Vite writes node_modules/.vite-temp while loading its config. Keep packages read-only,
    # but give this check a private writable node_modules directory for those cache entries.
    modules = frontend_root / "node_modules"
    modules.mkdir()
    for dependency in Path("/opt/frontend/node_modules").iterdir():
        if dependency.name.startswith(".vite"):
            continue
        (modules / dependency.name).symlink_to(dependency)
    command(["/opt/frontend/node_modules/.bin/vue-tsc", "-b"], frontend_root)
    command(["/opt/frontend/node_modules/.bin/vite", "build"], frontend_root)
    if not (frontend_root / "dist/index.html").is_file():
        raise RuntimeError("构建没有生成 dist/index.html")


def main():
    check = sys.argv[1]
    if check not in {"database", "backend", "frontend", "all"}:
        raise ValueError("Unknown check")
    materialize(json.load(sys.stdin))
    if check == "database":
        database()
    if check in {"backend", "all"}:
        backend()
    if check in {"frontend", "all"}:
        frontend()
    print("工程检查通过；业务验收和在线预览不在本检查范围内。", flush=True)


if __name__ == "__main__":
    main()
