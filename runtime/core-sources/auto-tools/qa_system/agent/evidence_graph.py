# -*- coding: utf-8 -*-
"""evidence_graph · 带 provenance 的三元组证据图(烧进引擎·纯 Python·无外部依赖)

来自用户 /goal(Kimi 方案·Graph-Based Memory with Provenance Tracking):
  核心改变 = 不是"搜索完再提取"·而是"每搜一步就结构化存入图·下一步从图里查·
  收敛时做图遍历"。升级现有扁平 Entity Bank(name/hits/sources) → 三元组图:
    Edge: (subject, relation, object, source, confidence, valid_from)
    例: (Bielik, loan_from, Arsenal, wikipedia_url, 0.95, step_3)

铁律(项目宪法 + 指导思想):
  1. 烧进引擎本体·不依赖 Neo4j/Graphiti(那违背"关掉 CC 能力还在"·且本地无 Neo4j)。
     用纯 Python dict/set 实现轻量有向图·够 BrowseComp 单题规模(<500 三元组)。
  2. provenance 必留: 每条边带 source(哪个工具/URL) + confidence + step(第几步落的)。
     "图上有边连接才是证据·路径最完整才是答案"——无 provenance 的边不算数。
  3. 落账即结构化: 工具结果一来就抽三元组入图(observe_triplets)·不事后正则。
  4. 收敛靠图遍历: find_paths 找连接约束的路径·路径完整(每边 conf>阈值)才算答案锚定。
  5. fail-safe·env MEYO_EVIDENCE_GRAPH=0 关(退回扁平 Entity Bank)。绝不透 gold。

存储: session["_evidence_graph"] = {
    "edges": [ {s,r,o,src,conf,step} ... ],       # 三元组边(有界)
    "nodes": { node_lower: {"name","degree","first_step"} },  # 实体节点
    "adj":   { node_lower: [edge_idx ...] },       # 邻接表(图遍历用)
}
"""
import os
import re

_MAX_EDGES = 500        # 单题三元组上限(防膨胀)
_MIN_CONF = 0.5         # 边最低置信(低于此不入图)
_PATH_CONF = 0.6        # 路径完整判据:每边 conf 需 >= 此
_MAX_GAP = 40           # 实体到关系词的最大字符距离(邻近约束·治跨从句乱配)


def _enabled():
    return os.environ.get("MEYO_EVIDENCE_GRAPH", "1") != "0"


def _norm(s):
    return str(s or "").strip().lower()


def _graph(session):
    if session is None:
        return None
    g = session.setdefault("_evidence_graph",
                           {"edges": [], "nodes": {}, "adj": {}})
    return g


# ── 三元组抽取(落账时·启发式·LLM 可选增强) ──────────────────────────────
# 关系触发词→规范关系(通用·非题目特判)。覆盖 BrowseComp 常见跳:归属/效力/
#   位置/时间/身份/获奖/参与。命中即抽一条 (前实体, 关系, 后实体)。
_REL_PATTERNS = [
    (r"\bon loan (?:from|at)\b", "loan_from"),
    (r"\bloaned (?:from|to)\b", "loan_from"),
    (r"\bplay(?:s|ed)? for\b", "plays_for"),
    (r"\bmember of\b", "member_of"),
    (r"\bborn in\b", "born_in"),
    (r"\bfounded\b", "founded"),
    (r"\bdirected by\b", "directed_by"),
    (r"\bwon\b", "won"),
    (r"\blocated in\b", "located_in"),
    (r"\bpart of\b", "part_of"),
    (r"\bagainst\b", "against"),
    (r"\bfeatured (?:on|in)\b", "featured_in"),
    (r"\bsigned (?:for|by|with)\b", "signed_for"),
    (r"\bmanaged by\b", "managed_by"),
    (r"\breleased in\b", "released_in"),
]

