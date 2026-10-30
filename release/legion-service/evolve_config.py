#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evolve_config:用户手动配置的自进化(用户 8-30 澄清:我们不做自进化·用户自己配置)。

设计原则(用户原话对齐):
- 「进化就是进化·你做啥呢」→ 不做自动加权 hack·真进化 = state_{n+1}=reflect(state_n,evidence)
- 「用户可以配置」→ 用户勾选 targets(进化什么) + schedule(节奏)
- 「自进化不默认开启」→ enabled=False 默认·所有 target 默认关
- 「进化后的具体变化可以看」→ diff 页可看 before/after
- 「还有具体变化效果」→ 每次进化记录 evolution·可对比效果

配置结构(每用户一份·完整 uid 的 UTF-8 SHA256 隔离·存 evolve/by-owner/<hash>.json):
{
  "enabled": bool,                    # 总开关(默认 False·符合用户"不默认开启")
  "targets": {                        # 进化什么(每项可单独开关·默认全 False)
    "harness_pref": False,            # 我最常用哪些家(进化→写入你的 pref 文档·派单会读)
    "captain_bias": False,            # 首脑竞选偏好(进化→写入 captain 提示)
    "workflow_snippets": False,       # 常用工作流片段(进化→存成 skill 建议)
  },
  "schedule": "manual",               # manual(手动·默认) / daily / weekly
  "evidence_window": 20,              # 基于最近多少次派活反思(默认 20)
  "auto_apply": False,                # 进化产出后是否自动应用(默认 False·必须手动审)
}

进化流程(用户点"立即进化" or 定时触发):
1. 读用户 recent_sessions(evidence_window 条)
2. 让 reflector(默认用 auto·可选 penguin/prime)读旧 pref + evidence → 提议新 pref
3. 写入 evolutions/by-owner/<hash>/<ts>.json:{before, after, diff, evidence_count, reflector}
4. 用户在面板看 diff·点"应用"才生效(或 auto_apply=True 自动应用但仍留 diff 可回滚)
5. 应用后 apply_active_pref 会把 active pref 挂进 runtime_brief

