#!/usr/bin/env python3
"""云电脑持久任务消费者：领取、续租、崩溃恢复与可注入执行器。"""
from __future__ import annotations

import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import desktop_workspace

_POLL_SEC = float(os.environ.get("LEGION_DESKTOP_WORKER_POLL_SEC", "3") or 3)
_LEASE_S = int(os.environ.get("LEGION_DESKTOP_WORKER_LEASE_SEC", "120") or 120)
_MAX_WORKERS = max(1, int(os.environ.get("LEGION_DESKTOP_WORKERS", "2") or 2))
_STOP = threading.Event()
_WAKE = threading.Event()
_THREAD: threading.Thread | None = None
_EXECUTOR: ThreadPoolExecutor | None = None
_ACTIVE: set[tuple[str, str, str]] = set()
_LOCK = threading.RLock()
_RUNNER: Callable[[dict], dict] | None = None
_EXCEPTION_RETRY_BASE_S = max(
    10.0, float(os.environ.get("LEGION_DESKTOP_EXCEPTION_RETRY_BASE_SEC", "30") or 30)
)
_EXCEPTION_RETRY_MAX_S = max(
    _EXCEPTION_RETRY_BASE_S,
    float(os.environ.get("LEGION_DESKTOP_EXCEPTION_RETRY_MAX_SEC", "900") or 900),
)
_MAX_REPEATED_EXCEPTION_RETRIES = max(
    1, int(os.environ.get("LEGION_DESKTOP_MAX_SAME_EXCEPTION_RETRIES", "3") or 3)
)
_MAX_EXCEPTION_RETRIES = max(
    _MAX_REPEATED_EXCEPTION_RETRIES,
    int(os.environ.get("LEGION_DESKTOP_MAX_EXCEPTION_RETRIES", "8") or 8),
)
_NON_RETRYABLE_RUNTIME_ERRORS = (
    AttributeError,
    ImportError,
    NameError,
    SyntaxError,
    TypeError,
)


def _exception_policy(task: dict, exc: Exception, attempt: int) -> dict:
    """Back off transient failures and pause deterministic/repeating code failures.

    A persistent task must survive infrastructure interruptions, but retrying the same
    local programming error every few seconds only relaunches the desktop and harnesses.
    The task remains recoverable in ``blocked`` and can be resumed after the code is fixed.
    """
    summary = f"{type(exc).__name__}: {exc}"[:4000]
    fingerprint = summary
    failures = list(task.get("failures") or [])
    repeated = 1
    for row in reversed(failures):
        prior = str(row.get("fingerprint") or row.get("summary") or "")
        if prior != fingerprint:
            break
        repeated += 1
    total_failures = 1 + sum(bool(row.get("side_effects_unknown")) for row in failures)
    deterministic = isinstance(exc, _NON_RETRYABLE_RUNTIME_ERRORS)
    retryable = (
        not deterministic
        and repeated < _MAX_REPEATED_EXCEPTION_RETRIES
        and total_failures < _MAX_EXCEPTION_RETRIES
    )
    delay_s = min(
        _EXCEPTION_RETRY_MAX_S,
        _EXCEPTION_RETRY_BASE_S * (2 ** max(0, repeated - 1)),
    ) if retryable else 0.0
    trace = traceback.format_exc(limit=20)[-12000:]
    return {
        "state": "queued" if retryable else "blocked",
        "phase": (
            f"运行异常，{int(delay_s)} 秒后重试（同类第 {repeated} 次）"
            if retryable else "重复或确定性运行错误，已暂停以防无限重试"
        ),
        "next_run_at": time.time() + delay_s if retryable else 0,
        "checkpoint": {
            **dict(task.get("checkpoint") or {}),
            "observe_before_resume": True,
            "runtime_failure": {
                "summary": summary,
                "fingerprint": fingerprint,
                "repeated": repeated,
                "total_failures": total_failures,
                "retryable": retryable,
                "retry_after_s": delay_s,
            },
        },
        "failure": {
            "summary": summary,
            "fingerprint": fingerprint,
            "attempt": attempt,
            "repeated": repeated,
            "total_failures": total_failures,
            "retryable": retryable,
            "side_effects_unknown": True,
            "traceback": trace,
        },
    }


