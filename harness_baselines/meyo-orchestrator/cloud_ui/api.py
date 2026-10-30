# -*- coding: utf-8 -*-
"""云 UI 编排 API(生产化版)—— 鉴权/多用户隔离 + 持久化 + SSE 进度 + 取消/重试 +
可选真 Omnigent 执行后端。

护栏与 autoloop 一致:gold 永不接受(防作弊);qhash 可选(默认 adhoc)。
鉴权:X-Meyo-Token 头 → 用户;token 映射来自环境 MEYO_API_TOKENS="tok1:user1,tok2:user2"。
  本地开发可设 MEYO_AUTH_DISABLED=1(所有请求归 'local' 用户)。生产必须配 token。
"""
from __future__ import annotations
import json, os, sys, threading, time, uuid
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "meyo_harnesses"))

from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

import orchestrator as O
import mcp_bridge as MB
import skills as SK
import templates as ST
import health as H
import cascade as CX
import autoloop as AL
import leader_registry as LR
from cloud_ui.store import RunStore
from cloud_ui import omnigent_runner as OR
from captain_context import build_context
from omnigent_runtime_manager import OmnigentRuntimeManager

app = FastAPI(title="meyo-orchestrator cloud API", version="0.2")

STORE = RunStore(Path(os.environ.get("MEYO_RUN_DB", str(_ROOT / ".cloud" / "runs.db"))))
STORE.recover_interrupted()   # 重启:queued/running → interrupted(保留 trace)

# run_id → {"thread":..., "cancel":Event}(内存·仅活跃跑用;状态本身在 STORE)
_ACTIVE: dict[str, dict] = {}
_DEFAULT_EXECUTOR = os.environ.get("MEYO_EXECUTOR", LR.select_default()["key"])
_RUNTIME = OmnigentRuntimeManager(
    omnigent_root=_ROOT.parent / "ecosystem" / "omnigent",
    orchestrator_root=_ROOT,
    server_url=os.environ.get("MEYO_OMNIGENT_URL"),
)
_RUNTIME_ENABLED = os.environ.get("MEYO_MANAGE_OMNIGENT_RUNTIME", "1") == "1"


# ── 鉴权 / 多用户隔离 ────────────────────────────────────────────
def _token_map() -> dict[str, str]:
    raw = os.environ.get("MEYO_API_TOKENS", "").strip()
    out = {}
    for pair in raw.split(","):
        if ":" in pair:
            tok, user = pair.split(":", 1)
            out[tok.strip()] = user.strip()
    return out


def current_user(x_meyo_token: str | None = Header(default=None)) -> str:
    if os.environ.get("MEYO_AUTH_DISABLED") == "1":
        return "local"
    mapping = _token_map()
    if not mapping:
        raise HTTPException(503, "auth not configured: set MEYO_API_TOKENS or MEYO_AUTH_DISABLED=1")
    user = mapping.get((x_meyo_token or "").strip())
    if not user:
        raise HTTPException(401, "invalid or missing X-Meyo-Token")
    return user


class SolveReq(BaseModel):
    goal: str
    qhash: str | None = None
    executor: str | None = None       # autoloop | captain | omnigent(默认取环境)


# 执行家能力/状态(诚实):Omnigent supervisor 已通过标准 MCP 取得统一军团 registry，
# 同时具备原生 session/spawn/inbox 生命周期；autoloop 保留为最小依赖降级通道。
_EXECUTORS = {
    "cascade": {"available": True, "kind": "confidence-cascade",
                "verified": True, "benchmark": "CC 17/40 → cascade 21/40"},
    "autoloop": {"available": True, "kind": "self-loop", "verified": True},
    "captain": {"available": True, "kind": "real-claude+mcp", "verified": True},
    "omnigent": {"available": True, "kind": "omnigent-supervisor",
                 "verified": True, "fallback": "autoloop"},
}


@app.get("/health")
def health():
    return {"status": "ok", "tools": len(AL.TOOLS_IMPL),
            "executor_default": _DEFAULT_EXECUTOR, "executors": _EXECUTORS,
            "leaders": LR.list_leaders(),
            "runtime_management": {"enabled": _RUNTIME_ENABLED,
                                   "user_action_required": False}}


@app.get("/harnesses")
def harnesses(user: str = Depends(current_user)):
    return O.list_harnesses()


@app.get("/tools/mcp")
def mcp_tools(user: str = Depends(current_user)):
    return MB.list_mcp_tools()


@app.get("/skills")
def skill_list(source: str = "pm-skills", user: str = Depends(current_user)):
    return SK.list_skills(source)


