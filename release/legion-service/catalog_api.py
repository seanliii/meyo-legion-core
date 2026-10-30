#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""catalog_api:统一目录(用户2026-08-30:harness届的OpenRouter·skill/工具/函数分类都让人看得到).

产品定位:OpenRouter 是"一个入口接所有 LLM";我们是"一个入口接所有 harness/agent"·且多三层:
①harness互相学习进化 ②共享函数和工具 ③因人而异(每个用户不一样)。

本模块把军团的可用资源聚成一个【可浏览目录】给前端(像 OpenRouter 的模型目录):
- agents  : 三类(通用Agent能派/平台端调军团/能力库调函数)——来自 agent_adapters。
- skills  : 全仓1908个各家skill + 外部三源·按 family 分类——来自 skill_factory。
- functions: 全仓8万+公开函数/工具·按 family+语言+tier(声明工具/普通函数)分类——来自 func_index。
- tools   : 军团已接通可执行工具·按 capability 分类——来自 toolbox。

全部只读聚合·带分类计数·前端据此做分类浏览/筛选/搜索(不塞全量·给分类概览+按需查明细)。
"""
from __future__ import annotations

import json
import os
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor

try:
    import legion_paths as _paths
    _SNAPSHOT = _paths.under("catalog_summary.json")
except Exception:
    _SNAPSHOT = os.path.expanduser("~/.meyo/legion/catalog_summary.json")

def _agents_section() -> dict:
    try:
        import agent_adapters
        o = agent_adapters.overview()
        # 每家首脑适任度(竞技场真数据·auto36垫底/jcode89高)→前端据此决定谁能"优先当首脑"·
        # 不给弱首脑(如auto)优先当首脑的入口(用户2026-08-30:auto之前定义不让当首脑)。
        apt = {}
        try:
            import captain_aptitude
            apt = captain_aptitude.aptitude_table() or {}
        except Exception:
            apt = {}
        def _apt_score(k):
            row = apt.get(k)
            return round(row["score"], 1) if isinstance(row, dict) and row.get("score") is not None else None
        return {
            "summary": o["summary"],
            "classes": {
                "answer_agent": {"label": "通用 Agent(能派活;编排模式下按适任度竞选首脑·非固定·弱首脑不优先)", "count": len(o["ready"]),
                                 "items": [{"agent": r["agent"], "captain_aptitude": _apt_score(r["agent"])} for r in o["ready"]]},
                "platform_client": {"label": "平台/端(作为端调军团)", "count": len(o.get("platform_role", [])),
                                    "items": [{"agent": r["agent"], "role": r.get("role")} for r in o.get("platform_role", [])]},
                "capability_lib": {"label": "能力库(func_call 调函数)", "count": len(o["capability_lib"]),
                                   "items": [{"agent": r["agent"], "functions": r["functions"]} for r in o["capability_lib"]]},
            },
            "note": o.get("note"),
        }
    except Exception as e:
        return {"error": f"agents: {e}"}


def _functions_section(sample_per_family: int = 0) -> dict:
    """全仓公开函数/工具分类概览(按 family/语言/tier 计数·不塞8万条明细)。"""
    try:
        import func_index
        idx = func_index.build()
        by_family, by_lang, by_tier = {}, {}, {"tool": 0, "function": 0}
        for e in idx["entries"]:
            by_family[e["family"]] = by_family.get(e["family"], 0) + 1
            lg = e.get("lang", "?")
            by_lang[lg] = by_lang.get(lg, 0) + 1
            by_tier[e["tier"]] = by_tier.get(e["tier"], 0) + 1
        return {
            "total": idx["total"], "declared_tools": by_tier.get("tool", 0),
            "public_functions": by_tier.get("function", 0),
            "directly_callable": None,
            "direct_candidates": idx.get("direct_candidates", 0),
            "callability_verified": False,
            "by_family": dict(sorted(by_family.items(), key=lambda x: -x[1])),
            "by_language": dict(sorted(by_lang.items(), key=lambda x: -x[1])),
            "search_hint": "明细按需查:run runnable:func_search {\"q\":\"能力\"} —— 不在目录里塞8万条",
        }
    except Exception as e:
        return {"error": f"functions: {e}"}


def _skills_section() -> dict:
    """全仓各家 skill 分类概览(按 family 计数)。"""
    try:
        import skill_factory
        rows = skill_factory._scan_repo_skills()
        by_family = {}
        for r in rows:
            by_family[r["family"]] = by_family.get(r["family"], 0) + 1
        return {
            "total_repo_skills": len(rows),
            "by_family": dict(sorted(by_family.items(), key=lambda x: -x[1])),
            "external_sources": ["meyo", "skills.sh", "skillfish"],
            "search_hint": "查/装:run runnable:skill_finder {\"query\":\"能力\"} —— 本仓现成优先·外部按需装",
        }
    except Exception as e:
        return {"error": f"skills: {e}"}


def _tools_section() -> dict:
    """军团已接通可执行工具·按 capability 分类。"""
    try:
        import toolbox
        # 完整工具箱由 server 预热先构建；目录聚合复用快照，避免再同步全仓扫描 45s。
        # 若仍在冷启动，scan_toolbox 会先给核心武器并单飞后台补齐，不阻塞用户线程。
        d = toolbox.scan_toolbox()
        by_cap = d.get("summary", {}).get("by_capability") or {}
        runnable = sum(1 for it in d.get("items", []) if it.get("runnable"))
        return {
            "total": d["summary"]["total"],
            "runnable": runnable,  # toolbox【已接通】的可执行工具(有真实现/桥)
            "note": "runnable 表示已有执行入口及当前已知就绪状态，不代表某次调用成功；functions.direct_candidates 是未经执行验证的源码候选",
            "by_capability": dict(sorted(by_cap.items(), key=lambda x: -x[1])[:30]) if by_cap else {},
            "search_hint": "查/跑:run runnable:func_call 或 legion_tool search '能力'",
            "warming": bool(d.get("warming")), "partial": bool(d.get("partial")),
        }
    except Exception as e:
        return {"error": f"tools: {e}"}


_CATALOG_TTL = 300.0
_CATALOG_SCHEMA = 2
_CATALOG_CACHE = {"at": 0.0, "data": None, "stamp": None}
_CATALOG_LOCK = threading.RLock()
_CATALOG_REFRESHING = False


def _read_snapshot() -> dict | None:
    try:
        stat = os.stat(_SNAPSHOT)
        stamp = (os.path.abspath(_SNAPSHOT), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None
    with _CATALOG_LOCK:
        if (isinstance(_CATALOG_CACHE["data"], dict)
                and _CATALOG_CACHE["data"].get("schema") == _CATALOG_SCHEMA
                and _CATALOG_CACHE["stamp"] == stamp):
            return _CATALOG_CACHE["data"]
    try:
        with open(_SNAPSHOT, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("schema") != _CATALOG_SCHEMA:
        return None
    with _CATALOG_LOCK:
        _CATALOG_CACHE.update({"at": float(data.get("updated_at") or stat.st_mtime),
                               "data": data, "stamp": stamp})
    return data


def _build_catalog() -> dict:
    started = _time.perf_counter()
    builders = (_agents_section, _skills_section, _functions_section, _tools_section)
    with ThreadPoolExecutor(max_workers=len(builders), thread_name_prefix="catalog") as pool:
        agents, skills, functions, tools = [future.result() for future in
                                            [pool.submit(builder) for builder in builders]]
    # functions 全量索引通常比 toolbox 后台刷新更慢；四路汇合后再读一次，尽量把刚完成的
    # 完整工具快照纳入本次 catalog，避免把 partial 结果固化进 5 分钟快照。
    if tools.get("partial"):
        refreshed_tools = _tools_section()
        if not refreshed_tools.get("partial"):
            tools = refreshed_tools
    partial = bool(tools.get("partial"))
    out = {
        "ok": True, "schema": _CATALOG_SCHEMA,
        "positioning": "harness届的OpenRouter:一个入口接所有agent·且互相学习进化+共享函数工具+因人而异",
        "agents": agents, "skills": skills, "functions": functions, "tools": tools,
        "updated_at": _time.time(),
        "built_ms": round((_time.perf_counter() - started) * 1000, 1),
        "warming": partial, "partial": partial,
    }
    try:
        os.makedirs(os.path.dirname(_SNAPSHOT), exist_ok=True)
        tmp = f"{_SNAPSHOT}.tmp.{os.getpid()}.{threading.get_ident()}"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(out, handle, ensure_ascii=False)
        os.replace(tmp, _SNAPSHOT)
        stat = os.stat(_SNAPSHOT)
        stamp = (os.path.abspath(_SNAPSHOT), stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = None
    with _CATALOG_LOCK:
        _CATALOG_CACHE.update({"at": out["updated_at"], "data": out, "stamp": stamp})
    return out


def _refresh_catalog() -> None:
    global _CATALOG_REFRESHING
    try:
        _build_catalog()
    except Exception:
        # 后台重建失败时旧快照仍是可用事实；释放 single-flight，后续 stale 请求可重试。
        pass
    finally:
        with _CATALOG_LOCK:
            _CATALOG_REFRESHING = False


def _schedule_refresh() -> None:
    global _CATALOG_REFRESHING
    with _CATALOG_LOCK:
        if _CATALOG_REFRESHING:
            return
        _CATALOG_REFRESHING = True
    threading.Thread(target=_refresh_catalog, daemon=True, name="catalog-refresh").start()


def catalog(force: bool = False, query: str = "", top: int = 8) -> dict:
    """统一目录摘要；冷请求只读快照并后台重建，绝不让用户线程全仓扫描。"""
    q = str(query or "").strip()
    if q:
        try:
            import legion_tool
            result = legion_tool.search(q, top=max(1, min(int(top), 30)))
            return {"ok": True, "query": q, "search": result,
                    "positioning": "与全部 harness 共用同一武器搜索、排序和学习结果"}
        except Exception as e:
            return {"ok": False, "query": q, "error": f"search: {type(e).__name__}: {e}"}
    if force:
        return {**_build_catalog(), "from_cache": False, "stale": False, "warming": False}
    cached = _read_snapshot()
    if cached is not None:
        age = _time.time() - float(cached.get("updated_at") or _CATALOG_CACHE["at"])
        stale = age >= _CATALOG_TTL
        if stale:
            _schedule_refresh()
        return {**cached, "from_cache": True, "stale": stale, "warming": stale}
    _schedule_refresh()
    return {
        "ok": True, "schema": _CATALOG_SCHEMA, "warming": True, "stale": False, "from_cache": False,
        "positioning": "目录正在后台建立；常用能力仍可直接派活，稍后会自动显示完整统计",
        "agents": {"summary": {}, "classes": {}}, "skills": {"total_repo_skills": 0, "by_family": {}},
        "functions": {"total": 0, "declared_tools": 0, "public_functions": 0,
                      "directly_callable": None, "direct_candidates": 0, "callability_verified": False,
                      "by_family": {}, "by_language": {}},
        "tools": {"total": 0, "runnable": 0, "by_capability": {}},
    }


if __name__ == "__main__":
    import json, os
    os.environ.setdefault("MEYO_LEGION_DIR", os.path.expanduser("~/.meyo/legion"))
    c = catalog()
    print("=== 军团统一目录(harness届OpenRouter) ===")
    a = c["agents"]["summary"]
    print(f"Agents: 通用{a['ready']} · 平台端{a.get('platform_role',0)} · 能力库{a['capability_lib']}")
    print(f"Skills: 本仓 {c['skills']['total_repo_skills']} · 分{len(c['skills']['by_family'])}家")
    print(f"Functions: {c['functions']['total']}(声明工具{c['functions']['declared_tools']}+函数{c['functions']['public_functions']})· {c['functions']['by_language']}")
    print(f"Tools: {c['tools']['total']}(可执行{c['tools']['runnable']})")
