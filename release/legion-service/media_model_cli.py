#!/usr/bin/env python3
"""随服务发布的 image/chat CLI；复用 router 的 OpenAI 请求和图像解码协议。

配置与授权只读 model_registry 或父层绑定的个人 profile，不搜索 HOME 下的可执行脚本。
候选按配置顺序选择，授权只属于该候选或所在方向，不借其他方向授权。
"""
import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request
import zlib

import model_registry


class MediaError(Exception):
    """仅允许不含请求、响应正文或凭证的固定错误码。"""

    def __init__(self, code, *, request_started=False):
        super().__init__(code)
        # Per-attempt fact for image JSON, not a claim about billing or delivery.
        self.request_started = request_started


_IMAGE_FORMATS = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".gif": "gif", ".webp": "webp"}
_IMAGE_PRIVATE_DIRS = frozenset({
    "secrets", ".ssh", ".aws", ".azure", ".gnupg", ".config",
    ".codex", ".claude", ".meyo", ".legion", ".git",
})
_DECISIONS_JSON_LIMIT = 2 * 1024 * 1024
_DECISIONS_BODY_LIMIT = 32 * 1024 * 1024
_DECISIONS_MEDIA_KEYS = frozenset({
    "image_url", "input_image", "input_audio", "audio_url", "video_url",
    "file_data", "file_url", "images", "assets",
})
_DECISIONS_MEDIA_TYPES = frozenset({
    "image", "image_url", "input_image", "audio", "input_audio", "audio_url",
    "video", "video_url", "file", "input_file",
})


def model_tool_specs():
    """只描述已随程序提供的入口；发现目录不探测或请求模型。"""
    content = {"anyOf": [{"type": kind} for kind in ("string", "object", "array")]}
    question_schemas = []
    for kind in ("noul", "choice", "score", "box"):
        fields = {"type": {"type": "string", "enum": [kind]}, "instructions": content}
        required = ["type", "instructions"]
        if kind == "noul":
            fields["criteria"] = {"type": "object", "properties": {"true": content, "false": content},
                                  "required": ["true", "false"], "additionalProperties": False}
        elif kind == "choice":
            fields["criteria"] = {"type": "object", "minProperties": 1,
                                  "additionalProperties": {"anyOf": [*content["anyOf"], {"type": "null"}]}}
            required.append("criteria")
        elif kind == "score":
            fields["criteria"] = {"type": "array", "minItems": 1, "items": content}
            required.append("criteria")
        question_schemas.append({"type": "object", "properties": fields, "required": required,
                                 "additionalProperties": False})
    properties = {
        "domain": {"type": "string"},
        "profile_id": {"type": "string", "description": "父层已绑定的私有候选 ID；按次选用，不改变默认"},
        "prompt": {"type": "string"}, "model": {"type": "string"},
        "image": {"type": "string", "description": "当前工作目录内的图片；读取并校验真实 bytes 后发送"},
        "max_tokens": {"type": "integer", "minimum": 1},
        "timeout_s": {"type": "number", "minimum": 0},
        "state": {**content, "description": "JSON文字上下文；不接受内嵌媒体，图片只经 image 工作区路径读取"},
        "questions": {"type": "object", "minProperties": 1,
                      "additionalProperties": {"oneOf": question_schemas}},
    }
    def schema(names, required):
        return {"type": "object", "properties": {key: properties[key] for key in names},
                "required": required, "additionalProperties": False}
    catalog = ["domain", "profile_id"]
    chat = ["prompt", "domain", "model", "image", "max_tokens", "timeout_s", "profile_id"]
    decisions = ["profile_id", "state", "questions", "image", "timeout_s"]
    return [
        {
            "id": "model_catalog", "description": "只读已授权模型目录：方向、候选、历史核验日期；不发生成请求",
            "params": catalog, "input_schema": schema(catalog, []),
            "capability_tags": ["model", "llm", "tool_discovery"],
        },
        {
            "id": "model_chat", "description": "调用已授权模型进行问答、代码、分析或识图；返回本次模型、结束原因和用量，不更换主任务模型",
            "params": chat, "input_schema": schema(chat, ["prompt"]),
            "capability_tags": ["model", "llm", "chat", "code", "vision"],
        },
        {
            "id": "model_decisions",
            "description": "按次调用私有 profile 的 Decisions 模型，返回 choice/noul/score/box 结构化答案与实际用量；不是生成文本或新成员，不切默认模型",
            "params": decisions, "input_schema": schema(decisions, ["profile_id", "state", "questions"]),
            "capability_tags": ["model", "decisions", "vision"],
        },
    ]


def _credential(value):
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    value = str(value).strip()
    if not value:
        return None
    resolved = model_registry._resolve_alias(value)
    if not resolved or resolved.startswith("appid-") or any(c.isspace() for c in resolved):
        return None
    return resolved


