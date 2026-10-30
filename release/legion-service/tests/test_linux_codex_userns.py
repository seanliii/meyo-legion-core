"""Offline deployment-scope checks; real namespace isolation is verified on Linux."""
from pathlib import Path
import re
import subprocess

import pytest


DEPLOY = Path(__file__).resolve().parents[3] / "deploy/linux"


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("MEYO_LEGION_DIR", str(tmp_path / "meyo"))
    monkeypatch.setenv("LEGION_HOME_DIR", str(tmp_path / "legion"))


def test_userns_profile_attaches_only_to_the_immutable_codex_executable():
    text = (DEPLOY / "meyo-codex.apparmor").read_text()
    rules = "\n".join(line for line in text.splitlines()
                      if line.strip() and not line.lstrip().startswith("#"))
    assert re.search(
        r"profile meyo-legion-codex /opt/meyo-legion/native/codex "
        r"flags=\(unconfined\)\s*\{\s*userns,\s*\}", rules
    )
    assert "abi <abi/4.0>," in rules
    assert "*" not in rules
    assert "/usr/bin/bwrap" not in rules


def test_installer_loads_app_policy_without_disabling_the_host_restriction():
    text = (DEPLOY / "bootstrap.sh").read_text()
    start = text.index('CODEX_APPARMOR_PROFILE=/etc/apparmor.d/meyo-legion-codex')
    end = text.index('# Restores the already-declared built-in CLI', start)
    block = text[start:end]
    assert 'kernel.apparmor_restrict_unprivileged_userns' in block
    assert 'install -m 0644 "$HERE/meyo-codex.apparmor" "$CODEX_APPARMOR_PROFILE"' in block
    assert 'apparmor_parser -r "$CODEX_APPARMOR_PROFILE"' in block
    assert 'cmp -s "$HERE/meyo-codex.apparmor" "$CODEX_APPARMOR_PROFILE"' in block
    assert '[[ ! -L "$CODEX_APPARMOR_PROFILE" ]]' in block
    assert "sysctl -w" not in text
    assert "apparmor_restrict_unprivileged_userns=0" not in text
    assert start > text.index('install -m 0755 "$STAGE/codex/')
    assert start < text.index("systemctl enable --now")


def test_bootstrap_remains_valid_shell():
    result = subprocess.run(
        ["bash", "-n", str(DEPLOY / "bootstrap.sh")],
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
