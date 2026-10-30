#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""军团跨进程契约：任务事实、失败信封、权限与 adapter 能力。

这些类型只描述现场，不替 harness 决定怎么解题。都能直接 JSON 序列化，供 server、
legion_core、worker 和各 adapter 共用，避免继续从长文本里猜状态。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
import hashlib
import json
from typing import Any


@dataclass
class ArtifactRef:
    artifact_id: str
    path: str
    sha256: str = ""
    mime: str = "application/octet-stream"
    bytes: int = 0
    name: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ArtifactRequirement:
    """一项可机器核验的交付要求；内容质量仍交给独立验证者。"""

    kind: str = "file"
    min_count: int = 1
    formats: list[str] = field(default_factory=list)
    description: str = ""
    owner_task: str = ""
    validation: str = "exists_nonempty"
    optional: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_value(cls, value: Any) -> "ArtifactRequirement | None":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            value = {"kind": value}
        if not isinstance(value, dict):
            return None
        try:
            count = max(0, int(value.get("min_count", value.get("count", 1))))
        except (TypeError, ValueError):
            count = 1
        formats = value.get("formats", value.get("extensions", [])) or []
        if isinstance(formats, str):
            formats = [formats]
        return cls(
            kind=str(value.get("kind") or value.get("type") or "file").strip().lower(),
            min_count=count,
            formats=[str(item).strip().lower().lstrip(".") for item in formats if str(item).strip()],
            description=str(value.get("description") or value.get("label") or "")[:500],
            owner_task=str(value.get("owner_task") or value.get("task_id") or "")[:120],
            validation=str(value.get("validation") or "exists_nonempty")[:120],
            optional=bool(value.get("optional")),
        )


@dataclass
class DeliveryContract:
    """用户目标编译出的结果契约；没有显式要求时不凭关键词猜产物。"""

    required_artifacts: list[ArtifactRequirement] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    require_final: bool = True
    integrity_mode: str = "development"
    source: str = "plan"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_value(cls, value: Any) -> "DeliveryContract":
        if isinstance(value, cls):
            return value
        value = value if isinstance(value, dict) else {}
        raw = value.get("required_artifacts") or value.get("artifacts") or []
        if isinstance(raw, (str, dict)):
            raw = [raw]
        requirements = [item for item in (ArtifactRequirement.from_value(entry) for entry in raw)
                        if item is not None]
        acceptance = value.get("acceptance") or value.get("acceptance_criteria") or []
        if isinstance(acceptance, str):
            acceptance = [acceptance]
        integrity = str(value.get("integrity_mode") or "development").strip().lower()
        if integrity not in {"development", "demo", "benchmark"}:
            integrity = "development"
        return cls(
            required_artifacts=requirements,
            acceptance=[str(item)[:1000] for item in acceptance if str(item).strip()],
            require_final=bool(value.get("require_final", True)),
            integrity_mode=integrity,
            source=str(value.get("source") or "plan")[:120],
        )

    @classmethod
    def from_plan(cls, plan: Any) -> "DeliveryContract":
        plan = plan if isinstance(plan, dict) else {}
        raw = plan.get("delivery_contract")
        if not isinstance(raw, dict):
            raw = {}
        raw = dict(raw)
        if "required_artifacts" not in raw and plan.get("required_artifacts") is not None:
            raw["required_artifacts"] = plan.get("required_artifacts")
        if "acceptance" not in raw and plan.get("acceptance") is not None:
            raw["acceptance"] = plan.get("acceptance")
        return cls.from_value(raw)


@dataclass
class VerificationSpec:
    """独立验证是 DAG policy，不与具体 harness 绑定。"""

    enabled: bool = False
    roles: list[str] = field(default_factory=list)
    reviewers: list[dict] = field(default_factory=list)
    criteria: list[str] = field(default_factory=list)
    max_rounds: int = 1
    require_independent: bool = True
    revision_owner: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_value(cls, value: Any) -> "VerificationSpec":
        if isinstance(value, cls):
            return value
        value = value if isinstance(value, dict) else {}
        roles = value.get("roles") or []
        criteria = value.get("criteria") or []
        reviewers = value.get("reviewers") or []
        if isinstance(roles, str):
            roles = [roles]
        if isinstance(reviewers, dict):
            reviewers = [reviewers]
        if isinstance(criteria, str):
            criteria = [criteria]
        try:
            rounds = min(3, max(0, int(value.get("max_rounds", 1))))
        except (TypeError, ValueError):
            rounds = 1
        return cls(
            enabled=bool(value.get("enabled")),
            roles=[str(item)[:80] for item in roles if str(item).strip()][:4],
            reviewers=[dict(item) for item in reviewers if isinstance(item, dict)][:4],
            criteria=[str(item)[:1000] for item in criteria if str(item).strip()][:20],
            max_rounds=rounds,
            require_independent=bool(value.get("require_independent", True)),
            revision_owner=str(value.get("revision_owner") or "")[:120],
        )