def _select(direction, *, model=None):
    try:
        config = model_registry._load_models()
    except model_registry.ModelCatalogError:
        raise MediaError("media_model_catalog_unavailable") from None
    spec = config.get(direction)
    if not isinstance(spec, dict) or not spec.get("candidates"):
        raise MediaError("media_direction_unavailable")
    exhausted = {_credential(a) for a in
                 (config.get("quota_exhausted_appids") or {}).get("appids", [])}
    broken = {m.get("model") if isinstance(m, dict) else m for m in
              (config.get("known_broken_models") or {}).get("models", [])}
    for candidate in spec["candidates"]:
        candidate_model = candidate.get("model")
        status = str(candidate.get("status") or "")
        if (not isinstance(candidate_model, str) or not candidate_model.strip()
                or candidate_model in broken or (model is not None and candidate_model != model)):
            continue
        if status and not status.startswith("PASS"):
            continue
        # 显式空授权不可被方向授权覆盖；只有字段缺省才继承方向授权。
        appids = candidate.get("appids", spec.get("appids", [])) or []
        if not isinstance(appids, list):
            continue
        for alias in appids:
            appid = _credential(alias)
            if appid and appid not in exhausted:
                return candidate_model, appid
    raise MediaError("media_authorized_model_unavailable")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 带授权的网关请求不跟随跳转，避免凭证被转发给另一个主机。
        return None


def _public_text(value, *, secret="", limit=512):
    if (not isinstance(value, str) or not value or len(value) > limit
            or not value.isprintable() or value != value.strip()
            or (secret and secret in value)):
        return None
    return value


def _amount(value):
    if type(value) not in (int, float) or value < 0:
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def _reported_cost(data):
    value = _amount(data.get("cost"))
    if value is not None:
        return value, "response.cost"
    usage = data.get("usage")
    value = _amount(usage.get("cost")) if isinstance(usage, dict) else None
    return value, "usage.cost" if value is not None else None


def _request(direction, endpoint, payload, timeout_s, *, model=None, profile=None):
    if profile is not None:
        # These three values come from ONE validated private snapshot. Never
        # mix its endpoint/model with the company catalog or an ambient key.
        model, gateway, appid = profile["model"], profile["base_url"], profile["api_key"]
    else:
        model, appid = _select(direction) if model is None else _select(direction, model=model)
        gateway = os.environ.get("MEYO_MEDIA_GATEWAY",
                                os.environ.get("MEYO_UNIFIED_GATEWAY") or "https://aigc.sankuai.com/v1/openai/native")
    try:
        parsed = urllib.parse.urlsplit(gateway)
        if "MEYO_MEDIA_GATEWAY" in os.environ:
            port = parsed.port
            if (parsed.query or parsed.fragment or "\\" in gateway
                    or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in gateway)
                    or (port is not None and not 1 <= port <= 65535)):
                raise ValueError
    except ValueError:
        raise MediaError("media_gateway_invalid") from None
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise MediaError("media_gateway_invalid")
    body = json.dumps({**payload, "model": model}).encode("utf-8")
    if endpoint == "/decisions" and len(body) > _DECISIONS_BODY_LIMIT:
        raise MediaError("media_decisions_request_too_large")
    request = urllib.request.Request(
        gateway.rstrip("/") + endpoint,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + appid})
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout_s) as response:
        if endpoint == "/decisions":
            raw = response.read(_DECISIONS_JSON_LIMIT + 1)
            if len(raw) > _DECISIONS_JSON_LIMIT:
                raise MediaError("media_decisions_response_too_large")
            data = json.loads(raw)
        elif endpoint == "/images/generations":
            from native_capability_host import _MAX_IMAGE_BYTES
            # One bounded base64 image plus 64 KiB for the JSON envelope.
            limit = 4 * ((_MAX_IMAGE_BYTES + 2) // 3) + 64 * 1024
            raw = response.read(limit + 1)
            if len(raw) > limit:
                raise MediaError("media_output_limit")
            data = json.loads(raw)
        else:
            data = json.load(response)
        header_id = getattr(response, "headers", {}).get("x-request-id")
    if not isinstance(data, dict) or (data.get("error") and endpoint != "/decisions"):
        raise MediaError("media_gateway_error")
    # 由本次实际选择写入，不能由上游响应伪造，也不二次读取可能已变化的目录。
    request_id = next((value for raw in (data.get("request_id"), data.get("id"), header_id)
                       if (value := _public_text(raw, secret=appid)) is not None), None)
    return {**data, "_configured_model": _public_text(model, secret=appid),
            "model": _public_text(data.get("model"), secret=appid),
            "provider": _public_text(data.get("provider"), secret=appid),
            "_request_id": request_id}


def _validate_png(content):
    """最小 PNG 格式检查，不解压像素：签名、完整 IHDR、合法参数及头部 CRC。"""
    if (len(content) < 33 or content[:8] != b"\x89PNG\r\n\x1a\n"
            or content[8:16] != b"\x00\x00\x00\rIHDR"):
        raise MediaError("media_image_invalid_png")
    width, height, depth, color, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", content[16:29])
    depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
    if (not 0 < width < 2**31 or not 0 < height < 2**31
            or depth not in depths.get(color, ()) or compression != 0
            or filtering != 0 or interlace not in (0, 1)
            or zlib.crc32(content[12:29]) != int.from_bytes(content[29:33], "big")):
        raise MediaError("media_image_invalid_png")
    # 只有头部的截断数据不算图片；至少有非空 IDAT 和完整 IEND。
    offset, has_pixels = 33, False
    while offset + 12 <= len(content):
        size = int.from_bytes(content[offset:offset + 4], "big")
        kind = content[offset + 4:offset + 8]
        end = offset + size + 12
        if end > len(content):
            break
        if kind == b"IDAT" and size:
            has_pixels = True
        if kind == b"IEND":
            if size == 0 and has_pixels:
                return
            break
        offset = end
    raise MediaError("media_image_invalid_png")


def _image_policy(environment):
    from agent_tool_provider import _json_env, _narrow_policy
    try:
        return _narrow_policy(_json_env(environment, "MEYO_CAPABILITY_POLICY_JSON"), {})
    except (ValueError, TypeError):
        raise MediaError("media_image_policy_invalid") from None


def _image_workspace(environment):
    from personal_runtime import model_tool_workspace
    if "MEYO_WORKDIR" not in environment and "MEYO_MODEL_WORKSPACE_ID" not in environment:
        return None  # Explicit operator path, never an ambient workspace grant.
    try:
        return model_tool_workspace(environment, require_bound=True)
    except (OSError, ValueError, TypeError, RuntimeError):
        raise MediaError("media_image_workspace_invalid") from None


def validate_image_generation(environment):
    """Read-only preflight using only the caller's trusted environment."""
    from legion_permissions import authorize_tool
    policy = _image_policy(environment)
    if not authorize_tool("gen_image", {}, policy)["allowed"]:
        raise MediaError("media_image_billable_denied")
    for field in ("network", "write"):
        if not policy[field]:
            raise MediaError("media_image_" + field + "_denied")
    return _image_workspace(environment)


@contextmanager
def _image_parent(value, environment, workspace):
    """Pin the directory chain; recheck names against the held no-follow FDs."""
    path = Path(value)
    if not path.is_absolute():
        if workspace is None:
            raise MediaError("media_image_path_invalid")
        path = workspace / path
    if not path.name or ".." in path.parts:
        raise MediaError("media_image_path_invalid")
    expected = None
    if workspace is not None:
        try:
            parts = path.relative_to(workspace).parts
        except ValueError:
            raise MediaError("media_image_path_invalid") from None
        if (not parts or parts[-1].startswith(".")
                or any(part.casefold() in _IMAGE_PRIVATE_DIRS for part in parts[:-1])):
            raise MediaError("media_image_path_invalid")
        info = workspace.stat()
        expected = (json.loads(environment["MEYO_MODEL_WORKSPACE_ID"])
                    if "MEYO_MODEL_WORKSPACE_ID" in environment
                    else [info.st_dev, info.st_ino])
    fds, chain = [], []
    try:
        fds.append(os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        for index, part in enumerate(path.parts[1:-1], 1):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1])
            chain.append((fds[-1], part, child, index))
            fds.append(child)
        parent = fds[-1]

        def check():
            for ancestor, name, child, index in chain:
                named = os.stat(name, dir_fd=ancestor, follow_symlinks=False)
                actual = os.fstat(child)
                if (not stat.S_ISDIR(named.st_mode)
                        or (named.st_dev, named.st_ino) != (actual.st_dev, actual.st_ino)):
                    raise MediaError("media_image_unavailable")
                if workspace is not None and index >= len(workspace.parts) - 1:
                    if actual.st_uid != os.getuid() or actual.st_mode & 0o022:
                        raise MediaError("media_image_unavailable")
                    if (index == len(workspace.parts) - 1
                            and expected != [actual.st_dev, actual.st_ino]):
                        raise MediaError("media_image_workspace_invalid")
            info = os.fstat(parent)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise MediaError("media_image_unavailable")

        check()
        yield path, parent, check
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _image_file_stamp(info):
    return tuple(getattr(info, field) for field in (
        "st_dev", "st_ino", "st_mode", "st_uid", "st_nlink",
        "st_size", "st_mtime_ns", "st_ctime_ns",
    ))


