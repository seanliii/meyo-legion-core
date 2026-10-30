#!/usr/bin/env python3
"""
qa_tool_impls · 工具/子Agent 的真实实现 (v58)

这个文件只做一件事：把 qa_agent_main.py 的现成 Step 函数 + 各独立
sub-agent 模块 (qa_action_cone / qa_multi_nocode / qa_clarify /
content_supervisor / deep_research_swarm ...) 逐个用 @register_tool
包一层薄封装，注册进 qa_tools_registry。

新增/改动一个 Step 或接入新 sub-agent 时，只需要在这里加一个新的
@register_tool 函数——qa_agent_main.orchestrate()（pipeline 模式）和
qa_auto_agent.run_auto_agent()（auto 模式）都从同一个 registry 取，
不会再出现「只改了一边，另一边没同步」的漂移。

统一约定:
  executor(question: str, session: dict) -> str (摘要文本，给 LLM/日志看)
  session 是调用方持有的一个普通 dict，字段基本对齐 qa_agent_main.orchestrate()
  的本地变量名 (scope/triage/cost_rec/evidence/cards/narrative/...)，
  方便同一份 session 在 pipeline 和 auto 两种模式间互相参照/调试。
"""
import os
import sys
import json
import time
import re  # v15.7 · 修 t_self_calibrate 裸用 re.search 抛 NameError(finalize前置信校准不能炸)
import concurrent.futures

HERE = os.path.dirname(os.path.abspath(__file__))
QA_ROOT = os.path.dirname(HERE)
SEED_ROOT = os.path.dirname(QA_ROOT)
V11_EVOLUTION = os.path.join(SEED_ROOT, "v11", "evolution")
if V11_EVOLUTION not in sys.path:
    sys.path.insert(0, V11_EVOLUTION)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from qa_tools_registry import register_tool

# qa_agent_main 是所有 Step 的宿主模块；这里延迟到函数体内 import 的地方
# 也直接顶层 import，因为 qa_tool_impls 本来就只在「需要用工具」时才被
# qa_tools_registry._ensure_builtin_registered() 懒加载进来。
import qa_step_library as M
# v59 · auto 模式(本文件)和 pipeline 模式(qa_agent_main.orchestrate)共享
# 同一份 mission_state——session dict 就是 auto 模式的 trace，字段对齐。
# auto 模式没有 step0.25 那种强制生成 goal_card 的节点(由 LLM 自主决策调用
# 哪些工具)，但只要写读同一个 session，仍能享受 known_facts/open_gaps 的
# 跨工具调用累积，不需要另起一套状态。
from mission_state import MissionState


def _sget(session, key, default=None):
    return session.get(key, default) if isinstance(session, dict) else default


# ═══════════════════════ 子任务委托 (delegate) 基础设施 ═══════════════════════
#
# 参照 Claude Code (CCB) 的 AgentTool / Hermes 的 delegate_task 设计:
# 「贵」的子任务(多轮 LLM + 多跳搜索，比如 run_deep_research_swarm)不应该
# 让其内部产出的全部中间态(几十条证据、多轮 hop 日志)直接怼进主 agent 的
# 对话上下文——那样几轮下来主循环的 token 预算就被子任务的"思考过程"耗尽了。
#
# 做法: 委托执行 = 隔离上下文(独立线程/独立超时预算) + 结果收敛(只回传
# Top-N 摘要，全量结果落 session 供下游工具按需读取，不经过模型"复述")。
# 这与 auto_agent 现有的"大对象存 session、只给模型摘要"的设计同源，这里
# 补的是"贵任务要有硬性时间/规模预算，超时/超规模就降级收口"这一环，
# 对应 CCB Agentic Loop 里 max_turns / token_budget 的"硬顶+优雅收尾"思路。

DELEGATE_DEFAULT_TIMEOUT_S = 150  # 子任务硬超时：超过这个时间直接收口，不能拖垮主循环
DELEGATE_SUMMARY_TOP_N = 5        # 回传给模型的摘要里，最多展示几条关键证据/结论


def run_delegated(fn, *args, timeout_s: float = DELEGATE_DEFAULT_TIMEOUT_S, **kwargs):
    """在独立线程里执行一个"贵"的子任务函数，带硬超时。

    这是委托执行的隔离层：子任务内部想怎么跑（多轮 LLM、多跳搜索）都不
    影响主 agent 的调用栈，一旦超过 timeout_s，主 agent 立刻拿到一个
    "超时降级"的结果继续往下走，而不是被动等到子任务自己认怂。

    Returns:
        (result, meta) · meta = {"ok": bool, "timed_out": bool, "elapsed_s": float, "error": str}
    """
    t0 = time.time()
    # 注意: 不用 `with ThreadPoolExecutor() as pool`——with 退出时会
    # shutdown(wait=True)，等价于又把超时熬没了。这里手动管理生命周期，
    # 超时后立刻把控制权交还给主循环；子线程留给解释器在后台自然收尾
    # (Python 线程非守护也不影响主流程继续跑，只在进程退出时才会被等待)。
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(fn, *args, **kwargs)
    try:
        result = future.result(timeout=timeout_s)
        pool.shutdown(wait=False)
        return result, {"ok": True, "timed_out": False,
                         "elapsed_s": round(time.time() - t0, 1), "error": ""}
    except concurrent.futures.TimeoutError:
        pool.shutdown(wait=False)
        return None, {"ok": False, "timed_out": True,
                       "elapsed_s": round(time.time() - t0, 1),
                       "error": f"子任务超过 {timeout_s}s 硬预算，已收口降级"}
    except Exception as e:
        pool.shutdown(wait=False)
        return None, {"ok": False, "timed_out": False,
                       "elapsed_s": round(time.time() - t0, 1),
                       "error": f"{type(e).__name__}: {str(e)[:150]}"}


# ═══════════════════════ scope 类 ═══════════════════════

def build_triage_context(scope, triage):
    """纯函数(无副作用·可单测/自检): 把 scope+triage 拼成给 agent 的上下文提示。
    抽出来是为了 mechanism_selftest 能用【合成信号】直接走这条真实注入代码·
    10ms 验证'axes/constraints 在透传链上没丢'·不用跑 LLM/不用跑整道题。
    (根因: 之前注入逻辑内联在 tool 里·没法单测·接线断了只能跑完整题才发现)"""
    scope = scope or {}
    triage = triage or {}
    base = (f"target_scope={scope.get('target_scope','')[:50]} · "
            f"task_level={triage.get('task_level')} · "
            f"search_route={triage.get('search_route')}" +
            (f" · direct_answer可用: {triage['direct_answer'][:60]}"
             if triage.get("direct_answer") else ""))
    # 模态需求(LLM 自判·非词表·选工具权仍在 agent)
    _mods = triage.get("modality_needs") or ["text"]
    if _mods and _mods != ["text"]:
        _hint = {"vision": "看画面(see_video_frame看视频帧/see_image_expert换模型看图)",
                 "audio": "听音频内容(transcribe_audio转录)",
                 "compute": "精确计算(写代码/装库算·别估)"}
        _tips = "·".join(_hint.get(m, "") for m in _mods if m in _hint)
        if _tips:
            base += f"\n[你自己判的模态需求={_mods}] 这题本质可能需要{_tips}——搜文字搜不到这类内容·你自己决定用不用对应工具。"
    # 口径约束(算前核对·防分母/时间窗错)
    _cons = triage.get("constraints") or []
    if _cons:
        base += f"\n[口径约束={_cons}] ⚠️算数据前先核对: 你用的数据范围(分母/时间窗/过滤)匹配这些约束吗?不匹配就重新过滤·别用默认全量直接算。"
    # G1 语义歧义轴(根因2·2026-08-12): axes 兼容两种形态——
    #   ①拍平字符串列表 ["perspective:maximin而非期望值",...](小模型输出更稳·首选)
    #   ②嵌套 dict {"perspective":"maximin?",...}(旧 schema·仍兼容)
    _axes = triage.get("ambiguity_axes")
    _axis_lines = []
    _label = {"entity": "实体消歧(到底哪个游戏/人/年份·选错全白算)",
              "time": "时间/区间(累计vs单期)", "set": "集合(交/并/补·De Morgan方向)",
              "version": "数据版本/哪张表", "perspective": "视角(谁的max/min)",
              "aggregate": "多片段整合(排序/去重/合并)", "nominal": "标注者简化口径"}
    if isinstance(_axes, dict):
        for k, v in _axes.items():
            if isinstance(v, str) and v.strip():
                _axis_lines.append(f"{_label.get(k, k)}={v.strip()}")
    elif isinstance(_axes, list):
        for item in _axes:
            s = str(item).strip()
            if not s:
                continue
            # "perspective:xxx" → 换成中文标签;无冒号则原样
            if ":" in s:
                k, v = s.split(":", 1)
                _axis_lines.append(f"{_label.get(k.strip(), k.strip())}={v.strip()}")
            else:
                _axis_lines.append(s)
    if _axis_lines:
        base += f"\n[⚠️语义歧义轴·求解/交卷前逐条核对别读偏] {'·'.join(_axis_lines)}"
    # 2026-08-14 · 实体消歧(通用·非背答案): triage 标了不确定的专有名词 → 先查"它到底是什么"
    #   再解题(别硬猜)。治"遇陌生实体当普通词搜偏"(如 CFM=视频补帧软件·Cheater Beater=某产品)。
    _unk = triage.get("unknown_entities") or []
    _unk = [str(x).strip() for x in _unk if str(x).strip()]
    if _unk:
        base += (f"\n[🔍先查这些陌生实体是什么={_unk}] 这些是你不确定含义的专有名词/术语。"
                 f"别当普通关键词直接搜——先用 search/ask_expert_model 查清'它到底是什么(哪个领域/"
                 f"什么产品/标准指代)'·搞懂再解题。查错实体=全白算。")
    # 2026-08-14 · 题型归类(通用·非背答案): triage 归类了这题像哪类经典问题 → 先查该类【标准解法】
    #   再动手·别从零硬凑(治 Bob=阶梯Nim/多边形=shapely 这类"有标准解法却硬猜")。只给"查标准解法"
    #   的通用引导·不给本题具体答案——换任何同类题都通用。
    _arch = str(triage.get("problem_archetype") or "").strip()
    if _arch:
        base += (f"\n[📐题型归类={_arch}·先查标准解法] 这题可能属于有成熟标准解法的经典问题类。"
                 f"动手前先查'这类问题({_arch})的标准解法/公式/现成库是什么'(search/search_github/"
                 f"ask_expert_model)·套标准解法而非从零硬凑。别背具体答案·是找这类题的通用解法。")
    return base


def _v112_intent_completion(question):
    """v112(2026-08-16 · 用户方向:意图补全) 题面→意图规格·确定性抽取(零LLM)。
    用户提问永远是残缺规格——隐含的时间锚/否定结构/单位精度/分母/最值方向
    不补全·下游就在错误的口径上跑完全程(v110 只在交卷时审·这里开局就立契约)。
    返回 (规格文本, spec_dict)。fail-safe:任何异常返回 ("", {})。"""
    try:
        import re as _re
        q = question or ""
        spec = {}
        # ① 时间锚: 年份/日期/as of/截至/since —— 数据该取哪个时点的快照?
        m = _re.search(r"\b(1[89]\d{2}|20\d{2})\b", q)
        if m:
            spec["time_anchor"] = f"题面年份锚={m.group(1)} → 数据要取【该时点】的值·不是当前值"
        if _re.search(r"\bas of\b|截至|as at\b|by (?:the end of\s+)?(?:1[89]\d{2}|20\d{2})", q, _re.I):
            spec.setdefault("time_anchor", "")
            spec["time_anchor"] = "题面有 as of/截至 锚 → 明确截止时点·找同期快照对账"
        # ② 数据源锚: according to X / on X website / 在X上 —— 唯一权威口径是谁?
        m = _re.search(r"(?:according to|on the|from the|on|in)\s+([A-Z][\w.-]*(?:\s+[A-Z][\w.-]*){0,3})", q)
        if m and any(k in m.group(0).lower() for k in ("according", "on the", "from the", "website", "site", "wikipedia", "site")):
            spec["source_anchor"] = f"指定信息源≈{m.group(1)[:40]} → 以该源【原样口径】为准·不做自行换算"
        # ③ 否定+or/and: 否定辖域多解 → 四组合并排算
        if _re.search(r"\b(?:not|except|other than|neither|nor|without|excluding)\b[^.?!]{0,60}?\b(?:or|and)\b", q, _re.I):
            spec["negation_scope"] = "题面含【否定+or/and】→ 否定辖域多解·各组合并排算完再选主答"
        # ④ 单位/精度: percent/小数位/round → 输出口径
        if _re.search(r"\bpercent(?:age)?\b|%", q, _re.I):
            spec["unit"] = "题面涉及百分比 → 明确是百分数还是小数·分子分母各是什么"
        m = _re.search(r"(?:round(?:ed)? to|精确到|小数点后)\s*(?:the\s*)?(?:nearest\s*)?([\w-]+\s)?(?:five|five\s+decimal|decimal|三位|3)\s*(?:places|位)?", q, _re.I)
        if m:
            spec["precision"] = f"题面有舍入要求({m.group(0)[:40].strip()}) → 按要求位数四舍五入"
        # ⑤ 分母/平均: average/ratio/proportion → 分母总体是什么
        if _re.search(r"\b(?:average|mean|ratio|proportion|percentage of)\b|平均|比例", q, _re.I):
            spec["denominator"] = "题面涉及平均/比例 → 先写清分子=什么·分母=哪个总体·含不含边界成员"
        # ⑥ 最值方向: highest/lowest/most/first → 方向别读反
        m = _re.search(r"\b(highest|lowest|most|least|first|last|largest|smallest|earliest|latest)\b", q, _re.I)
        if m:
            spec["extremum_dir"] = f"题面问的是【{m.group(1).lower()}】→ 方向锁死·列证据时按这个方向排序核对"
        # ⑦ 量词: each/at least/exactly → 计数口径
        if _re.search(r"\b(?:each|at least|at most|exactly|no more than|no fewer than)\b", q, _re.I):
            spec["quantifier"] = "题面含量词约束(each/at least/exactly…) → 逐项检查满足性·别只看总数"
        if not spec:
            return "", {}
        lines = ["[意图补全·开局契约] 从题面抽出的隐含约束轴——每一条都是评分点·开工前先逐条确认你的解法覆盖:"]
        for k, v in spec.items():
            lines.append(f"  · [{k}] {v}")
        lines.append("  下游每一步拿这个规格对照;交卷前 review_intent 逐条打勾。规格若与你对题意的理解冲突·以题面原文语法为准并在答案中说明。")
        return "\n".join(lines), spec
    except Exception:
        return "", {}


@register_tool(
    name="judge_scope_and_triage",
    description=(
        "第一步必调。判断用户问题的真实范围(target_scope，别扩大/别过度设计)，"
        "并做意图分诊(task_level=琐事/标准/重型，search_route=是否需要搜索及搜索强度)。"
        "会同时做【意图补全】:从题面抽出隐含约束轴(时间锚/否定辖域/单位精度/分母/"
        "最值方向/量词)作为开局契约。如果 task_level=琐事 且给出了 direct_answer，"
        "可以直接用 finalize_answer 把 direct_answer 交付，不必再走搜索/写作全流程。"
    ),
    category="scope", cost_tier="cheap",
)
def t_judge_scope_and_triage(question, session):
    t0 = time.time()
    # run_auto_agent already computes triage before the loop. Re-running two LLM calls here wastes
    # a whole turn (~17s observed) under a short wall. Reuse the existing triage/scope when the
    # remaining wall is tight and they are already present; otherwise recompute as before.
    # run_auto_agent already produced an authoritative triage before the loop. Recomputing an
    # identical triage costs a full model turn (~17-20s) for no new information, so reuse it
    # whenever it already exists. Only recompute when triage is genuinely missing.
    if session.get("triage"):
        triage = session["triage"]
        scope = session.get("scope") or {
            "target_scope": str(triage.get("target_scope") or question)[:500],
            "ban_expand_rules": [], "user_scale": "个人",
            "expected_length": "500-1500",
        }
        session["scope"] = scope
        _reused = build_triage_context(scope, triage)
        return (_reused + "\n[triage 复用·短墙钟省两次 LLM]").strip()
    scope = M.step0_scope_judge(question)
    triage = M.step0_triage(question)
    # v105(2026-08-15 · gaia9/10 实证·量词门静默真根因): agent 调本工具时常传
    # 【自己缩写/改写的 question】(实测 e142 被 agent 改写成 398 字符版·约束措辞
    # "one box must contain at least 2" 被摘要掉)——原样覆盖 session["question"]
    # 会把 run_auto_agent 存入的权威完整题面换掉·所有基于题面的门(量词二义性/
    # 题干契约缺口/假设审计上下文/经验召回)全在残缺文本上失配。修:只在 session
    # 题面为空时回填(兜底)·绝不覆盖已有权威题面。
    if not (session.get("question") or "").strip():
        session["question"] = question
    session["scope"] = scope
    session["triage"] = triage
    # v112 意图补全: 用【权威题面】(session优先·agent改写前)抽约束轴·存 session
    # 供下游各门对照·并把规格文本回显给 agent(开局契约)。
    _spec_txt, _spec = _v112_intent_completion(session.get("question") or question)
    if _spec:
        session["intent_spec"] = _spec
    # v59 · 兜底初始化 mission_state(auto 模式没有强制的 goal_card 生成节点·
    # 但保证 session["mission_state"] 容器一定存在，下游 run_deep_research_swarm /
    # run_factual_extract 等工具才能安全读写同一份状态，不会各自静默新建一份)。
    MissionState.init(session, question=question)
    _base = build_triage_context(scope, triage)
    return (_base + "\n\n" + _spec_txt).strip() if _spec_txt else _base


