"""个人设备登记回归；复用公开源函数，不启动 Docker 或远程连接。"""
import hashlib
import importlib
import json
import os
from pathlib import Path
import stat
from unittest import mock

import pytest

SERVICE = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _pin_isolation_env(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SERVICE))
    monkeypatch.setenv("MEYO_LEGION_DIR", str(tmp_path / "legion"))
    monkeypatch.setenv("LEGION_HOME_DIR", str(tmp_path / "home"))
    monkeypatch.delenv("LEGION_BILLD_SELF_HOSTED_ORIGIN", raising=False)
    monkeypatch.delenv("MEYO_LEGION_UID", raising=False)
    monkeypatch.delenv("MEYO_USER_ID", raising=False)


def _personal_device_file(uid):
    return Path(os.environ["MEYO_LEGION_DIR"]) / "personal-devices" / hashlib.sha256(uid.encode()).hexdigest() / "devices.json"


def test_personal_device_auth_and_input():
    import cloud_desktop as cd
    for auth in (False, None, 1, "true", [], {}):
        assert cd.register_personal_device("alice", "PC", "ABC", authorized=auth) == {"ok": False, "error": "authorization_required"}
    for fields in ({"name": ""}, {"name": "x" * 81}, {"name": 5},
                   {"device_code": "ab"}, {"device_code": "a" * 81},
                   {"device_code": "abc?password=x"}, {"device_code": "ＡBC"},
                   {"device_code": "ab\tc"}, {"device_code": None},
                   {"platform": "ios"}, {"platform": []}, {"provider": "https://evil"},
                   {"provider": []}, {"uid": ""}, {"uid": None}):
        args = {"uid": "alice", "name": "PC", "device_code": "ABC", "authorized": True, **fields}
        assert cd.register_personal_device(**args)["ok"] is False
    assert not _personal_device_file("alice").exists()
    assert cd.remove_personal_device("alice", "../bad")["error"] == "invalid_device_id"


def test_personal_device_persist_duplicates_remove():
    import cloud_desktop as cd
    first = cd.register_personal_device("alice", " My PC ", "AB C_1-", "macos", authorized=True)["device"]
    assert first["name"] == "My PC" and first["device_code"] == "ABC_1-"
    assert first["state"] == "registered" and first["connection_state"] == "not_observed"
    assert first["agent_control_available"] is False
    stored = json.loads(_personal_device_file("alice").read_text())
    assert stored["owner"] == "alice" and "viewer_url" not in stored["devices"][0]
    assert "password" not in json.dumps(stored)
    cd = importlib.reload(cd)
    assert cd.personal_devices("alice")["devices"] == [first]
    updated = cd.register_personal_device("alice", "Renamed", "ABC_1-", authorized=True)["device"]
    assert updated["id"] == first["id"] and updated["created_at"] == first["created_at"]
    assert cd.personal_devices("alice")["total"] == 1
    result = cd.remove_personal_device("alice", first["id"])
    assert result["ok"] and result["removed"] and result["remote_session_terminated"] is False
    assert cd.remove_personal_device("alice", first["id"])["removed"] is False
    assert cd.personal_devices("alice")["total"] == 0


def test_personal_device_exact_owner_and_dynamic_root(monkeypatch, tmp_path):
    import cloud_desktop as cd
    identities = ("a/b", "a_b", "alice", " alice ", "用户")
    for uid in identities:
        assert cd.register_personal_device(uid, uid, "ABC", authorized=True)["ok"]
    assert len({_personal_device_file(uid) for uid in identities}) == len(identities)
    row = cd.personal_devices("alice")["devices"][0]
    assert cd.remove_personal_device("a_b", row["id"])["removed"] is False
    file = _personal_device_file("alice")
    data = json.loads(file.read_text())
    data["owner"] = " alice "
    file.write_text(json.dumps(data))
    for result in (cd.personal_devices("alice"), cd.register_personal_device("alice", "PC", "DEF", authorized=True), cd.remove_personal_device("alice", row["id"])):
        assert result == {"ok": False, "error": "storage_owner_mismatch"}
    monkeypatch.setenv("MEYO_LEGION_DIR", str(tmp_path / "another"))
    assert cd.personal_devices("alice")["total"] == 0


