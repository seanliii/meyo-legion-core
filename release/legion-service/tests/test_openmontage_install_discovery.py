"""OpenMontage wiring only: synthetic sources, no installation or provider calls."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest


SERVICE = Path(__file__).resolve().parents[1]
REVISION = "1" * 40
UPSTREAM_REVISION = "cd9f3c1f03368be87b140af494914b8ee4e3c7a4"
ALIASES = ("repo:openmontage", "km:24:openmontage", "external-agent:openmontage")
_REAL_POPEN = subprocess.Popen


def forbidden(*args, **kwargs):
    raise AssertionError("this wiring test must not install, spawn or call a provider")


def source_fixture(root: Path, revision: str = REVISION, contents=None) -> Path:
    """A tiny verified source archive, not copied upstream code or a fake .git."""
    files = {}
    for relative in (
        "tools/__init__.py", "tools/base_tool.py", "tools/tool_registry.py",
        "tools/video/__init__.py", "tools/video/video_selector.py", "lib/scoring.py",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = (contents or {}).get(
            relative, "# Synthetic OpenMontage source fixture; never a real provider.\n",
        ).encode()
        path.write_bytes(raw)
        files[relative] = {"sha256": hashlib.sha256(raw).hexdigest()}
    (root / ".meyo-source.json").write_text(json.dumps({
        "revision": revision, "files": files,
    }))
    return root


@pytest.fixture(autouse=True)
def case(tmp_path, monkeypatch):
    for variable in (
        "HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "LEGION_EXTERNAL_SOURCE_ROOT",
        "LEGION_EXTERNAL_RUNTIME_ROOT", "LEGION_PROC_REGISTRY_DIR",
    ):
        path = tmp_path / variable.lower()
        path.mkdir()
        monkeypatch.setenv(variable, str(path))
    monkeypatch.delenv("MEYO_RUNTIME_ROOT", raising=False)
    monkeypatch.setenv("LEGION_EXT_AUTO_UPDATE", "0")
    monkeypatch.setenv("LEGION_GATEWAY_RETRIES", "0")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.syspath_prepend(str(SERVICE))

    registry = importlib.import_module("external_agent_registry")
    installs = importlib.import_module("install_registry")
    media = importlib.import_module("media_tools")
    sources = Path(os.environ["LEGION_EXTERNAL_SOURCE_ROOT"])
    current = source_fixture(sources / "openmontage")
    old_repo = tmp_path / "unrelated-old-checkout"
    old_source = source_fixture(old_repo / "harness_baselines/ecosystem/OpenMontage")
    spec = {
        "key": "openmontage", "name": "OpenMontage fixture", "kind": "capability",
        "url": "https://github.com/calesthio/OpenMontage",
        "version": REVISION, "revision": REVISION, "execution_role": "capability",
        "local_source": str(old_source), "capabilities": ["video", "media", "generation"],
        "install": [], "credentials": {}, "transports": [],
        "probe": {"method": "path", "target": "{source}/tools/video/video_selector.py"},
    }
    catalog = tmp_path / "external-agents-fixture.json"
    catalog.write_text(json.dumps({"agents": [spec]}))
    monkeypatch.setattr(registry, "CATALOG", catalog)
    monkeypatch.setattr(registry, "_STATE_PATH", tmp_path / "external-state.json")
    monkeypatch.setattr(registry, "_LOG_DIR", tmp_path / "external-logs")
    monkeypatch.setattr(registry, "_PROBE_CACHE", {"data": None, "ts": 0.0})
    monkeypatch.setattr(registry, "_node_bin_dirs", lambda item: [])
    monkeypatch.setattr(media, "HERE", str(old_repo / "release/legion-service"))
    monkeypatch.setattr(media, "_MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setattr(media, "_load_video_registry", forbidden)
    monkeypatch.setattr(media, "seedance_gateway_video", forbidden)
    monkeypatch.setattr(installs, "_repo_root", lambda: str(old_repo))
    for name in ("_OFFICIAL_PATH", "_REG_PATH", "_MANIFEST_PATH"):
        monkeypatch.setattr(installs, name, str(tmp_path / (name + ".json")))
    monkeypatch.setattr(installs, "_LEGION_DIR", str(tmp_path / "install-state"))
    monkeypatch.setattr(installs, "BUILTINS", [
        dict(item) for item in installs.BUILTINS if item["id"] == "repo:openmontage"
    ])
    monkeypatch.setattr(installs, "_ARSENAL_CACHE", {"items": None})
    monkeypatch.setattr(installs, "_SCAN_CACHE", {})
    monkeypatch.setattr(installs, "_INSTALLED_CACHE", {})
    monkeypatch.setattr(installs, "_SCAN_REFRESHING", set())
    # Preserve the actual legacy catalogue item, but never scan unrelated tools.
    arsenal = json.loads((SERVICE / "arsenal_catalog.json").read_text())
    old_item = next(item for item in arsenal["items"] if item["id"] == "km:24:openmontage")
    monkeypatch.setattr(installs, "load_arsenal", lambda: {"items": [old_item]})
    return SimpleNamespace(
        root=tmp_path, sources=sources, current=current, legacy=old_source,
        registry=registry, installs=installs, media=media, spec=spec, catalog=catalog,
    )


def test_declared_personal_source_wins_over_existing_old_checkout(case):
    assert case.media._openmontage_dir() == str(case.registry.source_dir(case.spec))
    assert case.media._openmontage_dir() == str(case.current)


def test_declared_source_map_is_shared_by_installation_and_consumption(case):
    mapped = source_fixture(case.sources / "generations/v1/openmontage")
    (case.sources / "source-map.json").write_text(json.dumps({"sources": {
        "openmontage": {"path": "generations/v1/openmontage", "revision": REVISION},
    }}))
    assert case.media._openmontage_dir() == str(mapped)
    item = next(item for item in case.installs.all_items() if item["id"] == ALIASES[0])
    assert item["check"]["target"] == str(mapped)
    assert case.installs.is_installed(item)


@pytest.mark.parametrize("damage", ["json", "revision", "escape", "symlink"])
def test_bad_source_map_fails_closed_without_loading_legacy_source(case, damage):
    mapping = case.sources / "source-map.json"
    if damage == "json":
        mapping.write_text("{")
    elif damage == "symlink":
        target = case.root / "foreign-map.json"
        target.write_text('{"sources": {}}')
        mapping.symlink_to(target)
    else:
        mapping.write_text(json.dumps({"sources": {"openmontage": {
            "path": "../unrelated-old-checkout/openmontage" if damage == "escape" else "openmontage",
            "revision": "2" * 40 if damage == "revision" else REVISION,
        }}}))
    status = case.media.video_provider_status()
    assert status["ok"] is False
    assert status["providers"] == []
    assert "OpenMontage" in status["error"]
    assert case.media.gen_video("must not reach a provider")["ok"] is False
    item = next(item for item in case.installs.all_items() if item["id"] == ALIASES[0])
    assert not case.installs.is_installed(item)


@pytest.mark.parametrize("damage", ["deleted", "missing_file", "wrong_revision", "empty"])
def test_missing_or_unverified_personal_source_is_not_available(case, damage):
    if damage in ("deleted", "empty"):
        shutil.rmtree(case.current)
        if damage == "empty":
            case.current.mkdir()
    elif damage == "missing_file":
        (case.current / "tools/video/video_selector.py").unlink()
    else:
        marker = case.current / ".meyo-source.json"
        data = json.loads(marker.read_text())
        data["revision"] = "2" * 40
        marker.write_text(json.dumps(data))
    assert case.media.video_provider_status()["ok"] is False
    assert case.media.gen_video("must not reach a provider")["ok"] is False
    item = next(item for item in case.installs.all_items() if item["id"] == ALIASES[0])
    assert not case.installs.is_installed(item)


def test_missing_declaration_does_not_adopt_a_legacy_checkout(case):
    case.catalog.write_text('{"agents": []}')
    assert case.media.video_provider_status()["ok"] is False
    assert case.media.gen_video("no implicit registration")["ok"] is False
    item = next(item for item in case.installs.all_items() if item["id"] == ALIASES[0])
    assert not case.installs.is_installed(item)
    assert not item.get("external_agent_key")


def test_install_centre_exposes_one_real_source_item_not_three_conflicting_rows(case):
    items = [item for item in case.installs.all_items()
             if item["id"] in ALIASES or item.get("external_agent_key") == "openmontage"]
    assert len(items) == 1
    item = items[0]
    assert item["id"] == "repo:openmontage"
    assert item["external_agent_key"] == "openmontage"
    assert item["check"]["target"] == case.media._openmontage_dir()
    assert "已随完整仓库" not in item.get("note", "")
    assert item["execution_role"] == "capability"
    assert not item["runnable"]


@pytest.mark.parametrize("item_id", ALIASES)
def test_install_aliases_reuse_real_registry_dry_run_without_installing(case, item_id):
    # Empty destination: registry must produce its ordinary fixed-revision plan.
    shutil.rmtree(case.current)
    result = case.installs.install(item_id, confirm=False, timeout_s=7)
    assert result["ok"], result
    assert result["status"] == "planned"
    assert result["destination"] == str(case.current)
    assert result["revision"] == REVISION
    assert result["commands"][0] == [
        "git", "clone", "--filter=blob:none", "--no-checkout",
        "https://github.com/calesthio/OpenMontage.git", str(case.current),
    ]
    assert not case.current.exists(), "planning must not create or install a source"
    assert not (case.root / "external-state.json").exists()


def test_explicit_install_delegates_confirmation_and_timeout_once(case, monkeypatch):
    calls = []

    def boundary(key, **kwargs):
        calls.append((key, kwargs))
        return {"ok": True, "status": "fixture_install_result"}

    monkeypatch.setattr(case.registry, "install", boundary)
    result = case.installs.install("repo:openmontage", confirm=True, timeout_s=19)
    assert result == {"ok": True, "status": "fixture_install_result"}
    assert calls == [("openmontage", {"confirm": True, "timeout_s": 19})]


def test_discovery_and_installation_checks_never_implicitly_install(case, monkeypatch):
    monkeypatch.setattr(case.registry, "install", forbidden)
    assert case.media._openmontage_dir() == str(case.current)
    item = next(item for item in case.installs.all_items() if item["id"] == ALIASES[0])
    assert case.installs.is_installed(item)


def test_cached_install_check_does_not_hide_source_loss(case):
    item = next(item for item in case.installs.all_items() if item["id"] == ALIASES[0])
    assert case.installs.is_installed(item)
    shutil.rmtree(case.current)
    assert not case.installs.is_installed(item)


def test_cached_scan_refreshes_openmontage_when_source_root_changes(case, monkeypatch):
    first = case.installs.scan("video")
    item = next(item for item in first["items"] if item["id"] == ALIASES[0])
    assert item["installed"]
    other = case.root / "another-personal-source-root"
    other.mkdir()
    monkeypatch.setenv("LEGION_EXTERNAL_SOURCE_ROOT", str(other))
    second = case.installs.scan("video")
    item = next(item for item in second["items"] if item["id"] == ALIASES[0])
    assert item["check"]["target"] == str(other / "openmontage")
    assert not item["installed"]
    assert second["summary"]["installed"] == 0
    assert first["items"][0]["installed"], "refresh must not mutate an earlier response"


def test_explicit_legacy_declaration_remains_usable_without_personal_override(case, monkeypatch):
    monkeypatch.delenv("LEGION_EXTERNAL_SOURCE_ROOT")
    assert case.media._openmontage_dir() == str(case.legacy)
    item = next(item for item in case.installs.all_items() if item["id"] == ALIASES[0])
    assert item["check"]["target"] == str(case.legacy)
    assert case.installs.is_installed(item)


def test_foreign_cached_python_package_is_rejected_before_discovery(case, monkeypatch):
    # Restore just this function; the fake foreign registry must never be used.
    spec = importlib.util.spec_from_file_location("_openmontage_loader_test", SERVICE / "media_tools.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    foreign = ModuleType("tools")
    foreign.__file__ = str(case.legacy / "tools/__init__.py")
    foreign.__path__ = [str(case.legacy / "tools")]
    registry_module = ModuleType("tools.tool_registry")
    registry_module.registry = SimpleNamespace(ensure_discovered=forbidden)
    monkeypatch.setitem(sys.modules, "tools", foreign)
    monkeypatch.setitem(sys.modules, "tools.tool_registry", registry_module)
    with pytest.raises(RuntimeError, match="OpenMontage.*source"):
        module._load_video_registry(str(case.current))


def test_scene_install_plans_openmontage_once(case):
    shutil.rmtree(case.current)
    shutil.rmtree(case.legacy)
    result = case.installs.install_scene("video", confirm=False)
    assert result["ok"], result
    assert result["count"] == 1
    assert result["results"][0]["destination"] == str(case.current)
    assert not case.current.exists()


@pytest.fixture
def clean_source_imports(monkeypatch):
    def selected(name):
        return name in ("tools", "lib") or name.startswith(("tools.", "lib."))

    for name in list(sys.modules):
        if selected(name):
            monkeypatch.delitem(sys.modules, name)
    yield
    for name in list(sys.modules):
        if selected(name):
            del sys.modules[name]


def actual_media_module(case):
    spec = importlib.util.spec_from_file_location("_openmontage_media_flow", SERVICE / "media_tools.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.HERE = str(case.legacy.parents[2] / "release/legion-service")
    module._MEDIA_DIR = str(case.root / "media")
    return module


def test_discovery_imports_only_the_selected_source(case, monkeypatch, clean_source_imports):
    # Exercise real source resolution + Python loading, not the fixture's forbidden loader.
    source_fixture(case.current, contents={"tools/tool_registry.py": """
