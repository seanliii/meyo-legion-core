#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Localhost-only HTTP adapter for the development verification center."""
from __future__ import annotations

import os

import verification_center as VERIFY
import verification_reviews as REVIEWS
import verification_workflow as WORKFLOW

_SHARED_TEST_UID = "default"
_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}
_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}


def enabled() -> bool:
    """开发默认开启；生产默认关闭，也允许显式开关。"""
    configured = str(os.environ.get("LEGION_VERIFICATION_CENTER") or "").strip().lower()
    if configured in _FALSE_VALUES:
        return False
    if configured in _TRUE_VALUES:
        return True
    environment = str(os.environ.get("LEGION_ENV") or os.environ.get("MEYO_ENV")
                      or os.environ.get("ENVIRONMENT") or "development").strip().lower()
    return environment not in {"prod", "production"}


def _available(handler) -> bool:
    if enabled():
        return True
    handler._send_json({"error": "验收中心在当前环境已关闭"}, 404)
    return False


def _allowed(handler) -> bool:
    remote = handler.client_address[0] if handler.client_address else ""
    address = handler.server.server_address if handler.server else ()
    bound = address[0] if address else ""
    allowed = VERIFY.local_admin_allowed(
        remote,
        bound,
        handler.headers.get("Host", ""),
        handler.headers.get("Origin", ""),
    )
    if not allowed:
        handler._send_json({"error": "本机验收中心只允许 localhost 回环访问"}, 403)
    return allowed


def _integer(values: dict, name: str, default: int) -> int:
    return int((values.get(name) or [str(default)])[0])


def _number(values: dict, name: str, default: float = 0) -> float:
    return float((values.get(name) or [str(default)])[0])


def _value(values: dict, name: str, default: str = "") -> str:
    return str((values.get(name) or [default])[0])


