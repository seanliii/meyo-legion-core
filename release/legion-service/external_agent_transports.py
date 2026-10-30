#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部 Agent 的统一传输层。"""
from __future__ import annotations

from contextlib import contextmanager
import json
import hashlib
import os
import pathlib
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable


class TransportError(RuntimeError):
    pass


def _mark_process_start_failure(exc: OSError) -> None:
    """An OS spawn failure occurred before any native/model work could run."""
    exc.error_envelope = {
        "category": "harness_runtime", "retryable": False, "scope": "harness",
        "code": "native_process_start_failed", "message": str(exc),
    }


# 单遍正则替换(军团 deepseek dogfood 揪出严重凭证泄漏):旧代码顺序 str.replace 会二次扫描——
# 先替 {task} 插入用户输入·若用户输入含字面 {ironclaw_token}·下一轮循环把它替换成真密钥随请求外发。
# re.sub 单遍不重扫已插入内容;未匹配的占位符保持原样。
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _format(value: Any, context: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _PLACEHOLDER.sub(lambda m: str(context.get(m.group(1), m.group(0))), value)
    if isinstance(value, list):
        return [_format(part, context) for part in value]
    if isinstance(value, dict):
        return {key: _format(part, context) for key, part in value.items()}
    return value


# ANSI 转义序列剥离(gnhf 根修·2026-09-02 真跑揪出):TUI 型 CLI(gnhf 等)在非 tty 管道下
# 仍画全屏界面——stdout 是 \x1b[?1049h(进备用屏)\x1b[?25l(藏光标)\x1b[H(光标归零)+一坨
# 转义码着色的 banner/统计面板。旧 _final_from_text 按行 strip 后整段拼接,final 里全是
# 转义码,answer 明明在(notes.md/iteration jsonl 的 structured_output.summary="2")却被
# 当垃圾。剥掉 CSI/OSC/单字符控制符后按行过滤才有干净文本。通用层修复——所有 TUI 型
# external 家自动受益,不是 gnhf 一家补丁(护城河:一次修=全生态自动适配)。
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[a-zA-Z]"        # CSI 序列:颜色/光标/屏操作 (\x1b[2m 等)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC 序列:标题等 (\x1b]0;title\x07)
    r"|\x1b[()][0-9A-B]"              # 字符集选择 \x1b(B
    r"|\x1b[=>78MDEHc]"               # 单字符控制:进备用屏/重置等
)


def _strip_ansi(text: str) -> str:
    """剥 ANSI 转义序列;失败原样返回(绝不抛)。"""
    try:
        return _ANSI_RE.sub("", str(text or ""))
    except Exception:
        return str(text or "")


def _read_final_notes(note_spec: Any, cwd: str) -> str:
    """从 CLI 家的产物目录回读它自己写下的答案(gnhf 类 TUI 家 stdout 没有答案行)。

    note_spec 形如 {"path": ".gnhf/runs/<runId>/notes.md", "pattern": r"\\*\\*Summary:\\*\\* (.+)",
    "latest_dir": ".gnhf/runs"}:
      · path 固定相对 cwd;带 latest_dir 时 path 里可用 {latest} 占最新 run 目录名
        (gnhf 每次运行生成新 runId·catalog 不可能写死)。
      · pattern 对文件全文 search·group(1) 优先·整条 match 兜底;多次命中取最后一次
        (最新 iteration 的 Summary)。
    只在 final 为空时兜底或显式 prefer——绝不覆盖 transport 已提取的真答案。
    """
    try:
        if not isinstance(note_spec, dict):
            return ""
        base = str(cwd or ".")
        rel = str(note_spec.get("path") or "")
        latest_root = str(note_spec.get("latest_dir") or "")
        if latest_root and "{latest}" in rel:
            runs_root = os.path.join(base, latest_root)
            if not os.path.isdir(runs_root):
                return ""
            candidates = sorted(
                (os.path.join(runs_root, d) for d in os.listdir(runs_root) if os.path.isdir(os.path.join(runs_root, d))),
                key=os.path.getmtime, reverse=True)
            if not candidates:
                return ""
            rel = rel.replace("{latest}", os.path.basename(candidates[0]))
        full = os.path.realpath(os.path.join(base, rel))
        # 路径安全:只读 cwd 内的文件(产物目录),不许用 ../ 逃逸读任意文件。
        if not full.startswith(os.path.realpath(base) + os.sep):
            return ""
        if not os.path.isfile(full):
            return ""
        import re as _re
        with open(full, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        pattern = str(note_spec.get("pattern") or "")
        if not pattern:
            return ""
        matches = list(_re.finditer(pattern, text, _re.MULTILINE))
        if not matches:
            return ""
        # skip_patterns(可选):多 iteration 的 notes 里,收尾轮常写"[FAIL] 重复 no-op/已在上轮完成"
        # 这类元评论(gnhf stop-when 机制)——它不是答案。从最后一轮往回找第一条不匹配 skip 的
        # Summary(最近一次干净交付)。skip 未命中则保持取最后一条(默认语义)。
        skips = [str(s) for s in (note_spec.get("skip_patterns") or [])]
        m = None
        for cand in reversed(matches):
            try:
                val = (cand.group(1) or cand.group(0)).strip()
            except IndexError:
                val = cand.group(0).strip()
            if any(_re.search(s, val) for s in skips):
                continue
            m = val
            break
        if m is None:
            _last = matches[-1]
            try:
                return (_last.group(1) or _last.group(0)).strip()
            except IndexError:
                return _last.group(0).strip()
        return m
    except Exception:
        return ""


def _snapshot_terminal(stdout: str, cwd: str, stderr: str = "") -> dict:
    """原始终端流是私有运行证据，不是可下载成品；不剥内容、不只保存末段。"""
    try:
        if not stdout and not stderr:
            return {}
        import tempfile
        from artifact_delivery import _provenance
        root = pathlib.Path(cwd or ".").resolve()
        directory = root / "logs" / "native-terminal"
        if not directory.resolve().is_relative_to(root):
            raise ValueError("terminal log directory escapes workdir")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        streams = {}
        for name, text in (("stdout", stdout), ("stderr", stderr)):
            if not text:
                continue
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", errors="replace", dir=directory,
                    prefix="_terminal_snapshot_", suffix=f".{name}.log", delete=False) as output:
                os.chmod(output.name, 0o600)
                output.write(text)
            streams[name] = {"path": output.name, "name": pathlib.Path(output.name).name,
                             "bytes": os.path.getsize(output.name),
                             "provenance": _provenance(output.name, "native-terminal-log")}
        return {**(streams.get("stdout") or streams["stderr"]),
                "visibility": "private", "streams": streams}
    except (OSError, ValueError) as exc:
        return {"visibility": "private", "storage_error": type(exc).__name__}