# 专名短语(实体节点候选)——首字母大写连续词/引号短语/显著数字(年份)。
_ENT_RE = re.compile(r"\b([A-Z][a-zA-Z.\-]{1,}(?:\s+[A-Z][a-zA-Z.\-]{1,}){0,3})\b")
_YEAR_RE = re.compile(r"\b(1[89]\d{2}|20[0-3]\d)\b")
_STOP_ENT = {
    "the", "this", "that", "these", "those", "search results", "retrieved",
    "wikipedia", "however", "according", "list of", "he", "she", "it", "they",
    "subject", "object", "relation", "entity", "candidate", "example", "format",
}
_META_RELATIONS = {"relation", "predicate", "property", "field", "column", "schema"}
_META_TEXT = re.compile(
    r"(?i)(?:subject\s*[,|\-]+\s*relation\s*[,|\-]+\s*object|"
    r"subject\s*->\s*relation\s*->\s*object|事实三元组|输出格式|只输出|format\s*:|"
    r"example\s*:|schema\s*:)")


def _is_meta_triplet(s, r, o):
    """过滤模型格式说明/占位符三元组，避免把抽取 prompt 本身写回证据图。"""
    try:
        vals = [_norm(s), _norm(r), _norm(o)]
        if not all(vals) or vals[0] == vals[2]:
            return True
        if vals[1] in _META_RELATIONS:
            return True
        if vals[0] in _STOP_ENT or vals[2] in _STOP_ENT:
            return True
        joined = " | ".join(vals)
        if _META_TEXT.search(joined):
            return True
        placeholders = {"subject", "relation", "object", "entity", "value", "name"}
        return sum(value in placeholders for value in vals) >= 1
    except Exception:
        return True


def _sentences(text):
    """粗切句(供逐句抽三元组)。fail-safe → []。"""
    try:
        t = re.sub(r"<[^>]{0,200}>", " ", str(text or ""))   # 去 HTML
        return [s.strip() for s in re.split(r"[.。;\n!?]+", t) if len(s.strip()) > 8][:60]
    except Exception:
        return []


def _entities_in(sent):
    """一句里的专名实体(去停用词/太短)。"""
    out = []
    try:
        for m in _ENT_RE.findall(sent):
            ml = _norm(m)
            if ml and ml not in _STOP_ENT and len(ml) >= 3 and not ml.isdigit():
                out.append(m.strip())
        for y in _YEAR_RE.findall(sent):
            out.append(y)
    except Exception:
        pass
    # 去重保序
    _seen, _o = set(), []
    for e in out:
        if _norm(e) not in _seen:
            _seen.add(_norm(e))
            _o.append(e)
    return _o


def _llm_extract_triplets(text, llm_call, *, source="", step=0, base_conf=0.75):
    """★LLM 语义抽三元组(真数据实证:纯正则在真搜噪声长文本上要么乱配要么漏抽·
    Kimi 方案原话 llm_extract_triplets)。有 llm_call 时用它·抽准得多。fail-safe → []。"""
    try:
        if llm_call is None:
            return []
        _snip = str(text or "")[:2500]
        if len(_snip) < 30:
            return []
        prompt = (
            "从下面文本抽取事实三元组(subject, relation, object)·只抽【文本明确陈述】的"
            "实体间关系(归属/效力/对阵/位置/时间/获奖/参与等)·不推断不脑补。每行一条·格式:\n"
            "subject | relation | object\n"
            "relation 用简短英文小写(如 plays_for/loan_from/against/born_in/located_in/won)。"
            "最多 12 条·只要专有名词实体(人名/球队/地名/机构/作品)·跳过代词和泛称。\n\n"
            "文本:\n" + _snip + "\n\n三元组:")
        resp = llm_call(prompt)
        out = []
        for line in str(resp or "").splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) == 3 and all(parts) and len(parts[0]) < 60 and len(parts[2]) < 60:
                s, r, o = parts
                r = r.lower().replace(" ", "_")[:30]
                if _norm(s) != _norm(o) and len(s) >= 2 and len(o) >= 2:
                    out.append((s, r, o, source[:80], base_conf, step))
        return out[:12]
    except Exception:
        return []


