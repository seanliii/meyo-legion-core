# -*- coding: utf-8 -*-
"""Captain 两条运行路径的就绪契约测试。"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import check_captain_runtime as C


def test_native_captain_ready():
    r = C.readiness()
    assert r["captain_native"]["ready"], r
    assert r["captain_native"]["bundle"]["valid"]
    assert r["captain_native"]["bundle"]["harness"] == "claude-native"


def test_sdk_bundle_valid_and_dependency_honest():
    r = C.readiness()
    assert r["captain_sdk"]["bundle"]["valid"]
    assert r["captain_sdk"]["bundle"]["harness"] == "claude-sdk"
    if not r["deps"]["claude_agent_sdk"]:
        assert not r["captain_sdk"]["ready"]
        assert "93MB" in r["captain_sdk"]["blocker"]
