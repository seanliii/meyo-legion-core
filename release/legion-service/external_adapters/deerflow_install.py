#!/usr/bin/env python3
"""Build DeerFlow via its existing installer; optionally reuse frontend dependencies."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
import zipfile


FRONTEND_RECEIPT = "frontend-build.json"
OVERLAY = Path(__file__).with_name("deerflow-api-before-files.patch")
PUBLIC_URLS = ("NEXT_PUBLIC_BACKEND_BASE_URL", "NEXT_PUBLIC_LANGGRAPH_BASE_URL")
SOURCE_EXCLUDES = {"node_modules", ".next", ".git", "__pycache__", ".turbo"}


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def gateway_origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or parsed.port is None or not 1 <= parsed.port <= 65535):
        raise ValueError("gateway origin must be http://127.0.0.1:<port>")
    return f"http://127.0.0.1:{parsed.port}"


def tree_digest(root, excludes=()):
    """Hash bytes/relative links, not timestamps; do not follow escaping links."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("frontend tree must be an existing real directory")
    root = root.resolve()
    rows = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name not in excludes)
        for name in sorted(dirs + files):
            if name in excludes:
                continue
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                link = os.readlink(path)
                if Path(link).is_absolute() or not path.resolve(strict=True).is_relative_to(root):
                    raise ValueError(f"frontend symlink escapes reusable tree: {relative}")
                rows[relative] = {"link": link}
            elif path.is_file():
                rows[relative] = {"sha256": sha(path)}
            elif not path.is_dir():
                raise ValueError(f"unsupported frontend entry: {relative}")
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def source_facts(source):
    marker = source / ".meyo-source.json"
    facts = {"frontend_sha256": tree_digest(source / "frontend", SOURCE_EXCLUDES)}
    if marker.exists() or marker.is_symlink():
        if marker.is_symlink():
            raise ValueError("source identity must not be a symlink")
        declaration = json.loads(marker.read_text())
        # Preserve the unpacker's declaration, not a fabricated patched revision.
        facts["upstream_declaration"] = {
            key: declaration[key] for key in ("revision", "url", "archive_sha256")}
        facts["source_marker_sha256"] = sha(marker)
    return facts


def require_declared_dependencies(dependencies, package):
    for section in ("dependencies", "devDependencies"):
        for name in package.get(section, {}):
            relative = Path(name)
            if (relative.is_absolute() or ".." in relative.parts
                    or not (dependencies / relative / "package.json").is_file()):
                raise ValueError(f"existing frontend dependency missing: {name}; no installation attempted")


def output_facts(frontend, include_artifacts=False):
    if frontend.is_symlink() or not frontend.is_dir():
        raise ValueError("unsafe DeerFlow frontend build directory")
    for name in (".next", ".next/BUILD_ID", ".next/routes-manifest.json"):
        path = frontend / name
        if path.is_symlink() or not path.resolve().is_relative_to(frontend.resolve()):
            raise ValueError("unsafe DeerFlow frontend build output")
    build_id = (frontend / ".next/BUILD_ID").read_text().strip()
    manifest = frontend / ".next/routes-manifest.json"
    if not build_id or not isinstance(json.loads(manifest.read_text()), dict):
        raise ValueError("DeerFlow frontend build outputs missing or invalid")
    facts = {"build_id": build_id, "routes_manifest_sha256": sha(manifest)}
    if include_artifacts:
        facts["next_artifacts_sha256"] = tree_digest(frontend / ".next", {"cache"})
    return facts


