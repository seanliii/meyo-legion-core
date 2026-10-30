# -*- coding: utf-8 -*-
"""finalize_governor · 统一收尾治理入口(auto+native 一个纯函数·消灭双份接线)

多专家诊断 P0(统一大机制专家): native 与 auto 各写一份收尾治理门(约束对账→找对的→
实体漂移→低置信)·逻辑已对称但双份维护会漂移。此模块把这条链收敛成【一个纯函数】
govern(...)·两侧各调一次。记忆(Entity Bank)+搜索(找对的退化 suggest)+推理(约束
三态对账)+校验(实体漂移/低置信)统一到这里·灵活编排·无接线问题。

设计铁律:
  1. 纯函数·无副作用(不写 session·不 I/O)·只读入参·返回裁决 dict。调用方按裁决行动。
  2. fail-safe: 任一子机制异常都不崩·降级为"不干预"(pass=True·advice="")。
  3. 优先级: 约束矛盾(reject) > 实体漂移(drift) > 低置信(low_conf)。任一触发即给
     advice + 找对的候选(best)。都不触发 → pass=True(放行 finalize)。
  4. 结果型/数值题(GAIA)由调用方在【进入前】用 route/is_num_q 拦掉·此函数不管路由——
     保持通用·不硬编码题型。
  5. env 各子机制自带开关(constraint_ledger / entity_drift_lowconf_guard)·此处不重复。

返回: {
  "pass": bool,          # True=放行·False=需干预
  "reason": str,         # 干预原因(reject/drift/low_conf/"")
  "advice": str,         # 给模型的自然语言指导(注入下一轮)
  "best": str | None,    # 找对的:证据/Entity Bank里约束覆盖最高的替代候选
  "detail": dict,        # 子机制原始产出(审计用)
}
"""
import os


def _enabled():
    """总开关·=0 整体旁路(退回调用方原行为·此函数返回 pass=True)。"""
    return os.environ.get("MEYO_FINALIZE_GOVERNOR", "1") != "0"


def _find_best_by_graph(session, question, candidate, claims):
    """★图遍历找对的(第3层·治高置信错锁·收敛从文本语义→图遍历路径):
    从题面约束关键实体出发·在证据图上 BFS·找【路径完整(每边 conf 达标)且度数最高】
    的枢轴节点=图证据支撑最强的候选。'图上有边连接才是证据·路径最完整才是答案'。
    纯读·fail-safe·找不到 None。绝不透 gold(只按图结构选·不知谁对)。"""
    try:
        import evidence_graph as _eg
        if not _eg._enabled():
            return None
        st = _eg.graph_stats(session)
        if st.get("edges", 0) < 2:
            return None   # 图太小·没有可遍历的连接
        # 从约束/题面里的实体作起点·收集所有可达枢轴节点·按(路径长×度数)排
        import re as _re
        seeds = set()
        _txt = " ".join([str(c.get("text") or "") for c in (claims or [])]) + " " + str(question or "")
        for m in _re.findall(r"\b([A-Z][a-zA-Z.\-]{2,}(?:\s+[A-Z][a-zA-Z.\-]{2,}){0,3})\b", _txt):
            m = m.strip()
            seeds.add(m)
            # 多词专名也拆单词作种子(图节点常是单实体名·如"Bielik Arsenal"→Bielik/Arsenal)
            for w in m.split():
                if len(w) >= 3:
                    seeds.add(w)
        cand_l = str(candidate or "").strip().lower()
        # 从每个种子遍历·统计到达的节点(路径完整)
        from collections import Counter
        reach = Counter()
        for seed in list(seeds)[:8]:
            for end_name, path in _eg.find_paths(session, seed, max_hops=3):
                el = str(end_name).lower()
                if el and el != cand_l and el not in (s.lower() for s in seeds):
                    # 权重:路径越长(多跳串起)+边置信越高 越可信
                    w = len(path) + sum(e.get("conf", 0) for e in path)
                    reach[end_name] += w
        if reach:
            return reach.most_common(1)[0][0]
        return None
    except Exception:
        return None


