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
import hashlib
import json
import os
import pathlib
import time


def _skill_factory():
    import skill_factory
    return skill_factory


def _user_skill_root(sf, uid: str) -> pathlib.Path:
    """Trust the configured users store, not symlinks redirecting one user's namespace."""
    root = sf.user_root(uid).resolve()
    expected = pathlib.Path(sf.ROOT).resolve() / sf._safe(uid or "default") / "skills"
    if root != expected:
        raise ValueError("user_skill_root_redirected")
    return root


def collect_feedback(skill_name: str, uid: str = "default", since_days: int = 30) -> dict:
    """拉某 skill 近 since_days 的累积使用反馈(improver 的输入)。
    返回 {skill, total, good, bad, whys:[...], samples:[...]}。"""
    sf = _skill_factory()
    try:
        root = _user_skill_root(sf, uid)
    except (OSError, RuntimeError, ValueError):
        return {"ok": False, "error": "user_skill_root_unavailable"}
    ledger = root / "factory.jsonl"
    if ledger.is_symlink():
        return {"ok": False, "error": "feedback_ledger_redirected"}
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


def _skill_parent(skill_name: str, uid: str) -> dict:
    """Identify the actual parent bytes without editing or silently choosing a namesake."""
    sf = _skill_factory()
    unresolved = {"status": "unresolved", "source": "", "skill_md": None,
                  "revision": "", "content_sha256": "", "candidates": []}
    try:
        root = _user_skill_root(sf, uid)
    except (OSError, RuntimeError, ValueError):
        return {**unresolved, "source": "user_active", "reason": "user_skill_root_redirected"}
    try:
        active = [row for row in sf.list_active(uid).get("skills", [])
                  if row.get("name") == sf._safe(skill_name)]
    except (OSError, RuntimeError, ValueError):
        return {**unresolved, "source": "user_active", "reason": "active_lookup_failed"}
    if active:
        if len(active) != 1:
            return {**unresolved, "source": "user_active", "reason": "multiple_active_parents"}
        try:
            md = pathlib.Path(active[0]["skill_md"]).resolve(strict=True)
            owned = root / sf._safe(skill_name)
            if owned.resolve() != owned or not md.is_relative_to(owned) or not md.is_file():
                return {**unresolved, "source": "user_active", "reason": "active_outside_user_skill"}
            raw = md.read_bytes()
            return {"status": "resolved", "source": "user_active", "skill_md": str(md),
                    "revision": md.parent.name, "content_sha256": hashlib.sha256(raw).hexdigest(),
                    "candidates": [], "note": "这是用户启用副本，不代表仓库维护源；提议不直接修改安装物。"}
        except (OSError, RuntimeError, ValueError, KeyError):
            return {**unresolved, "source": "user_active", "reason": "active_parent_unreadable"}
    candidates = []
    seen = set()
    try:
        for row in sf._scan_repo_skills():
            if row.get("name") != skill_name or not row.get("skill_md"):
                continue
            md = pathlib.Path(row["skill_md"])
            if not md.is_absolute():
                md = pathlib.Path(sf._REPO_SKILL) / md
            try:
                md = md.resolve(strict=True)
                if not md.is_file() or str(md) in seen:
                    continue
                raw = md.read_bytes()
            except (OSError, RuntimeError, ValueError):
                continue
            seen.add(str(md))
            candidates.append({"source": "repo", "skill_md": str(md), "revision": "",
                               "content_sha256": hashlib.sha256(raw).hexdigest()})
    except (OSError, RuntimeError, ValueError):
        return {**unresolved, "source": "repo", "reason": "repository_lookup_failed"}
    if len(candidates) == 1:
        return {"status": "resolved", **candidates[0], "candidates": candidates}
    return {**unresolved, "source": "repo", "candidates": candidates,
            "status": "ambiguous" if candidates else "unresolved",
            "reason": "multiple_repository_parents" if candidates else "parent_not_found"}


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
    # 用户正在使用的版本优先；同名仓库副本不等于真父代。
    base = _skill_parent(skill_name, uid)
    skill_md = base["skill_md"]
    bad_whys = [w for w in fb["whys"] if w["rating"] == "bad"]
    good_whys = [w for w in fb["whys"] if w["rating"] == "good"]
    # 提议 = 呈现真实反馈信号 + 给出"下一步怎么改"的入口(交军团模型/人写具体编辑·不父层猜)
    proposal = {
        "ok": True, "skill": skill_name, "has_proposal": bool(bad_whys or fb["bad"]),
        "skill_md": skill_md,
        "base": base,
        "feedback_version_binding": "name_only_unknown",
        "signal": {"good": fb["good"], "bad": fb["bad"],
                   "bad_reasons": [w["why"] for w in bad_whys][:8],
                   "good_reasons": [w["why"] for w in good_whys][:4]},
        "how_to_apply": (
            f"这是 improver 聚合的反馈信号(非自动改)。要改进 {skill_name}:"
            f"①核对父代 {skill_md or '未唯一定位；先查 base 状态与候选，不能猜一个同名副本'} "
            f"及内容指纹 {base['content_sha256'] or '未知'}；实际编辑前重新核对，变化则重新生成提议。"
            "反馈账本仅按名称聚合，不能假定全部来自当前版本。用户启用副本与仓库维护源分开。"
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
    try:
        root = _user_skill_root(sf, uid)
    except (OSError, RuntimeError, ValueError):
        return {"ok": False, "error": "user_skill_root_unavailable"}
    ledger = root / "factory.jsonl"
    if ledger.is_symlink():
        return {"ok": False, "error": "feedback_ledger_redirected"}
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
