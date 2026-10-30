#!/usr/bin/env python3
"""
v13.63 · site_bookmark · 人类式站点收藏库
────────────────────────────────────────────────────────────────
用户 2026-07-13 元规则:「深挖 google 找到啥的再去深度分析,然后记住这个站点或
收藏,然后再找这个站点,带有规划和目的的去分析站内内容」

── 数据模型 (sqlite v11/.bookmarks.db) ──
  sites  domain PK · first_seen · hits · avg_len · avg_time_s · best_backend
         topics(json list) · rating (0-100) · notes · last_visited
  visits id PK    · domain · url · ts · via · ok · len · time_s
         topic · extracted_rules(json)

── 使用 ──
  record(url, via, ok, len, time_s, topic, extracted_rules)
  rate(domain, delta)          · 好评 +5 差评 -5
  top(n, topic=None)           · 按 rating 排 · 可按 topic 过滤
  need_revisit(hours=24)       · 该复访的站点(超 24h 没访+rating≥50)
"""
import os, sys, json, sqlite3, time
from datetime import datetime, timedelta
from urllib.parse import urlparse


# v13.88.79 · 静默监控统一接入 · 用户 07-28「所有静默监控都到齐了吗」
def _alert(severity: str, msg: str):
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
        from alert_bus import notify as _n
        _n(_o.path.basename(__file__).replace(".py",""), severity, msg)
    except Exception:
        pass


BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(BASE, "v11", ".bookmarks.db")


def _conn():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS sites(
        domain TEXT PRIMARY KEY, first_seen TEXT, hits INT DEFAULT 0,
        avg_len REAL DEFAULT 0, avg_time_s REAL DEFAULT 0,
        best_backend TEXT, topics TEXT DEFAULT '[]',
        rating INT DEFAULT 50, notes TEXT DEFAULT '',
        last_visited TEXT)""")
    # v13.87.25 · 用户 2026-07-23 抓「site_bookmark 加 fetch_strategy」
    # sqlite ALTER 一次性加字段 · 存在就跳
    for col_ddl in [
        "fetch_strategy TEXT DEFAULT ''",  # json {primary, fallback, depth_mode, anti_bot_tier}
        "primary_backend TEXT DEFAULT ''",  # 冗余·快查
        "fallback_backend TEXT DEFAULT ''",
        "depth_mode TEXT DEFAULT ''",  # fast|balanced|deep
        "anti_bot_tier INT DEFAULT 0",  # 0=直连·1=中·2=强·3=极强
        # v13.87.33 · 用户 2026-07-23 抓「所有站点都做 tag 权重」
        "tags TEXT DEFAULT '{}'",  # json {topic1: score1, topic2: score2} · 每次搜到累加
    ]:
        try:
            col_name = col_ddl.split()[0]
            c.execute(f"ALTER TABLE sites ADD COLUMN {col_ddl}")
        except sqlite3.OperationalError:
            pass  # 已存在
    c.execute("""CREATE TABLE IF NOT EXISTS visits(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        domain TEXT, url TEXT, ts TEXT, via TEXT,
        ok INT, len INT, time_s REAL, topic TEXT,
        extracted_rules TEXT DEFAULT '[]')""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_visits_domain ON visits(domain)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_visits_ts ON visits(ts)")
    return c


