#!/usr/bin/env python3
"""高质量任务上下文：保留原始目标、研究证据、强 MVP 与跨轮差距事实。"""
from __future__ import annotations

import hashlib
import json
import time
from copy import deepcopy
from typing import Any

SCHEMA_VERSION = 1
_MAX_ROWS = 200


def _text(value: object, limit: int = 4000) -> str:
    return str(value or "").strip()[:limit]


def _strings(value: object, limit: int = 80) -> list[str]:
    rows = value if isinstance(value, list) else ([value] if value else [])
    return list(dict.fromkeys(_text(item) for item in rows if _text(item)))[:limit]


def _row_list(value: object) -> list[dict]:
    return [dict(item) for item in value if isinstance(item, dict)][:_MAX_ROWS] \
        if isinstance(value, list) else []


def _source_id(row: dict, index: int) -> str:
    value = _text(row.get("source_id") or row.get("evidence_id"), 120)
    if value:
        return value
    seed = "\0".join((str(row.get("url") or "").strip(), _text(row.get("title") or row.get("work"))))
    version = (_text(row.get("revision") or row.get("commit_sha"), 200),
               _text(row.get("content_sha256") or row.get("sha256"), 128))
    if any(version):
        seed += "\0" + "\0".join(version)
    return f"S-{hashlib.sha256(seed.encode()).hexdigest()[:10]}" if seed.strip("\0") else f"S-{index:03d}"


def _normalize_sources(value: object) -> list[dict]:
    rows = []
    for index, item in enumerate(_row_list(value), start=1):
        row = {
            "source_id": _source_id(item, index),
            "title": _text(item.get("title") or item.get("work"), 500),
            "url": str(item.get("url") or "").strip(),
            "kind": _text(item.get("kind") or "web", 80),
            "page_or_state": _text(item.get("page_or_state") or item.get("state"), 500),
            "viewport": _text(item.get("viewport"), 120),
            "visited_at": next((item[key] for key in ("visited_at", "accessed_at", "captured_at")
                                if item.get(key) is not None), None),
            "fetch_status": _text(item.get("fetch_status") or "not_recorded", 80),
            "content_sha256": _text(item.get("content_sha256") or item.get("sha256"), 128),
            "hash_scope": _text(item["hash_scope"], 200) if item.get("hash_scope") is not None else None,
            "revision": _text(item.get("revision") or item.get("commit_sha"), 200),
            "snapshot_ref": _text(item.get("snapshot_ref"), 4000),
            "supports_claims": _strings(item.get("supports_claims")),
            "decision": _text(item.get("decision") or "kept", 40),
            "decision_reason": _text(item.get("decision_reason") or item.get("reason"), 1000),
            "observations": _strings(item.get("observations") or item.get("facts")),
            "evidence_paths": _strings(item.get("evidence_paths") or item.get("artifacts")),
        }
        if row["url"] or row["evidence_paths"]:
            rows.append(row)
    return rows


def _normalize_queries(value: object) -> list[dict]:
    rows = []
    for index, item in enumerate(value if isinstance(value, list) else [], start=1):
        item = item if isinstance(item, dict) else {"query": item}
        query = _text(item.get("query"), 1000)
        if not query:
            continue
        rows.append({"query_id": _text(item.get("query_id"), 120) or f"Q-{index:03d}",
                     "query": query,
                     "why": _text(item.get("why"), 1000),
                     "derived_from": _strings(item.get("derived_from"), 20),
                     "result_source_ids": _strings(item.get("result_source_ids"), 80)})
    return rows


def _normalize_patterns(value: object) -> list[dict]:
    rows = []
    for index, item in enumerate(_row_list(value), start=1):
        pattern = _text(item.get("pattern") or item.get("finding") or item.get("claim"), 2000)
        if not pattern:
            continue
        rows.append({"pattern_id": _text(item.get("pattern_id"), 120) or f"P-{index:03d}",
                     "dimension": _text(item.get("dimension") or "product", 80),
                     "pattern": pattern,
                     "why_it_matters": _text(item.get("why_it_matters") or item.get("user_value"), 2000),
                     "source_ids": _strings(item.get("source_ids") or item.get("evidence_ids"), 80),
                     "adoption_note": _text(item.get("adoption_note") or item.get("tradeoff"), 2000)})
    return rows


