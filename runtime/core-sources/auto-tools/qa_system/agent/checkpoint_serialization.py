# -*- coding: utf-8 -*-
"""checkpoint_serialization · checkpoint/trace 的纯序列化工具。

2026-08-27 从 qa_auto_agent god-file 抽出,作为 checkpoint 模块化的前置底座。
只做 dict/list/预算/证据摘要的 JSON 安全投影·无 IO、无 session、副作用为零。
auto 通过显式 import 拿回·调用点零改动。
"""

_CKPT_EVIDENCE_PREVIEW_N = 8   # evidence 摘要里存前几条的 title/url(序列化配置)

def _evidence_digest(evidence):
    """把 evidence 列表压成摘要: 条数 + 前若干条的 title/url。不存全文。"""
    ev = evidence if isinstance(evidence, list) else []
    preview = []
    for e in ev[:_CKPT_EVIDENCE_PREVIEW_N]:
        if isinstance(e, dict):
            preview.append({
                "title": str(e.get("title") or e.get("name") or "")[:120],
                "url": str(e.get("url") or e.get("link") or "")[:200],
            })
        else:
            preview.append({"title": str(e)[:120], "url": ""})
    return {"n": len(ev), "preview": preview}


def _jsonable_budget(budget):
    """从 _budget 里挑出能 json 序列化的进度字段。"""
    if not isinstance(budget, dict):
        return {}
    out = {}
    for k in ("t_start", "hard_wall_s", "soft_wall_s", "max_iterations",
              "n_tool_calls", "soft_nudge_sent", "hard_block_active",
              "last_tool_name", "same_tool_streak",
              "no_progress_streak", "reflect_nudge_count",
              # v97f(2026-08-11) 修反思闸resume失效bug:这些字段不持久化→resume后
              #   反思闸计数归零(实测Tri-Rail题搜文字12次却没触发反思·ckpt _tool_seq=[])、
              #   agent自调的_wander_thresh丢失、_wander_reflected已反思标记丢失。加进白名单。
              "_wander_reflected", "_wander_thresh", "n_investigative_calls",
              "_video_guard_count", "_vision_guard_count",
"_forced_tool", "_force_gate_count", "_a_fail_audited",
"_key_verified", "_key_gate_count", "_watchdog_fired", "_constraint_checklisted", "_calc_split_nudged",
# v2: capability_writer + auto_diagnose 相关信号持久化
"_tool_error_count", "_auto_diag_suggested", "_force_cap_write", "_force_auto_diag",
"_write_cap_suggested", "_oc_forced_once",
              # v98t/v98u(2026-08-12) 观测缺口修复:推导链痕迹门 & 感知预算相关计数持久化·
              #   否则从 ckpt 看不到门有没有触发/换模型看图几次(上次多边形归因就卡在这)。
"_calc_trace_blocks", "_percep_last_extract_count", "_percep_stall_streak",
"_percep_last_token_count", "_deterministic_calc_priority",  # 感知→计算策略状态
            "_retry_route", "_retry_route_attempts",  # 门控拒绝后的受限自愈续跑状态
              # 语义契约审计状态：resume 后仍须完成被要求的分支验证。
              "_semantic_contract_audited", "_semantic_contract_inv_snapshot", "_semantic_contract_blocks",
        "_question_contract_gap_notified", "_hypothesis_check_snapshot", "_hypothesis_check_blocks",
        "_hypothesis_checked", "_problem_frontier", "_active_intent",
        "_intent_revision_history", "_intent_hypotheses", "_intent_replan_pending",
        "_intent_remedy", "_runtime_adaptation", "_tool_effectiveness", "_policy_ledger",
# v99c: 双轨熔断token基线

               # 层1/层2(v98x) · 心跳自省 & 自我加预算计数(持久化以便归因/resume)
               "_heartbeat_count", "_heartbeat_last_step", "_heartbeat_time_fired", "_budget_extends",
               # v100c: worksheet 共识门状态持久化(否则 resume 后重新计数→反复拦 finalize)
               "_ws_consensus_blocked", "_ws_timeout_honesty_done",
               "_ws_consensus_reread_ct", "_ws_consensus_upgrade_ct",
               "_hardwall_block_count",
               # v108b(M3/M3b/M3c): 约束映射门 + 单源对账(累计器/叙事门)
               "_constraint_mapping_audited", "_reconcile_nudged", "_m3b_domains",
               "_reconcile_narr_blocked"):
        v = budget.get(k)
        if isinstance(v, (int, float, bool, str)) or v is None:
            out[k] = v
    # _tool_seq 是 list(反思闸靠它数搜文字次数)·单独处理(只留最近30·都是str)
    _seq = budget.get("_tool_seq")
    if isinstance(_seq, list):
        out["_tool_seq"] = [t for t in _seq[-30:] if isinstance(t, str)]
    # _vlm_extracted_values 是 list[str](推导链痕迹门判"答案是否提取值的函数"的依据)·
    #   持久化以便从 ckpt 归因门决策(只留最近100·防爆)。
    _ev = budget.get("_vlm_extracted_values")
    if isinstance(_ev, list):
        out["_vlm_extracted_values"] = [str(x) for x in _ev[-100:]]
    return out


def _jsonable_generic(obj):
    """v104 · 通用递归 json 清洗(供 triage 等任意 session 字段落盘用):
    可直接序列化的原样保留·不可的递归转换·叶子降级成 str。与白名单式的
    _jsonable_budget 不同·不丢字段。"""
    try:
        json.dumps(obj)
        return obj
    except Exception:
        pass
    if isinstance(obj, dict):
        return {str(k): _jsonable_generic(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable_generic(v) for v in obj]
    return str(obj)


# v118 · 节流落盘的每-qid上次写入时间(模块级·进程内)
_CKPT_THROTTLE_LAST = {}


