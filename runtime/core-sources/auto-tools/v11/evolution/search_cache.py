"""v13.87 · 搜索/抓取本地内容缓存 · 抄 wigolo 亮点 5
从 wigolo 那学的：抓完存 sqlite · 二次抓命中 cache 0 网络 · 治 v13.79 138 条脏 URL 根子

主要 API:
- cache_get(url, max_age_s=86400)  → dict|None (返 hit 的 content/hash/completeness)
- cache_put(url, content, meta)    → hash (落盘 · 自动去重)
- cache_stats()                    → dict (总数 / 命中率 / 脏 URL 数)
- cache_reject(url, reason)        → 标脏·比黑名单先进的地方是记内容层脏因

字段:
  url TEXT PRIMARY KEY
  content_hash TEXT     -- sha256[:16] · 内容层去重
  content TEXT          -- 全文 (最大 200KB · 超则 head 200KB)
  content_len INT
  completeness TEXT     -- shell|partial|full  (抄亮点 1)
  fetched_at INT        -- unix ts
  fetcher TEXT          -- xhs_api|sogou_weixin|firecrawl|read_url|smart_fetch
  meta TEXT             -- json
  rejected INT DEFAULT 0
  reject_reason TEXT

不做:
- LLM 索引 (等 embedding 需求真出现再上 sqlite-vec)
- 多 URL 别名合并 (下版)
"""

import hashlib
import json
import os
import sqlite3
import time
from typing import Optional


# v13.88.79 · 静默监控统一接入 · 用户 07-28「所有静默监控都到齐了吗」
def _alert(severity: str, msg: str):
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
        from alert_bus import notify as _n
        _n(_o.path.basename(__file__).replace(".py",""), severity, msg)
    except Exception:
        pass


V11 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DB = os.path.join(V11, ".search_cache.sqlite")

_INIT_SQL = """
CREATE TABLE IF NOT EXISTS pages (
    url TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    content TEXT NOT NULL,
    content_len INTEGER NOT NULL,
    completeness TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    fetcher TEXT NOT NULL,
    meta TEXT DEFAULT '{}',
    rejected INTEGER DEFAULT 0,
    reject_reason TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_hash ON pages(content_hash);
CREATE INDEX IF NOT EXISTS idx_fetched ON pages(fetched_at);
CREATE INDEX IF NOT EXISTS idx_rejected ON pages(rejected);
"""

_CONTENT_MAX = 200 * 1024   # 单页最多留 200KB
_DEFAULT_TTL = 86400         # 24h


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(CACHE_DB, timeout=5)
    c.executescript(_INIT_SQL)
    return c


def score_completeness(content: str) -> str:
    """亮点 1 · 三挡: shell(<200) partial(200-1000) full(>1000)
    规则来自 wigolo doctor 观察 · 阈值贴中文/英文正文长度
    """
    n = len(content or "")
    if n < 200:
        return "shell"
    if n < 1000:
        return "partial"
    return "full"


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()[:16]


def cache_get(url: str, max_age_s: int = _DEFAULT_TTL) -> Optional[dict]:
    """二次抓命中返 dict · 未命中返 None
    - rejected=1 的 URL 直返 None (相当于黑名单)
    - 超 TTL 返 None (让上游重抓)
    """
    if not url:
        return None
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT content_hash, content, content_len, completeness, "
                "fetched_at, fetcher, meta, rejected, reject_reason "
                "FROM pages WHERE url=?", (url,)
            ).fetchone()
        if not row:
            return None
        if row[7]:  # rejected
            return None
        if int(time.time()) - int(row[4]) > max_age_s:
            return None
        return {
            "url": url, "content_hash": row[0], "content": row[1],
            "content_len": row[2], "completeness": row[3],
            "fetched_at": row[4], "fetcher": row[5],
            "meta": json.loads(row[6] or "{}"),
            "cached": True,
        }
    except Exception:
        return None


