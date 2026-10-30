# -*- coding: utf-8 -*-
"""Entity-Drift & Low-Confidence Guard · bc 足球题全链路诊断落地 (2026-08-19)
=============================================================================

病根(读真 agent_response 铁证·/tmp/football_trace.txt + /tmp/bc_chapulin.log):
  bc 足球题 GOLD=FC Krasnodar·agent 交 Rennes。前期链路全对(母队被多源确证为
  Arsenal·证据里正确信号更强)·但收尾时:
  - P0-a 实体漂移: 收敛时 agent 自述 "on loan from **Celtic**"——中间确证的母队
    Arsenal 在收敛时被覆盖成 Celtic → Celtic 对手 = Rennes → 交 Rennes。
    【中间确证的正确实体在收敛时被错实体覆盖】。
  - P0-b 低置信硬交: agent 自标 Confidence 20%·明说 "could not fully confirm"/
    "not confirmed"·但墙钟到了仍硬交自己都不信的答案(P1-c 墙钟仓促同源)。

本模块 = 纯逻辑检测原语(全部可单测·零 LLM 成本·全 fail-safe)。编排/注入由
native_agent_loop.py 的 finalize 门 + _run_multipath 消费。

设计原则:
  1. 只检测·不代答: 实体漂移只比对"答案实体 vs 已确证证据实体"·从不告诉 agent
     正确答案。低置信只判"agent 自己说不确定"·不注入答案。
  2. fail-safe: 任何异常一律退回"不干预"(不拦答题/不弄丢答案/不死循环)。
  3. 有限次: 拦截由 caller 计数封顶(防死循环)。
  4. GAIA 不误伤: caller 只在【非结果型/有已确证实体/agent 自认低置信】时介入。
"""
import os
import re


# ═══════════════════════════════════════════════════════════════════════════
# P0-b · 低置信自述检测
# ═══════════════════════════════════════════════════════════════════════════

# agent 自认不确定的措辞(中英)。命中 = agent 明说没把握。
_LOW_CONF_PHRASES = re.compile(
    r"(?i)"
    r"not\s+(?:fully\s+)?(?:confirmed|verified|certain|sure|confident)"
    r"|could\s+not\s+(?:fully\s+)?(?:confirm|verify|determine|establish)"
    r"|couldn'?t\s+(?:fully\s+)?(?:confirm|verify|determine)"
    r"|unable\s+to\s+(?:fully\s+)?(?:confirm|verify|determine)"
    r"|cannot\s+(?:fully\s+)?(?:confirm|verify|be\s+certain)"
    r"|(?:low|limited|weak)\s+confidence"
    r"|best[\s-]effort(?:\s+(?:answer|guess))?"
    r"|(?:this\s+is\s+(?:a|my))\s+(?:tentative|uncertain|speculative)"
    r"|tentative(?:ly)?|speculativ|educated\s+guess|not\s+entirely\s+sure"
    r"|remains?\s+(?:unconfirmed|uncertain)|with\s+(?:some\s+)?uncertainty"
    r"|未(?:能)?(?:完全)?(?:确认|证实|核实)|无法(?:完全)?(?:确认|证实|核实)"
    r"|不(?:太)?确定|置信(?:度)?(?:较)?低|把握不大|存疑|暂(?:不能|无法)确认"
)

# 显式置信数值: "Confidence: 20%" / "confidence 0.2" / "置信度: 20%"
_CONF_NUM = (
    re.compile(r"(?i)confidence\s*[:：]?\s*(\d{1,3})\s*%"),
    re.compile(r"(?i)confidence\s*[:：]?\s*(0?\.\d+)"),
    re.compile(r"置信(?:度)?\s*[:：]?\s*(\d{1,3})\s*%"),
    re.compile(r"置信(?:度)?\s*[:：]?\s*(0?\.\d+)"),
)


def parse_confidence(text):
    """从文本抽 agent 自述的置信数值·归一到 [0,1]。返回 float 或 None(没写)。

    "Confidence: 20%" → 0.2 · "confidence 0.2" → 0.2 · 无 → None。fail-safe → None。
    """
    try:
        t = str(text or "")
        if not t:
            return None
        for pat in _CONF_NUM:
            m = pat.search(t)
            if not m:
                continue
            raw = m.group(1)
            v = float(raw)
            if "%" in m.group(0) or v > 1.0:
                v = v / 100.0
            return max(0.0, min(1.0, v))
        return None
    except Exception:
        return None


