#!/usr/bin/env python3
"""
v13.60 · search_agent_orchestrator · 统一搜索 agent 编排
────────────────────────────────────────────────────────────────
用户 2026-07-13:「把所有工具都汇总·串联或并行合作·真正启动搜索 agent 变成
强大无比的·能够深度搜索的 agent 架构」

设计思路 (基于真跑对比数据):

    场景                        主战术                后备
    ──────                     ───────               ─────
    小站/API/文档              curl                  playwright
    反爬中等 (github/arxiv)    playwright (屏幕外)   firecrawl
    反爬强 (csdn/juejin)       browseract stealth    firecrawl
    反爬极强 (zhihu/xhs)       firecrawl (代理池)    browseract
    Google 搜索页              browseract           firecrawl
    自主发现类("找XX最火的3个") firecrawl.agent()    third_party_scanner
    列表页需点详情             browseract full/pw    click_and_read
    海量爬取(整站)             firecrawl crawl       playwright
    结构化提取(标题/价格/日期)  scrapegraph+raw       LLM 后处理

3 层策略:
  L1 · 情景分类 → 选主战术 · 秒级
  L2 · 主战术挂了 → 走后备 · 秒级
  L3 · 深度探索 → firecrawl.agent 自主搜 · 分钟级

真数据支撑 (2026-07-13 benchmark):
  - CSDN 首页: playwright 6s/6k · firecrawl 34s/45k · browseract 12s/27k
  - 知乎话题: playwright 6s/592 · firecrawl 1.4s/768 · browseract 5s/768
  - Firecrawl agent: "找 3 个火项目" 秒返 · 带真 star 数
"""
import os
import sys
import json
import re
import time
from datetime import datetime
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
V11 = os.path.join(BASE, "v11")
STRATEGY_LOG = os.path.join(V11, ".search_agent_strategy.jsonl")


# 反爬强度分级 · 真跑测过
DOMAIN_TIER = {
    # tier 3 · 反爬极强 · 优先 firecrawl (代理池)
    "zhihu.com": 3, "xiaohongshu.com": 3, "xhslink.com": 3,
    "weibo.com": 3, "douyin.com": 3,
    # tier 2 · 反爬强 · 优先 browseract stealth
    "csdn.net": 2, "juejin.cn": 2, "mp.weixin.qq.com": 2,
    "bilibili.com": 2, "36kr.com": 2,
    # v96i(2026-08-11) · 实证根因:GAIA 米氏方程题(NIH 1913 论文翻译)反复
    #   curl 拿到的是 reCAPTCHA 挑战页(21KB HTML 全是 recaptcha 关键词)，
    #   agent 换了 5+ 次 shell_exec(curl/urllib/不同 UA/不同镜像 URL)全部
    #   撞在这堵墙上，浪费预算还拿不到论文正文——根因是这些学术站域名
    #   压根没在 DOMAIN_TIER 里，被 classify_target 默认判成 tier 0(无反爬)
    #   直接走 curl，永远拿不到需要过 JS/人机验证挑战的真实页面。
    #   NCBI/PMC/EuropePMC 对自动化请求会触发 reCAPTCHA，需要真实浏览器
    #   (playwright/browseract)才能过验证拿到渲染后的正文。
    "ncbi.nlm.nih.gov": 2, "pmc.ncbi.nlm.nih.gov": 2,
    "europepmc.org": 2, "pubmed.ncbi.nlm.nih.gov": 2,
    # tier 1 · 反爬中 · playwright 够
    "github.com": 1, "arxiv.org": 1, "medium.com": 1,
    # tier 0 · 无反爬 · curl 秒返
}

# v13.95(2026-08-12) · 订阅墙域名表
# 这些域名被直接 fetch 时命中订阅墙/登录墙，需要走 OA 绕路策略
# (Unpaywall → OpenAlex → Semantic Scholar → Sci-Hub)
# 而不是浪费 browseract/firecrawl 轮次撞墙
_PAYWALL_DOMAINS = {
    # Elsevier/ScienceDirect
    "sciencedirect.com", "linkinghub.elsevier.com", "elsevier.com",
    # Springer/Nature
    "link.springer.com", "springer.com", "nature.com", "springerlink.com",
    # Wiley
    "onlinelibrary.wiley.com", "wiley.com",
    # Taylor & Francis
    "tandfonline.com",
    # JSTOR
    "jstor.org",
    # IEEE
    "ieeexplore.ieee.org",
    # ACM
    "dl.acm.org",
    # Oxford Academic
    "academic.oup.com",
    # Cambridge
    "cambridge.org",
    # ACS
    "pubs.acs.org",
    # APS
    "journals.aps.org",
    # Sage
    "journals.sagepub.com",
}

# 订阅墙内容特征（页面里有这些词说明撞墙了）
_PAYWALL_SIGNALS = [
    "Subscribe to view", "Subscribe or purchase", "Purchase access",
    "Get access", "Full text available to subscribers",
    "Please sign in", "Login required", "Access required",
    "This article is available to", "institutional access",
    "PDF only for subscribers", "Access denied", "403 Forbidden",
    "你需要订阅", "需要登录", "付费查看",
]