@register_tool(
    name="check_tool_cost_first",
    description=(
        "可选。仅当问题明确涉及「工具选型/多少钱/免费还是付费/买哪个」时调用，"
        "判断值不值得引入工具、该推荐哪个工具(一次性给结论，不铺陈候选列表)。"
        "纯方法论/概念类问题不需要调这个。"
    ),
    category="optional", cost_tier="cheap",
)
def t_check_tool_cost_first(question, session):
    cost_rec = M.step1_cost_first(question, _sget(session, "scope", {}))
    session["cost_rec"] = cost_rec
    return f"recommend={cost_rec.get('recommend','')[:60]}"


# ═══════════════════════ 搜索/证据类 ═══════════════════════

def _query_variants(q: str, max_extra: int = 3) -> list:
    """v166 · 多角度查询变体(文档《Agent彻底攻坚》Q2 Ethernity 真检索盲区:
    不是搜不到·是没搜对——字面化单一查询 misses 语义面)。确定性规则生成·
    零 LLM 成本: ①大写实体聚焦 ②年份/数字约束 ③引号短语。返回额外变体列表。"""
    try:
        import re as _re_v
        variants = []
        # ① 实体聚焦: 连续大写词(人名/机构/专名)——冷门 ID/人名题的关键锚点
        ents = _re_v.findall(r"\b[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,3}\b", q)
        if ents:
            _e = " ".join(ents[:6])
            if _e.strip() and _e.strip().lower() != q.strip().lower():
                variants.append(f"entity focus: {_e}")
        # ② 时间/数字约束: 年份+数字是 browsecomp 题的强区分器
        nums = _re_v.findall(r"\b(?:19|20)\d{2}\b|\b\d+(?:\.\d+)?\b", q)
        if nums and ents:
            _n = f"{' '.join(ents[:3])} {' '.join(dict.fromkeys(nums))}"
            if _n.strip() and _n.strip().lower() != q.strip().lower():
                variants.append(f"timeline: {_n}")
        # ③ 引号短语精确锚定
        quoted = _re_v.findall(r'"([^"]{3,60})"', q)
        if quoted:
            variants.append(f'exact phrase: "{quoted[0]}"')
        # 去重·限量(每变体多一次检索·上限防拖慢)
        _seen = {q.strip().lower()}
        out = []
        for v in variants:
            if v.strip().lower() not in _seen:
                out.append(v)
                _seen.add(v.strip().lower())
        return out[:max_extra]
    except Exception:
        return []


def _sanitize_query(q):
    """清洗 LLM 生成的脏 query(真e2e Russell题实证细缝1:query混进'年获得数学博士学位'
    /markdown ```/整句题面残留)。去 markdown 符+首尾中文说明句+压空白。通用·fail-safe。"""
    try:
        import re as _re
        s = str(q or "")
        s = s.replace("```", " ").replace("`", " ")          # 去 markdown 代码块符
        s = _re.sub(r"^[\s#>*\-·、。，,.]+", "", s)            # 去开头标点/列表符
        # 去开头孤立的中文说明碎片(如"年获得数学博士学位 XXX"·"的 XXX")——真query该以
        #   实体/英文关键词起头·开头是无主语中文短句(≤12字+空格+后续)多是LLM残留
        s = _re.sub(r"^[的了在与和及]?[一-鿿]{1,12}\s+(?=[A-Z0-9\"'])", "", s)
        s = _re.sub(r"\s+", " ", s).strip()
        return s or str(q or "")
    except Exception:
        return str(q or "")


def _query_is_precise_anchor(q):
    """判 query 是否【已是精确锚点】(上游 first_anchor / 模型已给好 query)→ 不必再变体扩展。
    判据(通用启发·非题目特判·本质区别 = 已锚定实体 vs 纯泛描述):
      ① 含引号短语("..." / “...”)——作者/上游标记的独特词组·已足够定向。
      ② 含大写专有名词序列(≥2 词的专名·如 "Matt Ox"/"FC Krasnodar")——query 已锁到
         具体实体·是【定向属性搜】而非泛描述撒网·再变体扩展只会稀释(实证 Minh 题
         'Matt Ox rapper born 2005 real name' 12-13词含专名·却因 >8词没跳过→烧378s)。
      ③ 精炼短 query(≤8 词·非整句)——已是关键词拼接·不是整段描述。
    只有【纯泛描述整句】(无实体锚点、词多、带句末标点)才保留变体扩展(治检索盲区)。
    fail-safe → False(异常时保守·走原变体扩展路径)。"""
    try:
        import re as _re_p
        s = str(q or "").strip()
        if not s:
            return False
        # ① 引号短语 = 强独特信号
        if _re_p.search(r'["“”][^"“”]{3,}["“”]', s):
            return True
        # ② 大写专有名词序列(≥2 连续首字母大写词)= 已锚定具体实体 → 精确定向搜
        #    (单个大写词如句首 The/What 区分度低·不算;≥2 词的专名如 Matt Ox 才算)
        if _re_p.search(r"\b[A-Z][a-zA-Z.]+\s+[A-Z][a-zA-Z.]+", s):
            return True
        # ③ 精炼短 query: 词数少 + 无整句特征(句末标点/多个句子)
        _words = s.split()
        _sentence_like = bool(_re_p.search(r"[.?!。？！][^.?!。？！]", s)) or s.count(",") >= 2
        if len(_words) <= 8 and not _sentence_like:
            return True
        return False
    except Exception:
        return False


def _publish_search_hits(session, hits=None, source_dim="", published_urls=None, *,
                         tool="run_parallel_search"):
    """Publish one completed search source to every evidence consumer immediately."""
    # Backward-compatible callback form: step2_parallel_search may invoke the callback
    # as ``on_hits(hits, source_dim)`` or pass only ``hits`` through older adapters.
    if not isinstance(session, dict):
        hits, session = session, None
    if session is None:
        return []
    hits = hits or []
    published_urls = published_urls if published_urls is not None else {
        item.get("url") for item in session.get("evidence", [])
        if isinstance(item, dict) and item.get("url")}
    evidence = session.setdefault("evidence", [])
    records = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        url = hit.get("url")
        if not url or url in published_urls:
            continue
        record = dict(hit)
        record.setdefault("source_dim", source_dim)
        evidence.append(record)
        records.append(record)
        published_urls.add(url)
    if not records:
        return []

    # The outer engine may time out before the whole parallel search returns. Persist each
    # completed source now so canonical replay and the relation graph retain real results.
    try:
        if session.get("_event_log_path"):
            import qa_auto_agent as _qa_incremental
            run_state = _qa_incremental._canonical_run_state(session)
            observed_ids = []
            for record in records:
                content = "\n".join(str(record.get(key) or "") for key in
                                    ("title", "content", "snippet", "url"))[:6000]
                if not content.strip():
                    continue
                observed = run_state.observe_evidence(
                    source=str(record.get("url") or record.get("source") or source_dim or tool),
                    content=content, title=str(record.get("title") or "")[:500], tool=tool,
                    extra={"source_dim": str(source_dim or "")[:80]})
                observed_ids.append(observed.payload["evidence_id"])
            if observed_ids:
                prior = list(session.get("_last_observation_evidence_ids") or [])
                session["_last_observation_evidence_ids"] = list(
                    dict.fromkeys(prior + observed_ids))[-100:]
                projected = run_state.project_legacy_views(session)
                session["_event_cursor"] = projected.last_seq
    except Exception as exc:
        session.setdefault("nonfatal_failures", []).append({
            "stage": "incremental_search_canonical_publish", "error": str(exc)[:200]})

    try:
        import evidence_graph as _eg_incremental
        if _eg_incremental._enabled():
            for record in records:
                content = "\n".join(str(record.get(key) or "") for key in
                                    ("title", "content", "snippet"))[:6000]
                if content.strip():
                    _eg_incremental.observe_triplets(
                        session, content,
                        source=str(record.get("url") or record.get("source") or source_dim or tool),
                        step=int(session.get("_search_publish_step", 0) or 0))
            session["_search_publish_step"] = int(session.get("_search_publish_step", 0) or 0) + 1
    except Exception as exc:
        session.setdefault("nonfatal_failures", []).append({
            "stage": "incremental_search_graph_publish", "error": str(exc)[:200]})
    # Incremental observations must survive a parent hard-kill before the outer tool returns.
    try:
        if session.get("qid"):
            import qa_auto_agent as _qa_checkpoint
            _qa_checkpoint._save_ckpt(str(session["qid"]), status="running")
    except Exception as exc:
        session.setdefault("nonfatal_failures", []).append({
            "stage": "incremental_search_checkpoint", "error": str(exc)[:200]})
    return records


def _thin_direct_search(query, session, published_urls):
    """Execute exactly one model-authored query and return raw search hits.

    Returns None only when the direct provider cannot initialize/execute, allowing the
    legacy planner path to remain a fail-open compatibility fallback. An empty list is a
    successful search with zero hits and must NOT trigger the planner (that would silently
    replace the model's query with framework-authored queries).
    """
    try:
        from search_fabric_verticals import auto_register_all
        auto_register_all()
        from search_fabric import SearchFabric
        fabric = SearchFabric(fanout=3, fallback_to_anysearch=True)
        forced = ["tavily"] if os.environ.get("MEYO_FORCE_TAVILY", "") == "1" else None

        # Hard per-search wall: a query with no results otherwise serializes the fanout wave
        # + anysearch fallback retries into ~60-70s (observed), starving the model of turns.
        # Bound it so control returns fast with whatever completed; the model then decides to
        # rephrase/pivot/finalize. Generic latency guard, not a domain heuristic.
        try:
            wall = float(os.environ.get("MEYO_THIN_SEARCH_WALL_S", "35") or 35)
        except Exception:
            wall = 35.0
        import concurrent.futures as _cf
        _ex = _cf.ThreadPoolExecutor(max_workers=1)
        fut = _ex.submit(lambda: fabric.search(query, top_k=8, mode="fast",
                                               force_sources=forced) or [])
        try:
            hits = fut.result(timeout=wall)
        except _cf.TimeoutError:
            session.setdefault("nonfatal_failures", []).append({
                "stage": "thin_direct_search", "error": f"search_wall_{wall:.0f}s"})
            fut.cancel()
            _ex.shutdown(wait=False, cancel_futures=True)
            return []          # bounded: a slow search is an empty observation, not None
        finally:
            if fut.done():
                _ex.shutdown(wait=False, cancel_futures=True)
        return [h for h in hits if isinstance(h, dict)]
    except Exception as exc:
        session.setdefault("nonfatal_failures", []).append({
            "stage": "thin_direct_search", "error": str(exc)[:200]})
        return None


@register_tool(
    name="run_parallel_search",
    description=(
        "核心步骤。多维度并行检索 + 证据深读，是后续 extract_cards / write_narrative "
        "的证据来源。会自动按题目规划查询并多源并发搜索，无需手动指定检索维度。"
        "标准/重型问题必调；search_route=no_search 的琐事题可跳过。\n\n"
        "⚠️ 反同义改写死循环警告: 如果连续 2-3 次调用只是换了关键词表述但仍是"
        "0 条新证据/找不到目标页面，说明问题不是'关键词不够好'，而是目标内容"
        "本身是通用搜索引擎覆盖不到的类型——继续换关键词是在浪费预算重复撞同"
        "一堵墙。此时应立即切换工具而不是原地重试:\n"
        "  • 目标是历史已下线网页/小众博客特定日期的帖子 → 改用 query_web_archive\n"
        "  • 目标是化学/人物属性/医学等结构化事实 → 改用 query_structured_db\n"
        "  • 确认这些都不适用、真的是资源暂时不可用 → 改用 extract_cards 靠"
        "已有证据(哪怕是空的)+ 通识继续往下走，诚实告知证据不足。"
    ),
    category="search", cost_tier="expensive", retryable=True, max_retry_hint=2,
)
def t_run_parallel_search(question, session):
    question = _sanitize_query(question)   # 细缝1:发搜索前清洗脏query
    # Strip triage-template syntax the model sometimes copies into the query
    # (e.g. "RDF 时间窗=early 2000s ..." → "RDF early 2000s ..."). These "key=val"
    # artifacts come from the triage constraint examples and never match real pages.
    import re as _re_q
    question = _re_q.sub(r"[一-鿿]{0,6}=", " ", question)
    question = _re_q.sub(r"\s+", " ", question).strip()
    # Cross-turn dedup: if this exact normalized query was already searched this session,
    # do not re-run it — 6× identical searches (observed) waste the budget and stall the
    # model on a dead hypothesis. Tell it to pivot instead. (Same-turn dedup lives in the
    # thin loop; this catches across-turn repeats.)
    _norm_q = " ".join(_re_q.findall(r"[a-z0-9一-鿿]+", question.lower()))
    _seen_q = session.setdefault("_searched_queries", {})
    _context_mode = os.environ.get("MEYO_SEARCH_CONTEXT", "1") == "1"
    _is_repeat = bool(_norm_q and _seen_q.get(_norm_q, 0) >= 1)
    if _is_repeat and not _context_mode:
        # A臂(legacy control):硬拦重复query。保留供AB对照。
        _seen_q[_norm_q] += 1
        return ("[重复检索已拦] 这条 query 本轮已搜过且无新证据。别再搜同一句——换一个"
                "决定性不同的角度:①用结果里出现的确切标题/专有名词/引号短语做锚点 "
                "②或换一个竞争假设 ③或直接基于已有证据 finalize。")
    # B臂(default·学DeepSeek guard):只提醒不否决。重复query仍执行,完整搜索路径/结果稍后
    # 作为context返回给模型,由模型自己判断重试/换向/收尾。
    if _norm_q:
        _seen_q[_norm_q] = _seen_q.get(_norm_q, 0) + 1
    evidence = session.setdefault("evidence", [])
    initial_count = len(evidence)
    published_urls = {e.get("url") for e in evidence if isinstance(e, dict) and e.get("url")}

    # RC5: thin mode runs the model's LITERAL query directly through the search fabric —
    # no _plan_queries fan-out, no forum lane, no variant expansion. The audit showed the
    # planner rewriting one query into several (often template-polluted) queries that timed
    # out at 90s each while competitors issued one clean query to the same fabric and got
    # hits. The model owns the query; the tool just executes it. Fail-open: any error falls
    # back to the legacy planned path below.
    if bool(session.get("_thin_mode")) or os.environ.get("MEYO_NATIVE_THIN_LOOP", "") == "1":
        _direct = _thin_direct_search(question, session, published_urls)
        if _direct is not None:
            _publish_search_hits(session, _direct, "model_query", published_urls)
            added_count = len(evidence) - initial_count
            # journey/全工具路径 context 现由统一 chokepoint native_agent_loop._run_tool 记录
            # (所有工具所有路径都经它·单一事实源),不再在此分层补(之前接这层被 _tool_fn 绕过)。
            return f"新增 {added_count} 条证据，累计 {len(evidence)} 条"

    scope = _sget(session, "scope") or M.step0_scope_judge(question)

    # Bind callback state as defaults: delegated executors may inspect/copy the callback
    # and invoke it with only the completed hit batch.
    def _publish_hits(hits, source_dim="", _session=session, _published=published_urls):
        _publish_search_hits(_session, hits, source_dim, _published)

    # A bounded caller (e.g. short-wall preflight) can cap total search wall so worker threads
    # cannot keep streaming/searching after the caller's deadline. Candidate-first strategies
    # publish snippets fast and defer deep-read to targeted candidate verification.
    _search_total = _sget(session, "_search_total_timeout_s")
    _route = (_sget(session, "_strategy_route") or {}).get("route")
    _snippets_only = _route == "inverted_multi_constraint_entity"
    _kwargs = {"on_hits": _publish_hits}
    if _search_total:
        _kwargs["total_timeout"] = float(_search_total)
    if _snippets_only:
        _kwargs["snippets_only"] = True
    try:
        new_evidence = M.step2_parallel_search(question, scope, **_kwargs)
    except TypeError:
        # Older/mocked step2 signatures may not accept the new keywords.
        new_evidence = M.step2_parallel_search(question, scope, on_hits=_publish_hits)
    # v166 · 多角度变体扩展(文档4·Q2检索盲区): 主查询之外·用实体/时间/
    #   精确短语锚点各再搜一轮·聚合同一 evidence 池(去重)。
    # ★2026-08-20(搜索绑定 bug 修复后实测): 变体扩展对【已精确的锚点 query】反成拖累——
    #   每个变体触发一整轮 step2(4并发+light预筛+5并发深读)·单次搜索从 6s 拖到 60s+·
    #   且正则变体质量差(把 'August' 当专名拼 'entity focus: August Israel' 撒网稀释证据池)。
    #   符合最高指导思想「撒网有害·增加搜索次数≠推理质量」:精确锚点已定向·再变体=撒网。
    #   修: query 已是精确锚点(含引号短语/精炼短 query)→ 跳过变体扩展(省墙钟·不稀释)。
    #   泛描述/整句 query(真检索盲区那类)→ 保留变体扩展(原行为不动)。env 可关。
    _added_by_variant = 0
    _skip_variants = (os.environ.get("MEYO_SEARCH_VARIANT_EXPAND", "1") == "0"
                      or _query_is_precise_anchor(question))
    if not _skip_variants:
        for _vq in _query_variants(question):
            try:
                _ve = M.step2_parallel_search(_vq, scope, on_hits=_publish_hits)
                if _ve:
                    new_evidence.extend(_ve)
                    _added_by_variant += len(_ve)
            except Exception:
                pass
    added = []
    for item in new_evidence:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not url or url in published_urls:
            continue
        evidence.append(item)
        published_urls.add(url)
        added.append(item)
    added_count = len(evidence) - initial_count
    _vt_note = f" · 多角度变体命中 {min(_added_by_variant, added_count)} 条" if _added_by_variant else ""
    # journey/全工具路径 context 由统一 chokepoint native_agent_loop._run_tool 记录(单一事实源)。
    return f"新增 {added_count} 条证据，累计 {len(evidence)} 条{_vt_note}"


