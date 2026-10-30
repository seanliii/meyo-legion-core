#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机验收中心的只读索引。

跨 uid 聚合只由 server 的 localhost 管理路由调用。本模块不移动、不删除、不改写
session、team、output 或 harness 原生历史；验收备注由 verification_reviews 独立存储。
"""
from __future__ import annotations

import hashlib
import heapq
import ipaddress
import json
import os
import re
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import artifact_delivery as _artifacts
import evidence_schema as _evidence
import legion_paths as _paths

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
_SESSION_CACHE: dict = {"stamp": None, "rows": [], "by_sid": {}, "links": {}}
_NATIVE_CACHE: dict[str, dict] = {}
_NATIVE_CACHE_TTL = 300.0
_EXAM_CACHE: dict = {"stamp": None, "tasks": []}
_CACHE_LOCK = threading.RLock()
_SKIP_DIRS = {".git", ".cache", ".venv", "venv", "node_modules", "__pycache__",
              ".pytest_cache", "target", "dist", "build", "tool-results", "subagents"}
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_SECRET_KEYS = ("api_key", "apikey", "access_token", "refresh_token", "authorization",
                "credential", "password", "secret")
_TEXT_SUFFIXES = {".md", ".txt", ".json", ".jsonl", ".log", ".py", ".js", ".ts",
                  ".html", ".css", ".sql", ".csv", ".yaml", ".yml", ".sh"}
_NATIVE_SPECS = {
    "cc": ("~/.claude/projects", {".jsonl"}, 5),
    "codex": ("~/.codex/sessions", {".jsonl"}, 7),
    "hermes": ("~/.hermes/state.db", {".db"}, 0),
    "jcode": ("~/.jcode/sessions", {".json"}, 2),
    "kimi-code": ("~/.kimi/sessions", {".json", ".jsonl"}, 5),
    "pi": ("~/.pi/agent/sessions", {".jsonl"}, 4),
    "openclaw": ("~/.openclaw/agents/main/sessions", {".jsonl"}, 1),
    "penguin": ("~/.penguin/data", {".jsonl"}, 7),
    "deepseek": ("~/.dsh/sessions", {".jsonl", ".zstd"}, 5),
    "auto": ("@meyo_users", {".json"}, 3),
    "prime": ("@prime_sessions", {".jsonl"}, 2),
}


def _is_loopback(value: str) -> bool:
    value = str(value or "").strip().strip("[]")
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def local_admin_allowed(remote_addr: str, bound_host: str, host_header: str,
                        origin_header: str = "") -> bool:
    """仅允许回环监听上的同机请求；有 Origin 时也必须是回环，阻断网页跨域读取本机历史。"""
    try:
        host = urlsplit("//" + str(host_header or "")).hostname or ""
        origin = urlsplit(str(origin_header or "")).hostname or ""
    except ValueError:
        return False
    origin_ok = not origin_header or _is_loopback(origin)
    return (_is_loopback(remote_addr) and _is_loopback(bound_host)
            and _is_loopback(host) and origin_ok)


def _sessions_dir() -> Path:
    return Path(os.environ.get("LEGION_SESSIONS_DIR") or _paths.home_under("sessions"))


def _teams_dir() -> Path:
    return Path(os.environ.get("LEGION_TEAM_DIR") or _paths.under("teams"))


def _outputs_dir() -> Path:
    return Path(_paths.under("outputs"))


def _mobile_root() -> Path:
    return Path(_paths.under("mobile"))


def _exam_root() -> Path:
    configured = os.environ.get("LEGION_ULTIMATE_EXAM_DIR")
    return Path(configured).expanduser().resolve() if configured else REPO / "dev" / "ultimate-interview-2026-09-03"


def _safe_sid(sid: str) -> str:
    value = str(sid or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", value):
        raise ValueError("invalid session id")
    return value


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _artifact_roots() -> list[tuple[str, Path, str]]:
    users_sibling = Path(os.path.realpath(os.path.join(os.path.dirname(_paths.legion_dir()), "users")))
    return [
        ("outputs", Path(_paths.under("outputs")).resolve(), "/home-outputs/"),
        ("service_outputs", (HERE / "outputs").resolve(), "/outputs/"),
        ("media", Path(_paths.media_dir()).resolve(), "/media/"),
        ("uploads", Path(_paths.uploads_dir()).resolve(), "/media/"),
        ("auto", users_sibling.resolve(), "/auto-outputs/"),
        ("desktop", Path(_paths.under("users")).resolve(), "/desktop-outputs/"),
        ("mobile", _mobile_root().resolve(), "/mobile-evidence/"),
        ("verification", _evidence.default_store_root().resolve(),
         "/api/verification/evidence/file?path="),
    ]


def _path_from_url(url: str) -> Path | None:
    raw = str(url or "")
    fallback = None
    for _name, root, prefix in _artifact_roots():
        if raw.startswith(prefix):
            candidate = (root / unquote(raw[len(prefix):])).resolve()
            if not _inside(candidate, root):
                continue
            fallback = fallback or candidate
            if candidate.exists():
                return candidate
    return fallback


def _safe_artifact_path(raw_path: str = "", raw_url: str = "") -> tuple[str, Path, Path, str] | None:
    candidates = []
    if raw_path:
        candidates.append(Path(os.path.realpath(os.path.expanduser(str(raw_path)))))
    mapped = _path_from_url(raw_url)
    if mapped is not None:
        candidates.append(mapped)
    for candidate in candidates:
        for name, root, prefix in _artifact_roots():
            if _inside(candidate, root):
                return name, candidate, root, prefix
    return None


def _kind(name: str) -> str:
    suffix = Path(str(name or "")).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
        return "image"
    if suffix in {".mp4", ".mov", ".webm"}:
        return "video"
    if suffix in {".mp3", ".wav", ".m4a", ".ogg"}:
        return "audio"
    if suffix in {".xlsx", ".xls", ".csv", ".tsv"}:
        return "spreadsheet"
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".html", ".htm"}:
        return "web"
    if suffix in {".py", ".js", ".ts", ".tsx", ".jsx", ".rs", ".go", ".java"}:
        return "code"
    if suffix in _TEXT_SUFFIXES:
        return "doc"
    return "file"


def _public_artifact(row: dict, sid: str = "", source: str = "") -> dict:
    name = str(row.get("name") or os.path.basename(str(row.get("path") or row.get("url") or "artifact")))
    safe = _safe_artifact_path(str(row.get("path") or ""), str(row.get("url") or ""))
    public = {"name": name, "kind": row.get("kind") or _kind(name),
              "bytes": int(row.get("bytes") or 0), "sid": sid,
              "source": source or row.get("source") or row.get("key") or row.get("harness") or "",
              "available": False, "url": "", "download_url": "", "root": "", "rel": ""}
    if not safe:
        return public
    root_name, path, root, prefix = safe
    rel = path.relative_to(root).as_posix()
    url = prefix + (quote(path.name) if prefix == "/media/" else "/".join(quote(x) for x in rel.split("/")))
    try:
        stat = path.stat()
        public.update({"bytes": stat.st_size, "mtime": stat.st_mtime})
    except OSError:
        pass
    public.update({"available": path.is_file(), "url": url,
                   "download_url": url + ("&" if "?" in url else "?") + "download=1",
                   "root": root_name, "rel": rel})
    if row.get("preview"):
        public["preview"] = str(row.get("preview"))[:1200]
    return public


def _result_block(turn: dict) -> dict:
    return turn.get("result") if isinstance(turn.get("result"), dict) else turn


def _collect_artifacts(session: dict) -> list[dict]:
    sid = str(session.get("sid") or "")
    rows, seen = [], set()
    for turn in session.get("turns") or []:
        block = _result_block(turn)
        groups = [(block.get("discovered_artifacts") or [], "discovered")]
        for member in block.get("results") or []:
            if isinstance(member, dict):
                groups.append((member.get("artifacts") or [], str(member.get("key") or member.get("harness") or "")))
        for artifacts, source in groups:
            for artifact in artifacts:
                if not isinstance(artifact, dict):
                    continue
                public = _public_artifact(artifact, sid=sid, source=source)
                identity = public.get("url") or (public.get("root"), public.get("rel")) or public.get("name")
                identity = str(identity)
                if identity in seen:
                    continue
                seen.add(identity)
                rows.append(public)
    return rows


def _directory_stamp(root: Path) -> tuple[int, int]:
    try:
        return root.stat().st_mtime_ns, sum(1 for p in root.iterdir() if p.suffix == ".json" and not p.name.startswith("."))
    except OSError:
        return 0, 0


def _build_session_cache() -> dict:
    root = _sessions_dir()
    stamp = _directory_stamp(root)
    with _CACHE_LOCK:
        if _SESSION_CACHE["stamp"] == stamp:
            return _SESSION_CACHE
    rows, by_sid, links, bundle_links = [], {}, {}, {}
    if root.is_dir():
        for path in root.iterdir():
            if path.suffix != ".json" or path.name.startswith("."):
                continue
            try:
                session = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            sid = str(session.get("sid") or path.stem)
            file_sid = path.stem
            canonical_sid = sid if _SESSION_ID_RE.fullmatch(sid) and (_sessions_dir() / (sid + ".json")).is_file() else file_sid
            turns = session.get("turns") or []
            modes, harnesses, statuses, search_parts, source_types = set(), set(), [], [], set()
            for turn in turns:
                modes.add(str(turn.get("mode") or ""))
                source_ref = turn.get("source_ref") if isinstance(turn.get("source_ref"), dict) else {}
                source_types.add(str(source_ref.get("type") or "legacy_unknown"))
                search_parts.append(str(turn.get("question") or turn.get("q") or "")[:2000])
                block = _result_block(turn)
                for member in block.get("results") or []:
                    if not isinstance(member, dict):
                        continue
                    key = str(member.get("key") or member.get("harness") or "")
                    if key:
                        harnesses.add(key)
                    statuses.append(str(member.get("status") or ""))
                    search_parts.append(str(member.get("final") or "")[:1200])
            artifacts = _collect_artifacts({**session, "sid": sid})
            for artifact in artifacts:
                key = artifact.get("url") or (artifact.get("root") + ":" + artifact.get("rel", ""))
                if key:
                    links.setdefault(str(key), set()).add(sid)
                if artifact.get("root") == "outputs" and artifact.get("rel"):
                    bundle = str(artifact["rel"]).split("/", 1)[0]
                    bundle_links.setdefault(bundle, set()).add(sid)
            row = {"sid": canonical_sid, "source_sid": sid,
                   "uid": str(session.get("user_id") or "default"),
                   "title": str((turns[0].get("question") if turns else "") or "(空会话)")[:240],
                   "created": float(session.get("created") or 0),
                   "updated": float(session.get("updated") or (path.stat().st_mtime if path.exists() else 0)),
                   "turns": len(turns), "modes": sorted(x for x in modes if x),
                   "source_types": sorted(x for x in source_types if x),
                   "harnesses": sorted(harnesses), "artifact_count": len(artifacts),
                   "status": "failed" if statuses and all(x not in {"done", "completed", "ok"} for x in statuses) else "completed",
                   "_search": "\n".join(search_parts).lower()}
            rows.append(row)
            by_sid[canonical_sid] = row
    rows.sort(key=lambda item: item.get("updated", 0), reverse=True)
    # 旧产物可能未写回 session.artifacts，但统一 workdir 命名中保留 session id。
    # 这是可核验的弱关联，只补展示关系，不改 owner/原 session，也不把它冒充显式引用。
    inferred_bundle_links = {}
    try:
        output_names = [entry.name for entry in os.scandir(_outputs_dir())
                        if not entry.name.startswith(".") and not entry.is_symlink()]
    except OSError:
        output_names = []
    session_ids = [row["sid"] for row in rows if len(row["sid"]) >= 6]
    for output_name in output_names:
        for sid in session_ids:
            start = output_name.find(sid)
            if start < 0:
                continue
            before = output_name[start - 1] if start else "_"
            end = start + len(sid)
            after = output_name[end] if end < len(output_name) else "_"
            before_ok = not start or before in "_-"
            after_ok = end == len(output_name) or after in "_-" or after.isdigit()
            if before_ok and after_ok:
                inferred_bundle_links.setdefault(output_name, set()).add(sid)
                break
    value = {"stamp": stamp, "rows": rows, "by_sid": by_sid,
             "links": links, "bundle_links": bundle_links,
             "inferred_bundle_links": inferred_bundle_links}
    with _CACHE_LOCK:
        _SESSION_CACHE.update(value)
    return _SESSION_CACHE


def _page(items: list, offset: int, limit: int) -> dict:
    offset = max(0, int(offset or 0))
    limit = max(1, min(int(limit or 50), 200))
    return {"items": items[offset:offset + limit], "total": len(items), "offset": offset,
            "limit": limit, "has_more": offset + limit < len(items)}


_EXAM_ORDER = ["codex", "cc", "hermes", "penguin", "deepseek", "jcode", "prime", "auto",
               "deepagents", "omnigent", "headlong", "qwen-code", "kimi-code", "aider", "opencode",
               "goose", "pi", "nanobot", "genericagent", "omniagent", "ironclaw", "cowagent", "cline",
               "openclaw", "gnhf", "openhands", "deer-flow"]
_EXAM_THEMES = [
    ("game", "游戏"), ("bc", "BC错题攻坚"), ("gaia", "GAIA难题"), ("software", "精致软件"),
    ("web", "网页应用"), ("product", "产品系统"), ("desktop", "桌面工具"), ("life", "生活应用"),
    ("visionary", "跨时代构想"), ("novel", "小说"), ("music", "音乐"), ("video", "视频"),
    ("science", "科学实验"), ("fitness", "体育3D健身"), ("stock", "股票交易系统"),
    ("quant", "量化系统"), ("girls", "女生相关"), ("baoma", "宝妈相关"), ("travel", "旅游"),
    ("job", "找工作"), ("money", "赚钱OPC"), ("biz", "发现商机"), ("shop", "商家运营"),
    ("saas", "企业SaaS"), ("intel", "资讯情报军事"), ("history", "历史政治"),
    ("photo", "拍照视频工具"), ("search", "超强搜索反爬"),
]


def _exam_plan() -> list[dict]:
    rows, seq = [], 0
    modes = ("single", "pk", "relay", "orchestrate")
    for index, harness in enumerate(_EXAM_ORDER):
        for mode_index, mode in enumerate(modes):
            seq += 1
            theme_id, theme = _EXAM_THEMES[(index * 3 + mode_index) % len(_EXAM_THEMES)] \
                if mode_index < 3 else ("selfchosen", "自选发散")
            peers = [_EXAM_ORDER[(index + 1) % len(_EXAM_ORDER)]] if mode in {"pk", "relay"} else \
                [_EXAM_ORDER[(index + 1) % len(_EXAM_ORDER)], _EXAM_ORDER[(index + 2) % len(_EXAM_ORDER)]] \
                if mode == "orchestrate" else []
            rows.append({"seq": seq, "key": harness, "theme_id": theme_id, "theme": theme,
                         "mode": mode, "peers": peers, "session_id": f"iview_task_{seq:03d}_{harness}_{theme_id}"})
    return rows


def _exam_stamp(root: Path) -> tuple:
    results = root / "results"
    progress = results / "_progress.log"
    try:
        files = tuple(sorted((path.name, path.stat().st_mtime_ns, path.stat().st_size)
                             for path in results.glob("[0-9][0-9][0-9].json")))
        progress_stamp = (progress.stat().st_mtime_ns, progress.stat().st_size) if progress.is_file() else (0, 0)
        return files, progress_stamp
    except OSError:
        return (), (0, 0)


def _exam_missing(row: dict) -> list[str]:
    gate = row.get("gate") if isinstance(row.get("gate"), dict) else {}
    values = []
    for field in ("missing", "organization_missing", "frontier_missing", "pk_missing"):
        values.extend(gate.get(field) or [])
    iteration = row.get("iteration") if isinstance(row.get("iteration"), dict) else {}
    values.extend(iteration.get("remaining_gaps") or [])
    return sorted(dict.fromkeys(str(value) for value in values if str(value).strip()))


def _exam_status(row: dict, current_rubric: int = 5) -> str:
    if not row:
        return "pending"
    explicit = str(row.get("run_status") or "")
    if explicit in {"queued", "running", "blocked"}:
        return explicit
    if int(row.get("rubric_version") or 0) != current_rubric:
        return "completed_legacy"
    if not row.get("done"):
        return "completed_needs_review"
    if row.get("frontier_proven"):
        return "verified_win"
    if row.get("delivered"):
        return "runnable_unproven"
    return "incomplete"


def _build_exam_cache() -> dict:
    root = _exam_root()
    stamp = _exam_stamp(root)
    with _CACHE_LOCK:
        if _EXAM_CACHE.get("stamp") == stamp:
            return _EXAM_CACHE
    recorded = {}
    for path in (root / "results").glob("[0-9][0-9][0-9].json"):
        value = _read_json_file(path)
        if value:
            recorded[int(value.get("seq") or path.stem)] = value
    tasks = []
    for plan in _exam_plan():
        raw = recorded.get(plan["seq"], {})
        row = {**plan, **raw}
        row["seq"] = plan["seq"]
        row["key"] = str(raw.get("key") or plan["key"])
        row["theme"] = str(raw.get("theme") or plan["theme"])
        row["mode"] = str(raw.get("mode") or plan["mode"])
        row["peers"] = list(raw.get("peers") or plan["peers"])
        row["session_id"] = str(raw.get("session_id") or plan["session_id"])
        row["status"] = _exam_status(raw)
        row["missing"] = _exam_missing(raw)
        row["recorded"] = bool(raw)
        row["title"] = f"第 {plan['seq']} 题 · {row['key']} · {row['theme']}"
        row["updated_at"] = 0.0
        try:
            row["updated_at"] = (root / "results" / f"{plan['seq']:03d}.json").stat().st_mtime
        except OSError:
            pass
        tasks.append(row)
    value = {"stamp": stamp, "tasks": tasks, "root": root}
    with _CACHE_LOCK:
        _EXAM_CACHE.update(value)
    return _EXAM_CACHE


def query_exam_tasks(*, offset: int = 0, limit: int = 50, q: str = "", status: str = "",
                     harness: str = "", mode: str = "") -> dict:
    tasks = _build_exam_cache()["tasks"]
    needle = str(q or "").strip().lower()
    rows = []
    for source in tasks:
        if status == "issues" and source["status"] in {"verified_win", "runnable_unproven"}:
            continue
        if status == "active" and source["status"] not in {"queued", "running"}:
            continue
        if status and status not in {"issues", "active"} and source["status"] != status:
            continue
        if harness and source["key"] != harness:
            continue
        if mode and source["mode"] != mode:
            continue
        searchable = " ".join((source["title"], source["session_id"], " ".join(source["missing"]))).lower()
        if needle and needle not in searchable:
            continue
        rows.append({key: value for key, value in source.items()
                     if key not in {"gate", "member_results", "files", "collected_artifacts"}})
    result = _page(rows, offset, limit)
    counts = {name: sum(row["status"] == name for row in tasks) for name in (
        "pending", "queued", "running", "completed_legacy", "completed_needs_review", "verified_win",
        "runnable_unproven", "incomplete", "blocked")}
    counts["recorded"] = sum(row["recorded"] for row in tasks)
    counts["finished"] = sum(row["status"] in {"completed_legacy", "completed_needs_review",
                                                   "verified_win", "runnable_unproven", "incomplete"}
                             for row in tasks)
    result.update({"ok": True, "counts": counts, "planned": len(tasks)})
    return result


def _exam_artifacts(row: dict, root: Path) -> list[dict]:
    outdir = Path(str(row.get("outdir") or root / "deliverables" / f"{int(row['seq']):03d}_{row['key']}_{row.get('theme_id') or ''}"))
    if not _inside(outdir, root) or not outdir.exists():
        return []
    artifacts, scanned = [], 0
    preferred = {"README.md", "作品报告.md", "MACHINE_EVIDENCE.json", "ITERATION_STATE.json",
                 "FRONTIER_RESEARCH.md", "index.html", "report.html"}
    for path in outdir.rglob("*"):
        if not path.is_file() or any(part in _SKIP_DIRS for part in path.parts):
            continue
        scanned += 1
        if scanned > 5000:
            break
        if path.name not in preferred and path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".pdf", ".mp4"}:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        artifacts.append({"name": path.name, "kind": _kind(path.name), "bytes": stat.st_size,
                          "available": True, "url": "/api/verification/exam/file?seq=" + str(row["seq"])
                          + "&path=" + quote(path.relative_to(outdir).as_posix()),
                          "download_url": "/api/verification/exam/file?seq=" + str(row["seq"])
                          + "&download=1&path=" + quote(path.relative_to(outdir).as_posix()),
                          "source": "终极大题", "bundle_rel": path.relative_to(outdir).as_posix()})
    return sorted(artifacts, key=lambda item: (item["name"] not in preferred, item["bundle_rel"]))[:300]


def exam_task_detail(seq: int) -> dict | None:
    cache = _build_exam_cache()
    row = next((item for item in cache["tasks"] if item["seq"] == int(seq)), None)
    if row is None:
        return None
    iteration = row.get("iteration") if isinstance(row.get("iteration"), dict) else {}
    members = []
    for member in row.get("member_results") or []:
        if not isinstance(member, dict):
            continue
        members.append({"key": member.get("key") or member.get("harness"),
                        "status": member.get("status"), "model": member.get("model") or member.get("model_used"),
                        "final": _redact_text(str(member.get("final") or member.get("error") or ""))[:12000]})
    return {"ok": True, "task": {key: value for key, value in row.items()
                                   if key not in {"member_results", "files", "collected_artifacts"}},
            "members": members, "artifacts": _exam_artifacts(row, cache["root"]),
            "iterations": iteration.get("history") or [], "missing": row["missing"],
            "result_summary": str(row.get("final_head") or "")[:12000]}


def exam_file(seq: int, rel: str) -> Path | None:
    cache = _build_exam_cache()
    row = next((item for item in cache["tasks"] if item["seq"] == int(seq)), None)
    if row is None:
        return None
    outdir = Path(str(row.get("outdir") or "")).resolve()
    target = (outdir / unquote(str(rel or ""))).resolve()
    return target if _inside(outdir, cache["root"]) and _inside(target, outdir) and target.is_file() else None


def query_sessions(*, offset: int = 0, limit: int = 50, q: str = "", uid: str = "",
                   harness: str = "", mode: str = "", source_type: str = "", after: float = 0,
                   before: float = 0, verdict: str = "", reviews: dict | None = None) -> dict:
    rows = _build_session_cache()["rows"]
    needle = str(q or "").strip().lower()
    reviews = reviews or {}
    out = []
    for source in rows:
        review = reviews.get("session:" + source["sid"], {})
        if uid and source["uid"] != uid:
            continue
        if harness and harness not in source["harnesses"]:
            continue
        if mode and mode not in source["modes"]:
            continue
        if source_type and source_type not in source.get("source_types", []):
            continue
        if after and source["updated"] < float(after):
            continue
        if before and source["updated"] > float(before):
            continue
        if verdict and str(review.get("verdict") or "pending") != verdict:
            continue
        if needle and needle not in source["_search"] and needle not in source["sid"].lower() and needle not in source["uid"].lower():
            continue
        row = {k: v for k, v in source.items() if not k.startswith("_")}
        row["review"] = review
        out.append(row)
    result = _page(out, offset, limit)
    counts = {"pending": 0, "pass": 0, "issue": 0}
    for source in rows:
        state = str((reviews.get("session:" + source["sid"]) or {}).get("verdict") or "pending")
        counts[state if state in counts else "pending"] += 1
    result["counts"] = counts
    result["filters"] = {"uids": sorted({x["uid"] for x in rows}),
                         "harnesses": sorted({h for x in rows for h in x["harnesses"]}),
                         "modes": sorted({m for x in rows for m in x["modes"]}),
                         "source_types": sorted({s for x in rows for s in x.get("source_types", [])})}
    return result


def _redact(value, key: str = "", include_raw: bool = False):
    low = str(key or "").lower()
    if any(secret in low for secret in _SECRET_KEYS):
        return "[redacted]"
    if low == "raw" and not include_raw:
        return "[raw available on explicit request]" if value else value
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k), include_raw) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, key, include_raw) for v in value]
    if isinstance(value, str):
        if value.startswith("/") or value.startswith("~"):
            safe = _safe_artifact_path(value, "")
            if safe:
                return _public_artifact({"path": value}).get("url") or "[local artifact]"
            if os.path.isabs(os.path.expanduser(value)):
                return "[local path hidden]/" + os.path.basename(value)
        return _redact_text(value)
    return value


def _candidate_team_ids(sid: str, session: dict) -> list[str]:
    found = set()
    for turn in session.get("turns") or []:
        block = _result_block(turn)
        for value in (turn.get("team_id"), block.get("team_id")):
            if value:
                found.add(str(value))
    root = _teams_dir()
    if not root.is_dir():
        return sorted(found)
    for path in root.glob("web_" + sid + "_*.json"):
        if not path.name.endswith(".events.jsonl"):
            found.add(path.stem)
    if found:
        return sorted(found)
    questions = {str(t.get("question") or "").strip() for t in session.get("turns") or [] if t.get("question")}
    target_time = float(session.get("updated") or 0)
    candidates = []
    for path in root.glob("*.json"):
        try:
            delta = abs(path.stat().st_mtime - target_time) if target_time else 0
            if not target_time or delta < 6 * 3600:
                candidates.append((delta, path))
        except OSError:
            continue
    for _delta, path in sorted(candidates, key=lambda item: item[0])[:120]:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(state.get("question") or "").strip() in questions:
            found.add(str(state.get("team_id") or path.stem))
    return sorted(found)


def _team_events(team_ids: list[str]) -> tuple[list[dict], bool]:
    rows, truncated = [], False
    root = _teams_dir().resolve()
    # 详情页只需要可读时间线，不应为一次点击读取/脱敏数万条底层心跳。
    # 从文件尾部取最近事件，并在保留关键事件的同时折叠高频 heartbeat/reason。
    cap = max(100, min(int(os.environ.get("LEGION_VERIFICATION_EVENT_CAP", "1200")), 5000))
    for team_id in team_ids:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", team_id)[:100]
        path = (root / (safe + ".events.jsonl")).resolve()
        if not _inside(path, root) or not path.is_file():
            continue
        try:
            lines, line_count = deque(maxlen=cap), 0
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line_count, line in enumerate(handle, 1):
                    lines.append(line)
            truncated = truncated or line_count > cap
            last_pulse = None
            for line in lines:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                data = row.get("data") if isinstance(row.get("data"), dict) else {}
                kind = str(data.get("t") or row.get("event") or "")
                if kind in {"heartbeat", "reason"} and row.get("event") == "step":
                    last_pulse = row
                    continue
                if last_pulse:
                    last_pulse["team_id"] = team_id
                    rows.append(_redact(last_pulse))
                    last_pulse = None
                row["team_id"] = team_id
                rows.append(_redact(row))
            if last_pulse:
                last_pulse["team_id"] = team_id
                rows.append(_redact(last_pulse))
        except OSError:
            continue
    rows.sort(key=lambda item: (float(item.get("ts") or 0), int(item.get("event_id") or 0)))
    return rows, truncated


def session_detail(sid: str, *, include_raw: bool = False, reviews: dict | None = None) -> dict | None:
    try:
        if not _SESSION_ID_RE.fullmatch(str(sid or "")):
            raise ValueError("invalid session id")
        sid = str(sid)
    except ValueError:
        return {"ok": False, "error": "这条历史记录的会话编号已损坏，仍可从终极大题或产物分类查看原始结果",
                "reason": "invalid_session_reference", "sid": str(sid or "")[:120]}
    path = (_sessions_dir() / (sid + ".json")).resolve()
    if not _inside(path, _sessions_dir()) or not path.is_file():
        return None
    try:
        session = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    session["sid"] = str(session.get("sid") or sid)
    team_ids = _candidate_team_ids(sid, session)
    events, truncated = _team_events(team_ids)
    tool_timeline = []
    for event in events:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        kind = str(data.get("t") or event.get("event") or "")
        if "tool" in kind or data.get("tool") or data.get("url"):
            tool_timeline.append(event)
    return {"ok": True, "session": _redact(session, include_raw=include_raw),
            "artifacts": _collect_artifacts(session), "team_ids": team_ids,
            "events": events, "events_truncated": truncated, "tool_timeline": tool_timeline,
            "review": (reviews or {}).get("session:" + sid, {})}


def _artifact_associations(path: Path, bundle_links: dict | None = None) -> list[str]:
    links = bundle_links if bundle_links is not None else _build_session_cache().get("bundle_links", {})
    return sorted(links.get(path.name, set()))


def query_artifacts(*, offset: int = 0, limit: int = 50, q: str = "",
                    state: str = "all", kind: str = "", harness: str = "",
                    after: float = 0, before: float = 0,
                    reviews: dict | None = None) -> dict:
    root = _outputs_dir().resolve()
    rows, entries, needle, reviews = [], [], str(q or "").lower(), reviews or {}
    all_count = associated_all = 0
    session_cache = _build_session_cache()
    bundle_links = session_cache.get("bundle_links", {})
    inferred_links = session_cache.get("inferred_bundle_links", {})
    if root.is_dir():
        try:
            entries = list(os.scandir(root))
        except OSError:
            entries = []
        for entry in entries:
            try:
                if entry.name.startswith(".") or entry.is_symlink():
                    continue
                stat = entry.stat(follow_symlinks=False)
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
                if not (is_dir or is_file):
                    continue
            except OSError:
                continue
            path = Path(entry.path)
            explicit_sessions = _artifact_associations(path, bundle_links)
            inferred_sessions = sorted(inferred_links.get(path.name, set()))
            sessions = sorted(set(explicit_sessions) | set(inferred_sessions))
            all_count += 1
            if sessions:
                associated_all += 1
            status = "associated" if sessions else "orphan"
            association = "explicit" if explicit_sessions else ("inferred" if inferred_sessions else "none")
            item_kind = "bundle" if is_dir else _kind(entry.name)
            source_harness = entry.name.split("_", 1)[0] if "_" in entry.name else ""
            review = reviews.get("artifact:" + entry.name, {})
            if state not in ("", "all") and status != state:
                continue
            if kind and item_kind != kind:
                continue
            if harness and source_harness != harness:
                continue
            if after and stat.st_mtime < float(after):
                continue
            if before and stat.st_mtime > float(before):
                continue
            if needle and needle not in entry.name.lower():
                continue
            url = "" if is_dir else "/home-outputs/" + quote(entry.name)
            rows.append({"name": entry.name, "rel": entry.name, "is_dir": is_dir,
                         "kind": item_kind, "harness": source_harness,
                         "bytes": stat.st_size if is_file else 0,
                         "mtime": stat.st_mtime, "state": status, "association": association,
                         "session_ids": sessions,
                         "url": url, "download_url": url + "?download=1" if url else "",
                         "review": review})
    rows.sort(key=lambda item: item["mtime"], reverse=True)
    result = _page(rows, offset, limit)
    result["counts"] = {"associated": associated_all, "orphan": all_count - associated_all}
    return result


def artifact_detail(rel: str, *, offset: int = 0, limit: int = 100, q: str = "") -> dict | None:
    root = _outputs_dir().resolve()
    rel = unquote(str(rel or "")).strip("/")
    target = (root / rel).resolve()
    if not rel or not _inside(target, root) or not target.exists():
        return None
    if target.is_file():
        return {"ok": True, "root": rel.split("/", 1)[0], "items": [_public_artifact({"path": str(target)})],
                "total": 1, "offset": 0, "limit": 1, "has_more": False, "truncated": False}
    needle, heap, scanned, truncated = str(q or "").lower(), [], 0, False
    started = time.monotonic()
    for dirpath, dirnames, filenames in os.walk(target):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
        for name in filenames:
            scanned += 1
            if scanned > 50000 or time.monotonic() - started > 2.5:
                truncated = True
                break
            if name.startswith(".") or (needle and needle not in name.lower()):
                continue
            path = Path(dirpath) / name
            try:
                if not _inside(path, target):
                    continue
                stat = path.stat()
            except OSError:
                continue
            item = _public_artifact({"path": str(path), "name": name, "bytes": stat.st_size})
            item["bundle_rel"] = path.relative_to(target).as_posix()
            heapq.heappush(heap, (stat.st_mtime, item["bundle_rel"], item))
            if len(heap) > 1000:
                heapq.heappop(heap)
        if truncated:
            break
    items = [entry[2] for entry in sorted(heap, reverse=True)]
    result = _page(items, offset, limit)
    result.update({"ok": True, "root": rel.split("/", 1)[0], "path": rel,
                   "scanned": scanned, "truncated": truncated})
    return result


def _native_root(harness: str) -> tuple[Path, set[str], int] | None:
    harness = str(harness or "")
    spec = _NATIVE_SPECS.get(harness)
    if not spec:
        return None
    raw = spec[0]
    if raw == "@meyo_users":
        root = Path(_paths.legion_dir()).parent / "users"
    elif raw == "@prime_sessions":
        root = Path(_paths.under("prime-sessions", "prime-sessions"))
    else:
        env_roots = {"cc": ("CLAUDE_CONFIG_DIR", "projects"),
                     "codex": ("CODEX_HOME", "sessions"),
                     "hermes": ("HERMES_HOME", "state.db"),
                     "deepseek": ("DSH_HOME", "sessions"),
                     "penguin": ("PENGUIN_HOME", "")}
        env_name, suffix = env_roots.get(harness, ("", ""))
        configured = os.environ.get(env_name, "") if env_name else ""
        if configured:
            configured_path = Path(configured)
            root = configured_path if suffix and configured_path.name == suffix else configured_path / suffix
        else:
            root = Path(os.path.expanduser(raw))
    return root.resolve(), set(spec[1]), int(spec[2])


def native_sources() -> list[dict]:
    rows = []
    for harness in _NATIVE_SPECS:
        root, _suffixes, _depth = _native_root(harness)
        rows.append({"harness": harness, "available": root.is_dir() or root.is_file()})
    return rows


def _redact_text(text: str) -> str:
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}", r"\1[redacted]", text)
    text = re.sub(
        r'(?i)(["\']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|secret)["\']?\s*[:=]\s*["\'])([^"\'\s]{4,})',
        r"\1[redacted]", text)
    return text


def _read_excerpt(path: Path, limit: int = 32000) -> str:
    try:
        if path.name.endswith(".zstd"):
            import zstandard
            with path.open("rb") as handle, zstandard.ZstdDecompressor().stream_reader(handle) as reader:
                data = reader.read(limit)
        else:
            with path.open("rb") as handle:
                data = handle.read(limit)
                if path.stat().st_size > limit:
                    handle.seek(max(0, path.stat().st_size - min(8192, limit)))
                    data += b"\n" + handle.read(min(8192, limit))
        return _redact_text(data.decode("utf-8", errors="replace"))
    except Exception:
        return ""


def _message_text(value) -> str:
    if isinstance(value, list):
        return " ".join(_message_text(item) for item in value)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("content") or value.get("question") or "")
    return str(value or "")


def _title_from_native(text: str, fallback: str) -> str:
    candidates = []
    for line in text.splitlines()[:160]:
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(row, dict):
            continue
        containers = [row]
        for key in ("payload", "message", "data"):
            if isinstance(row.get(key), dict):
                containers.append(row[key])
        for item in containers:
            role = str(item.get("role") or (item.get("message") or {}).get("role") or "")
            payload = item.get("content") or item.get("text") or item.get("prompt") or item.get("question")
            if payload is None and isinstance(item.get("message"), dict):
                payload = item["message"].get("content")
            if role in {"user", "human"} and payload:
                candidates.append(payload)
            elif item.get("question") or item.get("prompt"):
                candidates.append(item.get("question") or item.get("prompt"))
        if candidates:
            break
    raw = _message_text(candidates[0]) if candidates else ""
    if "\nQuestion:\n" in raw:
        raw = raw.rsplit("\nQuestion:\n", 1)[-1]
    title = re.sub(r"\s+", " ", _redact_text(raw)).strip()
    return title[:240] or fallback


def _native_candidate(harness: str, path: Path) -> bool:
    parts = set(path.parts)
    if harness == "penguin":
        return "traces" in parts and path.name.startswith("session-")
    if harness == "auto":
        return "sessions" in parts and path.name.startswith("auto_ckpt_")
    return True


def _recent_native_files(harness: str, limit: int = 300) -> tuple[list[Path], int, bool]:
    spec = _native_root(harness)
    if not spec:
        return [], 0, False
    root, suffixes, max_depth = spec
    if not root.is_dir():
        return [], 0, False
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _NATIVE_CACHE.get(harness) or {}
        if now - float(cached.get("at") or 0) < _NATIVE_CACHE_TTL:
            return list(cached.get("paths") or [])[:limit], int(cached.get("scanned") or 0), bool(cached.get("truncated"))
    heap, stack, scanned, truncated = [], [(root, 0)], 0, False
    scan_cap = 250000 if harness == "cc" else 30000
    time_cap = 5.0 if harness == "cc" else 2.0
    started = time.monotonic()
    while stack:
        directory, depth = stack.pop()
        try:
            entries = os.scandir(directory)
        except OSError:
            continue
        with entries:
            for entry in entries:
                scanned += 1
                if scanned > scan_cap or time.monotonic() - started > time_cap:
                    truncated = True
                    stack.clear()
                    break
                if entry.name.startswith(".") or entry.name in _SKIP_DIRS or entry.is_symlink():
                    continue
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False) and depth < max_depth:
                    stack.append((path, depth + 1))
                elif (entry.is_file(follow_symlinks=False) and path.suffix.lower() in suffixes
                      and not path.name.endswith((".bak", ".journal.jsonl", ".trajectory-path.json"))
                      and _native_candidate(harness, path)):
                    try:
                        heapq.heappush(heap, (entry.stat(follow_symlinks=False).st_mtime, str(path), path))
                        if len(heap) > max(1, min(limit, 30000)):
                            heapq.heappop(heap)
                            truncated = True
                    except OSError:
                        pass
    paths = [x[2] for x in sorted(heap, reverse=True)]
    with _CACHE_LOCK:
        _NATIVE_CACHE[harness] = {"at": now, "paths": paths, "scanned": scanned,
                                  "truncated": truncated}
    return paths, scanned, truncated


def _query_cc(offset: int, limit: int, needle: str) -> dict | None:
    spec = _native_root("cc")
    if not spec:
        return None
    root = spec[0]
    config_root = root.parent
    index = config_root / "history.jsonl"
    if not index.is_file():
        return None
    sessions = {}
    try:
        with index.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                session_id = str(item.get("sessionId") or "")
                project = str(item.get("project") or "")
                if not session_id or not project:
                    continue
                title = _redact_text(str(item.get("display") or "")).strip()
                timestamp = float(item.get("timestamp") or 0) / 1000
                current = sessions.get(session_id)
                if current is None:
                    project_key = re.sub(r"[^A-Za-z0-9._-]", "-", project)
                    path = (root / project_key / (session_id + ".jsonl")).resolve()
                    if not _inside(path, root) or not path.is_file():
                        continue
                    current = {"harness": "cc", "name": path.name,
                               "rel": path.relative_to(root).as_posix(), "title": title or session_id,
                               "mtime": timestamp, "bytes": path.stat().st_size,
                               "messages": 1, "project": os.path.basename(project) or project}
                    sessions[session_id] = current
                else:
                    current["messages"] += 1
                    current["mtime"] = max(current["mtime"], timestamp)
                    if not current["title"] and title:
                        current["title"] = title
    except OSError:
        return None
    rows = [row for row in sessions.values()
            if not needle or needle in row["title"].lower() or needle in row["rel"].lower()]
    rows.sort(key=lambda item: item.get("mtime", 0), reverse=True)
    result = _page(rows, offset, limit)
    result.update({"harness": "cc", "available": True, "scanned": len(sessions),
                   "truncated": False, "sources": native_sources()})
    return result


def _query_openclaw(offset: int, limit: int, needle: str) -> dict | None:
    spec = _native_root("openclaw")
    if not spec:
        return None
    root = spec[0]
    index = root / "sessions.json"
    if not index.is_file():
        return None
    try:
        payload = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    rows = []
    for key, item in (payload.items() if isinstance(payload, dict) else []):
        if not isinstance(item, dict):
            continue
        session_id = str(item.get("sessionId") or "")
        session_file = Path(str(item.get("sessionFile") or root / (session_id + ".jsonl"))).resolve()
        if not session_id or not _inside(session_file, root) or not session_file.is_file():
            continue
        title = str(item.get("displayName") or item.get("lastActivityDescription") or key)
        rel = session_file.relative_to(root).as_posix()
        if needle and needle not in title.lower() and needle not in rel.lower() and needle not in key.lower():
            continue
        rows.append({"harness": "openclaw", "name": session_file.name, "rel": rel,
                     "title": title, "mtime": float(item.get("updatedAt") or 0) / 1000,
                     "bytes": session_file.stat().st_size, "messages": 0,
                     "tools": 0, "model": str(item.get("model") or ""), "source": key})
    rows.sort(key=lambda item: item.get("mtime", 0), reverse=True)
    result = _page(rows, offset, limit)
    result.update({"harness": "openclaw", "available": True, "scanned": len(rows),
                   "truncated": False, "sources": native_sources()})
    return result


def _query_hermes(offset: int, limit: int, needle: str) -> dict:
    spec = _native_root("hermes")
    if not spec:
        return {"error": "unknown harness", "items": [], "total": 0}
    database = spec[0]
    rows = []
    if database.is_file():
        try:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=1)
            connection.row_factory = sqlite3.Row
            sql = ("SELECT id,title,started_at,last_activity_at,ended_at,message_count,"
                   "tool_call_count,model,source FROM sessions ")
            params = []
            if needle:
                sql += "WHERE lower(coalesce(title,'')) LIKE ? OR lower(id) LIKE ? "
                params.extend([f"%{needle}%", f"%{needle}%"])
            sql += "ORDER BY coalesce(last_activity_at,started_at) DESC LIMIT 30000"
            for item in connection.execute(sql, params):
                rows.append({"harness": "hermes", "name": str(item["id"]),
                             "rel": str(item["id"]), "title": item["title"] or str(item["id"]),
                             "mtime": float(item["last_activity_at"] or item["started_at"] or 0),
                             "bytes": 0, "messages": int(item["message_count"] or 0),
                             "tools": int(item["tool_call_count"] or 0),
                             "model": item["model"] or "", "source": item["source"] or ""})
            connection.close()
        except (OSError, sqlite3.Error):
            rows = []
    result = _page(rows, offset, limit)
    result.update({"harness": "hermes", "available": database.is_file(), "scanned": len(rows),
                   "truncated": len(rows) >= 30000, "sources": native_sources()})
    return result


def query_native(harness: str, *, offset: int = 0, limit: int = 30, q: str = "") -> dict:
    spec = _native_root(harness)
    if not spec:
        return {"error": "unknown harness", "items": [], "total": 0}
    needle = str(q or "").lower()
    if harness == "hermes":
        return _query_hermes(offset, limit, needle)
    if harness == "cc":
        indexed = _query_cc(offset, limit, needle)
        if indexed is not None:
            return indexed
    if harness == "openclaw":
        indexed = _query_openclaw(offset, limit, needle)
        if indexed is not None:
            return indexed
    root, _suffixes, _depth = spec
    paths, scanned, truncated = _recent_native_files(harness, 30000)
    rows = []
    for path in paths:
        rel = path.relative_to(root).as_posix()
        title, excerpt = path.stem, ""
        if needle:
            excerpt = _read_excerpt(path, 65536)
            title = _title_from_native(excerpt, path.stem)
            if needle not in rel.lower() and needle not in title.lower() and needle not in excerpt.lower():
                continue
        try:
            stat = path.stat()
        except OSError:
            continue
        rows.append({"harness": harness, "name": path.name, "rel": rel, "title": title,
                     "mtime": stat.st_mtime, "bytes": stat.st_size})
    rows.sort(key=lambda item: item.get("mtime", 0), reverse=True)
    start = max(0, int(offset or 0))
    stop = start + max(1, min(int(limit or 30), 200))
    if not needle:
        for row in rows[start:stop]:
            path = (root / row["rel"]).resolve()
            text = _read_excerpt(path, 65536)
            row["title"] = _title_from_native(text, path.stem)
    result = _page(rows, offset, limit)
    result.update({"harness": harness, "available": root.is_dir(), "scanned": scanned,
                   "truncated": truncated, "sources": native_sources()})
    return result


def _hermes_detail(session_id: str) -> dict | None:
    spec = _native_root("hermes")
    database = spec[0] if spec else Path()
    if not database.is_file() or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,180}", session_id):
        return None
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=1)
        connection.row_factory = sqlite3.Row
        session = connection.execute(
            "SELECT id,title,started_at,last_activity_at,model,source,message_count,tool_call_count "
            "FROM sessions WHERE id=?", (session_id,)).fetchone()
        if session is None:
            connection.close()
            return None
        messages = connection.execute(
            "SELECT role,content,tool_name,timestamp,finish_reason FROM messages "
            "WHERE session_id=? AND active=1 ORDER BY timestamp,id LIMIT 500", (session_id,)).fetchall()
        connection.close()
    except (OSError, sqlite3.Error):
        return None
    excerpt_lines = []
    for message in messages:
        content = _redact_text(str(message["content"] or ""))[:4000]
        excerpt_lines.append(f"[{message['role']}] {content}")
    text = "\n".join(excerpt_lines)
    artifacts = [_public_artifact(row, source="hermes") for row in _artifacts.extract(text)]
    return {"ok": True, "harness": "hermes", "rel": session_id, "name": session_id,
            "title": session["title"] or session_id, "excerpt": text,
            "excerpt_truncated": len(messages) >= 500, "bytes": len(text.encode("utf-8")),
            "mtime": float(session["last_activity_at"] or session["started_at"] or 0),
            "artifacts": artifacts, "messages": int(session["message_count"] or 0),
            "tools": int(session["tool_call_count"] or 0), "model": session["model"] or ""}


def native_detail(harness: str, rel: str) -> dict | None:
    if harness == "hermes":
        return _hermes_detail(unquote(str(rel or "")))
    spec = _native_root(harness)
    if not spec:
        return None
    root, suffixes, _depth = spec
    target = (root / unquote(str(rel or ""))).resolve()
    if not _inside(target, root) or not target.is_file() or target.suffix.lower() not in suffixes:
        return None
    text = _read_excerpt(target, 2_000_000)
    artifacts = [_public_artifact(row, source=harness) for row in _artifacts.extract(text)]
    lines = [line[:2000] for line in text.splitlines() if line.strip()]
    return {"ok": True, "harness": harness, "rel": target.relative_to(root).as_posix(),
            "name": target.name, "title": _title_from_native(text, target.stem),
            "excerpt": "\n".join(lines[:160]), "excerpt_truncated": target.stat().st_size > 2_000_000,
            "bytes": target.stat().st_size, "mtime": target.stat().st_mtime,
            "artifacts": artifacts}


def invalidate() -> None:
    with _CACHE_LOCK:
        _SESSION_CACHE.update({"stamp": None, "rows": [], "by_sid": {},
                               "links": {}, "bundle_links": {}, "inferred_bundle_links": {}})
        _NATIVE_CACHE.clear()
        _EXAM_CACHE.update({"stamp": None, "tasks": []})


def _read_json_file(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _evidence_status(path: Path, good: bool) -> tuple[str, float]:
    try:
        updated = path.stat().st_mtime
    except OSError:
        return "missing", 0
    return ("pass" if good and time.time() - updated <= _evidence.DEFAULT_MAX_AGE_SECONDS else
            "historical" if good else "issue"), updated


def _protected_path(name: str) -> Path:
    if name == "test_skeleton_guard.py":
        return HERE / "tests" / name
    if name == "harness_registry.py":
        return REPO / "harness_baselines" / "meyo-orchestrator" / name
    return HERE / name


def _mobile_artifact(value, task_id: str = "", source: str = "mobile-task") -> dict | None:
    if isinstance(value, dict):
        path = str(value.get("path") or value.get("file") or value.get("url") or "")
        name = str(value.get("name") or Path(path).name or "artifact")
    else:
        path = str(value or "")
        name = Path(path).name or "artifact"
    if not path:
        return None
    row = _public_artifact({"path": path, "name": name}, sid=task_id, source=source)
    row["task_id"] = task_id
    return row


def _mobile_task_row(task: dict) -> dict:
    task_id = str(task.get("id") or "")
    attempts = task.get("attempts") if isinstance(task.get("attempts"), list) else []
    events = task.get("events") if isinstance(task.get("events"), list) else []
    confirmation = task.get("confirmation") if isinstance(task.get("confirmation"), dict) else None
    artifacts, seen = [], set()
    candidates = list(task.get("artifacts") or [])
    for attempt in attempts:
        result = attempt.get("result") if isinstance(attempt.get("result"), dict) else {}
        deliveries = result.get("deliveries") if isinstance(result.get("deliveries"), dict) else {}
        candidates.extend(deliveries.values())
        for key in ("screenshot", "trajectory", "macro", "report"):
            if result.get(key):
                candidates.append(result[key])
    for value in candidates:
        artifact = _mobile_artifact(value, task_id)
        if not artifact:
            continue
        identity = artifact.get("url") or str(value)
        if identity in seen:
            continue
        seen.add(identity)
        artifacts.append(artifact)
    state = str(task.get("state") or "blocked")
    verification = task.get("verification") if isinstance(task.get("verification"), dict) else {}
    verified = verification.get("ok") is True
    if state == "done" and not verified:
        status = "issue"
        release_ready = False
        blockers = ["任务标为 done，但没有业务 verifier 的通过证据"]
    elif state == "done":
        status, release_ready, blockers = "pass", True, []
    elif state in {"partial", "blocked"}:
        status, release_ready = "issue", False
        blockers = [str(verification.get("error") or task.get("checkpoint", {}).get("blocked_reason")
                        or f"任务状态为 {state}")]
    else:
        status, release_ready = "pending", False
        blockers = [f"任务尚未结束：{state}"]
    return {
        "id": task_id, "kind": "mobile-task", "title": str(task.get("goal") or task_id),
        "uid": str(task.get("uid") or "default"), "state": state, "status": status,
        "updated_at": float(task.get("updated_at") or task.get("created_at") or 0),
        "created_at": float(task.get("created_at") or 0), "version": int(task.get("version") or 1),
        "business_verified": verified, "release_ready": release_ready, "blockers": blockers,
        "required_apps": task.get("required_apps") or [], "checkpoint": task.get("checkpoint") or {},
        "verification": verification, "confirmation": confirmation, "attempts": attempts,
        "events": events, "artifacts": artifacts,
        "counts": {"attempts": len(attempts), "events": len(events), "artifacts": len(artifacts),
                   "confirmations": sum(row.get("type") == "confirmation_required" for row in events)},
    }


def query_mobile_tasks(*, offset: int = 0, limit: int = 50, q: str = "", uid: str = "",
                       state: str = "", status: str = "") -> dict:
    """只读聚合云手机任务、确认、业务验真和可回看产物。"""
    rows, needle = [], str(q or "").casefold()
    root = _mobile_root() / "tasks"
    if root.is_dir():
        for path in root.glob("*/task.json"):
            task = _read_json_file(path)
            if not task:
                continue
            row = _mobile_task_row(task)
            searchable = " ".join((row["id"], row["title"], row["uid"], row["state"],
                                   json.dumps(row["verification"], ensure_ascii=False))).casefold()
            if needle and needle not in searchable:
                continue
            if uid and row["uid"] != uid:
                continue
            if state and row["state"] != state:
                continue
            if status and row["status"] != status:
                continue
            rows.append(row)
    rows.sort(key=lambda row: row["updated_at"], reverse=True)
    counts = {name: sum(row["status"] == name for row in rows)
              for name in ("pass", "pending", "issue")}
    start, cap = max(0, int(offset or 0)), max(1, min(int(limit or 50), 500))
    return {"ok": True, "items": rows[start:start + cap], "total": len(rows), "counts": counts}


def mobile_task_detail(task_id: str) -> dict | None:
    try:
        task_id = _safe_sid(task_id)
    except ValueError:
        return None
    task = _read_json_file(_mobile_root() / "tasks" / task_id / "task.json")
    return _mobile_task_row(task) if task else None


def _desktop_project_roots() -> list[tuple[str, Path]]:
    roots, seen = [], set()
    users_roots = [Path(_paths.under("users")),
                   Path(os.path.realpath(os.path.join(os.path.dirname(_paths.legion_dir()), "users")))]
    for users_root in users_roots:
        if not users_root.is_dir():
            continue
        for user in users_root.iterdir():
            if not user.is_dir() or user.is_symlink():
                continue
            candidates = [user / "desktop" / "Meyo" / "projects", user / "Meyo" / "projects"]
            for projects in candidates:
                identity = str(projects.resolve())
                if projects.is_dir() and identity not in seen:
                    roots.append((user.name, projects))
                    seen.add(identity)
    return roots


def _desktop_artifact(path: Path, task_id: str) -> dict:
    row = _public_artifact({"path": str(path), "name": path.name}, sid=task_id,
                           source="cloud-desktop")
    row["task_id"] = task_id
    return row


def _desktop_task_row(uid: str, root: Path, project: dict, task: dict) -> dict:
    project_id, task_id = str(project.get("project_id") or root.name), str(task.get("task_id") or "")
    delivery = _read_json_file(root / ".meyo" / "delivery.json")
    checks = delivery.get("checks") if isinstance(delivery.get("checks"), dict) else {}
    artifacts = []
    for value in task.get("artifacts") or []:
        raw_path = value.get("path") if isinstance(value, dict) else value
        path = Path(str(raw_path or ""))
        path = path if path.is_absolute() else root / path
        if path.is_file() and _inside(path, root):
            artifacts.append(_desktop_artifact(path, task_id))
    for value in task.get("screenshots") or []:
        raw_path = value.get("path") if isinstance(value, dict) else value
        path = Path(str(raw_path or ""))
        path = path if path.is_absolute() else root / path
        if path.is_file() and _inside(path, root):
            artifacts.append(_desktop_artifact(path, task_id))
    if not checks:
        readme = root / "README.md"
        deliverables = [path for path in (root / "deliverables").rglob("*") if path.is_file()] \
            if (root / "deliverables").is_dir() else []
        evidence = [path for path in (root / "evidence").rglob("*") if path.is_file()] \
            if (root / "evidence").is_dir() else []
        checks = {"readme": readme.is_file() and readme.stat().st_size > 0,
                  "deliverables": bool(deliverables), "evidence": bool(evidence),
                  "screenshot": any(path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
                                    for path in evidence)}
        delivery = {"checks": checks, "files": [path.relative_to(root).as_posix()
                                                   for path in deliverables + evidence]}
    state = str(task.get("state") or "queued")
    completed = state == "completed" and all(checks.get(key) is True for key in
                                               ("readme", "deliverables", "evidence", "screenshot"))
    if completed:
        status, blockers = "pass", []
    elif state == "completed" or state in {"partial", "failed", "cancelled", "blocked"}:
        status = "issue"
        missing = [name for name in ("readme", "deliverables", "evidence", "screenshot")
                   if checks and checks.get(name) is not True]
        blockers = (["缺少交付检查：" + "、".join(missing)] if missing else
                    [str(delivery.get("failure_summary") or f"任务状态为 {state}")])
    else:
        status, blockers = "pending", [f"任务尚未结束：{state}"]
    return {
        "id": f"{uid}:{project_id}:{task_id}", "kind": "desktop-task",
        "title": str(task.get("goal") or project.get("title") or task_id),
        "uid": uid, "project_id": project_id, "project_title": str(project.get("title") or project_id),
        "task_id": task_id, "state": state, "status": status,
        "updated_at": float(task.get("updated_at") or project.get("updated_at") or 0),
        "created_at": float(task.get("created_at") or project.get("created_at") or 0),
        "phase": str(task.get("phase") or ""), "mode": str(task.get("mode") or ""),
        "release_ready": completed, "blockers": blockers, "checks": checks,
        "checkpoint": task.get("checkpoint") or {}, "failures": task.get("failures") or [],
        "artifacts": artifacts, "delivery": delivery,
        "counts": {"artifacts": len(artifacts), "failures": len(task.get("failures") or [])},
    }


def query_desktop_tasks(*, offset: int = 0, limit: int = 50, q: str = "", uid: str = "",
                        state: str = "", status: str = "") -> dict:
    """只读聚合云电脑生产项目、长期任务、交付清单与机器证据。"""
    rows, needle = [], str(q or "").casefold()
    for owner, projects in _desktop_project_roots():
        if uid and owner != uid:
            continue
        for root in projects.iterdir():
            if not root.is_dir() or root.is_symlink():
                continue
            project = _read_json_file(root / ".meyo" / "project.json")
            if not project:
                continue
            task_paths = list((root / ".meyo" / "tasks").glob("*.json"))
            for task_path in task_paths:
                task = _read_json_file(task_path)
                if not task:
                    continue
                row = _desktop_task_row(owner, root, project, task)
                searchable = " ".join((row["id"], row["title"], row["project_title"], row["phase"],
                                       json.dumps(row["delivery"], ensure_ascii=False))).casefold()
                if needle and needle not in searchable:
                    continue
                if state and row["state"] != state:
                    continue
                if status and row["status"] != status:
                    continue
                rows.append(row)
    rows.sort(key=lambda row: row["updated_at"], reverse=True)
    counts = {name: sum(row["status"] == name for row in rows)
              for name in ("pass", "pending", "issue")}
    start, cap = max(0, int(offset or 0)), max(1, min(int(limit or 50), 500))
    return {"ok": True, "items": rows[start:start + cap], "total": len(rows), "counts": counts}


def desktop_task_detail(item_id: str) -> dict | None:
    parts = str(item_id or "").split(":", 2)
    if len(parts) != 3:
        return None
    uid, project_id, task_id = parts
    try:
        uid, project_id, task_id = _safe_sid(uid), _safe_sid(project_id), _safe_sid(task_id)
    except ValueError:
        return None
    for owner, projects in _desktop_project_roots():
        root = projects / project_id
        if owner != uid or not _inside(root, projects):
            continue
        project = _read_json_file(root / ".meyo" / "project.json")
        task = _read_json_file(root / ".meyo" / "tasks" / (task_id + ".json"))
        return _desktop_task_row(uid, root, project, task) if project and task else None
    return None


def _mobile_app_evidence() -> list[dict]:
    items, root = [], _mobile_root() / "apps"
    for kind, pattern in (("mobile-app-plan", "plans/*/plan.json"),
                          ("mobile-app-result", "plans/*/result.json"),
                          ("mobile-app-snapshot", "snapshots/*/*/snapshot.json")):
        for path in root.glob(pattern):
            data = _read_json_file(path)
            if not data:
                continue
            if kind == "mobile-app-plan":
                good = (data.get("trusted") is True and data.get("provider") == "fdroid"
                        and bool(re.fullmatch(r"[0-9a-f]{64}", str(data.get("sha256") or ""))))
                summary = f"{data.get('operation', 'install')} · {data.get('package', '')} · v{data.get('version_code', 0)}"
            elif kind == "mobile-app-result":
                good = data.get("ok") is True and data.get("installed") is True
                summary = f"{data.get('operation', 'install')} · {data.get('package', '')} · {'已复检' if good else '未通过'}"
            else:
                apks = data.get("apks") if isinstance(data.get("apks"), list) else []
                good = bool(apks) and all(re.fullmatch(r"[0-9a-f]{64}", str(row.get("sha256") or "")) for row in apks)
                summary = f"{data.get('package', '')} · v{data.get('version_code', 0)} · {len(apks)} APK"
            artifact = _public_artifact({"path": str(path), "name": path.name}, source="mobile-apps")
            items.append({"id": kind + ":" + path.parent.name, "kind": kind,
                          "title": {"mobile-app-plan": "可信安装计划", "mobile-app-result": "安装后复检",
                                    "mobile-app-snapshot": "回滚快照"}[kind],
                          "status": "pass" if good else "issue", "updated_at": path.stat().st_mtime,
                          "summary": summary, "digest": hashlib.sha256(path.read_bytes()).hexdigest(),
                          "details": data, "artifacts": [artifact]})
    return items


def _standard_evidence_items(repo: Path) -> list[dict]:
    """读取统一 v2 证据；每条都带谱系、新鲜度、基线差异和可回看产物。"""
    checked_rows = _evidence.list_envelopes()
    envelopes = [row.get("evidence") or {} for row in checked_rows]
    histories = {}
    for envelope in envelopes:
        subject = envelope.get("subject") if isinstance(envelope.get("subject"), dict) else {}
        key = (str(subject.get("target") or ""), str(envelope.get("evidence_type") or "generic"),
               str(subject.get("id") or ""))
        histories.setdefault(key, []).append(envelope)
    for values in histories.values():
        values.sort(key=lambda row: float(row.get("finished_at") or row.get("started_at") or 0), reverse=True)
    current_commit = _evidence._commit(repo)
    items = []
    for checked in checked_rows:
        envelope = checked.get("evidence") or {}
        subject = envelope.get("subject") if isinstance(envelope.get("subject"), dict) else {}
        lineage = envelope.get("lineage") if isinstance(envelope.get("lineage"), dict) else {}
        key = (str(subject.get("target") or ""), str(envelope.get("evidence_type") or "generic"),
               str(subject.get("id") or ""))
        history = histories.get(key) or []
        at = next((index for index, item in enumerate(history)
                   if item.get("evidence_id") == envelope.get("evidence_id")), -1)
        baseline = history[at + 1] if 0 <= at < len(history) - 1 else {}
        fresh = _evidence.freshness(envelope, current_commit=current_commit,
                                    input_root=repo)
        state = "issue" if not checked.get("ok") or envelope.get("status") == "issue" else \
            "pass" if fresh["current"] else "historical"
        artifacts = []
        base = Path(str(checked.get("path") or "")).parent
        for artifact in envelope.get("artifacts") or []:
            raw = Path(str(artifact.get("path") or ""))
            path = raw if raw.is_absolute() else base / raw
            artifacts.append(_public_artifact({"path": str(path), "name": raw.name,
                                               "kind": artifact.get("kind")},
                                              sid=str(subject.get("id") or ""),
                                              source=str(envelope.get("evidence_type") or "evidence")))
        comparison = _evidence.compare(envelope, baseline) if baseline else {}
        assertions = envelope.get("assertions") or []
        passed = sum(item.get("ok") is True for item in assertions if isinstance(item, dict))
        items.append({
            "id": str(envelope.get("evidence_id") or checked.get("path")),
            "kind": str(envelope.get("evidence_type") or "machine-evidence"),
            "title": str(subject.get("id") or envelope.get("evidence_type") or "机器证据"),
            "status": state, "updated_at": float(envelope.get("finished_at") or 0),
            "summary": f"{passed}/{len(assertions)} 断言 · 运行 {lineage.get('run_id') or 'legacy'}",
            "digest": str(envelope.get("envelope_digest") or _evidence.evidence_digest(envelope)),
            "details": {"subject": subject, "lineage": lineage, "source": envelope.get("source") or {},
                        "assertions": assertions, "blockers": envelope.get("blockers") or [],
                        "metrics": envelope.get("metrics") or {}, "freshness": fresh,
                        "comparison": comparison, "path": checked.get("path"),
                        "errors": checked.get("errors") or []},
            "artifacts": artifacts,
        })
    return items


def evidence_timeline(target: str = "") -> dict:
    rows = _standard_evidence_items(REPO)
    if target:
        rows = [row for row in rows if str((row.get("details") or {}).get("subject", {}).get("target") or "") == target]
    groups = {}
    for row in rows:
        run_id = str((row.get("details") or {}).get("lineage", {}).get("run_id") or "legacy")
        group = groups.setdefault(run_id, {"run_id": run_id, "items": [], "started_at": row.get("updated_at", 0),
                                           "finished_at": row.get("updated_at", 0), "status": "pass"})
        group["items"].append(row)
        group["started_at"] = min(float(group["started_at"] or 0), float(row.get("updated_at") or 0))
        group["finished_at"] = max(float(group["finished_at"] or 0), float(row.get("updated_at") or 0))
        if row.get("status") == "issue":
            group["status"] = "issue"
        elif row.get("status") != "pass" and group["status"] == "pass":
            group["status"] = "historical"
    ordered = sorted(groups.values(), key=lambda row: row["finished_at"], reverse=True)
    return {"ok": True, "target": target, "runs": ordered, "total": len(rows)}


def machine_evidence(repo: Path | None = None, git_dir: Path | None = None) -> dict:
    """汇总已有机器证据；只读、有限、绝不在请求里启动测试或网关。"""
    repo = Path(repo or REPO)
    git_dir = Path(git_dir or (repo / ".git"))
    if git_dir.is_file():
        try:
            raw = git_dir.read_text(encoding="utf-8").strip().removeprefix("gitdir:").strip()
            git_dir = (repo / raw).resolve() if not os.path.isabs(raw) else Path(raw)
        except OSError:
            pass
    items = _standard_evidence_items(repo)
    seen_ids = {str(item.get("id") or "") for item in items}
    cache_path = git_dir / "legion-selfcheck-cache.json"
    cache = _read_json_file(cache_path)
    for fingerprint, row in sorted((cache.get("entries") or {}).items(),
                                   key=lambda pair: float(pair[1].get("stored_at") or 0), reverse=True)[:12]:
        inputs = row.get("inputs") if isinstance(row.get("inputs"), dict) else {}
        current = bool(inputs)
        for name, expected in inputs.items():
            path = Path(name)
            if not path.is_absolute():
                path = HERE / path if name.startswith("tests/") else repo / name
            try:
                current = current and hashlib.sha256(path.read_bytes()).hexdigest() == expected
            except OSError:
                current = False
        ok = row.get("ok") is True
        status = "pass" if ok and current else "historical" if ok else "issue"
        legacy_id = "selfcheck:" + fingerprint
        if legacy_id in seen_ids:
            continue
        items.append({"id": legacy_id, "kind": "related-tests",
                      "title": "相关改动快验收", "status": status,
                      "updated_at": float(row.get("stored_at") or 0),
                      "summary": f"相关合同 {len(row.get('commands') or [])} 组 · {row.get('elapsed', 0)}s",
                      "digest": fingerprint, "details": {"current_content": current,
                      "commands": row.get("commands") or [], "inputs": inputs,
                      "elapsed": row.get("elapsed", 0), "output": str(row.get("stdout") or "")[-3000:]}})
    evidence_path = repo / "release" / "legion-service" / "skeleton_change_evidence.json"
    evidence = _read_json_file(evidence_path)
    for zone in ("red", "yellow"):
        row = evidence.get(zone) or {}
        if not row:
            continue
        files = [str(name) for name in row.get("files") or []]
        resolved = [(name, _protected_path(name)) for name in sorted(files)]
        payload = "\n".join(f"{name}:{hashlib.sha256(path.read_bytes()).hexdigest()}"
                            for name, path in resolved if path.is_file())
        current = len(files) == sum(path.is_file() for _, path in resolved) \
            and hashlib.sha256(payload.encode("utf-8")).hexdigest() == row.get("digest")
        try:
            updated = evidence_path.stat().st_mtime
        except OSError:
            updated = 0
        items.append({"id": "skeleton:" + zone, "kind": "skeleton-observation",
                      "title": f"骨架观察快照 · {zone} 区", "status": "historical",
                      "updated_at": updated,
                      "summary": f"{len(files)} 文件 · 仅供架构审阅，不阻断发布",
                      "digest": row.get("digest") or "", "details": {"current_content": current,
                      "release_blocking": False, "evidence_status": "optional",
                      "files": files, "motivation": row.get("motivation") or "",
                      "structure_changes": row.get("structure_changes") or [],
                      "all_harness_smoke_passed": row.get("all_harness_smoke_passed")}})
    for pattern, kind, title in (("all-harness-modes-*.json", "all-harness-modes", "全家多模式验收"),
                                 ("health_check_*.json", "harness-health", "全家首轮健康")):
        candidates = sorted((repo / "dev").glob(pattern), key=lambda path: path.stat().st_mtime,
                            reverse=True)
        if not candidates:
            continue
        path = candidates[0]
        data = _read_json_file(path)
        if kind == "all-harness-modes":
            summary = data.get("summary") or {}
            good = (summary.get("single_passed") == summary.get("single_total")
                    and summary.get("group_passed") == summary.get("group_total")
                    and summary.get("auto_passed") is True)
            short = (f"单家 {summary.get('single_passed', 0)}/{summary.get('single_total', 0)} · "
                     f"多模式 {summary.get('group_passed', 0)}/{summary.get('group_total', 0)}")
            details = {"summary": summary, "keys": data.get("keys") or [],
                       "started_at": data.get("started_at") or ""}
        else:
            summary = data.get("summary") or {}
            good = not (summary.get("failed") or []) and int(summary.get("passed") or 0) > 0
            short = f"通过 {summary.get('passed', 0)} · 失败 {len(summary.get('failed') or [])}"
            details = {"summary": summary, "sections": data.get("sections") or {},
                       "started_at": data.get("started_at") or ""}
        status, updated = _evidence_status(path, good)
        items.append({"id": kind + ":" + path.name, "kind": kind, "title": title,
                      "status": status, "updated_at": updated, "summary": short,
                      "digest": hashlib.sha256(path.read_bytes()).hexdigest(), "details": details})
    journey_dir = repo / "dev" / "frontend-journeys-2026-09-04"
    envelope_path = journey_dir / "evidence.json"
    if envelope_path.is_file():
        import evidence_schema
        checked = evidence_schema.read_envelope(envelope_path)
        envelope = checked.get("evidence") or {}
        assertions = envelope.get("assertions") or []
        passed = sum(row.get("ok") is True for row in assertions)
        status = "pass" if checked.get("ok") and envelope.get("status") == "pass" else "issue"
        items.append({"id": envelope.get("evidence_id") or "frontend-journeys:evidence.json",
                      "kind": "frontend-journeys", "title": "前端用户旅程",
                      "status": status, "updated_at": float(envelope.get("finished_at") or envelope_path.stat().st_mtime),
                      "summary": f"通过 {passed}/{len(assertions)} · 机器证据 {'有效' if checked.get('ok') else '无效'}",
                      "digest": envelope.get("envelope_digest") or "",
                      "details": {"assertions": assertions, "blockers": envelope.get("blockers") or [],
                                  "source": envelope.get("source") or {}, "errors": checked.get("errors") or []},
                      "artifacts": envelope.get("artifacts") or []})
    else:
        journey_path = journey_dir / "report.json"
        journey = _read_json_file(journey_path)
        if journey:
            failed = journey.get("failures") or []
            passed, total = int(journey.get("passed") or 0), int(journey.get("total") or 0)
            status, updated = _evidence_status(journey_path, total > 0 and passed == total and not failed)
            items.append({"id": "frontend-journeys:" + journey_path.name,
                          "kind": "frontend-journeys", "title": "前端用户旅程",
                          "status": status, "updated_at": updated,
                          "summary": f"通过 {passed}/{total} · 失败 {len(failed)}",
                          "digest": hashlib.sha256(journey_path.read_bytes()).hexdigest(),
                          "details": {"journeys": journey.get("journeys") or {}, "failures": failed,
                                      "legacy": True, "console_errors": journey.get("console_errors") or [],
                                      "http_errors": journey.get("http_errors") or []}})
    items.extend(_mobile_app_evidence())
    for task in query_desktop_tasks(limit=500)["items"]:
        items.append({"id": "desktop-task:" + task["id"], "kind": "desktop-task",
                      "title": "云电脑交付 · " + task["project_title"], "status": task["status"],
                      "updated_at": task["updated_at"],
                      "summary": f"{task['state']} · 交付检查 {'通过' if task['release_ready'] else '未通过'} · "
                                 f"产物 {task['counts']['artifacts']} 件",
                      "digest": hashlib.sha256(json.dumps(task, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                      "details": task, "artifacts": task["artifacts"]})
    for task in query_mobile_tasks(limit=500)["items"]:
        items.append({"id": "mobile-task:" + task["id"], "kind": "mobile-task",
                      "title": "云手机任务 · " + task["title"], "status": task["status"],
                      "updated_at": task["updated_at"],
                      "summary": f"{task['state']} · 业务验真 {'通过' if task['business_verified'] else '未通过'} · "
                                 f"证据 {task['counts']['artifacts']} 件",
                      "digest": hashlib.sha256(json.dumps(task, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                      "details": task, "artifacts": task["artifacts"]})
    items.sort(key=lambda row: float(row.get("updated_at") or 0), reverse=True)
    return {"ok": True, "items": items, "total": len(items),
            "counts": {"pass": sum(row["status"] == "pass" for row in items),
                       "pending": sum(row["status"] == "pending" for row in items),
                       "historical": sum(row["status"] == "historical" for row in items),
                       "issue": sum(row["status"] == "issue" for row in items)}}


def overview(reviews: dict | None = None) -> dict:
    started = time.perf_counter()
    cache = _build_session_cache()
    rows = cache["rows"]
    artifacts = query_artifacts(limit=1, reviews=reviews)
    # 列表只取首项即可，但 overview 的统计必须覆盖全部状态；limit=1 会让 counts 也只数一项。
    mobile = query_mobile_tasks(limit=500)
    desktop = query_desktop_tasks(limit=500)
    review_values = list((reviews or {}).values())
    exam = query_exam_tasks(limit=1)
    return {"ok": True, "localhost_only": True, "read_only_index": True,
            "ultimate_exam": {"planned": exam["planned"], "counts": exam["counts"]},
            "stats": {"sessions": len(rows), "turns": sum(x["turns"] for x in rows),
                      "uids": len({x["uid"] for x in rows}),
                      "artifact_refs": sum(x["artifact_count"] for x in rows),
                      "output_bundles": artifacts.get("total", 0),
                      "orphan_bundles": artifacts.get("counts", {}).get("orphan", 0),
                      "mobile_tasks": mobile.get("total", 0),
                      "mobile_passed": mobile.get("counts", {}).get("pass", 0),
                      "mobile_issues": mobile.get("counts", {}).get("issue", 0),
                      "desktop_tasks": desktop.get("total", 0),
                      "desktop_passed": desktop.get("counts", {}).get("pass", 0),
                      "desktop_issues": desktop.get("counts", {}).get("issue", 0),
                      "reviewed": sum(bool(x.get("verdict") and x.get("verdict") != "pending") for x in review_values)},
            "filters": {"uids": sorted({x["uid"] for x in rows}),
                        "harnesses": sorted({h for x in rows for h in x["harnesses"]}),
                        "modes": sorted({m for x in rows for m in x["modes"]})},
            "native_sources": native_sources(),
            "built_ms": round((time.perf_counter() - started) * 1000, 1), "updated_at": time.time()}