# v13.87.25 · fetch_strategy 学习规则 · 从 visits 累计推断
_KNOWN_STRATEGIES = {
    # 明确规则 · 从 memory 沉淀
    "csdn.net": {"primary": "webclaw", "fallback": "stealth",
                  "depth_mode": "balanced", "anti_bot_tier": 2},
    "blog.csdn.net": {"primary": "webclaw", "fallback": "stealth",
                       "depth_mode": "balanced", "anti_bot_tier": 2},
    "zhihu.com": {"primary": "friday_zh", "fallback": "stealth",
                   "depth_mode": "deep", "anti_bot_tier": 3},
    "www.zhihu.com": {"primary": "friday_zh", "fallback": "stealth",
                       "depth_mode": "deep", "anti_bot_tier": 3},
    "zhuanlan.zhihu.com": {"primary": "friday_zh", "fallback": "curl",
                            "depth_mode": "balanced", "anti_bot_tier": 2},
    "xiaohongshu.com": {"primary": "xhs_api", "fallback": "cdp",
                         "depth_mode": "deep", "anti_bot_tier": 3},
    "www.xiaohongshu.com": {"primary": "xhs_api", "fallback": "cdp",
                             "depth_mode": "deep", "anti_bot_tier": 3},
    "sspai.com": {"primary": "curl", "fallback": "friday_bing",
                   "depth_mode": "fast", "anti_bot_tier": 0},
    "github.com": {"primary": "curl", "fallback": "friday_bing",
                    "depth_mode": "fast", "anti_bot_tier": 0},
    "arxiv.org": {"primary": "friday_bing", "fallback": "curl",
                   "depth_mode": "deep", "anti_bot_tier": 0},
    "mp.weixin.qq.com": {"primary": "sogou_weixin", "fallback": "cdp",
                          "depth_mode": "deep", "anti_bot_tier": 3},
    "news.ycombinator.com": {"primary": "hn_algolia_api",
                               "fallback": "curl",
                               "depth_mode": "fast", "anti_bot_tier": 0},
    "www.woshipm.com": {"primary": "curl", "fallback": "webclaw",
                         "depth_mode": "balanced", "anti_bot_tier": 1},
    "juejin.cn": {"primary": "curl", "fallback": "webclaw",
                   "depth_mode": "balanced", "anti_bot_tier": 1},
    "reddit.com": {"primary": "cdp", "fallback": "friday_google",
                    "depth_mode": "deep", "anti_bot_tier": 2},
}


def _infer_strategy_from_stats(hits: int, avg_len: float,
                                 best_backend: str = "") -> dict:
    """从 visits 累计推断 · 无手动规则时用"""
    if hits < 3:
        return {}  # 数据不够
    # 简单启发式
    tier = 0
    if best_backend in ("stealth", "firecrawl", "cdp"):
        tier = 3
    elif best_backend in ("webclaw", "browseract"):
        tier = 2
    elif avg_len < 500:
        tier = 2  # 内容太短·可能反爬
    depth = "fast" if avg_len < 1000 else ("balanced" if avg_len < 5000 else "deep")
    return {
        "primary": best_backend or "curl",
        "fallback": "friday_bing" if tier < 2 else "stealth",
        "depth_mode": depth,
        "anti_bot_tier": tier,
    }


def get_strategy(domain: str) -> dict:
    """给 domain · 返 fetch strategy dict
    优先: DB 已存字段 → 手动 _KNOWN_STRATEGIES → 从 visits 推断 → {}
    """
    if not domain:
        return {}
    domain = domain.lower().strip()
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT primary_backend, fallback_backend, depth_mode, "
                "anti_bot_tier, hits, avg_len, best_backend, "
                "fetch_strategy FROM sites WHERE domain=?",
                (domain,)
            ).fetchone()
        if row and row[0]:
            return {
                "primary": row[0], "fallback": row[1] or "",
                "depth_mode": row[2] or "balanced",
                "anti_bot_tier": row[3] or 0,
                "source": "db_stored",
            }
        # 手动规则
        if domain in _KNOWN_STRATEGIES:
            s = dict(_KNOWN_STRATEGIES[domain])
            s["source"] = "known_rules"
            return s
        # 推断
        if row:
            hits, avg_len, best_backend = row[4], row[5], row[6]
            inferred = _infer_strategy_from_stats(hits, avg_len,
                                                    best_backend or "")
            if inferred:
                inferred["source"] = "inferred"
                return inferred
    except Exception:
        pass
    return {}


def learn_strategy(domain: str, primary: str, fallback: str = "",
                    depth_mode: str = "balanced",
                    anti_bot_tier: int = 0) -> bool:
    """把学到的 strategy 存回 db (让下次直接用)"""
    if not domain or not primary:
        return False
    try:
        with _conn() as c:
            c.execute("INSERT OR IGNORE INTO sites(domain, first_seen) "
                      "VALUES(?, ?)",
                      (domain, datetime.now().isoformat()[:19]))
            c.execute(
                "UPDATE sites SET primary_backend=?, fallback_backend=?, "
                "depth_mode=?, anti_bot_tier=? WHERE domain=?",
                (primary, fallback, depth_mode, anti_bot_tier, domain)
            )
        return True
    except Exception:
        return False


