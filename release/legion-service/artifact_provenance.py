#!/usr/bin/env python3
"""Artifact lineage and objective browser verification for Legion deliveries."""
from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path
from typing import Any

_VERIFY_CACHE: dict[tuple[str, int, int, bool, bool], dict[str, Any]] = {}
_VERIFY_LOCK = threading.RLock()


def _identity(item: dict) -> str:
    path = str(item.get("path") or "").strip()
    if path:
        return "path:" + os.path.realpath(os.path.expanduser(path))
    url = str(item.get("url") or "").strip()
    if url:
        return "url:" + url
    return "name:" + str(item.get("name") or "").strip()


def merge_artifacts(*groups) -> list[dict]:
    """Merge artifact records without losing richer metadata from either producer."""
    from artifact_delivery import _is_private_artifact
    merged: list[dict] = []
    indexes: dict[str, int] = {}
    for group in groups:
        for raw in group or []:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            if _is_private_artifact(item):
                continue
            identity = _identity(item)
            if not identity or identity == "name:":
                continue
            if identity in indexes:
                current = merged[indexes[identity]]
                for key, value in item.items():
                    if value not in (None, "", [], {}):
                        current[key] = value
                continue
            indexes[identity] = len(merged)
            merged.append(item)
    return merged


def _merge_rows(*groups: list[dict], keys: tuple[str, ...] = ("id", "call_id", "tool", "name")) -> list[dict]:
    rows, seen = [], set()
    for group in groups:
        for item in group or []:
            if not isinstance(item, dict):
                continue
            identity = tuple(str(item.get(key) or "") for key in keys)
            if identity in seen:
                continue
            seen.add(identity); rows.append(dict(item))
    return rows