def _final_from_json(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        # OpenClaw 自己的权威提取顺序是 meta.finalAssistantVisibleText → 可见 terminal payload
        # (src/agents/agent-run-result.ts:16-31)。payloads 可能只是 "exec" 等工具结果，不能抢在正文前。
        meta = value.get("meta")
        if isinstance(meta, dict):
            for key in ("finalAssistantVisibleText", "finalAssistantRawText"):
                if meta.get(key):
                    return _final_from_json(meta[key])
        for key in ("finalAssistantVisibleText", "finalAssistantRawText"):
            if value.get(key):
                return _final_from_json(value[key])
        if isinstance(value.get("payloads"), list) and value["payloads"]:
            visible = [p for p in value["payloads"]
                       if not isinstance(p, dict) or (p.get("visible") is not False and p.get("isError") is not True)]
            _txt = "\n".join(_final_from_json(p) for p in visible).strip()
            if _txt:
                return _txt
        for key in ("final", "output", "result", "inline_reply", "message", "content", "text"):
            if value.get(key):
                return _final_from_json(value[key])
    if isinstance(value, list):
        parts = [_final_from_json(item) for item in value]
        return "\n".join(part for part in parts if part).strip()
    return ""


_MISSING_OUTPUT_FIELD = object()


def _output_field(record: Any, field: str, default: Any = None) -> Any:
    """声明式字段路径；只查 JSON 对象，不执行表达式。"""
    value = record
    for part in field.split(".") if field else []:
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value if field else default


def _native_output_paths(spec: dict, cwd: str) -> list[pathlib.Path]:
    pattern = spec["output_contract"].get("source_glob")
    if (not isinstance(pattern, str) or not pattern or pathlib.Path(pattern).is_absolute()
            or ".." in pathlib.Path(pattern).parts):
        raise TransportError("native output source_glob must be relative to workdir")
    root = pathlib.Path(cwd).resolve()
    paths = []
    for candidate in root.glob(pattern):
        path = candidate.resolve()
        if not path.is_relative_to(root):
            raise TransportError("native output path escapes workdir")
        if path.is_file() and path not in paths:
            paths.append(path)
    return paths


def _native_output_snapshot(spec: dict, cwd: str) -> dict:
    from artifact_delivery import _provenance
    return {str(path): {"bytes": path.stat().st_size,
                       "sha256": _provenance(str(path), "native-output-before")["sha256"]}
            for path in _native_output_paths(spec, cwd)}


def _fresh_file_output(spec: dict, cwd: str, before: dict) -> dict:
    """只读本次新增/改写字节；最新迭代未给终态时不复活前一轮的答案。"""
    fresh = []
    for path in _native_output_paths(spec, cwd):
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        previous = before.get(str(path))
        if previous and digest == previous["sha256"]:
            continue  # touch/重新复制相同旧日志不构成本次原生输出。
        offset = 0
        if (previous and len(raw) >= previous["bytes"]
                and hashlib.sha256(raw[:previous["bytes"]]).hexdigest() == previous["sha256"]):
            offset = previous["bytes"]
            if offset and raw[offset - 1:offset] != b"\n":
                # 前一进程留下的半行不能被切片伪造成一个本次完整 JSONL 记录。
                boundary = raw.find(b"\n", offset)
                offset = len(raw) if boundary < 0 else boundary + 1
        fresh.append((path.stat().st_mtime_ns, str(path), raw, offset, digest))
    missing = {"record": {}, "final": "", "output_state": "incomplete"}
    if not fresh:
        return missing
    _, path, raw, offset, digest = max(fresh, key=lambda row: (row[0], row[1]))
    text = raw[offset:].decode("utf-8", errors="replace")
    numbered = []
    for line_number, line in enumerate(text.splitlines(), 1):
        try:
            numbered.append((line_number, json.loads(line)))
        except (TypeError, ValueError):
            continue
    output = _declared_json_output(text, [row for _, row in numbered], spec)
    if output is None:
        return missing
    from artifact_delivery import _provenance
    provenance = _provenance(path, "native-output-contract")
    if provenance["sha256"] != digest:
        raise TransportError("native output changed while being read")
    line_number = next((number for number, row in reversed(numbered)
                        if row == output["record"]), 1)
    output["output_ref"] = {
        "path": path, "visibility": "private", "byte_offset": offset,
        "record_line": raw[:offset].count(b"\n") + line_number, "provenance": provenance,
    }
    return output


def _declared_json_output(stdout: str, records: list[Any], spec: dict) -> dict | None:
    """None means unrecognized; a recognized envelope may have an empty final.

    Only declared native fields identify an envelope. Ordinary text transports
    keep their existing JSON/text fallback; native status is not a quality verdict.
    """
    contract = spec.get("output_contract")
    if not isinstance(contract, dict) or contract.get("type") != "json":
        return None
    text_field = contract.get("text_field")
    if not isinstance(text_field, str) or not text_field:
        raise TransportError("JSON output_contract requires text_field")
    status_field = contract.get("status_field") or ""
    session_field = contract.get("session_id_field") or ""
    success_field = contract.get("success_field") or ""
    error_field = contract.get("error_field") or ""
    error_message_field = contract.get("error_message_field") or ""
    if not all(isinstance(field, str) for field in
               (status_field, session_field, success_field, error_field, error_message_field)):
        raise TransportError("JSON output_contract field names must be strings")
    match = contract.get("record_match") or {}
    if not isinstance(match, dict) or not all(isinstance(key, str) for key in match):
        raise TransportError("JSON output_contract record_match must be a field mapping")
    completed = contract.get("completed_statuses")
    if status_field and (not isinstance(completed, list)
                         or not all(isinstance(value, str) for value in completed)):
        raise TransportError("JSON output_contract requires completed_statuses")

    # Status/session identify native envelopes, rather than earlier text deltas.
    # A text-only native protocol can explicitly declare just its text field.
    identifying_fields = [field for field in (status_field, session_field) if field] or [text_field]
    candidates = records
    try:
        whole = json.loads(stdout)
        if isinstance(whole, dict):
            candidates = [whole]
    except (TypeError, ValueError):
        pass
    record = next((row for row in reversed(candidates)
                   if isinstance(row, dict)
                   and all(_output_field(row, key, _MISSING_OUTPUT_FIELD) == value
                           for key, value in match.items())
                   and (match or any(_output_field(row, field, _MISSING_OUTPUT_FIELD)
                                     is not _MISSING_OUTPUT_FIELD for field in identifying_fields))), None)
    if record is None:
        return None

    text = _output_field(record, text_field)
    final = ("" if "source_glob" in contract and not isinstance(text, str)
             else _final_from_json(text))
    native_status = _output_field(record, status_field, _MISSING_OUTPUT_FIELD)
    native_success = _output_field(record, success_field, _MISSING_OUTPUT_FIELD)
    native_error = _output_field(record, error_field, _MISSING_OUTPUT_FIELD)
    failed = native_error is True or native_success is False
    ready = ((not status_field or native_status in completed)
             and (not success_field or native_success is True)
             and (not error_field or native_error is _MISSING_OUTPUT_FIELD or native_error is False))
    if failed or not ready:
        final = ""  # Running/aborted output remains in events, not a final answer.
    result = {"record": record, "final": final,
              "output_state": "error" if failed else "completed" if ready and final else "incomplete"}
    diagnostic = _output_field(record, error_message_field)
    if isinstance(diagnostic, str) and diagnostic.strip():
        result["native_error_message"] = diagnostic[-4000:]
    for key, value in (("native_status", native_status), ("native_success", native_success),
                       ("native_is_error", native_error)):
        if value is not _MISSING_OUTPUT_FIELD:
            result[key] = value
    session = _output_field(record, session_field)
    if session not in (None, ""):
        # A native ID is useful evidence; it does not declare resume support.
        result["native_session_id"] = str(session)
    return result


def _final_from_text(stdout: str, spec: dict) -> str:
    """按 manifest 声明提取纯文本 CLI 最终答案，不把启动 banner/统计当交付。"""
    # 先剥 ANSI(gnhf 类 TUI 家的转义码·见 _ANSI_RE 注释)再按行过滤,否则 ignore_patterns
    # 对着 "\x1b[2mfoo" 永远匹配不上,统计面板全混进 final。
    stdout = _strip_ansi(stdout)
    markers = spec.get("output_reasoning_markers")
    if isinstance(markers, dict) and markers.get("start") and markers.get("end"):
        start_lines = str(markers["start"]).splitlines()
        end_lines = str(markers["end"]).splitlines()
        raw_lines = stdout.splitlines()
        visible, hidden, index = [], False, 0
        while index < len(raw_lines):
            if raw_lines[index:index + len(start_lines)] == start_lines:
                hidden = True
                index += len(start_lines)
            elif hidden and raw_lines[index:index + len(end_lines)] == end_lines:
                hidden = False
                index += len(end_lines)
            else:
                if not hidden:
                    visible.append(raw_lines[index])
                index += 1
        stdout = "\n".join(visible)
    lines = [line.strip() for line in str(stdout or "").splitlines() if line.strip()]
    for pattern in spec.get("output_ignore_patterns") or []:
        try:
            matcher = re.compile(str(pattern))
        except re.error:
            continue
        lines = [line for line in lines if not matcher.search(line)]
    if spec.get("final_nonempty_line"):
        return lines[-1] if lines else ""
    return "\n".join(lines).strip()


def _stream_records_result(records: list[Any]) -> tuple[str, str]:
    """提取 Goose 等 stream-json 的 assistant 文本增量与结构化错误。

    Goose 每个 token 都是一条 type=message 记录，倒序取第一条只会得到最后一个标点。
    这里只消费协议本身的 role/content/type 字段，不按回答语义或长度猜交付。
    """
    text_parts, errors = [], []
    saw_stream_message = False
    for record in records:
        if isinstance(record, dict) and record.get("type") == "error":
            native = record.get("error")
            if isinstance(native, dict):
                data = native.get("data") if isinstance(native.get("data"), dict) else {}
                message = str(data.get("message") or native.get("message") or native.get("name") or "native error")
                # Vendor text sometimes arrives as UTF-8 decoded as Latin-1.
                try:
                    message = message.encode("latin1").decode("utf8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
                # Do not propagate headers, request URLs, or raw response bodies.
                for name, value in os.environ.items():
                    if value and len(value) >= 8 and any(k in name.upper() for k in ("KEY", "TOKEN", "SECRET", "APPID")):
                        message = message.replace(value, "[REDACTED]")
                errors.append(message[:1000])
                saw_stream_message = True
                continue
        # OpenCode stream-json: assistant 每段文本是一条 type=text 记录(part.text)·聚合而非倒序取一。
        if isinstance(record, dict) and str(record.get("type") or "").lower() == "text":
            _part = record.get("part") or {}
            _pt = str(_part.get("text") or "").strip()
            if _pt:
                text_parts.append(_pt)
                saw_stream_message = True
            continue
        if not isinstance(record, dict) or str(record.get("type") or "").lower() != "message":
            continue
        message = record.get("message")
        if not isinstance(message, dict) or str(message.get("role") or "").lower() != "assistant":
            continue
        saw_stream_message = True
        metadata = message.get("metadata") or {}
        if isinstance(metadata, dict) and metadata.get("outputTokenLimitReached"):
            errors.append("native output token limit reached")
        content = message.get("content")
        if isinstance(content, str):
            text_parts.append(content)
            continue
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "").lower()
            if kind in ("text", "output_text") and item.get("text") is not None:
                text_parts.append(str(item["text"]))
            elif kind == "error" and item.get("message"):
                errors.append(str(item["message"]))
    if not saw_stream_message:
        return "", ""
    if not text_parts and not errors:
        errors.append("native stream ended without assistant text")
    return "".join(text_parts).strip(), "\n".join(errors).strip()


def _event_from_json(value: Any, protocol: str) -> dict | None:
    if not isinstance(value, dict):
        return None
    # A CLI wrapper can emit the already-projected event from its inner transport.
    # Preserve that event instead of turning its `t` into a generic "status".
    if (isinstance(value.get("t"), str) and value["t"] in {"tool_started", "tool_end", "tool_timeout"}
            and isinstance(value.get("tool"), str) and value["tool"].strip()):
        event = {key: value[key] for key in (
            "t", "protocol", "tool", "call_id", "status", "ok", "ms",
            "selection", "source_ref", "args_brief", "result_brief") if key in value}
        for key in ("args_brief", "result_brief"):
            if key in event and event[key] is not None:
                event[key] = str(event[key])[:500]
        return event
    kind = str(value.get("type") or value.get("event") or value.get("method") or "status")
    # Native AgentEvent (Prime and other users of the same protocol). Only start
    # and terminal execution events count; message/turn copies are not new calls.
    if (kind in {"tool_execution_start", "tool_execution_end"}
            and isinstance(value.get("toolName"), str) and value["toolName"].strip()):
        started = kind == "tool_execution_start"
        event = {"t": "tool_started" if started else "tool_end",
                 "protocol": protocol, "tool": value["toolName"],
                 "selection": {"source": "external_harness", "candidate_verified": "unreported"},
                 "source_ref": {"kind": "native_tool", "protocol": protocol}}
        if isinstance(value.get("toolCallId"), str) and value["toolCallId"]:
            event["call_id"] = value["toolCallId"]
        if started:
            args = value.get("args")
            event.update(status="started", args_brief=(
                args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
                if args is not None else "")[:500])
        else:
            error = value.get("isError")
            ok = not error if isinstance(error, bool) else None
            event.update(status="succeeded" if ok is True else "failed" if ok is False else "completed",
                         ok=ok, result_brief=_final_from_json(value.get("result"))[:500])
        return event
    item = value.get("item") if isinstance(value.get("item"), dict) else {}
    item_kind = str(item.get("type") or item.get("item_type") or "")
    native_tool = item_kind in {"command_execution", "mcp_tool_call", "web_search"}
    if native_tool:
        tool = str(item.get("tool") or item.get("name") or item_kind)
        call_id = str(item.get("id") or item.get("call_id") or f"{protocol}:{tool}")
        if kind == "item.started":
            return {"t": "tool_started", "protocol": protocol, "tool": tool,
                    "call_id": call_id, "status": "started",
                    "selection": {"source": "external_harness", "candidate_verified": "unreported"},
                    "source_ref": {"kind": "native_tool", "protocol": protocol},
                    "args_brief": str(item.get("command") or item.get("query") or "")[:500]}
        if kind == "item.completed":
            exit_code = item.get("exit_code")
            ok = exit_code in (None, 0) and not item.get("error")
            return {"t": "tool_end", "protocol": protocol, "tool": tool,
                    "call_id": call_id, "status": "succeeded" if ok else "failed", "ok": ok,
                    "selection": {"source": "external_harness", "candidate_verified": "unreported"},
                    "source_ref": {"kind": "native_tool", "protocol": protocol},
                    "result_brief": str(item.get("aggregated_output") or item.get("result") or item.get("error") or "")[:500]}
    event = {"t": "harness_event", "protocol": protocol, "kind": kind,
             "status": value.get("status"), "url": value.get("url") or value.get("uri")}
    if kind == "task_started" and isinstance(value.get("taskId"), str) and value["taskId"]:
        event["native_session_id"] = value["taskId"]
    text = _final_from_json(value)
    if text:
        event["summary"] = text[:1000]
    return {key: part for key, part in event.items() if part not in (None, "")}


def _extract_contract(payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {}
    result = {}
    session = payload.get("session_id") or payload.get("conversation_id") or payload.get("thread_id")
    native_checkpoint = payload.get("native_checkpoint")
    if isinstance(native_checkpoint, dict) and native_checkpoint.get("session_id"):
        # A native worker owns its restore schema (config/home/runtime identity).
        # Do not collapse it to just an ID and silently lose the next-turn inputs.
        result["native_checkpoint"] = dict(native_checkpoint)
        result["checkpoint"] = dict(native_checkpoint)
        result["native_session_id"] = str(native_checkpoint["session_id"])
    if session:
        # 统一对齐 core adapter 的持久恢复契约。旧字段保留兼容现有调用；run_one 只认
        # native_checkpoint 并写入 task_memory，下一轮 external registry 才能真正拿它 resume。
        if not result.get("native_checkpoint"):
            checkpoint = {"session_id": str(session)}
            result["native_session_id"] = str(session)
            result["checkpoint"] = checkpoint
            result["native_checkpoint"] = checkpoint
    if isinstance(payload.get("resumed_native"), bool):
        result["resumed_native"] = payload["resumed_native"]
    if isinstance(payload.get("model_used"), str):
        result["model_used"] = payload["model_used"]
    if isinstance(payload.get("native_facts"), dict):
        result["native_facts"] = dict(payload["native_facts"])
    if isinstance(payload.get("error_envelope"), dict):
        # The existing core normalizer validates this runtime fact. Preserve it
        # rather than forcing the caller to guess a category from "rc1".
        result["error_envelope"] = dict(payload["error_envelope"])
    for key in ("usage", "artifacts", "orchestration_request"):
        if payload.get(key) is not None:
            result[key] = payload[key]
    return result


def _usage_records(records: list[Any]) -> list[dict]:
    """提取外部 CLI/SSE 原生 usage；只认结构化字段，不按文本长度估算。"""
    rows = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        candidates = [record.get("usage"), record.get("token_usage")]
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        candidates.extend((message.get("usage"), message.get("usage_metadata")))
        for usage in candidates:
            if isinstance(usage, dict):
                row = dict(usage)
                row.setdefault("source", "external_native_event")
                row.setdefault("model", record.get("model") or message.get("model"))
                rows.append(row)
    return rows


def _managed_cli_app():
    """The installed-layout decision shared by preparation and execution."""
    service = pathlib.Path(__file__).resolve().parent
    app = service.parent.parent
    managed = sys.platform.startswith("linux") and (
        (service.name == "legion-service" and service.parent.name == "legion")
        or any((app / name).exists() for name in ("image-payload.json", "runtime-ref.json")))
    return app if managed else None


@contextmanager
def _cli_process_boundary(command, context, env):
    """Confine installed Linux CLI descendants without changing their protocol."""
    app = _managed_cli_app()
    if app is None:
        yield None
        return

    from native_process_sandbox import prepare_native_process, NativeProcessSandboxError

    # Bind to the installed generation, not a task-selected binary or PATH.
    runtime = (app / "runtime").resolve(strict=True)
    declared = env.get("MEYO_RUNTIME_ROOT")
    if declared and (
            not pathlib.Path(declared).is_absolute()
            or pathlib.Path(declared).resolve() != runtime):
        raise NativeProcessSandboxError("CLI runtime differs from the installed program")
    effective = {**env, "MEYO_RUNTIME_ROOT": str(runtime)}
    protected = [str(app), str(runtime)]
    protected.extend(context[name] for name in ("source", "runtime_dir") if context.get(name))
    # Session files below cwd need no extra grant. Add only explicit memory/IPC
    # and adapter-prepared state roots, never HOME, shared data or runtime.
    writable = [context[name] for name in ("memory_dir", "session_socket_dir") if context.get(name)]
    native_state = context.get("native_writable_state_roots") or []
    if not isinstance(native_state, (list, tuple)):
        raise NativeProcessSandboxError("prepared native state roots must be a sequence")
    writable.extend(native_state)
    if context.get("native_adapter_key") == "opencode":
        from external_adapters.opencode_state import prepare_native_state
        try:
            writable.extend(prepare_native_state(
                effective.get("OPENCODE_CONFIG") or "", context["workdir"], effective,
                context.get("user_id") or "default"))
        except (OSError, ValueError) as exc:
            raise NativeProcessSandboxError(f"OpenCode native state preparation failed: {exc}") from exc
    data = pathlib.Path(env.get("MEYO_LEGION_DIR") or
                        pathlib.Path(env["HOME"]) / ".meyo/legion").resolve()
    shared = [data]
    if env.get("LEGION_HOME_DIR"):
        shared.append(pathlib.Path(env["LEGION_HOME_DIR"]).resolve())
    if any(root.is_relative_to(pathlib.Path(path).resolve())
           for root in shared for path in [context["workdir"], *writable]):
        raise NativeProcessSandboxError("CLI workspace/state must not open a shared data directory")
    with prepare_native_process(
            effective, context["workdir"], backend="codex",
            protected_roots=list(dict.fromkeys(protected)),
            writable_state_roots=list(dict.fromkeys(writable))) as launch:
        yield launch


def run_cli(spec: dict, context: dict[str, str], env: dict[str, str], timeout_s: int,
            event_cb: Callable[[dict], None] | None = None) -> dict:
    command = _format(spec.get("command") or [], context)
    if not command:
        raise TransportError("CLI adapter 缺 command")
    started = time.time()
    with _cli_process_boundary(command, context, env) as launch:
        result = _supervise_cli_process(command, spec, context, env, timeout_s,
                                        event_cb=event_cb, launch=launch)
        result["elapsed"] = round(time.time() - started, 2)
        return result


def _supervise_cli_process(command, spec, context, env, timeout_s, event_cb=None, *,
                           launch=None, report_command=None, termination_grace_s=0.5):
    """Existing CLI lifecycle, with protection owned by the trusted caller.

    This private function never interprets a task/catalog/environment bypass
    flag and never creates a boundary. Public run_cli always prepares its own;
    the fixed Cline bootstrap owns the launch passed by its native entry.
    """
    launch_command = launch.wrap(command) if launch is not None else command
    launch_env = launch.env if launch is not None else env
    if report_command is not None:
        command = report_command
    t0 = time.time()
    file_output = (isinstance(spec.get("output_contract"), dict)
                   and "source_glob" in spec["output_contract"])
    before_output = _native_output_snapshot(spec, context["workdir"]) if file_output else {}
    # stdin=DEVNULL:非交互 -p/--print 模式的 CLI(pi 等)若继承父进程 stdin(非 TTY 管道),
    # 会一直等 stdin 读不到→整进程卡死到超时(速度底线·pi 血泪:message 在参数里但它先等 stdin)。
    # 军团派活永远是一次性单发,不喂 stdin,统一关掉 stdin·让 CLI 直接用 argv 里的 task 跑。
    # start_new_session=True:给 worker 建独立进程组(子进程泄漏系统性根治·用户 2026-09-01 定)。
    # external worker 常自己 fork 孙进程(ironclaw onboard/set-provider/run 三进程·pi bridge 等)——
    # 超时/卡死只 kill 直接子进程会让孙进程变孤儿(争 CPU/占端口/内存泄漏·越用越慢越不稳)。
    # 建进程组后超时用 os.killpg 整组杀·一个不留。
    _popen_kw = {}
    if os.name != "nt":
        _popen_kw["start_new_session"] = True
    proc = None
    _preg = None

    def _kill_group():
        """整进程组回收(SIGTERM 宽限→SIGKILL)·不留孤儿孙进程。"""
        if proc is None:
            return
        try:
            if os.name != "nt":
                pgid = os.getpgid(proc.pid)
                try:
                    os.killpg(pgid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=termination_grace_s)
                    except subprocess.TimeoutExpired:
                        pass
                except Exception:
                    pass
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except Exception:
                    pass
            else:
                proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        # Let the wrapper unwind/release native scope locks first. Its root may
        # already be gone, but a separately registered child can still be alive.
        # Complete the existing owner-scoped/native cleanup before a retry.
        if _preg is not None:
            _preg.kill_owned_by(proc.pid)

    try:
        try:
            proc = subprocess.Popen(launch_command, cwd=context["workdir"], env=launch_env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True, bufsize=1,
                                    **_popen_kw)
        except OSError as exc:
            _mark_process_start_failure(exc)
            raise
        # Registration itself may be interrupted by an ordinary termination
        # signal. Keep it inside this same owning finally, not before it.
        try:
            import legion_proc_registry as _preg
            _preg.register(proc.pid, pgid=proc.pid, owner=os.getpid(),
                           comm=os.path.basename(str(launch_command[0])), meta={"kind": "cli-run"})
        except Exception:
            pass
        # 读线程 + 主线程看总墙钟:防 `for line in proc.stdout` 在 worker 卡住不输出也不退时永久阻塞
        # (旧 bug:timeout_s 只在 wait 生效·for 卡住根本到不了 wait→卡死不超时·还留孤儿)。
        records, output = [], []
        error_output = []
        _read_done = threading.Event()

        def _stderr_reader():
            try:
                if proc.stderr is not None:
                    while chunk := proc.stderr.read(8192):
                        error_output.append(chunk)
            except (OSError, ValueError):
                pass

        def _reader():
            last_activity = 0.0
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    output.append(line)
                    if file_output:
                        # TUI/调试流只作私有证据；真实有输出时报告活动，不公开其内容。
                        now = time.monotonic()
                        if event_cb and now - last_activity >= 1:
                            last_activity = now
                            try:
                                event_cb({"t": "harness_event", "protocol": "cli", "kind": "activity",
                                          "summary": "原生进程有新输出，终端日志私有保存"})
                            except Exception:
                                pass  # 展示回调失败不能停止排空管道或丢失原生终态。
                        continue
                    try:
                        record = json.loads(line)
                        records.append(record)
                        event = _event_from_json(record, "cli")
                        if event_cb and event:
                            event_cb(event)
                    except Exception:
                        if event_cb and line.strip():
                            event_cb({"t": "harness_event", "protocol": "cli", "kind": "output",
                                      "summary": line.strip()[:1000]})
            except Exception:
                pass
            finally:
                _read_done.set()

        _rt = threading.Thread(target=_reader, daemon=True)
        _et = threading.Thread(target=_stderr_reader, daemon=True)
        _et.start()
        _rt.start()
        _deadline = timeout_s or 0
        if not _read_done.wait(timeout=_deadline if _deadline > 0 else None):
            # 到墙钟仍没读完(worker 卡死/不退)→整组杀·抛超时(上层 run() 记为该 transport 失败·换模型/补位)
            _kill_group()
            _rt.join(timeout=2)
            _et.join(timeout=2)
            error = subprocess.TimeoutExpired(command, _deadline)
            checkpoint_event = spec.get("native_checkpoint_event_type")
            if isinstance(checkpoint_event, str) and checkpoint_event:
                received = next((row for row in reversed(records)
                                 if isinstance(row, dict) and row.get("type") == checkpoint_event
                                 and isinstance(row.get("native_checkpoint"), dict)
                                 and row["native_checkpoint"].get("session_id")), None)
                if received is not None:
                    error.native_checkpoint = dict(received["native_checkpoint"])
                    if isinstance(received.get("resumed_native"), bool):
                        error.resumed_native = received["resumed_native"]
            if file_output or spec.get("snapshot_terminal"):
                error.native_facts = {"terminal_log": _snapshot_terminal(
                    "".join(output), context["workdir"], "".join(error_output))}
            raise error
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_group()
        _et.join(timeout=2)
        stderr = "".join(error_output)
        raw_stdout = "".join(output)
        stdout = raw_stdout.strip()
        extra = {}
        if file_output or spec.get("snapshot_terminal"):
            snapshot = _snapshot_terminal(raw_stdout, context["workdir"], stderr)
            if snapshot:
                extra["terminal_snapshot"] = snapshot
        native_output = (_fresh_file_output(spec, context["workdir"], before_output) if file_output
                         else _declared_json_output(stdout, records, spec))
        final = native_output["final"] if native_output is not None else _final_from_text(stdout, spec)
        stream_error = ""
        _answer_rec = None
        # Goose stream-json 每个 assistant token 是一条 type=message 记录，必须按协议聚合；
        # 倒序取第一条只会拿到最后一个字/标点。结构化 error 单独回传为失败，不冒充交付。
        _stream_final, stream_error = ("", "") if native_output is not None else _stream_records_result(records)
        if native_output is not None:
            _answer_rec = native_output["record"]
        elif _stream_final:
            final = _stream_final
            # 合同元数据可能在较早的 JSON 记录里；不要把最后一条日志/banner 当权威记录。
            for _record in reversed(records):
                if isinstance(_record, dict) and (_extract_contract(_record) or _final_from_json(_record)):
                    _answer_rec = _record
                    break
        elif stream_error:
            final = ""
        else:
            # stdout 整体是【单块 JSON】(OpenClaw pretty JSON)时始终优先整体解析。pretty JSON 的某些
            # 单独行恰好也是合法 JSON 字符串(如 "exec")，records 因而非空；旧版受 `not records`
            # 限制跳过整块解析，最终把该工具名错当答案。
            if stdout.startswith("{"):
                try:
                    _whole = json.loads(stdout)
                    _cand = _final_from_json(_whole)
                    if _cand and _cand.strip():
                        final = _cand
                        _answer_rec = _whole
                except Exception:
                    pass
        if records and _answer_rec is None and not stream_error:
            # 通用取答案(底线:external家final别取错行):很多 CLI(kimi-code等)最后一条是
            # meta/session.resume_hint(带 content="To resume..."),无脑取 records[-1] 会把
            # 提示行当答案交付。从后往前找第一条【真答案记录】——跳过 meta/system/resume/hint 类。
            def _is_meta(r):
                if not isinstance(r, dict):
                    return False
                role = str(r.get("role") or "").lower()
                typ = str(r.get("type") or "").lower()
                blob = role + " " + typ
                return any(w in blob for w in ("meta", "system", "resume", "hint", "usage", "version", "init"))
            for r in reversed(records):
                if _is_meta(r):
                    continue
                cand = _final_from_json(r)
                if cand and cand.strip():
                    final = cand
                    _answer_rec = r
                    break
            if _answer_rec is None:
                # 纯文本 CLI(aider 等)答案行可能是裸数字/单词——json.loads 成功但 _final_from_json 对
                # 非 str/dict/list 一律返回空。此时绝不能用 raw stdout(含 banner)覆盖第 1 步
                # _final_from_text 已提取的正确答案;只在它也空时才退回 stdout。
                _cand = _final_from_json(records[-1]) or _final_from_json(records)
                if _cand and _cand.strip():
                    final = _cand
                elif not final.strip():
                    final = stdout
        if stream_error and not final.strip():
            final = ""
        error = stream_error or stderr[-4000:]
        contract = _extract_contract(_answer_rec if _answer_rec is not None else (records[-1] if records else {}))
        if native_output is None:
            # A legacy plain answer can itself be a JSON document. Its business
            # fields are not declarations about the native process/runtime.
            contract.pop("error_envelope", None)
        if file_output:
            # 子 CLI 的 session_id 仅作引用，不能据此替外层 Harness 宣布原生 resume。
            contract = {key: value for key, value in contract.items()
                        if key in ("usage", "artifacts", "orchestration_request")}
            extra["native_facts"] = {
                "process_returncode": proc.returncode,
                **{key: native_output[key] for key in
                   ("native_status", "native_success", "native_is_error", "output_ref")
                   if key in native_output},
                **({"terminal_log": extra["terminal_snapshot"]} if "terminal_snapshot" in extra else {}),
            }
            error = (f"native_process_exit:{proc.returncode}" if proc.returncode != 0 else
                     "native_output_reported_failure" if native_output["output_state"] == "error" else
                     "native_output_missing_or_incomplete" if native_output["output_state"] != "completed" else "")
        if (native_output is not None
                and (proc.returncode != 0 or native_output["output_state"] != "completed")
                and native_output.get("native_error_message")):
            diagnostic = native_output["native_error_message"]
            remaining = max(0, 4000 - len(diagnostic) - 1)
            extra_stderr = ("\n" + error[-remaining:]
                            if error and error not in diagnostic and remaining else "")
            error = diagnostic + extra_stderr
        # 旧式 notes 提取仅保留兼容。声明了机器文件出口后，即使本次没有终态，
        # native_output 也非 None：不得回退到旧 notes、TUI 或调试文本。
        if spec.get("final_notes_regex") and native_output is None:
            # 带 final_notes_regex 声明的家=【stdout 没有可判定的答案行·产物回读为权威】
            # (gnhf 统计面板/操作提示行都会被 _final_from_text 误当答案·漏网一个 pattern 就前功
            # 尽弃)。所以:回读成功就接管 final;回读失败才保留 stdout 提取结果(兜底·不让这单
            # 变空)。这仍是"取回它自己写的答案"·不是替它答题。
            _read = _read_final_notes(spec.get("final_notes_regex"), context.get("workdir") or ".")
            if _read:
                final = _read
        status = ("ok" if proc.returncode == 0 and final and not stream_error
                  else "error" if stream_error else f"rc{proc.returncode}")
        if file_output and proc.returncode == 0:
            status = "ok" if native_output["output_state"] == "completed" else native_output["output_state"]
        return {"status": status, "returncode": proc.returncode,
                "final": final if file_output else final[-60000:], "error": error[-4000:],
                "elapsed": round(time.time() - t0, 2), "command": command,
                "events": records[-100:],
                "usage_records": _usage_records([_answer_rec] if file_output else records),
                "protocol": "cli", **contract,
                **{key: native_output[key] for key in ("native_status", "native_session_id")
                   if native_output is not None and key in native_output},
                **extra}
    finally:
        # A finished native CLI must not leave a live-looking registration. Do
        # not discard evidence for a process that could not be confirmed dead.
        if proc is not None and proc.poll() is None:
            _kill_group()
        if proc is not None and _preg is not None and proc.poll() is not None:
            _preg.unregister(proc.pid)


def _consume_sse(url: str, timeout_s: int, event_cb: Callable[[dict], None] | None) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
    records = []
    final = ""
    with urllib.request.urlopen(request, timeout=timeout_s or 900) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                record = json.loads(line[5:].strip())
            except Exception:
                continue
            records.append(record)
            event = _event_from_json(record, "http-sse")
            if event_cb and event:
                event_cb(event)
            kind = str(record.get("type") or "")
            candidate = _final_from_json(record)
            if candidate and kind in ("done", "message", "text", "final"):
                final = candidate
            if kind in ("stream_end", "error"):
                break
    return {"final": final, "events": records[-100:],
            "usage_records": _usage_records(records)}


def run_http(spec: dict, context: dict[str, str], env: dict[str, str], timeout_s: int,
             event_cb: Callable[[dict], None] | None = None) -> dict:
    url = _format(spec.get("url") or "", context)
    if not url:
        raise TransportError("HTTP adapter 缺 url")
    body = _format(spec.get("body") or {"message": "{task}"}, context)
    headers = {"Content-Type": "application/json", **_format(spec.get("headers") or {}, context)}
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers,
                                 method=str(spec.get("method") or "POST").upper())
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s or 900) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status_code = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return {"status": f"http{exc.code}", "final": "", "error": raw[-4000:],
                "elapsed": round(time.time() - t0, 2), "protocol": "http"}
    contract = {}
    try:
        payload = json.loads(raw)
        final = _final_from_json(payload) or raw
        contract = _extract_contract(payload)
        event = _event_from_json(payload, "http")
        if event_cb and event:
            event_cb(event)
        request_id = payload.get("request_id") if isinstance(payload, dict) else None
        if request_id and spec.get("stream_url"):
            stream_url = _format(spec["stream_url"], {**context, "request_id": request_id})
            streamed = _consume_sse(stream_url, timeout_s, event_cb)
            final = streamed.get("final") or final
            contract["events"] = streamed.get("events") or []
            contract["usage_records"] = streamed.get("usage_records") or []
    except Exception:
        final = raw
    return {"status": "ok" if 200 <= status_code < 300 and final.strip() else f"http{status_code}",
            "final": final.strip()[-60000:], "error": "", "http_status": status_code,
            "elapsed": round(time.time() - t0, 2), "protocol": "http",
            "usage_records": _usage_records([payload] if isinstance(locals().get("payload"), dict) else []),
            **contract}


