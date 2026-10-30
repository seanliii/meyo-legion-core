# -*- coding: utf-8 -*-
"""feature_to_entity · 特征→实体反查(KM2782350173 层3·冷启动核心·烧进引擎)

KM 盲区3(★★★★★): "6 shots/4 yellow cards/4 corners"、"BPM 194 B Minor"、
"ice age/SAR ratio" 这类【数值/属性特征】无法用全文关键词反查具体实体——它们不是
搜索关键词·而是【结构化查询条件】。冷启动第一跳的核心难点。

本模块把描述特征转成两条路:
  1. 结构化查询: 有对应结构化源时·把特征转成源的查询语法(musicbrainz lucene:
     date:/tracks:·wikidata SPARQL·crossref 年份+作者)。
  2. LLM 联想: 特异度不足/无结构化源时·让 LLM 做"描述特征→可能实体"的联想
     (如"英格兰2019水瓶点球小抄"→门将 Pickford·这是常识联想·非全文匹配能做)。
     这直接治水瓶题——gold=Jordan Pickford 靠联想"水瓶+点球=皮克福德习惯"得到·
     不是靠搜"water bottle scuffle"。

铁律: 纯生成查询/联想 query·不自己发搜索(发射交调用方闭包)·fail-safe·
env MEYO_FEATURE_TO_ENTITY=0 关·不透 gold(LLM 联想 prompt 不含答案·只含题面特征)。
"""
import os
import re


def _enabled():
    return os.environ.get("MEYO_FEATURE_TO_ENTITY", "1") != "0"


# 数值特征模式(通用·非题目特判)。抽出 (名称, 值, 单位/类型)。
_NUM_PATTERNS = [
    (r"(\d+)\s*shots?\s*(?:on goal)?", "shots"),
    (r"(\d+)\s*yellow cards?", "yellow_cards"),
    (r"(\d+)\s*red cards?", "red_cards"),
    (r"(\d+)\s*corners?(?:\s*kicks?)?", "corners"),
    (r"(\d+)\s*goals?", "goals"),
    (r"(\d+(?:\.\d+)?)\s*bpm", "bpm"),
    (r"(\d+)\s*tracks?", "tracks"),
    (r"(\d+)\s*(?:songs?|titles?)", "songs"),
    (r"\b(1[89]\d{2}|20[0-3]\d)\b", "year"),
    (r"(\d+(?:\.\d+)?)\s*%", "percent"),
    (r"(\d+(?:\.\d+)?)\s*(?:miles?|km|meters?|metres?)", "distance"),
    (r"molecular weight\s*(?:of\s*)?(\d+(?:\.\d+)?)", "mol_weight"),
    (r"\b([A-G](?:\s*(?:sharp|flat|#|b))?\s*(?:major|minor))\b", "musical_key"),
]


def extract_numeric_features(text):
    """从题面抽数值/属性特征。返回 [{"name","value"} ...]。fail-safe → []。"""
    feats = []
    try:
        t = str(text or "")
        for pat, name in _NUM_PATTERNS:
            for m in re.finditer(pat, t, re.I):
                feats.append({"name": name, "value": m.group(1).strip()})
        # 去重
        seen, out = set(), []
        for f in feats:
            k = (f["name"], f["value"].lower())
            if k not in seen:
                seen.add(k)
                out.append(f)
        return out
    except Exception:
        return []


def build_structured_queries(features, domain):
    """把特征转成结构化源的查询语法(有对应源时)。返回 [{"source","query","kind"} ...]。
    通用·只对已知结构化源生成·fail-safe → []。"""
    out = []
    try:
        fd = {f["name"]: f["value"] for f in (features or [])}
        if domain == "music":
            # musicbrainz lucene: date:YYYY·曲目数
            parts = []
            if "year" in fd:
                parts.append("date:%s" % fd["year"])
            if "tracks" in fd:
                parts.append("tracks:%s" % fd["tracks"])
            if parts:
                out.append({"source": "musicbrainz",
                            "query": " AND ".join(parts), "kind": "lucene"})
        elif domain == "chemistry":
            if "mol_weight" in fd:
                out.append({"source": "pubchem",
                            "query": "molecular weight %s" % fd["mol_weight"],
                            "kind": "attr"})
        # 足球等无本地结构化源→交给 LLM 联想(下面 llm_associate)
        return out
    except Exception:
        return []


