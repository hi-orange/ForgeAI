"""Pure smoke contract validation shared by staging, preflight and sandbox execution."""

import json
import re
from pathlib import Path

SMOKE_CONTRACT = json.loads(
    Path(__file__).with_name("smoke_contract.json").read_text(encoding="utf-8")
)
BROWSER_CONTRACT = SMOKE_CONTRACT["browser"]
API_CONTRACT = SMOKE_CONTRACT["api"]
SMOKE_ACTIONS = BROWSER_CONTRACT["actions"]
SMOKE_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
REQUIRED_THEME_TOKENS = {"--color-primary", "--color-accent", "--color-muted"}


def browser_smoke_timeout_seconds(manifest: dict) -> int:
    """Budget the full serial browser manifest without allowing an unbounded run."""

    limits = SMOKE_CONTRACT["limits"]
    checks = manifest["browser_checks"]
    action_count = sum(len(check.get("actions", [])) for check in checks)
    return min(
        limits["browser_timeout_max_seconds"],
        limits["browser_timeout_seconds"]
        + max(0, len(checks) - 1) * limits["browser_timeout_per_additional_check_seconds"]
        + action_count * limits["browser_timeout_per_action_seconds"],
    )


def all_check_timeout_seconds(manifest: dict, configured_timeout: int) -> int:
    """Leave bounded time for builds and API probes before the browser budget."""

    limits = SMOKE_CONTRACT["limits"]
    return max(
        10,
        min(
            configured_timeout,
            limits["all_timeout_max_seconds"],
            browser_smoke_timeout_seconds(manifest) + limits["browser_setup_timeout_seconds"],
        ),
    )


def parse_smoke_manifest(content: str, *, root: Path, template_metadata: dict) -> dict:
    if len(content.encode("utf-8")) > SMOKE_CONTRACT["limits"]["manifest_bytes"]:
        raise ValueError("forgeai.smoke.json 超过 256KB")
    return validate_smoke_manifest(
        json.loads(content), root=root, template_metadata=template_metadata
    )


def _validate_local_path(value, *, api=False):
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError(f"联调路径必须是站内绝对路径：{value!r}")
    if api and not value.startswith("/api/v1/"):
        raise ValueError(f"API 联调路径必须位于 /api/v1：{value}")
    return value


def _source_path(root, value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"源码路径无效：{value!r}")
    candidate = (root / value).resolve()
    if not candidate.is_relative_to(root) or candidate == root:
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


