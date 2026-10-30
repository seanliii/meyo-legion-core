#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
from pathlib import Path

import omnigent_registry_bridge as bridge


def test_sync_projects_live_registry_to_valid_agent_bundles(tmp_path, monkeypatch):
    rows = [
        {"key": "cc", "display_name": "Claude Code", "available": True},
        {"key": "future-agent", "display_name": "Future Agent", "available": False},
    ]
    monkeypatch.setattr(bridge.orchestrator, "list_harnesses", lambda: rows)

    result = bridge.sync(tmp_path)

    assert [item["name"] for item in result["agents"]] == ["meyo-cc", "meyo-future-agent"]
    cc = (tmp_path / "meyo-cc" / "config.yaml").read_text(encoding="utf-8")
    assert "MEYO_MEMBER_KEY: cc" in cc
    assert f'PYTHONPATH: "{bridge.HERE}"' in cc
    assert "${MEYO_ORCHESTRATOR_ROOT}" not in cc
    assert "run_assigned_harness" in cc
    assert "harness: claude-native" in cc
    assert "model: gpt-5.6-sol" not in cc
    assert "harness: openai-agents" not in cc
    assert (tmp_path / "manifest.json").is_file()


def test_server_agent_args_include_captain_and_dynamic_members(tmp_path):
    for name in ("meyo-cc", "meyo-codex"):
        path = tmp_path / name
        path.mkdir()
        (path / "config.yaml").write_text("name: x\n", encoding="utf-8")

    args = bridge.server_agent_args(tmp_path)

    assert args[:2] == ["--agent", str(bridge.HERE / "agents" / "captain")]
    assert args[2:] == [
        "--agent", str(tmp_path / "meyo-cc"),
        "--agent", str(tmp_path / "meyo-codex"),
    ]
    env_value = bridge.builtin_agent_env(tmp_path)
    assert env_value.split(os.pathsep) == [
        str(bridge.HERE / "agents" / "captain"),
        str(tmp_path / "meyo-cc"),
        str(tmp_path / "meyo-codex"),
    ]


def test_bundle_fingerprint_changes_with_agent_content(tmp_path):
    agent = tmp_path / "meyo-cc"
    agent.mkdir()
    config = agent / "config.yaml"
    config.write_text("name: meyo-cc\nexecutor:\n  config:\n    harness: claude-native\n")
    first = bridge.bundle_fingerprint(tmp_path, include_captain=False)
    config.write_text("name: meyo-cc\ndescription: updated\nexecutor:\n  config:\n    harness: claude-native\n")
    second = bridge.bundle_fingerprint(tmp_path, include_captain=False)
    assert first != second