@dataclass
class OrchestrationSpec:
    """与成员描述解耦的协作规格；各种模式只是同一 DAG 的不同 policy。"""

    pattern: str = "single"
    result_policy: str = "direct"
    adaptive: bool = True
    auto_resume: bool = True
    min_agents: int = 1
    max_agents: int = 1
    verification: VerificationSpec = field(default_factory=VerificationSpec)
    delivery_contract: DeliveryContract = field(default_factory=DeliveryContract)
    # Retain request choices, not resolved credentials or a new recovery policy.
    team_policy: dict[str, Any] = field(default_factory=dict)
    model_overrides: dict[str, str] = field(default_factory=dict)
    _continuous_coordination: bool | None = None
    _planner_generated: bool | None = None

    def to_dict(self) -> dict:
        value = asdict(self)
        if not value["team_policy"]:
            value.pop("team_policy")
        if not value["model_overrides"]:
            value.pop("model_overrides")
        for name in ("_continuous_coordination", "_planner_generated"):
            if value[name] is None:
                value.pop(name)
        return value

    @classmethod
    def from_plan(cls, plan: Any, mode: str = "pk") -> "OrchestrationSpec":
        plan = plan if isinstance(plan, dict) else {}
        tasks = plan.get("tasks") if isinstance(plan.get("tasks"), dict) else {}
        pattern = str(plan.get("pattern") or mode or ("single" if len(tasks) <= 1 else "pk")).lower()
        if pattern not in {"single", "pk", "relay", "orchestrate", "iterative", "distributed", "review", "proof"}:
            pattern = "single" if len(tasks) <= 1 else "pk"
        default_policy = {"relay": "last", "orchestrate": "synthesis",
                          "review": "reviewed", "proof": "synthesis"}.get(
            pattern, "direct" if len(tasks) <= 1 else "parallel")
        result_policy = str(plan.get("result_policy") or default_policy).lower()
        if result_policy not in {"direct", "parallel", "last", "synthesis", "reviewed"}:
            result_policy = default_policy
        team_policy = plan.get("team_policy") if isinstance(plan.get("team_policy"), dict) else {}
        adaptive = bool(team_policy.get("adaptive", True))
        default_max = max(1, len(tasks) + (3 if adaptive else 0))
        try:
            minimum = max(1, int(team_policy.get("min_agents", 1)))
            maximum = max(minimum, min(32, int(team_policy.get("max_agents", default_max))))
        except (TypeError, ValueError):
            minimum, maximum = 1, default_max
        return cls(
            pattern=pattern,
            result_policy=result_policy,
            adaptive=adaptive,
            auto_resume=bool(team_policy.get("auto_resume", True)),
            min_agents=minimum,
            max_agents=maximum,
            verification=VerificationSpec.from_value(plan.get("verification")),
            delivery_contract=DeliveryContract.from_plan(plan),
            team_policy=deepcopy(team_policy),
            model_overrides=deepcopy(plan.get("model_overrides"))
            if isinstance(plan.get("model_overrides"), dict) else {},
            _continuous_coordination=plan.get("_continuous_coordination"),
            _planner_generated=plan.get("_planner_generated"),
        )


