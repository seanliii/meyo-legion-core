"""Expected SHA comes from authorized saved records, never mutable file bytes."""
from copy import deepcopy
import importlib
import os
from pathlib import Path

import pytest

from test_project_artifact_export import case, export, write, OWNER, SESSION, TEAM


@pytest.fixture(autouse=True)
def _native_control_isolation(case):
    # Reuse the export fixture's per-test runtime pins, including in a full
    # collection where another test supplied a parent system TMPDIR.
    for name in ("HOME", "CODEX_HOME", "TMPDIR"):
        native_root = Path(os.environ[name]).resolve()
        assert native_root.parent == case.root.resolve()
        assert not case.tokens.resolve().is_relative_to(native_root)


def save(case, rows, sid=SESSION, user=OWNER):
    sessions = importlib.import_module("sessions")
    sessions.save_turn(sid, "fixture", {"results": [{"artifacts": rows}]},
                       mode="single", user_id=user)


def exported(case):
    write(case, "page.html", b"<html>original version</html>")
    row, = export(case, ["page.html"])["artifacts"]
    return row


def test_registered_expected_hash_does_not_follow_later_file_mutation(case):
    row = exported(case)
    save(case, [row])
    identity = importlib.import_module("personal_identity")
    destination = Path(row["path"])
    destination.write_bytes(b"<html>changed later</html>")

    def forbidden(*a, **kw):
        pytest.fail("expected SHA must not read/recompute from artifact bytes")

    case.mp.setattr(case.ad._content_safety, "read_public_file", forbidden)
    case.mp.setattr(case.ad._content_safety, "check_file", forbidden)
    path_open = Path.open

    def no_artifact_read(path, *a, **kw):
        if path == destination:
            forbidden()
        return path_open(path, *a, **kw)

    case.mp.setattr(Path, "open", no_artifact_read)
    assert identity.expected_export_hash(row["path"], OWNER, SESSION) == row["sha256"]
    assert identity.expected_export_hash(row["path"], OWNER) == row["sha256"]
    assert identity.expected_export_hash(row["path"], "other-owner", SESSION) is None


@pytest.mark.parametrize("invalid", ["missing_snapshot", "wrong_snapshot", "missing_sha",
                                     "invalid_sha", "contradictory_sha"])
def test_unverifiable_export_registration_returns_no_expected_hash(case, invalid):
    row = exported(case)
    registered = deepcopy(row)
    if invalid == "missing_snapshot":
        registered.pop("export_snapshot_id")
    elif invalid == "wrong_snapshot":
        registered["export_snapshot_id"] = "0" * 64
    elif invalid == "missing_sha":
        registered.pop("sha256")
        registered["provenance"].pop("sha256")
    elif invalid == "invalid_sha":
        registered["sha256"] = "not-a-sha"
    else:
        registered["provenance"]["sha256"] = "0" * 64
    save(case, [registered])
    identity = importlib.import_module("personal_identity")
    assert identity.expected_export_hash(row["path"], OWNER, SESSION) is None


def test_conflicting_saved_hashes_for_same_export_path_fail_closed(case):
    row = exported(case)
    save(case, [row])
    other = deepcopy(row)
    other["sha256"] = other["provenance"]["sha256"] = "0" * 64
    save(case, [other])
    identity = importlib.import_module("personal_identity")
    assert identity.expected_export_hash(row["path"], OWNER, SESSION) is None


def test_live_export_event_after_first_page_has_expected_hash(case):
    row = exported(case)
    sessions = importlib.import_module("sessions")
    teams = importlib.import_module("team_state")
    identity = importlib.import_module("personal_identity")
    teams.ensure(TEAM, "fixture", {}, {}, user_id=OWNER, conversation_id=SESSION)
    sessions.save_turn(SESSION, "fixture", {"team_id": TEAM, "status": "running", "results": []},
                       mode="single", user_id=OWNER)
    for _ in range(501):
        teams.append_event(TEAM, {"t": "heartbeat"})
    event = {"t": "artifact_ready", "url": row["url"],
             "export_snapshot_id": row["export_snapshot_id"],
             "provenance": row["provenance"]}
    teams.append_event(TEAM, event)
    assert identity.expected_export_hash(row["path"], OWNER, SESSION) == row["sha256"]
    assert identity.expected_export_hash(row["path"], "other-owner", SESSION) is None
    assert identity.expected_export_hash(str(Path(row["path"]).with_name("unregistered.html")),
                                         OWNER, SESSION) is None


def test_explicit_session_cannot_borrow_same_owners_other_session_hash(case):
    row = exported(case)
    save(case, [], sid=SESSION)
    save(case, [row], sid="__ut_other_session")
    identity = importlib.import_module("personal_identity")
    assert identity.expected_export_hash(row["path"], OWNER, SESSION) is None
    assert identity.expected_export_hash(row["path"], OWNER, "__ut_other_session") == row["sha256"]


def test_non_export_legacy_file_has_no_expected_snapshot_hash(case):
    file = case.root / "data/outputs/legacy.txt"
    file.parent.mkdir(parents=True)
    file.write_text("legacy")
    save(case, [{"path": str(file), "sha256": "0" * 64}])
    identity = importlib.import_module("personal_identity")
    assert identity.file_allowed(str(file), OWNER, SESSION)
    assert identity.expected_export_hash(str(file), OWNER, SESSION) is None


def test_real_http_serves_registered_bytes_and_refuses_changed_snapshot(case):
    import http.client
    import threading
    row = exported(case)
    save(case, [row])
    server = importlib.import_module("server")
    case.mp.setattr(server.LEGION, "_swr_bg_refresh", lambda *a, **kw: None)
    case.mp.setattr(server.LEGION, "_autonomy_matrix", lambda: {})
    case.mp.setattr(server, "_PUBLIC_ORIGIN", None)
    case.mp.setattr(server, "_REMOTE_BIND_OK", False)
    listener = server.LegionServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=listener.serve_forever,
                              kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()

    def get():
        connection = http.client.HTTPConnection("127.0.0.1", listener.server_port, timeout=3)
        try:
            connection.request("GET", row["url"] + "?download=1",
                               headers={"Authorization": "Bearer fixture-personal-token"})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    try:
        original = Path(row["path"]).read_bytes()
        assert get() == (200, original)
        Path(row["path"]).write_bytes(b"<html>unexpected later version</html>")
        status, body = get()
        assert status == 409 and b"artifact_version_mismatch" in body
        assert b"unexpected later version" not in body
    finally:
        listener.shutdown()
        listener.server_close()
        thread.join(2)
