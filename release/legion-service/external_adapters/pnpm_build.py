#!/usr/bin/env python3
"""用 Corepack 缓存中的固定 pnpm 实体构建 workspace，保证子脚本也命中同一版本。"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys


def corepack_build_env(runtime: pathlib.Path, env: dict | None = None) -> dict:
    """Keep prepare/install and nested workspace scripts on the same cache."""
    result = dict(os.environ if env is None else env)
    result["COREPACK_HOME"] = result.get("COREPACK_HOME") or str(
        pathlib.Path(runtime).resolve() / "corepack")
    return result


def main() -> int:
    if len(sys.argv) < 4:
        print("usage: pnpm_build.py <runtime_dir> <version> <source>", file=sys.stderr)
        return 2
    runtime = pathlib.Path(sys.argv[1]).resolve()
    version = sys.argv[2]
    source = pathlib.Path(sys.argv[3]).resolve()
    env = corepack_build_env(runtime)
    cache = pathlib.Path(env["COREPACK_HOME"])
    matches = list(cache.glob(f"v*/pnpm/{version}/bin/pnpm.cjs"))
    if not matches:
        print(f"corepack cache 中找不到 pnpm {version}: {cache}", file=sys.stderr)
        return 2
    bindir = runtime / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    pnpm_cjs = matches[-1].resolve()
    wrapper = bindir / ("pnpm.cmd" if os.name == "nt" else "pnpm")
    if os.name == "nt":
        wrapper.write_text(f'@node "{pnpm_cjs}" %*\r\n', encoding="utf-8")
    else:
        wrapper.write_text(f'#!/bin/sh\nexec node "{pnpm_cjs}" "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    args = sys.argv[4:] or ["-r", "build"]
    return subprocess.run([str(wrapper), *args], cwd=str(source), env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
