"""Native MCP access to explicitly granted existing external and desktop capabilities.

This is a trusted single-user host, not a shell endpoint or a quality controller.
The native command sandbox stays in place; the existing capability wrapper keeps
its own ownership checks. No personal HTTP token or model key is needed here.
"""
from __future__ import annotations

from contextlib import contextmanager
import base64
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib


_DEVICE_ENV_KEYS = frozenset({
    "MEYO_DEVICE_DOCKER_HOST", "MEYO_DEVICE_DOCKER_DATA_ROOT",
    "MEYO_DESKTOP_PUBLISH_PORT", "MEYO_DESKTOP_PUBLIC_PATH", "MEYO_DESKTOP_OWNER",
})
_ENV_KEYS = frozenset({
    "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
    "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "LEGION_SESSIONS_DIR", "LEGION_TASK_MEMORY_DIR",
    "LEGION_TEAM_DIR", "LEGION_PROJECT_CONTEXT_DIR", "LEGION_PROC_REGISTRY_DIR",
    "LEGION_EXTERNAL_SOURCE_ROOT", "LEGION_EXTERNAL_RUNTIME_ROOT", "MEYO_ORCH_DIR",
    "LEGION_EXT_AUTO_UPDATE", "PYTHONDONTWRITEBYTECODE", "MEYO_AGENTBAY_CONFIG",
    "MEYO_LOCAL_MODEL_CONFIG", "MEYO_TASK_DEADLINE",
    "MEYO_PERSONAL_STATE_DIR", "LEGION_PERSONAL_OWNER",
}) | _DEVICE_ENV_KEYS
_ID = re.compile(r"external_capability:[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+")
_LOCAL_PREFIX = "local_mcp:cua-local-desktop:"
_MOBILE_PREFIX = "local_mcp:meyo-mobile:"
_MODEL_PREFIX = "local_mcp:meyo-local-model:"
_SHARED_MODEL_IDS = frozenset({"runnable:model_catalog", "runnable:model_chat", "runnable:model_decisions"})
_MOBILE_IDENTITY_FIELDS = frozenset({
    "uid", "user_id", "owner", "session_id", "sid", "sandbox_id", "config", "config_path",
    "MEYO_AGENTBAY_CONFIG", "api_key", "api_key_file", "control_dir", "env", "environment",
})
# Only public device facts, not provider connection/configuration internals.
# Text fields are bounded and sanitized by the existing mobile provider.
_MOBILE_RESULT_FIELDS = frozenset({
    "ok", "provider", "configured", "ready", "phase", "expires_at", "remaining_seconds",
    "remaining_budget_seconds", "session_seconds", "screen_size", "reaper_ready",
    "actions", "supports_app_install", "note", "released", "action", "stdout",
    "error_code", "error",
})
_MAX_REQUEST = 2_000_000
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_MODEL_OUTPUT = 128 * 1024 * 1024
_PROXY_ENV = frozenset({"HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
                        "https_proxy", "http_proxy", "no_proxy"})


def validate_ids(value):
    if (not isinstance(value, list) or len(value) > 64
            or any(not isinstance(item, str) for item in value)
            or len(set(value)) != len(value)):
        raise ValueError("native capabilities require an explicit unique tool-id list")
    local = _local_actions() if any(item.startswith("local_mcp:") for item in value) else {}
    if any(not _ID.fullmatch(item) and item not in local and item not in _SHARED_MODEL_IDS for item in value):
        raise ValueError("native capability is not an existing supported action")
    return list(value)


def _local_actions():
    """Read existing declarations only; never probe or start any device."""
    import cloud_desktop
    actions = {}
    for tool in cloud_desktop.local_mcp_tools():
        schema = copy.deepcopy(tool["input_schema"])
        schema.setdefault("properties", {}).pop("uid", None)
        if "required" in schema:
            schema["required"] = [key for key in schema["required"] if key != "uid"]
        schema["additionalProperties"] = False
        tool_id = _LOCAL_PREFIX + tool["name"]
        actions[tool_id] = {
            "id": tool_id, "name": tool["name"], "server": "cua-local-desktop",
            "description": tool["description"], "input_schema": schema,
            "params": list(schema["properties"]),
        }
    # Static facade declarations, not probes, grants, SDK installs or raw
    # provider tools. The caller's explicit IDs still select the exposed subset.
    for name, description, properties, required in (
        ("mobile_status", "读取已授权云手机状态及剩余使用时长，不自动启动手机。",
         {"force": {"type": "boolean"}}, []),
        ("mobile_start", "按当前用户已配置的额度启动或复用云手机，不接受身份或配置覆盖。",
         {"timeout": {"type": "integer"}}, []),
        ("mobile_screenshot", "读取当前用户云手机截图，校验 PNG 后返回原生图片及工作目录文件。",
         {}, []),
        ("mobile_action", "通过现有手机接口执行原始动作，不执行自然语言任务或更换设备身份。",
         {"name": {"type": "string"}, "params_json": {
             "type": "string",
             "description": '所选动作参数的 JSON 对象字符串；如 tap 使用 {"x":100,"y":200}，'
                            '无参数动作使用 "{}"。不得包含 uid、sandbox_id、凭据或配置路径。',
         }}, ["name", "params_json"]),
        ("mobile_stop", "释放当前用户的云手机，保留服务返回的真实释放状态。",
         {}, []),
    ):
        tool_id = _MOBILE_PREFIX + name
        actions[tool_id] = {
            "id": tool_id, "name": name, "server": "meyo-mobile", "description": description,
            "input_schema": {"type": "object", "properties": properties,
                             "required": required, "additionalProperties": False},
            "params": list(properties),
        }
    for name, description, properties, required in (
        ("preflight", "只检查已配置的本地运行环境；不加载模型、不生成作品、不安装或下载。",
         {"kind": {"type": "string", "enum": ["coding", "shape"]}}, ["kind"]),
        ("coding_text", "让已配置的本地模型生成代码文本，不执行生成的代码。输入为工作目录内的"
         "UTF-8提示文件，输出不覆盖；需由作者自行验证和采用，通道成功不是质量验收。",
         {"prompt_file": {"type": "string"}, "output_file": {"type": "string"},
          "max_tokens": {"type": "integer", "minimum": 1, "maximum": 32768},
          "context_tokens": {"type": "integer", "minimum": 1, "maximum": 32768},
          "thinking": {"type": "string", "enum": ["on", "off"]}},
         ["prompt_file", "output_file"]),
        ("shape_3d", "使用已配置的本地 Python 外形模型，将工作目录内的授权 PNG 转为 GLB。"
         "只生成外形，不代表 PBR、骨骼、动画或视觉质量通过；共享生成锁忙时不另开任务。"
         "当前 Python 预处理按转 RGBA 后的非零 alpha 定义前景，不自动分离背景。"
         "回包及 receipt 的 input 包含原 PNG 路径、SHA-256、尺寸、alpha 来源、是否含全透明像素"
         "及非零 alpha 包围框（像素坐标左上包含、右下不包含，全透明时为 null）。"
         "这些是原 PNG 样本事实，不是预处理后的裁剪结果或质量评价。",
         {"image_file": {"type": "string"}, "output_file": {"type": "string"},
          "seed": {"type": "integer", "minimum": 0, "maximum": 4294967295}},
         ["image_file", "output_file"]),
    ):
        tool_id = _MODEL_PREFIX + name
        actions[tool_id] = {
            "id": tool_id, "name": name, "server": "meyo-local-model",
            "description": description,
            "input_schema": {"type": "object", "properties": properties,
                             "required": required, "additionalProperties": False},
            "params": list(properties),
        }
    return actions