def _image_facts(path, raw):
    return {"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def image_file_facts(path, environment):
    """Read one authorized PNG snapshot; generation permission does not grant read."""
    from legion_permissions import authorize_tool
    from native_capability_host import _MAX_IMAGE_BYTES, _read_regular_at
    if not authorize_tool("see_image", {}, _image_policy(environment))["allowed"]:
        raise MediaError("media_image_read_denied")
    workspace = _image_workspace(environment)
    try:
        with _image_parent(path, environment, workspace) as (path, parent, check):
            before = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            raw = _read_regular_at(parent, path.name, _MAX_IMAGE_BYTES)
            check()
            after = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if _image_file_stamp(before) != _image_file_stamp(after):
                raise MediaError("media_image_unavailable")
            _validate_png(raw)
            return _image_facts(path, raw)
    except MediaError:
        raise
    except (OSError, ValueError, TypeError, RuntimeError):
        raise MediaError("media_image_unavailable") from None


def _generated_image_bytes(data, timeout_s, limit):
    item = (data.get("data") or [{}])[0]
    if item.get("b64_json"):
        if not isinstance(item["b64_json"], str):
            raise MediaError("media_image_invalid_png")
        if len(item["b64_json"]) > 4 * ((limit + 2) // 3):
            raise MediaError("media_output_limit")
        content = base64.b64decode(item["b64_json"], validate=True)
    elif item.get("url"):
        url = item["url"]
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise MediaError("media_image_url_invalid")
        # 独立的无授权请求；不用网关 opener/headers，也不用无超时的 urlretrieve。
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout_s) as response:
            content = response.read(limit + 1)
    else:
        raise MediaError("media_image_missing")
    if len(content) > limit:
        raise MediaError("media_output_limit")
    if not content:
        raise MediaError("media_image_empty")
    _validate_png(content)
    return content


def _image(prompt, out, timeout_s, *, as_json=False):
    from native_capability_host import _MAX_IMAGE_BYTES
    request_started = False
    try:
        if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or timeout_s <= 0:
            raise MediaError("media_timeout_invalid")
        environment = dict(os.environ)
        workspace = validate_image_generation(environment)
        # Only the legacy CLI may interpret an operator's explicit relative --out.
        target = Path(out)
        if workspace is None and not target.is_absolute():
            target = Path.cwd() / target
        with _image_parent(target, environment, workspace) as (path, parent, check):
            fd = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=parent)
            created, complete = None, False
            try:
                created = os.fstat(fd)
                os.fchmod(fd, 0o600)
                reserved = os.fstat(fd)
                check()
                if (not stat.S_ISREG(reserved.st_mode) or reserved.st_uid != os.getuid()
                        or stat.S_IMODE(reserved.st_mode) != 0o600
                        or reserved.st_nlink != 1 or reserved.st_size != 0
                        or _image_file_stamp(os.stat(path.name, dir_fd=parent, follow_symlinks=False))
                        != _image_file_stamp(reserved)):
                    raise MediaError("media_image_unavailable")
                # Entering _request is a conservative attempt boundary, not proof
                # of an HTTP send, provider receipt, or a charge.
                request_started = True
                data = _request("image_generation", "/images/generations",
                                {"prompt": prompt, "n": 1, "size": "1024x1024"}, timeout_s)
                content = _generated_image_bytes(data, timeout_s, _MAX_IMAGE_BYTES)
                check()
                if (_image_file_stamp(os.fstat(fd)) != _image_file_stamp(reserved)
                        or _image_file_stamp(os.stat(path.name, dir_fd=parent, follow_symlinks=False))
                        != _image_file_stamp(reserved)):
                    raise MediaError("media_image_unavailable")
                # The new file is read back through the SAME FD even when the
                # caller's separate authority to read existing files is denied.
                with os.fdopen(fd, "r+b", closefd=False) as stream:
                    if stream.write(content) != len(content):
                        raise MediaError("media_image_unavailable")
                    stream.flush()
                    os.fsync(fd)
                    written = os.fstat(fd)
                    stream.seek(0)
                    raw = stream.read(_MAX_IMAGE_BYTES + 1)
                    # Identity, owner, mode and link count cannot change with our write.
                    if (raw != content
                            or _image_file_stamp(written)[:5] != _image_file_stamp(reserved)[:5]
                            or _image_file_stamp(os.fstat(fd)) != _image_file_stamp(written)):
                        raise MediaError("media_image_unavailable")
                os.fsync(parent)
                check()
                if (_image_file_stamp(os.stat(path.name, dir_fd=parent, follow_symlinks=False))
                        != _image_file_stamp(written)):
                    raise MediaError("media_image_unavailable")
                result = _image_facts(path, raw)
                complete = True
            finally:
                try:
                    if not complete and created is not None:
                        # Never resolve a replaced parent or delete a replacement
                        # file. Do not search for a reservation moved elsewhere.
                        try:
                            named = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                            if (named.st_dev, named.st_ino) == (created.st_dev, created.st_ino):
                                os.unlink(path.name, dir_fd=parent)
                        except OSError:
                            pass
                finally:
                    os.close(fd)
        if as_json:
            print(json.dumps({"ok": True, **result, "request_started": True}))
        else:
            print(out)
    except MediaError as exc:
        raise MediaError(str(exc), request_started=request_started) from None
    except urllib.error.HTTPError as exc:
        raise MediaError(f"media_http_error:{exc.code}", request_started=request_started) from None
    except Exception:
        raise MediaError("media_image_unavailable", request_started=request_started) from None


