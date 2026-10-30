# -*- coding: utf-8 -*-
"""qa_task_analysis · 任务分析小工具(2026-08-27 从 qa_agent_main 旧 pipeline 剥离)。

auto 只借 qa_agent_main 这几个函数(step0_triage 判难度/歧义轴·_compress_query 压缩query)·
不用它的 orchestrate 固定流水线。剥出来切断 auto→旧pipeline 耦合·让旧 qa_agent_main
可退出 auto 生产链。依赖 qa_llm_gateway(独立底层模块)。
"""
import os, re, json
from qa_llm_gateway import call as _gateway_call

_QUERY_BOILER_RE = re.compile(
    r"(Your response should be in the following format|"
    r"Explanation[:：]\s*\{|Exact Answer[:：]|Confidence[:：]\s*\{|"
    r"你的回答应按以下格式).*$",
    re.DOTALL | re.IGNORECASE)
_EN_QSTOP = frozenset("""
the a an and or of to in on for with by from at as is are was were be been being
do does did can could should would will shall may might must have has had
i am looking you your me my we our they them their he she it this that these those
what which who whom whose when where why how tell give find identify know name
please also between during while about into over under above below there here
person people someone anyone complete completed published announced awarded elected
met meets meeting only just than then some any many much more most other another
can could would like want need looking search find list compiled diverse genres
response answer format explanation confidence question details following succinct
""".split())
_ZH_QSTOP = "的了吗呢吧啊呀哦嘛么是都有着了把被对于因为在和与及或者如果虽然但是"


def _call_llm(model, prompt, timeout=120, system="", capture_first_byte=False):
    """统一网关调用。v56h · model 参数复活: "longcat-2.0" → 走 longcat-2.0(判决/预筛类·省CPU)·
    其余走 opus(写作)。之前 model 被吞·所有号称 longcat-2.0 的轻步骤全用 opus(CPU 89%根因)。"""
    # gateway 不提供 token 流事件，因此以首个完整响应耗时作为首字节近似值。
    _tier = "light" if str(model).lower() in ("longcat-2.0", "longcat-2.0", "light", "sonnet") else "heavy"
    return _gateway_call(prompt, timeout=timeout, system=system, tier=_tier)

def _parse_json(text):
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {}

def _cjk_ratio(s: str) -> float:
    if not s:
        return 0.0
    n = sum(1 for c in s if '\u4e00' <= c <= '\u9fff')
    return n / max(1, len(s))

