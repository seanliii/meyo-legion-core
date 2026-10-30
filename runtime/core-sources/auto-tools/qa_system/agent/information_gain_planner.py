# -*- coding: utf-8 -*-
"""information_gain_planner · 通用信息增益检索规划器。

核心思想: 不按题型写秘诀，而是把题面约束、候选状态、证据价值和失败历史作为变量，
每轮选择最能缩小候选空间的下一步动作。

状态机:
  UNKNOWN_TARGET -> CANDIDATE_ENTITY_SET -> LOCKED_ENTITY -> ATTRIBUTE_VERIFICATION
  -> CONFLICT_RESOLUTION -> READY_TO_FINAL

fail-safe: 纯函数为主，异常返回空建议；env MEYO_INFO_GAIN_PLANNER=0 可关闭。
"""
import os
import re
import json


def _enabled():
    return os.environ.get("MEYO_INFO_GAIN_PLANNER", "1").strip().lower() not in (
        "0", "false", "off", "no")


def _text(obj):
    if isinstance(obj, dict):
        return str(obj.get("text") or obj.get("claim") or obj.get("content") or "")
    return str(obj or "")


def _tokens(text):
    try:
        return [t.lower() for t in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,}|\d+(?:\.\d+)?", str(text))]
    except Exception:
        return []


def score_constraint(claim):
    """约束信息增益评分。只看结构特征，不看任何领域答案。"""
    s = _text(claim)
    if not s:
        return 0.0
    toks = _tokens(s)
    score = 0.0
    # 精确短语/引号是高信息增益锚点。
    score += 2.0 * len(re.findall(r'"[^"]{3,}"|\'[^\']{3,}\'', s))
    # 数值、百分比、日期通常显著缩小候选空间。
    score += 1.2 * len(re.findall(r"\d+(?:\.\d+)?\s*%|\b\d{4}\b|\d+(?:\.\d+)?", s))
    # 大写专名/多词短语一般比普通领域词更稀有。
    score += 1.0 * len(re.findall(r"\b[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){1,4}\b", s))
    # 长度适中的具体约束比泛词更可检索。
    score += min(2.0, len(set(toks)) / 6.0)
    generic = {"person", "player", "company", "university", "tournament", "match",
               "year", "name", "answer", "entity", "data", "source", "official"}
    if toks and sum(1 for t in toks if t in generic) / float(len(toks)) > 0.5:
        score -= 1.5
    return round(max(0.0, score), 3)


def rank_constraints(claims, covered=None):
    covered = set(covered or [])
    ranked = []
    for idx, claim in enumerate(claims or []):
        if idx in covered:
            continue
        ranked.append({"index": idx, "text": _text(claim)[:220],
                       "score": score_constraint(claim),
                       "tokens": _tokens(_text(claim))[:12]})
    ranked.sort(key=lambda item: (-item["score"], item["index"]))
    return ranked


def evidence_value(evidence_item, claim_text="", candidate=""):
    """证据价值评分: 权威性/直接性/独立性/约束覆盖。"""
    try:
        if isinstance(evidence_item, dict):
            txt = str(evidence_item.get("text") or evidence_item.get("content") or "")
            src = str(evidence_item.get("source") or evidence_item.get("url") or "")
        else:
            txt, src = str(evidence_item), ""
        low = (txt + " " + src).lower()
        score = 0.0
        authority_markers = ("official", "edu", "gov", "profile", "cv", "resume", "press release",
                             "annual report", "filing", "database", "api", "archive")
        score += 0.8 * sum(1 for m in authority_markers if m in low)
        ctoks = set(_tokens(claim_text))
        if ctoks:
            score += min(2.0, sum(1 for t in ctoks if t in low) / max(1, len(ctoks)) * 2.0)
        if candidate and candidate.lower() in low:
            score += 1.0
        if len(txt) > 400:
            score += 0.3
        if any(x in low for x in ("forum", "reddit", "fandom", "fan wiki")):
            score -= 0.3
        return round(max(0.0, score), 3)
    except Exception:
        return 0.0


