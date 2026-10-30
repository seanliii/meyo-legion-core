"""Offline selfcheck contracts; subprocesses use tiny, non-recursive source fixtures."""

import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "harness_selfcheck.py"
REQUIRED = [
    "tests/test_core_distribution.py",
    "tests/test_personal_runtime_bootstrap.py",
    "tests/test_harness_selfcheck.py",
]
HISTORICAL_EXECUTION = [
    "tests/test_harness_ctx_contract.py",
    "tests/test_harness_failure_matrix.py",
    "tests/test_runtime_manifest.py",
]


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    for variable, leaf in (
        ("HOME", "home"), ("CODEX_HOME", "codex"),
        ("MEYO_LEGION_DIR", "meyo"), ("LEGION_HOME_DIR", "legion"),
        ("TMPDIR", "tmp"),
    ):
        directory = tmp_path / leaf
        directory.mkdir()
        monkeypatch.setenv(variable, str(directory))
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")


@pytest.fixture
def extracted(tmp_path):
    service = tmp_path / "extracted" / "release" / "legion-service"
    (service / "tests").mkdir(parents=True)
    (service / "harness_selfcheck.py").write_bytes(SOURCE.read_bytes())
    for name in REQUIRED:
        (service / name).write_text(
            "from pathlib import Path\n"
            "def test_bundled():\n"
            "    Path(__file__).with_suffix('.ran').write_text('executed')\n",
            encoding="utf-8",
        )
    # Fail, and leave proof, if selfcheck tries to discover a diff or Git cache.
    binary = tmp_path / "bin"
    binary.mkdir()
    git = binary / "git"
    git.write_text(
        "#!/bin/sh\nprintf invoked > "
        + shlex.quote(str(tmp_path / "git-invoked")) + "\nexit 99\n",
        encoding="utf-8",
    )
    git.chmod(0o755)
    return service


def run_cli(service, *extra, distribution=True, execute=True, inherited=None):
    root = service.parents[2]
    env = {
        "PATH": str(root / "bin") + os.pathsep + os.environ.get("PATH", os.defpath),
        "HOME": str(root / "home"),
        "CODEX_HOME": str(root / "codex"),
        "MEYO_LEGION_DIR": str(root / "meyo"),
        "LEGION_HOME_DIR": str(root / "legion"),
        "TMPDIR": str(root / "tmp"),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LEGION_EXT_AUTO_UPDATE": "0",
        "LEGION_GATEWAY_RETRIES": "0",
    }
    env.update(inherited or {})
    command = [sys.executable, "-I", "-B", str(service / "harness_selfcheck.py")]
    if distribution:
        command += ["--distribution", "private-core"]
    if execute:
        command.append("--run-tests")
    return subprocess.run(
        [*command, *extra], cwd=service.parents[1], env=env,
        capture_output=True, text=True, timeout=60,
    )


def report_from(completed, expected_code):
    assert completed.returncode == expected_code, completed.stderr + completed.stdout[-2000:]
    return json.loads(completed.stdout)


def test_distribution_runs_bundled_tests_without_git_or_legacy_imports(extracted, tmp_path):
    output = tmp_path / "report.json"
    completed = run_cli(extracted, "--output", str(output))
    report = report_from(completed, 0)
    assert json.loads(output.read_text()) == report
    assert report["distribution"] == "private-core"
    assert report["tests"]["scope"] == "private-core-regressions"
    assert report["tests"]["status"] == "passed"
    assert report["tests"]["ok"] is True
    assert report["tests"]["required"] == REQUIRED
    assert report["tests"]["cached"] is False
    assert "3 passed" in report["tests"]["stdout"]
    for name in REQUIRED:
        assert (extracted / name).with_suffix(".ran").read_text() == "executed"
    history = report["historical_tests"]
    assert history["ok"] is False
    assert history["status"] == "not_verified"
    assert "tests/test_runtime_manifest.py" in history["not_included"]
    assert set(history["not_included"]) <= set(history["not_verified"])
    assert "tests/test_harness_selfcheck.py" not in history["not_verified"]
    assert report["live_harnesses"]["status"] == "not_verified"
    assert not (tmp_path / "git-invoked").exists()
    assert not (extracted.parents[1] / ".git").exists()


def test_present_historical_test_is_not_run_or_claimed_verified(extracted):
    (extracted / "tests/test_runtime_manifest.py").write_text(
        "raise AssertionError('historical test must not be selected')\n",
        encoding="utf-8",
    )
    report = report_from(run_cli(extracted), 0)
    history = report["historical_tests"]
    assert "tests/test_runtime_manifest.py" not in history["not_included"]
    assert "tests/test_runtime_manifest.py" in history["not_verified"]
    assert history["status"] == "not_verified"


@pytest.mark.parametrize("missing", REQUIRED)
def test_missing_required_test_is_a_hard_failure_before_execution(extracted, missing):
    (extracted / missing).unlink()
    report = report_from(run_cli(extracted), 1)
    assert report["tests"]["ok"] is False
    assert report["tests"]["status"] == "failed"
    assert report["tests"]["missing"] == [missing]
    assert report["tests"]["commands"] == []
    assert not list((extracted / "tests").glob("*.ran"))


