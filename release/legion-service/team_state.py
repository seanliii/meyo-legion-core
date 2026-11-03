# -*- coding: utf-8 -*-
"""军团持久状态:Team / Task DAG / attempt 代际 / mailbox。

参考 dsh-agent-teams:
- types.ts:36-104 TeamState/Task/Member/Message
- scheduler.ts:215-244 原子 claim + attemptId 防陈旧结果

每个 team 一个 JSON 文件。写盘用临时文件+replace；attempt 单调递增，超时/重派后
旧 worker 的迟到结果不能覆盖新状态。mailbox 让成员/总指挥直达通信，不必父进程转述。
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
from pathlib import Path
import legion_paths as _paths

try:
    import fcntl as _fcntl
except ImportError:  # Windows 退回进程内锁
    _fcntl = None

# 目录运行时解析(2026-09-07·pytest组合污染形态①根修):模块级常量在收集期被 import 时
# 固化,后设的隔离 env(LEGION_TEAM_DIR/MEYO_LEGION_DIR)不生效→测试写进真实 ~/.meyo
# (实测 utwatch.json 污染真 teams 目录·跨进程残留触发 task_reused 假复用)。
# 保留 TEAM_DIR 名字作兼容别名(指向动态函数)不再被消费;全部用点改调 _team_dir()。
def _team_dir() -> Path:
    """团队状态根目录(调用时解析 env·测试/部署切换隔离目录立即生效)。"""
    return Path(os.environ.get("LEGION_TEAM_DIR") or _paths.under("teams"))


def _locks_dir() -> Path:
    return _team_dir() / ".locks"
_LOCK = threading.RLock()
_TEAM_LOCKS: dict[str, threading.RLock] = {}


@contextlib.contextmanager
def _state_lock(team_id: str):
    """团队级线程锁 + 跨进程 flock；覆盖 load→判断→replace 的完整原子区间。"""
    key = _safe(team_id)
    with _LOCK:
        thread_lock = _TEAM_LOCKS.setdefault(key, threading.RLock())
    with thread_lock:
        if _fcntl is None:
            yield
            return
        lock_dir = _locks_dir()
        lock_dir.mkdir(parents=True, exist_ok=True)
        with (lock_dir / f"{key}.lock").open("a+", encoding="utf-8") as handle:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
            try:
                yield
            finally:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "team"))[:100] or "team"


def _path(team_id: str) -> Path:
    return _team_dir() / f"{_safe(team_id)}.json"


def _write(state: dict) -> None:
    _team_dir().mkdir(parents=True, exist_ok=True)
    path = _path(state["team_id"])
    tmp = path.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load(team_id: str) -> dict | None:
    try:
        return json.loads(_path(team_id).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _after_terminal_start(state: dict, lifecycle: dict, until: float = float("inf")) -> dict:
    """只有真实重开/领取/新增任务能开始下一次执行；同步、消息与心跳不算。"""
    terminal_id = int(lifecycle.get("terminal_event_id") or 0)
    if not terminal_id or int(lifecycle.get("start_event_id") or 0) > terminal_id:
        return lifecycle
    terminal_ts = float(lifecycle.get("terminal_ts") or 0)
    if any(event.get("t") in {"task_reopened", "attempt_started", "task_added", "task_queued"}
           and terminal_ts < float(event.get("ts") or 0) <= until
           for event in state.get("events") or [] if isinstance(event, dict)):
        return {**lifecycle, "execution_id": f"after:{terminal_id}", "replay_after": terminal_id}
    return lifecycle


def _stream_lifecycle(state: dict, events_path: Path) -> dict:
    """终态索引来自原有 meta/final/error；旧记录流式读取完整行，不截断大 final。"""
    if isinstance(state.get("stream_lifecycle"), dict):
        return dict(state["stream_lifecycle"])
    lifecycle: dict = {}

    def observe(rec: dict) -> None:
        nonlocal lifecycle
        kind, seq = rec.get("event"), int(rec.get("event_id") or 0)
        if kind == "meta":
            lifecycle.update(start_event_id=seq, execution_id=f"event:{seq}",
                             replay_after=max(0, seq - 1))
        elif kind in {"final", "error"}:
            lifecycle = _after_terminal_start(state, lifecycle, float(rec.get("ts") or 0))
            lifecycle.setdefault("execution_id", "event:0")
            lifecycle.setdefault("replay_after", 0)
            lifecycle.update(terminal_event_id=seq, terminal_ts=float(rec.get("ts") or 0))

    try:
        with events_path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    observe(json.loads(line))
                except (ValueError, TypeError, AttributeError):
                    continue
    except OSError:
        for rec in state.get("stream_events") or []:
            if isinstance(rec, dict):
                observe(rec)
    return lifecycle


def list_by_session(sid: str, limit: int = 5, stale_after_s: float = 1800.0, *,
                    user_id: str = "") -> list[dict]:
    """按会话列出最近 team(2026-09-07会话同步实锤):其它客户端(CLI/API/另一页面)往
    同一会话派单时,已打开的页面一无所知。本函数让前端发现该会话正在跑的 team
    并实时接入过程;已完成的 turn 由前端拉 session 补渲染。

    active 表示本次执行是否还在跑，不是作品是否成功。final/error 后的同步事件
    不能复活任务；同 team 的新 meta 或真实任务重开可以。未完成节点保留原样。
    """
    # A filename is neither a session binding nor ownership. Follow the canonical
    # turn references, including legacy nested turns, and validate each target.
    import sessions
    if not sid or not user_id:
        return []
    session = sessions.load_session(sid, user_id=user_id)
    if (not isinstance(session, dict) or session.get("sid") != sid
            or str(session.get("user_id") or "default") != user_id):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    terminal = {"done", "error", "blocked", "cancelled"}
    now = time.time()
    for turn in reversed(session.get("turns") or []):
        if not isinstance(turn, dict):
            continue
        nested = turn.get("result")
        team_id = turn.get("team_id") or (
            nested.get("team_id") if isinstance(nested, dict) else "")
        if not isinstance(team_id, str) or not team_id or team_id in seen:
            continue
        seen.add(team_id)
        try:
            state = load(team_id)
            if (not isinstance(state, dict) or state.get("team_id") != team_id
                    or state.get("user_id") != user_id
                    or state.get("conversation_id") != sid):
                continue
            p = _path(team_id)
            tasks = state.get("tasks") or {}
            unfinished = [str(k) for k, t in tasks.items()
                          if not isinstance(t, dict) or str(t.get("status") or "") not in terminal]
            events_path = _event_path(team_id)
            try:
                last_touch = max(p.stat().st_mtime,
                                 events_path.stat().st_mtime if events_path.exists() else 0)
            except OSError:
                last_touch = p.stat().st_mtime
            lifecycle = _stream_lifecycle(state, events_path)
            execution = _after_terminal_start(state, lifecycle)
            terminal_id = int(lifecycle.get("terminal_event_id") or 0)
            stream_finished = bool(
                terminal_id and int(lifecycle.get("start_event_id") or 0) < terminal_id
                and execution.get("execution_id") == lifecycle.get("execution_id"))
            # 新 meta 已明确开始下一代，旧任务全 done 不能盖过它；无事件的旧记录仍兼容任务判定。
            new_stream_run = int(lifecycle.get("start_event_id") or 0) > terminal_id
            finished = stream_finished or (bool(tasks) and not unfinished and not new_stream_run)
            active = (not finished) and (now - last_touch) < stale_after_s
            out.append({"team_id": team_id, "active": active, "updated": last_touch,
                        "execution_id": str(execution.get("execution_id") or "event:0"),
                        "replay_after": int(execution.get("replay_after") or 0),
                        "terminal_event_id": terminal_id,
                        "unfinished": unfinished,
                        "question": str(state.get("question") or "")[:160]})
        except (OSError, ValueError, TypeError, AttributeError):
            # A deleted/corrupt reference must not hide the other owned teams.
            continue
    out.sort(key=lambda row: row["updated"], reverse=True)
    return out[:max(1, int(limit or 5))]


def _refresh_member_status(state: dict, harness: str) -> None:
    """从该 Harness 的全部逻辑节点重算汇总状态，避免一节点结束把另一在役节点写成 idle。"""
    harness = str(harness or "")
    if not harness:
        return
    statuses = [str(task.get("status") or "pending")
                for task in (state.get("tasks") or {}).values()
                if str(task.get("assigned_harness") or task.get("key") or "") == harness]
    if "running" in statuses:
        status = "running"
    elif "queued" in statuses:
        status = "queued"
    elif "waiting_approval" in statuses:
        status = "waiting_approval"
    else:
        status = "idle"
    state.setdefault("members", {}).setdefault(harness, {"key": harness})["status"] = status


def _cancel_request_for(state: dict, task_id: str, task: dict) -> dict | None:
    """返回命中该逻辑任务的最新取消请求；task 与 harness scope 永不混用。"""
    tasks = state.get("tasks") or {}
    assigned = str(task.get("assigned_harness") or task_id)
    for row in reversed(list(state.get("cancel_requests") or [])):
        target = str(row.get("key") or "")
        scope = str(row.get("scope") or "")
        if target == "__all__" or scope == "all":
            return row
        if scope == "task" and target == task_id:
            return row
        if scope == "harness" and target == assigned:
            return row
        if not scope:  # 旧记录兼容：同名时 task_id 优先，避免误伤同 Harness 的其它节点。
            if target in tasks:
                if target == task_id:
                    return row
            elif target == assigned:
                return row
    return None


def _criterion_values(value) -> list:
    """A single textual criterion is one fact, not an iterable of letters."""
    return [value] if isinstance(value, str) and value else list(value or [])


def ensure(team_id: str, question: str, tasks: dict[str, str],
           dependencies: dict[str, list[str]], captain: str = "",
           orchestration_spec: dict | None = None,
           delivery_contract: dict | None = None, user_id: str = "",
           conversation_id: str = "", captain_thesis: dict | None = None,
           quality_protocol: dict | None = None, source_ref: dict | None = None,
           input_snapshot: dict | None = None, version_identity: dict | None = None,
           quality_context: dict | None = None, execution_role: str = "",
           registered: set | list | None = None,
           sync_pending_only: bool = False, *, owner_check=None) -> dict:
    """创建或更新团队。已有 attempt/mailbox 保留，任务描述和依赖同步到最新计划。

    registered(P0-5·治R5):调用方传入当前注册成员目录时,assigned_harness 回退 task_id
    或指向未注册成员的节点不再静默入图——标 unbound_owner/binding_error,不进 members
    执行池(不产生"pending 死节点"被当成真 owner 跑);None 时保持旧兼容行为。
    sync_pending_only 只同步已存在且尚未领取的 pending 节点；检查与写入共用状态锁，
    不会借属性同步创建节点、改动活 attempt 或重复重开任务。
    """
    registered_set = {str(item) for item in (registered or [])} if registered is not None else None
    with _state_lock(team_id):
        state = load(team_id)
        # 与 read_messages 相同：在已有锁内核对原记录，尚未补写任何身份或任务。
        if owner_check is not None:
            owner_check(state)
        # 归属检查也覆盖同步时的空集合/活 attempt 短路。
        from project_context import require_owner
        require_owner(state, user_id, conversation_id)
        if sync_pending_only:
            if not state:
                return {}
            tasks = {
                key: value for key, value in tasks.items()
                if (state.get("tasks", {}).get(key) or {}).get("status") == "pending"
                and (state["tasks"][key]).get("active_attempt") is None
            }
            if not tasks:
                return state
        state = state or {
            "team_id": team_id, "captain": captain or "unselected", "captain_generation": 0,
            "captain_bids": [], "created_at": time.time(),
            "message_seq": 0, "approval_seq": 0, "members": {}, "tasks": {},
            "mailboxes": {}, "approvals": {}, "events": [],
        }
        state["question"] = question
        if user_id:
            state["user_id"] = str(state.get("user_id") or user_id)
        if conversation_id:
            state["conversation_id"] = str(conversation_id)
        if orchestration_spec is not None:
            state["orchestration_spec"] = dict(orchestration_spec)
        if delivery_contract is not None:
            state["delivery_contract"] = dict(delivery_contract)
        if captain_thesis is not None:
            state["captain_thesis"] = dict(captain_thesis)
        if quality_protocol is not None:
            state["quality_protocol"] = dict(quality_protocol)
        for name, value in (("source_ref", source_ref), ("input_snapshot", input_snapshot),
                            ("version_identity", version_identity), ("quality_context", quality_context)):
            if isinstance(value, dict) and value:
                state[name] = dict(value)
        if execution_role:
            state["execution_role"] = str(execution_role)
        if captain:
            state["captain"] = captain
        state["updated_at"] = time.time()
        for key, value in tasks.items():
            node = dict(value) if isinstance(value, dict) else {"task": str(value)}
            text = str(node.get("task") or node.get("prompt") or node.get("role") or "回答问题")
            assigned = str(node.get("assigned_harness") or node.get("assign_to") or
                           node.get("harness") or node.get("agent") or key)
            # P0-5:坏 owner 不入持久执行图——assigned 是回退 task_id 或未注册成员时,
            # 标 unbound_owner 待重绑,不注册 members(避免"research"这类逻辑角色被当 CLI key 启动)。
            unbound_owner = bool(registered_set is not None and assigned not in registered_set)
            if not unbound_owner:
                member = state["members"].setdefault(assigned, {"key": assigned, "status": "idle"})
                member.setdefault("attempts", 0)
            task = state["tasks"].setdefault(key, {
                "key": key, "task_id": key, "status": "pending", "attempt_seq": 0,
                "active_attempt": None,
            })
            task.update({"text": text, "assigned_harness": assigned,
                         "unbound_owner": unbound_owner,
                         **({"binding_error": "assigned_harness_not_registered"} if unbound_owner else {}),
                         "assignment_locked": bool(node.get("assignment_locked")),
                         "excluded_harnesses": list(node.get("excluded_harnesses") or []),
                         "capability_requirements": list(node.get("capability_requirements") or []),
                         "dependencies": list(dependencies.get(key, [])),
                         "dependency_policy": str(node.get("dependency_policy") or "all_succeeded"),
                         "native_capabilities": dict(node.get("native_capabilities") or {}),
                         "native_session_scope": node.get("native_session_scope"),
                         "input_hash": str(node.get("input_hash") or ""),
                         "idempotency_key": str(node.get("idempotency_key") or ""),
                         "task_kind": str(node.get("task_kind") or "delivery"),
                         "verification_role": str(node.get("verification_role") or ""),
                         "verification_targets": list(node.get("verification_targets") or
                                                      node.get("depends_on") or []),
                         "verification_criteria": _criterion_values(node.get("verification_criteria")),
                         "supporting_context": bool(node.get("supporting_context"))})
            state["mailboxes"].setdefault(key, [])
        state["mailboxes"].setdefault("commander", [])
        _write(state)
        return state


_NATIVE_SCOPE_UNSET = object()


def reopen_task(team_id: str, task_id: str, *, text: str = "", dependencies: list[str] | None = None,
                agent: str = "", reason: str = "captain_coordination",
                native_session_scope=_NATIVE_SCOPE_UNSET) -> bool:
    """让持续首脑在新证据到达后重开既有逻辑节点；保留旧结果到 history。"""
    with _state_lock(team_id):
        state = load(team_id)
        task = ((state or {}).get("tasks") or {}).get(task_id)
        if not task or task.get("active_attempt") is not None:
            return False
        previous = dict(task.get("result") or {})
        if previous:
            task.setdefault("result_history", []).append({
                "status": task.get("status"),
                "assigned_harness": task.get("assigned_harness"),
                "result": previous, "superseded_at": time.time(), "reason": str(reason)[:500]})
            task["result_history"] = task["result_history"][-20:]
        if text:
            task["text"] = str(text)
        if dependencies is not None:
            task["dependencies"] = [str(value) for value in dependencies]
        if native_session_scope is not _NATIVE_SCOPE_UNSET:
            task["native_session_scope"] = native_session_scope
        if agent:
            previous_agent = str(task.get("assigned_harness") or task_id)
            task["assigned_harness"] = str(agent)
            task["unbound_owner"] = False
            task.pop("binding_error", None)
            state["members"].setdefault(str(agent), {"key": str(agent), "status": "idle"})
            _refresh_member_status(state, previous_agent)
        task.update({"status": "pending", "result": {}, "finished_at": None,
                     "queued_at": None, "queue_reason": "", "queue_owner": "",
                     "lease_owner": "", "lease_expires_at": 0})
        _refresh_member_status(state, str(task.get("assigned_harness") or task_id))
        state["events"].append({"t": "task_reopened", "key": task_id, "task_id": task_id,
                                "harness": task.get("assigned_harness"),
                                "reason": str(reason)[:500], "ts": time.time()})
        state["events"] = state["events"][-500:]
        state["updated_at"] = time.time()
        _write(state)
        return True


def add_task(team_id: str, task_id: str, text: str, dependencies: list[str] | None = None,
             *, agent: str = "", metadata: dict | None = None,
             registered: set | list | None = None) -> dict:
    """运行中扩图；动态 fork/verification/revision 与初始节点共用同一持久状态机。

    registered(P0-5):未注册 agent 同 ensure 处理——标 unbound_owner,不进 members 执行池。
    """
    with _state_lock(team_id):
        state = load(team_id)
        if not state:
            raise KeyError(f"unknown team: {team_id}")
        if task_id in state.get("tasks", {}):
            return dict(state["tasks"][task_id])
        task_meta = dict(metadata or {})
        registered_set = {str(item) for item in (registered or [])} if registered is not None else None
        unbound_owner = bool(registered_set is not None and agent and str(agent) not in registered_set)
        node = {"key": task_id, "task_id": task_id, "agent": str(agent or task_id),
                "assigned_harness": str(agent or task_id), "text": str(text or ""),
                "dependencies": list(dependencies or []), "status": "pending",
                "attempt_seq": 0, "active_attempt": None, "created_at": time.time(),
                "metadata": task_meta,
                "unbound_owner": unbound_owner,
                **({"binding_error": "assigned_harness_not_registered"} if unbound_owner else {}),
                "task_kind": str(task_meta.get("task_kind") or task_meta.get("kind") or "delivery"),
                "native_session_scope": task_meta.get("native_session_scope"),
                "verification_role": str(task_meta.get("verification_role") or ""),
                "verification_targets": list(task_meta.get("verification_targets") or dependencies or []),
                "verification_criteria": _criterion_values(task_meta.get("verification_criteria")),
                "supporting_context": bool(task_meta.get("supporting_context"))}
        state["tasks"][task_id] = node
        if not unbound_owner:
            state["members"].setdefault(str(agent or task_id), {"key": str(agent or task_id), "status": "idle"})
        state["mailboxes"].setdefault(task_id, [])
        state["events"].append({"t": "task_added", "key": task_id, "agent": node["agent"],
                                "dependencies": node["dependencies"], "ts": time.time()})
        state["events"] = state["events"][-500:]
        state["updated_at"] = time.time()
        _write(state)
        return dict(node)


def select_captain(team_id: str, captain: str, bids: list[dict] | None = None,
                   reason: str = "") -> dict:
    """持久记录本单首脑与竞选现场；换届使用单调 generation，便于断线恢复。"""
    with _state_lock(team_id):
        state = load(team_id)
        if not state:
            raise KeyError(f"unknown team: {team_id}")
        previous = str(state.get("captain") or "unselected")
        generation = int(state.get("captain_generation", 0)) + 1
        state.update({"captain": str(captain), "captain_generation": generation,
                      "captain_bids": list(bids or []), "updated_at": time.time()})
        state["events"].append({"t": "captain_selected" if previous == "unselected" else "captain_handoff",
                                "captain": str(captain), "previous": previous,
                                "generation": generation, "reason": str(reason)[:500],
                                "ts": time.time()})
        state["events"] = state["events"][-500:]
        _write(state)
        return {"captain": str(captain), "previous": previous, "generation": generation}


def mark_task_blocked(team_id: str, task_id: str, reason: str, *, facts: dict | None = None) -> bool:
    """P0-5:无候选可绑的开放节点明确写 blocked(带原因),拒绝持久化为 pending 死节点。

    blocked 是"等上游/等决议的暂停态"(recoverable_tasks 仍可恢复),但必须带
    blocked_reason 让首脑/恢复循环看到"为什么卡住"而不是无限空转。
    """
    with _state_lock(team_id):
        state = load(team_id)
        task = ((state or {}).get("tasks") or {}).get(task_id)
        if (not task or task.get("active_attempt") is not None
                or str(task.get("status") or "pending") not in {"pending", "queued"}):
            return False
        now = time.time()
        task.update({"status": "blocked", "blocked_reason": str(reason)[:500],
                     "blocked_facts": dict(facts or {}), "finished_at": None,
                     "queue_reason": "", "queue_owner": "",
                     "lease_owner": "", "lease_expires_at": 0})
        assigned = str(task.get("assigned_harness") or task_id)
        _refresh_member_status(state, assigned)
        state["events"].append({"t": "task_blocked_with_reason", "key": task_id, "task_id": task_id,
                                "harness": assigned, "reason": str(reason)[:200], "ts": now})
        state["events"] = state["events"][-500:]
        state["updated_at"] = now
        _write(state)
        return True


# ── P0-6 continue→dispatch acknowledgement(治R6:11次reopen 0次attempt)──────────
_ACK_EVENT_TYPES = {"task_queued", "attempt_started", "task_blocked_with_reason"}


def attempt_ack_status(team_id: str, task_id: str, *, after_ts: float = 0.0) -> dict:
    """reopen 之后该任务是否出现过 queued/attempt_started/blocked_with_reason 之一。

    首脑 continue 后一轮内每个开放节点必须有执行痕迹;否则视为协调断链,
    由上层自动 rebind/re-dispatch(禁止再次纯 reopen)。
    """
    state = load(team_id)
    if not state:
        return {"acked": False, "via": "", "ts": 0.0}
    events = [event for event in (state.get("events") or []) if isinstance(event, dict)]
    for event in reversed(events):
        if str(event.get("key") or event.get("task_id") or "") != str(task_id):
            continue
        ts = float(event.get("ts") or 0.0)
        if after_ts and ts < after_ts:
            break
        if str(event.get("t") or "") in _ACK_EVENT_TYPES:
            return {"acked": True, "via": str(event.get("t")), "ts": ts}
    return {"acked": False, "via": "", "ts": 0.0}


def coordination_to_attempt_latency(team_id: str, task_id: str, reopened_at: float) -> float | None:
    """reopen 时刻 → 首个执行痕迹(queued/attempt_started)的墙钟秒数;未 ack 返回 None。"""
    ack = attempt_ack_status(team_id, task_id, after_ts=float(reopened_at or 0.0))
    if not ack.get("acked") or not ack.get("ts"):
        return None
    return round(float(ack["ts"]) - float(reopened_at or 0.0), 3)


def mark_queued(team_id: str, key: str, *, reason: str = "capacity",
                wait_seconds: float = 0.0, queue_facts: dict | None = None,
                owner: str = "") -> bool:
    """Persist capacity/memory waiting without pretending that a Harness attempt has started."""
    with _state_lock(team_id):
        state = load(team_id)
        task = ((state or {}).get("tasks") or {}).get(key)
        if (not task or task.get("active_attempt") is not None
                or str(task.get("status") or "pending") not in {"pending", "queued", "interrupted"}):
            return False
        now = time.time()
        previous_wait = float(task.get("queue_wait_seconds") or 0.0)
        next_wait = round(float(wait_seconds or 0.0), 3)
        next_reason = str(reason or "capacity")
        if (task.get("status") == "queued" and task.get("queue_reason") == next_reason
                and next_wait - previous_wait < 10.0):
            return True
        task.update({"status": "queued", "queued_at": task.get("queued_at") or now,
                     "queue_reason": next_reason, "queue_wait_seconds": next_wait,
                     "queue_owner": str(owner or task.get("queue_owner") or f"server:{os.getpid()}")})
        assigned = str(task.get("assigned_harness") or key)
        _refresh_member_status(state, assigned)
        state["events"].append({"t": "task_queued", "key": key, "task_id": key,
                                "harness": assigned, "reason": task["queue_reason"],
                                "wait_seconds": task["queue_wait_seconds"],
                                "queue": dict(queue_facts or {}), "ts": now})
        state["events"] = state["events"][-500:]
        state["updated_at"] = now
        _write(state)
        return True


def cancel_queued(team_id: str, key: str, reason: str) -> bool:
    """Cancel a task that has not acquired capacity and therefore has no attempt generation."""
    with _state_lock(team_id):
        state = load(team_id)
        task = ((state or {}).get("tasks") or {}).get(key)
        if (not task or task.get("active_attempt") is not None
                or str(task.get("status") or "pending") not in {"pending", "queued", "interrupted"}):
            return False
        now = time.time()
        task.update({"status": "cancelled", "finished_at": now,
                     "queue_reason": "cancelled", "queue_owner": "",
                     "lease_owner": "", "lease_expires_at": 0,
                     "result": {"final": "", "error": str(reason or "用户停止"), "elapsed": 0.0}})
        assigned = str(task.get("assigned_harness") or key)
        _refresh_member_status(state, assigned)
        state["events"].append({"t": "task_queue_cancelled", "key": key, "task_id": key,
                                "harness": assigned, "status": "cancelled",
                                "reason": str(reason or "用户停止")[:200], "ts": now})
        state["events"] = state["events"][-500:]
        state["updated_at"] = now
        _write(state)
        return True


def begin_attempt(team_id: str, key: str, *, owner: str = "", lease_s: int = 900,
                  dependency_ready: bool = False) -> int:
    """原子领取任务，返回单调 attempt id，并持久化可回收 lease。"""
    with _state_lock(team_id):
        state = load(team_id)
        if not state or key not in state["tasks"]:
            raise KeyError(f"unknown team/task: {team_id}/{key}")
        task = state["tasks"][key]
        now = time.time()
        active = task.get("active_attempt")
        if _cancel_request_for(state, key, task):
            raise RuntimeError(f"task cancelled: {key}")
        if active is not None and float(task.get("lease_expires_at") or 0) > now:
            raise RuntimeError(f"task already leased: {key} attempt={active}")
        status = str(task.get("status") or "pending")
        blocked_facts = (task.get("blocked_facts")
                         if isinstance(task.get("blocked_facts"), dict) else {})
        dependency_block = bool(
            isinstance(blocked_facts.get("blocked_by"), list) and blocked_facts["blocked_by"]
            and blocked_facts.get("dependency_policy")
            and not blocked_facts.get("error_envelope") and not blocked_facts.get("admission"))
        # 仅调度器已重新核过依赖时恢复纯依赖暂停。取消/在役租约仍在上方原子检查，
        # 授权或其它暂停不能借这个入口重新开跑。
        resume_dependency = status == "blocked" and dependency_ready and dependency_block
        # incomplete = 质量门降级（research_protocol_incomplete 等）——语义就是「重做到合格为止」。
        # 2026-09-22 修死锁：此前 incomplete 不在可领集合，重做只能靠首脑 reopen_task；而首脑可能被
        # 旧上下文误导不重开（12:55 落地页单实锤：研究已交付落盘、被判 incomplete 后 claim 即抛
        # not runnable，6 个下游 all_succeeded 依赖全体 blocked，0/7 交付）。直接允许重领，把上次
        # 的降级原因留档供 run_one 上下文引用。
        if status not in {"pending", "queued", "interrupted", "error", "timeout", "incomplete"} \
                and not resume_dependency:
            raise RuntimeError(f"task not runnable: {key} status={status}; call reopen_task first")
        if status == "incomplete":
            previous_failure = str((task.get("result") or {}).get("error") or "")
            task["result_history"] = (task.get("result_history") or [])[-19:] + [{
                "status": status, "assigned_harness": str(task.get("assigned_harness") or key),
                "result": dict(task.get("result") or {}), "superseded_at": now,
                "reason": "claim_redo_after_quality_gate"}]
            if previous_failure:
                task["quality_gate_retry_of"] = previous_failure[:400]
        seq = int(task.get("attempt_seq", 0)) + 1
        if dependency_ready and dependency_block:
            task.pop("blocked_reason", None)
            task.pop("blocked_facts", None)
        task.update({"attempt_seq": seq, "active_attempt": seq, "status": "running",
                     "started_at": now, "finished_at": None, "result": {},
                     "queued_at": None, "queue_reason": "", "queue_owner": "",
                     "queue_wait_seconds": float(task.get("queue_wait_seconds") or 0.0),
                     "lease_owner": str(owner or os.getpid()),
                     "lease_expires_at": now + max(1, int(lease_s))})
        assigned = str(task.get("assigned_harness") or key)
        state["members"].setdefault(assigned, {"key": assigned, "status": "idle"})
        state["members"][assigned].update({"status": "running", "attempts": seq})
        state["events"].append({"t": "attempt_started", "key": key, "task_id": key,
                                "harness": assigned, "attempt": seq,
                                "owner": task["lease_owner"], "lease_expires_at": task["lease_expires_at"],
                                "input_hash": task.get("input_hash") or "",
                                "idempotency_key": task.get("idempotency_key") or "", "ts": time.time()})
        state["events"] = state["events"][-500:]
        _write(state)
        return seq


def renew_attempt(team_id: str, key: str, attempt: int, lease_s: int = 900) -> bool:
    """仅当前 attempt 可续租；旧 worker 的心跳不能复活已被重派的代际。"""
    with _state_lock(team_id):
        state = load(team_id)
        task = ((state or {}).get("tasks") or {}).get(key)
        if not task or task.get("active_attempt") != attempt or task.get("status") != "running":
            return False
        task["lease_expires_at"] = time.time() + max(1, int(lease_s))
        state["updated_at"] = time.time()
        _write(state)
        return True


def finish_attempt(team_id: str, key: str, attempt: int, result: dict) -> bool:
    """仅当前 active attempt 能提交；完整正文独立落 artifact，team 状态只留引用。"""
    with _state_lock(team_id):
        state = load(team_id)
        if not state or key not in state["tasks"]:
            return False
        task = state["tasks"][key]
        if task.get("active_attempt") != attempt:
            state["events"].append({"t": "stale_result_ignored", "key": key,
                                    "attempt": attempt, "ts": time.time()})
            _write(state)
            return False
        status = str(result.get("status") or "error")
        final = str(result.get("final") or "")
        final_ref = None
        if final:
            try:
                import artifact_store
                final_ref = artifact_store.put_text(team_id, key, final, name=f"attempt-{attempt}-final.txt")
            except Exception:
                final_ref = None
        # 小结果同时内联，保持旧读取者与状态检查兼容；长结果仍只放 artifact 引用，避免 JSON 膨胀。
        stored = {"status": status,
                  "final": final if len(final) <= 4000 else "",
                  "final_preview": final[:1000], "final_ref": final_ref,
                  "error": str(result.get("error") or "")[:4000],
                  "error_envelope": result.get("error_envelope"),
                  "artifacts": list(result.get("artifacts") or []),
                  "prior_attempts": list(result.get("prior_attempts") or [])[-20:],
                  "model_used": str(result.get("model_used") or result.get("model") or ""),
                  "elapsed": result.get("elapsed"),
                  "usage": dict(result.get("usage") or {}),
                  "native_checkpoint": result.get("native_checkpoint"),
                  "native_session_scope": result.get("native_session_scope"),
                  "native_context_origin": result.get("native_context_origin"),
                  "resumed_native": bool(result.get("resumed_native")),
                  "native_events": list(result.get("native_events") or [])[-50:],
                  "native_facts": (dict(result["native_facts"])
                                   if isinstance(result.get("native_facts"), dict) else {}),
                  "context_budget": dict(result.get("context_budget") or {}),
                  "continuation": dict(result.get("continuation") or {}),
                  "relay_by": str(result.get("relay_by") or ""),
                  "relayed_from": str(result.get("relayed_from") or ""),
                  # 验证协议字段不得丢(2026-09-07真跑实锤:白名单丢verification_report→
                  # resume/auto_resume恢复的团队拿不到REVISE结论→下一轮revision永不触发。
                  # task_kind同理(区分研究/样片/翻译官/交付节点)。
                  "task_kind": result.get("task_kind"),
                  "verification_role": result.get("verification_role"),
                  "verification_targets": list(result.get("verification_targets") or []),
                  "verification_report": (dict(result["verification_report"])
                                           if isinstance(result.get("verification_report"), dict)
                                           else None),
                  "revision_round": result.get("revision_round")}
        # Host export observations remain parallel to native_facts/status and
        # must survive both ordinary completion and continuation persistence.
        for field, kind in (("artifact_export", dict), ("artifact_safety_observations", list),
                            ("checkpoint_writes", dict), ("checkpoint_storage_error", str),
                            ("native_resume_error", dict)):
            if isinstance(result.get(field), kind):
                stored[field] = kind(result[field])
        task.update({"status": status, "active_attempt": None, "finished_at": time.time(),
                     "lease_owner": "", "lease_expires_at": 0, "result": stored})
        assigned = str(task.get("assigned_harness") or key)
        _refresh_member_status(state, assigned)
        state["events"].append({"t": "attempt_finished", "key": key, "task_id": key,
                                "harness": assigned, "attempt": attempt,
                                "status": status, "usage": stored["usage"], "ts": time.time()})
        _write(state)
        return True


def resolve_result(team_id: str, key: str) -> dict:
    """按需恢复任务完整正文；旧状态里的 result.final 也兼容。"""
    state = load(team_id) or {}
    result = dict(((state.get("tasks") or {}).get(key) or {}).get("result") or {})
    if result.get("final"):
        return result
    ref = result.get("final_ref")
    if ref:
        try:
            import artifact_store
            result["final"] = artifact_store.read_text(ref)
        except Exception:
            result["final"] = ""
    else:
        result["final"] = result.get("final_preview") or ""
    return result


def cancel_attempt(team_id: str, key: str, attempt: int, reason: str,
                   status: str = "cancelled") -> bool:
    """取消/超时会使 active attempt 失效；迟到 worker 随后无法覆盖。

    status(军团自审 P0):默认 cancelled(用户主动停止·非失败)。超时路径可显式传 "timeout" 兼容。
    把"用户喊停"和"卡死超时"分开·前端据此显示"已停止"而非误导性的错/超时。
    """
    with _state_lock(team_id):
        state = load(team_id)
        if not state or key not in state["tasks"]:
            return False
        task = state["tasks"][key]
        if task.get("active_attempt") != attempt:
            return False
        task.update({"active_attempt": None, "status": status, "finished_at": time.time(),
                     "lease_owner": "", "lease_expires_at": 0,
                     "result": {"final": "", "error": reason, "elapsed": 0.0}})
        assigned = str(task.get("assigned_harness") or key)
        _refresh_member_status(state, assigned)
        state["events"].append({"t": "attempt_cancelled", "key": key, "task_id": key,
                                "harness": assigned, "attempt": attempt,
                                "status": status, "reason": reason[:200], "ts": time.time()})
        _write(state)
        return True


def _owner_process_alive(owner: str) -> bool:
    text = str(owner or "")
    if not text.startswith("server:"):
        return True
    try:
        # Resume claims add thread/request suffixes; those are not part of the PID.
        pid = int(text.split(":", 2)[1])
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True  # Lack of permission is not evidence that the owner died.
    except (OSError, ValueError):
        return False


def _resume_lease_active(state: dict, now: float) -> bool:
    lease = state.get("resume_lease") or {}
    return (float(lease.get("expires_at") or 0) > now
            and _owner_process_alive(str(lease.get("owner") or "")))


def recover_expired_attempts(team_id: str, now: float | None = None) -> list[dict]:
    """Recover expired leases or attempts owned by a dead server process."""
    with _state_lock(team_id):
        state = load(team_id)
        if not state:
            return []
        current = float(now if now is not None else time.time())
        recovered = []
        for key, task in (state.get("tasks") or {}).items():
            task_cancel = _cancel_request_for(state, key, task)
            if task.get("status") == "queued" and task.get("active_attempt") is None:
                queue_owner = str(task.get("queue_owner") or "")
                if queue_owner and _owner_process_alive(queue_owner):
                    continue
                record = {"attempt": None, "status": "interrupted",
                          "abort_reason": "queue_owner_dead", "owner": queue_owner,
                          "started_at": task.get("queued_at"), "recovered_at": current,
                          "previous_result": dict(task.get("result") or {})}
                next_status = "cancelled" if task_cancel else "pending"
                task.update({"status": next_status, "queue_owner": "", "queue_reason": "",
                             "lease_owner": "", "lease_expires_at": 0,
                             "recovery_context": record})
                if task_cancel:
                    task["finished_at"] = current
                    task["result"] = {"final": "", "error": str(task_cancel.get("reason") or "用户停止"),
                                      "elapsed": 0.0}
                assigned = str(task.get("assigned_harness") or key)
                _refresh_member_status(state, assigned)
                state["events"].append({"t": "task_queue_cancelled" if task_cancel else "task_queue_recovered",
                                        "key": key, "status": next_status,
                                        "reason": "queue_owner_dead", "ts": current})
                recovered.append({"key": key, **record, "next_status": next_status})
                continue
            active = task.get("active_attempt")
            expires = float(task.get("lease_expires_at") or 0)
            owner = str(task.get("lease_owner") or "")
            owner_dead = bool(owner and not _owner_process_alive(owner))
            expired = bool(expires and expires <= current)
            if task.get("status") != "running" or active is None or not (expired or owner_dead):
                continue
            abort_reason = "owner_dead" if owner_dead else "lease_expired"
            previous = dict(task.get("result") or {})
            record = {"attempt": active, "status": "interrupted", "abort_reason": abort_reason,
                      "owner": owner, "started_at": task.get("started_at"),
                      "recovered_at": current, "previous_result": previous}
            task.setdefault("attempt_history", []).append(record)
            task["attempt_history"] = task["attempt_history"][-20:]
            next_status = "cancelled" if task_cancel else "pending"
            task.update({"status": next_status, "active_attempt": None, "queue_owner": "",
                         "lease_owner": "", "lease_expires_at": 0, "recovery_context": record})
            if task_cancel:
                task["finished_at"] = current
                task["result"] = {"final": "", "error": str(task_cancel.get("reason") or "用户停止"),
                                  "elapsed": 0.0}
            assigned = str(task.get("assigned_harness") or key)
            _refresh_member_status(state, assigned)
            state["events"].append({"t": "attempt_cancelled" if task_cancel else "attempt_recovered",
                                    "key": key, "attempt": active, "status": next_status,
                                    "reason": abort_reason, "ts": current})
            recovered.append({"key": key, **record, "next_status": next_status})
        if recovered:
            state["events"] = state["events"][-500:]
            state["updated_at"] = current
            _write(state)
        return recovered


def recover_all_expired_attempts(now: float | None = None) -> dict:
    """服务启动时扫描全部团队，把过期 lease 恢复为 pending；不主动重复副作用。"""
    recovered = []
    if not _team_dir().is_dir():
        return {"teams": 0, "attempts": 0, "recovered": []}
    for path in _team_dir().glob("*.json"):
        try:
            recovered.extend(recover_expired_attempts(path.stem, now=now))
        except Exception:
            continue
    return {"teams": len({row.get("key") for row in recovered}),
            "attempts": len(recovered), "recovered": recovered}


def _resume_terminal_result(state: dict) -> dict:
    """Read the already-indexed terminal receipt, including coordinator pauses.

    No nested state lock: the caller may hold the claim lock. The index is
    persisted only after the event has been flushed; never read another team.
    """
    terminal_id = int((state.get("stream_lifecycle") or {}).get("terminal_event_id") or 0)
    if not terminal_id:
        return {}
    path = _event_path(str(state.get("team_id") or ""))
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                if row.get("event_id") == terminal_id:
                    return row.get("data") if isinstance(row.get("data"), dict) else {}
    except (OSError, ValueError):
        pass
    for row in state.get("stream_events") or []:
        if row.get("event_id") == terminal_id and isinstance(row.get("data"), dict):
            return row["data"]
    return {"_resume_receipt_unavailable": True}


def list_auto_resumable(now: float | None = None) -> list[dict]:
    """列出可在服务重启后自动续跑的团队；只读显式 auto_resume，不猜历史任务意图。"""
    current = float(now if now is not None else time.time())
    rows = []
    if not _team_dir().is_dir():
        return rows
    for path in _team_dir().glob("*.json"):
        state = load(path.stem)
        if not state:
            continue
        policy = state.get("orchestration_spec") if isinstance(state.get("orchestration_spec"), dict) else {}
        continuation = pending_continuation(state)
        if not continuation and (
                policy.get("continuation_parent_team_id")
                or (policy.get("team_policy") or {}).get("continuation_parent_team_id")):
            continue  # The parent owns the original turn, not this child's later user requests.
        if policy.get("auto_resume") is not True and not continuation:
            continue
        if any(str(item.get("key") or "") == "__all__" for item in state.get("cancel_requests") or []):
            continue
        if _resume_lease_active(state, current):
            continue
        try:
            from legion_contracts import resume_facts_from_state
            if resume_facts_from_state(state, now=current)["eligible"]:
                rows.append(state)
        except PermissionError:
            continue  # A canonical-owner mismatch is never a background takeover.
    rows.sort(key=lambda item: float(item.get("updated_at") or 0))
    return rows


def claim_resume(team_id: str, owner: str, lease_s: int = 3600,
                 now: float | None = None, *, explicit: bool = False) -> bool:
    """Atomically claim manual or automatic recovery; even the same server cannot double-dispatch."""
    with _state_lock(team_id):
        state = load(team_id)
        if not state:
            return False
        current = float(now if now is not None else time.time())
        if _resume_lease_active(state, current):
            return False
        # 前台 dispatcher 在役时，后台恢复不得启动第二个 scheduler。只有 owner 进程已死
        # 或租约过期的 attempt 才由 recover_expired_attempts 先转回 pending 后恢复。
        if any(str(task.get("status") or "") == "running"
               and task.get("active_attempt") is not None
               and float(task.get("lease_expires_at") or 0) > current
               and _owner_process_alive(str(task.get("lease_owner") or ""))
               for task in (state.get("tasks") or {}).values()):
            return False
        from legion_contracts import resume_facts_from_state
        facts = resume_facts_from_state(state, explicit=explicit, now=current)
        if not facts["eligible"]:
            return False
        if facts["coordination_pending"] and not facts.get("continuation"):
            state["coordination_resume_attempts"] = int(state.get("coordination_resume_attempts") or 0) + 1
            state["coordination_resume_fingerprint"] = facts["evidence_fingerprint"]
        state["resume_lease"] = {"owner": str(owner), "claimed_at": current,
                                 "expires_at": current + max(1, int(lease_s))}
        state["events"].append({"t": "resume_claimed", "owner": str(owner), "ts": current})
        state["events"] = state["events"][-500:]
        state["updated_at"] = current
        _write(state)
        return True


def renew_resume(team_id: str, owner: str, lease_s: int = 3600) -> bool:
    """Use existing progress callbacks, not another watchdog, to keep the claim alive."""
    with _state_lock(team_id):
        state = load(team_id)
        lease = (state or {}).get("resume_lease") or {}
        if lease.get("owner") != owner:
            return False
        current = time.time()
        if float(lease.get("expires_at") or 0) < current + lease_s / 2:
            lease["expires_at"] = current + max(1, lease_s)
            _write(state)
        return True


def release_resume(team_id: str, owner: str) -> bool:
    """只允许当前恢复者释放租约；其它实例的迟到清理不能抢掉在役租约。"""
    with _state_lock(team_id):
        state = load(team_id)
        if not state:
            return False
        lease = state.get("resume_lease") if isinstance(state.get("resume_lease"), dict) else {}
        if str(lease.get("owner") or "") != str(owner):
            return False
        state.pop("resume_lease", None)
        state["events"].append({"t": "resume_released", "owner": str(owner), "ts": time.time()})
        state["events"] = state["events"][-500:]
        state["updated_at"] = time.time()
        _write(state)
        return True


def resume_attempt_cap() -> int:
    """error 终态任务的恢复预算上限(防永久失败任务被每次重启无限重试)。

    根修 web_08e5c897fc9c(2026-09-07):旧版把 error 当永久终态排除在恢复外——但 attempt1
    常是 server 崩溃(owner_dead·非模型过错),attempt2 一错就被判死,「一家用尽 attempt
    仍失败→带完整失败 context 转交另一家接力」的契约在恢复层断裂。预算内可恢复,
    预算用尽才真正放弃。
    """
    try:
        return max(1, int(os.environ.get("LEGION_RESUME_MAX_ATTEMPTS", "4")))
    except (TypeError, ValueError):
        return 4


def attempt_count(task: dict) -> int:
    """任务已消耗的执行轮数(单一事实源:attempt_seq,attempt_history 兜底)。"""
    try:
        seq = int(task.get("attempt_seq") or 0)
        if seq > 0:
            return seq
    except (TypeError, ValueError):
        pass
    return max(1, len(task.get("attempt_history") or []))


def recoverable_tasks(team_id: str) -> list[dict]:
    """返回服务重启后可继续领取的节点及其失败/产物现场。

    error 任务在恢复预算内也计入(见 resume_attempt_cap)——多家失败后接力未转的根修;
    预算用尽的 error 保持终态,不再无限重试。
    """
    state = load(team_id) or {}
    rows = []
    from legion_contracts import paused_output_resume_tasks
    paused = paused_output_resume_tasks(state)
    for key, task in (state.get("tasks") or {}).items():
        if key in paused:
            continue
        _status = str(task.get("status") or "pending")
        # blocked 计入可恢复(2026-09-07单1重放实锤):blocked=等上游的暂停态,不是终态。
        # 上游 error 在预算内恢复重跑后,下游 blocked 必须跟着活;否则原对话永远无法续跑
        # (主链5任务留 blocked,relay兜底成品顶包,世界级编排形同虚设)。
        # incomplete 计入可恢复(2026-09-22):质量门降级态=重做到合格,与 begin_attempt 的
        # 可领集合保持一致(修复 12:55 单死锁:服务重启/续跑时 incomplete 不复活则永远 0 交付)。
        if _status not in {"pending", "queued", "interrupted", "blocked", "incomplete"}:
            if _status != "error" or attempt_count(task) >= resume_attempt_cap():
                continue
        if _cancel_request_for(state, key, task):
            continue
        if task.get("status") == "queued":
            queue_owner = str(task.get("queue_owner") or "")
            if queue_owner and _owner_process_alive(queue_owner):
                continue
        rows.append({"key": key, "task_id": key,
                     "assigned_harness": str(task.get("assigned_harness") or key),
                     "text": task.get("text") or "",
                     "status": _status,
                     "attempt_count": attempt_count(task),
                     "last_error": str((task.get("result") or {}).get("error") or "")[:1000],
                     "dependencies": list(task.get("dependencies") or []),
                     "input_hash": task.get("input_hash") or "",
                     "idempotency_key": task.get("idempotency_key") or "",
                     "capability_requirements": list(task.get("capability_requirements") or []),
                     "excluded_harnesses": list(task.get("excluded_harnesses") or []),
                     "native_capabilities": dict(task.get("native_capabilities") or {}),
                     "native_session_scope": task.get("native_session_scope"),
                     "recovery_context": dict(task.get("recovery_context") or {})})
    return rows


def resolve_by_idempotency(team_id: str, idempotency_key: str) -> dict | None:
    """返回同团队内已完成的同一逻辑输入；只复用真完成结果，不复用失败或在途 attempt。"""
    if not idempotency_key:
        return None
    state = load(team_id) or {}
    for task_id, task in (state.get("tasks") or {}).items():
        if str(task.get("idempotency_key") or "") != str(idempotency_key):
            continue
        if str(task.get("status") or "") != "done" or task.get("active_attempt") is not None:
            continue
        result = resolve_result(team_id, task_id)
        if str(result.get("final") or "").strip() or result.get("artifacts"):
            return {**result, "key": task_id, "task_id": task_id,
                    "harness": str(task.get("assigned_harness") or task_id),
                    "assigned_harness": str(task.get("assigned_harness") or task_id),
                    "status": str(result.get("status") or task.get("status") or "done"),
                    "attempts": max(1, int(task.get("attempt_seq") or 1)),
                    "idempotency_key": str(idempotency_key), "reused": True}
    return None


def replace_result(team_id: str, task_id: str, result: dict, *,
                   previous_harness: str = "", reason: str = "failover") -> bool:
    """Persist a continuation result after the original attempt already reached a terminal state."""
    with _state_lock(team_id):
        state = load(team_id)
        task = ((state or {}).get("tasks") or {}).get(task_id)
        if not task:
            return False
        final = str(result.get("final") or "")
        final_ref = None
        if final:
            try:
                import artifact_store
                final_ref = artifact_store.put_text(
                    team_id, task_id, final,
                    name=f"continuation-{int(task.get('attempt_seq') or 0) + 1}-final.txt")
            except Exception:
                pass
        stored = {"status": str(result.get("status") or "error"),
                  "final": final if len(final) <= 4000 else "", "final_preview": final[:1000],
                  "final_ref": final_ref, "error": str(result.get("error") or "")[:4000],
                  "error_envelope": result.get("error_envelope"),
                  "artifacts": list(result.get("artifacts") or []),
                  "prior_attempts": list(result.get("prior_attempts") or [])[-20:],
                  "model_used": str(result.get("model_used") or result.get("model") or ""),
                  "elapsed": result.get("elapsed"), "usage": dict(result.get("usage") or {}),
                  "native_checkpoint": result.get("native_checkpoint"),
                  "native_session_scope": result.get("native_session_scope"),
                  "native_context_origin": result.get("native_context_origin"),
                  "resumed_native": bool(result.get("resumed_native")),
                  "native_events": list(result.get("native_events") or [])[-50:],
                  "native_facts": (dict(result["native_facts"])
                                   if isinstance(result.get("native_facts"), dict) else {}),
                  "context_budget": dict(result.get("context_budget") or {}),
                  "continuation": dict(result.get("continuation") or {}),
                  "relay_by": str(result.get("relay_by") or ""),
                  "relayed_from": str(result.get("relayed_from") or previous_harness or ""),
                  # 验证协议字段(failover接力交付也带·与finish_attempt同治·2026-09-07)
                  "task_kind": result.get("task_kind"),
                  "verification_role": result.get("verification_role"),
                  "verification_targets": list(result.get("verification_targets") or []),
                  "verification_report": (dict(result["verification_report"])
                                           if isinstance(result.get("verification_report"), dict)
                                           else None),
                  "revision_round": result.get("revision_round")}
        for field, kind in (("artifact_export", dict), ("artifact_safety_observations", list),
                            ("checkpoint_writes", dict), ("checkpoint_storage_error", str),
                            ("native_resume_error", dict)):
            if isinstance(result.get(field), kind):
                stored[field] = kind(result[field])
        task.setdefault("result_history", []).append({"status": task.get("status"),
                                                       "assigned_harness": previous_harness,
                                                       "result": dict(task.get("result") or {}),
                                                       "superseded_at": time.time()})
        task["result_history"] = task["result_history"][-20:]
        task.update({"status": str(result.get("status") or "error"), "result": stored,
                     "finished_at": time.time(), "active_attempt": None,
                     "lease_owner": "", "lease_expires_at": 0})
        _refresh_member_status(state, str(task.get("assigned_harness") or task_id))
        state["events"].append({"t": "continuation_finished", "key": task_id,
                                "task_id": task_id, "previous_harness": previous_harness,
                                "harness": task.get("assigned_harness"), "reason": str(reason)[:500],
                                "status": task["status"], "ts": time.time()})
        state["events"] = state["events"][-500:]
        state["updated_at"] = time.time()
        _write(state)
        return True


def update_assignment(team_id: str, task_id: str, harness: str, *, reason: str = "") -> bool:
    """换人只更新 assignment，不改变稳定 task_id、依赖边或幂等身份。"""
    with _state_lock(team_id):
        state = load(team_id)
        task = ((state or {}).get("tasks") or {}).get(task_id)
        if not task or not harness:
            return False
        previous = str(task.get("assigned_harness") or task_id)
        task["assigned_harness"] = str(harness)
        # P0-5:重绑到合法成员时清除未绑定标记(与 reopen_task 同治)。
        task["unbound_owner"] = False
        task.pop("binding_error", None)
        state["members"].setdefault(str(harness), {"key": str(harness), "status": "idle"})
        _refresh_member_status(state, previous)
        _refresh_member_status(state, str(harness))
        state["events"].append({"t": "task_reassigned", "key": task_id, "task_id": task_id,
                                "previous": previous, "harness": str(harness),
                                "reason": str(reason)[:500], "ts": time.time()})
        state["events"] = state["events"][-500:]
        state["updated_at"] = time.time()
        _write(state)
        return True


def request_cancel(team_id: str, key: str = "", reason: str = "用户停止",
                   scope: str = "") -> bool:
    """Persist cancellation with an explicit all/task/harness scope."""
    with _state_lock(team_id):
        state = load(team_id)
        if not state:
            return False
        now = time.time()
        target = key or "__all__"
        if target == "__all__":
            effective_scope = "all"
        elif scope in {"task", "harness"}:
            effective_scope = scope
        else:
            effective_scope = "task" if target in (state.get("tasks") or {}) else "harness"
        message = str(reason or "用户停止")[:200]
        state.setdefault("cancel_requests", []).append(
            {"key": target, "scope": effective_scope, "reason": message, "ts": now})
        state["cancel_requests"] = state["cancel_requests"][-50:]
        state["events"].append({"t": "cancel_requested", "key": target,
                                "scope": effective_scope, "reason": message, "ts": now})
        for task_id, task in (state.get("tasks") or {}).items():
            assigned = str(task.get("assigned_harness") or task_id)
            if effective_scope == "task" and task_id != target:
                continue
            if effective_scope == "harness" and assigned != target:
                continue
            if task.get("active_attempt") is not None or task.get("status") not in {"pending", "queued", "interrupted"}:
                continue
            task.update({"status": "cancelled", "finished_at": now, "queue_reason": "cancelled",
                         "queue_owner": "", "lease_owner": "", "lease_expires_at": 0,
                         "result": {"final": "", "error": message, "elapsed": 0.0}})
            assigned = str(task.get("assigned_harness") or task_id)
            _refresh_member_status(state, assigned)
            state["events"].append({"t": "task_queue_cancelled", "key": task_id,
                                    "task_id": task_id, "harness": assigned,
                                    "scope": effective_scope, "status": "cancelled",
                                    "reason": message, "ts": now})
        state["events"] = state["events"][-500:]
        state["updated_at"] = now
        _write(state)
        return True


_MAILBOX_TEXT_CAP = 200000   # 持久 mailbox 单条上限(军团自审 #3:8000 太小·会腰斩长报告/代码
                             # 接力·durable 回放也丢内容)。放大到 20 万字防 team JSON 无限膨胀·
                             # 又能装下复杂交付物全文。真正的依赖成果传递走内存 results 完整 final
                             # (legion_core 已改)·这里只保证持久层重启回放不腰斩。


def send_messages(team_id: str, sender: str, messages: list[dict]) -> list[dict]:
    """Atomically append one protocol's messages and commit the mailbox once."""
    with _state_lock(team_id):
        state = load(team_id)
        if not state:
            raise KeyError(f"unknown team: {team_id}")
        delivered = []
        for message in messages:
            recipient = str(message.get("to") or "")
            if recipient not in (state.get("tasks") or {}):
                raise KeyError(f"unknown recipient: {recipient}")
            state["message_seq"] = int(state.get("message_seq", 0)) + 1
            body = str(message.get("text") or "")
            refs = list(message.get("artifact_refs") or [])
            if len(body) > 4000:
                try:
                    import artifact_store
                    refs.append(artifact_store.put_text(team_id, recipient, body,
                                                         name=f"mail-{state['message_seq']}.txt"))
                    inline = body[:1000]
                except Exception:
                    inline = body[:_MAILBOX_TEXT_CAP]
            else:
                inline = body
            msg = {"id": state["message_seq"], "from": sender, "to": recipient,
                   "text": inline, "artifact_refs": refs, "ts": time.time(), "read_at": None}
            state["mailboxes"].setdefault(recipient, []).append(msg)
            delivered.append(msg)
        if delivered:
            _write(state)
        return delivered