def _usage(value):
    """用量只保留协议计数，不公开上游任意元数据或内部思考正文。"""
    if not isinstance(value, dict):
        return None
    allowed = {"prompt_tokens", "completion_tokens", "total_tokens", "input_tokens",
               "output_tokens", "cached_tokens", "reasoning_tokens",
               "cache_read_input_tokens", "cache_creation_input_tokens"}
    result = {key: val for key, val in value.items()
              if key in allowed and type(val) is int and 0 <= val <= 2**63 - 1}
    for key in ("prompt_tokens_details", "completion_tokens_details",
                "input_tokens_details", "output_tokens_details"):
        if isinstance(value.get(key), dict):
            result[key] = {k: v for k, v in value[key].items()
                           if k in allowed and type(v) is int and 0 <= v <= 2**63 - 1}
    cost = _amount(value.get("cost"))
    if cost is not None:
        result["cost"] = cost
    return result


def _checked_image_bytes(image):
    """Read once through bounded no-follow FDs; encode exactly the validated bytes."""
    from personal_runtime import model_tool_workspace
    from native_capability_host import _MAX_IMAGE_BYTES, _read_regular_at

    # Billable permission does not grant access to workspace files. The parent
    # policy survives the existing tool thread and child environment unchanged.
    if "MEYO_CAPABILITY_POLICY_JSON" in os.environ:
        from agent_tool_provider import _json_env, _narrow_policy
        from legion_permissions import authorize_tool
        try:
            policy = _narrow_policy(_json_env(os.environ, "MEYO_CAPABILITY_POLICY_JSON"), {})
        except (ValueError, TypeError):
            raise MediaError("media_image_policy_invalid") from None
        if not authorize_tool("see_image", {}, policy)["allowed"]:
            raise MediaError("media_image_read_denied")
    try:
        workspace = model_tool_workspace(os.environ)
        path = Path(image)
        path = path if path.is_absolute() else workspace / path
        parts = path.relative_to(workspace).parts
        # Hidden artifact directories are normal workspaces for screenshots.
        # Reject private trees and hidden files, not every dot-prefixed parent.
        if (not parts or ".." in parts or parts[-1].startswith(".")
                or any(part.casefold() in _IMAGE_PRIVATE_DIRS for part in parts[:-1])):
            raise MediaError("media_image_path_invalid")
        mime = _IMAGE_FORMATS.get(path.suffix.lower())
        if mime is None:
            raise MediaError("media_image_type_unsupported")
        before = workspace.stat()
        expected = (json.loads(os.environ["MEYO_MODEL_WORKSPACE_ID"])
                    if "MEYO_MODEL_WORKSPACE_ID" in os.environ
                    else [before.st_dev, before.st_ino])
        parent = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            actual = os.fstat(parent)
            if (expected != [actual.st_dev, actual.st_ino]
                    or actual.st_uid != os.getuid() or actual.st_mode & 0o022):
                raise ValueError()
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = child
                info = os.fstat(parent)
                if info.st_uid != os.getuid() or info.st_mode & 0o022:
                    raise ValueError()
            raw = _read_regular_at(parent, parts[-1], _MAX_IMAGE_BYTES)
        finally:
            os.close(parent)
        # Keep the suffix allowlist above, but select the validator and MIME
        # from these same bounded bytes when an image has been misnamed.
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "png"
        elif raw.startswith(b"\xff\xd8\xff"):
            mime = "jpeg"
        elif raw[:6] in (b"GIF87a", b"GIF89a"):
            mime = "gif"
        elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            mime = "webp"
        if mime == "png":
            _validate_png(raw)
        elif mime == "jpeg":
            if not raw.startswith(b"\xff\xd8\xff") or not raw.endswith(b"\xff\xd9"):
                raise ValueError()
        elif mime == "gif":
            if len(raw) < 14 or raw[:6] not in (b"GIF87a", b"GIF89a") or raw[-1:] != b";":
                raise ValueError()
        elif (len(raw) < 20 or raw[:4] != b"RIFF" or raw[8:12] != b"WEBP"
              or int.from_bytes(raw[4:8], "little") + 8 != len(raw)):
            raise ValueError()
        return mime, raw
    except MediaError:
        raise
    except (OSError, ValueError, TypeError):
        raise MediaError("media_image_unavailable") from None


