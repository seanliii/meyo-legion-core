#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DeepAgents 原生 SDK worker；保留历史消息 JSON 的兼容读写入口。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def load(path: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except (OSError, ValueError, TypeError):
        return []


def save(path: str, messages: list[Any]) -> None:
    serializable = []
    for message in messages:
        if hasattr(message, "model_dump"):
            item = message.model_dump(mode="json")
        elif isinstance(message, dict):
            item = message
        else:
            item = {"type": "ai", "content": str(getattr(message, "content", message))}
        serializable.append(item)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + f".tmp.{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(serializable, ensure_ascii=False))
    tmp.chmod(0o600)
    os.replace(tmp, target)


def _native_backend(workdir, environment, error_filter=str):
    """Keep native filesystem tools; delegate shell lifecycle to the existing runner."""
    import subprocess
    import sys
    from deepagents.backends import LocalShellBackend
    from deepagents.backends.protocol import ExecuteResponse

    service = Path(__file__).resolve().parent
    candidates = [os.environ.get("MEYO_ORCH_DIR"),
                  str(service.parent / "orchestrator"),
                  str(service.parent / "meyo-orchestrator"),
                  str(service.parents[1] / "harness_baselines/meyo-orchestrator")]
    for directory in candidates:
        if directory and Path(directory, "harness_registry.py").is_file():
            if directory not in sys.path:
                sys.path.insert(0, directory)
            break
    from harness_registry import _run_live

    class RegisteredShellBackend(LocalShellBackend):
        def _resolve_path(self, key: str) -> Path:
            # Platform context and execute use host coordinates; native file tools
            # use virtual /. Alias only paths lexically inside this exact root,
            # then retain upstream traversal and resolved-symlink containment checks.
            if self.virtual_mode and Path(key).is_absolute():
                try:
                    relative = Path(key).relative_to(self.cwd)
                except ValueError:
                    pass  # Other absolute paths keep virtual semantics, never host access.
                else:
                    key = "/" + relative.as_posix()
            return super()._resolve_path(key)

        def execute(self, command, *, timeout=None):
            if not isinstance(command, str) or not command:
                return ExecuteResponse(output="Error: command must be a non-empty string.",
                                       exit_code=1, truncated=False)
            limit = self._default_timeout if timeout is None else timeout
            if limit <= 0:
                raise ValueError("timeout must be positive")
            argv = ([os.environ.get("COMSPEC", "cmd.exe"), "/c", command]
                    if os.name == "nt" else ["/bin/sh", "-c", command])
            try:
                done = _run_live(argv, limit, cwd=str(self.cwd), env=self._env,
                                 tag="deepagents-execute")
                parts = [done.stdout] if done.stdout else []
                if done.stderr:
                    parts.extend("[stderr] " + line for line in done.stderr.strip().splitlines())
                output = "\n".join(parts) or "<no output>"
                truncated = len(output) > self._max_output_bytes
                if truncated:
                    output = output[:self._max_output_bytes] + "\n... Output truncated"
                if done.returncode:
                    output = f"{output.rstrip()}\n\nExit code: {done.returncode}"
                return ExecuteResponse(output=output, exit_code=done.returncode, truncated=truncated)
            except subprocess.TimeoutExpired:
                return ExecuteResponse(output=f"Error: command timed out after {limit} seconds.",
                                       exit_code=124, truncated=False)
            except Exception as exc:
                return ExecuteResponse(
                    output=f"Error executing command ({type(exc).__name__}): {error_filter(exc)}",
                    exit_code=1, truncated=False)

    return RegisteredShellBackend(root_dir=workdir, virtual_mode=True, env=environment)


