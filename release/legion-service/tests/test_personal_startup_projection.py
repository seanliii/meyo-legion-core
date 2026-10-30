"""First-start projection uses the same explicit binding, without claiming model success."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

SERVICE = Path(__file__).resolve().parents[1]
ORCH = SERVICE.parents[1] / "harness_baselines/meyo-orchestrator"


@pytest.fixture
def startup(tmp_path, monkeypatch):
    for key, leaf in (
        ("HOME", "home"), ("MEYO_LEGION_DIR", "data"),
        ("LEGION_HOME_DIR", "legion"), ("LEGION_PROC_REGISTRY_DIR", "registry"),
    ):
        path = tmp_path / leaf
        path.mkdir()
        monkeypatch.setenv(key, str(path))
    monkeypatch.syspath_prepend(str(ORCH))
    monkeypatch.syspath_prepend(str(SERVICE))
    monkeypatch.setenv("LEGION_CODEX_TRANSPORT", "responses")
    monkeypatch.setenv("LEGION_CODEX_RESPONSES_BINDING", json.dumps({
        "model": "personal-fixture-model", "base_url": "https://models.example.invalid/v1",
        "api_key_env": "PERSONAL_FIXTURE_KEY",
    }))
    monkeypatch.setenv("PERSONAL_FIXTURE_KEY", "private-startup-fixture-value")
    monkeypatch.setenv("MEYO_UNIFIED_APPID", "old-company-fixture-value")
    monkeypatch.setenv("MEYO_UNIFIED_GATEWAY", "https://aigc.sankuai.com/v1/openai/native")
    model = importlib.import_module("model_registry")
    core = importlib.import_module("legion_core")

    def forbidden(*args, **kwargs):
        raise AssertionError("startup projection may not probe network/models or select the old pool")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(core, "select_usable_model", forbidden)
    monkeypatch.setattr(core, "gateway_status", forbidden)
    monkeypatch.setattr(core, "_swr_bg_refresh", forbidden)
    monkeypatch.setattr(core, "_ORCH_OK", True)
    monkeypatch.setattr(core, "_HARNESS_LIST_CACHE", {"data": []})
    monkeypatch.setattr(core, "list_harnesses", lambda *a, **kw: [])
    monkeypatch.setattr(core, "_AIGC_READY_CACHE", {
        "data": {"appid_configured": True, "runnable_clis": ["cc", "hermes"],
                 "gateway": "https://old-cache.example.invalid/v1"},
        "ts": 10**12,
    })
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: "/fixture/codex" if name == "codex" else None)
    monkeypatch.setattr(model, "_load_prefs", lambda: {"codex": "stale-preference-model"})
    monkeypatch.setattr(model, "_load_models", lambda: {})
    return SimpleNamespace(model=model, core=core, monkeypatch=monkeypatch, root=tmp_path)


def no_secret(value):
    text = json.dumps(value, ensure_ascii=False, default=str)
    assert "private-startup-fixture-value" not in text
    assert "old-company-fixture-value" not in text
    assert "PERSONAL_FIXTURE_KEY" not in text
    assert "api_key" not in text


def test_explicit_model_is_visible_without_company_catalog_or_old_preferences(startup):
    result = startup.model.list_models("programming", harness_key="codex")
    assert result["default"] == "personal-fixture-model"
    assert len(result["candidates"]) == 1
    row = result["candidates"][0]
    assert row["model"] == "personal-fixture-model"
    assert row["configured"] is True and row["verified"] is False
    assert row["stable"] is False
    assert row["harnesses"] == ["codex"]
    assert startup.model.resolve_model("codex")["model"] == "personal-fixture-model"
    assert startup.model.recommended_for("codex") == result["candidates"]
    assert "programming" in startup.model.all_directions()
    no_secret(result)


def test_bound_model_does_not_become_another_members_credential(startup):
    assert all(row.get("model") != "personal-fixture-model"
               for row in startup.model.recommended_for("hermes"))
    result = startup.model.list_models("programming", harness_key="hermes")
    assert result["candidates"] == []
    no_secret(result)


def test_global_catalog_keeps_legacy_default_and_marks_codex_only_row(startup):
    startup.monkeypatch.setattr(startup.model, "_load_models", lambda: {
        "programming": {"candidates": [
            {"model": "legacy-fixture-model", "rank": 1, "status": "PASS"},
        ]},
    })
    result = startup.model.list_models("programming")
    assert result["default"] == "legacy-fixture-model"
    assert {row["model"] for row in result["candidates"]} == {
        "legacy-fixture-model", "personal-fixture-model",
    }
    personal = next(row for row in result["candidates"] if row["model"] == "personal-fixture-model")
    assert personal["harnesses"] == ["codex"] and personal["verified"] is False
    other = startup.model.list_models("programming", harness_key="hermes")
    assert [row["model"] for row in other["candidates"]] == ["legacy-fixture-model"]
    no_secret(result)


def test_assignment_uses_operator_model_without_putting_key_into_plan(startup):
    assignments = startup.model.resolve_assignments({}, ["codex"])
    assert assignments["codex"]["model"] == "personal-fixture-model"
    assert not assignments["codex"].get("appid")
    assert assignments["codex"]["source"] == "operator_binding"
    no_secret(assignments)
    conflict = startup.model.resolve_assignments(
        {"model_overrides": {"codex": "different-model"}}, ["codex"])
    assert conflict["codex"]["model"] == "different-model"
    assert conflict["codex"].get("configuration_error")
    no_secret(conflict)


@pytest.mark.parametrize("fast", [False, True])
def test_configured_health_uses_current_binding_not_legacy_cache(startup, fast):
    result = startup.core.health(fast=fast, probe_gateway=True)
    assert result["ready_to_serve"] is True
    assert result["aigc"]["runnable_clis"] == ["codex"]
    assert result["aigc"]["appid_configured"] is True
    assert result["provider_verified"] is False
    assert result["readiness_basis"] == "configuration_and_executable"
    assert result["readiness_scope"] == ["codex"]
    assert result["fallback_model"]["model"] == "personal-fixture-model"
    assert "已验证" not in result["fallback_model"]["reason"]
    no_secret(result)


@pytest.mark.parametrize("change", ["key_removed", "bad_binding"])
def test_missing_or_bad_binding_cannot_use_cached_company_readiness(startup, change):
    if change == "key_removed":
        startup.monkeypatch.delenv("PERSONAL_FIXTURE_KEY")
    else:
        startup.monkeypatch.setenv("LEGION_CODEX_RESPONSES_BINDING", "{invalid")
    result = startup.core.health(fast=True)
    assert result["ready_to_serve"] is False
    assert result["aigc"]["appid_configured"] is False
    assert result["fallback_model"]["model"] is None
    assert startup.model.resolve_model("codex")["model"] is None
    assert startup.model.list_models("programming", harness_key="codex")["candidates"] == []
    no_secret(result)


def test_non_responses_mode_preserves_legacy_model_projection(startup):
    startup.monkeypatch.setenv("LEGION_CODEX_TRANSPORT", "auto")
    assert startup.model.resolve_model("codex")["model"] == "stale-preference-model"
    assert startup.model.list_models("programming")["candidates"] == []


def test_other_installed_clis_do_not_validate_a_missing_codex(startup):
    import shutil

    startup.monkeypatch.setattr(shutil, "which", lambda name: None if name == "codex" else "/fixture/other")
    result = startup.core.health(fast=True)
    assert result["ready_to_serve"] is False
    assert result["aigc"]["runnable_clis"] == []
    assert result["aigc"]["appid_configured"] is True
    assert result["provider_verified"] is False
    no_secret(result)


@pytest.mark.parametrize("key_present", [True, False])
def test_cached_member_rows_do_not_verify_a_different_current_provider(startup, key_present):
    import harness_view

    mp = startup.monkeypatch
    if not key_present:
        mp.delenv("PERSONAL_FIXTURE_KEY")
    readiness = {"declared": True, "connected": True, "verified": True,
                 "experienced": True, "samples": 9, "success_lb": 0.8}
    rows = [{"key": "codex", "available": True,
             "model": {"model": "cached-company-model", "source": "user_fixed"},
             "recommended": [{"model": "cached-company-model"}],
             "readiness": dict(readiness)},
            {"key": "hermes", "available": True,
             "model": {"model": "other-legitimate-model", "source": "user_fixed"},
             "readiness": dict(readiness)}]
    cached = {"harnesses": rows, "answer_pool": ["codex", "hermes"]}
    mp.setattr(harness_view, "_CACHE", {
        "alice": {"data": cached, "ts": time.time(), "refreshing": False},
    })
    mp.setattr(harness_view, "_native_entry_config", lambda: ([], ""))
    actual = harness_view.snapshot("alice", verified_user_id="alice")
    codex, hermes = actual["harnesses"]
    assert codex["model"]["model"] == ("personal-fixture-model" if key_present else None)
    assert codex["model"]["configured"] is key_present
    assert codex["readiness"]["provider_verified"] is False
    assert codex["readiness"]["verified"] is False
    assert codex["readiness"]["experienced"] is False
    assert codex["readiness"]["historical_verified"] is True
    assert hermes["model"] == rows[1]["model"]
    assert hermes["readiness"] == readiness
    assert rows[0]["readiness"] == readiness  # Do not rewrite evidence/cache.
    assert actual["answer_pool"] == ["codex", "hermes"]
    no_secret(actual)


@pytest.mark.parametrize("next_mode", ["auto", "bridge"])
def test_leaving_explicit_mode_reprojects_codex_without_overwriting_history(startup, next_mode):
    import harness_view

    mp = startup.monkeypatch
    mp.setattr(harness_view, "_CACHE", {})
    mp.setattr(harness_view, "_native_entry_config", lambda: ([], ""))
    mp.setattr(startup.core, "_harness_keys", lambda: {"codex"})
    mp.setattr(startup.core, "list_harnesses", lambda *a, **kw: [
        {"key": "codex", "available": True},
    ])
    mp.setattr(harness_view, "_profile_snapshot", lambda: {"codex": {"name": "Codex"}})
    mp.setattr(harness_view, "_performance_snapshot", lambda: {
        "codex": [{"good": 9, "bad": 0, "success_lb": 0.8}],
    })
    mp.setattr(harness_view, "_last_used_snapshot", lambda user: {})
    explicit = harness_view.snapshot("alice", force=True, verified_user_id="alice")
    assert explicit["harnesses"][0]["model"]["source"] == "operator_binding"
    assert explicit["harnesses"][0]["readiness"]["verified"] is False
    raw = harness_view._CACHE["alice"]["data"]["harnesses"][0]
    assert raw["model"]["source"] == "operator_binding" and raw["readiness"]["verified"]

    mp.setenv("LEGION_CODEX_TRANSPORT", next_mode)
    current = harness_view.snapshot("alice", verified_user_id="alice")["harnesses"][0]
    assert current["model"]["model"] == "stale-preference-model"
    assert current["model"]["source"] == "user_fixed"
    assert "configured" not in current["model"]
    assert all(row["model"] != "personal-fixture-model" for row in current["recommended"])
    assert raw["model"]["source"] == "operator_binding"  # Cached evidence remains intact.
    assert raw["readiness"]["samples"] == 9
    no_secret(current)
