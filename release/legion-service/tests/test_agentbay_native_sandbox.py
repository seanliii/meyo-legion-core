"""Optional native sandbox verification using only disposable, non-secret files."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest

from test_native_device_permissions import CLI, HELPER, _offline_cli, settings


@pytest.mark.skipif(
    sys.platform != "darwin" or not CLI.is_file()
    or not Path("/usr/bin/sandbox-exec").is_file(),
    reason="local macOS Codex sandbox is unavailable; no runtime is installed by this test",
)
def test_native_phone_private_parent_cannot_be_read_or_written(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    home, work, state, scratch, codex = (root / x for x in ("home", "work", "data", "tmp", "codex"))
    private = root / "secrets/agentbay"
    for directory in (home, work, state, scratch, codex, private / "control"):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    files = [private / "config.json", private / "api-key.txt", private / "control/lifecycle.json"]
    for path in files:
        path.write_text("non-secret test fixture")
        path.chmod(0o600)
    environment = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
        "HOME": str(home), "CODEX_HOME": str(codex), "TMPDIR": str(scratch),
        "MEYO_LEGION_DIR": str(state), "LEGION_HOME_DIR": str(root / "legion"),
        "MEYO_AGENTBAY_CONFIG": str(files[0]),
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    spec = importlib.util.spec_from_file_location("_phone_sandbox_permissions", HELPER)
    permissions = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(permissions)
    rc, version, err = _offline_cli(["--version"], environment, root)
    assert rc == 0, err
    if version.strip() != "codex-cli 0.153.4":
        pytest.skip("this proof requires local Codex 0.153.4; no runtime substitution")
    common = [
        "-C", str(work), "-s", "workspace-write",
        "-c", "sandbox_workspace_write.writable_roots=" + json.dumps([str(state), str(scratch)]),
        "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
        "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
    ]
    transformed = permissions.restrict_codex_device_control(common, environment)
    chosen = settings(transformed)["default_permissions"]
    script = """
set -eu
printf work > "$1/written.txt"
printf state > "$2/written.txt"
printf temp > "$3/written.txt"
if (printf forbidden > "$4/new-file") 2>/dev/null; then exit 72; fi
if (printf forbidden > "$4/control/new-file") 2>/dev/null; then exit 73; fi
shift 4
for file in "$@"; do
  if cat "$file" >/dev/null 2>&1; then exit 74; fi
  if (printf forbidden > "$file") 2>/dev/null; then exit 75; fi
done
printf PHONE_PRIVATE_PARENT_DENIED
"""
    args = ["sandbox", "-P", chosen, *transformed, "--", "/bin/sh", "-c", script,
            "probe", *map(str, (work, state, scratch, private, *files))]
    bad_rc, _, bad_err = _offline_cli(
        [item.replace('"none"', '"unsupported_access"') for item in args], environment, root,
    )
    assert bad_rc != 0 and "FilesystemPermissionToml" in bad_err
    rc, out, err = _offline_cli(args, environment, root)
    assert all(path.read_text() == "non-secret test fixture" for path in files)
    assert not (private / "new-file").exists() and not (private / "control/new-file").exists()
    if rc == 71 and err.strip() == "sandbox-exec: sandbox_apply: Operation not permitted":
        assert all(not (directory / "written.txt").exists() for directory in (work, state, scratch))
        pytest.skip("native profile parsed, but nested Seatbelt is unavailable; no kernel-boundary proof")
    assert rc == 0 and "PHONE_PRIVATE_PARENT_DENIED" in out, (rc, out, err)
    for directory, text in ((work, "work"), (state, "state"), (scratch, "temp")):
        assert (directory / "written.txt").read_text() == text
