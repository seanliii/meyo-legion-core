"""Actual persistent context, not a model-generated assertion of continuity."""
import importlib
import copy
import json
from pathlib import Path
import socket

import pytest

SERVICE = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_context(tmp_path, monkeypatch):
    for name, leaf in (
        ("MEYO_LEGION_DIR", "data"), ("LEGION_HOME_DIR", "home"),
        ("LEGION_PROJECT_CONTEXT_DIR", "projects"), ("LEGION_TASK_MEMORY_DIR", "memory"),
        ("LEGION_TEAM_DIR", "teams"), ("LEGION_SESSIONS_DIR", "sessions"),
        ("LEGION_ARTIFACT_STORE_DIR", "report-artifacts"),
    ):
        monkeypatch.setenv(name, str(tmp_path / leaf))
    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    monkeypatch.setattr(socket.socket, "connect", lambda *args, **kwargs:
                        pytest.fail("quality-context tests must not access the network"))
    monkeypatch.syspath_prepend(str(SERVICE))
    monkeypatch.setattr(importlib.import_module("artifact_store"), "ROOT", tmp_path / "report-artifacts")
    yield importlib.import_module("project_context")


def original(isolated_context, tmp_path):
    pc = isolated_context
    pc.initialize(
        "__ut_quality_resume", "original user objective",
        {"project_context": {
            "repo_root": str(tmp_path / "work"),
            "acceptance": ["the original user journey works"],
            "golden_task": {"input": "original input", "result": "usable result"},
            "experience_intent": {"user_result": "finish the original journey"},
            "critical_dimensions": ["correctness", "first-use experience"],
            "quality_context": {
                "sources": [{"url": "https://example.invalid/reference", "status": "fetched"}]},
        }},
        mode="orchestrate", user_id="fixture-owner", conversation_id="fixture-conversation")
    return pc.record_verification("__ut_quality_resume", {
        "report_id": "actual-journey-1", "role": "direction_reviewer",
        "journey_observations": [{
            "step": "open original result", "observation": "the required action did not change state",
            "evidence_ref": "evidence/actual-run.json"}],
        "gap_plan": [{"id": "core-action", "dimension": "correctness", "status": "open",
                      "gap": "required action is ineffective", "evidence_ref": "evidence/actual-run.json"}],
        "recommended_action": "continue",
    })


def test_sparse_resume_preserves_actual_quality_context_and_task_intent(isolated_context, tmp_path):
    before = original(isolated_context, tmp_path)
    after = isolated_context.initialize(
        "__ut_quality_resume", "original user objective",
        {"tasks": {"maker": {"task": "continue in the original workspace"}}},
        mode="orchestrate", user_id="fixture-owner", conversation_id="fixture-conversation")
    for field in ("repo_root", "acceptance", "golden_task", "experience_intent",
                  "critical_dimensions", "journey_observations", "gap_plan",
                  "open_gaps", "verification_findings"):
        assert after[field] == before[field], field
    view = isolated_context.snapshot("__ut_quality_resume", "maker", packet=after)
    assert view["journey_observations"] == before["journey_observations"]
    assert view["gap_plan"] == before["gap_plan"]
    assert "example.invalid/reference" in str(view["quality_context"])


def test_explicit_revised_intent_wins_over_persisted_context(isolated_context, tmp_path):
    original(isolated_context, tmp_path)
    after = isolated_context.initialize(
        "__ut_quality_resume", "original user objective",
        {"experience_intent": {"user_result": "explicitly revised by current task"},
         "acceptance": ["revised acceptance"],
         "project_context": {"golden_task": {"input": "explicitly revised input"}}},
        mode="orchestrate", user_id="fixture-owner", conversation_id="fixture-conversation")
    assert after["experience_intent"]["user_result"] == "explicitly revised by current task"
    assert after["acceptance"] == ["revised acceptance"]
    assert after["golden_task"] == {"input": "explicitly revised input"}
    assert after["journey_observations"], "revising intent must not erase actual observations"


def test_quality_context_cannot_be_inherited_by_another_owner(isolated_context, tmp_path):
    before = original(isolated_context, tmp_path)
    with pytest.raises(PermissionError):
        isolated_context.initialize(
            "__ut_quality_resume", "different user objective", {},
            mode="orchestrate", user_id="other-owner", conversation_id="other-conversation")
    assert isolated_context.load("__ut_quality_resume") == before


