#!/usr/bin/env python3.12
"""跨进程全局并发闸(军团反思·2026-08-28·治 rc-9 OOM 根因)。

真实现场(本 session 亲测扒出·非猜):
  · 前 3 轮 cc 全 err:worker_no_json:rc-9。查 `ps` 发现机器上【同时并跑 5+ 个 claude CLI】
    (多个独立 legion 调用 + pilot 跨轮续跑 + 同 qhash 还有 hermes 在跑),16GB 机器 free 仅
    ~50MB、compressor 占 ~7.6GB、大量 swap → macOS jetsam(内存压力 OOM)把 worker 子进程
    SIGKILL(rc-9)。同时同 appid 被并发打爆 → errors.log 里 39 次 HTTP 429 每分钟超限。
  · rc-9 不是 legion_core 主动 idle-kill(那会返回 status=timeout·带 kill_reason),而是
    worker 被【外部信号】杀 → worker_no_json:rc-9(legion_core.py:911 那条兜底)。

为什么进程内 Semaphore 不够(legion_core._GATEWAY_SEM 是 threading.Semaphore):
  它只限【同一个进程内】的并发。但机器上有多个独立 legion 进程(server 一个、adhoc 派单一个、
  pilot 续跑又起一个……),每个自己的 sem=3,加起来远超机器能扛 → 各自"守规矩"但全局挤爆。

修法(context not control·资源层给现场·不改任何 harness 答题逻辑·不加语义门):
  用 fcntl 文件锁做一个【OS 级跨进程信号量】——所有 legion 进程共享同一个"槽位目录",启动重型
  worker(会拉起 claude/codex CLI 吃内存)前先抢一个槽;槽满就排队等,而不是硬起新进程挤爆内存
  触发 OOM。再叠加【内存压力感知】:内存已用超阈值(默认 88%)时,即便有名义槽位也让新 worker
  稍等,直到压力回落——让系统"看见真实内存现场"自己调速,不是父层写死规则拦模型。

  这不是限制 harness 能力(它照样跑、照样用全部工具),只是让并发的 worker 数匹配机器真实承载,
  把"被 OOM 随机杀掉交白卷"换成"排队几秒稳稳跑完"。丝滑可靠 > 一拥而上被杀。

可调(env·默认值按 16GB 机器实测):
  LEGION_GLOBAL_SLOTS      全局最大并发 worker(默认 4·跨所有 legion 进程共享)
  LEGION_MEM_PRESSURE_PCT  内存已用超此百分比则新 worker 等待(默认 88)
  LEGION_SLOT_WAIT_S       抢槽最长等待(默认 240s·等不到则放行·绝不永久卡死一单)
  LEGION_CONCURRENCY_OFF=1 整体关闭本闸(退回老行为·出问题可一键回退)
"""
from __future__ import annotations

import contextlib
import errno
import os
import time

import legion_paths as _paths

_SLOTS = max(1, int(os.environ.get("LEGION_GLOBAL_SLOTS", "4")))
_MEM_PCT = float(os.environ.get("LEGION_MEM_PRESSURE_PCT", "88"))
_WAIT_S = float(os.environ.get("LEGION_SLOT_WAIT_S", "240"))
_OFF = os.environ.get("LEGION_CONCURRENCY_OFF", "") == "1"

_SLOT_DIR = _paths.under("concurrency", "slots")   # ~/.meyo/legion/concurrency/slots/


def _mem_used_pct() -> float:
    """当前内存已用百分比。psutil 有就用;没有退回 vm_stat 解析;都失败返 0(不拦)。"""
    try:
        import psutil  # noqa
        return float(psutil.virtual_memory().percent)
    except Exception:
        pass
    try:
        import subprocess
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=3).stdout
        pages = {}
        page_size = 4096
        for ln in out.splitlines():
            if "page size of" in ln:
                try:
                    page_size = int(ln.split("page size of")[1].split("bytes")[0].strip())
                except Exception:
                    pass
            if ":" in ln:
                k, _, v = ln.partition(":")
                v = v.strip().rstrip(".")
                if v.isdigit():
                    pages[k.strip()] = int(v)
        free = (pages.get("Pages free", 0) + pages.get("Pages inactive", 0)
                + pages.get("Pages speculative", 0)) * page_size
        active = (pages.get("Pages active", 0) + pages.get("Pages wired down", 0)
                  + pages.get("Pages occupied by compressor", 0)) * page_size
        total = free + active
        return (active / total * 100.0) if total else 0.0
    except Exception:
        return 0.0


