# -*- coding: utf-8 -*-
"""真实【组队 vs 单家】评测 —— 队长在多家真实候选上做选择/综合,对比单家最好成绩。

诚实设计:
- 候选来自各家【已保存的真实 trace final】(comparison/results/<qid>_<harness>.json),不是重新臆造。
- 喂给队长模型的只有:问题 + 匿名候选答案(不给 harness 名、不给 gold、不给 hit 标签),
  防止模型靠"知道谁强"或泄漏答案作弊。
- 队长输出后用与横评同款的关键词命中评估器对 gold 打分。
- 单家基线直接读各家该题真实 hit。

这回答"组队是否超过单独 harness":给出【组队命中】vs【最强单家命中】vs【oracle 上界】。
"""
from __future__ import annotations
import json, os, re, sys, urllib.request
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BASE = _HERE.parent
_RESULTS = _BASE / "comparison" / "results"
_MANIFEST = _BASE / "fair_bench_40" / "manifest_10.json"

GW = "https://aigc.sankuai.com/v1/openai/native/chat/completions"
APPID = os.environ.get("MEYO_UNIFIED_APPID") or os.environ.get("AIGC_APPID", "")
MODEL = "gpt-5.6-sol"
FALLBACK = "gpt-5.6-sol"

HARNESSES = ["auto", "codex", "deepseek", "hermes", "cc", "deepagents", "penguin", "prime"]


def _load_manifest_questions() -> list[dict]:
    m = json.loads(_MANIFEST.read_text())
    return [q for q in m["questions"] if "gold" in q]


def _candidate_final(qid: str, harness: str) -> str | None:
    p = _RESULTS / f"{qid}_{harness}.json"
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text())
        fin = d.get("result", {}).get("final") if isinstance(d.get("result"), dict) else d.get("final")
        fin = (fin or "").strip()
        return fin or None
    except Exception:
        return None


def _single_hit(qid: str, harness: str) -> bool | None:
    p = _RESULTS / f"{qid}_{harness}.json"
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text())
        ev = d.get("evaluation", {})
        return bool(ev.get("hit")) if "hit" in ev else None
    except Exception:
        return None


def _candidate_packet(qid: str, harness: str, max_chars: int = 1800) -> dict | None:
    """从真实 artifact 提取【答案+证据轨迹】,不读 evaluation/gold(防泄漏)。

    优先取 trace 里工具调用结果/搜索证据;无结构化 trace 时退到 stdout 尾部。
    严格截断防上下文爆炸,但保留末段(通常含最终收敛证据)。
    """
    p = _RESULTS / f"{qid}_{harness}.json"
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text()); r = d.get("result", {})
        final = str(r.get("final", "")).strip()
        chunks = []
        for ev in r.get("trace", []) or []:
            data = ev.get("data", ev)
            s = json.dumps(data, ensure_ascii=False)
            if any(k in s.lower() for k in ("search", "result", "source", "evidence", "url")):
                chunks.append(s[:600])
        evidence = "\n".join(chunks[-4:])
        if not evidence:
            evidence = str(r.get("stdout", ""))[-max_chars:]
        return {"answer": final[:500], "evidence": evidence[-max_chars:]}
    except Exception:
        return None


def _keyword_hit(final: str, gold: str) -> bool:
    """与横评一致的宽松命中:gold 分号/逗号切成关键片段,全部出现即命中。"""
    f = (final or "").lower()
    parts = [p.strip().lower() for p in re.split(r"[;,]", gold) if p.strip()]
    if not parts:
        return False
    return all(any(tok in f for tok in [p] + p.split()) if len(p) < 4 else p in f
               for p in parts)