# v13.87.33 · tag 权重累积 · 用户 2026-07-23 goal
_TAG_KEYWORDS = {
    # 每 tag · 一组关键词 · query 里命中就 +score
    "rag": [r"\brag\b", r"检索增强", r"retrieval"],
    "agent": [r"\bagent\b", r"多智能体", r"multi.agent"],
    "llm_eval": [r"评测", r"evaluation", r"benchmark"],
    "ai_side_hustle": [r"副业", r"变现", r"月入"],
    "coding_tool": [r"cursor", r"copilot", r"github copilot", r"claude code"],
    "framework_rust": [r"\brust\b", r"cargo", r"tokio"],
    "framework_py": [r"python", r"asyncio", r"fastapi", r"django"],
    "prompt_eng": [r"prompt", r"提示词", r"prompting"],
    "startup": [r"startup", r"独立开发", r"opc", r"一人公司"],
    "data_eng": [r"数据", r"etl", r"pipeline", r"warehouse"],
    "product_mgr": [r"产品经理", r"\bpm\b", r"prd", r"需求"],
    "ux_ui": [r"设计", r"ux", r"ui", r"figma"],
}


def _extract_tags_from_query(query: str) -> list:
    """给 query 抽命中的 tag"""
    if not query:
        return []
    import re as _re
    q_lower = query.lower()
    hit = []
    for tag, patterns in _TAG_KEYWORDS.items():
        for p in patterns:
            if _re.search(p, q_lower):
                hit.append(tag)
                break
    return hit


def record_tag_hit(domain: str, tags: list, content_len: int = 0) -> bool:
    """v13.87.33 · 记 domain 对 tag 的贡献 · content_len 越高 score+ 越多
    score 累加规则:
      full (>1000 字): +3
      partial (200-1000): +1
      shell (<200): +0
    """
    if not domain or not tags:
        return False
    if content_len < 200:
        return False
    delta = 3 if content_len > 1000 else 1
    try:
        with _conn() as c:
            row = c.execute("SELECT tags FROM sites WHERE domain=?",
                              (domain,)).fetchone()
            if not row:
                # domain 还没记 · 先建
                c.execute("INSERT OR IGNORE INTO sites(domain, first_seen) "
                          "VALUES(?, ?)",
                          (domain, datetime.now().isoformat()[:19]))
                cur_tags = {}
            else:
                try:
                    cur_tags = json.loads(row[0] or "{}")
                except Exception:
                    cur_tags = {}
            for t in tags:
                cur_tags[t] = cur_tags.get(t, 0) + delta
            c.execute("UPDATE sites SET tags=? WHERE domain=?",
                      (json.dumps(cur_tags, ensure_ascii=False), domain))
        return True
    except Exception:
        return False


def top_domains_for_tag(tag: str, limit: int = 5,
                        min_rating: int = 30) -> list:
    """v13.87.33 · 给 tag · 返 top N domain (按 tag score 排)
    planner 下次查同 tag 优先挑这些站点
    """
    if not tag:
        return []
    try:
        with _conn() as c:
            # SQLite JSON1 · 查 tags 里含该 tag 的 · 按 score 排
            # 简化: 全捞 · Python 侧过滤
            rows = c.execute(
                "SELECT domain, tags, rating, hits, primary_backend "
                "FROM sites WHERE tags LIKE ? AND rating >= ?",
                (f'%"{tag}"%', min_rating)
            ).fetchall()
        scored = []
        for r in rows:
            try:
                t = json.loads(r[1] or "{}")
                if tag in t:
                    scored.append({
                        "domain": r[0], "tag_score": t[tag],
                        "rating": r[2], "hits": r[3],
                        "primary": r[4],
                    })
            except Exception:
                continue
        scored.sort(key=lambda x: (-x["tag_score"], -x["rating"]))
        return scored[:limit]
    except Exception:
        return []


def bootstrap_known_rules() -> int:
    """v13.87.25 · 把 _KNOWN_STRATEGIES 写进 DB · 让后续 SearchAgent 直接查
    返回: 学到的 domain 数
    """
    count = 0
    for domain, s in _KNOWN_STRATEGIES.items():
        if learn_strategy(domain, s["primary"], s["fallback"],
                           s["depth_mode"], s["anti_bot_tier"]):
            count += 1
    return count