def member_project(pc, tmp_path):
    return pc.initialize(
        "__ut_member_reports", "original user objective",
        {"tasks": {"maker": "make the product", "peer": "check another part"},
         "project_context": {"repo_root": str(tmp_path / "work")}},
        mode="orchestrate", user_id="fixture-owner", conversation_id="fixture-conversation")


def member_report(label="maker", status="open"):
    return {
        "sources": [{
            "source_id": "shared-id", "url": f"https://example.invalid/{label}",
            "snapshot_ref": f"research/{label}.json", "fetch_status": "fetched",
            "decision": "adopt", "decision_reason": f"{label} explains the design choice",
        }],
        "journey_observations": [{
            "step": "use the required action", "observation": f"{label} observed no change",
            "evidence_ref": f"tests/{label}.json", "source_ids": ["shared-id"],
        }],
        "gap_plan": [{
            "gap_id": "same-local-id", "finding": "required action is ineffective",
            "status": status, "evidence_refs": [f"tests/{label}.json"],
        }],
    }


def publish_member(pc, task_id, report, *, harness="codex"):
    return pc.record_result(
        "__ut_member_reports", task_id,
        {"status": "done", "harness": harness,
         "final": "Partial work is available.\n<<LEGION_FRONTIER_CONTEXT>>"
                  + json.dumps(report) + "<<END>>"})


def read_report_reference(reference):
    value = json.loads(Path(reference["path"]).read_text())
    pointer = reference["json_pointer"]
    for part in pointer.lstrip("/").split("/") if pointer else []:
        value = value[part.replace("~1", "/").replace("~0", "~")]
    return value


def test_conditional_contribution_report_preserves_origin_without_certifying_effect(isolated_context, tmp_path):
    pc = isolated_context
    member_project(pc, tmp_path)
    report = {"contributions": [{
        "bottleneck": "The current design lacks evidence for one important assumption",
        "conditions": {"model": "fixture-model", "budget": "same request"},
        "contribution_refs": [{"task_id": "peer", "sha256": "fixture-content-hash"}],
        "adoption": "adopted", "artifact_refs": [{"path": "result.html", "sha256": "fixture-new-version"}],
        "verified": True,
    }]}
    publish_member(pc, "maker", report)
    view = pc.snapshot("__ut_member_reports", "peer")
    row, = view["quality_context"]["contributions"]
    assert row["bottleneck"] == report["contributions"][0]["bottleneck"]
    assert row["adoption"] == "adopted", "keep the author's claim, not an inferred verdict"
    assert row["verified"] is False and row["independent_verification"] is False
    assert row["provenance"]["task_id"] == "maker"
    assert read_report_reference(row["provenance"]["report_ref"]) == report
    assert view["frontier_context"]["contributions"] == [row]
    assert view["claim_ceiling"] == "UNVALIDATED"
    assert not view["verification_findings"]


def test_member_frame_reaches_next_snapshot_with_revisitable_provenance(isolated_context, tmp_path):
    pc = isolated_context
    member_project(pc, tmp_path)
    report = member_report()
    # A member cannot grant itself an independent review role through its payload.
    report.update(task_id="someone-else", role="direction_reviewer", verified=True)
    publish_member(pc, "maker", report)
    view = pc.snapshot("__ut_member_reports", "peer")
    source = view["quality_context"]["sources"][0]
    assert source["url"] == "https://example.invalid/maker"
    assert source["decision_reason"] == "maker explains the design choice"
    assert source["reported_source_id"] == "shared-id"
    assert source["source_id"] != "shared-id"
    provenance = source["provenance"]
    assert provenance["kind"] == "member_report"
    assert provenance["task_id"] == "maker" and provenance["harness"] == "codex"
    assert provenance["independent_verification"] is False
    assert read_report_reference(provenance["report_ref"]) == report
    observation = view["journey_observations"][0]
    assert observation["evidence_ref"] == "tests/maker.json"
    assert observation["source_ids"] == [source["source_id"]]
    assert observation["provenance"] == provenance
    gap = view["gap_plan"][0]
    assert gap["problem"] == "required action is ineffective"
    assert gap["owner_task"] == "maker"
    locator = provenance["report_ref"]["path"] + "#" + provenance["report_ref"]["json_pointer"]
    assert "tests/maker.json" in gap["evidence_refs"]
    normalized = importlib.import_module("quality_context").normalize_gaps([gap])[0]
    assert locator in normalized["evidence_refs"], "normalization must retain the raw-report reference"
    assert view["open_gaps"]
    assert view["verification_findings"] == []
    assert view["claim_ceiling"] == "UNVALIDATED"
    assert view["frontier_context"].get("role") != "direction_reviewer"
    assert view["frontier_context"].get("verified") is not True
    assert view["frontier_context"]["source_kind"] == "member_report"


