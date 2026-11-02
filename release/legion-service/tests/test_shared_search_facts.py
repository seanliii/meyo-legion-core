"""Shared search projection: real entry functions, controlled offline worker replies."""
from __future__ import annotations

import ast
import copy
import importlib.util
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

SERVICE = Path(__file__).resolve().parents[1]
ROOT = SERVICE.parents[1]
EVOLUTION = ROOT / "runtime/core-sources/auto-tools/v11/evolution"
BODY = "BODY_SENTINEL_" + "x" * 1500


def friday_hit():
    """Use the published provider normalizer; it does not fetch or load credentials."""
    path = EVOLUTION / "friday_universal.py"
    nodes = [node for node in ast.parse(path.read_text()).body
             if isinstance(node, ast.FunctionDef)
             and node.name in ("_score_completeness", "_normalize_item")]
    namespace = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace["_normalize_item"]({
        "url": "https://example.invalid/source", "title": "Synthetic source",
        "content": BODY, "snippet": "SHORT_EXCERPT",
        "source": "provider-web", "publish_time": "2026-11-01",
    }, "google-search")


@pytest.fixture
def search_stack(tmp_path, monkeypatch):
    for name in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "TMPDIR"):
        path = tmp_path / name.lower()
        path.mkdir()
        monkeypatch.setenv(name, str(path))
    monkeypatch.delenv("LEGION_WEB_SEARCH_DEFAULT_SOURCE", raising=False)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")

    def forbidden(*args, **kwargs):
        raise AssertionError("Search projection tests must not start processes or use the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    spec = importlib.util.spec_from_file_location("shared_search_facts_tool", SERVICE / "legion_tool.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    monkeypatch.setattr(tool, "_search_defaults", lambda: {})
    state = SimpleNamespace(tool=tool, replies={}, requests=[], exit_code=0)

    def worker(command, timeout, *, input_text):
        assert Path(command[-1]) == SERVICE / "search_fabric_worker.py"
        request = json.loads(input_text)
        state.requests.append(request)
        return state.exit_code, json.dumps(state.replies[request["query"]])

    monkeypatch.setitem(sys.modules, "media_tools", SimpleNamespace(
        MEDIA_TOOLS={}, _bounded_media_process=worker))
    return state


def invoke(stack, reply, **params):
    stack.replies["fixture"] = copy.deepcopy(reply)
    return stack.tool._run_tool_inner(
        "runnable:web_search", {"query": "fixture", "source": "fixture-source", **params})


def test_real_friday_metadata_and_publication_time_survive(search_stack):
    hit = friday_hit()
    result = invoke(search_stack, {"hits": [hit], "trace": []})
    row = result["results"][0]
    assert row["meta"] == hit["meta"]
    assert row["published_at"] == hit["meta"]["publish_time"]
    # This is the provider's length-based label, not a new product quality verdict.
    assert row["provider_completeness"] == hit["completeness"]
    assert row["source"] == hit["source"]


def test_body_is_preserved_exactly_once_and_unknown_facts_are_not_invented(search_stack):
    hit = {"url": "https://example.invalid/plain", "content": BODY}
    result = invoke(search_stack, {"hits": [hit], "trace": []})
    row = result["results"][0]
    assert row["snippet"] == BODY
    assert json.dumps(result).count(BODY) == 1
    assert not {"raw_content", "body_available", "cache_hit", "provider_completeness"} & row.keys()
    assert row["published_at"] is None


@pytest.mark.parametrize("flag", (False, True))
def test_only_provided_metadata_is_preserved_including_explicit_false(search_stack, flag):
    hit = friday_hit()
    hit["meta"] = {"engine": "tavily_cached", "score": 0,
                   "model_native_search": flag, "has_snippet": flag,
                   "private_trace": "DO_NOT_FORWARD", "content": BODY}
    row = invoke(search_stack, {"hits": [hit], "trace": []})["results"][0]
    assert row["meta"] == {"engine": "tavily_cached", "score": 0,
                           "model_native_search": flag, "has_snippet": flag}
    assert "DO_NOT_FORWARD" not in json.dumps(row)
    assert json.dumps(row).count(BODY) == 1
    assert "cache_hit" not in row


def test_native_cache_hit_trace_is_not_dropped_by_a_later_trace_tail(search_stack):
    # This exact marker is emitted by the native SearchFabric._call_source cache branch.
    trace = ["cache_hit:fixture-source"] + [f"operation:{i}" for i in range(13)]
    result = invoke(search_stack, {"hits": [friday_hit()], "trace": trace})
    assert result["trace"] == trace
    assert "cache_hit" not in result  # An absent hit marker is not a proven cache miss.


def test_native_transient_failure_marker_is_not_relabelled_as_just_empty(search_stack):
    marker = {"_source_health": "all_transient_fail", "_transient": True,
              "_note": "primary sources all rate-limited/timeout; NOT a genuine no-result",
              "_errors": ["fixture_429"]}
    result = invoke(search_stack, {"hits": [marker], "trace": []})
    assert result["ok"] is False and result["results"] == []
    assert result["source_errors"] == [{
        "query": "fixture", "requested_source": "fixture-source", **marker,
    }]


def test_worker_exception_keeps_its_type_and_message(search_stack):
    result = invoke(search_stack, {
        "hits": [], "trace": [], "error_type": "PermissionError",
        "error": "fixture permission denied",
    })
    assert result["ok"] is False
    assert result["source_errors"] == [{
        "query": "fixture", "requested_source": "fixture-source",
        "error_type": "PermissionError", "error": "fixture permission denied",
    }]


def test_source_error_after_result_limit_preserves_returned_success(search_stack):
    error = {"_error": "fixture provider failure", "_transient": False,
             "source": "fixture-provider", "private_trace": "DO_NOT_FORWARD"}
    result = invoke(search_stack, {"hits": [friday_hit(), error], "trace": []}, limit=1)
    assert result["ok"] is True and result["count"] == 1 and result["error"] is None
    assert result["source_errors"] == [{
        "query": "fixture", "requested_source": "fixture-source",
        "_error": "fixture provider failure", "_transient": False, "source": "fixture-provider",
    }]
    assert "DO_NOT_FORWARD" not in json.dumps(result)


def test_empty_result_does_not_invent_transient_or_permanent_error(search_stack):
    result = invoke(search_stack, {"hits": [], "trace": []})
    assert result["ok"] is False and result["source_errors"] == []
    assert "_transient" not in json.dumps(result)


def test_later_query_success_retains_earlier_worker_failure(search_stack):
    search_stack.replies.update({
        "first": {"hits": [], "trace": [], "error_type": "TimeoutError", "error": "fixture timeout"},
        "second": {"hits": [friday_hit()], "trace": []},
    })
    result = search_stack.tool._run_web_search(queries=["first", "second"], source="fixture-source")
    assert result["ok"] is True and result["results"][0]["query"] == "second"
    assert result["source_errors"][0]["query"] == "first"
    assert result["source_errors"][0]["error_type"] == "TimeoutError"


def test_cli_serializes_all_shared_search_facts_without_body_duplication(search_stack, monkeypatch, capsys):
    search_stack.replies["fixture"] = {"hits": [friday_hit()], "trace": ["cache_hit:fixture-source"]}
    # Only the unchanged permission/usage wrapper is bypassed; routing, projection and CLI are real.
    monkeypatch.setattr(search_stack.tool, "run_tool", lambda name, params, **kw:
                        search_stack.tool._run_tool_inner(name, params))
    code = search_stack.tool._main([
        "run", "runnable:web_search", json.dumps({"query": "fixture", "source": "fixture-source"}),
    ])
    assert code == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["results"][0]["meta"]["friday_source"] == "provider-web"
    assert result["trace"] == ["cache_hit:fixture-source"]
    assert output.count(BODY) == 1


@pytest.mark.parametrize("query", (
    "2031 roadmap release notes",
    "2031 RoadMap   2.1 release notes",
))
def test_google_empty_fallback_keeps_full_query_in_call_and_receipt(search_stack, monkeypatch, query):
    calls = []
    search_stack.replies[query] = {"hits": [], "trace": []}
    monkeypatch.setattr(search_stack.tool, "_run_search_github",
                        lambda current, limit: calls.append(current) or {"repos": [{
                            "full_name": "fixture/repository",
                            "clone_url": "https://example.invalid/fixture/repository.git",
                            "desc": "Synthetic fallback result",
                        }]})
    result = search_stack.tool._run_web_search(query, source="google")
    assert search_stack.requests[0]["query"] == query
    assert calls == [query]
    assert result["queries"] == [query]
    assert result["results"][0]["query"] == query
    assert result["results"][0]["source"] == "github"
    assert result["degraded_from"] == "google"


@pytest.mark.parametrize("kind", ("trace", "error-hit", "worker-error", "local-error"))
def test_shared_diagnostics_redact_existing_secret_forms_only(
        search_stack, monkeypatch, kind):
    bearer = "fixture-bearer-not-a-real-token"
    url_secret = "fixture-url-not-a-real-token"
    env_secret = "fixture-env-not-a-real-secret"
    monkeypatch.setenv("LEGION_FIXTURE_API_KEY", env_secret)
    message = (f"Bearer {bearer} url=https://example.invalid/?access_token={url_secret}"
               f"&mode=fixture native diagnostic {env_secret}")
    body = {"url": "https://example.invalid/body", "content": message}
    error = {"_error": message, "_transient": None,
             "_errors": [{"error": message, "status_code": None}]}
    reply = {"hits": [body], "trace": [message]}
    if kind == "error-hit":
        reply = {"hits": [body, error], "trace": []}
    elif kind == "worker-error":
        reply = {"hits": [], "trace": [], "error_type": "RuntimeError", "error": message}
    elif kind == "local-error":
        def failure(*args, **kwargs):
            raise RuntimeError(message)
        monkeypatch.setattr(sys.modules["media_tools"], "_bounded_media_process", failure)
    result = invoke(search_stack, reply)
    diagnostics = json.dumps({"trace": result["trace"], "source_errors": result["source_errors"]})
    for secret in (bearer, url_secret, env_secret):
        assert secret not in diagnostics
    assert "[REDACTED]" in diagnostics
    if kind in ("trace", "error-hit"):
        # A document can discuss credential syntax; diagnostic redaction must not edit it.
        assert result["results"][0]["snippet"] == message
        assert "cache_hit" not in result["results"][0]
    if kind == "error-hit":
        assert result["source_errors"][0]["_transient"] is None
        assert result["source_errors"][0]["_errors"][0]["status_code"] is None


@pytest.mark.parametrize("kind", ("worker-error", "local-error"))
def test_parent_redacts_environment_secret_before_error_clipping(
        search_stack, monkeypatch, kind):
    secret = "fixture-long-secret-" + "z" * 240
    monkeypatch.setenv("LEGION_FIXTURE_SECRET_KEY", secret)
    message = f"prefix {secret} suffix"
    if kind == "local-error":
        def failure(*args, **kwargs):
            raise RuntimeError(message)
        monkeypatch.setattr(sys.modules["media_tools"], "_bounded_media_process", failure)
    result = invoke(search_stack, {
        "hits": [], "trace": [], "error_type": "RuntimeError", "error": message,
    })
    diagnostics = json.dumps({"trace": result["trace"], "source_errors": result["source_errors"]})
    assert secret[:40] not in diagnostics
    assert result["source_errors"][0]["error"] == "prefix [REDACTED] suffix"
    assert result["trace"] == ["fixture:RuntimeError:prefix [REDACTED] suffix"]


def worker_module():
    spec = importlib.util.spec_from_file_location("shared_search_facts_worker", SERVICE / "search_fabric_worker.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_worker_redacts_exception_before_its_160_character_crop(search_stack, monkeypatch, capsys):
    secret = "fixture-worker-secret-" + "w" * 240
    monkeypatch.setenv("LEGION_FIXTURE_API_KEY", secret)
    worker = worker_module()

    def failure(request):
        raise RuntimeError(f"prefix {secret} suffix")

    monkeypatch.setattr(worker, "search", failure)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "query": "fixture", "source": "fixture-source", "mode": "fast", "top_k": 1,
    })))
    worker.main()
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == "prefix [REDACTED] suffix"
    assert result["error_type"] == "RuntimeError"


def test_worker_redacts_only_diagnostic_views_not_document_content(search_stack, monkeypatch, capsys):
    secret = "fixture-worker-environment-secret"
    monkeypatch.setenv("LEGION_FIXTURE_API_KEY", secret)
    message = (f"Bearer fixture-worker-bearer https://example.invalid/?api_key=fixture-worker-query "
               f"native diagnostic {secret}")
    worker = worker_module()
    original = {
        "hits": [
            {"url": "https://example.invalid/body", "content": message},
            {"_source_health": "all_transient_fail", "_transient": None, "_errors": [message]},
        ],
        "trace": [message],
    }
    monkeypatch.setattr(worker, "search", lambda request: copy.deepcopy(original))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "query": "fixture", "source": "fixture-source", "mode": "fast", "top_k": 1,
    })))
    worker.main()
    result = json.loads(capsys.readouterr().out)
    assert result["hits"][0]["content"] == message
    diagnostics = json.dumps({"trace": result["trace"], "marker": result["hits"][1]})
    for token in (secret, "fixture-worker-bearer", "fixture-worker-query"):
        assert token not in diagnostics
    assert result["hits"][1]["_transient"] is None
