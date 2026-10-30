"""A keyword hit cannot turn a past domain recipe into this task's instructions."""
import importlib
import json
from pathlib import Path

import pytest


QUERY = "quasar audit zx731"
FRESH_FACT = "FRESH_OBSERVATION_zx731_not_seed"
SOURCE = "https://offline-fixture.invalid/zx731"


@pytest.fixture(autouse=True)
def knowledge(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MEYO_LEGION_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LEGION_HOME_DIR", str(tmp_path / "legion"))
    monkeypatch.setenv("LEGION_DELIVERY_KB_DIR", str(tmp_path / "knowledge"))
    monkeypatch.setenv("LEGION_PROJECT_CONTEXT_DIR", str(tmp_path / "packets"))
    return importlib.import_module("delivery_knowledge")


def test_negated_domain_word_does_not_inject_a_recipe(knowledge):
    query = "制作学龄前英语启蒙，不要只用卡通、游戏化几个词敷衍，需要真学习体验。"
    context = knowledge.match_context(query, {})
    assert "core_loop必须锚它" not in context
    assert "Ghostrunner" not in context
    assert "配方锚点:" not in context
    assert "未选择" in context
    assert "load_entry" in context


def test_no_keyword_based_quality_authority_even_for_game_requests(knowledge):
    context = knowledge.match_context("做一个游戏", {})
    assert "禁止三车道" not in context
    assert "桂冠参照:" not in context
    assert "未判定适用" in context


def test_explicit_selection_retains_original_reference_and_provenance(knowledge):
    context = knowledge.match_context(
        "evaluate this past approach",
        {"delivery_knowledge_categories": ["game_3d_parkour"]})
    assert "Ghostrunner" in context
    assert "https://ghostrunner.com/" in context
    assert "本单显式选择" in context
    assert "不是本题" in context
    assert "2026-09-08" in context


def test_unknown_explicit_choice_does_not_fall_back_to_keyword_recipe(knowledge):
    context = knowledge.match_context(
        "游戏", {"delivery_knowledge_categories": ["missing-category"]})
    assert "Ghostrunner" not in context and "配方锚点:" not in context


def _capture(knowledge, *, question=QUERY, concept=FRESH_FACT):
    result = knowledge.capture_from_packet(
        question, {"experience_intent": {"ambition": "world_class"}},
        {"concept_anchors": [
            {"dimension": "source_observation", "concept": concept,
             "is_crown": False, "reference": "offline source", "evidence_url": SOURCE}],
         "effect_downgrades": [{"downgrade": "OFFLINE_FAILURE_zx731"}],
         "gap_plan": [{"dimension": "OFFLINE_GAP_zx731", "status": "open"}]},
        [], team_id="offline-team-zx731", user_id="alice")
    assert result["ok"], result
    return result["category"]


@pytest.mark.parametrize("selection_location", ["plan", "project_context"])
def test_persisted_capture_reaches_explicitly_selected_context(
        knowledge, selection_location):
    category = _capture(knowledge)
    stored = json.loads(knowledge._path(category, user_id="alice").read_text())
    assert stored["captures"][-1]["anchors"][0]["concept"] == FRESH_FACT
    before = knowledge._path(category, user_id="alice").read_bytes()
    selection = {"delivery_knowledge_categories": [category]}
    plan = selection if selection_location == "plan" else {"project_context": selection}
    context = knowledge.match_context(QUERY, plan, user_id="alice")
    observed = {name: value in context for name, value in {
        "fresh_fact": FRESH_FACT, "source": SOURCE, "team": "offline-team-zx731",
        "question": QUERY, "review_status": "pending_review",
        "failure": "OFFLINE_FAILURE_zx731", "open_gap": "OFFLINE_GAP_zx731",
    }.items()}
    assert all(observed.values()), observed
    assert str(stored["captures"][-1]["captured_at"]) in context
    assert knowledge._path(category, user_id="alice").read_bytes() == before


def test_unselected_capture_stays_an_index_not_automatic_instructions(knowledge):
    category = _capture(knowledge)
    for plan in ({}, {"delivery_knowledge_categories": []},
                 {"delivery_knowledge_categories": ["unknown"]}):
        context = knowledge.match_context(QUERY, plan, user_id="alice")
        assert FRESH_FACT not in context and SOURCE not in context
    assert category in knowledge.match_context(QUERY, {}, user_id="alice")


def test_pending_capture_does_not_inherit_verified_seed_status(knowledge, monkeypatch):
    seed = {"category": "offline_seed", "label": "Offline historical example",
            "triggers": ["seedtoken"], "status": "user_verified",
            "formula_anchors": [{"dimension": "old", "concept": "ORIGINAL_SEED_zx731"}],
            "provenance": "offline-user-confirmation"}
    monkeypatch.setattr(knowledge, "_SEED_ENTRIES", {"offline_seed": seed})
    category = _capture(knowledge, question="seedtoken audit")
    context = knowledge.match_context(
        QUERY, {"delivery_knowledge_categories": [category]}, user_id="alice")
    observed = {"old_fact": "ORIGINAL_SEED_zx731" in context,
                "fresh_fact": FRESH_FACT in context,
                "independent_status": "pending_review" in context,
                "original_provenance": "offline-user-confirmation" in context}
    assert all(observed.values()), observed
    entry = knowledge.load_entry(category, user_id="alice")
    assert entry["status"] == "user_verified"
    assert entry["captures"][-1]["status"] == "pending_review"
    assert entry["formula_anchors"] == seed["formula_anchors"]


def test_recent_capture_preview_keeps_full_history_retrievable(knowledge):
    category = _capture(knowledge, concept="OLDER_CAPTURE_zx731")
    for index in range(8):
        _capture(knowledge, concept=f"RECENT_CAPTURE_zx731_{index}")
    context = knowledge.match_context(
        QUERY, {"delivery_knowledge_categories": [category]}, user_id="alice")
    observed = {"latest": "RECENT_CAPTURE_zx731_7" in context,
                "readback": "load_entry" in context and category in context}
    assert all(observed.values()), observed
    assert "OLDER_CAPTURE_zx731" not in context
    entry = knowledge.load_entry(category, user_id="alice")
    assert len(entry["captures"]) == 9
    assert entry["captures"][0]["anchors"][0]["concept"] == "OLDER_CAPTURE_zx731"


def test_invalid_capture_rows_do_not_hide_valid_saved_observation(knowledge):
    category = _capture(knowledge)
    entry = knowledge.load_entry(category, user_id="alice")
    entry["captures"] = [None, "bad historical row", *entry["captures"], 7]
    knowledge._atomic_write(knowledge._path(category, user_id="alice"), entry)
    context = knowledge.match_context(
        QUERY, {"delivery_knowledge_categories": [category]}, user_id="alice")
    observed = {"fact": FRESH_FACT in context, "source": SOURCE in context}
    assert all(observed.values()), observed


@pytest.mark.parametrize("depth", ["quick", "standard", "deep"])
def test_saved_capture_reaches_actual_native_send(knowledge, monkeypatch, tmp_path, depth):
    # Reuse the existing real core/prompt/adapter fixture. Only SDK and process
    # I/O are replaced; restore the knowledge renderer that fixture suppresses.
    from test_research_native_send_payload import prepare_native_payload_stack
    renderer = knowledge.match_context
    category = _capture(knowledge)
    stack = prepare_native_payload_stack(tmp_path / "native-fixture", monkeypatch)
    monkeypatch.setattr(knowledge, "match_context", renderer)
    capture = tmp_path / "native-send.json"
    monkeypatch.setenv("C_NATIVE_PAYLOAD_CAPTURE", str(capture))
    plan = {"project_context": {
        "delivery_knowledge_categories": [category],
        "workspace_root": str(stack.project), "user_id": "alice"}}
    # This is the same canonical packet round-trip used before dispatch's
    # _build_prelude, not an ad-hoc copy of the rendering logic.
    import project_context
    plan["project_context"] = project_context.initialize(
        "knowledge-next-task", QUERY, plan, mode="single",
        user_id="alice", conversation_id="knowledge-next-conversation")
    task = QUERY + stack.core._build_prelude(QUERY, plan, user_id="alice")
    result = stack.core._run_core_inprocess(
        "__ut_terminal", task, "knowledge-send", model_override="fixture-model",
        appid_override="fixture-only", user_id="alice", session_id="knowledge-session",
        project_context=plan["project_context"], timeout_s=20, execution_depth=depth)
    assert capture.is_file(), {"status": result.get("status"), "error": result.get("error")}
    sent = json.loads(capture.read_text())["task"]
    observed = {"fresh_fact": FRESH_FACT in sent, "source": SOURCE in sent,
                "status": "pending_review" in sent, "origin": "offline-team-zx731" in sent}
    assert all(observed.values()), observed
    assert result["final"] == "NATIVE_PAYLOAD_OK"


@pytest.mark.parametrize("depth", ["quick", "standard", "deep"])
def test_disabled_auto_builds_shared_context_without_starting_engine(
        knowledge, monkeypatch, tmp_path, depth):
    from test_research_native_send_payload import prepare_native_payload_stack
    renderer = knowledge.match_context
    category = _capture(knowledge)
    stack = prepare_native_payload_stack(tmp_path / "auto-fixture", monkeypatch)
    monkeypatch.setattr(knowledge, "match_context", renderer)
    registry = stack.registry
    # Inspect the actual core layout, not the SDK fixture's relocated __file__.
    source = Path(registry._auto_adapter.__code__.co_filename).resolve()
    monkeypatch.setattr(registry, "__file__", str(source))
    assert not (source.parents[2] / "seed-users-system/qa_system/agent/run_auto_thin.py").exists()
    sent = []
    shared_prompt = registry._legion_prompt

    def observe(*args, **kwargs):
        prompt = shared_prompt(*args, **kwargs)
        sent.append(prompt)
        return prompt

    def forbidden_engine(*args, **kwargs):
        pytest.fail("Disabled Auto must not start an engine")

    monkeypatch.setattr(registry, "_legion_prompt", observe)
    monkeypatch.setattr(registry, "_run_task", forbidden_engine)
    plan = {"delivery_knowledge_categories": [category]}
    task = QUERY + stack.core._build_prelude(QUERY, plan, user_id="alice")
    ctx = registry.HarnessCtx(
        execution_depth=depth, user_id="alice", workdir=str(stack.project),
        stable_context=stack.core._stable_prelude(depth))
    result = registry._auto_adapter("knowledge-auto", task, ctx=ctx)
    assert result["status"] == "unavailable" and result["final"] == ""
    assert len(sent) == 1
    observed = {"fact": FRESH_FACT in sent[0], "source": SOURCE in sent[0],
                "review_status": "pending_review" in sent[0]}
    assert all(observed.values()), observed
