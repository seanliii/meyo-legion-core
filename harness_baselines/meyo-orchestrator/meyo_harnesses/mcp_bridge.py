# -*- coding: utf-8 -*-
"""共享 MCP 工具桥 —— 让编排层可发现/调用任意 MCP server 的工具 (2026-08-25)

设计原则(承接 ADR-005):**只做能力接入,不做路由/荐人**。
本模块给模型多两个客观工具:list_mcp_tools / call_mcp_tool。用不用、调哪个,
仍由 captain 模型自主决定——这里没有一行 if task_kind / recommend。

协议:标准 MCP over stdio(JSON-RPC 2.0)。最小自研 client,不引入 omnigent 运行时耦合,
但严格走 MCP 官方 stdio 传输,可对接 cua-mcp-server / supervision / 任意 stdio MCP server。

server 声明来自 mcp_servers.json(纯声明式:name/command/args/env/enabled)。
"""
from __future__ import annotations
import json, os, sys, subprocess, threading, time, urllib.request
import queue
import math
import re
import signal
import select
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent                      # meyo-orchestrator/
SERVERS_FILE = _HERE / "mcp_servers.json"
_MCP_LOG_DIR = _ROOT / "subagent_logs"
_PROTO = "2024-11-05"                      # MCP protocol version
_HARD_TIMEOUT = 60                        # 单次 MCP 调用硬超时(只读工具足够;真卡死熔断)


def _runtime_values(*, resolve_proxy: bool = True) -> dict[str, str]:
    home = str(Path.home())
    repo = str(_ROOT.parent.parent)
    legion_dir = os.environ.get("MEYO_LEGION_DIR") or os.path.join(home, ".meyo", "legion")
    proxy = ""
    if resolve_proxy:
        try:
            service = Path(repo) / "release" / "legion-service"
            if str(service) not in sys.path:
                sys.path.insert(0, str(service))
            import github_access
            proxy = github_access.proxy_url()
        except Exception:
            pass
    return {"__HOME__": home, "__REPO_ROOT__": repo, "__ORCH_ROOT__": str(_ROOT),
            "__LEGION_DIR__": legion_dir, "__PROXY_URL__": proxy}


def _expand(value: str, values: dict[str, str], *, resolve_environment: bool = True) -> str:
    for marker, replacement in values.items():
        value = value.replace(marker, replacement)
    value = os.path.expanduser(value)
    return os.path.expandvars(value) if resolve_environment else value


