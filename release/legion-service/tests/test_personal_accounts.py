"""Password identities and private model settings; no model/device/network calls."""
import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

SERVICE = Path(__file__).resolve().parents[1]


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SERVICE))
    monkeypatch.setenv("LEGION_PERSONAL_ACCOUNTS_DIR", str(tmp_path / "accounts"))
    monkeypatch.setenv("LEGION_PERSONAL_OWNER", "owner-example")
    return importlib.import_module("personal_accounts")


def test_register_password_session_logout_and_no_plaintext(accounts, tmp_path):
    password = "only-a-generated-test-password"
    result, token = accounts.register("Tester-A", password)
    assert result["username"] == "tester-a"
    assert result["user_id"].startswith("acct_")
    assert result["execution_enabled"] is False
    assert accounts.resolve_token(token) == result["user_id"]
    second, fresh = accounts.login("tester-a", password)
    assert second["user_id"] == result["user_id"] and fresh != token
    raw = (tmp_path / "accounts/accounts.sqlite3").read_bytes()
    assert password.encode() not in raw and token.encode() not in raw
    assert os.stat(tmp_path / "accounts").st_mode & 0o777 == 0o700
    assert os.stat(tmp_path / "accounts/accounts.sqlite3").st_mode & 0o777 == 0o600
    accounts.logout(fresh)
    assert accounts.resolve_token(fresh) is None
    assert accounts.resolve_token(token) == result["user_id"]


@pytest.mark.parametrize("username", ["owner-example", "admin", "../bad", "a b", "a", "x" * 65])
def test_reserved_or_invalid_names(accounts, username):
    with pytest.raises(accounts.AccountError):
        accounts.register(username, "test-password-only")


def test_duplicate_wrong_password_expiry_and_revocation(accounts, monkeypatch):
    result, token = accounts.register("tester-a", "test-password-only")
    with pytest.raises(accounts.AccountError):
        accounts.register("TESTER-A", "other-test-password")
    for name in ["tester-a", "unknown-user"]:
        with pytest.raises(accounts.AccountError) as caught:
            accounts.login(name, "incorrect-test-password")
        assert caught.value.code == "invalid_credentials"
    now = accounts.time.time()
    monkeypatch.setattr(accounts.time, "time", lambda: now + accounts.SESSION_TTL + 1)
    assert accounts.resolve_token(token) is None


def test_owner_password_binding_preserves_identity_and_requires_current(accounts):
    result, token = accounts.set_password("owner-example", "owner-test-password")
    assert result["user_id"] == "owner-example"
    assert result["execution_enabled"] is True
    with pytest.raises(accounts.AccountError):
        accounts.set_password("owner-example", "new-test-password")
    accounts.set_password("owner-example", "new-test-password", "owner-test-password")
    assert accounts.resolve_token(token) is None
    result, token = accounts.login("owner-example", "new-test-password")
    assert accounts.resolve_token(token) == "owner-example"
    with pytest.raises(accounts.AccountError):
        accounts.set_password("someone-else", "other-test-password")


def test_keys_are_scoped_never_returned_and_never_activated(accounts):
    a, _ = accounts.register("tester-a", "test-password-only")
    b, _ = accounts.register("tester-b", "test-password-only")
    before = dict(os.environ)
    profile = {"id": "main", "provider": "deepseek", "model": "example-model",
               "base_url": "https://api.deepseek.com/v1", "api_key": "fake-private-key-A"}
    saved = accounts.save_model(a["user_id"], profile)
    assert "fake-private-key-A" not in json.dumps(saved)
    assert saved["provider_verified"] is False
    assert accounts.models(b["user_id"])["profiles"] == []
    listing = accounts.models(a["user_id"])
    assert listing["profiles"][0]["key_configured"] is True
    assert listing["execution_enabled"] is False
    assert "api_key" not in json.dumps(listing)
    accounts.delete_model(b["user_id"], "main")
    assert len(accounts.models(a["user_id"])["profiles"]) == 1
    accounts.delete_model(a["user_id"], "main")
    assert accounts.models(a["user_id"])["profiles"] == []
    assert os.environ == before