from types import SimpleNamespace
class Registry:
    def __init__(self): self.local = {}
    def ensure_discovered(self): pass
    def get(self, name): return self.local.get(name)
    def register(self, tool): self.local[tool.name] = tool
    def get_by_capability(self, capability):
        return [SimpleNamespace(name='selected_source_fixture', provider='fixture',
                get_status=lambda: SimpleNamespace(value='unavailable'),
                capabilities=['image_to_video'], dependencies=['fixture_missing_dependency'])]
registry = Registry()
"""})
    (case.legacy / "tools/__init__.py").write_text("raise AssertionError('legacy source imported')\n")
    fallback = ModuleType("openmontage_local_provider")
    fallback.LegionStillMotionVideo = type("LocalFixture", (), {"name": "local_fixture"})
    monkeypatch.setitem(sys.modules, "openmontage_local_provider", fallback)
    result = actual_media_module(case).video_provider_status()
    assert result["ok"], result
    assert result["providers"][0]["name"] == "selected_source_fixture"
    assert result["available"] == [], "installed source must not invent an available provider"
    assert Path(sys.modules["tools.tool_registry"].__file__).parent.parent == case.current
    assert "local_fixture" in sys.modules["tools.tool_registry"].registry.local


def test_source_ready_does_not_hide_missing_provider_dependencies(case, clean_source_imports):
    source_fixture(case.current, contents={
        "tools/tool_registry.py": "raise ModuleNotFoundError('fixture_optional_dependency')\n",
    })
    item = next(item for item in case.installs.all_items() if item["id"] == ALIASES[0])
    assert case.installs.is_installed(item), "this assertion concerns source, not provider readiness"
    result = actual_media_module(case).video_provider_status()
    assert not result["ok"]
    assert result["providers"] == []
    assert "ModuleNotFoundError" in result["error"]


def test_foreign_cached_scoring_package_is_rejected(case, monkeypatch, clean_source_imports):
    tools = ModuleType("tools")
    tools.__file__ = str(case.current / "tools/__init__.py")
    tools.__path__ = [str(case.current / "tools")]
    registry_module = ModuleType("tools.tool_registry")
    registry_module.__file__ = str(case.current / "tools/tool_registry.py")
    registry_module.registry = SimpleNamespace(ensure_discovered=forbidden)
    foreign = ModuleType("lib")
    foreign.__file__ = str(case.legacy / "lib/__init__.py")
    foreign.__path__ = [str(case.legacy / "lib")]
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.tool_registry", registry_module)
    monkeypatch.setitem(sys.modules, "lib", foreign)
    with pytest.raises(RuntimeError, match="OpenMontage.*source"):
        actual_media_module(case)._load_video_registry(str(case.current))


def test_real_declaration_is_pinned_optional_and_not_a_company_model_binding():
    items = json.loads((SERVICE / "external_agents.json").read_text())["agents"]
    item = next((item for item in items if item["key"] == "openmontage"), None)
    assert item is not None, "OpenMontage needs a verified declaration via _upsert_catalog"
    assert item["url"] == "https://github.com/calesthio/OpenMontage"
    assert item["revision"] == UPSTREAM_REVISION
    assert item["execution_role"] == "capability"
    assert not item.get("transports")
    assert not item.get("credentials")
    assert "AGPL-3.0" in str(item.get("license"))
    assert not any(text in json.dumps(item) for text in ("sankuai", "/Users/", "models.json"))


@pytest.fixture
def native_checker(case, monkeypatch):
    """Real Python children, copied trusted runtime, synthetic sources and no secrets."""
    service = case.root / "runtime-package/release/legion-service"
    service.mkdir(parents=True)
    for name in (
        "install_registry.py", "media_tools.py", "source_audit.py", "legion_paths.py",
        "external_agent_registry.py", "external_agent_transports.py", "agent_manifest.py",
    ):
        shutil.copyfile(SERVICE / name, service / name)
    entry = next(item for item in json.loads((SERVICE / "external_agents.json").read_text())["agents"]
                 if item["key"] == "openmontage")
    catalog = service / "external_agents.json"
    catalog.write_text(json.dumps({"agents": [entry]}))
    monkeypatch.setattr(case.registry, "HERE", service)
    monkeypatch.setattr(case.registry, "CATALOG", catalog)
    source_fixture(case.current, revision=UPSTREAM_REVISION)
    environment = {
        name: os.environ[name] for name in (
            "HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "LEGION_EXTERNAL_SOURCE_ROOT",
            "LEGION_EXTERNAL_RUNTIME_ROOT", "LEGION_PROC_REGISTRY_DIR",
        )
    }
    environment.update(
        PATH=os.pathsep.join((str(Path(sys.executable).parent), "/usr/bin", "/bin")),
        PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1", LEGION_EXT_AUTO_UPDATE="0",
        LEGION_GATEWAY_RETRIES="0", GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_NOSYSTEM="1",
    )
    children, calls = [], []

    def python_only(command, *args, **kwargs):
        assert not kwargs.get("shell")
        assert Path(command[0]).resolve() == Path(sys.executable).resolve(), command
        assert Path(kwargs["cwd"]).resolve().is_relative_to(case.root.resolve())
        child_env = dict(kwargs.get("env") or environment)
        for name in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR"):
            assert Path(child_env[name]).resolve().is_relative_to(case.root.resolve())
        assert not any(name in child_env for name in ("OPENAI_API_KEY", "MEYO_UNIFIED_APPID"))
        kwargs["env"] = child_env
        kwargs["start_new_session"] = True
        process = _REAL_POPEN(command, *args, **kwargs)
        children.append(process)
        calls.append(list(command))
        return process

    monkeypatch.setattr(subprocess, "Popen", python_only)

    def run(endpoint, workdir=""):
        if endpoint == "build-probe":
            return case.registry.probe_build(entry)
        return case.registry.run_capability(
            "openmontage", "source-check", isolated_environment=True,
            workdir=str(workdir), env=environment,
        )

    yield SimpleNamespace(
        run=run, service=service, catalog=catalog, entry=entry,
        env=environment, calls=calls,
    )
    for process in children:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def _check_ok(result):
    return result.get("ok", result.get("available"))


def _check_output(result):
    return "\n".join(str(result.get(key) or "") for key in (
        "stdout", "stderr", "status_detail", "error",
    ))


def test_declared_source_check_uses_native_capability_and_no_provider(case, native_checker):
    result = native_checker.run("source-check")
    assert result["ok"], result
    assert result["stdout"] == "OpenMontage source syntax OK; providers not tested\n"
    assert len(native_checker.calls) == 1


@pytest.mark.parametrize("endpoint", ["source-check", "build-probe"])
def test_source_check_real_child_accepts_valid_source(case, native_checker, endpoint):
    result = native_checker.run(endpoint)
    assert _check_ok(result), result
    assert "OpenMontage source syntax OK" in _check_output(result)
    assert len(native_checker.calls) == 1


@pytest.mark.parametrize("endpoint,cwd_kind", [
    ("source-check", "source"), ("source-check", "workdir"), ("build-probe", "source"),
])
def test_source_check_real_child_does_not_import_cwd_ast(
    case, native_checker, endpoint, cwd_kind,
):
    source_fixture(case.current, revision=UPSTREAM_REVISION, contents={
        "tools/video/video_selector.py": "def INVALID_SYNTAX(\n",
    })
    workdir = case.current if cwd_kind == "source" else case.root / "bound-workdir"
    workdir.mkdir(exist_ok=True)
    marker = case.root / "synthetic-shadow-imported"
    (workdir / "ast.py").write_text(
        f"open({str(marker)!r}, 'w').write('synthetic only')\n"
        "def parse(*args, **kwargs): return None\n"
    )
    result = native_checker.run(endpoint, workdir=workdir)
    assert not marker.exists(), f"untrusted cwd ast.py executed: {result!r}"
    assert not _check_ok(result), f"invalid source syntax was accepted: {result!r}"
    assert len(native_checker.calls) == 1


@pytest.mark.parametrize("endpoint", ["source-check", "build-probe"])
def test_source_check_real_child_rejects_wrong_identity(case, native_checker, endpoint):
    marker = case.current / ".meyo-source.json"
    data = json.loads(marker.read_text())
    data["revision"] = "2" * 40
    marker.write_text(json.dumps(data))
    assert not case.installs.openmontage_source_status()["ok"]
    result = native_checker.run(endpoint)
    assert not _check_ok(result), f"unverified source was accepted: {result!r}"
    assert "OpenMontage source syntax OK" not in _check_output(result)


@pytest.mark.parametrize("endpoint", ["source-check", "build-probe"])
def test_source_check_real_child_never_reads_external_symlink(case, native_checker, endpoint):
    canary = case.root / "outside-synthetic-source.txt"
    canary.write_text("OUTSIDE_REVIEW_CANARY = (\n")
    target = case.current / "lib/scoring.py"
    target.unlink()
    target.symlink_to(canary)
    assert not case.installs.openmontage_source_status()["ok"]
    result = native_checker.run(endpoint)
    assert not _check_ok(result), result
    assert "OUTSIDE_REVIEW_CANARY" not in _check_output(result), result


def test_source_check_real_child_rejects_substituted_root(case, native_checker):
    other = source_fixture(case.root / "other-source", revision=UPSTREAM_REVISION)
    command = case.registry._format_argv(
        native_checker.entry["capability_actions"]["source-check"]["command"],
        native_checker.entry,
    )
    assert command[-1] == str(case.current)
    command[-1] = str(other)
    result = subprocess.run(
        command, cwd=str(case.root), env=native_checker.env,
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0, "checker adopted an undeclared source argument"
    assert "OpenMontage source syntax OK" not in result.stdout


_SYNTHETIC_REGISTRY = """
from types import SimpleNamespace
class Registry:
    def __init__(self): self.local = {}
    def ensure_discovered(self): pass
    def get(self, name): return self.local.get(name)
    def register(self, tool): self.local[tool.name] = tool
    def get_by_capability(self, capability):
        return [SimpleNamespace(name='old_revision_synthetic_provider', provider='fixture',
                get_status=lambda: SimpleNamespace(value='available'),
                capabilities=['image_to_video'], dependencies=[])]
