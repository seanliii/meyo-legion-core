"""Actual HTTP cookie/identity/ownership checks, without external calls."""
from http.cookies import SimpleCookie
import json
from pathlib import Path

import pytest

from test_public_origin_entry import isolated, http_entry  # shared isolated real HTTP fixture


@pytest.fixture(autouse=True)
def configure_accounts(isolated):
    isolated.monkeypatch.setenv("LEGION_PERSONAL_ACCOUNTS_DIR", str(isolated.root / "secrets/accounts"))
    isolated.monkeypatch.setenv("LEGION_PERSONAL_OWNER", "invited-test")


def signup(entry, name="test-member-a"):
    status, headers, data = entry.request("POST", "/api/legion/auth/register", authenticated=False,
                                         body={"username": name, "password": "generated-test-password"})
    assert status == 200, data
    assert data["authenticated"] and not data["execution_enabled"]
    cookie = SimpleCookie()
    cookie.load(headers["Set-Cookie"])
    return data["user_id"], f"legion_personal={cookie['legion_personal'].value}", headers


def test_registration_cookie_status_logout_and_expired_session(http_entry):
    uid, cookie, headers = signup(http_entry)
    assert all(part in headers["Set-Cookie"] for part in ["Secure", "HttpOnly", "SameSite=Strict", "Max-Age=604800"])
    code, _, data = http_entry.request("GET", "/api/legion/auth/status", authenticated=False,
                                     extra={"Cookie": cookie})
    assert code == 200 and data["user_id"] == uid and data["method"] == "password"
    code, _, data = http_entry.request("POST", "/api/legion/auth/logout", authenticated=False,
                                     body={}, extra={"Cookie": cookie})
    assert code == 200 and not data["authenticated"]
    code, _, _ = http_entry.request("GET", "/api/legion/auth/status", authenticated=False,
                                  extra={"Cookie": cookie})
    assert code == 401
    code, _, data = http_entry.request("POST", "/api/legion/auth/login", authenticated=False,
                                      body={"username": "test-member-a", "password": "generated-test-password"})
    assert code == 200 and data["user_id"] == uid


@pytest.mark.parametrize("origin", [None, "https://untrusted.invalid", "null"])
def test_account_mutations_require_same_origin(http_entry, origin):
    code, _, _ = http_entry.request("POST", "/api/legion/auth/register", authenticated=False, origin=origin,
                                    body={"username": "test-member-a", "password": "generated-test-password"})
    assert code == 403


def test_owner_cannot_be_claimed_or_uid_supplied_and_old_token_still_works(http_entry, isolated):
    before = (isolated.root / "secrets/personal-tokens.json").read_bytes()
    for body in [
        {"username": "invited-test", "password": "generated-test-password"},
        {"username": "test-member-a", "password": "generated-test-password", "uid": "invited-test"},
    ]:
        code, _, _ = http_entry.request("POST", "/api/legion/auth/register", authenticated=False, body=body)
        assert code in (400, 409)
    code, _, data = http_entry.request("GET", "/api/legion/auth/status")
    assert code == 200 and data["user_id"] == "invited-test"
    assert before == (isolated.root / "secrets/personal-tokens.json").read_bytes()
    code, _, data = http_entry.request("POST", "/api/legion/auth/password",
                                      body={"password": "generated-owner-password"})
    assert code == 200 and data["user_id"] == "invited-test" and data["execution_enabled"]
    assert before == (isolated.root / "secrets/personal-tokens.json").read_bytes()
    code, _, _ = http_entry.request("POST", "/api/legion/auth/password",
                                   body={"password": "another-owner-password"})
    assert code == 403
    code, _, data = http_entry.request("POST", "/api/legion/auth/login", authenticated=False,
                                      body={"username": "invited-test", "password": "generated-owner-password"})
    assert code == 200 and data["user_id"] == "invited-test"


