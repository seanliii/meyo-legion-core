"""legion_tool_usage · 军团工具检索的场景与伙伴学习器 v4。

把 CCR v7 的“当前对话就是场景”内化到军团公共工具链：harness 在同一运行会话中先
``search`` 得到候选，再 ``run`` 其中一件；成功执行的工具既与该次检索词项形成统计共现，
也与紧邻的上一个成功工具形成有向伙伴关系。下一次检索在基础相关候选池内轻微优化供给，
不给模型写意图规则、不凭空召回零相关工具，也不替模型选择或执行工具。

稳定性与速度契约：
- 可靠作用域 = ``MEYO_USER_ID + 完整 MEYO_SESSION_ID(key:logical-session)``；仅在 session
  缺失时退到 ``MEYO_WORKDIR/<key>_<qhash>``。同一整单的并行成员 key 不同，绝不混学。
- search 落盘的是“哈希词项 + 最终候选 ID + 时间”，不保存原始 query；run 只归因给同
  scope、时间窗口内、候选中确实包含该工具的最近一次检索。无可靠 scope 宁可不学习。
- 每个 scope 独立小文件；跨进程分片 flock + 锁内重读 + tmp/os.replace 原子写，不丢更新，
  不让 A 家搜索 PDF、B 家执行生图时串台。fcntl 不可用时退化为进程内锁。
- 查询共现与伙伴信号只在基础检索已有正相关证据的候选池内有限换位；最终 top-N 供给可
  淘汰相对弱项，但不会把零相关伙伴硬塞给模型。任意异常静默退回纯检索原序。
- 工具伙伴只学习相邻成功转移，历史和图大小均有界；失败/超时不推进序列、不建立边。
- 文件数按用户封顶，清理时也获取该文件对应分片锁并复核 mtime，不删除活跃写入。

``LEGION_TOOL_USAGE_FILE`` 只重定向文件位置（测试/多实例）；它不会凭空制造 scope。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import legion_paths as _paths

try:
    import fcntl as _fcntl
except ImportError:                                  # Windows:至少保住同进程线程安全
    _fcntl = None

_VERSION = 4
_ROOT = "tool_usage"
_PENDING_MAX = 16
_CANDIDATE_MAX = 64
_TERM_MAX = 64
_WORD_MAX = 2000
_PARTNER_SOURCE_MAX = 256
_PARTNER_PER_SOURCE_MAX = 32
_RECENT_SUCCESS_MAX = 8
_SCOPE_KEEP = 500                                    # 每用户最多保留的会话学习文件
_PRUNE_SAMPLE = 0.02                                 # 写后 2% 采样清理，避免每次扫目录
_LOCK_STRIPES = 64                                   # 固定锁文件数，避免每会话遗留一个锁
_BOOST_MAX = 1.35
_PARTNER_BOOST_MAX = 1.18
_MAX_RANK_MOVE = 2


def _env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


_ASSOCIATION_TTL_S = _env_int("LEGION_TOOL_USAGE_TTL", 600, 30)
_LOCK = threading.RLock()
_CACHE_PATH: str | None = None
_CACHE_STAT: tuple = ()
_CACHE_DATA: dict | None = None


def _safe(value: str, fallback: str = "default", limit: int = 64) -> str:
    text = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(value or ""))
    return (text or fallback)[:limit]


def _scope_context() -> dict | None:
    """返回可靠的 user+member-session 现场；没有就 None（不学习）。

    ``MEYO_SESSION_ID`` 在 legion_core 中是 ``<harness>:<conversation-or-qhash>``。完整保留
    harness key，正是为了同一 qhash 下并行成员互不串词。只有它缺失时才从 workdir
    ``<key>_<qhash>`` 恢复同等粒度；单独 MEYO_QHASH 缺 key，不足以可靠归因，故不采用。
    """
    sid = str(os.environ.get("MEYO_SESSION_ID") or "").strip()
    source = "session"
    if ":" not in sid or not all(part.strip() for part in sid.split(":", 1)):
        sid = ""
        workdir = str(os.environ.get("MEYO_WORKDIR") or "").rstrip("/")
        base = os.path.basename(workdir) if workdir else ""
        if "_" in base:
            key, logical = base.split("_", 1)
            if key.strip() and logical.strip():
                sid = f"{key}:{logical}"
                source = "workdir"
    if not sid:
        return None
    user = str(os.environ.get("MEYO_USER_ID") or
               os.environ.get("MEYO_LEGION_UID") or "default").strip() or "default"
    digest = hashlib.sha256(f"{user}\x1f{sid}".encode("utf-8")).hexdigest()[:24]
    return {"user": _safe(user), "scope_hash": digest, "source": source}


def _scoped_path(context: dict) -> Path:
    return Path(_paths.under(_ROOT, context["user"], f"{context['scope_hash']}.json"))


def _path() -> Path | None:
    """解析当前可靠 scope 的数据文件。显式路径只重定向，不绕过 scope 验证。"""
    context = _scope_context()
    if context is None:
        return None
    override = str(os.environ.get("LEGION_TOOL_USAGE_FILE") or "").strip()
    if override:
        target = Path(override)
        # override 表示“重定向根/模板”，不是把所有用户与会话强塞进同一个文件。即使测试、
        # 多实例或部署显式设置它，也必须保留 user+member-session 隔离。
        if "{scope_hash}" in override or "{user}" in override:
            return Path(override.format(user=context["user"], scope_hash=context["scope_hash"]))
        return target.with_name(f"{target.stem}.{context['scope_hash']}{target.suffix or '.json'}")
    return _scoped_path(context)


def _lock_path(path: Path) -> Path:
    """固定 64 个锁分片：锁数量有界，不必在数据清理时冒险删除活跃锁文件。"""
    stripe = int(hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:8], 16) % _LOCK_STRIPES
    return path.parent / ".locks" / f"{stripe:02d}.lock"


@contextmanager
def _cross_process_lock(path: Path):
    """线程锁 + 固定分片 flock。不同 scope 大多并行，同 scope 的读改写严格串行。"""
    with _LOCK:
        if _fcntl is None:
            yield
            return
        lock = _lock_path(path)
        lock.parent.mkdir(parents=True, exist_ok=True)
        handle = None
        try:
            handle = open(lock, "a")
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
            yield
        finally:
            if handle is not None:
                try:
                    _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
                except OSError:
                    pass
                handle.close()


def _empty() -> dict:
    return {"version": _VERSION, "pending": [], "tools": {}, "co": {},
            "transitions": {}, "recent_success": []}


def _normalize(raw) -> dict:
    """旧格式/坏字段一律冷启动，绝不把已知污染的全局 recent_queries 迁入新作用域。"""
    if not isinstance(raw, dict) or raw.get("version") != _VERSION:
        return _empty()
    data = raw
    if not isinstance(data.get("pending"), list):
        data["pending"] = []
    if not isinstance(data.get("tools"), dict):
        data["tools"] = {}
    if not isinstance(data.get("co"), dict):
        data["co"] = {}
    if not isinstance(data.get("transitions"), dict):
        data["transitions"] = {}
    if not isinstance(data.get("recent_success"), list):
        data["recent_success"] = []
    data["pending"] = [item for item in data["pending"][-_PENDING_MAX:]
                       if isinstance(item, dict)]
    data["recent_success"] = [item for item in data["recent_success"][-_RECENT_SUCCESS_MAX:]
                              if isinstance(item, dict) and item.get("id")]
    return data


def _stat(path: Path):
    try:
        stat = path.stat()
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


def _read_disk(path: Path) -> dict:
    try:
        return _normalize(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return _empty()


def _read(path: Path) -> dict:
    """进程内单槽缓存；stat 变化即重读，让其它 CLI 进程的原子写立即可见。"""
    global _CACHE_PATH, _CACHE_STAT, _CACHE_DATA
    with _LOCK:
        key = str(path)
        stat = _stat(path)
        if stat is not None and key == _CACHE_PATH and stat == _CACHE_STAT \
                and _CACHE_DATA is not None:
            return _CACHE_DATA
        data = _read_disk(path)
        _CACHE_PATH, _CACHE_STAT, _CACHE_DATA = key, stat or (), data
        return data


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _update(path: Path, mutate) -> None:
    """锁内从磁盘重读后更新；缓存从不作为并发写的事实源。"""
    global _CACHE_PATH, _CACHE_STAT, _CACHE_DATA
    path.parent.mkdir(parents=True, exist_ok=True)
    changed = False
    with _cross_process_lock(path):
        data = _read_disk(path)
        try:
            changed = bool(mutate(data))
        except Exception:
            return
        if changed:
            _atomic_write(path, data)
        _CACHE_PATH, _CACHE_STAT, _CACHE_DATA = str(path), _stat(path) or (), data
    if changed:
        _prune_if_scoped(path)


def _prune_if_scoped(path: Path) -> None:
    try:
        if os.environ.get("LEGION_TOOL_USAGE_FILE") or random.random() >= _PRUNE_SAMPLE:
            return
        context = _scope_context()
        if context is None or path != _scoped_path(context):
            return
        _prune(path.parent, _SCOPE_KEEP)
    except Exception:
        pass


def _prune(directory: Path, keep: int = _SCOPE_KEEP) -> None:
    """保留每用户最新 keep 个会话；删除前获取对应写锁并复核 mtime，避免删活跃文件。"""
    try:
        files = [item for item in directory.glob("*.json") if item.is_file()]
        if len(files) <= keep:
            return
        files.sort(key=lambda item: item.stat().st_mtime_ns, reverse=True)
        victims = files[keep:]
        cutoff = max((item.stat().st_mtime_ns for item in victims), default=0)
        for item in victims:
            with _cross_process_lock(item):
                current = _stat(item)
                if current is not None and current[0] <= cutoff:
                    try:
                        item.unlink()
                    except OSError:
                        pass
    except Exception:
        pass


def _words(query: str) -> set[str]:
    """通用字面切分（非意图词表）：ASCII 按词、CJK 按字。"""
    words: set[str] = set()
    buffer = ""
    for ch in str(query or ""):
        if ch.isascii() and (ch.isalnum() or ch in "_-./"):
            buffer += ch.lower()
        else:
            if len(buffer) >= 2:
                words.add(buffer)
            buffer = ""
            if "一" <= ch <= "鿿":
                words.add(ch)
    if len(buffer) >= 2:
        words.add(buffer)
    return {word for word in words if word not in {"the", "and", "for", "with", "of", "to", "的", "用"}}


def _term_keys(query: str) -> list[str]:
    """只落不可逆短哈希，不把用户原始 query/词项写入运行态文件。"""
    return sorted({hashlib.sha256(word.encode("utf-8")).hexdigest()[:16]
                   for word in _words(query)})[:_TERM_MAX]


def _candidate_ids(candidates) -> list[str]:
    ids = []
    seen = set()
    for item in candidates or []:
        value = item.get("id") if isinstance(item, dict) else item
        tool_id = str(value or "").strip()
        if not tool_id or len(tool_id) > 240 or tool_id in seen:
            continue
        seen.add(tool_id)
        ids.append(tool_id)
        if len(ids) >= _CANDIDATE_MAX:
            break
    return ids


def _fresh_pending(data: dict, now: int) -> list[dict]:
    floor = now - _ASSOCIATION_TTL_S
    return [item for item in data.get("pending", [])
            if isinstance(item.get("ts"), (int, float)) and item["ts"] >= floor
            and isinstance(item.get("terms"), list) and isinstance(item.get("candidates"), list)]


def record_query(query: str, candidates=None) -> None:
    """记录一次可归因检索；没有可靠 scope/词项/候选时完全 no-op。"""
    try:
        path = _path()
        terms = _term_keys(query)
        candidate_ids = _candidate_ids(candidates)
        if path is None or not terms or not candidate_ids:
            return
        now = int(time.time())

        def mutate(data: dict) -> bool:
            previous = data.get("pending", [])
            pending = _fresh_pending(data, now)
            record = {"ts": now, "terms": terms, "candidates": candidate_ids}
            if pending and pending[-1].get("terms") == terms \
                    and pending[-1].get("candidates") == candidate_ids \
                    and now - int(pending[-1].get("ts") or 0) <= 2:
                data["pending"] = pending
                return pending != previous               # 只在顺手淘汰过期记录时落盘
            record["used"] = []                         # 同一 search 可用多件候选；每件最多学习一次
            pending.append(record)
            data["pending"] = pending[-_PENDING_MAX:]
            return True

        _update(path, mutate)
    except Exception:
        pass


def record_run(tool_id: str, ok: bool) -> dict:
    """记录执行事实；返回是否命中候选，供 provenance 如实标注而不猜测。"""
    outcome = {"recorded": False, "candidate_matched": False, "query_age_s": None}
    try:
        path = _path()
        tid = str(tool_id or "").strip()
        if path is None or not tid:
            return outcome
        now = int(time.time())

        def mutate(data: dict) -> bool:
            outcome["recorded"] = True
            tools = data["tools"]
            rec = tools.setdefault(tid, {"count": 0, "ok": 0, "last": 0})
            rec["count"] = int(rec.get("count") or 0) + 1
            rec["last"] = now
            pending = _fresh_pending(data, now)
            data["pending"] = pending
            if not ok:
                return True
            rec["ok"] = int(rec.get("ok") or 0) + 1
            recent = [item for item in data.get("recent_success", [])
                      if isinstance(item.get("ts"), (int, float))
                      and item["ts"] >= now - _ASSOCIATION_TTL_S]
            previous_id = str(recent[-1].get("id") or "") if recent else ""
            if previous_id and previous_id != tid:
                transitions = data["transitions"]
                row = transitions.setdefault(previous_id, {})
                row[tid] = int(row.get(tid) or 0) + 1
                if len(row) > _PARTNER_PER_SOURCE_MAX:
                    transitions[previous_id] = dict(sorted(
                        row.items(), key=lambda pair: (-pair[1], pair[0]))[:_PARTNER_PER_SOURCE_MAX])
                if len(transitions) > _PARTNER_SOURCE_MAX:
                    ranked_sources = sorted(
                        transitions.items(), key=lambda pair: (-sum(pair[1].values()), pair[0]))
                    data["transitions"] = dict(ranked_sources[:_PARTNER_SOURCE_MAX])
            recent.append({"id": tid, "ts": now})
            data["recent_success"] = recent[-_RECENT_SUCCESS_MAX:]
            match_index = next((index for index in range(len(pending) - 1, -1, -1)
                                if tid in pending[index]["candidates"]
                                and tid not in pending[index].get("used", [])), None)
            if match_index is None:
                return True                            # 直接/重复 run 是事实，但不能猜或重复学习
            matched = pending[match_index]
            outcome["candidate_matched"] = True
            outcome["query_age_s"] = max(0, now - int(matched.get("ts") or now))
            used = matched.setdefault("used", [])
            used.append(tid)                           # 一次 search 可教多件候选，每件只教一次
            # 候选全已成功用过时，这条 search 已无剩余归因价值，及时消费掉；否则保留，允许
            # harness 在同一场景连续采用第二件不同候选（例如先抓网页、再生成 PDF）。
            if set(matched["candidates"]).issubset(used):
                pending.pop(match_index)
            data["pending"] = pending
            for term in matched["terms"]:
                row = data["co"].setdefault(term, {})
                row[tid] = int(row.get(tid) or 0) + 1
            if len(data["co"]) > _WORD_MAX:
                ranked = sorted(data["co"].items(), key=lambda pair: -sum(pair[1].values()))
                data["co"] = dict(ranked[:_WORD_MAX])
            return True

        _update(path, mutate)
    except Exception:
        pass
    return outcome


def _ranking_signals(data: dict, terms: list[str]) -> tuple[dict[str, float], dict[str, float]]:
    query_strength: dict[str, float] = {}
    for term in terms:
        row = data["co"].get(term)
        if not isinstance(row, dict) or not row:
            continue
        top = max(row.values()) or 1
        for tid, count in row.items():
            query_strength[tid] = query_strength.get(tid, 0.0) + float(count) / float(top)
    partner_strength: dict[str, float] = {}
    recent = data.get("recent_success") or []
    if recent:
        previous_id = str(recent[-1].get("id") or "")
        row = data.get("transitions", {}).get(previous_id) or {}
        top = max(row.values(), default=0) or 1
        for tid, count in row.items():
            partner_strength[tid] = float(count) / float(top)
    return query_strength, partner_strength


def boost_ranking(entries: list, query: str, id_key: str = "id",
                  score_key: str = "retrieval_score") -> list:
    """用查询成功史与最近工具伙伴优化任意武器目录的相关候选池；异常时保持原序。

    entries 必须已经由 BM25/语义/模型改写召回，因此伙伴关系只能在“本来就有相关证据”的候选
    内调整，不能凭空添加工具。单次调用每件工具最多前移两位；调用方随后裁 top-N，较弱候选自然
    退出供给，但完整 catalog 仍可按需再搜。最终选择权始终在 harness。
    """
    try:
        if not entries or len(entries) < 2:
            return entries
        path = _path()
        terms = _term_keys(query)
        if path is None:
            return entries
        data = _read(path)
        query_strength, partner_strength = _ranking_signals(data, terms)
        if not query_strength and not partner_strength:
            return entries
        query_max = max(query_strength.values(), default=0.0) or 1.0
        partner_max = max(partner_strength.values(), default=0.0) or 1.0
        weights = {}
        for item in entries:
            tid = str(item.get(id_key) or "")
            weights[tid] = (1.0
                            + (_BOOST_MAX - 1.0) * query_strength.get(tid, 0.0) / query_max
                            + (_PARTNER_BOOST_MAX - 1.0)
                            * partner_strength.get(tid, 0.0) / partner_max)
        ranked = list(entries)
        moved = {str(item.get(id_key) or ""): 0 for item in entries}
        for index in range(1, len(ranked)):
            cursor = index
            tid = str(ranked[cursor].get(id_key) or "")
            while cursor > 0 and moved.get(tid, 0) < _MAX_RANK_MOVE:
                current = ranked[cursor]
                previous = ranked[cursor - 1]
                current_score = float(current.get(score_key, 0.0) or 0.0) * weights.get(tid, 1.0)
                previous_id = str(previous.get(id_key) or "")
                previous_score = (float(previous.get(score_key, 0.0) or 0.0)
                                  * weights.get(previous_id, 1.0))
                if current_score <= previous_score:
                    break
                ranked[cursor - 1], ranked[cursor] = current, previous
                moved[tid] = moved.get(tid, 0) + 1
                cursor -= 1
        return ranked
    except Exception:
        return entries


def ranking_context(entries: list, query: str, id_key: str = "id") -> dict:
    """返回可向 harness/用户说明的统计事实，不暴露 query、分数、用户或强制选择。"""
    try:
        path = _path()
        terms = _term_keys(query)
        if path is None or not terms:
            return {"active": False, "reason": "missing_scope"}
        data = _read(path)
        query_strength, partner_strength = _ranking_signals(data, terms)
        visible = {str(item.get(id_key) or "") for item in entries}
        recent = data.get("recent_success") or []
        return {
            "active": bool(query_strength or partner_strength),
            "query_matches": sum(tid in visible for tid in query_strength),
            "partner_matches": sum(tid in visible for tid in partner_strength),
            "recent_tool": str(recent[-1].get("id") or "") if recent else "",
        }
    except Exception:
        return {"active": False, "reason": "unavailable"}


def stats() -> dict:
    """只暴露运行统计与哈希 scope，不泄露 user/session/query。"""
    try:
        context = _scope_context()
        path = _path()
        if context is None or path is None:
            return {"active": False, "reason": "missing_scope"}
        data = _read(path)
        return {"active": True,
                "lane": "override" if os.environ.get("LEGION_TOOL_USAGE_FILE") else "scoped",
                "scope_hash": context["scope_hash"],
                "scope_source": context["source"],
                "pending": len(data.get("pending", [])),
                "tools": len(data.get("tools", {})),
                "co_terms": len(data.get("co", {})),
                "transition_sources": len(data.get("transitions", {})),
                "transition_edges": sum(len(row) for row in data.get("transitions", {}).values()),
                "file": str(path)}
    except Exception:
        return {"active": False, "reason": "unavailable"}
