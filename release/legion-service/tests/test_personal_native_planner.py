"""On-demand personal planning through the existing native runner, offline only."""
from copy import deepcopy
import importlib
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest

from test_explicit_responses_provider import isolated, explicit_run_one
from test_personal_planner_native_scope import assert_read_only_payload


USER = "__ut_planner_member"
CONVERSATION = "__ut_planner_conversation"
PLAN = {
    "tasks": {"implement": {"task": "Keep the original objective",
                            "assigned_harness": "codex", "depends_on": []}},
    "suggested_mode": "pk",
    "reason": "Synthetic planner response; no model was called",
}


@pytest.fixture
def planner_case(explicit_run_one):
    case, mp = explicit_run_one, explicit_run_one.monkeypatch
    for name, leaf in (
        ("CODEX_HOME", "codex-home"), ("LEGION_PERSONAL_ACCOUNTS_DIR", "accounts"),
        ("LEGION_TEAM_DIR", "teams"), ("LEGION_SESSIONS_DIR", "sessions"),
        ("LEGION_PROJECT_CONTEXT_DIR", "projects"), ("LEGION_USER_CONFIG_DIR", "users"),
    ):
        mp.setenv(name, str(case.root / leaf))
    mp.setenv("LEGION_PERSONAL_OWNER", "__ut_service_owner_not_the_caller")
    mp.setenv("MEYO_PERSONAL_DEFAULT_HARNESS", "codex")
    here = case.root / "package/release/legion-service"
    here.mkdir(parents=True)
    mp.setattr(case.core, "HERE", str(here))  # No legacy planner helper in this package.
    mp.setattr(case.core, "_autonomy_matrix", lambda: {"codex": {"cost_class": "standard"}})
    mp.setattr(case.core, "_aigc_ready", lambda: {"runnable_clis": ["codex"]})
    mp.setattr(importlib.import_module("harness_profiles"), "get_profiles",
               lambda: [{"key": "codex"}])
    mp.setattr(importlib.import_module("harness_performance"), "strengths", lambda key: [])
    mp.setattr(importlib.import_module("harness_performance"), "table", lambda: {"rows": []})

    def forbidden(*args, **kwargs):
        raise AssertionError("planner must not use a model helper, listener or external capability")

    for method in ("connect", "connect_ex", "bind", "listen"):
        mp.setattr(socket.socket, method, forbidden)
    mp.setattr(importlib.import_module("planner_runtime"), "run_cli", forbidden)
    for module, method in (
        ("managed_browser", "open_managed_browser"),
        ("native_capability_host", "native_stdio_spec"),
        ("native_device_permissions", "restrict_codex_device_control"),
    ):
        mp.setattr(importlib.import_module(module), method, forbidden)
    case.reply = json.dumps(PLAN)

    def native(command, *args, **kwargs):
        case.calls.append((command, kwargs))
        Path(command[command.index("-o") + 1]).write_text(case.reply)
        return SimpleNamespace(returncode=0, stderr="", stdout="\n".join((
            json.dumps({"type": "thread.started", "thread_id": "fixture-planner-native"}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 11}}),
        )))

    mp.setattr(case.hr, "_run_task", native)
    case.invocations = []
    gateway = case.core._run_one_with_gateway

    def observe(key, prompt, **kwargs):
        case.invocations.append((key, prompt, deepcopy({
            k: v for k, v in kwargs.items() if not callable(v)})))
        return gateway(key, prompt, **kwargs)

    mp.setattr(case.core, "_run_one_with_gateway", observe)
    return case


def plan(case, **kwargs):
    return case.core.plan_request_with_model(
        "完整用户目标", user_id=USER, conversation_id=CONVERSATION, **kwargs)


def test_native_planner_receives_current_index_without_configured_keys_claim(planner_case):
    case = planner_case
    case.monkeypatch.setattr(importlib.import_module("harness_view"), "selection_context",
                            lambda **kwargs: {"members": [{"key": "codex", "runtime_available": None}],
                                              "resource_index": {"discovery": [{"id": "actual-resource-index"}]},
                                              "cache": {"warming": True}})
    result = plan(case)
    assert result["ok"]
    prompt = case.invocations[0][1]
    assert "actual-resource-index" in prompt
    assert '"runtime_available": null' in prompt
    assert "key已配" not in prompt


