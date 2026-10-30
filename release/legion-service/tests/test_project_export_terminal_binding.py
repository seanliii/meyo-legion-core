"""Terminal export regressions from the ordinary-entry identity shape.

Synthetic identities/files only. Real dispatch -> run_one -> exporter and
enrich execute; only native execution/discovery are isolated. Never load the
original order, operator token values, live state or model configuration.
"""
from contextlib import nullcontext
from copy import deepcopy
import hashlib
import importlib
import json
import os
from pathlib import Path

import pytest

from test_explicit_responses_provider import isolated, explicit_run_one


TEAM = "__ut_web_original_1792628078"
TASK = "codex"
USER = "owner-a"
CONVERSATION = "__ut_original_session"
LANE = CONVERSATION + ":task:" + TASK
NATIVE_THREAD = "__ut_original_native"


@pytest.fixture(autouse=True)
def scene(explicit_run_one):
    scene = explicit_run_one
    mp, root = scene.monkeypatch, scene.root
    for name, leaf in (
        ("CODEX_HOME", "codex-home"), ("TMPDIR", "native-tmp"),
        ("LEGION_PROJECT_CONTEXT_DIR", "projects"), ("LEGION_TEAM_DIR", "teams"),
        ("LEGION_SESSIONS_DIR", "sessions"), ("LEGION_TASK_MEMORY_DIR", "memory"),
        ("LEGION_ARTIFACT_STORE_DIR", "artifacts"), ("LEGION_USER_CONFIG_DIR", "users"),
    ):
        directory = root / leaf
        directory.mkdir(exist_ok=True)
        mp.setenv(name, str(directory))
    scene.pc = importlib.import_module("project_context")
    scene.ad = importlib.import_module("artifact_delivery")
    scene.identity = importlib.import_module("personal_identity")
    for module, leaf in (
        (scene.pc, "projects"), (importlib.import_module("task_memory"), "memory"),
        (importlib.import_module("artifact_store"), "artifacts"),
    ):
        mp.setattr(module, "ROOT", root / leaf)
    mp.setattr(scene.ad, "OUTPUTS", str(root / "service-outputs"))
    mp.setattr(scene.ad, "_git_changed_files", lambda _root: None)
    scene.work = root / "state/outputs/original-work"
    scene.work.mkdir(parents=True)
    (scene.work / "assets").mkdir()
    (scene.work / "assets/style.css").write_bytes(b"p { color: navy; }\n")
    (scene.work / "index.html").write_bytes(
        b'<link rel="stylesheet" href="assets/style.css"><p>Real fixture</p>')
    control = root / "operator-control"
    control.mkdir(mode=0o700)
    tokens = control / "identities.json"
    tokens.write_text(json.dumps({"fixture-token-not-an-original-key": {
        "user": USER, "project_grants": [{"root": str(scene.work), "export_paths": ["."]}],
    }}))
    tokens.chmod(0o600)
    mp.setenv("LEGION_PERSONAL_TOKENS_FILE", str(tokens))
    scene.plan = {
        "tasks": {TASK: {"task": "Return this existing fixture site",
                         "assigned_harness": TASK, "assignment_locked": True}},
        "project_context": {"repo_root": str(scene.work)},
    }
    # Same canonical shape as production: tasks is a list of task_id rows;
    # project conversation is the parent session, not the native :task: lane.
    scene.pc.initialize(TEAM, "original fixture request", deepcopy(scene.plan),
                        mode="single", user_id=USER, conversation_id=CONVERSATION)
    for name in ("_build_prelude", "_build_runtime_brief", "_read_async_deep_plans",
                 "_captain_mail_context"):
        mp.setattr(scene.core, name, lambda *a, **kw: "")
    mp.setattr(scene.core, "_aigc_ready", lambda **kw: {"runnable_clis": [TASK]})
    mp.setattr(scene.core, "_autonomy_matrix", lambda: {})
    mp.setattr(scene.core, "_baseline_snapshot", lambda *a, **kw: None)
    mp.setattr(importlib.import_module("model_registry"), "resolve_assignments",
               lambda *a, **kw: {})

    class Lease(nullcontext):
        wait_seconds = 0

    mp.setattr(scene.core, "_GatewayLease", lambda *a, **kw: Lease())
    scene.native_calls, scene.export_calls, scene.events = [], [], []

    def native(key, task, qhash, model_override=None, appid_override=None, **kwargs):
        scene.native_calls.append({
            "key": key, "qhash": qhash, "session_id": kwargs.get("session_id"),
            "project_conversation": (kwargs.get("project_context") or {}).get("conversation_id"),
        })
        return {
            "status": "ok", "final": str(scene.work / "index.html"),
            "workdir": str(scene.work),
            "native_checkpoint": {"kind": "codex_thread", "session_id": NATIVE_THREAD},
            "native_facts": {"process_returncode": 0, "turn_status": "completed"},
        }

    mp.setattr(scene.core, "_run_core_inprocess", native)
    scene.real_export = scene.ad.export_project_artifacts

    def observe_export(paths, **kwargs):
        # Observe, do not substitute identity or bypass production admission.
        result = scene.real_export(paths, **kwargs)
        scene.export_calls.append({
            **kwargs, "paths": list(paths),
            "reasons": [n["reason"] for n in result["artifact_safety_observations"]],
            "exported_files": len(result["artifacts"]),
        })
        return result

    mp.setattr(scene.ad, "export_project_artifacts", observe_export)
    return scene


