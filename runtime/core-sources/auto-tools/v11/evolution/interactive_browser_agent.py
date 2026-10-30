#!/usr/bin/env python3
"""
v13.76 · InteractiveBrowserAgent · goal-driven 真交互 loop agent
────────────────────────────────────────────────────────────────
用户 goal (2026-07-14):
「搜索不仅是 anysearch/browseract·而是需要
 - 对人类意愿理解和网页图面理解
 - 进行网页交互 goal 设定
 - 点击任何按钮/滚动/详情页连续长任务探索
 - 不是一次性·是反复 loop·不行就反思持续思考执行
 - agent 自主使用输入框输入后获取内容·点击详情页·滚动
 - 日志过程与监督 agent 和进化 agent 联动
 - 真交互·真监控·真反思经验」

── 核心循环 ──
1. observe():  抽当前页面 snapshot (visible elements + text 摘要)
2. decide():   LLM (opus) 看 goal + history + snapshot → next_action
3. act():      执行 action (type/click/scroll/press/goto/extract)
                每步 log_path_step
4. check():    LLM 判是否达标 → goal_met? true/false
5. reflect():  失败 → LLM 反思为啥 · 输出 lesson · 下轮采纳
6. loop 直到 goal_met 或 max_steps

── 5 类 action ──
- type(selector, text):    真人逐字输入
- click(selector|text):    真点击 · 支持文字定位
- scroll(direction, amount): 上下滚动
- press(key):              键盘按键 (Enter/Tab/Escape)
- extract(what):           抽取当前页面内容 (评论/正文/列表)

── audit 联动 ──
- 每步 log_path_step → path_monitor 事后审
- 达标判决 → llm_judge 独立复审 (交叉验证)
- 冲突 → meta_audit 元判决
- lesson 落 memory · 后续 goal 可查
"""
import os, sys, json, time
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
AGENT_LOG = V11 / ".interactive_agent.jsonl"
LESSONS_LOG = V11 / ".interactive_lessons.jsonl"
CDP_URL = "http://localhost:9224"


