"""Decisions protocol through existing tools: private fakes and loopback HTTP only."""
from __future__ import annotations

import base64
import copy
import json
import os
from pathlib import Path
import select
import signal
import struct
import subprocess
import sys
import time
from types import SimpleNamespace
import zlib

import pytest

from test_shared_model_profiles_cpu import case, _assert_clean, _context, SERVICE, SERVER


QUESTIONS = {
    "yes": {"type": "noul", "instructions": "Does the supplied statement hold?"},
    "pick": {"type": "choice", "instructions": ["Choose a label."],
             "criteria": {"a": None, "b": {"description": "another label"}}},
    "level": {"type": "score", "instructions": {"task": "Rate the supplied state."},
              "criteria": ["low", ["high"]]},
    "area": {"type": "box", "instructions": "Locate the supplied target."},
}
ANSWERS = {
    "yes": {"type": "noul", "noul": 0.96},
    "pick": {"type": "choice", "choice": "a", "confidence": 0.75,
             "probabilities": {"a": 0.8, "b": 0.2}},
    "level": {"type": "score", "score": 0.8, "legend": {"0": "low", "1": ["high"]}},
    "area": {"type": "box", "box": {"top": 0.1, "bottom": 0.8, "left": 0.2, "right": 0.9}},
}
# A complete 280x280 grayscale JPEG; tests need no image package or GPU.
JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAEYARgBAREA/8QAFQABAQAAAAAA"
    "AAAAAAAAAAAAAAf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAA/AJ+" + "A" * 408 + "//9k="
)


def _png(width=280, height=280, *, animated=False, color=80):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + (chunk(b"acTL", struct.pack(">II", 1, 0)) if animated else b"")
            + chunk(b"IDAT", zlib.compress((b"\x00" + bytes([color]) * width * 3) * height))
            + chunk(b"IEND", b""))


@pytest.fixture
def decision(case):
    case.runtime.save_model_profile(
        case.alice.root, "decisions", "fixture-decider", case.base + "/api/alpha",
        case.root / "alice-key", input_modalities=["text", "image"])
    case.params = {"profile_id": "decisions", "state": "fixture state",
                   "questions": copy.deepcopy(QUESTIONS), "timeout_s": 3}
    case.response.clear()
    case.response.update({
        "model": "fixture-reported", "answers": copy.deepcopy(ANSWERS),
        "usage": {"input_tokens": 19, "output_tokens": 4, "cost": 0.007, "debug": "fake-alice-key"},
        "id": "req-decision", "provider": "Fixture Provider", "debug": "fake-alice-key",
        "_configured_model": "forged-model",
    })
    return case


def _invoke(d, params=None, *, entry="shared", environment=None):
    params = copy.deepcopy(d.params if params is None else params)
    env = dict(d.alice.env if environment is None else environment)
    if entry == "shared":
        return d.call(params, name="model_decisions", env=env)
    if entry == "native":
        context = _context(d, ["runnable:model_decisions"])
        context["capability_policy"] = json.loads(env.get("MEYO_CAPABILITY_POLICY_JSON", "{}"))
        session = d.native.CapabilitySession(context, environment=env)
        return session.call_capability({"tool_id": "runnable:model_decisions", "params": params})
    proc = subprocess.Popen(
        [sys.executable, "-B", str(SERVICE / "media_model_cli.py"), "tool", "model_decisions"],
        cwd=d.alice.work, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = proc.communicate(json.dumps(params), timeout=6)
        assert stdout.strip(), (proc.returncode, stderr)
        result = json.loads(stdout)
        assert proc.returncode == (0 if result["ok"] else 1)
        assert stderr == ""
        return result
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=3)


