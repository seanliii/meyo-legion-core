#!/usr/bin/env python3
"""clarify_protocol · 2026-08-12 · 通用化 Phase1 · M-91澄清协议 + M-92假设账本

用户方向:G1题意歧义(GoW"2019game颁奖vs发行"/Bob规则/Cheater要哪指标 这3道纯枷锁)
在交互模式根本不存在——GAIA不许问必须猜·真实用户可反问。

M-91 澄清协议:triage 产的 ambiguity_axes 不再只写进 summary 让 agent 自觉·而是按
  interaction_budget 分流:budget>0 且有非空歧义轴 → 生成反问返回(不进内核);
  budget=0(GAIA) → 退回自主模式(内核照跑·行为不变)。
M-92 假设账本:budget=0 被迫做假设时·把非空 axes 结构化留痕
  {assumption, basis, confidence, falsifiable_by}——M-82污点追踪的镜像
  (M-82管推断值不许进工具参数·M-92管不得不推断时必须留痕)。

全在 run_task 层调用·内核 run_auto_agent 一字不改。should_clarify 是纯逻辑
(不调LLM·可秒级 selftest)·generate_clarifying_questions 才调 LLM。
"""
import os
import sys
import json
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# 歧义轴中文标签(与 qa_tool_impls.build_triage_context 同源)
_AXIS_LABEL = {
    "entity": "到底指哪个(哪个游戏/人/年份·如'2019'是颁奖年还是发行年)",
    "time": "时间/区间(累计vs单期·哪个时点)",
    "set": "集合(交/并/补·范围)",
    "version": "数据版本/用哪个来源",
    "perspective": "视角(谁的最大/最小·哪种口径)",
    "aggregate": "多项如何汇总(求和/平均/排序)",
    "nominal": "是否用简化口径",
}


def _nonempty_axes(axes):
    """从 ambiguity_axes(dict 或 list)提取真填了内容的轴 → [(key, value)]。"""
    out = []
    if isinstance(axes, dict):
        for k, v in axes.items():
            if isinstance(v, str) and v.strip():
                out.append((k, v.strip()))
    elif isinstance(axes, list):
        for item in axes:
            s = str(item).strip()
            if not s:
                continue
            if ":" in s:
                k, v = s.split(":", 1)
                out.append((k.strip(), v.strip()))
            else:
                out.append(("", s))
    return out


def should_clarify(axes, interaction_budget) -> bool:
    """纯逻辑(不调LLM·可秒级自检):有非空歧义轴 且 允许反问(budget>0) → 该反问。
    GAIA(budget=0)永远 False → 退自主模式(内核照跑·165等价不破)。"""
    try:
        if not interaction_budget or int(interaction_budget) <= 0:
            return False
    except Exception:
        return False
    return len(_nonempty_axes(axes)) > 0


def generate_clarifying_questions(axes, raw_input, budget=3):
    """把非空歧义轴转成给用户的反问(≤budget条)。调LLM生成自然反问·失败退回轴的原文。
    fail-safe:LLM挂就用轴原文拼(仍是有效反问·不阻断)。"""
    pairs = _nonempty_axes(axes)[:max(1, int(budget or 3))]
    if not pairs:
        return []
    # 先备一份 fail-safe 反问(轴原文·LLM挂也能用)
    fallback = [f"关于「{_AXIS_LABEL.get(k, k)}」:{v}?" for k, v in pairs]
    try:
        from qa_clarify import _call_llm, _parse_json  # 复用现成LLM通道
        axes_str = "\n".join(f"- {_AXIS_LABEL.get(k, k)}: {v}" for k, v in pairs)
        prompt = f"""用户问了一个有歧义的问题·下面是识别出的歧义点。为每个歧义点生成一句【自然、口语化的反问】·帮用户澄清到底要哪个。

# 用户原问
{raw_input[:500]}

# 歧义点(每个都要生成一句反问)
{axes_str}

# 输出严格JSON(questions数组·每条是一句反问·≤{len(pairs)}条)
{{"questions": ["反问1", "反问2"]}}
只输出JSON。"""
        raw = _call_llm("longcat-2.0", prompt, timeout=30)
        d = _parse_json(raw) or {}
        qs = d.get("questions")
        if isinstance(qs, list) and qs:
            return [str(q).strip() for q in qs if str(q).strip()][:len(pairs)]
    except Exception:
        pass
    return fallback


# ── 提案G1: 前置可解性检查(2026-08-14·从'题干截断却跑完全流程再靠finalize兜底'错题固化)
# 高确信截断信号才判不可解·纯逻辑·fail-safe(任何异常→judged solvable·绝不误杀正常题)
_TRAILING_PREP = re.compile(
    r"\b(as|of|in|on|at|to|for|with|from|by|the|a|an|and|or|that|which|whose|whom|than|"
    r"between|about|into|onto|over|under|show(?:s|n|ing)?|list(?:s|ed|ing)?)\s*$",
    re.I)