def extract_triplets(text, *, source="", step=0, base_conf=0.7, llm_call=None):
    """从一段文本抽三元组·落账即用。有 llm_call → LLM 语义抽取(真文本准);
    无/失败 → 严格启发式邻近抽取(干净短句可靠·真噪声文本保守)。fail-safe → []。"""
    # LLM 优先(真数据实证:真搜噪声文本正则不可靠·LLM 语义抽准)
    _llm = _llm_extract_triplets(text, llm_call, source=source, step=step)
    if _llm:
        return _llm
    triples = []
    try:
        for sent in _sentences(text):
            sl = sent.lower()
            ents = _entities_in(sent)
            if len(ents) < 2:
                continue
            for pat, rel in _REL_PATTERNS:
                m = re.search(pat, sl)
                if not m:
                    continue
                # 关系词位置·取词前最后一个实体作 subject·词后第一个作 object
                pos = m.start()
                # ★邻近约束(治真搜噪声文本跨从句乱配·如 Chelsea-against-EuropaLeague):
                #   subject=关系词前【最近】实体·object=词后【最近·且紧邻】实体·
                #   两者到关系词的字符距离都 < _MAX_GAP·中间不得跨过 从句连接词。
                subj = obj = None
                subj_pos = obj_pos = -1
                _end = m.end()
                for e in ents:
                    ei = sl.find(e.lower())
                    if ei < 0:
                        continue
                    _elen = len(e)
                    if ei + _elen <= pos:                 # 实体在关系词前
                        if ei > subj_pos:                 # 取最近(最靠右)
                            subj, subj_pos = e, ei
                    elif ei >= _end and obj is None:      # 词后第一个
                        obj, obj_pos = e, ei
                # 邻近判据: subject 紧贴关系词前·object【紧贴】词后(object gap 更严·
                #   治 'against another team in the Europa League' 把 Europa League 误当
                #   宾语——真宾语紧跟关系词·中间隔着 another team 等填充=不是真宾语)。
                _gap_s = pos - (subj_pos + len(subj)) if subj else 999
                _gap_o = obj_pos - _end if obj else 999
                # object 与关系词之间的填充文本: 若含名词性填充(another/other/the team/
                #   a team 等)说明真宾语被挡·作废(通用启发·非题目特判)。
                _seg_o = sl[_end:obj_pos] if obj else ""
                _filled = any(f in _seg_o for f in ("another", "other ", " team",
                                                    " side", " club", " player", " one"))
                # 从句边界: 关系词前隔着从句连接词→作废 subject
                _seg_s = sl[max(0, subj_pos):pos] if subj else ""
                _crossed = any(c in _seg_s for c in (" while ", " and ", " but ",
                                                     " which ", " where ", "，", "；"))
                if (subj and obj and _norm(subj) != _norm(obj)
                        and _gap_s <= _MAX_GAP and _gap_o <= 12
                        and not _crossed and not _filled):
                    triples.append((subj, rel, obj, source[:80], base_conf, step))
        # 去重(同 s-r-o 只留一条·conf 取高)
        best = {}
        for s, r, o, src, c, st in triples:
            k = (_norm(s), r, _norm(o))
            if k not in best or c > best[k][4]:
                best[k] = (s, r, o, src, c, st)
        return list(best.values())
    except Exception:
        return []


def observe_triplets(session, tool_result_text, *, source="", step=0, llm_call=None):
    """★工具结果落账时·抽三元组入图(核心:每搜一步就结构化存入图)。
    有 llm_call → LLM 语义抽取(真文本准);无 → 严格启发式。
    多源印证→conf 提升(同 s-r-o 再见 +0.1·上限 0.98)。fail-safe·静默。"""
    try:
        if not _enabled() or session is None:
            return 0
        g = _graph(session)
        trips = extract_triplets(tool_result_text, source=source, step=step,
                                 llm_call=llm_call)
        added = 0
        for s, r, o, src, conf, st in trips:
            if conf < _MIN_CONF or _is_meta_triplet(s, r, o):
                continue
            added += _add_edge(g, s, r, o, src, conf, st)
        # 有界
        if len(g["edges"]) > _MAX_EDGES:
            _prune(g)
        return added
    except Exception:
        return 0


