"""Omnigent 原生配置的请求绑定；不修改 daemon、vendor 或进程全局环境。"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


class NativeBindingError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _service_import_path():
    """Known source/release siblings only; never add native CLI site-packages here."""
    here = Path(__file__).resolve()
    for service in (here.parents[3] / "legion-service",
                    here.parents[4] / "release/legion-service"):
        if (service / "model_registry.py").is_file():
            if str(service) not in sys.path:
                sys.path.insert(0, str(service))
            return


def _cli_runtime(cli: str, environment: dict) -> tuple[list[str], dict, str]:
    """Resolve supported original console entries without evaluating shell code.

    Keep the *lexical* venv Python path: resolving its symlink would select the
    base interpreter and lose pyvenv.cfg. Shell forms are the existing publisher
    PYTHONPATH wrapper and the distlib long-shebang wrapper, not arbitrary shell.
    """
    env = dict(environment)
    # Native venv owns its dependencies. Do not inherit the service vendor or
    # a foreign interpreter home. A supported publisher wrapper below may
    # explicitly restore its own declared package path.
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    try:
        entry = Path(cli).resolve()
        with entry.open() as source:
            lines = [source.readline().strip() for _ in range(3)]
        if not lines[0].startswith("#!"):
            raise ValueError("not a supported console script")
        words = shlex.split(lines[0][2:])
        if Path(words[0]).name == "env":
            words = words[1:]
            if words and words[0] == "-S":
                words = words[1:]
        elif Path(words[0]).name in ("sh", "bash"):
            if len(words) != 1:
                raise ValueError("unsupported shell flags")
            if lines[1].startswith("'''exec' ") and lines[2] == "' '''":
                parts = shlex.split(lines[1][len("'''exec' "):])
                if parts[-2:] != ["$0", "$@"]:
                    raise ValueError("invalid distlib wrapper")
                words = parts[:-2]
            else:
                assignment = shlex.split(lines[1])
                command = shlex.split(lines[2])
                if (len(assignment) != 2 or assignment[0] != "export"
                        or not assignment[1].startswith("PYTHONPATH=")
                        or len(command) != 4 or command[0] != "exec" or command[-1] != "$@"):
                    raise ValueError("unsupported shell wrapper")
                value = assignment[1].split("=", 1)[1]
                if "$" in value or "`" in value or not Path(command[2]).is_absolute():
                    raise ValueError("wrapper requires shell expansion")
                env["PYTHONPATH"] = value
                words = [command[1]]
                entry = Path(command[2]).resolve()
                if not entry.is_file():
                    raise ValueError("inner console script missing")
        if (not words or not re.fullmatch(r"(?:python(?:\d+(?:\.\d+)*)?|pypy\d*)", Path(words[0]).name)
                or any(x not in ("-B", "-E", "-I", "-O", "-OO", "-P", "-S", "-s", "-u")
                       for x in words[1:])):
            raise ValueError("unsupported Python command")
        python = words[0] if os.path.isabs(words[0]) else shutil.which(words[0], path=env.get("PATH"))
        if not python or not os.path.isfile(python) or not os.access(python, os.X_OK):
            raise ValueError("native Python missing")
        return [python, *words[1:]], env, str(entry.parent)
    except (OSError, ValueError, IndexError) as exc:
        raise NativeBindingError(
            "native_cli_runtime_unsupported",
            "无法从原 Omnigent CLI 入口确定受支持的 Python 运行环境；未退回服务 Python") from exc


def _run_native_helper(cli: str, environment: dict, request: dict) -> dict:
    """Native-only imports run in the CLI's interpreter, registered and bounded."""
    _service_import_path()
    import legion_proc_registry as registry
    if __package__:
        from .omnigent_responses_bridge import _stop_owned
    else:
        from omnigent_responses_bridge import _stop_owned
    command, env, entry_dir = _cli_runtime(cli, environment)
    script = Path(__file__).with_name("omnigent_launcher_cli.py")
    proc = subprocess.Popen(
        [*command, str(script)], env=env, start_new_session=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        if not registry.register(proc.pid, pgid=proc.pid, owner=os.getpid(), comm=command[0],
                                 meta={"kind": "omnigent_native_helper", "operation": request["operation"]}):
            raise NativeBindingError("native_helper_registration_failed", "原生配置进程登记失败")
        try:
            stdout, stderr = proc.communicate(
                json.dumps({**request, "entry_dir": entry_dir}), timeout=30)
        except subprocess.TimeoutExpired as exc:
            raise NativeBindingError("native_helper_timeout", "原生配置进程超时；未调用模型") from exc
        if proc.returncode:
            detail = stderr
            if request.get("api_key"):
                detail = detail.replace(request["api_key"], "[redacted]")
            raise NativeBindingError("native_helper_failed", "原生配置进程失败：" + detail[-800:])
        try:
            result = json.loads(stdout)
        except ValueError as exc:
            raise NativeBindingError("native_helper_protocol_error", "原生配置进程未返回有效 JSON") from exc
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise NativeBindingError("native_helper_protocol_error", "原生配置进程未确认成功")
        return result
    finally:
        _stop_owned(proc, registry)


def resolve_binding(model: str, appid: str, gateway: str, sub: str, env: dict,
                    resume_id: str = "", checkpoint: dict | None = None) -> dict:
    """只解析这次调用的值。无法证明旧会话一致时不发送下一轮。"""
    _service_import_path()
    import model_registry

    if sub not in ("openai-agents", "openai-agents-sdk"):
        raise NativeBindingError("native_binding_unsupported",
                                 "当前 Omnigent 子家没有已接通的逐请求授权接口")
    if not model or not appid or not gateway:
        raise NativeBindingError("request_binding_incomplete",
                                 "显式 Omnigent 请求需要非空 model、appid 和 gateway；未退回默认配置")
    key = model_registry._resolve_alias(appid, environment=env)
    if not key:
        raise NativeBindingError("request_appid_unresolved",
                                 "当前请求的 appid 别名无法解析；未使用目录或环境中的其他凭证")
    parsed = urlsplit(gateway)
    if (parsed.scheme not in ("http", "https") or not parsed.netloc
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise NativeBindingError("request_gateway_invalid", "当前请求的 gateway 不是有效的 API base URL")
    endpoint = model_registry.responses_endpoint(model, gateway)
    if not endpoint and parsed.path.rstrip("/").endswith("/chat/completions"):
        raise NativeBindingError(
            "request_gateway_protocol_mismatch", "当前原生 SDK 使用 Responses，不能把 Chat endpoint 当作 base URL")
    base_url = endpoint["base_url"] if endpoint else gateway.rstrip("/")
    headers = endpoint.get("http_headers", {}) if endpoint else {}
    # Native CLI resume 不重新上传 bundle；相同 model 的 runner 又复用旧 client。
    # 指纹放回原 checkpoint，不另建会话/全局映射，也不持久化明文凭证。
    values = [model, key, base_url, headers, sub,
              env.get("MEYO_USER_ID", ""), env.get("MEYO_SESSION_ID", ""),
              env.get("MEYO_WORKDIR", "")]
    fingerprint = hashlib.sha256(json.dumps(
        values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    stamp = {"version": 1, "sha256": fingerprint}
    if resume_id or checkpoint:
        if (not isinstance(checkpoint, dict)
                or checkpoint.get("kind") != "omnigent_conversation"
                or not resume_id or checkpoint.get("session_id") != resume_id
                or not checkpoint.get("request_binding")):
            raise NativeBindingError("native_binding_unverified",
                                     "旧 Omnigent 会话缺少可核对的请求绑定；未更新授权、未新建会话")
        if checkpoint["request_binding"] != stamp:
            raise NativeBindingError(
                "native_binding_change_unsupported",
                "当前 Omnigent CLI 无法在原会话更新模型、授权、网关或请求归属；已保留原会话，未发送本轮")
    # Native ApiKeyAuth has no header field. The existing bridge's explicitly
    # selected transparent mode carries these headers without a Chat conversion.
    return {"model": model, "api_key": key, "base_url": base_url, "stamp": stamp,
            "http_headers": dict(headers), "wire_api": "responses"}


@contextlib.contextmanager
def native_launcher(binding: dict, sub: str, *, cli: str | None = None,
                    environment: dict | None = None):
    """Parent owns the temporary file; native factory/YAML stay in the CLI runtime."""
    env = dict(os.environ if environment is None else environment)
    entry = cli if cli is not None else shutil.which("omnigent", path=env.get("PATH"))
    if not entry:
        raise NativeBindingError("native_cli_runtime_unsupported", "Omnigent CLI 未找到")
    with tempfile.TemporaryDirectory(prefix="meyo-omnigent-launcher-") as directory:
        path = Path(directory) / "agent.yaml"
        _run_native_helper(entry, env, {
            "operation": "launcher", "output": str(path), "harness": sub, "model": binding["model"],
            "api_key": binding.get("client_api_key", binding["api_key"]),
            "base_url": binding.get("client_base_url", binding["base_url"]),
        })
        if not path.is_file():
            raise NativeBindingError("native_launcher_missing", "原生配置进程未生成 launcher 文件")
        yield path


def checkpoint_for(native_id: str, binding: dict | None) -> dict | None:
    if not native_id:
        return None
    result = {"kind": "omnigent_conversation", "session_id": native_id}
    if binding:
        result["request_binding"] = binding["stamp"]
        if binding.get("bridge"):
            result["bridge"] = dict(binding["bridge"])
    return result


def binding_failure(exc: NativeBindingError, elapsed: float, resume_id: str,
                    checkpoint: dict | None) -> dict:
    result = {
        "status": "error", "final": "", "elapsed": elapsed, "error": str(exc),
        "fail_kind": "harness_runtime", "resumed_native": False,
        "native_checkpoint": checkpoint or checkpoint_for(resume_id, None),
        "error_envelope": {"category": "harness_runtime", "scope": "harness",
                           "retryable": False, "tool": "omnigent",
                           "code": exc.code, "message": str(exc)},
    }
    if resume_id:
        result["native_resume_error"] = {"stage": "request_binding", "session_id": resume_id,
                                         "code": exc.code}
    return result
