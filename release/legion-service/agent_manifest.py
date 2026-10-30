#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Harness/工具统一 manifest：发现结果先成为草案，锁定后才可安装和注册。"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import urllib.parse
import urllib.request
from typing import Any

SCHEMA_VERSION = 1
_KINDS = {"agent", "agent-platform", "orchestrator", "tool", "capability", "framework"}
_TRANSPORTS = {"cli", "http", "acp", "mcp", "sdk"}


def validate(item: dict, locked: bool = True) -> dict:
    errors = []
    fields = ("key", "name", "kind", "url", "probe") if locked else ("key", "name", "kind", "probe")
    for field in fields:
        if not item.get(field):
            errors.append(f"缺 {field}")
    if item.get("kind") not in _KINDS:
        errors.append(f"不支持的 kind:{item.get('kind')}")
    url = str(item.get("url") or "")
    if url and not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", url):
        errors.append("url 必须是公开 GitHub 仓库")
    if locked:
        if not item.get("version"):
            errors.append("缺 version")
        if not re.fullmatch(r"[0-9a-f]{40}", str(item.get("revision") or "")):
            errors.append("revision 必须是40位 commit SHA")
    provenance = item.get("provenance") or {}
    if provenance.get("official") is False and not provenance.get("description"):
        errors.append("非官方项目必须声明 provenance.description")
    for step in item.get("install") or []:
        if not isinstance(step, list) or not step or not all(isinstance(part, str) for part in step):
            errors.append("install 每一步必须是 argv 字符串数组")
    install_env = item.get("install_env", {})
    if "build_probe" in item:
        probe = item["build_probe"]
        if (not isinstance(probe, dict) or probe.get("method") != "command"
                or not isinstance(probe.get("command"), list) or not probe["command"]
                or not all(isinstance(arg, str) for arg in probe["command"])):
            errors.append("build_probe 必须是 command 类型且包含非空 argv 字符串数组")
    if not isinstance(install_env, dict) or any(not isinstance(k, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) or not isinstance(v, str) or "\x00" in v for k, v in install_env.items()):
        errors.append("install_env 必须为合法环境变量名到字符串值的映射")
    for transport in item.get("transports") or []:
        if not isinstance(transport, dict) or transport.get("type") not in _TRANSPORTS:
            errors.append("transport 必须是 cli/http/acp/mcp/sdk")
    role = str(item.get("execution_role") or "agent")
    if role not in {"agent", "capability", "control-plane", "ui-platform"}:
        errors.append(f"不支持的 execution_role:{role}")
    declared = item.get("adapter_capabilities") or {}
    if role == "agent" and declared.get("tools", True) \
            and not (item.get("provided_tools") or item.get("capabilities")):
        errors.append("tools=true 但没有 provided_tools/capabilities 证据")
    return {"ok": not errors, "errors": errors}


