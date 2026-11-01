#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部 Agent 的版本锁定、安装、探测与统一执行入口。"""
from __future__ import annotations

import json
import hashlib
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from typing import Any

import legion_paths as _paths

try:  # POSIX 文件锁;Windows 无 fcntl 时退化为无锁(单进程场景不受影响)
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore

import threading

_LOCK_TLS = threading.local()  # catalog_lock 同线程可重入深度


@contextmanager
def catalog_lock(timeout_s: float = 30.0):
    """catalog 跨进程写锁(双 agent 并发丢写根治·2026-09-02 血泪)。

    两个 agent session 同时对 external_agents.json 做 load->改->save 的
    read-modify-write:A 读(15家)->B 读(15家)->A 写(16家含qwen)->B 按旧内存
    写(15家)->qwen 凭空消失。凡是要改 catalog 的路径(install/apply_update/
    onboarding)必须全程持锁;纯读(load_catalog)不必持锁(读个快照即可)。
    """
    # 可重入:同线程已持锁时嵌套调用直接放行(flock 不同 fd 同进程会自死锁)。
    _depth = getattr(_LOCK_TLS, "depth", 0)
    if fcntl is None or _depth > 0:
        _LOCK_TLS.depth = _depth + 1
        try:
            yield
        finally:
            _LOCK_TLS.depth = _depth
        return
    lock_path = CATALOG.parent / ".external_agents.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fh:
        deadline = time.time() + timeout_s
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= deadline:
                    raise TimeoutError(f"catalog lock timeout {timeout_s}s")
                time.sleep(0.2)
        _LOCK_TLS.depth = 1
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
            _LOCK_TLS.depth = 0

import agent_manifest
from external_agent_transports import TransportError, run_transport, _managed_cli_app, is_readonly_loopback_http

HERE = pathlib.Path(__file__).resolve().parent
CATALOG = HERE / "external_agents.json"
_STATE_DIR = pathlib.Path(_paths.under("external_agents"))
_STATE_PATH = _STATE_DIR / "state.json"
_LOG_DIR = _STATE_DIR / "logs"
_ALLOWED_EXECUTABLES = {"python", "python.exe", "python3", "python3.12", "pip", "pip3", "npm", "npx",
                        "pnpm", "corepack", "cargo", "make", "docker", "git", "uv", "brew",
                        "node", "node.exe"}


def load_catalog() -> list[dict]:
    try:
        data = json.loads(CATALOG.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [item for item in data.get("agents", []) if isinstance(item, dict) and item.get("key")]


# This is Meyo code identity, not a catalog/request assertion of sandbox ownership.
# Update atomically with the reviewed bootstrap; any mismatch retains generic protection.
_CLINE_BOOTSTRAP_SHA256 = "c2e5f90266c0cd5599792b71806f240556265636d81a996958bcc082d63f78ed"


def _trusted_cline_bootstrap(spec, context, env):
    """Admit only the fixed Meyo bootstrap; it must protect its own native CLI."""
    if str(spec.get("type") or "cli").lower() != "cli":
        return None
    # Python isolation does not neutralize the OS dynamic loader. Do not execute
    # a bootstrap (or a sandbox launcher) with a newly injected loader setting.
    if any(name.startswith(("LD_", "DYLD_")) and value != os.environ.get(name)
           for name, value in env.items() if value):
        raise TransportError("Cline bootstrap has an untrusted loader environment")
    from external_agent_transports import _format
    from native_process_sandbox import _directory, _overlaps, _trusted

    service = HERE.resolve()
    entry = service / "external_adapters/cline_cli.py"
    try:
        expected = [
            context["python"], str(entry), "--runtime-dir", context["runtime_dir"],
            "--source", context["source"], "--model", context["model"],
            "--gateway", context["gateway"], "--runtime-context",
            context["runtime_context_json"], "--", context["task"],
        ]
        if _format(spec.get("command") or [], context) != expected:
            return None
        interpreter = pathlib.Path(sys.executable).resolve(strict=True)
        requested = pathlib.Path(context["python"])
        if not requested.is_absolute() or requested.resolve(strict=True) != interpreter:
            return None
        for path in (service, entry.parent, interpreter):
            _trusted(path, "Meyo Cline bootstrap")
        for path in (entry, entry.parent / "cline_native.py",
                     service / "native_process_sandbox.py", service / "external_agent_transports.py"):
            if path.is_symlink() or not path.is_file():
                return None
            _trusted(path, "Meyo Cline bootstrap dependency")
        if hashlib.sha256(entry.read_bytes()).hexdigest() != _CLINE_BOOTSTRAP_SHA256:
            return None
        app = _directory(service.parent.parent, "Meyo app")
        _trusted(app, "Meyo app")
        _trusted(service.parent, "Meyo program directory")
        runtime = _directory(app / "runtime", "installed runtime")
        _trusted(runtime, "installed runtime")
        declared = env.get("MEYO_RUNTIME_ROOT")
        if declared and (not pathlib.Path(declared).is_absolute()
                         or pathlib.Path(declared).resolve(strict=True) != runtime):
            return None
        work = _directory(context["workdir"], "Cline workspace")
        protected = [app, runtime, service]
        protected.extend(_directory(context[name], name) for name in ("source", "runtime_dir"))
        if any(_overlaps(work, path) for path in protected) or interpreter.is_relative_to(work):
            return None
        shared = [pathlib.Path(env.get("MEYO_LEGION_DIR") or
                              pathlib.Path(env["HOME"]) / ".meyo/legion").resolve()]
        if env.get("LEGION_HOME_DIR"):
            shared.append(pathlib.Path(env["LEGION_HOME_DIR"]).resolve())
        if any(path.is_relative_to(work) for path in shared):
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    # Isolated Python cannot load task cwd/PYTHONPATH/user-site startup code.
    # These interpreter flags do not change the bootstrap's application argv.
    command = [str(interpreter), "-I", "-S", "-B", str(entry), *expected[2:]]
    return command, {**env, "MEYO_RUNTIME_ROOT": str(runtime)}, expected


def validate_catalog() -> dict:
    errors = []
    seen = set()
    for item in load_catalog():
        key = str(item.get("key") or "")
        if key in seen:
            errors.append(f"{key}:重复 key")
        seen.add(key)
        checked = agent_manifest.validate(item)
        errors.extend(f"{key}:{error}" for error in checked["errors"])
    return {"ok": not errors, "errors": errors, "count": len(seen)}


def _current_platform_target() -> str:
    """本机平台目标(catalog platform_target 同一词汇表):macOS→macos-native。"""
    if sys.platform == "darwin":
        return "macos-native"
    if sys.platform.startswith("win"):
        return "cloud-windows-vm"
    return "linux-native"


def _safe_key(value: str) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "-", str(value or "").lower()).strip("-.")[:80]


def _deployment_paths(item: dict, *,
                      require_pair: bool = True) -> tuple[pathlib.Path, pathlib.Path] | None:
    """Resolve one pre-provisioned release pair; absent declaration keeps legacy paths.

    The declaration is a literal relative directory, not a caller/template value.
    Source revision/content verification remains in the existing probe path.
    Mutation guards retain the declared namespace even if its contents are damaged.
    """
    runtime = item.get("runtime")
    if not isinstance(runtime, dict) or "deployment_directory" not in runtime:
        return None
    relative = runtime["deployment_directory"]
    if (not isinstance(relative, str)
            or not re.fullmatch(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*", relative)
            or any(part in {".", "..", "source", "runtime"} for part in relative.split("/"))):
        raise ValueError("invalid deployment_directory")
    configured_root = os.environ.get("MEYO_RUNTIME_STORE", "")
    if (not configured_root or configured_root != configured_root.strip()
            or any(ord(char) < 32 for char in configured_root)
            or not pathlib.Path(configured_root).is_absolute()):
        raise ValueError("deployment_directory requires an absolute MEYO_RUNTIME_STORE")
    try:
        store = pathlib.Path(configured_root).resolve(strict=True)
        directory = (store / relative).resolve(strict=require_pair)
        if not store.is_dir() or directory == store or not directory.is_relative_to(store):
            raise ValueError("deployment directory escapes runtime store")
        if any(part in {"source", "runtime"} for part in directory.relative_to(store).parts):
            raise ValueError("deployment directory cannot name a source/runtime subtree")
        if not require_pair:
            return directory / "source", directory / "runtime"
        source = (directory / "source").resolve(strict=True)
        runtime_dir = (directory / "runtime").resolve(strict=True)
        if (source != directory / "source" or runtime_dir != directory / "runtime"
                or not source.is_dir() or not runtime_dir.is_dir()):
            raise ValueError("deployment source/runtime must belong to the same directory")
        return source, runtime_dir
    except (OSError, RuntimeError) as exc:
        raise ValueError("deployment directory is not a valid pre-provisioned pair") from exc


def source_dir(item: dict) -> pathlib.Path:
    deployed = _deployment_paths(item)
    if deployed is not None:
        return deployed[0]
    packaged_root = os.environ.get("LEGION_EXTERNAL_SOURCE_ROOT", "").strip()
    if packaged_root:
        root = pathlib.Path(packaged_root).expanduser().resolve()
        key = _safe_key(item["key"])
        if not key:
            raise ValueError("external source key is empty")
        mapping = root / "source-map.json"
        if mapping.exists():
            if mapping.is_symlink():
                raise ValueError("source map must not be a symlink")
            rows = json.loads(mapping.read_text())["sources"]
            record = rows.get(key)
            if record is None:
                # Unmapped members remain absent, not redirected to another revision.
                path = (root / key).resolve()
                if path.parent != root:
                    raise ValueError("external source path escapes configured root")
                return path
            if record.get("revision") != item.get("revision"):
                raise ValueError("source map revision mismatch")
            relative = pathlib.Path(record["path"])
            if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.name != key:
                raise ValueError("invalid mapped source path")
            path = (root / relative).resolve()
            if not path.is_relative_to(root) or path == root:
                raise ValueError("external source path escapes configured root")
            return path
        path = (root / key).resolve()
        if path.parent != root:
            raise ValueError("external source path escapes configured root")
        return path
    local = str(item.get("local_source") or "").strip()
    if local:
        path = pathlib.Path(os.path.expanduser(local))
        if not path.is_absolute():
            path = HERE.parents[1] / path
        return path.resolve()
    return pathlib.Path(_paths.repos_dir(), "external-agents", _safe_key(item["key"])).resolve()


def _read_state() -> dict:
    try:
        value = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _write_state(key: str, patch: dict) -> dict:
    state = _read_state()
    row = {**(state.get(key) or {}), **patch, "updated_at": time.time()}
    state[key] = row
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="external-state-", suffix=".json", dir=str(_STATE_DIR))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, _STATE_PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    # 状态变更→清全量 probe 缓存·让新安装/卸载的 agent 立即对 list_harnesses 可见(一处覆盖所有变更)
    try:
        invalidate_probe_cache()
        invalidate_updates_cache()  # 装/更新后 revision 变了·升级检查缓存也要失效重查
    except Exception:
        pass
    return row


