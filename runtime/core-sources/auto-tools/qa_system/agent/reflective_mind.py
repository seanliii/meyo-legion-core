"""reflective_mind · 2026-08-10 · 给agent一个"像用户一样有主体性的反思脑"(用户: 让agent像我一样具备主体性)

用户洞察: agent缺主体性——只是被动执行工具循环·不像真人会每步对着目标反思、主动搜/建/
注册/用工具、卡了换策略、必要时human-in-the-loop提醒用户。这个模块=agent可主动调的
"反思脑"·扮演一个像用户一样思考的主体·看【目标+已走的步骤】·产出下一步该怎么办。

它不替agent执行·只给"反思结论"(判断权仍在agent)。核心问题(模拟用户的思维):
  1. 离目标还差什么?(gap)
  2. 当前这条路有效吗?无效说明啥·该换什么结构性不同的路?
  3. 我需要的工具/能力有吗?没有能不能搜/建/装/委托别的agent?
  4. 卡死了要不要提醒用户(human-in-the-loop)?
经验沉淀: 反思结论可回写·下次同类卡点复用。
"""
import os
import json as _json


def _mm():
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import multi_model as m
    return m


_REFLECT_PROMPT = """你现在扮演一个【有主体性的顶尖问题解决者】(像一个IQ极高、会用一切资源的真人)·
帮正在解题的agent反思。别复述·直接给可执行的反思结论。

【要解决的目标】
{goal}

【agent已经走过的步骤(工具序列+结果摘要)】
{trace}

【当前卡点/现象】
{stuck}

【这台电脑能用什么(环境)】
{env}

【已从证据里识别出的候选锚点实体(枢轴)·可能有噪声】
{pivots}

像真人一样反思·特别注意"枢轴发现/证据图谱扩散"这条路径(情报侦查思维: 反复
同义改写关键词搜不到时，正确做法不是继续换措辞，而是从已经半确认的枢轴实体
出发做定向扩散——作者名→查该作者其他作品/个人主页；域名→查该域名下同期
其他内容/同类站点；日期+专辑/事件锚点→用结构化数据库精确反查而非关键词搜；
已知具体URL但页面下线→查存档快照而非搜索引擎)。上面的候选锚点如果有价值，
next_move 应该具体给出"用哪个枢轴、通过什么工具、扩散到哪个方向"，而不是
泛泛地"换个角度搜索"。输出JSON:
{{
  "gap": "离目标还差什么(一句话)",
  "path_working": true/false,  // 当前这条路有效吗
  "why_stuck": "如果无效·根因是什么(是方法错?工具错?还是真边界?)",
  "premise_audit": "前提审计(G1b·治自洽循环): 看步骤——最近几步是在【引入新的外部证据】·还是只在【自己的前提内反复验证/自洽】(越算越确信但没新证据流入)? 若是后者=危险信号·很可能最初的题意/语义建模就读偏了(如'谁的max/min'搞反、'累计vs单期'读错)·此时该【停下重读题意】而非继续验证。填: 'ok'(在引入新证据) 或 '自洽循环·建议重读题意:<哪条语义轴可能读偏>'",
  "pivot_diffusion": "如果候选锚点里有可用的·具体写出扩散路径(如'已知专辑1986年17曲→用query_structured_db查MusicBrainz反查具体专辑名→拿专辑名反查博客帖')·没有可用锚点则填空",
  "next_move": "下一步具体该干什么(换哪个工具/搜什么/建什么工具/委托谁)·要可执行",
  "next_tool": "如果下一步该调某个具体工具·填工具名(如see_video_frame/solve_chess_move/transcribe_audio/ask_local_llm/scan_environment/query_web_archive/query_structured_db等)·否则填空",
  "need_new_tool": "需要但还没有的工具/能力(没有填空)·以及怎么搞到(pip装/搜CLI/写脚本·或者直接调write_capability让agent自己生成代码工具)",
  "write_capability_hint": "如果 need_new_tool 非空且是一个算法/计算类能力·填写调 write_capability 的 problem_desc 参数内容(一句清晰的任务描述，如'用闭合约束方程组求解正交多边形缺失边，然后用鞋带公式计算面积')·否则填空",
  "ask_user": "如果卡死到该找人·要问用户什么(不需要填空)",
  "lesson": "这次卡点的通用教训(供下次同类题复用·一句话)"
}}
只输出JSON。"""


