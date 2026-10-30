#!/usr/bin/env python3
"""search_loop_guard · 搜索死循环监督器 (v13.88.9)

病根 (用户 2026-07-31 抓):
  「curl 返回空值怎么处理」搜了 21 遍 · 「续费 用户」搜了几十遍话题不换
  多个组件手拼 google URL 死磕重搜·不换站/不去重/无上限
  search_supervisor 判 is_sufficient 永远 False 就重搜同一 goal

治法:
  1. should_search(query) — 同一 query 归一化后在窗口期内搜索次数超阈值 → 拒绝 + 建议
  2. record_search(query) — 每次真搜前记一笔·落 .search_loop_count.json
  3. loop_break(goal) — 供 content_supervisor 用·同一 goal 搜过 N 次仍不够 → 强制 stop/switch_topic

用户原话:「失败了灵活换同类网址/换策略/该放弃就放弃·不是原地重来」
"""
import os
import sys
import json
import time
import re
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_STATE = _DIR / ".search_loop_count.json"

# 阈值 · 可调
WINDOW_S = 24 * 3600      # 24h 窗口
MAX_PER_QUERY = 3         # 同一 query 24h 内最多搜 3 次
MAX_PER_GOAL = 3          # 同一 goal 被 supervisor 判不够重搜的上限


def _alert(severity: str, msg: str):
    try:
        sys.path.insert(0, str(_DIR))
        from alert_bus import notify
        notify("search_loop_guard", severity, msg)
    except Exception:
        pass


def _norm(query: str) -> str:
    """归一化 · 去空格/小写/压多空格 → 同一意图判重"""
    if not query:
        return ""
    q = str(query).strip().lower()
    q = re.sub(r"\s+", " ", q)
    # 去常见标点·让「curl返回空值」与「curl 返回空值?」判同一
    q = re.sub(r"[，。？?！!、,.:：;；\"'（）()\[\]【】]", "", q)
    # 去中/英与中文之间的空格 (让「curl 返回空值」与「curl返回空值」判同一意图)
    # 保留纯英文词之间的空格 (如 "curl empty output" 两个英文词有语义边界)
    q = re.sub(r"(?<=[一-鿿])\s+", "", q)   # 中文后的空格去掉
    q = re.sub(r"\s+(?=[一-鿿])", "", q)     # 中文前的空格去掉
    return q.strip()


def _load() -> dict:
    if not _STATE.exists():
        return {}
    try:
        with open(_STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict):
    try:
        tmp = _STATE.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, _STATE)
    except Exception as e:
        _alert("WARN", f"落盘失败: {type(e).__name__}:{str(e)[:80]}")


def _prune(entry: dict, now: float) -> dict:
    """剔除窗口外的时间戳"""
    ts = [t for t in entry.get("ts", []) if now - t < WINDOW_S]
    entry["ts"] = ts
    return entry


def should_search(query: str, kind: str = "query",
                  max_n: int = None) -> tuple:
    """搜索前先问 · (allowed: bool, reason: str)

    kind: "query" (阈值 MAX_PER_QUERY) 或 "goal" (阈值 MAX_PER_GOAL)
    同一归一化 key 在 24h 窗口内已搜 >= 阈值 → False + 建议
    """
    key = _norm(query)
    if not key:
        return True, "empty_query_pass"
    if max_n is None:
        max_n = MAX_PER_GOAL if kind == "goal" else MAX_PER_QUERY
    now = time.time()
    data = _load()
    entry = _prune(data.get(key, {"ts": [], "kind": kind}), now)
    n = len(entry["ts"])
    if n >= max_n:
        return False, (f"loop_guard:同一{kind}『{query[:40]}』"
                       f"24h内已搜{n}次(上限{max_n})·建议换题/换角度/放弃该query")
    return True, f"ok:{n}/{max_n}"