def compile_orchestration_plan(plan: Any, mode: str = "pk") -> tuple[dict, OrchestrationSpec]:
    """把兼容输入编译成唯一 typed DAG；不选择成员，也不改写任务内容。"""
    source = dict(plan) if isinstance(plan, dict) else {}
    try:
        import frontier_benchmarks
        source = frontier_benchmarks.augment_plan(source)
    except Exception:
        # 研究上下文不可用时保留原计划，不擅自补业务工序或阻断执行。
        pass
    if isinstance(source.get("orchestration_spec"), dict):
        spec_input = dict(source["orchestration_spec"])
        stored_policy = spec_input.get("team_policy")
        if "team_policy" in source and isinstance(stored_policy, dict):
            current_policy = source["team_policy"] if isinstance(source["team_policy"], dict) else {}
            for field, stored_choice in stored_policy.items():
                if current_policy.get(field) is None and stored_choice is not None:
                    current_policy = {**current_policy, field: deepcopy(stored_choice)}
            source["team_policy"] = current_policy
        spec_input.update({key: source[key] for key in ("tasks", "delivery_contract", "verification",
                                                        "team_policy", "model_overrides", "result_policy", "pattern",
                                                        "experience_intent", "golden_task", "north_star",
                                                        "critical_dimensions", "claim_ceiling", "quality_protocol",
                                                        "frontier_research_contract", "quality_profile")
                           if key in source})
        for name in ("_continuous_coordination", "_planner_generated"):
            if source.get(name) is not None:
                spec_input[name] = source[name]
        source = {**source, **spec_input}
    tasks = source.get("tasks") if isinstance(source.get("tasks"), dict) else {}
    compiled_tasks = {}
    for key, value in tasks.items():
        task_id = str(key)
        if isinstance(value, dict):
            node = dict(value)
        else:
            node = {"task": str(value or "回答问题"), "depends_on": []}
        node.setdefault("task_id", task_id)
        # 兼容旧 `{harness_key: task}`；新规格显式写 assigned_harness，逻辑节点身份不再随换人变化。
        assigned = str(node.get("assigned_harness") or node.get("assign_to") or
                       node.get("harness") or node.get("agent") or "").strip()
        if assigned:
            node["assigned_harness"] = assigned
        compiled_tasks[task_id] = node
    pipeline = [str(key) for key in (source.get("pipeline") or []) if str(key) in compiled_tasks]
    if mode == "relay":
        if len(pipeline) < 2:
            pipeline = list(compiled_tasks)
        for index, key in enumerate(pipeline):
            node = compiled_tasks[key]
            dependencies = node.get("depends_on", node.get("dependencies", [])) or []
            if isinstance(dependencies, str):
                dependencies = [dependencies]
            if index:
                dependencies = [pipeline[index - 1], *dependencies]
            node["depends_on"] = list(dict.fromkeys(str(item) for item in dependencies if str(item) != key))
            node.setdefault("dependency_policy", "all_succeeded")
    delivery = DeliveryContract.from_plan(source).to_dict()
    verification = VerificationSpec.from_value(source.get("verification"))
    base_task_ids = list(compiled_tasks)
    if verification.enabled:
        claimed_reviewers: set[str] = set()
        for index, reviewer in enumerate(verification.reviewers):
            reviewer_harness = str(reviewer.get("harness") or reviewer.get("assigned_harness") or
                                   reviewer.get("key") or "").strip()
            reviewer_key = str(reviewer.get("task_id") or f"verify_{index + 1}_{reviewer_harness or 'auto'}")
            role = str(reviewer.get("role") or "critic").strip().lower()
            while reviewer_key in compiled_tasks:
                existing_role = str(compiled_tasks[reviewer_key].get("verification_role") or "").strip().lower()
                if role and existing_role == role and reviewer_key not in claimed_reviewers:
                    break
                reviewer_key += "_review"
            claimed_reviewers.add(reviewer_key)
            if reviewer_key in compiled_tasks:
                # Resume/recompile already has this declaration's reviewer.
                # Keep its current task, targets, criteria and binding; one
                # existing node cannot satisfy two reviewer declarations.
                continue
            raw_targets = reviewer.get("targets")
            targets = raw_targets or base_task_ids
            if isinstance(targets, str):
                targets = [targets]
            resolved_targets, unresolved_targets = [], []
            for item in targets:
                target = str(item).strip()
                if target in base_task_ids:
                    resolved_targets.append(target)
                    continue
                matching_tasks = [
                    task_id for task_id in base_task_ids
                    if str(compiled_tasks[task_id].get("assigned_harness")
                           or compiled_tasks[task_id].get("assign_to")
                           or compiled_tasks[task_id].get("harness")
                           or compiled_tasks[task_id].get("agent") or "").strip() == target
                ]
                if matching_tasks:
                    resolved_targets.extend(matching_tasks)
                elif target:
                    unresolved_targets.append(target)
            # 旧规划会把 Harness key 写进 targets；一个目标都解析不出来时宁可等待所有基础任务，
            # 也不能把独立审查静默编译成首波根节点，在上游产物出现前先交一份 blocked 空审计。
            targets = list(dict.fromkeys(resolved_targets))
            if raw_targets and not targets:
                targets = list(base_task_ids)
            criteria = verification.criteria
            compiled_tasks[reviewer_key] = {
                "task": str(reviewer.get("task") or
                            f"独立执行 {role} 验证；依据验收标准检查上游交付，报告可复现缺陷、证据与修复建议。"
                            "先给人可读的审查结论，末尾再追加一行 "
                            '<<LEGION_VERIFY>>{"verdict":"pass|revise|fail","findings":[],"evidence":[],"revision_request":""}<<END>>'),
                "depends_on": targets,
                "dependency_policy": "all_finished",
                "assigned_harness": reviewer_harness,
                "verification_role": role,
                "verification_criteria": criteria,
                "unresolved_verification_targets": unresolved_targets,
            }
    for key, node in compiled_tasks.items():
        canonical = json.dumps({"task_id": key,
                                "task": node.get("task") or node.get("prompt") or node.get("role"),
                                "depends_on": node.get("depends_on") or node.get("dependencies") or [],
                                "delivery_contract": delivery}, ensure_ascii=False, sort_keys=True)
        input_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        node.setdefault("input_hash", input_hash)
        node.setdefault("idempotency_key", f"{key}:{input_hash[:24]}")
    compiled = {**source, "tasks": compiled_tasks, "pipeline": pipeline}
    spec = OrchestrationSpec.from_plan(compiled, mode)
    if len(compiled_tasks) <= 1:
        spec.pattern, spec.result_policy = "single", "direct"
        if not spec.adaptive:
            spec.max_agents = 1
    try:
        import collaboration_runtime
        compiled, _ = collaboration_runtime.bind_plan(compiled, set(), allow_unbound=True)
        compiled_tasks = dict(compiled.get("tasks") or compiled_tasks)
    except Exception:
        pass
    compiled["orchestration_spec"] = spec.to_dict()
    compiled["delivery_contract"] = spec.delivery_contract.to_dict()
    compiled["pattern"] = spec.pattern
    compiled["result_policy"] = spec.result_policy
    compiled["tasks"] = compiled_tasks
    return compiled, spec


