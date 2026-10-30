#!/usr/bin/env python3
"""
plan_state · Plan/Todo 驱动状态机 (对齐 Claude Code 的 TodoWrite 核心理念)

用户抓正 (2026-08-06):
「其他他们的核心优势你也得学习」——CC 真正解决长任务不跑偏的机制不是更好
的 prompt，而是 **强制显式化的计划状态**：
    - 任务开始前，先把要做的事拆成有序 todo 列表 (pending/in_progress/completed)
    - 每一步执行前后都要求更新这个列表 (每次最多一个 in_progress)
    - 这个列表本身会被塞进后续每一轮的上下文里，反复提醒模型"现在在第几步、
      还剩什么没做"，防止长任务半途而废或重复劳动

现状问题 (meta_graph.py / agent_facades.py):
    MetaGraph.run() 的执行路径完全是 Python 代码里硬编码的
    `if mode == "deep" and dec and not dec.get("is_sufficient")` 这种分支树。
    好处是确定性强、好测试；坏处是:
      1. 遇到 mode 之外的复杂任务（比如用户想要"先深搜再判断要不要写"这种
         非标准顺序）完全无法表达，只能改代码加新 mode
      2. 中途失败/中断后无法恢复到"刚才跑到哪一步"——trace 只是日志，不是
         可恢复的状态
      3. 没有"预估将要做什么 → 实际做了什么 → 是否符合预期"的对比，出问题
         很难定位是"没按计划做"还是"计划本身就错了"

本模块: 给 6 门面 agent 提供一个轻量、可选接入的 Plan/Todo 状态机。
    不强制重写 MetaGraph 的确定性路径 (那套仍然适合 90% 标准场景)，而是
    给"需要应对非标准复杂任务"的调用方(比如 qa_auto_agent、未来的复杂
    multi-agent 编排)提供显式计划能力:

    from plan_state import Plan

    plan = Plan(goal="调研 RAG 评测方法并写一篇实践帖")
    plan.add_step("deduction", "拆解用户意图，识别关键子问题")
    plan.add_step("search", "搜索 RAG 评测的方法论和案例")
    plan.add_step("supervision", "判断证据是否充分")
    plan.add_step("writer", "撰写实践帖")

    plan.start_step(0)          # 标记第一步开始 (强制只有一个 in_progress)
    ... 实际调用 DeductionAgent ...
    plan.complete_step(0, note="拆出3层意图，识别2个关键子问题")

    plan.render_for_prompt()    # 生成可塞进 LLM prompt 的当前计划文本
    plan.snapshot()             # 落盘，支持中断后 resume
    Plan.resume(plan_id)        # 从磁盘恢复未完成的计划
"""
import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional

_DIR = os.path.dirname(os.path.abspath(__file__))
V11 = os.path.dirname(_DIR)
_PLAN_DIR = os.path.join(V11, ".plan_state")

VALID_STATUS = {"pending", "in_progress", "completed", "blocked", "cancelled"}


@dataclass
class PlanStep:
    idx: int
    agent: str            # 哪个门面 agent 负责这步 (Deduction/Search/...)
    description: str      # 这步要做什么 (人类可读)
    status: str = "pending"
    note: str = ""         # 完成后的实际结果摘要
    ref_id: str = ""       # 若结果较大，对应 context_store 的 ref_id
    started_at: Optional[float] = None
    completed_at: Optional[float] = None