def is_low_confidence(text, threshold=0.5):
    """判断收尾文本是否【低置信】(agent 自认没把握)。返回 (low: bool, reason: str)。

    两条独立信号(任一命中即低置信):
      ① 显式置信数值 < threshold(如 "Confidence: 20%" 且 threshold=0.5)。
      ② 自认不确定措辞(not confirmed / could not verify / 未确认 …)。
    fail-safe → (False, "")(异常绝不判低置信·不误拦)。
    """
    try:
        t = str(text or "")
        if len(t.strip()) < 8:
            return False, ""
        try:
            thr = float(threshold)
        except Exception:
            thr = 0.5
        conf = parse_confidence(t)
        if conf is not None and conf < thr:
            return True, "self_confidence=%.2f<%.2f" % (conf, thr)
        m = _LOW_CONF_PHRASES.search(t)
        if m:
            return True, "uncertain_phrase:%s" % (m.group(0)[:40])
        return False, ""
    except Exception:
        return False, ""


# ═══════════════════════════════════════════════════════════════════════════
# P0-a · 关键主体实体确证追踪 + 漂移检测
# ═══════════════════════════════════════════════════════════════════════════
#   思路: 前期证据里【多源确证的关键主体实体】(母队/机构/人名这种·如 Arsenal 被
#   多次提到)存进 session["_confirmed_key_entities"]。finalize 抽答时·若答案的
#   关键主体实体与已确证实体【漂移】(Arsenal → Celtic)→ 告警。这【不比对答案本身】
#   (Krasnodar vs Rennes)·只比对答案里出现的【中间主体实体】是否与前期确证一致。

# 语义信号词: 引出"母队/所属/来源实体"的短语(通用·非题面特判)。命中后紧跟的
#   专名 = 被断言的关键主体实体。
_PARENT_ENTITY_CUES = (
    re.compile(r"(?i)on\s+loan\s+from\s+([A-Z][\w.&'-]+(?:\s+[A-Z][\w.&'-]+){0,3})"),
    re.compile(r"(?i)loaned\s+from\s+([A-Z][\w.&'-]+(?:\s+[A-Z][\w.&'-]+){0,3})"),
    re.compile(r"(?i)parent\s+club\s+(?:is\s+|was\s+)?([A-Z][\w.&'-]+(?:\s+[A-Z][\w.&'-]+){0,3})"),
    re.compile(r"(?i)owned\s+by\s+([A-Z][\w.&'-]+(?:\s+[A-Z][\w.&'-]+){0,3})"),
    re.compile(r"(?i)belongs?\s+to\s+([A-Z][\w.&'-]+(?:\s+[A-Z][\w.&'-]+){0,3})"),
    re.compile(r"(?i)signed\s+(?:by|for)\s+([A-Z][\w.&'-]+(?:\s+[A-Z][\w.&'-]+){0,3})"),
    re.compile(r"(?i)contract(?:ed)?\s+(?:with|at)\s+([A-Z][\w.&'-]+(?:\s+[A-Z][\w.&'-]+){0,3})"),
    re.compile(r"母队(?:是|为)?\s*([A-Za-z一-鿿][\w.&'一-鿿-]{1,30})"),
    re.compile(r"隶属(?:于)?\s*([A-Za-z一-鿿][\w.&'一-鿿-]{1,30})"),
)


def _norm(e):
    """实体归一(复用 identity_coverage 风格·抗大小写/空白)。fail-safe。"""
    try:
        import identity_coverage as _ic
        n = _ic._norm_entity(e)
        if n:
            return n
    except Exception:
        pass
    try:
        return re.sub(r"\s+", " ", str(e).strip().lower())[:120]
    except Exception:
        return str(e)[:120]


