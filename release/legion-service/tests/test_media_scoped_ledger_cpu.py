"""Workspace ledger / read-only legacy cache. No network, model or native worker."""
from concurrent.futures import ThreadPoolExecutor
import errno
import importlib
import json
import multiprocessing
import os
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import pytest


SERVICE = Path(__file__).resolve().parents[1]
TOOL = "media:gen_image"
PARAMS = {"prompt": "CPU-only fixture", "size": "1024x1024"}


@pytest.fixture(autouse=True)
def case(tmp_path, monkeypatch):
    tmp_path = tmp_path.resolve()
    for key in tuple(os.environ):
        if key.startswith(("MEYO_", "LEGION_", "OPENAI_", "AIGC_", "CODEX_")):
            monkeypatch.delenv(key)
    for key in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "TMPDIR"):
        folder = tmp_path / key
        folder.mkdir(mode=0o700)
        monkeypatch.setenv(key, str(folder))
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.syspath_prepend(str(SERVICE))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("network/model/native spawn forbidden in ledger CPU tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    ledger = importlib.import_module("side_effect_ledger")
    idem = importlib.import_module("legion_idempotency")
    default = tmp_path / "default" / "side_effect_ledger.json"
    monkeypatch.setattr(ledger, "_PATH", default)
    workspace = tmp_path / "work"
    workspace.mkdir(mode=0o700)
    return SimpleNamespace(ledger=ledger, idem=idem, root=tmp_path, work=workspace,
                           path=workspace / ".media-effects.json", default=default,
                           patch=monkeypatch)


def _expired(case, *, path=None, safe_retry=False):
    case.patch.setattr(case.ledger.time, "time", lambda: 100.0)
    kwargs = {"path": path} if path is not None else {}
    first = case.ledger.begin(TOOL, PARAMS, "same-order", lease_s=1,
                              safe_retry=safe_retry, **kwargs)
    case.patch.setattr(case.ledger.time, "time", lambda: 102.0)
    return first


def _legacy(case, environment, *, result=None, params=None, qhash="order"):
    root = Path(environment.get("MEYO_LEGION_DIR")
                or str(Path(environment.get("MEYO_PERSONAL_STATE_DIR")
                            or environment["LEGION_PERSONAL_STATE_DIR"]) / "data"))
    directory = root / "idempotency"
    directory.mkdir(parents=True, mode=0o700)
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in qhash)[:80]
    path = directory / (safe + ".json")
    key = case.idem._key(TOOL, PARAMS if params is None else params)
    path.write_text(json.dumps({key: {
        "tool": "gen_image", "ts": 1,
        "result": result if result is not None else {"ok": True, "path": "old.png"},
    }}))
    return path


def _bound_env(case, owner="alice", **extra):
    return {"MEYO_SESSION_ID": "codex:order",
            "MEYO_LEGION_DIR": str(case.root / owner),
            "MEYO_WORKDIR": str(case.work), **extra}


def _claim_in_child(path, ready, start, results):
    import side_effect_ledger
    ready.put(True)
    if not start.wait(5):
        raise AssertionError("CPU ledger start barrier timed out")
    try:
        results.put(side_effect_ledger.begin(TOOL, PARAMS, "concurrent", path=path)["decision"])
    except Exception as exc:
        # Preserve the real syscall failure instead of hiding it behind Queue.Empty.
        cause = exc.__context__
        frame = cause.__traceback__ if cause is not None else None
        while frame is not None and frame.tb_next is not None:
            frame = frame.tb_next
        results.put({"error": type(exc).__name__, "code": str(exc),
                     "errno": getattr(cause, "errno", None),
                     "site": (f"{frame.tb_frame.f_code.co_name}:{frame.tb_lineno}"
                              if frame is not None else None)})


def test_default_expired_unsafe_is_atomically_unknown_not_reexecuted(case):
    first = _expired(case)
    later = case.ledger.begin(TOOL, PARAMS, "same-order", safe_retry=True)
    assert later["decision"] == "requires_reconciliation"
    row = json.loads(case.default.read_text())["effects"][first["effect_id"]]
    assert row["status"] == "unknown"
    assert row["attempts"] == 1 and row["lease_expires_at"] == 0