def _unique_registry_fields(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field in native MCP registry")
        result[key] = value
    return result


def _native_stdio_declarations(servers: dict, names: tuple[str, ...]) -> dict:
    """Validate only requested servers; never silently drop execution semantics."""
    if not isinstance(servers, dict):
        raise ValueError("native MCP registry must contain a servers object")
    selected = {}
    for name in names:
        raw = servers.get(name)
        if not isinstance(raw, dict) or raw.get("enabled", True) is not True:
            raise ValueError(f"unknown or disabled MCP server: {name}")
        if (raw.get("transport", "stdio") != "stdio" or raw.get("type", "stdio") != "stdio"
                or any(raw.get(key) for key in ("cwd", "url", "headers", "auth", "optional_args"))):
            raise ValueError(f"unsupported native stdio declaration: {name}")
        command, args, env = raw.get("command"), raw.get("args", []), raw.get("env", {})
        if (not isinstance(command, str) or not command.strip()
                or not isinstance(args, list) or not all(isinstance(arg, str) for arg in args)
                or not isinstance(env, dict)
                or not all(isinstance(key, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                           and isinstance(value, str) for key, value in env.items())):
            raise ValueError(f"invalid native stdio command, args or env: {name}")
        if any("\0" in value for value in [command, *args, *env.values()]):
            raise ValueError(f"invalid NUL in native stdio declaration: {name}")
        if any("__PROXY_URL__" in value for value in [command, *args, *env.values()]):
            raise ValueError(f"native MCP export cannot materialize a proxy credential: {name}")
        # Static credentials cannot be distinguished from arbitrary literals.
        # Export references or known path markers instead of copying secret values.
        for value in env.values():
            if value and not re.fullmatch(
                r"\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*|"
                r"__(?:HOME|REPO_ROOT|ORCH_ROOT|LEGION_DIR)__(?:/.*)?", value
            ):
                raise ValueError(f"native MCP env must use references, not inline values: {name}")
        selected[name] = raw
    return selected


def _load_servers(*, native_names: tuple[str, ...] | None = None) -> dict[str, dict]:
    """读声明式 server 注册表，并把运行时路径/代理展开为本机真实值。"""
    native = native_names is not None
    try:
        data = json.loads(SERVERS_FILE.read_text(),
                          object_pairs_hook=_unique_registry_fields if native else None)
        servers = data.get("servers", {})
        if native:
            servers = _native_stdio_declarations(servers, native_names)
        values = _runtime_values(resolve_proxy=False) if native else _runtime_values()

        def expand(value):
            return _expand(value, values, resolve_environment=not native)

        out = {}
        for name, raw in servers.items():
            if not raw.get("enabled", True):
                continue
            spec = dict(raw)
            if spec.get("command"):
                spec["command"] = expand(spec["command"])
                packaged = Path(values["__REPO_ROOT__"]) / "runtime" / "node-tools" / "bin" / Path(spec["command"]).name
                if packaged.is_file() and os.access(packaged, os.X_OK):
                    spec["command"] = str(packaged)
            spec["args"] = [expand(str(arg)) for arg in spec.get("args", [])]
            family = Path(values["__REPO_ROOT__"]) / "runtime" / "py-tools" / ("mcp-" + name)
            entry = family / "bin" / ("mcp-server-" + name)
            if entry.is_file():
                args = list(spec["args"])
                if Path(spec.get("command", "")).name == "uvx":
                    executable = "mcp-server-" + name
                    positions = [i for i, arg in enumerate(args) if arg == executable]
                    args = args[positions[-1] + 1:] if positions else args
                spec["command"] = sys.executable
                spec["args"] = [str(entry), *args]
                spec["env"] = {**spec.get("env", {}), "PYTHONPATH": str(family)}
            if values["__PROXY_URL__"] and (spec.get("optional_args") or {}).get("proxy"):
                spec["args"] += [expand(str(arg))
                                 for arg in spec["optional_args"]["proxy"]]
            spec["env"] = {key: expand(str(value))
                           for key, value in (spec.get("env") or {}).items()}
            if spec.get("url"):
                spec["url"] = expand(spec["url"])
            out[name] = spec
        return out
    except (OSError, json.JSONDecodeError):
        if native:
            raise ValueError("cannot read native MCP registry") from None
        return {}
    except ValueError:
        if native:
            raise
        return {}
    except Exception:
        if native:
            raise ValueError("invalid native MCP registry") from None
        return {}


def native_stdio_config(names, namespace: str = "meyo") -> dict:
    """Return selected stdio declarations for CC, without writing or starting anything.

    Names are prefixed (e.g. ``meyo-playwright``), not merged into user config.
    Declared environment references stay references; process environment and proxy
    credentials are not embedded. Unsupported transports/cwd/inline env fail
    explicitly. Existing CLI/SDK callers of ``_load_servers()`` are unchanged.
    """
    valid_name = re.compile(r"[A-Za-z0-9_-]+")
    if not isinstance(namespace, str) or not valid_name.fullmatch(namespace):
        raise ValueError("invalid native MCP namespace")
    if isinstance(names, (str, bytes)):
        raise ValueError("native MCP names must be an explicit collection")
    try:
        selected = tuple(names)
    except TypeError:
        raise ValueError("native MCP names must be an explicit collection") from None
    if (any(not isinstance(name, str) or not valid_name.fullmatch(name) for name in selected)
            or len(set(selected)) != len(selected)):
        raise ValueError("invalid or duplicate native MCP server name")
    if not selected:
        return {"mcpServers": {}}
    servers = _load_servers(native_names=selected)
    result = {}
    for name, spec in servers.items():
        entry = {"type": "stdio", "command": spec["command"], "args": list(spec["args"])}
        if spec["env"]:
            entry["env"] = dict(spec["env"])
        result[f"{namespace}-{name}"] = entry
    return {"mcpServers": result}


def _http_headers(spec: dict) -> dict:
    headers = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}
    if spec.get("auth") == "github":
        try:
            service = _ROOT.parent.parent / "release" / "legion-service"
            if str(service) not in sys.path:
                sys.path.insert(0, str(service))
            import github_access
            token = github_access.token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        except Exception:
            pass
    headers.update(spec.get("headers") or {})
    return headers


def _decode_http_response(raw: str) -> dict:
    """Decode JSON or Streamable HTTP SSE data frames."""
    raw = raw.strip()
    if raw.startswith("{"):
        return json.loads(raw)
    for line in raw.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                return json.loads(payload)
    raise RuntimeError("mcp_http_response_without_json")


class _HttpMCP:
    def __init__(self, name: str, spec: dict):
        self.name, self.spec, self._id, self.session_id = name, spec, 0, ""

    def _rpc(self, method: str, params: dict | None = None, notify: bool = False):
        self._id += 1
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            payload["id"] = self._id
        headers = _http_headers(self.spec)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.spec.get("protocol_version"):
            headers["Mcp-Protocol-Version"] = self.spec["protocol_version"]
        req = urllib.request.Request(self.spec["url"], data=json.dumps(payload).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=_HARD_TIMEOUT) as response:
                self.session_id = response.headers.get("Mcp-Session-Id") or self.session_id
                if notify and response.status in (202, 204):
                    return None
                return _decode_http_response(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"mcp_http_{exc.code}:{body[:160]}") from exc

    def __enter__(self):
        response = self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                            "clientInfo": {"name": "meyo-orchestrator", "version": "0.2"}})
        negotiated = (response or {}).get("result", {}).get("protocolVersion")
        if negotiated:
            self.spec["protocol_version"] = negotiated
        self._rpc("notifications/initialized", notify=True)
        return self

    def __exit__(self, *exc):
        return None

    def list_tools(self):
        return (self._rpc("tools/list") or {}).get("result", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict):
        resp = self._rpc("tools/call", {"name": name, "arguments": arguments})
        return (resp or {}).get("result", (resp or {}).get("error", {}))


