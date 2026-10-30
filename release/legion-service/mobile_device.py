#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""受管 Android 手机：Redroid 生命周期、MobileRun 动作和网页实时控制。"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import urllib.request
import hashlib
import threading
import time

import legion_paths

PHONE_IMAGE = os.environ.get("MEYO_PHONE_IMAGE", "redroid64:14")
PHONE_NAME = os.environ.get("MEYO_PHONE_CONTAINER", "meyo-phone")
PHONE_SERIAL = os.environ.get("MEYO_PHONE_SERIAL", "localhost:5555")
PHONE_DATA_DIR = os.environ.get("MEYO_PHONE_DATA_DIR", "/tmp/redroid-data")
_RUNTIME = pathlib.Path(legion_paths.under("external_agents", "runtimes", "mobilerun", "venv", "bin", "mobilerun"))
def _runtime_path():
    root = os.environ.get("LEGION_EXTERNAL_RUNTIME_ROOT", "").strip()
    if not root:
        return _RUNTIME
    base = pathlib.Path(root).expanduser().resolve()
    member = (base / "mobilerun").resolve()
    if member.parent != base:
        raise ValueError("MobileRun runtime escapes configured root")
    return member / "venv" / "bin" / "mobilerun"


_LOCK = threading.RLock()
_STATUS_CACHE = {"data": None, "ts": 0.0}
_STATUS_TTL = 15.0
_STATUS_REFRESHING = False
_READY_CACHE = {"ok": False, "ts": 0.0}
_READY_TTL = 30.0


def cloud_configured(*, environment=None):
    import agentbay_mobile
    return agentbay_mobile.configured(environment=environment)


def authorize_cloud_owner(uid, *, environment=None):
    if cloud_configured(environment=environment):
        import agentbay_mobile
        agentbay_mobile._configuration(uid, environment=environment)


def read_cloud_screenshot(uid, path):
    if not cloud_configured():
        raise ValueError("cloud mobile screenshot is not configured")
    import agentbay_mobile
    return agentbay_mobile.read_screenshot(uid, path)


def stop(*, uid=""):
    if cloud_configured():
        import agentbay_mobile
        return agentbay_mobile.stop(uid)
    return {"ok": False, "error": "原本机手机未提供此释放接口"}


def _run(argv, timeout=30, binary=False):
    try:
        result = subprocess.run(argv, capture_output=True, timeout=timeout,
                                text=not binary)
        return {"ok": result.returncode == 0, "exit_code": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "stdout": b"" if binary else "", "stderr": b"" if binary else ""}


def _docker_state():
    if not shutil.which("docker"):
        return {"ok": False, "error": "Docker CLI 未安装"}
    probe = _run(["docker", "version", "--format", "{{.Server.Version}}"], 8)
    return {"ok": probe["ok"], "version": str(probe.get("stdout") or "").strip(),
            "error": str(probe.get("stderr") or probe.get("error") or "").strip()}


def _container_state():
    raw = _run(["docker", "inspect", PHONE_NAME], 10)
    if not raw["ok"]:
        return {"exists": False, "running": False, "verified": False}
    try:
        item = json.loads(raw["stdout"])[0]
    except (json.JSONDecodeError, IndexError, TypeError):
        return {"exists": True, "running": False, "verified": False,
                "error": "docker inspect 返回无法解析"}
    config, host = item.get("Config") or {}, item.get("HostConfig") or {}
    image = str(config.get("Image") or "")
    binds = host.get("Binds") or []
    return {
        "exists": True,
        "running": bool((item.get("State") or {}).get("Running")),
        "verified": image == PHONE_IMAGE and bool(host.get("Privileged"))
                    and f"{PHONE_DATA_DIR}:/data" in binds,
        "image": image,
        "privileged": bool(host.get("Privileged")),
        "restart_policy": str((host.get("RestartPolicy") or {}).get("Name") or ""),
        "data_dir": PHONE_DATA_DIR,
    }


def _adb(*args, timeout=30, binary=False):
    adb = shutil.which("adb")
    if not adb:
        return {"ok": False, "error": "ADB 未安装", "stdout": b"" if binary else "",
                "elapsed_ms": 0.0, "failure_kind": "missing_dependency"}
    started = time.perf_counter()
    out = _run([adb, "-s", PHONE_SERIAL, *map(str, args)], timeout, binary)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    if out.get("ok"):
        failure_kind = ""
    elif "TimeoutExpired" in str(out.get("error") or ""):
        failure_kind = "timeout"
    else:
        failure_kind = "adb_error"
    return {**out, "elapsed_ms": elapsed_ms, "failure_kind": failure_kind}