def test_scoped_lifecycle_preserves_default_api_and_global_path(case):
    first = case.ledger.begin(TOOL, PARAMS, "scope", path=case.path)
    assert first["decision"] == "execute" and first["attempt"] == 1
    out = {"ok": True, "path": "result.png"}
    assert case.ledger.finish(first["effect_id"], out, path=case.path,
                              attempt=first["attempt"])["ok"]
    reused = case.ledger.begin(TOOL, PARAMS, "scope", path=case.path)
    assert reused["decision"] == "reuse" and reused["result"] == out
    assert case.ledger.recover_expired(path=case.path) == []
    assert case.ledger._PATH == case.default and not case.default.parent.exists()
    old = case.ledger.begin(TOOL, PARAMS, "legacy-api")
    assert case.ledger.finish(old["effect_id"], {"ok": True})["ok"]
    assert case.ledger.begin(TOOL, PARAMS, "legacy-api")["decision"] == "reuse"


def test_scoped_expired_unsafe_needs_reconciliation_and_can_reuse_evidence(case):
    first = _expired(case, path=case.path)
    later = case.ledger.begin(TOOL, PARAMS, "same-order", path=case.path)
    assert later["decision"] == "requires_reconciliation"
    assert later["record"]["attempts"] == 1
    assert case.ledger.reconcile(first["effect_id"], "completed",
                                  {"ok": True, "path": "recovered.png"},
                                  path=case.path)["ok"]
    assert case.ledger.begin(TOOL, PARAMS, "same-order",
                              path=case.path)["decision"] == "reuse"


def test_safe_retry_new_attempt_rejects_late_finish_without_writing(case):
    first = _expired(case, path=case.path, safe_retry=True)
    second = case.ledger.begin(TOOL, PARAMS, "same-order", path=case.path,
                                safe_retry=True)
    assert second["decision"] == "execute" and second["attempt"] == 2
    before = case.path.read_bytes()
    late = case.ledger.finish(first["effect_id"], {"ok": True, "path": "stale.png"},
                               path=case.path, attempt=first["attempt"])
    assert late["ok"] is False and late["error"] == "stale_attempt"
    assert case.path.read_bytes() == before
    assert case.ledger.finish(second["effect_id"], {"ok": True}, path=case.path,
                              attempt=second["attempt"])["ok"]


@pytest.mark.parametrize("safe_retry", [False, True])
def test_uncertain_result_is_unknown_even_when_caller_requests_safe_retry(case, safe_retry):
    first = case.ledger.begin(TOOL, PARAMS, "uncertain", path=case.path,
                               safe_retry=safe_retry)
    final = case.ledger.finish(first["effect_id"], {"ok": False, "error": "lost reply"},
                               path=case.path, attempt=first["attempt"], uncertain=True)
    assert final["record"]["status"] == "unknown"
    assert case.ledger.begin(TOOL, PARAMS, "uncertain", path=case.path,
                              safe_retry=True)["decision"] == "requires_reconciliation"


@pytest.mark.parametrize("terminal", ["recovered", "reconciled", "completed"])
def test_attempt_cannot_overwrite_recovered_or_terminal_record(case, terminal):
    first = _expired(case, path=case.path)
    if terminal == "recovered":
        assert case.ledger.recover_expired(path=case.path)[0]["status"] == "unknown"
    elif terminal == "reconciled":
        case.ledger.reconcile(first["effect_id"], "cancelled", path=case.path)
    else:
        case.ledger.finish(first["effect_id"], {"ok": True}, path=case.path,
                           attempt=first["attempt"])
    before = case.path.read_bytes()
    late = case.ledger.finish(first["effect_id"], {"ok": False}, path=case.path,
                               attempt=first["attempt"])
    assert late["ok"] is False and late["error"] == "effect_not_running"
    assert case.path.read_bytes() == before


def test_recover_expired_uses_only_requested_path(case):
    _expired(case, path=case.path, safe_retry=True)
    other = case.work / ".other.json"
    case.ledger.begin(TOOL, PARAMS, "other", path=other)
    other_before = other.read_bytes()
    rows = case.ledger.recover_expired(now=102, path=case.path)
    assert len(rows) == 1 and rows[0]["status"] == "interrupted"
    assert other.read_bytes() == other_before and not case.default.exists()


def test_different_workspaces_and_threads_do_not_change_global_environment(case):
    paths = [case.root / owner / ".media-effects.json" for owner in ("alice", "bob")]
    for path in paths:
        path.parent.mkdir(mode=0o700)
    before = dict(os.environ)

    def work(path):
        first = case.ledger.begin(TOOL, PARAMS, "same-key", path=path)
        case.ledger.finish(first["effect_id"], {"ok": True, "owner": path.parent.name},
                           path=path, attempt=first["attempt"])
        return case.ledger.begin(TOOL, PARAMS, "same-key", path=path)["result"]["owner"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(work, paths)) == ["alice", "bob"]
    assert dict(os.environ) == before and case.ledger._PATH == case.default
    assert not case.default.parent.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX file-lock implementation")
