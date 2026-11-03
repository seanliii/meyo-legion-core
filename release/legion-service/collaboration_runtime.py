#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""协作规格到运行成员的绑定层。

逻辑任务使用稳定 task_id；Harness 只是一次可替换的运行时 assignment。这里仅消费规划器
显式给出的方向、能力要求、偏好与运行事实，不从任务文本猜意图，也不改写任务内容。
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
from typing import Any


COLLABORATION_PROTOCOL = "meyo.legion.collaboration"
COLLABORATION_VERSION = 1
COLLABORATION_OPERATIONS = (
    "coordinate", "handoff", "fork", "need_peer", "captain_bid",
    "handoff_captain", "need_approval", "upgrade",
)
COORDINATION_ACTIONS = frozenset({"continue", "complete", "blocked", "user_decision"})
_ENVELOPE_ENCODING = "collaboration-envelope/v1"
_LEGACY_ENCODING = "legacy-operation-map"
_MISSING = object()


def _shape_error(field: str, expected: str, value: Any = _MISSING, *,
                 code: str = "invalid_type") -> dict:
    error = {
        "field": field,
        "expected": expected,
        "actual": "missing" if value is _MISSING else type(value).__name__,
        "code": code,
    }
    if isinstance(value, str) and value not in COLLABORATION_OPERATIONS:
        error["actual"] = value[:160]
    return error


def _protocol_receipt(encoding: str, operations: list[str], *, team_id: str,
                      task_id: str, correlation_id: str, harness: str,
                      errors: list[dict] | None = None) -> dict:
    correlation = str(correlation_id or f"{team_id}:{task_id}")
    return {
        "name": COLLABORATION_PROTOCOL,
        "version": COLLABORATION_VERSION,
        "encoding": encoding,
        "team_id": str(team_id or ""),
        "task_id": str(task_id or ""),
        "harness": str(harness or ""),
        "correlation_id": correlation,
        "operations": [
            {"operation": name, "operation_index": index,
             "operation_id": f"{correlation}:{index + 1}:{name}"}
            for index, name in enumerate(operations)
        ],
        "errors": list(errors or []),
    }


def rebind_protocol_receipt(receipt: dict, *, correlation_id: str) -> dict:
    """Rebind decoded operations to the coordination correlation without double decoding."""
    bound = copy.deepcopy(receipt)
    correlation = str(correlation_id)
    bound["correlation_id"] = correlation
    for index, row in enumerate(bound.get("operations") or []):
        operation = str(row.get("operation") or "")
        operation_index = int(row.get("operation_index", index))
        row["operation_index"] = operation_index
        row["operation_id"] = f"{correlation}:{operation_index + 1}:{operation}"
    return bound


def _v1_operation_error(name: str, payload: Any) -> dict | None:
    """Reject operations that would otherwise become empty approvals or silent no-ops."""
    if name in {"handoff", "handoff_captain"}:
        target = payload.get("to")
        if not isinstance(target, str) or not target.strip():
            return _shape_error(f"{name}.to", "non-empty str", target)
    elif name == "need_peer":
        for field in ("key", "ask"):
            value = payload.get(field)
            if not isinstance(value, str) or not value.strip():
                return _shape_error(f"need_peer.{field}", "non-empty str", value)
    elif name == "need_approval":
        capability = payload.get("capability")
        if not isinstance(capability, str) or not capability.strip():
            return _shape_error("need_approval.capability", "non-empty str", capability)
    elif name == "upgrade":
        if payload.get("depth") != "standard":
            return _shape_error("upgrade.depth", "standard", payload.get("depth"))
    elif name == "fork":
        if not payload:
            return _shape_error("fork", "non-empty list[dict]", payload)
        for index, row in enumerate(payload):
            task = row.get("task") if isinstance(row, dict) else None
            if not isinstance(task, str) or not task.strip():
                return _shape_error(f"fork[{index}].task", "non-empty str", task)
    return None