def _chat_result(question, domain, image, timeout_s, *, model=None, max_tokens=4000,
                 profile=None, workspace_bound=False):
    if type(max_tokens) is not int or max_tokens <= 0:
        raise MediaError("media_budget_invalid")
    if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or timeout_s <= 0:
        raise MediaError("media_timeout_invalid")
    if not isinstance(question, str) or not question.strip():
        raise MediaError("media_tool_parameters_invalid")
    direction = domain or ("vision_understanding" if image else "general_chat_high_coverage")
    if image and direction != "vision_understanding":
        raise MediaError("media_image_requires_vision_direction")
    if direction in ("image_generation", "video_generation"):
        raise MediaError("media_chat_direction_invalid")
    content = []
    input_image = None
    if image:
        if profile is not None and "input_modalities" in profile and "image" not in profile["input_modalities"]:
            raise MediaError("media_profile_image_not_declared")
        if workspace_bound or profile is not None:
            mime, raw = _checked_image_bytes(image)
        else:
            # Preserve the existing operator CLI contract when no task workspace
            # is bound. Structured shared/native calls never use this branch.
            mime = _IMAGE_FORMATS.get(Path(image).suffix.lower())
            if mime is None:
                raise MediaError("media_image_type_unsupported")
            raw = Path(image).read_bytes()
        input_image = {"sha256": hashlib.sha256(raw).hexdigest(),
                       "bytes": len(raw), "mime_type": f"image/{mime}"}
        encoded = base64.b64encode(raw).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{encoded}"}})
    content.append({"type": "text", "text": question})
    selection = {"model": model} if model is not None else {}
    if profile is not None:
        selection["profile"] = profile
    data = _request(direction, "/chat/completions",
                    {"messages": [{"role": "user", "content": content}], "max_tokens": max_tokens},
                    timeout_s, **selection)
    choice = (data.get("choices") or [{}])[0]
    answer = (choice.get("message") or {}).get("content")
    reason = choice.get("finish_reason")
    valid_reason = reason is None or (
        isinstance(reason, str) and reason in
        ("stop", "length", "tool_calls", "function_call", "content_filter"))
    cost, cost_source = _reported_cost(data)
    result = {
        "ok": True, "configured_model": _public_text(data.get("_configured_model", model)),
        "reported_model": _public_text(data.get("model")),
        "provider": _public_text(data.get("provider")),
        "request_id": _public_text(data.get("_request_id")),
        "cost": cost, "cost_source": cost_source,
        "usage": _usage(data.get("usage")), "usage_scope": "single_model_tool_request",
        "finish_reason": reason if valid_reason else None, "max_tokens": max_tokens,
        "input_image": input_image,
    }
    if not valid_reason:
        return {**result, "ok": False, "isError": True, "error": "media_finish_reason_invalid"}
    if reason == "length":
        return {**result, "ok": False, "isError": True, "error": "media_output_limit"}
    if not isinstance(answer, str) or not answer.strip():
        return {**result, "ok": False, "isError": True, "error": "media_chat_empty_response"}
    return {**result, "answer": answer}


