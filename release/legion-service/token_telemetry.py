#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一 Token/模型/耗时遥测；只消费原生 usage 字段，缺失时明确 unavailable。"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any


_ALIASES = {
    "input_tokens": ("input_tokens", "uncached_input_tokens", "prompt_tokens", "inputTokens", "promptTokens", "input"),
    "output_tokens": ("output_tokens", "completion_tokens", "outputTokens", "completionTokens", "output"),
    "reasoning_output_tokens": ("reasoning_output_tokens", "reasoning_tokens", "reasoningTokens"),
    "cache_read_tokens": ("cache_read_input_tokens", "cache_read_tokens", "cached_input_tokens",
                          "cached_tokens", "cacheReadInputTokens", "cacheReadTokens", "cacheRead"),
    "cache_write_tokens": ("cache_creation_input_tokens", "cache_write_input_tokens",
                           "cache_write_tokens", "cacheCreationInputTokens", "cacheWriteTokens", "cacheWrite"),
    "total_tokens": ("total_tokens", "totalTokens", "total"),
}

_COST_ALIASES = ("cost_usd", "total_cost_usd", "estimated_cost_usd")
_DURATION_ALIASES = ("duration_api_ms", "api_duration_ms", "provider_ms")
_TURN_ALIASES = ("num_turns", "turns", "api_calls")


