#!/usr/bin/env python3
"""
v13.88.86 · 2026-07-29 · lxml 结构化抽取引擎

用户 2026-07-29:
  「lxml 实战... 和我们的搜索 agent 缺这个吗?」
  「统一加上... 确定监控系统能上下游监督和串联和调试, 自己进化」

── 解决什么 ──
之前 extract_readable() 用 trafilatura 抽"整块正文" → 列表页/表格/多字段混成一坨
lxml 补"精准结构化抽取"这环:
  · 列表页 → 逐 item 拆字段 (标题/链接/价格/评分/描述)
  · 详情页 → 精准抽 title/author/date/body
  · 表格 → 行列对齐
  · CSS 选择器 / XPath 都支持

── 设计 ──
4 层 fallback:
  L1 · 自动检测页面类型 → 选模板
  L2 · 域名专属 XPath 模板 (豆瓣/知乎/CSDN/京东...)
  L3 · 通用结构化抽取 (重复容器检测 + 字段推断)
  L4 · 退回纯文本 (text_content)

── 监控接入 ──
每次抽取落 .lxml_extract.jsonl → monitor_hub 看 EXTRACT store
  · method (auto/domain_template/generic/plain)
  · page_type (list/detail/table/unknown)
  · n_items (列表页抓了几个 item)
  · elapsed_ms
  · html_in / text_out 比
  · token_savings vs 全文

── 进化接入 ──
每域名成功模板落 .lxml_domain_templates.jsonl
下次同类域名直接走最优模板
"""

import os
import sys
import json
import time
import re
from pathlib import Path
from datetime import datetime
from typing import Optional

HERE = Path(__file__).resolve().parent
V11 = HERE.parent

# ═══════════════════════════════════════════════
# 韧性抓取接入 (2026-08-20 · 治「抓正文 429/超时/限流被 except 静默吞」)
#   复用公共模块 resilient_fetch: 限流(按域名) + 退避重试(429/timeout) + 缓存
# ═══════════════════════════════════════════════
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
try:
    import resilient_fetch as _rf  # 同目录公共韧性模块
except Exception:  # pragma: no cover - 兜底: 拿不到就退回裸抓(不破坏原有行为)
    _rf = None

_LXML_CACHE_NAMESPACE = "lxml_content"
_LXML_CACHE_TTL_S = 1800      # 正文缓存 30 分钟·同 URL 短期不重复抓
_LXML_FETCH_RATE_PER_SEC = 2  # 按域名限流·抓正文最易被目标站 429
_LXML_RETRY_BACKOFFS = (1.0, 2.0)

if _rf is not None:
    _LXML_RATE_LIMITER = _rf.RateLimiter(rate_root=HERE / ".lxml_ratelimit")
else:
    _LXML_RATE_LIMITER = None


def _fetch_html_resilient(url: str, headers: dict, timeout: int = 15) -> str:
    """抓正文·带 限流(按域名) + 429/超时退避重试 + 成功正文缓存。

    治「requests.get 遇 429/限流/超时被 except 静默吞成空」的裸奔病。
    韧性模块不可用时退回裸抓(保持原行为·不破坏调用方)。
    """
    import requests
    from urllib.parse import urlparse

    domain = (urlparse(url).netloc or url)[:120]

    # 1) 缓存命中直接返回(同 URL 短期不重复抓)
    if _rf is not None:
        cached = _rf.cache_get(_LXML_CACHE_NAMESPACE, [url], ttl=_LXML_CACHE_TTL_S)
        if cached is not None:
            return cached[0]

    # 2) 按域名限流(阻塞式·遵守目标站礼仪)
    if _LXML_RATE_LIMITER is not None:
        _LXML_RATE_LIMITER.acquire(domain, rate_per_sec=_LXML_FETCH_RATE_PER_SEC, block=True)

    # 3) 单次抓取(把 HTTP 429/超时等归一成可被 is_transient 识别的错误串)
    def _do_fetch():
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            # HTTP 429/503/5xx 状态码显式转成 transient 标记串·触发重试
            if r.status_code == 429 or r.status_code >= 500:
                return {"__transient__": f"HTTP {r.status_code} too many requests"}
            return {"__html__": r.text}
        except Exception as e:  # 超时/连接错误等·转成错误串让 is_transient 判定
            return {"__err__": str(e)}

    def _is_transient(res):
        if not isinstance(res, dict):
            return False
        marker = res.get("__transient__") or res.get("__err__")
        return bool(marker) and _rf.is_transient_error(marker)

    if _rf is not None:
        res = _rf.with_retry(_do_fetch, is_transient_fn=_is_transient,
                             max_retries=2, backoffs=_LXML_RETRY_BACKOFFS)
        if isinstance(res, dict) and "__html__" in res:
            html = res["__html__"]
            # 4) 成功非空正文入缓存
            if html:
                _rf.cache_set(_LXML_CACHE_NAMESPACE, [url], html, ttl=_LXML_CACHE_TTL_S)
            return html
        # 重试耗尽仍 transient·或真实错误: 抛出让调用方原有 except 处理
        marker = (res.get("__transient__") or res.get("__err__")) if isinstance(res, dict) else str(res)
        raise RuntimeError(f"fetch failed after retry: {marker}")
    else:
        # 无韧性模块·裸抓(原行为)
        r = requests.get(url, headers=headers, timeout=timeout)
        return r.text