def run_jsonrpc(spec: dict, context: dict[str, str], env: dict[str, str], timeout_s: int,
                protocol: str, event_cb: Callable[[dict], None] | None = None) -> dict:
    command = _format(spec.get("command") or [], context)
    if not command:
        raise TransportError(f"{protocol.upper()} adapter 缺 command")
    request = _format(spec.get("request") or {}, context)
    if not request:
        request = {"jsonrpc": "2.0", "id": 1, "method": "prompt",
                   "params": {"prompt": context["task"]}}
    t0 = time.time()
    # 进程组回收(子进程泄漏系统性根治):sdk worker(deer-flow 等)可能 fork 孙进程·subprocess.run
    # 超时只杀直接子进程留孤儿。改 Popen+start_new_session 建进程组·超时 os.killpg 整组杀。
    _popen_kw = {"start_new_session": True} if os.name != "nt" else {}
    try:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=context["workdir"], env=env, text=True, **_popen_kw)
    except OSError as exc:
        _mark_process_start_failure(exc)
        raise
    # 崩溃安全孤儿回收(2026-09-02 根修·与 run_cli 对称):sdk worker 自立进程组同样登记。
    try:
        import legion_proc_registry as _preg
        _preg.register(proc.pid, pgid=proc.pid, owner=os.getpid(),
                       comm=os.path.basename(str(command[0])), meta={"kind": "sdk-run"})
    except Exception:
        pass
    try:
        out, err = proc.communicate(input=json.dumps(request) + "\n", timeout=timeout_s or None)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        try:
            if os.name != "nt":
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM); time.sleep(0.5)
                os.killpg(pgid, signal.SIGKILL)
            else:
                proc.kill()
        except Exception:
            pass
        try: proc.wait(timeout=5)
        except Exception: pass
        raise
    payloads = []
    for line in (out or "").splitlines():
        try:
            record = json.loads(line)
            payloads.append(record)
            event = _event_from_json(record, protocol)
            if event_cb and event:
                event_cb(event)
        except Exception:
            continue
    payload = payloads[-1] if payloads else {}
    final = _final_from_json(payload)
    return {"status": "ok" if rc == 0 and final else f"rc{rc}",
            "final": final[-60000:], "error": (err or "")[-4000:],
            "events": payloads[-100:], "usage_records": _usage_records(payloads),
            "elapsed": round(time.time() - t0, 2),
            "command": command, "protocol": protocol, **_extract_contract(payload)}


def run_sdk(spec: dict, context: dict[str, str], env: dict[str, str], timeout_s: int,
            event_cb: Callable[[dict], None] | None = None) -> dict:
    worker = spec.get("worker") or spec.get("command")
    return run_jsonrpc({"command": worker, "request": spec.get("request") or {
        "jsonrpc": "2.0", "id": 1, "method": "run", "params": context}},
        context, env, timeout_s, "sdk", event_cb)


def run_transport(spec: dict, context: dict[str, str], env: dict[str, str], timeout_s: int,
                  event_cb: Callable[[dict], None] | None = None) -> dict:
    kind = str(spec.get("type") or "cli").lower()
    if kind == "cli":
        return run_cli(spec, context, env, timeout_s, event_cb)
    if kind == "http":
        return run_http(spec, context, env, timeout_s, event_cb)
    if kind in ("acp", "mcp"):
        return run_jsonrpc(spec, context, env, timeout_s, kind, event_cb)
    if kind == "sdk":
        return run_sdk(spec, context, env, timeout_s, event_cb)
    raise TransportError(f"不支持的 adapter 协议:{kind}")