def direct_export(scene, *, conversation=CONVERSATION, user=USER):
    return scene.real_export(
        ["."], team_id=TEAM, task_id=TASK, user_id=user, conversation_id=conversation)


@pytest.mark.parametrize("check", ["project_identity", "exported_resources"])
def test_real_dispatch_keeps_native_lane_but_exports_under_project_identity(scene, check):
    assert scene.identity.project_grant(USER, str(scene.work))["export_paths"] == ["."]
    out = scene.core.dispatch(
        "original fixture request", deepcopy(scene.plan), qhash=TEAM, mode="single",
        user_id=USER, conversation_id=CONVERSATION, cb=scene.events.append)
    row, = out["results"]
    assert row["status"] == "done", row
    assert len(scene.native_calls) == 1 and len(scene.export_calls) == 1
    assert row["native_conversation_id"] == LANE, "do not fix export by changing native resume identity"
    assert row["native_checkpoint"]["session_id"] == NATIVE_THREAD
    packet = scene.pc.load(TEAM)
    assert (packet["team_id"], packet["user_id"], packet["conversation_id"]) == (
        TEAM, USER, CONVERSATION)
    call, = scene.export_calls
    assert (call["team_id"], call["task_id"], call["user_id"]) == (TEAM, TASK, USER)
    print("TERMINAL_EXPORT_BINDING", json.dumps({
        "canonical_conversation": packet["conversation_id"],
        "native_conversation": row["native_conversation_id"],
        "export_conversation": call["conversation_id"],
        "exported_files": call["exported_files"], "reasons": call["reasons"],
    }))
    if check == "project_identity":
        assert call["conversation_id"] == CONVERSATION, "native task lane is not the project export identity"
    else:
        assert {a["name"] for a in row.get("artifacts") or []} == {"index.html", "style.css"}
        for artifact in row["artifacts"]:
            assert Path(artifact["path"]).read_bytes() == Path(artifact["provenance"]["source_path"]).read_bytes()
            assert hashlib.sha256(Path(artifact["path"]).read_bytes()).hexdigest() == artifact["sha256"]


def test_absent_terminal_exports_real_files_without_claiming_delivery_or_switching_author(scene):
    from test_native_final_diagnostics import absent_message_facts

    original = scene.core._run_core_inprocess
    selections = []

    def native(*args, **kwargs):
        result = original(*args, **kwargs)
        facts = absent_message_facts()
        facts["completion_message"]["thread_id"] = NATIVE_THREAD
        return {**result, "final": "", "native_facts": facts}

    scene.monkeypatch.setattr(scene.core, "_run_core_inprocess", native)
    scene.monkeypatch.setattr(
        scene.core, "_pick_relay_harness",
        lambda *args, **kwargs: selections.append((args, kwargs)) or None)
    plan = deepcopy(scene.plan)
    plan.update({
        "_user_selected_mode": True,
        "delivery_contract": {"require_final": True},
        "team_policy": {"adaptive": False, "max_failover": 1, "contract_repair_rounds": 1},
        "success_audit": {"enabled": False},
    })
    out = scene.core.dispatch(
        "original fixture request", plan, qhash=TEAM, mode="single",
        user_id=USER, conversation_id=CONVERSATION, cb=scene.events.append)
    row, = out["results"]
    assert row["status"] == "partial", row
    assert row["final"] == "" and row["error"] == ""
    assert len(scene.native_calls) == 1 and len(scene.export_calls) == 1
    assert {a["name"] for a in row["artifacts"]} == {"index.html", "style.css"}
    assert row["native_checkpoint"]["session_id"] == NATIVE_THREAD
    assert row["native_conversation_id"] == LANE
    assert scene.export_calls[0]["conversation_id"] == CONVERSATION
    assert selections == [], "missing final text alone is not model or author failure"
    assert not scene.core._is_delivered(row)
    assert out["outcome"]["status"] != "completed"


