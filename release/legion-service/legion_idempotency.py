#!/usr/bin/env python3.12
"""在途/已完成【花钱·部署】操作的幂等账本(军团反思⑤·2026-08-28)。

真金白银的坑(军团点名):接力家重跑时,若上一家已经【付费生成图/视频】或【部署】过,接手的会
重复付费/重复部署。这里给这类"有外部副作用·重做要花钱"的操作一个【按整单(qhash)共享的账本】:
同一单里参数完全相同的操作,第二次直接返回上次结果(idempotent_hit),不再真调网关/远端。

为什么按 qhash 不按 key:接力家的 key 变了(failed_key→relay_key)但 qhash(整单)不变。按 qhash
存→接力家天然命中失败家已做过的付费操作·不重复花钱。这正是"接力续跑"要防的重复。

★ context not control:这【不是父层拦模型"不许调"】。模型/harness 完全自主决定调不调、调什么;
只是当它调了一个"这单里刚用完全相同参数做过"的花钱操作时,如实把上次结果还给它(事实),避免
真金白银重复扣费。参数变一个字(prompt 改了)=新操作=正常花钱。只对【有外部副作用】的操作生效
(生图/生视频/TTS/部署)——读类操作(识图/读PDF/搜索)不记账(重做不花钱·无副作用·不拦)。

依据:军团反思⑤"在途工具的远端 job ID/幂等键·避免接力者重复提交部署/付费生成"。
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
import time
from contextlib import contextmanager

import legion_paths as _paths

_ROOT = "idempotency"                    # ~/.meyo/legion/idempotency/<qhash>.json
_LOCK = threading.RLock()

try:
    import fcntl as _fcntl
except ImportError:              # 非 POSIX(Windows):无 fcntl·退回进程内锁(至少保线程安全)
    _fcntl = None


@contextmanager
def _cross_process_lock(qhash: str):
    """跨进程互斥读-改-写。用 <qhash>.json.lock 上 fcntl 排他锁·独立进程也会真串行。
    fcntl 不可用时退回进程内 RLock。锁文件本身不删(留着复用·内容为空·无副作用)。"""
    with _LOCK:                  # 同进程线程也要串行(fcntl 同进程重复加锁语义不保证)
        if _fcntl is None:
            yield
            return
        lock_path = _path(qhash) + ".lock"
        f = None
        try:
            f = open(lock_path, "w")
            _fcntl.flock(f.fileno(), _fcntl.LOCK_EX)
            yield
        finally:
            if f is not None:
                try:
                    _fcntl.flock(f.fileno(), _fcntl.LOCK_UN)
                except OSError:
                    pass
                f.close()

# 只对【有外部副作用·重做要花钱/改远端状态】的操作记账。读类(见图/读PDF/搜索)不记(重做无害)。
# 这是"哪些算花钱操作"的事实标注·不是拦截规则——记账后仍是把上次结果还给模型让它自己判断。
_BILLABLE_TOOLS = {"gen_image", "gen_video", "tts", "text_to_speech", "deploy"}


def _qhash_from_env() -> str:
    """从运行环境解析整单标识 qhash(接力家 key 变但 qhash 不变·账本按它共享)。
    优先 MEYO_SESSION_ID(key:qhash)·退回 MEYO_WORKDIR(.../<key>_<qhash>)·再退回 MEYO_QHASH。"""
    return _qhash_from_environment(os.environ)


def _qhash_from_environment(environment) -> str:
    """Preserve legacy key derivation without consulting process env for bound callers."""
    sid = environment.get("MEYO_SESSION_ID", "")
    if ":" in sid:
        return sid.split(":", 1)[1].strip()
    wd = environment.get("MEYO_WORKDIR", "").rstrip("/")
    if wd:
        base = os.path.basename(wd)          # <key>_<qhash>
        if "_" in base:
            return base.split("_", 1)[1]
    return environment.get("MEYO_QHASH", "").strip()


def is_billable(tool_id: str) -> bool:
    return str(tool_id or "").split(":")[-1] in _BILLABLE_TOOLS


def _key(tool_id: str, params: dict) -> str:
    """操作指纹 = 工具名 + 规范化参数 hash。参数完全相同才算"同一个操作"(改一字=新操作)。"""
    norm = json.dumps(params or {}, ensure_ascii=False, sort_keys=True)
    h = hashlib.sha256(f"{tool_id}\x1e{norm}".encode("utf-8")).hexdigest()[:24]
    return f"{str(tool_id).split(':')[-1]}:{h}"


class BoundCacheError(RuntimeError):
    """A legacy cache is not a miss when its contents or readability are unknown."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _read_bound_cache(path: Path, root: Path) -> dict | None:
    """Read an atomic old snapshot without mkdir, lock files, or global path helpers."""
    parent = None
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        parent = os.open(path.anchor, flags)
        for index, part in enumerate(path.parts[1:-1], start=1):
            child = os.open(part, flags, dir_fd=parent)
            os.close(parent)
            parent = child
            # Ancestors may be system-owned. The explicit state root and its
            # idempotency directory must belong to the current OS user.
            if index >= len(root.parts) - 1:
                info = os.fstat(parent)
                if info.st_uid != os.getuid() or info.st_mode & 0o022:
                    raise BoundCacheError("legacy_cache_unsafe_path")
                if info.st_mode & 0o500 != 0o500:
                    raise BoundCacheError("legacy_cache_unreadable")
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=parent)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.getuid() or info.st_mode & 0o022):
                raise BoundCacheError("legacy_cache_unsafe_path")
            if not info.st_mode & 0o400:
                raise BoundCacheError("legacy_cache_unreadable")
            data = json.load(stream)
        if not isinstance(data, dict) or any(
                not isinstance(row, dict) or not isinstance(row.get("result"), dict)
                or not row["result"].get("ok") for row in data.values()):
            raise BoundCacheError("legacy_cache_corrupt")
        return data
    except FileNotFoundError:
        return None
    except OSError as exc:
        code = ("legacy_cache_unsafe_path"
                if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EISDIR}
                else "legacy_cache_unreadable")
        raise BoundCacheError(code) from None
    except (ValueError, TypeError, RecursionError):
        raise BoundCacheError("legacy_cache_corrupt") from None
    finally:
        if parent is not None:
            os.close(parent)


