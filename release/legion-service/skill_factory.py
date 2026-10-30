#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""军团共享 Skill 工厂：多源发现、安全审计、用户隔离安装与使用账本。"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from typing import Any

import legion_paths as _paths

ROOT = pathlib.Path(_paths.under("users"))
_ALLOWED_HOSTS = {"www.meyo.life", "github.com", "api.github.com", "skills.sh", "skill.fish"}
_DANGEROUS = ("os.system", "subprocess.Popen", "subprocess.run", "eval(", "exec(", "shutil.rmtree")


def _safe(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or "skill")).strip("-.")[:100]


def user_root(uid: str = "default") -> pathlib.Path:
    return ROOT / _safe(uid or "default") / "skills"


def _ledger(uid: str, event: dict) -> None:
    root = user_root(uid)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "factory.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": time.time(), **event}, ensure_ascii=False) + "\n")


def _run_json(command: list[str], timeout: int = 90) -> Any:
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        return {"ok": False, "error": (result.stderr or result.stdout)[-1000:]}
    try:
        return json.loads(result.stdout)
    except Exception:
        return {"ok": True, "raw": result.stdout[-8000:]}


def _search_meyo(query: str, limit: int) -> list[dict]:
    script = pathlib.Path.home() / ".claude/skills/deep-skill-finder/scripts/deep_skill_search.py"
    if not script.is_file():
        return []
    result = subprocess.run([os.environ.get("PYTHON", "python3.12"), str(script), query, "--json"],
                            capture_output=True, text=True, timeout=90)
    if result.returncode != 0:
        return []
    try:
        payload = json.loads(result.stdout)
    except Exception:
        return []
    rows = payload if isinstance(payload, list) else payload.get("results") or payload.get("skills") or []
    return [{"name": row.get("name"), "description": row.get("reason") or row.get("description", ""),
             "source": "meyo", "raw": row} for row in rows[:limit] if row.get("name")]


def _search_cli(command: list[str], source: str, limit: int) -> list[dict]:
    result = subprocess.run(command, capture_output=True, text=True, timeout=90)
    text = (result.stdout or "") + "\n" + (result.stderr or "")
    rows = []
    for line in text.splitlines():
        match = re.search(r"([\w.-]+/[\w.-]+)@([\w.-]+)", line)
        if match:
            repo = match.group(1)
            skill_name = match.group(2)
            rows.append({"name": skill_name, "repo": repo, "skill": skill_name,
                         "description": line.strip()[:500], "source": source})
            continue
        match = re.search(r"(?:https?://)?skills\.sh/([\w.-]+/[\w.-]+)/([\w.-]+)", line)
        if match:
            repo = match.group(1)
            skill_name = match.group(2)
            rows.append({"name": skill_name, "repo": repo, "skill": skill_name,
                         "description": line.strip()[:500], "source": source})
            continue
        match = re.search(r"([\w.-]+/[\w.-]+)", line)
        if match and match.group(1) not in ("owner/repo", "skills.sh/"):
            repo = match.group(1).strip(".,:;()[]")
            rows.append({"name": repo.rsplit("/", 1)[-1], "repo": repo,
                         "description": line.strip()[:500], "source": source})
    seen = set()
    return [row for row in rows if not (row["repo"] in seen or seen.add(row["repo"]))][:limit]


# ★全仓各家自己的 skill 共享(用户2026-08-30:各家自己的skill也要能共享出来)。
# 之前 search 只搜外部三源(meyo/skills.sh/skillfish)·全仓 1908 个 SKILL.md(penguin/gstack/
# hermes/各vendored)完全没共享→军团看不到别家已有skill。这里扫全仓·并入 search 且【优先】
# (本仓现成 skill 不用装·比外部要 npx 装的更该先给)。缓存6h。
_REPO_SKILL = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
_REPO_SKILL_CACHE = {"at": 0.0, "rows": None, "warming": False}
_REPO_SKILL_LOCK = __import__("threading").RLock()
_SCAN_STOP = __import__("threading").Event()


def stop_scans():
    """Abort background discovery at shutdown without publishing partial cache."""
    _SCAN_STOP.set()


