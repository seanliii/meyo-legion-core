#!/usr/bin/env python3
"""v38 · 多选题澄清模式 (可多选)

用户 2026-07-24 抓「其实很像 Claude 连续问题·如果你信息不全就给多个选择题让用户点击多题可多选·完成信息收集进而收敛问题·直到你觉得不需要问这个」

设计:
- longcat-2.0 判用户 query 有几个「关键维度缺失」(角色/规模/阶段/形态/预算/时间…)
- 每个缺失维度 → 生 1 道多选题 (3-5 选项 · 可多选 · 有「不确定」兜底)
- **一次性** 全部题给用户 (不是逐题问·省往返)
- 用户答完后 · 把答案作为 hint 塞进 scenarios · 重新跑 orchestrator

触发时机:
- 默认 --clarify 关 · 追求快速
- 用户显式 `--clarify` 或 `MEYO_QA_CLARIFY=1` 开
- 环境判「关键维度缺失度 >=2」自动开 (可选)

调用:
  from qa_clarify import maybe_clarify
  hints = maybe_clarify(question, scenarios)  # 返 dict · 空 dict = 用户跳
"""
import os
import sys
import json
import re
import subprocess

from qa_llm_gateway import text as _gateway_text


def _call_llm(model, prompt, timeout=45):
    return _gateway_text(model, prompt, timeout=timeout)


def _parse_json(text):
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {}


def generate_clarify_questions(question, scenarios, max_qs=4):
    """longcat-2.0 生 2-4 道多选题
    每题结构:
      { "dim": "role", "text": "你是哪个角色?",
        "options": ["SaaS PM", "C 端产品", "B 端销售", "教培运营", "不确定/其他"],
        "multi": true }
    """
    scen_str = "、".join(scenarios[:5]) if scenarios else "未指定"
    prompt = f"""为下面这个问题·判断有几个关键维度用户没说清楚·每个缺失维度生 1 道多选题让用户勾选。

# 用户原问
{question}

# 已识别场景 (LLM 猜的·可能不准)
{scen_str}

# 常见需要澄清的维度
- role: 用户角色 (SaaS PM / C 端产品 / B 端销售 / 教培运营 / HR / 咨询…)
- scale: 团队/公司规模 (10 人内 / 10-100 / 100-1000 / 大厂)
- stage: 产品阶段 (MVP / PMF / 规模化 / 存量运营)
- form: 产品形态 (订阅 SaaS / 一次性买断 / 免费+增值 / 硬件+软件)
- budget: 预算 (0 / 100 元内 / 1000 内 / 无限制)
- horizon: 时间跨度 (一次性 / 周复盘 / 月度 / 季度)
- data_available: 已有数据 (纯冷启 / 部分埋点 / 完整数仓)

# 判断标准
- 只在**真会改变答案方向**时才问 · 别浪费用户
- 如果原问已明确 (比如「续费流失访谈问题设计」暗含用户是 PM) 就不问 role
- 每题 3-5 选项 · **必须有「不确定/多个/其他」兜底**
- 最多 {max_qs} 题 · 太多用户嫌烦

# 严格 JSON
{{
  "should_clarify": true/false,
  "reason": "20 字",
  "questions": [
    {{
      "dim": "role",
      "text": "你(或团队里主要跑这事的人)是哪个角色?可多选。",
      "options": ["SaaS PM", "C 端产品经理", "B 端客户成功/销售", "教培运营", "不确定/多个都有"],
      "multi": true
    }},
    {{
      "dim": "scale",
      "text": "你的团队/公司规模?",
      "options": ["10 人以内 (小团队)", "10-100 (成长期)", "100-1000 (中型)", "大厂 (1000+)", "不确定"],
      "multi": false
    }}
  ]
}}
只输 JSON:"""
    r = _call_llm("longcat-2.0", prompt, timeout=45)
    d = _parse_json(r)
    return d


