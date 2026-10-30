"""space_web · 2026-08-13 · web 空间(站点/页面)的 Space 适配器(挂 traversal_core)

web 是跨空间接力最常用的节点(图像认出品牌 → 来 web 查该品牌的信息)。
不剥离 deep_research_agent(1635行) —— 只做一个轻量 Space:
  · indexer/reader: 调 search_fabric 的可用搜索源(多源容错·哪个能用用哪个)
  · fetch: requests 取页面正文(候选 URL 深读)
  · observe 真调网络(能动式读取·可能失败)

地址方案:
  · "search:{query}"     → 搜索,返回结果摘要 + 候选 URL
  · "url:{link}"         → fetch 该页面正文 + 页面内链接
  · "scroll:{url}:{n}"   → 滚动到第 n 屏(长页面分段读)
  · "tab:{url}:{label}"  → 切换到指定 Tab/折叠区域(交互后重读)
  · "form:{url}"         → 标记需登录态/表单交互的页面(回传需交互提示)

只做寻址,零新增遍历逻辑。搜索/fetch 失败明确回传(不静默)。
"""
from __future__ import annotations
import os, sys
from urllib.parse import urljoin, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".."))
# search_fabric 在 v11/evolution
_EVO = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    "v11", "evolution")
sys.path.insert(0, _EVO)
from traversal_core import Observation, Action

try:
    import search_fabric as _sf
except Exception:
    _sf = None

# 搜索源优先级(哪个可用用哪个·实测 anysearch 免key可用)
_SOURCES = ["_search_anysearch", "_search_ddgs", "_search_wikipedia",
            "_search_aisa_smart", "_search_tavily_fabric"]


def _do_search(query: str, top_k: int = 5) -> list[dict]:
    """多源容错搜索:按优先级试,返回第一个有真实结果的源。"""
    if _sf is None:
        return []
    for name in _SOURCES:
        fn = getattr(_sf, name, None)
        if not fn:
            continue
        try:
            r = fn(query, top_k) or []
        except Exception:
            continue
        # 过滤 _error 占位
        good = [x for x in r if isinstance(x, dict) and not x.get("_error")
                and (x.get("content") or x.get("title") or x.get("url"))]
        if good:
            return good
    return []


def _extract_links(html: str, base_url: str, limit: int = 12) -> list[str]:
    """从页面源码提取可寻址链接；不按站点规则猜页面结构。

    解析普通链接和 ``rel=next`` 分页链接。动态站点若源码没有链接，仍由 smart_fetch
    提供正文证据；不会把抓不到 DOM 伪装成已经遍历过页面内部。
    """
    try:
        import re
        links = []
        for href in re.findall(r"<a[^>]+href=[\"']([^\"'#]+)", html or "", flags=re.I):
            candidate = urljoin(base_url, href.strip())
            parsed = urlparse(candidate)
            if parsed.scheme in ("http", "https") and candidate not in links:
                links.append(candidate)
                if len(links) >= limit:
                    break
        return links
    except Exception:
        return []


def _extract_dom_blocks(html: str, base_url: str = "", limit: int = 20) -> list[dict]:
    """从 HTML 中抽取结构化语义块(标题/段落/列表/表格/表单)。
    每块返回 {kind, selector, text}。fail-safe:异常返回空列表。
    这不是完整的 DOM 解析——是从原始 HTML 里用正则提取语义区块，
    让决策器能定向跳到某个 DOM 区域而非只看一整坨正文。"""
    try:
        import re
        if not html:
            return []
        blocks = []
        # 标题块(h1-h6)
        for m in re.finditer(r'<(h[1-6])[^>]*>(.*?)</\1>', html, re.S | re.I):
            text = re.sub(r'<[^>]+>', '', m.group(2)).strip()
            if text:
                blocks.append({"kind": "heading", "selector": m.group(1), "text": text[:500]})
        # 列表块(ul/ol)
        for m in re.finditer(r'<(ul|ol)[^>]*>(.*?)</\1>', html, re.S | re.I):
            items = re.findall(r'<li[^>]*>(.*?)</li>', m.group(2), re.S | re.I)
            texts = [re.sub(r'<[^>]+>', '', it).strip() for it in items if it.strip()]
            if texts:
                blocks.append({"kind": "list", "selector": m.group(1),
                               "text": " | ".join(texts[:20])[:800]})
        # 表格块(table)
        for m in re.finditer(r'<table[^>]*>(.*?)</table>', html, re.S | re.I):
            rows = re.findall(r'<tr[^>]*>(.*?)</tr>', m.group(1), re.S | re.I)
            cells = []
            for row in rows[:50]:
                cols = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.S | re.I)
                cells.append([re.sub(r'<[^>]+>', '', c).strip() for c in cols if c.strip()])
            if cells:
                blocks.append({"kind": "table", "selector": "table",
                               "text": str(cells[:50])[:1000]})
        # 表单块(form)
        for m in re.finditer(r'<form[^>]*>(.*?)</form>', html, re.S | re.I):
            inputs = re.findall(r'<(?:input|select|textarea)[^>]*name=["\']([^"\']+)', m.group(1), re.I)
            if inputs:
                blocks.append({"kind": "form", "selector": "form",
                               "text": f"表单字段: {', '.join(inputs[:15])}"})
        # 截断到 limit
        return blocks[:limit]
    except Exception:
        return []


