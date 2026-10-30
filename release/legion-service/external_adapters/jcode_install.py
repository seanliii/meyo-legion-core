#!/usr/bin/env python3
"""harness_onboard 的 JCode 原生依赖配方；只消费 A 随包提供的固定归档，不下载。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import tomllib

REVISION = "57d58789970f4fe2405ca861c49275519df08147"
VERSION = "0.84.0"
ARCHIVE = "jcode-linux-x86_64.tar.gz"
DIGEST = "e00eaede1a4f26812e77382bda9d82e2d301affd983d18ada38fddd43dce9571"
LICENSE_DIGEST = "720443eee2efeda8f9f93a7a6a6f62763c17171106f60df58a35b8ea638fdf60"
WRAPPER = "jcode-linux-x86_64"
ENTRY_LINK = "../official-release/" + WRAPPER


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def registry():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import external_agent_registry
    return external_agent_registry


def validate_inputs(bundle: Path, source: Path) -> Path:
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
        raise ValueError("JCode fixed release requires Linux x86_64; no alternate build/download")
    info = json.loads((bundle / "manifest.json").read_text())
    expected = {"source_revision": REVISION, "version": VERSION,
                "archive": ARCHIVE, "sha256": DIGEST}
    if any(info.get(key) != value for key, value in expected.items()):
        raise ValueError("JCode release manifest identity mismatch")
    archive = bundle / ARCHIVE
    if archive.is_symlink() or not archive.is_file() or sha(archive) != DIGEST:
        raise ValueError("JCode official archive digest mismatch")
    package = tomllib.loads((source / "Cargo.toml").read_text()).get("package", {})
    if package.get("name") != "jcode" or package.get("version") != VERSION:
        raise ValueError("JCode source package/version mismatch")
    license_file = source / "LICENSE"
    if license_file.is_symlink() or sha(license_file) != LICENSE_DIGEST:
        raise ValueError("JCode source license digest mismatch")
    if registry()._git_revision(source) != REVISION:
        raise ValueError("JCode source revision mismatch")
    return archive


def extract_release(archive: Path, destination: Path):
    # 上游 build_linux_compat.sh 用 cp -L 保留 SSL 库，tar 成员均为顶层普通文件。
    # 固定摘要之外仍拒绝覆盖路径、链接、设备及异常膨胀，不对未知包使用宽松 extractall。
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        names = [member.name for member in members]
        if (len(members) > 128 or len(set(names)) != len(names)
                or sum(member.size for member in members) > 1024 * 1024 * 1024):
            raise ValueError("invalid JCode archive size/duplicate entries")
        for member in members:
            path = PurePosixPath(member.name)
            if (not member.isfile() or path.is_absolute() or len(path.parts) != 1
                    or path.name in ("", ".", "..") or "\\" in member.name):
                raise ValueError("unsafe JCode archive member")
        tar.extractall(destination, members=members, filter="data")
    wrapper = destination / WRAPPER
    binary = destination / (WRAPPER + ".bin")
    if not wrapper.is_file() or not wrapper.read_bytes().startswith(b"#!"):
        raise ValueError("JCode archive missing official shell wrapper")
    native = [binary, *destination.glob("libssl.so*"), *destination.glob("libcrypto.so*")]
    for file in native:
        with file.open("rb") as stream:
            header = stream.read(20)
        if (len(header) < 20 or header[:6] != b"\x7fELF\x02\x01"
                or int.from_bytes(header[18:20], "little") != 62):
            raise ValueError("JCode archive contains missing/wrong-architecture ELF")
    # 保留原文件字节及可执行位；不重写上游 wrapper，不猜测 OpenSSL SONAME。
    if not all(file.stat().st_mode & 0o111 for file in (wrapper, binary)):
        raise ValueError("JCode wrapper/binary is not executable")


def check_version(wrapper: Path) -> str:
    # 复用既有带进程登记、超时、整组回收的探测器；只运行原生版本命令。
    with tempfile.TemporaryDirectory(prefix="jcode-version-home-") as home:
        ok, output = registry()._run_probe_command(
            [str(wrapper), "--no-update", "--no-selfdev", "version"], wrapper.parent,
            timeout_s=20, env={**os.environ, "HOME": home, "JCODE_HOME": home})
    if not ok:
        raise ValueError("JCode native version command failed: " + output[-300:])
    found = []
    for line in output.splitlines():
        match = re.fullmatch(r"(?:version\s+|jcode\s+)v" + re.escape(VERSION)
                             + r"\s+\(([0-9a-f]{9,40})\)", line.strip())
        if match:
            found.append(match.group(1))
    if len(found) != 1 or not REVISION.startswith(found[0]):
        raise ValueError("JCode embedded version/revision mismatch")
    return output.strip()


def inventory(directory: Path) -> dict:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("JCode release directory is unsafe")
    result = {}
    for file in directory.iterdir():
        if file.is_symlink() or not file.is_file():
            raise ValueError("JCode release contains unexpected entry")
        result[file.name] = (sha(file), stat.S_IMODE(file.stat().st_mode))
    return result


def install_bundle(bundle: Path, runtime: Path, source: Path, *, verify_only=False) -> dict:
    archive = validate_inputs(bundle, source)
    target = runtime / "official-release"
    bindir = runtime / "bin"
    entry = bindir / "jcode"
    for directory in (runtime, bindir, target):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ValueError("existing JCode runtime path preserved")
    if (entry.exists() or entry.is_symlink()) and (
            not entry.is_symlink() or os.readlink(entry) != ENTRY_LINK):
        raise ValueError("existing JCode entry preserved")
    if verify_only and (not target.is_dir() or not entry.is_symlink()):
        raise ValueError("JCode runtime not installed")
    # 未校验输入前不创建目标目录。临时解包校验失败不会替换现有运行件。
    runtime.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".jcode-release-", dir=runtime) as temp:
        staged = Path(temp) / "release"
        staged.mkdir(mode=0o755)
        extract_release(archive, staged)
        license_target = staged / "SOURCE-LICENSE"
        if license_target.exists() and sha(license_target) != LICENSE_DIGEST:
            raise ValueError("archive/source license conflict")
        shutil.copyfile(source / "LICENSE", license_target)
        license_target.chmod(0o644)
        expected = inventory(staged)
        reused = target.exists()
        if reused and (inventory(target) != expected or stat.S_IMODE(target.stat().st_mode) != 0o755):
            raise ValueError("existing JCode runtime differs; preserved")
        # 摘要/结构检查都完成后，才执行已验证的官方 wrapper。
        version = check_version((target if reused else staged) / WRAPPER)
        if not reused:
            staged.chmod(0o755)
            os.rename(staged, target)
        if not verify_only:
            if not bindir.exists():
                bindir.mkdir(mode=0o755)
                bindir.chmod(0o755)
            if not entry.is_symlink():
                entry.symlink_to(ENTRY_LINK)
    return {"key": "jcode-runtime", "status": "verified", "version": version,
            "revision": REVISION, "archive_sha256": DIGEST, "entry": str(entry),
            "reused": reused, "task_acceptance": "not_run"}


def install_sdk(runtime: Path) -> dict:
    """Use the same install pipeline with the already verified npm lock, not latest."""
    target = runtime / "sdk"
    if target.is_symlink():
        raise ValueError("existing JCode SDK directory preserved")
    expected = {
        "package.json": Path(__file__).with_name("jcode-sdk-package.json"),
        "package-lock.json": Path(__file__).with_name("jcode-sdk-package-lock.json"),
    }
    for name, source in expected.items():
        path = target / name
        if path.exists() or path.is_symlink():
            if path.is_symlink() or json.loads(path.read_text()) != json.loads(source.read_text()):
                raise ValueError("existing JCode SDK package lock differs; preserved")
    package = target / "node_modules/@1jehuang/jcode-sdk"
    metadata = package / "package.json"
    if metadata.is_file():
        data = json.loads(metadata.read_text())
        if (data.get("name") != "@1jehuang/jcode-sdk" or data.get("version") != "1.1.0"
                or not all((package / "dist" / f).is_file() for f in ("client.js", "launch.js"))):
            raise ValueError("existing JCode SDK runtime differs; preserved")
        if not all((target / name).is_file() for name in expected):
            raise ValueError("existing JCode SDK lock missing; preserved")
        return {"sdk_version": "1.1.0", "reused": True, "runtime_acceptance": "not_run"}
    target.mkdir(parents=True, exist_ok=True)
    for name, source in expected.items():
        if not (target / name).exists():
            shutil.copyfile(source, target / name)
    result = registry()._run_install_step(
        ["npm", "ci", "--omit=optional", "--ignore-scripts", "--no-audit", "--no-fund"],
        target, dict(os.environ), 180, "jcode-runtime-sdk", 1)
    if result.get("exit_code") or result.get("timed_out"):
        raise ValueError("JCode SDK locked install failed: " +
                         str(result.get("stderr") or result.get("stdout") or "")[-400:])
    if not metadata.is_file() or json.loads(metadata.read_text()).get("version") != "1.1.0":
        raise ValueError("JCode SDK locked install did not provide the declared version")
    return {"sdk_version": "1.1.0", "reused": False, "runtime_acceptance": "not_run"}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "verify", "sdk"))
    parser.add_argument("runtime", type=Path)
    parser.add_argument("source", type=Path)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args(argv)
    result = (install_sdk(args.runtime.absolute()) if args.action == "sdk" else
              install_bundle(args.bundle.absolute(), args.runtime.absolute(), args.source.absolute(),
                             verify_only=args.action == "verify"))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