def _is_paywall_domain(url: str) -> bool:
    """判断 URL 是否属于已知订阅墙域名"""
    domain = urlparse(url).netloc.lower()
    return any(pw in domain for pw in _PAYWALL_DOMAINS)


def _detect_paywall_content(content: str) -> bool:
    """检测已抓取内容是否撞了订阅墙（内容很短或含订阅墙关键词）"""
    if not content:
        return True
    if len(content) < 500:
        content_lower = content.lower()
        return any(sig.lower() in content_lower for sig in _PAYWALL_SIGNALS)
    return False


def _domain_migration_url(url: str) -> str:
    """v15.5(2026-08-17 · 用户策略) 同站迁移换域名换算——通用规则非硬编码站:
    2010s 大量个人博客从 WordPress 迁到 Blogger·同子域同路径直存。
    例: foo.wordpress.com/2012/12/27/best-of/ → foo.blogspot.com/同路径。
    Blogspot 基本不反爬·比走 Wayback 快得多。不匹配返回 ""。
    """
    m = re.match(r"^(https?://)([a-z0-9-]+)\.wordpress\.com(/.*)?$",
                 url, re.IGNORECASE)
    if m:
        return f"{m.group(1)}{m.group(2)}.blogspot.com{m.group(3) or ''}"
    return ""


