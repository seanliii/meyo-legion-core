"""Explicit local router declarations must reach real consumers without secrets."""
import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

SERVICE = Path(__file__).resolve().parents[1]


@pytest.fixture
def case(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith(("MEYO_", "LEGION_", "AIGC_", "OPENAI_")):
            monkeypatch.delenv(key)
    for key, leaf in (("HOME", "home"), ("MEYO_LEGION_DIR", "data"), ("LEGION_HOME_DIR", "legion")):
        path = tmp_path / leaf
        path.mkdir()
        monkeypatch.setenv(key, str(path))
    monkeypatch.syspath_prepend(str(SERVICE))
    mr = importlib.import_module("model_registry")
    core = importlib.import_module("legion_core")
    cli = importlib.import_module("media_model_cli")
    catalog = {
        "_meta": {"last_verified": "2026-09-02"},
        "programming": {"description": "编程与规划", "candidates": [
            {"model": "gpt-fixture", "rank": 1, "status": "PASS", "appids": ["fixture-key-a"]},
            {"model": "fixture-second", "rank": 2, "status": "PASS", "appids": ["fixture-key-b"]},
        ]},
        "vision_understanding": {"description": "识图", "candidates": [
            {"model": "aws.claude-opus-fixture", "rank": 1, "status": "PASS",
             "appids": ["fixture-vision-key"]}]},
    }
    source = tmp_path / "authorized/models.json"
    source.parent.mkdir()
    source.write_text(json.dumps(catalog))
    monkeypatch.setenv("MEYO_MODEL_CATALOG_PATH", str(source))
    legacy = tmp_path / "home/.claude/skills/aigc-model-router/models.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"programming": {"candidates": [
        {"model": "legacy-placeholder", "appids": ["legacy-key"]}]}}))
    monkeypatch.setattr(mr, "_MODELS_PATH", str(legacy))
    return SimpleNamespace(core=core, mr=mr, cli=cli, patch=monkeypatch, root=tmp_path,
                           source=source, catalog=catalog)


def test_core_and_registry_use_the_same_explicit_catalog(case):
    assert case.core._load_model_router() == case.mr._load_models() == case.catalog


def test_explicit_catalog_does_not_silently_retire_models_by_name(case):
    assert [row["model"] for row in case.mr._catalog_models("programming")["candidates"]] == [
        "gpt-fixture", "fixture-second"]
    assert case.mr._catalog_models("vision_understanding")["candidates"][0]["model"] == "aws.claude-opus-fixture"


def test_explicit_matrix_is_shared_and_cannot_fall_back_to_legacy_cache(case):
    matrix = {"by_model": {"fixture-second": {"pass_appids": ["fixture-key-b"]}}}
    path = case.root / "authorized/matrix.json"
    path.write_text(json.dumps(matrix))
    case.patch.setenv("MEYO_MODEL_MATRIX_PATH", str(path))
    assert case.mr._load_appid_matrix() == matrix
    assert case.core._load_appid_matrix() == matrix
    path.unlink()
    with pytest.raises(case.mr.ModelCatalogError):
        case.core._load_appid_matrix()


def test_cli_can_select_an_explicit_declared_model_without_borrowing_its_key(case):
    assert case.cli._select("programming", model="fixture-second") == ("fixture-second", "fixture-key-b")
    with pytest.raises(case.cli.MediaError):
        case.cli._select("programming", model="not-authorized")
    with pytest.raises(case.cli.MediaError):
        case.cli._select("programming", model="aws.claude-opus-fixture")


def test_cli_model_index_reports_declarations_not_live_success_or_credentials(case, capsys):
    assert case.cli.main(["models", "--domain", "programming"]) == 0
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["source_last_verified"] == "2026-09-02"
    assert data["live_verified"] is False
    assert [row["model"] for row in data["candidates"]] == ["gpt-fixture", "fixture-second"]
    assert "fixture-key" not in output and "appids" not in output


def test_cli_chat_json_retains_selected_model_and_actual_usage(case, monkeypatch, capsys):
    calls = []

    def request(direction, endpoint, payload, timeout_s, *, model=None):
        calls.append({"direction": direction, "endpoint": endpoint, "payload": payload, "model": model})
        return {"model": "fixture-second", "usage": {"prompt_tokens": 2, "completion_tokens": 3},
                "choices": [{"finish_reason": "stop", "message": {"content": "fixture answer"}}]}

    monkeypatch.setattr(case.cli, "_request", request)
    assert case.cli.main(["chat", "fixture prompt", "--domain", "programming",
                          "--model", "fixture-second", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert calls[0]["model"] == "fixture-second"
    assert data["configured_model"] == "fixture-second"
    assert data["reported_model"] == "fixture-second"
    assert data["usage"]["completion_tokens"] == 3
    assert data["answer"] == "fixture answer"


def test_explicit_catalog_error_does_not_become_legacy_model_success(case):
    case.source.unlink()
    with pytest.raises(case.mr.ModelCatalogError):
        case.core._load_model_router()


def test_all_exhausted_model_keys_do_not_borrow_unrelated_trusted_key(case):
    value = case.catalog
    value["programming"]["candidates"] = [value["programming"]["candidates"][1]]
    value["quota_exhausted_appids"] = {"appids": ["fixture-key-b"]}
    value["trusted_appids"] = {"list": [{"appid": "unrelated-key"}]}
    case.source.write_text(json.dumps(value))
    assert case.mr._appids_of("fixture-second") == []
    result = case.core.select_usable_model("programming")
    assert result.get("appid") is None


def test_default_catalog_tracks_runtime_home_instead_of_import_home(case):
    case.patch.delenv("MEYO_MODEL_CATALOG_PATH")
    imported_path = case.mr._MODELS_PATH
    # Restore the actual unmodified default selector rather than a test override.
    case.patch.setattr(case.mr, "_MODELS_PATH", getattr(case.mr, "_IMPORTED_MODELS_PATH", imported_path))
    home = case.root / "another-home"
    source = home / ".claude/skills/aigc-model-router/models.json"
    source.parent.mkdir(parents=True)
    value = {"programming": {"candidates": [{"model": "runtime-home", "appids": ["new-home-key"]}]}}
    source.write_text(json.dumps(value))
    case.patch.setenv("HOME", str(home))
    assert case.core._load_model_router() == value