def infer_state(session):
    """从通用状态变量推断阶段。"""
    try:
        if (session or {}).get("_ga_verified_pass"):
            return "READY_TO_FINAL"
        if (session or {}).get("_candidate_conflicts"):
            return "CONFLICT_RESOLUTION"
        if (session or {}).get("_candidate_hypotheses"):
            return "ATTRIBUTE_VERIFICATION"
        if (session or {}).get("_candidate_vault") or (session or {}).get("_entity_bank"):
            return "CANDIDATE_ENTITY_SET"
        return "UNKNOWN_TARGET"
    except Exception:
        return "UNKNOWN_TARGET"


def _github_query(question, claim_text, candidate=""):
    """给代码/数据源发现生成通用英文/数字关键词，避免把自然语言长句原样丢给 GitHub。"""
    try:
        blob = " ".join([str(candidate or ""), str(claim_text or ""), str(question or "")])
        words = []
        stop = {"the", "and", "for", "with", "that", "which", "what", "name", "answer",
                "determine", "includes", "following", "clues", "about", "between", "somewhere",
                "need", "needs", "specific", "unknown", "target", "entity"}
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,}|\d+(?:\.\d+)?%?", blob):
            low = token.lower()
            if low in stop:
                continue
            if low not in [w.lower() for w in words]:
                words.append(token)
        numeric = [w for w in words if re.search(r"\d", w)]
        alpha = [w for w in words if not re.search(r"\d", w)]
        has_number = bool(numeric)
        # GitHub 适合找“数据源/工具”，不适合直接搜一串题面数字。数字只保留少量，
        # 优先保留领域名词；数据源关键词作为通用后缀。
        suffix = ["dataset", "csv", "api", "stats"] if has_number else ["data", "api"]
        chosen = alpha[:6] + numeric[:2] + suffix
        return " ".join(chosen)[:240]
    except Exception:
        return str(claim_text or question or "")[:200]


def _query_for(claim_text, candidate="", failed=None):
    failed = {str(x).lower() for x in (failed or [])}
    claim_text = str(claim_text or "").strip()
    candidate = str(candidate or "").strip()
    variants = []
    quoted = re.findall(r'"([^"]{3,80})"|\'([^\']{3,80})\'', claim_text)
    phrases = [a or b for a, b in quoted]
    nums = re.findall(r"\d+(?:\.\d+)?\s*%|\b\d{4}\b|\d+(?:\.\d+)?", claim_text)
    caps = re.findall(r"\b[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,3}\b", claim_text)
    if candidate:
        variants.append(" ".join([candidate, claim_text]))
        if nums:
            variants.append(" ".join([candidate] + nums[:4]))
    if phrases:
        variants.append(" ".join('"%s"' % p for p in phrases[:2]))
    if nums and caps:
        variants.append(" ".join(nums[:5] + caps[:3]))
    if nums:
        variants.append(" ".join(nums[:6]))
    variants.append(claim_text)
    for q in variants:
        q = re.sub(r"\s+", " ", q).strip()
        if len(q) >= 6 and q.lower() not in failed:
            return q[:300]
    return ""


