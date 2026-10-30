#!/usr/bin/env python3
"""v13.88.9 · 2026-07-28 · 搜索实验积累 + 收集分析
用户: 「积累每次搜索轨迹为实验·然后有人收集实验结果·再回去给他注入搜索工具这一环使用」

三件事:
  1. record()   — 每次真搜落一条实验记录 (query/策略/命中率/质量/延迟/backend)
  2. analyze()  — 收集实验结果·算「哪种 query 改写命中率高·哪个源质量好」
  3. inject()   — 把分析结论回注 (source 优先级 / query 改写规则) 供 SearchAgent 用

质量分类 (对齐外部分析「失败原因分类·4 种病」):
  ok       — 有正文 (completeness=full/partial · len>500)
  empty    — 0 hits
  shell    — 有 hit 但全是壳 (completeness=shell·无正文)
  timeout  — 搜索超时
  err      — 搜索报错

设计: 纯 jsonl 落盘·不碰网络·不上重家伙 (外部分析建议「SQLite 就行·别上重家伙」·jsonl 更轻)
"""
import os
import re
import sys
import json
from pathlib import Path
from datetime import datetime, timedelta
from collections import Counter, defaultdict

HERE = Path(__file__).resolve().parent
V11 = HERE.parent
EXP_LOG = V11 / ".search_experiments.jsonl"
INJECT_OUT = V11 / ".search_inject.json"  # SearchAgent 读这个调优


def _classify_hits(hits: list) -> str:
    """判这次搜索质量"""
    if not hits:
        return "empty"
    # 有 err/timeout 标记
    for h in hits:
        if isinstance(h, dict):
            src = h.get("source", "")
            if src == "timeout":
                return "timeout"
            if src == "err":
                return "err"
    # 有正文的
    good = sum(1 for h in hits if isinstance(h, dict)
               and (h.get("completeness") in ("full", "partial")
                    or len(h.get("content", "") or "") > 500))
    if good:
        return "ok"
    # 有 hit 但全是壳
    return "shell"


def _strip_goal_prefix(q: str) -> str:
    """剥 search_reflect_inject 回放链的 Goal 前缀包装"""
    return re.sub(
        r"^\s*Goal\s*(\(user wants to know\)|user wants to know|user wants know)\s*[:：]?\s*",
        "", q or "", flags=re.IGNORECASE)


def _is_demo_sentinel(query: str, query_original: str = "") -> bool:
    """v13.88.9g-2 · 探针 demo query 哨兵门 (用户: 「续费 用户 被搜了几十遍话题不换」)
    capability_probe._DEMO_QUERY 是探针专用 demo·探针本体已隔离·但回放链会
    包成 Goal 前缀再搜绕过隔离。在唯一落盘入口 record() 加硬门·任何路径
    写来的裸 demo 一律不落盘。
    判据: 剥 Goal 前缀 + 去尾部标点后·若只剩裸 demo 词 (续费/续费用户) → True
    带任何业务上下文的续费语料 → False (不误伤·测试锁死)。
    """
    for q in (query, query_original):
        if not q:
            continue
        core = _strip_goal_prefix(str(q))
        core = re.sub(r"[\s\?？。\.!！,，]+$", "", core).strip()
        if re.sub(r"\s+", "", core) in ("续费用户", "续费"):
            return True
    return False