def _default_runner(task: dict) -> dict:
    """按 execution_spec 选择持久工作流；研究办公任务必须经过真实 GUI 证据核验。"""
    import legion_core

    def dispatch(question: str) -> dict:
        return legion_core.dispatch(
            question,
            mode=str(task.get("mode") or "auto"),
            user_id=str(task.get("uid") or "default"),
            conversation_id=str(task.get("session_id") or ""),
            qhash=f"desktop_{task['task_id']}_a{task.get('active_attempt') or task.get('attempt_seq') or 1}",
        )

    kind = str((task.get("execution_spec") or {}).get("kind") or "desktop_agent")
    if kind in {"research_office", "desktop_research"}:
        import desktop_research_workflow
        return desktop_research_workflow.run(task, dispatch)
    if kind in {"quality_delivery", "frontier_delivery"}:
        import desktop_quality_workflow
        return desktop_quality_workflow.run(task, dispatch)
    if kind == "matrix_scenario":
        import desktop_production_matrix
        import desktop_quality_workflow
        scenario = str((task.get("execution_spec") or {}).get("scenario") or "")
        required = list((task.get("execution_spec") or {}).get("required_artifacts") or [])
        prompt = (f"在真实云电脑项目 {task['project_id']} 中执行生产矩阵场景 {scenario}：{task.get('goal') or ''}。"
                  f"必须生成 {required}，记录业务事实、真实打开和 GUI 截图；完成后只输出 JSON，包含 business_facts。")
        result = dispatch(prompt)
        output = desktop_quality_workflow._json_object(result.get("final_result") or "") if result else {}
        verification = desktop_production_matrix.verify_scenario(task["uid"], task["project_id"], scenario, output)
        if verification["ok"]:
            root = desktop_workspace._project_root(task["uid"], task["project_id"])
            reference = desktop_production_matrix._reference_bundle(
                root, scenario, verification, output,
                golden_task=(task.get("execution_spec") or {}).get("golden_task") or None,
            )
            return {"ok": True, "state": "completed", "phase": "同题参考生产完成",
                    "checkpoint": {"reference_bundle": reference["path"], "verification": verification}}
        return {"ok": True, "state": "queued", "phase": "同题参考生产未通过业务验收",
                "checkpoint": {"verification": verification, "observe_before_resume": True},
                "next_run_at": time.time() + 5}

    checkpoint = task.get("checkpoint") or {}
    resume = ""
    if checkpoint:
        resume = "\n\n恢复现场：" + __import__("json").dumps(checkpoint, ensure_ascii=False)
    if checkpoint.get("observe_before_resume") or task.get("observe_before_resume"):
        resume += "\n上次执行中断且外部副作用未知；第一步必须重新观察当前桌面和文件，禁止直接重放提交动作。"
    question = (
        f"请在云电脑持久项目 {task['project_id']} 中继续完成任务 {task['task_id']}："
        f"{task.get('goal') or ''}。读取项目 README 和 checkpoint；真实操作当前桌面软件，"
        "每阶段写回证据和恢复点；缺证据不得声称完成。" + resume
    )
    result = dispatch(question)
    delivered = int(result.get("delivered_count") or 0) > 0
    root = desktop_workspace._project_root(task["uid"], task["project_id"])
    files = desktop_workspace._collect_project_files(root)
    deliverables = [row for row in files if row["path"].startswith("deliverables/")]
    evidence = [row for row in files if row["path"].startswith("evidence/")]
    screenshots = [row for row in evidence
                   if row["path"].lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
    checkpoint = {
        "artifacts": [{"name": row["name"], "path": str(root / row["path"]),
                       "relative_path": row["path"], "bytes": row["bytes"]}
                      for row in deliverables + evidence],
        "screenshots": [str(root / row["path"]) for row in screenshots],
        "dispatch_delivered": delivered,
    }
    complete = delivered and bool(deliverables) and bool(evidence) and bool(screenshots)
    if complete:
        return {"ok": True, "state": "completed", "result": result,
                "checkpoint": checkpoint, "phase": "云端文件与截图已交付"}
    missing = [name for name, present in (("deliverables", deliverables),
                                           ("evidence", evidence), ("screenshot", screenshots))
               if not present]
    # 文字回答不是云电脑交付；把缺失事实写回并排队补救，不再称“本轮完成”。
    return {"ok": True, "state": "queued", "result": result,
            "checkpoint": {**checkpoint, "missing_delivery": missing,
                           "observe_before_resume": bool(files)},
            "next_run_at": time.time() + 5,
            "phase": "云端交付未完整，等待补齐：" + "、".join(missing)}


def set_runner(runner: Callable[[dict], dict] | None) -> None:
    global _RUNNER
    _RUNNER = runner


def wake() -> None:
    _WAKE.set()


def _heartbeat(uid: str, project_id: str, task_id: str, attempt: int,
               done: threading.Event) -> None:
    interval = max(1.0, _LEASE_S / 3)
    while not done.wait(interval):
        renewed = desktop_workspace.renew_lease(uid, project_id, task_id, attempt, _LEASE_S)
        if not renewed.get("ok"):
            return


def _execute(task: dict) -> None:
    uid, project_id, task_id = task["uid"], task["project_id"], task["task_id"]
    identity = (uid, project_id, task_id)
    claimed = desktop_workspace.claim_task(uid, project_id, task_id,
                                           owner=f"desktop-worker:{os.getpid()}", lease_s=_LEASE_S)
    if not claimed.get("ok"):
        with _LOCK:
            _ACTIVE.discard(identity)
        return
    attempt = int(claimed["attempt"])
    done = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat,
                                 args=(uid, project_id, task_id, attempt, done), daemon=True)
    heartbeat.start()
    try:
        runner = _RUNNER or _default_runner
        outcome = runner(claimed["task"]) or {}
        state = str(outcome.get("state") or (outcome.get("task") or {}).get("state")
                    or ("queued" if outcome.get("ok") else "failed"))
        phase = str(outcome.get("phase") or ("本轮完成" if outcome.get("ok") else "执行失败"))
        failure = None if outcome.get("ok") else {
            "summary": str(outcome.get("error") or (outcome.get("result") or {}).get("error") or "后台执行失败")[:4000],
            "attempt": attempt,
        }
        finished = desktop_workspace.finish_attempt(
            uid, project_id, task_id, attempt, state=state, phase=phase,
            checkpoint=outcome.get("checkpoint"), failure=failure,
            next_run_at=outcome.get("next_run_at"),
        )
        if finished.get("ok") and str((claimed["task"].get("execution_spec") or {}).get("kind")) == "matrix_scenario":
            parent = (claimed["task"].get("execution_spec") or {}).get("parent_quality_task") or {}
            if all(parent.get(key) for key in ("uid", "project_id", "task_id")):
                desktop_workspace.wake_task(
                    str(parent["uid"]), str(parent["project_id"]), str(parent["task_id"]),
                    phase="同题参考任务已更新，等待质量续跑",
                    checkpoint_patch={"reference_state": state,
                                      "reference_gaps": (outcome.get("checkpoint") or {}).get("verification", {}).get("remaining_gaps", [])},
                )
        if finished.get("ok") and state in {"completed", "partial", "failed", "cancelled"}:
            checkpoint = outcome.get("checkpoint") if isinstance(outcome.get("checkpoint"), dict) else {}
            updated = desktop_workspace.update_task(
                uid, project_id, task_id,
                artifacts=checkpoint.get("artifacts") if isinstance(checkpoint.get("artifacts"), list) else [],
                screenshots=checkpoint.get("screenshots") if isinstance(checkpoint.get("screenshots"), list) else [],
            )
            if updated.get("ok") and updated["task"].get("session_id"):
                import desktop_delivery
                desktop_delivery.notify(updated["task"], attempt)
    except Exception as exc:
        policy = _exception_policy(claimed["task"], exc, attempt)
        print(
            f"[desktop-worker] {uid}/{project_id}/{task_id} attempt {attempt} "
            f"failed ({policy['state']}): {policy['failure']['summary']}\n"
            f"{policy['failure']['traceback']}",
            flush=True,
        )
        desktop_workspace.finish_attempt(
            uid, project_id, task_id, attempt,
            state=policy["state"], phase=policy["phase"],
            checkpoint=policy["checkpoint"], failure=policy["failure"],
            next_run_at=policy["next_run_at"],
        )
    finally:
        done.set()
        with _LOCK:
            _ACTIVE.discard(identity)
        wake()


