#!/usr/bin/env python3
"""受限 macOS .app 安装、验证和启动入口，供声明式 UI capability 安全调用。"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

_INSTALL_ROOT = pathlib.Path("/Applications").resolve()


def _app_path(value: str) -> pathlib.Path | None:
    app = pathlib.Path(value).expanduser().resolve()
    return app if app.suffix == ".app" and app.is_dir() else None


def _verify(app: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)],
        capture_output=True, text=True, timeout=60,
    )


def main() -> int:
    args = sys.argv[1:]
    action = "verify"
    if args and args[0] in ("--launch", "--install"):
        action = args.pop(0)[2:]
    if len(args) != 1:
        print("usage: macos_app_verify.py [--install|--launch] <app-path>", file=sys.stderr)
        return 2
    app = _app_path(args[0])
    if app is None:
        print(f"invalid app bundle:{args[0]}", file=sys.stderr)
        return 2
    if action == "install":
        destination = _INSTALL_ROOT / app.name
        if app != destination:
            shutil.rmtree(destination, ignore_errors=True)
            subprocess.run(["/usr/bin/ditto", str(app), str(destination)],
                           check=True, timeout=180)
        app = destination
        result = _verify(app)
    elif action == "launch":
        result = subprocess.run(["/usr/bin/open", "-n", str(app)],
                                capture_output=True, text=True, timeout=30)
    else:
        result = _verify(app)
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if output:
        print(output)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
