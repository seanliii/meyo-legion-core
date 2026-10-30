#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轻量读视图：把首屏和“我的军团”从重型全量扫描中拆出来。"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import legion_paths as _paths

_CACHE_TTL = 15.0
_CACHE: dict[tuple, tuple[float, dict]] = {}
_REFRESHING: set[tuple] = set()
_LOCK = threading.RLock()


def _store(key: tuple, build) -> dict:
    started = time.perf_counter()
    value = build()
    value["built_ms"] = round((time.perf_counter() - started) * 1000, 1)
    value["updated_at"] = time.time()
    with _LOCK:
        _CACHE[key] = (value["updated_at"], value)
    return value


def _refresh(key: tuple, build) -> None:
    try:
        _store(key, build)
    except Exception:
        # SWR 后台失败不能污染请求线程或清掉旧值；释放 single-flight 后允许下次重试。
        pass
    finally:
        with _LOCK:
            _REFRESHING.discard(key)


def _schedule_refresh(key: tuple, build) -> None:
    with _LOCK:
        if key in _REFRESHING:
            return
        _REFRESHING.add(key)
    threading.Thread(target=_refresh, args=(key, build), daemon=True,
                     name=f"view-refresh-{key[0]}").start()


def _cached(key: tuple, build, force: bool = False) -> dict:
    if not force:
        with _LOCK:
            hit = _CACHE.get(key)
        if hit:
            stale = time.time() - hit[0] >= _CACHE_TTL
            if stale:
                _schedule_refresh(key, build)
            return {**hit[1], "from_cache": True, "stale": stale, "warming": stale}
    value = _store(key, build)
    return {**value, "from_cache": False, "stale": False, "warming": False}


def invalidate(uid: str | None = None) -> None:
    with _LOCK:
        if uid is None:
            _CACHE.clear()
            return
        uid = str(uid or "default")
        for key in [key for key in _CACHE if uid in key]:
            _CACHE.pop(key, None)


