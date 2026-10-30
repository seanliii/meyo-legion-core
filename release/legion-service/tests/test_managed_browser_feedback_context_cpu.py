"""Actual HTTP serialization with real read-only feedback, no browser/network."""
from copy import deepcopy
import io
import json
import threading
from types import SimpleNamespace

import pytest

from test_native_feedback_context_cpu import (
    isolated, module, payload, stored_bytes, tool_result,
)


def request(view, native, *, method="tools/call", auth=True, session=True, origin=True,
            output=None):
    from managed_browser import _Handler

    calls = []
    lease = SimpleNamespace(
        closed=False, _authority="127.0.0.1:45001", base_url="http://127.0.0.1:45001",
        _token="fixture-token", _mcp_session="fixture-session", _feedback_context=view,
        _backend=SimpleNamespace(initialization_result={}),
        tools=[{"name": "browser_snapshot", "inputSchema": {"type": "object"}}],
        call=lambda params: calls.append(params) or native,
    )
    message = json.dumps({
        "jsonrpc": "2.0", "id": 7, "method": method,
        "params": {"name": "browser_snapshot", "arguments": {}},
    }).encode()
    handler = _Handler.__new__(_Handler)
    handler.server = SimpleNamespace(
        lease=lease, _rpc_requests=threading.BoundedSemaphore(1), _capacity_wait_s=0.01)
    handler.path = "/mcp"
    handler.headers = {
        "Host": lease._authority,
        "Origin": lease.base_url if origin else "http://untrusted.invalid",
        "Authorization": "Bearer " + (lease._token if auth else "wrong-token"),
        "Mcp-Session-Id": lease._mcp_session if session else "wrong-session",
        "Content-Type": "application/json", "Content-Length": str(len(message)),
    }
    handler.rfile = io.BytesIO(message)
    handler.wfile = output if output is not None else io.BytesIO()
    statuses, headers = [], []
    handler.send_response = statuses.append
    handler.send_header = lambda name, value: headers.append((name, value))
    handler.end_headers = lambda: None
    # Keep the real _reply: it must successfully write before display dedup.
    handler.do_POST()
    data = handler.wfile.getvalue()
    body = json.loads(data) if data else None
    if data:
        assert int(dict(headers)["Content-Length"]) == len(data)
    return SimpleNamespace(body=body, statuses=statuses, calls=calls)


def test_pending_message_reaches_serialized_http_response_then_display_deduplicates(isolated):
    case = isolated
    view = module().create(case.ctx)
    assert view is not None
    native = tool_result()
    before_native = deepcopy(native)
    sixth = case.queue("完整的新反馈：只读HTTP可见，不消费、不代做。", "http-six")
    before = stored_bytes(case)
    first = request(view, native)
    assert first.statuses == [200] and len(first.calls) == 1
    result = first.body["result"]
    fact, = payload(result)["messages"]
    assert fact["text"] == sixth["text"]
    assert (fact["source_team_id"], fact["message_id"]) == (case.root, 6)
    assert payload(result)["visibility_only"] is True
    assert payload(result)["execution_deduplicated"] is False
    assert result["content"][:-1] == native["content"]
    assert {key: result[key] for key in ("isError", "_meta")} == {
        key: native[key] for key in ("isError", "_meta")}
    assert request(view, native).body["result"] == native
    assert native == before_native and stored_bytes(case) == before


def test_failed_http_write_does_not_acknowledge_display(isolated):
    case = isolated
    view = module().create(case.ctx)
    case.queue("失败发送后仍可见", "failed-write")
    before = stored_bytes(case)

    class BrokenOutput:
        def write(self, _raw):
            raise BrokenPipeError("fixture client disconnected")

    with pytest.raises(BrokenPipeError):
        request(view, tool_result(), output=BrokenOutput())
    assert not view._shown
    assert payload(request(view, tool_result()).body["result"])["messages"][0]["text"] == "失败发送后仍可见"
    assert stored_bytes(case) == before


@pytest.mark.parametrize("method", ["initialize", "tools/list", "ping", "unknown/method"])
def test_non_call_requests_do_not_display_or_acknowledge_feedback(isolated, method):
    case = isolated
    view = module().create(case.ctx)
    case.queue("仅工具回包展示", "tools-only")
    before = stored_bytes(case)
    response = request(view, tool_result(), method=method)
    assert response.statuses == [200]
    assert "MEYO_PENDING_FEEDBACK_JSON" not in json.dumps(response.body)
    assert not response.calls and not view._shown
    assert stored_bytes(case) == before


@pytest.mark.parametrize("denied", ["auth", "session", "origin"])
def test_failed_authentication_does_not_expose_or_acknowledge_feedback(isolated, denied):
    case = isolated
    view = module().create(case.ctx)
    case.queue("授权失败不能泄露正文", "auth-bound")
    before = stored_bytes(case)
    response = request(view, tool_result(), **{denied: False})
    assert response.statuses == [401 if denied == "auth" else 403]
    assert "MEYO_PENDING_FEEDBACK_JSON" not in json.dumps(response.body)
    assert not response.calls and not view._shown
    assert stored_bytes(case) == before


@pytest.mark.parametrize("phase", ["prepare", "note_sent"])
def test_optional_feedback_failure_cannot_replace_native_result_or_resend_http(isolated, phase):
    def fail(*_args):
        raise RuntimeError("private feedback error")

    view = SimpleNamespace(
        prepare=fail if phase == "prepare" else lambda result: (result, ("shown",)),
        note_sent=fail if phase == "note_sent" else lambda _keys: None,
    )
    native = tool_result()
    response = request(view, native)
    assert response.statuses == [200] and len(response.calls) == 1
    assert response.body["result"] == native


def test_legacy_lease_without_feedback_view_keeps_native_result(isolated):
    assert request(None, tool_result()).body["result"] == tool_result()
