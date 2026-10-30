#!/usr/bin/env python3
"""
file_hub · 军团通用文件管道(用户体验纲领·一次通用解决所有文件类型)

用户为中心:不管文件从哪来(用户上传/军团产出/本地路径),都走同一条:
  识别类型 → 对应处理 → 统一元数据 → 前端按类型渲染(显示/播放/下载/表格/代码)

一个机制同时解决多类用户痛点:
  - 数据分析师:Excel/CSV 解析+统计
  - 小白/运营/开发者:结果按类型看得见、能播、能下
  - 开发者:代码按语言高亮

解耦原则(军团不耦合 auto·用户在瘦身 auto/qa/native):
  Excel 读取自带轻量实现(openpyxl/csv 几行),不 import seed-users-system,避免瘦身断链。
"""
import csv
import os

# 类型 → kind 映射(前端按 kind 渲染)
_EXT_KIND = {
    "png": "image", "jpg": "image", "jpeg": "image", "gif": "image", "webp": "image", "svg": "image",
    "mp4": "video", "mov": "video", "webm": "video", "mkv": "video",
    "mp3": "audio", "wav": "audio", "m4a": "audio", "ogg": "audio", "flac": "audio",
    "pdf": "pdf",
    "html": "web", "htm": "web",
    "xlsx": "spreadsheet", "xlsm": "spreadsheet", "xls": "spreadsheet",
    "csv": "spreadsheet", "tsv": "spreadsheet",
    "py": "code", "js": "code", "ts": "code", "jsx": "code", "tsx": "code",
    "go": "code", "rs": "code", "java": "code", "c": "code", "cpp": "code", "h": "code",
    "rb": "code", "php": "code", "swift": "code", "kt": "code", "sh": "code", "sql": "code",
    "md": "text", "txt": "text", "json": "text", "yaml": "text", "yml": "text", "toml": "text",
    "ansi": "terminal", "cast": "terminal",   # TUI 家终端快照(原始 ANSI 流·对话流渲染成黑底终端卡)
}
_CODE_LANG = {"py": "python", "js": "javascript", "ts": "typescript", "go": "go",
              "rs": "rust", "java": "java", "sh": "bash", "sql": "sql", "rb": "ruby"}


def kind_of(path_or_name: str) -> str:
    ext = os.path.splitext(str(path_or_name))[1].lower().lstrip(".")
    return _EXT_KIND.get(ext, "file")


# ── 表格(Excel/CSV)轻量读取:自带·不依赖 auto ──
def _read_spreadsheet(path: str, max_rows: int = 200) -> dict:
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    rows, sheet = [], None
    try:
        if ext in ("csv", "tsv"):
            delim = "\t" if ext == "tsv" else ","
            with open(path, "r", encoding="utf-8", errors="ignore", newline="") as f:
                for i, r in enumerate(csv.reader(f, delimiter=delim)):
                    if i >= max_rows:
                        break
                    rows.append(r)
        else:  # xlsx/xlsm/xls
            import openpyxl
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            ws = wb.active
            sheet = ws.title
            for i, r in enumerate(ws.iter_rows(values_only=True)):
                if i >= max_rows:
                    break
                rows.append(["" if c is None else str(c) for c in r])
            wb.close()
    except Exception as e:
        return {"ok": False, "error": f"表格解析失败: {type(e).__name__}: {e}"}
    if not rows:
        return {"ok": False, "error": "表格为空"}
    header, body = rows[0], rows[1:]
    ncol = len(header)
    # 轻量统计:每列非空数 + 数值列的和/均值(给军团分析当上下文)
    stats = []
    for ci in range(ncol):
        vals = [row[ci] for row in body if ci < len(row) and row[ci] not in ("", None)]
        nums = []
        for v in vals:
            try:
                nums.append(float(str(v).replace(",", "")))
            except Exception:
                pass
        col = {"name": header[ci] if ci < len(header) else f"col{ci}", "nonempty": len(vals)}
        if nums:
            col["numeric"] = True
            col["sum"] = round(sum(nums), 4)
            col["avg"] = round(sum(nums) / len(nums), 4)
            col["min"] = min(nums)
            col["max"] = max(nums)
        stats.append(col)
    return {"ok": True, "sheet": sheet, "n_rows": len(body), "n_cols": ncol,
            "header": header, "rows": body[:50], "col_stats": stats,
            "truncated": len(rows) >= max_rows}


def _read_code(path: str, max_bytes: int = 60000) -> dict:
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read(max_bytes)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "lang": _CODE_LANG.get(ext, ext or "text"),
            "code": txt, "lines": txt.count("\n") + 1,
            "truncated": os.path.getsize(path) > max_bytes}


