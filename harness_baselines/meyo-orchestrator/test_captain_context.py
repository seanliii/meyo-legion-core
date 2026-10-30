import json
from pathlib import Path

import captain_context as C


def test_context_contains_runtime_facts_without_routing(monkeypatch, tmp_path):
    monkeypatch.setattr(C.orchestrator, "list_harnesses", lambda: [
        {"key": "cc", "display_name": "CC", "available": True}
    ])
    monkeypatch.setattr(C.orchestrator, "read_harness_history", lambda key, limit: [
        {"status": "ok", "elapsed": 2}
    ])
    monkeypatch.setattr(C.health, "health_snapshot", lambda: {"circuits_open": []})
    (tmp_path / "memory.json").write_text(json.dumps({"preference": "HTML"}))

    context = C.build_context(
        "做报告", acceptance=["必须HTML"], recent_events=[{"type": "artifact"}],
        memory_dir=tmp_path, toolbox={"total": 541}, permissions={"write": True},
        budgets={"seconds": 60}, delivery_contract={"required_files": ["report.html"]},
        include_histories=True,
        member_catalog=[{"key": "cc", "display_name": "CC", "available": True}],
    )

    data = context.to_dict()
    assert data["goal"] == "做报告" and data["members"][0]["recent_history"]
    assert data["long_term_memory"][0]["value"]["preference"] == "HTML"
    assert "selected_agent" not in data and "recommendation" not in data
    assert context.prompt_block().startswith("【首脑作战上下文·仅事实")
