# -*- coding: utf-8 -*-
"""multi_hypothesis · 多假设并行验证(KM2782350173 层4·治高置信错锁·烧进引擎)

KM 盲区4(★★★★☆): Agent 只追踪一个候选(Lil Pump)·不并行追踪多个(Minh Nguyen)——
一旦锁错就一路错到底。应【同时生成多候选→并行约束对账→按硬约束通过率(非 verbalized
confidence)选最优】。

例(Lil Pump 题·gold=越南裔 rapper):
  候选池: [Lil Pump, Minh Nguyen, Drake]
  约束对账: Lil Pump 5/6满足但"越南裔"contradicted→conf=0; Minh Nguyen 6/6→conf=1.0
  选择: Minh Nguyen(硬约束通过率最高·非它自评的置信度)

复用现有: constraint_ledger.constraint_reconcile(逐约束三态对账·硬约束通过率算置信)。
本模块只做【多候选生成 + 并行对账 + 选最优】的编排·不重造对账逻辑。

铁律: 纯函数·fail-safe·env MEYO_MULTI_HYPOTHESIS=0 关·不透 gold(只按约束对账客观选)。
"""
import os


def _enabled():
    return os.environ.get("MEYO_MULTI_HYPOTHESIS", "1") != "0"


def generate_candidates(question, evidence, *, session=None, extra=None, llm_call=None, max_cand=6):
    """生成多候选假设:①证据里的实体(Entity Bank/证据池)②约束联想(feature_to_entity)。
    extra=额外候选(如 anchor_hop/图遍历给的)。返回去重候选名列表。fail-safe → []。"""
    cands = []
    try:
        # 来源1: 证据池/Entity Bank 里的高频实体
        try:
            import constraint_ledger as _cl
            ents = _cl._extract_candidate_entities(evidence, max_cand)
            cands.extend(ents or [])
        except Exception:
            pass
        # 来源2: SoulDriver 基于目标类型+关系证据明确提名的候选。
        # Entity Bank/Graph 本身只是路径事实库，节点不能因高频或高度数自动晋升为答案。
        for rec in (session or {}).get("_candidate_hypotheses") or []:
            name = rec.get("name") if isinstance(rec, dict) else rec
            if name:
                cands.append(name)
        # 来源2b: Entity Bank 结构化库——只有存在 llm_call(质量过滤管线)时才纳入，
        # 且一律经 candidate_quality 的 SoulDriver 判断·不允许凭频次直接晋升为答案。
        if llm_call:
            try:
                import entity_memory_bank as _emb
                for name, _hits, _srcs in (_emb.get_bank_entities(session or {}) or []):
                    cands.append(name)
            except Exception:
                pass
        # 来源3: 外部传入(anchor_hop/图遍历/feature_to_entity 联想)
        if extra:
            cands.extend(extra if isinstance(extra, list) else [extra])
        try:
            import candidate_quality as _cq
            return _cq.filter_candidates(cands, question, evidence=evidence, llm_call=llm_call, max_cand=max_cand)
        except Exception:
            pass
        # 去重保序
        seen, out = set(), []
        for c in cands:
            cl = str(c or "").strip().lower()
            if cl and cl not in seen and 2 <= len(cl) <= 60:
                seen.add(cl)
                out.append(str(c).strip())
        return out[:max_cand]
    except Exception:
        return []


def verify_parallel(question, candidates, evidence, *, claims=None, triage=None,
                    llm_call=None):
    """并行验证每个候选(逐约束对账·硬约束通过率算置信)。
    返回按置信降序 [{"candidate","confidence","reject","satisfied","contradicted",
    "unknown"} ...]。复用 constraint_ledger。fail-safe → []。"""
    out = []
    try:
        if not _enabled() or not candidates:
            return out
        import constraint_ledger as _cl
        for cand in candidates:
            try:
                recon = _cl.constraint_reconcile(
                    cand, question, evidence,
                    claims=claims, triage=triage or {}, llm_call=llm_call)
                out.append({
                    "candidate": cand,
                    "confidence": float(recon.get("confidence") or 0.0),
                    "reject": bool(recon.get("reject")),
                    "satisfied": recon.get("satisfied"),
                    "contradicted": recon.get("contradicted"),
                    "unknown": recon.get("unknown"),
                })
            except Exception:
                out.append({"candidate": cand, "confidence": 0.0, "reject": True,
                            "satisfied": None, "contradicted": None, "unknown": None})
        # 按置信降序·被 reject 的沉底
        out.sort(key=lambda x: (not x["reject"], x["confidence"]), reverse=True)
        return out
    except Exception:
        return out


def select_best(question, evidence, *, session=None, claims=None, triage=None, extra=None,
                llm_call=None):
    """多假设总入口:生成候选→并行对账→选硬约束通过率最高的(非 verbalized)。
    返回 {"best","confidence","ranked","margin","advice"}。margin=第1与第2置信差·
    治过早锁死(margin 小=候选难分·别急着锁)。fail-safe → best=None。"""
    res = {"best": None, "confidence": 0.0, "ranked": [], "margin": 0.0, "advice": ""}
    try:
        if not _enabled():
            return res
        cands = generate_candidates(question, evidence, session=session,
                                    extra=extra, llm_call=llm_call)
        if not cands:
            return res
        ranked = verify_parallel(question, cands, evidence,
                                 claims=claims, triage=triage, llm_call=llm_call)
        res["ranked"] = ranked
        if not ranked:
            return res
        top = ranked[0]
        if top["reject"]:
            # 全被 reject → 没有站得住的候选
            res["advice"] = ("[多假设并行] 生成的 %d 个候选经约束对账【全被硬约束否证】·"
                             "没有站得住的答案·别硬选·继续补证据或诚实标存疑。" % len(cands))
            return res
        res["best"] = top["candidate"]
        res["confidence"] = top["confidence"]
        # margin: top1 与 top2(未被reject的)置信差
        others = [r for r in ranked[1:] if not r["reject"]]
        res["margin"] = round(top["confidence"] - (others[0]["confidence"] if others else 0.0), 3)
        if res["margin"] < 0.2 and others:
            res["advice"] = ("[多假设并行] 候选 %s(conf=%.2f) 与 %s(conf=%.2f) 区分度小"
                             "(margin=%.2f)·别急着锁死·补【区分这两者的约束】证据再定。"
                             % (top["candidate"], top["confidence"],
                                others[0]["candidate"], others[0]["confidence"], res["margin"]))
        else:
            res["advice"] = ("[多假设并行] 并行对账 %d 候选·硬约束通过率最高=%s(conf=%.2f)·"
                             "领先第二名 margin=%.2f·可作为主候选(按约束对账客观选·非自评)。"
                             % (len(cands), top["candidate"], top["confidence"], res["margin"]))
        return res
    except Exception:
        return res