def step0_triage(question):
    """v44 · 闸门 0 意图分诊 (用户 2026-07-24 抓「三闸门融合」)

    输出三元组:
      - task_level: 琐事 / 标准 / 重型
      - search_route: no_search / verify / heavy
      - scenario_gap: 0-2 (关键场景缺口数·>0 自动触发 qa_clarify)
      - task_intent_type: 独立字段·qa_action_cone 用 (methodology/tool_selection/etc)
      - direct_answer: 若 task_level=琐事 · longcat-2.0 直接给答案·跳所有闸门
    """
    # A方案第二刀(triage轻判前置)已试并回退:实测轻判净负——longcat 单次调用底噪 5-7s(tier=light
    # 模型速度·非prompt大小)·"先轻判再可能全套"变双倍成本(JTBD 13→22s)·且有误判风险(JTBD被判
    # 非琐事)。triage 真瓶颈是 longcat 模型速度·要提速须换更快模型(动网关 tier 映射·风险大·收益
    # 仅省几秒)——不值。保留 A 第一刀(direct_answer 短路·净收益 45→20s)。triage 维持单次全套。
    prompt = f"""判用户问题的意图分诊·输出三闸门参数。

题: {question}

# 三元组判决

## 1. task_level (决定后续闸门流程)
- 琐事: 【严格】纯概念解释·答案稳定不变·且绝不涉及具体代码/命令/工具实现 (例: 什么是 JTBD)
  ⚠️ 只要题目要「具体可执行的代码/命令/配置/技术实现/正确产出」→ 绝不是琐事·必须搜索
- 标准: 需要框架/对比/方法论/技术方案 · 一轮搜索够 (例: 续费访谈怎么设计/工具选哪个/某报错怎么修)
- 重型: 系统设计/多场景/技术难题/需要立体验证 · 需要多轮搜索 (例: 从零搭调研体系/冷门工具用法/polyglot代码)

⚠️ 搜索优先原则: 拿不准就判「标准+heavy」走搜索·别图省事判琐事。
   技术类题(代码/命令/工具/报错/实现/解题) 默认至少「标准」·几乎不判琐事。

## 2. search_route (搜索路由)
- no_search: 完全不用搜 · 概念解释/稳定知识
- verify: 需 1-2 查证 · 但主体靠通识 · 举例数据核对
- heavy: 完整多 agent 搜索 · 需要立体证据

## 3. scenario_gap (场景缺口·0-2)
- 0: 场景已明 (题里带角色 · 规模 · 阶段等) 无需澄清
- 1: 缺 1 个关键维度 (只缺角色 · 或只缺规模)
- 2: 缺 2 个关键维度 (角色+场景 · 场景+阶段)
- 上限 2 · 更多用户嫌烦

## 4. task_intent_type (工具锥类型 · 独立)
- methodology: 方法论/怎么设计 → 工具=脚本/SOP/追问树
- tool_selection: 工具选型 → 工具=推荐 + 部署链接
- decision: 决策题 → 工具=checklist/decision tree
- concept: 概念解释 → 无工具
- process: 系统设计 → 工具=agent 架构

## 5. direct_answer (只在 task_level=琐事时给·否则 null)
- 200 字内直答·代替走完整 8 step 流程

## 5b. constraints (这题的【口径约束】·你语义抽取·算之前必核对·防口径错位)
   题目里有没有限定: 时间窗(pre-2020/近30天)、分母/总体定义(占谁的百分比·总体是啥)、
   过滤条件(剔除退款/按人去重/某岛屿除外)? 抽成清单。算数据题时·先核对你用的数据范围是否
   匹配这些约束·不匹配就重新过滤·别用默认全量直接算(实证: 企鹅占比分母用错344行、
   OpenReview时间窗pre-2020算成pre-2022·都是口径没对准就算)。没约束填[]。

## 5c. ambiguity_axes (这题的【语义歧义轴】·★逐轴强制判定·防题意读偏)
   实证: 一批错题不是不会做·是【题意读偏了】。开局就把下面每条轴显式想清楚·填一句话
   (与题目无关的轴填""·别硬凑)。这不是套词表·是逐轴问自己"这题在这条轴上到底要哪个":
   - entity: 【问题指的到底是哪个实体·唯一确定吗】? 哪个游戏/哪个人/哪个"2019"(颁奖年vs发行年)?
     ★这是最上游的歧义——实体选错·后面怎么算都白算·先锁对实体再谈计数。(实证GoW:
     "2019 game won BAFTA"·agent锁定God of War(2019颁奖/2018发行)算477·题意其实是"2019
     发行的游戏"=Outer Wilds·gold=60。别在错实体上精算。)
   - time: 时间/区间是【累计还是单期】? 哪个时点/版本? (实证GoW: 算成"发布前累计477次修订"·
     题意其实是"发布月那一个月=60次")
   - set: 集合是【交/并/补】? De Morgan方向对吗? (实证企鹅: "不住Dream岛 或 喙>42mm"的补集·
     别算成"不住Dream 且 喙≤42")
   - version: 数据源用【哪个版本/哪张表】? (实证火车: 读了周末表·但5/27是周一该读工作日表)
   - perspective: 【谁的max/谁的min】? maximin还是别的? (实证Bob: 算成maximin保底·题意是另一机制)
   - aggregate: 多个片段【如何排序/去重/合并】? (实证分数图: 多次读取捏合成错顺序)
   - nominal: 标注者可能用了【简化口径】吗? (实证Freon: gold按理想气体·精确算反而"错")
   求解/交卷前对照这些轴核对一遍·发现读偏立即改。

## 6. modality_needs (这题本质需要什么【模态能力】·你自己判断·不是套词表)
   想清楚: 要拿到答案·本质需要「看画面/听音频/精确数值计算/看图识别/查文字资料」哪一种?
   - vision: 需看图像/视频某帧的**画面内容**(搜文字搜不到画面里有什么)
   - audio: 需听音视频**说了什么**(转录)
   - compute: 需精确数值计算/建模(该写代码算·非估)
   - text: 查公开文字资料就够(默认)
   一题可多个。这不是让你套关键词·是理解「这题要拿到答案·靠搜文字够不够·还是必须换个模态的工具」。

# 严格 JSON
{{
  "task_level": "琐事|标准|重型",
  "search_route": "no_search|verify|heavy",
  "scenario_gap": 0-2,
  "task_intent_type": "methodology|tool_selection|decision|concept|process",
  "constraints": ["口径约束清单·如'时间窗=pre-2020','分母=总企鹅population','剔除Dream岛'"] 或 [],
  "ambiguity_axes": {{"entity": "", "time": "", "set": "", "version": "", "perspective": "", "aggregate": "", "nominal": ""}},
  "modality_needs": ["text"] 或 ["vision"] 或 ["audio","compute"] 等,
  "unknown_entities": ["题里你【不确定是什么】的专有名词/产品/术语·如'Cheater Beater','Freon-12','GameGrumps'"] 或 [],
  "problem_archetype": "若这题是某类【有标准解法】的经典问题·归类它(如'博弈论-阶梯Nim','组合数学-穷举','热力学-物性计算','几何-多边形面积')·不确定填''",
  "direct_answer": null 或 "200 字内直答",
  "reason": "30 字为啥这么判"
}}
# unknown_entities/problem_archetype 说明(通用·非背答案):
#   unknown_entities = 你不认识、需先查"它到底是什么"才能解的名词(不是让你猜它是什么·是标出该先查的)。
#   problem_archetype = 只做【归类】(这题像哪类经典问题)·不给具体答案。目的:让 agent 先查
#   "这类问题标准怎么解"再动手·而非硬猜。判据:换任何同类题都通用——是通用引导·不是背本题答案。
只输 JSON:"""
    r = _call_llm("longcat-2.0", prompt, timeout=25)
    d = _parse_json(r["out"]) or {}
    # 根因1(2026-08-12·治静默默认掩盖失败): _parse_json 失败(d为空)时·"LLM说没约束"
    # 和"LLM调用整个失败"在旧代码里是同一个信号(都走 or []/or {})。加 triage_status
    # 区分二者·并响亮告警(不崩·仍走默认兜底保 fail-safe)——让上层/自检能看到"这题
    # 的 triage 其实没成·axes/constraints 的空不是真空"。
    _llm_failed = not d
    if _llm_failed:
        try:
            print(f"[triage_status=llm_failed] step0_triage 的 LLM 调用/解析失败·"
                  f"axes/constraints 走默认空值(不是真的没约束)·out_head={str(r.get('out',''))[:80]!r}",
                  file=sys.stderr)
        except Exception:
            pass
    return {
        "task_level": d.get("task_level", "标准"),
        "search_route": d.get("search_route", "heavy"),
        "scenario_gap": min(2, max(0, int(d.get("scenario_gap", 0)))),
        "task_intent_type": d.get("task_intent_type", "methodology"),
        "constraints": d.get("constraints") or [],
        "ambiguity_axes": d.get("ambiguity_axes") or {},  # G1(2026-08-12)·漏透传就等于没接
        "modality_needs": d.get("modality_needs") or ["text"],
        "unknown_entities": d.get("unknown_entities") or [],  # 2026-08-14·实体消歧:先查陌生实体
        "problem_archetype": d.get("problem_archetype") or "",  # 2026-08-14·题型归类:先查标准解法
        "direct_answer": d.get("direct_answer"),
        "reason": d.get("reason", ""),
        "triage_status": "llm_failed" if _llm_failed else "ok",  # 根因1·响亮区分"无"vs"失败"
    }