def test_exporter_does_not_accept_lane_or_another_owner_as_project_identity(scene):
    lane = direct_export(scene, conversation=LANE)
    assert not lane["artifacts"]
    assert lane["artifact_safety_observations"][0]["reason"] == "artifact_project_binding_required"
    foreign = direct_export(scene, user="another-owner")
    assert not foreign["artifacts"]
    assert foreign["artifact_safety_observations"][0]["reason"] == "artifact_owner_denied"
    correct = direct_export(scene)
    assert correct["artifact_safety_observations"] == []
    assert {a["name"] for a in correct["artifacts"]} == {"index.html", "style.css"}


@pytest.mark.parametrize("had_early_note", [False, True])
def test_final_enrich_surfaces_actual_terminal_export_rejection(scene, had_early_note):
    # Generate real rejection from the same canonical-vs-native-lane mismatch,
    # rather than inventing an error or changing export checks.
    rejected = direct_export(scene, conversation=LANE)
    terminal_notes = deepcopy(rejected["artifact_safety_observations"])
    member = {"key": TASK, "task_id": TASK, "status": "done",
              "workdir": str(scene.work), "final": str(scene.work / "index.html"),
              "artifact_safety_observations": deepcopy(terminal_notes),
              "artifact_export": {"exported_files": 0, "observations": deepcopy(terminal_notes)}}
    out = {"team_id": TEAM, "results": [member]}
    if had_early_note:
        early_notes = []
        assert scene.ad.scan_new_files(
            1, workdir=str(scene.work), safety_observations=early_notes) == []
        out["artifact_safety_observations"] = early_notes
    scene.ad.enrich(out, since_ts=1, workdirs=[str(scene.work)])
    actual = [n["reason"] for n in out.get("artifact_safety_observations") or []]
    print("TERMINAL_EXPORT_NOTES", json.dumps({
        "member_reasons": [n["reason"] for n in terminal_notes],
        "top_level_reasons": actual, "had_early_note": had_early_note,
    }))
    assert member["artifact_safety_observations"] == terminal_notes
    assert "artifact_project_binding_required" in actual, "generic scan must not hide the terminal rejection"


def test_final_enrich_does_not_invent_unmapped_failure_after_successful_export(scene):
    exported = direct_export(scene)
    assert len(exported["artifacts"]) == 2
    out = {"team_id": TEAM, "results": [{
        "key": TASK, "task_id": TASK, "status": "done", "workdir": str(scene.work),
        "final": str(scene.work / "index.html"), "artifacts": exported["artifacts"],
    }]}
    scene.ad.enrich(out, since_ts=1, workdirs=[str(scene.work)])
    reasons = [n["reason"] for n in out.get("artifact_safety_observations") or []]
    assert "artifact_workdir_unmapped" not in reasons, "this workdir already has a verified export mapping"
    assert len(out["results"][0]["artifacts"]) == 2


@pytest.mark.parametrize("method", ["finish_attempt", "replace_result"])
@pytest.mark.parametrize("field", ["artifact_export", "artifact_safety_observations"])
def test_team_result_preserves_current_export_facts_after_disk_readback(scene, method, field):
    teams = importlib.import_module("team_state")
    teams.ensure(TEAM, "fixture", scene.plan["tasks"], {TASK: []},
                 user_id=USER, conversation_id=CONVERSATION, registered={TASK})
    attempt = teams.begin_attempt(TEAM, TASK, owner=TASK)
    rejected = direct_export(scene, conversation=LANE)
    notes = rejected["artifact_safety_observations"]
    result = {
        "status": "done", "final": "fixture result",
        "native_checkpoint": {"kind": "codex_thread", "session_id": NATIVE_THREAD},
        "native_facts": {"process_returncode": 0, "turn_status": "completed"},
        "artifact_export": {"exported_files": 0, "observations": deepcopy(notes)},
        "artifact_safety_observations": deepcopy(notes),
    }
    if method == "finish_attempt":
        assert teams.finish_attempt(TEAM, TASK, attempt, result)
    else:
        assert teams.finish_attempt(TEAM, TASK, attempt, {"status": "error", "final": ""})
        assert teams.replace_result(TEAM, TASK, result, previous_harness=TASK)
    on_disk = json.loads(teams._path(TEAM).read_text())["tasks"][TASK]["result"]
    restored = teams.resolve_result(TEAM, TASK)
    print("PERSISTED_EXPORT_FACT", json.dumps({
        "method": method, "field": field, "present_on_disk": field in on_disk,
        "present_after_resolve": field in restored,
    }))
    assert on_disk.get(field) == result[field], "terminal export facts were dropped by result normalization"
    assert restored.get(field) == result[field]
    assert restored["native_facts"] == result["native_facts"], "host export facts remain parallel"
    assert restored["status"] == "done", "export observations must not redefine task completion"
    assert restored["native_checkpoint"] == result["native_checkpoint"]


