#!/usr/bin/env python3
"""Legacy Responses→Chat bridge, or explicitly bound transparent Responses transport."""
import argparse
import copy
import hashlib
import hmac
import json
import math
import os
import re
import stat
import sys
import threading
import uuid
from pathlib import Path
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import anyio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
import uvicorn

app = FastAPI()

_HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                "te", "trailer", "transfer-encoding", "upgrade"}
_IDENTITY_HEADERS = {"authorization", "api-key", "x-api-key", "x-aigc-appid",
                     "openai-organization", "openai-project", "x-context-provider", "x-source"}
_ERROR_PROFILE = "invalid-prompt-max-length-v1"
_ERROR_AUDIT = "meyo_error_compatibility"
_MAX_ERROR_FRAME_BYTES = 64 * 1024
_SSE_EOL = re.compile(rb"\r\n|\r|\n")
_SSE_LINES = re.compile(rb"([^\r\n]*)(\r\n|\r|\n)")
_REPRESENTATION_VALIDATORS = {"etag", "digest", "content-md5", "content-digest", "repr-digest"}


@dataclass(frozen=True)
class _ResponsesBinding:
    binding_id: str
    model: str
    base_url: str
    api_key: str
    client_token: str
    http_headers: tuple
    timeout_s: float
    max_output_tokens: int | None = None
    stream_idle_timeout_ms: int | None = None
    activity_fd: int | None = None
    error_compatibility_profile: str | None = None


def _configure_mode(application, environment=None):
    """Only the owned bridge process startup sets its immutable transport binding."""
    env = os.environ if environment is None else environment
    mode = env.get("MEYO_RESPONSES_BRIDGE_MODE", "chat")
    if mode == "chat":
        return
    if mode != "passthrough":
        raise ValueError("unknown Responses bridge mode")
    if getattr(application.state, "responses_binding", None) is not None:
        raise ValueError("bridge binding is immutable")
    raw = json.loads(env["MEYO_RESPONSES_BRIDGE_BINDING"])
    if raw.get("wire_api") != "responses":
        raise ValueError("transparent bridge requires a Responses upstream")
    for field in ("binding_id", "model", "base_url", "api_key", "client_token"):
        if (not isinstance(raw.get(field), str) or not raw[field].strip()
                or "\r" in raw[field] or "\n" in raw[field]):
            raise ValueError(f"missing bridge binding field: {field}")
    parsed = urlsplit(raw["base_url"])
    if (parsed.scheme not in ("http", "https") or not parsed.netloc
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path.rstrip("/").endswith(("/chat/completions", "/responses"))):
        raise ValueError("bridge upstream must be a Responses API base URL")
    headers = raw.get("http_headers", {})
    for name, value in headers.items():
        if (not isinstance(name, str) or not isinstance(value, str)
                or name.lower() in (_HOP_HEADERS | {"authorization", "host", "content-length"})
                or "\r" in name + value or "\n" in name + value):
            raise ValueError("invalid bound upstream header")
    timeout = float(raw.get("timeout_s", 180))
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("bridge upstream timeout must be finite and positive")
    limit = raw.get("max_output_tokens")
    if "max_output_tokens" in raw and (type(limit) is not int or limit <= 0):
        raise ValueError("bound max_output_tokens must be a positive integer")
    stream_idle = raw.get("stream_idle_timeout_ms")
    try:
        if "stream_idle_timeout_ms" in raw and (
                type(stream_idle) is not int or stream_idle <= 0
                or not math.isfinite(stream_idle / 1000)):
            raise ValueError()
    except (ValueError, OverflowError):
        raise ValueError("bound stream_idle_timeout_ms must be a positive finite integer") from None
    error_profile = raw.get("error_compatibility_profile")
    if "error_compatibility_profile" in raw and error_profile != _ERROR_PROFILE:
        raise ValueError("unsupported bound error compatibility profile")
    activity_fd = None
    if "MEYO_RESPONSES_ACTIVITY_FD" in env:
        try:
            activity_fd = int(env["MEYO_RESPONSES_ACTIVITY_FD"])
            if stream_idle is None or not stat.S_ISFIFO(os.fstat(activity_fd).st_mode):
                raise ValueError()
            os.set_blocking(activity_fd, False)
        except (OSError, ValueError, TypeError):
            raise ValueError("invalid private Responses activity pipe") from None
    application.state.responses_binding = _ResponsesBinding(
        raw["binding_id"], raw["model"], raw["base_url"].rstrip("/"),
        raw["api_key"], raw["client_token"], tuple(headers.items()), timeout, limit,
        stream_idle, activity_fd, error_profile)


def _unique_sse_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("ambiguous SSE object")
        result[key] = value
    return result