def decode_orchestration_request(request: Any, *, team_id: str = "", task_id: str = "",
                                 correlation_id: str = "", harness: str = "") -> tuple[dict, dict]:
    """Decode the neutral v1 envelope into the existing operation-map consumer.

    Legacy maps stay byte-equivalent in meaning. A v1 envelope is atomic: any framing,
    version or operation error rejects all operations instead of executing a valid prefix.
    Runtime identity comes from the caller, never from model-supplied envelope fields.
    """
    if not isinstance(request, dict):
        errors = [_shape_error("request", "dict", request)]
        return {}, _protocol_receipt(
            _ENVELOPE_ENCODING, [], team_id=team_id, task_id=task_id,
            correlation_id=correlation_id, harness=harness, errors=errors)
    is_envelope = any(field in request for field in ("protocol", "version", "operations"))
    if not is_envelope:
        operations = [name for name in COLLABORATION_OPERATIONS if name in request]
        return copy.deepcopy(request), _protocol_receipt(
            _LEGACY_ENCODING, operations, team_id=team_id, task_id=task_id,
            correlation_id=correlation_id, harness=harness)

    errors: list[dict] = []
    if request.get("protocol") != COLLABORATION_PROTOCOL:
        errors.append(_shape_error("protocol", COLLABORATION_PROTOCOL,
                                   request.get("protocol", _MISSING), code="invalid_protocol"))
    version = request.get("version", _MISSING)
    if isinstance(version, bool) or not isinstance(version, int) or version != COLLABORATION_VERSION:
        errors.append(_shape_error("version", str(COLLABORATION_VERSION), version,
                                   code="unsupported_version"))
    if set(request) - {"protocol", "version", "operations"}:
        errors.append(_shape_error("request", "envelope without legacy operation fields", request,
                                   code="mixed_envelope_and_legacy_operations"))
    raw_operations = request.get("operations", _MISSING)
    if not isinstance(raw_operations, list):
        errors.append(_shape_error("operations", "list", raw_operations))
        raw_operations = []
    elif len(raw_operations) > 16:
        errors.append(_shape_error("operations", "at most 16 items", raw_operations,
                                   code="operation_budget_exceeded"))

    decoded: dict = {}
    operation_names: list[str] = []
    for index, operation in enumerate(raw_operations[:16]):
        field = f"operations[{index}]"
        if not isinstance(operation, dict) or set(operation) - {"type", "payload"}:
            errors.append(_shape_error(field, "dict with only type/payload", operation))
            continue
        name = operation.get("type", _MISSING)
        if not isinstance(name, str) or name not in COLLABORATION_OPERATIONS:
            errors.append(_shape_error(f"{field}.type", "registered collaboration operation", name,
                                       code="unknown_operation"))
            continue
        if name in decoded:
            errors.append(_shape_error(f"{field}.type", "unique operation type", name,
                                       code="duplicate_operation"))
            continue
        payload = operation.get("payload", _MISSING)
        expected = "list" if name == "fork" else "dict"
        if not isinstance(payload, list if name == "fork" else dict):
            errors.append(_shape_error(f"{field}.payload", expected, payload))
            continue
        operation_error = _v1_operation_error(name, payload)
        if operation_error:
            operation_error["field"] = f"{field}.payload.{operation_error['field']}"
            errors.append(operation_error)
            continue
        decoded[name] = copy.deepcopy(payload)
        operation_names.append(name)
    if errors:
        decoded, operation_names = {}, []
    return decoded, _protocol_receipt(
        _ENVELOPE_ENCODING, operation_names, team_id=team_id, task_id=task_id,
        correlation_id=correlation_id, harness=harness, errors=errors)