def _auto_rating(hits, avg_len, prev_rating, ok_ratio=1.0):
    """v13.63.3 · rating 自动调整规则:
    · hits ≥ 10 + avg_len ≥ 5000 : +3 每 5 hits
    · avg_len < 500 (垃圾内容) : -2 每次
    · ok_ratio < 0.5 (半数挂) : -5
    · 上下夹 [10, 100]
    """
    r = prev_rating
    if hits >= 10 and avg_len >= 5000 and hits % 5 == 0:
        r += 3
    if avg_len and avg_len < 500 and hits >= 3:
        r -= 2
    if ok_ratio < 0.5 and hits >= 5:
        r -= 5
    return max(10, min(100, r))


def record(url: str, via: str, ok: bool, content_len: int, time_s: float,
           topic: str = "", extracted_rules: list = None):
    """每次抓取都调 · 累积站点画像"""
    if not url:
        return
    dom = urlparse(url).netloc.lower()
    if not dom:
        return
    now = datetime.now().isoformat()
    c = _conn()
    try:
        row = c.execute("SELECT hits, avg_len, avg_time_s, topics, best_backend, rating FROM sites WHERE domain=?", (dom,)).fetchone()
        if row:
            hits, avg_len, avg_t, topics_json, best, prev_rating = row
            topics = set(json.loads(topics_json or "[]"))
            if topic: topics.add(topic[:50])
            new_hits = hits + 1
            new_avg_len = (avg_len * hits + content_len) / new_hits
            new_avg_t = (avg_t * hits + time_s) / new_hits
            # best_backend: 命中最长的
            new_best = via if (content_len > avg_len and ok) else best
            # v13.63.3 · rating 每次 record 自动调
            new_rating = _auto_rating(new_hits, new_avg_len, prev_rating)
            c.execute("""UPDATE sites SET hits=?, avg_len=?, avg_time_s=?,
                         topics=?, best_backend=?, last_visited=?, rating=? WHERE domain=?""",
                      (new_hits, new_avg_len, new_avg_t,
                       json.dumps(list(topics)[:20]), new_best or via, now,
                       new_rating, dom))
        else:
            c.execute("""INSERT INTO sites(domain, first_seen, hits, avg_len,
                         avg_time_s, best_backend, topics, last_visited)
                         VALUES(?,?,?,?,?,?,?,?)""",
                      (dom, now, 1, content_len, time_s, via,
                       json.dumps([topic[:50]] if topic else []), now))
        c.execute("""INSERT INTO visits(domain, url, ts, via, ok, len, time_s, topic, extracted_rules)
                     VALUES(?,?,?,?,?,?,?,?,?)""",
                  (dom, url[:500], now, via, 1 if ok else 0, content_len, time_s,
                   topic[:100], json.dumps(extracted_rules or [])))
        c.commit()
    finally:
        c.close()


def rate(domain: str, delta: int, note: str = ""):
    """好评 +5 差评 -5 · 上下夹 0-100"""
    dom = domain.lower().strip()
    c = _conn()
    try:
        row = c.execute("SELECT rating, notes FROM sites WHERE domain=?", (dom,)).fetchone()
        if not row:
            return False
        new_r = max(0, min(100, row[0] + delta))
        new_notes = (row[1] or "") + ("\n" + note if note else "")
        c.execute("UPDATE sites SET rating=?, notes=? WHERE domain=?", (new_r, new_notes[-2000:], dom))
        c.commit()
        return True
    finally:
        c.close()


def top(n: int = 20, topic: str = None, min_rating: int = 30) -> list:
    """收藏 top · 按 rating*log(hits) 排"""
    import math
    c = _conn()
    try:
        rows = c.execute("""SELECT domain, hits, avg_len, best_backend, topics,
                             rating, notes, last_visited FROM sites
                             WHERE rating>=?""", (min_rating,)).fetchall()
    finally:
        c.close()
    out = []
    for domain, hits, avg_len, best, topics_json, rating, notes, last in rows:
        topics = json.loads(topics_json or "[]")
        if topic and not any(topic.lower() in t.lower() for t in topics):
            continue
        score = rating * math.log(hits + 1)
        out.append({
            "domain": domain, "hits": hits, "avg_len": int(avg_len or 0),
            "best_backend": best, "topics": topics, "rating": rating,
            "score": round(score, 1), "last_visited": last,
            "notes": (notes or "")[-200:],
        })
    return sorted(out, key=lambda x: -x["score"])[:n]


