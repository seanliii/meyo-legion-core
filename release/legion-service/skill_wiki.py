#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""skill_wiki · WikiSkill 三层知识架构引擎(每用户隔离)

依据 Google Research 论文 WikiSkill(arXiv:2608.27454v1·Tang/Rashtchian/Tomkins/Vu 等·
2026-08-27·CC BY 4.0·走用户代理 curl 真抓原文,见 dev/wikiskill-design-2026-08-31.md)。

论文核心(一手·非编造):把「原始执行经验 / 累积知识 / 可执行技能」三层分开,
持续把经验 consolidate 进持久 wiki,后续 skill 更新在此之上构建——
经验先沉淀、再复用,而不是分析完轨迹直接改 skill 用一次就丢(EvoSkill/Trace2Skill/SkillOpt 的做法)。

三层(§3.1 原文):
  Raw Layer   = 不可变执行轨迹  → 军团现有 sessions(只读引用·不复制)
  Wiki Layer  = 结构化持久知识  → 本模块新建(index/log/skill-impact/patterns)
  Skill Layer = 演化中程序化知识 → 提炼产物(交 evolve_config 做 gating/apply/rollback)

四组件循环(§3.2):Inference Agent(run_one 已有) → Wiki Maintainer(consolidate)
  → Skill Proposer(propose) → Gating&Rollback(evolve_config apply/rollback)。
  ★ 命门:skill 可回滚,但 wiki 持久沉淀(论文 ablation 证实持久知识是关键)。
  ★ 反直觉约束(论文§5.1):wiki 喂给「提炼 skill 的那步」,不喂给「正在干活的 agent」——
     给 agent 直接读 wiki 反而降低最终 skill 质量。本模块的 consolidate/propose 是"提炼侧"。

wiki 文件结构照论文附录 E(Wiki Maintainer prompt 真规范):
  wiki/<uid>/index.md        已知模式的简明目录(一行一 pattern)
  wiki/<uid>/log.md          时序进化日志(迭代/分数/accept-reject)
  wiki/<uid>/skill-impact.md 哪些 skill 试过、结果如何
  wiki/<uid>/patterns/<k>.md 每个模式一页·根因+证据+可操作 workaround

设计边界(对齐军团纲领「context not control」):
  - Wiki Maintainer 用军团自己的模型(reflector 家)读 trace 提炼 pattern·父层不写答题逻辑。
    模型不可用时降级为「结构化事实沉淀」(把 trace 事实归纳成 pattern·不编造根因)。
  - 每用户隔离(隐私铁律):wiki 只沉淀本用户轨迹·跨用户不可见。
  - 路径走 legion_paths(测试隔离铁律·禁止手写 ~/.meyo)。
