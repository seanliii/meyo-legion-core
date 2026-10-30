#!/usr/bin/env python3
"""
v13.77 · DeepResearchAgent · 长耐心研究员级搜索
────────────────────────────────────────────────────────────────
用户 R10 (2026-07-14):
「任务不够长·按钮理解不到位·还得反复 loop 挖信息
 收集的信息要沉淀成本次知识
 反思搜索结果·反复对应 goal list 和 todo list 经验去 check
 实时判断自己效果·激活探索欲望
 任务要足够详细和长·不能懒惰·耐心搜索·自己调整模式」

── 相比 v13.76 的升级 ──
v13.76 (5 步就 done):        v13.77 (20-50 步深挖):
- goal 单一                  → goal 拆 todo list · 逐项完成
- extract 单次落地           → knowledge_base 结构化累积
- CHECK done/not_done        → satisfaction 0-100 · 探索欲 0-100
- reflect 仅 stuck 时         → meta_reflect 每 5 步 · 主动调策略
- click by text              → element intent 预判 (按钮意图 LLM)
- 一路 sonnet                → 复杂决策切 opus · 简单切 haiku
- 一 pass 就 done            → 满意度 <85 强制继续 · 探索欲驱动

── 核心 loop v2 ──
0. plan_todos():  开头 opus 把 goal 拆成 5-10 子任务
1. observe():     snapshot 加 aria/role/意图预判
2. decide():      结合 knowledge_base + todo 完成度 决策
3. act():         执行
4. sediment():    extract 后立即结构化落 KB
5. self_score():  每步自评 satisfaction / exploration_desire
6. meta_reflect():每 5 步元反思 · 需换策略?
7. loop 直到所有 todo 完成 且 满意度 >= 85 或 max_steps

── knowledge_base 结构 ──
{
  "goal": "...",
  "todos": [{"id":1, "text":"...", "status":"done|pending", "evidence":[]}],
  "knowledge": [
    {"source_url":"", "title":"", "content":"", "insights":[], "for_todo":1}
  ],
  "meta_lessons": ["...", "..."]
}
"""
import os, sys, json, time
# v13.88.9f · 2026-07-31 · spawn claude 过统一咽喉·防 load 雪崩·fail-safe 降级
import os as _os_sg, sys as _sys_sg
_sys_sg.path.insert(0, _os_sg.path.dirname(_os_sg.path.abspath(__file__)))
try:
    from _spawn_gate import spawn_claude_cli as _spawn_gate_cli
except Exception:
    _spawn_gate_cli = None
def spawn_claude_cli(args, timeout=60, wait_in_queue=True, **kw):
    """过 _spawn_gate 咽喉·导入失败则回退 subprocess.run (不因咽喉缺失卡死)。"""
    import subprocess as _sp
    if _spawn_gate_cli is not None:
        return _spawn_gate_cli(args, timeout=timeout, wait_in_queue=wait_in_queue, **kw)
    kw.setdefault("capture_output", True); kw.setdefault("text", True)
    return _sp.run(args, timeout=timeout, **kw)
from datetime import datetime
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from audit_core import call_llm, append_event, push_correction, try_parse_json


# v13.88.79 · 静默监控统一接入 · 用户 07-28「所有静默监控都到齐了吗」
def _alert(severity: str, msg: str):
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
        from alert_bus import notify as _n
        _n(_o.path.basename(__file__).replace(".py",""), severity, msg)
    except Exception:
        pass


BASE = _HERE.parent.parent
V11 = BASE / "v11"
RESEARCH_LOG = V11 / ".deep_research.jsonl"
KB_DIR = V11 / "research_kb"
CDP_URL = "http://localhost:9224"