def _skill_files(root):
    for directory, dirs, files in os.walk(root, followlinks=False):
        if _SCAN_STOP.is_set():
            return
        dirs[:] = [name for name in dirs if name not in {"node_modules", ".venv", "venv", ".git"}]
        if "SKILL.md" in files:
            yield pathlib.Path(directory) / "SKILL.md"


def _skill_roots_stamp():
    import toolbox
    return (_REPO_SKILL, os.environ.get("LEGION_EXTERNAL_SOURCE_ROOT", ""),
            json.dumps(toolbox.skill_source_identity(_REPO_SKILL), sort_keys=True))


def _scan_repo_skills() -> list:
    """扫全仓所有 SKILL.md=各家自己声明的 skill·共享给全军团(现成·免装)。"""
    stamp = _skill_roots_stamp()
    if _REPO_SKILL_CACHE.get("roots") == stamp and _REPO_SKILL_CACHE["rows"] is not None and time.time() - _REPO_SKILL_CACHE["at"] < 6 * 3600:
        return _REPO_SKILL_CACHE["rows"]
    roots = [pathlib.Path(_REPO_SKILL) / base for base in (".claude/skills", ".agents/skills", "harness_baselines", "seed-users-system", "legion/skills", "runtime/core-sources")]
    if stamp[1]:
        roots.append(pathlib.Path(stamp[1]))
    rows = _collect_repo_skills(roots)
    if _SCAN_STOP.is_set():
        return []
    with _REPO_SKILL_LOCK:
        _REPO_SKILL_CACHE.update({"rows": rows, "at": time.time(),
                                 "warming": False, "roots": stamp, "origin": "repo_scan"})
    return rows


def _collect_repo_skills(roots) -> list:
    """Read declared Skill files; callers decide full versus lightweight scope."""
    rows = []
    seen = set()
    for root in roots:
        if not root.is_dir():
            continue
        for md in _skill_files(root):
            if any(part in {"node_modules", ".venv", "venv", ".git"} for part in md.parts) or md.resolve() in seen:
                continue
            if not md.resolve().is_relative_to(root.resolve()):
                continue
            seen.add(md.resolve())
            try:
                head = md.read_text(encoding="utf-8", errors="ignore")[:800]
            except Exception:
                head = ""
            name = md.parent.name
            # 从 SKILL.md 抓一句描述(首个非空非标题行)
            desc = ""
            for line in head.splitlines():
                s = line.strip().lstrip("#").strip()
                if s and not s.startswith(("---", "name:", "description:")):
                    desc = s[:120]
                    break
                if s.startswith("description:"):
                    desc = s.split(":", 1)[-1].strip()[:120]
                    break
            # 归属家:路径里的 harness 名
            rel = str(md.relative_to(_REPO_SKILL)) if md.is_relative_to(_REPO_SKILL) else str(md.resolve())
            fam = "penguin" if "penguin" in rel else ("auto/seed" if "seed-users" in rel
                  else "legion" if rel.startswith((".claude/skills/", ".agents/skills/"))
                  else rel.split(os.sep)[2] if "ecosystem" in rel and len(rel.split(os.sep)) > 2 else "repo")
            rows.append({"name": name, "skill": name, "family": fam, "description": desc,
                         "skill_md": rel, "source": "repo", "installed": True})
    return rows


def _warm_repo_skills() -> None:
    try:
        _scan_repo_skills()
    finally:
        with _REPO_SKILL_LOCK:
            _REPO_SKILL_CACHE["warming"] = False


def _toolbox_skill_snapshot() -> list | None:
    try:
        import toolbox
        cached = toolbox.cached_toolbox()
        if (cached and not cached.get("partial")
                and cached.get("skill_sources") == toolbox.skill_source_identity(_REPO_SKILL)):
            return [{"name": item.get("name"), "skill": item.get("name"),
                     "family": item.get("source_harness") or "repo",
                     "description": item.get("description") or "",
                     "skill_md": item.get("path") or "", "source": "repo", "installed": True}
                    for item in (cached.get("items") or []) if item.get("kind") == "skill"]
    except Exception:
        pass
    return None