def check_bound(tool_id: str, params: dict, *, environment) -> dict | None:
    """Pure compatibility read, using only a trusted per-call environment.

    MEYO_LEGION_DIR is the existing data root. MEYO_PERSONAL_STATE_DIR is the
    instance root exported by personal_runtime.environment; its legacy data
    is under data/. LEGION_PERSONAL_STATE_DIR remains a lower-priority
    compatibility alias only. No HOME/process-env fallback.
    Pass original business params, before adding a generated output filename.
    This snapshot read does not reserve an operation against old writers.
    """
    if not is_billable(tool_id):
        return None
    supplied = environment.get("MEYO_LEGION_DIR")
    instance = False
    if not supplied:
        supplied = (environment.get("MEYO_PERSONAL_STATE_DIR")
                    or environment.get("LEGION_PERSONAL_STATE_DIR"))
        instance = True
    if not supplied:
        return None
    if not isinstance(supplied, str):
        raise BoundCacheError("legacy_cache_unsafe_path")
    root = Path(supplied)
    if (not root.is_absolute() or ".." in root.parts or "\0" in supplied
            or root == Path(root.anchor)):
        raise BoundCacheError("legacy_cache_unsafe_path")
    qhash = _qhash_from_environment(environment)
    if not qhash:
        return None
    data_root = root / "data" if instance else root
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in qhash)[:80]
    data = _read_bound_cache(data_root / _ROOT / (safe + ".json"), root)
    if data is None:
        return None
    record = data.get(_key(tool_id, params))
    if record is None:
        return None
    out = dict(record["result"])
    out["idempotent_hit"] = True
    out["idempotent_note"] = (f"这单里已用完全相同参数做过 {str(tool_id).split(':')[-1]}"
                              "(省去重复付费/部署)·只读复用旧结果。")
    return out


def _path(qhash: str) -> str:
    d = _paths.under(_ROOT)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(qhash or "default"))[:80]
    return os.path.join(d, f"{safe}.json")


def _load(qhash: str) -> dict:
    try:
        with open(_path(qhash), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save(qhash: str, ledger: dict) -> None:
    try:
        p = _path(qhash)
        tmp = f"{p}.tmp.{os.getpid()}.{threading.get_ident()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(ledger, f, ensure_ascii=False)
        os.replace(tmp, p)
    except (OSError, TypeError, ValueError):
        pass


def check(tool_id: str, params: dict, qhash: str = "") -> dict | None:
    """查这单里有没有【完全相同的花钱操作】已完成。命中返回上次结果(带 idempotent_hit)·否则 None。
    非花钱操作恒返 None(不记账·不拦)。qhash 空则从 env 解析。绝不抛。"""
    if not is_billable(tool_id):
        return None
    qh = qhash or _qhash_from_env()
    if not qh:
        return None
    try:
        with _LOCK:
            rec = _load(qh).get(_key(tool_id, params))
        if rec and isinstance(rec.get("result"), dict):
            out = dict(rec["result"])
            out["idempotent_hit"] = True
            out["idempotent_note"] = (f"这单里已用完全相同参数做过 {str(tool_id).split(':')[-1]}"
                                      f"(省去重复付费/部署)·直接返回上次结果·如需重做请改参数或显式强制。")
            return out
        return None
    except Exception:
        return None


def record(tool_id: str, params: dict, result: dict, qhash: str = "") -> None:
    """记一次【成功的花钱操作】。只记成功(失败不记·下次可重试)。非花钱操作不记。绝不抛。

    ★ 跨进程原子:接力家/并行家是【独立 CLI 进程】共享同一 qhash 账本文件(见文件头+media_tools.py:508)。
    进程内 RLock 对它们无效——两个进程会各自 _load 到相同旧账本、各加各的记录、后 _save 的覆盖先写的
    (丢更新→重复付费,正是本模块要防的事)。故用 fcntl 文件锁把"读-改-写"锁到文件级别(跨进程串行);
    锁不可用时(非 POSIX)退回进程内 RLock,至少保住同进程线程安全。"""
    if not is_billable(tool_id) or not isinstance(result, dict) or not result.get("ok"):
        return
    qh = qhash or _qhash_from_env()
    if not qh:
        return
    entry = {
        "tool": str(tool_id).split(":")[-1], "ts": time.time(),
        # 只留可复用的结果字段(路径/url/job_id)·不存大 blob
        "result": {k: v for k, v in result.items()
                   if k in ("ok", "url", "path", "text", "answer", "job_id", "remote_id")},
    }
    ek = _key(tool_id, params)
    try:
        with _cross_process_lock(qh):
            ledger = _load(qh)      # 持锁后重新读盘·拿到别的进程已写入的最新账本
            ledger[ek] = entry
            _save(qh, ledger)
    except Exception:
        pass


def clear(qhash: str) -> None:
    """整单结束清账本(可选)。绝不抛。"""
    try:
        p = _path(qhash)
        if os.path.isfile(p):
            os.remove(p)
    except OSError:
        pass
