"""Prepare one native process boundary; never spawn, install, or choose agent work.

The caller owns process registration/waiting and must exit this context only after
the native process has stopped. ``config_dir`` is private scratch for non-secret
hooks/MCP configuration. Credentials stay in the supplied environment, never in
config files or argv. This is write confinement, not multi-user data isolation.

The host-only backend selector is not inherited. Nested native calls therefore
keep their existing kernel boundary, original state HOME and private TMPDIR;
there is no "already safe" flag. Explicitly requesting another Codex boundary
still performs all checks and fails if its control store is read-only.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile


class NativeProcessSandboxError(ValueError):
    """No usable declared native boundary; do not retry as an unsandboxed process."""


def _absolute(value, label):
    try:
        value = os.fspath(value)
        if not isinstance(value, str) or not value or "\0" in value:
            raise ValueError
        path = Path(value)
        if not path.is_absolute():
            raise ValueError
        return path
    except (TypeError, ValueError):
        raise NativeProcessSandboxError(f"{label} must be an absolute path") from None


def _directory(value, label):
    path = _absolute(value, label)
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_dir() or resolved == Path(resolved.anchor):
            raise ValueError
    except (OSError, ValueError, RuntimeError):
        raise NativeProcessSandboxError(f"{label} must be an existing non-root directory") from None
    return resolved


def _trusted(path, label):
    info = path.stat()
    if info.st_mode & 0o022 or (
        hasattr(os, "getuid") and info.st_uid not in (0, os.getuid())
    ):
        raise NativeProcessSandboxError(f"{label} is not operator-controlled")


def _overlaps(a, b):
    return a.is_relative_to(b) or b.is_relative_to(a)


def _roots(values, label):
    if isinstance(values, (str, bytes, os.PathLike)):
        raise NativeProcessSandboxError(f"{label} must be a sequence of directories")
    try:
        return list(dict.fromkeys(_directory(p, label) for p in values))
    except TypeError:
        raise NativeProcessSandboxError(f"{label} must be a sequence of directories") from None


def _binary(runtime):
    try:
        entry = runtime / "node-tools/bin/codex"
        binary = entry.resolve(strict=True)
        if not binary.is_relative_to(runtime) or not stat.S_ISREG(binary.stat().st_mode):
            raise ValueError
        if not os.access(binary, os.X_OK):
            raise ValueError
        _trusted(binary, "codex binary")
        for parent in {*entry.parents, *binary.parents}:
            if parent.is_relative_to(runtime):
                resolved = parent.resolve(strict=True)
                if not resolved.is_relative_to(runtime):
                    raise ValueError
                _trusted(resolved, "codex binary directory")
    except (OSError, ValueError, RuntimeError):
        raise NativeProcessSandboxError("codex binary is missing, untrusted, or not executable") from None
    return binary


def _private(stack, parent, prefix):
    try:
        name = stack.enter_context(tempfile.TemporaryDirectory(dir=parent, prefix=prefix))
    except OSError:
        raise NativeProcessSandboxError(
            "native control/scratch allocation failed; a nested read-only boundary is not bypassed"
        ) from None
    result = Path(name).resolve()
    result.chmod(0o700)
    return result


@dataclass
class NativeProcessLaunch:
    config_dir: Path
    env: dict[str, str] = field(repr=False)
    _prefix: tuple[str, ...] = field(default=(), repr=False)
    _closed: bool = field(default=False, repr=False)
    executable: Path | None = None
    native_args: tuple[str, ...] = ()

    def wrap(self, argv):
        if self._closed:
            raise NativeProcessSandboxError("native launch context is already closed")
        if (isinstance(argv, (str, bytes)) or not argv
                or any(not isinstance(word, str) or "\0" in word for word in argv)
                or not argv[0]):
            raise NativeProcessSandboxError("native argv must be a nonempty argument sequence")
        return [*self._prefix, *argv]


def _codex_node_prefix(binary, env, writable):
    """Pin the JS interpreter before launch; never execute an env/PATH shim."""
    with binary.open("rb") as stream:
        first_line = stream.read(256).split(b"\n", 1)[0]
    if binary.suffix not in (".js", ".cjs", ".mjs") and not (
        first_line.startswith(b"#!") and b"node" in first_line.split()
    ):
        return ()
    try:
        entry = _absolute(shutil.which("node", path=env.get("PATH", "")), "Node entry")
        node = entry.resolve(strict=True)
        if (not stat.S_ISREG(node.stat().st_mode) or not os.access(node, os.X_OK)
                or any(p.is_relative_to(root) for p in (entry, node) for root in writable)):
            raise ValueError
        _trusted(node, "Node executable")
        for parent in {*entry.parents, *node.parents}:
            # Root-owned sticky temporary ancestors do not let another owner
            # replace our private directory. Native-writable paths were denied above.
            info = parent.stat()
            if info.st_uid == 0 and info.st_mode & stat.S_ISVTX:
                continue
            _trusted(parent, "Node directory")
    except (OSError, ValueError, RuntimeError):
        raise NativeProcessSandboxError("Node interpreter is missing or untrusted") from None
    return (str(node),)


@contextmanager
def prepare_codex_native(env, cwd, protected_roots=(), writable_state_roots=(), *,
                         read_only=False):
    """Prepare Codex exec/resume's own write boundary, without an outer sandbox.

    Callers use executable/native_args and wrap(argv), pass the original cwd,
    and stop/register the child themselves before leaving this context.
    HOME, CODEX_HOME and all session/provider settings remain untouched.
    Only child TMPDIR/registry are private. ``read_only=True`` narrows native
    command permissions, not host checkpoint/output writes. No arbitrary sandbox
    mode is accepted. This is not read or user isolation.
    """
    if type(read_only) is not bool:
        raise NativeProcessSandboxError("read_only must be a boolean restriction")
    child_env = dict(env)
    child_env.pop("LEGION_CC_SANDBOX_BACKEND", None)
    workspace = _directory(cwd, "workspace")
    protected = _roots(protected_roots, "protected roots")
    writable = list(dict.fromkeys([workspace, *_roots(writable_state_roots, "writable roots")]))
    runtime = _directory(child_env.get("MEYO_RUNTIME_ROOT"), "MEYO_RUNTIME_ROOT")
    _trusted(runtime, "runtime root")
    if not any(runtime.is_relative_to(p) for p in protected):
        raise NativeProcessSandboxError("configured runtime must be covered by protected roots")
    if any(_overlaps(w, p) for w in writable for p in protected):
        raise NativeProcessSandboxError("writable roots overlap protected roots")
    binary = _binary(runtime)
    _directory(child_env.get("HOME"), "native HOME")
    if child_env.get("CODEX_HOME"):
        _absolute(child_env["CODEX_HOME"], "native CODEX_HOME")
    control_parent = _directory(
        child_env.get("MEYO_RUNTIME_STORE") or runtime.parent.parent, "control store")
    _trusted(control_parent, "control store")
    if (control_parent.is_relative_to(runtime.parent)
            or any(control_parent.is_relative_to(p) for p in [*writable, *protected])):
        raise NativeProcessSandboxError("control store is inside protected or native-writable roots")
    prefix = _codex_node_prefix(binary, child_env, writable)
    with ExitStack() as stack:
        control = _private(stack, control_parent, ".native-control-")
        inner_tmp = _private(stack, "/tmp" if os.name != "nt" else tempfile.gettempdir(), "mnp-")
        if any(_overlaps(control, p) for p in [*writable, *protected, inner_tmp]):
            raise NativeProcessSandboxError("control directory must remain outside the native boundary")
        if any(_overlaps(inner_tmp, p) for p in protected):
            raise NativeProcessSandboxError("native temporary directory overlaps protected roots")
        config = control / "config"
        registry = inner_tmp / "run/pgids"
        for directory in (config, registry):
            directory.mkdir(parents=True, mode=0o700)
        child_env.update(TMPDIR=str(inner_tmp), LEGION_PROC_REGISTRY_DIR=str(registry))
        native_args = ("-s", "read-only") if read_only else (
            "-s", "workspace-write",
            "-c", "sandbox_workspace_write.network_access=true",
            "-c", "sandbox_workspace_write.writable_roots=" + json.dumps(
                [str(p) for p in [*writable, inner_tmp]], ensure_ascii=False),
            "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
            "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
        )
        launch = NativeProcessLaunch(config, child_env, prefix,
                                     executable=binary, native_args=native_args)
        try:
            yield launch
        finally:
            launch._closed = True


@contextmanager
def prepare_native_process(env, cwd, backend="", protected_roots=(), writable_state_roots=()):
    """Return config_dir/env/wrap(argv), with no global environment mutation.

    ``backend=""`` preserves native argv/env except the host-only
    LEGION_CC_SANDBOX_BACKEND selector, removed from child env for both modes.
    ``backend="codex"`` consumes the installed binary under MEYO_RUNTIME_ROOT and
    never searches a task's PATH for it. Writable roots are only cwd, declared
    state/data directories and a private temporary subtree. All comparisons of
    protected/writable roots use physical paths.
    The caller must also pass this cwd to Popen. Codex's -C/--cd requires an
    explicit permissions profile, so the default sandbox inherits the caller cwd.

    MEYO_RUNTIME_STORE, when provided, is the existing control allocation parent;
    otherwise the canonical <store>/<generation>/runtime layout is used. Borrowed
    layouts should pass their existing store explicitly. Nothing is written into
    a fixed generation. The caller, not this helper, supplies meaningful protection
    and state roots and retains its normal HOME/Meyo data policy.
    """
    if backend not in ("", None, "codex"):
        raise NativeProcessSandboxError("unknown native process sandbox backend")
    child_env = dict(env)
    child_env.pop("LEGION_CC_SANDBOX_BACKEND", None)
    temporary_root = "/tmp" if os.name != "nt" else tempfile.gettempdir()
    with ExitStack() as stack:
        if not backend:
            config_parent = _directory(child_env.get("TMPDIR") or temporary_root, "native config parent")
            if cwd and config_parent.is_relative_to(Path(cwd).resolve()):
                raise NativeProcessSandboxError("native config directory must not enter the workspace")
            config = _private(stack, config_parent, "meyo-native-config-")
            launch = NativeProcessLaunch(config, child_env)
        else:
            workspace = _directory(cwd, "workspace")
            protected = _roots(protected_roots, "protected roots")
            writable = list(dict.fromkeys([workspace, *_roots(writable_state_roots, "writable roots")]))
            runtime = _directory(child_env.get("MEYO_RUNTIME_ROOT"), "MEYO_RUNTIME_ROOT")
            _trusted(runtime, "runtime root")
            if not any(runtime.is_relative_to(p) for p in protected):
                raise NativeProcessSandboxError("configured runtime must be covered by protected roots")
            if any(_overlaps(w, p) for w in writable for p in protected):
                raise NativeProcessSandboxError("writable roots overlap protected roots")
            binary = _binary(runtime)
            _directory(child_env.get("HOME"), "native HOME")
            original_home = child_env["HOME"]  # Validate without changing its logical spelling.
            original_codex_home = child_env.get("CODEX_HOME")
            if original_codex_home:
                _absolute(original_codex_home, "native CODEX_HOME")
            control_parent = _directory(
                child_env.get("MEYO_RUNTIME_STORE") or runtime.parent.parent, "control store"
            )
            _trusted(control_parent, "control store")
            if (control_parent.is_relative_to(runtime.parent)
                    or any(control_parent.is_relative_to(p) for p in [*writable, *protected])):
                raise NativeProcessSandboxError("control store is inside protected or native-writable roots")
            control = _private(stack, control_parent, ".native-control-")
            inner_tmp = _private(stack, temporary_root, "mnp-")
            if any(_overlaps(control, p) for p in [*writable, *protected, inner_tmp]):
                raise NativeProcessSandboxError("control directory must remain outside the native boundary")
            if any(_overlaps(inner_tmp, p) for p in protected):
                raise NativeProcessSandboxError("native temporary directory overlaps protected roots")
            config = control / "config"
            home = control / "home"
            codex_home = home / ".codex"
            outer_tmp = control / "tmp"
            registry = inner_tmp / "run/pgids"
            for directory in (config, codex_home, outer_tmp, registry):
                directory.mkdir(parents=True, mode=0o700)
            child_env.update(HOME=str(home), CODEX_HOME=str(codex_home), TMPDIR=str(outer_tmp),
                             LEGION_PROC_REGISTRY_DIR=str(registry))
            restore = ["/usr/bin/env"]
            if not original_codex_home:
                restore += ["-u", "CODEX_HOME"]
            restore += [f"HOME={original_home}", f"TMPDIR={inner_tmp}"]
            if original_codex_home:
                restore += [f"CODEX_HOME={original_codex_home}"]
            prefix = [
                str(binary), "sandbox",
                "-c", 'sandbox_mode="workspace-write"',
                "-c", "sandbox_workspace_write.network_access=true",
                "-c", "sandbox_workspace_write.writable_roots=" + json.dumps(
                    [str(p) for p in [*writable, inner_tmp]], ensure_ascii=True),
                "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
                "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
                "--", *restore,
            ]
            launch = NativeProcessLaunch(config, child_env, tuple(prefix))
        try:
            yield launch
        finally:
            launch._closed = True
