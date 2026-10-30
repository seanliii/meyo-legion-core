#!/usr/bin/env python3
"""
mission_state · 双向共享任务状态 (v59, 2026-08-06)

用户抓「各环节机械执行·互不感知」：goal_card 只在 step0.25 生成一次就冻结成
只读字典——下游 step2.6/2.7/4.9 全在"回看"它，却没人往回写。蜂群(deep_research_
swarm)跑完明明已经攒了 conclusions(已知事实)和 unresolved_questions(信息缺口)，
但这些从没被写回主流程状态，step4.9 抽答时对蜂群刚查到的东西一无所知，只能
从头再翻一遍 evidence——这就是"机械执行"的根因：没有一个贯穿全程、会越滚越大
的共享事实池。

设计对比 (为什么不是另起一个大而全的框架):
  - deepagents SummarizationMiddleware 的思路 = 「摘要 + 全量落盘 + 路径回读」
    这里的对应: known_facts 是滚动摘要(只留结论性事实，不留原始证据全文，
    避免 prompt 爆炸)；原始证据仍留在 trace["evidence"] / multihop_evidence
    里(全量落盘)，需要溯源时按 source 回查。
  - RubricMiddleware 的思路 = 把模糊目标拆成结构化 criteria + 独立 grader 判定
    这里先只做「open_gaps 是否清空」这个最小判据(轻量版 rubric)，不引入
    独立 grader 角色——那是 P1 任务(auto_agent 判定结构化)，本模块只管状态。

单一职责: 本模块只管 known_facts / open_gaps 两条状态的读写与渲染，
不管调度顺序(那是 orchestrate() 的事)，不做 LLM 调用(判断"这条算不算新
事实"由调用方决定，本模块只负责去重、累积、渲染)。

存放位置: 状态直接挂在调用方传入的 trace dict 上(trace["mission_state"])，
和 goal_card 现在的传递方式一致——单次请求生命周期内的进程内状态，不需要
落盘；跨请求持久化(比如断点续传)已有 qa_auto_agent.py 的 checkpoint 机制
负责，两者职责不重叠。

用法:
    from mission_state import MissionState

    ms = MissionState.init(trace, question, goal_card)   # step0.25 后调一次
    ms.add_fact("India 1977 年首相是 Morarji Desai", source="tavily_answer", hop=1)
    ms.add_gap("还不确定该地名对应的具体年份")
    ...
    ms.resolve_gap("还不确定该地名对应的具体年份")        # 后续跳查到了就消解
    hint = ms.render_for_prompt()                         # 塞进下游 LLM prompt
    ms.snapshot()                                          # 存进 trace 供 _mark 落日志
"""
import time


STATE_KEY = "mission_state"


