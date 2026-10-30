#!/usr/bin/env python3
"""steer_queue · 2026-08-15 · #51 Steer 双队列(中途插话·活着纠偏非死后收尸)

用户有 UI 后·需要【任务跑到一半·用户中途补一句/纠偏】·而不是等它跑完发现方向错了才重来。
材料 3 源印证 steer 是刚需(buzz SteerRequest / L0-L7 论文 / Pi 双队列)·配三态:
  rejected / admitted→applied / expired ——【受理≠生效】要显式区分·否则用户以为"发了就等于停了"。

设计(照 hitl_gate 形态·qid-keyed 文件队列 + 原子写):
  submit(qid, text, kind) → 写 ~/.meyo/steer/<qid>.jsonl · 返回 steer_id
  drain(qid)             → agent 步间检查点调:取未消费消息·标 consumed·返回 list
  mark(qid, steer_id, state) → applied/rejected/expired 三态回写(供 UI 显示)
  peek(qid)              → 只读全部(UI 查三态用·不消费)
全 fail-safe:任何异常吞掉·绝不影响 agent 主流程(steer 是增强·不是关键路径)。
"""
import os
import json
import time
import hashlib

_STEER_DIR = os.path.expanduser("~/.meyo/steer")
_MAX_TEXT = 2000


def _safe_qid(qid: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(qid or "unknown"))[:80]


def _path(qid: str) -> str:
    os.makedirs(_STEER_DIR, exist_ok=True)
    return os.path.join(_STEER_DIR, _safe_qid(qid) + ".jsonl")


def _now() -> float:
    try:
        return time.time()
    except Exception:
        return 0.0


def _atomic_append(path: str, rec: dict):
    """append 一行(先写全量 tmp 再 replace·避免并发半截行)。"""
    lines = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                lines = [l for l in f.read().splitlines() if l.strip()]
        except Exception:
            lines = []
    lines.append(json.dumps(rec, ensure_ascii=False))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def _rewrite(path: str, recs: list):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def submit(qid: str, text: str, kind: str = "steer") -> dict:
    """用户中途提交一条 steer(纠偏/补充)或 followup(停止后追加)。返回 {ok, steer_id, state}。
    state 初始 = admitted(已受理·尚未生效)——UI 要显式告诉用户"受理≠已生效"。"""
    try:
        _t = str(text or "").strip()[:_MAX_TEXT]
        if not _t:
            return {"ok": False, "error": "empty_text"}
        _ts = _now()
        # steer_id 稳定:qid+ts+text 哈希(不依赖随机)
        sid = hashlib.sha256(f"{qid}|{_ts}|{_t}".encode("utf-8")).hexdigest()[:12]
        rec = {"steer_id": sid, "qid": _safe_qid(qid), "kind": kind,
               "text": _t, "state": "admitted", "consumed": False, "ts": _ts}
        _atomic_append(_path(qid), rec)
        return {"ok": True, "steer_id": sid, "state": "admitted"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}


def drain(qid: str, max_items: int = 3) -> list:
    """agent 步间检查点调:取【未消费】的 steer 消息·标记 consumed(避免重复注入)·返回它们。
    max_items:一次最多取几条(防用户狂发一次性刷爆)。fail-safe 返回 []。"""
    try:
        p = _path(qid)
        if not os.path.exists(p):
            return []
        recs = []
        with open(p, encoding="utf-8") as f:
            for l in f:
                l = l.strip()
                if l:
                    try:
                        recs.append(json.loads(l))
                    except Exception:
                        pass
        fresh = [r for r in recs if not r.get("consumed")][:max_items]
        if not fresh:
            return []
        _fresh_ids = {r["steer_id"] for r in fresh}
        for r in recs:
            if r.get("steer_id") in _fresh_ids:
                r["consumed"] = True
        _rewrite(p, recs)
        return fresh
    except Exception:
        return []


def mark(qid: str, steer_id: str, state: str) -> bool:
    """三态回写:applied(真生效) / rejected(被拒·如越权/超上限) / expired(任务已结束没来得及)。
    供 UI 显示——治材料'受理≠生效·用户以为发了就等于停了'。fail-safe。"""
    try:
        p = _path(qid)
        if not os.path.exists(p):
            return False
        recs = []
        with open(p, encoding="utf-8") as f:
            for l in f:
                l = l.strip()
                if l:
                    try:
                        recs.append(json.loads(l))
                    except Exception:
                        pass
        hit = False
        for r in recs:
            if r.get("steer_id") == steer_id:
                r["state"] = state
                r["resolved_at"] = _now()
                hit = True
        if hit:
            _rewrite(p, recs)
        return hit
    except Exception:
        return False


def peek(qid: str) -> list:
    """只读全部 steer(不消费)·供 UI 查三态。fail-safe 返回 []。"""
    try:
        p = _path(qid)
        if not os.path.exists(p):
            return []
        out = []
        with open(p, encoding="utf-8") as f:
            for l in f:
                l = l.strip()
                if l:
                    try:
                        r = json.loads(l)
                        out.append({"steer_id": r.get("steer_id"), "kind": r.get("kind"),
                                    "text": (r.get("text") or "")[:200], "state": r.get("state"),
                                    "ts": r.get("ts")})
                    except Exception:
                        pass
        return out
    except Exception:
        return []


def expire_pending(qid: str) -> int:
    """任务结束时调:把还没消费/还 admitted 的 steer 标 expired(没来得及生效)。返回标记数。"""
    try:
        p = _path(qid)
        if not os.path.exists(p):
            return 0
        recs = []
        with open(p, encoding="utf-8") as f:
            for l in f:
                l = l.strip()
                if l:
                    try:
                        recs.append(json.loads(l))
                    except Exception:
                        pass
        n = 0
        for r in recs:
            if r.get("state") == "admitted" and not r.get("consumed"):
                r["state"] = "expired"
                n += 1
        if n:
            _rewrite(p, recs)
        return n
    except Exception:
        return 0


if __name__ == "__main__":
    # 自测:submit→drain→mark 三态闭环
    import tempfile
    _STEER_DIR = tempfile.mkdtemp()
    q = "test_qid"
    r1 = submit(q, "别用minimax·先确认题意", "steer")
    r2 = submit(q, "顺便查下2023版本", "steer")
    assert r1["ok"] and r1["state"] == "admitted", r1
    drained = drain(q)
    assert len(drained) == 2, drained
    assert all(d["consumed"] for d in drained)
    mark(q, r1["steer_id"], "applied")
    pk = peek(q)
    assert any(x["state"] == "applied" for x in pk), pk
    # 再 drain 应为空(已消费)
    assert drain(q) == []
    print("steer_queue 自测通过:", [(x["state"], x["text"][:12]) for x in pk])