def send_message(team_id: str, sender: str, recipient: str, text: str,
                 artifact_refs: list[dict] | None = None, *,
                 execution_request_id: str = "", user_id: str = "") -> dict:
    """成员/总指挥直达持久邮箱；长正文自动转内容引用，不塞爆 team JSON。"""
    with _state_lock(team_id):
        state = load(team_id)
        if not state:
            raise KeyError(f"unknown team: {team_id}")
        continuation = None
        if execution_request_id:
            import hashlib
            import project_context
            project_context.require_owner(state, user_id, str(state.get("conversation_id") or team_id))
            if not user_id or recipient != "commander" or len(execution_request_id) > 128:
                raise ValueError("invalid_continuation_request")
            digest = hashlib.sha256(str(text).encode()).hexdigest()
            for old in state.get("mailboxes", {}).get(recipient, []):
                request = old.get("continuation") or {}
                if request.get("request_id") == execution_request_id:
                    if request.get("text_sha256") != digest or request.get("user_id") != user_id:
                        raise ValueError("continuation_request_id_conflict")
                    return old
            suffix = hashlib.sha256(
                (str(team_id) + "\0" + execution_request_id).encode()).hexdigest()[:20]
            continuation = {
                "request_id": execution_request_id, "user_id": user_id,
                "conversation_id": str(state.get("conversation_id") or team_id),
                "source_team_id": team_id, "team_id": f"feedback_{suffix}",
                "text_sha256": digest, "status": "pending",
            }
        state["message_seq"] = int(state.get("message_seq", 0)) + 1
        body = str(text or "")
        refs = list(artifact_refs or [])
        if len(body) > 4000:
            try:
                import artifact_store
                refs.append(artifact_store.put_text(team_id, recipient, body,
                                                     name=f"mail-{state['message_seq']}.txt"))
                inline = body[:1000]
            except Exception:
                inline = body[:_MAILBOX_TEXT_CAP]
        else:
            inline = body
        msg = {"id": state["message_seq"], "from": sender, "to": recipient,
               "text": inline, "artifact_refs": refs, "ts": time.time(), "read_at": None}
        if continuation:
            msg["continuation"] = continuation
        state["mailboxes"].setdefault(recipient, []).append(msg)
        _write(state)
        return msg