"""
from __future__ import annotations
import json
import os
import re
import time
from typing import Any, Optional


def _root() -> str:
    # 与 evolve_config 同源(MEYO_LEGION_DIR 可重定向·测试隔离)
    return os.environ.get("MEYO_LEGION_DIR") or os.path.expanduser("~/.meyo/legion")


def _safe_uid(uid: str) -> str:
    return "".join(c for c in str(uid or "default") if c.isalnum() or c in "_.-")[:80] or "default"


def _wiki_dir(uid: str) -> str:
    d = os.path.join(_root(), "wiki", _safe_uid(uid))
    os.makedirs(os.path.join(d, "patterns"), exist_ok=True)
    return d


def _read(path: str) -> str:
    try:
        return open(path, encoding="utf-8").read()
    except Exception:
        return ""


def _write(path: str, content: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Patch 增量操作(照论文附录 E:append / replace / insert_after)
# Wiki Maintainer 用它增量更新 wiki·而不是整体重写(保留历史·可追溯)。
# ---------------------------------------------------------------------------
def apply_patch(text: str, edits: list) -> str:
    """对一段文本套用 patch 操作列表(照论文 patch 语义)。target 必须精确子串匹配。"""
    out = text
    for e in edits or []:
        if not isinstance(e, dict):
            continue
        op = e.get("op")
        content = str(e.get("content", ""))
        if op == "append":
            out = out + ("\n" if out and not out.endswith("\n") else "") + content
        elif op == "replace":
            tgt = str(e.get("target", ""))
            if tgt and tgt in out:
                out = out.replace(tgt, content, 1)
        elif op == "insert_after":
            tgt = str(e.get("target", ""))
            if tgt and tgt in out:
                idx = out.index(tgt) + len(tgt)
                out = out[:idx] + content + out[idx:]
    return out


# ---------------------------------------------------------------------------
# Raw Layer:只读引用 sessions(不复制轨迹·论文「immutable execution traces」)
# ---------------------------------------------------------------------------
def _collect_traces(uid: str, window: int = 20) -> list[dict]:
    """从 sessions 拿本用户最近 window 次派活的轨迹摘要(Raw Layer 引用·只读)。"""
    try:
        import sessions
        recent = sessions.list_sessions(limit=window, user_id=uid)
    except Exception:
        return []
    traces = []
    for s in recent:
        try:
            d = sessions.load_session(s["sid"], user_id=uid) or {}
        except Exception:
            continue
        for t in d.get("turns", []):
            r = t.get("result") if isinstance(t.get("result"), dict) else t
            q = t.get("question") or (d.get("turns", [{}])[0] or {}).get("question", "")
            per_harness = []
            for x in (r.get("results") or []):
                if isinstance(x, dict):
                    per_harness.append({
                        "key": x.get("key") or x.get("harness"),
                        "status": x.get("status"),
                        "final": (x.get("final") or x.get("answer") or "")[:600],
                        "error": (x.get("error") or "")[:200],
                    })
            winner = r.get("winner") if isinstance(r.get("winner"), dict) else {}
            traces.append({
                "sid": s["sid"],
                "question": (q or "")[:300],
                "winner_key": winner.get("key", ""),
                "winner_final": (winner.get("final") or winner.get("answer") or "")[:800],
                "per_harness": per_harness,
                "ts": t.get("ts") or s.get("ts"),
            })
    return traces


# ---------------------------------------------------------------------------
# Wiki Layer:Wiki Maintainer — consolidate traces into persistent knowledge
# ---------------------------------------------------------------------------
_MAINTAINER_PROMPT = """你是军团的 Wiki Maintainer(依据 WikiSkill 论文)。任务:读最近的执行轨迹,
把散落的经验【整合(consolidate)成结构化的可复用知识 pattern】——找根因,不只记表面症状。

请输出严格 JSON(不要任何多余文字):
{{
  "patterns": [
    {{
      "key": "kebab-case-短标识",
      "title": "一句话模式名(如:融资BP交付要配财务模型Excel)",
      "kind": "success" 或 "failure",
      "trigger": "什么情况下适用(任务特征)",
      "root_cause": "根因分析(失败模式:为什么失败;成功策略:为什么有效)",
      "workaround": "可操作的做法/复用策略(下次遇到该怎么做)",
      "evidence": "来自哪些轨迹的证据(引用 question 片段)"
    }}
  ]
}}
只提炼真正有复用价值的 pattern(2-6 个)。宁缺毋滥,别把一次性琐事当 pattern。