def run_once() -> dict:
    desktop_workspace.recover_expired()
    runnable = desktop_workspace.list_runnable(limit=max(10, _MAX_WORKERS * 4)).get("tasks") or []
    submitted = []
    with _LOCK:
        capacity = max(0, _MAX_WORKERS - len(_ACTIVE))
        for task in runnable:
            identity = (task["uid"], task["project_id"], task["task_id"])
            if identity in _ACTIVE or capacity <= 0:
                continue
            _ACTIVE.add(identity)
            submitted.append(task["task_id"])
            capacity -= 1
            assert _EXECUTOR is not None
            _EXECUTOR.submit(_execute, task)
    return {"ok": True, "submitted": submitted, "active": len(_ACTIVE)}


def start() -> dict:
    global _THREAD, _EXECUTOR
    if _THREAD is not None and _THREAD.is_alive():
        return {"ok": True, "already_running": True, "workers": _MAX_WORKERS}
    _STOP.clear()
    _EXECUTOR = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="desktop-task")

    def _loop() -> None:
        desktop_workspace.recover_expired()
        while not _STOP.is_set():
            try:
                run_once()
            except Exception:
                pass
            _WAKE.wait(_POLL_SEC)
            _WAKE.clear()

    _THREAD = threading.Thread(target=_loop, name="desktop-task-supervisor", daemon=True)
    _THREAD.start()
    return {"ok": True, "workers": _MAX_WORKERS, "poll_sec": _POLL_SEC, "lease_s": _LEASE_S}


def stop(wait: bool = False) -> dict:
    global _EXECUTOR
    _STOP.set()
    _WAKE.set()
    if _EXECUTOR is not None:
        _EXECUTOR.shutdown(wait=wait, cancel_futures=False)
        _EXECUTOR = None
    return {"ok": True}
