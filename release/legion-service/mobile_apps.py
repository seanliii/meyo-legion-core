#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Android 软件生命周期：可信目录、安装计划、设备复检和可回滚快照。"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import pathlib
import re
import shutil
import tempfile
import time
import urllib.parse
import urllib.request
import uuid

import legion_paths
import mobile_device

FDROID_INDEX = os.environ.get("MEYO_FDROID_INDEX", "https://f-droid.org/repo/index-v1.jar")
_MAX_APK_BYTES = 256 * 1024 * 1024
_PLAN_TTL = 24 * 3600


def _root() -> pathlib.Path:
    return pathlib.Path(legion_paths.under("mobile", "apps"))


def _atomic_json(path: pathlib.Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".mobile-app-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _fetch(url: str, target: pathlib.Path, timeout_s=120, max_bytes=_MAX_APK_BYTES) -> dict:
    if urllib.parse.urlsplit(url).scheme != "https":
        return {"ok": False, "error": "软件源只允许 HTTPS"}
    target.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Meyo-Mobile/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=int(timeout_s)) as response, target.open("wb") as handle:
            total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > int(max_bytes):
                    raise ValueError(f"下载超过 {int(max_bytes) // (1024 * 1024)}MiB 限制")
                handle.write(chunk)
        return {"ok": True, "bytes": total, "sha256": _sha256(target)}
    except Exception as exc:
        target.unlink(missing_ok=True)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_fdroid_index(path: pathlib.Path) -> dict:
    """读取 F-Droid index-v1.jar；测试/镜像也允许直接 JSON 或 gzip JSON。"""
    data = path.read_bytes()
    if data.startswith(b"PK\x03\x04"):
        import zipfile
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.endswith("index-v1.json")]
            if not names:
                raise ValueError("F-Droid 索引缺 index-v1.json")
            data = archive.read(names[0])
    elif data.startswith(b"\x1f\x8b"):
        data = gzip.decompress(data)
    value = json.loads(data.decode("utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("apps"), list):
        raise ValueError("F-Droid 索引结构无效")
    return value


def _index_path() -> pathlib.Path:
    return _root() / "catalog" / "fdroid-index-v1.jar"


def refresh_catalog(*, force=False, timeout_s=120) -> dict:
    path = _index_path()
    if path.is_file() and not force and time.time() - path.stat().st_mtime < 6 * 3600:
        try:
            index = _read_fdroid_index(path)
            return {"ok": True, "source": "fdroid", "url": FDROID_INDEX,
                    "cached": True, "apps": len(index.get("apps") or [])}
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    fetched = _fetch(FDROID_INDEX, path, timeout_s=timeout_s, max_bytes=64 * 1024 * 1024)
    if not fetched.get("ok"):
        return fetched
    try:
        index = _read_fdroid_index(path)
    except Exception as exc:
        path.unlink(missing_ok=True)
        return {"ok": False, "error": f"F-Droid 索引校验失败: {exc}"}
    return {"ok": True, "source": "fdroid", "url": FDROID_INDEX,
            "cached": False, "apps": len(index.get("apps") or []), "sha256": fetched["sha256"]}


def _localized(value, locale="zh-CN") -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    for key in (locale, "zh-CN", "zh", "en-US", "en"):
        if value.get(key):
            return str(value[key])
    return str(next(iter(value.values()), ""))


def _packages(index: dict) -> dict[str, list[dict]]:
    packages = index.get("packages") or {}
    return packages if isinstance(packages, dict) else {}


def _app_row(app: dict, versions: list[dict]) -> dict:
    package = str(app.get("packageName") or app.get("package") or "")
    candidates = [item for item in versions if isinstance(item, dict)]
    candidates.sort(key=lambda item: int(item.get("versionCode") or 0), reverse=True)
    latest = candidates[0] if candidates else {}
    repo_url = FDROID_INDEX.rsplit("/", 1)[0] + "/"
    apk_name = str(latest.get("apkName") or "")
    hash_value = str(latest.get("hash") or "")
    return {
        "provider": "fdroid", "trusted": True, "package": package,
        "name": _localized(app.get("name")) or package,
        "summary": _localized(app.get("summary")),
        "description": _localized(app.get("description"))[:2000],
        "developer": str(app.get("authorName") or app.get("authorEmail") or "F-Droid catalog"),
        "source_url": str(app.get("sourceCode") or app.get("webSite") or ""),
        "repo_url": FDROID_INDEX, "version_name": str(latest.get("versionName") or ""),
        "version_code": int(latest.get("versionCode") or 0),
        "permissions": sorted(str(item) for item in (latest.get("uses-permission") or []) if item),
        "apk_url": urllib.parse.urljoin(repo_url, apk_name) if apk_name else "",
        "sha256": hash_value if str(latest.get("hashType") or "sha256").lower() in {"sha256", "sha-256"} else "",
        "signer": str(latest.get("signer") or ""), "added": latest.get("added"),
        "size": int(latest.get("size") or 0), "versions_available": len(candidates),
    }


def search(query: str, *, limit=20, refresh=False) -> dict:
    query = str(query or "").strip().casefold()
    refreshed = refresh_catalog(force=refresh)
    if not refreshed.get("ok"):
        return {"ok": False, "error": refreshed.get("error"), "provider": "fdroid"}
    index = _read_fdroid_index(_index_path())
    packages = _packages(index)
    rows = []
    for app in index.get("apps") or []:
        if not isinstance(app, dict):
            continue
        package = str(app.get("packageName") or app.get("package") or "")
        row = _app_row(app, packages.get(package) or [])
        haystack = " ".join((row["name"], row["package"], row["summary"], row["developer"])).casefold()
        if query and query not in haystack:
            continue
        rows.append(row)
    rows.sort(key=lambda item: (query not in item["name"].casefold(), item["name"].casefold()))
    return {"ok": True, "provider": "fdroid", "trusted": True,
            "source": FDROID_INDEX, "apps": rows[:max(1, min(int(limit or 20), 100))],
            "total": len(rows), "catalog": refreshed}


def _inspect_apk(path: pathlib.Path) -> dict:
    aapt = shutil.which("aapt") or shutil.which("apkanalyzer")
    package = label = version_name = ""
    version_code = 0
    permissions = []
    if aapt and pathlib.Path(aapt).name.startswith("aapt"):
        out = mobile_device._run([aapt, "dump", "badging", str(path)], 60)
        text = str(out.get("stdout") or "")
        match = re.search(r"package: name='([^']+)'", text); package = match.group(1) if match else ""
        match = re.search(r"versionCode='([^']+)'", text); version_code = int(match.group(1)) if match and match.group(1).isdigit() else 0
        match = re.search(r"versionName='([^']*)'", text); version_name = match.group(1) if match else ""
        match = re.search(r"application-label:'([^']*)'", text); label = match.group(1) if match else ""
        permissions = sorted(set(re.findall(r"uses-permission(?:: name)?='([^']+)'", text)))
    elif aapt:
        pkg = mobile_device._run([aapt, "manifest", "application-id", str(path)], 60)
        ver = mobile_device._run([aapt, "manifest", "version-code", str(path)], 60)
        name = mobile_device._run([aapt, "manifest", "version-name", str(path)], 60)
        perms = mobile_device._run([aapt, "manifest", "permissions", str(path)], 60)
        package = str(pkg.get("stdout") or "").strip()
        version_name = str(name.get("stdout") or "").strip()
        raw_code = str(ver.get("stdout") or "").strip()
        version_code = int(raw_code) if raw_code.isdigit() else 0
        permissions = sorted(line.strip() for line in str(perms.get("stdout") or "").splitlines() if line.strip())
        label = package
    if not package:
        return {"ok": False, "error": "缺 aapt/apkanalyzer，无法核验 APK 元数据"}
    return {"ok": True, "package": package, "label": label or package,
            "version_name": version_name, "version_code": version_code,
            "permissions": permissions, "sha256": _sha256(path), "bytes": path.stat().st_size}


def plan(app: dict, *, operation="install", timeout_s=180) -> dict:
    operation = str(operation or "install")
    if operation not in {"install", "update"}:
        return {"ok": False, "error": "plan operation must be install/update"}
    if not isinstance(app, dict) or app.get("provider") != "fdroid" or not app.get("trusted"):
        return {"ok": False, "error": "只接受来自可信目录的 App 记录"}
    package = str(app.get("package") or "")
    url = str(app.get("apk_url") or "")
    expected_hash = str(app.get("sha256") or "").lower()
    if not package or not url or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        return {"ok": False, "error": "可信目录记录缺包名、下载地址或 SHA-256"}
    token = uuid.uuid4().hex[:24]
    folder = _root() / "plans" / token
    apk = folder / "candidate.apk"
    fetched = _fetch(url, apk, timeout_s=timeout_s)
    if not fetched.get("ok"):
        return fetched
    if fetched["sha256"].lower() != expected_hash:
        shutil.rmtree(folder, ignore_errors=True)
        return {"ok": False, "error": "下载 APK 的 SHA-256 与可信目录不一致"}
    inspected = _inspect_apk(apk)
    if not inspected.get("ok"):
        shutil.rmtree(folder, ignore_errors=True)
        return inspected
    if inspected["package"] != package:
        shutil.rmtree(folder, ignore_errors=True)
        return {"ok": False, "error": "APK 包名与可信目录不一致"}
    expected_version = int(app.get("version_code") or 0)
    if expected_version and inspected["version_code"] != expected_version:
        shutil.rmtree(folder, ignore_errors=True)
        return {"ok": False, "error": "APK 版本与可信目录不一致"}
    row = {"token": token, "operation": operation, "provider": "fdroid", "trusted": True,
           "source": FDROID_INDEX, "download_url": url, "package": package,
           "name": str(app.get("name") or inspected["label"]), "developer": str(app.get("developer") or ""),
           "repo_sha256": expected_hash, "sha256": inspected["sha256"],
           "version_name": inspected["version_name"], "version_code": inspected["version_code"],
           "permissions": inspected["permissions"], "bytes": inspected["bytes"],
           "signer": str(app.get("signer") or ""), "created_at": time.time(),
           "expires_at": time.time() + _PLAN_TTL, "apk_path": str(apk)}
    _atomic_json(folder / "plan.json", row)
    return {"ok": True, "needs_confirmation": True,
            **{key: value for key, value in row.items() if key != "apk_path"}}


def _safe_token(value: str) -> str:
    value = str(value or "")
    if not re.fullmatch(r"[A-Za-z0-9]{8,40}", value):
        raise ValueError("invalid mobile app token")
    return value


def _load_plan(token: str) -> tuple[dict, pathlib.Path] | tuple[None, None]:
    try:
        folder = _root() / "plans" / _safe_token(token)
        row = json.loads((folder / "plan.json").read_text(encoding="utf-8"))
        return row, folder
    except (OSError, ValueError, json.JSONDecodeError):
        return None, None


def _package_info(package: str) -> dict:
    out = mobile_device._adb("shell", "dumpsys", "package", package, timeout=45)
    text = str(out.get("stdout") or "")
    code = re.search(r"versionCode=(\d+)", text)
    name = re.search(r"versionName=([^\s]+)", text)
    paths = mobile_device._adb("shell", "pm", "path", package, timeout=30)
    path_rows = [line.split("package:", 1)[-1].strip() for line in str(paths.get("stdout") or "").splitlines()
                 if line.startswith("package:")]
    # `uninstall -k` 会在 dumpsys 中保留归档元数据与 versionCode；真正是否仍安装
    # 由 pm path 的可执行 APK 路径判断，不能把保留的数据记录误报成已安装。
    return {"installed": bool(paths.get("ok") and path_rows), "package": package,
            "version_code": int(code.group(1)) if code else 0,
            "version_name": name.group(1) if name else "", "paths": path_rows}


def inventory() -> dict:
    ready = mobile_device.ensure()
    if not ready.get("ok"):
        return ready
    packages = mobile_device._adb("shell", "pm", "list", "packages", "-3", timeout=45)
    rows = []
    for line in str(packages.get("stdout") or "").splitlines():
        package = line.split("package:", 1)[-1].strip()
        if package:
            rows.append(_package_info(package))
    rows.sort(key=lambda item: item["package"])
    return {"ok": True, "apps": rows, "total": len(rows)}


def _snapshot(package: str) -> dict:
    info = _package_info(package)
    if not info["installed"]:
        return {"ok": True, "snapshot": None}
    folder = _root() / "snapshots" / package / str(int(time.time() * 1000))
    folder.mkdir(parents=True, exist_ok=True)
    copied = []
    for index, remote in enumerate(info["paths"]):
        target = folder / ("base.apk" if index == 0 else f"split-{index}.apk")
        pull = mobile_device._adb("pull", remote, str(target), timeout=180)
        if not pull.get("ok") or not target.is_file():
            shutil.rmtree(folder, ignore_errors=True)
            return {"ok": False, "error": "更新前无法保存当前 APK，拒绝继续"}
        copied.append({"path": str(target), "sha256": _sha256(target), "bytes": target.stat().st_size})
    row = {"id": folder.name, "package": package, "version_code": info["version_code"],
           "version_name": info["version_name"], "created_at": time.time(), "apks": copied}
    _atomic_json(folder / "snapshot.json", row)
    return {"ok": True, "snapshot": row}


def apply_plan(token: str, *, confirm=False) -> dict:
    if not confirm:
        return {"ok": False, "needs_confirmation": True, "error": "安装或更新需要用户明确确认"}
    row, folder = _load_plan(token)
    if not row or not folder:
        return {"ok": False, "error": "安装计划不存在"}
    if time.time() >= float(row.get("expires_at") or 0):
        return {"ok": False, "error": "安装计划已过期"}
    apk = pathlib.Path(str(row.get("apk_path") or ""))
    if not apk.is_file() or _sha256(apk) != row.get("sha256"):
        return {"ok": False, "error": "确认前 APK 已变化，拒绝安装"}
    inspected = _inspect_apk(apk)
    if (not inspected.get("ok") or inspected.get("package") != row.get("package")
            or inspected.get("version_code") != row.get("version_code")):
        return {"ok": False, "error": "确认前 APK 元数据与计划不一致"}
    ready = mobile_device.ensure()
    if not ready.get("ok"):
        return ready
    previous = _snapshot(row["package"])
    if not previous.get("ok"):
        return previous
    installed = mobile_device._adb("install", "-r", "-g", str(apk), timeout=300)
    current = _package_info(row["package"])
    ok = bool(installed.get("ok") and current["installed"]
              and current["version_code"] == int(row.get("version_code") or 0))
    result = {"ok": ok, "operation": row["operation"], "package": row["package"],
              "name": row["name"], "source": row["source"], "sha256": row["sha256"],
              "version_name": current["version_name"], "version_code": current["version_code"],
              "installed": current["installed"], "rollback_snapshot": previous.get("snapshot"),
              "needs_account": ok, "next_state": "needs_account" if ok else "blocked",
              "error": "" if ok else str(installed.get("stderr") or installed.get("error") or "安装后版本复检失败")[-2000:]}
    _atomic_json(folder / "result.json", result)
    return result


def uninstall(package: str, *, confirm=False, keep_data=True) -> dict:
    if not confirm:
        return {"ok": False, "needs_confirmation": True, "error": "卸载需要用户明确确认"}
    if not re.fullmatch(r"[A-Za-z0-9_.]{3,180}", str(package or "")):
        return {"ok": False, "error": "invalid Android package"}
    ready = mobile_device.ensure()
    if not ready.get("ok"):
        return ready
    previous = _snapshot(package)
    if not previous.get("ok"):
        return previous
    # Android 14 的 adb uninstall -k 只打印迁移提示而不执行；设备端
    # package service 才是保留数据卸载的稳定入口。普通卸载仍走 adb uninstall。
    args = (["shell", "cmd", "package", "uninstall", "-k", package]
            if keep_data else ["uninstall", package])
    result = mobile_device._adb(*args, timeout=180)
    current = _package_info(package)
    return {"ok": bool(result.get("ok") and not current["installed"]), "package": package,
            "uninstalled": not current["installed"], "kept_data": bool(keep_data),
            "rollback_snapshot": previous.get("snapshot"),
            "error": "" if result.get("ok") else str(result.get("stderr") or result.get("error") or "")[-2000:]}


def rollback(package: str, snapshot_id: str, *, confirm=False) -> dict:
    if not confirm:
        return {"ok": False, "needs_confirmation": True, "error": "回滚需要用户明确确认"}
    if not re.fullmatch(r"[A-Za-z0-9_.]{3,180}", str(package or "")) or not re.fullmatch(r"\d{8,20}", str(snapshot_id or "")):
        return {"ok": False, "error": "invalid rollback target"}
    folder = _root() / "snapshots" / package / snapshot_id
    try:
        row = json.loads((folder / "snapshot.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "error": "回滚快照不存在"}
    paths = []
    for item in row.get("apks") or []:
        path = pathlib.Path(str(item.get("path") or ""))
        if not path.is_file() or _sha256(path) != item.get("sha256"):
            return {"ok": False, "error": "回滚快照 hash 校验失败"}
        paths.append(str(path))
    if not paths:
        return {"ok": False, "error": "回滚快照没有 APK"}
    ready = mobile_device.ensure()
    if not ready.get("ok"):
        return ready
    command = ["install-multiple", "-r", "-d"] if len(paths) > 1 else ["install", "-r", "-d"]
    result = mobile_device._adb(*command, *paths, timeout=300)
    current = _package_info(package)
    ok = bool(result.get("ok") and current["installed"]
              and current["version_code"] == int(row.get("version_code") or 0))
    return {"ok": ok, "package": package, "snapshot_id": snapshot_id,
            "version_code": current["version_code"], "version_name": current["version_name"],
            "error": "" if ok else str(result.get("stderr") or result.get("error") or "回滚后版本复检失败")[-2000:]}
