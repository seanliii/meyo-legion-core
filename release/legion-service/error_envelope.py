#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把各 adapter/worker 的失败归一为可机读 ErrorEnvelope。

结构化字段优先；文本识别只兼容尚未升级的第三方 CLI。
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import re

from legion_contracts import ErrorEnvelope

_RETRYABLE = {"gateway", "rate_limit", "provider", "process", "timeout", "tool_timeout", "truncated", "empty", "stalled", "output_limit",
              "model_config", "context_overflow"}
_CONTEXT_ERROR_MARKERS = (
    "context_length_exceeded", "exceeds the context window", "maximum context length",
    "too many tokens", "input is too long", "prompt is too long",
)
# Adapter/runtime setup failures are model-independent. Retrying the same broken
# daemon or missing dependency against every model only repeats identical work.
_NON_RETRYABLE_RUNTIME = "harness_runtime"
_USER = {"authorization_required", "credential_required", "user_cancel"}


def output_limit_from_native(terminal) -> ErrorEnvelope | None:
    """Only trusted native/provider terminal records establish an output limit.

    Not a regex over user answers/tool output. Codex currently loses the upstream
    limit/usage while mapping response.incomplete to turn.failed; keep unknown
    fields unknown rather than borrowing a model's advertised maximum.
    """
    if not isinstance(terminal, dict):
        return None
    kind = terminal.get("type")
    budget, usage = None, None
    if kind == "turn.failed":
        failure = terminal.get("error")
        if not isinstance(failure, dict) or failure.get("message") != (
                "Incomplete response returned, reason: max_output_tokens"):
            return None
    elif kind == "response.incomplete":
        response = terminal.get("response")
        if (not isinstance(response, dict)
                or (response.get("incomplete_details") or {}).get("reason") != "max_output_tokens"):
            return None
        budget = response.get("max_output_tokens")
        budget = budget if type(budget) is int and budget > 0 else None
        raw_usage = response.get("usage")
        if isinstance(raw_usage, dict):
            usage = {key: value for key, value in raw_usage.items()
                     if key in ("input_tokens", "output_tokens", "total_tokens")
                     and type(value) is int and value >= 0}
    else:
        return None
    return ErrorEnvelope(
        category="output_limit", scope="request", retryable=True, code="max_output_tokens",
        message="提供方报告本次输出达到上限；不是输入超窗或进程崩溃，工具是否执行需按原生结果和现场核实。",
        details={"native_source": kind, "reported_output_limit": budget,
                 "reported_usage": usage, "input_context_overflow": False,
                 "tool_execution_status": "inspect_native_results",
                 "replay_unconfirmed_operations": False},
    )


def requires_output_recovery_pause(result) -> bool:
    if not isinstance(result, dict) or result.get("status") in ("done", "ok", "success", "cancelled"):
        return False
    envelope = ErrorEnvelope.from_value(result.get("error_envelope"))
    return bool(envelope and envelope.category == "output_limit" and not envelope.retryable)


def paused_execution_tasks(results: dict, dependencies: dict, policies: dict | None = None, *,
                           pause=None, delivered=None, dependency_check=None) -> set[str]:
    """Shared pause projection; never delete unmet dependencies during recovery."""
    from legion_contracts import dependencies_satisfied, result_is_delivered
    pause = pause or requires_execution_pause
    delivered = delivered or result_is_delivered
    dependency_check = dependency_check or dependencies_satisfied
    paused = {key for key, result in results.items() if pause(result)}
    while True:
        descendants = {key for key, deps in dependencies.items()
                       if not delivered(results.get(key)) and any(dep in paused for dep in deps)
                       and not dependency_check(deps, (policies or {}).get(key, "all_succeeded"), results)[0]}
        if descendants <= paused:
            return paused
        paused.update(descendants)