@pytest.mark.parametrize("entry", ["shared", "native", "cli"])
def test_existing_entries_send_atomic_private_decisions_and_preserve_receipts(decision, entry):
    d = decision
    driver = (d.alice.root / "secrets/model.json").read_bytes()
    profiles = (d.alice.root / "secrets/model-profiles.json").read_bytes()
    result = _invoke(d, entry=entry)
    assert result["ok"] is True, result
    assert result["answers"] == ANSWERS
    assert result["configured_model"] == "fixture-decider"
    assert result["reported_model"] == "fixture-reported"
    assert result["provider"] == "Fixture Provider" and result["request_id"] == "req-decision"
    assert result["usage"] == {"input_tokens": 19, "output_tokens": 4, "cost": 0.007}
    assert result["cost"] == 0.007 and result["cost_source"] == "usage.cost"
    assert result["usage_scope"] == "single_model_tool_request"
    assert d.seen == [{"path": "/api/alpha/decisions", "alice_key": True,
                       "bob_key": False, "company_key": False, "body": {
                           "model": "fixture-decider", "state": "fixture state", "questions": QUESTIONS}}]
    assert (d.alice.root / "secrets/model.json").read_bytes() == driver
    assert (d.alice.root / "secrets/model-profiles.json").read_bytes() == profiles
    assert "fake-alice-key" not in json.dumps(result) and "debug" not in json.dumps(result)
    _assert_clean(d)


@pytest.mark.parametrize("profile,error", [
    (None, "media_profile_id_required"), ("missing", "media_profile_unknown"),
    ("aux", "media_decisions_endpoint_invalid"),
])
def test_profile_is_required_and_router_endpoint_never_converts_or_borrows_company(decision, profile, error):
    params = copy.deepcopy(decision.params)
    params.pop("profile_id")
    if profile is not None:
        params["profile_id"] = profile
    result = _invoke(decision, params)
    assert result["error"] == error and result["ok"] is False
    assert not decision.seen
    _assert_clean(decision)


@pytest.mark.parametrize("binding,error", [
    ("missing", "media_profile_binding_required"),
    ("owner", "media_profile_owner_mismatch"),
    ("private_workspace", "media_profile_workspace_invalid"),
    ("pin", "media_profile_workspace_invalid"),
])
def test_profile_binding_fails_closed_before_any_request(decision, binding, error):
    d = decision
    env = dict(d.alice.env)
    if binding == "missing":
        env.pop("MEYO_USER_ID")
    elif binding == "owner":
        env["MEYO_USER_ID"] = "bob"
    elif binding == "private_workspace":
        env["MEYO_WORKDIR"] = str(d.alice.root / "secrets")
    else:
        env["MEYO_MODEL_WORKSPACE_ID"] = "[0,0]"
    result = _invoke(d, environment=env)
    assert result["ok"] is False and result["error"] == error
    assert not d.seen


@pytest.mark.parametrize("field", ["model", "base_url", "api_key", "env", "trace", "user", "provider", "images"])
def test_override_or_unimplemented_fields_never_reach_lifecycle_logging(decision, field):
    d, events = decision, []
    with d.provider._runtime_scope(d.alice.env, None, inherit_process_context=False):
        result = d.tools.run_tool("runnable:model_decisions", {
            **d.params, field: "untrusted-never-log-this"}, event_cb=events.append)
    assert result["ok"] is False
    assert events == [] and not d.seen and not d.children
    assert "untrusted-never-log-this" not in json.dumps(result)


@pytest.mark.parametrize("state,question", [
    (None, QUESTIONS["yes"]), (True, QUESTIONS["yes"]), (42, QUESTIONS["yes"]),
    ("text", {"type": "chat", "instructions": "wrong protocol"}),
    ("text", {"type": "noul", "instructions": "x", "criteria": {"true": "yes"}}),
    ("text", {"type": "noul", "instructions": "x", "criteria": {"true": None, "false": "no"}}),
    ("text", {"type": "choice", "instructions": "x", "criteria": []}),
    ("text", {"type": "score", "instructions": "x", "criteria": []}),
    ("text", {"type": "score", "instructions": "x", "criteria": [None]}),
    ("text", {"type": "box", "instructions": "x", "criteria": {}}),
    ({"x": float("nan")}, QUESTIONS["yes"]),
])
def test_typed_request_validation_happens_before_profile_or_http(decision, state, question):
    result = _invoke(decision, {**decision.params, "state": state, "questions": {"q": question}})
    assert result["ok"] is False and result["error"] == "media_decisions_parameters_invalid"
    assert not decision.seen and not decision.children