def plan_next_action(question, session, claims, evidence=None):
    """返回下一步动作建议 dict。"""
    res = {"state": "UNKNOWN_TARGET", "action_tool": "", "action_args": {},
           "target_constraint": "", "reason": "", "score": 0.0}
    try:
        if not _enabled():
            return res
        session = session or {}
        state = infer_state(session)
        res["state"] = state
        # ── 合理搜索的正向收敛判据(2026-08-24 用户反馈:发散源于"穷尽"·改为证据充分即停)──
        #   已有支持候选 + 关键约束已覆盖(无未决/缺失约束) → 停止搜索、收敛交卷,
        #   不再为覆盖率追加低增益查询。通用·不针对题集·fail-safe(判不出就照常规划)。
        try:
            _missing = session.get("_missing_constraints")
            _constraints_closed = isinstance(_missing, list) and len(_missing) == 0
            _supported = ""
            for _c in (session.get("_candidate_hypotheses") or []):
                _st = str((_c.get("state") if isinstance(_c, dict) else "") or "").lower()
                if _st in ("supported", "selected", "delivery_verified"):
                    _supported = str(_c.get("name") if isinstance(_c, dict) else _c)
                    break
            # 冗余低增益护栏:最近若干步搜索都无投影变化(state_changed=False),也应收敛/换向
            _recent_no_gain = 0
            for _fp in (session.get("_pilot_failed_paths") or [])[-4:]:
                if (isinstance(_fp, dict)
                        and _fp.get("reason") in ("no_projection_delta", "no_candidate_delta")):
                    _recent_no_gain += 1
            if _supported and (_constraints_closed or _recent_no_gain >= 4):
                res.update({
                    "action_tool": "converge", "action_args": {},
                    "target_constraint": "", "score": 9.9,
                    "reason": "evidence_sufficient_converge:candidate=%s:closed=%s:no_gain=%d" % (
                        _supported[:40], _constraints_closed, _recent_no_gain)})
                return res
            # 无候选的止损收敛:不能因为永远产不出 supported 候选就无限搜索到墙钟。
            # 工具步已足够多 + 最近连续无 projection delta → 搜索边际信息增益为零,
            # 主动停止并让上层 best-effort/abstain。它不提高正确率,但防 66 次工具空转。
            _tool_count = len(session.get("tool_log") or [])
            if not _supported and _tool_count >= 12 and _recent_no_gain >= 4:
                res.update({
                    "action_tool": "converge", "action_args": {},
                    "target_constraint": "", "score": 0.0,
                    "reason": "search_exhausted_no_candidate:tools=%d:no_gain=%d" % (
                        _tool_count, _recent_no_gain)})
                return res
        except Exception:
            pass
        # Inverted questions must verify named candidates, not pivot from a failed broad search
        # to GitHub/data-source discovery. Candidate generation is handled by the strategy loop;
        # once hypotheses exist, every retrieval binds candidate + unresolved constraint.
        _strategy = session.get("_strategy_route") or {}
        if _strategy.get("route") == "inverted_multi_constraint_entity":
            try:
                import inverted_entity_pipeline as _inverted
                action = _inverted.next_verification_query(session)
                if action.get("query"):
                    res.update({
                        "state": "ATTRIBUTE_VERIFICATION", "action_tool": "search",
                        "action_args": {"question": action["query"]},
                        "target_constraint": action.get("claim_id") or "",
                        "reason": "inverted_candidate_constraint_verification", "score": 10.0,
                    })
                else:
                    res["reason"] = "inverted_candidate_generation_required"
                return res
            except Exception as exc:
                res["reason"] = "inverted_planner_error:%s:%s" % (
                    type(exc).__name__, str(exc)[:120])
                return res
        progress = session.get("_progress_state") or {}
        missing = session.get("_missing_constraints") or []
        failed = []
        for item in session.get("_pilot_executed_queries") or []:
            failed.append(item)
        failed_tools = set()
        for item in session.get("_pilot_failed_paths") or []:
            if isinstance(item, dict):
                failed.append(item.get("query") or item.get("fingerprint") or "")
                if item.get("tool"):
                    failed_tools.add(str(item.get("tool")).lower())
        for item in session.get("loop_events") or []:
            if isinstance(item, dict) and item.get("ev") == "gate_bus" and item.get("act") == "block":
                sig = str(item.get("sig") or "").lower()
                if sig:
                    failed_tools.add(sig.split("|")[0])
        # 覆盖过的约束: 用 progress 无法定位索引时，保守只按 missing 过滤。
        claim_texts = [_text(c) for c in (claims or [])]
        if missing:
            work_claims = [{"text": m} for m in missing]
        else:
            work_claims = [{"text": c} for c in claim_texts]
        ranked = rank_constraints(work_claims)
        if not ranked:
            return res
        top = ranked[0]
        candidate = ""
        hyps = session.get("_candidate_hypotheses") or []
        if hyps:
            last = hyps[-1]
            candidate = str(last.get("name") if isinstance(last, dict) else last)
        query = _query_for(top["text"], candidate=candidate, failed=failed)
        if not query:
            return res
        # 若 query 看起来是 URL → fetch；若像 SQL/SPARQL/代码片段 → structured_db；
        # 搜索通道熔断后 → search_github 做工具/数据源发现。这里不执行模型生成的 shell。
        low = query.lower().strip()
        is_structured_query = (
            low.startswith(("select ", "with ", "sparql"))
            or (" select " in low and " from " in low)
            or any(mark in low for mark in (" group by ", " order by ", " having ",
                                           "count_if", "wikidata", "pubchem", "openalex"))
        )
        if low.startswith(("http://", "https://")):
            tool, args = "fetch", {"url": query}
        elif is_structured_query:
            tool, args = "structured_db", {"query": query}
        elif any(t in failed_tools for t in ("run_parallel_search", "search")):
            # 搜索通道熔断后，改走工具/数据源发现；query 必须是适合 GitHub 的关键词串。
            tool, args = "search_github", {"query": _github_query(question, top["text"], candidate)}
        else:
            tool, args = "search", {"question": query}
        res.update({"action_tool": tool, "action_args": args,
                    "target_constraint": top["text"], "score": top["score"],
                    "reason": "highest_information_gain_constraint:%s state:%s" % (top["score"], state)})
        return res
    except Exception as exc:
        res["reason"] = "planner_error:%s:%s" % (type(exc).__name__, str(exc)[:120])
        return res


