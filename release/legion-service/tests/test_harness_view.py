#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="legion-harness-view-"))
os.environ["MEYO_LEGION_DIR"] = str(TMP / "meyo")
os.environ["LEGION_HOME_DIR"] = str(TMP / "home")
os.environ["LEGION_SESSIONS_DIR"] = str(TMP / "home" / "sessions")
sys.path.insert(0, str(ROOT))

import harness_view


@pytest.fixture(autouse=True)
def _pin_isolation_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MEYO_LEGION_DIR", str(tmp_path / "meyo"))
    monkeypatch.setenv("LEGION_HOME_DIR", str(tmp_path / "home"))
    monkeypatch.setenv("LEGION_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("MEYO_NATIVE_ENTRIES_FILE", str(tmp_path / "native_entries.json"))
    harness_view.invalidate()
    yield
    harness_view.invalidate()


def test_cold_snapshot_returns_manifest_without_probe(monkeypatch):
    harness_view._CACHE.clear()
    monkeypatch.setattr(harness_view, "_schedule", lambda _uid: None)
    out = harness_view.snapshot("alice")
    assert out["warming"] is True
    assert len(out["harnesses"]) >= 10
    assert all(row.get("profile") for row in out["harnesses"])
    assert all(row.get("model") for row in out["harnesses"])


def test_warm_snapshot_is_reused(monkeypatch):
    harness_view._CACHE.clear()
    harness_view._CACHE["alice"] = {
        "data": {"harnesses": [{"key": "cc"}], "warming": False},
        "ts": __import__("time").time(), "refreshing": False}
    monkeypatch.setattr(harness_view, "_build", lambda _uid: (_ for _ in ()).throw(AssertionError("must not build")))
    out = harness_view.snapshot("alice")
    assert out["from_cache"] is True and out["harnesses"][0]["key"] == "cc"


def test_snapshots_are_isolated_per_user(monkeypatch):
    harness_view._CACHE.clear()
    monkeypatch.setattr(harness_view, "_build", lambda uid: {
        "harnesses": [{"key": "cc", "readiness": {"last_used": uid}}],
        "updated_at": 1, "warming": False})
    harness_view.snapshot("alice", force=True)
    harness_view.snapshot("bob", force=True)
    assert harness_view.snapshot("alice")["harnesses"][0]["readiness"]["last_used"] == "alice"
    assert harness_view.snapshot("bob")["harnesses"][0]["readiness"]["last_used"] == "bob"


def test_model_update_patches_all_cached_user_views_without_probe():
    harness_view._CACHE.clear()
    for uid in ("alice", "bob"):
        harness_view._CACHE[uid] = {
            "data": {"harnesses": [{"key": "codex", "model": {"model": "old"}}]},
            "ts": 1, "refreshing": True}
    generation = harness_view._GENERATION
    harness_view.update_model("codex", {"model": "gpt-5.4", "source": "user_fixed"})
    assert harness_view._GENERATION == generation + 1
    assert all(state["data"]["harnesses"][0]["model"]["model"] == "gpt-5.4"
               and state["refreshing"] is False for state in harness_view._CACHE.values())


def test_warm_snapshot_does_not_read_model_preferences(monkeypatch):
    import model_registry
    harness_view._CACHE.clear()
    harness_view._CACHE["alice"] = {
        "data": {"harnesses": [{"key": "codex", "model": {
            "model": "gpt-5.4", "source": "user_fixed"}}]},
        "ts": __import__("time").time(), "refreshing": False}
    monkeypatch.setattr(model_registry, "get_prefs", lambda: (_ for _ in ()).throw(
        AssertionError("cache hit must be a pure memory read")))
    monkeypatch.setattr(model_registry, "resolve_model", lambda _key: (_ for _ in ()).throw(
        AssertionError("cache hit must not resolve models")))
    out = harness_view.snapshot("alice")
    assert out["harnesses"][0]["model"]["model"] == "gpt-5.4"


def test_stale_background_refresh_cannot_overwrite_new_model(monkeypatch):
    harness_view._CACHE.clear()
    harness_view._CACHE["alice"] = {
        "data": {"harnesses": [{"key": "codex", "model": {"model": "old"}}]},
        "ts": 1, "refreshing": True}
    old_generation = harness_view._GENERATION
    monkeypatch.setattr(harness_view, "_build", lambda _uid: {
        "harnesses": [{"key": "codex", "model": {"model": "stale"}}],
        "updated_at": 2, "warming": False})
    harness_view.update_model("codex", {"model": "gpt-5.4", "source": "user_fixed"})
    harness_view._refresh("alice", old_generation)
    assert harness_view._CACHE["alice"]["data"]["harnesses"][0]["model"]["model"] == "gpt-5.4"


@pytest.fixture
def entry_view(monkeypatch):
    import runtime_manifest
    definitions = {
        "__ut_cli": {"specs": {"integration": "cli-subprocess", "resume": "native"}},
        "__ut_web": {"specs": {"integration": "http", "resume": "session"}},
        "__ut_device": {"specs": {"integration": "cli", "resume": "unavailable"}},
    }
    monkeypatch.setattr(runtime_manifest, "load_definitions", lambda: definitions)
    for uid in ("alice", "bob"):
        harness_view._CACHE[uid] = {
            "data": {"harnesses": [{"key": key, "url": "https://github.com/source/repo",
                                    "model": {"model": "existing"}}
                                   for key in definitions],
                     "answer_pool": ["__ut_cli"], "warming": False},
            "ts": __import__("time").time(), "refreshing": False}

    def read(entries, uid="alice"):
        path = Path(os.environ["MEYO_NATIVE_ENTRIES_FILE"])
        path.write_text(json.dumps({"schema_version": 1, "entries": entries}))
        # 此处显式模拟已认证的服务调用；普通 HTTP uid 参数不能代替它。
        return {h["key"]: h for h in harness_view.snapshot(
            uid, verified_user_id=uid)["harnesses"]}
    return read


def _entry(url="/native-test/workspace", **kwargs):
    return {
        "harness": "__ut_web", "kind": "web", "url": url, "status": "needs_login",
        "authentication": "native",
        "verification": {"url": url, "checked_at": datetime.now(timezone.utc).isoformat(),
                         "evidence": "isolated-test-only"},
        **kwargs,
    }


def test_runtime_entry_declaration_is_not_a_source_repo_link(entry_view):
    rows = entry_view([])
    entry = rows["__ut_cli"]["entry_points"]["execution"]
    assert entry == {"harness": "__ut_cli", "selectable": True,
                     "integration": "cli-subprocess", "resume": "native"}
    assert rows["__ut_web"]["entry_points"]["web"] == []
    assert not rows["__ut_device"]["entry_points"]["execution"]["selectable"]
    assert "github.com" not in json.dumps(rows["__ut_cli"]["entry_points"])


def test_entry_access_and_business_login_are_separate_facts(entry_view):
    row = entry_view([_entry()])["__ut_web"]["entry_points"]["web"][0]
    assert row["status"] == "needs_login"
    assert row["access_verified"] is True
    assert row["url"] == "/native-test/workspace"
    assert "verification" in row


@pytest.mark.parametrize("verification", [
    {}, {"url": "/different", "checked_at": "2026-09-16", "evidence": "test"},
    {"url": "/native-test/workspace", "checked_at": "invalid", "evidence": "test"},
])
@pytest.mark.parametrize("status", ["configured", "ready", "needs_login", "needs_setup"])
def test_safe_configured_entry_can_open_without_access_evidence(entry_view, verification, status):
    row = entry_view([_entry(verification=verification, status=status)])["__ut_web"]["entry_points"]["web"][0]
    assert row["access_verified"] is False
    assert row["verification"] is None
    assert row["url"] == "/native-test/workspace"
    assert row["authentication"] == "native"


def test_entry_without_declared_authentication_is_still_not_launchable(entry_view):
    row = entry_view([_entry(authentication="none")])["__ut_web"]["entry_points"]["web"][0]
    assert row["access_verified"] is False
    assert row["url"] == ""


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "file:///etc/passwd", "//outside.example/workspace",
    "http://127.0.0.1:13100/", "http://localhost:13100/", "http://localhost./",
    "http://127.1/", "http://2130706433/", "http://0x7f000001/",
    "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://0.0.0.0/",
    "http://%31%32%37.0.0.1/", "https://name:password@workspace.example/",
    "https://workspace.example\\@outside.example/", "https://workspace.example/\n",
    "https://workspace.example:70000/", "/workspace?access_token=not-a-real-token",
    "/workspace#access_token=not-a-real-token", "/workspace#/auth?api_key=not-a-real-key",
])
def test_unsafe_or_server_loopback_addresses_are_not_user_entry_urls(entry_view, url):
    row = entry_view([_entry(url)])["__ut_web"]["entry_points"]["web"][0]
    assert row["url"] == ""
    assert row["access_verified"] is False