def _client(name: str, spec: dict):
    return _HttpMCP(name, spec) if spec.get("transport") in ("http", "streamable-http") else _StdioMCP(name, spec)


class _StdioMCP:
    """最小 stdio MCP client:启子进程·initialize·list/call·关闭。同步·单请求。"""

    def __init__(self, name: str, spec: dict, *, environment: dict | None = None,
                 on_notification=None, close_input_first: bool = False,
                 shutdown_grace_s: float = 1, rpc_timeout_s: float | None = None):
        self.name = name
        self.spec = spec
        self._environment = None if environment is None else dict(environment)
        self._on_notification = on_notification
        self.initialization_result = {}
        self._close_input_first = bool(close_input_first)
        self._shutdown_grace_s = float(shutdown_grace_s)
        if not 0 <= self._shutdown_grace_s <= 10:
            raise ValueError("MCP shutdown grace must be finite and between zero and ten seconds")
        self._rpc_timeout_s = None if rpc_timeout_s is None else float(rpc_timeout_s)
        if self._rpc_timeout_s is not None and (
                not math.isfinite(self._rpc_timeout_s) or self._rpc_timeout_s <= 0):
            raise ValueError("MCP RPC timeout must be finite and positive")
        self.shutdown_facts = {}
        self.proc: subprocess.Popen | None = None
        self._id = 0
        self._lines = queue.Queue()
        self._readers = []
        self._registry = None
        self._stop_readers = threading.Event()
        self._writers = []

    def _start(self):
        cmd = [self.spec["command"], *self.spec.get("args", [])]
        env = dict(os.environ) if self._environment is None else dict(self._environment)
        env.update(self.spec.get("env", {}))
        cwd = self.spec.get("cwd")
        if cwd == "__ORCH_ROOT__":       # 声明式占位符→编排层根目录(便于 -m/相对路径)
            cwd = str(_ROOT)
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, errors="replace", env=env, bufsize=1,
            cwd=cwd or None,
            start_new_session=os.name != "nt",
        )
        try:
            import legion_proc_registry
            self._registry = legion_proc_registry
            self._registry.register(self.proc.pid, owner=os.getpid(), meta={"kind": "mcp-stdio", "server": self.name})
        except ImportError:
            pass

        def drain_stdout():
            if os.name != "nt":
                pending = b""
                fd = self.proc.stdout.fileno()
                try:
                    while not self._stop_readers.is_set():
                        if not select.select([fd], [], [], .1)[0]:
                            continue
                        data = os.read(fd, 8192)
                        if not data:
                            break
                        pending += data
                        while b"\n" in pending:
                            line, pending = pending.split(b"\n", 1)
                            self._lines.put(line.decode("utf-8", errors="replace"))
                    if pending:
                        self._lines.put(pending.decode("utf-8", errors="replace"))
                finally:
                    self._lines.put(None)
                return
            try:
                for line in self.proc.stdout:
                    self._lines.put(line)
            finally:
                self._lines.put(None)

        def drain_stderr():
            # Never print server logs here: they may contain credentials.
            if os.name != "nt":
                fd = self.proc.stderr.fileno()
                while not self._stop_readers.is_set():
                    if select.select([fd], [], [], .1)[0] and not os.read(fd, 8192):
                        break
                return
            while self.proc.stderr.read(8192):
                pass

        for target in (drain_stdout, drain_stderr):
            reader = threading.Thread(target=target, daemon=True)
            reader.start()
            self._readers.append(reader)

    def _rpc(self, method: str, params: dict | None = None,
             notify: bool = False) -> dict | None:
        assert self.proc and self.proc.stdin and self.proc.stdout
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method,
                               "params": params or {}}
        if not notify:
            self._id += 1
            msg["id"] = self._id
        timeout = _HARD_TIMEOUT if self._rpc_timeout_s is None else self._rpc_timeout_s
        deadline = time.monotonic() + timeout
        written = queue.Queue(maxsize=1)

        def write_request():
            try:
                self.proc.stdin.write(json.dumps(msg) + "\n")
                self.proc.stdin.flush()
                written.put(None)
            except (OSError, ValueError) as error:
                written.put(error)

        if os.name != "nt":
            fd = self.proc.stdin.fileno()
            os.set_blocking(fd, False)
            payload = memoryview((json.dumps(msg) + "\n").encode())
            while payload:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
                    self.__exit__()
                    raise TimeoutError(f"mcp_rpc_timeout:{method}:write")
                try:
                    payload = payload[os.write(fd, payload):]
                except BlockingIOError:
                    continue
        else:
            writer = threading.Thread(target=write_request, daemon=True)
            self._writers.append(writer)
            writer.start()
            try:
                error = written.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                self.__exit__()
                writer.join(timeout=1)
                raise TimeoutError(f"mcp_rpc_timeout:{method}:write") from None
            writer.join(timeout=1)
            if error:
                raise error
        if notify:
            return None
        # 读到匹配 id 的响应(跳过 server 的 notification/log 行)
        while time.monotonic() < deadline:
            try:
                line = self._lines.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                raise TimeoutError(f"mcp_rpc_timeout:{method}") from None
            if line is None:
                raise RuntimeError("mcp_server_closed_stdout")
            line = line.strip()
            if not line:
                continue
            try:
                resp = json.loads(line)
            except Exception:
                continue
            if (isinstance(resp, dict) and "id" not in resp and resp.get("method")
                    and self._on_notification is not None):
                self._on_notification(resp)
            if resp.get("id") == self._id:
                return resp
        raise TimeoutError(f"mcp_rpc_timeout:{method}")

    def __enter__(self):
        try:
            self._start()
            self.initialization_result = self._rpc("initialize", {
                "protocolVersion": _PROTO,
                "capabilities": {},
                "clientInfo": {"name": "meyo-orchestrator", "version": "0.1"},
            })
            self._rpc("notifications/initialized", notify=True)
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc):
        if not self.proc:
            return
        try:
            if self._close_input_first and os.name != "nt":
                if self.proc.stdin and not self.proc.stdin.closed:
                    try:
                        self.proc.stdin.close()
                        self.shutdown_facts["stdin_closed"] = True
                    except (OSError, ValueError):
                        pass
                if self.proc.poll() is None:
                    try:
                        self.proc.wait(timeout=self._shutdown_grace_s)
                    except subprocess.TimeoutExpired:
                        pass
            if os.name != "nt" and (not self._close_input_first or self.proc.poll() is None):
                try:
                    os.killpg(self.proc.pid, signal.SIGTERM)
                    self.shutdown_facts["sigterm_sent"] = True
                except ProcessLookupError:
                    pass
            elif self.proc.poll() is None:
                self.proc.terminate()
                self.shutdown_facts["sigterm_sent"] = True
            try:
                self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            if os.name != "nt" and (not self._close_input_first or self.proc.poll() is None):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                    self.shutdown_facts["sigkill_sent"] = True
                except ProcessLookupError:
                    pass
            elif self.proc.poll() is None:
                self.proc.kill()
                self.shutdown_facts["sigkill_sent"] = True
            self.proc.wait(timeout=2)
        finally:
            self.shutdown_facts["returncode"] = self.proc.poll()
            self._stop_readers.set()
            if self._registry:
                self._registry.unregister(self.proc.pid)
            for reader in self._readers:
                reader.join(timeout=1)
            def close_streams():
                for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                    if stream:
                        stream.close()
            if any(worker.is_alive() for worker in [*self._readers, *self._writers]):
                # Windows anonymous pipes cannot use select; close off the caller.
                threading.Thread(target=close_streams, daemon=True).start()
            else:
                close_streams()

    def list_tools(self) -> list[dict]:
        resp = self._rpc("tools/list")
        return (resp or {}).get("result", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict) -> dict:
        resp = self._rpc("tools/call", {"name": name, "arguments": arguments})
        return (resp or {}).get("result", (resp or {}).get("error", {}))


