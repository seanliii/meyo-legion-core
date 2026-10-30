#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cron 定时任务登记(带过期时间) —— 用户需求

用户要"支持 cron 但给一个过期时间"。本机原型阶段:先做【接口 + 登记 + 过期】,
即任务能创建/列出/删除、到期自动失效,但不起常驻调度器真触发(部署新仓库时再接
真正的调度进程去按 cron 表达式跑军团)。

存储:所有任务一个 JSON,放 CRON_DIR(默认 ~/.legion/cron)。
每个任务:{id, question, mode, cron, created, expires_at, status, note}
"""
from __future__ import annotations
import os
import json
import time
import hashlib
import threading

import legion_paths as _paths
CRON_DIR = os.environ.get("LEGION_CRON_DIR") or _paths.home_under("cron")
_STORE = os.path.join(CRON_DIR, "jobs.json")
# 单一 jobs.json 的读改写要加锁:create/delete/purge 是 load→改→save,ThreadingHTTPServer
# 下并发创建会丢更新(实测 8 并发 create 只剩 5)。进程内锁即可。
_LOCK = threading.RLock()


def _ensure():
    os.makedirs(CRON_DIR, exist_ok=True)


def _load() -> list[dict]:
    if not os.path.isfile(_STORE):
        return []
    try:
        return json.load(open(_STORE, encoding="utf-8"))
    except Exception:
        return []


def _save(jobs: list[dict]):
    _ensure()
    with open(_STORE, "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=2)


def _job_id(question: str, ts: float) -> str:
    return "job_" + hashlib.sha1(f"{question}|{ts}".encode()).hexdigest()[:10]


def _valid_cron(expr: str) -> bool:
    """粗校验 5 段 cron 表达式(分 时 日 月 周)。不求全,挡明显错。"""
    parts = (expr or "").split()
    if len(parts) != 5:
        return False
    import re
    ok = re.compile(r"^[\d\*\-,/]+$")
    return all(ok.match(p) for p in parts)


def create_job(question: str, cron: str, mode: str = "pk",
               ttl_seconds: int | None = None, expires_at: float | None = None,
               note: str = "", user_id: str = "default",
               project_id: str = "", task_id: str = "") -> dict:
    """登记一个定时任务(带过期)。ttl_seconds 或 expires_at 二选一给过期时间。

    过期是硬要求:不给过期时间则默认 7 天后过期(避免僵尸任务永远登记)。
    """
    if not question.strip():
        return {"error": "missing question"}
    if not _valid_cron(cron):
        return {"error": f"非法 cron 表达式(需 5 段:分 时 日 月 周): {cron!r}"}
    now = time.time()
    if expires_at is None:
        expires_at = now + (ttl_seconds if ttl_seconds else 7 * 24 * 3600)
    if expires_at <= now:
        return {"error": "过期时间必须在未来"}
    job = {
        "id": _job_id(question, now),
        "question": question.strip(),
        "mode": mode if mode in ("pk", "relay") else "pk",
        "cron": cron,
        "created": now,
        "expires_at": expires_at,
        "status": "registered",  # registered → (真调度后)running/done/expired
        "note": note or "本机原型:已登记·真触发待部署调度进程",
        "user_id": user_id or "default",
        "project_id": str(project_id or ""),  # 挂项目(2026-09-07 打通做项目/盯变化)
        "task_id": str(task_id or ""),
    }
    with _LOCK:
        jobs = _load()
        jobs.append(job)
        _save(jobs)
    return job


def list_jobs(include_expired: bool = False, user_id: str | None = None) -> list[dict]:
    """列出任务。默认自动过滤掉已过期的(过期即失效·用户要的'过期时间')。

    user_id 非空时只返回该用户登记的任务(2026-09-07·跨用户泄漏根修:盯变化列表
    曾把别人的 cron 也展示给所有人,点击跳项目还会 project not found)。None=不过滤
    (调度器/管理面语义)。
    """
    now = time.time()
    with _LOCK:
        jobs = _load()
        changed = False
        out = []
        for j in jobs:
            if user_id and str(j.get("user_id") or "default") != str(user_id):
                continue
            expired = j.get("expires_at", 0) <= now
            if expired and j.get("status") != "expired":
                j["status"] = "expired"
                changed = True
            if expired and not include_expired:
                continue
            j["_expires_in_s"] = max(0, int(j.get("expires_at", 0) - now))
            out.append(j)
        if changed:
            _save(jobs)
    return out


def delete_job(job_id: str) -> dict:
    with _LOCK:
        jobs = _load()
        n = len(jobs)
        jobs = [j for j in jobs if j.get("id") != job_id]
        _save(jobs)
    return {"deleted": n - len(jobs), "id": job_id}


def purge_expired() -> dict:
    """物理清除已过期任务(可被调度进程/运维定期调用)。"""
    now = time.time()
    with _LOCK:
        jobs = _load()
        kept = [j for j in jobs if j.get("expires_at", 0) > now]
        removed = len(jobs) - len(kept)
        _save(kept)
    return {"purged": removed, "remaining": len(kept)}


if __name__ == "__main__":
    j = create_job("每天早上给我军团跑一遍行业资讯", "0 9 * * *", mode="relay", ttl_seconds=3600)
    print("created:", j.get("id"), j.get("cron"), "expires_in", int(j["expires_at"] - time.time()), "s")
    print("list:", [(x["id"], x["cron"], x["_expires_in_s"]) for x in list_jobs()])
