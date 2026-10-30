"""Search-source catalog and lightweight health probes for the legion."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlparse


HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
V11 = REPO / "seed-users-system" / "v11"
EVOLUTION = V11 / "evolution"

SITE_ALIASES = {
    "youtube": "youtube.com",
    "x": "x.com",
    "twitter": "x.com",
    "reddit": "reddit.com",
    "sogou": "sogou.com",
    "sogou_weixin": "weixin.sogou.com",
    "weixin": "weixin.sogou.com",
    "bilibili": "bilibili.com",
    "zhihu": "zhihu.com",
    "xiaohongshu": "xiaohongshu.com",
    "github": "github.com",
    "hackernews": "news.ycombinator.com",
}

# Dedicated paths describe what exists in the repository. Aggregate fallback remains
# available even when a platform's own API/CDP route is temporarily unhealthy.
DEDICATED_SOURCES = {
    "youtube": {
        "domain": "youtube.com",
        "kind": "api_fallback",
        "implementation": "evolution/api_discovery.py",
        "requirements": ["YouTube Data API key or a live Invidious mirror"],
    },
    "x": {
        "domain": "x.com",
        "kind": "cdp",
        "implementation": "global_sources.py:search_twitter",
        "requirements": ["running browser CDP", "signed-in X session"],
    },
    "reddit": {
        "domain": "reddit.com",
        "kind": "cdp_or_json",
        "implementation": "global_sources.py:search_reddit + evolution/api_discovery.py",
        "requirements": ["running browser CDP or reachable old.reddit.com"],
    },
    "sogou_weixin": {
        "domain": "weixin.sogou.com",
        "kind": "direct_plus_cdp",
        "implementation": "global_sources.py:search_sogou_weixin",
        "requirements": ["direct Sogou list access", "CDP on localhost:3457 for full text"],
    },
    "bilibili": {
        "domain": "bilibili.com",
        "kind": "public_api",
        "implementation": "evolution/api_discovery.py",
        "requirements": [],
    },
    "zhihu": {
        "domain": "zhihu.com",
        "kind": "module_or_cdp",
        "implementation": "evolution/zhihu_official_api.py + global_sources.py",
        "requirements": ["Zhihu credential for official API, or signed-in browser CDP"],
    },
    "github": {
        "domain": "github.com",
        "kind": "public_api",
        "implementation": "global_sources.py + legion_tool.py:search_github",
        "requirements": ["optional gh login for private repositories and higher rate limits"],
    },
    "hackernews": {
        "domain": "news.ycombinator.com",
        "kind": "public_api",
        "implementation": "global_sources.py:search_hackernews",
        "requirements": [],
    },
}


def _read_json(path: pathlib.Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _health_state() -> tuple[dict, dict]:
    state = _read_json(V11 / ".source_health_state.json")
    disabled = _read_json(V11 / ".search_disabled_sources.json")
    return state, disabled


def _source_runtime_state(name: str, state: dict, disabled: dict) -> dict:
    key = f"search_fabric/{'twitter' if name == 'x' else name}"
    streak = (state.get("streaks") or {}).get(key) or {}
    disabled_record = disabled.get(key)
    return {
        "disabled": bool(disabled_record),
        "disabled_reason": (disabled_record or {}).get("reason"),
        "streak": streak,
        "cumulative_fail": (state.get("cumulative_fail") or {}).get(key, 0),
    }


def inventory() -> dict[str, Any]:
    """Return the source-of-truth catalog plus honest runtime status."""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import install_registry

    discovered = install_registry.discover_search_sources()
    state, disabled = _health_state()
    platforms = []
    for name, spec in DEDICATED_SOURCES.items():
        runtime = _source_runtime_state(name, state, disabled)
        platforms.append({
            "name": name,
            **spec,
            "dedicated_ready": not runtime["disabled"],
            "aggregate_fallback": f"site:{spec['domain']} <query>",
            "runtime": runtime,
        })
    return {
        "ok": True,
        "summary": discovered.get("summary") or {},
        "systems": discovered.get("systems") or [],
        "platforms": platforms,
        "note": "登记可用不等于实时请求成功；使用 probe 获取当前网络结果。",
    }


def _run_json(command: list[str], timeout: int) -> tuple[dict, float]:
    started = time.time()
    try:
        result = subprocess.run(command, cwd=str(REPO), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timeout after {timeout}s"}, round(time.time() - started, 2)
    parsed = {}
    for line in reversed((result.stdout or "").splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            parsed = value
            break
    if not parsed:
        parsed = {"ok": False, "error": (result.stderr or result.stdout or f"rc={result.returncode}")[-500:]}
    return parsed, round(time.time() - started, 2)


def probe(source: str, query: str, limit: int = 3, timeout: int = 120) -> dict:
    """Run one current connectivity probe through the supported production entry."""
    name = (source or "").strip().lower()
    domain = SITE_ALIASES.get(name, name)
    if not domain:
        return {"ok": False, "error": "source is required"}
    if not query.strip():
        return {"ok": False, "error": "query is required"}

    if name in ("sogou_weixin", "weixin"):
        code = (
            "import json,sys;sys.path.insert(0," + repr(str(V11)) + ");"
            "import global_sources as g;"
            "print(json.dumps({'ok':True,'results':g.search_sogou_weixin(" + repr(query) +
            f",limit={min(max(1, limit), 8)},fetch_detail=False)" + "},ensure_ascii=False))"
        )
        data, elapsed = _run_json([sys.executable, "-c", code], timeout)
        results = data.get("results") or []
        return {"source": name, "path": "dedicated", "ok": bool(results),
                "count": len(results), "elapsed_s": elapsed, "results": results[:limit],
                "error": data.get("error") if not results else None}

    tool = HERE / "legion_tool.py"
    params = json.dumps({"query": f"site:{domain} {query}", "mode": "fast", "limit": limit},
                        ensure_ascii=False)
    data, elapsed = _run_json([sys.executable, str(tool), "run", "runnable:web_search", params], timeout)
    # 域名匹配只认主域或子域(军团 codex dogfood 揪出:netloc.endswith(domain) 会让
    # notgithub.com 误判成 github.com 的有效结果→探活假阳性)。用 hostname 去端口+规范小写。
    _dom = str(domain or "").lower().rstrip(".")
    def _host_ok(url: str) -> bool:
        host = (urlparse(str(url or "")).hostname or "").lower().rstrip(".")
        return bool(_dom) and (host == _dom or host.endswith("." + _dom))
    results = [item for item in data.get("results") or [] if _host_ok(item.get("url"))]
    return {"source": name or domain, "path": "aggregate", "ok": bool(results),
            "count": len(results), "elapsed_s": elapsed, "results": results[:limit],
            "backends": data.get("backends") or [], "trace": data.get("trace") or [],
            "error": data.get("error") if not results else None}