def test_member_reports_do_not_replace_other_tasks_or_async_review(isolated_context, tmp_path):
    pc = isolated_context
    member_project(pc, tmp_path)
    review = {
        "report_id": "independent-1", "task_id": "reviewer", "role": "direction_reviewer",
        "journey_observations": [{
            "step": "review action", "observation": "still ineffective",
            "evidence_ref": "tests/independent.json"}],
        "gap_plan": [{
            "gap_id": "review-gap", "problem": "required action is ineffective",
            "status": "open", "evidence_refs": ["tests/independent.json"]}],
        "claim_ceiling": "UNVALIDATED",
    }
    before = pc.record_verification("__ut_member_reports", review)
    closed = member_report(status="closed")
    closed["claim_ceiling"] = "VERIFIED_NARROW_WIN"
    publish_member(pc, "maker", closed)
    publish_member(pc, "peer", member_report("peer"))
    after = pc.snapshot("__ut_member_reports", "maker")
    assert before["gap_plan"][0] in after["gap_plan"], "member closure must not overwrite a reviewed gap"
    assert before["journey_observations"][0] in after["journey_observations"]
    assert after["verification_findings"] == before["verification_findings"]
    assert after["claim_ceiling"] == "UNVALIDATED"
    assert {row["url"] for row in after["quality_context"]["sources"]} == {
        "https://example.invalid/maker", "https://example.invalid/peer"}
    assert len({row["source_id"] for row in after["quality_context"]["sources"]}) == 2
    member_gap = next(g for g in after["gap_plan"] if g.get("owner_task") == "maker")
    assert member_gap["reported_status"] == "closed"
    assert member_gap["status"] == "closed", "preserve the member's actual report"
    assert member_gap not in after["open_gaps"]
    assert member_gap["source"] == "member_report"
    assert member_gap["independent_verification"] is False
    # A later independent callback must not erase the member's observed evidence either.
    pc.record_verification("__ut_member_reports", {
        **review, "report_id": "independent-2",
        "journey_observations": [{"step": "late review", "evidence_ref": "tests/late.json"}]})
    latest = pc.snapshot("__ut_member_reports", "peer")
    assert any(row.get("evidence_ref") == "tests/maker.json" for row in latest["journey_observations"])
    assert len(latest["quality_context"]["member_reports"]) == 2


def test_member_report_survives_sparse_resume_and_duplicate_delivery(isolated_context, tmp_path):
    pc = isolated_context
    member_project(pc, tmp_path)
    report = member_report()
    publish_member(pc, "maker", report)
    before = pc.load("__ut_member_reports")
    publish_member(pc, "maker", copy.deepcopy(report))
    pc.initialize("__ut_member_reports", "original user objective",
                  {"tasks": {"peer": "continue using recorded work"}},
                  mode="orchestrate", user_id="fixture-owner", conversation_id="fixture-conversation")
    after = pc.snapshot("__ut_member_reports", "peer")
    assert len(after["quality_context"]["member_reports"]) == 1
    assert after["quality_context"]["sources"] == before["quality_context"]["sources"]
    assert pc.load("__ut_member_reports")["quality_context"]["member_reports"] == before["quality_context"]["member_reports"]
    for field in ("journey_observations", "gap_plan", "open_gaps"):
        assert after[field] == before[field]
    assert after["quality_context"]["sources"][0]["decision_reason"]


@pytest.mark.parametrize("final", [
    "See docs/research.md for the report.",
    '<<LEGION_FRONTIER_CONTEXT>>{"sources":[<<END>>',
])
def test_arbitrary_markdown_or_malformed_frame_is_not_promoted(isolated_context, tmp_path, final):
    pc = isolated_context
    before = member_project(pc, tmp_path)
    document = tmp_path / "research.md"
    document.write_text("<<LEGION_FRONTIER_CONTEXT>>" + json.dumps(member_report()) + "<<END>>")
    pc.record_result("__ut_member_reports", "maker", {
        "status": "done", "harness": "codex", "final": final,
        "artifacts": [{"path": str(document), "kind": "md"}]})
    after = pc.snapshot("__ut_member_reports", "peer")
    assert after["quality_context"] == before["quality_context"]
    assert after["journey_observations"] == []
    assert after["gap_plan"] == []


