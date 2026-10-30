"""Current observations must reach the native send, not just a stored packet.

The installation/SDK fixtures are synthetic. Environment transport, prompt
projection, the isolated worker and the external CLI sender are product code.
"""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_research_native_send_payload import (
    configure_external_receiver,
    configure_isolated_worker,
    project,
    send_stack,
)


def observed_project(stack, material_bytes):
    packet = project(stack)
    packet["team_id"] = "__ut_observed_project"
    # Put the new facts between large earlier and later records, matching the
    # failure that a first/last text preview silently hides.
    packet["completed"] = [{"detail": "old-result-" * (material_bytes // 20)}]
    packet["verification_findings"] = [{
        "report_id": "current-preview-observation",
        "role": "artifact_preview_observer",
        "trust": "untrusted_browser_observation",
        "observations": [{
            "kind": "network_error",
            "message": "CURRENT_OBSERVED_FAILURE",
            "resource": "/api/current-action",
            "observed_snapshot_id": "b" * 64,
            "source_team_id": "__ut_previous_project",
            "source_turn": 6,
            "recorded_at": 1792676192.0,
        }],
    }]
    packet["journey_observations"] = [{
        "step": "use the original entry",
        "observation": "CURRENT_JOURNEY_FACT",
        "evidence_ref": "evidence/current.json",
    }]
    packet["open_gaps"] = [{
        "id": "current-gap",
        "finding": "CURRENT_UNRESOLVED_GAP",
        "status": "open",
    }]
    packet["artifacts"] = [{"path": "existing-result.txt",
                            "lineage": "older-artifact-" * (material_bytes // 20)}]
    return packet


def invoke_native(stack, monkeypatch, packet, path, *, task_memory=None):
    options = {
        "model_override": "fixture-model", "appid_override": "fixture-only",
        "session_id": "payload-session", "user_id": "alice",
        "project_context": packet, "task_facts": {"goal": packet["objective"]},
        "task_memory": task_memory,
        "execution_depth": "standard",
    }
    question = "Continue the existing original work using its recorded facts."
    if path == "core_inproc":
        return stack.core._run_core_inprocess(
            "__ut_terminal", question, "observed-core", timeout_s=20, **options)
    if path == "external_inproc":
        key = configure_external_receiver(stack, monkeypatch)
        return stack.core._run_external_inprocess(
            key, question, "observed-external", timeout_s=20, **options)
    configure_isolated_worker(stack, monkeypatch)
    return stack.core._run_harness_isolated(
        "__ut_terminal", question, "observed-isolate", timeout_s=20, **options)


@pytest.mark.parametrize("path", ["core_inproc", "external_inproc", "isolate"])
@pytest.mark.parametrize("material_bytes,input_budget", [
    (18000, 12000),   # Final prompt boundary only.
    (50000, 90000),   # Environment boundary, as in the real failed order.
    (50000, 12000),   # Both boundaries, without summarizing the summary again.
])
def test_current_observations_reach_actual_native_send(
        send_stack, monkeypatch, path, material_bytes, input_budget):
    stack = send_stack
    monkeypatch.setenv("LEGION_HARNESS_INPUT_BUDGET", str(input_budget))
    packet = observed_project(stack, material_bytes)
    before = copy.deepcopy(packet)
    result = invoke_native(stack, monkeypatch, packet, path)
    assert stack.capture.exists(), {
        key: result.get(key) for key in ("status", "error", "final")}
    actual = json.loads(stack.capture.read_text())
    assert result["final"] == "NATIVE_PAYLOAD_OK"
    sent = actual["task"]
    assert len(sent.encode("utf-8")) <= input_budget
    view = json.loads(sent.split("\n\nPROJECT_CONTEXT_JSON\n", 1)[1].split("\n\n", 1)[0])
    assert view.get("verification_findings") == packet["verification_findings"]
    assert view.get("journey_observations") == packet["journey_observations"]
    assert view.get("open_gaps") == packet["open_gaps"]
    assert view["user_id"] == "alice"
    assert view["conversation_id"] == "payload-session"
    assert view["workspace_root"] == str(stack.project)
    if view.get("context_ref"):
        ref = view["context_ref"]
        source = json.loads(Path(ref["path"]).read_text())
        for part in ref["json_pointer"].strip("/").split("/"):
            source = source[part]
        assert source == before, "Reference must still resolve to the full original facts."
        assert hashlib.sha256(json.dumps(source, ensure_ascii=False).encode()).hexdigest() == ref["sha256"]
    assert packet == before, "Projection must not modify the adapter's project object."


def many_observations(stack):
    packet = observed_project(stack, 50000)
    report = packet["verification_findings"][0]
    packet["verification_findings"] = []
    for version in range(24):
        row = copy.deepcopy(report)
        row["report_id"] = f"observation-{version}"
        row["source_ref"] = {"team_id": "__ut_source", "turn": version}
        row["observations"] *= 6
        row["observations"] = [copy.deepcopy(item) for item in row["observations"]]
        for index, item in enumerate(row["observations"]):
            item.update(
                message=f"FACT-{version}-{index} " + '较长的原始事实"\\\n' * 300,
                observed_snapshot_id=f"{version:064x}", source_turn=version,
            )
        packet["verification_findings"].append(row)
    return packet


def projection_context(stack, packet):
    """A real recoverable snapshot, without invoking any adapter or model."""
    encoded = json.dumps(packet, ensure_ascii=False).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    path = stack.root / "observation-snapshot.json"
    path.write_text(json.dumps({"runtime_context": {digest: packet}}, ensure_ascii=False))
    ref = {"path": str(path), "json_pointer": f"/runtime_context/{digest}",
           "sha256": digest, "bytes": len(encoded), "encoding": "json-utf8"}
    return SimpleNamespace(
        project_context=packet, context_refs={"MEYO_PROJECT_CONTEXT_JSON": ref},
        prior_attempts=[],
    ), ref


def assert_original_reference(view, original):
    ref = view["context_ref"]
    source = json.loads(Path(ref["path"]).read_text())
    for part in ref["json_pointer"].strip("/").split("/"):
        source = source[part]
    encoded = json.dumps(source, ensure_ascii=False).encode()
    assert source == original
    assert ref["bytes"] == len(encoded)
    assert ref["sha256"] == hashlib.sha256(encoded).hexdigest()


def assert_latest_observations(view, original):
    latest = view["verification_findings"][-1]
    expected = original["verification_findings"][-1]
    for field in ("report_id", "role", "trust", "source_ref"):
        assert latest[field] == expected[field]
    observed = latest["observations"][-1]
    for field in ("kind", "resource", "observed_snapshot_id", "source_team_id", "source_turn"):
        assert observed[field] == expected["observations"][-1][field]
    assert observed["message"].startswith("FACT-23-5 ")
    assert view["journey_observations"] == original["journey_observations"]
    assert view["open_gaps"] == original["open_gaps"]


@pytest.mark.parametrize("path,input_budget", [
    ("core_inproc", 12000), ("external_inproc", 12000), ("isolate", 12000),
    ("isolate", 4096),
])
def test_many_long_observations_remain_bounded_at_native_send(
        send_stack, monkeypatch, path, input_budget):
    monkeypatch.setenv("LEGION_HARNESS_INPUT_BUDGET", str(input_budget))
    packet = many_observations(send_stack)
    before = copy.deepcopy(packet)
    result = invoke_native(send_stack, monkeypatch, packet, path)
    assert result["final"] == "NATIVE_PAYLOAD_OK", {
        key: result.get(key) for key in ("status", "error")}
    sent = json.loads(send_stack.capture.read_text())["task"]
    assert len(sent.encode()) <= input_budget
    view = json.loads(sent.split("\n\nPROJECT_CONTEXT_JSON\n", 1)[1].split("\n\n", 1)[0])
    assert_latest_observations(view, before)
    summary = view["observation_projection"]
    assert summary["complete"] is False
    assert summary["fields"]["verification_findings"]["omitted_items"] > 0
    assert summary["fields"]["verification_findings"]["truncated"] is True
    assert_original_reference(view, before)
    assert packet == before


@pytest.mark.parametrize("with_preview", [True, False])
def test_reprojection_keeps_facts_and_the_original_reference(send_stack, with_preview):
    import harness_registry as registry
    packet = many_observations(send_stack)
    before = copy.deepcopy(packet)
    ctx, ref = projection_context(send_stack, packet)
    projected = registry._context_binding_view(packet, "MEYO_PROJECT_CONTEXT_JSON")
    projected["context_ref"] = ref
    env_view = json.loads(registry._context_preview_json(
        projected, projected, json.dumps(packet, ensure_ascii=False), 32000))
    if not with_preview:
        env_view.pop("preview", None)
    header = "\n\nPROJECT_CONTEXT_JSON\n"
    for limit in (6000, 3000, 2000):
        ctx.project_context = env_view
        env_before = copy.deepcopy(env_view)
        text = header + json.dumps(env_view, ensure_ascii=False)
        encoded = registry._bounded_prompt_section("project_context", text, limit, .45, ctx)
        view = json.loads(encoded[len(header):])
        assert len(encoded.encode()) <= limit
        assert_latest_observations(view, before)
        assert view["context_ref"] == ref
        # One occurrence in json_pointer and one in sha256, not a copied view.
        assert encoded.count(ref["sha256"]) == 2
        assert "received_context_ref" not in view
        assert "context_projection" not in view
        assert_original_reference(view, before)
        assert ctx.project_context == env_before
        env_view = view
    assert packet == before
    assert projected["context_ref"] == ref
    assert ctx.context_refs["MEYO_PROJECT_CONTEXT_JSON"] == ref


def test_soft_facts_do_not_become_an_impossible_minimum(send_stack, monkeypatch):
    import harness_registry as registry
    monkeypatch.setenv("LEGION_HARNESS_INPUT_BUDGET", "4096")
    packet = many_observations(send_stack)
    before = copy.deepcopy(packet)
    ctx, ref = projection_context(send_stack, packet)
    header = "\n\nPROJECT_CONTEXT_JSON\n"
    # The existing goal/protocol/identity minima fit. Adding optional facts must
    # not turn the same request into an input-budget exception.
    sections = [
        ("protocol", "P" * 1024, 10, .45),
        ("project_context", header + json.dumps(packet, ensure_ascii=False), 22, .45),
        ("question", "\n\nQuestion:\n" + "G" * 2036, 40, .5),
    ]
    sent, report = registry._budget_prompt_sections(sections, ctx)
    assert len(sent.encode()) <= 4096
    assert report["degraded"] is True
    view = json.loads(sent.split(header, 1)[1].split("\n\nQuestion:\n", 1)[0])
    assert view["context_ref"] == ref
    assert view["observation_projection"]["complete"] is False
    assert_original_reference(view, before)
    assert packet == before


def test_small_context_is_not_projected_or_modified(send_stack, monkeypatch):
    import harness_registry as registry
    monkeypatch.setenv("LEGION_HARNESS_INPUT_BUDGET", "4096")
    packet = observed_project(send_stack, 0)
    before = copy.deepcopy(packet)
    ctx, _ = projection_context(send_stack, packet)
    text = "\n\nPROJECT_CONTEXT_JSON\n" + json.dumps(packet, ensure_ascii=False)
    sent, report = registry._budget_prompt_sections([("project_context", text, 22, .45)], ctx)
    assert sent == text
    assert report["degraded"] is False
    assert packet == before


def pending_facts(item_id="item_21", command="/bin/zsh -lc 'python3.12 server.py'"):
    return {"pending_tool_results": [{
        "id": item_id, "type": "command_execution", "command": command,
        "last_event": "item.started", "terminal_event": "not_received",
    }]}


@pytest.mark.parametrize("path", ["core_inproc", "external_inproc", "isolate"])
@pytest.mark.parametrize("source", ["project_context", "task_memory"])
@pytest.mark.parametrize("material_bytes,input_budget", [
    (18000, 12000), (50000, 90000), (50000, 12000),
])
def test_pending_reaches_actual_native_send_after_both_bounds(
        send_stack, monkeypatch, path, source, material_bytes, input_budget):
    monkeypatch.setenv("LEGION_HARNESS_INPUT_BUDGET", str(input_budget))
    packet = observed_project(send_stack, 0)
    memory = None
    facts = pending_facts()
    if source == "project_context":
        packet = observed_project(send_stack, material_bytes)
        packet["attempts"] = [{
            "task_id": "original-task", "harness": "codex", "recorded_at": 1792676192.0,
            "native_checkpoint": {"kind": "codex_thread", "session_id": "original-native"},
            "native_facts": facts,
        }]
    else:
        memory = {"phase": "done", "completed_steps": ["before" * (material_bytes // 10)],
                  "native_facts": facts, "artifacts": ["after" * (material_bytes // 10)],
                  # The receiver fixture is Hermes, so do not bypass its real
                  # cross-harness checkpoint guard to test shared facts transport.
                  "native_checkpoint": {"kind": "hermes_session", "session_id": "original-native"}}
    before = copy.deepcopy(packet if source == "project_context" else memory)
    result = invoke_native(send_stack, monkeypatch, packet, path, task_memory=memory)
    assert result["final"] == "NATIVE_PAYLOAD_OK", {
        key: result.get(key) for key in ("status", "error")}
    sent = json.loads(send_stack.capture.read_text())["task"]
    assert len(sent.encode()) <= input_budget
    header = "\n\n" + source.upper() + "_JSON\n"
    view = json.loads(sent.split(header, 1)[1].split("\n\n", 1)[0])
    row = view["attempts"][-1] if source == "project_context" else view
    assert row["native_facts"]["pending_tool_results"] == facts["pending_tool_results"]
    assert row["native_checkpoint"]["session_id"] == "original-native"
    if source == "project_context":
        assert row["task_id"] == "original-task" and row["harness"] == "codex"
        assert row["recorded_at"] == 1792676192.0
    if "context_ref" in view:
        assert_original_reference(view, before)
    assert (packet if source == "project_context" else memory) == before


@pytest.mark.parametrize("with_preview", [True, False])
def test_pending_reprojection_is_bounded_scoped_and_keeps_original_ref(send_stack, with_preview):
    import harness_registry as registry
    packet = observed_project(send_stack, 50000)
    packet["attempts"] = [{
        "task_id": f"task-{index}", "harness": "codex", "recorded_at": index,
        "native_checkpoint": {"session_id": f"native-{index}"},
        "native_facts": {
            **pending_facts(command=f"command-{index} " + '长命令"\\\n' * 400),
            "pending_tool_results_omitted": 7,
            "tool_results": [{"aggregated_output": "DO_NOT_PROJECT_TOOL_BODY" * 1000}],
        },
    } for index in range(24)]
    before = copy.deepcopy(packet)
    ctx, ref = projection_context(send_stack, packet)
    projected = registry._context_binding_view(packet, "MEYO_PROJECT_CONTEXT_JSON")
    projected["context_ref"] = ref
    view = json.loads(registry._context_preview_json(projected, projected, "", 32000))
    if with_preview:
        view["preview"] = "older-preview" * 2000
    header = "\n\nPROJECT_CONTEXT_JSON\n"
    for limit in (6000, 4000, 3000):
        ctx.project_context = view
        prior = copy.deepcopy(view)
        text = registry._bounded_prompt_section(
            "project_context", header + json.dumps(view, ensure_ascii=False), limit, .45, ctx)
        view = json.loads(text[len(header):])
        assert len(text.encode()) <= limit
        latest = view["attempts"][-1]
        assert latest["task_id"] == "task-23" and latest["recorded_at"] == 23
        assert latest["native_checkpoint"]["session_id"] == "native-23"
        item = latest["native_facts"]["pending_tool_results"][0]
        assert item["id"] == "item_21" and item["terminal_event"] == "not_received"
        assert item["command"].startswith("command-23 ")
        assert latest["native_facts"]["pending_tool_results_omitted"] == 7
        assert "DO_NOT_PROJECT_TOOL_BODY" not in json.dumps(view.get("attempts"))
        assert view["observation_projection"]["fields"]["attempts"]["omitted_items"] > 0
        assert view["observation_projection"]["fields"]["attempts"]["truncated"] is True
        assert view["context_ref"] == ref and "received_context_ref" not in view
        assert_original_reference(view, before)
        assert ctx.project_context == prior
    assert packet == before


def test_pending_projection_does_not_reuse_old_item_ids_or_empty_current_facts(send_stack):
    import harness_registry as registry
    old = {"task_id": "old", "harness": "codex", "recorded_at": 1, "native_facts": pending_facts()}
    current = {"task_id": "current", "harness": "codex", "recorded_at": 2,
               "native_facts": pending_facts(command="current invocation")}
    packet = {"attempts": [old, current]}
    rows = registry._context_binding_view(packet, "MEYO_PROJECT_CONTEXT_JSON")["attempts"]
    assert [(row["task_id"], row["native_facts"]["pending_tool_results"][0]["command"])
            for row in rows] == [
                ("old", "/bin/zsh -lc 'python3.12 server.py'"), ("current", "current invocation")]
    memory = {"native_facts": {"pending_tool_results": []}, "previous": old}
    view = registry._context_binding_view(memory, "MEYO_TASK_MEMORY_JSON")
    assert view["native_facts"]["pending_tool_results"] == []
    assert "previous" not in view
    assert registry._context_binding_view({}, "MEYO_TASK_MEMORY_JSON") == {}


def test_pending_is_soft_under_tight_task_memory_budget(send_stack, monkeypatch):
    import harness_registry as registry
    monkeypatch.setenv("LEGION_HARNESS_INPUT_BUDGET", "4096")
    packet = {"native_facts": pending_facts(command="long-command " * 1000)}
    ctx, ref = projection_context(send_stack, packet)
    ctx.task_memory = packet
    ctx.context_refs = {"MEYO_TASK_MEMORY_JSON": ref}
    header = "\n\nTASK_MEMORY_JSON\n"
    sections = [
        ("protocol", "P" * 1024, 10, .45),
        ("task_memory", header + json.dumps(packet), 22, .45),
        ("question", "\n\nQuestion:\n" + "G" * 2036, 40, .5),
    ]
    sent, report = registry._budget_prompt_sections(sections, ctx)
    view = json.loads(sent.split(header, 1)[1].split("\n\nQuestion:\n", 1)[0])
    assert len(sent.encode()) <= 4096 and report["degraded"] is True
    assert view["context_ref"] == ref
    assert (view.get("native_facts", {}).get("pending_tool_results")
            or view.get("observation_projection", {}).get("complete") is False)
    assert_original_reference(view, packet)
