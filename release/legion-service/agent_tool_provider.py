#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把每个 Agent 的原生工具暴露成军团共享 capability provider。"""
from __future__ import annotations

import json
import math
import os
import pathlib
import time
import uuid
from contextlib import contextmanager
from typing import Any

import external_agent_registry as external

_BUILTIN = {
    "cc": ["bash", "read", "write", "search", "web", "subagent"],
    "codex": ["shell", "read", "write", "search", "web"],
    "hermes": ["terminal", "read", "write", "web", "mcp", "skills", "subagent"],
    "deepseek": ["shell", "read", "write", "search", "subagent"],
    "deepagents": ["shell", "read", "write", "web", "subagent"],
    "auto": ["shell", "read", "write", "search", "browser", "skills"],
    "goose": ["shell", "read", "write", "extensions", "mcp"],
    "cline": ["shell", "read", "write", "browser", "mcp"],
    "openclaw": ["shell", "read", "write", "skills", "memory", "mcp", "automation"],
    "deer-flow": ["shell", "read", "write", "search", "sandbox", "skills", "mcp", "subagent"],
    "cowagent": ["shell", "read", "write", "search", "skills", "mcp", "subagent"],
    "pi": ["shell", "read", "write", "search", "extensions", "session"],
    "nanobot": ["shell", "read", "write", "search", "mcp", "sessions", "channels", "plugins"],
    "genericagent": ["shell", "read", "write", "mcp", "gui", "web", "tui"],
    "omniagent": ["shell", "read", "write", "skills", "planning", "reflection", "orchestration"],
    "kimi-code": ["shell", "read", "write", "search", "mcp", "acp", "skills", "session", "subagent"],
    "ironclaw": ["shell", "read", "write", "mcp", "memory", "sandbox", "approvals"],
    "aeon": ["skills", "runs", "logs", "secrets", "config", "memory", "packs", "mcp", "strategy", "schedule"],
}
_CAPS = {
    "bash": "shell", "terminal": "shell", "shell": "shell", "read": "filesystem",
    "write": "filesystem", "search": "search", "web": "web", "browser": "browser",
    "mcp": "mcp", "skills": "tool_discovery", "extensions": "tools",
    "memory": "memory", "subagent": "orchestration", "automation": "automation",
    "sandbox": "sandbox",
}
_UNSET = object()
_CALL_FIELDS = ("task", "workdir", "user_id", "session_id", "project_context",
                "task_facts", "runtime_brief", "capability_policy", "cancel_check",
                "attempt_timeout", "direction", "execution_depth")
