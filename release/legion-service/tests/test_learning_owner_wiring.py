"""Owned learning receipts and the actual string-brief consumers; no models.

The capture helper executes the existing dispatch caller's AST unchanged. The
compatibility reads let the c8 baseline reproduce the original leak rather than
fail merely because the repaired API adds an explicit user_id argument.
"""
from __future__ import annotations

import ast
import copy
import importlib
import inspect
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


SERVICE = Path(__file__).resolve().parents[1]
ALICE = "__ut_learning_alice"
BOB = "__ut_learning_bob"
QUESTION = "PRIVATE_LEARNING_zq812 inspect exact source observations"
FACT = "PRIVATE_OBSERVATION_zq812_not_a_public_seed"
PREF = "APPLIED_PREFERENCE_zq815"


@pytest.fixture(autouse=True)
def case(monkeypatch, tmp_path):
    for name in tuple(os.environ):
        if name.startswith(("MEYO_", "LEGION_", "AIGC_", "OPENAI_", "ANTHROPIC_")):
            monkeypatch.delenv(name)
    for name, leaf in (
        ("HOME", "home"), ("MEYO_LEGION_DIR", "data"),
        ("LEGION_HOME_DIR", "legion"), ("LEGION_DELIVERY_KB_DIR", "knowledge"),
        ("LEGION_PROJECT_CONTEXT_DIR", "projects"), ("LEGION_SESSIONS_DIR", "sessions"),
        ("LEGION_USER_CONFIG_DIR", "users"), ("LEGION_TASK_MEMORY_DIR", "task-memory"),
    ):
        (tmp_path / leaf).mkdir()
        monkeypatch.setenv(name, str(tmp_path / leaf))
    monkeypatch.setenv("LEGION_MEMORY_FILE", str(tmp_path / "data/experience.jsonl"))
    monkeypatch.setenv("LEGION_GATEWAY_RETRIES", "0")
    monkeypatch.syspath_prepend(str(SERVICE))
    modules = {name: importlib.import_module(name) for name in (
        "delivery_knowledge", "legion_memory", "legion_core", "legion_tool", "evolve_config")}
    memory = modules["legion_memory"]
    monkeypatch.setattr(memory, "_MEM_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(memory, "_MEM_FILE", str(tmp_path / "data/experience.jsonl"))
    # Do not let this test's default-config mutation poison later test cases.
    evolve = modules["evolve_config"]
    monkeypatch.setattr(evolve, "_DEFAULT_CONFIG", copy.deepcopy(evolve._DEFAULT_CONFIG))
    monkeypatch.setattr(modules["legion_core"], "_media_prelude", lambda *a, **kw: "")
    return SimpleNamespace(
        root=tmp_path, mp=monkeypatch, knowledge=modules["delivery_knowledge"],
        memory=memory, core=modules["legion_core"], tool=modules["legion_tool"],
        evolve=evolve)


def owned_read(function, *args, owner):
    if "user_id" in inspect.signature(function).parameters:
        return function(*args, user_id=owner)
    return function(*args)  # c8 red: exercise the original unscoped read.


def capture(case, owner=ALICE, question=QUESTION, fact=FACT):
    tree = ast.parse((SERVICE / "legion_core.py").read_text())
    node = next(node for node in ast.walk(tree) if isinstance(node, ast.Try)
                and any(isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "capture_from_packet"
                        for statement in node.body for call in ast.walk(statement)))
    packet = {"user_id": BOB, "conversation_id": "untrusted-packet-conversation",
              "concept_anchors": [{"dimension": "source_observation", "concept": fact,
                                   "evidence_url": "https://offline.invalid/owned-observation"}]}
    plan = {"experience_intent": {"ambition": "world_class"}, "user_id": BOB,
            "actor": BOB, "project_context": {"user_id": BOB}}
    outputs = []
    real_capture = case.knowledge.capture_from_packet

    def observe(*args, **kwargs):
        result = real_capture(*args, **kwargs)
        outputs.append(result)
        return result

    with pytest.MonkeyPatch.context() as patches:
        patches.setattr(case.knowledge, "capture_from_packet", observe)
        exec(compile(ast.Module(body=[node], type_ignores=[]),
                     str(SERVICE / "legion_core.py"), "exec"), {
                         "question": question, "plan": plan, "results": [],
                         "_project_context": SimpleNamespace(load=lambda _: packet),
                         "team_id": "original-owned-team", "user_id": owner})
    assert len(outputs) == 1
    return outputs[0]


def selected(category):
    return {"delivery_knowledge_categories": [category],
            "user_id": ALICE, "actor": ALICE, "project_context": {"user_id": ALICE}}


def test_capture_records_real_caller_not_plan_or_packet_owner(case):
    result = capture(case)
    assert result["ok"], result
    stored = owned_read(case.knowledge.load_entry, result["category"], owner=ALICE)
    row = stored["captures"][-1]
    assert row.get("user_id") == ALICE, row
    assert row["team_id"] == "original-owned-team"
    assert row["status"] == "pending_review"


def test_other_user_cannot_read_capture_or_discover_its_private_category(case):
    result = capture(case)
    assert result["ok"], result
    category = result["category"]
    bob_entry = owned_read(case.knowledge.load_entry, category, owner=BOB)
    bob_kb = owned_read(case.knowledge.load_kb, owner=BOB)
    bob_detected = owned_read(case.knowledge.detect_category, QUESTION, owner=BOB)
    bob_selected = case.core._build_prelude(QUESTION, selected(category), user_id=BOB)
    bob_index = case.core._build_prelude("A different query", {}, user_id=BOB)
    checks = {"entry_hidden": bob_entry is None, "index_hidden": category not in bob_kb,
              "trigger_hidden": bob_detected != category,
              "selected_hidden": FACT not in bob_selected and QUESTION not in bob_selected,
              "default_index_hidden": category not in bob_index}
    assert all(checks.values()), checks
    own = case.core._build_prelude(QUESTION, selected(category), user_id=ALICE)
    assert FACT in own and "pending_review" in own and "original-owned-team" in own


def test_payload_identity_cannot_select_private_learning_without_bound_owner(case):
    result = capture(case)
    context = case.core._build_prelude(QUESTION, selected(result["category"]), user_id="")
    assert FACT not in context
    # No caller identity means no owner can be invented from the plan/packet.
    anonymous = capture(case, owner="")
    assert anonymous["ok"] is False, anonymous
    assert anonymous["reason"] == "owner_required"


def test_unknown_owner_legacy_category_is_quarantined_without_rewriting_it(case):
    knowledge = case.knowledge
    category = "auto_LEGACY_PRIVATE_LABEL"
    legacy = {"category": category, "label": "LEGACY_PRIVATE_LABEL",
              "triggers": ["LEGACY_PRIVATE_TRIGGER"], "status": "user_verified",
              "formula_anchors": [{"dimension": "legacy", "concept": FACT}],
              "captures": [{"question": QUESTION, "anchors": [], "status": "pending_review"}]}
    path = knowledge._path(category)
    knowledge._atomic_write(path, legacy)
    before = path.read_bytes()
    for owner in (ALICE, BOB, "", "default"):
        assert owned_read(knowledge.load_entry, category, owner=owner) is None
        assert category not in owned_read(knowledge.load_kb, owner=owner)
        assert "LEGACY_PRIVATE_LABEL" not in case.core._build_prelude(
            QUESTION, {}, user_id=owner)
    assert path.read_bytes() == before


def test_public_seed_remains_public_but_legacy_capture_does_not_inherit_that_scope(case):
    knowledge = case.knowledge
    category, seed = next(iter(knowledge._SEED_ENTRIES.items()))
    legacy = copy.deepcopy(seed)
    legacy["captures"] = [{"question": QUESTION, "anchors": [{"concept": FACT}],
                           "status": "pending_review"}]
    legacy["formula_anchors"].append({"dimension": "private-overlay", "concept": FACT})
    knowledge._atomic_write(knowledge._path(category), legacy)
    for owner in (ALICE, BOB, "", "default"):
        entry = owned_read(knowledge.load_entry, category, owner=owner)
        assert entry["formula_anchors"] == seed["formula_anchors"]
        assert not entry.get("captures")
        assert entry["status"] == seed["status"]
        assert FACT not in case.core._build_prelude(QUESTION, selected(category), user_id=owner)


def test_two_owners_can_append_same_category_without_erasing_or_sharing_captures(case):
    for owner, fact in ((ALICE, "ALICE_CAPTURE_1"), (BOB, "BOB_CAPTURE_1"),
                        (ALICE, "ALICE_CAPTURE_2")):
        result = capture(case, owner=owner, fact=fact)
        assert result["ok"], result
    category = result["category"]
    a = owned_read(case.knowledge.load_entry, category, owner=ALICE)
    b = owned_read(case.knowledge.load_entry, category, owner=BOB)
    assert [row["anchors"][0]["concept"] for row in a["captures"]] == [
        "ALICE_CAPTURE_1", "ALICE_CAPTURE_2"]
    assert [row["anchors"][0]["concept"] for row in b["captures"]] == ["BOB_CAPTURE_1"]


def test_owned_top_level_observations_remain_readable_but_do_not_edit_public_seed(case):
    knowledge = case.knowledge
    category, seed = next(iter(knowledge._SEED_ENTRIES.items()))
    private = copy.deepcopy(seed)
    private.update(user_id=ALICE, label="ALICE_PRIVATE_LABEL")
    private["formula_anchors"].append({"dimension": "owned-observation", "concept": FACT})
    knowledge._atomic_write(knowledge._path(category, user_id=ALICE), private)
    own = knowledge.load_entry(category, user_id=ALICE)
    assert own["label"] == "ALICE_PRIVATE_LABEL"
    assert FACT in json.dumps(own)
    for owner in (BOB, ""):
        public = knowledge.load_entry(category, user_id=owner)
        assert public["formula_anchors"] == seed["formula_anchors"]
        assert FACT not in json.dumps(public)


def test_owner_storage_and_retention_do_not_alias_or_trim_another_user(case):
    case.mp.setattr(case.knowledge, "_MAX_ITEMS", 3)
    owners = ("__ut_owner/a", "__ut_ownera")
    first = capture(case, owner=owners[0], fact="OTHER_OWNER_KEEP")
    for i in range(4):
        result = capture(case, owner=owners[1], fact=f"RECENT_{i}")
        assert result["ok"]
    one = case.knowledge.load_entry(first["category"], user_id=owners[0])
    two = case.knowledge.load_entry(result["category"], user_id=owners[1])
    assert [r["anchors"][0]["concept"] for r in one["captures"]] == ["OTHER_OWNER_KEEP"]
    assert [r["anchors"][0]["concept"] for r in two["captures"]] == [
        "RECENT_1", "RECENT_2", "RECENT_3"]


def add_memory(case, owner=ALICE, answer="OWNED_ANSWER"):
    result = case.memory.add(QUESTION, answer, source="__ut_same_member", user_id=owner)
    assert result["ok"], result
    return result["stored"]


def receipt_key(record):
    # c8 red reproduces the published Q-based command. The fixed recall exposes
    # an exact stored identity, and this fallback is no longer exercised.
    return record.get("entry_id") or record["q"]


def cli(case, capsys, *args):
    rc = case.tool._main(["adopted", *args])
    return rc, json.loads(capsys.readouterr().out)


def test_cli_adopts_one_exact_entry_not_same_question_or_another_owner(case, capsys):
    first = add_memory(case, answer="ALICE_FIRST")
    add_memory(case, answer="ALICE_SECOND")
    add_memory(case, owner=BOB, answer="BOB_PRIVATE")
    case.mp.setenv("MEYO_LEGION_UID", ALICE)
    case.mp.setenv("MEYO_USER_ID", BOB)  # Existing trusted binding has precedence.
    rc, result = cli(case, capsys, receipt_key(first))
    assert rc == 0 and result["marked"] == 1, result
    assert [row["adopted"] for row in case.memory._load()] == [1, 0, 0]


@pytest.mark.parametrize("argument", ["old_question_and_source", "other_owner", "no_owner"])
def test_adoption_rejects_ambiguous_or_unowned_receipts(case, capsys, argument):
    own = add_memory(case)
    other = add_memory(case, owner=BOB)
    case.mp.setenv("MEYO_USER_ID", ALICE)
    if argument == "old_question_and_source":
        args = (QUESTION, "__ut_same_member")
    elif argument == "other_owner":
        args = (receipt_key(other),)
    else:
        case.mp.delenv("MEYO_USER_ID")
        args = (receipt_key(own),)
    before = Path(case.memory._MEM_FILE).read_bytes()
    rc, result = cli(case, capsys, *args)
    assert rc != 0 and not result["ok"], result
    assert Path(case.memory._MEM_FILE).read_bytes() == before


def test_recall_exposes_distinct_durable_ids_without_claiming_adoption(case):
    first = add_memory(case, answer="ALICE_FIRST")
    second = add_memory(case, answer="ALICE_SECOND")
    one = case.memory.search(QUESTION, top=10, user_id=ALICE)
    ids = [row.get("entry_id") for row in one["experiences"]]
    assert all(ids) and len(set(ids)) == 2, one
    assert set(ids) == {first["entry_id"], second["entry_id"]}
    two = case.memory.search(QUESTION, top=10, user_id=ALICE)
    assert {row["entry_id"] for row in two["experiences"]} == set(ids)
    assert all(row["adopted"] == 0 for row in case.memory._load())
    context = case.core._recall_experience(QUESTION, user_id=ALICE)
    assert all(entry_id in context for entry_id in ids)
    assert "<那条经验的Q原文>" not in context


def test_legacy_recall_mints_distinct_ids_then_adopts_only_one(case, capsys):
    # Identical legacy entries are deliberately not collapsible by a q/source hash.
    record = {"q": QUESTION, "a": "LEGACY_ANSWER", "source": "__ut_same_member",
              "user_id": ALICE, "mtype": "long", "ts": 1, "recalled": 0, "adopted": 0}
    Path(case.memory._MEM_FILE).write_text(
        "\n".join(json.dumps(record) for _ in range(2)) + "\n")
    found = case.memory.search(QUESTION, top=10, user_id=ALICE)
    ids = [row.get("entry_id") for row in found["experiences"]]
    assert all(ids) and len(set(ids)) == 2, found
    assert {row["entry_id"] for row in case.memory._load()} == set(ids)
    case.mp.setenv("MEYO_USER_ID", ALICE)
    rc, result = cli(case, capsys, ids[0])
    assert rc == 0 and result["marked"] == 1
    assert [row["adopted"] for row in case.memory._load()] == [1, 0]


def test_failed_adoption_persistence_does_not_report_success(case, capsys):
    record = add_memory(case)
    before = Path(case.memory._MEM_FILE).read_bytes()
    case.mp.setenv("MEYO_USER_ID", ALICE)

    def fail_replace(*args, **kwargs):
        raise OSError("offline fixture: write-back unavailable")

    case.mp.setattr(os, "replace", fail_replace)
    rc, result = cli(case, capsys, receipt_key(record))
    assert rc != 0 and not result["ok"], result
    assert result.get("marked", 0) == 0
    assert Path(case.memory._MEM_FILE).read_bytes() == before


def test_existing_public_memory_visibility_is_not_removed(case):
    add_memory(case, owner=BOB, answer="BOB_PRIVATE")
    shared = case.memory.add(QUESTION, "EXPLICIT_SHARED_METHOD", mtype="procedural")
    assert shared["ok"]
    results = case.memory.search(QUESTION, top=10, user_id=ALICE)
    assert [row["a"] for row in results["experiences"]] == ["EXPLICIT_SHARED_METHOD"]


def test_failed_legacy_id_persistence_returns_facts_but_no_usable_receipt(case):
    record = {"q": QUESTION, "a": "LEGACY_ANSWER", "user_id": ALICE}
    path = Path(case.memory._MEM_FILE)
    path.write_text(json.dumps(record) + "\n")
    before = path.read_bytes()
    case.mp.setattr(case.memory, "_rewrite", lambda *_: False)
    found = case.memory.search(QUESTION, user_id=ALICE)
    assert found["experiences"][0]["a"] == "LEGACY_ANSWER"
    assert not found["usage_persisted"]
    assert not found["experiences"][0]["entry_id"]
    assert not found["experiences"][0]["adoptable"]
    assert found["experiences"][0]["used"] == {"recalled": 0, "adopted": 0}
    assert path.read_bytes() == before


def test_concurrent_recall_keeps_legacy_ids_and_exact_adoption_counters(case):
    from concurrent.futures import ThreadPoolExecutor
    legacy = {"q": QUESTION, "a": "LEGACY_ANSWER", "user_id": ALICE, "ts": 1}
    Path(case.memory._MEM_FILE).write_text(
        "\n".join(json.dumps(legacy) for _ in range(2)) + "\n")
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(
            lambda _: case.memory.search(QUESTION, top=10, user_id=ALICE), range(6)))
    identities = [{row["entry_id"] for row in found["experiences"]} for found in results]
    assert len(identities[0]) == 2 and all(ids == identities[0] for ids in identities)
    first = results[0]["experiences"][0]["entry_id"]
    with ThreadPoolExecutor(max_workers=3) as pool:
        receipts = list(pool.map(
            lambda _: case.memory.mark_adopted(first, user_id=ALICE), range(6)))
    assert all(row["ok"] and row["marked"] == 1 for row in receipts)
    assert [row.get("adopted", 0) for row in case.memory._load()] == [6, 0]


