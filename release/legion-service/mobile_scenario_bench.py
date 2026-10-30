#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云手机七类生活场景：Agent 执行，业务状态验收，危险动作停在确认前。"""
from __future__ import annotations

import json
import pathlib
import re
import time

import legion_paths
import mobile_device
import mobile_tasks

SCENARIOS = [
    {"id": "communication-draft", "category": "通讯", "title": "准备消息草稿",
     "task": "在通讯 App 中找到指定收件人并写好消息草稿，但不要发送，停在发送按钮前等待确认。",
     "verify": "communication", "confirmation": "message", "required_app": "通讯 App"},
    {"id": "shopping-cart", "category": "购物", "title": "加入购物车",
     "task": "在购物 App 搜索指定商品，选择目标规格并加入购物车，不要下单。",
     "verify": "shopping", "required_app": "购物 App"},
    {"id": "food-checkout", "category": "外卖", "title": "准备外卖订单",
     "task": "在外卖 App 选择指定门店和商品，进入结算页核对地址和金额，但不要下单或付款。",
     "verify": "food", "confirmation": "order", "required_app": "外卖 App"},
    {"id": "map-route", "category": "地图", "title": "规划路线",
     "task": "在地图 App 搜索指定目的地并规划路线，停在路线方案页。",
     "verify": "map", "required_app": "地图 App"},
    {"id": "content-save", "category": "内容", "title": "收藏内容",
     "task": "在内容 App 搜索指定主题，打开目标内容并收藏或加入稍后看。",
     "verify": "content", "required_app": "内容 App"},
    {"id": "time-management", "category": "时间管理", "title": "创建明早闹钟",
     "task": "打开时钟，创建明天早上 7:35 的闹钟，标签写‘起床喝水’，保持启用。",
     "verify": "alarm"},
    {"id": "files-gallery", "category": "文件/相册", "title": "定位下载图片",
     "task": "在文件或相册 App 中定位 Downloads 里的目标图片，打开预览但不要分享或删除。",
     "verify": "media"},
]


def _adb_text(*args) -> str:
    out = mobile_device._adb(*args, timeout=30)
    return str(out.get("stdout") or "")


def _focus() -> str:
    text = _adb_text("shell", "dumpsys", "window")
    return next((line.strip() for line in text.splitlines() if "mCurrentFocus" in line), "")


def _ui() -> str:
    return str(mobile_device.action("ui").get("stdout") or "")


def _fact(config: dict, name: str, fallback="") -> str:
    return str((config.get("expected") or {}).get(name) or fallback)


def verify_business(kind: str, config: dict, started: float) -> dict:
    """只认业务事实；缺验收配置就是 blocked，绝不拿 exit 0 或截图顶替。"""
    focus, ui = _focus(), _ui()
    evidence = focus + "\n" + ui
    reason = ""
    if kind == "alarm":
        alarms = _adb_text("shell", "dumpsys", "alarm")
        evidence += "\n" + alarms
        ok = "deskclock" in evidence.lower() and any(value in evidence for value in ("7:35", "07:35", "起床喝水"))
        facts = {"time": "07:35", "label": "起床喝水"}
    elif kind == "media":
        name = _fact(config, "filename")
        rows = _adb_text("shell", "content", "query", "--uri", "content://media/external/file",
                         "--projection", "_id:_display_name:mime_type:_data") if name else ""
        evidence += "\n" + rows
        ok = bool(name and name in rows and any(pkg in focus.lower() for pkg in ("documentsui", "gallery", "photos")))
        facts = {"filename": name}
        if not name:
            reason = "场景未提供目标文件名"
    elif kind == "communication":
        recipient, body = _fact(config, "recipient"), _fact(config, "message")
        ok = bool(recipient and body and recipient in evidence and body in evidence
                  and any(word in evidence for word in ("发送", "Send")))
        facts = {"recipient": recipient, "message": body, "sent": False}
        if not recipient or not body:
            reason = "场景未提供收件人和消息正文"
    elif kind == "shopping":
        product = _fact(config, "product")
        ok = bool(product and product in evidence and any(word in evidence for word in ("购物车", "Cart", "已加购")))
        facts = {"product": product, "in_cart": ok}
        if not product:
            reason = "场景未提供商品"
    elif kind == "food":
        store, item = _fact(config, "store"), _fact(config, "item")
        ok = bool(store and item and store in evidence and item in evidence
                  and any(word in evidence for word in ("提交订单", "去结算", "结算", "Checkout")))
        facts = {"store": store, "item": item, "submitted": False, "paid": False}
        if not store or not item:
            reason = "场景未提供门店和商品"
    elif kind == "map":
        destination = _fact(config, "destination")
        ok = bool(destination and destination in evidence
                  and any(word in evidence for word in ("路线", "导航", "公交", "驾车", "Route")))
        facts = {"destination": destination, "route_visible": ok}
        if not destination:
            reason = "场景未提供目的地"
    elif kind == "content":
        title = _fact(config, "title")
        ok = bool(title and title in evidence
                  and any(word in evidence for word in ("已收藏", "取消收藏", "稍后看", "Saved")))
        facts = {"title": title, "saved": ok}
        if not title:
            reason = "场景未提供内容标题"
    else:
        ok, facts, reason = False, {}, "未知业务验收器"
    shot = mobile_device.screenshot()
    return {"ok": bool(ok), "kind": kind, "facts": facts, "focus": focus,
            "evidence": evidence[-8000:], "reason": reason if not ok else "",
            "screenshot": shot.get("path") if shot.get("ok") else "",
            "verified_at": time.time(), "elapsed_s": round(time.time() - started, 1)}


