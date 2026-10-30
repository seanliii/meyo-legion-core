#!/usr/bin/env python3
"""net_watchdog · 2026-08-15 · A 断网保留+恢复续跑

用户:断网时保留对话和执行任务·网络几小时后恢复还能继续跑;或自动在1小时内拿到网络就跑。

设计(最小·复用已有 ckpt/resume 地基):
  - net_ok():轻量探测网络通不通(socket 连可靠主机·不发 HTTP·快)。
  - wait_for_net(max_wait):阻塞等网络恢复(轮询·退避)·超时返回 False。恢复返回 True。
  - 断网时:任务【不 force-fail】·persisted status 保持 running(_resume_interrupted_jobs 会续)·
    或显式标 paused_offline;网络回来 → 触发 resume(接上 ckpt·不从头)。
不引入新依赖(只用 socket)。全 fail-safe。
"""
import os
import time
import socket

# 探测目标(多个·任一通即算在线·避免单点误判)。可环境变量覆盖。
_PROBE_HOSTS = os.environ.get(
    "MEYO_NET_PROBE_HOSTS",
    "1.1.1.1:443,8.8.8.8:443,223.5.5.5:443").split(",")
_PROBE_TIMEOUT = float(os.environ.get("MEYO_NET_PROBE_TIMEOUT", "3"))


def net_ok() -> bool:
    """网络通不通:任一探测主机 TCP 连通即算在线。快(≤3s)·fail-safe(异常当离线)。"""
    for hp in _PROBE_HOSTS:
        hp = hp.strip()
        if not hp:
            continue
        try:
            host, _, port = hp.partition(":")
            port = int(port or "443")
            with socket.create_connection((host, port), timeout=_PROBE_TIMEOUT):
                return True
        except Exception:
            continue
    return False


def wait_for_net(max_wait: float = 3600, poll: float = 30) -> bool:
    """阻塞等网络恢复·最多 max_wait 秒(默认1h·对齐用户"1小时内拿到网络就跑")。
    每 poll 秒探一次(退避到 120s 上限)。恢复返回 True·超时返回 False。"""
    t0 = time.time()
    cur = poll
    while (time.time() - t0) < max_wait:
        if net_ok():
            return True
        time.sleep(min(cur, 120))
        cur = min(cur * 1.5, 120)   # 退避·别狂探
    return net_ok()  # 最后再探一次


def status() -> dict:
    """网络状态快照(供 UI/agent 查)。"""
    ok = net_ok()
    return {"online": ok, "probe_hosts": _PROBE_HOSTS,
            "note": "在线" if ok else "离线(任务会保留·联网工具暂停·网络回来自动续)"}


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "wait":
        print("等网络恢复(最多60s测试)...")
        print("恢复:" if wait_for_net(max_wait=60, poll=5) else "超时未恢复")
    else:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
