"""Onboarding reports must distinguish absent distribution checks from failures.

Slow/member/provider boundaries are test doubles. The report branch and any
temporary mode-check script execute for real; this is not member acceptance.
"""

import importlib.util
import json
from pathlib import Path
import sys
import tempfile

import pytest


SERVICE_DIR = Path(__file__).resolve().parents[1]
KEY = "__ut_distribution_member"
MODES = ["single_harness", "parallel_pk", "serial_relay", "orchestrate"]


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    for name in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR"):
        directory = tmp_path / name.lower()
        directory.mkdir()
        monkeypatch.setenv(name, str(directory))
    monkeypatch.setenv("LEGION_GATEWAY_RETRIES", "0")
    monkeypatch.syspath_prepend(str(SERVICE_DIR))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))


@pytest.fixture
def onboard(tmp_path, monkeypatch, isolated_environment):
    spec = importlib.util.spec_from_file_location(
        "__ut_onboard_distribution", SERVICE_DIR / "harness_onboard.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    core = tmp_path / "core"
    service = core / "release/legion-service"
    service.mkdir(parents=True)
    monkeypatch.setattr(module, "__file__", str(service / "harness_onboard.py"))
    monkeypatch.setattr(module, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(
        module.R, "load_catalog",
        lambda: [{"key": KEY, "execution_role": "agent"}],
    )
    monkeypatch.setattr(
        module.R, "install", lambda *a, **kw: {"ok": True, "status": "installed"}
    )
    monkeypatch.setattr(
        module.R, "probe", lambda *a, **kw: {"runnable": True, "status": "ready"}
    )
    monkeypatch.setattr(module, "_harness_pool_has", lambda *a: (True, "test pool"))
    monkeypatch.setattr(module, "_tool_run", lambda *a: (True, "test tool boundary"))
    monkeypatch.setattr(module, "_speed_runs", lambda *a: ([
        {"wall_s": 0.1, "api_ms": 1, "ok": True, "retried": False,
         "status": "ok", "final": "2", "error": ""}
        for _ in range(3)
    ], []))
    return module, core


def invoke(onboard, monkeypatch, *options):
    module, _ = onboard
    monkeypatch.setattr(sys, "argv", ["harness_onboard.py", "--key", KEY, *options])
    code = module.main()
    reports = list(module.REPORT_DIR.glob(f"{KEY}-*.json"))
    assert len(reports) == 1
    return code, json.loads(reports[0].read_text())


def write_modes(core, *, mode_names=MODES, exit_code=0, write_report=True):
    script = core / "dev/benchmark_execution_modes.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "import json, pathlib, sys\n"
        "output = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        + (f"output.write_text(json.dumps({{'runs': "
           f"[{{'mode': value}} for value in {mode_names!r}]}}))\n"
           if write_report else "")
        + f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    return script


def test_missing_optional_matrix_preserves_passed_checks_but_not_full_pass(onboard, monkeypatch):
    code, report = invoke(onboard, monkeypatch)

    assert report["verdict"] == "PARTIAL"
    assert code == 2
    assert all(step["status"] == "PASS" for step in report["steps"][:6])
    matrix = report["steps"][6]
    assert matrix["status"] == "SKIP"
    assert matrix["reason"] == "not_included"


def test_real_failure_is_not_hidden_by_missing_matrix(onboard, monkeypatch):
    module, _ = onboard
    monkeypatch.setattr(module.R, "install", lambda *a, **kw: {
        "ok": False, "status": "failed", "error": "synthetic build failure",
    })

    code, report = invoke(onboard, monkeypatch)

    assert code == 1
    assert report["verdict"] == "FAIL"
    assert report["steps"][1]["status"] == "FAIL"
    assert report["steps"][6].get("reason") == "not_included"


def test_present_matrix_really_runs_and_saves_unique_report(onboard, monkeypatch):
    module, core = onboard
    write_modes(core)

    code, report = invoke(onboard, monkeypatch)

    assert code == 0
    assert report["verdict"] == "PASS"
    assert report["steps"][6]["status"] == "PASS"
    matrices = list(module.REPORT_DIR.glob("modes-*/matrix.json"))
    assert len(matrices) == 1
    assert json.loads(matrices[0].read_text()) == {
        "runs": [{"mode": mode} for mode in MODES]
    }


def test_no_output_cannot_reuse_previous_member_matrix(onboard, monkeypatch, tmp_path):
    _, core = onboard
    write_modes(core, write_report=False)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    stale = tmp_path / f"onboard-modes-{KEY}.json"
    stale.write_text(json.dumps({"runs": [{"mode": mode} for mode in MODES]}))

    code, report = invoke(onboard, monkeypatch)

    assert code == 1
    assert report["verdict"] == "FAIL"
    assert report["steps"][6]["status"] == "FAIL"
    assert stale.is_file()


@pytest.mark.parametrize("exit_code,mode_names", [(3, MODES), (0, MODES[:1])])
def test_present_matrix_failure_remains_failure(onboard, monkeypatch, exit_code, mode_names):
    _, core = onboard
    write_modes(core, exit_code=exit_code, mode_names=mode_names)

    code, report = invoke(onboard, monkeypatch)

    assert code == 1
    assert report["verdict"] == "FAIL"
    assert report["steps"][6]["status"] == "FAIL"


def test_requested_skip_is_not_full_acceptance(onboard, monkeypatch):
    _, core = onboard
    write_modes(core)

    code, report = invoke(onboard, monkeypatch, "--skip-install")

    assert code == 2
    assert report["verdict"] == "PARTIAL"
    assert report["steps"][1].get("reason") == "requested_skip"


def test_build_only_keeps_existing_limited_result(onboard, monkeypatch, capsys):
    module, _ = onboard
    monkeypatch.setattr(sys, "argv", [
        "harness_onboard.py", "--key", KEY, "--build-only",
    ])

    assert module.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["scope"] == "build_only"
    assert result["runtime_acceptance"] == "not_run"
    assert not module.REPORT_DIR.exists()
