#!/usr/bin/env python3
"""GenericAgent headless 单发 worker(军团 legion cli transport 用)。

使用 GenericAgent 原生 `agentmain.py --func` 单发入口；每轮配置和任务文件只存在于
TemporaryDirectory 作用域内，进程结束即回收，不向系统临时目录积累副本。
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import hashlib
import shutil


def _session_source(source, workspace):
    marker = source / ".meyo-source.json"
    if not marker.is_file():
        raise ValueError("GenericAgent requires verified source manifest for isolated runtime")
    manifest = json.loads(marker.read_text())
    identity = manifest["archive_sha256"]
    if not isinstance(identity, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", identity):
        raise ValueError("invalid source identity")
    session = workspace / ".sessions" / "genericagent" / identity
    session.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not session.resolve().is_relative_to(workspace.resolve()):
        raise ValueError("GenericAgent session directory escapes workspace")
    for name, expected in manifest["files"].items():
        relative = pathlib.Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("invalid source manifest path")
        original, target = source / relative, session / relative
        if not original.resolve().is_relative_to(source.resolve()) or not target.resolve().is_relative_to(session.resolve()):
            raise ValueError("GenericAgent source/runtime path escapes root")
        if original.is_symlink() or "sha256" not in expected:
            raise ValueError("GenericAgent source links require explicit isolation handling")
        if hashlib.sha256(original.read_bytes()).hexdigest() != expected["sha256"]:
            raise ValueError("GenericAgent source differs from verified manifest")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            raise ValueError("GenericAgent runtime file symlink")
        if not target.exists():
            shutil.copy2(original, target)
        elif relative.parts[0] != "memory" and hashlib.sha256(target.read_bytes()).hexdigest() != expected["sha256"]:
            raise ValueError("GenericAgent runtime source modified")
    temp = session / "temp"
    if not temp.exists() and not temp.is_symlink():
        temp.symlink_to(workspace, target_is_directory=True)
    if not temp.is_symlink() or temp.resolve() != workspace.resolve():
        raise ValueError("GenericAgent task directory mismatch")
    return session


def _extract_final(raw: str) -> str:
    text = re.sub(r"<summary>.*?</summary>", "", raw, flags=re.S)
    text = re.split(r"\[ROUND END\]", text)[0]
    text = re.sub(r"LLM Running \(Turn \d+\)\s*\.\.\.", "", text)
    text = re.sub(r"^\s*🛠️.*$", "", text, flags=re.M)
    return text.strip()


def _run_in_dir(run_dir: pathlib.Path, source: pathlib.Path, task: str, model: str,
                appid: str, gateway: str, timeout_s: int) -> tuple[int, str, str]:
    (run_dir / "mykey.py").write_text(
        "native_oai_config0 = {'name':'meyo','apikey':%r,'apibase':%r,'model':%r}\n"
        % (appid, gateway, model), encoding="utf-8")
    task_file = run_dir / "task.txt"
    task_file.write_text(task, encoding="utf-8")
    out_file = run_dir / "task.out.txt"
    env = dict(os.environ)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(key, None)
    env["NO_PROXY"] = "aigc.sankuai.com,127.0.0.1,localhost"
    env["PYTHONPATH"] = os.pathsep.join([str(run_dir), str(source), env.get("PYTHONPATH", "")])
    cmd = [sys.executable, str(source / "agentmain.py"), "--func", str(task_file),
           "--nobg", "--nolog", "--no-user-tools"]
    proc = subprocess.run(cmd, cwd=str(source), env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          timeout=timeout_s)
    raw = out_file.read_text(encoding="utf-8", errors="replace") if out_file.exists() else ""
    return proc.returncode, raw or proc.stdout or "", proc.stderr or ""


def main() -> int:
    source = pathlib.Path(os.environ.get("GA_SOURCE") or "")
    task = os.environ.get("GA_TASK") or ""
    model = os.environ.get("GA_MODEL") or "gpt-5.4"
    appid = os.environ.get("GA_APPID") or ""
    gateway = os.environ.get("GA_GATEWAY") or "https://aigc.sankuai.com/v1/openai/native"
    timeout_s = int(os.environ.get("GA_TIMEOUT") or "600")
    if not source.is_dir():
        print(json.dumps({"error": f"genericagent source missing:{source}"}))
        return 2

    try:
        workspace = pathlib.Path(os.environ.get("GA_WORKDIR") or os.getcwd()).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        source = _session_source(source.resolve(), workspace)
        with tempfile.TemporaryDirectory(prefix="ga_func_") as tmp:
            returncode, raw, stderr = _run_in_dir(
                pathlib.Path(tmp), source, task, model, appid, gateway, timeout_s)
    except subprocess.TimeoutExpired:
        print(json.dumps({"error": "genericagent --func timeout"}))
        return 1

    final = _extract_final(raw)
    if returncode != 0 or not final:
        print(json.dumps({"error": f"genericagent no output. rc={returncode} stderr={stderr[-500:]}"}))
        return 1
    no_tool = re.sub(r"^\s*🛠️.*$", "", final, flags=re.M)
    no_tool = re.sub(r"^\s*(file_read|file_write|bash|run_command)\(.*$", "", no_tool, flags=re.M).strip()
    if not no_tool:
        print(json.dumps({"error": f"genericagent only tool-call残片无正文(单轮未收敛):{final[:200]}"}))
        return 1
    print(json.dumps({"final": final, "session_id": "genericagent"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
