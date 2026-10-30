#!/usr/bin/env python3.12
# -*- coding: utf-8 -*-
"""harness_autospec.py - GitHub URL -> 自动识别类型 -> 自动生成 catalog spec。
护城河固化的自动适配层(用户 2026-09-02 钉死:一个指令就自动接,全部验证 runnable)。
识别: Electron GUI/npm CLI/Python CLI/HTTP server/Win平台/设备能力后端。
命令一律 {node}/{python} 模板(grok-bot node_binary 血泪),绝不引用 repo 内相对路径。"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import tempfile
import tomllib

ALIYUN = "https://mirrors.aliyun.com/pypi/simple/"


def _read(path):
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _pyproject_meta(text: str) -> dict:
    """从标准 pyproject 元数据取名称/版本/CLI，正则只作损坏 TOML 的保守兜底。"""
    data = {}
    try:
        data = tomllib.loads(text) if text else {}
    except Exception:
        data = {}
    project = data.get("project") or {}
    poetry = ((data.get("tool") or {}).get("poetry") or {})
    scripts = project.get("scripts") or poetry.get("scripts") or {}
    if not isinstance(scripts, dict):
        scripts = {}
    name = str(project.get("name") or poetry.get("name") or "")
    version = str(project.get("version") or poetry.get("version") or "")
    description = str(project.get("description") or poetry.get("description") or "")
    requires_python = str(project.get("requires-python") or "")
    if not name:
        match = re.search(r"(?m)^\s*name\s*=\s*[\"']([^\"']+)[\"']", text or "")
        name = match.group(1) if match else ""
    if not version:
        match = re.search(r"(?m)^\s*version\s*=\s*[\"']([^\"']+)[\"']", text or "")
        version = match.group(1) if match else ""
    return {"name": name, "version": version, "description": description,
            "requires_python": requires_python, "scripts": scripts}


def _source_role(description: str, readme: str) -> dict:
    """依据项目自述的能力证据分流执行角色，不根据用户 query 猜意图。"""
    blob = f"{description}\n{readme}".lower()
    mobile = (("mobile device" in blob or "android and ios" in blob)
              and any(mark in blob for mark in (
                  "tap", "swipe", "adb", "device control", "controlling mobile", "control android")))
    browser = ("browser automation" in blob
               and any(mark in blob for mark in ("playwright", "chromium", "cdp", "browser control")))
    desktop = ("desktop automation" in blob
               and any(mark in blob for mark in ("screenshot", "accessibility", "click", "keyboard")))
    if mobile:
        return {"execution_role": "capability", "catalog_kind": "capability",
                "capabilities": ["mobile device automation", "android", "ios", "device control"],
                "provided_tools": ["mobile_ui", "screenshot", "tap", "swipe", "type"]}
    if browser:
        return {"execution_role": "capability", "catalog_kind": "capability",
                "capabilities": ["browser automation", "web tasks"],
                "provided_tools": ["browser", "navigate", "click", "type", "screenshot"]}
    if desktop:
        return {"execution_role": "capability", "catalog_kind": "capability",
                "capabilities": ["desktop automation", "computer use"],
                "provided_tools": ["desktop", "screenshot", "click", "type", "key"]}
    return {"execution_role": "agent", "catalog_kind": "agent",
            "capabilities": ["cli agent"], "provided_tools": []}


def analyze(repo_dir):
    repo_dir = pathlib.Path(repo_dir)
    pkg_p = repo_dir / "package.json"
    pkg = json.loads(pkg_p.read_text(encoding="utf-8")) if pkg_p.is_file() else {}
    pyproj = _read(repo_dir / "pyproject.toml")
    py_meta = _pyproject_meta(pyproj)
    readme_raw = _read(repo_dir / "README.md")
    readme = readme_raw.lower()
    win_marks = bool(list(repo_dir.glob("*.csproj")) or list(repo_dir.glob("*.sln")))
    if win_marks:
        return {"kind": "windows", "name": repo_dir.name, "version": "main",
                "description": "Windows 平台能力", "hint": "Win平台·登记 cloud-windows-vm capability"}
    if pkg:
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        bins_raw = pkg.get("bin") or {}
        bins = list(bins_raw.keys()) if isinstance(bins_raw, dict) else ([str(pkg.get("name") or repo_dir.name)] if bins_raw else [])
        common = {"name": str(pkg.get("name") or repo_dir.name),
                  "version": str(pkg.get("version") or "main"),
                  "description": str(pkg.get("description") or "")}
        if "electron" in deps or "electron-builder" in deps:
            return {**common, "kind": "gui-electron", "hint": "Electron GUI app·走 GUI 安装路径·不进编排池"}
        if bins:
            role = _source_role(common["description"], readme)
            return {**common, **role, "kind": "npm-cli", "bins": bins, "hint": "npm CLI 型"}
    if "agent-server" in readme or ("fastapi" in readme and "conversations" in readme):
        return {"kind": "http-server", "name": py_meta["name"] or repo_dir.name,
                "version": py_meta["version"] or "main", "description": py_meta["description"],
                "hint": "HTTP server 型·参考 openhands worker 模式"}
    if pyproj or _read(repo_dir / "setup.py"):
        entries = list(py_meta["scripts"].keys())
        entry = entries[:1]
        if not entry:
            match = re.search(r"(?m)^\s*([A-Za-z0-9_-]+)\s*=\s*[\"'][^\"']+:[^\"']+[\"']", pyproj)
            entry = [match.group(1)] if match else []
        role = _source_role(py_meta["description"], readme)
        task_prefix = []
        if entry and re.search(rf"(?m)^\s*{re.escape(entry[0])}\s+run\s+[\"']", readme_raw):
            task_prefix = ["run"]
        source_kind = "py-cli" if entry else "py-package"
        if not entry:
            role = {"execution_role": "capability", "catalog_kind": "capability",
                    "capabilities": ["python library"], "provided_tools": []}
        return {"kind": source_kind, "entry": entry, "task_prefix": task_prefix,
                "name": py_meta["name"] or repo_dir.name, "version": py_meta["version"] or "main",
                "description": py_meta["description"], "requires_python": py_meta["requires_python"],
                **role, "hint": ("Python CLI 型" if entry else "Python 包无 CLI·不猜可执行入口")}
    return {"kind": "unknown", "name": repo_dir.name, "version": "main",
            "description": "自动识别信息不足的外部框架", "hint": "无法自动识别·按 framework capability 登记待扩展"}


def build_spec(key, url, rev, info):
    source_kind = info["kind"]
    role_defaults = {
        "http-server": {"execution_role": "control-plane", "catalog_kind": "agent-platform",
                        "capabilities": ["http agent service"]},
        "windows": {"execution_role": "capability", "catalog_kind": "capability",
                    "capabilities": ["windows automation"]},
        "gui-electron": {"execution_role": "ui-platform", "catalog_kind": "agent-platform",
                         "capabilities": ["desktop application"]},
        "unknown": {"execution_role": "capability", "catalog_kind": "framework",
                    "capabilities": ["external framework"]},
        "py-package": {"execution_role": "capability", "catalog_kind": "capability",
                       "capabilities": ["python library"]},
    }
    role_info = {**role_defaults.get(source_kind, {}),
                 **{k: info[k] for k in ("execution_role", "catalog_kind", "capabilities", "provided_tools")
                    if k in info}}
    role = role_info.get("execution_role", "agent")
    catalog_kind = role_info.get("catalog_kind", "agent")
    capabilities = list(role_info.get("capabilities") or [f"{source_kind} runtime"])
    provided_tools = list(role_info.get("provided_tools") or [])
    spec = {
        "key": key,
        "name": str(info.get("name") or key),
        "kind": catalog_kind,
        "url": url,
        "version": str(info.get("version") or "main"),
        "revision": rev,
        "description": str(info.get("description") or f"{source_kind} 外部成员"),
        "runtime": {},
        "install": [],
        "credentials": {"any_of": ["MEYO_UNIFIED_APPID"], "signup_url": url,
                        "help": "autospec 自动生成·六环验收"},
        "transports": [],
        "probe": {"method": "path", "target": "{source}"},
        "execution_role": role,
        "capabilities": capabilities,
        "update": {"strategy": "github_commit", "branch": "main", "current_ref": "main"},
        "adapter_capabilities": {"streaming": "text", "tools": role in {"agent", "capability"},
                                 "artifacts": role == "agent", "model": "runtime"},
    }
    if provided_tools:
        spec["provided_tools"] = provided_tools
    if source_kind == "npm-cli":
        bin_name = info["bins"][0]
        spec["runtime"] = {"node": True, "npm": True}
        spec["install"] = [["npm", "install", "--prefix", "{runtime_dir}", "{source}"]]
        spec["transports"] = [{"type": "cli",
                               "command": ["{runtime_dir}/node_modules/.bin/" + bin_name, "{task}"],
                               "streaming_protocol": "text", "final_nonempty_line": True}]
        spec["probe"] = {"method": "cli", "target": "{runtime_dir}/node_modules/.bin/" + bin_name,
                         "command": ["{runtime_dir}/node_modules/.bin/" + bin_name, "--help"]}
    elif source_kind == "py-cli":
        entry = info["entry"][0]
        binary = "{runtime_dir}/venv/bin/" + entry
        spec["runtime"] = {"python": info.get("requires_python") or True}
        spec["install"] = [["{python}", "-m", "venv", "{runtime_dir}/venv"],
                           ["{runtime_python}", "-m", "pip", "install", "-i", ALIYUN, "."]]
        spec["transports"] = [{"type": "cli",
                               "command": [binary, *(info.get("task_prefix") or []), "{task}"],
                               "streaming_protocol": "text", "final_nonempty_line": True}]
        spec["probe"] = {"method": "cli", "target": binary,
                         "command": [binary, "--help"]}
        if role == "capability" and "mobile device automation" in capabilities:
            spec["transports"] = []
            spec["credentials"] = {"any_of": [], "signup_url": url,
                                   "help": "低层设备动作无需模型凭证；自然语言 task 复用 MEYO_UNIFIED_APPID"}
            spec["capability_actions"] = {
                "devices": {"description": "列出当前连接的移动设备", "command": [binary, "devices"], "params": []},
                "doctor": {"description": "诊断并修复设备、Portal、无障碍和截图链路", "command": [binary, "doctor"], "params": [], "timeout_s": 240},
                "screenshot": {"description": "截取当前手机屏幕并返回 PNG 路径", "command": [binary, "device", "screenshot"], "params": [], "output_type": "path"},
                "ui": {"description": "读取当前手机无障碍 UI 树", "command": [binary, "device", "ui"], "params": []},
                "tap": {"description": "点击手机屏幕坐标", "command": [binary, "device", "tap", "{x}", "{y}"], "params": ["x", "y"]},
                "swipe": {"description": "在手机屏幕执行滑动", "command": [binary, "device", "swipe", "{x1}", "{y1}", "{x2}", "{y2}"], "params": ["x1", "y1", "x2", "y2"]},
                "press": {"description": "发送 Android 系统按键", "command": [binary, "device", "press", "{key}"], "params": ["key"]},
                "type": {"description": "向当前输入框输入文本", "command": [binary, "device", "type", "{text}"], "params": ["text"]},
                "start": {"description": "按包名启动手机应用", "command": [binary, "device", "start", "{package}"], "params": ["package"]},
                "task": {"description": "用移动 Agent 执行自然语言手机任务", "command": [binary, "run", "{task}", "--device", "localhost:5555", "--provider", "OpenAI", "--model", "{model}", "--api_base", "{gateway}", "--steps", "15", "--no-tracing", "--no-stream"], "params": ["task"], "timeout_s": 600},
            }
            spec["onboard_checks"] = [
                {"action": "devices", "stdout_contains": "local device"},
                {"action": "screenshot", "output_file": True},
            ]
    elif source_kind == "py-package":
        spec["runtime"] = {"python": info.get("requires_python") or True}
        spec["install"] = [["{python}", "-m", "venv", "{runtime_dir}/venv"],
                           ["{runtime_python}", "-m", "pip", "install", "-i", ALIYUN, "."]]
        spec["credentials"] = {"any_of": [], "signup_url": url,
                               "help": "能力库安装和导入不需要模型凭证"}
        spec["probe"] = {"method": "path", "target": "{runtime_python}"}
    elif source_kind == "http-server":
        spec["runtime"] = {"python": info.get("requires_python") or True}
        spec["description"] = (spec["description"] + "·HTTP server 型，需通过专用 worker 暴露会话协议").strip("·")
    elif source_kind == "windows":
        spec["runtime"] = {"platform": "windows"}
        spec["platform_target"] = "cloud-windows-vm"
    elif source_kind == "gui-electron":
        spec["runtime"] = {"node": True, "electron": True}
    # 生成器自己的完成契约：返回前必须通过和 catalog 落盘相同的校验，不能先写半条记录
    # 再等 deploy/runtime_manifest 在下游才发现。导入放函数内，保持本模块轻量且避免循环依赖。
    import agent_manifest
    validation = agent_manifest.validate(spec)
    if not validation.get("ok"):
        raise ValueError("autospec 生成结果不合法:" + ";".join(validation.get("errors") or []))
    return spec


def generate(url, key=""):
    match = re.match(r"https://github\.com/([^/]+)/([^/]+)(?:\.git)?/?$", url)
    if not match:
        raise ValueError(f"不是标准 GitHub URL:{url}")
    repo = match.group(2)
    key = key or repo.lower()[:24]
    # 自动识别只需短暂读取源码；旧实现 mkdtemp 后从不删除，每接一家就永久泄漏一份 clone。
    # TemporaryDirectory 保证成功、异常和超时三条路径都会回收，不把高频 onboarding 变成磁盘炸弹。
    with tempfile.TemporaryDirectory(prefix="autospec-") as tmp:
        repo_dir = pathlib.Path(tmp) / repo
        subprocess.run(["git", "clone", "--depth", "1", url, str(repo_dir)],
                       check=True, capture_output=True, timeout=300)
        rev = subprocess.run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
                             check=True, capture_output=True, text=True, timeout=30).stdout.strip()
        info = analyze(repo_dir)
        spec = build_spec(key, url, rev, info)
    return {"spec": spec, "kind": info["kind"], "hint": info["hint"],
            "revision": rev}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--key", default="")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    result = generate(args.url, args.key)
    out = pathlib.Path(args.out) if args.out else pathlib.Path(f"/tmp/autospec-{result['spec']['key']}.json")
    out.write_text(json.dumps(result["spec"], ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"kind": result["kind"], "hint": result["hint"],
                      "spec_path": str(out), "revision": result["revision"]}, ensure_ascii=False))