def _context(item: dict, workdir: str = "", values: dict | None = None) -> dict[str, str]:
    deployed = _deployment_paths(item)
    if deployed is not None:
        root, runtime_dir = deployed
    else:
        root = source_dir(item)
        runtime_root = os.environ.get("LEGION_EXTERNAL_RUNTIME_ROOT", "").strip()
        runtime_dir = (pathlib.Path(runtime_root).expanduser() / _safe_key(item["key"]) if runtime_root
                       else pathlib.Path(_paths.under("external_agents", "runtimes", _safe_key(item["key"]))))
        runtime_dir = runtime_dir.resolve()
        if runtime_root and runtime_dir.parent != pathlib.Path(runtime_root).expanduser().resolve():
            raise ValueError("external runtime path escapes configured root")
    venv_dir = runtime_dir / "venv"
    venv_bin = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    node_bin = runtime_dir / "node_modules" / ".bin"
    runtime_bin = venv_bin if (venv_bin.exists() or item.get("runtime", {}).get("python")) else node_bin
    repo_root = HERE.parents[1]
    configured_node = str((item.get("runtime") or {}).get("node_binary") or "")
    node = configured_node.replace("{repo_root}", str(repo_root)) if configured_node else (shutil.which("node") or "node")
    # 动态 node_binary 失效时回退 PATH，而不是继续生成指向不存在解释器的启动包装器。
    if configured_node and not pathlib.Path(node).is_file():
        node = shutil.which("node") or "node"
    configured_npm = str((item.get("runtime") or {}).get("npm_cli") or "")
    npm_cli = configured_npm.replace("{repo_root}", str(repo_root)) if configured_npm else \
        str(pathlib.Path(node).resolve().parent.parent / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js")
    if not pathlib.Path(npm_cli).is_file():
        npm_cli = str(pathlib.Path(shutil.which("npm") or "npm"))
    ironclaw_token = ""
    if item.get("key") == "ironclaw":
        token_path = pathlib.Path(_paths.under("external_agents", "runtimes", "ironclaw", "home", "webui-token"))
        try:
            ironclaw_token = token_path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    # {python}锁定稳定解释器(2026-09-01 smoke钉死·server用3.14起时sys.executable=3.14会把所有 adapter worker
    # 也换成3.14跑→omniagent等worker在3.14下rc1(3.12验证过的脚本被父进程版本绑架)。worker
    # 一律优先系统python3.12(全部adapter在此验证)·找不到才回退sys.executable。
    _stable_py = shutil.which("python3.12") or sys.executable
    context = {"source": str(root), "workdir": workdir or str(root), "python": _stable_py,
               "repo_root": str(repo_root), "node": node, "npm_cli": npm_cli,
               "ironclaw_token": ironclaw_token,
               "runtime_dir": str(runtime_dir), "runtime_bin": str(runtime_bin),
               "runtime_python": str(venv_bin / ("python.exe" if os.name == "nt" else "python")),
               "adapter_dir": str(HERE / "external_adapters")}
    if deployed is not None:
        for name in ("source", "runtime_dir", "runtime_bin", "runtime_python"):
            if values and name in values and values[name] != context[name]:
                raise ValueError(f"deployment context cannot override {name}")
    return {**context, **(values or {})}


def _session_socket_dir(workdir: str, key: str, session_id: str) -> str:
    """Short private IPC address; persistent session data stays in the workspace."""
    import hashlib
    import stat
    if os.name == "nt":
        raise ValueError("session socket directory requires a Unix execution target")
    identity = json.dumps([os.path.realpath(workdir), key, session_id], ensure_ascii=False)
    digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
    path = pathlib.Path("/tmp") / f"meyo-ipc-{os.getuid()}-{digest}"
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("unsafe session socket directory")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("session socket directory is not private")
    return str(path)


def _format_argv(template: list[str], item: dict, workdir: str = "",
                 values: dict | None = None) -> list[str]:
    context = _context(item, workdir, values)
    if any("{session_socket_dir}" in str(part) for part in template):
        context["session_socket_dir"] = _session_socket_dir(
            context["workdir"], item["key"], str(context.get("session_id") or "external"))
    command = [str(part).format(**context) for part in template]
    if command and command[0] == "node":
        command[0] = context["node"]
    if command and not os.path.isabs(command[0]):
        command[0] = shutil.which(command[0]) or command[0]
    return command


def _safe_step(command: list[str]) -> bool:
    if not command:
        return False
    executable = pathlib.Path(str(command[0]))
    name = executable.name
    if name in _ALLOWED_EXECUTABLES:
        return True
    # 声明式 runtime 允许执行自己隔离目录内的解释器/CLI；不能放行任意绝对路径。
    try:
        runtime_root = pathlib.Path(_paths.under("external_agents", "runtimes")).resolve()
        if executable.is_absolute() and executable.resolve().is_relative_to(runtime_root):
            return True
        # 仓库根的 node_modules 内解释器(2026-09-02 grok 白名单误拒根修):db29be29 起项目根
        # node_modules 装了可移植 node@24({node} 占位在某些环境解析到这里)。它跟 runtime 目录
        # 一样是【自家声明式装出来的】,不是任意外部绝对路径——只放行 node_modules 下的解释器,
        # 不放行仓库根其它路径。
        _repo_root = HERE.parents[1]
        if (executable.is_absolute()
                and "/node_modules/" in str(executable.resolve())
                and executable.resolve().is_relative_to(_repo_root)):
            return True
        return False
    except (OSError, ValueError):
        return False


_PROBE_STOP = threading.Event()


def stop_probes() -> None:
    """Cancel background inspection during server shutdown; never assert success."""
    _PROBE_STOP.set()


def _git_revision(root: pathlib.Path) -> str:
    if _PROBE_STOP.is_set():
        return ""
    if not (root / ".git").exists():
        # 源码归档没有伪造 .git；校验导出时记录的实际文件后识别固定提交。
        marker = root / ".meyo-source.json"
        if marker.is_file() and not marker.is_symlink():
            try:
                import hashlib
                data = json.loads(marker.read_text())
                revision = str(data.get("revision") or "")
                files = data.get("files")
                if not re.fullmatch(r"[0-9a-f]{40}", revision) or not isinstance(files, dict) or not files:
                    return ""
                for relative, record in files.items():
                    if _PROBE_STOP.is_set():
                        return ""
                    path = root / relative
                    if not path.resolve().is_relative_to(root.resolve()):
                        return ""
                    if "symlink" in record:
                        if not path.is_symlink() or os.readlink(path) != record["symlink"]:
                            return ""
                    else:
                        if path.is_symlink() or not path.is_file():
                            return ""
                        with path.open("rb") as f:
                            if hashlib.file_digest(f, "sha256").hexdigest() != record.get("sha256"):
                                return ""
                return revision
            except (OSError, ValueError, TypeError, KeyError):
                return ""
        return ""
    try:
        return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                              text=True, timeout=15).stdout.strip()
    except Exception:
        return ""




def _node_bin_dirs(item: dict) -> list:
    """声明 runtime.node_binary 的家(kimi-code/openclaw/grok-bot):返回自带 node 所在目录。

    install/probe 的 PATH 前置它——系统 node 常不满足各家 engines(kimi-code 要 >=24.15,
    系统 v22 直接 ERR_PNPM_UNSUPPORTED_ENGINE·pnpm build 全挂)。"""
    configured = str((item.get("runtime") or {}).get("node_binary") or "")
    if not configured:
        return []
    nb = pathlib.Path(configured.replace("{repo_root}", str(HERE.parents[1])))
    return [nb.parent] if nb.is_file() else []



def _strip_dead_proxy(env: dict) -> dict:
    """探测 HTTP(S)_PROXY 指向的代理是否活着;死的直接清掉(死代理会让 git/npm 卡死几分钟)。

    CLAUDE.md 血泪教训:军团 rc1 先查环境代理。本机常见 HTTP_PROXY=127.0.0.1:xxxx 指向
    没起的代理→git fetch 直接连不上、npm 装 14 分钟卡死。这里 1s 探测,连不通就当没配。
    """
    import socket
    from urllib.parse import urlparse
    out = dict(env)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        value = (out.get(name) or "").strip()
        if not value:
            continue
        try:
            parsed = urlparse(value if "//" in value else "//" + value)
            host, port = parsed.hostname or "127.0.0.1", parsed.port or 80
            with socket.create_connection((host, port), timeout=1):
                pass  # 代理活着,保留
        except Exception:
            out.pop(name, None)  # 死代理,清掉
    return out


def _http_server_up(url: str, timeout: float = 1.0) -> bool:
    """探一个 http transport 的目标端口是否有进程 listen(server 起没起)。

    速度底线(CLAUDE.md 铁律0):http-transport 的家如果 server 没启动,真派活会
    Connection refused,整单要干等它 fail(~87s)才补位——直接拖慢速度。所以 probe
    阶段就 TCP 探端口:探不通 = server 未起 = execution_ready 置 False,别让它被误派。
    只探端口(不发业务请求),轻量不触发一次真对话。
    """
    import socket
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url if "//" in url else "//" + url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _credentials(item: dict, env: dict[str, str] | None = None, *,
                 inherit_environment: bool = True) -> dict:
    merged = ({**os.environ, **(env or {})} if inherit_environment else dict(env or {}))
    # 所有外部成员默认复用军团统一 AIGC 网关。与内置 harness 一样，从统一模型目录
    # 取得 appid；只有项目明确不兼容该网关时才要求自己的账号。
    if inherit_environment and not merged.get("MEYO_UNIFIED_APPID"):
        try:
            catalog = json.loads((pathlib.Path.home() / ".claude/skills/aigc-model-router/models.json").read_text())
            candidates = (catalog.get("programming") or {}).get("candidates") or []
            appids = candidates[0].get("appids") or [] if candidates else []
            if appids:
                merged["MEYO_UNIFIED_APPID"] = str(appids[0])
        except Exception:
            pass
    spec = item.get("credentials") or {}
    any_of = [str(name) for name in spec.get("any_of") or []]
    all_of = [str(name) for name in spec.get("all_of") or []]
    missing_all = [name for name in all_of if not merged.get(name)]
    any_ok = not any_of or any(merged.get(name) for name in any_of)
    missing = missing_all + ([] if any_ok else any_of)
    return {"ready": not missing, "missing": missing,
            "accepted": sorted(set(any_of + all_of)),
            "signup_url": spec.get("signup_url"), "help": spec.get("help", "")}


def _run_probe_command(command: list[str], cwd: pathlib.Path, timeout_s: int = 45,
                       env: dict[str, str] | None = None) -> tuple[bool, str]:
    """探活子进程必须自立进程组并挂注册表账本(血泪 2026-09-06:
    subprocess.run 超时只杀父进程,make→docker info 等孙进程逃逸成孤儿,
    Docker 不健康时探活挂死堆积→CPU 打满机器卡死)。
    超时/异常时 killpg 整组回收,绝不留孙进程。"""
    try:
        proc = subprocess.Popen(command, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, start_new_session=True)
    except Exception as exc:
        return False, f"{type(exc).__name__}:{exc}"
    try:
        try:
            import legion_proc_registry as _preg
            _preg.register(proc.pid, pgid=proc.pid, owner=os.getpid(),
                           comm=command[0] if command else "probe")
        except Exception:
            pass
        try:
            out, _ = proc.communicate(timeout=max(1, int(timeout_s)))
        except subprocess.TimeoutExpired:
            _kill_proc_tree(proc)
            return False, f"timeout after {timeout_s}s (process group killed)"
        return proc.returncode == 0, (out or "")[-2000:]
    finally:
        try:
            import legion_proc_registry as _preg
            _preg.unregister(proc.pid)
        except Exception:
            pass