def _fetch(url: str, max_chars: int = 4000) -> dict:
    """抓正文并返回页面内部候选地址。

    正文优先复用项目的 smart_fetch（可升级浏览器渲染/反爬分级）；原始 HTML 只用于
    提取链接与分页候选。二者均失败时明确回报边界，而非把空壳页面当作已读内容。
    """
    rendered = ""
    via = ""
    try:
        from search_agent_orchestrator import smart_fetch
        result = smart_fetch(url) or {}
        if result.get("ok"):
            rendered = str(result.get("content") or "")
            via = str(result.get("actual_via") or "smart_fetch")
    except Exception:
        pass
    try:
        import requests
        headers = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36")}
        response = requests.get(url, timeout=20, headers=headers, allow_redirects=True)
        if response.status_code != 200:
            if rendered:
                return {"content": rendered[:max_chars], "links": [],
                        "note": f"rendered via={via}; 原始HTML HTTP {response.status_code}"}
            return {"content": "", "links": [], "note": f"fetch失败 HTTP {response.status_code}"}
        html = response.text
        links = _extract_links(html, response.url)
        import re
        plain = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
        plain = re.sub(r"<[^>]+>", " ", plain)
        plain = re.sub(r"\s+", " ", plain).strip()
        content = rendered or plain
        note = f"正文={via}; 发现{len(links)}个页面内候选" if rendered else f"直取正文; 发现{len(links)}个页面内候选"
        return {"content": content[:max_chars], "links": links, "note": note, "html": html[:20000]}
    except Exception as e:
        if rendered:
            return {"content": rendered[:max_chars], "links": [],
                    "note": f"rendered via={via}; 原始HTML异常 {type(e).__name__}", "html": ""}
        return {"content": "", "links": [], "note": f"fetch异常 {type(e).__name__}:{str(e)[:60]}", "html": ""}


