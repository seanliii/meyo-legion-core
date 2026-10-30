"""移动报告只读本实例账本；不访问手机、模型或个人旧数据。"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess

import pytest


SERVICE = Path(__file__).resolve().parents[1]
ITEM_KEYS = [
    "confirmation-resume", "trusted-app-lifecycle", "alarm", "files-gallery",
    "osmand-route", "amap-route", "communication", "shopping", "food", "content",
    "hardware",
]
ROW_FIELDS = {
    "key", "title", "status", "facts", "evidence", "reason", "needs_user",
    "safety_boundary_respected",
}


@pytest.fixture(autouse=True)
def isolated_instance(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SERVICE))
    for name in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "LEGION_PROC_REGISTRY_DIR"):
        directory = tmp_path / name
        directory.mkdir()
        monkeypatch.setenv(name, str(directory))

    def offline(*_args, **_kwargs):
        raise AssertionError("报告不能访问网络、模型或启动设备进程")

    monkeypatch.setattr(socket.socket, "connect", offline)
    monkeypatch.setattr(subprocess, "Popen", offline)
    report = importlib.import_module("mobile_goal_report")
    ledger = importlib.import_module("mobile_tasks")
    apps = importlib.import_module("mobile_apps")
    device = importlib.import_module("mobile_device")
    # 即使软件已装、设备可用，也不能当作用户授权或业务操作的证据。
    monkeypatch.setattr(apps, "inventory", lambda: {"apps": [
        {"package": "net.osmand.plus"}, {"package": "com.autonavi.minimap"},
    ]})
    monkeypatch.setattr(device, "status", lambda **_kwargs: {"ready": True})
    read_artifact = report._artifact

    def isolated_artifact(path):
        if not Path(path).is_relative_to(tmp_path):
            raise AssertionError("证据只能读取本测试隔离目录，不能访问个人旧数据")
        return read_artifact(path)

    monkeypatch.setattr(report, "_artifact", isolated_artifact)
    return report, ledger


def _record_task(ledger, task_id, *, uid="owner", state="partial",
                 verification=None, artifacts=()):
    assert ledger.create("离线报告用例", uid=uid, task_id=task_id)["ok"]
    started = ledger.start_attempt(task_id, runner="offline-ledger-fixture")
    assert started["ok"]
    result = {"ok": True}
    if verification is not None:
        result["verification"] = verification
    finished = ledger.finish_attempt(
        task_id, started["attempt"]["id"], state=state, result=result,
        artifacts=[str(path) for path in artifacts],
    )
    assert finished["ok"]
    return finished["task"]


@pytest.mark.parametrize("explicit_output", [False, True])
def test_empty_instance_keeps_schema_uid_output_and_has_no_personal_claims(
    isolated_instance, tmp_path, explicit_output,
):
    report, _ = isolated_instance
    options = {"uid": "owner"}
    target = Path(os.environ["MEYO_LEGION_DIR"]) / "mobile/reports/overall-goal.json"
    if explicit_output:
        target = tmp_path / "chosen/subdirectory/report.json"
        options["output"] = str(target)
    result = report.build(**options)

    assert set(result) == {
        "schema", "generated_at", "uid", "counts", "items", "stage_complete",
        "overall_complete", "output",
    }
    assert result["schema"] == "meyo.mobile.goal-report.v1"
    assert result["uid"] == "owner"
    assert isinstance(result["generated_at"], (int, float))
    assert result["output"] == str(target)
    assert [row["key"] for row in result["items"]] == ITEM_KEYS
    assert result["counts"] == {"blocked": 0, "not_run": 11, "partial": 0, "pass": 0}
    assert result["stage_complete"] is False
    assert result["overall_complete"] is False
    for row in result["items"]:
        assert set(row) == ROW_FIELDS
        assert row["status"] == "not_run"
        assert row["facts"] == {} and row["evidence"] == []
    encoded = json.dumps(result)
    assert "approved_by_user" not in encoded
    assert "notification_post_verified" not in encoded
    # 沿用旧格式：磁盘文件不含只在返回值中附加的 output 字段。
    assert json.loads(target.read_text()) == {
        key: value for key, value in result.items() if key != "output"
    }


def test_build_does_not_probe_installed_apps_or_device(isolated_instance, monkeypatch):
    report, _ = isolated_instance

    def no_probe(*_args, **_kwargs):
        raise AssertionError("报告只读账本，不允许查询安装清单或设备探活")

    monkeypatch.setattr(importlib.import_module("mobile_apps"), "inventory", no_probe)
    monkeypatch.setattr(importlib.import_module("mobile_device"), "status", no_probe)
    assert report.build()["counts"]["not_run"] == 11


@pytest.mark.parametrize("key", ["osmand-route", "amap-route", "hardware"])
def test_explicit_task_mapping_preserves_real_partial_facts_and_evidence(
    isolated_instance, key,
):
    report, ledger = isolated_instance
    task_id = "portable-" + key
    evidence = Path(os.environ["MEYO_LEGION_DIR"]) / "mobile/observations" / (key + ".txt")
    evidence.parent.mkdir(parents=True)
    content = b"offline observed result\n"
    evidence.write_bytes(content)
    facts = {
        "observed_action": key, "privacy_decision": "denied",
        "notification_post_verified": False, "route_visible": False,
    }
    task = _record_task(
        ledger, task_id, verification={"ok": False, "facts": facts}, artifacts=[evidence],
    )
    ledger_path = evidence.parents[1] / "tasks" / task_id / "task.json"
    before = ledger_path.read_bytes()
    mapping = {key: task_id}
    result = report.build(uid="owner", task_ids=mapping)
    row = next(item for item in result["items"] if item["key"] == key)

    assert row["status"] == "partial"
    assert row["facts"] == {
        **facts, "task_id": task_id, "state": "partial", "attempts": 1,
        "events": len(task["events"]), "confirmations": 0, "release_ready": False,
    }
    assert row["evidence"] == [{
        "path": str(evidence), "exists": True, "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }]
    assert result["counts"] == {"blocked": 0, "not_run": 10, "partial": 1, "pass": 0}
    assert ledger_path.read_bytes() == before
    assert mapping == {key: task_id}


@pytest.mark.parametrize("key", ["osmand-route", "amap-route", "hardware"])
def test_installed_or_ready_task_without_business_verification_is_not_pass(
    isolated_instance, key,
):
    report, ledger = isolated_instance
    task_id = "readiness-only-" + key
    _record_task(
        ledger, task_id, state="done",
        verification={"facts": {"installed": True, "android_device_ready": True}},
    )
    result = report.build(uid="owner", task_ids={key: task_id})
    row = next(item for item in result["items"] if item["key"] == key)
    assert row["status"] == "partial"
    assert row["facts"]["release_ready"] is False
    assert "privacy_decision" not in row["facts"]
    assert "notification_post_verified" not in row["facts"]


@pytest.mark.parametrize("key", ["osmand-route", "amap-route", "hardware"])
def test_completed_task_uses_existing_business_verifier_result(isolated_instance, key):
    report, ledger = isolated_instance
    task_id = "verified-" + key
    facts = {"observed_action": key, "route_visible": True, "notification_post_verified": False}
    _record_task(ledger, task_id, state="done", verification={"ok": True, "facts": facts})
    result = report.build(uid="owner", task_ids={key: task_id})
    row = next(item for item in result["items"] if item["key"] == key)
    assert row["status"] == "pass"
    assert all(row["facts"][name] == value for name, value in facts.items())
    assert row["facts"]["release_ready"] is True


def test_task_owned_by_another_uid_is_not_used(isolated_instance):
    report, ledger = isolated_instance
    task_id = "other-owner-task"
    _record_task(
        ledger, task_id, uid="other-owner", state="done",
        verification={"ok": True, "facts": {"private_note": "other-owner-fixture"}},
    )
    result = report.build(uid="owner", task_ids={"amap-route": task_id})
    row = next(item for item in result["items"] if item["key"] == "amap-route")
    assert row["status"] == "not_run" and row["facts"] == {} and row["evidence"] == []
    assert "other-owner-fixture" not in json.dumps(result)


@pytest.mark.parametrize("override_ids", [False, True])
def test_legacy_stage_rows_keep_default_ids_required_facts_and_completion(
    isolated_instance, override_ids,
):
    report, ledger = isolated_instance
    cases = [
        ("confirmation-resume", "mobile-confirm-resume-proof",
         {"privacy_decision_observed": True, "screen": True}),
        ("trusted-app-lifecycle", "mobile-fdroid-klooni-lifecycle",
         {"installed": True, "launch_verified": True, "uninstall_verified": True,
          "rollback_verified": True}),
        ("alarm", "mobile-real-alarm-0735",
         {"system_alarm_visible": True, "ui_alarm_visible": True, "persisted_after_reboot": True}),
        ("files-gallery", "mobile-real-files-gallery",
         {"file_exists": True, "media_store_visible": True, "gallery_focus_visible": True}),
    ]
    mapping = {}
    for key, default_id, facts in cases:
        task_id = "selected-" + key if override_ids else default_id
        _record_task(ledger, task_id, state="done", verification={"ok": True, "facts": facts})
        mapping[key] = task_id
    options = {"task_ids": mapping} if override_ids else {}
    result = report.build(uid="owner", **options)
    assert [row["key"] for row in result["items"][:4]] == ITEM_KEYS[:4]
    assert all(row["status"] == "pass" for row in result["items"][:4])
    assert result["stage_complete"] is True and result["overall_complete"] is False
    assert result["counts"] == {"blocked": 0, "not_run": 7, "partial": 0, "pass": 4}


def test_valid_ledger_id_does_not_require_an_optional_detail_index(isolated_instance):
    report, ledger = isolated_instance
    task_id = "portable-" + "a" * 60  # mobile_tasks 允许的长度超过可选详情索引的 60 字符。
    _record_task(ledger, task_id, verification={"ok": False, "facts": {"observed": True}})
    result = report.build(uid="owner", task_ids={"hardware": task_id})
    row = next(item for item in result["items"] if item["key"] == "hardware")
    assert row["status"] == "partial"
    assert row["facts"]["observed"] is True and row["facts"]["task_id"] == task_id


def test_source_has_no_embedded_personal_home_paths():
    source = (SERVICE / "mobile_goal_report.py").read_bytes()
    assert re.search(rb"/(?:Users|home)/", source) is None, "公开报告源码仍含个人 HOME 路径"
