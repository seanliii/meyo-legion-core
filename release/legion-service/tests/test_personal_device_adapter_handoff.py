"""Capture the actual adapter's catalog and device permission argv together."""
import os
from pathlib import Path

import pytest

from test_personal_codex_model_catalog import adapter, context, isolate, managed


@pytest.mark.parametrize("is_managed", [False, True])
@pytest.mark.parametrize("resume", [False, True])
def test_real_adapter_composes_device_command_boundary_and_bound_catalog(
        adapter, monkeypatch, is_managed, resume):
    if is_managed:
        managed(adapter, monkeypatch)
    home = Path(os.environ["HOME"])
    control = f"/run/user/{os.getuid()}"
    data = home / ".local/share/docker"
    monkeypatch.setenv("MEYO_DEVICE_DOCKER_HOST", f"unix://{control}/docker.sock")
    monkeypatch.setenv("MEYO_DEVICE_DOCKER_DATA_ROOT", str(data))
    ctx = context(adapter, resume=resume)
    result = adapter.hr._codex_adapter("__ut_personal_devices", "original task", ctx=ctx)
    assert result["status"] == "ok", result
    call, = adapter.calls
    config = call["config"]
    profile = config.get("default_permissions")
    assert profile, "the actual native send still exposes the device control socket"
    filesystem = config["permissions"][profile]["filesystem"]
    assert filesystem[control] == filesystem[str(data)] == "none"
    assert filesystem[str(home / ".config/systemd/user")] == "none"
    assert filesystem[str(home / ".docker")] == "none"
    assert filesystem[ctx.workdir] == "write"
    assert "-s" not in call["command"] and "--sandbox" not in call["command"]
    assert "sandbox_mode" not in config and "sandbox_workspace_write" not in config
    assert call["catalog"]["models"][0]["slug"] == ctx.model
    assert call["command"][call["command"].index("-m") + 1] == ctx.model
    assert ("resume" in call["command"]) is resume
    assert not call["path"].exists()
    assert ctx.appid not in repr(call["command"]) + repr(filesystem)