def frontend_from_receipt(runtime, source, origin):
    """Resolve a built frontend without changing the original source mapping."""
    runtime, source = Path(runtime).resolve(), Path(source).resolve()
    path = runtime / FRONTEND_RECEIPT
    if not path.exists() and not path.is_symlink():
        return source / "frontend"  # Legacy builds still undergo the launcher's routing check.
    if path.is_symlink():
        raise ValueError("DeerFlow frontend receipt must not be a symlink")
    try:
        record = json.loads(path.read_text())
        if (not isinstance(record, dict) or record.get("schema_version") != 1
                or record.get("status") != "frontend_built"
                or record.get("gateway_origin") != gateway_origin(origin)
                or record.get("source") != source_facts(source)
                or record["overlay"]["sha256"] != sha(OVERLAY)):
            raise ValueError("DeerFlow frontend build receipt is stale; rebuild required")
        relative = Path(record["output"]["frontend"])
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError("DeerFlow frontend build path escapes runtime")
        frontend = runtime / relative
        if (frontend.is_symlink() or not frontend.resolve().is_relative_to(runtime)
                or frontend.resolve() == runtime):
            raise ValueError("DeerFlow frontend build path escapes runtime")
        actual = output_facts(frontend)
        if any(record["output"].get(key) != value for key, value in actual.items()):
            raise ValueError("DeerFlow frontend build output changed; rebuild required")
        return frontend
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid DeerFlow frontend build receipt/output; rebuild required") from exc


