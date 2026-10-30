"""Research provenance must reach execution, not become a fabricated quality verdict."""
import copy
import importlib
from pathlib import Path

import pytest


@pytest.fixture
def qc(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    for key, leaf in (("HOME", "home"), ("MEYO_LEGION_DIR", "data"), ("LEGION_HOME_DIR", "legion")):
        monkeypatch.setenv(key, str(tmp_path / leaf))
    return importlib.import_module("quality_context")


def test_empty_normalized_context_is_not_a_reference(qc):
    result = qc.benchmark_snapshot({"quality_context": qc.normalize({})})
    assert result["context_present"] is True
    assert result["reference_count"] == 0
    assert result["source_index"] == []
    assert "quality_passed" not in result


def test_nested_sources_retain_complete_links_and_declared_status(qc):
    url = "https://example.invalid/" + "long-path/" * 25 + "?item=one"
    source = {"source_id": "S1", "url": url, "fetch_status": "failed", "title": "reference"}
    plan = {"project_context": {"quality_context": {"sources": [source]}}}
    before = copy.deepcopy(plan)
    result = qc.benchmark_snapshot(plan)
    assert result["reference_count"] == 1
    assert result["source_index"][0]["url"] == url
    assert result["source_index"][0]["fetch_status"] == "failed"
    assert result["source_index"][0]["source_kind"] == "research_record"
    assert plan == before


def test_supplied_url_is_not_relabelled_as_autonomous_research(qc):
    result = qc.benchmark_snapshot({"source_ref": {"url": "https://example.invalid/given"}})
    assert result["reference_count"] == 1
    assert result["source_index"][0]["source_kind"] == "caller_supplied"
    assert result["source_index"][0]["fetch_status"] == "not_recorded"


def test_analysis_and_rendered_observations_reach_next_view_with_origins(qc):
    plan = {"project_context": {
        "quality_context": {
            "sources": [{"source_id": "R1", "url": "https://example.invalid/ref"}],
            "reference_patterns": [{
                "pattern_id": "P1", "dimension": "feedback", "source_ids": ["R1"],
                "pattern": "user sees the effect of each action",
                "adoption_note": "make candidate response visible at the action site",
            }],
        },
        "journey_observations": [{"problem": "candidate hides the effect", "version": "v1"}],
    }}
    result = qc.benchmark_snapshot(plan)
    assert result["analysis"][0]["value"]["source_ids"] == ["R1"]
    assert result["analysis"][0]["origin"] == "project_context.quality_context.reference_patterns"
    assert result["journey_observations"][0]["value"]["version"] == "v1"
    assert result["analysis_is_model_authored"] is True
    assert "score" not in result and "passed" not in result


def test_generated_artifact_is_not_a_world_reference(qc):
    plan = {"artifacts": [{"url": "https://our-own-output.invalid/index.html"}]}
    assert qc.benchmark_snapshot(plan)["reference_count"] == 0


def test_sparse_top_level_does_not_hide_latest_project_research(qc):
    result = qc.benchmark_snapshot({
        "quality_context": {"revision": 1, "sources": []},
        "project_context": {"quality_context": {"revision": 2, "sources": [
            {"url": "https://example.invalid/latest", "fetch_status": "retrieved"}]}}
    })
    assert result["reference_count"] == 1
    assert result["source_index"][0]["url"].endswith("/latest")
