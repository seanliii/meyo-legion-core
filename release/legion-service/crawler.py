#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""持续爬取:session 订阅 URL,抓取内容,检测变化,变化时把新内容追加进 session

用户需求"网页可以持续爬取内容更新"。这里做真爬取:
  - 一个 session 可订阅多个 URL(source)
  - crawl_now:抓取每个 URL、提取正文摘要、和上次快照比对
  - 内容变化 → 把变更追加进 session 的一个 turn(交付物网页持续更新)
  - 配合 cron(带过期)可定时爬;本机原型阶段 crawl_now 手动/按需触发,真定时待部署调度

存储:订阅 + 快照存 CRAWL_DIR(默认 ~/.legion/crawl),每个 session 一个 JSON。
抓取用 requests(公用工具·已装),超时/UA/异常都兜住,单个 URL 挂不影响其他。
"""
from __future__ import annotations
import os
import re
import json
import time
import html
import hashlib
import threading

import legion_paths as _paths
CRAWL_DIR = os.environ.get("LEGION_CRAWL_DIR") or _paths.home_under("crawl")
_UA = "Mozilla/5.0 (LegionCrawler/1.0)"
_TIMEOUT = 12
# 订阅源读改写要加锁:add/remove 是 load→改→save,ThreadingHTTPServer 下多请求并发写同一
# session 会丢更新(实测 10 并发 add 只剩 3)。进程内锁即可(单 session 文件单进程写)。
_LOCK = threading.RLock()


def _ensure():
    os.makedirs(CRAWL_DIR, exist_ok=True)


def _path(sid: str) -> str:
    safe = "".join(c for c in sid if c.isalnum() or c in "-_")[:60]
    return os.path.join(CRAWL_DIR, f"{safe}.json")


def _load(sid: str) -> dict:
    p = _path(sid)
    if os.path.isfile(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            pass
    return {"sid": sid, "sources": []}


def _save(sid: str, data: dict):
    _ensure()
    with open(_path(sid), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def add_source(sid: str, url: str, note: str = "") -> dict:
    """给 session 订阅一个要持续爬取的 URL。"""
    if not re.match(r"^https?://", url or ""):
        return {"error": f"非法 URL(需 http/https): {url!r}"}
    with _LOCK:  # load→改→save 全程持锁·防并发丢更新
        data = _load(sid)
        if any(s["url"] == url for s in data["sources"]):
            return {"error": "该 URL 已订阅", "url": url}
        data["sources"].append({"url": url, "note": note, "added": time.time(),
                                "last_hash": None, "last_crawled": None,
                                "last_summary": "", "crawl_count": 0})
        _save(sid, data)
        return {"sid": sid, "url": url, "sources": len(data["sources"])}


def list_sources(sid: str) -> list[dict]:
    return _load(sid).get("sources", [])


def remove_source(sid: str, url: str) -> dict:
    with _LOCK:
        data = _load(sid)
        n = len(data["sources"])
        data["sources"] = [s for s in data["sources"] if s["url"] != url]
        _save(sid, data)
        return {"removed": n - len(data["sources"]), "url": url}


def _extract_summary(raw_html: str, max_len: int = 600) -> str:
    """从 HTML 里粗提正文摘要:去脚本/样式/标签,取可读文本前 N 字。

    用 bs4 若在则更干净;不在则退回正则(公用工具里 bs4 已装,但也兜底)。"""
    text = ""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(raw_html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        # 标题 + 正文
        title = (soup.title.string if soup.title else "") or ""
        body = soup.get_text(separator=" ", strip=True)
        text = (title + " — " + body) if title else body
    except Exception:
        t = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw_html)
        t = re.sub(r"(?s)<[^>]+>", " ", t)
        text = html.unescape(t)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]


def crawl_now(sid: str, append_cb=None) -> dict:
    """抓取该 session 订阅的所有 URL,检测变化。有变化则通过 append_cb 追加进 session。

    append_cb(question, result_dict, mode) —— 由调用方(server)传入 sessions.save_turn 的包装,
    这样 crawler 不直接依赖 sessions(解耦)。
    返回本次爬取摘要:{crawled, changed, results:[{url, changed, summary, error}]}。
    """
    data = _load(sid)
    sources = data.get("sources", [])
    if not sources:
        return {"error": "该 session 无订阅源", "crawled": 0}
    import requests
    results, changed_items = [], []
    for s in sources:
        item = {"url": s["url"], "changed": False, "summary": "", "error": ""}
        try:
            # 流式读+硬上限(军团 hermes dogfood 揪出:.text 一次性下完整响应·超大页面内存DoS)。
            # 不依赖 Content-Length(可伪造/缺失)·逐块累计到上限即停。
            _MAX_BODY = 5 * 1024 * 1024
            with requests.get(s["url"], timeout=_TIMEOUT, headers={"User-Agent": _UA}, stream=True) as r:
                if r.status_code >= 400:
                    body = ""
                else:
                    chunks, size = [], 0
                    for chunk in r.iter_content(64 * 1024):
                        size += len(chunk)
                        if size > _MAX_BODY:
                            break  # 到上限即停·不再吃内存
                        chunks.append(chunk)
                    body = b"".join(chunks).decode(r.encoding or "utf-8", errors="replace")
            summary = _extract_summary(body) if body else f"HTTP {r.status_code}"
            h = hashlib.sha1(summary.encode("utf-8")).hexdigest()
            item["summary"] = summary
            if h != s.get("last_hash"):
                item["changed"] = True
                changed_items.append(item)
                s["last_hash"] = h
                s["last_summary"] = summary
            s["last_crawled"] = time.time()
            s["crawl_count"] = s.get("crawl_count", 0) + 1
        except Exception as e:
            item["error"] = f"{type(e).__name__}: {str(e)[:80]}"
        results.append(item)
    _save(sid, data)

    # 有变化 → 追加进 session(持续爬取更新交付物网页)
    if changed_items and append_cb:
        lines = "\n\n".join(
            f"**{it['url']}** 有更新：\n{it['summary'][:400]}" for it in changed_items)
        try:
            append_cb(
                f"🕷️ 持续爬取更新（{len(changed_items)} 个源变化）",
                {"results": [{"key": "crawler", "status": "done",
                              "final": lines, "elapsed": 0}], "mode": "pk"},
                "pk")
        except Exception:
            pass
    return {"sid": sid, "crawled": len(results),
            "changed": len(changed_items), "results": results}


if __name__ == "__main__":
    sid = "crawl_demo"
    print(add_source(sid, "https://example.com"))
    print(crawl_now(sid))