def _paywall_bypass_ladder(url: str, timeout: int = 12) -> dict:
    """通用绕墙兜底阶梯(2026-08-14)·OA/DOI 绕路失败后的最后手段·全通用非硬编码域名:
      ⓪同站迁移换域名: WordPress→Blogspot 同路径直抓(2010s 博客常见迁移·不反爬)。
      ①Googlebot UA 伪装:付费墙常对搜索引擎爬虫放行(求收录)·冒充 Googlebot 直抓。
      ②archive.org 快照:取该 URL 的历史存档(墙外快照·含撞墙前的全文)。
    返回 {ok, content, via} 或 {ok:False}。任何付费墙/死链 URL 都可用·不针对某题/某域名。
    """
    import urllib.request, urllib.parse, json as _json
    # ⓪ 同站迁移换域名(WordPress→Blogspot)
    _mig = _domain_migration_url(url)
    if _mig:
        try:
            req = urllib.request.Request(_mig, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "ignore")
            if raw and len(raw) > 800:
                return {"ok": True, "content": raw, "via": "wp_to_blogspot"}
        except Exception:
            pass
    # ① Googlebot UA 伪装
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
            "X-Forwarded-For": "66.249.66.1",  # Googlebot IP 段·部分墙据此放行
            "Referer": "https://www.google.com/",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "ignore")
        # 撞墙检测:内容够长且不含订阅墙信号才算成功绕过
        if raw and len(raw) > 800 and not _detect_paywall_content(raw[:500]):
            return {"ok": True, "content": raw, "via": "googlebot_ua"}
    except Exception:
        pass
    # ② archive.org 最新快照
    try:
        api = "https://archive.org/wayback/available?" + urllib.parse.urlencode({"url": url})
        req = urllib.request.Request(api, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            meta = _json.loads(resp.read().decode("utf-8", "ignore"))
        snap = (((meta.get("archived_snapshots") or {}).get("closest") or {}).get("url"))
        if snap:
            req2 = urllib.request.Request(snap, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req2, timeout=timeout) as resp2:
                snap_raw = resp2.read().decode("utf-8", "ignore")
            if snap_raw and len(snap_raw) > 800:
                return {"ok": True, "content": snap_raw, "via": "archive_org"}
    except Exception:
        pass
    return {"ok": False}


def _extract_doi_from_url(url: str) -> str:
    """从 URL 里提取 DOI"""
    # doi.org/10.xxxx/xxxxx
    doi_match = re.search(r'10\.[0-9]{4,}[^\s"<>\s]+', url)
    if doi_match:
        return doi_match.group(0).rstrip('/),"')
    # /pii/ 格式（Elsevier）
    pii_match = re.search(r'/pii/([A-Z0-9]+)', url, re.IGNORECASE)
    if pii_match:
        return f"pii:{pii_match.group(1)}"
    return ""


def fetch_oa_by_doi(doi: str, timeout: int = 8) -> dict:
    """根据 DOI 查 Open Access 全文

    优先级: Unpaywall API → OpenAlex → Semantic Scholar → Sci-Hub
    全部无门槛·免费·真实可用

    返回: {ok, via, doi, content, oa_url?, is_abstract_only?}
    """
    import urllib.request, urllib.parse as _up

    # v13.95 · 探测系统代理(macOS scutil)并注入给 urllib
    def _make_opener():
        _proxy_url = None
        try:
            import subprocess as _sp
            _po = _sp.run(["scutil", "--proxy"], capture_output=True, text=True, timeout=3)
            _po_out = _po.stdout or ""
            import re as _re_s
            if _re_s.search(r"HTTPSEnable\s*:\s*1", _po_out):
                _h = _re_s.search(r"HTTPSProxy\s*:\s*(\S+)", _po_out)
                _p = _re_s.search(r"HTTPSPort\s*:\s*(\d+)", _po_out)
                if _h and _p:
                    _proxy_url = f"http://{_h.group(1)}:{_p.group(1)}"
        except Exception:
            pass
        if _proxy_url:
            return urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": _proxy_url, "https": _proxy_url})
            )
        return urllib.request.build_opener()

    _opener = _make_opener()

    def _http_get(url, t=timeout):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "meyo-research/1.0 (mailto:seanliii@meituan.com)"}
            )
            with _opener.open(req, timeout=t) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            return None

    # 清洗 DOI
    clean_doi = doi.strip().lstrip("doi:").lstrip("DOI:").strip()
    if clean_doi.startswith("pii:"):
        # PII 需要先查 CrossRef 拿真 DOI
        pii = clean_doi[4:]
        cr = _http_get(f"https://api.crossref.org/works?query={pii}&rows=1")
        if cr:
            items = (cr.get("message") or {}).get("items") or []
            if items:
                clean_doi = items[0].get("DOI", "") or clean_doi

    if not clean_doi or not clean_doi.startswith("10."):
        return {"ok": False, "reason": "无法提取有效 DOI"}

    # ① Unpaywall API（最权威 OA 源，有 polite pool）
    uw_url = f"https://api.unpaywall.org/v2/{_up.quote(clean_doi)}?email=seanliii@meituan.com"
    uw = _http_get(uw_url)
    if uw and uw.get("is_oa"):
        best = uw.get("best_oa_location") or {}
        oa_url = best.get("url_for_pdf") or best.get("url") or ""
        if oa_url:
            try:
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                from web_access_bridge import WebAccess
                r = WebAccess().read_url(oa_url, prefer="curl")
                content = (r or {}).get("content", "")
                if content and len(content) > 500:
                    return {"ok": True, "via": "unpaywall", "doi": clean_doi,
                            "oa_url": oa_url, "content": content}
            except Exception:
                pass

    # ② OpenAlex（全文 URL + abstract）
    oa_api = f"https://api.openalex.org/works/doi:{_up.quote(clean_doi)}?email=seanliii@meituan.com"
    oa = _http_get(oa_api)
    if oa:
        oa_url = (oa.get("open_access") or {}).get("oa_url") or ""
        if oa_url:
            try:
                from web_access_bridge import WebAccess
                r = WebAccess().read_url(oa_url, prefer="curl")
                content = (r or {}).get("content", "")
                if content and len(content) > 500:
                    return {"ok": True, "via": "openalex_oa", "doi": clean_doi,
                            "oa_url": oa_url, "content": content}
            except Exception:
                pass
        # 没有 OA 全文，但 abstract 有价值
        inv = oa.get("abstract_inverted_index") or {}
        if inv:
            word_pos = sorted([(pos, w) for w, plist in inv.items() for pos in plist])
            abstract = " ".join(w for _, w in word_pos[:300])
            return {"ok": True, "via": "openalex_abstract", "doi": clean_doi,
                    "content": f"Title: {oa.get('title','')}\n\nAbstract: {abstract}",
                    "is_abstract_only": True}

    # ③ Semantic Scholar（abstract + OA PDF）
    ss_url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{_up.quote(clean_doi)}?fields=title,abstract,openAccessPdf"
    ss = _http_get(ss_url)
    if ss:
        pdf = (ss.get("openAccessPdf") or {}).get("url") or ""
        if pdf:
            try:
                from web_access_bridge import WebAccess
                r = WebAccess().read_url(pdf, prefer="curl")
                content = (r or {}).get("content", "")
                if content and len(content) > 500:
                    return {"ok": True, "via": "semantic_scholar_pdf",
                            "doi": clean_doi, "content": content}
            except Exception:
                pass
        abstract = ss.get("abstract") or ""
        if abstract:
            return {"ok": True, "via": "semantic_scholar_abstract", "doi": clean_doi,
                    "content": f"Title: {ss.get('title','')}\n\nAbstract: {abstract}",
                    "is_abstract_only": True}

    # ④ Sci-Hub fallback（高命中率，合规风险自行考量）
    for scihub_base in ["https://sci-hub.se", "https://sci-hub.st"]:
        try:
            scihub_url = f"{scihub_base}/{clean_doi}"
            from web_access_bridge import WebAccess
            r = WebAccess().read_url(scihub_url, prefer="curl")
            content = (r or {}).get("content", "")
            if content and len(content) > 1000 and "pdf" in content.lower():
                return {"ok": True, "via": "scihub", "doi": clean_doi, "content": content}
        except Exception:
            continue

    return {"ok": False, "reason": f"DOI {clean_doi} 所有 OA 路径均无结果", "doi": clean_doi}


