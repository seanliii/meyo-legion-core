"""The request total must not bill replayed results, and must include coordination."""
from copy import deepcopy

from test_coordination_wave_continuity import case, TEAM, OWNER, CONVERSATION, HARNESS


def test_persistent_counts_new_calls_and_coordinator_without_rebilling_retained(case):
    plan = deepcopy(case.plan)
    plan["tasks"] = {"A": plan["tasks"]["A"], "B": plan["tasks"]["B"]}
    plan["tasks"]["A"]["task"] = "Retained original work"
    calls = []

    def member(key, prompt, **kwargs):
        calls.append(kwargs["task_facts"]["task_id"])
        return {"status": "done", "final": "new result",
                "usage": {"input_tokens": 7, "output_tokens": 3}}

    def control(*args, **kwargs):
        return {"status": "done", "final": "all current results reviewed",
                "usage": {"input_tokens": 4, "output_tokens": 1},
                "orchestration_request": {"coordinate": {
                    "action": "complete", "reason": "current call finished"}}}

    case.mp.setattr(case.core, "run_one", member)
    case.mp.setattr(case.core, "_run_one_with_gateway", control)
    out = case.core.dispatch_persistent(
        "original objective", plan, qhash=TEAM, mode="orchestrate",
        user_id=OWNER, conversation_id=CONVERSATION, max_rounds=1,
        _retained_results={"A": {
            "key": "A", "task_id": "A", "status": "done", "final": "old work",
            "assigned_harness": HARNESS, "usage": {"input_tokens": 900, "output_tokens": 100}}})
    assert calls == ["B"]
    assert out["usage"]["total_tokens"] == 15
    assert out["usage"]["scope"] == "persistent_invocation"
    assert out["usage"]["retained_results_excluded"] == 1
    assert out["usage"]["coordination_calls"] == 1
    assert out["usage"]["member_calls"] == 1