def test_concurrent_member_callbacks_keep_each_raw_report(isolated_context, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    pc = isolated_context
    member_project(pc, tmp_path)
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda label: publish_member(pc, label, member_report(label)), ("maker", "peer")))
    view = pc.snapshot("__ut_member_reports", "maker")
    assert len(view["quality_context"]["member_reports"]) == 2
    assert {row["provenance"]["task_id"] for row in view["journey_observations"]} == {"maker", "peer"}
    assert len(view["gap_plan"]) == 2, "identical problem text from two tasks must retain both reports"


def test_local_source_and_single_evidence_ref_survive_normalization(isolated_context, tmp_path):
    pc = isolated_context
    member_project(pc, tmp_path)
    report = {
        "sources": [{"source_id": "local", "path": "research/observation.json",
                     "decision_reason": "the local observation changed the implementation"}],
        "gap_plan": [{"defect": "observed behavior differs from the requested outcome",
                      "evidence_ref": "tests/observed-step.json"}],
    }
    publish_member(pc, "maker", report)
    view = pc.snapshot("__ut_member_reports", "peer")
    source = view["quality_context"]["sources"][0]
    assert "research/observation.json" in source["evidence_paths"]
    assert source["decision_reason"] == report["sources"][0]["decision_reason"]
    gap = view["gap_plan"][0]
    assert "tests/observed-step.json" in gap["evidence_refs"]
    assert read_report_reference(gap["provenance"]["report_ref"]) == report


def test_namespaced_source_ids_keep_observation_and_gap_links_valid(isolated_context, tmp_path):
    pc = isolated_context
    member_project(pc, tmp_path)
    report = member_report()
    report["journey_observations"][0].pop("source_ids")
    report["journey_observations"][0]["source_id"] = "shared-id"
    report["journey_observations"][0]["evidence_refs"] = ["shared-id"]
    report["gap_plan"][0]["evidence_refs"] = ["shared-id", "tests/maker.json"]
    publish_member(pc, "maker", report)
    view = pc.snapshot("__ut_member_reports", "peer")
    source_id = view["quality_context"]["sources"][0]["source_id"]
    assert view["journey_observations"][0]["source_id"] == source_id
    assert source_id in view["journey_observations"][0]["evidence_refs"]
    assert source_id in view["gap_plan"][0]["evidence_refs"]
    assert "shared-id" not in view["gap_plan"][0]["evidence_refs"]
    assert "tests/maker.json" in view["gap_plan"][0]["evidence_refs"]


