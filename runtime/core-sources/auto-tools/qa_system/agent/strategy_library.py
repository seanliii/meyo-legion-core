"""strategy_library · 2026-08-10 · 顶尖问题解决者的策略库(用户: 像IQ200+天才科学家/研究者那样换策略思考)

来源=公认方法论(Polya《How to Solve It》/笛卡尔《方法论》/费曼学习法/TRIZ/first-principles/
科学方法),非实时事实无需联网验证。agent遇卡(策略不奏效)时按【卡点类型】检索适配策略·
像人一样"当前路走不通→换结构性不同的路"。

核心哲学(区别于蛮力重复): 卡住不是"再试一次同样的",而是"换一个维度/表征/角度"。
"""

# 策略库: 每条=一个可操作的策略·带触发场景(什么卡点用它)。
_STRATEGIES = [
    # ── 表征转换类(换一种方式表达问题) ──
    {"name": "换表征", "when": ["图", "看不清", "结构", "抽象", "格式", "生成出错", "识别"],
     "how": "把问题换一种表征: 图→逐格/逐行结构化清单(如棋盘→逐格JSON), 文字→表格/方程, "
            "整体→分块。VLM'看图列清单'比'生成精确格式串'稳。", "src": "认知科学·表征转换"},
    {"name": "局部到整体·整体到局部", "when": ["图", "看不清", "长文档", "信息密集", "识别", "一次处理"],
     "how": "先看整体定位边界/结构(棋盘方向、文档目录),再逐块精读局部,最后拼回整体交叉验证。"
            "放大/切片复杂图片后单独识别每块。", "src": "系统分解·Polya分解重组"},
    # ── 第一性原理类(回到本质) ──
    {"name": "第一性原理", "when": ["套公式", "带偏", "不合常识", "量级", "假设", "本质"],
     "how": "拆到不可再拆的基本事实/物理定律,从头推导。别套现成模板。质疑每个隐含假设"
            "(题目预期的是简化模型还是精确模型?单位对吗?量级合理吗?)。", "src": "第一性原理·费曼"},
    {"name": "量纲/量级检查", "when": ["数值", "算错", "计算", "结果存疑", "单位"],
     "how": "算完先查量纲一致(kg vs g)、量级合理(差1000x?)、单位换算对。用另一路径复算对拍。",
     "src": "物理·Fermi估算"},
    # ── 换渠道/换工具类 ──
    {"name": "换渠道找资料", "when": ["搜索", "无果", "拒答", "找不到", "数据源", "检索"],
     "how": "换搜索引擎/换关键词(实体前置+去缩写)/换数据源(Wikipedia→专业库API→原始文档)。"
            "被拒答的题:先中性提取事实再求解(决策脑只见中性内容)。", "src": "信息检索·多渠道"},
    {"name": "造/装工具", "when": ["缺", "专业", "计算", "手算", "算错", "工具"],
     "how": "缺什么工具就pip装/写脚本(CoolProp算物性、stockfish算棋、python-chess处理FEN、"
            "sympy解方程)。别硬用LLM心算专业计算。缺工具先想'能不能自己造/装'再放弃。", "src": "工具增强"},
    {"name": "多模态模型当工具", "when": ["看不清", "看不到", "识别不准", "搜不到内容"],
     "how": "把多模态模型(gemini/gpt-5/claude-VLM)当工具调: 图看不清→see_image_expert换gemini看; "
            "本模型VLM识别不准→换一家模型交叉看; 复杂图→逐块喂给VLM列清单。模型本身就是能换的工具。",
     "src": "模型即工具·多模态"},
    {"name": "视频/媒体攻法", "when": ["搜不到内容", "看不到画面", "听不到"],
     "how": "视频题两条路: ①问画面内容(某时刻显示什么)→yt-dlp下片段+ffmpeg抽该时刻帧→VLM看画面"
            "(用see_video_frame工具); ②问说了什么→transcribe_audio转音轨。别只搜文字网页(视频内容搜不到)。"
            "下不动(网络/反爬)就换渠道找二手资料(视频转录站/wiki/fandom记录)。", "src": "视频·多模态+工具"},
    # ── 多角度/交叉验证类 ──
    {"name": "异构交叉验证", "when": ["不确定", "高风险", "数值", "id", "日期", "怕算错", "验证"],
     "how": "让不同家的模型(gpt-5/gemini vs claude)基于相同资料独立复算,数值容差比对。"
            "不一致第三方仲裁。异构模型错法不同才能互纠。", "src": "集成·独立验证"},
    {"name": "反向验证", "when": ["候选答案", "过拟合", "验证", "自洽"],
     "how": "把答案代回题目验证是否自洽(棋招走完局面评分、化学式反查性质、数值代入原方程)。"
            "反面思考:什么情况这答案会错?", "src": "逆向·反证"},
    # ── 重构问题类 ──
    {"name": "重述问题·找真意图", "when": ["答非所问", "歧义", "反复", "答错", "意图", "口径"],
     "how": "用自己的话重述题目到底问什么(问'全吗'=问闭环真通;问'best move'可能是人工标注非引擎最优)。"
            "区分'题目预期解法'和'最精确解法'(可能有口径差)。", "src": "Polya:理解问题"},
    {"name": "类比/联想", "when": ["全新", "无思路", "没有现成", "陌生"],
     "how": "这道题像哪类解过的题?借用相似问题的解法结构。跨领域联想(棋题像搜索树、化学像约束求解)。",
     "src": "类比推理·Polya"},
    # ── 元策略(关于策略的策略) ──
    {"name": "撞墙换策略", "when": ["反复", "循环", "超时", "失败", "没进展", "卡"],
     "how": "识别'当前策略不奏效'→立即停手换结构性不同的策略(不是重复同样的)。"
            "记录哪条路走不通,避免回头再撞。像研究者:此路不通就另辟蹊径。", "src": "元认知·撞墙检测"},
    {"name": "分而治之", "when": ["多跳", "多步骤", "子问题", "复杂"],
     "how": "写plan拆成子问题,逐个击破,产出回填。每子问题独立验证再组装。", "src": "分治·Polya"},
    # ── v98 机制三(缺口3): locate 前置跳·跨域通用(视频找watch URL/论文找DOI/
    #    商品找SKU页/数据集找下载直链都是这一跳)。卡点="工具的输入资源还没有确切地址"。
    {"name": "定位确切资源(locate前置跳)",
     "when": ["搜索页", "search_page", "输入无效", "前置跳", "确切url", "资源地址",
              "视频url", "找不到链接", "描述定位", "watch", "doi", "直链", "缺一跳"],
     "how": "工具报'输入无效/需要确切资源URL'时,说明你跳过了一个隐含子目标:【从描述"
            "定位到确切资源页】。通用做法:①用描述搜索得候选 ②校验候选类型(classify_url"
            "确认是目标资源页·不是列表页/搜索页) ③把确切URL喂给下游工具 ④下游若报输入无效"
            "就回来换下一个候选。别把搜索页/频道页/描述残缺直接喂给要'确切资源'的工具。",
     "src": "缺一跳检测·资源定位通用模式"},
    # ── 语义建模/前提审计类(2026-08-12 · 错题归因G1·Bob博弈题三层归位的策略层) ──
    {"name": "目标函数语义建模", "when": ["博弈", "优化", "最大化", "最小化", "最优", "maximin", "策略", "保底", "对手"],
     "how": "求解前先【显式写出目标函数】: 最大化的是谁的最小值? 最小化的是谁的最大值? "
            "约束集穷尽了吗? 先用 2×2 小规模【手工验算】建模对不对, 再上大规模暴搜。"
            "关键: 暴搜执行对但服务的模型错了则全错——死磕的往往不是求解而是建模。", "src": "博弈论·目标函数消歧"},
    {"name": "自洽循环检测", "when": ["反复验证", "越验证越确信", "自洽", "前提", "置信度涨", "自证"],
     "how": "若最近 N 次验证都在【自己的前提内打转】(无新的外部证据流入·但置信度持续上涨)"
            "→ 这是危险信号: 停下·重读题意·质疑最初的语义建模(而非继续在错前提上验证)。"
            "验证要引入新证据才有意义·纯自洽只是把错误加固。", "src": "元认知·前提审计"},
]