def _add_edge(g, s, r, o, src, conf, step):
    """加边(同 s-r-o 已存在→多源印证提 conf·加 provenance)。返回新增条数(0/1)。"""
    sk, ok = _norm(s), _norm(o)
    # 查已存在的同 s-r-o
    for idx in g["adj"].get(sk, []):
        e = g["edges"][idx]
        if e["r"] == r and _norm(e["o"]) == ok:
            e["conf"] = min(0.98, e["conf"] + 0.1)   # 多源印证
            if src and src not in e.get("srcs", []):
                e.setdefault("srcs", []).append(src)
            return 0
    idx = len(g["edges"])
    g["edges"].append({"s": s, "r": r, "o": o, "srcs": [src] if src else [],
                       "conf": conf, "step": step})
    for n, name in ((sk, s), (ok, o)):
        rec = g["nodes"].get(n)
        if rec:
            rec["degree"] += 1
        else:
            g["nodes"][n] = {"name": name, "degree": 1, "first_step": step}
        g["adj"].setdefault(n, []).append(idx)
    return 1


def _prune(g):
    """裁边:保留高 conf + 高度数节点相关的边(有界防膨胀)。"""
    try:
        edges = sorted(enumerate(g["edges"]),
                       key=lambda x: -x[1].get("conf", 0))[:_MAX_EDGES]
        keep = {i for i, _ in edges}
        g["edges"] = [e for i, e in enumerate(g["edges"]) if i in keep]
        # 重建 adj/nodes
        g["adj"], g["nodes"] = {}, {}
        old = g["edges"]
        g["edges"] = []
        for e in old:
            _add_edge(g, e["s"], e["r"], e["o"],
                      (e.get("srcs") or [""])[0], e["conf"], e.get("step", 0))
    except Exception:
        pass


# ── 图遍历收敛(找连接约束的路径·路径完整才算锚定) ────────────────────────
def find_paths(session, start_entity, *, max_hops=3, min_conf=None):
    """从 start_entity 出发做 BFS·返回可达的 (终点节点, 路径边列表)。
    路径完整判据: 每条边 conf >= min_conf(默认 _PATH_CONF)。治高置信错锁:
    无完整路径连接的候选=没有图证据支撑=不该锁。fail-safe → []。"""
    out = []
    try:
        if not _enabled() or session is None:
            return out
        g = session.get("_evidence_graph") or {}
        if not g.get("edges"):
            return out
        mc = _PATH_CONF if min_conf is None else min_conf
        start = _norm(start_entity)
        if start not in g["adj"]:
            return out
        # BFS·记路径
        from collections import deque
        q = deque([(start, [])])
        seen = {start}
        while q:
            node, path = q.popleft()
            if len(path) >= max_hops:
                continue
            for idx in g["adj"].get(node, []):
                e = g["edges"][idx]
                if e["conf"] < mc:
                    continue
                # 无向遍历(边两端都能走)
                nxt = _norm(e["o"]) if _norm(e["s"]) == node else _norm(e["s"])
                if nxt in seen:
                    continue
                np = path + [e]
                out.append((g["nodes"].get(nxt, {}).get("name", nxt), np))
                seen.add(nxt)
                q.append((nxt, np))
        return out
    except Exception:
        return out


def graph_stats(session):
    """图规模(遥测/判据用)。返回 {nodes, edges, max_degree}。"""
    try:
        g = session.get("_evidence_graph") or {}
        deg = [r.get("degree", 0) for r in (g.get("nodes") or {}).values()]
        return {"nodes": len(g.get("nodes") or {}),
                "edges": len(g.get("edges") or []),
                "max_degree": max(deg) if deg else 0}
    except Exception:
        return {"nodes": 0, "edges": 0, "max_degree": 0}
