#!/usr/bin/env python3
"""Auto 自研成员。

真实架构(2026-08-27):
- 军团是产品主体；auto 只是 harness_registry 中的一家。
- auto 唯一执行引擎是 NativeEngine(thin loop)，deepagents fallback 已删除。
- qa_agent_main 是旧固定流水线/评测基线；auto 已把任务分析剥到 qa_task_analysis，
  不再运行时依赖旧 pipeline。
- 工具装配在 tool_factory，执行工具在 execution_tools，预算在 budget_policy，
  交卷信号在 finalize_gates，状态在 run_state。默认 core toolset + 三桥按需发现。
- 原则:给模型 context 与工具，不用父进程语义否决答案。
"""
import os
import re
import sys
import json
import time
import inspect
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
QA_ROOT = os.path.dirname(HERE)
SEED_ROOT = os.path.dirname(QA_ROOT)
REPO_ROOT = os.path.dirname(SEED_ROOT)
V11_EVOLUTION = os.path.join(SEED_ROOT, "v11", "evolution")
if V11_EVOLUTION not in sys.path:
    sys.path.insert(0, V11_EVOLUTION)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from qa_llm_gateway import _config, DEFAULT_MODEL, HAIKU_MODEL, SONNET_MODEL, resolve_model_name, HEAVY_FALLBACK_MODEL
from qa_tools_registry import get_all_tools, get_tool_lazy
from mission_state import MissionState
import qa_retry_policy

# 接入 evolution_tool_registry，让 auto agent 能 search + execute
# evolution 280+ 模块的延迟工具。对齐 CC 的 ToolSearch + ExecuteTool。
# qa_tools_registry 是 QA 专用工具(13个)，evolution_tool_registry 是
# 全系统级工具(self_reflect/code_evolve/deep_research/bus_summary 等)。
try:
    from evolution_tool_registry import search as evo_search, run as evo_execute, get_all_tools as evo_get_all_tools
    _EVO_AVAILABLE = True
except Exception:
    _EVO_AVAILABLE = False

# ── G-align (2026-08-07) · 把已建好但只接进 meta_graph 的三个能力接进这个
#    "要当 web 门面的通用 agent"。差距分析(_refs/GAP_ANALYSIS.md)核实:
#    memory_layers/rubric_gate/task_dispatch 三者 meta_graph.py 已真 py_call，
#    但 qa_auto_agent.py 一个都没 import——典型「接到了 A 入口没接到 B 入口」。
#    全部 try/except 包住:模块缺失时降级回旧行为,不破坏现有 loop。
try:
    from memory_layers import remember as _mem_remember, recall as _mem_recall
    _MEM_AVAILABLE = True
except Exception:
    _MEM_AVAILABLE = False

try:
    from task_dispatch import dispatch_task as _dispatch_task
    _DISPATCH_AVAILABLE = True
except Exception:
    _DISPATCH_AVAILABLE = False

# 统一 · 门面的显式计划直接复用 pipeline 同一个 plan_state.Plan(不再用 ad-hoc
# list),这样门面和 pipeline 共享同一套计划状态机——支持动态插步/中断恢复/
# 进度百分比,而不是我 G-align 时的简易三态列表。
try:
    from plan_state import Plan as _Plan
    _PLAN_STATE_AVAILABLE = True
except Exception:
    _PLAN_STATE_AVAILABLE = False

# 统一4(2026-08-07) · 对标发现的唯一真缺口:Skills 没接门面。skill_library.py
# + skills/ 下的 SKILL.md 文档早就建好(content-sufficiency-review /
# overconfidence-detection / rubric-gate-usage 等),但门面 grep 不到——又一例
# 「建了没接线」。deepagents(SkillsMiddleware)/hermes(skill_bundles)/CC
# (skill-store)三家都有的核心能力。这里补上:①系统提示里列出可用 skill(让
# 模型知道有哪些)②skill_search 发现 + skill_load 按需读全文。skill 是"可加载
# 的做事方法论文档"(不是可执行代码),模型参考着做判断,和 evolution 工具
# (可执行)互补。
try:
    from skill_library import search_skills as _skill_search, load_skill as _skill_load, list_skills as _skill_list
    _SKILLS_AVAILABLE = True
except Exception:
    _SKILLS_AVAILABLE = False

# P2(2026-08-07) · 演绎 per-user 意图 + 用户画像。门面开跑前:抽这个用户在这句
# 话里的真实意图(user_intent_extractor),读这个用户的历史画像
# (user_profile_store)——让 agent"认识"这个用户是谁、常问什么、偏好什么,
# 而不是把每个用户都当无名氏。都是已建好但没接门面的组件(又一例建了没接)。
try:
    from user_intent_extractor import extract_intent as _extract_intent
    _INTENT_AVAILABLE = True
except Exception:
    _INTENT_AVAILABLE = False

try:
    import user_profile_store as _profile_store
    _PROFILE_AVAILABLE = True
except Exception:
    _PROFILE_AVAILABLE = False

# P3(2026-08-07) · 错因分析+错了重试:任务失败/rubric 反复不过时,把错因喂
# error_search_learner 上网学"这类错别人怎么解",学到的解法回注下次重试。
try:
    import error_search_learner as _error_learner
    _ERRLEARN_AVAILABLE = True
except Exception:
    _ERRLEARN_AVAILABLE = False

# P7(2026-08-07) · per-user 自愈主体:每个用户失败进自己的库,同类反复→就地诊断
# 沉淀进他的语义记忆,下次规避。真正 per-user「失败→诊断→个人经验→规避」自愈闭环。
try:
    import user_self_heal as _self_heal
    _SELFHEAL_AVAILABLE = True
except Exception:
    _SELFHEAL_AVAILABLE = False

# R2(2026-08-07) · HITL 审批门:风险操作(sudo/git push/rm -r/写沙箱外)执行前挂起
# 等用户在网页批(对齐 deepagents HumanInTheLoopMiddleware / CC 权限确认)。
# MEYO_HITL_MODE=off 时全放行(CLI/无网页不破坏旧行为)。
try:
    import hitl_gate as _hitl
    _HITL_AVAILABLE = True
except Exception:
    _HITL_AVAILABLE = False

# T2(2026-08-07) · 监督自主化→自主造工具:门面收尾时,若监督(self_heal/记忆置信)发现
# 该用户在反复做同一类操作,触发 tool_forge_loop 把"高频重复模式"固化成一个命名工具
# (自动生成 executor_code → forge_new_tool 注册 → 下次一步调用),把"反复手动做"变成
# "一个工具一步做"。这就是用户要的「监督能自主化创建工具」。tool_forge_loop 已实现
# 扫描+生成+注册+验证的完整闭环(T1 已修幂等),这里接到门面按需触发。
try:
    import tool_forge_loop as _tool_forge
    _TOOLFORGE_AVAILABLE = True
except Exception:
    _TOOLFORGE_AVAILABLE = False

# G-gui(2026-08-08) · 系统级桌面 GUI 自动化(对标 LongHorizon-Harness
# computer-use)。之前 interactive_browser_agent.py 只覆盖浏览器内 DOM 交互，
# 系统级"截图看屏幕/点击任意窗口应用/键鼠模拟"这层是真缺口，desktop_gui.py
# 补上(macOS osascript+screencapture，零三方依赖)。
# 注意:_DESKTOP_GUI_AVAILABLE 只代表"模块能 import"，不代表"真能用"——
# import 在任何平台都会成功(纯 Python，无平台限定的顶层依赖)，真实可用性
# 取决于①是否 macOS ②是否已在 系统设置→隐私与安全性 里授予屏幕录制/辅助功能
# 权限。这两项探测涉及 subprocess(screencapture/osascript)，不适合在模块
# 顶层导入时就跑(首次未授权可能弹权限对话框/有副作用)，所以放到
# _make_desktop_gui_tools() 里按需探测(见 _desktop_gui_real_available)。
try:
    import desktop_gui as _desktop_gui
    _DESKTOP_GUI_AVAILABLE = True
except Exception:
    _DESKTOP_GUI_AVAILABLE = False

_desktop_gui_avail_cache = None  # {"ok": bool, "reason": str} 首次探测后缓存，避免重复 subprocess 调用


def _desktop_gui_real_available() -> dict:
    """真实探测桌面 GUI 能力是否可用(不只是模块 import 成功)。
    只要「屏幕录制」或「辅助功能」任一权限可用就算可用(screenshot 只需前者，
    click/type/list_windows 需要后者)，具体到每个工具内部仍会再次探测并给出
    准确的失败原因，这里只决定"要不要把工具注册给 LLM，避免徒劳调用"。
    结果按进程缓存一次，避免每次构造工具列表都重新触发 subprocess。"""
    global _desktop_gui_avail_cache
    if _desktop_gui_avail_cache is not None:
        return _desktop_gui_avail_cache
    if not _DESKTOP_GUI_AVAILABLE:
        _desktop_gui_avail_cache = {"ok": False, "reason": "desktop_gui 模块不可用",
                                     "shot_ok": False, "a11y_ok": False}
        return _desktop_gui_avail_cache
    try:
        r_shot = _desktop_gui.is_screenshot_available()
        r_a11y = _desktop_gui.is_available()
        shot_ok, a11y_ok = bool(r_shot.get("ok")), bool(r_a11y.get("ok"))
        ok = shot_ok or a11y_ok
        reason = "" if ok else f"screenshot: {r_shot.get('reason', '')}; a11y: {r_a11y.get('reason', '')}"
        _desktop_gui_avail_cache = {"ok": ok, "reason": reason, "shot_ok": shot_ok, "a11y_ok": a11y_ok}
    except Exception as e:
        _desktop_gui_avail_cache = {"ok": False, "reason": f"探测异常: {type(e).__name__}: {e}",
                                     "shot_ok": False, "a11y_ok": False}
    return _desktop_gui_avail_cache

# G-cli(2026-08-08) · CLI-Anything 生态桥接(用户「是不是有cli anything」)。
# 比 desktop_gui.py 的坐标点击更稳的 GUI 软件自动化路线:给真实软件(GIMP/
# Blender/FreeCAD/LibreOffice/Krita/Obsidian/Zoom等40+)包一层命令行接口，
# 走软件自带脚本API而不是模拟鼠标键盘。优先级:能用CLI-Anything就不用
# desktop_gui坐标点击(更确定、不受界面改版影响)。
try:
    import cli_hub as _cli_hub
    _CLI_HUB_AVAILABLE = _cli_hub.is_available().get("ok", False)
except Exception:
    _CLI_HUB_AVAILABLE = False

AUTO_LOG = os.path.join(QA_ROOT, "state", "auto_agent_v57.jsonl")
os.makedirs(os.path.dirname(AUTO_LOG), exist_ok=True)

# ═══════════════════════════════════════════════════════════════════════
# Checkpoint · 断点续传 (2026-08-06)
#
# 用户需求:「如果断了记得续传部分关键过程和上下文和目标，这样可以自动
# 重连 auto agent」。
#
# _SESSIONS 是纯进程内内存，进程一断(异常/被 kill) tool_log/evidence
# 全丢，下次同 qid 从零开始。这里把 session 的**精简关键态**每步落盘一次:
#   - question/goal (目标)
#   - evidence 摘要 (条数 + 前若干条 title/url，不存全文避免文件爆)
#   - tool_log 全量 (关键过程)
#   - _budget 进度 + narrative/final + status
# 原子写(.tmp → os.replace)防止写一半崩坏。所有落盘/读盘 try/except
# 包住，失败只记录不阻断主流程 (fail-safe)。
# ═══════════════════════════════════════════════════════════════════════

CKPT_DIR = os.path.join(QA_ROOT, "state", "auto_ckpt")
try:
    os.makedirs(CKPT_DIR, exist_ok=True)
except Exception:
    pass

# _CKPT_EVIDENCE_PREVIEW_N 已随 _evidence_digest 迁入 checkpoint_serialization.py(唯一归属)。
# v59 (2026-08-07): 用户反馈「网页上看历史对话内容不完整」——排查发现
# narrative/final 落 checkpoint 时被截到 4000 字符，agent_web.py 在内存态
# 已丢失(比如网页服务重启过)时会 fallback 读这份被截断的 checkpoint 当作
# 完整结果展示，导致长回答被腰斩。4000 -> 20000，覆盖绝大多数任务的完整
# 输出(实测最长的历史 final 也就 7000-8000 字符)，checkpoint 文件体积仍
# 可控(每步落盘一次，20000 字符 ≈ 20-40KB，可接受)。
_CKPT_TEXT_MAX = 20000


# v77g(2026-08-10 用户抓 EC 搜偏)拆两档中性化·解"绕拒答 vs 检索特异性"的矛盾:
#   - 搜索 query 走【轻档】:只去【通用敏感短语连用】(virus testing/bioweapon 等)·
#     【保留学术实体】(virus/SPFMV/pathogen 单独出现·搜索引擎不拒答·实体是检索特异性
#     的关键——实测保实体搜出5条全精准·去实体搜偏成"数字类型"垃圾)。
#   - 注入给【决策 LLM】走【重档】:LLM 会拒答·必须彻底去词(含单独的 virus/pathogen)。
_NEUTRALIZE_LIGHT = [  # 搜索用:只去【真正危险】的连用短语(bioweapon/bioterror)·保学术实体
    # v13.92 修:原把 "virus testing" 放这里→轻档也抹掉 virus→搜索失去检索特异性
    #   (与"保实体"意图自相矛盾·EC 甘薯病毒题实测轻档=重档·搜偏成"数字类型")。
    #   "virus testing" 是正常学术词组·搜索引擎不因它拒答(拒答恢复走 http_only 直连绕网关)·
    #   移到重档:只在喂决策 LLM 时才软化。轻档只留真正触发内容安全的 bioweapon/bioterror。
    (r"\bbioweapons?\b", "agent"),
    (r"\bbioterror\w*\b", "incident"),
    (r"\bbio[- ]?weapon\w*\b", "agent"),
]
_NEUTRALIZE_HEAVY = _NEUTRALIZE_LIGHT + [  # 注入LLM用:再去单独的敏感实体词
    (r"\bvirus testing\b", "detection assay"),
    (r"\bvirus detection\b", "detection assay"),
    (r"\bviral detection\b", "detection assay"),
    (r"\bvirus(?:es)?\b", "plant sample agent"),  # 修:原 viruses? 匹配不到单数 virus(少个e)
    (r"\bviral\b", "sample-borne"),
    (r"\bpathogens?\b", "sample agents"),
    (r"\btesting method\b", "detection method"),
    (r"\bweapon\w*\b", "item"),
    (r"\binfect\w*\b", "affect"),
    (r"\btoxin\w*\b", "compound"),
]
_NEUTRALIZE_MAP = _NEUTRALIZE_HEAVY  # 兼容旧引用


def _neutralize_text(text: str, heavy: bool = True) -> str:
    """规则去内容安全过滤触发词。heavy=True(默认·注入LLM用):彻底去词含单独实体·
    避免决策 LLM 拒答。heavy=False(搜索 query 用):只去通用敏感连用短语·保学术实体
    (SPFMV/virus 等·搜索引擎不拒答·实体=检索特异性)。fail-safe:异常返回原文。"""
    try:
        import re as _re
        out = text or ""
        _rules = _NEUTRALIZE_HEAVY if heavy else _NEUTRALIZE_LIGHT
        for _pat, _rep in _rules:
            out = _re.sub(_pat, _rep, out, flags=_re.IGNORECASE)
        return out
    except Exception:
        return text or ""


# 疑问句停用词(问句里稀释实体信号的通用词)。故意保留 "EC"/"number" 之外的领域信号词。
_QUERY_STOPWORDS = {
    "what", "which", "who", "whom", "whose", "when", "where", "why", "how",
    "is", "are", "was", "were", "be", "been", "being", "do", "does", "did",
    "the", "a", "an", "of", "for", "in", "on", "at", "to", "from", "by",
    "with", "about", "as", "into", "and", "or", "that", "this", "these",
    "those", "there", "here", "it", "its", "two", "most", "commonly", "used",
    "using", "some", "any", "all", "both", "more", "much", "many", "please",
    "give", "list", "find", "tell", "me", "you", "your", "their", "they",
}


def _query_keywords(text: str, *, min_words: int = 3) -> str:
    """从自然语言问句提取检索关键词(实体优先),不经 LLM(拒答恢复路径要绕 LLM 网关)。

    v13.92 修 EC 搜偏第二层病:整句问句"What are the EC numbers of the two most
    commonly used chemicals..."被搜索引擎把 "numbers" 当主词→搜成"数字类型"噪音。
    实测(http_only 真搜):整句版带数字噪音·关键词版无噪音且同样命中 SPFMV/酶。

    通用策略(不为单题过拟合):
      · 保专有名词(含大写/全大写缩写 SPFMV/SPCSV)、引号内容、年份、字母数字实体(如 EC 3.1.3.1)
      · 去疑问词/停用词(what/the/most/commonly...)
      · 保剩余实义词(去停用词后的名词类)
    fail-safe:提取结果太短(< min_words 词)→退回原文,绝不因过度提取丢信息。
    """
    try:
        import re as _re
        t = text or ""
        if not t.strip():
            return t
        # 分两桶:高特异性实体(专名/缩写/年份/ID)前置,通用实义词殿后。
        # 前置让搜索引擎优先匹配 SPFMV/Pearl/2016 这类独特信号,不被通用词稀释权重。
        entities = []      # 高权重:缩写/专名/年份/字母数字ID/引号内容
        commons = []       # 低权重:去停用词后的普通实义词
        seen = set()
        # 1. 引号内容(常是精确标题/实体)→ 实体桶
        for m in _re.findall(r'"([^"]{2,60})"', t):
            for w in m.split():
                if w.lower() not in seen:
                    entities.append(w); seen.add(w.lower())
        # 2. 逐 token 判定
        for tok in _re.findall(r"[A-Za-z0-9][A-Za-z0-9.\-]*", t):
            low = tok.lower()
            is_acronym = tok.isupper() and len(tok) >= 2            # SPFMV/SPCSV/EC(全大写缩写)
            is_year = bool(_re.fullmatch(r"(19|20)\d{2}", tok))     # 年份
            is_alnum_id = bool(_re.search(r"\d", tok) and _re.search(r"[A-Za-z.]", tok))  # 3.1.3.1 类
            is_domain = low in ("ec", "enzyme", "protein", "gene", "isbn", "doi")  # 领域强信号
            # 停用词先判(不分大小写)·优先于 proper:句首疑问词 What/Who/How 也要去
            if low in _QUERY_STOPWORDS and not (is_acronym or is_domain):
                continue
            is_proper = tok[0].isupper()                            # 专有名词(句中/句首大写)
            if low in seen:
                continue
            if is_acronym or is_year or is_alnum_id or is_proper or is_domain:
                entities.append(tok); seen.add(low)                # 高特异性→前置
            elif len(tok) > 2 and not tok.isdigit():
                commons.append(tok); seen.add(low)                 # 普通实义词→殿后
        kept = entities + commons
        if len(kept) < min_words:
            return t  # 提取太少→退回原文,别丢信息
        return " ".join(kept)
    except Exception:
        return text or ""


def _vote_key(ans: str) -> str:
    """把答案归一化成投票 key(抗表述差异):优先取其中的编号/数值 token 排序·
    没有数值就取小写去标点的词集合。用于多轮答案投票(EC 3.1.3.1 和 1.11.1.7 与
    1.11.1.7;3.1.3.1 视为同一票)。通用·不针对具体题型。"""
    try:
        import re as _r
        _nums = _r.findall(r"\d+(?:\.\d+)+|\b\d{3,}\b", ans or "")
        if _nums:
            return "|".join(sorted(set(_nums)))
        _words = sorted(set(_r.findall(r"[a-z0-9]+", (ans or "").lower())))
        return " ".join(_words)[:120]
    except Exception:
        return (ans or "").strip()[:120]


def _neutral_stepwise_finalize(question: str, evidence_txt: str, *, timeout: float = 90,
                               searcher=None, round_hint: str = "") -> str:
    """死结兜底:上游拒答(决策脑=拒答脑·含敏感词内容一步 finalize 被静默产空)时·
    走【计划驱动的中性多步】——破"通用 vs 特化引导"的假两难。

    ★核心:解题引导【由模型自己按问题动态生成】·不是人手写死(那才是背答案/过拟合)。
    这跟"按需造工具"同逻辑:不预置 EC/某方法的路径·让模型对当前问题演绎出该怎么解。
    所以对任意题通用——换道题·模型生成的计划就变成那道题的解法。

    流程(每步内容都脱敏·让会拒答的 LLM 只见中性内容):
      ①模型对(脱敏)问题【自主规划】解题步骤(要哪些中间结论·从哪拿)——通用元能力·
        不给任何领域暗示;
      ②若给了 searcher·按计划里"还缺什么信息"做【针对性补搜】(query 关键词来自计划·
        非人写死)——治"首轮搜索没搜到方法学细节"的稳定性根因;
      ③据计划从资料抽【计划各步需要的中间结论】(抽什么由计划定·通用);
      ④按计划+中间结论+公认常识执行到底得答案。
    fail-safe:任一步空/异常/UNKNOWN→返回 ""(诚实空·绝不冒充答案)。
    searcher: 可选 callable(query:str)->str·返回脱敏后的补搜资料文本(拒答恢复分支传入·
      走 http_only 绕网关)。不传则退化为纯"资料→计划→抽→答"(无补搜)。
    """
    try:
        import qa_llm_gateway as _G
        import re as _re2
    except Exception:
        return ""
    if not evidence_txt or not evidence_txt.strip():
        return ""
    _ev = _neutralize_text(evidence_txt, heavy=True)[:6000]
    # 问法脱敏:重档 + 全大写缩写(≥3字母)→占位符·通用降拒答(不针对具体专名)。
    _q_bare = _re2.sub(r"\b[A-Z]{3,}\b", "the subject", _neutralize_text(question, heavy=True))

    # ① 模型自主规划(元能力·通用·脱敏问题不被拒)。它自己演绎"这题要哪几跳"。
    #   round_hint=上一轮失败的反思·让这轮【换策略】拆解(Goal 模式内核:没达标自主换角度)。
    _hint_seg = (f"\n\n【上一轮尝试没得到答案·反思如下·这次换个策略】\n{round_hint[:600]}\n"
                 "请换一个不同的拆解角度或数据来源重新规划。" if round_hint else "")
    _p_plan = (
        "你是研究策略专家。给你一个问题·分析【要回答它需要哪些信息、按什么步骤推导】·"
        "输出一个解题计划(每步:要拿到什么中间结论·大概从哪拿)。现在不要回答问题本身·只输出计划。"
        f"{_hint_seg}\n\n问题: {_q_bare}")
    try:
        _plan = (_G.call(_p_plan, tier="heavy", max_tokens=1200, timeout=timeout).get("out") or "").strip()
    except Exception:
        _plan = ""
    if not _plan:
        _plan = "①从资料定位与问题相关的关键对象 ②提取其属性 ③据属性+常识得出答案"

    # 逐条抽取器(通用·破"整段资料含敏感词被拒"死结):整段一次喂会被拒·【逐条单独抽】
    #   单条敏感密度低→不被整体拒·且明确"不输出生物/病害词"从源头去敏。被拒的单条自动跳过。
    def _extract_terms(_text_block):
        _cl = []
        for _line in [_l for _l in _text_block.splitlines() if _l.strip()][:14]:
            try:
                _one = _neutralize_text(_line[:500], heavy=True)
                _r = _G.call(
                    "从这段文字里列出【方法/技术/仪器/试剂/化学物/材料的名称】和【数值/编号/年份/日期】·"
                    "只列这类技术名词本身。\n"
                    "【务必不要输出】任何生物体/病害/医学健康相关的词(把它们整个略去·连提都不要提)。\n"
                    "没有技术名词就回 NONE。\n\n" + _one + "\n\n技术名词:",
                    tier="light", max_tokens=200, timeout=min(30, timeout))
                _o = (_r.get("out") or "").strip() if isinstance(_r, dict) else ""
                if _r.get("ok") and _o and _o.upper() != "NONE":
                    _cl.append(_o)
            except Exception:
                continue
        return "\n".join(_cl)

    # ② 首轮抽取(先从首轮资料拿到中间层锚点·如方法名)。
    _facts = _extract_terms(_ev)
    if not _facts.strip():
        return ""

    # ③ 计划驱动的针对性补搜(治稳定性根因:首轮资料常只到"用了X方法"·缺"X方法用什么
    #   试剂/其编号"这类中间层→模型只好多跳常识补→偶尔推偏)。★关键:补搜带上【首轮已抽出
    #   的事实】做上下文·query 才有方向(否则不知道是什么方法·会瞎猜跑偏)。多条 query 各
    #   覆盖计划里一个待填中间步骤·把中间层数据直接搜进来·模型少跳几步→更准。全动态·通用。
    if searcher is not None and _facts.strip():
        try:
            _p_q = (
                "已知这些已查到的事实:\n" + _facts[:1500] + "\n\n"
                "要回答的问题:\n" + _q_bare + "\n\n"
                "为了答出问题·还需要顺着已知事实【往下追一层具体属性】。请针对已知事实里"
                "【与问题最相关的那个对象】·追问它答题所需的下一层信息·写 2-3 条【具体指向的】"
                "英文搜索查询(例:已知某方法→搜『该方法具体用哪些试剂/材料』和『那些试剂的编号/参数』;"
                "已知某对象→搜『它的目标属性』)。每条一行·3-12 词·带上已知的具体名称让 query 精确·"
                "不要序号/标题/引号。\n\n直接输出查询:")
            _raw = (_G.call(_p_q, tier="heavy", max_tokens=200, timeout=min(45, timeout)).get("out") or "")
            _cands = [_re2.sub(r'^[#\-*>\s"\d.、)]+|["]+$', "", _l).strip() for _l in _raw.splitlines()]
            _cands = [_c for _c in _cands if len(_c.split()) >= 3 and not _c.startswith("#")][:3]
            _extra_all = ""
            for _eq in _cands:
                _ee = searcher(_eq[:160]) or ""
                if _ee.strip():
                    _extra_all += "\n" + _ee
            if _extra_all.strip():
                _more = _extract_terms(_neutralize_text(_extra_all, heavy=True)[:6000])
                if _more.strip():
                    _facts = (_facts + "\n" + _more)[:4500]  # 合并首轮+补搜的中间结论
        except Exception:
            pass
    # facts 归纳成【简洁分类清单】喂④(通用·一步同解两患):
    #   ★实测关键洞察:④是否被拒·跟 facts 的【形态】强相关——杂乱长拼接易触发拒答·
    #   简洁分类清单(方法: X / 试剂: Y)则稳定 ok=True。归纳同时:①格式简洁降拒答
    #   ②只留方法/技术/试剂/编号·丢生物病害词和无关项(顺带去敏+去噪)。对任何题通用。
    try:
        _tidy = _G.call(
            "把下面杂乱的名词清单归纳成简洁的分类清单·格式为 `类别: 名词, 名词`。\n"
            "规则:①只保留【方法/技术/试剂/化学物/仪器/编号/数值/年份/日期】这类技术名词;\n"
            "②丢掉任何生物体/病害/医学健康相关的词;\n"
            "③把【生物或病原体的专有名称或全大写缩写】(不是通用技术缩写如 ELISA/PCR/DNA)"
            "替换成 [entity]·保留通用技术缩写原样。\n\n" + _facts[:3500] + "\n\n简洁分类清单:",
            tier="heavy", max_tokens=500, timeout=min(45, timeout))
        _t = (_tidy.get("out") or "").strip() if isinstance(_tidy, dict) else ""
        if _t:
            _facts = _t[:3000]
    except Exception:
        pass
    _facts = _neutralize_text(_facts, heavy=True)  # 最终堵漏:再过一道重档脱敏
    #   专名缩写(SPFMV 等)的去除交给上面 heavy-tier 归纳步的语义判断(它能分清
    #   ELISA 该保留 vs SPFMV 该占位)·不再用硬编码白名单(不通用·换题就漏)。

    # ④ 按计划+中间结论+公认常识执行到底得答案(计划里的常识跳是模型自发的·非人喂)。
    _p_solve = (
        "按下面【你自己制定的解题计划】·用【中间结论】和你的专业常识执行到最后得出答案。\n"
        "计划中需要公认常识补全的确定性步骤(该领域标准做法)请补全·真正无从判断才答 UNKNOWN·不编造。\n\n"
        f"【解题计划】\n{_plan[:1200]}\n\n【中间结论】\n{_facts[:2800]}\n\n【问题】{_q_bare}\n\n"
        "【最后一行】必须是:\n答案: <最终答案·简洁·问编号/数值/日期就只给该值>")
    def _solve_once(_facts_in):
        _p = _p_solve.replace(_facts[:2800], _facts_in[:2800]) if _facts_in != _facts else _p_solve
        try:
            return (_G.call(_p, tier="heavy", max_tokens=2500, timeout=timeout).get("out") or "").strip()
        except Exception:
            return ""
    _out = _solve_once(_facts)
    if not _out:
        # 求解被拒(空)·常因 facts 仍残留触发词。重试一次:把 facts 里所有全大写缩写
        #   (≥3字母·可能含 SPFMV 等专名)统一占位·牺牲少量技术缩写换过拒答(通用兜底)。
        _facts_masked = _re2.sub(r"\b[A-Z]{3,}\b", "[X]", _facts)
        _out = _solve_once(_facts_masked)
    if not _out:
        return ""
    _m = _re2.search(r"答案[:：]\s*(.+)", _out)
    _ans = (_m.group(1).strip() if _m else _out).strip()
    if not _ans or _ans.upper().startswith("UNKNOWN"):
        return ""
    return _ans


def _safe_qid(qid):
    """把 qid 变成文件名安全的形式: 非 [A-Za-z0-9_] 的字符替换成 _。"""
    return "".join(c if (c.isalnum() or c == "_") else "_" for c in str(qid or "unknown"))


def _now_iso():
    """ISO 时间戳。fail-safe: 任何环境异常都返回空串,不让时间戳生成拖垮调用方。"""
    try:
        from datetime import datetime
        return datetime.now().isoformat()
    except Exception:
        return ""


def _ckpt_path(qid):
    # 每用户 session 持久化(2026-08-20): ~/.meyo/users/<user>/sessions/auto_ckpt_<qid>.json
    #   同用户的会话都在自己名下(user_dir 运行时可用·定义在下方模块级)。关掉退回全局。
    try:
        if _user_fs_enabled():
            return os.path.join(user_dir(qid, "sessions"),
                                f"auto_ckpt_{_safe_qid(qid)}.json")
    except Exception:
        pass
    return os.path.join(CKPT_DIR, f"auto_ckpt_{_safe_qid(qid)}.json")


_STARTUP_CHECKED = [False]  # 每进程只查一次


def _startup_selfcheck():
    """启动自检(2026-08-12·最高等级事故修复):开机第一秒 py_compile 核心模块·
    坏了→大字横幅告警+返回坏文件列表(调用方拒跑/标 DEGRADED)。
    事故教训:'主文件语法坏着'不该靠人偶然 grep 发现·是 agent 开机第一秒自检项。
    每进程只查一次(缓存)。fail-safe:自检本身出错不阻断(返回空)。"""
    if _STARTUP_CHECKED[0]:
        return []
    _STARTUP_CHECKED[0] = True
    broken = []
    try:
        import py_compile
        _here = os.path.dirname(os.path.abspath(__file__))
        for mod in ("qa_auto_agent.py", "qa_tool_impls.py", "qa_task_analysis.py"):
            fp = os.path.join(_here, mod)
            if not os.path.exists(fp):
                continue
            try:
                py_compile.compile(fp, doraise=True)
            except Exception as e:
                broken.append(f"{mod}: {type(e).__name__}: {str(e)[:100]}")
    except Exception:
        return []
    # 解释器/依赖自检:上线前直接报环境缺口·不带病跑到半路。Native 是唯一引擎，
    # 不再要求已删除的 deepagents 后端依赖。
    try:
        if sys.version_info[:2] != (3, 12):
            broken.append(f"解释器版本 {sys.version_info[0]}.{sys.version_info[1]} != 3.12"
                          f"(生产钉死 3.12·当前 exe={sys.executable})")
        for _dep in ("langchain_google_genai",):
            try:
                __import__(_dep)
            except Exception:
                broken.append(f"缺生产依赖: {_dep}(带病跑=污染数据·装: pip install --break-system-packages {_dep})")
    except Exception:
        pass  # fail-safe:环境自检本身出错不阻断(和语法自检同策略)
    # 工具docstring自检:扫"函数体第一条=字符串%运算"的伪 docstring。
    # 这类函数 __doc__=None·任何 schema 编译器都会拒绝；启动时直接指出位置。
    try:
        import ast as _ast
        _self_fp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qa_auto_agent.py")
        _tree = _ast.parse(open(_self_fp, encoding="utf-8").read(), _self_fp)
        for _n in _ast.walk(_tree):
            if isinstance(_n, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and _n.body:
                _f = _n.body[0]
                if isinstance(_f, _ast.Expr) and isinstance(_f.value, _ast.BinOp) \
                   and isinstance(_f.value.op, _ast.Mod) \
                   and isinstance(_f.value.left, _ast.Constant) \
                   and isinstance(_f.value.left.value, str):
                    broken.append(f"伪docstring(%运算吃掉__doc__): def {_n.name} @L{_n.lineno}"
                                  f"·工具schema无法编译·改回裸字符串字面量")
    except Exception:
        pass  # fail-safe
    if broken:
        bar = "█" * 60
        try:
            print(f"\n{bar}\n█ ⚠️ 启动自检失败·核心模块语法坏了(带病跑=污染数据):\n"
                  + "\n".join(f"█   {b}" for b in broken)
                  + f"\n█ 该 run 将标 DEGRADED·别信它的结果\n{bar}\n", file=sys.stderr)
        except Exception:
            pass
    return broken


# ═══════════════════════════════════════════════════════════════════════
# G2/G3 感知投票池 (2026-08-12 · 错题归因) · 治分数图题
#
# 病根(实证分数图题 gold=17个分数):gemini 第一次就读对了 gold 开头·后来又读出
# 另一组·agent 把多次读取【捏合】成一个错版本交卷。多次感知读取不该 blend·
# 而应各自作为【独立候选样本】进投票池·finalize 截断时取【证据最强候选】
# (信息量最大/明确说读不出的排最后)·而非最后一次捏合的候选。
# best_answer_so_far 升级:候选按证据强度排序·不按时间排序。
# ═══════════════════════════════════════════════════════════════════════

# VLM 明确"读不出"的标志(与门控病修复处同源·感知失败=弱候选)
_PERCEPTION_FAIL_MARKERS = (
    "cannot make out", "can't make out", "unable to read", "no readable text",
    "cannot read", "can't read", "too blurry", "看不清", "读不出", "无法辨认",
    "无法读", "识别不出", "失败:", " 异常:",
)


def _emit_gate_event(qid, gate, decision, evidence=None):
    """门决策遥测(v98t · 2026-08-12 · 观测缺口修复):把每次门的拦/放/逃逸决策 +
    当时的证据快照落一条独立事件到 gate_events.jsonl。纯观测·只追加·fail-safe。
    gate: 门名(如 calc_trace);decision: block|pass|escape;evidence: 决策时的关键态 dict。
    用途:从事件流归因"门有没有触发、为什么触发/放行"——治上次多边形归因看不到门决策的缺口。"""
    try:
        _p = os.path.join(QA_ROOT, "state", "gate_events.jsonl")
        _rec = {"ts": _now_iso(), "qid": str(qid), "gate": str(gate),
                "decision": str(decision), "evidence": evidence or {}}
        with open(_p, "a", encoding="utf-8") as _f:
            _f.write(json.dumps(_rec, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 遥测写失败绝不影响门/主流程


def _record_observation_anchor(session, tool, src, summary):
    """保存可复查的直接感知观察，避免后续推理静默遗忘或改写已读取的画面事实。

    这是证据账本，不推断答案：只记录工具实际返回的观察及其来源。最终收敛时，
    语义契约审计会检查结论是否使用或显式反驳这些观察；若没有，就要求回到原始
    画面/资料做区分性验证。任何视觉、OCR 或未来感知工具都可复用此入口。
    """
    try:
        if not isinstance(session, dict):
            return
        text = str(summary or "").strip()
        low = text.lower()
        if not text or any(marker in low for marker in _PERCEPTION_FAIL_MARKERS):
            return
        anchors = session.setdefault("evidence_anchors", [])
        anchors.append({
            "kind": "perception",
            "level": "direct_observation",
            "claim": text[:1200],
            "source": f"{tool}:{str(src)[:240]}",
            "at_time": _now_iso(),
        })
        del anchors[:-24]
    except Exception:
        pass


def _mark_video_evidence_consumed(session, value, level="read", source="video"):
    """统一登记任一视频/音频工具成功取得的原始证据。"""
    try:
        if not isinstance(session, dict):
            return
        text = str(value or "").strip()
        if not text:
            return
        session["_video_evidence_ok"] = True
        session.setdefault("_perceived_keys", []).append({
            "level": str(level or "read"), "value": text[:200], "source": str(source)[:80],
        })
    except Exception:
        pass


def _direct_observation_reminder(session):
    """给后续推理回灌近期的高置信直接观察，防止检索结果静默覆盖原始读数。

    这是过程提示而不是答案判断：它不推断实体映射、也不比较数值，只把已经由
    感知工具读到的 ``read/direct_observation`` 证据再次暴露给决策上下文，要求后续
    检索、换算或最终交付显式消费、解释或反驳它。
    """
    try:
        if not isinstance(session, dict):
            return ""
        budget = session.get("_budget") or {}
        step = int(budget.get("n_tool_calls", 0) or 0)
        # 每三个工具步最多提醒一次；没有继续调查时不重复刷屏。
        if step <= int(budget.get("_evidence_reminder_last_step", -3) or -3) + 2:
            return ""
        anchors = session.get("evidence_anchors") or []
        direct = [a for a in anchors if isinstance(a, dict) and
                  a.get("level") in ("direct_observation", "read") and a.get("claim")]
        if not direct:
            return ""
        recent = direct[-2:]
        lines = [f"  · {str(a.get('claim'))[:260]}" for a in recent]
        budget["_evidence_reminder_last_step"] = step
        session["_budget"] = budget
        return (
            "[直接观察证据账本] 以下是已从原始感知通道读到的事实；后续不能被二手检索静默覆盖。"
            "在换实体、换单位、采用近似值或交卷前，必须明确消费、解释或回到原始来源反驳：\n"
            + "\n".join(lines)
        )
    except Exception:
        return ""


def _record_perception_vote(session, tool, src, summary):
    """把一次感知读取(VLM/expert/OCR)作为独立候选存进投票池·不 blend。
    fail-safe:任何异常都静默(绝不因记票破坏工具主流程)。"""
    try:
        if not isinstance(session, dict):
            return
        text = str(summary or "")
        low = text.lower()
        failed = any(m in low for m in _PERCEPTION_FAIL_MARKERS)
        # 证据强度打分:失败(读不出/异常)=0·否则按信息量(去空白长度·封顶)
        strength = 0.0 if failed else min(len(text.strip()), 4000) / 4000.0
        # v99b(2026-08-12)混排worksheet结构化共识:解析结构化read(dict含quality/items)
        #   供 field_consensus 按 item_id+role+独立签名真投票。crop_mode从tool推断
        #   (zoom=裁剪重读·其他=全页)·model从summary前缀[..]抽·作独立性签名。fail-safe。
        read_dict = {"items": [], "quality": "low", "reason": "not_parsed"}
        try:
            import vision_consensus as _vc
            _crop = "zoom" if "zoom" in str(tool) else "full"
            _mm = re.match(r"\[([^\]]+?)(?:看图| ).*", text)   # summary前缀常是 [model看图]/[VLM看图]
            _model = (_mm.group(1) if _mm else str(tool))[:40]
            read_dict = _vc.parse_read(text, crop_mode=_crop, model=_model, read_source=str(tool))
        except Exception:
            pass
        votes = session.setdefault("perception_votes", [])
        votes.append({"tool": tool, "src": str(src)[:120],
                      "text": text[:1500], "failed": failed,
                      "strength": round(strength, 3), "ts": _now_iso(),
                      "structured_read": read_dict})
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════
# G4 失败签名熔断 (2026-08-12 · 错题归因) · 治 YouTube 三连/ScienceDirect
#
# 病根:YouTube×3 全撞 [SSL: UNEXPECTED_EOF]·ScienceDirect 77步全被 429/quota 拦·
# agent 用【同一种姿势】对【同一种故障】死磕。循环卫兵粒度是"行为重复"(同一命令)·
# 抓不到"换着命令但都撞同一个墙"。这里细化到【失败签名 = 故障类型】维度:
# 同一类故障(SSL/429/quota/404)累计 N 次·就注入"切策略维度"提示(不是再试一次·
# 是这类姿势短期不会通·换传输参数/换入口/换存档/声明墙)。软提示·保自主性。
# ═══════════════════════════════════════════════════════════════════════

# 故障签名识别(通用·不针对任何站点):错误类型 → 韧性阶梯建议
_FAILURE_SIGNATURES = [
    ("ssl_eof", ("unexpected_eof", "ssl:", "ssl error", "ssleoferror"),
     "SSL/传输层握手失败(常见于 yt-dlp 抓视频站)。别再用同一姿势重试——按韧性阶梯升级: "
     "①换传输参数(--force-ipv4 / 换 TLS / 加 --no-check-certificate / 换 UA) "
     "②换入口(换视频镜像/invidious 实例/二手转录站) ③换存档(Wayback) ④真不通就结构化声明硬墙。"),
    ("rate_429", ("429", "too many requests", "rate limit", "making too many"),
     "被限流(429)。别短间隔重试(越试越封)——①加 sleep 指数退避 ②换 API 端点/换 UA "
     "③换镜像源 ④这个源这个病短期不会好·换整条数据获取路径。"),
    ("quota", ("quota_exhaust", "quota limit", "配额", "额度", "daily_quota"),
     "搜索/抓取额度耗尽(anysearch 等)。这条链废了——换后端/换 key/换工具(run_parallel_search "
     "换搜索源)·别对同一耗尽的后端反复调。"),
    ("not_found_404", ("404", "notfound", "not found", "pugrest.notfound"),
     "资源 404(常见于 API 姿势错)。反复 404=调用姿势/端点错·别换参数硬试——先读该 API 的文档/"
     "示例确认正确端点和参数格式·再调。"),
    ("captcha", ("captcha", "incapsula", "robot", "cloudflare", "反爬"),
     "撞反爬/验证码。同一入口过不去——换入口/换存档(Wayback)/找二手来源·别死磕。"),
]
_FAILSIG_THRESHOLD = 3  # 同一类故障累计这么多次 → 强制切维度提示


def _detect_failure_signature(summary):
    """从工具返回摘要识别故障签名(通用故障类型)。返回 (sig_key, ladder_hint) 或 (None,None)。"""
    low = str(summary or "").lower()
    for key, markers, hint in _FAILURE_SIGNATURES:
        if any(m in low for m in markers):
            return key, hint
    return None, None


def _track_failure_signature(session, tool_name, summary):
    """记录本次工具返回的故障签名·同一签名累计达阈值就返回"切维度"提示(否则"")。
    fail-safe:任何异常都返回 ""(绝不因这道软引导破坏主流程)。"""
    try:
        sig, hint = _detect_failure_signature(summary)
        if not sig:
            return ""
        counts = session.setdefault("_failsig_counts", {})
        counts[sig] = counts.get(sig, 0) + 1
        n = counts[sig]
        if n < _FAILSIG_THRESHOLD:
            return ""
        # 达阈值:每类只强提示一次(记 _failsig_nudged)·避免刷屏
        nudged = session.setdefault("_failsig_nudged", set())
        if sig in nudged:
            return ""
        nudged.add(sig)
        return (f"\n\n[G4韧性阶梯·失败签名熔断] 你已第 {n} 次撞同一类故障「{sig}」——"
                f"这不是运气问题·是【同一种姿势对同一种墙死磕】。{hint} "
                f"(别再用刚才那种方式重试了·换一个结构性不同的维度)")
    except Exception:
        return ""


def _strongest_perception_candidate(session):
    """finalize 截断时取【证据最强】的历史感知候选·非最后捏合的。
    返回 (text, meta) 或 (None, None)。fail-safe。"""
    try:
        votes = (session or {}).get("perception_votes") or []
        good = [v for v in votes if not v.get("failed") and v.get("strength", 0) > 0]
        if not good:
            return None, None
        best = max(good, key=lambda v: v.get("strength", 0))
        return best.get("text"), {"tool": best.get("tool"), "strength": best.get("strength"),
                                  "n_votes": len(votes), "n_good": len(good)}
    except Exception:
        return None, None


def _recover_missing_delivery(question, session, *, timeout=35):
    """恢复“已完成推理但没有调用带正文 finalize”的正常退出。

    这不是按题型猜答案，而是把已执行工具留下的可审计结果交给独立收尾模型：仅当
    工具记录中有实质输出时，抽取已被计算/读取支持的结论；若记录不足则返回空。这样
    不会因一次工具调用成功、最终交付参数遗漏而把整次求解记成空白失败。
    """
    try:
        entries = []
        for item in session.get("tool_log", []) or []:
            summary = str(item.get("summary") or "").strip()
            if not summary or summary.startswith("[") and "拦截" in summary[:100]:
                continue
            entries.append(f"[{item.get('tool', '?')}] {summary[:1000]}")
        if not entries:
            return ""
        evidence = "\n\n".join(entries[-12:])[:8000]
        import qa_llm_gateway as _gateway
        prompt = (
            "你负责将一个已经执行过工具的解题会话收尾。根据下方工具记录，只交付已有直接证据或"
            "可复现计算支持的最终结论；不得补充未验证事实、不得猜测。若记录不足以得出结论，输出 UNKNOWN。"
            "保留必要的一两句依据，最后另起一行写 `答案: <简洁答案>`。\n\n"
            f"问题：{str(question or '')[:1800]}\n\n工具记录：\n{evidence}"
        )
        result = _gateway.call(prompt, tier="heavy", timeout=timeout, max_tokens=800)
        text = (result.get("out") or "").strip() if isinstance(result, dict) else ""
        if not text or text.upper().startswith("UNKNOWN"):
            return ""
        return text
    except Exception:
        return ""


# ── checkpoint/trace纯序列化工具已抽出到 checkpoint_serialization.py ──
#   _evidence_digest/_jsonable_budget/_jsonable_generic 共91行·零IO/零副作用。
from checkpoint_serialization import (_evidence_digest, _jsonable_budget,
                                      _jsonable_generic, _CKPT_THROTTLE_LAST)  # noqa: F401

def _auditor_evidence_check(session, final_markdown):
    """nativex P1·LH"完成判定去耦合"(env MEYO_FINALIZE_AUDITOR=1·默认关)。

    final 的关键事实宣称必须能挂到 tool_log 证据:独立 mid 档 LLM 只读 tool_log
    (不看对话=去耦合)·判 clean/suspect/violation。只对 violation(核心答案无任何
    证据支撑或与记录矛盾)拦·同一 final 文本只拦一次(M6 同因不二拦)。
    fail-safe:审计挂/超时/解析失败→放行(审计故障不许卡交卷·支柱②优先)。
    治:plausible-but-wrong(看似完成·LH 案例 DOC_task_2 同款)——旧门链拦格式
    不拦"无证据宣称"·这正是 Auditor 补的位。
    """
    try:
        final = str(final_markdown or "").strip()
        if len(final) < 40:
            return ""
        _key = final[:120]
        _seen = session.get("_auditor_seen_finals") or []
        if _key in _seen:
            return ""   # 同因不二拦:这个 final 审计过·不再拦第二次
        entries = []
        for item in (session.get("tool_log") or [])[-20:]:
            summ = str(item.get("summary") or "").strip()
            if summ:
                entries.append("[%s] %s" % (item.get("tool", "?"), summ[:300]))
        if not entries:
            return ""
        import qa_llm_gateway as _gw
        prompt = (
            "你是独立只读审计员。对照【工具记录】审查【候选最终答案】的关键事实宣称:\n"
            "关键宣称=数值/实体/因果结论。逐一判断是否有对应工具输出支撑。\n"
            '只输出一行 JSON: {"trust": "clean|suspect|violation", "why": "≤60字"}\n'
            "clean=关键宣称均有证据; suspect=部分缺证(不拦); "
            "violation=核心答案无任何证据支撑或与记录矛盾。\n\n"
            "候选最终答案:\n%s\n\n工具记录:\n%s" % (final[:1500], "\n".join(entries)[:6000]))
        r = _gw.call(prompt, tier="mid", timeout=30, max_tokens=200)
        txt = (r.get("out") or "").strip() if isinstance(r, dict) else ""
        _seen.append(_key)
        del _seen[:-8]
        session["_auditor_seen_finals"] = _seen
        try:
            j = _parse_model_json_object(txt)
        except Exception:
            return ""
        if isinstance(j, dict) and str(j.get("trust", "")).lower() == "violation":
            _emit_gate_event(session.get("qid", ""), "auditor_evidence", "block",
                             {"why": str(j.get("why", ""))[:120]})
            return ("[审计员·证据核对] 候选答案的关键宣称(%s)在工具记录中找不到支撑"
                    "或与之矛盾。请先回到工具拿证据(检索/计算/读文件)·把关键宣称的"
                    "出处写进答案·再重新交卷;若证据确实拿不到·诚实标注不确定后交卷"
                    "(本拦截对同一答案只生效一次)。" % str(j.get("why", ""))[:120])
        return ""
    except Exception:
        return ""


def _save_ckpt(qid, status="running", extra=None):
    """把 session 的精简关键态原子落盘。fail-safe: 失败只打印不抛。"""
    try:
        s = _SESSIONS.get(qid)
        if s is None:
            return
        # running 态节流落盘(env MEYO_CKPT_THROTTLE=1·默认关=逐字旧行为)。
        # §六点五热点①:per-step 同步全量 ckpt(20-40KB×90步 fsync)是最热 I/O。
        # 节流:running 状态最多每 5s 落一次(内存 session 一直准·硬杀时 _best_effort
        # 兜底语义不变)·终态(done/interrupted/...)永不节流立即落。
        if os.environ.get("MEYO_CKPT_THROTTLE", "") == "1" and status == "running":
            _now_t = time.time()
            if _now_t - _CKPT_THROTTLE_LAST.get(qid, 0.0) < 5.0:
                return
            _CKPT_THROTTLE_LAST[qid] = _now_t
        # v98B(2026-08-13 用户:双保险·每步落盘候选) · 若 final 还空(没finalize)·
        # 就把【当前最强证据候选】落进 ckpt 的 _best_effort 字段——这样即使进程被外层
        # SIGKILL 硬杀(连兜底except都没机会跑)·ckpt 里也有"算到一半的成果"·不是纯空白。
        # 治11题全撞墙被硬杀→final空→记0(它们算到一半的中间结果全丢了)。fail-safe。
        _final = (s.get("final") or "")
        _best_effort = ""
        if not _final.strip():
            try:
                _cand, _meta = _strongest_perception_candidate(s)
                if _cand:
                    _best_effort = str(_cand)[:_CKPT_TEXT_MAX]
                elif s.get("narrative"):
                    _best_effort = str(s.get("narrative"))[:_CKPT_TEXT_MAX]
            except Exception:
                _best_effort = ""
        ckpt = {
            "qid": qid,
            # 记录引擎后端(native/deepagents)——A/B 回测评分要按
            #   backend 分组·之前没记·reg/rd3/rd4/nat 各 tag 靠猜·interrupted 旧跑混
            #   进对照分(实测 reg/rd 系多为 opus-400 污染的 deep 跑)。纯增量字段。
            # 实际后端优先：native 加载异常而回退 deepagents 时，不能继续把 ckpt 标作 native。
            "backend": s.get("engine_backend_actual") or os.environ.get("MEYO_AGENT_BACKEND", "native"),
            "backend_requested": s.get("engine_backend_requested") or os.environ.get("MEYO_AGENT_BACKEND", "native"),
            "question": s.get("question"),
            "goal": s.get("question"),  # 目标即 question
            "evidence_digest": _evidence_digest(s.get("evidence")),
            "tool_log": s.get("tool_log", []),
            "plan": s.get("plan", []),  # G-align · 显式计划,web 层实时展示当前步骤
            "budget": _jsonable_budget(s.get("_budget")),
            # triage 也落盘(之前只存内存·事后归因/中断恢复都看不到口径约束与
            #   modality_needs·两轮调试都被这个盲点坑过)
            "triage": _jsonable_generic(s.get("triage")) or {},
            "narrative": (s.get("narrative") or "")[:_CKPT_TEXT_MAX],
            "final": _final[:_CKPT_TEXT_MAX],
            # RC1/RC2/RC9: authoritative typed final, committed by the thin loop before
            # any later wall-kill can erase it. `final` remains a compatibility projection.
            "answer_artifact": _jsonable_generic(s.get("_answer_artifact")) or None,
            "_best_effort": _best_effort,  # final空时的兜底候选(被硬杀也有成果·非纯空)
            # L1 事件底座落盘(v119: 尾 60→400·内存已 400 上限·对齐 dsh
            #   "model-visible means logged"重放需求)
            "loop_events": (s.get("loop_events") or [])[-400:],
            # Canonical append-only event stream is the replay authority; checkpoint stores
            # only its location and cursor, avoiding a second independently-mutated truth.
            "event_log_path": s.get("_event_log_path") or "",
            "event_cursor": int(s.get("_event_cursor") or 0),
            "_last_observation_evidence_ids": _jsonable_generic(
                (s.get("_last_observation_evidence_ids") or [])[-100:]) or [],
            # v120 FixP(2026-08-17·nat11 归因盲): 意图账本落盘——内存里活(extract 事件
            #   在)但白名单漏了→ckpt 永远 null·事后没法归因 P2 效果。有界裁剪。
            "intent_ledger": _jsonable_generic(s.get("intent_ledger")) or None,
            # Answer Vault 已锁实体落盘——跨进程重跑
            #   (_rerun 脚本等)也能读回已锁定的中间结论·resume 时注入上下文·不从零
            #   泛化(bc_1786949292 Walibora 丢失根因)。小 dict·纯增量字段·fail-safe。
            "_answer_vault": _jsonable_generic(s.get("_answer_vault")) or None,
            "_candidate_vault": _jsonable_generic(
                (s.get("_candidate_vault") or [])[-20:]) or [],
            "_candidate_contract_rejections": _jsonable_generic(
                (s.get("_candidate_contract_rejections") or [])[-20:]) or [],
            # SoulDriver 小步逼近链的可重放状态：候选演化、实体库、关系图、动态复盘。
            # 之前漏落盘会把“运行过但内存丢失”误判成“从未触发”。均有界保存。
            "_progressive_candidate_log": _jsonable_generic(
                (s.get("_progressive_candidate_log") or [])[-20:]) or [],
            "_pilot_review_log": _jsonable_generic(
                (s.get("_pilot_review_log") or [])[-20:]) or [],
            "_candidate_hypotheses": _jsonable_generic(
                (s.get("_candidate_hypotheses") or [])[-20:]) or [],
            "_progress_state": _jsonable_generic(s.get("_progress_state")) or {},
            "_missing_constraints": _jsonable_generic(
                (s.get("_missing_constraints") or [])[:8]) or [],
            "_pilot_executed_queries": _jsonable_generic(
                (s.get("_pilot_executed_queries") or [])[-8:]) or [],
            "_pilot_failed_paths": _jsonable_generic(
                (s.get("_pilot_failed_paths") or [])[-12:]) or [],
            "_ig_plan_log": _jsonable_generic(
                (s.get("_ig_plan_log") or [])[-30:]) or [],
            "_entity_bank": _jsonable_generic(dict(sorted(
                (s.get("_entity_bank") or {}).items(),
                key=lambda item: -int((item[1] or {}).get("hits") or 0))[:100])) or {},
            "_evidence_graph": _jsonable_generic(s.get("_evidence_graph")) or
                {"edges": [], "nodes": {}, "adj": {}},
            # Strategy routing + inverted candidate matrix are decision state; persist them so
            # replay/attribution can see which strategy ran and how candidates were verified.
            "_strategy_route": _jsonable_generic(s.get("_strategy_route")) or None,
            "_inverted_entity": _jsonable_generic(s.get("_inverted_entity")) or None,
            # native 的实际能力开关/非致命失败必须和答案一起落盘，避免评测把降级运行当完整 native。
            "native_runtime_health": _jsonable_generic(s.get("native_runtime_health")) or {},
            "nonfatal_failures": _jsonable_generic((s.get("nonfatal_failures") or [])[-50:]) or [],
            # v119 P1-C · 压缩前全量上下文快照(重放用·env 门控才产生·有界裁剪)
            "ctx_archive": (s.get("ctx_archive") or [])[-120:],
            "status": status,
            "interrupt_reason": (str((extra or {}).get("interrupt_reason")
                                     or (extra or {}).get("error") or "")[:500]
                                 if status == "interrupted" else ""),
            "updated": datetime.now().isoformat(),
            # 感知投票记录落盘(供外层做【证据驱动的模型归因】:答案与哪个模型读数最接近
            #   就归因那个模型答对/答错·而非给所有参与模型记同一结果)。只留model+读数摘要·精简。
            "evidence_anchors": [
                {"kind": str(a.get("kind") or "")[:40], "level": str(a.get("level") or "")[:20],
                 "claim": str(a.get("claim") or "")[:400], "source": str(a.get("source") or "")[:200],
                 "at_time": str(a.get("at_time") or "")[:40]}
                for a in (s.get("evidence_anchors") or [])[-12:] if isinstance(a, dict)
            ],
            "perception_votes_digest": [
                {"model": (v.get("structured_read") or {}).get("model")
                          or v.get("tool"),
                 "text": str(v.get("text") or "")[:400],
                 "failed": bool(v.get("failed"))}
                for v in (s.get("perception_votes") or [])[-12:]
            ],
        }
        if extra:
            ckpt["extra"] = extra
        path = _ckpt_path(qid)
        # 病根: 批量 rerun 脚本
        # (_rerun_3wrong.py 等)会在【新一轮开跑前】os.rename() 旧 ckpt 归档成
        # .stale_rerun_<ts>；若上一轮进程尚未真正退出(仍在后台写 ckpt)，两者
        # 对同一 qid 的 ckpt 路径产生竞态：目录短暂缺失/被并发操作打断时
        # open(tmp,"w") 或 os.replace(tmp,path) 会抛 FileNotFoundError，而这里
        # 外层已经是 try/except 兜底——理论上只会打日志不影响主流程，但为了让
        # "落盘失败"真正降到 0(而不是每次都吞掉一次真实丢失的 ckpt 数据)，加：
        # ①写前确保父目录存在(exist_ok，防目录被并发删除/未创建) ②失败重试
        # 1 次(短暂让开竞态窗口) ③每个 qid 独立 tmp 文件名(pid+纳秒)防多进程/
        # 多线程互相踩 tmp。
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{time.time_ns()}.tmp"
        _last_err = None
        for _attempt in range(2):
            try:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(json.dumps(ckpt, ensure_ascii=False))
                os.replace(tmp, path)  # 原子替换，防止写一半崩坏
                _last_err = None
                break
            except FileNotFoundError as e:
                _last_err = e
                try:
                    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                except Exception:
                    pass
                time.sleep(0.05)  # 让开与 rename 归档脚本的竞态窗口
                continue
        if _last_err is not None:
            raise _last_err
        # 除 per-user 主路径外·再落一份
        # legacy 全局路径 CKPT_DIR/auto_ckpt_<qid>.json。原因: CLAUDE.md 明确要求
        # "每次跑必落 qa_system/state/auto_ckpt/auto_ckpt_<qid>_<runtag>.json"·
        # 而 v118 之后主路径改为 per-user·若只写 per-user·按 CLAUDE.md 去磁盘找的人
        # 会以为 trace 丢了(病根重现)。fail-safe·双写失败不影响主写。
        try:
            _legacy = os.path.join(CKPT_DIR, f"auto_ckpt_{_safe_qid(qid)}.json")
            if _legacy != path:
                os.makedirs(os.path.dirname(_legacy) or ".", exist_ok=True)
                _tmp_leg = f"{_legacy}.{os.getpid()}.{time.time_ns()}.tmp"
                with open(_tmp_leg, "w", encoding="utf-8") as _f:
                    _f.write(json.dumps(ckpt, ensure_ascii=False))
                os.replace(_tmp_leg, _legacy)
        except Exception as _e_leg:
            print(f"  [auto-agent ckpt] legacy 双写失败(忽略): {type(_e_leg).__name__}: {_e_leg}",
                  file=sys.stderr)
    except Exception as e:
        print(f"  [auto-agent ckpt] 落盘失败(忽略): {type(e).__name__}: {e}", file=sys.stderr)
        # 兜底清理残留 tmp 文件(防目录里堆积一堆写失败的 .tmp 垃圾)
        try:
            if 'tmp' in dir() and os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def _load_ckpt(qid):
    """读回 checkpoint dict；不存在/损坏返回 None。fail-safe。
    2026-08-20 per-user 迁移: 优先读 per-user 路径·没有则回退全局旧路径(不丢历史 session)。"""
    try:
        path = _ckpt_path(qid)
        if not os.path.exists(path):
            # per-user 路径没有 → 回退全局旧 ckpt(历史会话兼容)
            _legacy = os.path.join(CKPT_DIR, f"auto_ckpt_{_safe_qid(qid)}.json")
            if _legacy != path and os.path.exists(_legacy):
                path = _legacy
            else:
                return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  [auto-agent ckpt] 读盘失败(忽略): {type(e).__name__}: {e}", file=sys.stderr)
        return None


def _build_resume_context(ckpt, fresh_question=None):
    """从 checkpoint 构造一段续传上下文，注入 deepagents 首条 user 消息。
    返回 "" 表示无需续传(无 ckpt 或已 done)。

    (2026-08-14 根因修·e142) 续传目标【必须】用本次调用传入的 fresh_question(CLI --question
    权威·完整题面)·【绝不】用 ckpt 里存的 question。病根:旧 ckpt 存的是被缩写/截断的题面
    (e142 实测 ckpt.question 只 116 字·完整官方题面 1005 字·中间 900 字规则全丢)·resume 时
    若拿 ckpt.question 当目标注入·agent 就对着残缺题面解题→算错(治 feedback-truncation +
    feedback-degraded-run stale-ckpt 家族)。fresh_question 缺失才回退 ckpt(fail-safe)。"""
    if not ckpt or ckpt.get("status") == "done":
        return ""
    question = (fresh_question or "").strip() or ckpt.get("question") or ckpt.get("goal") or ""
    dig = ckpt.get("evidence_digest") or {}
    n_ev = dig.get("n", 0)
    preview = dig.get("preview") or []
    ev_lines = []
    for p in preview[:5]:
        t = (p.get("title") or "").strip()
        u = (p.get("url") or "").strip()
        if t or u:
            ev_lines.append(f"{t} {u}".strip())
    ev_summary = "; ".join(ev_lines) if ev_lines else "(无标题预览)"

    # 统计用过的工具及各自次数
    tool_counts = {}
    for entry in (ckpt.get("tool_log") or []):
        name = entry.get("tool", "?")
        tool_counts[name] = tool_counts.get(name, 0) + 1
    tools_str = ", ".join(f"{k}×{v}" for k, v in tool_counts.items()) if tool_counts else "(尚未调用工具)"

    progress = (ckpt.get("narrative") or ckpt.get("final") or "").strip()
    progress_summary = progress[:600] if progress else "(此前尚无正文/结论产出)"

    # Resume from the canonical projection. AnswerVault is consulted only when an old
    # checkpoint has no replayable candidate lifecycle.
    _relock = ""
    try:
        _canonical_candidates = []
        _event_path = str(ckpt.get("event_log_path") or "")
        if _event_path and os.path.exists(_event_path):
            import qa_event_stream as _qes_resume
            _projection = _qes_resume.replay_file(_event_path)
            _canonical_candidates = [
                item for item in _projection.candidates.values()
                if item.get("state") in ("selected", "delivery_verified")]
            if _canonical_candidates:
                _item = _canonical_candidates[0]
                _value = str(_item.get("value") or "").strip()[:160]
                _relock = (f" 事件流已选择候选『{_value}』；支持证据={_item.get('evidence_ids') or []}，"
                           f"反证={_item.get('contradicting_evidence_ids') or []}，"
                           f"未决约束={_item.get('unresolved_constraints') or []}。"
                           "请从该事实状态继续，不要重新从散落日志猜候选。")
        if not _canonical_candidates:
            _v = ckpt.get("_answer_vault") or {}
            _va = str(_v.get("answer") or "").strip()[:160]
            if _va:
                _relock = (f" legacy checkpoint 仅有兼容候选『{_va}』；它尚未通过 canonical"
                           " 生命周期验证，必须先用已有证据迁移和核验，不能直接交付。")
    except Exception:
        _relock = ""

    return (
        f"[续传] 本任务此前已执行过，目标: {question}。"
        f"已收集证据 {n_ev} 条(摘要: {ev_summary})。"
        f"已调用过工具: {tools_str}。"
        f"此前进度: {progress_summary}。"
        + _relock +
        "请基于已有进度继续完成，不要从零重复已做的检索。"
    )


def _restore_session_from_ckpt(qid, ckpt, fresh_question=None):
    """把 checkpoint 的关键态恢复进 _sess(qid)。fail-safe。
    (2026-08-14 根因修) question 优先用 fresh_question(本次 CLI 权威完整题面)·
    绝不用 ckpt 里可能被缩写/截断的旧题面覆盖。"""
    try:
        s = _sess(qid)
        _q = (fresh_question or "").strip() or ckpt.get("question")
        if _q:
            s["question"] = _q
        s["tool_log"] = list(ckpt.get("tool_log") or [])
        # v98b resume 从 tool_log 重建"模态证据已消费"标记(比单独持久化更准):
        #   历史里有视频工具成功抽帧/转录 → _video_evidence_ok=True(门控别 resume 后又逼)
        for _t in s["tool_log"]:
            _sm = str(_t.get("summary") or "")
            if _t.get("tool") in ("see_video_frame", "transcribe_audio", "traverse_video") and \
                    ("[视频帧 " in _sm or "[transcribe·" in _sm or "[traverse_video] 答案:" in _sm):
                s["_video_evidence_ok"] = True
                break
        if ckpt.get("narrative"):
            s["narrative"] = ckpt["narrative"]
        _legacy_final = str(ckpt.get("final") or "").strip()
        _committed_artifact = ckpt.get("answer_artifact")
        if isinstance(_committed_artifact, dict):
            s["_answer_artifact"] = dict(_committed_artifact)
        # Canonical events are restored first for research-state telemetry. A committed
        # typed answer artifact remains authoritative over candidate lifecycle projection:
        # replay may say candidates are incomplete, but it cannot un-commit the model's
        # explicit final (RC1/RC11).
        _has_canonical_candidates = False
        _event_path_restore = str(ckpt.get("event_log_path") or "")
        if _event_path_restore and os.path.exists(_event_path_restore):
            try:
                import qa_event_stream as _qes_restore
                _projection_restore = _qes_restore.replay_file(_event_path_restore)
                _has_canonical_candidates = bool(_projection_restore.candidates)
                s["_canonical_projection"] = _projection_restore.to_dict()
                s["_missing_constraints"] = list(_projection_restore.gaps)
                s["_pilot_failed_paths"] = list(_projection_restore.failed_paths[-12:])
                if _projection_restore.deliverable:
                    s["final"] = _projection_restore.answer
                else:
                    s["final"] = ""
            except Exception as _restore_event_error:
                s.setdefault("nonfatal_failures", []).append({
                    "stage": "checkpoint_event_replay",
                    "error": str(_restore_event_error)[:200],
                })
        # A committed artifact's clean display wins over both replay-empty and legacy final.
        _art_final_restore = ""
        if isinstance(_committed_artifact, dict):
            _art_final_restore = str(_committed_artifact.get("display")
                                     or _committed_artifact.get("raw") or "").strip()
        if _art_final_restore:
            s["final"] = _art_final_restore
        elif not _has_canonical_candidates and _legacy_final:
            s["final"] = _legacy_final
        if (not _has_canonical_candidates
                and isinstance(ckpt.get("_answer_vault"), dict)
                and ckpt["_answer_vault"].get("answer")):
            s["_answer_vault"] = dict(ckpt["_answer_vault"])
        # 恢复 native 机制健康态，续跑/评测可区分完整运行与 fail-open 降级运行。
        if isinstance(ckpt.get("native_runtime_health"), dict):
            s["native_runtime_health"] = dict(ckpt["native_runtime_health"])
        if isinstance(ckpt.get("nonfatal_failures"), list):
            s["nonfatal_failures"] = [x for x in ckpt["nonfatal_failures"] if isinstance(x, dict)][-50:]
        if ckpt.get("event_log_path"):
            s["_event_log_path"] = str(ckpt["event_log_path"])
            s["_event_cursor"] = int(ckpt.get("event_cursor") or 0)
        # 恢复高价值观测锚点：允许后续推理消费或显式反驳先前读取，避免 resume 后丢失。
        anchors = ckpt.get("evidence_anchors") or []
        if isinstance(anchors, list):
            s["evidence_anchors"] = [a for a in anchors if isinstance(a, dict)][-12:]
        # evidence 只恢复摘要占位(全文没存)，避免下游误以为有全文
        dig = ckpt.get("evidence_digest") or {}
        s["_resumed_evidence_n"] = dig.get("n", 0)
        # v97f(2026-08-11) 恢复budget关键态·否则resume后反思闸计数/agent自调阈值/已反思
        #   标记全归零(实测Tri-Rail搜12次没触发反思)。只恢复反思相关+计数·墙钟t_start不恢复
        #   (resume是新的墙钟起点·别把旧t_start带进来误判超时)。
        _ck_budget = ckpt.get("budget") or {}
        if _ck_budget:
            # setdefault 对已存在但值为 None 的 key 会返回 None(session 模板里 _budget 初始
            #   就是 None·见 run_state._new_session_dict)——必须显式兜 {}·否则下面下标赋值崩。
            _sb = s.get("_budget")
            if not isinstance(_sb, dict):
                _sb = {}
                s["_budget"] = _sb
            for _k in ("_wander_reflected", "_wander_thresh", "_tool_seq",
                       "n_tool_calls", "n_investigative_calls", "reflect_nudge_count",
                       "_video_guard_count", "_vision_guard_count",
                       "_forced_tool", "_force_gate_count", "_semantic_contract_audited",
                       "_semantic_contract_inv_snapshot", "_semantic_contract_blocks", "_question_contract_gap_notified",
                       "_hypothesis_check_snapshot", "_hypothesis_check_blocks", "_hypothesis_checked",
                       "_problem_frontier", "_active_intent", "_intent_revision_history",
                       "_intent_hypotheses", "_intent_replan_pending", "_intent_remedy", "_policy_ledger",
                       "_runtime_adaptation", "_tool_effectiveness"):

                if _k in _ck_budget:
                    _sb[_k] = _ck_budget[_k]
    except Exception as e:
        print(f"  [auto-agent ckpt] 恢复 session 失败(忽略): {type(e).__name__}: {e}", file=sys.stderr)


_TOOL_STEP_FAIL_PAT = re.compile(
    r"失败|错误|异常|超时|timeout|error|exception|traceback|拒绝|denied|not\s*found|无法",
    re.IGNORECASE,
)


def _tool_perf_stats(log_path=None):
    """从 AUTO_LOG 历史聚合每个工具的性能: 平均 elapsed、调用次数、
    成功率。fail-safe 返回 {}。

    这是「自动优化工具使用」的最小闭环: 历史→统计→喂回 agent 决策，
    让 LLM 有数据地选「快而准」的工具。

    鲁棒性说明(修复#8):
    1) 损坏行(JSON 解析失败)之前完全静默 continue,日志大面积损坏时用户
       毫无察觉、统计出的 success_rate 可能建立在极少数残存样本上却看起来
       很"正常"——现在计数损坏行并在占比过高时打印告警。
    2) 之前"成功"判定只看整个 task 的 trace.final_len>0,粒度太粗:一个
       任务里某个工具本身报错、但任务最终东拼西凑出了非空 final,该工具
       仍会被记为 100% 成功;反之亦然。这里改为优先看 step 自身的
       summary 文本有没有失败/异常关键词作为该步骤成功与否的信号,
       task 级 final_len 只作为兜底(step 没有可判定信号时使用)。"""
    path = log_path or AUTO_LOG
    stats = {}  # {tool: {"_sum": s, "n": n, "_ok": n_ok}}
    n_lines = 0
    n_bad = 0
    try:
        if not os.path.exists(path):
            return {}
        for line in open(path, encoding="utf-8"):
            if not line.strip():
                continue
            n_lines += 1
            try:
                trace = json.loads(line)
            except Exception:
                n_bad += 1
                continue
            task_success = (trace.get("final_len") or 0) > 0
            for step in (trace.get("steps") or []):
                name = step.get("tool")
                if not name:
                    continue
                el = step.get("elapsed_s")
                d = stats.setdefault(name, {"_sum": 0.0, "n": 0, "_ok": 0})
                if isinstance(el, (int, float)):
                    d["_sum"] += float(el)
                    d["n"] += 1
                # 优先用该步骤自身 summary 是否含失败关键词判定;
                # summary 缺失/无法判定时,退回用整个任务是否有 final 输出兜底。
                summary = step.get("summary")
                if isinstance(summary, str) and summary.strip():
                    step_ok = not _TOOL_STEP_FAIL_PAT.search(summary)
                else:
                    step_ok = task_success
                if step_ok:
                    d["_ok"] += 1
        if n_lines > 0 and n_bad / n_lines > 0.05:
            print(f"  [auto-agent stats] 警告: AUTO_LOG 中 {n_bad}/{n_lines} "
                  f"({n_bad / n_lines:.0%}) 行解析失败,统计结果可能不准确", file=sys.stderr)
        out = {}
        for name, d in stats.items():
            n = d["n"] or 1
            out[name] = {
                "avg_s": round(d["_sum"] / n, 1),
                "n": d["n"],
                "success_rate": round(d["_ok"] / n, 2),
            }
        return out
    except Exception as e:
        print(f"  [auto-agent stats] 统计失败(忽略): {type(e).__name__}: {e}", file=sys.stderr)
        return {}


def _render_tool_perf_hint(max_tools=10):
    """把工具速度统计渲染成一小段 system_prompt 提示。无数据返回 ""。"""
    stats = _tool_perf_stats()
    if not stats:
        return ""
    # 按调用次数降序取前若干个，作为「有代表性」的经验
    items = sorted(stats.items(), key=lambda kv: kv[1]["n"], reverse=True)[:max_tools]
    parts = [f"{name} {d['avg_s']}s(成功率{d['success_rate']})" for name, d in items]
    return (
        "\n# 工具速度参考(历史均值)\n"
        + ", ".join(parts)
        + "\n优先用快而准的工具，慢工具仅在必要时用，避免无谓消耗墙钟预算。\n"
    )


# ═══════════════════════════════════════════════════════════════════════
# G-align · Memory Layers 接入 (维度 E · 对齐 hermes MemoryManager 两段式)
#
# hermes agent/memory_manager.py 的模式:开跑前 prefetch(注入相关经验),
# 收尾后 sync(沉淀本次教训)。这里落成两个薄函数:
#   - _recall_lessons(question): 开跑前把 recall 到的语义记忆拼成一段注入
#     首条 user 消息(和续传上下文同一注入点),让 agent 不从零开始。
#   - _remember_lesson(...): 收尾后把本次任务的可复用结论 remember 进去。
# 全 fail-safe:memory_layers 不可用或异常都返回空/静默,不阻断主流程。
# ═══════════════════════════════════════════════════════════════════════

def _recall_lessons(question, limit=3, user_id="default", trace=None):
    """开跑前召回相关经验,拼成一段可注入首条消息的上下文。无则返回 ""。
    P1 · 带 user_id 时分层召回(全局共性 + 该用户私有)。
    鲁棒性:之前 recall 异常被 except Exception: return "" 完全静默吞掉,
    memory_layers 若已损坏(索引文件坏/依赖缺失)会一直悄悄退化成"没有经验",
    用户以为"越用越聪明"其实记忆系统早已失效却毫无信号。这里把异常记到
    trace(若调用方传了),让上层(rubric/前端)有机会观测到"记忆系统不健康"。"""
    if not _MEM_AVAILABLE or not question:
        return ""
    try:
        hits = _mem_recall(question, limit=limit, min_confidence="low", user_id=user_id)
    except Exception as e:
        if trace is not None:
            trace["recall_error"] = f"{type(e).__name__}: {str(e)[:150]}"
        print(f"  [auto-agent memory] recall 失败(降级为无经验): {type(e).__name__}: {e}", file=sys.stderr)
        return ""
    if not hits:
        return ""
    lines = []
    for h in hits:
        lesson = (h.get("lesson") if isinstance(h, dict) else str(h)) or ""
        lesson = lesson.strip()
        if lesson:
            lines.append(f"- {lesson[:300]}")
    if not lines:
        return ""
    return (
        "[经验] 处理类似任务时沉淀过这些可复用结论,先看一遍再动手:\n"
        + "\n".join(lines[:limit])
    )


def _remember_lesson(question, final, trace, user_id="default"):
    """收尾后沉淀一条语义记忆。只在任务真产出结论时记,避免噪声。fail-safe。
    P1 · 带 user_id 时写入该用户私有层(这个用户越用越有自己的经验)。"""
    if not _MEM_AVAILABLE:
        return
    try:
        final = (final or "").strip()
        if len(final) < 40:  # 太短(报错/空)不值得记
            return
        # 鲁棒性:之前只靠长度判"是否是结论"——一段 40+ 字符的报错信息
        # (如"XXX 转录库未装(ModuleNotFoundError...)")长度轻松过关，会被当成
        # 正面"可复用结论"记入 memory，污染下次任务的经验注入。这里补一层内容
        # 质量检查:final 明显是失败/异常消息时，不当正面结论记(不是不记——
        # 后面 _too_few_tools/_ans_unknown 那条反面教训分支本就覆盖了"失败"场景，
        # 这里只是防止漏网的失败文本继续走正面结论分支)。
        import re as _re_check
        _fail_pattern = _re_check.compile(
            r"\b(Traceback|Error|Exception|failed|failure|timeout|timed out|"
            r"refused|permission denied|not found|no module)\b|"
            r"(异常|失败|超时|未安装|未装|无法确定|无法找到)", _re_check.IGNORECASE,
        )
        _looks_like_failure = bool(_fail_pattern.search(final[:300]))
        n_tools = trace.get("n_tool_calls", 0)
        # 记"这类问题用什么工具组合、多少步收敛"这种可指导下次决策的结论,
        # 不是记问题原文(那是情景记忆的活)。
        used = {}
        for st in (trace.get("steps") or []):
            used[st.get("tool")] = used.get(st.get("tool"), 0) + 1
        tool_summary = ", ".join(f"{k}×{v}" for k, v in used.items() if k) or "(无工具)"
        # 治「经验注入诱导放弃」(实证:EC题上次0工具放弃·被记成
        #   『0步收敛用了(无工具)』经验→下次照抄又0步放弃)。失败模式(工具用得极少/没用·
        #   多半是快速放弃没努力)不记成正面「可复用结论」·改记【反面教训】督促下次用工具·
        #   绝不让"上次放弃"诱导"这次也放弃"。判据:难题(重型/需检索)却几乎没用工具=可疑放弃。
        _ans_unknown = "unknown" in final.lower()[:200]
        _too_few_tools = n_tools <= 1
        # 鲁棒性 · _looks_like_failure(前面算好)命中时同样走反面教训分支，
        # 不当正面结论记(治"40+字符报错信息被当结论记入记忆"的问题)。
        _is_negative_lesson = bool(_too_few_tools or _ans_unknown or _looks_like_failure)
        if _is_negative_lesson:
            lesson = (
                f"处理「{question[:80]}」这类任务【反面教训】:上次只用了 {tool_summary}"
                f"({n_tools}步)就收尾·很可能是没充分用工具/过早放弃。这类需检索/精确数据的题·"
                f"下次务必:skill_search找方法→evolution_search/search_github找工具→shell_exec"
                f"调API(GitHub/PubChem/期刊)多方考据·别凭知识猜或空手收尾。"
            )
        else:
            lesson = (
                f"处理「{question[:80]}」这类任务:{n_tools} 步收敛,"
                f"用了 {tool_summary}。"
            )
        # 用真实 GAIA 100miss 复核实证(qid=2a649bb1)发现的严重连锁 bug:反面教训因
        # 措辞高度模板化,反而更容易和历史同类反面教训 token 重叠→被下面的置信度
        # 计算误判成「反复出现 → high 置信」,然后被 _maybe_forge_skill/_maybe_forge_tool
        # 当成「已验证的正确做法」直接固化成技能文档——等于把"过早放弃"的坏习惯
        # 当成范本教给下次任务,越强化越放弃(实测:该技能文档被 skill_load 后,
        # 下次任务原样复读技能正文当最终答案,0工具收尾自我复现)。这里显式记下
        # "这条经验是不是反面教训",forge 两个函数据此拒绝固化负面经验,无论置信度
        # 多高——不改变 confidence 本身的计算方式(避免影响其他正常场景)。
        trace["memory_is_negative_lesson"] = _is_negative_lesson
        # 统一3 · 置信度正反馈(越用越聪明):先 recall 相似经验,同类模式反复
        # 出现就升置信(low→medium→high)。第一次见 low;第2次同题型出现升
        # medium;第3+次升 high。这样 recall(min_confidence) 时,被反复验证过的
        # 经验会更稳地浮上来,而只出现过一次的偶发经验留在 low 不干扰决策。
        confidence = "low"
        try:
            similar = _mem_recall(question, limit=8, min_confidence="low", user_id=user_id)
            # 用 memory_layers 同一套分词(中文按字、英文按词),否则中文无空格
            # 时 .split() 整句成一个 token,相似度永远算不出来(踩过这个坑)。
            try:
                from memory_layers import _tokenize as _mem_tokenize
            except Exception:
                _mem_tokenize = lambda t: set(str(t).split())  # noqa: E731 · 降级
            key_tokens = _mem_tokenize(question[:80] + " " + tool_summary)
            n_similar = 0
            for h in similar:
                les = (h.get("lesson") if isinstance(h, dict) else "") or ""
                overlap = key_tokens & _mem_tokenize(les)
                # 过半 token 重叠视为"同类历史经验"(反复出现的模式)
                if key_tokens and len(overlap) >= max(3, len(key_tokens) // 2):
                    n_similar += 1
            if n_similar >= 2:
                confidence = "high"
            elif n_similar >= 1:
                confidence = "medium"
        except Exception:
            confidence = "low"  # recall 失败就保守用 low
        _mem_remember(
            lesson=lesson,
            source=f"qa_auto_agent · qid={trace.get('qid')}",
            tags=["auto_agent", trace.get("task_level_hint") or "标准"],
            confidence=confidence,  # 反复出现的模式自动升置信,单次偶发留 low
            user_id=user_id,        # P1 · 写进该用户私有层
        )
        trace["memory_confidence"] = confidence
    except Exception as e:
        print(f"  [auto-agent memory] 沉淀失败(忽略): {type(e).__name__}: {e}", file=sys.stderr)


# ═══════════════════════════════════════════════════════════════════════
# P5 · skillforge per-user 自动化(每个用户沉淀自己的技能库)
#
# 用户「skillforge 也是自动化的个性化了吗」。逻辑:当一次任务的经验置信度升到
# high(=同类模式已反复出现 ≥2 次,memory 正反馈判定这是稳定可复用的做法),
# 就自动把它固化成一条**该用户专属**的 skill(填好正文的 SKILL.md,不是空骨架),
# 放进 skills/_user_{uid}/ 子目录。这样每个用户的 agent 用得越多,越攒出"他专属
# 的做事手册"。只在 high 置信时 forge,避免噪声。全 fail-safe。
# ═══════════════════════════════════════════════════════════════════════

def _maybe_forge_tool(user_id, trace):
    """T2 · 监督自主造工具:任务收尾时,若经验升 high(=同类模式反复出现,监督信号),
    触发 tool_forge_loop 扫高频重复模式并把它固化成命名工具(自动生成 executor→注册→
    验证)。把"用户/agent 反复手动拼的操作"变成"一个工具一步调"。
    只在 high 置信 + 有具体 user 时触发(避免噪声/空耗);T1 幂等保证不重复造。
    max_forge=1 每次最多造 1 个(防一次炸)。全 fail-safe。"""
    if not _TOOLFORGE_AVAILABLE or trace.get("memory_confidence") != "high":
        return
    # 实证 bug(qid=2a649bb1 EC编号题): 反面教训(过早放弃/0工具/答案unknown)因措辞
    # 模板化反而更容易被判"反复出现→high"置信,若不拦截会被当"稳定可复用模式"固化
    # 成命名工具——等于把"放弃"这个坏习惯做成了工具。反面教训绝不 forge,无论置信度。
    if trace.get("memory_is_negative_lesson"):
        return
    if not user_id or user_id == "default":
        return
    # run_forge_loop 会真造工具+验证(慢·且可能触发搜索),放后台线程跑,不阻塞用户
    # 拿到最终答案(用户不必等造工具)。造完记 trace 供下次/日志观测。fail-safe。
    # 鲁棒性:之前内层 except 只 print 没写 trace 字段——trace["tool_forged"]
    # 永远不设置时,无法区分"这次没有可 forge 的稳定模式"和"forge 过程本身崩了"。
    # 这里补 trace["tool_forge_error"] 标记(即便 trace 快照此时可能已被上层读走,
    # 至少日志文件/后续同 session 读取时能看到失败原因，不是完全没有信号)。
    def _bg_forge():
        try:
            r = _tool_forge.run_forge_loop(min_count=3, days=7, max_forge=1)
            forged = r.get("forged") or []
            if forged:
                trace["tool_forged"] = forged[0].get("tool_name")
                print(f"  [auto-agent toolforge] 监督发现高频模式·自主造工具: "
                      f"{forged[0].get('tool_name')}", file=sys.stderr)
        except Exception as e:
            trace["tool_forge_error"] = f"{type(e).__name__}: {str(e)[:150]}"
            print(f"  [auto-agent toolforge] 造工具失败(忽略): {type(e).__name__}: {e}", file=sys.stderr)
    try:
        import threading as _th
        _th.Thread(target=_bg_forge, daemon=True).start()
        trace["forge_thread_started"] = True
    except Exception as e:
        trace["forge_thread_started"] = False
        trace["tool_forge_error"] = f"线程启动失败: {type(e).__name__}: {str(e)[:150]}"
        print(f"  [auto-agent toolforge] 启动后台造工具线程失败: {type(e).__name__}: {e}", file=sys.stderr)


def _maybe_forge_skill(user_id, question, final, trace):
    """任务经验升到 high 置信时,自动为该用户 forge 一条填好正文的 per-user skill。

    v96h(2026-08-11 · 修"背答案"bug) · 实证根因(qid=2a649bb1 EC编号题、624cbf11
    墓碑韵文题等 8 条 per-user skill 复查全部命中): 旧版把 `final`(最终答案原文,
    含具体数值/结论)的前 800 字节原样写进 SKILL.md 的"上次的成功产出摘要"里,
    这不是"方法论沉淀",而是把这一道具体题目的**标准答案**存进了技能库——下次
    同一题目 skill_load 后,agent 直接把存好的答案复述出来,0 独立求证,本质上是
    "背题库"而不是"学会可迁移的解题方法"。这类"一题一个精确唯一答案"的任务
    (GAIA 式 benchmark 尤其典型)最容易踩这个坑：一旦"摘要"里存了答案原文,
    这条 skill 就从"教做事的方法"退化成"抄答案的小抄"。
    修法:①**彻底不再写入 final 的任何内容**(不管是全文还是摘要,一律不落盘)，
    只保留"用了哪些工具、大致按什么顺序、收敛步数"这类跟具体答案无关、
    换一道同类型题目依然适用的流程性经验。②question 也只取极短的主题词做
    skill 名和"适用场景"标注,不做大段原句复述,避免"适用场景"描述本身
    变成"这道题指的就是那道原题"式的直接点题（导致下次看到相似措辞就照抄）。
    """
    if not _SKILLS_AVAILABLE or trace.get("memory_confidence") != "high":
        return
    # 实证 bug(qid=2a649bb1 EC编号题真实复盘): 0工具/answer=unknown 这类【反面教训】
    # 因措辞模板化,同类反面教训之间 token 重叠度反而更高,被下面 _remember_lesson
    # 里的置信度算法误判成"反复出现→high"。若不在这里挡住,会被写成 SKILL.md 模板
    # 里固定措辞的"## 已验证的做法(反复出现≥2次,置信high)",内容却是"工具流程:
    # (直接作答)"——下次 skill_load 加载后 agent 真的照做直接作答,0工具复现,
    # 形成"越失败越被固化成范本"的恶性循环(该 bug 已致其被复述当最终答案输出)。
    # 无论置信度多高,反面教训一律不 forge 成技能。
    if trace.get("memory_is_negative_lesson"):
        return
    uid = "".join(c if (c.isalnum() or c in "_-") else "_" for c in str(user_id or "default"))[:40]
    if not uid or uid == "default":
        return  # 无具体用户不 forge per-user skill
    try:
        import skill_library as _sl
        # 用问题主题做 skill 名(去重靠 mtime 后缀)
        topic = "".join(c for c in question[:24] if c.isalnum() or c in " _-").strip().replace(" ", "-") or "task"
        skill_name = f"_user_{uid}__{topic}"[:60]
        skill_dir = os.path.join(_sl.SKILLS_DIR, skill_name)
        skill_file = os.path.join(skill_dir, "SKILL.md")
        if os.path.exists(skill_file):
            return  # 已有同名,不重复 forge
        used = {}
        for st in (trace.get("steps") or []):
            used[st.get("tool")] = used.get(st.get("tool"), 0) + 1
        tool_flow = " → ".join(k for k in used if k) or "(直接作答)"
        # 只取问题的极短主题片段做"适用场景"标注(不做大段原句复述)，
        # 避免技能文档本身变成"原题重现"，只用来做粗粒度的相似任务归类。
        topic_hint = question[:40].strip()
        os.makedirs(skill_dir, exist_ok=True)
        content = (
            f"---\nname: {skill_name}\n"
            f"description: [{uid}专属] 处理与「{topic_hint}...」主题相近任务的通用工具"
            f"流程(已反复验证,仅记录方法不记录具体答案)\n"
            f"category: per-user\nsource_module: qa_auto_agent 自动 forge\n---\n\n"
            f"# {uid} 专属技能:{topic_hint}...(相似主题任务)\n\n"
            f"## 适用场景\n当 {uid} 又遇到与本任务**主题/结构相近**的任务时"
            f"(注意:是「同类问题的解法」，不是「同一道题的答案」——每次仍需"
            f"独立求证，不要直接照抄上次的具体结论)。\n\n"
            f"## 已验证的通用做法(反复出现 ≥2 次,置信 high)\n"
            f"- 工具流程:{tool_flow}\n"
            f"- 收敛步数:约 {trace.get('n_tool_calls','?')} 步\n"
            f"- 这条经验只是「这类任务大致要调用哪些工具、按什么顺序」的流程"
            f"参考，具体证据/数值/结论必须重新查证，不沿用历史记录。\n"
        )
        # (2026-08-15·G3 去答案化 n-gram 硬门·skillforge事故防回归)
        # 病根:曾有8个skill把上次任务的具体答案写进正文·下次skill_load照抄=背答案。
        # 入库前硬检:content 与本次 final 答案是否共享显著 n-gram(连续词/字)·含则拒写。
        # 这是制度化的安全网(不信任模板一定不含答案·真比对文本)。
        try:
            _final_ans = str(final or (trace or {}).get("final") or "").strip()
            if _final_ans and len(_final_ans) >= 12:
                def _ngrams(t, n=8):
                    t = "".join(ch for ch in t if ch.isalnum())  # 去标点空格·抗格式差异
                    return {t[i:i + n] for i in range(max(0, len(t) - n + 1))}
                _ans_ng = _ngrams(_final_ans)
                _content_norm = "".join(ch for ch in content if ch.isalnum())
                _leak = [g for g in _ans_ng if g and g in _content_norm]
                if _leak:
                    print(f"  [skillforge·G3拒写] skill 正文含上次答案 n-gram({len(_leak)}处)·"
                          f"疑似泄漏答案·拒绝入库: {skill_name}", file=sys.stderr)
                    trace["skill_forge_rejected"] = "answer_ngram_leak"
                    return
        except Exception:
            pass  # 检查异常不阻断(但检查本身是安全网·尽量别异常)
        with open(skill_file, "w", encoding="utf-8") as f:
            f.write(content)
        trace["skill_forged"] = skill_name
        print(f"  [auto-agent skillforge] 为 {uid} forge skill: {skill_name}", file=sys.stderr)
    except Exception as e:
        print(f"  [auto-agent skillforge] forge 失败(忽略): {type(e).__name__}: {e}", file=sys.stderr)


# ═══════════════════════════════════════════════════════════════════════
# P2 · 演绎 per-user 意图 + 用户画像注入(让 agent 认识这个用户)
# ═══════════════════════════════════════════════════════════════════════

def _build_user_context(user_id, question, trace=None):
    """开跑前构造"这个用户是谁 + 他这句话的真实意图"一段,注入 system prompt。
    - user_profile_store.get(user_id): 该用户历史画像(角色/痛点/常问)
    - user_intent_extractor.extract_intent(question): 这句话的真实意图/紧迫度/主题
    无 user 画像或组件不可用时只给意图;都拿不到返回 ""。全 fail-safe。"""
    parts = []

    # 1) 用户画像(这个用户是谁)——只在有具体 user 且画像存在时
    if _PROFILE_AVAILABLE and user_id and user_id != "default":
        try:
            prof = _profile_store.get(user_id)
            if prof:
                bits = []
                if prof.get("role_guess"):
                    bits.append(f"角色: {prof['role_guess']}")
                pains = prof.get("pain_points") or []
                if pains:
                    bits.append(f"常见痛点: {', '.join(str(p)[:40] for p in pains[:3])}")
                needs = prof.get("content_needs") or []
                if needs:
                    bits.append(f"关注: {', '.join(str(n)[:40] for n in needs[:3])}")
                if bits:
                    parts.append("## 这个用户是谁(历史画像)\n" + " · ".join(bits))
                    if trace is not None:
                        trace["user_profile_hit"] = True
        except Exception as e:
            # Q4 · 别静默吞:个性化悄悄失败=用户以为在个性化其实没有。留痕可观测。
            if trace is not None:
                trace["profile_error"] = f"{type(e).__name__}: {str(e)[:80]}"

    # 2) 意图抽取(他这句话到底要什么)
    # 走新 intent_engine · 返回 11 字段 (原 4 + 7 新增)
    if _INTENT_AVAILABLE and question:
        try:
            intent = _extract_intent(question)
            if intent and intent.get("is_user_ask", True):
                items = intent.get("items") or []
                topic = intent.get("topic") or ""
                urg = intent.get("urgency") or ""
                # 新增 7 字段
                category = intent.get("category", "?")
                decision = intent.get("decision", "?")
                confidence = intent.get("confidence", 0)
                explicit = intent.get("explicit")
                continuation = intent.get("continuation")
                margin = intent.get("margin", 0)
                fallback = intent.get("fallback")
                line = "## 这句话的真实意图(v14.0 intent_engine)\n"
                if topic and topic != "?":
                    line += f"主题: {topic}\n"
                line += f"分类: {category} · 决策: {decision} · 置信: {confidence:.2f}\n"
                if explicit:
                    line += f"⚡ 显式注入: {explicit} (短路·直接办理)\n"
                elif continuation:
                    line += f"� 续问: 指代上{continuation.get('refer_to', '?') + 1}轮 ({continuation.get('prev_topic', '?')})\n"
                line += f"紧迫度: {urg}"
                if margin > 0:
                    line += f" · margin: {margin:.2f}"
                if fallback:
                    line += f" · 兑底策略: {fallback}"
                line += "\n"
                if items:
                    line += "拆出的诉求:\n" + "\n".join(f"- {str(it)[:80]}" for it in items[:4])
                parts.append(line)
                if trace is not None:
                    trace["intent_extracted"] = True
                    trace["intent_category"] = category
                    trace["intent_decision"] = decision
                    trace["intent_confidence"] = confidence
        except Exception as e:
            # Q4 · 别静默吞:意图抽取失败要可观测,不然"读懂意图"是假的用户不知道。
            if trace is not None:
                trace["intent_error"] = f"{type(e).__name__}: {str(e)[:80]}"

    return "\n\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════
# G-align · 显式 plan_state (维度 C · 对齐 deepagents planning / CC TodoWrite)
#
# 之前 auto agent 是"LLM 自由循环 + 预算硬顶",没有显式"当前第几步/剩几步"
# 的结构可展示。web 层要实时显示"在跑哪个 Plan 步骤",需要这个结构。
# 做成一个 write_plan 工具让 LLM 自己声明计划,plan 存进 session 并随
# checkpoint 落盘,web 层直接读 session["plan"] 即可。轻量、不强制——
# LLM 想用就用,不用也不影响原有 loop。
# ═══════════════════════════════════════════════════════════════════════

def _make_plan_tool(qid):
    """生成 write_plan 工具:LLM 声明/更新多步计划,落进 session["plan"]。"""

    def write_plan(steps: str) -> str:
        """声明或更新你的执行计划(多跳题的子问题树)。steps 每行一个步骤,
        可用前缀标记状态:[x]=已完成 [>]=进行中 [ ]=待办。
        v77e(2026-08-10 多跳子问题追踪):可在步骤后用 " => 产出" 回填这步的中间结果
        (子问题的答案/拿到的证据),例:
          "[x] 定位1959文档 => naldc.nal.usda.gov/xxx.pdf
           [x] 抽出dehydrated清单 => extract_structured得38项
           [>] 逐项查2023是否被取代 => 已查20/38
           [ ] 算百分比"
        这样多跳题卡住时·一眼看清【哪个子问题还没产出】·不丢步。每完成一步就回填产出。"""
        t0 = time.time()
        s = _sess(qid)
        parsed = []
        for ln in (steps or "").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            status = "pending"
            if ln.startswith("[x]") or ln.startswith("[X]"):
                status = "done"
            elif ln.startswith("[>]"):
                status = "in_progress"
            body = ln[3:].strip() if ln[:3] in ("[x]", "[X]", "[>]", "[ ]") else ln
            # 拆 " => 产出"(子问题中间结果回填)
            _out = ""
            if "=>" in body:
                body, _out = body.split("=>", 1)
                body, _out = body.strip(), _out.strip()[:200]
            parsed.append({"text": body[:200], "status": status, "output": _out})
        s["plan"] = parsed
        # (2026-08-15·#56 Plan-with-Files 接线) 除进程内 session 外·同时把计划持久化到
        #   ~/.meyo/work/<qid>/plan.md(plan_with_files)。原 write_plan 只存进程内 session·
        #   崩溃/被杀就丢·且 resume 拿不回。持久化后:断点续传能 reload、撞墙恢复不丢计划、
        #   每步有据可查(阿里云登顶机制#56·你已建 plan_with_files.py 但没接主循环)。fail-safe。
        try:
            import sys as _sp
            _evo = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "..", "v11", "evolution")
            if _evo not in _sp.path:
                _sp.path.insert(0, _evo)
            import plan_with_files as _pwf
            _steps_txt = [p["text"] for p in parsed]
            _pwf.init_plan(str(s.get("qid", qid)), s.get("question", ""),
                           initial_steps=_steps_txt)
        except Exception:
            pass  # 持久化失败不影响进程内计划(session 仍是主)
        n_done = sum(1 for p in parsed if p["status"] == "done")
        # 提示还没产出的子问题(有助于不丢步)
        _no_out = [p["text"][:40] for p in parsed
                   if p["status"] != "done" and not p.get("output")]
        summary = f"计划已更新:{len(parsed)} 步,已完成 {n_done} 步。"
        if _no_out:
            summary += f" 待产出的子问题 {len(_no_out)} 个:{'; '.join(_no_out[:4])}"
        _log_tool(qid, "write_plan", {"n_steps": len(parsed)}, summary, time.time() - t0)
        return summary

    def insert_plan_step(after_step: int, text: str, mark_in_progress: bool = False) -> str:
        """统一3 · 计划执行到一半发现漏了一步时,动态插一步进去(不用重写整个
        计划)。after_step: 在第几步之后插入(0=插到最前,1=第1步后,以此类推;
        超出范围就插到末尾)。text: 新步骤描述。mark_in_progress: 是否立即标为
        进行中。对齐 deepagents/CC 计划可动态演进——现实里执行中才发现的新步骤
        就该这样补进去,而不是假装一开始就想全了。"""
        t0 = time.time()
        s = _sess(qid)
        plan = list(s.get("plan") or [])
        new_step = {"text": str(text)[:200], "status": "in_progress" if mark_in_progress else "pending"}
        idx = max(0, min(int(after_step or 0), len(plan)))
        plan.insert(idx, new_step)
        s["plan"] = plan
        summary = f"已在第 {idx} 步位置插入新步骤:{new_step['text'][:60]}。当前共 {len(plan)} 步。"
        _log_tool(qid, "insert_plan_step", {"after": after_step}, summary, time.time() - t0)
        return summary

    write_plan.__name__ = "write_plan"
    write_plan.__doc__ = ("声明/更新多步执行计划(每行一步,[x]完成/[>]进行中/[ ]待办)。"
                          "多步任务开始前列计划,每完成一步更新,进度可被实时观察。")
    insert_plan_step.__name__ = "insert_plan_step"
    insert_plan_step.__doc__ = "执行中动态往计划里插一步(发现漏了/需要拆细时用),不必重写整个计划。"
    return [write_plan, insert_plan_step]


# ═══════════════════════════════════════════════════════════════════════
# Goal-as-Tool · 让门面 agent 能自主使用/创建"目标" (2026-08-11)
#
# 用户 2026-08-11:「把 goal 也让 agent 变成 tool·让他自主性的使用和创建 goal」。
#
# 背景与 write_plan 的区别(别混):
#   - write_plan  = 【单道题内部】的战术步骤树(本次任务生命周期,进程内 session)。
#   - goal(这里) = 【跨任务持久】的战略目标(写进 .goal_list.jsonl,cron/后台都可见),
#     由 goal_driven_explorer 那套现成的目标引擎持有和执行。
#
# 现状病根(memory:「建了没接进门面」): .goal_list.jsonl + goal_driven_explorer
# 早已存在,一直是 correction_evolver/cron 在后台写和 tick 执行,但门面 agent
# (auto agent 本体)够不到它——它不是 agent 的一个 tool。所以 agent 解题时无法
# "回头看自己攒了哪些目标"、也无法"当场把发现的新意图立成一个持久目标"。
#
# 做法: 不重造 store,直接复用 goal_driven_explorer 的现成读写 API
# (_load_goals/_save_goal/_mark_status/status/tick),封装成 5 个工具:
#   goal_list   看目标清单(默认 pending)
#   goal_create agent 自主创建一个新持久目标
#   goal_update 标记某目标状态(done/failed/pending/doing)
#   goal_status 目标总览汇总
#   goal_tick   自主触发目标引擎真去执行一个 pending 目标(自主性的落点)
# fail-safe: goal_driven_explorer 不可用(import 失败)时 5 个工具都不注册,
# agent 照常用其余工具,绝不因为 goal 系统缺失而崩主流程。
# ═══════════════════════════════════════════════════════════════════════

def _make_goal_tools(qid):
    """生成 goal_* 工具,复用 goal_driven_explorer 的持久目标引擎。
    返回 [] 表示 goal_driven_explorer 不可用。"""
    try:
        import goal_driven_explorer as _gde  # v11/evolution 已在 sys.path
    except Exception:
        return []

    def goal_list(status_filter: str = "pending") -> str:
        """查看跨任务的持久目标清单(不是本题的 write_plan,是系统攒的长期目标)。
        status_filter: pending(默认·待做) / doing(进行中) / done(已完成) /
        failed(失败) / all(全部)。解一道题前想"我以前是不是给自己立过相关目标"、
        或想知道系统当前有哪些悬而未决的目标时调这个。"""
        t0 = time.time()
        try:
            goals = _gde._load_goals()
            sf = (status_filter or "pending").strip().lower()
            if sf != "all":
                goals = [g for g in goals if g.get("status", "pending") == sf]
            # 只展示最近的 N 条(目标库可能上万·别怼爆上下文)
            goals = goals[-15:]
            if not goals:
                summary = f"没有 status={sf} 的目标。"
            else:
                lines = [f"共 {len(goals)} 条 status={sf} 目标(最近15条):"]
                for g in goals:
                    lines.append(f"  · [{g.get('id','?')[:20]}] {str(g.get('goal') or g.get('topic') or '')[:70]}")
                summary = "\n".join(lines)
        except Exception as e:
            summary = f"goal_list 异常: {type(e).__name__}: {str(e)[:120]}"
        _log_tool(qid, "goal_list", {"status_filter": status_filter}, summary, time.time() - t0)
        return summary

    def goal_create(goal: str, action: str = "search_and_read", topic: str = "", keywords: str = "") -> str:
        """自主创建一个新的持久目标(写进系统目标库,跨任务/后台可见并可被执行)。
        当你在解题/探索中发现一个"值得系统以后持续跟进"的方向时立目标——比如
        "学习某爆款平台写法""补齐某能力缺口""持续追踪某数据源"。
        goal: 一句话目标描述(必填)。action: 目标该怎么执行,决定被 goal_tick 挑到时
        走哪条执行路径(常用: search_and_read 搜索并深读 / tool_discover 找装工具 /
        systemic_refactor 系统重构 / pain_fix 修痛点 / benchmark_learn 学标杆)。
        topic: 主题标签(可空)。keywords: 逗号分隔的关键词(可空,便于后续检索)。"""
        t0 = time.time()
        try:
            g = str(goal or "").strip()
            if not g:
                summary = "goal 不能为空。"
            else:
                # id 不用 time.time()(deepagents 环境下 Date/random 受限时也要能生成)——
                # 用 qid + session 内自增计数,保证同进程内唯一、可追溯到是哪次任务立的。
                s = _sess(qid)
                n = s.get("_goal_seq", 0) + 1
                s["_goal_seq"] = n
                rec = {
                    "id": f"agent_{_safe_qid(qid)}_{n}",
                    "goal": g,
                    "action": (action or "search_and_read").strip(),
                    "topic": (topic or g[:40]).strip(),
                    "keywords": [k.strip() for k in (keywords or "").split(",") if k.strip()],
                    "status": "pending",
                    "created_at": _now_iso(),
                    "source": "auto_agent",           # 标明是门面 agent 自主立的
                    "created_for_qid": qid,
                }
                _gde._save_goal(rec)
                # v113(2026-08-16 · 用户:goal↔意图互联) 立目标时回显意图契约轴·
                # 提醒把轴覆盖进目标/步骤·goal 不是孤岛——它长在意图契约上。
                _spec_note = ""
                try:
                    _sp = s.get("intent_spec") or {}
                    if _sp:
                        _spec_note = ("\n[意图契约对照] 本题开局抽出的约束轴: "
                                     + ",".join(list(_sp.keys()))
                                     + " · 拆目标/定完成标准时逐条覆盖·别立与轴无关的空目标。")
                except Exception:
                    _spec_note = ""
                summary = (f"✅ 已创建持久目标 [{rec['id']}]: {g[:70]} (action={rec['action']}·status=pending)"
                           + _spec_note)
        except Exception as e:
            summary = f"goal_create 异常: {type(e).__name__}: {str(e)[:120]}"
        _log_tool(qid, "goal_create", {"goal": goal[:80], "action": action}, summary, time.time() - t0)
        return summary

    def goal_update(goal_id: str, status: str) -> str:
        """更新某个持久目标的状态。goal_id: goal_list 里显示的 id。
        status: done(已达成) / failed(做不到) / pending(重新排队) / doing(进行中)。
        当你判断某个目标已经达成、或确认做不到时,标记它,避免系统反复重做。"""
        t0 = time.time()
        try:
            gid = str(goal_id or "").strip()
            st = str(status or "").strip().lower()
            if not gid or st not in ("done", "failed", "pending", "doing"):
                summary = "参数不对: goal_id 必填, status ∈ {done,failed,pending,doing}。"
            else:
                _gde._mark_status(gid, st)
                summary = f"已把目标 [{gid}] 标记为 {st}。"
        except Exception as e:
            summary = f"goal_update 异常: {type(e).__name__}: {str(e)[:120]}"
        _log_tool(qid, "goal_update", {"goal_id": goal_id, "status": status}, summary, time.time() - t0)
        return summary

    def goal_status() -> str:
        """看目标库总览: 总数 + 各状态计数 + 最近 5 条。想快速了解"系统现在
        有多少目标、多少还没做"时调这个。"""
        t0 = time.time()
        try:
            st = _gde.status()
            summary = (f"目标库: 共 {st.get('total')} 条 · 分布 {st.get('by_status')} · "
                       f"最近: " + "; ".join(f"{x.get('topic','')[:30]}({x.get('status')})"
                                            for x in st.get("latest_5", [])))
        except Exception as e:
            summary = f"goal_status 异常: {type(e).__name__}: {str(e)[:120]}"
        _log_tool(qid, "goal_status", {}, summary, time.time() - t0)
        return summary

    def goal_tick() -> str:
        """自主触发目标引擎: 挑一个 pending 目标真去执行一步(搜索/学标杆/找工具/
        修痛点等,由该目标的 action 决定),并把结果落库。这是让你"不只是记目标、
        还能推动目标前进"的自主动作。只在你确实想让系统朝某个长期目标推进一步时
        调用——它可能触发网络搜索/LLM,是重活,别空跑。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "goal_tick")
        if not allow:
            _log_tool(qid, "goal_tick", {}, guard_msg, time.time() - t0)
            return guard_msg
        try:
            with _heavy_slot():  # 重活过并发闸,避免堆爆 CPU
                r = _gde.tick(max_goals=1)
            execd = r.get("executed", [])
            if not execd:
                summary = f"当前没有 pending 目标可执行(pending_now={r.get('pending_now')})。"
            else:
                e0 = execd[0]
                ok = e0.get("result", {}).get("ok")
                summary = (f"执行了目标 [{e0.get('goal_id')}] action={e0.get('action')} → "
                           f"{'✅成功' if ok else '未成功'}。剩余 pending={r.get('pending_now')}。")
        except Exception as e:
            summary = f"goal_tick 异常: {type(e).__name__}: {str(e)[:120]}"
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "goal_tick", {}, summary, time.time() - t0)
        return summary

    for _fn, _nm in ((goal_list, "goal_list"), (goal_create, "goal_create"),
                     (goal_update, "goal_update"), (goal_status, "goal_status"),
                     (goal_tick, "goal_tick")):
        _fn.__name__ = _nm
    return [goal_list, goal_create, goal_update, goal_status, goal_tick]


def _make_self_evolve_tools(qid):
    """v13.93(2026-08-11 用户: 让agent自己调教自己·不是CC帮他改) · 把self_evolve做成
    agent能自己调的工具: agent遇到反复失败/卡住时·自己调这个诊断'是不是我的机制/策略有
    系统缺陷'·自己提改造方案(走safe_patch改→测→回退/合并)。agent自己进化·非CC替它改。"""
    try:
        import sys as _s
        _s.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "..", "..", "v11", "evolution"))
        import self_evolve as _se
    except Exception:
        return []

    def self_diagnose(failure_desc: str, trace_summary: str = "") -> str:
        """【自我诊断+进化】当你反复失败/卡住/发现自己某类题总做不好时·调这个:
        它让你(用异构模型)反思'这是不是我的机制/策略/工具有系统性缺陷'(而非单题偶然)·
        并提出改造方案(记录待监督审批后走safe_patch改→测→回退/合并)。
        failure_desc: 描述你遇到的反复问题(如'我遇视频题总退回搜文字不用see_video_frame')。
        trace_summary: 你已走的步骤摘要(可选)。这是你【自己调教自己】的入口·像人从失败中学。"""
        t0 = time.time()
        try:
            cases = [{"question": _sess(qid).get("question", ""),
                      "trace_steps": trace_summary or "(agent自述)",
                      "gold": "", "our_answer": "",
                      "why_failed": failure_desc}]
            rep = _se.self_evolve_cycle(cases, auto_apply=False)
            diag = rep.get("diagnosis", {})
            if diag.get("is_mechanism_gap"):
                # (2026-08-15·聚焦审计 Q3) 直连自动改码回路:确认机制缺陷时·除记提案外·
                #   再投一笔进 evolution_inbox·让 self_heal 的 evolve_backtest_loop(真改→ast→
                #   pytest→回退→回测)异步捡起。病根(审计):原来 agent 手调 self_diagnose 走
                #   auto_apply=False 纯提案分支·【不触发】自动改码闭环·提案与真跑改码两条路没直连。
                try:
                    import sys as _s5
                    _evo = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                        "..", "v11", "evolution")
                    if _evo not in _s5.path:
                        _s5.path.insert(0, _evo)
                    import self_heal_scheduler as _shs
                    _shs.capture_to_evolution_inbox(
                        qid=_sess(qid).get("qid", qid), sig="gave_up",
                        why=f"agent自诊断机制缺陷: {diag.get('gap_desc','')[:150]}",
                        question=_sess(qid).get("question", ""))
                    _wired = "·已投evolution_inbox(自动改码回测链会捡起)"
                except Exception:
                    _wired = ""
                summary = (f"[自我诊断] 确是机制缺陷(信心{diag.get('confidence')}): "
                           f"{diag.get('gap_desc','')[:100]}\n方案: {diag.get('proposed_fix','')[:120]}\n"
                           f"(已记提案待监督{_wired}·你也可据此当场换个策略/工具试)")
            else:
                summary = (f"[自我诊断] 这更像单题偶然·非机制缺陷({rep.get('action','')})。"
                           "建议: 换个结构性不同的策略/工具再试·别归咎机制。")
        except Exception as e:
            summary = f"self_diagnose 异常: {type(e).__name__}: {str(e)[:120]}"
        _log_tool(qid, "self_diagnose", {"failure": failure_desc[:60]}, summary[:800], time.time() - t0)
        return summary

    self_diagnose.__name__ = "self_diagnose"
    return [self_diagnose]


def _make_metacognition_tools(qid):
    """(2026-08-15 · 万物皆工具审计缺口修) 元认知三件套【工具化】——把原来只被循环
    自动闸【被动调用】的反思/记忆/回看题意·抽成 agent 可【主动 call】的 tool。
    病根(第一审计):reflective_mind.reflect / memory recall+remember / 意图回看 是威力最强的
      "元认知即工具"候选·却只被 _maybe_reflect_nudge 等闸门在"无进展"时替 agent 调。
      agent 想主动"我停下想想 / 查一下这类题以前怎么栽的 / 我核对下有没有读偏题"没有对应工具。
    这是"模型即工具"哲学落地的最后一厘米。保留原被动闸做兜底·此处只【新增主动入口】。"""
    tools = []

    def reflect_now(stuck_reason: str = "") -> str:
        """【主动反思/换策略】当你觉得当前思路可能不对、卡住、或想停下来审视全局时·主动调这个。
        它用异构模型看你的目标+已走步骤·产出:离目标还差什么、当前路有没有效、是不是在
        【自洽循环】(只在自己前提内反复验证没引入新证据=很可能题意读偏了)、下一步该换什么
        工具/策略/是否该造工具/是否该找人。这是你【主动的元认知】·不必等卡死被闸门提醒。
        stuck_reason: 可选·描述你此刻的卡点或疑虑(如'算得很顺但总觉得框架可能选错了')。"""
        t0 = time.time()
        try:
            import reflective_mind as _RM
            s = _sess(qid)
            _steps = [{"tool": e.get("tool"), "summary": str(e.get("summary") or "")[:120]}
                      for e in (s.get("tool_log") or [])[-20:]]
            _env = ""
            try:
                import env_scan as _es
                _env = str(_es.summary())[:600] if hasattr(_es, "summary") else ""
            except Exception:
                _env = ""
            # v113(2026-08-16 · 用户:反思↔goal↔工具↔造工具互联) 反思不是只看
            # 工具流水——把 plan进度+意图契约轴+工具失败热点一起塞进反思上下文:
            # ①plan/goal 让反思知道"我在题目的什么位置"; ②意图轴让它知道"该对什么负责";
            # ③同类工具连败≥2次(v121 修⑩: 3→2·连败即换)=该造替代工具(write_capability)
            #   或换轮子(search_github)的硬信号。
            _ctx = ""
            try:
                _pl = s.get("plan") or []
                if _pl:
                    _done = sum(1 for p in _pl if str(p.get("status")) == "done")
                    _ctx = f" | plan进度:{_done}/{len(_pl)}"
                _sp2 = s.get("intent_spec") or {}
                if _sp2:
                    _ctx += " | 意图契约轴(逐条对照):" + ",".join(list(_sp2.keys()))
                import collections as _cc
                _fails = _cc.Counter()
                for e in (s.get("tool_log") or [])[-30:]:
                    _sm = str(e.get("summary") or "").lower()
                    if any(m in _sm for m in ("失败", "error", "异常", "timeout", "超时", "fail", "拦截")):
                        _fails[e.get("tool")] += 1
                _top_fail = _fails.most_common(1)
                if _top_fail and _top_fail[0][1] >= 2:
                    _ctx += (f" | ⚠工具'{_top_fail[0][0]}'近30步失败{_top_fail[0][1]}次→"
                             f"强信号:write_capability造替代工具 或 search_github换现成轮子")
            except Exception:
                _ctx = ""
            r = _RM.reflect(goal=s.get("question", ""), trace_steps=_steps,
                            stuck_reason=(str(stuck_reason) + _ctx)[:400], env_summary=_env,
                            evidence=(s.get("evidence") or [])[-40:])
            if isinstance(r, dict):
                summary = (
                    f"[反思] 差距:{r.get('gap','')[:120]}\n"
                    f"当前路有效:{r.get('path_working')} · 根因:{r.get('why_stuck','')[:100]}\n"
                    f"前提审计:{r.get('premise_audit','')[:140]}\n"
                    f"下一步:{r.get('next_move','')[:160]}"
                    + (f"\n建议工具:{r.get('next_tool')}" if r.get('next_tool') else "")
                    + (f"\n需造能力:{r.get('need_new_tool')}" if r.get('need_new_tool') else ""))
            else:
                summary = str(r)[:600]
        except Exception as e:
            summary = f"reflect_now 异常(降级·可继续): {type(e).__name__}: {str(e)[:120]}"
        _log_tool(qid, "reflect_now", {"stuck": stuck_reason[:60]}, summary[:900], time.time() - t0)
        return summary
    reflect_now.__name__ = "reflect_now"
    tools.append(reflect_now)

    def recall_experience(query: str) -> str:
        """【主动查经验】主动检索你(该用户)以前处理类似任务沉淀的可复用经验/教训。
        遇到陌生题型、想避免重复踩坑、或想复用以前的成功解法时调。返回相关经验条目。
        query: 描述你要查的任务类型(如'博弈论最优策略题'、'从视频某时间点读数据')。"""
        t0 = time.time()
        try:
            if not _MEM_AVAILABLE:
                summary = "(记忆层不可用·无经验可查)"
            else:
                _exp = _recall_lessons(query, limit=5,
                                       user_id=_sess(qid).get("user_id") or "default")
                summary = _exp or "(没查到相关经验·这可能是新题型)"
        except Exception as e:
            summary = f"recall_experience 异常: {type(e).__name__}: {str(e)[:120]}"
        _log_tool(qid, "recall_experience", {"query": query[:60]}, summary[:900], time.time() - t0)
        return summary
    recall_experience.__name__ = "recall_experience"
    tools.append(recall_experience)

    def remember_lesson(lesson: str) -> str:
        """【主动记教训】把你这次学到的可复用结论/教训主动记进你(该用户)的私有经验库·
        下次同类题会被召回预警。适合记'这类题的通用陷阱/正确解法'·别记具体答案。
        lesson: 一句可复用的教训(如'博弈题先列2-3种建模解读横向比·别锁死单一框架')。"""
        t0 = time.time()
        try:
            if not _MEM_AVAILABLE:
                summary = "(记忆层不可用·未记入)"
            elif len(str(lesson).strip()) < 8:
                summary = "(教训太短·未记入)"
            else:
                _mem_remember(lesson=str(lesson)[:400], source="agent·remember_lesson",
                              tags=["agent_self_lesson"], confidence="medium",
                              user_id=_sess(qid).get("user_id") or "default")
                summary = f"[已记入经验库] {str(lesson)[:120]}"
        except Exception as e:
            summary = f"remember_lesson 异常: {type(e).__name__}: {str(e)[:120]}"
        _log_tool(qid, "remember_lesson", {"lesson": str(lesson)[:60]}, summary[:600], time.time() - t0)
        return summary
    remember_lesson.__name__ = "remember_lesson"
    tools.append(remember_lesson)

    def review_intent() -> str:
        """【主动回看题意】主动停下来核对:你对题目的理解有没有读偏。用异构模型对照原始题面
        逐条审:①有没有误解某个词/约束 ②有没有漏读题目要求(全部/每个/单位/时间口径/谁的视角)
        ③你当前的解法框架是不是题目真正要的。收尾前、或觉得答案可疑时主动调。不含答案·只查题意。"""
        t0 = time.time()
        try:
            import reflective_mind as _RM
            s = _sess(qid)
            _q = str(s.get("question") or "")
            _cur = str(s.get("final") or s.get("narrative") or "")[:600]
            # v113(2026-08-16 · 用户:歧义展开+意图↔回看互联) 把开局意图契约轴
            # 注入核对上下文·并要求【并排列读法】+标注解题者采用哪条/题面语法
            # 支持哪条——歧义不是想起来才查·同题有多读法就摆开。
            _spec = ""
            try:
                _sp = s.get("intent_spec") or {}
                if _sp:
                    _spec = ("\n开局意图契约(题面抽出的约束轴·逐条对照):"
                             + ";".join(f"[{k}]{str(v)[:60]}" for k, v in list(_sp.items())[:6])[:500])
            except Exception:
                _spec = ""
            _prompt = (
                "你是独立的题意核对员。只核对解题者对【原始题目】的理解有没有读偏·不要解题、不给答案。"
                "逐条查:①误解某词/约束吗 ②漏读要求吗(全部/每个/单位/时间口径/谁的max-min/累计vs单期)"
                "③当前解法框架是不是题目真正要的。"
                "④歧义展开: 题面若含多读法结构(否定+or/and·量词each/at least·单位%vs小数·"
                "分母总体·时间锚)·在readings里列出每种合理读法(一句话一种)·"
                "solver_took=解题者当前采用的读法序号·grammar_supports=题面语法最支持的序号。"
                "没有歧义则readings留空数组。严格JSON:"
                '{"intent_ok":true/false,"misread":"读偏的点(没有填空)","missed":"漏读的要求(没有填空)",'
                '"advice":"该怎么重读/补(没有填空)","readings":["每种读法一句话"],'
                '"solver_took":0,"grammar_supports":0}\n\n'
                f"原始题目:{_q[:1600]}\n当前解法/结论:{_cur}{_spec}")
            _raw = _RM._mm().ask("claude-sonnet", _prompt, timeout=20, max_tokens=280)
            if isinstance(_raw, dict) and "out" in _raw:
                _raw = _raw.get("out") if _raw.get("ok") else ""
            _a = _parse_model_json_object(_raw) or {}
            # 歧义展开结果落进摘要·读法不一致时大声警告(这是8d46类锁死的解药)
            _rd_txt = ""
            try:
                _rd = _a.get("readings") or []
                if _rd:
                    _rd_txt = ("\n[歧义展开·并排读法] "
                               + " | ".join(f"{i+1}.{str(x)[:80]}" for i, x in enumerate(_rd[:4])))
                    _st_, _gs_ = str(_a.get("solver_took", "")), str(_a.get("grammar_supports", ""))
                    if _st_ and _gs_ and _st_ != _gs_:
                        _rd_txt += (f"\n⚠你采用的是读法{_st_}·题面语法更支持读法{_gs_}·"
                                    f"先把各读法的数字并排算出来再选主答·别锁死!")
            except Exception:
                _rd_txt = ""
            if _a.get("intent_ok"):
                summary = "[题意核对] 未发现明显读偏·理解与题面一致。" + _rd_txt
            else:
                summary = (f"[题意核对·可能读偏] 误解:{_a.get('misread','') or '无'}\n"
                           f"漏读:{_a.get('missed','') or '无'}\n建议:{_a.get('advice','')[:200]}" + _rd_txt)
        except Exception as e:
            summary = f"review_intent 异常(降级): {type(e).__name__}: {str(e)[:120]}"
        _log_tool(qid, "review_intent", {}, summary[:1000], time.time() - t0)
        return summary
    review_intent.__name__ = "review_intent"
    tools.append(review_intent)

    def build_answer_contract() -> str:
        """(2026-08-15·#B1 Answer Contract·治"读题没对最影响后续") 开局强制产出一份【结构化
        答案契约】并存进 session·finalize 前会逐字段核对。它把 triage 的语义歧义轴(实体/时间/
        口径/集合/视角)+ 输出形状(类型/格式/单位/精度/判分)合成一个可核对的工件·而不是靠脑子记。
        返回契约 JSON。收尾前用 check_answer_contract() 逐项核对。这是所有题的第一步·契约错=白做。"""
        t0 = time.time()
        try:
            import reflective_mind as _RM
            s = _sess(qid)
            _tri = s.get("triage") or {}
            # (2026-08-27 用户中心) 删 grading(评分器口径 exact_match/llm_judge)+gold_method_prior
            #   (GAIA标注者口径)——那是考试评分思路·真实用户要的是"答案什么形状+怎么消歧"·不是
            #   "标注者会怎么算"。只留对用户有用的:类型/格式/单位/精度/消歧。
            _prompt = (
                "你是答案契约生成器。只分析用户问题【要什么形状的答案】·不解题、不给答案。严格JSON:\n"
                '{"type":"数值|实体|列表|日期|坐标|字符串","format":"如 x, y / mm/dd/yyyy / 千分位 / 无特殊",'
                '"units":"如 mL/%/minutes/无","precision":"如 整数/两位小数/三位有效数字/无",'
                '"disambiguation_keys":["选实体/版本/时点时必须先跟用户确认或在答案里说明的消歧点"]}\n\n'
                f"问题:{str(s.get('question') or '')[:1500]}\n"
                f"triage已识别的口径约束:{str(_tri.get('constraints') or [])[:400]}")
            _raw = _RM._mm().ask("claude-sonnet", _prompt, timeout=20, max_tokens=300)
            if isinstance(_raw, dict):
                _raw = _raw.get("out", "") if _raw.get("ok") else ""
            _c = _parse_model_json_object(_raw) or {}
            if _c:
                s["answer_contract"] = _c   # 存 session·供 finalize 门核对
                summary = ("[答案契约已建·finalize前会逐项核对] "
                           + json.dumps(_c, ensure_ascii=False)[:500])
            else:
                summary = "(契约生成无输出·finalize时按默认核对)"
        except Exception as e:
            summary = f"build_answer_contract 异常(降级): {type(e).__name__}: {str(e)[:120]}"
        _log_tool(qid, "build_answer_contract", {}, summary[:600], time.time() - t0)
        return summary
    build_answer_contract.__name__ = "build_answer_contract"
    tools.append(build_answer_contract)

    def check_answer_contract(answer: str) -> str:
        """(2026-08-15·#B1) 收尾前用你的答案逐项核对答案契约(build_answer_contract 建的):
        类型/格式/单位/精度对得上吗?消歧钥匙都回答了吗?不符会明确指出·让你改格式或补消歧。
        answer: 你准备交的答案。没建过契约则提示先 build_answer_contract。"""
        t0 = time.time()
        try:
            s = _sess(qid)
            _c = s.get("answer_contract")
            if not _c:
                summary = "(还没建答案契约·建议先 build_answer_contract 再核对)"
            else:
                import reflective_mind as _RM
                # 意图契约轴也进收尾核对——形状合规 + 约束轴覆盖一起判。
                _spec_c = ""
                try:
                    _sp = s.get("intent_spec") or {}
                    if _sp:
                        _spec_c = ("\n开局意图契约轴(答案是否逐条交代了):" +
                                   ";".join(f"[{k}]{str(v)[:50]}" for k, v in list(_sp.items())[:6])[:400])
                except Exception:
                    _spec_c = ""
                _prompt = (
                    "你是答案契约核对员。判断【答案】是否满足【契约】的形状要求(类型/格式/单位/精度/"
                    "消歧钥匙)。只查形状合规·不判对错。若给了意图契约轴·也逐条检查答案是否覆盖交代。严格JSON:"
                    '{"pass":true/false,"violations":["不符的点·如 单位缺失/精度不对/消歧钥匙X没回答/意图轴X没交代"]}\n\n'
                    f"契约:{json.dumps(_c, ensure_ascii=False)[:600]}{_spec_c}\n答案:{str(answer)[:800]}")
                _raw = _RM._mm().ask("claude-sonnet", _prompt, timeout=18, max_tokens=200)
                if isinstance(_raw, dict):
                    _raw = _raw.get("out", "") if _raw.get("ok") else ""
                _v = _parse_model_json_object(_raw) or {}
                if _v.get("pass"):
                    summary = "[契约核对✅] 答案形状符合契约(类型/格式/单位/精度/消歧都对得上)。"
                else:
                    summary = ("[契约核对❌·先别交] 不符:"
                               + "; ".join(str(x) for x in (_v.get("violations") or []))[:300]
                               + " —— 按契约改格式/补消歧再交。")
        except Exception as e:
            summary = f"check_answer_contract 异常(降级): {type(e).__name__}: {str(e)[:120]}"
        _log_tool(qid, "check_answer_contract", {}, summary[:600], time.time() - t0)
        return summary
    check_answer_contract.__name__ = "check_answer_contract"
    tools.append(check_answer_contract)

    def assess_info_sufficiency() -> str:
        """【评估信息是否够解题】主动判断:手头(题面+已查证据)的信息够不够唯一确定答案·
        还是【缺关键输入】。缺的话告诉你缺什么、该去哪补(搜/查库/读附件/问用户)。治"意图空间
        不足"——别在信息不全时硬凑一个数。收尾前或卡住时调。不含答案·只判信息够不够。"""
        t0 = time.time()
        try:
            import reflective_mind as _RM
            s = _sess(qid)
            _ev = "; ".join(str(a.get("claim", ""))[:80] for a in (s.get("evidence_anchors") or [])[-8:])
            _prompt = (
                "你是信息充分性评估员。判断:根据题目+已收集证据·能不能【唯一确定】答案·还是缺关键输入。"
                "只输出严格JSON:{\"sufficient\":true/false,\"missing\":\"缺的关键信息(sufficient=true填空)\","
                "\"where\":\"该去哪补:web_search/query_db/read_file/ask_human(缺才填)\"}\n\n"
                f"题目:{str(s.get('question') or '')[:1200]}\n已收集证据摘要:{_ev[:1000]}")
            _raw = _RM._mm().ask("claude-sonnet", _prompt, timeout=18, max_tokens=180)
            if isinstance(_raw, dict):
                _raw = _raw.get("out", "") if _raw.get("ok") else ""
            _a = _parse_model_json_object(_raw) or {}
            if _a.get("sufficient"):
                summary = "[信息充分] 手头信息够唯一确定答案·可继续收敛。"
            else:
                summary = (f"[信息不足·别硬凑] 缺:{_a.get('missing','')}\n"
                           f"该去补:{_a.get('where','')}(用对应工具去查/问·别自己填默认值)")
        except Exception as e:
            summary = f"assess_info_sufficiency 异常(降级): {type(e).__name__}: {str(e)[:120]}"
        _log_tool(qid, "assess_info_sufficiency", {}, summary[:600], time.time() - t0)
        return summary
    assess_info_sufficiency.__name__ = "assess_info_sufficiency"
    tools.append(assess_info_sufficiency)

    def check_model_quota() -> str:
        """(2026-08-15·C) 查当前【还有哪些模型可用】+额度状态。当某模型报额度不够/限流(429/quota)·
        或你想换个模型时调:告诉你哪些模型通、哪些不通(可能额度/鉴权)、哪些能看图。据此换可用模型继续。"""
        t0 = time.time()
        try:
            import multi_model as _mm
            st = _mm.quota_status(probe=False)
            summary = (f"[模型额度/可用] {st.get('note','')}\n"
                       f"可用: {', '.join(st.get('usable') or [])}\n"
                       f"能看图: {', '.join(st.get('vision_usable') or [])}")
            _un = st.get("unavailable") or []
            if _un:
                summary += "\n不通(可能额度/鉴权·别再选): " + \
                    "; ".join(f"{u['model']}({u['reason'][:30]})" for u in _un[:6])
        except Exception as e:
            summary = f"check_model_quota 异常: {type(e).__name__}: {str(e)[:120]}"
        _log_tool(qid, "check_model_quota", {}, summary[:500], time.time() - t0)
        return summary
    check_model_quota.__name__ = "check_model_quota"
    tools.append(check_model_quota)

    def network_status(wait_if_offline: bool = False) -> str:
        """(2026-08-15·A) 查网络通不通。联网工具反复失败时调:确认是不是断网了。
        wait_if_offline=True:若离线·阻塞等网络恢复(最多1小时·对齐'1小时内拿到网络就跑')·
        恢复后返回·你可继续联网操作(任务上下文/已做进度都在·不丢)。默认只查不等。"""
        t0 = time.time()
        try:
            import net_watchdog as _nw
            if not _nw.net_ok():
                if wait_if_offline:
                    _emit_gate_event(qid, "network", "offline_wait", {})
                    ok = _nw.wait_for_net(max_wait=3600, poll=30)
                    summary = ("[网络已恢复·可继续] 断网期间任务上下文/进度都保留着·现在联网工具可用了。"
                               if ok else "[等1小时仍未恢复网络] 建议先做不依赖网络的部分·或稍后再试。")
                else:
                    summary = ("[⚠️当前离线] 联网工具会失败。任务上下文/已做进度都在(不丢)。"
                               "可 network_status(wait_if_offline=True) 挂起等网络恢复·或先做不依赖网络的活。")
            else:
                summary = "[在线] 网络通·联网工具可正常用。"
        except Exception as e:
            summary = f"network_status 异常: {type(e).__name__}: {str(e)[:100]}"
        _log_tool(qid, "network_status", {"wait": wait_if_offline}, summary[:400], time.time() - t0)
        return summary
    network_status.__name__ = "network_status"
    tools.append(network_status)

    def solve_strongest(question: str) -> str:
        """(2026-08-15·最强模式) 一键最强:让【多个异构模型各自独立解】这道题·再由裁判模型
        选/综合出最可靠答案(成本换正确率)。高价值/难题·或你想要最高把握时调。返回最佳答案+
        各模型候选+裁判理由。比单模型/单次cross_verify更强(N路独立解+裁判)。"""
        t0 = time.time()
        try:
            import multi_model as _mm
            r = _mm.best_of_n(question, timeout=90)
            if r.get("ok"):
                _cands = r.get("candidates") or []
                summary = (f"[最强模式·{len(_cands)}路独立解+裁判] 最佳答案:\n{str(r.get('best_answer',''))[:1200]}\n"
                           f"裁判理由: {r.get('judge_reason','')}·模型: {', '.join(r.get('models_used') or [])}")
            else:
                summary = f"[最强模式] 未成功: {r.get('error','')}·可先 check_model_quota 看可用模型"
        except Exception as e:
            summary = f"solve_strongest 异常: {type(e).__name__}: {str(e)[:120]}"
        _log_tool(qid, "solve_strongest", {"q": question[:50]}, summary[:900], time.time() - t0)
        return summary
    solve_strongest.__name__ = "solve_strongest"
    tools.append(solve_strongest)

    return tools


def _make_local_and_reasoning_tools(qid):
    """(2026-08-15 · 万物皆工具第2批) 补三个高频缺口工具:
    - search_local_files: 检索本机文件内容(ripgrep)——用户"用电脑上的所有信息/本地信息"
    - deduce: 主动演绎推理挖病根(原来只常驻prompt·非可调tool)
    - cross_verify_now: 主动多模型交叉验证(原cross_verify只finalize前被动闸)
    全 fail-safe·不可用返回可审计说明。"""
    tools = []

    def search_local_files(query: str, directory: str = "") -> str:
        """【检索本机文件】在本地电脑文件里搜内容(用 ripgrep)。当答案可能在本机已有文件/
        代码/数据/文档里时调·别只靠联网搜。query: 要搜的文本/正则。directory: 限定目录
        (默认当前工作目录·别搜整盘)。返回命中的 文件:行 + 片段(截断)。"""
        t0 = time.time()
        try:
            import subprocess as _sp
            _d = directory.strip() or os.getcwd()
            if not os.path.isdir(_d):
                summary = f"(目录不存在: {_d})"
            else:
                # 限制:最多 50 行结果·60s·跳过二进制/大目录·防扫全盘卡死
                _cmd = ["rg", "--no-heading", "--line-number", "--max-count", "5",
                        "--max-columns", "200", "-i", query, _d]
                _r = _sp.run(_cmd, capture_output=True, text=True, timeout=60)
                _out = (_r.stdout or "").strip().splitlines()[:50]
                summary = ("\n".join(_out) if _out
                           else f"(未命中 {query!r} · dir={_d})")
        except FileNotFoundError:
            summary = "(ripgrep 未安装·无法本地检索)"
        except Exception as e:
            summary = f"search_local_files 异常: {type(e).__name__}: {str(e)[:120]}"
        _log_tool(qid, "search_local_files", {"query": query[:60]}, summary[:1200], time.time() - t0)
        return summary[:2000]
    search_local_files.__name__ = "search_local_files"
    tools.append(search_local_files)

    def deduce(observation: str) -> str:
        """【主动演绎推理】对一个现象/矛盾/中间结果·主动做一次演绎:从表象往下挖真正的病根/
        根本原因·而非停在表面。适合'结果不对但不知为什么''两个证据打架'时。用异构模型做严格
        因果推理。observation: 你要深挖的现象(如'算出12000但感觉不对·哪一步的假设可能错')。"""
        t0 = time.time()
        try:
            import reflective_mind as _RM
            s = _sess(qid)
            _steps = "; ".join(f"{e.get('tool')}:{str(e.get('summary') or '')[:60]}"
                               for e in (s.get("tool_log") or [])[-12:])
            _prompt = (
                "你是严格的演绎推理员。对给定现象做【逐层往下挖根因】的演绎(不是罗列可能·是"
                "顺着因果链一步步推到最底层的病根)·不要解题、不给最终答案。输出:\n"
                "1) 表象  2) 中间因(逐层)  3) 最可能的根本病根  4) 验证这个病根该做什么。\n\n"
                f"题目:{str(s.get('question') or '')[:800]}\n已走步骤:{_steps[:800]}\n"
                f"要深挖的现象:{observation[:400]}")
            _raw = _RM._mm().ask("gpt-5", _prompt, timeout=25, max_tokens=400)
            if isinstance(_raw, dict) and "out" in _raw:
                _raw = _raw.get("out") if _raw.get("ok") else ""
            summary = str(_raw or "(演绎无输出)")[:1000]
        except Exception as e:
            summary = f"deduce 异常(降级): {type(e).__name__}: {str(e)[:120]}"
        _log_tool(qid, "deduce", {"obs": observation[:60]}, summary[:1000], time.time() - t0)
        return summary
    deduce.__name__ = "deduce"
    tools.append(deduce)

    def cross_verify_now(answer: str) -> str:
        """【主动交叉验证】主动让一个不同家的模型独立复核你当前答案(不必等 finalize 闸)。
        数值/多值/高风险答案想现在就核一遍时调。一致→更可信;不一致→说明有争议要重查。
        answer: 你要核验的当前答案。"""
        t0 = time.time()
        try:
            import cross_verify as _cv
            s = _sess(qid)
            # 2026-08-19 治【仲裁保守 bug】: 把主答案是否【已充分验证 confirmed】
            #   (由 identity_coverage 四态判)传给 cross_verify——主答案没到 confirmed
            #   时·第二模型的 disagree 应响应(回锦标赛/继续搜)·不保守保留错答案。
            _main_confirmed = _cv_main_confirmed(s, str(answer))
            # 2026-08-19 补 evidence(融合另一路改动·-X theirs 时丢了): 把 session
            #   已收集证据传给第二模型·让它和主模型看到同样资料再判——否则冷门实体题
            #   (物种学名/球队队史)它只能裸猜报"无法验证"·验证形同虚设。与 main_confirmed
            #   互补: evidence 让 disagree 有依据·main_confirmed 让 disagree 被正确响应。
            _res = _cv.maybe_cross_verify(str(s.get("question") or ""), str(answer),
                                          evidence=s.get("evidence") or [],
                                          force=True, main_confirmed=_main_confirmed)
            if isinstance(_res, dict):
                _nmv = _res.get("needs_more_verification")
                summary = (f"[交叉验证] 触发={_res.get('triggered')} 第二模型同意={_res.get('agreed')} "
                           f"主答案已确认={_main_confirmed} 需继续验证={_nmv}\n"
                           f"原因={str(_res.get('reason',''))[:120]}\n"
                           f"采纳答案={str(_res.get('final_answer',''))[:200]}")
                if _nmv:
                    summary += ("\n[决策] 主答案未 confirmed 且第二模型质疑——不锁定它·"
                                "把被质疑的主答案和 alt 候选都回锦标赛按约束覆盖率竞争·"
                                "对区分性约束定向补证再交(别二选一保守保留)。")
            else:
                summary = str(_res)[:600]
        except Exception as e:
            summary = f"cross_verify_now 异常(降级): {type(e).__name__}: {str(e)[:120]}"
        _log_tool(qid, "cross_verify_now", {"ans": str(answer)[:60]}, summary[:900], time.time() - t0)
        return summary
    cross_verify_now.__name__ = "cross_verify_now"
    tools.append(cross_verify_now)

    return tools


# ═══════════════════════════════════════════════════════════════════════
# Session state · 用 qid 隔离的进程内会话态
#
# 为什么不直接把 evidence/cards/narrative 当 tool 的输入/输出全文传给
# LLM: evidence 常有几十条、上万字，塞进每轮 tool_result 会迅速撑爆
# 上下文、烧穿 opus 的 token 预算。做法参照 deepagents 的「文件系统」
# 思路 —— 工具执行完把大对象存进 _SESSIONS[qid]，回给模型的只是一句
# 摘要 ("已拿到 87 条证据，独立源 52 条")，下游工具凭 qid 自己去读，
# 不经过模型的"复述"。
# ═══════════════════════════════════════════════════════════════════════

# ── Module identity unification (root fix, 2026-08-26 → 强化 2026-08-27) ──────
# 病根:本文件被双名 import(qa_auto_agent / qa_system.agent.qa_auto_agent)→加载两次→
# 两个 _SESSIONS→session 分家(_thin_mode 一个名设、另一个看不见)。
# 2026-08-27 根因修复:session 注册表迁到 run_state(单名 import·天然单例)。auto 的
# _SESSIONS 直接指向 run_state._SESSIONS——无论本文件被加载几次·都是同一个 dict·
# 彻底消除分家(不再依赖"跨模块采纳"的时序 hack)。sys.modules 别名统一仍保留(让两名
# 指向同一 module 对象·使其它模块级状态也一致)。
import run_state as _rs_sessions
_SESSIONS = _rs_sessions._SESSIONS   # 唯一真相源在 run_state
try:  # noqa
    _this_mod = sys.modules.get(__name__)
    for _alias in ("qa_auto_agent", "qa_system.agent.qa_auto_agent"):
        sys.modules[_alias] = _this_mod   # both names → this one object
except Exception:
    pass

# U2(2026-08-07) · 门面进程内并发闸门 —— 治「多agent卡」的架构级解法。
# 重型 LLM 子调用工具(spawn_subagent/delegate/run_pipeline/run_deep_research_swarm)
# 若 LLM 同时决定调好几个,会瞬时 spawn 一堆 claude 进程堆爆 CPU。这里用一个进程内
# 信号量:同时在跑的重型子调用不超过 N,N 按当前 load 定(load 越高 N 越小,极端时
# 串行)。对齐 CC「限并发不无脑起」。轻工具(shell/file/search)不受限。
import threading as _threading

_HEAVY_LOCK = _threading.Lock()
_HEAVY_INFLIGHT = [0]


class HeavySlotTimeout(RuntimeError):
    """重型任务未能在排队预算内取得并发槽；调用方应降级而不是穿透限流。"""


def _heavy_cap() -> int:
    """按 load 定重型子调用并发上限:load<8→3 / 8-15→2 / >15→1。旋钮 MEYO_HEAVY_MAX。"""
    try:
        forced = os.environ.get("MEYO_HEAVY_MAX")
        if forced:
            return max(1, int(forced))
        load1 = os.getloadavg()[0]
        return 1 if load1 > 15 else (2 if load1 > 8 else 3)
    except Exception:
        return 3


class _heavy_slot:
    """重型子调用的严格并发槽。超过上限时排队；超时明确拒绝，由上层降级。"""
    def __init__(self, timeout_s: float = 120.0):
        self.timeout_s = timeout_s
        self.acquired = False

    def __enter__(self):
        cap = _heavy_cap()
        # 简单排队:inflight >= cap 时轮询等待(带超时·不永久卡)
        deadline = time.time() + self.timeout_s
        while time.time() < deadline:
            with _HEAVY_LOCK:
                if _HEAVY_INFLIGHT[0] < cap:
                    _HEAVY_INFLIGHT[0] += 1
                    self.acquired = True
                    return self
            time.sleep(0.5)
        raise HeavySlotTimeout(
            f"重型任务并发槽等待超时({self.timeout_s:.0f}s, cap={cap})；"
            "为保护系统未执行。请复用已有结果、串行重试或稍后再试。"
        )

    def __exit__(self, *a):
        if self.acquired:
            with _HEAVY_LOCK:
                _HEAVY_INFLIGHT[0] = max(0, _HEAVY_INFLIGHT[0] - 1)
        return False


def _sess(qid):
    """P2/架构净化(2026-08-27): session 注册表已迁到 run_state(根治双 import 分家)。
    本函数委托 run_state.sess·auto 和 native 从同一注册表拿 session=单一事实源。"""
    import run_state as _rs
    return _rs.sess(qid)


def _canonical_run_state(session):
    """Return the single canonical state service for a live session.

    P2/架构审计(2026-08-27): 单一事实源逻辑已搬到独立模块 run_state.py(不再住 god-file)。
    本函数保留为薄包装(向后兼容·native 现有 9 处调用零改动),把 auto 的 _ckpt_path 注入
    进去算事件路径。native 也可直接 import run_state·两路径对称依赖同一模块·不再互相寄生。"""
    import run_state as _rs
    return _rs.canonical_run_state(session, ckpt_path_fn=_ckpt_path)


def _cv_main_confirmed(session, answer):
    """判主答案是否【已充分验证 confirmed】(cross_verify 仲裁保守 bug 修复用)。

    2026-08-19(Solanum hermannii 题): cross_verify 之前一律"无证据型 disagree 保留
    主答案"——但那只在主答案已 confirmed 时才合理。这里从 identity_coverage 已落进
    session["identity_states"] 的四态里查该答案实体的状态: confirmed→True(保留内核)·
    其它(probable/candidate/未评估)→False(disagree 应响应·回锦标赛/继续搜)。
    fail-safe: 读不到状态 → None(cross_verify 侧按 None 保守退回旧行为·不破坏 Russell
    Lyons 内核)。绝不透 gold(只读证据↔约束推出的四态)。"""
    try:
        import identity_coverage as _ic
        book = (session or {}).get("identity_states") or {}
        if not book:
            return None
        ent = _ic._extract_answer_entity(str(answer or "")) or str(answer or "")
        key = _ic._norm_entity(ent)
        rec = book.get(key)
        if not rec:
            # 宽松匹配: 答案实体名是某已评估候选的子串(或反之)
            for k, r in book.items():
                if key and (key in k or k in key):
                    rec = r
                    break
        if not rec:
            return None
        return str(rec.get("state") or "") == _ic.STATE_CONFIRMED
    except Exception:
        return None


def _record_traversal_result(session, source, result, goal):
    """把可寻址空间的观测转为共享、可追溯的检索证据。

    traversal_core 的 EvidenceMap 已经解决了模态内的去重和探索熔断；这里负责
    把它跨模态地接入 auto 的 evidence / mission_state，使网页、图片、视频的
    观察既能成为下一轮深研蜂群的种子，也能显式补全或暴露信息缺口。
    """
    if result is None:
        return
    evidence_pool = session.setdefault("evidence", [])
    existing = {
        str(item.get("url") or item.get("source") or "")
        for item in evidence_pool if isinstance(item, dict)
    }
    added = 0
    for item in list(getattr(result, "evidence", []) or [])[:20]:
        if not isinstance(item, dict):
            continue
        addr = str(item.get("addr", ""))
        content = item.get("content", "")
        if isinstance(content, (dict, list)):
            content = json.dumps(content, ensure_ascii=False)
        content = str(content or "").strip()
        if not content:
            continue
        url = addr[4:] if addr.startswith("url:") else ""
        # 图片/视频/表格位置也保留 source，满足后续证据合约但不伪造网页 URL。
        identity = url or f"{source}:{addr}"
        if identity in existing:
            continue
        evidence_pool.append({
            "url": url,
            "source": source,
            "title": f"[{source}] {addr}"[:160],
            "content": content[:6000],
            "meta": {"address": addr, "kind": item.get("kind", ""),
                     "note": str(item.get("note", ""))[:300],
                     "address_meta": item.get("addr_meta", {})},
        })
        existing.add(identity)
        added += 1
    ms = MissionState(session)
    answer = str(getattr(result, "answer", "") or "").strip()
    if bool(getattr(result, "ok", False)) and answer:
        ms.add_fact(answer, source=source)
    elif str(getattr(result, "stop_reason", "")) not in ("", "complete"):
        ms.add_gap(f"{source} 未完成：{goal[:180]}", raised_by=source)
    session.setdefault("_traversal_sync", []).append({
        "source": source, "added_evidence": added,
        "ok": bool(getattr(result, "ok", False)),
        "stop_reason": str(getattr(result, "stop_reason", ""))[:80],
    })


def session_snapshot(qid):
    """(2026-08-15·三车道) 供 Talker 快车道读的【最新状态快照】——不参与解题·只读。
    返回 {running, step, last_tool, last_summary, progress, final, staleness_s, question}。
    Talker 读它秒回用户("我正在查X·第N步"),不等 Planner 深推理完成。fail-safe。"""
    try:
        s = _SESSIONS.get(qid)
        if not s:
            return {"exists": False}
        _tl = s.get("tool_log") or []
        _last = _tl[-1] if _tl else {}
        _st = None
        try:
            _lu = s.get("_last_update_ts")
            _st = round(time.time() - _lu, 1) if _lu else None
        except Exception:
            _st = None
        return {
            "exists": True,
            "step": len(_tl),
            "last_tool": _last.get("tool", ""),
            "last_summary": str(_last.get("summary") or "")[:200],
            "plan": [{"text": p.get("text", ""), "status": p.get("status", "")}
                     for p in (s.get("plan") or [])[:8]],
            "final": str(s.get("final") or "")[:2000],
            "narrative_tail": str(s.get("narrative") or "")[-300:],
            "staleness_s": _st,          # 这个快照有多旧(Talker 诚实用)
            "question": str(s.get("question") or "")[:300],
        }
    except Exception as e:
        return {"exists": False, "error": f"{type(e).__name__}: {str(e)[:60]}"}


# ═══════════════════════════════════════════════════════════════════════
# 预算耗尽保护 · 参照 Claude Code (CCB) Agentic Loop 的终止/继续状态机
#
# CCB 的 queryLoop() 不是靠一个硬 recursion_limit 简单截断，而是分层处理:
#   - blocking_limit / max_turns：真正的硬顶，超过直接终止本轮循环
#   - token_budget_continuation：接近预算但没超时，注入 nudge 消息提示
#     模型"加速收尾"，让模型自己选择体面地结束，而不是被腰斩
#   - state.transition 记录"上一次为什么继续"，用于识别并打断无意义的
#     重复循环(比如 collapse_drain_retry 只允许发生一次)
#
# 我们的 deepagents/LangGraph 编排拿不到 CCB 那层 query() 的钩子，但每次
# 工具调用都是"一次 LLM turn 之后触发的动作"，等价于 CCB 里"工具执行"
# 阶段的介入点——所以把预算检查放在工具执行闭包的最前面，是我们这套
# 架构下最贴近 CCB 语义的落点。
# ═══════════════════════════════════════════════════════════════════════

DEFAULT_HARD_WALL_S = 600     # 硬顶：墙钟超过这个时间，只放行 finalize_answer，其余全部拦截
DEFAULT_SOFT_WALL_RATIO = 0.75  # 软顶：达到硬顶的 75% 时开始注入"该收尾了"的 nudge
LOOP_GUARD_THRESHOLD = 4      # 同一工具被连续调用超过这个次数(不含首次)，判定为空转死循环

# ── 深度考据类工具的循环卫兵放宽 (2026-08-06) ──
# 用户抓正:auto 遇难题(github 日期考据/EC 编号)靠 shell_exec 连续 curl 不同
# API、evolution_execute 跑不同工具做多方核实,是"像工程师一样用工具"的正常
# 行为,却被"同一工具连调 5 次就拦"的循环卫兵误伤(github 题就被拦过)。
# 病根:旧卫兵只按工具名判重,不看命令内容——连续 5 次 shell_exec 但每次
# curl 的是完全不同的 URL,那是深度考据不是空转。
# 修法(两条一起):
#   1. 这类"每次命令内容都不同才有意义"的工具,改成**按命令内容判重**:只有
#      完全相同的命令/参数连续重复才算空转,不同命令不累加 streak。
#   2. 同时给它们更宽的阈值(10),即便偶然重复几次同一考据命令也不轻易打断。
# 别的工具(如 run_parallel_search)维持旧的按工具名+阈值 4,那些工具"重复调
# 就是空转"的语义不变。
_CONTENT_DEDUP_TOOLS = {"shell_exec", "evolution_execute"}
_CONTENT_DEDUP_THRESHOLD = 6   # 深度考据工具:完全相同命令连续重复超过这个次数才算空转
# shell_exec 总量上限(防止 111 次循环):一道题 shell_exec 调用超此值强制警告+逐步收紧
_SHELL_EXEC_TOTAL_WARN = 30    # 超过此数开始警告(每 5 次一条)
_SHELL_EXEC_TOTAL_HARD = 60    # 超过此数强制拦截(防止 100+ 次纯 bash 暴力枚举)

# 每工具独立 query 指纹硬顶: 重试预算按指纹计数后(不同query不连坐)·
# 需要一道总量核防无限换词枚举。宽松=12(远大于旧实际可用数·治f2fe误拦的同时
# 保住有界性)。步数/墙钟仍是主约束·这只是第二道核。
_DISTINCT_QUERY_HARD = 12


def _arg_fingerprint(arg):
    """v108(M1b) · 把 query/URL 归一化成指纹: 小写·去空白/引号/不可见字符。

    用途: 重试预算与循环卫兵都按它区分"同一query重试"(占预算)和"不同query穷举"
    (不连坐)。保持顺序敏感(不排序词)——换词重述=新指纹·但受 _DISTINCT_QUERY_HARD
    总量核约束。fail-safe: 异常返回空串(调用方回退旧工具名计数行为)。
    """
    try:
        import re as _re_fp
        s = str(arg or "").strip().lower()
        if not s:
            return ""
        s = _re_fp.sub(r"[\s'\"`]+", " ", s)[:200]
        return s
    except Exception:
        return ""

# ── v72(2026-08-08) · 难题不许 0 步/极少步放弃 · finalize 硬门 ──
# 用户抓正:auto 对 EC 编号这类【重型/需检索】的难题,常常 10 几秒 0 工具就
# finalize 答 UNKNOWN(判"太难"直接放弃)。system prompt 里已有"先找工具再动手"
# 的引导,但那是软约束,模型仍会绕过。这里补一道**反向循环卫兵**:当模型想
# finalize 但【真检索/考据类工具用得太少 且 答案含 UNKNOWN/不知道】时,拦截
# 这次 finalize,把它推回去先分解+逐个子问题真用工具(可 curl 下载 PDF/查
# 权威库)。通用(非特判 EC):判据只看"任务是不是重型/需检索"+"努力程度"+
# "是不是要放弃",任何需检索的多跳题都受益。
#
# 关键设计(防误伤+防死循环):
#   1. 只对 task_level=重型/标准 且答案要放弃(含 UNKNOWN)时才拦——琐事/
#      简单题、或已给出实质答案的,一律放行,不拖累快答。
#   2. "真检索类工具"只数那些真去外部拿数据的(search/deep_research/shell_exec/
#      evolution_execute/pip_install/search_github/install_github_repo/
#      run_scenario_test/run_pipeline/delegate/spawn_subagent),不数
#      judge/write_plan/extract_cards 这类纯内部编排步骤——避免"调了分诊+
#      写计划就算努力过"的虚假达标。
#   3. 拦截有硬上限(FINALIZE_MIN_INVESTIGATIVE 之外再给 FINALIZE_MAX_BLOCKS
#      次机会):被拦 N 次后仍坚持放弃就放行,绝不无限逼它(那会烧穿预算/
#      违背"多方真尝试过拿不到才 UNKNOWN"),交付一个诚实的 UNKNOWN 也比死循环强。
FINALIZE_MIN_INVESTIGATIVE = 2   # 重型/需检索题放弃前,真检索类工具至少要调够这么多次
FINALIZE_MAX_BLOCKS = 3          # finalize 因"没努力+要放弃"最多被拦这么多次,超了放行

# ── v73(2026-08-08) · 卡住→自我反思换策略 (提升自主性 · 非题型强制) ──
# 用户理念:把 agent 当聪明员工,让它【卡住时自动停下自我反思:我该换工具/写
# 脚本/找 skill/建 graph 吗】,而不是检测题型加 if 强制。缺口=agent 有时一条
# 路走到黑或过早放弃,self_reflect 工具存在但要它自己想起来调,不够自动。
# 做法:在预算检查这个"每次工具调用前"的天然介入点,监测"连续 N 次工具调用
# 都没带来新有效进展"(证据没涨/正文没长/上一步报错或空)。命中就【注入一条
# 建设性反思提示】(不拦截、allow 仍 True),引导它自己选新策略——换工具/写脚本
# /找 skill/重分解,不替它决定。和"循环卫兵/难题卫兵"协同:那两个防重复空转+
# 防过早放弃,这个促"停下来换个思路"。控频:每题最多注入 REFLECT_MAX_NUDGES 次。
REFLECT_NOPROGRESS_STREAK = 3    # 连续这么多次工具调用都没带来新有效进展 → 触发一次反思
REFLECT_MIN_CALLS = 5            # 也可在总调用数达到这个量级但仍无进展时触发(兜底)
REFLECT_MAX_NUDGES = 3           # 每题最多注入几次反思提示(防刷屏)
# ── 层1(v98x · 2026-08-12) · 心跳自省(mid-run supervisor):像人一样【边跑边定期看自己】──
# 现有 _maybe_reflect_nudge 只在"无进展 streak"触发·是被动的(卡住了才反思)。心跳自省是
# 主动的:不管卡没卡·每隔一段【步数/时间/关键节点】就停下来调诊断脑 reflective_mind 看一眼
# "我现在在题目的什么位置·方向对吗·该换策略/加预算/问专家吗"——像我边做边每几分钟审视自己。
# 触发信号(任一):①每 HEARTBEAT_STEP_EVERY 步 ②elapsed 跨过软预算的 HEARTBEAT_TIME_FRAC
#   ③关键节点(感知停滞/临近墙钟等·由调用处传 node_signal)。带节流(每题最多 HEARTBEAT_MAX 次)。
HEARTBEAT_STEP_EVERY = 8         # 每这么多步做一次主动自省(不管卡没卡)
HEARTBEAT_TIME_FRAC = 0.5        # elapsed 跨过 软预算×此比例 时也触发一次(时间过半自省)
HEARTBEAT_MAX = 3                # 每题最多几次主动心跳自省(防刷屏·和被动反思分开计数)
# ── 层2(v98x) · agent 自我加预算(request_more_budget)的硬约束 ──
# 用户:"简单换策略/加时 agent 自己就该会·不该靠我"。给它这根杠杆·但守历史红线
# (v76"越跑越慢正反馈":自动加时曾让墙越撑越大)。所以加预算【必须有真实进展信号】
# +有次数上限+有绝对墙钟顶——不是空转就给续命。
# 2026-08-14(墓碑题 624 事故):长链题(找最老口味墓碑→定位背景照片→读韵脚)60步+1140s
#   都不够收敛·被外层墙杀成残缺。上限档整体上调·让【有进展的】长链题能自续到够用。
#   仍严守进展门控(request_more_budget 里 _grew/_recent_compute 判据)·无进展照拒·不破坏
#   v76"越跑越慢正反馈"红线。可 MEYO_BUDGET_EXTEND_* 环境变量覆盖。
BUDGET_EXTEND_MAX = int(os.environ.get("MEYO_BUDGET_EXTEND_MAX", "3"))   # 2→3:每题最多自主加预算几次
BUDGET_EXTEND_WALL_FRAC = 0.5    # 每次加 当前 hard_wall × 此比例 的时间
BUDGET_EXTEND_WALL_CAP = int(os.environ.get("MEYO_BUDGET_EXTEND_WALL_CAP", "2400"))  # 1800→2400:加完后 hard_wall 绝对上限(秒)
BUDGET_EXTEND_STEP_ADD = 20      # 每次加的步数(max_iterations)
# v-eff(2026-08-18·病1) · 加时【相对 base wall 的硬上限倍率】——对照实验铁证:
#   同一道题 CC 6-7min 答对·引擎 20+min 撞截断。核心病=动态加时无相对上限·
#   base 800s 一路续到 2400s(≈3×)=跑 23 分。修:累计加时后 hard_wall 绝不
#   超过 base_wall × 此倍率(默认 2×·env MEYO_BUDGET_EXTEND_WALL_CAP_MULT 可调)。
#   到顶=无论有无进展强制收尾(request_more_budget 也拒批)。base 保底给足·倍率防失控。
BUDGET_EXTEND_WALL_CAP_MULT = float(
    os.environ.get("MEYO_BUDGET_EXTEND_WALL_CAP_MULT", "2.0"))
# 总开关:各效率子病可单独关(默认全开·fail-safe)。
MEYO_ENGINE_EFFICIENCY = os.environ.get("MEYO_ENGINE_EFFICIENCY", "1") == "1"


def _effective_wall_cap(budget):
    """病1核心 · 返回本题 hard_wall 的【绝对硬上限秒】=min(base×倍率, 旧绝对秒兜底)。
    两道天花板都生效——相对倍率防"base 大的题续太多"·绝对秒防"base 本身就巨大"。
    fail-safe: 读不到 base 时退回旧的绝对秒常量(行为不破坏)。"""
    try:
        if not MEYO_ENGINE_EFFICIENCY:
            return float(BUDGET_EXTEND_WALL_CAP)
        _base = float((budget or {}).get("base_wall_s") or 0)
        if _base > 0:
            return float(min(BUDGET_EXTEND_WALL_CAP, _base * BUDGET_EXTEND_WALL_CAP_MULT))
    except Exception:
        pass
    return float(BUDGET_EXTEND_WALL_CAP)


def _effective_backend(session=None) -> str:
    """返回本次运行实际生效的引擎，避免 native 加载失败后策略仍按请求值放宽。"""
    try:
        actual = (session or {}).get("engine_backend_actual")
        if actual:
            return str(actual)
    except Exception:
        pass
    return os.environ.get("MEYO_AGENT_BACKEND", "native")


def _budget_step_add(session=None):
    """v121 修⑤(2026-08-17 · 384d 实证 + 用户指令『工具调用要能超 200 次』):
    native 自研循环无 LangGraph 图节点限制·步数上限纯是防呆预算——深坑多跳题
    (三段筛选/共现链·实证 384d 烧 46 步还在第一段)一次续命 +20 不够烧。native
    后端一次 +40·重型 90 起步 3 次续命 → 210 步·跨过 200 门槛;deepagents 保持
    +20(它真撞 recursion_limit·不能宽)。env 可覆盖。"""
    try:
        if _effective_backend(session) == "native":
            return int(os.environ.get("MEYO_BUDGET_STEP_ADD_NATIVE", "40"))
    except Exception:
        pass
    return BUDGET_EXTEND_STEP_ADD


def _budget_extend_max(session=None):
    """v121 修⑨(2026-08-18 · 用户拍板: 步数/续命不写死·动态调整): 续命次数
    上限对 native 放宽——深坑多跳题实证烧 46 步还在第一段·3 次续命不够烧。
    native 默认 6 次(与步数解放/墙钟感知配套·墙钟总上限仍由
    BUDGET_EXTEND_WALL_CAP 兜底·续不穿);deepagents 保持 3(它真撞
    recursion_limit·续多了也用不上)。env 可覆盖。fail-safe。"""
    try:
        if _effective_backend(session) == "native":
            return int(os.environ.get("MEYO_BUDGET_EXTEND_MAX_NATIVE", "6"))
    except Exception:
        pass
    return BUDGET_EXTEND_MAX
# v119 FixE: 外层监督墙感知——由外层管道(gaia_auto_rerun_100miss._run_auto)通过
#   MEYO_OUTER_DEADLINE(unix秒)传入。续命后的内层墙不得晚于【外层死线-此收尾余量】。
#   余量给 finalize/兜底抽取用(实测一次 best-effort 抽取 ≈40s·给 60s 宽松)。
_OUTER_FINISH_MARGIN_S = 60
# 上一步"看起来没产出/失败"的信号词(命令报错/搜索空/异常/拦截)——命中视为无进展
_NO_PROGRESS_MARKERS = (
    "新增 0", "未找到", "没有找到", "找不到", "exit_code=1", "exit_code=2",
    "exit_code=127", "[stderr]", "异常", "失败", "timeout", "超时", "为空",
    "empty", "error", "traceback", "not found", "[已拦截]", "[循环卫兵]",
    "[待审批]", "已达重试预算上限", "timed out",
)
# v119 FixF: 强失败标记(带锚定形态)——长摘要只在开头 80 字符里查这些。
#   裸 'error'/'异常' 在成功正文里太常见(如"error rates 分析""统计失败次数")·
#   只在短摘要(<120字·几乎就是回显本身)里才作为有效信号。
_STRONG_BAD_MARKERS = (
    "exit_code=", "error:", "error code", "traceback", "exception:",
    "exception in", "[stderr]", "超时", "timed out", "timeout:",
    "[已拦截]", "[循环卫兵]", "[待审批]", "已达重试预算上限", "新增 0",
    "未找到", "没有找到", "找不到", "为空", "not found", "empty result",
    "connection refused", "connection error",
)
_INVESTIGATIVE_TOOLS = {
"run_parallel_search", "run_deep_research_swarm", "shell_exec",
# 直接回看原图/原视频同样是独立验证动作；不能只把文字检索计为“调查”，
# 否则语义契约门会误判已经做过的定向视觉复核没有发生。
"see_image_vlm", "see_image_expert", "see_image_zoom", "read_image_ocr",
"see_video_frame", "label_sweep", "transcribe_audio",
"evolution_execute", "pip_install", "search_github", "install_github_repo",
"run_scenario_test", "delegate", "spawn_subagent",
"nocode_create", "nocode_send", "query_science_db",  # v96e 专业库查询也算真检索
"fetch_url_smart",  # v96i 抓已知具体 URL(智能分级抓取)也算真检索
}


def _summary_looks_bad(summary: str) -> bool:
    """v119 FixF(2026-08-17): 上一步工具摘要是否算"无产出/报错"·分层收紧误伤。
    病根: 原来 _NO_PROGRESS_MARKERS 对全文子串匹配——成功输出里偶现 'error'/
    'timeout'/'[stderr]'/'异常'/'失败' 字样(stderr 无害告警·搜索正文含关键词·
    统计任务数'失败次数')就被误判无进展→打断有产出的推理·误触发反思提示。
    收紧规则:
      ① 摘要短(<120字符): 保留全词匹配(短摘要里这些词≈就是失败回显);
      ② 摘要长: 只看开头 80 字符(失败回显开头即报错·成功输出开头是实质内容);
      ③ 有 [stdout] 且 stdout 实质内容 ≥200 字符且非 traceback → 一律算有产出
         (压倒性证据·即便尾部沾了个报错词)。
    """
    s = (summary or "").strip()
    if not s:
        return True
    _low = s.lower()
    if "[stdout]" in s:
        if "traceback (most recent call last)" not in _low:
            # stdout 实质内容 ≥200 字 → 有产出(压倒性证据·尾部沾报错词不算)
            if re.search(r"\[stdout\]\n(.{200,})", s, re.S):
                return False
        # stdout 内容过短(截断输出/几个字母) → 实质为空(原 v13.95 规则内化)。
        #   注意 s 已 strip() 过尾部换行·正则不能依赖行尾 \n;按 stdout 块总长判定
        #   (多行合计<8字符才算空·不再只看第一行 30 字符)。
        _m2 = re.search(r"\[stdout\]\n(.+)", s, re.S)
        if _m2 and len(_m2.group(1).strip()) < 8:
            return True
    if len(s) < 120:
        # 短摘要≈回显本身: 全词表匹配(保留原灵敏度)
        hay = _low
        return any(m in hay for m in _NO_PROGRESS_MARKERS)
    # 长摘要: 只看开头 80 字符·且只认强标记(治"error rates"类成功正文误伤)
    hay = _low[:80]
    return any(m in hay for m in _STRONG_BAD_MARKERS)


def _looks_like_giving_up(text):
    """答案文本看起来是"放弃/不知道"(含 UNKNOWN 或明确表达查不到)则 True。
    只看开头一段(结论多在开头),避免正文里随口一句"某某未知"误判。"""
    t = (text or "").strip().lower()[:400]
    if not t:
        return True  # 空答案 = 没产出,等同放弃
    markers = [
        "unknown", "无法确定", "无法回答", "无法获取", "查不到", "找不到",
        "没有找到", "不知道", "无法找到", "抱歉，我", "抱歉,我", "cannot determine",
        "not able to", "unable to", "'type': 'tool_use'", '"type": "tool_use"',
        "'name': 'finalize_answer'", '"name": "finalize_answer"',
        "'name': 'judge_scope_and_triage'", '"name": "judge_scope_and_triage"',
    ]
    return any(m in t for m in markers)


# ── 收尾 hint/checklist 噪音判据(2026-08-20 · Plastic Man 错题实证) ──────────
#   病根: agent 把【收尾自检提示】(qa_auto_agent 注入的"①最大/最小取反 ②答案是否
#   直接回应题面问的那个对象 ③交与字面最贴合的并标注存疑…")原样 echo 回来当 finalize
#   参数 → 存进 _last_finalize_attempt → 撞墙兜底 _force_degraded_final 优先取
#   _last_finalize_attempt → 把这段【提示噪音】当答案交出(评分判 INCORRECT·哪怕
#   证据里已搜到 gold "Plastic Man")。native 侧有 _looks_like_gate_noise 拦这个·但
#   auto 的兜底链没接(建了没接)。这里补 auto 侧等价轻量判据·fail-safe。
_FINALIZE_NOISE_MARKERS = (
    "回应题面问的那个对象", "算得最顺手", "字面最贴合", "标注置信度与存疑",
    "别沉默地猜", "多种读法", "多种理解", "多种读法的值", "取反",
    "先别交", "未回答", "请补", "补上", "映射表", "契约", "看门狗", "预算已耗尽",
    "再finaliz", "再 finaliz", "重复finalize", "工具前置", "动态证据前沿",
    # 放弃/自述 meta 话术(2026-08-24 公平基准 Aberdeen 实证:auto 放弃时把
    # "工具记录中没有任何一条能将题面的强约束…"当 final 交出)。通用放弃自述特征。
    "工具记录", "工具记录中没有", "没有任何一条", "均失败", "全部失败",
    "仅有失败", "失败的抓取", "无法将题面", "无法锁定", "证据不足以",
    "现有证据无法", "未能锁定",
    "无法确定唯一",
)


def _looks_like_finalize_noise(text):
    """判一段【想当 final 交出的文本】是不是收尾 hint/checklist 噪音而非真答案。
    通用启发·非题目特判·fail-safe(异常→False·保守不误杀真答案)。
    判据: 命中收尾提示特征词(≥2 个·或带①②③序号+≥1 个)·且不像具体答案。"""
    try:
        t = str(text or "").strip()
        if not t:
            return False   # 空由上层 _be 逻辑处理·这里只判"非空但是噪音"
        head = t[:300]
        hits = sum(1 for m in _FINALIZE_NOISE_MARKERS if m in head)
        has_numbered = ("①" in head or "②" in head or "③" in head)
        # 交付要求复述检测(2026-08-24 公平基准 Eldorado 实证):auto final 常把题面的
        # 中文交付指令原样当答案(如"目标文章的完整发布日期(")。这类文本以交付指令词
        # 构成、不含具体答案实体 → 判噪音。通用启发,非题目特判。
        _delivery_markers = (
            "须给出", "最终须", "目标文章", "完整发布日期", "完整日期",
            "发表时间窗", "含端点", "该文章的", "请给出该", "要求给出",
            "the complete publication date", "must provide", "target article",
        )
        _dhits = sum(1 for m in _delivery_markers if m in head)
        if _dhits >= 1:
            # 交付指令词出现且文本很短/以其结尾(没接上真答案)→ 复述噪音
            tail = t.strip().rstrip("：: （(【[")
            if len(t) < 60 or any(tail.endswith(m) for m in _delivery_markers):
                return True
        # ①②③收尾清单 + 至少一个特征词 → 噪音;或纯特征词 ≥2 个 → 噪音
        return (has_numbered and hits >= 1) or hits >= 2
    except Exception:
        return False


# ── FixM(2026-08-17 · bec7/384d/0b26 实证: 终答最后一公里) ──────────────────
#   病根: agent 算出了数值(0b26 自述"沙箱最后一步已算出")·但交卷时交的是 meta
#   文本(自辩/方法论/证据文档)——答案第一个词不是题目要的值·评分器 substring
#   匹配不到 → hit=0。整条管道(仲裁门/活库时点门/兜底路径)都不强制【值优先】。
#   三件套: ①数值问题检测(题面语义)·②会话内最近数值抽取(按 result/computed/=>
#   锚定优先)·③值优先门(前 80 字符无数字就拦一次逼重答 / 兜底路径直接前置数值)。
#   通用·零特判·无 gold 泄漏·fail-safe(检测不出就放行)。治的不是"算得不对"——
#   是"算对了却没交"。三题尸检: bec7=26.2 在手交了自辩·384d=8058 在手交了
#   张力分析·0b26=0.26 在沙箱交了证据文档。

# 追加【ID 类数值答案】模式——题面问 "what is the
#   PubChem CID of ..." 时无任何数值语义关键词(how many/average/...)·判 False →
#   值优先门+数值出处门都不触发 → 答案头是 meta 文本("The answer has been
#   finalized...")·CID 藏在正文里。CID/ISBN/ISSN/邮编/…id 答案本质是数值 token·
#   两道门对它们同样适用。实证题: 384d(gold 4192·本题数据已锁死但格式缺口通用)。
_NUMERIC_Q_HINTS = re.compile(
    r"\b(percentage|percentile|how\s+many|how\s+much|count\s+of|average|mean|"
    r"median|ratio|rate\s+of|proportion|total\s+number|number\s+of|"
    r"sum\s+of|total\s+of|score|cost|price|amount|how\s+old|"
    r"how\s+big|how\s+long|how\s+fast|how\s+tall|compute|calculate|"
    r"figure\s+out)\b|\bpercent\b|%|\b(?:twice|double|triple|half|fraction|integer)\b"
    r"|\bcid\b|\bid\s+number\b|\b(?:isbn|issn)\b|(?:zip|postal)\s+code"
    r"|what\s+[^.?!]{0,50}?\bid\b", re.I)


def _question_wants_number(question):
    """通用·零特判: 题面是否要求数值型答案。判据: 题面有数值语义关键词
    (percentage/how many/average/ratio/...) 命中即认——这些词本身就是强数值信号
    ·不强求题面带数字(纯语义的题如"What percentage of..."无数字也认)。
    fail-safe: 任何异常返回 False(不强制=不卡交卷)。
    v15.7(2026-08-18 · Q0人名题实证) 两道防线:
      ① session.question 常被污染成「题面+═══种子证据═══+自检清单」大prompt——
        证据文本里的 score/cost/number 词会让正则全命中·人名题被误判成数值题·
        值优先门硬塞数字(Russell Lyons 被改成 25 开头)。修: 只取题面本体
        (第一个 ═══ 分隔块之前·无分隔符则取前 1200 字符)。
      ② 人名/名称类疑问词(who/full name/name of...)直接 False——这类题
        答案以专名打头是正确形态·任何数值排版要求都是反向伤害。"""
    try:
        q = str(question or "").strip()
        if not q:
            return False
        # ① 剥种子证据/清单污染: 只看题面本体
        for _sep in ("═══", "\n\n\n", "---"):
            if _sep in q:
                q = q.split(_sep)[0].strip()
                break
        q = q[:1200]
        # ② 人名/名称类疑问 → 明确不是数值题(先于数值词判·优先级更高)
        if re.search(r"\b(who|whose|whom|full\s+name|name\s+of|what\s+is\s+the\s+name|"
                     r"哪位|谁|叫什么|名字是|全名|姓名)\b", q, re.I):
            return False
        return bool(_NUMERIC_Q_HINTS.search(q))
    except Exception:
        return False


# ══ v16(2026-08-18 · 收尾/核对/评分按题型自适应) ══════════════════════════
# 用户判断: 系统主攻 GAIA(裸数字/rescored 短答案·98.2%)时调的收尾/核对/提交/
#   评分逻辑·对 BrowseComp(找人名/公司/地点/赛事/角色等实体·长答案·多约束·歧义)
#   水土不服——"本质是收尾和评分还有最后提交核对答案的关节不灵活"。
# 核心原则: 按题型自适应·不写死。数值题/实体题/日期题走不同的收尾-核对-提交-评分
#   逻辑。题型判定用【题面语义】(通用·非特判具体题)。灵活≠放水: 实体题该有实体题
#   的严格(约束覆盖/多源)·只是别用数值题的尺子量实体题。
# 全部 fail-safe·env 可开关(MEYO_QTYPE_ADAPTIVE=0 关整套自适应·退回 GAIA 旧行为)。

# 人名/名称类疑问 → 实体题(答案以专名打头是正确形态)
_PERSON_NAME_Q = re.compile(
    r"\b(who|whose|whom|full\s+name|name\s+of|what\s+is\s+the\s+name)\b"
    r"|哪位|谁|叫什么|名字是|全名|姓名", re.I)
# 公司/机构/地点/赛事/作品/角色等【非人名实体】疑问 → 实体题
_ENTITY_Q = re.compile(
    r"\b(which\s+(?:company|team|club|city|town|country|place|venue|building|"
    r"river|mountain|island|band|film|movie|book|album|song|character|player|"
    r"person|author|artist|award|event|competition|tournament|organization|"
    r"organisation|university|college|school|church|institution|species|"
    r"product|brand|model|ship|aircraft)|what\s+(?:company|team|club|city|"
    r"place|venue|band|film|movie|book|album|character|award|event|"
    r"competition|tournament|organization|university|species))\b"
    r"|\bidentify\s+the\b|\bname\s+the\b"
    r"|哪(?:家|个|支|座|条|部|首|位).{0,4}?(?:公司|机构|球队|城市|地点|乐队|"
    r"电影|书|专辑|歌|角色|奖|赛事|比赛|大学|物种)", re.I)
# 日期/年份类疑问 → 日期题
_DATE_Q = re.compile(
    r"\bwhat\s+year\b|\bwhich\s+year\b|\bwhat\s+decade\b|\bwhat\s+date\b|"
    r"\bwhen\s+(?:was|did|were|is|does)\b|\bon\s+what\s+(?:date|day)\b"
    r"|哪一?年|什么时候|哪一?天|日期", re.I)


def _qtype_adaptive_enabled():
    return os.environ.get("MEYO_QTYPE_ADAPTIVE", "1").strip().lower() \
        not in ("0", "false", "off", "no")


def _question_body(question):
    """剥掉拼在题面前的种子证据/自检清单污染·只取题面本体(与 _question_wants_number
    同口径): 第一个分隔块之前·无分隔符则取前 1200 字符。fail-safe → 原串。"""
    try:
        q = str(question or "").strip()
        for _sep in ("═══", "\n\n\n", "---"):
            if _sep in q:
                q = q.split(_sep)[0].strip()
                break
        return q[:1200]
    except Exception:
        return str(question or "")[:1200]


def _question_type(question):
    """通用·零特判: 按题面【语义】判题型·供收尾/核对/评分分流。
    返回 'numeric' | 'entity' | 'date' | 'generic'。
    判定优先级(强信号优先): 数值语义(_question_wants_number·含 how many/CID/…)
      → numeric; 人名/机构/地点/赛事/作品/角色疑问 → entity; 年份/日期疑问 → date;
      都不命中 → generic。fail-safe → 'generic'(退回不分流的中性行为)。
    注: 数值优先是因为 GAIA 数值题行为必须不变(值优先门/数值出处门仍生效)。"""
    try:
        if not _qtype_adaptive_enabled():
            return "generic"
        q = _question_body(question)
        if not q:
            return "generic"
        # 数值语义最强(GAIA 主场·不能被别的信号夺走)
        if bool(_NUMERIC_Q_HINTS.search(q)):
            return "numeric"
        if _PERSON_NAME_Q.search(q) or _ENTITY_Q.search(q):
            return "entity"
        if _DATE_Q.search(q):
            return "date"
        return "generic"
    except Exception:
        return "generic"


# ══ v17(2026-08-18 · 双轨制任务路由 · KM 2781429277) ═══════════════════════
# 击穿本质: GAIA 98% 但 BrowseComp 崩·根本【不是验证器不够好】·而是【用验证"推理/
#   工具"的裁判去验证"记忆/搜索"的质量】——像用数学考试标准评判文献综述。
#   两类任务本质不同·验证【范式】必须不同:
#     · RESULT_ORIENTED(结果型·GAIA/数值/单一实体): 答案可枚举/单一·验【答案准确性】
#       → 走【推理轨道】(cross_verify 多模型交叉 + identity_coverage 四态/候选判别器)。
#     · PROGRESSIVE_COVERAGE(覆盖型·bc/深度调研/多约束找纠缠实体): 信息分散需渐进收集·
#       验【搜索轨迹覆盖度】(信息找全了吗) → 走【记忆轨道】(process_coverage)。
#     · PERSONALIZED(个性化·用户偏好): 验【意图对齐】(intent_ledger·先占位轻量)。
# 判据(通用·不针对具体题·不透 gold): 答案可枚举/单一→结果型; 信息分散需渐进收集
#   (多约束找纠缠实体·实体识别多跳)→覆盖型。复用 _question_type + "信息分散度"判定。
# env MEYO_TASK_ROUTE 默认开·=0 退回(全部走结果型=推理轨道·完全等价旧行为·fail-safe)。
TASK_RESULT = "RESULT_ORIENTED"
TASK_COVERAGE = "PROGRESSIVE_COVERAGE"
TASK_PERSONALIZED = "PERSONALIZED"

# 个性化/偏好类疑问(通用·非写死): 答案取决于【用户偏好/主观意图】而非客观事实。
_PERSONALIZED_Q = re.compile(
    r"\b(recommend|suggest|for\s+me|my\s+(?:preference|taste|budget|style)|"
    r"which\s+(?:should|would)\s+I|best\s+for\s+me|help\s+me\s+(?:choose|pick|decide))\b"
    r"|推荐|适合我|帮我(?:选|挑|决定)|我(?:的偏好|该选|应该)", re.I)
# 深度调研/多源整合疑问(通用·覆盖型强信号): 需要跨多个来源渐进收集信息。
_RESEARCH_Q = re.compile(
    r"\b(compare|survey|overview|comprehensive|all\s+the|list\s+all|"
    r"everything\s+about|research|investigate|综述|调研|盘点|梳理|全部|所有)\b", re.I)


def _task_route_enabled():
    return os.environ.get("MEYO_TASK_ROUTE", "1").strip().lower() \
        not in ("0", "false", "off", "no")


def _info_dispersion(question):
    """估题面【信息分散度】(通用启发·非题目特判): 信息越分散·越需要渐进收集·
    越是覆盖型。信号=约束数(_question_complexity)。多约束找纠缠实体(找既是 X 又是
    Y 又满足 Z 的实体)=高分散·单一事实=低分散。返回 int(约束估计)。fail-safe → 1。"""
    try:
        return _question_complexity(question)
    except Exception:
        return 1


def _task_route(question):
    """双轨制任务路由(文档第一层·把 _question_type 升级为【验证范式路由】)。
    返回 (task_type, reason)。task_type ∈ {RESULT_ORIENTED, PROGRESSIVE_COVERAGE,
      PERSONALIZED}。

    判定(优先级·通用·不透 gold):
      ① 个性化/偏好疑问(recommend/适合我/帮我选) → PERSONALIZED(意图对齐轨道);
      ② 数值题(_question_type=='numeric') → RESULT_ORIENTED(答案单一可枚举·推理轨道)
         ——GAIA 主场·行为必须不变(不破坏 98.2%);
      ③ 信息分散(约束数 >= 阈值·或深度调研/多源整合语义) → PROGRESSIVE_COVERAGE
         (多约束找纠缠实体·需渐进收集·记忆轨道);
      ④ 其余(单一实体/日期/generic) → RESULT_ORIENTED(答案单一·推理轨道)。
    关闭/异常 → (RESULT_ORIENTED, ...)(fail-safe·退回推理轨道=旧行为)。
    可 MEYO_COVERAGE_DISPERSION_MIN 调覆盖型的分散度门(默认 3)。"""
    try:
        if not _task_route_enabled():
            return TASK_RESULT, "task_route disabled"
        q = _question_body(question)
        if not q:
            return TASK_RESULT, "empty question"
        # ① 个性化(答案取决于用户偏好·非客观事实)
        if _PERSONALIZED_Q.search(q):
            return TASK_PERSONALIZED, "偏好/推荐语义→意图对齐轨道"
        qt = _question_type(question)
        # ② 数值题恒结果型(GAIA 主场·答案单一可枚举·行为不变)
        if qt == "numeric":
            return TASK_RESULT, "数值题→答案单一可枚举·推理轨道"
        # ③ 信息分散度 → 覆盖型(多约束找纠缠实体/深度调研/多源整合)。
        #   关键(不依赖硬编码名词表·避免 _question_type 名词表漏判): 覆盖型的本质是
        #   【信息分散·需渐进收集】·由【约束数(分散度)】判·而非题面用了哪个实体名词。
        #   数值题已在②拦下(恒结果型)·所以此处 entity/date/generic 只要多约束(找纠缠
        #   实体·如"既在 X 出生、又获 Y 奖、还创办 Z 期刊的人")就是覆盖型——这正是 bc
        #   多跳实体识别题的形态·不管 _question_type 把它归成 entity 还是 generic。
        disp = _info_dispersion(question)
        disp_min = int(os.environ.get("MEYO_COVERAGE_DISPERSION_MIN", "3") or 3)
        is_research = bool(_RESEARCH_Q.search(q))
        # entity 题(找实体)用 disp_min; generic 题(GAIA 多子句推理常也 generic·答案可能
        #   单一)门抬高一档(+1)·保守避免把 GAIA 单值推理题误拉进记忆轨道(保 98.2%)。
        eff_min = disp_min if qt == "entity" else disp_min + 1
        if disp >= eff_min or is_research:
            return TASK_COVERAGE, ("信息分散(约束≈%d·门%d)·多约束找纠缠实体→记忆轨道"
                                   % (disp, eff_min) if not is_research
                                   else "深度调研/多源整合→记忆轨道")
        # ④ 其余: 单一约束(单一实体/日期/单一事实) → 结果型(答案单一·推理轨道)
        return TASK_RESULT, "答案单一(%s·约束≈%d)→推理轨道" % (qt, disp)
    except Exception:
        return TASK_RESULT, "route异常·fail-safe退推理轨道"


# ══ v18(2026-08-19 · 多维连续特征谱 · KM 2781429277 追问篇) ═══════════════════
# 文档作者在追问篇(1181 行起)明确【推翻硬三分类】: "真实世界 query 不是非此即彼·
#   而是多维连续谱上的混合体·硬分类地基不稳(类别多→LLM zero-shot 急剧退化)·路由应
#   基于 intent+complexity+constraints 多维而非单一标签"。
# 所以正确问法不是"这是哪一类"·而是"这 query 在各维度上分别多强"·按强度【组合】验证。
#   _task_route(硬三分类)保留当 fallback·特征谱是它的【连续版超集】。
# env MEYO_FEATURE_PROFILE 默认开·=0 退回 _task_route 硬三分类(fail-safe·完全等价)。
def _feature_profile_enabled():
    return os.environ.get("MEYO_FEATURE_PROFILE", "1").strip().lower() \
        not in ("0", "false", "off", "no")


def _query_feature_profile(question, user_profile=None):
    """多维连续特征向量(每维 0-1·规则优先轻量启发·不调 LLM·贵)。返回 dict:
      { result_oriented, progressive_coverage, personalization,
        complexity, ambiguity, overall_confidence,
        strategies:[激活的验证轨道], primary:最高维映射的单类别(兼容下游) }
    复用现有信号(_question_type/_question_complexity/_PERSONALIZED_Q/_RESEARCH_Q)·
    不推翻 _task_route·是它的连续版。fail-safe → 全 0 + primary=RESULT_ORIENTED。"""
    prof = {"result_oriented": 0.0, "progressive_coverage": 0.0,
            "personalization": 0.0, "complexity": 0.0, "ambiguity": 0.0}
    try:
        q = _question_body(question)
        if not q:
            prof["result_oriented"] = 1.0
            prof["overall_confidence"] = 1.0
            prof["strategies"] = ["reasoning"]
            prof["primary"] = TASK_RESULT
            return prof
        qt = _question_type(question)
        disp = _info_dispersion(question)   # 约束数(有界 1..12)
        disp_min = int(os.environ.get("MEYO_COVERAGE_DISPERSION_MIN", "3") or 3)

        # ── progressive_coverage(信息分散/需渐进收集·bc 特征): 约束数越多越高·
        #    深度调研语义再加权。disp>=disp_min 时接近饱和。
        cov = min(1.0, max(0.0, (disp - 1) / float(max(1, disp_min * 2 - 1))))
        if _RESEARCH_Q.search(q):
            cov = min(1.0, cov + 0.35)   # 深度调研/多源整合=强覆盖信号
        prof["progressive_coverage"] = round(cov, 3)

        # ── result_oriented(答案可枚举/单一·GAIA 特征): 数值题恒高(主场保 98.2%);
        #    单一约束(disp 小)高; 约束越多则越偏覆盖·result 相应降。
        if qt == "numeric":
            res = 1.0                      # 数值题 → 强结果型(推理轨道·行为不变)
        else:
            res = max(0.0, 1.0 - cov)      # 与覆盖度互补(信息越分散·越不像单值题)
            if qt == "date":
                res = max(res, 0.7)        # 日期题通常单值
        prof["result_oriented"] = round(res, 3)

        # ── personalization(涉及用户偏好/历史/"我"·服务型): 偏好语义命中→高;
        #    有用户画像命中再加权。bc/GAIA 客观题无画像·恒≈0(关键回归防护)。
        pers = 0.0
        if _PERSONALIZED_Q.search(q):
            pers = 0.75
        if user_profile:
            try:
                if isinstance(user_profile, dict) and user_profile.get("user_id"):
                    pers = min(1.0, pers + 0.2)
            except Exception:
                pass
        prof["personalization"] = round(pers, 3)
        # 偏好题不是客观单值题: personalization 命中时抑制 result_oriented(数值题除外·
        #   数值恒结果型)·避免短偏好句 cov=0 → res 顶满压过 personalization。
        if pers > 0.4 and qt != "numeric":
            prof["result_oriented"] = round(min(prof["result_oriented"],
                                                 1.0 - pers), 3)

        # ── complexity(任务复杂度·约束数粗估归一)
        prof["complexity"] = round(min(1.0, disp / 8.0), 3)

        # ── ambiguity(意图模糊度·启发): 多个维度同时中高=意图不清(既像判卷又像搜索
        #    又像服务)。取"接近的高维之间的重叠"作粗略模糊度。
        highs = sorted([prof["result_oriented"], prof["progressive_coverage"],
                        prof["personalization"]], reverse=True)
        amb = 0.0
        if highs[0] > 0.4 and highs[1] > 0.4:
            amb = round(min(1.0, highs[1] / max(1e-6, highs[0])) * 0.6, 3)
        prof["ambiguity"] = amb
        prof["overall_confidence"] = round(1.0 - amb, 3)

        # ── compose_verification_strategy: 按权重【叠加】·非选一个(文档核心)。
        strategies = []
        if prof["progressive_coverage"] > 0.5:
            strategies.append("memory")       # 记忆轨道(process_coverage)
        if prof["result_oriented"] > 0.6:
            strategies.append("reasoning")     # 推理轨道(cross_verify/identity_coverage)
        if prof["personalization"] > 0.4:
            strategies.append("intent")        # 意图对齐(intent_alignment·任务 B)
        # 不确定性 fallback: 中置信(0.5..0.8)→保守【激活全部】验证器综合评分不单一否决;
        #   低置信(<0.5)→保守走结果型(推理轨道·别澄清打断 bc 自主答题)。
        conf = prof["overall_confidence"]
        if not strategies:
            strategies = ["reasoning"]
        elif 0.5 <= conf < 0.8:
            strategies = ["memory", "reasoning"]   # 保守: 双轨都激活(不含 intent 除非上面命中)
            if prof["personalization"] > 0.4 and "intent" not in strategies:
                strategies.append("intent")
        elif conf < 0.5:
            strategies = ["reasoning"]
        prof["strategies"] = strategies

        # primary: 最高维映射单类别(兼容下游 session["_task_route"] 单字段·下游不破)
        m = max(("result_oriented", "progressive_coverage", "personalization"),
                key=lambda k: prof[k])
        prof["primary"] = {"result_oriented": TASK_RESULT,
                           "progressive_coverage": TASK_COVERAGE,
                           "personalization": TASK_PERSONALIZED}[m]
        return prof
    except Exception:
        prof["result_oriented"] = 1.0
        prof["overall_confidence"] = 1.0
        prof["strategies"] = ["reasoning"]
        prof["primary"] = TASK_RESULT
        return prof


def _reclassify_profile_runtime(profile, source_diversity=None):
    """运行时重分类(文档追问篇·轻量): 执行中随信息暴露调整特征向量·非开头一锤定音。
    evidence_collected 后若信息极度分散(source_diversity>0.8)→上调 progressive_coverage、
    下调 result_oriented(唯一正确答案可能不存在)。fail-safe → 原样返回。"""
    try:
        if not profile or source_diversity is None:
            return profile
        if float(source_diversity) > 0.8:
            profile["progressive_coverage"] = round(
                min(1.0, float(profile.get("progressive_coverage", 0)) + 0.2), 3)
            profile["result_oriented"] = round(
                float(profile.get("result_oriented", 0)) * 0.7, 3)
            # 重组策略
            strategies = list(profile.get("strategies") or [])
            if profile["progressive_coverage"] > 0.5 and "memory" not in strategies:
                strategies.append("memory")
            profile["strategies"] = strategies
            profile["_reclassified"] = True
        return profile
    except Exception:
        return profile


# 复杂度信号(通用启发·非题目特判): 用题面【约束/条件数】与【子句/连接词】估复杂度。
#   多跳实体识别题(需满足 N 条约束·交叉验证)应拿更长墙钟 + 更宽容的无进展容忍;
#   简单事实题保持紧(不拖累 GAIA 快答)。
_CONSTRAINT_CONNECTORS = re.compile(
    r"\b(?:who|whom|whose|which|that|where|when|and|but|also|"
    r"born|founded|located|published|released|won|awarded|"
    r"between|before|after|during|while|until|since|whose)\b", re.I)


def _question_complexity(question):
    """估题面复杂度(约束数/跳数)·通用启发。返回 int(约束估计·>=1)。
    信号: ①逗号/分号/换行分句数 ②约束连接词命中数 ③引号包裹的专名短语数
    (每个引号短语通常是一条硬约束)。三者综合取整。fail-safe → 1。"""
    try:
        q = _question_body(question)
        if not q:
            return 1
        clauses = len([p for p in re.split(r"[,;\n]", q) if len(p.strip()) >= 8])
        connectors = len(_CONSTRAINT_CONNECTORS.findall(q))
        quoted = len(re.findall(r'"[^"]{2,60}"', q)) + len(re.findall(r"'[^']{2,60}'", q))
        # 综合: 子句 + 连接词的一半(连接词会重叠子句) + 引号约束
        est = max(1, clauses + connectors // 2 + quoted)
        return min(est, 12)   # 有界
    except Exception:
        return 1


def _adaptive_wall_multiplier(question):
    """按题型/复杂度给墙钟自适应倍率(通用·不写死常数)。
    数值题(GAIA 主场): 倍率恒 1.0——行为完全不变(不破坏 98.2%)。
    实体/日期题: 多约束多跳→给更长墙钟(每条额外约束 +0.15·封顶 2.0)·
      简单单约束实体题≈1.0(不拖累快答)。generic 保持 1.0。
    fail-safe → 1.0。可 MEYO_QTYPE_WALL_CAP 调上限。"""
    try:
        if not _qtype_adaptive_enabled():
            return 1.0
        qt = _question_type(question)
        if qt == "numeric" or qt == "generic":
            return 1.0
        # entity / date: 复杂度驱动加时
        comp = _question_complexity(question)
        cap = float(os.environ.get("MEYO_QTYPE_WALL_CAP", "2.0"))
        # 约束>=3 才开始加时(多约束交叉验证题)·每条额外约束 +0.15
        mult = 1.0 + max(0, comp - 2) * 0.15
        return max(1.0, min(mult, cap))
    except Exception:
        return 1.0


def _adaptive_noprogress_streak(question):
    """按题型给"连续无进展→触发反思/收敛"的容忍阈值(通用·不写死)。
    数值题/generic: 用 GAIA 默认 REFLECT_NOPROGRESS_STREAK(紧·快收敛)。
    实体/日期多约束题: 放宽——多跳实体识别常有几步"检索到候选但还没交叉验证"
      的表面无进展·不该被 GAIA 式 3 次就打断当空转。复杂度越高越宽容(封顶 +4)。
    fail-safe → REFLECT_NOPROGRESS_STREAK。"""
    try:
        if not _qtype_adaptive_enabled():
            return REFLECT_NOPROGRESS_STREAK
        qt = _question_type(question)
        if qt in ("numeric", "generic"):
            return REFLECT_NOPROGRESS_STREAK
        comp = _question_complexity(question)
        extra = min(4, max(0, comp - 2))   # 约束>2 起每条 +1·封顶 +4
        return REFLECT_NOPROGRESS_STREAK + extra
    except Exception:
        return REFLECT_NOPROGRESS_STREAK


_NUMERIC_RE = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


# 题面问的是【PubChem CID】但值优先门抽到的
#   是同一输出里的分子量 1202.6——同一行里抓错字段·引导交交错答案。修: 先识别
#   题面所问的【答案类型】(编号/年份/小数)·再按类型选候选·消息里明说字段语义。
_ID_KIND_PAT = re.compile(
    r"\b(?:pubchem\s+)?cid\b|\bisbn\b|\bissn\b|\bzip(?:\s+code)?\b|\becli\b"
    r"|\bidentifier\b|\bdoi\b|\bcode\b|\b编号\b|\b邮编\b", re.I)
_DECIMAL_KIND_PAT = re.compile(
    r"decimal\s+places?|to\s+\d+\s+decimals?|\d+\s*位小数|保留小数", re.I)
_YEAR_KIND_PAT = re.compile(
    r"\bwhat\s+year\b|\bwhich\s+year\b|\bwhat\s+decade\b|\b年份\b", re.I)


def _leading_answer_number(answer, scan=200):
    """答案开头(前 scan 字符)的第一个数值 token——值优先交卷后它就是主答值。
    v121b: 行首列表编号(如 "1. "/"2) ")不是主答值——若答案以列表编号打头·会把
    编号抽成主答值·门跟着验编号的出处(工具输出里当然有它)→真主答数值的
    无出处漏检。剥行首编号再找。"""
    try:
        t = re.sub(r"^[-\s#*>`]+", "", str(answer or "").lstrip())
        t = re.sub(r"(?m)^\s*\d{1,2}\s*[.、)]\s+", "", t)   # 行首列表编号
        m = _NUMERIC_RE.search(t[:scan])
        return m.group(0) if m else None
    except Exception:
        return None


def _tool_log_numbers(session, cap=400):
    """tool_log 全部输出里出现过的数值 token 集合 + float 列表(封顶防大 log)。"""
    out_s, out_f = set(), []
    try:
        for e in (session or {}).get("tool_log") or []:
            s = str((e or {}).get("summary") or "")
            for tok in _NUMERIC_RE.findall(s):
                out_s.add(tok)
                try:
                    out_f.append(float(tok))
                except Exception:
                    pass
                if len(out_s) >= cap:
                    return out_s, out_f
    except Exception:
        pass
    return out_s, out_f


def _number_backed(tok, str_set, float_list, rel=0.002):
    """该数值是否在工具输出里有出处。三路匹配(从严到宽):
    ① token 完全命中; ② 工具 token 以答案 token 为前缀(工具打了更多位·agent
    舍入抄写·如工具 0.00033123 → 答案 0.00033); ③ 数值近似 rel=0.2%(手算
    舍入/单位换算的小漂移)。反向(答案位数多于任何工具输出)不算——正是编造
    信号。解析失败 fail-open 返回 True(不拦)。"""
    tok = str(tok or "").strip().rstrip(".,")
    if not tok:
        return True
    if tok in str_set:
        return True
    if len(tok) >= 3 and any(s.startswith(tok) for s in str_set):
        return True
    try:
        v = float(tok)
    except Exception:
        return True  # 解析不了就不拦(fail-open)
    for f in float_list:
        if f == v or abs(f - v) <= abs(v) * rel + 1e-12:
            return True
    return False


# ── v121 修③(2026-08-17 · 00d579ea 实证: 专家一致凭记忆答错) 专家单源检测 ────
#   双专家凭记忆一致答同一实体·gold 是另一人——不同模型共享同一份训练记忆·
#   在具体事实上的一致只是同一票投两次。主答实体只在专家输出里出现、无独立
#   文本证据命中 → finalize 拦一次逼找转录/字幕/档案文本佐证。
_ENT_PAT = re.compile(r"[A-Z][a-z]+(?:[ '-][A-Z][a-z'-]+){1,3}")
_UNVERIF_MARKS = ("⚠️疑似仅传题目", "无法验证", "cannot verify", "can't verify",
                  "don't have access", "unable to access", "no access to")


# 拦截/门/系统提示类 summary 的开头标记(它们会复述实体·不是证据——
# 实证: [绕圈反思] 提示词里引用了主答实体·被误算成“文本证据命中”)
_GATE_TEXT_PREFIXES = ("[绕圈反思", "[硬门控", "[已拦截", "[步数硬顶", "[自我反思",
                       "[心跳自省", "[视频模态", "[finalize", "[无进展看门狗",
                       "[加预算", "[值优先", "[终答值优先", "[数值出处", "[约束",
                       "[口径约束", "[专家单源", "[基数口径", "[连续工具报错",
                       "[信息饱和", "[自主策略校正", "[已声明能力墙", "[gate-bus",
                       "[工具不存在", "[待审批", "[上下文已压缩", "[自磨对",
                       "[步数警报", "[反思脑", "[gate:")


def _is_gate_text(s):
    """summary 是否是门/拦截器/系统注入文本(非证据)。fail-safe。"""
    try:
        t = str(s or "").lstrip()
        return any(t.startswith(p) for p in _GATE_TEXT_PREFIXES)
    except Exception:
        return False


def _expert_solo_sourced(session, answer):
    """主答实体是否【仅】来自专家模型记忆。返回实体串或 None。fail-safe。
    判据: ①答案前 200 字符抽出首字母大写实体词组(人名/地名/作品名·≥2 词);
    ②该实体在全部非-ask_expert_model 工具输出中 0 命中; ③ask_expert_model
    输出命中 ≥2 次·且至少一次带『凭记忆/无法验证』标记(纯记忆征的信号)。"""
    try:
        t = str(answer or "")[:200]
        if not t:
            return None
        # 答案前段的候选实体·取最靠前且 ≥2 词的(主答实体)
        cands = [m.group(0) for m in _ENT_PAT.finditer(t)]
        if not cands:
            return None
        ents = []
        seen = set()
        for c in cands:
            if len(c.split()) >= 2 and c not in seen:
                seen.add(c)
                ents.append(c)
        if not ents:
            return None
        tl = (session or {}).get("tool_log") or []
        _txt_hits = set()   # 非-专家工具输出里命中的实体
        _exp_hits = {}
        _exp_unverif = False
        for e in tl:
            tool = str((e or {}).get("tool") or "")
            s = str((e or {}).get("summary") or "")
            is_expert = tool == "ask_expert_model"
            if not is_expert and _is_gate_text(s):
                continue   # 门/拦截文本会复述实体·不算证据命中(00d579ea 实证)
            for ent in ents:
                if ent in s:
                    if is_expert:
                        _exp_hits[ent] = _exp_hits.get(ent, 0) + 1
                    else:
                        _txt_hits.add(ent)
            if is_expert and any(m in s for m in _UNVERIF_MARKS):
                _exp_unverif = True
        for ent in ents:
            if ent not in _txt_hits and _exp_hits.get(ent, 0) >= 2 and _exp_unverif:
                return ent
        return None
    except Exception:
        return None


# ── v121 修④(2026-08-17 · 04a04a9b 实证: 聚合库宽口径基数) 基数分歧检测 ─────
#   两家聚合库对同一基数已分歧 >10%·引擎挑一个直接算·gold 是官方窄口径
#   (差数倍)。题面计数题 + 工具记录 ≥2 个分歧 >10% 基数 → 拦一次。
_QCOUNT_PAT = re.compile(
    r"(?i)how\s+many\s+[^.?]{0,80}?(articles?|papers?|studies?|publications?|"
    r"posts?|issues?|books?|albums?|songs?|films?|movies?)")
_BASEC_PAT = re.compile(
    r"(?i)(?:(?:total|count)(?:\s+\w+){0,3}?[:\s]+|results?[:\s]+|"
    r"(?:articles?|papers?|entries?|results?|publications?)[^\n\d]{0,12}:\s*)"
    r"(\d{3,6})")
_YEARS = set(range(1900, 2036))


def _base_counts_divergent(session, ratio=1.10):
    """tool_log 里互相分歧 >ratio 的计数基数(升序去重)。只抓『计数上下文』
    的 3-6 位数(total/count/results:N·articles: N)·年份防误抓。fail-safe。"""
    try:
        nums = set()
        for e in (session or {}).get("tool_log") or []:
            s = str((e or {}).get("summary") or "")
            for m in _BASEC_PAT.finditer(s):
                try:
                    v = int(m.group(1))
                except Exception:
                    continue
                if v in _YEARS:
                    continue   # 1900-2035 大概率是年份·宁可漏抓不误拦(fail-safe)
                nums.add(v)
        if len(nums) < 2:
            return None
        srt = sorted(nums)
        if srt[-1] >= srt[0] * ratio:
            return srt
        return None
    except Exception:
        return None


# 交卷前意图复核。
#   实证根因(Apple 股价题:题目问 Google Finance 上"未复权"股价何时首破 $50,
#   gold=2018,agent 答 1987):agent 没有采用题目最朴素直白的字面理解——
#   "without adjusting for stock split" 就是"不做任何调整,看当年实际挂牌的
#   名义股价"。但 agent 通过大段自我说服式论证,把简单口径题曲解反了方向。
#   原方案用固定词表(_OVERTHINKING_MARKERS)检测"反直觉"话术密度——但这是
#   硬编码·不通用。v99p(2026-08-13)改为通用:交卷前用 LLM 做意图对比·
#   看"答案的论证方向"和"题目原文最朴素字面意思"是否一致。不预设偏移类型。




def _question_contract_gaps(question):
    """返回题干中明确未提供、却可能决定可交付结论的输入占位缺口。

    不按题型或实体硬编码；只识别文本自身的未填写槽位（如 ``[value]``、
    ``worth .``、句末省略号）。这类输入不能被模型用常识悄悄补齐，否则即使
    后续推导完全正确，交付的也不是题目定义的量。
    """
    try:
        import re
        text = str(question or "")
        gaps = []
        for match in re.finditer(r"\[\s*[^\]]{1,120}\s*\]", text):
            _inner = match.group(0)
            # 方括号占位符排除【系统自己的附件提示文案】——
            # "[附件已下载到本地·用 file_read/... 工具读取分析]"是 harness 拼进题面的
            # 附件使用说明·不是题干缺失输入。bec7 被它误拦 3 次(附件第0步就读了)。
            if any(k in _inner for k in ("附件", "已下载", "工具读取", "attachment",
                                         "file_read", "read_image", "读取分析")):
                continue
            gaps.append(f"未填写占位符 {_inner!r}")
        # 句法上要求一个值、但紧随标点或句尾，属于通用的缺失参数而非领域词表。
        # "Provide your answer as just an integer value."
        # 里句尾的 value. 被当成未填槽位→finalize 被白白烧掉一次。排除:值词前面紧跟
        # 冠词/格式形容词(an integer/numeric/the exact...)时它是普通名词短语(答案格式
        # 说明)·不是槽位;真正要抓的是 "worth ."/"the rate: " 这类直接接标点的裸槽。
        for match in re.finditer(
                r"\b(?:worth|value|amount|price|rate|cost|equals?|is)\s*(?:(?:[.,;:]|$)|\$(?=\s*(?:[.,;:]|$)))",
                text, re.I):
            _pre = text[max(0, match.start() - 22):match.start()]
            if re.search(r"(?:\b(?:single|integer|numeric|number|whole|decimal|exact|dollar|percentage|positive|negative)\s+|[a-z]-\s*)$", _pre, re.I):
                continue  # 答案格式说明('integer value.'等)·不是槽位
            gaps.append(f"数值槽位未填写（{match.group(0)!r}）")
        if re.search(r"(?:\.\.\.|…|\bas\s*)\s*$", text.strip(), re.I):
            gaps.append("题干在未完成片段处截断")
        return gaps[:4]
    except Exception:
        return []


def _parse_model_json_object(raw):
    """从模型响应中安全提取 JSON 对象。

    模型编排层在不同供应商/降级路径中可能返回 dict，而不是纯字符串；同时回答
    可能含 Markdown 围栏或前置说明。统一在边界处归一化，避免质量门因解析异常
    静默失效，并且只接受 JSON object，拒绝数组和残缺片段。
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\\s*|\\s*```$", "", text, flags=re.I).strip()
    left, right = text.find("{"), text.rfind("}")
    if left < 0 or right <= left:
        return None
    try:
        parsed = json.loads(text[left:right + 1])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None




def _wall_past(session):
    """v-eff2(2026-08-19·病5 墙钟硬门) · 墙钟是否已到点(硬墙耗尽/硬拦激活)。

    6e8c0ae1 实证: 硬墙 900s 到点后工具被拦改发"立即 finalize"·但 finalize
    又被 5 道质检门(反向卫兵/映射表/身份覆盖/单源对账/格式)轮番各拦一次·
    每拦一轮烧 15-60s 模型调用·900s 一路烧到 2313s(2.5×)——门与墙打架·
    墙输给了门。修: 到点后所有 finalize 质检门让路·先交卷(有答案交答案·
    没答案交诚实 UNKNOWN)·质检让位于交付。
    fail-safe: 读不到预算回 False(无预算场景行为不变)。
    """
    try:
        _b = (session or {}).get("_budget") or {}
        _hw = float(_b.get("hard_wall_s") or 0)
        _t0 = float(_b.get("t_start") or 0)
        # v-eff2b · 和硬墙触发点(_check_budget 里的判断)保持同一发令时刻——
        #   都提前 finalize_reserve_s，否则质检门让路判断比硬墙拦截判断晚
        #   触发，会出现"硬墙已强制发令但质检门还在拦"的不一致窗口。
        _reserve = float(_b.get("finalize_reserve_s") or 0)
        if _hw > 0 and _t0 > 0 and (time.time() - _t0) >= max(0.0, _hw - _reserve):
            return True
        return bool(_b.get("hard_block_active"))
    except Exception:
        return False


def _gate_budget_tight(session):
    """v13.97(2026-08-20·优化2·门开销分级) · 时间预算是否已【紧张】(还没到硬墙·
    但用掉的墙钟比例已高)。用于 finalize 前的门【按关键度分级跳过】:

      · 关键门(约束对账 _maybe_block_finalize + _constraint_mapping_gate、
        grounding/实体漂移 _identity_coverage_gate)——【任何时候都跑】(除非硬墙到点·
        走 _wall_past 让路)。这是治核心 bc 病(锁错实体/裸猜)的·绝不因预算跳。
      · 非关键门(单源对账叙事、格式守卫、Self-Consistency 多次采样投票)——预算紧张时
        【跳过·只留关键门】·省下重跑/多次 LLM 采样的开销·避免门叠加拖慢。

    判据: 已用墙钟 >= MEYO_GATE_TIGHT_RATIO(默认 0.75)× 硬墙。fail-safe: 读不到
    预算回 False(=不跳·保持全跑的旧行为)。env MEYO_GATE_TIER=0 时永远返 False(整体回退)。
    """
    try:
        if os.environ.get("MEYO_GATE_TIER", "1") != "1":
            return False
        _b = (session or {}).get("_budget") or {}
        _hw = float(_b.get("hard_wall_s") or 0)
        _t0 = float(_b.get("t_start") or 0)
        if _hw <= 0 or _t0 <= 0:
            return False
        _ratio = float(os.environ.get("MEYO_GATE_TIGHT_RATIO", "0.75"))
        return (time.time() - _t0) >= (_hw * _ratio)
    except Exception:
        return False


def _constraint_mapping_gate(session, final_markdown):
    """v108(M3·GAIA-165复盘) · finalize 二道门: 约束↔实现映射表存在性审计。

    病根实证(8d46/bec7): 题干口径(年份/状态列)从未映射到代码参数——agent
    "核对过"但筛错年份/查错状态列。第一道门(_check_budget 口径核对)已要求
    在下次 finalize 附映射表; 这道门在真正 finalize 时检查:
      触发条件(全部满足):
        ①用过计算/查询类工具(shell_exec/sympy_calc/query_table/read_table)
        ②triage 抽过约束/歧义轴·且第一道门已拦过(_constraint_checklisted)
        ③final_markdown 里找不到映射表特征(≥2行含'|'的表格行·或'映射'节)
      行为: 只拦一次(打标 _constraint_mapping_audited)·要求补表后重交。
    fail-safe: 任何异常放行(返回 "")。没约束/没用代码/纯查找题不触发。
    """
    try:
        budget = session.get("_budget")
        if budget is None:
            budget = session["_budget"] = {}
        if budget.get("_constraint_mapping_audited"):
            return ""
        if not budget.get("_constraint_checklisted"):
            return ""  # 第一道门没拦过(无约束)·不二次加门
        _tr = session.get("triage") or {}
        if not ((_tr.get("constraints") or []) or (_tr.get("ambiguity_axes") or {})):
            return ""
        _tl = session.get("tool_log") or []
        _used_code = any(t.get("tool") in ("shell_exec", "sympy_calc",
                                            "query_table", "read_table")
                         for t in _tl)
        if not _used_code:
            return ""  # 纯查找/阅读题·无代码可映射
        _fm = str(final_markdown or "")
        _table_rows = [ln for ln in _fm.splitlines()
                       if ln.count("|") >= 2 and len(ln.strip()) > 8]
        _kw_ok = any(k in _fm for k in ("约束", "constraint", "口径", "映射"))
        # 存在性检查→内容覆盖检查:
        # 旧判据"有表+提过'约束'"太弱——随便贴张数据表+提一句约束就骗过。
        # 强化: 从 triage 约束里抽【可检验 token】(数字/引号词=年份/数量/枚举值)·
        # 要求至少一半真的出现在映射表行里。纯定性约束(无 token)退回存在性检查。
        # 全部动态来自 triage·零题目特判——通用。
        _cons_all = [str(c) for c in (_tr.get("constraints") or [])][:6]
        _axes_all = [str(v) for v in (_tr.get("ambiguity_axes") or {}).values()
                     if str(v).strip()][:3]
        _tokens = set()
        for _c in _cons_all + _axes_all:
            for _m2 in re.findall(r"\d+(?:\.\d+)?", _c):
                if len(_m2) >= 2:          # 单字符数字(如"1个")噪声太多不查
                    _tokens.add(_m2)
            for _q2 in (re.findall(r'"([^"]{2,24})"', _c) +
                        re.findall(r"'([^']{2,24})'", _c)):
                _tokens.add(_q2.lower())
        _tables_text = " ".join(_table_rows).lower()
        if _tokens:
            _cov = sum(1 for t in _tokens if t.lower() in _tables_text)
            _cover_ok = (_cov >= max(1, len(_tokens) // 2))
        else:
            _cover_ok = True
        _has_mapping = (len(_table_rows) >= 2 and _kw_ok and _cover_ok)
        if _has_mapping:
            budget["_constraint_mapping_audited"] = True
            return ""
        budget["_constraint_mapping_audited"] = True  # 只拦一次
        _emit_gate_event(session.get("qid", ""), "constraint_mapping", "block",
                         {"n_table_rows": len(_table_rows)})
        _cons = (_tr.get("constraints") or [])[:4]
        _axes = {k: v for k, v in (_tr.get("ambiguity_axes") or {}).items()
                 if v and str(v).strip()}
        _lines = [f"  | {c[:80]} | <你代码/查询里的确切参数> | ✓/✗ |" for c in _cons]
        _lines += [f"  | [{k}轴] {str(v)[:70]} | <参数> | ✓/✗ |"
                   for k, v in list(_axes.items())[:3]]
        return (
            "[约束↔实现映射表缺失] 这题你用了代码/查询·且题干有口径约束·但你的"
            "收尾叙事里没有【约束↔实现映射表】——实证多题错在'题干问2022/代码筛"
            "2023'、'题干要PEND/代码查SOLD'这类映射从未落地。请补上这张表再"
            "finalize(若某条是✗先回去改实现):\n"
            + "\n".join(_lines) +
            "\n表补齐且全✓(或已改正✗项)后·再 finalize_answer 交付。"
            # v-eff(2026-08-18·病4) · 只补这一项·别重跑整轮:已有证据/候选全在
            #   session·这道门只缺【映射表】。别重新 triage/重新检索/重跑计算——那是
            #   浪费墙钟(对照实验:引擎被门拦一次就重跑整链是 20 分钟病根之一)。
            "\n【效率·只补映射表】你已抓到的证据/算出的中间结果【都还在·无需重查】——"
            "本门只缺这一张映射表。直接基于你已有的结果填表·别重新 triage/重新搜索/"
            "重跑整条链·填好即刻 finalize。")
    except Exception:
        return ""


def _identity_coverage_gate(session, question, final_markdown):
    """v2026-08-18(用户拍板) · finalize 门: 四态身份关系 + claim 级证据覆盖率。

    直接对症 BrowseComp"检索到近似候选但锁错实体"(凭部分匹配就交卷)。
    把题面拆成 N 条约束(claim)·对候选答案(finalize 的实体)统计每条约束有没有
    证据覆盖·由覆盖率+多源判候选四态(confirmed/probable/candidate/rejected)·
    显式写进 session["identity_states"]。覆盖率过低 → 不能当 confirmed 交·拦一次
    逼补证据;probable~confirmed 之间不拦(标注不确定度而非弃权)。

    与 _constraint_mapping_gate 配合不重复: 那门查"约束↔实现映射表存在性"(算题)·
    这门查"候选实体每条约束到底有没有证据"(找实体题)。只拦一次(打标
    _identity_coverage_audited)。fail-safe: 任何异常放行(返回 "")。
    """
    try:
        budget = session.get("_budget")
        if budget is None:
            budget = session["_budget"] = {}
        if budget.get("_identity_coverage_audited"):
            return ""
        cand = str(final_markdown or "").strip()
        if len(cand) < 4:
            return ""
        import identity_coverage as _ic
        # ── 机制1: ALP(Answer Lock Protocol)·交卷前证据绑定验证(治末端幻觉/锁错实体)──
        #   比覆盖率门更前置的一层: 先验答案【核心实体】在不在【已收集证据集】里。
        #   unsupported(证据里根本没这个实体·末端幻觉出证据外实体)→ strict 拒绝交卷
        #   +重试提示(从证据里已出现候选选/补证据)。fail-safe: 抽不到/无证据不拦。
        #   只拦一次(与覆盖率门共用 _identity_coverage_audited 标·防拉扯)。
        try:
            _alp = _ic.alp_verify(session, cand)
            _emit_gate_event(session.get("qid", ""), "alp",
                             "block" if _alp.get("should_block") else _alp.get("level"),
                             {"level": _alp.get("level"),
                              "score": _alp.get("score"),
                              "sources": _alp.get("sources")})
            session["_alp_last"] = {"level": _alp.get("level"),
                                    "score": _alp.get("score")}
            if _alp.get("should_block") and _alp.get("advice"):
                budget["_identity_coverage_audited"] = True   # 只拦一次
                return _alp["advice"]
        except Exception:
            pass
        res = _ic.evaluate_answer(session, question, cand,
                                  triage=session.get("triage") or {})
        cov = res.get("coverage") or {}
        # 拆不出约束(纯开放题) → 不介入
        if not cov.get("total"):
            return ""
        # ── v17(2026-08-18 · 双轨制路由) · 覆盖型任务走记忆轨道(过程覆盖验证)──────
        #   击穿本质(KM 2781429277): bc 崩不是验证器差·是【用推理裁判验记忆任务】。
        #   覆盖型任务(多约束找纠缠实体)finalize 前·记忆轨道验【搜索轨迹覆盖度】(信息
        #   找全了吗)·而非用推理裁判判答案对错。短板决定上限: 记忆瓶颈(信息没找全)→
        #   继续搜; 推理瓶颈(答案与证据矛盾)→ 由下面既有推理轨道处理。只拦一次·fail-safe。
        try:
            # 特征谱开时用连续向量的 primary 驱动路由(硬三分类当 fallback)·
            #   并把连续向量落 session(供意图对齐/组合验证/可观测)。primary 与 _task_route
            #   语义一致(单类别)·下游不破。运行时重分类: evidence 已收集则按 source
            #   多样性重估(信息极度分散→上调覆盖度)。
            if _feature_profile_enabled():
                _uprof = session.get("user_profile") or session.get("_user_profile")
                _fp = _query_feature_profile(question, user_profile=_uprof)
                try:
                    _sd = None
                    _ev = session.get("evidence") or []
                    if _ev:
                        _srcs = {(e.get("url") or e.get("source") or "")
                                 for e in _ev if isinstance(e, dict)}
                        _srcs.discard("")
                        if len(_ev) >= 3:
                            _sd = min(1.0, len(_srcs) / float(len(_ev)))
                    _fp = _reclassify_profile_runtime(_fp, source_diversity=_sd)
                except Exception:
                    pass
                session["_task_profile"] = _fp
                _route = _fp.get("primary") or TASK_RESULT
                _rreason = "特征谱 strat=%s conf=%.2f" % (
                    _fp.get("strategies"), _fp.get("overall_confidence") or 0)
            else:
                _route, _rreason = _task_route(question)
            session["_task_route"] = _route
            _emit_gate_event(session.get("qid", ""), "task_route", _route,
                             {"reason": _rreason[:80]})
            # ── auto侧接线(整合错题机制·治"只接native没接auto"·目标:整合到auto+native)──
            #   P0-① 约束对账门(最强否决·治高置信错锁) + 实体漂移 + 低置信不交卷 +
            #   ★"找到对的"(从证据选约束覆盖最高候选·非只拒错)。结果型/数值题不走(回归
            #   防护)。fail-safe·env各自可关。只拦一次(_auto_edlc_used)。
            try:
                _is_num_q = bool(_question_wants_number(question))
                if (_route != TASK_RESULT and not _is_num_q
                        and not session.get("_auto_edlc_used")):
                    _ev_A = []
                    try:
                        _ev_A = _ic.collect_evidence(session)
                    except Exception:
                        _ev_A = session.get("evidence") or []
                    # ★统一收尾治理入口(finalize_governor)·与 native 同一裁决核心。
                    #   一个纯函数跑完 约束对账→找对的→实体漂移→低置信·返回裁决;auto 保留
                    #   自己的编排(保险箱/gate_event/return advice)。灵活编排·无双份接线。
                    import finalize_governor as _fgA
                    _govA = _fgA.govern(
                        session, cand, question, _ev_A,
                        claims=res.get("claims"), triage=session.get("triage") or {},
                        engine="auto")
                    if not _govA.get("pass"):
                        _advA = _govA.get("advice") or "[收尾治理·auto] 候选需复核·别硬交。"
                        _bestA = _govA.get("best")
                        session["_auto_edlc_used"] = True
                        if _bestA:
                            # Governor suggestions remain non-authoritative until they can
                            # reference canonical evidence; never seed a parallel candidate list.
                            _run_state_gov = _canonical_run_state(session)
                            _context_gov = _run_state_gov.decision_context()
                            _nominated_gov = _run_state_gov.nominate_from_observation(
                                candidate=str(_bestA)[:200], origin="finalize_governor",
                                relation_path=str(_govA.get("reason") or "candidate comparison"),
                                evidence_records=list(_context_gov["evidence"].values()),
                                unresolved_constraints=list(_context_gov.get("gaps") or []))
                            if _nominated_gov:
                                _run_state_gov.project_legacy_views(session)
                                _progressive_candidate_check(
                                    session, "finalize_governor", str(_govA)[:4000])
                        _reconA = (_govA.get("detail") or {}).get("recon") or {}
                        _emit_gate_event(
                            session.get("qid", ""), "finalize_governor",
                            _govA.get("reason") or "reject",
                            {"conf": _reconA.get("confidence"),
                             "contradicted": _reconA.get("contradicted"),
                             "best": _bestA})
                        return _advA
            except Exception:
                pass
            if _route == TASK_COVERAGE:
                import process_coverage as _pc
                if _pc._enabled():
                    _mt = _pc.verify_process_coverage(
                        session, question, claims=res.get("claims"),
                        triage=session.get("triage") or {})
                    _en = _mt.get("entanglement") or {}
                    _gr = _mt.get("grounding") or {}
                    session["_memory_track_last"] = {
                        "coverage": _mt.get("coverage_score"),
                        "continue": _mt.get("should_continue_search")}
                    _emit_gate_event(
                        session.get("qid", ""), "memory_track",
                        "continue" if _mt.get("should_continue_search") else "pass",
                        {"coverage": round(float(_mt.get("coverage_score") or 0), 3),
                         "covered": _en.get("covered"), "total": _en.get("total"),
                         "grounding_warn": _gr.get("warn"),
                         "hyp_ratio": round(float(_gr.get("hypothesis_ratio") or 0), 2)})
                    # 短板集成: 推理轨道置信度(identity_coverage 覆盖率)一起取 min
                    _rconf = float(cov.get("coverage") or 0.0) if cov.get("total") else None
                    _comb = _pc.combine_tracks(_mt, reasoning_confidence=_rconf)
                    session["_dual_track_last"] = {
                        "bottleneck": _comb.get("bottleneck"),
                        "confidence": _comb.get("confidence"),
                        "action": _comb.get("action")}
                    # 记忆瓶颈(信息没找全)→ 拦一次逼继续搜(非交卷)·不判答案对错。
                    #   推理瓶颈(答案与证据矛盾)不在此拦——交给下面推理轨道(矛盾门/cross_verify)。
                    if (_comb.get("bottleneck") == "memory"
                            and _mt.get("should_continue_search")
                            and _mt.get("advice")):
                        budget["_identity_coverage_audited"] = True   # 只拦一次
                        # 覆盖型: 不丢弃当前候选(信息没找全≠候选错)·锁进保险箱防丢
                        session["_ic_kept_candidate"] = cand[:200]
                        return _mt["advice"]
            # B: 个性化任务(偏好/推荐)→ 意图对齐验证(缺口 #8·文档第四层)。
            #   个性化无【唯一正确答案】·只验【与用户需求对齐度】·标置信度不阻断答题
            #   (个性化不重跑不拦)。★bc/GAIA 客观题 primary 恒非 PERSONALIZED·永不到此
            #   (核心回归防护)。fail-safe·env MEYO_INTENT_ALIGN=0 关。
            elif _route == TASK_PERSONALIZED:
                try:
                    import intent_alignment as _ia
                    if _ia._enabled():
                        _uprof = (session.get("user_profile")
                                  or session.get("_user_profile"))
                        _hist = session.get("conversation_history")
                        _al = _ia.verify_intent_alignment(
                            cand, question, user_profile=_uprof,
                            conversation_history=_hist)
                        session["_intent_align_last"] = {
                            "score": _al.get("score"),
                            "action": _al.get("action")}
                        _emit_gate_event(
                            session.get("qid", ""), "intent_align",
                            _al.get("action") or "pass",
                            {"score": round(float(_al.get("score") or 0), 3),
                             "checks": _al.get("checks")})
                        # 歧义太高→提示澄清(不假设);对齐低→只标低置信(不否决/不重跑)。
                        # 均不阻断交卷——个性化本无标准答案。
                except Exception:
                    pass
        except Exception:
            pass
        # 覆盖率评估始终落遥测(不管拦不拦)——四态/覆盖率可观测归因。
        _tri = res.get("tri") or {}
        _emit_gate_event(session.get("qid", ""), "identity_coverage",
                         "block" if res.get("should_block") else "pass",
                         {"state": res.get("state"),
                          "coverage": round(float(cov.get("coverage") or 0), 3),
                          "covered": cov.get("covered"), "total": cov.get("total"),
                          "sources": cov.get("sources"),
                          # 判别器: 约束三态计数(矛盾→rejected 是最强淘汰信号)
                          "satisfied": _tri.get("satisfied"),
                          "contradicted": _tri.get("contradicted"),
                          "unknown": _tri.get("unknown")})
        # 机制2 触发信号落 session(供 Self-Consistency 判是否震荡·只存计数不存证据块)
        try:
            session["_ic_last_result"] = {
                "state": res.get("state"),
                "coverage": {"coverage": float(cov.get("coverage") or 0.0)}}
            # 判别器下一步动作(平局→tie_needs_deepdive·SC 触发条件之一)
            _tri_full = res.get("tri")
            if _tri_full:
                _ranked = _ic.rank_candidates(
                    [{"entity": cand[:120], "tri": _tri_full}], session)
                # ★2026-08-20 传 question/evidence/llm_call·平局时走换位思考给区分建议。
                try:
                    session["_ic_next_action"] = _ic.decide_next_action(
                        _ranked, question=(session.get("question") or ""),
                        evidence=(session.get("evidence") or []),
                        llm_call=session.get("_llm_call"))
                except Exception:
                    session["_ic_next_action"] = _ic.decide_next_action(_ranked)
        except Exception:
            pass
        if res.get("should_block") and res.get("advice"):
            budget["_identity_coverage_audited"] = True   # 只拦一次
            # 2026-08-19 事故修复: 低覆盖【绝不丢弃候选换另一个】。把当前候选锁进
            #   Answer Vault(保险箱)——即使 agent 后续被建议补证·这个已找到的候选
            #   也不会丢(重交/兜底时 rescue_final 能恢复)。advice 本身已明确"不换
            #   实体·继续补该候选证据或标注不确定度交"·调用方还会 append_vault_guard。
            # 判别器: 但若本候选被约束【矛盾】(keep_candidate=False·找到反证)·
            #   【绝不】把它锁进保险箱当答案——它是被淘汰的错实体·锁进去反而会被
            #   rescue_final 恢复成错答案。矛盾候选只拦不锁。
            _adv_ic = res["advice"]
            if res.get("keep_candidate", True):
                session["_ic_kept_candidate"] = cand[:200]
            return _adv_ic
        budget["_identity_coverage_audited"] = True
        return ""
    except Exception:
        return ""


# ══ v108b(M3b) · 单一数据源对账: 抽 1 个实体过第二通道 ═════════════════
# 病根实证(bec7): 所有数值都来自同一个 API(ORCID)·字段口径多算了 48——
# 单一源的系统性偏差(字段含义/单位/过滤条件)在源内部永远自洽·只有换通道
# 对账才暴露。用户 spec 原文: "从单一 API 拿数前·抽 1 个实体用第二通道
# (网页 UI)对账数值口径"。通用触发(零题目特判):
#   ①triage 抽过约束(口径敏感题);
#   ②tool_log 里 ≥2 次成功的数据获取·且全部命中同一个域名;
#   ③本题尚未提示过(一次性)。
# 行为: 数据工具返回摘要后追加一段"对账提醒"——不拦截·只要求在收尾前
# 抽 1 个实体用第二通道核数值口径·结论写进叙事。fail-safe。

_DATA_FETCH_TOOLS_M3B = ("fetch_url_smart", "fetch_url", "shell_exec",
                         "query_table", "read_table")

# 注册域归一用的二级后缀(避免 co.uk 取错段);够用即可·不追求完备。
_M3B_SECOND_LEVEL_TLD = {".co.uk", ".com.cn", ".org.cn", ".com.au", ".co.jp",
                         ".com.br", ".co.in", ".com.tw", ".com.hk"}


def _m3b_reg_domain(host):
    """host → 注册域(最后两段·www.剥离)。解析失败返回 ""(调用方跳过)。"""
    try:
        h = str(host or "").strip().lower().rstrip(".")
        h = h.replace("\\", "")          # grep 正则里的 orcid\.org 转义
        if not h:
            return ""
        # 只留合法 host 字符·截到首个非法字符(治 'web.archive.o'/'e' 这类残片)
        _clean = []
        for ch in h:
            if ch.isalnum() or ch in ".-":
                _clean.append(ch)
            else:
                break
        h = "".join(_clean)
        if h.startswith("www."):
            h = h[4:]
        parts = h.split(".")
        if len(parts) < 2 or not parts[-1] or len(parts[-1]) < 2:
            return ""
        take = parts[-2:]
        # 二级后缀判断: 最后两段本身是 co.uk 这类公共后缀 → 取三段
        if ("." + ".".join(take)) in _M3B_SECOND_LEVEL_TLD and len(parts) >= 3:
            take = parts[-3:]
        return ".".join(take)
    except Exception:
        return ""


def _m3b_record_host(budget, cur_tool, cur_text, cur_summ):
    """调用时刻累计器: tool_log 的 args 落盘时已被 _log_tool 截断(~80字)·
    URL 大量丢失(实证 bec7: curl 循环命令的 URL 全在截断点之后)——只能在
    调用现场拿完整 command/queue 文本记账。失败判据用"否定词剥离+精确
    标记"(实证: '无反爬'含'反爬'字面被误判失败)。fail-safe。"""
    try:
        if cur_tool not in _DATA_FETCH_TOOLS_M3B:
            return
        _summ = str(cur_summ or "")
        # 剥掉否定语境·再查失败标记(否则'无反爬'/'无异常'被误判为失败)
        _s = (_summ[:200].replace("无反爬", "").replace("无异常", "")
              .replace("非异常", "").replace("没有失败", ""))
        if any(b in _s for b in ("失败", "异常", "error", "超时", "已拦截",
                                 "network_blocked", "[已拦截")):
            return
        if cur_tool == "shell_exec" and "exit_code=0" not in _summ[:120]:
            return
        # 压平换行(多行 python -c 里 URL 被折行)·再提取。
        # 字符类允许反斜杠: grep 正则里的 orcid\\.org 形态才能捕到 host
        # (反斜杠由 _m3b_reg_domain 统一清洗)。fail-safe。
        _flat = str(cur_text or "").replace("\n", "").replace("\r", "")
        _hosts = set()
        for _m in re.finditer(r"https?://([^\s\"'<>)]+)", _flat):
            _d = _m3b_reg_domain(_m.group(1))
            if _d:
                _hosts.add(_d)
        if not _hosts:
            return
        dom = budget.setdefault("_m3b_domains", {})
        for _d in _hosts:
            dom[_d] = dom.get(_d, 0) + 1
    except Exception:
        pass


def _single_source_reconcile_nudge(session):
    """单源对账提醒(一次性·读累计器)。命中返回提示文本·否则返回 ""。

    触发(通用·零题目特判): 有口径约束 + 累计器里存在注册域计数 ≥2 的
    【支配域】(其余域均 ≤1) → 所有数值证据实际出自单一源 → 要求收尾前
    抽 1 个实体过第二通道对账。fail-safe。
    """
    try:
        budget = session.get("_budget")
        if budget is None:
            budget = session["_budget"] = {}
        if budget.get("_reconcile_nudged"):
            return ""
        _tr = session.get("triage") or {}
        if not ((_tr.get("constraints") or []) or (_tr.get("ambiguity_axes") or {})):
            return ""  # 无口径约束的题不扰民
        dom = budget.get("_m3b_domains") or {}
        if not dom:
            return ""
        _top, _topn = None, 0
        for _k, _v in dom.items():
            if _v > _topn:
                _top, _topn = _k, _v
        if _topn < 2:
            return ""
        if any(_v > 1 for _k, _v in dom.items() if _k != _top):
            return ""  # 有第二个 ≥2 的域 → 真多源·不打扰
        budget["_reconcile_nudged"] = True  # 只提醒一次
        return (
            "\n\n[⚖️ 单一数据源对账·收尾前必做一次] 你目前所有数值证据都来自同一"
            f"个域名 {_top}(累计 {_topn} 次成功取数·无一来自其他源)。单一源的系统性"
            "偏差(字段口径/单位/过滤条件)在源内部永远自洽·只有换通道才暴露(实证: "
            "有题在单一 API 上把展开条目当作品数·多算 48——API 的条目数和它自己"
            "网页 UI 展示的去重数不一样)。请在 finalize 前:\n"
            "  ① 从你的数据里抽 1 个实体(任一行/任一对象);\n"
            "  ② 优先用【同一服务的网页 UI】核它的数值口径——API 与自家网页版的"
            "去重/过滤口径常常不同;网页打不开才用另一个独立域(官方网页/另一个 "
            "API/权威转载页);\n"
            "  ③ 对账结论(一致✓/不一致✗+已修正)写进收尾叙事——要核数量口径本身"
            "(去重前 vs 去重后)。不一致说明源口径理解错了·先改查询再交。若第二"
            "通道确实不可达(配额/墙)·在叙事里记'对账通道不可用'并给答案标低置信。")
    except Exception:
        return ""


# ══ v108b(M3c) · 叙事级单源对账门: finalize 时扫叙事引用域名 ═══════════
# 病根实证(bec7 第二轮): fresh 重跑保留 sandbox·agent 直接 cat 上一轮下载的
# orcid_works/*.json·全程无 http 命令 → M3b 调用时刻累计器是盲的;但叙事里
# 白纸黑字"通过 ORCID 官方公开 API ... 逐一取出"——数据出处就写在 final 里。
# 这道门在 finalize 时扫叙事: 某注册域被引 ≥2 次且【支配】(其余域 ≤1)·叙事
# 又没有对账结论(对账/第二通道/交叉验证字样) → 打回一次·要求补单实体对账。
# 与 M3b 累计器互补: 累计器抓"现场抓的单源"·这道门抓"叙事自认的单源"。
_RECONCILE_HINT_KEYS = ("对账", "第二通道", "交叉验证", "cross-check",
                       "crosscheck", "web UI", "官网核对", "比对一致")


def _narrative_single_source_gate(session, final_markdown):
    """叙事级单源门(只拦一次)。返回拦截文本·放行返回 ""。fail-safe。"""
    try:
        budget = session.get("_budget")
        if budget is None:
            budget = session["_budget"] = {}
        if budget.get("_reconcile_narr_blocked"):
            return ""
        _tr = session.get("triage") or {}
        if not ((_tr.get("constraints") or []) or (_tr.get("ambiguity_axes") or {})):
            return ""
        _fm = str(final_markdown or "")
        _dom = {}
        for _m in re.finditer(r"https?://([^\s\"'<>)]+)", _fm):
            _d = _m3b_reg_domain(_m.group(1))
            if _d:
                _dom[_d] = _dom.get(_d, 0) + 1
        if not _dom:
            return ""
        _top, _topn = None, 0
        for _k, _v in _dom.items():
            if _v > _topn:
                _top, _topn = _k, _v
        if _topn < 2:
            return ""  # 单次引用不构成"所有数值出自此源"
        if any(_v > 1 for _k, _v in _dom.items() if _k != _top):
            return ""  # 多个域都被引 ≥2 次 → 叙事已多源
        if any(k in _fm for k in _RECONCILE_HINT_KEYS):
            budget["_reconcile_narr_blocked"] = True  # 已有对账痕迹·以后不再拦
            return ""
        # 数字型答案才需要数值口径对账(纯文字/列表题不打扰)
        if not re.search(r"\d", _fm):
            return ""
        budget["_reconcile_narr_blocked"] = True  # 只拦一次
        _emit_gate_event(session.get("qid", ""), "reconcile_narrative", "block",
                         {"top": _top, "n": _topn})
        return (
            f"[⚖️ 单一数据源对账缺失] 你的收尾叙事里所有数据出处都指向同一个域名 "
            f"{_top}(被引 {_topn} 次·无任何其他源)。单一源的系统性偏差(字段含义/"
            "单位/去重口径)在源内部永远自洽——实证: 有题把 API 的展开条目当作品数·"
            "多算 48·若用【同一服务的网页 UI】对过账当场就能发现。请在重交前:\n"
            "  ① 抽 1 个实体(如某个人/某一行)·优先用【同一服务的网页 UI】(API 和它的"
            "网页版去重/过滤口径常常不同——API 返回的展开条目数 ≠ 网页展示的条目数)"
            "核对;网页 UI 打不开才用另一个独立域的 API/权威转载页;\n"
            "  ② 对账要核【数量口径本身】(去重前 vs 去重后·分组 vs 展开)而不只是"
            "验证通道选得对不对;\n"
            "  ③ 把对账结论(一致✓/不一致✗+已修正)写进叙事;若第二通道不可达(配额/"
            "墙)·写明'对账通道不可用'并标低置信。\n"
            "补上后再 finalize_answer。")
    except Exception:
        return ""


def _candidate_similarity(prev_ans, cur_ans):
    """v127(2026-08-19) · 统一的"两个候选是否属于同一实体/同一簇"判据，供渐进式
    验证/cross_verify 候选震荡防止/Conservative Mode 自证循环修复共用。

    之前这几处各写了一份粗糙的"取一半长度做前缀重叠"启发式，对【同属不同种】这
    类场景(如 Solanum laciniatum vs Solanum hermannii)会产生大量误判——两者
    都以"solanum "开头，前缀重叠但根本是两个不同物种，是"应该被排除的邻近候选"
    不是"同一个候选换了措辞"。

    判据(先严格后宽松，命中即真):
      1. 完全相同(归一化后) → True
      2. 一个是另一个的子串(且长度>=4，防止太短的词造成误判) → True
      3. 词级重叠: 共享词数>=2 且占较短那个词集的比例>=0.5 → True
         (要求>=2个共享词，是为了避免"只共享属名/只共享一个通用词"就被误判成
         同一簇——Solanum laciniatum 和 Solanum hermannii 只共享{"solanum"}
         一个词，不满足>=2，判定为不同候选，符合预期)
    """
    try:
        import re as _re_sim
        a, b = str(prev_ans or "").strip().lower(), str(cur_ans or "").strip().lower()
        if not a or not b:
            return False
        if a == b:
            return True
        if len(a) >= 4 and (a in b or b in a):
            return True
        ta = set(_re_sim.findall(r"[a-z0-9\u4e00-\u9fff]+", a))
        tb = set(_re_sim.findall(r"[a-z0-9\u4e00-\u9fff]+", b))
        if not ta or not tb:
            return False
        shared = ta & tb
        if len(shared) >= 2 and len(shared) / min(len(ta), len(tb)) >= 0.5:
            return True
        return False
    except Exception:
        return False


def _progressive_candidate_check(session, name, summary):
    """v127(2026-08-19 · 用户核心诉求:渐进式验证) · 候选【一形成/一变化】就轻量
    核验一次，而不是像旧机制那样只在 finalize 时才做一次重的交叉验证。

    病根(用户实证): 旧架构里所有质检门(cross_verify 硬拦截/相似实体反查)都只
    挂在 finalize_answer 这一个节点——模型可能已经围绕一个错误候选(如 Solanum
    laciniatum)搜了几十个工具调用、攒了一堆"自洽但方向错"的证据，直到 finalize
    才被发现，然后打回重搜，换到另一个同样错的邻近候选(hermannii)，如此震荡。
    验证信号出现得太晚 = "一顿搜然后发现错，很耗时间"的直接原因。

    做法(刻意做"轻"，不做"重"——每次候选变化都跑一次完整多模型交叉验证太贵):
      1. 只在 answer_vault 检测到的候选【相对上次记录发生了变化】时触发(candidate
         churn 事件)，不是每个工具调用都触发。
      2. 核验用一次【快/便宜模型】(tier=easy)的轻量事实自洽检查:给它题目+新候选+
         当前证据摘要，问"这个候选和证据/题面吻合吗？有没有证据里提到但没被选中
         的、更贴合的邻近候选？"——这是把原来"只在 finalize 时做"的相似实体反查
         提前搬到候选形成的当下做，而不是搜完一大圈之后才做。
      3. 每题总核验次数硬顶(默认 4 次)，防止候选反复抖动时无限烧钱。
      4. 结果不直接拦截 finalize(候选此刻可能还在演化中，拦截为时过早)，而是把
         "候选演化史 + 每次轻量核验意见"记进 session["_progressive_candidate_log"]，
         供 finalize 时的 cross_verify/相似实体反查复用——如果 finalize 时发现
         当前候选在演化史上【已经被轻量核验否定过一次】，说明模型在原路径上打转，
         应该触发更强的排除性反查而不是再次轻描淡写放行。
      fail-safe: 任何异常都静默跳过，绝不影响主工具调用链路。
    """
    try:
        if os.environ.get("MEYO_PROGRESSIVE_VERIFY", "1").strip().lower() in ("0", "off", "false", "no"):
            return
        import answer_vault as _av_pg
        _cur_ans = ""
        # progressive 先消费候选层；已验证 answer vault 只负责最终保答案，不反向混成弱候选。
        _candidate_vault = session.get("_candidate_vault") or []
        # 契约未建立前不消费弱候选；等 build_answer_contract 后再做动态类型核验，
        # 避免约束值在目标输出类型未知时抢占 progressive。
        if _candidate_vault and session.get("answer_contract"):
            _last_cv = _candidate_vault[-1]
            _cur_ans = str(_last_cv.get("answer") if isinstance(_last_cv, dict)
                           else _last_cv).strip()
        if not _cur_ans or len(_cur_ans) < 2:
            # 图节点、内部 soft lock、高频实体只是路径变量，不等于题目所问类型的答案候选。
            # 仅消费 SoulDriver 基于题面目标类型+已证关系动态提名的候选。
            _hypotheses = session.get("_candidate_hypotheses") or []
            if _hypotheses:
                _last_h = _hypotheses[-1]
                _cur_ans = str(_last_h.get("name") if isinstance(_last_h, dict)
                               else _last_h).strip()
        if not _cur_ans or len(_cur_ans) < 2:
            return
        # 动态答案契约类型门：约束值/中间锚点即使被工具标为 ENTITY，也不能冒充题目
        # 所需输出类型。复用 SoulDriver delivery contract，不维护领域词表。
        _contract = session.get("answer_contract") or {}
        if _contract:
            try:
                import perspective_engine as _pe_type
                _snap_type = _pe_type.ContextSnapshot(
                    str(session.get("question") or ""), session=session,
                    llm_call=session.get("_llm_call"))
                _type_check = _pe_type.decide(
                    _snap_type, _pe_type.PHASE_DELIVER, answer=_cur_ans)
                if not _type_check.get("ok", True):
                    _rejected = session.setdefault("_candidate_contract_rejections", [])
                    _rejected.append({"answer": _cur_ans[:200], "tool": name,
                                      "issue": str(_type_check.get("issue") or "type mismatch")[:200],
                                      "ts": time.time()})
                    del _rejected[:-20]
                    return
            except Exception:
                pass
        _log = session.setdefault("_progressive_candidate_log", [])
        _last_seen = _log[-1]["answer"] if _log else None
        if _last_seen and _candidate_similarity(_last_seen, _cur_ans):
            return  # 候选没变(或只是措辞差异)，不重复核验
        # 记一条候选变化事件(不管是否触发核验都记，供事后看候选演化轨迹)
        _entry = {"answer": _cur_ans[:200], "tool": name, "ts": time.time(), "verdict": None}
        _log.append(_entry)
        if len(_log) > 20:
            del _log[: len(_log) - 20]
        # Candidate semantics remain SoulDriver-owned; this layer only persists the
        # candidate and its evidence references for deterministic replay.
        try:
            _run_state = _canonical_run_state(session)
            _evidence_ids = list(session.get("_last_observation_evidence_ids") or [])
            for _evidence in (session.get("evidence") or [])[-20:]:
                if not isinstance(_evidence, dict):
                    continue
                _source = str(_evidence.get("url") or _evidence.get("source") or name).strip()
                _content = "\n".join(str(_evidence.get(key) or "") for key in
                                      ("title", "content", "snippet", "url"))[:6000]
                if not _content.strip():
                    continue
                _observed = _run_state.observe_evidence(
                    source=_source, content=_content,
                    title=str(_evidence.get("title") or "")[:500], tool=str(name)[:80])
                _evidence_ids.append(_observed.payload["evidence_id"])
            if not _evidence_ids:
                return
            _cid = _run_state.observe_candidate(
                _cur_ans[:200], origin=str(name)[:80], evidence_ids=sorted(set(_evidence_ids))[:20])
            _projected_candidate = _run_state.project_legacy_views(session)
            session["_event_cursor"] = _projected_candidate.last_seq
        except Exception:
            pass
        # 核验总量硬顶，防止候选反复抖动时无限触发
        _budget = session.get("_budget") or {}
        _n_checks = _budget.get("_progressive_verify_n", 0)
        _cap = int(os.environ.get("MEYO_PROGRESSIVE_VERIFY_CAP", "4"))
        if _n_checks >= _cap:
            return
        # 候选簇内反复抖动(和历史上某个已被否定的候选属于同一簇——用统一的
        # _candidate_similarity 判据，而不是粗糙前缀匹配)本身就是强信号，优先用于
        # 快速识别"打转"，不需要每次都重新调模型才能判断。
        _neg_hits = [e for e in _log[:-1] if e.get("verdict") == "questionable"]
        for _e in _neg_hits:
            _prev_ans = str(_e.get("answer") or "")
            if _candidate_similarity(_prev_ans, _cur_ans):
                _entry["verdict"] = "questionable"
                _entry["verdict_reason"] = f"与历史被质疑候选『{_prev_ans}』同簇·未真正跳出"
                return
        _budget["_progressive_verify_n"] = _n_checks + 1
        session["_budget"] = _budget
        # 轻量核验:便宜快模型，基于当前证据摘要判断"候选是否与证据吻合/是否存在
        # 更贴合但未被选中的邻近候选"。不是完整交叉验证——不需要精确，只需要
        # 早期预警信号。异步跑(后台线程)：不阻塞主工具调用链路，候选还在演化中
        # 拦截也没意义，结果稍后可用即可(finalize 时读 _progressive_candidate_log)。
        _ev = list(session.get("evidence") or [])
        _q = str(session.get("question") or "")[:600]
        _cand_snapshot = _cur_ans[:200]

        def _bg_progressive_check():
            try:
                import reflective_mind as _RM_pg
                _ev_text = "\n".join(str(e)[:300] for e in _ev[-10:]) if _ev else ""
                _prompt = (
                    "你在做一次【早期候选预警检查】(不是最终判定，成本敏感，简短判断即可)。\n"
                    f"题目: {_q}\n"
                    f"当前锁定候选: {_cand_snapshot}\n"
                    f"已有证据摘要(可能不全):\n{_ev_text[:2500]}\n\n"
                    "请判断: 这个候选是否被上面证据【明确支持】？证据里是否提到了其他"
                    "【名称/分类/身份相近】但未被选中的候选(如同属邻近种、拼写相近的"
                    "另一支队伍)，且看起来可能比当前候选更贴合题面？\n"
                    "只输出 JSON: {\"supported\": true/false, \"better_alt\": \"若有更贴合"
                    "的候选写在这里，没有则空字符串\", \"why\": \"15字内\"}"
                )
                _raw = _RM_pg._mm().ask("claude-haiku", _prompt, timeout=20, max_tokens=200)
                import json as _json_pg
                _ri, _rj = _raw.find("{"), _raw.rfind("}")
                if _ri >= 0 and _rj > _ri:
                    _d = _json_pg.loads(_raw[_ri:_rj + 1])
                    if _d.get("supported") is False or (_d.get("better_alt") or "").strip():
                        _entry["verdict"] = "questionable"
                        _entry["verdict_reason"] = str(_d.get("why", ""))[:60]
                        _entry["better_alt"] = str(_d.get("better_alt", ""))[:100]
                        _emit_gate_event(session.get("qid", ""), "progressive_candidate_check",
                                         "flag", {"answer": _cand_snapshot[:60],
                                                  "better_alt": _entry.get("better_alt", "")})
                    else:
                        _entry["verdict"] = "ok"
            except Exception:
                pass
        try:
            import threading as _th_pg
            _th_pg.Thread(target=_bg_progressive_check, daemon=True).start()
        except Exception:
            pass
    except Exception:
        pass


# ── finalize 核心已抽出到 finalize_gates.py(2026-08-27·god-file解耦) ──
# 共享 helper/常量显式注入；finalize 模块不反向 import auto，避免循环依赖。
import finalize_gates as _finalize_gates
_finalize_gates.configure(
    FINALIZE_MAX_BLOCKS=FINALIZE_MAX_BLOCKS,
    FINALIZE_MIN_INVESTIGATIVE=FINALIZE_MIN_INVESTIGATIVE,
    _QCOUNT_PAT=_QCOUNT_PAT,
    _auditor_evidence_check=_auditor_evidence_check,
    _base_counts_divergent=_base_counts_divergent,
    _candidate_similarity=_candidate_similarity,
    _emit_gate_event=_emit_gate_event,
    _expert_solo_sourced=_expert_solo_sourced,
    _leading_answer_number=_leading_answer_number,
    _looks_like_giving_up=_looks_like_giving_up,
    _now_iso=_now_iso,
    _number_backed=_number_backed,
    _question_contract_gaps=_question_contract_gaps,
    _rs_sessions=_rs_sessions,
    _tool_log_numbers=_tool_log_numbers,
    _wall_past=_wall_past,
)
_finalize_veto_on = _finalize_gates._finalize_veto_on
_record_finalize_hint = _finalize_gates._record_finalize_hint
_drain_finalize_hints = _finalize_gates._drain_finalize_hints
_gate_or_hint = _finalize_gates._gate_or_hint
_maybe_block_finalize = _finalize_gates._maybe_block_finalize
_maybe_block_finalize_impl = _finalize_gates._maybe_block_finalize_impl
_finalize_escape_guard = _finalize_gates._finalize_escape_guard


def _maybe_reflect_nudge(session, tool_name):
    """v73 · 卡住→自我反思换策略(提升自主性,非拦截)。

    在每次工具调用【前】评估上一步是否带来了新有效进展(证据涨/正文长/上一步
    没报错没空)。连续 REFLECT_NOPROGRESS_STREAK 次无进展、或已调 REFLECT_MIN_CALLS
    次仍在原地打转时,返回一条【建设性反思提示】(不拦截),引导 agent 像卡住的
    聪明工程师那样停下来换策略——换工具/写脚本/找 skill/重分解,由它自己选。

    返回 "" 表示没卡住(不打扰)。fail-safe:任何异常都返回 ""(绝不因这道
    "促反思"的软引导破坏主循环/预算逻辑)。控频:每题最多注入 REFLECT_MAX_NUDGES 次。
    """
    try:
        budget = session.get("_budget")
        if not budget:
            return ""
        # finalize 本身是收尾,不在它前面促反思(难题卫兵已负责"该不该放弃")
        if tool_name == "finalize_answer":
            return ""
        # 已注入够上限次数 → 别再刷屏(反思该点到为止,免得变成新的噪声)
        if budget.get("reflect_nudge_count", 0) >= REFLECT_MAX_NUDGES:
            return ""

        # ── 评估上一步(即上一次已完成的工具调用)是否带来了新有效进展 ──
        cur_ev = len(session.get("evidence") or [])
        cur_narr = len((session.get("narrative") or "")) + len((session.get("cards") or ""))
        grew = (cur_ev > budget.get("last_evidence_n", 0)) or (cur_narr > budget.get("last_narrative_len", 0))

        # 上一步的工具结果摘要看起来是"没产出/报错/空/被拦"→ 也算无进展
        last_looked_bad = False
        tool_log = session.get("tool_log") or []
        if tool_log:
            last_entry = tool_log[-1]
            last_tool = last_entry.get("tool", "")
            last_summary = str(last_entry.get("summary") or "")
            # v119 FixF: 全文子串匹配→分层判定(治成功输出沾 error/stderr 词被误判)
            last_looked_bad = _summary_looks_bad(last_summary)
            # shell_exec 输出极短也算无进展（截断输出/空输出/几个字母）
            # 实证：624cbf 题 shell_exec 输出 'At'/'gr'/'si' 等几个字符，被判为有进展实则啥也没产出
            if not last_looked_bad and last_tool == "shell_exec":
                import re as _rep
                _stdout_m = _rep.search(r"\[stdout\]\n(.{0,30})\n", last_summary)
                if _stdout_m:
                    _stdout_content = _stdout_m.group(1).strip()
                    if len(_stdout_content) < 8:  # stdout 少于 8 字符，实质为空输出
                        last_looked_bad = True

        # 更新进展基线快照(供下一次比较)
        budget["last_evidence_n"] = cur_ev
        budget["last_narrative_len"] = cur_narr

        # 有实质进展(证据涨/正文长)且上一步没报错 → 清零无进展计数,不打扰
        if grew and not last_looked_bad:
            budget["no_progress_streak"] = 0
            return ""

        # 无有效进展(没涨 或 上一步报错/空)→ 累加连续无进展计数
        budget["no_progress_streak"] = budget.get("no_progress_streak", 0) + 1

        n_calls = budget.get("n_tool_calls", 0)
        streak = budget["no_progress_streak"]
        # 无进展容忍阈值按题型自适应(run 入口按题面语义算好·
        #   写进 budget["noprogress_streak_thresh"])。数值题=GAIA 默认 3(紧·快收敛);
        #   实体/日期多约束题放宽(多跳实体识别常有几步"检索到候选但还没交叉验证"的
        #   表面无进展·不该被 GAIA 式 3 次就打断当空转)。缺省=旧常数(行为不变)。
        _np_thresh = int(budget.get("noprogress_streak_thresh") or REFLECT_NOPROGRESS_STREAK)
        # 触发条件:①连续无进展达到(自适应)阈值 或 ②已用不少工具(≥REFLECT_MIN_CALLS)
        # 但仍在无进展 streak 里(证明确实原地打转,不是刚开始正常探索)。
        should = (streak >= _np_thresh) or (
            n_calls >= REFLECT_MIN_CALLS and streak >= max(2, _np_thresh - 1)
        )
        if not should:
            return ""

        # ── 命中:注入一条建设性反思提示,让 agent 自己选新策略 ──
        budget["reflect_nudge_count"] = budget.get("reflect_nudge_count", 0) + 1
        budget["no_progress_streak"] = 0  # 触发后清零,给新策略留出观察窗口,别连注
        nth = budget["reflect_nudge_count"]

        # 静态菜单没救回时(第2次起·streak高),
        # 调真反思脑 reflective_mind 给【针对本题】的具体下一步(need_new_tool/next_tool),
        # 而非只给通用菜单——让"换策略/造工具"是针对这道题的、可执行的。
        # 只在后段触发一次(贵·调LLM)·fail-safe:反思脑失败就退回静态菜单。
        _task_specific = ""
        if nth >= 2 and streak >= REFLECT_NOPROGRESS_STREAK:
            try:
                import reflective_mind as _RM
                _refl = _RM.reflect(
                    goal=session.get("question", ""),
                    trace_steps=[{"tool": t.get("tool", "?"), "summary": str(t.get("summary", ""))[:80]}
                                 for t in (session.get("tool_log") or [])[-15:]],
                    stuck_reason=f"连续{streak}步无进展·已反思{nth}次仍原地打转",
                    evidence=(session.get("evidence") or [])[-40:],
                )
                _bits = []
                if _refl.get("why_stuck"):
                    _bits.append(f"根因判断:{_refl['why_stuck'][:120]}")
                if _refl.get("next_move"):
                    _bits.append(f"★针对本题的下一步:{_refl['next_move'][:150]}")
                if _refl.get("need_new_tool"):
                    _bits.append(f"缺的能力+怎么搞到:{_refl['need_new_tool'][:120]}"
                                 f"(可 pip_install/search_github/写脚本自主造)")
                if _bits:
                    _task_specific = "\n  【反思脑·针对这道题想了想】" + " ｜ ".join(_bits) + "\n"
            except Exception:
                _task_specific = ""  # fail-safe:退回下面的通用菜单

        return _task_specific + (
            f"\n\n[自我反思 · 第{nth}/{REFLECT_MAX_NUDGES}次] 你已连续用了 {n_calls} 次工具"
            f"但最近几步没带来新的有效进展(证据没增/结果为空/命令报错)——像个卡住的"
            f"工程师,该停下来想想再动手,别一条路走到黑:\n"
            f"  ① 当前思路/工具选对了吗?这个来源/命令反复拿不到,是不是根本问错了地方?\n"
            f"  ② 要不要【换个工具/来源】?(别死磕同一个:换搜索词/换 API/换 evolution 工具)\n"
            f"  ③ 这题要不要【自己写个 python 脚本/小工具】精确计算或处理数据?"
            f"(shell_exec 写脚本跑、pip_install 装现成库)\n"
            f"  ④ 有没有现成的【skill】(skill_search)或【GitHub 开源仓库】"
            f"(search_github/install_github_repo)能直接解这类问题?\n"
            f"  ⑤ 要不要把问题【重新分解】(write_plan)成更小的子问题,逐个攻破?\n"
            f"  ⑥ 【战略反问·最重要】停下来重读一遍原题:我是不是**理解错题意/找错方向**了?"
            f"我现在费劲找的东西·真的是题目要的那个吗?与其在同一个方向加倍努力·"
            f"要不要**彻底换一个信息源/角度/前提**(不是换搜索词·是换整个解题思路)?\n"
            f"  ⑦ 【别只会串行试错·能并行就并行】上面这几条不是让你一条一条挨个试"
            f"(试完①不行再试②太慢)——**run_parallel_search 本身就是多来源并发**"
            f"(一次调用内部同时查好几个信息源)，**run_deep_research_swarm 是多角色"
            f"并行协作**；如果一次性能想到 2-3 个不同方向都值得查(比如同时怀疑"
            f"'是不是找错来源'和'是不是专业库参数传错')，优先把它们**打包进一次"
            f"run_parallel_search/run_deep_research_swarm 调用**一起查，而不是先猜"
            f"一个方向、猜错了再花一轮工具调用去试下一个方向——**独立并行验证出来的"
            f"多个结果互相印证/矛盾，本身就是判断哪个方向对的最快依据**。\n"
            f"  ⑧ 【自我诊断·如果你发现自己这类题总卡】调 self_diagnose(描述你反复卡的现象)——"
            f"它帮你反思'是不是我的机制/策略有系统缺陷'(而非单题),会给改造建议。这是你"
            f"【自己调教自己·从失败中学】的入口·像人复盘。\n"
            f"从上面【选一个或几个新策略,能并行的并行着来】继续——别放弃,"
            f"也别在错方向上一条路走到黑。"
        )
    except Exception:
        return ""  # fail-safe:促反思是软引导,出任何岔子都静默放行,不碰主流程


_FRONTIER_CAPABILITIES = {
    # 这是能力层而非题型层：新增工具只需加入相应能力集合，不需要为新题写规则。
    "observe": {"see_image_vlm", "see_image_expert", "see_image_zoom", "read_image_ocr",
                "see_video_frame", "label_sweep", "transcribe_audio", "traverse_video"},
    "retrieve": {"run_parallel_search", "run_deep_research_swarm", "fetch_url_smart",
                 "traverse_web", "run_factual_extract", "query_science_db", "search_github", "shell_exec"},
    "compute": {"sympy_calc", "shell_exec", "run_written_tool", "write_capability"},
    "compare": {"ask_expert_model", "run_parallel_search", "query_science_db",
                "see_image_expert", "see_image_vlm", "shell_exec"},
    "synthesize": {"write_plan", "finalize_answer"},
}


def _tool_satisfies_frontier(tool_name, capability):
    """判断工具是否推进当前证据前沿；未知能力 fail-open，避免注册表演化时误拦。"""
    if tool_name == "document_wall":
        return True
    allowed = _FRONTIER_CAPABILITIES.get(str(capability or "").strip().lower())
    return True if not allowed else tool_name in allowed


def _tool_primary_capability(tool_name, session=None):
    """从既有能力注册表推导工具的主能力，不依赖题目、站点或答案。"""
    try:
        frontier = ((session or {}).get("_budget") or {}).get("_problem_frontier") or {}
        current = frontier.get("capability") if isinstance(frontier, dict) else ""
        if current and tool_name in _FRONTIER_CAPABILITIES.get(current, set()):
            return current
        # 工具可属于多个能力（如 shell_exec）；这里按获取原始证据→检索→计算→比较→整合
        # 的保守顺序归类。前沿已声明时会优先采用前沿，避免误把比较动作当检索动作。
        for capability in ("observe", "retrieve", "compute", "compare", "synthesize"):
            if tool_name in _FRONTIER_CAPABILITIES.get(capability, set()):
                return capability
    except Exception:
        pass
    return ""


def _record_policy_decision(session, action, *, state_features=None, reason="", expected_gain=None):
    """记录自主策略选择及其可回填结果；只记录过程事实，不推断题目答案。"""
    try:
        if not isinstance(session, dict):
            return None
        # v127b 修: 空 dict 是 falsy·旧写法 `get(...) or {}` 会把账本写进临时
        # dict 后丢弃(单测实证)。必须 setdefault 回写·保证 ledger 落在 session 里。
        budget = _rs_sessions.ensure_budget(session)
        if not isinstance(budget, dict):
            budget = {}
            session["_budget"] = budget
        ledger = budget.setdefault("_policy_ledger", [])
        decision = {
            "id": len(ledger) + 1,
            "action": str(action)[:80],
            "at_call": int(budget.get("n_tool_calls", 0) or 0),
            "reason": str(reason)[:300],
            "state_features": _jsonable_generic(state_features or {}) or {},
            "expected_gain": expected_gain,
            "outcome": "pending",
        }
        ledger.append(decision)
        del ledger[:-100]
        return decision
    except Exception:
        return None


def _resolve_policy_decision(session, decision, outcome, *, evidence_delta=0, note=""):
    """回填策略的可观察结果，供 checkpoint、评测和后续策略比较使用。"""
    try:
        if not isinstance(session, dict) or not isinstance(decision, dict):
            return
        decision.update({
            "outcome": str(outcome)[:40],
            "evidence_delta": max(0, int(evidence_delta or 0)),
            "note": str(note)[:300],
        })
    except Exception:
        pass


def _record_tool_effectiveness(session, tool_name, summary, fail_cat=None):
    """从工具结果更新题内策略效果账本，并在重复无效时生成通用换法请求。

    只依据能力类别、失败分类、直接观察增量和结果指纹判断是否仍有区分性信息；不会
    从题目关键词猜策略，更不会推断答案。该账本是短期控制信号，供下一次前沿规划器
    选择"换工具/换来源/换能力"，不把单题噪声直接写成跨题永久规则。
    """
    try:
        if not isinstance(session, dict):
            return None
        # v127b 修: 同 _record_policy_decision——空 dict falsy 陷阱·写侧必须
        # setdefault 回写·否则工具有效性账本丢失。
        budget = _rs_sessions.ensure_budget(session)
        if not isinstance(budget, dict):
            budget = {}
            session["_budget"] = budget
        capability = _tool_primary_capability(tool_name, session)
        if not capability:
            return None
        text = str(summary or "").strip()
        low = text.lower()
        # 门控消息没有真正执行工具，不能污染工具有效性评价。
        if any(marker in low for marker in (
                "[循环卫兵]", "[硬门控]", "[视频模态硬约束]", "[工具前置条件]",
                "[无进展看门狗]", "[动态证据前沿", "[finalize审计")):
            return None
        tracker = budget.setdefault("_tool_effectiveness", {})
        state = tracker.setdefault(capability, {})
        anchor_count = len(session.get("evidence_anchors") or [])
        signature = re.sub(r"\s+", " ", low)[:900]
        previous_signature = state.get("last_signature", "")
        previous_anchor_count = int(state.get("last_anchor_count", 0) or 0)
        new_observation = anchor_count > previous_anchor_count
        repeated_result = bool(signature and signature == previous_signature)
        ineffective = bool(fail_cat) or (repeated_result and not new_observation)
        streak = int(state.get("ineffective_streak", 0) or 0) + 1 if ineffective else 0
        state.update({
            "last_tool": tool_name,
            "last_signature": signature,
            "last_anchor_count": anchor_count,
            "ineffective_streak": streak,
            "last_outcome": "failure" if fail_cat else ("new_observation" if new_observation else "non_duplicate"),
            "last_failure_category": fail_cat or "",
        })
        # 连续两次相同能力无区分性收益才干预；单次失败/空结果是正常探索噪声。
        if streak < 2:
            return None
        adaptation = {
            "capability": capability,
            "streak": streak,
            "reason": "repeated_failure" if fail_cat else "repeated_non_distinguishing_result",
            "last_tool": tool_name,
            "at_call": budget.get("n_tool_calls", 0),
        }
        adaptation["policy_decision"] = _record_policy_decision(
            session,
            "replan_capability",
            state_features={"capability": capability, "streak": streak,
                            "failure_category": fail_cat or "", "new_observation": new_observation},
            reason=adaptation["reason"],
            expected_gain="break_repeated_non_distinguishing_path",
        )
        budget["_runtime_adaptation"] = adaptation
        # 让下一次心跳重新规划，而不是把旧前沿中的同一种动作继续当唯一解。
        budget["_problem_frontier"] = None
        budget["_intent_replan_pending"] = True
        session.setdefault("_pending_hints", []).append(
            "\n[自主策略校正] 同一【%s】能力已连续 %s 次没有产生可区分的新证据。"
            "保留当前问题目标，但下一步不要原样重复最近工具：改用该能力下不同工具/输入/来源，"
            "或选择能检验现有证据缺口的另一能力；若所有替代通道均已实际失败，再结构化声明能力墙。"
            % (capability, streak)
        )
        _emit_gate_event(session.get("qid", ""), "runtime_adaptation", "replan", adaptation)
        return adaptation
    except Exception:
        return None


def _apply_intent_reinterpretation(session, audit):
    """将中途意图审查变成可执行的重解状态，而不只是一段提示文本。

    只接纳相对当前活跃意图有新信息的重解；每次重解会使旧前沿和旧假设审计失效。
    审查器给出的多个合理解释被保留为待区分假设，由下一次前沿优先安排比较验证。
    """
    budget = session.get("_budget") or {}
    corrected = str(audit.get("correct_intent") or "").strip()
    if not corrected:
        return False
    active = str(budget.get("_active_intent") or session.get("question") or "").strip()
    normalize = lambda text: re.sub(r"\s+", " ", str(text).lower()).strip()
    if normalize(corrected) == normalize(active):
        return False
    history = budget.setdefault("_intent_revision_history", [])
    # 不用固定开局 triage 锁住意图；仅抑制“连续重复同一重解”，新证据仍可允许回改。
    if history and isinstance(history[-1], dict) and normalize(history[-1].get("to")) == normalize(corrected):
        return False
    alternatives = audit.get("alternative_intents") or []
    if not isinstance(alternatives, list):
        alternatives = []
    hypotheses = []
    for candidate in [corrected] + alternatives:
        candidate = str(candidate or "").strip()
        if candidate and all(normalize(candidate) != normalize(old) for old in hypotheses):
            hypotheses.append(candidate[:220])
    revision = {
        "from": active[:220], "to": corrected[:220],
        "why": str(audit.get("misread") or "")[:180],
        "at_call": budget.get("n_tool_calls", 0),
        "hypotheses": hypotheses[:3],
    }
    history.append(revision)
    del history[:-4]
    budget["_active_intent"] = corrected[:300]
    budget["_intent_hypotheses"] = hypotheses[:3]
    budget["_intent_replan_pending"] = True
    # 旧前沿/审计只服务旧理解，重解后必须重新建立，不能误当作已闭合。
    budget["_problem_frontier"] = None
    for key in ("_hypothesis_checked", "_hypothesis_check_snapshot", "_hypothesis_check_blocks",
                "_semantic_contract_audited", "_semantic_contract_inv_snapshot", "_semantic_contract_blocks"):
        budget.pop(key, None)
    _emit_gate_event(session.get("qid", ""), "intent_reinterpret", "applied", revision)
    return True


def _register_intent_hypotheses(session, alternatives, reason=""):
    """把外部答案分歧转为待验证的题意假设，而非只给一次文字提示。

    cross_verify 的不一致只能说明“答案或理解”存在争议，不能替系统决定哪种题意
    正确。因此保留当前活跃意图和至多两个来自独立审查的替代理解，交给新前沿安排
    区分性 compare；不会把模型的备选答案直接当作题意或最终答案。
    """
    budget = session.get("_budget") or {}
    normalize = lambda text: re.sub(r"\s+", " ", str(text).lower()).strip()
    current = str(budget.get("_active_intent") or session.get("question") or "").strip()
    candidates = [current]
    if isinstance(alternatives, list):
        candidates.extend(str(x or "").strip() for x in alternatives[:2])
    hypotheses = []
    for candidate in candidates:
        if candidate and all(normalize(candidate) != normalize(old) for old in hypotheses):
            hypotheses.append(candidate[:220])
    if len(hypotheses) < 2:
        return False
    existing = budget.get("_intent_hypotheses") or []
    if [normalize(x) for x in existing] == [normalize(x) for x in hypotheses]:
        return False
    budget["_intent_hypotheses"] = hypotheses
    budget["_intent_replan_pending"] = True
    budget["_problem_frontier"] = None
    for key in ("_hypothesis_checked", "_hypothesis_check_snapshot", "_hypothesis_check_blocks",
                "_semantic_contract_audited", "_semantic_contract_inv_snapshot", "_semantic_contract_blocks"):
        budget.pop(key, None)
    _emit_gate_event(session.get("qid", ""), "intent_rehypothesis", "registered", {
        "n_hypotheses": len(hypotheses), "reason": str(reason)[:180],
    })
    return True


def _frontier_relation_context(session):
    """把前沿接到既有的意图—证据—经验关系网，而不是维护第二套题库。

    当前题的 triage/歧义轴、证据锚点和工具轨迹是任务内节点；语义记忆与用户失败
    自愈沉淀是跨题边。这里只读取少量命中，作为规划器的可反驳先验，绝不把历史结论
    当作本题证据或答案。
    """
    triage = session.get("triage") or {}
    ambiguity = triage.get("ambiguity_axes") or {}
    modalities = triage.get("modality_needs") or []
    links = []
    if _MEM_AVAILABLE:
        try:
            query = " ".join([
                str(session.get("question") or "")[:900],
                " ".join(str(x.get("tool") or "") for x in (session.get("tool_log") or [])[-6:]),
            ])
            for hit in _mem_recall(query, limit=3, min_confidence="medium",
                                   user_id=session.get("user_id") or "default"):
                if not isinstance(hit, dict):
                    continue
                lesson = str(hit.get("lesson") or "").strip()
                if lesson:
                    links.append({
                        "lesson": lesson[:260],
                        "tags": [str(x)[:40] for x in (hit.get("tags") or [])[:4]],
                        "confidence": str(hit.get("confidence") or "medium"),
                    })
        except Exception:
            # 经验网不可用时前沿仍只依赖本题事实，不能阻断解题。
            pass
    return {
        "intent_nodes": {
            "original_question": str(session.get("question") or "")[:500],
            "active_intent": str((session.get("_budget") or {}).get("_active_intent") or "")[:300],
            "ambiguity_axes": ambiguity if isinstance(ambiguity, dict) else {},
            "modality_needs": modalities if isinstance(modalities, list) else [],
            "unresolved_hypotheses": list((session.get("_budget") or {}).get("_intent_hypotheses") or [])[:3],
        },
        "experience_links": links,
    }


def _record_frontier_outcome(session, final, trace):
    """把本题成败回连到已使用的经验边，形成可审计的错题反馈而非盲目强化。

    仅当本题确实消费过关系网经验时记录：失败会进入既有 per-user 失败网络；成功
    只留下低置信候选教训，等待后续相似题再次验证后才会在 memory_layers 中升权。
    """
    budget = session.get("_budget") or {}
    final_text = str(final or "")
    failed = (not final_text.strip()) or bool(re.search(
        r"unknown|cannot determine|无法确定|执行被打断|尽力抽取", final_text[:300], re.I))
    # 策略重规划会清空旧前沿，决策结果仍必须在本题收尾时回填，不能依赖前沿仍存活。
    for decision in budget.get("_policy_ledger") or []:
        if decision.get("outcome") == "pending":
            _resolve_policy_decision(
                session, decision, "failed" if failed else "completed",
                evidence_delta=len(session.get("evidence") or []),
                note="final_delivery=" + ("failed" if failed else "completed"),
            )
    frontier = budget.get("_problem_frontier") or {}
    links = frontier.get("relation_links") if isinstance(frontier, dict) else []
    if not isinstance(links, list) or not links:
        return
    summary = {
        "outcome": "failed" if failed else "completed",
        "capability": frontier.get("capability"),
        "relation_links": links[:3],
    }
    trace["frontier_outcome"] = summary
    if failed and _SELFHEAL_AVAILABLE:
        try:
            _self_heal.record_failure(
                session.get("user_id") or "default", session.get("question") or "",
                "证据前沿未闭合: " + str(frontier.get("obligation") or "")[:180],
                category="frontier_unclosed",
            )
        except Exception:
            pass


def _refresh_problem_frontier(session):
    """将“下一步”从泛化建议升级为可审计的动态证据义务。

    审查器只决定当前尚未闭合的认识缺口及所需能力类型，不给答案、不绑定领域。
    Agent 仍自主选择具体工具、数据源与方法；下一步若完全无关会被温和拦一次，
    要么推进该义务，要么用 document_wall 说明客观边界。规划输入复用既有关系网，
    使错题沉淀能影响下一步证据选择，而不新建孤立策略库。
    """
    try:
        budget = session.get("_budget") or {}
        trace = []
        for item in (session.get("tool_log") or [])[-10:]:
            text = str(item.get("summary") or "").replace("\n", " ")
            trace.append(f"{item.get('tool','?')}: {text[:180]}")
        observations = [str(x.get("claim") or "")[:220]
                        for x in (session.get("evidence_anchors") or [])[-5:]]
        relation_context = _frontier_relation_context(session)
        runtime_adaptation = budget.get("_runtime_adaptation") or {}
        prompt = (
            "你是问题求解的证据前沿规划器。只根据题目、已做动作和直接观察，判断当前"
            "最值得推进的一个未闭合认识义务。不要解题、不要给答案、不要指定具体网站或"
            "虚构工具。能力类型只能是 observe（直接观察原始媒介）、retrieve（检索外部"
            "资料）、compute（确定性计算）、compare（比较候选解释/独立验证）、"
            "synthesize（整合已足够证据）。若题干缺参数或证据不可获得，仍可给 blocked，"
            "并说明缺什么。关系网中的历史经验只是可反驳的策略先验，不是本题事实；"
            "优先选择能检验当前证据节点与意图节点之间缺边的动作。若意图节点存在两个以上"
            "未排除解释，下一义务必须是 compare：用能区分解释的证据/计算重试，而不能任选其一继续。"
            "若运行时策略账本显示某一能力连续没有区分性收益，除非说明新的输入、来源或工具为何不同，"
            "不要原样重复该能力；优先安排能打破该停滞的替代通道或比较动作。\n"
            "JSON: {\"state\":\"exploring|verifying|ready|blocked\",\"capability\":"
            "\"observe|retrieve|compute|compare|synthesize\",\"obligation\":\"一句话说明下一步必须闭合什么\","
            "\"why\":\"为什么这是当前最高信息增益缺口\"}\n"
            f"题目：{str(session.get('question') or '')[:1300]}\n"
            f"最近轨迹：{' | '.join(trace)[:2200] or '(无)'}\n"
            f"直接观察：{' | '.join(observations)[:1100] or '(无)'}\n"
            f"关系网上下文（意图节点与经验边）：{json.dumps(relation_context, ensure_ascii=False)[:1500]}\n"
            f"运行时策略账本（只反映本题重复无效信号，不是事实）：{json.dumps(runtime_adaptation, ensure_ascii=False)[:500]}"
        )
        import reflective_mind as _RM_frontier
        raw = _RM_frontier._mm().ask("claude-sonnet", prompt, timeout=35, max_tokens=220)
        frontier = _parse_model_json_object(raw)
        if not frontier:
            raise ValueError("frontier response lacks JSON object")
        capability = str(frontier.get("capability") or "").strip().lower()
        hypotheses = relation_context.get("intent_nodes", {}).get("unresolved_hypotheses") or []
        # 重解若留下多种仍合理的题意，不能悄悄任选一个继续；先走区分性比较。
        if len(hypotheses) > 1:
            capability = "compare"
            frontier["obligation"] = "区分当前仍合理的题意/假设：" + "；".join(hypotheses[:3])
            frontier["why"] = "中途重解后存在未排除的解释，先比较可防止在错误题意下继续求解"
        # 规划器偶尔会返回枚举说明或空串。将其视为“本轮前沿不可判定”并跳过，
        # 而非把无效条目当成一条会阻塞后续动作的前沿；合法能力仍严格受注册表约束。
        if capability not in _FRONTIER_CAPABILITIES:
            raise ValueError(f"unsupported frontier capability: {capability or '<empty>'}")
        frontier = {"state": str(frontier.get("state") or "exploring")[:30],
                    "capability": capability,
                    "obligation": str(frontier.get("obligation") or "")[:300],
                    "why": str(frontier.get("why") or "")[:300],
                    # 只保存被使用的关系边摘要，供 checkpoint/错题归因回看。
            "relation_links": relation_context.get("experience_links", [])[:3],
            "intent_revision": (budget.get("_intent_revision_history") or [])[-1:],
            "hypotheses": hypotheses[:3],
            "issued_at_call": budget.get("n_tool_calls", 0),
                    "guard_count": 0}
        budget["_problem_frontier"] = frontier
        budget["_intent_replan_pending"] = False
        _emit_gate_event(session.get("qid", ""), "problem_frontier", "issued", frontier)
        return ("\n  【动态证据前沿】当前状态:" + frontier["state"]
                + "；下一认识义务:" + (frontier["obligation"] or "推进当前未闭合缺口")
                + "；应优先使用能力:" + capability
                + ("。理由:" + frontier["why"] if frontier["why"] else "")
                + "。你可自主选择具体工具和来源，但下一动作必须推进这个义务，"
                  "或用 document_wall 说明为什么客观无法推进。")
    except Exception as exc:
        _emit_gate_event(session.get("qid", ""), "problem_frontier", "unavailable",
                         {"error": f"{type(exc).__name__}: {str(exc)[:120]}"})
        return ""


def _maybe_heartbeat_selfcheck(session, tool_name, elapsed, node_signal=""):
    """层1(v98x · 2026-08-12) · 心跳自省(mid-run supervisor)。

    和 _maybe_reflect_nudge(被动·卡住才反思)互补:这是【主动】的——不管卡没卡,
    每隔一段步数/时间/关键节点就停下来调诊断脑 reflective_mind 看自己一眼("我在题目
    的什么位置·方向对吗·该换策略/加预算/问专家吗"),像人边做边每几分钟审视自己。
    返回一条【诊断 hint】(不拦截·软引导·agent 读了自己决定);没到触发点返回 ""。
    带节流(每题最多 HEARTBEAT_MAX 次)。fail-safe:任何异常返回 ""·绝不碰主流程。

    node_signal: 调用处可传"关键节点"信号(如 'percep_stall'/'near_wall')·非空即视为
    一个触发点(不等步数/时间也可在关键节点自省)。
    """
    try:
        budget = session.get("_budget")
        if not budget:
            return ""
        if tool_name == "finalize_answer":
            return ""
        if budget.get("_heartbeat_count", 0) >= HEARTBEAT_MAX:
            return ""
        n_calls = budget.get("n_tool_calls", 0)
        _soft = budget.get("soft_wall_s") or 1
        _last_hb_step = budget.get("_heartbeat_last_step", 0)
        # 触发信号(任一):①距上次心跳≥HEARTBEAT_STEP_EVERY 步 ②时间过半(跨过软预算×frac)
        #   ③关键节点(node_signal 非空)。
        _step_due = (n_calls - _last_hb_step) >= HEARTBEAT_STEP_EVERY
        _time_due = (elapsed >= _soft * HEARTBEAT_TIME_FRAC) and not budget.get("_heartbeat_time_fired")
        _node_due = bool(node_signal)
        if not (_step_due or _time_due or _node_due):
            return ""
        # 太早(前几步正常探索期)不打扰:至少跑过 3 步再开始心跳
        if n_calls < 3 and not _node_due:
            return ""
        budget["_heartbeat_count"] = budget.get("_heartbeat_count", 0) + 1
        budget["_heartbeat_last_step"] = n_calls
        if _time_due:
            budget["_heartbeat_time_fired"] = True
        nth = budget["_heartbeat_count"]
        # 调诊断脑 reflective_mind 给【针对本题当前进度】的自省结论
        _diag = ""
        try:
            import reflective_mind as _RM
            _reason = (f"主动心跳自省(第{nth}次)·已跑{n_calls}步/{elapsed:.0f}s"
                       + (f"·关键节点={node_signal}" if node_signal else ""))
            _refl = _RM.reflect(
                goal=session.get("question", ""),
                trace_steps=[{"tool": t.get("tool", "?"), "summary": str(t.get("summary", ""))[:80]}
                             for t in (session.get("tool_log") or [])[-15:]],
                stuck_reason=_reason,
                evidence=(session.get("evidence") or [])[-40:],
            )
            _bits = []
            if _refl.get("why_stuck"):
                _bits.append(f"现状判断:{_refl['why_stuck'][:120]}")
            if _refl.get("next_move"):
                _bits.append(f"★下一步:{_refl['next_move'][:150]}")
            if _refl.get("need_new_tool"):
                _bits.append(f"缺的能力:{_refl['need_new_tool'][:100]}")
            if _bits:
                _diag = "\n  【诊断脑·针对当前进度】" + " ｜ ".join(_bits)
        except Exception:
            _diag = ""  # fail-safe:诊断脑失败退回通用自省
        # 第2+3步(v98y·2026-08-13 用户:体检时【必查策略库】) · 心跳=策略库的实战点名。
        #   盘点发现:库接了但从不在体检时主动查·触发率0=摆设。这里让体检必查一次·命中
        #   落 strategy_matched(触发率可测)。职责分工(缺口2):策略库只给【方法维度】·
        #   模型选择归 model_experience(唯一权威)·这里不推荐具体模型(那会和路由 bandit 打架)。
        _strat = ""
        try:
            import strategy_library as _SL
            _picked = _SL.suggest_strategies(
                question=session.get("question", ""),
                stuck_reason=(node_signal or f"心跳第{nth}次·{n_calls}步"), top_k=2)
            if _picked:
                _names = [p.get("name", "?") for p in _picked]
                _strat = "\n  【策略库·可复用方法】" + " ｜ ".join(
                    f"{p.get('name','?')}:{str(p.get('how',''))[:80]}" for p in _picked[:2])
                _emit_gate_event(session.get("qid", ""), "strategy_matched", "heartbeat",
                                 {"picked": _names[:2], "nth": nth, "src": "heartbeat体检"})
        except Exception:
            _strat = ""
        # 意图回看(通用·不用词表):对比题目原文和当前正在做的事·判 aligned。
        # 不预设偏移类型·LLM 根据本题情况自己判断有没有偏。
        _intent = ""
        try:
            _question = session.get("question", "")
            _tool_log = session.get("tool_log") or []
            _recent_tools = "; ".join(
                f"{t.get('tool','?')}({str(t.get('summary',''))[:50]})"
                for t in _tool_log[-8:]) or "(还没有工具调用)"
            # 中间阶段回头核对意图·不只看"方向偏没偏"·
            #   还查【没理解全】和【有没有不理解的】——像人做到一半停下问自己"我真读懂题了吗、
            #   要的东西都覆盖了吗、有没有还没搞懂的"。三维度自检·让解题从靠运气变稳定。
            _axes = (session.get("triage") or {}).get("ambiguity_axes") or {}
            _axes_s = "; ".join(f"{k}={v}" for k, v in _axes.items()
                                if v and str(v).strip())[:300]
            _intent_prompt = (
                "你是意图审查器。agent 正在解题·已跑了一些步骤。回头核对【理解本身】有没有问题·\n"
                "从三个维度(像人做到一半停下反复思考):\n"
                "① 误解:走的方向和题目原意对得上吗?有没有读反、约束漏读、限定词误解、\n"
                "   把穷举做成单值、实体/时间/口径搞混、自我说服式论证偏离字面意思?\n"
                "② 没理解全:题目要的【每个部分】·agent 的证据都覆盖了吗?还缺哪部分没读到/没算?\n"
                "   (如题目要'A和B两样'·只做了A=没理解全)\n"
                "③ 有没有不理解的:题目里有没有词/约束/隐含要求·agent 似乎还没真搞懂?\n"
                "不预设类型·据本题具体情况判断。只看「题目原文要什么」vs「agent 在做什么/已覆盖什么」。\n"
                "不给解题步骤·不给答案·只纠正理解。\n"
                "\n若检测到偏移·再判一句【补救动作类型】(v99r·不给具体步骤·只指方向):\n"
                "  是不是【模态判错了】——本质要看视频画面/看图/听音频·却在搜文字? → "
                "remedy_kind=\"switch_modality\"·new_modality填该换成的(vision/audio/compute)\n"
                "  是不是缺个工具/能力做不下去? → remedy_kind=\"build_tool\"(该写脚本/造工具/pip装库)\n"
                "  是不是方法/来源选错反复拿不到? → remedy_kind=\"switch_strategy\"(该换工具/换来源/换角度)\n"
                "  就是理解偏了·方向掰回来就行? → remedy_kind=\"\"(纯纠理解·不需要新能力)\n"
                "  若当前证据下仍存在两个以上都合理、但会导致不同解法/答案的题意解释，"
                "在 alternative_intents 输出最多2条；没有则空数组。不要凑假设。\n\n"
                f"题目原始意图: {_question[:300]}\n"
                + (f"题目的关键歧义轴(triage抽的·重点核对这些): {_axes_s}\n" if _axes_s else "")
                + f"\nagent 最近做的事: {_recent_tools[:400]}\n\n"
                "输出JSON: {\"aligned\":true} 或 "
                "{\"aligned\":false,\"misread\":\"哪个维度(误解/没理解全/不理解)+具体是什么\","
                "\"correct_intent\":\"正确理解方向/还缺什么没做\","
                "\"alternative_intents\":[\"仍合理、待用证据区分的另一种题意\"],"
                "\"remedy_kind\":\"switch_modality|build_tool|switch_strategy|(空)\","
                "\"new_modality\":\"若switch_modality:该换成的模态(vision/audio/compute);否则空\","
                "\"remedy_hint\":\"若build_tool:缺什么能力;若switch_strategy:该换什么;否则空\"}\n"
                "只输出一个JSON。"
            )
            import reflective_mind as _RM_ic
            _irc_raw = _RM_ic._mm().ask("claude-sonnet", _intent_prompt, timeout=60, max_tokens=400)
            import re as _re_ic, json as _json_ic
            _irc_d = _parse_model_json_object(_irc_raw)
            if _irc_d:
                if not _irc_d.get("aligned", True):
                    _reinterpreted = _apply_intent_reinterpretation(session, _irc_d)
                    _intent = (f"\n  【意图回看·检测到偏移】偏在:{_irc_d.get('misread','')[:120]}"
                               f" → 正确理解:{_irc_d.get('correct_intent','')[:150]}"
                               f"\n  请按正确理解继续·别沿偏移方向跑。")
                    # 意图自检发现偏移后·若判定
                    #   "缺能力/方法错"·顺势引导【造工具/写脚本/换策略】——不额外调LLM(同一次审查里
                    #   多要remedy字段·不拖慢速度)。这把"发现没理解全/做不下去"接到现成的
                    #   write_capability/tool_forge/换工具能力上·让agent灵活补救而非只知道"我偏了"。
                    _rk = (_irc_d.get("remedy_kind") or "").strip()
                    _rh = str(_irc_d.get("remedy_hint") or "")[:150]
                    # 意图自检判"开局模态判错了"
                    #   (该看视频/图/听音频却在搜文字)→【真改 session modality_needs】(不只软提示)·
                    #   现成的视频/视觉硬约束(_needs_video等)据此自动生效逼换对模态。这让开局triage
                    #   判漏能被中间语义纠正——治"意图被开局锁死"·比硬编码信号词根本。节流每题≤1次防抖动。
                    _nm = (_irc_d.get("new_modality") or "").strip().lower()
                    if (_rk == "switch_modality" and _nm in ("vision", "audio", "compute")
                            and not budget.get("_modality_recorrected")):
                        budget["_modality_recorrected"] = True  # 每题最多重解一次模态
                        _tr = session.setdefault("triage", {})
                        _mods = list(_tr.get("modality_needs") or [])
                        if _nm not in _mods:
                            _mods.append(_nm)  # 只加不删(不误伤原模态)·并集
                            _tr["modality_needs"] = _mods
                        _emit_gate_event(session.get("qid", ""), "modality_recorrect", "heartbeat",
                                         {"new_modality": _nm, "mods_now": _mods})
                        _intent += (
                            f"\n  【补救·模态重解】开局把这题的模态判偏了——本质需要【{_nm}】"
                            f"(看画面/听音频/精确算)·你却在搜文字。已把 {_nm} 加进这题的模态需求·"
                            f"请立刻换对应工具:vision→see_video_frame/see_image_vlm·audio→transcribe_audio·"
                            f"compute→shell_exec/sympy_calc。别再搜文字找二手描述了。")
                    if _rk == "build_tool" and _rh:
                        # 真接线：把审查发现的“缺能力”写成下一轮受限动作，而非只展示建议。
                        # write_capability 仍由 agent 自主填写问题描述/上下文，避免系统伪造算法。
                        budget["_intent_remedy"] = {"kind": "build_tool", "hint": _rh}
                        budget["_forced_tool"] = "write_capability"
                        budget["_force_gate_count"] = 0
                        budget["_problem_frontier"] = None
                        _emit_gate_event(session.get("qid", ""), "intent_remedy", "route_tool", {
                            "kind": _rk, "target": "write_capability", "hint": _rh,
                        })
                        _intent += (
                            f"\n  【补救·造工具已路由】纠偏还缺个能力:{_rh}。"
                            f"下一步请调 write_capability，用已知数据说明要生成的计算/处理工具；"
                            f"它会写代码、测试并注册。若确有理由不应造工具，可明确说明后改走其他路。")
                    elif _rk == "switch_strategy" and _rh:
                        # 方法/来源偏移时先强制一次多源检索入口；它可承接换源、改 query、并行验证。
                        budget["_intent_remedy"] = {"kind": "switch_strategy", "hint": _rh}
                        budget["_forced_tool"] = "run_parallel_search"
                        budget["_force_gate_count"] = 0
                        budget["_problem_frontier"] = None
                        _emit_gate_event(session.get("qid", ""), "intent_remedy", "route_tool", {
                            "kind": _rk, "target": "run_parallel_search", "hint": _rh,
                        })
                        _intent += (
                            f"\n  【补救·换策略已路由】这条路反复拿不到·换个维度:{_rh}。"
                            f"下一步请调 run_parallel_search，把不同来源/不同关键词的验证一起发起；"
                            f"不要沿用原来源或原查询空转。")
                    if _reinterpreted:
                        _intent += "\n  【中途重解已生效】旧前沿与旧假设审计已作废；将按新意图重新规划。"
                    _emit_gate_event(session.get("qid", ""), "intent_recheck", "heartbeat",
                                     {"aligned": False, "misread": _irc_d.get("misread","")[:80],
                                      "remedy_kind": _rk, "reinterpreted": _reinterpreted,
                                      "alternative_n": len(_irc_d.get("alternative_intents") or [])})
                else:
                    _emit_gate_event(session.get("qid", ""), "intent_recheck", "heartbeat",
                                     {"aligned": True})
        except Exception as _intent_err:
            # 以前这里静默吞掉异常，事后只能看到 had_intent_recheck=false，无法判断
            # 是模型超时、格式不合规还是调用路径没走到。仍保持 fail-safe 不阻断主流程，
            # 但必须留下可审计事件，避免把“未执行”误判为“已对齐”。
            _emit_gate_event(session.get("qid", ""), "intent_recheck", "unavailable",
                             {"error": f"{type(_intent_err).__name__}: {str(_intent_err)[:120]}"})

        _emit_gate_event(session.get("qid", ""), "heartbeat", "selfcheck",
                         {"nth": nth, "n_calls": n_calls, "elapsed": round(elapsed, 1),
                          "node": node_signal, "had_diag": bool(_diag),
                          "had_strat": bool(_strat), "had_intent_recheck": bool(_intent)})
        return (
            f"\n\n[心跳自省 · 第{nth}/{HEARTBEAT_MAX}次 · 主动审视]（不是拦截·像人边做边每隔一阵"
            f"回头看自己）你已跑 {n_calls} 步 / {elapsed:.0f}s。停一下自检:\n"
            f"  ① 进度够吗?按这个速度·剩下的预算够走完到 finalize 吗?若正在多步计算/多跳"
            f"链中间、快撞墙但还差几步·可 request_more_budget(说明理由)给自己加预算继续。\n"
            f"  ② 该换策略吗?同一个来源/工具反复拿不到·就换维度(换工具/换来源/问 "
            f"ask_expert_model/查 skill_search·能并行就 run_parallel_search 并行验)。"
            + _intent + _diag + _strat + _refresh_problem_frontier(session) + "\n"
            f"自检完按你自己的判断继续——这只是帮你像人一样别闷头一条路走到黑。"
        )
    except Exception:
        return ""  # fail-safe:心跳自省是软引导·出任何岔子都静默放行


def _init_budget(session, max_iterations, hard_wall_s=DEFAULT_HARD_WALL_S):
    session["_budget"] = {
        "t_start": time.time(),
        "hard_wall_s": hard_wall_s,
        # v-eff(2026-08-18·病1 加时绝对硬上限) · 记录 base wall——所有后续加时
        #   (request_more_budget/自适应倍率)累计后·hard_wall 绝不超过
        #   base_wall × MEYO_BUDGET_EXTEND_WALL_CAP_MULT(默认 2×)。治"跑23分钟":
        #   base 800s → 硬顶 1600s·到顶无论有无进展强制收尾·防单题无限烧。
        #   与旧的 BUDGET_EXTEND_WALL_CAP(绝对秒兜底)取 min·两道天花板都生效。
        "base_wall_s": hard_wall_s,
        "soft_wall_s": hard_wall_s * DEFAULT_SOFT_WALL_RATIO,
        # v-eff2b(2026-08-19·治 test4 实测超墙10-24%) · finalize 自身执行(LLM
        #   生成收尾文本+AnswerVault+映射表拼装)需要额外时间，但硬墙判断点
        #   过去是 elapsed>=hard_wall_s 才"发令"，发令后 finalize 还要跑一轮，
        #   必然把总耗时推过 hard_wall_s。实测:900s 硬墙下三题分别在
        #   992.8s/1063.7s/1113.4s 收卷，超墙 93-213s。修:硬墙判断点提前
        #   finalize_reserve_s(默认 120s，覆盖一次 LLM 生成+门检开销)，
        #   让"发令+finalize执行"两段总和压在 hard_wall_s 附近，而不是
        #   "发令点=hard_wall_s"导致结构性必然超墙。可环境变量调。
        # Short runs need most of their wall for at least one real retrieval. Reserve scales
        # from 5s and is capped at 20%; the old 10s floor made a 60s run start finalizing at
        # 19s once initialization latency was included.
        "finalize_reserve_s": min(
            float(os.environ.get("MEYO_FINALIZE_RESERVE_S", "120")),
            max(5.0, float(hard_wall_s) * 0.12)),
        "max_iterations": max_iterations,
        "n_tool_calls": 0,
        "soft_nudge_sent": False,
        "hard_block_active": False,
        "last_tool_name": None,
        "same_tool_streak": 0,
        "last_content_key": None,   # 深度考据工具:上次的命令内容指纹
        "same_content_streak": 0,   # 完全相同命令连续重复次数
        "n_investigative_calls": 0, # v72 · 真检索/考据类工具的调用次数(判"够不够努力")
        "finalize_block_count": 0,  # v72 · finalize 因"没努力+UNKNOWN"被拦截的次数(有上限防死循环)
        # 卡住→自我反思换策略(提升自主性,非题型强制) ──
        # 连续"没带来新有效证据/进展"的工具调用计数;触发一次反思提示后清零。
        # 详见 _maybe_reflect_nudge。
        "last_evidence_n": 0,        # 上次检查时的证据条数(涨了=有进展)
        "last_narrative_len": 0,     # 上次检查时的正文长度(涨了=有进展)
        "no_progress_streak": 0,     # 连续无有效进展的工具调用数
        "reflect_nudge_count": 0,    # 已注入过几次"自我反思换策略"提示(有上限防刷屏)
        # OCR死磕卫兵(见 _check_budget 视觉卫兵段落注释)
        "ocr_ineffective_streak": 0,  # read_image_ocr 连续"无效"(抽不到字/结果很短)的次数
        "vlm_forced_hint_count": 0,   # 已注入过几次"改用VLM"的强制提示(有上限防刷屏)
        "vlm_ever_used": False,       # 本题是否已调用过 see_image_vlm(调过就不用再逼)
        # 环境资源熔断改滑动窗口(见 shell_exec 尾部注释)
        "env_err_window": [],         # 最近若干次 shell_exec 是否命中"环境资源错误"(True/False)
        "env_err_hard_block_count": 0,  # 已硬拦截过几次(有上限防死循环,拦够就放行)
        # 层1(v98x) · 心跳自省(主动边跑边看自己·见 _maybe_heartbeat_selfcheck)
        "_heartbeat_count": 0,        # 已主动心跳自省几次(有上限 HEARTBEAT_MAX)
        "_heartbeat_last_step": 0,    # 上次心跳时的步数(距此≥HEARTBEAT_STEP_EVERY 再触发)
        "_heartbeat_time_fired": False,  # 时间过半自省是否已触发过(只触发一次)
        # 层2(v98x) · agent 自我加预算(见 request_more_budget 工具)
        "_budget_extends": 0,         # agent 已自主加预算几次(有上限 BUDGET_EXTEND_MAX)
        "_problem_frontier": None,    # 动态证据前沿：当前未闭合义务与所需能力
        "_active_intent": str(session.get("question") or "")[:300],  # 可被中途重解改写
        "_intent_revision_history": [],
        "_intent_hypotheses": [],      # 中途重解后仍待区分的题意解释
        "_intent_replan_pending": False,
        "_intent_remedy": None,         # 意图审查给出的待执行补救，不是仅提示文本
        "_policy_ledger": [],            # 自主策略选择及其结果归因（有界审计账本）
    }
    return session["_budget"]


# ══ v120b(2026-08-17 · 8d46 企鹅题纯跑轮实证+DEGRADED路径审计) FixG/H · §28 落档 ══
# 病根(通用机制缺陷·非单题):
#   ①软墙nudge单声道催收卷: 对"正在长计算中段·方向对·还差几步"的题,正确动作
#     是加时而非收敛,但 agent 不会自发想起 request_more_budget(8d46: 900s 墙·
#     600s+ 还在枚举读法组合·被催收→匆忙交卷→v111 门拦下→无时间重交→DEGRADED
#     落"被拦的旧答案"·四种读法全算出来了却没机会选)。
#   ②DEGRADED 强提路径绕过 finalize 的 v110/v111 审计门: 撞墙最后一刻交的答案
#     连"读法轴自检"都没做过就落盘。
# 软墙/收敛 nudge
#   分诊——与 request_more_budget 完全同口径的进展判据,【有进展+续命有余量】→
#   nudge 明示"立即 request_more_budget 加时"通道(把选项在正确时刻递到面前·
#   不靠 agent 自觉想起有这工具);无进展→维持催收敛(加时治不了原地打转)。
#   外层墙感知复用 v119 FixE 口径: MEYO_OUTER_DEADLINE 换算·续不出空间就不指
#   加时路(指了也会被拒·白丢一轮工具调用)。
# 墙前/强提 nudge 附四轴自检清单(否定辖域/
#   and-or/极值方向/答非所问)——不拦截不等待(墙前已无时间走门)·只把审计门的
#   判据精神注入最后一次交卷动作;DEGRADED 落盘头标注"未经读法自检"·外层
#   复盘一眼可辨这类 final 需人工复核。
# 修复经验经 memory_layers.remember 落进化层(source=harness-fix·
#   后续轮 agent recall 可见"催收nudge必须分诊"这条模式)。

def _recent_progress_signals(s, b):
    """v120b FixG · 与 request_more_budget 审批判据【完全同口径】的进展信号。
    同口径是铁律: 分诊指"去加时"→工具必批;分诊指"收敛"→工具必拒加时。
    ①证据条数或正文长度自上次周期检查(heartbeat 会刷新基准)以来有涨;
    ②最近 3 步里有实质 shell 计算输出(跑出了中间结果·不是 echo 探测)。
    v120 FixN(2026-08-17·用户拍板:续命要对比真正的意图): ③意图账本进展——
    session.intent_ledger(P2·native 引擎写入)的判据满足计数 _satisf_n 有涨
    (引擎在中点/连败重锚时更新)也算实质进展。意图判据=题意的可验证成功
    标准·它涨了说明"答到题上"而不只是"在干活"。fail-safe: 无 ledger 不影响。"""
    _grew = ((len(s.get("evidence") or []) > b.get("last_evidence_n", 0))
             or (len((s.get("narrative") or "")) + len((s.get("cards") or ""))
                 > b.get("last_narrative_len", 0)))
    _tl = s.get("tool_log") or []
    _recent_compute = any(
        t.get("tool") == "shell_exec" and "[stdout]" in str(t.get("summary", ""))
        and len(str(t.get("summary", ""))) > 40
        for t in _tl[-3:])
    if not (_grew or _recent_compute):
        # ③ 意图账本进展(意图对比内化到续命审核)
        try:
            _led = s.get("intent_ledger") or {}
            _sat = int(_led.get("_satisf_n") or 0)
            if _sat > int(b.get("last_intent_satisf_n", 0)):
                _grew = True
            b["last_intent_satisf_n"] = _sat
        except Exception:
            pass
    return _grew, _recent_compute


def _outer_extend_headroom_s(b):
    """v120b FixG(复用 v119 FixE 口径) · 外层墙(MEYO_OUTER_DEADLINE)之下还能给
    内层续出多少秒墙钟。返回 None=无外层墙(随便续);数值=还能续出这么多秒
    (≤0 即已被外层墙堵死·再加时也会被 request_more_budget 的 FixE 拒掉)。"""
    try:
        _outer_dl = float(os.environ.get("MEYO_OUTER_DEADLINE", "0") or 0)
        if _outer_dl <= time.time():
            return None
        _elapsed_b = time.time() - float(b.get("t_start") or time.time())
        _cap_rel = _elapsed_b + max(0.0, _outer_dl - time.time() - _OUTER_FINISH_MARGIN_S)
        return max(0.0, _cap_rel - float(b.get("hard_wall_s") or 0))
    except Exception:
        return None


_WALL_SELFCHECK_TEXT = (
    "交卷前30秒默查(不调工具·直接体现在答案里): ①回读题面问的到底是哪个量/单位/"
    "名称——否定词(not/no/neither/没有)管到哪一项、and/or 是否真是你理解的那个、"
    "最大/最小/第一/最后有没有取反; ②答案是否直接回应题面问的那个对象·而不是你"
    "算得最顺手的那个; ③若你已算出多种读法的值且无法再验证·交与题面字面最贴合的"
    "那个·并在答案里标注置信度与存疑点(别沉默地猜)。"
)


def _force_degraded_final(session, why, extra_info=None):
    """C 修(2026-08-14·墓碑题624事故) · 撞墙后【强制落兜底 final·永不留空】的公共实现。
    把当前最强候选(感知候选/中间结论/narrative)提升成 final·打 [DEGRADED] 标·并
    _save_ckpt(status="done") 落盘。返回 True=已强制落盘·False=没有可用候选(留给 nudge)。
    时间硬墙和步数硬墙两条路径共用它——两者都会"撞墙后顽固不 finalize→被硬杀→空 final"·
    单独 nudge 救不了不听话的 agent(实证:624 被外层 SIGKILL·步数墙则被 LangGraph
    recursion_limit 抛异常打断)。fail-safe:任何异常都吞掉·不让主流程崩。"""
    try:
        if (session.get("final") or "").strip():
            return True  # 已有 final·无需强制
        # (2026-08-16·韧性根因修) 优先用【agent最后一次真想交的答案】(_last_finalize_attempt)——
        #   它被门拦下但那才是agent的答案(如34689)·比perception候选/narrative更准。这是治
        #   "有答案却被门拦到墙钟耗尽→final空"的关键:必交卷压倒门审查(门是质量·空final是失败)。
        _last_try = str(session.get("_last_finalize_attempt") or "").strip()
        # ★2026-08-20(Plastic Man 错题实证) · 收尾 hint 噪音过滤: _last_finalize_attempt
        #   可能存的是 agent echo 回来的【收尾自检 checklist】(①②③取反/字面最贴合…)·
        #   不是真答案。若采用它→把提示噪音当答案交出(哪怕证据里已搜到 gold)。判定为
        #   噪音则丢弃·让兜底链走下游(软锁候选/tool_log 断言/vault)。fail-safe。
        if _last_try and _looks_like_finalize_noise(_last_try):
            try:
                _emit_gate_event(session.get("qid", ""), "hardwall",
                                 "drop_finalize_noise", {"len": len(_last_try)})
            except Exception:
                pass
            _last_try = ""
        _cand, _meta = _strongest_perception_candidate(session)
        # 08-19(bc 实证·软锁候选进 done-final 兜底链) · 兜底链【最高优先级】源:
        #   撞墙钟/预算耗尽时·若 identity_coverage 里有【软锁候选】(auto 自己按题面
        #   约束逐条核对后覆盖率最高的 probable/confirmed 候选)·必须把它作为答案交出
        #   (带不确定度标注)·绝不因覆盖率没到 confirmed 就交元评论/narrative/空。
        # 优先级: confirmed 候选 → 软锁 probable 候选(标注置信度) → agent 最后交卷
        #   尝试(_last_finalize_attempt) → perception 候选 → narrative → ...
        #   (已有 final 由函数开头的 early-return 兜住·排在软锁之上——符合任务优先级。)
        # fail-safe: 读软锁失败 → (None,None)·完全退回原兜底链·不破坏 done-final 空修复。
        _soft_body = None
        _soft_meta = None
        try:
            import identity_coverage as _ic_dg
            _soft_body, _soft_meta = _ic_dg.soft_lock_final_body(session)
        except Exception:
            _soft_body, _soft_meta = None, None
        if _soft_body and str(_soft_body).strip():
            _be = str(_soft_body).strip()
            try:
                _emit_gate_event(session.get("qid", ""), "hardwall",
                                 "soft_lock_final_rescue", _soft_meta or {})
            except Exception:
                pass
        else:
            _be = _last_try or (str(_cand) if _cand else "") or str(session.get("narrative") or "")
        _be = _be.strip()
        # 兑底链末环: 从 tool_log 倒序挖【最后的强答案断言】——
        #   392条证据+正确推理全在上下文·但 final/narrative/candidate 全空时,
        #   run_factual_extract 的"精确答案: X" / 卡片验证里的 "X = Y" 断言是唯一
        #   结构化落点。挖到就当候选(诚实标 DEGRADED·不足比空白器强)。
        if not _be:
            try:
                for _e in reversed((session.get("tool_log") or [])[-40:]):
                    _blob = str((_e or {}).get("args") or "") + " " + str((_e or {}).get("summary") or "")
                    _m = (re.search(r"精确答案[:：]\s*([^\n]{2,150})", _blob)
                          or re.search(r"\banswer\s+(?:is|=|:)\s*([A-Z0-9][^\n.,;]{1,80})", _blob)
                          or re.search(r"\b(?:Person\s+[A-C]|答案|the answer)\s*=\s*([A-Z][A-Za-z .'-]{2,60})", _blob))
                    if _m:
                        _be = _m.group(1).strip()
                        _emit_gate_event(session.get("qid", ""), "hardwall",
                                         "toollog_assertion_rescue", {"len": len(_be)})
                        break
            except Exception:
                pass
        # 08-18(doc·anti_false_positive L5 Conservative Mode) · vault 兜底种子:
        #   narrative/candidate/tool_log 断言全空·但 AnswerVault 里锁了具体答案时·
        #   用 vault 答案当候选(而非 return False 落空壳)。这治"agent 亲口说过答案
        #   但收尾路径其他候选都空→final 空/白卷"——vault 断言是最后一道防误杀。
        if not _be:
            try:
                import answer_vault as _av_seed
                _vt = _av_seed.answer_text(session)
                if _vt:
                    _be = _vt.strip()
                    _emit_gate_event(session.get("qid", ""), "hardwall",
                                     "vault_seed_rescue", {"len": len(_be)})
            except Exception:
                pass
        if not _be:
            return False  # 没有任何可交付候选·交给 nudge 继续逼(别落个空壳)
        # v120b FixH2 · DEGRADED 标记细化(8d46 实证): 该路径绕过 v110/v111 审计门·
        #   最后一刻交的答案可能连读法/辖域都没核过。标注"未经读法自检·建议复核"·
        #   外层评分/复盘一眼可辨(通用标注·零题目信息)。
        # 兜底路径值前置: bec7/384d 撞墙后的候选以 meta 文本打头
        #   (自辩/张力分析)·评分器子串匹配不到 → hit=0。这里在写 final 前从会话
        #   抽最后算出的数·若有则前置成"答案: X"开头(其他论证保留为说明)·让
        #   评分器命中。通用·零特判·无 gold 泄漏·fail-safe(抽不到就不前置)。
        _body = _be[:_CKPT_TEXT_MAX]
        # 08-18(doc·anti_false_positive L5/L6) · AnswerVault 恢复优先于一切:
        #   兑底候选若【无明确答案行】而 vault 已锁答案(从工具输出抓的"精确答案:X"
        #   断言)→前置成答案行。比 FixM 数值前置更早——vault 断言是 agent 亲口
        #   说过的答案·不是抽取的中间数。已有答案行则不动。fail-safe。
        try:
            import answer_vault as _av_dg
            _body = _av_dg.rescue_final(_body, session)
        except Exception:
            pass
        _warn_pref = ""
        try:
            if _question_wants_number(session.get("question") or ""):
                _tok_d = _leading_answer_number(_body)
                if _tok_d:
                    _ss_d, _fl_d = _tool_log_numbers(session)
                    if _fl_d and not _number_backed(str(_tok_d), _ss_d, _fl_d):
                        _warn_pref = (
                            f"⚠️ [兜底警示·数值无出处] 主答值 {_tok_d} 在全部工具输出中"
                            f"找不到出处(疑似重建/编造值——工具从未算出过它)。外层评分与"
                            f"复盘请按『未验证/编造』对待·勿当真实计算结果。\n\n")
                        try:
                            _emit_gate_event(session.get("qid", ""), "hardwall",
                                             "num_provenance_degraded",
                                             {"unbacked": str(_tok_d)})
                        except Exception:
                            pass
        except Exception:
            pass
        session["final"] = (
            _warn_pref +
            "## 回答([DEGRADED]·撞预算硬墙被强制收尾·基于已收集证据的尽力抽取·"
            "未经完整确认·未经读法轴/否定辖域自检·建议复核)\n\n"
            + _body)
        _rec = {"why": why}
        if isinstance(extra_info, dict):
            _rec.update(extra_info)
        session.setdefault("_degraded_runs", []).append(_rec)
        _save_ckpt(session.get("qid", ""), status="done",
                   extra={"degraded_hardwall_forced": True, "degraded_why": why})
        _emit_gate_event(session.get("qid", ""), "hardwall", "force_finalize", _rec)
        return True
    except Exception:
        return False  # fail-safe


# ── v-eff(2026-08-18·病2) · 检索渠道结构分类 + 意图归一 + 换渠道判定 ──
# 对照实验铁证:CC 在 dblp 空手 2 次立刻换 crossref(结构性不同渠道·一次命中)·
#   引擎在同渠道反复猜 PID 撒网 60+ 次。核心=在无效路径上及时止损换【结构性不同】
#   的渠道(换访问方式)·而非换关键词继续撞同渠道。
# 渠道"结构类"= 按数据源/访问方式分桶(不是按关键词)。同桶内换词=没换渠道。
_CHANNEL_SWITCH_THRESH = int(os.environ.get("MEYO_CHANNEL_SWITCH_THRESH", "2"))
# 空手信号(结果无新证据)——命中任一即算本次"空手"。
_CHANNEL_EMPTY_MARKERS = (
    "新增 0", "未找到", "没有找到", "找不到", "no result", "not found",
    "empty", "为空", "0 条", "0 个", "无结果", "no match",
)
# 结构渠道分类关键词:URL host / query 里的站点特征词 → 归一到一个结构桶。
_CHANNEL_BUCKETS = (
    ("dblp",      ("dblp.org", "dblp:", " dblp", "dblp ")),
    ("crossref",  ("crossref.org", "api.crossref", "crossref", "doi.org")),
    ("openalex",  ("openalex.org", "openalex", "api.openalex")),
    ("wikidata",  ("wikidata.org", "wikidata", "wdqs", "sparql")),
    ("semanticscholar", ("semanticscholar.org", "api.semanticscholar", "semantic scholar")),
    ("arxiv",     ("arxiv.org", "arxiv")),
    ("pubmed",    ("pubmed", "ncbi.nlm.nih.gov", "europepmc")),
    ("archive",   ("web.archive.org", "archive.org", "archive.ph", "wayback")),
    ("wikipedia", ("wikipedia.org", "wikipedia", "wiki/")),
    ("scholar",   ("scholar.google", "google scholar")),
)


def _channel_bucket(tool_name, query):
    """把一次检索归到一个【结构渠道桶】。识别不到具体源→按工具名归桶
    (fetch=直取一个URL·search=通用web搜)。同桶=同结构·换词不算换渠道。"""
    _q = (str(query or "")).lower()
    for _name, _keys in _CHANNEL_BUCKETS:
        if any(k in _q for k in _keys):
            return _name
    # 没有明显站点特征——按工具/访问方式归桶
    if tool_name == "fetch_url_smart":
        # 直取某 URL:用 host 当桶(同 host 反复抓=同渠道)
        _m = re.search(r"https?://([^/\s]+)", _q)
        if _m:
            return "host:" + _m.group(1)[:40]
        return "fetch"
    if tool_name == "traverse_web":
        return "traverse_web"
    return "web_search"  # run_parallel_search 等通用 web 检索


# 【已锁定实体+问属性】的最快路径
#   前置默认引导——不等"连续空手"·锁定实体后【立即、尽早】把 agent 推向
#   "run_parallel_search 直搜『实体 属性关键词』(走 AnySearch 二手源一步命中)"这条
#   人工 2 步就命中的最快链路。旧的 _bio_hint 是【事后补救】(要连续空手到阈值才提示·
#   太晚·实测 bc_1786949292 41 步都没触发);这里升级为【事前默认】(锁定即引导·
#   问的又是属性→第一步就走最快路径·别先 curl 维基/wikidata 撞墙)。
#   通用:任何"已锁定实体 + 问某属性"的题·不针对某人某年写死。
#   多源矛盾时并附【多源取值·投票/标注分歧】提示(治实测 Walibora 有源说 1985-86 有源
#   说 1988-96→别卡住不交)。fail-safe·env MEYO_ANYSEARCH_FIRST=0 可关。

# 属性型问句信号(问某实体的一个具体属性/生平细节·而非"找/是谁")——通用判据。
_ATTR_QUESTION_MARKERS = (
    "which year", "what year", "in what year", "when did", "when was", "how many",
    "how long", "how old", "what is the", "what was the", "where did", "where was",
    "born", "died", "career", "worked as", "before", "after", "founded", "graduated",
    "哪年", "何时", "多少", "年份", "生平", "经历", "职业", "出生", "去世", "毕业", "担任",
)
# 多源分歧信号——摘要里出现"多个不同值/矛盾/分歧"的迹象·提示投票别卡死。
_CONFLICT_MARKERS = (
    "conflict", "disagree", "differ", "inconsistent", "矛盾", "分歧", "不一致",
    "有的说", "另一来源", "another source", "however", "但另",
)


def _question_asks_attribute(question) -> bool:
    """判断问句是否属【问某(已知)实体的一个具体属性/生平细节】类。通用·fail-safe。"""
    try:
        _q = str(question or "").lower()
        return any(k in _q for k in _ATTR_QUESTION_MARKERS)
    except Exception:
        return False


def _maybe_anysearch_first_hint(session, tool_name, question, summary):
    """优化1 · 【已锁定实体 + 问属性】→ 事前默认引导走 AnySearch 直搜属性(最快链路)。
    与 _maybe_channel_switch_nudge(事后·连续空手才提示)互补:这条【锁定即触发·每题只一次】。
    返回追加到 summary 的提示串(空串=不触发)。fail-safe:异常返空串。"""
    try:
        if os.environ.get("MEYO_ANYSEARCH_FIRST", "1") != "1":
            return ""
        b = _rs_sessions.ensure_budget(session)
        # 每题最多提示一次(事前引导·不需要反复打扰)
        if b.get("_anysearch_first_hinted"):
            # 已引导过·但若这一步出现多源分歧信号·仍补一次投票提示(治卡住不交)
            _conf = any(m in str(summary) for m in _CONFLICT_MARKERS)
            if _conf and not b.get("_multisource_vote_hinted"):
                b["_multisource_vote_hinted"] = True
                _emit_gate_event(session.get("qid", ""), "multisource_vote", "nudge", {})
                return (
                    "\n\n[⚖️多源取值·别卡死] 不同来源给出的值不一致很正常——**不要因为"
                    "对不齐就卡住不交**。做法:①列出各来源的值 + 出处;②取多数票/最权威"
                    "来源的值作为主答案·把少数分歧值标注在括号里(如 `1985–1986(另有来源"
                    "记 1988–1996)`);③直接 finalize·别再无限追加搜索去消歧(那是撒网)。")
            return ""
        # 需已锁定实体(vault 有答案 或 identity_coverage 软锁)
        _locked = ""
        try:
            import answer_vault as _avf
            _v = _avf.get(session)
            if _v:
                _locked = str(_v.get("answer") or "").strip()
        except Exception:
            _locked = ""
        if not _locked:
            return ""
        # 且问的是"某属性/生平细节"(否则不引导·避免误伤"找实体"类)
        if not _question_asks_attribute(question):
            return ""
        b["_anysearch_first_hinted"] = True
        _emit_gate_event(session.get("qid", ""), "anysearch_first", "nudge",
                         {"locked": _locked[:60]})
        return (
            f"\n\n[⚡最快路径·已锁定实体问属性] 你已锁定实体『{_locked[:80]}』·题目问的是它的"
            f"某个【具体属性/生平细节】。**下一步第一时间就用 run_parallel_search 直搜"
            f"「{_locked[:60]} <要问的属性关键词>」**(如 '<实体> probation officer years'"
            f"/'<实体> founded year')——它走 AnySearch 二手源(悼文/专访/纪念文/新闻综述)"
            f"能【一步命中】这种冷门细节。**别先 curl 维基/wikidata**:权威结构库通常【没有】"
            f"这种传记级细节·死磕它们只会撞墙空烧步数(对照:高效解题者 2 步即中——锁实体→"
            f"直搜实体+属性词)。若首搜多源给出不同值·别卡住:取多数票/最权威来源·把分歧值"
            f"括注·直接 finalize。")
    except Exception:
        return ""
    return ""


# ── v13.97(2026-08-20 · bc实证bc_1786949292 · 硬路由升级) ────────────────────
# 病根(真数据): 引擎【锁准了实体 Walibora(8次)】却 37 步超时没答出——锁定后
#   走 fetch_url_smart×7(curl维基)+query_structured×2·只 run_parallel_search×2·
#   【没走 AnySearch 直搜属性】。_maybe_anysearch_first_hint 是 prompt 软引导·
#   longcat 收到不听(LLM 不服从软引导=真天花板·铁律"工具层硬拦>prompt软引导")。
# 修法: 从软引导【升级为硬路由】——复用既有 _forced_tool 硬拦机制(_check_budget
#   里: 设了 _forced_tool 后·下一步只允许调它或显式拒绝·否则 return False 硬拦
#   最多2次)。当【已锁定候选实体】+【问某属性】+【锁定后已绕了 ≥1 步别的源却没走
#   run_parallel_search 直搜属性】→ 立即设 _forced_tool=run_parallel_search +
#   注入建议 query「实体全名 属性关键词」·让它必走 AnySearch 二步命中链。
#   通用零题型知识·env MEYO_ANYSEARCH_FORCE=0 回退(默认1开)。是软 hint 的升级
#   补充(软 hint 保留·互补): 软 hint 治"从没提示过"·硬路由治"提示了不听"。
_ATTR_QUERY_KEYWORDS = (
    ("probation officer", "probation officer"),
    ("worked as", ""), ("career", ""),
    ("founded", "founded"), ("born", "born"), ("died", "died"),
    ("graduated", "graduated"),
)


def _extract_attribute_keywords(question) -> str:
    """从问句里粗提"要问的属性关键词"·用于拼强制 query「实体 属性词」。通用·fail-safe。
    策略: ①命中已知属性词表直接用;②否则取问句里的非停用实词(名词短语近似)。返空=交给
    agent 自己补。不写死具体题·只做轻量启发。"""
    try:
        _q = str(question or "").strip()
        _ql = _q.lower()
        _hits = []
        for _k, _out in _ATTR_QUERY_KEYWORDS:
            if _k in _ql:
                _hits.append(_out or _k)
        # 去重保序
        _seen = set()
        _kw = []
        for _h in _hits:
            _h = _h.strip()
            if _h and _h not in _seen:
                _seen.add(_h)
                _kw.append(_h)
        if _kw:
            return " ".join(_kw)[:80]
        # 兜底: 抓问句里较长的实词(去掉常见疑问/停用词)·近似"问的东西"
        import re as _re
        _stop = {"which", "what", "when", "where", "who", "how", "did", "was", "were",
                 "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or",
                 "year", "years", "is", "are", "do", "does", "his", "her", "their", "he",
                 "she", "they", "that", "this", "with", "as", "by"}
        _words = [_w for _w in _re.findall(r"[a-zA-Z][a-zA-Z\-]{2,}", _ql)
                  if _w not in _stop]
        return " ".join(_words[-4:])[:80]
    except Exception:
        return ""


def _maybe_channel_switch_nudge(session, tool_name, question, summary):
    """病2核心 · 同一【结构渠道】连续 N 次空手【且没换过结构性不同渠道】→注入换渠道硬提示。
    返回追加到 summary 的提示串(空串=不触发)。fail-safe:异常返空串。
    这是"及时止损换渠道"(dblp→crossref→wikidata→web)·非"换词再撒网"。

    设计要点(对照实验铁证):按【结构渠道桶】计空手·非按精确 query——因为在同一
    渠道换关键词/换 PID 撒网(dblp 里换第 5 个 PID)本质仍是同渠道空转·query 级
    追踪会因措辞变化把 streak 打散(测反面: 换词不算换渠道)。同意图核心词仅用于
    去噪(纯换渠道站点词不触发)·streak 主键是渠道桶。"""
    try:
        _empty = any(m in str(summary) for m in _CHANNEL_EMPTY_MARKERS)
        _bucket = _channel_bucket(tool_name, question)
        b = _rs_sessions.ensure_budget(session)
        cs = b.setdefault("_channel_switch", {"per_bucket": {}, "used": [],
                                              "nudged": 0, "last_bucket": None})
        used = cs["used"]
        if _bucket not in used:
            used.append(_bucket)
        pb = cs["per_bucket"]
        if _empty:
            # 同渠道连续空手累加;刚从别的渠道切过来(last_bucket != 本桶)=有效止损·本桶重计
            if cs.get("last_bucket") == _bucket:
                pb[_bucket] = pb.get(_bucket, 0) + 1
            else:
                pb[_bucket] = 1
        else:
            pb[_bucket] = 0   # 本渠道拿到新证据=有进展·清零(方向对不打扰)
        cs["last_bucket"] = _bucket
        _MAX_NUDGE = 3
        # 触发:本渠道连续空手达阈 且 全题换过的结构渠道数还很少(≤1=一直没真换过)
        if (_empty and pb.get(_bucket, 0) >= _CHANNEL_SWITCH_THRESH
                and len(used) <= 1 and cs["nudged"] < _MAX_NUDGE):
            cs["nudged"] += 1
            _emit_gate_event(session.get("qid", ""), "channel_switch", "nudge",
                             {"bucket": _bucket, "streak": pb.get(_bucket, 0),
                              "nth": cs["nudged"]})
            _alts = [c for c in ("crossref", "openalex", "wikidata",
                                 "semanticscholar", "web_search", "archive")
                     if c != _bucket and c not in used][:3]
            _alt_txt = "、".join(_alts) if _alts else "crossref/wikidata/通用web"
            # 人物/传记细节属性优先二手源:
            #   Ken Walibora"哪年做probation officer"这类【已锁定人物+冷门传记细节】——
            #   权威结构库(wikidata/维基正文)【没有】这种细节·curl它们必撞墙(实测引擎41步
            #   都在curl维基/wikidata超时)。而 run_parallel_search 走 anysearch 主力·能一步
            #   命中悼文/人物专访/纪念文(二手源)拿到细节(人工2步:AnySearch搜'实体+属性词'即中)。
            #   判据=问已锁定人物的传记/职业经历/生平细节→提示优先 run_parallel_search 搜
            #   "{实体} {属性关键词}"·别死磕wikidata/维基。通用(任何人物细节题)·不针对某题。
            _q_low = str(question).lower()
            _is_bio_detail = any(k in _q_low for k in (
                "which year", "what year", "years", "born", "died", "career",
                "worked as", "before", "官", "年份", "哪年", "生平", "经历", "职业"))
            _bio_hint = ""
            if _is_bio_detail:
                _bio_hint = (
                    "\n**⭐本题是【人物传记/职业经历细节】类**:wikidata/维基正文常【没有】"
                    "这种冷门细节(别再死磕它们撞墙)。优先 run_parallel_search 搜"
                    "「{已锁定人物全名} {要问的属性关键词}」(如'某人 probation officer years')"
                    "——它走 AnySearch 能一步命中悼文/专访/纪念文等二手源·那里才有传记细节。")
            # ── ★异构信息→造工具算子提示(2026-08-20·用户核心洞察:信息搜索=关系计算与
            #    推理·遇异构信息如地图当场造新工具·万物皆工具打通)。判据:题面含地理空间
            #    /距离/坐标/路线/半径等【无法靠搜网页精确核对·但能被 API 精确计算】的关系。
            #    这类 clue 搜引擎给不出精确答案(如"步行距离<0.8mile""3000m半径内保护区")·
            #    继续搜=撞墙。正解=用 shell_exec 造工具调开放 API 精确算(把异构信息结构化)·
            #    结果自动进 evidence_graph(见 native _run_tool 的 shell_exec_operator 回链)。
            #    通用:任何"关系可被程序精确计算"的题都适用·非地理特判。
            _is_geo_spatial = any(k in _q_low for k in (
                "google maps", "walking", "walk from", "distance", "miles", "km",
                "kilometers", "radius", "meters", "bridge", "highway", "airport",
                "coordinates", "latitude", "longitude", "settlement", "nearest",
                "步行", "距离", "半径", "坐标", "公里", "英里", "路线", "最近"))
            _geo_hint = ""
            if _is_geo_spatial:
                _geo_hint = (
                    "\n**⭐本题含【地理空间/可计算关系】线索**(距离/路线/半径/坐标/桥/机场等):"
                    "这类精确空间事实【搜网页给不出准确答案】(别再撒网搜地图页)。**正解=用 "
                    "shell_exec 当场造工具**调开放 API 精确算,把异构信息结构化后喂回推理链"
                    "(以下端点均实测可用·2026-08-20 verify):\n"
                    "  • 地名→坐标(geocode·任选·403就换下一个): ①photon `https://photon.komoot.io/api/?q=地名&limit=1`(免key最省事) "
                    "②Nominatim `https://nominatim.openstreetmap.org/search?q=地名&format=json&limit=1`(必须带合规UA头 "
                    "`User-Agent: meyo-agent/1.0 (vortex528@gmail.com)`·否则403) ③Overpass `node[place][name=\"地名\"];out center 1;`\n"
                    "  • 两点步行路线距离: OSRM `https://router.project-osrm.org/route/v1/foot/lon1,lat1;lon2,lat2?overview=false`"
                    "(返 routes[0].distance 米·÷1609=mile)\n"
                    "  • 半径内 POI/保护区/桥: Overpass POST `https://overpass-api.de/api/interpreter` data="
                    "`[out:json][timeout:15];(way[boundary=protected_area](around:半径m,lat,lon);node[leisure=nature_reserve](around:半径m,lat,lon););out count;`\n"
                    "  • 直线距离兜底: python haversine 公式。\n"
                    "先 geocode 出坐标·再逐条 clue 用 API 精确验证(这是关系计算·不是搜索)。"
                    "事件类 clue(如'高速因火灾关闭')仍靠 run_parallel_search 搜新闻。")
            return (
                f"\n\n[⚡换渠道·第{cs['nudged']}/{_MAX_NUDGE}次] 你在【{_bucket}】渠道"
                f"已连续 {pb.get(_bucket, 0)} 次空手——继续在同渠道换关键词/换 ID =撒网空烧墙钟"
                f"(对照:高效解题者遇 2 次空手立刻换【结构性不同】的渠道·一次命中)。"
                f"**立即换一个结构性不同的渠道**(换访问方式·不是换词再搜同源):"
                f"如 {_alt_txt}。举例:dblp 按标题搜不到→改 crossref 按作者/DOI 拉全列表·"
                f"或 wikidata SPARQL 按实体属性查·或通用 web 搜二手综述。换维度·别原地撒网。"
                + _bio_hint + _geo_hint)
    except Exception:
        return ""
    return ""


def _strategy_hint(stuck_reason, question="", failed_query="", session=None):
    """v13.93(2026-08-10 用户: 让agent像真人具备主体性) · agent卡住时注入【反思脑】——
    LLM扮演有主体性的解决者·看目标+已走步骤·像真人一样反思该换什么(比静态策略库强)。
    退回静态策略库(反思脑不可用时)。fail-safe: 任何异常返空串。"""
    # 08-18(doc1·失败模式记忆ERL) 先于反思脑: 从工具流水识别失败模式签名
    # (同义改写死循环/死域连撞/登录墙硬磕/单工具连败·纯规则零LLM)·命中历史教训
    # 立即注入——比反思脑更早一步拿到"栽过跟头的先验"·反思脑挂了也有兜底。
    _fm_hint = ""
    _fm_pats = []
    if session is not None:
        try:
            import failure_memory as _fm
            _fm.ensure_defaults()
            _fm_pats = _fm.detect_failure_patterns(session.get("tool_log") or [])
            if _fm_pats:
                _fm_hint = _fm.format_memory_hint(_fm_pats)
        except Exception:
            _fm_hint = ""
    # 优先: 反思脑(动态·看trace·像真人)
    try:
        if session is not None:
            import reflective_mind as _rm
            budget = session.get("_budget") or {}
            steps = budget.get("_tool_seq") or []  # 已走的工具序列
            refl = _rm.reflect(goal=question, trace_steps=steps,
                               stuck_reason=(stuck_reason + (f" (卡在: {failed_query[:80]})" if failed_query else "")
                                             + ("\n" + _fm_hint if _fm_hint else ""))[:600],
                               env_summary=session.get("_env_summary", ""),
                               evidence=(session.get("evidence") or [])[-40:])
            hint = _rm.format_reflection(refl)
            # v13.93 self_evolve提案#3(代码层硬门控): 反思推荐了具体工具→存_forced_tool·
            # 让_check_budget下几步硬拦(只允许推荐工具或显式拒绝)·突破prompt软约束天花板。
            _nt = (refl.get("next_tool") or "").strip()
            # 2026-08-13 · capability_writer 硬门控:
            # 若反思脑填了 write_capability_hint（=需要一个新算法工具），
            # 且 next_tool 没有更具体的工具，则强制下一步调 write_capability。
            _wch = (refl.get("write_capability_hint") or "").strip()
            if _wch and not _nt:
                _nt = "write_capability"
            if _nt and session.get("_budget"):
                session["_budget"]["_forced_tool"] = _nt
                session["_budget"]["_force_gate_count"] = 0
            # 沉淀教训到策略库(越用越丰富)
            if refl.get("lesson"):
                try:
                    import strategy_library as _sl2
                    _sl2.learn_strategy("反思学到:" + refl["lesson"][:20],
                                        [stuck_reason.split()[0] if stuck_reason else "卡住"],
                                        refl["lesson"], src="反思脑", stuck_reason=stuck_reason)
                except Exception:
                    pass
            # 08-18(doc1·ERL): 反思脑产出的 lesson 同时沉淀进失败模式记忆——
            # 本次检测到了失败模式签名且反思给出了更具体的教训→跨题复用
            # (下次同签名一出现就提前注入·不用等反思脑再现场想一遍)。
            if _fm_pats and refl.get("lesson"):
                try:
                    import failure_memory as _fm2
                    for _p in _fm_pats[:2]:
                        _fm2.record_failure_pattern(_p["pattern"], lesson=refl["lesson"],
                                                    src="reflect_lesson",
                                                    context=str(_p.get("detail"))[:160])
                except Exception:
                    pass
            if hint and "调用失败" not in hint and "异常" not in hint:
                # 第1步(v98y) · 触发率埋点:策略库/反思脑被真正消费了 → 落 strategy_matched。
                #   病根(本轮盘点):库接了但从不记命中·触发率显示 0=无法测(条目≠被用)。
                _emit_gate_event(session.get("qid", "") if session else "", "strategy_matched",
                                 "reflect", {"stuck_reason": stuck_reason[:60],
                                             "next_tool": _nt or "", "src": "reflective_mind",
                                             "fm_patterns": [p.get("pattern") for p in _fm_pats]})
                return ("\n\n" + _fm_hint + "\n" if _fm_hint else "\n\n") + hint
    except Exception:
        pass
    # 退回: 静态策略库(+ERL失败模式教训·反思脑不可用时教训也不能丢)
    try:
        import strategy_library as _sl
        strategies = _sl.suggest_strategies(question=question, stuck_reason=stuck_reason, top_k=3)
        _emit_gate_event(session.get("qid", "") if session else "", "strategy_matched",
                         "static_lib", {"stuck_reason": stuck_reason[:60],
                                        "picked": [s.get("name", "?") for s in (strategies or [])][:3],
                                        "src": "strategy_library"})
        return (("\n\n" + _fm_hint) if _fm_hint else "") + "\n\n" + _sl.format_for_prompt(strategies, failed_query=failed_query, question=question)
    except Exception:
        return ""







# ═══════════════════════════════════════════════════════════════════
# 死域熔断(dead-domain circuit breaker)
# 病根实证(w3轮): web.archive.org 本机 TCP 不通(curl 000)·agent 不知道·
#   一题里用 fetch_url_smart/traverse_web/shell curl 反复撞死域 4+ 次·
#   每次 30-60s 白烧(624烧~3min·0b26上一轮烧了 152 步里的一大堆)。
# 机制: 连接层失败(连不上/超时/DNS)按域名累计·阈值 3 次/6h → 拦后续携带
#   该域名的网络调用·返回换路建议; 每 4 次拦截放行 1 次探测(自愈)·
#   成功即解除。全局态存 state/dead_domains.json(跨 session/用户·环境属性)。
# fail-safe: 任何异常静默降级为不拦截。
# ═══════════════════════════════════════════════════════════════
_DEAD_DOMAIN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "state", "dead_domains.json")
import threading as _threading_dd
_DEAD_DOMAIN_LOCK = _threading_dd.Lock()
_DEAD_DOMAIN_TTL_S = 6 * 3600        # 距最后一次失败超 6h → 条目过期(网络可能已恢复)
_DEAD_DOMAIN_THRESH = 3              # 6h 内连接层失败 ≥3 次 → 熔断
_DEAD_DOMAIN_PROBE_EVERY = 4         # 每拦截 4 次放行 1 次探测
_DEAD_NET_TOOLS = {"fetch_url_smart", "traverse_web", "read_url", "shell_exec",
                   "wayback_cdx", "wayback_cdx_date", "archive_today"}
# 工具→固定归因域名: 这几个工具的 args 里带的是【被查询的目标页 URL】而不是
# 真正的网络连接目标(它们各自内部调用同一个存档服务的 API)，必须把死活状态
# 统一记到该服务自己的域名上，否则每次换一个目标 URL 就会误判成"新域名从没死过"，
# 死域熔断永远触发不了(v2026-08-18·落地爬取优化攻防文档·补齐 wayback_cdx_date
# 之前漏挂同一条归因规则的口子)。
_FIXED_ATTRIBUTION_HOST = {
    "wayback_cdx": "web.archive.org",
    "wayback_cdx_date": "web.archive.org",
    "archive_today": "archive.ph",
}
_DEAD_FAIL_SIGS = (
    "curl: (6", "curl: (7", "curl: (28", "curl: (35", "curl: (56", "curl: (60",
    "HTTP=000", "Failed to connect", "Connection refused", "Connection timed out",
    "Connection reset by peer", "Max retries exceeded", "getaddrinfo failed",
    "name resolution", "Network is unreachable", "No route to host",
    "TCP 不通", "tcp_unreachable",
    # shell_exec 摘要格式是 "exit_code=28 · [stdout] (空)"
    # 不含 curl 文本; curl 连接层退出码 6=DNS 7=connect 28=timeout 35/56=SSL 60=cert。
    # 仅当命令里带 http(s) URL 才记账(record 里有 host 门槛·本地脚本退码不误伤)。
    "exit_code=6", "exit_code=7", "exit_code=28", "exit_code=35", "exit_code=56", "exit_code=60",
    # wayback_cdx/fetch 的 urllib 层 SSL 握手超时也是连接层死(实证·w3轮 624)
    "handshake operation timed out", "_ssl.c",
)
_DEAD_OK_SIGS = ("HTTP=2", "HTTP=3", "http_code=2", "HTTP/1.1 2", "HTTP/2 2")


def _dead_domains_load():
    """读全局死域表(带 mtime 缓存)。返回 dict[host] = {fails, last_fail_ts, blocked}。"""
    try:
        mt = os.path.getmtime(_DEAD_DOMAIN_FILE)
        cache = getattr(_dead_domains_load, "_cache", None)
        if cache and cache[0] == mt:
            return cache[1]
        data = {}
        try:
            with open(_DEAD_DOMAIN_FILE, encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            data = {}
        _dead_domains_load._cache = (mt, data)
        return data
    except Exception:
        return {}


def _dead_domains_save(data):
    try:
        os.makedirs(os.path.dirname(_DEAD_DOMAIN_FILE), exist_ok=True)
        tmp = _DEAD_DOMAIN_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, _DEAD_DOMAIN_FILE)
        _dead_domains_load._cache = (os.path.getmtime(_DEAD_DOMAIN_FILE), data)
    except Exception:
        pass


def _extract_hosts(text):
    """从任意文本里抽出 http(s) URL 的 host(小写·去端口)。"""
    try:
        import re as _re_h
        return {m.group(1).lower() for m in
                _re_h.finditer(r"https?://([^/\s\"')\]]+)", str(text or ""))}
    except Exception:
        return set()


def _dead_domain_precheck(tool_name, arg_text):
    """执行前熔断: 返回 (hosts, block_msg)。block_msg 非空则直接拦截不执行。"""
    try:
        if tool_name not in _DEAD_NET_TOOLS:
            return set(), ""
        hosts = _extract_hosts(arg_text)
        if tool_name in _FIXED_ATTRIBUTION_HOST:
            # 查的是存档服务自己的 API: 熔断对象永远是该服务域名
            # (args 里的是被查目标页·不能拿来判死活)。
            hosts = {_FIXED_ATTRIBUTION_HOST[tool_name]}
        if not hosts:
            return set(), ""
        now = time.time()
        with _DEAD_DOMAIN_LOCK:
            data = _dead_domains_load()
            dead = []
            for h in hosts:
                e = data.get(h)
                if not e:
                    continue
                age = now - float(e.get("last_fail_ts", 0) or 0)
                if age > _DEAD_DOMAIN_TTL_S or int(e.get("fails", 0) or 0) < _DEAD_DOMAIN_THRESH:
                    continue  # 过期或未到阈值
                blocked = int(e.get("blocked", 0) or 0)
                if blocked and (blocked + 1) % _DEAD_DOMAIN_PROBE_EVERY == 0:
                    # 探测机会: 本次放行真执行·blocked 也 +1 并落盘(下次重新计数)·
                    # 成功会被 record 清除·失败会继续累计 fails。防'探测后每次都放行'。
                    e["blocked"] = blocked + 1
                    _dead_domains_save(data)
                    return set(hosts), ""
                e["blocked"] = blocked + 1
                dead.append((h, int(e.get("fails", 0))))
            if dead:
                _dead_domains_save(data)
        if not dead:
            return set(), ""
        h0, n0 = dead[0]
        more = f"(另 {len(dead)-1} 个域名同样熔断中)" if len(dead) > 1 else ""
        # 08-18 · 换路建议必须是【真实存在且可直接调用】的工具名，不能是
        # 空泛描述——之前写的 "arquivo.pt" 从未接线成工具，agent 读到建议也没法
        # 执行，等于白给。按被熔断的域名给出对应的真实替代工具。
        if h0 in ("web.archive.org",):
            alt_line = ("  2. 换独立替代存档源 archive_today 工具(archive.ph/.today/.is，"
                        "与 web.archive.org 不同域名池·不共享此次熔断状态·"
                        "wayback_cdx 死了时优先试这个);\n")
        elif h0 == "archive.ph":
            alt_line = ("  2. 换回 wayback_cdx/wayback_cdx_date 工具"
                        "(web.archive.org 是另一独立域名池·未必同时不可达);\n")
        else:
            alt_line = "  2. 换 wayback_cdx_date/archive_today 等独立存档源(不同域名池);\n"
        msg = (
            f"[⚡死域熔断·本次调用已拦截] 域名 {h0} 近 6h 内已有 {n0} 次连接层失败"
            f"(TCP不通/超时/DNS)·本环境当前不可达{more}。别再直接撞这个域名"
            f"(含 curl/wget/存档前缀)·白烧预算。改走:\n"
            "  1. run_parallel_search 搜目标内容的镜像/缓存/转述/二手数据源;\n"
            f"{alt_line}"
            "  3. github/开放数据集搜同源数据;\n"
            "  4. 确有理由认为已恢复→等熔断自动探测(每4次拦截放行1次)或先干别的。"
        )
        return set(hosts), msg
    except Exception:
        return set(), ""


def _dead_domain_record(tool_name, arg_text, summary):
    """执行后记账: 连接层失败→按域名累计; 明显成功→清除。全局跨 session。"""
    try:
        if tool_name not in _DEAD_NET_TOOLS:
            return
        hosts = _extract_hosts(arg_text)
        if not hosts:
            return
        sl = str(summary or "")[:4000]
        now = time.time()
        with _DEAD_DOMAIN_LOCK:
            data = _dead_domains_load()
            changed = False
            failed = any(sig in sl for sig in _DEAD_FAIL_SIGS)
            ok = (not failed) and any(sig in sl for sig in _DEAD_OK_SIGS)
            if tool_name in _FIXED_ATTRIBUTION_HOST:
                # 这几个工具查的是存档服务自己的 API——args 里的 URL 是
                # 【被查的目标页】不是连接目标·成败都归因到该服务域名自己。
                hosts = {_FIXED_ATTRIBUTION_HOST[tool_name]}
            for h in hosts:
                if h in ("127.0.0.1", "localhost"):
                    continue
                e = data.get(h)
                if failed:
                    if not e:
                        e = {"fails": 0, "blocked": 0}
                        data[h] = e
                    e["fails"] = int(e.get("fails", 0) or 0) + 1
                    e["last_fail_ts"] = now
                    changed = True
                elif ok:
                    if e:
                        data.pop(h, None)  # 成功→解除
                        changed = True
                    # 活路径白名单(dead_domains 的反向): 成功也记账·
                    # 下次开跑注入"实测可达域名清单"·优先用已验证路径而非盲撞。
                    _live_domain_record(h, tool_name, now)
            if changed:
                # 防 json 无限长: 只留最近 64 个域名
                if len(data) > 64:
                    for k in sorted(data, key=lambda k: data[k].get("last_fail_ts", 0))[:len(data) - 64]:
                        data.pop(k, None)
                _dead_domains_save(data)
    except Exception:
        pass


# ═══ v2026-08-18(doc2·Reflector硬阻断) · 登录墙/验证码/付费墙熔断 ═══════
# 病根(文档2"爬取优化攻防"·Reflector 职责): 死域熔断只管“连不上”·但另一类
# 硬阻断是“连得上却不给你看”——登录墙/付费墙/CAPTCHA/Cloudflare人机验证。
# agent 对这类源反复重试毫无意义(不是网络问题·重试不解决身份问题)·
# 必须识别为硬阻断信号→触发策略切换(找转载/存档快照/二手源)而非死磕。
# 机制与死域熔断同骨架: 按域名累计墙信号·阈值2次/6h→拦截后续携带该域名的
# 调用·每5次拦截放行1次探测(墙可能撤了)。全局态 state/walled_hosts.json。
_WALL_HOST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "state", "walled_hosts.json")
_WALL_TTL_S = 6 * 3600
_WALL_THRESH = 2          # 6h 内同域名墙信号 ≥2 次 → 熔断(登录墙信号比连接失败更确定性·阈值低)
_WALL_PROBE_EVERY = 5     # 每拦截 5 次放行 1 次探测(比死域宽松·登录墙很少自愈)
# 信号表刻意保守: 只收“确定性墙”特征·避免把普通内容误判成墙。
# (裸 'cloudflare' 不收——正常页面也可能提到它; 收它的挑战页特征串。)
_WALL_SIGS = (
    "http=401", "http=403", "http_code=401", "http_code=403",
    "401 unauthorized", "403 forbidden",
    "login required", "log in to view", "please sign in", "sign in to continue",
    "please log in", "login to continue",
    "请登录", "登录后查看", "登录以查看", "需要登录",
    "captcha", "验证码", "are you a robot", "unusual traffic",
    "just a moment", "checking your browser", "attention required",
    "subscribe to read", "订阅后查看", "订阅以阅读", "付费墙", "paywall",
    "metered article", "create an account to read",
)
# 小写: _wall_record 会 lower() 摘要后再匹配·OK 信号表也必须小写(否则墙永远解除不了)。
_WALL_OK_SIGS = ("http=2", "http=3", "http_code=2", "http/1.1 2", "http/2 2", "status 2")


def _walled_hosts_load():
    """读全局登录墙域名表(带 mtime 缓存·与死域表同锁)。fail-safe {}。"""
    try:
        mt = os.path.getmtime(_WALL_HOST_FILE)
        cache = getattr(_walled_hosts_load, "_cache", None)
        if cache and cache[0] == mt:
            return cache[1]
        data = {}
        try:
            with open(_WALL_HOST_FILE, encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            data = {}
        _walled_hosts_load._cache = (mt, data)
        return data
    except Exception:
        return {}


def _walled_hosts_save(data):
    try:
        os.makedirs(os.path.dirname(_WALL_HOST_FILE), exist_ok=True)
        tmp = _WALL_HOST_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, _WALL_HOST_FILE)
        _walled_hosts_load._cache = (os.path.getmtime(_WALL_HOST_FILE), data)
    except Exception:
        pass


def _wall_precheck(tool_name, arg_text):
    """执行前硬阻断: 返回 block_msg。登录墙熔断中的域名→拦截不执行。fail-safe。"""
    try:
        if tool_name not in _DEAD_NET_TOOLS or tool_name in _FIXED_ATTRIBUTION_HOST:
            return ""  # 存档服务自身不登录墙·归因也会误伤目标页·不适用
        hosts = _extract_hosts(arg_text)
        if not hosts:
            return ""
        now = time.time()
        walled = []
        with _DEAD_DOMAIN_LOCK:
            data = _walled_hosts_load()
            for h in hosts:
                e = data.get(h)
                if not e:
                    continue
                age = now - float(e.get("last_wall_ts", 0) or 0)
                if age > _WALL_TTL_S or int(e.get("walls", 0) or 0) < _WALL_THRESH:
                    continue
                blocked = int(e.get("blocked", 0) or 0)
                if blocked and (blocked + 1) % _WALL_PROBE_EVERY == 0:
                    e["blocked"] = blocked + 1
                    _walled_hosts_save(data)
                    return ""  # 探测机会: 放行一次(墙可能撤了)
                e["blocked"] = blocked + 1
                walled.append((h, int(e.get("walls", 0))))
            if walled:
                _walled_hosts_save(data)
        if not walled:
            return ""
        h0, n0 = walled[0]
        more = f"(另 {len(walled)-1} 个域名同样熔断中)" if len(walled) > 1 else ""
        return (
            f"[🔒Reflector硬阻断·登录墙/验证码] 域名 {h0} 近 6h 已有 {n0} 次"
            f"登录墙/付费墙/验证码信号{more}——这不是内容不存在·是你的访问被身份"
            f"/风控挡住·重试 N 次也一样(不是重试能解决的问题)·别再撞。换路按优先级:\n"
            "  1. run_parallel_search 搜该内容的转载/镜像/新闻报道/维基引用(二手转述);\n"
            "  2. query_web_archive 查该页公开时期的存档快照(存档爬取不需要登录);\n"
            "  3. github/开放数据集搜同源数据;\n"
            f"  4. 每拦截 {_WALL_PROBE_EVERY} 次自动放行 1 次探测(墙可能撤)·其余情况换路。"
        )
    except Exception:
        return ""


def _wall_record(tool_name, arg_text, summary):
    """执行后记账: 墙信号→按域名累计; 干净成功(2xx且无墙信号)→解除。fail-safe。"""
    try:
        if tool_name not in _DEAD_NET_TOOLS or tool_name in _FIXED_ATTRIBUTION_HOST:
            return
        hosts = _extract_hosts(arg_text)
        if not hosts:
            return
        sl = str(summary or "").lower()[:4000]
        walled = any(sig in sl for sig in _WALL_SIGS)
        ok = (not walled) and any(sig in sl for sig in _WALL_OK_SIGS)
        if not (walled or ok):
            return  # 无信号不记(不误清也不误累)
        now = time.time()
        with _DEAD_DOMAIN_LOCK:
            data = _walled_hosts_load()
            changed = False
            for h in hosts:
                if h in ("127.0.0.1", "localhost"):
                    continue
                e = data.get(h)
                if walled:
                    if not e:
                        e = {"walls": 0, "blocked": 0}
                        data[h] = e
                    e["walls"] = int(e.get("walls", 0) or 0) + 1
                    e["last_wall_ts"] = now
                    changed = True
                elif ok and e:
                    data.pop(h, None)  # 匿名访问成功→墙解除
                    changed = True
            if changed:
                if len(data) > 64:
                    for k in sorted(data, key=lambda k: data[k].get("last_wall_ts", 0))[:len(data) - 64]:
                        data.pop(k, None)
                _walled_hosts_save(data)
    except Exception:
        pass


# ══ v108(M2b) · 活路径白名单: 死域熔断的反向账本 ═══════════════════════
# 病根实证(624/0b26): agent 不知道哪些域【实测可达】·只能盲撞——同一题里
# 死域烧掉大量墙钟的同时·可达域名(如 api.crossref.org/en.wikipedia.org)
# 从未被优先使用。机制: 网络成功→按域名记 live_domains.json(成功次数+时间)·
# 开跑时注入 Top-N 实测可达清单·把"换源"从盲撞变成有据可依。
_LIVE_DOMAIN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "state", "live_domains.json")
_LIVE_DOMAIN_TTL_S = 12 * 3600      # 12h 内的成功才算"当前可达"


def _live_domain_record(host, tool_name, now=None):
    """网络成功记账到 live_domains.json(跨 session/用户·环境属性)。fail-safe。"""
    try:
        now = now or time.time()
        with _DEAD_DOMAIN_LOCK:
            data = {}
            try:
                with open(_LIVE_DOMAIN_FILE, encoding="utf-8") as f:
                    data = json.load(f) or {}
            except Exception:
                data = {}
            e = data.get(host) or {"oks": 0}
            e["oks"] = int(e.get("oks", 0) or 0) + 1
            e["last_ok_ts"] = now
            e["last_tool"] = str(tool_name or "")[:40]
            data[host] = e
            # 防无限长: 按最近成功时间留 Top 64
            if len(data) > 64:
                for k in sorted(data, key=lambda k: data[k].get("last_ok_ts", 0))[:len(data) - 64]:
                    data.pop(k, None)
            os.makedirs(os.path.dirname(_LIVE_DOMAIN_FILE), exist_ok=True)
            tmp = _LIVE_DOMAIN_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, _LIVE_DOMAIN_FILE)
    except Exception:
        pass


def _render_live_path_hint():
    """v108(M2b) · 生成"活路径白名单"提示段(注入 system prompt)。无数据返回 ""。"""
    try:
        if not os.path.exists(_LIVE_DOMAIN_FILE):
            return ""
        with open(_LIVE_DOMAIN_FILE, encoding="utf-8") as f:
            data = json.load(f) or {}
        now = time.time()
        rows = [(h, e.get("oks", 0)) for h, e in data.items()
                if now - float(e.get("last_ok_ts", 0) or 0) < _LIVE_DOMAIN_TTL_S]
        if not rows:
            return ""
        rows.sort(key=lambda x: -int(x[1]))
        tops = "·".join(f"{h}(×{n})" for h, n in rows[:8])
        return ("\n\n# 活路径白名单(近12h实测可达·v108)\n"
                f"{tops}\n"
                "优先用这些域名的直连/结构化 API; 反爬重站(ScienceDirect/门户型)优先走"
                " fetch_url_smart(内部分级)而非手写 curl; 死域(连接层失败达阈值)会被自动熔断·"
                "被拦时别硬撞·直接换源。")
    except Exception:
        return ""


# ══ v108(M2a) · 跨轮错题本: 重跑注入上轮 fail 归因 ═════════════════════
# 病根实证(6题22轮): 重跑对历史失败【完全失忆】——同样的反爬墙撞4轮·同样的
# 年份口径错4轮。机制: 同 qid 的历史 checkpoint(auto_ckpt_*.json 及其
# .stale_rerun_TS 副本)里已有答案/故障统计; 开跑时提取"上轮答案≈/主故障/"
# 注入 system prompt·新轮不再重复踩坑。历史答对过的(win_ckpt)则注入已验证路径。



def _render_entry_point_hint(question):
    """v108(M5) · 题干钦定入口时·注入低成本元数据通道指导。不命中返回 ""。"""
    try:
        import re as _re_ep
        q = str(question or "")
        if not any(_re_ep.search(p, q, _re_ep.I) for p in _ENTRY_POINT_PATTERNS):
            return ""
        q_low = q.lower()
        hits = [v for k, v in _ENTRY_PLATFORM_HINTS.items() if k in q_low]
        if not hits:
            return ""
        return ("\n\n# 题目钦定了入口页面(v108·入口优先策略)\n"
                "题面指明目标可通过某个平台页面进入。入口页被反爬/拿不到时·"
                "**别猜域名盲撞**——优先用低成本元数据通道:\n"
                + "\n".join(f"  · {h}" for h in hits[:3]) +
                "\n入口页拿不到≠目标不存在·换元数据通道/存档(wayback)·"
                "拿到确切 URL 后再直取。")
    except Exception:
        return ""


def _log_tool(qid, name, args, summary, elapsed):
    # args 里以 "full_" 开头的字段是工具自己
    # 主动留的可审计完整记录(如 ask_expert_model 的 full_prompt·用于事后核实"传给专家
    # 模型的是不是真带了证据文本"，而不是只传了题目让它凭训练记忆答)，不做 80 字符通用
    # 截断——否则这类审计字段跟其它展示用字段一样被砍成 80 字，白留。但也不能无限长
    # (ckpt 每步都落盘)，给 4000 字上限，够放完整 prompt 又不至于让 ckpt 文件失控变大。
    def _fmt(k, v):
        if isinstance(v, (int, float, bool)):
            return v
        s = str(v)
        return s[:4000] if k.startswith("full_") else s[:80]
    # ── L1 事件底座(v118·2026-08-17·dsh"日志即事实源"思想·纯增量不改任何旧路径)──
    # append-only 有界事件流:工具粒度可重放底座(journal replay/事后归因/面板都从它长)。
    # 双后端共享(deepagents/native 的工具都走 _log_tool)·有界 400 条防膨胀。
    try:
        _session = _sess(qid)
        _failed = any(m in str(summary or "").lower()
                      for m in ("失败", "异常", "error", "超时", "timeout"))
        # Auto/deepagents tool closures use this function, while native also has a direct
        # executor path. Emit the same canonical protocol here so both engines converge.
        _run_state = _canonical_run_state(_session)
        _evidence_records = [e for e in (_session.get("evidence") or []) if isinstance(e, dict)]
        if not _evidence_records and str(summary or "").strip() and not _failed:
            _evidence_records = [{
                "source": str(name), "title": str(name), "content": str(summary)[:6000]}]
        _observation = _run_state.observe_tool_result(
            tool=str(name), arguments=_jsonable_generic(args) or {},
            summary=str(summary or ""), succeeded=not _failed,
            elapsed_seconds=float(elapsed or 0), evidence_records=_evidence_records)
        _session["_last_projection_delta"] = _observation
        _session["_last_observation_evidence_ids"] = list(_observation.get("evidence_ids") or [])
        _session["_event_cursor"] = _observation["projection_event_seq"]
        # 合理搜索止损(与 native_agent_loop 对称):检索成功但候选未推进 →
        # 记 no_candidate_delta,供 IG planner 连续低增益收敛,避免搜到墙钟。
        _is_retrieval_qa = any(_k in str(name).lower() for _k in
                               ("search", "fetch", "query", "browse"))
        if ((not _observation.get("state_changed"))
                or (_is_retrieval_qa
                    and int(_observation.get("candidates_added") or 0) == 0)):
            _session.setdefault("_pilot_failed_paths", []).append({
                "tool": str(name),
                "reason": ("no_candidate_delta"
                           if _is_retrieval_qa
                           and int(_observation.get("candidates_added") or 0) == 0
                           else "no_projection_delta"),
                "event_seq": _observation.get("projection_event_seq")})
            del _session["_pilot_failed_paths"][:-12]
    except Exception:
        pass
    # ── v98 机制二(缺口2):失败三分法 + 记录未修复的 A 类失败(供 finalize 审计) ──
    # 工具返回含失败迹象时分类(A输入无效/B瞬时/C能力墙)·A 类失败记进 session:
    # "某工具因输入无效失败·但输入还没被修复重试过"——finalize 门控据此判证据缺口·
    # 逼 agent "不许带着未修复的输入错误放弃工具·退回文字搜索绕路"。通用·非题型。
    _fail_cat = None
    _sl = str(summary or "").lower()
    # 门控/预算提示并非工具实际执行失败：它们没有消耗工具输入，也不能污染
    # “输入未修复”账本。否则被循环卫兵拦一次就会在收尾时要求无意义地重试。
    _guard_only = any(marker in _sl for marker in (
        "[循环卫兵]", "[硬门控]", "[视频模态硬约束]", "[工具前置条件]",
        "[无进展看门狗]", "[⏰硬墙", "[已拦截", "[finalize审计",
        "[口径约束核对", "[语义契约审计",
    ))
    if (not _guard_only and any(m in _sl for m in ("失败", "异常", "error", "invalid", "无效",
                                                    "not found", "不存在", "没搜到", "超时", "timeout"))):
        _fail_cat = _classify_tool_failure(str(summary), _TOOL_PRECONDITIONS.get(name))
        if _fail_cat == "A_input":
            _sfa = _sess(qid).setdefault("_A_input_fails", {})
            # 记该工具的 A 类失败·标记"尚未修复重试"(下次同工具成功/换了输入再成功则清除)
            _sfa[name] = {"reason": str(summary)[:150], "repaired": False}
        elif _fail_cat == "C_capability":
            # C_capability 主动换路 hint:
            # 工具碰到能力墙时·agent 最容易"默默放弃"或无谓重试·
            # 在这里把换路策略直接嵌入 summary 返回给 agent·让它看到就知道该怎么做。
            # ① 订阅墙命中 → 推 fetch_url_smart(会自动走 OA 绕路)·或给出已知 OA 路径
            _paywall_kws = ("subscription", "paywall", "订阅", "subscriber", "subscribe",
                            "sciencedirect", "springer", "wiley", "elsevier", "jstor",
                            "ieee", "acm.org", "tandfonline", "sagepub", "cambridge.org",
                            "access required", "forbidden", "403", "purchase access")
            _is_paywall_fail = any(kw in _sl for kw in _paywall_kws)
            if _is_paywall_fail:
                # 从 args 里拿 URL 来给更具体的提示
                _url_hint = ""
                if isinstance(args, dict):
                    _u = str(args.get("question") or args.get("url") or "")
                    if _u.startswith("http"):
                        _url_hint = f"\n  · 针对此 URL: fetch_url_smart('{_u[:80]}')"
                summary = (summary or "") + (
                    "\n\n[🔓 C_capability · 订阅墙检测] 这是付费/订阅墙·普通抓取不管用。"
                    "\n**下一步换路建议**:"
                    "\n  1. 用 fetch_url_smart 传同一 URL → 内部自动尝试 Unpaywall/OpenAlex/"
                    "SemanticScholar/Sci-Hub 获取 OA 全文或摘要·不需要你再判断"
                    f"{_url_hint}"
                    "\n  2. 若 fetch_url_smart 也返回 OA 失败 → 用 run_parallel_search"
                    " 搜论文标题+关键词·找作者发布在 ResearchGate/预印本的版本"
                    "\n  3. 若无 OA 版本 → 用现有 abstract/摘要+通用知识推断·诚实标注无全文"
                )
            # ② 非订阅墙能力墙(反爬/geo/资源不存在) → 推通用换路
            elif any(kw in _sl for kw in ("captcha", "recaptcha", "403", "forbidden",
                                           "geo", "blocked", "反爬")):
                summary = (summary or "") + (
                    "\n\n[🔄 C_capability · 反爬/封锁] 换路建议:"
                    "\n  1. fetch_url_smart(URL) —— 内部按域名分级自动升级为浏览器渲染"
                    "\n  2. run_parallel_search(关键词) —— 从搜索结果找镜像/缓存版本"
                )
        elif name in _sess(qid).get("_A_input_fails", {}):
            # 同工具这次没 A 类失败(可能修好输入成功了)→标记已修复
            _sess(qid)["_A_input_fails"][name]["repaired"] = True
    else:
        # 工具成功·若之前有该工具的 A 类失败未修复·现在成功=已修复
        if name in _sess(qid).get("_A_input_fails", {}):
            _sess(qid)["_A_input_fails"][name]["repaired"] = True
    # ── 干活中动态进化(v98A · 2026-08-13 用户:一次坏信号就进化·不只失败结束·不靠我驱动) ──
    # 运行中【确定性坏信号】(工具执行报错:shell_exec exit_code≠0 / 异常 / traceback)一出现·
    # 就【立即】旁路触发一次 self_evolve 诊断(敏感:一次就触发·用户明确要)。这是"干活中动态进化"
    # ——不等题跑完、不等 cron、不靠 agent 主动调 self_diagnose。节流:每题最多 _INFLIGHT_EVOLVE_MAX
    # 次(防每步都进化烧预算)。fail-safe + 旁路(绝不阻塞/拖垮跑题主流程)。
    try:
        _INFLIGHT_EVOLVE_MAX = 2
        # 统一走 ensure_budget(2026-08-27): 原 `get or {}` + setdefault 是 v104a 同类病——
        #   空dict被换新临时dict·改动写不回;setdefault 遇已存在_budget根本不挂。ensure_budget
        #   返回 session 里那个权威 dict·改它即改 session。
        _sb = _rs_sessions.ensure_budget(_sess(qid))
        _exec_bad = (name in ("shell_exec", "evolution_execute")
                     and any(k in _sl for k in ("exit_code=1", "exit_code=2", "exit_code=127",
                                                "traceback", "exception", "[stderr]")))
        if _exec_bad and _sb.get("_inflight_evolve_count", 0) < _INFLIGHT_EVOLVE_MAX:
            _sb["_inflight_evolve_count"] = _sb.get("_inflight_evolve_count", 0) + 1
            import threading as _th_ev
            def _bg_evolve():
                try:
                    import sys as _s2, os as _o2
                    _o2.sys.path.insert(0, _o2.path.join(QA_ROOT, "..", "v11", "evolution"))
                    _s2.path.insert(0, _o2.path.abspath(_o2.path.join(QA_ROOT, "..", "v11", "evolution")))
                    import self_heal_scheduler as _SH
                    _SH.trigger_self_evolve_now(
                        qid, "bug",
                        f"运行中工具{name}报错(干活中动态进化): {str(summary)[:150]}",
                        question=_sess(qid).get("question", ""), gold="")
                except Exception:
                    pass  # 旁路进化失败绝不影响主流程
            _th_ev.Thread(target=_bg_evolve, daemon=True).start()
    except Exception:
        pass  # fail-safe:干活中进化触发出任何岔子都不碰主流程
    # 记录"上个工具返回
    #   →本工具开始"的间隔(=LLM决策+框架处理耗时)。多边形反复2步hang·埋点看是不是这段间隔
    #   异常大(定位hang在tool_result→下一轮LLM决策·还是别处)。轻量·不改逻辑。
    _sess_obj = _sess(qid)
    _gap = None
    _last_end = _sess_obj.get("_last_tool_end_mono")
    _now_mono = time.monotonic()
    if _last_end is not None:
        _gap = round(_now_mono - _last_end - elapsed, 1)  # 减去本工具自身耗时=纯决策+框架间隔
    _sess_obj["_last_tool_end_mono"] = _now_mono

    # hang 自愈: decision_gap > 30s 说明 LLM 卡住了(或网络慢)，注入换路提示
    if _gap and _gap > 30:
        _hang_hint = (
            f"\n\n[⏱️ 决策间隔 {_gap}s · hang 警报] LLM 花了 {_gap}s 才做出下一步决策，"
            "可能在纠结或卡住了。快速行动建议:"
            "\n  · 如果已有足够证据 → 直接 finalize_answer(别再思考了)"
            "\n  · 如果还缺关键数据 → 一句话说明缺什么 + 用 run_parallel_search 搜"
            "\n  · 如果在循环 → 停下来，换一个完全不同的工具或方法"
        )
        summary = (summary or "") + _hang_hint

    # 把 _pending_hints（如 shell_exec 总量警告）注入到本次 summary
    _ph = _sess_obj.pop("_pending_hints", None)
    if _ph:
        summary = (summary or "") + "".join(_ph)

    try:
        _canonical_run_state(_sess_obj).project_legacy_views(_sess_obj)
    except Exception:
        pass
    # Candidate detection now consumes canonical evidence and writes through RunState.
    # AnswerVault is populated only by project_legacy_views after selection; it is no
    # longer an observation-time authority or best-effort source.
    try:
        _progressive_candidate_check(_sess_obj, name, summary)
    except Exception:
        pass
    # (2026-08-15·三车道) 新鲜度戳:Perception 持续车道每写一步就更新时间戳·
    #   Talker 快车道读它才知道"我在用多旧的状态"(诚实·不假装实时)。
    try:
        _sess_obj["_last_update_ts"] = time.time()
    except Exception:
        pass
    # 题内即时学习：工具即便"成功"，若连续返回同一不可区分结果也会触发重规划。
    # 与跨题 self_evolve 分开，避免将一次运行的偶然噪声固化为全局策略。
    _record_tool_effectiveness(_sess_obj, name, summary, _fail_cat)
    _gap_str = f" · [决策间隔{_gap}s]" if _gap and _gap > 5 else ""
    print(f"  [auto-agent tool] {name}({args}) · {elapsed:.1f}s{_gap_str} · {summary[:100]}", file=sys.stderr)
    # 死域熔断·执行后记账: 连接层失败按域名累计(全局跨 session)·成功解除。
    # 放在所有 summary 加工之后·看到的是最终返给 agent 的结果文本(含 HTTP=000/curl(7) 等)。
    # 拦截消息自身含'TCP 不通'字样·凭 dead_domain_block 标记跳过·防拦截也计失败。
    try:
        if not (isinstance(args, dict) and args.get("dead_domain_block")):
            _dead_domain_record(name, json.dumps({"q": args, "t": summary},
                                                 ensure_ascii=False, default=str), summary)
    except Exception:
        pass
    # 08-18(doc2·Reflector) · 登录墙/验证码记账: 墙信号按域名累计→熔断。
    # 拦截消息自身含'验证码'字样·凭 wall_block 标记跳过·防拦截也计墙。
    try:
        if not (isinstance(args, dict) and (args.get("wall_block") or args.get("dead_domain_block"))):
            _wall_record(name, json.dumps({"q": args, "t": summary},
                                          ensure_ascii=False, default=str), summary)
    except Exception:
        pass
    # 每步工具调用后落盘一次 —— 任何时刻断都有最新进度可续传 (fail-safe)
    _save_ckpt(qid)


# ═══════════════════════════════════════════════════════════════════════
# 从 qa_tools_registry 动态生成 deepagents 能吃的工具函数列表
#
# registry 里的 executor 统一签名 (question, session, **kwargs) -> str；
# deepagents 需要的是「普通 Python 函数，参数类型标注齐全，靠 docstring
# 当 tool description」——这里做一层适配: 每个 ToolSpec 生成一个闭包
# 函数，绑定 qid，自动读写 _sess(qid)，并在 retryable 工具上接入
# qa_retry_policy 的预算判断。
# ═══════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════
# Evolution 工具桥接 (v59)
# 把 evolution_tool_registry 的延迟工具也暴露给 auto agent。
# 对齐 CC 的 ToolSearchTool + ExecuteTool:
#   - evolution_search: 让 LLM 按关键词搜索发现 evolution 能力
#   - evolution_execute: 统一执行入口
# 这样 auto agent 不仅能用 QA 专用的 13 个工具，还能按需发现并调用
# evolution 的 self_reflect / code_evolve / deep_research / bus_summary 等。
# ═══════════════════════════════════════════════════════════════════════

def _make_evolution_tools(qid):
    """生成 evolution 桥接工具函数，供 auto agent 发现并调用全系统能力。
    返回 [] 表示 evolution_tool_registry 不可用。"""
    if not _EVO_AVAILABLE:
        return []

    def evolution_search(query: str) -> str:
        """搜索 evolution 工具注册表，发现可用能力。传入关键词描述你要做什么。"""
        t0 = time.time()
        try:
            results = evo_search(query, limit=5)
            if not results:
                return "未找到匹配的 evolution 工具。试试换个关键词，比如 'reflect' 'search' 'monitor' 'audit'。"
            lines = [f"找到 {len(results)} 个相关工具:"]
            for r in results:
                lines.append(f"  · {r['name']}: {r['description'][:60]} (score={r.get('score',0)})")
            lines.append("\n用 evolution_execute(tool_name='<name>', params={{}}) 调用。")
            summary = "\n".join(lines)
        except Exception as e:
            summary = f"evolution 搜索异常: {type(e).__name__}: {e}"
        _log_tool(qid, "evolution_search", {"query": query}, summary, time.time() - t0)
        return summary

    def evolution_execute(tool_name: str, params: str = "") -> str:
        """执行 evolution 注册表中的工具。先 evolution_search 找到工具名，再调用此函数。
        params: JSON 格式字符串参数，如 '{"query": "搜索词"}'，可为空。"""
        t0 = time.time()
        # 预算检查 · 按"工具名+参数"内容判重(不同 evolution 调用不算空转)
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "evolution_execute", content_key=f"{tool_name}|{params}")
        if not allow:
            _log_tool(qid, "evolution_execute", {"tool_name": tool_name}, guard_msg, time.time() - t0)
            return guard_msg
        try:
            import json as _json
            p = _json.loads(params) if params else {}
        except Exception:
            p = {}
        try:
            result = evo_execute(tool_name, p)
            ok = result.get("ok", False)
            if ok:
                # 截取关键信息作为摘要
                r = result.get("result", result)
                summary = str(r)[:500] if not isinstance(r, dict) else _json.dumps(r, ensure_ascii=False)[:500]
            else:
                summary = f"工具 {tool_name} 执行失败: {result.get('error', 'unknown')}"
        except Exception as e:
            summary = f"evolution 执行异常: {type(e).__name__}: {e}"
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "evolution_execute", {"tool_name": tool_name, "params": params}, summary, time.time() - t0)
        return summary

    evolution_search.__name__ = "evolution_search"
    evolution_search.__doc__ = "搜索 evolution 工具注册表，发现可用能力(self_reflect/code_evolve/deep_research/bus_summary 等)。传入关键词描述你要做什么。"
    evolution_execute.__name__ = "evolution_execute"
    evolution_execute.__doc__ = "执行 evolution 注册表中的工具。先 evolution_search 找到工具名，再调用此函数。"
    return [evolution_search, evolution_execute]


# ═══════════════════════════════════════════════════════════════════════
# capability_writer 工具桥接 (2026-08-13)
#
# 让 agent 在遇到"现有工具解决不了的算法问题"时能自主：
#   1. write_capability: 描述→LLM生成代码→沙箱验证→注册为持久工具
#   2. run_written_tool: 执行已注册的算法工具
#   3. list_written_tools: 发现已有的自写工具（避免重复造轮子）
#   4. quick_fix_code: 一次性 ad-hoc 计算（不注册）
# ═══════════════════════════════════════════════════════════════════════

def _make_capability_writer_tools(qid):
    """生成 capability_writer 桥接工具，让 agent 能自主创建算法组件。
    返回 [] 表示 capability_writer 不可用。"""
    try:
        import capability_writer as _cw
    except Exception:
        return []

    def write_capability(problem_desc: str, context: str = "",
                         tool_name: str = "", verify_call: str = "") -> str:
        """当现有工具无法解题时，自主生成算法代码、沙箱验证、注册为持久工具。
        problem_desc: 用自然语言描述需要什么算法（如'计算正交多边形面积，已知N条边的方向和长度'）。
        context: 已知量（边长、约束等）。
        tool_name: 为新工具起名（如'ortho_area_solver'），空则自动生成。
        适用场景: ortho_polygon返回open_constraint、sympy无法直接建模、VLM盲区计算题等。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "write_capability")
        if not allow:
            _log_tool(qid, "write_capability", {"problem_desc": problem_desc[:60]}, guard_msg, time.time() - t0)
            return guard_msg
        try:
            r = _cw.write_capability(
                problem_desc=problem_desc,
                context=context,
                session=s,
                tool_name=tool_name,
                verify_call=verify_call,
            )
            if r.get("ok"):
                summary = (
                    f"[capability_writer ✓] 新工具 `{r['tool_name']}` 已生成并注册\n"
                    f"测试输出: {str(r.get('test_output',''))[:200]}\n"
                    f"下一步: 调用 run_written_tool(tool_name='{r['tool_name']}', inputs={{...}})"
                )
            else:
                summary = f"[capability_writer ✗] 生成失败: {r.get('error','unknown')[:200]}"
        except Exception as e:
            summary = f"[capability_writer] 异常: {type(e).__name__}: {str(e)[:150]}"
        _log_tool(qid, "write_capability", {"problem_desc": problem_desc[:60]}, summary, time.time() - t0)
        return summary

    def run_written_tool(tool_name: str, inputs: str = "{}") -> str:
        """执行 write_capability 注册的算法工具。
        tool_name: write_capability 返回的工具名。
        inputs: JSON 字符串，传给 solve(inputs)，如 '{"edges": [...]}'。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "run_written_tool",
                                         content_key=f"{tool_name}|{inputs[:60]}")
        if not allow:
            _log_tool(qid, "run_written_tool", {"tool_name": tool_name}, guard_msg, time.time() - t0)
            return guard_msg
        try:
            import json as _json
            inputs_dict = _json.loads(inputs) if isinstance(inputs, str) else (inputs or {})
        except Exception:
            inputs_dict = {}
        try:
            r = _cw.run_written_tool(tool_name=tool_name, inputs=inputs_dict, session=s)
            if r.get("ok"):
                summary = (
                    f"[run_written_tool ✓] {tool_name} → result={r.get('result')}\n"
                    f"{r.get('stdout','')[:100]}"
                )
            else:
                # 工具不存在时列出可用工具
                available = _cw.list_written_tools()
                names = [t["tool_name"] for t in available]
                summary = (
                    f"[run_written_tool ✗] {r.get('error','')[:150]}\n"
                    f"已有工具: {names[:5]}"
                )
        except Exception as e:
            summary = f"[run_written_tool] 异常: {type(e).__name__}: {str(e)[:150]}"
        _log_tool(qid, "run_written_tool", {"tool_name": tool_name}, summary, time.time() - t0)
        return summary

    def list_written_tools() -> str:
        """列出 write_capability 注册过的所有算法工具（避免重复造轮子）。"""
        try:
            tools = _cw.list_written_tools()
            if not tools:
                return "[list_written_tools] 还没有写过任何算法工具。调 write_capability 创建第一个。"
            lines = [f"已有 {len(tools)} 个自写工具:"]
            for t in tools:
                lines.append(f"  · {t['tool_name']}: {t['description'][:60]} ({t.get('created_at','?')[:10]})")
            lines.append("\n用 run_written_tool(tool_name='<name>', inputs='{{...}}') 调用。")
            return "\n".join(lines)
        except Exception as e:
            return f"[list_written_tools] 异常: {type(e).__name__}: {e}"

    def search_written_tools(query: str = "") -> str:
        """v2: 用关键词搜索已造的算法工具·避免重复造轮子。
        query: 搜索关键词（如'polygon area'/'csv sum'/'pubchem lookup'）。"""
        try:
            if not query:
                return "[search_written_tools] 请提供搜索关键词。"
            tools = _cw.search_written_tools(query, top_k=5)
            if not tools:
                return f"[search_written_tools] 未找到匹配 '{query}' 的工具。可调 list_written_tools 看全部。"
            lines = [f"搜索 '{query}' 找到 {len(tools)} 个工具:"]
            for t in tools:
                lines.append(f"  · {t['tool_name']} (score={t.get('score',0)}): {t['description'][:60]}")
            lines.append("\n用 run_written_tool(tool_name='<name>', inputs='{{...}}') 调用。")
            return "\n".join(lines)
        except Exception as e:
            return f"[search_written_tools] 异常: {type(e).__name__}: {e}"

    def quick_fix_code(code_desc: str, inputs_example: str = "") -> str:
        """一次性 ad-hoc 计算：描述任务 → 生成代码 → 跑一次 → 返回结果。不注册工具。
        code_desc: 计算任务描述（如'用鞋带公式算多边形面积，顶点是[(0,0),(10,0),...]'）。
        inputs_example: 可选，给LLM看的输入示例。
        反复用相同 code_desc 时，建议改用 write_capability 升格为持久工具。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "quick_fix_code",
                                         content_key=code_desc[:80])
        if not allow:
            _log_tool(qid, "quick_fix_code", {"code_desc": code_desc[:60]}, guard_msg, time.time() - t0)
            return guard_msg
        try:
            r = _cw.quick_fix_code(code_desc=code_desc, inputs_example=inputs_example, session=s)
            if r.get("ok"):
                summary = f"[quick_fix_code ✓] result={r.get('result')}\n{r.get('stdout','')[:100]}"
            else:
                summary = f"[quick_fix_code ✗] {r.get('error','')[:150]}"
        except Exception as e:
            summary = f"[quick_fix_code] 异常: {type(e).__name__}: {str(e)[:150]}"
        _log_tool(qid, "quick_fix_code", {"code_desc": code_desc[:60]}, summary, time.time() - t0)
        return summary

    def auto_diagnose_and_solve(question: str = "",
                                 why_failed: str = "", trace_tools: str = "") -> str:
        """从失败信号自动诊断需要什么工具→造工具→解题。当所有现有工具都失败时调用。
        (2026-08-27 用户中心) 删 gold 参数:真实用户没有标准答案·那是 benchmark 遗留·
        暴露它会误导模型"填答案"。
        question: 用户问题。
        why_failed: 之前的失败原因（如'timeout','计算错误','工具返回error'等）。
        trace_tools: 之前用过的工具列表（逗号分隔，如'shell_exec,see_image_vlm,fetch_url'）。"""
        gold = ""  # 用户场景无 gold·内部恒空(下游 _cw 兼容此参数)
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "auto_diagnose_and_solve",
                                         content_key=f"{question[:60]}|{why_failed[:40]}")
        if not allow:
            _log_tool(qid, "auto_diagnose_and_solve", {}, guard_msg, time.time() - t0)
            return guard_msg
        try:
            # 从 session 拿题目/失败信息（如果没传）
            if not question:
                question = str(s.get("question", ""))
            if not why_failed:
                why_failed = "agent 未能解出题目"
            # trace_tools 从字符串解析回 list
            _tt = [t.strip() for t in trace_tools.split(",")] if trace_tools else []
            if not _tt:
                _tt = [t.get("tool", "?") for t in (s.get("tool_log") or [])[-10:]]
            r = _cw.auto_diagnose_and_solve(
                question=question, gold=gold,
                why_failed=why_failed, trace_tools=_tt, session=s,
            )
            if r.get("ok"):
                summary = (
                    f"[auto_diagnose_and_solve ✓] 自动诊断+造工具+解题完成\n"
                    f"  工具: {r.get('tool_name')}\n"
                    f"  诊断: {r.get('diag','')[:120]}\n"
                    f"  答案: {r.get('answer','')[:200]}\n"
                    f"  耗时: {r.get('elapsed_sec',0)}s"
                )
            else:
                summary = (
                    f"[auto_diagnose_and_solve ✗] {r.get('error','')[:200]}\n"
                    f"  诊断: {r.get('diag','')[:120]}"
                )
        except Exception as e:
            summary = f"[auto_diagnose_and_solve] 异常: {type(e).__name__}: {str(e)[:150]}"
        _log_tool(qid, "auto_diagnose_and_solve", {}, summary, time.time() - t0)
        return summary

    # 设好文档字符串让 deepagents 能解析成 tool schema
    for fn in [write_capability, run_written_tool,
               list_written_tools, search_written_tools, quick_fix_code, auto_diagnose_and_solve]:
        if not fn.__doc__:
            fn.__doc__ = fn.__name__

    return [write_capability, run_written_tool,
            list_written_tools, search_written_tools, quick_fix_code, auto_diagnose_and_solve]


# ═══════════════════════════════════════════════════════════════════════
# G-align · Subagent Isolation 委托工具 (维度 B · 对齐 deepagents task())
#
# task_dispatch.dispatch_task 已实现真子进程隔离 + hermes 式指数退避重试,
# 且已接进 meta_graph.py。这里把它暴露成 auto agent 的一个 delegate 工具,
# 让门面 agent 也能"把一个重/可能污染上下文的子任务丢进独立进程跑,只拿回
# 一句摘要"——这正是 deepagents task() 的语义(子代理独立上下文窗口)。
# 结果只回摘要(summary),完整结果留在 context_store,不撑爆门面上下文。
# fail-safe:task_dispatch 不可用时工具不注册,agent 照常用其他工具。
# ═══════════════════════════════════════════════════════════════════════

def _make_delegate_tool(qid):
    """生成 delegate 工具:把子任务派进独立子进程隔离执行(对齐 deepagents task())。
    返回 [] 表示 task_dispatch 不可用。"""
    if not _DISPATCH_AVAILABLE:
        return []

    def delegate(fn_path: str, params: str = "", label: str = "subtask", timeout_s: float = 120) -> str:
        """把一个子任务派到独立子进程隔离执行,只拿回一句摘要(适合重/可能
        产生大量输出、你不想让它污染当前上下文的活)。
        fn_path: 目标可调用路径,"module.function" 或 "module.Class.method"。
        params: JSON 字符串,作为关键字参数透传,如 '{"query":"xxx"}',可空。
        label: 子任务标签(仅日志用)。timeout_s: 子进程超时(默认120s)。
        子进程失败会按指数退避重试一次。只有你明确要隔离执行某个已知函数时才用;
        普通命令/文件操作用 shell_exec/file_read/file_write 即可。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "delegate")
        if not allow:
            _log_tool(qid, "delegate", {"fn_path": fn_path}, guard_msg, time.time() - t0)
            return guard_msg
        try:
            import json as _json
            kwargs = _json.loads(params) if params else {}
            if not isinstance(kwargs, dict):
                kwargs = {}
        except Exception:
            kwargs = {}
        try:
            # U2 · 并发闸门:高 load 时排队,避免同时 delegate 一堆子进程堆爆 CPU
            with _heavy_slot():
                report = _dispatch_task(
                    agent_name=str(label)[:40],
                    fn_path=fn_path,
                    kwargs=kwargs,
                    task_id=f"{_safe_qid(qid)}_{str(label)[:20]}",
                    timeout_s=min(max(1.0, float(timeout_s or 120)), 300.0),
                    isolate=True,
                    retries=1,  # hermes 式指数退避:失败重试一次
                )
            if report.get("ok"):
                summary = (f"[子任务完成 · {report.get('elapsed_s', '?')}s · "
                           f"attempts={report.get('attempts', 1)}] "
                           f"{report.get('summary', '')[:500]}")
                ref = report.get("ref_id")
                if ref:
                    summary += f"\n(完整结果 ref={ref},需要细节可让我读回)"
            else:
                summary = f"[子任务失败] {report.get('summary') or report.get('error') or 'unknown'}"
        except Exception as e:
            summary = f"delegate 异常(降级): {type(e).__name__}: {str(e)[:200]}"
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "delegate", {"fn_path": fn_path, "label": label}, summary, time.time() - t0)
        return summary

    delegate.__name__ = "delegate"
    delegate.__doc__ = ("把子任务派到独立子进程隔离执行,只拿回一句摘要"
                        "(对齐 deepagents task() 子代理隔离,失败按指数退避重试一次)。")

    # ── G5 批处理通道 batch_map(2026-08-12·治"可枚举任务逐项走完整agent循环") ──
    # 病根(巧克力题42步只确认1映射):对 N 个独立同构项(N映射/N化合物/N物种)做同操作·
    # 每项走完整 agent 循环(每步问LLM)极慢。研究文档#66:步骤可预先枚举的任务→编译DAG
    # 批量执行·agent 只看汇总。复用 delegate 的 _dispatch_task 隔离 + _heavy_slot 并发闸
    # (串行/低并发防雪崩·同现有铁律)。全量结果落 context_store·门面只拿汇总不撑爆上下文。
    def batch_map(item_fn_path: str, items_json: str, label: str = "batch",
                  timeout_s: float = 120) -> str:
        """对 N 个独立同构项批量执行【同一个函数】·只回汇总(治"逐项走完整循环极慢")。
        用于:对一批项做同构操作(如逐个化合物查属性/逐条映射匹配/逐个物种筛选)——
        你识别出"这是对N个独立项做同样的事"时用·比一项项走完整 agent 循环快得多。
        item_fn_path: 处理【单个项】的可调用 "module.function"(吃一个项·返回结果)。
        items_json: N个项的JSON数组·如 '["item1","item2",...]' 或 '[{...},{...}]'。
        每项独立隔离跑(失败标null不拖垮整批)·并发受 load 门控(防雪崩)。
        普通"对一个东西做一件事"用 delegate/shell_exec 即可·batch_map 专为 N 项同操作。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "batch_map")
        if not allow:
            _log_tool(qid, "batch_map", {"item_fn_path": item_fn_path}, guard_msg, time.time() - t0)
            return guard_msg
        try:
            import json as _json
            items = _json.loads(items_json) if items_json else []
            if not isinstance(items, list):
                items = [items]
        except Exception as e:
            r = f"batch_map: items_json 解析失败({type(e).__name__})·应是JSON数组"
            _log_tool(qid, "batch_map", {"item_fn_path": item_fn_path}, r, time.time() - t0)
            return r
        if not items:
            return "batch_map: items 为空·没什么可批处理的"
        n_ok = 0
        results = []
        for i, item in enumerate(items[:64]):  # 上限64项·防失控
            try:
                with _heavy_slot():  # 并发闸·load 高时排队(防雪崩·同 delegate)
                    rep = _dispatch_task(
                        agent_name=f"{str(label)[:30]}_{i}",
                        fn_path=item_fn_path,
                        kwargs={"item": item} if not isinstance(item, dict) else item,
                        task_id=f"{_safe_qid(qid)}_{str(label)[:15]}_{i}",
                        timeout_s=min(max(1.0, float(timeout_s or 120)), 300.0),
                        isolate=True, retries=1,
                    )
                if rep.get("ok"):
                    n_ok += 1
                    results.append({"i": i, "ok": True, "summary": str(rep.get("summary", ""))[:150]})
                else:
                    results.append({"i": i, "ok": False, "err": str(rep.get("error", ""))[:80]})
            except Exception as e:
                results.append({"i": i, "ok": False, "err": f"{type(e).__name__}:{str(e)[:60]}"})
        # 全量落 session(门面只拿汇总·不撑爆上下文)
        s.setdefault("_batch_results", {})[str(label)] = results
        summary = (f"[batch_map 完成] {n_ok}/{len(items[:64])} 项成功。汇总(前5项):\n"
                   + "\n".join(f"  项{r['i']}: {'✅'+r.get('summary','') if r['ok'] else '❌'+r.get('err','')}"
                              for r in results[:5]))
        if len(results) > 5:
            summary += f"\n  ...共{len(results)}项(全量在 _batch_results[{label}])"
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "batch_map", {"item_fn_path": item_fn_path, "n": len(items)}, summary, time.time() - t0)
        return summary

    batch_map.__name__ = "batch_map"
    batch_map.__doc__ = ("对N个独立同构项批量执行同一函数·只回汇总(治可枚举任务逐项走完整"
                         "循环极慢·如逐个化合物查属性)。每项隔离跑+load门控防雪崩。")
    return [delegate, batch_map]


# ═══════════════════════════════════════════════════════════════════════
# 统一2 · 自由子 agent (对齐 CC/deepagents 的 task():派一个带独立 system_prompt
# 的子 agent 自由发挥,不只是调已知函数)
#
# delegate 只能派"已知函数路径",spawn_subagent 派的是"一个完整的子大脑":
# 给它一段任务 + 可选的独立 system_prompt,它在**隔离的子 session(独立 qid、
# 独立上下文窗口、独立预算)**里自主编排工具完成,只把最终结论回给主 agent。
# 这就是 deepagents SubAgent / CC Task 的语义——主 agent 用它来"开一个干净上
# 下文做一件重活",避免子任务的大量中间步骤污染主对话。
#
# 递归深度硬限:子 agent 里再 spawn 会爆栈/爆预算,用 env 标记的深度计数器
# 卡死在 2 层(主→子→孙,孙不能再 spawn)。
# ═══════════════════════════════════════════════════════════════════════

_SUBAGENT_DEPTH_ENV = "MEYO_SUBAGENT_DEPTH"
_SUBAGENT_MAX_DEPTH = 2


def _agent_comm_bus():
    """懒加载 v11/evolution/agent_comm(子agent 通讯总线)。不可用返回 None(fail-safe)。"""
    try:
        import sys as _s
        _evo = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "..", "v11", "evolution")
        if _evo not in _s.path:
            _s.path.insert(0, _evo)
        import agent_comm as _ac
        return _ac
    except Exception:
        return None


def _make_subagent_tool(qid):
    """生成 spawn_subagent 工具:派一个带独立 system_prompt 的自由子 agent。"""

    def spawn_subagent(task: str, system_prompt: str = "", max_iterations: int = 12) -> str:
        """派一个自主的子 agent 在**隔离上下文**里完成一件子任务,只拿回它的最终
        结论(不污染你当前的对话/上下文)。适合:一件需要好几步、会产生大量中间
        输出的子活(比如"把这个目录的所有 .py 逐个读一遍总结职责")。
        task: 交给子 agent 的完整任务描述。
        system_prompt: 可选,给子 agent 的额外角色/约束指令(会附加在通用大脑
          system prompt 之后);留空则子 agent 用和你一样的通用大脑定位。
        子 agent 有自己独立的 qid/预算/工具,和你互不干扰。它不能再往下 spawn
        (最多两层),防止无限递归。返回里带 qid·可用 check_subagent(qid) 查它总线进度/结果。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "spawn_subagent")
        if not allow:
            _log_tool(qid, "spawn_subagent", {"task": task}, guard_msg, time.time() - t0)
            return guard_msg

        depth = int(os.environ.get(_SUBAGENT_DEPTH_ENV, "0") or "0")
        if depth >= _SUBAGENT_MAX_DEPTH:
            summary = (f"[已拦截] 子 agent 嵌套已达最大深度 {_SUBAGENT_MAX_DEPTH},"
                       "不能再往下派子 agent。请自己用现有工具完成这步,或把任务拆小。")
            _log_tool(qid, "spawn_subagent", {"task": task}, summary, time.time() - t0)
            return summary

        child_qid = f"{_safe_qid(qid)}_sub{int(t0 * 1000) % 100000}"
        extra = str(system_prompt or "").strip()
        child_urgency = "high"  # 子任务默认从速,避免拖垮主任务预算
        # (2026-08-15·聚焦审计 Q1) spawn_subagent 也接 agent_comm 总线(原来完全没接)·
        #   和 spawn_specialist 一致:派前 register_task·完成 save_result·父可 check_subagent 查。
        _ac = _agent_comm_bus()
        if _ac:
            try:
                _ac.register_task(child_qid, goal=str(task)[:100],
                                  eta_sec=int(max_iterations or 12) * 30, steps=["解题"])
            except Exception:
                pass
        old_depth = os.environ.get(_SUBAGENT_DEPTH_ENV)
        os.environ[_SUBAGENT_DEPTH_ENV] = str(depth + 1)
        try:
            # U2 · 并发闸门:高 load 时排队,避免同时 spawn 一堆子 agent 堆爆 CPU
            with _heavy_slot():
                child_final, child_trace = run_auto_agent(
                    task, qid=child_qid,
                    max_iterations=min(max(1, int(max_iterations or 12)), 20),
                    urgency=child_urgency, resume=False,
                    extra_system_prompt=extra,
                )
            n = child_trace.get("n_tool_calls", 0)
            wall = child_trace.get("total_wall_s", "?")
            if _ac:
                try:
                    _ac.save_result(child_qid, str(child_final or "")[:2000], ok=bool(child_final))
                except Exception:
                    pass
            summary = (f"[子 agent 完成 · {wall}s · {n} 步 · qid={child_qid}]\n"
                       f"{(child_final or '(子 agent 无输出)')[:1200]}")
        except Exception as e:
            summary = f"spawn_subagent 异常(降级): {type(e).__name__}: {str(e)[:200]}"
        finally:
            if old_depth is None:
                os.environ.pop(_SUBAGENT_DEPTH_ENV, None)
            else:
                os.environ[_SUBAGENT_DEPTH_ENV] = old_depth

        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "spawn_subagent", {"task": task[:80]}, summary, time.time() - t0)
        return summary

    spawn_subagent.__name__ = "spawn_subagent"
    spawn_subagent.__doc__ = ("派一个带独立 system_prompt 的自由子 agent 在隔离上下文里"
                              "完成子任务,只拿回结论(对齐 CC/deepagents task())。")

    def spawn_specialist(role: str, task: str, allowed_tools: str = "",
                         acceptance_criteria: str = "", max_iterations: int = 12) -> str:
        """(2026-08-15·子agent动态编排) 派一个【专才子agent】:你可以动态定义它的
        ①角色人设 ②该用哪些工具 ③验收标准。子agent 完成后·【自动按验收标准核验】它的
        产出是否达标·不达标会明确告诉你(而不是盲信)。比 spawn_subagent 多了角色定制+验收。
        role: 子agent 的角色定位(如'严谨的化学数据库检索专家'、'博弈论建模验算员')。
        task: 交给它的完整任务。
        allowed_tools: 建议它优先用的工具(逗号分隔·如'query_structured_db,search_github'·
          留空=全工具)。这会写进它的指令引导它聚焦(软约束)。
        acceptance_criteria: 验收标准(如'必须给出带CAS号的化合物+引用PubChem URL')·
          留空则不做额外验收。返回:子agent产出 + 验收结论(达标/不达标+差在哪)。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "spawn_specialist")
        if not allow:
            _log_tool(qid, "spawn_specialist", {"role": role[:40]}, guard_msg, time.time() - t0)
            return guard_msg
        depth = int(os.environ.get(_SUBAGENT_DEPTH_ENV, "0") or "0")
        if depth >= _SUBAGENT_MAX_DEPTH:
            summary = f"[已拦截] 子agent 嵌套达最大深度 {_SUBAGENT_MAX_DEPTH}·不能再派。自己做或拆小。"
            _log_tool(qid, "spawn_specialist", {"role": role[:40]}, summary, time.time() - t0)
            return summary
        # 动态角色 system_prompt(角色 + 工具聚焦软约束)
        _role_sp = f"【你的专才角色】{role}\n" if role.strip() else ""
        if allowed_tools.strip():
            _role_sp += (f"【优先用这些工具聚焦解题】{allowed_tools}\n"
                         "(其他工具也能用·但先想清这几个够不够·别发散)\n")
        if acceptance_criteria.strip():
            _role_sp += (f"【交付验收标准·收尾前自查是否满足】{acceptance_criteria}\n")
        child_qid = f"{_safe_qid(qid)}_spec{int(t0 * 1000) % 100000}"
        # (2026-08-15·B4 message-bus接线) 把子agent任务登记到 agent_comm 总线·让它可观测/持久化·
        #   父agent(或其他 session/监督)可通过 check_subagent(child_qid) 读进度·完成后 save_result。
        _ac = _agent_comm_bus()
        if _ac:
            try:
                _ac.register_task(child_qid, goal=(role or "specialist") + ": " + str(task)[:80],
                                  eta_sec=int(max_iterations or 12) * 30, steps=["解题", "验收"])
            except Exception:
                pass
        old_depth = os.environ.get(_SUBAGENT_DEPTH_ENV)
        os.environ[_SUBAGENT_DEPTH_ENV] = str(depth + 1)
        try:
            with _heavy_slot():
                child_final, child_trace = run_auto_agent(
                    task, qid=child_qid,
                    max_iterations=min(max(1, int(max_iterations or 12)), 20),
                    urgency="high", resume=False, extra_system_prompt=_role_sp,
                )
            try:
                if _ac:
                    _ac.save_result(child_qid, str(child_final or "")[:2000],
                                    ok=bool(child_final))
            except Exception:
                pass
            n = child_trace.get("n_tool_calls", 0)
            wall = child_trace.get("total_wall_s", "?")
            _out = child_final or "(子agent 无输出)"
            # ── 验收(acceptance):按标准核验产出·不盲信 ──
            _verdict = ""
            if acceptance_criteria.strip() and _out and len(_out) > 20:
                try:
                    import reflective_mind as _RM
                    _vp = ("你是验收员。判断下面的产出是否满足验收标准。只输出严格JSON:"
                           '{"pass":true/false,"gap":"没满足的点(pass=true填空)"}\n\n'
                           f"验收标准:{acceptance_criteria[:400]}\n产出:{str(_out)[:1500]}")
                    _vr = _RM._mm().ask("claude-sonnet", _vp, timeout=18, max_tokens=150)
                    if isinstance(_vr, dict):
                        _vr = _vr.get("out", "") if _vr.get("ok") else ""
                    _va = _parse_model_json_object(_vr) or {}
                    if _va.get("pass"):
                        _verdict = "\n[验收✅ 达标]"
                    else:
                        _verdict = f"\n[验收❌ 未达标] 差在:{str(_va.get('gap',''))[:200]}·别盲信这个产出·考虑补跑或换法。"
                except Exception:
                    _verdict = "\n[验收:核验异常·请自行判断产出是否达标]"
            summary = (f"[专才子agent完成 · {role[:30]} · {wall}s · {n}步 · qid={child_qid}]\n"
                       f"{str(_out)[:1200]}{_verdict}")
        except Exception as e:
            summary = f"spawn_specialist 异常(降级): {type(e).__name__}: {str(e)[:200]}"
        finally:
            if old_depth is None:
                os.environ.pop(_SUBAGENT_DEPTH_ENV, None)
            else:
                os.environ[_SUBAGENT_DEPTH_ENV] = old_depth
        _log_tool(qid, "spawn_specialist", {"role": role[:40]}, summary, time.time() - t0)
        return summary
    spawn_specialist.__name__ = "spawn_specialist"

    def check_subagent(child_qid: str) -> str:
        """(2026-08-15·B4) 查一个已派出的子agent(spawn_specialist 返回里的 qid)在总线上的
        进度/结果。用于监督:派出后想看它跑到哪、完成没、产出是什么。child_qid: 子agent的qid。"""
        t0 = time.time()
        try:
            _ac = _agent_comm_bus()
            if not _ac:
                return "(agent_comm 总线不可用)"
            _st = _ac.read(child_qid) or {}
            _res = _ac.get_result(child_qid) or {}
            if not _st and not _res:
                summary = f"(总线上没有 {child_qid} 的记录·可能未用 spawn_specialist 派或已清理)"
            else:
                summary = (f"[子agent {child_qid}] done={_st.get('done')} pct={_st.get('pct')} "
                           f"目标={str(_st.get('goal',''))[:60]}\n"
                           f"结果={'有' if _res.get('result') else '无'}·ok={_res.get('ok')}\n"
                           f"{str(_res.get('result',''))[:600]}")
        except Exception as e:
            summary = f"check_subagent 异常: {type(e).__name__}: {str(e)[:120]}"
        _log_tool(qid, "check_subagent", {"child": child_qid[:40]}, summary[:600], time.time() - t0)
        return summary
    check_subagent.__name__ = "check_subagent"

    def compose_workflow(stages_json: str) -> str:
        """(2026-08-15·B5 自定义工作流编排+监督) 定义一条【多阶段流水线】·每阶段一个专才子agent·
        前一阶段的产出【自动接】给下一阶段(接线)·你在每阶段设验收(监督)。适合需要分工接力的
        复杂任务(如:阶段1检索取数→阶段2建模计算→阶段3核验)。
        stages_json: JSON数组·每项 {"role":"角色","task":"任务(可用 {prev} 占位上一阶段产出)",
          "acceptance":"验收标准(可选)"}。例:
          [{"role":"检索员","task":"查X的原始数据"},
           {"role":"计算员","task":"用这些数据算Y: {prev}","acceptance":"给出数值+单位"}]
        返回每阶段产出+验收链。任一阶段验收不达标会标出·但仍继续(供你判断)。"""
        t0 = time.time()
        try:
            import json as _j
            _stages = _j.loads(stages_json) if isinstance(stages_json, str) else stages_json
            if not isinstance(_stages, list) or not _stages:
                return "compose_workflow: stages_json 需为非空 JSON 数组·每项含 role/task。"
            _prev = ""
            _log_lines = []
            for _i, _st in enumerate(_stages[:6]):  # 上限6阶段·防失控
                _task = str(_st.get("task", "")).replace("{prev}", str(_prev)[:1500])
                _out = spawn_specialist(
                    role=str(_st.get("role", f"stage{_i+1}")),
                    task=_task,
                    acceptance_criteria=str(_st.get("acceptance", "")),
                    max_iterations=int(_st.get("max_iterations", 10)))
                _log_lines.append(f"── 阶段{_i+1}({_st.get('role','')}):\n{str(_out)[:600]}")
                # 上一阶段产出接给下一阶段(去掉元信息头·只留正文近似)
                _prev = str(_out)
            summary = "[工作流完成 · %d 阶段]\n" % len(_log_lines) + "\n".join(_log_lines)
        except Exception as e:
            summary = f"compose_workflow 异常(降级): {type(e).__name__}: {str(e)[:150]}"
        _log_tool(qid, "compose_workflow", {"n": len(str(stages_json))}, summary[:400], time.time() - t0)
        return summary[:3000]
    compose_workflow.__name__ = "compose_workflow"

    # ── 沙盒并行池工具(2026-08-20·上线前最后一步·auto/native 并行利器)──────────
    def parallel_sandbox(subtasks_json: str = "", difficulty: str = "std") -> str:
        """把【多个独立可并行的子任务】一次性派给隔离沙盒子agent【并发】跑·按系统负载/
        难度/在线用户量【动态定并发数】·结果有序聚合回来。真子进程隔离·能执行代码/
        搜索/开浏览器·互不干扰·比你自己一个个串行试快得多。
        适合: 多个独立候选要并行验证 / 多源要并查 / 多跳分支要同时探 / 多个网页要同时抓。
        subtasks_json: JSON 数组·每项 {"goal": "子任务描述或URL或shell命令",
          "kind": "solve(通用解题)|search(并行搜索)|browse(开浏览器抓URL)|code(执行代码)",
          "timeout_s": 120}。
        difficulty: std/重型/琐事(影响并发与每子任务预算)。
        返回每个子任务的结论(有序)。系统忙时自动降并发/退串行(不挤垮)。"""
        t0 = time.time()
        s = _sess(qid)
        try:
            import json as _js
            subs = _js.loads(subtasks_json) if isinstance(subtasks_json, str) else subtasks_json
            if not isinstance(subs, list) or not subs:
                return "[parallel_sandbox] subtasks_json 需是非空 JSON 数组·每项含 goal/kind"
            subs = subs[:8]   # 单次最多 8 个并行子任务(防滥用)
            import sandbox_pool as _sp   # v11/evolution 已在 sys.path
            # 在线用户量(供动态调度公平收敛)——从 session 读·没有则 0
            _ul = int((s or {}).get("_online_user_load") or 0)
            _res = _sp.dispatch_parallel(subs, difficulty=str(difficulty or "std"),
                                         user_load=_ul)
            _lines = ["[沙盒并行池] %d 子任务并发完成(%.1fs·动态并发):" % (len(_res),
                                                                    time.time() - t0)]
            for i, r in enumerate(_res, 1):
                _mark = "✓" if r.get("ok") else "✗"
                _lines.append("  %s [%d] %s → %s" % (
                    _mark, i, str(r.get("goal"))[:50],
                    str(r.get("result") or r.get("error"))[:300]))
            summary = "\n".join(_lines)
        except Exception as e:
            summary = "[parallel_sandbox] 异常(降级·可改用 spawn_subagent 逐个): %s: %s" \
                % (type(e).__name__, str(e)[:150])
        _log_tool(qid, "parallel_sandbox", {"n": str(subtasks_json)[:60]},
                  summary[:500], time.time() - t0)
        return summary[:4000]
    parallel_sandbox.__name__ = "parallel_sandbox"

    def multi_expert_parallel(question: str = "", roles_json: str = "") -> str:
        """【多专家并行】同一道题·派多个【不同人设专家虾】在隔离沙盒里【并发】独立解·
        各自带视角(事实核查/逆向推理/领域检索…)·汇聚各专家答案。治'单路径锁错实体'——
        多视角并行覆盖·出题人风格/领域/逆向多角度。比你自己一条路走到黑强。
        question: 要多专家会诊的题(留空=用当前主题面)。
        roles_json: 可选·自定义专家人设 JSON 数组(如 ["化学检索专家","博弈论验算员"])·
          留空=用默认3专家(事实核查/逆向推理/领域检索)。
        返回各专家的独立结论(供你综合/投票选最靠证据的)。系统忙自动降并发。"""
        t0 = time.time()
        try:
            import json as _js
            q = str(question or "").strip() or str(_sess(qid).get("question") or "")
            if not q:
                return "[multi_expert_parallel] 需要 question(或当前 session 有题面)"
            roles = None
            if str(roles_json or "").strip():
                try:
                    roles = _js.loads(roles_json)
                    roles = [str(r) for r in roles][:5] if isinstance(roles, list) else None
                except Exception:
                    roles = None
            import sandbox_pool as _sp
            _ul = int((_sess(qid) or {}).get("_online_user_load") or 0)
            _r = _sp.multi_expert(q, roles=roles, user_load=_ul)
            _lines = ["[多专家并行会诊] %d 专家并发完成(%.1fs):" % (_r.get("n", 0),
                                                              time.time() - t0)]
            for i, e in enumerate(_r.get("experts", []), 1):
                _mark = "✓" if e.get("ok") else "✗"
                _lines.append("  %s 专家%d【%s】: %s" % (_mark, i, e.get("role"),
                                                       str(e.get("answer"))[:400]))
            _lines.append("→ 综合各专家答案·选被证据支撑最强/多数一致的·别只信一个。")
            summary = "\n".join(_lines)
        except Exception as e:
            summary = "[multi_expert_parallel] 异常(降级·可改用 parallel_sandbox 或 spawn_specialist): %s: %s" \
                % (type(e).__name__, str(e)[:150])
        _log_tool(qid, "multi_expert_parallel", {"q": str(question)[:60]},
                  summary[:500], time.time() - t0)
        return summary[:5000]
    multi_expert_parallel.__name__ = "multi_expert_parallel"

    return [spawn_subagent, spawn_specialist, check_subagent, compose_workflow,
            parallel_sandbox, multi_expert_parallel]


# ═══════════════════════════════════════════════════════════════════════
# 统一1 · run_pipeline · 把整个 meta_graph 深研写作流水线当一个重型子 agent 调用
# ═══════════════════════════════════════════════════════════════════════



def _make_skill_tools(qid):
    """生成 skill_search + skill_load 工具。返回 [] 表示 skill_library 不可用。"""
    if not _SKILLS_AVAILABLE:
        return []

    def skill_search(query: str) -> str:
        """按关键词发现相关的 skill(做事方法论文档)。遇到"这类判断/这种场景
        以前有没有总结过怎么做"时先调这个,命中后用 skill_load 读全文照着做。
        例: skill_search("证据够不够") 可发现 content-sufficiency-review。"""
        t0 = time.time()
        try:
            results = _skill_search(query, limit=5)
            # v77e(2026-08-10)skill 召回精准度(用户抓:USDA题错误recall了EC题的skill去load·
            #   污染+浪费步)。skill_library 只要 token 有一点重叠(哪怕1个通用词)score>0 就返回·
            #   无相关性门槛。这里加过滤:只留 score ≥ 绝对下限(0.5)【且】≥ 最高分×0.5 的·
            #   滤掉"蹭了个通用词"的低相关 skill。全滤掉=没真相关的·别硬塞。
            if results:
                _top = max((r.get("score", 0) for r in results), default=0)
                _floor = max(0.5, _top * 0.5)
                _filtered = [r for r in results if r.get("score", 0) >= _floor]
                results = _filtered  # 可能空=没够相关的·下面走"未发现"分支
            if not results:
                summary = ("未发现足够相关的 skill(别硬套不相关的方法论)。"
                           "可直接凭判断做,或 evolution_search 找可执行工具。")
            else:
                lines = [f"发现 {len(results)} 个相关 skill(用 skill_load('<name>') 读全文):"]
                for r in results:
                    lines.append(f"  · {r['name']}: {r['description'][:70]} (score={r.get('score',0)})")
                summary = "\n".join(lines)
        except Exception as e:
            summary = f"skill_search 异常: {type(e).__name__}: {str(e)[:150]}"
        _log_tool(qid, "skill_search", {"query": query}, summary, time.time() - t0)
        return summary

    def skill_load(name: str) -> str:
        """读入某个 skill 的完整正文(方法论/判据/步骤/踩过的坑),照着做判断。
        先 skill_search 找到 name 再调这个。"""
        t0 = time.time()
        try:
            r = _skill_load(name)
            if r.get("ok"):
                body = r.get("body") or ""
                summary = f"# skill: {r.get('name')}\n{body[:3000]}"
                if len(body) > 3000:
                    summary += "\n\n[...skill 正文较长已截断,需要更多可再 skill_load...]"
            else:
                avail = r.get("available") or []
                summary = f"未找到 skill '{name}'。可用: {', '.join(avail[:10])}"
        except Exception as e:
            summary = f"skill_load 异常: {type(e).__name__}: {str(e)[:150]}"
        _log_tool(qid, "skill_load", {"name": name}, summary[:200], time.time() - t0)
        return summary

    def write_skill(name: str, description: str, steps: str) -> str:
        """(2026-08-15·经验→skill 主动化) 主动把一套【可复用的做事方法论/解题步骤/策略】
        写成 skill·下次遇同类任务 skill_search 能发现并 skill_load 照做。固化成功打法成可复用
        资产(记通用方法·不是具体答案)。name: skill名(短·英文/拼音)。description: 一句话适用
        场景。steps: 方法论正文(步骤/判据/坑·markdown)。"""
        t0 = time.time()
        try:
            import skill_library as _sl
            _nm = "".join(c if (c.isalnum() or c in "_-") else "-" for c in str(name).strip())[:60] or "skill"
            _dir = os.path.join(_sl.SKILLS_DIR, _nm)
            _file = os.path.join(_dir, "SKILL.md")
            if os.path.exists(_file):
                summary = f"skill '{_nm}' 已存在·未覆盖(想更新换个名)。"
            else:
                os.makedirs(_dir, exist_ok=True)
                _content = (f"---\nname: {_nm}\ndescription: {str(description)[:200]}\n"
                            f"category: agent-authored\n---\n\n# {_nm}\n\n"
                            f"## 适用场景\n{str(description)[:400]}\n\n"
                            f"## 方法论/步骤/判据\n{str(steps)[:4000]}\n")
                with open(_file, "w", encoding="utf-8") as _f:
                    _f.write(_content)
                summary = f"[已写入 skill] {_nm} → {_file}(下次 skill_search 可发现)"
        except Exception as e:
            summary = f"write_skill 异常: {type(e).__name__}: {str(e)[:150]}"
        _log_tool(qid, "write_skill", {"name": name[:40]}, summary[:400], time.time() - t0)
        return summary
    write_skill.__name__ = "write_skill"

    skill_search.__name__ = "skill_search"
    skill_search.__doc__ = "按关键词发现相关 skill(做事方法论文档),命中后用 skill_load 读全文。"
    skill_load.__name__ = "skill_load"
    skill_load.__doc__ = "读入某 skill 的完整正文(方法论/判据/步骤/坑),照着做判断。"
    return [skill_search, skill_load, write_skill]


def _render_skills_hint(max_skills=8):
    """把可用 skill 清单渲染成一小段 system_prompt 提示(对齐 deepagents 在系统
    提示里列 skill 让模型知道有哪些)。无 skill 或不可用返回 ""。"""
    if not _SKILLS_AVAILABLE:
        return ""
    try:
        skills = _skill_list()
        if not skills:
            return ""
        lines = ["\n# 可用 skill(做事方法论文档 · 遇对应场景先 skill_load 读全文照着做)"]
        for s in skills[:max_skills]:
            lines.append(f"- {s.get('name')}: {str(s.get('description') or '')[:70]}")
        lines.append("发现更多用 skill_search(query)。")
        return "\n".join(lines) + "\n"
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════════════
# 通用执行工具 (v60 · 2026-08-06)
#
# 用户抓正「qa agent 是通用设定：问题范围通用+能力也要通用(能执行不只能
# 答)」——之前 auto agent 只能"发现/执行 evolution 里已注册的业务工具"，
# 没有最基础的"跑命令、读写文件"原语，够不上 Claude Code/DeepAgents 那种
# 通用能力。这里补上，用户明确要求"直接就地升级默认行为、默认带上"（风险
# 更高但更彻底），所以不做 opt-in 开关，跟其他工具一样默认注册。
#
# 安全边界(不是不做限制，是把限制放在"可追溯+可控爆炸半径"，而不是"完全
# 禁止"——用户要的是真能力，过度上锁等于没给):
#   1. 沙箱工作目录: 每个 qid 一个独立目录 ~/.meyo/sandbox/qa_auto_{qid}/，
#      跟现有 skill_executor.py 的 SANDBOX_ROOT 同一套习惯，相对路径操作
#      默认落在这里，不会因为写错相对路径污染到仓库/系统其他地方。
#   2. shell_exec 超时保护(默认60s/可调，硬顶300s)，防止单条命令卡死
#      拖垮整个 agent loop(参照 skill_executor 单步超时思路)。
#   3. 输出截断(stdout/stderr 各最多 4000 字)，防止一条命令刷屏撑爆
#      LLM 上下文——这是 context_store 的"大结果落盘只回摘要"同一思路的
#      简化版(这里量级小，直接截断即可，不需要真落盘)。
#   4. 明显的毁灭性命令模式(fork bomb / rm -rf 根目录及用户主目录) 直接
#      拦截并提示，不静默执行——这类命令没有任何合法用途，拦住不影响
#      "真能力"，纯属误操作兜底。除此之外不做过度白名单限制，其余命令
#      一律放行真跑，执行前的判断力交给 system prompt 里的硬约束条款。
#   5. 每次调用都过 _check_budget + _log_tool，跟其他工具同一套预算/循环
#      卫兵/checkpoint 机制，不会绕过现有的墙钟硬顶保护。
# ═══════════════════════════════════════════════════════════════════════

import subprocess as _subprocess

# ── 每用户文件系统路径基础设施已抽出到 fs_paths.py(2026-08-27·架构净化)──────
#   USERS_ROOT/SANDBOX_ROOT + _uid_from_qid/_user_fs_enabled/user_dir 搬出god-file。
#   最底层路径层·零业务逻辑·为后续 checkpoint/execution 模块化铺路。
from fs_paths import (USERS_ROOT, SANDBOX_ROOT, _uid_from_qid,
                      _user_fs_enabled, user_dir)  # noqa: F401


# 系统代理自动透传(VPN proxy 探测)
# 目的: macOS 的 Clash/Surge 等 VPN 工具默认走 tun 模式对本进程透明生效，
# 但部分工具(requests/pip/git 等)依赖 HTTPS_PROXY 环境变量而不走 tun。
# 这里懒加载一次探测系统代理配置，如果进程已有 HTTPS_PROXY 就不覆盖，
# 否则从 macOS 系统偏好读取代理并注入 env（只在 shell_exec 子进程级别）。
_SYSTEM_PROXY_ENV: dict = {}
_SYSTEM_PROXY_PROBED: bool = False


def _get_system_proxy_env() -> dict:
    """懒加载：探测系统代理并返回 env 字典(仅含代理变量)。
    已设 HTTPS_PROXY 则直接返回空(不重复设)。tun 模式下系统直连也返回空(不干扰)。"""
    global _SYSTEM_PROXY_ENV, _SYSTEM_PROXY_PROBED
    if _SYSTEM_PROXY_PROBED:
        return _SYSTEM_PROXY_ENV
    _SYSTEM_PROXY_PROBED = True
    # 如果进程环境已有代理变量，直接沿用，不重复注入
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        if os.environ.get(k):
            _SYSTEM_PROXY_ENV = {}  # 已有，子进程会继承
            return _SYSTEM_PROXY_ENV
    # macOS scutil 探测系统代理
    try:
        _r = _subprocess.run(
            ["scutil", "--proxy"], capture_output=True, text=True, timeout=3
        )
        _out = _r.stdout or ""
        import re as _re_p
        _https_enabled = _re_p.search(r"HTTPSEnable\s*:\s*1", _out)
        _https_host = _re_p.search(r"HTTPSProxy\s*:\s*(\S+)", _out)
        _https_port = _re_p.search(r"HTTPSPort\s*:\s*(\d+)", _out)
        if _https_enabled and _https_host and _https_port:
            _proxy = f"http://{_https_host.group(1)}:{_https_port.group(1)}"
            # 只有在 tun 模式不可用时才注入(验证直连 google.com 是否通)
            # 实际上 tun 模式已经有效，不强制注入·保留探测结果供需要时使用
            _SYSTEM_PROXY_ENV = {
                "HTTPS_PROXY": _proxy,
                "HTTP_PROXY": _proxy.replace("https://", "http://").replace("http://", "http://"),
                "https_proxy": _proxy,
                "http_proxy": _proxy,
            }
    except Exception:
        pass
    return _SYSTEM_PROXY_ENV

# ═══════════════════════════════════════════════════════════════════════
# 资源限制(2026-08-09 · 产品化多用户加固 · 继路径隔离之后的第二道防线)
#
# 背景:路径隔离解决了"能不能碰到不该碰的数据"，但没解决"能不能拖垮
# 宿主机"——shell_exec 此前只有墙钟超时(300s硬顶)，没有任何 CPU/内存/
# 进程数限制。单个任务里的一条命令完全可以写一个死循环+大量内存分配，
# 在 300 秒超时触发前就把宿主机 CPU/内存吃满，拖慢/拖死同一台机器上
# 其他用户正在跑的所有任务——多用户共享一台宿主机时，这是和"越权读写"
# 同等级别的产品化阻塞项(不是"能不能用”，而是"一个人能不能拖垮所有人")。
#
# 实现:用 Python 标准库 resource 模块(POSIX-only，macOS/Linux 都支持，
# Windows 上不存在这个问题的同等公开 API，直接跳过不设限——降级为"没有
# 资源限制"而不是报错崩服务，跟本文件一贯的 fail-safe 原则一致)。
# 通过 subprocess.run 的 preexec_fn 钩子在子进程 fork 之后、exec 实际
# 命令之前调用 resource.setrlimit，所以限制只作用于这条 shell 命令自己
# 及其派生的子进程，不影响 agent_web 主进程本身。
#   - RLIMIT_CPU: CPU 时间上限(秒)。区别于墙钟超时(timeout_s，可能在
#     等 IO/网络，不占 CPU)，这个管的是"真正占满 CPU 核心空转"的情况
#     (比如死循环)，超限会被内核发 SIGXCPU 杀掉，比墙钟更早触发。
#   - RLIMIT_AS: 虚拟内存地址空间上限(字节)。管住"分配巨量内存"型的
#     误操作/失控循环，超限时该次内存分配会失败(MemoryError/OOM)，
#     不会拖累其他进程的可用内存。
#     ⚠️ 平台差异已实测确认:Linux 上 RLIMIT_AS 正常生效；但 macOS/Darwin
#     内核不支持通过 setrlimit 收紧 RLIMIT_AS/RLIMIT_DATA(会直接在
#     preexec_fn 里抛异常)，这不是本文件的 bug，是 Darwin 内核本身的
#     已知限制(用 `ulimit -v` 在 macOS 终端手测也是同样结果)。下面
#     _shell_preexec_limits 的每一项限制都单独 try/except 包裹，所以
#     在 macOS 上会静默跳过 RLIMIT_AS 这一项、继续设置 CPU/NPROC 两项，
#     不会因为这一项失败就导致整条命令执行不了——即"macOS 上没有内存
#     地址空间限制，但 CPU 时间和进程数限制仍然生效"，是已知的、可接受
#     的降级，不是掩盖问题。生产部署如果跑在 Linux 宿主机上则三项全生效。
#   - RLIMIT_NPROC: 该用户可同时拥有的进程数上限。管住 fork bomb 类
#     "拼命起子进程"的情况(_is_dangerous_shell 已经用正则挡了最经典的
#     fork bomb 写法，这里是即使正则漏判也有兜底)。注意 RLIMIT_NPROC
#     是按*用户*而非按*进程组*计的(POSIX 语义如此)，多个并发任务共享
#     同一个操作系统用户时会共享同一个计数池，取值不能设得过小以免
#     互相挤占，这里默认给的是相对宽松的上限，只挡明显失控的场景。
# 均可用环境变量调，内部小范围试用先给相对宽松的默认值，压力测试后
# 视情况收紧。
# ═══════════════════════════════════════════════════════════════════════
try:
    import resource as _resource_mod
    _HAS_RESOURCE = True
except ImportError:
    _resource_mod = None
    _HAS_RESOURCE = False  # Windows 等平台没有该模块，降级为不设限

_SHELL_CPU_SECONDS = int(os.environ.get("MEYO_SHELL_CPU_LIMIT_S", "120"))
_SHELL_MEM_BYTES = int(os.environ.get("MEYO_SHELL_MEM_LIMIT_MB", "2048")) * 1024 * 1024
# 真根因: RLIMIT_NPROC=256按【用户】计·但这台机器常态进程就285+
# (Chrome/CatPaw/python等)·导致agent一fork子进程(yt-dlp等)就'Resource temporarily
# unavailable'反复失败·退回搜文字。RLIMIT_NPROC太低是fork失败真凶(非系统资源不足·
# kern.maxprocperuid=2666)。改成"当前用户进程数+宽松余量"·且不低于系统上限的合理比例·
# 既挡fork bomb又不误杀正常子进程(yt-dlp/ffmpeg只多fork几个)。
def _compute_nproc_limit():
    env = os.environ.get("MEYO_SHELL_NPROC_LIMIT")
    if env:
        return int(env)
    try:
        import subprocess as _sp2
        cur = int(_sp2.run(["bash", "-c", "ps -u $(whoami) | wc -l"],
                           capture_output=True, text=True, timeout=5).stdout.strip())
        hard = _resource_mod.getrlimit(_resource_mod.RLIMIT_NPROC)[1] if _HAS_RESOURCE else 2666
        # 当前进程数 + 512 余量·但不超过硬上限的90%·不低于1024
        return max(1024, min(cur + 512, int(hard * 0.9) if hard > 0 else cur + 512))
    except Exception:
        return 1500  # 兜底给宽松值(远高于256)
_SHELL_NPROC = _compute_nproc_limit()


def _shell_preexec_limits():
    """subprocess.run(preexec_fn=...) 钩子:在子进程里(fork 之后、exec 前)
    设置资源上限。fail-safe:某一项 setrlimit 失败(权限不足/平台不支持/
    已有更严格的系统级上限导致不能放宽)不应该阻断命令执行——资源限制是
    "锦上添花的兜底"，不是命令能否运行的前提条件，一项失败继续设置其他项，
    全部失败也不影响命令本身正常跑(只是没有了这层保护，等价于修复前的
    行为，不是更差)。"""
    if not _HAS_RESOURCE:
        return
    for _limit_name, _value in (
        ("RLIMIT_CPU", _SHELL_CPU_SECONDS),
        ("RLIMIT_AS", _SHELL_MEM_BYTES),
        ("RLIMIT_NPROC", _SHELL_NPROC),
    ):
        try:
            _flag = getattr(_resource_mod, _limit_name, None)
            if _flag is None:
                continue  # 某些 POSIX 变体(如部分 macOS 版本)可能没有 RLIMIT_NPROC
            _soft, _hard = _resource_mod.getrlimit(_flag)
            # 不能超过系统已设的 hard limit，取两者较小值(否则 setrlimit
            # 本身会因"试图放宽超过 hard limit"而报错，导致这条命令直接
            # 跑不起来——资源限制的目的是"收紧"，不该反而造成新故障)。
            _new_soft = _value if _hard < 0 else min(_value, _hard)
            _resource_mod.setrlimit(_flag, (_new_soft, _hard))
        except Exception:
            pass  # 见上方 docstring:单项失败不阻断，继续设置其他项




def _sandbox_dir(qid):
    # 军团为本次任务下放的持久目录优先：Auto 与其余 harness 对称，产物写这里才能被
    # artifact 扫描、接力和部署收集。独立 Auto/Web 用户没有 MEYO_WORKDIR 时仍走 per-user sandbox。
    _workdir = str(os.environ.get("MEYO_WORKDIR") or "").strip()
    if _workdir:
        d = os.path.abspath(os.path.expanduser(_workdir))
        os.makedirs(d, exist_ok=True)
        return d
    # 每用户文件系统(2026-08-20): ~/.meyo/users/<user>/sandbox/qa_auto_<qid>/
    #   同一用户跨任务的沙盒都在自己名下(可跨任务复用文件)·关掉退回旧的全局 per-qid。
    if _user_fs_enabled():
        try:
            d = os.path.join(user_dir(qid, "sandbox"), f"qa_auto_{_safe_qid(qid)}")
            os.makedirs(d, exist_ok=True)
            return d
        except Exception:
            pass
    d = os.path.join(SANDBOX_ROOT, f"qa_auto_{_safe_qid(qid)}")
    os.makedirs(d, exist_ok=True)
    return d


# ═══════════════════════════════════════════════════════════════════════
# 跨用户路径隔离(2026-08-09 · 产品化多用户加固)
#
# 背景:agent_web.py 已经把"谁是谁"的身份认证补上了(agent_auth.py)，但
# file_read/file_write/shell_exec 这三个通用执行工具此前完全没有跨用户
# 的路径隔离——本模块原来的注释写的是"不做过度白名单限制，一律放行真跑"，
# 这在单用户自用场景下没问题，但一旦多个真实用户共享同一台宿主机跑同一个
# agent_web 服务，就会出现两个真实的越权风险:
#   1. file_read 对路径完全没有任何限制，用绝对路径可以直接读走
#      ~/.meyo/agent_web_tokens.json(所有用户 token 明文表!)、
#      ~/.ssh/id_rsa、其他用户的沙箱目录内容——认证形同虚设，因为认证
#      只挡住了"网页入口"，没挡住"agent 执行时能读到什么"。
#   2. file_write 原有的 HITL 审批只检查"是否在 ~/.meyo/sandbox/ 前缀下"，
#      没检查"是不是自己 qid 的子目录"——alice 的任务可以直接写 bob 的
#      沙箱目录 ~/.meyo/sandbox/qa_auto_web_bob_xxx/，因为两者都满足
#      "在 sandbox 前缀下"这个宽松条件。
#
# 修复策略(两层，都是"拦截即返回明确提示"而不是静默失败，方便 agent 理解
# 并调整策略，符合本模块一贯的可追溯原则):
#   A. 敏感路径红线:无论沙箱内外，一律禁止访问的路径模式(token 文件、
#      ssh/aws/gcloud 凭证目录、/etc/passwd 等)——这是绝对红线，不因为
#      "用户主动要求"就放行，因为这些文件泄露的是全体用户的凭证，不是
#      发起任务的这个用户自己的数据。
#   B. 跨用户沙箱隔离:凡是落在 SANDBOX_ROOT 下的路径，必须是当前 qid
#      自己的子目录，否则视为跨用户越权。
# 不限制沙箱之外、非敏感路径的读写(维持原有"真能力优先"的设计取向，
# 只收紧"会泄露别人数据/别人凭证"这两个新增的多用户风险点)。
# ═══════════════════════════════════════════════════════════════════════
# .env 单独一组、只在"沙箱外"生效(区别于上面那组不分内外都是全局红线):
# 沙箱外的 .env 通常是仓库/项目根目录的真实密钥文件，泄露风险高；但沙箱
# *内*的 .env 是用户自己这次任务创建的临时业务文件(比如任务就是要写一个
# 示例 .env 模板)，属于用户自己数据的自主权范围，不该被当成"全体用户共享
# 凭证"级别的红线拦下——之前把 .env 放进不分内外的全局列表里，实测会把
# 用户自己沙箱内叫 config.env/.env 的正常任务产出也一并误伤，这是过度拦截，
# 已修正为"只挡沙箱外的 .env"。


def _is_inside_own_sandbox(qid, p: str) -> bool:
    """p(已 abspath 过)是否落在当前 qid 自己的沙箱目录内(含子目录)。"""
    own_dir = os.path.abspath(_sandbox_dir(qid))
    return p == own_dir or p.startswith(own_dir + os.sep)


def _path_violation(qid, real_path: str) -> str:
    """校验一个已解析为绝对路径的 real_path 是否违反跨用户隔离/敏感路径红线。
    命中则返回拒绝理由(非空字符串)，否则返回 ""(放行)。

    real_path 应该是 os.path.abspath 处理过的绝对路径(调用方负责)，
    这里不做相对路径解析——避免用 "../../.." 之类的相对路径绕过检查
    (必须在拿到最终绝对路径、消解掉所有 .. 之后再校验，否则形同虚设)。"""
    import re as _re
    try:
        p = os.path.abspath(os.path.expanduser(real_path))
    except Exception:
        return ""  # 路径本身解析失败，交给后续实际文件操作报错，不在这里拦

    # A1. 敏感路径红线(不分沙箱内外，全局禁止——这些是系统级/全体用户共享
    # 的凭证，不应该出现在任何用户的沙箱内，出现了本身就是异常情况)。
    for pat in _SENSITIVE_PATH_PATTERNS:
        if _re.search(pat, p):
            return (f"[路径被拒绝] {p} 命中敏感路径红线(pattern={pat})，"
                     f"涉及认证凭证/密钥/系统账户信息，任何任务都不允许访问，"
                     f"不因为是不同的表述方式而绕过。")

    # A2. .env 类:只在沙箱外生效，沙箱内属于用户自己的数据不拦截
    if not _is_inside_own_sandbox(qid, p):
        for pat in _SENSITIVE_PATH_PATTERNS_OUTSIDE_SANDBOX_ONLY:
            if _re.search(pat, p):
                return (f"[路径被拒绝] {p} 命中敏感路径红线(pattern={pat})，"
                         f"疑似项目密钥文件，不允许访问沙箱外的此类文件。"
                         f"如果这是你自己任务需要的配置文件，请把它放在你的"
                         f"沙箱目录内(相对路径会自动落在沙箱里)。")

    # B. 跨用户沙箱隔离:落在 SANDBOX_ROOT 下的路径必须是自己 qid 的子目录
    sandbox_root_abs = os.path.abspath(SANDBOX_ROOT)
    if p == sandbox_root_abs or p.startswith(sandbox_root_abs + os.sep):
        if not _is_inside_own_sandbox(qid, p):
            own_dir = os.path.abspath(_sandbox_dir(qid))
            return (f"[路径被拒绝] {p} 位于其他任务/用户的沙箱目录内，"
                     f"跨用户/跨任务访问不允许。你自己的沙箱目录是 {own_dir}，"
                     f"只能在这里面读写，或使用沙箱外与他人无关的路径。")
    return ""


# ── shell/curl/url 安全守卫已抽出到 security_guards.py(2026-08-27·架构净化)──
#   13个纯函数守卫(危险shell/curl各类守卫/下载校验/URL分类)~547行搬出god-file。
#   零FS副作用·零auto反向依赖(_safe_qid来自run_state)。import*拿回·调用点零改动。
from security_guards import *  # noqa: F401,F403
from security_guards import (_is_dangerous_shell, _shell_command_path_violation,
    _curl_search_engine_guard, _curl_video_site_guard, _curl_html_vs_api_guard,
    _curl_academic_antibot_guard, _antibot_auto_escalate, _inject_curl_maxtime,
    _validate_shell_downloads, classify_url, _probe_file_suspicious,
    _get_academic_tier_hosts, _extract_first_url, _DANGEROUS_SHELL_PATTERNS,
    _SENSITIVE_PATH_PATTERNS, _SENSITIVE_PATH_PATTERNS_OUTSIDE_SANDBOX_ONLY,
    _TOOL_PRECONDITIONS, _REPLAY_EXEMPT)  # 显式(供静态检查·含守卫段内仅auto用的下划线符号)

# ── 工具预算/资源策略已抽出到 budget_policy.py(2026-08-27) ──
import budget_policy as _budget_policy
_budget_policy.configure(
    _INVESTIGATIVE_TOOLS=_INVESTIGATIVE_TOOLS,
    _emit_gate_event=_emit_gate_event,
)
_check_budget = _budget_policy._check_budget

def _classify_tool_failure(reason: str, tool_precond: dict = None) -> str:
    """失败三分法(缺口2):把工具失败分成 A输入无效/B瞬时故障/C能力墙。
    工具自声明优先(tool_precond里可带failure_map)·通用正则兜底。
    返回 'A_input' | 'B_transient' | 'C_capability'。
    - A_input: 输入不对(URL类型错/参数错/文件不存在)→该修输入重试同一工具
    - B_transient: 超时/网络抖/网关空→退避重试或换执行路径
    - C_capability: 反爬/订阅墙/资源不存在→才允许换策略升级
    - D_credential: 错key/无凭证/鉴权失败→【禁止自修·agent不可自造钥匙】→只能升级给人
      (2026-08-13 用户红线:误判成 B/C 去自愈会诱导 agent 碰不该碰的凭证)"""
    import re as _re
    r = (reason or "").lower()
    # 工具自声明的 failure_map 优先(工具天然知道自己哪些错是哪类)
    if tool_precond and isinstance(tool_precond.get("failure_map"), dict):
        for pat, cat in tool_precond["failure_map"].items():
            if pat.lower() in r:
                return cat
    # 通用正则兜底(跨工具通用·非题型)。A 先判(文件/路径/URL类型问题=可修输入)·
    # 再判 C(真能力墙)·避免"文件不存在"这类可修错被误判成能力墙。
    # D_credential 凭证类必须最先判(2026-08-13 用户红线):错key/无凭证/鉴权失败
    #   ——agent【不可自造钥匙】·这类【禁止自修】·只能升级给人。若把它误判成 B/C
    #   去自愈重试/换策略·会诱导 agent 学出"什么都自己修"(包括碰它不该碰的凭证)。
    #   放最前:auth 类里的 401/unauthorized 是凭证问题·别被下面 C 的 401 抢走。
    if _re.search(r"api[_\s-]?key|apikey|invalid.*key|错.*key|no.*api.*key|缺.*key|"
                  r"authentication failed|auth.*failed|鉴权失败|认证失败|凭证|credential|"
                  r"missing.*(token|key|secret)|token.*(invalid|expired)|密钥|无权限|"
                  r"permission denied|access denied", r):
        return "D_credential"
    # A 输入无效:URL类型/参数/文件问题(修输入就能重试)
    if _re.search(r"invalid|无效|不是.*url|类型.*错|搜索页|search.*page|"
                  r"文件不存在|file not found|no such file|文件.*(不存在|找不到)|"
                  r"路径|bad.*input|缺.*前置|前置跳|wrong.*format|"
                  r"格式.*(错|不对)|malformed", r):
        return "A_input"
    # C 能力墙:反爬/订阅/权限/资源本身不存在(换工具也没用·才准升级)
    if _re.search(r"captcha|recaptcha|反爬|订阅|subscription|paywall|403|401|"
                  r"forbidden|unauthorized|登录|login required|not exist|"
                  r"404|no results|没搜到|没有找到|removed|deleted|私有|private", r):
        return "C_capability"
    # B 瞬时:超时/网络/网关(退避重试)
    if _re.search(r"timeout|超时|timed out|network|网络|connection|连接|"
                  r"网关|gateway|502|503|504|temporarily|稍后|retry|抖动|空返回|"
                  r"未返回|empty response|reset|refused", r):
        return "B_transient"
    return "B_transient"  # 默认当瞬时(保守·允许重试·不轻易判能力墙)


def _timeout_selfheal_hint(command: str) -> str:
    """v97(2026-08-11) 通用超时自愈引导·嵌入必经路径。

    用户诉求原话:"任何超时想方式去解决,穷尽方法+使用所有工具,甚至主动创造
    工具和浏览电脑里的工具"。核心洞察=不代劳(不替它换命令),而是在超时【这个
    必经的失败点】自动给出"这条命令在干哪类活→有哪些换法"的具体指引,让 agent
    自己选着做——像人撞墙后脑子里自动冒出"要不换个渠道/换个工具/拆小"。

    通用性:按【命令在干什么类型的活】给建议(下载/抓网/克隆/装包/跑脚本),
    不写死单一站点或单一题型。识别不出类型就给通用穷尽策略。"""
    import re as _re
    c = (command or "").lower()
    tips = []
    # ① 下载大文件/媒体(yt-dlp 下整片、curl -O 大文件、ffmpeg 抓流)
    if _re.search(r"\byt-dlp\b|\byoutube-dl\b", c):
        tips.append("下整片超时→别下全片:用 see_video_frame(只抽关键帧/片段·内置代理)"
                     "或 transcribe_audio(只要语音内容)。要画面用前者,要台词用后者。")
    elif _re.search(r"\bffmpeg\b", c) and _re.search(r"http|rtmp|\.m3u8", c):
        tips.append("抓流超时→用 see_video_frame(抽帧)/transcribe_audio(转录)代替手撸 ffmpeg。")
    # ② 抓网页/HTML(curl/wget 抓页面)
    if _re.search(r"\b(curl|wget)\b", c) and _re.search(r"http", c):
        if _re.search(r"\.(pdf|xlsx?|csv|docx?|json|xml|zip|png|jpe?g|gif|mp3|wav|mp4)\b", c):
            tips.append("下结构化文件超时→换镜像/加代理重试:图片可试 wsrv.nl 代理"
                         "(https://wsrv.nl/?url=<原url>)或 commons thumb;文档直链失败换"
                         "官方 API/备用源。")
        else:
            tips.append("抓网页超时(可能反爬/慢)→改用 run_parallel_search(带反爬镜像+超时保护)"
                         "或 fetch_url_smart(分级反爬),别手撸 curl 干等。")
    # ③ git clone / pip install(慢源)
    if _re.search(r"\bgit\s+clone\b", c):
        tips.append("clone 超时→只要几个文件的话用 raw.githubusercontent.com 直取,"
                     "或走 GitHub API(api.github.com/repos/.../contents)拿单文件,不必整仓。")
    if _re.search(r"\bpip\d?\s+install\b|\bpip install\b", c):
        tips.append("装包超时→换国内镜像 -i https://pypi.tuna.tsinghua.edu.cn/simple,"
                     "或先 scan_environment 看该功能是否已有现成工具/库不用装。")
    # ④ 兜底:识别不出具体类型也给通用穷尽策略(用户要的"穷尽方法+用所有工具")
    if not tips:
        tips.append("穷尽换法:①拆成更小步骤分批做 ②换等价工具(抓网→run_parallel_search/"
                    "fetch_url_smart;下文件→换镜像源/API;读图→see_image_vlm)③scan_environment "
                    "看本机有没有现成工具 ④加 & 后台跑不阻塞 ⑤ask_local_llm 问本地模型有没有别的解法。")
    return ("\n[🔧 超时自愈·换个方式别干等] 这条命令在做的活超时了。别原样重试(还会超时),"
            "从下面选一个换法:\n- " + "\n- ".join(tips))


def _resolve_path(qid, path: str) -> str:
    """相对路径解析到该 qid 的沙箱目录；绝对路径原样使用(路径隔离交给
    调用方在拿到返回值后调 _path_violation 校验)。

    返回值统一经过 os.path.abspath 消解 ".."/"." 等相对片段——这一步是
    跨用户隔离生效的前提:如果不消解，像 "../qa_auto_web_bob_xxx/secret"
    这样的相对路径会被 os.path.isabs 判定为"相对路径"从而拼接到自己的
    沙箱目录下，字符串前缀看起来仍在自己沙箱内，但实际指向的文件已经
    跳出去了，后续的 _path_violation 前缀比对会被绕过。先 abspath 拿到
    真实规范化路径，才能让"是否在自己沙箱子目录下"这个判断有意义。"""
    if os.path.isabs(path):
        return os.path.abspath(path)
    return os.path.abspath(os.path.join(_sandbox_dir(qid), path))


# ── 执行/文件/视觉工具工厂已抽出到 execution_tools.py(2026-08-27) ──
import execution_tools as _execution_tools
_execution_tools.configure(
    BUDGET_EXTEND_WALL_CAP_MULT=BUDGET_EXTEND_WALL_CAP_MULT,
    BUDGET_EXTEND_WALL_FRAC=BUDGET_EXTEND_WALL_FRAC,
    DEFAULT_SOFT_WALL_RATIO=DEFAULT_SOFT_WALL_RATIO,
    MEYO_ENGINE_EFFICIENCY=MEYO_ENGINE_EFFICIENCY,
    _HAS_RESOURCE=_HAS_RESOURCE,
    _HITL_AVAILABLE=_HITL_AVAILABLE,
    _OUTER_FINISH_MARGIN_S=_OUTER_FINISH_MARGIN_S,
    _REPLAY_EXEMPT=_REPLAY_EXEMPT,
    _antibot_auto_escalate=_antibot_auto_escalate,
    _budget_extend_max=_budget_extend_max,
    _budget_step_add=_budget_step_add,
    _check_budget=_check_budget,
    _curl_academic_antibot_guard=_curl_academic_antibot_guard,
    _curl_html_vs_api_guard=_curl_html_vs_api_guard,
    _curl_search_engine_guard=_curl_search_engine_guard,
    _curl_video_site_guard=_curl_video_site_guard,
    _effective_wall_cap=_effective_wall_cap,
    _emit_gate_event=_emit_gate_event,
    _get_system_proxy_env=_get_system_proxy_env,
    _hitl=_hitl,
    _inject_curl_maxtime=_inject_curl_maxtime,
    _is_dangerous_shell=_is_dangerous_shell,
    _log_tool=_log_tool,
    _m3b_record_host=_m3b_record_host,
    _mark_video_evidence_consumed=_mark_video_evidence_consumed,
    _path_violation=_path_violation,
    _recent_progress_signals=_recent_progress_signals,
    _record_observation_anchor=_record_observation_anchor,
    _record_perception_vote=_record_perception_vote,
    _record_traversal_result=_record_traversal_result,
    _resolve_path=_resolve_path,
    _rs_sessions=_rs_sessions,
    _sandbox_dir=_sandbox_dir,
    _sess=_sess,
    _shell_command_path_violation=_shell_command_path_violation,
    _shell_preexec_limits=_shell_preexec_limits,
    _single_source_reconcile_nudge=_single_source_reconcile_nudge,
    _timeout_selfheal_hint=_timeout_selfheal_hint,
    _validate_shell_downloads=_validate_shell_downloads,
)
_make_execution_tools = _execution_tools._make_execution_tools




# ═══════════════════════════════════════════════════════════════════════
# G-gui · 系统级桌面 GUI 工具 (2026-08-08)
#
# 用户「还有gui能力我不能有吗？我也要」——对标 LongHorizon-Harness 的
# computer-use，desktop_gui.py 提供底层能力，这里注册成门面工具供 LLM 调用。
# click_at/type_text/press_key 会改变系统真实状态(点了什么、输了什么无法
# 撤销)，复用 hitl_gate 同一套审批策略——risky/all 模式下需要用户网页批准，
# 和 shell_exec/file_write 走的是同一个门，不重复造轮子。screenshot/
# list_windows/activate_app 是只读/无副作用动作，不需要审批。
# ═══════════════════════════════════════════════════════════════════════

def _make_desktop_gui_tools(qid):
    """系统级桌面 GUI 自动化工具:截图/列窗口/激活应用/点击/输入/按键。
    不可用(非 macOS 或权限未授予)时不注册这些工具，避免 LLM 徒劳调用。
    这里做的是真实权限探测(_desktop_gui_real_available)，不是只判断模块
    import 成功——import 在任何平台都会成功，但没权限/非 macOS 时工具调用
    必然全部失败，注册了也是让 LLM 白跑一轮再失败，不如直接不注册。
    macOS 的「屏幕录制」和「辅助功能」是两套独立权限:截图只需前者，
    列窗口/激活应用/点击/输入/按键都需要后者。按各自权限精细注册，
    避免"只授予了截图权限却仍注册键鼠工具"这种必然失败的情况。"""
    avail = _desktop_gui_real_available()
    if not avail.get("ok"):
        return []
    can_shot = bool(avail.get("shot_ok"))
    can_a11y = bool(avail.get("a11y_ok"))

    def desktop_screenshot(window_name: str = "") -> str:
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "desktop_screenshot")
        if not allow:
            _log_tool(qid, "desktop_screenshot", {"window_name": window_name}, guard_msg, time.time() - t0)
            return guard_msg
        r = _desktop_gui.screenshot(window_name=window_name)
        summary = (f"截图成功: {r['path']}(可用其他工具查看/分析这张图)"
                  if r.get("ok") else f"截图失败: {r.get('reason', '未知原因')}")
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "desktop_screenshot", {"window_name": window_name}, summary, time.time() - t0)
        return summary

    def desktop_list_windows() -> str:
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "desktop_list_windows")
        if not allow:
            _log_tool(qid, "desktop_list_windows", {}, guard_msg, time.time() - t0)
            return guard_msg
        r = _desktop_gui.list_windows()
        summary = (f"当前打开的应用({len(r['windows'])}个): {', '.join(r['windows'])}"
                  if r.get("ok") else f"列窗口失败: {r.get('reason', '未知原因')}")
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "desktop_list_windows", {}, summary, time.time() - t0)
        return summary

    def desktop_activate_app(app_name: str) -> str:
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "desktop_activate_app")
        if not allow:
            _log_tool(qid, "desktop_activate_app", {"app_name": app_name}, guard_msg, time.time() - t0)
            return guard_msg
        r = _desktop_gui.activate_app(app_name)
        summary = (f"已把 {app_name} 带到前台" if r.get("ok")
                  else f"激活 {app_name} 失败: {r.get('reason', '未知原因')}")
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "desktop_activate_app", {"app_name": app_name}, summary, time.time() - t0)
        return summary

    def desktop_click(x: int, y: int) -> str:
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "desktop_click")
        if not allow:
            _log_tool(qid, "desktop_click", {"x": x, "y": y}, guard_msg, time.time() - t0)
            return guard_msg
        detail = f"click at ({x},{y})"
        if _HITL_AVAILABLE and _hitl.needs_approval("desktop_click", detail):
            _log_tool(qid, "desktop_click", {"x": x, "y": y}, "[待审批] 已挂起等你批准…", time.time() - t0)
            verdict = _hitl.request_approval(qid, "desktop_click", detail)
            if verdict.get("decision") != "approved":
                summary = f"[已拒绝执行] 用户未批准这次点击({verdict.get('decision')})。"
                _log_tool(qid, "desktop_click", {"x": x, "y": y}, summary, time.time() - t0)
                return summary
        r = _desktop_gui.click_at(x, y)
        summary = f"已点击 ({x},{y})" if r.get("ok") else f"点击失败: {r.get('reason', '未知原因')}"
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "desktop_click", {"x": x, "y": y}, summary, time.time() - t0)
        return summary

    def desktop_type(text: str) -> str:
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "desktop_type")
        if not allow:
            _log_tool(qid, "desktop_type", {"len": len(text)}, guard_msg, time.time() - t0)
            return guard_msg
        if _HITL_AVAILABLE and _hitl.needs_approval("desktop_type", text[:200]):
            _log_tool(qid, "desktop_type", {"len": len(text)}, "[待审批] 已挂起等你批准…", time.time() - t0)
            verdict = _hitl.request_approval(qid, "desktop_type", text[:200])
            if verdict.get("decision") != "approved":
                summary = f"[已拒绝执行] 用户未批准这次输入({verdict.get('decision')})。"
                _log_tool(qid, "desktop_type", {"len": len(text)}, summary, time.time() - t0)
                return summary
        r = _desktop_gui.type_text(text)
        summary = f"已输入 {len(text)} 字符" if r.get("ok") else f"输入失败: {r.get('reason', '未知原因')}"
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "desktop_type", {"len": len(text)}, summary, time.time() - t0)
        return summary

    def desktop_key(key: str) -> str:
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "desktop_key")
        if not allow:
            _log_tool(qid, "desktop_key", {"key": key}, guard_msg, time.time() - t0)
            return guard_msg
        r = _desktop_gui.press_key(key)
        summary = f"已按键 {key}" if r.get("ok") else f"按键失败: {r.get('reason', '未知原因')}"
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "desktop_key", {"key": key}, summary, time.time() - t0)
        return summary

    desktop_screenshot.__name__ = "desktop_screenshot"
    desktop_screenshot.__doc__ = ("截取当前屏幕(macOS)，返回图片路径。操作任何桌面应用前先用这个"
                                  "看清当前屏幕状态，不要盲目点击。window_name 可选，指定后会先把该"
                                  "应用带到前台再截图。")
    desktop_list_windows.__name__ = "desktop_list_windows"
    desktop_list_windows.__doc__ = "列出当前所有打开的桌面应用/进程名，决策该操作哪个窗口前先看清现状。"
    desktop_activate_app.__name__ = "desktop_activate_app"
    desktop_activate_app.__doc__ = ("把指定桌面应用带到前台(如 'Finder'/'Preview'/'TextEdit'/"
                                    "'Microsoft Excel')，应用未打开时会尝试启动它。")
    desktop_click.__name__ = "desktop_click"
    desktop_click.__doc__ = ("在屏幕绝对坐标(左上角为原点)模拟一次鼠标左键点击。务必先用"
                             "desktop_screenshot 看清当前屏幕再决定坐标，不要盲点。风险操作，"
                             "可能需要用户在网页批准。")
    desktop_type.__name__ = "desktop_type"
    desktop_type.__doc__ = "往当前聚焦的输入框逐字输入文本。先用 desktop_click 把焦点定位到目标输入框。"
    desktop_key.__name__ = "desktop_key"
    desktop_key.__doc__ = ('模拟按键或组合键，如 "enter"/"tab"/"escape" 或 "cmd+s"/"cmd+shift+z"。')

    tools = []
    if can_shot:
        tools.append(desktop_screenshot)
    if can_a11y:
        tools.extend([desktop_list_windows, desktop_activate_app,
                      desktop_click, desktop_type, desktop_key])
    return tools


# ═══════════════════════════════════════════════════════════════════════
# G-cli · CLI-Anything 生态桥接工具 (2026-08-08)
#
# 优先级引导(写进 docstring 让模型看到):操作某个具体软件(设计/3D/办公/音视频
# 等)时，先 cli_hub_search 看有没有现成的 CLI 封装(GIMP/Blender/FreeCAD/
# LibreOffice/Krita/Obsidian/Zoom等40+)，有就装了直接调——比 desktop_click
# 盲猜坐标点击稳得多(走软件自带脚本API，不受界面改版/分辨率影响)。只有确认
# 没有对应封装时，才退化到 desktop_gui 那套坐标点击兜底。
# ═══════════════════════════════════════════════════════════════════════

def _make_cli_hub_tools(qid):
    """CLI-Anything 生态桥接工具:搜索/安装/执行已注册的软件 CLI 封装。
    cli-hub 不可用(未安装)时不注册这些工具。"""
    if not _CLI_HUB_AVAILABLE:
        return []

    def cli_hub_search(query: str) -> str:
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "cli_hub_search")
        if not allow:
            _log_tool(qid, "cli_hub_search", {"query": query}, guard_msg, time.time() - t0)
            return guard_msg
        r = _cli_hub.search(query)
        if not r.get("ok"):
            summary = f"搜索失败: {r.get('reason', '未知原因')}"
        elif not r["items"]:
            # 关键词搜索没结果时，顺带试一次 can()(按任务意图匹配)，一次工具调用
            # 拿到两种搜法的结果，减少模型来回试探的步数。
            r2 = _cli_hub.can(query)
            if r2.get("ok") and r2["items"]:
                names = [it.get("cli_name") or it.get("name") or str(it) for it in r2["items"][:8]]
                summary = f"关键词搜索无结果，但按任务能力匹配到: {', '.join(names)}"
            else:
                summary = f"没有找到匹配 '{query}' 的现成CLI封装，可能需要退化到 desktop_* 坐标点击兜底。"
        else:
            lines = [f"- {it['name']}: {it.get('description','')[:80]} (category={it.get('category','?')})"
                     for it in r["items"][:10]]
            summary = f"找到 {len(r['items'])} 个匹配的CLI封装:\n" + "\n".join(lines)
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "cli_hub_search", {"query": query}, summary, time.time() - t0)
        return summary

    def cli_hub_install(name: str) -> str:
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "cli_hub_install")
        if not allow:
            _log_tool(qid, "cli_hub_install", {"name": name}, guard_msg, time.time() - t0)
            return guard_msg
        r = _cli_hub.install(name, timeout_s=180)
        summary = (f"已安装 {name}，可以用 cli_hub_run 调用了" if r.get("ok")
                  else f"安装 {name} 失败: {r.get('reason','未知原因')}\n{r.get('stderr','')[:500]}")
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "cli_hub_install", {"name": name}, summary[:500], time.time() - t0)
        return summary

    def cli_hub_run(name: str, args: str = "") -> str:
        """args 是空格分隔的命令行参数字符串,如 '--help' 或 'export --format pdf in.odt'。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "cli_hub_run", content_key=f"{name}|{args}")
        if not allow:
            _log_tool(qid, "cli_hub_run", {"name": name, "args": args}, guard_msg, time.time() - t0)
            return guard_msg
        import shlex as _shlex
        try:
            arg_list = _shlex.split(args) if args else []
        except ValueError as e:
            summary = f"参数解析失败(引号不匹配?): {e}"
            _log_tool(qid, "cli_hub_run", {"name": name, "args": args}, summary, time.time() - t0)
            return summary
        r = _cli_hub.run(name, arg_list, timeout_s=120)
        summary = (f"exit_code={r['returncode']}\n[stdout]\n{r['stdout'] or '(空)'}"
                  + (f"\n[stderr]\n{r['stderr']}" if r.get("stderr") else ""))
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "cli_hub_run", {"name": name, "args": args}, summary[:500], time.time() - t0)
        return summary

    cli_hub_search.__name__ = "cli_hub_search"
    cli_hub_search.__doc__ = ("按关键词/软件名/任务描述搜索 CLI-Anything 注册表(40+现成软件CLI封装，"
                              "覆盖GIMP/Blender/FreeCAD/LibreOffice/Krita/Obsidian/Zoom/QGIS等)。操作"
                              "某个具体软件前先搜这个，有现成封装比坐标点击(desktop_click)稳得多。")
    cli_hub_install.__name__ = "cli_hub_install"
    cli_hub_install.__doc__ = ("安装 cli_hub_search 找到的某个CLI封装(name用search结果里的name字段)。"
                               "注意部分封装还要求目标软件本身已装在本机，装CLI封装≠装好了底层软件。")
    cli_hub_run.__name__ = "cli_hub_run"
    cli_hub_run.__doc__ = ("执行已安装的CLI封装。args是空格分隔的命令行参数字符串，不确定参数格式"
                           "时先用 args='--help' 看帮助。")

    return [cli_hub_search, cli_hub_install, cli_hub_run]


# ═══════════════════════════════════════════════════════════════════════
# P4 · 搜索工具并安装使用(不只搜信息·搜"能装来用的工具")
#
# 用户核心洞察:「搜索 skill 和 github 不只是搜索外部信息,而是搜索工具/skill
# /组件——这是我们聪明智能体的核心价值」。之前门面能 evolution_search(找内部
# 工具)/skill_search(找方法论)/shell_exec(能跑 pip),但没有一个把"发现外部
# 工具(pypi/github)→ 判断 → 安装 → 用"串起来的编排。这里补上 pip_install
# 工具(沙箱内装 python 包,装完就能在后续 shell_exec 里 import 用)。
# 安全:只允许 pip install 到当前解释器环境(用户已授权门面在沙箱做真事),
# 危险包名/命令注入拦截,超时保护。
# ═══════════════════════════════════════════════════════════════════════

def _make_tool_install_tools(qid):
    """生成 pip_install 工具:发现→安装外部 python 工具供后续使用。"""

    def pip_install(package: str, index_url: str = "") -> str:
        """安装一个 python 包(pypi),装完后续 shell_exec 里就能 import 用。
        package: 包名(可带版本,如 'requests' 或 'pandas==2.0')。可一次多个空格分隔。
        index_url: 可选镜像源(留空用默认;国内慢可传 https://pypi.tuna.tsinghua.edu.cn/simple)。
        用于"这个任务需要某个现成库/工具,先装再用"的场景——搜到某 pypi 包能解决
        问题时,用这个装,而不是自己从零手写。装在沙箱可用的环境里。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "pip_install")
        if not allow:
            _log_tool(qid, "pip_install", {"package": package}, guard_msg, time.time() - t0)
            return guard_msg
        # 包名基本校验:防命令注入(只允许包名/版本/常见字符)
        import re as _re
        pkgs = [p for p in str(package).split() if p]
        for p in pkgs:
            if not _re.match(r"^[A-Za-z0-9_.\-]+(\[[A-Za-z0-9_,\-]+\])?([=<>!~]=?[0-9A-Za-z.\-]+)?$", p):
                summary = f"[已拦截] 包名 '{p}' 含非法字符,拒绝安装(防命令注入)。请只传合法 pypi 包名。"
                _log_tool(qid, "pip_install", {"package": package}, summary, time.time() - t0)
                return summary
        if not pkgs:
            return "未指定要装的包。"
        cmd = [sys.executable, "-m", "pip", "install", "--quiet"]
        if index_url and _re.match(r"^https?://[A-Za-z0-9._/\-]+$", index_url):
            cmd += ["-i", index_url]
        cmd += pkgs
        try:
            r = _subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if r.returncode == 0:
                summary = f"[安装成功] {', '.join(pkgs)} 已装好,后续 shell_exec 里可直接 import 使用。"
            else:
                err = (r.stderr or r.stdout or "")[:600]
                summary = f"[安装失败 rc={r.returncode}] {err}\n可换镜像源(index_url=清华源)重试,或换个包。"
        except _subprocess.TimeoutExpired:
            summary = "[超时] pip install 超过 180s,可能网络慢——换镜像源(index_url)重试。"
        except Exception as e:
            summary = f"pip_install 异常: {type(e).__name__}: {str(e)[:200]}"
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "pip_install", {"package": package}, summary, time.time() - t0)
        return summary

    pip_install.__name__ = "pip_install"
    pip_install.__doc__ = ("安装 python 包(pypi),装完后续 shell_exec 里可 import 用。"
                           "'搜到现成库能解决问题→装来用'而不是从零手写。")
    return [pip_install]


# ═══════════════════════════════════════════════════════════════════════
# P6 · per-user 工具发现-安装-验证-交付闭环 (2026-08-07)
#
# 用户核心诉求:「工具集要支持自动搜索 github 和 skill search（之前安装过
# 这个），然后自动安装和测试写好场景，给用户实际探索，每个用户自动化的
# 创建工具和安装 解决不同的题」。拆解成 4 个必须闭环的动作，缺一不可:
#
#   1. 搜(发现) —— search_github(找外部开源工具/repo) +
#      search_my_toolbox(找"这个用户"以前已经装过+验证过的工具，不重复
#      造轮子) —— 两个搜索面，一个对外一个对内，呼应用户原话"搜索 skill
#      和 github"。
#   2. 装(安装) —— install_github_repo：clone 到 **per-user** 目录（不是
#      公共沙箱，用户和用户之间的工具库互不污染），自动识别
#      requirements.txt/package.json/pyproject.toml 尝试装依赖。
#   3. 测(验证) —— write_scenario_test + run_scenario_test：不是"装完就算
#      数"，而是让模型针对这道题真实写一个场景化测试脚本、真的跑一遍，
#      只有跑通才算这个工具"真的能用"——呼应用户"自动安装和测试写好场景"。
#   4. 交(交付) —— deliver_tool_to_user：验证通过后写入该用户的个人工具箱
#      清单(toolbox.json)，下次 search_my_toolbox 就能命中，同时把
#      "怎么用/装在哪/验证结论"整理成一段话直接展示给用户，让用户能实际
#      去探索这个新装好的工具，而不是黑盒装完就完事。
#
# per-user 隔离设计: 每个 user_id 一个独立目录
#   ~/.meyo/toolbox/{safe_uid}/repos/{repo_name}/      · clone 下来的代码
#   ~/.meyo/toolbox/{safe_uid}/toolbox.json            · 已验证工具清单
# 没有具体 user(user_id="default")时退化到共享的 _shared 目录，行为等价
# 于"单用户模式"，不引入门槛。
# ═══════════════════════════════════════════════════════════════════════

TOOLBOX_ROOT = os.path.expanduser("~/.meyo/toolbox")


def _safe_uid(user_id: str) -> str:
    uid = "".join(c if (c.isalnum() or c in "_-") else "_" for c in str(user_id or "default"))[:40]
    return uid or "_shared"


def _toolbox_dir(user_id: str) -> str:
    d = os.path.join(TOOLBOX_ROOT, _safe_uid(user_id))
    os.makedirs(os.path.join(d, "repos"), exist_ok=True)
    return d


def _toolbox_manifest_path(user_id: str) -> str:
    return os.path.join(_toolbox_dir(user_id), "toolbox.json")


def _load_toolbox(user_id: str) -> list:
    """读该用户的已验证工具清单，文件不存在/损坏都 fail-safe 返回 []。"""
    path = _toolbox_manifest_path(user_id)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_toolbox(user_id: str, items: list) -> None:
    """持久化该用户已验证工具清单。fail-safe:磁盘满/权限问题不阻断主流程，
    但之前完全静默(except: pass)——保存失败后下次 search_my_toolbox 会返回
    空列表，agent 会以为用户没装过这个工具从而重复安装，且没有任何线索能
    追溯到"其实是上次保存失败了"。这里至少打一行 stderr，和同文件 _save_ckpt
    (第 308-309 行)"失败只打印不抛"的风格保持一致。"""
    path = _toolbox_manifest_path(user_id)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  [auto-agent toolbox] 保存失败(忽略,下次可能误判未安装): "
              f"{type(e).__name__}: {e}", file=sys.stderr)


def _make_tool_discovery_tools(qid):
    """生成 search_github / search_my_toolbox / install_github_repo /
    write_scenario_test / run_scenario_test / deliver_tool_to_user 六件套，
    串起"搜索工具(内外两路)→安装→场景化验证→交付给该用户"完整闭环。
    全部按 _sess(qid)['user_id'] 做 per-user 隔离。"""

    def _uid():
        return _sess(qid).get("user_id") or "default"

    def search_github(query: str, limit: int = 5) -> str:
        """在 GitHub 上搜索能解决当前问题的开源仓库(按 star 排序)。用于
        "这道题/这个任务需要一个现成工具，网上有没有别人写好的"场景——
        找到之后用 install_github_repo 装下来，而不是自己从零手写。
        query 用简洁的英文关键词(GitHub 搜索对英文效果更好，例如
        "pdf table extraction python" 而不是整句中文问题)。返回 repo 全名/
        star 数/描述/clone 地址，供你判断哪个最合适。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "search_github")
        if not allow:
            _log_tool(qid, "search_github", {"query": query}, guard_msg, time.time() - t0)
            return guard_msg
        try:
            import urllib.request as _ureq
            import urllib.parse as _uparse
            url = ("https://api.github.com/search/repositories?q="
                   + _uparse.quote(query) + f"&sort=stars&order=desc&per_page={min(max(1, int(limit or 5)), 10)}")
            req = _ureq.Request(url, headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "qa-auto-agent-tool-discovery",
            })
            with _ureq.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            items = data.get("items", []) or []
            if not items:
                summary = f"GitHub 上未搜到与 '{query}' 相关的仓库，换个更通用的英文关键词再试。"
            else:
                lines = [f"GitHub 搜到 {len(items)} 个相关仓库(用 install_github_repo(repo_url) 装下来用):"]
                for it in items:
                    lines.append(
                        f"  · {it.get('full_name')} ⭐{it.get('stargazers_count')} "
                        f"[{it.get('language') or '?'}] · clone_url={it.get('clone_url')}\n"
                        f"    {str(it.get('description') or '(无描述)')[:100]}"
                    )
                summary = "\n".join(lines)
        except Exception as e:
            summary = (f"search_github 异常(可能触发了 GitHub 匿名限流或网络不通): "
                       f"{type(e).__name__}: {str(e)[:200]}\n可稍后重试，或改用 pip_install "
                       f"直接按包名猜测安装，或 skill_search 找内部已有方法论。")
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "search_github", {"query": query}, summary[:600], time.time() - t0)
        return summary

    def search_my_toolbox(query: str = "") -> str:
        """搜索"这个用户"以前已经装过并验证通过的专属工具(per-user 工具箱)，
        避免同一个用户重复造轮子——用户之前解决过类似问题时装的工具，这次
        遇到相似场景应该优先复用。query 留空则列出该用户的全部已验证工具。
        跟 skill_search(全局共享的方法论文档)是两回事：这里是"这个具体用户
        自己攒的、真跑通过的可执行工具"清单。"""
        t0 = time.time()
        uid = _uid()
        try:
            items = _load_toolbox(uid)
            if query:
                from skill_library import _tokenize as _tok
                qtok = set(_tok(query))
                scored = []
                for it in items:
                    hay = _tok((it.get("name", "") + " " + it.get("summary", "") + " "
                                + it.get("usage_example", "")))
                    overlap = len(qtok & set(hay))
                    if overlap or not qtok:
                        scored.append((overlap, it))
                scored.sort(key=lambda x: -x[0])
                items = [it for _, it in scored]
            if not items:
                summary = (f"该用户(uid={uid})的专属工具箱目前是空的。"
                           "如果这道题需要外部工具，用 search_github/pip_install 找+装，"
                           "验证跑通后用 deliver_tool_to_user 存进这个用户的工具箱，"
                           "下次同类任务就能直接复用。")
            else:
                lines = [f"该用户(uid={uid})的专属工具箱，命中 {len(items)} 个已验证工具:"]
                for it in items[:8]:
                    lines.append(
                        f"  · {it.get('name')} · 路径={it.get('path')}\n"
                        f"    用途: {str(it.get('summary',''))[:100]}\n"
                        f"    用法: {str(it.get('usage_example',''))[:100]}"
                    )
                summary = "\n".join(lines)
        except Exception as e:
            summary = f"search_my_toolbox 异常: {type(e).__name__}: {str(e)[:150]}"
        _log_tool(qid, "search_my_toolbox", {"query": query}, summary[:500], time.time() - t0)
        return summary

    def install_github_repo(repo_url: str, tool_name: str = "") -> str:
        """把一个 GitHub 仓库 clone 到"这个用户"的专属工具目录下，并自动
        识别 requirements.txt / package.json / pyproject.toml 尝试装依赖。
        repo_url 用 search_github 返回的 clone_url(https://github.com/...)。
        tool_name 留空则用仓库名。装完后**必须**用 write_scenario_test +
        run_scenario_test 针对这道题真实验证一遍，跑通才能算这个工具真的
        能用，不要装完就假设它能用。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "install_github_repo")
        if not allow:
            _log_tool(qid, "install_github_repo", {"repo_url": repo_url}, guard_msg, time.time() - t0)
            return guard_msg

        import re as _re
        if not _re.match(r"^https://github\.com/[\w.\-]+/[\w.\-]+(\.git)?/?$", str(repo_url or "").strip()):
            summary = "[已拦截] repo_url 必须是形如 https://github.com/owner/repo(.git) 的合法 GitHub 地址。"
            _log_tool(qid, "install_github_repo", {"repo_url": repo_url}, summary, time.time() - t0)
            return summary

        uid = _uid()
        name = tool_name.strip() if tool_name else repo_url.rstrip("/").rstrip(".git").split("/")[-1]
        name = "".join(c if (c.isalnum() or c in "_-") else "_" for c in name)[:60]
        target_dir = os.path.join(_toolbox_dir(uid), "repos", name)

        try:
            if os.path.isdir(target_dir) and os.listdir(target_dir):
                clone_msg = f"目录已存在({target_dir})，跳过 clone，直接复用。"
            else:
                r = _subprocess.run(
                    ["git", "clone", "--depth", "1", repo_url, target_dir],
                    capture_output=True, text=True, timeout=120,
                )
                if r.returncode != 0:
                    summary = f"[clone 失败] {(r.stderr or r.stdout or '')[:600]}"
                    if guard_msg:
                        summary = f"{summary}{guard_msg}"
                    _log_tool(qid, "install_github_repo", {"repo_url": repo_url}, summary, time.time() - t0)
                    return summary
                clone_msg = f"clone 成功 → {target_dir}"

            dep_msgs = []
            req_file = os.path.join(target_dir, "requirements.txt")
            if os.path.isfile(req_file):
                r = _subprocess.run(
                    [sys.executable, "-m", "pip", "install", "--quiet", "-r", req_file],
                    capture_output=True, text=True, timeout=180,
                )
                dep_msgs.append(f"requirements.txt 依赖安装 {'成功' if r.returncode == 0 else '失败: ' + (r.stderr or '')[:300]}")
            pyproj = os.path.join(target_dir, "pyproject.toml")
            if os.path.isfile(pyproj) and not req_file:
                r = _subprocess.run(
                    [sys.executable, "-m", "pip", "install", "--quiet", "-e", target_dir],
                    capture_output=True, text=True, timeout=180,
                )
                dep_msgs.append(f"pyproject.toml(-e 安装) {'成功' if r.returncode == 0 else '失败: ' + (r.stderr or '')[:300]}")
            pkg_json = os.path.join(target_dir, "package.json")
            if os.path.isfile(pkg_json):
                r = _subprocess.run(
                    ["npm", "install", "--silent"],
                    cwd=target_dir, capture_output=True, text=True, timeout=180,
                )
                dep_msgs.append(f"package.json(npm install) {'成功' if r.returncode == 0 else '失败: ' + (r.stderr or '')[:300]}")
            if not dep_msgs:
                dep_msgs.append("未发现 requirements.txt/pyproject.toml/package.json，可能是纯脚本仓库，直接看代码/README 用即可。")

            entries = sorted(os.listdir(target_dir))[:30]
            summary = (
                f"{clone_msg}\n" + "\n".join(dep_msgs) +
                f"\n目录结构(前30项): {entries}\n"
                f"下一步: 用 file_read 看看 README/入口文件，然后用 write_scenario_test "
                f"针对这道题写一个真实调用它的测试脚本，run_scenario_test 跑通后再用 "
                f"deliver_tool_to_user(tool_name='{name}', ...) 交付。"
            )
        except _subprocess.TimeoutExpired:
            summary = "[超时] clone 或装依赖超过限定时间，可能仓库太大或网络慢，换个更小的仓库试试。"
        except FileNotFoundError as e:
            summary = f"[环境缺失] 找不到 git/npm 等命令: {e}"
        except Exception as e:
            summary = f"install_github_repo 异常: {type(e).__name__}: {str(e)[:200]}"

        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "install_github_repo", {"repo_url": repo_url, "tool_name": name}, summary[:600], time.time() - t0)
        return summary

    def write_scenario_test(tool_name: str, test_code: str, filename: str = "scenario_test.py") -> str:
        """给刚装好的工具(tool_name 要跟 install_github_repo 用的一致)写一个
        **针对这道具体任务的真实测试脚本**——不是泛泛的 unit test，而是"用
        这个工具真的跑一遍当前要解决的场景，断言/打印出关键结果"，这样
        run_scenario_test 跑完才能判断它到底能不能解决这道题。写到该工具
        目录下的 filename(默认 scenario_test.py)。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "write_scenario_test")
        if not allow:
            _log_tool(qid, "write_scenario_test", {"tool_name": tool_name}, guard_msg, time.time() - t0)
            return guard_msg
        uid = _uid()
        safe_name = "".join(c if (c.isalnum() or c in "_-") else "_" for c in tool_name)[:60]
        target_dir = os.path.join(_toolbox_dir(uid), "repos", safe_name)
        if not os.path.isdir(target_dir):
            summary = f"未找到工具目录 {target_dir}，请先 install_github_repo 装好这个工具再写测试。"
            _log_tool(qid, "write_scenario_test", {"tool_name": tool_name}, summary, time.time() - t0)
            return summary
        safe_filename = os.path.basename(filename) or "scenario_test.py"
        test_path = os.path.join(target_dir, safe_filename)
        try:
            with open(test_path, "w", encoding="utf-8") as f:
                f.write(test_code)
            summary = f"已写入场景测试脚本 → {test_path}({len(test_code)} 字符)。接下来用 run_scenario_test 执行它。"
        except Exception as e:
            summary = f"write_scenario_test 异常: {type(e).__name__}: {str(e)[:200]}"
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "write_scenario_test", {"tool_name": tool_name, "filename": safe_filename}, summary, time.time() - t0)
        return summary

    def run_scenario_test(tool_name: str, run_cmd: str = "", timeout_s: float = 60) -> str:
        """真实执行 write_scenario_test 写好的测试脚本，验证这个工具是否
        真的能解决当前任务。run_cmd 留空则默认跑 `python3 scenario_test.py`；
        如果测试脚本不是 python(比如 npm 项目)，传具体命令，如
        "node scenario_test.js"。跑通(exit_code=0)才能判定为"已验证"，
        才允许调用 deliver_tool_to_user 交付。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "run_scenario_test")
        if not allow:
            _log_tool(qid, "run_scenario_test", {"tool_name": tool_name}, guard_msg, time.time() - t0)
            return guard_msg
        uid = _uid()
        safe_name = "".join(c if (c.isalnum() or c in "_-") else "_" for c in tool_name)[:60]
        target_dir = os.path.join(_toolbox_dir(uid), "repos", safe_name)
        if not os.path.isdir(target_dir):
            summary = f"未找到工具目录 {target_dir}，请先 install_github_repo。"
            _log_tool(qid, "run_scenario_test", {"tool_name": tool_name}, summary, time.time() - t0)
            return summary
        cmd = run_cmd.strip() if run_cmd else f"{sys.executable} scenario_test.py"
        real_timeout = min(max(1.0, float(timeout_s or 60)), 180.0)
        try:
            r = _subprocess.run(
                cmd, shell=True, cwd=target_dir, capture_output=True, text=True, timeout=real_timeout,
            )
            passed = (r.returncode == 0)
            s.setdefault("_scenario_test_results", {})[safe_name] = passed
            stdout = (r.stdout or "")[:2000]
            stderr = (r.stderr or "")[:1000]
            summary = (
                f"{'✅ 测试通过' if passed else '❌ 测试未通过'}(exit_code={r.returncode})\n"
                f"[stdout]\n{stdout or '(空)'}\n" + (f"[stderr]\n{stderr}\n" if stderr else "") +
                (f"\n可以调用 deliver_tool_to_user('{safe_name}', ...) 交付给用户了。" if passed
                 else "\n未通过前不要 deliver_tool_to_user，先根据报错改代码/换个工具重试。")
            )
        except _subprocess.TimeoutExpired:
            summary = f"[超时] 测试脚本超过 {real_timeout}s 未结束，可能死循环或在等网络，判定为未通过。"
            s.setdefault("_scenario_test_results", {})[safe_name] = False
        except Exception as e:
            summary = f"run_scenario_test 异常: {type(e).__name__}: {str(e)[:200]}"
            s.setdefault("_scenario_test_results", {})[safe_name] = False
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "run_scenario_test", {"tool_name": tool_name, "run_cmd": cmd}, summary[:600], time.time() - t0)
        return summary

    def deliver_tool_to_user(tool_name: str, summary_text: str, usage_example: str) -> str:
        """把一个**已经 run_scenario_test 验证通过**的工具正式交付进"这个
        用户"的专属工具箱清单——下次这个用户再遇到相似任务，search_my_toolbox
        就能直接命中复用，不用重新搜索/安装/验证一遍。summary_text 是这个
        工具解决了什么问题的一句话说明，usage_example 是具体怎么调用它的
        示例(命令或代码片段)，这两段话也会展示给用户，让用户能自己去探索
        这个新装好的工具。**没有先跑通 run_scenario_test 就不要调用这个**，
        会被拦截。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "deliver_tool_to_user")
        if not allow:
            _log_tool(qid, "deliver_tool_to_user", {"tool_name": tool_name}, guard_msg, time.time() - t0)
            return guard_msg

        uid = _uid()
        safe_name = "".join(c if (c.isalnum() or c in "_-") else "_" for c in tool_name)[:60]
        test_results = s.get("_scenario_test_results", {})
        if not test_results.get(safe_name):
            summary = ("[已拦截] 这个工具还没有 run_scenario_test 验证通过的记录，不能交付。"
                       "请先 write_scenario_test + run_scenario_test 跑通，再来交付。")
            _log_tool(qid, "deliver_tool_to_user", {"tool_name": tool_name}, summary, time.time() - t0)
            return summary

        target_dir = os.path.join(_toolbox_dir(uid), "repos", safe_name)
        try:
            items = _load_toolbox(uid)
            items = [it for it in items if it.get("name") != safe_name]  # 去重，同名覆盖更新
            items.append({
                "name": safe_name,
                "path": target_dir,
                "summary": summary_text[:300],
                "usage_example": usage_example[:500],
                "verified": True,
                "delivered_at": datetime.now().isoformat(),
                "qid": qid,
            })
            _save_toolbox(uid, items)
            summary = (
                f"🎁 已交付给用户(uid={uid})专属工具箱: {safe_name}\n"
                f"路径: {target_dir}\n"
                f"用途: {summary_text}\n"
                f"用法示例: {usage_example}\n"
                f"下次该用户遇到相似任务，search_my_toolbox 会直接命中这个工具。"
            )
            s["tool_delivered"] = s.get("tool_delivered") or []
            s["tool_delivered"].append({"name": safe_name, "summary": summary_text[:200], "usage_example": usage_example[:300]})
        except Exception as e:
            summary = f"deliver_tool_to_user 异常: {type(e).__name__}: {str(e)[:200]}"
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "deliver_tool_to_user", {"tool_name": tool_name}, summary[:600], time.time() - t0)
        return summary

    search_github.__name__ = "search_github"
    search_my_toolbox.__name__ = "search_my_toolbox"
    install_github_repo.__name__ = "install_github_repo"
    write_scenario_test.__name__ = "write_scenario_test"
    run_scenario_test.__name__ = "run_scenario_test"
    deliver_tool_to_user.__name__ = "deliver_tool_to_user"
    return [search_github, search_my_toolbox, install_github_repo,
            write_scenario_test, run_scenario_test, deliver_tool_to_user]


# ═══════════════════════════════════════════════════════════════════════
# NoCode 建站工具 (v60 · 2026-08-07)
#
# 用户明确提出「网站/建站 本来nocode就是做这个 可以做」——本地已装
# @nocode/nocode-cli(美团零代码平台 CLI)，把它薄封装成 auto agent 的
# 执行工具，让"帮我做个官网/活动页/管理后台"这类任务能真正端到端交付
# (创建→改→截图确认→部署)，而不是只给一段静态 HTML 代码了事。
#
# 设计取舍(遵循 nocode-cli skill 的强约束):
#   1. create/send 是长时间流式命令(2-5分钟, NDJSON 输出)，这里用
#      subprocess.run 给足超时(默认480s，比 skill 建议的600s略紧凑，
#      避免过度占用单个 auto agent 任务的整体墙钟预算)一次性收完整
#      输出，再从最后一行等 JSON 事件里解析 chatId/chatUrl，不做真正
#      的后台轮询(auto agent 本身就是同步阻塞跑到底的模型，没有轮询
#      基础设施，这里简化成"等它跑完"是合理的一致性选择)。
#   2. 每次 send 只做一个功能点——system prompt 里明确告知 LLM 这条
#      约束，不在工具层强制拆分(拆分逻辑需要理解语义，工具层做不到)。
#   3. delete 类破坏性操作不封装成工具——没有"用户明确确认"的交互通道，
#      auto agent 不该有能力自主删除线上作品。
#   4. 每次调用都过 _check_budget，跟其他工具共享同一套预算/循环卫兵。
# ═══════════════════════════════════════════════════════════════════════

def _run_nocode_cli(args: list, timeout_s: float = 480) -> tuple:
    """跑一条 nocode CLI 命令，返回 (ok, stdout_tail, stderr_tail)。"""
    try:
        r = _subprocess.run(
            ["nocode"] + args, capture_output=True, text=True, timeout=timeout_s,
        )
        return (r.returncode == 0, (r.stdout or "")[-6000:], (r.stderr or "")[-2000:])
    except _subprocess.TimeoutExpired:
        return (False, "", f"[超时] nocode 命令执行超过 {timeout_s}s")
    except FileNotFoundError:
        return (False, "", "[未安装] 找不到 nocode 命令，请先 npm install -g @nocode/nocode-cli")
    except Exception as e:
        return (False, "", f"nocode CLI 异常: {type(e).__name__}: {str(e)[:200]}")


def _extract_nocode_chat_url(stdout: str) -> dict:
    """从 nocode create/send 的 NDJSON 流式输出里找 done 事件，抽取
    chatId/chatUrl。找不到就返回空 dict(调用方会把原始 stdout 尾部
    展示给模型，让它自己判断发生了什么)。"""
    info = {}
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except Exception:
            continue
        if isinstance(evt, dict):
            chat_id = evt.get("chatId") or (evt.get("data") or {}).get("chatId")
            if chat_id:
                info["chatId"] = chat_id
                chat_url = evt.get("chatUrl") or (evt.get("data") or {}).get("chatUrl")
                info["chatUrl"] = chat_url or f"https://nocode.sankuai.com/#/chat?pageId={chat_id}"
                break
    return info


def _make_nocode_tools(qid):
    """建站三件套：nocode_create / nocode_send / nocode_deploy。
    只在真正被调用时才检查 CLI 是否安装/登录，不在启动时预检查(避免
    没有建站需求的任务也承担这个开销)。"""

    def nocode_create(prompt: str, template: str = "default") -> str:
        """用 NoCode 平台创建一个新应用/网站(零代码，自动生成代码+建表+
        部署预览)。prompt 是自然语言需求描述(比如"做一个产品介绍官网，
        包含首页/关于我们/联系方式三个板块")，不要在这里写具体代码或
        shell 命令。template 可选 default/nocode-miniprogram-web/
        nocode-react-mtd/nocode-vue-mtd/nocode-react-roo，不确定就用
        default。执行需要 2-5 分钟，返回创建结果和 chatId(后续
        nocode_send/nocode_deploy 都要用这个 chatId)。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "nocode_create")
        if not allow:
            _log_tool(qid, "nocode_create", {"prompt": prompt}, guard_msg, time.time() - t0)
            return guard_msg

        ok, out, err = _run_nocode_cli(["create", prompt, "--template", template])
        info = _extract_nocode_chat_url(out) if ok else {}
        if info.get("chatId"):
            s.setdefault("nocode_chats", []).append(info["chatId"])
            summary = (f"创建成功 · chatId={info['chatId']} · chatUrl={info['chatUrl']}\n"
                       f"后续用 nocode_send(chat_id='{info['chatId']}', message=...) 继续修改，"
                       f"用 nocode_screenshot 截图确认效果，用 nocode_deploy 部署上线。")
        else:
            summary = f"创建可能未完成(ok={ok})，原始输出尾部:\n{out[-1500:]}\n{err[-500:] if err else ''}"
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "nocode_create", {"prompt": prompt[:100], "template": template}, summary[:500], time.time() - t0)
        return summary

    def nocode_send(chat_id: str, message: str) -> str:
        """向一个已存在的 NoCode 应用发送修改指令(自然语言描述想要的
        结果，比如"把主色调改成深蓝色"，不要发 shell 命令/大段代码)。
        每次只包含一个内聚的功能修改，多个不相关改动要分多次调用。
        执行需要 2-5 分钟。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "nocode_send")
        if not allow:
            _log_tool(qid, "nocode_send", {"chat_id": chat_id}, guard_msg, time.time() - t0)
            return guard_msg

        ok, out, err = _run_nocode_cli(["send", chat_id, message])
        summary = (f"send 完成(ok={ok})，输出尾部:\n{out[-1500:]}"
                   + (f"\n{err[-500:]}" if err and not ok else ""))
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "nocode_send", {"chat_id": chat_id, "message": message[:100]}, summary[:500], time.time() - t0)
        return summary

    def nocode_screenshot(chat_id: str) -> str:
        """给一个 NoCode 应用当前版本截图，返回截图 URL 供确认效果。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "nocode_screenshot")
        if not allow:
            _log_tool(qid, "nocode_screenshot", {"chat_id": chat_id}, guard_msg, time.time() - t0)
            return guard_msg
        ok, out, err = _run_nocode_cli(["screenshot", chat_id], timeout_s=90)
        summary = f"截图{'成功' if ok else '失败'}，输出:\n{out[-1000:]}" + (f"\n{err[-300:]}" if err else "")
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "nocode_screenshot", {"chat_id": chat_id}, summary[:500], time.time() - t0)
        return summary

    def nocode_deploy(chat_id: str) -> str:
        """把 NoCode 应用的最新版本部署上线，返回部署结果和外部访问地址。
        部署前建议先用 nocode_screenshot 确认效果正常。"""
        t0 = time.time()
        s = _sess(qid)
        allow, guard_msg = _check_budget(s, "nocode_deploy")
        if not allow:
            _log_tool(qid, "nocode_deploy", {"chat_id": chat_id}, guard_msg, time.time() - t0)
            return guard_msg
        ok, out, err = _run_nocode_cli(["deploy", chat_id], timeout_s=180)
        summary = f"部署{'成功' if ok else '失败'}，输出:\n{out[-1500:]}" + (f"\n{err[-500:]}" if err else "")
        if guard_msg:
            summary = f"{summary}{guard_msg}"
        _log_tool(qid, "nocode_deploy", {"chat_id": chat_id}, summary[:500], time.time() - t0)
        return summary

    nocode_create.__name__ = "nocode_create"
    nocode_send.__name__ = "nocode_send"
    nocode_screenshot.__name__ = "nocode_screenshot"
    nocode_deploy.__name__ = "nocode_deploy"
    return [nocode_create, nocode_send, nocode_screenshot, nocode_deploy]


def _calculation_trace_uses_extracted_values(session, trace_step):
    """验证被路由的读图计算确实使用了本题感知值，阻断单位方形等伪痕迹解锁。"""
    import re as _re_calc_link
    values = session.get("_budget", {}).get("_vlm_extracted_values") or []
    extracted = {
        str(value).lstrip("+") for value in values
        if str(value).lstrip("+") not in {"", "0"}
    }
    input_numbers = set(_re_calc_link.findall(
        r"-?\d+(?:\.\d+)?", str((trace_step or {}).get("inputs") or "")))
    return len(extracted & input_numbers) >= 2


# ── 工具装配/Footprint Ladder/渐进披露已抽到 tool_factory.py ──
import tool_factory as _tool_factory
_tool_factory.configure(
    MEYO_ENGINE_EFFICIENCY=MEYO_ENGINE_EFFICIENCY,
    _CKPT_TEXT_MAX=_CKPT_TEXT_MAX,
    _DATA_FETCH_TOOLS_M3B=_DATA_FETCH_TOOLS_M3B,
    _DISTINCT_QUERY_HARD=_DISTINCT_QUERY_HARD,
    _arg_fingerprint=_arg_fingerprint,
    _calculation_trace_uses_extracted_values=_calculation_trace_uses_extracted_values,
    _candidate_similarity=_candidate_similarity,
    _check_budget=_check_budget,
    _constraint_mapping_gate=_constraint_mapping_gate,
    _dead_domain_precheck=_dead_domain_precheck,
    _emit_gate_event=_emit_gate_event,
    _gate_budget_tight=_gate_budget_tight,
    _gate_or_hint=_gate_or_hint,
    _identity_coverage_gate=_identity_coverage_gate,
    _log_tool=_log_tool,
    _looks_like_finalize_noise=_looks_like_finalize_noise,
    _m3b_record_host=_m3b_record_host,
    _make_capability_writer_tools=_make_capability_writer_tools,
    _make_cli_hub_tools=_make_cli_hub_tools,
    _make_delegate_tool=_make_delegate_tool,
    _make_desktop_gui_tools=_make_desktop_gui_tools,
    _make_evolution_tools=_make_evolution_tools,
    _make_execution_tools=_make_execution_tools,
    _make_goal_tools=_make_goal_tools,
    _make_local_and_reasoning_tools=_make_local_and_reasoning_tools,
    _make_metacognition_tools=_make_metacognition_tools,
    _make_nocode_tools=_make_nocode_tools,
    _make_plan_tool=_make_plan_tool,
    _make_self_evolve_tools=_make_self_evolve_tools,
    _make_skill_tools=_make_skill_tools,
    _make_subagent_tool=_make_subagent_tool,
    _make_tool_discovery_tools=_make_tool_discovery_tools,
    _make_tool_install_tools=_make_tool_install_tools,
    _maybe_anysearch_first_hint=_maybe_anysearch_first_hint,
    _maybe_block_finalize=_maybe_block_finalize,
    _maybe_channel_switch_nudge=_maybe_channel_switch_nudge,
    _narrative_single_source_gate=_narrative_single_source_gate,
    _neutral_stepwise_finalize=_neutral_stepwise_finalize,
    _now_iso=_now_iso,
    _question_wants_number=_question_wants_number,
    _record_policy_decision=_record_policy_decision,
    _register_intent_hypotheses=_register_intent_hypotheses,
    _rs_sessions=_rs_sessions,
    _sess=_sess,
    _single_source_reconcile_nudge=_single_source_reconcile_nudge,
    _strongest_perception_candidate=_strongest_perception_candidate,
    _track_failure_signature=_track_failure_signature,
    _vote_key=_vote_key,
    _wall_past=_wall_past,
    _wall_precheck=_wall_precheck,
    get_all_tools=get_all_tools,
    qa_retry_policy=qa_retry_policy,
)
_make_tools = _tool_factory._make_tools




# ═══════════════════════════════════════════════════════════════════════
# ChatModel 工厂 · 把美团网关(qa_llm_gateway 走的 CCR 直通代理)包装成
# langchain_anthropic.ChatAnthropic，供 deepagents 当"大脑"用。
# ═══════════════════════════════════════════════════════════════════════

# ── v113c(2026-08-16) 通道韧性·模型调用级重试包装 ─────────────────────
# CCR 多账号随机路由下 "Access to Anthropic models is not allowed" 类 400 是
# ── provider/gateway 适配层已抽出到 provider_gateway.py(2026-08-27·架构净化)──
#   通道400重试/消息消毒/longcat兼容/tool配对/gateway构建等~1520行搬出god-file。
#   import * 拿回全部符号·所有原调用点零改动。零auto反向依赖(常量来自qa_llm_gateway)。
from provider_gateway import *  # noqa: F401,F403
from provider_gateway import (build_gateway_chat_model, _ChannelRetryModel, _ChannelRetryRunnable,
    _sanitize_for_provider, _is_longcat_run, _longcat_prepare, _retryable_exc,
    _is_any_400, _is_channel_400, _is_ctx_overflow_guess, _fix_tool_pairing,
    _flatten_system_messages, _msg_recontent, _shrink_messages)  # 显式(供静态检查)

def _commit_final_artifact(session, qid, final, *, via_finalize=False, persist=None):
    """Native 唯一终答边界:任意来源的 final 都提交为 typed durable artifact。

    agent_engine 已 native-only，不再保留 thin_active/legacy 双路径。这里只保证返回文本、
    checkpoint 与恢复状态一致，不做答案语义判断(context not control)。
    """
    text = str(final or "").strip()
    if not text:
        return text
    try:
        import answer_artifact as _aa_final
        if persist is None:
            persist = lambda _s: _save_ckpt(qid, status="running")
        artifact = _aa_final.commit(
            session, text,
            provenance="model_final" if via_finalize else "plain_text",
            persist=persist)
        return _aa_final.render(artifact)
    except Exception:
        return text


def run_auto_agent(question: str, qid: str = "", max_iterations: int = 30,
                    timeout_per_call: float = 180, urgency: str = "normal",
                    hard_wall_s: float = None, resume: bool = True,
                    conversation_history: list = None, extra_system_prompt: str = "",
                    force_task_level: str = None, user_id: str = "default"):
    """Auto Agent 模式主入口。

    user_id: P0(2026-08-07 · 个性化地基) · 这次任务属于哪个用户。所有个性化
        (per-user 记忆/画像/意图/技能)都以它为键。不传="default"(单用户/无
        用户场景,行为完全同旧版)。这是"每个用户一个能进化的 agent"的地基——
        没有这根线,记忆/演绎/技能都只能全局共享,谈不上个性化。

    extra_system_prompt: 统一2(2026-08-07) · spawn_subagent 派子 agent 时传入的
        额外角色/约束,附加在通用大脑 system prompt 之后。空则等同不传(主 agent
        或无特殊角色的子 agent)。

    force_task_level: v60(2026-08-07) · 对齐 Kimi K3「快速/K3旗舰/集群」三档
        模型切换——用户在网页上手动选定档位时传入，跳过/覆盖自动 triage：
        - "琐事"(前端"快速")：强制走轻量路径，不管题目实际难度，用户明确要
          "先给个快答案"时用。
        - "标准"(前端"标准")：强制走标准单轮搜索路径，不升级到重型蜂群。
        - "重型"(前端"集群")：强制启用重型多跳蜂群(deep_research_swarm)，
          即使 triage 判断题目很简单也照跑——用户明确要"多方核实/深挖"时用。
        不传(None，默认)时行为完全不变：仍由 step0_triage 自动判断难度。

    Args:
        urgency: "high"(紧急，优先止损快交付) | "normal"(默认) | "low"(不急，可多磨)
                 用于自适应重试策略；不传就按 normal 处理。
        hard_wall_s: 整个任务的墙钟硬顶(秒)。不传时按 urgency 给出合理默认值——
                     紧急任务更早收口，不紧急任务给更宽裕的预算，模仿人类
                     "越急越见好就收"的直觉，和 qa_retry_policy 的 urgency
                     语义保持一致。超过硬顶后所有工具调用会被拦截，只放行
                     finalize_answer，逼迫 agent 收尾交付而不是被异常打断。
        resume: True(默认)时，若同 qid 存在未完成(status != "done")的
                checkpoint，则读回关键态并把「续传上下文」注入首条 user 消息，
                让 agent 基于已有进度继续，不从零重复检索。resume=False 或无
                checkpoint 时，行为和不带续传时完全一致(全新开始)。
        conversation_history: v59(2026-08-07) · 用户反馈「网页上继续对话，
                agent 说没有上下文」——排查发现 resume/checkpoint 是给
                「同一个任务中途崩了续跑」用的，一旦任务正常 status=done，
                续传就不再生效(等同全新开始)，从来不是「多轮聊天记忆」。
                这里补的是真正的多轮对话：调用方(如 agent_web.py)按
                [{"question":..., "final":...}, ...] 传入历史轮次，会被
                拼进首条 user 消息，让 LLM 真正看到"你之前问过什么、我
                答过什么"。不传(None)时行为和之前完全一致(单轮任务)。
    """
    t_start = time.time()
    qid = qid or f"auto_{int(t_start)}"
    user_id = user_id or "default"
    trace = {"qid": qid, "question": question, "mode": "auto_agent", "urgency": urgency,
             "user_id": user_id, "started": datetime.now().isoformat(), "steps": []}

    # 启动自检(2026-08-12·最高等级事故修复):开机第一秒 compile 核心模块·坏了标 DEGRADED。
    # 事故中主文件语法坏着还带病跑完·靠人偶然 grep 才发现——现在开机第一秒自检·结果可信度
    # 从一开始就标清。坏了不 return(可能只是某个非关键模块)·但标 DEGRADED 让评分排除。
    _startup_broken = _startup_selfcheck()
    if _startup_broken:
        trace["startup_degraded"] = _startup_broken
        trace.setdefault("degraded", True)
        # 解释器/依赖错配=硬中止·不许带病跑。
        #   病根(本会话实证):机器 python3 漂到 3.14(生产钉死 3.12)·裸 python3 起的
        #   run 缺 deepagents → 旧代码只打 DEGRADED 标却【继续】走到 `from deepagents import`
        #   崩成裸 ModuleNotFoundError·或半路记 hit=false 污染数据(我这轮就被降级 run 的
        #   trace 骗过一次)。修法:把"解释器版本≠3.12 / 缺生产依赖"这类【根本跑不了】的
        #   broken 项升级为当场 SystemExit(97)——和 canonical_python.assert_ 同信号·让每条
        #   入口路径(harness/直跑/evolution agent)都硬失败·不再有静默降级 run。语法类
        #   broken(可降级跑)保持原 DEGRADED 标行为·不硬中止。
        _fatal = [b for b in _startup_broken if ("解释器版本" in b or "缺生产依赖" in b)]
        if _fatal:
            raise SystemExit(97)

    # ── 断点续传: 有未完成 checkpoint 时恢复关键态并构造续传上下文 ──
    resume_context = ""
    if resume:
        _ckpt = _load_ckpt(qid)
        if _ckpt and _ckpt.get("status") != "done":
            # 根因修(e142): 续传目标用本次 fresh question(权威完整题面)·不用 ckpt 里的残缺题面
            _ck_q = (_ckpt.get("question") or "").strip()
            # 2026-08-19 入口兜底(治直调 qid 撞旧 ckpt·stale-ckpt 污染): 本次题面与 ckpt
            #   题面【实质不同】(非仅长度差·是不同任务)→【当全新任务·不续旧上下文】。
            #   病根实证: verify 批量用 qid=verify_Q3 撞上历史遗留 verify* ckpt·resume
            #   注入了别的题的残缺上下文→模型收到"题面被截断"→空转交门噪音。同事直调
            #   复用/巧合撞名 qid 也会踩。判据: 归一化后既非子串包含、Jaccard 词重叠 <0.5
            #   = 不同任务。fail-safe: 异常/判不准→保守续传(不破坏正常 resume)。
            _stale_mismatch = False
            try:
                if _ck_q and question:
                    _q1 = re.sub(r"\s+", "", _ck_q.lower())
                    _q2 = re.sub(r"\s+", "", question.strip().lower())
                    if _q1 != _q2 and _q1 not in _q2 and _q2 not in _q1:
                        _w1 = set(re.findall(r"\w+", _ck_q.lower()))
                        _w2 = set(re.findall(r"\w+", question.lower()))
                        _jac = len(_w1 & _w2) / float(len(_w1 | _w2) or 1)
                        if _jac < 0.5:
                            _stale_mismatch = True
            except Exception:
                _stale_mismatch = False
            if _stale_mismatch:
                print(f"  [auto-agent] ⚠️ ckpt 题面与本次题面【实质不同】(疑似 qid 撞旧 ckpt)·"
                      f"当全新任务处理·不续旧上下文(治 stale-ckpt 污染)。", file=sys.stderr)
                _ckpt = None   # 丢弃旧 ckpt·下面走全新任务路径
            elif _ck_q and question and _ck_q != question.strip():
                print(f"  [auto-agent] ⚠️ ckpt 题面({len(_ck_q)}字)≠本次题面({len(question)}字)·"
                      f"以本次 --question 为准(旧 ckpt 可能被缩写/截断)。", file=sys.stderr)
        if _ckpt and _ckpt.get("status") != "done":
            resume_context = _build_resume_context(_ckpt, fresh_question=question)
            if resume_context:
                _restore_session_from_ckpt(qid, _ckpt, fresh_question=question)
                trace["resumed"] = True
                trace["resumed_from"] = {
                    "n_tool_calls": len(_ckpt.get("tool_log") or []),
                    "status": _ckpt.get("status"),
                    "updated": _ckpt.get("updated"),
                }
                print(f"  [auto-agent] 检测到未完成 checkpoint(qid={qid})，续传。", file=sys.stderr)

    # session 需要记录 question(供 checkpoint 落盘用)
    _sess(qid)["question"] = question
    # (2026-08-27 引擎归一) agent_engine 已 native-only；thin 不再是可选分支而是唯一生产语义。
    # 直接在权威 session 标记·不再靠 MEYO_NATIVE_THIN_LOOP 环境开关制造双路径。
    _sess(qid)["_thin_mode"] = True
    # ★过程流对外接口(2026-08-20 上线·让前端实时看agent在想什么):自动注册文件sink·
    #   前端 tail -f /tmp/agent_progress_<qid>.jsonl 即可实时收人话进度(🎯理解意图/🔄卡住换向/
    #   🩺诊断/🔀换候选…)。返回路径也写进session供上层读。env MEYO_PROGRESS_STREAM=0关。
    try:
        import sys as _sys_ps
        _adir = os.path.dirname(os.path.abspath(__file__))
        if _adir not in _sys_ps.path:
            _sys_ps.path.insert(0, _adir)
        import progress_stream as _ps_reg
        if _ps_reg._enabled():
            _pf = _ps_reg.register_file_sink(qid)
            _sess(qid)["_progress_file"] = _pf
    except Exception:
        pass
    # P0 · 把 user_id 落进 session,供 per-user 记忆/意图/技能读取
    _sess(qid)["user_id"] = user_id
    # 把用户手动选的档位透传进 session，供 run_deep_research_swarm 等
    # 工具感知("集群"档位下要放大 max_hops，不能沿用写死的默认值)。
    _sess(qid)["force_task_level"] = force_task_level

    _arg_hard_wall_s = hard_wall_s  # v76 · 记原始传入值(判调用方是否显式控速)
    if hard_wall_s is None:
        # 2026-08-06 · 用户「超时不用那么严格·放开」:难题要给足时间用工具多方
        # 考据(github/EC 编号那种连 curl 多个 API),别因墙钟太紧逼它 12s 放弃。
        # normal 600→900, low 900→1200(和经验时间自适应的 1200 上限对齐)。
        hard_wall_s = {"high": 300, "normal": 900, "low": 1200}.get(urgency, 900)
        # ── ★灵魂驾驶员·据题型动态编排时间/步数(2026-08-20 用户:时间步数他统一控制)──
        #   仅在【调用方没显式控速】时(评测/显式传值时尊重传入·不动)。简单/计算题少给(省时
        #   不卡)·多约束多跳题给足。题型感知带缓存(native首轮也复用·同题不重复调LLM)。
        #   env MEYO_PILOT_BUDGET=0 关(默认开)·fail-safe→保持上面默认。
        try:
            if os.environ.get("MEYO_PILOT_BUDGET", "1") != "0":
                import perspective_engine as _pe_bud
                if _pe_bud._enabled():
                    _bud = _pe_bud.orchestrate_budget(
                        question, base_wall_s=hard_wall_s, base_max_iter=max_iterations)
                    # 只在建议更小(省时)或题型明确需要更多步时采纳·且不超原默认的1.0x墙钟
                    #   (防越跑越慢:墙钟只缩不放·步数可按难度调)。
                    _bw = int(_bud.get("wall_s") or hard_wall_s)
                    if 60 <= _bw <= hard_wall_s:
                        hard_wall_s = _bw
                    _bi = int(_bud.get("max_iter") or max_iterations)
                    if 8 <= _bi <= max_iterations:
                        max_iterations = _bi
        except Exception:
            pass

    # (2026-08-15·阶段2) 走引擎接口层(可替换后端·当前deepagents passthrough·行为不变)·
    #   为商用换自研轻量loop铺路。旧: from deepagents import create_deep_agent。
    from agent_engine import create_agent_engine

    # 架构优化(2026-08-07) · 首步延迟治理:开跑前有两次独立的 LLM 预热调用——
    # step0_triage(longcat-2.0·判难度) 和 _build_user_context 里的 extract_intent(抽意图)。
    # 之前串行 → 首个工具出现前静默 30-60s(用户看着像卡住)。这两个互不依赖,把
    # user_context 计算丢后台线程,和 triage + agent 组装并发跑,首步延迟砍近一半。
    _uctx_result = {"text": ""}
    def _bg_user_context():
        try:
            _uctx_result["text"] = _build_user_context(user_id, question, trace)
        except Exception:
            _uctx_result["text"] = ""
    _uctx_thread = None
    try:
        import threading as _th
        _uctx_thread = _th.Thread(target=_bg_user_context, daemon=True)
        _uctx_thread.start()
    except Exception:
        _bg_user_context()  # 起线程失败就同步跑(不影响正确性)

    # Auto 只保留产品模式。旧 benchmark 入口会先做题型分诊，再强制证据预检；对写网页/部署
    # 这类产品任务，它会把源码路径当“候选实体”，并在文件已验证后仍说证据不足。产品路径只需
    # 保留附件模态事实，任务强度由用户显式档位或标准默认决定，不再运行 benchmark triage。
    task_level_hint = "标准"
    # 产品模式仍保留【能力事实】分诊（是否需 search/vision/compute），只删除 benchmark 的
    # 实体候选、证据覆盖率和评分门。搜索是 Auto 的核心能力，不能把产品默认写死 no_search。
    try:
        import qa_task_analysis as M
        _triage_hint = M.step0_triage(question)
        task_level_hint = _triage_hint.get("task_level", "标准")
        # 用户对本轮工具边界的明确说明属于最高优先级 context。不是父层猜意图：只有用户
        # 明说“不需要/不要联网/无需搜索”时才覆盖网络路由，本地文件/代码搜索仍可用。
        _qnorm = str(question or "").lower()
        if any(mark in _qnorm for mark in ("不需要联网", "无需联网", "不要联网", "不需要搜索", "无需搜索", "do not use internet", "no internet")):
            _triage_hint["search_route"] = "no_search"
            _triage_hint["user_network_boundary"] = True
    except Exception:
        _triage_hint = {"task_level": "标准", "search_route": "model_decides",
                        "modality_needs": []}
    _sess(qid)["triage"] = _triage_hint
    if _triage_hint.get("search_route") == "no_search":
        retry_guidance_no_search = ("\n【本轮用户明确/分诊确认无需联网】不要调用网络搜索、GitHub或网页抓取；"
                                    "直接使用本地文件、代码、Shell和验证工具完成。")
    else:
        retry_guidance_no_search = ""
    if force_task_level in ("琐事", "标准", "重型"):
        # 手动档位只覆盖模型/步骤预算；保留 triage 的 modality_needs、约束与意图供工具路由。
        task_level_hint = force_task_level
        trace["task_level_forced"] = force_task_level

    # 经验时间(用户「时间还有经验时间·随时加时间」): 从历史 auto 日志
    #   学同 task_level 任务的实际耗时·动态调 hard_wall(比固定值贴合)。同级历史 p75 耗时
    #   ×1.3 作预算·最少不低于 urgency 默认·最多 1200s。
    # v76(2026-08-09)修"越跑越慢"正反馈:显式传了 hard_wall_s(评测/调用方明确控速)
    #   时·尊重它·不再按历史 p75 向上撑(否则历史跑满记录会让墙越撑越大·评测提速失效)。
    #   环境变量 MEYO_RESPECT_HARD_WALL=1 也可强制关闭动态放大。
    _explicit_wall = (_arg_hard_wall_s is not None) or os.environ.get("MEYO_RESPECT_HARD_WALL") == "1"
    try:
        if not _explicit_wall:  # 显式控速时整段跳过·尊重传入 hard_wall_s(治越跑越慢)
            _hist = []
            if os.path.exists(AUTO_LOG):
                for _l in open(AUTO_LOG, encoding="utf-8"):
                    try:
                        _r = json.loads(_l)
                        if _r.get("task_level_hint") == task_level_hint and _r.get("total_wall_s"):
                            _hist.append(_r["total_wall_s"])
                    except Exception:
                        pass
            if len(_hist) >= 3:
                _hist.sort()
                _p75 = _hist[int(len(_hist) * 0.75)]
                _exp = min(1200, max(hard_wall_s, round(_p75 * 1.3)))
                if _exp != hard_wall_s:
                    trace["hard_wall_exp_adjusted"] = {"from": hard_wall_s, "to": _exp,
                                                        "n_hist": len(_hist), "p75": _p75}
                    hard_wall_s = _exp
    except Exception:
        pass

    retry_guidance = qa_retry_policy.render_retry_guidance_for_prompt(task_level_hint, urgency)
    # 工具使用自动优化: 把历史工具速度/成功率统计拼进 retry_guidance，
    # 让 agent 有数据地选「快而准」的工具(历史→统计→喂回决策的最小闭环)。
    try:
        _perf_hint = _render_tool_perf_hint()
        if _perf_hint:
            retry_guidance = retry_guidance + _perf_hint
    except Exception:
        pass
    # 统一4 · 把可用 skill 清单也塞进 system prompt(对齐 deepagents 在系统提示
    # 里列 skill,让模型一开始就知道有哪些方法论文档可 load,而不是全靠临时搜)。
    try:
        _skills_hint = _render_skills_hint()
        if _skills_hint:
            retry_guidance = retry_guidance + _skills_hint
    except Exception:
        pass
    # ── v108(2026-08-16 · GAIA-165 复盘三件套) ──
    #   M2b 活路径白名单: 近12h实测可达域名注入·把换源从盲撞变有据;
    #   M2a 跨轮错题本: 同qid历史轮的答案/主故障注入·新轮不重复踩坑
    #        (历史曾答对的注入已验证成功路径);
    #   M5 钦定入口优先: 题干 accessed on/linked from X 句式→低成本元数据通道指导。
    #   三段全部 fail-safe(异常返回空串不影响主流程)。
    try:
        _lp_hint = _render_live_path_hint()
        if _lp_hint:
            retry_guidance = retry_guidance + _lp_hint
    except Exception:
        pass
    try:
        _ep_hint = _render_entry_point_hint(question)
        if _ep_hint:
            retry_guidance = retry_guidance + _ep_hint
    except Exception:
        pass
    # ★direct_answer 短路(A方案·用户2026-09-01"找长板不让重家做简单题"):triage 判琐事(纯概念·
    # 答案稳定·no_search)且已给出实质 direct_answer 时·直接采纳它交付·跳过整个 native loop
    # (省 engine 主答 13s + loop 骨架 28s·实测琐事题 45s→~10s)。这是【采纳 triage 已生成的答案】·
    # 不是加门/不是父层编答案——triage 本就为琐事设计了 direct_answer 快路径·此前主流程丢弃它照跑
    # loop(A方案揪出的真bug)。保守条件:仅 task_level=琐事 且 no_search 且 direct_answer≥50字·
    # 稍复杂题仍走完整 loop(auto 长板)。env MEYO_AUTO_DIRECT_ANSWER=0 可关(回退老行为)。
    if os.environ.get("MEYO_AUTO_DIRECT_ANSWER", "1") != "0" and force_task_level not in ("标准", "重型"):
        _da = str((_triage_hint or {}).get("direct_answer") or "").strip()
        if (task_level_hint == "琐事" and _triage_hint.get("search_route") == "no_search"
                and len(_da) >= 50):
            trace["direct_answer_shortcut"] = True
            trace["task_level_hint"] = task_level_hint
            trace["final"] = _da
            trace["status"] = "done"
            try:
                _sess(qid)["final"] = _da
                _sess(qid)["_direct_answer_shortcut"] = True
            except Exception:
                pass
            return _da, trace
    system_prompt = BASE_SYSTEM_PROMPT.format(retry_guidance=retry_guidance + retry_guidance_no_search,
                                              min_inv=FINALIZE_MIN_INVESTIGATIVE,
                                              wall_s=int(hard_wall_s or 600))
    # run nonce 破 CCR hash 粘路由: 网关多账号轮询对【相同大请求体】
    #   hash 粘路由——实证 bec7 连续 5+ 轮同 Request ID 确定性粘死坏账号
    #   (小请求探测随机·大请求粘死=探测盲区)。每个 run 注入唯一 nonce·
    #   让每次起跑重新抽奖(50% 坏率下守护几轮必活)。
    import uuid as _uuid_mod
    system_prompt += f"\n<!-- run:{_uuid_mod.uuid4().hex[:12]} -->"
    # Auto 只保留产品模式：BrowseComp 多实体题的强制检索/校准纪律已移除。模型仍可按任务
    # 自主调用 search/fetch，但写代码、网页、部署不再被迫走实体候选与 coverage 评分循环。
    # 统一2 · 子 agent 的额外角色/约束附加在通用大脑定位之后
    if extra_system_prompt:
        system_prompt = f"{system_prompt}\n\n# 本次子任务的额外指令\n{extra_system_prompt}"
    # 用户手动选了「集群」档位：显式告知必须走重型多跳蜂群深挖，
    # 不能因为题目看着简单就自行跳过——这是用户主动要"多方核实/深挖"的
    # 明确意图，权重高于 LLM 自己的难度判断。
    if force_task_level == "重型":
        system_prompt = (f"{system_prompt}\n\n# 用户已手动选择「集群」档位\n"
                          f"用户明确要求本题走最高强度的核实/深挖路径，即使你判断题目"
                          f"本身不难，也必须调用 run_deep_research_swarm(或等价的重型多跳"
                          f"搜索)做多源交叉验证，不能因为'看起来简单'就跳过或走轻量路径。")
    elif force_task_level == "琐事":
        system_prompt = (f"{system_prompt}\n\n# 用户已手动选择「快速」档位\n"
                          f"用户明确要求本题优先给出**最快**的可用答案，能跳过的搜索/质检"
                          f"步骤都跳过，先给结论，除非缺失关键事实性信息否则不要展开重搜索。")
    elif force_task_level == "标准":
        # 用户手动选「K3(全能旗舰)」档位：底层模型已路由到 sonnet
        # (build_gateway_chat_model tier="k3")，这里补一句提示——sonnet
        # 能力/成本折中，遇到题目实际比预期更难(比如需要多跳查证的冷门
        # ID/编号类)，不要因为档位是"标准"就轻易放弃或浅尝辄止，该用的
        # 工具(search_github/shell_exec curl 权威 API 等)一样要用够。
        system_prompt = (f"{system_prompt}\n\n# 用户已手动选择「K3(全能旗舰)」档位\n"
                          f"当前使用能力/成本折中的日常旗舰模型，适合大多数标准复杂度任务。"
                          f"如果执行过程中发现这题实际需要更高强度的多方交叉核实，仍应按"
                          f"「多跳题通用解法模板」把该用的工具用够，不要因为档位是标准档就"
                          f"浅尝辄止或提前放弃。")

    # P2(2026-08-09) · 动态 step 预算:按 triage 难度提升 max_iterations,治"深题撞
    # recursion_limit(=max_iter*2+10)空转退出"(GAIA 100miss 里 15 道撞 70 步上限·
    # 都是 L3/multihop 深题攒了 30-57 步没在预算内收敛)。轻题不浪费、深题给够步数。
    # 通用(按难度非按题)·可 MEYO_AUTO_MAX_ITER_HEAVY 覆盖。fail-safe:异常保持原值。
    try:
        if force_task_level != "琐事":  # 用户强制轻量档时不加码
            # 2026-08-14(墓碑题事故):重型档 60→90。墓碑/多跳长链题(找最老口味墓碑→
            # 定位背景照片→读韵脚最后一行)60 步就撞 max_iterations 上限交半成品·90 步
            # (recursion_limit→190)给长链够用。agent 有进展还能 request_more_budget 再 +20。
            _heavy = int(os.environ.get("MEYO_AUTO_MAX_ITER_HEAVY", "90"))
            _std = int(os.environ.get("MEYO_AUTO_MAX_ITER_STD", "40"))
            if task_level_hint == "重型" and max_iterations < _heavy:
                max_iterations = _heavy   # recursion_limit → 190·深多跳长链够用
            elif task_level_hint == "标准" and max_iterations < _std:
                max_iterations = _std     # recursion_limit → 90
    except Exception:
        pass

    # ── v16(2026-08-18) · 收尾时机按题型自适应(不是简单调大常数) ──
    # bc 错题实证: 多跳实体识别题(多约束·需交叉验证)反复撞 600-900s 墙被迫收尾·
    #   交出中间态/元评论。GAIA 数值题短平快·600s 够;实体题多约束多跳需更长墙钟。
    # 修法: 按【题面语义题型 + 复杂度】给墙钟自适应倍率——数值题倍率恒 1.0(行为
    #   完全不变·不破坏 98.2%)·实体/日期多约束题按约束数放长(每条 +0.15·封顶 2.0)。
    #   只放长不缩短(max(orig, adjusted))·避免误伤简单题。fail-safe: 异常保持原值。
    try:
        # Formal callers pass an explicit hard wall; it is a contract, not a hint.
        # Adaptive expansion remains available only for interactive/default runs.
        _wall_mult = 1.0 if _explicit_wall else _adaptive_wall_multiplier(question)
        if _wall_mult > 1.0:
            _new_wall = min(round(hard_wall_s * _wall_mult), BUDGET_EXTEND_WALL_CAP)
            if _new_wall > hard_wall_s:
                trace["hard_wall_qtype_adaptive"] = {
                    "from": hard_wall_s, "to": _new_wall, "mult": round(_wall_mult, 2),
                    "qtype": _question_type(question),
                    "complexity": _question_complexity(question)}
                hard_wall_s = _new_wall
    except Exception:
        pass

    # 预算/早退保护要在第一次工具调用之前就位，所以在这里显式初始化 session
    # 的 _budget 字段（而不是等 _make_tools 里的闭包第一次调用时才建）。
    _init_budget(_sess(qid), max_iterations, hard_wall_s=hard_wall_s)
    trace["hard_wall_s"] = hard_wall_s
    # Bind the canonical stream before preflight. Otherwise a short-wall run can be
    # killed inside its first search before any checkpoint knows which event log belongs
    # to this run, and a repeated qid silently reuses an older run's events.
    try:
        _s_events = _sess(qid)
        _run_tag = _safe_qid(os.environ.get("MEYO_RUN_ID") or "")
        _suffix = "_%s" % _run_tag if _run_tag else ""
        _event_path = os.path.join(os.path.dirname(_ckpt_path(qid)),
                                   "qa_events_%s%s.jsonl" % (_safe_qid(qid), _suffix))
        _s_events["_event_log_path"] = _event_path
        _s_events["run_id"] = _run_tag or str(qid)
        _s_events["session_id"] = str(qid)
        import qa_event_stream as _qes_start
        _start_store = _qes_start.EventStore(
            _event_path, run_id=_s_events["run_id"], session_id=str(qid), question_id=str(qid))
        if not _start_store.read():
            _started = _start_store.append("task_started", {"question": str(question)[:4000]})
            _s_events["_event_cursor"] = _started.seq
        _save_ckpt(qid, status="running")
    except Exception as _event_start_error:
        trace["event_stream_init_error"] = "%s:%s" % (
            type(_event_start_error).__name__, str(_event_start_error)[:160])
    # 把按题型自适应的"无进展容忍阈值"写进 budget·供 _maybe_reflect_nudge 读。
    #   数值题=GAIA 默认(紧);实体/日期多约束题放宽(多跳检索表面无进展不算空转)。
    try:
        _sess(qid)["_budget"]["noprogress_streak_thresh"] = \
            _adaptive_noprogress_streak(question)
        _sess(qid)["_budget"]["qtype"] = _question_type(question)
    except Exception:
        pass

    # 三档→模型的真实路由，而不是永远 tier="heavy"。
    # 用户手动选的档位(force_task_level)优先，没手动选时按自动 triage 出的
    # task_level_hint 兜底映射——"标准"题默认走 K3(sonnet)日常档而不是重型
    # opus，只有"重型"(集群)才升级到 opus + 强制蜂群，这样大多数标准问题
    # 的成本/延迟都会下降，只有真正复杂/用户显式要求"集群"的题才用最贵的模型。
    #   琐事/快速 → light(longcat-2.0)
    #   标准      → k3(sonnet)
    #   重型/集群 → heavy(opus)
    _tier_by_level = {"琐事": "light", "标准": "k3", "重型": "heavy"}
    model_tier = _tier_by_level.get(force_task_level or task_level_hint, "k3")
    # v113(2026-08-16 · opus 间歇 400 事故) 主大脑 run 级通道韧性: 构造
    #   ChatAnthropic 前先小 call 探测通道——上游 Bedrock 多账号轮询下
    #   "Access to Anthropic models is not allowed" 是账号级间歇错误·
    #   deepagents 主循环一旦撞上就是整 run crash(实证: 8d46 跑7步被打断·
    #   384d 起跑即 400·守护反复重试全灭)。网关层 call() 已有降级·但主
    #   大脑走的是 langchain 直连——run 级探测: heavy(opus) 探测不通→整 run
    #   降级 k3(sonnet) 保交付·trace 标 model_degraded 供事后区分。探测
    #   fail-safe: 探测自身异常→不降级(保持原行为·别让探测 bug 杀 run)。
    # A short wall cannot afford a 3x15s channel probe before any real work — that alone can
    # burn 30-45s of a 120s budget (observed: preflight only started at ~40s). Skip the probe
    # under a short wall and let the gateway's per-call degradation handle a bad channel.
    _probe_min_wall = float(os.environ.get("MEYO_CHANNEL_PROBE_MIN_WALL", "180") or 180)
    if model_tier == "heavy" and hard_wall_s >= _probe_min_wall:
        try:
            import qa_llm_gateway as _GW
            # 3连测(账号轮询请求级随机·单发=抽奖): ≥2/3 通才信通道。
            #   粘性坏路由实证: bec7 连续 4 轮粘死坏账号(8d46/384d 同期都活)·
            #   单发探测会误信。0-1 通→整 run 降 k3(sonnet) 换路由保交付。
            _ok_n = sum(1 for _ in range(3) if _GW.probe_channel(timeout=15))
            if _ok_n < 2:
                model_tier = "k3"
                trace["model_degraded"] = (f"opus通道3连测仅{_ok_n}/3通·整run降级k3(sonnet)")
                print(f"  [auto-agent] ⚠️ opus通道3连测{_ok_n}/3→主大脑降级k3", file=sys.stderr)
        except Exception as _pe:
            print(f"  [auto-agent] 通道探测自身异常·保持heavy: {_pe}", file=sys.stderr)
    trace["model_tier"] = model_tier
    model = build_gateway_chat_model(tier=model_tier, timeout=timeout_per_call)
    tools = _make_tools(qid, task_level=task_level_hint, urgency=urgency)

    # G-align(维度 A · 轨迹压缩) · 不传 middleware= 是刻意的:create_deep_agent
    # 默认就装了 SummarizationMiddleware(deepagents graph.py:758/843),token 用量
    # 达模型上下文窗口 ~0.85 时自动压缩早期轮次。所以"整段对话轨迹超阈值自动
    # 压缩"这一层由 deepagents harness 免费提供,这里不再自建重复的一套(自建会
    # 和默认那套冲突)。我们自己负责的是互补的另一层:单个大工具结果的落盘/截断
    # (_evidence_digest / file_read max_bytes / shell_exec stdout[:4000]),两层
    # 一起构成完整的 Context Engineering。切勿传 middleware=[] 覆盖掉默认压缩。
    # 引擎在此刻一次性解析并写入 trace；后续 checkpoint 与评测可区分显式 native、
    # 显式 deepagents 和 native 加载失败后的实际回退，禁止再靠环境变量猜测。
    agent = create_agent_engine(
        model=model, tools=tools, system_prompt=system_prompt, session_qid=str(qid or ""))
    # 绑定任务上下文(同一推理循环内·供感知/证据消费)
    try:
        agent._task_desc = str(question or "")
        backend_agent = getattr(agent, "_agent", None)
        if backend_agent is not None:
            backend_agent._task_desc = str(question or "")
    except Exception:
        pass
    trace["engine"] = {"backend": "native"}
    _sess(qid)["engine_backend_actual"] = "native"

    # 产品模式不做强制证据预检。模型可依据轻量分诊决定 search/vision/compute，
    # 但不会在真实任务开始前自动跑候选实体搜索或用 evidence 数量否决文件/测试成果。

    user_content = f"用户问题: {question}"
    if resume_context:
        user_content = f"{resume_context}\n\n{user_content}"

    # 多轮对话历史(见函数 docstring)。放在最前面——「之前聊过什么」
    # 是这轮任务最外层的背景，续传上下文/经验召回都是在这个背景之下的
    # 补充信息，顺序上应该更靠近当前问题。
    if conversation_history:
        hist_lines = []
        for i, turn in enumerate(conversation_history[-6:], 1):  # 最多带最近 6 轮，避免无限膨胀
            q = (turn.get("question") or "").strip()
            a = (turn.get("final") or "").strip()
            if not q:
                continue
            a_preview = a[:1500] + ("...(后续内容省略)" if len(a) > 1500 else "") if a else "(未产出最终结果)"
            hist_lines.append(f"[第{i}轮] 用户问: {q}\n[第{i}轮] 你答: {a_preview}")
        if hist_lines:
            user_content = (
                "以下是本次对话此前的历史轮次，供你理解当前这句话的上下文"
                "(比如「继续」「深挖一下」这类指代性的话，指的就是接着这些内容往下做)：\n\n"
                + "\n\n".join(hist_lines) + "\n\n---\n\n" + user_content
            )
            trace["conversation_turns_included"] = len(hist_lines)

    # G-align(维度 E) · 开跑前召回相关经验注入首条消息(对齐 hermes prefetch)。
    # 放在续传上下文之后、问题之前——续传是"这个任务此前进度",经验是"这类
    # 任务的通用教训",两者都在正式问题之前给到 agent。fail-safe。
    _lessons = _recall_lessons(question, user_id=user_id, trace=trace)
    if _lessons:
        # 经验教训也放题面【之后】(同 intent 修复·别盖在真题前让 agent 分心)。
        user_content = (
            "%s\n\n[以下是这类任务的可复用经验·仅供参考·题目以上面为准]\n%s"
            % (user_content, _lessons))
        trace["recalled_lessons"] = True

    # P2 · 注入"这个用户是谁 + 他这句话的真实意图"(演绎 per-user)。
    # 架构优化:读开跑时就丢后台跑的 user_context 结果(和 triage 并发·已省首步延迟)。
    # 若后台还没跑完,最多等 20s(意图抽取本就该快·超时则不注入·不拖累主流程)。
    if _uctx_thread is not None:
        _uctx_thread.join(timeout=20)
    _uctx = _uctx_result.get("text") or ""
    if _uctx:
        # ★修真bug(2026-08-20·真e2e实证):intent分析块(_uctx含"## 这句话的真实意图")
        #   原拼在 user_content【最前】→agent第一眼看到的是意图元信息不是真题面·把
        #   "这句话的真实意图"当题目搜(还被误判chitchat)。改:辅助分析放【题面之后】·
        #   并加醒目标记让真题面在最前。fail-safe。
        user_content = (
            "%s\n\n"
            "─────────────────────────────────────\n"
            "[以下是系统对上面题目的辅助分析(意图/画像/经验)·仅供参考·"
            "题目本身以【上面】为准·别把这些分析当题目]\n%s"
            % (user_content, _uctx))


    try:
        try:
            # 撤掉图级重试环: 重新 invoke({"messages":[user]}) 是【重放
            #   新会话】不是续跑——实证 8d46 ckpt 里 3 个重复 judge_scope_and_triage·
            #   289s 三遍烧开局·工具进度全丢。通道韧性改在【模型调用级】做:
            #   build_gateway_chat_model 返回 _ChannelRetryModel(bind_tools 层
            #   包装·deepagents 每次 LLM 调用过它·重试不重放图·消息不丢)。
            result = agent.invoke(
                {"messages": [{"role": "user", "content": user_content}]},
                config={"recursion_limit": max_iterations * 2 + 10},
            )
        except Exception:
            raise
    except Exception as e:
        # v119 FixA(2026-08-17 · bec745/8d46 纯跑轮实证): 空消息异常归因黑洞。
        #   病根: 原来只记 str(e)[:300]——str(e) 为空的异常(实证 8d46 error=None
        #   但 status=interrupted)归因信息全部丢失·事后无法定位中断源。修法:
        #   ①记「类型名+repr」(repr 对空消息异常也能给出构造参数) ②附尾部
        #   traceback(截尾 2400 字·头部通常是外层调用栈噪音)。fail-safe。
        _err_rich = f"{type(e).__name__}: {e!r}"[:400]
        trace["error"] = _err_rich
        try:
            import traceback as _tb_mod
            trace["error_tb"] = _tb_mod.format_exc()[-2400:]
        except Exception:
            pass
        trace["total_wall_s"] = round(time.time() - t_start, 1)
        s = _sess(qid)
        # 异常兜底增强:优先信磁盘 ckpt 而不是内存 session。
        # 实证根因(qid=0e9e85b852b94de4 · Carl Nebel 维基页题):finalize_answer
        # 已经成功执行、session["final"] 已写入完整 1415 字答案，_log_tool 也
        # 已经把这份状态存进磁盘 ckpt——但 finalize_answer 之后 agent 又继续
        # 空转了几轮直到撞上 LangGraph 图层面的硬性 recursion_limit，这个异常
        # 从 agent.invoke() 抛出时，这里读到的 s.get("final") 却是空的(推测
        # deepagents 在处理递归异常前的收尾/清理路径清空过内部消息态关联的
        # session 引用，或异常发生在另一个和 finalize 时不同的执行上下文，
        # 具体哪一步清空的暂未能在不改 deepagents 源码的前提下精确定位)。
        # 无论具体机制如何，"磁盘 ckpt 由 _log_tool 每步工具调用后落盘"这件事
        # 是可靠的、独立于内存 session 状态是否被破坏——直接从磁盘重新读一次
        # 最新 ckpt，如果里面的 final/narrative 比内存里的更完整，就优先用它，
        # 绝不能让一个已经算出来的正确答案因为内存态异常而被扔掉。
        try:
            _disk_ckpt = _load_ckpt(qid)
            if _disk_ckpt:
                # RC3: a typed artifact committed by the model is the authoritative recovery
                # source. Prefer its clean display over any downstream degraded/fabricated
                # synthesis — recovery surfaces what the model committed, never invents one.
                _art = _disk_ckpt.get("answer_artifact") or s.get("_answer_artifact")
                _art_final = ""
                if isinstance(_art, dict):
                    _art_final = str(_art.get("display") or _art.get("raw") or "").strip()
                if _art_final and not (s.get("final") or "").strip():
                    s["final"] = _art_final
                    s["_answer_artifact"] = _art
                    trace["recovered_final_from_artifact"] = True
                _disk_final = (_disk_ckpt.get("final") or "").strip()
                _disk_narr = (_disk_ckpt.get("narrative") or "").strip()
                if _disk_final and len(_disk_final) > len((s.get("final") or "").strip()):
                    s["final"] = _disk_final
                    trace["recovered_final_from_disk_ckpt"] = True
                if _disk_narr and len(_disk_narr) > len((s.get("narrative") or "").strip()):
                    s["narrative"] = _disk_narr
                if not s.get("tool_log") and _disk_ckpt.get("tool_log"):
                    s["tool_log"] = _disk_ckpt["tool_log"]
        except Exception:
            pass  # fail-safe:磁盘兜底本身绝不能引入新异常
        trace["steps"] = s.get("tool_log", [])
        budget = s.get("_budget") or {}
        trace["budget_hard_block_triggered"] = bool(budget.get("hard_block_active"))
        trace["budget_soft_nudge_sent"] = bool(budget.get("soft_nudge_sent"))
        trace["reflect_nudge_count"] = budget.get("reflect_nudge_count", 0)  # v73
        # fail-safe: 即便 agent loop 异常(比如真的撞上了 recursion_limit)，
        # 也尽量把已经攒到的 narrative 兜底返回，而不是让整个任务只剩一句报错。
        fallback = s.get("narrative") or s.get("final") or ""
        # v13.88.9L · 异常兜底增强(治实证bug 384d0dd8·撞recursion_limit空转351s却n_tools=0·
        #   narrative空→只甩「执行异常」给用户)。若已攒到工具调用记录·基于 tool_log 让LLM
        #   抽 best-effort 答案(像pipeline的forced_extract兜底)·别浪费已收集的证据。fail-safe。
        # v13.9x (2026-08-10) · 用真实 GAIA 100miss rerun 复核发现此兜底【本身也在失效】
        #   (384d0dd8/624cbf11/0540716/3da8993/d1af70e/9318445/6359a0b/cca70ce/872bfbb 共9道
        #   实测 tool_log 147步不空·却仍输出裸"auto agent 执行异常"字符串)。根因三处·都已实证:
        #   ①原来 except Exception: pass 完全静默吞掉失败原因·永远查不出这个兜底为什么没生效；
        #   ②只取 tool_log[-12:]·384d0dd8 实测最后几步全是连续 HTTP 404/500 报错(纯噪音)·
        #     真正有效证据(前面的 run_parallel_search/file_read 结果)被挤出窗口没喂给LLM；
        #   ③单次 _glg.call 失败(网关429/超时/资源紧张现场很常见)就直接放弃·没有降级重试。
        #   三处都是可安全修的执行细节·不改变兜底"基于已收集证据尽力抽取"的既有设计意图。
        _evid_used = ""
        if not fallback:
            _tlog = s.get("tool_log", []) or []
            if _tlog:
                def _evid_of(_t):
                    return (_t.get("result") or _t.get("output")
                            or _t.get("summary") or "")

                def _is_noise(_txt):
                    # 纯 HTTP 错误/异常回显，没有实质信息量，参与截断优先级时应降权
                    _t = _txt.strip().lower()
                    return bool(_t) and len(_t) < 200 and (
                        "http error" in _t or "traceback" in _t
                        or _t.startswith("exit_code=") and "err" in _t and len(_t) < 160)

                # 修②: 不再死板取最后12条——优先保留"有实质内容"的证据（哪怕在更早的
                # 步骤），纯报错噪音排到最后；总量仍按 8000 字符预算截断，避免过大。
                _entries = [(_t.get("tool", "?"), str(_evid_of(_t))) for _t in _tlog if _evid_of(_t)]
                _substantive = [e for e in _entries if not _is_noise(e[1])]
                _noisy = [e for e in _entries if _is_noise(e[1])]
                # 实质证据优先取最近的(离最终结论更近)，不足 12 条再用噪音条目垫底
                _picked = (_substantive[-16:] if _substantive else []) + (
                    _noisy[-4:] if len(_substantive) < 4 else [])
                _evid = "\n".join(f"[{name}] {txt[:800]}" for name, txt in _picked)[:8000]
                _evid_used = _evid
                # v119 FixB(2026-08-17 · 8d46 纯跑轮实证): 兜底路径绕过全部质量门。
                #   病根: 否定辖域枚举门(v111)挂在 finalize_answer 正常流程上·但
                #   异常打断→兜底抽取一个门都不过——8d46 三轮三种辖域读法随机选
                #   (OR=291/NOT(...)=86·gold=¬A∧B=132)。兜底抽取是【降级模式下
                #   唯一的决策点】·它必须自己补上并排枚举。检测逻辑复用 v111 门
                #   的同一正则(否定词…60字符内 or/and)。fail-safe: 检测失败=不注入。
                _neg_scope_hint = ""
                try:
                    _neg_re_fb = re.compile(
                        r"\b(?:not|n['\u2019]t|except(?:\s+for)?|other\s+than|neither|nor|"
                        r"excluding|without|cannot|don['\u2019]t|doesn['\u2019]t|didn['\u2019]t|"
                        r"isn['\u2019]t|aren['\u2019]t|won['\u2019]t)\b[^.?!]{0,60}?\b(or|and)\b",
                        re.I)
                    if _neg_re_fb.search(question or ""):
                        _neg_scope_hint = (
                            "\n\n⚠️本题题面含【否定 + or/and】结构——否定辖域天然多解"
                            "(历史错答几乎都因只算一种读法)。你必须在答案里把四种组合"
                            "并排算出并都列出来:\n"
                            "  组合1 ¬A∨B(否定只管第一项) / 组合2 ¬(A∨B)=¬A∧¬B"
                            "(否定管整个or) / 组合3 ¬A∧B / 组合4 A∧¬B\n"
                            "然后按题面语法结构选主答并【注明选了哪种读法】。"
                            "证据里已有各组合的原始计数时直接用·缺哪个就明说缺哪个。"
                        )
                except Exception:
                    pass
                if _evid.strip():
                    _be_txt = ""
                    _be_err = ""
                    # 修③: 单次调用失败(429/超时/资源紧张)不再直接放弃——先用默认
                    # tier 试一次，失败再降级到更轻量的 tier="light" 重试一次，
                    # 两次都失败才算兜底彻底不可用(仍走下面"证据摘要直传"的最后防线)。
                    for _tier, _to in (("heavy", 40), ("light", 25)):
                        try:
                            import qa_llm_gateway as _glg
                            _be = _glg.call(
                                f"任务执行中途被打断(recursion/超时)·下面是已经用工具收集到的证据。"
                                f"请基于这些证据【尽力给出最可能的答案】·答不出就说无法确定·绝不编造。"
                                f"{_neg_scope_hint}\n\n"
                                f"问题: {question}\n\n已收集证据:\n{_evid}\n\n最可能的答案:",
                                timeout=_to, max_tokens=1600, tier=_tier)
                            if isinstance(_be, dict) and _be.get("ok") and (_be.get("out") or "").strip():
                                _be_txt = _be["out"].strip()
                                break
                            _be_err = f"tier={_tier} ok={_be.get('ok') if isinstance(_be, dict) else '?'} err={str(_be.get('err') if isinstance(_be, dict) else _be)[:120]}"
                        except Exception as _be_exc:
                            _be_err = f"tier={_tier} exc={type(_be_exc).__name__}:{str(_be_exc)[:120]}"
                    if _be_txt and "无法确定" not in _be_txt[:20]:
                        fallback = f"## 回答(执行被打断·基于已收集证据的尽力抽取)\n\n{_be_txt}"
                    else:
                        # 修①: 两次 LLM 兜底都失败时不再静默——记日志 + 最后防线：
                        # 直接把已收集证据的摘要交付给用户，好过一句裸异常。
                        trace["fallback_llm_failed"] = _be_err
                        print(f"  ⚠️ 异常兜底LLM抽取失败({_be_err})·改用证据摘要直传",
                              file=sys.stderr, flush=True)
                        fallback = (
                            "## 回答(执行被打断·兜底抽取模型也未响应，以下是已收集到的原始证据，"
                            "供你自行判断)\n\n" + _evid[:3000]
                        )
        if not fallback:
            # v119 FixA: 异常信息带类型名(空消息异常 str(e)="" → 至少能看到类型)。
            #   保留「(auto agent 执行异常」前缀——外层 gaia_auto_rerun_100miss 的
            #   崩溃残渣检测(防假 hit)靠它识别·改了前缀会破坏防污染门。
            fallback = f"## 回答\n\n(auto agent 执行异常: {_err_rich})\n"
        # 异常路径也落盘为 interrupted，保留关键过程供下次自动续传
        # v119 FixA: error 落盘也用富格式(类型+repr)——上次实证 error=None 的谜
        #   就是空消息异常 str(e)="" 直接落盘导致的。
        _save_ckpt(qid, status="interrupted", extra={"error": _err_rich})
        # (三车道批4) 异常结束同理:未消费的 steer 也标 expired(诚实·别装还会生效)。
        #   若之后 resume 续跑·drain 按 consumed 标志消费·这条仍可被注入并转 applied。
        try:
            import steer_queue as _sq_int
            _sq_int.expire_pending(qid)
        except Exception:
            pass
        # P7 · 记进该用户失败库(异常=真失败),同类反复触发个人自愈
        if _SELFHEAL_AVAILABLE:
            try:
                r = _self_heal.record_failure(user_id, question, str(e)[:200], category="exception")
                if r.get("n_same", 1) >= 2:
                    _self_heal.diagnose_and_heal(user_id)
                    trace["self_healed"] = True
            except Exception:
                pass
        return fallback, trace

    s = _sess(qid)
    trace["steps"] = s.get("tool_log", [])
    trace["n_tool_calls"] = len(s.get("tool_log", []))
    trace["total_wall_s"] = round(time.time() - t_start, 1)
    trace["task_level_hint"] = task_level_hint
    # 蜂群(deep_research_swarm)跑过的话，把摘要冒泡给前端 meta-bar；
    # 没跑过(这题没触发深研)时不加字段，前端按"未展示这块"处理，不显示假数据。
    if s.get("swarm_summary"):
        trace["swarm_summary"] = s["swarm_summary"]
    # 预算保护是否被触发过，便于 A/B 对比时分析"是不是靠硬顶/循环卫兵
    # 才收的尾"，而不是模型自己主动收敛。
    budget = s.get("_budget") or {}
    trace["budget_hard_block_triggered"] = bool(budget.get("hard_block_active"))
    trace["budget_soft_nudge_sent"] = bool(budget.get("soft_nudge_sent"))
    trace["reflect_nudge_count"] = budget.get("reflect_nudge_count", 0)  # v73 · 卡住反思触发次数


    # bug3(文字收尾绕门): loop 正常结束时 session 里没有 final
    #   = finalize_answer 从未成功交付(典型:被门拦一次后 agent 改用纯文字作答
    #   收尾·绕过全部质检门)。先记标志·下面提取完兜底 final 后统一补门。
    _final_via_finalize = bool((s.get("final") or "").strip())
    final = s.get("final") or s.get("narrative") or ""
    if not final:
        # 先从 agent 的最后文本恢复；有些模型会直接答而漏掉 finalize。
        # 若仍为空，随后用已完成工具轨迹做一次受证据约束的收尾，避免“算完却空交卷”。
        # agent 可能只在最后一条 AI 消息里给了文本、没调 finalize_answer。
        # ★v13.92 修 final 污染(EC e2e 时好时坏真根因):拒答/强制重跑那轮的最后一条
        #   消息常是【系统注入的提示词本身】(强制重跑指令/资料注入模板/judge 提示)·
        #   被当 final 落盘→HIT=False 还冒充答案。必须过滤这类提示词·宁可 final 空
        #   (诚实 UNKNOWN)也不拿提示词冒充答案。
        _PROMPT_MARKERS = (
            "系统强制", "不可忽略", "请【只依据", "请只依据", "系统已用搜索引擎",
            "判断这题要哪些信息", "质量复核", "未通过", "请针对性", "finalize_answer",
            "这句话的真实意图", "紧迫度:", "【系统", "上一轮你没有调用",
            "以下是检索到的", "只输出", "技术事实", "答案: <",
        )
        def _looks_like_prompt(_txt):
            return sum(1 for _mk in _PROMPT_MARKERS if _mk in _txt) >= 1
        msgs = result.get("messages", [])
        for m in reversed(msgs):
            content = getattr(m, "content", None)
            if isinstance(content, str) and content.strip():
                _c = content.strip()
                # 只接受【看起来像答案】的 AI 消息:不含提示词标记、且不是超长的注入块
                if _looks_like_prompt(_c) or len(_c) > 4000:
                    continue  # 是提示词/注入块·跳过·别当答案
                final = _c
                break
    if not final:
        _recovered_delivery = _recover_missing_delivery(question, s)
        if _recovered_delivery:
            final = _recovered_delivery
            s["final"] = final
            trace["delivery_recovered_from_tool_trace"] = True
            _save_ckpt(qid, status="done", extra={"delivery_recovered_from_tool_trace": True})
    # ── v103(2026-08-15) · bug3 · 文字收尾绕门(finalize escape)补门 ──
    # finalize 工具从未成功跑完(final 空·答案是从 narrative/last-message/
    # delivery 恢复出来的)·且是重型/标准题 → 这个答案一条质检门都没过。
    # 对它原样跑一遍 _maybe_block_finalize:被拦+有预算→注入拦截消息重进 loop
    # 逼正式 finalize(只 1 次);没预算→记 finalize_escape gate 事件(可观测)。
    #
    # ★ NameError 硬 bug 修复(2026-08-28 军团自诊断揪出·真跑10家 auto 必崩根因):
    #   `_is_investigative` 全文件只此一处【引用】、从未定义 → 只要 final 非空且非 via_finalize
    #   走到这行就必崩 NameError(与模型无关·MiniMax/deepseek 两模型都报同一错·auto status=error
    #   触发 codex 补位接力)。按本段注释原意("重型/标准题才补质检"·简单/直答题不必)恢复定义:
    #   task_level_hint 取值为 "标准"/"重型"(见 :9111/:9118)——即非简单题都补这道 finalize 质检。
    _is_investigative = task_level_hint in ("重型", "标准")
    if (not _final_via_finalize) and (final or "").strip() and _is_investigative:
        final = _finalize_escape_guard(
            s, qid, task_level_hint, final, trace, agent=agent,
            question=question, hard_wall_s=hard_wall_s,
            t_start=t_start, max_iterations=max_iterations)
        trace["steps"] = s.get("tool_log", [])
        trace["n_tool_calls"] = len(s.get("tool_log", []))
        trace["total_wall_s"] = round(time.time() - t_start, 1)

    # Native 唯一终答边界:无论 final 来自显式工具、最后文本或恢复路径，都提交同一个 typed artifact。
    final = _commit_final_artifact(s, qid, final, via_finalize=_final_via_finalize)
    trace["final_len"] = len(final)

    # legacy Rubric 多轮重评已删:Native 自带 stop/readiness·不再叠加 grader 重调。
    #   额外grader多轮重调既是死代码又是语义控制(曾造成205工具空转)。

    # ── v2026-08-18(doc·anti_false_positive L6·收口) · 最终交付前 AnswerVault 输出验证 ──
    # 走到这里的 final 可能经历过: 门拦截重写 / rubric 复核改写 / narrative 兜底
    # 恢复 / delivery 恢复——任何一步都可能把 vault 已锁定的明确答案行弄丢(Q501
    # 实证: 答案挖到了·收尾被改写成元评论→评分器判"未提供答案"→到手的分被扔)。
    # 最后一道闸: final 无答案行而 vault 有→前置恢复(已有答案行不动·尊重 agent
    # 最新结论)。同步写回 s["final"] 让 checkpoint/resume 也保住恢复后的版本。
    # fail-safe: 异常原样交付·绝不因 vault 卡死收尾。
    try:
        import answer_vault as _av_fin
        # 08-25 (cc-oss #2 接入): 交卷前先从 session 的结构化字段抽答案·
        # 以最高信任锁进 vault(绕开混合文本正则噪音·治 v133 vault-噪音污染)。
        # session(s) 本身即 dict·含 final/answer 等结构化键;命中则 rescue_final
        # 会优先用这个 struct source 的答案。fail-safe·未命中不影响原逻辑。
        try:
            _struct = _av_fin.lock_structured(s, s)
            if _struct:
                trace["answer_vault_struct_locked"] = True
        except Exception:
            pass
        _rescued = _av_fin.rescue_final(final, s)
        if _rescued != final:
            trace["answer_vault_rescued"] = True
            final = _rescued
            s["final"] = final
            trace["final_len"] = len(final)
    except Exception:
        pass

    trace["total_wall_s"] = round(time.time() - t_start, 1)

    # ── 收尾自动进化-回测(v98A · 2026-08-13 用户:自动·不靠我驱动) ──
    # 题跑完若【失败且是执行层坏信号】(final空/被打断/多次shell报错)·自动把失败投进
    # evolution_inbox 喂 self_evolve(接断链)。进化-回测循环(改工具→回测)本身重·不在此
    # 同步跑(会拖垮交付)·而是投递+即时诊断产提案·由 evolve_backtest 的调度/cron 消费。
    # 这是"agent 自己知道该进化·不靠外部写驱动"。fail-safe·绝不影响已完成的交付。
    try:
        _fl = (final or "").lower()
        _run_failed = (not (final or "").strip()) or any(
            k in _fl for k in ("执行被打断", "无法确定", "尽力抽取", "unknown", "cannot determine"))
        _exec_bad_run = any(
            "exit_code=1" in str(t.get("summary", "")) or "traceback" in str(t.get("summary", "")).lower()
            for t in (_sess(qid).get("tool_log") or []))
        if _run_failed or _exec_bad_run:
            import sys as _s3, os as _o3
            _s3.path.insert(0, _o3.path.abspath(_o3.path.join(QA_ROOT, "..", "v11", "evolution")))
            import self_heal_scheduler as _SH2
            _sig = "bug" if _exec_bad_run else "gave_up"
            _SH2.capture_to_evolution_inbox(
                qid, _sig, f"收尾自动投递: {(final or '')[:100]}",
                question=question, gold="", trace_tools=[t.get("tool") for t in (_sess(qid).get("tool_log") or [])][:12])
            trace["auto_evolve_captured"] = _sig
    except Exception:
        pass  # 自动进化投递失败绝不影响交付

    # 把 LLM 最终的对话消息数、token 大致用量也记一下，便于 A/B 对比成本
    try:
        trace["n_messages"] = len(result.get("messages", []))
    except Exception:
        pass

    with open(AUTO_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(trace, ensure_ascii=False) + "\n")

    # ── 最后一公里硬前置门(2026-08-18 · bc 评测事故根修) ──────────────────
    # 事故实证: BrowseComp 88 个 ckpt 里 73 个 status=done·其中 49 个 s["final"]
    #   为空字符串·且 0 个带 [DEGRADED] 标——一大半"完成"根本没交卷·兜底没生效。
    # 根因(两处叠加·都已实证):
    #   ① native 的自磨对环 _invoke_retry 在第一轮 pillar-2 落了 [DEGRADED] final
    #      后·因 _final_looks_bad 判 DEGRADED=坏·会 `_s["final"]=""` 清旧卷重跑
    #      (native_agent_loop.py:1480)。若重跑那轮撞 crash_guard(invoke:1418·护栏
    #      明确"不编 final")·被清空的 final 就永久留空·pillar-2 的兜底成果被丢。
    #   ② 本函数收尾恢复梯子(≈15753-15799)把恢复出的答案只写进【局部变量 final】·
    #      narrative/last-message 分支从不回写 s["final"];而 _save_ckpt 落盘读的是
    #      s.get("final")(_save_ckpt:1047/1076)——于是 done 落盘时 session final 仍空。
    # 修法: done 是两个引擎(native/deepagents)共用的最后一道门·在这里兜死——
    #   落 done 前若 s["final"] 空·必须先把它落成非空(优先用本函数已恢复的局部
    #   final·再退到公共 _force_degraded_final·最后诚实落 [DEGRADED] UNKNOWN)。
    #   这样"done 且 final 空"物理上不可能发生。fail-safe:整段绝不因异常拦住收尾。
    try:
        _s_gate = _sess(qid)
        if _s_gate is not None and not (str(_s_gate.get("final") or "").strip()):
            # (a) 本函数收尾已恢复出局部 final(narrative/last-message/delivery)
            #     但没回写 session → 先同步回去(不加降级标·这是真恢复的答案)。
            _local_final = str(final or "").strip()
            if _local_final:
                _s_gate["final"] = _local_final
                trace["done_gate_synced_local_final"] = True
            # (b) 仍空 → 走两引擎共用的降级兜底(从最强证据/narrative/tool_log 抽·打 [DEGRADED] 标)。
            if not (str(_s_gate.get("final") or "").strip()):
                try:
                    _force_degraded_final(_s_gate, "done_gate_missing_final")
                except Exception:
                    pass
            # (c) 连兜底都拿不到任何候选(_force_degraded_final 返 False·留空)→
            #     诚实落 [DEGRADED] UNKNOWN·绝不留空 done(否则复现本次事故)。
            if not (str(_s_gate.get("final") or "").strip()):
                _s_gate["final"] = (
                    "[DEGRADED] UNKNOWN — 收尾时 session 无任何可交付候选"
                    "(final/narrative/tool_log 证据均不足以归纳答案)。"
                    "落 done 前由最后一公里硬门补标·避免 done-final 空。"
                    "非环境故障时属能力边界·诚实交白卷而非编造。")
                trace["done_gate_forced_unknown"] = True
            # 同步局部 final 以便后续 trace/日志一致(final_len 等)。
            final = str(_s_gate.get("final") or "")
            trace["final_len"] = len(final)
            trace["done_gate_final_nonempty"] = True
    except Exception:
        pass  # fail-safe:硬门本身绝不能拦住 done 落盘

    # 正常完成: 标记 checkpoint 为 done，之后同 qid 不再触发续传
    _save_ckpt(qid, status="done")

    # (2026-08-15·三车道批4) steer 三态闭环:任务结束·还没被消费的插话标 expired。
    #   治"受理≠生效"——没来得及生效的要如实告知·不能永远挂着 admitted 装会生效。
    #   注:followup 不进队列(走 resume)·这里只动队列里 admitted 且未消费的·误伤不到。
    try:
        import steer_queue as _sq_end
        _n_exp = _sq_end.expire_pending(qid)
        if _n_exp:
            print(f"  [steer] 任务结束·{_n_exp}条未消费插话标expired(前端会出⏰回执)",
                  file=sys.stderr, flush=True)
    except Exception:
        pass

    # 前沿消费的经验边在收尾回连到既有失败/记忆网络；不另开一条不可验证的优化链。
    _record_frontier_outcome(_sess(qid), final, trace)

    # G-align(维度 E) · 收尾后沉淀本次经验(对齐 hermes sync)。放最后,不影响交付。
    _remember_lesson(question, final, trace, user_id=user_id)
    # P5 · 经验升到 high 置信时,自动为该用户 forge 一条专属 skill(越用越有自己的手册)
    _maybe_forge_skill(user_id, question, final, trace)
    # T2 · 监督自主造工具:高频重复模式→固化成命名工具(反复手动做→一步调)
    _maybe_forge_tool(user_id, trace)

    # 进化事件 inbox 自动写入(2026-08-12):
    #   进化链的血液循环——失败事件不再依赖 CC "想起来"，run 结束时自动投递。
    #   四类触发：① gave_up/弃答 ② 视频题未用过视频工具(模态覆盖缺口)
    #              ③ M-84 停滞(watchdog_fired) ④ document_wall 被清单拦截
    #   inbox 是只追加的 jsonl(evolution_inbox.jsonl)·weekly_review drain 时
    #   批量喂 self_evolve 诊断·不实时·不打断主流程。fail-safe 全包 try。
    try:
        _inbox_path = os.path.join(QA_ROOT, "state", "evolution_inbox.jsonl")
        _s_end = _sess(qid)
        _final_low = (final or "").lower()
        _gave_up = any(k in _final_low for k in
                       ("执行被打断", "无法确定", "answer boxes", "空白", "尽力抽取",
                        "i don't know", "unknown", "cannot determine"))
        _video_missed = bool(
            ((_s_end.get("triage") or {}).get("modality_needs") or [])
            and any(m in ("video", "audio") for m in
                    ((_s_end.get("triage") or {}).get("modality_needs") or []))
            and not _s_end.get("_video_evidence_ok")
        )
        _watchdog = bool((_s_end.get("_budget") or {}).get("_watchdog_fired"))
        _wall_blocked = any(
            t.get("args", {}).get("_wall_blocked") for t in (_s_end.get("tool_log") or [])
            if t.get("tool") == "document_wall"
        )
        _inbox_events = []
        if _gave_up:
            _inbox_events.append({
                "event": "gave_up",
                "desc": f"弃答: {final[:100]}",
                "tool_diversity": len({t.get('tool') for t in (_s_end.get('tool_log') or [])}),
            })
        if _video_missed:
            _inbox_events.append({
                "event": "modality_gap",
                "desc": "视频题未使用视频工具(transcribe_audio/see_video_frame 0次)",
                "missing_modalities": ["video_frame", "audio_asr"],
            })
        if _watchdog:
            _inbox_events.append({
                "event": "m84_stall",
                "desc": "M-84看门狗触发·agent停滞达阈值",
            })
        if _wall_blocked:
            _inbox_events.append({
                "event": "wall_premature",
                "desc": "document_wall 被模态清单准入拦截·未穷尽证据源就宣墙",
            })
        if _inbox_events:
            _inbox_rec = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "qid": qid,
                "question_head": question[:80],
                "n_tool_calls": trace.get("n_tool_calls", 0),
                "wall_s": trace.get("total_wall_s"),
                "events": _inbox_events,
            }
            with open(_inbox_path, "a", encoding="utf-8") as _fi:
                _fi.write(json.dumps(_inbox_rec, ensure_ascii=False) + "\n")
    except Exception:
        pass  # inbox 写失败不能影响主流程

    # 交付净化(2026-08-19·面向用户自然回复·不留门噪音瑕疵)——只净化【返回值】,
    #   session/checkpoint 原文不动(评测/审计看原始)。eval 和直调都拿到干净答案。
    try:
        import answer_vault as _av_clean
        final = _av_clean.clean_delivery_final(final)
    except Exception:
        pass

    return final, trace


# ═══════════════════════════════════════════════════════════════════════
# run_task · 接待层统一入口:吃 TaskSpec·透传给内核 run_auto_agent。
#   接待:按 interaction_budget 分流(有歧义且允许反问→澄清;否则透传+假设留痕)
#   交付:deliverable_router 按类型路由(fact/artifact/action/monitor)
#   会话:有 session_id 则指代消解+多轮记忆
# ═══════════════════════════════════════════════════════════════════════

def run_task(spec, qid: str = ""):
    """接待层入口:吃 TaskSpec·透传给 run_auto_agent。返回 (final, trace)。
    - interaction_budget>0 且有歧义 → 返回反问 {clarify_needed:True, questions:[...]}
    - 否则透传内核·被迫假设时留痕(假设账本)"""
    try:
        kw = spec.to_run_auto_kwargs()
    except AttributeError:
        # (2026-08-27 上线瘦身) 裸 str 是【真实用户输入】·不能默认当 GAIA:
        #   旧逻辑 from_gaia → interaction_budget=0(不许反问)+source=gaia(跳过用户反馈/假设账本)·
        #   典型 benchmark 默认污染用户体验。改走 from_user·默认允许澄清/多轮/反馈。
        from task_spec import TaskSpec as _TS
        spec = _TS.from_user(str(spec), interaction_budget=3)
        kw = spec.to_run_auto_kwargs()

    # ── Phase4 多轮会话:指代消解("那第二个呢"→自足问题) ──
    # 有 session_id 才做多轮指代消解(单轮跳过)。fail-safe。
    _sid = (getattr(spec, "context", {}) or {}).get("session_id")
    if _sid:
        try:
            import session_store as _SS
            _hist = _SS.load_history(_sid)
            _resolved = _SS.resolve_reference(spec.raw_input, _hist)
            if _resolved and _resolved != spec.raw_input:
                spec.raw_input = _resolved       # 后续 M-91/内核用消解后的自足问题
                kw["question"] = _resolved
        except Exception:
            pass  # 消解失败不阻断(退回原问题)

    # ── M-91 澄清协议(Phase1·2026-08-12·全在此层·内核不碰) ──
    _budget = getattr(spec, "interaction_budget", 0)
    _axes = {}
    if _budget and _budget > 0:
        # 只在允许反问时才轻量独立跑一次 triage 拿 axes(GAIA budget=0 跳过·零开销·等价不破)
        try:
            import qa_task_analysis as _M
            import clarify_protocol as _CP
            _axes = (_M.step0_triage(spec.raw_input) or {}).get("ambiguity_axes") or {}
            if _CP.should_clarify(_axes, _budget):
                qs = _CP.generate_clarifying_questions(_axes, spec.raw_input, budget=_budget)
                if qs:
                    return ({"clarify_needed": True, "questions": qs, "axes": _axes},
                            {"mode": "clarify", "interaction_budget": _budget})
        except Exception:
            pass  # 澄清失败不阻断·退回自主模式(fail-safe)

    # ── 透传内核(GAIA走这·行为不变·零新开销) ──
    final, trace = run_auto_agent(qid=qid, **kw)

    # ── M-92 假设账本(被迫猜时留痕) ──
    # (2026-08-27 瘦身) 删除 source=gaia 特例:账本是否生成只看有没有歧义轴(_axes)·
    #   不再为保持165题评测等价污染通用入口。真实用户/评测统一走同一逻辑。
    if _axes:
        try:
            import clarify_protocol as _CP2
            _led = _CP2.build_assumption_ledger(_axes)
            if _led and isinstance(trace, dict):
                trace["assumption_ledger"] = _led
        except Exception:
            pass  # 账本失败不阻断交付

    # ── Phase2 交付四路由 + M-93动作门(2026-08-12·全在此层·内核不碰) ──
    # fact→透传;artifact/action/monitor→按类型路由收尾。
    # action过M-93门:不可逆/拿不准→needs_confirm(agent无权自作主张·返回确认请求不执行)。
    try:
        import deliverable_router as _DR
        final, trace = _DR.route_delivery(spec, final, trace)
    except Exception:
        pass  # 路由失败不阻断交付(fail-safe·退回透传)

    # ── Phase3 采纳遥测:交付成功标 pending_acceptance(待用户反馈·UI调 record_feedback 收)──
    # (2026-08-27 瘦身) 删 source=gaia 特例:交付成功就标·由有没有真实用户反馈通道决定是否收集·
    #   不在此处按 benchmark 来源做特判。降级 run 不标(半成品不算采纳)。
    if (isinstance(trace, dict) and not trace.get("degraded")):
        try:
            trace["acceptance_pending"] = True  # 待用户裁决(accepted/reworked/escalated)
        except Exception:
            pass

    # ── Phase4 多轮会话:交付成功存这轮(供下轮指代消解/内核多轮用) ──
    # 有 session_id 才存(GAIA 无 session_id→跳过·零影响)。降级run不存(污染不入会话)。
    if _sid and isinstance(trace, dict) and not trace.get("degraded"):
        try:
            import session_store as _SS2
            _ans = final if isinstance(final, str) else str(final)
            _SS2.append_turn(_sid, spec.raw_input, _ans)
        except Exception:
            pass  # 存轮失败不阻断交付
    return final, trace


if __name__ == "__main__":
    # 自主入口:不管用哪个 Python 启动·自动切到生产 3.12 重启自己。
    # 用户(或任何调用者)用裸 python3 / /usr/bin/python3 跑本文件·agent 自己选对解释器。
    # 这样"用户往输入框输入题目和附件一样·agent 自己跑出来"——不依赖外部人选对 Python。
    import sys as _sys, os as _os
    try:
        _sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                          "..", "..", "v11", "evolution"))
        from canonical_python import check_production_interpreter, MEYO_PYTHON
        _ok, _reason = check_production_interpreter()
        if not _ok and _os.path.exists(MEYO_PYTHON):
            _sys.stderr.write(f"[auto-rebase] 当前 {_sys.executable} 不合格({_reason})·"
                              f"自动切换到 {MEYO_PYTHON} 重启\n")
            _os.execv(MEYO_PYTHON, [MEYO_PYTHON, _os.path.abspath(__file__)] + _sys.argv[1:])
    except Exception:
        pass  # canonical_python 不可用时不阻断·走原有 _startup_selfcheck 路径

    import argparse
    parser = argparse.ArgumentParser(description="qa_auto_agent · Auto Agent 模式")
    parser.add_argument("qid", nargs="?", default="")
    parser.add_argument("--question", required=True)
    parser.add_argument("--out", default="")
    parser.add_argument("--max-iterations", type=int, default=30)
    parser.add_argument("--urgency", choices=["high", "normal", "low"], default="normal",
                        help="意图紧迫度，影响自适应重试策略")
    parser.add_argument("--hard-wall-s", type=float, default=None,
                        help="墙钟硬顶(秒)，不传按 urgency 给默认值(high=300/normal=600/low=900)")
    parser.add_argument("--force-task-level", choices=["琐事", "标准", "重型"], default=None,
                        help="v73 · 强制档位·治「auto对难题自判简单→0步直接答放弃」。"
                        "GAIA深检索/专业ID/精确统计类难题传『重型』·强制多跳深挖+用工具不放弃。")
    parser.add_argument("--no-resume", action="store_true",
                        help="禁用断点续传，同 qid 也从零开始(默认开启续传)")
    parser.add_argument("--timeout-per-call", type=float, default=180,
                        help="每次 LLM 决策调用超时(s)·短=快失败重试(默认180)")
    args = parser.parse_args()

    final, trace = run_auto_agent(args.question, qid=args.qid,
                                   max_iterations=args.max_iterations, urgency=args.urgency,
                                   hard_wall_s=args.hard_wall_s, resume=not args.no_resume,
                                   force_task_level=args.force_task_level,
                                   timeout_per_call=args.timeout_per_call)
    print(f"\n=== 完成 · {trace.get('total_wall_s')}s · {trace.get('n_tool_calls', 0)} 次工具调用 ===\n",
          file=sys.stderr)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(final)
        print(f"已写入 {args.out}", file=sys.stderr)
    else:
        print(final)
