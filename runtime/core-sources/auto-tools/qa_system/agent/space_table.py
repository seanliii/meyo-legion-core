"""space_table · 2026-08-13 · 表格空间(CSV/Excel)的 Space 适配器(挂 traversal_core)

表格空间与 PDF 表格【同构】——都是 header + records。所以本适配器:
  · indexer: openpyxl(xlsx/xlsm) / csv(csv/tsv) 抽原始行
  · 清洗: 直接【复用】pdf_space 已验证的表头识别/分节行/裁空列逻辑,不重写
          (图书馆PDF暴露的版面挑战——标题装饰行/分节行/空列——CSV/Excel一模一样)
  · reader: 同 space_pdf,records → traversal_core 的 Observation

地址方案(多 sheet):
  · "sheet:{name}"  → 该 sheet 的逻辑表(records)
  · 默认入口 = 第一个 sheet

只做寻址,零新增遍历逻辑——遍历循环在 traversal_core。
"""
from __future__ import annotations
import os, sys, csv as _csv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 复用 pdf_space 已验证的表格清洗(不重写·同构)
from pdf_space import (_is_section_row, _looks_like_header, _drop_empty_cols,
                       _row_matches_header)
from traversal_core import Observation, Action


def _clean_rows(raw: list) -> tuple[list, list]:
    """把原始行(含标题装饰/分节/空列)清洗成 (header, data_rows)。
    逻辑与 pdf_space 表格清洗一致(全局只认一次表头+继承)。

    关键(32102e3e 暴露·通用修复):分节标题行(如 DVD/Blu-Ray/Video Games)不是噪声,
    它标记【下面的行属于哪一类】——直接跳过会丢语义(题问"最老的Blu-Ray"就答不了)。
    改为:记住当前 section,作为附加列 _section 挂到它下面的每条记录。header 末尾补 _section。
    图书馆题(Books/DVD 分节)同样受益·是通用增强不是单题补丁。"""
    raw = _drop_empty_cols(raw or [])
    header: list = []
    data_rows: list = []
    cur_section = ""
    has_section = False
    for r in raw:
        cells = [("" if c is None else str(c)).strip() for c in r]
        if not any(cells):
            continue
        if _is_section_row(cells):        # 分节标题行 → 记住,附加到后续行
            cur_section = cells[0]
            has_section = True
            continue
        if not header:                    # 还没锁定表头
            if _looks_like_header(cells):
                header = cells
                continue
        if header and _row_matches_header(cells, header):  # 跨块重复表头
            continue
        data_rows.append(cells + [cur_section])  # 末列附加 section
    if header and has_section:
        header = header + ["_section"]
    elif header and not has_section:
        # 无分节时把多出的空 section 列去掉(保持列对齐)
        data_rows = [r[:len(header)] for r in data_rows]
    return header, data_rows


def _read_xlsx(path: str) -> dict:
    """返回 {sheet_name: (header, rows)}。data_only=True 取公式计算值。"""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = {}
    for sn in wb.sheetnames:
        ws = wb[sn]
        raw = [list(row) for row in ws.iter_rows(values_only=True)]
        out[sn] = _clean_rows(raw)
    wb.close()
    return out


def _read_csv(path: str) -> dict:
    delim = "\t" if path.lower().endswith(".tsv") else ","
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        raw = [row for row in _csv.reader(f, delimiter=delim)]
    return {"Sheet1": _clean_rows(raw)}


class TableSpace:
    name = "table"
    kind = "info"

    def __init__(self, path: str):
        self.path = path
        self.error = ""
        self.sheets: dict = {}
        try:
            lo = path.lower()
            if lo.endswith((".xlsx", ".xlsm", ".xls")):
                self.sheets = _read_xlsx(path)
            elif lo.endswith((".csv", ".tsv")):
                self.sheets = _read_csv(path)
            else:
                self.error = f"不支持的表格格式: {path}(仅 xlsx/xlsm/csv/tsv)"
        except ImportError as e:
            self.error = f"库未装: {e}"
        except Exception as e:
            self.error = f"{type(e).__name__}: {str(e)[:120]}"

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.sheets)

    def _records(self, sheet: str) -> tuple[list, list]:
        header, rows = self.sheets.get(sheet, ([], []))
        recs = []
        for r in rows:
            rec = {}
            for i, col in enumerate(header):
                rec[col] = r[i] if i < len(r) else ""
            recs.append(rec)
        return header, recs

    def entry_addrs(self) -> list[str]:
        return [f"sheet:{sn}" for sn in self.sheets] or ["sheet:Sheet1"]

    @staticmethod
    def _row_range_addr(sheet: str, start: int, end: int) -> str:
        return f"rows:{sheet}:{start}:{end}"

    def observe(self, addr: str, query: str = "") -> Observation:
        if addr.startswith("sheet:"):
            sn = addr[len("sheet:"):]
            header, recs = self._records(sn)
            # 全表入口只暴露有限行窗候选，避免大表一次性只读前部后就结束。
            windows = [self._row_range_addr(sn, start, min(start + 49, len(recs) - 1))
                       for start in range(0, len(recs), 50)]
            return Observation(
                addr=addr,
                content={"sheet": sn, "columns": header, "records": recs},
                candidates=[a for a in self.entry_addrs() if a != addr] + windows,
                kind="table", note=f"{len(recs)}条记录·{len(self.sheets)}个sheet",
                addr_meta={"axis": "sheet", "sheet": sn, "rows": len(recs)},
            )
        if addr.startswith("rows:"):
            try:
                _, sn, start, end = addr.split(":", 3)
                start, end = int(start), int(end)
                header, recs = self._records(sn)
                rows = recs[max(0, start):max(0, end) + 1]
            except Exception:
                return Observation(addr=addr, content="", kind="empty", note=f"非法行范围: {addr}")
            candidates = []
            if end + 1 < len(recs):
                candidates.append(self._row_range_addr(sn, end + 1, min(end + 50, len(recs) - 1)))
            if start > 0:
                candidates.append(self._row_range_addr(sn, max(0, start - 50), start - 1))
            return Observation(
                addr=addr, content={"sheet": sn, "columns": header, "records": rows,
                                    "row_range": [start, end]}, candidates=candidates,
                kind="table_rows", note=f"{sn} 第{start + 1}-{end + 1}行",
                addr_meta={"axis": "sheet", "sheet": sn, "row_start": start, "row_end": end},
            )
        if addr.startswith("cell:"):
            try:
                _, sn, row, col = addr.split(":", 3)
                row, col = int(row), int(col)
                header, recs = self._records(sn)
                value = recs[row][header[col]]
            except Exception:
                return Observation(addr=addr, content="", kind="empty", note=f"非法单元格: {addr}")
            return Observation(addr=addr, content={"sheet": sn, "row": row, "column": header[col],
                                                   "value": value}, kind="table_cell",
                               note=f"{sn}!R{row + 1}C{col + 1}",
                               addr_meta={"axis": "sheet", "sheet": sn, "row": row, "column": col})
        return Observation(addr=addr, content="", kind="empty",
                           note=f"地址不存在: {addr}")

    def act(self, action: Action) -> dict:
        return {"ok": True, "detail": f"table-space act {action.kind}"}


# ═══════════════════════════════════════════════════════════════
# 骨架驱动的 e2e(CSV/Excel golden case)
# ═══════════════════════════════════════════════════════════════


# (2026-08-27 瘦身) GAIA 自测 e2e __main__ 块已删:benchmark 测评脚手架·路径写死旧仓早跑不了·生产不走 __main__。测评阶段已过。