# v13.94 · 用户抓「pubchem.ncbi.nlm.nih.gov 被 F4 guard/classify_target 误伤」
# 根因: "ncbi.nlm.nih.gov" in domain 是子串匹配·pubchem.ncbi.nlm.nih.gov
# 域名里包含这段字符串会被一起命中 tier2。但 PubChem 是 NCBI 旗下独立子系统·
# 走自己的 PUG-REST 结构化 JSON API(不是给人看的网页)，不会触发 NCBI/PMC
# 那种给浏览器用户看的 reCAPTCHA 挑战页——用 curl 直连秒回，被误判成
# tier2 强制走 playwright/browseract 反而更慢更没必要。这里在 tier 判定前
# 先排除已知"同父域名但走独立结构化 API、无反爬"的子域名白名单。
_SUBDOMAIN_TIER_OVERRIDE = {
    "pubchem.ncbi.nlm.nih.gov": 0,  # 结构化 REST API，非网页，无反爬
}


def classify_target(url: str) -> dict:
    """判断 URL 反爬等级·推荐主战术"""
    domain = urlparse(url).netloc.lower()
    for override_host, override_tier in _SUBDOMAIN_TIER_OVERRIDE.items():
        if override_host in domain:
            return {"tier": override_tier, "strategy": "curl",
                    "reason": f"{domain} 结构化 API·白名单免反爬分级"}
    tier = 0
    for k, v in DOMAIN_TIER.items():
        if k in domain:
            tier = max(tier, v)

    # 特殊 pattern
    if "google.com/search" in url or "bing.com/search" in url:
        return {"tier": 3, "strategy": "browseract", "reason": "搜索引擎结果页"}

    if tier == 3:
        return {"tier": 3, "strategy": "firecrawl", "reason": f"{domain} 反爬极强"}
    if tier == 2:
        return {"tier": 2, "strategy": "browseract", "reason": f"{domain} 反爬强"}
    if tier == 1:
        return {"tier": 1, "strategy": "playwright", "reason": f"{domain} 反爬中"}
    return {"tier": 0, "strategy": "curl", "reason": "无反爬"}


