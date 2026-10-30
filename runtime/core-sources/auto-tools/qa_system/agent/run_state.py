# -*- coding: utf-8 -*-
"""run_state · 单一事实源(canonical run state)的独立归属层。

背景(架构审计 2026-08-27):`_canonical_run_state` 原本住在 qa_auto_agent.py 这个
19k 行 god-file 里,导致 native_agent_loop 为了拿这个共享状态源要反向
`import qa_auto_agent` 9 处——看似"native 寄生 auto",根因其实是【共享原语归属
放错了地方】。按"单一事实源律",canonical run state 该有自己独立的家,让 native
和 auto 【对称依赖同一个模块】,而不是一条路径 import 另一条路径的内核。

本模块零依赖 qa_auto_agent(只依赖 qa_event_stream + 标准库),路径计算通过参数
注入(event_path 由调用方算好传入),因此不会造成 auto↔native↔run_state 循环 import。

用法:
    from run_state import canonical_run_state
    run = canonical_run_state(session, event_path=<路径>)   # 显式传路径
    # 或让 auto/native 各自的薄包装先算好 _event_log_path 再调
"""
import os

# ── Session 单例注册表(2026-08-27·从 god-file 迁入)──────────────────────────
# 原本住 qa_auto_agent._SESSIONS·但那个文件被双名 import(qa_auto_agent /
# qa_system.agent.qa_auto_agent)会加载两次→两个 _SESSIONS→session 分家(_thin_mode
# 一个名设、另一个名看不见)。原代码用"跨模块采纳同一 dict"的 hack 兜。
# 迁到 run_state 后:run_state 只被单名 import·天然单例·auto/native 都从这里拿同一个
# session 注册表·根治双 import 分家问题(不只是搬家·是根因修复)。
_SESSIONS: dict = {}


def _new_session_dict(qid):
    return {
        "qid": qid, "question": None, "scope": {}, "triage": {}, "cost_rec": {},
        "evidence": [], "stance_stats": {}, "cards": "", "narrative": "",
        "nocode_decision": {}, "final": "", "tool_log": [],
        "plan": [],            # G-align · 显式 plan_state · [{"text","status"}]
        "user_id": "default",  # P0 · 个性化地基:这次任务属于哪个用户
        "_attempt_count": {},  # {tool_name: 已尝试次数} · 供 retry policy 用
        "_budget": None,       # 见 _init_budget · 墙钟/步数预算与早退保护
    }


def sess(qid):
    """取/建一个 live session(进程内单例·按 qid)。auto 和 native 共享同一注册表=单一事实源。"""
    if qid not in _SESSIONS:
        _SESSIONS[qid] = _new_session_dict(qid)
    return _SESSIONS[qid]


def ensure_budget(session):
    """返回 session 的预算 dict·未初始化(None/非dict)时创建并挂回。

    统一根治两类历史坑:
      · `session.setdefault("_budget", {})` 遇 key 已存在但值 None → 返回 None·后续下标崩;
      · `session.get("_budget") or {}` 遇已初始化的空 dict → 换新临时 dict·标记写不回 session。
    保留已有 dict 对象(identity不变)·只在非dict时新建。"""
    budget = session.get("_budget") if isinstance(session, dict) else None
    if not isinstance(budget, dict):
        budget = {}
        if isinstance(session, dict):
            session["_budget"] = budget
    return budget


def _safe_qid(qid):
    """把 qid 变成文件名安全的形式(与 qa_auto_agent._safe_qid 同语义·独立副本避免反向依赖)。"""
    return "".join(c if (c.isalnum() or c == "_") else "_" for c in str(qid or "unknown"))


def _default_ckpt_path(qid):
    """自给自足的 legacy ckpt 路径(不依赖 qa_auto_agent)。
    与 CLAUDE.md 记录的 legacy 双写路径一致: seed-users-system/qa_system/state/auto_ckpt/。
    仅在调用方未注入 ckpt_path_fn 且 session 无 _event_log_path 时兜底(如 native 独立调用)。
    per-user 主路径由 auto 薄包装注入其 _ckpt_path 处理;此处只保证 native 脱离 auto 也能算出路径。"""
    _here = os.path.dirname(os.path.abspath(__file__))
    _qa_root = os.path.dirname(_here)   # …/qa_system
    _ckpt_dir = os.path.join(_qa_root, "state", "auto_ckpt")
    return os.path.join(_ckpt_dir, "auto_ckpt_%s.json" % _safe_qid(qid))


def resolve_event_path(session, ckpt_path_fn):
    """算出/复用 session 的事件日志路径(单一真相源的落盘位置)。

    ckpt_path_fn: 调用方注入的"给 qid 算 ckpt 路径"的函数(auto=_ckpt_path·native 同)。
      run_state 不关心 ckpt 目录怎么定(per-user / legacy)·只借它拿到同目录。
    幂等:session 里已有 _event_log_path 就直接复用。"""
    event_path = str(session.get("_event_log_path") or "")
    if event_path:
        return event_path
    qid = str(session.get("qid") or "unknown")
    run_tag = _safe_qid(os.environ.get("MEYO_RUN_ID") or "")
    suffix = "_%s" % run_tag if run_tag else ""
    event_path = os.path.join(os.path.dirname(ckpt_path_fn(qid)),
                              "qa_events_%s%s.jsonl" % (_safe_qid(qid), suffix))
    session["_event_log_path"] = event_path
    return event_path


def canonical_run_state(session, event_path=None, ckpt_path_fn=None):
    """返回该 live session 的单一权威状态服务(EventStore→RunState)。

    event_path: 显式传入事件日志路径(优先)。
    ckpt_path_fn: 若不传 event_path·用它算(见 resolve_event_path)。
    二者都不传时·退化为"仅用 session 已有的 _event_log_path"(和旧行为一致)。"""
    import qa_event_stream as _qes
    if event_path is None:
        event_path = resolve_event_path(session, ckpt_path_fn or _default_ckpt_path)
    qid = str(session.get("qid") or "unknown")
    store = _qes.EventStore(
        event_path, run_id=str(session.get("run_id") or qid),
        session_id=str(session.get("session_id") or qid), question_id=qid)
    return _qes.RunState(store)