@app.get("/templates")
def template_list(user: str = Depends(current_user)):
    return ST.list_templates()


class TemplateReq(BaseModel):
    params: dict[str, Any] = {}


@app.post("/templates/{key}/instantiate")
def template_instantiate(key: str, req: TemplateReq,
                         user: str = Depends(current_user)):
    out = ST.instantiate_template(key, req.params)
    if out.get("error") == "unknown_template":
        raise HTTPException(404, "unknown template")
    return out


@app.get("/health/snapshot")
def health_snapshot(user: str = Depends(current_user)):
    return H.health_snapshot(_ROOT / "runs")


def _captain_context_facts(run_id: str, user: str, goal: str, qhash: str,
                           run: dict) -> dict:
    history = []
    try:
        history = [
            {"goal": row.get("goal"), "final": row.get("final"),
             "status": row.get("status"), "executor": row.get("executor")}
            for row in STORE.list(user)[-10:] if row.get("run_id") != run_id
        ]
    except Exception:
        pass
    memory, task_facts, toolbox_summary, permissions = [], {}, {}, {}
    service = _ROOT.parents[1] / "release" / "legion-service"
    if str(service) not in sys.path:
        sys.path.insert(0, str(service))
    try:
        import legion_memory
        memory = list((legion_memory.search(goal, top=3, user_id=user) or {}).get("results") or [])
    except Exception:
        pass
    try:
        import task_memory
        task_facts = task_memory.context(qhash, "captain")
    except Exception:
        pass
    try:
        import toolbox
        toolbox_summary = dict((toolbox.scan_toolbox() or {}).get("summary") or {})
    except Exception:
        pass
    try:
        import legion_permissions
        permissions = legion_permissions.capability_policy()
    except Exception:
        pass
    artifacts = list(run.get("artifacts") or [])
    return {
        "acceptance": list(run.get("acceptance") or []),
        "conversation_history": history,
        "recent_events": list(run.get("steps") or []),
        "long_term_memory": memory,
        "task_memory": task_facts,
        "toolbox": toolbox_summary,
        "permissions": permissions,
        "budgets": {"max_steps": 12, "child_timeout_s": 180},
        "delivery_contract": dict(run.get("delivery_contract") or {
            "minimum": "完整最终答案", "no_waiting_as_final": True,
        }),
        "artifacts": artifacts,
    }


def _run_worker(run_id: str, user: str, goal: str, qhash: str, executor: str, cancel):
    run = STORE.get(run_id, user) or {}
    run.update(status="running")
    STORE.put(run)
    try:
        facts = _captain_context_facts(run_id, user, goal, qhash, run)
        context = build_context(goal, **facts)
        context_block = context.prompt_block()
        runtime_status = None
        if executor == "omnigent" and _RUNTIME_ENABLED:
            runtime_status = _RUNTIME.ensure_ready(timeout_s=20)
            if not runtime_status.ready:
                executor = "autoloop"
                run["fallback_from"] = "omnigent"
                run["fallback_to"] = "autoloop"
                run["runtime_recovery"] = runtime_status.error
        if executor == "cascade":
            out = CX.run_cascade(goal, qhash=qhash, gold="")
            run.update(status="done" if out.get("status") not in (None, "") else "error",
                       final=out.get("final", ""), executor="cascade",
                       chosen=out.get("chosen"), escalated=out.get("escalated"),
                       route=out.get("route"))
        elif executor in ("omnigent", "captain"):
            def _supervisor_event(event):
                latest = STORE.get(run_id, user) or run
                steps = list(latest.get("steps") or [])
                steps.append({"type": "supervisor", **event})
                latest["steps"] = steps[-200:]
                STORE.put(latest)

            out = (OR.run_via_captain_mcp(goal, qhash=qhash, context_block=context_block)
                   if executor == "captain"
                   else OR.run_via_omnigent(
                       goal, qhash=qhash, context_block=context_block,
                       event_sink=_supervisor_event,
                       runtime_url=(runtime_status.server_url if runtime_status else ""),
                   ))
            final = str(out.get("final", ""))
            incomplete = (out.get("status") != "ok" or (
                executor == "omnigent" and (
                    len(final.strip()) < 20
                    or any(mark in final for mark in ("仍在运行", "等待", "尚未完成"))
                )
            ))
            if incomplete and executor == "omnigent":
                fallback = AL.autonomous_solve(goal, verbose=False, qhash=qhash, gold="",
                                               cancel_event=cancel)
                fallback_final = str(fallback.get("final", ""))
                if fallback_final and "[达步数上限未收敛]" not in fallback_final:
                    out = fallback
                    final = fallback_final
                    run["fallback_from"] = "omnigent"
                    run["fallback_to"] = "autoloop"
                    incomplete = False
            run.update(status="error" if incomplete else "done",
                       final=final, executor=executor,
                       error=("incomplete_delivery" if incomplete else None),
                       supervisor_scores=out.get("scores"),
                       supervisor_attempts=out.get("attempts"))
        else:
            out = AL.autonomous_solve(
                goal + "\n\n" + context_block, verbose=False,
                qhash=qhash, gold="", cancel_event=cancel,
            )
            st = out.get("status")
            run.update(status=("interrupted" if st == "interrupted" else "done"),
                       final=out.get("final", ""), n_steps=out.get("n_steps"),
                       steps=out.get("steps", []), trace_path=out.get("trace_path", ""),
                       executor="autoloop",
                       interrupt_reason=out.get("interrupt_reason"))
    except Exception as e:
        run.update(status="error", error=f"{type(e).__name__}:{str(e)[:200]}")
    STORE.put(run)
    _ACTIVE.pop(run_id, None)