def coordination_shape_errors(proposal: Any, *, require_action: bool = False,
                              strict: bool = False) -> list[dict]:
    """Validate control structure only; never infer tasks or translate member intent."""
    if not isinstance(proposal, dict):
        return [_shape_error("coordinate", "dict", proposal)]
    errors: list[dict] = []
    action = proposal.get("action", _MISSING)
    if action is _MISSING:
        if require_action:
            errors.append(_shape_error("action", "|".join(sorted(COORDINATION_ACTIONS)), action))
    else:
        canonical = action.strip().lower() if isinstance(action, str) else ""
        if strict and canonical not in COORDINATION_ACTIONS:
            errors.append(_shape_error("action", "|".join(sorted(COORDINATION_ACTIONS)), action,
                                       code="invalid_action"))
    reason = proposal.get("reason")
    if strict and reason is not None and not isinstance(reason, str):
        errors.append(_shape_error("reason", "str", reason))

    updates = proposal.get("task_updates")
    if updates is not None and not isinstance(updates, dict):
        errors.append(_shape_error("task_updates", "dict", updates))
    elif isinstance(updates, dict):
        for task_id, patch in updates.items():
            if not isinstance(patch, dict):
                errors.append(_shape_error(f"task_updates[{task_id!r}]", "dict", patch))
    next_plan = proposal.get("next_plan")
    if next_plan is not None and not isinstance(next_plan, dict):
        errors.append(_shape_error("next_plan", "dict", next_plan))
    elif isinstance(next_plan, dict):
        tasks = next_plan.get("tasks")
        if tasks is not None and not isinstance(tasks, dict):
            errors.append(_shape_error("next_plan.tasks", "dict keyed by task ID", tasks))

    messages = proposal.get("messages")
    if messages is not None and not isinstance(messages, list):
        errors.append(_shape_error("messages", "list", messages))
    elif isinstance(messages, list):
        if strict and len(messages) > 30:
            errors.append(_shape_error("messages", "at most 30 items", messages,
                                       code="message_budget_exceeded"))
        for index, message in enumerate(messages[:30]):
            if not isinstance(message, dict):
                errors.append(_shape_error(f"messages[{index}]", "dict with to/text", message))
                continue
            recipient = message.get("to", _MISSING)
            if not isinstance(recipient, str) or not recipient.strip():
                errors.append(_shape_error(f"messages[{index}].to", "non-empty str", recipient))
            if "text" not in message:
                if not message.get("artifact_refs"):
                    errors.append(_shape_error(f"messages[{index}].text", "str or artifact_refs"))
            elif not isinstance(message["text"], str):
                errors.append(_shape_error(f"messages[{index}].text", "str", message["text"]))
            elif strict and not message["text"].strip() and not message.get("artifact_refs"):
                errors.append(_shape_error(f"messages[{index}].text", "non-empty str or artifact_refs",
                                           message["text"]))
            refs = message.get("artifact_refs")
            if refs is not None and not isinstance(refs, list):
                errors.append(_shape_error(f"messages[{index}].artifact_refs", "list[dict]", refs))
            elif isinstance(refs, list):
                for ref_index, ref in enumerate(refs):
                    if not isinstance(ref, dict):
                        errors.append(_shape_error(
                            f"messages[{index}].artifact_refs[{ref_index}]", "dict", ref))
    return errors


def normalize_coordination(proposal: Any, *, protocol: dict,
                           require_action: bool = False, strict: bool | None = None) -> dict:
    """Return an executable proposal or shape errors, with a durable protocol receipt."""
    strict = require_action if strict is None else strict
    errors = coordination_shape_errors(proposal, require_action=require_action, strict=strict)
    if errors:
        return {"ok": False, "shape_errors": errors}
    action = proposal.get("action", "continue")
    action = action.strip().lower() if isinstance(action, str) else "continue"
    if not strict and action not in COORDINATION_ACTIONS:
        action = "continue"
    normalized = {
        "ok": True,
        "action": action,
        "reason": str(proposal.get("reason") or "")[:4000],
        "task_updates": copy.deepcopy(proposal.get("task_updates") or {}),
        "messages": copy.deepcopy((proposal.get("messages") or [])[:30]),
        "next_plan": copy.deepcopy(proposal.get("next_plan") or {}),
        "collaboration_protocol": copy.deepcopy(protocol),
    }
    return normalized


