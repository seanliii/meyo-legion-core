"""Owned desktop entry boundaries; no real app, network, credentials or model."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


FILE = Path(__file__).resolve().parents[1] / "external_adapters/ohmygame_native.py"


@pytest.fixture
def native(tmp_path, monkeypatch):
    for key, folder in (("HOME", "home"), ("MEYO_LEGION_DIR", "data"),
                        ("LEGION_HOME_DIR", "legion"), ("LEGION_PROC_REGISTRY_DIR", "registry")):
        monkeypatch.setenv(key, str(tmp_path / folder))
    assert FILE.is_file(), "Owned OhMyGame desktop entry has not been implemented"
    spec = importlib.util.spec_from_file_location("ohmygame_native_test", FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_refuses_symlinked_private_profile(native, tmp_path):
    destination = tmp_path / "other"
    destination.mkdir(mode=0o700)
    link = tmp_path / "operator"
    link.symlink_to(destination, target_is_directory=True)
    with pytest.raises(ValueError):
        native.private_directory(link)


def test_pid_reuse_cannot_be_adopted_as_our_application(native, monkeypatch):
    record = {"pid": 32000, "birth": "original", "app": "/owned/OhMyGame",
              "kind": "meyo-ohmygame-desktop-v1"}
    monkeypatch.setattr(native, "process_identity", lambda pid: {
        "birth": "different", "comm": "/owned/OhMyGame", "uid": native.os.getuid(), "pgid": pid,
    })
    with pytest.raises(ValueError):
        native.owned_state(record, Path("/owned/OhMyGame"))


def test_executable_mismatch_cannot_be_adopted(native, monkeypatch):
    record = {"pid": 32000, "birth": "same", "app": "/owned/OhMyGame",
              "kind": "meyo-ohmygame-desktop-v1"}
    monkeypatch.setattr(native, "process_identity", lambda pid: {
        "birth": "same", "comm": "/other/OhMyGame", "uid": native.os.getuid(), "pgid": pid,
    })
    with pytest.raises(ValueError):
        native.owned_state(record, Path("/owned/OhMyGame"))


def test_missing_process_is_not_a_verified_wait(native, monkeypatch):
    record = {"pid": 32000, "birth": "same", "app": "/owned/OhMyGame",
              "kind": "meyo-ohmygame-desktop-v1"}
    monkeypatch.setattr(native, "process_identity", lambda pid: None)
    assert native.owned_state(record, Path("/owned/OhMyGame")) is None


def test_connector_timeout_returns_known_native_receipt(native, monkeypatch, tmp_path, capsys):
    runtime = tmp_path / "runtime"
    private = runtime / "operator"
    private.mkdir(mode=0o700, parents=True)
    request = tmp_path / "request.json"
    request.write_text('{"projectId":"p","conversationId":"c","prompt":"original"}')
    native.save_record(private / "desktop.json", {"pid": 32000, "port": 43111})
    app = runtime / "app/OhMyGame.app"
    executable = app / "Contents/MacOS/OhMyGame"
    monkeypatch.setattr(native, "installed", lambda root: (app, executable))
    monkeypatch.setattr(native, "owned_state", lambda record, exe: record)
    monkeypatch.setattr(native, "verify_listener", lambda pid, port: None)

    def timeout(command, **kwargs):
        receipt = (Path(command[-1]) if Path(command[-1]).parent.name == "operations"
                   else private / "test-receipt.json")
        native.save_record(receipt, {
            "stage": "turn_submission", "projectId": "p",
            "conversationId": "c", "submission": "unknown",
        })
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 50))

    monkeypatch.setattr(native, "run", timeout)
    assert native.main(["--runtime-dir", str(runtime), "delegate", "--request-file", str(request)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["native"]["projectId"] == "p"
    assert result["native"]["conversationId"] == "c"
    assert result["native"]["submission"] == "unknown"
    assert json.loads(request.read_text())["prompt"] == "original"