@pytest.mark.parametrize("has_gap_id", [True, False])
def test_member_closes_only_its_own_stable_gap_and_keeps_report_versions(
        isolated_context, tmp_path, has_gap_id):
    pc = isolated_context
    member_project(pc, tmp_path)
    review = {
        "report_id": "independent-gap", "task_id": "reviewer", "role": "direction_reviewer",
        "gap_plan": [{"gap_id": "same-local-id", "problem": "required action is ineffective",
                      "status": "open", "evidence_refs": ["tests/independent.json"]}],
    }
    before = pc.record_verification("__ut_member_reports", review)
    opened = member_report()
    if not has_gap_id:
        opened["gap_plan"][0].pop("gap_id")
    publish_member(pc, "maker", opened)
    publish_member(pc, "peer", copy.deepcopy(opened))
    publish_member(pc, "maker", copy.deepcopy(opened), harness="hermes")
    old = pc.snapshot("__ut_member_reports", "maker")
    own = next(g for g in old["gap_plan"]
               if g.get("provenance", {}).get("task_id") == "maker"
               and g["provenance"]["harness"] == "codex")
    closed = copy.deepcopy(opened)
    closed["gap_plan"][0]["status"] = "closed"
    closed["gap_plan"][0]["evidence_refs"] = ["tests/fixed.json"]
    if has_gap_id:
        closed["gap_plan"][0]["finding"] = "the same identified action has now been repaired"
    publish_member(pc, "maker", closed)
    publish_member(pc, "maker", copy.deepcopy(closed))
    pc.initialize("__ut_member_reports", "original user objective", {},
                  mode="orchestrate", user_id="fixture-owner", conversation_id="fixture-conversation")
    view = pc.snapshot("__ut_member_reports", "peer")
    assert len(view["gap_plan"]) == 4, "new report versions must not create new gap entities"
    updated = next(g for g in view["gap_plan"] if g["gap_id"] == own["gap_id"])
    assert updated["status"] == "closed"
    assert updated["source"] == "member_report" and updated["independent_verification"] is False
    assert all(g["gap_id"] != own["gap_id"] for g in view["open_gaps"])
    assert len(view["open_gaps"]) == 3
    assert all(g["status"] == "open" for g in view["gap_plan"] if g["gap_id"] != own["gap_id"])
    assert before["gap_plan"][0] in view["gap_plan"]
    assert view["verification_findings"] == before["verification_findings"]
    assert view["claim_ceiling"] == "UNVALIDATED"
    assert next(g for g in view["quality_context"]["gap_plan"]
                if g["gap_id"] == own["gap_id"])["status"] == "closed"
    for frontier in (view["frontier_context"], view["quality_context"]["frontier_context"]):
        assert [g for g in frontier["gap_plan"] if g.get("gap_id") == own["gap_id"]] == [updated]
        assert all(g["status"] == "open" for g in frontier["gap_plan"] if g["gap_id"] != own["gap_id"])
    reports = list(view["quality_context"]["member_reports"].values())
    assert len(reports) == 4
    own_versions = [read_report_reference(r["report_ref"]) for r in reports
                    if r["task_id"] == "maker" and r["harness"] == "codex"]
    assert opened in own_versions and closed in own_versions
    stored = pc.load("__ut_member_reports")["quality_context"]["member_reports"]
    assert all(read_report_reference(r["report_ref"]) == stored[key]["payload"]
               for key, r in view["quality_context"]["member_reports"].items())


def test_gap_without_id_uses_problem_not_list_position(isolated_context, tmp_path):
    pc = isolated_context
    member_project(pc, tmp_path)
    first = {"gap_plan": [{"problem": "first gap", "status": "open"},
                          {"problem": "second gap", "status": "open"}]}
    publish_member(pc, "maker", first)
    initial = {g["problem"]: g["gap_id"] for g in pc.snapshot("__ut_member_reports")["gap_plan"]}
    publish_member(pc, "maker", {
        "gap_plan": [{"problem": "second gap", "status": "open"},
                     {"problem": "first gap", "status": "closed"}]})
    view = pc.snapshot("__ut_member_reports")
    assert len(view["gap_plan"]) == 2
    assert {g["problem"]: g["gap_id"] for g in view["gap_plan"]} == initial
    assert {g["problem"]: g["status"] for g in view["gap_plan"]} == {
        "first gap": "closed", "second gap": "open"}
    assert [g["problem"] for g in view["open_gaps"]] == ["second gap"]


@pytest.mark.parametrize("has_gap_id", [True, False])
def test_existing_report_hash_gap_is_updated_instead_of_left_open(
        isolated_context, tmp_path, has_gap_id):
    pc = isolated_context
    member_project(pc, tmp_path)
    opened = member_report()
    if not has_gap_id:
        opened["gap_plan"][0].pop("gap_id")
    publish_member(pc, "maker", opened)
    state = pc.load("__ut_member_reports")
    report_id = next(iter(state["quality_context"]["member_reports"]))
    # The prior implementation used a report hash and invented G-001 for no-id gaps.
    for rows in (state["gap_plan"], state["open_gaps"], state["quality_context"]["gap_plan"]):
        rows[0]["gap_id"] = f"{report_id}:G1"
        rows[0]["reported_gap_id"] = "same-local-id" if has_gap_id else "G-001"
    pc._atomic_write(pc._path("__ut_member_reports"), state)
    closed = copy.deepcopy(opened)
    closed["gap_plan"][0]["status"] = "closed"
    publish_member(pc, "maker", closed)
    view = pc.snapshot("__ut_member_reports")
    assert len(view["gap_plan"]) == 1
    assert view["gap_plan"][0]["status"] == "closed"
    assert view["open_gaps"] == []
    reports = list(view["quality_context"]["member_reports"].values())
    assert len(reports) == 2
    assert {read_report_reference(r["report_ref"])["gap_plan"][0]["status"]
            for r in reports} == {"open", "closed"}


