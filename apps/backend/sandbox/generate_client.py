"""Export the real FastAPI schema and generate a client using image-owned tooling."""

import base64
import importlib.util
import json
import shutil
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("forgeai_check", Path(__file__).with_name("check.py"))
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)


def main():
    checks.materialize(json.load(sys.stdin))
    backend = checks.ROOT / "backend"
    checks.command(["python", "-m", "compileall", "-q", "app"], backend)
    checks.ensure_backend_dependencies(backend)
    work = Path("/tmp/sdk-work")
    work.mkdir()
    checks.command(
        [
            "python",
            "-c",
            "import json; from app.main import app; "
            "from pathlib import Path; "
            "Path('/tmp/sdk-work/openapi.json').write_text("
            "json.dumps(app.openapi(),ensure_ascii=False),encoding='utf-8')",
        ],
        backend,
    )
    schema = json.loads((work / "openapi.json").read_text(encoding="utf-8"))
    if not isinstance(schema, dict) or not isinstance(schema.get("paths"), dict):
        raise ValueError("FastAPI 未导出有效 OpenAPI")
    (work / "node_modules").symlink_to("/opt/frontend/node_modules")
    (work / "package.json").write_text('{"type":"module"}', encoding="utf-8")
    shutil.copyfile("/opt/forgeai/openapi-ts.config.ts", work / "openapi-ts.config.ts")
    package_root = Path("/opt/frontend/node_modules/@hey-api/openapi-ts")
    package = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
    entry = package["bin"]
    entry = entry["openapi-ts"] if isinstance(entry, dict) else entry
    checks.command(["node", str(package_root / entry)], work, timeout=120)
    output = work / "src/client"
    if not (output / "types.gen.ts").is_file() or not (output / "sdk.gen.ts").is_file():
        raise ValueError("API 生成器未输出类型或 SDK")
    files = {"frontend/openapi.json": (work / "openapi.json").read_text(encoding="utf-8")}
    for path in sorted(output.rglob("*")):
        if path.is_file():
            if path.is_symlink() or path.suffix != ".ts":
                raise ValueError("API 生成器输出了不支持的文件")
            files["frontend/src/client/" + path.relative_to(output).as_posix()] = path.read_text(
                encoding="utf-8"
            )
    payload = json.dumps(files, ensure_ascii=False).encode()
    if len(files) > 128 or len(payload) > 2 * 1024 * 1024:
        raise ValueError("API 客户端超过生成上限")
    print("FORGEAI_API_CLIENT=" + base64.b64encode(payload).decode(), flush=True)


if __name__ == "__main__":
    main()
