"""Device-control permissions: pure argv tests and an offline, optional CLI check."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tomllib

import pytest


HELPER = Path(__file__).resolve().parents[1] / "native_device_permissions.py"
CLI = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
HOME = "/var/lib/meyo-legion/home"
WORK = "/var/lib/meyo-legion/data/outputs/current"
STATE = "/var/lib/meyo-legion/data"
TMP = "/var/lib/meyo-legion/tmp"
ENV = {
    "HOME": HOME,
    "TMPDIR": TMP,
    "MEYO_DEVICE_DOCKER_HOST": "unix:///run/user/999/docker.sock",
    "MEYO_DEVICE_DOCKER_DATA_ROOT": HOME + "/.local/share/docker",
}


@pytest.fixture
def restrict():
    assert HELPER.is_file(), "the authorized pure argv helper is not implemented"
    spec = importlib.util.spec_from_file_location("tested_device_permissions", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.restrict_codex_device_control


def common(*extra, roots=None):
    return [
        "--ignore-user-config", "--skip-git-repo-check", "-C", WORK,
        "-s", "workspace-write",
        "-c", "sandbox_workspace_write.network_access=true",
        "-c", "sandbox_workspace_write.writable_roots="
        + json.dumps([STATE, WORK] if roots is None else roots),
        "-m", "fixture-model", "--json", "-o", TMP + "/final.txt",
        "-c", 'model_provider="fixture"',
        "-c", 'model_providers.fixture={name="Fixture",wire_api="responses"}',
        "-c", 'mcp_servers.existing={command="/trusted/host",args=["--stdio"]}',
        "-c", 'approval_policy="never"',
        *extra,
    ]


def settings(argv):
    return tomllib.loads("\n".join(
        argv[i + 1] for i, arg in enumerate(argv) if arg in ("-c", "--config")
    ))


def profile(argv):
    config = settings(argv)
    return config["permissions"][config["default_permissions"]]


def test_preserves_scope_and_opaque_provider_mcp_arguments(restrict):
    original = common(roots=[STATE, "/opt/editable checkout", WORK])
    saved, environment = list(original), dict(ENV)
    result = restrict(original, environment)
    assert original == saved and environment == ENV
    assert result is not original
    assert "-s" not in result and not any(
        arg.startswith("sandbox_workspace_write") for arg in result
    )
    config = settings(result)
    for key in ("model_provider", "model_providers", "mcp_servers", "approval_policy"):
        assert config[key] == settings(original)[key]
    for flag in ("-C", "-m", "-o"):
        assert result[result.index(flag) + 1] == original[original.index(flag) + 1]
    fs = profile(result)["filesystem"]
    assert fs["/"] == "read"
    assert fs[WORK] == fs[STATE] == fs["/opt/editable checkout"] == "write"
    assert HOME not in fs  # Never add all of HOME merely to reach the daemon.
    assert "/opt/meyo-legion" not in fs
    assert profile(result)["network"]["enabled"] is True
    assert not any("bwrap" in arg or "sandbox-exec" in arg for arg in result)


@pytest.mark.parametrize("catalog", [
    '"/var/lib/meyo-legion/model-catalogs/fallback.catalog.json"',
    "/var/lib/meyo-legion/model-catalogs/fallback.catalog.json",
])
def test_preserves_native_model_catalog_with_device_permissions(restrict, catalog):
    # Volta supplies this in the same common argv; do not parse, open, rewrite
    # or reject the official fallback catalog's path while changing permissions.
    setting = "model_catalog_json=" + catalog
    original = common("-c", setting)
    result = restrict(original, ENV)
    assert result.count(setting) == 1
    position = result.index(setting)
    assert result[position - 1] == "-c"
    assert original[-2:] == ["-c", setting]
    assert 'default_permissions="meyo_device_commands"' in result


def test_all_control_paths_are_none_without_narrower_write_override(restrict):
    fs = profile(restrict(common(), ENV))["filesystem"]
    denied = {
        "/run/user/999", HOME + "/.local/share/docker",
        HOME + "/.config/systemd/user", HOME + "/.docker",
    }
    assert {key for key, mode in fs.items() if mode == "none"} == denied
    for path in denied:
        assert not any(mode == "write" and (key == path or key.startswith(path + "/"))
                       for key, mode in fs.items())
    for name in (".git", ".agents", ".codex"):
        assert fs[WORK + "/" + name] == "read"


@pytest.mark.parametrize("exclude_tmpdir,exclude_slash", [
    (False, False), (True, False), (False, True), (True, True),
])
def test_preserves_each_legacy_tmp_switch(restrict, exclude_tmpdir, exclude_slash):
    argv = common(
        "-c", f"sandbox_workspace_write.exclude_tmpdir_env_var={str(exclude_tmpdir).lower()}",
        "-c", f"sandbox_workspace_write.exclude_slash_tmp={str(exclude_slash).lower()}",
    )
    fs = profile(restrict(argv, ENV))["filesystem"]
    assert (fs.get(":tmpdir") == "write") is not exclude_tmpdir
    assert (fs.get(":slash_tmp") == "write") is not exclude_slash
    assert fs.get(TMP) is None  # Do not invent another explicit write grant.


def test_excluding_tmp_does_not_remove_an_explicit_tmp_grant(restrict):
    argv = common(
        "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
        "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
        roots=[WORK, TMP, "/tmp"],
    )
    fs = profile(restrict(argv, ENV))["filesystem"]
    assert fs[TMP] == fs["/tmp"] == "write"
    assert ":tmpdir" not in fs and ":slash_tmp" not in fs


def test_missing_legacy_settings_keep_native_defaults(restrict):
    argv = ["-C", WORK, "-s", "workspace-write", "--json"]
    p = profile(restrict(argv, ENV))
    assert p["network"]["enabled"] is False
    assert p["filesystem"][":tmpdir"] == p["filesystem"][":slash_tmp"] == "write"
    assert p["filesystem"][WORK] == "write"


def test_inline_legacy_table_and_configured_sandbox_mode(restrict):
    argv = [
        "--cd", WORK, "-c", 'sandbox_mode="workspace-write"',
        "--config", "sandbox_workspace_write={writable_roots="
        + json.dumps([STATE]) + ",network_access=false,exclude_slash_tmp=true}",
        "--json",
    ]
    p = profile(restrict(argv, ENV))
    assert p["filesystem"][STATE] == "write"
    assert p["network"]["enabled"] is False
    assert ":slash_tmp" not in p["filesystem"]
    assert "sandbox_mode" not in settings(restrict(argv, ENV))


@pytest.mark.parametrize("argv", [
    [], ["--future-flag", "unparsed"], ["-s", "danger-full-access"],
    ["-c", "permissions={anything=true}"], ["-c"],
])
def test_no_device_is_an_exact_noop_even_for_unknown_input(restrict, argv):
    before = copy.deepcopy(argv)
    result = restrict(argv, {"MEYO_DEVICE_DOCKER_DATA_ROOT": "/ignored"})
    assert result is argv
    assert result == before


@pytest.mark.parametrize("extra", [
    ["--dangerously-bypass-approvals-and-sandbox"],
    ["--yolo"], ["--full-auto"], ["--approve-for-me"],
    ["--permission-profile", "other"], ["--permissions-profile=other"],
    ["-P", ":workspace"], ["-p", "other"], ["--profile=other"],
    ["--add-dir", "/elsewhere"], ["--enable", "use_legacy_landlock"],
    ["-c", 'default_permissions="other"'], ["-c", 'permissions.other.filesystem={"/"="write"}'],
    ["-c", "sandbox_workspace_write.unknown=true"],
    ["-c", 'sandbox_workspace_write.writable_roots=["/new"]'],
    ["-c", 'sandbox_workspace_write.network_access="true"'],
    ["-c", "sandbox_workspace_write={network_access=true}"],
    ["-c", 'sandbox_mode="danger-full-access"'],
    ["-c", 'workspace_roots=["/elsewhere"]'],
    ["-c", "features.use_linux_sandbox_bwrap=false"],
    ["-c", "sandbox_workspace_write.exclude_slash_tmp=1"],
    ["-c", "sandbox_workspace_write.exclude_slash_tmp=true\nextra=true"],
    ["-c"], ["-C", "/second"], ["--", "ignored positional"],
])
def test_unknown_or_ambiguous_permission_shape_is_rejected(restrict, extra):
    with pytest.raises(ValueError):
        restrict(common(*extra), ENV)


@pytest.mark.parametrize("argv", [
    ["-C", WORK], ["-s", "workspace-write"],
    ["-C", "relative", "-s", "workspace-write"],
    ["-C", "/", "-s", "workspace-write"],
    ["-C", WORK, "-s", "read-only"],
    ["-C", WORK, "-s", "workspace-write", "-c", "sandbox_workspace_write.writable_roots=1"],
    ["-C", WORK, "-s", "workspace-write", "-c", 'sandbox_workspace_write.writable_roots=["relative"]'],
    ["-C", WORK, "-s", "workspace-write", "-c", 'sandbox_workspace_write.writable_roots=["/"]'],
    ["-C", WORK, "-s", "workspace-write", "-c", 'sandbox_workspace_write.writable_roots=["/data/**"]'],
])
def test_unresolvable_or_broadened_write_scope_is_rejected(restrict, argv):
    with pytest.raises(ValueError):
        restrict(argv, ENV)


@pytest.mark.parametrize("key,value", [
    ("MEYO_DEVICE_DOCKER_HOST", ""),
    ("MEYO_DEVICE_DOCKER_HOST", "tcp://127.0.0.1:2375"),
    ("MEYO_DEVICE_DOCKER_HOST", "unix://remote/run/user/999/docker.sock"),
    ("MEYO_DEVICE_DOCKER_HOST", "unix:///docker.sock"),
    ("MEYO_DEVICE_DOCKER_HOST", "unix:///run/user/999/docker.sock?mode=rw"),
    ("MEYO_DEVICE_DOCKER_HOST", "unix:///run/user/999/docker.sock#fragment"),
    ("MEYO_DEVICE_DOCKER_HOST", "unix:///run/user/999/../docker.sock"),
    ("MEYO_DEVICE_DOCKER_HOST", "unix:///run/user/%39%39%39/docker.sock"),
    ("MEYO_DEVICE_DOCKER_DATA_ROOT", ""),
    ("MEYO_DEVICE_DOCKER_DATA_ROOT", "/"),
    ("MEYO_DEVICE_DOCKER_DATA_ROOT", "/var/lib/../docker"),
    ("MEYO_DEVICE_DOCKER_DATA_ROOT", "/data/*"),
    ("HOME", ""),
    ("HOME", "relative"),
])
def test_invalid_operator_binding_fails_instead_of_returning_unrestricted(restrict, key, value):
    with pytest.raises(ValueError):
        restrict(common(), {**ENV, key: value})


@pytest.mark.parametrize("missing", ["HOME", "MEYO_DEVICE_DOCKER_DATA_ROOT"])
def test_explicit_device_requires_complete_operator_binding(restrict, missing):
    env = dict(ENV)
    del env[missing]
    with pytest.raises(ValueError):
        restrict(common(), env)


@pytest.mark.parametrize("root", [
    "/run/user/999", "/run/user/999/subdir",
    HOME + "/.local/share/docker", HOME + "/.docker/cache",
    HOME + "/.config/systemd/user/unit",
])
def test_explicit_write_root_cannot_reopen_a_control_directory(restrict, root):
    with pytest.raises(ValueError):
        restrict(common(roots=[WORK, root]), ENV)


def test_implicit_tmp_cannot_reopen_control_but_excluded_tmp_is_irrelevant(restrict):
    env = {**ENV, "TMPDIR": "/run/user/999/tmp"}
    with pytest.raises(ValueError):
        restrict(common(), env)
    result = restrict(common("-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true"), env)
    assert profile(result)["filesystem"]["/run/user/999"] == "none"


@pytest.mark.parametrize("directory", ['手机."quoted"', '📱."quoted"'])
def test_quotes_unicode_and_dots_in_paths_are_toml_values_not_cli_keys(restrict, directory):
    data_root = "/data/" + directory + "/docker"
    env = {**ENV, "MEYO_DEVICE_DOCKER_DATA_ROOT": data_root}
    argv = common(roots=[WORK, '/data/project."quoted"'])
    fs = profile(restrict(argv, env))["filesystem"]
    assert fs[data_root] == "none"
    assert fs['/data/project."quoted"'] == "write"


def test_transform_is_pure_and_does_not_inspect_host_files_or_launch_anything(restrict, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("argv construction must not perform I/O")

    with monkeypatch.context() as patch:
        patch.setattr("builtins.open", forbidden)
        patch.setattr(Path, "stat", forbidden)
        patch.setattr(Path, "resolve", forbidden)
        patch.setattr(os, "access", forbidden)
        patch.setattr(subprocess, "Popen", forbidden)
        result = restrict(common(), ENV)
    assert profile(result)["filesystem"]["/run/user/999"] == "none"


def _offline_cli(argv, env, cwd):
    """Local validation only: deny network for CLI and children, isolate HOME."""
    process = subprocess.Popen(
        ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)(deny network*)",
         str(CLI), *argv],
        cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    try:
        out, err = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise
    return process.returncode, out, err


@pytest.mark.skipif(
    sys.platform != "darwin" or not CLI.is_file()
    or not Path("/usr/bin/sandbox-exec").is_file(),
    reason="optional offline check requires the specified local macOS Codex CLI",
)
def test_official_01534_parses_profile_offline_without_a_model(restrict, tmp_path, record_property):
    # Not a Linux AF_UNIX/connect test. The main task owns that verification.
    # Some macOS hosts refuse nested Seatbelt. Keep the outer no-network guard:
    # compare a valid profile with a deliberately invalid native permission.
    root = tmp_path.resolve()
    home, work, state, scratch, outside = (root / x for x in ("home", "work", "state", "tmp", "outside"))
    controls = [root / "daemon", root / "docker-data", home / ".config/systemd/user", home / ".docker"]
    for p in [home, work, state, scratch, outside, root / "codex", *controls]:
        p.mkdir(parents=True, exist_ok=True)
    for p in controls:
        (p / "probe.txt").write_text("fixture control data")
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
        "HOME": str(home), "CODEX_HOME": str(root / "codex"), "TMPDIR": str(scratch),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "MEYO_DEVICE_DOCKER_HOST": "unix://" + str(controls[0] / "docker.sock"),
        "MEYO_DEVICE_DOCKER_DATA_ROOT": str(controls[1]),
    }
    rc, version, err = _offline_cli(["--version"], env, root)
    assert rc == 0, err
    if version.strip() != "codex-cli 0.153.4":
        pytest.skip("installed CLI is not the requested 0.153.4; no version substitution")
    source = [
        "-C", str(work), "-s", "workspace-write",
        "-c", "sandbox_workspace_write.writable_roots=" + json.dumps([str(state), str(scratch)]),
        "-c", "sandbox_workspace_write.network_access=true",
        "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
        "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
    ]
    transformed = restrict(source, env)
    script = """