def validate_smoke_manifest(manifest: object, *, root: Path, template_metadata: dict) -> dict:
    if not isinstance(template_metadata, dict):
        raise ValueError("template.json 必须是对象")
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ValueError("forgeai.smoke.json 必须是 version=1 的对象")
    unknown_top_level = sorted(set(manifest) - set(SMOKE_CONTRACT["top_level_fields"]))
    if unknown_top_level:
        raise ValueError("forgeai.smoke.json 包含未授权顶层字段：" + ", ".join(unknown_top_level))
    api_checks = manifest.get("api_checks")
    browser_checks = manifest.get("browser_checks")
    limits = SMOKE_CONTRACT["limits"]
    errors = []
    for field, checks in (("api_checks", api_checks), ("browser_checks", browser_checks)):
        if not isinstance(checks, list) or not 1 <= len(checks) <= limits[field]:
            count = len(checks) if isinstance(checks, list) else "非数组"
            errors.append(f"{field}: 实际 {count}，允许 1～{limits[field]} 项")
    total_actions = 0
    for check in browser_checks if isinstance(browser_checks, list) else []:
        if not isinstance(check, dict):
            continue
        actions = check.get("actions", [])
        if not isinstance(actions, list) or len(actions) > limits["actions_per_check"]:
            count = len(actions) if isinstance(actions, list) else "非数组"
            errors.append(
                f"browser_checks[{check.get('id', '?')}].actions: 实际 {count}，"
                f"上限 {limits['actions_per_check']}"
            )
        if isinstance(actions, list):
            total_actions += len(actions)
    if total_actions > limits["total_browser_actions"]:
        errors.append(
            f"browser actions 总数 {total_actions}，上限 {limits['total_browser_actions']}"
        )
    if errors:
        raise ValueError("forgeai.smoke.json: " + "; ".join(errors))
    assert isinstance(api_checks, list) and isinstance(browser_checks, list)
    visual_contract = manifest.get("visual_contract")
    if template_metadata.get("template_version") == "fullstack-react-v1":
        if not isinstance(visual_contract, dict):
            raise ValueError("React 项目必须在 forgeai.smoke.json 声明 visual_contract")
        theme_file = visual_contract.get("theme_file")
        if not isinstance(theme_file, str) or not theme_file.startswith("frontend/"):
            raise ValueError("visual_contract.theme_file 必须指向 frontend 内的主题文件")
        _source_path(root, theme_file)
        required_tokens = visual_contract.get("required_tokens")
        if not isinstance(required_tokens, list) or not all(
            isinstance(token, str) and token.startswith("--") for token in required_tokens
        ):
            raise ValueError("visual_contract.required_tokens 必须是 CSS 变量名数组")
        if not REQUIRED_THEME_TOKENS.issubset(required_tokens):
            raise ValueError("视觉合同必须包含主色、强调色和中性色语义 token")
        theme_mode = visual_contract.get("theme_mode")
        if not isinstance(theme_mode, str) or theme_mode not in {"template", "custom", "preserve"}:
            raise ValueError("visual_contract.theme_mode 必须是 template、custom 或 preserve")
        theme_reason = visual_contract.get("theme_reason")
        if not isinstance(theme_reason, str) or len(theme_reason.strip()) < 8:
            raise ValueError("visual_contract.theme_reason 必须说明当前配色意图")
        local_assets = visual_contract.get("local_assets", [])
        if not isinstance(local_assets, list) or len(local_assets) > 30:
            raise ValueError("visual_contract.local_assets 必须是最多 30 项的数组")
        for asset in local_assets:
            _source_path(root, asset)
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
        unknown = sorted(set(check) - set(API_CONTRACT["fields"]))
        if unknown:
            advice = ""
            if "auth" in unknown:
                advice = (
                    "；auth 不受支持：先在登录探针用 capture "
                    '{"seeker_token":"data.access_token"} 捕获令牌，'
                    '后续请求用 headers {"Authorization":"Bearer ${seeker_token}"}'
                )
            raise ValueError(
                f"api_checks[{check['id']}] 包含契约外字段：{', '.join(unknown)}{advice}"
            )
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
    missing_routes = []
    for index, check in enumerate(browser_checks):
        if (
            not isinstance(check, dict)
            or not isinstance(check.get("id"), str)
            or not check["id"].strip()
        ):
            raise ValueError("每个浏览器联调项都必须有字符串 id")
        if check["id"] in seen:
            raise ValueError(f"联调项 id 重复：{check['id']}")
        seen.add(check["id"])
        route_label = f"browser_checks[{index}][{check['id']}].route"
        if "route" not in check:
            missing_routes.append(route_label)
            continue
        try:
            _validate_local_path(check["route"])
        except ValueError as exc:
            raise ValueError(f"{route_label}: {exc}") from exc
        viewport = check.get("viewport")
        if viewport is not None:
            _validate_contract_object(viewport, BROWSER_CONTRACT["viewport"], "browser viewport")
        expect_api = check.get("expect_api", [])
        kind = check.get("kind", BROWSER_CONTRACT["default_kind"])
        if kind not in BROWSER_CONTRACT["kinds"]:
            raise ValueError(f"浏览器联调项 {check['id']} kind 必须是 integration 或 ui")
        if not isinstance(expect_api, list) or (kind == "integration" and not expect_api):
            raise ValueError(f"浏览器联调项 {check['id']} 必须声明 expect_api")
        for expected in expect_api:
            if isinstance(expected, str):
                _validate_local_path(expected, api=True)
            else:
                _validate_contract_object(
                    expected, BROWSER_CONTRACT["response_fields"], f"{check['id']}.expect_api"
                )
                _validate_local_path(expected["path"], api=True)
        expect_text = check.get("expect_text", [])
        if not isinstance(expect_text, list) or not all(
            isinstance(item, str) and item for item in expect_text
        ):
            raise ValueError("expect_text 必须是非空字符串数组")
        actions = check.get("actions", [])
        if (
            kind == "ui"
            and not expect_text
            and not any(
                isinstance(action, dict) and str(action.get("type", "")).startswith("expect_")
                for action in actions
            )
        ):
            raise ValueError(f"纯 UI 用例 {check['id']} 必须声明可观察断言")
        for index, action in enumerate(actions):
            if not isinstance(action, dict) or action.get("type") not in SMOKE_ACTIONS:
                raise ValueError(f"浏览器动作无效：{action!r}")
            action_type = action["type"]
            _validate_contract_object(
                action,
                SMOKE_ACTIONS[action_type]["fields"],
                f"browser_checks[{check['id']}].actions[{index}] 浏览器动作 {action_type}",
                extra_fields=("type",),
            )
    if missing_routes:
        shown = ", ".join(missing_routes[:8])
        remaining = f"；另有 {len(missing_routes) - 8} 项" if len(missing_routes) > 8 else ""
        raise ValueError(
            f"浏览器联调项缺少必填字段 route（请填写站内绝对路径，例如 /jobs）：{shown}{remaining}"
        )
    return manifest
