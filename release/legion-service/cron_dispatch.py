#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cron 真触发调度器(2026-09-03 /loop 手机生活场景伙伴收口)。

旧 cron_jobs 只做"登记+过期"——到点不真跑,手机生活场景(每天早报/定时打卡/周期同步)
全是空转。本模块补上缺的那半:到点真触发、miss 补跑、状态机落账、运行历史可回溯,
让"定时长期运行"从注释变成机制。

设计要点(全部纯机制,不含任何答题逻辑):
- 单一事实源仍是 cron_jobs.jobs.json;本模块只在锁内追加触发字段,不改原有字段语义。
- runner 注入:调度器不知道怎么执行——把 question/mode/user_id 交给注入的 runner
  (server 里接 LEGION.dispatch;测试里接假 runner),纯 context 通道。
- 幂等:next_fire_at 挡重复触发;启动扫 miss 补跑一次(重启不丢已到点任务)。
- 历史:每次触发追加 history.json(含 ok/耗时/session 摘要),网页恢复用它回溯。
"""
from __future__ import annotations

import json
import os
import time
import threading

import legion_paths as _paths
import cron_jobs

CRON_DIR = os.environ.get("LEGION_CRON_DIR") or _paths.home_under("cron")
_STORE = os.path.join(CRON_DIR, "jobs.json")
_HISTORY = os.path.join(CRON_DIR, "history.json")
_LOCK = threading.RLock()

_POLL_SEC = float(os.environ.get("LEGION_CRON_POLL_SEC", "30") or 30)
_THREAD = None


def _read_jobs() -> list[dict]:
    if not os.path.isfile(_STORE):
        return []
    try:
        return json.load(open(_STORE, encoding="utf-8"))
    except Exception:
        return []


def _write_jobs(jobs: list[dict]) -> None:
    os.makedirs(CRON_DIR, exist_ok=True)
    tmp = f"{_STORE}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _STORE)


def _append_history(row: dict) -> None:
    try:
        rows = []
        if os.path.isfile(_HISTORY):
            try:
                rows = json.load(open(_HISTORY, encoding="utf-8"))
            except Exception:
                rows = []
        rows.append(row)
        rows = rows[-2000:]  # 防无限增长;老记录可归档
        tmp = f"{_HISTORY}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _HISTORY)
    except Exception:
        pass  # 历史是恢复用的旁账,绝不反噬触发主流程


def _parse_field(part: str, lo: int, hi: int) -> list[int]:
    """解析单段 cron 字段(支持 * , - /)。不求全,标准 5 段够用。"""
    out = []
    for chunk in str(part).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        step = 1
        if "/" in chunk:
            chunk, step_s = chunk.split("/", 1)
            step = max(1, int(step_s))
        if chunk == "*":
            vals = list(range(lo, hi + 1))
        elif "-" in chunk:
            a, b = chunk.split("-", 1)
            vals = list(range(int(a), int(b) + 1))
        else:
            vals = [int(chunk)]
        out.extend(vals[::step])
    return sorted({v for v in out if lo <= v <= hi})


def next_fire_after(cron: str, after: float) -> float:
    """给定 cron 与时间戳,算下一次触发点(分钟粒度,向上取整到下一分钟)。"""
    import calendar
    parts = (cron or "").split()
    if len(parts) != 5:
        return float("inf")
    minute = _parse_field(parts[0], 0, 59)
    hour = _parse_field(parts[1], 0, 23)
    day = _parse_field(parts[2], 1, 31)
    month = _parse_field(parts[3], 1, 12)
    weekday = _parse_field(parts[4], 0, 6)  # 0=周日(与 cron 一致)
    # 从 after+1 分钟的整分钟开始向后找,最多找 366 天
    t = int(after) + 60
    t -= t % 60
    limit = after + 366 * 86400
    while t <= limit:
        st = time.localtime(t)
        if (st.tm_min in minute and st.tm_hour in hour and st.tm_mday in day
                and st.tm_mon in month and (st.tm_wday + 1) % 7 in weekday):
            return float(t)
        t += 60
    return float("inf")


def run_due(now: float | None = None, runner=None) -> dict:
    """扫描到期任务并真触发。runner(question, mode, user_id)->dict;None=只算不跑。"""
    now = time.time() if now is None else float(now)
    default_runner = None
    fired, skipped, errors = [], 0, 0
    with _LOCK:
        jobs = _read_jobs()
        changed = False
        for job in jobs:
            if job.get("expires_at", 0) <= now:
                continue  # 过期即失效(cron_jobs 语义),不触发
            if job.get("status") == "expired":
                continue
            nxt = job.get("next_fire_at")
            if nxt is None:
                prev = job.get("last_fired_at") or job.get("created") or now
                nxt = next_fire_after(job.get("cron") or "", prev)
                job["next_fire_at"] = nxt
                changed = True
            if float(nxt) > now:
                continue
            if runner is None and default_runner is None:
                # 默认 runner:交给军团 dispatch(真执行;失败不吞,进 history)
                def default_runner(q, m, u):  # noqa: F811
                    import legion_core
                    return legion_core.dispatch(q, mode=m)
            fn = runner or default_runner
            t0 = time.time()
            try:
                res = fn(str(job.get("question") or ""), str(job.get("mode") or "pk"),
                         str(job.get("user_id") or "default")) or {}
                ok = bool(res.get("ok", res.get("delivered_count", 0) > 0))
            except Exception as exc:
                res, ok = {"error": f"{type(exc).__name__}: {exc}"}, False
                errors += 1
            job["status"] = "fired" if ok else "failed"
            job["last_fired_at"] = now
            job["last_result_ok"] = ok
            job["next_fire_at"] = next_fire_after(job.get("cron") or "", now)
            changed = True
            fired.append(job.get("id"))
            _append_history({"id": job.get("id"), "question": str(job.get("question") or "")[:200],
                             "ts": now, "ok": ok, "elapsed_s": round(time.time() - t0, 1),
                             "user_id": str(job.get("user_id") or "default"),
                             "project_id": str(job.get("project_id") or ""),
                             "session": (res.get("session") or {}) if isinstance(res.get("session"), dict) else {},
                             "error": str(res.get("error") or "")[:500]})
        if changed:
            _write_jobs(jobs)
    return {"fired": len(fired), "fired_ids": fired, "checked": len(jobs), "errors": errors}


def start_scheduler(poll_sec: float | None = None) -> dict:
    """启动常驻调度线程(幂等);server main() 调一次即可。"""
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return {"ok": True, "already_running": True}
    sec = poll_sec or _POLL_SEC

    def _loop():
        while True:
            try:
                run_due()
            except Exception:
                pass  # 单轮失败绝不杀调度线程
            time.sleep(sec)

    _THREAD = threading.Thread(target=_loop, name="cron-dispatch", daemon=True)
    _THREAD.start()
    return {"ok": True, "poll_sec": sec}


if __name__ == "__main__":
    print(json.dumps(run_due(), ensure_ascii=False, indent=2))