def _read_json(path: pathlib.Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _read_text(path: pathlib.Path, limit: int = 200_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except Exception:
        return ""


def _github_parts(url: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(url.rstrip("/").removesuffix(".git"))
    if parsed.scheme != "https" or parsed.netloc != "github.com":
        raise ValueError("仅支持公开 GitHub HTTPS URL")
    parts = parsed.path.strip("/").split("/")
    if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts):
        raise ValueError("GitHub URL 格式不合法")
    return parts[0], parts[1]


def resolve_revision(url: str, ref: str = "HEAD") -> str:
    _github_parts(url)
    refs = [ref]
    if ref.startswith("refs/tags/") and not ref.endswith("^{}"):
        refs.insert(0, ref + "^{}")
    result = subprocess.run(["git", "ls-remote", url.rstrip("/") + ".git", *refs],
                            capture_output=True, text=True, timeout=90)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError((result.stderr or f"无法解析 ref:{ref}")[-500:])
    rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
    peeled = next((sha for sha, name in rows if name.endswith("^{}")), "")
    return peeled or rows[0][0]


# GitHub release 查询单次 urlopen 超时:此函数最多串 3 次 urlopen,原来各 30s→单 agent 最坏
# 挂 90s;check_updates 多 agent 叠加→端点挂 25-60s(还在企业网/GitHub 不可达时全额超时)。
# 收窄到 6s:健康的 GitHub 查询远快于此;不可达时快速失败走 except 返回 ok:False,不拖垮端点。
# 可用 env 覆盖(部署环境 GitHub 慢时调大)。
_GH_TIMEOUT = float(os.environ.get("LEGION_GH_TIMEOUT", "6"))


def _github_token() -> str:
    # GitHub token: GITHUB_TOKEN/GH_TOKEN env first, then gh CLI (keyring). Runtime read, never on disk.
    for name in ('GITHUB_TOKEN', 'GH_TOKEN'):
        value = (os.environ.get(name) or '').strip()
        if value:
            return value
    try:
        result = subprocess.run(['gh', 'auth', 'token'], capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and (result.stdout or '').strip():
            return (result.stdout or '').strip()
    except Exception:
        pass
    return ''


def _github_headers() -> dict:
    # GitHub API unified headers: Authorization when token exists (5000/hr vs anonymous 60/hr).
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'meyo-legion-installer'}
    token = _github_token()
    if token:
        headers['Authorization'] = f'Bearer {token}'
    return headers


def latest_github_release(url: str) -> dict:
    owner, repo = _github_parts(url)
    request = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/releases/latest",
        headers=_github_headers())
    try:
        with urllib.request.urlopen(request, timeout=_GH_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
        tag = str(payload.get("tag_name") or "")
        # Release API 的 target_commitish 常只是 main。先读 ref；若是 annotated tag，
        # 再解一次 tag object 得到真正 commit，避免把 tag object SHA 误报为升级。
        revision = ""
        if tag:
            ref_request = urllib.request.Request(
                f"https://api.github.com/repos/{owner}/{repo}/git/ref/tags/{urllib.parse.quote(tag, safe='')}",
                headers=_github_headers())
            with urllib.request.urlopen(ref_request, timeout=_GH_TIMEOUT) as response:
                ref = json.loads(response.read().decode("utf-8"))
            obj = ref.get("object") or {}
            revision = str(obj.get("sha") or "")
            if obj.get("type") == "tag" and obj.get("url"):
                tag_request = urllib.request.Request(str(obj["url"]), headers=_github_headers())
                with urllib.request.urlopen(tag_request, timeout=_GH_TIMEOUT) as response:
                    tag_object = json.loads(response.read().decode("utf-8"))
                revision = str((tag_object.get("object") or {}).get("sha") or revision)
        return {"ok": bool(tag and revision), "tag": tag, "revision": revision,
                "published_at": payload.get("published_at"), "release_url": payload.get("html_url")}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}:{exc}"}


def latest_branch_commit(url: str, branch: str = "main") -> dict:
    """查某分支最新 commit(供 github_commit 更新策略用·locked-main 类 agent)。

    与 latest_github_release 同款返回结构(tag=分支名),让 check_updates/apply_update
    对两种策略走同一条代码路径。单次 urlopen,不吃 release 的 3 连请求配额。
    """
    owner, repo = _github_parts(url)
    request = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/commits/{urllib.parse.quote(branch, safe='')}",
        headers=_github_headers())
    try:
        with urllib.request.urlopen(request, timeout=_GH_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
        sha = str(payload.get("sha") or "")
        commit = payload.get("commit") or {}
        date = (commit.get("committer") or {}).get("date") or (commit.get("author") or {}).get("date")
        return {"ok": bool(sha), "tag": branch, "revision": sha, "published_at": date,
                "release_url": f"https://github.com/{owner}/{repo}/tree/{sha[:12]}"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}:{exc}"}

def discover_local(path: str, url: str = "", ref: str = "HEAD") -> dict:
    """读取项目自述元数据形成候选 manifest；不自动执行、不猜未声明协议。"""
    root = pathlib.Path(path).expanduser().resolve()
    if not root.is_dir():
        return {"ok": False, "error": f"目录不存在:{root}"}
    package = _read_json(root / "package.json")
    pyproject = _read_text(root / "pyproject.toml")
    cargo = _read_text(root / "Cargo.toml")
    readme = _read_text(root / "README.md", 80_000)
    project_name = ""
    if pyproject:
        match = re.search(r"(?m)^\s*name\s*=\s*[\"']([^\"']+)[\"']", pyproject)
        project_name = match.group(1) if match else ""
    name = package.get("name") or project_name or root.name
    key = re.sub(r"[^a-z0-9_.-]+", "-", str(name).lower()).strip("-.")
    runtime: dict[str, Any] = {}
    install: list[list[str]] = []
    evidence = []
    if package:
        runtime["node"] = (package.get("engines") or {}).get("node") or True
        manager = str(package.get("packageManager") or "")
        if manager.startswith("pnpm") or (root / "pnpm-lock.yaml").exists():
            runtime["pnpm"] = manager.split("@", 1)[-1] if "@" in manager else True
            install = [["corepack", "enable"], ["pnpm", "install", "--frozen-lockfile"]]
            if (package.get("scripts") or {}).get("build"):
                install.append(["pnpm", "build"])
        else:
            install = [["npm", "install", "--prefix", "{runtime_dir}", "{source}"]]
        evidence.append("package.json")
    if pyproject or (root / "requirements.txt").exists():
        runtime["python"] = True
        # 追加不覆盖(军团 hermes dogfood 揪出:旧 install=[...] 覆盖赋值→同时有 package.json
        # 和 pyproject 的混合项目·前面识别的 Node 安装步骤被整段冲掉→只装 Python 漏 Node 依赖)。
        install.append(["{python}", "-m", "venv", "{runtime_dir}/venv"])
        if (root / "requirements.txt").exists():
            install.append(["{runtime_python}", "-m", "pip", "install", "-r", "{source}/requirements.txt"])
        install.append(["{runtime_python}", "-m", "pip", "install", "{source}"])
        evidence.extend(x for x in ("pyproject.toml" if pyproject else "", "requirements.txt" if (root / "requirements.txt").exists() else "") if x)
    if cargo:
        runtime["cargo"] = True
        evidence.append("Cargo.toml")
    if (root / "docker-compose.yml").exists() or (root / "compose.yml").exists() or (root / "docker").is_dir():
        runtime["docker"] = True
        evidence.append("Docker/Compose")
    bins = package.get("bin") or {}
    if isinstance(bins, str):
        bins = {key: bins}
    transports = [{"type": "cli", "command": ["{runtime_dir}/node_modules/.bin/" + binary, "{task}"]}
                  for binary in bins][:1]
    if re.search(r"\bACP\b", readme):
        evidence.append("README:ACP")
    if re.search(r"\bMCP\b", readme):
        evidence.append("README:MCP")
    revision = resolve_revision(url, ref) if url else ""
    item = {"key": key, "name": str(name), "kind": "agent", "url": url,
            "version": ref, "revision": revision, "runtime": runtime, "install": install,
            "credentials": {}, "transports": transports,
            "update": {"strategy": "github_release", "channel": "stable", "current_ref": ref} if url else {},
            "probe": ({"method": "cli", "target": next(iter(bins))} if bins else
                      {"method": "build", "target": str(root)}),
            "capabilities": [], "discovery_evidence": evidence, "candidate": True}
    return {"ok": True, "candidate": item, "validation": validate(item, locked=bool(url)),
            "note": "候选仅来自项目机器可读元数据；确认真实运行协议和 smoke 后再锁定注册。"}