def _log(rec: dict):
    rec.setdefault("ts", datetime.now().isoformat())
    try:
        RESEARCH_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(RESEARCH_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as _e:
        # v13.87 · 不 silent
        print(f"⚠️ RESEARCH_LOG 落盘失败: {type(_e).__name__}: {_e}",
              file=sys.stderr)


def _save_kb(session_id: str, kb: dict):
    """会话知识库落盘"""
    try:
        KB_DIR.mkdir(parents=True, exist_ok=True)
        kb_path = KB_DIR / f"{session_id}.json"
        kb_path.write_text(json.dumps(kb, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    except Exception as _e:
        # v13.87 · 不 silent
        print(f"⚠️ KB save fail: {type(_e).__name__}: {_e}",
              file=sys.stderr)


# ────────────── prompts ──────────────

PLAN_SYSTEM = """你是「研究员规划员」·干净独立上下文。

给一个 goal · 输出 5-10 个 todo 子任务 (研究员风格)。
每个 todo 应该:
- 具体可执行 (不是"搜索 X" · 而是"搜索 X 找 top 3 篇 · 抽核心方法")
- 覆盖广度 + 深度 (不同角度/来源)
- 有验收标准 (数量/质量)

只回 JSON: {
  "todos": [
    {"id":1, "text":"...", "success_criteria":"...", "priority":"high|medium"},
    ...
  ],
  "estimated_steps": 20-50
}
"""

DECIDE_SYSTEM = """你是「浏览器研究员」·干净独立上下文·耐心持久·细腻不懒惰。

决策原则 (v13.78 加强):
1. **图面预读**: 先扫 page 元素+content_blocks·像人先看一眼再决定
2. **意图对齐**: 点某元素前先问「这个点了会去哪·符合当前 todo 吗」
3. **URL 去重**: visited_urls 里的不重复点·重复见 → deepen/scroll/close/换角度
4. **满意度 < min 强制继续** · 不能提前 done
5. **探索欲 > 60 时**: 尝试 scroll / 换角度 / 深入详情
6. **知识库参考**: KB 有 relevance>70 的·可复用不重抽
7. **视频/复杂元素**: 若 title/text 说'视频/直播'·可选择 skip 或滚看右边文字
8. **右侧栏内容**: 详情页右边通常有作者/相关推荐·别只看主体
9. **返回列表**: 详情页看完·可 close (ESC) 回列表继续下一条·不 goto 从头
10. **完成硬门**: 选择 next_todo 前，该 Todo 必须已抽取带 source_url 的正文证据；
    仅看到列表、仅有模型总结、或没有来源时继续 extract/goto，不得标完成。

action 类型:
- type(idx/selector, text)  真人逐字输入
- click(idx/text)           点击 (含 aria/role 预判)
- scroll(direction, amount) 滚动
- press(key)                键盘
- extract(what)             结构化抽内容 (先 body 再 comments)
- goto(url)                 只初始/换 query
- close                     ESC 或点关闭·回列表
- switch_query(new_query)   卡住换关键词
- next_todo                 当前 todo 已完成·切下一个
- deepen                    对当前 todo 挖更深 (滚/点评论)
- skip                      这个元素不合适·换一个
- done                      所有 todo 完成 且满意度足够 才允许

只回 JSON: {
  "action": "...",
  "target_todo_id": 1,
  "element_idx": 0-24,
  "text": "...",
  "selector": "...",
  "key": "...",
  "direction": "up|down",
  "amount": 500,
  "what": "notes|comments|body",
  "url": "...",
  "new_query": "...",
  "why": "30 字为什么这一步"
}
"""

# v13.78 · click 前意图预判
CLICK_INTENT_SYSTEM = """你是「点击意图审查员」·独立干净上下文。

给定 goal + 当前 todo + 候选元素 (含 aria/text/href)。
判断: 这个元素点了会去哪·符不符合 goal / 当前 todo?

具体判据:
- 元素是「视频/直播/游戏」但 goal 要「文字方法」→ 不符合
- 元素是「广告」→ 不符合
- 元素是「登录/注册/关注」→ 不符合 (除非 goal 就是这)
- 元素文字里有 goal 关键词的近义词 → 高度符合
- 元素文字与 goal 完全无关 → 不符合

只回 JSON: {"go":true|false, "reason":"20 字", "confidence":0-100}
"""

# v13.78 · 换 query
SWITCH_QUERY_SYSTEM = """你是「query 换词师」·独立干净上下文。

当前 query 卡住 (满意度 3 步没涨·或抽的内容重复)。
根据 goal · 提出 3 个候选新 query:
- q1: 同义/近义 (换个说法)
- q2: 更窄 (加限定词)
- q3: 更宽 (去掉限定)

只回 JSON: {"candidates":["q1","q2","q3"], "recommend":"哪个最好·一句话"}
"""

SCORE_SYSTEM = """你是「效果自评员」·干净独立上下文。

给定 goal · todos 状态 · knowledge base · 最近动作 · 判 3 个分:

1. satisfaction (0-100): 当前收获对达成 goal 满意度
   - 只是搜到列表: 20-40
   - 读了详情: 50-70
   - 读了多个不同角度 + 抽出核心: 70-85
   - 全 todo 完成 + 沉淀成体系: 85-100

2. exploration_desire (0-100): 还有多少探索欲
   - 已见内容重复: 20-40 (该换 query 或结束)
   - 还有 pending todo: 60-80
   - 发现新线索: 80-100

3. next_hint: 一句话建议下一步

只回 JSON: {"satisfaction":0-100, "exploration_desire":0-100, "next_hint":"..."}
"""

META_REFLECT_SYSTEM = """你是「元策略反思员」·干净独立上下文。

给定 goal · todos · knowledge · 最近 5 步动作 · 满意度轨迹。
判是否需要换策略:

- 一直在同一页刷: 该 goto 换 query
- 抽的内容重复: 换搜索词 (加同义词/近义词)
- 一直找不到目标元素: 该 scroll 或换 selector 策略
- 已达大部分 todo: 该收尾

只回 JSON: {
  "should_switch_mode": true|false,
  "new_strategy": "20 字新策略",
  "why": "20 字为什么",
  "suggested_action": "..."
}
"""

SEDIMENT_SYSTEM = """你是「知识沉淀员」·干净独立上下文。

给一段抽到的原始内容 · 结构化提炼:
- title: 标题/主题
- key_insights: 3-5 条核心洞察
- methods: 提到的方法/工具 (如有)
- relevance_score: 0-100 (对 goal 的相关性)

只回 JSON: {
  "title":"...", "key_insights":["...","..."],
  "methods":["..."], "relevance_score":0-100
}
"""


# ────────────── 阶段函数 ──────────────

def plan_todos(goal: str, model: str = "opus", timeout: int = 90) -> dict:
    """开头拆 todo list"""
    r = call_llm(PLAN_SYSTEM, f"goal: {goal}\n\n拆解 JSON:",
                model_override=model, timeout=timeout)
    if r["ok"]:
        parsed = try_parse_json(r["raw"], default={"todos": [], "estimated_steps": 20})
        return parsed
    return {"todos": [{"id": 1, "text": goal, "priority": "high"}],
            "estimated_steps": 20}


def snapshot_page_v2(page, max_elements: int = 30) -> dict:
    """v3: 加 video/iframe 识别 + right_sidebar + is_detail_page 检测"""
    try:
        data = page.evaluate("""(maxE) => {
          const inView = (el) => {
            const r = el.getBoundingClientRect();
            return r.width > 5 && r.height > 5 &&
                   r.top < window.innerHeight && r.bottom > 0;
          };
          const abbr = (s, n) => (s||'').trim().slice(0, n);
          const nodes = [];

          // v13.78 · 识别 media_type (每个 link 关联最近的图/视频)
          const findParentMediaType = (el) => {
            let p = el.parentElement;
            let d = 5;
            while (p && d-- > 0) {
              if (p.querySelector('video') || p.querySelector('svg[class*="play"]')) return 'video';
              if (p.querySelector('img')) return 'image';
              p = p.parentElement;
            }
            return 'text';
          };

          document.querySelectorAll('input, textarea').forEach(el => {
            if (inView(el)) {
              nodes.push({
                idx: nodes.length, type: 'input',
                tag: el.tagName.toLowerCase(),
                placeholder: abbr(el.placeholder, 50),
                value: abbr(el.value, 50),
                name: el.name || el.id || '',
                aria: abbr(el.getAttribute('aria-label')||'', 50),
                selector: el.id ? '#' + el.id : (el.name ? el.tagName.toLowerCase() + "[name='" + el.name + "']" : null),
              });
            }
          });

          document.querySelectorAll('button, [role="button"], [role="tab"]').forEach(el => {
            if (inView(el) && nodes.length < maxE) {
              nodes.push({
                idx: nodes.length, type: 'button',
                text: abbr(el.innerText, 60),
                aria: abbr(el.getAttribute('aria-label')||'', 50),
                title: abbr(el.getAttribute('title')||'', 50),
                role: el.getAttribute('role') || 'button',
                cls: (el.className||'').toString().slice(0, 40),
              });
            }
          });

          document.querySelectorAll('a[href]').forEach(el => {
            if (inView(el) && nodes.length < maxE) {
              const t = abbr(el.innerText, 80);
              if (t) nodes.push({
                idx: nodes.length, type: 'link',
                text: t,
                href: (el.href||'').slice(0, 120),
                aria: abbr(el.getAttribute('aria-label')||'', 40),
                media_type: findParentMediaType(el),  // v13.78
              });
            }
          });

          // 主内容区
          const bigTextBlocks = [];
          document.querySelectorAll('article, section, main, div[class*="content"], div[class*="detail"]').forEach(el => {
            if (bigTextBlocks.length < 3) {
              const t = (el.innerText||'').trim();
              if (t.length > 200) bigTextBlocks.push(t.slice(0, 500));
            }
          });

          // v13.78 · 右侧栏 (小红书详情页有作者信息/相关推荐)
          const rightSideText = [];
          document.querySelectorAll('aside, div[class*="right"], div[class*="side"], div[class*="author"], div[class*="related"]').forEach(el => {
            if (rightSideText.length < 3 && inView(el)) {
              const t = (el.innerText||'').trim();
              if (t.length > 50 && t.length < 1500) rightSideText.push(t.slice(0, 500));
            }
          });

          // v13.78 · 视频/iframe 数量
          const n_videos = document.querySelectorAll('video, iframe[src*="video"]').length;

          // v13.78 · 是否 modal/详情页 (有 close 按钮/ESC 可关?)
          const closeCandidates = [];
          document.querySelectorAll('[aria-label*="close" i], [aria-label*="关闭"], .close, [class*="close-btn"]').forEach(el => {
            if (inView(el)) closeCandidates.push({
              text: abbr(el.innerText||'', 30),
              aria: abbr(el.getAttribute('aria-label')||'', 30),
            });
          });

          return {
            url: window.location.href,
            title: document.title,
            scrollY: window.scrollY,
            maxScrollY: Math.max(0, document.body.scrollHeight - window.innerHeight),
            scroll_pct: Math.round(window.scrollY / Math.max(1, document.body.scrollHeight - window.innerHeight) * 100),
            elements: nodes,
            main_content_blocks: bigTextBlocks,
            right_sidebar: rightSideText,
            n_videos: n_videos,
            close_candidates: closeCandidates.slice(0, 3),
            is_detail_page: /\\/explore\\/[a-f0-9]/.test(window.location.href) ||
                           /\\/discovery\\/item\\//.test(window.location.href),
          };
        }""", max_elements)
        return data
    except Exception as e:
        return {"error": str(e)[:200]}


# v13.78 · click 前意图审查
# v13.87 · wigolo-eval 建议 P0 · 意图理解层升 opus (env 开关避免默认涨钱)
def check_click_intent(candidate_el: dict, goal: str, current_todo: str,
                       model: str = None, timeout: int = 20) -> dict:
    """点击前独立 LLM 判「这个元素点了合不合适」
    v13.87 · MEYO_INTENT_MODEL 环境变量控制 (默认 haiku · 设 opus 走深判)
    """
    if model is None:
        model = os.environ.get("MEYO_INTENT_MODEL", "haiku")
    user_msg = (
        f"goal: {goal[:200]}\n\n"
        f"current_todo: {current_todo[:200]}\n\n"
        f"候选元素:\n"
        f"  type: {candidate_el.get('type','?')}\n"
        f"  text: {candidate_el.get('text','')[:80]}\n"
        f"  aria: {candidate_el.get('aria','')[:60]}\n"
        f"  media_type: {candidate_el.get('media_type','?')}\n"
        f"  href: {candidate_el.get('href','')[:100]}\n\n"
        f"审查 JSON:"
    )
    r = call_llm(CLICK_INTENT_SYSTEM, user_msg, model_override=model,
                timeout=timeout)
    if r["ok"]:
        return try_parse_json(r["raw"],
                default={"go": True, "reason": "no_json", "confidence": 50})
    return {"go": True, "reason": "llm_err", "confidence": 50}


# v13.78 · 换 query
def _switch_query_fallback(goal: str, current_query: str, tried: list) -> dict:
    """LLM 换词器不可用时的 fail-safe:派生一个「真的不同」的候选,而不是回吐
    current_query(回吐原词→下游以为换了实则没换·卡死循环空转)。策略:
    ①goal 与 current_query 不同→用 goal 当新词;②否则给 current_query 加一个
    未试过的检索修饰词。degraded=True 让调用方知道这是兜底而非 LLM 智能换词。"""
    tried_set = {str(t).strip() for t in (tried or [])}
    tried_set.add(str(current_query or "").strip())
    goal_q = str(goal or "").strip()
    if goal_q and goal_q not in tried_set:
        return {"candidates": [goal_q], "recommend": goal_q, "degraded": True}
    base = (current_query or goal_q or "").strip()
    for suffix in (" 官方", " 原始来源", " 文档", " 定义", " 详细"):
        cand = (base + suffix).strip()
        if cand and cand not in tried_set:
            return {"candidates": [cand], "recommend": cand, "degraded": True}
    # 实在没得换(全试过)→空候选·让调用方走各自的 no_new_query 分支·不假装换成功
    return {"candidates": [], "recommend": "", "degraded": True}


def switch_query_llm(goal: str, current_query: str, tried: list,
                     model: str = "sonnet", timeout: int = 45) -> dict:
    try:
        user_msg = (
            f"goal: {goal[:200]}\n\n"
            f"current_query: {current_query}\n\n"
            f"tried queries:\n{json.dumps(tried[-5:], ensure_ascii=False)}\n\n"
            f"换 query JSON:"
        )
        r = call_llm(SWITCH_QUERY_SYSTEM, user_msg, model_override=model,
                    timeout=timeout)
        if r.get("ok"):
            parsed = try_parse_json(r["raw"], default=None)
            cands = [str(c).strip() for c in (parsed or {}).get("candidates", [])
                     if str(c).strip()] if isinstance(parsed, dict) else []
            # LLM 只回吐原词/空→等于没换·走兜底派生真正不同的候选(fail-safe)
            if cands and cands != [str(current_query or "").strip()]:
                return {"candidates": cands,
                        "recommend": parsed.get("recommend", cands[0]) or cands[0]}
    except Exception:
        pass
    # LLM 挂/超时/只回原词 → 兜底派生·绝不回吐 current_query 假装换成功
    return _switch_query_fallback(goal, current_query, tried)


def decide_action(kb: dict, snapshot: dict, history: list,
                  satisfaction: int, exploration: int,
                  model: str = "sonnet", timeout: int = 60) -> dict:
    pending_todos = [t for t in kb.get("todos", []) if t.get("status") != "done"]
    kb_summary = {
        "goal": kb.get("goal", "")[:150],
        "todos_done": [t.get("text","")[:60] for t in kb.get("todos", []) if t.get("status") == "done"],
        "todos_pending": [t.get("text","")[:60] for t in pending_todos],
        "knowledge_count": len(kb.get("knowledge", [])),
        "recent_titles": [k.get("title", "")[:50] for k in kb.get("knowledge", [])[-3:]],
        "claim_evidence_coverage": kb.get("claim_evidence_coverage", {}),
        "identity_relation_counts": {
            state: sum(1 for relation in kb.get("identity_relations", []) if relation.get("state") == state)
            for state in ("confirmed", "probable", "candidate", "rejected")
        },
    }
    # v13.87 · 抄 RE-TRAC continue_prompt · 前 cycle 压缩状态塞进 · 敢质疑
    cycle_hint = build_cycle_continuation_prompt(kb.get("cycle_states", []))
    # 五子 Agent 的 Judge 未解问题注入决策上下文，让浏览器知道还缺什么
    swarm_unresolved = []
    for lesson in kb.get("meta_lessons", []):
        if lesson.get("kind") == "five_agent_swarm":
            swarm_unresolved = lesson.get("unresolved_questions", [])[:5]
            break
    unresolved_hint = ""
    if swarm_unresolved:
        unresolved_hint = (f"\n\n═══ Judge 未解问题（优先研究）═══\n"
                           + "\n".join(f"  · {q[:120]}" for q in swarm_unresolved))
    user_msg = (
        f"knowledge_base:\n{json.dumps(kb_summary, ensure_ascii=False, indent=1)[:1500]}\n\n"
        f"satisfaction: {satisfaction}/100\n"
        f"exploration_desire: {exploration}/100\n\n"
        f"history (最近 5):\n{json.dumps(history[-5:], ensure_ascii=False, indent=1)[:1500]}\n\n"
        f"page:\n"
        f"  URL: {snapshot.get('url','')[:150]}\n"
        f"  scroll: {snapshot.get('scroll_pct',0)}%\n"
        f"  elements: {json.dumps(snapshot.get('elements',[]), ensure_ascii=False, indent=1)[:2500]}\n"
        f"  content_blocks: {json.dumps(snapshot.get('main_content_blocks',[]), ensure_ascii=False)[:500]}\n"
        f"{cycle_hint}{unresolved_hint}\n\n"
        f"next_action JSON:"
    )
    r = call_llm(DECIDE_SYSTEM, user_msg, model_override=model, timeout=timeout)
    if r["ok"]:
        return try_parse_json(r["raw"], default={"action": "done", "why": "no_json"})
    return {"action": "done", "why": f"llm_err:{r.get('error','')[:60]}"}


def sediment_content(raw_content: str, for_todo_id: int, goal: str,
                     source_url: str, model: str = "sonnet",
                     timeout: int = 45) -> dict:
    """LLM 结构化沉淀 · 抽 insights/methods/相关性"""
    user_msg = (
        f"goal: {goal[:200]}\n\n"
        f"source_url: {source_url[:150]}\n\n"
        f"raw content (前 3000 字):\n{raw_content[:3000]}\n\n"
        f"沉淀 JSON:"
    )
    r = call_llm(SEDIMENT_SYSTEM, user_msg, model_override=model, timeout=timeout)
    if r["ok"]:
        parsed = try_parse_json(r["raw"],
                default={"title": "unknown", "key_insights": [],
                        "methods": [], "relevance_score": 50})
        parsed["source_url"] = source_url
        parsed["for_todo"] = for_todo_id
        parsed["raw_snippet"] = raw_content[:500]
        return parsed
    return {"title": "sediment_err", "key_insights": [],
            "source_url": source_url, "for_todo": for_todo_id}


def self_score(kb: dict, history: list, model: str = "sonnet",
               timeout: int = 45) -> dict:
    """满意度 + 探索欲 双评"""
    summary = {
        "goal": kb.get("goal", "")[:150],
        "todos_status": {t.get("text","")[:40]: t.get("status","pending")
                        for t in kb.get("todos", [])[:10]},
        "knowledge_count": len(kb.get("knowledge", [])),
        "insights_total": sum(len(k.get("key_insights", []))
                             for k in kb.get("knowledge", [])),
        "recent_actions": [h.get("action", {}).get("action", "?")
                          for h in history[-5:]],
    }
    r = call_llm(SCORE_SYSTEM,
                f"state:\n{json.dumps(summary, ensure_ascii=False, indent=1)[:2000]}\n\n评分 JSON:",
                model_override=model, timeout=timeout)
    if r["ok"]:
        return try_parse_json(r["raw"],
                default={"satisfaction": 50, "exploration_desire": 50})
    return {"satisfaction": 50, "exploration_desire": 50}


def meta_reflect(kb: dict, history: list, satisfaction_trace: list,
                 model: str = "opus", timeout: int = 60) -> dict:
    """每 5 步元反思 · 判是否换策略"""
    user_msg = (
        f"goal: {kb.get('goal','')[:200]}\n\n"
        f"satisfaction_trace: {satisfaction_trace[-8:]}\n\n"
        f"todos:\n{json.dumps(kb.get('todos',[])[:10], ensure_ascii=False, indent=1)[:1000]}\n\n"
        f"recent_actions:\n{json.dumps(history[-6:], ensure_ascii=False, indent=1)[:1500]}\n\n"
        f"元反思 JSON:"
    )
    r = call_llm(META_REFLECT_SYSTEM, user_msg, model_override=model,
                timeout=timeout)
    if r["ok"]:
        return try_parse_json(r["raw"],
                default={"should_switch_mode": False, "new_strategy": "",
                        "why": ""})
    return {"should_switch_mode": False}


# ────────────── 执行动作 (v13.76 复用 + 扩展) ──────────────

def verify_todo_completion(kb: dict, todo_id: int) -> dict:
    """Todo 完成硬门：必须有该 Todo 对应的可追溯正文证据。

    不接受列表页、无 URL 的模型总结或仅凭满意度的自我声明。若 Todo 指定了
    success_criteria/acceptance 中的数字，则将其作为最小独立来源数；默认至少 1 条。
    """
    import re
    todo = next((t for t in kb.get("todos", []) if t.get("id") == todo_id), None)
    if todo is None:
        return {"ok": False, "reason": "todo_not_found", "evidence_count": 0}
    requirement = str(todo.get("success_criteria") or todo.get("acceptance") or "")
    match = re.search(r"(\d+)", requirement)
    required_sources = max(1, int(match.group(1))) if match else 1
    valid = []
    seen_urls = set()
    for item in kb.get("knowledge", []):
        if item.get("for_todo") != todo_id:
            continue
        url = str(item.get("source_url") or item.get("url") or "").strip()
        insights = item.get("key_insights") or []
        if not url or url in seen_urls or not any(str(x).strip() for x in insights):
            continue
        seen_urls.add(url)
        valid.append(url)
    return {
        "ok": len(valid) >= required_sources,
        "reason": "evidence_sufficient" if len(valid) >= required_sources else "insufficient_traceable_evidence",
        "evidence_count": len(valid),
        "required_sources": required_sources,
        "source_urls": valid,
    }


def _apply_swarm_judge_to_kb(kb: dict, swarm: dict, session_id: str = "") -> None:
    """把五子 Agent 的 Judge 结论回写到 KB：写入 evidence 并尝试标记 Todo 为 done。

    提取为独立函数便于单元测试。在 run_deep_research 中由 swarm 回写段调用。
    """
    swarm_plan = swarm.get("plan", {})
    plan_questions = swarm_plan.get("questions", []) if isinstance(swarm_plan, dict) else []
    for ci, conclusion in enumerate(swarm.get("conclusions", [])):
        if not isinstance(conclusion, dict):
            continue
        verdict = str(conclusion.get("verdict", "")).strip().lower()
        if verdict not in ("verified", "partial", "contradicted"):
            continue
        urls = [str(u).strip() for u in conclusion.get("source_urls", []) if str(u).strip()]
        claim = str(conclusion.get("claim", "")).strip()
        reason = str(conclusion.get("reason", "")).strip()
        # 尝试匹配到对应 Todo（按 claim 关键词匹配 Todo 文本）
        matched_todo_id = None
        for todo in kb["todos"]:
            todo_text = str(todo.get("text", "")).strip()
            if claim and any(kw in todo_text for kw in claim.split()[:3] if len(kw) > 1):
                matched_todo_id = todo.get("id")
                break
        # 如果没有按 claim 匹配到 Todo，则尝试按 plan_questions 索引匹配
        if matched_todo_id is None and ci < len(plan_questions):
            q_claim = str(plan_questions[ci].get("claim", "")).strip()
            for todo in kb["todos"]:
                if q_claim and q_claim in str(todo.get("text", "")):
                    matched_todo_id = todo.get("id")
                    break
        for url in urls:
            kb["knowledge"].append({
                "title": claim[:120] or f"Judge conclusion #{ci+1}",
                "key_insights": [reason[:1000]] if reason else [claim[:500]],
                "methods": [],
                "relevance_score": 90 if verdict == "verified" else (60 if verdict == "partial" else 30),
                "source_url": url,
                "for_todo": matched_todo_id,
                "lane": "judge",
                "verdict": verdict,
            })
        # verified/partial 且通过证据门 → 标记 done
        # contradicted → 不标记 done（结论被反驳，需要进一步研究）
        if matched_todo_id is not None and verdict in ("verified", "partial"):
            gate = verify_todo_completion(kb, matched_todo_id)
            if gate["ok"]:
                for todo in kb["todos"]:
                    if todo.get("id") == matched_todo_id:
                        todo["status"] = "done"
                        todo.setdefault("evidence", []).extend(gate["source_urls"])
                        break
                if session_id:
                    _log({"type": "swarm_judge_todo_done", "id": session_id,
                          "todo_id": matched_todo_id, "verdict": verdict,
                          "evidence_n": gate["evidence_count"]})
        elif matched_todo_id is not None and verdict == "contradicted":
            # contradicted 结论写入 meta_lessons 作为警示
            kb["meta_lessons"].append({
                "kind": "judge_contradicted",
                "todo_id": matched_todo_id,
                "claim": claim[:200],
                "reason": reason[:300],
                "source_urls": urls[:3],
            })
            if session_id:
                _log({"type": "swarm_judge_contradicted", "id": session_id,
                      "todo_id": matched_todo_id, "claim": claim[:100]})


def _consume_unresolved(kb: dict, insights: list, session_id: str, step: int) -> None:
    """浏览器循环每收集到新证据后，检查是否回答了 Judge 的未解问题。

    匹配策略：
    - 如果问题按空格分词后有多个词，所有词都出现在 insights 中则视为已回答；
    - 如果问题是一个没有空格的短语（如中文），取前 6 个字符做子串匹配。
    被回答的问题从 kb['swarm']['unresolved_questions'] 和 meta_lessons 中移除。
    """
    if not insights:
        return
    swarm = kb.get("swarm")
    if not swarm or not swarm.get("unresolved_questions"):
        return
    combined_text = " ".join(str(i) for i in insights).lower()
    remaining = []
    consumed = []
    for q in swarm["unresolved_questions"]:
        q_str = str(q).strip()
        if not q_str:
            continue
        q_lower = q_str.lower()
        words = q_str.split()
        if len(words) > 1:
            # v13.88.9L-64 · 整合修: 原 all(全命中) 太严·一个泛词(方法/用法/是什么)
            #   没出现就永远消费不掉→未解问题一直挂·浏览器loop空转追已答问题。
            #   改: 过滤泛词 + 多数命中(ceil(2/3)·至少1实词)即算已回答。
            _STOP = {"方法", "用法", "是什么", "怎么", "如何", "多少", "哪个", "哪些",
                     "什么", "the", "a", "an", "of", "is", "what", "how", "里"}
            keywords = [w.lower() for w in words
                        if len(w) > 1 and w.lower() not in _STOP][:5]
            if keywords:
                hit_n = sum(1 for kw in keywords if kw in combined_text)
                need = max(1, (len(keywords) * 2 + 2) // 3)  # ceil(2/3)
                matched = hit_n >= need
            else:
                matched = False
        else:
            # 单词/中文短语：检查前 4 字符或后 4 字符是否作为子串出现
            # 前 4 字符捕获问题开头（如"确认版本"），后 4 字符捕获核心词（如"性能限制"）
            prefix = q_lower[:4]
            suffix = q_lower[-4:]
            matched = (len(prefix) >= 2 and prefix in combined_text) or \
                      (len(suffix) >= 2 and suffix in combined_text)
        if matched:
            consumed.append(q_str)
        else:
            remaining.append(q_str)
    if consumed:
        swarm["unresolved_questions"] = remaining
        for lesson in kb.get("meta_lessons", []):
            if lesson.get("kind") == "five_agent_swarm":
                lesson["unresolved_questions"] = [
                    q for q in lesson.get("unresolved_questions", [])
                    if str(q).strip() not in {str(c).strip() for c in consumed}
                ]
                break
        _log({"type": "unresolved_consumed", "id": session_id, "step": step,
              "consumed_n": len(consumed), "remaining_n": len(remaining),
              "consumed": [c[:80] for c in consumed]})


def build_research_quality_report(kb: dict) -> dict:
    """汇总可交付答案的证据质量，而非以请求量作为完成度。

    该摘要可被最终回答、审计和下一轮决策共用：只有 covered 的 claim 才是
    已覆盖结论；candidate/probable 身份边不得被误写成已确认事实。
    """
    coverage = kb.get("claim_evidence_coverage") or {}
    entries = [item for item in coverage.values() if isinstance(item, dict)]
    covered = [item.get("claim", "") for item in entries if item.get("covered")]
    gaps = [item.get("claim", "") for item in entries if not item.get("covered")]
    states = ("confirmed", "probable", "candidate", "rejected")
    relations = kb.get("identity_relations") or []
    return {
        "claims_total": len(entries),
        "claims_covered": len(covered),
        "claim_coverage_ratio": round(len(covered) / len(entries), 3) if entries else 0.0,
        "covered_claims": covered,
        "coverage_gaps": gaps,
        "claims": coverage,
        "identity_relation_counts": {
            state: sum(1 for edge in relations if isinstance(edge, dict) and edge.get("state") == state)
            for state in states
        },
        "unresolved_questions": list((kb.get("swarm") or {}).get("unresolved_questions") or [])[:10],
    }


def _exec_action(page, action: dict, snapshot: dict, kb: dict) -> dict:
    """执行浏览器动作；Todo 完成前强制通过可追溯证据验收门。"""
    from path_monitor import log_path_step

    at = action.get("action")

    if at == "next_todo":
        tid = action.get("target_todo_id")
        gate = verify_todo_completion(kb, tid)
        if not gate["ok"]:
            for todo in kb.get("todos", []):
                if todo.get("id") == tid:
                    todo["status"] = "investigating"
                    todo["completion_gate"] = gate
                    break
            log_path_step("next_todo", f"todo_id={tid}", False,
                         f"evidence={gate['evidence_count']}/{gate['required_sources']}", "deep_research")
            return {"ok": False, "detail": "todo_evidence_gate_blocked", "gate": gate,
                    "should_pick_another": False}
        for todo in kb.get("todos", []):
            if todo.get("id") == tid:
                todo["status"] = "done"
                todo["completion_gate"] = gate
                break
        log_path_step("next_todo", f"todo_id={tid}", True,
                     f"evidence={gate['evidence_count']}/{gate['required_sources']}", "deep_research")
        return {"ok": True, "detail": f"todo {tid} verified_done", "gate": gate}

    if at == "deepen":
        page.evaluate("window.scrollBy(0, 500)")
        page.wait_for_timeout(1200)
        log_path_step("deepen", "scroll+wait", True, "", "deep_research")
        return {"ok": True, "detail": "deepened by scroll"}

    # v13.78 · close: ESC 或点关闭·返列表
    if at == "close":
        closed = False
        # 优先点 close 按钮
        for cand in snapshot.get("close_candidates", []):
            try:
                text = cand.get("text") or cand.get("aria", "")
                if text:
                    page.get_by_text(text[:20], exact=False).first.click(timeout=3000)
                    closed = True
                    break
            except Exception:
                pass
        if not closed:
            page.keyboard.press("Escape")
            closed = True
        page.wait_for_timeout(2000)
        log_path_step("close", "ESC or button", closed,
                     "back to list", "deep_research")
        return {"ok": closed, "detail": "closed detail"}

    # 换 Query 一律走统一 SearchAgent，而不是跳回某个站点的搜索页。
    # 候选结果写回 KB，下一轮 decide 可按 URL 执行 goto 深读。
    if at == "switch_query":
        new_q = action.get("new_query", "").strip()
        if not new_q:
            sq = switch_query_llm(
                goal=kb.get("goal", ""),
                current_query=kb.get("current_query", ""),
                tried=kb.get("tried_queries", []),
            )
            new_q = sq.get("candidates", [""])[0] if sq.get("candidates") else ""
        if not new_q:
            return {"ok": False, "detail": "no_new_query"}
        kb.setdefault("tried_queries", []).append(kb.get("current_query", ""))
        kb["current_query"] = new_q
        try:
            from search_agent import SearchAgent
            # v13.90 省电: 进化搜索默认禁 CDP 浏览器(最吃性能)·走 http_only。
            try:
                from power_gate import browser_allowed
                _http_only = not browser_allowed()
            except Exception:
                _http_only = True  # 拿不到闸就保守禁 CDP
            # 新旧 SearchAgent 都必须可用：深研是可选增强，不能因构造参数或
            # http_only 新关键字与既有测试/旧实现不兼容而把一次有效搜索误报为失败。
            try:
                search_agent = SearchAgent(use_cache=True, use_forger=True, use_supervisor=True)
            except TypeError:
                search_agent = SearchAgent()
            try:
                hits = search_agent.search(new_q, mode="deep", top_k=6, http_only=_http_only)
            except TypeError as exc:
                if "http_only" not in str(exc) and "unexpected keyword" not in str(exc):
                    raise
                hits = search_agent.search(new_q, mode="deep", top_k=6)
            candidates = []
            known_urls = {k.get("source_url", "") for k in kb.get("knowledge", [])}
            for hit in hits or []:
                if not isinstance(hit, dict) or not hit.get("url") or hit["url"] in known_urls:
                    continue
                candidates.append({
                    "url": hit.get("url", ""),
                    "title": hit.get("title", "")[:160],
                    "content": hit.get("content", "")[:1200],
                    "source": hit.get("source", ""),
                    "query": new_q,
                })
            kb.setdefault("search_candidates", []).extend(candidates)
            kb["meta_lessons"].append({
                "kind": "search_backend_switch",
                "query": new_q,
                "candidate_urls": [c["url"] for c in candidates[:6]],
            })
            log_path_step("switch_query", new_q, bool(candidates),
                         f"SearchAgent 候选={len(candidates)}", "deep_research")
            # fail-safe:query 已成功切换(kb.current_query 已更新)就算动作成功·
            # 「0 条新候选」是正常检索结果(如全被 known_urls 去重)·不是失败——
            # 原先 ok=bool(candidates) 会把它当失败累进 consecutive_failures·
            # 逼出无谓的 meta_reflect/强制切换·反而扰乱研究流程。
            return {"ok": True,
                    "detail": f"search_backend_candidates={len(candidates)}"
                              + ("" if candidates else "(0新候选·query已切换·非失败)"),
                    "data": {"kind": "search_candidates", "items": candidates}}
        except Exception as exc:
            log_path_step("switch_query", new_q, False,
                         f"SearchAgent:{type(exc).__name__}", "deep_research")
            # 搜索后端异常也不算硬失败:query 已切换·下一轮 decide 可继续·
            # 不中断整个研究流程(fail-safe·仅标注 degraded 供追踪)。
            return {"ok": True, "degraded": True,
                    "detail": f"search_backend_err:{type(exc).__name__}(query已切换·降级继续)"}

    # v13.78 · skip: 不点这个元素·换下一个
    if at == "skip":
        log_path_step("skip", action.get("why", ""), True,
                     "skip this element", "deep_research")
        return {"ok": True, "detail": "skipped"}

    # 复用 v13.76 的 _exec_action 逻辑
    try:
        if at == "goto":
            url = action.get("url", "").strip()
            if not url:
                candidates = kb.get("search_candidates", [])
                url = next((c.get("url", "") for c in candidates
                            if c.get("url") and c.get("url") not in kb.get("visited_urls", [])), "")
            if not url:
                return {"ok": False, "detail": "no_url_or_candidate"}
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            log_path_step("goto", url, True, "", "deep_research")
            return {"ok": True, "detail": f"goto {url[:80]}"}

        elif at == "type":
            text = action.get("text", "")
            idx = action.get("element_idx")
            target = None
            if idx is not None and idx < len(snapshot.get("elements", [])):
                el = snapshot["elements"][idx]
                if el.get("selector"):
                    try:
                        target = page.locator(el["selector"]).first
                    except Exception: pass
            if not target:
                for loc in page.locator("textarea, input").all():
                    try:
                        if loc.is_visible():
                            target = loc; break
                    except Exception: pass
            if not target:
                return {"ok": False, "detail": "no_input"}
            target.click(force=True)
            page.wait_for_timeout(400)
            # 清空老内容
            try:
                target.fill("")
                page.wait_for_timeout(200)
            except Exception: pass
            for c in text:
                page.keyboard.type(c, delay=80)
            log_path_step("type", text[:80], True,
                         f"len={len(text)}", "deep_research")
            return {"ok": True, "detail": f"typed '{text[:60]}'"}

        elif at == "click":
            idx = action.get("element_idx")
            selector = action.get("selector")
            text_hint = action.get("text")
            clicked = False

            # v13.78 · click 前意图预判
            candidate_el = None
            if idx is not None and idx < len(snapshot.get("elements", [])):
                candidate_el = snapshot["elements"][idx]

            if candidate_el:
                # URL 去重
                href = candidate_el.get("href", "")
                if href and href in kb.get("visited_urls", set()):
                    log_path_step("click", "SKIP visited",
                                 False, f"already visited {href[:80]}",
                                 "deep_research")
                    return {"ok": False, "detail": "already_visited",
                           "should_pick_another": True}
                # 意图预判
                current_todo_text = ""
                for t in kb.get("todos", []):
                    if t.get("status") != "done":
                        current_todo_text = t.get("text", ""); break
                intent = check_click_intent(candidate_el,
                                           kb.get("goal", ""),
                                           current_todo_text)
                kb.setdefault("intent_checks", []).append({
                    "el_text": candidate_el.get("text", "")[:60],
                    "media": candidate_el.get("media_type", "?"),
                    "go": intent.get("go"),
                    "reason": intent.get("reason", "")[:80],
                })
                if not intent.get("go"):
                    log_path_step("click", "REJECTED by intent",
                                 False, intent.get("reason", "")[:100],
                                 "deep_research")
                    return {"ok": False, "detail": "intent_reject",
                           "reason": intent.get("reason", ""),
                           "should_pick_another": True}

            if candidate_el:
                el = candidate_el
                if el.get("type") == "link" and el.get("href"):
                    try:
                        page.locator(f"a[href='{el['href']}']").first.click(timeout=5000)
                        clicked = True
                        kb.setdefault("visited_urls", []).append(el["href"])
                    except Exception: pass
                elif el.get("text"):
                    try:
                        page.get_by_text(el["text"][:40], exact=False).first.click(timeout=5000)
                        clicked = True
                    except Exception: pass
            if not clicked and selector:
                try:
                    page.locator(selector).first.click(timeout=5000)
                    clicked = True
                except Exception: pass
            if not clicked and text_hint:
                try:
                    page.get_by_text(text_hint[:40], exact=False).first.click(timeout=5000)
                    clicked = True
                except Exception: pass
            if clicked:
                page.wait_for_timeout(3000)
                log_path_step("click",
                             (text_hint or selector or f"idx={idx}")[:80],
                             True, "", "deep_research")
                return {"ok": True, "detail": "clicked"}
            log_path_step("click", "failed", False, "no target",
                         "deep_research")
            return {"ok": False, "detail": "click_failed"}

        elif at == "press":
            key = action.get("key", "Enter")
            page.keyboard.press(key)
            page.wait_for_timeout(2500)
            log_path_step("press", key, True, "", "deep_research")
            return {"ok": True, "detail": f"pressed {key}"}

        elif at == "scroll":
            direction = action.get("direction", "down")
            amount = action.get("amount", 500)
            dy = amount if direction == "down" else -amount
            page.evaluate(f"window.scrollBy(0, {dy})")
            page.wait_for_timeout(1000)
            log_path_step("scroll", f"{direction}×{amount}", True, "",
                         "deep_research")
            return {"ok": True, "detail": f"scrolled {direction}"}

        elif at == "extract":
            what = action.get("what", "body")
            if what in ("notes", "list"):
                data = page.evaluate("""() => {
                  const items = [];
                  document.querySelectorAll("a[href*='/search_result'], a[href*='/explore/']").forEach(a => {
                    const t = (a.innerText||'').trim();
                    if (t && t.length > 3)
                      items.push({text: t.slice(0,100), href: a.href});
                  });
                  const seen = new Set();
                  return items.filter(x => !seen.has(x.href) && seen.add(x.href)).slice(0,10);
                }""")
                log_path_step("extract", f"notes×{len(data or [])}", True,
                             "", "deep_research")
                return {"ok": True, "detail": f"notes={len(data or [])}",
                       "data": {"kind": "notes", "items": data}}
            else:
                # v13.78 · extract 前 URL 去重
                cur_url = snapshot.get("url", "").split("?")[0]  # 去掉 query
                extracted_urls = [k.get("source_url","").split("?")[0]
                                 for k in kb.get("knowledge", [])]
                if cur_url in extracted_urls:
                    log_path_step("extract", "SKIP already extracted",
                                 False, f"url {cur_url[:80]}",
                                 "deep_research")
                    return {"ok": False, "detail": "already_extracted",
                           "should_close_or_scroll": True}

                text = page.evaluate("""() => {
                  const sels = ['div[class*="note-scroller"]', '.note-content',
                               '#detail-desc', 'article', 'main', 'body'];
                  for (const s of sels) {
                    const el = document.querySelector(s);
                    if (el && (el.innerText||'').length > 100)
                      return {sel: s, text: el.innerText.slice(0, 4000)};
                  }
                  return {sel: 'body', text: document.body.innerText.slice(0,4000)};
                }""")
                log_path_step("extract", f"body {len(text.get('text',''))}",
                             True, "", "deep_research")
                return {"ok": True, "detail": f"body {len(text.get('text',''))}",
                       "data": {"kind": "body", "url": snapshot.get("url",""),
                               **text}}

        elif at == "done":
            log_path_step("done", "goal_reached", True,
                         action.get("why", ""), "deep_research")
            return {"ok": True, "detail": "done"}

        return {"ok": False, "detail": f"unknown:{at}"}
    except Exception as e:
        return {"ok": False, "detail": f"exc:{str(e)[:200]}"}


# ────────────── 主 loop v2 ──────────────

def compress_cycle_trajectory(kb: dict, history: list, cycle_idx: int,
                              model: str = "sonnet", timeout: int = 90) -> dict:
    """v13.87 · 抄 RE-TRAC (ICML 2026 · microsoft/InfoAgent)
    · cycle 结束把本轮全部经验压成 5 段结构化状态

    输出 5 段:
      0) current_best_answer · 目前最靠谱的答案
      1) facts_evidence      · 已收集事实+来源+verified 状态
      2) sources_visited     · 访问过的 URL (下 cycle 去重)
      3) uncertainties       · 未解决的疑点 (下 cycle 优先攻)
      4) failure_modes       · 失败的探索方向 (下 cycle 避坑)

    下 cycle 塞进 system prompt · 用户可选是否信任 (敢重启)
    """
    import subprocess as _sp
    kb_summary = {
        "goal": kb.get("goal", "")[:200],
        "todos_status": [
            {"id": t.get("id"), "text": t.get("text", "")[:100],
             "status": t.get("status")}
            for t in kb.get("todos", [])
        ],
        "knowledge_count": len(kb.get("knowledge", [])),
        "knowledge_titles": [
            k.get("title", "")[:80]
            for k in kb.get("knowledge", [])[-10:]
        ],
        "visited_urls": kb.get("visited_urls", [])[-15:],
        "tried_queries": kb.get("tried_queries", [])[-8:],
        "meta_lessons": kb.get("meta_lessons", []),
        "recent_actions": [
            {"step": h.get("step"),
             "action": h.get("action", {}).get("action", "?"),
             "sat": h.get("sat")}
            for h in history[-15:]
        ],
    }
    prompt = f"""你把 cycle {cycle_idx} 的探索压成 5 段结构化状态·给下 cycle 用。

原始目标: {kb.get('goal', '')}

本 cycle 已知信息(kb_summary):
{json.dumps(kb_summary, ensure_ascii=False, indent=2)[:6000]}

严格输出 JSON · 5 个字段:
{{
  "current_best_answer": "<目前最靠谱的答案 · 无则 null>",
  "facts_evidence": [
    {{"fact": "<原文事实>", "source": "<URL 或工具>", "verified": "yes|no|partial"}}
  ],
  "sources_visited": ["<URL>", ...],
  "uncertainties": [
    {{"question": "<未解决的疑点>", "why_blocked": "<为啥没解决>"}}
  ],
  "failure_modes": [
    {{"tried": "<尝试过啥>", "why_failed": "<失败原因>", "avoid": "<下 cycle 别这么做>"}}
  ]
}}

只输出 JSON · 不加解释。"""
    try:
        cmd = ["claude", "--model", model, "--max-turns", "1", "--print"]
        p = spawn_claude_cli(cmd, input=prompt, timeout=timeout)
        if getattr(p, "blocked", False):
            raise RuntimeError("spawn_gate_blocked")
        out = (p.stdout or "").strip()
        # 兜底解析
        if "```" in out:
            out = out.split("```json")[-1].split("```")[0] if "```json" in out else out.split("```")[1] if out.count("```") >= 2 else out
            out = out.strip()
        return json.loads(out)
    except Exception as e:
        return {
            "current_best_answer": None,
            "facts_evidence": [],
            "sources_visited": kb.get("visited_urls", [])[-10:],
            "uncertainties": [{"question": "cycle 压缩失败", "why_blocked": str(e)[:100]}],
            "failure_modes": [],
            "_error": str(e)[:200],
        }


def build_cycle_continuation_prompt(compressed_states: list) -> str:
    """v13.87 · 抄 RE-TRAC continue_prompt (敢重启的巧思)
    · 明确告诉下 cycle: 上一 cycle 的总结*建议不是真相*·可无视重启

    这是 RE-TRAC 论文关键 trick · 反过度依赖压缩
    """
    if not compressed_states:
        return ""
    latest = compressed_states[-1]
    lines = [
        "\n\n═══ 前 cycle 压缩状态 (可参考·非圣旨) ═══",
        "",
        "【关键提示】以下是前几轮探索的*建议性*总结·不是绝对真理。它可能包含:",
        "  · 幻觉·未验证的假设·过早的结论",
        "  · 走进死胡同的逻辑循环",
        "",
        "如果发现以下任一情况·可以完全无视·重启新策略:",
        "  · 总结方向感觉是死胡同",
        "  · 关键事实被标记 verified=no/partial",
        "  · 你判断当前搜索空间不够",
        "",
    ]
    for idx, s in enumerate(compressed_states[-3:]):  # 只塞最近 3 cycle
        lines.append(f"─ cycle {len(compressed_states)-len(compressed_states[-3:])+idx+1} 压缩 ─")
        if s.get("current_best_answer"):
            lines.append(f"  最佳答案: {s['current_best_answer'][:200]}")
        if s.get("facts_evidence"):
            lines.append(f"  已知事实({len(s['facts_evidence'])} 条·verified 状态见下):")
            for f in s["facts_evidence"][:5]:
                lines.append(f"    · [{f.get('verified','?')}] {f.get('fact','')[:120]}"
                             f" ← {f.get('source','?')[:60]}")
        if s.get("uncertainties"):
            lines.append(f"  未解决疑点({len(s['uncertainties'])}·下 cycle 优先攻):")
            for u in s["uncertainties"][:5]:
                lines.append(f"    · {u.get('question','')[:100]}"
                             f" (卡在: {u.get('why_blocked','')[:60]})")
        if s.get("failure_modes"):
            lines.append(f"  失败方向({len(s['failure_modes'])}·别再走):")
            for fm in s["failure_modes"][:3]:
                lines.append(f"    · 试过 {fm.get('tried','')[:80]}·"
                             f"失败因 {fm.get('why_failed','')[:60]}·"
                             f"改: {fm.get('avoid','')[:60]}")
        if s.get("sources_visited"):
            lines.append(f"  已访问 URL({len(s['sources_visited'])}·别重复):")
            for u in s["sources_visited"][:8]:
                lines.append(f"    · {u[:100]}")
        lines.append("")

    lines.append("═══ 现在开始新 cycle · 你可以质疑上面任何一条 ═══")
    return "\n".join(lines)


def run_deep_research(goal: str, start_url: str = None,
                     max_steps: int = 40,
                     min_satisfaction: int = 85,
                     reflect_every: int = 5,
                     cdp: str = None,
                     max_cycles: int = 1,
                     steps_per_cycle: int = None,
                     use_swarm: bool = True) -> dict:
    """深度研究员·长耐心 loop

    v13.87 (2026-07-22) · 加 cycle 层
      max_cycles: 探索轮次数 · 默认 1 (兼容原行为)·>1 时启用 RE-TRAC 压缩
      steps_per_cycle: 每 cycle 内 max_steps · 默认 max_steps//max_cycles
    v13.88.9 · 默认先跑五子 Agent：Planner / Scout / Evidence / Skeptic / Judge。
      子 Agent 先给出候选来源、支持证据、反证和未解问题；浏览器循环仅消费未解项，
      作为需要页面交互时的降级补充，而非替代证据研究。
    """
    from playwright.sync_api import sync_playwright
    from path_monitor import log_path_plan, log_path_done

    session_id = f"dr_{int(time.time())}"
    # v13.87 · cycle 层参数处理
    if steps_per_cycle is None:
        steps_per_cycle = max(10, max_steps // max(1, max_cycles))
    _log({"type": "start", "id": session_id, "goal": goal,
          "max_steps": max_steps, "min_satisfaction": min_satisfaction,
          "max_cycles": max_cycles, "steps_per_cycle": steps_per_cycle})

    log_path_plan(query=goal, planned_steps=[
        {"action": "loop", "target": "plan→observe→decide→act→sediment→score→reflect",
         "why": "deep research long loop"},
    ], caller="deep_research")

    # 0. 拆 todo list
    plan = plan_todos(goal)
    todos = plan.get("todos", [])
    for i, t in enumerate(todos):
        t.setdefault("id", i + 1)
        t.setdefault("status", "pending")
        t.setdefault("evidence", [])
    kb = {
        "goal": goal,
        "session_id": session_id,
        "todos": todos,
        "knowledge": [],
        "meta_lessons": [],
        "visited_urls": [],       # v13.78 · URL 去重
        "tried_queries": [],      # v13.78 · 换 query 追踪
        "intent_checks": [],      # v13.78 · click 意图审查历史
        "current_query": "",      # v13.78 · 当前搜索 query
        "cycle_states": [],       # v13.87 · 每 cycle 压缩后的状态
        # 共享研究状态：身份图严格四态，结论以 claim 级可追溯覆盖率而非请求数为准。
        "identity_relations": [],
        "claim_evidence_coverage": {},
        "started_at": datetime.now().isoformat(),
    }
    _log({"type": "plan", "id": session_id, "n_todos": len(todos),
          "todos": [t.get("text", "")[:80] for t in todos]})

    # 五子 Agent 先完成证据发现、深读、反证与交叉裁决。失败时保留原浏览器研究路径，
    # 避免一个可选并行环节导致整个研究任务不可用。
    swarm = None
    if use_swarm:
        try:
            from deep_research_swarm import run_five_agent_research
            swarm = run_five_agent_research(goal)
            # swarm evidence 按 Todo 关联：用 plan questions 的 claim 匹配 Todo 文本
            swarm_questions = swarm.get("plan", {}).get("questions", [])
            for evidence in swarm.get("evidence", []):
                if not evidence.get("url"):
                    continue
                # 尝试匹配到对应 Todo
                matched_todo_id = None
                ev_title = str(evidence.get("title", ""))
                ev_lane = str(evidence.get("lane", ""))
                # evidence agent 的 title 通常包含 query 关键词，用它匹配 plan questions
                for qi, q in enumerate(swarm_questions):
                    q_claim = str(q.get("claim", ""))
                    q_query = str(q.get("query", ""))
                    if q_claim and (q_claim in ev_title or any(w in ev_title for w in q_query.split() if len(w) > 1)):
                        for todo in kb["todos"]:
                            if q_claim in str(todo.get("text", "")) or any(w in str(todo.get("text", "")) for w in q_claim.split()[:3] if len(w) > 1):
                                matched_todo_id = todo.get("id")
                                break
                        break
                kb["knowledge"].append({
                    "title": evidence.get("title", "")[:120],
                    "key_insights": [evidence.get("content", "")[:1000]] if evidence.get("content") else [],
                    "methods": [],
                    "relevance_score": 0,
                    "source_url": evidence.get("url", ""),
                    "for_todo": matched_todo_id,
                    "lane": evidence.get("lane", ""),
                })
            kb["identity_relations"] = swarm.get("identity_relations", [])
            kb["claim_evidence_coverage"] = swarm.get("claim_evidence_coverage", {})
            kb["swarm"] = {
                "task_id": swarm.get("task_id"),
                "conclusions": swarm.get("conclusions", []),
                "unresolved_questions": swarm.get("unresolved_questions", []),
                "identity_relations": kb["identity_relations"],
                "claim_evidence_coverage": kb["claim_evidence_coverage"],
            }
            kb["meta_lessons"].append({
                "kind": "five_agent_swarm",
                "conclusions": swarm.get("conclusions", [])[:6],
                "unresolved_questions": swarm.get("unresolved_questions", [])[:6],
                "claim_evidence_coverage": kb["claim_evidence_coverage"],
                "identity_relation_counts": {
                    state: sum(1 for relation in kb["identity_relations"] if relation.get("state") == state)
                    for state in ("confirmed", "probable", "candidate", "rejected")
                },
            })

            # Judge 结论回写：调用独立函数，把 verified / partial 结论写入
            # kb["knowledge"] 并尝试标记匹配 Todo 为 done（通过证据门校验）。
            _apply_swarm_judge_to_kb(kb, swarm, session_id=session_id)

            if not start_url:
                start_url = next(iter(swarm.get("candidate_urls", [])), None)
            # candidate_urls 兜底后仍空(蜂群标 candidate_urls_degraded)时·别让 start_url
            # 保持 None 导致浏览器 loop 从空白页瞎起步。用 goal 派生一个统一搜索入口做种子·
            # 保证 loop 有起点(fail-safe·不断链·不硬编码具体站点)。
            if not start_url and swarm.get("candidate_urls_degraded"):
                _seed_q = (kb.get("current_query") or goal or "").strip()
                if _seed_q:
                    import urllib.parse as _seed_up
                    start_url = "https://www.bing.com/search?q=" + _seed_up.quote(_seed_q[:200])
                    _log({"type": "candidate_urls_empty_seed_fallback",
                          "id": session_id, "q": _seed_q[:80],
                          "note": "[DEGRADED] 蜂群无候选URL·用 goal 派生搜索入口做浏览器种子"})
            _log({"type": "five_agent_swarm", "id": session_id,
                  "task_id": swarm.get("task_id"),
                  "evidence_n": len(swarm.get("evidence", [])),
                  "conclusions_n": len(swarm.get("conclusions", [])),
                  "unresolved_n": len(swarm.get("unresolved_questions", []))})
        except Exception as exc:
            kb["meta_lessons"].append({"kind": "five_agent_swarm_error", "error": f"{type(exc).__name__}:{str(exc)[:180]}"})
            _log({"type": "five_agent_swarm_error", "id": session_id,
                  "error": f"{type(exc).__name__}:{str(exc)[:200]}"})
    _save_kb(session_id, kb)

    history = []
    satisfaction_trace = []
    step = 0
    consecutive_failures = 0
    cycle_idx = 0  # v13.87 · 当前 cycle 索引 (从 0 起 · 结束时 +1)

    with sync_playwright() as p:
        # v13.88.9L-64 · CDP 断连 fail-safe(690fd5e自标短板「deep模式高load下CDP脆」):
        #   连不上时明确降级日志·不丢五子群已收集的成果(swarm在CDP连接前已写kb)·
        #   page=None 显式初始化(否则 connect 抛异常→finally 里 if page 触发 NameError)。
        page = None
        try:
            b = p.chromium.connect_over_cdp(cdp or CDP_URL)
            ctx = b.contexts[0] if b.contexts else b.new_context()
            page = ctx.new_page()
        except Exception as _cdp_e:
            _log({"type": "cdp_unavailable", "id": session_id,
                  "cdp": (cdp or CDP_URL),
                  "err": f"{type(_cdp_e).__name__}: {str(_cdp_e)[:150]}",
                  "note": "CDP连不上·降级: 跳过浏览器loop·保留五子群/swarm已收集证据结算"})
            max_steps = 0  # 跳过浏览器循环·直接进结算(swarm 成果已在 kb)

        try:
            if page is not None and start_url:
                page.goto(start_url, timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
                from path_monitor import log_path_step
                log_path_step("goto", start_url, True, "start", "deep_research")

                # v13.88.9 · 撞 Google 人工校验检测 (根因: 之前 goto google.com/search
                # 被 302 到 /sorry/index·agent 对着校验页空转 step·tab 永不关堆积 14 个)
                # 检测 page.url 重定向·撞校验则立即改用 SearchAgent 拿候选·不在校验页浪费
                try:
                    _cur = (page.url or "").lower()
                except Exception:
                    _cur = ""
                if "/sorry/" in _cur or "recaptcha" in _cur or "unusual" in _cur:
                    _log({"type": "captcha_hit", "id": session_id,
                          "url": _cur[:120],
                          "note": "goto 撞 Google 人工校验·改走 SearchAgent·关此标签"})
                    log_path_step("goto", start_url, False, "captcha_redirect",
                                  "deep_research")
                    # 抽 query·改用 SearchAgent (服务端搜·不占 CDP·不撞校验)
                    try:
                        import urllib.parse as _up
                        _q = _up.parse_qs(_up.urlparse(start_url).query).get("q", [""])[0]
                        if _q:
                            from search_agent import SearchAgent
                            _sa = SearchAgent(use_cache=True, use_forger=True,
                                              use_supervisor=False)
                            _hits = _sa.search(_q, mode="balanced", top_k=5)
                            for _h in (_hits or [])[:5]:
                                kb["knowledge"].append({
                                    "title": (_h.get("title") or "")[:120],
                                    "key_insights": [(_h.get("content") or "")[:300]]
                                                    if _h.get("content") else [],
                                    "methods": [],
                                    "source": _h.get("url", ""),
                                    "via": "searchagent_captcha_fallback",
                                })
                            _log({"type": "searchagent_fallback", "id": session_id,
                                  "q": _q[:80], "n_hits": len(_hits or [])})
                    except Exception as _e:
                        _log({"type": "searchagent_fallback_err", "id": session_id,
                              "err": str(_e)[:150]})
                    # 校验页无价值·关标签后跳过 CDP 循环·直接进结算
                    try:
                        page.close()
                    except Exception:
                        pass
                    page = None
                    max_steps = 0  # 跳过循环·SearchAgent 已回填 knowledge

            # v13.87 · cycle 层外套: cycle_idx 每达 steps_per_cycle 就压缩+重启
            cycle_step_counter = 0
            while step < max_steps and cycle_idx < max_cycles:
                step += 1
                cycle_step_counter += 1

                # v13.87 · cycle 结束前判定: 已达 steps_per_cycle 或所有 todo done
                # 压缩当前 cycle · 塞进下 cycle 的 meta_lessons
                pending_check = [t for t in kb["todos"] if t.get("status") != "done"]
                if (cycle_step_counter > steps_per_cycle or
                    (not pending_check and satisfaction_trace and
                     satisfaction_trace[-1] < min_satisfaction)):
                    # 只有 max_cycles > 1 且不是最后 cycle 时才压缩
                    if max_cycles > 1 and cycle_idx < max_cycles - 1:
                        _log({"type": "cycle_end", "id": session_id,
                              "cycle": cycle_idx, "step": step,
                              "cycle_steps": cycle_step_counter - 1})
                        compressed = compress_cycle_trajectory(
                            kb, history, cycle_idx)
                        kb["cycle_states"].append(compressed)
                        _log({"type": "cycle_compressed", "id": session_id,
                              "cycle": cycle_idx,
                              "facts_n": len(compressed.get("facts_evidence", [])),
                              "uncertainties_n": len(compressed.get("uncertainties", [])),
                              "failures_n": len(compressed.get("failure_modes", [])),
                              "best_answer": (compressed.get("current_best_answer") or "")[:100]})
                        # 把压缩状态加入 meta_lessons (供 decide/reflect prompt 使用)
                        kb["meta_lessons"].append({
                            "kind": "cycle_summary",
                            "cycle": cycle_idx,
                            "best_answer": compressed.get("current_best_answer"),
                            "uncertainties_top": [
                                u.get("question", "")[:100]
                                for u in compressed.get("uncertainties", [])[:3]
                            ],
                            "avoid": [
                                fm.get("avoid", "")[:80]
                                for fm in compressed.get("failure_modes", [])[:3]
                            ],
                        })
                        _save_kb(session_id, kb)
                        cycle_idx += 1
                        cycle_step_counter = 0
                        # 满意度 trace 清空 · 给下 cycle 新起点
                        satisfaction_trace = []
                        consecutive_failures = 0
                    else:
                        # 已是最后 cycle · 走完剩余 step 即可 · 不压缩
                        pass

                # 1. observe
                snapshot = snapshot_page_v2(page)

                # 2. self_score (先评当前状态)
                score = self_score(kb, history) if step > 1 else {"satisfaction": 20, "exploration_desire": 90}
                satisfaction_trace.append(score.get("satisfaction", 50))
                _log({"type": "score", "id": session_id, "step": step,
                      **score})

                # 达标条件: 所有 todo done 且满意度 >= min
                pending = [t for t in kb["todos"] if t.get("status") != "done"]
                if not pending and score.get("satisfaction", 0) >= min_satisfaction:
                    _log({"type": "goal_reached", "id": session_id, "step": step,
                          "satisfaction": score.get("satisfaction")})
                    break

                # 3. decide
                action = decide_action(kb, snapshot, history,
                                      score.get("satisfaction", 50),
                                      score.get("exploration_desire", 50))
                _log({"type": "decide", "id": session_id, "step": step,
                      "action": action})
                history.append({"step": step, "action": action,
                              "sat": score.get("satisfaction")})

                # 4. act
                if action.get("action") == "done":
                    # 检查是否真的所有 todo done
                    if pending:
                        # 强制不允许提前 done
                        _log({"type": "done_rejected", "id": session_id,
                              "step": step, "pending": len(pending)})
                        # 继续 · 让下一步换到 pending todo
                        continue
                    _exec_action(page, action, snapshot, kb)
                    break

                exec_r = _exec_action(page, action, snapshot, kb)
                _log({"type": "act", "id": session_id, "step": step,
                      "action": action.get("action"),
                      "ok": exec_r.get("ok"),
                      "detail": exec_r.get("detail")})

                # 5. sediment (extract 后立即结构化落 KB)
                if exec_r.get("ok") and exec_r.get("data", {}).get("kind") == "body":
                    sed = sediment_content(
                        raw_content=exec_r["data"].get("text", ""),
                        for_todo_id=action.get("target_todo_id"),
                        goal=goal,
                        source_url=exec_r["data"].get("url", ""),
                    )
                    kb["knowledge"].append(sed)
                    _log({"type": "sediment", "id": session_id, "step": step,
                          "title": sed.get("title", "")[:80],
                          "insights_n": len(sed.get("key_insights", [])),
                          "relevance": sed.get("relevance_score", 0)})
                    # 消费 Judge 未解问题：如果新证据的关键洞察包含未解问题的关键词，
                    # 则将该问题从 unresolved_questions 中移除。
                    _consume_unresolved(kb, sed.get("key_insights", []), session_id, step)
                    _save_kb(session_id, kb)
                elif exec_r.get("ok") and exec_r.get("data", {}).get("kind") == "notes":
                    # 列表也存一条·标记 for_todo
                    kb["knowledge"].append({
                        "kind": "list",
                        "items": exec_r["data"].get("items", []),
                        "url": snapshot.get("url", ""),
                        "for_todo": action.get("target_todo_id"),
                    })
                    _save_kb(session_id, kb)

                # 追踪失败
                if not exec_r.get("ok"):
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0

                # 6. meta_reflect 每 N 步
                if step % reflect_every == 0 or consecutive_failures >= 3:
                    meta = meta_reflect(kb, history, satisfaction_trace)
                    _log({"type": "meta_reflect", "id": session_id, "step": step,
                          **meta})
                    if meta.get("should_switch_mode"):
                        strategy = meta.get("new_strategy", "")
                        kb["meta_lessons"].append({
                            "step": step,
                            "strategy": strategy,
                            "why": meta.get("why", ""),
                            "suggested_action": meta.get("suggested_action", ""),
                        })
                        # 反思必须导致动作变化：优先换统一搜索后端；无 query 时跳转未访问候选。
                        suggested = str(meta.get("suggested_action", "")).lower()
                        if any(token in suggested for token in ("query", "search", "搜", "换词")) or not kb.get("search_candidates"):
                            forced = _exec_action(page, {"action": "switch_query"}, snapshot, kb)
                        else:
                            forced = _exec_action(page, {"action": "goto"}, snapshot, kb)
                        _log({"type": "meta_reflect_executed", "id": session_id,
                              "step": step, "strategy": strategy[:160],
                              "action_result": forced.get("detail", "")})
                        history.append({"step": step, "action": {"action": "meta_strategy_switch", "strategy": strategy},
                                        "sat": score.get("satisfaction"), "result": forced})
                        _save_kb(session_id, kb)
                    consecutive_failures = 0

                # v13.78 · stuck_detector: sat 连续 4 步不涨 → 强制 switch_query
                if len(satisfaction_trace) >= 4:
                    last4 = satisfaction_trace[-4:]
                    if max(last4) - min(last4) < 5:  # 变化 <5 视为卡
                        _log({"type": "stuck_forced_switch_query",
                              "id": session_id, "step": step,
                              "sat_trace": last4})
                        # v13.79 · 卡住·查 skill_bridge 有没有可复用
                        try:
                            from skill_bridge import on_stuck
                            reuse = on_stuck(goal=goal,
                                          current_url=snapshot.get("url", ""))
                            if reuse.get("reusable"):
                                _log({"type": "stuck_reuse_skill_found",
                                      "id": session_id, "step": step,
                                      "skill_path": reuse.get("skill_path"),
                                      "confidence": reuse.get("confidence")})
                                # 目前只 log · 不自动切换 (安全)
                                # 未来: subprocess 调 skill_path 的 replay.py
                        except Exception as _se:
                            # v13.87 · 不 silent (skill_bridge optional 但至少一句)
                            _log({"type": "stuck_reuse_skill_err",
                                  "id": session_id, "step": step,
                                  "err": f"{type(_se).__name__}: {str(_se)[:150]}"})
                        sq = switch_query_llm(
                            goal=kb.get("goal", ""),
                            current_query=kb.get("current_query", ""),
                            tried=kb.get("tried_queries", []),
                        )
                        recommend = sq.get("candidates", [""])[0] if sq.get("candidates") else ""
                        if recommend:
                            _exec_action(page, {"action": "switch_query",
                                              "new_query": recommend},
                                       snapshot, kb)
                            satisfaction_trace.clear()  # reset · 给新 query 机会

        except Exception as e:
            _log({"type": "session_error", "id": session_id, "error": str(e)[:300]})
        finally:
            # v13.88.9 · 无论正常/异常/校验·都关标签·杜绝 tab 泄漏堆积
            # 根因: 原 page.close() 在 try 内·except 无 close·撞 sorry 抛异常就漏 tab
            try:
                if page is not None:
                    page.close()
            except Exception:
                pass

    # 结束前最后评一次
    final_score = self_score(kb, history)
    kb["final_score"] = final_score
    kb["total_steps"] = step
    kb["ended_at"] = datetime.now().isoformat()
    _save_kb(session_id, kb)

    log_path_done("deep_research",
                 f"steps={step} kb={len(kb['knowledge'])} sat={final_score.get('satisfaction')}")

    # audit event
    append_event({
        "agent": "deep_research",
        "verdict": "pass" if final_score.get("satisfaction", 0) >= min_satisfaction else "warn",
        "reason": f"steps={step} sat={final_score.get('satisfaction')} kb={len(kb['knowledge'])}",
        "meta": {"session_id": session_id, "goal": goal[:150],
                "todos_done": sum(1 for t in kb["todos"] if t.get("status") == "done"),
                "todos_total": len(kb["todos"])},
    })

    result = {
        "session_id": session_id,
        "final_satisfaction": final_score.get("satisfaction"),
        "steps": step,
        "kb_size": len(kb["knowledge"]),
        "todos_done": sum(1 for t in kb["todos"] if t.get("status") == "done"),
        "todos_total": len(kb["todos"]),
        "kb_path": str(KB_DIR / f"{session_id}.json"),
        "swarm_task_id": swarm.get("task_id") if swarm else None,
        "swarm_conclusions": len(swarm.get("conclusions", [])) if swarm else 0,
        # 最终交付显式携带 claim 级覆盖率与四态身份图统计；调用方据此展示
        # 已证实、待验证和已排除内容，避免靠追加请求制造未验证噪声。
        "research_quality": build_research_quality_report(kb),
    }

    # v13.88.9 · final report 整合 swarm 结论摘要
    if swarm:
        swarm_conclusions = swarm.get("conclusions", [])
        result["swarm_summary"] = {
            "verified": [c.get("claim", "")[:100] for c in swarm_conclusions if c.get("verdict") == "verified"],
            "partial": [c.get("claim", "")[:100] for c in swarm_conclusions if c.get("verdict") == "partial"],
            "contradicted": [c.get("claim", "")[:100] for c in swarm_conclusions if c.get("verdict") == "contradicted"],
            "unresolved": swarm.get("unresolved_questions", [])[:5],
            "evidence_count": len(swarm.get("evidence", [])),
            "candidate_urls": len(swarm.get("candidate_urls", [])),
        }

    # v13.79 · session 结束 hook · 判是否 forge skill
    try:
        from skill_bridge import on_session_done
        forge_result = on_session_done(session_id, result["kb_path"])
        result["skill_forge"] = forge_result
        _log({"type": "session_forge_hook", "id": session_id,
              "forged": forge_result.get("forged"),
              "skill_name": forge_result.get("skill_name", "")})
    except Exception as e:
        _log({"type": "session_forge_error", "id": session_id, "error": str(e)[:200]})

    return result


if __name__ == "__main__":
    try:
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("cmd", choices=["run", "kb", "log"])
        p.add_argument("--goal", default="")
        p.add_argument("--start-url", default=None)
        p.add_argument("--max-steps", type=int, default=40)
        p.add_argument("--min-sat", type=int, default=85)
        p.add_argument("--session", default=None)
        p.add_argument("--last-n", type=int, default=40)
        args = p.parse_args()
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise

    if args.cmd == "run":
        if not args.goal:
            print("--goal required"); sys.exit(1)
        # v13.88.9 · fire-and-forget 前置闸 · 过载就先冷却再开跑(只拦重活 run · 不拦 kb/log)
        try:
            from perf_guard import wait_until_cool
            wait_until_cool("deep_research_agent")
        except Exception:
            pass
        r = run_deep_research(args.goal, args.start_url,
                             args.max_steps, args.min_sat)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    elif args.cmd == "kb":
        if args.session:
            p = KB_DIR / f"{args.session}.json"
            if p.exists():
                print(p.read_text())
        else:
            for f in sorted(KB_DIR.glob("*.json"))[-5:]:
                print(f)
    elif args.cmd == "log":
        if not RESEARCH_LOG.exists():
            print("no log"); sys.exit(0)
        with open(RESEARCH_LOG) as f:
            for line in list(f)[-args.last_n:]:
                print(line.strip())
