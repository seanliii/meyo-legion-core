"""Offline profile regressions: temporary instances and fake credentials only."""
import importlib
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys

import pytest

SERVICE = Path(__file__).resolve().parents[1]
ENTRY = SERVICE.parents[1] / "meyo.py"
LIBRARY = "secrets/model-profiles.json"


def test_files_transport_option_survives_profile_activation_and_rollback(runtime, root, tmp_path):
    original = legacy(runtime, root, tmp_path)
    option = {"profile": "deepseek-files-v1", "ttl_seconds": 3600, "default_detail": "auto"}
    runtime.save_model_profile(root, "files", "legacy-fixture", "https://legacy.example.invalid/v1",
                               key_file(tmp_path, "files"), image_file_cache=option)
    assert (root / "secrets/model.json").read_bytes() == original
    activation = runtime.activate_model_profile(root, "files")
    env, _ = runtime.environment(root, inherited={})
    binding = json.loads(env["LEGION_CODEX_RESPONSES_BINDING"])
    assert binding["image_file_cache"] == option
    assert "api_key" not in binding
    runtime.activate_model_profile(root, activation["rollback_profile"])
    assert (root / "secrets/model.json").read_bytes() == original


@pytest.mark.parametrize("command", ["configure-model", "save-model-profile"])
def test_files_transport_cli_saves_explicit_options(runtime, root, tmp_path, command):
    args = [command, "--state-dir", root, "--model", "files-fixture",
            "--base-url", "https://files.example.invalid/v1", "--api-key-file", key_file(tmp_path, "files-cli"),
            "--image-file-cache-ttl-seconds", "3600", "--image-file-cache-default-detail", "auto"]
    if command == "save-model-profile":
        args += ["--id", "files"]
    rc, out, err = cli(tmp_path, *args)
    assert rc == 0, err
    saved = json.loads((root / ("secrets/model.json" if command == "configure-model" else LIBRARY)).read_text())
    if command == "save-model-profile":
        saved = saved["files"]
    assert saved["image_file_cache"] == {"profile": "deepseek-files-v1", "ttl_seconds": 3600, "default_detail": "auto"}
    assert "fake-profile-key-files-cli" not in out + err


def test_explicit_binding_preserves_declared_inputs_without_verification(runtime):
    registry = importlib.import_module("model_registry")
    binding = registry.explicit_responses_binding({
        "LEGION_CODEX_RESPONSES_BINDING": json.dumps({
            "model": "vision-fixture", "base_url": "https://vision.example.invalid/v1",
            "api_key_env": "TEST_MODEL_KEY", "input_modalities": ["text", "image"],
        }),
        "TEST_MODEL_KEY": "fake-profile-capability-key",
    })
    assert binding.get("input_modalities") == ["text", "image"]
    assert "verified" not in binding


def test_declared_inputs_survive_profile_activation_environment_and_rollback(
        runtime, root, tmp_path, monkeypatch):
    original = legacy(runtime, root, tmp_path)
    result = runtime.save_model_profile(
        root, "vision", "vision-fixture", "https://vision.example.invalid/v1",
        key_file(tmp_path, "vision"), input_modalities=["text", "image"],
    )
    assert not result["provider_verified"]
    assert (root / "secrets/model.json").read_bytes() == original
    activation = runtime.activate_model_profile(root, "vision")
    env, _ = runtime.environment(root, inherited={})
    public_binding = json.loads(env["LEGION_CODEX_RESPONSES_BINDING"])
    assert public_binding["input_modalities"] == ["text", "image"]
    assert "api_key" not in public_binding
    monkeypatch.setenv("LEGION_CODEX_TRANSPORT", "responses")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    registry = importlib.import_module("model_registry")
    assert set(registry.input_modalities("vision-fixture")) == {"text", "image"}
    assert registry.input_modalities("different-fixture") is None
    status = registry.explicit_responses_status("codex")
    assert set(status["input_modalities"]) == {"text", "image"}
    assert status["verified"] is False
    assert "fake-profile-key-vision" not in json.dumps(status)
    runtime.activate_model_profile(root, activation["rollback_profile"])
    assert (root / "secrets/model.json").read_bytes() == original


@pytest.mark.parametrize("modalities", [
    [], "image", ["image"], ["text", "video"], ["text", 1], None,
])
def test_malformed_declared_inputs_are_not_silently_accepted(runtime, modalities):
    registry = importlib.import_module("model_registry")
    with pytest.raises(ValueError):
        registry.explicit_responses_binding({
            "LEGION_CODEX_RESPONSES_BINDING": json.dumps({
                "model": "input-fixture", "base_url": "https://input.example.invalid/v1",
                "api_key_env": "TEST_MODEL_KEY", "input_modalities": modalities,
            }),
            "TEST_MODEL_KEY": "fake-profile-input-key",
        })