def _kill_proc_tree(proc: subprocess.Popen) -> None:
    """按进程组整组终止(SIGTERM 宽限→SIGKILL),不留任何孙进程逃逸者。"""
    import signal
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except Exception:
            return
        try:
            proc.wait(timeout=5)
            return
        except Exception:
            continue
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def _probe_one(item: dict, env: dict[str, str] | None = None, *,
               inherit_environment: bool = True) -> dict:
    if _PROBE_STOP.is_set():
        return {"key": item["key"], "available": False, "status": "cancelled",
                "reason": "server shutdown cancelled inspection", "revision_ok": False,
                "execution_ready": False, "runnable": False,
                "status_detail": "server shutdown cancelled inspection"}
    root = None
    source_present = False
    try:
        root = source_dir(item)
        source_present = root.is_dir()
        runtime_dir = pathlib.Path(_context(item)["runtime_dir"])
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        # An unprovisioned/invalid optional release is one unavailable member,
        # not a catalogue-wide failure. Keep the resolvers strict; never fall back.
        missing = isinstance(exc, FileNotFoundError) or isinstance(exc.__cause__, FileNotFoundError)
        return {
            "key": item["key"], "name": item.get("name"), "kind": item.get("kind", "agent"),
            "url": item.get("url"), "version": item.get("version"), "revision": "",
            "locked_revision": item.get("revision"), "revision_ok": False,
            "source": str(root) if root is not None else "", "source_present": source_present,
            "installed": False, "available": False, "execution_ready": False, "runnable": False,
            "status": "not_installed" if missing else "build_failed",
            "status_detail": f"runtime path configuration failed: {type(exc).__name__}: {exc}"[:500],
            "probe": {"method": str((item.get("probe") or {}).get("method") or ""), "target": None},
            "credentials": _credentials(item, env, inherit_environment=inherit_environment),
            "protocols": [x.get("type") for x in item.get("transports") or []],
            "execution_role": item.get("execution_role", "agent"),
            "provenance": item.get("provenance") or {"official": True},
            "capabilities": item.get("capabilities") or [], "updated_at": None,
        }
    revision = _git_revision(root)
    revision_ok = bool(source_present and revision == str(item.get("revision") or ""))
    probe = item.get("probe") or {}
    method = str(probe.get("method") or "")
    target = str(probe.get("target") or "")
    available = False
    detail = ""
    resolved: Any = target
    probe_env = ({**os.environ, **(env or {})} if inherit_environment else dict(env or {}))
    probe_bins = _node_bin_dirs(item) + [runtime_dir / "venv" / ("Scripts" if os.name == "nt" else "bin"),
                                        runtime_dir / "node_modules" / ".bin", runtime_dir / "bin"]
    probe_env["PATH"] = os.pathsep.join(str(path) for path in probe_bins) + os.pathsep + probe_env.get("PATH", "")
    if method == "cli":
        rendered_target = _format_argv([target], item)[0] if target else ""
        # probe 假阴性修复(军团 codex dogfood 揪出):shutil.which 默认查父进程 PATH,
        # 忽略了上面 :205 刚拼进的 runtime venv/bin、node_modules/.bin——已装在隔离 runtime 的
        # 裸 CLI 名会被误报未安装→build_failed。传 path=probe_env["PATH"] 让它查 runtime PATH。
        resolved = rendered_target if os.path.isfile(rendered_target) else shutil.which(rendered_target, path=probe_env["PATH"])
        available = bool(resolved)
        if available and probe.get("command"):
            available, detail = _run_probe_command(_format_argv(probe["command"], item), root, env=probe_env)
    elif method in ("command", "build"):
        command = _format_argv(probe.get("command") or [], item)
        resolved = command
        if method == "build" and not command:
            available = revision_ok
        elif command and _safe_step(command):
            available, detail = _run_probe_command(command, root, env=probe_env)
    elif method == "path":
        # 支持完整 context 占位符({source}/{runtime_dir}/{runtime_bin}…),不只 {source}。
        # (ironclaw 等 cargo 装到 {runtime_dir}/bin 的家 probe 要用 runtime_dir。)
        rendered = _format_argv([target], item)[0] if target else ""
        path = pathlib.Path(rendered)
        available = path.exists()
        resolved = str(path)
    elif method == "python_import":
        try:
            import importlib.util
            available = importlib.util.find_spec(target) is not None
        except Exception:
            available = False
    elif method == "http":
        try:
            rendered_target = _format_argv([target], item)[0]
            resolved = rendered_target
            # 死代理防劫持(2026-09-02 红灯根修):urllib 默认尊重 HTTP(S)_PROXY——本机回环
            # 健康探测(127.0.0.1:3100)被死代理劫持会报 Connection refused,把"server 活着"
            # 误判成 build_failed。①先 strip 死代理(同 install/run 语义对称);②回环地址
            # 永远直连(装 OpenerDirector 显式 no_proxy)——本机 server 走代理没有任何正当理由。
            _pe = _strip_dead_proxy(probe_env)
            _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with _opener.open(rendered_target, timeout=5) as response:
                available = response.status < 500
                detail = f"HTTP {response.status}"
        except Exception as exc:
            detail = f"{type(exc).__name__}:{exc}"
    credentials = _credentials(item, env, inherit_environment=inherit_environment)
    has_transport = bool(item.get("transports"))
    executable_role = item.get("execution_role") not in ("control-plane", "ui-platform", "capability")
    execution_ready = bool(revision_ok and available and credentials["ready"] and has_transport and executable_role)
    # 平台不匹配如实上报(2026-09-02 红灯清单根修):catalog 声明 platform_target(如
    # cloud-windows-vm 的 WPS/HASS 家)而本机是 darwin——按设计【本机不装不 probe】,旧逻辑
    # 把它们报 not_installed/build_failed 混进红灯名单,和真失败分不清。部署目标=云 Windows
    # 真机·状态如实标 platform_mismatch(不冒充 ready·也不算失败),UI/体检不再误读。
    _platform_mismatch = False
    _pt = str(item.get("platform_target") or "").strip()
    if _pt and _pt not in ("local", "") and _pt != _current_platform_target():
        _platform_mismatch = True
        execution_ready = False
    if revision_ok and available and not credentials["ready"]:
        execution_ready = False
    # 速度底线(铁律0):http-transport 家若 server 端口没起→真派活会 Connection refused,
    # 整单干等它 fail 才补位。probe 阶段就探端口:探不通就如实标未就绪,不被误派拖慢整单。
    # (probe.method=http 的家已在上面探过健康端点,这里只补 method≠http 但走 http transport 的家。)
    if execution_ready and method != "http":
        _http_ts = [t for t in (item.get("transports") or []) if t.get("type") == "http" and t.get("url")]
        if _http_ts:
            _url = _format_argv([_http_ts[0]["url"]], item)[0]
            if not _http_server_up(_url):
                execution_ready = False
                available = False
                detail = f"http server 未启动:{_url}"
    persisted = _read_state().get(item["key"]) or {}
    # install() 内部完成后要看实时探针结果，不能被自己刚写下的 installing 永久遮住。
    # 只有跨调用查询时，持久状态才用于向 UI 暴露仍在进行的安装。
    # installing 卡死修复(paperclip 红灯根因):旧判据只看探针没过就保持 installing——
    # 但安装步骤可能早已全部 rc=0 完成,只是 http server(如 docker compose 服务)没起。
    # 这样 UI 永远显示"安装中",实际是 server 未启动的结构性问题被"正在装"掩盖。
    # 修:installing 只在「真有安装进程在跑」的时间窗口内保持(updated_at 距今 < 30min
    # 且 install 进程存在);窗口外如实落 build_failed,detail 指明 server 未启动。
    _install_alive = False
    _was_stale_installing = persisted.get("status") == "installing"
    if _was_stale_installing:
        _fresh = (time.time() - float(persisted.get("updated_at") or 0)) < 1800
        if _fresh and not (revision_ok and available):
            status = "installing"
            _install_alive = True
    if _platform_mismatch:
        status = "platform_mismatch"
        detail = detail or f"部署目标 {_pt}(本机 { _current_platform_target() })·按设计不在本机安装"
    elif _install_alive:
        pass  # 真有安装进程在跑(新鲜 installing 且探针未过):保持 installing,不走后续判定
    elif not source_present and not available:
        status = "not_installed"
    elif not revision_ok:
        status = "build_failed"
        detail = detail or f"revision mismatch:{revision or 'not a git checkout'}"
    elif not available:
        # 凭证和构建是两条正交状态：源码在但可执行探针没过，必须先报构建/服务问题；
        # 不能因同时缺 key 就把“CLI 根本没生成”掩盖成 missing_credentials。
        status = "build_failed"
        if _was_stale_installing and not _install_alive:
            # 安装早已结束(窗口外)但探针仍不过:如实暴露,不让 UI 无限转圈"安装中"。
            detail = detail or f"安装步骤已完成但探针未通过(server 未启动?):{persisted.get('error', '')[:120]}"
        else:
            detail = detail or persisted.get("error", "运行探针未通过")
    elif not credentials["ready"]:
        status = "missing_credentials"
    elif available:
        status = "ready" if (execution_ready or not executable_role) else "build_failed"
    else:
        status = "build_failed"
    return {
        "key": item["key"], "name": item.get("name"), "kind": item.get("kind", "agent"),
        "url": item.get("url"), "version": item.get("version"), "revision": revision,
        "locked_revision": item.get("revision"), "revision_ok": revision_ok,
        "source": str(root), "source_present": source_present, "installed": revision_ok,
        "available": available, "execution_ready": execution_ready, "runnable": execution_ready,
        "status": status, "status_detail": detail or persisted.get("error", ""),
        "probe": {"method": method, "target": resolved}, "credentials": credentials,
        "protocols": [x.get("type") for x in item.get("transports") or []],
        "execution_role": item.get("execution_role", "agent"),
        "provenance": item.get("provenance") or {"official": True},
        "capabilities": item.get("capabilities") or [], "updated_at": persisted.get("updated_at"),
    }


# 升级检查结果 TTL 缓存(深挖揪出:check_updates 对每个 github_release agent 调
# latest_github_release·后者最多 3 次 urlopen×30s→单 agent 可挂 90s·多 agent 叠加→
# 端点挂 25s+·还撞 GitHub 限流)。GitHub release 秒级不变·10min 缓存足够;和 capabilities/
# install 同一"慢网络探测必须缓存"模式。按 key 分桶(空 key=全量各存各的)。
_UPDATES_CACHE: dict = {}
_UPDATES_TTL = 600.0


def invalidate_updates_cache() -> None:
    _UPDATES_CACHE.clear()


def check_updates(key: str = "", force: bool = False) -> dict:
    ck = key or "__all__"
    if not force:
        hit = _UPDATES_CACHE.get(ck)
        if hit and time.time() - hit["ts"] < _UPDATES_TTL:
            return {**hit["data"], "from_cache": True}
    rows = []
    for item in load_catalog():
        if key and item["key"] != key:
            continue
        update = item.get("update") or {}
        strategy = update.get("strategy")
        if strategy == "github_release":
            latest = agent_manifest.latest_github_release(str(item.get("url") or ""))
        elif strategy == "github_commit":
            # locked-main 家:查分支 HEAD(单次请求·不吃 release 的 3 连配额)
            latest = agent_manifest.latest_branch_commit(str(item.get("url") or ""),
                                                         str(update.get("branch") or "main"))
        else:
            rows.append({"key": item["key"], "ok": False, "status": "unsupported"})
            continue
        rows.append({"key": item["key"], "name": item.get("name"), "current_version": item.get("version"),
                     "current_revision": item.get("revision"), "latest_version": latest.get("tag"),
                     "latest_revision": latest.get("revision"), "release_url": latest.get("release_url"),
                     "published_at": latest.get("published_at"), "ok": latest.get("ok", False),
                     "update_available": bool(latest.get("ok") and latest.get("revision") != item.get("revision")),
                     "error": latest.get("error", "")})
    data = {"updates": rows, "summary": {"checked": len(rows),
            "available": sum(row.get("update_available", False) for row in rows),
            "failed": sum(not row.get("ok", False) for row in rows)}}
    _UPDATES_CACHE[ck] = {"data": data, "ts": time.time()}
    return {**data, "from_cache": False}


