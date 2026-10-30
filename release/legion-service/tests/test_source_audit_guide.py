"""Optional source-audit guide facts, without a service, model or old checkout."""

import importlib.util
import os
from pathlib import Path

import pytest


SERVICE_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    for variable, leaf in (
        ("HOME", "home"),
        ("MEYO_LEGION_DIR", "meyo"),
        ("LEGION_HOME_DIR", "legion"),
    ):
        directory = tmp_path / leaf
        directory.mkdir()
        monkeypatch.setenv(variable, str(directory))
    monkeypatch.syspath_prepend(str(SERVICE_DIR))


@pytest.fixture
def source_audit(tmp_path, monkeypatch, isolated_environment):
    spec = importlib.util.spec_from_file_location(
        "source_audit_guide_under_test", SERVICE_DIR / "source_audit.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "core"
    monkeypatch.setattr(
        module, "_SKILL", root / ".claude/skills/source-audit-evolve/SKILL.md"
    )
    monkeypatch.setattr(module, "_BASELINE", root / "dev/baseline.md")
    return module


def write_guide(source_audit):
    path = source_audit._SKILL
    path.parent.mkdir(parents=True)
    path.write_text("# Temporary source-audit guide\n", encoding="utf-8")
    return path


def test_missing_guide_is_null_without_a_nonexistent_read_step(source_audit):
    result = source_audit.prepare("核对已有源码", mode="audit")

    assert result["ok"] is True
    assert result["skill"] is None
    assert result.get("skill_status") == "missing"
    assert "再读取 skill" not in result["next"]
    assert "prior_art" in result["next"]
    assert "fork/need_peer/handoff" in result["next"]
    assert result["baseline"] is None


def test_regular_guide_keeps_its_real_path_and_legacy_next_step(source_audit):
    path = write_guide(source_audit)

    result = source_audit.prepare("核对已有源码")

    assert result["ok"] is True
    assert result["skill"] == str(path)
    assert result.get("skill_status") == "present"
    assert result["next"] == (
        "先调用 prior_art action 并选择 reuse/extend/replace，再读取 skill 建立待证事实矩阵；"
        "需要同伴时使用现有 fork/need_peer/handoff。"
    )


@pytest.mark.parametrize("kind", ["directory", "symlink", "dangling_symlink", "fifo"])
def test_non_regular_guide_is_not_advertised(source_audit, tmp_path, kind):
    path = source_audit._SKILL
    path.parent.mkdir(parents=True)
    if kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        target = tmp_path / "other-guide.md"
        target.write_text("# Another guide\n", encoding="utf-8")
        path.symlink_to(target)
    elif kind == "dangling_symlink":
        path.symlink_to(tmp_path / "missing-guide.md")
    else:
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO is not supported on this platform")
        os.mkfifo(path)

    result = source_audit.prepare("核对已有源码")

    assert result["ok"] is True
    assert result["skill"] is None
    assert result.get("skill_status") == "not_regular"
    assert "再读取 skill" not in result["next"]


@pytest.mark.parametrize("error", [PermissionError, OSError])
def test_uninspectable_guide_returns_a_fact_not_an_exception(
    source_audit, monkeypatch, error
):
    real_lstat = Path.lstat

    def unavailable(path):
        if path == source_audit._SKILL:
            raise error("synthetic inspection failure")
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", unavailable)

    result = source_audit.prepare("核对已有源码")

    assert result["ok"] is True
    assert result["skill"] is None
    assert result.get("skill_status") == "unavailable"
    assert "再读取 skill" not in result["next"]
    assert "synthetic inspection failure" not in str(result)


def test_removed_guide_is_rechecked_without_losing_existing_context(source_audit):
    path = write_guide(source_audit)
    args = {
        "goal": "核对已有源码",
        "scope": ["existing-component"],
        "dimensions": ["runtime"],
        "mode": "compare",
        "uid": "__ut_guide",
    }
    present = source_audit.prepare(**args)
    path.unlink()

    missing = source_audit.prepare(**args)

    assert missing["skill"] is None
    assert missing.get("skill_status") == "missing"
    assert missing["prior_art_required"] is True
    assert missing["prior_art_action"]["tool"] == "runnable:source_audit"
    assert missing["prior_art_action"]["action"] == "prior_art"
    assert missing["evidence_contract"] == [
        "source_root", "revision", "file", "line", "quote",
        "runtime_evidence", "verifier", "status",
    ]
    assert missing["source_search"]
    for key in (
        "goal", "scope", "dimensions", "mode", "uid", "prior_art_required",
        "prior_art_action", "source_search", "evidence_contract", "ecosystem_loop",
    ):
        assert missing[key] == present[key]


def test_empty_goal_keeps_existing_rejection(source_audit):
    assert source_audit.prepare("  ") == {"ok": False, "error": "goal 不能为空"}