# ═══════════════════════════════════════════════
# 数据落盘
# ═══════════════════════════════════════════════
EXTRACT_LOG = V11 / ".lxml_extract.jsonl"
DOMAIN_TEMPLATES_LOG = V11 / ".lxml_domain_templates.jsonl"

# ═══════════════════════════════════════════════
# 域名专属模板库 (经验积累 · 可进化)
# ═══════════════════════════════════════════════
DOMAIN_TEMPLATES = {
    "books.toscrape.com": {
        "page_type": "list",
        "item_selector": "//article[@class='product_pod']",
        "fields": {
            "title":      ".//h3/a/@title",
            "link":       ".//h3/a/@href",
            "price":      ".//p[@class='price_color']/text()",
            "in_stock":   ".//p[contains(@class,'availability')]/@class",
            "rating":     ".//p[contains(@class,'star-rating')]/@class",
        },
        "list_pager": "//li[@class='next']/a/@href",
    },
    "movie.douban.com": {
        "page_type": "list",
        "item_selector": "//div[@class='item']",
        "fields": {
            "title":      ".//span[@class='title']/text()",
            "rating":     ".//span[@class='rating_num']/text()",
            "quote":      ".//p[@class='quote']/span/text()",
            "link":       ".//div[@class='hd']/a/@href",
        },
    },
    "news.ycombinator.com": {
        "page_type": "list",
        "item_selector": "//tr[@class='athing']",
        "fields": {
            "rank":       ".//span[@class='rank']/text()",
            "title":      ".//span[@class='titleline']/a/text()",
            "link":       ".//span[@class='titleline']/a/@href",
            "site":       ".//span[@class='sitestr']/text()",
        },
    },
    "github.com": {
        "page_type": "list",
        "item_selector": "//div[contains(@class,'Box-sc-')]//article[contains(@class,'border')]",
        "fields": {
            "name":       ".//h3//a/text()",
            "link":       ".//h3//a/@href",
            "description": ".//p/text()",
            "stars":      ".//a[contains(@href,'stargazers')]/text()",
        },
    },
}


# ═══════════════════════════════════════════════
# 通用列表检测器 (无域名模板时用)
# ═══════════════════════════════════════════════
# 候选容器 (出现 3+ 次视为列表页)
LIST_CANDIDATE_SELECTORS = [
    ("//article",                           "article"),
    ("//div[contains(@class,'item')]",      "div.item"),
    ("//div[contains(@class,'product')]",   "div.product"),
    ("//div[contains(@class,'card')]",      "div.card"),
    ("//div[contains(@class,'result')]",    "div.result"),
    ("//li[contains(@class,'item')]",       "li.item"),
    ("//tr[@class='athing']",               "tr.athing"),
    ("//div[contains(@class,'post')]",      "div.post"),
    ("//div[contains(@class,'entry')]",     "div.entry"),
    ("//a[contains(@class,'question')]",    "a.question"),
]


def _safe_import_lxml():
    """尝试导入 lxml · 失败返回 None"""
    try:
        from lxml import html as lh
        return lh
    except ImportError:
        return None