def test_current_user_only_sees_owned_device_and_project_entries(entry_view):
    alice = _entry("/native-test/alice", user_id="alice",
                   binding={"session_id": "__ut_alice", "project_id": "__ut_project"})
    bob = _entry("/native-test/bob", user_id="bob")
    device = _entry("/native-test/device", harness="__ut_device", kind="device",
                    authentication="gateway", user_id="alice",
                    binding={"device_id": "__ut_assigned_device"})
    unowned_device = _entry("/native-test/unowned", harness="__ut_device", kind="device")
    config = [alice, bob, device, unowned_device]
    a = entry_view(config, "alice")
    b = entry_view(config, "bob")
    assert [e["url"] for e in a["__ut_web"]["entry_points"]["web"]] == ["/native-test/alice"]
    assert [e["url"] for e in b["__ut_web"]["entry_points"]["web"]] == ["/native-test/bob"]
    assert len(a["__ut_device"]["entry_points"]["device"]) == 1
    assert b["__ut_device"]["entry_points"]["device"] == []
    assert "__ut_alice" not in json.dumps(b)


@pytest.mark.parametrize("cache_mode", ["warm", "cold", "stale", "force"])
def test_private_entries_require_verified_identity_on_every_snapshot_path(
        entry_view, monkeypatch, cache_mode):
    entry_view([_entry("/native-test/alice-private", user_id="alice",
                       binding={"project_id": "__ut_private_project"})])
    data = harness_view._CACHE["alice"]["data"]
    monkeypatch.setattr(harness_view, "_build", lambda _uid: data)
    monkeypatch.setattr(harness_view, "_schedule", lambda _uid: None)
    monkeypatch.setattr(harness_view, "_profile_snapshot", lambda: {
        "__ut_web": {"name": "test"}})
    if cache_mode == "cold":
        harness_view.invalidate()
    elif cache_mode == "stale":
        harness_view._CACHE["alice"]["ts"] = 1
    authorized = harness_view.snapshot("alice", force=cache_mode == "force",
                                       verified_user_id="alice")
    assert "/native-test/alice-private" in json.dumps(authorized)
    assert authorized["entry_identity_verified"] is True
    assert authorized["entry_identity_user_id"] == "alice"
    unauthorized = harness_view.snapshot("alice", force=cache_mode == "force")
    assert "/native-test/alice-private" not in json.dumps(unauthorized)
    assert "__ut_private_project" not in json.dumps(unauthorized)
    assert unauthorized["entry_identity_verified"] is False
    assert unauthorized["entry_identity_user_id"] is None