@dataclass(frozen=True)
class AssignmentDecision:
    task_id: str
    harness: str
    source: str
    candidates: list[str]
    requirements: list[str]
    excluded: list[str]
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def bind_explicit_owner_request(question: str, plan: Any, *, mode: str = "auto",
                                default_owner: str = "") -> dict:
    """Bind a selected or personally configured owner, without planning its solution.

    This only supplies the one execution node required by the existing runner.
    The original goal, resource controls, context and any native subplanning stay
    with that member. Explicit collaboration structures retain their own path.
    """
    if mode != "auto" or not isinstance(plan, dict) or not str(question).strip():
        return {"bound": False}
    policy = plan.get("team_policy")
    if policy is None and default_owner:
        policy = {}
    if not isinstance(policy, dict):
        return {"bound": False}
    try:
        if policy.get("force_multi") or int(policy.get("min_agents", 1)) > 1:
            return {"bound": False}
    except (TypeError, ValueError):
        return {"bound": False}
    raw_candidates = policy.get("candidate_harnesses")
    source = "explicit_owner_binding"
    if raw_candidates is None and default_owner:
        raw_candidates = [default_owner]
        source = "personal_runtime_owner_binding"
    if not isinstance(raw_candidates, (list, tuple, set)):
        return {"bound": False}
    candidates = _strings(raw_candidates)
    if len(candidates) != 1 or plan.get("solo") is False:
        return {"bound": False}
    owner = candidates[0]
    leaders = _strings(plan.get("captain_candidates"))
    if leaders and set(leaders) != {owner}:
        return {"bound": False}
    if any(plan.get(field) for field in (
        "tasks", "pipeline", "hierarchy", "organization", "orchestration",
        "orchestration_spec", "pattern", "verification", "result_policy",
        "captain", "captain_synthesis",
    )):
        return {"bound": False}
    result = copy.deepcopy(plan)
    result["team_policy"] = {**copy.deepcopy(policy), "candidate_harnesses": candidates}
    result["tasks"] = {
        owner: {"task": str(question), "assigned_harness": owner,
                "preferred_harnesses": [owner], "assignment_locked": True}
    }
    return {"bound": True, "owner": owner, "plan": result,
            "source": source}


def _task_node(task_id: str, value: Any) -> dict:
    if isinstance(value, dict):
        node = copy.deepcopy(value)
        node["task"] = str(node.get("task") or node.get("prompt") or node.get("role") or "回答问题")
    else:
        node = {"task": str(value or "回答问题")}
    node["task_id"] = str(task_id)
    node["depends_on"] = _strings(node.get("depends_on", node.get("dependencies", [])))
    node["role"] = str(node.get("role") or "").strip()
    node["capability_requirements"] = _strings(
        node.get("capability_requirements", node.get("requires", node.get("capabilities", []))))
    node["preferred_harnesses"] = _strings(
        node.get("preferred_harnesses", node.get("preferred_harness", [])))
    node["excluded_harnesses"] = _strings(
        node.get("excluded_harnesses", node.get("exclude_harnesses", [])))
    return node


def _definition_capabilities(definition: dict) -> set[str]:
    runtime = definition.get("runtime") if isinstance(definition.get("runtime"), dict) else {}
    profile = definition.get("profile") if isinstance(definition.get("profile"), dict) else {}
    specs = definition.get("specs") if isinstance(definition.get("specs"), dict) else {}
    values = [
        *list(definition.get("scenes") or []),
        *list(runtime.get("capabilities") or []),
        *list(runtime.get("provided_tools") or []),
        *list(profile.get("strengths") or []),
    ]
    for key in ("streaming", "resume", "policy", "model", "cost_class", "usage", "compaction"):
        if specs.get(key):
            values.append(f"{key}:{specs[key]}")
    for key in ("checkpoint", "tools", "artifacts", "interrupt"):
        if specs.get(key):
            values.append(key)
    return {str(value).strip().lower() for value in values if str(value).strip()}


def _hard_runtime_requirements(requirements: list[str]) -> set[str]:
    """Return only objective runtime requirements that can safely exclude a Harness.

    Semantic abilities remain ranking context.  Tool/file/browser facts are hard because assigning a
    node that must create artifacts to a text-only Harness makes execution impossible, not merely
    lower quality.
    """
    aliases = {
        "tool": "tools", "tools": "tools", "tool_use": "tools",
        "artifact": "artifacts", "artifacts": "artifacts", "file_output": "artifacts",
        "shell": "shell", "bash": "shell", "command_execution": "shell",
        "read": "read", "file_read": "read", "write": "write", "file_write": "write",
        "browser": "browser", "browser_use": "browser", "browser_automation": "browser",
    }
    return {aliases[item] for item in requirements if item in aliases}


