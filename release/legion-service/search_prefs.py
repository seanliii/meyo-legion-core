"""用户级搜索源偏好(用户 2026-08-30:搜索的强大也需要展示和自定义)。

军团有几十个搜索源(见 install_registry.discover_search_sources)。这里让用户
【自定义开关】哪些源参与搜索——存每用户的 disabled 集合(默认全开)。
军团搜索时读这个偏好过滤(context not control:只是让用户表达偏好,不改搜索逻辑本身)。
"""
from __future__ import annotations
import json
import os
import threading

try:
    import legion_paths
    _DIR = legion_paths.under("search_prefs")
except Exception:
    _DIR = os.path.join(os.path.expanduser("~/.meyo/legion"), "search_prefs")

# toggle 是 load→改→save 无锁·同用户并发开关多个源会丢更新(实测 8 并发只剩 1)。加进程内锁。
_LOCK = threading.RLock()


def _path(uid: str) -> str:
    uid = "".join(c for c in str(uid or "default") if c.isalnum() or c in "_.:-")[:80] or "default"
    return os.path.join(_DIR, f"{uid}.json")


def get(uid: str = "default") -> dict:
    """返回该用户的搜索源偏好:{disabled:[源id...]}。默认空(全开)。"""
    try:
        with open(_path(uid), encoding="utf-8") as f:
            d = json.load(f)
        return {"disabled": list(d.get("disabled") or [])}
    except Exception:
        return {"disabled": []}


def toggle(uid: str, source_id: str, enabled: bool) -> dict:
    """开/关某个搜索源。enabled=True→从 disabled 移除;False→加入 disabled。"""
    uid = str(uid or "default")
    src = str(source_id or "").strip()
    if not src:
        return {"ok": False, "error": "empty source_id"}
    with _LOCK:  # load→改→save 全程持锁·防同用户并发 toggle 丢更新
        cur = set(get(uid)["disabled"])
        if enabled:
            cur.discard(src)
        else:
            cur.add(src)
        try:
            os.makedirs(_DIR, exist_ok=True)
            with open(_path(uid), "w", encoding="utf-8") as f:
                json.dump({"disabled": sorted(cur)}, f, ensure_ascii=False)
            return {"ok": True, "disabled": sorted(cur), "source_id": src, "enabled": enabled}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