@register_tool(
    name="run_deep_research_swarm",
    description=(
        "可选、较贵(多轮 LLM + 多跳搜索)。五角色蜂群(Planner规划/Scout搜索/"
        "Evidence证据/Skeptic质疑/Judge裁决)协作做多跳深度研究，能发现一手证据、"
        "反证、未解决问题。适合重型/有争议/需要立体验证的问题。"
        "建议先跑过 run_parallel_search 拿到种子证据再调用这个，省重复检索。"
        "简单/标准问题不需要，直接跳过更省时间和成本。"
        "内部以委托子任务方式执行(独立上下文+硬超时预算)，超时会自动降级收口，"
        "不会拖垮整体任务；只允许调用一次，不要重复调用。"
    ),
    category="deep_research", cost_tier="expensive", retryable=False,
)
def t_run_deep_research_swarm(question, session):
    """委托执行: 五角色蜂群本身会产出大量中间态(每跳的 scout/evidence/skeptic
    命中、hop 日志)，如果全部经过模型"过目"，几轮下来主 agent 的上下文就被
    子任务的思考过程占满了。这里参照 CCB AgentTool / Hermes delegate_task
    的做法——蜂群在隔离的线程+超时预算里跑完，全量结果落 session（下游
    extract_cards 等工具仍可完整读取），但回给 LLM 的只是一句收敛过的
    摘要(Top-N 关键结论 + 数量统计)，不会把几十条证据/多跳日志堆进对话历史。
    """
    evidence = session.setdefault("evidence", [])
    # auto 的图像/网页/视频空间遍历也会把可追溯 observation 写入同一 evidence 池。
    # 这里按来源多样性挑种子，避免普通搜索的前 30 条挤掉跨模态和站点恢复线索。
    seed_evidence = []
    seen_seed_sources = set()
    for item in evidence:
        if not isinstance(item, dict) or not (item.get("content") or item.get("url")):
            continue
        source = str(item.get("source", ""))
        if source and source not in seen_seed_sources:
            seed_evidence.append(item)
            seen_seed_sources.add(source)
        if len(seed_evidence) >= 12:
            break
    for item in evidence:
        if isinstance(item, dict) and item not in seed_evidence:
            seed_evidence.append(item)
        if len(seed_evidence) >= 30:
            break
    task_level = str(session.get("force_task_level") or session.get("task_level") or "")
    max_hops = 5 if task_level in ("重型", "heavy", "deep") else 3
    goal_card = session.get("goal_card") if isinstance(session.get("goal_card"), dict) else None

    def _invoke():
        from deep_research_swarm import run_five_agent_research
        return run_five_agent_research(
            question, llm=M._qa_gateway_audit_call, seed_evidence=seed_evidence,
            max_hops=max_hops, goal_card=goal_card)

    result, meta = run_delegated(_invoke, timeout_s=180)

    if not meta["ok"]:
        # 超时/异常都走降级路径：不让子任务的失败影响主循环继续往下走，
        # 也不把技术细节(堆栈/超时秒数以外的东西)灌给模型，只给它需要的判断依据。
        return f"深研蜂群未完成({meta['error']})，已收口·耗时{meta['elapsed_s']}s，请改用已有证据继续"

    deep_evidence = [h for h in (result or {}).get("evidence", []) if isinstance(h, dict)]
    # (2026-08-15·第三梯队#65 合约前置校验) 蜂群每条证据【当场过离散合约】再入库:
    #   合约="这条证据有来源(url/source)吗"。无来源的证据是噪声·当场滤掉·不让它流到下游
    #   extract_cards/judge 才爆炸(材料:噪声流到 _hop_judge 才发现=太晚)。滤掉数记进 session 供审计。
    def _passes_source_contract(h):
        return bool((h.get("url") or h.get("source") or h.get("link") or "").strip())
    _pre = len(deep_evidence)
    deep_evidence = [h for h in deep_evidence if _passes_source_contract(h)]
    _filtered = _pre - len(deep_evidence)
    if _filtered:
        session["_swarm_contract_filtered"] = session.get("_swarm_contract_filtered", 0) + _filtered
    existed = {e.get("url") for e in evidence if isinstance(e, dict)}
    added = [e for e in deep_evidence if e.get("url") not in existed]
    evidence.extend(added)  # 全量证据落 session，供 extract_cards 等下游工具完整消费
    session["deep_research_result"] = result

    conclusions = (result or {}).get("conclusions", []) or []
    unresolved = (result or {}).get("unresolved_questions", []) or []
    # v59 · 回写 mission_state(与 pipeline 模式 step2.7 同一套逻辑)：conclusions
    # → known_facts，unresolved_questions → open_gaps。auto 模式下 LLM 自主决定
    # 后续调哪个工具，如果不把这些结论/缺口沉淀到 session["mission_state"]，
    # 后面即使调用 run_factual_extract 也只能重新翻一遍原始证据，看不到蜂群
    # 已经"判决过"的结论——这正是本次要治的"机械执行·互不感知"根因。
    _ms = MissionState(session)
    for _c in conclusions:
        if isinstance(_c, dict) and str(_c.get("verdict", "")).lower() not in ("false", "反驳", "refuted"):
            _claim = _c.get("claim", "")
            # 只把被实际来源支撑的结论提升为共享事实；partial/unresolved 仍留在
            # 蜂群完整结果内，避免未证实推断污染后续 auto 检索与最终回答。
            _sources = _c.get("source_urls") or []
            if _claim and _sources:
                _ms.add_fact(_claim, source="run_deep_research_swarm")
    for _uq in unresolved:
        _ms.add_gap(_uq, raised_by="run_deep_research_swarm")
    # 回传给模型的摘要做 Top-N 收敛：只带前几条最关键的结论标题，而不是
    # 全部 conclusions/证据原文——这是"结果收敛"的核心，避免蜂群的详细
    # 产出污染主 agent 的对话上下文。
    top_claims = [c.get("claim", "")[:40] for c in conclusions[:DELEGATE_SUMMARY_TOP_N] if isinstance(c, dict)]
    claims_preview = "；".join(top_claims) if top_claims else "(无)"
    return (f"新增一手证据 {len(added)} 条 · 结论 {len(conclusions)} 条(前排: {claims_preview}) · "
            f"未解决问题 {len(unresolved)} 个 · 跳数 hop={result.get('hop_count', 1)} · "
            f"耗时{meta['elapsed_s']}s")


# ═══ 《Agent搜索链路彻底修复_6案例攻坚》+《终极进化指南》落地(v166) ═══
# 文档核心: ①curl globbing(pubchem 案例: URL 带 {} 被 curl 当模式匹配字符)
# ②结构化错误分类(不是"重试"是"降级链"·LLM 换策略仍命中同一故障→工具级
# Retry Budget+错误分类让降级建立在确定性逻辑上) ③FailureMemory(域名→
# 最有效武器持久记忆·ERL 启发式·防重复踩坑) ④断路器状态机(3次败→OPEN
# 900s→HALF_OPEN 试探·防级联失败) ⑤替代源链(死链/阻断域名自动走
# jina/textise/移动版/存档·不只在搜索词层面绕)

def sanitize_url_for_curl(url: str) -> str:
    """修复 curl globbing: URL 中的 {} 被 curl 当模式匹配字符(pubchem 案例:
    'nested brace in URL position 155')。方案A(文档推荐): percent-encode。"""
    try:
        if "{" in url or "}" in url:
            # 已 encode 过的不动·只 encode 裸花括号
            url = url.replace("{%7B", "{").replace("%7D}", "}")  # 防双重
            url = url.replace("{", "%7B").replace("}", "%7D")
        return url
    except Exception:
        return url


def classify_fetch_failure(text: str) -> str:
    """结构化错误分类(文档四·ResilientCrawler._classify_error)。返回:
    cloudflare_js / baidu_captcha / cdp_empty / timeout / rate_limit /
    empty_response / unknown。让"换武器"建立在确定性分类上而非 LLM 概率推理。"""
    msg = (text or "").lower()
    if ("baidu" in msg or "wenku" in msg) and ("captcha" in msg or "verify" in msg):
        return "baidu_captcha"
    if "captcha" in msg or "cf-challenge" in msg or "just a moment" in msg or "turnstile" in msg:
        return "cloudflare_js"
    if "cdp_empty" in msg or "len=0" in msg or ("empty" in msg and len(msg) < 400):
        return "cdp_empty"
    if "timed out" in msg or "timeout" in msg or "connection timed out" in msg:
        return "timeout"
    if "rate_limit" in msg or "429" in msg or "1015" in msg or "too many requests" in msg:
        return "rate_limit"
    if "103" in msg or "empty_response" in msg:
        return "empty_response"
    return "unknown"