def prepare_pref(case, owner=ALICE, marker=PREF, apply=True):
    config = case.evolve.save_config(owner, {
        "enabled": True, "targets": {"workflow_snippets": True}, "auto_apply": False})
    assert config["ok"]
    evolution_id = "offline-manually-reviewed"
    path = Path(case.evolve._evolution_dir(owner)) / f"{evolution_id}.json"
    path.write_text(json.dumps({
        "evolution_id": evolution_id,
        "after": {"workflow_snippets": {"recent_task_types": [marker]}}}))
    if apply:
        assert case.evolve.apply_evolution(owner, evolution_id)["ok"]
    return evolution_id


def build_brief(case, owner):
    return owned_read(case.core._build_runtime_brief, QUESTION,
                      {"tasks": {"member": "inspect"}, "user_id": BOB,
                       "project_context": {"user_id": BOB}}, owner=owner)


def test_manual_pref_reaches_real_string_brief_only_for_bound_owner(case):
    evolution_id = prepare_pref(case, apply=False)
    assert PREF not in build_brief(case, ALICE)  # A proposal is not an application.
    assert case.evolve.apply_evolution(ALICE, evolution_id)["ok"]
    brief = build_brief(case, ALICE)
    assert isinstance(brief, str) and PREF in brief and evolution_id in brief
    assert "可参考但不强制" in brief
    assert PREF not in build_brief(case, BOB)
    assert PREF not in build_brief(case, "")
    assert case.evolve.load_config(ALICE)["auto_apply"] is False