def _adb_ready():
    if not shutil.which("adb"):
        return {"ok": False, "error": "ADB 未安装"}
    _run([shutil.which("adb"), "connect", PHONE_SERIAL], 10)
    boot = _adb("shell", "getprop", "sys.boot_completed", timeout=8)
    return {"ok": boot["ok"] and str(boot.get("stdout") or "").strip() == "1",
            "serial": PHONE_SERIAL, "boot_completed": str(boot.get("stdout") or "").strip(),
            "error": str(boot.get("stderr") or boot.get("error") or "").strip()}


def _probe_status():
    docker = _docker_state()
    container = _container_state() if docker["ok"] else {"exists": False, "running": False, "verified": False}
    adb = _adb_ready() if container.get("running") else {"ok": False, "serial": PHONE_SERIAL, "error": "容器未运行"}
    focus = ""
    size = ""
    if adb["ok"]:
        size_result = _adb("shell", "wm", "size", timeout=8)
        size = str(size_result.get("stdout") or "").strip().split(":")[-1].strip()
        focus_result = _adb("shell", "dumpsys", "window", timeout=10)
        focus = next((line.strip() for line in str(focus_result.get("stdout") or "").splitlines()
                      if "mCurrentFocus" in line), "")
    return {"ok": True, "ready": bool(container.get("verified") and container.get("running") and adb["ok"]),
            "docker": docker, "container": container, "adb": adb, "screen_size": size,
            "current_focus": focus, "agent_ready": _runtime_path().is_file(),
            "actions": ["screenshot", "tap", "swipe", "press", "type", "start", "ui", "apps", "task"],
            "note": "task=自然语言生活场景(截图/轨迹/宏自动落盘);其余为底层动作"}


def _refresh_status():
    global _STATUS_REFRESHING
    try:
        data = _probe_status()
        with _LOCK:
            _STATUS_CACHE.update({"data": data, "ts": time.time()})
    except Exception:
        pass
    finally:
        with _LOCK:
            _STATUS_REFRESHING = False


def status(force=False, *, uid=""):
    if cloud_configured():
        import agentbay_mobile
        return agentbay_mobile.status(uid)
    global _STATUS_REFRESHING
    with _LOCK:
        cached = _STATUS_CACHE["data"]
        stale = cached is not None and time.time() - _STATUS_CACHE["ts"] >= _STATUS_TTL
        if force:
            pass
        elif cached is not None and not stale:
            return {**cached, "from_cache": True, "stale": False}
        elif not _STATUS_REFRESHING:
            _STATUS_REFRESHING = True
            threading.Thread(target=_refresh_status, daemon=True, name="mobile-status-refresh").start()
    if force:
        data = _probe_status()
        with _LOCK:
            _STATUS_CACHE.update({"data": data, "ts": time.time()})
        return {**data, "from_cache": False, "stale": False}
    if cached is not None:
        return {**cached, "from_cache": True, "stale": True, "warming": True}
    return {"ok": True, "ready": False, "warming": True, "stale": False,
            "docker": {"ok": False},
            "container": {"exists": False, "running": False, "verified": False},
            "adb": {"ok": False, "serial": PHONE_SERIAL}, "screen_size": "",
            "current_focus": "", "agent_ready": _runtime_path().is_file(),
            "actions": ["screenshot", "tap", "swipe", "press", "type", "start", "ui", "apps", "task"],
            "note": "手机状态正在后台更新"}