def _kind(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"):
        return "image"
    if ext in (".mp4", ".mov", ".webm"):
        return "video"
    if ext in (".mp3", ".wav", ".m4a", ".ogg"):
        return "audio"
    if ext in (".xlsx", ".csv", ".xls"):
        return "spreadsheet"
    if ext == ".pdf":
        return "pdf"
    if ext in (".html", ".htm"):
        return "web"
    if ext in (".py", ".js", ".ts", ".rs", ".go", ".java", ".c", ".cpp"):
        return "code"
    if ext in (".md", ".txt", ".json"):
        return "doc"
    return "file"


def _session_signals(recent: list[dict], session_limit: int = 12,
                     artifact_limit: int = 60) -> tuple[list[dict], dict]:
    """从列表扫描时附带的轻量信号提取交付物和常用成员，不再重读会话 JSON。"""
    rows, seen, counts = [], set(), {}
    for summary in recent[:session_limit]:
        for key, count in (summary.get("agents") or {}).items():
            counts[key] = counts.get(key, 0) + int(count or 0)
        for artifact in summary.get("artifacts") or []:
            identity = str(artifact.get("url") or artifact.get("path") or artifact.get("name") or "")
            if not identity or identity in seen:
                continue
            seen.add(identity)
            url = str(artifact.get("url") or "")
            rows.append({
                "name": artifact.get("name") or os.path.basename(identity),
                "url": url,
                "path": artifact.get("path") or "",
                "download_url": (url + ("&" if "?" in url else "?") + "download=1") if url else "",
                "kind": artifact.get("kind") or _kind(identity),
                "bytes": int(artifact.get("bytes") or 0),
                "sid": summary.get("sid"),
                "title": summary.get("title") or "",
                "question": str(summary.get("title") or "")[:160],
                "ts": float(summary.get("updated") or 0),
                "source": artifact.get("source") or artifact.get("key") or artifact.get("harness") or "",
                **{key: artifact[key] for key in
                   ("delivery_role", "role", "is_primary", "entrypoint", "description")
                   if key in artifact},
            })
            if len(rows) >= artifact_limit:
                break
    return rows, dict(sorted(counts.items(), key=lambda item: -item[1]))


def session_index(uid: str = "default", limit: int = 12, include_artifacts: bool = True,
                  force: bool = False) -> dict:
    uid, limit = str(uid or "default"), max(1, min(int(limit or 12), 50))

    def build():
        import sessions
        recent = sessions.list_sessions(limit=limit, user_id=uid)
        artifacts, favorites = _session_signals(recent) if include_artifacts else ([], {})
        return {"ok": True, "uid": uid, "sessions": recent,
                "artifacts": artifacts, "favorite_agents": favorites}

    return _cached(("session-index", uid, limit, bool(include_artifacts)), build, force)


def mine_summary(uid: str = "default", force: bool = False) -> dict:
    uid = str(uid or "default")

    def build():
        import captain_preferences
        import legion_memory
        import legion_pilot
        import skill_factory

        index = session_index(uid, limit=12, include_artifacts=True, force=force)
        sessions = index.get("sessions") or []
        favorites = index.get("favorite_agents") or {}
        skills = skill_factory.list_active(uid).get("skills") or []
        captain = captain_preferences.load(uid)
        pilot = legion_pilot.load_user_config(uid)
        memory_rows = legion_memory._load()
        memory = [r for r in memory_rows
                  if str(r.get("user_id") or "") == uid
                  or (uid == "default" and not r.get("user_id"))]
        outputs = Path(_paths.under("outputs"))
        compute = {"sandbox_present": outputs.is_dir(), "cloud_desktop": "unknown"}
        try:
            import my_workspace
            hit = my_workspace._COMPUTE_STATUS_CACHE.get(uid)
            if hit and time.time() - hit["ts"] < my_workspace._COMPUTE_STATUS_TTL:
                cloud = hit["data"]
                compute["cloud_desktop"] = "ready" if cloud.get("desktop_ready") else "stopped"
        except Exception:
            pass
        return {
            "ok": True,
            "uid": uid,
            "favorite_agents": {"count": len(favorites), "top": list(favorites.items())[:6]},
            "skills": {"count": len(skills), "items": [s.get("name") for s in skills[:12]]},
            "captain": {"mode": captain.get("mode", "auto"),
                        "preferred": captain.get("preferred_captain", "")},
            "pilot": {"enabled": bool(pilot.get("enabled")), "scope": pilot.get("scope", "session")},
            "memory": {"count": len(memory),
                       "items": [{"q": str(r.get("q") or "")[:80], "source": r.get("source") or "",
                                  "ts": r.get("ts") or 0} for r in sorted(memory, key=lambda r: r.get("ts", 0), reverse=True)[:6]]},
            "recent_sessions": sessions[:6],
            "recent_artifacts": (index.get("artifacts") or [])[:12],
            "compute": compute,
        }

    return _cached(("mine-summary", uid), build, force)


def file_index(uid: str = "default", limit: int = 50, force: bool = False) -> dict:
    uid, limit = str(uid or "default"), max(1, min(int(limit or 50), 200))

    def build():
        outputs = Path(_paths.under("outputs"))
        if not outputs.is_dir():
            return {"ok": True, "uid": uid, "count": 0, "items": []}
        # 非 default 用户只能看自己 session 明确登记过的产物；缓存键隔离不是数据隔离。
        # 旧产物没有 user_id 归 default，保持兼容但绝不暴露给其他用户。
        owned = None
        if uid != "default":
            import sessions
            owned = set()
            for summary in sessions.list_sessions(limit=1000, user_id=uid):
                for artifact in summary.get("artifacts") or []:
                    raw_path = str(artifact.get("path") or "")
                    raw_url = str(artifact.get("url") or "")
                    if raw_path:
                        try:
                            owned.add(Path(raw_path).resolve())
                        except OSError:
                            pass
                    if raw_url.startswith("/home-outputs/"):
                        try:
                            owned.add((outputs / raw_url.removeprefix("/home-outputs/")).resolve())
                        except OSError:
                            pass
        candidates = []
        for workdir in outputs.iterdir():
            try:
                if workdir.is_dir():
                    candidates.append((workdir.stat().st_mtime, workdir))
                elif workdir.is_file() and not workdir.name.startswith("."):
                    candidates.append((workdir.stat().st_mtime, workdir))
            except OSError:
                continue
        candidates.sort(reverse=True, key=lambda item: item[0])
        items = []
        for _, root in candidates[:80]:
            paths = [root] if root.is_file() else root.rglob("*")
            for path in paths:
                try:
                    if not path.is_file() or path.name.startswith("."):
                        continue
                    if any(part in (".git", "node_modules", "__pycache__") for part in path.parts):
                        continue
                    if owned is not None and path.resolve() not in owned:
                        continue
                    st = path.stat()
                except OSError:
                    continue
                rel = path.relative_to(outputs).as_posix()
                url = "/home-outputs/" + rel
                items.append({"name": path.name, "rel": rel, "size": st.st_size,
                              "mtime": int(st.st_mtime), "kind": _kind(path.name),
                              "url": url, "download_url": url + "?download=1"})
                if len(items) >= limit * 3:
                    break
            if len(items) >= limit * 3:
                break
        items.sort(key=lambda item: item["mtime"], reverse=True)
        return {"ok": True, "uid": uid, "count": len(items), "items": items[:limit],
                "root": str(outputs)}

    return _cached(("file-index", uid, limit), build, force)