@pytest.mark.parametrize("conversation", [
    CONVERSATION + ":task:another-task",
    CONVERSATION + "-another:task:" + TASK,
    CONVERSATION + ":task:" + TASK + ":suffix",
])
def test_export_cannot_rebind_unrelated_native_lane_to_canonical_project(scene, conversation):
    result = scene.core.run_one(
        TASK, "fixture result", qhash=TEAM + "_task_" + TASK,
        user_id=USER, conversation_id=conversation,
        task_facts={"team_id": TEAM, "task_id": TASK,
                    "project_context": scene.pc.load(TEAM)})
    assert result["status"] == "done"
    assert len(scene.native_calls) == 1
    assert not result.get("artifacts"), "do not strip suffixes or accept a similar session prefix"
    assert result["artifact_safety_observations"]
    assert result["native_conversation_id"] == conversation


@pytest.mark.parametrize("changed_field", ["conversation_id", "repo_root", "assigned_harness"])
def test_export_revalidates_canonical_binding_after_native_execution(scene, changed_field):
    original = scene.pc.load(TEAM)
    native = scene.core._run_core_inprocess

    def changed(*args, **kwargs):
        result = native(*args, **kwargs)
        packet = scene.pc.load(TEAM)
        if changed_field == "conversation_id":
            packet["conversation_id"] = CONVERSATION + "-different"
        elif changed_field == "repo_root":
            other = scene.root / "another-project"
            other.mkdir()
            packet["repo_root"] = str(other)
        else:
            packet["tasks"][0]["assigned_harness"] = "__ut_other_member"
        scene.pc._atomic_write(scene.pc._path(TEAM), packet)
        return result

    scene.monkeypatch.setattr(scene.core, "_run_core_inprocess", changed)
    result = scene.core.run_one(
        TASK, "fixture result", qhash=TEAM + "_task_" + TASK, user_id=USER,
        conversation_id=CONVERSATION, task_facts={
            "team_id": TEAM, "task_id": TASK, "project_context": original})
    assert result["status"] == "done"
    assert not result.get("artifacts"), "changed canonical identity must not be silently adopted"
    assert result["artifact_safety_observations"]


def test_enrich_merges_notes_without_cross_task_loss_or_duplicates(scene):
    note = scene.ad._not_exported("artifact_project_binding_required")
    other_note = scene.ad._not_exported("artifact_sensitive_content")
    first = {"task_id": "first", "artifact_safety_observations": [deepcopy(note)],
             "artifact_export": {"exported_files": 0, "observations": [deepcopy(note)]}}
    second = {"task_id": "second", "artifact_safety_observations": [deepcopy(note), other_note]}
    out = {"team_id": TEAM, "results": [first, second]}
    scene.ad.enrich(out)
    notes = deepcopy(out.get("artifact_safety_observations") or [])
    scene.ad.enrich(out)
    assert out.get("artifact_safety_observations") == notes, "enrich must not multiply the same facts"
    assert {(n.get("task_id"), n["reason"]) for n in notes} == {
        ("first", "artifact_project_binding_required"),
        ("second", "artifact_project_binding_required"),
        ("second", "artifact_sensitive_content")}
    assert first["artifact_safety_observations"] == [note]


def test_processed_project_does_not_suppress_other_workdir_discovery(scene):
    exported = direct_export(scene)
    other = scene.root / "data/outputs/other-task"
    other.mkdir(parents=True)
    (other / "other.txt").write_text("other task in the same authorized request")
    out = {"team_id": TEAM, "results": [
        {"key": TASK, "task_id": TASK, "status": "done", "workdir": str(scene.work),
         "artifacts": exported["artifacts"]},
        {"key": "another-task", "task_id": "another-task", "workdir": str(other)},
    ]}
    scene.ad.enrich(out, since_ts=1, workdirs=[str(scene.work), str(other)])
    assert [row["path"] for row in out.get("discovered_artifacts", [])] == [str(other / "other.txt")]
    assert "artifact_workdir_unmapped" not in [
        n["reason"] for n in out.get("artifact_safety_observations") or []]