def _cached_repo_skills() -> tuple[list, bool]:
    stamp = _skill_roots_stamp()
    snapshot = _toolbox_skill_snapshot()
    with _REPO_SKILL_LOCK:
        if _REPO_SKILL_CACHE.get("roots") != stamp:
            _REPO_SKILL_CACHE.update({"rows": None, "at": 0.0, "roots": stamp})
        if snapshot is not None:
            _REPO_SKILL_CACHE.update({"rows": snapshot, "at": time.time(),
                                     "warming": False, "origin": "toolbox"})
            return snapshot, False
        rows = _REPO_SKILL_CACHE["rows"]
        fresh = rows is not None and time.time() - _REPO_SKILL_CACHE["at"] < 6 * 3600
        if fresh and _REPO_SKILL_CACHE.get("origin") == "repo_scan":
            return rows, False
    # A one-shot CLI must return its already installed package Skills before
    # exiting. Do not start a second full-runtime scanner whose result dies
    # with that process. The existing toolbox refresh remains the full-index
    # owner, and its persisted source-bound snapshot is shared by later calls.
    local = _collect_repo_skills([
        pathlib.Path(_REPO_SKILL) / base
        for base in (".claude/skills", ".agents/skills", "legion/skills")
    ])
    import toolbox
    toolbox._schedule_refresh()
    return local, True


def _search_repo_skills(query: str, limit: int) -> tuple[list, bool]:
    terms = [t for t in str(query).lower().replace("_", " ").split() if t]
    scored = []
    rows, warming = _cached_repo_skills()
    for r in rows:
        hay = f"{r['name']} {r['description']} {r['family']} {r['skill_md']}".lower()
        score = sum(hay.count(t) for t in terms)
        if score:
            scored.append((score, r))
    scored.sort(key=lambda p: -p[0])
    return [r for _, r in scored[:limit]], warming


def search(query: str, uid: str = "default", limit: int = 8,
           scope: str = "local") -> dict:
    """搜索 Skill；默认只查本仓/已装，外部源必须由 harness 显式要求。

    ``scope=external`` 只查外部三源；``scope=all`` 同时查本地和外部。父层不根据关键词猜
    “困难”，本地结果是否够用由 harness 自己判断，再决定是否扩大搜索范围。
    """
    rows = []
    warming = False
    mode = str(scope or "local").lower()
    use_local = mode not in ("external", "remote")
    use_external = mode in ("external", "remote", "all")
    if use_local:
        try:
            local_rows, warming = _search_repo_skills(query, limit)
            rows += local_rows
        except Exception:
            pass
    if use_external:
        try:
            rows += _search_meyo(query, limit)
        except Exception:
            pass
        try:
            rows += _search_cli(["npx", "-y", "skills", "find", query], "skills.sh", limit)
        except Exception:
            pass
        try:
            rows += _search_cli(["npx", "-y", "skillfish", "--json", "search", query], "skillfish", limit)
        except Exception:
            pass
    unique = []
    seen = set()
    for row in rows:
        key = (row.get("repo") or row.get("name") or "").lower()
        if key and key not in seen:
            seen.add(key); unique.append(row)
    sources = (["repo(各家自己的skill·现成免装)"] if use_local else [])
    if use_external:
        sources += ["meyo", "skills.sh", "skillfish"]
    _ledger(uid, {"event": "search", "query": query, "count": len(unique), "scope": mode})
    return {"ok": True, "query": query, "scope": mode, "results": unique[:limit],
            "sources": sources, "warming": warming,
            "next": ({"scope": "local", "reason": "先返回本包可用候选；完整来源索引由现有工具箱后台刷新，尚不能把未命中当没有技能"}
                     if use_local and warming else
                     {"scope": "external", "reason": "本地候选由你判断；不足时再显式查外部三源"}
                     if use_local and not use_external else None)}


