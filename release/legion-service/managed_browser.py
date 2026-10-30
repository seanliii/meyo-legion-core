"""One parent-owned browser capability; no business planning or quality decisions.

The native process keeps its existing filesystem boundary. This controller owns
a fresh browser and authenticated loopback MCP transport, not a personal browser
or an arbitrary command service. The browser implementation remains Playwright's.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import mimetypes
import os
from pathlib import Path
import secrets
import shutil
import stat
import sys
import tempfile
import threading
import time
from urllib.parse import quote, unquote, urlsplit


_TOOLS = frozenset({
    "browser_close", "browser_resize", "browser_console_messages", "browser_handle_dialog",
    "browser_evaluate", "browser_find", "browser_fill_form", "browser_press_key",
    "browser_type", "browser_navigate", "browser_navigate_back", "browser_network_requests",
    "browser_take_screenshot", "browser_snapshot", "browser_click", "browser_drag",
    "browser_hover", "browser_select_option", "browser_tabs", "browser_wait_for",
    "browser_mouse_click_xy", "browser_mouse_drag_xy", "browser_mouse_move_xy", "browser_mouse_wheel",
    "browser_recover",
})
_RECOVER_TOOL = {
    "name": "browser_recover",
    "description": (
        "Explicitly recover this owned browser capability after a transport failure. "
        "No arguments; keeps the same owner, workspace and permissions. Does not retry "
        "or replay any previous navigation/click. A fresh session has no previous pages "
        "or login state. Healthy, closed, cancelled or budget-exhausted sessions are not restarted."
    ),
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}
_ENV = frozenset({
    "PATH", "LD_LIBRARY_PATH", "LANG", "LC_ALL", "MEYO_RUNTIME_ROOT",
    "MEYO_RUNTIME_STORE", "MEYO_LEGION_DIR", "MEYO_PLAYWRIGHT_RUNTIME_PROFILE",
    "PLAYWRIGHT_MCP_EXECUTABLE_PATH", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE",
    "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
})
_MAX_REQUEST = 65536
_MAX_IMAGE = 32 * 1024 * 1024
_SCREENSHOT_RETURN_CONTEXT = (
    "In this managed browser, a non-empty filename returns a saved-file link "
    "without inline image content. Omit filename to receive an inline image response. "
    "This describes successful calls with pinned MCP 0.0.79 and imageResponses=allow; "
    "omitting filename still saves a screenshot file. "
    "MEYO_BROWSER_ARTIFACTS_JSON keeps path as the durable archive; workspace_path, "
    "when present, contains the same bytes inside this task's workspace for file-based tools. "
    "workspace_copy_error means only that the workspace copy is unavailable."
)


class BrowserUnavailable(RuntimeError):
    pass


def _directory(value, label):
    path = Path(str(value or ""))
    if not path.is_absolute():
        raise BrowserUnavailable(f"{label} must be absolute")
    path = path.resolve(strict=True)
    if not path.is_dir() or path == Path(path.anchor):
        raise BrowserUnavailable(f"{label} must be a non-root directory")
    return path


def _same_secret(actual, expected):
    return isinstance(actual, str) and actual.isascii() and hmac.compare_digest(actual, expected)


def _process_identity(pid):
    if Path("/proc/self/stat").exists():
        try:
            fields = Path("/proc", str(pid), "stat").read_text().rsplit(")", 1)[1].split()
            return (int(fields[1]), fields[19], fields[0])
        except (OSError, ValueError, IndexError):
            return None
    try:
        import psutil
        process = psutil.Process(pid)
        return (process.ppid(), process.create_time(), process.status())
    except Exception:
        return None


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = True
    request_queue_size = 16
    _capacity_wait_s = 5.0

    def __init__(self, lease):
        self.lease = lease
        # Bound all connection threads, including headers and lane waiters.
        # A navigation RPC must leave room for its own static subrequests.
        self._requests = threading.BoundedSemaphore(16)
        self._rpc_requests = threading.BoundedSemaphore(4)
        self._static_requests = threading.BoundedSemaphore(4)
        super().__init__(("127.0.0.1", 0), _Handler)

    def process_request(self, request, client_address):
        if not self._requests.acquire(blocking=False):
            # No handler/thread slot is available. Send a bounded, explicit
            # transport failure rather than silently resetting a valid asset.
            body = b'{"error":"browser_connection_capacity_exceeded"}'
            response = (
                b"HTTP/1.0 503 Service Unavailable\r\n"
                b"Content-Type: application/json\r\n"
                b"Cache-Control: no-store\r\nRetry-After: 1\r\nConnection: close\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode() + body
            )
            try:
                request.settimeout(0.2)
                request.sendall(response)
            except OSError:
                pass  # The peer may already have gone away.
            finally:
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


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def log_message(self, *args):
        pass  # No credentials, URLs or user content in access logs.

    def _reply(self, status, value, *, session=False):
        raw = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        if status == 503:
            self.send_header("Retry-After", "1")
        if session:
            self.send_header("Mcp-Session-Id", self.server.lease._mcp_session)
        self.end_headers()
        self.wfile.write(raw)

    def _acquire_capacity(self, semaphore, lane):
        if semaphore.acquire(timeout=self.server._capacity_wait_s):
            return True
        self._reply(503, {"error": f"browser_{lane}_capacity_exceeded"})
        return False

    def _origin_allowed(self):
        lease = self.server.lease
        return (self.headers.get("Host") == lease._authority
                and self.headers.get("Origin") in (None, "", lease.base_url))

    def do_POST(self):
        lease = self.server.lease
        if self.path != "/mcp" or not self._origin_allowed():
            return self._reply(403, {"error": "request_scope_not_allowed"})
        if not _same_secret(self.headers.get("Authorization"), "Bearer " + lease._token):
            return self._reply(401, {"error": "unauthorized"})
        if lease.closed:
            return self._reply(410, {"error": "browser_lease_closed"})
        try:
            size = int(self.headers.get("Content-Length", "-1"))
            if (self.headers.get("Transfer-Encoding") or not 0 < size <= _MAX_REQUEST
                    or self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json"):
                raise ValueError
            body = self.rfile.read(size)
            if len(body) != size:
                raise ValueError
            message = json.loads(body)
            if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0"
                    or not isinstance(message.get("method"), str)
                    or not isinstance(message.get("params", {}), dict)
                    or not isinstance(message.get("id"), (str, int, type(None)))):
                raise ValueError
        except (ValueError, OSError):
            return self._reply(400, {"error": "invalid_mcp_request"})
        method, request_id = message["method"], message.get("id")
        if method == "initialize":
            upstream = getattr(lease._backend, "initialization_result", {}).get("result", {})
            return self._reply(200, {
                "jsonrpc": "2.0", "id": request_id, "result": {
                    "protocolVersion": upstream.get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "meyo-managed-browser", "version": "1"},
                }}, session=True)
        if not _same_secret(self.headers.get("Mcp-Session-Id"), lease._mcp_session):
            return self._reply(403, {"error": "browser_session_mismatch"})
        if method == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": lease.tools}
            if isinstance(lease, ManagedBrowserLease):
                result["_meta"] = {"meyo_browser": lease.recovery_state()}
        elif method == "tools/call":
            if not self._acquire_capacity(self.server._rpc_requests, "rpc"):
                return
            try:
                result = lease.call(message.get("params") or {})
            finally:
                self.server._rpc_requests.release()
        else:
            return self._reply(200, {"jsonrpc": "2.0", "id": request_id,
                                     "error": {"code": -32601, "message": "method not granted"}})
        feedback = getattr(lease, "_feedback_context", None) if method == "tools/call" else None
        presentation_keys = ()
        if feedback is not None:
            try:
                result, presentation_keys = feedback.prepare(result)
            except Exception:
                pass  # Optional context must never replace a native observation.
        self._reply(200, {"jsonrpc": "2.0", "id": request_id, "result": result})
        if feedback is not None and presentation_keys:
            try:
                feedback.note_sent(presentation_keys)
            except Exception:
                pass  # A completed HTTP response must not be sent a second time.

    def do_GET(self):
        lease = self.server.lease
        if self.path == "/mcp":
            self.send_response(405)
            self.send_header("Allow", "POST")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not self._origin_allowed():
            return self._reply(403, {"error": "request_scope_not_allowed"})
        if not _same_secret(self.headers.get("X-Meyo-Browser-Artifact"), lease.artifact_token):
            return self._reply(401, {"error": "unauthorized"})
        if not self._acquire_capacity(self.server._static_requests, "static"):
            return
        try:
            path = unquote(urlsplit(self.path).path, errors="strict")
            parts = path.lstrip("/").split("/") if path != "/" else ["index.html"]
            with os.fdopen(lease._open_artifact(parts), "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise PermissionError
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(parts[-1])[0] or "application/octet-stream")
                self.send_header("Content-Length", str(info.st_size))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                shutil.copyfileobj(stream, self.wfile, length=65536)
        except FileNotFoundError:
            self._reply(404, {"error": "artifact_not_found"})
        except (OSError, ValueError):
            self._reply(403, {"error": "artifact_path_not_granted"})
        finally:
            self.server._static_requests.release()


class ManagedBrowserLease:
    def __init__(self, ctx, *, env, config_dir, action_timeout_ms=None):
        if action_timeout_ms is None and "LEGION_MANAGED_BROWSER_ACTION_TIMEOUT_MS" in env:
            value = env["LEGION_MANAGED_BROWSER_ACTION_TIMEOUT_MS"]
            if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
                raise BrowserUnavailable("managed browser action timeout environment must be decimal milliseconds")
            try:
                action_timeout_ms = int(value)
            except ValueError:
                raise BrowserUnavailable("managed browser action timeout environment is invalid") from None
        if action_timeout_ms is not None and (
                type(action_timeout_ms) is not int or not 1 <= action_timeout_ms <= 60000):
            raise BrowserUnavailable("managed browser action timeout must be an integer from 1 to 60000 ms")
        self._action_timeout_ms = action_timeout_ms
        self.user_id = str(getattr(ctx, "user_id", "") or "")
        self.session_id = str(getattr(ctx, "session_id", "") or "")
        if not self.user_id or not self.session_id:
            raise BrowserUnavailable("managed browser requires an existing owner and native session")
        self._workspace_alias = Path(str(getattr(ctx, "workdir", "") or ""))
        self.workspace = _directory(self._workspace_alias, "workspace")
        self.control_dir = _directory(config_dir, "control directory")
        if self.control_dir.is_relative_to(self.workspace):
            raise BrowserUnavailable("browser control must stay outside native-writable workspace")
        self.env = dict(env)
        # HTTP workers must capture the owning adapter thread's existing check,
        # not read their own empty thread-local or accept a caller-supplied one.
        registry = sys.modules.get("harness_registry")
        self._cancel_check = getattr(getattr(registry, "_INPROC_CANCEL_CHECK", None), "check", None)
        self._terminal_reason = ""
        try:
            self._deadline = float(self.env.get("MEYO_TASK_DEADLINE") or 0)
        except (TypeError, ValueError, OverflowError):
            raise BrowserUnavailable("invalid parent task deadline") from None
        if not math.isfinite(self._deadline) or self._deadline < 0:
            raise BrowserUnavailable("invalid parent task deadline")
        wall_s = float(getattr(registry, "_WALL_S", 0) or 0)
        if wall_s > 0:
            self._deadline = min(x for x in (self._deadline, time.monotonic() + wall_s) if x > 0)
        self.store = _directory(self.env.get("MEYO_RUNTIME_STORE"), "runtime control store")
        if self.store.is_relative_to(self.workspace):
            raise BrowserUnavailable("browser resource control must stay outside workspace")
        facts = dict(getattr(ctx, "task_facts", None) or {})
        self.team_id = str(facts.get("team_id") or self.session_id)
        self.task_id = str(facts.get("task_id") or "browser")
        self._token, self._mcp_session, self.artifact_token = (secrets.token_urlsafe(32) for _ in range(3))
        self._rpc_lock = threading.RLock()
        self._backend_stop_lock = threading.RLock()
        self._backend_stopped = False
        self._backend_cleanup_ok = True
        self._backend_exit_error = False
        self._recovering = False
        self._generation = 0
        self._last_error_type = ""
        self._lock_fd = None
        self._root_fd = None
        self._output_fd = None
        self._backend = None
        self._failed = False
        self.closed = False
        self.observations = []
        self.browser_processes = {}
        self.cleanup = {}
        self._temporary = None
        self.server = None
        self.server_thread = None
        self._feedback_context = None
        try:
            from native_feedback_context import create
            self._feedback_context = create(ctx)
        except Exception:
            pass  # Missing feedback visibility must not disable the browser.

    @property
    def mcp_config(self):
        return {"mcpServers": {"meyo-playwright": {
            "type": "http", "url": self.base_url + "/mcp",
            "headers": {"Authorization": "Bearer " + self._token},
        }}}

    def __enter__(self):
        try:
            # This is the explicitly authorized tool's own scratch, not a
            # fallback out of the native process's read-only /tmp.
            self._temporary = tempfile.TemporaryDirectory(
                prefix="mb-", dir="/tmp" if os.name != "nt" else tempfile.gettempdir())
            self.scratch_dir = Path(self._temporary.name)
            self.scratch_dir.chmod(0o700)
            if any(self.scratch_dir.resolve().is_relative_to(root)
                   or self.control_dir.is_relative_to(root) for root in self._native_writable_roots()):
                raise BrowserUnavailable("browser scratch and control must stay outside native-writable roots")
            self.output_dir = self.scratch_dir / "output"
            self.output_dir.mkdir(mode=0o700)
            self._output_fd = os.open(self.output_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self._root_fd = os.open(self.workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self.server = _Server(self)
            self._authority = f"127.0.0.1:{self.server.server_port}"
            self.base_url = "http://" + self._authority
            self._install_backend()
            self.server_thread = threading.Thread(target=self.server.serve_forever,
                                                  kwargs={"poll_interval": .05}, daemon=True)
            self.server_thread.start()
            return self
        except BaseException:
            self.close()
            raise

    def _stop_reason(self):
        if self.closed:
            return "closed"
        if not self._terminal_reason:
            try:
                if callable(self._cancel_check) and self._cancel_check():
                    self._terminal_reason = "cancelled"
            except Exception:
                self._terminal_reason = "parent_check_failed"
            if not self._terminal_reason and self._deadline and time.monotonic() >= self._deadline:
                self._terminal_reason = "runtime_budget"
        return self._terminal_reason

    def recovery_state(self):
        """Local facts only: listing never contacts or starts the browser."""
        reason = self._stop_reason()
        if not reason and self._failed and not self._backend_cleanup_ok:
            reason = "cleanup_unconfirmed"
        status = (reason if reason else "recovering" if self._recovering else
                  "failed" if self._failed else "ready")
        return {"status": status, "reason": reason or self._last_error_type or None,
                "recoverable": bool(self._failed and not reason and not self._recovering),
                "recovery_tool": "browser_recover", "generation": self._generation}

    def _recovery_result(self, message, *, error=True):
        return {"isError": error, "content": [
            {"type": "text", "text": message},
            {"type": "text", "text": "MEYO_BROWSER_STATE_JSON\n"
             + json.dumps(self.recovery_state())}]}

    def _install_backend(self):
        if self._stop_reason():
            raise BrowserUnavailable("parent browser scope is no longer active")
        # Reuse the originally opened workspace, never follow a substituted root.
        current, original = os.stat(self.workspace), os.fstat(self._root_fd)
        if (_directory(self._workspace_alias, "workspace") != self.workspace
                or (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino)):
            raise BrowserUnavailable("original browser workspace changed")
        backend = self._create_backend()  # Repeat the existing fixed-runtime validation.
        with self._backend_stop_lock:
            if self._stop_reason():
                raise BrowserUnavailable("parent browser scope is no longer active")
            self._backend = backend
            self._backend_stopped = False
            self._backend_cleanup_ok = True
            self._backend_exit_error = False
        backend.__enter__()
        initialized = getattr(backend, "initialization_result", None)
        if not isinstance(initialized, dict) or not isinstance(initialized.get("result"), dict):
            raise BrowserUnavailable("native browser MCP initialization was not accepted")
        tools = [dict(item) for item in backend.list_tools()
                 if item.get("name") in _TOOLS and item["name"] != "browser_recover"]
        names = {item["name"] for item in tools}
        if (len(names) != len(tools) or not
                {"browser_navigate", "browser_take_screenshot", "browser_snapshot"} <= names):
            raise BrowserUnavailable("native browser MCP did not expose its required core tools")
        for tool in tools:
            if tool["name"] == "browser_take_screenshot":
                tool["description"] = "\n\n".join(filter(None, (
                    tool.get("description"), _SCREENSHOT_RETURN_CONTEXT)))
        with self._backend_stop_lock:
            if self._stop_reason():
                raise BrowserUnavailable("parent browser scope is no longer active")
            contract = {tool["name"]: tool for tool in tools}
            if hasattr(self, "_native_tools") and contract != self._native_tools:
                raise BrowserUnavailable("recovered browser tool contract changed")
            self._native_tools = contract
            self.tools = [*tools, _RECOVER_TOOL]
            self._tool_names = names | {"browser_recover"}

    def _recover(self):
        with self._rpc_lock:
            if self._stop_reason() or not self._failed:
                return self._recovery_result("Browser recovery was not started: this scope is not recoverable.")
            if not self._stop_backend():
                return self._recovery_result("Browser recovery was not started: previous cleanup is unconfirmed.")
            self._recovering = True
            started = time.time()
            try:
                # Confirmed cleanup released the old slot. The replacement is
                # lazy and must compete for the same slot on its next action.
                self._install_backend()
                with self._backend_stop_lock:
                    if self._stop_reason():
                        raise BrowserUnavailable("parent browser scope is no longer active")
                    self._failed = False
                    self._last_error_type = ""
                    self._generation += 1
            except BaseException as exc:
                self._failed = True
                self._last_error_type = type(exc).__name__
                self._stop_backend()
                if not isinstance(exc, Exception):
                    raise
            finally:
                self._recovering = False
                self.observations.append({"tool": "browser_recover", "started_at": started,
                                          "ended_at": time.time(), "is_error": self._failed,
                                          "generation": self._generation})
            if self._failed or self._stop_reason():
                return self._recovery_result(
                    "Browser recovery did not complete. No actions were replayed; inspect the recovery state.")
            return self._recovery_result(
                "Fresh browser session prepared: no previous pages or login state; "
                "no actions were replayed. The browser starts on the next explicit tool call.", error=False)

    def _create_backend(self):
        from meyo_harnesses import mcp_bridge
        spec = mcp_bridge.native_stdio_config(["playwright"])["mcpServers"]["meyo-playwright"]
        expected = Path(mcp_bridge.__file__).parent / "playwright_runtime.cjs"
        if spec.get("args") != [str(expected)] or spec.get("env"):
            raise BrowserUnavailable("managed browser requires the declared fixed Playwright entry")
        worker = expected.with_name("managed_browser_worker.cjs")
        if not worker.is_file():
            raise BrowserUnavailable("managed browser worker is not installed")
        config = self.control_dir / ("managed-browser-" + secrets.token_hex(8) + ".json")
        settings = {"schema_version": 1, "artifact_origin": self.base_url,
                    "artifact_token": self.artifact_token, "output_dir": str(self.output_dir)}
        if self._action_timeout_ms is not None:
            settings["action_timeout_ms"] = self._action_timeout_ms
        with config.open("x") as stream:
            json.dump(settings, stream)
        config.chmod(0o600)
        child_env = {key: value for key, value in self.env.items() if key in _ENV}
        mutable = self._native_writable_roots()
        # A separate host tool must not discover executables/libraries from the
        # member's editable home/project. Native CLI environment is unchanged.
        for key in ("PATH", "LD_LIBRARY_PATH"):
            if key in child_env:
                child_env[key] = os.pathsep.join(
                    part for part in child_env[key].split(os.pathsep)
                    if part and Path(part).is_absolute()
                    and not any(Path(part).resolve().is_relative_to(root) for root in mutable))
        home = self.scratch_dir / ("home-" + secrets.token_hex(8))
        home.mkdir(mode=0o700)
        child_env.update(HOME=str(home), TMPDIR=str(self.scratch_dir),
                         MEYO_MANAGED_BROWSER_CONFIG=str(config),
                         MEYO_MANAGED_BROWSER_WRITABLE_ROOTS=json.dumps([str(root) for root in mutable]),
                         NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
        command = shutil.which(spec["command"], path=child_env.get("PATH")) or spec["command"]
        executable = Path(command).resolve(strict=True)
        if executable.is_relative_to(self.workspace) or not os.access(executable, os.X_OK):
            raise BrowserUnavailable("browser executable must come from trusted runtime, not workspace")
        self._validate_materials(executable, worker)
        # Pinned MCP navigation can take 60s before returning a native error.
        # Leave room for that response; genuine transport stalls still close.
        lease = self

        class OwnedMCP(mcp_bridge._StdioMCP):
            """Serialize spawn/stop, not the blocking initialization RPC."""
            _lease_exited = False
            _lease_cleanup_ok = True

            def _start(self):
                with lease._backend_stop_lock:
                    if self._lease_exited or lease._stop_reason():
                        raise BrowserUnavailable("parent browser scope is no longer active")
                    super()._start()

            def _rpc(self, *args, **kwargs):
                if lease._stop_reason():
                    raise BrowserUnavailable("parent browser scope is no longer active")
                if lease._deadline:
                    self._rpc_timeout_s = min(self._rpc_timeout_s, lease._deadline - time.monotonic())
                    if self._rpc_timeout_s <= 0:
                        raise BrowserUnavailable("parent browser budget exhausted")
                return super()._rpc(*args, **kwargs)

            def __exit__(self, *args):
                with lease._backend_stop_lock:
                    if self._lease_exited:
                        return
                    self._lease_exited = True
                    self._lease_cleanup_ok = lease._remember_descendants(self.proc)
                    try:
                        super().__exit__(*args)
                    except BaseException:
                        self._lease_cleanup_ok = False
                        raise

        return OwnedMCP(
            "managed-playwright", {"command": str(executable), "args": [str(worker)],
                                   "cwd": str(self.scratch_dir)},
            environment=child_env, on_notification=self._notification,
            close_input_first=True, shutdown_grace_s=5, rpc_timeout_s=90)

    def _native_writable_roots(self):
        import sys
        mutable = [self.workspace]
        keys = ("HOME", "CODEX_HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR")
        # Local native launch explicitly grants only its private temporary
        # directory; host browser materials must stay outside that grant too.
        if sys.platform == "darwin":
            keys += ("TMPDIR",)
        for key in keys:
            if self.env.get(key):
                path = Path(str(self.env[key]))
                if not path.is_absolute():
                    raise BrowserUnavailable("native state root must be absolute")
                mutable.append(path.resolve())
        return mutable

    def _validate_materials(self, node, worker):
        """A host tool must not execute material selected by native-writable data."""
        mutable = self._native_writable_roots()

        def readonly(value, label, *, directory=False):
            path = Path(str(value or ""))
            if not path.is_absolute():
                raise BrowserUnavailable(f"{label} must be an absolute runtime path")
            logical = path
            path = path.resolve(strict=True)
            if any(logical.is_relative_to(root) or path.is_relative_to(root)
                   or (directory and root.is_relative_to(path)) for root in mutable):
                raise BrowserUnavailable(f"{label} must be outside all native-writable roots")
            if not (path.is_dir() if directory else path.is_file()):
                raise BrowserUnavailable(f"{label} is not a runtime material")
            return path

        try:
            readonly(node, "Node executable")
            readonly(worker, "browser worker")
            source = self.env.get("MEYO_PLAYWRIGHT_RUNTIME_PROFILE")
            if not source:
                raise BrowserUnavailable("managed browser requires an explicit sealed runtime profile")
            profile = readonly(source, "runtime profile")
            if Path(source).is_symlink():
                raise BrowserUnavailable("runtime profile must not be a symlink")
            fd = os.open(profile, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 65536):
                    raise BrowserUnavailable("runtime profile is not privately owned")
                value = json.loads(stream.read())
            runtime = readonly(value.get("runtime_root"), "browser runtime", directory=True)
            if value.get("schema_version") != 1 or runtime != _directory(
                    self.env.get("MEYO_RUNTIME_ROOT"), "configured runtime"):
                raise BrowserUnavailable("browser runtime does not match the configured fixed runtime")
            if value.get("platform") == "darwin":
                import sys
                if sys.platform != "darwin":
                    raise BrowserUnavailable("browser runtime platform mismatch")
                package = readonly(value.get("mcp_package_path"), "browser MCP package")
                readonly(package.parent, "browser MCP directory", directory=True)
                readonly(package.parent / "cli.js", "browser MCP CLI")
                readonly(package.parent / "index.js", "browser MCP entry")
                readonly(value.get("browser_executable_path"), "browser executable")
            else:
                readonly(value.get("native_libs_root"), "browser libraries", directory=True)
                readonly(value.get("font_path"), "browser font")
        except BrowserUnavailable:
            raise
        except (OSError, ValueError, TypeError, AttributeError):
            raise BrowserUnavailable("managed browser material trust check failed") from None

    def _notification(self, message):
        if message.get("method") != "notifications/meyo/browser_process":
            return
        value = message.get("params") or {}
        pid = value.get("pid")
        root = getattr(self._backend, "proc", None)
        if not isinstance(pid, int) or pid <= 1 or root is None or pid in (os.getpid(), root.pid):
            raise BrowserUnavailable("invalid browser process observation")
        identity = _process_identity(pid)
        current = identity
        for _ in range(16):
            if current is None:
                raise BrowserUnavailable("browser process ownership unavailable")
            parent = current[0]
            if parent == root.pid:
                break
            if parent <= 1:
                raise BrowserUnavailable("browser process is not owned by this MCP worker")
            current = _process_identity(parent)
        else:
            raise BrowserUnavailable("browser process ancestry exceeded")
        import legion_proc_registry
        legion_proc_registry.register(pid, pgid=os.getpgid(pid), owner=root.pid,
                                      meta={"kind": "managed-browser", "session": self.session_id})
        self.browser_processes[pid] = identity

    def _acquire_browser(self):
        with self._backend_stop_lock:
            if self._stop_reason():
                return False
            if self._lock_fd is not None:
                return True
            import fcntl
            fd = os.open(self.store / ".managed-browser.lock",
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                return False
            self._lock_fd = fd
            return True

    def _arguments(self, name, arguments):
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        # The native MCP reads this private field before schema parsing and can
        # use it to replace cwd. It is not part of the advertised tool contract.
        if "_meta" in arguments:
            raise ValueError("caller-controlled native execution metadata is not granted")
        result = dict(arguments)
        if name == "browser_navigate" or (name == "browser_tabs" and "url" in result):
            url = result.get("url")
            if not isinstance(url, str):
                raise ValueError("navigation URL is required")
            parsed = urlsplit(url)
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("URL credentials are not accepted")
            if parsed.scheme == "file":
                if parsed.netloc not in ("", "localhost"):
                    raise ValueError("remote file URLs are not granted")
                candidate = Path(unquote(parsed.path, errors="strict"))
                if any(part in (".", "..") or "\\" in part or "\0" in part for part in candidate.parts):
                    raise ValueError("file path is not granted")
                try:
                    relative = candidate.relative_to(self.workspace)
                except ValueError:
                    # Only the trusted root may have a logical alias. Keep all
                    # child components unchanged so symlinks cannot disappear.
                    relative = candidate.relative_to(self._workspace_alias)
                fd = self._open_artifact(relative.parts)
                os.close(fd)
                result["url"] = (self.base_url + "/" + quote(relative.as_posix())
                                 + ("?" + parsed.query if parsed.query else "")
                                 + ("#" + parsed.fragment if parsed.fragment else ""))
            elif parsed.scheme not in ("http", "https") and url != "about:blank":
                raise ValueError("only web URLs or owned workspace files are granted")
        if "filename" in result:
            filename = result["filename"]
            if (name != "browser_take_screenshot" or not isinstance(filename, str)
                    or not filename or len(filename) > 120 or filename.startswith(".")
                    or Path(filename).name != filename or "\\" in filename or "\0" in filename
                    or Path(filename).suffix.lower() not in (".png", ".jpg", ".jpeg")):
                raise ValueError("screenshot filename must remain in the owned output directory")
        return result

    def _open_artifact(self, parts):
        if not parts or any(not p or p.startswith(".") or "\\" in p or "\0" in p for p in parts):
            raise PermissionError("artifact path not granted")
        fd = (os.dup(self._root_fd) if self._root_fd is not None else
              os.open(self.workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                os.close(file_fd)
                raise PermissionError("only regular artifacts are granted")
            return file_fd
        finally:
            os.close(fd)

    @staticmethod
    def _error(message):
        return {"isError": True, "content": [{"type": "text", "text": message}]}

    def _workspace_screenshot_reference(self, ref, raw):
        """Bridge an owned image to task tools without granting access to state."""
        from artifact_store import _put_bytes_at, _safe

        def check_directory(info):
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o022):
                raise BrowserUnavailable("screenshot directory is not reader-compatible")

        def check_workspace():
            if self._root_fd is None:
                raise BrowserUnavailable("browser_workspace_closed")
            current = os.stat(self.workspace, follow_symlinks=False)
            original = os.fstat(self._root_fd)
            check_directory(original)
            if (not stat.S_ISDIR(current.st_mode)
                    or _directory(self._workspace_alias, "workspace") != self.workspace
                    or (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino)):
                raise BrowserUnavailable("original browser workspace changed")

        try:
            from contextlib import ExitStack
            from native_capability_host import _MAX_IMAGE_BYTES, _read_regular_at

            if not 0 < len(raw) <= _MAX_IMAGE_BYTES:
                raise BrowserUnavailable("screenshot exceeds workspace image reader budget")
            check_workspace()
            parts = (".meyo-browser", _safe(self.team_id), _safe(self.task_id), Path(ref["path"]).name)
            copy = _put_bytes_at(
                self.workspace, parts, raw, ref["name"], _root_fd=self._root_fd, blocking=False)
            # Use the same bounded reader as file-based image tools, without
            # mutating process-wide environment in this multithreaded server.
            with ExitStack() as opened:
                parent = os.dup(self._root_fd)
                opened.callback(os.close, parent)
                chain = []
                for part in parts[:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=parent)
                    opened.callback(os.close, child)
                    check_directory(os.fstat(child))
                    chain.append((parent, part, child))
                    parent = child
                before = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                if _read_regular_at(parent, parts[-1], _MAX_IMAGE_BYTES) != raw:
                    raise BrowserUnavailable("screenshot workspace readback differs")
                after = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                if any(getattr(before, field) != getattr(after, field) for field in
                       ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")):
                    raise BrowserUnavailable("screenshot workspace entry changed")
                for directory, part, child in chain:
                    current = os.stat(part, dir_fd=directory, follow_symlinks=False)
                    original = os.fstat(child)
                    check_directory(original)
                    if (not stat.S_ISDIR(current.st_mode)
                            or (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino)):
                        raise BrowserUnavailable("screenshot workspace directory changed")
            check_workspace()
            return {**ref, "workspace_path": copy["path"]}
        except Exception as exc:
            # The archive/native observation is still useful. A copy failure
            # must not stop a healthy browser or disclose private path details.
            return {**ref, "workspace_copy_error": type(exc).__name__}

    def _named_screenshot_reference(self, filename):
        """Publish only the declared screenshot, never paths found in page text."""
        if self._output_fd is None:
            raise BrowserUnavailable("named_screenshot_output_closed")
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=self._output_fd)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise BrowserUnavailable("named_screenshot_not_regular")
            if not 0 < before.st_size <= _MAX_IMAGE:
                raise BrowserUnavailable("named_screenshot_size_out_of_bounds")
            raw = stream.read(_MAX_IMAGE + 1)
            after = os.fstat(stream.fileno())
            if (len(raw) != before.st_size or
                    (before.st_size, before.st_mtime_ns, before.st_ino) !=
                    (after.st_size, after.st_mtime_ns, after.st_ino)):
                raise BrowserUnavailable("named_screenshot_changed_during_read")
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            extension = ".png"
        elif raw.startswith(b"\xff\xd8\xff"):
            extension = ".jpg"
        else:
            raise BrowserUnavailable("named_screenshot_format_unrecognized")
        suffix = Path(filename).suffix.lower()
        compatible = suffix == extension or (extension == ".jpg" and suffix == ".jpeg")
        name = filename if compatible else Path(filename).stem + extension
        from artifact_store import put_bytes
        ref = put_bytes(self.team_id, self.task_id, raw, name=name)
        return self._workspace_screenshot_reference(ref, raw)

    def call(self, params):
        name = params.get("name")
        if not isinstance(name, str) or name not in self._tool_names:
            return self._error("Tool is not granted by this browser capability.")
        if name == "browser_recover":
            arguments = params.get("arguments", {})
            if not isinstance(arguments, dict) or arguments:
                return self._recovery_result("browser_recover accepts no arguments; the original scope is fixed.")
            return self._recover()
        try:
            arguments = self._arguments(name, params.get("arguments") or {})
        except (ValueError, TypeError, OSError):
            return self._error("Requested browser arguments exceed this task's granted scope.")
        with self._rpc_lock:
            if self._stop_reason() or self._failed:
                return self._recovery_result("This browser lease is unavailable; no automatic retry was started.")
            if not self._acquire_browser():
                if self._stop_reason():
                    return self._recovery_result("This browser lease is unavailable; no browser was started.")
                return self._error("The host browser slot is in use; no browser was started for this call.")
            started = time.time()
            try:
                result = self._backend.call_tool(name, arguments)
                if self.closed:
                    return self._error("The browser lease was closed while this call was in flight.")
                refs = []
                native_error = bool(result.get("isError"))
                delivery_error = None
                for block in result.get("content") or []:
                    if not isinstance(block, dict) or block.get("type") != "image":
                        continue
                    if not isinstance(block.get("data"), str) or len(block["data"]) > _MAX_IMAGE * 4 // 3 + 4:
                        raise BrowserUnavailable("browser image exceeded transport budget")
                    raw = base64.b64decode(block["data"], validate=True)
                    if len(raw) > _MAX_IMAGE:
                        raise BrowserUnavailable("browser image exceeded transport budget")
                    from artifact_store import put_bytes
                    ext = ".jpg" if block.get("mimeType") == "image/jpeg" else ".png"
                    ref = put_bytes(self.team_id, self.task_id, raw, name="browser-observation" + ext)
                    refs.append(self._workspace_screenshot_reference(ref, raw))
                if (name == "browser_take_screenshot" and arguments.get("filename")
                        and not native_error and not refs):
                    try:
                        refs.append(self._named_screenshot_reference(arguments["filename"]))
                    except (OSError, ValueError, BrowserUnavailable) as exc:
                        # File delivery failure is not a dead browser. Preserve
                        # the native receipt and leave later tool calls usable.
                        delivery_error = type(exc).__name__
                        result = {**result, "isError": True, "content": [
                            *result.get("content", []), {
                                "type": "text",
                                "text": "Meyo screenshot delivery failed: " + delivery_error
                                + ". The native file link could not be safely published."
                                  " The browser is still available; no image reference was produced.",
                            }]}
                self.observations.append({"tool": name, "started_at": started,
                                          "ended_at": time.time(), "is_error": bool(result.get("isError")),
                                          "native_is_error": native_error,
                                          "delivery_error": delivery_error,
                                          "actions": {key: value for key, value in arguments.items()
                                                      if key in ("x", "y", "button", "deltaX", "deltaY",
                                                                 "key", "ref", "fullPage", "type",
                                                                 "width", "height", "action", "index", "filename")
                                                      and isinstance(value, (str, int, float, bool))},
                                          "artifacts": refs})
                # Images and native observations are unchanged; the extra block
                # names durable copies, not a verdict about the user's outcome.
                if refs:
                    result = {**result, "content": [*result.get("content", []), {
                        "type": "text", "text": "MEYO_BROWSER_ARTIFACTS_JSON\n"
                        + json.dumps(refs, ensure_ascii=False)}]}
                return result
            except Exception as exc:
                self._failed = True
                self._last_error_type = type(exc).__name__
                self.observations.append({"tool": name, "started_at": started,
                                          "ended_at": time.time(), "runtime_error": type(exc).__name__})
                self._stop_backend()
                return self._recovery_result("Managed browser tool failed: " + type(exc).__name__
                                             + ". Original files are retained; no automatic replay was started.")

    def _remember_descendants(self, root):
        # Snapshot this worker's descendants before they can be reparented.
        # This adds no host-wide scan or authority to kill unrelated processes.
        if root is None or root.poll() is not None:
            return True
        try:
            import psutil
            for process in psutil.Process(root.pid).children(recursive=True):
                identity = _process_identity(process.pid)
                if identity is not None:
                    self.browser_processes[process.pid] = identity
            return True
        except Exception:
            return root.poll() is not None

    def _remaining_browser_processes(self):
        return [pid for pid, identity in self.browser_processes.items()
                if (current := _process_identity(pid)) is not None
                and current[1] == identity[1] and current[2] not in ("Z", "zombie")]

    def _stop_backend(self):
        with self._backend_stop_lock:
            if self._backend is None:
                return True
            root = getattr(self._backend, "proc", None)
            if not self._backend_stopped:
                self._backend_stopped = True
                try:
                    self._backend.__exit__()
                except Exception as exc:
                    self._backend_exit_error = True
                    self.cleanup["backend_close_error"] = type(exc).__name__
                finally:
                    if root is not None:
                        import legion_proc_registry
                        legion_proc_registry.kill_owned_by(root.pid)
                        self.cleanup["mcp_returncode"] = root.poll()
                        self.cleanup["mcp_shutdown"] = dict(getattr(self._backend, "shutdown_facts", {}))
            self._backend_cleanup_ok = bool(
                not self._backend_exit_error and getattr(self._backend, "_lease_cleanup_ok", True)
                and (root is None or root.poll() is not None)
                and not self._remaining_browser_processes())
            self.cleanup["backend_cleanup_confirmed"] = self._backend_cleanup_ok
            if self._backend_cleanup_ok and self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None
            return self._backend_cleanup_ok

    def close(self):
        if self.closed:
            self._stop_backend()  # May confirm a previously pending exit; never restarts anything.
            return
        self.closed = True
        # Interrupt a blocked upstream RPC first. Waiting on _rpc_lock before
        # shutting down the worker would turn cancellation into a 60-second wait.
        self._stop_backend()
        if self.server_thread is not None and self.server_thread.is_alive():
            self.server.shutdown()
            self.server_thread.join(timeout=3)
        if self.server is not None:
            self.server.server_close()
        with self._rpc_lock:
            self.cleanup["browser_processes_remaining"] = self._remaining_browser_processes()
            # Unknown cleanup retains capacity rather than authorizing a
            # second browser. A later close may confirm exit; owner process
            # exit also releases its descriptor. Never revive this scope.
            if self._root_fd is not None:
                os.close(self._root_fd)
                self._root_fd = None
            if self._output_fd is not None:
                os.close(self._output_fd)
                self._output_fd = None
            if self._temporary is not None:
                self._temporary.cleanup()

    def __exit__(self, *args):
        self.close()


@contextmanager
def open_managed_browser(ctx, *, env, config_dir, action_timeout_ms=None):
    with ManagedBrowserLease(ctx, env=env, config_dir=config_dir,
                             action_timeout_ms=action_timeout_ms) as lease:
        yield lease
