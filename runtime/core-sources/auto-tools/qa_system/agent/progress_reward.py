# -*- coding: utf-8 -*-
"""progress_reward · 进度奖励模型(烧进引擎·目标导向自主判断)

用户 /goal(Kimi 方案·AgentPRM/Agent-RRM/Progress Reward Model 三层):
  核心 = 不是"agent 做所有事"·而是【每步搜索后 agent 都问: 我离成功还有多远?
  基于这个距离·下一步最该做什么? 无效动作被惩罚·有效动作被奖励】。
  从"盲目搜索"升级到"目标导向的自主判断"。

落地(纯启发·烧进引擎·非训练版 reward model·用可观测过程信号近似进度函数 Φ):
  进度 Φ(state) ∈ [0,1] = 加权融合:
    · 候选约束覆盖率(信息找全程度)                  权重 0.55
    · 约束相关关系覆盖率(无关边不计分)              权重 0.35
    · grounding 比例(每步 query 顺着证据推进非裸猜) 权重 0.10
  每步奖励 r = Φ(s_t) - Φ(s_{t-1}):
    · r > 0  有效步(进度前进)→ 奖励(维持方向)
    · r ≈ 0  无效步(原地踏步)→ 惩罚(该换向·别同层再搜)
    · 连续 STALL_K 步 r≈0 → 强信号: 换渠道/顺藤/收敛·别烧墙钟

铁律:
  1. 纯函数·读 session 过程信号算进度·不改状态·不 I/O·fail-safe。
  2. 与指导思想一致: 无效步(搜了图没长/覆盖没增)= 撒网信号·惩罚→换向。
  3. env MEYO_PROGRESS_REWARD=0 关。绝不透 gold(只看过程结构·不判候选对错)。
"""
import os

_W_COVERAGE = 0.55
_W_GRAPH = 0.35
_W_GROUNDING = 0.10
_STALL_K = 2          # 连续几步无进展→强换向信号
_EPS = 0.03           # 进度增量小于此算"无效步"


def _enabled():
    return os.environ.get("MEYO_PROGRESS_REWARD", "1") != "0"


def _constraint_relation_score(session, claims=None):
    """只计算与题面约束相连的关系边；任意新增网页/节点/边不算进步。"""
    try:
        graph = (session or {}).get("_evidence_graph") or {}
        edges = graph.get("edges") or []
        claims = claims or (session or {}).get("_progress_claims") or []
        token_sets = []
        for claim in claims:
            tokens = claim.get("tokens") if isinstance(claim, dict) else []
            useful = {str(token).lower() for token in (tokens or []) if len(str(token)) >= 2}
            if useful:
                token_sets.append(useful)
        if not token_sets or not edges:
            return 0.0, 0, len(token_sets)
        covered = set()
        relevant_edges = 0
        for edge in edges:
            # 只有外部检索/读取/结构化查询/可复现计算产生的边才是进度证据。
            # triage/finalize/prompt 自述属于假设，不得靠模型重复题面刷关系覆盖。
            sources = [str(src or "").lower() for src in (edge.get("srcs") or [])]
            ungrounded = ("judge_scope_and_triage", "finalize_answer", "build_answer_contract",
                          "review_intent", "write_plan", "soul_driver_review")
            if not sources or all(any(tag in src for tag in ungrounded) for src in sources):
                continue
            text = " ".join(str(edge.get(key) or "") for key in ("s", "r", "o")).lower()
            hit = False
            for index, tokens in enumerate(token_sets):
                matched = {token for token in tokens if token in text}
                # 单个普通词容易把无关边算成进步；数字/精确短语命中可单独成立，
                # 普通词至少两个共同命中才算约束相关。规则与领域无关。
                distinctive = any(any(ch.isdigit() for ch in token) or " " in token
                                  for token in matched)
                if distinctive or len(matched) >= 2:
                    covered.add(index)
                    hit = True
            if hit and edge.get("srcs"):
                relevant_edges += 1
        # 多约束实体识别题中，单条相关边只是线索，不足以证明关系路径在逼近。
        # 至少覆盖两个约束才给关系轨进度；一约束命中只记录 relevant_edges，分数仍为0。
        if len(token_sets) >= 3 and len(covered) < 2:
            return 0.0, relevant_edges, len(token_sets)
        score = len(covered) / float(len(token_sets))
        if covered:
            score = min(1.0, score + min(0.15, relevant_edges * 0.02))
        return round(score, 4), relevant_edges, len(token_sets)
    except Exception:
        return 0.0, 0, 0


