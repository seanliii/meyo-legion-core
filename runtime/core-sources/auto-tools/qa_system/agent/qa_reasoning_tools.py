# -*- coding: utf-8 -*-
"""Model-callable adapters over Auto's accumulated reasoning capabilities.

These wrappers turn formerly hardwired gates/planners into OPTIONAL tools for the
Codex/DeepSeek-style thin loop. They do not block, route, or finalize anything; the
model chooses when to call them and consumes their structured advice.

Four consolidated tools are intentionally exposed (not dozens of micro-tools):
- analyze_research_state: evidence -> candidates/coverage/gaps
- compare_candidate_hypotheses: rank/contrast candidate answers
- suggest_high_information_search: one next high-value action/query
- check_answer_readiness: answer vs constraints/evidence + noise hygiene
"""
from __future__ import annotations

import json
from typing import Any


def _session(qid: str) -> dict:
    try:
        import qa_auto_agent as qa
        return qa._sess(str(qid or ""))
    except Exception:
        return {}


def _question(session: dict) -> str:
    return str(session.get("question") or session.get("task") or session.get("goal") or "")


def _evidence(session: dict) -> list:
    try:
        import identity_coverage as ic
        return ic.collect_evidence(session)
    except Exception:
        return list(session.get("evidence") or [])


def _claims(session: dict, question: str) -> list:
    try:
        import identity_coverage as ic
        return ic.extract_claims(
            question, existing_triage=session.get("triage"),
            llm_call=session.get("_llm_call"))
    except Exception:
        return []


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)[:12000]