def result_is_delivered(result: dict | None) -> bool:
    """Shared existing delivery-presence rule, not a quality assessment."""
    if not result or result.get("status") != "done":
        return False
    final = str(result.get("final") or "").strip()
    return bool(final and final.upper() not in {"UNKNOWN", "NONE", "N/A", "NULL", "未完成", "无"})


def dependencies_satisfied(deps: list[str], policy: str, results: dict, *, delivered=None):
    """Same runtime dependency semantics for dispatch and persisted recovery."""
    if not deps:
        return True, []
    from error_envelope import requires_native_settlement
    delivered = delivered or result_is_delivered
    unsettled = {dep for dep in deps if requires_native_settlement(results.get(dep))}
    ready = {dep for dep in deps if dep not in unsettled and delivered(results.get(dep))}
    missing = [dep for dep in deps if dep not in ready]
    if policy == "all_finished":
        terminal = {"done", "error", "timeout", "cancelled", "failed"}
        unfinished = [dep for dep in deps if dep in unsettled
                      or str((results.get(dep) or {}).get("status") or "") not in terminal]
        return not unfinished, unfinished
    if policy == "any_succeeded":
        return bool(ready), missing if not ready else []
    return not missing, missing


def retained_resume_results(state: dict, *, include_completed: bool = False) -> dict:
    """Only trusted persisted results, never an HTTP plan's caller-supplied result."""
    from error_envelope import requires_output_recovery_pause
    retained = {}
    for task_id, task in (state.get("tasks") or {}).items():
        result = task.get("result")
        if include_completed and task.get("status") == "done":
            # The preview is not the original result. Reuse the existing reader
            # and leave an unreadable original unretained, never re-execute it.
            import team_state
            result = team_state.resolve_result(str(state.get("team_id") or ""), task_id)
        if requires_output_recovery_pause(result) or (
                include_completed and result_is_delivered(result)):
            harness = str(task.get("assigned_harness") or result.get("harness") or task_id)
            retained[task_id] = {**deepcopy(result), "key": task_id, "task_id": task_id,
                                 "harness": harness, "assigned_harness": harness}
    return retained