def pending_continuation(state: dict) -> dict | None:
    """An authenticated user action, not read_at, feedback text or an observation."""
    for message in (state.get("mailboxes") or {}).get("commander", []):
        request = message.get("continuation") or {}
        if (request.get("status") in ("pending", "running")
                and request.get("source_team_id") == state.get("team_id")
                and request.get("user_id") == state.get("user_id")
                and request.get("conversation_id") == state.get("conversation_id")):
            return message
    return None


def record_continuation(team_id: str, request_id: str, *, owner: str = "", **facts) -> bool:
    """Receipt for one invocation. A returned invocation does not prove artifact quality."""
    with _state_lock(team_id):
        state = load(team_id) or {}
        if owner and (state.get("resume_lease") or {}).get("owner") != owner:
            return False
        for message in (state.get("mailboxes") or {}).get("commander", []):
            request = message.get("continuation") or {}
            if request.get("request_id") == request_id:
                request.update({key: value for key, value in facts.items()
                                if key in {"status", "reason", "started_at", "returned_at"}})
                _write(state)
                return True
    return False


def resolve_message_text(message: dict) -> str:
    """只展开 send_message 生成的正文引用；普通附件不替换反馈正文。"""
    body_name = f"mail-{message.get('id')}.txt"
    body_refs = [ref for ref in message.get("artifact_refs") or []
                 if isinstance(ref, dict) and ref.get("name") == body_name]
    if body_refs:
        try:
            import artifact_store
            # 与 NativeMailbox 的既有正文引用契约一致：正文在普通附件后追加。
            full = artifact_store.read_text(body_refs[-1])
            if full:
                return full
        except Exception:
            pass
    return str(message.get("text") or "")


