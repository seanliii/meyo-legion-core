#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面向最终用户价值的独立成功审计契约。

这里只编译审计现场并校验结构化报告，不替 Harness 判断答案内容。简单任务默认跳过，
复杂任务交给独立成员审计需求满足、正确性、可用性、工艺细节、一致性与克制度。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import urlparse

_START = "<<LEGION_VERIFY>>"
_END = "<<END>>"

DIMENSIONS = (
    "requirement_fit",
    "correctness_and_evidence",
    "usability_and_interaction",
    "craft_and_detail",
    "coherence",
    "restraint_and_focus",
)

# 审计 payload 自身先做语义分层；Responses bridge 仍负责最终请求硬预算。两层职责不同：
# 这里保证“目标/验收/旧 objection/最终候选”都有席位，bridge 保证所有来源组合后绝不超窗。
_DEFAULT_AUDIT_INPUT_TOKENS = 18000
# 与最终 Responses bridge 使用同一“UTF-8 字节数即 token 上界”口径；这比常见 4 bytes/token
# 更保守，但能保证审计层给出的 input_tokens 预算传到下一层后仍成立，不发生二次整段裁剪。
_APPROX_BYTES_PER_TOKEN = 1


def _approx_tokens(value: Any) -> int:
    raw = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), default=str)
    return len(raw.encode("utf-8", errors="replace"))


def _bounded_text(value: Any, tokens: int, *, head_ratio: float = 0.45) -> tuple[str, dict | None]:
    text = str(value or "")
    if _approx_tokens(text) <= tokens:
        return text, None
    raw = text.encode("utf-8", errors="replace")
    digest = hashlib.sha256(raw).hexdigest()[:12]
    marker = f"\n…[审计材料压缩 sha256={digest} 原始字节={len(raw)}]…\n"
    budget = max(0, int(tokens) * _APPROX_BYTES_PER_TOKEN - len(marker.encode("utf-8")))
    head = int(budget * head_ratio)
    tail = budget - head
    result = (raw[:head].decode("utf-8", errors="ignore") + marker +
              (raw[-tail:].decode("utf-8", errors="ignore") if tail else ""))
    return result, {"sha256": digest, "original_bytes": len(raw), "kept_tokens": tokens}


def _bounded_list(values: Any, tokens: int) -> tuple[list, list[dict]]:
    rows = list(values or []) if isinstance(values, (list, tuple)) else [values]
    if not rows:
        return [], []
    per_item = max(128, int(tokens) // len(rows))
    bounded, omissions = [], []
    for index, item in enumerate(rows):
        if isinstance(item, str):
            text, note = _bounded_text(item, per_item, head_ratio=0.6)
            bounded.append(text)
        elif isinstance(item, dict):
            # objection 是审计契约的结构字段，不可因 evidence 很长而在整段首尾裁剪中消失。
            # 身份、结论和 revision_request 各自保留；长 findings/evidence 再用剩余额度分层压缩。
            identity = {key: item.get(key) for key in
                        ("key", "task_id", "role", "targets", "verdict", "needs_human")
                        if item.get(key) is not None}
            revision, revision_note = _bounded_text(
                item.get("revision_request") or item.get("reason") or "", max(256, per_item // 3),
                head_ratio=0.7)
            detail_budget = max(256, (per_item - max(256, per_item // 3)) // 2)
            findings, finding_notes = _bounded_list(item.get("findings") or [], detail_budget)
            evidence, evidence_notes = _bounded_list(item.get("evidence") or [], detail_budget)
            bounded.append({**identity, "revision_request": revision,
                            "findings": findings, "evidence": evidence})
            note = None
            if revision_note or finding_notes or evidence_notes:
                note = {"fields": {"revision_request": revision_note,
                                   "findings": finding_notes, "evidence": evidence_notes}}
        else:
            raw = json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str)
            text, note = _bounded_text(raw, per_item, head_ratio=0.6)
            bounded.append(json.loads(text) if note is None else {"compacted": text})
        if note:
            omissions.append({"index": index, **note})
    return bounded, omissions


def profile(plan: dict | None, candidate_count: int = 0) -> str:
    source = plan if isinstance(plan, dict) else {}
    explicit = str(source.get("quality_profile") or
                   (source.get("success_audit") or {}).get("profile") or "").lower()
    if explicit in {"answer", "analysis", "artifact", "frontier"}:
        return explicit
    contract = source.get("delivery_contract") if isinstance(source.get("delivery_contract"), dict) else {}
    required = contract.get("required_artifacts") or source.get("required_artifacts") or []
    acceptance = contract.get("acceptance") or source.get("acceptance") or []
    verification = source.get("verification") if isinstance(source.get("verification"), dict) else {}
    tasks = source.get("tasks") if isinstance(source.get("tasks"), dict) else {}
    if source.get("human_benchmark") or source.get("frontier_evidence"):
        return "frontier"
    if required:
        return "artifact"
    if verification.get("enabled") or len(tasks) > 1 or len(acceptance) > 1 or candidate_count > 1:
        return "analysis"
    return "answer"


def _benchmark_payload(plan: dict | None) -> dict:
    source = plan if isinstance(plan, dict) else {}
    benchmark = source.get("human_benchmark") if isinstance(source.get("human_benchmark"), dict) else {}
    frontier = source.get("frontier_evidence")
    if isinstance(frontier, dict):
        benchmark = {**benchmark, **frontier}
    elif isinstance(frontier, list):
        benchmark = {**benchmark, "references": frontier}
    if source.get("golden_task") and not benchmark.get("golden_task"):
        benchmark["golden_task"] = source.get("golden_task")
    # 具名世界级目录是审计 context，不是同题证据。只有调用方显式给 human_benchmark 的
    # 可打开同题产物才进入 references；目录候选单列，杜绝“品牌有名=我们已超过”的自证。
    try:
        import frontier_benchmarks
        named = frontier_benchmarks.context_for_plan(source)
        if named.get("families"):
            benchmark.setdefault("named_reference_context", named)
    except Exception:
        pass
    return benchmark


def _evidence_location(value: object) -> bool:
    text = str(value or "").strip()
    parsed = urlparse(text)
    if parsed.scheme in {"http", "https", "file"} and (parsed.netloc or parsed.path):
        return True
    return __import__("pathlib").Path(text).is_absolute() and __import__("pathlib").Path(text).is_file()


def _reference_verified(reference: dict, golden: dict) -> bool:
    """只信可重新读取的本地参考包或统一 evidence envelope，不信 plan 里的自报布尔值。"""
    verification = reference.get("verification") if isinstance(reference.get("verification"), dict) else {}
    source_path = str(verification.get("path") or "")
    parsed = urlparse(str(reference.get("url") or ""))
    if not source_path and parsed.scheme == "file":
        source_path = parsed.path
    if not source_path:
        return False
    path = __import__("pathlib").Path(source_path).expanduser()
    if not path.is_file():
        return False
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return False
    if str(verification.get("digest") or "") != digest:
        return False
    if path.name == "reference-bundle.json":
        try:
            import desktop_reference_bundles
            return bool(desktop_reference_bundles.validate(path, golden).get("ok"))
        except Exception:
            return False
    try:
        import evidence_schema
        checked = evidence_schema.read_envelope(path)
        envelope = checked.get("evidence") or {}
        if not (checked.get("ok") and envelope.get("status") == "pass"
                and envelope.get("evidence_type") == "frontier_reference"
                and str(envelope.get("evidence_id") or "") == str(verification.get("evidence_id") or "")):
            return False
        baseline = envelope.get("baseline") if isinstance(envelope.get("baseline"), dict) else {}
        bound_golden = baseline.get("golden_task") if isinstance(baseline.get("golden_task"), dict) else {}
        required = ("input", "constraints", "expected_user_outcome")
        same_task = all(str(bound_golden.get(key) or "").strip() == str(golden.get(key) or "").strip()
                        for key in required)
        assertions = envelope.get("assertions") if isinstance(envelope.get("assertions"), list) else []
        same_assertion = any(isinstance(row, dict) and row.get("id") == "same_golden_task"
                             and row.get("ok") is True for row in assertions)
        return bool(same_task and same_assertion and envelope.get("artifacts"))
    except Exception:
        return False


def frontier_contract(plan: dict | None) -> dict:
    """校验同题顶尖参照合同；没有可复核参照时永不允许声称超越。"""
    benchmark = _benchmark_payload(plan)
    golden = benchmark.get("golden_task") if isinstance(benchmark.get("golden_task"), dict) else {}
    references = [row for row in benchmark.get("references") or [] if isinstance(row, dict)]
    dimensions = [str(value) for value in benchmark.get("dimensions") or DIMENSIONS if str(value)]
    required = ("input", "constraints", "expected_user_outcome")
    blockers = []
    if not all(str(golden.get(key) or "").strip() for key in required):
        blockers.append("缺少 candidate/reference 共用的 golden task")
    if not references:
        blockers.append("缺少具名顶尖人类/团队/产品同题参照")
    for index, reference in enumerate(references):
        comparison = reference.get("comparison") if isinstance(reference.get("comparison"), dict) else {}
        identity = str(reference.get("name") or "").strip() and str(reference.get("work") or "").strip()
        same = comparison.get("same_golden_task") is True and all(
            str(comparison.get(key) or "").strip() == str(golden.get(key) or "").strip()
            for key in required)
        locations = [*(comparison.get("artifact_paths") or []), *(comparison.get("evidence_paths") or [])]
        verified = _reference_verified(reference, golden)
        if not identity or not _evidence_location(reference.get("url")):
            blockers.append(f"参照 {index + 1} 缺少具名作品或一手 URL")
        if not same:
            blockers.append(f"参照 {index + 1} 未证明使用完全相同的输入、限制和用户结果")
        if not locations or not all(_evidence_location(value) for value in locations):
            blockers.append(f"参照 {index + 1} 缺少可打开的同题产物/证据")
        if not verified:
            blockers.append(f"参照 {index + 1} 尚未由独立流程验真")
    return {"ready": not blockers, "golden_task": golden, "references": references,
            "named_reference_context": benchmark.get("named_reference_context") or {},
            "dimensions": list(dict.fromkeys(dimensions)), "blockers": list(dict.fromkeys(blockers))}


def should_run(plan: dict | None, candidates: list[dict]) -> tuple[bool, str]:
    source = plan if isinstance(plan, dict) else {}
    config = source.get("success_audit") if isinstance(source.get("success_audit"), dict) else {}
    mode = profile(source, len(candidates))
    if config.get("enabled") is False:
        return False, mode
    if not any(str(row.get("final") or "").strip() or row.get("artifacts") for row in candidates):
        return False, mode
    if config.get("enabled") is True:
        return True, mode
    # A file requirement is not a request for another model to grade the work.
    # Keep the capability opt-in; ordinary artifact verification remains separate.
    return False, mode


def author_hidden_for_prompt(candidates: list[dict], authors: set[str] | list[str]) -> dict:
    """核对真正发给评委的字段是否泄露作者；不能靠调用方直接声称“已匿名”。"""
    identifiers = sorted({str(value or "").strip().lower() for value in authors if str(value or "").strip()},
                         key=len, reverse=True)
    leaks = []
    for index, candidate in enumerate(candidates or [], 1):
        visible = [str(candidate.get("final") or "")]
        for item in [*(candidate.get("artifacts") or []), *(candidate.get("discovered_artifacts") or [])]:
            if not isinstance(item, dict):
                continue
            visible.extend(str(item.get(name) or "") for name in ("name", "path", "url") if item.get(name))
        text = "\n".join(visible).lower()
        for author in identifiers:
            if re.search(rf"(?<![a-z0-9]){re.escape(author)}(?![a-z0-9])", text):
                leaks.append({"candidate": index, "author": author})
    return {"hidden": not leaks, "leaks": leaks[:20]}


def _candidate_budgets(candidates: list[dict], total_budget: int) -> list[int]:
    """按实际答案大小共享预算；短答案释放的额度自动供长答案使用。"""
    needs = [_approx_tokens(str(candidate.get("final") or "")) for candidate in candidates]
    if not needs:
        return []
    remaining = set(range(len(needs)))
    available = max(len(needs), int(total_budget))
    allocations = [0] * len(needs)
    while remaining:
        share, remainder = divmod(available, len(remaining))
        fitted = [index for index in remaining if needs[index] <= share]
        if not fitted:
            for position, index in enumerate(sorted(remaining)):
                allocations[index] = share + (1 if position < remainder else 0)
            break
        for index in fitted:
            allocations[index] = needs[index]
            available -= needs[index]
            remaining.remove(index)
    return allocations


def _candidate_snapshot(candidate: dict, alias: str, budget: int) -> dict:
    final = str(candidate.get("final") or "")
    artifacts = []
    for item in [*(candidate.get("artifacts") or []), *(candidate.get("discovered_artifacts") or [])]:
        if not isinstance(item, dict):
            continue
        artifacts.append({name: item.get(name) for name in
                          ("name", "path", "url", "kind", "bytes", "validation", "verified")
                          if item.get(name) is not None})
    answer, omission = _bounded_text(final, budget, head_ratio=0.42)
    return {
        "candidate_id": alias,
        "answer": answer,
        "answer_truncated": omission is not None,
        "answer_omission": omission,
        "artifacts": artifacts[:50],
        "status": candidate.get("status"),
    }


def build_prompt(question: str, plan: dict, candidates: list[dict]) -> tuple[str, dict[str, str]]:
    """匿名化候选身份，避免按 Harness 名气打分；产物路径保留供审计者真实检查。"""
    usable = [row for row in candidates if str(row.get("final") or "").strip() or row.get("artifacts")]
    aliases = {f"candidate_{index + 1}": str(row.get("task_id") or row.get("key") or index)
               for index, row in enumerate(usable)}
    result_policy = str(plan.get("result_policy") or
                        (plan.get("orchestration_spec") or {}).get("result_policy") or "")
    evaluation_mode = "selection" if len(usable) > 1 and result_policy in {"parallel", "direct"} else "bundle"
    if evaluation_mode == "bundle":
        aliases["delivery_bundle"] = "__delivery__"
    configured_budget = int((plan.get("success_audit") or {}).get(
        "input_budget_tokens", _DEFAULT_AUDIT_INPUT_TOKENS)) if isinstance(
            plan.get("success_audit"), dict) else _DEFAULT_AUDIT_INPUT_TOKENS
    total_budget = max(12000, min(20000, configured_budget))
    goal_budget = max(2000, int(total_budget * 0.12))
    prior_budget = max(3000, int(total_budget * 0.18))
    candidate_budget = max(4000, total_budget - goal_budget - prior_budget - 6000)
    candidate_aliases = [alias for alias in aliases if alias.startswith("candidate_")]
    candidate_allocations = _candidate_budgets(usable, candidate_budget)
    snapshots = [_candidate_snapshot(row, alias, budget)
                 for alias, row, budget in zip(candidate_aliases, usable, candidate_allocations)]
    contract = plan.get("delivery_contract") if isinstance(plan.get("delivery_contract"), dict) else {}
    criteria = list(contract.get("acceptance") or [])
    verification = plan.get("verification") if isinstance(plan.get("verification"), dict) else {}
    criteria.extend(str(item) for item in verification.get("criteria") or [] if str(item).strip())
    quality_profile = profile(plan, len(usable))
    frontier = frontier_contract(plan) if quality_profile == "frontier" else {}
    bounded_goal, goal_omission = _bounded_text(question, goal_budget, head_ratio=0.7)
    bounded_prior, prior_omissions = _bounded_list(
        plan.get("_prior_verification_reports") or [], prior_budget)
    payload = {
        "user_goal": bounded_goal,
        "user_goal_omission": goal_omission,
        "quality_profile": quality_profile,
        "evaluation_mode": evaluation_mode,
        "acceptance": list(dict.fromkeys(criteria)),
        "required_artifacts": contract.get("required_artifacts") or [],
        "prior_verification": bounded_prior,
        "prior_verification_omissions": prior_omissions,
        "candidates": snapshots,
        "frontier_contract": frontier,
        "context_budget": {"input_tokens": total_budget, "goal_tokens": goal_budget,
                           "prior_verification_tokens": prior_budget,
                           "candidate_tokens": candidate_budget,
                           "candidate_allocations": dict(zip(candidate_aliases, candidate_allocations)),
                           "estimated_payload_tokens": 0},
    }
    payload["context_budget"]["estimated_payload_tokens"] = _approx_tokens(payload)
    example_winner = "delivery_bundle" if evaluation_mode == "bundle" else "candidate_1"
    prompt = (
        "你是最终 Success Auditor，与候选作者独立。请从真实用户能否直接成功使用成品出发审计，"
        "不要按篇幅、成员数量或文件数量奖励；简单需求以准确、直接、低摩擦为优，复杂需求才要求"
        "完整专业深度、证据、交互细节和适当丰富度。请读取能访问的真实产物并核对验收标准；"
        "无法核实就明确标记，不得猜测。候选已匿名，禁止根据作者身份偏好。"
        "先复核 prior_verification 中旧 objection 是否已在最终成品中真正解决，不能用新分数掩盖旧缺陷。\n"
        "六个维度均给 0-100：requirement_fit、correctness_and_evidence、"
        "usability_and_interaction、craft_and_detail、coherence、restraint_and_focus。"
        "evaluation_mode=selection 时从候选中选 winner；evaluation_mode=bundle 时把全部互补成果视为一个"
        "交付，winner 必须写 delivery_bundle，不能拿某个分工成员冒充整单成果。\n"
        "quality_profile=frontier 时，必须在同一 golden task 下逐维比较经验证的 reference；"
        "frontier_contract.ready=false 时只能返回 blocked/unverified，绝不能自封超越。"
        "先给一句人可读结论，末尾严格输出："
        "<<LEGION_VERIFY>>{\"verdict\":\"pass|revise|fail|blocked\","
        f'"winner":"{example_winner}","scores":{{"{example_winner}":{{"overall":0,'
        "\"requirement_fit\":0,\"correctness_and_evidence\":0,\"usability_and_interaction\":0,"
        "\"craft_and_detail\":0,\"coherence\":0,\"restraint_and_focus\":0}},"
        "\"author_hidden\":true,\"reference_scores\":{\"requirement_fit\":0,\"correctness_and_evidence\":0,"
        "\"usability_and_interaction\":0,\"craft_and_detail\":0,\"coherence\":0,\"restraint_and_focus\":0},"
        "\"frontier_preference\":\"candidate|reference|tie\","
        "\"findings\":[],\"evidence\":[],\"unmet_requirements\":[],"
        "\"needs_human\":false,\"reason\":\"...\",\"revision_request\":\"...\"}<<END>>。\n"
        "审计现场：\n" + json.dumps(payload, ensure_ascii=False)
    )
    return prompt, aliases


def parse_report(final: str, aliases: dict[str, str], *, reviewer: str = "",
                 author_hidden: bool = False) -> tuple[str, dict]:
    from legion_contracts import _last_verification_frame

    text = str(final or "")
    frame = _last_verification_frame(text)
    if frame is None:
        return text, normalize_report({}, aliases, reviewer=reviewer, author_hidden=author_hidden)
    start, end, value = frame
    cleaned = (text[:start] + text[end:]).strip()
    return cleaned, normalize_report(value, aliases, reviewer=reviewer, author_hidden=author_hidden)


def normalize_report(report: Any, aliases: dict[str, str], *, reviewer: str = "",
                     author_hidden: bool = False) -> dict:
    value = dict(report) if isinstance(report, dict) else {}
    verdict = str(value.get("verdict") or "unverified").lower()
    if verdict not in {"pass", "revise", "fail", "blocked", "unverified"}:
        verdict = "unverified"
    raw_scores = value.get("scores") if isinstance(value.get("scores"), dict) else {}
    scores: dict[str, dict] = {}
    for alias, row in raw_scores.items():
        if alias not in aliases or not isinstance(row, dict):
            continue
        normalized = {}
        for name in (*DIMENSIONS, "overall"):
            try:
                normalized[name] = max(0.0, min(100.0, float(row.get(name))))
            except (TypeError, ValueError):
                continue
        scores[aliases[alias]] = normalized
    winner_alias = str(value.get("winner") or "")
    # Bundle audits assess complementary contributions as one delivery. A
    # candidate alias is still useful for scores and findings, but cannot
    # identify a winner for that delivery. Never fabricate a bundle choice.
    bundle_scope = aliases.get("delivery_bundle") == "__delivery__"
    scope_conflict = bool(bundle_scope and winner_alias and winner_alias != "delivery_bundle")
    if bundle_scope and winner_alias != "delivery_bundle":
        winner_alias = ""
    elif not winner_alias and len(aliases) == 1:
        winner_alias = next(iter(aliases))
    winner = aliases.get(winner_alias, "")
    winning_scores = scores.get(winner) or {}
    missing_dimensions = [name for name in (*DIMENSIONS, "overall") if name not in winning_scores]
    if verdict == "pass" and (not winner or missing_dimensions):
        verdict = "unverified"
    def strings(name: str) -> list[str]:
        raw = value.get(name) or []
        if isinstance(raw, str):
            raw = [raw]
        return [str(item)[:1000] for item in raw if str(item).strip()][:50]
    reference_scores = {}
    for name in DIMENSIONS:
        try:
            if name in (value.get("reference_scores") or {}):
                reference_scores[name] = max(0.0, min(100.0, float(value["reference_scores"][name])))
        except (TypeError, ValueError):
            continue
    preference = str(value.get("frontier_preference") or "")
    if preference not in {"candidate", "reference", "tie"}:
        preference = ""
    return {
        "enabled": True,
        "reviewer": str(reviewer),
        "verdict": verdict,
        "profile": str(value.get("profile") or ""),
        "evaluated_at": "post_delivery",
        "winner": winner,
        **({"winner_rejected": {"alias": str(value.get("winner") or "")[:128],
                                "reason": "bundle_requires_delivery_bundle"}}
           if scope_conflict else {}),
        "scores": scores,
        "findings": strings("findings"),
        "evidence": strings("evidence"),
        "unmet_requirements": strings("unmet_requirements"),
        "missing_dimensions": missing_dimensions,
        "author_hidden": bool(author_hidden),
        "reference_scores": reference_scores,
        "frontier_preference": preference,
        "needs_human": bool(value.get("needs_human")),
        "reason": str(value.get("reason") or "")[:2000],
        "revision_request": str(value.get("revision_request") or "")[:4000],
    }


def frontier_verdict(plan: dict | None, reports: list[dict]) -> dict:
    """以两名独立盲评和确定性规则判定窄面胜出；模型不能自行宣布“顶尖”。"""
    contract = frontier_contract(plan)
    valid = []
    for report in reports or []:
        winner = str(report.get("winner") or "")
        candidate = (report.get("scores") or {}).get(winner) if winner else None
        reference = report.get("reference_scores") if isinstance(report.get("reference_scores"), dict) else {}
        complete = isinstance(candidate, dict) and all(name in candidate and name in reference for name in DIMENSIONS)
        if (report.get("author_hidden") is True and str(report.get("reviewer") or "") and complete
                and report.get("verdict") == "pass"
                and report.get("frontier_preference") in {"candidate", "reference", "tie"}):
            valid.append(report)
    judges = {str(report.get("reviewer") or "") for report in valid}
    reviewer_models = {str(report.get("reviewer_model") or "") for report in valid
                       if str(report.get("reviewer_model") or "")}
    blockers = list(contract.get("blockers") or [])
    if len(judges) < 2:
        blockers.append("至少需要两个隐藏作者身份、结论为 pass 的独立评委")
    if len(reviewer_models) < 2:
        blockers.append("两个独立评委必须使用不同的实际模型")
    chosen = {str(report.get("winner") or "") for report in valid}
    decisive = (len(judges) >= 2 and len(chosen) == 1
                and all(report.get("frontier_preference") == "candidate" for report in valid))
    if len(judges) >= 2 and len(chosen) != 1:
        blockers.append("独立评委未对同一候选或整单交付形成一致结论")
    correctness_ok = bool(valid) and all(
        float((report.get("scores") or {}).get(report.get("winner"), {}).get("correctness_and_evidence", -1)) >=
        float((report.get("reference_scores") or {}).get("correctness_and_evidence", 101))
        for report in valid)
    winning_dimensions = set(DIMENSIONS)
    for report in valid:
        candidate = (report.get("scores") or {}).get(report.get("winner"), {})
        reference = report.get("reference_scores") or {}
        winning_dimensions &= {name for name in DIMENSIONS
                               if float(candidate.get(name, -1)) > float(reference.get(name, 101))}
    if not correctness_ok:
        blockers.append("正确性与证据不得低于顶尖参照")
    if len(winning_dimensions) < 2:
        blockers.append("至少两个质量维度须经所有独立评委一致判定胜出")
    proven = bool(contract.get("ready") and decisive and len(reviewer_models) >= 2
                  and correctness_ok and len(winning_dimensions) >= 2)
    below = bool(contract.get("ready") and valid and (not correctness_ok or
                 all(report.get("frontier_preference") == "reference" for report in valid)))
    verdict = "VERIFIED_NARROW_WIN" if proven else "BELOW_FRONTIER" if below else "PARITY_OR_INCONCLUSIVE"
    return {"verdict": verdict, "proven": proven, "contract": contract,
            "judge_count": len(judges), "reviewer_models": sorted(reviewer_models),
            "winning_dimensions": sorted(winning_dimensions),
            "blockers": list(dict.fromkeys(blockers))}


def score(report: dict | None) -> float | None:
    value = report if isinstance(report, dict) else {}
    winner = str(value.get("winner") or "")
    row = (value.get("scores") or {}).get(winner) if winner else None
    if isinstance(row, dict) and row.get("overall") is not None:
        try:
            return max(0.0, min(100.0, float(row["overall"])))
        except (TypeError, ValueError):
            return None
    return None
