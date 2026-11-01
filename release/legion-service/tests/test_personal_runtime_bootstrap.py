"""Local first-run configuration. No company/personal credentials or model calls."""
from pathlib import Path
import importlib
import importlib.util
import json
import os
import stat
import sys

import pytest

SERVICE = Path(__file__).resolve().parents[1]


@pytest.fixture
def bootstrap(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SERVICE))
    monkeypatch.delenv("MEYO_PERSONAL_STATE_DIR", raising=False)
    monkeypatch.setenv("LEGION_PILOT_GOALS", str(tmp_path / "unconfigured-goals.md"))
    for name, leaf in (("HOME", "home"), ("MEYO_LEGION_DIR", "old-data"),
                       ("LEGION_HOME_DIR", "old-legion")):
        path = tmp_path / leaf
        path.mkdir()
        monkeypatch.setenv(name, str(path))
    assert importlib.util.find_spec("personal_runtime") is not None, "independent first-run entry is missing"
    return importlib.import_module("personal_runtime")


def direction_file(tmp_path, text=None):
    path = tmp_path / "chosen-direction.md"
    path.write_text(text or (
        "# 操作者选中的方向\n[MANUAL]\n"
        "头号纲领: 交付能实际使用的作品\n"
        "Owner代理:\n- 保留来源和失败现场，让作者自主返工\n"
        "铁律:\n- 上下文不是固定流程\n[/MANUAL]\n"
        "[AUTO]\n旧任务、旧机器和旧授权不得迁移\n[/AUTO]\n"), encoding="utf-8")
    path.chmod(0o644)  # The chosen source may be a shipped, readable example.
    return path


def test_goals_are_not_implicitly_adopted_from_shell_or_old_checkout(bootstrap, tmp_path):
    root = tmp_path / "instance"
    bootstrap.initialize(root, "owner-test", 18080)
    source = direction_file(tmp_path)
    env, _ = bootstrap.environment(root, inherited={"LEGION_PILOT_GOALS": str(source)})
    assert env.get("LEGION_PILOT_GOALS") == str(root / "pilot-goals.md")
    assert not (root / "pilot-goals.md").exists()


def test_public_model_cache_reference_survives_without_inheriting_memory_or_credentials(bootstrap, tmp_path):
    root = tmp_path / "instance"
    bootstrap.initialize(root, "owner-test", 18080)
    cache = tmp_path / "public-model-assets"
    env, _ = bootstrap.environment(root, inherited={
        "LEGION_JCODE_PUBLIC_MODEL_CACHE": str(cache),
        "JCODE_HOME": str(tmp_path / "another-users-memory"),
        "OPENAI_API_KEY": "unrelated-private-fixture",
    })
    assert env.get("LEGION_JCODE_PUBLIC_MODEL_CACHE") == str(cache)
    assert env["HOME"] == str(root / "home")
    assert "JCODE_HOME" not in env and "OPENAI_API_KEY" not in env


