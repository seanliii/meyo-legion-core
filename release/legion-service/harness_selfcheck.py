#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""军团改动后的自动关联检查与安全 smoke 入口。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
ORCHESTRATOR = REPO / "harness_baselines" / "meyo-orchestrator"
for path in (HERE, ORCHESTRATOR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

_SHARED_PATHS = {
    "runtime_manifest.py": {"manifest", "routing", "all-harnesses"},
    "harness_manifest.json": {"manifest", "routing", "all-harnesses"},
    "external_agents.json": {"manifest", "routing", "external"},
    "model_registry.py": {"models", "routing", "all-harnesses"},
    "error_envelope.py": {"failures", "all-harnesses"},
    "legion_core.py": {"execution", "failures", "all-harnesses"},
    "harness_registry.py": {"execution", "adapters", "all-harnesses"},
    "skill_factory.py": {"skills"},
    "sessions.py": {"users"},
    "harness_performance.py": {"ranking", "routing", "all-harnesses"},
    "captain_aptitude.py": {"ranking", "routing", "all-harnesses"},
    "benchmark_execution_modes.py": {"modes", "execution", "all-harnesses"},
    "verification_center.py": {"verification"},
    "verification_reviews.py": {"verification", "product-review"},
    "verification_http.py": {"verification", "product-review"},
    "evidence_schema.py": {"evidence", "release-gate"},
    "release_gate.py": {"evidence", "release-gate"},
    "test_evidence_schema.py": {"evidence"},
    "test_release_gate.py": {"release-gate"},
    "quality_executor.py": {"evidence", "quality-execution", "release-gate"},
    "test_quality_executor.py": {"quality-execution"},
    "journey_runner.py": {"journeys", "release-gate"},
    "test_journey_runner.py": {"journeys"},
    "core-matrix.json": {"journeys", "release-gate"},
    "test_verification_center.py": {"verification", "product-review"},
    "test_verification_center_browser.py": {"verification", "product-review", "frontend"},
    "index_v2.html": {"frontend", "verification"},
}

_QUALITY_LEVELS = {
    "L0": "短答、自交付、无接力与延迟",
    "L1": "原生工具执行、错误回流与文件落盘",
    "L2": "源码 revision、file:line 与引用语义",
    "L3": "复杂文件交付与真实打开",
    "L4": "跨轮 session、重启恢复、取消与多用户隔离",
}

_TESTS = {
    "manifest": ["tests/test_runtime_manifest.py", "tests/test_external_agents.py"],
    "routing": ["tests/test_runtime_manifest.py", "tests/test_harness_ctx_contract.py"],
    "models": ["tests/test_runtime_manifest.py", "tests/test_harness_ctx_contract.py"],
    "failures": ["tests/test_harness_failure_matrix.py", "tests/test_harness_ctx_contract.py"],
    "execution": ["tests/test_harness_ctx_contract.py", "tests/test_harness_failure_matrix.py"],
    "adapters": ["tests/test_harness_ctx_contract.py", "tests/test_external_agents.py"],
    "external": ["tests/test_external_agents.py"],
    "skills": ["tests/test_skill_factory.py"],
    "users": ["tests/test_dag_policy_and_memory.py", "tests/test_subscription_history.py"],
    "ranking": ["tests/test_harness_performance.py"],
    "modes": ["tests/test_harness_performance.py", "tests/test_dispatch_modes.py"],
    "all-harnesses": ["tests/test_runtime_manifest.py", "tests/test_harness_failure_matrix.py"],
    "verification": ["tests/test_verification_center.py"],
    "product-review": ["tests/test_verification_center.py"],
    "evidence": ["tests/test_evidence_schema.py", "tests/test_verification_center.py"],
    "quality-execution": ["tests/test_quality_executor.py"],
    "release-gate": ["tests/test_release_gate.py", "tests/test_harness_selfcheck.py"],
    "journeys": ["tests/test_journey_runner.py", "tests/test_frontend_user_journeys.py"],
    "frontend": ["tests/test_frontend_user_journeys.py", "tests/test_verification_center_browser.py"],
}

# 固定分发合同，不能按磁盘现存测试缩减，否则漏打包会被误报为通过。
_PRIVATE_CORE_TESTS = [
    "tests/test_core_distribution.py",
    "tests/test_personal_runtime_bootstrap.py",
    "tests/test_harness_selfcheck.py",
]


def changed_files(base: str = "HEAD", explicit: list[str] | None = None) -> list[str]:
    """返回本次验收输入；显式文件可隔离其它并行会话尚未提交的改动。"""
    if explicit is None:
        # -z 避免 core.quotePath 把中文路径转成带引号的八进制转义；同时只纳入会影响产品/测试的源码，
        # 不让历史截图和浏览器日志把自检报告淹没。
        result = subprocess.run(["git", "diff", "--name-only", "-z", base, "--"], cwd=REPO,
                                capture_output=True, timeout=30)
        extra = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=REPO,
                               capture_output=True, timeout=30)
        values = (result.stdout + extra.stdout).decode("utf-8", errors="replace").split("\0")
    else:
        values = [str(value or "").strip() for value in explicit]
    allowed = (".py", ".html", ".css", ".js", ".ts", ".json", ".yaml", ".yml", ".sh", ".md", ".toml")
    return sorted({path for path in values if path and path.endswith(allowed)
                   and not path.startswith((".playwright-mcp/", "dev/collective-arena-runs/"))})


