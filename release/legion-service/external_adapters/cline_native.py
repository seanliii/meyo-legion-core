"""Cline 2.5.0 配置、原生 taskId 与输出事实；不实现它的解题循环。

依据 v2.5.0-cli / 452733c3 的 provider-config、plain-text-task、disk，
以及同版已安装 cli.mjs。原生历史是权威，平台会话名不是 Cline taskId。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile

VERSION = "2.5.0"
REVISION = "452733c3fddb6fffa9b06fbd4c96a0f5e88400d8"


def _read(path: Path, default=None):
    if path.is_symlink():
        raise ValueError("Cline native state must not be a symlink")
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def _task_id(value):
    if (not isinstance(value, str) or not value or Path(value).name != value
            or value in {".", ".."} or any(ord(c) < 32 for c in value)):
        raise ValueError("Cline native taskId is not a safe file name")
    return value


def task_files(home: Path, sid: str) -> tuple[Path, list, dict]:
    """Only the named task; never discover an ID by newest-directory guessing."""
    sid = _task_id(sid)
    data = home / "data"
    task = data / "tasks" / sid
    if task.is_symlink() or not task.resolve().is_relative_to(home.resolve()):
        raise ValueError("Cline native task escapes private configuration")
    history = _read(data / "state/taskHistory.json", [])
    if not isinstance(history, list) or not any(
            isinstance(row, dict) and row.get("id") == sid for row in history):
        raise ValueError("Cline taskId is absent from the native history index")
    conversation = _read(task / "api_conversation_history.json")
    messages = _read(task / "ui_messages.json")
    if not isinstance(conversation, list) or not isinstance(messages, list):
        raise ValueError("Cline native conversation or UI history is missing")
    metadata = _read(task / "task_metadata.json", {})
    return task, messages, metadata if isinstance(metadata, dict) else {}


def session_home(workdir: Path, context: dict, checkpoint: dict) -> Path:
    workdir = Path(workdir).resolve(strict=True)
    base = workdir / ".sessions" / "cline"
    if not base.resolve().is_relative_to(workdir):
        raise ValueError("Cline private state escapes current workspace")
    owner = str(context.get("user_id") or "default")
    logical = str(context.get("session_id") or "")
    if not logical:
        raise ValueError("Cline requires the caller's logical session identity")
    if checkpoint.get("kind") not in (None, "cline_task"):
        raise ValueError("Cline checkpoint belongs to another native runtime")
    if checkpoint.get("user_id", owner) != owner:
        raise ValueError("Cline checkpoint belongs to another owner")
    if checkpoint.get("workdir") and Path(checkpoint["workdir"]).resolve() != workdir:
        raise ValueError("Cline checkpoint belongs to another workspace")
    slot = hashlib.sha256((owner + "\0" + logical).encode()).hexdigest()[:24]
    home = Path(checkpoint["config_dir"]) if checkpoint.get("config_dir") else base / slot
    if (not home.is_absolute() or home.is_symlink()
            or home.resolve().parent != base.resolve()):
        raise ValueError("Cline checkpoint config is outside the caller's private state")
    if checkpoint:
        if not home.is_dir():
            raise ValueError("Cline native configuration is missing; checkpoint preserved")
        task_files(home, checkpoint.get("session_id"))
    else:
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)
    return home.resolve()


def _write(path: Path, content: dict):
    if path.is_symlink():
        raise ValueError("Cline native config must not be a symlink")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".meyo-config-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            os.chmod(temporary, 0o600)
            json.dump(content, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def configure(home: Path, *, model: str, appid: str, gateway: str, session_id: str = "") -> None:
    """Merge the same fields as native quick auth; no secret-bearing argv."""
    if not model or not appid or not gateway:
        raise ValueError("Cline requires this request's model, API credential and gateway")
    data = home / "data"
    if data.is_symlink() or not data.resolve().is_relative_to(home.resolve()):
        raise ValueError("Cline data directory escapes private configuration")
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_path, secrets_path = data / "globalState.json", data / "secrets.json"
    state, secrets = _read(state_path, {}), _read(secrets_path, {})
    if not isinstance(state, dict) or not isinstance(secrets, dict):
        raise ValueError("Cline existing config is malformed; no reset performed")
    binding = dict(actModeApiProvider="openai", planModeApiProvider="openai",
                   actModeOpenAiModelId=model, planModeOpenAiModelId=model, openAiBaseUrl=gateway)
    task_settings = None
    if session_id:
        task, _, _ = task_files(home, session_id)
        task_settings_path = task / "settings.json"
        task_settings = _read(task_settings_path, {})
        if not isinstance(task_settings, dict):
            raise ValueError("Cline task settings are malformed; original state preserved")
        # Native precedence is task settings > global. These are connection
        # settings, not historical messages or model_usage evidence. Keep all
        # unrelated native preferences and bind this request before loading it.
        task_settings.update(binding)
    state.update(binding, welcomeViewCompleted=True)
    state.setdefault("telemetrySetting", "disabled")
    secrets["openAiApiKey"] = appid
    if task_settings is not None:
        _write(task_settings_path, task_settings)
    _write(state_path, state)
    _write(secrets_path, secrets)


def result_from_native(records: list[dict], returncode: int, home: Path, workdir: Path,
                       context: dict, checkpoint: dict, *, started_ms: int,
                       requested_model: str, cancelled: bool = False,
                       old_message_ts=()) -> dict:
    result = {"type": "cline_native_result", "status": "harness_runtime", "final": "",
              "error": "", "native_checkpoint": checkpoint or None, "resumed_native": False,
              "model_used": None, "requested_model": requested_model, "usage": None,
              "native_facts": {"native_version": VERSION, "process_returncode": returncode}}
    old_ts = set(old_message_ts)
    records = [row for row in records if isinstance(row, dict)]
    ids = {row.get("taskId") for row in records if row.get("type") == "task_started"}
    if len(ids) > 1:
        result["error"] = "Cline returned conflicting native task identities"
        return result
    sid = next(iter(ids), None)
    if sid:
        try:
            _task_id(sid)
            if checkpoint.get("session_id") and checkpoint["session_id"] != sid:
                raise ValueError("Cline taskId changed during resume")
            result.update(
                session_id=sid,
                native_checkpoint={"kind": "cline_task", "session_id": sid,
                                   "config_dir": str(home), "workdir": str(workdir.resolve()),
                                   "user_id": str(context.get("user_id") or "default")},
            )
            result["native_facts"]["native_id_received"] = True
            # Received identity survives a later flush/persistence failure. It
            # does not claim the corresponding native history is already usable.
            task, stored, metadata = task_files(home, sid)
            result["resumed_native"] = bool(checkpoint)
            models = metadata.get("model_usage") or []
            latest = next((row for row in reversed(models) if isinstance(row, dict)), {})
            result["model_used"] = latest.get("model_id")
            result["native_facts"].update(native_provider=latest.get("model_provider_id"),
                                          task_path=str(task), native_history_verified=True)
            # stdout replays old history and suppresses same-ts usage updates.
            # Read only this task's persisted rows; never sum the cumulative index.
            usage_rows = {}
            for row in stored:
                if (not isinstance(row, dict) or row.get("say") != "api_req_started"
                        or row.get("ts", 0) <= started_ms or row.get("ts") in old_ts):
                    continue
                try:
                    usage = json.loads(row.get("text") or "{}")
                except (ValueError, TypeError):
                    continue
                if isinstance(usage, dict) and any(k in usage for k in ("tokensIn", "tokensOut")):
                    usage_rows[row["ts"]] = usage
            if usage_rows:
                keys = {"input_tokens": "tokensIn", "output_tokens": "tokensOut",
                        "cache_read_tokens": "cacheReads", "cache_write_tokens": "cacheWrites"}
                result["usage"] = {
                    key: sum(row[field] for row in usage_rows.values()
                             if isinstance(row.get(field), (int, float)))
                    for key, field in keys.items()
                    if any(isinstance(row.get(field), (int, float)) for row in usage_rows.values())}
                result["usage"].update(source="cline_native_ui_history", granularity="turn")
        except (ValueError, OSError, TypeError) as exc:
            result["error"] = str(exc)
            return result
    errors = []
    complete = []
    for row in records:
        if row.get("type") == "error":
            errors.append(str(row.get("message") or "Cline native error"))
        if row.get("say") == "error" or row.get("ask") == "api_req_failed":
            errors.append(str(row.get("text") or "Cline native API error"))
        ts = row.get("ts")
        if (row.get("say") == "completion_result" and not row.get("partial")
                and isinstance(ts, (int, float)) and ts > started_ms and ts not in old_ts
                and isinstance(row.get("text"), str) and row["text"].strip()):
            complete.append(row)
    if cancelled:
        result.update(status="cancelled", error="Cline caller cancelled")
    elif returncode != 0 or errors:
        result["error"] = "; ".join(dict.fromkeys(errors)) or f"Cline native exit {returncode}"
    elif not sid:
        result["error"] = "Cline emitted no native taskId; no platform ID substituted"
    elif not complete:
        result["error"] = "Cline emitted no completion for this turn"
    elif result["model_used"] and result["model_used"] != requested_model:
        result["error"] = "Cline native model differs from the current request"
    else:
        result.update(status="ok", final=complete[-1]["text"])
    return result