def is_provider_context_overflow(status: str, error: str = "", *, supplied=None,
                                 tool: str = "") -> bool:
    """Recognize the reported vendor 400/1261 record, not a quoted phrase or API body limit."""
    if str(status or "").lower() in {"ok", "done", "success", "cancelled"}:
        return False
    existing = ErrorEnvelope.from_value(supplied)
    if tool or (existing and (
            existing.tool or existing.scope in {"tool", "user"}
            or existing.category not in {"harness_runtime", "provider", "gateway",
                                         "model_config", "process", "context_overflow"})):
        return False
    text = str(error or (existing.message if existing else "")).strip()
    decoder = json.JSONDecoder()
    # Unwrap only the observed transport envelopes, never search arbitrary
    # tool output / final text for a JSON substring. Raw error text stays intact.
    for _ in range(4):
        prefix = re.match(
            r"^(?:internal:\s*)?Anthropic API error \(400 Bad Request\):\s*"
            r"|^Error from provider\([^()\n]*:\s*400\):\s*", text, re.I)
        if prefix:
            text = text[prefix.end():]
        if not text.startswith("{"):
            return False
        try:
            value, _ = decoder.raw_decode(text)
        except (TypeError, ValueError):
            return False
        if not isinstance(value, dict) or not isinstance(value.get("error"), dict):
            return False
        failure = value["error"]
        extension = value.get("ext")
        source = extension.get("error") if isinstance(extension, dict) else None
        if (str(failure.get("code")) == "1261"
                and str(failure.get("message") or "").strip().lower() == "prompt exceeds max length"):
            return bool(isinstance(source, dict) and source.get("source") == "UPSTREAM_VENDOR"
                        and source.get("stage") == "downstream_http"
                        and str(source.get("upstreamStatus")) == "400")
        if (failure.get("code") != "provider_response_error"
                or failure.get("type") != "api_error"
                or not isinstance(failure.get("message"), str)):
            return False
        text = failure["message"].strip()
    return False


def requires_provider_backoff(result) -> bool:
    """当前失败需要等待提供方，不等于永久不可重试或任务已经完成。

    只消费执行层已声明的类别和作用域；不从答案、工具输出或历史错误文本猜限流。
    调用方据此停止本请求的立即重放，仍保留原 envelope、产物和原生恢复引用。
    """
    if not isinstance(result, dict) or result.get("status") in ("done", "cancelled"):
        return False
    envelope = ErrorEnvelope.from_value(result.get("error_envelope"))
    return bool(envelope and envelope.category == "rate_limit"
                and envelope.scope == "provider")


def provider_authorization_from_cc_terminal(
        terminal, *, expected_route: str = "", authorization_fingerprint: str = "") -> ErrorEnvelope | None:
    """Only the current failed native API terminal can establish this denial.

    Do not scan a user prompt, a successful answer, stderr or an arbitrary JSON
    substring. The repeated provider error/stack after the first JSON object is
    part of the observed CCR transport, not another source of authorization.
    """
    if (not isinstance(terminal, dict) or terminal.get("type") != "result"
            or terminal.get("is_error") is not True
            or terminal.get("terminal_reason") != "api_error"
            or terminal.get("api_error_status") != 401):
        return None
    text = terminal.get("result")
    if not isinstance(text, str):
        return None
    prefix = re.match(
        r"^(?:(?:Failed to authenticate\.\s*)?API Error:\s*401\s+)?"
        r"Error from provider\((?P<route>[^()\n]+):\s*401\):\s*", text)
    if not prefix:
        return None
    try:
        body, _ = json.JSONDecoder().raw_decode(text[prefix.end():])
    except ValueError:
        return None
    if not isinstance(body, dict) or body.get("status") != 401:
        return None
    extension = body.get("ext")
    source = extension.get("error") if isinstance(extension, dict) else None
    message = body.get("message")
    if (not isinstance(source, dict) or source.get("source") != "AIGC"
            or source.get("service") != "aigc" or source.get("stage") != "validation"
            or not isinstance(message, str)
            or not re.match(r"^当前\s*App\s*ID\s*已被禁用(?:[，。,.\s]|$)", message)):
        return None
    details = {"http_status": 401, "source": "AIGC", "service": "aigc", "stage": "validation"}
    if (expected_route and prefix.group("route") == expected_route
            and re.fullmatch(r"[0-9a-f]{64}", authorization_fingerprint or "")):
        details["authorization_fingerprint"] = authorization_fingerprint
    return ErrorEnvelope(
        category="authorization_required", retryable=False, scope="provider",
        tool="claude", code="appid_disabled",
        message=message,
        details=details)