def _log(kind: str, payload: dict):
    """MCP 调用落盘(遵 CLAUDE.md 完整 trace·与 subagent_logs 同款)。fail-safe。"""
    try:
        _MCP_LOG_DIR.mkdir(exist_ok=True)
        stamp = str(int(payload.get("_t0", 0)) or len(list(_MCP_LOG_DIR.glob("mcp_*"))))
        f = _MCP_LOG_DIR / f"mcp_{kind}_{payload.get('server','_')}_{stamp}.json"
        f.write_text(json.dumps(payload, ensure_ascii=False, indent=2)[:20000])
    except Exception:
        pass


def list_mcp_tools() -> list[dict[str, Any]]:
    """客观列出所有已注册 MCP server 的可用工具。无评分/排序/推荐。

    返回每工具:server/name/description/input_schema。server 起不来→标 error,
    模型自行判断是否可用(和 read_harness_history 同样把客观事实交给模型)。
    """
    out: list[dict] = []
    try:
        service = _ROOT.parent.parent / "release" / "legion-service"
        if str(service) not in sys.path:
            sys.path.insert(0, str(service))
        import cloud_desktop
        for tool in cloud_desktop.local_mcp_tools():
            out.append({"server": "cua-local-desktop", **tool})
    except Exception as exc:
        out.append({"server": "cua-local-desktop", "error": f"{type(exc).__name__}:{str(exc)[:120]}"})
    for name, spec in _load_servers().items():
        entry_base = {"server": name}
        try:
            with _client(name, spec) as c:
                for t in c.list_tools():
                    out.append({**entry_base, "name": t.get("name"),
                                "description": t.get("description", ""),
                                "input_schema": t.get("inputSchema", {})})
        except Exception as e:
            out.append({**entry_base, "error": f"{type(e).__name__}:{str(e)[:120]}"})
    return out