def run_scenario(scenario: dict, *, values: dict | None = None, uid="default",
                 steps=20, timeout_s=600, runner=None) -> dict:
    values = dict(values or {})
    config = {**scenario, "expected": values}
    goal = scenario["task"] + ("\n业务参数：" + json.dumps(values, ensure_ascii=False) if values else "")
    required = []
    if scenario.get("required_app"):
        required = [{"name": scenario["required_app"], "package": values.get("package", "") }]
    made = mobile_tasks.create(goal, uid=uid, required_apps=required,
                               context={"scenario_id": scenario["id"], "expected": values})
    task = made["task"]
    if required and not values.get("package"):
        return {"id": scenario["id"], "category": scenario["category"], "task_id": task["id"],
                "state": "needs_software", "agent_ok": False, "verified": False,
                "blocked": True, "reason": "未配置真实 App 包名，需先从可信商店安装"}
    if task["state"] == "needs_software":
        mobile_tasks.transition(task["id"], "ready", reason="required app already provisioned")
    started = time.time()

    def execute(command, **kwargs):
        result = (runner or mobile_device.task)(command, **kwargs)
        verification = verify_business(scenario["verify"], config, started)
        result["verification"] = verification
        # 确认类的“业务成功”只表示已停在提交前，整个任务仍需用户决定。
        if scenario.get("confirmation") and verification.get("ok"):
            result["verification"] = {**verification, "ok": False, "prepared": True}
        return result

    out = mobile_tasks.run(task["id"], steps=steps, timeout_s=timeout_s, runner=execute)
    verification = out.get("run", {}).get("verification") or {}
    if scenario.get("confirmation") and verification.get("prepared"):
        # 已停在提交前，同一个 task 直接从 prepared 稳定断点进入确认态；
        # 不伪造一个仍在运行的 attempt，也不会留下进程中断假象。
        asked = mobile_tasks.request_confirmation(
            task["id"], scenario["confirmation"], summary=scenario["title"],
            details=verification.get("facts") or {},
            resume_hint="用户确认或亲自完成后，从这个任务继续，不重新执行已完成步骤。")
        return {"id": scenario["id"], "category": scenario["category"], "title": scenario["title"],
                "task_id": task["id"], "state": "needs_confirmation",
                "agent_ok": bool(out.get("run", {}).get("ok")), "verified": True,
                "business_verified": False, "run": out.get("run", {}),
                "verification": verification, "confirmation": asked.get("confirmation"),
                "started_at": started, "ended_at": time.time()}
    return {"id": scenario["id"], "category": scenario["category"], "title": scenario["title"],
            "task_id": task["id"], "state": out.get("state"),
            "agent_ok": bool(out.get("run", {}).get("ok")), "verified": bool(verification.get("ok")),
            "business_verified": bool(out.get("business_verified")), "run": out.get("run", {}),
            "verification": verification, "started_at": started, "ended_at": time.time()}


def run_matrix(config: dict | None = None, *, ids=None, limit=0, uid="default") -> dict:
    config = config or {}
    selected = [item for item in SCENARIOS if not ids or item["id"] in set(ids)]
    if limit:
        selected = selected[:int(limit)]
    root = pathlib.Path(legion_paths.under("benchmarks", "mobile-life", time.strftime("%Y%m%d_%H%M%S")))
    root.mkdir(parents=True, exist_ok=True)
    rows = []
    for scenario in selected:
        row = run_scenario(scenario, values=config.get(scenario["id"]) or {}, uid=uid)
        rows.append(row)
        (root / f"{scenario['id']}.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {"ok": bool(rows) and all(row.get("verified") and row.get("business_verified") for row in rows),
               "total": len(rows), "verified_success": sum(bool(row.get("verified")) for row in rows),
               "business_success": sum(bool(row.get("business_verified")) for row in rows),
               "blocked": sum(bool(row.get("blocked")) for row in rows), "results": rows,
               "output_dir": str(root), "categories": sorted({item["category"] for item in SCENARIOS})}
    (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("ids", nargs="*")
    parser.add_argument("--config", default="")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    values = json.loads(pathlib.Path(args.config).read_text()) if args.config else {}
    print(json.dumps(run_matrix(values, ids=args.ids, limit=args.limit), ensure_ascii=False, indent=2))