def test_same_ledger_cross_process_claim_executes_once(case):
    # Only short CPU children. No external executable, GPU, network or native author.
    ctx = multiprocessing.get_context("fork")
    ready, results, start = ctx.Queue(), ctx.Queue(), ctx.Event()
    children = [ctx.Process(target=_claim_in_child,
                            args=(str(case.path), ready, start, results)) for _ in range(4)]
    try:
        for child in children:
            child.start()
        for _ in children:
            assert ready.get(timeout=5)
        start.set()
        decisions = [results.get(timeout=5) for _ in children]
        for child in children:
            child.join(timeout=5)
            assert child.exitcode == 0
        assert all(isinstance(item, str) for item in decisions), decisions
        assert sorted(decisions) == ["execute", "in_progress", "in_progress", "in_progress"]
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(timeout=2)
        ready.close()
        results.close()


@pytest.mark.parametrize("content", [
    b"{broken", b"[]", b"null", b'{"version":1,"effects":[]}', b"{}",
    b'{"version":1,"effects":{"x":null}}', b"\xff",
])
def test_corrupt_scoped_ledger_never_becomes_empty_or_execute(case, content):
    case.path.write_bytes(content)
    with pytest.raises(case.ledger.LedgerError, match="^ledger_corrupt$"):
        case.ledger.begin(TOOL, PARAMS, "new-key", path=case.path)
    assert case.path.read_bytes() == content


@pytest.mark.parametrize("damage", ["status", "safe_retry", "attempts", "lease", "result"])
def test_malformed_effect_record_is_not_treated_as_retryable(case, damage):
    first = case.ledger.begin(TOOL, PARAMS, "record", path=case.path)
    data = json.loads(case.path.read_text())
    row = data["effects"][first["effect_id"]]
    row.update({
        "status": {"status": "typo"},
        "safe_retry": {"safe_retry": "false"},
        "attempts": {"attempts": -1},
        "lease": {"lease_expires_at": "NaN"},
        "result": {"status": "completed", "result": None},
    }[damage])
    case.path.write_text(json.dumps(data))
    before = case.path.read_bytes()
    with pytest.raises(case.ledger.LedgerError, match="^ledger_corrupt$"):
        case.ledger.begin(TOOL, PARAMS, "record", path=case.path)
    assert case.path.read_bytes() == before


@pytest.mark.parametrize("api", ["begin", "finish", "reconcile", "recover_expired"])
def test_all_explicit_path_apis_reject_corrupt_file(case, api):
    case.path.write_text("{broken")
    args = {
        "begin": (TOOL, PARAMS, "bad"),
        "finish": ("missing", {"ok": True}),
        "reconcile": ("missing", "completed"),
        "recover_expired": (),
    }[api]
    with pytest.raises(case.ledger.LedgerError, match="^ledger_corrupt$"):
        getattr(case.ledger, api)(*args, path=case.path)


def test_unreadable_scoped_file_is_not_missing(case):
    case.path.write_text('{"version":1,"effects":{}}')
    case.path.chmod(0)
    try:
        with pytest.raises(case.ledger.LedgerError, match="^ledger_unreadable$"):
            case.ledger.begin(TOOL, PARAMS, "bad", path=case.path)
    finally:
        case.path.chmod(0o600)


@pytest.mark.parametrize("kind", ["ledger", "lock", "parent", "ancestor", "hardlink", "fifo"])
def test_scoped_io_does_not_follow_links_or_open_nonregular_files(case, kind):
    outside = case.root / "outside"
    outside.mkdir(mode=0o700)
    victim = outside / "victim"
    victim.write_text("do not touch")
    path = case.path
    if kind in {"ledger", "lock"}:
        (path if kind == "ledger" else path.with_suffix(".lock")).symlink_to(victim)
    elif kind == "parent":
        link = case.work / "linked"
        link.symlink_to(outside, target_is_directory=True)
        path = link / ".media-effects.json"
    elif kind == "ancestor":
        (outside / "nested").mkdir()
        link = case.work / "linked"
        link.symlink_to(outside, target_is_directory=True)
        path = link / "nested" / ".media-effects.json"
    elif kind == "hardlink":
        os.link(victim, path)
    else:
        os.mkfifo(path)
    with pytest.raises(case.ledger.LedgerError, match="^ledger_unsafe_path$"):
        case.ledger.begin(TOOL, PARAMS, "unsafe", path=path)
    assert victim.read_text() == "do not touch"