def describe(path: str, url: str = "") -> dict:
    """统一元数据:任意文件 → {kind, url, name, bytes, preview, renderable, downloadable}。

    前端一套逻辑按 kind 渲染;preview 是喂给 harness / 前端预览的结构化内容。
    """
    if not os.path.isfile(path):
        return {"ok": False, "error": f"文件不存在: {path}", "kind": "file"}
    kind = kind_of(path)
    meta = {"ok": True, "kind": kind, "name": os.path.basename(path),
            "url": url or ("/media/" + os.path.basename(path)),
            "bytes": os.path.getsize(path), "renderable": kind != "file",
            "downloadable": True}
    if kind == "spreadsheet":
        meta["preview"] = _read_spreadsheet(path)
    elif kind == "code":
        meta["preview"] = _read_code(path)
    elif kind == "text":
        try:
            meta["preview"] = {"ok": True, "text": open(path, encoding="utf-8",
                                                        errors="ignore").read(20000)}
        except Exception as e:
            meta["preview"] = {"ok": False, "error": str(e)}
    # 图/视频/音频/pdf 由前端直接按 url 渲染,无需 preview
    return meta


def describe_repo(repo_path: str, max_files: int = 80, max_chars: int = 50000) -> dict:
    """本地代码库 → 目录树 + 关键文件内容,供军团分析。

    权限边界:只读用户显式给的目录;跳过 .git/node_modules/venv/缓存/二进制;
    realpath 校验每个文件必须仍在该目录内(符号链接不能越界)。
    """
    root = os.path.realpath(os.path.expanduser(repo_path))
    if not os.path.isdir(root):
        return {"ok": False, "error": f"代码库目录不存在: {repo_path}"}
    skip_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
                 ".next", ".cache", "target", "vendor"}
    allowed_exts = set(_EXT_KIND) | {"html", "css", "xml", "proto", "gradle", "dockerfile"}
    files, total = [], 0
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
        for name in sorted(names):
            if len(files) >= max_files or total >= max_chars:
                break
            p = os.path.realpath(os.path.join(base, name))
            if not (p == root or p.startswith(root + os.sep)):
                continue  # symlink 越界
            ext = os.path.splitext(name)[1].lower().lstrip(".")
            if ext not in allowed_exts and name.lower() not in ("dockerfile", "makefile", "readme"):
                continue
            try:
                raw = open(p, "r", encoding="utf-8", errors="ignore").read(min(10000, max_chars-total))
            except Exception:
                continue
            rel = os.path.relpath(p, root)
            files.append({"path": rel, "lang": _CODE_LANG.get(ext, ext or "text"),
                          "lines": raw.count("\n") + 1, "content": raw})
            total += len(raw)
        if len(files) >= max_files or total >= max_chars:
            break
    return {"ok": True, "root": root, "n_files": len(files), "chars": total,
            "files": files, "truncated": len(files) >= max_files or total >= max_chars}


def repo_to_harness_context(repo_path: str) -> str:
    d = describe_repo(repo_path)
    if not d.get("ok"):
        return f"[本地代码库读取失败: {d.get('error')}]"
    parts = [f"[本地代码库 {os.path.basename(d['root'])}·{d['n_files']}个关键文件·只读]"]
    parts.append("文件树:\n" + "\n".join("  " + f["path"] for f in d["files"]))
    for f in d["files"][:20]:
        parts.append(f"\n--- {f['path']} ({f['lang']}·{f['lines']}行) ---\n{f['content'][:5000]}")
    return "\n".join(parts)[:60000]


def to_harness_context(path: str) -> str:
    """文件 → 喂给 harness 的文本上下文(军团据此分析)。表格给统计+样例行,代码给全文。"""
    kind = kind_of(path)
    if kind == "spreadsheet":
        d = _read_spreadsheet(path)
        if not d.get("ok"):
            return f"[表格 {os.path.basename(path)} 解析失败: {d.get('error')}]"
        lines = [f"[表格 {os.path.basename(path)}·{d['n_rows']}行×{d['n_cols']}列]"]
        lines.append("列: " + " | ".join(d["header"]))
        for c in d["col_stats"]:
            if c.get("numeric"):
                lines.append(f"  {c['name']}: 数值列·和={c['sum']}·均={c['avg']}·"
                             f"min={c['min']}·max={c['max']}·非空{c['nonempty']}")
            else:
                lines.append(f"  {c['name']}: 文本列·非空{c['nonempty']}")
        lines.append("样例行(前5):")
        for r in d["rows"][:5]:
            lines.append("  " + " | ".join(str(x) for x in r))
        return "\n".join(lines)
    if kind == "code":
        d = _read_code(path)
        if not d.get("ok"):
            return f"[代码 {os.path.basename(path)} 读取失败]"
        return f"[代码 {os.path.basename(path)}·{d['lang']}·{d['lines']}行]\n```{d['lang']}\n{d['code']}\n```"
    if kind == "text":
        try:
            return f"[文档 {os.path.basename(path)}]\n" + open(path, encoding="utf-8", errors="ignore").read(12000)
        except Exception:
            return f"[文档 {os.path.basename(path)} 读取失败]"
    return f"[文件 {os.path.basename(path)}·{kind}]"


if __name__ == "__main__":
    import sys
    import json
    if len(sys.argv) > 1:
        print(json.dumps(describe(sys.argv[1]), ensure_ascii=False, indent=1))
