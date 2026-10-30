#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""能力总线只读快照：HTTP 请求只聚合现成事实，慢探针在独立后台源中刷新。"""
from __future__ import annotations

import os
import shutil
import threading
import time

_TTL = 600.0
_LOCK = threading.RLock()
_SOURCES: dict[str, tuple[float, object]] = {}
_REFRESHING: set[str] = set()


def invalidate(name: str = "") -> None:
    with _LOCK:
        if name:
            _SOURCES.pop(name, None)
        else:
            _SOURCES.clear()


def _media() -> list:
    import media_tools
    aigc_ok = os.path.isfile(os.path.expanduser(
        "~/.claude/skills/aigc-model-router/scripts/call_model.py"))
    ffmpeg_ok = bool(shutil.which("ffmpeg"))
    rows = media_tools.list_media_tools()
    for row in rows:
        tool_id = row.get("id")
        if tool_id in ("gen_image", "see_image"):
            row["ready"] = aigc_ok
            row["needs"] = None if aigc_ok else "aigc-model-router skill(生图/识图需要)"
        elif tool_id in ("trim_media", "extract_audio", "video_frame"):
            row["ready"] = ffmpeg_ok
            row["needs"] = None if ffmpeg_ok else "ffmpeg(brew install ffmpeg)"
        elif tool_id == "gen_video":
            row["ready"] = False
            row["needs"] = "网关视频配额(当前枯竭)"
        else:
            row["ready"] = True
            row["needs"] = None
    return rows


def _search() -> dict:
    import install_registry
    value = install_registry.discover_search_sources()
    summary = value.get("summary") or {}
    return {"systems": len(value.get("systems") or []),
            "engines": int(summary.get("total_engines") or 0),
            "ready": int(summary.get("ready") or 0),
            "by_system": [{"name": row.get("system"), "engines": len(row.get("engines") or [])}
                          for row in value.get("systems") or []]}


def _file() -> list:
    import file_hub  # noqa: F401
    return ["describe(任意文件→类型+预览)", "read_spreadsheet(Excel/CSV)",
            "read_code / describe_repo(本地代码库)", "to_harness_context(喂给harness)"]


def _web() -> list:
    import opencli_bridge
    return ["opencli(100+网站→CLI·JSON喂harness)"] if opencli_bridge.available() else []


def _ecosystem() -> list:
    import meyo_harnesses.backends as backends
    rows = []
    for name, probe in backends.BACKEND_PROBES.items():
        if name == "omnigent":
            continue
        try:
            state = probe()
        except Exception as exc:
            state = {"available": False, "reason": f"{type(exc).__name__}:{exc}"}
        rows.append({"key": name, **state})
    return rows


def _installed() -> dict:
    import install_registry
    value = install_registry.discover_environment()
    summary = value.get("summary") or {}
    return {"total": int(summary.get("total") or 0), "by_kind": summary.get("by_kind") or {}}


def _toolbox() -> dict:
    import toolbox
    value = toolbox.toolbox_summary()
    summary = value.get("summary") or {}
    return {"total": int(summary.get("total") or 0),
            "skills": int(summary.get("skills") or 0),
            "runnable": int(summary.get("runnable_tools") or 0),
            "warming": bool(value.get("warming") or value.get("stale"))}


_BUILDERS = {"media": _media, "search": _search, "file": _file, "web": _web,
             "ecosystem": _ecosystem, "installed": _installed, "toolbox": _toolbox}
_EMPTY = {"media": [], "search": {}, "file": [], "web": [], "ecosystem": [],
          "installed": {}, "toolbox": {}}
_HOW_TO_CALL = {
    "media": "run_media_tool(tool_id, params) 或 POST /api/legion/tool",
    "search": "search_fabric.SearchFabric().search(query, top_k=8)",
    "file": "file_hub.describe(path) / 上传走 /api/legion/upload",
    "web": "opencli_bridge.run(args) 或 POST /api/legion/opencli",
    "ecosystem": "meyo_harnesses/backends/<key>_backend.py；先 probe 再调用",
    "installed": "pip库直接import·CLI直接调·GitHub仓库在repo内",
    "toolbox": "GET /api/legion/toolbox?cap= 按能力筛",
}


def _refresh(name: str) -> None:
    try:
        value = _BUILDERS[name]()
        with _LOCK:
            _SOURCES[name] = (time.time(), value)
    except Exception:
        pass
    finally:
        with _LOCK:
            _REFRESHING.discard(name)


def _schedule(name: str) -> None:
    with _LOCK:
        if name in _REFRESHING:
            return
        _REFRESHING.add(name)
    threading.Thread(target=_refresh, args=(name,), daemon=True,
                     name=f"capability-view-{name}").start()


def snapshot(force: bool = False) -> dict:
    now = time.time()
    values, warming, stale = {}, [], []
    for name in _BUILDERS:
        with _LOCK:
            hit = _SOURCES.get(name)
        fresh = bool(hit and now - hit[0] < _TTL)
        if force:
            _refresh(name)
            with _LOCK:
                hit = _SOURCES.get(name)
            fresh = True
        elif not fresh:
            _schedule(name)
        if hit:
            values[name] = hit[1]
            if not fresh:
                stale.append(name)
        else:
            values[name] = _EMPTY[name]
            warming.append(name)
    ecosystem = values["ecosystem"] if isinstance(values["ecosystem"], list) else []
    search = values["search"] if isinstance(values["search"], dict) else {}
    installed = values["installed"] if isinstance(values["installed"], dict) else {}
    toolbox = values["toolbox"] if isinstance(values["toolbox"], dict) else {}
    summary = {"media": len(values["media"] or []),
               "search_engines": int(search.get("engines") or 0),
               "file": len(values["file"] or []), "web": len(values["web"] or []),
               "ecosystem": len(ecosystem),
               "ecosystem_ready": sum(bool(row.get("available")) for row in ecosystem),
               "installed": int(installed.get("total") or 0),
               "toolbox": int(toolbox.get("total") or 0)}
    return {**values, "how_to_call": dict(_HOW_TO_CALL), "summary": summary,
            "from_cache": not warming, "warming": bool(warming), "stale": bool(stale),
            "warming_sources": warming, "stale_sources": stale}


def health() -> dict:
    value = snapshot()
    summary = value["summary"]
    return {"armed": summary["media"] > 0 and summary["search_engines"] > 0
                     and summary["installed"] > 0,
            "media_ready": summary["media"], "search_engines": summary["search_engines"],
            "file_ops": summary["file"], "web_tools": summary["web"],
            "ecosystem_ready": summary["ecosystem_ready"],
            "ecosystem_total": summary["ecosystem"], "installed_libs": summary["installed"],
            "toolbox_items": summary["toolbox"], "warming": value["warming"],
            "stale": value["stale"], "warming_sources": value["warming_sources"]}