def extract_pivots(evidence, question="", max_items=8):
    """枢轴发现器(v2026-08-18·落地"情报侦查思维引擎设计.md"): 从已收集证据里
    提取候选锚点实体(人名/域名/专辑或作品名/日期/组织)，供反思脑做证据图谱扩散。

    定位: BrowseComp/GAIA 多跳题反复失败的一个典型模式是"同义改写死循环"——
    搜索多轮换关键词表述但从未真正沉淀出可复用的锚点。本函数不做语义理解，
    只做轻量规则抽取(域名/年份/大写专名片段)，抽取质量不要求高(反思脑会自己
    判断噪声)，目的只是把"证据里已经隐含但没被显式点出来"的线索递出来，
    让下一步决策看得见这些锚点而不是从头再猜。fail-safe: 空/异常返回空列表。
    """
    if not evidence:
        return []
    import re as _re_pv
    pivots = []
    seen = set()

    def _add(kind, val, strict=True):
        val = str(val or "").strip()
        # strict=通用启发抽取(标题/正文)·需 len>=3 防噪声; meta 结构化字段
        # (artist/album/blog_domain 等已解析好的)不过长度闸·短名(如艺人'X')也是真锚点。
        if not val or (strict and len(val) < 3):
            return
        key = (kind, val.lower())
        if key in seen:
            return
        seen.add(key)
        pivots.append({"kind": kind, "value": val})

    for e in evidence[:40]:
        if not isinstance(e, dict):
            continue
        url = str(e.get("url") or "")
        title = str(e.get("title") or "")
        content = str(e.get("content") or "")[:500]
        meta = e.get("meta") or {}
        # 域名(排除通用搜索引擎/大平台域名·那些不是有信息量的枢轴)
        # 正则要抓【完整多级域名】(如 myblog.blogspot.com/web.archive.org):
        # 旧版只捕最后两段→web.archive.org 被捕成'web.archive'·排除名单
        # endswith('web.archive.org') 匹配不上·熔断域混进枢轴列表。
        m = _re_pv.search(
            r"https?://(?:www\.)?([a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)*\.[a-z]{2,10})",
            url, _re_pv.I)
        if m:
            dom = m.group(1).lower()
            _generic = ("google.com", "bing.com", "duckduckgo.com", "wikipedia.org",
                        "youtube.com", "twitter.com", "x.com", "facebook.com",
                        "web.archive.org", "archive.ph", "reddit.com")
            if not any(dom == g or dom.endswith("." + g) for g in _generic):
                _add("domain", dom)
        # meta 里的结构化锚点(存档工具/musicbrainz等已经解析出的字段直接复用·strict=False)
        for k in ("blog_domain", "artist", "album", "original_url"):
            if meta.get(k):
                _add(k, meta[k], strict=False)
        # 年份(4位数·1900-2030 合理区间)
        for y in _re_pv.findall(r"\b(19[5-9]\d|20[0-3]\d)\b", title + " " + content):
            _add("year", y)
        # 标题里的引号短语/专名(简单启发·多带噪声但反思脑会甄别)
        for q in _re_pv.findall(r'"([^"]{3,60})"', title):
            _add("quoted_phrase", q)

    return pivots[:max_items]


def _format_pivots(pivots):
    if not pivots:
        return "(暂无从证据中识别出的候选锚点)"
    lines = []
    by_kind = {}
    for p in pivots:
        by_kind.setdefault(p["kind"], []).append(p["value"])
    for kind, vals in by_kind.items():
        lines.append(f"  · {kind}: {', '.join(vals[:5])}")
    return "\n".join(lines)


def reflect(goal, trace_steps, stuck_reason="", env_summary="", model="claude-sonnet",
            evidence=None):
    """反思脑: 看目标+步骤·产出下一步该怎么办。返回dict(agent读了自己决定)。
    trace_steps: [{tool, summary}] 或工具名列表。model用中档(便宜快)。
    evidence: 可选，传入已收集的证据列表时会先跑枢轴发现器，把候选锚点实体
    (域名/年份/结构化字段/引号短语)一并交给反思脑，让它能给出"用哪个枢轴
    往哪个方向扩散"的具体建议，而不是泛泛的"换个角度搜索"。"""
    # 压缩trace
    if trace_steps and isinstance(trace_steps[0], dict):
        tr = "\n".join(f"{i+1}. {s.get('tool','?')}: {str(s.get('summary',''))[:80]}"
                       for i, s in enumerate(trace_steps[-20:]))
    else:
        tr = " → ".join(str(t) for t in (trace_steps or [])[-25:])
    try:
        pivots = extract_pivots(evidence, question=goal) if evidence else []
    except Exception:
        pivots = []
    prompt = _REFLECT_PROMPT.format(
        goal=(goal or "")[:800], trace=tr[:2500],
        stuck=(stuck_reason or "(agent主动反思·未必卡住)")[:300],
        env=(env_summary or "(未探测·可先调scan_environment)")[:600],
        pivots=_format_pivots(pivots)[:800])
    try:
        r = _mm().ask(model, prompt, timeout=60, max_tokens=800)
        if not r["ok"]:
            return {"error": r["err"][:100], "next_move": "反思脑调用失败·按自己判断继续"}
        import re
        m = re.search(r"\{.*\}", r["out"], re.DOTALL)
        if m:
            parsed = _json.loads(m.group(0))
            if pivots:
                parsed["_pivots_found"] = pivots
            return parsed
        return {"raw": r["out"][:300], "next_move": r["out"][:200]}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:80]}", "next_move": "反思脑异常·按自己判断继续"}