def test_members_never_reach_shared_execution_or_native_ui(http_entry, isolated):
    uid, cookie, _ = signup(http_entry)
    isolated.monkeypatch.setattr(http_entry.server.LEGION, "dispatch",
                                 lambda *a, **k: pytest.fail("shared execution reached"))
    for method, path in [
        ("POST", "/api/legion/dispatch"), ("POST", "/api/legion/run_one"),
        ("POST", "/api/legion/tool"), ("POST", "/api/legion/parse_plan"),
        ("POST", "/api/legion/cloud_desktop/start"), ("POST", "/api/legion/mobile/ensure"),
        ("GET", "/api/legion/gateway?probe=1"), ("GET", "/cc"), ("GET", "/hermes"),
        ("GET", "/api/legion/cloud_desktop/display/auth"),
    ]:
        code, _, data = http_entry.request(method, path, authenticated=False, extra={"Cookie": cookie},
                                          body={} if method == "POST" else None)
        assert code == 403 and data["error"] == "account_execution_unavailable", (path, code, data)
    assert http_entry.seen == []
    code, _, data = http_entry.request("GET", "/api/sessions?uid=invited-test", authenticated=False,
                                      extra={"Cookie": cookie})
    assert code == 403


def test_http_key_save_delete_and_cross_user_privacy(http_entry):
    a, ca, _ = signup(http_entry)
    b, cb, _ = signup(http_entry, "test-member-b")
    value = {"id": "my-model", "provider": "openrouter", "model": "sample-model",
             "base_url": "https://openrouter.ai/api/v1", "api_key": "fake-test-private-key"}
    code, headers, data = http_entry.request("POST", "/api/legion/my/models", authenticated=False,
                                            body=value, extra={"Cookie": ca})
    assert code == 200 and data["provider_verified"] is False
    assert "fake-test-private-key" not in json.dumps(data)
    assert headers["Cache-Control"] == "no-store"
    for cookie, expected in [(ca, 1), (cb, 0)]:
        code, _, data = http_entry.request("GET", "/api/legion/my/models", authenticated=False,
                                          extra={"Cookie": cookie})
        assert code == 200 and len(data["profiles"]) == expected
        assert "fake-test-private-key" not in json.dumps(data) and not data["execution_enabled"]
    code, _, _ = http_entry.request("POST", "/api/legion/my/models/delete", authenticated=False,
                                    body={"uid": a, "id": "my-model"}, extra={"Cookie": cb})
    assert code == 403
    code, _, _ = http_entry.request("POST", "/api/legion/my/models/delete", authenticated=False,
                                    body={"id": "my-model"}, extra={"Cookie": ca})
    assert code == 200


def test_oversized_auth_input_is_rejected_before_password_work(http_entry):
    code, _, _ = http_entry.request("POST", "/api/legion/auth/login", authenticated=False,
                                    body={"username": "test-member-a", "password": "x" * 17000})
    assert code == 413


def test_anonymous_and_settings_only_roster_cannot_read_owner_history_or_probe(http_entry, isolated):
    import harness_view
    import time
    secret = "PRIVATE-OWNER-QUERY-DO-NOT-SHARE"
    isolated.monkeypatch.setattr(harness_view, "_CACHE", {
        "invited-test": {"data": {"harnesses": [{"key": "codex", "readiness": {
            "last_used": {"question": secret}}}]}, "ts": time.time(), "refreshing": False},
    })
    isolated.monkeypatch.setattr(harness_view, "_profile_snapshot", lambda: {"codex": {"name": "Codex"}})
    isolated.monkeypatch.setattr(harness_view, "_build",
                                 lambda *a: pytest.fail("untrusted force must not build private snapshot"))
    for suffix in ["", "&force=1"]:
        code, _, data = http_entry.request("GET", "/api/legion/harnesses?uid=invited-test" + suffix,
                                          authenticated=False)
        assert code == 200
        assert secret not in json.dumps(data)
        assert all(row["readiness"]["last_used"] is None for row in data["harnesses"])
    _, cookie, _ = signup(http_entry)
    code, _, data = http_entry.request("GET", "/api/legion/harnesses?force=1", authenticated=False,
                                      extra={"Cookie": cookie})
    assert code == 200 and secret not in json.dumps(data)


def test_account_store_failure_does_not_disable_legacy_owner(http_entry, isolated):
    path = isolated.root / "secrets/accounts"
    path.mkdir(mode=0o755)
    path.chmod(0o755)
    code, _, data = http_entry.request("GET", "/api/legion/auth/status")
    assert code == 200 and data["user_id"] == "invited-test"
    assert data["account_settings_available"] is False
    assert data["execution_enabled"] is True