def suggest_strategies(question="", stuck_reason="", kind="", top_k=3):
    """fallback策略(仅反思脑reflective_mind不可用时用)·返回【通用普适元策略】。
    v13.93(用户: 别用词表判题型): 不再按题型词表匹配(那是替agent判断·不通用)·
    只给不依赖题型的普适方法论(撞墙换策略/重述问题/第一性原理/多模态当工具)·
    题型该由agent自己+反思脑(LLM语义)判断·这里只是反思脑挂了时的通用兜底。"""
    ctx = f"{question} {stuck_reason} {kind}".lower()
    scored = []
    pool = _STRATEGIES + _load_learned()  # 内置 + 动态学到的(策略库越用越丰富)
    for s in pool:
        # 只保留元策略/普适策略的匹配·不做题型词表判断(赋能agent自己判断·非替它判)
        score = sum(1 for w in s.get("when", []) if str(w).lower() in ctx)
        # 撞墙换策略在'反复/循环/超时'时强触发
        if s["name"] == "撞墙换策略" and any(k in ctx for k in ["循环", "反复", "超时", "stuck", "again", "重复"]):
            score += 2
        scored.append((score, s))
    scored.sort(key=lambda x: -x[0])
    picked = [s for sc, s in scored if sc > 0][:top_k]
    if not picked:  # 无匹配→给通用元策略
        picked = [s for s in _STRATEGIES if s["name"] in ("撞墙换策略", "重述问题·找真意图", "第一性原理")]
    return picked


def _kw(when_phrase):
    """把触发场景拆成关键词(粗匹配)。"""
    import re
    return [w for w in re.split(r"[/·、\s]+", when_phrase) if len(w) >= 2]


import os as _os
import json as _json

_LEARNED_PATH = _os.path.expanduser("~/.meyo/strategy_learned.jsonl")


# ═══ 动态能力① · 动态改写卡住的query(不只说"换渠道"·给出具体改写后的query) ═══
def rewrite_query(failed_query, question="", attempt=1):
    """卡住的搜索/命令query动态改写(真正的动态策略·非静态查表)。
    按尝试轮次逐步升级改写手法·返回具体可用的新query列表。"""
    import re
    q = (failed_query or "").strip()
    variants = []
    # 提取实体(大写词/引号内/专名)做实体前置(实测:实体前置比平铺权重高)
    entities = re.findall(r'"([^"]+)"|([A-Z][a-zA-Z0-9\-]{2,}(?:\s+[A-Z][a-zA-Z0-9\-]+)*)', q)
    ents = [e[0] or e[1] for e in entities if (e[0] or e[1])]
    if ents:
        variants.append(" ".join(ents[:3]) + " " + re.sub(r'"[^"]+"', '', q).strip())  # 实体前置
    # 去缩写/展开(SPFMV等)·去噪问句词
    stripped = re.sub(r'\b(what|which|how|when|where|the|is|are|of|in|a|an)\b', '', q, flags=re.I).strip()
    if stripped and stripped != q:
        variants.append(re.sub(r'\s+', ' ', stripped))  # 去问句噪音·只留关键词
    # 加限定词(site/filetype/年份)提升精度
    if attempt >= 2:
        variants.append(q + " site:wikipedia.org OR site:.gov OR filetype:pdf")
    # 换语言/换措辞(用问题里的原始术语)
    if question and attempt >= 2:
        qterms = re.findall(r'[A-Z][a-zA-Z0-9\-]{3,}', question)
        if qterms:
            variants.append(" ".join(qterms[:4]))
    # 去重非空
    seen = set(); out = []
    for v in variants:
        v = v.strip()
        if v and v != q and v not in seen:
            seen.add(v); out.append(v)
    return out[:3]


# ═══ 动态能力② · 从解题经验学新策略(策略库动态补全·越用越丰富) ═══
def learn_strategy(name, when_keywords, how, src="经验学习", stuck_reason=""):
    """把一次成功的"换策略"经验沉淀成新策略(动态补全策略库)。fail-safe。
    when_keywords: 触发关键词list。how: 可操作做法。下次遇同类卡点自动推荐。

    职责边界护栏(2026-08-13·缺口2):策略库【只管方法维度】(撞墙换路/重述问题/该用什么
    工具类)·【不管模型选择】(哪类题用哪个模型)——那归 model_orchestrator 的模型经验库
    (唯一权威·带 bandit 学 win-rate×速度)。若沉淀内容是"某类题用某模型"的路由决策·
    拒绝写入本库并提示改走 record_experience·防两库互相矛盾("gemini快对" vs "先用claude")。"""
    try:
        import re as _re_g
        _txt = f"{name} {how}".lower()
        # 路由决策特征:同时含"具体模型名"+"用/换/选/首选"这类路由动词 → 该进模型经验库
        _has_model = bool(_re_g.search(r"claude|gemini|gpt-?\d|opus|sonnet|longcat-2.0", _txt))
        _is_routing = _has_model and bool(_re_g.search(
            r"用\s*\w*模型|先用|首选|优先.*用|route|路由|该用哪个模型|choose.*model|pick.*model", _txt))
        if _is_routing:
            # 不写策略库·提示归位(fail-safe:返回 False 让调用方知道没入库)
            return False
        _os.makedirs(_os.path.dirname(_LEARNED_PATH), exist_ok=True)
        rec = {"name": name, "when": when_keywords if isinstance(when_keywords, list) else [when_keywords],
               "how": how, "src": src, "stuck_reason": stuck_reason[:100]}
        with open(_LEARNED_PATH, "a", encoding="utf-8") as f:
            f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def _load_learned():
    """加载学到的策略(合并进检索池)。fail-safe空。"""
    if not _os.path.exists(_LEARNED_PATH):
        return []
    try:
        out = []
        for l in open(_LEARNED_PATH, encoding="utf-8"):
            if l.strip():
                out.append(_json.loads(l))
        return out
    except Exception:
        return []


def format_for_prompt(strategies, failed_query="", question=""):
    """把策略格式化成给agent的提示文本。若有卡住的query·附上动态改写建议。"""
    lines = ["【策略提示】当前思路若不奏效,像顶尖研究者那样换个维度试(别重复同样的):"]
    for s in strategies:
        lines.append(f"· {s['name']}: {s['how']}")
    # 动态: 若传入卡住的query·给具体改写后的query
    if failed_query:
        rewrites = rewrite_query(failed_query, question=question, attempt=2)
        if rewrites:
            lines.append("【动态改写】卡住的query可换成: " + " | ".join(f"「{r}」" for r in rewrites))
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    reason = sys.argv[1] if len(sys.argv) > 1 else "图看不清"
    print(f"卡点='{reason}' 推荐策略:\n")
    print(format_for_prompt(suggest_strategies(stuck_reason=reason)))