def test_planner_permission_scope_does_not_revoke_user_deliverable(planner_case):
    case = planner_case
    question = "只读核对，不修改源代码；把核对结果写成可下载的说明文件。"
    result = case.core.plan_request_with_model(
        question, user_id=USER, conversation_id=CONVERSATION)
    assert result["ok"]
    prompt = case.invocations[0][1]
    payload = json.loads(prompt.split("现场:", 1)[1])
    scope = payload["planning_invocation"]
    assert scope["role"] == "plan_proposal"
    assert scope["permissions_apply_to"] == "current_planner_invocation_only"
    assert scope["user_request"] == question
    assert scope["delegated_permissions"] == "resolved_by_original_execution_path"
    assert "不继承规划进程的只读限制" in scope["meaning"]
    # Explaining the boundary must not relax the actual native invocation.
    _, _, kwargs = case.invocations[0]
    assert kwargs["task_facts"]["native_read_only"] is True
    assert_read_only_payload(*case.calls[0])


def test_native_planner_keeps_prompt_but_not_main_workspace_or_history(planner_case):
    case = planner_case
    memory = importlib.import_module("task_memory")
    original = case.root / "original-work"
    original.mkdir()
    artifact = original / "work.txt"
    artifact.write_text("untouched original artifact")
    memory.update(f"conv:codex:{CONVERSATION}", "codex",
                  native_checkpoint={"session_id": "do-not-resume-main-native"},
                  workdir=str(original), user_id=USER)
    main_path = memory._path(f"conv:codex:{CONVERSATION}", "codex")
    before = main_path.read_bytes()
    context = {
        "project_context": {"repo_root": str(original), "user_id": USER,
                            "conversation_id": CONVERSATION, "project_fact": "full original context"},
        "input_snapshot": {"original_query": "完整原话" * 1500},
        "conversation_context": "现场只进 prompt，不能充当授权或原生历史。",
        "native_checkpoint": {"session_id": "forged-main-session"},
        "capability_policy": {"write": True},
    }
    for _ in range(2):
        result = plan(case, request_context=deepcopy(context), candidate_harnesses=["codex"])
        assert result["ok"], result
        assert result["tasks"]["implement"]["assigned_harness"] == "codex"
        assert result["planner_transport"] == "native_codex"
    assert len(case.invocations) == len(case.calls) == 2
    requests = []
    for (key, prompt, kwargs), (command, options) in zip(case.invocations, case.calls):
        assert key == "codex"
        assert kwargs["user_id"] == USER and kwargs["conversation_id"] == CONVERSATION
        request_id = kwargs["qhash"]
        assert request_id.startswith("planner_")
        requests.append(request_id)
        assert kwargs["task_facts"] == {
            "native_session_scope": "request", "native_session_request_id": request_id,
            "native_read_only": True,
        }
        assert "lease_plan" not in kwargs and "capability_policy" not in kwargs
        sent_context = json.loads(prompt.split("现场:", 1)[1])
        for field in ("project_context", "input_snapshot", "conversation_context"):
            assert sent_context[field] == context[field]
        assert "媒体工具已共享且key已配" not in prompt
        assert "本次" in prompt and "授权" in prompt
        assert not Path(options["cwd"]).is_relative_to(original)
        assert_read_only_payload(command, options)
    assert len(set(requests)) == 2
    assert not case.selections
    assert main_path.read_bytes() == before
    assert artifact.read_text() == "untouched original artifact"


@pytest.mark.parametrize("identity", [
    {}, {"user_id": USER}, {"conversation_id": CONVERSATION},
    {"user_id": "", "conversation_id": CONVERSATION},
    {"user_id": USER, "conversation_id": "  "},
    {"user_id": 42, "conversation_id": CONVERSATION},
])
def test_personal_identity_cannot_come_from_context_or_service_owner(planner_case, identity):
    case = planner_case
    result = case.core.plan_request_with_model(
        "需求", request_context={
            "user_id": USER, "conversation_id": CONVERSATION,
            "project_context": {"user_id": USER, "conversation_id": CONVERSATION},
        }, **identity)
    assert not result["ok"] and result["error"] == "planner_identity_required"
    assert not case.invocations and not case.calls and not case.selections


def test_missing_explicit_provider_is_a_real_config_failure_not_helper_fallback(planner_case):
    case = planner_case
    case.monkeypatch.delenv("LEGION_CODEX_RESPONSES_BINDING")
    result = plan(case)
    assert not result["ok"] and result["error_envelope"]["category"] == "model_config"
    assert result["blocked_before_native"] is True
    assert not case.calls and not case.selections


def test_matching_request_provider_hold_prevents_native_launch(planner_case):
    case = planner_case
    errors = importlib.import_module("error_envelope")
    rejected = {
        "key": "prior-election", "status": "error",
        "error_envelope": {
            "category": "authorization_required", "scope": "provider",
            "retryable": False, "code": "appid_disabled",
            "message": "provider denied private-fixture-key",
            "details": {"authorization_fingerprint": errors.provider_authorization_fingerprint(
                "private-fixture-key", "https://models.example.invalid/v1")},
        },
    }
    result = plan(case, _provider_results=lambda: [rejected])
    assert not result["ok"] and errors.requires_provider_pause(result), result
    assert result["blocked_before_native"] is True and result["execution_calls"] == 0
    assert result["error_envelope"]["details"]["authorization_hold"]["basis"] == "same_authorization"
    assert "private-fixture-key" not in json.dumps(result)
    assert not case.calls and not case.selections