def handle_get(handler, path: str, query: dict) -> None:
    """Serve a verification GET route after enforcing the loopback boundary."""
    if not _available(handler) or not _allowed(handler):
        return
    try:
        if path == "/api/verification/access":
            handler._send_json({"ok": True, "localhost_only": True,
                                "shared_test_uid": _SHARED_TEST_UID,
                                "account_label": "测试账号", "environment": "development"})
        elif path == "/api/verification/overview":
            if _value(query, "refresh") in {"1", "true", "yes"}:
                VERIFY.invalidate()
            reviews = REVIEWS.load()
            overview = VERIFY.overview(reviews)
            overview["product_manager"] = REVIEWS.summary(reviews)
            handler._send_json(overview)
        elif path == "/api/verification/exam/tasks":
            result = VERIFY.query_exam_tasks(
                offset=_integer(query, "offset", 0),
                limit=_integer(query, "limit", 50),
                q=_value(query, "q"),
                status=_value(query, "status"),
                harness=_value(query, "harness"),
                mode=_value(query, "mode"),
            )
            handler._send_json(result)
        elif path == "/api/verification/exam/task":
            detail = VERIFY.exam_task_detail(_integer(query, "seq", 0))
            handler._send_json(detail or {"error": "exam task not found"}, 200 if detail else 404)
        elif path == "/api/verification/sessions":
            result = VERIFY.query_sessions(
                offset=_integer(query, "offset", 0),
                limit=_integer(query, "limit", 50),
                q=_value(query, "q"),
                uid=_value(query, "uid"),
                harness=_value(query, "harness"),
                mode=_value(query, "mode"),
                source_type=_value(query, "source_type"),
                after=_number(query, "after"),
                before=_number(query, "before"),
                verdict=_value(query, "verdict"),
                reviews=REVIEWS.load(),
            )
            handler._send_json({"ok": True, **result})
        elif path == "/api/verification/evidence":
            handler._send_json(VERIFY.machine_evidence())
        elif path == "/api/verification/evidence/timeline":
            handler._send_json(VERIFY.evidence_timeline(_value(query, "target")))
        elif path == "/api/verification/dashboard":
            machine = VERIFY.machine_evidence()
            handler._send_json(WORKFLOW.dashboard(machine.get("items") or []))
        elif path == "/api/verification/workflow/runs":
            rows = WORKFLOW.list_runs(_value(query, "target"), _integer(query, "limit", 100))
            handler._send_json({"ok": True, "items": rows, "total": len(rows)})
        elif path == "/api/verification/workflow/run":
            row = WORKFLOW.get(_value(query, "id"))
            handler._send_json(row or {"error": "workflow run not found"}, 200 if row else 404)
        elif path == "/api/verification/release-decision":
            handler._send_json(WORKFLOW.release_decision(_value(query, "target")))
        elif path == "/api/verification/release-notes":
            handler._send_json(REVIEWS.release_notes())
        elif path == "/api/verification/desktop/tasks":
            handler._send_json(VERIFY.query_desktop_tasks(
                offset=_integer(query, "offset", 0),
                limit=_integer(query, "limit", 50),
                q=_value(query, "q"),
                uid=_value(query, "uid"),
                state=_value(query, "state"),
                status=_value(query, "status"),
            ))
        elif path == "/api/verification/desktop/task":
            detail = VERIFY.desktop_task_detail(_value(query, "id"))
            handler._send_json(detail or {"error": "desktop task not found"}, 200 if detail else 404)
        elif path == "/api/verification/mobile/tasks":
            handler._send_json(VERIFY.query_mobile_tasks(
                offset=_integer(query, "offset", 0),
                limit=_integer(query, "limit", 50),
                q=_value(query, "q"),
                uid=_value(query, "uid"),
                state=_value(query, "state"),
                status=_value(query, "status"),
            ))
        elif path == "/api/verification/mobile/task":
            detail = VERIFY.mobile_task_detail(_value(query, "id"))
            handler._send_json(detail or {"error": "mobile task not found"}, 200 if detail else 404)
        elif path == "/api/verification/product-reviews":
            result = REVIEWS.query(
                offset=_integer(query, "offset", 0),
                limit=_integer(query, "limit", 50),
                q=_value(query, "q"),
                priority=_value(query, "priority"),
                upgrade_status=_value(query, "upgrade_status"),
                release_ready=_value(query, "release_ready"),
                owner=_value(query, "owner"),
                machine_status=_value(query, "machine_status"),
            )
            handler._send_json({"ok": True, **result})
        elif path == "/api/verification/session":
            detail = VERIFY.session_detail(
                _value(query, "sid"),
                include_raw=_value(query, "raw") in {"1", "true", "yes"},
                reviews=REVIEWS.load(),
            )
            status = 200 if detail and detail.get("ok") else 422 if detail else 404
            handler._send_json(detail or {"error": "session not found"}, status)
        elif path == "/api/verification/artifacts":
            result = VERIFY.query_artifacts(
                offset=_integer(query, "offset", 0),
                limit=_integer(query, "limit", 50),
                q=_value(query, "q"),
                state=_value(query, "state", "all"),
                kind=_value(query, "kind"),
                harness=_value(query, "harness"),
                after=_number(query, "after"),
                before=_number(query, "before"),
                reviews=REVIEWS.load(),
            )
            handler._send_json({"ok": True, **result})
        elif path == "/api/verification/artifact":
            detail = VERIFY.artifact_detail(
                _value(query, "path"),
                offset=_integer(query, "offset", 0),
                limit=_integer(query, "limit", 100),
                q=_value(query, "q"),
            )
            handler._send_json(detail or {"error": "artifact not found"}, 200 if detail else 404)
        elif path == "/api/verification/native":
            result = VERIFY.query_native(
                _value(query, "harness"),
                offset=_integer(query, "offset", 0),
                limit=_integer(query, "limit", 30),
                q=_value(query, "q"),
            )
            handler._send_json(result, 400 if result.get("error") else 200)
        elif path == "/api/verification/native/session":
            detail = VERIFY.native_detail(_value(query, "harness"), _value(query, "path"))
            handler._send_json(detail or {"error": "native session not found"}, 200 if detail else 404)
        elif path == "/api/verification/review":
            target = _value(query, "target")
            review = REVIEWS.get(target)
            handler._send_json({"ok": True, "review": review,
                                "runs": WORKFLOW.list_runs(target, 30),
                                "timeline": VERIFY.evidence_timeline(target),
                                "release_decision": WORKFLOW.release_decision(target) if review else {}})
        else:
            handler._send_json({"error": "verification route not found"}, 404)
    except (TypeError, ValueError) as exc:
        handler._send_json({"error": str(exc)}, 400)