def test_pref_application_preserves_string_and_dictionary_contracts(case):
    prepare_pref(case)
    text = "ORIGINAL_RUNTIME_FACTS"
    rendered = case.evolve.apply_active_pref(text, ALICE)
    assert isinstance(rendered, str) and rendered.startswith(text) and PREF in rendered
    structured = {"original": {"fact": True}}
    original = copy.deepcopy(structured)
    output = case.evolve.apply_active_pref(structured, ALICE)
    assert PREF in json.dumps(output) and structured == original
    assert case.evolve.apply_active_pref(text, BOB) == text
    assert case.evolve.apply_active_pref(text, "") == text


def test_disabled_pref_and_rollback_never_reenable_or_inject(case):
    prepare_pref(case)
    case.evolve.save_config(ALICE, {"enabled": False})
    assert PREF not in build_brief(case, ALICE)
    assert case.evolve.load_config(ALICE)["enabled"] is False
    case.evolve.save_config(ALICE, {"enabled": True})
    case.evolve.rollback(ALICE)
    assert PREF not in build_brief(case, ALICE)


def test_one_users_target_settings_do_not_mutate_other_users_defaults(case):
    case.evolve.save_config(ALICE, {"targets": {"workflow_snippets": True}})
    assert case.evolve.load_config(BOB)["targets"]["workflow_snippets"] is False
    assert case.evolve._DEFAULT_CONFIG["targets"]["workflow_snippets"] is False