def detect_page_type(tree, lh) -> str:
    """检测页面类型: list / detail / table / unknown"""
    # 表格检测
    tables = tree.xpath("//table")
    for t in tables:
        rows = t.xpath(".//tr")
        if len(rows) >= 3:
            cells = rows[0].xpath(".//td | .//th")
            if len(cells) >= 2:
                return "table"

    # 列表检测
    for sel, _ in LIST_CANDIDATE_SELECTORS:
        items = tree.xpath(sel)
        if len(items) >= 3:
            return "list"

    # 详情页检测
    title = tree.xpath("//h1//text() | //title/text()")
    article = tree.xpath("//article | //div[contains(@class,'article')] | //div[contains(@class,'content')]")
    if title and article:
        return "detail"

    return "unknown"


def extract_fields_from_item(item_tree, lh) -> dict:
    """从一个 item 里推断通用字段"""
    fields = {}

    # 标题: 第一个有意义的 h1-h4 或加粗文本
    for tag in ["h1", "h2", "h3", "h4", "b", "strong"]:
        t = item_tree.xpath(f".//{tag}//text()")
        if t and t[0].strip():
            fields["title"] = t[0].strip()
            break

    # 链接: 第一个 a/@href
    hrefs = item_tree.xpath(".//a/@href")
    links = [h for h in hrefs if h.startswith("http") or h.startswith("/")]
    if links:
        fields["link"] = links[0]

    # 价格: 正则匹数字+货币
    all_text = " ".join(item_tree.xpath(".//text()"))
    price_match = re.search(r'[£$€¥]\s*[\d,]+\.?\d*|\d+\.?\d*\s*(元|美元|USD|EUR|GBP)', all_text)
    if price_match:
        fields["price"] = price_match.group()

    # 评分: 数字+星/分 或 x.x/5 x.x/10
    rating_match = re.search(r'(\d+\.?\d*)\s*(/\s*5|/\s*10|星|分|★|☆)', all_text)
    if rating_match:
        fields["rating"] = rating_match.group()

    # 描述: p 标签文字 (排除极短)
    paras = item_tree.xpath(".//p//text()")
    desc = " ".join(p.strip() for p in paras if len(p.strip()) > 15)
    if desc:
        fields["description"] = desc[:500]

    # 图片: img/@src
    imgs = item_tree.xpath(".//img/@src")
    imgs = [i for i in imgs if i and not i.startswith("data:")]
    if imgs:
        fields["image"] = imgs[0]

    return fields


def extract_table(tree, lh) -> list:
    """从 table 抽结构化数据 → list of dict"""
    result = []
    tables = tree.xpath("//table")
    for t in tables:
        rows = t.xpath(".//tr")
        if len(rows) < 2:
            continue
        # 第一行作 header
        headers = [th.text_content().strip() for th in rows[0].xpath(".//th | .//td")]
        if not any(headers):
            continue
        for row in rows[1:]:
            cells = [td.text_content().strip() for td in row.xpath(".//td")]
            if len(cells) == len(headers):
                result.append(dict(zip(headers, cells)))
    return result


def extract_list(tree, lh, template: dict = None) -> list:
    """列表页抽取 → list of dict"""
    if template and "item_selector" in template:
        items = tree.xpath(template["item_selector"])
        field_map = template.get("fields", {})
    else:
        # 通用检测
        items = []
        field_map = {}
        for sel, _ in LIST_CANDIDATE_SELECTORS:
            items = tree.xpath(sel)
            if len(items) >= 3:
                break

    results = []
    for item in items:
        if template and field_map:
            # 按模板字段提取
            row = {}
            for fname, fsel in field_map.items():
                try:
                    v = item.xpath(fsel)
                    if v:
                        if isinstance(v, list):
                            row[fname] = v[0].strip() if isinstance(v[0], str) else v[0]
                        else:
                            row[fname] = v.strip() if isinstance(v, str) else v
                except Exception:
                    pass
            if row:
                results.append(row)
        else:
            # 通用推断
            row = extract_fields_from_item(item, lh)
            if row:
                results.append(row)

    return results