def record(query, hits, strategy="", latency_ms=0, query_original="",
           backend="", extra=None):
    """落一条搜索实验记录"""
    # v13.88.9g-2 · demo 哨兵硬门 · 探针 query 任何路径来的一律不落盘
    if _is_demo_sentinel(query, query_original):
        return {"skipped": "demo_sentinel"}
    quality = _classify_hits(hits or [])
    rec = {
        "ts": datetime.now().isoformat(),
        "query": query[:120],
        "query_original": query_original[:120],
        "strategy": strategy,            # 源顺序·如 "bing,zhipu"
        "backend": backend,
        "n_hits": len(hits or []),
        "quality": quality,              # ok/empty/shell/timeout/err
        "latency_ms": int(latency_ms),
    }
    if extra:
        rec.update(extra)
    try:
        with open(EXP_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return rec


def _load_recent(hours=168):
    """读最近 N 小时实验 (默认 7 天)"""
    if not EXP_LOG.exists():
        return []
    cutoff = datetime.now() - timedelta(hours=hours)
    out = []
    for line in EXP_LOG.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
            ts = datetime.fromisoformat(d.get("ts", "")[:19])
            if ts >= cutoff:
                out.append(d)
        except Exception:
            continue
    return out


def analyze(hours=168) -> dict:
    """收集实验结果·算各维度胜率"""
    recs = _load_recent(hours)
    if not recs:
        return {"note": "no_experiments", "n": 0}

    # 1. 各 backend/strategy 的成功率 (quality==ok)
    by_backend = defaultdict(lambda: {"ok": 0, "total": 0, "lat": []})
    quality_dist = Counter()
    for r in recs:
        q = r.get("quality", "?")
        quality_dist[q] += 1
        bk = r.get("backend") or r.get("strategy") or "?"
        by_backend[bk]["total"] += 1
        if q == "ok":
            by_backend[bk]["ok"] += 1
        if r.get("latency_ms"):
            by_backend[bk]["lat"].append(r["latency_ms"])

    backend_stats = {}
    for bk, s in by_backend.items():
        lat = sorted(s["lat"])
        p95 = lat[int(len(lat) * 0.95)] if lat else 0
        backend_stats[bk] = {
            "ok_rate": round(s["ok"] / max(s["total"], 1) * 100, 1),
            "total": s["total"],
            "p95_ms": p95,
        }

    # 2. query 改写效果: 有 query_original 且改写过的·对比命中
    rewritten_ok = sum(1 for r in recs
                       if r.get("query_original")
                       and r.get("query") != r.get("query_original")
                       and r.get("quality") == "ok")
    rewritten_total = sum(1 for r in recs
                          if r.get("query_original")
                          and r.get("query") != r.get("query_original"))

    return {
        "n": len(recs),
        "hours": hours,
        "quality_dist": dict(quality_dist),
        "backend_stats": backend_stats,
        "rewrite_ok_rate": round(rewritten_ok / max(rewritten_total, 1) * 100, 1),
        "rewrite_n": rewritten_total,
    }


def inject(hours=168) -> dict:
    """把分析结论回注·SearchAgent 可读 .search_inject.json 调 source 优先级"""
    a = analyze(hours)
    if a.get("n", 0) < 5:
        return {"note": "too_few_experiments", "n": a.get("n", 0)}

    # 按 ok_rate 排 backend·成功率高的优先·成功率<20% 的建议熔断
    ranked = sorted(a["backend_stats"].items(),
                    key=lambda kv: kv[1]["ok_rate"], reverse=True)
    source_order = [bk for bk, s in ranked if s["ok_rate"] >= 20 and bk != "?"]
    circuit_open = [bk for bk, s in ranked
                    if s["ok_rate"] < 20 and s["total"] >= 5 and bk != "?"]

    out = {
        "updated": datetime.now().isoformat(),
        "source_order": source_order,       # 建议源优先级
        "circuit_open": circuit_open,        # 建议熔断的源 (连续失败)
        "quality_dist": a["quality_dist"],
        "rewrite_ok_rate": a["rewrite_ok_rate"],
        "based_on_n": a["n"],
    }
    try:
        with open(INJECT_OUT, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    # v13.88.9 · 熔断闭环 · 把 circuit_open 写进 _fanout 读的死源文件
    # (_fanout line 258 已读 .search_disabled_sources.json 跳死源·这里补写入端)
    # 只熔断·不永久禁 (带 ts·下次分析成功率回升会解禁)
    if circuit_open:
        dis_p = V11 / ".search_disabled_sources.json"
        try:
            cur = {}
            if dis_p.exists():
                cur = json.load(open(dis_p))
            for src in circuit_open:
                cur[src] = {"reason": "circuit_open·成功率<20%",
                            "disabled_at": datetime.now().isoformat(),
                            "by": "search_experiment.inject"}
            # 解熔断: source_order 里成功率回升的·从死源移除 (只移 inject 加的)
            for src in list(cur.keys()):
                if (src in source_order
                        and isinstance(cur[src], dict)
                        and cur[src].get("by") == "search_experiment.inject"):
                    del cur[src]
            with open(dis_p, "w", encoding="utf-8") as f:
                json.dump(cur, f, ensure_ascii=False, indent=2)
            out["circuit_written_to"] = str(dis_p.name)
        except Exception:
            pass
    return out


if __name__ == "__main__":
    try:
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("--analyze", action="store_true")
        ap.add_argument("--inject", action="store_true")
        ap.add_argument("--hours", type=int, default=168)
        args = ap.parse_args()

        if args.inject:
            print(json.dumps(inject(args.hours), ensure_ascii=False, indent=2))
        else:
            print(json.dumps(analyze(args.hours), ensure_ascii=False, indent=2))
    except Exception as _e:
        import traceback as _tb, sys as _s, os as _o
        _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
        try:
            from alert_bus import notify as _n
            _n(_o.path.basename(__file__).replace(".py",""), "CRITICAL",
               f"{_o.path.basename(__file__)} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        except Exception:
            pass
        _tb.print_exc()
        raise