def _normalize_journey(value: object) -> list[dict]:
    rows = []
    for index, item in enumerate(value if isinstance(value, list) else [], start=1):
        item = item if isinstance(item, dict) else {"name": item}
        name = _text(item.get("name") or item.get("step") or item.get("user_action"), 500)
        if not name:
            continue
        rows.append({"step_id": _text(item.get("step_id"), 120) or f"J-{index:02d}",
                     "name": name,
                     "user_intent": _text(item.get("user_intent"), 1000),
                     "expected_observation": _text(item.get("expected_observation") or item.get("expected"), 2000),
                     "failure_or_recovery": _text(item.get("failure_or_recovery"), 2000),
                     "reference_pattern_ids": _strings(item.get("reference_pattern_ids"), 40)})
    return rows


def _normalize_critical_dimensions(value: object) -> list[dict]:
    rows = []
    for index, item in enumerate(value if isinstance(value, list) else [], start=1):
        item = item if isinstance(item, dict) else {"dimension": item}
        dimension = _text(item.get("dimension") or item.get("key"), 120)
        outcome = _text(item.get("user_outcome") or item.get("outcome") or
                        item.get("minimum_standard"), 2000)
        if not dimension or not outcome:
            continue
        try:
            weight = min(1.0, max(0.0, float(item.get("impact_weight", item.get("weight", 0)) or 0)))
        except (TypeError, ValueError):
            weight = 0.0
        rows.append({
            "dimension": dimension,
            "user_outcome": outcome,
            "minimum_standard": _text(item.get("minimum_standard") or outcome, 2000),
            "impact_weight": weight,
            "failure_mode": _text(item.get("failure_mode") or item.get("risk"), 2000),
            "required_strengths": _strings(item.get("required_strengths"), 40),
            "owner_task": _text(item.get("owner_task") or item.get("owner"), 120),
            "sample_question": _text(item.get("sample_question") or
                                      item.get("breakthrough_question"), 2000),
            "acceptance_observation": _text(item.get("acceptance_observation"), 2000),
            "rank": index,
        })
    if rows and not any(row["impact_weight"] for row in rows):
        weight = round(1.0 / len(rows), 6)
        for row in rows:
            row["impact_weight"] = weight
    return rows