def _chat(question, domain, image, timeout_s, *, model=None, as_json=False, max_tokens=4000):
    result = _chat_result(question, domain, image, timeout_s, model=model, max_tokens=max_tokens,
                          workspace_bound="MEYO_WORKDIR" in os.environ)
    if not result["ok"]:
        if as_json:
            print(json.dumps(result, ensure_ascii=False))
        raise MediaError(result["error"])
    if as_json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(result["answer"])


def _decision_json(value, *, depth=0, secret=""):
    """Validate JSON data, not a second provider request or an inline media reader."""
    if depth > 32:
        raise MediaError("media_decisions_parameters_invalid")
    if isinstance(value, str):
        if secret and secret in value:
            raise MediaError("media_decisions_parameters_invalid")
        if value.lstrip().lower().startswith(("data:image/", "data:audio/", "data:video/")):
            raise MediaError("media_decisions_raw_media_forbidden")
    elif isinstance(value, dict):
        if any(not isinstance(key, str) or (secret and secret in key) for key in value):
            raise MediaError("media_decisions_parameters_invalid")
        kind = value.get("type")
        if (_DECISIONS_MEDIA_KEYS.intersection(value)
                or (isinstance(kind, str) and kind in _DECISIONS_MEDIA_TYPES)):
            raise MediaError("media_decisions_raw_media_forbidden")
        for item in value.values():
            _decision_json(item, depth=depth + 1, secret=secret)
    elif isinstance(value, list):
        for item in value:
            _decision_json(item, depth=depth + 1, secret=secret)
    elif value is not None and type(value) is not bool and not _decision_number(value):
        raise MediaError("media_decisions_parameters_invalid")


def _decision_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _decision_content(value, *, nullable=False, secret=""):
    if value is None and nullable:
        return
    if not isinstance(value, (str, dict, list)):
        raise MediaError("media_decisions_parameters_invalid")
    _decision_json(value, secret=secret)


def _validate_decisions(params):
    _decision_content(params.get("state"))
    questions = params.get("questions")
    if not isinstance(questions, dict) or not questions:
        raise MediaError("media_decisions_parameters_invalid")
    for name, question in questions.items():
        if (not isinstance(name, str) or not name or not isinstance(question, dict)
                or not {"type", "instructions"} <= question.keys()):
            raise MediaError("media_decisions_parameters_invalid")
        kind = question["type"]
        if (kind not in ("choice", "noul", "score", "box")
                or set(question) - {"type", "instructions", "criteria"}
                or (kind == "box" and "criteria" in question)):
            raise MediaError("media_decisions_parameters_invalid")
        _decision_content(question["instructions"])
        criteria = question.get("criteria")
        if kind in ("choice", "noul") and (kind == "choice" or "criteria" in question):
            if (not isinstance(criteria, dict) or not criteria
                    or any(not isinstance(key, str) or not key for key in criteria)
                    or (kind == "noul" and set(criteria) != {"true", "false"})):
                raise MediaError("media_decisions_parameters_invalid")
            for value in criteria.values():
                _decision_content(value, nullable=kind == "choice")
        elif kind == "score":
            if not isinstance(criteria, list) or not criteria:
                raise MediaError("media_decisions_parameters_invalid")
            for value in criteria:
                _decision_content(value)
    try:
        raw = json.dumps(params, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise MediaError("media_decisions_parameters_invalid") from None
    if len(raw) > _DECISIONS_JSON_LIMIT:
        raise MediaError("media_decisions_request_too_large")


def _decision_image(image):
    # Do not reopen the path: all protocol checks and encoding use this ONE read.
    mime, raw = _checked_image_bytes(image)
    if mime == "png":
        width, height = struct.unpack(">II", raw[16:24])
        offset = 8
        while offset + 12 <= len(raw):
            size = int.from_bytes(raw[offset:offset + 4], "big")
            if raw[offset + 4:offset + 8] == b"acTL":
                raise MediaError("media_decisions_image_animated")
            offset += size + 12
    elif mime == "jpeg":
        # Walk bounded JPEG segments to the first scan; no package, path read,
        # EXIF rotation or silent resizing. This is not a full entropy decoder.
        offset, dimensions, valid_scan = 2, None, False
        while offset + 4 < len(raw):
            if raw[offset] != 0xFF:
                break
            while offset < len(raw) and raw[offset] == 0xFF:
                offset += 1
            if offset + 3 > len(raw):
                break
            marker = raw[offset]
            offset += 1
            size = int.from_bytes(raw[offset:offset + 2], "big")
            end = offset + size
            if size < 2 or end > len(raw) - 2:
                break
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                if size < 8 or size != 8 + 3 * raw[offset + 7]:
                    break
                height, width = struct.unpack(">HH", raw[offset + 3:offset + 7])
                dimensions = (width, height)
            if marker == 0xDA:
                valid_scan = (dimensions is not None and size >= 8
                              and 1 <= raw[offset + 2] <= 4
                              and size == 6 + 2 * raw[offset + 2]
                              and end < len(raw) - 2)
                break
            offset = end
        if not valid_scan:
            raise MediaError("media_decisions_image_invalid")
        width, height = dimensions
    else:
        raise MediaError("media_decisions_image_type_unsupported")
    if not (256 <= width <= 1568 and 256 <= height <= 1568
            and 78400 <= width * height <= 2458624):
        raise MediaError("media_decisions_image_size_invalid")
    encoded = base64.b64encode(raw).decode("ascii")
    if mime == "png":
        from native_capability_host import _validated_screenshot
        try:
            _validated_screenshot(encoded, "png")  # Existing bounded pixel/CRC check, same bytes.
        except ValueError:
            raise MediaError("media_decisions_image_invalid") from None
    return {"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{encoded}"}}