def smart_fetch(url: str, prefer: str = None,
                use_cache: bool = True) -> dict:
    """智能抓取 · 自动分类+选战术
    v13.87 更新 (抄 wigolo 亮点):
    - 亮点 5 · 先查本地 search_cache (24h TTL)·命中 0 网络
    - 亮点 2 · http-first 三挡降级: curl → 主战术 → 后备
      (原本一步走 wa.read_url · 现在 tier<=1 时先试 curl 快路径)
    - 亮点 1 · 返回值加 completeness 字段
    - 亮点 4 · 走 fetcher_health.health_log
    v13.95 · 订阅墙自动绕路:
    - 订阅墙域名（ScienceDirect/Springer/Wiley/IEEE 等）命中就直接走 OA 路径
    - 不再浪费 browseract/firecrawl 轮次撞墙拿一堆"请订阅"内容
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    # v13.95 · 订阅墙域名快速检测 + OA 绕路
    # 在任何 fetch 动作之前先判：已知订阅墙域名 → 直接走 OA 路径
    if not prefer and _is_paywall_domain(url):
        t0_pw = time.time()
        doi = _extract_doi_from_url(url)
        oa_result = None
        if doi:
            try:
                oa_result = fetch_oa_by_doi(doi)
            except Exception as _e:
                oa_result = {"ok": False, "reason": str(_e)[:100]}
        else:
            # 没有 DOI 但是订阅墙域名·试着用 Unpaywall metadata API 按 URL 查
            try:
                import urllib.request, urllib.parse as _up
                # Unpaywall 支持直接按 URL/DOI 解析
                # 先抓 page 拿到 DOI meta tag，再查
                from web_access_bridge import WebAccess
                _probe = WebAccess().read_url(url, prefer="curl")
                _probe_content = (_probe or {}).get("content", "")
                # 尝试从页面内容里提取 DOI
                _doi2 = _extract_doi_from_url(_probe_content)
                if _doi2:
                    oa_result = fetch_oa_by_doi(_doi2)
            except Exception:
                pass
        if oa_result and oa_result.get("ok"):
            content = oa_result.get("content", "")
            dt_pw = round(time.time() - t0_pw, 1)
            via_tag = oa_result.get("via", "oa")
            is_abstract = oa_result.get("is_abstract_only", False)
            abstract_note = "【仅摘要·无全文 OA】" if is_abstract else ""
            return {
                "url": url,
                "planned_strategy": "oa_bypass",
                "tier": -1,  # 特殊标记：OA 绕路
                "actual_via": f"oa:{via_tag}",
                "reason": f"订阅墙域名·自动走 OA 绕路({via_tag}){abstract_note}",
                "ok": True,
                "content_len": len(content),
                "time_s": dt_pw,
                "content": content,
                "completeness": "abstract" if is_abstract else "full",
                "cached": False,
                "oa_doi": oa_result.get("doi", doi),
                "oa_url": oa_result.get("oa_url", ""),
            }
        # OA 失败 → 2026-08-14·通用绕墙兜底阶梯(Googlebot UA + archive.org)再试一次·
        #   不再一失败就放弃。治付费墙题(0b260a57 类)撞墙无兜底。
        _bp = _paywall_bypass_ladder(url)
        if _bp.get("ok") and _bp.get("content"):
            _bc = _bp["content"]
            return {
                "url": url,
                "planned_strategy": "paywall_bypass",
                "tier": -1,
                "actual_via": f"bypass:{_bp.get('via')}",
                "reason": f"OA无果·通用绕墙兜底({_bp.get('via')})拿到内容",
                "ok": True,
                "content_len": len(_bc),
                "time_s": round(time.time() - t0_pw, 1),
                "content": _bc,
                "completeness": "full",
                "cached": False,
            }
        # OA + 绕墙兜底都失败 → 返回失败
        doi_hint = f"(DOI: {doi})" if doi else "(未找到 DOI)"
        return {
            "url": url,
            "planned_strategy": "oa_bypass",
            "tier": -1,
            "actual_via": "oa_failed",
            "reason": f"订阅墙域名{doi_hint}·OA/Googlebot/archive 均无结果·此文献无免费版本",
            "ok": False,
            "content_len": 0,
            "time_s": round(time.time() - t0_pw if 't0_pw' in dir() else 0, 1),
            "content": "",
            "completeness": "none",
            "cached": False,
        }

    from web_access_bridge import WebAccess
    wa = WebAccess()
    t0 = time.time()
    plan = classify_target(url)
    strategy = prefer or plan["strategy"]

    # v13.87 亮点 5 · cache-first
    cached = None
    if use_cache:
        try:
            from search_cache import cache_get
            cached = cache_get(url)
        except Exception:
            cached = None
    if cached and cached.get("content"):
        dt = time.time() - t0
        result = {
            "url": url,
            "planned_strategy": plan["strategy"],
            "tier": plan["tier"],
            "reason": plan["reason"],
            "actual_via": "cache",
            "ok": True,
            "content_len": cached["content_len"],
            "time_s": round(dt, 3),
            "content": cached["content"],
            "completeness": cached["completeness"],
            "cached": True,
        }
        # 缓存命中也记 log · 方便统计命中率
        log_rec = {k: v for k, v in result.items() if k != "content"}
        log_rec["ts"] = datetime.now().isoformat()
        try:
            with open(STRATEGY_LOG, "a") as f:
                f.write(json.dumps(log_rec, ensure_ascii=False) + "\n")
        except Exception:
            pass
        return result

    # v13.94(2026-08-12) · 先试 search_fabric_fetch L2 梯度
    # httpx→curl_cffi 更轻量·不需要 CDP 进程·tier<=2 都先试
    used_fast_probe = False
    r = None
    if not prefer:
        try:
            from search_fabric_fetch import fetch_with_gradient
            _fab_r = fetch_with_gradient(url, max_tier=2, timeout=10)
            if _fab_r.get("ok") and len(_fab_r.get("content", "")) > 300:
                # L2 拿到货·包装成 web_access_bridge 兼容格式
                r = {
                    "ok": True,
                    "content": _fab_r["content"],
                    "via": _fab_r.get("via", "fabric_l2"),
                    "url": url,
                }
                used_fast_probe = True
        except Exception:
            pass  # fail-safe·继续走老路

    # v13.87 亮点 2 · http-first 快路径 (L2 没拿到货时才走)
    # tier<=1 (无/中反爬) 且未强指定 prefer 时 · 先试轻量 curl
    if r is None:
        if plan["tier"] <= 1 and not prefer:
            try:
                probe = wa.read_url(url, prefer="curl")
                if (probe and probe.get("ok") and
                        len(probe.get("content", "")) > 800):
                    # http-first 拿到货 · 不必上浏览器
                    r = probe
                    used_fast_probe = True
                else:
                    r = wa.read_url(url, prefer=strategy)
            except Exception:
                r = wa.read_url(url, prefer=strategy)
        else:
            r = wa.read_url(url, prefer=strategy)

    dt = time.time() - t0
    content = r.get("content", "")
    # v13.87 亮点 1 · content_completeness
    try:
        from search_cache import score_completeness, cache_put
        completeness = score_completeness(content)
    except Exception:
        completeness = ("full" if len(content) > 1000
                        else "partial" if len(content) >= 200
                        else "shell")
        cache_put = None

    result = {
        "url": url,
        "planned_strategy": plan["strategy"],
        "tier": plan["tier"],
        "actual_via": ("curl_fast" if used_fast_probe
                       else r.get("via")),
        "reason": plan["reason"],
        "ok": r.get("ok"),
        "content_len": len(content),
        "time_s": round(dt, 1),
        "content": content,
        "completeness": completeness,   # 亮点 1
        "cached": False,
    }

    # v13.87 亮点 5 · 落 cache (只对 partial/full 落 · shell 不污染)
    if cache_put and r.get("ok") and completeness != "shell":
        try:
            cache_put(url, content, "smart_fetch",
                      {"strategy": strategy,
                       "actual_via": result["actual_via"]})
        except Exception:
            pass

    # 落策略日志
    log_rec = {k: v for k, v in result.items() if k != "content"}
    log_rec["ts"] = datetime.now().isoformat()
    try:
        with open(STRATEGY_LOG, "a") as f:
            f.write(json.dumps(log_rec, ensure_ascii=False) + "\n")
    except Exception:
        pass

    # v13.87 亮点 4 · 通用健康度
    try:
        from fetcher_health import health_log
        health_log("smart_fetch", {
            "ok": bool(r.get("ok")),
            "count": 1 if r.get("ok") else 0,
            "elapsed_s": round(dt, 2),
            "unix_ts": int(time.time()),
            "url_domain": urlparse(url).netloc,
            "completeness": completeness,
            "via": result["actual_via"],
            "err": ("" if r.get("ok") else str(r.get("reason", ""))[:120]),
        })
    except Exception:
        pass

    # v15.5(2026-08-17 · 用户策略): 通用失败兜底——抓不到/只有壳时走
    # 【迁移换域名(WordPress→Blogspot)→Googlebot→archive.org】阶梯·不再直接交失败。
    # 治 Q3 型死链: 老博客文章主站已死/反爬·但 Blogger 镜像或存档里活着。
    if (not r.get("ok")) or completeness == "shell" or len(content) < 300:
        _bp = _paywall_bypass_ladder(url)
        if _bp.get("ok") and _bp.get("content"):
            _bc = _bp["content"]
            try:
                from search_cache import score_completeness as _sc, cache_put as _cp
                _bp_comp = _sc(_bc)
                if _cp:
                    _cp(url, _bc, "bypass_ladder", {"via": _bp.get("via")})
            except Exception:
                _bp_comp = "full" if len(_bc) > 1000 else "partial"
            return {
                "url": url,
                "planned_strategy": "bypass_ladder",
                "tier": -1,
                "actual_via": f"bypass:{_bp.get('via')}",
                "reason": f"主路失败·兜底阶梯({_bp.get('via')})拿到内容",
                "ok": True,
                "content_len": len(_bc),
                "time_s": round(time.time() - t0, 1),
                "content": _bc,
                "completeness": _bp_comp,
                "cached": False,
            }

    return result


def agent_search(prompt: str, use_firecrawl: bool = True) -> dict:
    """自主搜:一句话描述·让 firecrawl agent 帮找
    用户原文'找到 XX 最火的 3 个 GitHub 项目'"""
    if use_firecrawl and os.environ.get("FIRECRAWL_API_KEY"):
        try:
            from firecrawl import Firecrawl
            app = Firecrawl(api_key=os.environ["FIRECRAWL_API_KEY"])
            t0 = time.time()
            r = app.agent(prompt=prompt)
            data = r.data if hasattr(r, 'data') else r
            return {
                "ok": True, "via": "firecrawl_agent",
                "prompt": prompt, "data": data,
                "time_s": round(time.time() - t0, 1),
            }
        except Exception as e:
            return {"ok": False, "reason": f"firecrawl_exc: {e}"}
    return {"ok": False, "reason": "no firecrawl key"}


def structured_extract(url: str, prompt: str) -> dict:
    """深度提取:抓页面+LLM 结构化"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from web_access_bridge import WebAccess
    wa = WebAccess()
    return wa.scrapegraph_structured_extract(url, prompt, is_url=True)