def _post(messages: list[dict]) -> str:
    if not APPID or APPID.startswith("appid-"):
        raise ValueError("Configure a resolved MEYO_UNIFIED_APPID or AIGC_APPID")
    for model in (MODEL, FALLBACK):
        try:
            body = {"model": model, "messages": messages, "max_tokens": 800}
            req = urllib.request.Request(
                GW, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {APPID}"})
            r = json.loads(urllib.request.urlopen(req, timeout=180).read())
            return r["choices"][0]["message"]["content"] or ""
        except Exception:
            continue
    return ""


def team_select(question: str, candidates: list[str]) -> str:
    """v1:朴素综合/选择(plausibility)。基线对照。"""
    lines = "\n".join(f"候选{i+1}: {c}" for i, c in enumerate(candidates))
    msg = [
        {"role": "system", "content":
         "你是答案裁决队长。下面是多个独立 agent 对同一问题给出的候选答案。"
         "逐条核对问题的每个约束,选出最可能正确的答案;若多个候选互补,可综合。"
         "只输出最终答案本身,不要解释。"},
        {"role": "user", "content": f"问题:{question}\n\n{lines}\n\n最终答案:"},
    ]
    return _post(msg).strip()


def _extract_final(out: str) -> str:
    """从裁决文本稳健抽最终答案。优先 FINAL/最终答案/答案 行,否则末个非空行。"""
    if not out:
        return ""
    for pat in (r"(?im)^\s*FINAL\s*:\s*(.+?)\s*$",
                r"(?im)^\s*最终答案\s*[：:]\s*(.+?)\s*$",
                r"(?im)^\s*答案\s*[：:]\s*(.+?)\s*$"):
        ms = re.findall(pat, out)
        if ms:
            return ms[-1].strip().strip("*` ")
    lines = [re.sub(r"^[-*•\d.\)]+\s*", "", x.strip().strip("*` "))
             for x in out.splitlines() if x.strip()]
    return lines[-1] if lines else ""


def team_select_v2(question: str, candidates: list[str]) -> str:
    """v2:约束核验式裁决 —— 先抽约束,逐候选逐约束打分,只选"每条约束都被满足"的候选。

    动机:v1 实测 4/10 < 单家最好 5/10,且会选 plausible-but-wrong。约束核验能减少
    "听起来对"的误选。仍不给 gold/harness 名,纯基于问题约束。
    """
    lines = "\n".join(f"候选{i+1}: {c}" for i, c in enumerate(candidates))
    msg = [
        {"role": "system", "content":
         "你是严格的约束核验裁决官。步骤:\n"
         "1) 把问题拆成一条条【可判定的约束】。\n"
         "2) 对每个候选,逐条判断是否满足(满足/不满足/无法判定)。\n"
         "3) 只选【所有关键约束都满足】的候选;若都不完全满足,选满足最多且无明显矛盾的;\n"
         "   若候选间只是表述差异(同一实体),归一到最规范写法。\n"
         "4) 若没有任何候选可信,输出 UNKNOWN。\n"
         "最后一行以 `FINAL: <答案>` 给出,不要多余解释。"},
        {"role": "user", "content": f"问题:{question}\n\n{lines}"},
    ]
    return _extract_final(_post(msg).strip())


def team_select_v3_evidence(question: str, packets: list[dict]) -> str:
    """v3:证据级聚合 —— 队长看每个候选的【答案+证据轨迹】,逐约束用证据核验后裁决。

    与 v1/v2 区别:不再只看答案字符串,而是读各家真实搜索/证据片段。这是审计定位的
    "answer-only 天花板"突破尝试。仍匿名、不给 gold。
    """
    blocks = []
    for i, pk in enumerate(packets):
        blocks.append(f"[候选{i+1}] 答案: {pk['answer']}\n证据轨迹(节选):\n{pk['evidence']}")
    body = "\n\n".join(blocks)
    msg = [
        {"role": "system", "content":
         "你是证据核验裁决官。每个候选都给了答案+其证据轨迹节选。要求:\n"
         "1) 把问题拆成可判定约束;\n"
         "2) 只采信有证据轨迹支持的主张,证据缺失的候选降权;\n"
         "3) 逐约束核对哪个候选的证据真正满足;可跨候选拼合互补证据;\n"
         "4) 选出证据最完整、无矛盾的答案;都不足则 UNKNOWN。\n"
         "最后一行 `FINAL: <答案>`。"},
        {"role": "user", "content": f"问题:{question}\n\n{body}"},
    ]
    return _extract_final(_post(msg).strip())


_SEARCH_CLI = _BASE / "comparison" / "search_fabric_cli.py"


def _search(query: str, k: int = 6) -> str:
    """真调 search_fabric,返回精简结果文本(标题+片段)。"""
    import subprocess
    try:
        r = subprocess.run(["python3.12", str(_SEARCH_CLI), query],
                           capture_output=True, text=True, timeout=90,
                           cwd=str(_SEARCH_CLI.parent))
        rows = json.loads(r.stdout)
        out = []
        for it in rows[:k]:
            out.append(f"- {it.get('title','')}: {str(it.get('content',''))[:240]} ({it.get('url','')})")
        return "\n".join(out)
    except Exception as e:
        return f"[search_error:{type(e).__name__}]"


def team_select_v4_reexec(question: str, candidates: list[str], max_rounds: int = 3) -> str:
    """v4:分歧驱动【重新执行】—— 队长基于候选分歧发起真实新搜索,读真结果后再裁决。

    这是审计定位的唯一有望突破 post-hoc 天花板的方向:不在成品答案上投票,
    而是主动补证据。队长每轮出 1 个最有信息增益的 query,系统真搜,回灌;
    最多 max_rounds 轮后交卷。仍匿名、不给 gold。
    """
    uniq = sorted(set(c.strip() for c in candidates if c.strip()))
    transcript = [f"问题:{question}", "各 agent 候选答案(分歧):"] + [f"- {c}" for c in uniq]
    for rnd in range(max_rounds):
        ask = _post([
            {"role": "system", "content":
             "你在做分歧裁决。若已能用已知证据确定答案,直接回 `FINAL: <答案>`;"
             "否则只输出 `SEARCH: <一个最能区分候选真伪的检索式>`(每轮只出一个)。"},
            {"role": "user", "content": "\n".join(transcript)},
        ]).strip()
        if "FINAL:" in ask.upper():
            return _extract_final(ask)
        m = re.search(r"SEARCH:\s*(.+)", ask, re.I)
        query = (m.group(1).strip() if m else ask).splitlines()[0][:200]
        res = _search(query)
        transcript.append(f"[第{rnd+1}轮检索] {query}\n{res}")
    # 用尽轮次:基于已补证据强制裁决
    final = _post([
        {"role": "system", "content": "基于以上问题、候选与检索证据,逐约束核验后给最终答案,"
         "仅一行 `FINAL: <答案>`;不确定则 FINAL: UNKNOWN。"},
        {"role": "user", "content": "\n".join(transcript)},
    ]).strip()
    return _extract_final(final)


def _clean_candidate(c: str) -> str | None:
    """去掉退化/进度噪声候选(如 [DEGRADED]/'25+ searches'/'task ... complete' 元话)。"""
    c = (c or "").strip()
    if not c or len(c) > 200:
        return None
    low = c.lower()
    noise = ("[degraded]", "need more steps", "not a match", "task is fully",
             "the task", "i have now run", "searches across")
    if any(w in low for w in noise):
        return None
    return c


def team_select_v5_verify(question: str, candidates: list[str], per_ans_searches: int = 1) -> str:
    """v5:候选级独立核验投票 —— 对每个【不同】候选答案单独发起验证性搜索,
    用证据支持度打分,取最高分。关键:能救回"少数正确"(如 1/7 的 prime=Aberdeen),
    因为它按证据强度而非票数选择。

    这是审计定位的正解方向:候选×约束×来源绑定 + 逐候选反证,而非多数表决。
    """
    # 归一去噪 + 去重(大小写/标点)
    seen, uniq = {}, []
    for c in candidates:
        cc = _clean_candidate(c)
        if not cc:
            continue
        key = re.sub(r"[^a-z0-9一-鿿]", "", cc.lower())[:40]
        if key and key not in seen:
            seen[key] = cc; uniq.append(cc)
    if not uniq:
        return "UNKNOWN"
    if len(uniq) == 1:
        return uniq[0]

    scored = []
    for ans in uniq:
        q = _post([
            {"role": "system", "content":
             "给你一个问题和一个【待验证答案】。生成一个最能【证伪或证实】该答案是否满足"
             "问题所有约束的检索式。只输出检索式一行。"},
            {"role": "user", "content": f"问题:{question}\n待验证答案:{ans}"},
        ]).strip().splitlines()[0][:200]
        evidence = _search(q) if q else ""
        verdict = _post([
            {"role": "system", "content":
             "基于检索证据,判断【待验证答案】是否逐条满足问题约束。"
             "第一行只输出 0-100 的整数支持分(证据充分且吻合→高分;矛盾/无证据→低分)。"},
            {"role": "user", "content":
             f"问题:{question}\n待验证答案:{ans}\n检索证据:\n{evidence}"},
        ]).strip()
        m = re.search(r"\d{1,3}", verdict)
        score = int(m.group()) if m else 0
        scored.append((min(score, 100), ans))
    scored.sort(reverse=True)
    top, ans = scored[0]
    return ans if top >= 50 else "UNKNOWN"


def _json_from_text(text: str) -> dict:
    """从模型文本中提取首个 JSON object。"""
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try: return json.loads(m.group())
            except Exception: pass
    return {}


def team_select_v6_constraint_matrix(question: str, candidates: list[str], max_constraints: int = 5) -> str:
    """v6:候选×约束证据矩阵 —— 一次规划所有约束/查询,真搜,再结构化打分。

    与 v5 的"每候选一条搜索"相比,这里逐约束验证多跳链,并把所有证据放在同一矩阵中
    横向比较,减少各候选评分漂移。这是 event-store/state-machine 的最小可运行原型。
    """
    uniq=[]; seen=set()
    for c in candidates:
        cc=_clean_candidate(c)
        if not cc: continue
        key=re.sub(r"[^a-z0-9一-鿿]","",cc.lower())[:60]
        if key and key not in seen: seen.add(key); uniq.append(cc)
    if not uniq: return "UNKNOWN"
    cand_lines="\n".join(f"{i}: {c}" for i,c in enumerate(uniq))
    plan_txt=_post([
      {"role":"system","content":
       "把问题拆成最多5个关键可验证约束,并为每个候选生成最多3个能区分真伪的搜索query。"
       "严格输出JSON:{\"constraints\":[...],\"candidates\":[{\"id\":0,\"queries\":[...]}]}"},
      {"role":"user","content":f"问题:{question}\n候选:\n{cand_lines}"}
    ])
    plan=_json_from_text(plan_txt)
    constraints=plan.get("constraints",[])[:max_constraints]
    evidence={}
    for row in plan.get("candidates",[]):
        try:i=int(row.get("id"))
        except:continue
        if i<0 or i>=len(uniq):continue
        ev=[]
        for q in row.get("queries",[])[:3]:
            ev.append({"query":str(q)[:200],"results":_search(str(q),k=5)})
        evidence[i]=ev
    score_txt=_post([
      {"role":"system","content":
       "你是严格证据状态机。对每个候选逐约束给 support/contradict/unknown,再给0-100总分。"
       "无来源支持不得高分;明确矛盾重罚。严格JSON:{\"scores\":[{\"id\":0,"
       "\"support\":2,\"contradict\":0,\"unknown\":3,\"score\":50}]}"},
      {"role":"user","content":json.dumps({"question":question,"constraints":constraints,
          "candidates":[{"id":i,"answer":c,"evidence":evidence.get(i,[])} for i,c in enumerate(uniq)]},
          ensure_ascii=False)[:30000]}
    ])
    scored=[]
    for s in _json_from_text(score_txt).get("scores",[]):
        try:
            i=int(s["id"]); score=float(s.get("score",0)); sup=int(s.get("support",0)); con=int(s.get("contradict",0))
            if 0<=i<len(uniq): scored.append((score,sup,-con,uniq[i]))
        except:continue
    if not scored:return "UNKNOWN"
    scored.sort(reverse=True)
    score,sup,negcon,ans=scored[0]
    # 有明确矛盾或零支持不交;否则选证据最高者(宁可少答,不乱答)
    return ans if score>=45 and sup>=1 and negcon>=-1 else "UNKNOWN"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())[:40]