def impact(files: list[str]) -> dict:
    dimensions = set()
    harnesses = set()
    for file in files:
        name = pathlib.Path(file).name
        dimensions.update(_SHARED_PATHS.get(name, set()))
        for key in _manifest_keys():
            if key in file.lower():
                harnesses.add(key)
    if "all-harnesses" in dimensions:
        harnesses.update(_manifest_keys())
    tests = sorted({test for dimension in dimensions for test in _TESTS.get(dimension, [])})
    return {"files": files, "dimensions": sorted(dimensions),
            "harnesses": sorted(harnesses), "tests": tests}


def _manifest_keys() -> list[str]:
    import runtime_manifest
    return sorted(runtime_manifest.load_definitions())


def quality_plan(keys: list[str] | None = None) -> dict:
    """按成员角色生成分层验收计划；不拿控制面与答题 harness 使用同一把尺。"""
    import runtime_manifest
    definitions = runtime_manifest.load_definitions()
    selected = sorted(definitions) if keys is None else keys
    rows = []
    for key in selected:
        item = definitions.get(key)
        if not item:
            continue
        runtime = item.get("runtime") or {}
        role = str(runtime.get("execution_role") or "agent")
        specs = item.get("specs") or {}
        levels = ["L0"] if role == "agent" else []
        if role == "agent" and specs.get("tools"):
            levels.append("L1")
        if role == "agent":
            levels.append("L2")
        if role == "agent" and specs.get("artifacts"):
            levels.append("L3")
        if role == "agent" and specs.get("resume") == "native":
            levels.append("L4")
        if role != "agent":
            levels = ["L2"]
        rows.append({"key": key, "source": item.get("source"), "role": role,
                     "manifest_valid": item.get("manifest_valid"), "levels": levels,
                     "checks": [_QUALITY_LEVELS[level] for level in levels]})
    return {"registered": len(definitions), "selected": len(rows), "levels": _QUALITY_LEVELS,
            "members": rows}


def quick_environment_report() -> dict:
    """日常开发只查便宜且确定的本地事实，不为无关改动初始化完整执行栈。"""
    import runtime_manifest
    return {"python": sys.version.split()[0], "python_ok": sys.version_info[:2] == (3, 12),
            "manifest": runtime_manifest.diagnostics(force=True),
            "scope": "quick", "hint": "需要代理、router 和可运行成员诊断时加 --environment"}


