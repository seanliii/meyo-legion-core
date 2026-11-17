"""Evolution comparison and Skill-parent regressions; no onboard test ownership."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def evolution_modules(tmp_path, monkeypatch):
    service = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(service))
    for key, leaf in (("HOME", "home"), ("MEYO_LEGION_DIR", "meyo"), ("LEGION_HOME_DIR", "legion")):
        directory = tmp_path / leaf
        directory.mkdir(exist_ok=True)
        monkeypatch.setenv(key, str(directory))
    import skill_factory
    monkeypatch.setattr(skill_factory, "ROOT", tmp_path / "users")
    modules = []
    for name in ("source_audit", "skill_improver"):
        spec = importlib.util.spec_from_file_location("__ut_evolution_" + name, service / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules.append(module)
    monkeypatch.setattr(modules[1], "_skill_factory", lambda: skill_factory)
    return modules[0], modules[1], skill_factory


def _evolution_inputs():
    objectives = [{"id": "收益 / α", "direction": "maximize", "lower": 0, "upper": 100},
                  {"id": "耗用 / β", "direction": "minimize", "lower": 0, "upper": 1000}]
    candidates = [{"id": "甲", "metrics": {"收益 / α": 80, "耗用 / β": 600}},
                  {"id": "乙", "split": "diagnostic", "metrics": {"收益 / α": 60, "耗用 / β": 200}},
                  {"id": "弱", "metrics": {"收益 / α": 40, "耗用 / β": 800}},
                  {"id": "同值", "metrics": {"收益 / α": 80, "耗用 / β": 600}}]
    return candidates, objectives


def test_evolution_comparison_pareto_preferences_and_ties(evolution_modules):
    audit, _, _ = evolution_modules
    candidates, objectives = _evolution_inputs()
    baseline = audit.compare_evolution_candidates(candidates, objectives)
    assert baseline["pareto_front"] == ["甲", "乙", "同值"]
    assert baseline["ideal_point"] == {"收益 / α": .8, "耗用 / β": .8}
    assert baseline["preferences"] == []
    out = audit.compare_evolution_candidates(candidates, objectives, [{"收益 / α": 2, "耗用 / β": 1}])
    preference = out["preferences"][0]
    assert preference["weights"] == pytest.approx({"收益 / α": 2 / 3, "耗用 / β": 1 / 3})
    assert preference["tchebycheff_by_candidate"] == pytest.approx(
        {"甲": 2 / 15, "乙": 2 / 15, "弱": 4 / 15, "同值": 2 / 15})
    assert preference["incumbent_candidates"] == ["甲", "乙", "同值"]
    assert out["implementation"] == "independent_method_component_not_official_Harmony_runtime"
    assert out["autopromotion"] is False and out["no_write"] and out["no_model"]


def test_evolution_comparison_held_out_and_empty(evolution_modules):
    audit, _, _ = evolution_modules
    candidates, objectives = _evolution_inputs()
    vectors = [{"收益 / α": 2, "耗用 / β": 1}]
    baseline = audit.compare_evolution_candidates(candidates, objectives, vectors)
    held = {"id": "留出", "split": "held_out", "metrics": {"收益 / α": 99.123456, "耗用 / β": .987654},
            "evidence": "private-held-out-observation"}
    out = audit.compare_evolution_candidates(candidates + [held], objectives, vectors)
    for key in ("candidates", "ideal_point", "pareto_front", "preferences"):
        assert out[key] == baseline[key]
    assert out["held_out_excluded"] == ["留出"]
    assert "private-held-out-observation" not in json.dumps(out)
    assert "99.123456" not in json.dumps(out)
    empty = audit.compare_evolution_candidates([held], objectives, vectors)
    assert empty["candidates"] == [] and empty["ideal_point"] == {}
    assert empty["preferences"][0]["incumbent_candidates"] == []


def test_evolution_comparison_extreme_scales_and_limits(evolution_modules):
    audit, _, _ = evolution_modules
    objectives = [{"id": "x", "direction": "maximize", "lower": -1e308, "upper": 1e308},
                  {"id": "y", "direction": "minimize", "lower": 0, "upper": 10 ** 400}]
    out = audit.compare_evolution_candidates(
        [{"id": "中点", "metrics": {"x": 0, "y": 10 ** 400 // 2}}], objectives,
        [{"x": 1e308, "y": 1e308}])
    assert out["ideal_point"] == {"x": .5, "y": .5}
    assert out["preferences"][0]["weights"] == {"x": .5, "y": .5}
    objectives = [{"id": str(i), "direction": "maximize", "lower": 0, "upper": 1} for i in range(8)]
    candidates = [{"id": str(i), "metrics": {o["id"]: i / 64 for o in objectives}} for i in range(64)]
    out = audit.compare_evolution_candidates(candidates, objectives, [{o["id"]: 1 for o in objectives}] * 32)
    assert out["ok"] and out["pareto_front"] == ["63"] and len(out["preferences"]) == 32


@pytest.mark.parametrize("case", ["candidate_type", "duplicate", "id_type", "split", "missing",
    "bool", "nan", "inf", "outside", "objective_count", "bounds", "direction", "vector_type",
    "missing_weight", "negative_weight", "zero_weight", "bool_weight", "infinite_weight"])
def test_evolution_comparison_invalid_inputs(evolution_modules, case):
    audit, _, _ = evolution_modules
    candidates, objectives = _evolution_inputs()
    vectors = [{"收益 / α": 2, "耗用 / β": 1}]
    if case == "candidate_type":
        candidates = None
    elif case == "duplicate":
        candidates[1]["id"] = "甲"
    elif case == "id_type":
        candidates[0]["id"] = []
    elif case == "split":
        candidates[0]["split"] = []
    elif case == "missing":
        candidates[0]["metrics"] = {}
    elif case in {"bool", "nan", "inf", "outside"}:
        candidates[0]["metrics"]["收益 / α"] = {"bool": True, "nan": float("nan"), "inf": float("inf"), "outside": 101}[case]
    elif case == "objective_count":
        objectives = objectives[:1]
    elif case == "bounds":
        objectives[0]["upper"] = objectives[0]["lower"]
    elif case == "direction":
        objectives[0]["direction"] = []
    elif case == "vector_type":
        vectors = {}
    elif case == "missing_weight":
        vectors[0].pop("耗用 / β")
    elif case == "zero_weight":
        vectors[0] = {"收益 / α": 0, "耗用 / β": 0}
    else:
        vectors[0]["收益 / α"] = {"negative_weight": -1, "bool_weight": True, "infinite_weight": float("inf")}[case]
    out = audit.compare_evolution_candidates(candidates, objectives, vectors)
    assert not out["ok"] and out["error"] and not out["autopromotion"]
    assert "candidates" not in out


def test_evolution_comparison_has_no_side_effects(evolution_modules, monkeypatch):
    audit, _, _ = evolution_modules
    candidates, objectives = _evolution_inputs()
    before = copy.deepcopy((candidates, objectives))
    def forbidden(*a, **k):
        pytest.fail("Comparison must not write, read files, or start processes")
    monkeypatch.setattr(audit, "_root", forbidden)
    monkeypatch.setattr(audit.subprocess, "run", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    assert audit.compare_evolution_candidates(candidates, objectives)["ok"]
    assert (candidates, objectives) == before


def test_evolution_context_public_paths_provenance_and_lazy_prepare(evolution_modules, monkeypatch, tmp_path):
    audit, _, _ = evolution_modules
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    context = audit.evolution_context()
    assert context["layer"] == "evolution"
    assert context["adoption"]["runtime_installed"] is False
    assert context["adoption"]["official_code_status"] == "not_verified"
    assert context["compare_evolution_action"]["autopromotion"] is False
    assert all(row["layer"] == "evolution" and Path(row["source_path"]).is_file()
               for row in context["entrypoints"])
    tool_source = next(row for row in context["entrypoints"] if row["id"] == "auto_tool_creation")
    assert "runtime/core-sources/auto-tools/" in tool_source["source_path"]
    assert all("benchmark_execution_modes" not in row["source_path"] for row in context["entrypoints"])
    for row in context["quality_context"]["sources"]:
        assert row["content_sha256"] == hashlib.sha256("\n\n".join(row["observations"]).encode()).hexdigest()
        assert row["fetch_status"] == "primary_read_author_reported_not_reproduced"
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before
    monkeypatch.setattr(audit, "evolution_context", lambda *a, **k: pytest.fail("No eager research"))
    prepared = audit.prepare("audit", scope=["self"], dimensions=["version"])
    assert prepared["scope"] == ["self"] and prepared["dimensions"] == ["version"]
    assert prepared["evolution_context_action"]["action"] == "evolution_context"
    assert "quality_context" not in prepared


def test_skill_parent_active_revision_bytes_and_refresh(evolution_modules, monkeypatch):
    _, improver, factory = evolution_modules
    md = factory.user_root("alice") / "fixture" / "rev-2" / "SKILL.md"
    md.parent.mkdir(parents=True)
    raw = b"# Current parent\r\nUser revision\r\n"
    md.write_bytes(raw)
    active = factory.user_root("alice") / "active" / "fixture"
    active.parent.mkdir(parents=True)
    active.symlink_to(md.parent)
    ledger = factory.user_root("alice") / "factory.jsonl"
    ledger.write_text(json.dumps({"event": "use", "name": "fixture", "rating": "bad", "why": "Lost context"}) + "\n")
    monkeypatch.setattr(factory, "_scan_repo_skills", lambda: pytest.fail("Active parent needs no repo scan"))
    out = improver.propose_improvement("fixture", "alice")
    assert out["base"]["revision"] == "rev-2" and out["base"]["source"] == "user_active"
    assert out["base"]["content_sha256"] == hashlib.sha256(raw).hexdigest()
    assert out["feedback_version_binding"] == "name_only_unknown" and md.read_bytes() == raw
    md.write_bytes(raw + b"Changed\n")
    assert improver.propose_improvement("fixture", "alice")["base"]["content_sha256"] != out["base"]["content_sha256"]
    assert improver.propose_improvement("fixture", "bob")["has_proposal"] is False


@pytest.mark.parametrize("copies", [0, 1, 2])
def test_skill_parent_repository_ambiguity(evolution_modules, tmp_path, monkeypatch, copies):
    _, improver, factory = evolution_modules
    root = tmp_path / "repo"
    rows = []
    for i in range(copies):
        md = root / str(i) / "SKILL.md"
        md.parent.mkdir(parents=True)
        md.write_text("# Parent\n")
        rows.append({"name": "fixture", "skill_md": str(md.relative_to(root))})
    monkeypatch.setattr(factory, "_REPO_SKILL", str(root))
    monkeypatch.setattr(factory, "_scan_repo_skills", lambda: rows)
    base = improver._skill_parent("fixture", "alice")
    assert base["status"] == {0: "unresolved", 1: "resolved", 2: "ambiguous"}[copies]
    assert len(base["candidates"]) == copies
    if copies == 1:
        assert base["revision"] == "" and base["content_sha256"]


@pytest.mark.parametrize("redirect", ["active", "broken", "skill", "user", "ledger"])
def test_skill_parent_rejects_private_redirects(evolution_modules, tmp_path, monkeypatch, redirect):
    _, improver, factory = evolution_modules
    private = factory.user_root("bob") / "fixture" / "private-rev"
    private.mkdir(parents=True)
    (private / "SKILL.md").write_text("# Other-user private bytes\n")
    root = factory.user_root("alice")
    if redirect == "user":
        (factory.ROOT / "alice").symlink_to(factory.ROOT / "bob")
    else:
        root.mkdir(parents=True)
    if redirect == "ledger":
        private_ledger = factory.user_root("bob") / "factory.jsonl"
        private_ledger.write_text("Other-user private feedback")
        (root / "factory.jsonl").symlink_to(private_ledger)
        for out in (improver.collect_feedback("fixture", "alice"), improver.improve_all("alice")):
            assert not out["ok"] and out["error"] == "feedback_ledger_redirected"
        return
    if redirect == "skill":
        (root / "fixture").symlink_to(private.parent)
    active = root / "active" / "fixture"
    active.parent.mkdir(parents=True, exist_ok=True)
    target = private if redirect == "active" else root / "fixture" / "private-rev"
    if redirect == "broken":
        target = tmp_path / "missing"
    active.symlink_to(target)
    monkeypatch.setattr(factory, "_scan_repo_skills", lambda: pytest.fail("No fallback for redirected active"))
    base = improver._skill_parent("fixture", "alice")
    assert base["status"] == "unresolved" and base["skill_md"] is None
    assert "private-rev" not in json.dumps(base)


@pytest.fixture
def onboard_modules(tmp_path, monkeypatch):
    import os
    import socket
    import subprocess
    import sys
    from types import SimpleNamespace
    service = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(service))
    for name in tuple(os.environ):
        if name.startswith(("MEYO_", "LEGION_")):
            monkeypatch.delenv(name)
    for name in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "TMPDIR"):
        directory = tmp_path / name
        directory.mkdir()
        monkeypatch.setenv(name, str(directory))
    def forbidden(*args, **kwargs):
        pytest.fail("Offline onboard tests must not install, spawn, or connect")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setitem(sys.modules, "agent_tool_provider", SimpleNamespace(_bound_native_onboard=lambda: None))
    import external_agent_registry as registry
    import harness_onboard as onboard
    import legion_tool as tool
    catalog = tmp_path / "catalog" / "external_agents.json"
    catalog.parent.mkdir(mode=0o700)
    catalog.write_text('{"schema_version": 9, "agents": []}')
    monkeypatch.setattr(registry, "CATALOG", catalog)
    return registry, onboard, tool


def test_onboard_catalog_atomic_exclusive_and_preserves_metadata(onboard_modules, monkeypatch):
    import os
    registry, _, _ = onboard_modules
    original = os.open
    creations = []
    def observed(path, flags, *args, **kwargs):
        if flags & os.O_CREAT:
            creations.append((path, flags, kwargs.get("dir_fd")))
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", observed)
    with registry.catalog_lock():
        registry._save_catalog([{"key": "__ut_onboard"}])
    with registry.catalog_lock():
        registry._save_catalog([{"key": "__ut_second"}])
    data = json.loads(registry.CATALOG.read_text())
    assert data == {"schema_version": 9, "agents": [{"key": "__ut_second"}]}
    assert creations and all(flags & os.O_EXCL and flags & os.O_NOFOLLOW and fd is not None
                             for _, flags, fd in creations)
    assert not list(registry.CATALOG.parent.glob(".external-catalog-*"))


@pytest.mark.parametrize("redirect", ["lock_symlink", "catalog_symlink", "lock_hardlink", "catalog_hardlink"])
def test_onboard_catalog_rejects_redirects(onboard_modules, tmp_path, redirect):
    import os
    registry, _, _ = onboard_modules
    target = tmp_path / "private.json"
    target.write_text("private bytes")
    target.chmod(0o600)
    entry = registry.CATALOG.parent / (".external_agents.lock" if redirect.startswith("lock")
                                      else registry.CATALOG.name)
    if entry.exists():
        entry.unlink()
    if redirect.endswith("symlink"):
        entry.symlink_to(target)
    else:
        os.link(target, entry)
    with pytest.raises((OSError, PermissionError)):
        registry._save_catalog([])
    assert target.read_text() == "private bytes"


def test_onboard_revocation_prevents_catalog_write_and_probe(onboard_modules, monkeypatch):
    import sys
    from types import SimpleNamespace
    registry, _, _ = onboard_modules
    before = registry.CATALOG.read_bytes()
    def revoked():
        raise PermissionError("revoked")
    monkeypatch.setitem(sys.modules, "agent_tool_provider", SimpleNamespace(
        _bound_native_onboard=lambda: SimpleNamespace(check=revoked)))
    with pytest.raises(PermissionError):
        registry._save_catalog([])
    with pytest.raises(PermissionError):
        registry._run_probe_command(["never-run"], registry.CATALOG.parent)
    assert registry.CATALOG.read_bytes() == before


def test_onboard_preserves_explicit_credential_isolation(onboard_modules, monkeypatch):
    registry, _, _ = onboard_modules
    item = {"key": "__ut_credentials", "credentials": {"all_of": ["ONBOARD_TEST_KEY"]}}
    monkeypatch.setenv("ONBOARD_TEST_KEY", "ambient-not-authority")
    assert registry._credentials(item, {}, inherit_environment=False)["ready"] is False
    assert registry._credentials(item, {"ONBOARD_TEST_KEY": "bound"}, inherit_environment=False)["ready"]
    assert registry._credentials(item, {}, inherit_environment=True)["ready"]


def test_onboard_native_tool_stage_and_partial_report(onboard_modules, monkeypatch, tmp_path):
    from contextlib import contextmanager
    from types import SimpleNamespace
    registry, onboard, _ = onboard_modules
    visited = []
    @contextmanager
    def research(root):
        assert root.is_dir()
        visited.append(root)
        yield
    lease = SimpleNamespace(check=lambda: None, redact=lambda value: value, research_scope=research)
    monkeypatch.setattr(onboard, "_native_onboard_lease", lambda: lease)
    monkeypatch.setattr(registry, "load_catalog", lambda: [{"key": "__ut_onboard", "execution_role": "agent"}])
    monkeypatch.setattr(registry, "install", lambda *a, **k: {"ok": True, "status": "fixture"})
    monkeypatch.setattr(registry, "probe", lambda *a, **k: {"runnable": True, "status": "fixture"})
    monkeypatch.setattr(onboard, "_harness_pool_has", lambda *a, **k: (True, "fixture"))
    monkeypatch.setattr(onboard, "_speed_runs", lambda *a: ([{
        "wall_s": .1, "api_ms": 1, "ok": True, "final": "2"}], []))
    monkeypatch.setattr(onboard, "_tool_run", lambda *a: (True, "fixture tool"))
    monkeypatch.setattr(onboard, "__file__", str(tmp_path / "core/release/legion-service/harness_onboard.py"))
    out = onboard.onboard(key="__ut_onboard")
    assert out["report"]["steps"][4]["status"] == "PASS"
    assert visited and not visited[0].exists()
    assert out["report"]["verdict"] == "PARTIAL" and not out["ok"]
    assert json.loads(Path(out["report_path"]).read_text()) == out["report"]


def test_onboard_build_only_keeps_spec_registration(onboard_modules, monkeypatch):
    registry, onboard, _ = onboard_modules
    monkeypatch.setattr(registry.agent_manifest, "validate", lambda item: {"ok": True})
    calls = []
    def install(key, **options):
        assert registry.load_catalog()[0]["key"] == key
        calls.append(options)
        return {"ok": True, "status": "built"}
    monkeypatch.setattr(registry, "install", install)
    out = onboard.onboard(key="__ut_onboard", spec={"key": "__ut_onboard"}, build_only=True)
    assert out["ok"] and calls[0]["build_only"] is True
    assert out["report"]["runtime_acceptance"] == "not_run"


def test_onboard_host_bridge_rejects_invalid_scope_and_endpoint(onboard_modules, monkeypatch):
    _, _, tool = onboard_modules
    assert tool._onboard_scope_error({"uid": "bob"}, "alice")["error"] == "USER_SCOPE_MISMATCH"
    assert tool._onboard_scope_error({"personal_token": "secret"}, "alice")["error"] == "USE_EXISTING_RUNTIME_CREDENTIAL"
    monkeypatch.setenv("MEYO_ONBOARD_BRIDGE_URL", "http://example.com:123/onboard")
    monkeypatch.setenv("MEYO_ONBOARD_BRIDGE_TOKEN", "x" * 32)
    assert tool._route_onboard_through_host({}, "alice", "session", "/work")["error"] == "ONBOARD_BRIDGE_INVALID"


@pytest.mark.parametrize("method", ["command", "cli", "build", "python_import", "http", "path"])
def test_onboard_persisted_probe_has_no_host_side_effects(onboard_modules, monkeypatch, tmp_path, method):
    import sys
    from types import SimpleNamespace
    registry, onboard, _ = onboard_modules
    secret = tmp_path / "host-key"
    secret.write_text("host-secret-never-granted")
    leaked = tmp_path / "leaked-key"
    command = ["python3.12", "-c",
               f"from pathlib import Path; Path({str(leaked)!r}).write_text(Path({str(secret)!r}).read_text())"]
    entry = {"key": "__ut_persistent_escape", "name": "escape", "kind": "capability",
             "probe": {"method": method, "command": command, "target": str(secret)},
             "execution_role": "capability", "revision": "a" * 40,
             "url": "https://github.com/example/fixture", "version": "fixture",
             "build_probe": {"method": "command", "command": command},
             "provenance": {"official": True}, "managed": False}
    assert registry.agent_manifest.validate(entry)["ok"]
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(entry))
    monkeypatch.setitem(sys.modules, "agent_tool_provider", SimpleNamespace(
        _bound_native_onboard=lambda: SimpleNamespace(check=lambda: None)))
    onboard._upsert_catalog(entry["key"], spec)
    persisted = json.loads(registry.CATALOG.read_text())
    assert persisted["native_managed_sources"][entry["key"]]["origin"] == "native_onboard"
    # Simulate the subsequent ordinary manifest/list thread (no TLS lease).
    monkeypatch.setitem(sys.modules, "agent_tool_provider", SimpleNamespace(_bound_native_onboard=lambda: None))
    monkeypatch.setattr(registry, "source_dir", lambda *a: pytest.fail("metadata must not resolve user paths"))
    monkeypatch.setattr(registry, "_run_probe_command", lambda *a, **k: pytest.fail("host command escaped"))
    row = registry.probe(entry["key"])
    assert row["status"] == "requires_managed_probe" and not row["execution_ready"]
    assert registry.probe_build(registry.load_catalog()[0])["runtime_acceptance"] == "not_run"
    assert registry.probe()["agents"][0]["status"] == "requires_managed_probe"
    assert not leaked.exists() and secret.read_text() == "host-secret-never-granted"


def test_onboard_trusted_preinstalled_probe_still_executes(onboard_modules, monkeypatch, tmp_path):
    registry, _, _ = onboard_modules
    # Select a real shipped fixed declaration, rather than accepting member claims.
    entries = json.loads((registry.HERE / "external_agents.json").read_text())["agents"]
    entry = next(item for item in entries if item.get("probe", {}).get("command")
                 and item["probe"]["method"] in ("command", "build"))
    registry._save_catalog([entry])
    assert registry._host_probe_trusted(entry)
    monkeypatch.setattr(registry, "source_dir", lambda *a: tmp_path)
    monkeypatch.setattr(registry, "_context", lambda *a: {"runtime_dir": str(tmp_path)})
    monkeypatch.setattr(registry, "_git_revision", lambda *a: entry.get("revision", ""))
    monkeypatch.setattr(registry, "_node_bin_dirs", lambda *a: [])
    monkeypatch.setattr(registry, "_read_state", lambda: {})
    monkeypatch.setattr(registry, "_format_argv", lambda command, item: [
        "python3.12" if arg == "{runtime_python}" else arg for arg in command])
    calls = []
    monkeypatch.setattr(registry, "_run_probe_command", lambda command, *a, **k: (calls.append(command) or True, "ok"))
    row = registry.probe(entry["key"])
    assert calls == [registry._format_argv(entry["probe"]["command"], entry)] and row["available"]
    # Same-key replacement cannot inherit the original probe authority.
    modified = {**entry, "probe": {"method": "command", "command": ["python3.12", "-c", "print('escape')"]}}
    registry._save_catalog([modified])
    assert registry.probe(entry["key"])["status"] == "requires_managed_probe"
    assert len(calls) == 1


def test_onboard_managed_origin_survives_same_key_restore(onboard_modules, monkeypatch):
    import sys
    from types import SimpleNamespace
    registry, _, _ = onboard_modules
    entry = json.loads((registry.HERE / "external_agents.json").read_text())["agents"][0]
    registry._save_catalog([entry])
    monkeypatch.setitem(sys.modules, "agent_tool_provider", SimpleNamespace(
        _bound_native_onboard=lambda: SimpleNamespace(check=lambda: None)))
    registry._save_catalog([{**entry, "name": "user replacement"}])
    registry._save_catalog([entry])
    monkeypatch.setitem(sys.modules, "agent_tool_provider", SimpleNamespace(_bound_native_onboard=lambda: None))
    assert not registry._host_probe_trusted(entry)
    assert registry.probe(entry["key"])["status"] == "requires_managed_probe"


def test_onboard_lease_probe_does_not_poison_host_list_cache(onboard_modules, monkeypatch):
    import sys
    from types import SimpleNamespace
    registry, _, _ = onboard_modules
    entry = {"key": "__ut_lease_cache", "probe": {"method": "command", "command": ["python3.12", "-V"]}}
    registry._save_catalog([entry])
    monkeypatch.setitem(sys.modules, "agent_tool_provider", SimpleNamespace(
        _bound_native_onboard=lambda: SimpleNamespace(check=lambda: None)))
    original = registry._probe_one
    monkeypatch.setattr(registry, "_probe_one", lambda *a: {"status": "ready", "execution_ready": True})
    assert registry.probe()["summary"]["execution_ready"] == 1
    assert registry._PROBE_CACHE["data"] is None
    monkeypatch.setattr(registry, "_probe_one", original)
    monkeypatch.setitem(sys.modules, "agent_tool_provider", SimpleNamespace(_bound_native_onboard=lambda: None))
    assert registry.probe()["agents"][0]["status"] == "requires_managed_probe"
