"""space_gui · 2026-08-13 · GUI/桌面操作空间(能动·可写)的 Space 适配器(挂 traversal_core)

这是最"像人"、也最危险的空间——用户问的"挪动图面/点击/键盘"。
与前五个【只读信息空间】的本质区别:GUI 是【可写、不可逆】能动空间。
  · observe(读屏): screenshot + VLM 看当前屏幕(只读·无副作用·不需审批)
  · act(操作):    click_at / type_text / press_key —— 改变真实机器状态、不可逆

安全红线(项目宪法·记忆反复强调·不可绕过):
  1. 所有【写操作】(click/type/key)执行前【必须过 hitl_gate.request_approval 人工审批】,
     批准才真点/真打;拒绝/超时则不执行,回传结果。—— 点错删文件/发消息不可逆。
  2. 系统层双重门:screenshot 需"屏幕录制"权限、click/type 需"辅助功能"权限,
     desktop_gui.is_available()/is_screenshot_available() 会先探测·没授权明确回传不静默。
  这两道门 desktop_gui + hitl_gate 已实现,本适配器只是【确保 act 走审批】,不新开后门。

地址方案:
  · "screen"           → 截屏+VLM 读整屏(observe)
  · "window:{name}"     → 截某窗口读
  · "region:{x},{y},{w},{h}" → 截屏指定矩形区域精读(UI元素级寻址)
  · "grid:{r},{c}"      → 屏幕网格分块(粗粒度区域定位)

动作(decide 产出·经骨架路由到 act):
  · delegate/compute 不适用;GUI 用 act 的 target 编码操作:
    "click:{x},{y}" / "type:{text}" / "key:{keyname}" / "activate:{app}"
  操作前 act() 内部调 request_approval;这是能动空间的 act 真副作用。

DOM 语义映射(v99p · 页面 DOM 语义与屏幕元素的映射):
  observe "screen" 时 VLM 不仅描述画面·还抽取可交互元素列表(buttons/links/inputs),
  每个元素附带估算的 bbox 坐标→生成 region: 地址候选,让决策器能定向精读/操作某个 UI 元素。
  这不是完整 OCR/DOM 解析——是 VLM 从截屏中提取的语义元素→坐标映射。

只做寻址+安全执行,零新增遍历逻辑。
"""
from __future__ import annotations
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from traversal_core import Observation, Action

try:
    import desktop_gui as _gui
except Exception:
    _gui = None
try:
    import hitl_gate as _hitl
except Exception:
    _hitl = None
try:
    from see_image import see as _see
except Exception:
    _see = None


