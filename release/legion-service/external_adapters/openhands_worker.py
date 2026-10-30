#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""openhands_worker.py - OpenHands agent-server HTTP 适配 worker。
流程:确保 8787 server 活着(不在则拉起)->POST conversation(initial_message+agent llm 网关)
     ->轮询 agent_final_response 到出结果。模型经 model_compat 已把 gpt-5.6-sol 换 deepseek
     (litellm 对 gpt-5* 特判走 Responses API·网关 404)。"""
import json, os, subprocess, sys, time, urllib.request, signal
from pathlib import Path

PORT = int(os.environ.get("OH_PORT", "8787"))
BASE = "http://127.0.0.1:%d" % PORT
RUNTIME = os.environ.get("OH_RUNTIME_DIR", "")
MODEL = os.environ.get("OH_MODEL", "openai/deepseek-v4-pro")
GATEWAY = os.environ.get("OH_GATEWAY", "https://aigc.sankuai.com/v1/openai/native")
APPID = os.environ.get("OH_APPID", "")
TASK = os.environ.get("OH_TASK", "")
WORKDIR = os.environ.get("MEYO_WORKDIR", "")
TIMEOUT_S = int(os.environ.get("OH_TIMEOUT", "600"))


def _alive() -> bool:
    try:
        with urllib.request.urlopen(BASE + "/health", timeout=5) as r:
            return r.status < 500
    except Exception:
        return False


def _spawn_server():
    env = {**os.environ, "OPENHANDS_SUPPRESS_BANNER": "1"}
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    state = Path(os.environ.get("MEYO_LEGION_DIR") or str(Path.home() / ".meyo/legion")) / "external_agents/openhands"
    state.mkdir(parents=True, exist_ok=True)
    env["OH_CONVERSATIONS_PATH"] = str(state / "conversations")
    fd = os.open(state / "server.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "ab") as log:
        proc = subprocess.Popen([RUNTIME + "/venv/bin/agent-server", "--host", "127.0.0.1", "--port", str(PORT)],
                                cwd=state, env=env, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import legion_proc_registry
    legion_proc_registry.register(proc.pid, pgid=proc.pid, owner=os.getpid(), meta={"kind": "openhands-server"})
    deadline = time.monotonic() + float(os.environ.get("OH_START_TIMEOUT", "60"))
    while time.monotonic() < deadline and proc.poll() is None:
        if _alive():
            return True
        time.sleep(0.25)
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
    legion_proc_registry.unregister(proc.pid)
    return False


def main() -> int:
    if not TASK or not RUNTIME or not WORKDIR:
        print(json.dumps({"error": "OH_TASK/OH_RUNTIME_DIR/MEYO_WORKDIR missing"}))
        return 2
    os.makedirs(WORKDIR, exist_ok=True)
    if not _alive() and not _spawn_server():
        print(json.dumps({"error": "agent-server %s 拉起失败，查看 external_agents/openhands/server.log" % PORT}))
        return 3
    body = {"workspace": {"working_dir": WORKDIR, "kind": "LocalWorkspace"},
            "agent": {"kind": "Agent", "name": "legion-agent", "description": "legion worker",
                      "tools": [{"name": "terminal"}, {"name": "file_editor"}, {"name": "task_tracker"}],
                      "llm": {"model": MODEL, "api_key": APPID, "base_url": GATEWAY}},
            "initial_message": {"role": "user", "content": [{"type": "text", "text": TASK}], "run": True},
            "max_iterations": 30}
    req = urllib.request.Request(BASE + "/api/conversations", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=90).read().decode())
    except Exception as exc:
        print(json.dumps({"error": "create conversation failed:%s" % exc}))
        return 4
    cid = d.get("id")
    t0 = time.time()
    while time.time() - t0 < TIMEOUT_S:
        time.sleep(1)
        try:
            fr = json.loads(urllib.request.urlopen(BASE + "/api/conversations/%s/agent_final_response" % cid, timeout=15).read().decode())
        except Exception:
            continue
        resp = str(fr.get("response") or "")
        if resp.strip():
            print(json.dumps({"final": resp, "session_id": cid}, ensure_ascii=False))
            return 0
    print(json.dumps({"error": "timeout %ds waiting final_response" % TIMEOUT_S}))
    return 5


if __name__ == "__main__":
    raise SystemExit(main())
