"""协议回归的最小离线场景；不调用模型、网络或外部进程。"""
from collections import Counter
from copy import deepcopy
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import pytest


SERVICE = Path(__file__).resolve().parents[1]
OWNER = "__ut_backpressure_owner"
SID = "__ut_backpressure_session"
TEAM = "__ut_backpressure_request"
HARNESS = "__ut_backpressure_worker"


def done(text="已交付"):
    return {"status": "done", "final": text}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in (
        "HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "LEGION_SESSIONS_DIR",
        "LEGION_TEAM_DIR", "LEGION_TASK_MEMORY_DIR", "LEGION_PROJECT_CONTEXT_DIR",
        "LEGION_ARTIFACT_STORE_DIR", "LEGION_PROC_REGISTRY_DIR", "LEGION_USER_CONFIG_DIR",
    ):
        path = tmp_path / name
        path.mkdir()
        monkeypatch.setenv(name, str(path))
    monkeypatch.setenv("LEGION_GATEWAY_RETRIES", "1")
    monkeypatch.setenv("LEGION_MAX_FAILOVER", "0")
    monkeypatch.syspath_prepend(str(SERVICE))

    def forbidden(*args, **kwargs):
        pytest.fail("offline regression must not use network or spawn an external process")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


@pytest.fixture
def scene(tmp_path, monkeypatch):
    import artifact_delivery
    import legion_core as core
    import legion_pilot
    import model_registry
    import task_memory
    import team_state

    monkeypatch.setattr(core, "_ORCH_OK", True)
    monkeypatch.setattr(core, "_CORE_INPROC_KEYS", set())
    monkeypatch.setattr(core, "_EXTERNAL_INPROC", False, raising=False)
    monkeypatch.setattr(core, "_harness_keys", lambda: {HARNESS})
    monkeypatch.setattr(core, "_aigc_ready", lambda: {"runnable_clis": [HARNESS]})
    monkeypatch.setattr(core, "_autonomy_matrix", lambda: {})
    monkeypatch.setattr(core, "_build_prelude", lambda *a, **kw: "")
    monkeypatch.setattr(core, "_build_runtime_brief", lambda *a, **kw: "")
    monkeypatch.setattr(core, "_record_experience", lambda *a, **kw: None)
    monkeypatch.setattr(core, "record_model_health", lambda *a, **kw: None)
    monkeypatch.setattr(core, "_record_gateway_call", lambda *a, **kw: None)
    monkeypatch.setattr(core, "_fallback_candidates", lambda *a, **kw: [])
    monkeypatch.setattr(core, "_dynamic_max_attempts", lambda *a: 1)
    monkeypatch.setattr(core, "_INTERMEDIATE_AFTER_S", 0)
    monkeypatch.setattr(task_memory, "ROOT", tmp_path / "LEGION_TASK_MEMORY_DIR")
    monkeypatch.setattr(artifact_delivery, "_git_changed_files", lambda root: None)
    monkeypatch.setattr(legion_pilot, "DEFAULT_USER_DIR", tmp_path / "LEGION_USER_CONFIG_DIR")
    legion_pilot.save_user_config(OWNER, {"enabled": True})
    monkeypatch.setattr(model_registry, "resolve_assignments", lambda plan, keys: {
        key: {"model": "offline-model", "appid": "offline-appid"} for key in keys})

    class Lease:
        wait_seconds = 0

        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(core, "_GatewayLease", Lease)
    work = tmp_path / "MEYO_LEGION_DIR" / "outputs" / "existing"
    work.mkdir(parents=True)
    state = SimpleNamespace(
        core=core, pilot=legion_pilot, teams=team_state, memory=task_memory,
        work=work, calls=[], events=[], replies={}, counts=Counter(), cooldowns=[],
        picks=[], artifact_tasks=set(), cancel_after=None, after_leaf=None,
    )
    monkeypatch.setattr(core, "_cooldown_appid", state.cooldowns.append)

    def pick(*a, **kw):
        state.picks.append(kw)
        return {"model": "offline-next-model", "appid": "offline-next-appid"}

    monkeypatch.setattr(core, "select_usable_model", pick)

    def native(key, task, qhash, timeout_s, model_override=None, appid_override=None, *,
               session_id="", user_id="", memory_dir="", tried_models=None, prior_attempts=None,
               direction="", runtime_brief="", task_facts=None, capability_policy=None,
               task_memory=None, project_context=None, execution_depth="standard",
               progress_cb=None, cancel_check=None):
        task_id = (task_facts or {}).get("task_id") or qhash
        state.counts[task_id] += 1
        state.calls.append({
            "task_id": task_id, "key": key, "model": model_override, "appid": appid_override,
            "qhash": qhash, "facts": deepcopy(task_facts), "memory": deepcopy(task_memory),
        })
        responses = state.replies.get(task_id, [done()])
        result = deepcopy(responses[min(state.counts[task_id] - 1, len(responses) - 1)])
        result.update(
            workdir=str(work), model_used=model_override,
            native_checkpoint={"kind": "fixture_session", "session_id": "native-" + task_id},
            native_facts={"turn_status": "completed" if result["status"] == "done" else "failed"},
        )
        if task_id in state.artifact_tasks:
            path = work / (task_id + ".txt")
            path.write_text("persisted " + task_id)
        if state.cancel_after == task_id:
            core.request_cancel(TEAM)
        if state.after_leaf:
            state.after_leaf(task_id)
        return result

    monkeypatch.setattr(core, "_run_harness_isolated", native)
    yield state
    core.clear_cancel(TEAM)