class _FailureMemory:
    """域名→武器成败持久记忆(文档六·ERL 启发式)。文件级持久化·跨 run 存活。
    record(): 每次武器成败记一笔; get_chain(): 返回该域名推荐的武器顺序
    (历史最佳武器优先)。防"同域名重复踩坑"(文档: 你现在的降级只有换关键词
    没有换武器·每次从头踩)。全部 fail-safe·记忆系统故障不影响主链路。"""

    def __init__(self, path: str = ""):
        import os as _os
        self.path = path or _os.path.join(
            _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
            "state", "failure_memory.json")
        self.mem = {}
        self._dirty = False
        try:
            import json as _json
            if _os.path.exists(self.path):
                with open(self.path) as f:
                    self.mem = _json.load(f)
        except Exception:
            self.mem = {}

    def record(self, domain: str, weapon: str, success: bool, error: str = ""):
        try:
            import time as _t
            d = self.mem.setdefault(domain, {"attempts": [], "best_weapon": None,
                                             "success_rate": {}, "last_failure": ""})
            # attempts 上限 40 条·防文件无限膨胀
            d["attempts"].append({"w": weapon, "ok": bool(success), "e": error[:60],
                                  "ts": int(_t.time())})
            d["attempts"] = d["attempts"][-40:]
            if not success:
                d["last_failure"] = classify_fetch_failure(error)
            stats = {}
            for a in d["attempts"]:
                s = stats.setdefault(a["w"], {"ok": 0, "n": 0})
                s["n"] += 1
                if a["ok"]:
                    s["ok"] += 1
            best, best_rate = None, -1.0
            for w, s in stats.items():
                rate = s["ok"] / max(1, s["n"])
                d["success_rate"][w] = round(rate, 2)
                if rate > best_rate and s["n"] >= 2 and rate >= 0.5:
                    best_rate, best = rate, w
            d["best_weapon"] = best
            self._dirty = True
            self._flush()
        except Exception:
            pass

    def _flush(self):
        if not self._dirty:
            return
        try:
            import json as _json, os as _os
            _os.makedirs(_os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w") as f:
                _json.dump(self.mem, f, ensure_ascii=False, indent=1)
            self._dirty = False
        except Exception:
            pass

    def get_best_weapon(self, domain: str):
        return (self.mem.get(domain) or {}).get("best_weapon")

    def get_last_failure(self, domain: str):
        return (self.mem.get(domain) or {}).get("last_failure") or ""


_FM = _FailureMemory()


def fm_record(url: str, weapon: str, success: bool, error: str = ""):
    """便捷入口: 按域名记武器成败。"""
    _FM.record(_fp_domain(url) or "global", weapon, success, error)


# 断路器状态机(文档 resilient_search_2026_v2 §2.5 DomainCircuitBreaker·完整三态):
#   CLOSED  --(连续 fail>=阈值)--> OPEN
#   OPEN    --(冷却到期)--------> HALF_OPEN   (只放行【一次】试探直连)
#   HALF_OPEN --(试探成功)------> CLOSED
#   HALF_OPEN --(试探失败)------> OPEN         (立刻重开·无需再攒满阈值)
# 文档实证: 三态断路器减级联失败 83.5%。原实现是两态(OPEN 冷却到期后直接
# 当 CLOSED·允许 agent 再硬撞满 3 次才重开)——本次补齐 HALF_OPEN 探针态:
# 冷却后只给【一次】直连机会·探针失败立即重开·探针成功才恢复常态。这样死域
# 冷却后不会被连撞 3 次·真正砍掉级联失败。
# OPEN 期间 fetch_url_smart 直接跳过直连·改走替代源/存档·别硬撞(文档: 阻断
# 域名自动切档案库·不要只记"下次换 anysearch"要记"下次直接走 Wayback+缓存")。
# 兼容: circuit_is_open 语义不变(True=调用方应跳过直连走替代源)——OPEN 与
# "HALF_OPEN 探针名额已用尽" 两种都返回 True; 探针名额未用则返回 False 放一次直连。
_CIRCUIT_STATE: dict = {}  # {domain: {"fails": n, "open_until": ts, "state": str, "half_open_probes": n}}
_CIRCUIT_FAIL_THRESHOLD = 3
_CIRCUIT_COOLDOWN_S = 900.0        # 文档 900s(15min)
_CIRCUIT_HALF_OPEN_MAX_PROBES = 1  # HALF_OPEN 只放行一次试探


def _circuit_st(dom: str) -> dict:
    return _CIRCUIT_STATE.setdefault(
        dom, {"fails": 0, "open_until": 0.0, "state": "CLOSED", "half_open_probes": 0})


def circuit_record_fail(url: str, error: str = ""):
    """硬失败计数。CLOSED: 累计到阈值→OPEN 900s; HALF_OPEN: 探针失败→立刻重开。
    (文档: 3次同类失败→断路器打开; HALF_OPEN--失败-->OPEN)"""
    import time as _t
    dom = _fp_domain(url) or "global"
    # 超时类不算硬失败(网络抖动≠域名阻断)·只数反爬/空页/限流
    if classify_fetch_failure(error) in ("timeout", "unknown"):
        return
    st = _circuit_st(dom)
    if st.get("state") == "HALF_OPEN":
        # 探针失败: 立刻重开·不必再攒满阈值(HALF_OPEN --failure--> OPEN)
        st["open_until"] = _t.time() + _CIRCUIT_COOLDOWN_S
        st["state"] = "OPEN"
        st["fails"] = 0
        st["half_open_probes"] = 0
        return
    st["fails"] += 1
    if st["fails"] >= _CIRCUIT_FAIL_THRESHOLD:
        st["open_until"] = _t.time() + _CIRCUIT_COOLDOWN_S  # OPEN 15分钟
        st["state"] = "OPEN"
        st["fails"] = 0


def circuit_record_success(url: str):
    """成功→CLOSED(HALF_OPEN 探针成功也在此恢复常态·CLOSED--success-->fails 清零)。"""
    dom = _fp_domain(url) or "global"
    st = _CIRCUIT_STATE.get(dom)
    if st:
        st["fails"] = 0
        st["open_until"] = 0.0
        st["state"] = "CLOSED"
        st["half_open_probes"] = 0


def circuit_state(url: str) -> str:
    """当前断路器状态(CLOSED/OPEN/HALF_OPEN)·同时驱动 OPEN→HALF_OPEN 的到期转移。
    读即转移(惰性): 冷却到期的 OPEN 自动进 HALF_OPEN。fail-safe。"""
    import time as _t
    dom = _fp_domain(url) or "global"
    st = _CIRCUIT_STATE.get(dom)
    if not st:
        return "CLOSED"
    if st.get("state") == "OPEN":
        if st.get("open_until", 0.0) <= _t.time():
            # 冷却到期: OPEN --cooldown expired--> HALF_OPEN(重置探针名额)
            st["state"] = "HALF_OPEN"
            st["half_open_probes"] = 0
        return st["state"]
    return st.get("state", "CLOSED")


def circuit_is_open(url: str) -> bool:
    """调用方应否跳过直连(走替代源)。三态映射:
      CLOSED    → False(正常直连);
      OPEN      → True (冷却中·走替代源);
      HALF_OPEN → 放行【一次】探针直连(返回 False 并占用探针名额)·名额用尽后 True。
    fail-safe: 异常按 False(不阻断)处理。"""
    try:
        state = circuit_state(url)
        if state == "CLOSED":
            return False
        if state == "OPEN":
            return True
        # HALF_OPEN: 给一次探针名额
        dom = _fp_domain(url) or "global"
        st = _circuit_st(dom)
        if int(st.get("half_open_probes", 0)) < _CIRCUIT_HALF_OPEN_MAX_PROBES:
            st["half_open_probes"] = int(st.get("half_open_probes", 0)) + 1
            return False   # 放行这一次直连探针(结果由 record_fail/success 定夺)
        return True        # 探针名额已用尽·仍按 OPEN 处理(走替代源)
    except Exception:
        return False


def _alt_source_chain(url: str, timeout_s: int = 18) -> tuple:
    """替代源找平(文档·情报扩散 S3/S6+修复3): 直连全灭后的廉价替代链。
    顺序: r.jina.ai 文本代理 → textise 纯文本 → m.移动版。返回 (content, via)
    或 (None, "")。每次尝试都记 FailureMemory(武器=alt_*)。"""
    import requests as _rq
    candidates = [
        (f"https://r.jina.ai/{url}", "jina_reader"),
        (f"https://r.jina.ai/http://{url.split('://', 1)[-1]}", "jina_reader_http"),
    ]
    # 移动版: www.example.com → m.example.com (只对 www. 开头的有意义)
    try:
        from urllib.parse import urlparse as _up
        _host = _up(url).netloc
        if _host.startswith("www."):
            candidates.append((url.replace("//www.", "//m.", 1), "mobile_mirror"))
    except Exception:
        pass
    for _c_url, _via in candidates:
        try:
            r = _rq.get(_c_url, timeout=timeout_s, headers=fp_headers_for_url(url))
            body = r.text or ""
            if r.status_code == 200 and len(body) >= 400:
                _low = body[:2500].lower()
                if not any(m in _low for m in ("captcha", "just a moment", "access denied")):
                    fm_record(url, _via, True)
                    fp_report_success(url)
                    return body, _via
            fm_record(url, _via, False, f"http {r.status_code} len={len(body)}")
        except Exception as _e:
            fm_record(url, _via, False, str(_e))
    return None, ""


def _curl_fallback(url, timeout_s=25, min_chars=400):
    """v108(M2c) · 双网络栈互备: 主抓取栈失败后·用子进程 curl 直接重试同一 URL。

    返回抓到的文本(str)·失败返回 None。质量门: <min_chars 或内容像错误页
    (反爬/验证/无内容)→当作失败返回 None·不把垃圾存进 evidence。
    v165 · 第4章反爬落地: 单一固定 UA→身份池轮换。同一域名内指纹粘性(检测方
    看不出轮换)·失败后换下一个身份重试一次(多身份轮换)·429/403 后进域名冷却
    (频率自适应)。fail-safe: 任何异常返回 None(调用方走原失败路径)。
    """
    try:
        import subprocess as _sp_fb
        import os as _os_fb
        _env = dict(_os_fb.environ)
        _last_body = None
        url = sanitize_url_for_curl(url)  # v166 · curl globbing 修复(pubchem 案例)
        for _attempt in range(2):
            _hdrs = fp_headers_for_url(url, attempt=_attempt)
            _args = ["curl", "-sL", "--max-time", str(timeout_s)]
            for _k, _v in _hdrs.items():
                _args += ["-H", f"{_k}: {_v}"]
            _args += ["--compressed", str(url)]
            r = _sp_fb.run(_args, capture_output=True, text=True,
                           timeout=timeout_s + 8, env=_env)
            body = (r.stdout or "")
            _last_body = body
            if r.returncode == 0 and len(body) >= min_chars:
                _low = body[:3000].lower()
                _err_marks = ("<title>access denied", "recaptcha", "just a moment",
                              "attention required", "captcha", "403 forbidden",
                              "service unavailable", "too many requests")
                if not any(m in _low for m in _err_marks):
                    fm_record(url, "curl", True)
                    circuit_record_success(url)
                    return body
                # 撞反爬页→换下一个身份重试(第4章·多身份轮换)
                _err_cls = classify_fetch_failure(_low[:600])
                fm_record(url, "curl", False, _err_cls)
                fp_report_block(url)
                circuit_record_fail(url, _err_cls)
        fm_record(url, "curl", False, f"rc_fail len={len(_last_body or '')}")
        circuit_record_fail(url, "empty_response")
        if _last_body and len(_last_body) >= min_chars:
            return None  # 有内容但像错误页·不返垃圾
        return None
    except Exception as _e:
        fm_record(url, "curl", False, str(_e))
        return None


# ═══ 《情报搜寻优化·搜索设计》第4章 反爬对抗落地(v165) ═══
# 文档结论: 检测方靠「指纹不一致」识别 bot(可降绕过率 45-48%)·绕过方靠
# 「不常见浏览器可绕 82% 防护」。实战策略不是完美伪装·而是:
#   ① 多身份轮换: 单点检测失效——同一域名失败后换下一个身份;
#   ② 指纹一致性: 每个身份的 UA/Accept/Accept-Language/sec-ch-ua 全套自洽
#      (Safari UA 不带 Chrome 的 sec-ch-ua·iPhone UA 不带 macOS 平台头)·
#      避免自相矛盾被「指纹不一致检测」抓;
#   ③ 同域粘性: 正常用户不会一个会话里连续换 5 个浏览器——同域名成功后
#      粘住该身份·只在失败时轮换·检测方看不出规律;
#   ④ 频率自适应: 429/反爬页后该域名进指数冷却·别用硬撞浪费墙钟。
# 全部内存态·进程级·fail-safe。

_FP_IDENTITIES = (
    {  # Chrome 126 desktop macOS(主身份·兼容性最好)
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "Upgrade-Insecure-Requests": "1",
    },
    {  # Safari 17 macOS(文档:不常见浏览器可绕 82% 防护)
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                      "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        # Safari 不发 sec-ch-ua 系头——带了反而指纹矛盾
    },
    {  # Chrome mobile iPhone(移动端指纹池)
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
                      "(KHTML, like Gecko) CriOS/126.0.6478.114 Mobile/15E148 Safari/604.1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    },
    {  # Firefox 127 Windows(Gecko 引擎·完全不同的指纹簇)
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Upgrade-Insecure-Requests": "1",
    },
    {  # Edge 126 Windows
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Microsoft Edge";v="126"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Upgrade-Insecure-Requests": "1",
    },
)

# 域名态: {domain: {"idx": 身份下标, "fails": 连续失败数, "cooldown_until": ts}}
_FP_DOMAIN_STATE: dict = {}


def _fp_domain(url: str) -> str:
    try:
        from urllib.parse import urlparse as _up
        return (_up(url).netloc or "").lower().removeprefix("www.")
    except Exception:
        return ""


def fp_headers_for_url(url: str, attempt: int = 0) -> dict:
    """取该域名的身份请求头(粘性+失败轮换+冷却)。attempt=0 粘性身份·≥1 递进轮换。

    冷却中的域名返回 None 语义由调用方用 fp_domain_cooled() 判断——这里只发头。
    """
    import time as _t
    dom = _fp_domain(url) or "global"
    st = _FP_DOMAIN_STATE.setdefault(dom, {"idx": 0, "fails": 0, "cooldown_until": 0.0})
    if attempt > 0:
        st["idx"] = (st["idx"] + 1) % len(_FP_IDENTITIES)  # 多身份轮换
        st["fails"] += 1
        st["cooldown_until"] = _t.time() + min(60.0, 5.0 * (2 ** min(st["fails"], 4)))  # 频率自适应
    return dict(_FP_IDENTITIES[st["idx"] % len(_FP_IDENTITIES)])


def fp_report_block(url: str) -> None:
    """上报撞反爬(403/验证页)→该域名换身份+进指数冷却。"""
    fp_headers_for_url(url, attempt=1)


def fp_report_success(url: str) -> None:
    """上报成功→该域名粘住当前身份并清失败计数。"""
    dom = _fp_domain(url) or "global"
    st = _FP_DOMAIN_STATE.get(dom)
    if st:
        st["fails"] = 0
        st["cooldown_until"] = 0.0


def fp_domain_cooled(url: str) -> bool:
    """该域名是否在冷却中(调用方可先走存档/镜像路径·别硬撞)。"""
    import time as _t
    dom = _fp_domain(url) or "global"
    st = _FP_DOMAIN_STATE.get(dom)
    return bool(st and st.get("cooldown_until", 0.0) > _t.time())


@register_tool(
    name="fetch_url_smart",
    description=(
        "当你已经知道一个具体 URL(比如从搜索结果/论文 DOI/PMC ID 拼出来的地址)，"
        "想直接打开它拿到正文时用这个——不要用 shell_exec 写 curl/urllib 去抓，"
        "很多站点(尤其 NCBI/PMC/EuropePMC 这类学术站、以及知乎/CSDN/公众号等)"
        "对自动化直连请求会返回 reCAPTCHA 验证页或空壳而不是真内容，手写 curl"
        "只会反复撞墙拿到一堆 recaptcha/验证码 HTML，白白浪费工具调用。"
        "本工具内部会按域名反爬强度自动分级选择抓取战术(普通页面用 curl 秒回；"
        "中/强反爬站点自动升级为真实浏览器 playwright/browseract 渲染绕过验证)，"
        "不需要你自己判断该用什么方式。\n"
        "【🔓 订阅墙自动绕路】对于以下付费订阅墙域名，本工具会自动跳过直接抓取，"
        "走 Open Access 路径(Unpaywall → OpenAlex → Semantic Scholar → Sci-Hub)，"
        "尝试获取免费全文或摘要：ScienceDirect/Elsevier、Springer/Nature、"
        "Wiley、IEEE Xplore、ACM DL、JSTOR、Taylor&Francis、Sage、"
        "Cambridge、Oxford Academic 等。你直接传这些域名的 URL 即可，"
        "不需要自己判断能不能访问——如果有 OA 版本就自动拿到，没有 OA 版本"
        "会告诉你并建议搜 ResearchGate/预印本。\n"
        "传入 question 参数为要抓取的完整 URL。"
        "如果一次拿不到完整正文(比如是分页/需要点开某个 tab)，可以换更精确的"
        "子页面 URL 再调用一次。"
    ),
    category="search", cost_tier="medium", retryable=True, max_retry_hint=2,
)
def t_fetch_url_smart(question, session):
    url = (question or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return "错误: question 参数需要是完整的 URL(以 http:// 或 https:// 开头)"

    # v14.3(2026-08-17) · 加 Google 反代兜底(fail-safe):
    #   病根: anysearch_extract_network_blocked 是 anysearch 单源断点·
    #   fetch_url_smart 走 smart_fetch → 一旦 anysearch 抽风整条链路断。
    #   改造: 进 smart_fetch 之前先试 Google webcache 反代(谷歌全文镜像·
    #   能破 anysearch 90% 反爬 URL)→ 真拿到完整文本即立刻返回 evidence·
    #   失败再走原有 smart_fetch 路径(fail-safe,行为不破坏)。
    def _try_google_cache(target_url):
        try:
            import requests as _gc_req
            cached_url = f"https://webcache.googleusercontent.com/search?q=cache:{target_url}"
            r = _gc_req.get(
                cached_url, timeout=12,
                headers=fp_headers_for_url(target_url),  # v165 · 身份池(第4章)
            )
            if r.status_code == 200 and len(r.text) > 500:
                # 2026-08-26·0b26 实证: webcache 常回 Google 搜索/同意页而非目标缓存·
                #   之前无校验→把搜索页 HTML 当目标正文·下游抽数全错。命中中转页特征→
                #   判失败退回 smart_fetch(不返回假 content)。
                _low = r.text[:4000].lower()
                _wrapper = any(m in _low for m in (
                    "<title>google search</title>", "before you continue to google",
                    "consent.google", "did not match any documents",
                    "/sorry/index", "unusual traffic", "enablejs?", "our systems have detected"))
                if not _wrapper:
                    fp_report_success(target_url)
                    return {"ok": True, "content": r.text[:8000],
                            "via": "google_cache", "actual_via": "google_cache"}
        except Exception:
            pass
        return None

    def _invoke():
        # v166 · 断路器 OPEN 的域名直接跳过直连(文档: 阻断域名自动切档案库·
        # 别硬撞)·先走替代源链·拿到就返回。
        if circuit_is_open(url):
            _alt_body, _alt_via = _alt_source_chain(url)
            if _alt_body:
                return {"ok": True, "content": _alt_body[:20000],
                        "via": "alt_source", "actual_via": f"circuit_open:alt_{_alt_via}"}
        # 1) 先试 google cache 反代 — 命中即返回(零 LLM 成本,只 1 个 GET)
        _gc = _try_google_cache(url)
        if _gc:
            fm_record(url, "google_cache", True)
            return _gc
        # 2) 原有 smart_fetch 路径不变
        from search_agent_orchestrator import smart_fetch
        _r = smart_fetch(sanitize_url_for_curl(url))
        fm_record(url, "smart_fetch", bool(_r and _r.get("ok")),
                  str(( _r or {}).get("reason", ""))[:200])
        return _r

    # v2026-08-18(doc·wallclock PASTE-lite) · 预取命中优先: 搜索落账后后台线程
    # 已把 top URL 抓好(wallclock_optim 预热)→这里零等待直接当成功结果用。
    # 命中后仍走下方原有成功路径(evidence 落账/结论关键词定位/预览)·行为不变。
    # 未命中(无缓存/TTL过期/模块异常)→原样走 run_delegated·零回归。fail-safe。
    _result = None
    _meta = {"ok": True, "timed_out": False, "elapsed_s": 0.0, "error": ""}
    try:
        import wallclock_optim as _wco_f
        _pf = _wco_f.take_prefetched(url)
        if _pf and _pf.get("content"):
            _result = {"ok": True, "content": _pf["content"],
                       "via": "prefetch_cache",
                       "actual_via": f"prefetch:{_pf.get('via', '?')}"}
            fp_report_success(url)
    except Exception:
        _result = None

    if _result is not None:
        result, meta = _result, _meta
    else:
        # 机制2(2026-08-18·证据实证治 fetch 43 次重复) · URL 级缓存包装 real-fetch:
        #   TTL(900s)内同 URL 直接复用上次抓到的正文·不重抓(错题里同 URL 被反复抓)。
        #   env MEYO_FETCH_CACHE 默认开=0关。fail-safe: 缓存模块缺失/异常退回原路径。
        _invoke_final = _invoke
        try:
            import search_fabric_fetch as _sff_cache

            def _invoke_cached():
                def _rf(u, query=""):
                    return _invoke()   # 忽略 query(fetch_url_smart 无独立 query 参数)
                return _sff_cache.cached_fetch(url, query="", real_fetch=_rf)
            _invoke_final = _invoke_cached
        except Exception:
            _invoke_final = _invoke
        result, meta = run_delegated(_invoke_final, timeout_s=90)
    if not meta["ok"]:
        # v108(M2c·ded2实证) · 双网络栈互备: smart_fetch 整体超时/异常 ≠ URL 不可达
        # (实证: Crossref API smart_fetch 失败·同 URL 裸 curl 秒回)。降级用子进程
        # curl 重试一次·成功照样存证据(via=curl_fallback)。失败才报错。
        _fb = _curl_fallback(url)
        if _fb is not None:
            evidence = session.setdefault("evidence", [])
            evidence.append({"url": url, "title": url, "content": _fb,
                             "source": "fetch_url_smart(curl_fallback)"})
            _fb_preview = _fb[:500].replace("\n", " ")
            return (f"[fetch_url_smart] via=curl_fallback(主栈失败后双栈互备) · "
                    f"抓到 {len(_fb)} 字符 · 已存入 evidence\n"
                    f"预览: {_fb_preview}")
        return f"抓取未完成({meta['error']})，耗时{meta['elapsed_s']}s，可换个更精确的 URL 或改用 run_parallel_search"

    if not result or not result.get("ok"):
        reason = (result or {}).get("reason", "未知原因")
        actual_via = (result or {}).get("actual_via", "?")
        # v13.95 · OA 绕路失败时给 agent 明确提示（不要再反复调这个 URL）
        if actual_via == "oa_failed":
            oa_doi = (result or {}).get("oa_doi", "")
            doi_hint = f" DOI={oa_doi}" if oa_doi else ""
            return (f"抓取失败: {reason}\n"
                    f"[订阅墙确认{doi_hint}] 已尝试 Unpaywall/OpenAlex/SemanticScholar/Sci-Hub 均无 OA 版本。\n"
                    f"建议：①用 run_parallel_search 搜该论文标题+关键词找开放转载；"
                    f"②查找该研究组 ResearchGate/个人主页；③改用通用知识推断。")
        # v108(M2c) · 非订阅墙失败(反爬/超时/内部错误)→双栈互备再试一次裸 curl。
        # 订阅墙域名不降级(curl 拿不到付费全文·只会撞同一道墙)。
        _paywall_marks = ("订阅墙", "paywall", "oa_failed", "sciencedirect",
                          "springer", "wiley", "ieee", "jstor", "taylor",
                          "sage", "cambridge.org", "academic.oup.com")
        _low_reason = f"{reason} {url}".lower()
        if not any(k in _low_reason for k in _paywall_marks):
            _fb = _curl_fallback(url)
            if _fb is not None:
                evidence = session.setdefault("evidence", [])
                evidence.append({"url": url, "title": url, "content": _fb,
                                 "source": "fetch_url_smart(curl_fallback)"})
                _fb_preview = _fb[:500].replace("\n", " ")
                return (f"[fetch_url_smart] via=curl_fallback(主栈 {actual_via} 失败后双栈互备) · "
                        f"抓到 {len(_fb)} 字符 · 已存入 evidence\n"
                        f"预览: {_fb_preview}")
        # v166 · 终极替代源链(文档·修复3+情报扩散): 主栈+curl 全灭后·走
        # jina/textise/移动版廉价替代·拿到照样存 evidence(断路器同帧记录·
        # 下次该域名直接从替代源走·不重复踩坑)。
        _err_cls = classify_fetch_failure(f"{reason} {url}")
        circuit_record_fail(url, _err_cls)
        if not any(k in _low_reason for k in _paywall_marks):
            _alt_body, _alt_via = _alt_source_chain(url)
            if _alt_body:
                evidence = session.setdefault("evidence", [])
                evidence.append({"url": url, "title": url, "content": _alt_body[:20000],
                                 "source": f"fetch_url_smart(alt:{_alt_via})"})
                return (f"[fetch_url_smart] via=alt_source({_alt_via}·直连全灭后的替代源找平) · "
                        f"抓到 {len(_alt_body)} 字符 · 已存 evidence\n"
                        f"预览: {_alt_body[:400]}")
        _circ_note = " · 该域名已进断路器(直连冷却中·下次自动走替代源/存档)" if circuit_is_open(url) else ""
        return (f"抓取失败: {reason} · via={actual_via}{_circ_note}\n"
                f"建议：用 query_web_archive 查该 URL 的 Wayback 历史快照·"
                f"或 run_parallel_search 搜同主题其他来源")

    content = result.get("content", "") or ""
    evidence = session.setdefault("evidence", [])
    if content:
        circuit_record_success(url)
        fm_record(url, "fetch_url_smart", True)
        _via_tag = (f"({result.get('actual_via')})" if result.get("via") == "prefetch_cache"
                    else "")
        evidence.append({
            "url": url, "title": url, "content": content,
            "source": f"fetch_url_smart{_via_tag}",
        })
    preview = content[:500].replace("\n", " ")

    # v96i(2026-08-11) · 通用能力:长文档"结论性关键词定位"摘要。
    #   实证根因(米氏方程题):抓到 32092 字符的论文全文后，真正给出答案的
    #   那句话("a better form of the Michaelis-Menten equation would be...")
    #   在第 27873 字处(接近文末)，即使不再截断证据，后续 LLM(不管是抽答器
    #   还是主 agent 自己读)也容易在长文档摘要/预览里"读了开头就以为懂了"，
    #   漏掉藏在文档中后段的真正结论——这是通用的长文档场景问题，不只这一
    #   道题。这里在工具返回摘要时，主动用一批"这句话通常标志着作者给出最终
    #   结论/公式/答案"的短语去扫描全文，命中就把该句前后一段摘出来提到摘要
    #   最前面，让下一步决策(不管是继续调用哪个工具还是直接推理)不会错过它，
    #   而不是只给一段可能只覆盖文档开头背景介绍的 preview。
    _CONCLUSION_MARKERS = (
        "final equation", "final form", "better form", "preferred form",
        "the correct equation", "in conclusion", "we conclude", "final answer",
        "最终公式", "最终方程", "最终结论", "综上", "结论是",
    )
    _hit_snippets = []
    _content_lower = content.lower()
    for _kw in _CONCLUSION_MARKERS:
        _idx = _content_lower.find(_kw.lower())
        if _idx >= 0:
            _s = max(0, _idx - 150)
            _e = min(len(content), _idx + 400)
            _hit_snippets.append(f"…{content[_s:_e]}…".replace("\n", " "))
    _conclusion_block = ""
    if _hit_snippets:
        _joined = "\n---\n".join(_hit_snippets[:3])
        _conclusion_block = (
            f"\n\n【⚠️ 命中疑似关键结论段(优先看这里，可能就是答案所在，"
            f"别只看下面的开头预览)】\n{_joined}\n"
        )

    return (f"[fetch_url_smart] via={result.get('actual_via')} · "
            f"tier={result.get('tier')}({result.get('reason','')}) · "
            f"抓到 {len(content)} 字符 · 已存入 evidence"
            f"{_conclusion_block}\n预览(仅文档开头，完整正文已在 evidence 里): {preview}")


@register_tool(
    name="fetch_outlinks",
    description=(
        "【层3图遍历原语】给定一个 Wikipedia 页面标题，通过 MediaWiki API prop=links "
        "返回该页面所有出链标题列表（干净·完整·免费·无需抓 HTML）。\n\n"
        "适用场景:\n"
        "  • Wikipedia 链接跳数 / 几度分离题（如'从 A 到 B 最少几跳'）\n"
        "  • 需要图遍历(BFS)的题：用这个工具获取每跳的 frontier，写 BFS 循环\n"
        "  • 找某人/某物在 Wikipedia 上的直接关联实体\n\n"
        "用法:\n"
        "  • question = Wikipedia 页面标题，如 'Kevin Bacon' 或 'Python (programming language)'\n"
        "  • 返回: 出链标题列表(字符串)，可直接作为 BFS 下一层 frontier\n\n"
        "BFS 算法框架(在 code_exec 里实现):\n"
        "  frontier = {start_page}; visited = {start_page}; depth = 0\n"
        "  while depth < MAX_HOPS:\n"
        "    depth += 1\n"
        "    next_frontier = set()\n"
        "    for page in frontier:\n"
        "      links = fetch_outlinks(page)  # 本工具每次拿一页的出链\n"
        "      if target in links: return depth  # 命中即停\n"
        "      next_frontier |= set(links) - visited\n"
        "    frontier = next_frontier; visited |= frontier\n\n"
        "注意: 这是图遍历的原语层，'最短路径'类题一定要用 BFS 而非随机搜索。"
    ),
    category="search", cost_tier="cheap", retryable=True, max_retry_hint=2,
)
def t_fetch_outlinks(question, session):
    """获取 Wikipedia 页面出链列表 · MediaWiki API prop=links · 层3 BFS 原语"""
    page_title = (question or "").strip()
    if not page_title:
        return "错误: question 参数需要是 Wikipedia 页面标题（如 'Kevin Bacon'）"

    def _invoke():
        import sys, os
        v11_evo = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "v11", "evolution"
        )
        if v11_evo not in sys.path:
            sys.path.insert(0, v11_evo)
        from search_fabric_verticals import fetch_wikipedia_outlinks
        return fetch_wikipedia_outlinks(page_title, limit=200)

    result, meta = run_delegated(_invoke, timeout_s=15)
    if not meta["ok"]:
        return f"获取出链失败({meta['error']})，耗时{meta['elapsed_s']}s"

    links = result or []
    if not links:
        return f"页面 '{page_title}' 未找到或无出链（请检查页面标题拼写）"

    # 存入 session 供多跳循环读取
    outlinks_cache = session.setdefault("wiki_outlinks_cache", {})
    outlinks_cache[page_title] = links

    return (
        f"[fetch_outlinks] 页面 '{page_title}' · {len(links)} 条出链\n"
        f"出链列表(前30): {links[:30]}\n"
        f"...(共 {len(links)} 条·全量已存入 session['wiki_outlinks_cache']['{page_title}'])\n"
        f"提示: BFS 用法 → frontier 每跳调一次本工具取出链，visited 去重，命中 target 即输出跳数。"
    )


@register_tool(
    name="query_structured_db",
    description=(
        "【层2 API-first 精准查询】直接查结构化数据库（PubChem/Wikidata/PubMed 等），"
        "不走搜索引擎 SERP 刮取。适合需要精确属性值的题型:\n\n"
        "  • 化学物质: 分子式/分子量/IUPAC名/InChIKey/SMILES（→ PubChem PUG-REST）\n"
        "  • 实体关系/属性: 国籍/职业/出生日期/父子关系等（→ Wikidata SPARQL）\n"
        "  • 生物医学文献: 作者/摘要/发表年（→ PubMed E-utilities）\n\n"
        "用法:\n"
        "  • question = '{db}:{query}' 格式，如 'pubchem:aspirin'、'wikidata:Marie Curie'\n"
        "  • 或直接 question = 'aspirin molecular formula'（自动路由到最合适的 db）\n\n"
        "为什么不用 run_parallel_search:\n"
        "  搜索引擎对结构化数据库的查询质量差（刮取 HTML 脏·容易漏·格式不稳定）。\n"
        "  本工具走官方 REST/SPARQL API，直接返回机器可读的精确属性值。"
    ),
    category="search", cost_tier="cheap", retryable=True, max_retry_hint=2,
)
def t_query_structured_db(question, session):
    """层2 API-first 结构化数据库精准查询"""
    q = (question or "").strip()
    if not q:
        return "错误: question 参数不能为空"

    # 解析 db:query 格式。真e2e实证:agent常传"wikipedia:X"(它以为wikipedia是库)·
    #   但wikidata的wbsearchentities要【纯实体名】·带"wikipedia:"前缀搜不到→白查。
    #   把 wikipedia/wiki/dbpedia 都归为 wikidata·并剥离前缀(query质量修复)。
    _DB_ALIAS = {"wikipedia": "wikidata", "wiki": "wikidata", "dbpedia": "wikidata",
                 "pubchem": "pubchem", "wikidata": "wikidata", "pubmed": "pubmed",
                 "openalex": "openalex", "crossref": "openalex", "scholar": "openalex"}
    db = "auto"
    query = q
    if ":" in q and q.split(":")[0].lower() in _DB_ALIAS:
        parts = q.split(":", 1)
        db = _DB_ALIAS[parts[0].lower()]
        query = parts[1].strip()

    def _invoke():
        import sys, os
        v11_evo = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "v11", "evolution"
        )
        if v11_evo not in sys.path:
            sys.path.insert(0, v11_evo)
        from search_fabric_verticals import _VERTICAL_SOURCES, _search_pubchem, _search_wikidata, _search_pubmed

        # 自动路由(2026-08-15 · 语义判定·治硬编码词表违反铁律):
        #   优先让快模型【语义判】query 属于哪个库·词表只当模型不可用时的兜底(不是主判据)。
        #   病根:原 chem_kws/wiki_kws/med_kws 硬词表·query 用别的说法(如问"这个分子多重")
        #   就漏判·且违反 CLAUDE.md"脱敏/过滤/路由用语义判定非固定表"。
        def _keyword_route(q_lower):
            chem_kws = ("molecular", "formula", "weight", "smiles", "inchi", "compound",
                        "enzyme", "cas ", "ec number", "pubchem", "chemical")
            wiki_kws = ("nationality", "born", "died", "occupation", "country",
                        "founded", "located", "capital", "population", "wikidata")
            med_kws = ("pubmed", "clinical trial", "treatment", "disease", "drug",
                       "therapy", "gene", "protein", "pmid")
            if any(kw in q_lower for kw in chem_kws):
                return "pubchem"
            if any(kw in q_lower for kw in med_kws):
                return "pubmed"
            if any(kw in q_lower for kw in wiki_kws):
                return "wikidata"
            return "wikidata"

        if db == "auto":
            chosen_db = None
            try:
                import sys as _s2
                _agent_dir = os.path.dirname(os.path.abspath(__file__))
                if _agent_dir not in _s2.path:
                    _s2.path.insert(0, _agent_dir)
                import multi_model as _mm2
                _p = ("下面这个查询最该查哪个结构化数据库? 只回一个词:"
                      "pubchem(化学/化合物/分子/酶/CAS/EC) / "
                      "wikidata(人物/地点/组织/国家/通用事实) / "
                      "pubmed(医学/临床/疾病/药物/基因/文献)。\n查询: " + str(query)[:300])
                _r = _mm2.ask("claude-haiku", _p, timeout=12, max_tokens=12)
                if isinstance(_r, dict):
                    _r = _r.get("out", "") if _r.get("ok") else ""
                _rt = str(_r or "").strip().lower()
                for _cand in ("pubchem", "pubmed", "wikidata"):
                    if _cand in _rt:
                        chosen_db = _cand
                        break
            except Exception:
                chosen_db = None
            if chosen_db is None:  # 语义判不可用/无输出 → 词表兜底(非主判据)
                chosen_db = _keyword_route(query.lower())
        else:
            chosen_db = db

        db_map = {
            "pubchem": _search_pubchem,
            "wikidata": _search_wikidata,
            "pubmed": _search_pubmed,
        }
        fn = db_map.get(chosen_db)
        if fn is None:
            return {"db": chosen_db, "hits": [], "error": f"unsupported db: {chosen_db}"}

        hits = fn(query, top_k=5)
        return {"db": chosen_db, "hits": hits, "error": None}

    result, meta = run_delegated(_invoke, timeout_s=20)
    if not meta["ok"]:
        return f"查询失败({meta['error']})，耗时{meta['elapsed_s']}s"

    db_used = (result or {}).get("db", "?")
    hits = (result or {}).get("hits", [])
    err = (result or {}).get("error")
    if err:
        return f"[query_structured_db/{db_used}] 错误: {err}"

    # 过滤错误项
    valid = [h for h in hits if not h.get("_error")]
    errors = [h for h in hits if h.get("_error")]

    if not valid:
        errmsg = " | ".join(str(e.get("_error","")) for e in errors)
        return f"[query_structured_db/{db_used}] 未找到结果（query='{query}'）{' 错误:'+errmsg if errmsg else ''}"

    # 存入 evidence
    evidence = session.setdefault("evidence", [])
    for h in valid:
        evidence.append(h)

    # 格式化返回
    lines = [f"[query_structured_db/{db_used}] query='{query}' · {len(valid)} 条结果\n"]
    for i, h in enumerate(valid[:5], 1):
        lines.append(f"  [{i}] {h.get('title','')}")
        lines.append(f"       {h.get('content','')[:200]}")
        lines.append(f"       URL: {h.get('url','')}")
    lines.append(f"\n已存入 evidence，可调用 run_factual_extract 精确抽取答案。")
    return "\n".join(lines)


_chain_probe_ctr = [0]  # v2026-08-18(doc2) 链式探测计数: 每5次 query_web_archive 放行1次全源探测(给熔断源恢复机会)


@register_tool(
    name="query_web_archive",
    description=(
        "【网页存档/历史快照精确反查】当目标页面已下线/被新内容淹没/搜索引擎"
        "翻不到时用这个，不要继续用 run_parallel_search 反复换关键词硬搜——"
        "历史已下线内容属于结构化反查，通用搜索引擎基本答不了。\n\n"
        "触发场景（命中任一条就该用这个而不是继续换关键词）：\n"
        "  • 题目描述的是某个具体日期发布的博客/新闻/论坛帖，但搜索引擎搜"
        "不到原文（多为小众博客/已下线站点/被 SEO 新内容淹没）\n"
        "  • run_parallel_search 连续 2-3 轮都是新的关键词变体但零新证据\n"
        "  • 需要验证某个已知 URL 在特定历史时间点的内容\n\n"
        "用法（question 支持四种自然语言协议，工具自动路由 + 全部失败时自动"
        "串行降级到下一个，不需要你自己挑用哪个源）：\n"
        "  • 已知具体 URL → 直接给 URL，如 'https://myblog.com/2012/12/27/post/'\n"
        "  • 已知域名+日期区间+关键词 → 'myblog.com 2011-2014 best-songs-of'\n"
        "  • 已知域名+精确到日的发布日期(找当天全部发帖，域名未知时无效)→ "
        "'myblog.com 2012-12-27'\n"
        "  • 已知 blogspot 域名+日期窗 → 'myblog.blogspot.com 2012-12'\n\n"
        "内部依次尝试: wayback_cdx(通用CDX检索) → wayback_cdx_date(按发布日期"
        "枚举 WordPress 固定链接) → blogspot_feed(Blogspot Feed API) → "
        "archive_today(archive.ph/.today/.is，与 web.archive.org 完全不同域名池"
        "的独立替代存档源，前三个都因死域熔断/网络问题拿不到结果时的最后备份)。"
        "任何一个源已拿到有效结果就立即返回，不空跑后续。"
    ),
    category="search", cost_tier="cheap", retryable=True, max_retry_hint=2,
)
def t_query_web_archive(question, session):
    q = (question or "").strip()
    if not q:
        return "错误: question 参数不能为空，请给 URL 或 '域名 日期/区间 关键词'"

    def _invoke():
        import sys, os
        v11_evo = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "v11", "evolution"
        )
        if v11_evo not in sys.path:
            sys.path.insert(0, v11_evo)
        from search_fabric_verticals import (
            _search_wayback_cdx, _search_wayback_cdx_date,
            _search_blogspot_feed, _search_archive_today,
        )

        # v2026-08-18(doc2闭环) · 链式降级感知死域熔断: 直接读全局死域账本·
        # 熔断中的源【跳过不试】(wayback 熔断时省 30-45s 超时·直奔独立域名池源)。
        # 每5次链式调用放行1次全源探测(给熔断源恢复机会·与主循环探测定理对齐)。
        # 同时回写: 链内连接层失败/成功也更新账本(主循环只记 query_web_archive
        # 这个壳工具名·不往 _DEAD_NET_TOOLS 里加多源壳·归因会乱)。
        _SRC_HOST = {
            "wayback_cdx": "web.archive.org",
            "wayback_cdx_date": "web.archive.org",
            "archive_today": "archive.ph",
        }
        _DD_FILE = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "state", "dead_domains.json"
        )

        def _dd_host_dead(host):
            try:
                import json as _j, time as _t
                with open(_DD_FILE, encoding="utf-8") as _f:
                    _d = _j.load(_f) or {}
                _e = _d.get(host) or {}
                return (int(_e.get("fails", 0) or 0) >= 3
                        and _t.time() - float(_e.get("last_fail_ts", 0) or 0) < 6 * 3600)
            except Exception:
                return False

        def _dd_mark(host, ok):
            try:
                import json as _j, time as _t
                _d = {}
                try:
                    with open(_DD_FILE, encoding="utf-8") as _f:
                        _d = _j.load(_f) or {}
                except Exception:
                    _d = {}
                _e = _d.get(host)
                if ok:
                    if _e:
                        _d.pop(host, None)
                else:
                    if not _e:
                        _e = {"fails": 0, "blocked": 0}
                        _d[host] = _e
                    _e["fails"] = int(_e.get("fails", 0) or 0) + 1
                    _e["last_fail_ts"] = _t.time()
                import threading as _th
                del _th  # 链内单线程串行·无需真锁(主循环侧有锁·最坏丢一次计数)
                with open(_DD_FILE, "w", encoding="utf-8") as _f:
                    _j.dump(_d, _f, ensure_ascii=False)
            except Exception:
                pass

        _CONN_DEAD_SIGS = ("timed out", "timeout", "unreachable", "connection",
                   "failed to connect", "getaddrinfo", "name resolution",
                   "网络", "超时", "不可达", "tcp")
        _chain_probe_ctr[0] += 1

        # 按"通用性→精确性→独立备份"顺序尝试，命中即停(每源内部已有自己的
        # 超时/重试)。任何单源异常都不能中断整条链——存档源本就不稳定，
        # 这正是需要"链式降级"而不是"选一个就赌上"的原因(证据图谱扩散/替代源
        # 找平器的核心思想:一条路走不通立刻自动换下一条，而不是原地同义改写)。
        chain = [
            ("wayback_cdx", _search_wayback_cdx),
            ("wayback_cdx_date", _search_wayback_cdx_date),
            ("blogspot_feed", _search_blogspot_feed),
            ("archive_today", _search_archive_today),
        ]
        tried = []
        _probe_all = (_chain_probe_ctr[0] % 5 == 0)  # 每5次放行1次全源探测
        for name, fn in chain:
            _host = _SRC_HOST.get(name)
            if (_host and not _probe_all and _dd_host_dead(_host)):
                tried.append((name, f"跳过: {_host} 死域熔断中(账本fails≥3)"))
                continue
            try:
                hits = fn(q, top_k=6) or []
            except Exception as e:
                if _host:
                    _dd_mark(_host, ok=False)
                tried.append((name, f"exception: {type(e).__name__}: {str(e)[:80]}"))
                continue
            valid = [h for h in hits if isinstance(h, dict) and not h.get("_error")]
            if valid:
                if _host:
                    _dd_mark(_host, ok=True)
                return {"source_used": name, "hits": valid, "tried": tried, "error": None}
            errs = [h.get("_error", "") for h in hits if isinstance(h, dict) and h.get("_error")]
            _err_txt = "; ".join(str(e) for e in errs)[:120]
            if _host and any(s in _err_txt.lower() for s in _CONN_DEAD_SIGS):
                _dd_mark(_host, ok=False)  # 连接层失败→回写账本
            tried.append((name, _err_txt if errs else "无结果"))
        return {"source_used": None, "hits": [], "tried": tried,
                "error": "四个存档源全部未命中/不可达"}

    # 90s: 四源链式降级最坏情况(wayback_cdx 重试~15s + wayback_cdx_date~30s +
    # blogspot_feed~10s + archive_today 25s 预算)之和留够余量·不会被外层
    # timeout 提前腰斩导致明明能拿到结果的后段源没机会跑。
    result, meta = run_delegated(_invoke, timeout_s=90)
    if not meta["ok"]:
        return f"[query_web_archive] 查询失败({meta['error']})，耗时{meta['elapsed_s']}s"

    src = (result or {}).get("source_used")
    hits = (result or {}).get("hits", [])
    tried = (result or {}).get("tried", [])
    if not hits:
        tried_lines = "\n".join(f"  · {n}: {msg}" for n, msg in tried)
        return (
            f"[query_web_archive] 四个存档源均未命中或不可达:\n{tried_lines}\n"
            f"建议: ①换更精确的 URL/日期重查；②改用 run_parallel_search 找"
            f"该内容的转述/镜像/二手引用来源；③若已有强锚点线索(作者名/专辑名/"
            f"关联实体)，优先反查该实体本身而非死磕这一个页面。"
        )

    evidence = session.setdefault("evidence", [])
    for h in hits:
        evidence.append(h)

    lines = [f"[query_web_archive/{src}] query='{q}' · {len(hits)} 条存档结果(已存入 evidence)\n"]
    for i, h in enumerate(hits[:6], 1):
        lines.append(f"  [{i}] {h.get('title','')}")
        lines.append(f"       {h.get('content','')[:200]}")
        lines.append(f"       URL: {h.get('url','')}")
    if len(tried) > 1:
        skipped = [n for n, _ in tried if n != src]
        lines.append(f"\n(已跳过/失败源: {', '.join(skipped) if skipped else '无'} · 用的是 {src})")
    lines.append(f"\n下一步: 用 fetch_url_smart 打开上述快照 URL 读全文，或直接 run_factual_extract。")
    return "\n".join(lines)