@pytest.mark.parametrize("first,second", [
    ("__ut_independent_owner/a", "__ut_independent_ownera"),
    ("__ut_" + "x" * 80 + "first", "__ut_" + "x" * 80 + "second"),
    ("__ut_owner_case", "__ut_OWNER_CASE"),
    ("__ut_owner_caf\u00e9", "__ut_owner_cafe\u0301"),
], ids=["removed-character", "truncated-owner", "case", "unicode-normalization"])
def test_full_owner_namespaces_do_not_share_config_or_evolutions(
        case, monkeypatch, first, second):
    identity = importlib.import_module("personal_identity")
    # Keep the real resolver; only the operator-store IO is synthetic.
    monkeypatch.setattr(identity, "_operator_records", lambda: {
        "offline-owner-one": {"user": first}, "offline-owner-two": {"user": second}})
    assert identity.resolve_token("offline-owner-one") == first
    assert identity.resolve_token("offline-owner-two") == second
    evolution_id = prepare_pref(case, owner=first, marker="FIRST_OWNER_PRIVATE_PREF")
    config_path = Path(case.evolve._config_path(first))
    evolution_path = Path(case.evolve._evolution_dir(first)) / f"{evolution_id}.json"
    before = (config_path.read_bytes(), evolution_path.read_bytes())

    second_brief = build_brief(case, second)
    assert "FIRST_OWNER_PRIVATE_PREF" not in second_brief
    assert case.evolve.load_config(second)["enabled"] is False
    assert case.evolve._config_path(first) != case.evolve._config_path(second)
    assert case.evolve._evolution_dir(first) != case.evolve._evolution_dir(second)
    assert case.evolve.list_evolutions(second)["evolutions"] == []
    assert not case.evolve.get_evolution(second, evolution_id)["ok"]
    assert not case.evolve.apply_evolution(second, evolution_id)["ok"]
    assert (config_path.read_bytes(), evolution_path.read_bytes()) == before

    # The same receipt ID is valid in a different owner's independent directory.
    prepare_pref(case, owner=second, marker="SECOND_OWNER_PRIVATE_PREF")
    assert "FIRST_OWNER_PRIVATE_PREF" in build_brief(case, first)
    assert "SECOND_OWNER_PRIVATE_PREF" not in build_brief(case, first)
    assert "SECOND_OWNER_PRIVATE_PREF" in build_brief(case, second)
    case.evolve.rollback(second)
    assert "SECOND_OWNER_PRIVATE_PREF" not in build_brief(case, second)
    assert (config_path.read_bytes(), evolution_path.read_bytes()) == before


@pytest.mark.parametrize("owner", [
    ALICE, "__ut_legacy_owner/a", "__ut_legacy_ownera",
    "__ut_" + "x" * 80 + "first", "__ut_" + "x" * 80 + "second",
], ids=["plain-owner", "removed-character", "alias", "long-owner", "truncated-alias"])
def test_legacy_lossy_owner_records_are_not_claimed_or_migrated(case, owner):
    # The old writer did not persist a full owner. Even a plain filename may
    # have been written by another UID after filtering/truncation.
    safe = "".join(c for c in owner if c.isalnum() or c in "_.-")[:80] or "default"
    root = Path(case.evolve._root())
    config_path = root / "evolve" / f"{safe}.json"
    evolution_path = root / "evolutions" / safe / "legacy-receipt.json"
    for path in (config_path, evolution_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    old_pref = {"workflow_snippets": {"recent_task_types": ["LEGACY_AMBIGUOUS_PREF"]}}
    config_path.write_text(json.dumps({
        "enabled": True, "targets": {"workflow_snippets": True}, "auto_apply": True,
        "active_pref": old_pref, "active_evolution_id": "legacy-receipt"}))
    evolution_path.write_text(json.dumps({
        "evolution_id": "legacy-receipt", "after": old_pref}))
    before = (config_path.read_bytes(), evolution_path.read_bytes())

    cfg = case.evolve.load_config(owner)
    assert cfg["enabled"] is False and cfg["auto_apply"] is False
    assert cfg["active_pref"] == {}
    assert "LEGACY_AMBIGUOUS_PREF" not in build_brief(case, owner)
    assert case.evolve.list_evolutions(owner)["evolutions"] == []
    assert not case.evolve.get_evolution(owner, "legacy-receipt")["ok"]
    assert not case.evolve.apply_evolution(owner, "legacy-receipt")["ok"]

    # Explicit new settings do not implicitly adopt the old active preference.
    assert case.evolve.save_config(owner, {"enabled": True})["ok"]
    assert case.evolve.load_config(owner)["active_pref"] == {}
    prepare_pref(case, owner=owner, marker="EXPLICIT_NEW_OWNER_PREF")
    assert "EXPLICIT_NEW_OWNER_PREF" in build_brief(case, owner)
    assert "LEGACY_AMBIGUOUS_PREF" not in build_brief(case, owner)
    assert (config_path.read_bytes(), evolution_path.read_bytes()) == before


def test_full_owner_namespace_keeps_missing_owner_distinct_from_named_default(case):
    prepare_pref(case, owner="default", marker="NAMED_DEFAULT_PRIVATE_PREF")
    assert case.evolve._config_path("") != case.evolve._config_path("default")
    assert case.evolve._evolution_dir("") != case.evolve._evolution_dir("default")
    assert case.evolve.load_config("")["enabled"] is False
    assert case.evolve.apply_active_pref("original facts", "") == "original facts"


def test_owner_namespace_preserves_partial_config_compatibility_and_runtime_roots(
        case, monkeypatch):
    path = Path(case.evolve._config_path(ALICE))
    # Missing optional fields still receive the original defaults. Unknown
    # payload fields cannot change the owner's storage key or enable apply.
    path.write_text(json.dumps({"enabled": True, "targets": {"workflow_snippets": True},
                                "user_id": BOB, "unexpected": "not a config field"}))
    cfg = case.evolve.load_config(ALICE)
    assert cfg["enabled"] is True and cfg["targets"]["workflow_snippets"] is True
    assert cfg["targets"]["harness_pref"] is False and cfg["auto_apply"] is False
    assert "user_id" not in cfg and "unexpected" not in cfg
    assert Path(case.evolve._config_path(ALICE)) == path
    before = path.read_bytes()
    monkeypatch.setenv("MEYO_LEGION_DIR", str(case.root / "other-runtime"))
    assert Path(case.evolve._config_path(ALICE)) != path
    assert case.evolve.load_config(ALICE)["enabled"] is False
    assert path.read_bytes() == before


def receipt_fixture(path, evolution_id, marker):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "evolution_id": evolution_id, "targets": [marker],
        "after": {"workflow_snippets": {"recent_task_types": [marker]}}}))
    return path


