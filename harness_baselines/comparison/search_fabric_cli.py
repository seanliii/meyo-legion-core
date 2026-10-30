#!/usr/bin/env python3
"""Shared search command-line adapter for harness comparisons.

2026-08-24: backend switched from the dead local 127.0.0.1:18757 service to the
Tavily cached search layer (harness_baselines/fair_bench_40/cached_tavily.py).

Fairness: every harness (codex / deepseek / hermes / deepagents) issues web
searches through THIS command, so they all hit the same cache. Identical
normalized query -> identical cached results -> zero extra key spend and a
reproducible offline evidence library per question.

Output contract (unchanged): a JSON array of {url, title, content, source}.
Pass --qid <qid> to route the raw response into that question's library.
"""
import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# cached_tavily 实现始终在 fair_bench_40(缓存/keys/证据库共享·不重复烧额度)。
FAIR = HERE.parent / "fair_bench_40"
sys.path.insert(0, str(FAIR))
# MEYO_FAIR_DIR 覆盖时(如 GAIA 跑用 fair_bench_gaia40):搜索 ledger 落到该目录,
# 让 run_comparison._count_fair_searches 读到本轮真实搜索数(否则非 auto 家会被误判
# invalid_no_search)。tavily 缓存/keys/per_question_lib 仍共享 fair_bench_40。
_FAIR_RUN_DIR = HERE.parent / os.environ.get("MEYO_FAIR_DIR", "fair_bench_40")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--qid", default=os.environ.get("MEYO_FAIR_QID", ""))
    parser.add_argument("--depth", choices=["basic", "advanced"], default="advanced")
    args = parser.parse_args()

    # ── 后端选择(2026-08-25 用户: GAIA 直接用 search fabric·不锁 Tavily)──
    # MEYO_SEARCH_BACKEND=fabric → 走 auto 的原生 SearchFabric(与 auto 同源检索,
    #   多源路由+去重,不依赖 Tavily key 池);默认(未设/=tavily)= 缓存 Tavily(bc 不变)。
    if os.environ.get("MEYO_SEARCH_BACKEND", "").lower() == "fabric":
        return _run_fabric(args)

    import cached_tavily

    # ledger 落到本轮 FAIR 目录(per_question_lib 也随之·便于逐题证据归档到本轮);
    # 缓存/keys 仍共享 fair_bench_40。默认(未设 MEYO_FAIR_DIR)= 原地不变。
    if _FAIR_RUN_DIR.resolve() != FAIR.resolve():
        cached_tavily.LEDGER_FILE = _FAIR_RUN_DIR / "ledger" / "search_requests.jsonl"
        cached_tavily.QUESTION_LIB = _FAIR_RUN_DIR / "per_question_lib"
        (_FAIR_RUN_DIR / "ledger").mkdir(parents=True, exist_ok=True)
        (_FAIR_RUN_DIR / "per_question_lib").mkdir(parents=True, exist_ok=True)

    # Keep the shared search implementation usable in restricted harnesses whose file
    # sandbox blocks writes outside their own workspace. The old fallback wrote to
    # HERE/.search_fabric_runtime (the comparison/ dir) — which sandboxed harnesses like
    # deepseek (workspace = deepseek-harness) also cannot write, so every search failed
    # with PermissionError, returned [], and the model silently fell back to prior
    # knowledge (a fairness hole). Fall back to a location that IS writable under the
    # sandbox: prefer the current workspace (cwd), then system temp.
    if not os.access(cached_tavily.CACHE_DIR, os.W_OK):
        import tempfile
        runtime_root = None
        for cand in (Path.cwd() / ".search_fabric_runtime",
                     Path(tempfile.gettempdir()) / "search_fabric_runtime"):
            try:
                (cand / "tavily_cache").mkdir(parents=True, exist_ok=True)
                probe = cand / ".wtest"
                probe.write_text("ok"); probe.unlink()
                runtime_root = cand
                break
            except Exception:
                continue
        if runtime_root is None:
            runtime_root = Path(tempfile.mkdtemp(prefix="search_fabric_"))
        cached_tavily.CACHE_DIR = runtime_root / "tavily_cache"
        cached_tavily.LEDGER_FILE = runtime_root / "ledger" / "search_requests.jsonl"
        cached_tavily.QUESTION_LIB = runtime_root / "per_question_lib"
        cached_tavily.STATE_FILE = runtime_root / "rotation_state.json"
        cached_tavily.LOCK_FILE = cached_tavily.CACHE_DIR / ".cache.lock"

    try:
        payload = cached_tavily.search(
            args.query, qid=args.qid, search_depth=args.depth,
            max_results=max(1, args.top_k), include_raw_content=True)
    except Exception as exc:  # noqa: BLE001 - CLI must emit a parseable envelope
        print(json.dumps([], ensure_ascii=False))
        print(f"[search_fabric_cli] tavily error: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1

    results = []
    for item in (payload.get("response") or {}).get("results", []):
        results.append({
            "url": item.get("url", ""),
            "title": item.get("title", ""),
            # Prefer full raw_content (deep read) when available; fall back to snippet.
            "content": item.get("raw_content") or item.get("content", ""),
            "score": item.get("score"),
            "source": "tavily",
        })
    print(json.dumps(results, ensure_ascii=False))
    print(f"[search_fabric_cli] tavily results={len(results)} "
          f"cache_id={payload.get('cache_id', '')[:12]}", file=sys.stderr)
    return 0


def _run_fabric(args):
    """原生 SearchFabric 后端(auto 同源)。输出契约不变:[{url,title,content,source}]。
    仍向本轮 FAIR ledger 记一条搜索(qid+ts+query),让公平门统计到真实搜索数。"""
    import time
    _fab_dir = str((HERE.parent.parent / "seed-users-system" / "v11" / "evolution"))
    if _fab_dir not in sys.path:
        sys.path.insert(0, _fab_dir)
    try:
        from search_fabric import SearchFabric
        sf = SearchFabric()
        mode = "deep" if args.depth == "advanced" else "balanced"
        hits = sf.search(args.query, top_k=max(1, args.top_k), mode=mode) or []
    except Exception as exc:  # noqa: BLE001
        print(json.dumps([], ensure_ascii=False))
        print(f"[search_fabric_cli] fabric error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    results = [{
        "url": h.get("url", ""), "title": h.get("title", ""),
        "content": h.get("content", "") or h.get("snippet", ""),
        "score": h.get("score"), "source": h.get("source", "fabric"),
    } for h in hits if isinstance(h, dict)]
    # 记 ledger(供 _count_fair_searches 统计;fail-safe)
    try:
        led = _FAIR_RUN_DIR / "ledger" / "search_requests.jsonl"
        led.parent.mkdir(parents=True, exist_ok=True)
        with open(led, "a", encoding="utf-8") as f:
            f.write(json.dumps({"qid": args.qid, "ts": time.time(),
                                "backend": "fabric",
                                "request": {"query": args.query}}, ensure_ascii=False) + "\n")
    except Exception:
        pass
    print(json.dumps(results, ensure_ascii=False))
    print(f"[search_fabric_cli] fabric results={len(results)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