def read_messages(team_id: str, recipient: str, mark_read: bool = True, *,
                  owner_check=None) -> list[dict]:
    """Optional internal ownership check runs under the read lock and raises on denial."""
    with _state_lock(team_id):
        state = load(team_id)
        if owner_check is not None:
            owner_check(state)
        if not state:
            return []
        messages = list(state["mailboxes"].get(recipient, []))
        if mark_read:
            now = time.time()
            for message in state["mailboxes"].get(recipient, []):
                message["read_at"] = message.get("read_at") or now
            _write(state)
        return messages


class NativeMailbox:
    """Bridge facts to Hermes' existing steer/drain, never an execution loop.

    read_at is a UI read receipt. Native delivery requires the complete envelope
    in pre_api_request AND the matching post_api_request (a provider response).
    A crash/cancel between these leaves mail replayable on the next native run.
    """

    def __init__(self, team_id: str, task_id: str, user_id: str, agent, emit=None,
                 *, receipt_sink=None, native_home=None, workdir=None):
        if receipt_sink is not None and not callable(receipt_sink):
            raise TypeError("mailbox receipt_sink must be callable")
        self.team_id, self.task_id, self.user_id = team_id, task_id, user_id
        self.agent = agent
        self.session_id = str(agent.session_id or "")
        self.emit = emit or (lambda event: None)
        self.receipt_sink = receipt_sink
        self.queued: dict[int, str] = {}
        self._delivered_ids: set[int] = set()
        self.message_digests: dict[int, str] = {}
        self.requests: dict[str, list[int]] = {}
        self.handles = []
        self.native_home = Path(native_home).resolve(strict=True) if native_home is not None else None
        self.native_workdir = Path(workdir).resolve(strict=True) if workdir is not None else None
        self._native_home_identity = (self.native_home.stat().st_dev, self.native_home.stat().st_ino,
                                      self.native_home.stat().st_uid) if self.native_home else None
        self._native_db_identity = None

    def _state(self):
        state = load(self.team_id)
        task = (state or {}).get("tasks", {}).get(self.task_id) or {}
        if (not self.session_id or not self.user_id or not state
                or state.get("team_id") != self.team_id
                or state.get("user_id") != self.user_id or not task
                or (task.get("assigned_harness") or self.task_id) != "hermes"):
            return None
        return state

    def _record(self, ids, phase, request_id=""):
        if self.receipt_sink is not None:
            for mid in ids:
                digest = self.message_digests.get(mid)
                if not digest:
                    raise ValueError("mailbox receipt has no queued body identity")
                self.receipt_sink({"message_id": mid, "message_digest": digest,
                                   "native_session_id": self.session_id,
                                   "phase": phase, "api_request_id": request_id})
            return
        events = []
        with _state_lock(self.team_id):
            state = self._state()
            if not state:
                return
            for message in state.get("mailboxes", {}).get(self.task_id, []):
                if message.get("id") not in ids:
                    continue
                facts = message.setdefault("native_delivery", {}).setdefault(self.session_id, {})
                if facts.get("delivered_at"):
                    continue
                facts.update(phase=phase, **{phase + "_at": time.time()})
                if request_id:
                    facts["api_request_id"] = request_id
                events.append({"message_id": message["id"], "phase": phase,
                               "native_session_id": self.session_id})
            if events:
                _write(state)
        for event in events:
            self.emit(event)  # Metadata only, never the message body or request.

    def _message_envelope(self, message):
        """Resolve the authoritative full body; never accept a path from a receipt."""
        import hashlib
        mid = message["id"]
        body = str(message.get("text") or "")
        refs = message.get("artifact_refs") or []
        body_refs = [r for r in refs if r.get("name") == f"mail-{mid}.txt"]
        if body_refs:
            import artifact_store
            ref = body_refs[-1]
            body = artifact_store.read_text(ref)
            if not body or hashlib.sha256(body.encode()).hexdigest() != ref.get("sha256"):
                return None
        identity = json.dumps([self.team_id, self.task_id, mid, body], ensure_ascii=False)
        marker = hashlib.sha256(identity.encode()).hexdigest()
        envelope = (f"[Meyo mailbox {marker}]\n"
                    f"From: {message.get('from') or ''}; message_id: {mid}\n{body}")
        other_refs = [r for r in refs if r not in body_refs]
        if other_refs:
            envelope += "\nArtifact references: " + json.dumps(other_refs, ensure_ascii=False)
        envelope += f"\n[/Meyo mailbox {marker}]"
        return envelope, hashlib.sha256(envelope.encode()).hexdigest()

    def _validate_receipt_session(self, session_id):
        if session_id == self.session_id:
            return
        if not self.native_home or not self.native_workdir:
            raise ValueError("mailbox receipt native session is not bound")
        import sqlite3
        import stat
        # Read only the SessionDB metadata needed by its compression contract:
        # publish_compression_child atomically closes a parent with reason
        # 'compression' and copies cwd/user_id into a nonempty child.
        # No SDK import, schema migration, transcript read, or database write.
        path = self.native_home / "state.db"
        fd, connection = None, None
        try:
            home_info = self.native_home.stat()
            if ((home_info.st_dev, home_info.st_ino, home_info.st_uid) != self._native_home_identity
                    or home_info.st_uid != os.geteuid()):
                raise ValueError("Hermes native home identity changed")
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            info = os.fstat(fd)
            identity = (info.st_dev, info.st_ino, info.st_uid)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or (self._native_db_identity and identity != self._native_db_identity)):
                raise ValueError("Hermes native database identity changed")
            connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                                         timeout=1, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            columns = "id,parent_session_id,user_id,cwd,source,end_reason,ended_at"
            current = connection.execute(
                f"SELECT {columns} FROM sessions WHERE id=?", (self.session_id,)).fetchone()
            owner = (current["user_id"] or None) if current else None
            # CLI roots intentionally use NULL user_id. The trusted HOME/root
            # SID + parent ctx bind ownership; descendants must inherit that
            # same NULL or explicit owner, not adopt another logical user.
            if current is None or (owner is not None and owner != self.user_id):
                raise ValueError("Hermes compression root owner is not bound")

            def check_row(row):
                cwd = row["cwd"]
                if ((row["user_id"] or None) != owner or not isinstance(cwd, str)
                        or not Path(cwd).is_absolute() or Path(cwd).resolve() != self.native_workdir):
                    raise ValueError("Hermes compression owner or cwd differs from the bound session")

            def rank(row):
                return 0 if row["end_reason"] == "compression" else 1 if row["ended_at"] is None else 2

            check_row(current)
            seen = {self.session_id}
            for _ in range(100):
                if current["end_reason"] != "compression" or current["ended_at"] is None:
                    break
                parent = current["id"]
                # Same branch/delegate/tool exclusion as SessionDB's direct
                # compression-child contract; inherited markers naming an
                # older ancestor must not reject a real compression child.
                rows = connection.execute(
                    f"""SELECT {columns} FROM sessions WHERE parent_session_id=?
                        AND COALESCE(source,'') != 'tool'
                        AND COALESCE(json_extract(COALESCE(model_config,'{{}}'),'$._branched_from'),'') != ?
                        AND COALESCE(json_extract(COALESCE(model_config,'{{}}'),'$._delegate_from'),'') != ?
                        AND COALESCE(json_extract(COALESCE(model_config,'{{}}'),'$._reset_from'),'') != ?
                        ORDER BY CASE WHEN end_reason='compression' THEN 0
                                      WHEN ended_at IS NULL THEN 1 ELSE 2 END LIMIT 2""",
                    (parent, parent, parent, parent)).fetchall()
                if not rows or (len(rows) > 1 and rank(rows[0]) == rank(rows[1])):
                    raise ValueError("Hermes compression continuation is missing or ambiguous")
                current = rows[0]
                if current["id"] in seen:
                    raise ValueError("Hermes compression lineage is cyclic")
                seen.add(current["id"])
                check_row(current)
                if not connection.execute("SELECT 1 FROM messages WHERE session_id=? LIMIT 1",
                                          (current["id"],)).fetchone():
                    raise ValueError("Hermes compression child has no durable handoff")
                if current["id"] == session_id:
                    after = path.lstat()
                    home_after = self.native_home.stat()
                    if ((after.st_dev, after.st_ino, after.st_uid) != identity
                            or (home_after.st_dev, home_after.st_ino, home_after.st_uid)
                            != self._native_home_identity):
                        raise ValueError("Hermes native database changed during lineage verification")
                    self._native_db_identity = identity
                    return
            raise ValueError("mailbox receipt is not a proven native compression descendant")
        except (OSError, sqlite3.Error) as exc:
            raise ValueError("Hermes compression lineage could not be read safely") from exc
        finally:
            if connection is not None:
                connection.close()
            if fd is not None:
                os.close(fd)

    def apply_receipt(self, receipt):
        """Parent-only write using this instance's bound team/user/task/native SID."""
        keys = {"message_id", "message_digest", "native_session_id", "phase", "api_request_id"}
        if (self.receipt_sink is not None or not isinstance(receipt, dict)
                or set(receipt) != keys or type(receipt["message_id"]) is not int
                or receipt["message_id"] <= 0
                or not isinstance(receipt["native_session_id"], str)
                or not receipt["native_session_id"] or len(receipt["native_session_id"]) > 256
                or not isinstance(receipt["message_digest"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", receipt["message_digest"])):
            raise ValueError("mailbox receipt identity does not match its parent binding")
        phase, request_id = receipt["phase"], receipt["api_request_id"]
        if (phase not in ("queued", "context_prepared", "delivered")
                or not isinstance(request_id, str) or len(request_id) > 256
                or ("\0" in request_id)
                or (phase == "queued" and request_id)
                or (phase != "queued" and not request_id)):
            raise ValueError("mailbox receipt phase/request is invalid")
        with _state_lock(self.team_id):
            state = self._state()
            if not state:
                raise ValueError("mailbox parent team/user/task binding is no longer valid")
            message = next((m for m in state.get("mailboxes", {}).get(self.task_id, [])
                            if m.get("id") == receipt["message_id"]), None)
            resolved = self._message_envelope(message) if message else None
            if not resolved or resolved[1] != receipt["message_digest"]:
                raise ValueError("mailbox receipt does not match the authoritative message body")
            receipt_sid = receipt["native_session_id"]
            self._validate_receipt_session(receipt_sid)
            existing = (message.get("native_delivery") or {}).get(receipt_sid, {})
            digest = receipt["message_digest"]
            if existing.get("message_digest", digest) != digest:
                raise ValueError("mailbox recorded body identity has changed")
            if existing.get("delivered_at"):
                if phase != "queued" and existing.get("api_request_id") != request_id:
                    raise ValueError("mailbox terminal receipt names a different request")
                return {"accepted": True, "changed": False}
            if phase != "queued" and not existing.get("queued_at"):
                raise ValueError("mailbox receipt has no queued predecessor")
            if phase == "delivered" and (
                    existing.get("phase") != "context_prepared"
                    or not existing.get("context_prepared_at")
                    or existing.get("api_request_id") != request_id):
                raise ValueError("mailbox delivered receipt has no matching prepared request")
            if (existing.get("message_digest") == digest
                    and ((phase == "queued" and existing.get("queued_at"))
                         or (existing.get("phase") == phase
                             and existing.get("api_request_id", "") == request_id))):
                return {"accepted": True, "changed": False}
            updated = dict(existing, phase=phase, message_digest=digest,
                           **{phase + "_at": time.time()})
            if request_id:
                updated["api_request_id"] = request_id
            message.setdefault("native_delivery", {})[receipt_sid] = updated
            _write(state)
        return {"accepted": True, "changed": True}

    def enqueue(self):
        # Compaction may change Hermes' native session; don't reuse old queue facts.
        sid = str(self.agent.session_id or "")
        if sid != self.session_id:
            self.session_id = sid
            self.queued.clear()
            self._delivered_ids.clear()
            self.message_digests.clear()
            self.requests.clear()
        if self.receipt_sink is not None:
            # Writers replace complete snapshots atomically. No shared lock,
            # read_at write, or temporary file is needed in the native process.
            state = self._state()
            messages = list((state or {}).get("mailboxes", {}).get(self.task_id, []))
        else:
            with _state_lock(self.team_id):
                state = self._state()
                messages = list((state or {}).get("mailboxes", {}).get(self.task_id, []))
        for message in messages:
            mid = message.get("id")
            delivered = (message.get("native_delivery") or {}).get(self.session_id, {})
            if (type(mid) is not int or mid in self.queued or mid in self._delivered_ids
                    or delivered.get("delivered_at")):
                continue
            resolved = self._message_envelope(message)
            if resolved is None:
                if self.receipt_sink is not None:
                    raise ValueError("native mailbox full message body is unavailable")
                self.emit({"message_id": mid, "phase": "body_unavailable"})
                continue
            envelope, digest = resolved
            if self.agent.steer(envelope):
                self.queued[mid] = envelope
                self.message_digests[mid] = digest
                self._record([mid], "queued")

    def pre_request(self, *, session_id="", api_request_id="", request_messages=None, **_):
        if session_id != self.session_id or not api_request_id:
            return
        # This is the native provider projection AFTER steer/drain/middleware,
        # not prev_tools or a transcript copy. Don't inspect reasoning fields.
        contents = []
        for message in request_messages or []:
            if not isinstance(message, dict) or message.get("role") != "tool":
                continue
            content = message.get("content")
            if isinstance(content, str):
                contents.append(content)
            elif isinstance(content, list):
                contents.extend(b["text"] for b in content
                                if isinstance(b, dict) and isinstance(b.get("text"), str))
        ids = [mid for mid, envelope in self.queued.items()
               if any(envelope in content for content in contents)]
        # Retry may reuse request ID with a different projection. Clear old proof.
        self.requests = {api_request_id: ids}
        self._record(ids, "context_prepared", api_request_id)

    def post_request(self, *, session_id="", api_request_id="", **_):
        if session_id == self.session_id:
            ids = self.requests.get(api_request_id, [])
            self._record(ids, "delivered", api_request_id)
            self.requests.pop(api_request_id, None)
            # Retire only after the matching post and receipt emission succeed.
            # Parent persistence may lag; retained context is not new delivery.
            self._delivered_ids.update(ids)
            for mid in ids:
                self.queued.pop(mid, None)
                self.message_digests.pop(mid, None)

    def attach(self):
        from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager
        context = PluginContext(PluginManifest(name="meyo-native-mailbox"), get_plugin_manager())
        try:
            self.handles.append(context.register_hook("pre_api_request", self.pre_request))
            self.handles.append(context.register_hook("post_api_request", self.post_request))
        except Exception:
            self.close()
            raise

    def close(self):
        for handle in reversed(self.handles):
            handle.dispose()
        self.handles.clear()
        # Never promote queued/prepared to delivered at cleanup or cancellation.


# ── 可恢复事件流(军团自审 #1:断线续播)──────────────────────────────────────────
# 事件独立写 JSONL，避免每来一步就重写整个 team JSON。team 只保存单调序号；旧版内嵌
# stream_events 仍可读取，保证已有会话不丢。
_EVENT_CAP = int(os.environ.get("LEGION_TEAM_EVENT_CAP", "1000"))


def _event_path(team_id: str) -> Path:
    return _team_dir() / f"{_safe(team_id)}.events.jsonl"


def append_event(team_id: str, event: dict, event_type: str = "step") -> dict | None:
    """持久追加一条用户可见过程事件并分配单调 event_id。"""
    if not team_id or not isinstance(event, dict):
        return None
    with _state_lock(team_id):
        state = load(team_id)
        if not state:
            return None
        seq = int(state.get("event_seq", 0)) + 1
        state["event_seq"] = seq
        data = dict(event)
        try:
            from performance_contract import classify_event
            data.update({key: value for key, value in classify_event(event_type, data).items()
                         if key in ("stage", "heartbeat", "meaningful") and key not in data})
        except Exception:
            pass
        rec = {"event_id": seq, "event": str(event_type or "step"),
               "data": data, "ts": time.time()}
        if rec["event"] in {"meta", "final", "error"}:
            lifecycle = _stream_lifecycle(state, _event_path(team_id))
            if rec["event"] == "meta":
                lifecycle.update(start_event_id=seq, execution_id=f"event:{seq}",
                                 replay_after=max(0, seq - 1))
            else:
                lifecycle = _after_terminal_start(state, lifecycle, rec["ts"])
                lifecycle.setdefault("execution_id", "event:0")
                lifecycle.setdefault("replay_after", 0)
                lifecycle.update(terminal_event_id=seq, terminal_ts=rec["ts"])
            state["stream_lifecycle"] = lifecycle
        _team_dir().mkdir(parents=True, exist_ok=True)
        with _event_path(team_id).open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        _write(state)
        return rec


def read_events(team_id: str, after: int = 0, limit: int = 500) -> list[dict]:
    """读取 event_id>after 的过程事件(断线/刷新后增量续播)。"""
    with _state_lock(team_id):
        state = load(team_id)
        if not state:
            return []
        rows = []
        path = _event_path(team_id)
        if path.is_file():
            try:
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        row = json.loads(line)
                        if int(row.get("event_id", 0)) > int(after or 0):
                            rows.append(row)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        continue
            except OSError:
                rows = []
        if not rows:
            rows = [e for e in state.get("stream_events", [])
                    if int(e.get("event_id", 0)) > int(after or 0)]
        return rows[:max(1, min(int(limit or 500), _EVENT_CAP))]
