#!/usr/bin/env python3
"""task_spec · 2026-08-12 · 通用化 Phase 0 · 接待层地基

用户方向:GAIA agent 是"只会闭卷考试的机器"·真实用户是"开卷对话"。通用化=换接待/
交付/评估三层皮·内核(run_auto_agent 的搜索/验证/诚实/自进化)一个都不动。

TaskSpec = 统一的任务描述对象·GAIA 只是它的一个特例(interaction_budget=0,
deliverable=fact)。这是三层皮的挂载点:
  - Phase1 M-91澄清/M-92假设账本 挂 interaction_budget
  - Phase2 交付路由/M-93动作门 挂 deliverable
  - Phase3 M-94遥测 / Phase4多轮记忆 挂 context

Phase 0 只做骨架+GAIA适配+透传:证明"换皮没伤内核"(165回归全绿是总闸门)。
纯 dataclass·零依赖·可单测·不 import 重模块(避免拖慢/循环依赖)。
"""
from dataclasses import dataclass, field
from typing import Optional, List, Dict


# 交付物类型(Phase2 路由到四种收尾器·现在只用 fact)
DELIVERABLE_FACT = "fact"        # 一个短答案(GAIA)·复用现有 finalize 门
DELIVERABLE_ARTIFACT = "artifact"  # 报告/表格/文件
DELIVERABLE_ACTION = "action"    # 发消息/预订/改外部状态(最危险·Phase2 加动作门)
DELIVERABLE_MONITOR = "monitor"  # 持续监控(挂 perception 车道+cron)

# 深度预算 → run_auto_agent 的 urgency/force_task_level 映射
# quick=省时快答·standard=默认·deep=多方核实深挖
_DEPTH_TO_URGENCY = {"quick": "high", "standard": "normal", "deep": "low"}
_DEPTH_TO_FORCE_LEVEL = {"quick": "琐事", "standard": None, "deep": "重型"}


@dataclass
class TaskSpec:
    """统一任务描述。GAIA = TaskSpec(interaction_budget=0, deliverable=fact) 的实例。"""
    raw_input: str                                    # 用户原话(=GAIA的question)
    modality: List[str] = field(default_factory=lambda: ["text"])  # text/file/image/link
    context: Dict = field(default_factory=dict)       # 用户时区/身份/历史会话指针(Phase3/4)
    deliverable: str = DELIVERABLE_FACT               # fact|artifact|action|monitor
    interaction_budget: int = 0                       # 允许反问几轮(GAIA=0·真实用户>0)
    depth_budget: str = "standard"                    # quick|standard|deep
    deadline: Optional[str] = None                    # ISO 时间串(Phase2/3)

    @classmethod
    def from_gaia(cls, question: str, qid: str = ""):
        """GAIA 适配器:把一道 GAIA 题变成 TaskSpec(闭卷特例·不许反问·要短答案)。
        这是"GAIA 降级为体检仪"的接口——165 题原封不动变回归 CI。"""
        return cls(
            raw_input=question,
            modality=["text"],
            context={"source": "gaia", "qid": qid} if qid else {"source": "gaia"},
            deliverable=DELIVERABLE_FACT,
            interaction_budget=0,          # GAIA 铁律:不许反问·必须猜(这是它的枷锁)
            depth_budget="standard",
        )

    @classmethod
    def from_user(cls, raw_input: str, *, interaction_budget: int = 3,
                  deliverable: str = DELIVERABLE_FACT, depth_budget: str = "standard",
                  context: Dict = None, modality: List[str] = None, session_id: str = ""):
        """真实用户适配器:默认允许反问(interaction_budget>0)——G1题意歧义在这里可消解。
        Phase1 的 M-91 澄清协议就靠这个 budget>0 分流。
        Phase4:session_id 有值→多轮会话(存 context·run_task 做指代消解+透传历史)。"""
        ctx = dict(context or {})
        if session_id:
            ctx["session_id"] = session_id
        return cls(
            raw_input=raw_input,
            modality=modality or ["text"],
            context=ctx,
            deliverable=deliverable,
            interaction_budget=interaction_budget,
            depth_budget=depth_budget,
        )

    def to_run_auto_kwargs(self) -> Dict:
        """转成 run_auto_agent 能吃的 kwargs(向后兼容内核·签名一字不改)。
        Phase0 映射 question/urgency/force_task_level。Phase4 加 conversation_history
        透传(内核已有多轮机制·复用)——有 session_id 就带历史·GAIA 无 session_id→None(单轮·等价不破)。"""
        kw = {
            "question": self.raw_input,
            "urgency": _DEPTH_TO_URGENCY.get(self.depth_budget, "normal"),
            "force_task_level": _DEPTH_TO_FORCE_LEVEL.get(self.depth_budget),
            "user_id": self.context.get("user_id", "default"),
        }
        # Phase4 多轮:有 session_id 就带会话历史(透传内核已有 conversation_history 机制)。
        # GAIA 无 session_id → 不带(None)= 单轮·行为不变(165等价严格保)。
        sid = self.context.get("session_id")
        if sid:
            try:
                from session_store import load_history
                hist = load_history(sid)
                if hist:
                    kw["conversation_history"] = hist
            except Exception:
                pass  # 历史加载失败不阻断(退回单轮)
        return kw


if __name__ == "__main__":
    # 自测:GAIA适配无损往返 + 用户适配默认可反问
    g = TaskSpec.from_gaia("What is the area of the green polygon?", qid="q1")
    kw = g.to_run_auto_kwargs()
    assert kw["question"] == "What is the area of the green polygon?", "GAIA question 无损"
    assert g.interaction_budget == 0, "GAIA 不许反问"
    assert g.deliverable == "fact", "GAIA 要短答案"
    assert kw["urgency"] == "normal", "standard→normal"
    print("✅ GAIA适配:", kw)
    u = TaskSpec.from_user("盯着这个票价", interaction_budget=3, deliverable="monitor")
    assert u.interaction_budget == 3 and u.deliverable == "monitor", "用户可反问+监控类"
    print("✅ 用户适配:", u.deliverable, "budget=", u.interaction_budget)
    # deep→low urgency + 重型
    d = TaskSpec.from_user("深挖", depth_budget="deep")
    assert d.to_run_auto_kwargs()["force_task_level"] == "重型", "deep→重型"
    print("✅ depth映射: deep→", d.to_run_auto_kwargs()["urgency"], d.to_run_auto_kwargs()["force_task_level"])
    print("\n✅✅ TaskSpec 全通(GAIA=通用的特例·内核kwargs向后兼容)")