registry = Registry()
"""

_REVISION_CHILD = """
import hashlib, json, os, pathlib, socket, sys
from types import ModuleType
def forbidden(*args, **kwargs):
    raise AssertionError('no network or provider calls in this fixture')
socket.socket.connect = socket.create_connection = socket.getaddrinfo = forbidden
service, source, mode = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
sys.path.insert(0, str(service))
import install_registry, media_tools
media_tools.seedance_gateway_video = forbidden
fallback = ModuleType('openmontage_local_provider')
fallback.LegionStillMotionVideo = type('LocalFixture', (), {'name': 'local_fixture'})
sys.modules['openmontage_local_provider'] = fallback
before = media_tools.video_provider_status()
original = sys.modules.get('tools.tool_registry')
if mode != 'unchanged':
    target = source / 'tools/tool_registry.py'
    target.write_text("raise ModuleNotFoundError('new_revision_fixture_dependency')\\n")
    marker = source / '.meyo-source.json'
    data = json.loads(marker.read_text())
    data['files']['tools/tool_registry.py']['sha256'] = hashlib.sha256(target.read_bytes()).hexdigest()
    if mode == 'revision':
        data['revision'] = '2' * 40
        catalog = service / 'external_agents.json'
        entry = json.loads(catalog.read_text())
        entry['agents'][0]['revision'] = data['revision']
        entry['agents'][0]['version'] = data['revision']
        catalog.write_text(json.dumps(entry))
    marker.write_text(json.dumps(data))
state = install_registry.openmontage_source_status()
after = media_tools.video_provider_status()
print(json.dumps({'before': before, 'source': state, 'after': after,
                  'module_retained': sys.modules.get('tools.tool_registry') is original}))
"""


@pytest.mark.parametrize("change", ["revision", "content", "unchanged"])
def test_registry_real_child_binds_cache_to_source_identity(case, native_checker, change):
    source_fixture(case.current, revision=UPSTREAM_REVISION, contents={
        "tools/tool_registry.py": _SYNTHETIC_REGISTRY,
    })
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", _REVISION_CHILD,
         str(native_checker.service), str(case.current), change],
        cwd=str(case.root), env=native_checker.env, capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["before"]["ok"], result
    assert result["before"]["available"][0]["name"] == "old_revision_synthetic_provider"
    assert result["source"]["ok"], "replacement is a valid synthetic source, not a missing install"
    assert result["module_retained"], "product must not remove somebody else's modules"
    if change == "unchanged":
        assert result["after"] == result["before"], result
    else:
        assert not result["after"]["ok"], f"stale module was accepted: {result!r}"
        assert result["after"]["providers"] == []
        assert "重启" in result["after"]["error"], result
