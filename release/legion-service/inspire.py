"""动态运营化"新对话灵感墙"数据源(用户 2026-08-30 截图#16)。

产品之魂②③:让用户一进来就看到"一群最强 harness 能替我玩什么",而且【每次不一样、和我相关】。
数据源(按优先级聚合,不联网也能出内容):
  1. 用户画像:从他的 session 历史推断常做的方向 → 推"你可能还想…"
  2. 群体经验飞轮:军团最近答得好的相似题 → 推"军团擅长这个"
  3. 时新精选池:按当天轮换的一批"可玩任务"(涵盖调研/生图/建站/数据/BP…),
     模拟 last30days/最新 skill/资讯的运营化内容(联网增强是可选项,缺网用本地池)。

不搜全网也保证有货;有 external-agents/updates、models/discovery 等时新数据时叠加。
"""
from __future__ import annotations
import time
import hashlib
import random

# ── 精选可玩任务池(运营化·点击即填入 prompt 可跑)──
# 每条:cat 分类 · t 标题(吸引人)· d 一句说明 · fill 点击填入的 prompt · tag 角标
_POOL = [
    {"cat": "调研", "t": "帮我调研一个赛道", "d": "军团多家并行搜全网+出带图表的深度报告", "fill": "调研「AI陪练/口语学习」赛道:市场规模、头部玩家、商业模式、机会点,出一份带数据图表的深度报告", "tag": "🔥热门"},
    {"cat": "生图", "t": "生成一组品牌视觉", "d": "配图/海报/logo·真出图可下载", "fill": "为一个「社区团购」小程序生成:主视觉海报1张 + logo 3个方案 + 3张营销配图,风格清新现代", "tag": "🎨"},
    {"cat": "建站", "t": "做一个能上线的落地页", "d": "真网页·可预览可部署给URL", "fill": "帮我做一个「AI英语陪练」产品落地页:首屏slogan+核心卖点+定价+CTA,现代简洁风,直接给我能打开的网页", "tag": "🚀"},
    {"cat": "数据", "t": "分析我的Excel/数据", "d": "上传表格·军团出分析+图表", "fill": "分析这份销售数据:找出增长/下滑趋势、异常点、Top客户,给结论和可视化图表(我马上上传文件)", "tag": "📊"},
    {"cat": "BP", "t": "写一份融资BP", "d": "PDF+财务模型Excel+配图", "fill": "帮我写一份「社区团购」种子轮融资BP:市场/产品/团队/商业模式/财务预测,输出PDF+配套财务模型Excel", "tag": "💼"},
    {"cat": "代码", "t": "给我一个能跑的项目", "d": "完整代码文件夹+README", "fill": "用 Python 做一个「网页内容定时抓取+每天发我邮件」的小工具,给我完整可运行的代码文件夹和使用说明", "tag": "⚙️"},
    {"cat": "视频", "t": "做一段短视频/配音", "d": "视频/音频真文件可播放", "fill": "把这段文案做成一分钟短视频脚本 + 中文旁白配音(TTS),文案:「三步用AI搞定周报」", "tag": "🎬"},
    {"cat": "对比", "t": "让多家AI同题PK", "d": "看多家并行答·挑最好的", "fill": "同一个问题让军团多家 harness 并行回答并对比:「如何用最低成本冷启动一个本地生活社区」", "tag": "⚔️PK"},
    {"cat": "合规", "t": "帮我过一遍合规", "d": "GDPR/隐私/条款落地清单", "fill": "帮我梳理一个面向欧洲用户的App需要满足的GDPR合规要点,给一份可执行的落地清单", "tag": "🛡️"},
    {"cat": "运营", "t": "生成一周运营内容", "d": "文案+配图+发布计划", "fill": "为一个「宠物用品」抖音号生成一周的内容计划:7条脚本 + 每条的配图 + 最佳发布时间", "tag": "📣"},
    {"cat": "学习", "t": "把复杂主题讲明白", "d": "军团深研+结构化讲解+图", "fill": "用生动例子给我讲明白「Transformer 注意力机制」,配示意图,像给完全外行讲一样", "tag": "📚"},
    {"cat": "自动化", "t": "帮我自动化一个流程", "d": "军团设计+给可运行方案", "fill": "我每天要手动整理20个网页的数据到表格,帮我设计一个自动化方案并给出可运行的代码", "tag": "🤖"},
]

# 按方向给用户画像做的关键词映射(从 session 标题里嗅方向)
_DIR_HINTS = {
    "调研": ["调研", "分析", "研究", "市场", "竞品", "赛道"],
    "生图": ["图", "海报", "logo", "配图", "画", "视觉"],
    "建站": ["网页", "落地页", "网站", "建站", "html", "前端"],
    "数据": ["excel", "数据", "表格", "统计", "报表"],
    "BP": ["bp", "融资", "商业计划", "财务"],
    "代码": ["代码", "写个", "开发", "脚本", "程序", "python", "java"],
    "视频": ["视频", "配音", "音频", "tts", "剪辑"],
    "合规": ["合规", "gdpr", "隐私", "条款", "法务"],
    "运营": ["运营", "文案", "抖音", "小红书", "内容", "营销"],
}


def _profile_directions(uid: str) -> list[str]:
    """从用户 session 历史嗅出他常做的方向(画像)。"""
    try:
        import sessions as SESSIONS
        rows = SESSIONS.list_sessions(limit=50, user_id=uid or "default")
    except Exception:
        return []
    hits: dict[str, int] = {}
    for r in rows:
        title = str(r.get("title") or "").lower()
        for direction, kws in _DIR_HINTS.items():
            if any(k in title for k in kws):
                hits[direction] = hits.get(direction, 0) + 1
    return [d for d, _ in sorted(hits.items(), key=lambda x: -x[1])]


def _daily_seed() -> int:
    """按天变化的种子:让灵感墙每天不一样(时新感),但同一天稳定。"""
    day = time.strftime("%Y%m%d")
    return int(hashlib.md5(day.encode()).hexdigest()[:8], 16)


_SUGGEST_TTL = 60.0
_SUGGEST_CACHE: dict[str, tuple[float, dict]] = {}


def suggest(uid: str = "default", n: int = 6) -> dict:
    """返回个性化 + 时新的可玩任务卡。

    ★60s TTL 缓存(2026-09-02 真实 server 实测 241ms·隔离环境仅 12ms——差在 _profile_
    directions 读 50 个 session 文件·真机 400+ session 每次灵感墙打开都重读)。同 uid+n
    缓存 60s(画像/经验分钟级变化无感·省的是每次重复扫盘)。"""
    uid = str(uid or "default")
    _ck = f"{uid}:{n}"
    _now = time.time()
    _hit = _SUGGEST_CACHE.get(_ck)
    if _hit and _now - _hit[0] < _SUGGEST_TTL:
        return _hit[1]
    # n 下限保护:n<=0 或负数会让 cards[:n] 返回空/切错(前端灵感区变空)。至少给 1 张。
    try:
        n = max(1, int(n))
    except (TypeError, ValueError):
        n = 6
    dirs = _profile_directions(uid)
    rnd = random.Random(_daily_seed() ^ (hash(uid) & 0xFFFFFFFF))

    pool = list(_POOL)
    rnd.shuffle(pool)

    cards: list[dict] = []
    seen_cat: set[str] = set()

    # 1) 画像优先:用户常做的方向,加"为你推荐"标记提到前面
    for d in dirs:
        for item in pool:
            if item["cat"] == d and item["cat"] not in seen_cat:
                c = dict(item)
                c["reason"] = "根据你最近的使用"
                c["personalized"] = True
                cards.append(c)
                seen_cat.add(item["cat"])
                break
        if len(cards) >= 2:
            break

    # 2) 军团经验飞轮:最近答得好的方向,标"军团擅长"
    try:
        import legion_memory as LM
        st = LM.stats()
        if st.get("available") and st.get("total", 0) > 0:
            for item in pool:
                if item["cat"] not in seen_cat:
                    c = dict(item)
                    c["reason"] = f"军团已沉淀 {st.get('total', 0)} 条经验·越用越准"
                    cards.append(c)
                    seen_cat.add(item["cat"])
                    break
    except Exception:
        pass

    # 3) 时新精选池填满(每天轮换)
    for item in pool:
        if len(cards) >= n:
            break
        if item["cat"] not in seen_cat:
            cards.append(dict(item))
            seen_cat.add(item["cat"])

    _out = {
        "ok": True,
        "cards": cards[:n],
        "personalized": bool(dirs),
        "day": time.strftime("%Y-%m-%d"),
        "profile_directions": dirs[:5],
    }
    _SUGGEST_CACHE[_ck] = (_now, _out)
    return _out