def build_reasoning_tools(qid: str):
    """Return four plain functions suitable for StructuredTool.from_function."""

    def analyze_research_state(focus: str = "") -> str:
        """Analyze current evidence once: extract concrete answer candidates, constraint
        coverage, conflicts, missing information, and search-efficiency warnings. Use this
        when evidence is hard to synthesize; it is advisory and does not change the answer."""
        session = _session(qid)
        question = _question(session)
        evidence = _evidence(session)
        claims = _claims(session, question)
        try:
            import candidate_provider as cp
            candidates = cp.candidates(
                question, evidence, session=session, extra=[focus] if focus else None,
                llm_call=session.get("_llm_call"), max_cand=8)
        except Exception:
            candidates = []
        try:
            import identity_coverage as ic
            coverage = ic.compute_coverage(
                claims, evidence, candidate=focus,
                llm_call=session.get("_llm_call"))
        except Exception as exc:
            coverage = {"error": f"{type(exc).__name__}:{exc}"}
        try:
            import process_coverage as pc
            process = pc.verify_process_coverage(
                session, question, claims=claims, triage=session.get("triage"),
                llm_call=session.get("_llm_call"))
        except Exception as exc:
            process = {"error": f"{type(exc).__name__}:{exc}"}
        return _dump({
            "candidates": candidates,
            "claims": claims,
            "coverage": coverage,
            "process": process,
            "instruction": "Treat as advice. Inspect cited evidence before answering.",
        })

    def compare_candidate_hypotheses(candidates_json: str = "[]") -> str:
        """Compare concrete candidate answers against all known evidence and constraints.
        Input a JSON list of candidate strings. Returns ranked support/contradictions and
        a best candidate if evidence warrants one. Generate at least one less-salient
        alternative when the leading candidate is famous or obvious."""
        session = _session(qid)
        question = _question(session)
        evidence = _evidence(session)
        claims = _claims(session, question)
        try:
            parsed = json.loads(candidates_json or "[]")
            candidates = [str(x) for x in parsed] if isinstance(parsed, list) else []
        except Exception:
            candidates = [x.strip() for x in str(candidates_json).split(",") if x.strip()]
        try:
            import multi_hypothesis as mh
            ranked = mh.verify_parallel(
                question, candidates, evidence, claims=claims,
                triage=session.get("triage"), llm_call=session.get("_llm_call"))
        except Exception as exc:
            ranked = {"error": f"{type(exc).__name__}:{exc}"}
        return _dump({
            "input_candidates": candidates,
            "comparison": ranked,
            "instruction": "Prefer joint constraint fit over fame or mention frequency.",
        })

    def suggest_high_information_search(candidate: str = "") -> str:
        """Suggest exactly one next search/tool action with the highest expected information
        gain from current evidence and unresolved constraints. Call only when another search
        is genuinely needed; do not repeat low-gain wording variants."""
        session = _session(qid)
        question = _question(session)
        evidence = _evidence(session)
        claims = _claims(session, question)
        try:
            import information_gain_planner as ig
            plan = ig.plan_next_action(question, session, claims, evidence=evidence)
        except Exception as exc:
            plan = {"error": f"{type(exc).__name__}:{exc}"}
        try:
            import candidate_provider as cp
            advice = cp.short_loop_advice(
                question, evidence, session=session, candidate=candidate)
        except Exception:
            advice = ""
        return _dump({"plan": plan, "advice": advice,
                      "instruction": "Execute at most one suggested action, then reassess."})

    def perspective_plan(candidate: str = "") -> str:
        """Setter/solver-perspective planning over current evidence: diagnose why you may
        be stuck (searched wrong / locked wrong / circling / source gap / need compute) and
        propose ONE concrete next step (a precise query, a tool to build, converge, or
        backtrack). Evidence-grounded, never guesses. Call when unsure what to do next.
        (This exposes the former hardwired '换位思考' layer as an opt-in shared tool.)"""
        session = _session(qid)
        question = _question(session)
        evidence = _evidence(session)
        tried = [str((t.get("args") or {})) [:80]
                 for t in (session.get("tool_log") or [])[-12:]
                 if isinstance(t, dict) and "search" in str(t.get("tool") or "")]
        out = {}
        try:
            import perspective_engine as pe
            plan = pe.plan_next_step(question, evidence, llm_call=session.get("_llm_call"),
                                     tried_queries=tried,
                                     current_candidate=candidate or None)
            out["plan"] = plan
        except Exception as exc:
            out["plan_error"] = f"{type(exc).__name__}:{exc}"
        try:
            import perspective_engine as pe
            diag = pe.diagnose_stuck(question, evidence, llm_call=session.get("_llm_call"),
                                     tried_queries=tried, candidate=candidate,
                                     stall_rounds=len(tried))
            out["diagnosis"] = diag
        except Exception as exc:
            out["diagnosis_error"] = f"{type(exc).__name__}:{exc}"
        out["instruction"] = ("Take the single highest-value next step; if the leading "
                              "candidate is contradicted, backtrack to a competing one.")
        return _dump(out)

    def extract_source_anchors(focus: str = "") -> str:
        """Extract distinctive source anchors already present in collected evidence:
        exact quoted phrases, book/work titles, and proper-name entities. Reuse these
        verbatim (quoted) as the next search query to converge, instead of paraphrasing.
        This is how strong solvers pivot from a discovered title/name to the answer."""
        import re
        session = _session(qid)
        evidence = _evidence(session)
        text_blocks = []
        for item in evidence[:40]:
            if isinstance(item, dict):
                text_blocks.append(" ".join(str(item.get(k) or "") for k in
                                            ("title", "content", "snippet", "text", "url")))
            else:
                text_blocks.append(str(item))
        blob = "\n".join(text_blocks)[:20000]
        quotes = re.findall(r'"([^"\n]{6,80})"', blob) + re.findall(r'“([^”\n]{6,80})”', blob)
        titles = re.findall(r'\b([A-Z][A-Za-z0-9.\'’]+(?:\s+[A-Z][A-Za-z0-9.\'’]+){1,6})\b', blob)
        _skip = {"SNIPPET", "QUOTE", "ENTITY", "SRC", "DESC", "AKA", "URL",
                 "SUMMARY", "TITLE", "Instagram", "Facebook", "Twitter", "YouTube"}
        seen, anchors = set(), []
        for phrase in quotes + titles:
            p = phrase.strip()
            key = p.lower()
            if not p or key in seen or p in _skip:
                continue
            if any(w == p for w in _skip):
                continue
            seen.add(key)
            anchors.append(p)
        # Quotes are the most discriminative; keep them first, cap total.
        ranked = ([f'"{q}"' for q in quotes[:6]]
                  + [t for t in anchors if t not in quotes][:10])
        return _dump({
            "anchors": ranked[:12],
            "instruction": "Pick the most distinctive anchor and search it verbatim; "
                           "combine two anchors when one alone is ambiguous.",
        })

    def spawn_research_routes(routes: str = "") -> str:
        """Run several independent research subagents in parallel — YOU decide everything:
        how many, each one's goal, and its role/approach. Nothing is hardwired.

        `routes` is a JSON array you author, e.g.:
          [{"goal": "<what this subagent must find>",
            "approach": "<optional: how it should work / what perspective>",
            "max_iterations": 12}]
        Each item spawns one autonomous subagent (its own context, tools, budget) whose
        task is `goal` and whose extra instructions are `approach`. Omit `approach` to let
        the subagent use the default general reasoning stance. Returns each route's raw
        conclusion; you then synthesize yourself (prefer the answer whose cited evidence
        jointly satisfies every constraint — do not majority-vote or default to fame).

        Use for a hard multi-hop task when one line of attack is uncertain and independent
        angles would help. Keep the count small (2-4 is usually enough)."""
        import concurrent.futures
        session = _session(qid)
        base_question = _question(session)
        try:
            specs = json.loads(routes) if routes and routes.strip() else []
        except Exception as exc:
            return _dump({"error": f"routes_must_be_json_array:{type(exc).__name__}:{exc}"})
        if isinstance(specs, dict):
            specs = [specs]
        if not isinstance(specs, list) or not specs:
            return _dump({"error": "empty_routes",
                          "hint": 'pass a JSON array like [{"goal": "..."}]'})
        # Cap to a sane concurrency bound; the model chooses within it.
        specs = [s for s in specs if isinstance(s, dict)][:6]
        try:
            import qa_auto_agent as qa
            _factory = qa._make_subagent_tool(str(qid or "routes"))
            # BUGFIX(2026-08-26·05fba 实证): _make_subagent_tool 返回的是【工具列表】
            # (return [spawn_subagent, spawn_specialist, ...]),不是单个 callable。
            # 旧代码直接 spawn(goal,...) → TypeError:'list' object is not callable →
            # 每条 route 全废 → 多跳分解崩溃退回整句 web 空搜(05fba 20次0证据的直接根因)。
            # 健壮取出 spawn_subagent:支持 list/tuple(选名字匹配的,回退第一个 callable)
            # 或本身就是 callable(向后兼容)。
            spawn = None
            if callable(_factory):
                spawn = _factory
            elif isinstance(_factory, (list, tuple)):
                for _t in _factory:
                    if callable(_t) and getattr(_t, "__name__", "") == "spawn_subagent":
                        spawn = _t
                        break
                if spawn is None:
                    spawn = next((_t for _t in _factory if callable(_t)), None)
            if spawn is None:
                return _dump({"error": "spawn_unavailable:no_callable_in_factory"})
        except Exception as exc:
            return _dump({"error": f"spawn_unavailable:{type(exc).__name__}:{exc}"})

        def _run(idx_spec):
            idx, spec = idx_spec
            goal = str(spec.get("goal") or spec.get("task") or base_question).strip()
            if not goal:
                return {"route": idx, "error": "empty_goal"}
            approach = str(spec.get("approach") or spec.get("role") or "").strip()
            try:
                iters = int(spec.get("max_iterations") or 12)
            except Exception:
                iters = 12
            iters = max(1, min(iters, 20))
            try:
                out = spawn(goal, system_prompt=approach, max_iterations=iters)
                return {"route": idx, "goal": goal[:160], "approach": approach[:160],
                        "output": str(out)[:6000]}
            except Exception as exc:
                return {"route": idx, "goal": goal[:160],
                        "error": f"{type(exc).__name__}:{exc}"}
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(specs))) as pool:
            results = list(pool.map(_run, list(enumerate(specs))))
        return _dump({
            "routes": results,
            "instruction": ("Synthesize these independent routes yourself. Prefer the answer "
                            "whose cited evidence jointly satisfies every constraint; do not "
                            "majority-vote or default to the most famous option."),
        })

    def check_answer_readiness(answer: str) -> str:
        """Check a proposed final answer against evidence and material constraints. Returns
        ready/gaps/contradictions plus formatting or meta-noise warnings. This is advisory:
        use it once before finalizing, not as a repeated gate."""
        session = _session(qid)
        question = _question(session)
        evidence = _evidence(session)
        claims = _claims(session, question)
        try:
            import identity_coverage as ic
            coverage = ic.compute_coverage(
                claims, evidence, candidate=str(answer or ""),
                llm_call=session.get("_llm_call"))
        except Exception as exc:
            coverage = {"error": f"{type(exc).__name__}:{exc}"}
        try:
            import constraint_ledger as cl
            reconcile = cl.constraint_reconcile(
                answer, question, evidence, claims=claims,
                triage=session.get("triage") or {},
                llm_call=session.get("_llm_call"))
        except Exception as exc:
            reconcile = {"error": f"{type(exc).__name__}:{exc}"}
        try:
            import qa_auto_agent as qa
            noisy = qa._looks_like_finalize_noise(answer)
        except Exception:
            noisy = False
        contradicted = int((reconcile or {}).get("contradicted") or 0)
        satisfied = int((reconcile or {}).get("satisfied") or 0)
        ready = bool(answer and not noisy and contradicted == 0 and satisfied > 0)
        return _dump({
            "ready": ready,
            "answer": answer,
            "noise": noisy,
            "coverage": coverage,
            "reconcile": reconcile,
            "instruction": ("Finalize now." if ready else
                            "Resolve material gaps or choose a better-supported candidate."),
        })

    # Give closures stable tool names/docstrings for StructuredTool.
    analyze_research_state.__name__ = "analyze_research_state"
    compare_candidate_hypotheses.__name__ = "compare_candidate_hypotheses"
    suggest_high_information_search.__name__ = "suggest_high_information_search"
    perspective_plan.__name__ = "perspective_plan"
    extract_source_anchors.__name__ = "extract_source_anchors"
    spawn_research_routes.__name__ = "spawn_research_routes"
    check_answer_readiness.__name__ = "check_answer_readiness"
    return [
        analyze_research_state,
        compare_candidate_hypotheses,
        suggest_high_information_search,
        perspective_plan,
        extract_source_anchors,
        spawn_research_routes,
        check_answer_readiness,
    ]
