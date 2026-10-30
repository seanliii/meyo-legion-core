"""Incomplete native accounting remains explicitly partial at every projection."""
import importlib
from pathlib import Path

import pytest


@pytest.fixture
def telemetry(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    monkeypatch.setenv("MEYO_LEGION_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LEGION_HOME_DIR", str(tmp_path / "home"))
    return importlib.import_module("token_telemetry")


def test_unknown_attempt_is_not_zero_or_a_full_total(telemetry):
    result = telemetry.aggregate([
        {"available": False},
        {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
    ])
    assert result["available"] and result["total_tokens"] == 10
    assert result["complete"] is False and result["coverage"] == "partial"
    assert result["attempts_total"] == 2 and result["attempts_unreported"] == 1
    assert "已知部分" in telemetry.summary_text(result)


def test_all_unknown_stays_unknown(telemetry):
    result = telemetry.aggregate([None, {"available": False}])
    assert result["available"] is False and result["total_tokens"] is None
    assert result["coverage"] == "unavailable" and result["complete"] is False


def test_partial_flag_survives_attach_reaggregation_and_request_summary(telemetry):
    first = telemetry.aggregate([
        {"available": False}, {"input_tokens": 7, "output_tokens": 3}])
    attached = telemetry.attach({"status": "done", "usage": first})["usage"]
    assert attached["coverage"] == "partial" and attached["attempts_unreported"] == 1
    merged = telemetry.aggregate([attached, {"input_tokens": 6, "output_tokens": 4}])
    assert merged["total_tokens"] == 20 and merged["complete"] is False
    assert merged["attempts_unreported"] == 1 and merged["attempts_total"] == 3
    summary = telemetry.summarize([{"key": "a", "usage": merged}])
    assert summary["complete"] is False and summary["coverage"] == "partial"
    assert summary["members"][0]["complete"] is False
    unknown_input = telemetry.summarize([{"key": "b", "usage": {"output_tokens": 3}}])
    assert unknown_input["input_tokens"] is None


def test_absent_input_or_output_is_not_invented_as_zero(telemetry):
    value = telemetry.normalize({"output_tokens": 3})
    assert value["input_tokens"] is None and value["total_tokens"] == 3
    assert value["complete"] is False and value["coverage"] == "partial"
    assert "输入 未上报" in telemetry.summary_text(value)