def _entities_match(a, b):
    """两实体是否【同指】(别名/简称容忍)。规则(保守·宁可判不同不误判相同):
      · 归一后完全相等 → 匹配;
      · 一方是另一方的【词级子集】且共享 >=1 个显著词(len>=3) → 匹配
        (Arsenal ↔ Arsenal F.C. / FC Krasnodar ↔ Krasnodar)。
    fail-safe → False。"""
    try:
        na, nb = _norm(a), _norm(b)
        if not na or not nb:
            return False
        if na == nb:
            return True
        wa = set(w for w in re.findall(r"[\w一-鿿]+", na) if len(w) >= 3)
        wb = set(w for w in re.findall(r"[\w一-鿿]+", nb) if len(w) >= 3)
        if not wa or not wb:
            return False
        _stop = {"the", "club", "team", "and"}
        wa2, wb2 = wa - _stop, wb - _stop
        if not wa2 or not wb2:
            return False
        if wa2 <= wb2 or wb2 <= wa2:
            return True
        return False
    except Exception:
        return False


def extract_asserted_entities(text):
    """从一段文本抽【被语义线索引出的关键主体实体】(母队/所属/来源)。
    返回去重后的实体列表(原文形态)。fail-safe → []。

    只抽被 _PARENT_ENTITY_CUES 引出的专名(通用信号·非题面特判)——这些是"agent
    正在断言的中间主体实体"·正是漂移会发生的地方。
    """
    try:
        t = str(text or "")
        if len(t) < 8:
            return []
        out, seen = [], set()
        for pat in _PARENT_ENTITY_CUES:
            for m in pat.finditer(t):
                ent = (m.group(1) or "").strip().strip(".,;:'\"")
                if len(ent) < 2:
                    continue
                k = _norm(ent)
                if k and k not in seen:
                    seen.add(k)
                    out.append(ent)
        return out[:20]
    except Exception:
        return []


def observe_key_entities(session, text, min_sources=2):
    """L1 追踪钩子: 从证据/工具文本抽被语义线索引出的关键主体实体·计其出现来源数·
    达 min_sources(默认 2·多源确证)锁进 session["_confirmed_key_entities"]。

    存储结构: {norm_key: {"entity": 原文, "count": 出现次数}}。
    静默(不注入 agent)·fail-safe。返回锁定实体数(便于观测)。
    """
    try:
        if not isinstance(session, dict):
            return 0
        ents = extract_asserted_entities(text)
        if not ents:
            return 0
        store = session.setdefault("_confirmed_key_entities", {})
        for e in ents:
            k = _norm(e)
            if not k:
                continue
            rec = store.get(k) or {"entity": e, "count": 0}
            rec["count"] = int(rec.get("count") or 0) + 1
            if len(e) > len(str(rec.get("entity") or "")):
                rec["entity"] = e
            store[k] = rec
        if len(store) > 64:
            for k in sorted(store, key=lambda x: store[x].get("count", 0))[:16]:
                store.pop(k, None)
        return sum(1 for r in store.values() if int(r.get("count") or 0) >= int(min_sources))
    except Exception:
        return 0


def get_confirmed_entities(session, min_sources=2):
    """读【已确证(多源)】关键主体实体列表 [(entity, count), …]·按 count 降序。
    fail-safe → []。"""
    try:
        store = (session or {}).get("_confirmed_key_entities") or {}
        try:
            ms = int(min_sources)
        except Exception:
            ms = 2
        out = [(r.get("entity"), int(r.get("count") or 0))
               for r in store.values() if int(r.get("count") or 0) >= ms]
        out.sort(key=lambda x: x[1], reverse=True)
        return out
    except Exception:
        return []