def check_solvable(question, has_required_file=None):
    """前置可解性检查。返回 {solvable, reason, signal}。
    只在【高确信截断信号】命中时判 not solvable——避免误伤正常但长/表述不完整的题。
    纯逻辑·不调LLM·fail-safe:任何异常→solvable=True(内核照跑·165等价不破)。

    信号(需高确信):
      - trailing_prep : 题干结尾悬空介词/冠词/连词(半句被截·如 '...May 14, 2017 as ')
      - dangling_open : 结尾是开括号/引号未闭合 且 无问号/句号
    has_required_file=False 且题干明说需附件 → missing_file(可选·调用方传入)
    """
    try:
        q = (question or "").strip()
        if not q:
            return {"solvable": True, "reason": "", "signal": ""}  # 空题不由这里判(别处处理)
        # 去掉末尾的策略提示块(非题目内容)
        for marker in ("[本次重跑的策略提示", "[进化提示", "\n\n[进化"):
            idx = q.find(marker)
            if idx > 0:
                q = q[:idx].strip()
        if not q:
            return {"solvable": True, "reason": "", "signal": ""}

        # 信号1:结尾悬空介词/冠词/连词(高确信半句截断)——且结尾没有问号/句号/引号收尾
        tail = q[-1]
        if tail not in "?.!\"')]}。？！" and _TRAILING_PREP.search(q):
            return {"solvable": False,
                    "reason": f"题干疑似被截断(结尾悬空:...{q[-30:]!r})",
                    "signal": "trailing_prep"}

        # 信号2:结尾是开符号未闭合 且 全句无终止标点
        if tail in "([{\"'" and not any(c in q for c in "?.!。？！"):
            return {"solvable": False,
                    "reason": f"题干疑似未闭合(结尾开符号:...{q[-20:]!r})",
                    "signal": "dangling_open"}

        # 信号3(可选):明说需附件却没附件
        if has_required_file is False:
            low = q.lower()
            if any(w in low for w in ("attached", "the image", "the file", "the spreadsheet",
                                      "the document", "the audio", "the video", "provided image")):
                return {"solvable": False,
                        "reason": "题干引用附件但未提供文件",
                        "signal": "missing_file"}

        return {"solvable": True, "reason": "", "signal": ""}
    except Exception:
        # fail-safe:任何异常绝不误杀·退回可解(内核照跑)
        return {"solvable": True, "reason": "", "signal": ""}


def build_assumption_ledger(axes):
    """M-92:budget=0 被迫做假设时·把非空轴结构化成假设账本随答案输出。
    让用户/评分一眼看到答案建在哪些沙地上。纯逻辑·不调LLM。"""
    ledger = []
    for k, v in _nonempty_axes(axes):
        ledger.append({
            "axis": k or "?",
            "assumption": f"歧义未澄清·自主选了常见解读({_AXIS_LABEL.get(k, k)})",
            "basis": v,
            "confidence": "low",   # 被迫猜=低置信
            "falsifiable_by": "用户明确指定该轴的取值即可推翻",
        })
    return ledger


if __name__ == "__main__":
    # 自测:should_clarify 分流(纯逻辑)
    axes = {"time": "发布月累计vs当月", "set": "", "perspective": "谁的min"}
    assert should_clarify(axes, 3) is True, "有歧义轴+budget>0该反问"
    assert should_clarify(axes, 0) is False, "GAIA budget=0永远不反问(165等价)"
    assert should_clarify({}, 3) is False, "无歧义轴不反问"
    assert should_clarify({"time": ""}, 3) is False, "空轴不反问"
    print("✅ should_clarify分流:有轴+budget>0→反问·GAIA budget0→不反问·空轴→不反问")
    # 假设账本
    led = build_assumption_ledger(axes)
    assert len(led) == 2 and led[0]["confidence"] == "low", "账本记非空轴·低置信"
    print("✅ 假设账本:", json.dumps(led[0], ensure_ascii=False)[:100])
    # fail-safe反问(不调LLM时退轴原文)
    fb = generate_clarifying_questions(axes, "test", budget=3)
    print("✅ 反问(可能LLM可能fallback):", fb[:2])

    # 提案G1: 可解性检查 — 正例(截断题该判不可解)
    r = check_solvable("The photograph was taken on May 14, 2017 as ")
    assert r["solvable"] is False and r["signal"] == "trailing_prep", f"截断题该拦: {r}"
    r = check_solvable("Which museum holds the painting whose")
    assert r["solvable"] is False, f"半句从句该拦: {r}"
    r = check_solvable("What is the value shown in (")
    assert r["solvable"] is False and r["signal"] == "dangling_open", f"未闭合该拦: {r}"
    r = check_solvable("Describe the image", has_required_file=False)
    assert r["solvable"] is False and r["signal"] == "missing_file", f"缺附件该拦: {r}"
    # ★反例(正常题绝不能误杀·这是proposal点名的风险)
    assert check_solvable("What is the capital of France?")["solvable"], "正常问句误杀"
    assert check_solvable("List all prime numbers under 100.")["solvable"], "正常陈述句误杀"
    assert check_solvable("How many of them survived to 2019?")["solvable"], "正常长题误杀"
    assert check_solvable("The report covers data from 2019.")["solvable"], "结尾年份+句号不该拦"
    assert check_solvable("Compute 3/4 + 1/8 and simplify.")["solvable"], "含分数正常题不该拦"
    assert check_solvable("")["solvable"], "空题不由此判(别处处理)"
    # 带策略提示尾巴的截断题(去尾后仍截断→拦)
    assert not check_solvable("...taken on May 14, 2017 as \n\n[进化提示] xxx")["solvable"], "去策略尾后仍截断该拦"
    # 带策略尾巴的正常题(去尾后正常→放行)
    assert check_solvable("What year?\n\n[进化提示] use new tool")["solvable"], "正常题带尾巴该放行"
    print("✅ 可解性检查:4类截断信号拦住·8个正常题全放行(无误杀)")
    print("\n✅✅ clarify_protocol 全通(M-91分流+M-92账本·纯逻辑部分秒级)")