class MissionState:
    """挂在 trace 上的双向共享状态·所有方法直接改 trace[STATE_KEY]，
    多个 MissionState(trace) 实例读写同一份底层字典，天然"共享"。"""

    def __init__(self, trace):
        self.trace = trace if isinstance(trace, dict) else {}
        if STATE_KEY not in self.trace:
            self.trace[STATE_KEY] = {
                "question": "",
                "success_criteria": "",
                "known_facts": [],   # [{"fact":str,"source":str,"hop":int,"ts":float}]
                "open_gaps": [],     # [{"gap":str,"raised_by":str,"ts":float,"resolved":bool}]
                "updates": [],       # 变更审计轨迹·谁在什么时候写了什么(治"互不感知")
            }

    # ── 初始化 ──────────────────────────────────────────────────────
    @classmethod
    def init(cls, trace, question="", goal_card=None):
        """step0.25 goal_card 解析完后调一次·把 success_criteria 种进去。
        重复调用是幂等的(不会清空已累积的 facts/gaps)，只补充缺失字段。"""
        ms = cls(trace)
        st = ms.trace[STATE_KEY]
        if question and not st.get("question"):
            st["question"] = question[:300]
        gc = goal_card or {}
        if gc.get("success_criteria") and not st.get("success_criteria"):
            st["success_criteria"] = str(gc["success_criteria"])[:200]
        # is_multihop 题目天然有已知的信息缺口:"还没解开指代链"
        if str(gc.get("is_multihop", "")).lower() in ("true", "1", "yes") and not st["open_gaps"]:
            ms.add_gap("多跳题：核心中间实体尚未解开", raised_by="step0.25_goal_card")
        return ms

    # ── 写: 已知事实 ────────────────────────────────────────────────
    def add_fact(self, fact, source="", hop=None):
        """登记一条新查到的事实。去重(同一 fact 文本已存在就跳过)。
        source 建议填 step 名(如 "step2.7_swarm_hop1")，方便追溯是谁贡献的。"""
        fact = str(fact or "").strip()
        if not fact or fact.upper() in ("NONE", "UNKNOWN"):
            return False
        st = self.trace[STATE_KEY]
        if any(f["fact"] == fact[:300] for f in st["known_facts"]):
            return False  # 已有·别重复堆
        rec = {"fact": fact[:300], "source": source[:60], "hop": hop, "ts": round(time.time(), 1)}
        st["known_facts"].append(rec)
        st["updates"].append({"op": "add_fact", "by": source[:60], "ts": rec["ts"],
                               "brief": fact[:60]})
        return True

    def add_facts(self, facts, source=""):
        """批量登记(蜂群 conclusions 这类列表一次性回写用)。"""
        n = 0
        for f in (facts or []):
            if self.add_fact(f, source=source):
                n += 1
        return n

    # ── 写: 信息缺口 ────────────────────────────────────────────────
    def add_gap(self, gap, raised_by=""):
        """登记一个尚未满足的信息缺口(比如蜂群 hop_judge 的 gap_summary)。
        去重: 同一 gap 文本已存在且未 resolve 就跳过。"""
        gap = str(gap or "").strip()
        if not gap:
            return False
        st = self.trace[STATE_KEY]
        for g in st["open_gaps"]:
            if g["gap"] == gap[:300] and not g["resolved"]:
                return False
        rec = {"gap": gap[:300], "raised_by": raised_by[:60], "ts": round(time.time(), 1),
               "resolved": False}
        st["open_gaps"].append(rec)
        st["updates"].append({"op": "add_gap", "by": raised_by[:60], "ts": rec["ts"],
                               "brief": gap[:60]})
        return True

    def resolve_gap(self, gap, resolved_by=""):
        """标记某个缺口已被后续检索/推理补上。模糊匹配(子串)容错措辞差异。"""
        st = self.trace[STATE_KEY]
        gap_l = str(gap or "").strip().lower()
        if not gap_l:
            return 0
        n = 0
        for g in st["open_gaps"]:
            if g["resolved"]:
                continue
            if gap_l in g["gap"].lower() or g["gap"].lower() in gap_l:
                g["resolved"] = True
                g["resolved_by"] = resolved_by[:60]
                g["resolved_ts"] = round(time.time(), 1)
                n += 1
        if n:
            st["updates"].append({"op": "resolve_gap", "by": resolved_by[:60],
                                   "ts": round(time.time(), 1), "brief": gap[:60]})
        return n

    def sync_gaps_from_hop_judge(self, hop_judge, source=""):
        """v59 · 蜂群每跳 HopJudge 产出直接回写(专治"蜂群查完主流程不知道"):
        sufficient=true → 视为本轮缺口已满足，全部 resolve；
        否则把 gap_summary 记成新缺口、next_queries 里能看出还缺什么。"""
        if not isinstance(hop_judge, dict):
            return
        if hop_judge.get("sufficient"):
            for g in self.trace[STATE_KEY]["open_gaps"]:
                if not g["resolved"]:
                    g["resolved"] = True
                    g["resolved_by"] = source[:60] or "hop_judge_sufficient"
                    g["resolved_ts"] = round(time.time(), 1)
        else:
            gap_summary = hop_judge.get("gap_summary", "")
            if gap_summary:
                self.add_gap(gap_summary, raised_by=source or "hop_judge")

    # ── 读: 未解决的缺口数 (轻量 rubric 判据) ───────────────────────
    def unresolved_gaps(self):
        return [g for g in self.trace[STATE_KEY]["open_gaps"] if not g["resolved"]]

    def is_mission_complete(self):
        """最小判据: 没有未解决的 gap，就算"目标齐"了(不代表答案已生成，
        只代表"信息缺口"这个维度不再阻塞——是否要独立 grader 判定答案对错
        是 P1 任务的范畴，这里先给最便宜的信号)。"""
        return len(self.unresolved_gaps()) == 0

    # ── 渲染: 塞进下游 LLM prompt ────────────────────────────────────
    def render_for_prompt(self, max_facts=8, max_gaps=5):
        """生成一段可以直接拼进 prompt 的文本块，让下游(抽答/写作)"回看"
        全程已经攒下的事实和缺口，不用每步都重新从零猜。"""
        st = self.trace[STATE_KEY]
        facts = st["known_facts"][-max_facts:]
        gaps = self.unresolved_gaps()[-max_gaps:]
        if not facts and not gaps:
            return ""
        lines = []
        if st.get("success_criteria"):
            lines.append(f"【判定对错标准】{st['success_criteria']}")
        if facts:
            lines.append("【已查到的事实(全程累积·别重新猜)】")
            for f in facts:
                tag = f"hop{f['hop']}" if f.get("hop") is not None else f.get("source", "")
                lines.append(f"  - [{tag}] {f['fact']}")
        if gaps:
            lines.append("【仍未解决的信息缺口(优先补这些)】")
            for g in gaps:
                lines.append(f"  - {g['gap']}")
        return "\n".join(lines)

    # ── 落日志 ───────────────────────────────────────────────────────
    def snapshot(self):
        """给 _mark() 落日志用的精简摘要(别把全量 known_facts 都塞进 steps 日志)。"""
        st = self.trace[STATE_KEY]
        return {
            "n_facts": len(st["known_facts"]),
            "n_gaps_open": len(self.unresolved_gaps()),
            "n_gaps_total": len(st["open_gaps"]),
            "n_updates": len(st["updates"]),
            "complete": self.is_mission_complete(),
        }