@register_tool(
    name="run_factual_extract",
    description=(
        "可选、较快。当问题要的是一个精确答案(具体数字/日期/名称/人名，比如"
        "「多少个」「哪一年」「叫什么名字」)时调用，从已收集证据(evidence)"
        "里只抽不推导地找出精确答案，抽不到会诚实返回 UNKNOWN 而不是编造。"
        "建议先跑过 run_parallel_search 或 run_deep_research_swarm 攒够证据再调，"
        "抽到的答案存入 session['factual_answer']，finalize_answer 时可直接引用。"
        "内部会自动带上 run_deep_research_swarm 已经沉淀的结论/未解决问题作为"
        "上下文，不需要你重复描述。"
    ),
    category="qc", cost_tier="cheap",
)
def t_run_factual_extract(question, session):
    evidence = _sget(session, "evidence", []) or []
    if not evidence:
        return "错误: 还没有证据，请先调用 run_parallel_search 或 run_deep_research_swarm"
    # v59 · 带上 mission_state 累积的已知事实/未解决缺口(如 run_deep_research_swarm
    # 已经沉淀过的 conclusions/unresolved_questions)，让抽答"回看"全程进展，
    # 不是每次都从零翻一遍证据——这正是本次要接的 auto 模式对应节点。
    mission_hint = MissionState(session).render_for_prompt()
    ans = M._factual_extract(
        question, evidence, {}, call_llm_fn=M._call_llm,
        multihop_evidence=[], log=None, mission_hint=mission_hint)
    if ans:
        session["factual_answer"] = ans
        MissionState(session).add_fact(ans, source="run_factual_extract")
        return f"精确答案: {ans[:100]}"
    # RC7/RC8: direct extraction found no explicit answer. Do not ask a second model to
    # rank loosely-related entity tokens and relabel one as "精确答案" — the trace audit
    # showed this fabricating `University`, `Spain`, `Films`, and wrong numbers. Return a
    # typed negative observation and let the primary model decide whether to search,
    # compute, or abstain. The tool never selects the answer for the model.
    return json.dumps({
        "status": "not_found",
        "answer": None,
        "reason": "no explicit answer found in collected evidence",
        "evidence_count": len(evidence),
    }, ensure_ascii=False)


