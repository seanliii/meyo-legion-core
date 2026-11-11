"""Offline real-dispatch regressions: facts survive a failed control turn."""
from contextlib import nullcontext
from copy import deepcopy
import importlib
import json
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import pytest

SERVICE = Path(__file__).resolve().parents[1]
TEAM = "__ut_coordination_continuity"
OWNER = "fixture-owner"
CONVERSATION = "fixture-conversation"
HARNESS = "__ut_member"


@pytest.fixture
def case(tmp_path, monkeypatch):
    for name, leaf in (
        ("HOME", "home"), ("MEYO_LEGION_DIR", "data"), ("LEGION_HOME_DIR", "legion"),
        ("LEGION_PROJECT_CONTEXT_DIR", "projects"), ("LEGION_TASK_MEMORY_DIR", "memory"),
        ("LEGION_TEAM_DIR", "teams"), ("LEGION_SESSIONS_DIR", "sessions"),
        ("LEGION_USER_CONFIG_DIR", "users"), ("LEGION_ARTIFACT_STORE_DIR", "artifacts"),
        ("LEGION_PROC_REGISTRY_DIR", "registry"),
    ):
        (tmp_path / leaf).mkdir(exist_ok=True)
        monkeypatch.setenv(name, str(tmp_path / leaf))
    monkeypatch.syspath_prepend(str(SERVICE))

    def forbidden(*args, **kwargs):
        raise AssertionError("real model/network/process access forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    core = importlib.import_module("legion_core")
    pc = importlib.import_module("project_context")
    tm = importlib.import_module("task_memory")
    artifacts = importlib.import_module("artifact_store")
    # These three roots are import-time constants; pin at test execution time.
    for module, leaf in ((pc, "projects"), (tm, "memory"), (artifacts, "artifacts")):
        monkeypatch.setattr(module, "ROOT", tmp_path / leaf)
    monkeypatch.setattr(core, "_harness_keys", lambda: {HARNESS})
    monkeypatch.setattr(core, "_aigc_ready", lambda **kw: {"runnable_clis": [HARNESS]})
    monkeypatch.setattr(core, "_build_prelude", lambda *a, **kw: "")
    monkeypatch.setattr(core, "_build_runtime_brief", lambda *a, **kw: "")
    monkeypatch.setattr(core, "_read_async_deep_plans", lambda *a, **kw: "")
    monkeypatch.setattr(core, "_captain_mail_context", lambda *a, **kw: "")
    monkeypatch.setattr(core, "_baseline_snapshot", lambda *a, **kw: None)
    class Lease(nullcontext):
        wait_seconds = 0

    monkeypatch.setattr(core, "_GatewayLease", lambda *a, **kw: Lease())
    monkeypatch.setattr(core, "run_one", forbidden)
    monkeypatch.setattr(core, "_run_one_with_gateway", forbidden)
    plan = {
        "captain": HARNESS, "team_policy": {"continuous_coordination": True},
        "tasks": {
            task: {"task": task + " original work", "assigned_harness": HARNESS,
                   "assignment_locked": True, "depends_on": [prior] if prior else []}
            for task, prior in (("A", ""), ("B", "A"), ("C", "B"))
        },
        "project_context": {"repo_root": str(tmp_path / "work")},
    }
    (tmp_path / "work").mkdir()
    return SimpleNamespace(core=core, pc=pc, tm=tm, mp=monkeypatch, root=tmp_path, plan=plan)


def initialize(case):
    return case.pc.initialize(TEAM, "original objective", case.plan, mode="orchestrate",
                              user_id=OWNER, conversation_id=CONVERSATION)


def dispatch(case, **kwargs):
    return case.core.dispatch(
        "original objective", deepcopy(case.plan), qhash=TEAM, mode="orchestrate",
        user_id=OWNER, conversation_id=CONVERSATION, **kwargs)


def install_native(case, *, cancel_after_failure=False, pause=False):
    members, controls = [], []

    def member(key, prompt, **kwargs):
        facts = deepcopy(kwargs["task_facts"])
        members.append(facts)
        return {"status": "done", "final": facts["task_id"] + " actual result"}

    def control(key, prompt, **kwargs):
        controls.append({"prompt": prompt, **deepcopy(kwargs)})
        if len(controls) == 1:
            if cancel_after_failure:
                case.mp.setattr(case.core, "is_cancelled", lambda *a, **kw: True)
            result = {"status": "timeout", "final": "", "error": "fixture control timeout"}
            if pause:
                result["error_envelope"] = {
                    "category": "authorization_required", "scope": "provider",
                    "code": "appid_disabled", "retryable": False, "message": "fixture denial",
                }
            return result
        return {"status": "done", "final": "use the new returned facts",
                "orchestration_request": {"coordinate": {
                    "action": "continue", "reason": "B supplied new evidence",
                    "task_updates": {"C": {"task": "C revised by captain after B"}},
                }}}

    case.mp.setattr(case.core, "run_one", member)
    case.mp.setattr(case.core, "_run_one_with_gateway", control)
    return members, controls


def test_reviewer_is_not_an_implicit_delivery_owner(case):
    work = {"key": "work", "task_id": "work", "status": "done", "final": "user deliverable"}
    review = {"key": "review", "task_id": "review", "status": "done",
              "final": "review observations", "verification_role": "critic"}
    assert case.core._winner_final({"results": [work, review]}) == "user deliverable"
    assert case.core._recommend_winner([work, review], "pk", None) == ""


@pytest.mark.parametrize("check", ["next_member", "next_coordination", "resolved_history"])
def test_failed_control_does_not_lose_facts_or_disable_next_new_wave(case, check):
    members, controls = install_native(case)
    out = dispatch(case)
    assert [row["task_id"] for row in members] == ["A", "B", "C"]
    assert [row["status"] for row in out["results"]] == ["done"] * 3
    if check == "next_member":
        decisions = members[1]["project_context"]["decisions"]
        assert any(row.get("error") == "captain_execution_failed" for row in decisions)
        assert members[1]["role"] == "B original work", "failure must not rewrite the graph"
    elif check == "next_coordination":
        assert len(controls) == 2, "B is new evidence, not a retry of the A control turn"
        latest = controls[1]["task_facts"]["project_context"]
        assert {row["task_id"] for row in latest["completed"]} == {"A", "B"}
        assert latest["decisions"][-1]["error"] == "captain_execution_failed"
        assert "PREVIOUS_COORDINATION_EXECUTION_JSON" in controls[1]["prompt"]
        assert members[2]["project_context"]["decisions"][-1]["sequence"] == 2
        assert members[2]["role"] == "C revised by captain after B"
    else:
        assert not out.get("coordination_incomplete"), "superseded failure stays history, not veto"
        assert [row["ok"] for row in out["coordination_history"]] == [False, True]
        assert [row["sequence"] for row in case.pc.load(TEAM)["decisions"]] == [1, 2]


def test_existing_sequence_continues_without_reusing_old_request_slot(case):
    initialize(case)
    case.pc.record_coordination(TEAM, HARNESS, {
        "sequence": 7, "ok": False, "error": "captain_execution_failed", "action": None})
    members, controls = install_native(case)
    dispatch(case)
    assert controls[0]["qhash"] == TEAM + "_coordinate_8"
    assert controls[1]["qhash"] == TEAM + "_coordinate_9"


def test_sequence_uses_saved_record_not_truncated_model_view(case):
    initialize(case)
    saved = case.pc.record_coordination(TEAM, HARNESS, {
        "sequence": 7, "ok": False, "error": "captain_execution_failed", "action": None})
    saved["decisions"].extend({"kind": "other_fixture_observation"} for _ in range(41))
    case.pc._atomic_write(case.pc._path(TEAM), saved)
    _, controls = install_native(case)
    dispatch(case)
    assert controls[0]["qhash"] == TEAM + "_coordinate_8"


@pytest.mark.parametrize("prior_sequence", [0, 7])
def test_round_complete_does_not_reuse_wave_or_previous_request_slots(case, prior_sequence):
    initialize(case)
    if prior_sequence:
        case.pc.record_coordination(TEAM, HARNESS, {
            "sequence": prior_sequence, "ok": False,
            "error": "captain_execution_failed", "action": None})
    members, _ = install_native(case)
    controls = []

    def control(key, prompt, **kwargs):
        controls.append(kwargs["qhash"])
        target = {1: "B", 2: "C"}.get(len(controls))
        return {"status": "done", "final": "observed current results",
                "orchestration_request": {"coordinate": {
                    "action": "continue" if target else "complete",
                    "reason": "current results consumed",
                    "task_updates": {
                        target: {"task": target + " original work"}} if target else {},
                }}}

    case.mp.setattr(case.core, "_run_one_with_gateway", control)
    pilot = importlib.import_module("legion_pilot")
    case.mp.setattr(pilot, "evaluate", lambda *a, **kw: {"action": "stop", "kind": "done"})
    case.core.dispatch_persistent(
        "original objective", deepcopy(case.plan), qhash=TEAM, mode="orchestrate",
        user_id=OWNER, conversation_id=CONVERSATION, max_rounds=1)
    assert [row["task_id"] for row in members] == ["A", "B", "C"]
    assert controls == [
        f"{TEAM}_coordinate_{prior_sequence + offset}" for offset in (1, 2, 3)]


@pytest.mark.parametrize("stop", ["cancel", "provider_pause", "retained_wave"])
def test_new_control_opportunity_preserves_existing_stop_and_reuse_boundaries(case, stop):
    members, controls = install_native(
        case, cancel_after_failure=stop == "cancel", pause=stop == "provider_pause")
    kwargs = {}
    if stop == "retained_wave":
        kwargs["_retained_results"] = {
            "B": {"key": "B", "task_id": "B", "status": "done", "final": "already done",
                  "assigned_harness": HARNESS}}
    dispatch(case, **kwargs)
    assert len(controls) == 1
    if stop == "retained_wave":
        assert [row["task_id"] for row in members] == ["A", "C"]


def coordinate(case, native_result):
    initialize(case)
    case.mp.setattr(case.core, "_run_one_with_gateway",
                    lambda *a, **kw: deepcopy(native_result))
    case.core._run_captain_coordination(
        "original objective", case.plan, TEAM,
        {"A": {"task_id": "A", "status": "done", "final": "A actual result"}},
        ["B", "C"], phase="wave_0_complete", sequence=1,
        user_id=OWNER, conversation_id=CONVERSATION)
    return case.pc.load(TEAM)["decisions"][-1]["execution_result_ref"]


@pytest.mark.parametrize("stored", ["absent", "previous_only", "different_checkpoint", "wrong_identity"])
def test_missing_current_checkpoint_never_creates_a_fictitious_reference(case, stored):
    checkpoint = {"kind": "fixture", "session_id": "original-native", "provider_binding": "opaque-fixture"}
    prior_id = f"conv:{HARNESS}:{CONVERSATION}:captain-control"
    before = case.tm.update(prior_id, HARNESS, native_checkpoint=checkpoint,
                            native_conversation_id=CONVERSATION + ":captain-control")
    result = {"status": "timeout", "final": "", "error": "fixture timeout"}
    request_id = TEAM + "_coordinate_1"
    if stored in ("different_checkpoint", "wrong_identity"):
        result["native_checkpoint"] = checkpoint
        case.tm.update(
            request_id, HARNESS,
            native_checkpoint={**checkpoint, "session_id": "different-native"}
            if stored == "different_checkpoint" else checkpoint,
            native_conversation_id="other-conversation" if stored == "wrong_identity"
            else CONVERSATION + ":captain-control")
    reference = coordinate(case, result)
    assert reference.get("checkpoint_status") == "missing_current"
    assert not reference.get("task_memory_ref"), "a path is not proof that its field exists"
    assert not reference.get("project_context_ref"), "no commander artifact was saved"
    assert not reference.get("resumed_native")
    assert not reference.get("native_checkpoint"), "old/mismatched anchors are not current evidence"
    assert case.tm.load(prior_id, HARNESS) == before, "no empty checkpoint may overwrite the old slot"


def test_saved_current_checkpoint_and_artifact_references_read_back(case):
    checkpoint = {"kind": "fixture", "session_id": "current-native", "provider_binding": "opaque-fixture"}
    request_id = TEAM + "_coordinate_1"
    case.tm.update(request_id, HARNESS, native_checkpoint=checkpoint,
                   native_conversation_id=CONVERSATION + ":captain-control")
    path = case.root / "work" / "control-note.txt"
    path.write_text("actual local bytes")
    reference = coordinate(case, {
        "status": "timeout", "final": "", "native_checkpoint": checkpoint,
        "artifacts": [{"path": str(path), "kind": "text"}]})
    memory_ref = reference["task_memory_ref"]
    stored = json.loads(Path(memory_ref["path"]).read_text())[memory_ref["field"]]
    assert stored == checkpoint
    project_ref = reference["project_context_ref"]
    rows = json.loads(Path(project_ref["path"]).read_text())[project_ref["field"]]
    assert any(row["task_id"] == "commander" and row["path"] == str(path) for row in rows)
    assert "opaque-fixture" not in json.dumps(reference), "do not export binding digests"


def test_artifact_storage_failure_does_not_publish_dangling_project_reference(case):
    def cannot_save(*args, **kwargs):
        raise OSError("fixture storage failure")

    case.mp.setattr(case.pc, "record_progress_artifact", cannot_save)
    reference = coordinate(case, {
        "status": "timeout", "final": "", "artifacts": [{"path": "/fixture/unsaved.txt"}]})
    assert not reference.get("project_context_ref")
    assert reference.get("artifact_storage_error") == "OSError"


# Reuse CPU leaves only; the imported graph spy calls the real dispatch.
from test_explicit_responses_provider import isolated, explicit_run_one  # noqa: E402, F401
from test_native_terminal_dispatch_graph import (  # noqa: E402, F401
    graph, ORIGINAL, WRITER, _node, _plan, _returned, _rows,
)
from test_project_export_terminal_binding import (  # noqa: E402
    USER as PEER_USER, CONVERSATION as PEER_CONVERSATION,
)


@pytest.fixture
def scene(explicit_run_one):
    # Do not import an autouse export scene into the older case-based tests.
    return importlib.import_module("test_project_export_terminal_binding").scene.__wrapped__(explicit_run_one)


@pytest.fixture(autouse=True)
def _peer_safety(tmp_path, monkeypatch):
    for name in (
        "HOME", "CODEX_HOME", "TMPDIR", "MEYO_LEGION_DIR", "LEGION_HOME_DIR",
        "LEGION_PROJECT_CONTEXT_DIR", "LEGION_TASK_MEMORY_DIR", "LEGION_TEAM_DIR",
        "LEGION_SESSIONS_DIR", "LEGION_USER_CONFIG_DIR", "LEGION_ARTIFACT_STORE_DIR",
        "LEGION_PROC_REGISTRY_DIR",
    ):
        root = tmp_path / "peer-safety" / name.lower()
        root.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(name, str(root))
    monkeypatch.setenv("LEGION_API_TOKEN", "__ut_peer_api_token")
    monkeypatch.setenv("LEGION_PERSONAL_TOKENS_FILE", str(tmp_path / "no-live-tokens.json"))
    monkeypatch.setenv("LEGION_GATEWAY_RETRIES", "0")
    monkeypatch.syspath_prepend(str(SERVICE))

    def forbidden(*args, **kwargs):
        raise AssertionError("peer regressions permit no network or native process")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    core = importlib.import_module("legion_core")
    monkeypatch.setattr(core, "_CANCEL_REGISTRY", {})


def _peer_setup(case, team):
    plan = _plan(case, {"A": _node("Original CPU author", owner=ORIGINAL)})
    plan["team_policy"]["candidate_harnesses"] = [ORIGINAL, WRITER]
    case.pc.initialize(team, "original peer objective", plan, mode="single",
                       user_id=PEER_USER, conversation_id=PEER_CONVERSATION)
    case.teams.ensure(team, "original peer objective", plan["tasks"], {"A": []},
                      user_id=PEER_USER, conversation_id=PEER_CONVERSATION,
                      registered={ORIGINAL, WRITER})
    return plan, {
        "status": "done", "final": "original partial work", "assigned_harness": ORIGINAL,
        "orchestration_request": {"need_peer": {"key": WRITER, "ask": "independent CPU fact"}},
    }


def _request_peer(case, team, plan, result, **kwargs):
    return case.core._run_requested_peer(
        "original peer objective", plan, result, task_id="A", team_id=team,
        user_id=PEER_USER, conversation_id=PEER_CONVERSATION, qhash=team, **kwargs)


@pytest.mark.parametrize("target", ["parent", "child", "harness"])
def test_requested_peer_respects_parent_child_and_harness_cancel(graph, target):
    case, team = graph, "__ut_peer_cancel_" + target
    plan, original = _peer_setup(case, team)
    before = deepcopy(original)
    if target == "parent":
        case.core.request_cancel(team, "A", scope="task")
    else:
        real_add = case.teams.add_task

        def add_and_cancel(team_id, child_id, *args, **kwargs):
            child = real_add(team_id, child_id, *args, **kwargs)
            case.core.request_cancel(
                team, child_id if target == "child" else WRITER,
                scope="task" if target == "child" else "harness")
            return child

        case.monkeypatch.setattr(case.teams, "add_task", add_and_cancel)
    assert _request_peer(case, team, plan, original) is None
    assert not case.native_calls, "cancel before admission must never reach a native leaf"
    assert original == before
    assert case.teams.read_messages(team, "A", mark_read=False) == []
    saved = case.teams.load(team)["tasks"]
    assert saved["A"]["assigned_harness"] == ORIGINAL
    children = [row for key, row in saved.items() if key != "A"]
    if target == "parent":
        assert not children
    else:
        assert len(children) == 1
        assert children[0]["status"] == "cancelled" and children[0]["attempt_seq"] == 0


@pytest.mark.parametrize("boundary", [
    "short_answer", "foreign_owner", "foreign_user", "foreign_conversation", "outside_candidates",
])
def test_requested_peer_requires_explicit_request_and_authorized_owner(graph, boundary):
    case, team = graph, "__ut_peer_admission_" + boundary
    plan, original = _peer_setup(case, team)
    if boundary == "short_answer":
        original = {"status": "done", "final": "好", "assigned_harness": ORIGINAL}
    elif boundary == "outside_candidates":
        plan["team_policy"]["candidate_harnesses"] = [ORIGINAL]
    elif boundary == "foreign_owner":
        original["assigned_harness"] = WRITER
    before = deepcopy(original)
    if boundary in {"foreign_user", "foreign_conversation"}:
        with pytest.raises(PermissionError):
            case.core._run_requested_peer(
                "original peer objective", plan, original, task_id="A", team_id=team,
                user_id="__ut_foreign_user" if boundary == "foreign_user" else PEER_USER,
                conversation_id="__ut_foreign_conversation"
                if boundary == "foreign_conversation" else PEER_CONVERSATION, qhash=team)
    elif boundary == "foreign_owner":
        with pytest.raises(PermissionError):
            _request_peer(case, team, plan, original)
    else:
        assert _request_peer(case, team, plan, original) is None
    assert not case.native_calls and original == before
    assert set(case.teams.load(team)["tasks"]) == {"A"}
    assert case.teams.read_messages(team, "A", mark_read=False) == []


@pytest.mark.parametrize("source", ["original_execution", "request_ledger"])
def test_requested_peer_provider_results_budget_stops_before_native(graph, source):
    case, team = graph, "__ut_peer_budget_" + source
    plan, original = _peer_setup(case, team)
    from test_request_budget_dispatch import BUDGET
    observed = {"status": "error", "error_envelope": deepcopy(BUDGET),
                "source": source, "final": "", "task_id": "A"}
    before = deepcopy(original)
    assert _request_peer(case, team, plan, original,
                         _provider_results=lambda: [observed]) is None
    pause = original.pop("peer_input_pause")
    assert pause["blocked_before_native"] is True and pause["execution_calls"] == 0
    assert pause["error_envelope"]["code"] == BUDGET["code"]
    assert pause["native_facts"]["runtime_budget_observation"]["source"] == source
    assert original == before, "budget is an independent fact, not a rewrite of author work"
    assert not case.native_calls and not case.picks
    assert set(case.teams.load(team)["tasks"]) == {"A"}
    assert case.teams.read_messages(team, "A", mark_read=False) == []


def test_initial_author_can_delegate_a_real_graph_then_resume_without_captain(graph):
    case, team = graph, "__ut_author_graph"
    plan = _plan(case, {"A": _node("Original complete objective", owner=ORIGINAL)})
    plan["initial_author"] = {"task_id": "A", "harness": ORIGINAL,
                              "advantage_verification": "unproven"}
    plan["team_policy"]["candidate_harnesses"] = [ORIGINAL, WRITER]
    calls = []

    def forbidden(*args, **kwargs):
        raise AssertionError("the author's returned proposal must not create another captain/planner call")

    case.monkeypatch.setattr(case.core, "_run_captain_coordination", forbidden)
    case.monkeypatch.setattr(case.core, "plan_request_with_model", forbidden)

    def answer(call):
        calls.append(call)
        returned = _returned(case, "__ut_author_cursor_" + call["key"])
        if call["task_id"] == "B":
            return {**returned, "final": "Independent contribution with a concrete fact"}
        if sum(item["task_id"] == "A" for item in calls) == 1:
            return {**returned, "final": "Saved original work", "orchestration_request": {
                "coordinate": {"action": "continue", "reason": "A missing fact is now identified",
                    "task_updates": {"A": {"depends_on": ["B"]}},
                    "next_plan": {"tasks": {"B": {
                        "task": "Resolve the identified missing fact", "assigned_harness": WRITER,
                        "supporting_context": True, "depends_on": []}}}}}}
        return {**returned, "final": "Original author completed the user's work using the contribution"}

    case.answer = answer
    out = case.core.dispatch_persistent(
        "Original complete objective", plan, qhash=team, mode="single", user_id=PEER_USER,
        conversation_id=PEER_CONVERSATION, max_rounds=3, cb=case.events.append)
    assert [call["task_id"] for call in calls] == ["A", "B", "A"]
    assert calls[-1]["checkpoint"]["session_id"] == "__ut_author_cursor_" + ORIGINAL
    assert len(case.rounds) == 2 and not case.picks and not case.empty_plans
    assert case.core._winner_final(out).startswith("Original author completed")
    rows = _rows(out)
    assert rows["B"]["supporting_context"] is True
    input_ref, = rows["A"]["contribution_input_refs"]
    assert input_ref["key"] == "B" and input_ref["harness"] == WRITER
    assert input_ref["observation"] == "included_upstream_input; adoption unverified"
    assert case.teams.resolve_result(team, "A")["contribution_input_refs"] == [input_ref]
    assert case.teams.load(team)["tasks"]["B"]["attempt_seq"] == 1
    assert case.teams.load(team)["tasks"]["A"]["attempt_seq"] == 2
    assert out["coordination"]["source"] == "native_author_proposal"
    assert case.rounds[-1]["plan"]["team_policy"]["candidate_harnesses"] == [ORIGINAL, WRITER]


@pytest.mark.parametrize("kind", ["outside_owner", "cleared_policy", "alias"])
def test_author_proposal_cannot_expand_request_scope_or_erase_it(graph, kind):
    case, team = graph, "__ut_author_scope_" + kind
    plan = _plan(case, {"A": _node("Original work", owner=ORIGINAL)})
    plan["initial_author"] = {"task_id": "A", "harness": ORIGINAL}
    plan["team_policy"]["candidate_harnesses"] = [ORIGINAL]
    proposal = {"action": "continue", "reason": "Member proposal, not a new grant",
                "task_updates": {"A": {"assigned_harness": WRITER}}}
    if kind == "alias":
        proposal.pop("task_updates")
        proposal["next_plan"] = {"tasks": {"B": {"task": "Independent work", "assign_to": WRITER}}}
    elif kind == "cleared_policy":
        proposal["next_plan"] = {"team_policy": None, "tasks": {"B": {
            "task": "Independent work", "assigned_harness": WRITER}}}
    else:
        proposal["next_plan"] = {"team_policy": {"candidate_harnesses": [ORIGINAL, WRITER]}}
    case.answer = lambda call: {**_returned(case, "__ut_scope_cursor"),
        "final": "Original useful work retained",
        "orchestration_request": {"coordinate": deepcopy(proposal)}}
    out = case.core.dispatch_persistent(
        "Original work", plan, qhash=team, mode="single", user_id=PEER_USER,
        conversation_id=PEER_CONVERSATION, max_rounds=3)
    assert [call["key"] for call in case.native_calls] == [ORIGINAL]
    assert case.teams.load(team)["tasks"]["A"]["assigned_harness"] == ORIGINAL
    assert "B" not in case.teams.load(team)["tasks"]
    assert out["coordination_incomplete"] is True
    assert not out["coordination"]["ok"]
    assert "outside_request_candidates" in out["coordination"]["protocol_feedback"]
    assert case.core._winner_final(out) == "Original useful work retained"


def test_author_minimal_continue_reuses_original_native_cursor_and_records_decision(graph):
    case, team = graph, "__ut_author_minimal_continue"
    plan = _plan(case, {"A": _node("Original work", owner=ORIGINAL)})
    plan["initial_author"] = {"task_id": "A", "harness": ORIGINAL}
    def answer(call):
        value = _returned(case, "__ut_minimal_cursor")
        if len(case.native_calls) == 1:
            return {**value, "final": "First useful partial work", "orchestration_request": {
                "coordinate": {"action": "continue", "reason": "An original operation is unfinished"}}}
        return {**value, "final": "Original operation finished"}
    case.answer = answer
    out = case.core.dispatch_persistent(
        "Original work", plan, qhash=team, mode="single", user_id=PEER_USER,
        conversation_id=PEER_CONVERSATION, max_rounds=3)
    assert len(case.native_calls) == 2 and not case.empty_plans
    assert case.native_calls[-1]["checkpoint"]["session_id"] == "__ut_minimal_cursor"
    packet = case.pc.load(team)
    decision, = [row for row in packet["decisions"] if row["kind"] == "author_coordination"]
    assert decision["author"] == ORIGINAL and decision["task_id"] == "A"
    assert not packet.get("captain")
    assert case.core._winner_final(out) == "Original operation finished"


def test_author_messages_reach_new_support_task_after_registration(graph):
    case, team = graph, "__ut_author_outbound"
    plan = _plan(case, {"A": _node("Original objective", owner=ORIGINAL)})
    plan["initial_author"] = {"task_id": "A", "harness": ORIGINAL}
    plan["team_policy"]["candidate_harnesses"] = [ORIGINAL, WRITER]
    def answer(call):
        value = _returned(case, "__ut_message_cursor_" + call["key"])
        if call["task_id"] == "B":
            mailbox = case.teams.load(team)["mailboxes"]["B"]
            assert any(row["from"] == ORIGINAL and row["text"] == "Actual observed bottleneck" for row in mailbox)
            return {**value, "final": "A concrete support finding"}
        if len(case.native_calls) == 1:
            return {**value, "final": "Useful initial version", "orchestration_request": {"coordinate": {
                "action": "continue", "reason": "New independent work is needed",
                "task_updates": {"A": {"depends_on": ["B"]}},
                "next_plan": {"tasks": {"B": {"task": "Investigate the actual observation",
                    "assigned_harness": WRITER, "supporting_context": True}}},
                "messages": [{"to": "B", "text": "Actual observed bottleneck"}]}}}
        return {**value, "final": "Original work finished"}
    case.answer = answer
    out = case.core.dispatch_persistent(
        "Original objective", plan, qhash=team, mode="single", user_id=PEER_USER,
        conversation_id=PEER_CONVERSATION, max_rounds=3)
    receipt, = out["coordination"]["message_receipts"]
    assert receipt["to"] == "B" and receipt["message_id"] is not None
    assert "adoption unverified" in receipt["meaning"]
    decision, = [row for row in case.pc.load(team)["decisions"] if row["kind"] == "author_coordination"]
    assert decision["message_receipts"] == [receipt]
    assert case.core._winner_final(out) == "Original work finished"


@pytest.mark.parametrize("collision", [False, True])
def test_author_fork_input_is_durable_and_does_not_replace_parent_delivery(graph, collision):
    case, team = graph, "__ut_author_fork_" + str(collision)
    plan = _plan(case, {"A": _node("Original objective", owner=ORIGINAL)})
    plan["initial_author"] = {"task_id": "A", "harness": ORIGINAL}
    plan["team_policy"]["candidate_harnesses"] = [ORIGINAL, WRITER]
    def answer(call):
        value = _returned(case, "__ut_fork_cursor_" + call["key"])
        if call["key"] == WRITER:
            assert case.teams.load(team)["tasks"][call["task_id"]]["active_attempt"] is not None
            return {**value, "final": "Independent fork contribution"}
        if len(case.native_calls) == 1:
            return {**value, "final": "Author's original version", "orchestration_request": {
                "coordinate": {"action": "continue", "reason": "Original work is unfinished"},
                "fork": [{"task": "Independent contribution", "suggest_key": WRITER,
                          "task_id": "A" if collision else "B"}]}}
        return {**value, "final": "Author's final version"}
    case.answer = answer
    out = case.core.dispatch_persistent(
        "Original objective", plan, qhash=team, mode="single", user_id=PEER_USER,
        conversation_id=PEER_CONVERSATION, max_rounds=3)
    assert case.core._winner_final(out) == "Author's final version"
    assert case.native_calls[-1]["checkpoint"]["session_id"] == "__ut_fork_cursor_" + ORIGINAL
    assert case.teams.load(team)["tasks"]["A"]["assigned_harness"] == ORIGINAL
    if collision:
        assert [call["key"] for call in case.native_calls] == [ORIGINAL, ORIGINAL]
        assert case.rounds[0]["out"]["results"][0]["fork_protocol_feedback"][0]["reason"] == "fork_task_id_already_exists"
    else:
        assert [call["key"] for call in case.native_calls] == [ORIGINAL, WRITER, ORIGINAL]
        saved_first = case.rounds[0]["state"]["tasks"]["A"]["result"]
        assert saved_first["fork_results"][0]["supporting_context"] is True
        assert saved_first["fork_results"][0]["mailbox_ref"]["recipient"] == "A"
        assert any(row["text"] == "Independent fork contribution"
                   for row in case.teams.load(team)["mailboxes"]["A"])


def test_fork_concurrent_task_collision_preserves_existing_result(graph):
    case, team = graph, "__ut_fork_concurrent_collision"
    plan = _plan(case, {"A": _node("Original work", owner=ORIGINAL)})
    plan["initial_author"] = {"task_id": "A", "harness": ORIGINAL}
    plan["team_policy"]["candidate_harnesses"] = [ORIGINAL, WRITER]
    real_add = case.teams.add_task

    def competing_add(team_id, task_id, *args, **kwargs):
        if task_id == "B":
            real_add(team_id, task_id, "Existing independent task", agent=WRITER,
                     metadata={"kind": "another_request"}, registered={ORIGINAL, WRITER})
            attempt = case.teams.begin_attempt(team_id, task_id, owner="fixture-other")
            assert case.teams.finish_attempt(team_id, task_id, attempt, {
                "status": "error", "final": "Existing partial work", "error": "Existing failure"})
        return real_add(team_id, task_id, *args, **kwargs)

    case.monkeypatch.setattr(case.teams, "add_task", competing_add)
    def answer(call):
        value = _returned(case, "__ut_collision_cursor_" + call["key"])
        if call["key"] == ORIGINAL:
            return {**value, "final": "Original partial work", "orchestration_request": {
                "fork": [{"task_id": "B", "task": "New work", "suggest_key": WRITER}]}}
        return {**value, "final": "Must never replace the existing task"}
    case.answer = answer
    out = case.core.dispatch("Original work", plan, qhash=team, mode="single", user_id=PEER_USER,
                             conversation_id=PEER_CONVERSATION)
    assert [call["key"] for call in case.native_calls] == [ORIGINAL]
    saved = case.teams.load(team)["tasks"]["B"]
    assert saved["text"] == "Existing independent task" and saved["attempt_seq"] == 1
    assert saved["result"]["final"] == "Existing partial work" and saved["result"]["error"] == "Existing failure"
    assert any(row["reason"] == "fork_task_id_already_exists"
               for row in _rows(out)["A"]["fork_protocol_feedback"])


def test_fork_waiter_stops_when_prior_child_exhausts_request_budget(graph):
    from test_request_budget_dispatch import BUDGET
    from test_native_terminal_dispatch_graph import REPAIR
    case, team = graph, "__ut_fork_request_budget"
    plan = _plan(case, {"A": _node("Original work", owner=ORIGINAL)})
    plan["initial_author"] = {"task_id": "A", "harness": ORIGINAL}
    plan["team_policy"].update(candidate_harnesses=[ORIGINAL, WRITER, REPAIR], max_parallel=1)
    def answer(call):
        value = _returned(case, "__ut_budget_cursor_" + call["key"])
        if call["key"] == ORIGINAL:
            return {**value, "final": "Original partial work", "orchestration_request": {"fork": [
                {"task_id": "B", "task": "First finding", "suggest_key": WRITER},
                {"task_id": "C", "task": "Second finding", "suggest_key": REPAIR}]}}
        if call["key"] == WRITER:
            return {**value, "status": "error", "final": "", "error": BUDGET["message"],
                    "error_envelope": deepcopy(BUDGET)}
        raise AssertionError("a stopped request budget cannot admit another native child")
    case.answer = answer
    out = case.core.dispatch("Original work", plan, qhash=team, mode="single", user_id=PEER_USER,
                             conversation_id=PEER_CONVERSATION)
    assert [call["key"] for call in case.native_calls] == [ORIGINAL, WRITER]
    saved = case.teams.load(team)["tasks"]
    assert saved["B"]["result"]["error_envelope"] == BUDGET
    assert saved["C"]["attempt_seq"] == 0 and saved["C"]["status"] == "blocked"
    assert _rows(out)["C"]["error_envelope"] == BUDGET
    assert _rows(out)["C"]["blocked_before_native"] is True
    assert _rows(out)["A"]["final"] == "Original partial work"


def test_fork_waiter_respects_parent_task_cancellation(graph):
    from test_native_terminal_dispatch_graph import REPAIR
    case, team = graph, "__ut_fork_parent_cancel"
    plan = _plan(case, {"A": _node("Original work", owner=ORIGINAL)})
    plan["initial_author"] = {"task_id": "A", "harness": ORIGINAL}
    plan["team_policy"].update(candidate_harnesses=[ORIGINAL, WRITER, REPAIR], max_parallel=1)
    def answer(call):
        value = _returned(case, "__ut_cancel_cursor_" + call["key"])
        if call["key"] == ORIGINAL:
            return {**value, "final": "Original partial work", "orchestration_request": {"fork": [
                {"task_id": "B", "task": "First finding", "suggest_key": WRITER},
                {"task_id": "C", "task": "Second finding", "suggest_key": REPAIR}]}}
        if call["key"] == WRITER:
            case.core.request_cancel(team, "A", scope="task")
            return {**value, "final": "Finished before cancellation"}
        raise AssertionError("cancelled parent must not admit the next child")
    case.answer = answer
    out = case.core.dispatch("Original work", plan, qhash=team, mode="single", user_id=PEER_USER,
                             conversation_id=PEER_CONVERSATION)
    assert [call["key"] for call in case.native_calls] == [ORIGINAL, WRITER]
    saved = case.teams.load(team)["tasks"]
    assert saved["C"]["attempt_seq"] == 0 and saved["C"]["status"] == "cancelled"
    assert _rows(out)["C"]["status"] == "cancelled"
    assert _rows(out)["A"]["final"] == "Original partial work"


def test_fork_native_exception_is_persisted_and_returned_to_author(graph):
    case, team = graph, "__ut_fork_native_exception"
    plan = _plan(case, {"A": _node("Original work", owner=ORIGINAL)})
    plan["initial_author"] = {"task_id": "A", "harness": ORIGINAL}
    plan["team_policy"]["candidate_harnesses"] = [ORIGINAL, WRITER]
    original_call = case.core._run_one_with_gateway
    def execute(key, *args, **kwargs):
        if key == WRITER and kwargs.get("qhash", "").endswith("_fork_B"):
            raise OSError("fixture native failure")
        return original_call(key, *args, **kwargs)
    case.monkeypatch.setattr(case.core, "_run_one_with_gateway", execute)
    case.answer = lambda call: {**_returned(case, "__ut_exception_cursor"),
        "final": "Original partial work", "orchestration_request": {
            "fork": [{"task_id": "B", "task": "Independent finding", "suggest_key": WRITER}]}}
    out = case.core.dispatch("Original work", plan, qhash=team, mode="single", user_id=PEER_USER,
                             conversation_id=PEER_CONVERSATION)
    saved = case.teams.load(team)["tasks"]["B"]
    assert saved["status"] == "error" and saved["active_attempt"] is None
    assert saved["result"]["error_envelope"]["code"] == "fork_execution_exception"
    assert _rows(out)["A"]["fork_results"][0]["error_envelope"]["code"] == "fork_execution_exception"
    assert any(row["text"] == "OSError" for row in case.teams.read_messages(team, "A", mark_read=False))
    assert _rows(out)["A"]["final"] == "Original partial work"


def test_initial_author_can_transfer_its_existing_task_without_fixed_leader(graph):
    case, team = graph, "__ut_author_transfer"
    plan = _plan(case, {"A": _node("Original work", owner=ORIGINAL)})
    plan["initial_author"] = {"task_id": "A", "harness": ORIGINAL}
    plan["team_policy"]["candidate_harnesses"] = [ORIGINAL, WRITER]

    def answer(call):
        returned = _returned(case, "__ut_transfer_cursor_" + call["key"])
        if call["key"] == ORIGINAL:
            return {**returned, "final": "Original version retained", "orchestration_request": {
                "coordinate": {"action": "continue", "reason": "Peer can now carry the complete work",
                    "task_updates": {"A": {"assigned_harness": WRITER,
                                           "task": "Continue the same project and preserve the original"}}}}}
        return {**returned, "final": "Transferred author completed the original work"}

    case.answer = answer
    out = case.core.dispatch_persistent(
        "Original work", plan, qhash=team, mode="single", user_id=PEER_USER,
        conversation_id=PEER_CONVERSATION, max_rounds=3, cb=case.events.append)
    assert [call["key"] for call in case.native_calls] == [ORIGINAL, WRITER]
    assert all(call["project_workdir"] == str(case.work) for call in case.native_calls)
    assert case.teams.load(team)["tasks"]["A"]["assigned_harness"] == WRITER
    assert case.core._winner_final(out) == "Transferred author completed the original work"
    assert len(case.teams.load(team)["tasks"]["A"]["result_history"]) == 1


def test_requested_peer_authorization_failure_pauses_and_preserves_author(graph):
    case, team = graph, "__ut_peer_denial"
    plan, original = _peer_setup(case, team)
    original["orchestration_request"]["coordinate"] = {"action": "continue", "reason": "await peer"}

    def answer(call):
        returned = _returned(case, "__ut_peer_cursor_" + call["key"])
        if call["key"] == ORIGINAL:
            return {**returned, **deepcopy(original)}
        return {**returned, "status": "error", "final": "", "error": "fixture denial",
                "native_facts": {"process_returncode": 1, "turn_status": "failed"},
                "error_envelope": {"category": "authorization_required", "scope": "provider",
                                   "code": "appid_disabled", "retryable": False,
                                   "message": "fixture denial"}}

    case.answer = answer
    out = case.core.dispatch_persistent(
        "original peer objective", plan, qhash=team, mode="single", user_id=PEER_USER,
        conversation_id=PEER_CONVERSATION, max_rounds=3, cb=case.events.append)
    assert [call["key"] for call in case.native_calls] == [ORIGINAL, WRITER]
    assert len(case.rounds) == 1 and not case.picks and not case.empty_plans
    assert out["pilot"]["kind"] == "provider_authorization_required"
    rows = _rows(out)
    assert rows["A"]["final"] == original["final"]
    assert rows["A"]["orchestration_request"] == original["orchestration_request"]
    peer, = [row for row in rows.values() if row.get("peer_for") == "A"]
    assert peer["error_envelope"]["code"] == "appid_disabled"
    assert peer["supporting_context"] is True
    saved = case.teams.load(team)["tasks"]
    assert saved["A"]["assigned_harness"] == ORIGINAL and saved["A"]["attempt_seq"] == 1
    assert saved[peer["task_id"]]["result"]["error_envelope"] == peer["error_envelope"]
    assert case.core._winner_final(out) == original["final"]


@pytest.mark.parametrize("scope", ["conversation", "request"])
def test_peer_support_keeps_author_continue_winner_and_single_mail_receipt(graph, scope):
    case, team = graph, "__ut_peer_continue_" + scope
    plan, request = _peer_setup(case, team)
    plan["tasks"]["A"]["native_session_scope"] = scope
    inputs, author_turns = [], []
    real_native = case.core._run_core_inprocess

    def observe_native(key, prompt, *args, **kwargs):
        inputs.append((key, prompt))
        return real_native(key, prompt, *args, **kwargs)

    case.monkeypatch.setattr(case.core, "_run_core_inprocess", observe_native)
    real_peer = case.core._run_requested_peer
    replays = []

    def observe_peer(*args, **kwargs):
        peer = real_peer(*args, **kwargs)
        if peer is not None:
            # Replay within the same author attempt: a new author attempt is
            # legitimately a different peer identity and must not be conflated.
            calls_before = deepcopy(case.native_calls)
            messages_before = case.teams.read_messages(team, "A", mark_read=False)
            replay_args = (*args[:2], deepcopy(args[2]), *args[3:])
            replay = real_peer(*replay_args, **kwargs)
            assert replay["retained_peer_input"] is True
            assert replay["task_id"] == peer["task_id"]
            assert case.native_calls == calls_before
            assert case.teams.read_messages(team, "A", mark_read=False) == messages_before
            replays.append(replay)
        return peer

    case.monkeypatch.setattr(case.core, "_run_requested_peer", observe_peer)

    def answer(call):
        returned = _returned(case, "__ut_peer_cursor_" + call["key"])
        if call["key"] == WRITER:
            return {**returned, "final": "independent CPU fact, not user delivery"}
        author_turns.append(call)
        if len(author_turns) == 1:
            return {**returned, **deepcopy(request), "orchestration_request": {
                **request["orchestration_request"],
                "coordinate": {"action": "continue", "reason": "consume independent input"}}}
        return {**returned, "final": "author actual completed delivery"}

    case.answer = answer
    out = case.core.dispatch_persistent(
        "original peer objective", plan, qhash=team, mode="single", user_id=PEER_USER,
        conversation_id=PEER_CONVERSATION, max_rounds=3, cb=case.events.append)
    assert [call["key"] for call in case.native_calls] == [ORIGINAL, WRITER, ORIGINAL]
    assert len(case.rounds) == 2 and not case.picks and not case.empty_plans
    assert all(call["scope"] == scope for call in author_turns)
    assert author_turns[1]["checkpoint"]["session_id"] == "__ut_peer_cursor_" + ORIGINAL
    assert "TASK_MAIL_CONTEXT_JSON" in inputs[-1][1]
    assert "independent CPU fact, not user delivery" in inputs[-1][1]
    assert case.core._winner_final(out) == "author actual completed delivery"
    rows = _rows(out)
    peer, = [row for row in rows.values() if row.get("peer_for") == "A"]
    assert peer["supporting_context"] is True and rows["A"]["assigned_harness"] == ORIGINAL
    saved = case.teams.load(team)["tasks"]
    assert saved["A"]["attempt_seq"] == 2 and saved[peer["task_id"]]["attempt_seq"] == 1
    messages = case.teams.read_messages(team, "A", mark_read=False)
    assert sum(message.get("idempotency_key") == peer["task_id"] for message in messages) == 1
    assert len(replays) == 1, "exercise persisted replay, not merely a single initial request"


def test_requested_peer_publication_error_preserves_author_and_pending_request(graph):
    case, team = graph, "__ut_peer_publication_error"
    plan, original = _peer_setup(case, team)
    draft = case.work / "index.html"
    original["artifacts"] = [{"path": str(draft), "kind": "html"}]
    before, draft_before = deepcopy(original), draft.read_bytes()
    case.answer = lambda call: {
        **_returned(case, "__ut_peer_publication_cursor"), "final": "independent CPU fact"}
    publications = []
    real_send = case.teams.send_message

    def denied_publication(*args, **kwargs):
        owner_check = kwargs["owner_check"]

        def deny(current):
            owner_check(current)
            publications.append(kwargs["idempotency_key"])
            raise PermissionError("fixture publication storage unavailable")

        return real_send(*args, **{**kwargs, "owner_check": deny})

    case.monkeypatch.setattr(case.teams, "send_message", denied_publication)
    assert _request_peer(case, team, plan, original) is None
    error = original.pop("peer_input_error")
    assert error and "PermissionError" in json.dumps(error)
    assert original == before, "author final, artifacts and pending need_peer must survive"
    assert draft.read_bytes() == draft_before
    assert [call["key"] for call in case.native_calls] == [WRITER]
    child_id, = publications
    saved = case.teams.load(team)
    assert saved["tasks"][child_id]["finished_at"]
    assert saved["tasks"][child_id]["active_attempt"] is None
    assert case.teams.resolve_result(team, child_id)["final"] == "independent CPU fact"
    assert saved.get("message_seq", 0) == 0
    assert case.teams.read_messages(team, "A", mark_read=False) == []


def test_requested_peer_native_exception_preserves_author_draft_and_pending_request(graph):
    case, team = graph, "__ut_peer_native_exception"
    plan, original = _peer_setup(case, team)
    draft = case.work / "index.html"
    original["artifacts"] = [{"path": str(draft), "kind": "html"}]
    before, draft_before = deepcopy(original), draft.read_bytes()
    calls = []

    def broken_native(key, *args, **kwargs):
        calls.append(key)
        raise RuntimeError("fixture peer native execution failed")

    case.monkeypatch.setattr(case.core, "_run_one_with_gateway", broken_native)
    assert _request_peer(case, team, plan, original) is None
    error = original.pop("peer_input_error")
    assert error and "RuntimeError" in json.dumps(error)
    assert original == before, "an optional peer failure must not erase the author draft"
    assert draft.read_bytes() == draft_before
    assert calls == [WRITER]
    assert case.teams.load(team).get("message_seq", 0) == 0
    assert case.teams.read_messages(team, "A", mark_read=False) == []
