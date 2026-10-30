# -*- coding: utf-8 -*-
"""按题型学习 harness 路由(leave-one-out),真实验证"路由是否超过最强固定单家"。

不做答案聚合(已证伪),改做【正确杠杆】:用历史 per-harness×topic 命中率,给新题选最可能对的家。
LOO:对每题,用其余39题学 topic→best_harness,再看该题被选中的家是否命中。无泄漏。
只用 fair_bench_40/traces 真实 hit,不调模型。
"""
from __future__ import annotations
import json, glob, collections
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_FB = _HERE.parent / "fair_bench_40"
_TRACES = _FB / "traces"
HARNESSES = ["codex", "deepseek", "hermes", "cc", "deepagents", "auto"]


def _load():
    topic = {q["qid"]: q.get("topic", "Other")
             for q in json.loads((_FB / "manifest_40.json").read_text())["questions"]}
    hits: dict = collections.defaultdict(dict)  # qid -> harness -> hit
    for p in glob.glob(str(_TRACES / "*/*/latest.json")):
        try:
            d = json.loads(Path(p).read_text()); ev = d["evaluation"]
            hits[d["qid"]][d["harness"]] = bool(ev.get("hit"))
        except Exception:
            continue
    return topic, hits


def _best_fixed(hits) -> tuple[str, int, int]:
    n = len(hits)
    scores = {h: sum(hits[q].get(h, False) for q in hits) for h in HARNESSES}
    best = max(scores, key=scores.get)
    return best, scores[best], n


def _route_loo(topic, hits, global_fallback) -> tuple[int, int, dict]:
    """LOO 路由命中数。训练集=其余题;按 topic 命中率选家,topic 无数据回退全局最佳。"""
    qids = list(hits)
    correct = 0; picks = {}
    for held in qids:
        train = [q for q in qids if q != held]
        # topic -> harness -> (hit,n)
        by_topic = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0]))
        glob_s = collections.defaultdict(lambda: [0, 0])
        for q in train:
            t = topic.get(q, "Other")
            for h in HARNESSES:
                if h in hits[q]:
                    by_topic[t][h][0] += hits[q][h]; by_topic[t][h][1] += 1
                    glob_s[h][0] += hits[q][h]; glob_s[h][1] += 1
        t = topic.get(held, "Other")
        # 先验:全局最强家(如 cc)。仅当某 topic 专家【样本>=3 且 topic命中率>全局最强家全局率+0.15】才偏离。
        gbest = max(HARNESSES, key=lambda h: glob_s[h][0] / glob_s[h][1] if glob_s[h][1] else 0)
        gbest_rate = glob_s[gbest][0] / glob_s[gbest][1] if glob_s[gbest][1] else 0
        pick = gbest
        tt = by_topic.get(t, {})
        best_spec, best_rate = None, -1
        for h in HARNESSES:
            c, nn = tt.get(h, [0, 0])
            if nn >= 3:
                rr = c / nn
                if rr > best_rate:
                    best_spec, best_rate = h, rr
        if best_spec and best_rate > gbest_rate + 0.15:
            pick = best_spec
        picks[held] = pick
        correct += hits[held].get(pick, False)
    return correct, len(qids), picks


def main():
    topic, hits = _load()
    best_name, best_hits, n = _best_fixed(hits)
    union = sum(any(hits[q].get(h, False) for h in HARNESSES) for q in hits)
    r_correct, r_n, picks = _route_loo(topic, hits, best_name)
    out = {"n": n, "best_fixed_single": best_name, "best_fixed_hits": best_hits,
           "router_loo_hits": r_correct, "oracle_union": union,
           "beats_best_single": r_correct > best_hits}
    (_HERE / "captain_traces" / "router_loo_eval.json").write_text(
        json.dumps({**out, "picks": picks}, ensure_ascii=False, indent=2))
    print(f"best fixed single: {best_name} {best_hits}/{n}")
    print(f"router (LOO by topic): {r_correct}/{n}")
    print(f"oracle union: {union}/{n}")
    print(f"ROUTER BEATS BEST SINGLE: {out['beats_best_single']}")


if __name__ == "__main__":
    main()
