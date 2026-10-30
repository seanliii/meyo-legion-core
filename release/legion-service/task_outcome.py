#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TaskOutcome:军团任务终态的 typed 单一事实源(2026-09-02 S3 五根因根治①③)。

根因(28 成员互评+独立核验定位·详见 dev/legion-three-sessions-summary-2026-09-02.md):
  ①"任务完成"曾被结果层(_is_delivered)/产物层(artifacts 非空)/SSE final/Session 持久化
    各自判断,四处口径不一 → 同一单在四处的"完成"互相打架,复杂任务没法表达部分交付。
  ③done+final 不足以表达复杂任务的完整/部分/失败/验收——规划器和 pilot 只能拿到布尔。

设计(对齐 codex thread_manager.rs:203-214 ThreadShutdownReport 分类收集思想:
每个 thread 终态分类为 completed/submit_failed/timed_out·不用一个笼统布尔;且
codex_delegate.rs:237 区分 TurnComplete vs TurnAborted——完成≠中止):

  outcome.status ∈ {completed, partial, failed, incomplete, cancelled, blocked}
    completed  真交付(所有/唯一成员过交付判据·final 非空)
    partial    多成员中部分交付(用户/下游要能看到谁没交)
    failed     全没交付且非取消(all_failed 的 typed 化)
    incomplete 截断/托词/思考半句——模型说了话但没形成成品(不算 completed·自愈候选)
    cancelled  用户停止(不包装成失败·也不包装成完成)
    blocked    依赖未满足(DAG 上游失败·成员未跑)

  outcome 是纯数据投影:不改任何答案内容、不加语义门、不替模型判断——只是把
  run_one/dispatch 已有的客观事实(status/final/artifacts/依赖)归一成一处可查询的
  结构化终态,给规划器(资格账本)、SSE final、Session 持久化、UI 四层状态共用。

验收语义(不新增门·只是如实分层):
  delivered_count / total / artifact_verified(本单产物真落盘且可读) / first_attempt_delivered /
  via_failover(靠补位接力交付)——全部来自已有字段,不重新发明。