# ═══════════════════════ Step 1 · Cost-first 判决 ═══════════════════════

def _compress_query(question, max_tokens=12, max_chars=90):
    """把任意长题(含模板尾巴)压成干净短查询。压缩不动就不伤短题(fail-safe)。"""
    q = _QUERY_BOILER_RE.sub("", str(question or "")).strip()
    if _cjk_ratio(q) > 0.15:  # 中文题: 去停字+标点切段取内容最长的两段拼
        segs = [s.strip() for s in re.split(r"[··,。；;！!？?\n\t]+", q) if s.strip()]
        keep = []
        for s in segs:
            t = "".join(c for c in s if c not in _ZH_QSTOP)
            if len(t) >= 4:
                keep.append(t)
        out = " ".join(keep)[:max_chars]
        return out or q[:max_chars]
    # 英文题的判别性排序(2026-08-24 跨框架 trace 根因):
    #   deepseek 命中冷门题靠「精确引号短语 anchor + 专有名词」;auto 旧序把通用关系词
    #   (distance/church/theater/northeast…)排最前,搜索引擎当成「电影院/城堡旅游」关键词
    #   返回 SEO 噪音(instagram/facebook),证据 0 命中 gold。修正判别性优先级:
    #   引号精确短语 > 专有名词(实体 anchor) > 数字+相邻单位 > 稀有长内容词 > 通用关系词(仅补位)。
    #   关系词本身在任何题里都高频、低判别,只有在更强的 anchor 不足时才作填充。
    quotes = re.findall(r'"([^"]{3,60})"', q)
    # 判别性数字:4 位年份、带单位的量、数字区间(600 and 1100)。孤立无单位单数字
    # (如从 "1 kilometer" 拆出的 "1")跨题高频、零判别,过滤掉。不硬编码具体数值。
    keep_numeric = set()
    for m_num in re.finditer(r"\b(\d{4})\b", q):            # 年份
        keep_numeric.add(m_num.group(1))
    for m_num in re.finditer(r"\b(\d+(?:\.\d+)?)\s*[-–]?\s*(?:and\s+)?(\d+(?:\.\d+)?)?\s*"
                             r"(?:m|km|meters?|metres?|kilometers?|kilometres?|miles?|ft|feet)\b",
                             q, re.I):                       # 带单位的量 / 区间
        keep_numeric.add(m_num.group(1))
        if m_num.group(2):
            keep_numeric.add(m_num.group(2))
    toks = re.findall(r"[A-Za-z][A-Za-z'\-]+|\d+(?:\.\d+)?", q)
    # 通用空间/关系词:跨题高频、低判别。不硬编码任何 benchmark 实体,只列语言层面的
    # 泛化方位/距离/设施类停用型内容词,命中即降权到补位。
    relation_terms = {
        "distance", "walking", "meters", "metres", "kilometer", "kilometre",
        "kilometers", "miles", "north", "south", "east", "west",
        "northeast", "northwest", "southeast", "southwest", "entrance",
        "nearby", "near", "further", "away", "within", "between", "sea",
        "theater", "theaters", "theatre", "theatres", "church", "castle",
    }
    seen, proper, numeric, content, relational = set(), [], [], [], []
    quoted = [f'"{t}"' for t in quotes[:2]]
    for t in toks:
        tl = t.lower()
        if tl in _EN_QSTOP or tl in seen:
            continue
        seen.add(tl)
        if t.replace(".", "").isdigit():
            if t in keep_numeric:                            # 只保留判别性数字
                numeric.append(t)
        elif t[0].isupper():
            proper.append(t)
        elif tl in relation_terms:
            relational.append(t)
        else:
            content.append(t)
    # 精确短语与实体 anchor 最判别;通用关系词只在前面凑不满 max_tokens 时补位。
    picked = (quoted + proper + numeric + content + relational)[:max_tokens]
    out = " ".join(picked)[:max_chars]
    return out or q[:max_chars]