def _try_grab_slot(fd_holder: list) -> bool:
    """尝试在 _SLOT_DIR 下抢一个空槽(0.._SLOTS-1)。抢到=拿到该槽文件的排他 fcntl 锁。
    进程持有 fd(存进 fd_holder);进程退出/崩溃→OS 自动释放锁(不留死槽·这是 fcntl 的优点)。"""
    import fcntl
    os.makedirs(_SLOT_DIR, exist_ok=True)
    for i in range(_SLOTS):
        p = os.path.join(_SLOT_DIR, f"slot_{i}.lock")
        try:
            fd = os.open(p, os.O_CREAT | os.O_RDWR, 0o644)
        except Exception:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                os.write(fd, f"pid={os.getpid()} t={int(time.time())}\n".encode())
            except Exception:
                pass
            fd_holder.append(fd)
            return True
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EACCES):
                os.close(fd)            # 该槽被别的进程占着·试下一个
                continue
            os.close(fd)
        except Exception:
            with contextlib.suppress(Exception):
                os.close(fd)
    return False


@contextlib.contextmanager
def slot(tag: str = "", *, cancel_check=None, on_wait=None):
    """跨进程重型 worker 并发闸 + 内存压力感知。用法:
        with legion_concurrency.slot("cc"):
            ... 起 claude/codex worker 子进程 ...
    抢到槽且内存不吃紧才进入;槽满或内存吃紧则排队等(最多 _WAIT_S);等不到也放行(绝不永久
    卡死一单——宁可偶发挤一下,也不能让一单永远出不来)。OFF 时直接放行(退回老行为)。
    取消也 yield False；调用方必须在进入后检查取消，不能把 False 当作已取得槽。
    on_wait 只报告观测到的等待原因，首次及之后至多每 10 秒一次。
    """
    if _OFF:
        yield False
        return
    fd_holder: list = []
    started = time.time()
    deadline = started + _WAIT_S
    got = False
    last_notice = None

    def cancelled():
        return cancel_check is not None and bool(cancel_check())

    def notify_wait(reason, **facts):
        nonlocal last_notice
        now = time.time()
        if on_wait is None or (last_notice is not None and now - last_notice < 10.0):
            return
        last_notice = now
        with contextlib.suppress(Exception):
            on_wait({"tag": tag, "reason": reason,
                     "waited_s": round(max(0.0, now - started), 1), **facts})

    try:
        while time.time() < deadline:
            if cancelled():
                break
            # 1) 内存吃紧就先等(即便有名义槽位·避免抢到槽却撞 OOM)
            memory_used = _mem_used_pct()
            if memory_used >= _MEM_PCT:
                notify_wait("memory_pressure", memory_used_pct=memory_used,
                            memory_pressure_pct=_MEM_PCT)
                if cancelled():
                    break
                time.sleep(2.0)
                continue
            # 2) 抢一个全局槽
            if _try_grab_slot(fd_holder):
                got = True
                break
            # 未取得锁并不一定是满槽，也可能是锁文件不可用；不伪造占用数。
            notify_wait("slot_unavailable")
            if cancelled():
                break
            time.sleep(1.5)             # 槽满·等已在跑的 worker 让出
        if cancelled():
            got = False
        # 等不到(超时):放行(fd_holder 为空·不持锁)——绝不因为闸卡死一单交白卷
        yield got
    finally:
        import fcntl
        for fd in fd_holder:
            with contextlib.suppress(Exception):
                fcntl.flock(fd, fcntl.LOCK_UN)
            with contextlib.suppress(Exception):
                os.close(fd)


def status() -> dict:
    """当前闸状态(供自检/前端观测)。占用槽数=能抢到锁失败的个数。"""
    import fcntl
    if _OFF:
        return {"enabled": False, "slots": _SLOTS, "mem_pct": _mem_used_pct()}
    os.makedirs(_SLOT_DIR, exist_ok=True)
    busy = 0
    for i in range(_SLOTS):
        p = os.path.join(_SLOT_DIR, f"slot_{i}.lock")
        try:
            fd = os.open(p, os.O_CREAT | os.O_RDWR, 0o644)
        except Exception:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)   # 能抢到=该槽空闲
        except OSError:
            busy += 1
        finally:
            with contextlib.suppress(Exception):
                os.close(fd)
    return {"enabled": True, "slots": _SLOTS, "busy": busy, "free": _SLOTS - busy,
            "mem_pct": round(_mem_used_pct(), 1), "mem_pressure_pct": _MEM_PCT}


if __name__ == "__main__":
    import json
    print(json.dumps(status(), ensure_ascii=False, indent=2))