def test_personal_unknown_inputs_do_not_borrow_company_metadata(
        runtime, root, tmp_path, monkeypatch):
    legacy(runtime, root, tmp_path)
    env, _ = runtime.environment(root, inherited={})
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    registry = importlib.import_module("model_registry")

    def no_company_lookup(*args):
        raise AssertionError("personal binding must not borrow unrelated model capabilities")

    monkeypatch.setattr(registry, "_raw_candidate", no_company_lookup)
    assert registry.input_modalities("legacy-fixture") is None
    assert registry.input_modalities("another-model") is None


def test_configure_model_cli_accepts_explicit_inputs(runtime, root, tmp_path):
    key = key_file(tmp_path, "vision-cli")
    rc, out, err = cli(
        tmp_path, "configure-model", "--state-dir", root, "--model", "vision-cli",
        "--base-url", "https://vision.example.invalid/v1", "--api-key-file", key,
        "--input-modality", "text", "--input-modality", "image",
    )
    assert rc == 0, err
    saved = json.loads((root / "secrets/model.json").read_text())
    assert saved["input_modalities"] == ["text", "image"]
    assert "fake-profile-key-vision-cli" not in out + err


def test_invalid_capability_write_leaves_active_configuration_unchanged(
        runtime, root, tmp_path):
    original = legacy(runtime, root, tmp_path)
    with pytest.raises(ValueError):
        runtime.configure_model(
            root, "invalid-fixture", "https://vision.example.invalid/v1",
            key_file(tmp_path, "invalid"), input_modalities=["text", "video"],
        )
    assert (root / "secrets/model.json").read_bytes() == original


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SERVICE))
    for name in ("HOME", "CODEX_HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR"):
        folder = tmp_path / name.lower()
        folder.mkdir(mode=0o700)
        monkeypatch.setenv(name, str(folder))

    def offline(*args, **kwargs):
        raise AssertionError("This regression must not access the network")

    monkeypatch.setattr(socket.socket, "connect", offline)
    monkeypatch.setattr(socket.socket, "connect_ex", offline)
    monkeypatch.setattr(socket, "getaddrinfo", offline)


@pytest.fixture
def runtime():
    return importlib.import_module("personal_runtime")


@pytest.fixture
def root(runtime, tmp_path):
    path = tmp_path / "instance"
    runtime.initialize(path, "profile-test", 18080)
    return path


def key_file(tmp_path, name="legacy", value=None):
    path = tmp_path / f"{name}.key"
    path.write_text(value if value is not None else f"fake-profile-key-{name}\n")
    path.chmod(0o600)
    return path


def save(runtime, root, tmp_path, name, **kwargs):
    return runtime.save_model_profile(
        root, name, kwargs.get("model", f"{name}-fixture"),
        kwargs.get("base_url", f"https://{name}.example.invalid/v1"),
        kwargs["key"] if "key" in kwargs else key_file(tmp_path, name),
    )


def legacy(runtime, root, tmp_path):
    runtime.configure_model(root, "legacy-fixture", "https://legacy.example.invalid/v1",
                            key_file(tmp_path))
    return (root / "secrets/model.json").read_bytes()


def read_library(root):
    return json.loads((root / LIBRARY).read_text())


def cli(tmp_path, *arguments):
    # Execute the actual meyo.py entry, with no inherited user/provider settings.
    # An audit hook also blocks unexpected networking or child tool/model starts.
    code = (
        "import runpy, sys\n"
        "def offline(event, args):\n"
        "    if event.startswith('socket.') or event in ('subprocess.Popen', 'os.system'):\n"
        "        raise AssertionError('offline CLI regression')\n"
        "sys.addaudithook(offline)\n"
        "sys.argv = sys.argv[1:]\n"
        "runpy.run_path(sys.argv[0], run_name='__main__')\n"
    )
    environment = {
        "HOME": str(tmp_path), "PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1",
        "MEYO_LEGION_DIR": str(tmp_path / "data"),
        "LEGION_HOME_DIR": str(tmp_path / "legion"),
    }
    process = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", code, str(ENTRY), *map(str, arguments)],
        env=environment, cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    try:
        out, err = process.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise
    assert "fake-profile-key-" not in out + err
    return process.returncode, out, err