@pytest.mark.parametrize("refused", [False, True])
def test_enrich_cannot_bypass_processed_export_scope_with_final_paths(scene, refused):
    # Even a globally served source root is not permission to bypass this
    # export's selection or reintroduce editable source URLs beside snapshots.
    scene.work = scene.root / "data/outputs/served-project"
    (scene.work / "site").mkdir(parents=True)
    (scene.work / "site/index.html").write_text("selected fixture")
    (scene.work / "notes.txt").write_text("not selected for export")
    scene.pc.initialize(
        TEAM, "fixture", {"tasks": scene.plan["tasks"],
                         "project_context": {"repo_root": str(scene.work)}},
        mode="single", user_id=USER, conversation_id=CONVERSATION)
    tokens = Path(os.environ["LEGION_PERSONAL_TOKENS_FILE"])
    tokens.write_text(json.dumps({"fixture-token-not-an-original-key": {
        "user": USER, "project_grants": [{"root": str(scene.work), "export_paths": ["site"]}]}}))
    exported = scene.real_export(
        ["notes.txt"] if refused else ["site"], team_id=TEAM, task_id=TASK,
        user_id=USER, conversation_id=CONVERSATION)
    if refused:
        assert not exported["artifacts"]
    else:
        assert len(exported["artifacts"]) == 1
    member = {
        "key": TASK, "task_id": TASK, "status": "done", "workdir": str(scene.work),
        "final": f"`{scene.work / 'notes.txt'}` `{scene.work / 'site/index.html'}`",
        "artifacts": exported["artifacts"],
        "artifact_export": {"exported_files": len(exported["artifacts"]),
                            "observations": exported["artifact_safety_observations"]},
    }
    expected_paths = [a["path"] for a in exported["artifacts"]]
    out = {"team_id": TEAM, "results": [member]}
    scene.ad.enrich(out, since_ts=1, workdirs=[str(scene.work)])
    assert [a["path"] for a in member["artifacts"]] == expected_paths
    assert not out.get("discovered_artifacts")


def test_first_default_allocation_still_uses_exact_legacy_scan(scene):
    scene.work = scene.root / "data/outputs" / f"{TASK}_{TEAM}_task_{TASK}"
    scene.work.mkdir(parents=True)
    (scene.work / "index.html").write_text("already produced by fixture")
    Path(os.environ["LEGION_PERSONAL_TOKENS_FILE"]).write_text(
        json.dumps({"fixture-token-not-an-original-key": {"user": USER}}))
    plan = deepcopy(scene.plan)
    plan["project_context"] = {"repo_root": str(scene.work)}
    # Keep the real default scan's mtime window by writing at the native boundary.
    native = scene.core._run_core_inprocess

    def write_during_native(*args, **kwargs):
        (scene.work / "index.html").write_text("new native fixture bytes")
        return native(*args, **kwargs)

    scene.monkeypatch.setattr(scene.core, "_run_core_inprocess", write_during_native)
    out = scene.core.dispatch(
        "fixture default allocation", plan, qhash=TEAM, mode="single",
        user_id=USER, conversation_id=CONVERSATION)
    row, = out["results"]
    assert row["status"] == "done"
    assert [a["path"] for a in row.get("artifacts") or []] == [str(scene.work / "index.html")]
    assert scene.export_calls == [], "do not turn default allocation into a new external-root grant"


@pytest.mark.parametrize("method", ["finish_attempt", "replace_result"])
def test_legacy_result_does_not_gain_fictitious_export_facts(scene, method):
    teams = importlib.import_module("team_state")
    teams.ensure(TEAM, "fixture", scene.plan["tasks"], {TASK: []},
                 user_id=USER, conversation_id=CONVERSATION, registered={TASK})
    attempt = teams.begin_attempt(TEAM, TASK, owner=TASK)
    result = {"status": "done", "final": "legacy result"}
    if method == "finish_attempt":
        assert teams.finish_attempt(TEAM, TASK, attempt, result)
    else:
        assert teams.finish_attempt(TEAM, TASK, attempt, {"status": "error", "final": ""})
        assert teams.replace_result(TEAM, TASK, result)
    restored = teams.resolve_result(TEAM, TASK)
    assert "artifact_export" not in restored and "artifact_safety_observations" not in restored