@pytest.mark.parametrize("when", ["before", "after", "native"])
def test_planner_cancellation_wins_over_json_and_failure(planner_case, when):
    case = planner_case
    called, cancelled = [], [when == "before"]

    def execute(*args, **kwargs):
        called.append(kwargs)
        assert callable(kwargs["cancel_check"])
        if when == "after":
            cancelled[0] = True
        return {"status": "cancelled" if when == "native" else "done",
                "cancelled": when == "native", "final": json.dumps(PLAN)}

    case.monkeypatch.setattr(case.core, "_run_one_with_gateway", execute)
    result = plan(case, cancel_check=lambda: cancelled[0])
    assert result["cancelled"] is True and not result["ok"]
    assert result["error"] == "planner_cancelled"
    assert len(called) == (0 if when == "before" else 1)


@pytest.mark.parametrize("status", ["error", "incomplete", "blocked"])
def test_failed_native_turn_cannot_publish_json_as_a_valid_plan(planner_case, status):
    case = planner_case
    calls = []

    def execute(*args, **kwargs):
        calls.append(kwargs)
        return {
            "status": status, "final": json.dumps(PLAN), "attempts": 1,
            "error": "HTTP 403: Bearer another-fixture-token private-fixture-key",
            "error_envelope": {
                "category": "authorization_required", "scope": "provider",
                "retryable": False, "code": "appid_disabled",
                "message": "Authorization: Bearer another-fixture-token; private-fixture-key",
                "details": {"http_status": 403, "nested": [
                    {"api_key": "nested-fixture-secret", "headers": {"Cookie": "cookie-fixture"}},
                ]},
            },
            "native_facts": {"native_turn_terminal": "unobserved", "cli_wait_timed_out": True},
        }

    case.monkeypatch.setattr(case.core, "_run_one_with_gateway", execute)
    result = plan(case)
    assert not result["ok"] and not result.get("tasks")
    assert len(calls) == 1 and not case.selections
    assert result["error_envelope"]["code"] == "appid_disabled"
    assert result["native_facts"]["native_turn_terminal"] == "unobserved"
    encoded = json.dumps(result)
    for secret in ("private-fixture-key", "another-fixture-token",
                   "nested-fixture-secret", "cookie-fixture"):
        assert secret not in encoded


def test_native_exception_never_echoes_command_or_prompt_and_does_not_retry(planner_case):
    case, calls = planner_case, []

    def execute(*args, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("argv contains private-fixture-key and original private prompt")

    case.monkeypatch.setattr(case.core, "_run_one_with_gateway", execute)
    result = plan(case)
    assert not result["ok"] and result["error"] == "planner_call:RuntimeError"
    assert len(calls) == 1
    assert "private-fixture-key" not in json.dumps(result)
    assert "original private prompt" not in json.dumps(result)


@pytest.mark.parametrize("reply", ["not JSON", '{"tasks": {}}', '{"tasks":'])
def test_native_parser_failure_is_not_retried_with_company_models(planner_case, reply):
    case = planner_case
    case.reply = reply
    result = plan(case)
    assert not result["ok"], result
    assert result["status"] == "error"
    assert result["planner_attempts"][0]["status"] == "done"  # Native completed, parsing failed.
    assert len(case.calls) == 1 and not case.selections


def test_valid_task_names_are_not_mistaken_for_private_diagnostic_fields(planner_case):
    case = planner_case
    # These are task IDs, not credential-bearing error fields.
    case.reply = json.dumps({"tasks": {
        key: {"task": "Implement this legitimate module", "assigned_harness": "codex"}
        for key in ("authorization", "headers", "token")
    }})
    expected = case.core._parse_planner_json(case.reply)
    result = plan(case)
    assert result["ok"] and result["tasks"] == expected["tasks"]


@pytest.mark.parametrize("personal,transport", [(False, "responses"), (True, "auto")])
def test_nonpersonal_or_nonresponses_keeps_existing_helper_route(planner_case, personal, transport):
    case = planner_case
    if not personal:
        case.monkeypatch.delenv("LEGION_PERSONAL_ACCOUNTS_DIR")
    case.monkeypatch.setenv("LEGION_CODEX_TRANSPORT", transport)
    result = case.core.plan_request_with_model("旧入口不需要新身份参数")
    assert result == {"ok": False, "error": "planner_tool_unavailable"}
    assert not case.invocations and not case.calls