@pytest.mark.parametrize("target", ["parent", "ledger", "lock"])
def test_group_or_other_writable_paths_are_rejected(case, target):
    path = {"parent": case.work, "ledger": case.path,
            "lock": case.path.with_suffix(".lock")}[target]
    if target != "parent":
        path.write_text('{"version":1,"effects":{}}')
    old = path.stat().st_mode & 0o777
    path.chmod(old | 0o022)
    try:
        with pytest.raises(case.ledger.LedgerError, match="^ledger_unsafe_path$"):
            case.ledger.begin(TOOL, PARAMS, "unsafe", path=case.path)
    finally:
        path.chmod(old)


def test_wrong_parent_owner_is_rejected(case):
    real_fstat = os.fstat
    inode = case.work.stat().st_ino

    def wrong_owner(fd):
        info = real_fstat(fd)
        if info.st_ino == inode:
            values = list(info)
            values[4] = os.getuid() + 1
            return os.stat_result(values)
        return info

    case.patch.setattr(case.ledger.os, "fstat", wrong_owner)
    with pytest.raises(case.ledger.LedgerError, match="^ledger_unsafe_path$"):
        case.ledger.begin(TOOL, PARAMS, "unsafe", path=case.path)
    assert not case.path.exists()


def test_explicit_path_does_not_create_missing_parent(case):
    path = case.work / "missing" / ".media-effects.json"
    with pytest.raises(case.ledger.LedgerError, match="^ledger_unreadable$"):
        case.ledger.begin(TOOL, PARAMS, "missing", path=path)
    assert not path.parent.exists()


def test_failed_atomic_replace_preserves_previous_claim_and_cleans_temporary(case):
    first = case.ledger.begin(TOOL, PARAMS, "atomic", path=case.path)
    before = case.path.read_bytes()
    before_names = set(case.work.iterdir())

    def fail_replace(*_args, **_kwargs):
        raise OSError("fixture-only sensitive path must not escape")

    with case.patch.context() as patch:
        patch.setattr(case.ledger.os, "replace", fail_replace)
        with pytest.raises(case.ledger.LedgerError, match="^ledger_write_failed$"):
            case.ledger.finish(first["effect_id"], {"ok": True}, path=case.path,
                               attempt=first["attempt"])
    assert case.path.read_bytes() == before
    assert set(case.work.iterdir()) == before_names
    assert case.ledger.begin(TOOL, PARAMS, "atomic", path=case.path)["decision"] == "in_progress"


def test_explicit_path_requires_cross_process_lock_primitive(case):
    case.patch.setattr(case.ledger, "fcntl", None)
    with pytest.raises(case.ledger.LedgerError, match="^ledger_lock_unavailable$"):
        case.ledger.begin(TOOL, PARAMS, "lock", path=case.path)
    assert not case.path.exists()


def test_existing_lock_does_not_use_racy_nonexclusive_create(case):
    lock_name = case.path.with_suffix(".lock").name
    (case.work / lock_name).write_text("")
    real_open = os.open

    def observed_lookup_race(path, flags, *args, **kwargs):
        # The real fork test observed ENOENT at O_CREAT|O_NOFOLLOW while another
        # process created this same lock. Existing-file opens must not create.
        if path == lock_name and flags & os.O_CREAT and not flags & os.O_EXCL:
            raise FileNotFoundError(errno.ENOENT, "fixture concurrent lock creation")
        return real_open(path, flags, *args, **kwargs)

    case.patch.setattr(case.ledger.os, "open", observed_lookup_race)
    assert case.ledger.begin(TOOL, PARAMS, "lock-race", path=case.path)["decision"] == "execute"


@pytest.mark.parametrize("root_key", ["MEYO_LEGION_DIR", "MEYO_PERSONAL_STATE_DIR",
                                    "LEGION_PERSONAL_STATE_DIR"])