def save_frontend_receipt(runtime, record):
    target = runtime / FRONTEND_RECEIPT
    if target.is_symlink():
        raise ValueError("DeerFlow frontend receipt must not be a symlink")
    with tempfile.NamedTemporaryFile(mode="w", dir=runtime, prefix=".frontend-receipt-",
                                     delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(record, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_uv(repo, runtime):
    bundle = repo / "runtime/uv"
    info = json.loads((bundle / "manifest.json").read_text())
    archive = bundle / info["archive"]
    if archive.parent != bundle or archive.is_symlink():
        raise ValueError("invalid uv archive path")
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != info["sha256"]:
            raise ValueError("uv archive checksum mismatch")
    target = runtime / "build-uv"
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as wheel:
        for name in wheel.namelist():
            path = Path(name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("unsafe uv wheel path")
        wheel.extractall(target)
    binaries = list(target.glob("*.data/scripts/uv"))
    if len(binaries) != 1:
        raise ValueError("uv wheel executable missing")
    binaries[0].chmod(0o755)
    return binaries[0]


def run(command, cwd, env, log):
    configured = (os.environ if env is None else env).get("MEYO_BUILD_INSTALL_TIMEOUT")
    timeout = 1800
    if configured not in (None, ""):
        value = str(configured)
        if not value.isascii() or not value.isdecimal() or int(value) <= 0:
            raise ValueError("MEYO_BUILD_INSTALL_TIMEOUT must be a positive integer")
        timeout = int(value)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import legion_proc_registry
    with log.open("ab") as output:
        proc = subprocess.Popen(command, cwd=cwd, env=env, stdout=output, stderr=output,
                                start_new_session=os.name != "nt")
        try:
            if not legion_proc_registry.register(proc.pid, owner=os.getpid(), meta={"kind": "deerflow-install"}):
                raise RuntimeError("process registration failed")
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired as error:
                # Keep the original exception and cleanup semantics. A path is
                # enough for recovery; never put unredacted log contents here.
                if hasattr(error, "add_note"):
                    error.add_note(f"Private installation log: {log}")
                raise
            if rc:
                raise RuntimeError(f"DeerFlow build step exit {rc}; inspect private log {log}")
        finally:
            if proc.poll() is None:
                if os.name != "nt":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
                proc.wait(timeout=5)
            legion_proc_registry.unregister(proc.pid)


def private_build_log(runtime, source, repo, env):
    fallback = Path(env.get("HOME") or Path.home()) / ".cache" / "meyo-build-logs"
    directory = Path(env.get("TMPDIR") or fallback).resolve()
    forbidden = [runtime.resolve(), source.resolve(),
                 *(repo.resolve() / name for name in ("bin", "runtime", "legion"))]
    if any(directory == path or directory.is_relative_to(path) for path in forbidden):
        raise ValueError("private build logs must be outside source and published runtime")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # CI serializes installers and may restore old cache trees before packaging.
    # Do not move another live frontend rebuild's log in normal service use.
    if (runtime / "frontend-builds").is_symlink():
        raise ValueError("unsafe legacy build directory; preserved")
    builds = (runtime / "frontend-builds").resolve()
    old_logs = builds.glob("build-*/build.log") if env.get("MEYO_BUILD_CACHE_ROOT") else ()
    for old in old_logs:
        if (old.parent.is_symlink() or old.is_symlink() or not old.is_file()
                or not old.resolve().is_relative_to(builds)):
            raise ValueError("unsafe legacy private build log; preserved")
        before = old.stat()
        with tempfile.NamedTemporaryFile(prefix="deerflow-legacy-", suffix=".log",
                                         dir=directory, delete=False) as target:
            with old.open("rb") as original:
                shutil.copyfileobj(original, target)
            target.flush()
            os.fsync(target.fileno())
        after = old.stat()
        if (old.is_symlink() or old.parent.is_symlink()
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise RuntimeError("legacy build log changed during copy; original preserved")
        old.unlink()
    with tempfile.NamedTemporaryFile(prefix="deerflow-build-", suffix=".log",
                                     dir=directory, delete=False) as target:
        return Path(target.name)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime", "source", "repo"):
        parser.add_argument(name, type=Path)
    parser.add_argument("--frontend-only", action="store_true",
                        help="copy/reuse existing dependencies; never run uv or pnpm install")
    parser.add_argument("--gateway-origin", help="selected native gateway's loopback origin")
    args = parser.parse_args(argv)
    runtime, source, repo = (getattr(args, name).resolve() for name in ("runtime", "source", "repo"))
    if runtime.is_relative_to(source) or source.is_relative_to(runtime):
        raise ValueError("frontend build runtime and original source must be separate")
    env = dict(os.environ)
    configured = env.get("DEER_FLOW_INTERNAL_GATEWAY_BASE_URL")
    if args.gateway_origin and configured and gateway_origin(args.gateway_origin) != gateway_origin(configured):
        raise ValueError("gateway origin conflicts with DEER_FLOW_INTERNAL_GATEWAY_BASE_URL")
    if args.frontend_only and not (args.gateway_origin or configured):
        raise ValueError("--frontend-only requires an explicit selected gateway origin")
    origin = gateway_origin(args.gateway_origin or configured or "http://127.0.0.1:8001")
    if any(name in env for name in PUBLIC_URLS):
        raise ValueError("NEXT_PUBLIC backend/langgraph URLs must be unset, including empty values")
    package = json.loads((source / "frontend/package.json").read_text())
    if package.get("scripts", {}).get("build") != "next build" or package.get("packageManager") != "pnpm@10.26.2":
        raise ValueError("unrecognized native DeerFlow frontend build recipe")
    lock = source / "frontend/pnpm-lock.yaml"
    if not lock.is_file() or not OVERLAY.is_file():
        raise ValueError("DeerFlow frontend lockfile or source overlay missing")
    node, git = shutil.which("node"), shutil.which("git")
    if not node or not git:
        raise ValueError("existing Node and git are required; automatic installation is disabled")
    if (runtime / FRONTEND_RECEIPT).is_symlink():
        raise ValueError("DeerFlow frontend receipt must not be a symlink")
    facts = source_facts(source)
    dependencies = source / "frontend/node_modules"
    if args.frontend_only:
        # Full installs now put dependencies in their private frontend view.
        # A later gateway-only rebuild may reuse those without reinstalling.
        if not dependencies.exists() and (runtime / FRONTEND_RECEIPT).is_file():
            previous = json.loads((runtime / FRONTEND_RECEIPT).read_text())
            dependencies = frontend_from_receipt(
                runtime, source, previous["gateway_origin"]) / "node_modules"
        if dependencies.is_symlink() or not (dependencies / "next/dist/bin/next").is_file():
            raise ValueError("existing frontend dependencies missing or unsafe; no installation attempted")
        require_declared_dependencies(dependencies, package)
        dependency_digest = tree_digest(dependencies)
    runtime.mkdir(parents=True, exist_ok=True)
    if (runtime / FRONTEND_RECEIPT).is_symlink():
        raise ValueError("DeerFlow frontend receipt must not be a symlink")
    builds = runtime / "frontend-builds"
    if builds.is_symlink():
        raise ValueError("DeerFlow frontend build directory must not be a symlink")
    builds.mkdir(mode=0o700, exist_ok=True)
    view = Path(tempfile.mkdtemp(prefix="build-", dir=builds))
    frontend = view / "frontend"
    shutil.copytree(source / "frontend", frontend, symlinks=True,
                    ignore=lambda directory, names: [name for name in names if name in SOURCE_EXCLUDES])
    if args.frontend_only:
        # Real copies, not hardlinks or an out-of-root node_modules symlink:
        # build writes cannot modify old dependencies; pnpm's internal links remain relative.
        shutil.copytree(dependencies, frontend / "node_modules", symlinks=True)
        if tree_digest(frontend / "node_modules") != dependency_digest:
            raise ValueError("frontend dependencies changed while copying; no installation attempted")
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        env.pop(name, None)
    env["GIT_CEILING_DIRECTORIES"] = str(builds)
    env.update(DEER_FLOW_INTERNAL_GATEWAY_BASE_URL=origin, NODE_ENV="production")
    env.update(UV_PROJECT_ENVIRONMENT=str(runtime / "venv"), UV_PYTHON=sys.executable,
               UV_PYTHON_DOWNLOADS="never")
    env["UV_CACHE_DIR"] = env.get("UV_CACHE_DIR") or str(runtime / "uv-cache")
    env["COREPACK_HOME"] = env.get("COREPACK_HOME") or str(runtime / "corepack")
    log = private_build_log(runtime, source, repo, env)
    config_before = sha(frontend / "next.config.js")
    run([git, "apply", "--check", str(OVERLAY)], view, env, log)
    run([git, "apply", str(OVERLAY)], view, env, log)
    if sha(frontend / "next.config.js") == config_before:
        raise ValueError("DeerFlow source overlay was not applied")
    if not args.frontend_only:
        uv = prepare_uv(repo, runtime) if sys.platform.startswith("linux") else Path(shutil.which("uv") or "uv")
        run([str(uv), "sync", "--frozen", "--no-dev", "--no-editable"], source / "backend", env, log)
        # Preserve development dependencies required by the normal native build.
        install_env = dict(env)
        install_env.pop("NODE_ENV", None)
        run(["corepack", "pnpm@10.26.2", "install", "--frozen-lockfile"], frontend, install_env, log)
        require_declared_dependencies(frontend / "node_modules", package)
        dependency_digest = tree_digest(frontend / "node_modules")
    cli = frontend / "node_modules/next/dist/bin/next"
    if not cli.is_file() or not cli.resolve().is_relative_to(frontend):
        raise ValueError("existing native Next CLI missing or escapes frontend")
    next_package = frontend / "node_modules/next/package.json"
    build_command = [node, str(cli), "build"]
    # package.json's verified build script is exactly "next build". Direct Node
    # uses the installed CLI without a Corepack download or a second install.
    run(build_command, frontend, env, log)
    output = output_facts(frontend, include_artifacts=True)
    if source_facts(source) != facts:
        raise ValueError("original DeerFlow frontend source changed during build")
    record = {
        "schema_version": 1, "status": "frontend_built",
        "mode": "frontend-only" if args.frontend_only else "full",
        "runtime_acceptance": "not_run", "gateway_origin": origin, "source": facts,
        "overlay": {"file": OVERLAY.name, "sha256": sha(OVERLAY),
                    "next_config_sha256": sha(frontend / "next.config.js")},
        "inputs": {
            "installer_sha256": sha(Path(__file__)), "package_json_sha256": sha(source / "frontend/package.json"),
            "lockfile_sha256": sha(lock), "dependencies_sha256": dependency_digest,
            "node_binary_sha256": sha(Path(node).resolve()),
            "next_version": json.loads(next_package.read_text())["version"],
            "public_gateway_urls": "unset", "node_env": "production",
            "build_environment_sha256": {
                key: hashlib.sha256(value.encode()).hexdigest() for key, value in sorted(env.items())
                if key.startswith("NEXT_PUBLIC_") or key in ("NODE_OPTIONS", "NEXT_CONFIG_BUILD_OUTPUT")},
        },
        "output": {
            "frontend": frontend.relative_to(runtime).as_posix(), **output,
        },
        # Evidence and entry resolution stay relocatable; no build-host roots.
        "command": ["node", "node_modules/next/dist/bin/next", "build"],
    }
    save_frontend_receipt(runtime, record)
    print(json.dumps({"status": record["status"], "mode": record["mode"],
                      "receipt": str(runtime / FRONTEND_RECEIPT), "runtime_acceptance": "not_run"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
