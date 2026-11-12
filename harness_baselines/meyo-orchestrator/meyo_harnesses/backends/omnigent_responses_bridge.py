"""Launch only the existing Responses bridge, with an immutable per-session binding."""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

if __package__:
    from .omnigent_request_binding import NativeBindingError
else:
    from omnigent_request_binding import NativeBindingError


def _stop_owned(proc, registry):
    """Reap this one owned group, never all groups belonging to the worker."""
    try:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
        # The leader can exit on TERM while a descendant ignores it. Reap the
        # remaining owned group before removing its only registry entry.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    finally:
        if proc.poll() is not None:
            registry.unregister(proc.pid)


@contextlib.contextmanager
def transparent_bridge(binding: dict, environment: dict, checkpoint: dict | None = None):
    """A cached native client keeps its URL/token across bridge process restarts.

    The checkpoint contains only a loopback port and binding fingerprint. The
    local capability token is reproducible from the current authorized key, but
    is not that upstream key. A busy saved port fails; it never switches to a new
    port or borrows someone else's daemon.
    """
    limit = binding.get("max_output_tokens")
    if "max_output_tokens" in binding and (type(limit) is not int or limit <= 0):
        raise NativeBindingError("bridge_output_budget_invalid", "显式输出预算须为正整数")
    stream_idle = binding.get("stream_idle_timeout_ms")
    try:
        if "stream_idle_timeout_ms" in binding and (
                type(stream_idle) is not int or stream_idle <= 0
                or not stream_idle / 1000 < float("inf")):
            raise ValueError()
    except (ValueError, OverflowError):
        raise NativeBindingError("bridge_stream_timeout_invalid", "显式流等待预算须为正有限整数") from None
    error_profile = binding.get("error_compatibility_profile")
    image_files = binding.get("image_file_cache")
    if ("error_compatibility_profile" in binding
            and error_profile != "invalid-prompt-max-length-v1"):
        raise NativeBindingError("bridge_error_profile_invalid", "显式错误兼容配置不受支持")
    if (not binding.get("http_headers") and limit is None and stream_idle is None
            and error_profile is None and image_files is None):
        yield binding
        return
    if os.name != "posix":
        raise NativeBindingError("bridge_runtime_unsupported", "此平台暂不支持已登记的 socket 继承启动")
    import legion_proc_registry as registry

    saved = (checkpoint or {}).get("bridge")
    binding_id = binding["stamp"]["sha256"]
    if checkpoint and (
            not isinstance(saved, dict) or saved.get("kind") != "responses_passthrough"
            or saved.get("version") != 1 or saved.get("binding_id") != binding_id
            or type(saved.get("port")) is not int or not 0 < saved["port"] < 65536):
        raise NativeBindingError("native_bridge_unverified", "原生会话缺少可核对的透明桥地址；未新建会话")
    token = hmac.new(binding["api_key"].encode(),
                     ("meyo-omnigent-local:" + binding_id).encode(), hashlib.sha256).hexdigest()
    # Reuse the existing resolver: release uses legion/tools, source uses comparison.
    orchestrator = Path(__file__).resolve().parents[2]
    if str(orchestrator) not in sys.path:
        sys.path.insert(0, str(orchestrator))
    from harness_registry import _find_bridge
    bridge = _find_bridge()
    if not bridge:
        raise NativeBindingError("responses_bridge_missing", "未找到发行包或源码树中的既有 Responses 桥")
    with (socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener,
          tempfile.TemporaryFile() as log, contextlib.ExitStack() as activity_handles):
        activity_reader = activity_writer = None
        if stream_idle is not None:
            read_fd, write_fd = os.pipe()
            activity_reader = activity_handles.enter_context(os.fdopen(read_fd, "rb", buffering=0))
            activity_writer = activity_handles.enter_context(os.fdopen(write_fd, "wb", buffering=0))
            os.set_blocking(read_fd, False)
            os.set_blocking(write_fd, False)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", saved["port"] if saved else 0))
        except OSError as exc:
            raise NativeBindingError(
                "native_bridge_port_unavailable", "原生会话透明桥端口不可用；未复用其他服务或更换会话") from exc
        listener.listen(128)
        port = listener.getsockname()[1]
        descriptor = {"kind": "responses_passthrough", "version": 1,
                      "port": port, "binding_id": binding_id}
        config = {"binding_id": binding_id, "model": binding["model"],
                  "base_url": binding["base_url"], "api_key": binding["api_key"],
                  "client_token": token, "http_headers": binding["http_headers"],
                  "wire_api": "responses",
                  "timeout_s": environment.get("LEGION_RESPONSES_UPSTREAM_TIMEOUT", "180")}
        if limit is not None:
            config["max_output_tokens"] = limit
        if stream_idle is not None:
            config["stream_idle_timeout_ms"] = stream_idle
        if error_profile is not None:
            config["error_compatibility_profile"] = error_profile
        if image_files is not None:
            # Use the native user's original private state, never cwd or a task-selected path.
            home = environment.get("CODEX_HOME")
            if not home and environment.get("HOME"):
                home = str(Path(environment["HOME"]) / ".codex")
            user = binding.get("image_file_cache_user")
            workdir = binding.get("image_file_cache_workdir")
            if (not home or not Path(home).is_absolute() or not isinstance(user, str)
                    or not user.strip() or not workdir or not Path(workdir).is_absolute()
                    or Path(home).resolve().is_relative_to(Path(workdir).resolve())):
                raise NativeBindingError("image_files_state_invalid", "Files 缓存需要原用户私有状态及明确归属，不能位于工程目录")
            config.update(image_file_cache=image_files,
                          image_file_cache_root=str(Path(home).resolve() / "meyo-image-files"),
                          image_file_cache_user=user)
        env = dict(environment, MEYO_RESPONSES_BRIDGE_MODE="passthrough",
                   MEYO_RESPONSES_BRIDGE_BINDING=json.dumps(config))
        env.pop("MEYO_RESPONSES_ACTIVITY_FD", None)
        pass_fds = (listener.fileno(),)
        if activity_writer is not None:
            env["MEYO_RESPONSES_ACTIVITY_FD"] = str(activity_writer.fileno())
            pass_fds += (activity_writer.fileno(),)
        # Retain the bound socket through exec; there is no release/rebind race.
        proc = subprocess.Popen(
            [sys.executable, str(bridge), "--port", str(port), "--fd", str(listener.fileno())],
            env=env, pass_fds=pass_fds, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=log,
        )
        if activity_writer is not None:
            activity_writer.close()  # Only the owned bridge keeps the write end.
        try:
            if not registry.register(proc.pid, pgid=proc.pid, owner=os.getpid(), comm=sys.executable,
                                     meta={"kind": "omnigent_responses_bridge", "binding_id": binding_id}):
                raise NativeBindingError("bridge_registration_failed", "透明桥进程登记失败；未执行原生请求")
            deadline = time.monotonic() + 15
            ready = False
            with httpx.Client(timeout=0.2, trust_env=False) as client:
                while time.monotonic() < deadline and proc.poll() is None:
                    try:
                        data = client.get(f"http://127.0.0.1:{port}/health").json()
                        ready = (data.get("mode") == "passthrough" and data.get("wire_api") == "responses"
                                 and data.get("binding_id") == binding_id)
                        if ready:
                            break
                    except (httpx.HTTPError, ValueError):
                        pass
                    time.sleep(0.05)
            if not ready:
                log.seek(0)
                detail = log.read().decode(errors="replace")
                for secret in (binding["api_key"], token):
                    detail = detail.replace(secret, "[redacted]")
                raise NativeBindingError("bridge_startup_failed", "透明桥启动失败：" + detail[-1200:])
            yield {**binding, "bridge": descriptor,
                   "client_base_url": f"http://127.0.0.1:{port}/v1", "client_api_key": token,
                   **({"runtime_activity_fd": activity_reader.fileno()} if activity_reader is not None else {})}
        finally:
            _stop_owned(proc, registry)
