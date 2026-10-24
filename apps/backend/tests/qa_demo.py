"""Approved demo intent, executable cases and source for the real Docker regression."""

import json
from pathlib import Path

from test_architect import valid_spec

from app.generation.workspace import create_workspace
from app.schemas.app_spec import AppSpec
from app.schemas.qa_suite import QASuite

ORACLES = {
    "ac_track": "登录后新建记录，列表可读取，刷新仍存在",
    "ac_private": "另一账号不能读取私有记录",
    "ac_credentials": "错误凭据不能登录",
    "ac_anonymous": "未登录不能读取记录",
    "ac_invalid": "空标题不能保存",
}


def demo_spec():
    raw = valid_spec().model_dump(mode="json")
    raw["features"] = [{"id": "feat_track", "text": "已登录用户管理自己的私有记录"}]
    raw["acceptance_criteria"] = [
        {"id": key, "text": value, "source_ids": ["feat_track"]} for key, value in ORACLES.items()
    ]
    return AppSpec.model_validate(raw)


def api(path, status=200, *, method="GET", body=None, token=None, assertions=None, capture=None):
    return {
        "kind": "api",
        "method": method,
        "path": path,
        "expect_status": status,
        "json_body": body,
        "headers": {"Authorization": f"Bearer {token}"} if token else {},
        "assertions": assertions or [],
        "capture": capture or {},
    }


def assertion(path, value, operator="equals"):
    return {"path": path, "operator": operator, "value": value}


def login(name):
    return api(
        "/api/v1/login",
        method="POST",
        body={"username": name, "password": "test-only"},
        assertions=[assertion("token", name)],
        capture={name: "token"},
    )


def browser(action, **arguments):
    return {"kind": "browser", "action": action, **arguments}


def demo_suite():
    scripts = {
        "ac_track": [
            browser("navigate", path="/"),
            browser("fill", selector='[data-testid="username"]', value="alice"),
            browser("fill", selector='[data-testid="password"]', value="test-only"),
            browser("click", selector='[data-testid="login"]'),
            browser("wait_for_visible", selector='p:has-text("Signed in as alice")'),
            browser("expect_text", value="Signed in as alice"),
            browser("fill", selector='[data-testid="title"]', value="Browser persistent record"),
            browser("click", selector='[data-testid="save"]'),
            browser("expect_text", value="Browser persistent record"),
            browser("reload"),
            browser("expect_text", value="Browser persistent record"),
        ],
        "ac_private": [
            login("alice"),
            api(
                "/api/v1/records",
                201,
                method="POST",
                token="${alice}",
                body={"title": "Alice private record"},
                assertions=[assertion("title", "Alice private record")],
                capture={"record": "id"},
            ),
            api(
                "/api/v1/records/${record}",
                token="${alice}",
                assertions=[assertion("title", "Alice private record")],
            ),
            login("bob"),
            api("/api/v1/records", token="${bob}", assertions=[assertion("items", 0, "length")]),
            api(
                "/api/v1/records/${record}",
                404,
                token="${bob}",
                assertions=[assertion("detail", "Not found")],
            ),
        ],
        "ac_credentials": [
            api(
                "/api/v1/login",
                403,
                method="POST",
                body={"username": "alice", "password": "wrong"},
                assertions=[assertion("detail", "Wrong credentials")],
            )
        ],
        "ac_anonymous": [
            api("/api/v1/records", 401, assertions=[assertion("detail", "Authentication required")])
        ],
        "ac_invalid": [
            login("alice"),
            api(
                "/api/v1/records",
                422,
                method="POST",
                token="${alice}",
                body={"title": ""},
                assertions=[assertion("detail", 1, "length")],
            ),
        ],
    }
    return QASuite.model_validate(
        {
            "revision": 1,
            "scenarios": [
                {
                    "test_id": f"test_{key}",
                    "acceptance_ids": [key],
                    "objective": oracle,
                    "preconditions": ["隔离 SQLite 应用，两个预置测试账号 alice 和 bob"],
                    "test_data": ["测试密码 test-only；独立记录标题"],
                    "steps": [oracle],
                    "expected_results": [oracle],
                    "category": "journey"
                    if key == "ac_track"
                    else "permission"
                    if key in {"ac_private", "ac_anonymous"}
                    else "negative",
                    "environment": "isolated_browser" if key == "ac_track" else "isolated_api",
                    "approved_oracles": {key: oracle},
                }
                for key, oracle in ORACLES.items()
            ],
            "scripts": {f"test_{key}": {"steps": steps} for key, steps in scripts.items()},
        }
    )


def create_demo(root: Path):
    create_workspace(root)
    fixtures = Path(__file__).with_name("fixtures")
    (root / "backend/app/main.py").write_text(
        (fixtures / "qa_demo_main.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (root / "frontend/src/App.tsx").write_text(
        (fixtures / "qa_demo_App.tsx").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    manifest_path = root / "forgeai.smoke.json"
    smoke = json.loads(manifest_path.read_text(encoding="utf-8"))
    smoke["visual_contract"].update(
        theme_mode="custom", theme_reason="独立 QA 样例使用蓝色主色与紫色强调色"
    )
    theme_path = root / "frontend/src/index.css"
    theme_path.write_text(
        theme_path.read_text(encoding="utf-8")
        .replace("--color-primary: #136f63", "--color-primary: #2355a1")
        .replace("--color-accent: #f4b942", "--color-accent: #8855aa"),
        encoding="utf-8",
    )
    smoke["api_checks"] = [
        {
            "id": "login",
            "method": "POST",
            "path": "/api/v1/login",
            "json": {"username": "alice", "password": "test-only"},
            "expect_status": 200,
            "expect_json_paths": ["token"],
        }
    ]
    smoke["browser_checks"] = [
        {
            "id": "record",
            "route": "/",
            "expect_text": ["Private records"],
            "expect_api": ["/api/v1/login", "/api/v1/records"],
            "actions": [
                {"type": "fill", "selector": '[data-testid="username"]', "value": "alice"},
                {"type": "fill", "selector": '[data-testid="password"]', "value": "test-only"},
                {"type": "click", "selector": '[data-testid="login"]'},
                {"type": "fill", "selector": '[data-testid="title"]', "value": "smoke record"},
                {"type": "click", "selector": '[data-testid="save"]'},
                {"type": "expect_text", "text": "smoke record"},
            ],
        }
    ]
    manifest_path.write_text(json.dumps(smoke, ensure_ascii=False), encoding="utf-8")
