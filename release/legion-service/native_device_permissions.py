"""Restrict Codex command tools, not the Codex process or its stdio MCP hosts.

This is a pure transformation of an adapter's ``common`` argv and its final
environment. It neither inspects the daemon nor resolves filesystem symlinks.
The operator supplies the verified Docker paths and/or private phone config path.
Only the known legacy workspace-write shape is translated; other permission
forms fail closed. No device binding means an exact, unvalidated no-op.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from pathlib import PurePosixPath
import tomllib
from urllib.parse import urlsplit


class NativeDevicePermissionsError(ValueError):
    """The requested command boundary cannot be translated without guessing."""


_WORKSPACE = "sandbox_workspace_write"
_FIELDS = frozenset({
    "writable_roots", "network_access", "exclude_slash_tmp", "exclude_tmpdir_env_var",
})
_FLAGS = frozenset({
    "--ignore-user-config", "--skip-git-repo-check", "--json", "--ephemeral",
})
_OPTIONS = frozenset({
    "-m", "--model", "-o", "--output-last-message", "-a", "--ask-for-approval", "--color",
})
# Preserve these opaque values, including the existing MCP configuration.
# New permission/feature/profile options must be reviewed rather than silently
# surviving the conversion and overriding the generated boundary.
_CONFIG_ROOTS = frozenset({
    "model", "model_provider", "model_providers", "model_catalog_json", "model_reasoning_effort",
    "model_reasoning_summary", "model_context_window", "model_max_output_tokens",
    "disable_response_storage", "mcp_servers", "approval_policy", "approvals_reviewer",
})
_PROFILE = "meyo_device_commands"


def _fail(message: str):
    # Do not echo arbitrary argv/env values: a malformed value may be a secret.
    raise NativeDevicePermissionsError("Codex device permissions: " + message)


def _path(value, field: str) -> str:
    if (not isinstance(value, str) or not value.startswith("/") or value.startswith("//")
            or value == "/" or str(PurePosixPath(value)) != value
            or ".." in PurePosixPath(value).parts
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or any(c in value for c in "*?[]{}\\%")):
        _fail(field + " must be a normalized absolute non-root path, without globs")
    return value


def _value(text: str):
    try:
        parsed = tomllib.loads("value=" + text)
    except tomllib.TOMLDecodeError:
        _fail("invalid permission TOML value")
    if set(parsed) != {"value"}:
        _fail("permission override must contain exactly one value")
    return parsed["value"]


def _read_common(common: Sequence[str]):
    if not isinstance(common, (list, tuple)) or any(not isinstance(x, str) for x in common):
        _fail("common must be an argv sequence of strings")
    kept, modes, directories = [], [], []
    workspace, table_seen = {}, False
    i = 0
    while i < len(common):
        arg = common[i]
        if arg in _FLAGS:
            kept.append(arg)
            i += 1
            continue
        option, equals, attached = arg.partition("=")
        if option not in {"-C", "--cd", "-s", "--sandbox", "-c", "--config"} | _OPTIONS:
            _fail("unsupported argv option or positional argument")
        if equals:
            if not option.startswith("--"):
                _fail("unsupported attached short option")
            value, tokens = attached, [arg]
            i += 1
        else:
            if i + 1 >= len(common) or common[i + 1].startswith("-"):
                _fail("missing option value")
            value, tokens = common[i + 1], list(common[i:i + 2])
            i += 2
        if option in _OPTIONS:
            kept.extend(tokens)
        elif option in {"-C", "--cd"}:
            directories.append(_path(value, "cwd"))
            kept.extend(tokens)
        elif option in {"-s", "--sandbox"}:
            modes.append(value)
        else:
            key, separator, raw_value = value.partition("=")
            key = key.strip()
            if not separator:
                _fail("config override needs key=value")
            if key == "sandbox_mode":
                modes.append(_value(raw_value))
            elif key == _WORKSPACE:
                if table_seen or workspace:
                    _fail("mixed or duplicate workspace settings")
                settings = _value(raw_value)
                if not isinstance(settings, dict):
                    _fail("workspace settings must be a table")
                workspace.update(settings)
                table_seen = True
            elif key.startswith(_WORKSPACE + "."):
                field = key[len(_WORKSPACE) + 1:]
                if table_seen or field in workspace:
                    _fail("mixed or duplicate workspace settings")
                workspace[field] = _value(raw_value)
            elif key.split(".", 1)[0] in _CONFIG_ROOTS:
                kept.extend(tokens)
            else:
                _fail("unsupported config override")
    if len(directories) != 1 or modes != ["workspace-write"]:
        _fail("exactly one absolute cwd and one explicit workspace-write mode are required")
    if set(workspace) - _FIELDS:
        _fail("unknown workspace permission setting")
    roots = workspace.get("writable_roots", [])
    if not isinstance(roots, list):
        _fail("writable_roots must be a list")
    roots = [_path(root, "writable root") for root in roots]
    for field in _FIELDS - {"writable_roots"}:
        if field in workspace and type(workspace[field]) is not bool:
            _fail(field + " must be a boolean")
    return kept, directories[0], roots, workspace


def restrict_codex_device_control(common: list[str], env: Mapping[str, str]) -> list[str]:
    """Replace legacy command permissions only for an explicit device binding.

    Return ``common`` itself when neither MEYO_DEVICE_DOCKER_HOST nor
    MEYO_AGENTBAY_CONFIG is present. Otherwise return a new argv or raise
    NativeDevicePermissionsError (a ValueError).
    All HOME/TMPDIR/device values come from ``env``, never the calling process.
    Existing provider, approval and MCP argv are retained byte-for-byte.

    ``none`` is the native filesystem no-access spelling accepted by Codex
    0.153.4; this does not add an outer bwrap or change MCP process permissions.
    Linux socket/connect enforcement still requires the deployment's real test.
    """
    if not {"MEYO_DEVICE_DOCKER_HOST", "MEYO_AGENTBAY_CONFIG"}.intersection(env):
        return common
    denied = []
    if "MEYO_DEVICE_DOCKER_HOST" in env:
        host = env["MEYO_DEVICE_DOCKER_HOST"]
        if not isinstance(host, str) or not host.startswith("unix:///"):
            _fail("device Docker host must be an explicit local unix socket")
        try:
            parsed = urlsplit(host)
        except ValueError:
            _fail("invalid device Docker host")
        socket_path = _path(parsed.path, "device socket")
        if (parsed.scheme != "unix" or parsed.netloc or parsed.query or parsed.fragment
                or host != "unix://" + socket_path):
            _fail("device Docker host must contain only a local unix socket path")
        socket_parent = _path(str(PurePosixPath(socket_path).parent), "device socket parent")
        data_root = _path(env.get("MEYO_DEVICE_DOCKER_DATA_ROOT"), "device Docker data root")
        home = _path(env.get("HOME"), "HOME")
        denied.extend([
            socket_parent, data_root, home + "/.config/systemd/user", home + "/.docker",
        ])
    if "MEYO_AGENTBAY_CONFIG" in env:
        config = _path(env["MEYO_AGENTBAY_CONFIG"], "phone configuration")
        # The parent owns both credentials and lifecycle/budget records. Never
        # read those files here or leave a narrower writable control subdirectory.
        denied.append(_path(str(PurePosixPath(config).parent), "phone configuration parent"))
    denied = list(dict.fromkeys(denied))
    kept, cwd, extra_roots, workspace = _read_common(common)
    writes = list(dict.fromkeys([cwd, *extra_roots]))
    effective_writes = list(writes)
    if not workspace.get("exclude_slash_tmp", False):
        effective_writes.append("/tmp")
    if not workspace.get("exclude_tmpdir_env_var", False) and env.get("TMPDIR"):
        effective_writes.append(_path(env["TMPDIR"], "TMPDIR"))
    for root in effective_writes:
        if any(PurePosixPath(root).is_relative_to(control) for control in denied):
            _fail("an existing write root would reopen a denied device-control path")
    # Preserve workspace-write's top-level metadata read carveouts. A caller
    # deliberately granting metadata writes is a different permission shape.
    metadata = [cwd + "/" + name for name in (".git", ".agents", ".codex")]
    if any(PurePosixPath(root).is_relative_to(path) for root in writes for path in metadata):
        _fail("explicit workspace metadata write grants are not supported")
    filesystem = {"/": "read", **dict.fromkeys(writes, "write")}
    filesystem.update(dict.fromkeys(metadata, "read"))
    if not workspace.get("exclude_tmpdir_env_var", False):
        filesystem[":tmpdir"] = "write"
    if not workspace.get("exclude_slash_tmp", False):
        filesystem[":slash_tmp"] = "write"
    filesystem.update(dict.fromkeys(denied, "none"))
    # Put literal filesystem keys inside the TOML VALUE: CLI dotted-key parsing
    # otherwise splits dots in paths such as .docker and .config.
    table = ", ".join(json.dumps(key, ensure_ascii=False) + "=" + json.dumps(mode)
                      for key, mode in filesystem.items())
    network = str(workspace.get("network_access", False)).lower()
    return kept + [
        "-c", "default_permissions=" + json.dumps(_PROFILE),
        "-c", f"permissions={{{_PROFILE}={{filesystem={{{table}}},network={{enabled={network}}}}}}}",
    ]
