"""Composable research capabilities shared by every legion harness.

The module owns typed envelopes, durable stage artifacts and source provenance. It
intentionally does not prescribe a workflow: the calling agent chooses which
capability to invoke next from the facts returned by each stage.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import legion_paths as _paths
from capstack_bridge import source_root as _tool_source_root


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
# Share the retained-source layout and legacy checkout fallback with the tool bridge.
AGENT_DIR = Path(_tool_source_root()) / "qa_system" / "agent"
EVOLUTION_DIR = REPO / "seed-users-system" / "v11" / "evolution"
CALL_MODEL = Path.home() / ".claude" / "skills" / "aigc-model-router" / "scripts" / "call_model.py"


def _workdir(value: str = "") -> Path:
    root = Path(value or os.environ.get("MEYO_WORKDIR") or _paths.under("outputs", "research"))
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-.")
    return cleaned[:80] or "research"


def _write_stage(workdir: Path, stage: str, data: Any, *, suffix: str = "json",
                 description: str = "", next_action: str = "") -> dict:
    stamp = f"{int(time.time() * 1000)}-{hashlib.sha1(stage.encode()).hexdigest()[:6]}"
    path = workdir / f"{stamp}-{_safe_name(stage)}.{suffix}"
    if suffix == "json":
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        path.write_text(str(data), encoding="utf-8")
    sidecar = path.with_name(path.name + ".progress.json")
    sidecar.write_text(json.dumps({"description": description or stage,
                                   "next_action": next_action}, ensure_ascii=False), encoding="utf-8")
    return {"name": path.name, "path": str(path), "kind": suffix,
            "bytes": path.stat().st_size, "stage": stage,
            "description": description, "next_action": next_action}


def _envelope(stage: str, *, content: str = "", data: Any = None,
              sources: list | None = None, artifacts: list | None = None,
              truncated: bool = False, error: str | None = None,
              next_inputs: list | None = None) -> dict:
    return {"ok": error is None, "stage": stage, "content": content,
            "data": data if data is not None else {}, "sources": sources or [],
            "artifacts": artifacts or [], "truncated": bool(truncated),
            "error": error, "next_inputs": next_inputs or []}


def capabilities() -> dict:
    """List composable stages without selecting a workflow for the agent."""
    return {"ok": True, "stages": [
        {"id": "research_search", "input": ["query|queries", "limit", "mode", "source"],
         "output": ["sources", "content", "trace"]},
        {"id": "research_fetch", "input": ["url", "timeout_s"],
         "output": ["source", "content", "artifact"]},
        {"id": "research_extract", "input": ["text|path", "fields"],
         "output": ["data", "artifact"]},
        {"id": "research_compress", "input": ["text|path", "max_chars"],
         "output": ["content", "artifact", "retrieval_hint"]},
        {"id": "research_graph", "input": ["text|path", "source", "start_entity", "max_hops"],
         "output": ["entities", "relations", "paths", "artifact"]},
        {"id": "research_document", "input": ["title", "sections|text", "format"],
         "output": ["artifact"]},
        {"id": "research_media", "input": ["operation", "url|path", "question", "at_s"],
         "output": ["content", "artifact"]},
    ], "contract": list(_envelope("example").keys()),
        "principle": "Stages are composable; the agent chooses the next stage from returned evidence and gaps."}


def search(query: str = "", queries: list | None = None, limit: int = 8,
           mode: str = "fast", source: str = "", workdir: str = "") -> dict:
    import legion_tool
    result = legion_tool._run_web_search(query, queries, limit, mode, source)
    sources = [{"source_id": f"s{idx + 1}", "url": hit.get("url"),
                "title": hit.get("title"), "snippet": hit.get("snippet"),
                "published_at": hit.get("published_at"), "provider": hit.get("source")}
               for idx, hit in enumerate(result.get("results") or [])]
    content = "\n".join(f"[{s['source_id']}] {s.get('title') or s['url']}\n{s['url']}\n{s.get('snippet') or ''}"
                        for s in sources)
    payload = {"queries": result.get("queries") or [], "sources": sources,
               "backends": result.get("backends") or [], "trace": result.get("trace") or []}
    artifacts = []
    if sources:
        artifacts.append(_write_stage(_workdir(workdir), "search-results", payload,
                                      description=f"搜索得到 {len(sources)} 条带来源结果",
                                      next_action="选择可信来源抓取正文，或基于缺口继续搜索。"))
    return _envelope("search", content=content[:24000], data=payload, sources=sources,
                     artifacts=artifacts, truncated=len(content) > 24000,
                     error=None if sources else result.get("error") or "search returned no sources",
                     next_inputs=["research_fetch", "research_graph", "research_search"])


def fetch(url: str, timeout_s: int = 25, workdir: str = "") -> dict:
    import media_tools
    result = media_tools.fetch_url(url, timeout_s=timeout_s)
    if not result.get("ok"):
        return _envelope("fetch", error=result.get("error") or "fetch failed",
                         sources=[{"source_id": "s1", "url": url, "status_code": result.get("status_code")}],
                         data=result, next_inputs=["research_search", "research_media"])
    text = str(result.get("text") or "")
    source = {"source_id": "s1", "url": result.get("url") or url,
              "title": result.get("title"), "status_code": result.get("status_code"),
              "method": result.get("method")}
    artifact = _write_stage(_workdir(workdir), "fetched-page",
                            {"source": source, "text": text},
                            description=f"已抓取网页正文：{source.get('title') or url}",
                            next_action="可继续结构化抽取、压缩或提取实体关系。")
    return _envelope("fetch", content=text[:24000], data={"chars": len(text)}, sources=[source],
                     artifacts=[artifact], truncated=len(text) > 24000,
                     next_inputs=["research_extract", "research_compress", "research_graph"])


def _read_input(text: str = "", path: str = "") -> tuple[str, str]:
    if path:
        target = Path(path).expanduser().resolve()
        if not target.is_file():
            raise FileNotFoundError(path)
        data = target.read_text(encoding="utf-8", errors="ignore")
        # 解包 stage artifact(军团 deepseek dogfood 揪出:fetch 把正文用 json.dumps 写成
        # {"source":..,"text":..} 的 .json·下游 extract/compress/graph 直接 read_text 会拿到
        # JSON 包装+字面\n 的噪声→段落解析/证据压缩/三元组抽取全失真)。一处解包覆盖三阶段:
        # .json 且是 {..,"text":str} 就返回干净正文,来源用 obj["source"]。
        if target.suffix == ".json":
            try:
                obj = json.loads(data)
                if isinstance(obj, dict) and isinstance(obj.get("text"), str):
                    src = obj.get("source")
                    src_s = (src.get("url") or src.get("title")) if isinstance(src, dict) else src
                    return obj["text"], str(src_s or target)
            except Exception:
                pass
        return data, str(target)
    return str(text or ""), "inline"


def extract(text: str = "", path: str = "", fields: list | None = None,
            workdir: str = "") -> dict:
    try:
        raw, source = _read_input(text, path)
    except OSError as exc:
        return _envelope("extract", error=f"{type(exc).__name__}: {exc}")
    if not raw.strip():
        return _envelope("extract", error="text or path is required")
    wanted = [str(x) for x in (fields or []) if str(x).strip()]
    cleaned = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    paragraphs = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", cleaned) if p.strip()]
    links = [{"text": html.unescape(label).strip(), "url": url}
             for url, label in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', raw, re.S)]
    tables = []
    for block in re.findall(r"<table[^>]*>(.*?)</table>", raw, re.S | re.I):
        rows = []
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", block, re.S | re.I):
            cells = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
                     for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.S | re.I)]
            if cells:
                rows.append(cells)
        if rows:
            tables.append(rows)
    values = {}
    for field in wanted:
        match = re.search(rf"(?im)^\s*{re.escape(field)}\s*[:：]\s*(.+)$", cleaned)
        values[field] = match.group(1).strip() if match else None
    data = {"source": source, "fields": values, "headings": re.findall(r"(?m)^#{1,6}\s+(.+)$", raw)[:100],
            "paragraphs": paragraphs[:500], "links": links[:500], "tables": tables[:50]}
    artifact = _write_stage(_workdir(workdir), "structured-extract", data,
                            description="网页或文档的结构化字段、段落、链接和表格",
                            next_action="检查字段和来源后，可压缩或提取实体关系。")
    return _envelope("extract", content="\n\n".join(paragraphs)[:24000], data=data,
                     artifacts=[artifact], truncated=len(cleaned) > 24000,
                     next_inputs=["research_compress", "research_graph", "research_document"])


def compress(text: str = "", path: str = "", max_chars: int = 12000,
             workdir: str = "") -> dict:
    try:
        raw, source = _read_input(text, path)
    except OSError as exc:
        return _envelope("compress", error=f"{type(exc).__name__}: {exc}")
    if not raw:
        return _envelope("compress", error="text or path is required")
    max_chars = min(max(1000, int(max_chars)), 50000)
    if len(raw) <= max_chars:
        compact = raw
        truncated = False
    else:
        marker = f"\n\n[... omitted {len(raw) - max_chars} characters; full text retained in source artifact ...]\n\n"
        head = max_chars * 3 // 4
        tail = max_chars - head - len(marker)
        compact = raw[:head] + marker + raw[-max(0, tail):]
        truncated = True
    full = _write_stage(_workdir(workdir), "evidence-full", {"source": source, "text": raw},
                        description="完整证据原文（上下文仅使用有界预览）",
                        next_action="需要遗漏细节时按路径分段读取，不要重新抓取。")
    preview = _write_stage(_workdir(workdir), "evidence-compact", compact, suffix="md",
                           description="供模型继续研究的有界证据预览",
                           next_action="根据证据缺口继续抓取、实体关系抽取或多跳搜索。")
    return _envelope("compress", content=compact, data={"source": source, "original_chars": len(raw),
                     "retained_chars": len(compact), "retrieval_hint": f"read {full['path']} with offset/limit"},
                     artifacts=[full, preview], truncated=truncated,
                     next_inputs=["research_graph", "research_search", "research_document"])


def _model_triplets(text: str, source: str, timeout_s: int = 120) -> list[tuple]:
    if not CALL_MODEL.is_file() or len(text.strip()) < 30:
        return []
    prompt = ("Return only a JSON array. Extract up to 30 factual entity relationships explicitly stated "
              "in the evidence. Each object must contain subject, relation, object. Do not infer facts. "
              "Keep names, dates and amounts exact. Evidence:\n" + text[:18000])
    try:
        result = subprocess.run([sys.executable, str(CALL_MODEL), "chat", prompt, "--domain", "programming"],
                                capture_output=True, text=True, timeout=timeout_s)
        raw = (result.stdout or "").strip()
        match = re.search(r"\[[\s\S]*\]", raw)
        rows = json.loads(match.group(0) if match else raw)
    except Exception:
        return []
    triples = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        subject, relation, obj = (str(row.get(k) or "").strip() for k in ("subject", "relation", "object"))
        if subject and relation and obj and subject.lower() != obj.lower():
            triples.append((subject, relation[:80], obj, source[:500], 0.75, 1))
    return triples


def graph(text: str = "", path: str = "", source: str = "", start_entity: str = "",
          max_hops: int = 3, workdir: str = "") -> dict:
    try:
        raw, input_source = _read_input(text, path)
    except OSError as exc:
        return _envelope("graph", error=f"{type(exc).__name__}: {exc}")
    if not raw:
        return _envelope("graph", error="text or path is required")
    if str(AGENT_DIR) not in sys.path:
        sys.path.insert(0, str(AGENT_DIR))
    import evidence_graph
    session: dict[str, Any] = {}
    provenance = source or input_source
    triples = _model_triplets(raw, provenance)
    if not triples:
        triples = evidence_graph.extract_triplets(raw, source=provenance, step=1)
    graph_data = {"nodes": {}, "edges": [], "adj": {}}
    for subject, relation, obj, relation_source, confidence, step in triples:
        evidence_graph._add_edge(graph_data, subject, relation, obj,
                                 relation_source, confidence, step)
    session["_evidence_graph"] = graph_data
    paths = []
    if start_entity:
        for end, edges in evidence_graph.find_paths(session, start_entity, max_hops=min(max(1, max_hops), 8)):
            paths.append({"to": end, "edges": edges})
    data = {"entities": list((graph_data.get("nodes") or {}).values()),
            "relations": graph_data.get("edges") or [], "paths": paths,
            "raw_triplets": [{"subject": x[0], "relation": x[1], "object": x[2],
                              "source": x[3], "confidence": x[4], "step": x[5]} for x in triples]}
    artifact = _write_stage(_workdir(workdir), "knowledge-graph", data,
                            description="带来源和置信度的实体关系图",
                            next_action="沿未连接实体或关系缺口生成下一跳查询。")
    return _envelope("graph", content=json.dumps(data, ensure_ascii=False)[:24000], data=data,
                     sources=[{"source_id": "s1", "url": source}] if source.startswith("http") else [],
                     artifacts=[artifact], truncated=len(json.dumps(data, ensure_ascii=False)) > 24000,
                     next_inputs=["research_search", "research_fetch", "research_document"])


def document(title: str = "Research Report", sections: list | None = None, text: str = "",
             format: str = "md", workdir: str = "") -> dict:
    fmt = format.lower()
    if fmt not in ("md", "html", "json"):
        return _envelope("document", error="format must be md, html or json")
    normalized = sections or [{"heading": "Summary", "content": text}]
    if fmt == "json":
        content: Any = {"title": title, "sections": normalized}
    elif fmt == "html":
        blocks = "".join(f"<section><h2>{html.escape(str(s.get('heading') or ''))}</h2><p>{html.escape(str(s.get('content') or '')).replace(chr(10), '<br>')}</p></section>" for s in normalized)
        content = f"<!doctype html><meta charset='utf-8'><title>{html.escape(title)}</title><main><h1>{html.escape(title)}</h1>{blocks}</main>"
    else:
        content = "# " + title + "\n\n" + "\n\n".join(f"## {s.get('heading') or ''}\n\n{s.get('content') or ''}" for s in normalized)
    artifact = _write_stage(_workdir(workdir), "research-report", content, suffix=fmt,
                            description=f"整理后的研究报告：{title}", next_action="打开、下载或继续补充研究。")
    return _envelope("document", content=str(content)[:12000], data={"title": title, "format": fmt},
                     artifacts=[artifact], next_inputs=[])


def _download(url: str, workdir: Path, timeout_s: int) -> dict:
    parsed = urllib.parse.urlparse(url)
    name = _safe_name(Path(parsed.path).name or "download.bin")
    path = workdir / name
    try:
        if parsed.hostname and parsed.hostname.lower().removeprefix("www.") in ("youtube.com", "youtu.be"):
            ytdlp = shutil.which("yt-dlp")
            if not ytdlp:
                return {"ok": False, "error": "yt-dlp is not installed"}
            template = str(workdir / "%(title).80s-%(id)s.%(ext)s")
            result = subprocess.run([ytdlp, "--no-playlist", "--restrict-filenames", "-o", template, url],
                                    capture_output=True, text=True, timeout=timeout_s)
            candidates = sorted(workdir.glob("*"), key=lambda item: item.stat().st_mtime, reverse=True)
            downloaded = next((item for item in candidates if item.is_file() and not item.name.endswith(".json")), None)
            if result.returncode != 0 or not downloaded:
                return {"ok": False, "error": (result.stderr or result.stdout or "yt-dlp failed")[-500:]}
            return {"ok": True, "path": str(downloaded), "bytes": downloaded.stat().st_size,
                    "backend": "yt-dlp"}
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=timeout_s) as response, path.open("wb") as target:
            shutil.copyfileobj(response, target)
        return {"ok": True, "path": str(path), "bytes": path.stat().st_size,
                "backend": "http"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _local_ocr(path: str, timeout_s: int) -> dict:
    command = shutil.which("tesseract")
    if not command:
        return {"ok": False, "error": "tesseract is not installed"}
    try:
        result = subprocess.run([command, path, "stdout", "-l", "eng"], capture_output=True,
                                text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"OCR timeout after {timeout_s}s"}
    text = (result.stdout or "").strip()
    return {"ok": bool(text), "text": text, "engine": "tesseract",
            "error": None if text else (result.stderr or "OCR returned no text")[-500:]}


def _desktop_screenshot(workdir: Path) -> dict:
    if sys.platform != "darwin" or not shutil.which("screencapture"):
        return {"ok": False, "error": "desktop screenshot is only available with macOS screencapture"}
    path = workdir / f"desktop-{int(time.time())}.png"
    try:
        result = subprocess.run(["screencapture", "-x", str(path)], capture_output=True,
                                text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "desktop screenshot timeout"}
    if result.returncode == 0 and path.is_file() and path.stat().st_size > 100:
        return {"ok": True, "path": str(path), "bytes": path.stat().st_size,
                "engine": "screencapture"}
    return {"ok": False, "error": (result.stderr or "screen recording permission is unavailable")[-500:]}


def media(operation: str, url: str = "", path: str = "", question: str = "",
          at_s: float = 1.0, timeout_s: int = 180, workdir: str = "") -> dict:
    import media_tools
    wd = _workdir(workdir)
    target = path
    artifacts = []
    if url:
        downloaded = _download(url, wd, timeout_s)
        if not downloaded.get("ok"):
            return _envelope("media", error=downloaded.get("error"), data=downloaded)
        target = downloaded["path"]
        artifacts.append({"name": Path(target).name, "path": target, "kind": "file",
                          "bytes": downloaded["bytes"], "stage": "download",
                          "description": "下载的原始多媒体文件", "next_action": "继续 OCR、抽帧或视觉理解。"})
    if operation == "download":
        return _envelope("media", data={"path": target}, artifacts=artifacts,
                         next_inputs=["research_media"])
    if operation == "screenshot":
        result = _desktop_screenshot(wd)
        target = result.get("path") or target
        if result.get("ok") and question:
            understood = media_tools.see_image(target, question, timeout_s)
            result["understanding"] = understood
            if understood.get("answer"):
                result["answer"] = understood["answer"]
    elif operation == "ocr":
        result = _local_ocr(target, timeout_s)
        if not result.get("ok"):
            result = media_tools.see_image(target, question or "提取图片里的全部文字", timeout_s)
    elif operation == "understand":
        result = media_tools.see_image(target, question or "详细描述并解释图片内容", timeout_s)
    elif operation == "frame":
        result = media_tools.video_frame(target, at_s, timeout_s)
    elif operation == "audio":
        result = media_tools.extract_audio(target, timeout_s)
    elif operation == "pdf":
        result = media_tools.read_pdf(target, summarize=True, timeout_s=timeout_s)
    else:
        return _envelope("media", error="operation must be download, ocr, understand, screenshot, frame, audio or pdf")
    if not result.get("ok"):
        return _envelope("media", error=result.get("error") or "media operation failed", data=result,
                         artifacts=artifacts)
    if result.get("path") and Path(result["path"]).is_file():
        artifacts.append({"name": Path(result["path"]).name, "path": result["path"],
                          "url": result.get("url"), "kind": Path(result["path"]).suffix.lstrip("."),
                          "bytes": result.get("bytes") or Path(result["path"]).stat().st_size,
                          "stage": operation, "description": f"多媒体处理结果：{operation}",
                          "next_action": "可继续视觉理解、抽取或整理到报告。"})
    content = str(result.get("answer") or result.get("summary") or result.get("text") or "")
    return _envelope("media", content=content[:24000], data=result, artifacts=artifacts,
                     truncated=len(content) > 24000,
                     next_inputs=["research_extract", "research_compress", "research_document"])
