"""resilient_fetch.py · 2026-08-20 · 通用「韧性抓取」原语库

把 search_fabric.py 里已验证有效的三样东西抽成不绑定 search 的公共原语,
再补上一个此前缺失的关键件——**全局(跨进程)令牌桶限流器**:

  1. is_transient_error / TRANSIENT_MARKERS
       —— 判断错误串是否属于「临时/可重试」通道故障(429/限流/maxlag/超时)
  2. cache_get / cache_set (redis 优先·文件兜底·带 namespace)
       —— 只缓存成功非空;不同调用方(search/lxml/llm)用 namespace 隔离
  3. with_retry(fn, is_transient_fn, ...)
       —— 通用指数退避重试;是否可重试由调用方给的判据函数决定
  4. RateLimiter (【本次新增】redis 令牌桶·连不上→fcntl.flock+json 文件兜底)
       —— 治「多进程/多用户各刷各的·把源限流(按 IP)刷爆」

设计:
  * 模块可独立 import·零副作用(懒连 redis·懒建目录)
  * 降级(redis 不可用)全部可被察觉(backend 返回 "file"/last_backend)
  * 纯 stdlib + 可选 redis;不依赖 search_fabric
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

try:
    import fcntl  # POSIX only
except Exception:  # pragma: no cover - Windows 兜底
    fcntl = None  # type: ignore


# ══════════════════════════════════════════════════════════════════════════════
# 0. 路径 / 常量
# ══════════════════════════════════════════════════════════════════════════════
_HERE = Path(__file__).resolve().parent

# 临时故障(可重试类)信号词·大小写不敏感命中即视为通道故障而非「真没结果」
TRANSIENT_MARKERS = ("429", "too many requests", "maxlag", "timeout", "timed out")

# 默认退避配置: 最多 3 次尝试(初次 + 2 次重试)·等 1s、2s
DEFAULT_BACKOFFS = (1.0, 2.0)

# 默认缓存 TTL·10 分钟
DEFAULT_CACHE_TTL_S = 600

# 通用缓存根目录(按 namespace 再分子目录)
CACHE_ROOT = _HERE / ".resilient_cache"

# 限流状态文件根目录(文件兜底时用)
RATE_ROOT = _HERE / ".resilient_ratelimit"


# ══════════════════════════════════════════════════════════════════════════════
# 1. transient 判定
# ══════════════════════════════════════════════════════════════════════════════
def is_transient_error(err_str: Any) -> bool:
    """判断一个错误字符串是否属于「临时/可重试」通道故障(429/限流/maxlag/超时)"""
    if not err_str:
        return False
    low = str(err_str).lower()
    return any(m in low for m in TRANSIENT_MARKERS)


# ══════════════════════════════════════════════════════════════════════════════
# 2. redis 单例(懒连·连不上→None→自动降级)
# ══════════════════════════════════════════════════════════════════════════════
_REDIS_CLIENT = None
_REDIS_CHECKED = False
_REDIS_LOCK = threading.Lock()


def get_redis():
    """懒连 redis·连不上返回 None(触发文件降级)。任何异常都吞成 None。"""
    global _REDIS_CLIENT, _REDIS_CHECKED
    if _REDIS_CHECKED:
        return _REDIS_CLIENT
    with _REDIS_LOCK:
        if _REDIS_CHECKED:
            return _REDIS_CLIENT
        try:
            import redis  # type: ignore
            r = redis.Redis(socket_connect_timeout=0.5, socket_timeout=0.5)
            r.ping()
            _REDIS_CLIENT = r
        except Exception:
            _REDIS_CLIENT = None
        _REDIS_CHECKED = True
    return _REDIS_CLIENT


def _reset_redis_cache():
    """测试用: 清掉 redis 单例缓存·下次 get_redis 重新探测。"""
    global _REDIS_CLIENT, _REDIS_CHECKED
    with _REDIS_LOCK:
        _REDIS_CLIENT = None
        _REDIS_CHECKED = False


# ══════════════════════════════════════════════════════════════════════════════
# 3. 查询缓存(redis 优先·文件兜底·namespace 隔离)
# ══════════════════════════════════════════════════════════════════════════════
def _key_hash(namespace: str, key_parts: Sequence[Any]) -> str:
    raw = namespace + "|" + "|".join(str(p) for p in key_parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _redis_key(namespace: str, khash: str) -> str:
    return f"rf_cache:{namespace}:{khash}"


def _cache_dir(namespace: str, cache_root: Optional[Path] = None) -> Path:
    root = cache_root if cache_root is not None else CACHE_ROOT
    return Path(root) / namespace


def cache_get(namespace: str, key_parts: Sequence[Any],
              ttl: int = DEFAULT_CACHE_TTL_S,
              cache_root: Optional[Path] = None,
              redis_client: Any = "__auto__") -> Optional[tuple]:
    """查缓存。返回 (value, backend_str) 或 None。backend ∈ {"redis","file"}。

    namespace 用于区分不同调用方(search/lxml/llm)·文件目录与 redis key 都带前缀。
    """
    khash = _key_hash(namespace, key_parts)
    r = get_redis() if redis_client == "__auto__" else redis_client
    if r is not None:
        try:
            raw = r.get(_redis_key(namespace, khash))
            if raw:
                data = json.loads(raw)
                return data, "redis"
        except Exception:
            pass  # redis 读失败→落文件兜底
    # 文件兜底(mtime TTL)
    try:
        fp = _cache_dir(namespace, cache_root) / (khash + ".json")
        if fp.exists():
            age = time.time() - fp.stat().st_mtime
            if age <= ttl:
                data = json.loads(fp.read_text(encoding="utf-8"))
                return data, "file"
    except Exception:
        pass
    return None


def cache_set(namespace: str, key_parts: Sequence[Any], value: Any,
              ttl: int = DEFAULT_CACHE_TTL_S,
              cache_root: Optional[Path] = None,
              redis_client: Any = "__auto__") -> str:
    """写缓存(只应在成功非空结果时调用)。返回实际写入的 backend 字符串。"""
    khash = _key_hash(namespace, key_parts)
    payload = json.dumps(value, ensure_ascii=False)
    r = get_redis() if redis_client == "__auto__" else redis_client
    if r is not None:
        try:
            r.set(_redis_key(namespace, khash), payload, ex=ttl)
            return "redis"
        except Exception:
            pass  # redis 写失败→降级文件
    try:
        d = _cache_dir(namespace, cache_root)
        d.mkdir(parents=True, exist_ok=True)
        (d / (khash + ".json")).write_text(payload, encoding="utf-8")
        return "file"
    except Exception:
        return "none"


# ══════════════════════════════════════════════════════════════════════════════
# 4. 通用退避重试
# ══════════════════════════════════════════════════════════════════════════════
def with_retry(fn: Callable[[], Any], *,
               is_transient_fn: Callable[[Any], bool],
               max_retries: int = 2,
               backoffs: Sequence[float] = DEFAULT_BACKOFFS,
               on_retry: Optional[Callable[[int, float, Any], None]] = None,
               sleep_fn: Callable[[float], None] = time.sleep) -> Any:
    """通用指数退避重试。

    fn()          : 无参可调用·返回一个结果(由调用方 is_transient_fn 判断可否重试)
    is_transient_fn(result) -> bool : True 表示结果是临时故障·可重试
    max_retries   : 重试次数(总尝试 = 1 + max_retries)
    backoffs      : 每次重试前等待秒数(不足则复用最后一个)
    on_retry(attempt_idx, wait_s, last_result) : 每次重试前回调(打 trace 用)

    返回最后一次 fn() 的结果(成功结果·或重试耗尽仍 transient 的结果)。
    注意: fn 自身抛异常不在此拦截(调用方决定异常语义),原样上抛。
    """
    attempts = 1 + max(0, max_retries)
    result: Any = None
    for i in range(attempts):
        result = fn()
        if not is_transient_fn(result):
            return result  # 成功 or 非 transient → 立即返回
        if i < attempts - 1:  # 还有重试机会
            wait = backoffs[i] if i < len(backoffs) else (backoffs[-1] if backoffs else 0.0)
            if on_retry is not None:
                try:
                    on_retry(i + 1, wait, result)
                except Exception:
                    pass
            if wait > 0:
                sleep_fn(wait)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# 5. 【新增】全局令牌桶限流器(跨进程)
# ══════════════════════════════════════════════════════════════════════════════
class RateLimiter:
    """按 source 维度的全局(跨进程)限流器·治「多进程各刷各的把源按 IP 刷爆」。

    实现:
      * redis 优先: per-second 固定窗口计数(INCR + 首次 EXPIRE)·超 rate 拒绝
      * redis 不可用→ fcntl.flock + json 文件·单机多进程安全·同样固定窗口计数

    用法:
        rl = RateLimiter()
        if rl.acquire("wikipedia", rate_per_sec=1):
            ... do network call ...
        # 阻塞式:
        rl.acquire("wikipedia", rate_per_sec=1, block=True)
        # 上下文:
        with rl.rate_limited("wikipedia", rate_per_sec=1) as g:
            if g.acquired: ...

    降级可察觉: last_backend 属性记录本次判定走的后端("redis"/"file")。

    速率语义(rate_per_sec >= 1): 每个 1 秒固定窗口内最多允许 int(rate_per_sec) 次。
    速率 < 1: 用 ceil(1/rate) 秒粗窗口·每粗窗口最多 1 次(遵守官方串行礼仪)。
    """

    def __init__(self, rate_root: Optional[Path] = None,
                 default_rate_per_sec: float = 1.0,
                 redis_client: Any = "__auto__",
                 now_fn: Callable[[], float] = time.time):
        self._rate_root = Path(rate_root) if rate_root is not None else RATE_ROOT
        self._default_rate = float(default_rate_per_sec)
        self._redis_client = redis_client
        self._now = now_fn  # 可注入时钟(测试固定窗口·避免跨秒边界抖动)
        self.last_backend: Optional[str] = None
        self._proc_lock = threading.Lock()

    def _redis(self):
        return get_redis() if self._redis_client == "__auto__" else self._redis_client

    # ── 单次(非阻塞)尝试获取一个令牌 ─────────────────────────────────────
    def _try_acquire_once(self, source: str, rate_per_sec: float) -> bool:
        r = self._redis()
        if r is not None:
            try:
                return self._redis_try_acquire(r, source, rate_per_sec)
            except Exception:
                pass  # redis 失败→文件兜底
        self.last_backend = "file"
        return self._file_try_acquire(source, rate_per_sec)

    def _redis_try_acquire(self, r, source: str, rate_per_sec: float) -> bool:
        if rate_per_sec >= 1:
            window = int(self._now())
            rkey = f"rf_rate:{source}:{window}"
            limit = max(1, int(rate_per_sec))
            expire = 2
        else:
            span = max(1, int(round(1.0 / rate_per_sec)))
            window = int(self._now()) // span
            rkey = f"rf_rate:{source}:c{span}:{window}"
            limit = 1
            expire = span + 1
        cur = r.incr(rkey)
        if cur == 1:
            try:
                r.expire(rkey, expire)
            except Exception:
                pass
        self.last_backend = "redis"
        return cur <= limit

    def _file_try_acquire(self, source: str, rate_per_sec: float) -> bool:
        """fcntl.flock + json·单机多进程安全的固定窗口计数。"""
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in source)
        if rate_per_sec >= 1:
            window = int(self._now())
            limit = max(1, int(rate_per_sec))
        else:
            span = max(1, int(round(1.0 / rate_per_sec)))
            window = int(self._now()) // span
            limit = 1
        with self._proc_lock:  # 同进程多线程也要串行
            try:
                self._rate_root.mkdir(parents=True, exist_ok=True)
                fp = self._rate_root / (safe + ".json")
                fd = os.open(str(fp), os.O_RDWR | os.O_CREAT, 0o644)
                try:
                    if fcntl is not None:
                        fcntl.flock(fd, fcntl.LOCK_EX)
                    raw = os.read(fd, 65536)
                    try:
                        state = json.loads(raw.decode("utf-8")) if raw else {}
                    except Exception:
                        state = {}
                    if state.get("window") != window:
                        count = 0
                    else:
                        count = int(state.get("count", 0))
                    allowed = count < limit
                    if allowed:
                        count += 1
                    new_state = {"window": window, "count": count}
                    os.lseek(fd, 0, os.SEEK_SET)
                    os.ftruncate(fd, 0)
                    os.write(fd, json.dumps(new_state).encode("utf-8"))
                    return allowed
                finally:
                    if fcntl is not None:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_UN)
                        except Exception:
                            pass
                    os.close(fd)
            except Exception:
                # 兜底出错时保守放行(不因限流器自身故障阻断主流程)
                self.last_backend = "file_error"
                return True

    def acquire(self, source: str, rate_per_sec: Optional[float] = None,
                block: bool = False, max_wait_s: float = 5.0,
                sleep_fn: Callable[[float], None] = time.sleep) -> bool:
        """尝试获取一个令牌。

        block=False: 立即返回 True(拿到)/False(超速被拒)
        block=True : 阻塞轮询直到拿到或超 max_wait_s·超时返回 False
        """
        rate = self._default_rate if rate_per_sec is None else float(rate_per_sec)
        if rate <= 0:
            self.last_backend = "disabled"
            return True  # rate<=0 视为不限流
        if not block:
            return self._try_acquire_once(source, rate)
        deadline = self._now() + max_wait_s
        while True:
            if self._try_acquire_once(source, rate):
                return True
            if self._now() >= deadline:
                return False
            now = self._now()
            sleep_fn(min(0.2, max(0.0, (int(now) + 1) - now)))

    def rate_limited(self, source: str, rate_per_sec: Optional[float] = None,
                     block: bool = True, max_wait_s: float = 5.0):
        """上下文管理器: 进入前获取令牌·结果记在 .acquired。"""
        limiter = self

        class _Ctx:
            def __init__(self):
                self.acquired = False

            def __enter__(self_inner):
                self_inner.acquired = limiter.acquire(
                    source, rate_per_sec, block=block, max_wait_s=max_wait_s)
                return self_inner

            def __exit__(self_inner, *exc):
                return False

        return _Ctx()


# 进程级默认单例(供热路径共享·避免每次 new)
_DEFAULT_LIMITER: Optional[RateLimiter] = None
_DEFAULT_LIMITER_LOCK = threading.Lock()


def get_default_limiter() -> RateLimiter:
    global _DEFAULT_LIMITER
    if _DEFAULT_LIMITER is None:
        with _DEFAULT_LIMITER_LOCK:
            if _DEFAULT_LIMITER is None:
                _DEFAULT_LIMITER = RateLimiter()
    return _DEFAULT_LIMITER