def declared_capability_actions():
    """The same declaration source for explicit grants and the native host."""
    import external_agent_registry
    import media_model_cli
    shared = [{**row, "id": "runnable:" + row["id"], "name": row["id"]}
              for row in media_model_cli.model_tool_specs()]
    actions = [*external_agent_registry.capability_actions()["actions"],
               *_local_actions().values(), *shared]
    return {"actions": actions, "count": len(actions)}


def _device_environment(user_id, environment):
    if any(not isinstance(environment.get(key), str) or not environment[key]
           for key in _DEVICE_ENV_KEYS):
        raise ValueError("local device capabilities need complete parent device configuration")
    import cloud_desktop
    if cloud_desktop._desktop_config_for(user_id, environment) is None:
        raise ValueError("local device capabilities have no bound owner")


def _mobile_environment(user_id, environment):
    """Reuse the provider's owner/config validation without reading any API key."""
    import mobile_device
    if not mobile_device.cloud_configured(environment=environment):
        raise ValueError("native mobile capabilities require private cloud configuration")
    mobile_device.authorize_cloud_owner(user_id, environment=environment)


def _local_model_config(environment, workspace):
    """Only a trusted parent selects executables, HOME, state and call budgets."""
    raw = environment.get("MEYO_LOCAL_MODEL_CONFIG") or ""
    if not isinstance(raw, str) or len(raw.encode()) > 32_000:
        raise ValueError("invalid parent local model configuration")
    config = json.loads(raw)
    fields = {"version", "python", "coding_script", "shape_script", "home",
              "state_dir", "registry_dir", "timeout_s", "wait_s", "term_grace_s"}
    if (not isinstance(config, dict) or not fields <= config.keys() <= fields | {"executable_dirs"}
            or type(config["version"]) is not int or config["version"] != 1):
        raise ValueError("local models require a complete versioned parent configuration")
    config = dict(config)
    for name in ("python", "coding_script", "shape_script", "home", "state_dir", "registry_dir"):
        value = config[name]
        if not isinstance(value, str) or not Path(value).is_absolute() or ".." in Path(value).parts:
            raise ValueError("local model parent paths must be absolute")
        path = Path(value).resolve(strict=True)
        info = path.stat()
        directory = name in ("home", "state_dir", "registry_dir")
        if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
                or path == Path(path.anchor) or path.is_relative_to(workspace)
                or (directory and info.st_uid != os.getuid())):
            raise ValueError("local model implementation and state must be outside the task workspace")
        config[name] = str(path)
    if not os.access(config["python"], os.X_OK):
        raise ValueError("parent local model interpreter is not executable")
    # Resolving a package-manager interpreter loses its companion-tool directory.
    # Only the trusted parent may name such directories; never borrow the author
    # or service PATH (which may contain workspace-controlled executables).
    directories = config.get("executable_dirs", [])
    if not isinstance(directories, list):
        raise ValueError("parent local model executable directories must be a list")
    resolved = []
    for value in directories:
        if (not isinstance(value, str) or not Path(value).is_absolute()
                or ".." in Path(value).parts or os.pathsep in value):
            raise ValueError("parent local model executable directories must be absolute")
        path = Path(value).resolve(strict=True)
        info = path.stat()
        if (not stat.S_ISDIR(info.st_mode) or path == Path(path.anchor)
                or path.is_relative_to(workspace) or info.st_uid not in (0, os.getuid())
                or os.pathsep in str(path) or info.st_mode & stat.S_IWOTH):
            raise ValueError("local model executable directory is not a trusted host directory")
        resolved.append(str(path))
    if "executable_dirs" in config:
        config["executable_dirs"] = list(dict.fromkeys(resolved))
    for name in ("timeout_s", "wait_s", "term_grace_s"):
        value = config[name]
        if (type(value) not in (int, float) or not math.isfinite(value)
                or (value < 0 if name == "wait_s" else value <= 0)):
            raise ValueError("invalid parent local model time budget")
    return config


def _read_regular_at(parent, name, limit, *, tail=False):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_nlink != 1 or before.st_size <= 0
                or (not tail and before.st_size > limit)):
            raise ValueError("capability data must be an owned bounded non-linked regular file")
        if tail:
            stream.seek(max(0, before.st_size - limit))
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if (len(raw) != (min(before.st_size, limit) if tail else before.st_size)
                or any(getattr(before, key) != getattr(after, key) for key in fields)):
            raise ValueError("capability file changed during readback")
    return raw


def _host_file_bytes(path, limit, *, tail=False):
    """Receipt/output readback never follows a returned symlink, even in parents."""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("wrapper returned a non-absolute path")
    parent = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        return _read_regular_at(parent, path.name, limit, tail=tail)
    finally:
        os.close(parent)


def _wrapper_observation(stdout):
    """Wrappers may log before their final, possibly pretty-printed JSON object."""
    if not isinstance(stdout, str) or len(stdout.encode()) > _MAX_REQUEST:
        raise ValueError("local wrapper response exceeds its transport budget")
    decoder = json.JSONDecoder()
    for match in reversed(list(re.finditer(r"(?m)^\s*\{", stdout))):
        candidate = stdout[match.start():].lstrip()
        try:
            value, end = decoder.raw_decode(candidate)
        except ValueError:
            continue
        if isinstance(value, dict) and not candidate[end:].strip():
            return value
    raise ValueError("local wrapper returned no final structured observation")


def _schema_value(value, schema):
    """Validate the small JSON schema already declared by these local tools."""
    kind = schema.get("type")
    valid = {
        "object": isinstance(value, dict), "array": isinstance(value, list),
        "string": isinstance(value, str), "integer": type(value) is int,
        "number": type(value) in (int, float), "boolean": type(value) is bool,
        "null": value is None,
    }
    if kind is not None and not valid.get(kind, False):
        raise ValueError("device parameter has a different type than its declaration")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("device parameter is outside its declared values")
    if kind in ("integer", "number"):
        if (not math.isfinite(value)
                or ("minimum" in schema and value < schema["minimum"])
                or ("maximum" in schema and value > schema["maximum"])):
            raise ValueError("capability parameter is outside its declared bounds")
    if kind == "object":
        properties = schema.get("properties", {})
        if (not set(schema.get("required", ())) <= value.keys()
                or (schema.get("additionalProperties") is False
                    and not value.keys() <= properties.keys())):
            raise ValueError("device parameters must match the declared schema; no identity overrides")
        for name, field in properties.items():
            if name in value:
                _schema_value(value[name], field)
    elif kind == "array" and "items" in schema:
        for item in value:
            _schema_value(item, schema["items"])