def test_saving_two_providers_preserves_active_identity_sessions_and_permissions(
        runtime, root, tmp_path):
    before = legacy(runtime, root, tmp_path)
    session = root / "legion-home/sessions"
    session.mkdir()
    (session / "existing.json").write_text('{"history":"keep"}')
    protected = {p: p.read_bytes() for p in (
        root / "config.json", root / "login-token.txt",
        root / "secrets/personal-tokens.json", session / "existing.json",
    )}
    outputs = [save(runtime, root, tmp_path, name) for name in ("kimi", "deepseek")]
    assert (root / "secrets/model.json").read_bytes() == before
    assert all(p.read_bytes() == content for p, content in protected.items())
    assert set(read_library(root)) == {"kimi", "deepseek"}
    assert all(set(value) == {"model", "base_url", "api_key"}
               for value in read_library(root).values())
    assert stat.S_IMODE((root / LIBRARY).stat().st_mode) == 0o600
    assert stat.S_IMODE((root / "secrets").stat().st_mode) == 0o700
    assert all(row["provider_verified"] is False for row in outputs)
    assert "fake-profile-key-" not in repr(outputs)


def test_first_profile_does_not_create_an_active_binding(runtime, root, tmp_path):
    save(runtime, root, tmp_path, "openrouter")
    assert not (root / "secrets/model.json").exists()
    env, _ = runtime.environment(root, {})
    assert runtime.KEY_ENV not in env
    assert "LEGION_CODEX_RESPONSES_BINDING" not in env
    assert runtime.list_model_profiles(root) == {"profiles": [{
        "id": "openrouter", "model": "openrouter-fixture",
        "base_url": "https://openrouter.example.invalid/v1", "current": False,
    }]}
    result = runtime.activate_model_profile(root, "openrouter")
    assert result["changed"] and result["rollback_profile"] is None


def test_activation_and_explicit_rollback_keep_candidates_and_environment(
        runtime, root, tmp_path):
    old = json.loads(legacy(runtime, root, tmp_path))
    running, _ = runtime.environment(root, {})
    save(runtime, root, tmp_path, "kimi")
    save(runtime, root, tmp_path, "deepseek")
    candidates = read_library(root)
    inherited = dict(os.environ)
    result = runtime.activate_model_profile(root, "kimi")
    assert dict(os.environ) == inherited
    assert result["provider_verified"] is False
    rollback = result["rollback_profile"]
    assert rollback and read_library(root)[rollback] == old
    assert all(read_library(root)[name] == value for name, value in candidates.items())
    active, _ = runtime.environment(root, {})
    assert active[runtime.KEY_ENV] == "fake-profile-key-kimi"
    assert running[runtime.KEY_ENV] == "fake-profile-key-legacy"
    assert {row["id"] for row in runtime.list_model_profiles(root)["profiles"]
            if row["current"]} == {"kimi"}
    back = runtime.activate_model_profile(root, rollback)
    assert back["rollback_profile"] == "kimi"
    assert json.loads((root / "secrets/model.json").read_text()) == old
    assert set(read_library(root)) == {"kimi", "deepseek", rollback}
    assert "fake-profile-key-" not in repr((result, back, runtime.list_model_profiles(root)))


def test_backup_name_collision_never_overwrites_existing_candidate(
        runtime, root, tmp_path, monkeypatch):
    old = json.loads(legacy(runtime, root, tmp_path))
    save(runtime, root, tmp_path, "previous-collision")
    save(runtime, root, tmp_path, "kimi")
    before = read_library(root)
    original = runtime.secrets.token_hex
    suffixes = iter(("collision", "unused"))
    monkeypatch.setattr(runtime.secrets, "token_hex",
                        lambda n: next(suffixes) if n == 8 else original(n))
    result = runtime.activate_model_profile(root, "kimi")
    assert result["rollback_profile"] == "previous-unused"
    assert read_library(root)["previous-unused"] == old
    assert all(read_library(root)[name] == value for name, value in before.items())