@register_tool(
    name="judge_evidence_stance",
    description=(
        "判断已收集证据的利益立场(独立第三方/有倾向/纯广告)，广告类证据后续"
        "写作会被剔除。在 extract_cards 之前调用效果最好，但不是强制前置条件。"
    ),
    category="qc", cost_tier="cheap",
)
def t_judge_evidence_stance(question, session):
    stats = M.step3_stance_judge(_sget(session, "evidence", []) or [])
    session["stance_stats"] = stats
    return json.dumps(stats, ensure_ascii=False)


# ═══════════════════════ 写作类 ═══════════════════════

@register_tool(
    name="extract_cards",
    description=(
        "核心步骤。把已收集的证据(run_parallel_search / run_deep_research_swarm"
        "的产出)提炼成结构化观点卡片(三件套：论点/依据/来源)，供 write_narrative "
        "写作使用。必须在至少一次搜索类工具调用之后再调用；如果证据为空会用"
        "通识兜底但质量会打折。"
    ),
    category="writing", cost_tier="medium",
)
def t_extract_cards(question, session):
    scope = _sget(session, "scope") or {}
    cards = M.step4_extract_cards(question, _sget(session, "evidence", []) or [], scope)
    session["cards"] = cards
    return f"卡片已生成，{len(cards)} 字"


@register_tool(
    name="write_narrative",
    description=(
        "核心步骤。基于 extract_cards 的卡片写最终叙事正文(严格用户示范格式："
        "画面感标题+展示搜索过程+具体可执行方案)。必须在 extract_cards 之后调用。"
        "写完建议接 reader_check_and_fix 做读者视角检查。"
    ),
    category="writing", cost_tier="expensive",
    required_session_keys=["cards"],
)
def t_write_narrative(question, session):
    if not _sget(session, "cards"):
        return "错误: 还没有卡片，请先调用 extract_cards"
    # v111(2026-08-16 · first_shot 65/165 快错实证) 写前证据充分性闸: 快错型
    # agent 顺利不卡壳(卡壳闸抓不到)·但写正文是必经点——首次写正文前还没调过
    # assess_info_sufficiency → 拦一次·逼它逐条对照题面约束查证据缺口。
    # once-only(session 标记)·查过=自证免拦·fail-safe。
    try:
        _tl = session.get("tool_log") or []
        _did = any(e.get("tool") == "assess_info_sufficiency" for e in _tl)
        if not _did and not session.get("_assess_nudge_done"):
            session["_assess_nudge_done"] = True
            return (
                "[写前证据充分性闸 · 先补一步再写] 你准备写正文了·但全程还没调过 "
                "assess_info_sufficiency——快错多数不是不会做·而是题面某条约束"
                "(时间点/范围/包含标准/否定条件/单位精度)的证据缺口没被发现就开写了。"
                "先调一次 assess_info_sufficiency 逐条对照:\n"
                "  ① 题面每条约束 → 各有哪条证据支撑?逐条列出来;\n"
                "  ② 有缺口 → 先回去补搜索/换数据源·补齐再回来写;\n"
                "  ③ 题面含否定+or/and 等多读法结构 → 把各读法的数字并排算出来再选主答。\n"
                "查完确认无缺口·带着核对记录再调 write_narrative(不会再拦)。"
            )
    except Exception:
        pass
    scope = _sget(session, "scope") or {}
    cost_rec = _sget(session, "cost_rec") or {"skipped": "未走cost-first"}
    narrative = M.step5_narrative(
        question, session["cards"], scope, cost_rec,
        evidence=_sget(session, "evidence", []), real_queries={}, scen_display={})
    session["narrative"] = narrative
    return f"正文已生成，{len(narrative)} 字"


@register_tool(
    name="reader_check_and_fix",
    description=(
        "建议在 write_narrative 之后调用一次。装读者视角通读正文，把「不清楚在"
        "说什么」的地方就地替换成具体表述。会原地更新正文，不需要你手动传内容。"
    ),
    category="qc", cost_tier="medium",
    required_session_keys=["narrative"],
)
def t_reader_check_and_fix(question, session):
    if not _sget(session, "narrative"):
        return "错误: 还没有正文，请先调用 write_narrative"
    narrative, n_unclear = M.step6_reader_check_replace(question, session["narrative"])
    session["narrative"] = narrative
    return f"替换了 {n_unclear} 处不清楚表述"


# ═══════════════════════ 澄清类 (qa_clarify) ═══════════════════════

@register_tool(
    name="generate_clarify_questions",
    description=(
        "可选。当问题缺关键场景信息(角色/规模/阶段)、直接回答容易文不对题时调用，"
        "生成 2-4 道多选题交给用户先选再深入。适合 scenario_gap>0 的题。"
        "如果用户是自动化/无交互场景，不要调用这个，直接按合理假设往下走。"
    ),
    category="optional", cost_tier="cheap",
)
def t_generate_clarify_questions(question, session):
    try:
        from qa_clarify import generate_clarify_questions
        scenarios = _sget(session, "scenarios", []) or []
        clarify = generate_clarify_questions(question, scenarios, max_qs=3)
        session["clarify"] = clarify
        if not clarify.get("should_clarify"):
            return "判定无需澄清，可直接继续"
        return f"生成 {len(clarify.get('questions', []))} 道澄清多选题，已存入 session['clarify']"
    except Exception as e:
        return f"澄清生成失败(跳过): {str(e)[:100]}"


# ═══════════════════════ 工具生产类 (nocode) ═══════════════════════

@register_tool(
    name="produce_nocode_tool",
    description=(
        "可选、较慢(可达数分钟)。仅当 check_tool_cost_first 判断值得引入工具、"
        "且问题类型适合真部署一个可交互小工具时调用(比如做个表单/计算器/清单页"
        "比甩一段文字说明更帮读者)。会把部署后的 URL 追加到正文末尾。"
        "纯方法论/概念解释类问题不需要调用。"
    ),
    category="optional", cost_tier="expensive",
    required_session_keys=["narrative"],
)
def t_produce_nocode_tool(question, session):
    """委托执行: 部署可达数分钟，同样套用 run_delegated 硬超时保护，
    避免一个卡住的部署任务把整个 auto agent 的墙钟时间预算都吃掉。
    超时不影响 narrative 已有内容，直接降级跳过工具部署。"""
    if not _sget(session, "narrative"):
        return "错误: 还没有正文，请先调用 write_narrative"
    scope = _sget(session, "scope") or {}
    cost_rec = _sget(session, "cost_rec") or {}
    qid = _sget(session, "qid", "")
    narrative = session["narrative"]

    result, meta = run_delegated(
        M.step7_nocode_produce, question, scope, cost_rec, narrative, qid=qid,
        timeout_s=240,
    )
    if not meta["ok"]:
        return f"工具部署未完成({meta['error']})，已收口·耗时{meta['elapsed_s']}s，跳过部署继续交付正文"

    final, decision = result
    session["narrative"] = final
    session["nocode_decision"] = decision
    return f"url={decision.get('url','无')}" if isinstance(decision, dict) else str(decision)[:80]


