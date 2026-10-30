#!/usr/bin/env python3
"""Cached Tavily search for fair six-framework benchmarking.

Fairness contract:
- Identical normalized query + identical search parameters -> exact same cached response.
- Cache miss -> one Tavily request, atomically persisted before returning.
- Multiple processes use an exclusive file lock so six harnesses cannot spend credits twice.
- API keys rotate on quota/rate/network errors; keys are never logged or committed.
- Every raw response is retained verbatim. Per-question libraries symlink/cache-reference every query.
- An append-only JSONL ledger records cache hit/miss, timing, selected key index (not key), and artifact.

Usage:
    python3.12 cached_tavily.py --query "..." --qid <qid> [--depth advanced]
    # JSON response on stdout; diagnostics on stderr.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "tavily_cache"
KEYS_FILE = ROOT / "keys" / "tavily_keys.json"
LEDGER_FILE = ROOT / "ledger" / "search_requests.jsonl"
QUESTION_LIB = ROOT / "per_question_lib"
STATE_FILE = ROOT / "keys" / "rotation_state.json"
# Lock lives in the system temp dir so sandboxed harnesses (e.g. codex
# workspace-write) can always acquire it even when ROOT is read-only to them.
LOCK_DIR = Path(tempfile.gettempdir()) / "meyo_fair_tavily_locks"


def _normalize_query(query: str) -> str:
    return " ".join(str(query or "").split()).strip()


def _canonical_request(query: str, *, search_depth: str, max_results: int,
                       include_raw_content: bool, topic: str) -> dict[str, Any]:
    return {
        "query": _normalize_query(query),
        "search_depth": search_depth,
        "max_results": int(max_results),
        "include_raw_content": bool(include_raw_content),
        "topic": topic,
    }


def _cache_id(request: dict[str, Any]) -> str:
    raw = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
        f.flush()
        os.fsync(f.fileno())


@contextlib.contextmanager
def _exclusive_lock(cache_id="global"):
    """Per-cache-id lock: identical queries serialize (dedup one live request),
    different queries run concurrently. A single global lock would serialize all
    six harnesses' searches and cause 220s+ queue stalls."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = LOCK_DIR / f"{cache_id}.lock"
    with open(lock_path, "a+", encoding="utf-8") as f:
        try:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(Exception):
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _load_keys() -> list[str]:
    env = os.environ.get("TAVILY_API_KEYS", "").strip()
    if env:
        keys = [x.strip() for x in env.split(",") if x.strip()]
    else:
        keys = json.loads(KEYS_FILE.read_text(encoding="utf-8"))
    if not keys:
        raise RuntimeError("no Tavily API keys configured")
    return keys


def _rotation_index(n: int) -> int:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return int(state.get("next_index", 0)) % n
    except Exception:
        return 0


def _set_rotation_index(idx: int, n: int) -> None:
    _atomic_json(STATE_FILE, {"next_index": int(idx) % n})


def _question_ref(qid: str, cache_id: str, request: dict[str, Any],
                  artifact: Path) -> Path | None:
    if not qid:
        return None
    safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in qid)[:80]
    p = QUESTION_LIB / safe / f"{cache_id}.json"
    _atomic_json(p, {
        "qid": qid,
        "cache_id": cache_id,
        "request": request,
        "artifact": str(artifact.relative_to(ROOT)),
    })
    return p


_STOP_TOK = {"the", "a", "an", "of", "in", "on", "at", "and", "or", "to", "for",
             "who", "what", "when", "which", "was", "were", "is", "are", "his",
             "her", "their", "this", "that", "with", "from", "by", "as"}


def _tok(q: str) -> set[str]:
    import re as _re
    return {w for w in _re.findall(r"[a-z0-9]{3,}", str(q or "").lower())
            if w not in _STOP_TOK}