def compute_progress(session, *, coverage=None, grounding_ratio=None, claims=None):
    """算当前进度 Φ ∈ [0,1]：约束覆盖 + 约束相关关系 + grounding。
    无关证据或无关图边即使大量增长，也不会提高进度。fail-safe → 0.0。"""
    try:
        # 约束覆盖(信息找全程度)
        cov = coverage
        if cov is None:
            cov = float(((session or {}).get("_memory_track_last") or {}).get("coverage") or 0.0)
        cov = max(0.0, min(1.0, float(cov or 0.0)))
        # 关系进度只看“哪些边覆盖了题面约束”，不奖励无关图规模或高度数泛词节点。
        gscore, relevant_edges, claim_count = _constraint_relation_score(session, claims=claims)
        # grounding(每步顺着证据推进)
        gr = grounding_ratio
        if gr is None:
            gr = float(((session or {}).get("_dual_track_last") or {}).get("grounding") or 0.0)
        gr = max(0.0, min(1.0, float(gr or 0.0)))
        phi = _W_COVERAGE * cov + _W_GRAPH * gscore + _W_GROUNDING * gr
        if isinstance(session, dict):
            session["_progress_state"] = {
                "constraint_coverage": round(cov, 4),
                "relation_coverage": gscore,
                "relevant_edges": relevant_edges,
                "claim_count": claim_count,
                "grounding": round(gr, 4),
            }
        return round(max(0.0, min(1.0, phi)), 4)
    except Exception:
        return 0.0


def step_reward(session, *, coverage=None, grounding_ratio=None, claims=None):
    """算本步奖励 r = Φ(s_t) - Φ(s_{t-1})·并更新 session 里的进度历史。
    返回 {
      "phi": float,          # 当前进度
      "reward": float,       # 本步增量(>0 前进·≈0 原地)
      "effective": bool,     # 是否有效步(reward > EPS)
      "stall_streak": int,   # 连续无进展步数
      "advice": str,         # 无进展时的换向提示(有效步为空)
    }。fail-safe → 中性(effective=True·不干扰)。"""
    out = {"phi": 0.0, "reward": 0.0, "effective": True,
           "constraint_delta": 0.0, "relation_delta": 0.0,
           "relation_effective": True, "stall_streak": 0, "advice": ""}
    try:
        if not _enabled() or session is None:
            return out
        phi = compute_progress(session, coverage=coverage,
                               grounding_ratio=grounding_ratio, claims=claims)
        hist = session.setdefault("_progress_hist", {"phi": 0.0, "stall": 0})
        prev = float(hist.get("phi") or 0.0)
        state = session.get("_progress_state") or {}
        prev_constraint = float(hist.get("constraint_coverage") or 0.0)
        prev_relation = float(hist.get("relation_coverage") or 0.0)
        constraint_now = float(state.get("constraint_coverage") or 0.0)
        relation_now = float(state.get("relation_coverage") or 0.0)
        r = round(phi - prev, 4)
        out["phi"] = phi
        out["reward"] = r
        out["constraint_delta"] = round(constraint_now - prev_constraint, 4)
        out["relation_delta"] = round(relation_now - prev_relation, 4)
        out["relation_effective"] = out["relation_delta"] > _EPS
        out["effective"] = r > _EPS
        # 更新历史 + 连续无进展计数
        if r > _EPS:
            hist["stall"] = 0
        else:
            hist["stall"] = int(hist.get("stall") or 0) + 1
        hist["phi"] = phi
        hist["constraint_coverage"] = constraint_now
        hist["relation_coverage"] = relation_now
        out["stall_streak"] = int(hist.get("stall") or 0)
        # 无进展惩罚→换向提示(指导思想:无效动作被惩罚)
        if out["stall_streak"] >= _STALL_K:
            out["advice"] = (
                "[进度评估·换向] 连续 %d 步没让你更接近目标(进度 Φ=%.2f 停滞·"
                "约束覆盖/约束相关关系没增·或仍在裸猜)。这是【无效动作】——按目标导向判断·"
                "下一步别再同层撒网。最该做的: ①有确证锚点就顺藤(锚点×缺口正向补搜);"
                "②没锚点就提取题面【最特异单一特征】换角度/换源;③证据够了就图遍历收敛"
                "别硬凑。目标是【离答案更近】·不是【搜得更多】。" % (out["stall_streak"], phi))
        return out
    except Exception:
        return out


def progress_summary(session):
    """当前进度摘要(注入给模型看·让它'看到自己离成功多远')。fail-safe → ''。"""
    try:
        if not _enabled() or session is None:
            return ""
        hist = (session or {}).get("_progress_hist") or {}
        phi = float(hist.get("phi") or 0.0)
        import evidence_graph as _eg
        st = _eg.graph_stats(session)
        return ("[进度自评] 离目标进度 Φ≈%.2f(0=刚开始 1=证据充分可收敛)·"
                "已积累结构化证据图 %d 节点/%d 边。%s"
                % (phi, st.get("nodes", 0), st.get("edges", 0),
                   "证据在积累·继续顺藤。" if phi >= 0.3 else
                   "进度低·先锚定一个确证实体再顺藤。"))
    except Exception:
        return ""