# 全量 probe TTL 缓存(顶级测试 dogfood 揪出:list_harnesses 每次实时探活15个外部agent→6.5s→
# 高频只读端点首页加载慢+并发雪崩超时)。15家CLI/HTTP探活是重操作·状态秒级不变·30s TTL 足够;
# 安装/卸载后主动 invalidate 让新状态立即可见(和 toolbox 缓存同思路·理念:缓存要能失效)。
_PROBE_CACHE: dict = {"data": None, "ts": 0.0}
_PROBE_TTL = float(os.environ.get("LEGION_EXT_PROBE_TTL", "30"))


def invalidate_probe_cache() -> None:
    _PROBE_CACHE["data"] = None
    _PROBE_CACHE["ts"] = 0.0


def probe(key: str = "", env: dict[str, str] | None = None) -> dict:
    items = load_catalog()
    if key:
        # 单家探活不走缓存(装/卸后即时验证需要真实结果)
        item = next((x for x in items if x["key"] == key), None)
        return _probe_one(item, env) if item else {"ok": False, "error": f"unknown external agent:{key}"}
    # 全量探活:命中 TTL 内缓存直接返回(env 定制时不缓存·避免串味)
    if env is None and not _PROBE_STOP.is_set():
        import time as _t
        if _PROBE_CACHE["data"] is not None and (_t.time() - _PROBE_CACHE["ts"]) < _PROBE_TTL:
            return _PROBE_CACHE["data"]
    rows = [_probe_one(item, env) for item in items]
    counts = {name: sum(row["status"] == name for row in rows) for name in
              ("not_installed", "installing", "ready", "missing_credentials", "build_failed",
               "platform_mismatch")}
    out = {"agents": rows, "summary": {"total": len(rows), **counts,
           "execution_ready": sum(row["execution_ready"] for row in rows)}}
    if env is None:
        import time as _t
        _PROBE_CACHE["data"] = out
        _PROBE_CACHE["ts"] = _t.time()
    return out


def _docker_daemon_dead_proxy() -> str:
    """构建期死代理体检(docker daemon 层):docker build 走 VM/daemon 的 proxy 配置,
    不吃宿主机 _strip_dead_proxy。daemon 代理死了会导致所有镜像拉取 proxyconnect refused
    (paperclip 红灯真根因:colima systemd drop-in 里残留旧代理 192.168.5.2:15236)。

    返回人话诊断串;空串=健康或未用 docker。修法提示:colima VM 里
    /etc/systemd/system/docker.service.d/proxy.conf 的 Environment=HTTP(S)_PROXY 指向死端口
    → 删该文件 + daemon-reload + systemctl restart docker。
    """
    import subprocess as _sp
    try:
        info = _sp.run(["docker", "info", "--format",
                        "{{.HTTPProxy}}|{{.HTTPSProxy}}"], capture_output=True, text=True, timeout=8)
        if info.returncode != 0:
            return ""  # docker 不可用另有 install 步骤报错,这里不抢
        import socket as _sk
        from urllib.parse import urlparse as _up
        dead = []
        for value in (info.stdout or "").strip().split("|"):
            value = value.strip()
            if not value:
                continue
            try:
                parsed = _up.parse_url(value) if hasattr(_up, "parse_url") else _up.urlparse(value if "//" in value else "//" + value)
                host, port = parsed.hostname or "127.0.0.1", parsed.port or 80
                with _sk.create_connection((host, port), timeout=1):
                    pass
            except Exception:
                # VM 视角代理地址(如 192.168.5.2)在宿主机上探不通是正常的——只报「VM 内 daemon 配了代理」
                dead.append(value)
        if dead:
            return ("docker daemon 配置了不可达代理:" + ",".join(dead) +
                    ";镜像拉取会 proxyconnect refused。修:colima ssh 删 /etc/systemd/system/docker.service.d/proxy.conf "
                    "后 systemctl daemon-reload && systemctl restart docker(2026-09-02 paperclip 红灯实测)")
        return ""
    except Exception:
        return ""


# install 进程内体检缓存:同一轮 install 多个 docker 步骤只体检一次 daemon(别反复 8s)。
_install_env_cache: dict = {}


def _run_install_step(command, cwd, env, timeout_s, key, index):
    """Stream installation evidence to disk and reclaim the entire timed-out process group."""
    import legion_proc_registry
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = _LOG_DIR / f"{_safe_key(key)}-{time.time_ns()}-step{index}.log"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    timed_out = False
    with os.fdopen(fd, "wb") as output:
        proc = subprocess.Popen(command, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                                stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            if not legion_proc_registry.register(proc.pid, owner=os.getpid(),
                                                 meta={"kind": "harness-install", "key": key}):
                raise RuntimeError("installation process registration failed")
            try:
                proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_proc_tree(proc)
        finally:
            if proc.poll() is None:
                _kill_proc_tree(proc)
            legion_proc_registry.unregister(proc.pid)
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - 6000))
        tail = stream.read().decode("utf-8", "replace")
    return {"command": command, "exit_code": proc.returncode, "stdout": tail,
            "stderr": "", "timed_out": timed_out, "log_path": str(path)}


def probe_build(item: dict) -> dict:
    """Verify declared build outputs without claiming the native service is running."""
    declaration = item.get("build_probe")
    if declaration is None:
        return _probe_one(item)
    root = source_dir(item)
    revision_ok = _git_revision(root) == str(item.get("revision") or "")
    command = _format_argv(declaration.get("command") or [], item)
    if declaration.get("method") != "command" or not command or not _safe_step(command):
        return {"available": False, "revision_ok": revision_ok, "status": "build_failed",
                "status_detail": "invalid build probe", "runtime_acceptance": "not_run"}
    available, detail = _run_probe_command(command, root)
    return {"available": available, "revision_ok": revision_ok,
            "status": "built" if available and revision_ok else "build_failed",
            "status_detail": detail, "runtime_acceptance": "not_run"}


def _published_runtime_mutation(item: dict) -> dict | None:
    """Keep install/update writes out of an attached, fixed runtime generation."""
    deployed = _deployment_paths(item)
    paths = deployed or (source_dir(item).resolve(),
                         pathlib.Path(_context(item)["runtime_dir"]).resolve())
    roots = [deployed[0].parent] if deployed is not None else []
    # Another spec/physical alias must not turn a declared release back into an
    # install workspace. Reuse the existing catalog, not a second registry.
    for declared in load_catalog():
        try:
            published = _deployment_paths(declared, require_pair=False)
        except ValueError:
            # The selected item was checked above; unrelated broken bindings
            # must not redirect it or change its legacy path selection.
            continue
        if published is not None:
            roots.append(published[0].parent)
    if os.environ.get("MEYO_RUNTIME_ROOT"):
        # attach may point at a verified old extraction, outside the marker's
        # reference generation. Its physical root is still protected.
        roots.append(pathlib.Path(os.environ["MEYO_RUNTIME_ROOT"]).resolve())
    for path in paths:
        for ancestor in path.parents:
            if (ancestor / ".materialized.json").is_file():
                roots.append((ancestor / "runtime").resolve())
    if any(path.is_relative_to(root) for path in paths for root in roots):
        return {"ok": False, "status": "requires_release_update", "key": item["key"],
                "revision": item.get("revision"), "runtime_acceptance": "not_run",
                "error": "该成员属于已发布的固定运行层，不能原地重装；"
                         "请在独立构建副本修复或升级，再通过发布流程更新。",
                "source_preserved": True}
    return None