def ensure(timeout=120, *, uid=""):
    """复用已跑通容器；热路径不为每个动作重复 Docker+ADB 全探测。"""
    if cloud_configured():
        import agentbay_mobile
        return agentbay_mobile.ensure(uid)
    with _LOCK:
        # 安全检查必须在任何缓存短路之前：受管容器被外部替换成同名非受管容器时，
        # 热路径缓存(30s TTL)不能绕过"拒绝覆盖"。_container_state 是单次 docker inspect
        # (~百毫秒)，设备操作本身是秒级 adb 往返，为安全不变量付这点开销是值的。
        current = _container_state()
        if current.get("exists") and not current.get("verified"):
            return {"ok": False, "error": "同名容器参数与受管 Redroid 不一致，拒绝覆盖", "container": current}
        # 缓存只省略 adb boot 探测等待，且要求刚查的容器状态仍是受管且运行中。
        if (_READY_CACHE["ok"] and time.monotonic() - _READY_CACHE["ts"] < _READY_TTL
                and current.get("exists") and current.get("verified") and current.get("running")):
            return {"ok": True, "action": "ready_cached", "from_cache": True}
        if not current.get("exists"):
            if not _docker_state()["ok"]:
                return {"ok": False, "error": "Docker 服务不可用"}
            pathlib.Path(PHONE_DATA_DIR).mkdir(parents=True, exist_ok=True)
            command = ["docker", "run", "-d", "--name", PHONE_NAME,
                       "--privileged", "--security-opt", "label=disable",
                       "--restart", "unless-stopped", "-v", f"{PHONE_DATA_DIR}:/data",
                       "-p", "127.0.0.1:5555:5555", PHONE_IMAGE,
                       "androidboot.redroid_keep=1"]
            launched = _run(command, 120)
            if not launched["ok"]:
                return {"ok": False, "error": str(launched.get("stderr") or launched.get("error") or "启动失败")}
        elif not current.get("running"):
            started = _run(["docker", "start", PHONE_NAME], 60)
            if not started["ok"]:
                return {"ok": False, "error": str(started.get("stderr") or started.get("error") or "恢复失败")}
        deadline = time.monotonic() + max(10, int(timeout))
        while time.monotonic() < deadline:
            # ensure 是控制路径，必须读取真状态；普通 status 的 stale-while-refresh
            # 只适合页面秒开，不能拿旧缓存决定安装、截图等设备操作是否可执行。
            ready = status(force=True)
            if ready["ready"]:
                with _LOCK:
                    _READY_CACHE.update({"ok": True, "ts": time.monotonic()})
                return {"ok": True, "action": "ready", "status": ready}
            time.sleep(2)
        return {"ok": False, "error": "Android 启动后未在时限内通过 ADB boot_completed",
                "status": status(force=True)}


def screenshot(*, uid=""):
    if cloud_configured():
        import agentbay_mobile
        return agentbay_mobile.screenshot(uid)
    ready = ensure()
    if not ready["ok"]:
        return ready
    shot = _adb("exec-out", "screencap", "-p", timeout=20, binary=True)
    data = shot.get("stdout") or b""
    if not shot["ok"] or not data.startswith(b"\x89PNG"):
        return {"ok": False, "error": str(shot.get("stderr") or shot.get("error") or "截图不是 PNG")}
    folder = pathlib.Path(legion_paths.under("media", "mobile"))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"phone-{int(time.time() * 1000)}.png"
    temp = pathlib.Path(tempfile.mkstemp(prefix="phone-", suffix=".png", dir=folder)[1])
    temp.write_bytes(data)
    os.replace(temp, path)
    return {"ok": True, "path": str(path), "bytes": len(data),
            "width_height": status().get("screen_size", ""),
            "adb_ms": shot.get("elapsed_ms", 0.0), "failure_kind": ""}


def action(name, params=None, *, uid=""):
    if cloud_configured():
        import agentbay_mobile
        return agentbay_mobile.action(uid, name, params)
    params = dict(params or {})
    if name == "screenshot":
        return screenshot()
    if name == "task":
        return task(str(params.get("task") or ""), steps=int(params.get("steps") or 0) or None,
                   timeout_s=int(params.get("timeout_s") or 0) or None,
                   model=str(params.get("model") or ""), serial=str(params.get("serial") or ""))
    ready = ensure()
    if not ready["ok"]:
        return ready
    commands = {
        "tap": ["shell", "input", "tap", params.get("x"), params.get("y")],
        "swipe": ["shell", "input", "swipe", params.get("x1"), params.get("y1"), params.get("x2"), params.get("y2"), params.get("duration_ms", 350)],
        "press": ["shell", "input", "keyevent", params.get("key")],
        "type": ["shell", "input", "text", str(params.get("text") or "").replace(" ", "%s")],
        # `monkey -p` 在 Redroid 真跑出现“报告 injected=1 但前台仍是旧 App”；用 cmd package
        # resolve-activity + am start 精确启动 launcher Activity，避免生活任务在应用间反复跳错。
        "start": ["shell", "am", "start", "-W", "-a", "android.intent.action.MAIN",
                  "-c", "android.intent.category.LAUNCHER", "-p", params.get("package")],
        "ui": ["exec-out", "uiautomator", "dump", "/dev/tty"],
        "apps": ["shell", "pm", "list", "packages", "-3"],
    }
    command = commands.get(name)
    if not command or any(value is None for value in command):
        return {"ok": False, "error": f"未知动作或缺少参数:{name}"}
    result = _adb(*command, timeout=45)
    return {"ok": result["ok"], "action": name, "stdout": str(result.get("stdout") or "")[-50000:],
            "error": str(result.get("stderr") or result.get("error") or "")[-2000:],
            "adb_ms": result.get("elapsed_ms", 0.0),
            "failure_kind": result.get("failure_kind") or ""}