def format_reflection(refl):
    """把反思结论格式化成给agent的提示(它读了自己决定下一步)。"""
    if refl.get("error"):
        return f"[反思脑] {refl.get('next_move','')}"
    lines = ["[反思脑·像真人一样想了想]"]
    if refl.get("gap"): lines.append(f"· 离目标差: {refl['gap']}")
    if refl.get("path_working") is False and refl.get("why_stuck"):
        lines.append(f"· 当前路无效·根因: {refl['why_stuck']}")
    # G1b 前提审计(2026-08-12): 检测到自洽循环就醒目提示"重读题意"(治Bob类:错前提上反复验证)
    _pa = (refl.get("premise_audit") or "").strip()
    if _pa and _pa.lower() != "ok" and ("自洽" in _pa or "重读" in _pa or "循环" in _pa):
        lines.append(f"· ⚠️前提审计: {_pa}(别在错前提上继续验证·先重读题意/重填歧义轴)")
    _pd = (refl.get("pivot_diffusion") or "").strip()
    if _pd:
        lines.append(f"· 🔗证据图谱扩散(枢轴): {_pd}")
    if refl.get("next_move"): lines.append(f"· ★下一步: {refl['next_move']}")
    if refl.get("need_new_tool"):
        lines.append(f"· 缺的能力/怎么搞到: {refl['need_new_tool']}")
        # 2026-08-13 · capability_writer 接入：当反思脑认为需要一个算法类新工具时，
        # 主动推荐 agent 调 write_capability 自主生成，而不是等人类来写。
        _wch = (refl.get("write_capability_hint") or "").strip()
        if _wch:
            lines.append(
                f"\n【★自主造工具】反思脑认为需要新算法能力，且已提供参数描述:\n"
                f"  → 立即调用: `write_capability(problem_desc='{_wch[:200]}')`\n"
                f"  这个工具会让LLM自动生成Python代码、沙箱验证、注册为持久工具，\n"
                f"  然后调 `run_written_tool(tool_name='<生成的工具名>', inputs={{...}})` 使用它。\n"
                f"  你必须二选一: (A) 立即调用 write_capability; "
                f"(B) 若判断不该用·必须写明理由。"
            )
    # (修复2026-08-18) 原先这里有一段 `if not _rt:` 引用了下方才赋值的 _rt·
    # 导致 write_capability_hint 非空时抛 UnboundLocalError·整个反思输出丢失。已删除死代码·
    # _rt 的提取统一放到下方(它本来就在那儿)。
    if refl.get("ask_user"): lines.append(f"· (可human-in-loop问用户): {refl['ask_user']}")
    # v13.93 self_evolve · agent自己诊断+提案的"反思-执行断裂"修复(用户监督后应用):
    # 反思推荐了具体工具时·强化执行约束(保留否决权·不写死):agent必须采纳或显式给拒绝理由·
    # 不允许读了诊断却不落实为动作。recommended_tool从next_move里提取工具名(通用·非题型词表)。
    import re as _re
    # 优先读结构化next_tool字段(反思脑明确填的·agent提案原文要求)·退回从next_move正则抠(兜底)
    _rt = (refl.get("next_tool") or "").strip()
    if not _rt:
        _nm = refl.get("next_move", "") or ""
        _m = _re.search(r"(see_video_frame|solve_chess_move|see_image_expert|transcribe_audio|"
                        r"ask_local_llm|scan_environment|ask_expert_model|query_science_db|"
                        r"extract_structured|read_image_ocr|query_web_archive|query_structured_db)",
                        _nm)
        _rt = _m.group(1) if _m else ""
    if _rt:
        lines.append(
            f"\n【强制下一步】反思已诊断并推荐调用工具:`{_rt}`。"
            f"\n你必须二选一:(A) 立即按此建议调用该工具;"
            f"(B) 若认为不该采纳·须在下一步显式写明拒绝理由后再另作行动。"
            f"\n不允许忽略本建议、复述诊断却不落实为具体动作。")
    return "\n".join(lines)


if __name__ == "__main__":
    # 冒烟: 模拟GameGrumps卡住·反思脑给建议
    r = reflect(
        goal="找YouTube视频某时刻画面上的赛道·再查该赛道世界纪录",
        trace_steps=["run_parallel_search", "run_parallel_search", "extract_cards",
                     "shell_exec(yt-dlp搜)", "fetch_url_smart", "run_parallel_search"],
        stuck_reason="搜了很多文字+yt-dlp找URL·但没看到视频画面内容·绕圈",
        env_summary="有see_video_frame工具(下视频抽帧VLM看)·有VPN代理·docker沙盒")
    print(format_reflection(r))
    print("\n原始:", _json.dumps(r, ensure_ascii=False)[:400])
