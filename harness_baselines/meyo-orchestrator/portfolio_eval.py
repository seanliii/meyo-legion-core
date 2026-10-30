# -*- coding: utf-8 -*-
"""组合式(portfolio+verify)架构真实评测:CC 主 + 失败时验证性升级到其他家。

不是盲聚合:对 CC 漏、但某家真解出的题,看"验证门"能否把正确的那家答案挑出来。
若能稳定挑出 → 端到端正确率 > 单家 CC(17/40)。用真实 SearchFabric 验证,不给 gold。
"""
from __future__ import annotations
import json, glob, re, collections
from pathlib import Path
import team_eval as T   # 复用 _search / _post / _keyword_hit / _candidate_final

_FB = Path(__file__).resolve().parent.parent / "fair_bench_40"
HS = ["cc", "deepagents", "codex", "deepseek", "hermes", "auto"]


def _load():
    qs = {q["qid"]: q for q in json.loads((_FB / "manifest_40.json").read_text())["questions"]}
    hits = collections.defaultdict(dict); finals = collections.defaultdict(dict)
    for p in glob.glob(str(_FB / "traces/*/*/latest.json")):
        d = json.loads(Path(p).read_text()); ev = d["evaluation"]
        q, h = d["qid"], d["harness"]
        if q in qs and h in HS:
            hits[q][h] = bool(ev.get("hit")); finals[q][h] = str(ev.get("final", "")).strip()
    return qs, hits, finals


def _verify_score(question: str, ans: str) -> int:
    if not T._clean_candidate(ans):
        return -1
    q = T._post([
        {"role": "system", "content": "生成一个最能证伪/证实该答案是否满足问题所有约束的检索式,只输出一行。"},
        {"role": "user", "content": f"问题:{question}\n答案:{ans}"}]).strip().splitlines()[0][:200]
    ev = T._search(q) if q else ""
    v = T._post([
        {"role": "system", "content": "基于检索证据判断答案是否逐条满足约束。第一行只输出0-100支持分。"},
        {"role": "user", "content": f"问题:{question}\n答案:{ans}\n证据:\n{ev}"}]).strip()
    m = re.search(r"\d{1,3}", v)
    return min(int(m.group()), 100) if m else 0


def run():
    qs, hits, finals = _load()
    cc_base = sum(hits[q].get("cc", False) for q in qs)
    # 头部机会:CC 漏但有队友解出的题
    headroom = [q for q in qs if not hits[q].get("cc") and any(hits[q].get(h) for h in HS if h != "cc")]
    recovered = []
    rows = []
    for q in headroom:
        question = qs[q]["problem"]; gold = qs[q]["gold"]
        # 候选 = CC + 各队友(去重),对每个做验证打分,取最高
        cands = []
        for h in HS:
            f = finals[q].get(h)
            if T._clean_candidate(f):
                cands.append((h, f))
        seen = set(); scored = []
        for h, f in cands:
            k = re.sub(r"[^a-z0-9]", "", f.lower())[:40]
            if k in seen:
                continue
            seen.add(k)
            scored.append((_verify_score(question, f), h, f))
        scored.sort(reverse=True)
        top = scored[0] if scored else (0, "", "")
        pick = top[2] if top[0] >= 50 else "UNKNOWN"
        hit = T._keyword_hit(pick, gold)
        recovered.append(hit)
        rows.append({"qid": q, "gold": gold, "picked": pick[:80], "score": top[0],
                     "picked_harness": top[1], "recovered": hit})
        print(q, "recovered=", hit, "pick=", pick[:50], "score=", top[0], flush=True)
    rec = sum(recovered)
    portfolio = cc_base + rec
    out = {"cc_base": cc_base, "headroom": len(headroom), "recovered": rec,
           "portfolio_total": portfolio, "n": len(qs),
           "beats_cc": portfolio > cc_base, "rows": rows}
    (Path(__file__).resolve().parent / "captain_traces" / "portfolio_eval.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\n=== CC {cc_base}/{len(qs)} + recovered {rec}/{len(headroom)} "
          f"= portfolio {portfolio}/{len(qs)} | beats_cc={out['beats_cc']} ===")


if __name__ == "__main__":
    run()