def agreement_gate(cand_by_harness: dict[str, str], min_agree: int = 2) -> tuple[str, bool]:
    """置信门控:≥min_agree 家答案一致 → 高精度接受;否则不接受(交给专家路由/abstain)。

    数据实证(10题):deepseek+codex 一致的 3 题 100% 正确。这是唯一被证明高精度的信号。
    返回 (答案, 是否达成一致高置信)。
    """
    from collections import Counter
    vals = [(h, c) for h, c in cand_by_harness.items() if _clean_candidate(c)]
    if not vals:
        return "UNKNOWN", False
    groups: dict[str, list[str]] = {}
    for h, c in vals:
        groups.setdefault(_norm(c), []).append(c)
    key, members = max(groups.items(), key=lambda kv: len(kv[1]))
    if len(members) >= min_agree:
        return members[0], True
    return "UNKNOWN", False


def main():
    qs = _load_manifest_questions()
    rows = []
    for q in qs:
        qid, gold, prob = q["qid"], q["gold"], q["problem"]
        cands = [(h, _candidate_final(qid, h)) for h in HARNESSES]
        cands = [(h, c) for h, c in cands if c]
        if not cands:
            continue
        # 单家基线(真实 hit)
        singles = {h: _single_hit(qid, h) for h, _ in cands}
        best_single = any(v for v in singles.values() if v)
        oracle = any(_keyword_hit(c, gold) for _, c in cands)  # 候选里是否存在正确答案(上界)
        cand_texts = [c for _, c in cands]
        packets = [_candidate_packet(qid, h) for h, _ in cands]
        packets = [p for p in packets if p]
        a1 = team_select(prob, cand_texts); h1 = _keyword_hit(a1, gold)
        a2 = team_select_v2(prob, cand_texts); h2 = _keyword_hit(a2, gold)
        a3 = team_select_v3_evidence(prob, packets); h3 = _keyword_hit(a3, gold)
        rows.append({"qid": qid, "gold": gold, "n_candidates": len(cands),
                     "best_single_hit": best_single, "oracle_candidate_hit": oracle,
                     "team_v1_answer": a1[:160], "team_v1_hit": h1,
                     "team_v2_answer": a2[:160], "team_v2_hit": h2,
                     "team_v3_answer": a3[:160], "team_v3_hit": h3})
        print(f"{qid} v1={h1} v2={h2} v3={h3} best={best_single} oracle={oracle} "
              f"v3='{a3[:50]}'", flush=True)
    n = len(rows)
    v1 = sum(r["team_v1_hit"] for r in rows)
    v2 = sum(r["team_v2_hit"] for r in rows)
    v3 = sum(r["team_v3_hit"] for r in rows)
    # 真正的"最强单家":同一家在整组题上的总命中;不能把逐题挑赢家的 union 冒充单家。
    single_scores = {}
    for h in HARNESSES:
        vals = [_single_hit(r["qid"], h) for r in rows]
        n_eval = sum(v is not None for v in vals)
        if n_eval:
            single_scores[h] = {"hits": sum(v is True for v in vals), "n": n_eval}
    comparable = {h: s for h, s in single_scores.items() if s["n"] == n}
    best_name, best_row = max(comparable.items(), key=lambda kv: kv[1]["hits"])
    union = sum(r["oracle_candidate_hit"] for r in rows)
    summary = {"n": n, "team_v1_hits": v1, "team_v2_hits": v2, "team_v3_evidence_hits": v3,
               "single_scores": single_scores, "best_single": best_name,
               "best_single_hits": best_row["hits"], "best_single_n": best_row["n"],
               "candidate_union_upper_bound": union, "rows": rows}
    out = _HERE / "captain_traces" / "team_vs_single_eval.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n=== TEAM v1 {v1}/{n} | v2 {v2}/{n} | v3-evidence {v3}/{n} | "
          f"best-single {best_name} {best_row['hits']}/{best_row['n']} | 候选并集上界 {union}/{n} ===")
    print(f"saved {out}")


if __name__ == "__main__":
    main()
