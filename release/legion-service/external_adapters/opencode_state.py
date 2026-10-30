"""OpenCode-owned XDG state; no HOME grant or implicit legacy-state migration."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import secrets
import stat


@contextmanager
def _directory(root, parts=(), *, create=False):
    """Walk from a trusted root without following task-created directory links."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.dup(root) if isinstance(root, int) else os.open(root, flags)
    try:
        for part in parts:
            if part in ("", ".", "..") or "/" in part:
                raise ValueError("invalid OpenCode state path component")
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _config_location(config, workdir):
    logical_workspace = Path(os.path.abspath(workdir))
    workspace = logical_workspace.resolve(strict=True)
    path = Path(config)
    if not path.is_absolute():
        path = logical_workspace / path
    path = Path(os.path.abspath(path))
    for base in (logical_workspace, workspace):
        try:
            parts = path.relative_to(base).parts
            break
        except ValueError:
            continue
    else:
        raise ValueError("OpenCode generated config must be inside its workspace") from None
    if not parts:
        raise ValueError("OpenCode config must name a file")
    return workspace, workspace.joinpath(*parts), parts


def _ordinary_config(fd, name, *, optional=False):
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        if optional:
            return
        raise
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("OpenCode config must be an ordinary file, not a link")


def write_config(config, workdir, content):
    """Atomically write the existing template without touching a link target."""
    workspace, _, parts = _config_location(config, workdir)
    with _directory(workspace, parts[:-1], create=True) as parent:
        _ordinary_config(parent, parts[-1], optional=True)
        temporary = ".opencode-config-" + secrets.token_hex(12)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(content)
            os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


def prepare_native_state(config, workdir, environment, owner):
    """Prepare this owner's stable per-config state and update only child env."""
    if not config:
        raise ValueError("OpenCode native state requires its generated config")
    workspace, config_path, parts = _config_location(config, workdir)
    with _directory(workspace, parts[:-1]) as parent:
        _ordinary_config(parent, parts[-1])
    data = Path(environment.get("MEYO_LEGION_DIR") or
                Path(environment["HOME"]) / ".meyo/legion").resolve(strict=True)
    digest = hashlib.sha256((str(owner) + "\0" + str(config_path)).encode()).hexdigest()
    state_parts = ("external_sessions", "opencode", digest)
    state = data.joinpath(*state_parts)
    if state.is_relative_to(workspace):
        raise ValueError("OpenCode native state must be outside the task workspace")
    with _directory(data, state_parts, create=True) as directory:
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("OpenCode native state must be private to its service identity")
        for name in ("data", "cache", "config", "state"):
            with _directory(directory, (name,), create=True):
                pass
    # The caller passes a copy; an inherited global XDG directory is not an
    # authorization to write shared state. Existing HOME and its history stay put.
    for name in ("data", "cache", "config", "state"):
        environment["XDG_" + name.upper() + "_HOME"] = str(state / name)
    return [str(state)]
