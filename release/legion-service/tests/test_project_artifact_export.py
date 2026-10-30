"""Real temporary-file export, no models/devices or original task data."""
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import zipfile
from types import SimpleNamespace
from urllib.parse import unquote
from urllib.request import urlopen

import pytest

from test_artifact_archive_safety import archive_bytes


SERVICE = Path(__file__).resolve().parents[1]
TEAM, TASK, OWNER, SESSION = "__ut_export", "maker", "owner-a", "__ut_session"


@pytest.fixture(autouse=True)
def case(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith(("MEYO_", "LEGION_", "AIGC_", "OPENAI_")):
            monkeypatch.delenv(name)
    for name, leaf in (
        ("HOME", "home"), ("CODEX_HOME", "codex-home"), ("TMPDIR", "native-tmp"),
        ("MEYO_LEGION_DIR", "data"), ("LEGION_HOME_DIR", "legion"),
        ("LEGION_PROJECT_CONTEXT_DIR", "projects"), ("LEGION_ARTIFACT_STORE_DIR", "store"),
        ("LEGION_SESSIONS_DIR", "sessions"), ("LEGION_TEAM_DIR", "teams"),
    ):
        (tmp_path / leaf).mkdir()
        monkeypatch.setenv(name, str(tmp_path / leaf))
    monkeypatch.syspath_prepend(str(SERVICE))
    ad = importlib.import_module("artifact_delivery")
    pc = importlib.import_module("project_context")
    store = importlib.import_module("artifact_store")
    monkeypatch.setattr(ad, "OUTPUTS", str(tmp_path / "service-outputs"))
    monkeypatch.setattr(store, "ROOT", tmp_path / "store")
    monkeypatch.setattr(pc, "ROOT", tmp_path / "projects")
    monkeypatch.setattr(ad, "_git_changed_files", lambda _root: None)

    def forbidden(*a, **kw):
        raise AssertionError("no subprocess or external network in export tests")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    real_connect = socket.socket.connect

    def loopback_only(sock, address):
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return real_connect(sock, address)
        return forbidden()

    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    # A legitimate project may be under an ancestor called state or outputs.
    work = tmp_path / "state" / "outputs" / "custom-project"
    work.mkdir(parents=True)
    tokens = tmp_path / "control" / "personal-tokens.json"
    # Control is a sibling of every native write root, not beneath an
    # inherited system TMPDIR containing pytest's entire temporary tree.
    tokens.parent.mkdir(mode=0o700)
    tokens.write_text(json.dumps({"fixture-personal-token": {
        "user": OWNER, "project_grants": [{"root": str(work), "export_paths": ["."]}]}}))
    tokens.chmod(0o600)
    monkeypatch.setenv("LEGION_PERSONAL_TOKENS_FILE", str(tokens))
    pc.initialize(TEAM, "fixture objective",
                  {"tasks": {TASK: {"task": "produce a document", "assigned_harness": "__ut_member"}},
                   "project_context": {"repo_root": str(work)}},
                  mode="orchestrate", user_id=OWNER, conversation_id=SESSION)
    return SimpleNamespace(ad=ad, pc=pc, store=store, work=work, root=tmp_path,
                           mp=monkeypatch, tokens=tokens)


def export(case, paths, **kw):
    return case.ad.export_project_artifacts(
        paths, **{"team_id": TEAM, "task_id": TASK, "user_id": OWNER,
                  "conversation_id": SESSION, **kw})


def write(case, name, body=b"real document\n"):
    path = case.work / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def test_unmapped_explicit_workdir_never_scans_another_task(case):
    other = case.root / "data/outputs/another-task/new.txt"
    other.parent.mkdir(parents=True)
    other.write_text("not this task")
    write(case, "mine.txt")
    notes = []
    result = case.ad.scan_new_files(0, workdir=str(case.work), safety_observations=notes)
    assert result == [], "an explicit unmapped project must not fall back to shared roots"
    assert notes[0]["reason"] == "artifact_workdir_unmapped"
    assert notes[0]["downloadable"] is False and notes[0]["file_preserved"] is True


def test_same_owner_export_preserves_exact_bytes_and_lineage(case):
    source = write(case, "deliverable.txt", "原字节，不转码\r\n".encode())
    body = source.read_bytes()
    out = export(case, [str(source)])
    assert out["artifact_safety_observations"] == []
    row, = out["artifacts"]
    assert Path(row["path"]).read_bytes() == body == source.read_bytes()
    assert row["url"].startswith("/home-outputs/")
    assert (case.root / "data/outputs" / unquote(row["url"].removeprefix("/home-outputs/"))).resolve() == Path(row["path"])
    assert row["sha256"] == hashlib.sha256(body).hexdigest()
    assert row["provenance"]["source_path"] == str(source)
    assert row["provenance"]["sha256"] == row["sha256"]
    assert (row["user_id"], row["team_id"], row["task_id"]) == (OWNER, TEAM, TASK)
    assert row["lineage"]["task"]["id"] == TASK
    manifest = json.loads(case.store.read_text(out["export_manifest"]))
    assert manifest["user_id"] == OWNER and manifest["task_id"] == TASK
    assert manifest["files"][0]["sha256"] == row["sha256"]
    source.write_text("editable original after export")
    assert Path(row["path"]).read_bytes() == body, "export must be a snapshot, not a symlink"


@pytest.mark.parametrize("override", [
    {"user_id": "another-owner"}, {"user_id": ""}, {"team_id": "unknown-team"},
    {"task_id": "not-this-task"}, {"conversation_id": "other-session"},
])
def test_canonical_owner_session_and_task_required(case, override):
    source = write(case, "mine.txt")
    out = export(case, [str(source)], **override)
    assert not out["artifacts"]
    assert out["artifact_safety_observations"]
    assert not list((case.root / "data/outputs").rglob("mine.txt"))


def test_shell_workdir_env_does_not_authorize_another_directory(case):
    outside = case.root / "outside.txt"
    outside.write_text("not the authorized project")
    case.mp.setenv("MEYO_WORKDIR", str(case.root))
    case.mp.setenv("MEYO_PROJECT_CONTEXT_JSON", json.dumps({
        "repo_root": str(case.root), "user_id": OWNER, "tasks": {TASK: {}}}))
    out = export(case, [str(outside)])
    assert not out["artifacts"]
    assert out["artifact_safety_observations"][0]["reason"] == "artifact_path_outside_project"


@pytest.mark.parametrize("name", [
    ".env", ".git/config", "state/result.json", "logs/trace.jsonl",
    "state.json", "page.html.progress.json", "deepagents-messages.json",
])
def test_explicit_internal_state_is_not_exportable(case, name):
    source = write(case, name)
    out = export(case, [str(source)])
    assert not out["artifacts"]
    assert out["artifact_safety_observations"][0]["reason"] == "artifact_internal_path"


@pytest.mark.parametrize("directory_link", [False, True])
def test_symlink_escape_is_refused_without_reading_target(case, directory_link):
    outside = case.root / "other"
    outside.mkdir()
    secret = outside / "file.txt"
    secret.write_text("do not read")
    if directory_link:
        (case.work / "alias").symlink_to(outside, target_is_directory=True)
        candidate = case.work / "alias/file.txt"
    else:
        candidate = case.work / "alias.txt"
        candidate.symlink_to(secret)
    read = case.ad._content_safety._inspect

    def inspect(stream, name=""):
        assert os.fstat(stream.fileno()).st_ino != secret.stat().st_ino
        return read(stream, name)

    case.mp.setattr(case.ad._content_safety, "_inspect", inspect)
    out = export(case, [str(candidate)])
    assert not out["artifacts"]
    assert out["artifact_safety_observations"][0]["reason"] == "artifact_symlink_refused"


def test_sensitive_content_fails_closed_without_publication(case):
    source = write(case, "notes.txt", b'api_key="live-fixture-credential-7a93c81f56dd"\n')
    out = export(case, [str(source)])
    assert not out["artifacts"]
    assert out["artifact_safety_observations"][0]["reason"] == "artifact_sensitive_content"
    assert "7a93c81f56dd" not in json.dumps(out)
    assert not list((case.root / "data/outputs").rglob("notes.txt"))


@contextmanager
def served(case, artifacts):
    """Own loopback fixture using existing route, byte guard and exact file grants."""
    routes = {a["url"]: Path(a["path"]) for a in artifacts}
    safety = case.ad._content_safety

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            file = routes.get(self.path)
            if file is None:
                self.send_error(403)
                return
            body = safety.read_public_file(str(file))
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_html_directory_snapshot_keeps_relative_assets_and_all_file_grants(case):
    html = b'<link rel="stylesheet" href="assets/style.css"><script src="assets/app.js"></script>'
    files = {
        "site/index.html": html,
        "site/assets/style.css": b'body { background: url("icon.svg"); }',
        "site/assets/app.js": b'fetch("data.json");',
        "site/assets/icon.svg": b'<svg xmlns="http://www.w3.org/2000/svg"/>',
        "site/data.json": b'{"value": 3}',
    }
    for name, body in files.items():
        write(case, name, body)
    write(case, "site/index.html.progress.json", b'{"runtime":"private"}')
    out = export(case, [str(case.work / "site")])
    rows = out["artifacts"]
    assert len(rows) == len(files)
    by_source = {a["provenance"]["source_path"]: a for a in rows}
    entry = by_source[str(case.work / "site/index.html")]
    # Relative layout and grants survive a fresh HTTP fixture, no live daemon.
    with served(case, rows) as origin:
        for name, raw in files.items():
            row = by_source[str(case.work / name)]
            assert row["url"] == entry["url"].removesuffix("index.html") + name.removeprefix("site/")
            with urlopen(origin + row["url"], timeout=2) as response:
                assert response.read() == raw
    assert any(n["reason"] == "artifact_internal_path" for n in out["artifact_safety_observations"])


def test_identical_export_is_stable_new_bytes_make_new_version(case):
    source = write(case, "item.txt", b"version one")
    first = export(case, [str(source)])["artifacts"]
    second = export(case, [str(source)])["artifacts"]
    assert first[0]["path"] == second[0]["path"]
    source.write_bytes(b"version two")
    third = export(case, [str(source)])["artifacts"]
    assert third[0]["path"] != first[0]["path"]
    assert Path(first[0]["path"]).read_bytes() == b"version one"


def test_default_served_root_scan_remains_compatible(case):
    work = case.root / "data/outputs/default-task"
    work.mkdir(parents=True)
    file = work / "legacy.txt"
    file.write_text("legacy result")
    rows = case.ad.scan_new_files(0, workdir=str(work))
    assert [r["path"] for r in rows] == [str(file)]
    assert rows[0]["url"] == "/home-outputs/default-task/legacy.txt"


def test_no_workdir_legacy_shared_scan_remains_available(case):
    directory = case.root / "data/outputs/legacy"
    directory.mkdir(parents=True)
    file = directory / "file.txt"
    file.write_text("legacy")
    assert [r["path"] for r in case.ad.scan_new_files(0)] == [str(file)]


def test_explicit_unmapped_root_does_not_enable_extra_or_shared_fallback(case):
    other = case.root / "data/outputs/other"
    other.mkdir(parents=True)
    (other / "other.txt").write_text("another task")
    assert case.ad.scan_new_files(
        0, workdir=str(case.work), extra_roots=[str(other)], scan_shared_roots=True) == []


def test_partial_snapshot_write_failure_cleans_only_owned_staging(case):
    write(case, "site/first.txt")
    write(case, "site/second.txt")
    writer = case.store._put_bytes_at
    calls = []

    def fail_second(root, parts, raw, name, **kw):
        if kw.get("_root_fd") is not None:
            calls.append(name)
            if len(calls) == 2:
                raise OSError("fixture write failure")
        return writer(root, parts, raw, name, **kw)

    case.mp.setattr(case.store, "_put_bytes_at", fail_second)
    out = export(case, ["site"])
    assert calls == ["first.txt", "second.txt"]
    assert not out["artifacts"]
    assert out["artifact_safety_observations"]
    assert not [p for p in (case.root / "data/outputs").rglob("*") if p.is_file()]
    assert not list((case.root / "data/outputs").rglob(".export-*"))
    assert (case.work / "site/first.txt").is_file()


def test_corrupt_existing_snapshot_is_not_overwritten_or_returned(case):
    original = write(case, "item.txt")
    first = export(case, ["item.txt"])
    exported = Path(first["artifacts"][0]["path"])
    exported.write_text("externally corrupted")
    again = export(case, ["item.txt"])
    assert not again["artifacts"]
    assert exported.read_text() == "externally corrupted", "do not silently repair or bless a changed version"
    assert original.read_bytes() == b"real document\n"


def test_export_storage_symlink_cannot_redirect_writes(case):
    outside = case.root / "unrelated"
    outside.mkdir()
    root = case.root / "data/outputs"
    root.mkdir()
    (root / "exports").symlink_to(outside, target_is_directory=True)
    write(case, "item.txt")
    out = export(case, ["item.txt"])
    assert not out["artifacts"]
    assert list(outside.iterdir()) == []


def test_source_mutation_while_collecting_bundle_is_refused(case):
    first = write(case, "site/a.txt", b"version one")
    write(case, "site/b.txt")
    inspect = case.ad._content_safety._inspect

    def mutate(stream, name=""):
        result = inspect(stream, name)
        if name == "site/b.txt":
            first.write_bytes(b"version two")
        return result

    case.mp.setattr(case.ad._content_safety, "_inspect", mutate)
    out = export(case, ["site"])
    assert not out["artifacts"]
    assert out["artifact_safety_observations"][0]["reason"] == "artifact_source_changed"
    assert not list((case.root / "data/outputs").rglob("a.txt"))


def test_native_sidecars_are_skipped_but_secret_asset_aborts_whole_bundle(case):
    write(case, "site/index.html", b'<script src="assets/data.js"></script>')
    write(case, "site/.env", b"never publish internal files")
    write(case, "site/assets/data.js", b'const api_key = "live-fixture-credential-7a93c81f56dd";')
    out = export(case, ["site"])
    assert not out["artifacts"]
    assert {n["reason"] for n in out["artifact_safety_observations"]} == {
        "artifact_internal_path", "artifact_sensitive_content"}
    assert not list((case.root / "data/outputs").rglob("index.html"))


def test_two_owners_identical_names_and_bytes_have_separate_export_paths(case):
    write(case, "file.txt")
    first = export(case, ["file.txt"])["artifacts"][0]
    case.pc.initialize(
        "another-team", "another objective",
        {"tasks": {TASK: "make something"}, "project_context": {"repo_root": str(case.work)}},
        mode="orchestrate", user_id="owner-b", conversation_id="session-b")
    records = json.loads(case.tokens.read_text())
    records["other-fixture-token"] = {
        "user": "owner-b", "project_grants": [{"root": str(case.work), "export_paths": ["."]}]}
    case.tokens.write_text(json.dumps(records))
    second = export(case, ["file.txt"], team_id="another-team",
                    user_id="owner-b", conversation_id="session-b")["artifacts"][0]
    assert first["sha256"] == second["sha256"]
    assert first["path"] != second["path"]


def test_existing_private_byte_store_contract_is_unchanged(case):
    store = case.store
    reference = store.put_bytes("team/one", "task/one", b"private content", name="note.md")
    assert Path(reference["path"]).parent == store.ROOT / "team_one" / "task_one"
    assert store.read_text(reference) == "private content"
    assert reference == store.put_bytes("team/one", "task/one", b"private content", name="note.md")
    escaped = case.root / "not-store"
    escaped.mkdir()
    (store.ROOT / "evil").symlink_to(escaped, target_is_directory=True)
    with pytest.raises(OSError):
        store.put_bytes("evil", "task", b"never write here")
    assert list(escaped.iterdir()) == []


def test_returned_resource_rows_use_existing_session_file_authorization(case):
    write(case, "site/index.html", b'<script src="app.js"></script>')
    write(case, "site/app.js", b'console.log("fixture");')
    rows = export(case, ["site"])["artifacts"]
    sessions = importlib.import_module("sessions")
    identity = importlib.import_module("personal_identity")
    sessions.save_turn(SESSION, "fixture", {"results": [{"artifacts": rows}]},
                       mode="single", user_id=OWNER)
    for row in rows:
        assert identity.file_allowed(row["path"], OWNER, SESSION)
        assert not identity.file_allowed(row["path"], "other-owner", SESSION)
    source = case.work / "site/app.js"
    assert not identity.file_allowed(str(source), OWNER, SESSION), "source workdir itself was not granted"


@pytest.mark.parametrize("kind", ["zip", "tar", "tgz", "zip-tgz", "tgz-zip"])
@pytest.mark.parametrize("entry", [".git/config", "state/turn.json", "../outside.txt", "logs/trace.jsonl"])
def test_archive_cannot_smuggle_internal_or_escaping_members(case, entry, kind):
    source = write(case, "bundle." + kind.split("-")[0],
                   archive_bytes(kind, [(entry, b"nonsecret but private state")]))
    result = export(case, [str(source)])
    assert not result["artifacts"]
    assert result["artifact_safety_observations"][0]["reason"] == "artifact_internal_path"


@pytest.mark.parametrize("kind", ["zip", "tar", "tgz", "zip-tgz", "tgz-zip"])
def test_ordinary_selected_zip_keeps_original_archive_bytes(case, kind):
    source = write(case, "bundle." + kind.split("-")[0], archive_bytes(kind, [
        ("index.html", b"public file"), ("assets/data.json", b'{"n": 1}')]))
    row, = export(case, [str(source)])["artifacts"]
    assert Path(row["path"]).read_bytes() == source.read_bytes()
    assert row["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert case.ad._content_safety.read_public_file(row["path"]) == source.read_bytes()


def test_tgz_uses_existing_task_output_scan_without_new_authorization(case):
    work = case.root / "data/outputs/__ut_tgz_task"
    work.mkdir(parents=True)
    source = work / "package.tgz"
    source.write_bytes(archive_bytes("tgz", [("package/index.js", b'export const n = 1;')]))
    observations = []
    row, = case.ad.scan_new_files(0, workdir=str(work), safety_observations=observations)
    assert observations == []
    assert row["path"] == str(source)
    assert row["url"] == "/home-outputs/__ut_tgz_task/package.tgz"
    assert row["provenance"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert case.ad._content_safety.read_public_file(row["path"]) == source.read_bytes()


@pytest.mark.parametrize("entry", ["package/.git/config", "package/state/task.json",
                                  "package/logs/run.jsonl"])
def test_tgz_task_output_scan_withholds_private_members_and_preserves_original(case, entry):
    work = case.root / "data/outputs/__ut_tgz_task"
    work.mkdir(parents=True)
    source = work / "package.tgz"
    raw = archive_bytes("tgz", [(entry, b"nonsecret but private state")])
    source.write_bytes(raw)
    observations = []
    assert case.ad.scan_new_files(0, workdir=str(work), safety_observations=observations) == []
    assert observations == [{"name": "package.tgz", "reason": "artifact_internal_path",
                             "scan_status": "not_scanned", "file_preserved": True,
                             "downloadable": False}]
    assert source.read_bytes() == raw
    with pytest.raises(case.ad._content_safety.ArtifactUnsafe, match="^artifact_internal_path$"):
        case.ad._content_safety.read_public_file(str(source))


def test_selected_project_root_keeps_site_but_skips_git_and_runtime_state(case):
    write(case, "index.html", b'<script src="assets/app.js"></script>')
    write(case, "assets/app.js", b'console.log("original site");')
    write(case, ".git/config", b"internal git metadata")
    write(case, "index.html.progress.json", b"internal progress")
    result = export(case, ["."])
    assert {row["name"] for row in result["artifacts"]} == {"index.html", "app.js"}
    assert {note["reason"] for note in result["artifact_safety_observations"]} == {"artifact_internal_path"}


def test_office_relationship_descriptor_is_not_private_runtime_state(case):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("_rels/.rels", "<Relationships/>")
        archive.writestr("word/document.xml", "<document>fixture</document>")
    write(case, "report.docx", stream.getvalue())
    row, = export(case, ["report.docx"])["artifacts"]
    assert Path(row["path"]).read_bytes() == stream.getvalue()


def test_manifest_write_failure_does_not_publish_partial_export(case):
    write(case, "file.txt")

    def fail_manifest(*a, **kw):
        raise OSError("fixture private store failure")

    case.mp.setattr(case.store, "put_bytes", fail_manifest)
    result = export(case, ["file.txt"])
    assert not result["artifacts"]
    assert not [p for p in (case.root / "data/outputs").rglob("*") if p.is_file()]


def test_new_asset_added_during_snapshot_requires_retry_not_incomplete_bundle(case):
    write(case, "site/index.html", b'<script src="assets/app.js"></script>')
    write(case, "site/assets/app.js")
    inspect = case.ad._content_safety._inspect

    def add_asset(stream, name=""):
        result = inspect(stream, name)
        if name == "site/index.html":
            write(case, "site/assets/late.js")
        return result

    case.mp.setattr(case.ad._content_safety, "_inspect", add_asset)
    result = export(case, ["site"])
    assert not result["artifacts"]
    assert result["artifact_safety_observations"][0]["reason"] == "artifact_source_changed"


def test_canonical_project_record_does_not_replace_operator_grant(case):
    write(case, "file.txt")
    case.tokens.write_text(json.dumps({"fixture-personal-token": {"user": OWNER}}))
    assert case.pc.load(TEAM)["repo_root"] == str(case.work)
    out = export(case, ["file.txt"])
    assert not out["artifacts"]
    assert out["artifact_safety_observations"][0]["reason"] == "artifact_project_not_authorized"


def test_subtree_export_grant_does_not_implicitly_allow_notes_or_whole_project(case):
    write(case, "site/index.html", b'<link rel="stylesheet" href="assets/style.css">')
    write(case, "site/assets/style.css", b"body {color: navy}")
    write(case, "notes/customer.txt", b"private ordinary text")
    case.tokens.write_text(json.dumps({"fixture-personal-token": {
        "user": OWNER, "project_grants": [{"root": str(case.work), "export_paths": ["site"]}]}}))
    for denied in (["."], ["notes/customer.txt"]):
        out = export(case, denied)
        assert not out["artifacts"]
        assert out["artifact_safety_observations"][0]["reason"] == "artifact_export_path_not_authorized"
    result = export(case, ["site"])
    assert {a["name"] for a in result["artifacts"]} == {"index.html", "style.css"}
    assert "customer.txt" not in repr(result)


def test_grant_revocation_between_collection_and_publish_is_effective(case):
    write(case, "file.txt")
    inspect = case.ad._content_safety._inspect

    def revoke(stream, name=""):
        result = inspect(stream, name)
        case.tokens.write_text(json.dumps({"fixture-personal-token": {"user": OWNER}}))
        return result

    case.mp.setattr(case.ad._content_safety, "_inspect", revoke)
    result = export(case, ["file.txt"])
    assert not result["artifacts"]
    assert result["artifact_safety_observations"][0]["reason"] == "artifact_project_not_authorized"
