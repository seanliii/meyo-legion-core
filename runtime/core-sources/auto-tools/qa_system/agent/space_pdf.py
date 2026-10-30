"""space_pdf · 2026-08-13 · PDF 空间的 Space 适配器(挂 traversal_core)

只做寻址(把 pdf_space 的块索引暴露成 traversal_core 的 Space 接口),
不写任何遍历逻辑——遍历循环在 traversal_core,这里零新增。

地址方案:
  · "table"        → 逻辑表(跨页合并的所有表格记录)
  · "p{n}:text"    → 第 n 页正文块
每个块的 addr 就是 traversal_core 的可跳转地址。
"""
from __future__ import annotations
import os, sys, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pdf_space as ps
from traversal_core import Observation, Action


class PdfSpace:
    name = "pdf"
    kind = "info"

    def __init__(self, path: str, backend: str = "auto"):
        self.path = path
        self.index = ps.index_pdf(path, backend=backend)

    def entry_addrs(self) -> list[str]:
        # 每个 Block 都是独立版面地址；逻辑表仅作为跨页聚合的快捷入口。
        addrs = []
        if any(b.kind == "table" for b in self.index.get("blocks", [])):
            addrs.append("table")
        addrs.extend(b.addr() for b in self.index.get("blocks", []))
        return list(dict.fromkeys(addrs)) or ["table"]

    def observe(self, addr: str, query: str = "") -> Observation:
        note = ""
        if self.index.get("needs_ocr"):
            note = "needs_ocr(扫描件·已按backend路由)"
        if addr == "table":
            recs = ps.to_records(self.index)
            hdr = ps.all_tables(self.index).get("header", [])
            return Observation(
                addr="table",
                content={"columns": hdr, "records": recs},
                candidates=[a for a in self.entry_addrs() if a != "table"],
                kind="table", note=note or f"{len(recs)}条记录",
            )
        # 块级读取：正文、表格和 section 都保留页号+bbox，可表达页眉/图注/正文/续表关系。
        for b in self.index.get("blocks", []):
            if b.addr() != addr:
                continue
            payload = ps.read_block(self.index, addr)
            nearby = [other.addr() for other in self.index.get("blocks", [])
                      if other.page == b.page and other.addr() != addr]
            meta = {"axis": "page", "page": b.page, "bbox": list(b.bbox), "block_kind": b.kind}
            return Observation(addr=addr, content=payload, kind=f"pdf_{b.kind}",
                               candidates=nearby + ["table"], note=note or b.kind,
                               addr_meta=meta)
        return Observation(addr=addr, content="", kind="empty",
                           note=f"地址不存在: {addr}")

    def act(self, action: Action) -> dict:
        # 信息空间:hop 无副作用(仅换观测地址,由骨架处理);无 delegate。
        return {"ok": True, "detail": f"pdf-space act {action.kind}"}


# ═══════════════════════════════════════════════════════════════
# 骨架驱动的 e2e(golden case:e9a2c537 gold=7)
# ═══════════════════════════════════════════════════════════════


# (2026-08-27 瘦身) GAIA 自测 e2e __main__ 块已删:benchmark 测评脚手架·路径写死旧仓早跑不了·生产不走 __main__。测评阶段已过。