def _fuzzy_same_question_hit(qid: str, request: dict[str, Any]) -> dict[str, Any] | None:
    """同题证据复用(用户 2026-08-26 第4条:证据存好了后面不用重搜)。

    精确 hash miss 时,在【本题 per_question_lib】内(严格 qid 隔离·绝不跨题=不泄漏
    其它题答案)按 query token 重叠找最相近的历史缓存,命中就复用其证据。这样模型换
    措辞发的 query 也能捞到本题已付费搜过的证据,不必重搜/撞 captcha。
    仅在 MEYO_TAVILY_FUZZY_CACHE=1(默认开·开发回归用)启用;=0 走原精确逻辑。
    """
    if os.environ.get("MEYO_TAVILY_FUZZY_CACHE", "1") != "1" or not qid:
        return None
    safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in qid)[:80]
    libdir = QUESTION_LIB / safe
    if not libdir.is_dir():
        return None
    want = _tok(request.get("query", ""))
    if len(want) < 2:
        return None
    best = None
    best_score = 0.0
    for ref in libdir.glob("*.json"):
        try:
            meta = json.loads(ref.read_text(encoding="utf-8"))
            cand_q = (meta.get("request") or {}).get("query", "")
            have = _tok(cand_q)
            if not have:
                continue
            inter = want & have
            n_inter = len(inter)
            if n_inter < 3:
                continue  # 绝对重叠<3词=不算同题相关(防短query碰巧撞几个通用词)
            # 双向 Jaccard:交集 /(并集)——要求两个 query 主题真正接近,不只是本次被覆盖。
            union = len(want | have)
            score = n_inter / max(1, union)
            if score > best_score:
                art = ROOT / meta.get("artifact", "")
                if art.exists():
                    best_score = score
                    best = art
        except Exception:
            continue
    # 阈值:双向 Jaccard≥0.5 且交集≥3 词,才算同题相关命中(严防跨题/噪声误命中)。
    if best is not None and best_score >= 0.5:
        try:
            return json.loads(best.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def search(query: str, *, qid: str = "", search_depth: str = "advanced",
           max_results: int = 10, include_raw_content: bool = True,
           topic: str = "general", force_refresh: bool = False) -> dict[str, Any]:
    request = _canonical_request(query, search_depth=search_depth,
                                 max_results=max_results,
                                 include_raw_content=include_raw_content,
                                 topic=topic)
    if not request["query"]:
        raise ValueError("query is empty")
    cid = _cache_id(request)
    artifact = CACHE_DIR / f"{cid}.json"
    t0 = time.monotonic()
    with _exclusive_lock(cid):
        if artifact.exists() and not force_refresh:
            payload = json.loads(artifact.read_text(encoding="utf-8"))
            _question_ref(qid, cid, request, artifact)
            _append_jsonl(LEDGER_FILE, {
                "ts": time.time(), "qid": qid, "cache_id": cid,
                "cache_hit": True, "elapsed_s": round(time.monotonic() - t0, 6),
                "request": request, "artifact": str(artifact.relative_to(ROOT)),
                "result_count": len((payload.get("response") or {}).get("results") or []),
            })
            return payload

        # 精确 hash miss → 同题证据复用(用户第4条:证据存好了不用重搜)。严格 qid 隔离
        # 在本题 per_question_lib 内按 query token 重叠找相近历史缓存,命中即复用(不重搜)。
        _fz = _fuzzy_same_question_hit(qid, request)
        if _fz is not None:
            _append_jsonl(LEDGER_FILE, {
                "ts": time.time(), "qid": qid, "cache_id": cid,
                "cache_hit": True, "fuzzy_same_question": True,
                "elapsed_s": round(time.monotonic() - t0, 6),
                "request": request,
                "result_count": len((_fz.get("response") or {}).get("results") or []),
            })
            return _fz

        # Cache-only mode applies ONLY to already-seen (old) questions: a question that
        # already has an offline library should reuse it deterministically. A NEW question
        # (no per-question library yet) is always allowed to go live, so cache-only never
        # blocks fresh questions.
        if os.environ.get("MEYO_TAVILY_CACHE_ONLY", "") == "1":
            has_library = bool(qid) and (QUESTION_LIB / (
                "".join(c if c.isalnum() or c in "_-" else "_" for c in str(qid))[:80]
            )).is_dir()
            if has_library:
                _append_jsonl(LEDGER_FILE, {
                    "ts": time.time(), "qid": qid, "cache_id": cid,
                    "cache_hit": False, "cache_only_miss": True,
                    "elapsed_s": round(time.monotonic() - t0, 6),
                    "request": request,
                })
                return {"schema_version": "fair-tavily-cache-v1", "cache_id": cid,
                        "request": request,
                        "response": {"results": [], "query": request["query"]},
                        "cache_only_miss": True}
            # New question → fall through to live search.

        # Per-question live-API cap (default 12). Reasonable search beats exhaustive:
        # the 40q run showed hermes/deepagents/cc/codex burning 30-40 live calls/question
        # (5019 total) which tripped the dev-tier rate limit, while deepseek averaged 3.4
        # with equal accuracy. Once a question exceeds the cap, further cache misses return
        # an explicit empty-miss so the agent converges on collected evidence instead of
        # spraying more searches. 0 disables the cap.
        try:
            live_cap = int(os.environ.get("MEYO_TAVILY_PER_Q_LIVE_CAP", "50") or 50)
        except Exception:
            live_cap = 50
        if live_cap > 0 and qid:
            counter = LOCK_DIR / f"livecount_{_cache_id({'q': qid})[:16]}.txt"
            try:
                used = int(counter.read_text()) if counter.exists() else 0
            except Exception:
                used = 0
            if used >= live_cap:
                _append_jsonl(LEDGER_FILE, {
                    "ts": time.time(), "qid": qid, "cache_id": cid,
                    "cache_hit": False, "live_cap_reached": True, "live_cap": live_cap,
                    "elapsed_s": round(time.monotonic() - t0, 6), "request": request,
                })
                return {"schema_version": "fair-tavily-cache-v1", "cache_id": cid,
                        "request": request,
                        "response": {"results": [], "query": request["query"]},
                        "live_cap_reached": True}

        from tavily import TavilyClient
        keys = _load_keys()
        start = _rotation_index(len(keys))
        errors: list[dict[str, str | int]] = []
        # Rate limits are transient: rotate across all keys, and if every key is
        # currently throttled, back off and retry a bounded number of rounds instead
        # of failing the whole request (which surfaced as tavily returning 0 results).
        max_rounds = 4
        for round_no in range(max_rounds):
            throttled_all = True
            for offset in range(len(keys)):
                idx = (start + offset) % len(keys)
                try:
                    response = TavilyClient(keys[idx]).search(**request)
                    payload = {
                        "schema_version": "fair-tavily-cache-v1",
                        "cache_id": cid,
                        "created_at": time.time(),
                        "request": request,
                        "response": response,
                        "key_index": idx,
                        "attempt_errors": errors,
                    }
                    _atomic_json(artifact, payload)
                    _set_rotation_index(idx + 1, len(keys))
                    if live_cap > 0 and qid:
                        with contextlib.suppress(Exception):
                            counter.write_text(str(used + 1))
                    qref = _question_ref(qid, cid, request, artifact)
                    _append_jsonl(LEDGER_FILE, {
                        "ts": time.time(), "qid": qid, "cache_id": cid,
                        "cache_hit": False, "elapsed_s": round(time.monotonic() - t0, 6),
                        "key_index": idx, "attempt_errors": errors[-3:],
                        "request": request, "artifact": str(artifact.relative_to(ROOT)),
                        "question_ref": str(qref.relative_to(ROOT)) if qref else "",
                        "result_count": len(response.get("results") or []),
                        # Tavily billing: advanced=2 credits, basic=1. Record credits,
                        # not just request count, so quota accounting is accurate.
                        "credits": 2 if request.get("search_depth") == "advanced" else 1,
                    })
                    return payload
                except Exception as exc:
                    name = type(exc).__name__
                    errors.append({"key_index": idx, "error_type": name,
                                   "detail": str(exc)[:240], "round": round_no})
                    # A non-throttle error (auth/quota) means that key is done for now;
                    # only rate-limit errors justify a backoff+retry round.
                    if "UsageLimit" not in name and "RateLimit" not in name \
                            and "429" not in str(exc):
                        throttled_all = False
                    continue
            if not throttled_all:
                break
            # All keys throttled this round: exponential backoff before retrying.
            time.sleep(min(30.0, 3.0 * (2 ** round_no)))
        _append_jsonl(LEDGER_FILE, {
            "ts": time.time(), "qid": qid, "cache_id": cid,
            "cache_hit": False, "failed": True,
            "elapsed_s": round(time.monotonic() - t0, 6),
            "request": request, "attempt_errors": errors,
        })
        raise RuntimeError(f"all {len(keys)} Tavily keys failed: {errors[-1] if errors else 'unknown'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", required=True)
    ap.add_argument("--qid", default="")
    ap.add_argument("--depth", choices=["basic", "advanced"], default="advanced")
    ap.add_argument("--max-results", type=int, default=10)
    ap.add_argument("--topic", default="general")
    ap.add_argument("--no-raw-content", action="store_true")
    ap.add_argument("--force-refresh", action="store_true")
    args = ap.parse_args()
    payload = search(args.query, qid=args.qid, search_depth=args.depth,
                     max_results=args.max_results,
                     include_raw_content=not args.no_raw_content,
                     topic=args.topic, force_refresh=args.force_refresh)
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    print(f"[cached_tavily] {'cache' if (CACHE_DIR / (payload['cache_id'] + '.json')).exists() else 'live'} "
          f"id={payload['cache_id'][:12]} results={len(payload.get('response', {}).get('results', []))}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