def _integer(value: Any) -> int | None:
    try:
        return max(0, int(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _find(mapping: dict, aliases: tuple[str, ...]) -> int | None:
    for key in aliases:
        if key in mapping:
            value = _integer(mapping.get(key))
            if value is not None:
                return value
    return None


def _number(mapping: dict, aliases: tuple[str, ...]) -> float | None:
    for key in aliases:
        try:
            if mapping.get(key) is not None:
                return max(0.0, float(mapping[key]))
        except (TypeError, ValueError):
            continue
    return None


def normalize(value: Any, *, model: str = "", elapsed: float | None = None,
              source: str = "native") -> dict:
    """递归查找常见 SDK/CLI usage 结构；没有原生数字就不造假。"""
    roots = []
    if isinstance(value, dict):
        roots.append(value)
        for key in ("usage", "token_usage", "metrics", "response", "result", "meta"):
            nested = value.get(key)
            if isinstance(nested, dict):
                roots.append(nested)
    found = {name: None for name in _ALIASES}
    usage_model = str(model or "")
    cost_usd = duration_api_ms = None
    num_turns = None
    granularity = "request"
    native_source = source
    for root in roots:
        usage_model = usage_model or str(root.get("model") or root.get("model_used") or "")
        native_source = str(root.get("source") or native_source)
        granularity = str(root.get("granularity") or granularity)
        if root.get("granularity") == "thread_cumulative":
            continue  # A thread counter is not this invocation's consumption.
        for name, aliases in _ALIASES.items():
            if found[name] is None:
                found[name] = _find(root, aliases)
        details = root.get("input_tokens_details") or root.get("prompt_tokens_details") or {}
        if isinstance(details, dict) and found["cache_read_tokens"] is None:
            found["cache_read_tokens"] = _find(details, ("cached_tokens", "cache_read_tokens"))
        # Penguin 的 input 是 cache_read + cache_write，协议不提供独立 uncached input。
        if root.get("input_semantics") == "cache_buckets" and found["input_tokens"] is not None:
            found["input_tokens"] = max(0, int(found["input_tokens"])
                                         - int(found["cache_read_tokens"] or 0)
                                         - int(found["cache_write_tokens"] or 0))
        cost_usd = cost_usd if cost_usd is not None else _number(root, _COST_ALIASES)
        duration_api_ms = duration_api_ms if duration_api_ms is not None else _number(root, _DURATION_ALIASES)
        num_turns = num_turns if num_turns is not None else _find(root, _TURN_ALIASES)
    if found["total_tokens"] is None and (found["input_tokens"] is not None or found["output_tokens"] is not None):
        found["total_tokens"] = int(found["input_tokens"] or 0) + int(found["output_tokens"] or 0)
    available = any(item is not None for item in found.values())
    complete = bool(available and found["input_tokens"] is not None and found["output_tokens"] is not None)
    if any(root.get("complete") is False or root.get("coverage") in ("partial", "unavailable")
           or (_integer(root.get("attempts_unreported")) or 0) > 0 for root in roots):
        complete = False
    return {"available": available, "source": native_source if available else "unavailable",
            "complete": complete, "coverage": "complete" if complete else "partial" if available else "unavailable",
            "granularity": granularity, "model": usage_model, "elapsed_s": elapsed,
            "cost_usd": cost_usd, "duration_api_ms": duration_api_ms,
            "num_turns": num_turns, **found}


def summary_text(usage: dict | None) -> str:
    if not isinstance(usage, dict):
        return "该 Harness 未提供原生 Token 数据"
    parts = []
    if usage.get("model"):
        parts.append(f"模型 {usage['model']}")
    if usage.get("available"):
        parts.extend([f"输入 {int(usage['input_tokens']):,}" if usage.get("input_tokens") is not None else "输入 未上报",
                      f"输出 {int(usage['output_tokens']):,}" if usage.get("output_tokens") is not None else "输出 未上报"])
    else:
        parts.append("仅有原线程累计值，本次用量未知"
                     if usage.get("granularity") == "thread_cumulative" else "Token 未上报")
    if usage.get("cache_read_tokens"):
        parts.append(f"缓存命中 {int(usage['cache_read_tokens']):,}")
    if usage.get("cache_write_tokens"):
        parts.append(f"缓存写入 {int(usage['cache_write_tokens']):,}")
    if usage.get("reasoning_output_tokens"):
        parts.append(f"推理 {int(usage['reasoning_output_tokens']):,}")
    if usage.get("available"):
        label = "已知部分" if usage.get("complete") is False or usage.get("coverage") == "partial" else "合计"
        if usage.get("total_tokens") is not None:
            parts.append(f"{label} {int(usage['total_tokens']):,}")
        if usage.get("attempts_unreported"):
            parts.append(f"{usage['attempts_unreported']} 次执行用量未上报")
    if usage.get("duration_api_ms") is not None:
        parts.append(f"API {float(usage['duration_api_ms']) / 1000:.2f}s")
    if usage.get("elapsed_s") is not None:
        parts.append(f"总耗时 {float(usage['elapsed_s']):.2f}s")
    if usage.get("cost_usd") is not None:
        parts.append(f"${float(usage['cost_usd']):.4f}")
    return " · ".join(parts)


def attach(result: dict, *, source: str = "native") -> dict:
    target = result if isinstance(result, dict) else {}
    model = str(target.get("model_used") or target.get("model") or "")
    records = target.get("usage_records") if isinstance(target.get("usage_records"), list) else []
    existing = target.get("usage") if isinstance(target.get("usage"), dict) else {}
    if existing:
        telemetry = normalize(existing, model=model, elapsed=target.get("elapsed"), source=source)
        telemetry = {**existing, **telemetry}
    elif records:
        telemetry = aggregate(records, model=model, elapsed=target.get("elapsed"))
    else:
        telemetry = normalize(target, model=model, elapsed=target.get("elapsed"), source=source)
    target["usage"] = telemetry
    return target


def aggregate(usages: list[dict], *, model: str = "", elapsed: float | None = None) -> dict:
    """合并同一任务的多次模型尝试；失败重试消耗也必须计入，不只展示成功末轮。"""
    rows = []
    identities = set()
    for row in usages or []:
        row = row if isinstance(row, dict) else {}
        identity = row.get("usage_identity")
        if isinstance(identity, str) and identity:
            if identity in identities:
                continue
            identities.add(identity)
        normalized = normalize(row, model=str(row.get("model") or model),
                               elapsed=row.get("elapsed_s"), source=str(row.get("source") or "native"))
        rows.append({**row, **normalized})
    available = [row for row in rows if row.get("available")]
    totals = {name: (sum(int(row[name]) for row in available if row.get(name) is not None)
                     if any(row.get(name) is not None for row in available) else None)
              for name in _ALIASES}
    attempts_total = sum(_integer(row.get("attempts_total")) if row.get("attempts_total") is not None else 1
                         for row in rows)
    attempts_reported = sum((_integer(row.get("attempts_reported")) or 0)
                            if row.get("attempts_reported") is not None else int(bool(row.get("available")))
                            for row in rows)
    attempts_unreported = sum((_integer(row.get("attempts_unreported")) or 0)
                              if row.get("attempts_unreported") is not None else int(not row.get("available"))
                              for row in rows)
    complete = bool(rows and all(row.get("complete") for row in rows) and not attempts_unreported)
    models = list(dict.fromkeys(str(row.get("model") or "") for row in rows if row.get("model")))
    costs = [row.get("cost_usd") for row in rows if row.get("cost_usd") is not None]
    api_durations = [row.get("duration_api_ms") for row in rows if row.get("duration_api_ms") is not None]
    turns = [row.get("num_turns") for row in rows if row.get("num_turns") is not None]
    return {"available": bool(available), "source": "native" if available else "unavailable",
            "complete": complete, "coverage": "complete" if complete else "partial" if available else "unavailable",
            "granularity": "task", "model": model or (models[-1] if models else ""),
            "models": models, "elapsed_s": elapsed, "attempts_reported": attempts_reported,
            "attempts_total": attempts_total, "attempts_unreported": attempts_unreported,
            "cost_usd": sum(float(value) for value in costs) if costs else None,
            "duration_api_ms": sum(float(value) for value in api_durations) if api_durations else None,
            "num_turns": sum(int(value) for value in turns) if turns else None, **totals}


def summarize(results: list[dict]) -> dict:
    rows = []
    totals = {name: 0 for name in _ALIASES}
    available = 0
    costs: list[float] = []
    for result in results or []:
        raw_usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        usage = normalize(raw_usage or result,
                          model=str(result.get("model_used") or result.get("model") or ""),
                          elapsed=result.get("elapsed"))
        usage = {**raw_usage, **usage}
        rows.append({"task_id": result.get("task_id") or result.get("key"),
                     "harness": result.get("assigned_harness") or result.get("harness") or result.get("key"),
                     **usage})
        if usage.get("available"):
            available += 1
            for name in totals:
                totals[name] += int(usage.get(name) or 0)
        if usage.get("cost_usd") is not None:
            costs.append(float(usage["cost_usd"]))
    completeness = aggregate(rows)
    return {"members": rows, "available_members": available,
            "unavailable_members": max(0, len(rows) - available),
            "cost_usd": sum(costs) if costs else None,
            **{name: completeness[name] for name in _ALIASES},
            "complete": completeness["complete"], "coverage": completeness["coverage"],
            "attempts_total": completeness["attempts_total"],
            "attempts_unreported": completeness["attempts_unreported"]}


def _codex_usage_paths(home: str, thread_id: str) -> list[Path]:
    if not home or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id):
        return []
    root = Path(home).resolve()
    return [p for p in (root / "sessions").glob(f"*/*/*/*{thread_id}.jsonl")
            if p.is_file() and not p.is_symlink() and p.resolve().is_relative_to(root)]


def codex_usage_cursor(home: str, thread_id: str) -> dict:
    """Private invocation-local offsets, not a new store or a saved credential."""
    files = {}
    for path in _codex_usage_paths(home, thread_id):
        stat = path.stat()
        files[str(path)] = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_ctime_ns]
    return {"home": home, "thread_id": thread_id, "files": files}


