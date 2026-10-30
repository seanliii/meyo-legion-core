#!/usr/bin/env python3
"""v44 · step8.5 · 五问自检 (用户 2026-07-24 第二人建议 + 2026-08-05 加 Q5)

五问 (longcat-2.0 跑):
  Q1: 结论都有证据吗? 每个 ### 段落是否有 URL 超链引用
  Q2: 证据都无严重立场偏吗? 主线源里厂商源是否超 1/3
  Q3: 段落逻辑贴题吗? 段落顺序是否响应用户认知/行动路径
  Q4: 行动可落地吗? 配套工具是否有真成品/真 nocode/真行动指引
  Q5: 关键概念/术语归类站得住脚吗? (v56h · 2026-08-05 新增)
      治「结构/证据/立场全部检查通过·但内容本身有专业性错误」这类盲区——
      实测抓到过 qa_agent 把 LangGraph(通用图编排引擎) 和 CrewAI/AutoGen
      (Agent-first 框架) 笼统并列成「开源 Agent 框架三剑客」，Q1-Q4 全部
      pass，因为证据链/立场/格式都没问题，错的是「归类/类比本身」——这类
      语义层面的错误，旧的 4 问结构化检查天然查不出来，只能靠专门问一遍。

任意 fail → opus 修

调用:
  from qa_self_check import run_self_check
  final, report = run_self_check(question, final)
"""
import os
import re
import json
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