def install(key: str, confirm: bool = False, timeout_s: int = 1800,
            build_only: bool = False) -> dict:
    item = next((x for x in load_catalog() if x["key"] == key), None)
    if not item:
        return {"ok": False, "error": f"unknown external agent:{key}"}
    url = str(item.get("url") or "")
    revision = str(item.get("revision") or "")
    if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", url):
        return {"ok": False, "status": "build_failed", "error": "不是允许的 GitHub URL"}
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        return {"ok": False, "status": "build_failed", "error": "缺不可变 revision"}
    immutable = _published_runtime_mutation(item)
    if immutable:
        return immutable
    destination = source_dir(item)
    runtime_dir = pathlib.Path(_context(item)["runtime_dir"])
    bundled = (destination / ".meyo-source.json").is_file() and not (destination / ".git").exists()
    if bundled and _git_revision(destination) != revision:
        return {"ok": False, "status": "build_failed", "key": key,
                "error": "bundled source verification failed; source preserved"}
    commands = [] if bundled else [["git", "clone", "--filter=blob:none", "--no-checkout", url + ".git", str(destination)],
                ["git", "-C", str(destination), "fetch", "--depth", "1", "origin", revision],
                ["git", "-C", str(destination), "checkout", "--detach", revision]]
    commands.extend(_format_argv(step, item) for step in item.get("install") or [])
    if not confirm:
        return {"ok": True, "status": "planned", "key": key, "commands": commands,
                "destination": str(destination), "version": item.get("version"),
                "revision": revision}
    _write_state(key, {"status": "installing", "phase": "clone", "error": ""})
    destination.parent.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    logs = []
    try:
        if not bundled and destination.exists() and _git_revision(destination) != revision:
            shutil.rmtree(destination)
        for index, command in enumerate(commands):
            if not bundled and index == 0 and destination.exists():
                continue
            if not _safe_step(command):
                raise RuntimeError(f"安装命令不在白名单:{command[0] if command else ''}")
            _write_state(key, {"status": "installing", "phase": f"step_{index + 1}"})
            # 构建期 docker 死代理预检(docker 类步骤):daemon 层代理不吃 _strip_dead_proxy,
            # 死了会让 docker build/pull 全部 proxyconnect refused 卡满 timeout。提前一次 8s 体检,
            # 带人话修法直接报给调用方,别让它烧完 30min 才见 refused(铁律0:谁挡速度底线就先揪出来)。
            _step_name = pathlib.Path(str(command[0])).name
            if _step_name in ("docker", "make") and "docker" not in _install_env_cache:
                _install_env_cache["docker"] = _docker_daemon_dead_proxy()
            if _install_env_cache.get("docker"):
                raise RuntimeError(_install_env_cache["docker"])
            step_env = _strip_dead_proxy(os.environ)
            install_env = item.get("install_env") or {}
            if not isinstance(install_env, dict) or any(not isinstance(k, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) or not isinstance(v, str) or "\x00" in v for k, v in install_env.items()):
                raise ValueError("invalid install_env")
            step_env.update(install_env)
            # Build each member against its own interpreter, not the server vendor
            # directory. Otherwise pip treats unrelated global packages as installed.
            step_env.pop("PYTHONPATH", None)
            step_env.pop("PYTHONHOME", None)
            step_env["PYTHONNOUSERSITE"] = "1"
            ctx = _context(item)
            if (item.get("runtime") or {}).get("pnpm"):
                from external_adapters.pnpm_build import corepack_build_env
                step_env = corepack_build_env(runtime_dir, step_env)
            bins = _node_bin_dirs(item) + [runtime_dir / "venv" / ("Scripts" if os.name == "nt" else "bin"),
                                          runtime_dir / "node_modules" / ".bin", runtime_dir / "bin"]
            step_env["PATH"] = os.pathsep.join(str(path) for path in bins) + os.pathsep + step_env.get("PATH", "")
            result = _run_install_step(command, destination if destination.exists() else destination.parent,
                                       step_env, timeout_s, key, index + 1)
            logs.append(result)
            if result.get("timed_out"):
                raise RuntimeError(f"install step timed out after {timeout_s}s; log={result['log_path']}")
            if result["exit_code"] != 0:
                raise RuntimeError((result["stderr"] or result["stdout"] or f"exit {result['exit_code']}")[-1000:])
        if build_only and item.get("build_probe") is not None:
            state = probe_build(item)
            ok = bool(state.get("revision_ok") and state.get("available"))
            status = "built" if ok else "build_failed"
            _write_state(key, {"status": status, "phase": "build_probe",
                               "error": "" if ok else state.get("status_detail", "")})
            _LOG_DIR.mkdir(parents=True, exist_ok=True)
            log_path = _LOG_DIR / f"{_safe_key(key)}-{time.time_ns()}.json"
            log_path.write_text(json.dumps(logs, ensure_ascii=False, indent=2), encoding="utf-8")
            return {"ok": ok, "status": status, "key": key, "revision": revision,
                    "probe": state, "log_path": str(log_path), "runtime_acceptance": "not_run"}
        state = _probe_one(item)
        # http 型家收尾重试等待(2026-09-02 paperclip 根修):install 最后一步刚把 server
        # 守护起来,heavy 家(embedded postgres 初始化+迁移)就绪可能晚于 daemon 的等待窗口。
        # 收尾 probe 按 3s 步进重试至多 60s——daemon 已确保进程活着,这里只是等它把
        # "listening" 说出口;60s 仍不绿就如实落 build_failed(不无限等)。
        if str((item.get("probe") or {}).get("method")) == "http" and not state.get("available"):
            import time as _tw
            _deadline = _tw.time() + 60
            while _tw.time() < _deadline and not state.get("available"):
                _tw.sleep(3)
                state = _probe_one(item)
        status = state["status"]
        # 安装成功与执行就绪是两件事：依赖和探针已过、只缺账号时安装本身成功，
        # UI 如实显示 missing_credentials；不能把“缺凭证”误报成构建失败。
        install_complete = state["revision_ok"] and state["available"]
        ok = install_complete and status in ("ready", "missing_credentials")
        _write_state(key, {"status": status, "phase": "probe", "error": state.get("status_detail", "")})
        state = _probe_one(item)
    except Exception as exc:
        ok, status = False, "build_failed"
        _write_state(key, {"status": status, "phase": "install", "error": f"{type(exc).__name__}:{exc}"})
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = _LOG_DIR / f"{_safe_key(key)}-{int(time.time())}.json"
    log_path.write_text(json.dumps(logs, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": ok, "status": status, "key": key, "destination": str(destination),
            "version": item.get("version"), "revision": revision, "probe": _probe_one(item),
            "log_path": str(log_path), "error": "" if ok else (_read_state().get(key) or {}).get("error", "")}


def capability_actions(key: str = "", *, catalog: list[dict] | None = None) -> dict:
    """List declarative actions exposed by non-answer external capabilities."""
    rows = []
    for item in (load_catalog() if catalog is None else catalog):
        if key and item["key"] != key:
            continue
        for name, action in (item.get("capability_actions") or {}).items():
            if not isinstance(action, dict):
                continue
            if ("transport" in action
                    and (action.get("command") or not is_readonly_loopback_http(action["transport"]))
                    or "transport" not in action and not action.get("command")):
                continue
            rows.append({
                "id": f"external_capability:{item['key']}:{name}",
                "key": item["key"], "action": name,
                "description": action.get("description") or name,
                "params": action.get("params") or [],
                "capabilities": item.get("capabilities") or [],
            })
    return {"ok": True, "actions": rows, "count": len(rows)}


def _readonly_capability_instance(key: str, env: dict | None,
                                  isolated_environment: bool) -> tuple[str, dict | None]:
    """Read only this caller's explicit private namespace, never another profile."""
    import stat

    missing = {"ok": False, "status": "unconfigured", "configured": False,
               "code": "instance_unconfigured", "error": "No private instance URL is configured."}
    invalid = {"ok": False, "status": "invalid_config", "configured": False,
               "code": "instance_config_invalid", "error": "Invalid private instance configuration."}
    scope = dict(env or {}) if env is not None or isolated_environment else os.environ
    root = scope.get("MEYO_LEGION_DIR")
    if not root:
        return "", missing
    if (not isinstance(root, str) or root != root.strip()
            or any(ord(char) < 32 for char in root) or not pathlib.Path(root).is_absolute()):
        return "", invalid
    path = pathlib.Path(root) / "external_agents" / "instances.json"
    try:
        if path.parent.is_symlink() or path.is_symlink():
            return "", invalid
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                    or hasattr(os, "getuid") and info.st_uid != os.getuid()):
                return "", invalid
            raw = stream.read(65_537)
        if len(raw) > 65_536:
            return "", invalid
        document = json.loads(raw)
        if not isinstance(document, dict):
            return "", invalid
        row = document.get(key)
        if row is None:
            return "", missing
        if not isinstance(row, dict) or set(row) - {"url"}:
            return "", invalid
        url = row.get("url")
        if url in (None, ""):
            return "", missing
        if not isinstance(url, str):
            return "", invalid
        return url, None
    except FileNotFoundError:
        return "", missing
    except (OSError, ValueError, UnicodeError):
        return "", invalid


def _run_readonly_http_capability(key: str, action: str, contract: dict, params: dict,
                                  timeout_s: float | None, env: dict | None,
                                  isolated_environment: bool) -> dict:
    identity = {"key": key, "action": action}
    if contract.get("command") or not is_readonly_loopback_http(contract.get("transport")):
        return {**identity, "ok": False, "status": "invalid_config",
                "code": "invalid_readonly_http_contract", "error": "Invalid read-only HTTP declaration."}
    if params:
        return {**identity, "ok": False, "status": "invalid_request",
                "code": "readonly_parameters_not_allowed",
                "error": "Read-only instance actions do not accept endpoint, credential or method overrides."}
    try:
        configured_limit = float(contract.get("timeout_s", 5))
        limit = min(configured_limit, float(timeout_s)) if timeout_s is not None else configured_limit
        if not 0 < limit <= 30:
            raise ValueError()
    except (TypeError, ValueError):
        return {**identity, "ok": False, "status": "invalid_request",
                "code": "invalid_timeout", "error": "Invalid read-only HTTP timeout."}
    url, problem = _readonly_capability_instance(key, env, isolated_environment)
    if problem:
        return {**identity, **problem}
    result = run_transport(contract["transport"], {"instance_url": url}, {}, limit)
    return {**identity, **result, "ok": result.get("status") == "ok"}


def run_capability(key: str, action: str, params: dict | None = None,
                   timeout_s: int | None = None, env: dict[str, str] | None = None,
                   workdir: str = "", isolated_environment: bool = False) -> dict:
    """Run one catalog-declared capability action without shell interpolation."""
    item = next((x for x in load_catalog() if x["key"] == key), None)
    if not item:
        return {"ok": False, "error": f"unknown external capability:{key}"}
    actions = item.get("capability_actions") or {}
    contract = actions.get(action)
    if not isinstance(contract, dict):
        return {"ok": False, "error": f"unknown capability action:{key}:{action}"}
    params = dict(params or {})
    if "transport" in contract:
        # An already-running local API needs neither an install probe nor model credentials.
        return _run_readonly_http_capability(
            key, action, contract, params, timeout_s, env, isolated_environment)
    if not contract.get("command"):
        return {"ok": False, "error": f"unknown capability action:{key}:{action}"}
    required = [str(name) for name in contract.get("params") or []]
    missing = [name for name in required if params.get(name) is None]
    if missing:
        return {"ok": False, "error": "missing params:" + ",".join(missing)}
    state = (_probe_one(item, env, inherit_environment=False)
             if isolated_environment else _probe_one(item, env))
    if not state.get("available"):
        return {"ok": False, "error": state.get("status_detail") or state.get("status"),
                "probe": state}
    run_env = _strip_dead_proxy(dict(env or {}) if isolated_environment
                               else {**os.environ, **(env or {})})
    run_env.pop("ELECTRON_RUN_AS_NODE", None)
    appid = run_env.get("MEYO_UNIFIED_APPID") or run_env.get("AIGC_APPID") or ""
    if not appid and not isolated_environment:
        try:
            model_catalog = json.loads((pathlib.Path.home() / ".claude/skills/aigc-model-router/models.json").read_text())
            candidates = (model_catalog.get("programming") or {}).get("candidates") or []
            appids = candidates[0].get("appids") or [] if candidates else []
            appid = str(appids[0]) if appids else ""
        except Exception:
            pass
    gateway = run_env.get("MEYO_UNIFIED_GATEWAY") or (
        "" if isolated_environment else "https://aigc.sankuai.com/v1/openai/native")
    if appid and not isolated_environment:
        run_env.setdefault("OPENAI_API_KEY", appid)
    values = {name: str(params.get(name, "")) for name in required}
    values.update({"task": str(params.get("task") or ""),
                   "model": str(params.get("model") or "gpt-5.6-sol"),
                   "gateway": gateway, "appid": appid})
    command = _format_argv(contract["command"], item, values=values)
    if not _safe_step(command):
        return {"ok": False, "error": "capability command not allowed"}
    limit = int(timeout_s or contract.get("timeout_s") or 120)
    cwd = pathlib.Path(workdir) if workdir else source_dir(item)
    if workdir and (not cwd.is_absolute() or not cwd.is_dir()):
        return {"ok": False, "error": "bound capability workdir is not an absolute directory"}
    started = time.time()
    try:
        result = subprocess.run(command, cwd=str(cwd), env=run_env,
                                capture_output=True, text=True, timeout=limit)
    except subprocess.TimeoutExpired:
        return {"ok": False, "status": "timeout", "error": f"timeout after {limit}s",
                "key": key, "action": action}
    stdout, stderr = result.stdout or "", result.stderr or ""
    out = {"ok": result.returncode == 0, "status": "ok" if result.returncode == 0 else "error",
           "key": key, "action": action, "exit_code": result.returncode,
           "stdout": stdout[-12000:], "stderr": stderr[-4000:],
           "elapsed_ms": round((time.time() - started) * 1000)}
    if contract.get("output_type") == "path" and result.returncode == 0:
        candidate = next((line.strip() for line in reversed(stdout.splitlines()) if line.strip()), "")
        path = pathlib.Path(candidate).expanduser()
        out["artifact"] = str(path) if path.is_file() else ""
        out["ok"] = bool(out["artifact"])
        if not out["ok"]:
            out["error"] = "capability did not return an existing artifact path"
    elif result.returncode != 0:
        out["error"] = (stderr or stdout)[-1000:]
    return out


