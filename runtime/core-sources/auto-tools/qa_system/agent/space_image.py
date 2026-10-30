"""space_image · 2026-08-13 · 图像空间(像素画布)的 Space 适配器(挂 traversal_core)

图像是【空间检索】的画布:地址 = bbox(区域坐标)。这是用户说的"空间上的检索"。
  · indexer: grid_scan 把整图切成网格候选区域(最朴素的 region_propose)
  · reader:  see(整图) / crop_zoom_read(按 region 裁剪放大精读) —— 复用现成 see_image/zoom
  · observe 真调 VLM(有成本/可能失败),不是本地免费读 —— 属能动式读取

地址方案:
  · "full"          → 整图 VLM 读(see)
  · "grid:{r},{c}"  → 第 r 行 c 列网格块精读(crop_zoom_read 按比例 region)
  · "region:l,t,r,b"→ 任意比例区域(0-1)精读

遍历价值(挂骨架后·相比单次调用):整图读不清 → grid 分块候选 → 区域精读 → 证据图登记
(mark_examined·哪块看过读出啥) → stall 熔断。补齐视觉侧缺的"证据图+区域遍历"那一环。

只做寻址,零新增遍历逻辑。
"""
from __future__ import annotations
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from traversal_core import Observation, Action

try:
    from see_image import see as _see
except Exception:
    _see = None
try:
    from see_image_zoom import (crop_zoom_read as _crop, grid_scan_read as _grid,
                                perceive_with_budget as _perceive)
except Exception:
    _crop = _grid = None

def _vision_candidates():
    """读取当前可用的视觉模型池；失败时返回空，由调用方安全降级。"""
    try:
        import multi_model
        return multi_model.usable_models(vision_only=True)
    except Exception:
        return []