def test_snapshot_report_index_omits_raw_body_but_keeps_current_facts(isolated_context, tmp_path):
    pc = isolated_context
    member_project(pc, tmp_path)
    report = member_report()
    report["raw_notes"] = "RAW_REPORT_BODY_ONLY " * 2000
    report["implementation_brief"] = [{"decision": "retain current design reasoning"}]
    publish_member(pc, "maker", report)
    before = pc._path("__ut_member_reports").read_bytes()
    view = pc.snapshot("__ut_member_reports", "peer")
    entries = view["quality_context"]["member_reports"]
    entry = next(iter(entries.values()))
    assert "payload" not in entry
    assert "RAW_REPORT_BODY_ONLY" not in json.dumps(view)
    assert read_report_reference(entry["report_ref"]) == report
    store = importlib.import_module("artifact_store")
    assert Path(entry["report_ref"]["path"]).parent == store.ROOT / "__ut_member_reports" / "maker"
    assert store.read_text(entry["report_ref"]), "the existing artifact reader must validate the reference"
    assert view["objective"] == "original user objective"
    assert view["quality_context"]["sources"][0]["decision_reason"]
    assert view["journey_observations"] and view["gap_plan"]
    assert view["frontier_context"]["implementation_brief"] == report["implementation_brief"]
    assert pc._path("__ut_member_reports").read_bytes() == before, "snapshot must remain read-only"


def test_strict_report_projection_excludes_other_candidate_and_shared_packet(isolated_context, tmp_path):
    pc = isolated_context
    member_project(pc, tmp_path)
    for task in ("maker", "peer", "upstream"):
        report = member_report(task)
        report["raw_notes"] = f"PRIVATE_BODY_{task}"
        report["implementation_brief"] = [{"decision": f"analysis-{task}"}]
        publish_member(pc, task, report)
    packet = pc.load("__ut_member_reports")
    packet["visibility"] = {"candidate_isolation": "strict"}
    packet["candidate_tasks"] = {"maker": {}, "peer": {}}
    packet["tasks"][0]["depends_on"] = ["peer", "upstream"]
    packet["quality_context"]["context_ref"] = {"path": str(pc._path("__ut_member_reports")),
                                               "field": "quality_context"}
    view = pc.snapshot("__ut_member_reports", "maker", packet=packet)
    serialized = json.dumps(view)
    assert "PRIVATE_BODY_" not in serialized
    assert "analysis-peer" not in serialized
    assert "https://example.invalid/peer" not in serialized
    assert "tests/peer.json" not in serialized
    assert str(pc._path("__ut_member_reports")) not in serialized
    assert str(pc._path("__ut_member_reports").resolve()) not in serialized
    reports = view["quality_context"]["member_reports"]
    assert {r["task_id"] for r in reports.values()} == {"maker", "upstream"}
    assert {s["url"] for s in view["quality_context"]["sources"]} == {
        "https://example.invalid/maker", "https://example.invalid/upstream"}
    assert {g["owner_task"] for g in view["gap_plan"]} == {"maker", "upstream"}
    assert "analysis-maker" in serialized and "analysis-upstream" in serialized
    for report in reports.values():
        payload = read_report_reference(report["report_ref"])
        assert payload["raw_notes"] == "PRIVATE_BODY_" + report["task_id"]


def test_strict_legacy_report_without_safe_reference_is_explicitly_unavailable(isolated_context, tmp_path):
    pc = isolated_context
    member_project(pc, tmp_path)
    publish_member(pc, "maker", member_report())
    packet = pc.load("__ut_member_reports")
    packet["visibility"] = {"candidate_isolation": "strict"}
    packet["candidate_tasks"] = {"maker": {}, "peer": {}}
    report_id, report = next(iter(packet["quality_context"]["member_reports"].items()))
    shared = str(pc._path("__ut_member_reports"))
    legacy = {"path": shared, "json_pointer": f"/quality_context/member_reports/{report_id}/payload",
              "sha256": "0" * 64}
    report["report_ref"] = legacy
    for rows in (packet["quality_context"]["sources"], packet["journey_observations"],
                 packet["gap_plan"], packet["open_gaps"]):
        for row in rows:
            row["provenance"]["report_ref"] = legacy
    view = pc.snapshot("__ut_member_reports", "maker", packet=packet)
    projected = view["quality_context"]["member_reports"][report_id]
    assert projected["projection_status"] == "unavailable"
    assert "report_ref" not in projected and "payload" not in projected
    assert shared not in json.dumps(view)
    assert packet["quality_context"]["member_reports"][report_id]["payload"] == report["payload"]
