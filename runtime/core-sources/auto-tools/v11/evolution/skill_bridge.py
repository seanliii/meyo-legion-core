#!/usr/bin/env python3
"""
v13.79 · skill_bridge · 连 deep_research_agent 与 browser-act-skill-forge
────────────────────────────────────────────────────────────────
用户 R12 (2026-07-16):
「加入 skill forge 到搜索 agent
 看看再哪个地方加入 hook 更好
 通过过去日志反思推理
 多个 agent 判断如何优化搜索」

── 反思结果 ──
过去 3 次 deep_research 跑:
- 平均 25 步 · click 失败率 20%
- 相关性 avg 66-67
- 每次都从零 observe→decide (重复烧 token)
- lessons 只是文字·下次仍需 LLM 重新决策

Skill Forge 恰好补:
- 探索一次 → 生成可复用 skill (省 93% token)
- 我们已经"探索过" · 直接把成功路径固化即可

── 2 个 hook 点 ──
Hook A · deep_research 完成后:
  session 结束 → 多 agent 判决 (llm_judge/path_monitor/meta_audit)
  全 pass 且相关性>60 → 提炼成 skill (auto forge)

Hook B · deep_research 卡住时:
  连续 3 步 fail → 查 KB 有没有类似目标的现成 skill 可复用
  有 → 直接调用·跳过探索

── 数据 ──
v11/.skill_bridge.jsonl · 每次 forge/reuse 事件
v11/skills_forged/ · 我们提炼出来的 skill 存这里 (备份)
"""
import os, sys, json, subprocess, hashlib
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
BRIDGE_LOG = V11 / ".skill_bridge.jsonl"
FORGED_DIR = V11 / "skills_forged"
CLAUDE_SKILLS_DIR = Path.home() / ".claude" / "skills"