def test_goals_explicit_adoption_persists_only_manual_content(bootstrap, tmp_path):
    root = tmp_path / "instance"
    bootstrap.initialize(root, "owner-test", 18080)
    token = (root / "login-token.txt").read_bytes()
    source = direction_file(tmp_path)
    assert callable(getattr(bootstrap, "configure_goals", None)), "missing explicit goals configuration"
    result = bootstrap.configure_goals(root, source)
    target = root / "pilot-goals.md"
    body = target.read_text(encoding="utf-8")
    assert body.startswith("[MANUAL]\n") and body.endswith("[/MANUAL]\n")
    assert "保留来源和失败现场" in body and "[AUTO]" not in body and "旧授权" not in body
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert result["configured"] is True
    assert result["source"] == "standing_user_direction" and result["is_user_message"] is False
    import hashlib
    assert result["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert "保留来源和失败现场" not in repr(result)  # Receipt is metadata, not private prose.
    assert (root / "login-token.txt").read_bytes() == token
    source.unlink()
    assert bootstrap.environment(root, inherited={})[0]["LEGION_PILOT_GOALS"] == str(target)


def test_goals_reload_after_import_edit_and_clear_without_other_instance_leak(
        bootstrap, tmp_path, monkeypatch):
    root = tmp_path / "instance"
    bootstrap.initialize(root, "owner-test", 18080)
    pilot = importlib.import_module("legion_pilot")
    assert callable(getattr(bootstrap, "configure_goals", None)), "missing explicit goals configuration"
    source = direction_file(tmp_path)
    bootstrap.configure_goals(root, source)
    env, _ = bootstrap.environment(root, inherited={})
    monkeypatch.setenv("LEGION_PILOT_GOALS", env["LEGION_PILOT_GOALS"])
    assert pilot.read_shared_goals()["prime"] == "交付能实际使用的作品"
    assert pilot.settings_for_humans()["shared_goals_path"] == str(root / "pilot-goals.md")
    source.write_text("[MANUAL]\n头号纲领: 下一轮看到新方向\nOwner代理:\n- 新方向\n[/MANUAL]\n")
    bootstrap.configure_goals(root, source)
    assert pilot.read_shared_goals()["owner_proxy"] == ["新方向"]
    # A second initialized instance has no adopted direction, even in this same process.
    other = tmp_path / "other-instance"
    bootstrap.initialize(other, "other-owner", 18081)
    other_env, _ = bootstrap.environment(other, inherited=env)
    monkeypatch.setenv("LEGION_PILOT_GOALS", other_env["LEGION_PILOT_GOALS"])
    assert pilot.read_shared_goals()["owner_proxy"] == []
    monkeypatch.setenv("LEGION_PILOT_GOALS", env["LEGION_PILOT_GOALS"])
    assert bootstrap.configure_goals(root)["configured"] is False
    assert not (root / "pilot-goals.md").exists()
    assert pilot.read_shared_goals()["owner_proxy"] == []
    assert bootstrap.configure_goals(root)["configured"] is False  # Repeat clear is harmless.


@pytest.mark.parametrize("damage", ["no-manual", "empty", "double-manual", "oversize",
                                   "invalid-utf8", "symlink", "directory"])
def test_bad_goals_source_preserves_previous_direction(bootstrap, tmp_path, damage):
    root = tmp_path / "instance"
    bootstrap.initialize(root, "owner-test", 18080)
    source = direction_file(tmp_path)
    assert callable(getattr(bootstrap, "configure_goals", None)), "missing explicit goals configuration"
    bootstrap.configure_goals(root, source)
    previous = _instance_snapshot(root)
    if damage == "no-manual":
        source.write_text("do not silently treat arbitrary text as confirmed direction")
    elif damage == "empty":
        source.write_text("[MANUAL]\n \n[/MANUAL]")
    elif damage == "double-manual":
        source.write_text(source.read_text() + "[MANUAL]\nsecond\n[/MANUAL]")
    elif damage == "oversize":
        source.write_text("[MANUAL]\n" + "a" * 32769 + "\n[/MANUAL]")
    elif damage == "invalid-utf8":
        source.write_bytes(b"[MANUAL]\xff[/MANUAL]")
    elif damage == "symlink":
        real = tmp_path / "real.md"
        source.rename(real)
        source.symlink_to(real)
    else:
        source.unlink()
        source.mkdir()
    with pytest.raises((ValueError, OSError, UnicodeError)):
        bootstrap.configure_goals(root, source)
    assert _instance_snapshot(root) == previous


def test_managed_goals_symlink_is_not_read_cleared_or_replaced(bootstrap, tmp_path):
    root = tmp_path / "instance"
    bootstrap.initialize(root, "owner-test", 18080)
    source = direction_file(tmp_path)
    target = root / "pilot-goals.md"
    target.symlink_to(source)
    assert callable(getattr(bootstrap, "configure_goals", None)), "missing explicit goals configuration"
    for action in (lambda: bootstrap.configure_goals(root),
                   lambda: bootstrap.configure_goals(root, source),
                   lambda: bootstrap.environment(root, inherited={})):
        with pytest.raises((ValueError, OSError)):
            action()
    assert target.is_symlink() and "旧授权" in source.read_text()


def test_goals_cli_and_check_keep_parent_environment_and_private_text(
        bootstrap, tmp_path, monkeypatch, capsys):
    root = tmp_path / "instance"
    bootstrap.initialize(root, "owner-test", 18080)
    source = direction_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    before = dict(os.environ)
    assert bootstrap.main(["configure-goals", "--state-dir", "instance",
                           "--file", source.name]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["configured"] is True and os.environ == before
    # check is configuration-only; isolate its deliberate environment replacement.
    with monkeypatch.context() as mp:
        mp.setattr(bootstrap.os, "environ", dict(os.environ))
        assert bootstrap.main(["check", "--state-dir", str(root)]) == 0
        check = json.loads(capsys.readouterr().out)
        assert check["standing_direction"]["sha256"] == result["sha256"]
        assert "保留来源和失败现场" not in repr(check)
    assert bootstrap.main(["configure-goals", "--state-dir", str(root), "--clear"]) == 0
    assert json.loads(capsys.readouterr().out)["configured"] is False


def test_pilot_runtime_goal_path_is_not_frozen_at_import(tmp_path, monkeypatch, bootstrap):
    pilot = importlib.import_module("legion_pilot")
    chosen = direction_file(tmp_path)
    monkeypatch.setenv("LEGION_PILOT_GOALS", str(chosen))
    assert pilot.read_shared_goals()["prime"] == "交付能实际使用的作品"
    assert pilot.settings_for_humans()["shared_goals_path"] == str(chosen)
    assert pilot.read_shared_goals(chosen)["owner_proxy"] == ["保留来源和失败现场，让作者自主返工"]


@pytest.mark.parametrize("damage", [
    "public-mode", "hardlink", "auto-only", "two-manuals", "manual-in-auto", "invalid-utf8",
])
def test_managed_goals_live_reader_matches_configuration_contract(
        bootstrap, tmp_path, monkeypatch, damage):
    root = tmp_path / "instance"
    bootstrap.initialize(root, "owner-test", 18080)
    source = direction_file(tmp_path)
    bootstrap.configure_goals(root, source)
    env, _ = bootstrap.environment(root, inherited={})
    for name in ("LEGION_PILOT_GOALS", "MEYO_PERSONAL_STATE_DIR"):
        monkeypatch.setenv(name, env[name])
    pilot = importlib.import_module("legion_pilot")
    target = root / "pilot-goals.md"
    original = target.read_text()
    if damage == "public-mode":
        target.chmod(0o644)
    elif damage == "hardlink":
        os.link(target, tmp_path / "other-link.md")
    elif damage == "auto-only":
        target.write_text("[AUTO]\nOwner代理:\n- 未采用的自动内容\n[/AUTO]\n")
    elif damage == "two-manuals":
        target.write_text(original + original)
    elif damage == "manual-in-auto":
        target.write_text("[AUTO]\n" + original + "[/AUTO]\n")
    else:
        target.write_bytes(b"[MANUAL]\xff[/MANUAL]")
    with pytest.raises((ValueError, OSError, UnicodeError)):
        bootstrap.environment(root, inherited={})
    assert pilot.read_shared_goals()["owner_proxy"] == []
    settings = pilot.settings_for_humans()
    assert settings["shared_goals_observation"]["status"] == "invalid"
    assert settings["owner_proxy"] == []
    # A damaged optional direction cannot swallow the author's existing result
    # or manufacture a stop/continue decision.
    state = {"native_continuation": {
        "source": "orchestration_request.coordinate", "action": "continue", "reason": "未完",
    }}
    expected = pilot.classify("好", state, {"enabled": True})
    observed = pilot.evaluate("好", state, config={"enabled": True})
    for key, value in expected.items():
        assert observed[key] == value


@pytest.mark.parametrize("operation", ["clear", "replace"])
@pytest.mark.parametrize("bad_body", [b"[MANUAL]\n \n[/MANUAL]\n", b"\xff"])
def test_goals_explicit_recovery_does_not_require_old_body_to_parse(
        bootstrap, tmp_path, capsys, operation, bad_body):
    root = tmp_path / "instance"
    bootstrap.initialize(root, "owner-test", 18080)
    source = direction_file(tmp_path)
    bootstrap.configure_goals(root, source)
    target = root / "pilot-goals.md"
    target.write_bytes(bad_body)
    token = (root / "login-token.txt").read_bytes()
    args = ["configure-goals", "--state-dir", str(root)]
    args += ["--clear"] if operation == "clear" else ["--file", str(source)]
    assert bootstrap.main(args) == 0
    assert json.loads(capsys.readouterr().out)["configured"] is (operation == "replace")
    assert (root / "login-token.txt").read_bytes() == token
    assert bootstrap.environment(root, inherited={})[0]["LEGION_PILOT_GOALS"] == str(target)


def test_goals_status_distinguishes_missing_from_invalid_and_keeps_legacy_export(
        bootstrap, tmp_path, monkeypatch):
    pilot = importlib.import_module("legion_pilot")
    assert isinstance(getattr(pilot, "DEFAULT_GOALS", None), Path)
    source = direction_file(tmp_path)
    monkeypatch.setenv("LEGION_PILOT_GOALS", str(source))
    assert pilot.settings_for_humans()["shared_goals_observation"]["status"] == "configured"
    source.unlink()
    assert pilot.settings_for_humans()["shared_goals_observation"]["status"] == "not_configured"
    source.symlink_to(tmp_path / "missing-target")
    assert pilot.settings_for_humans()["shared_goals_observation"]["status"] == "invalid"
    assert pilot.read_shared_goals()["owner_proxy"] == []


def fixture_key(tmp_path, value="not-a-real-provider-key"):
    path = tmp_path / "fixture-key.txt"
    path.write_text(value + "\n")
    path.chmod(0o600)
    return path


def _instance_snapshot(root):
    result = {}
    for path in root.rglob("*"):
        info = path.lstat()
        content = (path.read_bytes() if stat.S_ISREG(info.st_mode) else
                   os.readlink(path) if stat.S_ISLNK(info.st_mode) else None)
        result[path.relative_to(root)] = (info.st_mode, info.st_nlink, content)
    return result


def test_first_run_creates_existing_identity_format_without_printing_token(bootstrap, tmp_path):
    root = tmp_path / "instance"
    result = bootstrap.initialize(root, "owner-test", 18080)
    config = json.loads((root / "config.json").read_text())
    token = (root / "login-token.txt").read_text().strip()
    records = json.loads((root / "secrets/personal-tokens.json").read_text())
    assert records[token]["user"] == "owner-test"
    assert len(token) >= 24 and token not in repr(result)
    assert config["user"] == "owner-test" and config["port"] == 18080
    assert config["host"] == "127.0.0.1"
    for path in (root, root / "secrets"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    for path in (root / "config.json", root / "login-token.txt", root / "secrets/personal-tokens.json"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_repeated_init_preserves_login_identity_and_other_state(bootstrap, tmp_path):
    root = tmp_path / "instance"
    bootstrap.initialize(root, "owner-test", 18080)
    token_before = (root / "login-token.txt").read_bytes()
    (root / "data/user-result.txt").write_text("existing work")
    bootstrap.initialize(root, "owner-test", 18080)
    assert (root / "login-token.txt").read_bytes() == token_before
    assert (root / "data/user-result.txt").read_text() == "existing work"
    with pytest.raises(ValueError):
        bootstrap.initialize(root, "other-user", 18080)
    assert (root / "login-token.txt").read_bytes() == token_before


def test_dsh_config_is_nonsecret_read_only_and_does_not_probe_a_provider(bootstrap, tmp_path, monkeypatch):
    root = tmp_path / "instance"
    bootstrap.initialize(root, "owner-test", 18080)
    token = (root / "login-token.txt").read_text().strip()
    monkeypatch.setattr(bootstrap, "environment", lambda *_: pytest.fail("must not build or replace the server environment"))
    before_env = dict(os.environ)
    before_files = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert callable(getattr(bootstrap, "dsh_config", None)), "the existing launcher must expose nonsecret DSH configuration"
    result = bootstrap.dsh_config(root)
    assert result == {
        "plugin": "dsh-plugin-legion",
        "config": {"baseUrl": "http://127.0.0.1:18080", "tokenFile": str(root.resolve() / "login-token.txt")},
        "verification": "configuration_only_no_model_request",
    }
    assert token not in repr(result)
    assert bootstrap.dsh_config(root) == result
    assert os.environ == before_env
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before_files


@pytest.mark.parametrize("damage", ["mismatch", "symlink", "permissions"])
def test_dsh_config_refuses_damaged_identity_without_a_fallback(bootstrap, tmp_path, damage):
    root = tmp_path / "instance"
    bootstrap.initialize(root, "owner-test", 18080)
    token_file = root / "login-token.txt"
    if damage == "mismatch":
        token_file.write_text("not-the-registered-token\n")
    elif damage == "symlink":
        other = fixture_key(tmp_path, token_file.read_text().strip())
        token_file.unlink()
        token_file.symlink_to(other)
    else:
        token_file.chmod(0o644)
    assert callable(getattr(bootstrap, "dsh_config", None))
    with pytest.raises((ValueError, OSError)):
        bootstrap.dsh_config(root)


def test_dsh_config_cli_does_not_clear_environment_or_print_credentials(bootstrap, tmp_path, capsys):
    root = tmp_path / "instance"
    bootstrap.initialize(root, "owner-test", 18080)
    before = dict(os.environ)
    assert bootstrap.main(["dsh-config", "--state-dir", str(root)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["config"]["tokenFile"] == str(root.resolve() / "login-token.txt")
    assert (root / "login-token.txt").read_text().strip() not in repr(result)
    assert os.environ == before


@pytest.mark.parametrize("ending", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_login_token_preserves_existing_4096_character_identity(
        bootstrap, tmp_path, monkeypatch, capsys, ending):
    root = tmp_path / "instance"
    bootstrap.initialize(root, "owner-test", 18080)
    token = "t" * 4096
    (root / "login-token.txt").write_bytes((token + ending).encode())
    store = root / "secrets/personal-tokens.json"
    store.write_text(json.dumps({token: {"user": "owner-test"}}))
    monkeypatch.setenv("LEGION_PERSONAL_TOKENS_FILE", str(store))
    from personal_identity import resolve_token
    assert resolve_token(token) == "owner-test"
    before_files, before_env = _instance_snapshot(root), dict(os.environ)

    assert bootstrap.initialize(root, "owner-test", 18080)["user"] == "owner-test"
    env, _ = bootstrap.environment(root, {"OPENAI_API_KEY": "unused-fixture-key"})
    assert env["LEGION_PERSONAL_TOKENS_FILE"] == str(store)
    assert "OPENAI_API_KEY" not in env
    result = bootstrap.dsh_config(root)
    assert result["config"] == {
        "baseUrl": "http://127.0.0.1:18080", "tokenFile": str(root / "login-token.txt"),
    }
    saved_umask = os.umask(0o077)
    try:
        assert bootstrap.main(["dsh-config", "--state-dir", str(root)]) == 0
    finally:
        os.umask(saved_umask)
    captured = capsys.readouterr()
    assert json.loads(captured.out) == result and captured.err == ""
    assert token not in captured.out
    assert _instance_snapshot(root) == before_files and os.environ == before_env


@pytest.mark.parametrize("record", [
    None, [], "fixture-private-record",
    {"user": "other-private-owner", "private_field": "fixture-private-value"},
], ids=["null", "list", "string", "wrong-owner"])
def test_login_token_rejects_invalid_records_without_disclosure_or_fallback(
        bootstrap, tmp_path, monkeypatch, capsys, record):
    root = tmp_path / "instance"
    bootstrap.initialize(root, "owner-test", 18080)
    token = (root / "login-token.txt").read_text().strip()
    (root / "secrets/personal-tokens.json").write_text(json.dumps({token: record}))
    fallback = tmp_path / "unused-fallback.json"
    fallback.write_text(json.dumps({token: {"user": "owner-test"}}))
    fallback.chmod(0o600)
    monkeypatch.setenv("LEGION_PERSONAL_TOKENS_FILE", str(fallback))
    before_files, before_env = _instance_snapshot(root), dict(os.environ)
    fallback_before = fallback.read_bytes()

    for call in (
        lambda: bootstrap.initialize(root, "owner-test", 18080),
        lambda: bootstrap.environment(root, before_env),
        lambda: bootstrap.dsh_config(root),
    ):
        with pytest.raises(ValueError, match="^本地身份配置不一致$"):
            call()
    saved_umask = os.umask(0o077)
    try:
        for command in (["init", "--user", "owner-test", "--port", "18080"],
                        ["dsh-config"], ["check"]):
            assert bootstrap.main([*command, "--state-dir", str(root)]) == 2
            captured = capsys.readouterr()
            assert captured.out == "" and "未改用其它凭据" in captured.err
            for private in (token, str(root), "owner-test", "other-private-owner",
                            "private_field", "fixture-private-value", "fixture-private-record"):
                assert private not in captured.err
            assert os.environ == before_env
    finally:
        os.umask(saved_umask)
    assert _instance_snapshot(root) == before_files
    assert fallback.read_bytes() == fallback_before


@pytest.mark.parametrize("damage", ["oversize", "hardlink", "fifo", "empty"])
def test_login_token_keeps_bounded_private_file_checks(bootstrap, tmp_path, damage):
    root = tmp_path / "instance"
    bootstrap.initialize(root, "owner-test", 18080)
    token_file = root / "login-token.txt"
    if damage == "oversize":
        token_file.write_bytes(b"t" * (32768 + 1))
    elif damage == "hardlink":
        os.link(token_file, tmp_path / "token-alias")
    elif damage == "fifo":
        token_file.unlink()
        os.mkfifo(token_file, mode=0o600)
    else:
        token_file.write_text("\n")
        (root / "secrets/personal-tokens.json").write_text(json.dumps({"": {"user": "owner-test"}}))
    before_files = _instance_snapshot(root)
    for call in (
        lambda: bootstrap.initialize(root, "owner-test", 18080),
        lambda: bootstrap.environment(root, {}),
        lambda: bootstrap.dsh_config(root),
    ):
        with pytest.raises((ValueError, OSError)):
            call()
    assert _instance_snapshot(root) == before_files


def test_model_pair_persists_and_reloads_without_inherited_provider_credentials(bootstrap, tmp_path):
    root = tmp_path / "instance"
    bootstrap.initialize(root, "owner-test", 18080)
    key = fixture_key(tmp_path)
    result = bootstrap.configure_model(root, "test-model", "https://model.example.invalid/v1", key)
    assert "not-a-real-provider-key" not in repr(result)
    before = dict(os.environ)
    env, config = bootstrap.environment(root, {
        "PATH": "/usr/bin:/bin", "LANG": "C",
        "MEYO_UNIFIED_APPID": "wrong-company-key",
        "OPENAI_API_KEY": "wrong-user-key", "GH_TOKEN": "wrong-github-token",
        "MEYO_ORCH_DIR": "/wrong/source", "CODEX_HOME": "/wrong/home",
        "LEGION_PERSONAL_TOKENS_FILE": "/wrong/people.json",
        "LEGION_SESSIONS_DIR": "/wrong/sessions", "PYTHONPATH": "/wrong/imports",
    })
    assert os.environ == before
    binding = json.loads(env["LEGION_CODEX_RESPONSES_BINDING"])
    assert binding["model"] == "test-model" and binding["base_url"] == "https://model.example.invalid/v1"
    assert env[binding["api_key_env"]] == "not-a-real-provider-key"
    assert all(name not in env for name in ("MEYO_UNIFIED_APPID", "OPENAI_API_KEY", "GH_TOKEN", "PYTHONPATH"))
    assert Path(env["HOME"]) == root / "home"
    assert Path(env["CODEX_HOME"]) == root / "codex-home"
    assert Path(env["LEGION_PERSONAL_TOKENS_FILE"]) == root / "secrets/personal-tokens.json"
    assert Path(env["MEYO_LEGION_DIR"]) == root / "data"
    assert Path(env["LEGION_HOME_DIR"]) == root / "legion-home"
    assert Path(env["LEGION_SESSIONS_DIR"]) == root / "legion-home/sessions"
    assert config["port"] == 18080
    assert env["LEGION_ALLOW_UNAUTHENTICATED_LOCAL_CLI"] == "0"
    assert env["LEGION_CODEX_TRANSPORT"] == "responses"
    assert env["LEGION_EXT_AUTO_UPDATE"] == "0"
    assert bootstrap.environment(root, {})[0][binding["api_key_env"]] == "not-a-real-provider-key"


@pytest.mark.parametrize("kind", ["symlink", "public_readable", "bad_json"])
def test_untrusted_saved_configuration_fails_without_fallback(bootstrap, tmp_path, kind):
    root = tmp_path / "instance"
    bootstrap.initialize(root, "owner-test", 18080)
    config = root / "config.json"
    if kind == "symlink":
        other = tmp_path / "other.json"
        other.write_bytes(config.read_bytes())
        other.chmod(0o600)
        config.unlink()
        config.symlink_to(other)
    elif kind == "public_readable":
        config.chmod(0o644)
    else:
        config.write_text("{broken")
    with pytest.raises((ValueError, OSError)):
        bootstrap.environment(root, {"MEYO_UNIFIED_APPID": "fallback-must-not-be-used"})


def test_unconfigured_start_never_uses_old_key_or_company_binding(bootstrap, tmp_path):
    root = tmp_path / "instance"
    bootstrap.initialize(root, "owner-test", 18080)
    env, _ = bootstrap.environment(root, {
        "MEYO_UNIFIED_APPID": "wrong-company-key", "LEGION_CODEX_RESPONSES_BINDING": "old",
    })
    assert env["LEGION_CODEX_TRANSPORT"] == "responses"
    assert "LEGION_CODEX_RESPONSES_BINDING" not in env
    assert "wrong-company-key" not in repr(env)


def test_invalid_model_replacement_preserves_working_configuration(bootstrap, tmp_path):
    root = tmp_path / "instance"
    bootstrap.initialize(root, "owner-test", 18080)
    key = fixture_key(tmp_path)
    bootstrap.configure_model(root, "test-model", "https://model.example.invalid/v1", key)
    before = (root / "secrets/model.json").read_bytes()
    with pytest.raises(ValueError) as error:
        bootstrap.configure_model(root, "test-model", "http://remote.example.invalid/v1", key)
    assert "not-a-real-provider-key" not in str(error.value)
    assert (root / "secrets/model.json").read_bytes() == before


def test_initializer_does_not_adopt_nonempty_directory_or_follow_state_link(bootstrap, tmp_path):
    root = tmp_path / "existing"
    root.mkdir(mode=0o700)
    (root / "keep.txt").write_text("keep")
    with pytest.raises(ValueError):
        bootstrap.initialize(root, "owner-test", 18080)
    assert (root / "keep.txt").read_text() == "keep"
    link = tmp_path / "linked"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError):
        bootstrap.initialize(link, "owner-test", 18080)


def test_unsafe_key_file_is_rejected_before_persisting_secrets(bootstrap, tmp_path):
    root = tmp_path / "instance"
    bootstrap.initialize(root, "owner-test", 18080)
    key = fixture_key(tmp_path)
    key.chmod(0o644)
    with pytest.raises((ValueError, OSError)):
        bootstrap.configure_model(root, "test-model", "https://model.example.invalid/v1", key)
    assert not (root / "secrets/model.json").exists()


def test_dead_proxy_cleanup_does_not_print_proxy_credentials(bootstrap, monkeypatch, capsys):
    import socket
    import server

    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://fixture-user:proxy-secret-fixture@127.0.0.1:12345")
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: 111)
    server._strip_dead_proxy_env()
    output = capsys.readouterr().out
    assert "HTTPS_PROXY" in output
    assert "proxy-secret-fixture" not in output
    assert "fixture-user" not in output
    assert "127.0.0.1:12345" not in output
    assert "HTTPS_PROXY" not in os.environ


@pytest.mark.parametrize("name", ["home", "codex-home", "data", "registry", "repos", "runtimes", "tmp"])
def test_runtime_subdirectories_cannot_redirect_state_outside_instance(bootstrap, tmp_path, name):
    root = tmp_path / "instance"
    bootstrap.initialize(root, "owner-test", 18080)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (root / name).rmdir()
    (root / name).symlink_to(outside, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        bootstrap.environment(root, {})
    assert list(outside.iterdir()) == []


def test_configuring_unknown_instance_does_not_read_the_key(bootstrap, tmp_path, monkeypatch):
    root = tmp_path / "unknown"
    root.mkdir(mode=0o700)
    key = fixture_key(tmp_path)
    reads = []
    real_private = bootstrap._private

    def checked(path, **kwargs):
        reads.append(Path(path))
        return real_private(path, **kwargs)

    monkeypatch.setattr(bootstrap, "_private", checked)
    with pytest.raises((ValueError, OSError)):
        bootstrap.configure_model(root, "test-model", "https://model.example.invalid/v1", key)
    assert key not in reads


def test_deleting_saved_model_cannot_reuse_last_shell_binding(bootstrap, tmp_path):
    root = tmp_path / "instance"
    bootstrap.initialize(root, "owner-test", 18080)
    bootstrap.configure_model(root, "test-model", "https://model.example.invalid/v1",
                              fixture_key(tmp_path))
    previous, _ = bootstrap.environment(root, {})
    (root / "secrets/model.json").unlink()
    current, _ = bootstrap.environment(root, previous)
    assert "LEGION_CODEX_RESPONSES_BINDING" not in current
    assert bootstrap.KEY_ENV not in current
    assert current["LEGION_CODEX_TRANSPORT"] == "responses"


def test_instance_secrets_cannot_be_initialized_in_the_source_checkout(bootstrap, tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(bootstrap, "PROJECT", checkout)
    state = checkout / "instance"
    with pytest.raises(ValueError):
        bootstrap.initialize(state, "owner-test", 18080)
    assert not (state / "secrets").exists()


def test_concurrent_initialization_keeps_one_identity(bootstrap, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    state = tmp_path / "instance"
    original_open = os.open

    def observed_open(path, flags, mode=0o777, *, dir_fd=None):
        try:
            return original_open(path, flags, mode, dir_fd=dir_fd)
        except OSError as exc:
            details = {"path": str(path), "state": str(state), "dir_fd": dir_fd}
            if dir_fd is not None:
                try:
                    info = os.fstat(dir_fd)
                    details.update(parent_inode=info.st_ino,
                                   parent_is_directory=stat.S_ISDIR(info.st_mode),
                                   children=os.listdir(dir_fd))
                except OSError as detail_error:
                    details["parent_error"] = detail_error.errno
            exc.add_note("Owned fixture filesystem observation: " + json.dumps(details))
            raise

    monkeypatch.setattr(os, "open", observed_open)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: bootstrap.initialize(state, "owner-test", 18080), range(3)))
    assert all(row["user"] == "owner-test" for row in results)
    token = (state / "login-token.txt").read_text().strip()
    assert json.loads((state / "secrets/personal-tokens.json").read_text()) == {
        token: {"user": "owner-test"},
    }


def test_invalid_config_lock_is_not_followed_or_read(bootstrap, tmp_path):
    root = tmp_path / "instance"
    bootstrap.initialize(root, "owner-test", 18080)
    key_before = (root / "login-token.txt").read_bytes()
    (root / ".configure.lock").unlink()
    os.mkfifo(root / ".configure.lock", mode=0o600)
    with pytest.raises((ValueError, OSError)):
        bootstrap.initialize(root, "owner-test", 18080)
    assert (root / "login-token.txt").read_bytes() == key_before


@pytest.mark.parametrize("tilde_state,tilde_key", [(True, False), (False, True)])
def test_cli_freezes_caller_paths_before_changing_home(
        bootstrap, tmp_path, monkeypatch, capsys, tilde_state, tilde_key):
    caller_home = tmp_path / "caller-home"
    caller_home.mkdir()
    monkeypatch.setenv("HOME", str(caller_home))
    root = caller_home / "instance"
    bootstrap.initialize(root, "owner-test", 18080)
    key = caller_home / "provider-key"
    key.write_text("caller-selected-fixture-key\n")
    key.chmod(0o600)
    decoy = root / "home/provider-key"
    decoy.write_text("instance-home-decoy-fixture-key\n")
    decoy.chmod(0o600)
    saved_environment = dict(os.environ)
    saved_umask = os.umask(0o077)
    try:
        rc = bootstrap.main([
            "configure-model", "--state-dir", "~/instance" if tilde_state else str(root),
            "--model", "test-model", "--base-url", "https://model.example.invalid/v1",
            "--api-key-file", "~/provider-key" if tilde_key else str(key),
        ])
    finally:
        os.environ.clear()
        os.environ.update(saved_environment)
        os.umask(saved_umask)
    assert rc == 0
    saved = json.loads((root / "secrets/model.json").read_text())
    assert saved["api_key"] == "caller-selected-fixture-key"
    assert decoy.read_text() == "instance-home-decoy-fixture-key\n"
    output = capsys.readouterr()
    assert "caller-selected-fixture-key" not in output.out + output.err
    assert "instance-home-decoy-fixture-key" not in output.out + output.err