def _decision_answers(answers, questions, secret):
    if not isinstance(answers, dict) or answers.keys() != questions.keys():
        raise ValueError()
    result = {}
    for name, question in questions.items():
        answer, kind = answers[name], question["type"]
        if not isinstance(answer, dict) or answer.get("type") != kind or (secret and secret in name):
            raise ValueError()
        value = answer.get(kind)
        if kind == "choice":
            if not isinstance(value, str) or value not in question["criteria"] or (secret and secret in value):
                raise ValueError()
        elif kind == "box":
            coordinates = {"top", "bottom", "left", "right"}
            if (not isinstance(value, dict) or not coordinates <= value.keys()
                    or any(not _decision_number(value[key]) for key in coordinates)):
                raise ValueError()
            value = {key: value[key] for key in ("top", "bottom", "left", "right")}
        elif not _decision_number(value):
            raise ValueError()
        row = {"type": kind, kind: value}
        if "confidence" in answer:
            if not _decision_number(answer["confidence"]):
                raise ValueError()
            row["confidence"] = answer["confidence"]
        if "probabilities" in answer:
            probabilities = answer["probabilities"]
            if (not isinstance(probabilities, dict)
                    or any(not isinstance(key, str) or (secret and secret in key)
                           or not _decision_number(val) for key, val in probabilities.items())):
                raise ValueError()
            row["probabilities"] = probabilities
        if kind == "score" and "legend" in answer:
            legend = answer["legend"]
            if not isinstance(legend, dict):
                raise ValueError()
            for key, item in legend.items():
                if not isinstance(key, str) or (secret and secret in key):
                    raise ValueError()
                _decision_content(item, secret=secret)
            row["legend"] = legend
        result[name] = row
    return result


def _decisions_result(params, profile):
    # An existing chat profile is NOT a Decisions profile. Never rewrite /api/v1.
    endpoint = urllib.parse.urlsplit(profile["base_url"])
    if endpoint.path.rstrip("/") != "/api/alpha" or endpoint.query or endpoint.fragment:
        raise MediaError("media_decisions_endpoint_invalid")
    state = params["state"]
    if params.get("image"):
        if "input_modalities" in profile and "image" not in profile["input_modalities"]:
            raise MediaError("media_profile_image_not_declared")
        part = _decision_image(params["image"])
        state = [*state, part] if isinstance(state, list) else [state, part]
    data = _request("", "/decisions", {"state": state, "questions": params["questions"]},
                    params.get("timeout_s", 90), profile=profile)
    cost, source = _reported_cost(data)
    receipt = {
        "configured_model": data.get("_configured_model"), "reported_model": data.get("model"),
        "provider": data.get("provider"), "request_id": data.get("_request_id"),
        "usage": _usage(data.get("usage")), "usage_scope": "single_model_tool_request",
        "cost": cost, "cost_source": source,
    }
    try:
        usage = data.get("usage")
        if (data.get("error") or not data.get("model") or not isinstance(usage, dict)
                or any(type(usage.get(key)) is not int or not 0 <= usage[key] <= 2**63 - 1
                       for key in ("input_tokens", "output_tokens"))):
            raise ValueError()
        answers = _decision_answers(data.get("answers"), params["questions"], profile["api_key"])
    except (ValueError, TypeError, MediaError):
        return {**receipt, "ok": False, "isError": True, "error": "media_decisions_response_invalid"}
    return {**receipt, "ok": True, "answers": answers}


def _models_result(direction, *, profile=None, profile_id=None):
    if profile is not None:
        return {"ok": True, "source": "personal_profile", "profile_id": profile_id,
                "source_last_verified": None, "live_verified": False, "candidates": [{
                    "model": profile["model"], "base_url": profile["base_url"],
                    "inputs": profile.get("input_modalities"), "credential_present": True,
                    "live_verified": False,
                }]}
    try:
        config = model_registry._load_models()
    except model_registry.ModelCatalogError:
        raise MediaError("media_model_catalog_unavailable") from None
    groups = [direction] if direction else [
        key for key, value in config.items()
        if isinstance(value, dict) and isinstance(value.get("candidates"), list)]
    candidates = []
    for group in groups:
        spec = config.get(group) or {}
        for row in spec.get("candidates") or []:
            if isinstance(row, dict) and isinstance(row.get("model"), str):
                # Raw notes can contain authorization identifiers; project only
                # named capability facts, never credentials or arbitrary metadata.
                candidates.append({"model": row["model"], "direction": group,
                                   "rank": row.get("rank"), "declared_status": row.get("status"),
                                   "inputs": row.get("input"), "live_verified": False})
    return {"ok": True, "source_last_verified": (config.get("_meta") or {}).get("last_verified"),
            "live_verified": False, "candidates": candidates}


