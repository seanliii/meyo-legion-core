#!/usr/bin/env python3
"""IronClaw(Reborn) 原生 `run -m` 单发 worker。

每轮 home 始终是作用域临时目录；只有调用方未提供持久 workdir 时才临时创建 workspace。
两个目录都由 TemporaryDirectory 保证正常、异常和超时路径自动回收。
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile
from contextlib import ExitStack, contextmanager
import threading
from urllib.parse import urlsplit


@contextmanager
def _transport_endpoint(gateway, appid, timeout_s):
    if urlsplit(gateway).hostname != "aigc.sankuai.com":
        yield gateway, appid
        return
    import sys
    import queue
    import signal
    service = str(pathlib.Path(__file__).resolve().parents[1])
    if service not in sys.path:
        sys.path.insert(0, service)
    import legion_proc_registry
    proc = subprocess.Popen(
        [sys.executable, str(pathlib.Path(__file__).with_name("ironclaw_transport.py"))],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, start_new_session=os.name != "nt")
    responses = queue.Queue()
    reader = threading.Thread(target=lambda: responses.put(proc.stdout.readline()), daemon=True)
    try:
        if not legion_proc_registry.register(proc.pid, owner=os.getpid(), meta={"kind": "ironclaw-transport"}):
            raise RuntimeError("cannot register task transport process")
        reader.start()
        proc.stdin.write(json.dumps({"gateway": gateway, "appid": appid, "timeout": timeout_s}) + "\n")
        proc.stdin.flush()
        try:
            response = responses.get(timeout=10)
        except queue.Empty:
            raise RuntimeError("IronClaw transport startup timeout") from None
        if not response:
            raise RuntimeError("IronClaw transport startup failed")
        yield tuple(json.loads(response))
    finally:
        if proc.stdin:
            proc.stdin.close()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
            proc.wait(timeout=2)
        legion_proc_registry.unregister(proc.pid)
        if reader.ident is not None:
            reader.join(timeout=1)
        proc.stdout.close()


def _temporary_home(workspace, stack):
    candidates = [workspace.parent, pathlib.Path(tempfile.gettempdir()), pathlib.Path('/tmp')]
    for candidate in dict.fromkeys(p.resolve() for p in candidates):
        if candidate.is_relative_to(workspace.resolve()):
            continue
        try:
            return pathlib.Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="ironclaw_home_", dir=candidate)))
        except OSError:
            continue
    raise OSError("no writable temporary home outside IronClaw workspace")


def _clean_env() -> dict:
    env = dict(os.environ)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(key, None)
    env["NO_PROXY"] = "aigc.sankuai.com,127.0.0.1,localhost"
    return env


def _run_scoped(ic_bin: str, task: str, model: str, appid: str, gateway: str,
                timeout_s: int, home: pathlib.Path, workspace: pathlib.Path) -> tuple[int, str, str]:
    (home / "providers.json").write_text(json.dumps([{
        "id": "meyo", "aliases": [], "protocol": "open_ai_completions",
        "api_key_env": "MEYO_IRONCLAW_KEY", "api_key_required": True,
        "default_base_url": gateway, "default_model": model,
        "model_env": "MEYO_IRONCLAW_MODEL", "description": "Meyo unified gateway",
    }], ensure_ascii=False), encoding="utf-8")
    env = _clean_env()
    env["IRONCLAW_REBORN_HOME"] = str(home)
    env["IRONCLAW_REBORN_WORKSPACE_ROOT"] = str(workspace.resolve())
    env["MEYO_IRONCLAW_KEY"] = appid
    env["MEYO_IRONCLAW_MODEL"] = model

    def step(args, limit=60):
        return subprocess.run([ic_bin, *args], cwd=str(workspace), env=env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, timeout=limit)

    for args, limit in [(["onboard"], 60), (["models", "set-provider", "meyo"], 40)]:
        setup = step(args, limit=limit)
        if setup.returncode != 0:
            return setup.returncode, "", setup.stderr or setup.stdout or "native setup failed"
    proc = step(["run", "-m", task, "--confirm-host-access"], limit=timeout_s)
    return proc.returncode, (proc.stdout or "").strip(), proc.stderr or ""


def main() -> int:
    ic_bin = os.environ.get("IC_BIN") or ""
    task = os.environ.get("IC_TASK") or ""
    model = os.environ.get("IC_MODEL") or "gpt-5.4"
    appid = os.environ.get("IC_APPID") or ""
    gateway = os.environ.get("IC_GATEWAY") or "https://aigc.sankuai.com/v1/openai/native"
    timeout_s = int(os.environ.get("IC_TIMEOUT") or "600")
    if not (ic_bin and pathlib.Path(ic_bin).exists()):
        print(json.dumps({"error": f"ironclaw bin missing:{ic_bin}"}))
        return 2

    configured_workspace = pathlib.Path(os.environ.get("IC_WORKDIR") or os.getcwd()).resolve()
    try:
        # Native skill storage must not be nested inside the tool workspace.
        # TMPDIR may itself be the task directory, so do not use it for this home.
        with ExitStack() as stack:
            workspace = configured_workspace if configured_workspace.is_dir() else pathlib.Path(
                stack.enter_context(tempfile.TemporaryDirectory(prefix="ironclaw_ws_")))
            home = _temporary_home(workspace, stack)
            endpoint, credential = stack.enter_context(_transport_endpoint(gateway, appid, timeout_s))
            returncode, final, stderr = _run_scoped(
                ic_bin, task, model, credential, endpoint, timeout_s, home, workspace)
    except subprocess.TimeoutExpired:
        print(json.dumps({"error": "ironclaw setup/run timeout"}))
        return 1
    except Exception as exc:
        print(json.dumps({"error": f"ironclaw setup/run failed:{type(exc).__name__}:{exc}"}))
        return 1

    if returncode != 0 or not final:
        print(json.dumps({"error": f"ironclaw no output. rc={returncode} stderr={stderr[-500:]}"}))
        return 1
    print(json.dumps({"final": final, "session_id": "ironclaw"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
