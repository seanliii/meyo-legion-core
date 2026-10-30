#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""func_index:全仓【全部公开函数和工具】统一发现索引(用户2026-08-30:全部源码·全部公开函数被发现和使用).

不是挑肥拣瘦——各家源码都在本仓库(harness_baselines/+ecosystem/+seed auto),
本索引 AST 扫【全部】公开函数/类方法/声明工具,分两层(用户定的口径):
- tier=tool     : 各家【正式声明为工具】的(register_tool 装饰器 / file_per_tool 目录约定 /
                  BaseTool 子类 / manifest 声明 / t_ 前缀)→ 一等公民·优先展示。
- tier=function : 其余所有公开函数/公开方法(def 不带下划线)→ 二等·全部进索引可发现可调,
                  不被一等淹没(检索降权)但【一个不漏】。

这统一了两个诉求:①全部覆盖(几万函数无遗漏进索引)②不被噪音淹没(分层+降权)。
调用分层由 harness_tool_bus/agent_tool_provider 决定(能直调直调·否则派家),本模块只管【发现】。
"""
from __future__ import annotations
import ast
import json
import os
from pathlib import Path
import tempfile
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
_HB = os.path.join(_REPO, "harness_baselines")
_SEED = os.path.join(_REPO, "seed-users-system")

try:
    import legion_paths
    _CACHE = legion_paths.under("func_index_cache.json")
except Exception:
    _CACHE = os.path.join(os.path.expanduser("~/.meyo/legion"), "func_index_cache.json")

_SEARCH_CACHE: dict[tuple, tuple[float, dict]] = {}
_SEARCH_CACHE_TTL = 300.0
_SEARCH_CACHE_MAX = 256
_SEARCH_LOCK = threading.RLock()
_SEARCH_DOCS = {"entries_id": 0, "rows": []}
_BUILD_MEM = {"path_stat": None, "data": None}
_BUILD_LOCK = threading.RLock()

# 扫描根:每家源码目录 → 归属 harness 名(family)。ecosystem 各项目单列。
_ROOTS = {
    "auto": os.path.join(_SEED, "qa_system"),
    "codex": os.path.join(_HB, "codex"),
    "deepseek": os.path.join(_HB, "deepseek-harness"),
    "hermes": os.path.join(_HB, "hermes-agent"),
    "claude-code": os.path.join(_HB, "claude-code-oss"),
    "meyo-orchestrator": os.path.join(_HB, "meyo-orchestrator"),
    # ★我们自己的工具也要被军团发现和使用(用户2026-08-30:所有权限/工具/函数/skill才是闭环)。
    # release/legion-service 本身=军团的工具层(legion_tool runnable/func_invoke/media_tools/
    # artifact_delivery/权限…)·之前只扫别家漏了自己→军团发现不了自己的工具。补上=真闭环。
    "legion": _HERE,
}
# ecosystem 每个子项目一个 family
_ECO = os.path.join(_HB, "ecosystem")
if os.path.isdir(_ECO):
    for _name in sorted(os.listdir(_ECO)):
        _p = os.path.join(_ECO, _name)
        if os.path.isdir(_p):
            _ROOTS[f"eco:{_name}"] = _p

_SKIP_DIR = {"__pycache__", ".git", "node_modules", ".venv", "venv", "dist", "build",
             ".pytest_cache", ".mypy_cache", "site-packages"}


import re as _re
_SKIP_DIR = _SKIP_DIR | {"target", "vendor", ".next", "coverage", "__snapshots__"}
# 全部源码语言(用户2026-08-30:全部源码·不只py。codex是Rust/deepseek是TS·工具定义在那里)。
_LANG_EXT = {".py": "python", ".rs": "rust", ".ts": "ts", ".tsx": "ts",
             ".js": "js", ".mjs": "js", ".go": "go"}

# 非py语言的公开函数/方法声明正则(语言通用初筛·非AST但覆盖 export/pub/大写公开约定)。
_LANG_PATS = {
    "rust": [_re.compile(r'^\s*pub\s+(?:async\s+)?fn\s+([a-z_][A-Za-z0-9_]*)\s*(?:<[^>]*>)?\s*\(([^)]*)', _re.M)],
    "ts": [_re.compile(r'^\s*export\s+(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\(([^)]*)', _re.M),
           _re.compile(r'^\s*export\s+const\s+([A-Za-z_$][\w$]*)\s*(?::[^=]*)?=\s*(?:async\s*)?(?:<[^>]*>)?\s*\(([^)]*)', _re.M),
           _re.compile(r'^\s*export\s+(?:default\s+)?class\s+([A-Za-z_$][\w$]*)', _re.M)],
    "js": [_re.compile(r'^\s*export\s+(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)', _re.M),
           _re.compile(r'^\s*export\s+const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(([^)]*)', _re.M),
           _re.compile(r'^\s*module\.exports\.([A-Za-z_$][\w$]*)\s*=', _re.M)],
    "go": [_re.compile(r'^\s*func\s+(?:\([^)]*\)\s*)?([A-Z][A-Za-z0-9_]*)\s*\(([^)]*)', _re.M)],
}


def _iter_py(root: str):
    """遍历全部支持语言的源码(python/rust/ts/js/go),跳测试与依赖目录。"""
    boundary = Path(root).resolve()
    if boundary.is_file():
        if boundary.suffix in _LANG_EXT:
            yield str(boundary)
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR and not d.startswith(".")]
        for fn in filenames:
            ext = os.path.splitext(fn)[1]
            if ext not in _LANG_EXT:
                continue
            low = fn.lower()
            # 跳测试/类型声明/生成物(不是可复用工具·减噪·不跳普通模块)
            if (fn.startswith("test_") or fn == "conftest.py"
                    or low.endswith((".test.ts", ".test.js", ".spec.ts", ".spec.js", ".d.ts",
                                     "_test.go", ".test.tsx"))):
                continue
            path = Path(dirpath, fn)
            try:
                if path.resolve(strict=True).is_relative_to(boundary):
                    yield str(path)
            except (OSError, RuntimeError, ValueError):
                continue


def _source_path(path: str) -> str:
    """Keep existing in-repo paths, but never emit ../ paths for retained sources."""
    resolved, repo = Path(path).resolve(), Path(_REPO).resolve()
    return str(resolved.relative_to(repo)) if resolved.is_relative_to(repo) else str(resolved)


def _source_inputs() -> tuple[list[dict], dict]:
    """Reuse native context's roots and bind all caches to their current identity."""
    from legion_sandbox import peer_source_entries
    declared = peer_source_entries(Path(_REPO))
    candidates = [{"family": family, "realpath": os.path.realpath(path),
                   "path": os.path.abspath(path), "revision": "", "manifest": ""}
                  for family, path in _ROOTS.items() if os.path.exists(path)]
    candidates.extend(declared)
    by_path = {}
    for entry in candidates:
        existing = by_path.get(entry["realpath"])
        if existing is None:
            by_path[entry["realpath"]] = dict(entry)
        else:
            for key in ("revision", "manifest"):
                existing[key] = existing.get(key) or entry.get(key, "")
    # Legacy development roots can contain a narrower native context entry.
    # Do not double-index the same file, and do not widen a declared single file.
    roots = [entry for path, entry in by_path.items()
             if not any(other != path and value["family"] == entry["family"]
                        and Path(other).is_dir() and Path(path).is_relative_to(other)
                        for other, value in by_path.items())]

    def stamp(path: str) -> list | None:
        if not path:
            return None
        try:
            stat = os.stat(path)
            return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
        except OSError:
            return None

    identity = {
        "schema": 2, "repo": os.path.realpath(_REPO),
        "roots": [{**entry, "root_stat": stamp(entry["realpath"]),
                   "manifest_stat": stamp(entry.get("manifest", ""))}
                  for entry in roots],
        "release_inputs": {name: stamp(os.path.join(_REPO, name))
                           for name in ("image-payload.json", "runtime-ref.json")},
    }
    # Immutable release/manifest changes invalidate automatically. Deep edits in
    # an unversioned development tree still require the existing force=True.
    return roots, identity


