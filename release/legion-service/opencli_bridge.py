#!/usr/bin/env python3
"""
opencli_bridge · 把 OpenCLI 接进军团工具箱(用户 goal 第一步点名要装的)

OpenCLI(@jackwener/opencli)= "Make any website your CLI, AI-powered":
  - 复用用户【已登录浏览器 session】操作 100+ 网站,无需 API key
  - 所有命令支持 --format json,输出干净结构化数据(正好喂给军团 harness)
  - 能包装本地 CLI / 装第三方插件 → 这就是"harness/工具自动加入工具箱"的通道

军团用法:任意一家需要"从某网站取结构化数据"时,经此桥调 opencli <cmd> -f json,
拿到 JSON 当证据/上下文。给 context 不加 control:只把 opencli 产出交给模型,自主用。

真验证(2026-08-27):opencli 1.8.7 已装,`opencli hackernews top -f json` 真出 JSON·exit 0。
"""
import json
import os
import shutil
import subprocess
import time


def available() -> bool:
    return bool(shutil.which("opencli"))


# opencli doctor 探浏览器桥连接·subprocess ~8s。短 TTL(30s)缓存:用户连点自检/多标签打开
# 不重复付 8s;桥状态秒级不变·30s 内复用足够(和其他慢探测同"限时+缓存"理念·此处限时已由
# subprocess timeout 保证·补个短缓存去掉重复卡顿)。force=True 可强制重探。
_DOCTOR_CACHE: dict = {"data": None, "ts": 0.0}
_DOCTOR_TTL = 30.0


def doctor(timeout_s: int = 30, force: bool = False) -> dict:
    """OpenCLI 就绪自检(浏览器桥连接状态)。"""
    if not available():
        return {"ok": False, "installed": False,
                "install_hint": "npm install -g @jackwener/opencli (需 Node>=20)"}
    if not force and _DOCTOR_CACHE["data"] and time.time() - _DOCTOR_CACHE["ts"] < _DOCTOR_TTL:
        return {**_DOCTOR_CACHE["data"], "from_cache": True}
    try:
        r = subprocess.run(["opencli", "doctor"], capture_output=True, text=True, timeout=timeout_s)
        detail = (r.stdout or r.stderr or "")[-800:]
        # opencli 1.8.7 即使 Extension 未连接、Connectivity FAIL 也返回 rc=0；只看退出码会
        # 向 UI 谎报 ok=true，随后真调用才 ECONNREFUSED。doctor 正文才是浏览器桥就绪事实。
        low = detail.lower()
        bridge_failed = ("[fail]" in low or "[missing] extension" in low
                         or "extension: not connected" in low
                         or "browser bridge extension not connected" in low)
        data = {"ok": r.returncode == 0 and not bridge_failed, "installed": True,
                "bridge_ready": r.returncode == 0 and not bridge_failed,
                "detail": detail}
        if bridge_failed:
            data["error"] = "OpenCLI 已安装，但浏览器桥扩展未连接；请连接扩展或重启 daemon 后再试"
    except Exception as e:
        data = {"ok": False, "installed": True, "error": f"{type(e).__name__}: {e}"}
    _DOCTOR_CACHE["data"] = data
    _DOCTOR_CACHE["ts"] = time.time()
    return {**data, "from_cache": False}


def list_commands(timeout_s: int = 30) -> dict:
    """列出 opencli 可用命令(军团工具箱展示用)。"""
    if not available():
        return {"ok": False, "error": "opencli 未安装"}
    try:
        r = subprocess.run(["opencli", "list"], capture_output=True, text=True, timeout=timeout_s)
        return {"ok": r.returncode == 0, "raw": (r.stdout or "")[:8000]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def run(args: list, timeout_s: int = 90, want_json: bool = True) -> dict:
    """军团调 opencli 的统一入口。args 如 ['hackernews','top','--limit','5']。

    want_json=True 时自动追加 -f json 并解析。返回 {ok, data|text, exit_code, error}。
    OpenCLI 遵循 sysexits:66=空结果 69=桥失效 75=超时 77=需认证 78=配置错——如实回传给模型判断。
    """
    if not available():
        return {"ok": False, "error": "opencli 未安装(npm install -g @jackwener/opencli)"}
    if not isinstance(args, list) or not args:
        return {"ok": False, "error": "args 需为非空列表,如 ['hackernews','top']"}
    cmd = ["opencli"] + [str(a) for a in args]
    # 管理类子命令(doctor/list/help/version)不接受 -f json,只有数据查询才加。
    _MGMT = {"doctor", "list", "help", "--help", "-h", "version", "--version", "login", "logout"}
    is_mgmt = str(args[0]).lower() in _MGMT
    if want_json and not is_mgmt and "-f" not in args and "--format" not in args:
        cmd += ["-f", "json"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"opencli 超时({timeout_s}s)", "exit_code": 75}
    out = (r.stdout or "").strip()
    ec = r.returncode
    # sysexits 语义:如实告诉模型是空结果/需登录/桥失效,而不是笼统失败
    ec_note = {66: "空结果", 69: "浏览器桥失效(试 opencli doctor)",
               75: "超时", 77: "该站需先在浏览器登录", 78: "配置错误"}.get(ec)
    if ec == 0 and want_json:
        try:
            return {"ok": True, "data": json.loads(out), "exit_code": 0}
        except Exception:
            return {"ok": True, "text": out[:8000], "exit_code": 0,
                    "note": "输出非 JSON,返回原文"}
    if ec == 0:
        return {"ok": True, "text": out[:8000], "exit_code": 0}
    return {"ok": False, "exit_code": ec, "error": ec_note or (r.stderr or "opencli 失败")[-300:],
            "text": out[:2000]}


if __name__ == "__main__":
    import sys
    print(json.dumps(run(sys.argv[1:]) if len(sys.argv) > 1 else doctor(),
                     ensure_ascii=False, indent=1))
