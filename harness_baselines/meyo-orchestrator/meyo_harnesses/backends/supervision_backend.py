# -*- coding: utf-8 -*-
"""supervision(roboflow CV)后端 adapter。

真装真测:pip 装 `supervision` 后即可用其 CV 原语(检测框处理/标注/几何)。
本 adapter 提供一个客观的视觉工具入口:给定动作+入参,调用 supervision 能力。
不做任何"该不该用视觉"的路由——那由 captain 模型自主决定。
"""
from __future__ import annotations
import time


def probe() -> dict:
    """诚实探测:supervision 是否可导入。"""
    try:
        import supervision as sv
        return {"backend": "supervision", "available": True,
                "version": getattr(sv, "__version__", "?")}
    except Exception as e:
        return {"backend": "supervision", "available": False,
                "reason": f"{type(e).__name__}:{str(e)[:120]}",
                "hint": "pip install --break-system-packages supervision"}


def run(qhash: str, task: str, gold: str = "") -> dict:
    """执行一个视觉动作。task 是自然语言/JSON 指令;当前支持连通性动作 'version'。

    真实 CV 推理需模型/图片输入,应经 call_mcp_tool 或后续扩展;此 runner 先保证
    'supervision 真装即真可调'的闭环(version/能力自述),不假跑。
    """
    t0 = time.time()
    p = probe()
    if not p["available"]:
        return {"harness": "supervision", "final": "", "status": "unavailable",
                "reason": p.get("reason"), "hint": p.get("hint"),
                "elapsed": round(time.time() - t0, 1)}
    import supervision as sv
    caps = [n for n in ("Detections", "BoxAnnotator", "ByteTrack", "PolygonZone")
            if hasattr(sv, n)]
    return {"harness": "supervision", "status": "ok",
            "final": f"supervision {sv.__version__} 可用·CV能力:{caps}",
            "version": sv.__version__, "capabilities": caps,
            "elapsed": round(time.time() - t0, 1)}