def _sig(fn: ast.FunctionDef | ast.AsyncFunctionDef, drop_self: bool = False) -> str:
    args = fn.args
    positional = [*args.posonlyargs, *args.args]
    first_default = len(positional) - len(args.defaults)
    parts = []
    for i, arg in enumerate(positional):
        if drop_self and i == 0:
            continue
        part = ast.unparse(arg)
        if i >= first_default:
            part += "=" + ast.unparse(args.defaults[i - first_default])
        parts.append(part)
        if i + 1 == len(args.posonlyargs):
            parts.append("/")
    if args.vararg:
        parts.append("*" + ast.unparse(args.vararg))
    elif args.kwonlyargs:
        parts.append("*")
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        parts.append(ast.unparse(arg) + ("=" + ast.unparse(default) if default is not None else ""))
    if args.kwarg:
        parts.append("**" + ast.unparse(args.kwarg))
    return f"({', '.join(parts)})"


def _deferred_body(fn) -> bool:
    if isinstance(fn, ast.AsyncFunctionDef):
        return True
    pending = list(fn.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            return True
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            pending.extend(ast.iter_child_nodes(node))
    return False


def _callability(*, requires_instance=False, runtime_reason="") -> dict:
    """Discovery reports requirements, never a verified invocation."""
    return {
        "callable": ("requires_instance" if requires_instance else
                     "requires_runtime" if runtime_reason else "direct_candidate"),
        "requires_instance": requires_instance,
        "callability_verified": False, "dispatched": False, "dispatch_attempted": False,
        "reason": ("需要已有实例，未构造实例或验证调用" if requires_instance else
                   runtime_reason or "静态候选，尚未验证导入、参数和实际调用"),
    }


def _is_declared_tool(fn, family: str, path: str, decorators: list[str]) -> bool:
    """判定这个函数/类是不是各家【正式声明的工具】(一等公民)。按各家约定,不是猜意图。"""
    name = getattr(fn, "name", "")
    # register_tool / tool 装饰器(auto/deepseek 等)
    if any("register_tool" in d or d == "tool" or d.endswith(".tool") for d in decorators):
        return True
    # auto t_ 前缀工具
    if name.startswith("t_"):
        return True
    # hermes file_per_tool:tools/ 目录下与文件同名的顶层函数 = 该文件声明的工具
    if family == "hermes" and os.sep + "tools" + os.sep in path:
        stem = os.path.splitext(os.path.basename(path))[0]
        if name == stem or name.endswith("_tool"):
            return True
    # 名字带 _tool 后缀的公开函数(通用约定)
    if name.endswith("_tool"):
        return True
    return False


def _tool_name_signal(name: str, family: str, path: str, lang: str) -> bool:
    """非py语言:靠命名/目录约定判是否声明工具(rust/ts没装饰器·用_tool后缀/tools目录/大写Tool)。"""
    if name.endswith("_tool") or name.endswith("Tool") or name.startswith("t_"):
        return True
    if (os.sep + "tools" + os.sep) in path or (os.sep + "tool" + os.sep) in path:
        return True
    return False


def _scan_file_regex(path: str, family: str, lang: str) -> list[dict]:
    """非py语言(rust/ts/js/go)公开函数抽取:正则初筛 export/pub/大写公开声明。"""
    try:
        src = open(path, encoding="utf-8", errors="ignore").read()
    except Exception:
        return []
    rel = _source_path(path)
    seen = set()
    out = []
    # 抓函数上一行的行注释当 doc(rust/ts 常见 /// 或 //)
    lines = src.splitlines()
    line_start = {}
    off = 0
    for i, ln in enumerate(lines):
        line_start[off] = i
        off += len(ln) + 1
    for pat in _LANG_PATS.get(lang, []):
        for m in pat.finditer(src):
            name = m.group(1)
            if not name or name in seen:
                continue
            seen.add(name)
            args = (m.group(2) if m.lastindex and m.lastindex >= 2 else "") or ""
            args = " ".join(args.split())[:80]
            # doc = 声明上一行的 // 或 /// 注释
            doc = ""
            pos = m.start()
            li = min(line_start, key=lambda k: abs(k - pos)) if line_start else None
            idx = line_start.get(pos)
            if idx is None:
                # 找最接近的行号
                idx = sum(1 for c in src[:pos] if c == "\n")
            if idx and idx - 1 < len(lines):
                prev = lines[idx - 1].strip()
                if prev.startswith(("///", "//", "/**", "*")):
                    doc = prev.lstrip("/*").strip()[:120]
            is_tool = _tool_name_signal(name, family, path, lang)
            out.append({
                "qual": name, "sig": f"({args})", "doc": doc,
                "path": rel, "family": family,
                "tier": "tool" if is_tool else "function",
                "kind": "func", "lang": lang,
                **_callability(runtime_reason=f"{lang} 源码需要所属项目的运行入口"),
            })
    return out


def _scan_file(path: str, family: str) -> list[dict]:
    lang = _LANG_EXT.get(os.path.splitext(path)[1], "")
    if lang and lang != "python":
        return _scan_file_regex(path, family, lang)
    try:
        src = open(path, encoding="utf-8", errors="ignore").read()
        tree = ast.parse(src)
    except Exception:
        return []
    rel = _source_path(path)
    out = []
    import func_invoke
    local_candidate, module_reason = func_invoke.can_direct_call(path, "python")

    def callability(fn, requires_instance=False):
        reason = ("异步或生成器函数需要项目运行入口" if _deferred_body(fn) else
                  "" if local_candidate else module_reason)
        return _callability(requires_instance=requires_instance, runtime_reason=reason)

    def _dec_names(node):
        names = []
        for d in getattr(node, "decorator_list", []):
            if isinstance(d, ast.Name):
                names.append(d.id)
            elif isinstance(d, ast.Attribute):
                names.append(d.attr)
            elif isinstance(d, ast.Call):
                f = d.func
                names.append(getattr(f, "id", None) or getattr(f, "attr", "") or "call")
        return names

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            decs = _dec_names(node)
            tool = _is_declared_tool(node, family, path, decs)
            out.append({
                "qual": node.name, "sig": _sig(node),  # sig=纯参数(args)
                "doc": (ast.get_docstring(node) or "").split("\n")[0][:120],
                "path": rel, "family": family,
                "tier": "tool" if tool else "function",
                "kind": "func", "lang": "python",
                **callability(node),
            })
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            # 公开类的公开方法也算公开可用(BaseTool 子类等)
            base_names = [getattr(b, "id", None) or getattr(b, "attr", "") for b in node.bases]
            is_tool_class = any("Tool" in str(b) for b in base_names)
            for m in node.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and not m.name.startswith("_"):
                    decorators = _dec_names(m)
                    is_static = "staticmethod" in decorators
                    is_class = "classmethod" in decorators
                    out.append({
                        "qual": f"{node.name}.{m.name}", "sig": _sig(m, drop_self=is_class and not is_static),
                        "doc": (ast.get_docstring(m) or "").split("\n")[0][:120],
                        "path": rel, "family": family,
                        "tier": "tool" if (is_tool_class or m.name.endswith("_tool")) else "function",
                        "kind": "method", "lang": "python",
                        **callability(m, requires_instance=not (is_static or is_class)),
                    })
    return out


def build(force: bool = False) -> dict:
    """Scan declared sources once; reuse disk/memory only for the same inputs."""
    with _BUILD_LOCK:
        return _build(force)


def _build(force: bool) -> dict:
    roots, source_identity = _source_inputs()

    def fresh(data) -> bool:
        return (isinstance(data, dict) and data.get("source_identity") == source_identity
                and time.time() - data.get("built_at", 0) < 6 * 3600)

    if not force and os.path.isfile(_CACHE):
        try:
            stat = os.stat(_CACHE)
            stat_key = (os.path.abspath(_CACHE), stat.st_mtime_ns, stat.st_size)
            with _SEARCH_LOCK:
                if _BUILD_MEM["path_stat"] == stat_key and fresh(_BUILD_MEM["data"]):
                    return {**_BUILD_MEM["data"], "from_cache": True}
            with open(_CACHE, encoding="utf-8") as handle:
                d = json.load(handle)
            if fresh(d):
                d["from_cache"] = True
                with _SEARCH_LOCK:
                    _BUILD_MEM.update({"path_stat": stat_key, "data": d})
                return d
        except Exception:
            pass
    entries = []
    per_family = {}
    for source in roots:
        family, root = source["family"], source["realpath"]
        cnt = 0
        for path in _iter_py(root):
            rows = _scan_file(path, family)
            entries.extend(rows)
            cnt += len(rows)
        if cnt:
            per_family[family] = per_family.get(family, 0) + cnt
    tools = sum(1 for e in entries if e["tier"] == "tool")
    # No imports or calls occur during discovery. Count candidates separately
    # instead of presenting a module-level heuristic as thousands of successes.
    direct_candidates = sum(e["callable"] == "direct_candidate" for e in entries)
    result = {
        "built_at": time.time(),
        "index_version": str(time.time_ns()),
        "source_identity": source_identity,
        "total": len(entries),
        "tools": tools,
        "functions": len(entries) - tools,
        "directly_callable": None,
        "direct_candidates": direct_candidates,
        "callability_verified": False,
        "families": len(per_family),
        "per_family": per_family,
        "entries": entries,
        "from_cache": False,
    }
    temporary = ""
    try:
        directory = os.path.dirname(os.path.abspath(_CACHE))
        os.makedirs(directory, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix=".func-index-", delete=False) as handle:
            temporary = handle.name
            json.dump(result, handle, ensure_ascii=False)
        os.replace(temporary, _CACHE)
        stat = os.stat(_CACHE)
        with _SEARCH_LOCK:
            _BUILD_MEM.update({"path_stat": (os.path.abspath(_CACHE), stat.st_mtime_ns,
                                           stat.st_size), "data": result})
    except Exception:
        pass
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    return result


def search(query: str, top: int = 15, tier: str | None = None) -> dict:
    """检索全部公开函数/工具；缓存相同查询并复用预构建检索文本。"""
    idx = build()
    q = str(query or "").strip()
    index_key = (os.path.abspath(_CACHE), idx.get("index_version") or id(idx["entries"]))
    key = (index_key, q.lower(), int(top), tier or "")
    now = time.time()
    with _SEARCH_LOCK:
        cached = _SEARCH_CACHE.get(key)
        if cached and now - cached[0] < _SEARCH_CACHE_TTL:
            return {**cached[1], "from_cache": True}
        entries = idx["entries"]
        if _SEARCH_DOCS.get("index_key") != index_key:
            _SEARCH_DOCS["index_key"] = index_key
            _SEARCH_DOCS["entries_id"] = id(entries)
            _SEARCH_DOCS["rows"] = [
                (e, f"{e['qual']} {e['doc']} {e['family']} {e['path']}".lower())
                for e in entries]
        documents = _SEARCH_DOCS["rows"]
    terms = [t for t in q.lower().replace("_", " ").replace(".", " ").split() if t]
    rows = []
    for e, hay in documents:
        if tier and e["tier"] != tier:
            continue
        score = sum(hay.count(t) for t in terms)
        if score:
            if e["tier"] == "tool":
                score *= 2.0
            rows.append((score, e))
    rows.sort(key=lambda p: -p[0])
    picked = [dict(e) for _, e in rows[:top]]
    for e in picked:
        e["use"] = (f"run runnable:func_call '{{\"path\":\"{e['path']}\",\"qual\":\"{e['qual']}\",\"family\":\"{e['family']}\"}}'；"
                    "按签名补齐 args，调用结果以实际回执为准"
                    if e["callable"] == "direct_candidate" else
                    f"先查看 {e['path']} 的调用入口——{e['reason']}；未派发成员")
    result = {"ok": True, "query": query, "total_indexed": idx["total"],
              "results": picked, "from_cache": False}
    with _SEARCH_LOCK:
        _SEARCH_CACHE[key] = (now, result)
        if len(_SEARCH_CACHE) > _SEARCH_CACHE_MAX:
            oldest = sorted(_SEARCH_CACHE.items(), key=lambda item: item[1][0])
            for victim, _ in oldest[:len(_SEARCH_CACHE) - _SEARCH_CACHE_MAX]:
                _SEARCH_CACHE.pop(victim, None)
    return result


if __name__ == "__main__":
    import sys
    os.environ.setdefault("MEYO_LEGION_DIR", os.path.expanduser("~/.meyo/legion"))
    if len(sys.argv) > 1 and sys.argv[1] == "search":
        r = search(" ".join(sys.argv[2:]) or "screenshot")
        print(f"索引总数 {r['total_indexed']} · 命中:")
        for e in r["results"]:
            print(f"  [{e['tier']}] {e['family']} :: {e['qual']}{e['sig']} — {e['doc'][:50]}")
    else:
        t0 = time.time()
        idx = build(force=True)
        print(f"=== 全仓公开函数/工具索引(耗时 {time.time()-t0:.1f}s) ===")
        print(f"总数 {idx['total']} · 声明工具 {idx['tools']} · 公开函数 {idx['functions']} · 家 {idx['families']}")
        for fam, n in sorted(idx["per_family"].items(), key=lambda x: -x[1]):
            print(f"  {fam}: {n}")