def need_revisit(hours: int = 24, min_rating: int = 60) -> list:
    """该复访的:rating 高但超 N 小时没访 · 用于 goal_driven_explorer 主动巡站"""
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    c = _conn()
    try:
        rows = c.execute("""SELECT domain, rating, last_visited, topics FROM sites
                             WHERE rating>=? AND (last_visited IS NULL OR last_visited < ?)
                             ORDER BY rating DESC LIMIT 30""",
                         (min_rating, cutoff)).fetchall()
        return [{"domain": d, "rating": r, "last_visited": lv,
                 "topics": json.loads(t or "[]")} for d, r, lv, t in rows]
    finally:
        c.close()


def stats() -> dict:
    c = _conn()
    try:
        n_sites = c.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
        n_visits = c.execute("SELECT COUNT(*) FROM visits").fetchone()[0]
        n_high = c.execute("SELECT COUNT(*) FROM sites WHERE rating>=70").fetchone()[0]
        recent = c.execute("""SELECT domain, hits, rating FROM sites
                               ORDER BY last_visited DESC LIMIT 5""").fetchall()
    finally:
        c.close()
    return {"n_sites": n_sites, "n_visits": n_visits, "n_high_rated": n_high,
            "recent": [{"domain": d, "hits": h, "rating": r} for d, h, r in recent]}


# 种子数据 · 冷启动时喂进最基础的公认好站
SEED_SITES = [
    ("github.com", 70, "code", "playwright"),
    ("news.ycombinator.com", 70, "tech_discussion", "hn_algolia_frontpage"),
    ("stackoverflow.com", 65, "code_qa", "curl"),
    ("arxiv.org", 60, "research", "playwright"),
    ("docs.python.org", 65, "docs", "curl"),
    ("developer.mozilla.org", 65, "docs", "curl"),
    ("reddit.com", 60, "discussion", "cdp"),
    ("twitter.com", 55, "social", "cdp"),
    ("x.com", 55, "social", "cdp"),
    ("juejin.cn", 55, "cn_tech", "browseract"),
    ("csdn.net", 50, "cn_tech", "browseract"),
    ("zhihu.com", 60, "cn_qa", "firecrawl"),
    ("xiaohongshu.com", 55, "cn_life", "firecrawl"),
    ("weibo.com", 50, "cn_social", "firecrawl"),
    ("36kr.com", 55, "cn_tech_news", "browseract"),
    ("mp.weixin.qq.com", 55, "cn_articles", "browseract"),
    ("v2ex.com", 55, "cn_tech_discussion", "cdp"),
    ("sspai.com", 55, "cn_tools", "playwright"),
    ("bilibili.com", 50, "cn_video", "browseract"),
    ("medium.com", 55, "articles", "playwright"),
    ("firecrawl.dev", 60, "tool_docs", "firecrawl"),
    ("docs.browseract.com", 60, "tool_docs", "firecrawl"),
    ("hn.algolia.com", 65, "hn_api", "curl"),
    ("hacker-news.firebaseio.com", 65, "hn_api", "curl"),
]


def seed():
    """冷启动 · 喂进公认好站"""
    now = datetime.now().isoformat()
    c = _conn()
    try:
        for domain, rating, topic, best in SEED_SITES:
            c.execute("""INSERT OR IGNORE INTO sites(domain, first_seen, hits, rating,
                         topics, best_backend, last_visited)
                         VALUES(?,?,?,?,?,?,?)""",
                      (domain, now, 0, rating, json.dumps([topic]), best, None))
        c.commit()
    finally:
        c.close()


if __name__ == "__main__":
    try:
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("cmd", choices=["seed", "stats", "top", "revisit", "record"])
        ap.add_argument("--topic", default=None)
        ap.add_argument("--n", type=int, default=20)
        ap.add_argument("--hours", type=int, default=24)
        ap.add_argument("--url", default="")
        args = ap.parse_args()
        if args.cmd == "seed":
            seed(); print(json.dumps(stats(), ensure_ascii=False, indent=2))
        elif args.cmd == "stats":
            print(json.dumps(stats(), ensure_ascii=False, indent=2))
        elif args.cmd == "top":
            for r in top(n=args.n, topic=args.topic):
                print(f"  {r['rating']:>3} · {r['domain']:<35} · hits={r['hits']:>3} · {r['topics']}")
        elif args.cmd == "revisit":
            for r in need_revisit(hours=args.hours):
                print(f"  {r['rating']:>3} · {r['domain']:<35} · last={r['last_visited']}")
        elif args.cmd == "record":
            record(args.url, "manual", True, 0, 0.0)
            print("recorded")
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise
