#!/usr/bin/env python3
"""v43.1 · 配套工具锥 (再修正·用户 2026-07-24 二次抓)

用户核心诉求 (原话):
  「不同认知/行为顺序匹不同工具形式·先给简单能立即上手的 MVP·帮他开工」
  「不一定都是时间顺序·都是要靠你正文的回复理解做的·不是死板的」

修正认知:
  错法 (v43 初版) · 硬套「MVP → 本周深化 → 长期架构」时间轴给所有题
  真意          · **MVP 硬规**(第一层必最简能立即开工) + **后续层根据正文段落逻辑自决**

层次由**正文段落逻辑决定** · 举例:
  正文段落逻辑           · 工具锥可能长什么样
  ─────────────────────────
  认知顺序 (新手→高阶)   · MVP=新手照抄脚本 → 进阶信号手册 → 高阶系统化
  行动顺序 (访前→访中→访后) · MVP=访前 15min 数据 → 访中追问树 → 访后归因表
  决策链顺序 (老板→使用者→一线) · MVP=一线版脚本 → 使用者版 → 老板版
  时间顺序 (取消当下→周内→月后) · MVP=取消弹窗 3 题 → 24h 电话 → 月复访
  因果顺序 (现象→根因→对策) · MVP=现象 checklist → 根因追问 → 对策 SOP
  长短期顺序 (短期挽回→长期预警) · MVP=挽回话术 → 预警指标 → 健康度模型

设计:
  1. longcat-2.0 判决题目类型 (methodology/tool_selection/decision/concept/process)
  2. opus 读自己 narrative 的段落逻辑段 → 决定工具锥的层次数(2-4 层)和展开顺序
  3. **唯一硬规**: 第一层必为 MVP (5-10 分钟可立即用) · 后续层自由
  4. **每层必带成品** · 表格/代码/prompt/URL · 不是「你去搜 X」空话
  5. **配套现有 nocode** · 若有必嵌入合适层 · 不抛开

调用:
  from qa_action_cone import generate_action_cone
  block = generate_action_cone(question, narrative, nocode_url=None)
"""
import os
import re
import json
import subprocess
import sys

from qa_llm_gateway import call as _gateway_call, text as _gateway_text


def _call_llm(model, prompt, timeout=45):
    return _gateway_text(model, prompt, timeout=timeout)


def _report_cone_failure(reason: str, question: str):
    """v56 · 用户抓「配套工具段为空」· 空结果不能只静默返回 ""
    把这次真实缺陷喂进统一演绎/进化闭环 (correction_evolver)，
    而不是让下一次运行原地重犯同一个问题。
    """
    try:
        HERE = os.path.dirname(os.path.abspath(__file__))
        QA_ROOT = os.path.dirname(HERE)
        SEED_ROOT = os.path.dirname(QA_ROOT)
        v11_evo = os.path.join(SEED_ROOT, "v11", "evolution")
        if v11_evo not in sys.path:
            sys.path.insert(0, v11_evo)
        from correction_evolver import ingest as _ce_ingest
        _ce_ingest(
            text=f"qa_action_cone 生成失败: {reason} · question={question[:120]}",
            context="output_gate:qa_action_cone_empty",
            markers=["qa_action_cone_empty"],
        )
    except Exception:
        pass


def _parse_json(text):
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {}


def judge_action_type(question):
    """v54 · 用户 2026-07-27 抓「Q6 流程图 longcat-2.0 判 process 结果生 3 层·太复杂」
    修: 更细的判决 · 工具选型题标 tool_selection · 1-2 层给 URL 够

    v56 · 用户抓「全接入」验证时发现: 题目开头是「XX 分别是什么」定义句式，
    但结尾明确要「给出 XX 方案/SOP/怎么组合用」——longcat-2.0 只看到开头关键词就
    判成 concept，导致配套工具段整段被跳过 (generate_action_cone 对
    concept 类型直接 return "")。修: 把「结尾有行动诉求」列为最高优先级，
    压过开头的定义句式判断。
    """
    prompt = f"""判用户问题需要什么形式的「开始行动」段:

题: {question}

类型判决 (关键词看题目里有啥):
- **tool_selection** · 题含「哪个/什么工具/如何用 X 生成 Y」等 · 用户想要**推荐 + 使用说明** · 1-2 层给 URL 够
- **methodology** · 题含「如何设计/怎么写/框架」等 · 需要方法论 SOP + 追问树 · 3-4 层
- **decision** · 题含「是否上/怎么选/该不该」等 · 决策题 · checklist / decision tree
- **concept** · 题**只**问「什么是 X/为什么 Y」且**全程无任何行动诉求** · 概念解释·**不需行动**
- **process** · 题含「从零搭建/工程化落地/CI/CD」等 · 系统设计·多阶段 agent 架构

**判决优先级 (从高到低·前面命中就不用看后面)**:
1. **最高优先·别被开头的「是什么/分别是」定义句式骗** · 只要题目**任何位置**出现
   「给出方案/怎么组合/怎么落地/SOP/流程/该怎么用/三阶段/怎么选」等行动/落地诉求词·
   一律判 **methodology** 或 **process** (系统性组合流程判 process·单一方法判 methodology)·
   **禁止**因为开头是定义句式就判 concept
2. 题含工具名 (Mermaid/GitHub/飞书/钉钉) 或「自动生成/追踪/监控」 → tool_selection
3. 题含「设计/框架/怎么问/如何做」抽象方法 → methodology
4. 题含「体系/落地/工程化」系统词 → process
5. 只有题目**完全没有**上述任何行动诉求、纯粹问定义/原理时才判 concept

严格 JSON: {{"type": "methodology|tool_selection|decision|concept|process", "reason": "20 字·具体触发词"}}
只输 JSON:"""
    r = _call_llm("longcat-2.0", prompt, timeout=15)
    d = _parse_json(r)
    action_type = d.get("type", "methodology")

    # v56 · 二次兜底 · 纯 regex 检测行动诉求词，防止 longcat-2.0 判决仍然漏判
    # (LLM 判决本身有不确定性，不能只信一次调用结果)
    _action_markers = ["给出", "怎么组合", "怎么落地", "SOP", "三阶段", "如何组合",
                        "该怎么用", "怎样组合", "评估方案", "落地方案", "执行方案"]
    if action_type == "concept" and any(m in question for m in _action_markers):
        action_type = "methodology"
    return action_type


