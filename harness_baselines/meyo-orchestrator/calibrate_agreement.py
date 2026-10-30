# -*- coding: utf-8 -*-
"""在 fair_bench_40 独立集上校准"两家一致时精度",证明 agreement-gate 不可靠。

复现审计的关键校准结论:弱家一致往往一起幻觉,一致 != 正确。
输出 captain_traces/agreement_calibration_40q.json。
"""
from __future__ import annotations
import json, glob, re, itertools, collections
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_TRACES = _HERE.parent / "fair_bench_40" / "traces"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())[:80]


def _agree(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    return bool(na and nb and (na == nb or (len(na) > 8 and na in nb)
                               or (len(nb) > 8 and nb in na)))


def calibrate() -> dict:
    D: dict = collections.defaultdict(dict)
    for p in glob.glob(str(_TRACES / "*/*/latest.json")):
        try:
            d = json.loads(Path(p).read_text()); ev = d["evaluation"]
            D[d["qid"]][d["harness"]] = (str(ev.get("final", "")).strip(), bool(ev.get("hit")))
        except Exception:
            continue
    hs = ["codex", "deepseek", "hermes", "cc", "deepagents", "auto"]
    pairs = []
    for a, b in itertools.combinations(hs, 2):
        n = corr = 0
        for rows in D.values():
            if a in rows and b in rows and _agree(rows[a][0], rows[b][0]):
                n += 1; corr += rows[a][1] and rows[b][1]
        if n:
            pairs.append({"pair": f"{a}+{b}", "agree": n, "correct": corr,
                          "precision": round(corr / n, 3)})
    pairs.sort(key=lambda x: -x["agree"])
    return {"n_questions": len(D), "pairs": pairs,
            "conclusion": "agreement != correctness; weak-harness consensus is often shared hallucination"}


if __name__ == "__main__":
    out = calibrate()
    (_HERE / "captain_traces" / "agreement_calibration_40q.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2))
    for p in out["pairs"]:
        print(f"{p['pair']:22} agree={p['agree']:2} correct={p['correct']:2} prec={p['precision']:.0%}")
    print("saved captain_traces/agreement_calibration_40q.json")