def test_bound_cache_hit_reads_only_explicit_environment_without_writes(case, root_key):
    environment = {"MEYO_SESSION_ID": "codex:order",
                   root_key: str(case.root / "legacy"), "MEYO_WORKDIR": str(case.work)}
    path = _legacy(case, environment)
    before = {p: p.read_bytes() for p in (case.root / "legacy").rglob("*") if p.is_file()}
    case.patch.setenv("MEYO_SESSION_ID", "codex:wrong")
    case.patch.setenv("MEYO_LEGION_DIR", str(case.root / "unrelated"))
    value = case.idem.check_bound(TOOL, PARAMS, environment=environment)
    assert value["ok"] and value["idempotent_hit"] and value["path"] == "old.png"
    assert {p: p.read_bytes() for p in (case.root / "legacy").rglob("*") if p.is_file()} == before
    assert path.exists() and not case.default.exists()
    assert not (case.root / "unrelated").exists()


def test_production_environment_personal_root_reaches_legacy_cache(case):
    runtime = importlib.import_module("personal_runtime")
    root = case.root / "production-shape"
    root.mkdir(mode=0o700)
    for directory in runtime._STATE_DIRS:
        (root / directory).mkdir(mode=0o700)
    for name, content in {
        "config.json": json.dumps({"schema_version": 1, "user": "ut-ledger",
                                    "host": "127.0.0.1", "port": 12345}),
        "login-token.txt": "cpu-fixture-only",
        "secrets/personal-tokens.json": json.dumps({
            "cpu-fixture-only": {"user": "ut-ledger"}}),
    }.items():
        path = root / name
        path.write_text(content)
        path.chmod(0o600)
    # Use the real constructor, with no provider profile or inherited host env.
    environment, _ = runtime.environment(root, inherited={})
    assert environment["MEYO_PERSONAL_STATE_DIR"] == str(root)
    assert environment["MEYO_LEGION_DIR"] == str(root / "data")
    assert "LEGION_PERSONAL_STATE_DIR" not in environment
    environment.pop("MEYO_LEGION_DIR")  # Only the trusted per-tool root survives.
    environment["MEYO_SESSION_ID"] = "codex:order"
    path = _legacy(case, environment)
    before = path.read_bytes()
    result = case.idem.check_bound(TOOL, PARAMS, environment=environment)
    assert result is not None and result["path"] == "old.png"
    assert path.read_bytes() == before and list(path.parent.iterdir()) == [path]


def test_production_instance_root_precedes_compatibility_alias(case):
    real = {"MEYO_SESSION_ID": "codex:order",
            "MEYO_PERSONAL_STATE_DIR": str(case.root / "production")}
    alias = {"MEYO_SESSION_ID": "codex:order",
             "LEGION_PERSONAL_STATE_DIR": str(case.root / "compatibility")}
    _legacy(case, real, result={"ok": True, "path": "production.png"})
    _legacy(case, alias, result={"ok": True, "path": "alias.png"})
    assert case.idem.check_bound(TOOL, PARAMS, environment={**alias, **real})[
        "path"] == "production.png"


def test_production_root_is_never_borrowed_from_host_environment(case):
    environment = {"MEYO_SESSION_ID": "codex:order",
                   "MEYO_PERSONAL_STATE_DIR": str(case.root / "host")}
    path = _legacy(case, environment)
    path.write_text("{broken")
    for key, value in environment.items():
        case.patch.setenv(key, value)
    assert case.idem.check_bound(TOOL, PARAMS, environment={
        "MEYO_SESSION_ID": "codex:order"}) is None


@pytest.mark.parametrize(("fields", "qhash"), [
    ({"MEYO_SESSION_ID": "codex:order:task", "MEYO_QHASH": "ignored"}, "order:task"),
    ({"MEYO_WORKDIR": "/unused/codex_work_qhash", "MEYO_QHASH": "ignored"}, "work_qhash"),
    ({"MEYO_QHASH": "explicit"}, "explicit"),
])
def test_bound_cache_qhash_derivation_is_compatible_with_old_api(case, fields, qhash):
    environment = {"MEYO_LEGION_DIR": str(case.root / "legacy"), **fields}
    _legacy(case, environment, qhash=qhash)
    assert case.idem.check_bound(TOOL, PARAMS, environment=environment)["idempotent_hit"]


@pytest.mark.parametrize("environment", [{}, {"MEYO_SESSION_ID": "codex:order"},
                                        {"MEYO_WORKDIR": "/unused/key_order"},
                                        {"MEYO_QHASH": "order"}])
def test_unbound_legacy_lookup_never_reads_global_environment(case, environment):
    global_env = _bound_env(case)
    path = _legacy(case, global_env)
    for key, value in global_env.items():
        case.patch.setenv(key, value)
    path.write_text("{broken")
    assert case.idem.check_bound(TOOL, PARAMS, environment=environment) is None