def extract_detail(tree, lh) -> dict:
    """详情页抽取 → dict"""
    result = {}

    # 标题
    h1 = tree.xpath("//h1//text()")
    if h1:
        result["title"] = h1[0].strip()
    else:
        t = tree.xpath("//title/text()")
        if t:
            result["title"] = t[0].strip()

    # 作者
    author = tree.xpath(
        '//*[contains(@class,"author")]//text() | '
        '//a[contains(@rel,"author")]//text() | '
        '//span[contains(@class,"name")]//text()'
    )
    author = [a.strip() for a in author if len(a.strip()) > 1 and len(a.strip()) < 50]
    if author:
        result["author"] = author[0]

    # 日期
    date_el = tree.xpath(
        '//*[contains(@class,"date")]//text() | '
        '//*[contains(@class,"time")]//text()'
    )
    date_el = [d.strip() for d in date_el if re.search(r'\d{4}', d)]
    if date_el:
        result["date"] = date_el[0]

    # 正文: 候选节点取最长的
    body_candidates = []
    for sel in ["//article", "//div[contains(@class,'article-body')]",
                "//div[contains(@class,'article-content')]",
                "//div[contains(@class,'content')]",
                "//div[contains(@class,'post-content')]",
                "//main"]:
        nodes = tree.xpath(sel)
        for n in nodes:
            txt = n.text_content().strip()
            if len(txt) > 100:
                body_candidates.append(txt)
    if body_candidates:
        result["body"] = max(body_candidates, key=len)[:10000]

    return result


def get_domain_template(url: str) -> Optional[dict]:
    """按 URL 查域名模板"""
    from urllib.parse import urlparse
    domain = urlparse(url).netloc.lower()
    # 精确匹配
    if domain in DOMAIN_TEMPLATES:
        return DOMAIN_TEMPLATES[domain]
    # 二级域名匹配 (如 movie.douban.com → douban.com)
    parts = domain.split(".")
    if len(parts) > 2:
        parent = ".".join(parts[-2:])
        if parent in DOMAIN_TEMPLATES:
            return DOMAIN_TEMPLATES[parent]
    return None