def test_identity_subject_is_not_returned_for_mismatched_user_partition(entry_view):
    entry_view([_entry("/native-test/alice-private", user_id="alice")])
    data = harness_view.snapshot("alice", verified_user_id="bob")
    assert data["entry_identity_verified"] is False
    assert data["entry_identity_user_id"] is None
    assert "/native-test/alice-private" not in json.dumps(data)


def test_config_change_updates_entry_without_slow_harness_probe(entry_view, monkeypatch):
    monkeypatch.setattr(harness_view, "_build", lambda _uid: pytest.fail("must reuse cached models"))
    before = entry_view([_entry("/native-test/first")])
    after = entry_view([_entry("/native-test/second")])
    assert before["__ut_web"]["entry_points"]["web"][0]["url"] == "/native-test/first"
    assert after["__ut_web"]["entry_points"]["web"][0]["url"] == "/native-test/second"
    assert "entry_points" not in harness_view._CACHE["alice"]["data"]["harnesses"][0]


def test_expired_device_lease_does_not_offer_launch(entry_view):
    expiry = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    row = entry_view([_entry(expires_at=expiry)])["__ut_web"]["entry_points"]["web"][0]
    assert row["status"] == "expired"
    assert row["url"] == ""


def test_entry_config_failure_is_visible_but_does_not_break_directory(entry_view):
    entry_view([])
    Path(os.environ["MEYO_NATIVE_ENTRIES_FILE"]).write_text("{")
    out = harness_view.snapshot("alice")
    assert out["answer_pool"] == ["__ut_cli"]
    assert out["entry_configuration_error"] == "JSONDecodeError"
    assert out["harnesses"][0]["entry_points"]["execution"]["selectable"]