def render_questions_for_stdin(clarify):
    """把 questions 渲染给终端·让用户输入 '1a,1b,2c' 这种"""
    if not clarify or not clarify.get("should_clarify"):
        return None
    qs = clarify.get("questions", [])
    if not qs:
        return None
    lines = []
    lines.append("═" * 60)
    lines.append("我需要先问你几个问题·确保回答不偏 (可多选·回车跳过):")
    lines.append("═" * 60)
    lines.append(f"[判断依据] {clarify.get('reason','')}")
    lines.append("")
    for i, q in enumerate(qs, 1):
        multi_hint = "·可多选" if q.get("multi") else "·单选"
        lines.append(f"Q{i}. {q.get('text','')} {multi_hint}")
        for j, opt in enumerate(q.get("options", []), 1):
            letter = chr(96 + j)  # a, b, c...
            lines.append(f"    {i}{letter}) {opt}")
        lines.append("")
    lines.append("━" * 60)
    lines.append("输入格式: 1a,1c,2b (逗号分隔·每项 = 题号+字母)")
    lines.append("跳过全部: 直接回车")
    lines.append("━" * 60)
    return "\n".join(lines)


def parse_user_answer(answer_str, clarify):
    """把 '1a,1c,2b' 解成 {'role': ['SaaS PM','B 端客户成功/销售'], 'scale': ['10-100 (成长期)']}"""
    if not answer_str or not answer_str.strip():
        return {}
    hits = {}
    qs = clarify.get("questions", [])
    # 兼容中文逗号
    parts = re.split(r"[,,\s]+", answer_str.strip())
    for p in parts:
        m = re.match(r"(\d+)([a-zA-Z])", p.strip())
        if not m:
            continue
        qi, letter = int(m.group(1)) - 1, m.group(2).lower()
        oi = ord(letter) - ord("a")
        if 0 <= qi < len(qs):
            q = qs[qi]
            opts = q.get("options", [])
            if 0 <= oi < len(opts):
                dim = q.get("dim", f"q{qi}")
                hits.setdefault(dim, []).append(opts[oi])
    return hits


def hints_to_scenarios(hints):
    """把 {'role': [...], 'scale': [...]} 转成 scenarios 字符串 list · 拼进原 scenarios"""
    if not hints:
        return []
    scen = []
    for dim, vals in hints.items():
        v = " / ".join(vals)
        if "不确定" not in v and "多个" not in v:
            scen.append(f"{dim}={v}")
    return scen


def maybe_clarify(question, scenarios, interactive=None, auto_answer=None):
    """主入口
    interactive: None (默认由 --clarify / env 定) / True (强制交互) / False (跳过)
    auto_answer: 字符串 '1a,2b' 或 None (从 stdin 读)
    返: {'hints': {...}, 'answer_raw': '1a,2b', 'clarify': {...}}·空 dict = 用户跳
    """
    # 默认关 · 显式开
    if interactive is None:
        interactive = os.environ.get("MEYO_QA_CLARIFY", "0") == "1"
    if not interactive:
        return {}

    clarify = generate_clarify_questions(question, scenarios)
    if not clarify.get("should_clarify"):
        return {"clarify": clarify, "hints": {}, "answer_raw": "",
                "skip_reason": "longcat-2.0 判无需澄清"}

    rendered = render_questions_for_stdin(clarify)
    if not rendered:
        return {}

    print(rendered, file=sys.stderr, flush=True)

    if auto_answer is not None:
        ans = auto_answer
        print(f"[auto-answer] {ans}", file=sys.stderr)
    else:
        try:
            ans = input("你的选择: ").strip()
        except (EOFError, KeyboardInterrupt):
            ans = ""

    hints = parse_user_answer(ans, clarify)
    return {"clarify": clarify, "hints": hints, "answer_raw": ans,
            "scen_hints": hints_to_scenarios(hints)}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--question", required=True)
    ap.add_argument("--auto", default=None, help="e.g. 1a,2b")
    args = ap.parse_args()
    r = maybe_clarify(args.question, [], interactive=True, auto_answer=args.auto)
    print(json.dumps(r, ensure_ascii=False, indent=2))