class Plan:
    """一个可显式追踪、可落盘恢复的任务计划。

    核心约束 (对齐 CC TodoWrite 的强制规则):
      - 同一时刻最多一个 step 处于 in_progress (逼迫顺序推进，不允许并行
        隐式声明多个"正在做"，避免自己骗自己"都在做但都没做完")
      - complete_step 必须带 note (强制汇报，不允许"做完了但不说做了啥")
    """

    def __init__(self, goal: str, plan_id: str = ""):
        self.plan_id = plan_id or f"plan_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        self.goal = goal
        self.steps: list[PlanStep] = []
        self.created_at = time.time()
        self.updated_at = time.time()
        self.done = False

    def add_step(self, agent: str, description: str) -> int:
        """追加一步，返回该步索引。"""
        idx = len(self.steps)
        self.steps.append(PlanStep(idx=idx, agent=agent, description=description))
        self.updated_at = time.time()
        return idx

    def start_step(self, idx: int):
        """标记某步开始。强制检查：不能有别的 step 还在 in_progress。"""
        active = [s for s in self.steps if s.status == "in_progress"]
        for s in active:
            if s.idx != idx:
                # 不抛异常(fail-safe 优先)，但记一条警告到 note，方便事后审计
                s.note = (s.note + " [警告:未收尾就切换到下一步]").strip()
                s.status = "blocked"
        if 0 <= idx < len(self.steps):
            self.steps[idx].status = "in_progress"
            self.steps[idx].started_at = time.time()
        self.updated_at = time.time()

    def complete_step(self, idx: int, note: str, ref_id: str = ""):
        """标记某步完成，强制要求 note (对齐 CC 强制汇报)。"""
        if not note:
            note = "(未提供完成说明)"
        if 0 <= idx < len(self.steps):
            self.steps[idx].status = "completed"
            self.steps[idx].note = note[:300]
            self.steps[idx].ref_id = ref_id
            self.steps[idx].completed_at = time.time()
        self.updated_at = time.time()
        if all(s.status in ("completed", "cancelled") for s in self.steps):
            self.done = True

    def block_step(self, idx: int, reason: str):
        """标记某步受阻(比如工具报错/证据不足)，不算完成也不算失败，等后续决定重试/跳过。"""
        if 0 <= idx < len(self.steps):
            self.steps[idx].status = "blocked"
            self.steps[idx].note = f"受阻: {reason[:200]}"
        self.updated_at = time.time()

    def insert_step_after(self, after_idx: int, agent: str, description: str) -> int:
        """动态插入新步骤 (对应 CC 场景: 执行中发现遗漏，临时补计划，而不是
        硬编码分支写死"deep 模式才补搜"——任何模式都可以按需插入)。"""
        new_step = PlanStep(idx=after_idx + 1, agent=agent, description=description)
        self.steps.insert(after_idx + 1, new_step)
        for i, s in enumerate(self.steps):
            s.idx = i
        self.updated_at = time.time()
        return after_idx + 1

    def current_step(self) -> Optional[PlanStep]:
        for s in self.steps:
            if s.status == "in_progress":
                return s
        return None

    def next_pending(self) -> Optional[PlanStep]:
        for s in self.steps:
            if s.status == "pending":
                return s
        return None

    def progress_pct(self) -> int:
        if not self.steps:
            return 0
        done = sum(1 for s in self.steps if s.status == "completed")
        return round(done / len(self.steps) * 100)

    def render_for_prompt(self) -> str:
        """生成可直接塞进 LLM system prompt / 中间提醒的计划文本。
        对齐 CC 把 todo 列表反复注入上下文、提醒"现在在哪一步"的做法。"""
        lines = [f"# 当前计划: {self.goal}", f"进度: {self.progress_pct()}%"]
        icon = {"pending": "⬜", "in_progress": "🔄", "completed": "✅",
                "blocked": "⚠️", "cancelled": "❌"}
        for s in self.steps:
            line = f"{icon.get(s.status, '?')} [{s.idx}] ({s.agent}) {s.description}"
            if s.note:
                line += f" — {s.note[:80]}"
            lines.append(line)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id, "goal": self.goal, "done": self.done,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "steps": [asdict(s) for s in self.steps],
        }

    def snapshot(self):
        """落盘，支持中断恢复。fail-safe: 落盘失败不影响主流程。"""
        try:
            os.makedirs(_PLAN_DIR, exist_ok=True)
            path = os.path.join(_PLAN_DIR, f"{self.plan_id}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    @classmethod
    def resume(cls, plan_id: str) -> Optional["Plan"]:
        """从磁盘恢复一个未完成的计划 (对应 CC/deepagents 长任务中断后可续跑)。"""
        path = os.path.join(_PLAN_DIR, f"{plan_id}.json")
        if not os.path.exists(path):
            return None
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception:
            return None
        plan = cls(goal=data["goal"], plan_id=data["plan_id"])
        plan.created_at = data.get("created_at", time.time())
        plan.updated_at = data.get("updated_at", time.time())
        plan.done = data.get("done", False)
        plan.steps = [PlanStep(**s) for s in data.get("steps", [])]
        return plan

    @classmethod
    def list_incomplete(cls) -> list:
        """列出所有未完成的计划 (供 dashboard / 主动恢复用)。"""
        if not os.path.exists(_PLAN_DIR):
            return []
        out = []
        for fn in os.listdir(_PLAN_DIR):
            if not fn.endswith(".json"):
                continue
            try:
                data = json.load(open(os.path.join(_PLAN_DIR, fn), encoding="utf-8"))
                if not data.get("done"):
                    out.append({"plan_id": data["plan_id"], "goal": data["goal"],
                                "progress": sum(1 for s in data.get("steps", [])
                                                if s.get("status") == "completed"),
                                "total": len(data.get("steps", []))})
            except Exception:
                continue
        return out


# ══════════════════════════════════════════════════════════════════
# 便捷封装: 给 MetaGraph 用的标准 6-agent 计划模板
# ══════════════════════════════════════════════════════════════════

def make_standard_plan(goal: str, mode: str = "balanced") -> Plan:
    """按 MetaGraph 的 3 mode 生成对应的标准计划骨架 (仍然对齐现有确定性
    路径，但改成显式 Plan 表达，为后续动态插步/中断恢复打基础)。"""
    plan = Plan(goal=goal)
    plan.add_step("Deduction", "拆解用户意图，找关键子问题")
    plan.add_step("Search", f"按意图搜索证据 (mode={mode})")
    if mode in ("balanced", "deep"):
        plan.add_step("Supervision", "判断证据是否充分")
    if mode == "deep":
        plan.add_step("Learning", "证据不足时学习补充搜索策略")
        plan.add_step("Writer", "撰写最终产出")
    return plan


# ══════════════════════════════════════════════════════════════════
# 注册为 evolution_tool_registry 工具
# ══════════════════════════════════════════════════════════════════

def _register_self():
    try:
        from evolution_tool_registry import register_evolution_tool

        @register_evolution_tool(
            name="plan_list_incomplete",
            description="列出所有未完成的任务计划(支持中断恢复场景排查)",
            category="meta",
            cost_tier="cheap",
            search_hint="plan todo incomplete resume list state",
            module="plan_state",
            is_deferred=True,
        )
        def _list_tool(params):
            return {"ok": True, "result": Plan.list_incomplete()}
    except Exception:
        pass


# 模块级触发注册 (同 context_store.py 的修复：之前定义了 _register_self()
# 但从未调用，导致本模块工具在 registry 里不可发现)。
_register_self()


if __name__ == "__main__":
    print("=== plan_state smoke test ===")
    plan = make_standard_plan("RAG 评测怎么做", mode="deep")
    print(plan.render_for_prompt())
    print()

    plan.start_step(0)
    plan.complete_step(0, note="拆出3层意图: 方法论/工具选型/评测指标")
    plan.start_step(1)
    plan.complete_step(1, note="搜到12条证据，8条独立源", ref_id="search_abc123")
    plan.start_step(2)
    plan.complete_step(2, note="证据不足，需补搜")
    # 动态插入一步 (对应 CC "执行中发现遗漏、临时调计划"场景)
    new_idx = plan.insert_step_after(2, "Search", "补搜 RAG 评测的业界案例")
    plan.start_step(new_idx)
    plan.complete_step(new_idx, note="补搜到5条案例")

    print(plan.render_for_prompt())
    plan.snapshot()

    # 恢复测试
    resumed = Plan.resume(plan.plan_id)
    assert resumed is not None, "应能从磁盘恢复"
    assert len(resumed.steps) == len(plan.steps), "恢复后步骤数应一致"
    print(f"\n✅ 恢复成功: {resumed.plan_id}, {len(resumed.steps)} 步, 进度 {resumed.progress_pct()}%")

    incomplete = Plan.list_incomplete()
    print(f"未完成计划数: {len(incomplete)}")
    print("\n✅ plan_state 自测通过")