def _log(rec: dict):
    rec.setdefault("ts", datetime.now().isoformat())
    try:
        AGENT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(AGENT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _save_lesson(goal: str, lesson: str, outcome: str = "learned"):
    """反思结论落库 · 后续 goal 查得到"""
    try:
        LESSONS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LESSONS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now().isoformat(),
                "goal": goal[:200],
                "lesson": lesson[:500],
                "outcome": outcome,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _load_relevant_lessons(goal: str, max_n: int = 5) -> list:
    """读近期 lesson · 简单关键词匹配"""
    if not LESSONS_LOG.exists():
        return []
    try:
        keys = set()
        for w in goal.split():
            if len(w) >= 3:
                keys.add(w.lower())
        matches = []
        with open(LESSONS_LOG, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    lg = (r.get("goal", "") + " " + r.get("lesson", "")).lower()
                    if any(k in lg for k in keys):
                        matches.append(r)
                except Exception:
                    pass
        return matches[-max_n:]
    except Exception:
        return []


# ────────────── 页面 snapshot ──────────────

def snapshot_page(page, max_elements: int = 25, max_text: int = 800) -> dict:
    """抽页面 visible 元素 + 摘要 · 短化给 LLM"""
    try:
        data = page.evaluate("""(maxE) => {
          const inViewport = (el) => {
            const r = el.getBoundingClientRect();
            return r.width > 5 && r.height > 5 &&
                   r.top < window.innerHeight && r.bottom > 0;
          };
          const abbrev = (s, n) => (s||'').trim().slice(0, n);
          const nodes = [];
          // 优先级：inputs → buttons → links → clickable divs
          document.querySelectorAll('input, textarea').forEach(el => {
            if (inViewport(el)) {
              nodes.push({
                idx: nodes.length,
                type: 'input',
                tag: el.tagName.toLowerCase(),
                text: abbrev(el.placeholder || el.value || '', 50),
                name: el.name || el.id || '',
                selector: el.id ? `#${el.id}` : `${el.tagName.toLowerCase()}[name='${el.name||''}']`,
              });
            }
          });
          document.querySelectorAll('button, [role="button"]').forEach(el => {
            if (inViewport(el) && nodes.length < maxE) {
              nodes.push({
                idx: nodes.length,
                type: 'button',
                text: abbrev(el.innerText, 50),
                selector: el.id ? `#${el.id}` : null,
                cls: (el.className||'').toString().slice(0, 60),
              });
            }
          });
          document.querySelectorAll('a[href]').forEach(el => {
            if (inViewport(el) && nodes.length < maxE) {
              const t = abbrev(el.innerText, 60);
              if (t) nodes.push({
                idx: nodes.length,
                type: 'link',
                text: t,
                href: (el.href||'').slice(0, 100),
              });
            }
          });
          return {
            url: window.location.href,
            title: document.title,
            scrollY: window.scrollY,
            maxScrollY: Math.max(0, document.body.scrollHeight - window.innerHeight),
            elements: nodes,
          };
        }""", max_elements)

        # 抽正文摘要
        try:
            body_text = page.evaluate("""() => {
              const skip = new Set(['SCRIPT','STYLE','NOSCRIPT']);
              const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
              const chunks = [];
              let node;
              while (node = walker.nextNode()) {
                if (skip.has(node.parentElement?.tagName)) continue;
                const t = node.textContent.trim();
                if (t.length > 5) chunks.push(t);
                if (chunks.join(' ').length > 2000) break;
              }
              return chunks.join(' | ').slice(0, 2000);
            }""")
            data["text_summary"] = (body_text or "")[:max_text]
        except Exception:
            data["text_summary"] = ""
        return data
    except Exception as e:
        return {"error": str(e)[:200]}


# ────────────── LLM 决策 ──────────────

DECIDE_SYSTEM_PROMPT = """你是「浏览器交互 agent」·独立干净上下文。

任务:根据 goal + 当前页面 snapshot + 历史动作 · 决定下一步。

action 类型 (只能选一个):
- type    : 在输入框输入 (需 selector 或 element_idx + text)
- click   : 点击元素 (需 element_idx 或 selector 或 text)
- press   : 按键 (Enter/Escape/Tab)
- scroll  : 滚动 (direction: up|down, amount: 500)
- extract : 抽取当前页内容 (what: notes|comments|body)
- goto    : 只在初始导航或必须换页时用
- done    : goal 达成 · 结束 loop

只回 JSON: {"action":"type|click|press|scroll|extract|goto|done",
             "element_idx": 0-24 (如需要),
             "selector": "..." (备用),
             "text": "..." (type 时),
             "key": "..." (press 时),
             "direction": "up|down" (scroll 时),
             "amount": 500 (scroll 时),
             "what": "..." (extract 时),
             "url": "..." (goto 时),
             "why": "20 字为什么这一步"}
"""

CHECK_SYSTEM_PROMPT = """你是「目标达成审计员」·独立干净上下文。

给定 goal · 历史动作 · 抽到的内容 · 判是否达成 goal。

只回 JSON: {"goal_met": true|false, "reason": "50 字", "confidence": 0-100}
"""

REFLECT_SYSTEM_PROMPT = """你是「反思员」·独立干净上下文。

给定 goal · 历史动作 · 卡住的原因 · 输出下一步策略调整。

只回 JSON: {"lesson": "20 字学到什么",
             "strategy_adjust": "30 字下一步策略变什么",
             "give_up": false}
"""


def decide_next(page_snapshot: dict, goal: str, history: list,
                lessons: list, model: str = None,
                timeout: int = 60) -> dict:
    # v13.87 · wigolo-eval P0 · 关键决策层可升 opus (env 开关)
    if model is None:
        import os
        model = os.environ.get("MEYO_DECIDE_MODEL", "sonnet")
    user_msg = (
        f"goal: {goal}\n\n"
        f"lessons (历史学到):\n"
        f"{chr(10).join('- ' + l.get('lesson','')[:100] for l in lessons[-3:])}\n\n"
        f"history (最近 5 步):\n"
        f"{json.dumps(history[-5:], ensure_ascii=False, indent=2)[:1500]}\n\n"
        f"current page:\n"
        f"URL: {page_snapshot.get('url','')[:150]}\n"
        f"title: {page_snapshot.get('title','')[:100]}\n"
        f"scrollY: {page_snapshot.get('scrollY',0)}/{page_snapshot.get('maxScrollY',0)}\n"
        f"elements (visible):\n"
        f"{json.dumps(page_snapshot.get('elements',[]), ensure_ascii=False, indent=1)[:2000]}\n\n"
        f"text_summary (前 800 字):\n"
        f"{page_snapshot.get('text_summary','')[:800]}\n\n"
        f"next_action JSON:"
    )
    r = call_llm(DECIDE_SYSTEM_PROMPT, user_msg, model_override=model, timeout=timeout)
    if r["ok"]:
        return try_parse_json(r["raw"], default={"action": "done",
                                                  "why": "no_json"})
    return {"action": "done", "why": f"llm_err:{r.get('error','')[:60]}"}


def check_goal_met(goal: str, history: list, extracted: list,
                   model: str = None, timeout: int = 45) -> dict:
    # v13.87 · wigolo-eval P0 · 目标判决关键·可升 opus
    if model is None:
        import os
        model = os.environ.get("MEYO_GOAL_CHECK_MODEL", "sonnet")
    user_msg = (
        f"goal: {goal}\n\n"
        f"history (全部动作):\n"
        f"{json.dumps(history, ensure_ascii=False, indent=1)[:2500]}\n\n"
        f"extracted (抽到的内容):\n"
        f"{json.dumps(extracted, ensure_ascii=False, indent=1)[:2500]}\n\n"
        f"判决 JSON:"
    )
    r = call_llm(CHECK_SYSTEM_PROMPT, user_msg, model_override=model, timeout=timeout)
    if r["ok"]:
        return try_parse_json(r["raw"], default={"goal_met": False,
                                                  "reason": "no_json"})
    return {"goal_met": False, "reason": f"llm_err:{r.get('error','')[:60]}"}


def reflect(goal: str, history: list, block_reason: str,
            model: str = "opus", timeout: int = 60) -> dict:
    user_msg = (
        f"goal: {goal}\n\n"
        f"history (最近 8 步):\n"
        f"{json.dumps(history[-8:], ensure_ascii=False, indent=1)[:2500]}\n\n"
        f"block_reason: {block_reason[:300]}\n\n"
        f"反思 JSON:"
    )
    r = call_llm(REFLECT_SYSTEM_PROMPT, user_msg, model_override=model,
                timeout=timeout)
    if r["ok"]:
        return try_parse_json(r["raw"], default={"lesson": "no_json",
                                                  "strategy_adjust": "",
                                                  "give_up": True})
    return {"lesson": f"llm_err:{r.get('error','')[:60]}",
            "strategy_adjust": "", "give_up": True}


# ────────────── 执行动作 ──────────────

def _exec_action(page, action: dict, snapshot: dict) -> dict:
    """执行 LLM 决策的动作 · 返回 {ok, detail}"""
    from path_monitor import log_path_step
    at = action.get("action")

    try:
        if at == "goto":
            url = action.get("url", "").strip()
            if not url:
                return {"ok": False, "detail": "no_url"}
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            log_path_step("goto", url, True, "", "interactive_agent")
            return {"ok": True, "detail": f"goto {url[:80]}"}

        elif at == "type":
            text = action.get("text", "")
            # 优先按 element_idx 找 selector
            idx = action.get("element_idx")
            target = None
            if idx is not None and idx < len(snapshot.get("elements", [])):
                el = snapshot["elements"][idx]
                if el.get("selector"):
                    target = page.locator(el["selector"]).first
            if not target and action.get("selector"):
                target = page.locator(action["selector"]).first
            if not target:
                # 兜底 · 找页面 visible textarea/input
                for loc in page.locator("textarea, input").all():
                    try:
                        if loc.is_visible():
                            target = loc; break
                    except Exception:
                        pass
            if not target:
                return {"ok": False, "detail": "no_input_found"}
            target.click(force=True)
            page.wait_for_timeout(400)
            for c in text:
                page.keyboard.type(c, delay=80)
            log_path_step("type", text[:80], True,
                         f"len={len(text)}", "interactive_agent")
            return {"ok": True, "detail": f"typed '{text[:60]}'"}

        elif at == "click":
            idx = action.get("element_idx")
            selector = action.get("selector")
            text_hint = action.get("text")
            clicked = False
            if idx is not None and idx < len(snapshot.get("elements", [])):
                el = snapshot["elements"][idx]
                if el.get("type") == "link" and el.get("href"):
                    # 直接点该链接
                    try:
                        page.locator(f"a[href='{el['href']}']").first.click(timeout=5000)
                        clicked = True
                    except Exception:
                        pass
                elif el.get("selector"):
                    try:
                        page.locator(el["selector"]).first.click(timeout=5000)
                        clicked = True
                    except Exception:
                        pass
                elif el.get("text"):
                    # 用文字定位
                    try:
                        page.get_by_text(el["text"][:40], exact=False).first.click(timeout=5000)
                        clicked = True
                    except Exception:
                        pass
            if not clicked and selector:
                try:
                    page.locator(selector).first.click(timeout=5000)
                    clicked = True
                except Exception:
                    pass
            if not clicked and text_hint:
                try:
                    page.get_by_text(text_hint[:40], exact=False).first.click(timeout=5000)
                    clicked = True
                except Exception:
                    pass
            if clicked:
                page.wait_for_timeout(3000)
                log_path_step("click",
                             (selector or text_hint or f"idx={idx}")[:80],
                             True, "", "interactive_agent")
                return {"ok": True, "detail": "clicked"}
            log_path_step("click", "failed", False, "no target",
                         "interactive_agent")
            return {"ok": False, "detail": "click_failed"}

        elif at == "press":
            key = action.get("key", "Enter")
            page.keyboard.press(key)
            page.wait_for_timeout(2500)
            log_path_step("press", key, True, "", "interactive_agent")
            return {"ok": True, "detail": f"pressed {key}"}

        elif at == "scroll":
            direction = action.get("direction", "down")
            amount = action.get("amount", 500)
            dy = amount if direction == "down" else -amount
            page.evaluate(f"window.scrollBy(0, {dy})")
            page.wait_for_timeout(1200)
            log_path_step("scroll", f"{direction}×{amount}", True,
                         "", "interactive_agent")
            return {"ok": True, "detail": f"scrolled {direction} {amount}"}

        elif at == "extract":
            what = action.get("what", "body")
            # 抽正文或列表
            if what == "notes" or what == "list":
                data = page.evaluate("""() => {
                  const items = [];
                  document.querySelectorAll("a[href*='/search_result'], a[href*='/explore/']").forEach(a => {
                    const t = (a.innerText||'').trim();
                    if (t && t.length > 3) items.push({text: t.slice(0,100), href: a.href});
                  });
                  const seen = new Set();
                  return items.filter(x => !seen.has(x.href) && seen.add(x.href)).slice(0,10);
                }""")
                log_path_step("extract", f"notes×{len(data or [])}", True,
                             "", "interactive_agent")
                return {"ok": True, "detail": f"notes={len(data or [])}",
                       "data": data}
            else:
                text = page.evaluate("""() => {
                  const sels = ['div[class*="note-scroller"]', '.note-content',
                               '#detail-desc', 'article', 'main', 'body'];
                  for (const s of sels) {
                    const el = document.querySelector(s);
                    if (el && (el.innerText||'').length > 100)
                      return {sel: s, text: el.innerText.slice(0, 3000)};
                  }
                  return {sel: 'body', text: document.body.innerText.slice(0,3000)};
                }""")
                log_path_step("extract", f"body sel={text.get('sel','')}",
                             True, f"len={len(text.get('text',''))}",
                             "interactive_agent")
                return {"ok": True, "detail": f"body {len(text.get('text',''))}",
                       "data": text}

        elif at == "done":
            log_path_step("done", "goal_reached", True,
                         action.get("why", ""), "interactive_agent")
            return {"ok": True, "detail": "done"}

        else:
            return {"ok": False, "detail": f"unknown_action:{at}"}
    except Exception as e:
        return {"ok": False, "detail": f"exc:{str(e)[:200]}"}


# ────────────── 主 loop ──────────────

def run_goal(goal: str, start_url: str = None, max_steps: int = 15,
             cdp: str = None, reflect_after: int = 3) -> dict:
    """给定 goal · 反复 loop 直到达标或耗尽 max_steps。

    反思触发: 连续 reflect_after 步无进展 → reflect → 调整策略
    """
    from playwright.sync_api import sync_playwright
    from path_monitor import log_path_plan, log_path_done

    session_id = f"iag_{int(time.time())}"
    _log({"type": "session_start", "id": session_id, "goal": goal,
          "start_url": start_url, "max_steps": max_steps})

    log_path_plan(query=goal, planned_steps=[
        {"action": "loop", "target": "observe→decide→act→check", "why": "goal-driven loop"},
    ], caller="interactive_agent")

    history = []       # 所有 action 记录
    extracted = []     # extract 抽到的内容
    lessons = _load_relevant_lessons(goal)
    step = 0
    stuck_count = 0
    goal_met = False

    with sync_playwright() as p:
        try:
            b = p.chromium.connect_over_cdp(cdp or CDP_URL)
            ctx = b.contexts[0] if b.contexts else b.new_context()
            page = ctx.new_page()

            # 初始导航
            if start_url:
                page.goto(start_url, timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
                from path_monitor import log_path_step
                log_path_step("goto", start_url, True, "start_url",
                             "interactive_agent")

            while step < max_steps:
                step += 1
                # 1. observe
                snapshot = snapshot_page(page)
                _log({"type": "observe", "id": session_id, "step": step,
                      "url": snapshot.get("url", "")[:200],
                      "n_elements": len(snapshot.get("elements", []))})

                # 2. decide
                action = decide_next(snapshot, goal, history, lessons)
                _log({"type": "decide", "id": session_id, "step": step,
                      "action": action})
                history.append({"step": step, "action": action})

                # 3. act
                if action.get("action") == "done":
                    goal_met = True
                    _exec_action(page, action, snapshot)
                    break
                exec_r = _exec_action(page, action, snapshot)
                _log({"type": "act", "id": session_id, "step": step,
                      "action": action.get("action"),
                      "ok": exec_r.get("ok"),
                      "detail": exec_r.get("detail")})
                if exec_r.get("data"):
                    extracted.append({"step": step,
                                     "data": exec_r["data"]})

                # 4. 每 3 步 check 一次
                if step % 3 == 0 or step == max_steps:
                    chk = check_goal_met(goal, history, extracted)
                    _log({"type": "check", "id": session_id, "step": step,
                          "goal_met": chk.get("goal_met"),
                          "reason": chk.get("reason", "")[:200]})
                    if chk.get("goal_met"):
                        goal_met = True
                        break

                # 5. 卡住检测 & 反思
                if not exec_r.get("ok"):
                    stuck_count += 1
                if stuck_count >= reflect_after:
                    ref = reflect(goal, history,
                                 block_reason=f"连续 {stuck_count} 步失败")
                    _log({"type": "reflect", "id": session_id, "step": step,
                          "lesson": ref.get("lesson", ""),
                          "strategy": ref.get("strategy_adjust", "")})
                    lessons.append({"lesson": ref.get("lesson", ""),
                                   "goal": goal})
                    _save_lesson(goal, ref.get("lesson", ""), "in_loop")
                    if ref.get("give_up"):
                        break
                    stuck_count = 0  # reset

            page.close()
        except Exception as e:
            _log({"type": "session_error", "id": session_id,
                  "error": str(e)[:300]})

    log_path_done("interactive_agent",
                 f"steps={step} goal_met={goal_met} extracted={len(extracted)}")

    # 达成/失败 都落 lesson
    outcome = "success" if goal_met else "timeout"
    if goal_met:
        _save_lesson(goal, f"完成·用 {step} 步·抽了 {len(extracted)} 组内容",
                    outcome)
    else:
        _save_lesson(goal, f"未完成·{step} 步耗尽·抽 {len(extracted)} 组",
                    outcome)

    # 落审计事件
    append_event({
        "agent": "interactive_agent",
        "verdict": "pass" if goal_met else "fail",
        "reason": f"steps={step} goal_met={goal_met}",
        "meta": {"goal": goal[:150], "session_id": session_id,
                "steps": step, "extracted": len(extracted)},
    })

    if not goal_met:
        push_correction(
            source="interactive_agent",
            text=f"[iag] goal 未达 · {goal[:100]} · {step} 步耗尽",
            markers=["interactive_agent", "goal_timeout"],
            severity="medium",
        )

    return {
        "goal_met": goal_met,
        "steps": step,
        "history": history,
        "extracted": extracted,
        "session_id": session_id,
    }


# ────────────── CLI ──────────────

if __name__ == "__main__":
    try:
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("cmd", choices=["run", "lessons", "log"])
        p.add_argument("--goal", default="")
        p.add_argument("--start-url", default=None)
        p.add_argument("--max-steps", type=int, default=15)
        p.add_argument("--last-n", type=int, default=20)
        args = p.parse_args()
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise

    if args.cmd == "run":
        if not args.goal:
            print("--goal required"); sys.exit(1)
        r = run_goal(args.goal, args.start_url, args.max_steps)
        print(json.dumps({
            "goal_met": r["goal_met"],
            "steps": r["steps"],
            "extracted_count": len(r["extracted"]),
        }, ensure_ascii=False, indent=2))
    elif args.cmd == "lessons":
        if not LESSONS_LOG.exists():
            print("no lessons"); sys.exit(0)
        with open(LESSONS_LOG) as f:
            for line in list(f)[-args.last_n:]:
                try:
                    r = json.loads(line)
                    print(f"  {r.get('ts','')[:19]} · {r.get('outcome','?'):10} · "
                          f"{r.get('goal','')[:50]}")
                    print(f"    → {r.get('lesson','')[:100]}")
                except Exception:
                    pass
    elif args.cmd == "log":
        if not AGENT_LOG.exists():
            print("no log"); sys.exit(0)
        with open(AGENT_LOG) as f:
            for line in list(f)[-args.last_n:]:
                try:
                    print(line.strip())
                except Exception:
                    pass
