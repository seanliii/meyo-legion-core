#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""军团产品权限边界：除四类对外发布外，全部默认自治。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


AUTO_ALLOWED = frozenset({
    "local_fs", "shell", "network_read", "dependency_install", "github_clone", "local_git",
    "destructive", "billable", "credential_login", "system_security",
})
_TOOL_ACTIONS = {
    "web_search": "network_read", "web_fetch": "network_read", "fetch_url": "network_read",
    "research_search": "network_read", "research_fetch": "network_read",
    "search_source_probe": "network_read", "fetch_url_smart": "network_read",
    "fetch_outlinks": "network_read",
    "search_github": "network_read", "inspect_github_repo": "network_read",
    "install_github_repo": "dependency_install", "gen_image": "billable", "gen_video": "billable",
    "tts": "billable", "text_to_speech": "billable", "deploy_site": "local_fs",
    "model_chat": "billable", "model_decisions": "billable",
    "func_call": "execute", "mcp_call": "execute", "opencli": "execute",
}
CONFIRM_REQUIRED = frozenset({
    "git_push", "pull_request", "release", "public_deploy",
})


@dataclass(frozen=True)
class ProductPermissionPolicy:
    mode: str = "zero_friction_local"
    auto_allowed: list[str] = field(default_factory=lambda: sorted(AUTO_ALLOWED))
    confirm_required: list[str] = field(default_factory=lambda: sorted(CONFIRM_REQUIRED))

    def to_dict(self) -> dict:
        return asdict(self)


def decision(action: str) -> dict:
    action = str(action or "").strip()
    if action in AUTO_ALLOWED:
        return {"allowed": True, "requires_confirmation": False, "action": action}
    if action in CONFIRM_REQUIRED:
        return {"allowed": False, "requires_confirmation": True, "action": action}
    return {"allowed": False, "requires_confirmation": True, "action": action,
            "reason": "未知副作用类型，需确认后执行"}


def capability_action(tool_id: str, params: dict | None = None) -> str:
    """把工具声明映射为副作用能力；不从自然语言 prompt 猜。"""
    tool = str(tool_id or "").split(":")[-1]
    values = params if isinstance(params, dict) else {}
    if tool == "deploy_site" and str(values.get("mode") or "local") == "public":
        return "public_deploy"
    if tool == "skill_finder" and str(values.get("action") or "") == "install_local":
        return "dependency_install"
    if tool.startswith("git_push"):
        return "git_push"
    if tool in _TOOL_ACTIONS:
        return _TOOL_ACTIONS[tool]
    if str(tool_id or "").startswith(("agent_tool:", "external_capability:")):
        return "execute"
    return "local_fs"


def authorize_tool(tool_id: str, params: dict | None = None,
                   policy: dict[str, Any] | None = None) -> dict:
    """工具执行前的统一 Allow/Ask/Deny 判定；未知高影响动作 fail closed。"""
    action = capability_action(tool_id, params)
    defaults = capability_policy()
    effective = {**defaults, **(policy if isinstance(policy, dict) else {})}
    if action in CONFIRM_REQUIRED:
        confirmed = bool((params or {}).get("confirm"))
        return {"decision": "allow" if confirmed else "ask", "action": action,
                "allowed": confirmed, "requires_confirmation": not confirmed}
    field = {"dependency_install": "install", "local_fs": "read", "network_read": "network"}.get(action, action)
    allowed = bool(effective.get(field, action in AUTO_ALLOWED)) if action != "unknown" else False
    return {"decision": "allow" if allowed else "deny", "action": action,
            "allowed": allowed, "requires_confirmation": False,
            "reason": "capability policy denied" if not allowed else ""}


def capability_policy() -> dict:
    return {
        "read": True,
        "write": True,
        "network": True,
        "execute": True,
        "install": True,
        "local_git": True,
        "publish": False,
        "destructive": True,
        "billable": True,
        "credential_login": True,
        "system_security": True,
        "confirm_required": sorted(CONFIRM_REQUIRED),
        "mode": "zero_friction_local",
    }