@pytest.mark.parametrize("position", ["state", "instructions", "choice", "noul", "score"])
def test_nested_raw_media_cannot_bypass_workspace_authorization(decision, position):
    d = decision
    part = {"nested": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}
    params = copy.deepcopy(d.params)
    if position == "state":
        params["state"] = part
    elif position == "instructions":
        params["questions"]["yes"]["instructions"] = part
    elif position == "choice":
        params["questions"]["pick"]["criteria"]["a"] = part
    elif position == "noul":
        params["questions"]["yes"]["criteria"] = {"true": part, "false": "no"}
    else:
        params["questions"]["level"]["criteria"] = [part]
    result = _invoke(d, params)
    assert result["error"] == "media_decisions_raw_media_forbidden"
    assert not d.seen and not d.children


@pytest.mark.parametrize("entry", ["shared", "native", "cli"])
@pytest.mark.parametrize("permission", ["billable", "read"])
def test_explicit_permissions_survive_every_entry(decision, entry, permission):
    d = decision
    image = d.alice.work / "input.png"
    image.write_bytes(_png())
    env = {**d.alice.env, "MEYO_CAPABILITY_POLICY_JSON": json.dumps({permission: False})}
    result = _invoke(d, {**d.params, "image": str(image)}, entry=entry, environment=env)
    assert result["ok"] is False
    assert result["error"] == ("CAPABILITY_DENIED:billable" if permission == "billable"
                               else "media_image_read_denied")
    assert not d.seen
    _assert_clean(d)


def test_read_denial_does_not_disable_text_decisions(decision):
    d = decision
    result = _invoke(d, environment={**d.alice.env, "MEYO_CAPABILITY_POLICY_JSON": '{"read":false}'})
    assert result["ok"] is True and len(d.seen) == 1


@pytest.mark.parametrize("directory,entry,extension", [
    ("ordinary", "shared", "png"), (".playwright-mcp", "shared", "png"),
    (".local-run", "native", "png"), ("ordinary", "cli", "jpeg"),
])
def test_real_image_bytes_reach_state_not_a_path_or_assets(decision, directory, entry, extension):
    d = decision
    folder = d.alice.work / directory
    folder.mkdir()
    content = JPEG if extension == "jpeg" else _png()
    image = folder / ("input." + extension)
    image.write_bytes(content)
    result = _invoke(d, {**d.params, "image": str(image)}, entry=entry)
    assert result["ok"] is True, result
    body = d.seen[0]["body"]
    assert set(body) == {"state", "questions", "model"}
    assert body["state"][0] == "fixture state"
    part = body["state"][1]
    assert part["type"] == "image_url"
    prefix, encoded = part["image_url"]["url"].split(",", 1)
    assert prefix == "data:image/" + extension + ";base64"
    assert base64.b64decode(encoded, validate=True) == content
    assert str(image) not in json.dumps(body)
    _assert_clean(d)


@pytest.mark.parametrize("width,height,animated", [(255, 308, False), (256, 256, False),
                                                  (1569, 280, False), (280, 280, True)])
def test_jev_image_size_and_animation_errors_do_not_send(decision, width, height, animated):
    image = decision.alice.work / "input.png"
    image.write_bytes(_png(width, height, animated=animated))
    result = _invoke(decision, {**decision.params, "image": str(image)})
    assert result["ok"] is False and result["error"].startswith("media_decisions_image_")
    assert not decision.seen


@pytest.mark.parametrize("scan_size", [1, 2, 65535])
def test_invalid_jpeg_scan_header_does_not_send(decision, scan_size):
    content = bytearray(JPEG)
    offset = content.index(b"\xff\xda") + 2
    content[offset:offset + 2] = struct.pack(">H", scan_size)
    image = decision.alice.work / "input.jpeg"
    image.write_bytes(content)
    result = _invoke(decision, {**decision.params, "image": str(image)})
    assert result["ok"] is False and result["error"] == "media_decisions_image_invalid"
    assert not decision.seen


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "outside", "secrets"])
def test_decisions_keep_existing_file_boundaries(decision, kind):
    d = decision
    outside = d.root / "outside.png"
    outside.write_bytes(_png())
    image = d.alice.work / "input.png"
    if kind == "symlink":
        image.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, image)
    elif kind == "outside":
        image = outside
    else:
        (d.alice.work / "secrets").mkdir()
        image = d.alice.work / "secrets/input.png"
        image.write_bytes(_png())
    result = _invoke(d, {**d.params, "image": str(image)})
    assert result["ok"] is False and not d.seen