@pytest.mark.parametrize("endpoint", [
    "http://api.deepseek.com", "https://user:secret@example.com", "https://127.0.0.1",
    "https://example.com/?api_key=secret", "https://example.com/#secret",
])
def test_key_destination_validation_does_not_echo_input(accounts, endpoint):
    a, _ = accounts.register("tester-a", "test-password-only")
    with pytest.raises(accounts.AccountError) as caught:
        accounts.save_model(a["user_id"], {
            "id": "main", "provider": "custom", "model": "model",
            "base_url": endpoint, "api_key": "fake-private-key-A",
        })
    assert endpoint not in str(caught.value)
    assert "fake-private-key-A" not in str(caught.value)


def test_rate_limit_precedes_password_work(accounts, monkeypatch):
    for _ in range(accounts.USER_ATTEMPT_LIMIT):
        with pytest.raises(accounts.AccountError):
            accounts.login("unknown-user", "incorrect-test-password")
    monkeypatch.setattr(accounts, "_derive", lambda *args: pytest.fail("must throttle before hashing"))
    with pytest.raises(accounts.AccountError) as caught:
        accounts.login("unknown-user", "incorrect-test-password")
    assert caught.value.status == 429


def test_concurrent_registration_cannot_duplicate_username(accounts):
    def attempt(_):
        try:
            return accounts.register("tester-a", "test-password-only")[0]["user_id"]
        except accounts.AccountError:
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        values = list(executor.map(attempt, range(2)))
    assert len([value for value in values if value]) == 1


def test_insecure_or_symlink_store_fails_closed(accounts, tmp_path):
    target = tmp_path / "other"
    target.mkdir()
    (tmp_path / "accounts").symlink_to(target)
    with pytest.raises(accounts.AccountError):
        accounts.register("tester-a", "test-password-only")


def test_member_policy_denies_all_unlisted_capabilities(accounts):
    user, _ = accounts.register("tester-a", "test-password-only")
    for method, path in [
        ("POST", "/api/legion/dispatch"), ("POST", "/api/legion/tool"),
        ("POST", "/api/legion/parse_plan"), ("GET", "/api/legion/gateway"),
        ("GET", "/api/legion/cloud_desktop/display/auth"), ("POST", "/api/legion/mobile/ensure"),
        ("GET", "/cc"), ("GET", "/api/legion/newly-added-route"),
    ]:
        assert accounts.request_allowed(user["user_id"], method, path) is False
        assert accounts.request_allowed("owner-example", method, path) is True
    assert accounts.request_allowed(user["user_id"], "GET", "/api/sessions")
    assert accounts.request_allowed(user["user_id"], "GET", "/api/legion/my/models")
    assert accounts.request_allowed(user["user_id"], "GET", "/")


def test_non_string_provider_is_a_safe_validation_error(accounts):
    with pytest.raises(accounts.AccountError) as caught:
        accounts.save_model("owner-example", {
            "id": "example", "provider": [], "model": "model",
            "base_url": "https://example.invalid", "api_key": "fake-test-key",
        })
    assert caught.value.status == 400


def test_busy_or_successful_login_cannot_lock_out_everyone(accounts, monkeypatch):
    accounts.register("tester-a", "test-password-only")
    accounts._HASH_SLOTS.acquire()
    accounts._HASH_SLOTS.acquire()
    try:
        for i in range(125):
            with pytest.raises(accounts.AccountError) as caught:
                accounts.login("unknown-" + str(i), "test-password-only")
            assert caught.value.code == "account_service_busy"
    finally:
        accounts._HASH_SLOTS.release()
        accounts._HASH_SLOTS.release()
    for _ in range(10):
        result, _ = accounts.login("tester-a", "test-password-only")
        assert result["authenticated"]


@pytest.mark.parametrize("owner", ["acct_legacy", "a", "alice.smith", "a" * 49])
def test_legacy_owner_names_keep_password_and_execution_compatibility(accounts, monkeypatch, owner):
    monkeypatch.setenv("LEGION_PERSONAL_OWNER", owner)
    result, token = accounts.set_password(owner, "owner-test-password")
    assert result["execution_enabled"]
    assert accounts.request_allowed(owner, "POST", "/api/legion/dispatch")
    logged, _ = accounts.login(owner, "owner-test-password")
    assert logged["user_id"] == owner