旧清洗/截断路径没有完整 owner 证明，保留原文件，不自动认领或迁移。
"""
from __future__ import annotations
from contextlib import contextmanager
import copy
import hashlib
import json
import os
import stat
import time
from typing import Any


def _root() -> str:
    root = os.environ.get("MEYO_LEGION_DIR") or os.path.expanduser("~/.meyo/legion")
    return root


def _owner_key(uid: str) -> str:
    """完整身份不清洗、截断或折叠；缺失身份也不冒充字面 default 用户。"""
    owner = "" if uid is None else str(uid)
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()


def _config_path(uid: str) -> str:
    d = os.path.join(_root(), "evolve", "by-owner")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{_owner_key(uid)}.json")


def _evolution_dir(uid: str) -> str:
    d = os.path.join(_root(), "evolutions", "by-owner", _owner_key(uid))
    os.makedirs(d, exist_ok=True)
    return d


@contextmanager
def _storage_dir(uid: str, *, evolution: bool = False, create: bool = False):
    """从可信状态根逐层固定目录；下级链接或临开替换不能切换 owner。"""
    root = _root()
    if create:
        os.makedirs(root, exist_ok=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(root, flags)
    parts = ("evolutions", "by-owner", _owner_key(uid)) if evolution else ("evolve", "by-owner")
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _json_file(directory: int, name: str, *, write: bool = False):
    """相对固定目录打开同一普通文件；先拒绝链接/特殊文件，再读取或截断。"""
    flags = os.O_NOFOLLOW | os.O_NONBLOCK
    flags |= (os.O_WRONLY | os.O_CREAT) if write else os.O_RDONLY
    fd = os.open(name, flags, mode=0o600, dir_fd=directory)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        if write:
            os.ftruncate(fd, 0)
        stream = os.fdopen(fd, "w" if write else "r", encoding="utf-8")
        fd = None
        with stream:
            yield stream
    finally:
        if fd is not None:
            os.close(fd)


def _valid_evolution_id(evolution_id: str) -> bool:
    # ID 是原样文件名而非路径；不清洗、强转或猜测另一条记录。
    return (isinstance(evolution_id, str) and bool(evolution_id)
            and evolution_id not in {".", ".."}
            and not any(char in evolution_id for char in ("/", "\\", "\0")))


_DEFAULT_CONFIG = {
    "enabled": False,           # 用户"不默认开启"(pref 层旧语义·保持)
    "targets": {"harness_pref": False, "captain_bias": False, "workflow_snippets": False},
    "schedule": "manual",
    "evidence_window": 20,
    "auto_apply": False,
    "reflector": "penguin",     # 反思用哪家 harness(用户2026-09-01:优先带进化能力的家。
                                # penguin='agents build agents'自进化·prime=IPython自进化·
                                # 比 auto 更懂"从历史学习优化"。可选 prime/auto/openhuman)
    "active_pref": {},          # 已应用的进化产物(harness_pref/captain_bias/... 各一份)
    "active_evolution_id": "",  # 当前生效的 evolution 时间戳(用于回滚)
    # ── WikiSkill 三层知识架构(用户 2026-08-31 定:给每个用户默认实现·三层全部默认开)──
    # 依据 Google Research WikiSkill 论文(arXiv:2608.27454·见 dev/wikiskill-design-2026-08-31.md):
    #   经验先沉淀进持久 wiki·再复用·而不是分析完轨迹直接改 skill 用一次就丢。
    #   沉淀(consolidate)+提炼(propose)默认开(后台无感·只读积累);
    #   应用(apply)默认关(守用户"应用要审"的产品定调·符合论文 Gating 由验证/人审把关)。
    "wiki": {
        "enabled": True,        # WikiSkill 总开关(默认开·用户可关)
        "consolidate": True,    # Wiki Maintainer:沉淀经验进持久 wiki(默认开)
        "propose": True,        # Skill Proposer:从 wiki 提炼可复用 skill(默认开)
        "auto_apply": False,    # 提炼产物是否自动应用(默认关·必须用户审·论文 gating)
    },
}


def load_config(uid: str) -> dict:
    """加载用户自进化配置·不存在返回默认(全关)。"""
    try:
        with _storage_dir(uid) as directory:
            with _json_file(directory, f"{_owner_key(uid)}.json") as stream:
                d = json.load(stream)
        # 合并默认值(向前兼容:老配置缺字段时补默认)
        cfg = copy.deepcopy(_DEFAULT_CONFIG)
        cfg.update({k: v for k, v in d.items() if k in _DEFAULT_CONFIG})
        cfg["targets"] = {**_DEFAULT_CONFIG["targets"], **(d.get("targets") or {})}
        cfg["wiki"] = {**_DEFAULT_CONFIG["wiki"], **(d.get("wiki") or {})}
        return cfg
    except Exception:
        return copy.deepcopy(_DEFAULT_CONFIG)


def _write_config(uid: str, cfg: dict) -> dict:
    try:
        with _storage_dir(uid, create=True) as directory:
            with _json_file(directory, f"{_owner_key(uid)}.json", write=True) as stream:
                json.dump(cfg, stream, ensure_ascii=False, indent=2)
    except Exception as e:
        return {"ok": False, "reason": "config_write_failed", "error": str(e)}
    return {"ok": True, "config": cfg}


def save_config(uid: str, cfg: dict) -> dict:
    """保存用户自进化配置。cfg 只允许更新已知字段·防注入。"""
    cur = load_config(uid)
    for k in ("enabled", "schedule", "evidence_window", "auto_apply", "reflector"):
        if k in cfg:
            cur[k] = cfg[k]
    if "targets" in cfg and isinstance(cfg["targets"], dict):
        for tk in _DEFAULT_CONFIG["targets"]:
            if tk in cfg["targets"]:
                cur["targets"][tk] = bool(cfg["targets"][tk])
    if "wiki" in cfg and isinstance(cfg["wiki"], dict):
        for wk in _DEFAULT_CONFIG["wiki"]:
            if wk in cfg["wiki"]:
                cur["wiki"][wk] = bool(cfg["wiki"][wk])
    return _write_config(uid, cur)


def _collect_evidence(uid: str, window: int = 20) -> dict:
    """收集用户最近 window 次派活的证据(用于反思器·不是自动加权)。"""
    try:
        import sessions
        recent = sessions.list_sessions(limit=window, user_id=uid)
    except Exception:
        return {"sessions": [], "harness_usage": {}, "count": 0}
    harness_usage: dict[str, int] = {}
    session_summaries = []
    for s in recent:
        try:
            d = sessions.load_session(s["sid"], user_id=uid) or {}
        except Exception:
            continue
        turn_harnesses = []
        for t in d.get("turns", []):
            r = t.get("result") if isinstance(t.get("result"), dict) else t
            for x in (r.get("results") or []):
                if isinstance(x, dict) and (x.get("key") or x.get("harness")):
                    k = x.get("key") or x.get("harness")
                    harness_usage[k] = harness_usage.get(k, 0) + 1
                    turn_harnesses.append(k)
            if isinstance(r.get("winner"), dict) and r["winner"].get("key"):
                w = r["winner"]["key"]
                harness_usage[w] = harness_usage.get(w, 0) + 1
                turn_harnesses.append(w)
        session_summaries.append({
            "title": (d.get("turns", [{}])[0] or {}).get("question", "")[:80],
            "harnesses_used": turn_harnesses,
            "turns": len(d.get("turns", [])),
        })
    return {"sessions": session_summaries, "harness_usage": harness_usage,
            "count": len(session_summaries)}


def run_evolution(uid: str) -> dict:
    """执行一次进化:读证据 → 反思产出新 pref → 存 diff(不自动应用·需用户审)。"""
    cfg = load_config(uid)
    if not cfg["enabled"]:
        return {"ok": False, "error": "自进化未启用·先在面板打开总开关"}
    active_targets = [k for k, v in cfg["targets"].items() if v]
    if not active_targets:
        return {"ok": False, "error": "没勾选任何进化目标·先在面板选择要进化什么"}

    evidence = _collect_evidence(uid, cfg.get("evidence_window", 20))
    if evidence["count"] == 0:
        return {"ok": False, "error": "无派活历史·先派几次活积累证据(切身份不是default后)"}

    # ── WikiSkill 闭环:进化前先把经验沉淀进持久 wiki(经验先沉淀再复用·论文命门)──
    wiki_step = None
    wcfg = cfg.get("wiki") or {}
    if wcfg.get("enabled") and wcfg.get("consolidate"):
        try:
            import skill_wiki
            wiki_step = skill_wiki.consolidate(uid, cfg.get("evidence_window", 20),
                                               cfg.get("reflector", "auto"))
        except Exception as e:
            wiki_step = {"ok": False, "error": str(e)}

    # 先把证据事实结构化(统计层·始终兜底)。用户2026-09-01 推翻"不做自进化"老约束:
    # 在此之上再【真派带进化能力的家(penguin/prime)读全上下文做深度反思】(见下 reflection 步)。
    before = dict(cfg.get("active_pref") or {})
    after: dict[str, Any] = {}
    for t in active_targets:
        if t == "harness_pref":
            # 事实:你最近这些家用得多(不加权·只呈现事实·让用户自己看要不要固化)
            top = sorted(evidence["harness_usage"].items(), key=lambda x: -x[1])[:5]
            after["harness_pref"] = {
                "top_used": [{"key": k, "count": c} for k, c in top],
                "hint_for_captain": f"用户最近 {evidence['count']} 次派活里最常用: " +
                                    ", ".join(f"{k}({c})" for k, c in top[:3]),
            }
        elif t == "captain_bias":
            top1 = max(evidence["harness_usage"].items(), key=lambda x: x[1], default=("", 0))
            after["captain_bias"] = {
                "note": f"你最近最常派 {top1[0]}({top1[1]} 次)·可考虑设为首脑偏好(不强制)",
                "suggested_captain": top1[0] if top1[1] >= 3 else "",
            }
        elif t == "workflow_snippets":
            titles = [s["title"] for s in evidence["sessions"] if s["title"]][:10]
            after["workflow_snippets"] = {
                "recent_task_types": titles,
                "note": "最近你做的任务类型·可基于这些归纳快捷指令(手动)",
            }

    # ── Skill Proposer:从持久 wiki 提炼可复用 skill 挂进 after(论文:wiki 喂提炼步)──
    wiki_skills = None
    if wcfg.get("enabled") and wcfg.get("propose"):
        try:
            import skill_wiki
            prop = skill_wiki.propose_skills(uid)
            if prop.get("ok"):
                wiki_skills = prop
                after["wiki_skills"] = {
                    "proposed_skills": prop.get("proposed_skills", []),
                    "watch_failures": prop.get("watch_failures", []),
                    "note": "从持久 wiki 提炼的可复用做法(经验先沉淀再复用·WikiSkill)",
                }
        except Exception:
            pass

    # ── 真派进化家深度反思(用户2026-09-01·推翻"不做自进化"·context not control)──
    # 把【全上下文:历史统计事实 + 最近任务 + 当前 pref】给带进化能力的家(penguin/prime),
    # 让它自己反思"该怎么进化"——不替它写建议(那是 control),只给它看清全貌(那是 context)。
    # 失败不阻断:统计层 after 已兜底,深度反思只是【锦上添花的可选增强】。
    reflector = cfg.get("reflector", "penguin")
    reflection = None
    try:
        import legion_core
        titles = [s["title"] for s in evidence["sessions"] if s.get("title")][:12]
        _ctx = {
            "harness_usage": evidence.get("harness_usage", {}),
            "recent_tasks": titles,
            "current_pref": before,
            "statistical_facts": after,
        }
        _prompt = (
            "你是军团里带自进化能力的成员。这是某用户最近 "
            f"{evidence['count']} 次派活的全貌(统计事实/最近任务/当前偏好),"
            "请你【从中学习】,给出让军团下次为这个用户做得更好的进化建议:"
            "哪家该优先带队、哪类任务该用哪套打法、有什么可复用的经验。"
            "只输出你的反思结论(说人话·给用户看),别复述数据。现场:"
            + json.dumps(_ctx, ensure_ascii=False)
        )
        r = legion_core.run_one(reflector, _prompt, f"evolve_reflect_{int(time.time())}",
                                lambda ev: None)
        if isinstance(r, dict) and r.get("final") and len(str(r["final"]).strip()) > 40:
            reflection = {"by": reflector, "insight": str(r["final"]).strip()[:4000]}
            after["deep_reflection"] = reflection   # 挂进 after·用户在面板可见
    except Exception as e:
        reflection = {"by": reflector, "error": str(e)[:200]}

    ts = int(time.time())
    evolution_id = f"{ts}"
    diff = {"before": before, "after": after, "targets": active_targets,
            "evidence_count": evidence["count"], "reflector": reflector,
            "ts": ts, "evolution_id": evolution_id,
            "deep_reflection": reflection,   # 进化家真反思(可溯源·可能为空)
            "wiki_step": wiki_step}          # WikiSkill 沉淀这次做了什么(可溯源)
    try:
        with _storage_dir(uid, evolution=True, create=True) as directory:
            with _json_file(directory, f"{evolution_id}.json", write=True) as stream:
                json.dump(diff, stream, ensure_ascii=False, indent=2)
    except Exception as e:
        return {"ok": False, "reason": "evolution_write_failed", "error": str(e),
                "evolution_id": evolution_id, "diff": diff}

    result = {"ok": True, "evolution_id": evolution_id, "diff": diff,
              "wiki_step": wiki_step, "wiki_skills": wiki_skills,
              "auto_applied": False, "note": "已产出 diff·在面板点[应用]才生效"}
    if cfg.get("auto_apply"):
        applied = apply_evolution(uid, evolution_id)
        result["apply_result"] = applied
        result["auto_applied"] = bool(applied.get("ok"))
        result["note"] = ("已自动应用(auto_apply=True)·仍可回滚" if result["auto_applied"]
                          else "已产出 diff·自动应用未成功，详见 apply_result")
    return result


# ── WikiSkill 三层知识架构入口(用户 2026-08-31·默认每用户开)──────────────
# 依据 Google Research WikiSkill 论文(见 dev/wikiskill-design-2026-08-31.md)。
# 「经验先沉淀进持久 wiki·再复用」——run_evolution 在 wiki 开启时先 consolidate 沉淀经验,
# 再 propose 从 wiki 提炼可复用 skill,让进化建立在累积知识上而非用一次就丢。

def wiki_consolidate(uid: str) -> dict:
    """Wiki Maintainer:把该用户 recent 经验沉淀进持久 wiki(默认开·后台无感积累)。"""
    cfg = load_config(uid)
    w = cfg.get("wiki") or {}
    if not (w.get("enabled") and w.get("consolidate")):
        return {"ok": False, "error": "wiki 沉淀未启用(在自进化面板可开)"}
    try:
        import skill_wiki
    except Exception as e:
        return {"ok": False, "error": f"skill_wiki 不可用:{e}"}
    return skill_wiki.consolidate(uid, cfg.get("evidence_window", 20),
                                  cfg.get("reflector", "auto"))


def wiki_view(uid: str) -> dict:
    """看某用户的持久 wiki(index/log/skill-impact/patterns)·用户「可查看」。"""
    try:
        import skill_wiki
    except Exception as e:
        return {"ok": False, "error": f"skill_wiki 不可用:{e}"}
    return {"ok": True, "wiki": skill_wiki.read_wiki(uid)}


def wiki_pattern(uid: str, name: str) -> dict:
    try:
        import skill_wiki
    except Exception as e:
        return {"ok": False, "error": f"skill_wiki 不可用:{e}"}
    return skill_wiki.read_pattern(uid, name)


def list_evolutions(uid: str, limit: int = 20) -> dict:
    """列出该用户所有进化记录(倒序·最新在前)。用户面板看历史。"""
    rows = []
    cfg = load_config(uid)
    active_id = cfg.get("active_evolution_id", "")
    try:
        with _storage_dir(uid, evolution=True) as directory:
            files = sorted([f for f in os.listdir(directory)
                            if f.endswith(".json") and _valid_evolution_id(f[:-5])], reverse=True)
            for fn in files[:limit]:
                try:
                    with _json_file(directory, fn) as stream:
                        ev = json.load(stream)
                    rows.append({
                        "id": ev.get("evolution_id"),
                        "ts": ev.get("ts"),
                        "targets": ev.get("targets"),
                        "evidence_count": ev.get("evidence_count"),
                        "reflector": ev.get("reflector"),
                        "is_active": ev.get("evolution_id") == active_id,
                    })
                except Exception:
                    continue
    except FileNotFoundError:
        return {"ok": True, "evolutions": [], "count": 0}
    except Exception as e:
        return {"ok": False, "evolutions": [], "count": 0,
                "reason": "evolution_unreadable", "error": str(e)}
    return {"ok": True, "evolutions": rows, "count": len(rows), "active": active_id}


def get_evolution(uid: str, evolution_id: str) -> dict:
    """看某次进化完整 diff(用户点某条进化记录时)。"""
    if not _valid_evolution_id(evolution_id):
        return {"ok": False, "reason": "invalid_evolution_id", "error": "invalid evolution id"}
    try:
        with _storage_dir(uid, evolution=True) as directory:
            with _json_file(directory, f"{evolution_id}.json") as stream:
                evolution = json.load(stream)
        if not isinstance(evolution, dict):
            raise ValueError("evolution record is not an object")
        return {"ok": True, "evolution": evolution}
    except FileNotFoundError:
        return {"ok": False, "error": "evolution not found"}
    except Exception as e:
        return {"ok": False, "reason": "evolution_unreadable", "error": str(e)}


def apply_evolution(uid: str, evolution_id: str) -> dict:
    """把某次进化的 after 应用为 active_pref。用户面板点"应用"触发。"""
    ev = get_evolution(uid, evolution_id)
    if not ev.get("ok"):
        return ev
    cfg = load_config(uid)
    cfg["active_pref"] = ev["evolution"].get("after") or {}
    cfg["active_evolution_id"] = evolution_id
    saved = _write_config(uid, cfg)
    if not saved["ok"]:
        return saved
    return {"ok": True, "evolution_id": evolution_id, "active_pref": cfg["active_pref"]}


def rollback(uid: str) -> dict:
    """回滚:清空 active_pref(退回未进化状态)。用户面板点"回滚"触发。"""
    cfg = load_config(uid)
    cfg["active_pref"] = {}
    cfg["active_evolution_id"] = ""
    saved = _write_config(uid, cfg)
    if not saved["ok"]:
        return saved
    return {"ok": True, "note": "已回滚到未进化状态"}


def apply_active_pref(brief: str | dict, uid: str) -> str | dict:
    """简报总装点调用：保留字符串/字典格式，把已手动应用的 active_pref 交给当前用户。
    - 未启用/无 active_pref → brief 不变
    - 有 active_pref → brief["user_evolution"] = {...}·harness 派活时能看到用户进化后的偏好
    诚实边界:只是"用户主动告诉军团的偏好"·不覆盖 auto 竞选·harness 可选择性参考。"""
    brief = dict(brief) if isinstance(brief, dict) else str(brief or "")
    if not uid:
        return brief
    cfg = load_config(uid)
    if not cfg.get("enabled"):
        return brief
    active = cfg.get("active_pref") or {}
    if not active:
        return brief
    preference = {"pref": active, "evolution_id": cfg.get("active_evolution_id"),
                  "note": "用户主动进化的偏好·可参考但不强制"}
    if isinstance(brief, str):
        return brief + "\n\nUSER_EVOLUTION_JSON\n" + json.dumps(preference, ensure_ascii=False)
    brief["user_evolution"] = preference
    return brief


if __name__ == "__main__":
    import sys
    os.environ.setdefault("MEYO_LEGION_DIR", os.path.expanduser("~/.meyo/legion"))
    uid = sys.argv[1] if len(sys.argv) > 1 else "default"
    print("=== 用户", uid, "自进化状态 ===")
    print(json.dumps(load_config(uid), ensure_ascii=False, indent=2))