class ImageSpace:
    name = "image"
    kind = "info"

    def __init__(self, path_or_url: str, *, grid=(2, 2), precision=False,
                 precision_models=None, adaptive=True):
        self.src = path_or_url
        self.grid = grid
        # 只对“读错一个字符就整题错”的任务启用：手写答案、棋盘格子、评分表、小字数值。
        self.precision = precision
        self.precision_models = list(precision_models or [])
        # 主路径默认消费预算感知阶梯；关闭仅供低成本调用或回归测试。
        self.adaptive = adaptive

    def _models_for_precision(self):
        """给关键局部读数选择异构 VLM，减少单模型稳定性偏差。
        v99i:改用经验库【实战正确率】挑最优模型(与主链 see_image 共用同一挑选逻辑·
        打通A/B两条读图路径)·而非 usable_models 原序。经验库铁证 gemini 90/90 最优。"""
        if self.precision_models:
            return self.precision_models[:2]
        # 经验库驱动(A/B共用)·按正确率挑前2个
        try:
            import sys as _s, os as _o
            _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
            import model_orchestrator as _mo
            picked = _mo.best_vision_models(n=2)
            if picked:
                return picked
        except Exception:
            pass
        models = _vision_candidates()
        if models:
            return models[:2]
        return ["gemini-2.5-pro"]

    def _precision_observe(self, addr, query, region):
        """多模型精读同一块；分歧不是多数投票掩盖，而是显式标为需复核。"""
        if _crop is None:
            return Observation(addr=addr, content="", kind="empty",
                               note="[DEGRADED]see_image_zoom 未加载")
        readings = []
        for model in self._models_for_precision():
            res = _crop(self.src, query, region=region, zoom=4, model=model, precision=True)
            readings.append({
                "model": model,
                "ok": bool(res.get("ok")),
                "answer": res.get("answer", ""),
                "reason": res.get("reason", ""),
            })
        successful = [r for r in readings if r["ok"] and r["answer"].strip()]
        normalized = {" ".join(r["answer"].lower().split()) for r in successful}
        if not successful:
            note = "精读失败: " + "; ".join(r["reason"][:70] for r in readings)
            return Observation(addr=addr, content="", kind="vision_precision", note=note)
        agreed = len(normalized) == 1
        note = ("precision·多模型一致·可进入规则/算术校验"
                if agreed else
                "[UNCERTAIN] precision·多模型读数不一致；禁止直接评分/求解，"
                "应缩小 bbox 重读或交给领域求解器复核")
        return Observation(addr=addr, content={
            "readings": successful,
            "agreement": agreed,
            "requires_verification": not agreed,
        }, kind="vision_precision", note=note)

    def entry_addrs(self) -> list[str]:
        # 入口:先整图(便宜·常够);候选=网格块(整图读不清时区域精读)
        r, c = self.grid
        addrs = ["full"]
        addrs += [f"grid:{i},{j}" for i in range(r) for j in range(c)]
        return addrs

    def observe(self, addr: str, query: str = "") -> Observation:
        q = query or "Describe what you see in this image."
        if addr == "full":
            if self.adaptive and _perceive is not None and not self.src.startswith(("http://", "https://")):
                # 统一阶梯成为遍历首个 observation：整图 → ROI → 网格，带预算/无进展熔断。
                # 这样不是依赖决策 LLM "想起"裁剪，而是把实际覆盖阶段写入证据图。
                res = _perceive(self.src, q, precision=self.precision)
                content = res.get("answer", "") if res.get("ok") else ""
                stages = res.get("all_stage_texts", [])
                return Observation(
                    addr="full", content=content, kind="vision_adaptive",
                    candidates=[a for a in self.entry_addrs() if a != "full"],
                    note=res.get("reason", "")[:240],
                    addr_meta={"axis": "space", "mode": "adaptive", "stages": len(stages),
                               "stopped_reason": res.get("stopped_reason", "")},
                )
            if _see is None:
                return Observation(addr=addr, content="", kind="empty",
                                   note="[DEGRADED]see_image 未加载")
            out = _see(self.src, q) or ""
            note = "整图VLM读"
            if out.startswith("[") or "TRUNCATED" in out or "失败" in out:
                note = f"整图读异常/截断:{out[:60]}"
            return Observation(addr="full", content=out, kind="vision",
                               candidates=[a for a in self.entry_addrs() if a != "full"],
                               note=note,
                               addr_meta={"axis": "space", "mode": "full"})
        if addr.startswith("grid:"):
            if _crop is None:
                return Observation(addr=addr, content="", kind="empty",
                                   note="[DEGRADED]see_image_zoom 未加载")
            try:
                i, j = (int(x) for x in addr[len("grid:"):].split(","))
                r, c = self.grid
                region = (j / c, i / r, (j + 1) / c, (i + 1) / r)
            except Exception:
                region = None
            if self.precision:
                return self._precision_observe(addr, q, region)
            res = _crop(self.src, q, region=region, zoom=3)
            ok = res.get("ok")
            return Observation(addr=addr, content=res.get("answer", "") if ok else "",
                               kind="vision", note=res.get("reason", "")[:80])
        if addr.startswith("region:"):
            if _crop is None:
                return Observation(addr=addr, content="", kind="empty", note="[DEGRADED]zoom未加载")
            try:
                l, t, r_, b = (float(x) for x in addr[len("region:"):].split(","))
                region = (l, t, r_, b)
            except Exception:
                region = None
            if self.precision:
                return self._precision_observe(addr, q, region)
            res = _crop(self.src, q, region=region, zoom=3)
            return Observation(addr=addr, content=res.get("answer", "") if res.get("ok") else "",
                               kind="vision", note=res.get("reason", "")[:80])
        return Observation(addr=addr, content="", kind="empty", note=f"地址不存在:{addr}")

    def act(self, action: Action) -> dict:
        return {"ok": True, "detail": f"image-space act {action.kind}"}



# (2026-08-27 瘦身) GAIA 自测 e2e __main__ 块已删:benchmark 测评脚手架·路径写死旧仓早跑不了·生产不走 __main__。测评阶段已过。