def _normalize_release_vector(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    allowed = {"pass", "partial", "inconclusive", "below_reference", "not_assessed", "not_applicable"}
    result = {}
    for key in ("file", "runtime", "flow", "outcome", "frontier"):
        state = _text(source.get(key) or "not_assessed", 40).lower()
        result[key] = state if state in allowed else "not_assessed"
    return result


def normalize(raw: dict | None, original_query: str = "", *, previous: dict | None = None,
              reason: str = "compiled", experience_intent: dict | None = None) -> dict:
    """Normalize a model-authored context packet while preserving the user's exact query."""
    source = deepcopy(raw) if isinstance(raw, dict) else {}
    prior = previous if isinstance(previous, dict) else {}
    world_intent = experience_intent if isinstance(experience_intent, dict) else {}
    if not world_intent and isinstance(source.get("experience_intent"), dict):
        world_intent = source["experience_intent"]
    if not world_intent and isinstance(prior.get("experience_intent"), dict):
        world_intent = prior["experience_intent"]
    exact_query = str(original_query if original_query != "" else
                      source.get("original_query") or prior.get("original_query") or "")
    sources = _normalize_sources(source.get("sources") or source.get("visited_sources") or
                                 (source.get("research") or {}).get("sources") or prior.get("sources"))
    queries = _normalize_queries(source.get("query_ledger") or
                                 (source.get("research") or {}).get("query_ledger") or prior.get("query_ledger"))
    patterns = _normalize_patterns(source.get("reference_patterns") or prior.get("reference_patterns"))
    mvp_source = source.get("strong_mvp") if isinstance(source.get("strong_mvp"), dict) else {}
    previous_mvp = prior.get("strong_mvp") if isinstance(prior.get("strong_mvp"), dict) else {}
    strong_mvp = {
        "value_loop": _strings(mvp_source.get("value_loop") or previous_mvp.get("value_loop")),
        "must_have": _strings(mvp_source.get("must_have") or previous_mvp.get("must_have")),
        "deferred": _strings(mvp_source.get("deferred") or previous_mvp.get("deferred")),
        "must_feel": _strings(mvp_source.get("must_feel") or previous_mvp.get("must_feel")),
        "anti_goals": _strings(mvp_source.get("anti_goals") or mvp_source.get("must_not_be") or
                               previous_mvp.get("anti_goals")),
    }
    revision = max(1, int(prior.get("revision") or 0) + (1 if prior else 0))
    frontier_source = source.get("frontier_context") if isinstance(source.get("frontier_context"), dict) else {}
    previous_frontier = prior.get("frontier_context") if isinstance(prior.get("frontier_context"), dict) else {}
    frontier_context = {
        "named_references": _row_list(frontier_source.get("named_references") or
                                       previous_frontier.get("named_references")),
        "dimension_leaders": _row_list(frontier_source.get("dimension_leaders") or
                                        previous_frontier.get("dimension_leaders")),
        "experience_dimensions": _row_list(frontier_source.get("experience_dimensions") or
                                             previous_frontier.get("experience_dimensions")),
        "minimum_parity": _strings(frontier_source.get("minimum_parity") or
                                    previous_frontier.get("minimum_parity")),
        "implementation_brief": _row_list(frontier_source.get("implementation_brief") or
                                            previous_frontier.get("implementation_brief")),
        "dimension_acceptance": _row_list(frontier_source.get("dimension_acceptance") or
                                            previous_frontier.get("dimension_acceptance")),
        "breakthrough_targets": _row_list(frontier_source.get("breakthrough_targets") or
                                            previous_frontier.get("breakthrough_targets")),
        "remaining_gaps": _strings(frontier_source.get("remaining_gaps") or
                                    previous_frontier.get("remaining_gaps")),
    }
    desired_outcome = _text(source.get("desired_outcome") or source.get("user_outcome") or
                            world_intent.get("desired_outcome") or prior.get("desired_outcome"), 2000)
    north_star_source = source.get("north_star") if isinstance(source.get("north_star"), dict) else {}
    prior_north_star = prior.get("north_star") if isinstance(prior.get("north_star"), dict) else {}
    north_star = {
        "user": _text(north_star_source.get("user") or source.get("target_user") or
                      world_intent.get("audience") or prior_north_star.get("user"), 2000),
        "job": _text(north_star_source.get("job") or source.get("core_job") or
                     world_intent.get("core_need") or prior_north_star.get("job"), 2000),
        "observable_outcome": _text(north_star_source.get("observable_outcome") or desired_outcome or
                                    prior_north_star.get("observable_outcome"), 2000),
        "non_negotiables": _strings(north_star_source.get("non_negotiables") or
                                    world_intent.get("non_negotiables") or
                                    prior_north_star.get("non_negotiables")),
    }
    critical_dimensions = _normalize_critical_dimensions(
        source.get("critical_dimensions") or prior.get("critical_dimensions") or
        world_intent.get("experience_dimensions"))
    result = {
        "schema_version": SCHEMA_VERSION,
        "revision": revision,
        "original_query": exact_query,
        "experience_intent": deepcopy(world_intent),
        "north_star": north_star,
        "critical_dimensions": critical_dimensions,
        "claim_ceiling": _text(source.get("claim_ceiling") or prior.get("claim_ceiling") or
                               ("PARITY_OR_INCONCLUSIVE" if frontier_context.get("minimum_parity") else
                                "UNVALIDATED"), 120),
        "current_hypothesis": _text(source.get("current_hypothesis") or
                                    prior.get("current_hypothesis"), 4000),
        "sample_evidence": _row_list(source.get("sample_evidence") or prior.get("sample_evidence")),
        "rendered_observations": _row_list(source.get("rendered_observations") or
                                            prior.get("rendered_observations")),
        "discarded_directions": _row_list(source.get("discarded_directions") or
                                          prior.get("discarded_directions")),
        "owner_changes": _row_list(source.get("owner_changes") or prior.get("owner_changes")),
        "release_vector": _normalize_release_vector(source.get("release_vector") or
                                                    prior.get("release_vector")),
        "frontier_context": frontier_context,
        "target_user": _text(source.get("target_user") or world_intent.get("audience") or
                             prior.get("target_user"), 2000),
        "core_job": _text(source.get("core_job") or world_intent.get("core_need") or
                          prior.get("core_job"), 2000),
        "desired_outcome": desired_outcome,
        "retention_reason": _text(source.get("retention_reason") or prior.get("retention_reason"), 2000),
        "strong_mvp": strong_mvp,
        "golden_journey": _normalize_journey(source.get("golden_journey") or prior.get("golden_journey")),
        "query_ledger": queries,
        "sources": sources,
        "reference_patterns": patterns,
        "technical_context": _row_list(source.get("technical_context") or prior.get("technical_context")),
        "safety_context": _strings(source.get("safety_context") or prior.get("safety_context")),
        "commercial_release_line": _strings(source.get("commercial_release_line") or
                                             source.get("release_line") or prior.get("commercial_release_line")),
        "open_questions": _strings(source.get("open_questions") or prior.get("open_questions")),
        "gap_plan": normalize_gaps(source.get("gap_plan") or prior.get("gap_plan")),
        "change_reason": _text(reason, 1000),
        "updated_at": time.time(),
    }
    return result


def evidence_issues(context: dict | None) -> list[str]:
    """Report missing links as facts; callers decide whether and how to continue."""
    value = context if isinstance(context, dict) else {}
    source_ids = {str(item.get("source_id")) for item in value.get("sources") or [] if isinstance(item, dict)}
    issues = []
    if not value.get("original_query"):
        issues.append("原始 Query 缺失")
    if not value.get("strong_mvp", {}).get("value_loop"):
        issues.append("强 MVP 价值闭环尚未形成")
    if not value.get("golden_journey"):
        issues.append("黄金用户旅程尚未形成")
    if not source_ids:
        issues.append("尚无实际访问的商业参考来源")
    frontier = value.get("frontier_context") if isinstance(value.get("frontier_context"), dict) else {}
    if frontier.get("minimum_parity") and not value.get("critical_dimensions"):
        issues.append("尚未识别决定用户结果的关键体验维度")
    if frontier.get("minimum_parity") and not frontier.get("implementation_brief"):
        issues.append("前沿研究尚未编译为可执行实现简报")
    if frontier.get("minimum_parity") and not frontier.get("dimension_acceptance"):
        issues.append("前沿研究尚未形成逐维度验收观察")
    if frontier.get("minimum_parity") and not value.get("sample_evidence"):
        issues.append("最高风险体验尚无真实样片证据")
    if frontier.get("minimum_parity") and not value.get("rendered_observations"):
        issues.append("尚未记录真实渲染观察")
    if frontier.get("minimum_parity") and not value.get("claim_ceiling"):
        issues.append("当前证据允许的声明上限尚未明确")
    for row in frontier.get("implementation_brief") or []:
        dimension = _text(row.get("dimension") or row.get("key"), 120) or "未命名维度"
        required = ("reference_behavior", "candidate_required_behavior", "acceptance_observation")
        if any(not _text(row.get(key)) for key in required):
            issues.append(f"实现简报 {dimension} 缺少参考行为、候选要求或验收观察")
        if not _strings(row.get("evidence_refs") or row.get("source_ids")):
            issues.append(f"实现简报 {dimension} 没有证据引用")
    for pattern in value.get("reference_patterns") or []:
        missing = [item for item in pattern.get("source_ids") or [] if item not in source_ids]
        if not pattern.get("source_ids"):
            issues.append(f"参考模式 {pattern.get('pattern_id')} 没有来源")
        elif missing:
            issues.append(f"参考模式 {pattern.get('pattern_id')} 引用了未知来源: {', '.join(missing)}")
    return list(dict.fromkeys(issues))


def normalize_gaps(value: object) -> list[dict]:
    rows = []
    for index, item in enumerate(value if isinstance(value, list) else [], start=1):
        item = item if isinstance(item, dict) else {"problem": item}
        problem = _text(item.get("problem") or item.get("finding") or item.get("defect"), 2000)
        if not problem:
            continue
        try:
            impact_weight = min(1.0, max(0.0, float(item.get("impact_weight", item.get("weight", 0)) or 0)))
        except (TypeError, ValueError):
            impact_weight = 0.0
        status = _text(item.get("status") or "open", 40).lower()
        if status not in {"open", "improved", "closed", "inconclusive"}:
            status = "open"
        rows.append({"gap_id": _text(item.get("gap_id"), 120) or f"G-{index:03d}",
                     "dimension": _text(item.get("dimension") or "product", 120),
                     "problem": problem,
                     "severity": _text(item.get("severity") or "medium", 40),
                     "impact_weight": impact_weight,
                     "status": status,
                     "owner_task": _text(item.get("owner_task") or item.get("owner"), 120),
                     "observed": _text(item.get("observed") or item.get("evidence"), 2000),
                     "candidate_observation": _text(item.get("candidate_observation"), 2000),
                     "reference_behavior": _text(item.get("reference_behavior"), 2000),
                     "user_consequence": _text(item.get("user_consequence"), 2000),
                     "root_cause_hypothesis": _text(item.get("root_cause_hypothesis"), 2000),
                     "recommended_outcome": _text(item.get("recommended_outcome") or
                                                   item.get("revision_request"), 2000),
                     "next_experiment": _text(item.get("next_experiment"), 2000),
                     "acceptance_observation": _text(item.get("acceptance_observation") or
                                                       item.get("acceptance"), 2000),
                     "evidence_refs": _strings(item.get("evidence_refs") or item.get("evidence_ids"), 80)})
    return rows


def merge_gaps(context: dict, gaps: object, *, reason: str = "independent review gaps") -> dict:
    merged = deepcopy(context if isinstance(context, dict) else {})
    existing = normalize_gaps(merged.get("gap_plan"))
    incoming = normalize_gaps(gaps)
    by_problem = {item["problem"]: item for item in existing}
    for item in incoming:
        by_problem[item["problem"]] = item
    merged["gap_plan"] = list(by_problem.values())[:_MAX_ROWS]
    merged["revision"] = max(1, int(merged.get("revision") or 1) + 1)
    merged["change_reason"] = _text(reason, 1000)
    merged["updated_at"] = time.time()
    return merged


def reconcile(*contexts: dict | None) -> dict:
    """Merge sparse, same-owner context updates without turning an old stub into authority.

    Higher revisions win conflicting facts. Source/query records are retained
    across sparse updates, and the first recorded original query is preserved.
    This pure view does not advance a revision or read another user's storage.
    """
    values = [deepcopy(value) for value in contexts if isinstance(value, dict) and value]
    if not values:
        return {}

    def revision(value):
        try:
            return max(0, int(value.get("revision") or 0))
        except (TypeError, ValueError):
            return 0

    def identity(field, row, index):
        return (_source_id(row, index) if field == "sources" else
                str(row.get("query") or row.get("query_id") or index))

    def overlay(target, source):
        for key, item in source.items():
            if item in (None, "", [], {}):
                continue
            if isinstance(item, dict) and isinstance(target.get(key), dict):
                overlay(target[key], item)
            else:
                target[key] = deepcopy(item)
        return target

    # Seed order from existing context before applying revision precedence.
    records = {field: {identity(field, row, i): {} for value in values
                       for i, row in enumerate(_row_list(value.get(field)), 1)}
               for field in ("sources", "query_ledger")}
    merged = {}
    for value in sorted(values, key=revision):
        for key, item in value.items():
            if key in records:
                for i, row in enumerate(_row_list(item), 1):
                    overlay(records[key][identity(key, row, i)], row)
            elif item not in (None, "", [], {}):
                if isinstance(item, dict) and isinstance(merged.get(key), dict):
                    overlay(merged[key], item)
                else:
                    merged[key] = item
    for field, rows in records.items():
        if rows or any(field in value for value in values):
            merged[field] = list(rows.values())
    original = next((value["original_query"] for value in values
                     if value.get("original_query")), None)
    if original is not None:
        merged["original_query"] = original
    merged["revision"] = max(map(revision, values))
    return merged


def benchmark_snapshot(plan: dict | None, *, max_sources: int = 16) -> dict:
    """One request's reference/analysis view; presence is never a quality verdict.

    Only explicit provenance fields are consumed, not URLs scraped from our own
    final answer or delivered artifacts. Keep whole links and recorded failures.
    Authorship/claim status stays distinct from observed execution or user feedback.
    """
    root = plan if isinstance(plan, dict) else {}
    packet = root.get("project_context")
    containers = [("", root)]
    if isinstance(packet, dict):
        containers.append(("project_context.", packet))
    sources, analysis, observations = [], [], []
    context_present = False

    def add_source(row, origin, source_kind):
        if not isinstance(row, dict):
            return
        url = row.get("url") or row.get("km_url")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            return
        from urllib.parse import urlsplit
        try:
            parsed = urlsplit(url)
            if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                    or any(ord(char) < 32 for char in url)):
                return
        except ValueError:
            return
        safe = {key: deepcopy(row[key]) for key in (
            "source_id", "title", "kind", "fetch_status", "visited_at", "revision",
            "snapshot_ref", "supports_claims", "observations", "decision_reason",
        ) if key in row}
        safe.update(url=url, origin=origin, source_kind=source_kind)
        safe.setdefault("fetch_status", "not_recorded")
        # Do not collapse contradictory visits or different authors into one fact.
        if safe not in sources:
            sources.append(safe)

    for prefix, item in containers:
        context = item.get("quality_context")
        if isinstance(context, dict):
            context_present = True
            for row in context.get("sources") or []:
                add_source(row, prefix + "quality_context.sources", "research_record")
            for field in ("reference_patterns", "rendered_observations", "gap_plan"):
                for row in _row_list(context.get(field)):
                    destination = observations if field == "rendered_observations" else analysis
                    destination.append({"origin": prefix + "quality_context." + field,
                                        "value": deepcopy(row)})
        frontiers = [(prefix + "frontier_context.", item.get("frontier_context"))]
        if isinstance(context, dict):
            frontiers.append((prefix + "quality_context.frontier_context.", context.get("frontier_context")))
        for origin, frontier in frontiers:
            if not isinstance(frontier, dict):
                continue
            context_present = True
            for field in ("named_references", "dimension_leaders"):
                for row in frontier.get(field) or []:
                    add_source(row, origin + field, "named_reference")
            for field in ("implementation_brief", "dimension_acceptance", "experience_dimensions"):
                analysis.extend({"origin": origin + field, "value": deepcopy(row)}
                                for row in _row_list(frontier.get(field)))
        add_source(item.get("source_ref"), prefix + "source_ref", "caller_supplied")
        add_source(item.get("reference_atlas"), prefix + "reference_atlas", "reference_index")
        observations.extend({"origin": prefix + "journey_observations", "value": deepcopy(row)}
                            for row in _row_list(item.get("journey_observations")))
    count = len(sources)
    return {
        "context_present": context_present, "reference_count": count,
        "source_index": sources[:max_sources], "source_index_omitted": max(0, count - max_sources),
        "analysis": analysis[:_MAX_ROWS], "analysis_omitted": max(0, len(analysis) - _MAX_ROWS),
        "analysis_is_model_authored": True,
        "journey_observations": observations[:_MAX_ROWS],
        "observations_omitted": max(0, len(observations) - _MAX_ROWS),
    }