def audit_directory(path: str) -> dict:
    root = pathlib.Path(path).resolve()
    findings = []
    files = []
    for item in root.rglob("*"):
        if not item.is_file():
            continue
        rel = item.relative_to(root).as_posix()
        files.append(rel)
        if item.stat().st_size > 20 * 1024 * 1024:
            findings.append({"severity": "high", "file": rel, "issue": "file_too_large"})
        if item.suffix == ".py":
            text = item.read_text(encoding="utf-8", errors="replace")
            try: ast.parse(text)
            except SyntaxError: findings.append({"severity": "medium", "file": rel, "issue": "invalid_python"})
            for token in _DANGEROUS:
                if token in text:
                    findings.append({"severity": "medium", "file": rel, "issue": token})
    if "SKILL.md" not in files:
        findings.append({"severity": "high", "file": "", "issue": "missing_SKILL.md"})
    high = any(row["severity"] == "high" for row in findings)
    return {"ok": not high, "path": str(root), "files": files, "findings": findings,
            "risk": "high" if high else "review" if findings else "low"}


def _safe_extract(archive: pathlib.Path, destination: pathlib.Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        base = destination.resolve()
        for member in bundle.infolist():
            target = (destination / member.filename).resolve()
            if not target.is_relative_to(base):
                raise ValueError("zip path traversal")
        bundle.extractall(destination)


def install_local(source: str, name: str, uid: str = "default", revision: str = "local",
                  confirm: bool = False) -> dict:
    # 空 source 直接拒:否则 Path("") 会被解析成仓库根→audit_directory 对全仓 rglob→挂 20s+。
    if not str(source or "").strip():
        return {"ok": False, "status": "bad_source", "error": "source 不能为空(需 SKILL.md 路径或目录)"}
    # search() 返回的 skill_md 是【相对仓库根的 SKILL.md 文件路径】,不是目录:
    # ①相对路径按仓库根解析(不按 CWD·避免换目录就失效)②指向 SKILL.md 文件时取其父目录去 audit/装。
    raw = pathlib.Path(source)
    if not raw.is_absolute():
        cand = pathlib.Path(_REPO_SKILL) / source
        raw = cand if cand.exists() else raw
    src = raw.resolve()
    if src.is_file() and src.name.upper() == "SKILL.MD":
        src = src.parent
    audit = audit_directory(str(src))
    if not confirm:
        return {"ok": audit["ok"], "status": "planned", "audit": audit,
                "destination": str(user_root(uid) / _safe(name) / _safe(revision))}
    if not audit["ok"]:
        return {"ok": False, "status": "audit_failed", "audit": audit}
    destination = user_root(uid) / _safe(name) / _safe(revision)
    active = user_root(uid) / "active" / _safe(name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    active.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists(): shutil.rmtree(destination)
    shutil.copytree(src, destination)
    tmp = active.with_name(active.name + ".tmp")
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(destination)
    os.replace(tmp, active)
    _ledger(uid, {"event": "install", "name": name, "revision": revision,
                  "destination": str(destination), "risk": audit["risk"]})
    return {"ok": True, "status": "installed", "name": name, "revision": revision,
            "active": str(active), "audit": audit}


def list_active(uid: str = "default") -> dict:
    active = user_root(uid) / "active"
    rows = []
    if active.is_dir():
        for path in active.iterdir():
            if path.is_dir() or path.is_symlink():
                rows.append({"name": path.name, "path": str(path.resolve()),
                             "skill_md": str(path.resolve() / "SKILL.md")})
    return {"ok": True, "uid": uid, "skills": rows}


def feedback(name: str, task: str, result: str, agent: str, uid: str = "default",
             rating: str = "", why: str = "") -> dict:
    """记一次 skill 使用反馈。Warp 自进化经验:带【why 的详细反馈】比二元好坏更有价值——
    rating(good/bad/'')+why(为什么好/坏·如'我们代码库这类全局变量命名约定是X')供 improver 聚合。
    人在本来工作的地方低摩擦给·improver 定时拉这些反馈去改进 skill。"""
    _ledger(uid, {"event": "use", "name": name, "task": task[:500], "result": result[:1000],
                  "agent": agent, "rating": str(rating or ""), "why": str(why or "")[:800],
                  "ts": time.time()})
    try:
        import legion_memory
        memory = legion_memory.add(task, f"使用共享 Skill {name}：{result}", source=agent,
                                   tags=["skill", name], mtype="procedural", user_id=uid)
    except Exception as exc:
        memory = {"ok": False, "error": str(exc)}
    return {"ok": True, "memory": memory}