def parallel_multi_source(query: str, sources: list = None) -> list:
    """并行搜多源 · 汇总
    sources: URLs 或 平台名 list
    v13.87 · 抄 wigolo 亮点 3 · 每条结果加 evidence_score
    """
    import concurrent.futures
    if sources is None:
        # v13.87.39 · 用户 2026-07-23 抓「about:blank 一堆」
        # 之前拼死路径 csdn/searchresults + juejin/search + zhihu/search
        # zhihu 反爬 · juejin SPA · csdn 部分反爬 → CDP 留 blank
        # 改 · 走 Google site:xxx dork (借 Google 找真存在的 URL)
        try:
            from friday_universal import search_google
            # v13.88.9L-11 · 上总线 (google site: dork 有意直调·补 log_search 求可见)
            try:
                import agent_comm as _ac
                _ac.log_search("orchestrator_dork", query,
                               source="google:site:csdn/juejin/zhihu", trigger="dork")
            except Exception:
                pass
            _hits = []
            for domain in ["csdn.net", "juejin.cn", "zhihu.com"]:
                r = search_google(f"{query} site:{domain}", top_k=3)
                if r:
                    _hits.extend(r)
            return _hits
        except Exception:
            # fallback · 老硬拼 (至少不返 about:blank · 返 error)
            sources = [
                f"https://blog.csdn.net/",  # 只回首页 · 不直搜
                f"https://juejin.cn/",
                f"https://www.zhihu.com/",
            ]
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(smart_fetch, u): u for u in sources}
        for f in concurrent.futures.as_completed(futs, timeout=60):
            try:
                results.append(f.result())
            except Exception as e:
                results.append({"url": futs[f], "ok": False,
                                "reason": str(e)[:200]})
    # v13.87 亮点 3 · evidence_score · 排序 · 让 LLM judge 拿分数排序
    for r in results:
        r["evidence_score"] = score_evidence(r, query)
    results.sort(key=lambda x: -x.get("evidence_score", {}).get("final", 0))
    return results