def _supports_hard_requirements(definition: dict, requirements: set[str]) -> bool:
    if not requirements:
        return True
    specs = definition.get("specs") if isinstance(definition.get("specs"), dict) else {}
    capabilities = _definition_capabilities(definition)
    if "tools" in requirements and specs.get("tools") is not True:
        return False
    if "artifacts" in requirements and specs.get("artifacts") is not True:
        return False
    capability_groups = {
        "shell": {"shell", "bash", "command_execution", "desktop_exec"},
        "read": {"read", "file_editor", "desktop_fs_read"},
        "write": {"write", "file_editor", "desktop_fs_write"},
        "browser": {"browser", "browser use", "browser-use", "browser automation agent",
                    "computer-use", "computer use", "desktop", "web tasks"},
    }
    return all(bool(capabilities.intersection(capability_groups.get(requirement, {requirement})))
               for requirement in requirements - {"tools", "artifacts"})


def _default_definitions() -> dict[str, dict]:
    try:
        import runtime_manifest
        return runtime_manifest.load_definitions()
    except Exception:
        return {}


def _performance_order(candidates: list[str], scene: str) -> list[str]:
    try:
        import harness_performance
        return harness_performance.rank(candidates, scene or "general", "single")
    except Exception:
        return list(candidates)


def bind_plan(plan: Any, valid: set[str] | list[str], *,
              runnable: set[str] | list[str] | None = None,
              definitions: dict[str, dict] | None = None,
              allow_unbound: bool = False,
              max_harnesses: int | None = None) -> tuple[dict, list[dict]]:
    """把逻辑 task_id 绑定到当前可执行 Harness，并保留旧 `{harness: task}` 输入。

    显式 `assigned_harness/assign_to/harness` 优先；旧任务键本身是 Harness 时自动兼容。
    其余节点只依据显式 capability requirements、direction、偏好、排除项和真实性能账本选择。
    """
    source = copy.deepcopy(plan) if isinstance(plan, dict) else {}
    raw_tasks = source.get("tasks") if isinstance(source.get("tasks"), dict) else {}
    valid_set = {str(item) for item in valid if str(item)}
    runnable_set = {str(item) for item in (runnable or []) if str(item)}
    available = valid_set & runnable_set if runnable_set else valid_set
    definitions = definitions if isinstance(definitions, dict) else _default_definitions()
    directions = source.get("directions") if isinstance(source.get("directions"), dict) else {}
    usage: dict[str, int] = {}
    selected_pool: list[str] = []
    planner_generated = bool(source.get("_planner_generated"))
    team_cap = max(1, int(max_harnesses)) if max_harnesses else 0
    tasks: dict[str, dict] = {}
    decisions: list[dict] = []
    errors: list[dict] = []

    # 第一遍先确定显式/兼容绑定，给独立验证者排除目标执行者时使用。
    provisional: dict[str, str] = {}
    nodes = {str(task_id): _task_node(str(task_id), value) for task_id, value in raw_tasks.items()}
    # compile_orchestration_plan 只做结构编译时没有运行时成员目录；必须原样保留显式绑定，
    # 等 dispatch 拿到真实 valid/runnable 后再校验，不能在无事实时删成员。
    if allow_unbound and not valid_set:
        source["tasks"] = nodes
        source["task_assignments"] = {
            task_id: str(node.get("assigned_harness") or node.get("assign_to") or
                         node.get("harness") or node.get("agent") or "")
            for task_id, node in nodes.items()
        }
        source["assignment_decisions"] = []
        source["binding_errors"] = []
        return source, []
    for task_id, node in nodes.items():
        suggested = str(node.get("assigned_harness") or node.get("assign_to") or
                        node.get("harness") or node.get("agent") or "").strip()
        locked = bool(node.get("assignment_locked"))
        # 只有真实注册成员才能成为显式绑定。规划器或旧调用偶尔把 research/implement/test
        # 这类逻辑 task_id 回填进 assigned_harness；未锁定时应视为无效建议并重新选可运行成员，
        # 不能把语义角色当 CLI key 启动。用户明确锁定的未知成员仍保留为错误，避免静默改派。
        explicit = suggested if suggested in valid_set and (not planner_generated or locked) else ""
        if suggested and suggested not in valid_set:
            node["invalid_assignment_suggestion"] = suggested
            for field in ("assigned_harness", "assign_to", "harness", "agent"):
                node.pop(field, None)
            if locked:
                node["assigned_harness"] = suggested
                explicit = suggested
        elif suggested and planner_generated and not locked:
            node["preferred_harnesses"] = list(dict.fromkeys([suggested, *node.get("preferred_harnesses", [])]))
        if not explicit and task_id in valid_set and not planner_generated:
            explicit = task_id
        if explicit:
            provisional[task_id] = explicit

    binding_order = [task_id for task_id, node in nodes.items() if not node.get("verification_role")]
    binding_order.extend(task_id for task_id, node in nodes.items() if node.get("verification_role"))
    for task_id in binding_order:
        node = nodes[task_id]
        explicit = provisional.get(task_id, "")
        requirements = [item.lower() for item in node.get("capability_requirements") or []]
        preferred = [item for item in node.get("preferred_harnesses") or [] if item in available]
        excluded = set(node.get("excluded_harnesses") or [])
        if node.get("verification_role"):
            for target in node.get("depends_on") or []:
                bound_target = tasks.get(str(target)) if isinstance(tasks.get(str(target)), dict) else {}
                target_harness = str(bound_target.get("assigned_harness") or
                                     provisional.get(str(target), ""))
                if target_harness:
                    excluded.add(target_harness)
        candidates = [key for key in available if key not in excluded]
        hard_requirements = _hard_runtime_requirements(requirements)
        if hard_requirements:
            qualified = [key for key in candidates
                         if _supports_hard_requirements(definitions.get(key) or {}, hard_requirements)]
            rejected = sorted(set(candidates) - set(qualified))
            excluded.update(rejected)
            candidates = qualified
        if explicit and explicit in excluded:
            if node.get("assignment_locked"):
                errors.append({"task_id": task_id, "error": "reviewer_not_independent",
                               "assigned_harness": explicit, "excluded": sorted(excluded)})
                node["binding_error"] = "reviewer_not_independent"
                tasks[task_id] = node
                continue
            node["invalid_assignment_suggestion"] = explicit
            explicit = ""
        if team_cap and len(selected_pool) >= team_cap:
            pooled = [key for key in selected_pool if key in candidates]
            if pooled:
                candidates = pooled
        if explicit:
            if explicit not in valid_set:
                errors.append({"task_id": task_id, "error": "assigned_harness_not_registered",
                               "assigned_harness": explicit})
                node["binding_error"] = "assigned_harness_not_registered"
                tasks[task_id] = node
                continue
            if runnable_set and explicit not in runnable_set:
                node["runtime_available"] = False
            harness, source_kind = explicit, "explicit" if any(
                node.get(name) for name in ("assigned_harness", "assign_to", "harness", "agent")) else "legacy_key"
        else:
            if not candidates:
                if allow_unbound:
                    tasks[task_id] = node
                    continue
                error = "no_capability_qualified_harness" if hard_requirements else "no_runnable_harness"
                errors.append({"task_id": task_id, "error": error,
                               "requirements": sorted(hard_requirements)})
                node["binding_error"] = error
                tasks[task_id] = node
                continue
            scene = str(node.get("direction") or directions.get(task_id) or source.get("direction") or "general")
            performance = _performance_order(candidates, scene)
            perf_index = {key: index for index, key in enumerate(performance)}

            def score(key: str) -> tuple:
                capabilities = _definition_capabilities(definitions.get(key) or {})
                requirement_matches = sum(1 for item in requirements if item in capabilities)
                direction_match = int(scene in {str(definitions.get(key, {}).get("direction") or "").lower(),
                                                *{str(item).lower() for item in definitions.get(key, {}).get("scenes") or []}})
                preferred_rank = preferred.index(key) if key in preferred else len(preferred) + 1
                return (int(key in preferred), requirement_matches, direction_match,
                        -usage.get(key, 0), -preferred_rank, -perf_index.get(key, len(candidates)))

            harness = max(candidates, key=score)
            source_kind = "runtime_capability_match"
        usage[harness] = usage.get(harness, 0) + 1
        if harness not in selected_pool:
            selected_pool.append(harness)
        definition = definitions.get(harness) or {}
        specs = definition.get("specs") if isinstance(definition.get("specs"), dict) else {}
        node["assigned_harness"] = harness
        node["assignment_source"] = source_kind
        node["runtime_available"] = not runnable_set or harness in runnable_set
        node["native_capabilities"] = {
            key: specs.get(key) for key in
            ("streaming", "resume", "checkpoint", "tools", "artifacts", "interrupt", "policy",
             "cost_class", "usage", "compaction")
            if key in specs
        }
        tasks[task_id] = node
        decisions.append(AssignmentDecision(
            task_id=task_id, harness=harness, source=source_kind,
            candidates=sorted(candidates or [harness]), requirements=requirements,
            excluded=sorted(excluded),
            reason=("显式/旧格式绑定" if source_kind in {"explicit", "legacy_key"}
                    else ((f"忽略未注册成员建议 {node.get('invalid_assignment_suggestion')}，"
                           "按显式能力、方向、可运行状态、真实战绩与当前负载绑定")
                          if node.get("invalid_assignment_suggestion") else
                          "按显式能力、方向、可运行状态、真实战绩与当前负载绑定")),
        ).to_dict())

    # 绑定时让验证节点后置，保证能排除目标节点的实际执行者；对外仍恢复原 DAG 节点顺序。
    source["tasks"] = {task_id: tasks[task_id] for task_id in nodes if task_id in tasks}
    source["task_assignments"] = {task_id: node.get("assigned_harness", "")
                                  for task_id, node in tasks.items()}
    source["assignment_decisions"] = decisions
    source["binding_errors"] = errors
    return source, decisions