def test_same_endpoint_with_different_key_is_not_current_and_reactivation_is_noop(
        runtime, root, tmp_path, monkeypatch):
    save(runtime, root, tmp_path, "original", model="same-model",
         base_url="https://same.example.invalid/v1/responses")
    save(runtime, root, tmp_path, "rotated", model="same-model",
         base_url="https://same.example.invalid/v1")
    runtime.activate_model_profile(root, "original")
    rows = runtime.list_model_profiles(root)["profiles"]
    assert {r["id"] for r in rows if r["current"]} == {"original"}
    assert all(r["base_url"] == "https://same.example.invalid/v1" for r in rows)
    result = runtime.activate_model_profile(root, "rotated")
    assert result["rollback_profile"] == "original"
    assert len(read_library(root)) == 2

    def no_write(*args):
        raise AssertionError("Reactivating the same binding should not write")

    monkeypatch.setattr(runtime, "_write", no_write)
    assert runtime.activate_model_profile(root, "rotated")["changed"] is False


def test_duplicate_id_and_unknown_activation_leave_all_configuration_unchanged(
        runtime, root, tmp_path):
    old = legacy(runtime, root, tmp_path)
    save(runtime, root, tmp_path, "kimi")
    before = (root / LIBRARY).read_bytes()
    with pytest.raises(ValueError):
        save(runtime, root, tmp_path, "kimi", model="replacement")
    with pytest.raises(ValueError):
        runtime.activate_model_profile(root, "missing")
    assert (root / LIBRARY).read_bytes() == before
    assert (root / "secrets/model.json").read_bytes() == old


@pytest.mark.parametrize("identifier", ["", "../escape", "/absolute", ".hidden", "a/b",
                                        "bad id", "x" * 65, "a\nb"])
def test_invalid_ids_do_not_write(runtime, root, tmp_path, identifier):
    old = legacy(runtime, root, tmp_path)
    with pytest.raises(ValueError):
        runtime.save_model_profile(root, identifier, "fixture",
                                   "https://model.example.invalid/v1", key_file(tmp_path))
    assert not (root / LIBRARY).exists()
    assert (root / "secrets/model.json").read_bytes() == old


@pytest.mark.parametrize("url", [
    "http://remote.example.invalid/v1",
    "https://model.example.invalid/v1/chat/completions",
    "https://name:fake-profile-key-url@model.example.invalid/v1",
    "https://model.example.invalid/v1?api_key=fake-profile-key-url",
    "https://model.example.invalid/v1#fragment",
])
def test_existing_url_rules_apply_without_secret_errors(runtime, root, tmp_path, url):
    old = legacy(runtime, root, tmp_path)
    with pytest.raises(ValueError) as error:
        save(runtime, root, tmp_path, "bad", base_url=url)
    assert "fake-profile-key-" not in str(error.value)
    assert not (root / LIBRARY).exists()
    assert (root / "secrets/model.json").read_bytes() == old


@pytest.mark.parametrize("value", ["", "fake-profile-key-first\nfake-profile-key-second", "\xff"])
def test_invalid_key_never_changes_active_or_leaks(runtime, root, tmp_path, value):
    old = legacy(runtime, root, tmp_path)
    key = key_file(tmp_path, "bad")
    key.write_bytes(value.encode("latin-1"))
    with pytest.raises((ValueError, UnicodeError)) as error:
        save(runtime, root, tmp_path, "bad", key=key)
    assert "fake-profile-key-" not in str(error.value)
    assert not (root / LIBRARY).exists()
    assert (root / "secrets/model.json").read_bytes() == old


@pytest.mark.parametrize("target", ["key", "library", "active", "secrets", "root", "lock"])
@pytest.mark.parametrize("dangling", [False, True])
def test_symlinks_are_not_followed_or_overwritten(
        runtime, root, tmp_path, target, dangling):
    old = legacy(runtime, root, tmp_path)
    save(runtime, root, tmp_path, "kimi")
    key = key_file(tmp_path, "incoming")
    paths = {"key": key, "library": root / LIBRARY, "active": root / "secrets/model.json",
             "secrets": root / "secrets", "root": root, "lock": root / ".configure.lock"}
    path = paths[target]
    stored = tmp_path / f"stored-{target}"
    path.rename(stored)
    destination = tmp_path / "missing" if dangling else stored
    path.symlink_to(destination, target_is_directory=target in ("root", "secrets"))
    snapshot = {p: p.read_bytes() for p in tmp_path.rglob("*")
                if p.is_file() and not p.is_symlink()}
    with pytest.raises((ValueError, OSError)):
        if target == "key":
            save(runtime, root, tmp_path, "incoming", key=key)
        elif target == "library":
            save(runtime, root, tmp_path, "incoming")
        else:
            runtime.activate_model_profile(root, "kimi")
    assert path.is_symlink()
    assert all(p.read_bytes() == content for p, content in snapshot.items())
    assert not (tmp_path / "missing").exists()
    if target not in ("root", "secrets", "active"):
        assert (root / "secrets/model.json").read_bytes() == old