def record_action_result(session, plan, *, progress_delta=0.0, relevant_edges_delta=0):
    try:
        hist = session.setdefault("_ig_plan_log", [])
        item = dict(plan or {})
        item["progress_delta"] = round(float(progress_delta or 0), 4)
        item["relevant_edges_delta"] = int(relevant_edges_delta or 0)
        hist.append(item)
        del hist[:-30]
    except Exception:
        pass


# ----------------------------------------------------------------------------
# SoulDriver-driven one-query planner (merged from upstream 2026-08-24).
# 与上方状态机版并存：native_agent_loop 走 plan_next_action 状态机；
# candidate_provider 走 next_query 纯模型驱动单查询。无任何写死语义词表。
# ----------------------------------------------------------------------------

def next_query(question, *, evidence=None, candidate="", failed=None, llm_call=None):
    """Return one discriminative next query from current context.

    This is intentionally model-driven. The model decides what relation/entity is
    useful in the current task; the code does not encode relation word lists.
    """
    if llm_call is None:
        return ""
    try:
        ev = "\n".join(
            f"- {x.get('title','')}: {str(x.get('text') or x.get('content') or x.get('summary') or '')[:260]}"
            for x in (evidence or [])[:10] if isinstance(x, dict)
        )
        prompt = f"""基于当前题面、已有证据、当前候选和失败query，生成【一个】下一步最有信息增益的搜索query。
目标：区分候选、补齐最关键未知约束、避免同义改写撒网。
只输出严格JSON: {{"query":"...", "why":"..."}}

Question:
{str(question)[:1500]}

Current candidate:
{str(candidate)[:200]}

Recent evidence:
{ev[:3000]}

Failed queries:
{json.dumps(list(failed or [])[-8:], ensure_ascii=False)}
"""
        raw = llm_call(prompt)
        m = re.search(r"\{[\s\S]*\}", str(raw or ""))
        data = json.loads(m.group(0)) if m else {}
        q = str(data.get("query") or "").strip()
        if q and q.lower() not in {str(x).lower() for x in (failed or [])}:
            return q[:220]
    except Exception:
        return ""
    return ""