def handle_review_post(handler) -> None:
    """执行验收中心动作；全部沿用 localhost + server token 双边界。"""
    if not _available(handler) or not _allowed(handler):
        return
    payload = handler._read_json_body()
    if payload is None:
        return
    action = str(payload.get("action") or "save_review")
    actor = str(payload.get("actor") or "local-reviewer")
    target = str(payload.get("target") or "")
    identity = {}
    if action in {"reverify", "repair", "run_recommended", "run_quality_cycle_once"}:
        # _read_json_body has already checked resource ownership and normalized
        # session_id. The authenticated subject is never payload uid/user_id/actor;
        # a missing conversation stays missing rather than becoming a new run ID.
        reader = getattr(handler, "_personal_user", None)
        identity = {"user_id": reader() if callable(reader) else "",
                    "conversation_id": payload.get("session_id") or ""}
    if action == "save_review":
        protected = {"source_finding_id", "source_finding_kind", "skeleton_zones",
                     "root_cause", "root_cause_status", "root_cause_evidence"}
        clean_payload = {key: value for key, value in payload.items() if key not in protected}
        result = REVIEWS.save(target, str(payload.get("verdict") or "pending"),
                              str(payload.get("note") or ""), actor, metadata=clean_payload)
    elif action == "batch_update":
        protected = {"source_finding_id", "source_finding_kind", "skeleton_zones",
                     "root_cause", "root_cause_status", "root_cause_evidence"}
        fields = {key: value for key, value in payload.items()
                  if key not in {"action", "targets", "actor"} | protected}
        result = REVIEWS.batch_update(payload.get("targets") or [], actor=actor, **fields)
    elif action == "reverify":
        result = WORKFLOW.start_reverify(
            target, levels=payload.get("levels") if isinstance(payload.get("levels"), list) else None,
            live_harnesses=payload.get("live_harnesses") if isinstance(payload.get("live_harnesses"), list) else None,
            actor=actor, phase=str(payload.get("phase") or "release"),
            continuous=payload.get("continuous") if "continuous" in payload else None, **identity)
    elif action == "repair":
        result = WORKFLOW.start_repair(target, actor=actor,
                                       auto_reverify=payload.get("auto_reverify") is not False,
                                       reverify_phase=str(payload.get("phase") or "release"), **identity)
    elif action == "run_recommended":
        result = WORKFLOW.run_recommended(target, actor=actor, **identity)
    elif action == "run_quality_cycle_once":
        machine_items = VERIFY.machine_evidence().get("items") or []
        result = WORKFLOW.run_quality_operator_once(
            machine_items, actor=actor, execute=payload.get("execute") is not False, **identity)
    elif action == "transition":
        result = WORKFLOW.transition(target, str(payload.get("state") or ""), actor=actor,
                                     note=str(payload.get("note") or ""))
    elif action == "adopt_evidence":
        evidence_id = str(payload.get("evidence_id") or "")
        machine_items = VERIFY.machine_evidence().get("items") or []
        item = next((row for row in machine_items if str(row.get("id") or "") == evidence_id), None)
        if item is not None:
            result = WORKFLOW.adopt_evidence_issue(item, actor=actor)
        else:
            intelligence = WORKFLOW.quality_intelligence(machine_items)
            finding = next((row for row in intelligence.get("findings") or []
                            if str(row.get("id") or "") == evidence_id), None)
            result = WORKFLOW.adopt_intelligence_finding(finding or {}, actor=actor)
    else:
        result = {"ok": False, "error": "unknown verification action"}
    handler._send_json(result, 200 if result.get("ok") else 400)