最近的执行轨迹:
{traces}
"""


def _summarize_traces_for_prompt(traces: list[dict], limit: int = 15) -> str:
    lines = []
    for t in traces[:limit]:
        ph = "; ".join(
            f"{h.get('key')}={h.get('status')}" + (f"(err:{h['error']})" if h.get("error") else "")
            for h in t.get("per_harness", [])
        )
        lines.append(
            f"- 问题:{t.get('question','')[:160]}\n"
            f"  各家:{ph or '(无)'}\n"
            f"  胜出:{t.get('winner_key','')} | 交付摘要:{(t.get('winner_final') or '')[:200]}"
        )
    return "\n".join(lines) if lines else "(暂无轨迹)"


def _reflect_patterns(traces: list[dict], reflector: str = "auto") -> tuple[list[dict], str]:
    """用军团模型(reflector 家)提炼 pattern。模型不可用则降级为结构化事实沉淀。

    返回 (patterns, mode)·mode ∈ {"model","fallback"}·诚实标注是模型提炼还是事实归纳。
    """
    prompt = _MAINTAINER_PROMPT.format(traces=_summarize_traces_for_prompt(traces))
    try:
        import legion_core
        r = legion_core.run_one(reflector, prompt, "wiki_maintain", lambda ev: None)
        final = (r.get("final") or "").strip() if isinstance(r, dict) else ""
        m = re.search(r"\{.*\}", final, re.S)
        if m:
            data = json.loads(m.group(0))
            pats = data.get("patterns")
            if isinstance(pats, list) and pats:
                # 规范化 + 兜底 key
                out = []
                for p in pats:
                    if not isinstance(p, dict):
                        continue
                    k = str(p.get("key") or p.get("title") or "pattern")
                    k = re.sub(r"[^a-z0-9\-]+", "-", k.lower()).strip("-")[:50] or f"p{len(out)}"
                    out.append({
                        "key": k,
                        "title": str(p.get("title", ""))[:120],
                        "kind": p.get("kind") if p.get("kind") in ("success", "failure") else "success",
                        "trigger": str(p.get("trigger", ""))[:300],
                        "root_cause": str(p.get("root_cause", ""))[:600],
                        "workaround": str(p.get("workaround", ""))[:600],
                        "evidence": str(p.get("evidence", ""))[:300],
                    })
                if out:
                    return out, "model"
    except Exception:
        pass
    # 降级:模型不可用/没返 JSON → 只做结构化事实沉淀(不编造根因·诚实标注)
    return _fallback_patterns(traces), "fallback"


def _fallback_patterns(traces: list[dict]) -> list[dict]:
    """降级沉淀:把轨迹里的可观测事实归纳成 pattern(不臆造根因)。

    context not control:只呈现"发生了什么"(哪家在哪类任务成功/失败),让用户/模型自己判断。
    """
    from collections import defaultdict
    succ = defaultdict(int)
    fail = defaultdict(list)
    for t in traces:
        for h in t.get("per_harness", []):
            k = h.get("key") or "?"
            st = (h.get("status") or "").lower()
            if st in ("done", "ok"):
                succ[k] += 1
            elif h.get("error") or st in ("error", "timeout", "failed"):
                fail[k].append((t.get("question", "")[:80], h.get("error", "")[:100]))
    pats = []
    if succ:
        top = sorted(succ.items(), key=lambda x: -x[1])[:3]
        pats.append({
            "key": "reliable-harnesses",
            "title": "近期稳定交付的家",
            "kind": "success",
            "trigger": "常规派活时的家选择参考",
            "root_cause": "（事实沉淀·未经模型根因分析）",
            "workaround": "近期成功次数最多:" + ", ".join(f"{k}({c})" for k, c in top),
            "evidence": f"基于最近 {len(traces)} 条轨迹的可观测状态",
        })
    for k, fs in list(fail.items())[:3]:
        if fs:
            pats.append({
                "key": f"failure-{re.sub(r'[^a-z0-9]+','-',k.lower())}"[:50],
                "title": f"{k} 近期失败模式(待关注)",
                "kind": "failure",
                "trigger": "; ".join(q for q, _ in fs[:3]),
                "root_cause": "（事实沉淀·未经模型根因分析·建议开启模型 reflector 提炼)",
                "workaround": "遇到该家失败可换家补位;错误样本:" + "; ".join(e for _, e in fs[:2] if e),
                "evidence": f"{len(fs)} 次失败观测",
            })
    return pats


def consolidate(uid: str, window: int = 20, reflector: str = "auto") -> dict:
    """Wiki Maintainer:读 recent traces → 提炼 pattern → 增量 patch 进 wiki(持久沉淀)。

    这是 WikiSkill 的命门:经验先沉淀成结构化知识,而不是分析完直接改 skill 就丢。
    """
    traces = _collect_traces(uid, window)
    if not traces:
        return {"ok": False, "error": "无派活轨迹·先派几次活积累经验(切身份不是 default 后)"}

    patterns, mode = _reflect_patterns(traces, reflector)
    if not patterns:
        return {"ok": False, "error": "未能提炼出有复用价值的 pattern"}

    wd = _wiki_dir(uid)
    ts = int(time.time())
    tstr = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))

    # 1) patterns/<key>.md — 每个模式一页(照论文结构)
    written = []
    for p in patterns:
        page_path = os.path.join(wd, "patterns", f"{p['key']}.md")
        existing = _read(page_path)
        page = (
            f"# {p['title']}\n\n"
            f"- **类型**: {'✅ 成功策略' if p['kind']=='success' else '⚠️ 失败模式'}\n"
            f"- **适用**: {p['trigger']}\n"
            f"- **根因**: {p['root_cause']}\n"
            f"- **做法(workaround)**: {p['workaround']}\n"
            f"- **证据**: {p['evidence']}\n"
            f"- **更新**: {tstr}\n"
        )
        if existing:
            # 已有则追加一条更新记录(patch append·保留历史·论文增量语义)
            page = apply_patch(existing, [{"op": "append",
                "content": f"\n---\n### 更新 {tstr}\n{p['workaround']}\n"}])
        _write(page_path, page)
        written.append(p["key"])

    # 2) index.md — 目录(一行一 pattern·照论文)
    idx_lines = ["# Wiki 索引(已知模式目录)\n"]
    for fn in sorted(os.listdir(os.path.join(wd, "patterns"))):
        if fn.endswith(".md"):
            key = fn[:-3]
            first = _read(os.path.join(wd, "patterns", fn)).splitlines()
            title = first[0].lstrip("# ").strip() if first else key
            idx_lines.append(f"- [{title}](patterns/{fn}) `{key}`")
    _write(os.path.join(wd, "index.md"), "\n".join(idx_lines) + "\n")

    # 3) log.md — 时序进化日志(patch append·照论文)
    log_path = os.path.join(wd, "log.md")
    log = _read(log_path) or "# 进化日志(时序·迭代/分数/accept-reject)\n"
    log = apply_patch(log, [{"op": "append",
        "content": f"\n## {tstr}\n- consolidate:{len(traces)} 条轨迹 → {len(patterns)} 个 pattern"
                   f"({'模型提炼' if mode=='model' else '事实沉淀降级'})\n"
                   f"- 更新 pattern:{', '.join(written)}\n"}])
    _write(log_path, log)

    return {"ok": True, "consolidated": len(traces), "patterns": len(patterns),
            "pattern_keys": written, "mode": mode, "ts": ts,
            "note": "经验已沉淀进持久 wiki(即使 skill 回滚 wiki 也保留)"}


# ---------------------------------------------------------------------------
# Skill Layer:Skill Proposer — 用 wiki 提议可复用 skill/pref(不读原始 trace)
# ★ 论文§5.1:wiki 喂提炼步·不喂正在干活的 agent。这里正是提炼步。
# ---------------------------------------------------------------------------
def record_verified_pattern(uid: str, pattern: dict, source: str = "source_audit") -> dict:
    """把已核验的源码/运行经验直接沉淀为共享 Wiki pattern。

    source_audit 已负责证据校验；这里保留增量历史并更新索引，让一次适配问题自动成为
    后续所有成员可召回的进化知识，而不是只留在某次聊天或报告里。
    """
    required = ("key", "title", "kind", "trigger", "root_cause", "workaround", "evidence")
    missing = [key for key in required if pattern.get(key) in (None, "")]
    if missing:
        return {"ok": False, "recorded": False, "error": "pattern 缺字段", "missing": missing}
    key = re.sub(r"[^a-z0-9\-]+", "-", str(pattern["key"]).lower()).strip("-")[:100]
    if not key:
        return {"ok": False, "recorded": False, "error": "pattern key 无效"}
    wd = _wiki_dir(uid)
    tstr = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    page_path = os.path.join(wd, "patterns", f"{key}.md")
    page = (
        f"# {pattern['title']}\n\n"
        f"- **类型**: {'✅ 成功策略' if pattern['kind']=='success' else '⚠️ 失败模式'}\n"
        f"- **适用**: {pattern['trigger']}\n"
        f"- **根因**: {pattern['root_cause']}\n"
        f"- **做法(workaround)**: {pattern['workaround']}\n"
        f"- **证据**: {pattern['evidence']}\n"
        f"- **来源**: {source}\n"
        f"- **更新**: {tstr}\n"
    )
    existing = _read(page_path)
    if existing:
        page = apply_patch(existing, [{"op": "append", "content": (
            f"\n---\n### 核验更新 {tstr}\n- 来源: {source}\n"
            f"- 证据: {pattern['evidence']}\n- 做法: {pattern['workaround']}\n")}])
    _write(page_path, page)
    idx_lines = ["# Wiki 索引(已知模式目录)\n"]
    for fn in sorted(os.listdir(os.path.join(wd, "patterns"))):
        if fn.endswith(".md"):
            lines = _read(os.path.join(wd, "patterns", fn)).splitlines()
            title = lines[0].lstrip("# ").strip() if lines else fn[:-3]
            idx_lines.append(f"- [{title}](patterns/{fn}) `{fn[:-3]}`")
    _write(os.path.join(wd, "index.md"), "\n".join(idx_lines) + "\n")
    log_path = os.path.join(wd, "log.md")
    log = _read(log_path) or "# 进化日志(时序·迭代/分数/accept-reject)\n"
    log = apply_patch(log, [{"op": "append", "content": (
        f"\n## {tstr}\n- verified-source-pattern:`{key}` · 来源:{source}\n")}])
    _write(log_path, log)
    return {"ok": True, "recorded": True, "key": key, "path": page_path, "source": source}


def read_wiki(uid: str) -> dict:
    """读某用户的完整 wiki(供 Skill Proposer / 前端查看)。"""
    wd = _wiki_dir(uid)
    patterns = []
    pdir = os.path.join(wd, "patterns")
    if os.path.isdir(pdir):
        for fn in sorted(os.listdir(pdir)):
            if fn.endswith(".md"):
                patterns.append({"key": fn[:-3], "content": _read(os.path.join(pdir, fn))})
    return {
        "index": _read(os.path.join(wd, "index.md")),
        "log": _read(os.path.join(wd, "log.md")),
        "skill_impact": _read(os.path.join(wd, "skill-impact.md")),
        "patterns": patterns,
        "pattern_count": len(patterns),
    }


def read_pattern(uid: str, name: str) -> dict:
    key = re.sub(r"[^a-z0-9\-]+", "", str(name or "").lower())
    fp = os.path.join(_wiki_dir(uid), "patterns", f"{key}.md")
    if not os.path.isfile(fp):
        return {"ok": False, "error": "pattern not found"}
    return {"ok": True, "key": key, "content": _read(fp)}


def propose_skills(uid: str) -> dict:
    """Skill Proposer:基于 wiki(不基于原始 trace)提议可复用 skill/pref 更新。

    产出 = 交给 evolve_config 做 gating/apply/rollback 的候选。诚实边界:
    只从沉淀的 pattern 归纳"可复用做法",不替 harness 写答题逻辑。
    """
    wiki = read_wiki(uid)
    if wiki["pattern_count"] == 0:
        return {"ok": False, "error": "wiki 为空·先 consolidate 沉淀经验"}
    success_skills, watch_failures = [], []
    for p in wiki["patterns"]:
        c = p["content"]
        title = (c.splitlines()[0].lstrip("# ").strip() if c else p["key"])
        is_fail = "失败模式" in c or '"failure"' in c
        # 抽 workaround 行作为可复用做法
        mm = re.search(r"做法\(workaround\)\*\*:\s*(.+)", c)
        workaround = mm.group(1).strip() if mm else ""
        item = {"key": p["key"], "title": title, "workaround": workaround}
        (watch_failures if is_fail else success_skills).append(item)
    return {
        "ok": True,
        "proposed_skills": success_skills,   # 可复用的成功策略(建议固化)
        "watch_failures": watch_failures,    # 需关注的失败模式(建议规避)
        "source": "wiki",
        "note": "基于持久 wiki 提炼(论文:wiki 喂提炼步·不喂正在干活的 agent)",
    }


def record_skill_impact(uid: str, skill_key: str, outcome: str, detail: str = "") -> dict:
    """outer-loop 程序化更新 skill-impact.md(照论文:哪些 skill 试过、结果如何)。"""
    wd = _wiki_dir(uid)
    fp = os.path.join(wd, "skill-impact.md")
    cur = _read(fp) or "# Skill 影响记录(哪些 skill 试过·结果如何)\n"
    tstr = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    cur = apply_patch(cur, [{"op": "append",
        "content": f"\n- [{tstr}] `{skill_key}` → **{outcome}** {detail}"}])
    _write(fp, cur)
    return {"ok": True, "skill_key": skill_key, "outcome": outcome}


def stats(uid: str) -> dict:
    w = read_wiki(uid)
    return {"uid": _safe_uid(uid), "pattern_count": w["pattern_count"],
            "has_log": bool(w["log"]), "has_impact": bool(w["skill_impact"]),
            "dir": _wiki_dir(uid)}


if __name__ == "__main__":
    import sys
    os.environ.setdefault("MEYO_LEGION_DIR", os.path.expanduser("~/.meyo/legion"))
    uid = sys.argv[2] if len(sys.argv) > 2 else "default"
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "consolidate":
        print(json.dumps(consolidate(uid), ensure_ascii=False, indent=2))
    elif cmd == "view":
        print(json.dumps(read_wiki(uid), ensure_ascii=False, indent=2))
    elif cmd == "propose":
        print(json.dumps(propose_skills(uid), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(stats(uid), ensure_ascii=False, indent=2))