def _rewrite_error_frame(frame):
    """One proven terminal alias; preserve the original error in this same frame.

    The extension is stream-local evidence, not a claim that a native client
    persists it. Unknown/malformed frames and audit namespace collisions pass
    through byte-for-byte. No retry, input/history editing or synthetic success.
    """
    lines = _SSE_LINES.findall(frame)
    data = []
    data_indexes = []
    for index, (line, _) in enumerate(lines):
        field, _, value = line.partition(b":")
        value = value.removeprefix(b" ")
        if field == b"event" and value != b"response.failed":
            return frame
        if field == b"data":
            data.append(value)
            data_indexes.append(index)
    if not data:
        return frame
    try:
        event = json.loads(b"\n".join(data).decode("utf-8"), object_pairs_hook=_unique_sse_object)
        if not isinstance(event, dict) or event.get("type") != "response.failed" or _ERROR_AUDIT in event:
            return frame
        response = event.get("response")
        if not isinstance(response, dict) or response.get("status") != "failed":
            return frame
        error = response.get("error")
        if (not isinstance(error, dict) or "type" in error
                or error.get("code") != "invalid_prompt"
                or error.get("message") != "Prompt exceeds max length"):
            return frame
        original_error = copy.deepcopy(error)
        error["code"] = "context_length_exceeded"
        event[_ERROR_AUDIT] = {
            "profile": _ERROR_PROFILE, "original_error": original_error,
            "original_frame_sha256": hashlib.sha256(frame).hexdigest()}
        encoded = json.dumps(event, ensure_ascii=True, allow_nan=False,
                             separators=(",", ":")).encode("ascii")
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        return frame
    first, *rest = data_indexes
    lines[first] = (b"data: " + encoded, lines[first][1])
    rest = set(rest)
    return b"".join(line + ending for index, (line, ending) in enumerate(lines) if index not in rest)


class _ErrorCompatibilitySSE:
    """Bounded byte framing; only ASCII CR/LF delimit SSE, even across chunks."""
    def __init__(self):
        self.frame = bytearray()
        self.overflow = False
        self.line_has_content = False
        self.pending_cr = False

    def _append(self, part, end_frame=False):
        if self.overflow:
            yield part
        elif len(self.frame) + len(part) > _MAX_ERROR_FRAME_BYTES:
            if self.frame:
                yield bytes(self.frame)
                self.frame.clear()
            yield part
            self.overflow = True
        else:
            self.frame.extend(part)
        if end_frame:
            if not self.overflow:
                yield _rewrite_error_frame(bytes(self.frame))
                self.frame.clear()
            self.overflow = False

    def feed(self, chunk, *, final=False):
        if self.pending_cr:
            chunk = b"\r" + chunk
            self.pending_cr = False
        if not final and chunk.endswith(b"\r"):
            self.pending_cr = True
            chunk = chunk[:-1]
        start = 0
        for match in _SSE_EOL.finditer(chunk):
            if match.start() > start:
                self.line_has_content = True
            end_frame = not self.line_has_content
            self.line_has_content = False
            yield from self._append(chunk[start:match.end()], end_frame)
            start = match.end()
        if start < len(chunk):
            self.line_has_content = True
            yield from self._append(chunk[start:])

    def finish(self):
        yield from self.feed(b"", final=True)
        if self.frame:
            yield bytes(self.frame)  # EOF is not a synthetic blank line/event.
            self.frame.clear()


def _transport_activity(binding, call_id, phase):
    """Owned, content-free pipe; never copy SSE data, headers or credentials."""
    if binding.activity_fd is None:
        return
    frame = {"call_id": call_id, "phase": phase,
             "budget_s": binding.stream_idle_timeout_ms / 1000}
    try:
        os.write(binding.activity_fd, json.dumps(frame, separators=(",", ":")).encode() + b"\n")
    except OSError:
        pass  # Nonblocking observation must not stall or rewrite the original response.


def _request_binding(request):
    # Legacy unit callers also pass tiny request doubles without an ASGI app.
    application = (getattr(request, "scope", None) or {}).get("app")
    return getattr(getattr(application, "state", None), "responses_binding", None)


def _end_to_end_headers(headers, *, request=False):
    connection_headers = {v.strip().lower() for v in headers.get("connection", "").split(",")}
    excluded = _HOP_HEADERS | connection_headers | {"host", "content-length"}
    if request:
        excluded |= _IDENTITY_HEADERS
    return {k: v for k, v in headers.items() if k.lower() not in excluded}