def cache_put(url: str, content: str, fetcher: str,
              meta: Optional[dict] = None) -> str:
    """落盘 · 自动打 completeness 分 · 返内容 hash
    - content 空/None 不落盘 (避免污染)
    - 超 200KB 截断
    - 二次 put 同 URL: 内容相同 skip · 内容变了覆盖
    """
    if not url or not content:
        return ""
    content = content[:_CONTENT_MAX]
    h = _hash(content)
    completeness = score_completeness(content)
    now = int(time.time())
    try:
        with _conn() as c:
            # 先查旧 hash · 完全相同就不写(省 IO)
            old = c.execute(
                "SELECT content_hash FROM pages WHERE url=?", (url,)
            ).fetchone()
            if old and old[0] == h:
                return h
            c.execute(
                "INSERT OR REPLACE INTO pages "
                "(url, content_hash, content, content_len, completeness, "
                " fetched_at, fetcher, meta) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (url, h, content, len(content), completeness,
                 now, fetcher, json.dumps(meta or {}, ensure_ascii=False))
            )
        return h
    except Exception:
        return ""


def cache_reject(url: str, reason: str = "") -> bool:
    """标脏 · 之后 cache_get 会返 None · 治 v13.79 138 条脏 URL 根子
    - URL 不在库: 建条空占位 rejected=1
    - 在库: 覆盖 rejected=1 + reason
    """
    if not url:
        return False
    now = int(time.time())
    try:
        with _conn() as c:
            exists = c.execute(
                "SELECT 1 FROM pages WHERE url=?", (url,)
            ).fetchone()
            if exists:
                c.execute(
                    "UPDATE pages SET rejected=1, reject_reason=? WHERE url=?",
                    (reason[:200], url)
                )
            else:
                c.execute(
                    "INSERT INTO pages (url, content_hash, content, "
                    "content_len, completeness, fetched_at, fetcher, "
                    "meta, rejected, reject_reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                    (url, "", "", 0, "shell", now, "reject",
                     "{}", reason[:200])
                )
        return True
    except Exception:
        return False


def cache_stats() -> dict:
    """总条目/命中(24h 内 rejected=0)/脏/按 completeness 分档"""
    try:
        with _conn() as c:
            total = c.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
            rejected = c.execute(
                "SELECT COUNT(*) FROM pages WHERE rejected=1"
            ).fetchone()[0]
            fresh_cutoff = int(time.time()) - _DEFAULT_TTL
            fresh = c.execute(
                "SELECT COUNT(*) FROM pages WHERE rejected=0 AND fetched_at>=?",
                (fresh_cutoff,)
            ).fetchone()[0]
            by_comp = dict(c.execute(
                "SELECT completeness, COUNT(*) FROM pages "
                "WHERE rejected=0 GROUP BY completeness"
            ).fetchall())
            by_fetcher = dict(c.execute(
                "SELECT fetcher, COUNT(*) FROM pages "
                "WHERE rejected=0 GROUP BY fetcher ORDER BY 2 DESC LIMIT 10"
            ).fetchall())
        return {
            "total": total, "rejected": rejected, "fresh_24h": fresh,
            "by_completeness": by_comp, "by_fetcher": by_fetcher,
            "db_path": CACHE_DB,
        }
    except Exception as e:
        return {"err": str(e)}


if __name__ == "__main__":
    try:
        import sys
        if len(sys.argv) > 1 and sys.argv[1] == "stats":
            print(json.dumps(cache_stats(), ensure_ascii=False, indent=2))
        else:
            # smoke test
            print("[smoke] put/get roundtrip")
            h = cache_put("https://test.local/a", "hello world " * 100,
                          "smoke_test", {"foo": "bar"})
            print(f"  put hash={h}")
            r = cache_get("https://test.local/a")
            print(f"  get len={r['content_len']} comp={r['completeness']} "
                  f"cached={r['cached']}")
            cache_reject("https://test.local/a", "smoke_reject")
            r2 = cache_get("https://test.local/a")
            print(f"  after reject: {r2}")
            print(json.dumps(cache_stats(), ensure_ascii=False, indent=2))
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise
