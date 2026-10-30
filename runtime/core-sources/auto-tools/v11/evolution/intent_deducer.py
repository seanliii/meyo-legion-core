#!/usr/bin/env python3
"""intent_deducer · v13.88.37 · 2026-07-24

读用户对话原文 · 归纳 1天/3天/本周意图
用户 2026-07-24 · 「演绎 agent 会思考我的目的和习惯 · 动态演绎近期/远期意图」

数据源:
  · ~/.claude/memory_gates/conversation_index.jsonl (50925 条 · 03:11 cron 增量)
  · 只取本项目 session_dir (33893 条)
  · 只取用户 turn (过滤 skill prompt / tool call)

输出:
  · v11/.intent_history.jsonl · 一行一次归纳
  · v11/.intent_current.json  · 最新 3 时窗 (1d/3d/7d)

消费者:
  · self_harness · 挖弱点时看 recent intent
  · overnight_report · 摘要
  · deductive_reasoner · 演绎前先读

3 时窗:
  · 1 天 (24h)  · 眼前主线
  · 3 天 (72h)  · 战术方向
  · 7 天 (168h) · 战略方向

用 LLM 归纳 (llm_judge · claude-opus)
不用 · 只做词频统计 (兜底 · 免费 · 秒级)
"""
import os, sys, json, re
from datetime import datetime, timedelta
from collections import Counter, defaultdict
from pathlib import Path

V11 = Path(__file__).parent.parent
HISTORY = V11 / ".intent_history.jsonl"
CURRENT = V11 / ".intent_current.json"

# 走原始 project jsonl · 有 type: user 明确
PROJECT_JSONL_DIR = Path.home() / ".claude/projects/-Users-xuan-Desktop-cursor-----"

# 精准起始黑名单 · text[:80] startswith 就砍 · 不能 markers 放行
STRICT_START_BLACKLIST = [
    "用户消息:", "用户消息：",
    "助手回复:", "助手回复：",
    "评估问题:", "评估问题：",
    "你之前写的帖", "你把一条 agent", "你把一条", "你在扫真实用户",
    "你在一个隔离的沙箱",
    "Query:", "User query:", "JSON plan:",
    "帖子标题:", "帖子标题：",
    "角色:", "角色：",
    "断言:", "断言：",
    "Task:", "任务:",
    "严格 JSON",
    "从下面",
    "请归纳", "请判断", "请分类", "请从下面", "请把",
    "You are a", "You are an",
    "<system", "<command",
    "判用户消息",
    "判用户", "判 用户",
    # QA agent reflect prompt
    "Goal (", "Source used:", "Already searched:", "Hits (",
    "Objective:", "Context:", "Instructions:",
    # search agent 内部
    "Rank these", "Given the query",
    "reflect prompt", "planner prompt",
    # writer / curator 内部
    "原标题:", "原正文",
    "生成风格:", "生成风格：",
]

# 用户 turn 特征 · 排除 skill prompt / tool result / LLM judge prompt
NOT_USER_MARKERS = [
    "在一个隔离的沙箱工作目录",
    "SKILL_MANIFEST.md",
    "SUCCESS_CRITERIA.md",
    "<command-message>",
    "<system-reminder>",
    "<local-command",
    "tool_use_id",
    "Skill: ",
    # correction_evolver.llm_judge 的 judge prompt · 反复出现
    "从下面用户当次说",
    "事实纠正",
    "风格纠正",
    "词别用",
    "工具名",
    "提到的产品",
    "对之前答复的具体",
    "严格 JSON 输出",
    # 其他 prompt 模板
    "你是一个",
    "You are a",
    "请归纳",
    "请判断",
    "请分类",
    "请从下面",
    # deep-grill skill 自动派单 · 主对话里出现
    "断言:",
    "断言：",
    "建议验证方式:",
    "建议验证方式：",
    "穷尽列出",
    "若这个断言",
    "反证据",
    "反证路径",
    # 常见 skill 硬派模板
    "只输出 JSON",
    "只输出JSON",
    "严格输出",
    "只返回",
    # output_gate / jargon 拦截 hook 的提示 · 也存 user turn
    "黑话命中",
    "换人话重写",
    "请重写这段回复",
    "违反的地方",
    "用户明令禁止",
    "别打折",
# 其他 hook / 系统提示
"hook 拦",
"hook 触发",
"system_reminder",
# v13.90 · P2-1 · 测试探针语 (pytest fixture/deduce_hub 探针泄漏进 user turn)
"[pitfall]",
"演绎真错",
"some content",
    # qa/search agent 自动 query 模板
    "Query:",
    "User query:",
    "JSON plan:",
    "帖子标题:",
    "帖子标题：",
    "角色: ✍️",
    "角色：✍️",
]


def classify_user_turn(text: str) -> dict:
    """v13.88.37 · 2026-07-24 用户反馈: 不能砍长消息 · 我意图很长
    改为分类而非过滤 · 保留所有 · 加标签
    返回 {kind, is_real_user, tags[]}
    kind: real_user | pasted_content | tool_prompt | hook_msg | code_paste
    """
    if not text or len(text) < 3:
        return {"kind": "empty", "is_real_user": False, "tags": []}

    tags = []
    # v13.88.37 · 精准起始黑名单 · 绝对否
    stripped = text.lstrip()
    for pref in STRICT_START_BLACKLIST:
        if stripped.startswith(pref):
            return {"kind": "tool_prompt", "is_real_user": False,
                    "tags": [f"start:{pref[:15]}"]}

    # 全文黑名单 · 弱信号 · 但组合命中就砍
    for m in NOT_USER_MARKERS:
        if m in text[:500]:
            tags.append(f"marker:{m[:20]}")
            return {"kind": "tool_prompt", "is_real_user": False, "tags": tags}

    # 代码粘贴 · 但用户可能贴代码问问题 · 只在纯代码块时排除
    fence_n = text.count("```")
    if fence_n >= 4:  # 至少 2 组完整 code block
        tags.append("multi_code_block")
        return {"kind": "code_paste", "is_real_user": False, "tags": tags}
    if text.count("CREATE TABLE") >= 1:
        return {"kind": "code_paste", "is_real_user": False, "tags": ["sql_ddl"]}

    # 长消息 · 但含用户话特征 → 是真话
    L = len(text)
    cn = sum(1 for c in text if "一" <= c <= "鿿")
    cn_ratio = cn / L if L else 0

    # 用户命令/追问特征词
    USER_MARKERS = ["为啥", "为什么", "怎么", "咋", "啊", "呢", "吗", "呀",
                    "我要", "我想", "我看", "帮我", "给我", "别", "不要",
                    "错", "对", "傻", "笨", "妈", "他妈"]
    has_marker = any(m in text for m in USER_MARKERS)

    if L > 5000:
        # 超长 · 多半粘贴 · 但若含 ≥3 命令词也留
        if has_marker and cn_ratio > 0.15:
            tags.append("long_with_command")
            return {"kind": "real_user", "is_real_user": True, "tags": tags}
        tags.append("very_long")
        return {"kind": "pasted_content", "is_real_user": False, "tags": tags}

    if L > 800 and cn_ratio < 0.10:
        # 长 + 无中文 → 纯英文粘贴
        tags.append("long_no_cn")
        return {"kind": "pasted_content", "is_real_user": False, "tags": tags}

    # 中文占比过低 (< 15%)
    if L > 100 and cn_ratio < 0.15 and not has_marker:
        tags.append("low_cn")
        return {"kind": "pasted_content", "is_real_user": False, "tags": tags}

    # 是真用户话 · 加长度标签
    if L < 50: tags.append("short")
    elif L < 300: tags.append("medium")
    elif L < 1500: tags.append("long")
    else: tags.append("very_long_but_user")
    if has_marker: tags.append("has_command_marker")
    return {"kind": "real_user", "is_real_user": True, "tags": tags}


def is_user_turn(text: str) -> bool:
    """向后兼容 · 只保 is_real_user"""
    return classify_user_turn(text).get("is_real_user", False)


def _extract_text(msg_content) -> str:
    """从原始 project jsonl 的 message.content 抽 text · 兼容 str 和 list"""
    if isinstance(msg_content, str):
        return msg_content
    if isinstance(msg_content, list):
        parts = []
        for c in msg_content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
        return "\n".join(parts)
    return ""


def load_recent(hours: int) -> list:
    """加载最近 hours 小时的本项目用户 turn · 走原始 jsonl
    v13.88.37 fix · session jsonl 存 UTC · 必须 utcnow 比较
    """
    if not PROJECT_JSONL_DIR.exists():
        return []
    # v13.88.37 · 修 tz · session jsonl 的 timestamp 是 UTC
    from datetime import timezone
    # v13.88.54 · utcnow 弃用 → now(timezone.utc)·replace(tzinfo=None) 保 naive 与 mt 比较不破
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
    out = []
    # 只扫 mtime 在 cutoff 之后的 jsonl · 快
    for jsonl in PROJECT_JSONL_DIR.glob("*.jsonl"):
        try:
            mt = datetime.fromtimestamp(jsonl.stat().st_mtime)
            if mt < cutoff - timedelta(hours=2):
                continue
        except Exception:
            continue
        try:
            for line in jsonl.read_text(encoding='utf-8', errors='ignore').splitlines():
                try:
                    r = json.loads(line.strip())
                except Exception:
                    continue
                if r.get("type") != "user":
                    continue
                # 排 subagent Task tool 的 prompt (isSidechain=true 是 subagent 输入)
                if r.get("isSidechain"):
                    continue
                # user 消息也含 tool_result · 排掉
                msg = r.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, list):
                    # 若含 tool_result · 跳
                    has_tool_result = any(
                        isinstance(c, dict) and c.get("type") == "tool_result"
                        for c in content
                    )
                    if has_tool_result:
                        continue
                text = _extract_text(content)
                cls = classify_user_turn(text)
                if not cls["is_real_user"]:
                    continue
                ts_s = r.get("timestamp", "")
                if not ts_s:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_s.replace("Z", "+00:00")).replace(tzinfo=None)
                except Exception:
                    continue
                if ts < cutoff:
                    continue
                # v13.88.37 · 保留完整原文 (最多 5000 字 · 不砍) · 加时间/session/tag
                out.append({
                    "ts": ts_s,
                    "text": text[:5000],  # 从 2000 提到 5000 · 别砍
                    "len": len(text),
                    "session": jsonl.stem[:8],
                    "tags": cls["tags"],
                    "kind": cls["kind"],
                })
        except Exception:
            continue
    return out


# ═══════════════ v13.88.37 · 情感强度 + 连续段抽取 ═══════════════

# 用户 2026-07-24: 「看语气严厉程度就知道了 · count 没有叼用」
# 用户: 「我们是在开发大项目 · 都是复杂语句里面拆解意图出来」
# 用户: 「得看上下文连续几句去分析」

SEVERITY_PATTERNS = {
    "curse": ["妈的", "他妈", "傻逼", "笨蛋", "白痴", "别逼我", "垃圾", "白干"],
    "urgent": ["快", "立刻", "马上", "现在就", "赶紧", "都做啊", "还不", "怎么还", "咋还"],
    "correcting": ["错", "不对", "错了", "反了", "别", "不要", "你没", "你不", "你居然",
                    "怎么办", "咋回事", "咋整", "为啥", "为什么"],
    "denying": ["不", "没", "没有", "并不", "算了", "算什么", "怕啥"],
    "questioning": ["吗?", "吗？", "呢?", "呢？", "?", "？"],
    "commanding": ["给我", "帮我", "去做", "去查", "去建", "去改", "让 ", "改成", "换成"],
    "meta_rule": ["元规则", "硬规则", "硬门", "永久", "永远", "一定", "必须", "以后",
                   "记住", "别再", "不能"],
}


def score_severity(text: str) -> dict:
    """算一条 turn 的情感强度 · 严厉 = 高优先级"""
    hits = {}
    for cat, kws in SEVERITY_PATTERNS.items():
        n = sum(1 for kw in kws if kw in text)
        if n:
            hits[cat] = n
    # 分数 · curse+urgent+correcting 加权最重
    score = (
        hits.get("curse", 0) * 5
        + hits.get("urgent", 0) * 3
        + hits.get("correcting", 0) * 2
        + hits.get("denying", 0) * 1
        + hits.get("commanding", 0) * 2
        + hits.get("meta_rule", 0) * 4
        + hits.get("questioning", 0) * 1
    )
    return {"score": score, "hits": hits}


def extract_paragraphs(turns: list, gap_minutes: int = 15) -> list:
    """连续几句 turn 拼成 段 · 挖复合意图
    连续两条间隔 < 15min · 视为同一意图段
    """
    if not turns:
        return []
    turns = sorted(turns, key=lambda x: x["ts"])
    paras = []
    cur = [turns[0]]
    for i in range(1, len(turns)):
        prev_ts = datetime.fromisoformat(turns[i-1]["ts"].replace("Z", "+00:00")).replace(tzinfo=None)
        this_ts = datetime.fromisoformat(turns[i]["ts"].replace("Z", "+00:00")).replace(tzinfo=None)
        if (this_ts - prev_ts).total_seconds() / 60 <= gap_minutes:
            cur.append(turns[i])
        else:
            paras.append(cur)
            cur = [turns[i]]
    if cur:
        paras.append(cur)
    return paras


def paragraph_summary(paras: list, top_k: int = 20) -> list:
    """给每段算强度 + 抽关键词 · 返回按强度排序"""
    out = []
    for p in paras:
        combined = "\n".join(t["text"] for t in p)
        sev = score_severity(combined)
        # 抽 3-8 字中文关键词
        kws = Counter()
        for m in re.findall(r"[一-龥]{3,8}", combined):
            if m not in STOP_CN:
                kws[m] += 1
        out.append({
            "n_turns": len(p),
            "ts_start": p[0]["ts"],
            "ts_end": p[-1]["ts"],
            "total_len": sum(t["len"] for t in p),
            "severity": sev,
            "top_kws": kws.most_common(6),
            "excerpt": combined[:500],  # 段头 500 字
        })
    # 按严厉度排 · 强的在前
    out.sort(key=lambda x: -x["severity"]["score"])
    return out[:top_k]


# ═══════════════ 词频统计 (免费兜底) ═══════════════

STOP_CN = {"什么", "怎么", "为什么", "为啥", "这个", "那个", "一样", "一定",
           "没有", "然后", "如果", "或者", "或是", "还有", "还是", "都是",
           "就是", "已经", "还没", "现在", "开始", "结束", "问题", "东西",
           "事情", "地方", "时候", "方式", "方法", "以后", "以前"}
STOP_EN = {"the", "this", "that", "with", "from", "have", "would", "should",
           "could", "will", "your", "their", "what", "when", "where", "there"}


def keyword_summary(turns: list, top_k: int = 15) -> dict:
    """从 turns 里抽高频关键词 (中文 3-8 字 + 英文 3-15 字符)"""
    cn_words = Counter()
    en_words = Counter()
    for t in turns:
        text = t.get("text", "")
        for m in re.findall(r"[一-龥]{3,8}", text):
            if m not in STOP_CN:
                cn_words[m] += 1
        for m in re.findall(r"\b[A-Za-z][A-Za-z0-9_-]{2,15}\b", text.lower()):
            if m not in STOP_EN:
                en_words[m] += 1
    return {
        "n_turns": len(turns),
        "cn_top": cn_words.most_common(top_k),
        "en_top": en_words.most_common(top_k),
    }


# ═══════════════ LLM 归纳 (可选 · 高质) ═══════════════

def llm_summarize(turns: list, window: str, model: str = None) -> dict:
    """v13.88.37 · 用户 07-24: 「不是关键词 · 是调模型判断我的事儿和对话」
    直接 subprocess claude CLI · 自定 schema · 不走 _judge_one (它硬 verdict/reason)
    """
    if not turns:
        return None
    import subprocess as _sp

    # 拼所有 turn · 保原文 (不砍) · 让模型看长意图
    sample = turns[-40:] if len(turns) > 40 else turns
    corpus = "\n\n---\n\n".join(
        f"[{t['ts'][:16]} tags={t.get('tags',[])}] {t['text'][:1500]}"
        for t in sample
    )

    _model = model or "haiku"
    system = ("你是资深项目分析师 · 读用户和 AI agent 的原话流水 · "
              "推断用户真实意图和需求 · 基于语义不是关键词 · 返回严格 JSON")

    # v13.88.37 · 用户: 「演绎再 input 前先搜索和演绎」
    # 归纳当前意图前 · 先搜上次归纳的历史意图 · 让模型看到「用户上次要啥」
    _prior_intent = ""
    try:
        if CURRENT.exists():
            _prior = json.loads(CURRENT.read_text(encoding='utf-8'))
            _pw = _prior.get("windows", {}).get(window, {}).get("llm_intent", {})
            if _pw.get("main_goal"):
                _prior_intent = (
                    f"\n## 【上次归纳的历史意图】(对比·看用户诉求有无变化)\n"
                    f"上次 main_goal: {_pw.get('main_goal','')}\n"
                    f"上次 unmet_needs: {_pw.get('unmet_needs',[])[:3]}\n"
                    f"上次 trust_level: {_pw.get('trust_level','?')}\n"
                )
    except Exception:
        pass

    prompt = f"""用户过去 {window} 在项目 seed-users-system 里的原话流水 (每段用 --- 分隔):

{corpus[:15000]}
{_prior_intent}
请深度归纳 (若有历史意图·对比这次是否转向) · 只返 JSON:
{{
  "main_goal": "20 字内核心目的",
  "sub_goals": ["3-5 个具体子任务"],
  "user_style": "沟通风格 (急/耐心/严厉/详细)",
  "unmet_needs": ["未闭环 pain 1", "pain 2", "pain 3"],
  "meta_rules_stated": ["用户提到的元规则"],
  "context_shift_from_earlier": "相比更早时段是否转向",
  "trust_level": "high/normal/low/critical",
  "urgency_1_10": 5,
  "user_current_frustration_source": "用户最新一段最气啥",
  "next_action_expected_by_user": "用户希望我下一步做啥",
  "compute_tier": "trivial/normal/heavy (吸收 Stark『高算力问题用子代理』· trivial=改typo/单点查; normal=常规改一处; heavy=跨多子系统/写方案/审计/需并行读多文件)",
  "needs_fanout": true,
  "fanout_reason": "若 needs_fanout=true·为啥该开子代理/workflow (如『跨 5 子系统需并行读·一个上下文装不下』)·否则空串"
}}"""

    try:
        # v13.88.9j · 走统一 spawn 闸门 (防 load 雪崩)·闸门拒绝 returncode=199 走下面 error 降级
        from _spawn_gate import spawn_claude_cli
        r = spawn_claude_cli(
            ["claude", "--print", "--model", _model,
             "--append-system-prompt", system, prompt],
            timeout=180,
        )
        if r.returncode != 0:
            return {"error": f"exit={r.returncode}: {r.stderr[:200]}"}
        text = r.stdout.strip()
        # 抽 JSON
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return {"error": "no_json", "raw": text[:500]}
        return json.loads(m.group())
    except _sp.TimeoutExpired:
        return {"error": "timeout 180s"}
    except json.JSONDecodeError as e:
        return {"error": f"json parse: {e}", "raw": text[:300]}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# ═══════════════ 主流 ═══════════════

def deduce_all(use_llm: bool = True) -> dict:
    """v13.88.37 · 6 时窗 · 加连续段情感强度 · 用户 2026-07-24 要求
    用户: 「只看 1 天?」→ 6 时窗
    用户: 「count 没用 · 看语气严厉」→ 段情感排序
    用户: 「上下文连续几句分析」→ paragraph 抽取
    """
    windows = [
        ("1h", 1),      # 即时 · 刚说的
        ("6h", 6),      # 近半日
        ("1d", 24),     # 今日
        ("3d", 72),     # 战术
        ("7d", 168),    # 战略
        ("30d", 720),   # 长期
    ]
    result = {"ts": datetime.now().isoformat(), "windows": {}}

    for name, hours in windows:
        turns = load_recent(hours)
        kw = keyword_summary(turns)
        # 连续段抽取
        paras = extract_paragraphs(turns, gap_minutes=15)
        para_top = paragraph_summary(paras, top_k=10)
        entry = {
            "hours": hours,
            "n_turns": len(turns),
            "n_paragraphs": len(paras),
            "keyword_summary": kw,
            "paragraphs_by_severity": para_top,
        }
        if use_llm and turns:
            llm = llm_summarize(turns, name)
            if llm:
                entry["llm_intent"] = llm
        result["windows"][name] = entry

    # 落 history (append)
    with open(HISTORY, "a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")

    # 落 current (overwrite · 供 consumer 快查)
    CURRENT.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                       encoding="utf-8")

    # v13.88.37 · 2026-07-24 用户: 「告警全吗」· 严厉段立告警
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        from alert_bus import notify as _alert
        # 1h/6h 最严厉段
        for wname in ["1h", "6h"]:
            w = result["windows"].get(wname, {})
            paras = w.get("paragraphs_by_severity", [])
            if paras:
                top = paras[0]
                sev = top.get("severity", {}).get("score", 0)
                if sev >= 60:
                    severity_level = "CRITICAL" if sev >= 100 else "ERROR"
                    kws = "/".join(k for k, _ in top.get("top_kws", [])[:3])
                    _alert(
                        "intent_deducer", severity_level,
                        f"{wname} 严厉段 sev={sev} · 关键: {kws}",
                        throttle_min=45,
                    )
                    break
    except Exception:
        pass

    return result


