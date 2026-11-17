#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把每个 Agent 的原生工具暴露成军团共享 capability provider。"""
from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
import pathlib
import secrets
import shutil
import tempfile
import threading
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
# Host credentials never enter task facts or the native process environment.
_PRIVATE_CALL = threading.local()
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
def _runtime_scope(env, cancel_check, *, inherit_process_context=None,
                   personal_token=_UNSET, native_onboard=_UNSET):
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
    old_token = getattr(_PRIVATE_CALL, "personal_token", _UNSET)
    old_onboard = getattr(_PRIVATE_CALL, "native_onboard", _UNSET)
    if native_onboard is not _UNSET:
        _PRIVATE_CALL.native_onboard = native_onboard
    elif not inherit:
        _PRIVATE_CALL.native_onboard = None
    if personal_token is not _UNSET:
        _PRIVATE_CALL.personal_token = personal_token
    elif not inherit:
        _PRIVATE_CALL.personal_token = ""
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
        for name, value in (("personal_token", old_token), ("native_onboard", old_onboard)):
            if value is _UNSET:
                if hasattr(_PRIVATE_CALL, name):
                    delattr(_PRIVATE_CALL, name)
            else:
                setattr(_PRIVATE_CALL, name, value)


def _bound_personal_token():
    """Never recover a credential from another caller's process environment."""
    return getattr(_PRIVATE_CALL, "personal_token", "")


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

    token = _bound_personal_token()
    onboard = _bound_native_onboard()

    def call():
        with _runtime_scope(env, check, inherit_process_context=inherit,
                            personal_token=token, native_onboard=onboard):
            return fn(*args)
    return call


def _bound_native_onboard():
    return getattr(_PRIVATE_CALL, "native_onboard", None)


def _bound_native_onboard_environment() -> dict:
    lease = _bound_native_onboard()
    return lease.environment() if lease is not None else {}