def learn_domain_template(domain: str, page_type: str, item_selector: str,
                          fields: dict, success: bool):
    """成功后积累域名模板 · 供下次同类域名用"""
    try:
        entry = {
            "ts": datetime.now().isoformat(),
            "domain": domain,
            "page_type": page_type,
            "item_selector": item_selector,
            "fields": fields,
            "success": success,
        }
        with open(DOMAIN_TEMPLATES_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _log_extract(entry: dict):
    """落监控 log"""
    try:
        with open(EXTRACT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ═══════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════
def extract(html: str, url: str = "", hint: str = None) -> dict:
    """
    统一抽取入口 · 返回结构化结果

    Args:
        html: 原始 HTML
        url: 页面 URL (用于查域名模板)
        hint: 用户提示 (list/detail/table/page)

    Returns:
        {
            "ok": bool,
            "page_type": str,
            "method": str,        # domain_template / auto_list / auto_detail / auto_table / generic / plain
            "items": list,        # list/table 页
            "fields": dict,       # detail 页
            "text": str,          # 兜底纯文本
            "n_items": int,
            "elapsed_ms": float,
            "html_in": int,
            "text_out": int,
            "compression_ratio": float,  # 压缩比 (越小越好)
            "token_savings_pct": float,  # 比全文省了 % token
        }
    """
    t0 = time.time()
    result = {
        "ok": False,
        "page_type": "unknown",
        "method": "none",
        "items": [],
        "fields": {},
        "text": "",
        "n_items": 0,
        "elapsed_ms": 0,
        "html_in": len(html),
        "text_out": 0,
        "compression_ratio": 1.0,
        "token_savings_pct": 0.0,
    }

    if not html or len(html) < 100:
        result["elapsed_ms"] = (time.time() - t0) * 1000
        return result

    # 尝试 lxml
    lh = _safe_import_lxml()
    if lh is None:
        # lxml 不可用 · 退回 text_content (用正则清标签)
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text).strip()
        result.update({
            "ok": bool(text),
            "method": "plain_regex",
            "text": text[:10000],
            "text_out": len(text),
            "elapsed_ms": (time.time() - t0) * 1000,
        })
        _log_extract({**result, "url": url, "ts": datetime.now().isoformat()})
        return result

    # 解析 DOM
    try:
        tree = lh.fromstring(html)
    except Exception:
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text).strip()
        result.update({
            "ok": bool(text),
            "method": "plain_regex_parse_fail",
            "text": text[:10000],
            "elapsed_ms": (time.time() - t0) * 1000,
        })
        _log_extract({**result, "url": url, "ts": datetime.now().isoformat()})
        return result

    # 检测页面类型
    page_type = hint or detect_page_type(tree, lh)
    domain = ""
    try:
        from urllib.parse import urlparse
        domain = urlparse(url).netloc.lower()
    except Exception:
        pass

    # L2 · 域名专属模板
    template = get_domain_template(url) if url else None
    if template and page_type == "list":
        items = extract_list(tree, lh, template)
        if items:
            result.update({
                "ok": True,
                "page_type": "list",
                "method": "domain_template",
                "items": items,
                "n_items": len(items),
                "text_out": len(json.dumps(items, ensure_ascii=False)),
                "elapsed_ms": (time.time() - t0) * 1000,
            })
            # 积累经验
            learn_domain_template(domain, "list", template["item_selector"],
                                  template.get("fields", {}), True)
            _log_extract({**result, "url": url, "ts": datetime.now().isoformat()})
            _calc_ratios(result)
            return result

    # L3 · 通用结构化抽取
    if page_type == "list":
        items = extract_list(tree, lh, None)
        if items:
            result.update({
                "ok": True,
                "page_type": "list",
                "method": "auto_list",
                "items": items,
                "n_items": len(items),
                "text_out": len(json.dumps(items, ensure_ascii=False)),
                "elapsed_ms": (time.time() - t0) * 1000,
            })
            learn_domain_template(domain, "list", "auto", {}, True)
            _log_extract({**result, "url": url, "ts": datetime.now().isoformat()})
            _calc_ratios(result)
            return result

    elif page_type == "table":
        items = extract_table(tree, lh)
        if items:
            result.update({
                "ok": True,
                "page_type": "table",
                "method": "auto_table",
                "items": items,
                "n_items": len(items),
                "text_out": len(json.dumps(items, ensure_ascii=False)),
                "elapsed_ms": (time.time() - t0) * 1000,
            })
            _log_extract({**result, "url": url, "ts": datetime.now().isoformat()})
            _calc_ratios(result)
            return result

    elif page_type == "detail":
        fields = extract_detail(tree, lh)
        if fields:
            result.update({
                "ok": True,
                "page_type": "detail",
                "method": "auto_detail",
                "fields": fields,
                "text_out": len(json.dumps(fields, ensure_ascii=False)),
                "elapsed_ms": (time.time() - t0) * 1000,
            })
            _log_extract({**result, "url": url, "ts": datetime.now().isoformat()})
            _calc_ratios(result)
            return result

    # L4 · 退回纯文本
    text = tree.text_content()
    text = re.sub(r'\s+', ' ', text).strip()
    result.update({
        "ok": bool(text),
        "page_type": page_type,
        "method": "auto_plain_fallback",
        "text": text[:10000],
        "text_out": len(text),
        "elapsed_ms": (time.time() - t0) * 1000,
    })
    _log_extract({**result, "url": url, "ts": datetime.now().isoformat()})
    _calc_ratios(result)
    return result


def _calc_ratios(result: dict):
    """计算压缩比和 token 节省率"""
    html_in = result.get("html_in", 1)
    text_out = result.get("text_out", 0)
    if html_in > 0:
        result["compression_ratio"] = round(text_out / html_in, 3)
        result["token_savings_pct"] = round((1 - text_out / html_in) * 100, 1)