def detect_entity_drift(session, final_text, min_sources=2):
    """P0-a 核心: finalize 抽答时·检测答案里断言的关键主体实体是否与【已确证】实体漂移。

    逻辑:
      1. 从 final_text 抽被语义线索引出的关键主体实体(agent 收尾时正断言的中间实体·
         如 "on loan from Celtic" → Celtic)。
      2. 无已确证实体 → 不干预(fail-safe·GAIA 无中间主体实体的题天然不触发)。
      3. final 断言了某主体实体 X·且已确证实体集非空·且 X 不匹配任何已确证实体 →
         drift(附最强确证实体供回退)。保守: 只要有一个断言实体漂出确证集就告警。
      返回 {"drift": bool, "asserted": [X…], "confirmed": [(e,c)…],
             "expected": 最强确证实体 or "", "reason": str}。fail-safe → drift=False。
    """
    empty = {"drift": False, "asserted": [], "confirmed": [],
             "expected": "", "reason": ""}
    try:
        confirmed = get_confirmed_entities(session, min_sources=min_sources)
        if not confirmed:
            return empty
        asserted = extract_asserted_entities(final_text)
        if not asserted:
            return empty
        conf_ents = [c[0] for c in confirmed]
        drifted = []
        for a in asserted:
            if not any(_entities_match(a, ce) for ce in conf_ents):
                drifted.append(a)
        if not drifted:
            return {**empty, "asserted": asserted, "confirmed": confirmed}
        return {"drift": True, "asserted": asserted, "confirmed": confirmed,
                "expected": conf_ents[0] if conf_ents else "",
                "reason": ("答案断言主体实体 %r 与已多源确证实体 %r 不一致(疑似收敛时实体漂移)"
                           % (drifted[:3], conf_ents[:3]))}
    except Exception:
        return empty


# ═══════════════════════════════════════════════════════════════════════════
# 开关 / 阈值(env)
# ═══════════════════════════════════════════════════════════════════════════
def entity_drift_guard_enabled():
    return os.environ.get("MEYO_ENTITY_DRIFT_GUARD", "1").strip().lower() \
        not in ("0", "false", "off", "no")


def low_conf_no_submit_enabled():
    return os.environ.get("MEYO_LOW_CONF_NO_SUBMIT", "1").strip().lower() \
        not in ("0", "false", "off", "no")


def conf_submit_min():
    try:
        return float(os.environ.get("MEYO_CONF_SUBMIT_MIN", "0.5"))
    except Exception:
        return 0.5


def min_confirm_sources():
    try:
        return int(os.environ.get("MEYO_ENTITY_CONFIRM_SOURCES", "2"))
    except Exception:
        return 2


def max_guard_blocks():
    """漂移+低置信门合计最多拦几次(防死循环)。默认 2。"""
    try:
        return int(os.environ.get("MEYO_GUARD_MAX_BLOCKS", "2"))
    except Exception:
        return 2


if __name__ == "__main__":  # 冒烟自测
    # ── P0-b 低置信 ──
    low, r = is_low_confidence("Confidence: 20%\nAnswer: Rennes")
    assert low and "0.20" in r, (low, r)
    low, r = is_low_confidence("I could not fully confirm this. Answer: Rennes")
    assert low, (low, r)
    low, r = is_low_confidence("未能完全确认，答案: Rennes")
    assert low, (low, r)
    low, r = is_low_confidence("Answer: 42. This is well established by three sources.")
    assert not low, (low, r)
    low, r = is_low_confidence("Confidence: 95%\nAnswer: Paris")
    assert not low, (low, r)
    assert abs(parse_confidence("Confidence: 20%") - 0.2) < 1e-6
    assert abs(parse_confidence("confidence 0.85") - 0.85) < 1e-6
    assert parse_confidence("no number here") is None

    # ── P0-a 实体漂移 ──
    sess = {}
    observe_key_entities(sess, "Bielik is on loan from Arsenal, per source A.")
    observe_key_entities(sess, "He was loaned from Arsenal (confirmed by source B).")
    ce = get_confirmed_entities(sess)
    assert ce and "arsenal" in _norm(ce[0][0]), ce
    d = detect_entity_drift(sess, "The player is on loan from Celtic, so opponent = Rennes.")
    assert d["drift"], d
    assert "arsenal" in _norm(d["expected"]), d
    d2 = detect_entity_drift(sess, "on loan from Arsenal F.C.; opponent = X")
    assert not d2["drift"], d2
    assert _entities_match("Arsenal", "Arsenal F.C.")
    assert _entities_match("FC Krasnodar", "Krasnodar")
    assert not _entities_match("Arsenal", "Celtic")
    d3 = detect_entity_drift({}, "on loan from Celtic")
    assert not d3["drift"], d3
    d4 = detect_entity_drift(sess, "The answer is 42.")
    assert not d4["drift"], d4
    print("ENTITY_DRIFT_LOWCONF_GUARD SMOKE PASS")