@pytest.mark.parametrize("origin", ["legacy", "other-owner"])
@pytest.mark.parametrize("absolute", [False, True], ids=["relative", "absolute"])
def test_receipt_boundary_rejects_foreign_and_legacy_paths(case, origin, absolute):
    prepare_pref(case)
    owner_dir = Path(case.evolve._evolution_dir(ALICE))
    foreign_dir = (Path(case.evolve._root()) / "evolutions" / "__ut_legacy_ambiguous"
                   if origin == "legacy" else Path(case.evolve._evolution_dir(BOB)))
    marker = "FOREIGN_RECEIPT_MUST_NOT_BE_ADOPTED"
    source = receipt_fixture(foreign_dir / "foreign-receipt.json", "foreign-receipt", marker)
    source_before = source.read_bytes()
    config_path = Path(case.evolve._config_path(ALICE))
    config_before = config_path.read_bytes()
    requested = (str(source) if absolute else os.path.relpath(source, owner_dir))[:-5]
    detail = case.evolve.get_evolution(ALICE, requested)
    applied = case.evolve.apply_evolution(ALICE, requested)
    assert not detail["ok"] and not applied["ok"]
    assert marker not in json.dumps(detail) and marker not in build_brief(case, ALICE)
    assert config_path.read_bytes() == config_before
    assert source.read_bytes() == source_before