def run_native(request: dict) -> dict:
    """Run upstream DeepAgents once; its graph owns tools, subagents and recovery.

    The old load/save API remains for legacy message files. New runs use the
    upstream SQLite checkpointer, including native todos and pending graph state.
    """
    import hashlib
    import importlib.metadata
    import shlex
    import sqlite3
    import uuid
    from contextlib import closing

    result = {"status": "error", "final": "", "usage_records": [],
              "native_facts": {"engine": "deepagents", "completed": False}}
    facts = result["native_facts"]

    def safe_error(message):
        text = str(message)
        secrets = {str(request.get("appid") or "")}
        secrets.update(os.environ.get(name, "") for name in (
            "AIGC_APPID", "MEYO_UNIFIED_APPID", "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"))
        for secret in sorted((value for value in secrets if value), key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        return text

    def fail(message, category="harness_runtime"):
        result["error"] = safe_error(message)
        facts["failed"] = True
        result["error_envelope"] = {
            "category": category, "retryable": False, "scope": "harness",
            "message": safe_error(message)}
        return result

    try:
        from deepagents import create_deep_agent
        from langchain.agents.middleware import AgentMiddleware, TodoListMiddleware
        from langchain_core.callbacks import BaseCallbackHandler
        from langchain_core.messages import AIMessage, HumanMessage, convert_to_messages
        from langchain_core.tools import tool
        from langchain_openai import ChatOpenAI
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError as exc:
        return fail(f"DeepAgents native dependency unavailable: {exc}")

    try:
        facts["version"] = importlib.metadata.version("deepagents")
    except importlib.metadata.PackageNotFoundError:
        facts["version"] = None
    workdir = Path(request["workdir"]).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    result["workdir"] = str(workdir)
    checkpoint = request.get("native_checkpoint") or {}
    if not isinstance(checkpoint, dict):
        return fail("invalid DeepAgents checkpoint", "native_session_unavailable")
    kind = checkpoint.get("kind")
    if checkpoint and kind not in ("deepagents_messages", "deepagents_langgraph_sqlite"):
        return fail("unsupported DeepAgents checkpoint kind", "native_session_unavailable")
    thread_id = str(checkpoint.get("session_id") if kind == "deepagents_langgraph_sqlite"
                    else request.get("session_id") or request.get("qhash") or "")
    if not thread_id:
        return fail("DeepAgents requires a logical session id", "native_session_unavailable")
    folder = workdir / ".sessions"
    if folder.resolve().parent != workdir:
        return fail("DeepAgents session directory escapes workdir", "native_session_unavailable")
    folder.mkdir(mode=0o700, exist_ok=True)
    identity = json.dumps([request.get("user_id") or "default", thread_id]).encode()
    path = folder / ("deepagents-native-" + hashlib.sha256(identity).hexdigest()[:24] + ".sqlite3")
    if kind == "deepagents_langgraph_sqlite":
        if not checkpoint.get("path"):
            return fail("native checkpoint path missing", "native_session_unavailable")
        path = Path(checkpoint["path"])
        if (path.is_symlink() or not path.is_file() or path.resolve().parent != folder.resolve()
                or checkpoint.get("workdir") != str(workdir)):
            return fail("native checkpoint/project unavailable", "native_session_unavailable")
    elif path.is_symlink():
        return fail("unsafe native checkpoint path", "native_session_unavailable")

    legacy = []
    if kind == "deepagents_messages":
        try:
            legacy_path = Path(str(checkpoint.get("session_id") or ""))
            if legacy_path.is_symlink() or legacy_path.resolve().parent != folder.resolve():
                raise ValueError("legacy checkpoint outside project")
            legacy = json.loads(legacy_path.read_text())
            if not isinstance(legacy, list) or not legacy:
                raise ValueError("legacy checkpoint has no message history")
            legacy = convert_to_messages(legacy)
            for message in legacy:
                if not message.id:
                    message.id = str(uuid.uuid4())
        except (OSError, ValueError, TypeError) as exc:
            result["native_resume_error"] = {"stage": "legacy_load"}
            return fail(exc, "native_session_unavailable")

    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    path.chmod(0o600)
    config = {"configurable": {"thread_id": thread_id}}
    reference = {"kind": "deepagents_langgraph_sqlite", "session_id": thread_id,
                 "path": str(path), "workdir": str(workdir), "qhash": request.get("qhash")}
    try:
        with closing(sqlite3.connect(str(path), check_same_thread=False)) as connection:
            saver = SqliteSaver(connection)
            existing = saver.get_tuple(config)
            if kind == "deepagents_langgraph_sqlite" and existing is None:
                result["native_resume_error"] = {"stage": "checkpoint_load"}
                return fail("native thread is absent from checkpoint", "native_session_unavailable")
            options = {"model": request["model"], "base_url": request["gateway"],
                       "api_key": request["appid"], "timeout": 90, "max_retries": 3}
            requested_limit = max(0, int(request.get("max_tokens") or 0))
            gateway_limit = int(request.get("gateway_output_limit") or 0)
            if gateway_limit > 0:
                output_limit = min(requested_limit or gateway_limit, gateway_limit)
                # These declared gateway models use max_tokens. ChatOpenAI
                # otherwise renames it to max_completion_tokens on the wire.
                options["extra_body"] = {"max_tokens": output_limit}
                facts["output_limit"] = output_limit
            elif requested_limit > 0:
                options["max_tokens"] = requested_limit
            model = ChatOpenAI(**options)
            environment = {**os.environ, "MEYO_WORKDIR": str(workdir),
                           "MEYO_SESSION_ID": thread_id,
                           "MEYO_USER_ID": str(request.get("user_id") or "default"),
                           "AIGC_APPID": request["appid"], "MEYO_UNIFIED_APPID": request["appid"],
                           "MEYO_UNIFIED_GATEWAY": request["gateway"],
                           "MEYO_UNIFIED_MODEL": request["model"],
                           "OPENAI_API_KEY": request["appid"], "OPENAI_BASE_URL": request["gateway"]}
            backend = _native_backend(workdir, environment, safe_error)
            tools = []
            if request.get("legion_tool") and Path(request["legion_tool"]).is_file():
                @tool
                def discover_tools(query: str) -> str:
                    """Discover shared legion capabilities; returns their native CLI entrypoints."""
                    command = shlex.join([request["python"], request["legion_tool"], "search", query])
                    return backend.execute(command).output
                tools.append(discover_tools)
            if request.get("search_cli") and Path(request["search_cli"]).is_file():
                @tool
                def web_search(query: str) -> str:
                    """Search through the existing shared search CLI."""
                    command = shlex.join([request["python"], request["search_cli"], query, "--top-k", "8"])
                    return backend.execute(command).output
                tools.append(web_search)

            def project_model_history(model_request):
                # Retain original checkpoint evidence; omit only unusable empty
                # completions from the next provider's request, not tool messages.
                messages = [
                    message for message in model_request.messages
                    if not (isinstance(message, AIMessage) and not message.content
                            and not message.tool_calls and not message.invalid_tool_calls
                            and not any(message.additional_kwargs.values()))
                ]
                omitted = len(model_request.messages) - len(messages)
                if omitted:
                    facts["omitted_empty_assistant_messages"] = (
                        facts.get("omitted_empty_assistant_messages", 0) + omitted)
                    model_request = model_request.override(messages=messages)
                return model_request

            class NativeHistoryProjection(AgentMiddleware):
                def wrap_model_call(self, model_request, handler):
                    return handler(project_model_history(model_request))

                async def awrap_model_call(self, model_request, handler):
                    return await handler(project_model_history(model_request))

            agent = create_deep_agent(
                model=model, backend=backend, checkpointer=saver, tools=tools,
                middleware=[TodoListMiddleware(), NativeHistoryProjection()])
            previous = agent.get_state(config)
            result["resumed_native"] = existing is not None
            if legacy and existing is None:
                facts["legacy_messages_imported"] = True
            question = request["question"]
            question = (f"Runtime filesystem: virtual / maps to {workdir}; "
                        "execute uses this real working directory.\n" + question)
            incoming = {"messages": ([] if existing else legacy) +
                        [HumanMessage(content=question, id=str(uuid.uuid4()))]}
            # Resume an unfinished graph only for the same parent request.
            if previous.next and checkpoint.get("qhash") == request.get("qhash"):
                incoming = None

            class NativeUsage(BaseCallbackHandler):
                # Native callbacks propagate through task/subagent calls, unlike
                # reading only the parent graph's final message list.
                run_inline = True

                def __init__(self):
                    import sys
                    import threading
                    self.stream = sys.stdout
                    self.lock = threading.RLock()
                    self.active = {}

                def emit(self, event):
                    # JSON line protocol only: never serialize prompts, reasoning,
                    # tool arguments/results or arbitrary native console output.
                    try:
                        self.stream.write(json.dumps(event, ensure_ascii=False) + "\n")
                        self.stream.flush()
                    except (OSError, ValueError):
                        pass  # Observability must not abort native execution.

                def start(self, run_id, kind, name="", budget=None):
                    if run_id is None:
                        return
                    call_id = str(run_id)
                    with self.lock:
                        if call_id in self.active:
                            return  # Duplicate start cannot extend the same lease.
                        self.active[call_id] = (kind, name)
                        event = {"type": "runtime_activity", "call_id": call_id, "phase": "start"}
                        if type(budget) in (int, float) and 0 < budget < float("inf"):
                            event["budget_s"] = budget
                        self.emit(event)
                        if kind == "tool":
                            self.emit({"type": "assistant", "message": {"content": [{
                                "type": "tool_use", "id": call_id, "name": name, "input": {},
                            }]}})

                def finish(self, run_id, failed=None):
                    with self.lock:
                        call_id = str(run_id)
                        entry = self.active.pop(call_id, None)
                        if entry is None:
                            return
                        self.emit({"type": "runtime_activity", "call_id": call_id, "phase": "end"})
                        if entry[0] == "tool":
                            self.emit({"type": "user", "message": {"content": [{
                                "type": "tool_result", "tool_use_id": call_id, "name": entry[1],
                                "is_error": failed, "content": "",
                                **({"status": "returned"} if failed is None else {}),
                            }]}})

                def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
                    self.start(run_id, "provider", budget=options["timeout"])

                def on_llm_start(self, serialized, prompts, *, run_id, **kwargs):
                    self.start(run_id, "provider", budget=options["timeout"])

                def on_llm_error(self, error, *, run_id, **kwargs):
                    self.finish(run_id, failed=True)

                def on_tool_start(self, serialized, input_str, *, run_id, inputs=None, **kwargs):
                    name = str((serialized or {}).get("name") or "tool")
                    budget = None
                    # Only these tools delegate to this backend. A task/subagent's
                    # duration is not the shell timeout; filesystem tools have none.
                    if name in {"execute", "discover_tools", "web_search"}:
                        budget = getattr(backend, "_default_timeout", None)
                        if name == "execute" and isinstance(inputs, dict) and inputs.get("timeout") is not None:
                            budget = inputs["timeout"]
                    self.start(run_id, "tool", safe_error(name), budget)

                def on_tool_end(self, output, *, run_id, **kwargs):
                    artifact = getattr(output, "artifact", None)
                    exit_code = artifact.get("exit_code") if isinstance(artifact, dict) else None
                    status = getattr(output, "status", None)
                    failed = (True if status == "error" else exit_code != 0
                              if type(exit_code) is int else False
                              if status == "success" else None)
                    self.finish(run_id, failed=failed)

                def on_tool_error(self, error, *, run_id, **kwargs):
                    self.finish(run_id, failed=True)

                def on_llm_end(self, response, **kwargs):
                    self.finish(kwargs.get("run_id"))
                    if not response.generations or not response.generations[0]:
                        return
                    message = getattr(response.generations[0][0], "message", None)
                    if not isinstance(message, AIMessage):
                        return
                    finish_reason = message.response_metadata.get("finish_reason")
                    if finish_reason is not None:
                        facts["last_model_finish_reason"] = finish_reason
                    usage = message.usage_metadata or message.response_metadata.get("token_usage")
                    if isinstance(usage, dict):
                        result["usage_records"].append({
                            **usage, "source": "deepagents_native_message",
                            "model": message.response_metadata.get("model_name") or request["model"]})

            try:
                agent.invoke(incoming, config={**config, "callbacks": [NativeUsage()]})
            except Exception as exc:
                result["error"] = safe_error(exc)
                facts["failed"] = True
                facts["exception_type"] = type(exc).__name__
            state = agent.get_state(config)
            messages = state.values.get("messages") or []
            if saver.get_tuple(config) is not None:
                result["native_checkpoint"] = reference
            facts["pending_nodes"] = list(state.next)
            facts["checkpoint_backend"] = "langgraph.sqlite"
            if not facts.get("failed"):
                last = messages[-1] if messages else None
                final = last.text if isinstance(last, AIMessage) and not last.tool_calls else ""
                finish_reason = (last.response_metadata.get("finish_reason")
                                 if isinstance(last, AIMessage) else None)
                interrupted = finish_reason in ("length", "max_tokens")
                complete = not state.next and bool(final.strip()) and not interrupted
                result.update(status="ok" if complete else "incomplete",
                              final=final, model_used=request["model"])
                facts["completed"] = complete
                if interrupted:
                    result["error"] = "Native model response reached its output limit."
                    result["error_envelope"] = {
                        "category": "truncated", "retryable": True, "scope": "request",
                        "message": result["error"]}
    except Exception as exc:
        return fail(exc, "native_session_unavailable" if checkpoint else "harness_runtime")
    return result