def plan(scene, deps, *, coordinate=False, policy="all_succeeded", failover=0):
    return {
        "team_id": TEAM,
        "captain": HARNESS if coordinate else "",
        "tasks": {key: {"task": "执行原计划节点 " + key, "assigned_harness": HARNESS,
                        "assignment_locked": True, "depends_on": list(parents),
                        "dependency_policy": policy}
                  for key, parents in deps.items()},
        "project_context": {"repo_root": str(scene.work), "workspace_root": str(scene.work)},
        "team_policy": {"continuous_coordination": coordinate, "max_failover": failover},
    }


@pytest.fixture
def captain_scene(isolated, tmp_path, monkeypatch):
    import artifact_store
    import legion_core as core
    import project_context as pc
    import team_state as ts

    monkeypatch.setattr(artifact_store, "ROOT", tmp_path / "LEGION_ARTIFACT_STORE_DIR")
    team, owner, sid = "__ut_captain_mail", "__ut_mail_owner", "__ut_mail_session"
    work = tmp_path / "work"
    work.mkdir()
    goal = "保留原目标，依据本单事实决定下一步"
    spec = {"captain": "cc", "tasks": {"build": {"task": "继续原作品", "assigned_harness": "cc"}},
            "project_context": {"repo_root": str(work), "workspace_root": str(work)}}
    pc.initialize(team, goal, spec, mode="orchestrate", user_id=owner, conversation_id=sid)
    ts.ensure(team, goal, spec["tasks"], {"build": []}, captain="cc",
              user_id=owner, conversation_id=sid)
    monkeypatch.setattr(core, "_harness_keys", lambda: {"cc"})
    monkeypatch.setattr(core, "_cost_class", lambda *a, **k: "light")

    class Lease:
        wait_seconds = 0.0

        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(core, "_GatewayLease", Lease)
    calls = []
    capture_path = tmp_path / "captured-run-one-input.json"
    proposal = {"action": "complete", "reason": "离线固定提案，不代表真实完成",
                "task_updates": {}, "messages": [], "next_plan": {}}

    def capture(key, prompt, **kwargs):
        calls.append({"key": key, "prompt": prompt,
                      "task_facts": deepcopy(kwargs.get("task_facts")),
                      "runtime_brief": kwargs.get("runtime_brief")})
        capture_path.write_text(json.dumps(calls[-1], ensure_ascii=False, indent=2))
        return {"status": "done", "final": "离线叶子",
                "orchestration_request": {"coordinate": deepcopy(proposal)}}

    import json
    monkeypatch.setattr(core, "run_one", capture)

    def run():
        return core._run_captain_coordination(
            goal, spec, team, {}, ["build"], phase="wave_complete", sequence=1,
            run_facts={"pending_task_ids": ["build"]}, user_id=owner, conversation_id=sid)

    return SimpleNamespace(core=core, pc=pc, ts=ts, store=artifact_store, team=team,
                           owner=owner, sid=sid, calls=calls, proposal=proposal, run=run,
                           capture_path=capture_path)