def product_release_gate(target: str, *, evidence_paths: list[str] | None = None,
                         required_subjects: tuple[str, ...] | None = None,
                         security_blockers: list[str] | None = None,
                         commit: str = "", max_age_seconds: float = 7 * 86400) -> dict:
    """兼容产品门；传机器证据后启用统一发布门，旧调用仍只检查人工评审。"""
    import verification_reviews
    target = str(target or "").strip()
    review = verification_reviews.get(target)
    if evidence_paths is not None or required_subjects is not None or security_blockers:
        import release_gate
        return release_gate.unified_release_gate(
            target=target, review=review, evidence_paths=evidence_paths,
            required_subjects=required_subjects or ("frontend-user-journeys",),
            security_blockers=security_blockers, commit=commit,
            max_age_seconds=max_age_seconds)
    if not review:
        return {"ok": False, "target": target,
                "blockers": ["验收中心没有这项功能的产品记录"]}
    gate = verification_reviews.product_gate(review)
    return {"ok": bool(gate.get("release_ready")), "target": target,
            "quality_score": review.get("quality_score"),
            "current_version": review.get("current_version") or "",
            "target_version": review.get("target_version") or "",
            "upgrade_status": review.get("upgrade_status") or "not_planned",
            "blockers": gate.get("blockers") or []}


def environment_report() -> dict:
    import runtime_manifest
    import harness_registry
    import legion_core
    router_url = str(os.environ.get("ANTHROPIC_BASE_URL") or "").rstrip("/")
    router = None
    if router_url.startswith(("http://127.0.0.1", "http://localhost")):
        router = harness_registry._probe_local_router(router_url)
    proxies = {name: value for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                                        "http_proxy", "https_proxy", "all_proxy")
               if (value := os.environ.get(name))}
    ready = legion_core._aigc_ready(cached_only=True)
    runnable = sorted(ready.get("runnable_clis") or [])
    return {"python": sys.version.split()[0], "python_ok": sys.version_info[:2] == (3, 12),
            "manifest": runtime_manifest.diagnostics(force=True),
            "router": {"url": router_url, "business_request_ok": router},
            "proxies": proxies, "runnable": runnable,
            "runnable_state": "cached" if runnable else "cache_cold_not_a_failure"}


def _test_cache_path() -> pathlib.Path:
    override = os.environ.get("LEGION_SELFCHECK_CACHE")
    if override:
        return pathlib.Path(override)
    git_dir = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=REPO,
                             capture_output=True, text=True)
    root = pathlib.Path(git_dir.stdout.strip()) if git_dir.returncode == 0 else REPO / ".git"
    if not root.is_absolute():
        root = REPO / root
    return root / "legion-selfcheck-cache.json"


def _resolve_test_input(name: str) -> pathlib.Path:
    path = pathlib.Path(name)
    if path.is_absolute():
        return path
    return HERE / path if name.startswith("tests/") else REPO / path


