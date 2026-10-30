#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""skill_improver:军团自进化 improver 层(用户2026-08-30·对照Warp《self-improving agents》补齐).

Warp 自进化杀手锏 = outer/improver skill:一个【定时观察者】,不是每任务跑,而是定期拉
累积的【人类反馈】,对比 agent 当初的建议 vs 人类实际反应,对 base skill 提【一个小而聚焦的
编辑】,走【人审→合并】,下次 skill 继承改进。任何 agent 只要建了这个 loop 就会越用越强。

我们已有的原料(前几轮做的):
- legion_memory 经验飞轮(跨家经验+置信度/采纳率)——被动记忆召回
- skill_factory.feedback(带 rating/why 的反馈·Warp:带why最有价值)——反馈原料
- cron_jobs(定时基建)——让 improver 定时跑
缺的就是把它们串成【定时拉反馈→聚合反复出现的问题→产出可审的skill编辑提议→人审→生效】。

守 CLAUDE.md 铁律:
- 人在回路:improver 只【产出提议】(proposal),绝不自动改 skill 文件·不自动 merge(Warp 也强调
  "keep a human in the loop"·"final human step closes the loop")。
- context not control:提议的原料是【真实用户反馈】·不是父层猜;把反馈聚合给用户/军团判断。
- 反馈可能是错的(Warp:assume it will be):improver 只聚合呈现·由人筛选采纳·不盲信。
"""
from __future__ import annotations
import json
import os
import time


def _skill_factory():
    import skill_factory
    return skill_factory


def collect_feedback(skill_name: str, uid: str = "default", since_days: int = 30) -> dict:
    """拉某 skill 近 since_days 的累积使用反馈(improver 的输入)。
    返回 {skill, total, good, bad, whys:[...], samples:[...]}。"""
    sf = _skill_factory()
    root = sf.user_root(uid)
    ledger = root / "factory.jsonl"
    if not ledger.is_file():
        return {"ok": True, "skill": skill_name, "total": 0, "good": 0, "bad": 0, "whys": [], "samples": []}
    cutoff = time.time() - since_days * 86400
    good = bad = 0
    whys = []
    samples = []
    try:
        for line in ledger.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("event") != "use" or ev.get("name") != skill_name:
                continue
            if float(ev.get("ts", 0) or 0) < cutoff and ev.get("ts"):
                continue
            r = (ev.get("rating") or "").lower()
            if r == "good":
                good += 1
            elif r == "bad":
                bad += 1
            why = (ev.get("why") or "").strip()
            if why:
                whys.append({"rating": r, "why": why, "task": (ev.get("task") or "")[:120]})
            samples.append({"task": (ev.get("task") or "")[:100], "rating": r,
                            "agent": ev.get("agent", "")})
    except Exception as e:
        return {"ok": False, "error": f"读反馈失败: {e}"}
    return {"ok": True, "skill": skill_name, "total": good + bad + len([s for s in samples if not s["rating"]]),
            "good": good, "bad": bad, "whys": whys[-20:], "samples": samples[-20:]}


def propose_improvement(skill_name: str, uid: str = "default", since_days: int = 30) -> dict:
    """Warp 式:对比反馈·产出【可审的 skill 编辑提议】(不自动改文件·人审后才生效)。

    只做聚合+呈现:把带 why 的负反馈归拢成"反复出现的问题",连同原 skill 定位一起给用户/军团,
    让【模型/人】据此判断怎么改(context not control·不由父层写死改法·不自动 merge)。
    """
    fb = collect_feedback(skill_name, uid, since_days)
    if not fb.get("ok"):
        return fb
    if fb["total"] == 0:
        return {"ok": True, "skill": skill_name, "has_proposal": False,
                "reason": "该 skill 近期无使用反馈·无可改进信号(Warp:低摩擦收集反馈是前提)"}
    # 找 skill 文件位置(可读可审的 base skill)
    skill_md = None
    try:
        import func_index  # 复用全仓 skill 扫描不必要·直接找 repo skill
    except Exception:
        pass
    try:
        sf = _skill_factory()
        for r in sf._scan_repo_skills():
            if r["name"] == skill_name:
                skill_md = r.get("skill_md")
                break
    except Exception:
        pass
    bad_whys = [w for w in fb["whys"] if w["rating"] == "bad"]
    good_whys = [w for w in fb["whys"] if w["rating"] == "good"]
    # 提议 = 呈现真实反馈信号 + 给出"下一步怎么改"的入口(交军团模型/人写具体编辑·不父层猜)
    proposal = {
        "ok": True, "skill": skill_name, "has_proposal": bool(bad_whys or fb["bad"]),
        "skill_md": skill_md,
        "signal": {"good": fb["good"], "bad": fb["bad"],
                   "bad_reasons": [w["why"] for w in bad_whys][:8],
                   "good_reasons": [w["why"] for w in good_whys][:4]},
        "how_to_apply": (
            f"这是 improver 聚合的反馈信号(非自动改)。要改进 {skill_name}:"
            f"①读 {skill_md or '该skill的SKILL.md'} 现状 "
            f"②对照上面 bad_reasons(用户反复指出的问题)提一个【小而聚焦】的编辑(Warp:小编辑·写原则非规则·解释why)"
            f"③走人审(改动可审可并)→ 下次 skill 继承。"
            f"可派军团:run runnable:func_call 让某家读skill+反馈后产出编辑;人审后再写入。"),
        "human_in_loop": "improver 只产出提议·绝不自动改 skill/自动 merge(Warp:final human step closes the loop·反馈可能错要人筛)",
    }
    return proposal


def improve_all(uid: str = "default", since_days: int = 30, limit: int = 20) -> dict:
    """定时观察者入口(供 cron 调):扫近期有反馈的 skill·各产出提议汇总。
    定时(非每任务)运行=Warp improver 的关键特征。"""
    sf = _skill_factory()
    root = sf.user_root(uid)
    ledger = root / "factory.jsonl"
    if not ledger.is_file():
        return {"ok": True, "proposals": [], "note": "无反馈账本"}
    cutoff = time.time() - since_days * 86400
    skills_with_fb = set()
    for line in ledger.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("event") == "use" and ev.get("name") and (ev.get("rating") or ev.get("why")):
            if not ev.get("ts") or float(ev.get("ts", 0)) >= cutoff:
                skills_with_fb.add(ev["name"])
    proposals = []
    for s in sorted(skills_with_fb)[:limit]:
        p = propose_improvement(s, uid, since_days)
        if p.get("has_proposal"):
            proposals.append(p)
    return {"ok": True, "proposals": proposals, "scanned": len(skills_with_fb),
            "note": "improver 定时观察者·聚合反馈产出可审提议·人审后才改 skill(自进化闭环)"}


if __name__ == "__main__":
    os.environ.setdefault("MEYO_LEGION_DIR", os.path.expanduser("~/.meyo/legion"))
    import sys
    if len(sys.argv) > 1:
        print(json.dumps(propose_improvement(sys.argv[1]), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(improve_all(), ensure_ascii=False, indent=2))
