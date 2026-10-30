#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""user_view:因人而异的个性化视图(用户2026-08-30·愿景第3块·OpenRouter没有的差异化).

产品愿景:harness届的OpenRouter·且【因人而异·每个用户不一样】。OpenRouter 的目录对所有人一样;
我们的差异化 = 同一个 harness 目录·对不同用户【突出他自己的】:已装的 skill、常用的 agent、
偏好的首脑、最近的历史。军团越用·"我的视图"越贴合我。

数据源都已存在(前几轮/各模块):
- skill_factory.list_active(uid): 用户已装的 skill
- captain_preferences.load(uid): 用户的首脑偏好
- sessions.list_sessions(uid): 用户历史会话(推常用 agent)
- install_registry: 用户已装项/升级状态

本模块只【聚合呈现】·不改任何用户数据。给前端"我的"入口用。
"""
from __future__ import annotations
import json
import os
import threading
import time


_VIEW_TTL = 15.0
_VIEW_CACHE: dict[str, tuple[float, dict]] = {}
_VIEW_LOCK = threading.RLock()


def _safe_call(fn, default):
    try:
        return fn()
    except Exception:
        return default


def invalidate(uid: str | None = None) -> None:
    with _VIEW_LOCK:
        if uid is None:
            _VIEW_CACHE.clear()
        else:
            _VIEW_CACHE.pop(str(uid), None)


def _recent_agents(uid: str, scan: int = 15, recent: list | None = None) -> dict:
    """从最近 scan 个会话里聚合【这个用户常用哪些 agent】(因人而异的核心信号)。
    读 session 详情里 turns[].result 出现的 harness key·计数。限量读避免重。"""
    try:
        import sessions
        recent = recent if recent is not None else sessions.list_sessions(limit=scan, user_id=uid)
    except Exception:
        return {}
    counts = {}
    for s in recent:
        try:
            d = sessions.load_session(s["sid"], user_id=uid) or {}
        except Exception:
            continue
        for t in d.get("turns", []):
            # session turn 里 results/captain 在 turn 顶层(实测结构)·兼容嵌 result 的旧格式
            r = t.get("result") if isinstance(t.get("result"), dict) else t
            keys = []
            if isinstance(r.get("results"), list):
                keys += [x.get("key") or x.get("harness") for x in r["results"] if isinstance(x, dict)]
            if isinstance(r.get("winner"), dict):
                keys.append(r["winner"].get("key") or r["winner"].get("harness"))
            if r.get("captain"):
                keys.append(r.get("captain"))
            for k in keys:
                if k:
                    counts[k] = counts.get(k, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


def my_view(uid: str = "default", force: bool = False) -> dict:
    """聚合某用户的个性化视图；短缓存避免同一面板重复读取15份会话 JSON。"""
    uid = str(uid or "default")
    if not force:
        with _VIEW_LOCK:
            cached = _VIEW_CACHE.get(uid)
            if cached and time.time() - cached[0] < _VIEW_TTL:
                return {**cached[1], "from_cache": True}
    # 已装 skill
    skills = _safe_call(lambda: __import__("skill_factory").list_active(uid), {"skills": []})
    my_skills = [s["name"] for s in skills.get("skills", [])]
    # 首脑偏好
    cap = _safe_call(lambda: __import__("captain_preferences").load(uid), {})
    # 最近会话
    recent = _safe_call(lambda: __import__("sessions").list_sessions(15, uid), [])
    # 常用 agent(因人而异核心)复用刚取的 recent，避免再次扫描会话目录。
    fav_agents = _recent_agents(uid, recent=recent)
    result = {
        "ok": True, "uid": uid,
        "positioning": "因人而异:同一harness目录·对你突出【你已装的skill/常用agent/偏好首脑/历史】",
        "my_skills": {"count": len(my_skills), "items": my_skills[:30]},
        "captain_preference": {"mode": cap.get("mode", "auto"),
                               "preferred_captain": cap.get("preferred_captain", ""),
                               "note": "auto=编排时按适任度竞选;也可固定偏好某家(弱首脑如auto不建议)"},
        "favorite_agents": {"count": len(fav_agents),
                            "top": list(fav_agents.items())[:8],
                            "note": "从你最近会话聚合·你常派哪些家"},
        "recent_sessions": {"count": len(recent),
                            "items": [{"title": s.get("title"), "url": s.get("url"),
                                       "turns": s.get("turns")} for s in recent[:8]]},
        "hint": "军团越用·这里越贴合你(已装skill/常用agent会积累)。想装更多能力→harness目录按需装。",
        "from_cache": False,
    }
    with _VIEW_LOCK:
        _VIEW_CACHE[uid] = (time.time(), result)
    return result


if __name__ == "__main__":
    os.environ.setdefault("MEYO_LEGION_DIR", os.path.expanduser("~/.meyo/legion"))
    import sys
    uid = sys.argv[1] if len(sys.argv) > 1 else "default"
    print(json.dumps(my_view(uid), ensure_ascii=False, indent=2))