def _models(direction):
    print(json.dumps(_models_result(direction), ensure_ascii=False))


def validate_model_tool(tool_id, params):
    spec = next((row for row in model_tool_specs() if row["id"] == tool_id), None)
    if not spec or not isinstance(params, dict) or set(params) - set(spec["params"]):
        raise MediaError("media_tool_parameters_invalid")
    for key in ("prompt", "domain", "model", "image", "profile_id"):
        if key in params and not isinstance(params[key], str):
            raise MediaError("media_tool_parameters_invalid")
    if tool_id == "model_decisions":
        if "profile_id" not in params:
            raise MediaError("media_profile_id_required")
        _validate_decisions(params)
    if tool_id == "model_chat":
        if not params.get("prompt", "").strip():
            raise MediaError("media_tool_parameters_invalid")
        budget = params.get("max_tokens", 4000)
        if type(budget) is not int or budget <= 0:
            raise MediaError("media_budget_invalid")
    if tool_id in ("model_chat", "model_decisions"):
        timeout = params.get("timeout_s", 90)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise MediaError("media_timeout_invalid")
    if "profile_id" in params:
        from personal_runtime import _profile_id
        try:
            _profile_id(params["profile_id"])
        except ValueError:
            raise MediaError("media_profile_id_invalid") from None
    return dict(params)


def run_model_tool(tool_id, params):
    """既有 CLI 的结构化出口；不接受 caller 的 Key、网关或任意请求体。"""
    try:
        params = validate_model_tool(tool_id, params)
        if tool_id == "model_decisions":
            # The operator CLI also obeys the parent's billable denial. Shared
            # and native calls narrow that same policy before spawning this child.
            from agent_tool_provider import _json_env, _narrow_policy
            from legion_permissions import authorize_tool
            try:
                policy = _narrow_policy(_json_env(os.environ, "MEYO_CAPABILITY_POLICY_JSON"), {})
            except (TypeError, ValueError):
                raise MediaError("media_decisions_policy_invalid") from None
            if not authorize_tool(tool_id, {}, policy)["allowed"]:
                raise MediaError("CAPABILITY_DENIED:billable")
        profile = None
        if "profile_id" in params:
            from personal_runtime import resolve_model_profile, ModelProfileError
            try:
                profile = resolve_model_profile(params["profile_id"], environment=os.environ)
            except ModelProfileError as exc:
                raise MediaError(str(exc)) from None
            if "model" in params and params["model"] != profile["model"]:
                raise MediaError("media_profile_model_conflict")
        if tool_id == "model_catalog":
            return _models_result(params.get("domain", ""), profile=profile,
                                  profile_id=params.get("profile_id"))
        if tool_id == "model_decisions":
            return _decisions_result(params, profile)
        return _chat_result(params["prompt"], params.get("domain", ""), params.get("image", ""),
                            params.get("timeout_s", 90), model=params.get("model"),
                            max_tokens=params.get("max_tokens", 4000), profile=profile,
                            workspace_bound=True)
    except MediaError as exc:
        error = str(exc)
    except urllib.error.HTTPError as exc:
        error = f"media_http_error:{exc.code}"
    except Exception as exc:
        # 不复制异常消息；URL、响应正文和授权可能在其中。
        error = f"media_call_error:{type(exc).__name__}"
    return {"ok": False, "isError": True, "error": error}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    image = sub.add_parser("image")
    image.add_argument("prompt")
    image.add_argument("--out", required=True)
    image.add_argument("--timeout", type=float, default=180)
    image.add_argument("--json", action="store_true")
    chat = sub.add_parser("chat")
    chat.add_argument("prompt")
    chat.add_argument("--domain", default="")
    chat.add_argument("--image", default="")
    chat.add_argument("--timeout", type=float, default=90)
    chat.add_argument("--model", default=None)
    chat.add_argument("--json", action="store_true")
    chat.add_argument("--max-tokens", type=int, default=4000)
    models = sub.add_parser("models")
    models.add_argument("--domain", default="")
    tool = sub.add_parser("tool")
    tool.add_argument("name", choices=[row["id"] for row in model_tool_specs()])
    args = parser.parse_args(argv)
    try:
        if args.command == "tool":
            result = run_model_tool(args.name, json.load(sys.stdin))
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result.get("ok") else 1
        if args.command == "models":
            _models(args.domain)
            return 0
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise MediaError("media_timeout_invalid")
        if args.command == "image":
            _image(args.prompt, args.out, args.timeout, as_json=args.json)
        else:
            _chat(args.prompt, args.domain, args.image, args.timeout,
                  model=args.model, as_json=args.json, max_tokens=args.max_tokens)
        return 0
    except MediaError as exc:
        if args.command == "image" and args.json:
            print(json.dumps({"ok": False, "error": str(exc),
                              "request_started": exc.request_started}))
        else:
            print(str(exc), file=sys.stderr)
    except urllib.error.HTTPError as exc:
        print(f"media_http_error:{exc.code}", file=sys.stderr)
    except Exception as exc:
        # 异常文本、HTTP 正文可能回显授权、URL 或用户输入，只报告异常类型。
        print(f"media_call_error:{type(exc).__name__}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