@pytest.mark.parametrize("kind", ["bad_json", "bad_binding", "public", "too_large"])
def test_invalid_library_is_never_replaced(runtime, root, tmp_path, kind):
    old = legacy(runtime, root, tmp_path)
    save(runtime, root, tmp_path, "kimi")
    path = root / LIBRARY
    if kind == "bad_json":
        path.write_text('{"api_key":"fake-profile-key-in-broken-json",')
    elif kind == "bad_binding":
        path.write_text(json.dumps({"kimi": {"api_key": "fake-profile-key-incomplete"}}))
    elif kind == "public":
        path.chmod(0o644)
    else:
        path.write_text(" " * 32769)
    before = path.read_bytes()
    for action in (lambda: save(runtime, root, tmp_path, "deepseek"),
                   lambda: runtime.activate_model_profile(root, "kimi"),
                   lambda: runtime.list_model_profiles(root)):
        with pytest.raises((ValueError, OSError)) as error:
            action()
        assert "fake-profile-key-" not in str(error.value)
        assert path.read_bytes() == before
        assert (root / "secrets/model.json").read_bytes() == old


def test_backup_write_failure_does_not_change_active(runtime, root, tmp_path, monkeypatch):
    before = legacy(runtime, root, tmp_path)
    save(runtime, root, tmp_path, "kimi")
    write = runtime._write

    def fail_library(path, content):
        if path.name == "model-profiles.json":
            raise OSError("offline injected write failure")
        write(path, content)

    monkeypatch.setattr(runtime, "_write", fail_library)
    with pytest.raises(OSError):
        runtime.activate_model_profile(root, "kimi")
    assert (root / "secrets/model.json").read_bytes() == before


def test_atomic_binding_replace_failure_leaves_old_binding_and_reusable_backup(
        runtime, root, tmp_path, monkeypatch):
    before = legacy(runtime, root, tmp_path)
    save(runtime, root, tmp_path, "kimi")
    replace = os.replace

    def fail_active(source, destination, **kwargs):
        if destination == "model.json":
            raise OSError("offline injected atomic replace failure")
        return replace(source, destination, **kwargs)

    monkeypatch.setattr(os, "replace", fail_active)
    with pytest.raises(OSError):
        runtime.activate_model_profile(root, "kimi")
    assert (root / "secrets/model.json").read_bytes() == before
    backup = next(name for name, binding in read_library(root).items()
                  if binding == json.loads(before))
    assert not list((root / "secrets").glob(".meyo-*"))
    assert runtime.activate_model_profile(root, backup)["changed"] is False
    monkeypatch.setattr(os, "replace", replace)
    assert runtime.activate_model_profile(root, "kimi")["rollback_profile"] == backup
    assert len(read_library(root)) == 2


def test_growing_library_beyond_private_reader_limit_is_rejected(runtime, root, tmp_path):
    active = legacy(runtime, root, tmp_path)
    save(runtime, root, tmp_path, "kimi")
    before = (root / LIBRARY).read_bytes()
    with pytest.raises(ValueError):
        save(runtime, root, tmp_path, "oversized", model="m" * 32768)
    assert (root / LIBRARY).read_bytes() == before
    assert (root / "secrets/model.json").read_bytes() == active


def test_concurrent_saves_keep_both_candidates(runtime, root, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    active = legacy(runtime, root, tmp_path)
    inputs = [(name, key_file(tmp_path, name)) for name in ("kimi", "deepseek")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda pair: save(runtime, root, tmp_path, pair[0], key=pair[1]), inputs))
    assert all(row["saved"] for row in results)
    assert set(read_library(root)) == {"kimi", "deepseek"}
    assert (root / "secrets/model.json").read_bytes() == active


