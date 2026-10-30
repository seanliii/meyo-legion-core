#!/usr/bin/env python3
"""
qa_retry_policy · 自适应重试策略 (v58)

用户 2026-08-06 需求:
  「可以按资源情况和上下文和意图紧迫度、任务难度去尝试改变尝试次数，
   模仿人类」

背景 (来自 v57 A/B 实测的真实教训):
  auto agent 跑「访谈转写归类」题时，run_parallel_search 连续 3 次
  0 命中(系统负载 load_1min 一度到 46)，agent 每次都乖乐地重试，
  白白多耗了 400+ 秒，最后反而比 pipeline 慢 59%——这不是「LLM 不够
  聪明」，是它压根不知道「现在系统多卡」「这题多急」「已经试过几次
  等价 query 了」，只能凭 system prompt 里一句笼统提示自己拿捏。

设计: 人在决定「还要不要再试一次」时的直觉，大概是这四个信号的合成:
  1. 资源情况 (resource)   —— 机器越卡，越倾向见好就收
  2. 意图紧迫度 (urgency)  —— 越急，越倾向少等、快交付 (哪怕差一点)
                               但同时也可能「豁出去多等一次求最优解」——
                               这里取「不紧急才愿意多花时间试」的保守解读，
                               紧迫时优先止损交付
  3. 任务难度 (task_level) —— 越难/越重要的题，越值得多花一次力气搜
  4. 已尝试次数 (attempt)  —— 前面试的次数越多、收益递减，继续试的
                               边际价值越低 (尤其是连续 0 命中时)

  这四个信号不是加权算分，是按人类会问自己的顺序做门槛判断：
  「这题急吗？急就别试了」→「机器卡吗？卡就至多再试1次」→
  「这题值得死磕吗？值得就多给1-2次」→「已经试过几次同类操作了？
  超过硬上限就不再试，转兜底路径」。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SEED_ROOT = os.path.dirname(os.path.dirname(HERE))
V11_EVOLUTION = os.path.join(SEED_ROOT, "v11", "evolution")
if V11_EVOLUTION not in sys.path:
    sys.path.insert(0, V11_EVOLUTION)

# 硬上限: 不管信号怎么合成，同一个工具在同一次任务里最多重试这么多次。
# 防止极端情况下 (比如 prompt 被绕过) 陷入无限重试。
HARD_MAX_RETRY = 3


def _sample_resource():
    """复用 v11/evolution 现成的 perf_guard 资源探测，取不到就当"正常"处理
    (fail-open，不能因为探测失败就锁死重试策略)。"""
    try:
        from perf_guard import sample_perf, is_overloaded
        sample = sample_perf()
        overloaded, reason = is_overloaded(sample)
        return {"overloaded": overloaded, "reason": reason,
                "cpu_idle": sample.get("cpu_idle"), "load_1min": sample.get("load_1min")}
    except Exception as e:
        return {"overloaded": False, "reason": f"探测失败(默认当正常): {e}",
                "cpu_idle": None, "load_1min": None}


def decide_retry_budget(*, task_level: str = "标准", urgency: str = "normal",
                         cost_tier: str = "expensive", attempt: int = 0,
                         last_result_empty: bool = False) -> dict:
    """决定「这一类工具/这一次调用还能不能再试」。

    Args:
        task_level: "琐事" | "标准" | "重型" (来自 triage，任务难度信号)
        urgency: "high"(紧急，能不等就不等) | "normal"(默认) | "low"(不急，可以多磨)
        cost_tier: 工具的 cost_tier (cheap/medium/expensive)，只有 expensive
                   的工具(比如联网搜索)才真正需要这套重试预算控制；
                   cheap/medium 工具直接放行，不做过度设计。
        attempt: 这一类操作已经尝试过几次了 (从 0 开始)
        last_result_empty: 上一次尝试是不是「有效但没收获」(比如搜索 0 命中)，
                            用于「连续空转就别再试了」的判断。

    Returns:
        {"allow_retry": bool, "budget": int, "reason": str, "resource": dict}
        - budget: 综合各信号后，这一类操作在本次任务里总共"建议"的最大尝试次数
          (不含第 0 次「首次尝试」，是首次失败后还能补几次)
        - allow_retry: attempt < budget 时的便捷判断
    """
    resource = _sample_resource()

    if cost_tier != "expensive":
        # 便宜的判决类工具 (longcat-2.0 秒级) 不值得做这套精细控制，直接放行，
        # 人也不会纠结「要不要再问一遍 1+1 等于几」。
        return {"allow_retry": True, "budget": HARD_MAX_RETRY, "resource": resource,
                "reason": "cheap/medium 工具不做重试预算控制，直接放行"}

    # ── 第一道门槛: 紧急就别试了，先用已有的东西兜底交付 ──
    if urgency == "high":
        budget = 0 if attempt == 0 else 0
        return {"allow_retry": attempt < budget, "budget": budget, "resource": resource,
                "reason": "任务紧急(urgency=high)，优先用已有结果/通识兜底交付，不为搜索多等"}

    # ── 第二道门槛: 机器卡，最多再给 1 次机会 ──
    if resource["overloaded"]:
        budget = 1
        allow = attempt < budget
        return {"allow_retry": allow, "budget": budget, "resource": resource,
                "reason": f"系统资源紧张({resource['reason']})，最多再给 1 次重试机会，"
                          f"避免在过载时反复空转拖慢整体"}

    # ── 第三道门槛: 资源正常，按任务难度 + 是否连续空转决定 ──
    # v108(M1b·GAIA-165复盘): 预算语义已变——上游(qa_auto_agent)现在按【参数指纹】
    #   计数(不同 query/URL 不连坐·另受每工具12个独立query硬顶)。这里的 budget
    #   只约束【同一 query 的重试】: 瞬时网络抖动/引擎限流值得多试几次·旧值(标准1)
    #   太紧。琐事0→1·标准1→3·重型2→3。总量仍有 HARD_MAX_RETRY/步数/墙钟/多样性
    #   硬顶四道核·不会失控。
    # 重型题(GAIA式多域聚合/多跳)常需换多个渠道/分页才凑齐证据·同一 query 的重试
    #   上限比标准题高一档(3→5);琐事/标准不变。这是【任务难度】驱动的通用调节·
    #   不针对任何具体题。总量仍受步数/墙钟/多样性/每工具12指纹硬顶约束·不会失控。
    #   (根因实证 0b26:ScienceDirect 多域计数题·run_parallel_search 撞 3 次上限即停·
    #    gold 需要的域数据没凑齐→检索失败。放宽重型档让它有机会换渠道拉全。)
    base_budget = {"琐事": 1, "标准": 3, "重型": 5}.get(task_level, 3)
    if urgency == "low":
        base_budget += 1  # 不急，愿意多磨一次求更好的结果，模拟人类"有的是时间就多试试"
    if last_result_empty and attempt >= 1:
        # 已经空手而归过一次，再空转的边际收益低，收紧 1 档，别无限拉长
        base_budget = max(0, base_budget - 1)

    # 硬顶按难度分档:重型 5·其余仍 3(HARD_MAX_RETRY)。
    _hard_cap = 5 if task_level == "重型" else HARD_MAX_RETRY
    budget = min(_hard_cap, base_budget)
    return {
        "allow_retry": attempt < budget, "budget": budget, "resource": resource,
        "reason": (f"资源正常 · task_level={task_level} · urgency={urgency} · "
                   f"已尝试{attempt}次 · 上次空转={last_result_empty} → 预算{budget}次"),
    }


def render_retry_guidance_for_prompt(task_level: str = "标准", urgency: str = "normal") -> str:
    """生成一段可以直接塞进 auto agent system prompt 的重试指导文字，
    让 LLM 在决策时就已经知道"这题该多有耐心"，而不是等工具调用失败了
    才靠工具描述里的静态提示去纠正。"""
    resource = _sample_resource()
    lines = ["# 关于「要不要重试搜索」的指导 (模仿人类会怎么权衡)"]
    if resource["overloaded"]:
        lines.append(f"- 当前系统资源紧张({resource['reason']})，搜索类工具建议**最多重试 1 次**，"
                      "连续拿不到新证据就别再试了，直接用已有证据(哪怕很少)+通识继续往下走。")
    elif urgency == "high":
        lines.append("- 当前任务标记为紧急，优先用已有结果快速交付，不要为了追求更全的证据反复搜索。")
    else:
        budget = {"琐事": 1, "标准": 3, "重型": 5}.get(task_level, 3)
        lines.append(f"- 当前系统资源正常，这是「{task_level}」难度的题，同一个 query 最多值得重试约 "
                      f"{budget} 次(不同关键词/不同URL是新尝试·不占这个预算)；如果连续 "
                      "2 次同一 query 都 0 命中，说明这个 query 本身不对·换关键词或换能力，"
                      "不要原样重试。")
    _cap = 5 if task_level == "重型" else 3
    lines.append(f"- 任何时候「同一 query 已经试过 {_cap} 次以上」都应该停止重试，这是硬上限。")
    return "\n".join(lines)