def _find_best(session, question, candidate, evidence, claims, triage, llm_call):
    """找对的:①多假设并行对账(KM层4·选硬约束通过率最高)→②图遍历→③Entity Bank→
    ④证据池 suggest_best。纯读·fail-safe·找不到返回 None。"""
    _best = None
    # 0) ★多假设并行(KM层4·治高置信错锁·同时对账多候选选硬约束通过率最高)
    try:
        import multi_hypothesis as _mh
        if _mh._enabled():
            _mr = _mh.select_best(question, evidence, session=session,
                                  claims=claims, triage=triage,
                                  extra=[candidate] if candidate else None,
                                  llm_call=llm_call)
            # 只有当选出的最优≠当前被拒候选·才采纳(否则退下面的图遍历)
            _mb = (_mr or {}).get("best")
            if _mb and str(_mb).strip().lower() != str(candidate or "").strip().lower():
                _best = _mb
    except Exception:
        _best = None
    # 1) ★图遍历(第3层·收敛从文本语义→图遍历路径)
    if not _best:
        _best = _find_best_by_graph(session, question, candidate, claims)
    # 2) Entity Bank(记忆)——结构化实体库按约束覆盖排
    if not _best:
        try:
            import entity_memory_bank as _emb
            _r = _emb.rank_by_coverage(
                session, question, claims=claims, evidence=evidence,
                exclude=[candidate], llm_call=llm_call)
            _best = (_r or {}).get("best")
        except Exception:
            _best = None
    # 3) 退化:直接从证据池推荐(搜索维度)
    if not _best:
        try:
            import constraint_ledger as _cl
            _s = _cl.suggest_best_candidate(
                question, evidence, claims=claims,
                triage=triage or {}, exclude=[candidate], llm_call=llm_call)
            _best = (_s or {}).get("best")
        except Exception:
            _best = None
    return _best or None


def _norm_in_graph(session, candidate):
    """候选实体是否作为节点出现在证据图里(有图证据锚定)。fail-safe → False。"""
    try:
        g = (session or {}).get("_evidence_graph") or {}
        cl = str(candidate or "").strip().lower()
        return any(cl == n or (len(cl) >= 4 and cl in n)
                   for n in (g.get("nodes") or {}))
    except Exception:
        return False