def inspect_apk(source: str, timeout_s=120):
    """下载/检查 APK，不安装。返回安装确认所需的包名、权限、sha256 和暂存 token。

    只接受 https 或本机绝对路径；下载限制 256MiB，APK 必须能被 aapt/apkanalyzer/pm
    识别。检查与安装两阶段分离，防网页一点击就把未知二进制装进手机。
    """
    source = str(source or "").strip()
    if not source:
        return {"ok": False, "error": "请输入 APK 的 https 地址或本机绝对路径"}
    root = pathlib.Path(legion_paths.under("mobile", "apk-staging"))
    root.mkdir(parents=True, exist_ok=True)
    token = hashlib.sha256(f"{source}|{time.time_ns()}".encode()).hexdigest()[:20]
    target = root / f"{token}.apk"
    try:
        if source.startswith("https://"):
            req = urllib.request.Request(source, headers={"User-Agent": "Meyo-Mobile/1.0"})
            with urllib.request.urlopen(req, timeout=int(timeout_s)) as response, target.open("wb") as f:
                total = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > 256 * 1024 * 1024:
                        raise ValueError("APK 超过 256MiB 限制")
                    f.write(chunk)
        elif os.path.isabs(source) and os.path.isfile(source):
            shutil.copy2(source, target)
        else:
            return {"ok": False, "error": "只允许 https 地址或存在的本机绝对路径"}
        data = target.read_bytes()
        if not data.startswith(b"PK\x03\x04"):
            target.unlink(missing_ok=True)
            return {"ok": False, "error": "下载内容不是 APK/ZIP"}
        sha = hashlib.sha256(data).hexdigest()
        aapt = shutil.which("aapt") or shutil.which("apkanalyzer")
        package, permissions, label = "", [], ""
        if aapt and os.path.basename(aapt).startswith("aapt"):
            badging = _run([aapt, "dump", "badging", str(target)], 60)
            text = str(badging.get("stdout") or "")
            import re
            m = re.search(r"package: name='([^']+)'", text); package = m.group(1) if m else ""
            m = re.search(r"application-label:'([^']*)'", text); label = m.group(1) if m else ""
            permissions = re.findall(r"uses-permission: name='([^']+)'", text)
        elif aapt:
            pkg = _run([aapt, "manifest", "application-id", str(target)], 60)
            perms = _run([aapt, "manifest", "permissions", str(target)], 60)
            package = str(pkg.get("stdout") or "").strip()
            permissions = [line.strip() for line in str(perms.get("stdout") or "").splitlines() if line.strip()]
            label = package
        # 没 aapt 时让 adb install --test-only 做解析会改变设备，不能用；明确阻塞而非盲装。
        if not package:
            target.unlink(missing_ok=True)
            return {"ok": False, "error": "缺 aapt/apkanalyzer，无法安全识别 APK 包名；已拒绝安装"}
        manifest = {"token": token, "source": source, "path": str(target), "package": package,
                    "label": label or package, "permissions": permissions, "sha256": sha,
                    "bytes": len(data), "inspected_at": time.time()}
        (root / f"{token}.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, **{k: v for k, v in manifest.items() if k != "path"}}
    except Exception as exc:
        target.unlink(missing_ok=True)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def install_apk(token: str, confirm=False):
    if cloud_configured():
        return {"ok": False, "error": "当前手机连接未授权APK安装，未操作任何设备"}
    """安装已检查 APK；必须明确 confirm=True，且 token/sha256 二次校验。"""
    if not confirm:
        return {"ok": False, "needs_confirmation": True, "error": "安装 APK 需要用户明确确认"}
    root = pathlib.Path(legion_paths.under("mobile", "apk-staging"))
    token = "".join(c for c in str(token or "") if c.isalnum())[:40]
    meta = root / f"{token}.json"
    apk = root / f"{token}.apk"
    if not meta.is_file() or not apk.is_file():
        return {"ok": False, "error": "APK 检查记录不存在或已过期，请重新检查"}
    info = json.loads(meta.read_text(encoding="utf-8"))
    if hashlib.sha256(apk.read_bytes()).hexdigest() != info.get("sha256"):
        return {"ok": False, "error": "APK 在确认前发生变化，拒绝安装"}
    ready = ensure()
    if not ready.get("ok"):
        return ready
    result = _adb("install", "-r", "-g", str(apk), timeout=300)
    packages = _adb("shell", "pm", "list", "packages", info.get("package"), timeout=30)
    installed = info.get("package") in str(packages.get("stdout") or "")
    return {"ok": result.get("ok") and installed, "package": info.get("package"),
            "label": info.get("label"), "installed": installed,
            "stdout": str(result.get("stdout") or "")[-3000:],
            "error": str(result.get("stderr") or result.get("error") or "")[-2000:],
            "needs_account": True,
            "next_action": "请打开 App 完成注册/登录；军团会为此创建待办对话，完成后续跑原任务。"}


def _app_id() -> str:
    """网关 appid:ctx 没有(catalog 层),按铁律 env 兜底,env 没有读 models.json 首选。"""
    appid = os.environ.get("MEYO_UNIFIED_APPID") or os.environ.get("AIGC_APPID") or ""
    if appid:
        return appid
    try:
        catalog = json.loads(pathlib.Path.home().joinpath(
            ".claude/skills/aigc-model-router/models.json").read_text())
        return str((((catalog.get("programming") or {}).get("candidates") or [{}])[0]
                   .get("appids") or [""])[0])
    except Exception:
        return ""


def task(command, steps=None, timeout_s=None, model="", serial=""):
    if cloud_configured():
        return {"ok": False, "error": "请通过军团已授权的手机工具执行；不调用旧本机MobileRun或额外收费的Agent"}
    """自然语言手机任务(MobileRun agent 真跑):多步 UI 操作 + 轨迹/截图/宏真落盘。

    OpenAI Responses 端点在网关 404(实测 /v1/openai/native/responses 不存在),
    OpenAILike(openai_compatible 别名)走 /chat/completions 真通——这正是
    "生活场景伙伴"的执行通道:像人一样看屏幕、点、滑、输入,并留下可重放的宏。
    """
    command = str(command or "").strip()
    if not command:
        return {"ok": False, "error": "empty task"}
    ready = ensure()
    if not ready["ok"]:
        return ready
    if not _runtime_path().is_file():
        return {"ok": False, "error": "MobileRun runtime 未安装"}
    gateway = os.environ.get("MEYO_UNIFIED_GATEWAY") or "https://aigc.sankuai.com/v1/openai/native"
    argv = [str(_runtime_path()), "run", command,
            "--device", serial or PHONE_SERIAL,
            "--provider", "openai_compatible",
            "--model", model or os.environ.get("MEYO_PHONE_MODEL") or "gpt-5.6-sol",
            "--api_base", gateway,
            "--steps", str(int(steps or 12)),
            "--no-tracing", "--no-stream", "--reasoning", "--vision",
            "--save-trajectory", "action"]
    run_env = dict(os.environ)
    run_env.setdefault("OPENAI_API_KEY", _app_id())
    run_env.pop("ELECTRON_RUN_AS_NODE", None)
    started = time.time()
    try:
        result = subprocess.run(argv, capture_output=True, text=True,
                               timeout=int(timeout_s or 600), env=run_env,
                               cwd=str(_runtime_path().parent.parent.parent))
    except subprocess.TimeoutExpired:
        return {"ok": False, "status": "timeout", "error": f"timeout after {timeout_s or 600}s"}
    stdout = str(result.stdout or "")
    stderr = str(result.stderr or "")
    out = {"ok": result.returncode == 0, "exit_code": result.returncode,
           "action": "task", "stdout": stdout[-20000:], "stderr": stderr[-4000:],
           "elapsed_ms": round((time.time() - started) * 1000)}
    # 交付多样性:轨迹目录(截图/ui_states/macro.json)是真产物,从 stdout 摘给调用方。
    # CLI 在窄终端会把路径折行输出(实测 74 列折成两行),逐行拼跨行路径再判目录。
    lines = [line.rstrip() for line in stdout.splitlines()]
    for i in range(len(lines) - 1, -1, -1):
        candidate = lines[i].strip()
        if "Trajectory saved" not in candidate and "trajectories" not in candidate:
            continue
        joined = candidate
        j = i + 1
        while "trajectories" not in joined and j < len(lines) and j - i <= 3:
            joined = (joined + lines[j].strip()).strip()
            j += 1
        if "trajectories" not in joined:
            continue
        # 去掉 "📁 Trajectory saved:" 前缀只留路径
        path_part = joined.split("trajectories", 1)[-1]
        path_part = ("trajectories" + path_part).strip().rstrip(":")
        if os.path.isdir(path_part):
            out["trajectory_dir"] = path_part
            break
    # stdout 折行摘取失败时兜底:扫 runtime trajectories 下 mtime 在本次任务窗口内的唯一目录
    if not out.get("trajectory_dir"):
        traj_root = _runtime_path().parent.parent.parent / "trajectories"
        try:
            recent = [p for p in traj_root.iterdir()
                      if p.is_dir() and p.stat().st_mtime >= started - 5]
            if len(recent) == 1:
                out["trajectory_dir"] = str(recent[0])
        except Exception:
            pass
    folder = out.get("trajectory_dir") or ""
    if folder:
        out["deliveries"] = {
            "screenshots": sorted(str(p) for p in pathlib.Path(folder).glob("screenshots/*.png")),
            "macro": str(pathlib.Path(folder) / "macro.json"),
            "trajectory": str(pathlib.Path(folder) / "trajectory.json"),
            "gif": str(pathlib.Path(folder) / "screenshots" / "trajectory.gif"),
        }
    return out


def replay_macro(macro_path: str, *, start_from=1, max_steps=0, on_mismatch="agent",
                 timeout_s=600, model="", serial=""):
    """按原生状态守卫重放成功流程；UI 漂移时交回 MobileRun Agent 自愈。"""
    ready = ensure()
    if not ready.get("ok"):
        return ready
    path = pathlib.Path(str(macro_path or "")).expanduser().resolve()
    allowed_roots = [
        pathlib.Path(legion_paths.under("mobile")).resolve(),
        (_runtime_path().parent.parent.parent / "trajectories").resolve(),
    ]
    try:
        if not path.is_file() or not any(path.is_relative_to(root) for root in allowed_roots):
            return {"ok": False, "error": "宏不存在或不在受管云手机目录"}
    except (OSError, ValueError):
        return {"ok": False, "error": "宏路径无效"}
    if on_mismatch not in {"stop", "agent"}:
        return {"ok": False, "error": "on_mismatch must be stop/agent"}
    argv = [str(_runtime_path()), "macro", "replay", str(path), "--device", serial or PHONE_SERIAL,
            "--start-from", str(max(1, int(start_from or 1))), "--on-mismatch", on_mismatch]
    if max_steps:
        argv.extend(["--max-steps", str(max(1, int(max_steps)))])
    if on_mismatch == "agent":
        argv.extend(["--provider", "openai_compatible",
                     "--model", model or os.environ.get("MEYO_PHONE_MODEL") or "gpt-5.6-sol"])
    env = dict(os.environ)
    env.setdefault("OPENAI_API_KEY", _app_id())
    env.pop("ELECTRON_RUN_AS_NODE", None)
    started = time.time()
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=int(timeout_s),
                                env=env, cwd=str(_runtime_path().parent.parent.parent))
    except subprocess.TimeoutExpired:
        return {"ok": False, "status": "timeout", "error": f"macro replay timeout after {timeout_s}s"}
    text = str(result.stdout or "") + "\n" + str(result.stderr or "")
    divergence = "State mismatch" in text or "Handing off to agent" in text
    return {"ok": result.returncode == 0 and "completed successfully" in text.lower(),
            "exit_code": result.returncode, "action": "replay", "macro": str(path),
            "on_mismatch": on_mismatch, "agent_handoff": "Handing off to agent" in text,
            "divergence": divergence, "elapsed_ms": round((time.time() - started) * 1000),
            "stdout": str(result.stdout or "")[-12000:], "stderr": str(result.stderr or "")[-4000:]}