# v13.87 · 抄 wigolo 亮点 3 · evidence 分层打分
# wigolo 用 rrf/domain/lexical/consensus/rare_terms/cross_encoder 六层
# 我们轻量版: 4 层 · 无 rerank 模型依赖
_DOMAIN_QUALITY = {
    # tier · 越高越好
    "github.com": 1.0, "arxiv.org": 1.0, "stackoverflow.com": 0.95,
    "mdn.io": 0.9, "developer.mozilla.org": 0.9,
    "juejin.cn": 0.85, "sspai.com": 0.85,
    "zhihu.com": 0.8, "xiaohongshu.com": 0.8,
    "mp.weixin.qq.com": 0.8, "sogou.com": 0.6,
    "csdn.net": 0.75, "36kr.com": 0.75, "medium.com": 0.85,
    "baidu.com": 0.4, "zhidao.baidu.com": 0.3,
}


def score_evidence(hit: dict, query: str = "") -> dict:
    """轻量版 evidence_score · 抄 wigolo 亮点 3
    - completeness_boost: shell 0.1 · partial 0.5 · full 1.0
    - domain_quality: 查表·默认 0.5
    - lexical_alignment: query 词在 content 出现比例 (0-1)
    - recency: 无时间数据默认 0.5
    final = 加权和 (可调) · 满分 1.0
    """
    content = hit.get("content", "") or ""
    url = hit.get("url", "") or ""
    comp = hit.get("completeness", "shell")

    comp_score = {"shell": 0.1, "partial": 0.5, "full": 1.0}.get(comp, 0.3)

    # domain
    domain = urlparse(url).netloc.lower()
    dq = 0.5
    for k, v in _DOMAIN_QUALITY.items():
        if k in domain:
            dq = v
            break

    # lexical
    lex = 0.0
    if query and content:
        q_terms = [t for t in re.split(r'\s+', query.lower()) if len(t) > 1]
        if q_terms:
            hits = sum(1 for t in q_terms if t in content.lower())
            lex = hits / len(q_terms)

    # recency: 有 publish_time 且是 2025+ 才加分
    rec = 0.5
    pt = str(hit.get("meta", {}).get("publish_time", ""))
    if pt.startswith(("2026", "2025")):
        rec = 1.0
    elif pt.startswith(("2024", "2023")):
        rec = 0.7

    # 加权和
    final = (comp_score * 0.35 + dq * 0.30 + lex * 0.25 + rec * 0.10)
    return {
        "final": round(final, 3),
        "components": {
            "completeness_boost": round(comp_score, 3),
            "domain_quality": round(dq, 3),
            "lexical_alignment": round(lex, 3),
            "recency": round(rec, 3),
        },
        "explanation": (
            f"comp={comp_score:.2f} dom={dq:.2f} "
            f"lex={lex:.2f} rec={rec:.2f}"
        ),
    }


def score_evidence_v2(hits_by_engine: dict, query: str = "") -> list:
    """v13.88.58 · 2026-07-28 · wigolo 5 维完整评分
    用户 07-28 抓「缺 rrf + engine consensus 两维·干」

    hits_by_engine: {"zh": [hit1, hit2, ...], "google": [...], ...}
                    每 hit 内部 rank 已按引擎自己排序

    5 维:
      1. completeness · shell/partial/full → 0.1/0.5/1.0
      2. domain_quality · 查 _DOMAIN_QUALITY 表
      3. lexical_alignment · query 词在 content 命中率
      4. rrf · reciprocal rank fusion · 1/(k+rank) 跨引擎累加 (k=60 标准)
      5. engine_consensus · 同 URL 被 N 个引擎命中 / 总引擎数 (0-1)

    返回: [{"hit": hit, "final": 0.x, "components": {...}, "engines": [...]}]
          按 final 降序
    """
    from collections import defaultdict
    K_RRF = 60
    n_engines = len(hits_by_engine)

    # 1. URL 归一化 & 跨引擎聚合 (同 URL 收进一个 record)
    url_to_record = {}
    for engine, hits in hits_by_engine.items():
        for rank, h in enumerate(hits, start=1):
            u = (h.get("url") or "").strip()
            if not u:
                continue
            rec = url_to_record.setdefault(u, {
                "hit": h, "engines": [], "ranks": {},
            })
            rec["engines"].append(engine)
            rec["ranks"][engine] = rank
            # 保留 content 更长的那份
            if len(h.get("content", "")) > len(rec["hit"].get("content", "")):
                rec["hit"] = h

    # 2. 打 5 维
    scored = []
    for u, rec in url_to_record.items():
        h = rec["hit"]
        content = h.get("content", "") or ""
        comp = h.get("completeness", "shell")
        comp_score = {"shell": 0.1, "partial": 0.5, "full": 1.0}.get(comp, 0.3)

        # domain
        domain = urlparse(u).netloc.lower()
        dq = 0.5
        for k, v in _DOMAIN_QUALITY.items():
            if k in domain:
                dq = v
                break

        # lexical
        lex = 0.0
        if query and content:
            q_terms = [t for t in re.split(r'\s+', query.lower()) if len(t) > 1]
            if q_terms:
                lex = sum(1 for t in q_terms if t in content.lower()) / len(q_terms)

        # rrf: 跨引擎 rank 倒数累加·归一化到 0-1
        rrf_raw = sum(1.0 / (K_RRF + r) for r in rec["ranks"].values())
        # 理论最大 = n_engines * 1/(K_RRF+1) · 归一
        rrf_max = n_engines / (K_RRF + 1)
        rrf = min(1.0, rrf_raw / rrf_max) if rrf_max > 0 else 0.0

        # engine consensus
        consensus = len(rec["engines"]) / max(1, n_engines)

        # 5 维加权 · rrf/consensus 权重给足 (差异化亮点)
        final = (comp_score * 0.25 +
                 dq * 0.20 +
                 lex * 0.15 +
                 rrf * 0.20 +
                 consensus * 0.20)

        scored.append({
            "hit": h, "url": u,
            "engines": rec["engines"],
            "final": round(final, 3),
            "components": {
                "completeness": round(comp_score, 3),
                "domain_quality": round(dq, 3),
                "lexical_alignment": round(lex, 3),
                "rrf": round(rrf, 3),
                "engine_consensus": round(consensus, 3),
            },
        })

    scored.sort(key=lambda x: -x["final"])
    return scored