def test_real_bundled_test_failure_is_not_hidden(extracted):
    (extracted / REQUIRED[0]).write_text(
        "def test_regression():\n    assert False, 'bundled regression failed'\n",
        encoding="utf-8",
    )
    report = report_from(run_cli(extracted), 1)
    assert report["tests"]["ok"] is False
    assert report["tests"]["status"] == "failed"
    assert report["tests"]["returncode"] == 1
    assert "bundled regression failed" in report["tests"]["stdout"]
    assert report["historical_tests"]["status"] == "not_verified"


def test_inventory_without_execution_is_not_a_green_check(extracted):
    report = report_from(run_cli(extracted, execute=False), 1)
    assert report["tests"]["ok"] is False
    assert report["tests"]["status"] == "not_verified"
    assert report["tests"]["commands"] == []
    assert not list((extracted / "tests").glob("*.ran"))


@pytest.mark.parametrize("extra", [
    ["--environment"], ["--quality-levels", "L0"], ["--live-harnesses", "codex"],
    ["--product-target", "feature:fixture"], ["--files", "legion_core.py"],
    ["--base", "old-ref"],
])
def test_distribution_rejects_live_or_diff_selection_options(extracted, extra):
    completed = run_cli(extracted, *extra)
    assert completed.returncode == 2
    assert "--distribution" in completed.stderr
    assert not list((extracted / "tests").glob("*.ran"))


def test_child_isolation_drops_inherited_credentials_and_disables_caches(extracted, tmp_path):
    captured = tmp_path / "child-environment.json"
    forbidden = [
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "HTTPS_PROXY",
        "LEGION_PERSONAL_CONFIG", "LEGION_SELFCHECK_CACHE", "UNRELATED_ENV_SECRET",
        "PYTHONPATH",
    ]
    directories = ["HOME", "CODEX_HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR"]
    (extracted / REQUIRED[0]).write_text(
        "import json, os\nfrom pathlib import Path\n"
        "def test_isolation():\n"
        f"    dirs = {{name: os.environ[name] for name in {directories!r}}}\n"
        "    readback = {}\n"
        "    for name, directory in dirs.items():\n"
        "        probe = Path(directory) / 'probe.txt'\n"
        "        probe.write_text('isolated')\n"
        "        readback[name] = probe.read_text()\n"
        f"    inherited = [name for name in {forbidden!r} if name in os.environ]\n"
        f"    Path({str(captured)!r}).write_text(json.dumps({{\n"
        "        'directories': dirs, 'inherited': inherited, 'readback': readback,\n"
        "        'git_global': os.environ.get('GIT_CONFIG_GLOBAL'),\n"
        "        'git_system': os.environ.get('GIT_CONFIG_NOSYSTEM'),\n"
        "        'plugins': os.environ.get('PYTEST_DISABLE_PLUGIN_AUTOLOAD')}))\n"
        "    assert inherited == []\n",
        encoding="utf-8",
    )
    inherited = {name: "synthetic-private-value" for name in forbidden}
    inherited["LEGION_SELFCHECK_CACHE"] = str(tmp_path / "unused-cache.json")
    report = report_from(run_cli(extracted, inherited=inherited), 0)
    child = json.loads(captured.read_text())
    assert child["inherited"] == []
    assert set(child["readback"].values()) == {"isolated"}
    assert child["git_global"] == os.devnull
    assert child["git_system"] == child["plugins"] == "1"
    for name, path in child["directories"].items():
        assert path != os.environ[name]
        assert not Path(path).exists(), "child temporary state must be cleaned up"
    assert not (tmp_path / "unused-cache.json").exists()
    assert "synthetic-private-value" not in json.dumps(report)
    assert not (tmp_path / "git-invoked").exists()
    for name in (".git", "__pycache__", ".pytest_cache"):
        assert not list(extracted.parents[1].rglob(name))


def test_default_mode_keeps_historical_selection_and_missing_test_failure(extracted):
    # Only the old environment dependencies are replaced; selection and pytest are real.
    (extracted / "runtime_manifest.py").write_text(
        "def load_definitions(): return {}\n"
        "def diagnostics(force=False): return {'ok': True}\n",
        encoding="utf-8",
    )
    (extracted / "evidence_schema.py").write_text(
        "def new_run_id(prefix): return 'fixture-run'\n", encoding="utf-8",
    )
    report = report_from(run_cli(
        extracted, "--files", "release/legion-service/legion_core.py", "--no-cache",
        distribution=False,
    ), 1)
    assert "distribution" not in report
    assert report["impact"]["tests"] == HISTORICAL_EXECUTION
    assert report["tests"]["ok"] is False
    assert report["tests"]["returncode"] == 4
    assert "file or directory not found" in report["tests"]["stdout"]
    assert not list((extracted / "tests").glob("*.ran"))


def test_default_runner_still_reuses_its_explicit_cache(extracted, tmp_path, monkeypatch):
    cache = tmp_path / "legacy-cache.json"
    monkeypatch.setenv("LEGION_SELFCHECK_CACHE", str(cache))
    monkeypatch.syspath_prepend(str(extracted))
    spec = importlib.util.spec_from_file_location(
        "selfcheck_fixture", extracted / "harness_selfcheck.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    first = module.run_tests([REQUIRED[0]])
    assert first["ok"] is True
    assert first["cached"] is False
    marker = (extracted / REQUIRED[0]).with_suffix(".ran")
    assert marker.read_text() == "executed"
    marker.unlink()
    second = module.run_tests([REQUIRED[0]])
    assert second["ok"] is True
    assert second["cached"] is True
    assert not marker.exists()
