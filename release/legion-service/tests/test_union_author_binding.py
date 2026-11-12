import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
if str(SERVICE) not in sys.path:
    sys.path.insert(0, str(SERVICE))

import collaboration_runtime


def _bind(tasks):
    plan = {"tasks": tasks, "_planner_generated": True}
    members = {"codex", "pi", "qwen-code"}
    bound, _ = collaboration_runtime.bind_plan(plan, members, runnable=members)
    return {task_id: node.get("assigned_harness") for task_id, node in bound["tasks"].items()}


def test_locked_union_authors_keep_their_own_task():
    question = "做一个可玩的 3D 游戏"
    tasks = {key: {"task": question, "assigned_harness": key, "assignment_locked": True}
             for key in ("codex", "pi")}
    assert _bind(tasks) == {"codex": "codex", "pi": "pi"}


def test_server_union_plan_locks_each_author():
    source = (SERVICE / "server.py").read_text(encoding="utf-8")
    union = source[source.index("if _union_authors:"):source.index("elif _author_binding.get(\"bound\"):")]
    assert '"assignment_locked": True' in union
    assert '"assigned_harness": key' in union


def test_union_is_skipped_for_shared_project_workspace():
    source = (SERVICE / "server.py").read_text(encoding="utf-8")
    start = source.index("_shared_workspace = ")
    block = source[start:source.index("LEGION.auto_union_authors(_plan, _candidate_pool)", start)]
    assert "repo_root" in block and "workspace_root" in block
    assert "_shared_workspace else" in block