def _launch(user: str, goal: str, qhash: str, executor: str) -> str:
    run_id = uuid.uuid4().hex[:12]
    STORE.put({"run_id": run_id, "user_id": user, "goal": goal, "qhash": qhash,
               "status": "queued", "steps": [], "executor": executor})
    cancel = threading.Event()
    t = threading.Thread(target=_run_worker,
                         args=(run_id, user, goal, qhash, executor, cancel), daemon=True)
    _ACTIVE[run_id] = {"thread": t, "cancel": cancel}
    t.start()
    return run_id


@app.post("/solve")
def solve(req: SolveReq, user: str = Depends(current_user)):
    if not req.goal.strip():
        raise HTTPException(400, "goal required")
    executor = (req.executor or _DEFAULT_EXECUTOR)
    if executor not in _EXECUTORS:
        raise HTTPException(400, "executor must be autoloop|captain|omnigent")
    if not _EXECUTORS[executor]["available"]:
        raise HTTPException(503, _EXECUTORS[executor].get("note", "executor unavailable"))
    run_id = _launch(user, req.goal, req.qhash or "adhoc", executor)
    return {"run_id": run_id, "status": "queued", "executor": executor}


@app.get("/runs")
def runs(user: str = Depends(current_user)):
    return [{"run_id": r["run_id"], "goal": r["goal"][:120], "status": r["status"],
             "executor": r.get("executor"), "n_steps": r.get("n_steps")}
            for r in STORE.list(user)]


@app.get("/runs/{run_id}")
def run_detail(run_id: str, user: str = Depends(current_user)):
    r = STORE.get(run_id, user)
    if not r:
        raise HTTPException(404, "unknown run_id")
    return r


@app.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, user: str = Depends(current_user)):
    r = STORE.get(run_id, user)
    if not r:
        raise HTTPException(404, "unknown run_id")
    act = _ACTIVE.get(run_id)
    if act:
        act["cancel"].set()          # 协作式取消:loop 下一步检测到即中断
        return {"run_id": run_id, "status": "cancelling"}
    # 不在活跃集(已结束)→ 幂等
    return {"run_id": run_id, "status": r["status"]}


@app.post("/runs/{run_id}/retry")
def retry_run(run_id: str, user: str = Depends(current_user)):
    r = STORE.get(run_id, user)
    if not r:
        raise HTTPException(404, "unknown run_id")
    new_id = _launch(user, r["goal"], r.get("qhash", "adhoc"),
                     r.get("executor", _DEFAULT_EXECUTOR))
    return {"run_id": new_id, "status": "queued", "retried_from": run_id}


@app.get("/runs/{run_id}/events")
def run_events(run_id: str, user: str = Depends(current_user)):
    """SSE 实时进度:轮询 STORE 推 status/steps,直到终态。"""
    if not STORE.get(run_id, user):
        raise HTTPException(404, "unknown run_id")

    def gen():
        last = None
        for _ in range(600):          # 最长 ~10min 保护
            r = STORE.get(run_id, user) or {}
            payload = {"status": r.get("status"), "n_steps": r.get("n_steps"),
                       "steps": r.get("steps", []), "final": r.get("final", "")}
            blob = json.dumps(payload, ensure_ascii=False)
            if blob != last:
                yield f"data: {blob}\n\n"
                last = blob
            if r.get("status") in ("done", "error", "interrupted"):
                break
            time.sleep(1)
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/", response_class=HTMLResponse)
def index():
    return (_HERE / "index.html").read_text()