@register_tool(
    name="generate_action_cone",
    description=(
        "可选。给答案追加「配套工具锥」段(MVP工具→本周深化→长期架构，分层给"
        "不同深度的落地建议)，并可能对每层各自判断要不要单独部署 nocode 工具。"
        "适合 tool_selection/decision/process 类问题；纯概念解释(concept)不需要。"
    ),
    category="optional", cost_tier="expensive",
    required_session_keys=["narrative"],
)
def t_generate_action_cone(question, session):
    if not _sget(session, "narrative"):
        return "错误: 还没有正文，请先调用 write_narrative"
    try:
        from qa_action_cone import generate_action_cone, judge_action_type
        action_type = judge_action_type(question)
        if action_type == "concept":
            return "判定为概念解释题，不需要工具锥"
        nocode_decision = _sget(session, "nocode_decision") or {}
        nocode_url = nocode_decision.get("url") if isinstance(nocode_decision, dict) else None
        cone_block = generate_action_cone(question, session["narrative"], nocode_url=nocode_url,
                                          question_type=action_type)
        if not cone_block:
            return "工具锥生成为空，跳过"
        qid = _sget(session, "qid", "")
        try:
            from qa_multi_nocode import deploy_multi_nocode
            cone_block = deploy_multi_nocode(
                question, cone_block, qid=qid, max_deploys=3, log=print,
                main_nocode_url=nocode_url,
                main_tool_spec=nocode_decision.get("tool_spec", "") if isinstance(nocode_decision, dict) else "")
        except Exception as e:
            print(f"  [multi_nocode fail] {e}·工具锥用原版", file=sys.stderr)
        session["narrative"] = session["narrative"].rstrip() + "\n\n" + cone_block
        return f"工具锥已生成并追加，type={action_type}"
    except Exception as e:
        return f"工具锥生成失败(跳过): {str(e)[:100]}"


# ═══════════════════════ 自检/收尾类 ═══════════════════════

@register_tool(
    name="run_self_check",
    description=(
        "建议在准备交付前调用一次。对当前正文做四问自检(scope有没有跑偏/"
        "证据够不够/方案够不够具体/有没有前后矛盾)，返回 overall 评级(pass/"
        "needs_fix)和具体问题列表。如果 needs_fix，优先针对性补救(比如证据"
        "不够就调用 run_parallel_search 补搜索、方案不具体就重新 write_narrative)，"
        "而不是无限次调用同一个工具。"
    ),
    category="qc", cost_tier="medium",
    required_session_keys=["narrative"],
)
def t_run_self_check(question, session):
    if not _sget(session, "narrative"):
        return "错误: 还没有正文，请先调用 write_narrative"
    final, report = M.step8_5_self_check(question, session["narrative"])
    session["narrative"] = final
    session["self_check_report"] = report
    return f"overall={report.get('overall','?')} · critical_fails={len(report.get('critical_fails', []))}"


# ════════════════════════════════════════════════════════════════════
# 2026-08-19 事故修复③ · finalize 只传 question 丢答案 · 从 agent 自己的推理抽答案
# ════════════════════════════════════════════════════════════════════
# 病根(bc 实测活样本 2026-08-19): 数学多跳题 auto 已推理出正确答案(Russell
#   Lyons 类)·验证/完整性 5 维自检全过·调了 finalize_answer 3 次——但**每次参数
#   只填了 question(题面)·漏填了它找到的答案(final_markdown 空)**。旧 fallback
#   链(final_markdown → narrative → _last_finalize_attempt → question)直接退到
#   narrative/question·结果交出的是"题面复述+元评论"而非答案·session.final 无答案值·
#   评分器判 ❌。这是 done-final 空病根的同类(finalize 只传 question 不传 answer)。
#
# 修法(与软锁候选收尾必交/answer_vault 反误杀同源·不重复): finalize 时检出
#   "answer 缺失/等于 question/是纯元评论(无具体实体)"→ 从【agent 自己的产出】
#   按优先级抽答案填 final(绝不碰 gold):
#     ① 软锁候选(identity_coverage confirmed/probable·四态覆盖率最高候选·最优先)
#     ② answer_vault(工具输出/AI 断言里锁的明确答案行)
#     ③ session["factual_answer"](抽事实工具锁定的实体)
#     ④ light tier LLM 从 narrative/evidence/tool_log 抽"agent 到底认为答案是什么"
#   抽到→填 final(非元评论);抽不到→退回原兜底链(仍保证 done-final 非空)。
# env 开关 MEYO_FINALIZE_ANSWER_EXTRACT 默认开·fail-safe·通用不针对具体题。
def _finalize_extract_enabled():
    return os.environ.get("MEYO_FINALIZE_ANSWER_EXTRACT", "1").strip().lower() \
        not in ("0", "false", "off", "no")


_META_ONLY = re.compile(
    r"(?i)(?:我(?:已|会|将|需要)|你(?:应该|需要)|请(?:先|继续|调用)|建议|需要更多|"
    r"i\s+(?:will|need|should|have)|you\s+(?:should|need)|let\s+me|"
    r"unable\s+to|cannot\s+determine|无法确定|尚未|还没|下一步|接下来)")


def _looks_like_answer(text, question):
    """判 text 是否像【真答案】(而非题面复述/纯元评论/空)。fail-safe→保守判 True。

    True = 可直接当答案交;False = 触发从 agent 产出抽答案。
    通用: 不针对具体题·只看结构(等于题面 / 疑问词开头 / 只有元评论动词 / 太短)。"""
    try:
        import answer_vault as _av
        t = (text or "").strip()
        q = (question or "").strip()
        if len(t) < 2:
            return False
        # 等于题面(或题面是它的前缀/它是题面前缀)= 只复述了 question
        if t == q or (q and len(q) >= 8 and (t.startswith(q[:60]) or q.startswith(t[:60]))):
            return False
        # 疑问词开头(what/which/who/find/identify...) = 题面复述·不是答案
        if _av._QUESTIONY.match(t):
            return False
        # 占位/放弃句(unknown/无法确定...) = 无答案
        if _av._PLACEHOLDER.search(t[:300]):
            return False
        # 纯元评论: 短文本(<120)且只有"我将去做/你应该/需要更多"这类过程话·
        #   又检不出任何明确答案断言 → 判元评论。长文本(narrative)不在此列。
        if len(t) < 120 and _META_ONLY.search(t):
            ans, _ = _av.detect_answer(t)
            if not ans:
                return False
        return True
    except Exception:
        return True


def _extract_agent_answer(session, question):
    """从 agent 已形成的候选和证据中恢复答案，不接触 gold。

    新会话以 canonical event projection 为权威；只有尚未建立候选生命周期的
    legacy 会话才回退到 soft lock / AnswerVault / factual_answer / LLM 抽取。
    """
    if not _finalize_extract_enabled():
        return None, None
    if not isinstance(session, dict):
        return None, None
    try:
        event_path = str(session.get("_event_log_path") or "")
        if event_path and os.path.exists(event_path):
            import qa_event_stream as _qes_extract
            projected = _qes_extract.replay_file(event_path)
            verified = [(cid, item) for cid, item in projected.candidates.items()
                        if item.get("state") == "delivery_verified"]
            selected = [(cid, item) for cid, item in projected.candidates.items()
                        if item.get("state") == "selected"]
            canonical = verified or selected
            if canonical:
                candidate_id, item = canonical[0]
                value = str(item.get("value") or "").strip()
                if value:
                    return "答案: %s" % value, "event_candidate(%s)" % candidate_id
            if projected.candidates:
                return None, "event_lifecycle_incomplete"
    except Exception:
        return None, "event_projection_invalid"
    # Legacy-only fallback: no canonical candidate lifecycle exists yet.
    # ① 软锁候选(与 identity_coverage.soft_lock_final_body 复用同一渲染·不重复造轮子)
    try:
        import identity_coverage as _ic
        body, meta = _ic.soft_lock_final_body(session)
        if body and str(body).strip():
            return body, f"soft_lock({(meta or {}).get('entity','?')})"
    except Exception:
        pass
    # ② answer_vault(工具输出/AI 断言里锁定的明确答案行)
    try:
        import answer_vault as _av
        vt = _av.answer_text(session)
        if vt and len(vt.strip()) >= 2:
            return "答案: %s" % vt.strip(), "answer_vault"
    except Exception:
        pass
    # ③ factual_answer(抽事实工具锁定的实体)
    try:
        fa = str(session.get("factual_answer") or "").strip()
        if fa and len(fa) >= 2 and _looks_like_answer(fa, question):
            return "答案: %s" % fa, "factual_answer"
    except Exception:
        pass
    # ④ light tier LLM 从 agent 自己的产出(narrative/evidence/tool_log)抽结论。
    #    关键: 从 agent 的推理过程/证据抽"它认为答案是什么"·不是从 gold。
    try:
        narrative = str(_sget(session, "narrative", "") or "")[:4000]
        ev_list = _sget(session, "evidence", []) or []
        ev_txt = "\n".join(
            "- %s" % str((e.get("text") if isinstance(e, dict) else e) or "")[:300]
            for e in ev_list[:15])[:4000]
        tl = session.get("tool_log") or []
        tl_txt = "\n".join(
            str((e.get("summary") if isinstance(e, dict) else e) or "")[:200]
            for e in tl[-15:] if e)[:3000]
        material = ("[narrative]\n%s\n\n[evidence]\n%s\n\n[tool_log]\n%s"
                    % (narrative, ev_txt, tl_txt)).strip()
        if len(material) < 20:
            return None, None
        import multi_model as _mm
        prompt = (
            "下面是一个解题 agent 自己产出的推理过程 / 证据 / 工具日志。它已经推出了答案·"
            "但在交卷时忘了把答案填进交付字段(只填了题面)。你的任务: 从【agent 自己的产出】"
            "里读出【它到底认为答案是什么】——不是你来解题·是把它已经得出的结论抽出来。\n"
            "规则: ①只从下面材料抽·不要自己另算/猜; ②如果材料里 agent 明确表述了结论"
            "(如 'Person A = X' / '答案是 X' / 锁定某实体)·输出那个具体答案值; "
            "③如果材料里根本没有明确结论(agent 还没得出答案)·输出 NONE。\n"
            "只输出答案值本身(一行·尽量简短)·或 NONE。不要解释。\n\n"
            "题目: %s\n\n材料:\n%s" % ((question or "")[:600], material[:9000]))
        r = _mm.ask("longcat-2.0", prompt, timeout=45, max_tokens=120)
        if isinstance(r, dict) and r.get("ok"):
            out = str(r.get("out") or "").strip().strip("`*#> ").strip('"“”').strip()
            out = out.splitlines()[0].strip() if out else ""
            if (out and out.upper() != "NONE" and len(out) >= 2
                    and _looks_like_answer(out, question)):
                return "答案: %s" % out, "llm_extract"
    except Exception:
        pass
    return None, None


@register_tool(
    name="finalize_answer",
    description=(
        "最后一步必调，用于结束整个任务并交付答案。"
        "两种用法: "
        "1) 正常流程: 不传参数(留空)，会自动取当前会话里的 narrative 做格式后处理"
        "(URL 超链接化/中文标点/完整性检查)并交付。 "
        "2) 琐事直答: 如果 judge_scope_and_triage 判定 task_level=琐事 并给出了"
        "direct_answer，必须原样把 direct_answer 的内容作为 final_markdown 传入"
        "(可以在前面加一个简短标题，但正文内容不要重新展开成长文)，跳过整个"
        "搜索写作流程，快速交付。 "
        "调用后整个任务结束，之后不应再调用其他工具。"
    ),
    category="delivery", cost_tier="cheap",
)
def t_finalize_answer(question, session, final_markdown=""):
    # v15.7 · 四级 fallback(final_markdown → narrative → _last_finalize_attempt
    #   → question): auto-agent 常把答案文本塞 question 参数而非 final_markdown
    #   (Q0 白卷实证: 两交 finalize 均未带 final_markdown·narrative 又空→拒服务)
    _fm = (final_markdown or "").strip()

    # ── 2026-08-19 事故修复③ · finalize 只传 question 丢答案 · 从 agent 产出抽答案 ──
    # 病根(bc 实测): agent 已推出答案·finalize 却只填 question(final_markdown 空)·
    #   或填了纯元评论/题面复述——旧链会退到 narrative/question 交出元评论(判 ❌)。
    # 修: final_markdown 缺失 / 等于题面 / 是纯元评论(无具体实体)时·先从 agent
    #   【自己的推理/证据/软锁候选/vault】抽答案(绝不碰 gold);抽到就用·抽不到
    #   才退回原兜底链(仍保证 done-final 非空)。参数校验 + 抽取合一·通用·fail-safe。
    _need_extract = (not _fm) or (not _looks_like_answer(_fm, question))
    if _need_extract:
        try:
            _ext, _src = _extract_agent_answer(session, question)
        except Exception:
            _ext, _src = None, None
        if _ext and str(_ext).strip():
            # 抽到 agent 自己的结论 → 用它当交付正文(不再交元评论/题面)。
            _fm = str(_ext).strip()
            try:
                session["_finalize_answer_extracted"] = {
                    "source": _src, "orig_had_answer": bool(final_markdown),
                }
            except Exception:
                pass

    # 兜底链: 抽取后的 _fm → narrative → _last_finalize_attempt → question。
    # 抽不到任何答案线索时仍退回原链(narrative/question)保证非空(不破坏 done-final
    #   空修复)——此时交出的是 best-effort·外层 DEGRADED/软锁收尾仍会打标兜底。
    text = (_fm
            or _sget(session, "narrative", "")
            or str(session.get("_last_finalize_attempt") or "").strip()
            or (question or "").strip())
    if not text:
        return "错误: 没有可交付的内容，narrative 为空且未提供 final_markdown"
    try:
        text = M.postprocess_v35_format(text)
    except Exception:
        pass
    try:
        issues = M.sanity_check_full(text)
        if issues:
            text = M.sanity_auto_fix(text, issues)
    except Exception:
        pass
    # Canonical projection is authoritative when a lifecycle exists. Legacy-only
    # sessions keep their old behavior during migration.
    try:
        event_path = str(session.get("_event_log_path") or "")
        if event_path and os.path.exists(event_path):
            import qa_event_stream as _qes_final
            store = _qes_final.EventStore(
                event_path, run_id=str(session.get("run_id") or session.get("qid") or "unknown"),
                session_id=str(session.get("session_id") or session.get("qid") or "unknown"),
                question_id=str(session.get("qid") or "unknown"))
            projected = _qes_final.project(store.read())
            selected = [(cid, record) for cid, record in projected.candidates.items()
                        if record.get("state") == "selected"]
            if not selected:
                return "[候选事件未闭环] 当前没有 selected 候选，先让 SoulDriver 完成比较和锁定。"
            if len(selected) > 1:
                return "[候选事件未闭环] 当前存在多个 selected 候选，先完成区分性比较。"
            candidate_id, candidate = selected[0]
            candidate_value = str(candidate.get("value") or "").strip()
            # Empty/invalid finalize payload is exactly the recovery case: use the
            # canonical selected value rather than narrative/question/Vault fallbacks.
            if _need_extract:
                text = "答案: %s" % candidate_value
            normalized_text = _qes_final._normalize_value(text)
            for prefix in ("答案:", "answer:"):
                if normalized_text.startswith(prefix):
                    normalized_text = normalized_text[len(prefix):].strip()
            if normalized_text != _qes_final._normalize_value(candidate_value):
                return "[候选事件未闭环] 交付文本与 selected 候选不一致，先对齐候选后再交付。"
            evidence_ids = list(candidate.get("evidence_ids") or [])
            if not evidence_ids:
                return "[候选事件未闭环] selected 候选没有可追溯证据，先补齐证据引用。"
            if candidate.get("contradicting_evidence_ids") or candidate.get("unresolved_constraints"):
                return "[候选事件未闭环] selected 候选仍有反证或未决约束，先完成核验。"
            proposed = store.append("answer_proposed", {
                "answer": candidate_value, "candidate_id": candidate_id})
            verified = store.append("answer_verified", {
                "passed": True, "candidate_id": candidate_id,
                "evidence_ids": evidence_ids}, caused_by=proposed.seq)
            delivered = store.append("answer_delivered", {
                "answer": candidate_value, "candidate_id": candidate_id}, caused_by=verified.seq)
            session["_event_cursor"] = delivered.seq
            _qes_final.RunState(store).project_legacy_views(session)
    except Exception as exc:
        session.setdefault("nonfatal_failures", []).append({
            "component": "qa_event_finalizer", "exception_type": type(exc).__name__,
            "detail": str(exc)[:200]})
        if str(session.get("_event_log_path") or ""):
            return "[候选事件未闭环] canonical 交付失败: %s" % str(exc)[:160]
    session["final"] = text
    return f"交付完成，{len(text)} 字"