def test_missing_bound_cache_does_not_create_root_directories_or_locks(case):
    environment = _bound_env(case)
    assert case.idem.check_bound(TOOL, PARAMS, environment=environment) is None
    assert not Path(environment["MEYO_LEGION_DIR"]).exists()
    Path(environment["MEYO_LEGION_DIR"]).mkdir()
    assert case.idem.check_bound(TOOL, PARAMS, environment=environment) is None
    assert list(Path(environment["MEYO_LEGION_DIR"]).iterdir()) == []


@pytest.mark.parametrize("content", [b"{broken", b"[]", b"null", b"\xff"])
def test_corrupt_legacy_cache_raises_fixed_error_instead_of_miss(case, content):
    environment = _bound_env(case)
    path = _legacy(case, environment)
    path.write_bytes(content)
    with pytest.raises(case.idem.BoundCacheError, match="^legacy_cache_corrupt$"):
        case.idem.check_bound(TOOL, PARAMS, environment=environment)
    assert path.read_bytes() == content


@pytest.mark.parametrize("entry", [None, {}, {"result": None},
                                  {"result": {"ok": False}}, {"result": "bad"}])
def test_malformed_matching_legacy_entry_is_not_a_miss(case, entry):
    environment = _bound_env(case)
    path = _legacy(case, environment)
    path.write_text(json.dumps({case.idem._key(TOOL, PARAMS): entry}))
    with pytest.raises(case.idem.BoundCacheError, match="^legacy_cache_corrupt$"):
        case.idem.check_bound(TOOL, PARAMS, environment=environment)


def test_unreadable_bound_cache_raises_fixed_error(case):
    environment = _bound_env(case)
    path = _legacy(case, environment)
    path.chmod(0)
    try:
        with pytest.raises(case.idem.BoundCacheError, match="^legacy_cache_unreadable$"):
            case.idem.check_bound(TOOL, PARAMS, environment=environment)
    finally:
        path.chmod(0o600)


@pytest.mark.parametrize("kind", ["file", "root", "parent"])
def test_bound_cache_never_follows_links(case, kind):
    environment = _bound_env(case)
    path = _legacy(case, environment)
    if kind == "file":
        saved = path.with_suffix(".saved")
        path.rename(saved)
        path.symlink_to(saved)
    else:
        old = Path(environment["MEYO_LEGION_DIR"]) if kind == "root" else path.parent
        saved = old.with_name(old.name + "-saved")
        old.rename(saved)
        old.symlink_to(saved, target_is_directory=True)
    with pytest.raises(case.idem.BoundCacheError, match="^legacy_cache_unsafe_path$"):
        case.idem.check_bound(TOOL, PARAMS, environment=environment)


def test_legacy_cache_can_be_read_from_owner_readonly_directory_without_lock(case):
    environment = _bound_env(case)
    path = _legacy(case, environment)
    path.parent.chmod(0o500)
    path.chmod(0o400)
    try:
        assert case.idem.check_bound(TOOL, PARAMS, environment=environment)["idempotent_hit"]
        assert list(path.parent.iterdir()) == [path]
    finally:
        path.parent.chmod(0o700)


def test_bound_cache_thread_scopes_do_not_cross_and_params_remain_original(case):
    environments = [_bound_env(case, owner) for owner in ("alice", "bob")]
    for environment, owner in zip(environments, ("alice", "bob")):
        _legacy(case, environment, result={"ok": True, "path": owner + ".png"})
    original = dict(PARAMS)
    before_env = dict(os.environ)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(
            lambda env: case.idem.check_bound(TOOL, PARAMS, environment=env), environments))
    assert [out["path"] for out in outputs] == ["alice.png", "bob.png"]
    assert PARAMS == original and dict(os.environ) == before_env
    assert case.idem.check_bound(TOOL, {**PARAMS, "prompt": "changed"},
                                 environment=environments[0]) is None


def test_existing_idempotency_apis_remain_available(case):
    case.patch.setenv("MEYO_SESSION_ID", "codex:old-api")
    case.idem.record(TOOL, PARAMS, {"ok": True, "path": "old.png"})
    assert case.idem.check(TOOL, PARAMS)["idempotent_hit"]
    assert case.idem.check("media:see_image", PARAMS) is None
    case.idem.clear("old-api")
    assert case.idem.check(TOOL, PARAMS) is None