class WebSpace:
    name = "web"
    kind = "info"

    def __init__(self, seed_query: str = ""):
        self.seed = seed_query

    def entry_addrs(self) -> list[str]:
        # 入口=用子目标搜索(seed 为空时由 observe 拿 query)
        return [f"search:{self.seed}" if self.seed else "search:"]

    def observe(self, addr: str, query: str = "") -> Observation:
        if _sf is None:
            return Observation(addr=addr, content="", kind="empty",
                               note="[DEGRADED]search_fabric 未加载")
        if addr.startswith("search:"):
            q = addr[len("search:"):] or query
            if not q:
                return Observation(addr=addr, content="", kind="empty", note="无搜索词")
            results = _do_search(q, top_k=5)
            if not results:
                return Observation(addr=addr, content="", kind="empty",
                                   note="所有搜索源无结果(库未装/无网/被拦)·换渠道")
            # 摘要 + 候选 URL
            summary = "\n".join(
                f"- {r.get('title','')[:80]}: {(r.get('content') or '')[:200]} <{r.get('url','')}>"
                for r in results[:5])
            urls = [f"url:{r['url']}" for r in results if r.get("url")]
            return Observation(addr=addr, content=summary, kind="search_results",
                               candidates=urls, note=f"{len(results)}条结果",
                               addr_meta={"axis": "web_search", "query": q})
        if addr.startswith("url:"):
            url = addr[len("url:"):]
            page = _fetch(url)
            parsed = urlparse(url)
            urls = [f"url:{link}" for link in page.get("links") or [] if link != url]
            # v99p · DOM 语义提取:从 HTML 中抽取结构化语义块(标题/列表/表格/表单)
            #   让决策器看到的不是一坨正文,而是有结构的 DOM 语义地址。
            dom_blocks = _extract_dom_blocks(page.get("html", ""), url)
            block_cands = [f"block:{url}:{i}" for i in range(len(dom_blocks))]
            # 检测是否需要 JS 交互(空正文但有链接/表单元素)
            needs_js = (not page.get("content", "").strip()
                        and (page.get("links") or dom_blocks))
            # 生成交互候选(滚动/Tab/表单)
            inter_cands = []
            if len(page.get("content", "")) > 3000:
                inter_cands.append(f"scroll:{url}:1")
            if any(b.get("kind") == "form" for b in dom_blocks):
                inter_cands.append(f"form:{url}")
            note = page.get("note", "")[:160]
            if needs_js:
                note += " · [需JS渲染]页面正文为空但有DOM结构,可能需浏览器交互"
            return Observation(addr=addr, content=page.get("content", ""), kind="page",
                               candidates=urls + block_cands + inter_cands, note=note,
                               addr_meta={"axis": "web", "host": parsed.netloc,
                                          "path": parsed.path or "/",
                                          "needs_js": needs_js,
                                          "dom_blocks": len(dom_blocks)})
        if addr.startswith("scroll:"):
            # 滚动到第 n 屏:重新 fetch 并截取对应段落(模拟分页读)
            parts = addr[len("scroll:"):].split(":")
            if len(parts) < 2:
                return Observation(addr=addr, content="", kind="empty", note=f"非法scroll地址:{addr}")
            url = parts[0]
            try:
                screen_n = int(parts[1])
            except Exception:
                screen_n = 0
            page = _fetch(url, max_chars=20000)
            content = page.get("content", "")
            # 按 3000 字符一屏分段
            chunk_size = 3000
            start = screen_n * chunk_size
            end = start + chunk_size
            chunk = content[start:end]
            if not chunk:
                return Observation(addr=addr, content="", kind="empty",
                                   note=f"已到页面底部(第{screen_n}屏无内容)")
            next_cands = []
            if len(content) > end:
                next_cands.append(f"scroll:{url}:{screen_n + 1}")
            if screen_n > 0:
                next_cands.append(f"scroll:{url}:{screen_n - 1}")
            return Observation(addr=addr, content=chunk, kind="page_scroll",
                               candidates=next_cands,
                               note=f"第{screen_n}屏({start}-{end}字符)",
                               addr_meta={"axis": "web", "host": urlparse(url).netloc,
                                          "scroll": screen_n, "url": url})
        if addr.startswith("tab:"):
            # Tab/折叠区域切换:重新 fetch 并尝试定位标签内容
            parts = addr[len("tab:"):].split(":", 1)
            if len(parts) < 2:
                return Observation(addr=addr, content="", kind="empty", note=f"非法tab地址:{addr}")
            url = parts[0]
            tab_label = parts[1]
            page = _fetch(url)
            # 在正文中搜索 Tab 标签附近的内容(简单文本匹配)
            content = page.get("content", "")
            idx = content.lower().find(tab_label.lower())
            if idx >= 0:
                # 取 Tab 标签后 2000 字符
                chunk = content[idx:idx + 2000]
            else:
                chunk = f"未找到Tab '{tab_label}'·可能需JS交互"
            return Observation(addr=addr, content=chunk, kind="page_tab",
                               candidates=[],
                               note=f"Tab '{tab_label}' 内容段",
                               addr_meta={"axis": "web", "tab": tab_label, "url": url})
        if addr.startswith("form:"):
            url = addr[len("form:"):]
            return Observation(addr=addr, content="", kind="page_form",
                               note=f"该页面含表单/登录态·可能需要交互才能获取完整内容·URL={url}",
                               addr_meta={"axis": "web", "needs_interaction": True, "url": url})
        if addr.startswith("block:"):
            # DOM 语义块:从 HTML 抽取的结构化子区域
            parts = addr[len("block:"):].split(":", 1)
            if len(parts) < 2:
                return Observation(addr=addr, content="", kind="empty", note=f"非法block地址:{addr}")
            url = parts[0]
            try:
                block_idx = int(parts[1])
            except Exception:
                block_idx = 0
            page = _fetch(url)
            blocks = _extract_dom_blocks(page.get("html", ""), url)
            if block_idx < 0 or block_idx >= len(blocks):
                return Observation(addr=addr, content="", kind="empty", note=f"块索引超出范围:{block_idx}/{len(blocks)}")
            block = blocks[block_idx]
            return Observation(addr=addr, content=block.get("text", ""), kind=f"dom_{block.get('kind', 'block')}",
                               candidates=[f"block:{url}:{i}" for i in range(len(blocks)) if i != block_idx],
                               note=f"DOM块[{block.get('kind','block')}] {block.get('selector','')}",
                               addr_meta={"axis": "web", "block_idx": block_idx,
                                          "dom_kind": block.get("kind", ""), "url": url})
        return Observation(addr=addr, content="", kind="empty", note=f"地址不存在:{addr}")

    def act(self, action: Action) -> dict:
        return {"ok": True, "detail": f"web-space act {action.kind}"}


if __name__ == "__main__":
    from traversal_core import traverse
    import sys as _s
    q = _s.argv[1] if len(_s.argv) > 1 else "What year was the Python programming language first released?"
    sp = WebSpace()
    # search space 入口需要 query→用 goal 作 seed
    sp.seed = q
    r = traverse(sp, q, max_steps=4)
    print(f"ans={r.answer[:150]!r} steps={r.steps} stop={r.stop_reason}")
    for t in r.trace:
        print(f"  step{t.get('step')}: {t.get('action')} {t.get('target','')[:40]}")