def run(key: str, task: str, workdir: str = "", timeout_s: int = 900,
        env: dict[str, str] | None = None, model: str = "gpt-5.6-sol",
        session_id: str = "external", runtime_context: dict[str, Any] | None = None,
        event_cb=None) -> dict:
    item = next((x for x in load_catalog() if x["key"] == key), None)
    if not item:
        return {"status": "unknown_agent", "final": "", "error": f"unknown external agent:{key}"}
    state = _probe_one(item, env)
    if not state["execution_ready"]:
        return {"status": "cancelled" if state["status"] == "cancelled" else "unavailable",
                "final": "", "error": state["status_detail"] or state["status"],
                "probe": state}
    cwd = workdir or str(source_dir(item))
    pathlib.Path(cwd).mkdir(parents=True, exist_ok=True)
    # 编辑器型 Harness 依赖已有项目现场。空目录里自动初始化仓库会把“没有目标源码”伪装成
    # 可执行环境，最终只交一句声明；这是环境失败，应交给上层换家或先取得源码。
    if item.get("requires_existing_workspace"):
        visible_entries = [entry for entry in pathlib.Path(cwd).iterdir()
                           if not entry.name.startswith(".")]
        if not visible_entries:
            return {"status": "harness_runtime", "final": "",
                    "error": "existing project workspace required; current workdir has no source files",
                    "workdir": cwd}
    # requires_git(gnhf 根修·2026-09-02):该 CLI 硬要求在 git repo 里跑(它做分支/commit
    # 迭代·src/cli.ts humanizeErrorMessage)。军团统一 workdir 是普通目录→启动即挂
    # "must be run inside a Git repository"。声明式解:catalog 声明 requires_git=true 的家,
    # workdir 里没有 .git 就 git init(空的·不动产品仓)。这是给它能跑的前提(链路补全),
    # 不是替它答题。git 不可用/初始化失败时如实返回错误(不静默吞)。
    if item.get("requires_git") and not pathlib.Path(cwd, ".git").exists():
        import subprocess as _sp
        _gi = _sp.run(["git", "init", "-q"], cwd=cwd, capture_output=True, timeout=30)
        if _gi.returncode != 0:
            return {"status": "error", "final": "",
                    "error": f"该 harness 需要 git 仓库环境,初始化失败:{_gi.stderr.decode(errors='replace')[:200]}"}
        # git init 后无任何提交→HEAD 不存在,gnhf 这类做 base-commit/branch-diff 的 CLI
        # 跑 git rev-parse HEAD 直接挂。补一个空初始提交让 HEAD 成立。
        _gc = _sp.run(["git", "commit", "-q", "--allow-empty", "-m", "init"],
                      cwd=cwd, capture_output=True, timeout=30,
                      env={**os.environ, "GIT_AUTHOR_NAME": "legion",
                           "GIT_AUTHOR_EMAIL": "legion@meyo", "GIT_COMMITTER_NAME": "legion",
                           "GIT_COMMITTER_EMAIL": "legion@meyo"})
        if _gc.returncode != 0:
            return {"status": "error", "final": "",
                    "error": f"该 harness 需要 git 初始提交,创建失败:{_gc.stderr.decode(errors='replace')[:200]}"}
    session_dir = str(pathlib.Path(cwd, ".sessions"))
    pathlib.Path(session_dir).mkdir(parents=True, exist_ok=True)
    if not pathlib.Path(session_dir).resolve().is_relative_to(pathlib.Path(cwd).resolve()):
        return {"status": "error", "final": "", "error": "session directory escapes task workspace"}
    pathlib.Path(session_dir).chmod(0o700)
    # 环境消毒(Lumi 血泪 2026-09-02):CatPaw shell 带 ELECTRON_RUN_AS_NODE=1,
    # electron 二进制继承后秒退 rc=0 零输出(当 node 跑·main.js 找不到)。
    # 军团派活的 CLI 子进程永远不该继承这个变量——spawn 前一律剥掉,一次修全部家。
    run_env = _strip_dead_proxy({**os.environ, **(env or {})})
    run_env.pop("ELECTRON_RUN_AS_NODE", None)
    appid = run_env.get("MEYO_UNIFIED_APPID") or run_env.get("AIGC_APPID") or ""
    if not appid:
        try:
            model_catalog = json.loads((pathlib.Path.home() / ".claude/skills/aigc-model-router/models.json").read_text())
            candidates = (model_catalog.get("programming") or {}).get("candidates") or []
            appids = candidates[0].get("appids") or [] if candidates else []
            appid = str(appids[0]) if appids else ""
        except Exception:
            pass
    gateway = run_env.get("MEYO_UNIFIED_GATEWAY") or "https://aigc.sankuai.com/v1/openai/native"
    if appid:
        run_env.setdefault("OPENAI_API_KEY", appid)
        run_env.setdefault("DEEPSEEK_API_KEY", appid)
    run_env.setdefault("OPENAI_BASE_URL", gateway)
    run_env.setdefault("OPENAI_API_BASE", gateway)
    # 工作目录也通过 env 下放给包 worker。仅给 cwd 还不够：部分原生 harness 会从自己的
    # workspace 配置决定文件工具根目录；显式 MEYO_WORKDIR 让 adapter 能把两者绑定到同一事实源。
    run_env["MEYO_WORKDIR"] = cwd
    # 跨家互学源码地图(护城河对齐 core 家):外部家与内置家一样拿到「已核验可读」的 peer
    # 源码入口(legion_sandbox.prepare_peer_sources·context not control·只给路径不写规则),
    # 不会读别家源码自愈的能力差距归零。异常不拦派活(上游断链铁律)。
    try:
        import legion_sandbox as _ls
        _project = (runtime_context or {}).get("project_context") or {}
        run_env["MEYO_PEER_SOURCE_CONTEXT"] = _ls.prepare_peer_sources(
            key, cwd, preserve_project=bool(_project.get("repo_root") or _project.get("workspace_root")))
    except Exception:
        pass
    runtime_context = runtime_context or {}
    # 所有 external transport 都拿到同一隔离缓存槽；原生 CLI 是否显式支持由各家 manifest/
    # config 决定，不支持的仍可按 session_id 复用原生历史，绝不伪报缓存命中。
    if runtime_context.get("prompt_cache_key"):
        run_env["MEYO_PROMPT_CACHE_KEY"] = str(runtime_context["prompt_cache_key"])[:64]
    native_checkpoint = (runtime_context.get("task_memory") or {}).get("native_checkpoint") or {}
    native_session_id = str(native_checkpoint.get("session_id") or session_id or "external")
    # 网关兼容映射(声明式):gpt-5.6-sol 拒 reasoning_effort+tools 组合,cline 等家会带该参数
    # 直发必 400。catalog 里 model_compat 声明替换模型;显式传非默认模型则尊重调用方。
    compat = item.get("model_compat") or {}
    if model in compat:
        model = str(compat[model])
    context = _context(item, cwd, {"task": task, "model": model, "appid": appid,
                                   "gateway": gateway, "session_id": _safe_key(native_session_id) or "external",
                                   "session_dir": session_dir,
                                   "user_id": str(runtime_context.get("user_id") or "default"),
                                   "memory_dir": str(runtime_context.get("memory_dir") or ""),
                                   "prompt_cache_key": str(runtime_context.get("prompt_cache_key") or "")[:64],
                                   "tool_cli": str(HERE / "legion_tool.py"),
                                   "runtime_context_json": json.dumps(runtime_context, ensure_ascii=False)})
    if "{session_socket_dir}" in json.dumps(item.get("transports") or []):
        context["session_socket_dir"] = _session_socket_dir(cwd, key, context["session_id"])
    runtime_dir = pathlib.Path(context["runtime_dir"])
    bin_candidates = _node_bin_dirs(item) + [runtime_dir / "venv" / ("Scripts" if os.name == "nt" else "bin"),
                                             runtime_dir / "node_modules" / ".bin", runtime_dir / "bin"]
    run_env["PATH"] = os.pathsep.join(str(path) for path in bin_candidates) + os.pathsep + run_env.get("PATH", "")
    for name, value in (item.get("env") or {}).items():
        rendered = str(value)
        for field, replacement in context.items():
            rendered = rendered.replace("{" + field + "}", str(replacement))
        if name in (item.get("session_env") or []):
            run_env[name] = rendered
        else:
            run_env.setdefault(name, rendered)
    # config 模板生成(底线:需 config 文件才能跑的家自动生成·不污染用户家目录·测试隔离铁律):
    # 有些 CLI(nanobot/cline/openclaw…)必须有一份 config 注册网关 provider/model 才认。父层在
    # run 前把 catalog 声明的 config_template 用 {model}/{appid}/{gateway}/{runtime_dir} 渲染后写到
    # 隔离路径(runtime_dir 下·非 ~/.nanobot 真家目录)。这是链路补全(给它能跑的前提)·不替它答题。
    def _render_tmpl(obj):
        if isinstance(obj, str):
            out = obj
            for field, repl in context.items():
                out = out.replace("{" + field + "}", str(repl))
            return out
        if isinstance(obj, dict):
            # 模板占位也允许出现在 JSON key 中（OpenCode models 以模型名为 key）。
            return {_render_tmpl(k) if isinstance(k, str) else k: _render_tmpl(v)
                    for k, v in obj.items()}
        if isinstance(obj, list):
            return [_render_tmpl(v) for v in obj]
        return obj
    def _write_one_config(_cfg_tmpl: dict) -> str | None:
        if not (isinstance(_cfg_tmpl, dict) and _cfg_tmpl.get("path")):
            return
        try:
            _cfg_path = pathlib.Path(_render_tmpl(_cfg_tmpl["path"]))
            _content = _cfg_tmpl.get("content")
            _model_contents = _cfg_tmpl.get("content_by_model")
            if isinstance(_model_contents, dict) and model in _model_contents:
                _content = _model_contents[model]
            _rendered = _render_tmpl(_content)
            if key == "opencode" and _managed_cli_app() is not None:
                from external_adapters.opencode_state import write_config
                if _cfg_tmpl.get("merge"):
                    raise ValueError("OpenCode generated config does not support implicit merging")
                content = (json.dumps(_rendered, ensure_ascii=False, indent=2)
                           if isinstance(_rendered, (dict, list)) else _rendered)
                if not isinstance(content, str):
                    raise ValueError("OpenCode config template has no content")
                write_config(str(_cfg_path), cwd, content)
                return None
            _cfg_path.parent.mkdir(parents=True, exist_ok=True)
            # merge 模式(openclaw 等有自己 profile config 机制的家):不整体覆盖·只深合并关键字段
            # (provider baseUrl/api 等)到它已有 config·保留它 doctor/迁移维护的其它字段。深合并·
            # 已存在的叶子值以模板为准(保证 api=openai-completions 这类关键修复不被旧值盖回)。
            if _cfg_tmpl.get("merge") and isinstance(_rendered, dict) and _cfg_path.exists():
                try:
                    _existing = json.loads(_cfg_path.read_text() or "{}")
                except Exception:
                    _existing = {}
                def _deep_merge(base, over):
                    for k, v in over.items():
                        if isinstance(v, dict) and isinstance(base.get(k), dict):
                            _deep_merge(base[k], v)
                        else:
                            base[k] = v
                    return base
                _merged = _deep_merge(_existing if isinstance(_existing, dict) else {}, _rendered)
                _cfg_path.write_text(json.dumps(_merged, ensure_ascii=False, indent=2))
            elif isinstance(_rendered, (dict, list)):
                _cfg_path.write_text(json.dumps(_rendered, ensure_ascii=False, indent=2))
            elif isinstance(_rendered, str):
                _cfg_path.write_text(_rendered)
            _cfg_path.chmod(0o600)
        except Exception as _cfg_exc:
            if key == "opencode" and _managed_cli_app() is not None:
                return f"OpenCode config preparation failed: {_cfg_exc}"
            if event_cb:
                event_cb({"t": "harness_event", "protocol": "config",
                          "kind": "warn", "summary": f"config_template 生成失败:{_cfg_exc}"})
    # config_template 支持 dict(单文件)或 list(多文件·如 pi 需 models.json+auth.json 两份)
    _cfg_tmpl_raw = item.get("config_template")
    for _one in (_cfg_tmpl_raw if isinstance(_cfg_tmpl_raw, list) else [_cfg_tmpl_raw]):
        config_error = _write_one_config(_one)
        if config_error:
            return {"status": "harness_runtime", "final": "", "error": config_error,
                    "workdir": cwd, "key": key}
    errors = []
    transport_evidence = []
    last_native_recovery = {}
    last_error_envelope = None

    def preserve_failed_transport(transport, result):
        nonlocal last_error_envelope
        import math
        import error_envelope as _ee
        import token_telemetry as _tokens

        # This is a typed observation projection, not a verdict about content.
        # Unknown strings (including new vendor reasons) stay in the private file.
        statuses = {"ok", "done", "completed", "error", "failed", "incomplete", "cancelled",
                    "aborted", "interrupted", "timeout", "exception", "unavailable",
                    "harness_runtime", "malformed", "auth", "running", "unreported", "unknown"}
        reasons = {"stop", "end_turn", "stop_sequence", "tool_use", "tool_calls", "tool-calls",
                   "length", "max_tokens", "max_output_tokens", "content_filter", "content-filter",
                   "refusal", "pause_turn", "error", "aborted", "cancelled", "interrupted",
                   "timeout", "completed", "unknown", "other", "unrecognized_stop",
                   "user_interrupt", "api_error", "max_turns", "budget_exhausted"}

        def status_code(value):
            if not isinstance(value, str):
                return None
            if value in statuses or re.fullmatch(r"http[1-5][0-9]{2}", value):
                return value
            if re.fullmatch(r"rc-?(?:0|[1-9][0-9]{0,9})", value):
                if -(2**31) <= int(value[2:]) < 2**32:
                    return value
            return None

        def number(value):
            return (type(value) in (int, float) and value >= 0
                    and (type(value) is int or math.isfinite(value)))

        def machine_fields(raw):
            raw = raw if isinstance(raw, dict) else {}
            out = {name: raw[name] for name in (
                "completed", "failed", "cancelled", "interrupted", "aborted", "parent_timeout",
                "partial_observation", "matched_session", "result_seen", "session_received",
                "budget_exhausted", "stderr_is_error", "is_error")
                if type(raw.get(name)) is bool}
            if "process_returncode" in raw and (
                    raw["process_returncode"] is None or type(raw["process_returncode"]) is int):
                out["process_returncode"] = raw["process_returncode"]
            out.update({name: raw[name] for name in ("attempt", "num_turns", "attempts", "execution_calls")
                        if type(raw.get(name)) is int and raw[name] >= 0})
            out.update({name: raw[name] for name in ("http_status", "api_error_status")
                        if type(raw.get(name)) is int and 100 <= raw[name] <= 599})
            out.update({name: raw[name] for name in (
                "elapsed", "elapsed_s", "duration_ms", "duration_api_ms", "ttft_ms")
                if number(raw.get(name))})
            enums = {
                "status": statuses, "turn_status": statuses, "request_status": statuses,
                "reason": reasons, "stop_reason": reasons, "turn_exit_reason": reasons,
                "terminal_reason": reasons,
                "type": {"step_start", "step_finish", "text", "request_end", "result",
                         "turn.completed", "turn.failed", "response.completed",
                         "response.incomplete", "message_stop", "agent_end", "error"},
            }
            out.update({name: raw[name] for name, allowed in enums.items()
                        if isinstance(raw.get(name), str) and raw[name] in allowed})
            # The existing core budget consumer recognizes this one reason
            # family with optional numeric counters; it is not arbitrary text.
            exit_reason = raw.get("turn_exit_reason")
            if isinstance(exit_reason, str) and re.fullmatch(
                    r"max_iterations_reached(?:\([0-9]+/[0-9]+\))?", exit_reason):
                out["turn_exit_reason"] = exit_reason
            return out

        def usage_fields(raw):
            if not isinstance(raw, dict):
                return {}
            # Keep the existing telemetry spellings/semantics, without coercing
            # string counts, estimating tokens, or exposing model/source strings.
            integers = set().union(*_tokens._ALIASES.values(), _tokens._TURN_ALIASES,
                                   ("attempts_total", "attempts_reported", "attempts_unreported"))
            out = {name: value for name, value in raw.items()
                   if name in integers and type(value) is int and value >= 0}
            out.update({name: raw[name] for name in (
                *_tokens._COST_ALIASES, *_tokens._DURATION_ALIASES, "elapsed_s")
                if number(raw.get(name))})
            out.update({name: raw[name] for name in ("available", "complete")
                        if type(raw.get(name)) is bool})
            for name, allowed in {
                "granularity": {"request", "task", "thread_cumulative"},
                "coverage": {"complete", "partial", "unavailable"},
                "source": {"native", "external_native_event", "unavailable"},
                "input_semantics": {"cache_buckets"},
            }.items():
                if isinstance(raw.get(name), str) and raw[name] in allowed:
                    out[name] = raw[name]
            for name in ("input_tokens_details", "prompt_tokens_details"):
                detail = raw.get(name)
                if isinstance(detail, dict):
                    known = {k: detail[k] for k in ("cached_tokens", "cache_read_tokens")
                             if type(detail.get(k)) is int and detail[k] >= 0}
                    if known:
                        out[name] = known
            return out

        envelope = result.get("error_envelope")
        last_error_envelope = dict(envelope) if isinstance(envelope, dict) else None
        # A failed native attempt can already own a real session. Keep the
        # received restore object, not just its private failure-log reference.
        checkpoint = result.get("native_checkpoint")
        if isinstance(checkpoint, dict) and checkpoint.get("session_id"):
            last_native_recovery.clear()
            last_native_recovery.update(native_checkpoint=dict(checkpoint))
            if isinstance(result.get("resumed_native"), bool):
                last_native_recovery["resumed_native"] = result["resumed_native"]
        raw_facts = result.get("native_facts")
        raw_facts = raw_facts if isinstance(raw_facts, dict) else {}
        facts = machine_fields(raw_facts)
        for name in ("terminal_event", "request_end", "turn_result"):
            nested = machine_fields(raw_facts.get(name))
            # A reason without its recognized frame discriminator must not
            # become a terminal record merely because the reason is familiar.
            if "type" in nested:
                facts[name] = nested
        kind = transport.get("type")
        fact = {"transport": kind if kind in ("cli", "http", "acp", "mcp", "sdk") else "unknown",
                "status": status_code(result.get("status")) or "error", "native_facts": facts}
        if type(result.get("returncode")) is int:
            fact["returncode"] = result["returncode"]
        if number(result.get("elapsed")):
            fact["elapsed"] = result["elapsed"]
        if type(result.get("http_status")) is int and 100 <= result["http_status"] <= 599:
            fact["http_status"] = result["http_status"]
        if result.get("exception_type") in (
                "TransportError", "TimeoutExpired", "OSError", "TimeoutError", "FileNotFoundError",
                "PermissionError", "ConnectionError", "ConnectionRefusedError",
                "ConnectionResetError", "BrokenPipeError"):
            fact["exception_type"] = result["exception_type"]
        for name in ("cancelled", "partial", "budget_exhausted"):
            if type(result.get(name)) is bool:
                fact[name] = result[name]
        usage = usage_fields(result.get("usage"))
        if usage:
            fact["usage"] = usage
        records, identities = [], set()
        raw_records = result.get("usage_records")
        for row in raw_records if isinstance(raw_records, list) else []:
            if not isinstance(row, dict):
                continue
            identity = row.get("usage_identity")
            if isinstance(identity, str) and identity:
                if identity in identities:
                    continue
                identities.add(identity)  # Existing telemetry dedup; never expose the opaque ID.
            known = usage_fields(row)
            if known:
                records.append(known)
        if records:
            fact["usage_records"] = records
        if isinstance(envelope, dict):
            observed = {}
            for name, allowed in {
                "category": _ee._RETRYABLE | _ee._USER | {
                    "harness_runtime", "runtime_budget", "task_error", "unknown", "request_too_large"},
                "scope": {"request", "task", "harness", "provider", "tool", "user"},
            }.items():
                if isinstance(envelope.get(name), str) and envelope[name] in allowed:
                    observed[name] = envelope[name]
            if type(envelope.get("retryable")) is bool:
                observed["retryable"] = envelope["retryable"]
            code = envelope.get("code")
            if status_code(code) or code in ("request_total_budget_exhausted", "max_output_tokens",
                                            "native_output_limit", "context_length_exceeded"):
                observed["code"] = code
            details = envelope.get("details")
            if isinstance(details, dict):
                known = {name: details[name] for name in ("used", "limit", "reserved_output")
                         if number(details.get(name))}
                if details.get("dimension") in (
                        "input_tokens", "output_tokens", "model_calls", "wall_seconds"):
                    known["dimension"] = details["dimension"]
                if type(details.get("usage_known")) is bool:
                    known["usage_known"] = details["usage_known"]
                if known:
                    observed["details"] = known
            if observed:
                fact["error_envelope"] = observed
        # Bodies/diagnostics stay private. Only the last original error is used
        # by the final result; an earlier diagnostic must not classify a later attempt.
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=session_dir,
                                             prefix="transport-failure-", suffix=".json",
                                             delete=False) as output:
                os.chmod(output.name, 0o600)
                json.dump(result, output, ensure_ascii=False)
                fact["private_result_path"] = output.name
        except (OSError, TypeError, ValueError) as exc:
            fact["evidence_storage_error"] = type(exc).__name__
        transport_evidence.append((fact, result.get("error", "")))

    def attach_transport_evidence(result):
        if transport_evidence:
            import copy

            if result.get("status") != "ok":
                # The final actually executed transport owns current facts.
                # Never fill missing fields from an older failure, or OR failure
                # flags into a later success. History remains separate evidence.
                latest, error = transport_evidence[-1]
                for name in ("status", "native_facts", "returncode", "http_status", "elapsed",
                             "usage", "usage_records", "cancelled", "partial", "budget_exhausted"):
                    if name in latest:
                        result[name] = copy.deepcopy(latest[name])
                result["error"] = error
            facts = dict(result.get("native_facts") or {})
            facts["transport_failures"] = [observation for observation, _ in transport_evidence]
            result["native_facts"] = facts
            result["transport_failures"] = list(facts["transport_failures"])
        return result

    for transport in item.get("transports") or []:
        try:
            # 原生续跑的声明式接线：首轮走 command 创建会话；task_memory 已有 checkpoint 后
            # 才改用 resume_command。这样 Goose/Pi 等可复用各自原生 history/compaction/tool state，
            # 又不会在首轮对不存在的 session 强行 --resume。没有 resume_command 的家零变化。
            selected = transport
            if native_checkpoint and transport.get("resume_command"):
                selected = {**transport, "command": transport["resume_command"]}
            elif not native_checkpoint and transport.get("first_command"):
                selected = {**transport, "command": transport["first_command"]}
            execution_context = context
            if key == "opencode" and str(selected.get("type") or "cli").lower() == "cli":
                # Fixed adapter identity, never a request/environment permission.
                execution_context = {**context, "native_adapter_key": "opencode"}
            if key == "nanobot" and str(selected.get("type") or "cli").lower() == "cli":
                from external_adapters.nanobot_wrapper import prepare_native_state
                try:
                    roots = prepare_native_state(run_env.get("NANOBOT_CONFIG") or "", cwd, run_env)
                except ValueError as exc:
                    raise TransportError(str(exc)) from exc
                # This permission fact comes from fixed adapter code, never the
                # request's runtime_context or an environment-supplied root list.
                execution_context = {**context, "native_writable_state_roots": roots}
            bootstrap = (_trusted_cline_bootstrap(selected, execution_context, run_env)
                         if key == "cline" else None)
            if bootstrap is None:
                result = run_transport(selected, execution_context, run_env, timeout_s, event_cb=event_cb)
            else:
                from external_agent_transports import _supervise_cli_process
                command, bootstrap_env, reported = bootstrap
                result = _supervise_cli_process(
                    command, selected, execution_context, bootstrap_env, timeout_s,
                    event_cb=event_cb, report_command=reported, termination_grace_s=2.0)
            result.update({"workdir": cwd, "key": key})
            if result.get("status") == "ok":
                # 有些 CLI(Pi/OpenClaw)用调用方提供的稳定 session id 恢复，但成功 JSON 不回显 id。
                # 目录明确声明 native_resume 时，把本次实际使用的 id 写回统一 checkpoint；否则
                # run_one 无法持久化，下一轮会退成“碰巧同 id”而非有证据的原生恢复契约。
                if transport.get("native_resume") and not result.get("native_checkpoint"):
                    checkpoint = {"session_id": native_session_id}
                    result["native_session_id"] = native_session_id
                    result["checkpoint"] = checkpoint
                    result["native_checkpoint"] = checkpoint
                return attach_transport_evidence(result)
            preserve_failed_transport(transport, result)
            errors.append(f"{transport.get('type')}:{result.get('error') or result.get('status')}")
        except (TransportError, subprocess.TimeoutExpired, OSError) as exc:
            failure = {"status": "exception",
                       "exception_type": type(exc).__name__, "error": str(exc)}
            envelope = getattr(exc, "error_envelope", None)
            if isinstance(envelope, dict):
                failure["error_envelope"] = dict(envelope)
            checkpoint = getattr(exc, "native_checkpoint", None)
            if isinstance(checkpoint, dict) and checkpoint.get("session_id"):
                failure["native_checkpoint"] = dict(checkpoint)
                if isinstance(getattr(exc, "resumed_native", None), bool):
                    failure["resumed_native"] = exc.resumed_native
            native_facts = getattr(exc, "native_facts", None)
            if isinstance(native_facts, dict):
                # 原生事实只进既有私有证据；公开结果仍只携带文件引用。
                failure["native_facts"] = native_facts
            preserve_failed_transport(transport, failure)
            errors.append(f"{transport.get('type')}:{type(exc).__name__}:{exc}")
    return attach_transport_evidence({"status": "error", "final": "", **last_native_recovery,
                                      **({"error_envelope": last_error_envelope}
                                         if last_error_envelope is not None else {}),
                                      "error": " | ".join(errors)[-4000:],
                                      "workdir": cwd, "key": key})