def llm_associate_entity(question, features, llm_call, *, domain=""):
    """★LLM 联想:描述特征→可能的实体(治水瓶题类·常识联想非全文匹配)。
    例:"英格兰2019足球比赛·水瓶·点球冲突"→LLM联想"皮克福德有水瓶贴点球小抄习惯"→
    候选实体 Jordan Pickford。这是全文搜索做不到、但 LLM 常识能做的关键一跳。
    返回候选实体名列表(不透 gold·prompt 只给题面特征)。fail-safe → []。"""
    try:
        if llm_call is None:
            return []
        _feat = "、".join("%s=%s" % (f["name"], f["value"]) for f in (features or [])[:8])
        prompt = (
            "下面是一道多跳检索题的题面。这类题的突破口是【识别题面里最独特的细节·联想到它"
            "标志性关联的具体实体】。请像一个知识渊博的人那样思考:\n"
            "  ① 题面里哪个细节【最独特/最反常/最像某个著名事件或某人的标志性特征】?\n"
            "     (例:'守门员在水瓶上贴对手点球习惯'→这是某著名门将的标志性做法;\n"
            "      '五岁弹钢琴给某人听'→某音乐人的著名早年经历)\n"
            "  ② 这个独特细节【最可能指向哪个具体的人/球队/作品/地点】?给出真实的专有名词。\n"
            "  ③ 大胆给出你最可能的【具体实体名】·哪怕不完全确定——它们是下一步精确检索的锚点。\n"
            "  ★关键警觉(出题人视角): 题面的线索是出题人【精心挑来指向唯一实体】的·答案"
            "【很可能不是你第一时间想到的最有名的那个】——最有名的往往被某条线索排除了(否则"
            "题目没区分度)。所以【别只列最知名的】: 想想【哪些线索是具体传记细节/罕见属性】"
            "(如具体人生经历、家庭身世、与某团体/人物的关联)·优先列出【同时符合这些独特细节的"
            "候选·哪怕它比较小众、你不太熟】。宁可多列几个小众的·别漏掉那个真正符合全部细节的。\n"
            "不要回答最终答案·只列出【最可能值得去搜的具体实体名】(每行一个·最多8个·"
            "必须是真实存在的专有名词人名/队名/作品名·不要列'racism''penalty shootout'这类"
            "抽象词或题面已有的通用词)。\n\n"
            "题面: %s\n数值特征: %s\n领域: %s\n\n最可能的具体锚点实体(每行一个·专有名词):"
            % (str(question or "")[:600], _feat, domain or "未知"))
        resp = llm_call(prompt)
        cands = []
        # 实体名判据(真e2e实证:LLM有时回解释句"这道题信息不完整..."被当实体名发搜)
        _bad = ("候选", "锚点", "实体", "note", "example", "这道题", "信息", "联想",
                "不完整", "可以", "题面", "轮", "细节", "based on", "let me", "i can",
                "首先", "接下来", "因此", "answer")
        for line in str(resp or "").splitlines():
            s = re.sub(r"^[\d\.\-\*、·\s]+", "", line).strip()
            s = re.sub(r"[（(].*?[）)]", "", s).strip()   # 去括号注释
            sl = s.lower()
            _ok = (2 <= len(s) <= 50 and not any(w in sl for w in _bad)
                   and not any(p in s for p in ("，", "。", "；", ": "))
                   and not ("," in s and len(s) > 30))
            if _ok:
                cands.append(s)
        # 去重保序
        seen, out = set(), []
        for c in cands:
            if c.lower() not in seen:
                seen.add(c.lower())
                out.append(c)
        return out[:8]
    except Exception:
        return []


def convert(question, *, domain="", llm_call=None):
    """特征→实体总入口。返回 {
      "features":[...], "structured_queries":[...], "assoc_entities":[...],
      "advice":str}。fail-safe → 空。"""
    res = {"features": [], "structured_queries": [], "assoc_entities": [], "advice": ""}
    try:
        if not _enabled():
            return res
        feats = extract_numeric_features(question)
        res["features"] = feats
        res["structured_queries"] = build_structured_queries(feats, domain)
        # LLM 联想(治无结构化源的冷启动·如足球水瓶题)
        res["assoc_entities"] = llm_associate_entity(
            question, feats, llm_call, domain=domain)
        _bits = []
        if res["structured_queries"]:
            _bits.append("用结构化查询: %s" % "; ".join(
                "%s→%s" % (q["source"], q["query"]) for q in res["structured_queries"]))
        if res["assoc_entities"]:
            _bits.append("先搜这些【联想锚点实体】(别再搜整句描述): %s"
                         % "、".join(res["assoc_entities"][:5]))
        if _bits:
            res["advice"] = ("[特征→实体反查] 题面的数值/属性特征(%s)不该当全文关键词撒网。"
                             "%s。" % (
                                 "、".join("%s=%s" % (f["name"], f["value"]) for f in feats[:5]),
                                 "。".join(_bits)))
        return res
    except Exception:
        return res
