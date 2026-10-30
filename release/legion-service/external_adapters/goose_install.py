#!/usr/bin/env python3
"""Goose install recipe used by harness_onboard; fixed upstream GNU release on Linux."""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import signal

REVISION = "71fc4be1ed729e26b1dc0a4466abdd03be548a53"
DIGEST = "c631a55e7d826d28c3bcd87b71a0aaecc9612f5a44e6c0d01e7a5b82dd8b7143"


def run_checked(command, timeout):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import legion_proc_registry
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=os.name != "nt")
    try:
        if not legion_proc_registry.register(proc.pid, owner=os.getpid(), meta={"kind": "goose-install"}):
            raise RuntimeError("cannot register Goose install process")
        stdout, stderr = proc.communicate(timeout=timeout)
        if proc.returncode:
            raise subprocess.CalledProcessError(proc.returncode, command, output=stdout, stderr=stderr)
        return stdout
    finally:
        if proc.poll() is None:
            if os.name != "nt":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
            proc.communicate(timeout=5)
        legion_proc_registry.unregister(proc.pid)


def install_bundle(bundle, runtime, source):
    info = json.loads((bundle / "manifest.json").read_text())
    if info.get("source_revision") != REVISION or info.get("sha256") != DIGEST:
        raise ValueError("Goose release identity mismatch")
    archive = bundle / info["archive"]
    if archive.parent != bundle or archive.is_symlink():
        raise ValueError("unsafe Goose archive path")
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != DIGEST:
            raise ValueError("Goose archive checksum mismatch")
    runtime.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".goose-release-", dir=runtime) as temp:
        with tarfile.open(archive) as tar:
            tar.extractall(temp, filter="data")
        binary = Path(temp) / "goose"
        if binary.is_symlink() or not binary.is_file():
            raise ValueError("Goose archive missing native binary")
        binary.chmod(binary.stat().st_mode | 0o111)
        version = run_checked([str(binary), "--version"], 20).strip()
        if "1.49.0" not in version:
            raise ValueError("Goose native version mismatch")
        target = runtime / "official-release"
        if target.exists():
            current = target / "goose"
            if not current.is_file() or hashlib.sha256(current.read_bytes()).digest() != hashlib.sha256(binary.read_bytes()).digest():
                raise ValueError("existing Goose runtime differs; preserved")
        else:
            shutil.copytree(temp, target)
        # TemporaryDirectory is 0700; copying its root must not make the
        # published executable inaccessible to the actual service account.
        target.chmod(0o755)
        bindir = runtime / "bin"
        bindir.mkdir(exist_ok=True)
        link = bindir / "goose"
        if link.exists() or link.is_symlink():
            if not link.is_symlink() or os.readlink(link) != "../official-release/goose":
                raise ValueError("existing Goose entry preserved")
        else:
            link.symlink_to("../official-release/goose")
        shutil.copy2(source / "LICENSE", target / "SOURCE-LICENSE")
    print("Goose official GNU runtime verified; task acceptance not run")


def main():
    runtime, source, repo = map(lambda p: Path(p).resolve(), sys.argv[1:4])
    if platform.system() == "Linux" and platform.machine() == "x86_64":
        install_bundle(repo / "runtime/goose-cli", runtime, source)
        return 0
    if not shutil.which("cmake"):
        if platform.system() == "Darwin" and shutil.which("brew"):
            run_checked(["brew", "install", "cmake"], 600)
        else:
            raise RuntimeError("native Goose source build requires cmake")
    run_checked(["cargo", "install", "--locked", "--root", str(runtime),
                 "--path", str(source / "crates/goose-cli")], 3600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