set -eu
printf work > "$1/written.txt"
printf state > "$2/written.txt"
printf temp > "$3/written.txt"
if (printf forbidden > "$4/written.txt") 2>/dev/null; then exit 71; fi
shift 4
for dir in "$@"; do
  if cat "$dir/probe.txt" >/dev/null 2>&1; then exit 72; fi
  if (printf forbidden > "$dir/written.txt") 2>/dev/null; then exit 73; fi
done
printf NATIVE_PROFILE_OK
"""
    native_args = [
        # Unlike `exec`, 0.153.4's `sandbox` requires an explicit profile selector.
        "sandbox", "-P", settings(transformed)["default_permissions"],
        *transformed, "--", "/bin/sh", "-c", script, "probe",
        *map(str, [work, state, scratch, outside, *controls]),
    ]
    bad_rc, _, bad_err = _offline_cli(
        [arg.replace('"none"', '"unsupported_access"') for arg in native_args], env, root,
    )
    assert bad_rc != 0 and "FilesystemPermissionToml" in bad_err
    assert "sandbox_apply" not in bad_err
    rc, out, err = _offline_cli(native_args, env, root)
    if rc == 71 and err.strip() == "sandbox-exec: sandbox_apply: Operation not permitted":
        # Parsing and native policy construction reached the Seatbelt launcher,
        # whereas invalid access was rejected earlier. Do not call this a
        # kernel-boundary pass or relax the outer network restriction.
        assert not any((p / "written.txt").exists() for p in [work, state, scratch, outside, *controls])
        record_property("verification_scope", "0.153.4 config parse; nested Seatbelt unavailable")
        return
    assert rc == 0, (rc, out, err)
    record_property("verification_scope", "0.153.4 config parse and macOS paths; not Linux AF_UNIX")
    assert "NATIVE_PROFILE_OK" in out
    assert (work / "written.txt").read_text() == "work"
    assert (state / "written.txt").read_text() == "state"
    assert (scratch / "written.txt").read_text() == "temp"
    assert not (outside / "written.txt").exists()
    assert all((p / "probe.txt").read_text() == "fixture control data" for p in controls)
