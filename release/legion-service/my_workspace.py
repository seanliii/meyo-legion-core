#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""my_workspace:用户【配置和查看自己的】工作区(用户2026-08-30).

用户诉求:配置和查看自己的这种 md 和 pilot、还有自己的沙盒文件、或者云电脑,是否在,
用户界面是否可以配置和正确展示。盘点后端:pilot 全有;memory/沙盒文件/云电脑后端有基础
但缺"列我的/看状态"的聚合入口·前端也缺展示。本模块补聚合(只读+链现有配置·不新造存储)。

四块:
1. my_memory   : 用户自己的经验/记忆(legion_memory 里 user_id 匹配的·可查看)
2. my_files    : 用户的沙盒/产出文件(~/.meyo/legion/outputs/ 下·可查看可下载)
3. my_pilot    : Pilot 续航配置(legion_pilot·可配置·链现有 /api/legion/pilot)
4. my_compute  : 沙盒/云电脑环境是否在(探测·诚实报在/不在)
"""
from __future__ import annotations
import json
import os
import threading
import time


def _legion_dir() -> str:
    try:
        import legion_paths
        return legion_paths.legion_dir()
    except Exception:
        return os.path.expanduser("~/.meyo/legion")


def my_memory(uid: str = "default", limit: int = 30) -> dict:
    """列出属于该用户的记忆/经验(可查看自己的md类知识)。"""
    try:
        import legion_memory
        rows = legion_memory._load()
    except Exception as e:
        return {"ok": False, "error": f"memory不可用: {e}"}
    mine = [r for r in rows if str(r.get("user_id") or "") == uid or
            (uid == "default" and not r.get("user_id"))]
    mine.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return {"ok": True, "uid": uid, "count": len(mine),
            "items": [{"q": r.get("q", "")[:100], "a": r.get("a", "")[:160],
                       "source": r.get("source", ""), "tags": r.get("tags", []),
                       "mtype": r.get("mtype", ""), "adopted": r.get("adopted", 0),
                       "ts": int(r.get("ts", 0))}  # 删除定位用(配置md:用户可删自己的)
                      for r in mine[:limit]]}


def my_files(uid: str = "default", limit: int = 50) -> dict:
    """列出用户沙盒产出；复用有界文件索引，避免每次面板打开全量 os.walk。"""
    try:
        import performance_views
        out = performance_views.file_index(uid, limit=limit)
        return {**out, "note": "沙盒产出·可下载/预览;军团各家产物写这里"}
    except Exception as e:
        return {"ok": False, "error": f"扫沙盒失败: {e}"}


def _kind(fn: str) -> str:
    ext = os.path.splitext(fn)[1].lower()
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        return "image"
    if ext in (".mp4", ".mov", ".webm"):
        return "video"
    if ext in (".xlsx", ".csv", ".xls"):
        return "spreadsheet"
    if ext in (".pdf",):
        return "pdf"
    if ext in (".html", ".htm"):
        return "web"
    if ext in (".py", ".js", ".ts", ".rs", ".go", ".java", ".c", ".cpp"):
        return "code"
    if ext in (".md", ".txt"):
        return "doc"
    return "file"


def my_pilot(uid: str = "default") -> dict:
    """Pilot 续航配置(可配置·此处只读回展示·配置改动走 POST /api/legion/pilot)。"""
    try:
        import legion_pilot
        cfg = legion_pilot.load_user_config(uid)
        human = legion_pilot.settings_for_humans(cfg)
        return {"ok": True, "uid": uid, "config": human,
                "note": "续航=咬住主目标自动推进;可在界面开关+设范围(turn/session)。改动走 /api/legion/pilot"}
    except Exception as e:
        return {"ok": False, "error": f"pilot不可用: {e}"}


# 云电脑状态展示缓存(2026-09-01 loop 揪出:my_workspace 面板 2.18s·大头是 cloud_desktop.status
# 探 docker/container/noVNC HTTP 的 1.48s)。这是【展示热路径】·状态不需毫秒级实时→加 30s TTL 缓存·
# 首次探完后老用户打开"我的工作区/进化"面板秒回。★只缓存展示路径(my_compute)·不动 status 本身→
# install_control/start_session 等【操作后】仍拿实时真状态(同 catalog/autonomy_matrix/aigc_ready 的
# "同步探测阻塞热路径→缓存/SWR"修法·别在展示场景付真实探测成本)。
_COMPUTE_STATUS_CACHE: dict = {}   # uid -> {"ts":float,"data":dict}
_COMPUTE_STATUS_TTL = 30.0
_COMPUTE_REFRESHING: set[str] = set()
_COMPUTE_LOCK = threading.RLock()


def _refresh_compute(uid: str) -> None:
    try:
        import cloud_desktop
        data = cloud_desktop.status(uid, force=True)
        with _COMPUTE_LOCK:
            _COMPUTE_STATUS_CACHE[uid] = {"ts": time.time(), "data": data}
    finally:
        with _COMPUTE_LOCK:
            _COMPUTE_REFRESHING.discard(uid)


def _schedule_compute(uid: str) -> None:
    with _COMPUTE_LOCK:
        if uid in _COMPUTE_REFRESHING:
            return
        _COMPUTE_REFRESHING.add(uid)
    threading.Thread(target=_refresh_compute, args=(uid,), daemon=True,
                     name="workspace-compute-refresh").start()


def _cua_status_cached(uid: str) -> dict:
    with _COMPUTE_LOCK:
        ent = _COMPUTE_STATUS_CACHE.get(uid)
    if ent:
        if time.time() - ent["ts"] >= _COMPUTE_STATUS_TTL:
            _schedule_compute(uid)
        return {**ent["data"], "from_cache": True,
                "stale": time.time() - ent["ts"] >= _COMPUTE_STATUS_TTL}
    _schedule_compute(uid)
    return {"ok": True, "uid": uid, "warming": True, "control_ready": False,
            "desktop_ready": False, "display_ready": False,
            "hint": "云端桌面状态正在后台更新"}


def my_compute(uid: str = "default") -> dict:
    """聚合本地产出目录与该用户绑定的 CUA 官方 sandbox 真实状态。"""
    result = {"ok": True, "uid": uid, "sandbox": {}, "cloud_desktop": {}}
    outputs = os.path.join(_legion_dir(), "outputs")
    result["sandbox"] = {"present": os.path.isdir(outputs), "path": outputs,
                         "note": "本地沙盒(产出/工作目录)·军团各家在此读写"}
    try:
        cua = _cua_status_cached(uid)
        result["cloud_desktop"] = {
            "present": bool(cua.get("control_ready")),
            "control_ready": bool(cua.get("control_ready")),
            "desktop_ready": bool(cua.get("desktop_ready")),
            "display_ready": bool(cua.get("display_ready")),
            "sandbox_name": cua.get("sandbox_name"),
            "status": cua,
            "note": cua.get("hint", "CUA 状态不可用"),
        }
    except Exception as exc:
        result["cloud_desktop"] = {"present": False, "control_ready": False,
                                   "desktop_ready": False, "error": f"{type(exc).__name__}: {exc}"}
    return result



def workspace(uid: str = "default") -> dict:
    """聚合:用户配置和查看自己的一切(memory/文件/pilot/云电脑)。"""
    return {
        "ok": True, "uid": uid,
        "positioning": "你的工作区:配置和查看你自己的记忆(md)、沙盒文件、Pilot续航、云电脑是否在",
        "my_memory": my_memory(uid),
        "my_files": my_files(uid),
        "my_pilot": my_pilot(uid),
        "my_compute": my_compute(uid),
    }


if __name__ == "__main__":
    os.environ.setdefault("MEYO_LEGION_DIR", os.path.expanduser("~/.meyo/legion"))
    import sys
    uid = sys.argv[1] if len(sys.argv) > 1 else "default"
    print(json.dumps(workspace(uid), ensure_ascii=False, indent=2)[:1500])
