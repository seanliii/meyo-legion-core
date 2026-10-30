"""Prime Intellect JSONL 会话接线；执行/进程回收仍由现有 CLI transport 负责。

依据固定源码 1eee2938 的 session-manager.ts、print-mode.ts；
不是答题器，不迁移/清空旧 kernel 快照。能力边界见同目录 prime_native.md。
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
from uuid import UUID

from external_agent_transports import _final_from_json, _format, _usage_records, run_transport

REVISION = "1eee2938b4eeb7a4d72e17035adda669a89b63de"
SESSION_FORMAT = "prime_jsonl_v3"


class PrimeSessionError(ValueError):
    pass


def read_session(path: str, expected_id: str = "") -> tuple[dict, bool]:
    """严格只读；native 的修复/新建行为不得掩盖坏 checkpoint。"""
    file = Path(path)
    if not file.is_absolute() or file.suffix != ".jsonl" or file.is_symlink() or not file.is_file():
        raise PrimeSessionError("prime_checkpoint_missing_or_unsafe")
    try:
        with file.open(encoding="utf-8") as stream:
            rows = [json.loads(line) for line in stream if line.strip()]
        header = rows[0]
        if (not isinstance(header, dict) or header.get("type") != "session"
                or header.get("version") != 3 or not isinstance(header.get("id"), str)
                or not isinstance(header.get("cwd"), str) or not Path(header["cwd"]).is_absolute()
                or not isinstance(header.get("timestamp"), str)):
            raise ValueError("invalid v3 header")
        UUID(header["id"])
        if expected_id and header["id"] != expected_id:
            raise ValueError("native ID mismatch")
        ids = set()
        used_kernel = False
        for row in rows[1:]:
            if (not isinstance(row, dict) or not isinstance(row.get("type"), str)
                    or row["type"] == "session" or not isinstance(row.get("id"), str)
                    or not row["id"] or row["id"] in ids
                    or "parentId" not in row
                    or (row["parentId"] is not None and row["parentId"] not in ids)):
                raise ValueError("invalid session tree")
            ids.add(row["id"])
            if row["type"] == "message":
                message = row.get("message")
                if not isinstance(message, dict) or not isinstance(message.get("role"), str):
                    raise ValueError("invalid message")
                content = message.get("content")
                if not isinstance(content, (str, list)):
                    raise ValueError("invalid message content")
                used_kernel |= message.get("toolName") == "ipython" or (
                    isinstance(content, list) and any(
                        isinstance(part, dict) and part.get("type") == "toolCall"
                        and part.get("name") == "ipython" for part in content))
        return header, used_kernel
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise PrimeSessionError("prime_checkpoint_invalid_v3") from exc


def checkpoint_details(checkpoint: dict) -> dict:
    """通用 transport 只保存 session_id；从原生文件旁读回本引导器保存的身份。"""
    if not checkpoint:
        return {}
    if not isinstance(checkpoint, dict) or checkpoint.get("kind") not in (None, "prime_session"):
        raise PrimeSessionError("prime_checkpoint_wrong_kind")
    # 旧 core 的 session_id 是完整路径；新结构明确分开原生 ID 和路径。
    path = checkpoint.get("session_path") or checkpoint.get("session_id")
    if not isinstance(path, str):
        raise PrimeSessionError("prime_checkpoint_missing_path")
    expected = str(checkpoint.get("session_id") or "") if checkpoint.get("session_path") else ""
    header, _ = read_session(path, expected)
    receipt = Path(path).parent.parent / "session-artifacts" / header["id"] / "meyo-prime-runtime.json"
    saved = {}
    if receipt.exists():
        saved = json.loads(receipt.read_text())
        if (not isinstance(saved, dict) or saved.get("session_id") != header["id"]
                or saved.get("session_path") != path or saved.get("kind") != "prime_session"):
            raise PrimeSessionError("prime_runtime_receipt_identity_mismatch")
        for key in ("logical_session_id", "daemon_socket", "config_dir", "runtime_revision"):
            if key in checkpoint and checkpoint[key] != saved.get(key):
                raise PrimeSessionError("prime_runtime_receipt_conflict:" + key)
    return {**saved, **checkpoint, "kind": "prime_session",
            "session_id": header["id"], "session_path": path}


def prepare_resume(checkpoint: dict) -> tuple[str, str]:
    checkpoint = checkpoint_details(checkpoint)
    if not checkpoint:
        return "", ""
    path = checkpoint["session_path"]
    header, used_kernel = read_session(path, checkpoint["session_id"])
    artifact_dir = Path(path).parent.parent / "session-artifacts" / header["id"]
    snapshot = artifact_dir / "kernel-state.dill"
    manifest = artifact_dir / "kernel-state.json"
    # 0.8 已有自动 snapshot/restore；不能假设换新进程就不需要迁移状态。
    if checkpoint.get("runtime_revision") != REVISION and (
            snapshot.exists() or manifest.exists() or used_kernel):
        raise PrimeSessionError("prime_legacy_kernel_resume_unverified: 保留旧会话及快照，未启动新版")
    if used_kernel and not snapshot.is_file():
        raise PrimeSessionError("prime_kernel_snapshot_missing: 不以空 kernel 冒充恢复")
    return path, header["id"]


def parse_events(records: list[dict]) -> dict:
    """复用通用文本/usage 读取；仅 message_end 计数，turn_end 是同一消息的重复。"""
    texts, usage, headers = [], [], []
    error = ""
    for event in records:
        if event.get("type") == "session":
            headers.append(event)
        if event.get("type") == "error":
            error = str(event.get("error") or event.get("message") or "prime_native_error")
        if event.get("type") != "message_end":
            continue
        message = event.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        if message.get("stopReason") in ("error", "aborted"):
            error = str(message.get("errorMessage") or message["stopReason"])
        content = message.get("content")
        visible = ([part for part in content if isinstance(part, dict) and part.get("type") == "text"]
                   if isinstance(content, list) else content if isinstance(content, str) else "")
        text = _final_from_json(visible)
        if text and (not texts or texts[-1] != text):
            texts.append(text)
        for row in _usage_records([{"usage": message.get("usage"), "model": message.get("model")}]):
            row["source"] = "prime_message"
            cost = row.get("cost")
            if isinstance(cost, dict):
                row["cost_usd"] = cost.get("total")
            usage.append(row)
    return {"final": "" if error else "\n\n".join(texts), "usage_records": usage,
            "error": error, "headers": headers}


def _records(result: dict) -> list[dict]:
    # run_cli.events 仅末100条；必须从现有完整私有日志读，不能漏前段答案/费用。
    snapshot = result.get("terminal_snapshot") or (result.get("native_facts") or {}).get("terminal_log") or {}
    stdout = (snapshot.get("streams") or {}).get("stdout") or {}
    if not stdout.get("path"):
        raise PrimeSessionError("prime_native_stdout_missing")
    records = []
    with Path(stdout["path"]).open(encoding="utf-8") as stream:
        for line in stream:
            if not line.lstrip().startswith("{"):
                continue
            try:
                value = json.loads(line)
            except ValueError as exc:
                raise PrimeSessionError("prime_native_jsonl_invalid") from exc
            if isinstance(value, dict):
                records.append(value)
    return records


def validate_native_runtime(command: list[str], context: dict, models: dict) -> tuple[Path, Path]:
    """共享既有运行身份检查，前台 daemon 与 print CLI 必须在 spawn 前一致。"""
    cli = Path(context["source"]) / "packages/coding-agent/dist/bundle/cli.js"
    if command[:2] != [context["node"], str(cli)] or not cli.is_file():
        raise PrimeSessionError("prime_cli_source_mismatch")
    if command[command.index("--provider") + 1] != "meyo":
        raise PrimeSessionError("prime_provider_mismatch")
    kernel = Path(context["runtime_python"])
    venv = Path(context["runtime_dir"]) / "venv"
    if kernel.parent.parent != venv or not kernel.is_file():
        raise PrimeSessionError("prime_kernel_runtime_mismatch")
    provider = models["providers"]["meyo"]
    if (provider.get("baseUrl") != context["gateway"]
            or provider.get("apiKey") != "MEYO_PRIME_INTELLECT_API_KEY"
            or not any(model.get("id") == context["model"] for model in provider.get("models", []))):
        raise PrimeSessionError("prime_model_config_mismatch")
    return kernel, venv


def run_native(item: dict, spec: dict, context: dict, env: dict, timeout_s: int, event_cb=None) -> dict:
    runtime = json.loads(context.get("runtime_context_json") or "{}")
    checkpoint = (runtime.get("task_memory") or {}).get("native_checkpoint") or {}
    result = {"status": "harness_runtime", "final": "", "resumed_native": False,
              "native_checkpoint": checkpoint or None, "usage_records": []}
    native_failure = None
    try:
        path, native_id = prepare_resume(checkpoint)
        if item.get("url", "").rstrip("/") != "https://github.com/PrimeIntellect-ai/prime-agent" or item.get("revision") != REVISION:
            raise PrimeSessionError("prime_source_revision_mismatch")
        context = {**context, "native_session_path": path}
        command = list(spec["command"]) if spec.get("resolved_argv") else _format(spec["command"], context)
        config = Path(context.get("prime_config_dir") or
                      Path(context["session_dir"]) / "prime-intellect" / context["session_id"] / "config")
        kernel, venv = validate_native_runtime(command, context, json.loads((config / "models.json").read_text()))
        sessions = Path(command[command.index("--session-dir") + 1])
        if path:
            if "--resume" not in command or command[command.index("--resume") + 1] != path:
                raise PrimeSessionError("prime_resume_command_missing")
            # 原 artifact 目录由 session-dir 的父目录推导；不能指向新逻辑会话目录。
            sessions = Path(path).parent
            command[command.index("--session-dir") + 1] = str(sessions)
        env = {**env, "PRIME_AGENT_CODING_AGENT_DIR": str(config),
               "PRIME_AGENT_SESSION_DIR": str(sessions),
               "PRIME_AGENT_KERNEL_PYTHON": str(kernel), "PRIME_AGENT_KERNEL_VENV": str(venv),
               "MEYO_PRIME_INTELLECT_API_KEY": context["appid"]}
        # 命令可能来自已渲染的 CLI argv；每个参数作为一次替换值，绝不二次改写任务字面量。
        literal_context = {**context, **{f"prime_arg_{i}": value for i, value in enumerate(command)}}
        native_spec = {**spec, "command": [f"{{prime_arg_{i}}}" for i in range(len(command))],
                       "snapshot_terminal": True}
        try:
            result.update(run_transport(native_spec, literal_context, env, timeout_s, event_cb))
        except subprocess.TimeoutExpired as exc:
            result.update(status="timeout", final="", error="prime_native_timeout",
                          native_facts=getattr(exc, "native_facts", {}))
        # 丢弃通用 parser 从任意文本 envelope 猜出的 session；只认下方文件读回。
        result["native_checkpoint"] = checkpoint or None
        result.pop("native_session_id", None)
        if result["status"] != "ok":
            native_failure = (result["status"], result.get("error", ""))
            result.update(final="", resumed_native=False)
            snapshot = result.get("terminal_snapshot") or (result.get("native_facts") or {}).get("terminal_log") or {}
            if not ((snapshot.get("streams") or {}).get("stdout") or {}).get("path"):
                # CLI 已失败且只有 stderr：无需寻找不存在的 header，原生错误及私有引用就是现场。
                result.pop("checkpoint", None)
                return result
        parsed = parse_events(_records(result))
        result["usage_records"] = parsed["usage_records"]
        result["final"] = parsed["final"] if result["status"] == "ok" else ""
        if not native_failure and (parsed["error"] or not result["final"]):
            result.update(status="error" if result["status"] == "ok" else result["status"],
                          final="", error=parsed["error"] or result.get("error") or "prime_no_assistant_final")
        headers = parsed["headers"]
        if len(headers) != 1 or headers[0].get("version") != 3:
            raise PrimeSessionError("prime_native_session_header_missing_or_ambiguous")
        observed_id = headers[0].get("id")
        UUID(observed_id)
        if native_id and observed_id != native_id:
            raise PrimeSessionError("prime_native_session_id_changed")
        session_path = path or str(sessions / f"{observed_id}.jsonl")
        read_session(session_path, observed_id)
        result["native_session_id"] = observed_id
        if result["status"] == "ok":
            result["native_checkpoint"] = {
                "kind": "prime_session", "session_id": observed_id, "session_path": session_path,
                "logical_session_id": context["session_id"], "runtime_revision": REVISION,
                "daemon_socket": command[command.index("--daemon-socket") + 1], "config_dir": str(config)}
            result["resumed_native"] = bool(path)
        elif not checkpoint:
            # 首轮失败也保存已核实的新文件；不把它称为成功恢复。
            result["native_checkpoint"] = {
                "kind": "prime_session", "session_id": observed_id, "session_path": session_path,
                "logical_session_id": context["session_id"], "runtime_revision": REVISION,
                "daemon_socket": command[command.index("--daemon-socket") + 1], "config_dir": str(config)}
        result["native_facts"] = {**(result.get("native_facts") or {}),
                                  "kernel_snapshot_restore": "not_verified"}
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        if native_failure:
            result.update(status=native_failure[0], error=native_failure[1], final="", resumed_native=False)
            result["native_facts"] = {**(result.get("native_facts") or {}), "output_parse_error": str(exc)}
        else:
            result.update(status="harness_runtime", final="", error=str(exc), resumed_native=False)
    # 通用 parser 的 checkpoint 猜测不应覆盖 Prime 的原始恢复凭据。
    if result["status"] != "ok" and checkpoint:
        result["native_checkpoint"] = checkpoint
    result.pop("checkpoint", None)
    return result


def unwrap_result(result: dict) -> dict:
    """core 薄入口读回本引导器的完整结果；失败用 registry 已有私有取证，不改通用解析器。"""
    candidates = [result]
    for failure in result.get("transport_failures") or []:
        if failure.get("private_result_path"):
            try:
                candidates.append(json.loads(Path(failure["private_result_path"]).read_text()))
            except (OSError, ValueError):
                pass
    for candidate in candidates:
        for event in reversed(candidate.get("events") or []):
            if isinstance(event, dict) and event.get("type") == "prime_native_result":
                native = event.get("prime_native_result")
                if isinstance(native, dict):
                    return {**result, **native}
    return result