def attach_lineage(artifacts: list[dict], *, objective: str = "", task: str = "",
                   task_id: str = "", agent: str = "", actions: list | None = None,
                   inherited_from: str = "") -> list[dict]:
    """Attach only observed candidate/tool/source facts to each artifact."""
    actions = [row for row in (actions or []) if isinstance(row, dict)]
    candidates, tools, skills, sources, events = [], [], [], [], []
    candidate_ids = set()
    seen_tools = set()
    for action in actions:
        kind = str(action.get("t") or "")
        if kind == "weapon_candidates":
            events.append({key: action.get(key) for key in
                           ("t", "query_hash", "pool", "returned", "candidates")
                           if action.get(key) is not None})
            for candidate in action.get("candidates") or []:
                if isinstance(candidate, dict):
                    row = {key: candidate.get(key) for key in
                           ("id", "name", "kind", "source", "runnable", "rank")
                           if candidate.get(key) is not None}
                    candidates.append(row)
                    if row.get("id"):
                        candidate_ids.add(str(row["id"]))
        if kind == "harness_step":
            text = str(action.get("brief") or "")
            try:
                import re
                for source_path in re.findall(
                        r"(?:^|\s)([^\s]+\.(?:py|ts|tsx|js|rs|go))(?::\d+)?", text)[:10]:
                    sources.append({"kind": "source_file", "path": source_path})
            except Exception:
                pass
            continue
        if kind not in {"tool_end", "tool_timeout"}:
            continue
        call_id = str(action.get("call_id") or action.get("tool") or "")
        if call_id in seen_tools:
            continue
        seen_tools.add(call_id)
        tool_id = str(action.get("tool") or "")
        selection = dict(action.get("selection") or {})
        if tool_id in candidate_ids:
            selection["candidate_verified"] = "matched"
        elif candidates:
            selection["candidate_verified"] = "not_in_latest_candidates"
        tool = {key: action.get(key) for key in
                ("tool", "call_id", "ok", "status", "ms", "source_ref")
                if action.get(key) is not None}
        tool["selection"] = selection
        tools.append(tool)
        events.append({"t": kind, **tool})
        source = action.get("source_ref")
        if isinstance(source, dict):
            sources.append(dict(source))
            if source.get("kind") == "skill":
                skills.append({"skill": source.get("name") or source.get("id"),
                               "source": source.get("path") or source.get("repository") or ""})
    for artifact in artifacts or []:
        if not isinstance(artifact, dict):
            continue
        provenance = artifact.get("provenance") if isinstance(artifact.get("provenance"), dict) else {}
        evidence = {
            "verified": bool(provenance.get("verified")),
            "sha256": str(provenance.get("sha256") or ""),
            "verification": str(provenance.get("verification") or ""),
        }
        lineage = artifact.get("lineage") if isinstance(artifact.get("lineage"), dict) else {}
        handoffs = list(lineage.get("handoffs") or [])
        if inherited_from and not any(row.get("from") == inherited_from and row.get("to") == agent
                                      for row in handoffs if isinstance(row, dict)):
            handoffs.append({"from": inherited_from, "to": agent, "reason": "failure_relay"})
        artifact["lineage"] = {
            **lineage,
            "objective": lineage.get("objective") or {"label": str(objective)[:500]},
            "task": lineage.get("task") or {"id": str(task_id), "label": str(task)[:500]},
            "agent": lineage.get("agent") or {"label": str(agent)},
            "current_agent": {"label": str(agent)},
            "candidates": _merge_rows(lineage.get("candidates") or [], candidates)[:30],
            "tools": _merge_rows(lineage.get("tools") or [], tools,
                                  keys=("call_id", "tool", "status", "source_ref"))[:50],
            "skills": _merge_rows(lineage.get("skills") or [], skills,
                                   keys=("skill", "source"))[:20],
            "sources": _merge_rows(lineage.get("sources") or [], sources,
                                    keys=("id", "path", "repository", "kind"))[:30],
            "handoffs": handoffs,
            "events": _merge_rows(lineage.get("events") or [], events,
                                   keys=("t", "call_id", "tool", "query_hash"))[:100],
            "evidence": {**(lineage.get("evidence") or {}), **evidence},
        }
    return artifacts


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_html(path: str, *, require_visual: bool = False, offline: bool = False,
                timeout_ms: int = 10000, screenshot: bool = False) -> dict:
    """Open an HTML file in Chromium and return reproducible render evidence.

    External requests are blocked when ``offline`` is true, so a passing result proves the file can
    be opened directly rather than merely containing an HTML tag.
    """
    target = Path(str(path or "")).expanduser().resolve()
    if not target.is_file() or target.stat().st_size <= 0:
        return {"ok": False, "method": "playwright_file_url", "error": "missing_or_empty"}
    stat = target.stat()
    cache_key = (str(target), stat.st_mtime_ns, stat.st_size, bool(require_visual), bool(offline), bool(screenshot))
    with _VERIFY_LOCK:
        cached = _VERIFY_CACHE.get(cache_key)
        if cached is not None:
            return dict(cached)
        try:
            from playwright.sync_api import sync_playwright
            external_requests: list[str] = []
            console_errors: list[str] = []
            page_errors: list[str] = []
            screenshot_path = target.parent / f".{target.stem}-{_sha256(target)[:12]}.render-proof.png"
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("console", lambda message: console_errors.append(message.text)
                        if message.type == "error" else None)
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                if offline:
                    def route(request_route):
                        url = str(request_route.request.url or "")
                        if url.startswith(("http://", "https://")):
                            external_requests.append(url)
                            request_route.abort()
                        else:
                            request_route.continue_()
                    page.route("**/*", route)
                page.goto(target.as_uri(), wait_until="load", timeout=max(1000, int(timeout_ms)))
                page.wait_for_timeout(500)
                metrics = page.evaluate("""() => {
                  const visible = el => { const s=getComputedStyle(el), r=el.getBoundingClientRect();
                    return s.display!=='none' && s.visibility!=='hidden' && r.width>0 && r.height>0; };
                  let painted=0;
                  for (const canvas of document.querySelectorAll('canvas')) { try {
                    const data=canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data;
                    for(let i=3;i<data.length;i+=4){if(data[i]){painted++;break;}}
                  } catch (_) {} }
                  const graphics=[...document.querySelectorAll('svg,img,[role="img"],.chart,[class*="chart"],[class*="graph"],[class*="bar"]')].filter(visible).length;
                  return {ready:document.readyState, title:document.title,
                    textChars:(document.body?.innerText||'').trim().length,
                    visibleNodes:[...(document.body?.querySelectorAll('*')||[])].filter(visible).length,
                    graphics, paintedCanvases:painted};
                }""")
                if screenshot:
                    page.screenshot(path=str(screenshot_path), full_page=True)
                browser.close()
            rendered = metrics.get("ready") in {"interactive", "complete"} \
                and int(metrics.get("visibleNodes") or 0) > 0 \
                and (int(metrics.get("textChars") or 0) >= 20
                     or int(metrics.get("graphics") or 0) > 0
                     or int(metrics.get("paintedCanvases") or 0) > 0)
            visual = int(metrics.get("graphics") or 0) > 0 or int(metrics.get("paintedCanvases") or 0) > 0
            result = {
                "ok": bool(rendered and not page_errors and not console_errors
                           and (not require_visual or visual)
                           and (not offline or not external_requests)),
                "method": "playwright_file_url",
                "offline": bool(offline),
                "require_visual": bool(require_visual),
                "metrics": metrics,
                "console_errors": console_errors[:20],
                "page_errors": page_errors[:20],
                "external_requests": external_requests[:20],
                "screenshot_path": str(screenshot_path) if screenshot else "",
                "screenshot_url": "",
                "verified_at": time.time(),
            }
            try:
                import artifact_delivery
                if screenshot:
                    result["screenshot_url"] = artifact_delivery._url_for(str(screenshot_path))
            except Exception:
                pass
        except Exception as exc:
            result = {"ok": False, "method": "playwright_file_url",
                      "offline": bool(offline), "require_visual": bool(require_visual),
                      "error": f"{type(exc).__name__}:{str(exc)[:300]}"}
        _VERIFY_CACHE[cache_key] = dict(result)
        return result
