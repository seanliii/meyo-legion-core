"""继续对话应保留历史文件，不把交付快照或路径引用冒充写权限。"""
import hashlib
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

SERVICE = Path(__file__).resolve().parents[1]
SNAPSHOT = "a" * 64


@pytest.fixture(autouse=True)
def case(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith(("MEYO_", "LEGION_", "AIGC_", "OPENAI_", "ANTHROPIC_", "CODEX_")):
            monkeypatch.delenv(key)
    for name, leaf in (("HOME", "home"), ("MEYO_LEGION_DIR", "data"),
                       ("LEGION_HOME_DIR", "legion"), ("LEGION_SESSIONS_DIR", "sessions")):
        folder = tmp_path / leaf
        folder.mkdir()
        monkeypatch.setenv(name, str(folder))
    monkeypatch.syspath_prepend(str(SERVICE))
    spec = importlib.util.spec_from_file_location("artifact_role_sessions", SERVICE / "sessions.py")
    sessions = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sessions)
    original = tmp_path / "project/figure.html"
    exported = tmp_path / "data/outputs/exports/team" / SNAPSHOT / "figure.html"
    for path in (original, exported):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("<main>fixture</main>")

    def save(artifacts):
        sessions.save_turn("__ut_artifact_context", "继续原项目", {
            "status": "done", "team_id": "__ut_team",
            "results": [{"key": "maker", "status": "done", "final": "已有结果",
                         "workdir": str(original.parent), "artifacts": artifacts}],
        }, user_id="owner")

    def context(**kwargs):
        return sessions.conversation_context("__ut_artifact_context", user_id="owner", **kwargs)

    return SimpleNamespace(sessions=sessions, original=original, exported=exported,
                           save=save, context=context)


def test_exported_snapshot_is_not_presented_as_editable_source(case):
    case.save([{"path": str(case.exported), "kind": "html", "export_snapshot_id": SNAPSHOT}])
    text = case.context()
    assert str(case.exported) in text
    assert "已交付快照引用" in text
    assert "续作使用当前授权工程" in text
    assert "可直接读取/修改" not in text


def test_ordinary_source_reference_stays_available_without_invented_permission(case):
    case.save([{"path": str(case.original), "kind": "html"}])
    text = case.context()
    assert str(case.original) in text
    assert "历史文件引用" in text
    assert "读改须依当前授权" in text
    assert "已交付快照引用" not in text
    assert "只读文件" not in text


def test_mixed_references_preserve_both_roles_and_do_not_mutate_files_or_record(case):
    case.save([
        {"path": str(case.original), "kind": "html"},
        {"path": str(case.exported), "kind": "html", "export_snapshot_id": SNAPSHOT},
    ])
    paths = [case.original, case.exported,
             Path(case.sessions._path("__ut_artifact_context"))]
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    text = case.context()
    assert str(case.original) in text and str(case.exported) in text
    snapshot_line = next(line for line in text.splitlines() if "已交付快照引用" in line)
    ordinary_line = next(line for line in text.splitlines() if "历史文件引用" in line)
    assert str(case.original) not in snapshot_line
    assert str(case.exported) not in ordinary_line
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths} == before


@pytest.mark.parametrize("marker", [None, "", 123, "not-a-snapshot-hash"])
def test_unknown_or_legacy_role_is_not_assumed_to_be_writable_or_exported(case, marker):
    case.save([{"url": "https://example.test/report.pdf", "kind": "pdf",
                "export_snapshot_id": marker}])
    text = case.context()
    assert "https://example.test/report.pdf" in text
    assert "历史文件引用" in text
    assert "已交付快照引用" not in text
    assert "可直接读取/修改" not in text


def test_context_still_obeys_owner_boundary(case):
    case.save([{"path": str(case.exported), "kind": "html", "export_snapshot_id": SNAPSHOT}])
    assert case.sessions.conversation_context("__ut_artifact_context", user_id="another-owner") == ""