class GuiSpace:
    name = "gui"
    kind = "agentic"          # 能动空间(可写·不可逆)——区别于信息空间

    def __init__(self, qid: str = "gui", *, require_approval: bool = True):
        self.qid = qid
        self.require_approval = require_approval  # 默认 True·写操作必审批(红线)

    def entry_addrs(self) -> list[str]:
        return ["screen"]

    @staticmethod
    def _parse_elements_from_vlm(desc: str) -> list[dict]:
        """从 VLM 描述中解析可交互元素及其位置(v99p DOM映射)。
        VLM 返回的格式约定:每行 `[elem] (x,y,w,h) text` 或 JSON 列表。
        fail-safe:解析失败返回空列表(不影响主流程)。"""
        import re as _re
        try:
            elements = []
            # 尝试 JSON 列表格式
            m = _re.search(r'\[.*\]', desc, _re.S)
            if m:
                import json as _json
                try:
                    arr = _json.loads(m.group(0))
                    if isinstance(arr, list):
                        for item in arr:
                            if isinstance(item, dict) and "bbox" in item:
                                elements.append(item)
                            elif isinstance(item, dict) and "text" in item:
                                elements.append(item)
                except Exception:
                    pass
            # 退回正则: `(x,y,w,h) text` 格式
            if not elements:
                for m in _re.finditer(r'\(?([\d,.\s]+)\)\s*([^\n]+)', desc):
                    coords_str = m.group(1)
                    text = m.group(2).strip()[:80]
                    coords = [int(float(x.strip())) for x in coords_str.split(',') if x.strip()]
                    if len(coords) >= 4 and text:
                        x, y = coords[0], coords[1]
                        elements.append({"text": text, "bbox": coords[:4]})
            return elements[:20]
        except Exception:
            return []

    def _take_screenshot(self, addr: str) -> tuple[str, str]:
        """截屏并返回 (img_path, error_note)。成功时 error_note 为空。"""
        avail = _gui.is_screenshot_available()
        if not avail.get("ok"):
            return "", f"无屏幕录制权限:{avail.get('reason','')[:80]}·需系统授权"
        win = ""
        if addr.startswith("window:"):
            win = addr[len("window:"):]
        shot = _gui.screenshot(window_name=win)
        if not shot.get("ok"):
            return "", f"截屏失败:{shot.get('reason','')[:80]}"
        return shot.get("path", ""), ""

    def observe(self, addr: str, query: str = "") -> Observation:
        """读屏:screenshot + VLM 看。只读,无副作用,不需审批。
        v99p: screen/window → VLM 读整屏+DOM元素坐标映射;region/grid → 区域精读。"""
        if _gui is None:
            return Observation(addr=addr, content="", kind="empty",
                               note="[DEGRADED]desktop_gui 未加载")
        # region/grid 分支:先截屏(复用上次路径或重拍),再定向精读
        if addr.startswith("region:"):
            try:
                x, y, w, h = (int(float(v)) for v in addr[len("region:"):].split(","))
            except Exception:
                return Observation(addr=addr, content="", kind="empty", note=f"非法区域:{addr}")
            # 截屏(区域精读需要当前画面)
            img_path, err = self._take_screenshot(addr)
            if err:
                return Observation(addr=addr, content="", kind="empty", note=err)
            if _see is not None and img_path:
                _region_query = (query or f"Read the UI element at region ({x},{y},{w},{h})") + (
                    f"\nFocus only on the screen region bbox=({x},{y},{w},{h})."
                )
                desc_r = _see(img_path, _region_query) or ""
                return Observation(addr=addr, content=desc_r, kind="screen_region",
                                   candidates=[], note=f"区域({x},{y},{w},{h})精读",
                                   addr_meta={"axis": "screen", "bbox": [x, y, w, h]})
            return Observation(addr=addr, content="", kind="empty", note="VLM不可用·无法精读区域")
        if addr.startswith("grid:"):
            try:
                r, c = (int(x) for x in addr[len("grid:"):].split(","))
            except Exception:
                return Observation(addr=addr, content="", kind="empty", note=f"非法网格:{addr}")
            img_path, err = self._take_screenshot(addr)
            if err:
                return Observation(addr=addr, content="", kind="empty", note=err)
            if _see is not None and img_path:
                _grid_query = (query or "Describe") + f" grid block ({r},{c})"
                desc_g = _see(img_path, _grid_query) or ""
                return Observation(addr=addr, content=desc_g, kind="screen_grid",
                                   candidates=[f"grid:{i},{j}" for i in range(3) for j in range(3) if (i, j) != (r, c)],
                                   note=f"网格({r},{c})",
                                   addr_meta={"axis": "screen", "grid": [r, c]})
            return Observation(addr=addr, content="", kind="empty", note="VLM不可用")
        # screen / window 分支:整屏读 + DOM 元素映射
        img_path, err = self._take_screenshot(addr)
        if err:
            return Observation(addr=addr, content="", kind="empty", note=err)
        desc = ""
        if _see is not None and img_path:
            _dom_query = (query or "Describe the current screen") + (
                "\n\nList interactive elements (buttons/links/inputs/menu items) "
                "with their estimated bbox coordinates as JSON: "
                '[{"text":"button label","bbox":[x,y,w,h]}]'
            )
            desc = _see(img_path, _dom_query) or ""
        # 从 VLM 描述中解析 UI 元素 → 生成 region 地址候选
        elements = self._parse_elements_from_vlm(desc)
        region_cands = []
        for el in elements:
            bbox = el.get("bbox") or []
            if len(bbox) >= 4:
                x, y, w, h = bbox[0], bbox[1], bbox[2], bbox[3]
                region_cands.append(f"region:{x},{y},{w},{h}")
        grid_cands = [f"grid:{r},{c}" for r in range(3) for c in range(3)]
        return Observation(addr=addr, content=desc or f"截屏已存:{img_path}(VLM未读)",
                           kind="screen", candidates=list(dict.fromkeys(region_cands + grid_cands)),
                           note=f"screenshot={img_path}; elements={len(elements)}",
                           addr_meta={"axis": "screen", "img_path": img_path,
                                      "elements": elements})

    def act(self, action: Action) -> dict:
        """执行 GUI 操作。写操作(click/type/key)【强制过 hitl 审批】——安全红线。"""
        if _gui is None:
            return {"ok": False, "detail": "[DEGRADED]desktop_gui 未加载"}
        target = action.target or ""
        op, _, arg = target.partition(":")

        # 只读操作:list/activate 无破坏性(activate 只是切窗口·可逆)——不强制审批
        if op == "activate":
            return _gui.activate_app(arg)
        if op == "list":
            return _gui.list_windows()

        # 写操作:click/type/key —— 不可逆,必须先审批
        if op in ("click", "type", "key"):
            # 系统权限门(辅助功能)
            avail = _gui.is_available()
            if not avail.get("ok"):
                return {"ok": False, "detail": f"无辅助功能权限:{avail.get('reason','')[:80]}·需系统授权"}
            # hitl 审批门(红线·不可绕过)
            if self.require_approval:
                if _hitl is None:
                    return {"ok": False, "detail": "[SAFE]hitl_gate 不可用·拒绝执行不可逆GUI写操作"}
                kind = "desktop_click" if op == "click" else "desktop_type"
                verdict = _hitl.request_approval(self.qid, kind, f"{op}:{arg}")
                if verdict.get("decision") != "approved":
                    return {"ok": False, "detail": f"[BLOCKED]用户未批准({verdict.get('decision')})·未执行 {op}:{arg}"}
            # 批准 → 真执行
            try:
                if op == "click":
                    x, y = (int(v) for v in arg.split(","))
                    return _gui.click_at(x, y)
                if op == "type":
                    return _gui.type_text(arg)
                if op == "key":
                    return _gui.press_key(arg)
            except Exception as e:
                return {"ok": False, "detail": f"GUI执行异常:{type(e).__name__}:{e}"}
        return {"ok": False, "detail": f"未知GUI操作:{target}"}