@pytest.mark.parametrize("evolution_id", [
    "1700000000", "offline-manually-reviewed", "receipt.v1", "记录-01", " spaced id ",
])
def test_receipt_boundary_preserves_literal_valid_ids(case, evolution_id):
    case.evolve.save_config(ALICE, {"enabled": True})
    path = receipt_fixture(
        Path(case.evolve._evolution_dir(ALICE)) / f"{evolution_id}.json",
        evolution_id, "EXACT_OWN_RECEIPT")
    before = path.read_bytes()
    detail = case.evolve.get_evolution(ALICE, evolution_id)
    assert detail["ok"] and detail["evolution"]["evolution_id"] == evolution_id
    assert "EXACT_OWN_RECEIPT" not in build_brief(case, ALICE)
    assert case.evolve.apply_evolution(ALICE, evolution_id)["ok"]
    assert "EXACT_OWN_RECEIPT" in build_brief(case, ALICE)
    assert case.evolve.load_config(ALICE)["active_evolution_id"] == evolution_id
    rows = case.evolve.list_evolutions(ALICE)["evolutions"]
    assert [row["id"] for row in rows] == [evolution_id] and rows[0]["is_active"]
    assert not case.evolve.get_evolution(BOB, evolution_id)["ok"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("requested", [
    "./owned", "sub/../owned", "../unused", r"..\owned", "", None, 123,
    ".", "..", "owned\0ignored",
])
def test_receipt_boundary_does_not_normalize_or_coerce_ids(case, requested):
    case.evolve.save_config(ALICE, {"enabled": True})
    owner_dir = Path(case.evolve._evolution_dir(ALICE))
    for name in ("owned", "None", "123", r"..\owned", ""):
        receipt_fixture(owner_dir / f"{name}.json", name, "MUST_NOT_GUESS_THIS_ID")
    (owner_dir / "sub").mkdir()
    config_path = Path(case.evolve._config_path(ALICE))
    before = config_path.read_bytes()
    for result in (case.evolve.get_evolution(ALICE, requested),
                   case.evolve.apply_evolution(ALICE, requested)):
        assert not result["ok"], result
        assert result.get("reason") == "invalid_evolution_id", result
    assert config_path.read_bytes() == before


@pytest.mark.parametrize("origin", ["legacy", "other-owner"])
def test_receipt_boundary_rejects_symlinked_record_in_detail_apply_and_list(case, origin):
    prepare_pref(case)
    owner_dir = Path(case.evolve._evolution_dir(ALICE))
    foreign_dir = (Path(case.evolve._root()) / "evolutions" / "__ut_legacy_ambiguous"
                   if origin == "legacy" else Path(case.evolve._evolution_dir(BOB)))
    source = receipt_fixture(foreign_dir / "foreign.json", "FOREIGN_ID", "PRIVATE_LINK_TARGET")
    (owner_dir / "linked.json").symlink_to(source)
    source_before = source.read_bytes()
    config_path = Path(case.evolve._config_path(ALICE))
    config_before = config_path.read_bytes()
    detail = case.evolve.get_evolution(ALICE, "linked")
    applied = case.evolve.apply_evolution(ALICE, "linked")
    listing = case.evolve.list_evolutions(ALICE)
    assert not detail["ok"] and not applied["ok"]
    assert "PRIVATE_LINK_TARGET" not in json.dumps([detail, listing])
    assert [row["id"] for row in listing["evolutions"]] == ["offline-manually-reviewed"]
    assert config_path.read_bytes() == config_before
    assert source.read_bytes() == source_before


@pytest.mark.parametrize("component", ["evolutions", "by-owner", "owner"])
def test_receipt_boundary_rejects_symlinked_owner_ancestors(case, component):
    case.evolve.save_config(ALICE, {"enabled": True})
    parts = ["evolutions", "by-owner", case.evolve._owner_key(ALICE)]
    index = {"evolutions": 0, "by-owner": 1, "owner": 2}[component]
    link = Path(case.evolve._root()).joinpath(*parts[:index + 1])
    outside = case.root / "foreign-receipts"
    source = receipt_fixture(
        outside.joinpath(*parts[index + 1:], "receipt.json"),
        "receipt", "FOREIGN_DIRECTORY_RECORD")
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    source_before = source.read_bytes()
    config_path = Path(case.evolve._config_path(ALICE))
    config_before = config_path.read_bytes()
    detail = case.evolve.get_evolution(ALICE, "receipt")
    applied = case.evolve.apply_evolution(ALICE, "receipt")
    listing = case.evolve.list_evolutions(ALICE)
    assert not detail["ok"] and not applied["ok"]
    assert listing["evolutions"] == []
    assert "FOREIGN_DIRECTORY_RECORD" not in json.dumps([detail, listing])
    assert config_path.read_bytes() == config_before
    assert source.read_bytes() == source_before


@pytest.mark.parametrize("component", ["evolve", "by-owner", "config"])
def test_receipt_boundary_config_symlinks_are_not_read_or_written_through(case, component):
    evolution_id = prepare_pref(case, apply=False)
    config_path = Path(case.evolve._config_path(ALICE))
    parts = ["evolve", "by-owner", config_path.name]
    index = {"evolve": 0, "by-owner": 1, "config": 2}[component]
    link = Path(case.evolve._root()).joinpath(*parts[:index + 1])
    outside = case.root / "foreign-config"
    source = outside.joinpath(*parts[index + 1:]) if index < 2 else outside
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(json.dumps({
        "enabled": True, "active_evolution_id": "foreign-config",
        "active_pref": {"workflow_snippets": {"recent_task_types": ["FOREIGN_CONFIG_PREF"]}}}))
    before = source.read_bytes()
    link.rename(link.with_name(link.name + ".original"))
    link.symlink_to(outside, target_is_directory=index < 2)
    config = case.evolve.load_config(ALICE)
    brief = build_brief(case, ALICE)
    results = [
        case.evolve.apply_evolution(ALICE, evolution_id),
        case.evolve.save_config(ALICE, {"enabled": True}),
        case.evolve.rollback(ALICE),
    ]
    assert config["enabled"] is False and config["active_pref"] == {}
    assert "FOREIGN_CONFIG_PREF" not in brief
    assert all(not result["ok"] for result in results), results
    assert source.read_bytes() == before


@pytest.mark.parametrize("replace", ["record", "owner-directory"])
def test_receipt_boundary_keeps_owner_when_path_changes_at_open(case, monkeypatch, replace):
    import builtins
    evolution_id = prepare_pref(case, marker="ORIGINAL_OWN_PREF", apply=False)
    owner_dir = Path(case.evolve._evolution_dir(ALICE))
    own_file = owner_dir / f"{evolution_id}.json"
    source = receipt_fixture(
        case.root / "foreign-on-open" / own_file.name,
        evolution_id, "SWAPPED_FOREIGN_PREF")
    before = source.read_bytes()
    real_open, real_os_open = builtins.open, os.open
    replaced = []

    def swap(value):
        if replaced or not isinstance(value, (str, bytes, os.PathLike)):
            return
        if os.fsdecode(value) not in {str(own_file), own_file.name}:
            return
        replaced.append(True)
        path, target = ((own_file, source) if replace == "record"
                        else (owner_dir, source.parent))
        path.rename(path.with_name(path.name + ".original"))
        path.symlink_to(target, target_is_directory=replace == "owner-directory")

    def normal_open(value, *args, **kwargs):
        swap(value)
        return real_open(value, *args, **kwargs)

    def fd_open(value, *args, **kwargs):
        swap(value)
        return real_os_open(value, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", normal_open)
    monkeypatch.setattr(os, "open", fd_open)
    result = case.evolve.get_evolution(ALICE, evolution_id)
    assert replaced, "replace exactly at the real file-open boundary"
    assert "SWAPPED_FOREIGN_PREF" not in json.dumps(result), result
    if result["ok"]:
        # A descriptor pinned before the directory rename may safely finish
        # reading the original owner's file, never the new link target.
        assert "ORIGINAL_OWN_PREF" in json.dumps(result)
    assert not case.evolve.apply_evolution(ALICE, evolution_id)["ok"]
    assert source.read_bytes() == before


def test_receipt_boundary_distinguishes_missing_and_unreadable_records(case):
    prepare_pref(case)
    config_path = Path(case.evolve._config_path(ALICE))
    before = config_path.read_bytes()
    for result in (case.evolve.get_evolution(ALICE, "missing"),
                   case.evolve.apply_evolution(ALICE, "missing")):
        assert not result["ok"] and result["error"] == "evolution not found"
    owner_dir = Path(case.evolve._evolution_dir(ALICE))
    (owner_dir / "broken.json").write_text("{broken json")
    (owner_dir / "not-an-object.json").write_text("[]")
    os.mkfifo(owner_dir / "pipe.json")
    for evolution_id in ("broken", "not-an-object", "pipe"):
        for result in (case.evolve.get_evolution(ALICE, evolution_id),
                       case.evolve.apply_evolution(ALICE, evolution_id)):
            assert not result["ok"]
            assert result.get("reason") == "evolution_unreadable", result
    assert config_path.read_bytes() == before


@pytest.mark.parametrize("operation", ["apply", "save", "rollback"])
def test_receipt_boundary_rejects_config_link_replaced_at_write(
        case, monkeypatch, operation):
    import builtins
    evolution_id = prepare_pref(case)
    config_path = Path(case.evolve._config_path(ALICE))
    foreign = case.root / "foreign-config-at-write.json"
    foreign.write_text('{"enabled": true, "private_marker": "DO_NOT_OVERWRITE"}')
    before = foreign.read_bytes()
    real_open, real_os_open = builtins.open, os.open
    replaced = []

    def swap(value, writing):
        if not writing or replaced or not isinstance(value, (str, bytes, os.PathLike)):
            return
        if os.fsdecode(value) not in {str(config_path), config_path.name}:
            return
        replaced.append(True)
        config_path.rename(config_path.with_suffix(".original"))
        config_path.symlink_to(foreign)

    def normal_open(value, mode="r", *args, **kwargs):
        swap(value, any(flag in mode for flag in "wa+"))
        return real_open(value, mode, *args, **kwargs)

    def fd_open(value, flags, *args, **kwargs):
        swap(value, flags & (os.O_WRONLY | os.O_RDWR))
        return real_os_open(value, flags, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", normal_open)
    monkeypatch.setattr(os, "open", fd_open)
    if operation == "apply":
        result = case.evolve.apply_evolution(ALICE, evolution_id)
    elif operation == "save":
        result = case.evolve.save_config(ALICE, {"enabled": False})
    else:
        result = case.evolve.rollback(ALICE)
    assert replaced and not result["ok"], result
    assert foreign.read_bytes() == before


@pytest.mark.parametrize("blocked_stage", ["record-write", "auto-apply"])
def test_receipt_boundary_generation_does_not_write_through_or_claim_failed_apply(
        case, monkeypatch, blocked_stage):
    assert case.evolve.save_config(ALICE, {
        "enabled": True, "targets": {"workflow_snippets": True},
        "wiki": {"enabled": False}, "auto_apply": blocked_stage == "auto-apply",
    })["ok"]
    monkeypatch.setattr(case.evolve, "time", SimpleNamespace(time=lambda: 1700000000))
    monkeypatch.setattr(case.evolve, "_collect_evidence", lambda *args: {
        "count": 1, "sessions": [{"title": "OFFLINE_OWN_OBSERVATION"}], "harness_usage": {}})
    foreign = case.root / "foreign-generation-target.json"
    foreign.write_text('{"private_marker": "KEEP_FOREIGN_GENERATION_BYTES"}')
    before = foreign.read_bytes()

    def offline_reflection(*args, **kwargs):
        if blocked_stage == "auto-apply":
            config_path = Path(case.evolve._config_path(ALICE))
            config_path.rename(config_path.with_suffix(".original"))
            config_path.symlink_to(foreign)
        return {"final": "Offline synthetic reflection; no model or network call was made."}

    monkeypatch.setattr(case.core, "run_one", offline_reflection)
    if blocked_stage == "record-write":
        path = Path(case.evolve._evolution_dir(ALICE)) / "1700000000.json"
        path.symlink_to(foreign)
    result = case.evolve.run_evolution(ALICE)
    if blocked_stage == "record-write":
        assert not result["ok"], result
    else:
        assert result["ok"] and result["diff"]["evolution_id"] == "1700000000"
        assert result["auto_applied"] is False, result
        assert result["apply_result"]["ok"] is False, result
    assert foreign.read_bytes() == before


def server_brief(case, name, owner):
    tree = ast.parse((SERVICE / "server.py").read_text())
    statements = None
    for parent in ast.walk(tree):
        for _field, value in ast.iter_fields(parent):
            if not isinstance(value, list):
                continue
            for i, node in enumerate(value):
                if (isinstance(node, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
                        and isinstance(node.value, ast.Call)
                        and isinstance(node.value.func, ast.Attribute)
                        and node.value.func.attr == "_build_runtime_brief"):
                    statements = [node]
                    # c8 has a separate failing try/except application; execute
                    # it too until the production caller is unified.
                    for following in value[i + 1:i + 3]:
                        if isinstance(following, ast.Try) and any(
                                isinstance(n, ast.Attribute) and n.attr == "apply_active_pref"
                                for n in ast.walk(following)):
                            statements.append(following)
                    break
    assert statements is not None
    state = {"LEGION": case.core, "task": QUESTION, "question": QUESTION,
             "_single_plan": {"tasks": {"member": "inspect"}, "user_id": BOB},
             "_plan": {"tasks": {"member": "inspect"}, "user_id": BOB},
             "_uid": owner, "_uid_s": owner}
    exec(compile(ast.Module(body=statements, type_ignores=[]),
                 str(SERVICE / "server.py"), "exec"), state)
    return state[name]


@pytest.mark.parametrize("name", ["_brief", "_solo_brief"])
def test_real_server_single_and_solo_callers_bind_preference_once(case, name):
    prepare_pref(case)
    result = server_brief(case, name, ALICE)
    assert isinstance(result, str) and result.count(PREF) == 1
    assert PREF not in server_brief(case, name, BOB)


def test_every_existing_core_brief_caller_forwards_its_trusted_owner(case):
    tree = ast.parse((SERVICE / "legion_core.py").read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "_build_runtime_brief"]
    assert len(calls) >= 4
    missing = [node.lineno for node in calls if not any(
        item.arg == "user_id" and isinstance(item.value, ast.Name) and item.value.id == "user_id"
        for item in node.keywords)]
    assert not missing, {"callers_without_trusted_owner": missing}


def test_real_dispatch_consumes_the_same_owners_learning_and_preference(case, tmp_path, monkeypatch):
    from test_dispatch_result_order import (
        case as dispatch_fixture, task, HARNESS, TEAM, OWNER, CONVERSATION)
    prelude, brief_builder = case.core._build_prelude, case.core._build_runtime_brief
    runtime_root = tmp_path / "dispatch-fixture"
    runtime_root.mkdir()
    fixture = dispatch_fixture.__wrapped__(runtime_root, monkeypatch)
    runtime = next(fixture)
    try:
        monkeypatch.setattr(runtime.core, "_build_prelude", prelude)
        monkeypatch.setattr(runtime.core, "_build_runtime_brief", brief_builder)
        category = capture(case, owner=OWNER)["category"]
        prepare_pref(case, owner=OWNER)
        record = add_memory(case, owner=OWNER, answer="OWNED_DISPATCH_MEMORY")
        calls = []

        def member(key, prompt, **kwargs):
            calls.append({"key": key, "prompt": prompt, **kwargs})
            return {"status": "done", "final": "original task completed"}

        monkeypatch.setattr(runtime.core, "run_one", member)
        plan = {"captain": HARNESS, "tasks": {"A": task()},
                "delivery_knowledge_categories": [category],
                "team_policy": {"continuous_coordination": False},
                "project_context": {"repo_root": str(runtime.work), "user_id": OWNER,
                                    "conversation_id": CONVERSATION}}
        out = runtime.core.dispatch(QUESTION, plan, qhash=TEAM, mode="orchestrate",
                                    user_id=OWNER, conversation_id=CONVERSATION)
        assert out.get("results"), out
        assert len(calls) == 1, calls
        assert calls[0]["user_id"] == OWNER
        assert FACT in calls[0]["prompt"] and "pending_review" in calls[0]["prompt"]
        assert "OWNED_DISPATCH_MEMORY" in calls[0]["prompt"]
        assert record["entry_id"] in calls[0]["prompt"]
        assert PREF in calls[0]["runtime_brief"]
        original = next(row for row in case.memory._load()
                        if row["entry_id"] == record["entry_id"])
        assert original["adopted"] == 0
    finally:
        with pytest.raises(StopIteration):
            next(fixture)


@pytest.mark.parametrize("caller,plan_owner", [
    (ALICE, BOB), (ALICE, ALICE), ("", BOB), ("default", BOB),
], ids=["bound-conflict", "bound-match", "empty-caller", "default-caller"])
@pytest.mark.parametrize("cancelled", [False, True], ids=["running", "cancelled"])
def test_dispatch_owner_comes_from_invocation_not_plan(
        case, tmp_path, monkeypatch, caller, plan_owner, cancelled):
    from test_dispatch_result_order import case as dispatch_fixture, task, HARNESS
    prelude, brief_builder = case.core._build_prelude, case.core._build_runtime_brief
    runtime_root = tmp_path / "dispatch-owner"
    runtime_root.mkdir()
    fixture = dispatch_fixture.__wrapped__(runtime_root, monkeypatch)
    runtime = next(fixture)
    qhash, conversation = "__ut_owner_invocation", "__ut_owner_conversation"
    expected = caller if caller not in ("", "default") else f"anon:{qhash}"
    try:
        monkeypatch.setattr(runtime.core, "_build_prelude", prelude)
        monkeypatch.setattr(runtime.core, "_build_runtime_brief", brief_builder)
        category = capture(case, owner=BOB)["category"]
        prepare_pref(case, owner=BOB, marker="BOB_PRIVATE_APPLIED_PREF")
        add_memory(case, owner=BOB, answer="BOB_PRIVATE_MEMORY")
        capture(case, owner=expected, fact="TRUSTED_CALLER_OBSERVATION")
        prepare_pref(case, owner=expected, marker="TRUSTED_CALLER_APPLIED_PREF")
        calls = []

        def member(key, prompt, **kwargs):
            calls.append({"key": key, "prompt": prompt, **kwargs})
            return {"status": "done", "final": "synthetic task completed"}

        monkeypatch.setattr(runtime.core, "run_one", member)
        plan = {
            "captain": HARNESS, "tasks": {"A": task()}, "user_id": plan_owner,
            "team_policy": {"continuous_coordination": False},
            "delivery_knowledge_categories": [category],
            "project_context": {"repo_root": str(runtime.work), "user_id": expected,
                                "conversation_id": conversation},
        }
        if cancelled:
            runtime.core.request_cancel(qhash)
        out = runtime.core.dispatch(QUESTION, plan, qhash=qhash, mode="orchestrate",
                                    user_id=caller, conversation_id=conversation)
        packet = importlib.import_module("project_context").load(qhash)
        assert packet["user_id"] == expected
        assert packet["conversation_id"] == conversation
        assert packet["repo_root"] == str(runtime.work)
        assert plan["user_id"] == plan_owner, "binding must not mutate caller task content"
        if cancelled:
            assert runtime.core.is_cancelled(qhash)
            assert calls == [], "an owner conflict must not restart a cancelled request"
        else:
            assert out.get("results") and len(calls) == 1
            assert calls[0]["user_id"] == expected
            assert "TRUSTED_CALLER_OBSERVATION" in calls[0]["prompt"]
            assert "pending_review" in calls[0]["prompt"]
            assert FACT not in calls[0]["prompt"]
            assert "BOB_PRIVATE_MEMORY" not in calls[0]["prompt"]
            brief = calls[0]["runtime_brief"]
            assert isinstance(brief, str) and "TRUSTED_CALLER_APPLIED_PREF" in brief
            assert "BOB_PRIVATE_APPLIED_PREF" not in brief
    finally:
        runtime.core.clear_cancel(qhash)
        with pytest.raises(StopIteration):
            next(fixture)


def test_dispatch_plan_owner_cannot_claim_another_owners_existing_project(
        case, tmp_path, monkeypatch):
    from test_dispatch_result_order import case as dispatch_fixture, task, HARNESS
    runtime_root = tmp_path / "dispatch-existing-owner"
    runtime_root.mkdir()
    fixture = dispatch_fixture.__wrapped__(runtime_root, monkeypatch)
    runtime = next(fixture)
    qhash, conversation = "__ut_existing_owner", "__ut_existing_conversation"
    try:
        plan = {
            "captain": HARNESS, "tasks": {"A": task()}, "user_id": BOB,
            "team_policy": {"continuous_coordination": False},
            "project_context": {"repo_root": str(runtime.work), "user_id": BOB,
                                "conversation_id": conversation},
        }
        project = importlib.import_module("project_context")
        project.initialize(qhash, QUESTION, plan, mode="orchestrate",
                           user_id=BOB, conversation_id=conversation)
        path = project._path(qhash)
        before = path.read_bytes()
        with pytest.raises(PermissionError):
            runtime.core.dispatch(QUESTION, plan, qhash=qhash, mode="orchestrate",
                                  user_id=ALICE, conversation_id=conversation)
        assert runtime.calls == []
        assert path.read_bytes() == before
        assert plan["user_id"] == BOB
    finally:
        with pytest.raises(StopIteration):
            next(fixture)


@pytest.mark.parametrize("depth", ["standard", "deep"])
@pytest.mark.parametrize("consumer", ["native", "disabled_auto"])
def test_owned_capture_and_applied_pref_reach_actual_prompt_consumers(
        case, tmp_path, monkeypatch, depth, consumer):
    from test_research_native_send_payload import prepare_native_payload_stack
    category = capture(case)["category"]
    renderer = case.knowledge.match_context
    stack = prepare_native_payload_stack(tmp_path / "native-fixture", monkeypatch)
    monkeypatch.setattr(case.knowledge, "match_context", renderer)
    # The adapter fixture intentionally changes the runtime roots. Apply the
    # user's preference at the current runtime root, not the previous fixture's.
    prepare_pref(case, owner=ALICE)
    plan = selected(category)
    task = QUESTION + stack.core._build_prelude(QUESTION, plan, user_id=ALICE)
    brief = build_brief(case, ALICE)
    if consumer == "native":
        path = tmp_path / "native-send.json"
        monkeypatch.setenv("C_NATIVE_PAYLOAD_CAPTURE", str(path))
        result = stack.core._run_core_inprocess(
            "__ut_terminal", task, "learning-send", model_override="fixture-model",
            appid_override="fixture-only", user_id=ALICE, session_id="learning-session",
            runtime_brief=brief, timeout_s=20, execution_depth=depth)
        assert result["final"] == "NATIVE_PAYLOAD_OK", result
        sent = json.loads(path.read_text())["task"]
    else:
        registry = stack.registry
        real_source = Path(registry._auto_adapter.__code__.co_filename).resolve()
        monkeypatch.setattr(registry, "__file__", str(real_source))
        observed = []
        prompt_builder = registry._legion_prompt

        def observe(*args, **kwargs):
            prompt = prompt_builder(*args, **kwargs)
            observed.append(prompt)
            return prompt

        def no_engine(*args, **kwargs):
            pytest.fail("Auto is disabled; it must not be enabled by this repair")

        monkeypatch.setattr(registry, "_legion_prompt", observe)
        monkeypatch.setattr(registry, "_run_task", no_engine)
        result = registry._auto_adapter("learning-auto", task, ctx=registry.HarnessCtx(
            user_id=ALICE, execution_depth=depth, runtime_brief=brief))
        assert result["status"] == "unavailable" and result["final"] == ""
        assert len(observed) == 1
        sent = observed[0]
    assert FACT in sent and "pending_review" in sent
    assert PREF in sent and "offline-manually-reviewed" in sent