def test_request_uses_original_profile_snapshot_and_checked_image_bytes(decision):
    d = decision
    image = d.alice.work / "input.png"
    original = _png()
    image.write_bytes(original)
    resolve, read = d.runtime.resolve_model_profile, d.cli._checked_image_bytes
    calls = []
    def resolve_then_replace(*args, **kwargs):
        snapshot = resolve(*args, **kwargs)
        calls.append("profile")
        # Simulate an external replacement in this isolated fake store, not an
        # overwrite through the intentionally append-only public save API.
        profiles = d.runtime._model_profiles(d.alice.root)
        profiles["decisions"] = {**profiles["decisions"], "model": "replacement-model",
                                "base_url": d.base + "/changed/api/alpha",
                                "api_key": "fake-replacement-key"}
        d.runtime._write_model_profiles(d.alice.root, profiles)
        return snapshot
    def read_then_replace(path):
        value = read(path)
        calls.append("image")
        image.write_bytes(_png(color=170))
        return value
    d.patch.setattr(d.runtime, "resolve_model_profile", resolve_then_replace)
    d.patch.setattr(d.cli, "_checked_image_bytes", read_then_replace)
    for key, value in d.alice.env.items():
        d.patch.setenv(key, value)
    result = d.cli.run_model_tool("model_decisions", {**d.params, "image": str(image)})
    assert result["ok"] is True, result
    assert calls == ["profile", "image"]
    assert d.seen[0]["path"] == "/api/alpha/decisions"
    assert d.seen[0]["body"]["model"] == "fixture-decider"
    assert len(d.seen) == 1 and d.seen[0]["alice_key"]
    assert not d.seen[0]["bob_key"] and not d.seen[0]["company_key"]
    encoded = d.seen[0]["body"]["state"][1]["image_url"]["url"].split(",", 1)[1]
    assert base64.b64decode(encoded) == original


@pytest.mark.parametrize("bad", [
    {}, {"yes": {"type": "noul", "noul": True}},
    {**ANSWERS, "pick": {"type": "choice", "choice": "not-a-choice"}},
    {**ANSWERS, "level": {"type": "score", "score": float("nan")}},
    {**ANSWERS, "area": {"type": "box", "box": [0.1, 0.2, 0.3, 0.4]}},
    {**ANSWERS, "yes": {"type": "choice", "choice": "a"}},
    {**ANSWERS, "pick": {"type": "choice", "choice": "a", "confidence": float("inf")}},
    {**ANSWERS, "level": {"type": "score", "score": 1, "legend": {"0": "fake-alice-key"}}},
])
def test_malformed_answers_fail_without_losing_actual_usage(decision, bad):
    d = decision
    d.response["answers"] = copy.deepcopy(bad)
    result = _invoke(d)
    assert result["ok"] is False and result["error"] == "media_decisions_response_invalid"
    assert "answers" not in result
    assert result["configured_model"] == "fixture-decider"
    assert result["reported_model"] == "fixture-reported" and result["cost"] == 0.007
    assert result["usage"]["input_tokens"] == 19
    assert "fake-alice-key" not in json.dumps(result)


@pytest.mark.parametrize("field", ["model", "usage"])
def test_required_reply_identity_and_usage_cannot_be_fabricated(decision, field):
    decision.response.pop(field)
    result = _invoke(decision)
    assert result["ok"] is False and result["error"] == "media_decisions_response_invalid"


def test_cost_and_optional_provider_identity_stay_unknown_if_not_reported(decision):
    d = decision
    d.response["usage"] = {"input_tokens": 0, "output_tokens": 0, "cost": False}
    d.response.pop("provider")
    d.response.pop("id")
    result = _invoke(d)
    assert result["ok"] is True
    assert result["cost"] is None and result["cost_source"] is None
    assert result["provider"] is None and result["request_id"] == "header-request"
    assert result["usage"] == {"input_tokens": 0, "output_tokens": 0}