# ── 一键升级 + 无人值守自动更新(用户 2026-09-01 定:所有 agent 自动更新到最新·线上也是,
#    每周二/五各一轮;升级后必须 probe 验证"代码可自主运行",失败自动回滚旧版)──


def _save_catalog(items: list[dict]) -> None:
    """把整份 agents 列表写回 external_agents.json(保留 schema_version 等顶层字段)。

    持跨进程写锁落盘:两个 agent session 并发 save 不再互相覆盖(2026-09-02 丢写根治)。
    调用方若先 load 再改再 save,应整段包在 catalog_lock() 里(load 也在锁内)防丢更新。
    """
    with catalog_lock():
        try:
            data = json.loads(CATALOG.read_text(encoding="utf-8")) if CATALOG.exists() else {}
        except Exception:
            data = {}
        data["agents"] = items
        CATALOG.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _npm_latest_version(package: str) -> str:
    """查 npm 包最新版本(npm view)。清代理 env:本机常见死代理会把 npm 卡死。"""
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
    try:
        r = subprocess.run(["npm", "view", package, "version"], capture_output=True,
                           text=True, timeout=90, env=env)
        if r.returncode == 0:
            for line in (r.stdout or "").splitlines():
                line = line.strip()
                if line:
                    return line
    except Exception:
        pass
    return ""