class CapabilityImageResult(dict):
    """Only host-created instances can add a native MCP image content block."""
    def __init__(self, metadata, png):
        super().__init__(metadata)
        self.png = png


def _png_image_info(pixels, width, height, depth, color, passes, transparency, *, check=None):
    """Observe original PNG samples from the already bounded inflation, without rewriting pixels."""
    if check is not None:
        check()
    alpha_source = ("channel" if color in (4, 6) else
                    "tRNS" if transparency is not None else "implicit_opaque")
    info = {"width": width, "height": height, "bit_depth": depth, "color_type": color,
            "alpha_source": alpha_source, "has_fully_transparent_pixels": False,
            "nonzero_alpha_bbox": None}
    if alpha_source == "implicit_opaque":
        info["nonzero_alpha_bbox"] = {
            "left": 0, "top": 0, "right_exclusive": width, "bottom_exclusive": height}
        return info
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    bpp = max(1, (channels * depth + 7) // 8)
    transparent_color = (struct.unpack(">H" if color == 0 else ">HHH", transparency)
                         if color in (0, 2) and transparency is not None else None)
    if transparent_color is not None:
        transparent_color = tuple(value & ((1 << depth) - 1) for value in transparent_color)
    position = 0
    for x0, y0, dx, dy in passes:
        pw = max(0, (width - x0 + dx - 1) // dx)
        if not pw or y0 >= height:
            continue
        size = (pw * channels * depth + 7) // 8
        previous = bytearray(size)
        for y in range(y0, height, dy):
            if check is not None:
                check()
            kind = pixels[position]
            row = bytearray(pixels[position + 1:position + 1 + size])
            position += 1 + size
            if kind:
                # Reuse parent cancellation/deadline at bounded CPU batches, not a new budget.
                for start in range(0, size, 4096):
                    if check is not None:
                        check()
                    for i in range(start, min(start + 4096, size)):
                        left, above = row[i - bpp] if i >= bpp else 0, previous[i]
                        corner = previous[i - bpp] if i >= bpp else 0
                        if kind == 1:
                            predictor = left
                        elif kind == 2:
                            predictor = above
                        elif kind == 3:
                            predictor = (left + above) // 2
                        else:  # PNG Paeth; filter bounds were checked by the existing validator.
                            p = left + above - corner
                            a, b, c = abs(p - left), abs(p - above), abs(p - corner)
                            predictor = left if a <= b and a <= c else above if b <= c else corner
                        row[i] = (row[i] + predictor) & 255
            previous = row
            if depth < 8:
                samples = [(row[i * depth // 8] >> (8 - depth - i * depth % 8)) & ((1 << depth) - 1)
                           for i in range(pw)]
            else:
                samples = row if depth == 8 else struct.unpack(f">{len(row) // 2}H", row)
            if color in (4, 6):
                alphas = samples[channels - 1::channels]
            elif color == 3:
                alphas = (transparency[index] if index < len(transparency) else 255
                          for index in samples)
            else:
                alphas = (tuple(samples[i:i + channels]) != transparent_color
                          for i in range(0, len(samples), channels))
            nonzero = []
            for i, alpha in enumerate(alphas):
                if check is not None and i % 4096 == 0:
                    check()
                if alpha != 0:
                    nonzero.append(i)
            if len(nonzero) < pw:
                info["has_fully_transparent_pixels"] = True
            if nonzero:
                left, right = x0 + nonzero[0] * dx, x0 + nonzero[-1] * dx + 1
                box = info["nonzero_alpha_bbox"]
                info["nonzero_alpha_bbox"] = {
                    "left": min(box["left"], left) if box else left,
                    "top": min(box["top"], y) if box else y,
                    "right_exclusive": max(box["right_exclusive"], right) if box else right,
                    "bottom_exclusive": max(box["bottom_exclusive"], y + 1) if box else y + 1,
                }
    return info


def _validated_screenshot(encoded, image_format, *, image_info=None, check=None):
    if (image_format != "png" or not isinstance(encoded, str)
            or len(encoded) > ((_MAX_IMAGE_BYTES + 2) // 3) * 4):
        raise ValueError("device screenshot format or transport size is invalid")
    try:
        png = base64.b64decode(encoded, validate=True)
        if len(png) > _MAX_IMAGE_BYTES:
            raise ValueError()
        from media_model_cli import _validate_png
        _validate_png(png)
        width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", png[16:29])
        if width > 16384 or height > 16384 or width * height > 16 * 1024 * 1024:
            raise ValueError()
        offset, chunks, palette, transparency = 8, [], None, None
        while offset < len(png):
            length = int.from_bytes(png[offset:offset + 4], "big")
            kind = png[offset + 4:offset + 8]
            end = offset + length + 12
            if (end > len(png)
                    or zlib.crc32(png[offset + 4:end - 4]) != int.from_bytes(png[end - 4:end], "big")):
                raise ValueError()
            if kind == b"IHDR" and offset != 8:
                raise ValueError()
            if kind == b"PLTE":
                if (palette is not None or transparency is not None or chunks or not length or length % 3
                        or length > 768 or color in (0, 4)):
                    raise ValueError()
                palette = length // 3
            if kind == b"tRNS":
                if (transparency is not None or chunks or color not in (0, 2, 3)
                        or (color == 0 and length != 2)
                        or (color == 2 and length != 6)
                        or (color == 3 and (palette is None or not 0 < length <= palette))):
                    raise ValueError()
                transparency = png[offset + 8:end - 4]
            if kind == b"IDAT":
                if color == 3 and (palette is None or palette > 2**depth):
                    raise ValueError()
                chunks.append(png[offset + 8:end - 4])
            if kind == b"IEND" and end != len(png):
                raise ValueError()
            offset = end
        channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
        passes = ((0, 0, 1, 1),) if not interlace else (
            (0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
            (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
        rows = []
        for x, y, dx, dy in passes:
            pw, ph = max(0, (width - x + dx - 1) // dx), max(0, (height - y + dy - 1) // dy)
            if pw and ph:
                rows.extend([1 + (pw * channels * depth + 7) // 8] * ph)
        expected = sum(rows)
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(b"".join(chunks), expected + 1)
        if (len(pixels) != expected or not decoder.eof
                or decoder.unused_data or decoder.unconsumed_tail):
            raise ValueError()
        position = 0
        for size in rows:
            if pixels[position] > 4:
                raise ValueError()
            position += size
        if image_info is not None:
            image_info.update(_png_image_info(
                pixels, width, height, depth, color, passes, transparency, check=check))
        return png
    except (InterruptedError, subprocess.TimeoutExpired):
        raise
    except Exception:
        # Never echo base64, response bodies or device contents in an error.
        raise ValueError("device screenshot is not a complete bounded PNG") from None


def native_stdio_spec(ctx, environment: dict, server_path: Path):
    """Parent-only configuration; never accept these fields as tool arguments."""
    if getattr(ctx, "execution_depth", "") == "quick":
        return None
    ids = validate_ids(json.loads(environment.get("MEYO_NATIVE_CAPABILITY_IDS") or "[]"))
    if not ids:
        return None
    if any(item.startswith(_LOCAL_PREFIX) for item in ids):
        _device_environment(ctx.user_id, environment)
    mobile_granted = any(item.startswith(_MOBILE_PREFIX) for item in ids)
    if mobile_granted:
        _mobile_environment(ctx.user_id, environment)
    workspace = Path(ctx.workdir).resolve(strict=True)
    if (not ctx.user_id or not ctx.session_id or not workspace.is_dir()
            or workspace == Path(workspace.anchor)):
        raise ValueError("native capabilities need a bound owner, session and workspace")
    local_config = (_local_model_config(environment, workspace)
                    if any(item.startswith(_MODEL_PREFIX) for item in ids) else None)
    shared_models = bool(_SHARED_MODEL_IDS.intersection(ids))
    if shared_models:
        from personal_runtime import model_profile_context
        model_profile_context({**environment, "MEYO_USER_ID": ctx.user_id,
                               "MEYO_WORKDIR": str(workspace)})
    server = server_path.resolve(strict=True)
    protected = [Path(__file__).resolve().parent, server.parent.parent]
    protected.extend(Path(environment[name]).resolve(strict=True) for name in
                     ("LEGION_EXTERNAL_SOURCE_ROOT", "LEGION_EXTERNAL_RUNTIME_ROOT")
                     if environment.get(name))
    if any(workspace.is_relative_to(root) or root.is_relative_to(workspace)
           for root in protected):
        raise ValueError("host capability implementation overlaps the task workspace")
    context = {
        "user_id": ctx.user_id, "session_id": ctx.session_id, "workdir": str(workspace),
        "task_facts": ctx.task_facts, "project_context": ctx.project_context,
        "capability_policy": ctx.capability_policy, "allowed_ids": ids,
    }
    if shared_models:
        info = workspace.stat()
        context["workspace_identity"] = [info.st_dev, info.st_ino]
    encoded = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode()) > 64_000:
        raise ValueError("native capability binding exceeds the inline transport budget")
    env = {key: value for key, value in environment.items()
           if key in _ENV_KEYS and key not in _PROXY_ENV}
    if not mobile_granted:
        env.pop("MEYO_AGENTBAY_CONFIG", None)
    if local_config is None:
        env.pop("MEYO_LOCAL_MODEL_CONFIG", None)
    if not shared_models:
        env.pop("MEYO_PERSONAL_STATE_DIR", None)
        env.pop("LEGION_PERSONAL_OWNER", None)
    env["MEYO_NATIVE_HOST_CONTEXT"] = encoded
    return {
        "command": str(Path(sys.executable).resolve()),
        "args": ["-I", str(server), "--shared-capabilities"],
        # Proxy values may contain credentials. Pass selected names, not values
        # in CLI/config text; from_environment removes every unrelated key.
        "env": env, "env_vars": sorted(_PROXY_ENV.intersection(environment)),
        "tool_timeout_sec": (max(120, math.ceil(local_config["timeout_s"]
                                              + local_config["term_grace_s"] + 5))
                             if local_config else 120),
    }


class CapabilitySession:
    def __init__(self, context, *, environment=None):
        if not isinstance(context, dict):
            raise ValueError("missing parent capability binding")
        self.allowed = frozenset(validate_ids(context.get("allowed_ids")))
        workdir = context.get("workdir")
        if not isinstance(workdir, str) or not Path(workdir).is_absolute():
            raise ValueError("capability workspace must be explicitly absolute")
        self.workspace = Path(workdir).resolve(strict=True)
        if (not self.workspace.is_dir() or self.workspace == Path(self.workspace.anchor)
                or not all(isinstance(context.get(key), str) and context[key]
                           for key in ("user_id", "session_id"))):
            raise ValueError("invalid parent capability identity")
        for key in ("task_facts", "project_context", "capability_policy"):
            if not isinstance(context.get(key), dict):
                raise ValueError("invalid parent capability context")
        self.environment = {key: value for key, value in
                            (os.environ if environment is None else environment).items()
                            if key in _ENV_KEYS}
        if any(item.startswith(_MOBILE_PREFIX) for item in self.allowed):
            _mobile_environment(context["user_id"], self.environment)
        self.local_config = (_local_model_config(self.environment, self.workspace)
                             if any(item.startswith(_MODEL_PREFIX) for item in self.allowed) else None)
        self.context = copy.deepcopy(context)
        self._cancellation = threading.local()
        info = self.workspace.stat()
        self._workspace_identity = (info.st_dev, info.st_ino)
        shared_models = bool(self.allowed.intersection(_SHARED_MODEL_IDS))
        if shared_models:
            from personal_runtime import model_profile_context
            identity = context.get("workspace_identity", list(self._workspace_identity))
            model_profile_context({
                **self.environment, "MEYO_USER_ID": context["user_id"],
                "MEYO_WORKDIR": str(self.workspace), "MEYO_MODEL_WORKSPACE_ID": json.dumps(identity),
            })
        else:
            self.environment.pop("MEYO_PERSONAL_STATE_DIR", None)
            self.environment.pop("LEGION_PERSONAL_OWNER", None)
        self.bound = {
            "MEYO_USER_ID": context["user_id"], "MEYO_SESSION_ID": context["session_id"],
            "MEYO_WORKDIR": str(self.workspace),
            "MEYO_TASK_FACTS_JSON": json.dumps(context["task_facts"], ensure_ascii=False),
            "MEYO_PROJECT_CONTEXT_JSON": json.dumps(context["project_context"], ensure_ascii=False),
            "MEYO_CAPABILITY_POLICY_JSON": json.dumps(context["capability_policy"]),
            "MEYO_NATIVE_CAPABILITY_IDS": json.dumps(sorted(self.allowed)),
        }
        for key in ("MEYO_LOCAL_MODEL_CONFIG", "MEYO_TASK_DEADLINE", "LEGION_PROC_REGISTRY_DIR"):
            if key in self.environment:
                self.bound[key] = self.environment[key]
        # Only a caller-supplied namespace; an in-process host must not borrow the operator's instance.
        if environment is not None and "MEYO_LEGION_DIR" in self.environment:
            self.bound["MEYO_LEGION_DIR"] = self.environment["MEYO_LEGION_DIR"]
        if shared_models:
            for key in ("MEYO_PERSONAL_STATE_DIR", "LEGION_PERSONAL_OWNER"):
                self.bound[key] = self.environment[key]
            self.bound["MEYO_MODEL_WORKSPACE_ID"] = json.dumps(self._workspace_identity)
        actions = declared_capability_actions()["actions"]
        self.actions = {row["id"]: row for row in actions if row["id"] in self.allowed}
        self.tools = [
            ("list_capabilities", "List this task's explicitly granted capability actions. "
             "Declarations are not proof of model/runtime availability.",
             {"type": "object", "properties": {}, "additionalProperties": False},
             self.list_capabilities),
            ("call_capability", "Run an explicitly granted capability through its existing host "
             "wrapper. Keep request_file and output files inside this task workspace. "
             "A submitted/accepted native task is not completed: read its result and continue "
             "with the returned native IDs. Do not bypass a failed wrapper with private protocols.",
             {"type": "object", "properties": {
                 "tool_id": {"type": "string", "enum": sorted(self.actions)},
                 "params_json": {
                     "type": "string",
                     "description": 'JSON object matching the selected action input_schema when present, '
                     'otherwise its exact declared parameters. For a file action use '
                     '{"request_file":"/absolute/task/workspace/request.json"}; '
                     'for an action with no parameters use "{}".',
                 },
             }, "required": ["tool_id", "params_json"], "additionalProperties": False},
             self.call_capability_json),
        ]

    @contextmanager
    def cancellation_scope(self, cancel_check):
        if cancel_check is not None and not callable(cancel_check):
            raise ValueError("capability cancellation must be a parent callback")
        previous = self._cancel_check()
        self._cancellation.check = cancel_check
        try:
            yield
        finally:
            self._cancellation.check = previous

    def _cancel_check(self):
        return getattr(self._cancellation, "check", None)

    def list_capabilities(self, arguments):
        if arguments:
            raise ValueError("list_capabilities takes no identity or environment overrides")
        return {"ok": True, "actions": list(self.actions.values()),
                "workdir": str(self.workspace), "readiness": "not_probed"}

    def _within_workspace(self, value, *, existing=False):
        path = Path(str(value))
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("capability file paths must be absolute and stay in the workspace")
        relative = path.relative_to(self.workspace)
        current = self.workspace
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("capability file paths cannot follow task symlinks")
        if not path.resolve(strict=existing).is_relative_to(self.workspace):
            raise ValueError("capability file path escaped the workspace")
        return path

    @contextmanager
    def _request_snapshot(self, value):
        source = self._within_workspace(value, existing=True)
        parts = source.relative_to(self.workspace).parts
        if not parts:
            raise ValueError("request_file must name a regular file")
        parent_fd = os.open(self.workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                child_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                   dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = child_fd
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_size > _MAX_REQUEST):
                raise ValueError("request_file must be an owned, bounded regular file")
            raw = stream.read(_MAX_REQUEST + 1)
        if len(raw) > _MAX_REQUEST:
            raise ValueError("request_file exceeds its transport budget")
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise ValueError("request_file must contain a JSON object")
        # These are existing path-valued capability request fields, not task plans.
        for field in ("workspacePath", "output_file"):
            if request.get(field) is not None:
                self._within_workspace(request[field], existing=field == "workspacePath")
        control = Path(os.environ["MEYO_LEGION_DIR"]) / "run" / "native-capability-calls"
        if control.resolve().is_relative_to(self.workspace):
            raise ValueError("host request snapshots must stay outside the task workspace")
        control.mkdir(mode=0o700, parents=True, exist_ok=True)
        if control.is_symlink():
            raise ValueError("host request directory cannot be a symlink")
        # The child receives the bytes just validated, not a mutable model-owned file.
        with tempfile.TemporaryDirectory(prefix="call-", dir=control) as directory:
            snapshot = Path(directory) / "request.json"
            snapshot.write_bytes(raw)
            snapshot.chmod(0o600)
            yield str(snapshot)

    def call_capability(self, arguments):
        if (not isinstance(arguments, dict) or set(arguments) != {"tool_id", "params"}
                or arguments["tool_id"] not in self.actions
                or not isinstance(arguments["params"], dict)):
            raise ValueError("capability action is not granted by this parent")
        tool_id, params = arguments["tool_id"], dict(arguments["params"])
        if tool_id.startswith(_LOCAL_PREFIX):
            return self._call_local_device(tool_id, params)
        if tool_id.startswith(_MOBILE_PREFIX):
            return self._call_mobile_device(tool_id, params)
        if tool_id.startswith(_MODEL_PREFIX):
            return self._call_local_model(tool_id, params)
        if tool_id in _SHARED_MODEL_IDS:
            _schema_value(params, self.actions[tool_id]["input_schema"])
        else:
            required = set(self.actions[tool_id].get("params") or [])
            if set(params) != required:
                raise ValueError("expected capability parameters: " + json.dumps(sorted(required)))
        import legion_tool
        import agent_tool_provider
        policy = agent_tool_provider._narrow_policy(self.context["capability_policy"], {})

        def execute():
            with agent_tool_provider._runtime_scope(
                    self.bound, self._cancel_check(), inherit_process_context=False):
                return legion_tool.run_tool(tool_id, {**params, "_capability_policy": policy})

        if "request_file" in params:
            with self._request_snapshot(params["request_file"]) as snapshot:
                params["request_file"] = snapshot
                return execute()
        return execute()

    @contextmanager
    def _task_parent(self, value):
        path = self._within_workspace(value)
        parts = path.relative_to(self.workspace).parts
        if not parts:
            raise ValueError("capability file must not be the workspace itself")
        parent = os.open(self.workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            root = os.fstat(parent)
            if (root.st_dev, root.st_ino) != self._workspace_identity:
                raise ValueError("capability workspace was replaced")
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=parent)
                os.close(parent)
                parent = child
            if os.fstat(parent).st_uid != os.getuid():
                raise ValueError("capability file directory belongs to another owner")
            yield path, parent
        finally:
            os.close(parent)

    def _same_task_parent(self, path, parent):
        with self._task_parent(str(path)) as (_, current):
            expected, actual = os.fstat(parent), os.fstat(current)
            if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
                raise ValueError("capability output directory was replaced")

    @staticmethod
    def _vacant_outputs(parent, name):
        for leaf in (name, name + ".receipt.json"):
            try:
                os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise ValueError("capability output or receipt already exists; no overwrite")

    def _publish_local_output(self, path, parent, raw, receipt, check):
        self._same_task_parent(path, parent)
        self._vacant_outputs(parent, path.name)
        created = {}
        try:
            for name, data in (
                (path.name, raw),
                (path.name + ".receipt.json",
                 json.dumps(receipt, ensure_ascii=False, indent=2).encode("utf-8")),
            ):
                check()
                fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
                with os.fdopen(fd, "w+b") as stream:
                    info = os.fstat(stream.fileno())
                    created[name] = (info.st_dev, info.st_ino)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                    stream.seek(0)
                    if stream.read() != data:
                        raise ValueError("local model publication readback differs")
            os.fsync(parent)
            self._same_task_parent(path, parent)
            for name, identity in created.items():
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (info.st_dev, info.st_ino) != identity:
                    raise ValueError("local model publication was replaced")
            check()
        except BaseException:
            # Delete only this call's own inodes, through the still-bound directory.
            for name, identity in created.items():
                try:
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if (info.st_dev, info.st_ino) == identity:
                        os.unlink(name, dir_fd=parent)
                except FileNotFoundError:
                    pass
            raise

    def _call_local_model(self, tool_id, params):
        _schema_value(params, self.actions[tool_id]["input_schema"])
        import agent_tool_provider
        import media_tools
        from legion_permissions import authorize_tool
        policy = agent_tool_provider._narrow_policy(self.context["capability_policy"], {})
        authorization = authorize_tool("mcp_call", {}, policy)
        name = self.actions[tool_id]["name"]
        for field in ("read", "write") if name != "preflight" else ("read",):
            if not policy[field]:
                authorization = {"allowed": False, "decision": "deny", "action": field,
                                 "requires_confirmation": False, "reason": "capability policy denied"}
        if not authorization["allowed"]:
            return {"ok": False, "authorization": authorization}
        cfg = self.local_config
        if cfg is None:
            raise ValueError("local model tool has no trusted parent configuration")
        duration = cfg["timeout_s"]
        if self.environment.get("MEYO_TASK_DEADLINE"):
            deadline = float(self.environment["MEYO_TASK_DEADLINE"])
            if not math.isfinite(deadline) or deadline < 0:
                raise ValueError("invalid parent task deadline")
            # This is legion_core._runtime_deadline's monotonic absolute point.
            # Zero means no inherited limit, not an already expired task.
            if deadline:
                duration = min(duration, deadline - time.monotonic())
        deadline = time.monotonic() + duration
        cancel = self._cancel_check()
        staging = generated = None
        input_facts = None
        attempted = succeeded = False
        diagnostics = {}

        def failure_result(status, **facts):
            result = {"ok": False, "status": status,
                      "session_id": self.context["session_id"], **facts}
            if name == "shape_3d" and input_facts is not None:
                result["input"] = input_facts
            if attempted and staging is not None:
                # The native sandbox cannot read this private host directory.
                # Return bounded error text as well as retaining the original files.
                context = {**diagnostics, "staging_dir": str(staging), "files": {}}
                if generated is not None and name == "coding_text":
                    for label, suffix in (("receipt", ".receipt.json"), ("log", ".log")):
                        path = Path(str(generated) + suffix)
                        try:
                            raw = _host_file_bytes(path, 8193, tail=True)
                        except FileNotFoundError:
                            continue
                        except (OSError, ValueError) as exc:
                            context["files"][label] = {"path": str(path), "read_error": str(exc)}
                        else:
                            context["files"][label] = {
                                "path": str(path), "tail": raw[-8192:].decode("utf-8", errors="replace"),
                                "truncated": len(raw) > 8192,
                            }
                result["failure_context"] = context
            return result

        def check():
            if callable(cancel) and cancel():
                raise InterruptedError("local model call cancelled")
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired([], duration)

        def child_env(staging):
            # Neither the author HOME nor the service/model credentials are inherited.
            return {
                "PATH": os.pathsep.join(dict.fromkeys([
                    *cfg.get("executable_dirs", []), str(Path(cfg["python"]).parent),
                    "/usr/bin", "/bin", "/usr/sbin", "/sbin"])),
                "HOME": cfg["home"], "TMPDIR": str(staging), "LANG": "en_US.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                "MEYO_USER_ID": self.context["user_id"],
                "MEYO_SESSION_ID": self.context["session_id"],
                "MEYO_WORKDIR": str(self.workspace),
                "MEYO_LEGION_DIR": cfg["state_dir"], "LEGION_HOME_DIR": cfg["state_dir"],
                "LEGION_PROC_REGISTRY_DIR": cfg["registry_dir"],
                "LOCAL_MEDIA_LEGION_SERVICE_DIR": str(Path(__file__).resolve().parent),
                "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                "HF_DATASETS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
            }

        def run(command, staging):
            nonlocal attempted
            check()
            attempted = True
            rc, stdout = media_tools._bounded_media_process(
                command, deadline - time.monotonic(), cancel_check=cancel,
                env=child_env(staging), cwd=str(staging), term_grace_s=cfg["term_grace_s"],
                diagnostics=diagnostics)
            check()
            try:
                observation = _wrapper_observation(stdout)
            except ValueError:
                if rc == 0:
                    raise
                observation = {"status": "failed", "error": "wrapper exited without a structured receipt"}
            return rc, observation

        try:
            check()
            control = Path(cfg["state_dir"]) / "run" / "native-local-model-calls"
            if control.resolve().is_relative_to(self.workspace):
                raise ValueError("local model staging must stay outside the task workspace")
            control.mkdir(mode=0o700, parents=True, exist_ok=True)
            if control.resolve() != control or control.stat().st_uid != os.getuid():
                raise ValueError("local model staging cannot follow symlinks or another owner")
            staging = Path(tempfile.mkdtemp(prefix="call-", dir=control))
            try:
                command = [cfg["python"], "-I", "-B"]
                if name == "preflight":
                    command += [cfg["coding_script"] if params["kind"] == "coding"
                                else cfg["shape_script"], "doctor"]
                    if params["kind"] == "shape":
                        command += ["--backend", "python"]
                    rc, observation = run(command, staging)
                    # Both trusted doctors use their exit code for CPU readiness.
                    # Their nested diagnostics are not generation receipts.
                    succeeded = rc == 0
                    result = {"ok": succeeded, "returncode": rc, "observation": observation,
                              "generation_tested": False, "session_id": self.context["session_id"]}
                    if not succeeded:
                        result.update(failure_result("failed"))
                    return result
                is_coding = name == "coding_text"
                input_key = "prompt_file" if is_coding else "image_file"
                with self._task_parent(params[input_key]) as (source, input_parent):
                    input_raw = _read_regular_at(
                        input_parent, source.name, _MAX_REQUEST if is_coding else _MAX_IMAGE_BYTES)
                input_hash = hashlib.sha256(input_raw).hexdigest()
                input_facts = {"path": str(source), "sha256": input_hash, "bytes": len(input_raw)}
                generated = staging / ("result.txt" if is_coding else "result.glb")
                if is_coding:
                    max_tokens = params.get("max_tokens", 512)
                    context_tokens = params.get("context_tokens", 8192)
                    if max_tokens > context_tokens:
                        raise ValueError("max_tokens must not exceed the context_tokens budget")
                    prompt = input_raw.decode("utf-8")
                    if "\0" in prompt or not prompt.strip():
                        raise ValueError("local coding prompt must be non-empty UTF-8 without NUL")
                    snapshot = staging / "prompt.txt"
                    snapshot.write_bytes(input_raw)
                    snapshot.chmod(0o600)
                    command += [
                        cfg["coding_script"], "text", "--prompt-file", str(snapshot),
                        "--output", str(generated),
                        "--max-tokens", str(max_tokens), "--context-tokens", str(context_tokens),
                        "--thinking", params.get("thinking", "off"), "--wait-seconds", str(cfg["wait_s"]),
                    ]
                else:
                    image_info = {}
                    _validated_screenshot(
                        base64.b64encode(input_raw).decode("ascii"), "png", image_info=image_info, check=check)
                    input_facts["image"] = image_info
                    snapshot = staging / "input.png"
                    snapshot.write_bytes(input_raw)
                    snapshot.chmod(0o600)
                    command += [cfg["shape_script"], "generate", "--backend", "python",
                                "--texture", "none", "--image", str(snapshot),
                                "--output", str(generated), "--seed", str(params.get("seed", 42))]
                with self._task_parent(params["output_file"]) as (target, output_parent):
                    self._vacant_outputs(output_parent, target.name)
                    check()
                    command += ["--timeout", str(deadline - time.monotonic())]
                    rc, observation = run(command, staging)
                    if rc != 0 or observation.get("status") != "success":
                        return failure_result("failed", returncode=rc, observation=observation)
                    if observation.get("output") != str(generated):
                        raise ValueError("wrapper output does not match the bound staging file")
                    receipt_path = Path(str(observation.get("receipt" if is_coding else "receipt_path") or ""))
                    if is_coding:
                        if receipt_path != Path(str(generated) + ".receipt.json"):
                            raise ValueError("coding wrapper receipt is not beside its output")
                    else:
                        root = Path(cfg["home"]) / ".local/share/local-media/runs/3d"
                        if (not receipt_path.is_relative_to(root) or ".." in receipt_path.parts
                                or len(receipt_path.relative_to(root).parts) != 2
                                or receipt_path.name != "receipt.json"):
                            raise ValueError("shape wrapper receipt escaped its trusted run directory")
                    receipt_raw = _host_file_bytes(receipt_path, _MAX_REQUEST)
                    wrapper_receipt = json.loads(receipt_raw)
                    if (not isinstance(wrapper_receipt, dict)
                            or wrapper_receipt.get("status") != "success"
                            or wrapper_receipt.get("output") != str(generated)):
                        raise ValueError("wrapper receipt does not confirm this output")
                    if ("input_sha256" in wrapper_receipt
                            and wrapper_receipt["input_sha256"] != input_hash):
                        raise ValueError("wrapper receipt input hash differs from the frozen input")
                    raw = _host_file_bytes(generated, _MAX_REQUEST if is_coding else _MAX_MODEL_OUTPUT)
                    if is_coding:
                        raw.decode("utf-8")
                    elif (len(raw) < 20 or raw[:4] != b"glTF"
                          or struct.unpack("<II", raw[4:12]) != (2, len(raw))):
                        raise ValueError("wrapper did not produce a complete GLB transport")
                    output_hash = hashlib.sha256(raw).hexdigest()
                    receipt = {
                        "status": "success", "tool_id": tool_id,
                        "user_id": self.context["user_id"], "session_id": self.context["session_id"],
                        "input": input_facts,
                        "output": {"path": str(target), "sha256": output_hash, "bytes": len(raw)},
                        "wrapper_receipt": wrapper_receipt,
                        "wrapper_receipt_source": {"path": str(receipt_path),
                                                   "sha256": hashlib.sha256(receipt_raw).hexdigest(),
                                                   "original_file_retained": not is_coding,
                                                   "retained_as": "embedded_wrapper_receipt"},
                        "quality_review": "not_performed",
                    }
                    self._publish_local_output(target, output_parent, raw, receipt, check)
                    succeeded = True
                    return {"ok": True, "status": "success", "path": str(target),
                            "sha256": output_hash, "bytes": len(raw),
                            "receipt_path": str(target) + ".receipt.json",
                            "session_id": self.context["session_id"],
                            "finish_reason": wrapper_receipt.get("finish_reason"),
                            "quality_review": "not_performed",
                            **({"input": input_facts} if not is_coding else {})}
            finally:
                if not attempted or succeeded:
                    shutil.rmtree(staging)
        except InterruptedError:
            return failure_result("cancelled")
        except subprocess.TimeoutExpired:
            return failure_result("timeout")
        except (ValueError, OSError) as exc:
            # Preserve prelaunch rejections and errors after a committed publication.
            if not attempted or succeeded:
                raise
            return failure_result("failed", error=f"{type(exc).__name__}: {exc}"[:8192])

    def _call_mobile_device(self, tool_id, params):
        _schema_value(params, self.actions[tool_id]["input_schema"])
        owner = self.context["user_id"]
        _mobile_environment(owner, os.environ)
        name = self.actions[tool_id]["name"]
        action_params = None
        if name == "mobile_action":
            action_params = json.loads(params["params_json"])
            if (not isinstance(action_params, dict)
                    or _MOBILE_IDENTITY_FIELDS.intersection(action_params)):
                raise ValueError("mobile action params_json must be an object without identity overrides")
        import agent_tool_provider
        import mobile_device
        from legion_permissions import authorize_tool
        policy = agent_tool_provider._narrow_policy(self.context["capability_policy"], {})
        authorization = authorize_tool("mcp_call", params, policy)
        if not authorization["allowed"]:
            return {"ok": False, "authorization": authorization}
        with agent_tool_provider._runtime_scope(
                self.bound, None, inherit_process_context=False):
            try:
                if name == "mobile_status":
                    reply = mobile_device.status(force=params.get("force", False), uid=owner)
                elif name == "mobile_start":
                    reply = mobile_device.ensure(timeout=params.get("timeout", 120), uid=owner)
                elif name == "mobile_screenshot":
                    reply = mobile_device.screenshot(uid=owner)
                elif name == "mobile_action":
                    reply = mobile_device.action(params["name"], action_params, uid=owner)
                else:  # Only mobile_stop remains in the explicit declaration set.
                    reply = mobile_device.stop(uid=owner)
            except Exception as exc:
                # An unexpected transport exception may contain a credentialed
                # provider URL. The facade's normal error dictionaries remain intact.
                return {"ok": False, "provider": "agentbay", "error_code": "mobile_callback_failed",
                        "error": f"手机操作未确认（{type(exc).__name__}）；没有自动重试。"}
        if not isinstance(reply, dict):
            raise ValueError("mobile callback returned no structured result")
        result = {key: value for key, value in reply.items() if key in _MOBILE_RESULT_FIELDS}
        if reply.get("ok") is not True:
            return {**result, "ok": False}
        if (name == "mobile_screenshot"
                or (name == "mobile_action" and params["name"] == "screenshot")):
            return self._read_mobile_screenshot(reply.get("path"))
        return result

    def _read_mobile_screenshot(self, value):
        """Open the bound owner's PNG without following even a parent symlink."""
        try:
            if not isinstance(value, str) or not value:
                raise ValueError()
            root = Path(os.environ.get("MEYO_LEGION_DIR") or "")
            source = Path(value)
            folder = root / "users" / self.context["user_id"] / "mobile"
            if (not root.is_absolute() or not source.is_absolute()
                    or ".." in source.parts or source.parent != folder
                    or source.suffix != ".png"):
                raise ValueError()
            parent = os.open(source.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for part in source.parts[1:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=parent)
                    os.close(parent)
                    parent = child
                fd = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=parent)
            finally:
                os.close(parent)
            with os.fdopen(fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                        or not 0 < before.st_size <= _MAX_IMAGE_BYTES):
                    raise ValueError()
                raw = stream.read(_MAX_IMAGE_BYTES + 1)
                after = os.fstat(stream.fileno())
                if ((before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                        != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                    raise ValueError()
        except (ValueError, OSError):
            raise ValueError("mobile screenshot must be an unchanged owned file under the mobile directory") from None
        png = _validated_screenshot(base64.b64encode(raw).decode("ascii"), "png")
        result = self._save_screenshot(png)
        width, height = struct.unpack(">II", png[16:24])
        result["width_height"] = f"{width}x{height}"
        return result

    def _call_local_device(self, tool_id, params):
        _schema_value(params, self.actions[tool_id]["input_schema"])
        _device_environment(self.context["user_id"], os.environ)
        import agent_tool_provider
        import cloud_desktop
        from legion_permissions import authorize_tool
        policy = agent_tool_provider._narrow_policy(self.context["capability_policy"], {})
        authorization = authorize_tool("mcp_call", params, policy)
        if not authorization["allowed"]:
            return {"ok": False, "authorization": authorization}
        name = self.actions[tool_id]["name"]
        # Explicit uid also reaches the existing forwarded desktop_fs callbacks;
        # the thread-local context alone is not a process environment identity.
        with agent_tool_provider._runtime_scope(
                self.bound, None, inherit_process_context=False):
            reply = cloud_desktop.local_mcp_call(
                name, {**params, "uid": self.context["user_id"]},
                bound_uid=self.context["user_id"])
        if not isinstance(reply, dict):
            raise ValueError("device callback returned no structured result")
        payload = reply.get("result")
        inner = payload.get("result") if isinstance(payload, dict) else None
        ok = (reply.get("status") == "ok"
              and reply.get("ok") is not False
              and not (isinstance(payload, dict) and payload.get("ok") is False)
              and not (name == "desktop_exec" and isinstance(payload, dict)
                       and payload.get("command_failed") is True)
              and not (isinstance(inner, dict) and inner.get("success") is False))
        if not ok:
            return {**reply, "ok": False}
        if (name in {"computer_command", "desktop_control"}
                and params.get("command") == "screenshot"):
            if not isinstance(inner, dict) or inner.get("success") is not True:
                raise ValueError("device screenshot has no successful image result")
            png = _validated_screenshot(inner.get("image_data"), inner.get("format"))
            return self._save_screenshot(png)
        if name == "desktop_screenshot":
            if not isinstance(payload, dict):
                raise ValueError("device screenshot has no file result")
            return self._read_desktop_screenshot(payload.get("path"))
        return {**reply, "ok": True}

    def _read_desktop_screenshot(self, value):
        """Read the existing screenshot once, through its bound owner's Meyo root."""
        import desktop_fs
        owner = self.context["user_id"]
        _, relative = desktop_fs._resolve(owner, value, require_file=True)
        root = desktop_fs.prepare_user_root(owner)["root"]
        parts = Path(relative).parts
        parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                next_parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                      dir_fd=parent)
                os.close(parent)
                parent = next_parent
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        finally:
            os.close(parent)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= _MAX_IMAGE_BYTES:
                raise ValueError("device screenshot must be a bounded regular file")
            raw = stream.read(_MAX_IMAGE_BYTES + 1)
            after = os.fstat(stream.fileno())
            if ((before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise ValueError("device screenshot changed during readback")
        png = _validated_screenshot(base64.b64encode(raw).decode("ascii"), "png")
        return self._save_screenshot(png)

    def _save_screenshot(self, png):
        parent = os.open(self.workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if os.fstat(parent).st_uid != os.getuid():
                raise ValueError("screenshot workspace belongs to a different owner")
            name = "desktop-screenshot-" + secrets.token_hex(12) + ".png"
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=parent)
            with os.fdopen(fd, "w+b") as stream:
                stream.write(png)
                stream.flush()
                os.fsync(stream.fileno())
                stream.seek(0)
                if stream.read() != png:
                    raise ValueError("device screenshot readback differs")
            os.fsync(parent)
        finally:
            os.close(parent)
        return CapabilityImageResult({
            "ok": True, "command": "screenshot", "path": str(self.workspace / name),
            "bytes": len(png), "sha256": hashlib.sha256(png).hexdigest(),
            "mime_type": "image/png", "session_id": self.context["session_id"],
            "model_visual_understanding": "unverified",
        }, png)

    def call_capability_json(self, arguments):
        """Use a scalar schema so native schema projections cannot erase dynamic fields."""
        if (not isinstance(arguments, dict)
                or set(arguments) != {"tool_id", "params_json"}
                or not isinstance(arguments["params_json"], str)
                or len(arguments["params_json"].encode()) > 32_000):
            raise ValueError("tool_id and bounded params_json are required")
        return self.call_capability({
            "tool_id": arguments["tool_id"],
            "params": json.loads(arguments["params_json"]),
        })


def from_environment():
    context = json.loads(os.environ.get("MEYO_NATIVE_HOST_CONTEXT") or "null")
    # A native MCP server is not allowed to inherit the model or personal HTTP key.
    clean = {key: value for key, value in os.environ.items() if key in _ENV_KEYS}
    os.environ.clear()
    os.environ.update(clean)
    os.environ["MEYO_NATIVE_HOST_ACTIVE"] = "1"
    session = CapabilitySession(context)
    if not any(item.startswith(_MOBILE_PREFIX) for item in session.allowed):
        os.environ.pop("MEYO_AGENTBAY_CONFIG", None)
    if not session.allowed.intersection(_SHARED_MODEL_IDS):
        os.environ.pop("MEYO_PERSONAL_STATE_DIR", None)
        os.environ.pop("LEGION_PERSONAL_OWNER", None)
    return session
