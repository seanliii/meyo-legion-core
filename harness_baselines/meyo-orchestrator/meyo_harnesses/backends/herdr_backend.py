# -*- coding: utf-8 -*-
"""Herdr 终端常驻 runtime 后端 adapter(经 CLI + socket API)。

Herdr = Rust 单二进制。若本机已 build/装 herdr(PATH 或 target/release),真调
`herdr api snapshot` 验证 runtime 连通;否则诚实返回 unavailable + 构建提示。
"""
from __future__ import annotations
import json, shutil, subprocess, time
from pathlib import Path

_HERDR_SRC = Path(__file__).resolve().parents[3] / "ecosystem" / "herdr"
_REL_BIN = _HERDR_SRC / "target" / "release" / "herdr"


def _bin() -> str | None:
    return shutil.which("herdr") or (str(_REL_BIN) if _REL_BIN.is_file() else None)


def _cli(args: list[str], timeout: int = 20) -> dict:
    """跑一条 herdr socket API 子命令,解析其 JSON 响应。"""
    b = _bin()
    if not b:
        return {"error": "herdr_binary_not_found"}
    try:
        r = subprocess.run([b, *args], capture_output=True, text=True, timeout=timeout)
        # Herdr 成功 JSON 写 stdout,错误 JSON 写 stderr(rc=1)——两路都要解析。
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        for blob in (out, err):
            if not blob:
                continue
            try:
                return json.loads(blob)
            except Exception:
                continue
        return {"raw": out or err, "rc": r.returncode}
    except Exception as e:
        return {"error": f"{type(e).__name__}:{str(e)[:120]}"}


def server_running() -> bool:
    r = _cli(["status", "server"], timeout=8)
    if not isinstance(r, dict):
        return False
    # 成功是纯文本 "status: running\n...";失败 JSON 的 message 也含
    # "no herdr server is running"——绝不能用宽松 substring(会假阳性)。
    raw = str(r.get("raw", "")).strip().lower()
    return raw.startswith("status: running")


def ensure_server() -> bool:
    """确保 headless server 在跑;不在则后台起。返回是否就绪。"""
    if server_running():
        return True
    b = _bin()
    if not b:
        return False
    try:
        subprocess.Popen([b, "server"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception:
        return False
    for _ in range(12):
        time.sleep(0.5)
        if server_running():
            return True
    return False


def api_snapshot() -> dict:
    return _cli(["api", "snapshot"])


def probe() -> dict:
    b = _bin()
    if not b:
        return {"backend": "herdr", "available": False,
                "reason": "herdr_binary_not_found",
                # 真实阻塞记录:vendored libghostty-vt 精确锁 Zig 0.15.2,
                # 而 brew 只提供 0.16.0(版本 pin 冲突)。需装 0.15.2 专用 toolchain 后重试。
                "hint": (f"cd {_HERDR_SRC} && cargo +1.95.0 build --release "
                         f"(需先装 Zig 0.15.2·brew 的 0.16.0 会被 build.zig 拒)"),
                "source_present": _HERDR_SRC.is_dir()}
    return {"backend": "herdr", "available": True, "bin": b}


def create_workspace(cwd: str = "/tmp", label: str = "meyo") -> dict:
    """真调 socket API 创建 workspace + root terminal pane。"""
    return _cli(["workspace", "create", "--cwd", cwd, "--label", label, "--no-focus"])


def pane_send_text(pane_id: str, text: str) -> dict:
    """向 pane 发送字面文本。"""
    return _cli(["pane", "send-text", pane_id, text])


def pane_wait_output(pane_id: str, match: str, timeout_ms: int = 10000) -> dict:
    """等 pane 出现指定文本(真实 socket 事件/终端输出)。"""
    return _cli(["pane", "wait-output", "--match", match,
                 "--timeout", str(timeout_ms), pane_id], timeout=max(2, timeout_ms // 1000 + 3))


def agent_prompt(target: str, text: str, wait: bool = True,
                 until: str = "done", timeout_ms: int = 120000) -> dict:
    """接通 agent.prompt:向 Herdr 检测到的 agent 发 prompt,可等待状态。"""
    args = ["agent", "prompt", target, text]
    if wait:
        args += ["--wait", "--until", until, "--timeout", str(timeout_ms)]
    return _cli(args, timeout=max(5, timeout_ms // 1000 + 5))


def agent_wait(target: str, until: str = "done", timeout_ms: int = 120000) -> dict:
    """接通 agent.wait:事件驱动等待 agent 状态。"""
    return _cli(["agent", "wait", target, "--until", until,
                 "--timeout", str(timeout_ms)], timeout=max(5, timeout_ms // 1000 + 5))


def run(qhash: str, task: str, gold: str = "") -> dict:
    """真启动/连接 Herdr headless runtime,返回 live `api snapshot`。"""
    t0 = time.time(); p = probe()
    if not p["available"]:
        return {"harness": "herdr", "status": "unavailable", "final": "",
                **p, "elapsed": round(time.time() - t0, 1)}
    if not ensure_server():
        return {"harness": "herdr", "status": "runtime_not_running", "final": "",
                "bin": p["bin"], "elapsed": round(time.time() - t0, 1)}
    snap = api_snapshot()
    ok = "result" in snap and "snapshot" in snap.get("result", {})
    return {"harness": "herdr", "status": "ok" if ok else "runtime_error",
            "final": json.dumps(snap, ensure_ascii=False)[:800], "snapshot": snap,
            "bin": p["bin"], "elapsed": round(time.time() - t0, 1)}