# ═══════════════════════════════════════════════
# CLI 入口 (调试 + 对比)
# ═══════════════════════════════════════════════
def main():
    import argparse
    ap = argparse.ArgumentParser(description="lxml 结构化抽取引擎")
    ap.add_argument("--url", type=str, help="抓 URL 并抽取")
    ap.add_argument("--html-file", type=str, help="从本地 HTML 文件抽取")
    ap.add_argument("--hint", type=str, default=None, help="页面类型提示: list/detail/table")
    ap.add_argument("--stats", action="store_true", help="EXTRACT_LOG 统计")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    if args.stats:
        _cli_stats()
        return

    html = ""
    url = ""
    if args.url:
        url = args.url
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
            # 走韧性抓取: 限流(按域名) + 429/超时退避重试 + 正文缓存
            html = _fetch_html_resilient(url, headers, timeout=15)
        except Exception as e:
            print(f"fetch fail: {e}")
            sys.exit(1)
    elif args.html_file:
        html = Path(args.html_file).read_text(encoding="utf-8")
    else:
        # 测试模式: 用内置 sample HTML
        html = _SAMPLE_LIST_HTML
        url = "https://books.toscrape.com/"

    result = extract(html, url, hint=args.hint)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"═══ lxml 抽取结果 ═══")
        print(f"  method:    {result['method']}")
        print(f"  page_type: {result['page_type']}")
        print(f"  ok:        {result['ok']}")
        print(f"  items:     {result['n_items']}")
        print(f"  html_in:   {result['html_in']:,} chars")
        print(f"  text_out:  {result['text_out']:,} chars")
        print(f"  ratio:     {result['compression_ratio']}")
        print(f"  savings:   {result['token_savings_pct']}%")
        print(f"  elapsed:   {result['elapsed_ms']:.1f}ms")
        if result["items"]:
            print(f"\n  ── items (前 3) ──")
            for it in result["items"][:3]:
                print(f"    {json.dumps(it, ensure_ascii=False)[:200]}")
        if result["fields"]:
            print(f"\n  ── fields ──")
            for k, v in result["fields"].items():
                print(f"    {k}: {str(v)[:150]}")
        if result["text"] and not result["items"] and not result["fields"]:
            print(f"\n  ── text (前 500) ──")
            print(f"    {result['text'][:500]}")


def _cli_stats():
    """EXTRACT_LOG 统计"""
    if not EXTRACT_LOG.exists():
        print("无 EXTRACT_LOG 数据")
        return
    lines = EXTRACT_LOG.read_text(encoding="utf-8").strip().splitlines()
    total = len(lines)
    by_method = {}
    by_page_type = {}
    total_savings = 0
    total_elapsed = 0
    for line in lines:
        try:
            d = json.loads(line)
            m = d.get("method", "?")
            p = d.get("page_type", "?")
            by_method[m] = by_method.get(m, 0) + 1
            by_page_type[p] = by_page_type.get(p, 0) + 1
            total_savings += d.get("token_savings_pct", 0)
            total_elapsed += d.get("elapsed_ms", 0)
        except Exception:
            pass
    print(f"═══ EXTRACT_LOG · 共 {total} 条 ═══")
    print(f"\n  by method:")
    for k, v in sorted(by_method.items(), key=lambda x: -x[1]):
        print(f"    {k:<25} {v:>4}")
    print(f"\n  by page_type:")
    for k, v in sorted(by_page_type.items(), key=lambda x: -x[1]):
        print(f"    {k:<25} {v:>4}")
    if total:
        print(f"\n  avg savings: {total_savings/total:.1f}%")
        print(f"  avg elapsed: {total_elapsed/total:.1f}ms")


# 测试用 sample HTML (模拟 books.toscrape.com 列表)
_SAMPLE_LIST_HTML = """
<html><body>
<article class="product_pod">
  <h3><a href="/catalogue/a-light-in-the-attic_1000/index.html" title="A Light in the Attic">A Light in the Attic</a></h3>
  <p class="price_color">£51.77</p>
  <p class="availability in-stock">In stock</p>
  <p class="star-rating Three">***</p>
</article>
<article class="product_pod">
  <h3><a href="/catalogue/tipping-the-velvet_999/index.html" title="Tipping the Velvet">Tipping the Velvet</a></h3>
  <p class="price_color">£53.74</p>
  <p class="availability in-stock">In stock</p>
  <p class="star-rating One">*</p>
</article>
<article class="product_pod">
  <h3><a href="/catalogue/soumission_998/index.html" title="Soumission">Soumission</a></h3>
  <p class="price_color">£50.10</p>
  <p class="availability in-stock">In stock</p>
  <p class="star-rating One">*</p>
</article>
</body></html>
"""


if __name__ == "__main__":
    try:
        main()
    except Exception as _e:
        import traceback
        traceback.print_exc()
        # 内部 alert
        try:
            sys.path.insert(0, str(HERE))
            from alert_bus import notify
            notify("lxml_extractor", "ERROR", f"{type(_e).__name__}: {str(_e)[:200]}")
        except Exception:
            pass