if __name__ == "__main__":
    # dry-run:验证权限门/审批门逻辑(不真操作·不需授权环境·不 re-import 避免重入)
    import types
    sp = GuiSpace("guitest")
    print("entry:", sp.entry_addrs())
    g = globals()  # 直接 patch 本模块级变量,不 re-import

    # 1) 红线:无 hitl 批准 → 写操作必须被 BLOCKED
    g["_hitl"] = types.SimpleNamespace(request_approval=lambda *a, **k: {"decision": "denied"})
    g["_gui"] = types.SimpleNamespace(is_available=lambda: {"ok": True})
    r = sp.act(Action(kind="act", target="click:100,200"))
    assert not r["ok"] and "BLOCKED" in r["detail"], f"红线失效!未审批却执行: {r}"
    print(f"安全红线 self-test ✓ · 未批准的click被拦: {r['detail'][:50]}")

    # 2) 批准 → 放行执行
    g["_hitl"] = types.SimpleNamespace(request_approval=lambda *a, **k: {"decision": "approved"})
    g["_gui"] = types.SimpleNamespace(is_available=lambda: {"ok": True},
                                       click_at=lambda x, y: {"ok": True, "detail": f"clicked {x},{y}"})
    r2 = sp.act(Action(kind="act", target="click:100,200"))
    assert r2["ok"], f"批准后应执行: {r2}"
    print(f"批准后放行 self-test ✓ · {r2['detail']}")

    # 3) hitl 不可用 → fail-safe 拒绝(不是放行!不可逆操作宁可不做)
    g["_hitl"] = None
    r3 = sp.act(Action(kind="act", target="type:hello"))
    assert not r3["ok"] and "SAFE" in r3["detail"], f"fail-safe 失效: {r3}"
    print(f"fail-safe self-test ✓ · hitl不可用时拒绝写操作: {r3['detail'][:50]}")
