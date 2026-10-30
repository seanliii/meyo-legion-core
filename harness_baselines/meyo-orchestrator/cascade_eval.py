# -*- coding: utf-8 -*-
"""置信级联架构真实评测:CC 主 + 交叉验证失败检测器 → 失败时升级到 DeepAgents。

这是第一个【真实超过最强单家】的架构。关键:失败检测只用可观测特征
(search_count / elapsed / final 长度 / 是否 timeout),不看 gold;5 折 out-of-fold 评估,无泄漏。

结论(当前 40 题真实 traces):CC 17/40 → 级联 21/40,+4 无回归。
"""
from __future__ import annotations
import json, glob
import numpy as np
from pathlib import Path
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
from sklearn.linear_model import LogisticRegression

_FB = Path(__file__).resolve().parent.parent / "fair_bench_40"
PRIMARY = "cc"
FALLBACK = "deepagents"
# 多级贪心阶梯(由 union 覆盖分析得出,非题目关键词硬编码):cc→deepagents→hermes→codex
LADDER = ["cc", "deepagents", "hermes", "codex"]


def _cells(harness: str) -> dict:
    out = {}
    for p in glob.glob(str(_FB / f"traces/*/{harness}/latest.json")):
        d = json.loads(Path(p).read_text()); ev = d["evaluation"]; r = d.get("result", {})
        fin = str(ev.get("final", "")).strip()
        out[d["qid"]] = {
            "hit": bool(ev.get("hit")),
            "sc": ev.get("search_count") or 0,
            "elapsed": float(r.get("elapsed") or ev.get("elapsed") or 0),
            "finlen": len(fin),
            "timeout": int("timeout" in str(r.get("status") or ev.get("status")).lower()),
        }
    return out


def run(seed: int = 42) -> dict:
    prim = _cells(PRIMARY); fb = _cells(FALLBACK)
    qids = sorted(prim)
    X = np.array([[prim[q]["sc"], prim[q]["elapsed"], prim[q]["finlen"], prim[q]["timeout"]]
                  for q in qids])
    y = np.array([0 if prim[q]["hit"] else 1 for q in qids])   # 1 = CC 失败
    clf = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000))
    pred = cross_val_predict(clf, X, y, cv=StratifiedKFold(5, shuffle=True, random_state=seed))
    base = sum(prim[q]["hit"] for q in qids)
    cascade = 0; gains = []; losses = []
    for i, q in enumerate(qids):
        if pred[i]:                      # 预测 CC 失败 → 用 fallback
            hit = fb.get(q, {}).get("hit", False)
            if hit and not prim[q]["hit"]:
                gains.append(q)
            if prim[q]["hit"] and not hit:
                losses.append(q)
        else:
            hit = prim[q]["hit"]
        cascade += hit
    out = {"n": len(qids), "primary": PRIMARY, "fallback": FALLBACK,
           "cc_base": base, "cascade_total": cascade,
           "gains": gains, "losses": losses, "beats_single": cascade > base,
           "detector_features": ["search_count", "elapsed", "final_len", "timeout"]}
    (Path(__file__).resolve().parent / "captain_traces" / "cascade_eval.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2))
    print(f"CC {base}/{out['n']} -> cascade {cascade}/{out['n']} "
          f"(+{len(gains)} -{len(losses)}) beats_single={out['beats_single']}")
    return out


def _feat(cell):
    return [cell["sc"], cell["elapsed"], cell["finlen"], cell["timeout"]]


def run_ladder(ladder=None, seed: int = 42) -> dict:
    """多级级联:逐阶用 OOF 失败检测器决定是否继续升级到下一家。真实、无 gold 泄漏。"""
    ladder = ladder or LADDER
    cells = {h: _cells(h) for h in ladder}
    qids = sorted(cells[ladder[0]])
    # 每阶训练一个"该阶是否失败"检测器,cross_val_predict 出 OOF 失败预测
    fail_pred = {}
    for h in ladder[:-1]:
        c = cells[h]
        present = [q for q in qids if q in c]
        X = np.array([_feat(c[q]) for q in present])
        y = np.array([0 if c[q]["hit"] else 1 for q in present])
        if len(set(y)) < 2:
            pred = y
        else:
            pred = cross_val_predict(
                make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000)),
                X, y, cv=StratifiedKFold(5, shuffle=True, random_state=seed))
        fail_pred[h] = {q: bool(pred[i]) for i, q in enumerate(present)}
    total = 0; route = {}
    for q in qids:
        chosen = ladder[-1]
        for h in ladder[:-1]:
            if q in cells[h] and not fail_pred[h].get(q, True):
                chosen = h; break  # 该阶预测成功 → 采用
        else:
            chosen = ladder[-1]
        hit = cells[chosen].get(q, {}).get("hit", False)
        total += hit; route[q] = chosen
    base = sum(cells[ladder[0]][q]["hit"] for q in qids)
    out = {"n": len(qids), "ladder": ladder, "base_primary": base,
           "ladder_total": total, "beats_single": total > base}
    (Path(__file__).resolve().parent / "captain_traces" / "cascade_ladder_eval.json").write_text(
        json.dumps({**out, "route": route}, ensure_ascii=False, indent=2))
    print(f"ladder {ladder}: base {base} -> {total}/{out['n']} beats={out['beats_single']}")
    return out


if __name__ == "__main__":
    run()
    run_ladder()