def test_personal_device_safe_providers(monkeypatch):
    import cloud_desktop as cd
    official = cd.personal_devices("alice")["providers"]
    assert official[0]["mode"] == "official_pro_manual"
    for origin in ("", "http://desk.example", "https://u:p@desk.example", "https://desk.example/",
                   "https://desk.example/path", "https://desk.example?password=x", "https://desk.example?",
                   "https://desk.example#", "https://desk.example/#/remote", "https://", "https://desk.example:99999",
                   "https://desk.example:0", " https://desk.example", "https://desk.example\n", "https://desk.example\\evil"):
        monkeypatch.setenv("LEGION_BILLD_SELF_HOSTED_ORIGIN", origin)
        assert cd.personal_devices("alice")["providers"] == official
        assert cd.register_personal_device("alice", "PC", "ABC", provider="self_hosted", authorized=True)["error"] == "provider_unavailable"
    monkeypatch.setenv("LEGION_BILLD_SELF_HOSTED_ORIGIN", "https://desk.example:8443")
    result = cd.register_personal_device("alice", "PC", "ABC", provider="self_hosted", authorized=True)
    assert result["device"]["viewer_url"] == "https://desk.example:8443/#/remote"
    monkeypatch.delenv("LEGION_BILLD_SELF_HOSTED_ORIGIN")
    assert cd.personal_devices("alice")["devices"][0]["viewer_url"] == ""


def test_personal_device_private_files_and_no_network(monkeypatch):
    import cloud_desktop as cd
    def forbidden(*args, **kwargs):
        raise AssertionError("network/docker/desktop must not be called")
    monkeypatch.setattr(cd.urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(cd, "_docker_env", forbidden)
    monkeypatch.setattr(cd, "_cua_uid", forbidden)
    monkeypatch.setattr(cd, "status", forbidden)
    monkeypatch.setattr(cd.subprocess, "run", forbidden)
    row = cd.register_personal_device("alice", "PC", "ABC", authorized=True)["device"]
    file = _personal_device_file("alice")
    for directory in (file.parent, file.parent.parent):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for item in (file, file.parent / ".lock"):
        assert stat.S_IMODE(item.stat().st_mode) == 0o600
    data = json.loads(file.read_text())
    data["devices"][0].update(password="fixture-only", viewer_url="https://evil", state="connected", agent_control_available=True)
    file.write_text(json.dumps(data))
    result = cd.personal_devices("alice")["devices"][0]
    assert "password" not in result and result["viewer_url"] == row["viewer_url"]
    assert result["state"] == "registered" and result["agent_control_available"] is False
    assert cd.remove_personal_device("alice", row["id"])["ok"]


@pytest.mark.parametrize("kind", ["base", "user", "file", "lock", "fifo", "hardlink", "lock_fifo", "lock_hardlink"])
def test_personal_device_symlinks_and_nonregular(monkeypatch, tmp_path, kind):
    import cloud_desktop as cd
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "fixture"
    target.write_text("do not touch")
    root = tmp_path / kind
    monkeypatch.setenv("MEYO_LEGION_DIR", str(root))
    file = _personal_device_file("alice")
    if kind == "base":
        root.mkdir()
        (root / "personal-devices").symlink_to(outside, target_is_directory=True)
    elif kind == "user":
        file.parent.parent.mkdir(parents=True)
        file.parent.symlink_to(outside, target_is_directory=True)
    else:
        file.parent.mkdir(parents=True)
        destination = file.parent / ".lock" if kind.startswith("lock") else file
        if "fifo" in kind:
            os.mkfifo(destination)
        elif "hardlink" in kind:
            os.link(target, destination)
        else:
            destination.symlink_to(target)
    assert cd.personal_devices("alice")["ok"] is False
    assert cd.register_personal_device("alice", "PC", "ABC", authorized=True)["ok"] is False
    assert cd.remove_personal_device("alice", "a" * 32)["ok"] is False
    assert target.read_text() == "do not touch"
    assert not (outside / "devices.json").exists()


def test_personal_device_storage_limits_and_failure_cleanup(monkeypatch):
    import cloud_desktop as cd
    for number in range(50):
        assert cd.register_personal_device("alice", "PC", f"dev-{number}", authorized=True)["ok"]
    assert cd.register_personal_device("alice", "PC", "extra", authorized=True)["error"] == "device_limit"
    assert cd.register_personal_device("alice", "renamed", "dev-0", authorized=True)["ok"]
    file = _personal_device_file("alice")
    original = file.read_bytes()
    with mock.patch.object(cd.os, "replace", side_effect=OSError("fixture-path")):
        assert cd.register_personal_device("alice", "bad", "dev-0", authorized=True) == {"ok": False, "error": "storage_error"}
    assert file.read_bytes() == original and not list(file.parent.glob("*.tmp"))
    file.write_bytes(b"x" * (128 * 1024 + 1))
    assert cd.personal_devices("alice")["error"] == "storage_too_large"
    file.write_text("not json")
    assert cd.personal_devices("alice")["error"] == "storage_invalid"
    monkeypatch.setenv("MEYO_LEGION_DIR", "~unresolved/private")
    assert cd.personal_devices("alice") == {"ok": False, "error": "storage_unresolved"}


def test_personal_device_concurrent_writes():
    from concurrent.futures import ThreadPoolExecutor
    import cloud_desktop as cd
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda n: cd.register_personal_device("thread-user", "PC", f"dev-{n}", authorized=True), range(16)))
    assert all(row["ok"] for row in results)
    assert cd.personal_devices("thread-user")["total"] == 16
