"""自主 loop 收尾/诊断/世界标准三处增强的回归锁。

覆盖：
1. `_autocommit_delivery` 只在「真 git 仓库 + 有真实交付 + 有改动」时提交；
2. `dispatch_persistent` 首轮在缺世界参照时注入研究 context；
3. 每轮产出结构化 diagnostics 并随结果返回。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("LEGION_HOME_DIR", "/tmp/legion_test_home_loop_v2")
os.environ.setdefault("MEYO_LEGION_DIR", "/tmp/legion_test_dir_loop_v2")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGION_HOME_DIR", str(tmp_path / "home"))
    monkeypatch.setenv("MEYO_LEGION_DIR", str(tmp_path / "legion"))
    yield


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "loop@test.local")
    _git(path, "config", "user.name", "loop test")
    return path


def test_autocommit_commits_delivered_changes(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    import legion_core

    repo = _init_repo(tmp_path / "repo")
    (repo / "a.txt").write_text("delivered", encoding="utf-8")
    plan = {"project_context": {"repo_root": str(repo)}}
    out = {"results": [{"status": "done", "artifacts": [{"path": "a.txt"}]}]}

    first = legion_core._autocommit_delivery(plan, "team-1", 2, out)
    assert first["status"] == "committed", first
    assert first["files"] == 1 and first["commit"]

    second = legion_core._autocommit_delivery(plan, "team-1", 2, out)
    assert second["status"] == "clean", second


def test_autocommit_does_not_sweep_unrelated_changes(tmp_path, monkeypatch):
    """工作区里的无关文件（CLI 缓存/日志）不得被卷进交付 commit。"""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    import legion_core

    repo = _init_repo(tmp_path / "repo")
    (repo / "product.txt").write_text("real work", encoding="utf-8")
    (repo / "cli-cache.log").write_text("runtime junk", encoding="utf-8")
    plan = {"project_context": {"repo_root": str(repo)}}
    out = {"results": [{"status": "done", "artifacts": [{"path": "product.txt"}]}]}

    result = legion_core._autocommit_delivery(plan, "team-1", 1, out)
    assert result["status"] == "committed", result
    assert result["files"] == 1
    shown = subprocess.run(["git", "-C", str(repo), "show", "--name-only", "--pretty=format:"],
                           capture_output=True, text=True).stdout
    assert "product.txt" in shown and "cli-cache.log" not in shown
    left = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                          capture_output=True, text=True).stdout
    assert "cli-cache.log" in left, "unrelated file must stay uncommitted"


def test_autocommit_skips_without_artifacts(tmp_path, monkeypatch):
    """只有文字没有产物时不提交（不能把无关工作区改动当成交付）。"""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    import legion_core

    repo = _init_repo(tmp_path / "repo")
    (repo / "junk.log").write_text("x", encoding="utf-8")
    plan = {"project_context": {"repo_root": str(repo)}}
    out = {"results": [{"status": "done"}]}

    result = legion_core._autocommit_delivery(plan, "team-1", 1, out)
    assert result["status"] == "skipped"
    assert result["reason"] == "no_artifacts"
    left = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                          capture_output=True, text=True).stdout
    assert "junk.log" in left


def test_autocommit_skips_when_nothing_delivered(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    import legion_core

    repo = _init_repo(tmp_path / "repo")
    (repo / "a.txt").write_text("draft", encoding="utf-8")
    plan = {"project_context": {"repo_root": str(repo)}}

    result = legion_core._autocommit_delivery(
        plan, "team-1", 1, {"results": [{"status": "error"}]})
    assert result["status"] == "skipped"
    assert result["reason"] == "nothing_delivered"


def test_autocommit_skips_without_repo_root(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    import legion_core

    result = legion_core._autocommit_delivery({}, "team-1", 1,
                                              {"results": [{"status": "done"}]})
    assert result == {"status": "skipped", "reason": "no_repo_root"}


def test_autocommit_skips_non_git_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    import legion_core

    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.txt").write_text("x", encoding="utf-8")
    plan = {"project_context": {"workspace_root": str(plain)}}

    result = legion_core._autocommit_delivery(plan, "team-1", 1,
                                              {"results": [{"status": "done",
                                                            "artifacts": [{"path": "a.txt"}]}]})
    assert result["status"] == "skipped"
    assert result["reason"] == "not_a_git_repo"


def test_unadjudicated_candidates_remain_available_to_persistent_continuation(monkeypatch):
    """An empty selected final must not erase real candidate evidence on resume."""
    from copy import deepcopy
    import legion_core
    import legion_pilot

    rounds = []
    verdicts = iter([
        {"action": "continue", "kind": "fixture_continue", "next_action": "continue original work"},
        {"action": "stop", "kind": "fixture_stop"},
    ])
    rows = [
        {"key": "alpha", "task_id": "alpha", "status": "done", "final": "candidate alpha evidence"},
        {"key": "beta", "task_id": "beta", "status": "done", "final": "candidate beta evidence"},
    ]
    def fake_dispatch(question, plan=None, cb=None, **kwargs):
        rounds.append(deepcopy(plan))
        return {"execution_mode": "pk", "results": deepcopy(rows), "winner": "",
                "selection": {"source": "unadjudicated_candidates"},
                "delivered_count": 2, "total": 2, "outcome": {"status": "completed"}}

    monkeypatch.setattr(legion_core, "dispatch", fake_dispatch)
    monkeypatch.setattr(legion_pilot, "evaluate", lambda *a, **kw: next(verdicts))
    monkeypatch.setattr(legion_core, "_collect_benchmark_refs", lambda *a, **kw: [])
    monkeypatch.setattr(legion_core, "_autocommit_delivery", lambda *a, **kw: {"status": "skipped"})
    monkeypatch.setattr(legion_core, "_plan_empty_continuation", lambda q, p, rp, tid, g, **kw:
                        ({**p, "tasks": {"followup": {"task": "continue"}}}, None))
    out = legion_core.dispatch_persistent(
        "preserve distinct candidate evidence", plan={"tasks": {"alpha": {"task": "original"}}},
        mode="pk", max_rounds=2, qhash="__ut_unadjudicated_continue")
    assert len(rounds) == 2
    context = rounds[1].get("conversation_context") or ""
    assert "candidate alpha evidence" in context
    assert "candidate beta evidence" in context
    assert "未裁决" in context
    assert out["selection"]["source"] == "unadjudicated_candidates"
    assert legion_core._winner_final(out) == ""
    assert [row["final"] for row in out["results"]] == [row["final"] for row in rows]


def test_unadjudicated_context_is_bounded_and_does_not_include_supporting_or_failed(monkeypatch):
    import legion_core

    rows = [{"task_id": f"candidate-{index}", "status": "done", "final": str(index) * 2000}
            for index in range(14)]
    rows.extend([
        {"task_id": "private-support", "supporting_context": True, "status": "done", "final": "not a candidate"},
        {"task_id": "review", "verification_role": "critic", "status": "done", "final": "not a candidate"},
        {"task_id": "failed", "status": "error", "final": "not a candidate"},
    ])
    snapshot = legion_core._persistent_candidate_context({"results": rows})
    assert "未裁决" in snapshot and "非最终结论" in snapshot
    assert "candidate-0:" in snapshot and "candidate-11:" in snapshot
    assert "candidate-12:" not in snapshot and "另有 2 条候选" in snapshot
    assert "正文已截断" in snapshot
    assert "not a candidate" not in snapshot
    assert len(snapshot) < 3000


def test_persistent_loop_injects_world_standard_and_diagnostics(monkeypatch):
    """首轮缺参照时注入研究 context；结果带 diagnostics；无交付不提交。"""
    import legion_core
    import legion_pilot

    seen_questions: list[str] = []

    def fake_dispatch(question, plan=None, cb=None, **kwargs):
        seen_questions.append(question)
        return {
            "results": [{"key": "k", "status": "done", "final": "做完了"}],
            "delivered_count": 1,
            "total": 1,
            "outcome": {"status": "completed"},
        }

    monkeypatch.setattr(legion_core, "dispatch", fake_dispatch)
    monkeypatch.setattr(legion_pilot, "evaluate",
                        lambda *a, **k: {"action": "stop", "kind": "done"})
    monkeypatch.setattr(legion_core, "_winner_final", lambda out: "做完了")
    monkeypatch.setattr(legion_core, "_collect_benchmark_refs", lambda *a, **k: [])

    out = legion_core.dispatch_persistent(
        "把项目优化到世界级", plan={"tasks": {"t1": {"task": "优化"}}},
        max_rounds=2, qhash="qt")

    assert seen_questions, "dispatch should run at least once"
    assert "世界标准研究" in seen_questions[0]
    assert "quality_context.sources" in seen_questions[0]
    diag = out.get("persistent_diagnostics") or []
    assert diag and diag[0]["round"] == 1
    assert diag[0]["references_recorded"] is False
    assert "autocommit" in out


def test_persistent_loop_continues_after_failure_then_stops(monkeypatch):
    """第1轮未交付→pilot 判继续→第2轮交付→停止；诊断覆盖两轮。"""
    import legion_core
    import legion_pilot

    rounds_seen: list[str] = []
    verdicts = iter([
        {"action": "continue", "kind": "not_done",
         "next_action": "上一轮没交付，换方法把产物真写出来"},
        {"action": "stop", "kind": "done"},
    ])

    def fake_dispatch(question, plan=None, cb=None, **kwargs):
        rounds_seen.append(question)
        if len(rounds_seen) == 1:
            return {"results": [{"key": "k", "status": "error", "fail_kind": "idle_stall"}],
                    "delivered_count": 0, "total": 1, "all_failed": True}
        return {"results": [{"key": "k", "status": "done", "final": "产物已交付"}],
                "delivered_count": 1, "total": 1,
                "outcome": {"status": "completed"}}

    monkeypatch.setattr(legion_core, "dispatch", fake_dispatch)
    monkeypatch.setattr(legion_pilot, "evaluate", lambda *a, **k: next(verdicts))
    monkeypatch.setattr(legion_core, "_winner_final", lambda out: "产物已交付")
    monkeypatch.setattr(legion_core, "_collect_benchmark_refs", lambda *a, **k: [])
    monkeypatch.setattr(legion_core, "_plan_empty_continuation",
                        lambda q, p, rp, tid, g, **kw: ({**p, "tasks": {"t1": {"task": "优化"}}}, None))

    out = legion_core.dispatch_persistent(
        "把项目优化到世界级", plan={"tasks": {"t1": {"task": "优化"}}},
        max_rounds=3, qhash="qt2")

    assert len(rounds_seen) == 2, rounds_seen
    assert "世界标准研究" in rounds_seen[0]
    assert "续跑指引" in rounds_seen[1]
    diag = out.get("persistent_diagnostics") or []
    assert [row["round"] for row in diag] == [1, 2]
    assert diag[0]["delivered_count"] == 0 and diag[1]["delivered_count"] == 1
    assert out["pilot"]["action"] == "stop"
    assert out["persistent_rounds"] == 2
