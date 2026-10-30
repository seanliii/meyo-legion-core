"""Lossless vision results through the bundled CLI; no real model/network calls."""
import importlib
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest


SERVICE = Path(__file__).resolve().parents[1]
ANSWER = "开头不可丢\n" + "完整视觉观察。" * 1000 + "\n结尾也保留"


@pytest.fixture(autouse=True)
def case(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith(("MEYO_", "LEGION_", "AIGC_", "OPENAI_", "ANTHROPIC_", "CODEX_")):
            monkeypatch.delenv(name)
    for name, leaf in (
        ("HOME", "home"), ("CODEX_HOME", "codex"), ("TMPDIR", "tmp"),
        ("MEYO_LEGION_DIR", "data"), ("LEGION_HOME_DIR", "legion"),
    ):
        directory = tmp_path / leaf
        directory.mkdir()
        monkeypatch.setenv(name, str(directory))
    monkeypatch.setenv("MEYO_MODEL_CATALOG_PATH", str(tmp_path / "no-real-catalog.json"))
    monkeypatch.syspath_prepend(str(SERVICE))
    media = importlib.import_module("media_tools")
    image = tmp_path / "fixture.png"
    image.write_bytes(b"local fixture; never transmitted")
    reply = {
        "ok": True, "answer": ANSWER, "configured_model": "fixture-selected",
        "reported_model": "fixture-reported",
        "usage": {"prompt_tokens": 11, "completion_tokens": 23, "total_tokens": 34,
                  "completion_tokens_details": {"reasoning_tokens": 7}},
        "usage_scope": "single_model_tool_request", "finish_reason": "stop", "max_tokens": 4000,
        "input_image": {"sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                        "bytes": image.stat().st_size, "mime_type": "image/png"},
    }
    scene = SimpleNamespace(media=media, image=image, reply=reply, calls=[], root=tmp_path,
                            patch=monkeypatch, real_run=subprocess.run)

    def forbidden(*a, **kw):
        raise AssertionError("no network in vision result regression")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    def cli_boundary(command, **kwargs):
        scene.calls.append((list(command), {key: kwargs[key] for key in
                                            ("timeout", "capture_output", "text") if key in kwargs}))
        return subprocess.CompletedProcess(
            command, 0, json.dumps(scene.reply, ensure_ascii=False) if "--json" in command else ANSWER + "\n", "")

    monkeypatch.setattr(media.subprocess, "run", cli_boundary)
    return scene


def test_bundled_cli_retains_full_answer_and_actual_filtered_metadata(case):
    # Extra raw response fields must never be copied into the tool result.
    case.reply.update(debug="not-public", reasoning_content="not-public")
    expected_image = dict(case.reply["input_image"])
    case.reply["input_image"].update(path="not-public", data_url="not-public")
    result = case.media.see_image(str(case.image), "fixture question", timeout_s=5)
    command, kwargs = case.calls[0]
    assert len(case.calls) == 1 and "--json" in command
    assert command[:3] == [sys.executable, str(SERVICE / "media_model_cli.py"), "chat"]
    assert command[command.index("--timeout") + 1] == "5"
    assert kwargs["timeout"] == 20, "do not change the existing timeout/executor contract"
    assert result["answer"] == ANSWER and len(result["answer"]) > 6000
    assert result["model"] == "vision_understanding", "preserve the legacy semantic field"
    for field in ("configured_model", "reported_model", "usage", "usage_scope", "finish_reason", "max_tokens"):
        assert result[field] == case.reply[field]
    assert result["metadata_source"] == "bundled_cli_json"
    assert result["input_image"] == expected_image
    assert "not-public" not in json.dumps(result)


@pytest.mark.parametrize("explicit", [False, True])
def test_explicit_reference_to_same_bundled_cli_is_known(case, explicit):
    if explicit:
        case.patch.setenv("MEYO_MEDIA_MODEL_CLI", str(SERVICE / "media_model_cli.py"))
    result = case.media.see_image(str(case.image))
    assert result["ok"] and "--json" in case.calls[0][0]


def test_unknown_legacy_cli_gets_one_plain_text_call_and_unknown_metadata(case):
    # Same basename is not proof that a replacement implements the new protocol.
    legacy = case.root / "media_model_cli.py"
    legacy.write_text("# unknown legacy entry")
    case.patch.setenv("MEYO_MEDIA_MODEL_CLI", str(legacy))
    result = case.media.see_image(str(case.image))
    assert len(case.calls) == 1 and "--json" not in case.calls[0][0]
    assert result["answer"] == ANSWER, "legacy text must not be silently tail-truncated either"
    assert result["model"] == "vision_understanding"
    assert result["metadata_source"] == "legacy_cli_text"
    for field in ("configured_model", "reported_model", "usage", "usage_scope", "finish_reason", "input_image"):
        assert result[field] is None


def test_legacy_json_looking_text_does_not_forge_actual_model(case):
    legacy = case.root / "legacy.py"
    legacy.write_text("# unknown legacy entry")
    case.patch.setenv("MEYO_MEDIA_MODEL_CLI", str(legacy))
    body = '{"answer":"text","configured_model":"unverified-claim","usage":{"total_tokens":8}}'
    calls = []

    def boundary(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, body + "\n", "")

    case.patch.setattr(case.media.subprocess, "run", boundary)
    result = case.media.see_image(str(case.image))
    assert len(calls) == 1 and "--json" not in calls[0]
    assert result["answer"] == body
    assert result["configured_model"] is None and result["usage"] is None


@pytest.mark.parametrize("stdout", ["not JSON", "[]", '{"ok":true}', '{"ok":true,"answer":42}'])
def test_bad_bundled_json_is_not_success_or_a_second_paid_call(case, stdout):
    calls = []

    def boundary(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout, "")

    case.patch.setattr(case.media.subprocess, "run", boundary)
    result = case.media.see_image(str(case.image))
    assert len(calls) == 1
    assert result["ok"] is False
    assert result["error"] == "media_tool_invalid_response"
    assert stdout not in result["error"]


def test_missing_response_metadata_stays_unknown_not_inferred_from_catalog(case):
    case.reply = {"ok": True, "answer": "only a body"}
    result = case.media.see_image(str(case.image))
    assert result["answer"] == "only a body"
    for field in ("configured_model", "reported_model", "usage", "finish_reason", "input_image"):
        assert result[field] is None


@pytest.mark.parametrize("bad", [
    None, [], "not-public", {},
    {"sha256": "not-public", "bytes": 3, "mime_type": "image/png"},
    {"sha256": "a" * 64, "bytes": True, "mime_type": "image/png"},
    {"sha256": "a" * 64, "bytes": 0, "mime_type": "image/png"},
    {"sha256": "a" * 64, "bytes": -1, "mime_type": "image/png"},
    {"sha256": "a" * 64, "bytes": "3", "mime_type": "image/png"},
    {"sha256": "a" * 64, "bytes": 3, "mime_type": []},
    {"sha256": "a" * 64, "bytes": 3, "mime_type": "text/plain"},
    {"sha256": "a" * 64, "mime_type": "image/png"},
])
def test_invalid_image_receipt_stays_unknown_without_losing_answer(case, bad):
    case.reply["input_image"] = bad
    result = case.media.see_image(str(case.image))
    assert result["ok"] and result["answer"] == ANSWER
    assert result["input_image"] is None
    assert len(case.calls) == 1, "Receipt validation cannot trigger another model call"
    assert "not-public" not in json.dumps(result)


def test_bundled_answer_preserves_leading_and_trailing_content(case):
    case.reply["answer"] = "  opening\n" + "x" * 6100 + "\nending  "
    result = case.media.see_image(str(case.image))
    assert result["answer"] == case.reply["answer"]


@pytest.mark.parametrize("error", ["media_output_limit", "media_finish_reason_invalid", "media_http_error:429"])
def test_failed_child_remains_failure_without_fallback_or_stdout_promotion(case, error):
    calls = []

    def boundary(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, json.dumps(case.reply),
                                           "unsafe diagnostic must not escape\n" + error + "\n")

    case.patch.setattr(case.media.subprocess, "run", boundary)
    result = case.media.see_image(str(case.image))
    assert len(calls) == 1
    assert result == {"ok": False, "error": error}


def test_real_bundled_cli_json_protocol_with_fake_request_boundary(case):
    # A real Python child runs the actual bundled CLI parser/response filter.
    # Only its model request is replaced; network and further spawns fail closed.
    script = """
import json, sys
def no_external(event, args):
    if event in ('socket.connect', 'socket.getaddrinfo', 'subprocess.Popen'):
        raise AssertionError('offline child only')
sys.addaudithook(no_external)
sys.path.insert(0, sys.argv.pop(1))
import media_model_cli as cli
def request(direction, endpoint, payload, timeout_s, **kwargs):
    assert direction == 'vision_understanding' and endpoint == '/chat/completions'
    assert payload['max_tokens'] == 4000
    return {'_configured_model': 'fixture-selected', 'model': 'fixture-reported',
            'usage': {'total_tokens': 34, 'debug': 'not-public'},
            'choices': [{'finish_reason': 'stop', 'message': {
                'content': 'begin-' + 'x' * 6500 + '-end', 'reasoning_content': 'not-public'}}]}
cli._request = request
sys.argv = sys.argv[1:]
sys.exit(cli.main())
"""
    calls = []

    def child(command, **kwargs):
        calls.append(command)
        return case.real_run([sys.executable, "-c", script, str(SERVICE), *command[1:]], **kwargs)

    case.patch.setattr(case.media.subprocess, "run", child)
    result = case.media.see_image(str(case.image), "fixture question", timeout_s=5)
    assert len(calls) == 1
    assert result["ok"] and result["answer"] == "begin-" + "x" * 6500 + "-end"
    assert result["configured_model"] == "fixture-selected"
    assert result["reported_model"] == "fixture-reported"
    assert result["usage"] == {"total_tokens": 34} and result["finish_reason"] == "stop"
    assert result["input_image"] == {
        "sha256": hashlib.sha256(case.image.read_bytes()).hexdigest(),
        "bytes": case.image.stat().st_size, "mime_type": "image/png",
    }
    assert "not-public" not in json.dumps(result)


def test_real_unknown_legacy_child_needs_no_new_flags_or_second_invocation(case):
    legacy = case.root / "legacy_cli.py"
    invocations = case.root / "legacy-invocations.jsonl"
    legacy.write_text(
        "import json, sys\n"
        f"with open({str(invocations)!r}, 'a') as stream:\n"
        "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "assert '--json' not in sys.argv, 'unknown option'\n"
        "print('legacy-begin-' + 'x' * 6500 + '-legacy-end')\n")
    case.patch.setenv("MEYO_MEDIA_MODEL_CLI", str(legacy))
    case.patch.setattr(case.media.subprocess, "run", case.real_run)
    result = case.media.see_image(str(case.image), "fixture question", timeout_s=5)
    calls = [json.loads(line) for line in invocations.read_text().splitlines()]
    assert len(calls) == 1 and calls[0][0] == "chat"
    assert result["answer"] == "legacy-begin-" + "x" * 6500 + "-legacy-end"
    assert result["metadata_source"] == "legacy_cli_text"
    assert result["configured_model"] is None and result["usage"] is None


def run_media_entry(case, args, *, fail=False):
    """Real media_tools __main__ with only its child transport replaced."""
    fixture = case.root / "public-fixture-response.json"
    fixture.write_text(json.dumps(case.reply, ensure_ascii=False))
    script = """
import json, runpy, subprocess, sys
def no_external(event, args):
    if event in ('socket.connect', 'socket.getaddrinfo', 'subprocess.Popen'):
        raise AssertionError('offline CLI only')
sys.addaudithook(no_external)
service, response, failed = sys.argv[1:4]
sys.path.insert(0, service)
reply = json.load(open(response))
def child(command, **kwargs):
    return subprocess.CompletedProcess(command, 1 if failed == 'yes' else 0,
        json.dumps(reply, ensure_ascii=False) if '--json' in command else reply['answer'] + '\\n',
        'media_http_error:429\\n' if failed == 'yes' else '')
subprocess.run = child
sys.argv = [service + '/media_tools.py', *sys.argv[4:]]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
    return case.real_run([sys.executable, "-c", script, str(SERVICE), str(fixture),
                          "yes" if fail else "no", *args],
                         text=True, capture_output=True, timeout=5)


def test_default_media_cli_stdout_is_still_only_complete_answer(case):
    result = run_media_entry(case, ["see_image", str(case.image), "fixture question"])
    assert result.returncode == 0, result.stderr
    assert result.stdout == ANSWER + "\n"


def test_leading_json_switch_emits_full_result_for_quantification(case):
    result = run_media_entry(case, ["--json", "see_image", str(case.image), "fixture question"])
    assert result.returncode == 0, result.stderr
    row = json.loads(result.stdout)
    assert isinstance(row, dict) and row["ok"] is True
    assert row["answer"] == ANSWER and row["usage"] == case.reply["usage"]
    assert row["configured_model"] == "fixture-selected" and row["finish_reason"] == "stop"
    assert row["model"] == "vision_understanding"
    assert row["input_image"] == case.reply["input_image"]


@pytest.mark.parametrize("structured", [False, True])
def test_media_cli_failure_format_and_exit_status_are_preserved(case, structured):
    args = (["--json"] if structured else []) + ["see_image", str(case.image)]
    result = run_media_entry(case, args, fail=True)
    assert result.returncode == 1
    if structured:
        assert json.loads(result.stdout) == {"ok": False, "error": "media_http_error:429"}
    else:
        assert result.stdout == "ERROR: media_http_error:429\n"


@pytest.mark.parametrize("args", [
    ["--json", "see_image"], ["--json", "not-an-installed-tool"],
    ["--json", "see_image", "/fixture/nonexistent-image.png"],
])
def test_json_cli_does_not_return_catalog_success_for_unexecuted_call(case, args):
    result = run_media_entry(case, args)
    assert result.returncode == 1
    row = json.loads(result.stdout)
    assert isinstance(row, dict) and row["ok"] is False and row["error"]
