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


def build_spec(tmp_path, **changes):
    entry = {
        "key": KEY, "name": "Offline onboarding fixture", "kind": "agent",
        "execution_role": "agent", "url": "https://github.com/example/fixture",
        "version": "0.0.0-test", "revision": "a" * 40,
        "probe": {"method": "path", "target": "{source}/fixture"},
        "adapter_capabilities": {"tools": False},
    }
    entry.update(changes)
    path = tmp_path / "explicit-spec.json"
    path.write_text(json.dumps(entry))
    return path, entry


@pytest.mark.parametrize("existing", [False, True])
def test_build_only_explicit_spec_uses_locked_upsert_before_install(
    onboard, monkeypatch, tmp_path, capsys, existing
):
    from contextlib import contextmanager

    module, _ = onboard
    spec_path, entry = build_spec(tmp_path)
    catalog = tmp_path / "catalog" / "external_agents.json"
    catalog.parent.mkdir()
    other = {"key": "__ut_unrelated_member", "retained": True}
    items = [other, {"key": KEY, "version": "old"}] if existing else [other]
    catalog.write_text(json.dumps({"agents": items}))
    monkeypatch.setattr(module.R, "CATALOG", catalog)
    monkeypatch.setattr(module.R, "load_catalog", lambda: json.loads(catalog.read_text())["agents"])
    original_lock = module.R.catalog_lock
    events = []

    @contextmanager
    def observed_lock():
        with original_lock():
            events.append("locked")
            yield
        events.append("unlocked")

    monkeypatch.setattr(module.R, "catalog_lock", observed_lock)
    original_save = module.R._save_catalog

    def save(items):
        # _save_catalog also re-enters the same lock; require the outer read/update lock.
        assert events.count("locked") > events.count("unlocked")
        return original_save(items)

    monkeypatch.setattr(module.R, "_save_catalog", save)

    def install(key, **kwargs):
        assert events and events.count("locked") == events.count("unlocked")
        assert key == KEY
        assert kwargs == {"confirm": True, "timeout_s": 17, "build_only": True}
        assert module.R.load_catalog() == [other, entry]
        events.append("install")
        return {"ok": True, "status": "built", "runtime_acceptance": "not_run"}

    def unexpected(*args, **kwargs):
        pytest.fail("build-only must not run model, pool or runtime-acceptance stages")

    monkeypatch.setattr(module.R, "install", install)
    monkeypatch.setattr(module.R, "probe", unexpected)
    for name in ("_harness_pool_has", "_speed_runs", "_tool_run", "_capability_runs"):
        monkeypatch.setattr(module, name, unexpected)
    monkeypatch.setattr(sys, "argv", [
        "harness_onboard.py", "--key", KEY, "--spec", str(spec_path),
        "--build-only", "--install-timeout", "17",
    ])

    assert module.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["scope"] == "build_only"
    assert result["runtime_acceptance"] == "not_run"
    assert events[-1] == "install"
    assert events.count("locked") == events.count("unlocked")
    assert not module.REPORT_DIR.exists()


@pytest.mark.parametrize("changes", [
    {"key": "__ut_another_key"},
    {"revision": "not-a-pinned-revision"},
])
def test_build_only_invalid_spec_never_installs(onboard, monkeypatch, tmp_path, changes):
    module, _ = onboard
    spec_path, _ = build_spec(tmp_path, **changes)
    monkeypatch.setattr(module.R, "install", lambda *a, **kw: pytest.fail("invalid spec installed"))
    monkeypatch.setattr(module.R, "_save_catalog", lambda *a: pytest.fail("invalid spec saved"))
    monkeypatch.setattr(sys, "argv", [
        "harness_onboard.py", "--key", KEY, "--spec", str(spec_path), "--build-only",
    ])
    with pytest.raises(ValueError):
        module.main()


@pytest.mark.parametrize("options", [
    ["--build-only", "--spec", "must-not-read.json"],
    ["--key", KEY, "--build-only", "--from-github", "https://github.com/example/fixture"],
    ["--key", KEY, "--build-only", "--skip-install"],
    ["--key", KEY, "--build-only", "--spec", "must-not-read.json", "--install-timeout", "0"],
])
def test_build_only_conflicts_still_reject_before_registration_or_install(onboard, monkeypatch, options):
    module, _ = onboard
    monkeypatch.setattr(module, "_upsert_catalog", lambda *a: pytest.fail("conflicting spec registered"))
    monkeypatch.setattr(module.R, "install", lambda *a, **kw: pytest.fail("conflicting flags installed"))
    monkeypatch.setattr(sys, "argv", ["harness_onboard.py", *options])
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2


def test_build_only_spec_preserves_failed_build_without_runtime_acceptance(
    onboard, monkeypatch, tmp_path, capsys
):
    module, _ = onboard
    spec_path, _ = build_spec(tmp_path)
    calls = []
    monkeypatch.setattr(module, "_upsert_catalog", lambda *args: calls.append(args))
    monkeypatch.setattr(module.R, "install", lambda *a, **kw: {
        "ok": False, "status": "build_failed", "error": "synthetic build failure",
    })
    monkeypatch.setattr(sys, "argv", [
        "harness_onboard.py", "--key", KEY, "--spec", str(spec_path), "--build-only",
    ])
    assert module.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert calls == [(KEY, str(spec_path))]
    assert result["status"] == "build_failed"
    assert result["runtime_acceptance"] == "not_run"
    assert not module.REPORT_DIR.exists()