def codex_usage_after(cursor: dict, thread_id: str, counter: dict | None, *,
                      resumed: bool, terminal: str, model: str,
                      completion: dict | None = None) -> dict | None:
    """Read current-call usage and, optionally, its public terminal message.

    A late record from the previous turn cannot become current consumption.
    The same invocation window binds the optional completion fact; missing or
    ambiguous lifecycle evidence stays unknown. Prompts, tool bodies and
    reasoning messages never leave this function.
    """
    if completion is not None:
        completion.clear()
        completion.update(state="unknown", thread_id=thread_id)
    counter = counter if isinstance(counter, dict) else {}
    unknown = {
        "available": False, "complete": False, "coverage": "unavailable",
        "granularity": "thread_cumulative", "usage_scope": "native_increment_unknown",
        "model": model, "native_counter": counter,
        "source": "codex_thread_counter_not_an_invocation_total",
    }
    selected = {}
    starts, ends = set(), set()
    totals = {}
    malformed = False
    public_starts, public_ends = [], []
    lifecycle_valid = not resumed or cursor.get("thread_id") == thread_id
    paths = _codex_usage_paths(cursor.get("home", ""), thread_id)
    if len(paths) == 1:
        path = paths[0]
        saved = (cursor.get("files") or {}).get(str(path))
        stat = path.stat()
        if resumed and (not saved or [stat.st_dev, stat.st_ino] != saved[:2]
                        or stat.st_size < saved[2]):
            return unknown
        offset = saved[2] if saved else 0
        if stat.st_size - offset > 64 * 1024 * 1024:
            return {**unknown, "scope_reason": "native_usage_window_too_large"}
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (stat.st_dev, stat.st_ino):
                return unknown
            stream.seek(offset)
            data = stream.read(stat.st_size - offset)
        if len(data) != stat.st_size - offset:
            return unknown
        # JSONL uses physical LF; U+2028 may legitimately occur in JSON strings.
        for line in data.split(b"\n"):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError):
                malformed = True
                lifecycle_valid = False
                continue
            payload = record.get("payload") if isinstance(record, dict) else None
            if not isinstance(payload, dict):
                continue
            kind = payload.get("type") or record.get("type")
            turn = payload.get("turn_id")
            if record.get("type") == "event_msg":
                if kind == "task_started":
                    public_starts.append(turn)
                if kind in ("task_started", "task_complete") and (
                        kind == "task_started" or turn in starts):
                    lifecycle_valid = lifecycle_valid and (
                        isinstance(turn, str) and bool(turn)
                        and payload.get("thread_id", thread_id) == thread_id
                        and payload.get("root_turn_id", turn) == turn)
                if kind == "task_complete" and turn in starts:
                    public_ends.append(payload)
            if kind == "task_started" and isinstance(turn, str):
                starts.add(turn)
            elif kind == "task_complete" and turn in starts:
                ends.add(turn)
            elif (kind == "token_usage_record" and turn in starts
                  and payload.get("thread_id", thread_id) == thread_id
                  and isinstance(payload.get("response_id"), str)
                  and isinstance(payload.get("usage"), dict)):
                key = (turn, payload["response_id"])
                values = payload["usage"]
                if not all(type(v) is int and v >= 0 for v in values.values()):
                    malformed = True
                    continue
                selected[key] = values
                if isinstance(payload.get("turn_token_usage"), dict):
                    totals[turn] = payload["turn_token_usage"]
    if (completion is not None and lifecycle_valid
            and len(public_starts) == len(public_ends) == len(starts) == 1):
        end = public_ends[0]
        message = end.get("last_agent_message")
        if ("last_agent_message" in end and end.get("turn_id") == public_starts[0]
                and (message is None or isinstance(message, str))):
            completion.update(
                state="absent" if message in (None, "") else "present",
                turn_id=public_starts[0], last_agent_message=message,
                source="codex_task_complete_event")
    if selected:
        if len(starts) != 1:
            return {**unknown, "scope_reason": "ambiguous_native_turn_window"}
        sums = {name: sum(row.get(name, 0) for row in selected.values())
                for name in set().union(*(row.keys() for row in selected.values()))}
        matched = set(totals) == starts and all(
            sum(row.get(field, 0) for (row_turn, _), row in selected.items() if row_turn == turn)
            == values.get(field)
            for turn, values in totals.items() for field in ("input_tokens", "output_tokens"))
        complete = terminal == "completed" and starts <= ends and matched and not malformed
        identity = hashlib.sha256(json.dumps(
            [thread_id, sorted(selected)], separators=(",", ":")).encode()).hexdigest()
        return {**sums, "granularity": "task", "usage_scope": "native_call",
                "source": "codex_per_response_usage_records", "model": model,
                "usage_identity": identity, "response_records": len(selected),
                "complete": complete, "coverage": "complete" if complete else "partial"}
    if resumed:
        return unknown
    if counter:
        # A genuinely new thread has no previous turns to double-count.
        return {**counter, "model": model, "usage_scope": "new_native_thread",
                "granularity": "task", "source": "codex_new_thread_counter",
                **({"complete": False, "coverage": "partial"} if terminal != "completed" else {})}
    return None