def record_search(query: str, kind: str = "query"):
    """真搜后记一笔"""
    key = _norm(query)
    if not key:
        return
    now = time.time()
    data = _load()
    entry = _prune(data.get(key, {"ts": [], "kind": kind}), now)
    entry["ts"].append(now)
    entry["kind"] = kind
    entry["last_query"] = str(query)[:120]
    data[key] = entry
    _save(data)


def guarded_search(query: str, mode: str = "fast", top_k: int = 5,
                   kind: str = "query", **kwargs):
    """便捷包装 · should_search 过关才真搜·并自动 record
    返回 (hits: list, blocked: bool, reason: str)
    blocked=True 时 hits=[]·上游据 reason 换题/放弃
    """
    allowed, reason = should_search(query, kind=kind)
    if not allowed:
        _alert("INFO", reason)
        return [], True, reason
    try:
        sys.path.insert(0, str(_DIR))
        from search_agent import search
        hits = search(query, mode=mode, top_k=top_k, **kwargs)
    except Exception as e:
        return [], False, f"search_err:{type(e).__name__}:{str(e)[:80]}"
    record_search(query, kind=kind)
    return hits, False, reason


def loop_break(goal: str, is_sufficient: bool, next_action: str) -> dict:
    """供 content_supervisor 用 · 判 goal 是否该强制停

    调用时机: supervisor 已判 is_sufficient=False 准备重搜
    返回 {"force_stop": bool, "override_action": str, "reason": str}
    若同一 goal 已被判不够重搜 >= MAX_PER_GOAL 次 → 强制 switch_topic
    只在 is_sufficient=False 且 next_action 会导致重搜时才计数/拦截
    """
    if is_sufficient or next_action in ("stop", "ask_user"):
        return {"force_stop": False, "override_action": next_action,
                "reason": "not_looping"}
    key = _norm(goal)
    if not key:
        return {"force_stop": False, "override_action": next_action,
                "reason": "empty_goal"}
    now = time.time()
    data = _load()
    gk = f"__goal__:{key}"
    entry = _prune(data.get(gk, {"ts": [], "kind": "goal"}), now)
    n = len(entry["ts"])
    # 记这次「判不够→想重搜」
    entry["ts"].append(now)
    entry["kind"] = "goal"
    entry["last_query"] = str(goal)[:120]
    data[gk] = entry
    _save(data)
    if n + 1 >= MAX_PER_GOAL:
        _alert("INFO", f"goal『{goal[:40]}』重搜{n+1}次仍不够·强制 switch_topic")
        return {"force_stop": True, "override_action": "switch_topic",
                "reason": (f"loop_guard:goal 已重搜{n+1}次仍 is_sufficient=False"
                           f"·停止原地重来·建议换题")}
    return {"force_stop": False, "override_action": next_action,
            "reason": f"goal_loop:{n+1}/{MAX_PER_GOAL}"}


def stats() -> dict:
    """看当前计数状态"""
    now = time.time()
    data = _load()
    out = {"queries": 0, "goals": 0, "hot": []}
    for k, v in data.items():
        ts = [t for t in v.get("ts", []) if now - t < WINDOW_S]
        if not ts:
            continue
        if k.startswith("__goal__:"):
            out["goals"] += 1
        else:
            out["queries"] += 1
        if len(ts) >= 2:
            out["hot"].append({"key": v.get("last_query", k)[:50],
                               "n": len(ts), "kind": v.get("kind")})
    out["hot"].sort(key=lambda x: -x["n"])
    out["hot"] = out["hot"][:10]
    return out


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="search_loop_guard")
    p.add_argument("--stats", action="store_true")
    p.add_argument("--check", help="测某 query 是否允许搜")
    p.add_argument("--reset", action="store_true", help="清空计数")
    args = p.parse_args()
    if args.reset:
        if _STATE.exists():
            _STATE.unlink()
        print("已清空")
    elif args.check:
        print(should_search(args.check))
    else:
        print(json.dumps(stats(), ensure_ascii=False, indent=2))