def call_mcp_tool(server: str, name: str, arguments: dict | None = None) -> dict:
    """真调某 MCP server 的某工具。模型自主决定调谁/参数。全程落 trace。

    不自动重试、不加护栏语义之外的逻辑;返回原始 result 供模型继续判断。
    """
    t0 = time.time()
    if server == "cua-local-desktop":
        try:
            service = _ROOT.parent.parent / "release" / "legion-service"
            if str(service) not in sys.path:
                sys.path.insert(0, str(service))
            import cloud_desktop
            out = cloud_desktop.local_mcp_call(name, arguments or {})
            out["elapsed_s"] = round(time.time() - t0, 1)
        except Exception as exc:
            out = {"server": server, "tool": name, "status": f"err:{type(exc).__name__}:{str(exc)[:160]}", "elapsed_s": round(time.time() - t0, 1)}
        _log("call", {**out, "_t0": t0, "arguments": arguments or {}})
        return out
    servers = _load_servers()
    if server not in servers:
        return {"status": "unknown_server", "server": server,
                "available": list(servers)}
    try:
        with _client(server, servers[server]) as c:
            result = c.call_tool(name, arguments or {})
        # A received MCP reply can still report a tool execution failure.
        # Preserve its complete result so the caller can act on the real error.
        failed = isinstance(result, dict) and result.get("isError") is True
        out = {"server": server, "tool": name, "status": "tool_error" if failed else "ok",
               "result": result, "elapsed_s": round(time.time() - t0, 1)}
    except Exception as e:
        out = {"server": server, "tool": name, "final": "",
               "status": f"err:{type(e).__name__}:{str(e)[:160]}",
               "elapsed_s": round(time.time() - t0, 1)}
    _log("call", {**out, "_t0": t0, "arguments": arguments or {}})
    return out


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        print(json.dumps(list_mcp_tools(), ensure_ascii=False, indent=2))
    elif cmd == "call":
        print(json.dumps(call_mcp_tool(sys.argv[2], sys.argv[3],
              json.loads(sys.argv[4]) if len(sys.argv) > 4 else {}),
              ensure_ascii=False, indent=2))
    else:
        print("usage: list | call <server> <tool> [json_args]")
