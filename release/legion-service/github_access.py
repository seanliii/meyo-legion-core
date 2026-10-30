#!/usr/bin/env python3
"""GitHub access shared by every legion harness without persisting credentials."""
from __future__ import annotations

import os
import shutil
import subprocess


def _system_proxy() -> str:
    """Read the active macOS HTTPS proxy without changing shell or Git config."""
    if sys_platform() != "darwin" or not shutil.which("scutil"):
        return ""
    try:
        result = subprocess.run(["scutil", "--proxy"], capture_output=True, text=True, timeout=3)
    except Exception:
        return ""
    values = {}
    for line in (result.stdout or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.strip().split(":", 1)
        values[key.strip()] = value.strip()
    if values.get("HTTPSEnable") != "1" or not values.get("HTTPSProxy") or not values.get("HTTPSPort"):
        return ""
    return f"http://{values['HTTPSProxy']}:{values['HTTPSPort']}"


def sys_platform() -> str:
    import sys
    return sys.platform


def proxy_url() -> str:
    """Return an explicit proxy first, then the active macOS system proxy."""
    for name in ("GITHUB_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return _system_proxy()


def shared_environment(base: dict | None = None) -> dict:
    """Expose non-secret GitHub capability facts to child harness processes."""
    env = dict(base or os.environ)
    proxy = proxy_url()
    if proxy:
        env.setdefault("GITHUB_PROXY", proxy)
    env["MEYO_GITHUB_AUTHORIZED"] = "1" if token() else "0"
    env["MEYO_GITHUB_AUTH_COMMAND"] = "gh auth login"
    return env


def git_environment() -> dict:
    """Inject a per-process Git proxy; never mutate ~/.gitconfig or shell startup files."""
    env = os.environ.copy()
    proxy = proxy_url()
    if proxy:
        env.update({
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.proxy",
            "GIT_CONFIG_VALUE_0": proxy,
        })
    return env


def token() -> str:
    """Return a user-provided token, falling back to the authenticated gh keyring."""
    for name in ("GITHUB_PERSONAL_ACCESS_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    if not shutil.which("gh"):
        return ""
    try:
        result = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return ""
    return (result.stdout or "").strip() if result.returncode == 0 else ""


def status() -> dict:
    """Describe the shared authorization state without exposing the token."""
    ready = bool(token())
    return {
        "provider": "github",
        "authorized": ready,
        "mode": "authenticated" if ready else "anonymous_public",
        "proxy_configured": bool(proxy_url()),
        "auth_command": "gh auth login",
        "auth_note": (
            "已复用用户的 GitHub 登录；凭证不会写入任务上下文。"
            if ready else
            "公开仓库仍可匿名搜索；需要更高限额或私有仓库时，请用户运行 gh auth login 授权。"
        ),
    }


def request_headers() -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "legion-tool-discovery",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    value = token()
    if value:
        headers["Authorization"] = f"Bearer {value}"
    return headers