async def _transparent_responses(request, binding):
    """No Chat conversion/retry; only operator-bound budget/error compatibility."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
            token.strip().encode(), binding.client_token.encode()):
        return JSONResponse(status_code=401, content={
            "error": {"type": "authentication_error", "code": "binding_credential_mismatch",
                      "message": "bridge credential does not match this binding"}})
    raw_body = await request.body()
    try:
        body = json.loads(raw_body)
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(status_code=400, content={"error": {"code": "invalid_json"}})
    if not isinstance(body, dict) or body.get("model") != binding.model:
        return JSONResponse(status_code=403, content={
            "error": {"type": "invalid_request_error", "code": "model_binding_mismatch",
                      "message": "request model does not match this binding"}})
    if binding.max_output_tokens is not None:
        if "max_output_tokens" in body:
            # Native request authority is preserved, not silently clamped or enlarged.
            if type(body["max_output_tokens"]) is not int or body["max_output_tokens"] <= 0:
                return JSONResponse(status_code=400, content={
                    "error": {"code": "invalid_max_output_tokens",
                              "message": "native max_output_tokens must be a positive integer"}})
        else:
            body["max_output_tokens"] = binding.max_output_tokens
            raw_body = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = _end_to_end_headers(request.headers, request=True)
    headers.update(dict(binding.http_headers))
    headers["Authorization"] = f"Bearer {binding.api_key}"
    timeout = (httpx.Timeout(binding.timeout_s, read=binding.stream_idle_timeout_ms / 1000)
               if binding.stream_idle_timeout_ms is not None else binding.timeout_s)
    client = httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False)
    call_id = uuid.uuid4().hex if binding.activity_fd is not None else ""
    _transport_activity(binding, call_id, "start")
    try:
        upstream = await client.send(client.build_request(
            "POST", binding.base_url + "/responses", content=raw_body, headers=headers), stream=True)
    except httpx.HTTPError as exc:
        _transport_activity(binding, call_id, "error")
        await client.aclose()
        timeout = isinstance(exc, httpx.TimeoutException)
        return JSONResponse(status_code=504 if timeout else 502, content={
            "error": {"type": "upstream_error",
                      "code": "upstream_timeout" if timeout else "upstream_transport_error",
                      "message": type(exc).__name__}})
    except BaseException:
        _transport_activity(binding, call_id, "error")
        with anyio.CancelScope(shield=True):
            await client.aclose()
        raise

    async def close():
        with anyio.CancelScope(shield=True):
            try:
                await upstream.aclose()
            finally:
                await client.aclose()

    normalize_errors = (
        binding.error_compatibility_profile == _ERROR_PROFILE
        and upstream.status_code == 200
        and upstream.headers.get("content-type", "").split(";", 1)[0].strip().lower() == "text/event-stream"
        and upstream.headers.get("content-encoding", "").strip().lower() in ("", "identity"))
    normalizer = _ErrorCompatibilitySSE() if normalize_errors else None
    response_headers = _end_to_end_headers(upstream.headers)
    if normalize_errors:
        # Headers precede the stream, so invalidate these before any possible rewrite.
        response_headers = {k: v for k, v in response_headers.items()
                            if k.lower() not in _REPRESENTATION_VALIDATORS}

    async def chunks():
        phase = "error" if upstream.status_code >= 400 else "end"
        try:
            try:
                async for chunk in upstream.aiter_raw():
                    if chunk:
                        _transport_activity(binding, call_id, "bytes")
                    if normalizer is None:
                        yield chunk
                    else:
                        for part in normalizer.feed(chunk):
                            yield part
            except httpx.HTTPError:
                if normalizer is not None:
                    for part in normalizer.finish():
                        yield part
                raise  # Preserve an actual broken stream, not a completed event.
            if normalizer is not None:
                for part in normalizer.finish():
                    yield part
        except BaseException:
            phase = "error"
            raise
        finally:
            _transport_activity(binding, call_id, phase)
            # A mid-stream failure closes the stream; never fabricate completion.
            await close()

    return StreamingResponse(chunks(), status_code=upstream.status_code,
                             headers=response_headers,
                             background=BackgroundTask(close))


def _chat_upstream(value: str) -> str:
    """兼容模式连接 Chat 入口，不能在 Responses 路径后继续拼 Chat 路径。"""
    parsed = urlsplit(value.rstrip("/"))
    if (parsed.hostname == "aigc.sankuai.com" and parsed.scheme == "https"
            and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment
            and parsed.path in ("/v1", "/v1/responses", "/agentic/v1", "/agentic/v1/responses")):
        return urlunsplit((parsed.scheme, parsed.netloc, "/v1/openai/native", "", ""))
    return value.rstrip("/").removesuffix("/chat/completions")


UPSTREAM = _chat_upstream(os.environ.get("MEYO_UNIFIED_GATEWAY")
                          or "https://aigc.sankuai.com/v1/openai/native")
_MODELS_PATH = Path.home() / ".claude/skills/aigc-model-router/models.json"


def _resolve_alias(value: str) -> str | None:
    """别名→真实appid。语义与 model_registry._resolve_alias 一致(不复制实现·同一约定:
    MEYO_APPID_ALIASES env·格式 alias=值,逗号分隔)。bridge 由 _codex_bridge_ctx 拉起,
    继承部署 env,因此解析器与父进程同源同约定即可。"""
    if not str(value).startswith("appid-"):
        return str(value)
    env_map = os.environ.get("MEYO_APPID_ALIASES", "")
    for pair in env_map.split(","):
        pair = pair.strip()
        if "=" in pair:
            k, v = pair.split("=", 1)
            if k.strip() == str(value):
                return v.strip() or None
    return None


def _load_models_lazy() -> dict:
    """按需读模型配置。import 时强读会在独立 bridge(env 已齐但无配置文件)场景起不来;
    读不到返回空 dict(凭证链此时只走 env,不依赖配置)。"""
    try:
        return json.loads(_MODELS_PATH.read_text())
    except Exception:
        return {}


def _request_api_key(request: Request) -> str | None:
    """本请求凭证(每单作用域·不缓存跨请求),以 Codex 真实请求为准:
    ① 请求自带 Authorization: Bearer <key>(codex provider env_key=AIGC_APPID 原生发送)
       — 真值原样返回;别名必须经 MEYO_APPID_ALIASES 解析成功,失败=未授权 None(不兜底)
    ② env AIGC_APPID(父进程 _codex_adapter 写的每单 ctx.appid)·别名同样必须解析成功
    ③ MEYO_UNIFIED_APPID(部署模板批准的兜底)
    ④ 配置首个可解析 appid(按需读)
    全缺/全未解析 → None:401,绝不发空或未解析别名 Bearer。"""
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
        if supplied:
            return _resolve_alias(supplied)
    for env_name in ("AIGC_APPID", "MEYO_UNIFIED_APPID"):
        v = os.environ.get(env_name, "").strip()
        if v:
            r = _resolve_alias(v)
            if r:
                return r
    for cand in _load_models_lazy().get("programming", {}).get("candidates", []):
        for a in (cand.get("appids") or []):
            r = _resolve_alias(str(a))
            if r:
                return r
    return None
MARS_DATA = Path(__file__).resolve().parents[2]
EVOLUTION = MARS_DATA / "seed-users-system" / "v11" / "evolution"
sys.path.insert(0, str(EVOLUTION))
_SEARCH_CACHE: dict[tuple[str, int], Any] = {}
_SEARCH_LOCKS: dict[tuple[str, int], threading.Lock] = {}
_SEARCH_LOCKS_GUARD = threading.Lock()

# Responses 请求的最后一道硬预算。网关侧模型窗口并不总能从 /models 获得，因此默认按
# 128K 家族保守留出 32K 输出/内部推理空间，并允许部署按真实模型覆盖。估算使用 UTF-8 字节数
# 作为 token 上界：BPE token 不会多于组成文本的原始字节数，虽然比真实 token 更保守，但能保证
# “估算通过却仍超窗”不会成为常态。正常短请求完全不改，只有组合后超预算才分层退化。
_DEFAULT_CONTEXT_WINDOW = 131072
_DEFAULT_INPUT_BUDGET = 90000
_MIN_INPUT_BUDGET = 4096
_CONTEXT_RETRY_RATIOS = (0.62, 0.38)
_CONTEXT_ERROR_MARKERS = (
    "context_length_exceeded",
    "exceeds the context window",
    "maximum context length",
    "too many tokens",
)
_CONTEXT_NOTICE = "【上下文预算降级】完整现场仍保存在原生会话/任务存储中；本次发送保留用户目标、最新现场和关键工具链，较旧或过大的中间材料已按层级压缩。"


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for item in content or []:
        if isinstance(item, dict) and item.get("type") in {"input_text", "output_text", "text"}:
            parts.append(str(item.get("text") or ""))
    return "\n".join(parts)


_CHAT_ROLES = {"system", "user", "assistant", "tool"}


def _map_role(role: str) -> tuple[str, str]:
    """Responses role → chat completions role。

    developer 是 Responses 协议的高优先级系统指令位(chat 协议无此 role;实测
    kimi 400 'role developer is not allowed'·deepseek 'unknown variant developer')。
    映射为 system 并在内容前标注来源(保留可追溯性);user 永不提升为 system。
    其他 chat 已知 role 原样;未知 role 保守按 user(不丢内容)。
    """
    role = str(role or "user")
    if role in _CHAT_ROLES:
        return role, ""
    if role == "developer":
        return "system", "[developer] "
    return "user", ""


def _messages(body: dict) -> list[dict]:
    messages = []
    instructions = body.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": instructions})
    pending_calls = []
    for item in body.get("input") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "message":
            role, prefix = _map_role(item.get("role", "user"))
            messages.append({"role": role, "content": prefix + _text(item.get("content"))})
        elif kind == "function_call":
            pending_calls.append({
                "id": item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {"name": item.get("name", ""), "arguments": item.get("arguments", "{}")},
            })
        elif kind == "local_shell_call":
            # codex re-sends its shell call; present it to the chat model as a
            # `shell` function tool_call so the tool-loop history stays coherent.
            action = item.get("action") or {}
            pending_calls.append({
                "id": item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {
                    "name": SHELL_TOOL_NAME,
                    "arguments": json.dumps({"command": action.get("command") or []},
                                            ensure_ascii=False),
                },
            })
        elif kind in ("function_call_output", "local_shell_call_output"):
            if pending_calls:
                messages.append({"role": "assistant", "content": None, "tool_calls": pending_calls})
                pending_calls = []
            output = item.get("output")
            if isinstance(output, dict):
                output = output.get("content") or output.get("stdout") or json.dumps(output, ensure_ascii=False)
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id") or "",
                "content": str(output or ""),
            })
    if pending_calls:
        messages.append({"role": "assistant", "content": None, "tool_calls": pending_calls})
    return messages


def _token_upper_bound(value: Any) -> int:
    """返回可审计的保守 token 上界；序列化结构开销也进入预算。"""
    if isinstance(value, str):
        raw = value
    else:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return max(1, len(raw.encode("utf-8", errors="replace")))


def _truncate_text(text: Any, budget: int, *, head_ratio: float = 0.45) -> str:
    """按上界预算保留首尾；首部保目标/协议，尾部保最新问题/错误。"""
    value = str(text or "")
    if budget <= 0:
        return ""
    if _token_upper_bound(value) <= budget:
        return value
    encoded = value.encode("utf-8", errors="replace")
    digest = hashlib.sha256(encoded).hexdigest()[:12]
    marker = f"\n…[上下文压缩 sha256={digest} 原始字节={len(encoded)}]…\n"
    marker_bytes = len(marker.encode("utf-8"))
    if budget <= marker_bytes:
        return encoded[:budget].decode("utf-8", errors="ignore")
    available = budget - marker_bytes
    head_bytes = int(available * head_ratio)
    tail_bytes = available - head_bytes
    head = encoded[:head_bytes].decode("utf-8", errors="ignore")
    tail = encoded[-tail_bytes:].decode("utf-8", errors="ignore") if tail_bytes else ""
    return head + marker + tail


def _compact_schema(value: Any, *, description_limit: int = 320) -> Any:
    """保留工具名称、类型、properties/required；只压缩解释性长文本和巨大示例。"""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in {"examples", "example", "$comment"}:
                continue
            if key in {"description", "title"} and isinstance(item, str):
                result[key] = _truncate_text(item, description_limit, head_ratio=0.75)
            else:
                result[key] = _compact_schema(item, description_limit=description_limit)
        return result
    if isinstance(value, list):
        if len(value) > 80:
            return [*_compact_schema(value[:40], description_limit=description_limit),
                    "…[列表已按上下文预算压缩]…",
                    *_compact_schema(value[-39:], description_limit=description_limit)]
        return [_compact_schema(item, description_limit=description_limit) for item in value]
    if isinstance(value, str) and _token_upper_bound(value) > 2048:
        return _truncate_text(value, 2048)
    return value


def _message_groups(messages: list[dict]) -> list[list[dict]]:
    """把 assistant tool_calls 与紧随其后的 tool outputs 视为不可拆的历史单元。"""
    groups: list[list[dict]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        group = [message]
        if message.get("role") == "assistant" and message.get("tool_calls"):
            index += 1
            while index < len(messages) and messages[index].get("role") == "tool":
                group.append(messages[index])
                index += 1
            groups.append(group)
            continue
        groups.append(group)
        index += 1
    return groups


def _compact_call_arguments(message: dict, limit: int = 4096) -> dict:
    result = copy.deepcopy(message)
    for call in result.get("tool_calls") or []:
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict):
            continue
        arguments = str(function.get("arguments") or "")
        if _token_upper_bound(arguments) <= limit:
            continue
        function["arguments"] = json.dumps({
            "_context_compacted": True,
            "sha256": hashlib.sha256(arguments.encode("utf-8", errors="replace")).hexdigest(),
            "preview": _truncate_text(arguments, max(512, limit - 160)),
        }, ensure_ascii=False, separators=(",", ":"))
    return result


def _shrink_group(group: list[dict], budget: int, *, latest_user: bool = False) -> list[dict]:
    """压缩一个必须保留的消息组，不制造孤立 tool output。"""
    rows = [_compact_call_arguments(row) for row in group]
    if _token_upper_bound(rows) <= budget:
        return rows
    text_rows = [index for index, row in enumerate(rows)
                 if isinstance(row.get("content"), str) and row.get("content")]
    structural = copy.deepcopy(rows)
    for index in text_rows:
        structural[index]["content"] = ""
    structural_cost = _token_upper_bound(structural)
    available = max(0, budget - structural_cost)
    weights = []
    for index in text_rows:
        role = str(rows[index].get("role") or "")
        weights.append(4 if latest_user and role == "user" else 3 if role == "tool" else 2)
    weight_total = sum(weights) or 1
    for index, weight in zip(text_rows, weights):
        share = max(96, int(available * weight / weight_total)) if available else 0
        rows[index]["content"] = _truncate_text(
            rows[index].get("content"), share,
            head_ratio=0.35 if rows[index].get("role") in {"user", "tool"} else 0.65)
    if _token_upper_bound(rows) <= budget:
        return rows
    for row in rows:
        for call in row.get("tool_calls") or []:
            function = call.get("function") if isinstance(call, dict) else None
            if isinstance(function, dict):
                function["arguments"] = '{"_context_compacted":true}'
    # JSON 结构本身会带少量字节开销；若只是超出几个字节，继续精确收紧最大正文，不能突然
    # 从完整首尾窗口坠落到 256 字节。这样目标附近的 objection 不会因估算边际被误删。
    while _token_upper_bound(rows) > budget:
        candidates = [(index, _token_upper_bound(row.get("content")))
                      for index, row in enumerate(rows)
                      if isinstance(row.get("content"), str) and row.get("content")]
        if not candidates:
            break
        index, size = max(candidates, key=lambda item: item[1])
        excess = _token_upper_bound(rows) - budget
        target = max(0, size - excess - 8)
        rows[index]["content"] = _truncate_text(rows[index]["content"], target,
                                                  head_ratio=0.35 if latest_user else 0.65)
    return rows


def _compact_messages(messages: list[dict], budget: int) -> tuple[list[dict], dict]:
    original = copy.deepcopy(messages)
    original_tokens = _token_upper_bound(original)
    report = {"original_message_tokens": original_tokens, "dropped_message_groups": 0,
              "truncated_message_groups": 0}
    if original_tokens <= budget:
        report["final_message_tokens"] = original_tokens
        return original, report

    notice = {"role": "system", "content": _CONTEXT_NOTICE}
    notice_cost = _token_upper_bound([notice])
    working_budget = max(512, budget - notice_cost)
    groups = _message_groups(original)
    latest_user_group = max((index for index, group in enumerate(groups)
                             if any(row.get("role") == "user" for row in group)), default=-1)
    system_group = next((index for index, group in enumerate(groups)
                         if any(row.get("role") == "system" for row in group)), -1)
    latest_group = len(groups) - 1
    protected = {index for index in (system_group, latest_user_group, latest_group) if index >= 0}
    kept = list(groups)
    for index in range(len(groups)):
        if _token_upper_bound(kept) <= working_budget:
            break
        if index in protected:
            continue
        kept[index] = []
        report["dropped_message_groups"] += 1
    selected = [group for group in kept if group]

    if _token_upper_bound(selected) > working_budget:
        active = [index for index in sorted(protected) if kept[index]]
        def _weight(index: int) -> int:
            if index == latest_user_group:
                return 6
            if index == latest_group:
                return 3
            return 2 if index == system_group else 1
        total_weight = sum(_weight(index) for index in active) or 1
        remaining = working_budget
        for position, index in enumerate(active):
            allocation = remaining if position == len(active) - 1 else max(
                512, int(working_budget * _weight(index) / total_weight))
            allocation = min(allocation, remaining)
            remaining = max(0, remaining - allocation)
            before = _token_upper_bound(kept[index])
            kept[index] = _shrink_group(kept[index], allocation,
                                        latest_user=index == latest_user_group)
            if _token_upper_bound(kept[index]) < before:
                report["truncated_message_groups"] += 1
        selected = [group for group in kept if group]

    flattened = [notice, *(row for group in selected for row in group)]
    while _token_upper_bound(flattened) > budget:
        candidates = [(index, _token_upper_bound(row.get("content")))
                      for index, row in enumerate(flattened)
                      if isinstance(row.get("content"), str) and row.get("content")]
        if not candidates:
            break
        index, size = max(candidates, key=lambda item: item[1])
        target = max(0, size - (_token_upper_bound(flattened) - budget) - 64)
        flattened[index]["content"] = _truncate_text(flattened[index]["content"], target)
    report["final_message_tokens"] = _token_upper_bound(flattened)
    return flattened, report


def _context_window(model: str) -> int:
    try:
        overrides = json.loads(os.environ.get("MEYO_MODEL_CONTEXT_WINDOWS_JSON", "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        overrides = {}
    try:
        return max(8192, int(overrides.get(model) or os.environ.get(
            "MEYO_RESPONSES_CONTEXT_WINDOW", _DEFAULT_CONTEXT_WINDOW)))
    except (TypeError, ValueError):
        return _DEFAULT_CONTEXT_WINDOW


def _input_budget(body: dict, model: str, override: int | None = None) -> int:
    window = _context_window(model)
    try:
        requested_output = max(1024, int(body.get("max_output_tokens") or 16384))
    except (TypeError, ValueError):
        requested_output = 16384
    safety = max(4096, int(window * 0.05))
    available = max(_MIN_INPUT_BUDGET, window - min(requested_output, window // 2) - safety)
    try:
        hard_cap = max(_MIN_INPUT_BUDGET, int(os.environ.get(
            "MEYO_RESPONSES_INPUT_BUDGET", _DEFAULT_INPUT_BUDGET)))
    except (TypeError, ValueError):
        hard_cap = _DEFAULT_INPUT_BUDGET
    budget = min(available, hard_cap)
    return max(_MIN_INPUT_BUDGET, min(budget, int(override))) if override else budget


def _budget_payload(payload: dict, body: dict, *, budget_override: int | None = None,
                    retry_index: int = 0) -> tuple[dict, dict]:
    """在唯一网络发送边界预算完整 payload，而非只截某一来源。"""
    result = copy.deepcopy(payload)
    model = str(result.get("model") or "")
    budget = _input_budget(body, model, budget_override)
    original_tokens = _token_upper_bound({"messages": result.get("messages") or [],
                                          "tools": result.get("tools") or []})
    try:
        requested_output = max(0, int(result.get("max_tokens") or 0))
    except (TypeError, ValueError):
        requested_output = 0
    # 正常短请求字节级保持不变：不删 schema example、不改消息、不插降级提示。
    if original_tokens <= budget and not retry_index:
        report = {
            "degraded": False, "reason": "within_budget", "model": model,
            "context_window": _context_window(model), "input_budget_tokens": budget,
            "original_estimated_tokens": original_tokens,
            "final_estimated_tokens": original_tokens,
            "original_max_output_tokens": requested_output,
            "final_max_output_tokens": requested_output,
            "dropped_tools": 0, "tools_compacted": False, "retry_index": 0,
            "original_message_tokens": _token_upper_bound(result.get("messages") or []),
            "final_message_tokens": _token_upper_bound(result.get("messages") or []),
            "dropped_message_groups": 0, "truncated_message_groups": 0,
        }
        return result, report
    tools = list(result.get("tools") or [])
    compacted_tools = [_compact_schema(tool) for tool in tools]
    tools_compacted = compacted_tools != tools
    tool_budget = max(1024, int(budget * 0.28))
    while compacted_tools and _token_upper_bound(compacted_tools) > tool_budget:
        compacted_tools.pop()
    dropped_tools = len(tools) - len(compacted_tools)
    if compacted_tools:
        result["tools"] = compacted_tools
        result["tool_choice"] = "auto"
    else:
        result.pop("tools", None)
        result.pop("tool_choice", None)
    message_budget = max(1024, budget - _token_upper_bound(compacted_tools) - 256)
    compacted_messages, message_report = _compact_messages(
        list(result.get("messages") or []), message_budget)
    result["messages"] = compacted_messages
    final_tokens = _token_upper_bound({"messages": compacted_messages,
                                       "tools": result.get("tools") or []})
    safety = max(4096, int(_context_window(model) * 0.05))
    max_output = max(1024, _context_window(model) - final_tokens - safety)
    if requested_output:
        result["max_tokens"] = min(requested_output, max_output)
    report = {
        "degraded": bool(original_tokens > budget or tools_compacted or dropped_tools or retry_index),
        "reason": "context_window_retry" if retry_index else
                  "preflight_hard_budget" if original_tokens > budget else "within_budget",
        "model": model, "context_window": _context_window(model),
        "input_budget_tokens": budget,
        "original_estimated_tokens": original_tokens,
        "final_estimated_tokens": final_tokens,
        "original_max_output_tokens": requested_output,
        "final_max_output_tokens": int(result.get("max_tokens") or 0),
        "dropped_tools": dropped_tools, "tools_compacted": tools_compacted,
        "retry_index": retry_index, **message_report,
    }
    return result, report


def _is_context_error(response: Any) -> bool:
    if int(getattr(response, "status_code", 0) or 0) < 400:
        return False
    text = str(getattr(response, "text", "") or "").lower()
    return any(marker in text for marker in _CONTEXT_ERROR_MARKERS)


# Codex exec relies on a built-in local shell. Against the real Responses API the
# shell is provided server-side, so codex sends tools=[] and never gets a callable
# shell when bridged to chat completions. We synthesize a `shell` function tool so
# the chat model can drive codex's shell, and map the model's calls back to codex's
# native local_shell_call output items (and its outputs back to tool messages).
# Must match codex's built-in tool exactly: name "exec_command" with a string
# "cmd" parameter (see codex-rs tools/handlers/shell_spec.rs). Codex's router only
# executes calls whose name/schema match its registered exec tool.
SHELL_TOOL_NAME = "exec_command"
_SHELL_FUNCTION = {
    "type": "function",
    "function": {
        "name": SHELL_TOOL_NAME,
        "description": (
            "Execute a shell command and return its stdout/stderr. Use this for "
            "every web search and file operation. For web search run exactly: "
            "python3.12 <search_fabric_cli.py path> \"QUERY\"."),
        "parameters": {
            "type": "object",
            "properties": {
                "cmd": {
                    "type": "string",
                    "description": "Shell command to execute, e.g. echo hi",
                },
                "workdir": {"type": "string",
                            "description": "Working directory (optional)."},
            },
            "required": ["cmd"],
        },
    },
}


def _tools(body: dict) -> list[dict]:
    tools = []
    has_shell = False
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        ttype = tool.get("type")
        if ttype == "function":
            tools.append({
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters") or {},
                },
            })
        elif ttype == "custom":
            tools.append({
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": {"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]},
                },
            })
        elif ttype in ("local_shell", "shell"):
            has_shell = True
    # Ensure the exec_command shell tool is present exactly once. Bug fixed 2026-08-25:
    # the old code appended _SHELL_FUNCTION in BOTH branches, so when codex declared a
    # shell tool (or a function already named exec_command) the upstream got duplicate
    # tool names → "Tool names must be unique" 400 → codex turn.failed. Dedup by name and
    # add the synthesized shell only if no exec_command is already present.
    seen, deduped = set(), []
    for t in tools:
        nm = (t.get("function") or {}).get("name", "")
        if nm in seen:
            continue
        seen.add(nm)
        deduped.append(t)
    # Codex sends no callable shell schema and needs the compatibility tool above.
    # Other Responses clients (notably Omnigent openai-agents) already send their
    # own client-side tools such as Read/Write/Bash. Injecting exec_command beside
    # those advertises a tool their executor cannot dispatch, so the model calls it
    # and the turn fails with "Tool exec_command not found". Only synthesize the
    # Codex shell when the request has no function tools of its own.
    if not deduped and SHELL_TOOL_NAME not in seen:
        deduped.append(_SHELL_FUNCTION)
    return deduped


def _event(kind: str, payload: dict) -> str:
    return f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.get("/health")
def health(request: Request = None):
    binding = _request_binding(request)
    if binding:
        return {"ok": True, "mode": "passthrough", "wire_api": "responses",
                "binding_id": binding.binding_id}
    return {"ok": True}


@app.get("/search")
def search(q: str, top_k: int = 8, request: Request = None):
    if _request_binding(request):
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    key = (q.strip(), top_k)
    if key in _SEARCH_CACHE:
        return _SEARCH_CACHE[key]
    with _SEARCH_LOCKS_GUARD:
        key_lock = _SEARCH_LOCKS.setdefault(key, threading.Lock())
    with key_lock:
        if key not in _SEARCH_CACHE:
            from search_fabric import SearchFabric
            _SEARCH_CACHE[key] = SearchFabric().search(key[0], top_k=top_k)
        return _SEARCH_CACHE[key]


@app.post("/v1/responses")
async def responses(request: Request):
    binding = _request_binding(request)
    if binding:
        return await _transparent_responses(request, binding)
    body = await request.json()
    rid = f"resp_{uuid.uuid4().hex}"
    # 每单凭证(2026-09-10 监督指正):父进程 per-request 选的授权优先,不能全局主 key
    # 把 24 条映射分配抹掉。请求头 X-AIGC-Appid > env AIGC_APPID > MEYO_UNIFIED_APPID
    # > 配置首项;并发复用同一 bridge 时每请求独立解析,不沿用上一单授权。
    _api_key = _request_api_key(request)
    if not _api_key:
        # 缺凭证:明确 401,绝不发空/别名 Bearer(宁缺勿假)。
        return JSONResponse(status_code=401, content={
            "error": {"message": "bridge: no usable credential for this request "
                                  "(X-AIGC-Appid/AIGC_APPID/MEYO_UNIFIED_APPID all absent)",
                             "type": "authentication_error"}})
    # 模型以请求实际 body.model 为准(父层 -c -m 已选 GLM/Kimi 等;监督指正:旧默认
    # gpt-5.6-sol 会把 GLM/Kimi 请求改回旧模型=替模型选择)。仅当 codex 内部别名
    # (如 vertex.claude-*)上游不认时,才用 MEYO_BRIDGE_MODEL 显式覆盖;无 body.model
    # 时才退部署 env,再退 GLM-5.3(当前可用主力),不再默认退役模型。
    bridge_model = (str(body.get("model") or "").strip()
                    or os.environ.get("MEYO_BRIDGE_MODEL", "")
                    or "GLM-5.3")
    payload = {
        "model": bridge_model,
        "messages": _messages(body),
        "stream": False,
    }
    # Codex 已按逻辑 session 生成 prompt_cache_key；bridge 只做协议转换并原样透传，
    # 让 AIGC 网关把同一用户/会话/家路由到同一缓存槽。空值不发送，保持旧请求兼容。
    cache_key = os.environ.get("MEYO_PROMPT_CACHE_KEY") or body.get("prompt_cache_key")
    if cache_key:
        payload["prompt_cache_key"] = str(cache_key)[:64]
    tools = _tools(body)
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if body.get("max_output_tokens"):
        payload["max_tokens"] = body["max_output_tokens"]
    # 在最后网络边界统一预算 instructions、原生 resume 历史、工具结果、工具 schema 和本轮任务。
    # 上游各层仍可保真保存完整现场；这里只决定本次能安全进入模型窗口的投影。
    original_payload = copy.deepcopy(payload)
    payload, context_report = _budget_payload(original_payload, body)
    # trust_env=False:网关(aigc.sankuai.com)是内网服务·必须直连·不读环境代理。持久化根治
    # (用户2026-09-01):部署机若设死代理 HTTP_PROXY,httpx 默认会把网关请求也发给死代理→
    # 502/Connection refused→codex 等走 Responses 协议的家整家卡死。bridge 只连 UPSTREAM 内网·
    # 直连最稳(此前 codex smoke 卡在 http://127.0.0.1:8765/v1/responses→upstream 走死代理的真根因)。
    async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
        upstream = await client.post(
            f"{UPSTREAM}/chat/completions",
            headers={"Authorization": f"Bearer {_api_key}", "Content-Type": "application/json"},
            json=payload,
        )
        # 模型实际窗口可能小于清单/默认值。只对明确的上下文错误逐级缩预算重组原请求，
        # 不对认证、限流或普通 5xx 做同样重试，避免把基础设施故障伪装成“继续压缩”。
        if _is_context_error(upstream):
            base_budget = min(int(context_report["input_budget_tokens"]),
                              int(context_report["original_estimated_tokens"]))
            for retry_index, ratio in enumerate(_CONTEXT_RETRY_RATIOS, 1):
                retry_budget = max(_MIN_INPUT_BUDGET, int(base_budget * ratio))
                payload, context_report = _budget_payload(
                    original_payload, body, budget_override=retry_budget, retry_index=retry_index)
                upstream = await client.post(
                    f"{UPSTREAM}/chat/completions",
                    headers={"Authorization": f"Bearer {_api_key}", "Content-Type": "application/json"},
                    json=payload,
                )
                if not _is_context_error(upstream):
                    break
    if upstream.status_code >= 400:
        return JSONResponse(status_code=upstream.status_code, content={
            "error": upstream.text, "context_budget": context_report})
    data = upstream.json()
    message = ((data.get("choices") or [{}])[0].get("message") or {})
    usage = data.get("usage") or {}
    events = [
        ("response.created", {"type": "response.created", "response": {
            "id": rid, "status": "in_progress", "output": [],
            "metadata": {"meyo_context_budget": context_report}}}),
    ]
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        call_id = call.get("id") or f"call_{uuid.uuid4().hex[:12]}"
        name = function.get("name", "")
        arguments = function.get("arguments", "{}")
        # exec_command uses a string "cmd"; if the model emitted an argv array or a
        # bare string, normalize to codex's expected schema.
        if name == SHELL_TOOL_NAME:
            try:
                args = json.loads(arguments or "{}")
            except Exception:
                args = {}
            cmd = args.get("cmd")
            if cmd is None:
                c = args.get("command")
                if isinstance(c, list):
                    import shlex
                    cmd = " ".join(shlex.quote(str(x)) for x in c[2:]) if (
                        len(c) >= 3 and c[0] in ("bash", "zsh", "sh") and c[1] == "-lc") \
                        else " ".join(str(x) for x in c)
                else:
                    cmd = str(c or "")
            new_args = {"cmd": cmd}
            if args.get("workdir"):
                new_args["workdir"] = args["workdir"]
            arguments = json.dumps(new_args, ensure_ascii=False)
        events.append(("response.output_item.done", {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
            },
        }))
    content = message.get("content") or ""
    if content:
        events.append(("response.output_item.done", {
            "type": "response.output_item.done",
            "item": {"type": "message", "role": "assistant", "id": f"msg_{uuid.uuid4().hex}",
                     "content": [{"type": "output_text", "text": content}]},
        }))
    events.append(("response.completed", {
        "type": "response.completed",
        "response": {
            "id": rid,
            # codex 解析 completed 事件要求 response.status=="completed"——缺失时
            # 会话被判未完成(rc0 空 final 的真因之一·2026-09-10 监督指正)。
            "status": "completed",
            # OpenAI Agents SDK 的 ModelResponse 要求 completed.response.output 为 list；
            # 旧 bridge 只发 output_item.done，却在 completed 里漏掉 output，第二轮恢复时校验成 None 崩溃。
            "output": [e[1]["item"] for e in events if e[0] == "response.output_item.done"],
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "input_tokens_details": None,
                "output_tokens": usage.get("completion_tokens", 0),
                "output_tokens_details": None,
                "total_tokens": usage.get("total_tokens", 0),
            },
            "metadata": {"meyo_context_budget": context_report},
        },
    }))
    if body.get("stream", True):
        async def generate():
            for kind, event in events:
                yield _event(kind, event)
        return StreamingResponse(
            generate(), media_type="text/event-stream",
            headers={"X-Meyo-Context-Degraded": "1" if context_report.get("degraded") else "0"})
    return {"id": rid, "object": "response", "status": "completed",
            "output": [e[1].get("item") for e in events if e[0] == "response.output_item.done"],
            "metadata": {"meyo_context_budget": context_report}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18756)
    parser.add_argument("--fd", type=int, default=None)
    args = parser.parse_args()
    _configure_mode(app)
    uvicorn.run(app, host="127.0.0.1", port=args.port, fd=args.fd, log_level="warning")


if __name__ == "__main__":
    main()
