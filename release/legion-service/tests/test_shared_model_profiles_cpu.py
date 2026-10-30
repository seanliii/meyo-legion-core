"""Private per-call profiles; fake credentials, loopback HTTP and real CLI children only."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
import os
from pathlib import Path
import select
import signal
import struct
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import zlib

import pytest


SERVICE = Path(__file__).resolve().parents[1]
ORCH = SERVICE.parents[1] / "harness_baselines/meyo-orchestrator"
SERVER = ORCH / "meyo_mcp_servers/orchestrator_server.py"
PROFILE_FIELDS = ("MEYO_PERSONAL_STATE_DIR", "LEGION_PERSONAL_OWNER",
                  "MEYO_USER_ID", "MEYO_WORKDIR")


def _png():
    def chunk(kind, content):
        return (struct.pack(">I", len(content)) + kind + content
                + struct.pack(">I", zlib.crc32(kind + content)))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))


def _jpeg():
    # A complete 1x1 grayscale JPEG; no image library is needed at test runtime.
    return bytes.fromhex(
        "ffd8ffe000104a46494600010100000100010000ffdb00430008060607060508"
        "0707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720"
        "222c231c1c2837292c30313434341f27393d38323c2e333432ffc0000b080001"
        "000101011100ffc40014000100000000000000000000000000000000ffc40014"
        "100100000000000000000000000000000000ffda0008010100003f003fffd9"
    )


@pytest.fixture(autouse=True)
def case(tmp_path, monkeypatch):
    # Collection and execution must never inherit real provider or user state.
    for key in tuple(os.environ):
        if key.startswith(("MEYO_", "LEGION_", "OPENAI_", "OPENROUTER_",
                           "AIGC_", "ANTHROPIC_", "CODEX_")):
            monkeypatch.delenv(key)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    for key in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "LEGION_PROC_REGISTRY_DIR"):
        folder = tmp_path / key
        folder.mkdir()
        monkeypatch.setenv(key, str(folder))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("LEGION_EXT_AUTO_UPDATE", "0")
    monkeypatch.setenv("LEGION_GATEWAY_RETRIES", "0")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.syspath_prepend(str(ORCH))
    monkeypatch.syspath_prepend(str(SERVICE))
    runtime = importlib.import_module("personal_runtime")
    cli = importlib.import_module("media_model_cli")
    media = importlib.import_module("media_tools")
    provider = importlib.import_module("agent_tool_provider")
    tools = importlib.import_module("legion_tool")
    native = importlib.import_module("native_capability_host")
    registry = importlib.import_module("legion_proc_registry")
    monkeypatch.setattr(tools, "_record_tool_usage", lambda *a, **k: {})
    monkeypatch.setattr(importlib.import_module("external_agent_registry"),
                        "load_catalog", lambda: [])
    seen, children = [], []
    received, release = threading.Event(), threading.Event()
    response = {
        "model": "fixture-executor", "provider": "Fixture Provider", "id": "req-fixture",
        "usage": {"total_tokens": 7, "cost": 0.0007, "debug": "fake-private"},
        "debug": {"key": "fake-alice-key"},
        "_configured_model": "forged-by-upstream",
        "choices": [{"finish_reason": "stop", "message": {
            "content": "ok", "reasoning_content": "fake-private"}}],
    }
    mode = {"block": False, "redirect": False}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({
                "path": self.path, "body": body,
                "alice_key": self.headers.get("Authorization") == "Bearer fake-alice-key",
                "bob_key": self.headers.get("Authorization") == "Bearer fake-bob-key",
                "company_key": self.headers.get("Authorization") == "Bearer fake-company-key",
            })
            received.set()
            if mode["block"]:
                release.wait(5)
            if mode["redirect"]:
                self.send_response(307)
                self.send_header("Location", f"http://127.0.0.1:{server.server_port}/trap")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            content = json.dumps(response).encode()
            self.send_response(mode.get("status", 200))
            self.send_header("Content-Type", "application/json")
            self.send_header("x-request-id", "header-request")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Deliberate cancellation of our own fake request.

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    company = tmp_path / "company-models.json"
    company.write_text(json.dumps({direction: {"candidates": [
        {"model": "company-model", "appids": ["fake-company-key"]},
    ]} for direction in ("programming", "vision_understanding")}))
    monkeypatch.setenv("MEYO_MODEL_CATALOG_PATH", str(company))
    monkeypatch.setenv("MEYO_MEDIA_GATEWAY", base + "/company/v1")

    def profile(owner="alice"):
        root = tmp_path / (owner + "-instance")
        work = tmp_path / (owner + "-work")
        work.mkdir()
        runtime.initialize(root, owner, 18080)
        key = tmp_path / (owner + "-key")
        key.write_text("fake-" + owner + "-key")
        key.chmod(0o600)
        runtime.configure_model(root, "unchanged-driver", base + "/default", key)
        runtime.save_model_profile(root, "aux", owner + "-router",
                                   base + "/" + owner + "/v1", key,
                                   input_modalities=["text", "image"])
        env, _ = runtime.environment(root, {"PATH": os.environ.get("PATH", os.defpath),
                                            "NO_PROXY": "127.0.0.1,localhost"})
        # Supply the proposed trusted contract explicitly so tests can go red
        # on the resolver, not just on the launcher's new export.
        env.update(MEYO_PERSONAL_STATE_DIR=str(root), LEGION_PERSONAL_OWNER=owner,
                   MEYO_USER_ID=owner, MEYO_WORKDIR=str(work),
                   MEYO_MODEL_CATALOG_PATH=str(company), MEYO_MEDIA_GATEWAY=base + "/company/v1")
        return SimpleNamespace(root=root, work=work, env=env)

    alice = profile()
    original_popen = media.subprocess.Popen

    def spawn(*args, **kwargs):
        proc = original_popen(*args, **kwargs)
        # subprocess is a shared module: registry `ps` observations are not
        # model children and must not be counted or killed as process groups.
        if kwargs.get("start_new_session"):
            children.append(proc)
        return proc

    monkeypatch.setattr(media.subprocess, "Popen", spawn)

    def call(params=None, *, name="model_chat", env=None, inherit=False, check=None):
        with provider._runtime_scope(alice.env if env is None else env, check,
                                     inherit_process_context=inherit):
            return tools.run_tool("runnable:" + name, params if params is not None else
                                  {"profile_id": "aux", "prompt": "test", "timeout_s": 3})

    value = SimpleNamespace(
        runtime=runtime, cli=cli, media=media, native=native, provider=provider,
        tools=tools, registry=registry, profile=profile, alice=alice, call=call,
        seen=seen, received=received, release=release, response=response, mode=mode,
        base=base, children=children, patch=monkeypatch, root=tmp_path,
    )
    try:
        yield value
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        for proc in children:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            proc.wait(timeout=3)


def _assert_clean(case, env=None):
    env = case.alice.env if env is None else env
    assert all(proc.poll() is not None for proc in case.children)
    assert not list(Path(env["LEGION_PROC_REGISTRY_DIR"]).glob("*.json"))


def _operator_chat_cli(case, *, as_json, image=None, workspace_bound=True):
    command = [sys.executable, "-B", str(SERVICE / "media_model_cli.py"),
               "chat", "fixture question", "--domain",
               "vision_understanding" if image else "programming", "--max-tokens", "1"]
    if image:
        command.extend(["--image", str(image)])
    if as_json:
        command.append("--json")
    env = dict(case.alice.env, HOME=str(case.root / "HOME"), PYTHONNOUSERSITE="1",
               PYTHONDONTWRITEBYTECODE="1")
    if not workspace_bound:
        env.pop("MEYO_WORKDIR", None)
        env.pop("MEYO_MODEL_WORKSPACE_ID", None)
    process = subprocess.Popen(
        command, cwd=case.alice.work, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=8)
        return process.returncode, stdout, stderr
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=3)


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
@pytest.mark.parametrize("reason,answer,error", [
    ("length", "partial answer must not escape", "media_output_limit"),
    ("stop", "", "media_chat_empty_response"),
    ("invalid\nreason", "not a valid final", "media_finish_reason_invalid"),
])
def test_operator_cli_failure_keeps_safe_receipt_only_in_json(case, as_json, reason, answer, error):
    case.response["choices"] = [{"finish_reason": reason, "message": {
        "content": answer, "reasoning_content": "private reasoning must not escape"}}]
    case.response["usage"] = {"input_tokens": 5, "output_tokens": 1, "cost": 0.04,
                              "debug": "fake-company-key"}
    code, stdout, stderr = _operator_chat_cli(case, as_json=as_json)
    assert code == 1 and stderr == error + "\n"
    assert len(case.seen) == 1 and case.seen[0]["company_key"]
    if not as_json:
        assert stdout == ""
    else:
        assert stdout.strip(), "explicit --json lost the existing safe failure receipt"
        result = json.loads(stdout)
        assert result == {
            "ok": False, "isError": True, "error": error,
            "configured_model": "company-model", "reported_model": "fixture-executor",
            "provider": "Fixture Provider", "request_id": "req-fixture",
            "usage": {"input_tokens": 5, "output_tokens": 1, "cost": 0.04},
            "usage_scope": "single_model_tool_request", "cost": 0.04,
            "cost_source": "usage.cost", "max_tokens": 1,
            "finish_reason": None if error == "media_finish_reason_invalid" else reason,
            "input_image": None,
        }
    for private in ("fake-company-key", "fake-alice-key", "private reasoning", "partial answer"):
        assert private not in stdout + stderr
    _assert_clean(case)


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_operator_cli_http_error_never_exports_response_body(case, as_json):
    case.mode["status"] = 403
    case.response["error"] = "fake-company-key is present in this forbidden response body"
    code, stdout, stderr = _operator_chat_cli(case, as_json=as_json)
    assert (code, stdout, stderr) == (1, "", "media_http_error:403\n")
    assert len(case.seen) == 1
    _assert_clean(case)


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_operator_cli_success_output_stays_compatible(case, as_json):
    code, stdout, stderr = _operator_chat_cli(case, as_json=as_json)
    assert code == 0 and stderr == ""
    if as_json:
        result = json.loads(stdout)
        assert result["ok"] is True and result["answer"] == "ok"
        assert result["configured_model"] == "company-model"
    else:
        assert stdout == "ok\n"
    _assert_clean(case)


@pytest.mark.parametrize("reason,answer,error", [
    ("length", "partial answer must not escape", "media_output_limit"),
    ("stop", "", "media_chat_empty_response"),
    ("invalid\nreason", "not a valid final", "media_finish_reason_invalid"),
])
def test_see_image_failure_preserves_bundled_safe_receipt(case, reason, answer, error):
    image = case.alice.work / "frame.png"
    image.write_bytes(_png())
    case.response["choices"] = [{"finish_reason": reason, "message": {
        "content": answer, "reasoning_content": "private reasoning"}}]
    result = case.call({"image_path": str(image), "question": "describe", "timeout_s": 3},
                       name="see_image")
    assert result["ok"] is False and result["error"] == error
    assert result.get("configured_model") == "company-model", result
    assert result["reported_model"] == "fixture-executor"
    assert result["provider"] == "Fixture Provider"
    assert result["request_id"] == "req-fixture"
    assert result["usage"] == {"total_tokens": 7, "cost": 0.0007}
    assert result["usage_scope"] == "single_model_tool_request"
    assert result["cost"] == 0.0007 and result["cost_source"] == "usage.cost"
    assert result["finish_reason"] == (None if error == "media_finish_reason_invalid" else reason)
    assert result["metadata_source"] == "bundled_cli_json"
    received = base64.b64decode(
        case.seen[0]["body"]["messages"][0]["content"][0]["image_url"]["url"].split(",", 1)[1],
        validate=True)
    assert result["input_image"] == {
        "sha256": hashlib.sha256(received).hexdigest(),
        "bytes": len(received), "mime_type": "image/png",
    }
    assert "answer" not in result
    for private in ("partial answer", "fake-private", "fake-company-key", "private reasoning"):
        assert private not in json.dumps(result)
    assert len(case.seen) == 1 and case.seen[0]["company_key"]
    _assert_clean(case)


def test_see_image_http_error_still_hides_body_and_does_not_invent_receipt(case):
    image = case.alice.work / "frame.png"
    image.write_bytes(_png())
    case.mode["status"] = 403
    case.response["error"] = "fake-company-key in HTTP body"
    result = case.call({"image_path": str(image), "question": "describe", "timeout_s": 3},
                       name="see_image")
    assert result["ok"] is False and result["error"] == "media_http_error:403"
    assert "answer" not in result and result.get("cost") is None
    assert result.get("input_image") is None
    assert "fake-company-key" not in json.dumps(result)
    assert len(case.seen) == 1
    _assert_clean(case)


@pytest.mark.parametrize("code", [0, 1])
def test_see_image_overridden_legacy_cli_keeps_text_contract(case, code):
    image = case.alice.work / "frame.png"
    image.write_bytes(_png())
    cli = case.root / "legacy-cli.py"
    cli.write_text("import sys\nassert '--json' not in sys.argv\n"
                   "print('legacy text answer')\n"
                   f"raise SystemExit({code})\n")
    case.patch.setenv("MEYO_MEDIA_MODEL_CLI", str(cli))
    result = case.call({"image_path": str(image), "timeout_s": 3}, name="see_image")
    assert result["ok"] is (code == 0)
    if code == 0:
        assert result["answer"] == "legacy text answer"
        assert result["metadata_source"] == "legacy_cli_text"
        assert result["cost"] is None
    else:
        assert "answer" not in result and "metadata_source" not in result
    assert not case.seen
    _assert_clean(case)


def test_launcher_exports_only_its_own_profile_source(case):
    env, _ = case.runtime.environment(case.alice.root, {
        "MEYO_PERSONAL_STATE_DIR": "/untrusted", "LEGION_PERSONAL_OWNER": "bob",
    })
    assert env["MEYO_PERSONAL_STATE_DIR"] == str(case.alice.root)
    assert env["LEGION_PERSONAL_OWNER"] == "alice"


def test_real_shared_child_uses_one_profile_and_does_not_activate(case):
    paths = (case.alice.root / "secrets/model.json",
             case.alice.root / "secrets/model-profiles.json", case.alice.root / ".configure.lock")
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
    result = case.call()
    assert result["ok"] is True, result
    assert len(case.children) == len(case.seen) == 1
    request = case.seen[0]
    assert request["path"] == "/alice/v1/chat/completions"
    assert request["alice_key"] and not request["company_key"]
    assert request["body"]["model"] == result["configured_model"] == "alice-router"
    assert result["reported_model"] == "fixture-executor"
    assert result["provider"] == "Fixture Provider" and result["request_id"] == "req-fixture"
    assert result["cost"] == result["usage"]["cost"] == 0.0007
    assert all((p.read_bytes(), p.stat().st_mtime_ns) == snapshot for p, snapshot in before.items())
    assert "fake-alice-key" not in json.dumps(result)
    assert "fake-private" not in json.dumps(result)
    assert "fake-alice-key" not in repr(case.children[0].args)
    _assert_clean(case)


def test_profile_catalog_is_scoped_readonly_and_never_a_live_probe(case):
    result = case.call({"profile_id": "aux"}, name="model_catalog")
    assert result["ok"] is True, result
    assert result["live_verified"] is False
    row, = result["candidates"]
    assert row["model"] == "alice-router" and row["base_url"] == case.base + "/alice/v1"
    assert row["credential_present"] is True and row["inputs"] == ["text", "image"]
    assert not case.seen
    assert "fake-alice-key" not in json.dumps(result)
    _assert_clean(case)


@pytest.mark.parametrize("name", ["model_chat", "model_catalog"])
@pytest.mark.parametrize("fault,error", [
    ("empty", "media_profile_binding_required"), ("root", "media_profile_binding_required"),
    ("caller", "media_profile_binding_required"), ("owner", "media_profile_binding_required"),
    ("workdir", "media_profile_binding_required"), ("other_owner", "media_profile_owner_mismatch"),
    ("other_caller", "media_profile_owner_mismatch"),
    ("unknown", "media_profile_unknown"), ("id_path", "media_profile_id_invalid"),
    ("workspace_contains_state", "media_profile_workspace_invalid"),
    ("workspace_is_secrets", "media_profile_workspace_invalid"),
])
def test_profile_failures_never_borrow_the_host_company_or_owner(case, name, fault, error):
    env = dict(case.alice.env)
    params = {"profile_id": "aux", **({"prompt": "test"} if name == "model_chat" else {})}
    for key, value in case.alice.env.items():
        case.patch.setenv(key, value)  # Deliberately tempting ambient Alice credentials.
    if fault == "empty":
        env = {}
    elif fault in ("root", "caller", "owner", "workdir"):
        env.pop({"root": "MEYO_PERSONAL_STATE_DIR", "caller": "MEYO_USER_ID",
                 "owner": "LEGION_PERSONAL_OWNER", "workdir": "MEYO_WORKDIR"}[fault])
    elif fault == "other_owner":
        env["LEGION_PERSONAL_OWNER"] = "bob"
    elif fault == "other_caller":
        env["MEYO_USER_ID"] = "bob"
    elif fault == "unknown":
        params["profile_id"] = "not-saved"
    elif fault == "id_path":
        params["profile_id"] = "../secrets/model"
    elif fault == "workspace_contains_state":
        env["MEYO_WORKDIR"] = str(case.root)
    elif fault == "workspace_is_secrets":
        env["MEYO_WORKDIR"] = str(case.alice.root / "secrets")
    result = case.call(params, name=name, env=env)
    assert result["ok"] is False and result["error"] == error, result
    assert not case.seen
    assert "fake-" not in json.dumps(result)


@pytest.mark.parametrize("fault", ["profile_symlink", "profile_hardlink", "profile_public",
                                  "root_public", "secrets_symlink", "secrets_public",
                                  "config_public", "bad_json", "bad_binding", "bad_config"])
def test_private_file_security_is_reused_without_default_fallback(case, fault):
    path = case.alice.root / "secrets/model-profiles.json"
    if fault == "profile_symlink":
        other = case.root / "private-copy"
        path.rename(other)
        path.symlink_to(other)
    elif fault == "profile_hardlink":
        os.link(path, case.root / "linked-profile")
    elif fault.endswith("_public"):
        target = {"profile_public": path, "root_public": case.alice.root,
                  "secrets_public": path.parent, "config_public": case.alice.root / "config.json"}[fault]
        target.chmod(0o755 if target.is_dir() else 0o644)
    elif fault == "secrets_symlink":
        other = case.root / "private-secrets"
        path.parent.rename(other)
        path.parent.symlink_to(other, target_is_directory=True)
    elif fault == "bad_json":
        path.write_text("{not-json")
    elif fault == "bad_binding":
        path.write_text(json.dumps({"aux": {"model": "oops", "base_url": case.base}}))
    elif fault == "bad_config":
        config = case.alice.root / "config.json"
        data = json.loads(config.read_text())
        data["host"] = "0.0.0.0"
        config.write_text(json.dumps(data))
    result = case.call()
    assert result["ok"] is False and result["error"] == "media_profile_configuration_invalid", result
    assert not case.seen


def test_model_override_must_match_the_selected_profile(case):
    result = case.call({"profile_id": "aux", "prompt": "test", "model": "company-model"})
    assert result["error"] == "media_profile_model_conflict" and not case.seen
    assert case.call({"profile_id": "aux", "prompt": "test", "model": "alice-router"})["ok"]


@pytest.mark.parametrize("extra", [
    {"api_key": "forged"}, {"base_url": "http://127.0.0.1:1"}, {"state_dir": "/private"},
    {"environment": {"MEYO_USER_ID": "alice"}}, {"env": {}}, {"owner": "alice"},
    {"image_bytes": "untrusted"}, {"MEYO_PERSONAL_STATE_DIR": "/private"},
])
def test_tool_parameters_cannot_supply_trust_or_image_snapshots(case, extra):
    events = []
    with case.provider._runtime_scope(case.alice.env, None, inherit_process_context=False):
        result = case.tools.run_tool("runnable:model_chat",
                                     {"profile_id": "aux", "prompt": "test", **extra},
                                     event_cb=events.append)
    assert result["error"] == "media_tool_parameters_invalid"
    assert not case.seen and not events and not case.children


def test_profile_binding_and_thread_context_do_not_leak_between_owners(case):
    bob = case.profile("bob")
    with case.provider._runtime_scope({"MEYO_USER_ID": "outer"}, None, inherit_process_context=False):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(case.call, env=owner.env) for owner in (case.alice, bob)]
            results = [f.result(timeout=8) for f in futures]
        assert case.provider._runtime_binding()[1]["MEYO_USER_ID"] == "outer"
    assert all(r["ok"] for r in results), results
    assert {r["configured_model"] for r in results} == {"alice-router", "bob-router"}
    assert len(case.seen) == 2
    for request in case.seen:
        owner = request["body"]["model"].split("-")[0]
        assert request[owner + "_key"] and not request["company_key"]
        assert request["path"] == "/" + owner + "/v1/chat/completions"
    for owner in (bob, case.alice):
        assert case.call({"profile_id": "aux"}, env=owner.env, name="model_catalog")["candidates"][0]["model"] == owner.env["MEYO_USER_ID"] + "-router"
        _assert_clean(case, owner.env)


@pytest.mark.parametrize("entry", ["shared", "native", "legacy"])
@pytest.mark.parametrize("folder", ["frames", ".playwright-mcp", ".local-run"])
@pytest.mark.parametrize("raw,mime", [
    pytest.param(_png(), "png", id="png"),
    pytest.param(_jpeg(), "jpeg", id="jpeg-misnamed-png"),
])
def test_image_bytes_reach_the_real_http_receiver(case, folder, entry, raw, mime):
    directory = case.alice.work / folder
    directory.mkdir()
    image = directory / "frame.png"
    image.write_bytes(raw)
    params = {"profile_id": "aux", "prompt": "describe", "image": folder + "/frame.png"}
    if entry == "native":
        session = case.native.CapabilitySession(
            _context(case, ["runnable:model_chat"]), environment=case.alice.env)
        result = session.call_capability({"tool_id": "runnable:model_chat", "params": params})
    elif entry == "legacy":
        # No profile support is added to the old operator CLI. Its existing
        # task workspace comes from the thread, not a tempting ambient root.
        ambient = case.root / "ambient-work"
        ambient.mkdir()
        case.patch.setenv("MEYO_WORKDIR", str(ambient))
        case.patch.chdir(SERVICE)
        result = case.call({"image_path": str(image), "question": "describe", "timeout_s": 3},
                           name="see_image")
    else:
        result = case.call(params)
    assert result["ok"] is True, result
    assert len(case.seen) == 1
    content = case.seen[0]["body"]["messages"][0]["content"]
    assert content[0]["type"] == "image_url"
    header, encoded = content[0]["image_url"]["url"].split(",", 1)
    received = base64.b64decode(encoded, validate=True)
    assert header == f"data:image/{mime};base64"
    assert received == raw
    assert hashlib.sha256(received).digest() == hashlib.sha256(raw).digest()
    assert result["input_image"] == {
        "sha256": hashlib.sha256(received).hexdigest(),
        "bytes": len(received), "mime_type": f"image/{mime}",
    }
    assert content[1] == {"type": "text", "text": "describe"}
    assert case.seen[0]["company_key"] is (entry == "legacy")


@pytest.mark.parametrize("as_json", [False, True])
def test_unbound_operator_keeps_its_existing_image_contract(case, as_json):
    # Deliberately no workspace/profile: this old CLI branch remains suffix-based,
    # not a new shared-tool route or a newly imposed workspace/content validator.
    image = case.root / "operator-frame.png"
    image.write_bytes(_jpeg())
    code, stdout, stderr = _operator_chat_cli(
        case, as_json=as_json, image=image, workspace_bound=False)
    assert code == 0 and stderr == ""
    if as_json:
        result = json.loads(stdout)
        assert result["answer"] == "ok"
        # MIME records the actual outgoing declaration, not invented validation.
        assert result["input_image"] == {
            "sha256": hashlib.sha256(_jpeg()).hexdigest(),
            "bytes": len(_jpeg()), "mime_type": "image/png",
        }
    else:
        assert stdout == "ok\n"
    assert len(case.seen) == 1 and case.seen[0]["company_key"]
    data_url = case.seen[0]["body"]["messages"][0]["content"][0]["image_url"]["url"]
    header, encoded = data_url.split(",", 1)
    assert header == "data:image/png;base64"
    assert base64.b64decode(encoded, validate=True) == _jpeg()


@pytest.mark.parametrize("suffix,raw", [
    pytest.param(".png", b"\x89PNG\r\n\x1a\nprivate text", id="forged-png-header"),
    pytest.param(".jpg", _png()[:29] + b"\x00" * 4 + _png()[33:], id="bad-png-crc"),
    pytest.param(".jpg", _png()[:-12], id="truncated-misnamed-png"),
    pytest.param(".png", _jpeg()[:-2], id="truncated-misnamed-jpeg"),
    pytest.param(".png", b"\xff\xd8\xffprivate text", id="forged-jpeg-header"),
    pytest.param(".png", b"GIF89a" + b"\x00" * 12, id="missing-gif-trailer"),
    pytest.param(".png", b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 4,
                 id="bad-webp-length"),
    pytest.param(".png", b"RIFF\x0c\x00\x00\x00WAVE" + b"\x00" * 8,
                 id="unsupported-riff-type"),
    pytest.param(".png", b"<svg xmlns='http://www.w3.org/2000/svg'/>",
                 id="unsupported-svg-content"),
    pytest.param(".png", b"BM" + b"\x00" * 32, id="unsupported-bmp-content"),
])
def test_format_detection_does_not_bypass_existing_validation(case, suffix, raw):
    image = case.alice.work / ("invalid" + suffix)
    image.write_bytes(raw)
    result = case.call({"profile_id": "aux", "prompt": "test", "image": str(image),
                        "timeout_s": 2})
    assert result["ok"] is False and result["error"].startswith("media_image_"), result
    assert not case.seen


@pytest.mark.parametrize("suffix", [".txt", ".svg", ".avif"])
def test_valid_image_signature_does_not_allow_unsupported_suffixes(case, suffix):
    image = case.alice.work / ("frame" + suffix)
    image.write_bytes(_jpeg())
    for name, value in case.alice.env.items():
        case.patch.setenv(name, value)

    def forbidden(*args, **kwargs):
        pytest.fail("unsupported suffix must be rejected before reading the file")

    case.patch.setattr(case.native, "_read_regular_at", forbidden)
    result = case.cli.run_model_tool("model_chat", {
        "profile_id": "aux", "prompt": "test", "image": str(image)})
    assert result["ok"] is False and result["error"] == "media_image_type_unsupported", result
    assert not case.seen


@pytest.mark.parametrize("entry", ["shared", "native", "bound_cli"])
@pytest.mark.parametrize("image", [True, False], ids=["image", "text"])
def test_bound_read_denial_blocks_image_but_not_text(case, entry, image):
    (case.alice.work / "frame.png").write_bytes(_png())
    policy = {"billable": True, "read": False}
    params = {"profile_id": "aux", "prompt": "inspect",
              **({"image": "frame.png"} if image else {})}
    if entry == "native":
        context = _context(case, ["runnable:model_chat"])
        context["capability_policy"] = policy
        session = case.native.CapabilitySession(context, environment=case.alice.env)
        result = session.call_capability({"tool_id": "runnable:model_chat", "params": params})
    elif entry == "bound_cli":
        for name, value in case.alice.env.items():
            case.patch.setenv(name, value)
        case.patch.setenv("MEYO_CAPABILITY_POLICY_JSON", json.dumps(policy))
        result = case.cli.run_model_tool("model_chat", params)
    else:
        result = case.call({**params, "_capability_policy": policy})
    if image:
        assert result["ok"] is False and result["error"] == "media_image_read_denied", result
        assert not case.seen
    else:
        assert result["ok"] is True and len(case.seen) == 1, result


def test_image_read_denial_cannot_be_widened_by_call_policy_or_company_selection(case):
    (case.alice.work / "frame.png").write_bytes(_png())
    env = {**case.alice.env, "MEYO_CAPABILITY_POLICY_JSON": json.dumps({"read": False})}
    result = case.call({
        "prompt": "inspect", "image": "frame.png", "model": "company-model",
        "_capability_policy": {"read": True, "billable": True},
    }, env=env)
    assert result["ok"] is False and result["error"] == "media_image_read_denied", result
    assert not case.seen


@pytest.mark.parametrize("fault", ["outside", "missing_binding"])
def test_legacy_thread_image_never_borrows_the_host_workspace(case, fault):
    ambient = case.root / "ambient-work"
    ambient.mkdir()
    image = ambient / "frame.png"
    image.write_bytes(_png())
    case.patch.setenv("MEYO_WORKDIR", str(ambient))
    env = dict(case.alice.env)
    if fault == "missing_binding":
        env.pop("MEYO_WORKDIR")
    result = case.call({"image_path": str(image), "timeout_s": 3}, name="see_image", env=env)
    assert result["ok"] is False and result["error"].startswith("media_image_"), result
    assert not case.seen


@pytest.mark.parametrize("fault", ["outside", "symlink", "parent_symlink", "hardlink",
                                  "fifo", "not_image", "oversized", "dotfile"])
def test_image_cannot_expand_the_workspace_read_boundary(case, fault):
    outside = case.root / "outside.png"
    outside.write_bytes(_png())
    image = case.alice.work / "frame.png"
    if fault == "outside":
        image = outside
    elif fault == "symlink":
        image.symlink_to(outside)
    elif fault == "parent_symlink":
        parent = case.alice.work / "linked"
        parent.symlink_to(case.root, target_is_directory=True)
        image = parent / "outside.png"
    elif fault == "hardlink":
        os.link(outside, image)
    elif fault == "fifo":
        os.mkfifo(image)
    elif fault == "not_image":
        image.write_text("this is private text, not an image")
    elif fault == "oversized":
        with image.open("wb") as stream:
            stream.truncate(8 * 1024 * 1024 + 1)
    elif fault == "dotfile":
        image = case.alice.work / ".private.png"
        image.write_bytes(_png())
    result = case.call({"profile_id": "aux", "prompt": "test", "image": str(image),
                        "timeout_s": 2})
    assert result["ok"] is False and result["error"].startswith("media_image_"), result
    assert not case.seen


@pytest.mark.parametrize("suffix", [".png", ".jpg"])
def test_image_replacement_after_validation_never_reopens_the_path(case, suffix):
    image = case.alice.work / ("frame" + suffix)
    image.write_bytes(_png())
    outside = case.root / "outside-secret.png"
    outside.write_text("must never be sent")
    original = case.cli._validate_png

    def replace_after_validation(raw):
        original(raw)
        image.unlink()
        image.symlink_to(outside)

    case.patch.setattr(case.cli, "_validate_png", replace_after_validation)
    case.patch.chdir(case.alice.work)
    for name, value in case.alice.env.items():
        case.patch.setenv(name, value)
    result = case.cli.run_model_tool("model_chat", {
        "profile_id": "aux", "prompt": "test", "image": image.name})
    assert result["ok"] is True, result
    data_url = case.seen[0]["body"]["messages"][0]["content"][0]["image_url"]["url"]
    assert data_url.startswith("data:image/png;base64,")
    assert base64.b64decode(data_url.split(",", 1)[1]) == _png()
    assert result["input_image"] == {
        "sha256": hashlib.sha256(_png()).hexdigest(),
        "bytes": len(_png()), "mime_type": "image/png",
    }, "The receipt must use the same read as the payload, not reopen the replaced path"


def test_image_growth_past_limit_during_read_is_rejected(case):
    image = case.alice.work / "frame.png"
    image.write_bytes(_png())
    identity = image.stat()
    original = os.fstat
    grew = False

    def grow_after_stat(fd):
        nonlocal grew
        info = original(fd)
        if not grew and (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
            grew = True
            with image.open("ab") as stream:
                stream.truncate(8 * 1024 * 1024 + 1)
        return info

    def forbidden(raw):
        pytest.fail("readback size/identity checks must precede format validation")

    case.patch.setattr(os, "fstat", grow_after_stat)
    case.patch.setattr(case.cli, "_validate_png", forbidden)
    for name, value in case.alice.env.items():
        case.patch.setenv(name, value)
    result = case.cli.run_model_tool("model_chat", {
        "profile_id": "aux", "prompt": "test", "image": image.name})
    assert grew
    assert result["ok"] is False and result["error"] == "media_image_unavailable", result
    assert not case.seen


def test_image_workspace_replaced_between_check_and_open_is_not_read(case):
    (case.alice.work / "frame.png").write_bytes(_png())
    identity = case.alice.work.stat()
    for name, value in case.alice.env.items():
        case.patch.setenv(name, value)
    case.patch.setenv("MEYO_MODEL_WORKSPACE_ID", json.dumps([identity.st_dev, identity.st_ino]))
    original = case.runtime.model_tool_workspace

    def replace_after_check(environment, **kwargs):
        workspace = original(environment, **kwargs)
        if not kwargs.get("require_bound"):
            workspace.rename(case.root / "original-workspace")
            workspace.mkdir()
            (workspace / "frame.png").write_bytes(_png())
        return workspace

    case.patch.setattr(case.runtime, "model_tool_workspace", replace_after_check)
    result = case.cli.run_model_tool("model_chat", {
        "profile_id": "aux", "prompt": "test", "image": "frame.png"})
    assert result["ok"] is False and result["error"].startswith("media_image_"), result
    assert not case.seen


@pytest.mark.parametrize("folder", [".ssh", ".codex", "secrets"])
def test_image_secret_directories_are_not_read_even_with_a_png_suffix(case, folder):
    directory = case.alice.work / folder
    directory.mkdir()
    (directory / "private.png").write_bytes(_png())
    for name, value in case.alice.env.items():
        case.patch.setenv(name, value)

    def forbidden(*args, **kwargs):
        pytest.fail("private directory must be rejected before opening the image")

    case.patch.setattr(case.native, "_read_regular_at", forbidden)
    result = case.cli.run_model_tool("model_chat", {
        "profile_id": "aux", "prompt": "test", "image": folder + "/private.png"})
    assert result["ok"] is False and result["error"].startswith("media_image_"), result
    assert not case.seen


@pytest.mark.parametrize("bad", [None, True, -1, float("nan"), float("inf"), "1", {"debug": "fake-private"}])
def test_receipt_projection_never_invents_cost_or_leaks_metadata(case, bad):
    case.response.update(model={"key": "fake-alice-key"}, provider="x" * 2048, id="bad\nid")
    case.response["usage"] = {"total_tokens": 7, "cost": bad, "debug": "fake-private"}
    result = case.call()
    assert result["ok"] is True, result
    assert result["configured_model"] == "alice-router"
    assert result["reported_model"] is None and result["provider"] is None
    assert result["request_id"] == "header-request"
    assert result["cost"] is None and "cost" not in result["usage"]
    assert "fake-private" not in json.dumps(result)


def test_profile_http_redirect_does_not_forward_credentials(case):
    case.mode["redirect"] = True
    result = case.call()
    assert result["error"] == "media_http_error:307"
    assert len(case.seen) == 1 and case.seen[0]["path"] != "/trap"


@pytest.mark.parametrize("reason", ["cancel", "deadline", "expired"])
def test_profile_calls_keep_total_deadline_cancellation_and_cleanup(case, reason):
    case.mode["block"] = True
    env = dict(case.alice.env)
    if reason in ("deadline", "expired"):
        env["MEYO_TASK_DEADLINE"] = str(time.monotonic() + (0.5 if reason == "deadline" else -1))
    result = case.call(env=env, check=case.received.is_set if reason == "cancel" else None)
    assert result["error"] == ("media_call_cancelled" if reason == "cancel" else "media_call_timeout"), result
    if reason == "expired":
        assert not case.children and not case.seen
    else:
        assert case.seen
    _assert_clean(case)


def _context(case, ids):
    return {"user_id": "alice", "session_id": "fixture-session",
            "workdir": str(case.alice.work), "allowed_ids": ids,
            "task_facts": {}, "project_context": {}, "capability_policy": {"billable": True}}


def test_native_grants_only_the_exact_existing_ids_and_reuses_same_entry(case):
    ids = ["runnable:model_catalog"]
    session = case.native.CapabilitySession(_context(case, ids), environment=case.alice.env)
    assert [a["id"] for a in session.list_capabilities({})["actions"]] == ids
    assert session.call_capability({"tool_id": ids[0], "params": {"profile_id": "aux"}})["ok"]
    with pytest.raises(ValueError):
        session.call_capability({"tool_id": "runnable:model_chat",
                                 "params": {"profile_id": "aux", "prompt": "test"}})
    with pytest.raises(ValueError):
        case.native.validate_ids(["runnable:arbitrary_shell"])
    assert not case.seen


def test_native_model_grant_still_obeys_existing_billable_policy(case):
    context = _context(case, ["runnable:model_chat"])
    context["capability_policy"] = {"billable": False}
    session = case.native.CapabilitySession(context, environment=case.alice.env)
    result = session.call_capability({"tool_id": "runnable:model_chat",
                                     "params": {"profile_id": "aux", "prompt": "test"}})
    assert result["error"] == "CAPABILITY_DENIED:billable" and not case.seen


def test_native_spec_never_exports_key_and_profiles_do_not_grant_tools(case):
    context = _context(case, ["runnable:model_catalog", "runnable:model_chat"])
    ctx = SimpleNamespace(**context, execution_depth="professional")
    env = {**case.alice.env, "MEYO_NATIVE_CAPABILITY_IDS": json.dumps(context["allowed_ids"])}
    spec = case.native.native_stdio_spec(ctx, env, SERVER)
    assert spec["env"]["MEYO_PERSONAL_STATE_DIR"] == str(case.alice.root)
    assert "fake-alice-key" not in json.dumps(spec)
    assert "MEYO_PERSONAL_PROVIDER_KEY" not in spec["env"]
    assert case.native.native_stdio_spec(ctx, {**env, "MEYO_NATIVE_CAPABILITY_IDS": "[]"}, SERVER) is None
    with pytest.raises(ValueError):
        case.native.native_stdio_spec(
            SimpleNamespace(**{**context, "user_id": "bob"}, execution_depth="professional"), env, SERVER)


def test_native_rejects_workspace_replaced_since_binding(case):
    session = case.native.CapabilitySession(_context(case, ["runnable:model_chat"]),
                                            environment=case.alice.env)
    case.alice.work.rename(case.root / "original-work")
    case.alice.work.mkdir()
    result = session.call_capability({"tool_id": "runnable:model_chat",
                                     "params": {"profile_id": "aux", "prompt": "test"}})
    assert result["ok"] is False and not case.seen


def test_legacy_directory_call_without_profile_remains_available(case):
    result = case.call({"prompt": "legacy", "domain": "programming", "model": "company-model"})
    assert result["ok"] is True, result
    assert case.seen[0]["company_key"] and case.seen[0]["path"] == "/company/v1/chat/completions"


def test_real_native_stdio_cancels_profile_child_and_reuses_the_host(case):
    context = _context(case, ["runnable:model_catalog", "runnable:model_chat"])
    env = {**case.alice.env, "MEYO_NATIVE_CAPABILITY_IDS": json.dumps(context["allowed_ids"])}
    spec = case.native.native_stdio_spec(
        SimpleNamespace(**context, execution_depth="professional"), env, SERVER)
    process = subprocess.Popen(
        [spec["command"], *spec["args"]], env=spec["env"], cwd=case.alice.work,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    buffer = b""

    def send(request):
        process.stdin.write(json.dumps({"jsonrpc": "2.0", **request}) + "\n")
        process.stdin.flush()

    def receive(request_id):
        nonlocal buffer
        deadline = time.monotonic() + 6
        while True:
            if b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                reply = json.loads(line)
                if reply.get("id") == request_id:
                    return reply
                continue
            remaining = deadline - time.monotonic()
            assert remaining > 0 and select.select([process.stdout], [], [], remaining)[0], "native stdio did not respond"
            raw = os.read(process.stdout.fileno(), 65536)
            assert raw, "native stdio closed"
            buffer += raw

    def call(request_id, tool_id, params):
        send({"id": request_id, "method": "tools/call", "params": {
            "name": "call_capability", "arguments": {
                "tool_id": tool_id, "params_json": json.dumps(params)}}})

    try:
        send({"id": "init", "method": "initialize", "params": {}})
        assert "result" in receive("init")
        case.mode["block"] = True
        call("chat", "runnable:model_chat", {"profile_id": "aux", "prompt": "test"})
        assert case.received.wait(4)
        send({"method": "notifications/cancelled", "params": {"requestId": "chat"}})
        result = receive("chat")
        assert "media_call_cancelled" in json.dumps(result), result
        # The reusable MCP host is still alive; only its model child must be
        # gone at the cancellation receipt. Host registration ends at shutdown.
        assert {int(path.name.split(".", 1)[0]) for path in
                Path(env["LEGION_PROC_REGISTRY_DIR"]).glob("*.json")} == {process.pid}
        call("catalog", "runnable:model_catalog", {"profile_id": "aux"})
        assert "alice-router" in json.dumps(receive("catalog"))
        assert "fake-alice-key" not in json.dumps(result)
        process.stdin.close()
        assert process.wait(timeout=4) == 0
        assert not list(Path(env["LEGION_PROC_REGISTRY_DIR"]).glob("*.json"))
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()