def govern(session, candidate, question, evidence, *,
           claims=None, triage=None, llm_call=None, engine="native"):
    """统一收尾治理·纯函数。engine 只用于 advice 文案标注(native/auto)·不改逻辑。

    链: 约束三态对账(推理) → [没拒] 实体漂移(校验) → 低置信(校验);
        任一触发 → 找对的(记忆+搜索)。fail-safe·任一子机制炸都降级放行。"""
    _out = {"pass": True, "reason": "", "advice": "", "best": None, "detail": {}}
    if not _enabled():
        return _out
    triage = triage or (session.get("triage") if isinstance(session, dict) else None) or {}

    # ── ① 约束三态对账(推理维度·硬矛盾/unknown过半→reject) ──
    _reject = False
    try:
        import constraint_ledger as _cl
        _recon = _cl.constraint_reconcile(
            candidate, question, evidence,
            claims=claims, triage=triage, llm_call=llm_call)
        _out["detail"]["recon"] = _recon
        _reject = bool(_recon.get("reject"))
    except Exception:
        _recon = {}
        _reject = False

    # ── ①b ★换位思考·用户视角逐约束核对(2026-08-20 用户重大判断·perspective_engine)──
    #   增强(非替换)constraint_ledger: 用【用户/出题人视角+带已搜证据】逐约束核对候选·
    #   ★缺证据判 unknown 不算满足(方法D 实验验证: 把"更有名但缺传记细节证据"的错候选
    #   比下去·如 Matt Ox unk=3 vs Kid Trunks unk=1)。判 reject→也触发换候选;判
    #   insufficient(有 unknown)→不硬锁·要求补证据。★带已有信息·绝不脑补候选满足。
    #   与 constraint_ledger 是【双判据取严】·任一判该换/该补都不放行。fail-safe·env可关。
    try:
        import perspective_engine as _pe
        if _pe._enabled() and candidate and evidence and not _reject:
            # ★优雅收敛(2026-08-20·KM理想架构): 走统一 decide(PHASE_LOCK)出口·
            #   五元组ContextSnapshot·底层复用已验证judge_candidate(行为不变·更优雅)。
            _snap = _pe.ContextSnapshot(question, session=session, evidence=evidence,
                                        claims=claims, llm_call=llm_call)
            _pj = _pe.decide(_snap, _pe.PHASE_LOCK, candidate=candidate)
            _out["detail"]["perspective_judge"] = {
                "verdict": _pj.get("verdict"), "satisfied": _pj.get("satisfied"),
                "unknown": _pj.get("unknown"), "contradicted": _pj.get("contradicted")}
            # 换位视角判【明确矛盾】→ reject(和约束门取严);判 insufficient 且 unknown 多
            #   (缺证据过半)→ 也视为不可锁定(别因它有名就硬交)。
            if _pj.get("verdict") == "reject":
                _reject = True
            elif (_pj.get("verdict") == "insufficient"
                  and int(_pj.get("unknown") or 0) >= 2
                  and int(_pj.get("unknown") or 0) > int(_pj.get("satisfied") or 0)):
                _reject = True
                _out["detail"]["perspective_judge"]["forced_by_unknown"] = True
    except Exception:
        pass

    # ── ② 实体漂移 + ③ 低置信(校验维度·仅在约束没拒时补查·与调用方一致) ──
    _drift = False
    _low = False
    if not _reject:
        try:
            import entity_drift_lowconf_guard as _edg
            if _edg.entity_drift_guard_enabled():
                _dr = _edg.detect_entity_drift(
                    session, candidate, min_sources=_edg.min_confirm_sources())
                _out["detail"]["drift"] = _dr
                _drift = bool(_dr.get("drift"))
            if not _drift and _edg.low_conf_no_submit_enabled():
                _low, _lr = _edg.is_low_confidence(
                    candidate, threshold=_edg.conf_submit_min())
                _out["detail"]["low_conf"] = {"low": _low, "reason": _lr}
        except Exception:
            _drift = False
            _low = False

    # ── ④ 进度撞墙门(治B类无锚点搜不到时别硬猜·真e2e实证错题5 Φ=0 stall=3) ──
    #   长期无进展(进度停滞)+候选在证据图上【无路径支撑】=没有图证据锚定这个候选·
    #   它多半是 LLM 脑补(如错题5的 Rennes)。此时不换候选(没别的)·而是【要求诚实标
    #   存疑】——别沉默地猜一个搜不到证据的实体。fail-safe。
    _wall = False
    if not (_reject or _drift or _low):
        try:
            hist = (session or {}).get("_progress_hist") or {} if isinstance(session, dict) else {}
            _stall = int(hist.get("stall") or 0)
            _phi = float(hist.get("phi") or 0.0)
            if _stall >= 3 and _phi < 0.2:
                # 候选有没有图路径支撑?无路径=没图证据锚定=多半脑补
                import evidence_graph as _eg
                _has_path = bool(_eg.find_paths(session, candidate, max_hops=2)) \
                    or _eg.graph_stats(session).get("edges", 0) >= 1 \
                    and _norm_in_graph(session, candidate)
                if not _has_path:
                    _wall = True
        except Exception:
            _wall = False

    if not (_reject or _drift or _low or _wall):
        return _out   # 四门全过·放行 finalize

    # ── 触发任一门 → 找对的(记忆 Entity Bank + 搜索 suggest_best) ──
    _best = _find_best(session, question, candidate, evidence,
                       claims, triage, llm_call)
    _out["pass"] = False
    _out["best"] = _best
    _eng = "auto" if engine == "auto" else "native"

    if _reject:
        _out["reason"] = "reject"
        try:
            import constraint_ledger as _cl
            _out["advice"] = _cl.gate_advice(_recon, best_suggestion=(
                {"best": _best} if _best else None))
        except Exception:
            _out["advice"] = ("[约束对账·%s] 候选与硬约束矛盾/关键约束未证实·"
                              "别交·换未被矛盾的候选。" % _eng)
            if _best:
                _out["advice"] += "证据里约束覆盖最高候选=%s。" % _best
    elif _drift:
        _out["reason"] = "drift"
        _out["advice"] = ("[实体漂移·%s] 收尾答案主体实体与前期确证不一致·"
                          "以确证实体为准重走最后一跳。" % _eng)
        if _best:
            _out["advice"] += "证据里约束覆盖最高候选=%s。" % _best
    elif _low:
        _out["reason"] = "low_conf"
        _out["advice"] = ("[低置信不交卷·%s] 你自认没把握·别硬交。继续补关键证据"
                          "或诚实标不确定。" % _eng)
        if _best:
            _out["advice"] += "证据里约束覆盖最高候选=%s。" % _best
    else:
        # ④ 进度撞墙:长期无进展+候选无图路径支撑=多半脑补·要求诚实标存疑(不硬猜)
        _out["reason"] = "wall_no_evidence"
        # ★Kimi 式 best-effort:有合理候选(_best)+搜到过证据(非凭空)→给最可能答案+标存疑
        #   +背景(而非纯弃权/硬猜)。治"撞墙就弃权(用户没收获)/乱猜(误导)"。
        _be_used = False
        try:
            import best_effort_answer as _be
            _be_cand = _best   # 多假设/图遍历给的最可能候选
            if _be.should_best_effort(session, _be_cand):
                _be_msg = _be.build_best_effort(
                    question, _be_cand,
                    reasoning="基于你已搜到的证据链(哪几步指向它)",
                    confidence_note="说明没找到哪部分直接证据·这是推断非确证")
                if _be_msg:
                    _out["advice"] = _be_msg
                    _be_used = True
        except Exception:
            _be_used = False
        if not _be_used:
            _out["advice"] = ("[进度撞墙·%s] 你已长期无实质进展(进度停滞)·且当前候选在你已"
                              "积累的证据图里【没有任何路径支撑】——它很可能是没有证据支撑的"
                              "脑补(像错题5把没搜到的实体猜成 Rennes)。别沉默地交一个搜不到"
                              "证据的实体。要么①换一个结构性不同的检索渠道/最特异特征再试;"
                              "要么②诚实交'证据不足·无法确证'并标注你的不确定度——这比硬猜"
                              "一个错实体好。" % _eng)
            if _best:
                _out["advice"] += "证据图里有路径支撑的候选=%s(优先它而非脑补)。" % _best
    return _out