def deep_search(topic: str) -> dict:
    """深度搜索:自主搜 + 结构化提取 + 汇总
    这是给用户看的高层 API · 一句话搞定"""
    result = {"topic": topic, "ts": datetime.now().isoformat()}

    # 步 1 · Firecrawl agent 自主搜发现
    print(f"  [1/3] agent 自主搜索...")
    a = agent_search(f"找到 2026 年关于「{topic}」讨论最热的 3 个源 (URL)")
    result["agent_hits"] = a

    # 步 2 · 从 agent 结果提 URL · 并行深读
    urls_to_deep = []
    if a.get("ok"):
        data_str = str(a.get("data", ""))
        urls_to_deep = re.findall(r'https?://[^\s\'"]+', data_str)[:3]
    if not urls_to_deep:
        # fallback · 用 topic 造 URL
        # v13.87.39 · 用户抓「blank 一堆」· 老硬拼 csdn/juejin search 打不开
        # 改 · 走 Google site:xxx 找真存在页
        urls_to_deep = []
        try:
            from friday_universal import search_google
            # v13.88.9L-11 · 上总线 (google site: dork 有意直调·补 log_search 求可见)
            try:
                import agent_comm as _ac
                _ac.log_search("orchestrator_dork", topic,
                               source="google:site:csdn/juejin", trigger="dork")
            except Exception:
                pass
            for domain in ["blog.csdn.net", "juejin.cn"]:
                r = search_google(f"{topic} site:{domain}", top_k=2)
                for h in (r or [])[:2]:
                    if h.get("url"):
                        urls_to_deep.append(h["url"])
        except Exception:
            pass
        if not urls_to_deep:
            urls_to_deep = ["https://blog.csdn.net/", "https://juejin.cn/"]
    print(f"  [2/3] 并行深读 {len(urls_to_deep)} 个源...")
    result["deep_reads"] = parallel_multi_source(topic, urls_to_deep)

    # 步 3 · 结构化提取(如果内容够多)
    ok_sources = [r for r in result["deep_reads"]
                  if r.get("ok") and r.get("content_len", 0) > 500]
    if ok_sources:
        print(f"  [3/3] 结构化提取...")
        try:
            source_url = ok_sources[0]["url"]
            r_struct = structured_extract(
                source_url,
                f"从内容提取:关于「{topic}」的 3 个具体产品名/工具名/方法·和每个的 1 句描述"
            )
            result["structured"] = r_struct
        except Exception as e:
            result["structured_err"] = str(e)[:200]

    return result


if __name__ == "__main__":
    try:
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("cmd", choices=["classify", "smart", "agent", "deep", "parallel"])
        ap.add_argument("target")
        ap.add_argument("--prompt", default="")
        args = ap.parse_args()
        if args.cmd == "classify":
            print(json.dumps(classify_target(args.target), ensure_ascii=False, indent=2))
        elif args.cmd == "smart":
            r = smart_fetch(args.target)
            r.pop("content", None)
            print(json.dumps(r, ensure_ascii=False, indent=2))
        elif args.cmd == "agent":
            print(json.dumps(agent_search(args.target), ensure_ascii=False, indent=2, default=str))
        elif args.cmd == "deep":
            print(json.dumps(deep_search(args.target), ensure_ascii=False, indent=2, default=str))
        elif args.cmd == "parallel":
            r = parallel_multi_source(args.target)
            for x in r: x.pop("content", None)
            print(json.dumps(r, ensure_ascii=False, indent=2))
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise
