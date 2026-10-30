# -*- coding: utf-8 -*-
"""domain_source_router · 垂类源发现机制(KM2782350173 层1·最大盲区·烧进引擎)

KM 诊断的最大盲区(★★★★★): Agent 不知道"足球统计该去 StatsBomb、音乐 BPM 该去
MusicBrainz、学术论文该去 Scholar"——所有 query 都走通用全文搜索(ddgs+wikipedia)·
对结构化/垂类信息命中率低。

本模块 = 领域识别 → 映射到【真实已注册的垂类源】 → 提示 agent 优先用对的源。
不硬编码"足球→StatsBomb"(StatsBomb 本地没注册·映射不存在的源=假接线)·而是映射到
search_fabric 里【真实存在】的结构化源:
  music      → musicbrainz(年份+曲目数精确反查)
  academic   → crossref/openalex/arxiv/sh_scholar(论文/引用/DOI)
  biomedical → pubmed
  chemistry  → pubchem(分子式/分子量反查)
  legal      → courtlistener(判例/法律)
  biography/geography/facts/entities → wikidata(SPARQL 结构化实体·按属性查)
  news/events → gdelt(事件/时间线)
  archive/old_page → wayback/archive_today

铁律(项目宪法+指导思想):
  1. 只映射到【真实已注册】的源(可 probe 验证)·不假接不存在的源。
  2. 关键词命中给方向提示·不强制(agent 仍可用通用源)·env MEYO_DOMAIN_ROUTER=0 关。
  3. 纯函数·fail-safe·不透 gold(只按题面领域词判·不判答案)。
  4. 支持自动发现新领域(auto_discover 留接口·但默认走已注册源·不联网乱注册)。
"""
import os
import re

# 领域 → (关键词, 真实已注册的结构化源列表, 该领域为何用这些源的理由)。
#   源名必须是 search_fabric SOURCE_REGISTRY 里真实存在的(经 domains 字段核对)。
DOMAINS = {
    "music": {
        "keywords": ["song", "album", "artist", "band", "track", "bpm", "genre",
                     "billboard", "spotify", "discography", "single", "melody",
                     "音乐", "专辑", "歌曲", "乐队", "曲目"],
        "sources": ["musicbrainz"],
        "why": "MusicBrainz 可按年份+曲目数+艺人精确反查专辑/录音(结构化·非全文)",
    },
    "academic": {
        "keywords": ["paper", "journal", "citation", "doi", "phd", "fellow",
                     "ieee", "acm", "publication", "author", "arxiv", "preprint",
                     "论文", "期刊", "引用", "学者", "研究"],
        "sources": ["crossref", "openalex", "arxiv", "sh_scholar"],
        "why": "Crossref/OpenAlex/arXiv 按 DOI/作者/年份结构化查论文·比全文准",
    },
    "biomedical": {
        "keywords": ["clinical", "disease", "drug", "gene", "protein", "trial",
                     "pharmacology", "medicine", "patient", "临床", "疾病", "药"],
        "sources": ["pubmed"],
        "why": "PubMed 生物医学专库·按 MeSH/临床试验结构化查",
    },
    "chemistry": {
        "keywords": ["compound", "molecule", "chemical", "formula", "molecular weight",
                     "cas number", "smiles", "化合物", "分子", "化学式"],
        "sources": ["pubchem"],
        "why": "PubChem 按分子式/分子量/CAS 号精确反查化合物",
    },
    "legal": {
        "keywords": ["court", "lawsuit", "litigation", "judge", "ruling", "case law",
                     "plaintiff", "defendant", "statute", "法院", "судебн", "案件", "诉讼"],
        "sources": ["courtlistener"],
        "why": "CourtListener 美国判例/法律文书专库",
    },
    "news_events": {
        "keywords": ["news", "event", "incident", "announced", "reported", "happened",
                     "protest", "election", "新闻", "事件", "报道"],
        "sources": ["gdelt", "wikipedia"],
        "why": "GDELT 全球事件时间线·按时间/地点/事件类型查",
    },
    "biography_geo_facts": {
        "keywords": ["born", "died", "founded", "located", "population", "capital",
                     "author", "writer", "poet", "historian", "professor", "player",
                     "actor", "director", "team", "club", "city", "country", "mountain",
                     "出生", "作家", "球员", "城市", "国家", "创立"],
        "sources": ["wikidata", "wikipedia"],
        "why": "Wikidata SPARQL 按属性(出生地/职业/时间)结构化查实体·wikipedia 补全文",
    },
    "archive": {
        "keywords": ["blog post", "archived", "web archive", "old page", "wayback",
                     "deleted page", "dead link", "存档", "旧网页", "博客"],
        "sources": ["archive_today", "wayback_cdx"],
        "why": "Wayback/Archive.today 找已删除/历史版本网页",
    },
}


