"""search_fabric_verticals.py · 2026-08-12 · L1 垂直联邦注册表

把 8 个已有插件 + 免费官方 API 注册进 search_fabric 语义路由。
「接线·不改路由代码·新源接入只需在这里 register」

已有插件 (你系统里建了没接的老病):
  iFinD/SEC EDGAR/Yahoo Finance/天眼查/IMF/世界银行/元典法律/Scholar

新增免费官方 API:
  OpenAlex  - 学术论文 (免费·无 key·20万+条/查)
  CrossRef  - DOI/引文元数据 (免费·Polite Pool)
  PubMed    - 生物医学文献 NCBI E-utilities (免费·无 key)
  GDELT     - 全球新闻事件数据库 (免费·无 key)
  CourtListener - 美国法院文书 (免费·无 key)
  arXiv     - 预印本论文 (免费·无 key)
  Wikidata  - 结构化知识图谱 (免费·SPARQL endpoint)

调用方式:
    from search_fabric_verticals import auto_register_all
    auto_register_all()  # 启动时一次性调用·把所有垂直源接进 search_fabric 路由
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))


# ══════════════════════════════════════════════════════════════════════════════
# 学术源
# ══════════════════════════════════════════════════════════════════════════════

def _search_openalex(query: str, top_k: int = 8) -> list[dict]:
    """OpenAlex 学术搜索 API - 完全免费·无 key·返回论文元数据"""
    try:
        params = urllib.parse.urlencode({
            "search": query,
            "per-page": min(top_k, 25),
            "select": "id,title,doi,authorships,publication_year,primary_location,abstract_inverted_index",
        })
        url = f"https://api.openalex.org/works?{params}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "search_fabric/1.0 (research; mailto:agent@example.com)"}
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        results = []
        for w in data.get("results", [])[:top_k]:
            doi = w.get("doi") or ""
            article_url = doi if doi.startswith("http") else f"https://doi.org/{doi}" if doi else ""
            if not article_url:
                article_url = f"https://openalex.org/{w.get('id','').split('/')[-1]}"

            # 从 abstract_inverted_index 重建摘要
            abstract = ""
            aii = w.get("abstract_inverted_index") or {}
            if aii:
                max_pos = max((p for positions in aii.values() for p in positions), default=0)
                words = [""] * (max_pos + 1)
                for word, positions in aii.items():
                    for pos in positions:
                        if pos <= max_pos:
                            words[pos] = word
                abstract = " ".join(w for w in words if w)[:500]

            authors = ", ".join(
                a.get("author", {}).get("display_name", "")
                for a in (w.get("authorships") or [])[:3]
            )
            year = w.get("publication_year", "")

            results.append({
                "url": article_url,
                "title": w.get("title", ""),
                "content": f"[{year}] {authors}\n{abstract}",
                "source": "openalex",
                "cost_usd": 0.0,
                "meta": {"doi": doi, "year": year, "authors": authors},
            })
        return results
    except Exception as e:
        return [{"_error": f"openalex: {type(e).__name__}: {str(e)[:100]}"}]


def _search_arxiv(query: str, top_k: int = 8) -> list[dict]:
    """arXiv 预印本论文搜索 API - 完全免费·无 key"""
    try:
        params = urllib.parse.urlencode({
            "search_query": f"all:{query}",
            "start": 0,
            "max_results": min(top_k, 20),
            "sortBy": "relevance",
        })
        url = f"https://export.arxiv.org/api/query?{params}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "search_fabric/1.0"}
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(resp.read())

        ns = {"atom": "http://www.w3.org/2005/Atom"}
        results = []
        for entry in root.findall("atom:entry", ns)[:top_k]:
            title = (entry.findtext("atom:title", "", ns) or "").strip().replace("\n", " ")
            summary = (entry.findtext("atom:summary", "", ns) or "").strip()[:400]
            link = ""
            for link_elem in entry.findall("atom:link", ns):
                if link_elem.get("type") == "text/html":
                    link = link_elem.get("href", "")
                    break
            if not link:
                link = entry.findtext("atom:id", "", ns) or ""

            authors = ", ".join(
                (a.findtext("atom:name", "", ns) or "")
                for a in entry.findall("atom:author", ns)[:3]
            )
            results.append({
                "url": link,
                "title": title,
                "content": f"{authors}\n{summary}",
                "source": "arxiv",
                "cost_usd": 0.0,
                "meta": {"authors": authors},
            })
        return results
    except Exception as e:
        return [{"_error": f"arxiv: {type(e).__name__}: {str(e)[:100]}"}]


def _search_crossref(query: str, top_k: int = 8) -> list[dict]:
    """CrossRef DOI/引文元数据 - 免费·Polite Pool·无 key"""
    try:
        params = urllib.parse.urlencode({
            "query": query,
            "rows": min(top_k, 20),
            "select": "DOI,title,author,abstract,published,URL,type",
        })
        url = f"https://api.crossref.org/works?{params}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "search_fabric/1.0 (research; mailto:agent@example.com)"}
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        results = []
        for item in data.get("message", {}).get("items", [])[:top_k]:
            doi = item.get("DOI", "")
            art_url = item.get("URL") or (f"https://doi.org/{doi}" if doi else "")
            titles = item.get("title", [])
            title = titles[0] if titles else ""
            abstract = (item.get("abstract") or "")[:400]
            # 去 HTML 标签
            import re
            abstract = re.sub(r"<[^>]+>", "", abstract)
            authors = ", ".join(
                f"{a.get('family','')}" for a in (item.get("author") or [])[:3]
            )
            year = ""
            pub = item.get("published")
            if pub:
                dp = pub.get("date-parts", [[]])
                if dp and dp[0]:
                    year = str(dp[0][0])

            results.append({
                "url": art_url,
                "title": title,
                "content": f"[{year}] {authors}\n{abstract}",
                "source": "crossref",
                "cost_usd": 0.0,
                "meta": {"doi": doi, "type": item.get("type", ""), "year": year},
            })
        return results
    except Exception as e:
        return [{"_error": f"crossref: {type(e).__name__}: {str(e)[:100]}"}]


def _search_pubmed(query: str, top_k: int = 8) -> list[dict]:
    """PubMed 生物医学文献 - NCBI E-utilities·免费·无 key"""
    try:
        # Step 1: esearch 拿 ID
        search_params = urllib.parse.urlencode({
            "db": "pubmed",
            "term": query,
            "retmax": min(top_k, 20),
            "retmode": "json",
            "sort": "relevance",
        })
        search_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?{search_params}"
        req = urllib.request.Request(search_url,
                                     headers={"User-Agent": "search_fabric/1.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            search_data = json.loads(resp.read().decode("utf-8"))
        ids = search_data.get("esearchresult", {}).get("idlist", [])[:top_k]
        if not ids:
            return []

        # Step 2: esummary 拿摘要
        summary_params = urllib.parse.urlencode({
            "db": "pubmed",
            "id": ",".join(ids),
            "retmode": "json",
        })
        summary_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?{summary_params}"
        req2 = urllib.request.Request(summary_url,
                                      headers={"User-Agent": "search_fabric/1.0"})
        with urllib.request.urlopen(req2, timeout=12) as resp2:
            summary_data = json.loads(resp2.read().decode("utf-8"))

        results = []
        for uid in ids:
            item = summary_data.get("result", {}).get(uid, {})
            if not item:
                continue
            title = item.get("title", "")
            authors = ", ".join(a.get("name", "") for a in (item.get("authors") or [])[:3])
            pub_date = item.get("pubdate", "")
            source_journal = item.get("source", "")
            results.append({
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
                "title": title,
                "content": f"[{pub_date}] {authors} | {source_journal}",
                "source": "pubmed",
                "cost_usd": 0.0,
                "meta": {"pmid": uid, "pub_date": pub_date, "journal": source_journal},
            })
        return results
    except Exception as e:
        return [{"_error": f"pubmed: {type(e).__name__}: {str(e)[:100]}"}]


# ══════════════════════════════════════════════════════════════════════════════
# 新闻/事件源
# ══════════════════════════════════════════════════════════════════════════════

def _search_gdelt(query: str, top_k: int = 8) -> list[dict]:
    """GDELT 全球新闻事件数据库 - 免费·无 key"""
    try:
        params = urllib.parse.urlencode({
            "query": query,
            "mode": "ArtList",
            "maxrecords": min(top_k, 25),
            "sort": "HybridRel",
            "format": "json",
        })
        url = f"https://api.gdeltproject.org/api/v2/doc/doc?{params}"
        req = urllib.request.Request(
            url, headers={"User-Agent": "search_fabric/1.0"}
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        results = []
        for art in (data.get("articles") or [])[:top_k]:
            results.append({
                "url": art.get("url", ""),
                "title": art.get("title", ""),
                "content": art.get("seendate", "") + " | " + art.get("domain", ""),
                "source": "gdelt",
                "cost_usd": 0.0,
                "meta": {
                    "domain": art.get("domain", ""),
                    "date": art.get("seendate", ""),
                    "language": art.get("language", ""),
                },
            })
        return results
    except Exception as e:
        return [{"_error": f"gdelt: {type(e).__name__}: {str(e)[:100]}"}]


# ══════════════════════════════════════════════════════════════════════════════
# 法律源
# ══════════════════════════════════════════════════════════════════════════════

def _search_courtlistener(query: str, top_k: int = 8) -> list[dict]:
    """CourtListener 美国法院文书 - 免费·无 key"""
    try:
        params = urllib.parse.urlencode({
            "q": query,
            "type": "o",  # opinions
            "order_by": "score desc",
            "stat_Precedential": "on",
        })
        url = f"https://www.courtlistener.com/api/rest/v3/search/?{params}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "search_fabric/1.0",
                "Accept": "application/json",
            }
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        results = []
        for r in (data.get("results") or [])[:top_k]:
            results.append({
                "url": f"https://www.courtlistener.com{r.get('absolute_url', '')}",
                "title": r.get("caseName", ""),
                "content": (r.get("snippet") or "")[:400],
                "source": "courtlistener",
                "cost_usd": 0.0,
                "meta": {
                    "court": r.get("court", ""),
                    "date": r.get("dateFiled", ""),
                    "docket": r.get("docketNumber", ""),
                },
            })
        return results
    except Exception as e:
        return [{"_error": f"courtlistener: {type(e).__name__}: {str(e)[:100]}"}]


# ══════════════════════════════════════════════════════════════════════════════
# 社交/论坛源（接 friday_universal 已有实现 + 独立 API）
# ══════════════════════════════════════════════════════════════════════════════

def _search_xiaohongshu(query: str, top_k: int = 8) -> list[dict]:
    """小红书 · Friday universal-search xhc source
    内部源·免费无 quota·秒返·无需 key
    适用: 国内用户真实消费体验/笔记/评测/种草内容
    """
    try:
        sys.path.insert(0, str(_HERE))
        from friday_universal import search_xhc
        hits = search_xhc(query, top_k=top_k) or []
        results = []
        for h in hits:
            if not isinstance(h, dict):
                continue
            results.append({
                "url": h.get("url", ""),
                "title": h.get("title", ""),
                "content": (h.get("content") or h.get("snippet") or "")[:500],
                "source": "xiaohongshu",
                "cost_usd": 0.0,
                "meta": {"author": h.get("author", ""), "score": h.get("score", 0)},
            })
        return results[:top_k]
    except Exception as e:
        return [{"_error": f"xiaohongshu: {type(e).__name__}: {str(e)[:100]}"}]


def _search_reddit(query: str, top_k: int = 8) -> list[dict]:
    """Reddit · 官方 .json 端点（免 OAuth·10 QPM 匿名·GAIA 单题足够）
    适用: 英文用户真实讨论/技术社区/产品评测/事件舆情
    高价值场景: 'site:reddit.com {query}' 搜索精准命中帖子
    升级路径: REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET 环境变量存在时走 OAuth 100 QPM
    """
    try:
        # 优先: Reddit 搜索 API (.json 端点)
        params = urllib.parse.urlencode({
            "q": query,
            "sort": "relevance",
            "limit": min(top_k, 25),
            "type": "link",
        })
        url = f"https://www.reddit.com/search.json?{params}"
        req = urllib.request.Request(
            url,
            headers={
                # Reddit 要求 UA 格式：<platform>:<app>:<ver> (by /u/<user>)
                "User-Agent": "python:search_fabric:v1.0 (by /u/meyo_research)",
                "Accept": "application/json",
            }
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        posts = data.get("data", {}).get("children", [])
        results = []
        for child in posts[:top_k]:
            p = child.get("data", {})
            if not p:
                continue
            selftext = (p.get("selftext") or "")[:400]
            results.append({
                "url": f"https://www.reddit.com{p.get('permalink', '')}",
                "title": p.get("title", ""),
                "content": selftext or p.get("url", ""),
                "source": "reddit",
                "cost_usd": 0.0,
                "meta": {
                    "subreddit": p.get("subreddit", ""),
                    "score": p.get("score", 0),
                    "num_comments": p.get("num_comments", 0),
                    "author": p.get("author", ""),
                    "created_utc": p.get("created_utc", 0),
                },
            })
        return results[:top_k]
    except Exception as e:
        return [{"_error": f"reddit: {type(e).__name__}: {str(e)[:100]}"}]


def _search_x_oembed(query: str, top_k: int = 5) -> list[dict]:
    """X/Twitter · 两条路径：
    1. query 含 twitter.com/x.com URL → oembed 端点免 key 取推文全文
    2. 普通关键词 → Google site:twitter.com 搜索找推文 URL
    适用: 确认特定推文内容 / 找名人/机构在 X 上的公开声明
    注意: 纯关键词搜索质量有限，已知 URL 时准确率 ~100%
    """
    try:
        import re
        results = []

        # 路径 1: query 本身就是 tweet URL
        tweet_url_pattern = re.compile(
            r'https?://(?:www\.)?(?:twitter\.com|x\.com)/\w+/status/(\d+)'
        )
        found_urls = tweet_url_pattern.findall(query)
        direct_urls = tweet_url_pattern.findall(query)
        if direct_urls:
            for tweet_id in direct_urls[:top_k]:
                # 从原始 query 拼回完整 URL
                match = tweet_url_pattern.search(query)
                tweet_url = match.group(0) if match else f"https://twitter.com/i/status/{tweet_id}"
                oembed_url = (
                    f"https://publish.twitter.com/oembed"
                    f"?url={urllib.parse.quote(tweet_url)}&omit_script=true"
                )
                req = urllib.request.Request(
                    oembed_url,
                    headers={"User-Agent": "search_fabric/1.0"}
                )
                try:
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        oe = json.loads(resp.read().decode("utf-8"))
                    # 从 HTML 提取纯文本
                    html = oe.get("html", "")
                    text = re.sub(r"<[^>]+>", " ", html).strip()[:500]
                    results.append({
                        "url": tweet_url,
                        "title": f"@{oe.get('author_name', '')} on X",
                        "content": text,
                        "source": "twitter",
                        "cost_usd": 0.0,
                        "meta": {
                            "author": oe.get("author_name", ""),
                            "author_url": oe.get("author_url", ""),
                        },
                    })
                except Exception:
                    pass
            if results:
                return results[:top_k]

        # 路径 2: 关键词搜索 → 走 friday_universal google + site:twitter.com
        try:
            sys.path.insert(0, str(_HERE))
            from friday_universal import search_google
            q_with_site = f"{query} site:twitter.com OR site:x.com"
            hits = search_google(q_with_site, top_k=top_k) or []
            for h in hits[:top_k]:
                if not isinstance(h, dict):
                    continue
                url = h.get("url", "")
                if not ("twitter.com" in url or "x.com" in url):
                    continue
                results.append({
                    "url": url,
                    "title": h.get("title", ""),
                    "content": (h.get("content") or h.get("snippet") or "")[:400],
                    "source": "twitter",
                    "cost_usd": 0.0,
                    "meta": {},
                })
        except Exception:
            pass

        return results[:top_k]
    except Exception as e:
        return [{"_error": f"twitter: {type(e).__name__}: {str(e)[:100]}"}]


def _search_baidu_via_friday(query: str, top_k: int = 8) -> list[dict]:
    """百度搜索 · Friday universal-search baidu-search-v2 source
    适用: 中文内容/国内事件/中文技术资料/百度百科词条
    注意: 需 Friday 外部引擎 RPM 配额；无配额时自动返空（fail-safe）
    """
    try:
        sys.path.insert(0, str(_HERE))
        from friday_universal import search_baidu
        hits = search_baidu(query, top_k=top_k) or []
        results = []
        for h in hits:
            if not isinstance(h, dict):
                continue
            results.append({
                "url": h.get("url", ""),
                "title": h.get("title", ""),
                "content": (h.get("content") or h.get("snippet") or "")[:500],
                "source": "baidu",
                "cost_usd": 0.0,
                "meta": {},
            })
        return results[:top_k]
    except Exception as e:
        return [{"_error": f"baidu: {type(e).__name__}: {str(e)[:100]}"}]


def _search_hackernews(query: str, top_k: int = 8) -> list[dict]:
    """Hacker News · Algolia API 直连（免费·无 key·无 quota）
    适用: 技术圈讨论/开源项目/工程师视角/Show HN/Ask HN
    """
    try:
        sys.path.insert(0, str(_HERE))
        from friday_universal import search_hn
        hits = search_hn(query, top_k=top_k) or []
        results = []
        for h in hits:
            if not isinstance(h, dict):
                continue
            results.append({
                "url": h.get("url", ""),
                "title": h.get("title", ""),
                "content": (h.get("content") or "")[:500],
                "source": "hackernews",
                "cost_usd": 0.0,
                "meta": {
                    "score": h.get("score", 0),
                    "author": h.get("author", ""),
                    "created_at": h.get("created_at", ""),
                },
            })
        return results[:top_k]
    except Exception as e:
        return [{"_error": f"hackernews: {type(e).__name__}: {str(e)[:100]}"}]


# ══════════════════════════════════════════════════════════════════════════════
# 结构化数据库源（层2：API-first 精准查询，不走 SERP 刮取）
# ══════════════════════════════════════════════════════════════════════════════

def _search_pubchem(query: str, top_k: int = 8) -> list[dict]:
    """PubChem PUG-REST 化学物质数据库
    免费·无 key·支持名称/CID/SMILES/InChI 查询
    策略: 名称→CID→属性(MolecularFormula/IUPACName/MolecularWeight/EC number)
    适用题型: 化学物质EC号/CAS号/分子式/分子量/化学分类/InChI
    """
    try:
        results = []
        # Step 1: 名称→CID 列表
        name_url = (
            f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/"
            f"{urllib.parse.quote(query)}/cids/JSON?name_type=word"
        )
        req = urllib.request.Request(
            name_url, headers={"User-Agent": "search_fabric/1.0"}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                cid_data = json.loads(resp.read().decode("utf-8"))
            cids = cid_data.get("IdentifierList", {}).get("CID", [])[:top_k]
        except Exception:
            # 精确名称查询
            exact_url = (
                f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/"
                f"{urllib.parse.quote(query)}/cids/JSON"
            )
            req2 = urllib.request.Request(
                exact_url, headers={"User-Agent": "search_fabric/1.0"}
            )
            try:
                with urllib.request.urlopen(req2, timeout=10) as resp2:
                    cid_data2 = json.loads(resp2.read().decode("utf-8"))
                cids = cid_data2.get("IdentifierList", {}).get("CID", [])[:top_k]
            except Exception:
                cids = []

        if not cids:
            return []

        # Step 2: CID→属性（批量）
        props_url = (
            f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/"
            f"{','.join(str(c) for c in cids)}/property/"
            f"IUPACName,MolecularFormula,MolecularWeight,InChIKey,CanonicalSMILES/JSON"
        )
        req3 = urllib.request.Request(
            props_url, headers={"User-Agent": "search_fabric/1.0"}
        )
        with urllib.request.urlopen(req3, timeout=10) as resp3:
            props_data = json.loads(resp3.read().decode("utf-8"))

        for prop in props_data.get("PropertyTable", {}).get("Properties", []):
            cid = prop.get("CID", "")
            iupac = prop.get("IUPACName", "")
            mf = prop.get("MolecularFormula", "")
            mw = prop.get("MolecularWeight", "")
            inchikey = prop.get("InChIKey", "")
            smiles = prop.get("CanonicalSMILES", "")
            results.append({
                "url": f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}",
                "title": iupac or f"PubChem CID {cid}",
                "content": (
                    f"MolecularFormula: {mf} | MolecularWeight: {mw} g/mol | "
                    f"InChIKey: {inchikey} | SMILES: {smiles[:80]}"
                ),
                "source": "pubchem",
                "cost_usd": 0.0,
                "meta": {
                    "cid": cid, "iupac": iupac, "formula": mf,
                    "weight": mw, "inchikey": inchikey,
                },
            })
        return results[:top_k]
    except Exception as e:
        return [{"_error": f"pubchem: {type(e).__name__}: {str(e)[:100]}"}]


def _search_wikipedia_links(query: str, top_k: int = 8) -> list[dict]:
    """Wikipedia MediaWiki API · prop=links 精准出链查询
    免费·无 key·直接返回页面全部出链（不抓 HTML·干净完整）
    适用题型:
      - Wikipedia 链接跳数 / 几度分离（图遍历类·BFS 的原始数据源）
      - 实体关联实体（某人→合作过的人/作品）
      - 分类成员（category:XXX 所有成员）
    策略: query→搜索页面→prop=links 出链列表 → 可直接作 BFS frontier
    """
    try:
        results = []
        # Step 1: 搜索页面标题
        search_params = urllib.parse.urlencode({
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": min(3, top_k),  # 只取最相关的前 3 页
            "format": "json",
            "utf8": 1,
        })
        search_url = f"https://en.wikipedia.org/w/api.php?{search_params}"
        req = urllib.request.Request(
            search_url,
            headers={"User-Agent": "search_fabric/1.0 (research tool)"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            search_data = json.loads(resp.read().decode("utf-8"))

        pages = search_data.get("query", {}).get("search", [])
        if not pages:
            return []

        # Step 2: 对最相关页面获取出链列表（prop=links）
        page_title = pages[0].get("title", "")
        links_params = urllib.parse.urlencode({
            "action": "query",
            "titles": page_title,
            "prop": "links|extracts",
            "pllimit": 50,           # 最多拿 50 条出链
            "exintro": 1,            # 只拿摘要段
            "explaintext": 1,        # 纯文本
            "format": "json",
            "utf8": 1,
        })
        links_url = f"https://en.wikipedia.org/w/api.php?{links_params}"
        req2 = urllib.request.Request(
            links_url,
            headers={"User-Agent": "search_fabric/1.0 (research tool)"}
        )
        with urllib.request.urlopen(req2, timeout=12) as resp2:
            links_data = json.loads(resp2.read().decode("utf-8"))

        pages_map = links_data.get("query", {}).get("pages", {})
        for pid, page in pages_map.items():
            title = page.get("title", "")
            extract = (page.get("extract") or "")[:600]
            links_raw = page.get("links", [])
            # 出链列表（用于 BFS frontier）
            link_titles = [lk.get("title", "") for lk in links_raw
                          if lk.get("ns", 0) == 0]  # ns=0 主命名空间

            # 主页面作第一条结果（带摘要）
            results.append({
                "url": f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}",
                "title": title,
                "content": extract,
                "source": "wikipedia_links",
                "cost_usd": 0.0,
                "meta": {
                    "outlinks": link_titles,          # BFS 直接可用
                    "outlink_count": len(link_titles),
                    "page_title": title,
                },
            })
            # 每条出链也作为候选结果（供检索排序）
            for lt in link_titles[: top_k - 1]:
                results.append({
                    "url": f"https://en.wikipedia.org/wiki/{urllib.parse.quote(lt.replace(' ', '_'))}",
                    "title": lt,
                    "content": f"[outlink from '{title}']",
                    "source": "wikipedia_links",
                    "cost_usd": 0.0,
                    "meta": {"parent_page": title, "is_outlink": True},
                })

        return results[:top_k]
    except Exception as e:
        return [{"_error": f"wikipedia_links: {type(e).__name__}: {str(e)[:100]}"}]


def fetch_wikipedia_outlinks(page_title: str, limit: int = 200) -> list[str]:
    """独立工具函数：获取 Wikipedia 页面全部出链标题列表
    供 BFS 图遍历直接调用（不走搜索路由，直接按标题取）
    支持分页：pllimit 500 + plcontinue

    Args:
        page_title: Wikipedia 页面标题（如 "Kevin Bacon"）
        limit:      最多返回多少条出链（默认 200·BFS 每跳用）

    Returns:
        list of page title strings（主命名空间·ns=0）
    """
    try:
        all_links: list[str] = []
        plcontinue = None
        while len(all_links) < limit:
            batch = min(500, limit - len(all_links))
            params: dict = {
                "action": "query",
                "titles": page_title,
                "prop": "links",
                "pllimit": batch,
                "plnamespace": 0,   # 只要主命名空间
                "format": "json",
                "utf8": 1,
            }
            if plcontinue:
                params["plcontinue"] = plcontinue

            url = f"https://en.wikipedia.org/w/api.php?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(
                url, headers={"User-Agent": "search_fabric/1.0 (BFS traversal)"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            pages_map = data.get("query", {}).get("pages", {})
            for page in pages_map.values():
                for lk in (page.get("links") or []):
                    if lk.get("ns", -1) == 0:
                        all_links.append(lk["title"])

            cont = data.get("continue", {})
            plcontinue = cont.get("plcontinue")
            if not plcontinue:
                break

        return all_links[:limit]
    except Exception as e:
        return []


# ══════════════════════════════════════════════════════════════════════════════
# 知识图谱源
# ══════════════════════════════════════════════════════════════════════════════

def _search_wikidata(query: str, top_k: int = 8) -> list[dict]:
    """Wikidata 结构化知识图谱 - 免费·SPARQL·无 key"""
    try:
        params = urllib.parse.urlencode({
            "action": "wbsearchentities",
            "search": query,
            "language": "en",
            "limit": min(top_k, 20),
            "format": "json",
            "type": "item",
        })
        url = f"https://www.wikidata.org/w/api.php?{params}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "search_fabric/1.0 (research tool)"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        results = []
        for r in (data.get("search") or [])[:top_k]:
            qid = r.get("id", "")
            results.append({
                "url": f"https://www.wikidata.org/wiki/{qid}",
                "title": r.get("label", ""),
                "content": r.get("description", ""),
                "source": "wikidata",
                "cost_usd": 0.0,
                "meta": {"qid": qid, "aliases": r.get("aliases", [])},
            })
        return results
    except Exception as e:
        return [{"_error": f"wikidata: {type(e).__name__}: {str(e)[:100]}"}]


# ══════════════════════════════════════════════════════════════════════════════
# Wayback Machine CDX 存档检索 (archive.org · 免费无 key)
# ══════════════════════════════════════════════════════════════════════════════

_WB_CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
# 域名/URL 提取(含可选 scheme·可带路径或 /* 通配)
_WB_URL_RE = re.compile(
    r"(?:https?://)?(?:[a-z0-9][a-z0-9-]*\.)+[a-z]{2,}(?:/[^\s]*)?", re.IGNORECASE)
# 年份区间: 2011-2014 / 2011..2014 / 2011~2014 / 2011 to 2014
_WB_RANGE_RE = re.compile(
    r"\b(19\d{2}|20[0-2]\d)\s*(?:-|–|—|~|\.\.|to|至|到)\s*(19\d{2}|20[0-2]\d)\b",
    re.IGNORECASE)
# 年-月粒度: 2012-12 / 2012/12 / 2012年12月 → 收紧到月窗(能力探针实证: 精确月窗
# 能显著缩小噪声·枚举年末列表页时必要)
_WB_YM_RE = re.compile(
    r"\b(19\d{2}|20[0-2]\d)[-\/年]\s?(0?[1-9]|1[0-2])\b月?")
_WB_YEAR_RE = re.compile(r"\b(19\d{2}|20[0-2]\d)\b")


def _wb_humanize_ts(ts: str) -> str:
    """YYYYMMDDhhmmss → YYYY-MM-DD(取前8位·容错)"""
    if len(ts) >= 8 and ts[:8].isdigit():
        return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
    return ts


def _search_wayback_cdx(query: str, top_k: int = 8) -> list[dict]:
    """Wayback Machine CDX 存档检索 · 按 域名+日期窗+URL关键词 搜历史快照

    定位(2026-08-17 · BrowseComp Q3 三连错实证): 老博客文章/已下线网页/
    历史版本常规搜索引擎搜不到(web 已被 SEO 新内容淹没)·wayback
    availability API 又常 503。CDX 是唯一能按 URL 模式+日期窗枚举存档的口。

    查询协议(从自然语言 query 解析·agent 友好):
      - 域名/URL(必须): 裸域名自动 matchType=domain(含子域)·
        带路径或 * 透传为 URL 模式
      - 年份: '2012' 或区间 '2011-2014' → from/to 窗口
      - 其余词: 作为 original URL 路径关键词过滤(AND 组合)
    例: 'fingertipsmusic.com 2011-2014 best-songs-of'
        'chromewaves.net 2012/12 *'

    返回存档快照 URL(https://web.archive.org/web/<ts>/<original>)·可直接 fetch。
    fail-safe: 503/超时重试 2 次(递增退避)·无域名返错误+用法提示(诚实·不瞎搜)。
    """
    q = query.strip()
    if not q:
        return [{"_error": "wayback_cdx: 空 query"}]

    # 1) 提取 URL/域名模式
    url_pat = ""
    m_url = _WB_URL_RE.search(q)
    if m_url:
        url_pat = m_url.group(0).rstrip(".,;)")
        q_wo_url = q.replace(m_url.group(0), " ")
    else:
        q_wo_url = q

    if not url_pat:
        return [{
            "_error": (
                "wayback_cdx: 需要 URL 模式(域名或URL)才能查 CDX 索引。"
                "请用 '域名 年份或区间 路径关键词' 形式重查·"
                "例: 'myblog.com 2012-2014 best-songs'。"
                "若还不知道域名·先用 ddgs/google 找候选博客域名再来查存档。"
            )
        }]

    # 2) 日期窗口: 优先 年-月粒度(2012-12)·其次年份区间(2011-2014)·最后单年
    from_y = to_y = ""
    m_ym = _WB_YM_RE.search(q_wo_url)
    if m_ym:
        import calendar
        _yy, _mm = m_ym.group(1), int(m_ym.group(2))
        _last = calendar.monthrange(int(_yy), _mm)[1]
        from_y, to_y = f"{_yy}{_mm:02d}01", f"{_yy}{_mm:02d}{_last}"
        q_wo_url = _WB_YM_RE.sub(" ", q_wo_url, count=1)
    else:
        m_rng = _WB_RANGE_RE.search(q_wo_url)
        if m_rng:
            from_y, to_y = m_rng.group(1), m_rng.group(2)
            q_wo_url = _WB_RANGE_RE.sub(" ", q_wo_url, count=1)
        else:
            years = _WB_YEAR_RE.findall(q_wo_url)
            if years:
                from_y, to_y = min(years), max(years)
                q_wo_url = _WB_YEAR_RE.sub(" ", q_wo_url)

    # 3) 路径关键词(去掉操作符词·取小写)
    stop = {"site", "inurl", "http", "https", "www", "com", "net", "org",
            "blog", "wayback", "archive", "cdx", "and", "or", "the", "of",
            "in", "on", "for", "to"}
    kws = []
    for tok in re.split(r"[\s,/|]+", q_wo_url):
        tok = tok.strip("'\"*")
        if len(tok) < 3 or tok.lower() in stop:
            continue
        kws.append(re.escape(tok.lower()))
    kws = kws[:3]  # 过滤词过多会全空·CDX filter 是 AND

    # 4) 组装 CDX 参数
    # (2026-08-17 实测教训: 不用服务端 collapse+statuscode:200 组合——collapse 按
    #  urlkey 取首个抓取·常是 2006 年的 404·再被 status 过滤就全军覆没(实测 0 行)。
    #  改为: 服务端只做关键词过滤+限流·客户端去重/择优/排序。)
    params: list[tuple[str, str]] = [("output", "json"),
                                     ("fl", "timestamp,original,statuscode,mimetype,length"),
                                     ("limit", str(max(top_k * 6, 60)))]
    if "*" in url_pat or "/" in url_pat.replace("/*", ""):
        params.append(("url", url_pat))            # 显式模式(带路径/通配)
    else:
        params.append(("url", url_pat))            # 裸域名
        params.append(("matchType", "domain"))    # 含子域(www 等)
    if from_y:
        params.append(("from", from_y))
    if to_y:
        params.append(("to", to_y))
    for kw in kws:
        params.append(("filter", f"original:.*{kw}.*"))

    api = f"{_WB_CDX_ENDPOINT}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        api, headers={"User-Agent": "search_fabric/1.0 (research; contact: local)"})

    # 5) 请求·503/超时重试 2 次(实证 wayback 偶发 503)
    data = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
            data = json.loads(raw) if raw.strip() else []
            break
        except urllib.error.HTTPError as e:
            if e.code in (503, 502, 504) and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            return [{"_error": f"wayback_cdx: HTTP {e.code} (重试{attempt}次后)"}]
        except Exception as e:
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            return [{"_error": f"wayback_cdx: {type(e).__name__}: {str(e)[:100]}"}]

    if not data:
        return []
    # 首行是字段名·其余是数据行
    rows = data[1:] if (data and data[0] and "timestamp" in str(data[0])) else data

    # 客户端后处理: 去重(同 original 取最新快照)·擗垃圾(广告/静态资源)·择优排序
    _JUNK = ("/adserv/", "adjs.php", "adframe", "doubleclick", "/ads/",
             ".js?", ".css?", ".gif", ".jpg", ".jpeg", ".png", ".ico", ".swf",
             "google_analytics", "/xmlrpc.php", "?feed=rss")
    best: dict[str, dict] = {}
    for r in rows:
        try:
            ts, orig, status, mime, length = (
                str(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4]))
        except Exception:
            continue
        if status not in ("200", "-", ""):
            continue                      # 非 200 抓取直接弃(客户端过滙·免服务端组合坑)
        ol = orig.lower()
        if any(j in ol for j in _JUNK):
            continue
        if mime and "html" not in mime and "xml" not in mime:
            continue                      # 只留网页快照
        try:
            _len = int(length) if length.isdigit() else 0
        except Exception:
            _len = 0
        prev = best.get(orig)
        if not prev or ts > prev["ts"]:   # 同 URL 取最新抓取
            best[orig] = {"ts": ts, "len": _len, "mime": mime}

    # 排序: 内容更长的靠前(首页/列表页往往最长·正文页也优于重定向壳)
    ranked = sorted(best.items(), key=lambda kv: kv[1]["len"], reverse=True)

    results = []
    for orig, info in ranked[:top_k]:
        ts = info["ts"]
        date = _wb_humanize_ts(ts)
        path = orig.split("/", 3)[-1] if "/" in orig else orig
        results.append({
            "url": f"https://web.archive.org/web/{ts}/{orig}",
            "title": f"[archived {date}] {path[:100]}",
            "content": (
                f"Wayback 存档快照 · 抓取于 {date} · 原始URL: {orig} · "
                f"mime={info['mime']} · 快照URL可直接 fetch 读全文。"
            ),
            "source": "wayback_cdx",
            "cost_usd": 0.0,
            "meta": {"original_url": orig, "timestamp": ts,
                     "mimetype": info["mime"], "length": info["len"]},
        })
    return results


# ── 日期驱动博客发现 (WordPress 固定链接 /YYYY/MM/DD/slug/ · 域名未知也能发现) ──
_WB_MONTHS = {m.lower(): i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"])}
for _ab, _full in [("jan", 1), ("feb", 2), ("mar", 3), ("apr", 4), ("jun", 6),
                   ("jul", 7), ("aug", 8), ("sep", 9), ("sept", 9), ("oct", 10),
                   ("nov", 11), ("dec", 12)]:
    _WB_MONTHS[_ab] = _full


def _wb_parse_day(q: str):
    """从自然语言里解析 精确到日 的日期 → (yyyy, mm, dd) · 解析不到返回 None"""
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", q)
    if m:
        return m.group(1), int(m.group(2)), int(m.group(3))
    m = re.search(r"\b(\d{4})(\d{2})(\d{2})\b", q)
    if m:
        return m.group(1), int(m.group(2)), int(m.group(3))
    # December 27 2012 / Dec 27, 2012 / 27 December 2012
    m = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", q)
    if m and m.group(1).lower() in _WB_MONTHS:
        return m.group(3), _WB_MONTHS[m.group(1).lower()], int(m.group(2))
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([A-Za-z]{3,9}),?\s+(\d{4})\b", q)
    if m and m.group(2).lower() in _WB_MONTHS:
        return m.group(3), _WB_MONTHS[m.group(2).lower()], int(m.group(1))
    return None


def _search_wayback_cdx_date(query: str, top_k: int = 10) -> list[dict]:
    """按发布日期枚举 WordPress 博客帖存档 · 已知(或候选)域名时一次命中当天全部帖子

    定位(2026-08-18 · BrowseComp Q3 实证): 小众博客(年访问量四位数)常不被
    Google/Bing 索引·而 WordPress(含 wordpress.com 与自托管)的固定链接结构是
      https://<域名>/YYYY/MM/DD/<post-slug>/
    CDX 尾部通配前缀查询 '域名/YYYY/MM/DD*' = 该博客**当天发布的全部帖子**
    存档·直接给出快照 URL。(注意: 实测 matchType=domain 会忽略路径前缀·
    所以本源只做单域名+日期路径的尾通配查询·不支持跨域名枚举——域名未知时
    请先用 ddgs/google 找候选域名·或用 blogspot_feed 查 blogspot 博客。)

    查询协议(自然语言·agent 友好):
      - 域名(必须): 'myblog.wordpress.com' 或裸 'myblog.com'
      - 日期(必须·精确到日): '2012-12-27' / 'December 27 2012' / '20121227'
      - slug 关键词(可选): 返回结果后客户端按关键词优先排序
    例: 'myblog.wordpress.com 2012-12-27' / ' DereksMusicBlog.com December 27 2013'
    """
    q = query.strip()
    day = _wb_parse_day(q)
    if not day:
        return [{
            "_error": (
                "wayback_cdx_date: 需要 '域名 + 精确到日的日期' (如 "
                "'myblog.wordpress.com 2012-12-27')才能按 WordPress 固定链接 "
                "/YYYY/MM/DD/ 枚举当天帖子。若博客域名未知·先用引擎找候选域名·"
                "blogspot 博客改用 blogspot_feed。"
            )
        }]
    yyyy, mm, dd = day
    if not (1 <= mm <= 12 and 1 <= dd <= 31 and 1990 <= int(yyyy) <= 2030):
        return [{"_error": f"wayback_cdx_date: 日期 {yyyy}-{mm:02d}-{dd:02d} 不合法"}]
    date_path = f"{yyyy}/{mm:02d}/{dd:02d}"

    # 域名: 去掉日期与月份词后剩余的第一个类域名 token
    m_dom = _WB_URL_RE.search(q)
    if m_dom:
        domain = m_dom.group(0).rstrip(".,;)")
    else:
        q_wo_date = _WB_YM_RE.sub(" ", q)
        q_wo_date = re.sub(r"\b\d{4}\b|\b\d{1,2}(?:st|nd|rd|th)?\b", " ", q_wo_date)
        q_wo_date = re.sub(r"\b[A-Za-z]{3,9}\.?\b",
                           lambda m: " " if m.group(0).lower() in _WB_MONTHS else m.group(0),
                           q_wo_date)
        m_bare = re.search(r"\b([a-z0-9][a-z0-9-]{2,40}(?:\.[a-z]{2,10})?)\b", q_wo_date, re.I)
        if m_bare:
            domain = m_bare.group(1).lower()
        else:
            return [{
                "_error": (
                    "wayback_cdx_date: 缺域名。CDX 无法跨域名按日期枚举"
                    "(全网扫描会被服务端短路)。请提供候选域名·如 "
                    f"'myblog.com {yyyy}-{mm:02d}-{dd:02d}'。"
                )
            }]

    # slug 关键词: 去日期/平台/停用词·取剩余可过滤词
    stop = {"site", "inurl", "http", "https", "www", "com", "net", "org",
            "blog", "wordpress", "wp", "blogspot", "wayback", "archive",
            "cdx", "date", "post", "posted", "published", "and", "or",
            "the", "of", "in", "on", "for", "to", "a", "an"}
    q_wo = re.sub(r"\b\d{4}\b", " ", q)
    q_wo = re.sub(r"\b\d{1,2}(?:st|nd|rd|th)?\b", " ", q_wo)
    q_wo = re.sub(r"\b[A-Za-z]{3,9}\.?\b",
                  lambda m: " " if m.group(0).lower() in _WB_MONTHS else m.group(0), q_wo)
    kws = []
    for tok in re.split(r"[\s,/|]+", q_wo):
        tok = tok.strip("'\"*.-")
        if len(tok) < 2 or tok.lower() in stop:
            continue
        kws.append(tok.lower())
    kws = kws[:3]

    # 一次 CDX 前缀查询: 域名/日期路径/* (尾通配·含当天全部帖子)
    params: list[tuple[str, str]] = [
        ("url", f"{domain}/{date_path}/*"),
        ("output", "json"),
        ("fl", "timestamp,original,statuscode,mimetype,length"),
        ("limit", "300"),
    ]
    api = f"{_WB_CDX_ENDPOINT}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        api, headers={"User-Agent": "search_fabric/1.0 (research; contact: local)"})
    data = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
            data = json.loads(raw) if raw.strip() else []
            break
        except urllib.error.HTTPError:
            data = None
            break
        except Exception:
            if attempt < 1:
                time.sleep(1.5)
                continue
            data = None
            break
    best: dict[str, dict] = {}
    if data:
        rows = data[1:] if (data and data[0] and "timestamp" in str(data[0])) else data
        for r in rows:
            try:
                ts, orig, status, mime, length = (
                    str(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4]))
            except Exception:
                continue
            if status not in ("200", "-"):
                continue
            if mime and "html" not in mime:
                continue
            ol = orig.lower()
            if any(j in ol for j in (".jpg", ".png", ".gif", ".css", ".js",
                                     "/feed", "?replytocom", "/author/",
                                     "/tag/", "/category/", "/page/")):
                continue
            try:
                _len = int(length) if length.isdigit() else 0
            except Exception:
                _len = 0
            prev = best.get(orig)
            if not prev or ts > prev["ts"]:
                best[orig] = {"ts": ts, "len": _len, "mime": mime, "combo": (domain,)}

    if not best:
        return []
    ranked = sorted(best.items(), key=lambda kv: kv[1]["len"], reverse=True)
    results = []
    for orig, info in ranked[:top_k]:
        ts = info["ts"]
        date = _wb_humanize_ts(ts)
        domain = orig.split("/")[0] if "/" in orig else orig
        path = orig.split("/", 3)[-1] if "/" in orig else orig
        results.append({
            "url": f"https://web.archive.org/web/{ts}/{orig}",
            "title": f"[{domain} · published {date_path} · archived {date}] {path[:90]}",
            "content": (
                f"日期驱动发现的 WordPress 博客帖 · 博客域名: {domain} · "
                f"发布日期路径: {date_path} · 快照抓取于 {date} · "
                f"slug过滤: {'+'.join(info['combo'])} · 快照URL可直接 fetch 读全文·"
                f"命中词组合越多越可能是目标帖。"
            ),
            "source": "wayback_cdx_date",
            "cost_usd": 0.0,
            "meta": {"original_url": orig, "timestamp": ts, "blog_domain": domain,
                     "published_path": date_path, "mimetype": info["mime"],
                     "length": info["len"], "slug_filter": list(info["combo"])},
        })
    return results


# ── archive.today (archive.ph/.is/.md) · 独立域名替代存档源 ──────────────────
# 定位(2026-08-18 · 落地"爬取优化攻防与架构优化.md"替代源找平器): wayback_cdx/
# wayback_cdx_date 两个工具的可用性都系于同一个域名 web.archive.org——一旦该
# 域名被 _dead_domain_precheck 熔断(实证: fails=8/blocked=53·本机网络对
# archive.org 访问不稳定)，两个工具会被同时拦截，agent 陷入"唯一解题路径被锁死"
# 的死局(BrowseComp 音乐博客题连续多轮实测: 26 次工具调用无一次真正读到 CDX 数据，
# 全部退化为 run_parallel_search 同义改写死循环)。
# archive.today 是完全独立的存档服务(不同运营方/不同域名池 archive.ph|.today|.is|
# .md，DNS/网络故障域独立)，当 web.archive.org 被熔断时仍可能可达——这正是
# "死域检测→替代源链"里第一条真正可执行的替代路径(而非只给 agent 一句文字建议)。
_AT_MIRRORS = ("archive.ph", "archive.today", "archive.is", "archive.md")


def _search_archive_today(query: str, top_k: int = 8) -> list[dict]:
    """archive.today 存档快照查询 · web.archive.org 被熔断/不可达时的独立替代源

    查询协议(自然语言·agent 友好):
      - URL/域名(必须): 完整 URL 精确查该页全部快照；裸域名查该域名下已知快照
      - 例: 'https://myblog.com/2012/12/27/best-songs-of-2012/'
            'myblog.com'

    实现说明: archive.today 无官方公开 JSON API(个人/小站点常見 429)，用其
    timemap 兼容端点(/newest/<url> 重定向 + /<url> 列表页)按 URL 精确查存档，
    HTML 里的 <a href="https://archive.ph/xxxxx"> 快照链接用正则抽取(不整页解析·
    轻量·避免额外依赖)。fail-safe: 403/429/超时都归因到 archive.ph 系域名
    (由 _DEAD_NET_TOOLS 统一走死域熔断)，不抛异常阻塞主流程。
    """
    q = query.strip()
    if not q:
        return [{"_error": "archive_today_search: 空 query"}]

    m_url = _WB_URL_RE.search(q)
    if not m_url:
        return [{
            "_error": (
                "archive_today_search: 需要 URL 或域名才能查存档。"
                "请用完整 URL 重查·例 'https://myblog.com/2012/12/27/post-slug/'。"
                "若还不知道具体 URL·先用 wayback_cdx_date/blogspot_feed/搜索引擎"
                "定位候选页面 URL 再来查。"
            )
        }]
    target = m_url.group(0).rstrip(".,;)")
    if not target.startswith("http"):
        target = f"https://{target}"

    results: list[dict] = []
    last_err = ""
    # 全局耗时预算(2026-08-18 修): 单 mirror 超时收紧到 8s(原 15s·4 个 mirror 全部
    # 网络不可达时会累计到 60s，拖垮上游 t_query_web_archive 的 45s 总超时/agent
    # 每步的墙钟预算)。累计耗时超预算立即停止尝试剩余 mirror，诚实返回已知错误
    # 而不是让调用方硬等到自己的 timeout 机制粗暴杀掉整个调用链。
    _t0 = time.time()
    _BUDGET_S = 25.0
    for mirror in _AT_MIRRORS:
        if time.time() - _t0 > _BUDGET_S:
            last_err = f"{last_err or ''} · 已超全局预算{_BUDGET_S}s提前终止".strip(" ·")
            break
        api = f"https://{mirror}/newest/{target}"
        req = urllib.request.Request(
            api,
            headers={
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/124.0 Safari/537.36"),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                final_url = resp.geturl()
                raw = resp.read(200000).decode("utf-8", errors="ignore")
            # /newest/<url> 成功时 30x 重定向到具体快照页(final_url 即快照 URL)
            if final_url and final_url.rstrip("/") != api.rstrip("/") and mirror in final_url:
                results.append({
                    "url": final_url,
                    "title": f"[archive.today snapshot] {target[:100]}",
                    "content": (
                        f"archive.today({mirror}) 存档快照(独立于 web.archive.org 的"
                        f"替代存档源) · 原始URL: {target} · 快照URL可直接 fetch 读全文。"
                    ),
                    "source": "archive_today",
                    "cost_usd": 0.0,
                    "meta": {"original_url": target, "mirror": mirror},
                })
                break
            # 未重定向但页面里可能嵌了历史快照列表(旧版 archive.today 行为)
            hrefs = re.findall(
                rf'https?://{re.escape(mirror)}/[0-9A-Za-z]{{5,8}}(?=["\'\s>])', raw)
            for h in list(dict.fromkeys(hrefs))[:top_k]:
                results.append({
                    "url": h,
                    "title": f"[archive.today snapshot list] {target[:100]}",
                    "content": (
                        f"archive.today({mirror}) 存档快照候选 · 原始URL: {target} · "
                        f"快照URL可直接 fetch 读全文。"
                    ),
                    "source": "archive_today",
                    "cost_usd": 0.0,
                    "meta": {"original_url": target, "mirror": mirror},
                })
            if results:
                break
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            continue
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:80]}"
            continue

    if not results:
        return [{
            "_error": (
                f"archive_today_search: 全部镜像({', '.join(_AT_MIRRORS)}) 均未命中"
                f"或不可达(最后错误: {last_err or '无快照'})。该 URL 可能确实未被"
                f"archive.today 存档过，或该域名此刻不可达；可换回 wayback_cdx 或"
                f"直接用搜索引擎找替代来源。"
            )
        }]
    return results[:top_k]


# ── MusicBrainz 结构化音乐数据库 (免费API·专辑/录音/年份/曲目数精确反查) ──────
def _search_musicbrainz(query: str, top_k: int = 8) -> list[dict]:
    """MusicBrainz 开放音乐元数据库 · 按年份+曲目数+艺人精确反查专辑/录音

    定位(2026-08-18 · BrowseComp Q3 实证): '1986年的专辑有17首歌'这类**结构化
    反查**是通用引擎做不到的·MB 免费API直接支持 (primarytype:album AND
    date:1986 AND tracks:17 → 244张候选)。对一切音乐题(专辑年份/曲目数/
    艺人作品年表/B面/重发行)是权威一手源·无 key·限速1req/s。

    查询协议(自然语言解析):
      - 艺家/专辑/歌名: 透传 Lucene 语法 'artist:"X"' 'recording:"Y"' 'title'
      - 年份: '1986' → date:1986 · 区间 '2011-2014' → date:[2011 TO 2014]
      - 曲目数: '17轨'/'17 tracks'/'17首歌' → tracks:17
      - 实体类型: 默认 release·含 '录音/recording' 切 recording 实体
    例: '1986 album 17 tracks' / 'Tobin Sprout 全部专辑' / '"Serial Killer" recording'
    """
    import urllib.parse as _up
    q = query.strip()
    if not q:
        return [{"_error": "musicbrainz: 空 query"}]

    # 实体类型
    entity = "release"
    if re.search(r"recording|录音|单曲表|song by|哪首歌", q, re.I):
        entity = "recording"

    # 曲目数
    m_trk = re.search(r"(?:tracks?|轨|trk|songs?)\s*[:=]?\s*(\d{1,3})|(\d{1,3})\s*(?:轨|tracks?\b|songs?\b)", q, re.I)
    tracks = None
    if m_trk:
        tracks = m_trk.group(1) or m_trk.group(2)

    # 年份区间
    date_clause = ""
    m_rng = re.search(r"\b(\d{4})\s*[-~到至]\s*(\d{4})\b", q)
    if m_rng:
        date_clause = f" AND date:[{m_rng.group(1)} TO {m_rng.group(2)}]"
        q = q.replace(m_rng.group(0), " ")
    else:
        years = re.findall(r"\b(19\d{2}|20[0-2]\d)\b", q)
        if years:
            for y in set(years):
                q = q.replace(y, " ")
            if len(set(years)) == 1:
                date_clause = f" AND date:{years[0]}"

    # 艺术家/标题引用短语
    parts = []
    for m in re.finditer(r'"([^"]{2,60})"', q):
        parts.append(f'artist:"{m.group(1)}"' if entity == "release"
                     and not parts else f'"{m.group(1)}"')
        q = q.replace(m.group(0), " ")
    # 剩余裸词
    for tok in re.split(r"\s+", q.strip()):
        tok = tok.strip("'\"·,.，。")
        if len(tok) >= 3 and tok.lower() not in ("album", "albums", "track",
                                                 "tracks", "song", "songs", "cd"):
            parts.append(f'"{tok}"')
    if tracks:
        parts.append(f"tracks:{tracks}")
    parts.append("AND primarytype:album" if entity == "release" and tracks else "")
    lucene = " ".join(p for p in parts if p).strip()
    if not lucene:
        lucene = f'"{query.strip()[:40]}"'
    if date_clause:
        lucene = lucene + date_clause

    api = (f"https://musicbrainz.org/ws/2/{entity}/?query="
           f"{_up.quote(lucene)}&fmt=json&limit={max(top_k, 10)}")
    req = urllib.request.Request(
        api, headers={"User-Agent": "search_fabric/1.0 (research; contact: local)"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            d = json.loads(resp.read().decode("utf-8", errors="ignore"))
    except urllib.error.HTTPError as e:
        return [{"_error": f"musicbrainz: HTTP {e.code} (query语法可能非法)"}]
    except Exception as e:
        return [{"_error": f"musicbrainz: {type(e).__name__}: {str(e)[:80]}"}]

    items = d.get("releases" if entity == "release" else "recordings", []) or []
    results = []
    for r in items[:top_k]:
        if entity == "release":
            artist = ""
            ac = r.get("artist-credit") or []
            if ac and isinstance(ac[0], dict):
                artist = str(ac[0].get("name") or "")
            ntracks = sum(int(m.get("track-count") or 0)
                          for m in (r.get("mediums") or []) if isinstance(m, dict))
            title = str(r.get("title") or "")
            date = str(r.get("date") or "")
            country = str(r.get("country") or "")
            content = (f"MusicBrainz 专辑 · {artist} – {title} · 发行 {date} "
                       f"· 总曲目数 {ntracks} · 国家 {country} · "
                       f"mbid: {r.get('id','')}")
            url = f"https://musicbrainz.org/release/{r.get('id','')}"
        else:
            artist = ""
            ac = r.get("artist-credit") or []
            if ac and isinstance(ac[0], dict):
                artist = str(ac[0].get("name") or "")
            title = str(r.get("title") or "")
            rels = [str(x.get("title") or "") for x in (r.get("releases") or [])[:3]]
            content = (f"MusicBrainz 录音 · {artist} – {title} · 出现在发行: "
                       f"{'; '.join(rels) or '无'} · mbid: {r.get('id','')}")
            url = f"https://musicbrainz.org/recording/{r.get('id','')}"
        results.append({
            "url": url, "title": f"[MB] {artist} – {title}",
            "content": content, "source": "musicbrainz",
            "cost_usd": 0.0, "meta": {"entity": entity},
        })
    if not results:
        return [{"_error": f"musicbrainz: 0 命中 (lucene: {lucene[:120]})"}]
    return results


# ── Blogspot Feed API (按发布日期窗枚举帖子·标题+日期+全文摘要·免费) ─────────
def _search_blogspot_feed(query: str, top_k: int = 10) -> list[dict]:
    """Blogspot 官方 Feed API · 已知博客域名时按**发布日期窗**精确枚举帖子

    定位(2026-08-18 实证): Blogspot 博客(海量BrowseComp答案源)的归档页
    用JS藏帖·Wayback又常常没存到目标月。但官方Feed API
      /feeds/posts/default?published-min=YYYY-MM-DD&T00:00:00
      &published-max=YYYY-MM-DD&T23:59:59&max-results=60&alt=json
    一次拿全当月帖子(标题+精确时间戳+URL+正文HTML摘要)·免费无需key。

    查询协议:
      - 域名(必须): 'xxx.blogspot.com' 或裸 'xxx'(自动补 .blogspot.com)
      - 日期窗(建议): '2012-12' 单月 / '2012-12-20..2012-12-31' 区间 /
        '2012-12-27' 单日·无日期默认当月前后 60 天窗
      - 关键词(可选): 标题/正文过滤·AND
    例: 'lineartrackinglives 2012-12-27' / 'myblog.blogspot.com 2011-12 songs'
    """
    import datetime as _dt
    import urllib.parse as _up
    q = query.strip()
    if not q:
        return [{"_error": "blogspot_feed: 空 query"}]

    # 域名
    m_dom = re.search(r"([a-z0-9-]+(?:\.[a-z0-9-]+)*\bblogspot\.[a-z.]+)", q, re.I)
    if m_dom:
        domain = m_dom.group(1).lower()
        q = q.replace(m_dom.group(1), " ")
    else:
        m_bare = re.search(r"\b([a-z0-9][a-z0-9-]{3,40})\b", q, re.I)
        if not m_bare:
            return [{"_error": "blogspot_feed: 需要博客域名·如 'myblog 2012-12' 或 'myblog.blogspot.com 2012-12-27'"}]
        domain = m_bare.group(1).lower() + ".blogspot.com"
        q = q.replace(m_bare.group(1), " ", 1)

    # 日期窗
    pmin = pmax = None
    m_day = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", q)
    if m_day:
        y_, mo_, d_ = int(m_day.group(1)), int(m_day.group(2)), int(m_day.group(3))
        try:
            pmin = _dt.datetime(y_, mo_, d_, 0, 0, 0)
            pmax = pmin + _dt.timedelta(days=1) - _dt.timedelta(seconds=1)
        except ValueError:
            pmin = None
    if not pmin:
        m_rng = re.search(r"\b(\d{4}-\d{2}-\d{2})\s*\.\.\s*(\d{4}-\d{2}-\d{2})\b", q)
        if m_rng:
            try:
                pmin = _dt.datetime.strptime(m_rng.group(1), "%Y-%m-%d")
                pmax = _dt.datetime.strptime(m_rng.group(2), "%Y-%m-%d") + _dt.timedelta(days=1) - _dt.timedelta(seconds=1)
            except ValueError:
                pmin = None
    if not pmin:
        m_ym = re.search(r"\b(\d{4})-(\d{2})\b", q)
        if m_ym:
            y_, mo_ = int(m_ym.group(1)), int(m_ym.group(2))
            pmin = _dt.datetime(y_, mo_, 1)
            nx = _dt.datetime(y_ + (mo_ == 12), (mo_ % 12) + 1, 1)
            pmax = nx - _dt.timedelta(seconds=1)
    if not pmin:
        # 无日期: 博客全量·靠关键词过滤(feed 默认最新25条)
        pass

    # 关键词
    kws = []
    for tok in re.split(r"\s+", q.strip()):
        tok = tok.strip("'\"·,.，。:..-")
        if len(tok) >= 3 and tok.lower() not in ("blog", "post", "posts", "feed"):
            kws.append(tok.lower())
    kws = kws[:3]

    params = ["alt=json", "max-results=60", "orderby=published"]
    if pmin:
        params.append("published-min=" + pmin.strftime("%Y-%m-%dT%H:%M:%S"))
    if pmax:
        params.append("published-max=" + pmax.strftime("%Y-%m-%dT%H:%M:%S"))
    api = f"https://{domain}/feeds/posts/default?" + "&".join(params)
    req = urllib.request.Request(api, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            d = json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception as e:
        return [{"_error": f"blogspot_feed: {type(e).__name__}: {str(e)[:80]} (域名可能不存在或博客已死)"}]

    entries = (d.get("feed") or {}).get("entry") or []
    results = []
    for e in entries:
        title = str(((e.get("title") or {}).get("$t")) or "")
        pub = str(((e.get("published") or {}).get("$t")) or "")
        link = next((l.get("href") for l in (e.get("link") or [])
                     if l.get("rel") == "alternate"), "")
        content_html = str(((e.get("content") or {}).get("$t"))
                           or ((e.get("summary") or {}).get("$t")) or "")
        text = re.sub(r"<[^>]+>", " ", content_html)
        text = re.sub(r"\s+", " ", text).strip()
        blob = (title + " " + text).lower()
        if kws and not all(k in blob for k in kws):
            continue
        results.append({
            "url": link, "title": f"[{domain} · {pub[:10]}] {title[:80]}",
            "content": (f"Blogspot 帖子 · 博客 {domain} · 发布 {pub} · 标题 {title} · "
                        f"正文摘要: {text[:700]}"),
            "source": "blogspot_feed", "cost_usd": 0.0,
            "meta": {"domain": domain, "published": pub},
        })
        if len(results) >= top_k:
            break
    if not results:
        return [{"_error": f"blogspot_feed: {domain} 该日期窗/关键词 0 帖 (entries={len(entries)})"}]
    return results


# ══════════════════════════════════════════════════════════════════════════════
# 用户浏览器历史 (本机 Chrome 历史·无需 key·本地极快)
# ══════════════════════════════════════════════════════════════════════════════

def _search_browser_history(query: str, top_k: int = 10) -> list[dict]:
    """读取用户本机 Chrome 历史·关键词匹配·返回最近访问过的相关页面

    设计:
    - 只读·拷贝一份临时文件绕过浏览器锁
    - 按 last_visit_time 排序取最新
    - query 关键词在 url OR title 里匹配
    """
    import shutil
    import sqlite3
    import tempfile
    DB_PATHS = [
        os.path.expanduser("~/Library/Application Support/Google/Chrome/Default/History"),
        os.path.expanduser("~/Library/Application Support/Google/Chrome/Profile 1/History"),
        os.path.expanduser("~/.config/google-chrome/Default/History"),          # Linux
        os.path.expanduser("~/AppData/Local/Google/Chrome/User Data/Default/History"),  # Win
    ]
    db_path = next((p for p in DB_PATHS if os.path.exists(p)), None)
    if not db_path:
        return []
    try:
        keywords = [k.strip() for k in query.split() if len(k.strip()) > 2][:5]
        if not keywords:
            return []
        with tempfile.TemporaryDirectory(prefix="bf_history_") as temp_root:
            tmp_db = os.path.join(temp_root, "History")
            shutil.copy2(db_path, tmp_db)
            with sqlite3.connect(tmp_db) as conn:
                # 关键词匹配 url/title，按最近访问排序
                like_clauses = " OR ".join([f"(url LIKE ? OR title LIKE ?)" for _ in keywords])
                params = []
                for kw in keywords:
                    params.extend([f"%{kw}%", f"%{kw}%"])
                rows = conn.execute(
                    f"SELECT url, title, visit_count, last_visit_time FROM urls "
                    f"WHERE ({like_clauses}) AND url NOT LIKE 'chrome://%' "
                    f"ORDER BY last_visit_time DESC LIMIT ?",
                    params + [top_k * 2]
                ).fetchall()
        # 转换 Chrome timestamp (microseconds since 1601-01-01) → readable
        def _ts(t):
            try:
                import datetime
                epoch = datetime.datetime(1601, 1, 1)
                return (epoch + datetime.timedelta(microseconds=t)).strftime("%Y-%m-%d")
            except Exception:
                return ""
        seen_urls = set()
        results = []
        for url, title, visit_count, last_visit in rows:
            if url in seen_urls:
                continue
            seen_urls.add(url)
            results.append({
                "url": url,
                "title": title or url[:80],
                "content": f"[用户浏览历史] 最近访问: {_ts(last_visit)} | 访问{visit_count}次",
                "source": "browser_history",
                "cost_usd": 0.0,
                "meta": {
                    "local_only": True,
                    "last_visit": _ts(last_visit),
                    "visit_count": visit_count,
                },
            })
            if len(results) >= top_k:
                break
        return results
    except Exception as e:
        return [{"_error": f"browser_history: {type(e).__name__}: {str(e)[:100]}"}]


# ══════════════════════════════════════════════════════════════════════════════
# 多模型知识 Panel (Gemini/豆包/DeepSeek/GPT 知识互补)
# ══════════════════════════════════════════════════════════════════════════════

def _search_panel(query: str, top_k: int = 5) -> list[dict]:
    """多模型并行知识检索·Gemini/豆包/DeepSeek/GPT 各答一版·知识互补"""
    try:
        from multi_model_panel import panel_search
        return panel_search(query, timeout=25)[:top_k]
    except ImportError:
        return []
    except Exception as e:
        return [{"_error": f"panel_search: {type(e).__name__}: {str(e)[:80]}"}]


# ══════════════════════════════════════════════════════════════════════════════
# AIsa 自托管复刻 (sh_ 前缀 · 完全免费无 key · 抄 AIsa 算法)
# ══════════════════════════════════════════════════════════════════════════════

def _search_sh_web(query: str, top_k: int = 10) -> list[dict]:
    """自托管 web 搜索·复刻 AIsa web 命令·ddgs + Brave 降级"""
    try:
        from search_fabric_self_hosted import search_web_self_hosted
        return search_web_self_hosted(query, top_k)[:top_k]
    except ImportError:
        return []
    except Exception as e:
        return [{"_error": f"sh_web: {type(e).__name__}: {str(e)[:80]}"}]


def _search_sh_smart(query: str, top_k: int = 10) -> list[dict]:
    """自托管 smart 搜索·复刻 AIsa smart 命令·web+学术并发混搭"""
    try:
        from search_fabric_self_hosted import smart_self_hosted
        return smart_self_hosted(query, top_k)[:top_k]
    except ImportError:
        return []
    except Exception as e:
        return [{"_error": f"sh_smart: {type(e).__name__}: {str(e)[:80]}"}]


def _search_sh_scholar(query: str, top_k: int = 10) -> list[dict]:
    """自托管学术搜索·复刻 AIsa scholar 命令·Semantic Scholar + OpenAlex"""
    try:
        from search_fabric_self_hosted import search_scholar_self_hosted
        return search_scholar_self_hosted(query, top_k)[:top_k]
    except ImportError:
        return []
    except Exception as e:
        return [{"_error": f"sh_scholar: {type(e).__name__}: {str(e)[:80]}"}]


def _search_sh_verity(query: str, top_k: int = 8) -> list[dict]:
    """自托管 verity·复刻 AIsa verity·四路并发+置信度评分"""
    try:
        from search_fabric_self_hosted import verity_self_hosted, _to_hits
        result = verity_self_hosted(query, top_k)
        if not result.get("ok") and not result.get("raw"):
            return []
        raw = result.get("raw", {})
        all_results = raw.get("results", [])
        hits = _to_hits(all_results[:top_k], "sh_verity")
        # 把置信度加到每个 hit 的 meta 里
        conf = result.get("confidence", {})
        for h in hits:
            h["meta"]["confidence_score"] = conf.get("score")
            h["meta"]["confidence_level"] = result.get("confidence_level")
        return hits
    except ImportError:
        return []
    except Exception as e:
        return [{"_error": f"sh_verity: {type(e).__name__}: {str(e)[:80]}"}]


# ══════════════════════════════════════════════════════════════════════════════
# 已有插件适配器 (接线而不是重新实现)
# ══════════════════════════════════════════════════════════════════════════════

def _search_via_tool_impl(tool_name: str, query: str, top_k: int = 8) -> list[dict]:
    """通用已有插件适配器·从 qa_tool_impls 调用已注册工具

    你系统里「建了没接」的 8 个插件:
      iFinD / SEC EDGAR / Yahoo Finance / 天眼查 / IMF / 世界银行 / 元典法律 / Scholar
    """
    try:
        sys.path.insert(0, str(_HERE.parent.parent / "qa_system" / "agent"))
        from qa_tool_impls import TOOL_IMPLS
        fn = TOOL_IMPLS.get(tool_name)
        if fn is None:
            return [{"_error": f"tool_impl_not_found:{tool_name}"}]
        result = fn({"query": query, "top_k": top_k})
        # 标准化输出
        if isinstance(result, list):
            return [
                {
                    "url": r.get("url", ""),
                    "title": r.get("title", r.get("name", "")),
                    "content": r.get("content", r.get("snippet", r.get("summary", ""))),
                    "source": tool_name,
                    "cost_usd": 0.0,
                    "meta": {k: v for k, v in r.items()
                             if k not in ("url", "title", "content", "snippet")},
                }
                for r in result[:top_k]
            ]
        return [{"_error": f"unexpected_result_type:{type(result).__name__}"}]
    except Exception as e:
        return [{"_error": f"{tool_name}: {type(e).__name__}: {str(e)[:100]}"}]


# ══════════════════════════════════════════════════════════════════════════════
# 垂直源 schema 定义
# ══════════════════════════════════════════════════════════════════════════════

_VERTICAL_SOURCES = {
    # ── 社交/论坛源 ────────────────────────────────────────────────────────────
    "xiaohongshu": {
        "schema": {
            "desc": (
                "小红书 · Friday internal xhc source · 免费无 quota · 秒返 · "
                "国内用户真实消费体验/笔记/种草/评测内容"
            ),
            "domains": [
                "xiaohongshu", "xhs", "chinese_social", "consumer_review",
                "lifestyle", "beauty", "food", "travel", "cn_community",
            ],
            "input": "query_str",
            "cost_usd_per_call": 0.0,
            "daily_quota": 999999,
            "compliance": "internal_api",
            "tier": 1,
            "avg_latency_s": 0.5,
            "needs_key": False,
            "channel": "friday_internal",
            "health_note": "内网源·外网不可用时自动 fallback",
        },
        "callable": _search_xiaohongshu,
    },
    "reddit": {
        "schema": {
            "desc": (
                "Reddit 官方 .json 端点 · 免 OAuth · 英文用户真实讨论/技术社区/舆情 · "
                "10 QPM 匿名配额·GAIA 单题足够 · 升级路径: REDDIT_CLIENT_ID 环境变量→OAuth 100 QPM"
            ),
            "domains": [
                "reddit", "english_community", "tech_discussion", "user_opinion",
                "product_review", "news_reaction", "social_media", "forum",
            ],
            "input": "query_str",
            "cost_usd_per_call": 0.0,
            "daily_quota": 600,   # 匿名 10 QPM × ~60s/min × 保守值
            "compliance": "api_tos",
            "tier": 1,
            "avg_latency_s": 1.2,
            "needs_key": False,
            "channel": "reddit_json_api",
            "health_note": "真实帖子内容·subreddit 元数据·score/comment 数",
        },
        "callable": _search_reddit,
    },
    "twitter": {
        "schema": {
            "desc": (
                "X/Twitter · 双路径: ①已知 tweet URL → oembed 免 key 取全文 "
                "②关键词 → google site:twitter.com · "
                "适用于确认特定推文/名人声明/实时舆情"
            ),
            "domains": [
                "twitter", "x_com", "social_media", "real_time", "opinion",
                "breaking_news", "influencer", "official_statement",
            ],
            "input": "tweet_url_or_query",
            "cost_usd_per_call": 0.0,
            "daily_quota": 999999,
            "compliance": "public",   # oembed 是官方公开端点
            "tier": 1,
            "avg_latency_s": 1.5,
            "needs_key": False,
            "channel": "oembed_plus_google_site",
            "health_note": "已知 URL 准确率~100%；纯关键词搜索依赖 Friday google quota",
        },
        "callable": _search_x_oembed,
    },
    "baidu": {
        "schema": {
            "desc": (
                "百度搜索 · Friday baidu-search-v2 source · "
                "中文内容/国内事件/中文技术资料/百度百科词条 · "
                "需 Friday 外部引擎 RPM 配额；无配额时返空（fail-safe）"
            ),
            "domains": [
                "baidu", "chinese_web", "cn_news", "cn_tech", "baike",
                "tieba", "zhidao", "cn_any",
            ],
            "input": "query_str",
            "cost_usd_per_call": 0.002,   # ¥14.4/千 ≈ $0.002/次
            "daily_quota": 500,
            "compliance": "api_tos",
            "tier": 1,
            "avg_latency_s": 1.5,
            "needs_key": True,            # 需 Friday RPM 配额
            "channel": "friday_external_baidu",
            "health_note": "中文精确度高；需确认 RPM 配额已开放",
        },
        "callable": _search_baidu_via_friday,
    },
    "hackernews": {
        "schema": {
            "desc": (
                "Hacker News · Algolia API 直连 · 完全免费无 key · "
                "技术圈讨论/开源项目/工程师视角/Show HN/Ask HN"
            ),
            "domains": [
                "hackernews", "hn", "tech_community", "startup", "open_source",
                "engineering", "programming", "product_launch",
            ],
            "input": "query_str",
            "cost_usd_per_call": 0.0,
            "daily_quota": 999999,
            "compliance": "public",
            "tier": 1,
            "avg_latency_s": 0.8,
            "needs_key": False,
            "channel": "algolia_direct",
            "health_note": "Algolia 速率宽松·实测稳定",
        },
        "callable": _search_hackernews,
    },
    # ── 结构化数据库源 ─────────────────────────────────────────────────────────
    "pubchem": {
        "schema": {
            "desc": (
                "PubChem PUG-REST 化学物质数据库·名称→CID→属性(分子式/分子量/IUPAC名/InChIKey/SMILES)·"
                "免费·无 key·精准查单个化合物·不走 SERP 刮取"
            ),
            "domains": [
                "chemistry", "chemical", "compound", "molecular_formula",
                "molecular_weight", "ec_number", "cas_number", "inchi",
                "smiles", "pharmacology", "enzyme", "biochemistry",
            ],
            "input": "compound_name_or_query",
            "cost_usd_per_call": 0.0,
            "daily_quota": 400,        # PUG-REST 速率：5 req/s，实际远超
            "compliance": "api_tos",
            "tier": 1,
            "avg_latency_s": 1.5,
            "needs_key": False,
            "api_first": True,         # 标记：结构化 API·不走 SERP
            "example": (
                "query='aspirin' → CID 2244 → MolecularFormula: C9H8O4, "
                "MolecularWeight: 180.16 g/mol, IUPACName: 2-acetyloxybenzoic acid"
            ),
        },
        "callable": _search_pubchem,
    },
    "wikipedia_links": {
        "schema": {
            "desc": (
                "Wikipedia MediaWiki API prop=links·返回页面全部出链标题列表·"
                "免费·无 key·用于 Wikipedia 链接跳数/图遍历类题·BFS frontier 原始数据"
            ),
            "domains": [
                "wikipedia", "wiki_graph", "link_distance", "degrees_of_separation",
                "graph_traversal", "bfs", "shortest_path", "six_degrees",
                "entity_links", "outlinks",
            ],
            "input": "entity_or_page_name",
            "cost_usd_per_call": 0.0,
            "daily_quota": 999999,
            "compliance": "public",
            "tier": 1,
            "avg_latency_s": 0.8,
            "needs_key": False,
            "api_first": True,
            "example": (
                "query='Kevin Bacon' → returns page extract + outlinks list "
                "['Apollo 13', 'Footloose', ...] for BFS traversal"
            ),
        },
        "callable": _search_wikipedia_links,
    },
    "openalex": {
        "schema": {
            "desc": "OpenAlex 学术搜索·论文/机构/作者·2亿+记录·完全免费·无 key",
            "domains": ["academic", "science", "citation", "biology", "chemistry",
                        "physics", "medicine", "computer_science"],
            "input": "query_str",
            "cost_usd_per_call": 0.0,
            "daily_quota": 100000,
            "compliance": "api_tos",
            "tier": 1,
            "avg_latency_s": 1.2,
            "needs_key": False,
        },
        "callable": _search_openalex,
    },
    "arxiv": {
        "schema": {
            "desc": "arXiv 预印本论文·物理/数学/CS/生物·完全免费·无 key",
            "domains": ["academic", "preprint", "physics", "math", "cs",
                        "biology", "quantitative_finance"],
            "input": "query_str",
            "cost_usd_per_call": 0.0,
            "daily_quota": 10000,
            "compliance": "api_tos",
            "tier": 1,
            "avg_latency_s": 1.5,
            "needs_key": False,
        },
        "callable": _search_arxiv,
    },
    "crossref": {
        "schema": {
            "desc": "CrossRef DOI/引文元数据·1.5亿+记录·免费 Polite Pool·无 key",
            "domains": ["academic", "citation", "doi", "science", "humanities"],
            "input": "query_str",
            "cost_usd_per_call": 0.0,
            "daily_quota": 50000,
            "compliance": "api_tos",
            "tier": 1,
            "avg_latency_s": 0.9,
            "needs_key": False,
        },
        "callable": _search_crossref,
    },
    "pubmed": {
        "schema": {
            "desc": "PubMed 生物医学文献·NCBI E-utilities·3400万+记录·免费·无 key",
            "domains": ["biomedical", "clinical", "biology", "chemistry",
                        "pharmacology", "academic"],
            "input": "query_str",
            "cost_usd_per_call": 0.0,
            "daily_quota": 10000,
            "compliance": "api_tos",
            "tier": 1,
            "avg_latency_s": 1.2,
            "needs_key": False,
        },
        "callable": _search_pubmed,
    },
    "gdelt": {
        "schema": {
            "desc": "GDELT 全球新闻事件数据库·实时新闻/事件/地缘·免费·无 key",
            "domains": ["news", "events", "geopolitics", "current_affairs",
                        "media", "international"],
            "input": "query_str",
            "cost_usd_per_call": 0.0,
            "daily_quota": 999999,
            "compliance": "public",
            "tier": 1,
            "avg_latency_s": 2.0,
            "needs_key": False,
        },
        "callable": _search_gdelt,
    },
    "courtlistener": {
        "schema": {
            "desc": "CourtListener 美国法院文书·判决书/意见书·免费·无 key",
            "domains": ["legal", "court", "law", "litigation", "us_law"],
            "input": "query_str",
            "cost_usd_per_call": 0.0,
            "daily_quota": 1000,
            "compliance": "api_tos",
            "tier": 1,
            "avg_latency_s": 1.5,
            "needs_key": False,
        },
        "callable": _search_courtlistener,
    },
    "wikidata": {
        "schema": {
            "desc": "Wikidata 结构化知识图谱·实体/关系/属性·免费·无 key",
            "domains": ["facts", "entities", "biography", "geography",
                        "science", "history", "any"],
            "input": "query_str",
            "cost_usd_per_call": 0.0,
            "daily_quota": 999999,
            "compliance": "public",
            "tier": 1,
            "avg_latency_s": 0.8,
            "needs_key": False,
        },
        "callable": _search_wikidata,
    },
    "wayback_cdx": {
        "schema": {
            "desc": (
                "Wayback Machine CDX 存档检索 · archive.org 历史网页快照 · "
                "免费无key · 按域名+日期窗+URL关键词枚举存档页面 · "
                "找老博客文章/已下线网页/历史版本/旧新闻页必用(常规搜索引擎搜不到) · "
                "查询格式: '域名 年份或区间 路径关键词'·例 'myblog.com 2012-2014 best-songs'"
            ),
            "domains": ["archive", "old_webpage", "web_history", "dead_link",
                        "blog", "old_post", "history", "version_history",
                        "retired_page", "any"],
            "input": "query_str",
            "cost_usd_per_call": 0.0,
            "daily_quota": 2000,
            "compliance": "public",
            "tier": 1,
            "avg_latency_s": 4.0,
            "needs_key": False,
        },
        "callable": _search_wayback_cdx,
    },
    "wayback_cdx_date": {
        "schema": {
            "desc": (
                "按发布日期发现 WordPress 博客帖存档 · **不知道博客域名也能发现小众博客** · "
                "原理: WordPress 固定链接结构 /YYYY/MM/DD/slug/ · CDX 域匹配 "
                "wordpress.com/日期 = 枚举全网当天发帖的博客 · slug 关键词过滤定位目标帖 · "
                "当搜索引擎(Google/Bing/DDG)撒网搜不到某个小众博客·但你知道帖子发布日期时必用 · "
                "查询格式: '日期(精确到日) slug关键词'·例 '2012-12-27 24 songs' / "
                "'December 27 2012 favorite songs year'"
            ),
            "domains": ["blog", "old_post", "niche_blog_discovery", "archive",
                        "old_webpage", "web_history", "music_blog", "personal_blog",
                        "any"],
            "input": "query_str",
            "cost_usd_per_call": 0.0,
            "daily_quota": 500,
            "compliance": "public",
            "tier": 1,
            "avg_latency_s": 10.0,
            "needs_key": False,
        },
        "callable": _search_wayback_cdx_date,
    },
    "archive_today": {
        "schema": {
            "desc": (
                "archive.today(archive.ph/.is/.md) 存档快照查询 · "
                "**web.archive.org 被熔断/不可达时的独立替代存档源**(不同域名池·"
                "网络故障域独立·不共享 wayback_cdx 的死域状态) · "
                "已知具体 URL 时用来查其存档快照 · 免费无key · "
                "wayback_cdx/wayback_cdx_date 连续失败或被死域熔断拦截后必试 · "
                "查询格式: 完整 URL·例 'https://myblog.com/2012/12/27/best-songs-of/'"
            ),
            "domains": ["archive", "old_webpage", "web_history", "dead_link",
                        "blog", "old_post", "history", "version_history",
                        "retired_page", "wayback_fallback", "any"],
            "input": "query_str",
            "cost_usd_per_call": 0.0,
            "daily_quota": 500,
            "compliance": "public",
            "tier": 1,
            "avg_latency_s": 6.0,
            "needs_key": False,
        },
        "callable": _search_archive_today,
    },
    "musicbrainz": {
        "schema": {
            "desc": (
                "MusicBrainz 开放音乐元数据库 · 专辑/录音/艺人的**结构化精确反查** · "
                "支持 年份+曲目数+艺人+专辑名 组合过滤(Lucene) · 免费·权威一手源 · "
                "适用: '哪张1986年专辑有17首曲'/'某艺人全部专辑及年份'/'某歌名出现在"
                "哪些发行'/'B面/重发行/单曲表' · 音乐类多跳题的反查锚点 · "
                "查询格式: 自然语言含 艺家名/专辑名/'歌名'/年份/区间/N轨 · "
                "例 '1986 album 17 tracks' / 'Tobin Sprout 全部专辑' / "
                "'recording Serial Killer artist Sprout'", 
            ),
            "domains": ["music", "album", "discography", "tracklist",
                        "recording", "song", "artist", "release", "any"],
            "input": "query_str",
            "cost_usd_per_call": 0.0,
            "daily_quota": 2000,
            "compliance": "public",
            "tier": 1,
            "avg_latency_s": 2.0,
            "needs_key": False,
        },
        "callable": _search_musicbrainz,
    },
    "blogspot_feed": {
        "schema": {
            "desc": (
                "Blogspot 官方 Feed API · 已知博客域名时按**发布日期窗**精确枚举帖子 · "
                "一次拿全当月/当日帖子(标题+精确时间戳+URL+正文摘要) · 免费·无反爬 · "
                "Blogspot 归档页用JS藏帖·Wayback常没存到目标月——Feed API 绕开两者 · "
                "适用: 题目说'某12月27日的博客帖'且定位到 blogspot 域名后枚举验证 · "
                "查询格式: '博客名或域名 日期窗 关键词'·例 'myblog 2012-12-27' / "
                "'myblog.blogspot.com 2011-12 songs'"
            ),
            "domains": ["blog", "blogspot", "post_by_date", "old_post",
                        "archive", "music_blog", "personal_blog", "any"],
            "input": "query_str",
            "cost_usd_per_call": 0.0,
            "daily_quota": 2000,
            "compliance": "public",
            "tier": 1,
            "avg_latency_s": 3.0,
            "needs_key": False,
        },
        "callable": _search_blogspot_feed,
    },
    # ── 用户浏览器历史 (本机 Chrome/Safari) ────────────────────────────────────
    "browser_history": {
        "schema": {
            "desc": (
                "读取用户本机 Chrome 浏览历史·返回用户最近访问过的相关页面 URL+标题 · "
                "无需网络请求·本地极快·适合复现用户最近研究过的问题 · "
                "只读 Chrome Default/History 数据库(只读副本·不影响浏览器)"
            ),
            "domains": ["personal", "research", "recently_visited", "user_context",
                        "any"],
            "input": "query_str",
            "cost_usd_per_call": 0.0,
            "daily_quota": 999999,
            "compliance": "local_only",  # 本机数据·不出网
            "tier": 1,
            "avg_latency_s": 0.2,
            "needs_key": False,
            "local_only": True,
        },
        "callable": _search_browser_history,
    },
    # ── 多模型知识 Panel (Gemini/豆包/DeepSeek/GPT 知识互补) ────────────────────
    "panel_search": {
        "schema": {
            "desc": (
                "多模型知识并行检索·Gemini/豆包/DeepSeek/GPT 各答一版·知识互补 · "
                "适合历史/定义/解释/方法类查询·各模型训练截止日期不同→覆盖互补 · "
                "无额外费用(美团网关已有权限)·不是实时联网·是知识检索"
            ),
            "domains": ["knowledge", "facts", "science", "history", "explanation",
                        "definition", "method", "any"],
            "input": "query_str",
            "cost_usd_per_call": 0.0,
            "daily_quota": 999999,
            "compliance": "internal_api",
            "tier": 1,
            "avg_latency_s": 5.0,
            "needs_key": False,
            "model_native_search": True,
            "knowledge_retrieval": True,
        },
        "callable": _search_panel,
    },
    # ── AIsa 自托管复刻 (sh = self-hosted · 完全免费无 key) ────────────────────
    # 完整复刻 AIsa web/smart/scholar/verity 四个命令的功能
    # 替代方案: 当 AISA_API_KEY 余额空了时零降级使用
    "sh_web": {
        "schema": {
            "desc": (
                "自托管 web 搜索·复刻 AIsa /scholar/search/web · "
                "ddgs 聚合 8 引擎 + Brave 降级·完全免费无 key"
            ),
            "domains": ["general", "news", "tech", "any"],
            "input": "query_str",
            "cost_usd_per_call": 0.0,
            "daily_quota": 999999,
            "compliance": "public",
            "tier": 1,
            "avg_latency_s": 2.0,
            "needs_key": False,
            "self_hosted_aisa": True,
        },
        "callable": _search_sh_web,
    },
    "sh_smart": {
        "schema": {
            "desc": (
                "自托管 smart 搜索·复刻 AIsa /scholar/search/smart · "
                "web + 学术双路并发自动混搭·完全免费无 key"
            ),
            "domains": ["general", "academic", "science", "tech", "any"],
            "input": "query_str",
            "cost_usd_per_call": 0.0,
            "daily_quota": 999999,
            "compliance": "public",
            "tier": 1,
            "avg_latency_s": 4.0,
            "needs_key": False,
            "self_hosted_aisa": True,
        },
        "callable": _search_sh_smart,
    },
    "sh_scholar": {
        "schema": {
            "desc": (
                "自托管学术搜索·复刻 AIsa /scholar/search/scholar · "
                "Semantic Scholar + OpenAlex 双路·2亿+论文·完全免费无 key"
            ),
            "domains": ["academic", "science", "biology", "chemistry", "physics",
                        "cs", "medicine", "citation", "research"],
            "input": "query_str",
            "cost_usd_per_call": 0.0,
            "daily_quota": 999999,
            "compliance": "api_tos",
            "tier": 1,
            "avg_latency_s": 2.5,
            "needs_key": False,
            "self_hosted_aisa": True,
        },
        "callable": _search_sh_scholar,
    },
    "sh_verity": {
        "schema": {
            "desc": (
                "自托管 verity·复刻 AIsa verity 多源置信度检索 · "
                "scholar+web+smart+wikipedia 四路并发·带置信度分·完全免费无 key"
            ),
            "domains": ["verification", "facts", "science", "any"],
            "input": "query_str",
            "cost_usd_per_call": 0.0,
            "daily_quota": 999999,
            "compliance": "public",
            "tier": 1,
            "avg_latency_s": 6.0,
            "needs_key": False,
            "self_hosted_aisa": True,
            "cross_verification": True,
        },
        "callable": _search_sh_verity,
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# 层3 策略库（Strategy Library）
# agent 通过 get_strategy(task_type) 语义查询获取算法框架
# 不是关键词触发，是任务类型语义匹配
# ══════════════════════════════════════════════════════════════════════════════

STRATEGY_LIBRARY: dict[str, dict] = {
    "graph_traversal_bfs": {
        "task_types": [
            "wikipedia link distance", "degrees of separation", "shortest path",
            "how many hops", "how many clicks", "link chain", "six degrees",
            "最短路径", "链接跳数", "几度分离", "几跳", "最少几步",
        ],
        "trigger_keywords": [
            "hops", "clicks", "degrees", "links away", "shortest path",
            "wikipedia", "link distance", "how many steps",
        ],
        "description": "Wikipedia 链接跳数 / 几度分离 / 最短路径 → BFS 图遍历",
        "tools_needed": ["fetch_outlinks"],
        "algorithm": """
BFS 图遍历策略（Wikipedia 链接跳数）:

原理: Wikipedia 链接图是有向图，BFS 保证找到最短路径。
     不要用随机搜索猜——BFS 是数学上保证找到最短路径的唯一正确方法。

工具: fetch_outlinks(page_title) → 返回该页面所有出链标题列表（MediaWiki API·干净完整）

算法框架（在 code_exec 里实现）:
```python
from collections import deque

def bfs_link_distance(start: str, target: str, max_hops: int = 6) -> int:
    # 规范化标题（首字母大写，空格替换）
    start = start.strip()
    target = target.strip()
    if start == target:
        return 0

    queue = deque([(start, 0)])
    visited = {start}

    while queue:
        page, depth = queue.popleft()
        if depth >= max_hops:
            continue
        # 用 fetch_outlinks 工具获取出链（每次 API 调用一页）
        outlinks = fetch_outlinks(page)  # → list[str]
        for link in outlinks:
            if link == target:
                return depth + 1
            if link not in visited:
                visited.add(link)
                queue.append((link, depth + 1))
    return -1  # 未在 max_hops 内找到
```

优化: 双向 BFS（从 start 和 target 同时扩展，相遇即找到最短路径，效率更高）:
  frontier_a = {start}, frontier_b = {target}
  visited_a = {start}, visited_b = {target}, depth = 0
  while True:
    depth += 1
    next_a = {l for p in frontier_a for l in fetch_outlinks(p)} - visited_a
    if next_a & (visited_b | frontier_b): return depth
    visited_a |= next_a; frontier_a = next_a
    （同理对 frontier_b 扩展，depth += 1，判断相交）

注意:
  - fetch_outlinks 每次只拿一页的出链，BFS 第 N 跳需对每个 frontier 页分别调用
  - pllimit=200 每次最多 200 条出链，一页通常 50-200 条
  - Wikipedia 链接图稠密，3-4 跳内通常可达大多数实体
""",
        "example_case": "从 'Kevin Bacon' 到 'Barack Obama'，BFS 找最短路径",
        "success_rate_hint": "BFS 对此类题成功率接近 100%；随机搜索成功率 <30%",
    },

    "api_first_structured_lookup": {
        "task_types": [
            "chemical property", "molecular formula", "molecular weight",
            "ec number", "cas number", "inchi", "smiles", "iupac name",
            "compound property", "chemical identifier",
            "化学物质属性", "分子式", "分子量", "EC号", "CAS号", "化学结构",
            "wikidata property", "entity attribute", "structured fact",
            "实体属性", "结构化事实查询",
        ],
        "trigger_keywords": [
            "molecular formula", "molecular weight", "ec number", "cas number",
            "inchikey", "smiles", "pubchem", "cid", "compound",
            "nationality", "born in", "founded in", "capital of",
        ],
        "description": "结构化属性精准查询 → API-first（PubChem/Wikidata）而非 SERP 刮取",
        "tools_needed": ["query_structured_db"],
        "algorithm": """
API-first 策略（结构化数据库精准查询）:

原理: 对于"某物质的分子式是多少""某人的国籍"这类有标准答案的结构化属性问题，
     搜索引擎返回的是人类可读网页（脏·格式不稳定·可能错），
     而官方 API 返回机器可读的精确值。

工具: query_structured_db('{db}:{query}')

数据库选择:
  化学物质 → PubChem PUG-REST
    query_structured_db('pubchem:aspirin')
    → CID 2244, MolecularFormula: C9H8O4, MolecularWeight: 180.16 g/mol
    → IUPACName: 2-acetyloxybenzoic acid

  实体关系/属性 → Wikidata
    query_structured_db('wikidata:Marie Curie')
    → nationality, born, occupation, awards

  生物医学 → PubMed
    query_structured_db('pubmed:{gene/disease/drug name}')

  学术论文 → OpenAlex / CrossRef
    run_parallel_search 已包含这些源

关键原则: 对于 PubChem/Wikidata 有覆盖的题，
  先调 query_structured_db，拿到精确值后直接 finalize_answer。
  不要先跑 run_parallel_search 再从网页摘要里抽——那是慢 + 不准的路径。
""",
        "example_case": "某酶的 EC 号 → query_structured_db('pubchem:{enzyme_name}')，直接得 EC number",
        "success_rate_hint": "API 直查对 PubChem/Wikidata 覆盖范围内的题成功率 >95%",
    },

    "multi_hop_entity_chain": {
        "task_types": [
            "multi-hop factual", "bridge entity", "chain of facts",
            "A的B是谁", "A的B的C", "中间实体", "桥接实体",
            "who is the X of Y's Z",
        ],
        "description": "多跳事实链（A→B→C）→ 分解子问题·逐跳搜索",
        "tools_needed": ["run_parallel_search", "run_deep_research_swarm"],
        "algorithm": """
多跳事实链策略:

1. 识别桥接实体: 分析题目找出需要分几跳才能答出
   如 "Obama 的 SecDef 在哪所大学就读" → 跳1: Obama的SecDef是谁 → 跳2: 他在哪上大学

2. 每跳单独搜索（不要一次搜全部）:
   run_parallel_search("Obama Secretary of Defense")
   → 得到名字 → run_parallel_search("{name} university education")

3. 结构化 DB 优先 (如果某跳的答案是结构化属性):
   query_structured_db('wikidata:{entity}') 比搜索更精准

4. 证据链闭环: 每跳结果存入 known_facts，下跳 query 带入已知事实
""",
        "example_case": "奥巴马的第一任国防部长在哪所大学拿到学位",
        "success_rate_hint": "分跳搜索比一次搜全部成功率高 40%+",
    },

    "hard_wall_detection": {
        "task_types": [
            "paywalled paper", "subscription required", "sciencedirect paywall",
            "elsevier full text", "springer link paywall",
            "private video", "unlisted youtube", "deleted content",
            "需要订阅", "付费墙", "无法访问全文",
        ],
        "description": "识别真墙题并及时停止烧 load → 挂起机制",
        "algorithm": """
真墙检测策略（停损）:

识别标志（任意一条命中即为真墙）:
  - fetch_url_smart 返回 "login required" / "subscribe to view" / "403"
  - 所有搜索命中都指向同一付费平台（ScienceDirect/Springer/Nature/Elsevier）
  - 视频平台返回 "private" / "deleted" / "members only"

行动: 一旦确认真墙:
  1. 尝试公开替代源（arXiv 预印本/PubMed Central/Semantic Scholar 开放版本）
  2. 两次尝试仍为墙 → 标记 task_type='hard_wall' → 用已有信息尽力回答 + 说明局限
  3. 不要继续烧工具调用追同一个付费墙——那是无效开销

挂起清单: session['hard_wall_tasks'] 记录确认为真墙的题，避免重复探索。
""",
        "example_case": "ScienceDirect 论文全文、无标签 YouTube 视频",
    },
}


def get_strategy(task_description: str) -> dict | None:
    """根据任务描述语义匹配最合适的策略条目

    Args:
        task_description: 描述任务类型的自然语言字符串

    Returns:
        匹配到的策略 dict，包含 algorithm / tools_needed / description
        未匹配到返回 None
    """
    desc_lower = task_description.lower()

    # 计算每个策略的匹配分（关键词命中数）
    best_score = 0
    best_strategy = None

    for strategy_name, strategy in STRATEGY_LIBRARY.items():
        score = 0
        for kw in strategy.get("trigger_keywords", []):
            if kw.lower() in desc_lower:
                score += 2  # keyword 权重更高
        for tt in strategy.get("task_types", []):
            if tt.lower() in desc_lower:
                score += 1
        if score > best_score:
            best_score = score
            best_strategy = {**strategy, "_name": strategy_name, "_score": score}

    return best_strategy if best_score > 0 else None


def get_all_strategies() -> dict[str, dict]:
    """返回完整策略库（供 agent prompt 注入用）"""
    return STRATEGY_LIBRARY


def auto_register_all() -> int:
    """把所有垂直源注册进 search_fabric 路由·返回注册成功数

    在程序启动时调一次·新源接入加到 _VERTICAL_SOURCES 里即可·不改路由代码。
    """
    try:
        from search_fabric import register_source, SOURCE_REGISTRY
    except ImportError as e:
        print(f"[search_fabric_verticals] search_fabric not available: {e}",
              flush=True)
        return 0

    count = 0
    for name, spec in _VERTICAL_SOURCES.items():
        # 去掉 schema 里的 slot 标记 (W3 实现完了不需要了)
        schema = dict(spec["schema"])
        schema.pop("slot", None)
        register_source(name, schema, spec["callable"])
        count += 1

    # 2026-08-20 · 第二/三条翻墙通道 browser_bing(借道本地浏览器·web-access proxy
    # 优先→playwright headless 兜底)。AnySearch(主力·云端墙外服务器·1000/日)额度耗尽
    # 或失败时降级到此·无次数限制但慢·故 tier=2(fallback级·不抢主并发窗口)。
    try:
        from browser_search_channel import search_via_browser
        register_source("browser_bing", {
            "desc": "借道本地浏览器翻墙搜索(bing)·AnySearch降级通道·无次数限制但慢",
            "domains": ["overseas", "news", "sports", "any"],
            "input": "query_str",
            "cost_usd_per_call": 0.0,
            "daily_quota": 999999,
            "compliance": "browser_local",
            "tier": 2,  # fallback 级·主力(anysearch/wiki)空时才降级用
            "avg_latency_s": 8.0,
            "needs_key": False,
        }, search_via_browser)
        count += 1
    except Exception:
        pass  # 通道不可用不阻断其他源注册(fail-safe)

    return count


# 兼容·直接 import 时自动注册
def _auto_register_on_import():
    """在 import 时自动注册·避免需要显式调用"""
    try:
        auto_register_all()
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# CLI 自测
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="search_fabric_verticals 自测")
    ap.add_argument("query", nargs="?", default="SPFMV sweet potato feathery mottle virus")
    ap.add_argument("--source", default="openalex",
                    choices=list(_VERTICAL_SOURCES.keys()))
    ap.add_argument("--top_k", type=int, default=5)
    args = ap.parse_args()

    src = _VERTICAL_SOURCES.get(args.source)
    if not src:
        print(f"Unknown source: {args.source}")
        sys.exit(1)

    print(f"🔍 [{args.source}] query: {args.query}\n")
    t0 = time.time()
    hits = src["callable"](args.query, top_k=args.top_k)
    elapsed = time.time() - t0

    errors = [h for h in hits if h.get("_error")]
    valids = [h for h in hits if not h.get("_error")]
    print(f"✅ {len(valids)} hits, {len(errors)} errors | {elapsed:.2f}s\n")
    for i, h in enumerate(valids[:args.top_k], 1):
        print(f"  [{i}] {h.get('title','')[:70]}")
        print(f"       {h.get('url','')[:80]}")
        print(f"       {h.get('content','')[:150]}...")
        print()
    if errors:
        print(f"  ERRORS: {errors}")

    # 注册测试
    n = auto_register_all()
    print(f"\n✅ 注册了 {n} 个垂直源到 search_fabric")