_ENV_FIELDS = ("MEYO_USER_ID", "MEYO_SESSION_ID", "MEYO_WORKDIR", "MEYO_TASK_DEADLINE",
               "MEYO_PROJECT_CONTEXT_JSON", "MEYO_TASK_FACTS_JSON", "MEYO_RUNTIME_BRIEF",
               "MEYO_CAPABILITY_POLICY_JSON", "MEYO_TASK_MEMORY_JSON", "MEYO_PRIOR_ATTEMPTS",
               "MEYO_DIRECTION", "MEYO_EXECUTION_DEPTH", "MEYO_TASK_IDEMPOTENCY_KEY",
               "MEYO_MODEL_CATALOG_PATH", "MEYO_MODEL_MATRIX_PATH", "MEYO_APPID_ALIASES",
               "MEYO_MEDIA_GATEWAY", "MEYO_UNIFIED_GATEWAY",
               "MEYO_NATIVE_CAPABILITY_IDS", "MEYO_LOCAL_MODEL_CONFIG",
               "MEYO_PERSONAL_STATE_DIR", "LEGION_PERSONAL_OWNER",
               "MEYO_MODEL_WORKSPACE_ID", "LEGION_PROC_REGISTRY_DIR",
               "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "MEYO_QHASH")


def _runtime_binding():
    # core 定位现有 orchestrator；不另找成员 HOME，也不从请求参数接受一份任意 env。
    import legion_core  # noqa: F401
    import harness_registry as registry
    inherit = getattr(registry._INPROC_RUNTIME_ENV, "inherit_process_context", True)
    env = ({key: os.environ[key] for key in _ENV_FIELDS if key in os.environ}
           if inherit else {})
    local = getattr(registry._INPROC_RUNTIME_ENV, "values", {})
    env.update({key: local[key] for key in _ENV_FIELDS if key in local})
    return registry, env, getattr(registry._INPROC_CANCEL_CHECK, "check", None)


@contextmanager
def _runtime_scope(env, cancel_check, *, inherit_process_context=None):
    """仅复用已有 thread-local；同线程嵌套借用结束后恢复调用者现场。"""
    registry, _, _ = _runtime_binding()
    old_env = dict(getattr(registry._INPROC_RUNTIME_ENV, "values", {}))
    old_inherit = getattr(registry._INPROC_RUNTIME_ENV, "inherit_process_context", _UNSET)
    inherit = ((True if old_inherit is _UNSET else old_inherit)
               if inherit_process_context is None else inherit_process_context)
    if not isinstance(inherit, bool):
        raise ValueError("inherit_process_context must be boolean")
    old_check = getattr(registry._INPROC_CANCEL_CHECK, "check", None)
    old_cancelled = getattr(registry._INPROC_CANCEL_CHECK, "cancelled", False)
    old_sink = getattr(registry._INPROC_EVENT_SINK, "cb", None)
    registry.set_inproc_runtime_env({**old_env, **env} if inherit else dict(env))
    # HTTP identity comes from its authenticated request, not the process that
    # happened to start the server. This flag is thread-local, never an env opt-out.
    registry._INPROC_RUNTIME_ENV.inherit_process_context = inherit
    registry.set_inproc_cancel_check(cancel_check)
    try:
        yield
    finally:
        registry.set_inproc_runtime_env(old_env)
        if old_inherit is _UNSET:
            del registry._INPROC_RUNTIME_ENV.inherit_process_context
        else:
            registry._INPROC_RUNTIME_ENV.inherit_process_context = old_inherit
        registry.set_inproc_cancel_check(old_check)
        registry._INPROC_CANCEL_CHECK.cancelled = old_cancelled
        registry.set_inproc_event_sink(old_sink)


def _bind_runtime_call(fn, *args, capability_policy=None, _binding=None):
    """现有工具线程池不会继承 threading.local，提交前捕获、线程内临时绑定。"""
    if _binding is None:
        registry, env, check = _runtime_binding()
        inherit = getattr(registry._INPROC_RUNTIME_ENV, "inherit_process_context", True)
    else:
        # 仅供调用入口复用授权时已捕获的可信现场；不接收模型参数中的 binding。
        env, check, inherit = _binding
    env = dict(env)
    if capability_policy is not None:
        env["MEYO_CAPABILITY_POLICY_JSON"] = json.dumps(_narrow_policy(
            _json_env(env, "MEYO_CAPABILITY_POLICY_JSON"), capability_policy))

    def call():
        with _runtime_scope(env, check, inherit_process_context=inherit):
            return fn(*args)
    return call


def _object(value, name):
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return dict(value)


def _json_env(env, name, kind=dict):
    if name not in env:
        return kind()
    value = json.loads(env[name])
    if not isinstance(value, kind):
        raise ValueError(f"{name} must be {kind.__name__}")
    return value


def _narrow_policy(parent, requested):
    import legion_permissions
    from legion_contracts import CapabilityPolicy
    defaults = legion_permissions.capability_policy()
    parent, requested = _object(parent, "capability_policy"), _object(requested, "capability_policy")
    # 原归一入口接受 dict；先拒绝 "false"/null 等歧义类型，不能把它们当授权 True。
    for values in (parent, requested):
        for name, value in values.items():
            if name in defaults and not isinstance(value, type(defaults[name])):
                raise ValueError(f"capability_policy.{name} has an invalid type")
            if name == "confirm_required" and any(not isinstance(item, str) for item in value):
                raise ValueError("capability_policy.confirm_required must be a string list")
    bound = CapabilityPolicy.from_value(parent).to_dict()
    child = CapabilityPolicy.from_value({**bound, **requested}).to_dict()
    # 这里只求能力交集；动作归类和最终 Allow/Ask/Deny 仍唯一走 authorize_tool。
    for name, value in bound.items():
        if isinstance(value, bool):
            child[name] = value and child[name]
    child["confirm_required"] = sorted(set(bound["confirm_required"]) | set(child["confirm_required"]))
    return {**defaults, **child, "mode": parent.get("mode", defaults["mode"])}


def _call_kwargs(values):
    """两入口保留相同字段；缺省与显式空值不能用 `or` 混在一起。"""
    result = {key: values[key] for key in _CALL_FIELDS if key in values}
    if "uid" in values:
        if "user_id" in result and result["user_id"] != values["uid"]:
            raise ValueError("uid conflicts with user_id")
        result["user_id"] = values["uid"]
    if "_capability_policy" in values:
        result["capability_policy"] = _narrow_policy(
            values["_capability_policy"], result.get("capability_policy", {}))
    return result


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _bound_text(explicit, env, name, fallback):
    bound = _text(env[name], name) if name in env else fallback
    if explicit is _UNSET:
        return bound
    value = _text(explicit, name)
    if name in env and value != bound:
        raise PermissionError(f"{name} conflicts with the bound caller")
    return value


def _directory(value):
    path = pathlib.Path(_text(value, "workdir"))
    if not path.is_absolute():
        raise ValueError("workdir must be absolute")
    return str(path.resolve(strict=True))


def _brief(value):
    if not isinstance(value, (str, dict)):
        raise ValueError("runtime_brief must be text or an object")
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            if isinstance(decoded, (str, dict)):
                return decoded
        except ValueError:
            pass  # 历史 MEYO_RUNTIME_BRIEF 合法地使用纯文本。
    return value


def inventory() -> dict:
    import runtime_manifest
    definitions = runtime_manifest.load_definitions()
    probe_error = ""
    try:
        external_rows = {row["key"]: row for row in external.probe().get("agents", [])
                         if isinstance(row, dict) and row.get("key")}
    except Exception as exc:
        external_rows = {}
        probe_error = f"{type(exc).__name__}: {exc}"
    declarations = []
    for provider in sorted(set(_BUILTIN) | set(definitions) | set(external_rows)):
        definition = definitions.get(provider) or {}
        runtime = definition.get("runtime") or {}
        row = external_rows.get(provider) or {}
        role = str(runtime.get("execution_role") or definition.get("execution_role")
                   or row.get("execution_role") or "agent")
        declared = runtime.get("tools", definition.get("tools",
                               row.get("tools", _BUILTIN.get(provider, []))))
        # A capability label is not a native tool name. No declaration means
        # no borrowed tool, rather than an invented shell/read/write default.
        names = (
            item if isinstance(item, str) else item.get("name", "")
            for item in declared if isinstance(item, (str, dict))
        ) if isinstance(declared, list) else ()
        tools = list(dict.fromkeys(name for name in names
                                   if isinstance(name, str) and name.strip()))
        ready = row.get("execution_ready") if isinstance(row.get("execution_ready"), bool) else None
        installed = row.get("installed") if isinstance(row.get("installed"), bool) else None
        reason = str(row.get("status_detail") or row.get("reason") or row.get("status")
                     or probe_error or "尚无运行就绪探测记录")
        if role in {"capability", "control-plane", "ui-platform"}:
            ready = False
            reason = f"execution_role={role}，不作为答题成员派发"
        declaration = {
            "provider": provider, "available": ready, "execution_ready": ready,
            "installed": installed, "execution_role": role, "reason": reason,
            "new_member": provider not in _BUILTIN, "tools": [
            {"id": f"agent_tool:{provider}:{tool}", "name": tool,
             "capability": _CAPS.get(tool, "tools"),
             "permissions": ["execute"] + (["filesystem"] if tool in ("read", "write", "shell", "bash", "terminal") else []),
             "isolation": "agent_runtime", "streaming": True}
            for tool in tools]}
        declarations.append(declaration)
    providers = [row for row in declarations if row["execution_ready"] is True and row["tools"]]
    return {"ok": True, "providers": providers, "declarations": declarations,
            "probe_error": probe_error or None,
            "summary": {"providers": len(providers), "tools": sum(len(p["tools"]) for p in providers),
                        "declared_providers": len(declarations),
                        "declared_tools": sum(len(p["tools"]) for p in declarations)}}


def search(query: str) -> dict:
    terms = set(str(query).lower().replace("_", " ").split())
    rows = []
    for provider in inventory()["declarations"]:
        for tool in provider["tools"]:
            hay = f"{tool['name']} {tool['capability']} {provider['provider']}".lower()
            score = sum(term in hay for term in terms)
            if score:
                rows.append({**tool, **{key: provider[key] for key in (
                    "provider", "installed", "execution_ready", "available", "execution_role", "reason")},
                    "score": score})
    rows.sort(key=lambda row: (-row["score"], row["id"]))
    return {"ok": True, "query": query, "results": rows}


def invoke(provider: str, tool: str, arguments: dict[str, Any], *, task: str = "",
           workdir=_UNSET, user_id=_UNSET, session_id=_UNSET, project_context=_UNSET,
           task_facts=_UNSET, runtime_brief=_UNSET, capability_policy=_UNSET,
           cancel_check=_UNSET, attempt_timeout=_UNSET, direction=_UNSET,
           execution_depth=_UNSET) -> dict:
    dispatch_attempted = False
    try:
        import legion_core
        import legion_permissions
        import legion_sandbox
        registry, env, parent_cancel = _runtime_binding()
        arguments = _object(arguments, "arguments")
        if not isinstance(task, str):
            raise ValueError("task must be text")
        uid = _bound_text(user_id, env, "MEYO_USER_ID", "default")
        parent_session = _bound_text(session_id, env, "MEYO_SESSION_ID", "")
        parent_project = _json_env(env, "MEYO_PROJECT_CONTEXT_JSON")
        parent_facts = _json_env(env, "MEYO_TASK_FACTS_JSON")
        extra_facts = {} if task_facts is _UNSET else _object(task_facts, "task_facts")
        if project_context is _UNSET and "project_context" in extra_facts:
            project_context = extra_facts["project_context"]
        explicit_project = ({} if project_context is _UNSET
                            else _object(project_context, "project_context"))
        if project_context is not _UNSET and not explicit_project:
            raise ValueError("explicit project_context cannot be empty")
        for project in (parent_project, explicit_project):
            if "user_id" in project and _text(project["user_id"], "project user_id") != uid:
                raise PermissionError("project workspace belongs to another user")
        if (getattr(registry._INPROC_RUNTIME_ENV, "inherit_process_context", True) is False
                and not (parent_project.get("repo_root") or parent_project.get("workspace_root"))
                and (workdir is not _UNSET or explicit_project.get("repo_root")
                     or explicit_project.get("workspace_root"))):
            # A checked HTTP user is not proof that a caller-supplied directory
            # belongs to them. No parent project still permits the normal fresh
            # workspace allocation below; ordinary tool arguments are untouched.
            raise PermissionError("explicit workspace requires an existing owned project")
        # 所有已声明目录必须指向同一个物理工作根，不能借标签覆盖原调用者现场。
        directories = []
        for project in (parent_project, explicit_project):
            root = project.get("repo_root") or project.get("workspace_root")
            if root:
                directories.append(_directory(root))
        if "MEYO_WORKDIR" in env:
            directories.append(_directory(env["MEYO_WORKDIR"]))
        if workdir is not _UNSET:
            directories.append(_directory(workdir))
        if len(set(directories)) > 1:
            raise PermissionError("workdir conflicts with the bound project")
        project = {**parent_project, **explicit_project, "user_id": uid}
        if directories and not (project.get("repo_root") or project.get("workspace_root")):
            # 比较用 realpath，实际 resolver 仍接原路径，保留 outputs 内 symlink 越界检查。
            project["workspace_root"] = (workdir if workdir is not _UNSET else env["MEYO_WORKDIR"])
        # qhash 是每次执行的流水号；即使同父任务并发借同一家，也不能复用其原生槽。
        execution_id = "tool-provider-" + uuid.uuid4().hex
        actual_workdir = legion_sandbox.workdir(provider, execution_id, project, uid)
        if not (project.get("repo_root") or project.get("workspace_root")):
            project["workspace_root"] = actual_workdir
        policy = _narrow_policy(_json_env(env, "MEYO_CAPABILITY_POLICY_JSON"),
                                {} if capability_policy is _UNSET else capability_policy)
        authorization = legion_permissions.authorize_tool(f"agent_tool:{provider}:{tool}", arguments, policy)
        if not authorization["allowed"]:
            raise PermissionError(f"CAPABILITY_DENIED:{authorization['action']}")
        parent_brief = _brief(env.get("MEYO_RUNTIME_BRIEF", ""))
        supplied_brief = parent_brief if runtime_brief is _UNSET else _brief(runtime_brief)
        parent_memory = _json_env(env, "MEYO_TASK_MEMORY_JSON")
        memory_ref = parent_memory.get("context_ref")
        if not isinstance(memory_ref, dict):
            import task_memory as memory_store
            parent_key = parent_session.split(":", 1)[0]
            path = memory_store._path(f"conv:{parent_session}", parent_key)
            available = bool(parent_memory and "MEYO_SESSION_ID" in env and path.is_file()
                             and memory_store.context(f"conv:{parent_session}", parent_key) == parent_memory)
            memory_ref = {"kind": "task_memory", "session_id": parent_session,
                          "available": available}
            if memory_ref["available"]:
                memory_ref["path"] = str(path.resolve())
        source = {
            "user_id": uid, "session_id": parent_session, "workdir": actual_workdir,
            "project_context_ref": (project.get("context_ref")
                                    or {"conversation_id": project.get("conversation_id", ""),
                                        "packet_path": project.get("packet_path", "")}),
            "task_facts": parent_facts if "MEYO_TASK_FACTS_JSON" in env else extra_facts,
            "runtime_brief": parent_brief if "MEYO_RUNTIME_BRIEF" in env else supplied_brief,
            "task_memory_ref": memory_ref,
            "prior_attempts": _json_env(env, "MEYO_PRIOR_ATTEMPTS", list),
        }
        facts = {**parent_facts, **extra_facts, "project_context": project}
        for key in ("_native_resume_conversation", "native_checkpoint", "native_conversation_id"):
            facts.pop(key, None)  # 父恢复记录只在 borrowed_from 中，不是借来成员自己的记录。
        brief = {"borrowed_tool": tool, "arguments": arguments,
                 "caller": "legion_tool_factory", "borrowed_from": source}
        if runtime_brief is not _UNSET:
            brief["runtime_brief"] = supplied_brief
        now = time.monotonic()
        deadline = float(env.get("MEYO_TASK_DEADLINE", 0))
        if not math.isfinite(deadline) or deadline < 0:
            raise ValueError("MEYO_TASK_DEADLINE must be finite and non-negative")
        if attempt_timeout is not _UNSET:
            limit = float(attempt_timeout)
            if not math.isfinite(limit) or limit < 0:
                raise ValueError("attempt_timeout must be finite and non-negative")
            if limit > 0:
                deadline = min(deadline, now + limit) if deadline else now + limit
        if deadline and deadline <= now:
            raise TimeoutError("borrowed task deadline exhausted")
        if cancel_check is not _UNSET and not callable(cancel_check):
            raise ValueError("cancel_check must be callable")
        checks = [check for check in (parent_cancel, cancel_check) if callable(check)]

        def cancelled():
            return any(check() for check in checks) or legion_core.is_cancelled(execution_id, provider)

        if cancelled():
            return {"ok": False, "provider": provider, "tool": tool,
                    "error": "caller cancelled", "status": "cancelled",
                    "dispatch_attempted": False, "dispatched": False}
        declaration = next((row for row in inventory()["declarations"]
                            if row["provider"] == provider), {})
        if tool not in {item["name"] for item in declaration.get("tools", [])}:
            return {"ok": False, "error": f"unknown agent tool:{provider}/{tool}",
                    "dispatch_attempted": False, "dispatched": False}
        if declaration.get("execution_ready") is False:
            return {"ok": False, "provider": provider, "tool": tool,
                    "error": declaration["reason"], "status": "unavailable",
                    "dispatch_attempted": False, "dispatched": False}
        # Unknown readiness is not a prohibition: an explicit declared-tool
        # request may try the original executor without inventing install facts.
        prompt = task or f"请使用你的原生工具 {tool} 完成以下调用，返回真实结果。参数：{json.dumps(arguments, ensure_ascii=False)}"
        with _runtime_scope({**env, "MEYO_TASK_DEADLINE": str(deadline)}, cancelled):
            dispatch_attempted = True
            result = legion_core.run_one(
                provider, prompt, qhash=execution_id, conversation_id=execution_id,
                user_id=uid, runtime_brief=brief, task_facts=facts, capability_policy=policy,
                cancel_check=cancelled, attempt_timeout=deadline - now if deadline else None,
                direction=env.get("MEYO_DIRECTION", "programming") if direction is _UNSET else direction,
                execution_depth=(env.get("MEYO_EXECUTION_DEPTH", "standard")
                                 if execution_depth is _UNSET else execution_depth))
        started = result.get("execution_started")
        if not isinstance(started, bool):
            started = True if result.get("status") in {"done", "running"} else None
        return {"ok": result.get("status") == "done", "provider": provider, "tool": tool,
                "result": result, "workdir": result.get("workdir", ""),
                "error": result.get("error", ""),
                "dispatch_attempted": True, "dispatched": started}
    except Exception as exc:
        return {"ok": False, "provider": provider, "tool": tool,
                "error": f"{type(exc).__name__}:{exc}",
                "dispatch_attempted": dispatch_attempted,
                "dispatched": None if dispatch_attempted else False}