def resume_facts_from_state(state: dict, *, explicit: bool = False,
                           lease_owner: str = "", now: float | None = None) -> dict:
    """Read existing recovery facts; no model decision, fake task or new runner.

    Automatic coordination recovery needs evidence recorded after the last
    decision. Claiming consumes its fingerprint, so a failed recovery/lease
    timestamp cannot itself authorize another identical automatic request.
    """
    import time
    import project_context
    import team_state
    from error_envelope import requires_execution_pause

    team_id = str(state.get("team_id") or "")
    packet = project_context.load(team_id)
    uid, sid = str(state.get("user_id") or ""), str(state.get("conversation_id") or team_id)
    project_context.require_owner(packet, uid, sid)
    if packet and packet.get("conversation_id") and packet["conversation_id"] != sid:
        raise PermissionError("persisted recovery context belongs to another conversation")
    spec = state.get("orchestration_spec") or {}
    policy = spec.get("team_policy") or {}
    mode = str(packet.get("mode") or spec.get("pattern") or "pk")
    mode = mode if mode in ("pk", "relay", "orchestrate") else "pk"
    captain = str(state.get("captain") or packet.get("captain") or "")
    continuous = policy.get("continuous_coordination")
    persistent = bool(captain and mode == "orchestrate" and continuous is not False)
    decision = next((row for row in reversed(packet.get("decisions") or [])
                     if isinstance(row, dict) and row.get("kind") == "captain_coordination"), {})
    unresolved = bool(persistent and (
        (decision.get("error") and decision.get("ok") is False)
        or decision.get("action") == "continue"))
    continuation = team_state.pending_continuation(state)
    facts = {"eligible": False, "reason": "no recoverable tasks", "mode": mode,
             "persistent": persistent, "coordination_pending": unresolved,
             "continuation": continuation,
             "project_context": packet, "decision_sequence": decision.get("sequence")}
    current = time.time() if now is None else now
    lease = state.get("resume_lease") or {}
    admitted = bool(lease_owner and lease.get("owner") == lease_owner)
    if team_state._resume_lease_active(state, current) and not admitted:
        return {**facts, "reason": "active_resume_lease"}
    if team_state._cancel_request_for(state, "commander", {"assigned_harness": captain}):
        return {**facts, "reason": "cancelled"}
    if any(task.get("active_attempt") is not None
           and float(task.get("lease_expires_at") or 0) > current
           and team_state._owner_process_alive(str(task.get("lease_owner") or ""))
           for task in (state.get("tasks") or {}).values()):
        return {**facts, "reason": "active_task_lease"}

    terminal = team_state._resume_terminal_result(state)
    if terminal.get("_resume_receipt_unavailable"):
        return {**facts, "reason": "terminal_resume_receipt_unavailable"}
    if terminal.get("cancelled"):
        return {**facts, "reason": "cancelled"}
    # Coordination pauses are not business nodes. Read the actual terminal
    # receipt, not its missing-action label or a model's natural-language claim.
    last_control = terminal.get("coordination") or {}
    if not last_control:
        last_control = next(iter(reversed(terminal.get("coordination_history") or [])), {})
    control_results = []
    if int(last_control.get("sequence") or 0) >= int(decision.get("sequence") or 0):
        control_results.append(last_control.get("result") or {})
    reference = decision.get("execution_result_ref") or {}
    request_id = str(reference.get("qhash") or "")
    allowed_ids = {f"{team_id}_coordinate_{decision.get('sequence')}",
                   f"{team_id}_coordinate_{decision.get('sequence')}_format_repair"}
    if request_id in allowed_ids:
        import task_memory
        memory = task_memory.load(request_id, str(decision.get("captain") or captain))
        if memory.get("team_id") == request_id:
            control_results.append({"status": reference.get("status"), **{
                name: memory[name] for name in ("native_facts", "error_envelope") if name in memory}})
    def paused(row):
        envelope = row.get("error_envelope") or {} if isinstance(row, dict) else {}
        budget_stop = (isinstance(envelope, dict) and envelope.get("retryable") is False
                       and envelope.get("category") in ("runtime_budget", "native_iteration_budget")
                       and row.get("status") not in ("done", "ok", "success", "completed"))
        return requires_execution_pause(row) or budget_stop

    if any(paused(row) for row in control_results):
        return {**facts, "reason": "coordination_execution_paused"}
    # A newly admitted coordinator must not bypass an existing request pause.
    if (persistent or continuation) and any(
            paused(task.get("result")) or (task.get("result") or {}).get("status") == "cancelled"
            for task in (state.get("tasks") or {}).values()):
        return {**facts, "reason": "execution_paused"}
    if continuation:
        # An old browser card may submit again after a newer turn paused.
        # Its old successful result is not permission to bypass the newer stop.
        for message in reversed((state.get("mailboxes") or {}).get("commander", [])):
            request = message.get("continuation") or {}
            child = team_state.load(str(request.get("team_id") or "")) if request else None
            if not child:
                continue
            project_context.require_owner(child, uid, sid)
            if (team_state._cancel_request_for(child, "commander", {})
                    or any(paused(task.get("result"))
                           for task in (child.get("tasks") or {}).values())
                    or paused(team_state._resume_terminal_result(child))):
                return {**facts, "reason": "continuation_execution_paused"}
            break

    pending = team_state.recoverable_tasks(team_id)
    if not pending and not unresolved and not continuation:
        return facts
    reports = (packet.get("quality_context") or {}).get("member_reports") or {}
    evidence = {
        "tasks": {key: {name: task.get(name) for name in
                       ("status", "attempt_seq", "finished_at", "result")}
                  for key, task in (state.get("tasks") or {}).items()},
        "member_reports": sorted(reports),
        **{name: packet.get(name) for name in
           ("completed", "attempts", "artifacts", "journey_observations", "gap_plan")},
    }
    fingerprint = hashlib.sha256(json.dumps(
        evidence, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    facts["evidence_fingerprint"] = fingerprint
    if unresolved and not admitted and not continuation:
        if int(state.get("coordination_resume_attempts") or 0) >= team_state.resume_attempt_cap():
            return {**facts, "reason": "coordination_resume_budget_exhausted"}
        if not explicit and not pending:
            times = [float(task.get("finished_at") or 0)
                     for task in (state.get("tasks") or {}).values()]
            times += [float(row.get("recorded_at") or 0)
                      for row in [*(packet.get("attempts") or []), *reports.values()]
                      if isinstance(row, dict)]
            if (fingerprint == state.get("coordination_resume_fingerprint")
                    or max(times, default=0) <= float(decision.get("recorded_at") or 0)):
                return {**facts, "reason": "no_new_coordination_facts"}
    return {**facts, "eligible": True, "reason": (
        "user_feedback" if continuation else "coordination_pending" if unresolved else "tasks_pending")}


def paused_output_resume_tasks(state: dict) -> set[str]:
    from error_envelope import paused_execution_tasks, requires_output_recovery_pause
    tasks = state.get("tasks") or {}
    return paused_execution_tasks(
        {key: task.get("result") or {} for key, task in tasks.items()},
        {key: list(task.get("dependencies") or []) for key, task in tasks.items()},
        {key: str(task.get("dependency_policy") or "all_succeeded") for key, task in tasks.items()},
        pause=requires_output_recovery_pause,
    )


def _resumable_status(task: dict) -> bool:
    """恢复集判定(与 team_state.recoverable_tasks 同语义):

    pending/interrupted/running 可恢复;error 任务在恢复预算内也可恢复
    (根修 web_08e5c897fc9c 多家失败后接力未转:attempt1 常是 server 崩溃非模型过错,
    attempt2 一错就被判死违背「失败→带完整 context 转交另一家接力」契约)。
    预算用尽的 error 保持终态。done/cancelled 不恢复。
    blocked 恢复(2026-09-07单1重放实锤):blocked=等上游的暂停态非终态,上游恢复后
    下游必须跟着活,否则主链永远留 blocked,原对话无法续跑世界级编排。
    """
    from error_envelope import requires_output_recovery_pause
    if requires_output_recovery_pause(task.get("result")):
        return False
    status = str(task.get("status") or "pending")
    if status in {"pending", "interrupted", "running", "blocked", "queued"}:
        return True
    if status == "error":
        try:
            import team_state as _ts
            return _ts.attempt_count(task) < _ts.resume_attempt_cap()
        except Exception:
            seq = int(task.get("attempt_seq") or 0) or len(task.get("attempt_history") or [])
            return seq < 4
    return False


def resume_plan_from_state(state: Any, *, include_completed: bool = False) -> tuple[dict, str]:
    """从持久 TeamState 重建只含未完成节点的计划；已完成依赖已在状态与产物引用中保留。"""
    state = state if isinstance(state, dict) else {}
    stored_tasks = state.get("tasks") if isinstance(state.get("tasks"), dict) else {}
    pending = {key for key, task in stored_tasks.items() if _resumable_status(task)}
    held = retained_resume_results(state, include_completed=include_completed)
    if include_completed:
        pending |= set(held)
    elif held:
        # A mixed team retains the full relevant dependency graph. The server
        # supplies held results privately; omission must not turn "blocked" into
        # "no prerequisites". All-held teams have no automatically runnable work.
        if pending - paused_output_resume_tasks(state):
            pending |= set(held)
        else:
            pending = set()
    tasks = {}
    completed_context = []
    for completed_id, completed_task in stored_tasks.items():
        if completed_id in pending or str(completed_task.get("status") or "") != "done":
            continue
        result = dict(completed_task.get("result") or {})
        completed_context.append({"task_id": completed_id,
                                  "harness": completed_task.get("assigned_harness") or completed_id,
                                  "final": result.get("final") or result.get("final_preview") or "",
                                  "final_ref": result.get("final_ref"),
                                  "artifacts": result.get("artifacts") or []})
    for key in stored_tasks:
        if key not in pending:
            continue
        task = stored_tasks[key]
        node = {
            "task": str(task.get("text") or "回答问题"),
            "depends_on": [dep for dep in (task.get("dependencies") or []) if dep in pending],
            "dependency_policy": str(task.get("dependency_policy") or "all_succeeded"),
            "input_hash": str(task.get("input_hash") or ""),
            "idempotency_key": str(task.get("idempotency_key") or ""),
            "capability_requirements": list(task.get("capability_requirements") or []),
            "native_capabilities": dict(task.get("native_capabilities") or {}),
            "recovery_context": dict(task.get("recovery_context") or {}),
            "task_kind": str(task.get("task_kind") or "delivery"),
            "verification_role": str(task.get("verification_role") or ""),
            "verification_targets": list(task.get("verification_targets") or []),
            "verification_criteria": (
                [task["verification_criteria"]]
                if isinstance(task.get("verification_criteria"), str) and task["verification_criteria"]
                else list(task.get("verification_criteria") or [])),
            "supporting_context": bool(task.get("supporting_context")),
            "excluded_harnesses": list(task.get("excluded_harnesses") or []),
        }
        previous_harness = str(task.get("assigned_harness") or task.get("agent") or "")
        if task.get("assignment_locked") or key in held:
            node.update({"assigned_harness": previous_harness, "assignment_locked": True})
        else:
            # 失败接力防线(根修):不止排除原执行家——result_history 里每个接棒又没交付的
            # 家都要进 excluded,否则恢复后同一失败家被反复选中重蹈覆辙
            # (web_08e5c897fc9c:openclaw/nanobot 接棒失败后都没进 excluded)。
            _failed = list(task.get("excluded_harnesses") or [])
            for _rh in (task.get("result_history") or []):
                _h = str(_rh.get("assigned_harness") or "")
                if _h:
                    _failed.append(_h)
            never_started = (
                str(task.get("status") or "pending") in {"pending", "queued", "blocked"}
                and task.get("attempt_seq") in (None, 0, "", "0")
                and task.get("active_attempt") is None
                and not any(task.get(field) for field in (
                    "started_at", "attempt_history", "result_history", "result"))
            )
            blocked_before_native = (
                task.get("active_attempt") is None
                and str(task.get("status")) in {"pending", "queued", "blocked", "error"}
                and (task.get("result") or {}).get("blocked_before_native") is True
            )
            unexecuted = never_started or blocked_before_native
            if previous_harness and not unexecuted:
                _failed.append(previous_harness)
            node["excluded_harnesses"] = list(dict.fromkeys([h for h in _failed if h]))
            if unexecuted:
                if previous_harness and not task.get("unbound_owner"):
                    node["assigned_harness"] = previous_harness
            else:
                node["rebind_reason"] = ("relay_after_failed_attempts" if str(task.get("status")) == "error"
                                          else "recovering_unfinished_node_after_interruption")
        # error 任务接力必须带失败 context(不带=接力家失忆重跑):末次错误摘要随行。
        _last_err = str((task.get("result") or {}).get("error") or "")
        if _last_err:
            _rc = dict(node.get("recovery_context") or {})
            _rc["last_error"] = _last_err[:1000]
            node["recovery_context"] = _rc
        tasks[key] = node
        if task.get("native_session_scope") is not None:
            node["native_session_scope"] = task["native_session_scope"]
    spec = state.get("orchestration_spec") if isinstance(state.get("orchestration_spec"), dict) else {}
    plan = {"tasks": tasks, "team_id": str(state.get("team_id") or ""),
            "captain": str(state.get("captain") or ""),
            "delivery_contract": dict(state.get("delivery_contract") or {}),
            "source_ref": dict(state.get("source_ref") or {}),
            "input_snapshot": dict(state.get("input_snapshot") or {}),
            "version_identity": dict(state.get("version_identity") or {}),
            "quality_context": dict(state.get("quality_context") or {}),
            "execution_role": str(state.get("execution_role") or "champion"),
             "recovery_context": {"completed_tasks": completed_context},
             "conversation_context": json.dumps({"completed_tasks": completed_context}, ensure_ascii=False),
             "orchestration_spec": spec, "team_policy": {"force_multi": True, "auto_resume": True}}
    stored_policy = spec.get("team_policy")
    for field in ("adaptive", "auto_resume", "min_agents", "max_agents"):
        if field in spec:
            plan["team_policy"][field] = deepcopy(spec[field])
    if isinstance(stored_policy, dict):
        plan["team_policy"].update(deepcopy(stored_policy))
    if isinstance(spec.get("model_overrides"), dict):
        plan["model_overrides"] = deepcopy(spec["model_overrides"])
    for name in ("_continuous_coordination", "_planner_generated"):
        if spec.get(name) is not None:
            plan[name] = spec[name]
    return plan, str(spec.get("pattern") or "pk")


def _last_verification_frame(text: str):
    """Find complete top-level frames without promoting quoted examples.

    Keep the last top-level frame policy, including invalid-report cleanup.
    Quoted markers are data; an invalid frame never executes an inner example.
    """
    marker, terminator = "<<LEGION_VERIFY>>", "<<END>>"
    cursor, latest = 0, None
    while True:
        start = text.find(marker, cursor)
        if start < 0:
            return latest
        payload_start = start + len(marker)
        while payload_start < len(text) and text[payload_start].isspace():
            payload_start += 1
        if payload_start < len(text) and text[payload_start] not in '{["':
            # JSON scalars are invalid reports but still count as the last
            # frame. Skipping null/false/0 could revive an earlier "pass".
            try:
                value, end = json.JSONDecoder().raw_decode(text, payload_start)
            except (TypeError, ValueError):
                end = -1
            if end >= 0:
                while end < len(text) and text[end].isspace():
                    end += 1
                if text.startswith(terminator, end):
                    cursor = end + len(terminator)
                    latest = (start, cursor, value)
                    continue
            end = text.find(terminator, payload_start)
            next_start = text.find(marker, payload_start)
            if end >= 0 and (next_start < 0 or end < next_start):
                # Even a closed "nul"/garbage frame replaces an earlier pass.
                cursor = end + len(terminator)
                latest = (start, cursor, None)
                continue
            if next_start >= 0:
                cursor = next_start  # Unclosed prose mention before the report.
                continue
            return None
        end, quoted, escaped = payload_start, False, False
        while end < len(text):
            char = text[end]
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif text.startswith(terminator, end):
                break
            end += 1
        if end == len(text):
            # No trustworthy complete frame. Keep the pre-existing invalid
            # display cleanup only; do not decode the legacy inner candidate.
            old_start = text.rfind(marker)
            old_end = text.find(terminator, old_start + len(marker))
            return (old_start, old_end + len(terminator), None) if old_end >= 0 else None
        try:
            value = json.loads(text[payload_start:end])
        except (TypeError, ValueError):
            value = None
        cursor = end + len(terminator)
        latest = (start, cursor, value)


def parse_verification_report(final: str) -> tuple[str, dict | None]:
    """解析独立验证者的结构化结论；无效协议不伪造通过结论。"""
    text = str(final or "")
    frame = _last_verification_frame(text)
    if frame is None:
        return text, None
    start, end, value = frame
    cleaned = (text[:start] + text[end:]).strip()
    if not isinstance(value, dict):
        return cleaned, None
    verdict = str(value.get("verdict") or "unverified").strip().lower()
    if verdict not in {"pass", "revise", "fail", "blocked"}:
        verdict = "unverified"
    findings = value.get("findings") or []
    evidence = value.get("evidence") or []
    if isinstance(findings, str):
        findings = [findings]
    if isinstance(evidence, str):
        evidence = [evidence]
    report = {
        "verdict": verdict,
        "findings": [str(item)[:1000] for item in findings if str(item).strip()][:50],
        "evidence": [str(item)[:1000] for item in evidence if str(item).strip()][:50],
        "revision_request": str(value.get("revision_request") or "")[:4000],
    }
    # 方向评审可把样片观察和 Gap 作为结构化事实回流；普通 critic 不提供这些字段时
    # 保持旧协议。这里只保真、限长，不替评审者决定是否扩展或换 owner。
    for name in ("current_hypothesis", "recommended_action", "claim_ceiling"):
        if value.get(name) is not None:
            report[name] = str(value.get(name) or "")[:4000]
    for name in ("sample_evidence", "rendered_observations", "gap_plan"):
        rows = value.get(name)
        if isinstance(rows, list):
            report[name] = [item if isinstance(item, dict) else {"observation": str(item)[:2000]}
                            for item in rows[:80]]
    requirements = value.get("replacement_requirements")
    if isinstance(requirements, str):
        requirements = [requirements]
    if isinstance(requirements, list):
        report["replacement_requirements"] = [str(item)[:1000] for item in requirements
                                               if str(item).strip()][:30]
    # P1-2 验证五层 verdict:runtime/flow/outcome/frontier/release 各层独立判定,
    # 低层 PASS 不得上推高层;缺层如实 unknown(不是 pass)。
    layers = value.get("layer_verdicts")
    if isinstance(layers, dict):
        report["layer_verdicts"] = {
            layer: (str(layers.get(layer) or "unknown").strip().lower()
                    if str(layers.get(layer) or "unknown").strip().lower()
                    in {"pass", "fail", "revise", "unknown"} else "unknown")
            for layer in ("runtime", "flow", "outcome", "frontier", "release")}
    # P0-3/P0-8:下游必须声明消费的锚点 revision——过期复验不算数。
    if value.get("anchors_revision") is not None:
        try:
            report["anchors_revision"] = int(value.get("anchors_revision") or 0)
        except (TypeError, ValueError):
            report["anchors_revision"] = 0
    # P1-3:真人证据(专家审校/新手测试/盲评)是独立于机器 verdict 的事实。
    human = value.get("human_evidence")
    if isinstance(human, list):
        report["human_evidence"] = [item if isinstance(item, dict) else {"observation": str(item)[:2000]}
                                    for item in human[:40]]
    return cleaned, report


@dataclass
class TaskFacts:
    goal: str = ""
    role: str = ""
    deliverables: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    upstream_artifacts: list[dict] = field(default_factory=list)
    upstream_results: list[dict] = field(default_factory=list)
    task_id: str = ""
    team_id: str = ""
    attempt_id: int = 0
    input_hash: str = ""
    idempotency_key: str = ""
    delivery_contract: dict = field(default_factory=dict)
    orchestration: dict = field(default_factory=dict)
    project_context: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_value(cls, value: Any) -> "TaskFacts":
        if isinstance(value, cls):
            return value
        value = value if isinstance(value, dict) else {}
        return cls(**{name: value[name] for name in cls.__dataclass_fields__ if name in value})


@dataclass
class ErrorEnvelope:
    category: str
    retryable: bool
    scope: str
    message: str
    tool: str = ""
    code: str = ""
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_value(cls, value: Any) -> "ErrorEnvelope | None":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict) or not value.get("category"):
            return None
        return cls(
            category=str(value.get("category")),
            retryable=bool(value.get("retryable")),
            scope=str(value.get("scope") or "task"),
            message=str(value.get("message") or ""),
            tool=str(value.get("tool") or ""),
            code=str(value.get("code") or ""),
            details=value.get("details") if isinstance(value.get("details"), dict) else {},
        )


@dataclass
class CapabilityPolicy:
    read: bool = True
    write: bool = True
    network: bool = True
    execute: bool = True
    install: bool = True
    local_git: bool = True
    publish: bool = False
    destructive: bool = True
    billable: bool = True
    credential_login: bool = True
    system_security: bool = True
    confirm_required: list[str] = field(default_factory=lambda: [
        "git_push", "pull_request", "release", "public_deploy",
    ])

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_value(cls, value: Any) -> "CapabilityPolicy":
        if isinstance(value, cls):
            return value
        value = value if isinstance(value, dict) else {}
        return cls(**{name: value[name] for name in cls.__dataclass_fields__ if name in value})


@dataclass(frozen=True)
class AdapterCapabilities:
    integration: str
    streaming: str
    resume: str
    checkpoint: bool
    tools: bool
    artifacts: bool
    interrupt: bool
    policy: str
    model: str
    cost_class: str = "standard"

    def to_dict(self) -> dict:
        return asdict(self)