def test_cli_legacy_three_fields_named_profiles_and_redacted_errors(runtime, root, tmp_path):
    key = key_file(tmp_path, "cli")
    rc, out, err = cli(tmp_path, "configure-model", "--state-dir", root,
                       "--model", "legacy-fixture", "--base-url",
                       "https://legacy.example.invalid/v1", "--api-key-file", key)
    assert rc == 0 and not err
    assert json.loads(out)["configured"] is True
    old = (root / "secrets/model.json").read_bytes()
    assert set(json.loads(old)) == {"model", "base_url", "api_key"}
    rc, out, err = cli(tmp_path, "save-model-profile", "--state-dir", "~/instance",
                       "--id", "kimi", "--model", "kimi-fixture", "--base-url",
                       "https://kimi.example.invalid/v1", "--api-key-file", "~/cli.key")
    assert rc == 0 and not err and json.loads(out)["saved"]
    assert (root / "secrets/model.json").read_bytes() == old
    rc, out, err = cli(tmp_path, "list-model-profiles", "--state-dir", root)
    assert rc == 0 and not err
    assert json.loads(out)["profiles"][0]["current"] is False
    rc, out, err = cli(tmp_path, "activate-model-profile", "--state-dir", root, "--id", "kimi")
    assert rc == 0 and not err
    rollback = json.loads(out)["rollback_profile"]
    rc, out, err = cli(tmp_path, "activate-model-profile", "--state-dir", root, "--id", rollback)
    assert rc == 0 and not err
    assert (root / "secrets/model.json").read_bytes() == old
    library = (root / LIBRARY).read_bytes()
    rc, out, err = cli(tmp_path, "configure-model", "--state-dir", root,
                       "--model", "other-fixture", "--base-url",
                       "https://other.example.invalid/v1", "--api-key-file", key)
    assert rc == 0 and not err and json.loads(out)["configured"]
    assert (root / LIBRARY).read_bytes() == library
    rc, out, err = cli(tmp_path, "list-model-profiles", "--state-dir", root)
    assert rc == 0 and not err
    assert not any(row["current"] for row in json.loads(out)["profiles"])
    active = (root / "secrets/model.json").read_bytes()
    key.write_text("fake-profile-key-bad\nfake-profile-key-second\n")
    rc, out, err = cli(tmp_path, "save-model-profile", "--state-dir", root,
                       "--id", "invalid", "--model", "fixture",
                       "--base-url", "https://model.example.invalid/v1", "--api-key-file", key)
    assert rc == 2 and not out and "本地配置不可用" in err
    assert (root / LIBRARY).read_bytes() == library
    assert (root / "secrets/model.json").read_bytes() == active


def test_context_options_survive_profile_activation_and_rollback(runtime, root, tmp_path):
    original = legacy(runtime, root, tmp_path)
    result = runtime.save_model_profile(
        root, "capacity", "capacity-fixture", "https://context.example.invalid/v1",
        key_file(tmp_path, "capacity"), context_window=1048576,
        model_auto_compact_token_limit=900000,
    )
    assert not result["provider_verified"]
    assert (root / "secrets/model.json").read_bytes() == original
    activation = runtime.activate_model_profile(root, "capacity")
    env, _ = runtime.environment(root, inherited={})
    binding = json.loads(env["LEGION_CODEX_RESPONSES_BINDING"])
    assert binding["context_window"] == 1048576
    assert binding["model_auto_compact_token_limit"] == 900000
    assert "api_key" not in binding
    runtime.activate_model_profile(root, activation["rollback_profile"])
    assert (root / "secrets/model.json").read_bytes() == original


@pytest.mark.parametrize("option", ["context_window", "model_auto_compact_token_limit"])
@pytest.mark.parametrize("invalid", [0, -1, True, 1.5, "1000000", 2**63])
def test_invalid_context_option_does_not_replace_active_profile(
        runtime, root, tmp_path, option, invalid):
    original = legacy(runtime, root, tmp_path)
    with pytest.raises(ValueError):
        runtime.configure_model(
            root, "invalid-fixture", "https://context.example.invalid/v1",
            key_file(tmp_path, "invalid"), **{option: invalid},
        )
    assert (root / "secrets/model.json").read_bytes() == original


@pytest.mark.parametrize("command", ["configure-model", "save-model-profile"])
def test_context_options_work_through_real_cli(runtime, root, tmp_path, command):
    args = [command, "--state-dir", root, "--model", "context-cli",
            "--base-url", "https://context.example.invalid/v1", "--api-key-file",
            key_file(tmp_path, "context-cli"), "--context-window", "1048576",
            "--model-auto-compact-token-limit", "900000"]
    if command == "save-model-profile":
        args += ["--id", "capacity-cli"]
    rc, out, err = cli(tmp_path, *args)
    assert rc == 0, err
    if command == "save-model-profile":
        runtime.activate_model_profile(root, "capacity-cli")
    env, _ = runtime.environment(root, inherited={})
    binding = json.loads(env["LEGION_CODEX_RESPONSES_BINDING"])
    assert binding["context_window"] == 1048576
    assert binding["model_auto_compact_token_limit"] == 900000
    assert "fake-profile-key-context-cli" not in out + err