def _input_hashes(paths: list[str], files: list[str]) -> dict[str, str]:
    values = {}
    for name in sorted(set(paths + files)):
        path = _resolve_test_input(name)
        try:
            values[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            values[name] = "missing"
    return values


def _test_fingerprint(paths: list[str], files: list[str]) -> str:
    digest = hashlib.sha256(sys.version.encode())
    for name, value in _input_hashes(paths, files).items():
        digest.update(f"{name}:{value}".encode())
    return digest.hexdigest()


def run_tests(paths: list[str], *, files: list[str] | None = None, use_cache: bool = True,
              isolated: bool = False) -> dict:
    if not paths:
        return {"ok": True, "skipped": True, "reason": "当前 diff 未命中已知共享链路"}
    use_cache = use_cache and not isolated
    fingerprint = _test_fingerprint(paths, files or [])
    cache_path = _test_cache_path() if use_cache else None
    if use_cache:
        try:
            ledger = json.loads(cache_path.read_text(encoding="utf-8"))
            cached = (ledger.get("entries") or {}).get(fingerprint) or {}
            if cached.get("ok") is True:
                return {**cached, "cached": True, "elapsed": 0.0}
        except (OSError, json.JSONDecodeError):
            pass
    with tempfile.TemporaryDirectory(prefix="legion-selfcheck-") as root:
        if isolated:
            # 仅分发模式使用干净子进程；不继承个人凭据、配置路径、代理或 pytest 参数。
            env = {name: os.environ[name] for name in
                   ("PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL") if name in os.environ}
            for name, leaf in (
                ("HOME", "home"), ("CODEX_HOME", "codex"),
                ("MEYO_LEGION_DIR", "meyo"), ("LEGION_HOME_DIR", "legion"),
                ("TMPDIR", "tmp"), ("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"),
            ):
                directory = pathlib.Path(root, leaf)
                directory.mkdir()
                env[name] = str(directory)
            env.update({
                "PYTHONDONTWRITEBYTECODE": "1",
                "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "LEGION_EXT_AUTO_UPDATE": "0", "LEGION_GATEWAY_RETRIES": "0",
            })
        else:
            env = dict(os.environ)
            env["MEYO_LEGION_DIR"] = str(pathlib.Path(root, "meyo"))
            env["LEGION_HOME_DIR"] = str(pathlib.Path(root, "legion"))
            env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(HERE), env.get("PYTHONPATH", "")]))
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        standalone_scripts = {"tests/test_verification_center_browser.py",
                              "tests/test_frontend_user_journeys.py"}
        browser_scripts = [path for path in paths if path in standalone_scripts]
        pytest_paths = [path for path in paths if path not in browser_scripts]
        commands = []
        python = [sys.executable, "-I", "-B"] if isolated else [sys.executable]
        if pytest_paths:
            options = ["-p", "no:cacheprovider", "--basetemp", str(pathlib.Path(root, "pytest"))] if isolated else []
            commands.append([*python, "-m", "pytest", *pytest_paths, "-q", *options])
        commands.extend([*python, path] for path in browser_scripts)
        started = time.monotonic()
        outputs, returncode = [], 0
        for command in commands:
            result = subprocess.run(command, cwd=HERE, env=env, capture_output=True, text=True, timeout=600)
            outputs.append("$ " + " ".join(command) + "\n" + result.stdout + result.stderr)
            if result.returncode:
                returncode = result.returncode
                break
        report = {"ok": returncode == 0, "returncode": returncode,
                  "elapsed": round(time.monotonic() - started, 2), "commands": commands,
                  "stdout": "\n".join(outputs)[-9000:], "stderr": "",
                  "fingerprint": fingerprint, "inputs": _input_hashes(paths, files or []),
                  "cached": False}
        if report["ok"] and use_cache:
            try:
                import fcntl
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                lock_path = cache_path.with_suffix(".lock")
                with lock_path.open("a+") as lock:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                    try:
                        ledger = json.loads(cache_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        ledger = {"version": 1, "entries": {}}
                    entries = ledger.setdefault("entries", {})
                    entries[fingerprint] = {**report, "stored_at": time.time()}
                    if len(entries) > 64:
                        keep = sorted(entries, key=lambda key: entries[key].get("stored_at", 0),
                                      reverse=True)[:64]
                        ledger["entries"] = {key: entries[key] for key in keep}
                    tmp = cache_path.with_suffix(f".tmp.{os.getpid()}")
                    tmp.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
                    os.replace(tmp, cache_path)
            except OSError:
                pass
        return report


def private_core_report(*, execute: bool = False) -> dict:
    """不借 Git/大仓环境推断范围；只报告固定随包回归的实际结果。"""
    required = list(_PRIVATE_CORE_TESTS)
    missing = [name for name in required if not (HERE / name).is_file()]
    tests = {"ok": False, "status": "not_verified", "commands": []}
    if missing:
        tests.update(status="failed", reason="缺少应随包提供的关键回归，不能跳过")
    elif execute:
        tests = run_tests(required, use_cache=False, isolated=True)
        tests["status"] = "passed" if tests["ok"] else "failed"
    else:
        tests["reason"] = "仅列出范围；加 --run-tests 才执行随包回归"
    tests.update(scope="private-core-regressions", required=required, missing=missing)
    historical = sorted({name for paths in _TESTS.values() for name in paths})
    verified = set(required) if tests["ok"] else set()
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "distribution": "private-core",
        "tests": tests,
        "historical_tests": {
            "ok": False, "status": "not_verified",
            "not_included": [name for name in historical if not (HERE / name).is_file()],
            "not_verified": [name for name in historical if name not in verified],
        },
        "live_harnesses": {
            "ok": False, "status": "not_verified",
            "reason": "未调用模型、启动服务或执行真实成员体检",
        },
        "scope_note": "退出码 0 仅表示随包关键回归通过，不代表大仓、产品或全军团已验证",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="军团改动后关联检查")
    parser.add_argument("--base", default="HEAD")
    parser.add_argument("--distribution", choices=["private-core"],
                        help="显式运行私有核心随包回归；不依赖源码目录中的 .git，不作真实成员体检")
    parser.add_argument("--run-tests", action="store_true")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--environment", action="store_true",
                        help="额外加载完整 harness 执行栈，诊断代理/router/可运行成员")
    parser.add_argument("--product-target", default="",
                        help="发布前检查验收中心中的 feature:/session:/artifact: 产品记录")
    parser.add_argument("--files", nargs="*", default=None,
                        help="只验这些文件，隔离其它并行会话的工作树变化")
    parser.add_argument("--output", default="")
    parser.add_argument("--quality-levels", default="",
                        help="逗号分隔 L0-L4；发布验收时执行并产标准机器证据")
    parser.add_argument("--live-harnesses", default="",
                        help="L0 真实 server 体检成员，逗号分隔；空则只跑确定性合同")
    parser.add_argument("--evidence-run-id", default="")
    args = parser.parse_args()
    if args.distribution:
        if (args.environment or args.product_target or args.files is not None
                or args.quality_levels or args.live_harnesses or args.base != "HEAD"):
            parser.error("--distribution 不能与 diff 选择、环境诊断或真实/产品验收参数组合")
        report = private_core_report(execute=args.run_tests)
        if args.evidence_run_id:
            report["evidence_run_id"] = args.evidence_run_id
        encoded = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            pathlib.Path(args.output).write_text(encoded, encoding="utf-8")
        print(encoded)
        return 0 if report["tests"]["ok"] else 1
    files = changed_files(args.base, args.files)
    impacted = impact(files)
    import evidence_schema
    evidence_run_id = args.evidence_run_id or evidence_schema.new_run_id("selfcheck")
    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "evidence_run_id": evidence_run_id,
              "impact": impacted,
              "environment": environment_report() if args.environment else quick_environment_report(),
              "quality_plan": quality_plan(impacted["harnesses"])}
    if args.run_tests:
        report["tests"] = run_tests(report["impact"]["tests"], files=files,
                                    use_cache=not args.no_cache)
        try:
            import quality_executor
            report["fast_evidence"] = quality_executor.record_fast_contract(
                report["tests"], target=args.product_target or "feature:development",
                files=files, run_id=evidence_run_id)
        except Exception as exc:
            report["fast_evidence"] = {"error": f"{type(exc).__name__}: {exc}"}
    if args.quality_levels:
        import quality_executor
        report["quality_execution"] = quality_executor.execute_levels(
            target=args.product_target or "feature:development",
            levels=[item for item in args.quality_levels.split(",") if item], files=files,
            run_id=evidence_run_id,
            live_harnesses=[item for item in args.live_harnesses.split(",") if item],
            live=bool(args.live_harnesses), trigger="harness-selfcheck")
        if args.product_target:
            import verification_reviews
            report["quality_record"] = verification_reviews.record_machine_result(
                args.product_target, report["quality_execution"], actor="harness-selfcheck")
    if args.product_target:
        if args.quality_levels:
            import quality_executor
            report["product_gate"] = product_release_gate(
                args.product_target,
                evidence_paths=quality_executor.evidence_paths(
                    target=args.product_target, run_id=evidence_run_id),
                required_subjects=tuple(report["quality_execution"]["required_subjects"]),
                commit=evidence_schema._commit(REPO))
        else:
            report["product_gate"] = product_release_gate(args.product_target)
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        pathlib.Path(args.output).write_text(encoded, encoding="utf-8")
    print(encoded)
    checks = [report["environment"]["manifest"]["ok"], report.get("tests", {"ok": True})["ok"],
              report.get("quality_execution", {"ok": True})["ok"],
              report.get("quality_record", {"ok": True})["ok"]]
    if "product_gate" in report:
        checks.append(report["product_gate"]["ok"])
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
