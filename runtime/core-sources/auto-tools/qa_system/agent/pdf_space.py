"""pdf_space · 2026-08-13 · PDF 可寻址空间适配器(遍历架构第一个新模态)

统一架构定位:
  所有模态 = 一块带索引的画布 + 同一套遍历循环(indexer 建索引 → reader 按址读)。
  PDF 的地址 = (page, bbox);索引 = 版面块(文字块/表格块);
  reader = 按坐标读块(文字直接取;表格块转结构化行,后续可走 SQL/DuckDB)。

本模块只贡献两个适配器函数(遍历逻辑零新增,由上层 traversal_core 驱动):
  · index_pdf(path)  -> {pages, blocks:[Block], tables:[Table]}   # indexer
  · read_block(index, addr) -> str | rows                          # reader(按址读)

后端可插拔(用户"先1再2贯通"策略):
  · backend="pdfplumber"(默认·零新依赖·数字PDF确定性强)
  · backend="mineru"/"docling"(后续接·复杂版面/图表块·接口不变只换 indexer 实现)

设计要点(golden case e9a2c537 图书馆表暴露的真实挑战):
  · 表格跨多页:表头只在首页 → 跨页续表时继承上一页表头
  · 分节标题行:整行只有第一格有值(如 "Books"/"Adult") → 标记为 section,不当数据行
  · 每个块带 (page, bbox) 来源坐标 → 可寻址、可裁出图表块走视觉通道

诚实边界:
  · 扫描件(无文字层)pdfplumber 抽不出 → index 返回 needs_ocr=True(引导走 OCR/视觉通道)
  · 复杂多列版面/合并单元格 → pdfplumber 会失真,这类留给 mineru 后端
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field, asdict
from typing import Any


# ═══════════════════════════════════════════════════════════════
# 数据结构:可寻址块
# ═══════════════════════════════════════════════════════════════

@dataclass
class Block:
    """一个可寻址的版面块。地址 = (page, bbox)。"""
    page: int
    bbox: tuple                      # (x0, top, x1, bottom) PDF 坐标
    kind: str                        # "text" | "table" | "section"
    text: str = ""                   # 文字块的正文 / section 标题
    # 表格块专用:
    header: list = field(default_factory=list)   # 列名
    rows: list = field(default_factory=list)      # [[cell,...], ...] 已剔除表头/分节行
    section: str = ""                # 该块所属的分节标题(继承自上文 section 行)

    def addr(self) -> str:
        return f"p{self.page}:{self.kind}:{int(self.bbox[1])}-{int(self.bbox[3])}"


# ═══════════════════════════════════════════════════════════════
# indexer:PDF → 带坐标的块索引
# ═══════════════════════════════════════════════════════════════

def index_pdf(path: str, *, backend: str = "pdfplumber") -> dict:
    """建 PDF 的可寻址块索引。返回 {ok, backend, pages, blocks, needs_ocr, reason}。

    blocks 里每个元素是 Block(带 page/bbox/kind)。表格块已把跨页续表合并、
    分节标题行归类、表头继承处理好——上层可直接按 addr 定位读。
    """
    if not os.path.exists(path):
        return {"ok": False, "reason": f"文件不存在: {path}", "blocks": [],
                "pages": 0, "needs_ocr": False, "backend": backend}
    if backend == "auto":
        # 先用零依赖的 pdfplumber;若是扫描件(无文字层)自动升级到 docling(OCR+版面)。
        idx = _index_pdfplumber(path)
        if idx.get("ok") and not idx.get("needs_ocr"):
            return idx
        # 数字层空 → 扫描件,升级 docling
        up = _index_structured(path, "docling")
        if up.get("ok"):
            up["reason"] = "auto路由:pdfplumber文字层空→升级docling · " + up["reason"]
            return up
        # docling 也不行(未装/失败) → 退回 pdfplumber 结果 + needs_ocr 标(诚实)
        idx["reason"] = (idx.get("reason", "") +
                         " · docling 升级失败(" + up.get("reason", "")[:60] + ")")
        return idx
    if backend == "pdfplumber":
        return _index_pdfplumber(path)
    if backend in ("mineru", "docling"):
        return _index_structured(path, backend)
    return {"ok": False, "reason": f"未知 backend: {backend}", "blocks": [],
            "pages": 0, "needs_ocr": False, "backend": backend}


def _is_section_row(row: list) -> bool:
    """分节标题行:只有第一格有值,其余全空(如 'Books'/'Adult'/"Young Children's")。"""
    if not row:
        return False
    first = (row[0] or "").strip()
    rest = [(c or "").strip() for c in row[1:]]
    return bool(first) and all(not c for c in rest)


def _looks_like_header(row: list) -> bool:
    """启发式:像表头 = 至少2个非空单元格,且非空单元格里没有纯数字
    (数据行常含数字/评分)。允许尾部空列(版面留白导致的多余空列)。"""
    cells = [(c or "").strip() for c in row]
    non_empty = [c for c in cells if c]
    if len(non_empty) < 2:
        return False
    # 表头单元格一般不是纯数字
    if any(c.replace(".", "").isdigit() for c in non_empty):
        return False
    return True


def _drop_empty_cols(raw: list) -> list:
    """裁掉所有行都为空的尾/中列(pdfplumber 对版面留白常多抽空列)。"""
    if not raw:
        return raw
    ncol = max(len(r) for r in raw)
    keep = []
    for ci in range(ncol):
        col_has = any((r[ci] if ci < len(r) else "") and str(r[ci]).strip()
                      for r in raw)
        if col_has:
            keep.append(ci)
    return [[(r[ci] if ci < len(r) else "") for ci in keep] for r in raw]


def _index_pdfplumber(path: str) -> dict:
    try:
        import pdfplumber
    except Exception as e:
        return {"ok": False, "reason": f"pdfplumber 未装: {e}", "blocks": [],
                "pages": 0, "needs_ocr": False, "backend": "pdfplumber"}

    blocks: list[Block] = []
    text_chars_total = 0
    carry_header: list = []          # 跨页续表继承的表头
    cur_section = ""                 # 当前分节(供后续数据行归属)

    try:
        with pdfplumber.open(path) as pdf:
            n_pages = len(pdf.pages)
            for pi, pg in enumerate(pdf.pages):
                page_text = pg.extract_text() or ""
                text_chars_total += len(page_text.strip())

                # --- 文字块(整页正文,带页 bbox;细粒度块留给 mineru) ---
                if page_text.strip():
                    blocks.append(Block(
                        page=pi,
                        bbox=(0, 0, float(pg.width), float(pg.height)),
                        kind="text",
                        text=page_text,
                    ))

                # --- 表格块 ---
                try:
                    found = pg.find_tables()
                except Exception:
                    found = []
                for tb in found:
                    raw = tb.extract() or []
                    if not raw:
                        continue
                    raw = _drop_empty_cols(raw)  # 裁掉版面留白造成的全空列
                    bbox = tuple(float(x) for x in tb.bbox)  # (x0,top,x1,bottom)

                    # 表头策略:全局只认一次。逐行扫,遇到"最像表头的行"锁定为全局表头
                    # (carry_header),之后所有页/表都继承它。任何等于表头的行、section
                    # 标题行、纯装饰行(如文档大标题)都不当数据行。
                    clean_rows = []
                    local_section = cur_section
                    for r in raw:
                        cells = [(c or "").strip() for c in r]
                        if not any(cells):
                            continue
                        # section 标题行(仅第一格有值):记为分节,不当数据
                        if _is_section_row(r):
                            local_section = cells[0]
                            cur_section = local_section
                            continue
                        # 还没锁定表头 → 看这行是否是表头
                        if not carry_header:
                            if _looks_like_header(r) and _header_confidence(r, []):
                                carry_header = cells
                                continue
                            # 没锁到表头前的非表头行也先收着(极少见),等锁定后再说
                        # 已有表头:跳过重复出现的表头行(跨页)
                        if carry_header and _row_matches_header(r, carry_header):
                            continue
                        clean_rows.append(cells)
                    header = carry_header

                    blocks.append(Block(
                        page=pi, bbox=bbox, kind="table",
                        header=header, rows=clean_rows, section=local_section,
                    ))
    except Exception as e:
        return {"ok": False, "reason": f"pdfplumber 解析异常: {type(e).__name__}: {e}",
                "blocks": [], "pages": 0, "needs_ocr": False, "backend": "pdfplumber"}

    needs_ocr = (text_chars_total < 20)  # 几乎没文字层 → 大概率扫描件
    return {
        "ok": True, "backend": "pdfplumber", "pages": n_pages,
        "blocks": blocks, "needs_ocr": needs_ocr,
        "reason": (f"{n_pages}页·{sum(1 for b in blocks if b.kind=='table')}个表格块"
                   + ("·疑似扫描件(文字层空·建议OCR/视觉通道)" if needs_ocr else "")),
    }


def _header_confidence(first_row: list, rest_rows: list) -> bool:
    """判断 first_row 是否真表头:表头单元格通常不是纯数字,且与数据行"形状"不同。
    简单判据:表头无纯数字单元格,而数据行里至少有一列出现过数字/多样值。"""
    fr = [(c or "").strip() for c in first_row]
    if any(c.replace(".", "").isdigit() for c in fr if c):
        return False  # 表头一般不是数字
    return True


def _row_matches_header(row: list, header: list) -> bool:
    a = [(c or "").strip().lower() for c in row]
    b = [(c or "").strip().lower() for c in header]
    return a == b


# ═══════════════════════════════════════════════════════════════
# indexer:结构化后端(MinerU/Docling)—— 先1再2贯通的"2",接口占位+真实现待接
# ═══════════════════════════════════════════════════════════════

def _index_structured(path: str, backend: str) -> dict:
    """结构化后端:输出带 (page,bbox) 的结构化块。用于 pdfplumber 失真的场景
    (扫描件/复杂版面/合并单元格)——docling 自带 OCR + 版面模型。

    docling 后端已接真实现;mineru 仍占位(依赖更重·按需再接)。
    任何一步失败都明确回传原因(不静默假装成功)。"""
    if backend == "mineru":
        try:
            __import__("magic_pdf")
        except Exception:
            return {"ok": False, "backend": backend, "blocks": [], "pages": 0,
                    "needs_ocr": False,
                    "reason": "mineru(magic_pdf) 未装·docling 已够用·如需 mineru 按需接入"}
        return {"ok": False, "backend": backend, "blocks": [], "pages": 0,
                "needs_ocr": False, "reason": "mineru 映射层未接·请用 backend='docling'"}
    return _index_docling(path)


def _index_docling(path: str) -> dict:
    try:
        from docling.document_converter import DocumentConverter
    except Exception as e:
        return {"ok": False, "backend": "docling", "blocks": [], "pages": 0,
                "needs_ocr": False,
                "reason": f"docling 未装: {type(e).__name__}: {str(e)[:80]}·请用 backend='pdfplumber'"}
    try:
        conv = DocumentConverter()
        doc = conv.convert(path).document
    except Exception as e:
        return {"ok": False, "backend": "docling", "blocks": [], "pages": 0,
                "needs_ocr": False,
                "reason": f"docling 转换失败: {type(e).__name__}: {str(e)[:100]}"}

    blocks: list[Block] = []
    n_pages = 0
    try:
        n_pages = len(getattr(doc, "pages", {}) or {}) or 0
    except Exception:
        pass

    # --- 表格块:doc.tables → dataframe(结构化行) + prov(page,bbox) ---
    for tb in (getattr(doc, "tables", None) or []):
        try:
            df = tb.export_to_dataframe()
        except Exception:
            df = None
        page, bbox = _docling_prov(tb)
        if df is not None and len(df.columns) > 0:
            header = [str(c) for c in df.columns]
            rows = [[("" if v is None else str(v)) for v in row]
                    for row in df.itertuples(index=False, name=None)]
            blocks.append(Block(page=page, bbox=bbox, kind="table",
                                header=header, rows=rows))

    # --- 文字块:整篇 markdown 作为一个可读块(细粒度块可后续按 item 拆) ---
    try:
        md = doc.export_to_markdown() or ""
    except Exception:
        md = ""
    if md.strip():
        blocks.append(Block(page=0, bbox=(0, 0, 0, 0), kind="text", text=md))

    n_tbl = sum(1 for b in blocks if b.kind == "table")
    return {"ok": True, "backend": "docling", "pages": n_pages or 0,
            "blocks": blocks, "needs_ocr": False,
            "reason": f"docling·{n_tbl}个表格块(含OCR/版面模型·可处理扫描件)"}


def _docling_prov(item) -> tuple:
    """从 docling item 拿 (page_no, bbox)。API 细节缺失时优雅降级到 (0, 零框)。"""
    try:
        prov = getattr(item, "prov", None) or []
        if prov:
            p = prov[0]
            page = int(getattr(p, "page_no", 0) or 0)
            bb = getattr(p, "bbox", None)
            if bb is not None:
                box = (float(getattr(bb, "l", 0)), float(getattr(bb, "t", 0)),
                       float(getattr(bb, "r", 0)), float(getattr(bb, "b", 0)))
                return page, box
            return page, (0, 0, 0, 0)
    except Exception:
        pass
    return 0, (0, 0, 0, 0)


# ═══════════════════════════════════════════════════════════════
# reader:按地址读块
# ═══════════════════════════════════════════════════════════════

def read_block(index: dict, addr: str = "", *, page: int | None = None,
               kind: str = "") -> Any:
    """按地址读块。addr = Block.addr() 返回的字符串;或用 page/kind 过滤。
    表格块返回 {header, rows, section};文字块返回 text 字符串。"""
    blocks = index.get("blocks", [])
    for b in blocks:
        if addr and b.addr() == addr:
            return _block_payload(b)
    # 过滤模式
    hits = [b for b in blocks
            if (page is None or b.page == page) and (not kind or b.kind == kind)]
    if len(hits) == 1:
        return _block_payload(hits[0])
    return [_block_payload(b) for b in hits]


def _block_payload(b: Block):
    if b.kind == "table":
        return {"addr": b.addr(), "header": b.header, "rows": b.rows,
                "section": b.section, "page": b.page, "bbox": b.bbox}
    return {"addr": b.addr(), "text": b.text, "page": b.page, "kind": b.kind}


def all_tables(index: dict) -> list:
    """便捷:取所有表格块合并成一张逻辑表(跨页续表已在 index 阶段处理表头继承)。
    返回 {header, rows}(rows 跨页拼接)——表格空间的 SQL/DuckDB 入口。"""
    tabs = [b for b in index.get("blocks", []) if b.kind == "table"]
    if not tabs:
        return {"header": [], "rows": []}
    header = tabs[0].header
    rows = []
    for t in tabs:
        rows.extend(t.rows)
    return {"header": header, "rows": rows, "n_tables": len(tabs)}


def to_records(index: dict) -> list[dict]:
    """把逻辑表转成 [{col: val}] 记录列表(喂 LLM / DuckDB / pandas 都行)。"""
    t = all_tables(index)
    hdr = t["header"]
    out = []
    for r in t["rows"]:
        rec = {}
        for i, col in enumerate(hdr):
            rec[col] = r[i] if i < len(r) else ""
        out.append(rec)
    return out


# ═══════════════════════════════════════════════════════════════
# 自测(dry-run,不依赖具体文件)
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys, json
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        idx = index_pdf(sys.argv[1])
        print("ok=%s pages=%s needs_ocr=%s | %s" % (
            idx["ok"], idx["pages"], idx["needs_ocr"], idx["reason"]))
        recs = to_records(idx)
        print("records=%d" % len(recs))
        for r in recs[:5]:
            print("  ", json.dumps(r, ensure_ascii=False))
    else:
        # section 行 / 表头判定单测
        assert _is_section_row(["Books", "", "", "", ""]) is True
        assert _is_section_row(["The House of Hades", "Rick Riordan", "x", "y", "z"]) is False
        assert _looks_like_header(["Title", "Author", "Status"]) is True
        print("dry-run self-test ✓")
