#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nanobot_wrapper.py - nanobot HEADLESS wrapper(中文任务不走argv).
(根因:macOS 下Python posix_spawn 传中文 argv 时 setproctitle 库 import 即崩（UnicodeDecodeError）。本 wrapper 从 env 读任务，假 setproctitle 模块垫背，再进程内调 nanobot main()。)
"""
from __future__ import annotations
import os
import sys
import typing
import json
import hashlib
from pathlib import Path


def prepare_native_state(config: str, workdir: str, environment=None) -> list[str]:
    """Prepare only this adapter's existing per-config state, without reading it."""
    if not config:
        return []
    environment = os.environ if environment is None else environment
    workspace = Path(workdir).resolve()
    original = Path(config)
    original = (original if original.is_absolute() else workspace / original).resolve()
    if not original.is_relative_to(workspace):
        # A raw external config path is not permission to open its parent.
        return []
    state = Path(environment.get("MEYO_LEGION_DIR") or
                 Path(environment.get("HOME") or Path.home()) / ".meyo/legion").resolve()
    digest = hashlib.sha256(str(original).encode()).hexdigest()
    target_dir = state / "external_sessions/nanobot" / digest
    resolved = target_dir.resolve()
    if target_dir.is_symlink() or not resolved.is_relative_to(state):
        raise ValueError("nanobot session state escapes persistent root")
    if resolved.is_relative_to(workspace):
        raise ValueError("nanobot persistent state must be outside workspace")
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target_dir.is_symlink() or not target_dir.resolve().is_relative_to(state):
        raise ValueError("nanobot session state escapes persistent root")
    target_dir.chmod(0o700)
    return [str(target_dir)]


def private_config(config: str, workdir: str) -> str:
    """Honor native session storage outside the writable agent workspace."""
    workspace = Path(workdir).resolve()
    original = Path(config).resolve()
    if not original.is_relative_to(workspace):
        return str(original)
    target_dir = Path(prepare_native_state(str(original), workdir)[0])
    target = target_dir / "config.json"
    import tempfile
    fd, temporary = tempfile.mkstemp(dir=target_dir, prefix=".config-")
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(original.read_bytes())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    original.unlink()
    return str(target)


def main() -> int:
    task = os.environ.get("NANOBOT_TASK") or ""
    config = os.environ.get("NANOBOT_CONFIG") or ""
    if not task or not config:
        print(json.dumps({"init_error": "NANOBOT_TASK/NANOBOT_CONFIG missing"}))
        return 2
    config = private_config(config, os.environ.get("MEYO_WORKDIR") or os.getcwd())
    if sys.platform == "darwin":
        import types as _types; fake = _types.ModuleType("setproctitle")
        fake.setproctitle = lambda *a, **k: None
        fake.getproctitle = lambda: "nanobot-wrapper"
        sys.modules["setproctitle"] = fake
    adapter_dir = os.environ.get("NANOBOT_ADAPTER_DIR") or ""
    if adapter_dir and os.path.isdir(adapter_dir):
        if adapter_dir not in sys.path:
            sys.path.insert(0, adapter_dir)
    sys.argv = ["nanobot", "agent", "--config", config, "--message", task]
    from nanobot.cli.entry import main as nb_main
    nb_main()
    return 0


if __name__ == "__main__":
    import json
    raise SystemExit(main())