def provider_authorization_fingerprint(appid: str, gateway: str) -> str:
    """Fingerprint an explicit runtime binding, never an unresolved alias/default."""
    if not appid or not gateway:
        return ""
    from model_registry import _resolve_alias
    resolved = _resolve_alias(str(appid))
    if not resolved:
        return ""
    encoded = json.dumps([str(gateway).rstrip("/"), resolved],
                         ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def is_provider_authorization_denial(value) -> bool:
    envelope = ErrorEnvelope.from_value(value)
    return bool(envelope and envelope.category == "authorization_required"
                and envelope.scope == "provider" and envelope.code == "appid_disabled"
                and not envelope.retryable)


def requires_provider_pause(result) -> bool:
    """Request-local pause, not a timed retry or a cross-request credential ban."""
    if requires_provider_backoff(result):
        return True
    return bool(isinstance(result, dict)
                and result.get("status") not in ("ok", "done", "success", "cancelled")
                and is_provider_authorization_denial(result.get("error_envelope")))


def requires_native_settlement(result) -> bool:
    """A stopped caller is not proof its resident native execution finished."""
    if not isinstance(result, dict):
        return False
    facts = result.get("native_facts")
    return bool(
        isinstance(facts, dict)
        and facts.get("native_turn_terminal") == "unobserved"
        and (facts.get("cli_wait_timed_out") is True or facts.get("cli_cancelled") is True)
    )


def requires_execution_pause(result) -> bool:
    """Reuse request-local pauses without treating runtime state as a provider ban."""
    if not isinstance(result, dict) or result.get("status") == "cancelled":
        return False
    return (requires_provider_pause(result) or requires_native_settlement(result)
            or requires_output_recovery_pause(result))


def request_provider_authorization_hold(results, appid: str, gateway: str) -> dict | None:
    """Read only this request's returned failures; do not create an eligibility store."""
    denials = [row for row in results
               if requires_provider_pause(row)
               and is_provider_authorization_denial(row.get("error_envelope"))]
    if not denials:
        return None
    fingerprint = provider_authorization_fingerprint(appid, gateway)
    for result in denials:
        envelope = ErrorEnvelope.from_value(result["error_envelope"])
        observed = envelope.details.get("authorization_fingerprint")
        known = isinstance(observed, str) and bool(re.fullmatch(r"[0-9a-f]{64}", observed))
        if known and fingerprint and observed != fingerprint:
            continue
        held = envelope.to_dict()  # Deep copy: never rewrite the original failed node.
        same = bool(known and fingerprint)
        held["details"]["authorization_hold"] = {
            "basis": "same_authorization" if same else "binding_unverified",
            "source_task_id": str(result.get("task_id") or result.get("key") or ""),
        }
        note = ("当前调用绑定了本请求已明确拒绝的授权，未再次发起原生调用。"
                if same else "无法确认当前绑定与本请求已拒绝的授权独立，未发起原生调用。")
        held["message"] = note + "需管理员处理。\n" + envelope.message
        return held
    return None


def normalize(status: str, error: str = "", fail_kind: str = "", supplied=None,
              tool: str = "") -> ErrorEnvelope:
    existing = ErrorEnvelope.from_value(supplied)
    provider_length = is_provider_context_overflow(status, error, supplied=existing, tool=tool)
    if existing:
        if provider_length or existing.category == "context_overflow":
            # Reclassification is not a grant of execution rights. In particular
            # an explicit False survives repeated normalization.
            return replace(existing, category="context_overflow", scope="request")
        return existing
    status = str(status or "error")
    message = str(error or status)
    if provider_length and (status == "harness_runtime" or fail_kind == "harness_runtime"):
        # The SDK's generic runtime fallback was nonretryable before recognition.
        return ErrorEnvelope(category="context_overflow", retryable=False, scope="request",
                             tool=tool, code=status, message=message)
    blob = f"{status} {message}".lower()
    category = str(fail_kind or "")
    code = status
    scope = "task"
    if not category:
        if status == "cancelled":
            category = "user_cancel"
        elif "permission" in blob or "authorization" in blob or "需要授权" in blob:
            category = "authorization_required"
        elif "credential" in blob or "missing_credential" in blob:
            category = "credential_required"
        elif status == "unknown_harness":
            category = "task_error"
        elif "tool_timeout" in blob or "工具超时" in blob:
            category = "tool_timeout"; scope = "tool"
        elif status == "HTTP 413" and not tool:
            # A reported request-body rejection is not a bad model or a token
            # window overflow. Return the unchanged request to its owner;
            # do not automatically change models or grant a compact retry.
            category = "request_too_large"; scope = "request"
        elif any(x in blob for x in _CONTEXT_ERROR_MARKERS):
            category = "context_overflow"; scope = "request"
        elif any(x in blob for x in ("http 429", "rate limit", "quota exceeded", "每分钟请求次数超过限制")):
            category = "rate_limit"; scope = "provider"
        elif status == "timeout" or "timed out" in blob:
            category = "timeout"
        elif status == "harness_runtime" or "existing project workspace required" in blob:
            category = _NON_RETRYABLE_RUNTIME
        elif any(x in blob for x in ("bwrap", "enospc", "max_user_namespaces",
                                      "max_*_namespaces", "creating new namespace failed",
                                      "nesting depth")):
            # 沙盒/namespace 配额耗尽(2026-09-23):环境病,换模型救不了(每家 CLI 同样撞墙)。
            # 归 harness_runtime 不可重试,交上层换家;接力选家见 _pick_relay_harness 的
            # environment_exhausted 逃生(优先换非 bwrap 消耗型家)。
            category = _NON_RETRYABLE_RUNTIME
        elif "missing environment variable" in blob or "environment variable" in blob and "missing" in blob:
            # 缺 env(如 AIGC_APPID)是环境病:换模型救不了(每个模型都一样缺)——归
            # harness_runtime 不可换模型重试,直接交上层修环境/换家(2026-09-02 codex 27连rc1根因)。
            category = _NON_RETRYABLE_RUNTIME
        elif "本地 router 不可用" in message or "router 不可用" in blob or ("connection refused" in blob and "127.0.0.1" in blob):
            # 本地 router/bridge 进程病(cc-router 3456 无响应):与模型无关,换模型白烧——
            # 归 harness_runtime 交上层重启/换家(b296f23b 同类)。
            category = _NON_RETRYABLE_RUNTIME
        elif re.fullmatch(r"v?\d+(?:\.\d+){1,3}", message.strip()):
            # 部分 CLI 不支持当前无交互执行方式时只打印版本号后退出（实测 opencode:
            # "1.18.26"）。这不是任务内容错误，也不是换模型能修；按 Harness 运行资格失败
            # 立即交给上层换家补位，不能把语义节点永久判死。
            category = _NON_RETRYABLE_RUNTIME
        elif re.search(r"\brc-?\d+\b|worker_no_json", blob):
            category = "process"
        elif any(x in blob for x in ("http 500", "http 502", "http 503", "http 504",
                                     "api call failed", "gateway", "model overloaded", "no choices",
                                     "api stream from", "api request to")):
            category = "gateway"; scope = "provider"
        elif status == "truncated_reasoning":
            category = "truncated"
        # ★模型白名单不认(2026-09-02 真跑揪出:penguin 报"Model is not in the Project config:
        # model_id=deepseek-v4-flash"→归 task_error/retryable=False→不换模型直接退出·补位接力
        # 才救回。这明确是【该家 CLI 不认这个模型名】·换一个它认的模型就好——同 kimi-code
        # "Model is not configured"(_GATEWAY_STRONG 已认)·这里在 envelope 层统一收口)。
        elif re.search(r"(is )?not (in the project config|configured)", blob) \
                or "model_id=" in blob and "config" in blob:
            category = "model_config"
        elif "http 400" in blob or "参数非法" in blob or ("bad request" in blob):
            # 400=该家×该模型的请求形态不兼容(max_tokens 超限/参数拒收)——换一个模型常真救回
            # (2026-09-02 真跑:deepseek×GLM-5.3 400·换 gpt-5.4 done)。归 model_config 可重试,
            # 兼容矩阵(harness_model_compat)会让下一轮选到兼容模型·不重复撞。
            category = "model_config"
        elif "空交付" in message:
            category = "empty"
        elif "计划句" in message or "托词" in message:
            category = "stalled"
        else:
            category = "task_error"
    if category in ("gateway", "rate_limit", "provider"):
        scope = "provider"
    elif category in ("process", "timeout", _NON_RETRYABLE_RUNTIME):
        scope = "harness"
    elif category in _USER:
        scope = "user"
    retryable = category in _RETRYABLE
    if provider_length:
        category, scope = "context_overflow", "request"
    return ErrorEnvelope(category=category, retryable=retryable, scope=scope,
                         tool=tool, code=code, message=message)