def research_snapshot(context: dict | None, *, max_sources: int = 12,
                      max_chars: int = 6000) -> dict:
    """A bounded provenance index, not an assessment or an instruction to search.

    The canonical source rows retain their complete URLs. If a row cannot fit
    the prompt index, omit the row rather than truncate it into a broken link.
    """
    value = context if isinstance(context, dict) else {}
    sources = _normalize_sources(value.get("sources"))
    queries = _normalize_queries(value.get("query_ledger"))
    keys = ("source_id", "title", "url", "kind", "fetch_status", "visited_at",
            "content_sha256", "hash_scope", "revision", "snapshot_ref")
    rows, size = [], 0
    for source in sources[:max(0, max_sources)]:
        row = {key: source[key] for key in keys}
        cost = len(json.dumps(row, ensure_ascii=False))
        if size + cost > max_chars:
            break
        rows.append(row)
        size += cost
    return {"source_count": len(sources), "query_count": len(queries),
            "source_index": rows, "source_index_omitted": len(sources) - len(rows),
            "context_ref": deepcopy(value.get("context_ref") or {})}


def role_snapshot(context: dict | None, role: str = "", *, max_sources: int = 12) -> dict:
    """Return bounded role context with the shared North Star facts kept for every member."""
    value = deepcopy(context if isinstance(context, dict) else {})
    role_name = _text(role, 120).lower()
    common = {key: value.get(key) for key in
              ("schema_version", "revision", "original_query", "experience_intent", "north_star",
               "critical_dimensions", "claim_ceiling", "current_hypothesis", "sample_evidence",
               "rendered_observations", "discarded_directions", "owner_changes", "release_vector",
               "frontier_context", "target_user", "core_job", "desired_outcome", "retention_reason",
               "strong_mvp", "golden_journey", "commercial_release_line", "safety_context",
               "open_questions", "gap_plan")}
    # Implementation and arbitrary member roles need the same revisitable
    # provenance as research/review roles; full bodies remain in shared context.
    common.update(research_snapshot(value, max_sources=max_sources))
    if any(token in role_name for token in ("visual", "design", "视觉", "设计", "content", "内容")):
        common["reference_patterns"] = value.get("reference_patterns") or []
        common["sources"] = (value.get("sources") or [])[:max_sources]
    elif any(token in role_name for token in ("research", "scout", "研究", "调研")):
        common["query_ledger"] = value.get("query_ledger") or []
        common["sources"] = (value.get("sources") or [])[:max_sources]
        common["reference_patterns"] = value.get("reference_patterns") or []
    elif any(token in role_name for token in ("engineer", "implement", "工程", "开发", "实现")):
        common["technical_context"] = value.get("technical_context") or []
        common["reference_patterns"] = value.get("reference_patterns") or []
    elif any(token in role_name for token in ("review", "audit", "评审", "审计")):
        common["sources"] = (value.get("sources") or [])[:max_sources]
        common["reference_patterns"] = value.get("reference_patterns") or []
        common["evidence_issues"] = evidence_issues(value)
    else:
        common["reference_patterns"] = value.get("reference_patterns") or []
        common["technical_context"] = value.get("technical_context") or []
    common["role"] = role
    return common


def history_entry(context: dict, *, reason: str = "") -> dict:
    value = context if isinstance(context, dict) else {}
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return {"revision": int(value.get("revision") or 0),
            "reason": _text(reason or value.get("change_reason"), 1000),
            "sha256": hashlib.sha256(canonical.encode()).hexdigest(),
            "updated_at": value.get("updated_at") or time.time(),
            "context": deepcopy(value)}
