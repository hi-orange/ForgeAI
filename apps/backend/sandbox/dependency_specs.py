"""Registry-only dependency specifications; values are passed as argv, never shell."""

import re

from packaging.requirements import InvalidRequirement, Requirement

NPM_SPEC = re.compile(
    r"^(?:@[A-Za-z0-9][A-Za-z0-9._-]*/)?[A-Za-z0-9][A-Za-z0-9._-]*"
    r"(?:@[A-Za-z0-9*^~<>=][A-Za-z0-9.*^~<>=| -]*)?$"
)


def validate_package_spec(spec: str, manager: str, action: str) -> None:
    if not spec or len(spec) > 256 or any(ord(char) < 32 for char in spec):
        raise ValueError("非法包名或版本规格")
    if manager == "uv":
        try:
            requirement = Requirement(spec)
        except InvalidRequirement as exc:
            raise ValueError("非法 Python 包规格") from exc
        if requirement.url is not None:
            raise ValueError("依赖仅允许包注册表，不允许 URL、本地路径或 VCS")
        if action == "remove" and (
            requirement.extras or requirement.specifier or requirement.marker
        ):
            raise ValueError("移除 Python 依赖时仅提供包名")
    elif manager == "npm":
        if not NPM_SPEC.fullmatch(spec):
            raise ValueError("非法 npm 注册表包规格")
    else:
        raise ValueError("不支持的包管理器")