class _NativeOnboardHandler(BaseHTTPRequestHandler):
    """A run-owned install action, not a personal login or arbitrary tool host."""
    def log_message(self, *_args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def _reply(self, code, result):
        raw = json.dumps(self.server.lease.redact(result), ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        lease = self.server.lease
        if (self.path != "/onboard" or self.headers.get("Host") != lease.authority
                or self.headers.get("Origin") not in (None, "", "http://" + lease.authority)):
            return self._reply(403, {"ok": False, "error": "ONBOARD_REQUEST_SCOPE"})
        auth = self.headers.get("Authorization", "")
        if not auth.isascii() or not hmac.compare_digest(auth, "Bearer " + lease._token):
            return self._reply(401, {"ok": False, "error": "ONBOARD_LEASE_AUTH_REQUIRED"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if (self.headers.get("Transfer-Encoding") or not 0 < length <= 1024 * 1024
                    or self.headers.get("Content-Type", "").split(";")[0] != "application/json"):
                raise ValueError
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError
            data = json.loads(body)
            if (not isinstance(data, dict) or set(data) != {"uid", "session_id", "workdir", "params"}
                    or data["uid"] != lease.user_id or data["session_id"] != lease.session_id
                    or data["workdir"] != str(lease.workdir) or not isinstance(data["params"], dict)):
                raise ValueError
        except (ValueError, TypeError, OSError):
            return self._reply(400, {"ok": False, "error": "ONBOARD_LEASE_SCOPE_MISMATCH"})
        if not lease._call_lock.acquire(blocking=False):
            return self._reply(409, {"ok": False, "error": "ONBOARD_ALREADY_RUNNING"})
        try:
            lease.check()
            import legion_tool
            context = {"MEYO_USER_ID": lease.user_id, "MEYO_SESSION_ID": lease.session_id,
                       "MEYO_WORKDIR": str(lease.workdir),
                       "MEYO_CAPABILITY_POLICY_JSON": json.dumps(lease.policy),
                       "MEYO_PROJECT_CONTEXT_JSON": json.dumps(lease.project_context)}
            # Preserve the public instance's model/profile binding, never host fallback.
            context = {**lease._runtime_env, **context}
            with _runtime_scope(context, lease.cancelled, inherit_process_context=False,
                                personal_token=lease._personal_token, native_onboard=lease):
                result = legion_tool.run_tool("runnable:harness_onboard", data["params"])
            self._reply(200, result)
        except PermissionError:
            self._reply(403, {"ok": False, "error": "ONBOARD_LEASE_NOT_AUTHORIZED"})
        except Exception:
            self._reply(500, {"ok": False, "error": "ONBOARD_LEASE_EXECUTION_FAILED"})
        finally:
            lease._call_lock.release()


class _NativeOnboardServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True
    request_queue_size = 2

    def __init__(self, lease):
        self.lease = lease
        self._requests = threading.BoundedSemaphore(2)
        super().__init__(("127.0.0.1", 0), _NativeOnboardHandler)

    def process_request(self, request, client_address):
        if not self._requests.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._requests.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._requests.release()


def _onboard_catalog():
    import personal_identity
    path = personal_identity._absolute_unlinked_path(str(external.CATALOG), directory=False)
    info = path.stat()
    if info.st_nlink != 1 or info.st_uid not in (0, os.getuid()):
        raise PermissionError("native onboard catalog must be independently owned")
    return path


class _NativeOnboardLease:
    def __init__(self, *, user_id, session_id, owner_session, workdir, policy,
                 cancel_check, project_context, personal_token, deadline=0):
        import personal_identity
        self.user_id, self.session_id, self.owner_session = user_id, session_id, owner_session
        self.workdir = personal_identity._absolute_unlinked_path(workdir)
        self.catalog = _onboard_catalog()
        self.policy, self.project_context = dict(policy), dict(project_context or {})
        self._personal_token, self._cancel_check = personal_token, cancel_check
        self._closed, self._call_lock = threading.Event(), threading.Lock()
        self._deadline = deadline if deadline > 0 else time.monotonic() + 1800
        self._token, self.authority = secrets.token_urlsafe(32), ""
        self._host_env = dict(os.environ)
        _, self._runtime_env, _ = _runtime_binding()
        # Profile storage is captured from the trusted caller, not another
        # user's process-global data root or the registry's import-time state.
        self._host_env.update(self._runtime_env)
        data_root = self._runtime_env.get("MEYO_LEGION_DIR")
        repos = (pathlib.Path(data_root) / "repos" if data_root
                 else pathlib.Path(external._paths.repos_dir()))
        state = (pathlib.Path(data_root) / "external_agents" if data_root
                 else external._STATE_DIR)
        self._install_roots = (self.workdir,
            (repos / "external-agents").resolve(),
            pathlib.Path(self._host_env.get("LEGION_EXTERNAL_RUNTIME_ROOT")
                         or state / "runtimes").resolve())
        self._authorized_paths = set()
        self._launch_policy = None
        self.check()

    def __repr__(self):
        return "<NativeOnboardLease run-owned install capability>"

    def cancelled(self):
        return (self._closed.is_set() or time.monotonic() >= self._deadline
                or bool(self._cancel_check and self._cancel_check()))

    def check(self):
        import personal_identity
        import sessions
        if (self.cancelled() or personal_identity.resolve_token(self._personal_token) != self.user_id
                or personal_identity.source_write_grant(self.user_id, str(self.workdir)) is None
                or self.policy.get("install") is not True
                or _onboard_catalog() != self.catalog
                or not self.catalog.is_relative_to(self.workdir)):
            raise PermissionError("native onboard authorization is unavailable")
        session = sessions.load_session(self.owner_session, user_id=self.user_id)
        if session is None:
            raise PermissionError("native onboard owner session is unavailable")
        turns = session.get("turns") or []
        latest = turns[-1] if turns else {}
        parent = (latest.get("input_snapshot") or {}).get("capability_policy", {})
        effective = _narrow_policy(self.policy, parent)
        if not all(effective.get(name) for name in ("install", "write", "execute")):
            raise PermissionError("native onboard parent denied installation")
        if self._launch_policy is not None and any(
                value is True and effective.get(name) is not True
                for name, value in self._launch_policy.items() if isinstance(value, bool)):
            raise PermissionError("native onboard launch authority was narrowed")
        return effective

    def environment(self):
        self.check()
        return {"MEYO_ONBOARD_BRIDGE_URL": "http://" + self.authority + "/onboard",
                "MEYO_ONBOARD_BRIDGE_TOKEN": self._token}

    def redact(self, value):
        if isinstance(value, str):
            for secret in (self._token, self._personal_token):
                if secret:
                    value = value.replace(secret, "[redacted]")
            return value
        if isinstance(value, dict):
            return {self.redact(key): self.redact(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(item) for item in value]
        return value

    def install_paths(self, item, destination, runtime_dir):
        self.check()
        import personal_identity
        protected = [pathlib.Path(__file__).resolve().parent,
                     personal_identity._operator_store_path().resolve().parent]
        protected.extend(pathlib.Path(self._host_env[name]).resolve()
                         for name in ("MEYO_RUNTIME_ROOT", "MEYO_TOOLBOX_ROOT", "LEGION_PROC_REGISTRY_DIR")
                         if self._host_env.get(name))
        key = item.get("key")
        if not isinstance(key, str) or not key or key in {".", ".."} or external._safe_key(key) != key:
            raise PermissionError("native onboard member key is not canonical")
        protected.append(self.catalog)
        paths = []
        for value, root in zip((destination, runtime_dir), self._install_roots[1:]):
            path = pathlib.Path(value)
            owned_source = path.is_relative_to(self.workdir) and path != self.workdir
            if (not path.is_absolute() or path.resolve() != path
                    or not (owned_source or path == root / key)
                    or any(path.is_relative_to(boundary) or boundary.is_relative_to(path)
                           for boundary in protected)):
                raise PermissionError("native onboard installation path is outside granted storage")
            paths.append(path)
        self._authorized_paths.update(paths)

    @contextmanager
    def research_scope(self, root):
        path = pathlib.Path(root).resolve(strict=True)
        if path.stat().st_mode & 0o077 or path.stat().st_uid != os.getuid():
            raise PermissionError("native onboard research directory is not private")
        self.check()
        self._authorized_paths.add(path)
        try:
            yield
        finally:
            self._authorized_paths.discard(path)

    @contextmanager
    def install_launch(self, command, cwd, env, key):
        policy = self.check()
        from native_process_sandbox import prepare_native_process, _sandbox_binary
        directory = pathlib.Path(cwd).resolve(strict=True)
        if (not directory.is_relative_to(self.workdir)
                and not any(directory == path or directory.is_relative_to(path)
                            or directory == path.parent for path in self._authorized_paths)):
            raise PermissionError("native onboard command directory is outside granted storage")
        selected = shutil.which("codex", path=self._host_env.get("PATH", ""))
        if not selected:
            raise PermissionError("native onboard requires the existing native write boundary")
        binary = pathlib.Path(selected).resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix="onboard-control-") as temporary:
            control = pathlib.Path(temporary).resolve()
            home = control / "build-home"
            home.mkdir(mode=0o700)
            (home / ".codex").mkdir(mode=0o700)
            build_fields = {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "CI",
                            "CC", "CXX", "CFLAGS", "CPPFLAGS", "CXXFLAGS", "LDFLAGS",
                            "PKG_CONFIG_PATH", "SDKROOT", "MACOSX_DEPLOYMENT_TARGET",
                            "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS"}
            child_env = {name: value for name, value in env.items() if name in build_fields}
            child_env["PYTHONNOUSERSITE"] = "1"
            for path in self._authorized_paths:
                if path.resolve() != path:
                    raise PermissionError("native onboard installation path changed")
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
            command_dir = directory
            if not directory.is_relative_to(self.workdir) and directory not in self._authorized_paths:
                command_dir = home / "command"
                command_dir.mkdir(mode=0o700)
            writable = list(self._authorized_paths)
            _sandbox_binary(binary, [command_dir, home, *writable])
            build_path = child_env.get("PATH", "")
            child_env.update(HOME=str(home), CODEX_HOME=str(home / ".codex"),
                             PATH=self._host_env.get("PATH", ""), MEYO_USER_ID=self.user_id,
                             MEYO_RUNTIME_STORE=str(control))
            with prepare_native_process(child_env, str(command_dir), backend="codex",
                    protected_roots=[binary.parent, pathlib.Path(__file__).resolve().parent],
                    writable_state_roots=[home, *writable], sandbox_executable=binary,
                    network_access=policy["network"]) as launch:
                launch.cwd = command_dir
                wrap = launch.wrap

                def checked_wrap(argv):
                    self.check()
                    if directory.resolve() != directory or any(
                            path.resolve() != path for path in self._authorized_paths):
                        raise PermissionError("native onboard installation path changed")
                    return wrap(["/usr/bin/env", "PATH=" + build_path, *argv])

                launch.wrap = checked_wrap
                previous = self._launch_policy
                self._launch_policy = policy
                try:
                    self.check()
                    yield launch
                    self.check()
                finally:
                    self._launch_policy = previous

    def pool_has(self, key):
        self.check()
        import legion_core
        rows = legion_core.list_harnesses(force=True)
        self.check()
        return any(row.get("key") == key for row in rows), "host catalog total=%d" % len(rows)


@contextmanager
def native_onboard_scope(*, user_id, session_id, owner_session, workdir,
                         capability_policy=None, cancel_check=None, project_context=None):
    """Delegate an authenticated run's install action; never issue a user token."""
    import personal_identity
    import sessions
    token = _bound_personal_token()
    if (not token or personal_identity.resolve_token(token) != user_id
            or not session_id or not owner_session
            or sessions.load_session(owner_session, user_id=user_id) is None
            or personal_identity.source_write_grant(user_id, workdir) is None):
        yield None
        return
    _, env, parent_check = _runtime_binding()
    requested_check = cancel_check
    cancel_check = lambda: bool(parent_check and parent_check()) or bool(requested_check and requested_check())
    deadline = float(env.get("MEYO_TASK_DEADLINE") or 0)
    if not math.isfinite(deadline) or deadline < 0:
        raise ValueError("native onboard deadline must be finite and non-negative")
    policy = _narrow_policy(_json_env(env, "MEYO_CAPABILITY_POLICY_JSON"), capability_policy or {})
    if not policy.get("install"):
        yield None
        return
    try:
        lease = _NativeOnboardLease(user_id=user_id, session_id=session_id,
            owner_session=owner_session, workdir=workdir, policy=policy,
            cancel_check=cancel_check, project_context=project_context,
            personal_token=token, deadline=deadline)
    except (PermissionError, OSError, ValueError):
        yield None
        return
    server = _NativeOnboardServer(lease)
    lease.authority = "127.0.0.1:%d" % server.server_port
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05),
                              name="native-onboard", daemon=True)
    thread.start()
    try:
        with _runtime_scope(env, cancel_check, personal_token=token, native_onboard=lease):
            yield lease
    finally:
        lease._closed.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


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
        # This child borrows the caller's authority, not process-global context.
        token = _bound_personal_token() if uid == env.get("MEYO_USER_ID") else ""
        onboard = _bound_native_onboard()
        if (onboard is not None and (onboard.user_id != uid
                or onboard.session_id != parent_session
                or str(onboard.workdir) != actual_workdir or not token)):
            onboard = None
        with _runtime_scope({**env, "MEYO_USER_ID": uid, "MEYO_WORKDIR": actual_workdir,
                             "MEYO_PROJECT_CONTEXT_JSON": json.dumps(project),
                             "MEYO_CAPABILITY_POLICY_JSON": json.dumps(policy),
                             "MEYO_TASK_DEADLINE": str(deadline)}, cancelled,
                            inherit_process_context=False,
                            personal_token=token, native_onboard=onboard):
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