"""
from __future__ import annotations

import csv
import json
import os
from typing import Any

from legion_contracts import DeliveryContract

# 终态全集(机器可读·前端/规划器/pilot 都消费这同一套词)
COMPLETED = "completed"
PARTIAL = "partial"
FAILED = "failed"
INCOMPLETE = "incomplete"
CANCELLED = "cancelled"
BLOCKED = "blocked"
_ALL = (COMPLETED, PARTIAL, FAILED, INCOMPLETE, CANCELLED, BLOCKED)


def from_run_one(result: dict) -> dict:
    """run_one 单家结果 → typed outcome 投影。

    不重算交付判据(那是 run_one/_is_delivered 的职责·单一事实源),只做状态词映射:
      status=done+final非空      → completed
      status=cancelled           → cancelled
      status=blocked             → blocked
      status in (timeout,error)  → failed(真失败·和"慢"分开)
      其余(incomplete/托词/思考截断/网关报错被 run_one 归成的非 done 状态)→ incomplete
    artifact_verified=本单产物真存在且非空(读盘验证·不轻信字段)。
    """
    r = result or {}
    status = str(r.get("status") or "")
    final = str(r.get("final") or "").strip()
    if status == "done" and final:
        st = COMPLETED
    elif status == "cancelled":
        st = CANCELLED
    elif status == "blocked":
        st = BLOCKED
    elif status in ("timeout", "error"):
        st = FAILED
    else:
        st = INCOMPLETE
    arts = [*(r.get("artifacts") or []), *(r.get("discovered_artifacts") or [])]
    _verified = bool(arts) and any(
        _artifact_readable(a) for a in arts if isinstance(a, dict))
    attempts = int(r["attempts"]) if r.get("attempts") is not None else (0 if st == CANCELLED else 1)
    return {
        "status": st,
        "key": str(r.get("key") or ""),
        "model_used": str(r.get("model_used") or ""),
        "delivered": st == COMPLETED,
        "first_attempt_delivered": st == COMPLETED and attempts <= 1,
        "attempts": attempts,
        "via_failover": bool(r.get("via_relay") or r.get("via_failover")),
        "artifact_verified": _verified,
        "artifact_count": len(arts),
        "final_preview": final[:120],
        "elapsed": r.get("elapsed"),
    }


def _validate_artifact(artifact: dict, validation: str) -> tuple[bool, str]:
    path = str(artifact.get("path") or "")
    if not _artifact_readable(artifact):
        return False, "missing_or_empty"
    rule = str(validation or "exists_nonempty").strip().lower()
    try:
        if rule in {"exists", "exists_nonempty", "nonempty"}:
            return True, "exists_nonempty"
        if rule in {"image_decode", "image"}:
            from PIL import Image
            with Image.open(path) as image:
                image.verify()
            return True, "image_decoded"
        if rule in {"pdf_open", "pdf"}:
            import pymupdf
            document = pymupdf.open(path)
            pages = document.page_count
            document.close()
            return pages > 0, f"pdf_pages={pages}"
        if rule in {"spreadsheet_open", "spreadsheet", "xlsx_open"}:
            suffix = os.path.splitext(path)[1].lower()
            if suffix in {".xlsx", ".xlsm"}:
                import openpyxl
                workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
                sheets = len(workbook.sheetnames)
                workbook.close()
                return sheets > 0, f"sheets={sheets}"
            with open(path, encoding="utf-8", errors="replace", newline="") as handle:
                rows = sum(1 for _ in csv.reader(handle))
            return rows > 0, f"rows={rows}"
        if rule in {"json_parse", "json"}:
            with open(path, encoding="utf-8") as handle:
                json.load(handle)
            return True, "json_parsed"
        if rule in {"icalendar_parse", "icalendar", "ics"}:
            with open(path, encoding="utf-8-sig", errors="replace") as handle:
                text = handle.read().replace("\r\n", "\n").upper()
            valid = ("BEGIN:VCALENDAR" in text and "END:VCALENDAR" in text
                     and "BEGIN:VEVENT" in text and "END:VEVENT" in text)
            return valid, "icalendar_parsed" if valid else "invalid_icalendar"
        if rule in {"html_renderable", "html", "browser_open", "browser_render"}:
            import artifact_provenance
            verdict = artifact_provenance.verify_html(
                path, require_visual=rule in {"html_renderable", "browser_render"}, offline=True)
            artifact.setdefault("provenance", {}).update({
                "verified": bool(verdict.get("ok")),
                "verification": "Playwright file:// 离线渲染通过" if verdict.get("ok") else
                                "Playwright file:// 离线渲染失败",
                "browser": verdict,
            })
            return bool(verdict.get("ok")), str(
                "playwright_file_url" if verdict.get("ok") else
                verdict.get("error") or verdict.get("page_errors") or
                verdict.get("console_errors") or verdict.get("external_requests") or "browser_render_failed")
    except Exception as exc:
        return False, f"{type(exc).__name__}:{str(exc)[:120]}"
    return False, f"unsupported_validation:{rule}"


def _artifact_kind(artifact: dict) -> str:
    kind = str(artifact.get("kind") or "").strip().lower()
    if kind:
        return kind
    path = str(artifact.get("path") or artifact.get("name") or "")
    try:
        import file_hub
        return str(file_hub.kind_of(path) or "file")
    except Exception:
        suffix = os.path.splitext(path)[1]
        return suffix.lower().lstrip(".") or "file"


def verify_delivery_contract(results: list[dict], contract_value: Any,
                             event_cb: Any = None) -> dict:
    """只核验显式契约里的客观文件事实，不从用户文本猜需求，也不评价内容好坏。"""
    contract = DeliveryContract.from_value(contract_value)
    artifacts: list[dict] = []
    seen = set()
    for result in results:
        for artifact in [*(result.get("artifacts") or []), *(result.get("discovered_artifacts") or [])]:
            if not isinstance(artifact, dict) or not _artifact_readable(artifact):
                continue
            try:
                import artifact_delivery
                if artifact_delivery._is_internal_artifact_path(
                        str(artifact.get("path") or artifact.get("url") or "")):
                    continue
            except Exception:
                pass
            identity = os.path.realpath(str(artifact.get("path") or ""))
            if identity in seen:
                continue
            seen.add(identity)
            artifacts.append(artifact)
    checks, missing = [], []
    for requirement in contract.required_artifacts:
        formats = set(requirement.formats)
        matches = []
        for artifact in artifacts:
            kind = _artifact_kind(artifact)
            suffix = os.path.splitext(str(artifact.get("path") or artifact.get("name") or ""))[1].lower().lstrip(".")
            kind_match = requirement.kind in ("", "file", "any") or kind == requirement.kind
            format_match = not formats or kind in formats or suffix in formats
            owner_match = not requirement.owner_task or str(artifact.get("task_id") or "") == requirement.owner_task
            if kind_match and format_match and owner_match:
                valid, evidence = _validate_artifact(artifact, requirement.validation)
                if event_cb:
                    try:
                        event_cb({"agent": "commander", "t": "artifact_verified", "stage": "verifying",
                                  "status": "passed" if valid else "failed", "ok": valid,
                                  "name": artifact.get("name") or os.path.basename(str(artifact.get("path") or "")),
                                  "path": artifact.get("path"), "url": artifact.get("url"),
                                  "verification": evidence,
                                  "x": f"{artifact.get('name') or '交付物'}：{evidence}"})
                    except Exception:
                        pass
                if valid:
                    matches.append({**artifact, "verification": evidence})
        passed = requirement.optional or len(matches) >= requirement.min_count
        row = {**requirement.to_dict(), "matched_count": len(matches), "passed": passed,
               "matched": [{"path": str(item.get("path") or item.get("name") or ""),
                            "verification": item.get("verification")} for item in matches]}
        checks.append(row)
        if not passed:
            missing.append(row)
    specified = bool(contract.required_artifacts)
    return {
        "specified": specified, "satisfied": not missing,
        "required_count": sum(item.min_count for item in contract.required_artifacts if not item.optional),
        "verified_count": len(artifacts), "checks": checks, "missing": missing,
        "contract": contract.to_dict(),
    }


def terminal_verification_reports(reports: list[dict]) -> list[dict]:
    """把验证历史折叠为仍对最终成品生效的报告。

    report_id/supersedes_report_ids 是版本链单一事实源：旧 Critic 的 objection 会留在 history，
    但修订后 Auditor 明确复核并 supersede 后不再作为永久否决票。无版本字段的旧调用继续按
    (role, targets) 保留最后一份，向后兼容既有测试/插件。
    """
    active: dict[str, dict] = {}
    legacy: dict[tuple[str, tuple[str, ...]], dict] = {}
    for index, raw in enumerate(reports or []):
        if not isinstance(raw, dict):
            continue
        report = dict(raw)
        for superseded in report.get("supersedes_report_ids") or []:
            active.pop(str(superseded), None)
        report_id = str(report.get("report_id") or "")
        if report_id:
            active[report_id] = report
            continue
        identity = (str(report.get("role") or "critic"),
                    tuple(str(value) for value in report.get("targets") or []))
        legacy[identity] = report
    return [*active.values(), *legacy.values()]


def from_dispatch(out: dict, is_cancelled: Any = None, delivery_contract: Any = None) -> dict:
    """dispatch 整单返回 → typed outcome；显式要求缺产物时只允许 partial。"""
    results = (out or {}).get("results") or []
    contract_inputs = list(results)
    if (out or {}).get("discovered_artifacts"):
        contract_inputs.append({"artifacts": list(out.get("discovered_artifacts") or [])})
    contract_result = verify_delivery_contract(
        contract_inputs, delivery_contract or out.get("delivery_contract"))
    if not results:
        whole_cancelled = (is_cancelled() if callable(is_cancelled)
                           else bool((out or {}).get("cancelled")))
        return {"status": CANCELLED if whole_cancelled else FAILED,
                "delivered_count": 0, "total": 0,
                "cancelled": whole_cancelled, "cancelled_count": 0,
                "timed_out": 0, "blocked_count": 0,
                "artifact_verified": False, "first_attempt_delivered": False,
                "delivery_contract": contract_result, "members": []}
    member = [from_run_one(r) for r in results]
    completed_n = sum(1 for m in member if m["status"] == COMPLETED)
    cancelled_n = sum(1 for m in member if m["status"] == CANCELLED)
    timeout_n = sum(1 for index, m in enumerate(member)
                    if m["status"] == FAILED and str((results[index] or {}).get("status")) == "timeout")
    blocked_n = sum(1 for m in member if m["status"] == BLOCKED)
    whole_cancelled = (is_cancelled() if callable(is_cancelled)
                       else bool(out.get("cancelled")))
    if whole_cancelled or cancelled_n == len(member):
        st = CANCELLED
    elif completed_n == len(member):
        st = COMPLETED
    elif completed_n > 0:
        st = PARTIAL
    elif timeout_n > 0:
        st = FAILED
    else:
        st = FAILED if any(m["status"] == FAILED for m in member) else INCOMPLETE
    contract_incomplete = contract_result["specified"] and not contract_result["satisfied"]
    require_final = bool(contract_result["contract"].get("require_final", True))
    reports = list((out or {}).get("verification_reports") or [])
    revisions = list((out or {}).get("revision_results") or [])
    terminal_reports = terminal_verification_reports(reports)
    success_audit = (out or {}).get("success_audit") \
        if isinstance((out or {}).get("success_audit"), dict) else {}
    success_required = bool(success_audit.get("enabled"))
    verification_required = bool((out or {}).get("verification_required") or success_required)
    delivered_revision = any(
        str(row.get("status") or "") == "done" and str(row.get("final") or "").strip()
        for row in revisions)
    if success_required:
        # Success Auditor 已拿到原目标、验收契约、全部旧 objection 与修订后的最终成品；
        # 它是终态质量事实源。旧报告继续留在 history 供复盘，但不再重复否决最终 verdict。
        verification_passed = str(success_audit.get("verdict") or "unverified") == "pass"
        if str(success_audit.get("profile") or "") == "frontier":
            verification_passed = verification_passed and bool(
                (success_audit.get("frontier") or {}).get("proven"))
        terminal_source = "success_auditor"
    else:
        verification_passed = bool(terminal_reports) and all(
            str(report.get("verdict") or "unverified") == "pass" for report in terminal_reports)
        terminal_source = "post_revision_auditor" if delivered_revision else "verification_reports"
    # 修订成品不能自封成功：没有最终 Success Auditor 时，必须存在一份明确针对该修订版本的
    # 独立 pass 报告；仅“revision status=done”仍保持未验收。
    if delivered_revision and not success_required:
        revision_ids = {str(row.get("task_id") or row.get("key") or "") for row in revisions}
        revision_verified = any(
            str(report.get("verdict") or "") == "pass" and
            str(report.get("assessed_candidate_id") or "") in revision_ids
            for report in terminal_reports)
        verification_passed = verification_passed and revision_verified
    verification_incomplete = verification_required and not verification_passed
    coordination_incomplete = bool(out.get("coordination_incomplete"))
    if contract_incomplete or verification_incomplete or coordination_incomplete:
        if st == COMPLETED:
            st = PARTIAL
    elif not whole_cancelled and verification_passed and completed_n > 0:
        # 成员失败/旧版本 revise 仍保留在 members/history 中供诊断；整单是否完成只看最终成品、
        # 客观交付契约和终态独立审计。否则一次被修好的旧失败会永久把整单卡在 partial。
        st = COMPLETED
    elif (not require_final and contract_result["specified"] and contract_result["satisfied"]
          and st == INCOMPLETE):
        st = COMPLETED
    return {
        "status": st, "delivered_count": completed_n, "total": len(member),
        "cancelled": whole_cancelled, "cancelled_count": cancelled_n,
        "timed_out": timeout_n, "blocked_count": blocked_n,
        "artifact_verified": any(m["artifact_verified"] for m in member),
        "first_attempt_delivered": bool(member) and member[0]["first_attempt_delivered"],
        "via_failover": any(m["via_failover"] for m in member),
        "delivery_contract": contract_result, "contract_incomplete": contract_incomplete,
        "verification": {"required": verification_required, "passed": verification_passed,
                         "terminal_source": terminal_source,
                         "reports": terminal_reports, "history": reports,
                         "success_audit": success_audit},
        "verification_incomplete": verification_incomplete,
        **({"coordination_incomplete": True} if coordination_incomplete else {}),
        "members": member, "partial": st == PARTIAL,
    }


def attach(out: dict, delivery_contract: Any = None, is_cancelled: Any = None) -> dict:
    """把所有旧/新执行路径的顶层完成字段统一投影回返回值。"""
    target = out if isinstance(out, dict) else {}
    outcome = from_dispatch(target, is_cancelled=is_cancelled, delivery_contract=delivery_contract)
    target["outcome"] = outcome
    target["delivered_count"] = outcome["delivered_count"]
    target["total"] = outcome["total"]
    target["timed_out"] = outcome["timed_out"]
    target["cancelled"] = outcome["cancelled"]
    if outcome["status"] == CANCELLED:
        # A late cancellation also overrides an earlier explicit partial status
        # before HTTP/session persistence reads that top-level field.
        target["status"] = CANCELLED
    target["cancelled_count"] = outcome.get("cancelled_count", 0)
    target["partial"] = outcome["status"] == PARTIAL
    target["contract_incomplete"] = bool(outcome.get("contract_incomplete"))
    target["all_failed"] = (outcome["delivered_count"] == 0
                            and outcome["status"] not in {COMPLETED, CANCELLED})
    return target


def _artifact_readable(a: dict) -> bool:
    """产物真可读验证(不轻信字段·读盘一次)。空路径/文件消失/零字节=不算真产物。"""
    try:
        p = str(a.get("path") or "")
        if not p:
            return False
        return os.path.isfile(p) and os.path.getsize(p) > 0
    except (OSError, ValueError):
        return False
