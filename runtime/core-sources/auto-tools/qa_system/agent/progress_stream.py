# -*- coding: utf-8 -*-
"""progress_stream · agent 过程流对外接口(2026-08-20 上线·让前端实时"看见"agent在想什么)

用户上线诉求: 像市面 agent 那样给个接口·实时展示 agent 在想什么/搜什么/判断什么。
现状: loop_events 很丰富(intent/stall/reflect/domain/multipath…20+种)·但只落 session
内存·前端拿不到。本模块 = 把内部事件【实时推给可订阅的 sink + 翻译成人话】。

设计(优雅·不侵入·可选·fail-safe):
  - emit(session, ev_dict): agent 落 loop_event 时【同时】调这个·推给已注册 sink。
  - register_sink(fn) / register_file_sink(path): 前端注册回调(in-process) 或落 JSONL
    文件(前端 tail/SSE 读)。两种前端都能接。
  - humanize(ev): 内部事件→人话(intent→"🎯 理解问题意图"·stall→"🔄 卡住了·换方向")·
    像市面 agent 那样可读。
  - 关时(env MEYO_PROGRESS_STREAM=0)零开销·emit 直接返回。

前端接法:
  ① 进程内(同 python): pe_ps.register_sink(lambda e: my_ui.push(e))
  ② 跨进程(web/前端): pe_ps.register_file_sink("/tmp/agent_progress_<qid>.jsonl")
     前端 tail -f 该文件 / SSE 逐行推送 → 实时进度条。
"""
import os
import json
import time

_SINKS = []            # in-process 回调列表
_FILE_SINKS = {}       # qid -> file path


def _enabled():
    return os.environ.get("MEYO_PROGRESS_STREAM", "1") != "0"


def register_sink(fn):
    """注册进程内回调(fn(human_event_dict))。前端/UI 用。返回取消函数。"""
    if callable(fn) and fn not in _SINKS:
        _SINKS.append(fn)
    def _unregister():
        try:
            _SINKS.remove(fn)
        except Exception:
            pass
    return _unregister


def register_file_sink(qid, path=None):
    """注册文件 sink(落 JSONL·前端 tail -f / SSE 读)。返回文件路径。"""
    try:
        if path is None:
            import tempfile
            path = os.path.join(tempfile.gettempdir(), "agent_progress_%s.jsonl" % (qid or "x"))
        _FILE_SINKS[qid or "x"] = path
        # 清空旧内容(新 run 重开)
        try:
            open(path, "w", encoding="utf-8").close()
        except Exception:
            pass
        return path
    except Exception:
        return ""


# ── 事件→人话翻译(像市面 agent 那样可读·展示 agent 在做什么) ──────────────
#   每类事件给 {icon, label, detail_key}。detail_key 从 ev 里取补充信息。
_HUMANIZE = {
    "sys_prompt":        ("🚀", "开始分析问题", None),
    "intent":            ("🎯", "理解问题意图", "n_readings"),
    "flight":            ("🗺️", "规划解题路线", "complexity"),
    "paradigm":          ("🧭", "确定解题范式", "topic"),
    "domain":            ("📚", "选择检索来源", "dom"),
    "memory_track":      ("💭", "回忆相似经验", None),
    "snowball":          ("🔍", "顺线索深入", None),
    "search_coach":      ("🔎", "调整搜索策略", None),
    "parallel_tools":    ("⚡", "并行执行工具", "n"),
    "reflect":           ("🤔", "停下来反思", None),
    "stall":             ("🔄", "卡住了·换个方向", None),
    "goal_approach":     ("📐", "核对约束·逼近答案", None),
    "verify_search":     ("✅", "验证候选", None),
    "constraint_gate":   ("🚧", "候选不符约束·换", "best"),
    "hard_switch_candidate": ("🔀", "换更合适的候选", "to"),
    "stop_when_confirmed": ("🎯", "答案已确证·收尾", "cand"),
    "multipath":         ("🌿", "多路径并行探索", None),
    "auto_multi_expert": ("👥", "多专家会诊", "n"),
    "diagnose":          ("🩺", "诊断卡点根因", "root_cause"),
    "delivery_contract": ("📋", "检查答案是否回应题问", "expected"),
    "wallclock":         ("⏱️", "时间管理", "act"),
    "gate_bus":          ("🛡️", "质量门校验", None),
    "self_retry":        ("♻️", "自我重试", None),
    "hardwall":          ("⏰", "到点收尾", None),
    "tool":              ("🔧", "调用工具", "tool"),
    "toolfail_unknown":  ("⚠️", "工具报错·换法", None),
    "struct_compact":    ("📦", "整理上下文", None),
    "softlock_enforce":  ("🔒", "锁定候选", None),
    "constraint_gate":   ("🚧", "候选不符约束·换", "best"),
    "entity_drift":      ("🧭", "纠正实体漂移", None),
    "value_first_prepend": ("🔢", "前置数值答案", None),
    "force_finalize":    ("📤", "强制收尾交付", None),
}


def humanize(ev):
    """内部事件 dict → 人话 dict {icon,label,detail,raw_ev,t}。fail-safe→通用标签。"""
    try:
        _e = str((ev or {}).get("ev") or "")
        icon, label, dkey = _HUMANIZE.get(_e, ("•", _e or "处理中", None))
        detail = ""
        if dkey and isinstance(ev, dict) and ev.get(dkey) not in (None, ""):
            detail = str(ev.get(dkey))[:60]
        # 有些事件带 act 细化(如 wallclock/domain)
        if not detail and isinstance(ev, dict) and ev.get("act"):
            detail = str(ev.get("act"))[:40]
        return {
            "icon": icon, "label": label, "detail": detail,
            "ev": _e, "t": (ev or {}).get("t") or round(time.time(), 1),
        }
    except Exception:
        return {"icon": "•", "label": "处理中", "detail": "", "ev": "", "t": round(time.time(), 1)}


def emit(ev, *, qid=""):
    """★agent 落 loop_event 时同时调这个·推给所有 sink(回调+文件)。fail-safe·零侵入。
    ev = 原始 loop_event dict(含 'ev' 键)。关时直接返回(零开销)。"""
    if not _enabled():
        return
    try:
        human = humanize(ev)
        # ① 进程内回调
        for fn in list(_SINKS):
            try:
                fn(human)
            except Exception:
                pass
        # ② 文件 sink(前端 tail/SSE)
        _fp = _FILE_SINKS.get(qid or "x") or _FILE_SINKS.get("x")
        if _fp:
            try:
                with open(_fp, "a", encoding="utf-8") as f:
                    f.write(json.dumps(human, ensure_ascii=False) + "\n")
            except Exception:
                pass
    except Exception:
        pass


def snapshot_human(session, *, limit=30):
    """从 session.loop_events 一次性导出【人话进度列表】(前端首屏/回放用)。fail-safe→[]。"""
    try:
        evs = (session or {}).get("loop_events") or []
        return [humanize(e) for e in evs[-limit:]]
    except Exception:
        return []