def run_self_check(question, final):
    """四问自检 · 返 (final, report)
    fail 太多才让 opus 修 · 只 1-2 处小 fail 只 log 不改
    """
    # 只截取「回答」和「配套工具/开始行动」段自检 · 头部展示段跳过
    # v56 修复 ①: 工具锥标题在 v55.3 已从「## 配套工具」改成「## 开始行动」，
    # 但这里的正则一直没跟着改 → tool_body 永远抓空 → Q4 必然误判 fail。
    # v56 修复 ②: lookahead 用的 `##\s` 会连 `### ▶ 层标题` 这种三级标题也
    # 当成「下一个二级段」提前截断 (### 本身含 ##)，导致工具段只抓到标题两
    # 个字就被切了。改用「按行首严格两个#、非三级」切段落」，不再用
    # inline (?m) 拼接 (拼在表达式中间会被 Python re 报 "global flags not
    # at the start" 语法错)。
    # 用户 2026-08-04「全接入」压测才把这几处历史 bug 暴露出来。
    _h2_pat = re.compile(r'^##(?!#)\s*(.*)$', re.MULTILINE)
    _sections = {}
    _matches = list(_h2_pat.finditer(final))
    for i, mm in enumerate(_matches):
        title = mm.group(1).strip()
        start = mm.start()
        end = _matches[i + 1].start() if i + 1 < len(_matches) else len(final)
        _sections[title] = final[start:end]

    answer_body = _sections.get("回答", "") or final[:5000]
    tool_body = _sections.get("开始行动", "") or _sections.get("配套工具", "")

    # v56 修复: 长回答硬截 [:3000] 会把结尾截在句子中间，longcat-2.0 误判「回答被截断」。
    # 改成头部+尾部各留一段，中间省略号标注，让 longcat-2.0 看到「这是摘录不是全文」。
    def _head_tail(s, head=2200, tail=800):
        if len(s) <= head + tail:
            return s
        return s[:head] + "\n\n...(中间省略，非真实截断，仅自检摘录)...\n\n" + s[-tail:]

    answer_excerpt = _head_tail(answer_body)
    tool_excerpt = _head_tail(tool_body, head=1500, tail=500) if tool_body else ""

    prompt = f"""对下面这份回答做四问自检·输出 JSON 判决。

⚠️ 注意: 下面的【回答段】【配套工具段】是全文摘录 (可能含"...(中间省略，非真实截断，仅自检摘录)..."标记)，
这个标记本身不代表原文被截断，只是喂给你自检时省的篇幅，别把它当「正文写到一半断了」的证据。

# 原题
{question[:200]}

# 【回答段】(摘录)
{answer_excerpt}

# 【配套工具段】(摘录·若为空说明确实没有配套工具/开始行动段)
{tool_excerpt}

# 四问

## Q1 · 结论都有证据吗?
- 每个 ### 段落是否至少一处 [](URL) 超链引用
- 结论 (「必须做 X」「关键是 Y」这种) 是否有源支撑
- 判决: pass / fail (fail 时列出哪段无引用)

## Q2 · 证据都无严重立场偏吗?
- 有多少个引用带「(观点参考)」「利益相关」这种标注
- 主线源里厂商源 (卖同类产品的公司博客) 是否超 1/3
- 判决: pass / fail (fail 时列举厂商源比例)

## Q3 · 段落逻辑贴题吗?
- 是否有明确「段落逻辑段」(说清按什么顺序拆)
- 段落展开是否真按此顺序 (如说「访前→访中→访后」是否真这么排)
- 判决: pass / fail (fail 时说不匹配之处)

## Q4 · 行动可落地吗?
- 配套工具段是否每层有真成品 (markdown 表格 / prompt / URL 或 CLI 命令)
- 是否有「行动指引」1 句话
- 是否含真跑的 nocode URL
- 判决: pass / fail (fail 时列缺哪个)

## Q5 · 关键概念/术语归类站得住脚吗?
- 找出正文中所有「并列比较/归类」的表述 (如「A、B、C 都是 XX」「三大 XX」「XX 三剑客」)
- 对每一组，判断被并列的对象是否真的处于同一抽象层级/同一类别，
  还是把定位不同的东西 (如「通用底层工具」vs「该工具之上的一种应用形态」) 混着比了
- 只挑「明显站不住脚、内行一看就皱眉」的错误，不要吹毛求疵到「表述不够严谨」这种程度
- 判决: pass / fail (fail 时指出具体是哪一组、错在哪，10-30 字)

# 严格 JSON
{{
  "q1_evidence": {{"verdict":"pass|fail", "detail":"20 字"}},
  "q2_stance": {{"verdict":"pass|fail", "detail":"20 字"}},
  "q3_logic": {{"verdict":"pass|fail", "detail":"20 字"}},
  "q4_action": {{"verdict":"pass|fail", "detail":"20 字"}},
  "q5_concept": {{"verdict":"pass|fail", "detail":"30 字"}},
  "overall": "pass|need_fix",
  "critical_fails": ["最严重的 1-2 处·需 opus 修"]
}}
只输 JSON:"""
    r = _call_llm("longcat-2.0", prompt, timeout=45)
    d = _parse_json(r)
    if not d:
        return final, {"skipped": "longcat-2.0 fail"}

    # 渲染自检 block 追加到 final (作 debug 信息 · 不长)
    _keys = ["q1_evidence", "q2_stance", "q3_logic", "q4_action", "q5_concept"]
    fail_count = sum(1 for k in _keys if d.get(k, {}).get("verdict") == "fail")
    if fail_count == 0:
        block = "\n\n---\n> ✅ **v56h 五问自检** · 结论有证据 · 立场平衡 · 段落贴题 · 行动可落地 · 概念归类站得住脚\n"
    else:
        details = []
        for k, label in [("q1_evidence", "证据"), ("q2_stance", "立场"),
                          ("q3_logic", "段落逻辑"), ("q4_action", "行动"),
                          ("q5_concept", "概念归类")]:
            v = d.get(k, {})
            emoji = "✅" if v.get("verdict") == "pass" else "⚠️"
            details.append(f"{emoji} {label}: {v.get('detail','')}")
        block = f"\n\n---\n> **v56h 五问自检** · {fail_count}/5 fail:\n"
        for det in details:
            block += f"> - {det}\n"
    return final + block, d


if __name__ == "__main__":
    import sys as _s
    if len(_s.argv) < 3:
        print("usage: qa_self_check.py <question> <md_file>")
        _s.exit(1)
    q = _s.argv[1]
    md = open(_s.argv[2], encoding="utf-8").read()
    final, rep = run_self_check(q, md)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