def _enabled():
    return os.environ.get("MEYO_DOMAIN_ROUTER", "1") != "0"


def _kw_score(text, keywords):
    """关键词命中数(小写子串匹配·通用启发)。返回命中个数。"""
    tl = str(text or "").lower()
    return sum(1 for kw in keywords if kw.lower() in tl)


def classify(question, top_k=3):
    """识别题目领域·返回按命中降序的领域列表(带推荐源)。
    返回 [{"domain","score","sources","why"} ...]。fail-safe → []。"""
    try:
        if not _enabled():
            return []
        out = []
        for did, info in DOMAINS.items():
            sc = _kw_score(question, info["keywords"])
            if sc >= 1:
                out.append({"domain": did, "score": sc,
                            "sources": info["sources"], "why": info["why"]})
        out.sort(key=lambda x: -x["score"])
        return out[:top_k]
    except Exception:
        return []


def recommend_sources(question, *, llm_call=None):
    """给 agent 的源推荐(去重·保序·只含真实已注册源)。
    返回 {"sources":[...], "advice":str, "domains":[...]}。fail-safe → 空。

    ★2026-08-20(灵魂驾驶员·语义兜底): 关键词表 classify 没命中时(题面不含预设领域词=
    写死关键词的盲区)·用驾驶员题型感知做【语义领域判断】给方向提示——变量而非死关键词。
    传 llm_call 才启用语义兜底·不传则退回纯关键词(向后兼容·不卡)。"""
    res = {"sources": [], "advice": "", "domains": []}
    try:
        if not _enabled():
            return res
        matches = classify(question)
        if not matches:
            # ★语义兜底: 关键词没命中·问驾驶员这题什么领域(带缓存·同题一次)
            if llm_call is not None:
                try:
                    import perspective_engine as _pe_dsr
                    if _pe_dsr._enabled():
                        # ★优雅收敛:走统一 decide(PHASE_PARADIGM)(底层复用sense_topic_and_paradigm)
                        _snap_dsr = _pe_dsr.ContextSnapshot(question, llm_call=llm_call)
                        _tp = _pe_dsr.decide(_snap_dsr, _pe_dsr.PHASE_PARADIGM)
                        _topic = str(_tp.get("topic") or "").strip()
                        _para = str(_tp.get("paradigm") or "").strip()
                        if _topic and _topic != "general":
                            res["domains"] = [_topic]
                            res["advice"] = (
                                "[垂类源发现·语义识别] 关键词没命中预设领域·但驾驶员判断这题属于"
                                "【%s】领域。别只用通用全文搜索——按这个领域的范式找【对应的结构化/"
                                "权威源】: %s" % (_topic, _para[:160]))
                            return res
                except Exception:
                    pass
            return res
        seen, srcs, whys, doms = set(), [], [], []
        for m in matches:
            doms.append(m["domain"])
            for s in m["sources"]:
                if s not in seen:
                    seen.add(s)
                    srcs.append(s)
            whys.append("%s(%s)" % (m["domain"], m["why"]))
        res["sources"] = srcs
        res["domains"] = doms
        res["advice"] = (
            "[垂类源发现] 这题看起来属于【%s】领域。别只用通用全文搜索(ddgs/wikipedia)——"
            "优先用结构化垂类源: %s。理由: %s。结构化源能【按属性反查】(如按年份/曲目数/"
            "分子式/出生地精确查)·比描述撒网命中率高得多。"
            % ("、".join(doms), "、".join(srcs), whys[0] if whys else ""))
        return res
    except Exception:
        return res


def probe_source_available(source_id):
    """探测源是否真实可用(KM Step2·源可用性检测)。真跑一次 probe。
    返回 'AVAILABLE'|'EMPTY'|'ERROR'|'NOT_REGISTERED'。fail-safe。"""
    try:
        import sys
        # (2026-08-27 瘦身·删硬编码绝对路径) 上线独立部署必崩的旧仓写死路径已改为相对本文件根。
        #   search_fabric* 就在本仓 seed-users-system/v11/evolution·MEYO_V11_DIR 可覆盖。
        _v11 = os.environ.get("MEYO_V11_DIR") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "v11", "evolution")
        if _v11 not in sys.path:
            sys.path.insert(0, _v11)
        try:
            from search_fabric_verticals import auto_register_all as _reg
            _reg()   # 确保垂直源已注册(否则全 NOT_REGISTERED)
        except Exception:
            pass
        import search_fabric as _sf
        if source_id not in _sf.SOURCE_REGISTRY:
            return "NOT_REGISTERED"
        fn = _sf._SOURCE_CALLABLES.get(source_id)
        if fn is None:
            return "NOT_REGISTERED"
        r = fn("test", 1)
        if isinstance(r, list) and r and not (isinstance(r[0], dict) and r[0].get("_error")):
            return "AVAILABLE"
        return "EMPTY"
    except Exception:
        return "ERROR"
