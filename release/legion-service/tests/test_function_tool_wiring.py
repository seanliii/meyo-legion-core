"""Source discovery, direct calls and member delegation; no model or live probe."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest


SERVICE = Path(__file__).resolve().parents[1]
REPO = SERVICE.parents[1]


@pytest.fixture(autouse=True)
def case(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith(("MEYO_", "LEGION_", "AIGC_", "OPENAI_", "ANTHROPIC_")):
            monkeypatch.delenv(key)
    for key in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "LEGION_PROC_REGISTRY_DIR"):
        directory = tmp_path / key
        directory.mkdir()
        monkeypatch.setenv(key, str(directory))
    monkeypatch.setenv("LEGION_GATEWAY_RETRIES", "0")
    monkeypatch.setenv("LEGION_EXT_AUTO_UPDATE", "0")
    monkeypatch.syspath_prepend(str(REPO / "harness_baselines/meyo-orchestrator"))
    monkeypatch.syspath_prepend(str(SERVICE))
    invoke = importlib.import_module("func_invoke")
    index = importlib.import_module("func_index")
    provider = importlib.import_module("agent_tool_provider")
    core = importlib.import_module("legion_core")
    tools = importlib.import_module("legion_tool")
    toolbox = importlib.import_module("toolbox")
    registry, _, _ = provider._runtime_binding()
    monkeypatch.setattr(registry._INPROC_RUNTIME_ENV, "values", {}, raising=False)
    monkeypatch.setattr(registry._INPROC_RUNTIME_ENV, "inherit_process_context", True, raising=False)
    monkeypatch.setattr(registry._INPROC_CANCEL_CHECK, "check", None, raising=False)
    monkeypatch.setattr(registry._INPROC_CANCEL_CHECK, "cancelled", False, raising=False)
    monkeypatch.setattr(core, "is_cancelled", lambda *a: False)
    monkeypatch.setattr(core, "run_one", lambda *a, **kw: pytest.fail("unexpected member execution"))
    monkeypatch.setattr(tools, "_tool_source_ref", lambda name: {"name": name})
    monkeypatch.setattr(tools, "_record_tool_usage", lambda *a: {})
    external_rows = []
    monkeypatch.setattr(provider.external, "probe", lambda *a, **kw: {"agents": external_rows})
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(invoke, "_REPO", str(tmp_path))
    monkeypatch.setattr(index, "_REPO", str(tmp_path))
    monkeypatch.setattr(index, "_ROOTS", {"__ut_source": str(source)})
    # The packaged peer-source manifest is a separate discovery input. Keep
    # this synthetic test independent of any installed sources on the host.
    sandbox = importlib.import_module("legion_sandbox")
    monkeypatch.setattr(sandbox, "peer_source_entries", lambda *a, **kw: [])
    monkeypatch.setattr(index, "_CACHE", str(tmp_path / "index.json"))
    monkeypatch.setattr(index, "_BUILD_MEM", {"path_stat": None, "data": None})
    monkeypatch.setattr(index, "_SEARCH_CACHE", {})
    monkeypatch.setattr(index, "_SEARCH_DOCS", {"entries_id": 0, "rows": []})
    monkeypatch.setattr(toolbox, "_CACHE", str(tmp_path / "toolbox.json"))
    monkeypatch.setattr(toolbox, "_CACHE_MEMORY", {"data": None, "stamp": None})
    monkeypatch.setattr(toolbox, "_skill_source_roots", lambda *a: [])
    old_modules = {name: value for name, value in sys.modules.items() if name.startswith("_fi_")}
    value = SimpleNamespace(
        root=tmp_path, source=source, invoke=invoke, index=index, provider=provider,
        core=core, tools=tools, toolbox=toolbox, external_rows=external_rows,
    )
    yield value
    for name in list(sys.modules):
        if name.startswith("_fi_") and name not in old_modules:
            sys.modules.pop(name, None)
    sys.modules.update(old_modules)


def source_file(case, text, name="functions.py"):
    path = case.source / name
    path.write_text(text)
    return str(path)


def source_modules(path):
    return {name: module for name, module in tuple(sys.modules.items())
            if name.startswith("_fi_") and getattr(module, "__file__", None) == path}


def call_source(case, path, qual, args=None, **extra):
    return case.tools.run_tool("runnable:func_call", {
        "path": path, "qual": qual, "family": "__ut_source", "args": args or {},
        "dispatch": False, **extra,
    })


def declare_member(case, *, ready=True, installed=True, tools=None):
    row = {
        "key": "__ut_member", "installed": installed, "execution_ready": ready,
        "available": ready, "execution_role": "agent",
        "tools": ["shell"] if tools is None else tools,
        "status": "ready" if ready is True else "unknown" if ready is None else "missing_credentials",
        "status_detail": "" if ready is not False else "fixture credential not configured",
    }
    case.external_rows.append(row)
    return row


def test_real_stdlib_dataclass_function_works_through_shared_entry(case):
    path = REPO / "runtime/core-sources/auto-tools/qa_system/agent/qa_step_orchestrator.py"
    result = call_source(case, str(path), "decide", {"projection": {}, "budget_closing": True})
    assert result["ok"] is True, result
    assert "budget_closing" in result["result"] and "converge" in result["result"]
    assert result["authorization"]["action"] == "execute"


def test_module_identity_is_present_during_import_and_function_execution(case):
    path = source_file(case, (
        "import sys\n"
        "registered_at_import = __name__ in sys.modules\n"
        "def module_identity():\n"
        "    return registered_at_import and sys.modules[__name__].__dict__ is globals()\n"
    ))
    result = call_source(case, path, "module_identity")
    assert result["ok"] is True, result
    assert result["result"] == "True"


@pytest.mark.parametrize("failure", ["RuntimeError('fixture import failure')", "SystemExit('fixture stop')"])
def test_failed_import_removes_only_its_incomplete_module(case, failure):
    path = source_file(case, "import sys\nassert __name__ in sys.modules\nraise " + failure + "\n")
    if failure.startswith("SystemExit"):
        with pytest.raises(SystemExit, match="fixture stop"):
            case.invoke.invoke(path, "unused", "__ut_source", dispatch=False)
    else:
        result = case.invoke.invoke(path, "unused", "__ut_source", dispatch=False)
        assert "fixture import failure" in result["reason"], result
    assert not source_modules(path)


@pytest.mark.parametrize("failure", ["RuntimeError('failed reload')", "SystemExit('failed reload')"])
def test_failed_reload_preserves_previous_complete_module(case, failure):
    path = source_file(case, "def answer():\n    return 7\n")
    assert call_source(case, path, "answer")["result"] == "7"
    previous = source_modules(path)
    assert len(previous) == 1
    Path(path).write_text("raise " + failure + "\n")
    if failure.startswith("SystemExit"):
        with pytest.raises(SystemExit, match="failed reload"):
            case.invoke.invoke(path, "answer", "__ut_source", dispatch=False)
    else:
        result = call_source(case, path, "answer")
        assert result["ok"] is False and "failed reload" in result["reason"]
    assert source_modules(path) == previous
    assert next(iter(previous.values())).answer() == 7


def test_same_source_version_preserves_module_state_and_import_side_effects(case):
    path = source_file(case, (
        "from pathlib import Path\n"
        "with Path(__file__).with_suffix('.imports').open('a') as receipt:\n"
        "    receipt.write('import\\n')\n"
        "calls = 0\n"
        "def next_call():\n"
        "    global calls\n"
        "    calls += 1\n"
        "    return calls\n"
    ))
    first = call_source(case, path, "next_call")
    second = call_source(case, path, "next_call")
    assert first["ok"] and second["ok"], (first, second)
    assert [first["result"], second["result"]] == ["1", "2"]
    assert Path(path).with_suffix(".imports").read_text() == "import\n"


def test_nested_calls_keep_active_module_identity(case):
    path = source_file(case, (
        "from dataclasses import dataclass\n"
        "import pickle\n"
        "import func_invoke\n"
        "@dataclass\n"
        "class Parcel:\n"
        "    value: int\n"
        "def inner():\n"
        "    return 'inner completed'\n"
        "def outer():\n"
        "    reply = func_invoke.invoke(__file__, 'inner', '__ut_source', dispatch=False)\n"
        "    assert reply['ok'], reply\n"
        "    return pickle.loads(pickle.dumps(Parcel(7))).value\n"
    ))
    result = call_source(case, path, "outer")
    assert result["ok"] is True, result
    assert result["result"] == "7"


def test_concurrent_calls_share_identity_without_serializing_function_bodies(case):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    path = source_file(case, (
        "from dataclasses import dataclass\n"
        "import pickle\n"
        "@dataclass\n"
        "class Parcel:\n"
        "    value: int\n"
        "def round_trip(value, barrier):\n"
        "    barrier.wait(timeout=5)\n"
        "    return pickle.loads(pickle.dumps(Parcel(value))).value\n"
    ))
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(case.invoke.invoke, path, "round_trip", "__ut_source",
                               {"value": value, "barrier": barrier}, dispatch=False)
                   for value in (3, 7)]
        results = [future.result(timeout=7) for future in futures]
    assert all(result["ok"] for result in results), results
    assert [result["result"] for result in results] == ["3", "7"]


def test_changed_source_keeps_inflight_dataclass_identity(case):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    source = (
        "from dataclasses import dataclass\n"
        "import pickle\n"
        "@dataclass\n"
        "class Parcel:\n"
        "    value: int\n"
        "def round_trip(entered=None, release=None):\n"
        "    if entered is not None:\n"
        "        entered.set()\n"
        "        assert release.wait(timeout=5), 'fixture release timed out'\n"
        "    return pickle.loads(pickle.dumps(Parcel(1))).value\n"
    )
    path = source_file(case, source)
    before = Path(path).stat()
    entered, release = Event(), Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(case.invoke.invoke, path, "round_trip", "__ut_source",
                             {"entered": entered, "release": release}, dispatch=False)
        try:
            assert entered.wait(timeout=5), "original function did not start"
            Path(path).write_text(source.replace("Parcel(1)", "Parcel(2)"))
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            current = call_source(case, path, "round_trip")
            assert current["ok"] and current["result"] == "2", current
        finally:
            release.set()
        original = future.result(timeout=7)
    assert original["ok"] and original["result"] == "1", original


def test_function_failure_does_not_repeat_side_effect_or_delegate(case):
    path = source_file(case, (
        "from pathlib import Path\n"
        "def fail_after_write():\n"
        "    with Path(__file__).with_suffix('.calls').open('a') as receipt:\n"
        "        receipt.write('called\\n')\n"
        "    raise RuntimeError('fixture failure after effect')\n"
    ))
    result = call_source(case, path, "fail_after_write", dispatch=True)
    assert result["ok"] is False and "fixture failure after effect" in result["error"]
    assert result["dispatch_attempted"] is False
    assert Path(path).with_suffix(".calls").read_text() == "called\n"


def test_plain_function_api_and_current_source_remain_usable(case):
    path = source_file(case, "def add(a, b=2):\n    return a + b\n")
    assert call_source(case, path, "add", {"a": 3})["result"] == "5"
    Path(path).write_text("def add(a, b=2):\n    return a + b + 10\n")
    assert call_source(case, path, "add", {"a": 3})["result"] == "15"


def test_instance_method_reports_binding_requirement_without_constructing(case):
    path = source_file(case, (
        "class Journal:\n"
        "    def __init__(self):\n        raise AssertionError('do not invent an instance')\n"
        "    def remember(self, fact):\n        return fact\n"
    ))
    result = call_source(case, path, "Journal.remember", {"fact": "isolated fact"})
    assert result["ok"] is False
    assert result.get("callable") == "requires_instance", result
    assert result["requires_instance"] is True and result["dispatched"] is False
    row = next(r for r in case.index.search("Journal.remember")["results"]
               if r["qual"] == "Journal.remember")
    assert row["callable"] == "requires_instance"
    assert row["requires_instance"] is True and "self" in row["sig"]


@pytest.mark.parametrize(("qual", "args", "expected"), [
    ("Tools.plus", {"n": 3}, "4"),
    ("Tools.named", {}, "Tools"),
])
def test_static_and_class_methods_remain_callable(case, qual, args, expected):
    path = source_file(case, (
        "class Tools:\n"
        "    @staticmethod\n    def plus(n):\n        return n + 1\n"
        "    @classmethod\n    def named(cls):\n        return cls.__name__\n"
    ))
    result = call_source(case, path, qual, args)
    assert result["ok"] is True and result["result"] == expected
    row = next(r for r in case.index.search(qual)["results"] if r["qual"] == qual)
    assert row.get("requires_instance") is False
    assert row["callable"] == "direct_candidate" and row["callability_verified"] is False


def test_index_reports_candidates_not_verified_calls_and_keeps_keyword_arguments(case):
    source_file(case, "def choose(value, *, closing=False):\n    return value\n")
    index = case.index.build()
    assert index["directly_callable"] is None, "Discovery has not verified any execution."
    assert index["direct_candidates"] == 1
    assert index["callability_verified"] is False
    result = case.tools.run_tool("runnable:func_search", {"q": "choose"})
    row = next(r for r in result["results"] if r["qual"] == "choose")
    assert row["callable"] == "direct_candidate"
    assert row["callability_verified"] is False and "closing" in row["sig"]


def test_search_does_not_import_candidate_or_claim_dispatch(case):
    source_file(case, "raise AssertionError('search must not import')\ndef inspect_later():\n    pass\n")
    row = case.index.search("inspect_later")["results"][0]
    assert row["callable"] == "direct_candidate" and row["callability_verified"] is False
    source_file(case, "export function other_language() { return 1; }\n", "other.js")
    row = case.index.search("other_language")["results"][0]
    assert row["callable"] == "requires_runtime"
    assert row["dispatched"] is False


def test_old_index_claims_are_invalidated_without_touching_source(case):
    source_file(case, "def current():\n    return 1\n")
    _, identity = case.index._source_inputs()
    identity["schema"] = 1
    Path(case.index._CACHE).write_text(json.dumps({
        "source_identity": identity, "built_at": time.time(), "index_version": "legacy",
        "total": 1, "tools": 0, "functions": 1, "directly_callable": 1,
        "entries": [{"qual": "old", "family": "__ut_source", "tier": "function"}],
    }))
    result = case.index.build()
    assert result["from_cache"] is False
    assert result["directly_callable"] is None
    assert any(row["qual"] == "current" for row in result["entries"])


@pytest.mark.parametrize("lang", ["python", "javascript"])
def test_source_delegation_uses_existing_executor_and_preserves_parent_context(case, monkeypatch, lang):
    declare_member(case)
    path = source_file(case, "raise RuntimeError('fixture import failure')\n")
    project = case.root / "project"
    project.mkdir()
    scope = {
        "MEYO_USER_ID": "__ut_owner", "MEYO_SESSION_ID": "__ut_parent",
        "MEYO_WORKDIR": str(project),
        "MEYO_PROJECT_CONTEXT_JSON": json.dumps({"repo_root": str(project), "user_id": "__ut_owner"}),
        "MEYO_CAPABILITY_POLICY_JSON": '{"read":true,"execute":true,"network":false}',
        "MEYO_TASK_FACTS_JSON": '{"original_fact":"keep","native_conversation_id":"parent-slot"}',
        "MEYO_TASK_DEADLINE": str(time.monotonic() + 60),
    }
    calls = []

    def member(key, question, **kwargs):
        calls.append((key, question, kwargs))
        return {"status": "done", "final": "original member result", "workdir": str(project)}

    monkeypatch.setattr(case.core, "run_one", member)
    with case.provider._runtime_scope(scope, lambda: False, inherit_process_context=False):
        result = case.invoke.invoke(path, "arbitrary_function", "__ut_member", {"n": 3}, lang=lang)
    assert result["ok"] is True, result
    assert len(calls) == 1
    key, question, kwargs = calls[0]
    assert key == "__ut_member" and "arbitrary_function" in question and path in question
    assert kwargs["runtime_brief"]["borrowed_tool"] == "shell"
    assert kwargs["runtime_brief"]["borrowed_from"]["session_id"] == "__ut_parent"
    assert kwargs["user_id"] == "__ut_owner"
    assert kwargs["task_facts"]["project_context"]["repo_root"] == str(project)
    assert kwargs["task_facts"]["original_fact"] == "keep"
    assert "native_conversation_id" not in kwargs["task_facts"]
    assert kwargs["capability_policy"]["network"] is False
    assert 0 < kwargs["attempt_timeout"] <= 60
    assert kwargs["cancel_check"]() is False
    assert result["result"]["final"] == "original member result"
    assert result["dispatched"] is True and result["dispatch_attempted"] is True
    assert result["source_reason"], "Why local calling failed must remain visible."


@pytest.mark.parametrize("stop", ["cancel", "permission", "no_executor", "not_ready"])
def test_unstarted_delegation_is_not_labelled_dispatched_and_keeps_both_causes(case, stop):
    declare_member(case, ready=stop != "not_ready",
                   tools=["read"] if stop == "no_executor" else ["shell"])
    path = source_file(case, "raise RuntimeError('source import cause')\n")
    policy = {"execute": stop != "permission"}
    scope = {"MEYO_USER_ID": "__ut_owner", "MEYO_CAPABILITY_POLICY_JSON": json.dumps(policy)}
    with case.provider._runtime_scope(scope, lambda: stop == "cancel", inherit_process_context=False):
        result = case.invoke.invoke(path, "arbitrary_function", "__ut_member")
    assert result["ok"] is False
    assert result.get("dispatched") is False, result
    assert result["dispatch_attempted"] is False
    assert result["callable"] != "dispatched"
    assert "source import cause" in result["source_reason"]
    assert result["reason"]
    if stop == "cancel":
        assert "cancel" in result["reason"]
    elif stop == "permission":
        assert "CAPABILITY_DENIED" in result["reason"]
    elif stop == "not_ready":
        assert "fixture credential not configured" in result["reason"]


def test_unknown_native_tool_name_stays_rejected_before_member_execution(case):
    declare_member(case)
    result = case.provider.invoke("__ut_member", "arbitrary_function", {})
    assert result["ok"] is False and result["dispatched"] is False
    assert result["error"] == "unknown agent tool:__ut_member/arbitrary_function"


def test_member_failure_is_not_erased_or_claimed_to_have_started(case, monkeypatch):
    declare_member(case)
    path = source_file(case, "raise RuntimeError('source import cause')\n")
    monkeypatch.setattr(case.core, "run_one", lambda *a, **kw: {
        "status": "unavailable", "error": "fixture runtime missing", "final": "original partial",
    })
    result = case.invoke.invoke(path, "arbitrary_function", "__ut_member")
    assert result["ok"] is False
    assert result["dispatch_attempted"] is True
    assert result["dispatched"] is None and result["callable"] == "dispatch_attempted"
    assert result["result"]["final"] == "original partial"
    assert "fixture runtime missing" in result["reason"]


def test_missing_dependency_only_reports_and_does_not_install_or_delegate(case):
    declare_member(case)
    path = source_file(case, "import __ut_nonexistent_dependency_20261029\n")
    result = case.invoke.invoke(path, "unused", "__ut_member")
    assert result["ok"] is False and "__ut_nonexistent_dependency_20261029" in result["reason"]
    assert result.get("dispatched") is False
    assert result["dispatch_attempted"] is False


def test_shared_call_cannot_bypass_execute_permission(case):
    path = source_file(case, "def protected():\n    raise AssertionError('must not execute')\n")
    result = call_source(case, path, "protected", _capability_policy={"execute": False})
    assert result["error"] == "CAPABILITY_DENIED:execute"


def test_inventory_separates_declaration_installation_and_readiness(case):
    declare_member(case, installed=True, ready=False)
    inventory = case.provider.inventory()
    declared = {row["provider"]: row for row in inventory.get("declarations", [])}
    assert "__ut_member" in declared and "codex" in declared and "auto" in declared
    assert declared["__ut_member"]["installed"] is True
    assert declared["__ut_member"]["execution_ready"] is False
    assert declared["__ut_member"]["reason"] == "fixture credential not configured"
    assert declared["codex"]["installed"] is None and declared["codex"]["execution_ready"] is None
    assert declared["auto"]["execution_ready"] is False
    assert declared["auto"]["execution_role"] == "capability"
    assert not {"auto", "codex", "__ut_member"} & {row["provider"] for row in inventory["providers"]}
    search = case.provider.search("__ut_member")
    assert search["results"] and search["results"][0]["installed"] is True
    assert search["results"][0]["execution_ready"] is False


def test_role_wins_over_a_ready_probe_and_blocks_auto_member_invocation(case):
    case.external_rows.append({"key": "auto", "installed": True, "execution_ready": True,
                               "execution_role": "agent", "status": "ready"})
    inventory = case.provider.inventory()
    assert "auto" not in {row["provider"] for row in inventory["providers"]}
    result = case.provider.invoke("auto", "shell", {})
    assert result["ok"] is False and result["dispatched"] is False
    assert "capability" in result["error"]


def test_ready_list_does_not_invent_installation_information(case):
    declare_member(case, ready=True, installed=None)
    inventory = case.provider.inventory()
    row = next(row for row in inventory["providers"] if row["provider"] == "__ut_member")
    assert row["available"] is True and row["execution_ready"] is True
    assert row.get("installed") is None


def test_unknown_readiness_does_not_ban_an_explicit_declared_native_tool(case, monkeypatch):
    calls = []
    monkeypatch.setattr(case.core, "run_one", lambda *a, **kw: (
        calls.append((a, kw)) or {"status": "done", "final": "explicit runtime result"}))
    result = case.provider.invoke("codex", "shell", {"command": "fixture only"})
    assert result["ok"] is True and result["result"]["final"] == "explicit runtime result"
    assert len(calls) == 1
    declared = {row["provider"]: row for row in case.provider.inventory()["declarations"]}
    assert declared["codex"]["execution_ready"] is None, "An attempt must not rewrite installation facts."


def test_probe_error_retains_unknown_declarations_instead_of_fabricating_ready(case, monkeypatch):
    monkeypatch.setattr(case.provider.external, "probe",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("probe unavailable")))
    inventory = case.provider.inventory()
    assert inventory["providers"] == []
    declared = {row["provider"]: row for row in inventory["declarations"]}
    assert declared["codex"]["execution_ready"] is None
    assert declared["codex"]["reason"]


def test_toolbox_keeps_unknown_and_disabled_member_states(case):
    declare_member(case, ready=False, installed=True)
    rows = {row["id"]: row for row in case.toolbox._scan_agent_tool_providers()}
    assert rows["agent_tool:codex:shell"]["installed"] is None
    assert rows["agent_tool:codex:shell"]["runnable"] is None
    assert rows["agent_tool:codex:shell"]["health"] == "unknown"
    assert rows["agent_tool:auto:shell"]["runnable"] is False
    assert rows["agent_tool:__ut_member:shell"]["installed"] is True
    assert rows["agent_tool:__ut_member:shell"]["runnable"] is False
    assert rows["agent_tool:__ut_member:shell"]["reason"] == "fixture credential not configured"


def test_toolbox_invalidates_old_always_ready_projection(case):
    identity = case.toolbox.skill_source_identity()
    identity.pop("agent_tool_projection", None)
    Path(case.toolbox._CACHE).write_text(json.dumps({
        "skill_sources": identity, "scanned_at": time.time(),
        "items": [{"id": "agent_tool:auto:shell", "installed": True, "runnable": True}],
    }))
    assert case.toolbox._read_cached_toolbox() is None


def test_same_size_source_edit_does_not_reuse_stale_bytecode(case):
    path = source_file(case, "def answer():\n    return 1\n")
    assert call_source(case, path, "answer")["result"] == "1"
    before = Path(path).stat()
    Path(path).write_text("def answer():\n    return 2\n")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert call_source(case, path, "answer")["result"] == "2"


@pytest.mark.parametrize("source", [
    "async def later():\n    return 1\n",
    "def later():\n    yield 1\n",
])
def test_deferred_body_is_not_reported_as_an_executed_result(case, source):
    path = source_file(case, source)
    row = case.index.search("later")["results"][0]
    assert row["callable"] == "requires_runtime"
    result = call_source(case, path, "later")
    assert result["ok"] is False and result["callable"] == "requires_runtime"
    assert result["dispatched"] is False


def test_new_member_without_tool_declarations_gets_no_invented_defaults(case):
    case.external_rows.append({"key": "__ut_undeclared", "installed": True,
                               "execution_ready": True, "capabilities": ["coding", "web"]})
    inventory = case.provider.inventory()
    row = next(row for row in inventory["declarations"] if row["provider"] == "__ut_undeclared")
    assert row["tools"] == []
    assert all(row["provider"] != "__ut_undeclared"
               for row in case.provider.search("__ut_undeclared")["results"])


def test_invalid_tool_names_do_not_break_other_declarations(case):
    declare_member(case, tools=[{"name": []}, {"name": {}}, {"name": None},
                                {"name": 7}, "", " ", "shell", {"name": "shell"}])
    inventory = case.provider.inventory()
    row = next(row for row in inventory["declarations"] if row["provider"] == "__ut_member")
    assert [tool["name"] for tool in row["tools"]] == ["shell"]
    assert any(row["provider"] == "codex" for row in inventory["declarations"])


def prepare_main_search(case, monkeypatch, items=None):
    """Keep real retrieval/projection; isolate unrelated catalog IO and models."""
    items = items or []
    monkeypatch.setattr(case.tools, "_load_toolbox",
                        lambda: {"items": items, "summary": {"total": len(items)}})
    monkeypatch.setattr(case.tools, "_search_arsenal", lambda *a, **kw: [])
    monkeypatch.setattr(case.tools, "_search_mcp", lambda *a, **kw: [])
    semantic = importlib.import_module("tool_semantic_index")
    monkeypatch.setattr(semantic, "scores", lambda rows, query: ([0.0] * len(rows), "none"))
    usage = importlib.import_module("legion_tool_usage")
    monkeypatch.setattr(usage, "ranking_context", lambda *a, **kw: {})
    monkeypatch.setattr(usage, "boost_ranking", lambda rows, *a, **kw: rows)
    monkeypatch.setattr(usage, "record_query", lambda *a, **kw: None)
    case.index.build()


@pytest.mark.parametrize(("source", "qual"), [
    ("def candidate(value, *, enabled=False):\n    return value\n", "candidate"),
    ("class SomeTool:\n    def candidate(self, value):\n        return value\n",
     "SomeTool.candidate"),
    ("async def candidate(value):\n    return value\n", "candidate"),
])
def test_main_search_preserves_function_requirements_without_inventing_native_tools(
        case, monkeypatch, source, qual):
    source_file(case, source)
    prepare_main_search(case, monkeypatch)
    upstream = next(row for row in case.index.search("candidate")["results"]
                    if row["qual"] == qual)
    result = case.tools.search("candidate")
    row = next(row for row in result["harness_functions"] if row["qual"] == qual)
    for field in ("callable", "callability_verified", "requires_instance", "dispatched",
                  "dispatch_attempted", "reason", "use", "lang", "sig"):
        assert row.get(field) == upstream[field], (field, row)
    assert row["how"] == upstream["use"]
    assert result["match"] == "candidates", "A real source hit must not be reported as no match."


@pytest.mark.parametrize("ready", [True, False, None])
def test_main_search_keeps_member_readiness_reason_in_result_and_trace(case, monkeypatch, ready):
    declare_member(case, ready=ready, installed=True)
    item = next(row for row in case.toolbox._scan_agent_tool_providers()
                if row["id"] == "agent_tool:__ut_member:shell")
    prepare_main_search(case, monkeypatch, [item])
    result = case.tools.search("__ut_member")
    row = result["tools"][0]
    assert row["runnable"] is ready
    assert row["execution"]["execution_ready"] is ready
    assert row["execution"]["execution_role"] == "agent"
    assert row["execution"]["reason"] == item["reason"]
    trace = result["candidate_trace"]["candidates"][0]
    assert trace["runnable"] is ready, "Installed does not mean ready to execute."
    group = ("runnable_candidates" if ready is True else
             "reference_candidates" if ready is False else "unknown_candidates")
    assert result[group] == [row]


def test_unknown_member_search_does_not_label_declared_entry_reference_only(case, monkeypatch):
    item = next(row for row in case.toolbox._scan_agent_tool_providers()
                if row["id"] == "agent_tool:codex:shell")
    prepare_main_search(case, monkeypatch, [item])
    result = case.tools.search("codex")
    row = next(entry for entry in result["tools"] if entry["id"] == item["id"])
    calls = []

    def leaf(provider, prompt, **kwargs):
        calls.append((provider, kwargs["runtime_brief"]["borrowed_tool"]))
        return {"status": "done", "final": "offline executor boundary"}

    monkeypatch.setattr(case.core, "run_one", leaf)
    attempted = case.tools.run_tool(item["id"], {"arguments": {"command": "fixture-only"}})
    assert attempted["dispatch_attempted"] is True
    assert calls == [("codex", "shell")]
    assert row["runnable"] is None
    assert row["execution"]["installed"] is None
    assert row["execution"]["execution_ready"] is None
    assert row["execution"]["reason"] == item["reason"]
    assert row["execution"]["health"] == "unknown"
    assert "reference only" not in row["how"]
    assert f"legion_tool.py run {item['id']}" in row["how"]
    assert "未知" in row["how"] and "显式尝试" in row["how"]
    assert row["params"] == item["params"]
    assert result["unknown_candidates"] == [row]
    assert not result["runnable_candidates"] and not result["reference_candidates"]


def test_signature_keeps_positional_only_defaults_and_keyword_only(case):
    source_file(case, "def select(a, /, b=3, *, chosen=True, **extras):\n    return a\n")
    row = case.index.search("select")["results"][0]
    assert row["sig"] == "(a, /, b=3, *, chosen=True, **extras)"


def test_catalogue_does_not_turn_candidates_into_verified_calls(case, monkeypatch):
    source_file(case, "def only_candidate():\n    return 1\n")
    catalog = importlib.import_module("catalog_api")
    result = catalog._functions_section()
    assert result["directly_callable"] is None
    assert result["direct_candidates"] == 1
    assert result["callability_verified"] is False


def test_catalogue_discards_legacy_invented_call_counts(case, monkeypatch):
    catalog = importlib.import_module("catalog_api")
    path = case.root / "catalog.json"
    path.write_text(json.dumps({"functions": {"directly_callable": 9000}, "updated_at": time.time()}))
    monkeypatch.setattr(catalog, "_SNAPSHOT", str(path))
    monkeypatch.setattr(catalog, "_CATALOG_CACHE", {"at": 0, "data": None, "stamp": None})
    assert catalog._read_snapshot() is None