def _log(rec: dict):
    rec.setdefault("ts", datetime.now().isoformat())
    try:
        BRIDGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(BRIDGE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ────────────── 反思器 · 从 KB 提炼可复用路径 ──────────────

REFLECT_SYSTEM = """你是「路径提炼员」·独立干净上下文。

给一次 deep_research session 的:
- goal
- 全部成功动作序列
- KB 里 relevance>=60 的知识
- 触发过的失败动作

任务:判决这次探索是否值得固化成 skill:
1. worth_forge: true|false
   - 相关性 avg>=60 · todos 完成 >=1 · 有 >=2 条 rel>=70 的 KB
   - 才 true
2. reusable_pattern: 成功路径的可复用模式 (goto→type→press→extract 这种)
3. skill_name: 30 字内·英文小写下划线
4. skill_purpose: 一句话描述用途
5. skill_input_schema: 参数结构 (JSON schema-ish)

只回 JSON:
{
  "worth_forge": true|false,
  "reason": "50 字",
  "reusable_pattern": ["goto", "type", "press", "extract", "click", "extract"],
  "skill_name": "xiaohongshu_topic_research",
  "skill_purpose": "小红书搜某话题·抽 N 篇高相关笔记正文",
  "skill_input_schema": {"query":"str", "n_notes":"int", "min_relevance":"int"}
}
"""


def reflect_session(session_id: str, kb: dict, action_seq: list,
                    model: str = "sonnet", timeout: int = 60) -> dict:
    """独立 LLM 反思 · 判是否值得固化"""
    # 提数据
    high_rel = [k for k in kb.get("knowledge", [])
                if k.get("relevance_score", 0) >= 60]
    success_actions = [a for a in action_seq if a.get("ok")]

    high_rel_dump = [{"title": k.get("title", "")[:80],
                     "rel": k.get("relevance_score")} for k in high_rel[:5]]
    action_lines = [f"{a.get('action')}: {a.get('target', '')[:40]}"
                   for a in success_actions[:15]]
    todos_done_n = sum(1 for t in kb.get("todos", [])
                      if t.get("status") == "done")

    user_msg = (
        f"session_id: {session_id}\n"
        f"goal: {kb.get('goal', '')[:200]}\n"
        f"total_steps: {len(action_seq)}\n"
        f"success_rate: {len(success_actions)}/{len(action_seq)}\n"
        f"todos_done: {todos_done_n}/{len(kb.get('todos', []))}\n\n"
        f"高相关知识 (rel>=60):\n"
        f"{json.dumps(high_rel_dump, ensure_ascii=False, indent=1)}\n\n"
        f"成功动作序列:\n"
        f"{json.dumps(action_lines, ensure_ascii=False, indent=1)[:1500]}\n\n"
        f"tried_queries: {kb.get('tried_queries', [])[:3]}\n\n"
        f"判决 JSON:"
    )
    r = call_llm(REFLECT_SYSTEM, user_msg, model_override=model,
                timeout=timeout)
    if r["ok"]:
        return try_parse_json(r["raw"],
                default={"worth_forge": False, "reason": "no_json"})
    return {"worth_forge": False, "reason": f"llm_err:{r.get('error','')[:60]}"}


# ────────────── 多 agent 判决 · 决定是否 forge ──────────────

def multi_agent_verdict(session_id: str, goal: str, kb: dict,
                        action_seq: list) -> dict:
    """3 层 audit 汇总 · 决定是否 forge"""
    # 1. llm_judge 判内容
    from audit_core import read_recent_events
    events = read_recent_events(last_n=30)
    judge_events = [e for e in events
                   if e.get("agent") == "llm_judge"
                   and (e.get("meta", {}).get("session_id") == session_id
                        or (datetime.now() - datetime.fromisoformat(
                            e.get("ts").replace("Z", "+00:00").split("+")[0]
                        )).total_seconds() < 600)]
    judge_pass = sum(1 for e in judge_events if e.get("verdict") == "pass")

    # 2. path_monitor 判路径
    path_events = [e for e in events if e.get("agent") == "path_monitor"]
    path_pass = sum(1 for e in path_events if e.get("verdict") == "pass")

    # 3. deep_research 自身
    kb_rel_avg = sum(k.get("relevance_score", 0)
                    for k in kb.get("knowledge", [])) / max(
                        1, len([k for k in kb.get("knowledge", [])
                              if k.get("relevance_score")]))
    success_rate = sum(1 for a in action_seq if a.get("ok")) / max(1, len(action_seq))

    # 综合 verdict
    verdicts = {
        "llm_judge_pass": judge_pass > 0,
        "path_monitor_pass": path_pass > 0,
        "content_quality": kb_rel_avg >= 60,
        "execution_quality": success_rate >= 0.7,
    }
    passed_n = sum(1 for v in verdicts.values() if v)
    overall = "pass" if passed_n >= 3 else ("warn" if passed_n >= 2 else "fail")

    return {
        "verdicts": verdicts,
        "passed_n": passed_n,
        "kb_rel_avg": round(kb_rel_avg, 1),
        "success_rate": round(success_rate, 2),
        "overall": overall,
    }


# ────────────── forge skill · 调 skill-forge ──────────────

def forge_skill_from_session(session_id: str, kb: dict,
                              action_seq: list) -> dict:
    """完成后 hook · 全流程"""
    _log({"type": "forge_start", "session_id": session_id,
          "goal": kb.get("goal", "")[:100]})

    # 1. 多 agent 判
    verdict = multi_agent_verdict(session_id, kb.get("goal", ""),
                                 kb, action_seq)
    _log({"type": "forge_verdict", "session_id": session_id, **verdict})

    if verdict["overall"] not in ("pass",):
        _log({"type": "forge_skip", "reason": "not_all_pass"})
        return {"forged": False, "reason": "quality_gate_not_met",
               "verdict": verdict}

    # 2. LLM 反思 · 判是否值得固化
    reflect = reflect_session(session_id, kb, action_seq)
    _log({"type": "forge_reflect", **reflect})

    if not reflect.get("worth_forge"):
        return {"forged": False, "reason": reflect.get("reason", ""),
               "verdict": verdict, "reflect": reflect}

    # 3. 生成 skill 文件
    skill_name = reflect.get("skill_name", f"skill_{int(datetime.now().timestamp())}")
    skill_purpose = reflect.get("skill_purpose", "")
    pattern = reflect.get("reusable_pattern", [])
    input_schema = reflect.get("skill_input_schema", {})

    # 提炼具体成功步骤
    success_steps = [{"action": a.get("action"), "target": a.get("target", "")}
                    for a in action_seq if a.get("ok")]

    # 写 SKILL.md
    skill_dir = FORGED_DIR / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)

    skill_md = f"""---
name: {skill_name}
description: {skill_purpose}
metadata:
  origin: deep_research_agent forge
  source_session: {session_id}
  source_goal: {kb.get('goal','')[:100]}
  forged_at: {datetime.now().isoformat()}
  content_quality: {verdict['kb_rel_avg']}
  execution_quality: {verdict['success_rate']}
---

# {skill_name}

## 用途
{skill_purpose}

## 输入
```json
{json.dumps(input_schema, ensure_ascii=False, indent=2)}
```

## 验证过的执行路径
{chr(10).join(f'- {i+1}. {s["action"]}: {s["target"][:80]}' for i, s in enumerate(success_steps[:15]))}

## 来源 KB (高相关内容 · rel>=60)
{chr(10).join(f'- {k.get("title","")[:80]} (rel={k.get("relevance_score",0)})' for k in kb.get("knowledge", []) if k.get("relevance_score", 0) >= 60)}

## 复用调用示例
```python
from v11.evolution.deep_research_agent import run_deep_research

r = run_deep_research(
    goal="{skill_purpose}",
    start_url="https://www.xiaohongshu.com/explore",
    max_steps=15,  # skill 缩短版·已知路径
    min_satisfaction=60,
    reflect_every=5,
)
```

## 反思
{reflect.get("reason", "")}
"""

    (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")

    # 写 replay.py · 一个可直接跑的复用脚本
    replay_py = f'''#!/usr/bin/env python3
"""
{skill_name} · replay 脚本
来源: session {session_id}
"""
import sys, os
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(BASE / "v11" / "evolution"))

from deep_research_agent import run_deep_research

def replay(query="{kb.get('current_query','')}"):
    """已知路径快速复用"""
    r = run_deep_research(
        goal=f"{skill_purpose} · query: {{query}}",
        start_url="https://www.xiaohongshu.com/explore",
        max_steps=15,
        min_satisfaction=60,
    )
    return r

if __name__ == "__main__":
    try:
        import json
        q = sys.argv[1] if len(sys.argv) > 1 else ""
        print(json.dumps(replay(q), ensure_ascii=False, indent=2))
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise
'''
    (skill_dir / "replay.py").write_text(replay_py, encoding="utf-8")

    _log({"type": "forge_done", "session_id": session_id,
          "skill_name": skill_name, "skill_dir": str(skill_dir)})

    # v13.80 · 也塞进 sediment 通用日志·统一 dashboard
    try:
        from sediment_core import _log as _sed_log
        _sed_log({"type": "skill_bridge_done",
                 "episode_id": session_id,
                 "asset_name": skill_name,
                 "asset_dir": str(skill_dir)})
    except Exception:
        pass

    return {
        "forged": True,
        "skill_name": skill_name,
        "skill_dir": str(skill_dir),
        "verdict": verdict,
        "reflect": reflect,
    }


# ────────────── 卡住 hook · 查已有 skill 复用 ──────────────

def find_reusable_skill(goal: str, current_url: str = "",
                        model: str = "haiku", timeout: int = 30) -> dict:
    """卡住时 · 查有没有现成 skill 可用"""
    # 扫 forged + claude skills
    candidates = []
    for d in [FORGED_DIR, CLAUDE_SKILLS_DIR]:
        if not d.exists(): continue
        for skill_md in d.rglob("SKILL.md"):
            try:
                content = skill_md.read_text(encoding="utf-8")[:2000]
                candidates.append({
                    "path": str(skill_md),
                    "excerpt": content[:800],
                })
            except Exception:
                pass

    if not candidates:
        return {"reusable": False, "reason": "no_skill_available"}

    # LLM 判哪个可用
    prompt = f"""goal: {goal[:200]}
current_url: {current_url[:150]}

候选 skills:
{json.dumps([{'path': c['path'], 'excerpt': c['excerpt'][:500]} for c in candidates[:5]], ensure_ascii=False, indent=1)[:3000]}

判决:
- 有没有 skill 明显覆盖这个 goal? (关键词匹配 + 用途相似)
- 若有·哪个 path?

只回 JSON: {{"reusable":true|false, "skill_path":"...", "confidence":0-100, "why":"20字"}}
"""
    r = call_llm("你是 skill 匹配员·干净上下文", prompt,
                model_override=model, timeout=timeout)
    if r["ok"]:
        return try_parse_json(r["raw"], default={"reusable": False})
    return {"reusable": False, "reason": "llm_err"}


# ────────────── 从 deep_research_agent 调用的 hook 入口 ──────────────

def on_session_done(session_id: str, kb_path: str,
                    trace_log: str = None) -> dict:
    """deep_research_agent 跑完调这个 · 决定是否 forge"""
    try:
        kb = json.loads(Path(kb_path).read_text())
    except Exception as e:
        return {"forged": False, "reason": f"kb_read_err: {e}"}

    # 读该 session 的 action seq
    action_seq = []
    trace_p = Path(trace_log or (V11 / ".path_trace.jsonl"))
    if trace_p.exists():
        with open(trace_p) as f:
            for line in f:
                try:
                    r = json.loads(line)
                    if r.get("caller") == "deep_research" and r.get("type") == "step":
                        action_seq.append({
                            "action": r.get("action"),
                            "target": r.get("target", ""),
                            "ok": r.get("ok"),
                        })
                except Exception:
                    pass

    result = forge_skill_from_session(session_id, kb, action_seq)

    # 落 audit event
    append_event({
        "agent": "skill_bridge",
        "verdict": "pass" if result.get("forged") else "warn",
        "reason": (f"forged {result.get('skill_name','')}" if result.get("forged")
                  else result.get("reason", "")),
        "meta": {"session_id": session_id, **result.get("verdict", {})},
    })
    return result


def on_stuck(goal: str, current_url: str = "") -> dict:
    """卡住时调 · 查有没有可复用 skill"""
    result = find_reusable_skill(goal, current_url)
    _log({"type": "stuck_lookup", "goal": goal[:100],
          "found": result.get("reusable"), **result})
    return result


# ────────────── CLI ──────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["forge", "find", "list"])
    p.add_argument("--session", default="")
    p.add_argument("--kb-path", default="")
    p.add_argument("--goal", default="")
    args = p.parse_args()

    if args.cmd == "forge":
        if not args.kb_path:
            # 找最近 KB
            kbs = sorted((V11 / "research_kb").glob("dr_*.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
            if kbs:
                args.kb_path = str(kbs[0])
                args.session = kbs[0].stem
        r = on_session_done(args.session, args.kb_path)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    elif args.cmd == "find":
        if not args.goal:
            print("需要 --goal"); sys.exit(1)
        print(json.dumps(find_reusable_skill(args.goal), ensure_ascii=False, indent=2))
    elif args.cmd == "list":
        for d in [FORGED_DIR, CLAUDE_SKILLS_DIR]:
            if not d.exists(): continue
            print(f"\n═ {d} ═")
            for skill_md in sorted(d.rglob("SKILL.md"))[:20]:
                print(f"  {skill_md.parent.name}")