def generate_action_cone(question, narrative, nocode_url=None, question_type=None):
    """基于 narrative 生智能行动锥
    返: markdown block (可空字符串 · 若 concept 类)
    """
    if question_type is None:
        question_type = judge_action_type(question)

    if question_type == "concept":
        return ""  # 无需行动

    # narrative 摘要
    narr_head = narrative[:2000]

    # v44 · type_hint 保留提示 · 但每层要求带 4 个元素 (工具/skill/github/nocode)
    type_hint = {
        "methodology": "工具形式: 话术模板 / SOP 表 / 追问树 / 归因分类枝 / agent 化拆解",
        "tool_selection": "工具形式: 推荐工具+URL / 候选对比表 / 评估回顾模板",
        "decision": "工具形式: 快速 checklist / decision tree / 验证节点表",
        "process": "工具形式: 最小闭环 / 完整 SOP / 角色分工表 / agent 架构 prompt",
    }.get(question_type, "自决工具形式")

    prompt = f"""基于下面这题和已有回复·生成「配套工具」段·**融合『想深入哪块』的深挖入口**。

# 原题
{question}

# 已有回复摘要 (⚠️ **重点看段落逻辑段** · 工具展开顺序要贴这个逻辑)
{narr_head}

# nocode 部署 URL (v48 硬规·用户 2026-07-27 抓「不能编 URL」)
# 状态 · {"有" if nocode_url else "无 · 禁编域名"}
# 若"有"·必须一字不差用: {nocode_url or "(无·下面所有层禁写 nocode 部署段·禁编 .meyo.app / .mynocode.host)"}

# 题目类型: {question_type}
# 参考工具形式: {type_hint}

# ═══ v48 元规则 · 用户 2026-07-27 抓「每层智能判工具形式·不硬套 nocode」 ═══

**这段本质是「开始行动」·不是「卖 SaaS 工具」** · 让用户看完立即能干活。

**每层的工具形式由 longcat-2.0 智能判** · 不是全塞 nocode。参考选择依据:
- **nocode 部署** · 需要**交互输入输出**才有价值 (例: 填名单→分组→出脚本)
- **skill / 现成组件** · 已有市面工具能干 (例: 通义听悟做转写·飞书文档做协作)
- **github repo** · 需要代码级复用 (例: prompt 模板·数据结构·工作流)
- **内容深度链接** · 深度理论/复盘/长文 (例: HBR 文章·Reforge 课程)
- **纯 markdown 表格/prompt** · 简单可打印 (例: 5 段访谈脚本·checklist)

**判决依据**:
- 数据要处理 → nocode / skill
- 内容要学 → 内容链接 / github
- 直接照做 → 纯 markdown 成品

**每层不是必须 5 元素全有** · 有啥给啥·**opus 判每层最需要哪 1-2 种**:
- MVP 层 (第 1 层) · 可能只需**打印卡** (5 分钟能用)
- 深化层 · 可能是 **nocode + 说明** (需要交互)
- 长期层 · 可能是 **github + skill** (复用)

# ═══ nocode URL 硬规 · v52 用户 2026-07-27 抓「同一 URL 两个标题嵌 2 层」 ═══

**只有传入的 nocode_url 非空**·才允许写 nocode 部署段。传入为空·**禁编造域名**！

**同一个 nocode_url 只能嵌 1 层**·哪层最合适就嵌哪层 (通常 MVP 或最需要交互输入输出的层)。

若 nocode_url 非空 · 在最合适的**唯一一层**里写:
```
**nocode 部署** · [唯一页面标题](实际传入的 nocode_url · 一字不差)

**打开怎么用** (100-150 字·具体动作序):
1. 打开填 X 字段 (例「产品名·流失名单 CSV」)
2. 点 Y 按钮 (例「生成访谈计划」)
3. 得 Z 结果 (例「按 3:5:2 分组的问题脚本」)
4. 然后 W (例「访完粘转写点洞察」)
```

**禁忌 (v48 用户 2026-07-27 抓「meyo.app 假域名」+ v52 抓「同 URL 2 层」)**:
- ❌ 编造 `.meyo.app` / `.mynocode.host` 等域名 · 只用传入的
- ❌ nocode_url 为空时·**禁硬凑一个 nocode 段** · 换成 skill / github / 内容深度
- ❌ 只写「nocode 真跑生成」这种没说明的
- ❌ **同一个 URL 不能嵌 2 层** (v52) · 一 URL 只 1 层 · 别的层用别的工具形式 (skill/github/markdown)
- ❌ **同一个 URL 不能用 2 个不同标题** (v52) · 只 1 个真标题·多层引用同 URL 就复用同一标题
- ✅ 真域名只有 `.mynocode.host` (来自 nocode-cli deploy 输出)

# ═══ 好标题特征 (描述特征·非硬规) ═══
- **具体感 > 硬套结构** · 「访前 30 分钟 · 样本筛选」比「挖真因四板斧」信息量高
- **数字量化** · 「五层追问树」比「追问树」明确
- **动词开头** · 「筛」「拉」「问」「归」有落地感

# ═══ 基本要求 (仍是硬规) ═══
1. **第 1 层必是 MVP** · 5-10 分钟能立即用
2. **层数** · 按题目类型自决:
   - **tool_selection 类** · **1-2 层够**·MVP=推荐工具+URL+使用说明·别硬凑复杂
   - **methodology 类** · 3-4 层·完整方法论 SOP
   - **process 类** · 3-5 层·系统设计多阶段
   - **decision 类** · 1-2 层·checklist / decision tree
   - **禁**「工具选型题硬凑 4 层」(v54 用户 2026-07-27 抓 Q6 复杂)
3. **每层名字唯一** · 不能重复
4. **展开顺序** = 贴 narrative 段落逻辑 (longcat-2.0 自决·不硬套访前访中访后)
5. **每层工具形式智能选** · 不全塞 nocode
6. **国内视角** · 飞书/腾讯/字节优先

# ═══ 禁忌 ═══
- ❌ 「立即上手 (5 分钟能开工)」/「本周深化 (2-8h)」这种硬时间轴
- ❌ 空动作清单「打开 X 加字段」·必须给**成品**
- ❌ 「中国」→ 用「国内」·「用户」→ 保留
- ❌ 中文段用 `·` 当逗号

# 严格 markdown 格式 (v48 · longcat-2.0 智能判每层最合适的形式)
```
### ▶ [层标题·贴段落逻辑关键词·每层唯一名]

**这一步做什么** · 1 句话·让用户懂

[基于 longcat-2.0 判此层类型·输出 1-3 种最合适的:]

[若适合 nocode:]
**nocode 部署** · [页面标题](URL)

**打开怎么用**:
1. 填 [具体字段]
2. 点 [具体按钮]
3. 得 [具体输出]
4. 然后 [具体动作]

[若适合 github/skill/现成工具:]
**参考工具** · [名称](URL) · 1 句话说为什么用这个

[若适合内容学习:]
**深度阅读** · [文章标题](URL) · 1 句话说学什么

[必带 · 行动指引:]
**行动指引** · 1 句话·用户看完知道下一步做啥

---

### ▶ [下一层标题·和前面不重复]
[longcat-2.0 判此层最合适的形式·可以完全不同]
...
```

只输 markdown · 不加代码块包裹 · 不加解释:"""
    # v56 · 用户抓「配套工具段为空缺表格与prompt」· 单次调用无重试+无失败上报
    # 修: 空结果重试一次 (不同 timeout 排除瞬时超时)；仍空则上报进化闭环，不静默吞掉
    r_dict = _gateway_call(prompt, timeout=150)
    out = r_dict.get("out", "")
    if not out:
        r_dict = _gateway_call(prompt, timeout=220)
        out = r_dict.get("out", "")
    if not out:
        _report_cone_failure(
            reason=r_dict.get("err", "empty_after_retry")[:200],
            question=question,
        )
        return ""
    # v55.3 · 用户 2026-07-27 抓「内部版本号读者看不懂」· 干净标题
    return "## 开始行动\n\n" + out + "\n"


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--question", required=True)
    ap.add_argument("--nocode", default=None)
    args = ap.parse_args()
    t = judge_action_type(args.question)
    print(f"[type] {t}")
    print(generate_action_cone(args.question, "示例 narrative", nocode_url=args.nocode))