def should_use_solo_fastpath(plan: Any, bound_task: dict | None = None) -> bool:
    """Use the latency shortcut only when planning explicitly classified the task as quick."""
    source = plan if isinstance(plan, dict) else {}
    bound = bound_task if isinstance(bound_task, dict) else {}
    direction = str(bound.get("direction") or "").strip().casefold()
    return bool(source.get("solo") is True or source.get("via") == "quick_pred_fastpath"
                or direction in {"quick", "fast", "快答"})


def bind_single_task(plan: Any, valid: set[str] | list[str], *,
                     runnable: set[str] | list[str] | None = None,
                     definitions: dict[str, dict] | None = None) -> dict:
    """Resolve one logical task for the server solo fast path.

    The planner's task key is a durable task_id, not necessarily a Harness key.  Returning both
    identities prevents the latency fast path from bypassing the normal runtime binding layer.
    """
    source = copy.deepcopy(plan) if isinstance(plan, dict) else {}
    raw_tasks = source.get("tasks") if isinstance(source.get("tasks"), dict) else {}
    if len(raw_tasks) != 1:
        return {"ok": False, "error": "single_task_required"}
    bound, decisions = bind_plan(source, valid, runnable=runnable,
                                 definitions=definitions, max_harnesses=1)
    task_id = next(iter(raw_tasks))
    node = ((bound.get("tasks") or {}).get(task_id) or {})
    if not isinstance(node, dict):
        node = _task_node(task_id, node)
    harness = str(node.get("assigned_harness") or "")
    if not harness:
        return {"ok": False, "error": str(node.get("binding_error") or "task_unbound"),
                "task_id": task_id, "plan": bound, "decisions": decisions}
    directions = bound.get("directions") if isinstance(bound.get("directions"), dict) else {}
    result = {"ok": True, "task_id": task_id, "harness": harness,
              "task": str(node.get("task") or "回答问题"),
              "direction": str(node.get("direction") or directions.get(task_id) or "programming"),
              "plan": bound, "decisions": decisions}
    result["use_solo_fastpath"] = should_use_solo_fastpath(source, result)
    return result


def assigned_harness(plan: dict, task_id: str) -> str:
    node = ((plan or {}).get("tasks") or {}).get(task_id) or {}
    if isinstance(node, dict):
        return str(node.get("assigned_harness") or node.get("assign_to") or
                   node.get("harness") or node.get("agent") or "")
    return str(task_id)