@pytest.mark.parametrize("status,redirect", [(403, False), (200, True)])
def test_http_errors_and_redirects_never_echo_or_forward_keys(decision, status, redirect):
    d = decision
    d.mode.update(status=status, redirect=redirect)
    d.response["error"] = "fake-alice-key in an untrusted error body"
    result = _invoke(d)
    assert result["error"] == f"media_http_error:{307 if redirect else status}"
    assert len(d.seen) == 1 and "fake-alice-key" not in json.dumps(result)


@pytest.mark.parametrize("reason", ["cancel", "deadline", "expired"])
def test_decisions_use_existing_total_deadline_cancel_and_process_cleanup(decision, reason):
    d = decision
    d.mode["block"] = True
    env = dict(d.alice.env)
    if reason != "cancel":
        env["MEYO_TASK_DEADLINE"] = str(time.monotonic() + (0.7 if reason == "deadline" else -1))
    result = d.call(d.params, name="model_decisions", env=env,
                    check=d.received.is_set if reason == "cancel" else None)
    assert result["error"] == ("media_call_cancelled" if reason == "cancel" else "media_call_timeout")
    if reason == "expired":
        assert not d.seen and not d.children
    else:
        assert len(d.seen) == 1
    _assert_clean(d)


def test_native_requires_its_own_decisions_grant_and_matching_business_owner(decision):
    d = decision
    session = d.native.CapabilitySession(_context(d, ["runnable:model_chat"]), environment=d.alice.env)
    with pytest.raises(ValueError):
        session.call_capability({"tool_id": "runnable:model_decisions", "params": d.params})
    context = _context(d, ["runnable:model_decisions"])
    with pytest.raises(ValueError):
        d.native.CapabilitySession({**context, "user_id": "bob"}, environment=d.alice.env)
    assert not d.seen


def test_real_native_stdio_cancels_only_decisions_child_and_keeps_host(decision):
    d = decision
    context = _context(d, ["runnable:model_catalog", "runnable:model_decisions"])
    env = {**d.alice.env, "MEYO_NATIVE_CAPABILITY_IDS": json.dumps(context["allowed_ids"])}
    spec = d.native.native_stdio_spec(
        SimpleNamespace(**context, execution_depth="professional"), env, SERVER)
    assert "fake-alice-key" not in json.dumps(spec)
    proc = subprocess.Popen([spec["command"], *spec["args"]], env=spec["env"], cwd=d.alice.work,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    buffer = b""
    def send(request):
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", **request}) + "\n")
        proc.stdin.flush()
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
            assert remaining > 0 and select.select([proc.stdout], [], [], remaining)[0]
            raw = os.read(proc.stdout.fileno(), 65536)
            assert raw
            buffer += raw
    def call(request_id, tool_id, params):
        send({"id": request_id, "method": "tools/call", "params": {
            "name": "call_capability", "arguments": {
                "tool_id": tool_id, "params_json": json.dumps(params)}}})
    try:
        send({"id": "init", "method": "initialize", "params": {}})
        assert "result" in receive("init")
        d.mode["block"] = True
        call("decisions", "runnable:model_decisions", d.params)
        assert d.received.wait(4)
        send({"method": "notifications/cancelled", "params": {"requestId": "decisions"}})
        reply = receive("decisions")
        assert "media_call_cancelled" in json.dumps(reply), reply
        assert {int(path.name.split(".", 1)[0]) for path in
                Path(env["LEGION_PROC_REGISTRY_DIR"]).glob("*.json")} == {proc.pid}
        call("catalog", "runnable:model_catalog", {"profile_id": "decisions"})
        assert "fixture-decider" in json.dumps(receive("catalog"))
        assert "fake-alice-key" not in json.dumps(reply)
        proc.stdin.close()
        assert proc.wait(timeout=4) == 0
        assert not list(Path(env["LEGION_PROC_REGISTRY_DIR"]).glob("*.json"))
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=2)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream and not stream.closed:
                stream.close()