def apply_update(key: str, target_tag: str = "", target_revision: str = "",
                 timeout_s: int = 1800) -> dict:
    """把某家升级到最新(或指定 tag/revision)→ 重装 → probe 验证 → 失败自动回滚。

    "代码可自主运行"的硬验收 = install() ok(revision_ok+available 双过)。不通过就把
    catalog 恢复旧条目并重装旧版(回到已知可用状态),绝不让坏升级留在编排池里。
    """
    items = load_catalog()
    idx = next((i for i, x in enumerate(items) if x.get("key") == key), None)
    if idx is None:
        return {"ok": False, "error": f"unknown external agent:{key}"}
    item = items[idx]
    immutable = _published_runtime_mutation(item)
    if immutable:
        return immutable
    update = item.get("update") or {}
    strategy = update.get("strategy")
    if strategy not in ("github_release", "github_commit"):
        strat_text = str(strategy or "none")
        return {"ok": False, "error": f"unsupported update strategy:{strat_text}"}
    url = str(item.get("url") or "")
    old_version, old_revision = str(item.get("version") or ""), str(item.get("revision") or "")
    # 1) 确定目标版本
    if target_tag and target_revision:
        new_tag, new_rev = target_tag, target_revision
        latest = {"tag": target_tag, "revision": target_revision, "ok": True}
    elif strategy == "github_release":
        latest = agent_manifest.latest_github_release(url)
        new_tag, new_rev = str(latest.get("tag") or ""), str(latest.get("revision") or "")
    else:
        latest = agent_manifest.latest_branch_commit(url, str(update.get("branch") or "main"))
        new_tag, new_rev = str(latest.get("tag") or ""), str(latest.get("revision") or "")
    if not latest.get("ok") or not new_rev:
        err_text = str(latest.get("error") or "no revision")
        return {"ok": False, "status": "check_failed", "key": key,
                "error": f"查询最新版本失败:{err_text}"}
    if new_rev == old_revision:
        return {"ok": True, "status": "already_latest", "key": key,
                "version": old_version, "revision": old_revision}
    # npm runtime 家(cline/openclaw):install argv 里 pkg@x.y.z 的版本号与 github tag
    # 各自独立,单独查 npm 最新替换;查不到就保留原 npm 版本(源码 revision 已更新)。
    npm_package = str(update.get("npm_package") or "")
    new_npm_version = _npm_latest_version(npm_package) if npm_package else ""
    # 2) 备份旧条目(回滚用)
    backup_item = json.loads(json.dumps(item))
    # 3) 更新 catalog:version/revision/current_ref + install argv 里的旧版本引用
    item["version"] = new_tag
    item["revision"] = new_rev
    update["current_ref"] = new_tag
    replaced = []
    for step in item.get("install") or []:
        for i, part in enumerate(step):
            text = str(part)
            new_text = text
            if old_revision and old_revision in new_text:
                new_text = new_text.replace(old_revision, new_rev)
                replaced.append(f"revision:{old_revision[:8]}->{new_rev[:8]}")
            if old_version and old_version != new_tag and old_version in new_text:
                new_text = new_text.replace(old_version, new_tag)
                replaced.append(f"version:{old_version}->{new_tag}")
            if npm_package and new_npm_version:
                m = re.search(re.escape(npm_package) + r"@\S+", new_text)
                if m and m.group(0) != f"{npm_package}@{new_npm_version}":
                    new_text = new_text[:m.start()] + f"{npm_package}@{new_npm_version}" + new_text[m.end():]
                    replaced.append(f"npm:{m.group(0)}->{npm_package}@{new_npm_version}")
            step[i] = new_text
    # 锁内重读+按 key 合并:npm view 等耗时步骤在 load 之后,期间别家可能已写入新成员,
    # 直接整表覆盖会把它抹掉(2026-09-02 丢写根治)。只替换本 key 的条目,其余以盘上最新为准。
    with catalog_lock():
        _items_now = load_catalog()
        for _i, _x in enumerate(_items_now):
            if _x.get("key") == key:
                _items_now[_i] = items[idx]
        _save_catalog(_items_now)
    # 4) 重装(clone+checkout 新 revision+install steps)
    result = install(key, confirm=True, timeout_s=timeout_s)
    # 5) 硬验收 + 失败回滚(catalog 恢复旧条目并重装旧版)
    state = result.get("probe") or {}
    if not result.get("ok"):
        with catalog_lock():
            items_now = load_catalog()
            for i2, x2 in enumerate(items_now):
                if x2.get("key") == key:
                    items_now[i2] = backup_item
            _save_catalog(items_now)
        rollback = install(key, confirm=True, timeout_s=timeout_s)
        err_text = str(result.get("error") or state.get("status_detail") or "install failed")[:400]
        return {"ok": False, "status": "rolled_back", "key": key, "error": err_text,
                "attempted": {"version": new_tag, "revision": new_rev,
                              "npm_version": new_npm_version or None},
                "restored": {"version": old_version, "revision": old_revision,
                             "rollback_ok": bool(rollback.get("ok"))},
                "probe": (rollback.get("probe") or state)}
    invalidate_probe_cache()
    invalidate_updates_cache()
    return {"ok": True, "status": "updated", "key": key,
            "from": {"version": old_version, "revision": old_revision},
            "to": {"version": new_tag, "revision": new_rev, "npm_version": new_npm_version or None},
            "install_replacements": replaced, "probe": state}


# 无人值守自动更新状态(落盘·前端/日志可查)
_AUTO_STATE_PATH = _STATE_DIR / "auto_update.json"


def _auto_state() -> dict:
    try:
        value = json.loads(_AUTO_STATE_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _write_auto_state(patch: dict) -> dict:
    state = {**_auto_state(), **patch, "updated_at": time.time()}
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    _AUTO_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def auto_update_enabled() -> bool:
    # 默认开(用户 2026-09-01 定:全部自动更新·线上部署也是);LEGION_EXT_AUTO_UPDATE=0 关。
    return str(os.environ.get("LEGION_EXT_AUTO_UPDATE", "1")).strip().lower() not in ("0", "false", "no", "off")


def auto_update_window(now: float | None = None) -> str:
    """今天是周二/五则返回窗口 id(YYYY-MM-DD),否则空串。周二=1 周五=4。"""
    import datetime
    day = datetime.datetime.fromtimestamp(now if now is not None else time.time())
    return day.strftime("%Y-%m-%d") if day.weekday() in (1, 4) else ""


def auto_update_status() -> dict:
    """自动更新当前状态(开关/上次运行/窗口进度)。"""
    import datetime
    now = time.time()
    st = _auto_state()
    return {"enabled": auto_update_enabled(),
            "schedule": "每周二/周五自动跑一轮", 
            "window_today": auto_update_window(now),
            "window_done": st.get("window_done") or "",
            "last_run": st.get("last_run") or None,
            "today": datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d %A")}


def auto_update_all(force: bool = False, timeout_s: int = 1800) -> dict:
    """跑一轮自动更新:对 check_updates 显示可升级的家依次 apply_update。

    串行(每家 install 可能数分钟);每家独立成败,失败的已由 apply_update 回滚,
    一家失败不挡下一家。结果落 auto_update.json(前端/日志可查)。
    """
    if not force and not auto_update_enabled():
        return {"ok": False, "skipped": "disabled", "results": []}
    checks = check_updates(force=True).get("updates") or []
    pending = [row for row in checks if row.get("update_available")]
    results = []
    for row in pending:
        key = str(row.get("key") or "")
        try:
            r = apply_update(key, timeout_s=timeout_s)
        except Exception as exc:
            r = {"ok": False, "key": key, "status": "error",
                 "error": f"{type(exc).__name__}:{exc}"[:300]}
        r["latest_version"] = row.get("latest_version")
        results.append(r)
    out = {"ok": all(r.get("ok") for r in results) if results else True,
           "ran_at": time.time(), "checked": len(checks), "pending": len(pending),
           "updated": sum(1 for r in results if r.get("status") == "updated"),
           "rolled_back": sum(1 for r in results if r.get("status") == "rolled_back"),
           "already_latest": sum(1 for r in results if r.get("status") == "already_latest"),
           "results": results}
    _write_auto_state({"last_run": out})
    return out


def start_auto_update_scheduler(interval_s: int = 1800) -> None:
    """daemon 线程:每 30min 醒一次,今天若是周二/五且本窗口没跑过 → 跑一轮。

    窗口 id 落盘(window_done),服务重启不会重复跑同一窗口;force 手动触发不受窗口限制。
    随 server 启动(LEGION_EXT_AUTO_UPDATE=0 整体关闭)。
    """
    import threading as _threading

    def _loop():
        while True:
            try:
                if auto_update_enabled():
                    window = auto_update_window()
                    done = str(_auto_state().get("window_done") or "")
                    if window and window != done:
                        _write_auto_state({"window_done": window})
                        auto_update_all(force=True)
            except Exception:
                pass
            time.sleep(max(300, int(interval_s)))

    _threading.Thread(target=_loop, name="ext-auto-update", daemon=True).start()