# ════════════════════════════════════════════════════════════════════
# v15 P0 · AgentIR 思想: 推理感知检索
# ════════════════════════════════════════════════════════════════════

@register_tool(
    name="predict_then_search",
    description=(
        "v15 P0 · AgentIR 思想: **搜前先推理,精确查实体不靠关键词**。"
        "用法: question 直接传完整题目。内部: ①1 次轻量 LLM (longcat-2.0) 抽出 5-10 个候选实体名; "
        "②对每个实体直接调 wikipedia API (避免走 run_parallel_search 的关键词噪声); "
        "③每个实体再调 wikidata API 拿结构化描述 + ID; "
        "④返回结构化 [(entity, snippet, wiki_url, wikidata_id, wikidata_desc)]。"
        "为什么不用 run_parallel_search: 关键词搜带回 90% 噪声,实体重名撞"
        "(BrowseComp 多跳推理的核心痛点)。此工具每实体 1 次 API=5-10 次小 GET,"
        "比一次并行搜索便宜 5-10 倍。"
        "适用: 多跳实体链推理(BrowseComp 类)·同名实体消歧·事实查询。"
    ),
    category="search", cost_tier="cheap", retryable=False,
)
def t_predict_then_search(question, session):
    """v15 P0 · 推理感知检索."""
    if not (question or "").strip():
        return "错误: question 必填"

    def _llm_pick_entities(q):
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import multi_model as _mm
            r = _mm.ask("longcat-2.0",
                        "Extract 5-10 distinct named entities (people/places/orgs/songs/companies/events) "
                        "from the question. Return ONLY a comma-separated list, no other text.\n\n"
                        "Question: " + q[:1500],
                        timeout=45, max_tokens=200)
            if r.get("ok"):
                raw = re.split(r"[,;\n]+", r["out"])
                out = []
                seen = set()
                for x in raw:
                    x = re.sub(r"^[\s\-\*\d\.\)]+", "", x).strip().strip('"').strip("'")
                    if x and 2 <= len(x) <= 80 and x.lower() not in seen:
                        out.append(x)
                        seen.add(x.lower())
                return out[:8]
        except Exception:
            pass
        return []

    def _wiki_search_one(entity):
        try:
            params = urllib.parse.urlencode({
                "action": "query", "list": "search", "srsearch": entity[:100],
                "srlimit": 1, "format": "json",
            })
            req = urllib.request.Request(
                f"https://en.wikipedia.org/w/api.php?{params}",
                headers={"User-Agent": "auto_agent/1.0 (research)"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                d = json.loads(resp.read().decode())
            hits = (d.get("query") or {}).get("search") or []
            if hits:
                title = hits[0].get("title", "")
                snippet = re.sub(r"<[^>]+>", "", hits[0].get("snippet", ""))[:160]
                url = f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"
                return {"wiki_title": title, "wiki_snippet": snippet, "wiki_url": url}
        except Exception:
            pass
        return {}

    def _wikidata_search_one(entity):
        try:
            params = urllib.parse.urlencode({
                "action": "wbsearchentities", "search": entity[:100],
                "language": "en", "limit": 1, "format": "json",
            })
            req = urllib.request.Request(
                f"https://www.wikidata.org/w/api.php?{params}",
                headers={"User-Agent": "auto_agent/1.0"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                d = json.loads(resp.read().decode())
            es = d.get("search") or []
            if es:
                return {"qid": es[0].get("id", ""),
                        "label": es[0].get("label", ""),
                        "desc": es[0].get("description", "")[:140]}
        except Exception:
            pass
        return {}

    entities = _llm_pick_entities(question)
    if not entities:
        return "无可抽实体·请改用 run_parallel_search"

    results = []
    for ent in entities:
        ws = _wiki_search_one(ent)
        wd = _wikidata_search_one(ent)
        row = {"entity": ent}
        row.update({f"wiki_{k}": v for k, v in ws.items()} if ws else {})
        row.update({f"wikidata_{k}": v for k, v in wd.items()} if wd else {})
        results.append(row)

    evidence = session.setdefault("evidence", [])
    for r in results:
        url = r.get("wiki_url") or ""
        if url:
            evidence.append({
                "url": url,
                "title": r.get("wiki_title") or r.get("entity", ""),
                "content": f"[predict_then_search] {r['entity']}: "
                           f"{r.get('wiki_snippet') or r.get('wikidata_desc') or ''}",
                "source": "predict_then_search",
            })

    lines = ["[predict_then_search · AgentIR · 推理感知检索]"]
    lines.append(f"  从题面抽出 {len(entities)} 个候选实体")
    lines.append("")
    for r in results:
        wd = f" | WD={r.get('wikidata_qid','')}({r.get('wikidata_desc','')[:50]})" if r.get("wikidata_qid") else ""
        ws = f" | {r.get('wiki_title','')}" if r.get("wiki_title") else ""
        snippet = (r.get("wiki_snippet") or r.get("wikidata_desc") or "")[:100]
        lines.append(f"• ENTITY={r['entity']}{ws}{wd}")
        lines.append(f"  SNIPPET={snippet!r}")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════
# v15 P0 · V→E→P→E 思想: finalize 前自检
# ════════════════════════════════════════════════════════════════════

@register_tool(
    name="self_calibrate",
    description=(
        "v15 P0 · Validate→Explore→Plan→Execute 思想: **在 finalize 前自检**。"
        "用法: question 传完整题,系统自动从 session.evidence 读已有证据。"
        "内部: 1 次 longcat-2.0 抽 3-6 个 sub-claims(题面硬约束),对每个 claim 把所有 evidence "
        "做 map-supported/unsupported/conflict 三分类。"
        "返回结构化 + 总体 coverage_score (0-1),并列出 next_search_claim (未覆盖的 claim)。"
        "推荐时机: 准备 finalize_answer 之前调一次,若 coverage≥0.7 才真 finalize,"
        "否则按 next_search 继续搜。"
    ),
    category="synthesis", cost_tier="cheap", retryable=False,
)
def t_self_calibrate(question, session):
    if not (question or "").strip():
        return "错误: question 必填"
    evidence = session.get("evidence") or []
    ev_text = ""
    for i, e in enumerate(evidence[:30]):
        ev_text += f"\n[E{i}] {e.get('title','')[:60]} :: {(e.get('content','') or '')[:200]}"
    if not ev_text:
        ev_text = "\n(尚无 evidence)"

    prompt = (
        "你是答案核对员。请把 question 拆成 3-6 个具体 sub-claims(题面里的硬事实/约束),"
        "然后对每个 claim 检查下面 evidence 列表,标 supported/unsupported/conflict,"
        "最后给个 coverage_score (0-1)。\n\n"
        # ★通用洞察(2026-08-20·用户总纲:信息搜索=严格关系推理+信息计算·不足则推动准确信息):\n
        #   补最缺的缺口时·手段不止'再搜一次'。判断补 next_search_claim 的【最佳手段类型】:\n
        #   - search: 靠检索能拿到的事实(人物/事件/属性·搜引擎/结构库有)\n
        #   - compute: 这个缺口【可被程序精确计算/求解】而搜网页给不出准值——如两地距离/路线、\n
        #     半径内是否有某设施、时间跨度计算、单位换算、异构结构化数据(地图/表格/坐标)解析、\n
        #     集合/关系运算。这类应【用 shell_exec 造工具调 API 或写 python 精确算】·不是撒网搜。\n
        #   - both: 先搜出关键实体/坐标·再计算验证。\n
        "判断补 next_search_claim 的最佳手段 next_step_kind: search / compute / both"
        "(compute=该缺口可被程序精确计算而搜网页给不出准值·如距离/路线/半径内设施/时间跨度"
        "/单位换算/异构数据解析/集合运算)。\n\n"
        "输出严格 JSON 格式:\n"
        '{"claims":[{"claim":"...","status":"supported|unsupported|conflict",'
        '"evidence_ids":["E0"]}],'
        '"coverage_score":0.0,"next_search_claim":"最缺什么","next_step_kind":"search|compute|both"}\n\n'
        f"Question: {question[:1200]}\n\nEvidence (E0..EN):{ev_text}"
    )

    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import multi_model as _mm
        r = _mm.ask("longcat-2.0", prompt, timeout=60, max_tokens=600)
        if not r.get("ok"):
            return f"[self_calibrate] llm 失败:{r.get('err','')[:120]}"
        out = r["out"]
        m = re.search(r"\{[\s\S]+\}", out)
        if m:
            try:
                data = json.loads(m.group(0))
                cs = float(data.get("coverage_score", 0))
                lines = [f"[self_calibrate · V→E→P→E 验证] coverage={cs:.2f}"]
                lines.append(f"  next_search: {data.get('next_search_claim','(none)')[:120]}")
                # ★通用:缺口若可计算/需处理异构信息→提示造算子(不是撒网搜)。判据是缺口性质
                #   (LLM 判的 next_step_kind)·非题面关键词=对任何'可程序求解'的题通用·非地理特判。
                _nsk = str(data.get("next_step_kind", "search") or "search").lower()
                if "compute" in _nsk or "both" in _nsk:
                    lines.append(
                        "  ⚙️[缺口可计算·造算子推进] 上面这个缺口【可被程序精确求解】而搜网页给不出准值——"
                        "别再撒网搜。用 shell_exec 当场造工具:写 python 调开放 API 或直接计算"
                        "(距离/路线→OSRM;半径内设施→Overpass around;地名→坐标→photon/Nominatim;"
                        "时间跨度/单位换算/集合运算→直接 python)。把算出的结构化事实喂回推理链再推进。"
                        "这是关系计算·不是搜索——信息搜索本质是'不足就推动准确信息'的严格推理过程。")
                for i, c in enumerate(data.get("claims", [])[:8]):
                    ev_ids = ",".join(c.get("evidence_ids", [])[:5])
                    lines.append(f"  C{i}[{c.get('status','?')}] {c.get('claim','')[:80]} (E:{ev_ids})")
                session["_self_calibrate_result"] = data
                return "\n".join(lines)
            except Exception:
                pass
        session["_self_calibrate_result"] = {"raw": out[:500]}
        return f"[self_calibrate] raw: {out[:600]}"
    except Exception as e:
        return f"[self_calibrate] 异常: {type(e).__name__}: {str(e)[:120]}"


# ════════════════════════════════════════════════════════════════════
# v15 P1 · WebSailor 思想: 搜到满意自动停
# ════════════════════════════════════════════════════════════════════

@register_tool(
    name="search_until_satisfied",
    description=(
        "v15 P1 · WebSailor 思想: **搜到满意就停**。"
        "用法: question 传完整题,系统读 session.evidence。"
        "内部: 1 次 longcat-2.0 评估当前 evidence 是否『anchor 充分」(是否锁定主题/约束已大部分满足)。"
        "返回 {sufficient: bool, confidence: 0-1, missing_dims: [建议继续搜的维度]}"
        "适用: 跑题快撞 480s 硬墙前调一次——sufficient=True 可直接 finalize;"
        "sufficient=False 时按 missing_dims 提示继续(给 agent 明确下一跳方向。"
    ),
    category="synthesis", cost_tier="cheap", retryable=False,
)
def t_search_until_satisfied(question, session):
    if not (question or "").strip():
        return "错误: question 必填"
    evidence = session.get("evidence") or []
    ev_text = ""
    for i, e in enumerate(evidence[:25]):
        ev_text += f"\n[E{i}] {e.get('title','')[:60]} :: {(e.get('content','') or '')[:180]}"
    if not ev_text:
        ev_text = "\n(尚无 evidence·无法判断充分性)"

    prompt = (
        "请基于现有 evidence 判断『对解答这道题是否已足够』。"
        "判断标准: ①核心主题/实体是否锁定 (有候选 wiki_url/qid/desc); "
        "②关键约束(年代/地点/类别/官方名等)是否已 satisfied ≥70%; "
        "③如有冲突证据是否已分辨主候选与排除候选。"
        '输出严格 JSON: {sufficient: bool, confidence: 0-1, missing_dims: [\"n 维度\"],'
        "                    primary_candidate: \"目前最有力的候选实体/答案\","
        "                    reasoning: 1-2 句解释}\n\n"
        f"Question: {question[:1200]}\n\nEvidence:{ev_text}"
    )
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import multi_model as _mm
        r = _mm.ask("longcat-2.0", prompt, timeout=60, max_tokens=500)
        if not r.get("ok"):
            return f"[search_until_satisfied] llm 失败:{r.get('err','')[:120]}"
        out = r["out"]
        m = re.search(r"\{[\s\S]+\}", out)
        if m:
            try:
                data = json.loads(m.group(0))
                sf = bool(data.get("sufficient", False))
                cf = float(data.get("confidence", 0))
                pc = data.get("primary_candidate", "")
                rs = data.get("reasoning", "")[:120]
                md = data.get("missing_dims", []) or []
                lines = [f"[search_until_satisfied · WebSailor] sufficient={sf} confidence={cf:.2f}"]
                lines.append(f"  primary_candidate: {pc[:120]}")
                if md and not sf:
                    lines.append(f"  missing_dims: {md[:5]}")
                lines.append(f"  reasoning: {rs}")
                session["_search_until_satisfied_result"] = data
                return "\n".join(lines)
            except Exception:
                pass
        session["_search_until_satisfied_result"] = {"raw": out[:500]}
        return f"[search_until_satisfied] raw: {out[:500]}"
    except Exception as e:
        return f"[search_until_satisfied] 异常: {type(e).__name__}: {str(e)[:120]}"


# ════════════════════════════════════════════════════════════════════
# v15 P1 · Context-Folding 思想: 主动折叠长 narrative
# ════════════════════════════════════════════════════════════════════

@register_tool(
    name="fold_context",
    description=(
        "v15 P1 · Context-Folding 思想: **主动压缩 narrative 释放 context 空间**。"
        "用法: question 可不传(纯折叠内部状态)。"
        "内部: 读 session.narrative + evidence·1 次 longcat-2.0 做 summary-fold(保留关键事实+已确认锚+未覆盖约束)。"
        "压缩后 narrative ≤ 原文 1/3·且必须保留所有数字/日期/ID/候选实体名(不可改写)。"
        "安全: 压缩后反而变长则回滚·不改写。"
        "适用: agent 跑了 5+ 步后 narrative 接近撞 token 预算时主动调(每 5-8 步 fold 1 次)。"
    ),
    category="synthesis", cost_tier="cheap", retryable=False,
)
def t_fold_context(question, session):
    narrative = (session.get("narrative") or "").strip()
    if len(narrative) < 800:
        return f"[fold_context] narrative 长度 {len(narrative)} < 800·折叠无必要·跳过"
    evidence = session.get("evidence") or []
    ev_summary = ""
    for i, e in enumerate(evidence[:30]):
        ev_summary += f"\n[E{i}] {e.get('title','')[:50]} :: {(e.get('content','') or '')[:160]}"

    prompt = (
        "你是 narrative compressor。把下面的 narrative + evidence 压缩,目标是 narrative ≤原文 1/3。"
        "硬规则(违反任一就等于失败): ①所有数字/日期/年份/绝对值须原样保留; "
        "②所有候选实体名/wiki_url/wikidata_id 须原样保留; "
        "③已确认的 anchor 和未覆盖的 constraint 必须明确列出; "
        "④不得修改任何被引用的 evidence 原文。"
        "输出: 折叠后的 narrative + 一段『anchor 摘要』+ 一段『uncovered 约束』 + 一段『next_step 建议』。\n\n"
        f"Original narrative ({len(narrative)} chars):\n{narrative[:6000]}\n\n"
        f"Evidence 摘要:{ev_summary}"
    )
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import multi_model as _mm
        r = _mm.ask("longcat-2.0", prompt, timeout=80, max_tokens=2500)
        if not r.get("ok"):
            return f"[fold_context] llm 失败:{r.get('err','')[:120]}"
        out = r["out"]
        if len(out) > len(narrative) * 0.95:
            # 压缩失败(没省到 ≥5%)·回滚不改
            return (f"[fold_context] 失败: 压缩后 {len(out)} 字符 ≥原文 95%({len(narrative)})"
                    f"·跳过不修改 narrative")
        # 压缩成功·记录到 session,不直接替换 narrative(由 agent 显式调 finalize 时再决定用哪个)
        session["_folded_narrative"] = out
        session["_fold_stats"] = {
            "n_chars_before": len(narrative),
            "n_chars_after": len(out),
            "ratio": round(len(out) / max(len(narrative), 1), 3),
            "ts": time.time(),
        }
        return (f"[fold_context · 折叠成功] {len(narrative)}→{len(out)} 字符 "
                f"(ratio={len(out)/max(len(narrative),1):.2f}) 已缓存 _folded_narrative·"
                f"agent 调用 finalize 时可考虑用压缩版(原文保留)")
    except Exception as e:
        return f"[fold_context] 异常: {type(e).__name__}: {str(e)[:120]}"