def brief() -> str:
    """给 self_harness/overnight_report 消费的一句话摘要
    v13.88.37 · 显示 6 时窗 + top 严厉段
    """
    if not CURRENT.exists():
        return "(intent_current.json 未生成 · 先跑 --run)"
    j = json.loads(CURRENT.read_text(encoding="utf-8"))
    lines = []
    for w in ["1h", "6h", "1d", "3d", "7d", "30d"]:
        e = j.get("windows", {}).get(w, {})
        n = e.get("n_turns", 0)
        if n == 0: continue
        # 最严厉段
        top_paras = e.get("paragraphs_by_severity", [])
        if top_paras:
            top = top_paras[0]
            sev = top["severity"]["score"]
            kws = "/".join(k for k, _ in top.get("top_kws", [])[:3])
            lines.append(f"[{w}] {n}t/{e.get('n_paragraphs',0)}段 · 最严厉段 sev={sev} · {kws}")
        else:
            lines.append(f"[{w}] {n}t")
    return "\n".join(lines) if lines else "(无用户 turn)"


if __name__ == "__main__":
    try:
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("--run", action="store_true", help="跑 6 时窗归纳")
        ap.add_argument("--no-llm", action="store_true", help="只词频+情感段 · 秒级")
        ap.add_argument("--llm", action="store_true", help="haiku 归纳 · ~30s")
        ap.add_argument("--llm-deep", action="store_true", help="opus 归纳 · ~90s · 每天 1 次")
        ap.add_argument("--brief", action="store_true", help="一句话摘要")
        ap.add_argument("--json", action="store_true")
        args = ap.parse_args()

        if args.brief:
            print(brief())
        elif args.run:
            # 3 模式:
            #   --no-llm      · 秒级 (纯词频+情感段) · 3h cron 用
            #   --llm         · haiku ~30s · 6h cron 用
            #   --llm-deep    · opus ~90s · 每天 12:00 用
            _use_llm = args.llm or args.llm_deep
            _model = "opus" if args.llm_deep else ("haiku" if args.llm else None)
            # 猴补 model 参 · 让 llm_summarize 用
            if _use_llm:
                import intent_deducer as _self
                _orig = _self.llm_summarize
                _self.llm_summarize = lambda turns, w: _orig(turns, w, model=_model)
            r = deduce_all(use_llm=_use_llm)
            if args.json:
                print(json.dumps(r, ensure_ascii=False))
            else:
                print(f"═══ intent_deducer · {r['ts'][:16]} ═══\n")
                for w, e in r["windows"].items():
                    n = e["keyword_summary"]["n_turns"]
                    cn = e["keyword_summary"]["cn_top"][:8]
                    print(f"── 窗口 {w} ({e['hours']}h) · {n} turns ──")
                    print(f"  关键词: {[k for k,_ in cn]}")
                    if "llm_intent" in e:
                        li = e["llm_intent"]
                        if "main_goal" in li:
                            print(f"  主线: {li['main_goal']}")
                        if li.get("concrete_tasks"):
                            print(f"  任务: {li['concrete_tasks'][:3]}")
                        if li.get("pain_points"):
                            print(f"  痛点: {li['pain_points'][:3]}")
                    print()
        else:
            ap.print_help()
    except Exception as _e:
        import traceback as _tb, sys as _s, os as _o
        _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
        try:
            from alert_bus import notify as _n
            _n(_o.path.basename(__file__).replace(".py",""), "CRITICAL",
               f"{_o.path.basename(__file__)} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        except Exception:
            pass
        _tb.print_exc()
        raise
