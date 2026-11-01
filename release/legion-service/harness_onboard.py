#!/usr/bin/env python3.12
"""harness_onboard —— 新 harness 接入一条命令全自动流水线(用户 2026-09-02 钉死)。

用户原话精神:以后我说安装,就自动走自动完整的校验,不要你动手了。本文件就是那条
固定流程,任何人(agent/人)都不许再手工 npm/pip/手改 catalog 绕过它:

  python3.12 harness_onboard.py --key <family> [--spec spec.json] [--skip-install] [--no-server] [--model M]

六环全自动,每环独立判定 PASS/FAIL/SKIP,最终一份体检报告(表+JSON 落盘):
  1 catalog   声明式注册(持跨进程文件锁,杜绝双 agent 并发丢写)
  2 install   自动 git clone(revision 锁死) -> 构建声明命令 -> probe 验收
  3 pool      自动进入编排池(查 live server /api/legion/harnesses)
  4 speed     简单题x3 计速(目标 avg<=10s,网关过载单独归因 api_ms)
  5 tool      工具题(真落盘文件+final 回读,验 agent loop 不是哑管道)
  6 stability 状态全 ok,final 非空,probe runnable

速度底线(铁律0):任何一环卡住都有独立超时,绝不整条流水线干等。
完整接入报告区分 PASS(退出0)、FAIL(退出1)、PARTIAL(有适用检查未运行,退出2)。
独立发行包未附带旧模式矩阵时如实保留 not_included,不是成员失败或完整通过。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import external_agent_registry as R  # noqa: E402

REPORT_DIR = pathlib.Path(R._paths.under("external_agents", "onboard_reports"))
SPEED_TARGET_AVG = 10.0
SPEED_HARD_SINGLE = 25.0
SERVER = os.environ.get("LEGION_BASE_URL", "http://127.0.0.1:8000")


class Step:
    def __init__(self, name):
        self.name, self.status, self.detail, self.ms = name, "SKIP", "", 0.0
        self.reason = "not_run"

    def __enter__(self):
        self._t0 = time.time()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.ms = round((time.time() - self._t0) * 1000)
        if exc is not None:
            self.status = "FAIL"
            self.reason = "exception"
            self.detail = str(exc)[:300]
            return True
        return False

    def ok(self, detail=""):
        self.status, self.detail = "PASS", str(detail)[:300]
        self.reason = ""

    def fail(self, detail):
        self.status, self.detail = "FAIL", str(detail)[:300]
        self.reason = "check_failed"

    def skip(self, detail="", *, reason="not_run"):
        self.status, self.detail = "SKIP", str(detail)[:300]
        self.reason = reason


def _upsert_catalog(key, spec_path):
    entry = json.loads(pathlib.Path(spec_path).read_text(encoding="utf-8"))
    if entry.get("key") != key:
        raise ValueError("spec.key=%s 与 --key=%s 不一致" % (entry.get("key"), key))
    validation = R.agent_manifest.validate(entry)
    if not validation.get("ok"):
        raise ValueError("spec 不满足 catalog 契约:" + ";".join(validation.get("errors") or []))
    with R.catalog_lock():
        items = R.load_catalog()
        replaced = False
        for i, x in enumerate(items):
            if x.get("key") == key:
                items[i] = entry
                replaced = True
                break
        if not replaced:
            items.append(entry)
        R._save_catalog(items)
    return "catalog %s:%s (total=%d)" % ("updated" if replaced else "appended", key, len(items))

def _harness_pool_has(key, *, personal_token: str | None = None, uid: str | None = None):
    # Same caller-owned credential contract as desktop/CLI clients. A denied
    # directory request is not evidence that the server or member is unavailable.
    import legion_client
    try:
        data = legion_client.harnesses(SERVER, personal_token=personal_token, uid=uid, timeout=120)
    except legion_client.LegionClientError as exc:
        return False, f"{exc.code}: " + (
            "成员目录需要调用方已有个人Bearer（LEGION_PERSONAL_TOKEN），未判成员或server不可用"
            if exc.code == "auth_required" else
            "显式owner与个人凭证不匹配，未查询他人目录" if exc.code == "owner_mismatch" else
            "成员目录读取失败；未据此判定成员安装状态")
    pool = data.get("harnesses") or []
    for h in pool:
        if str(h.get("key")) == key:
            return True, "pool total=%d" % len(pool)
    return False, "pool total=%d 但无 %s" % (len(pool), key)


def _speed_runs(key, model, rounds=3):
    runs, problems = [], []
    for i in range(rounds):
        t0 = time.time()
        # 40s 单轮硬顶:网关 5xx 时 CLI 常带长退避重试,120s 只会白等;速度环本来目标<=10s,
        # 40s 还没出结果就该快速判失败看真错误,不让流水线干等(铁律0:速度底线)。
        r = R.run(key, "1+1等于几?只回答数字", timeout_s=40, model=model)
        retried = False
        if r.get("status") != "ok":
            retried = True
            r = R.run(key, "1+1等于几?只回答数字", timeout_s=40, model=model)
        wall = round(time.time() - t0, 1)
        final = str(r.get("final") or "").strip()
        api_ms = ""
        for e in (r.get("events") or []):
            if isinstance(e, dict) and e.get("type") == "result":
                api_ms = e.get("duration_ms")
        ok = final == "2"
        if not ok:
            problems.append("round%d final=%r status=%s err=%s" % (i + 1, final[:30], r.get("status"), str(r.get("error"))[:120]))
        runs.append({"wall_s": wall, "api_ms": api_ms, "ok": ok, "retried": retried,
                     "status": r.get("status"), "final": final[:30], "error": str(r.get("error"))[:150]})
        time.sleep(0.5)
    return runs, problems


def _tool_run(key, model, workdir):
    marker = "MARS-ONBOARD-TOOL-OK"
    workdir.mkdir(parents=True, exist_ok=True)
    target = workdir / "onboard_tool_test.txt"
    if target.exists():
        target.unlink()
    task = ("用工具在工作目录创建 onboard_tool_test.txt 写入内容 %s,然后读出来确认,final 里报告文件内容" % marker)
    r = R.run(key, task, timeout_s=240, model=model, workdir=str(workdir))
    final = str(r.get("final") or "")
    disk = target.read_text().strip() if target.is_file() and not target.is_symlink() else ""
    hit_final, hit_disk = marker in final, disk == marker
    detail = "final含标记=%s 磁盘真身=%s(%r) status=%s" % (marker in final, hit_disk, disk[:20], r.get("status"))
    return (r.get("status") == "ok" and hit_disk and hit_final), detail


def _capability_runs(key, entry):
    """Run the capability's own declarative checks instead of irrelevant QA prompts."""
    rows, problems = [], []
    for check in entry.get("onboard_checks") or []:
        action = str(check.get("action") or "")
        t0 = time.time()
        result = R.run_capability(key, action, check.get("params") or {},
                                  timeout_s=int(check.get("timeout_s") or 0) or None)
        ok = bool(result.get("ok"))
        stdout = str(result.get("stdout") or "")
        expected = str(check.get("stdout_contains") or "")
        if expected and expected.lower() not in stdout.lower():
            ok = False
        artifact = str(result.get("artifact") or "")
        if check.get("output_file") and not (artifact and pathlib.Path(artifact).is_file()):
            ok = False
        row = {"action": action, "ok": ok,
               "wall_s": round(time.time() - t0, 1),
               "artifact": artifact, "error": str(result.get("error") or "")[:160]}
        rows.append(row)
        if not ok:
            problems.append("%s:%s" % (action, row["error"] or stdout[-120:]))
    if not rows:
        problems.append("capability 未声明 onboard_checks")
    return rows, problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default="")
    ap.add_argument("--spec", default="")
    ap.add_argument("--from-github", default="", help="GitHub URL 一条指令全自动:clone->识别->生成spec->六环")
    ap.add_argument("--skip-install", action="store_true")
    ap.add_argument("--no-server", action="store_true")
    ap.add_argument("--build-only", action="store_true", help="仅构建成员；可用显式 --spec 持锁登记，不调用模型、不作运行验收")
    ap.add_argument("--install-timeout", type=int, default=1500, help="单个安装步骤时限（秒）；不改变任务执行时限")
    ap.add_argument("--model", default="gpt-5.6-sol")
    args = ap.parse_args()
    if args.install_timeout <= 0:
        ap.error("--install-timeout 必须为正整数")

    if args.build_only:
        if not args.key or args.from_github or args.skip_install:
            ap.error("--build-only 必须指定 --key，不能与 --from-github 或 --skip-install 组合")
        if args.spec:
            _upsert_catalog(args.key, args.spec)
        result = R.install(args.key, confirm=True, timeout_s=args.install_timeout, build_only=True)
        # Build evidence is deliberately distinct from the full onboarding verdict.
        print(json.dumps({"scope": "build_only", "runtime_acceptance": "not_run",
                          **result}, ensure_ascii=False))
        return 0 if result.get("ok") else 1

    autospec_note = ""
    if args.from_github:
        # 一条指令全自动(护城河固化 2026-09-02):URL->clone->读源码识别->自动生成spec->落盘->六环
        import harness_autospec
        gen = harness_autospec.generate(args.from_github, args.key)
        args.key = gen["spec"]["key"]
        spec_path = pathlib.Path("/tmp/autospec-%s.json" % args.key)
        spec_path.write_text(json.dumps(gen["spec"], ensure_ascii=False, indent=2), encoding="utf-8")
        args.spec = str(spec_path)
        autospec_note = " [autospec:%s|%s]" % (gen["kind"], gen["hint"])
    if not args.key:
        ap.error("--key 或 --from-github 必须给一个")

    print("== harness_onboard:%s (model=%s)%s ==" % (args.key, args.model, autospec_note))
    steps = []

    s = Step("1_catalog"); steps.append(s)
    with s:
        if args.spec:
            s.ok(_upsert_catalog(args.key, args.spec))
        else:
            keys = [x.get("key") for x in R.load_catalog()]
            if args.key in keys:
                s.ok("已在 catalog(%d家),无 --spec 不动声明" % len(keys))
            else:
                s.fail("catalog 无 %s·新家必须给 --spec 或 --from-github(声明式注册,禁止手改 json)" % args.key)

    # catalog upsert 后再读取角色；旧代码在 upsert 前读取，修正 agent→capability 的 spec
    # 会在本轮仍按旧角色跑数学题，产生与真实能力无关的假 FAIL。
    entry = next((x for x in R.load_catalog() if x.get("key") == args.key), {})
    # 接入只处理当前成员；不得在安装前清理其他成员的保留源码。
    role = entry.get("execution_role") or "agent"
    is_answer_agent = role == "agent"

    s = Step("2_install"); steps.append(s)
    with s:
        if args.skip_install:
            s.skip("--skip-install", reason="requested_skip")
        else:
            res = R.install(args.key, confirm=True, timeout_s=args.install_timeout)
            if res.get("ok"):
                s.ok("status=%s" % res.get("status"))
            else:
                s.fail("status=%s err=%s" % (res.get("status"), str(res.get("error"))[:200]))

    s = Step("3_pool"); steps.append(s)
    with s:
        if args.no_server:
            s.skip("--no-server", reason="requested_skip")
        else:
            has, detail = _harness_pool_has(args.key)
            (s.ok if has else s.fail)(("已进编排池" if has else "未进编排池") + "·" + detail)

    s = Step("4_speed"); steps.append(s)
    with s:
        runs, problems = [], []
        capability_runs = []
        if not is_answer_agent:
            capability_runs, problems = _capability_runs(args.key, entry)
            if problems:
                s.fail("capability 真验收失败:" + "; ".join(problems))
            else:
                s.ok("execution_role=%s 原生动作 %d/%d 通过 walls=%s" %
                     (role, len(capability_runs), len(entry.get("onboard_checks") or []),
                      [row["wall_s"] for row in capability_runs]))
        else:
            runs, problems = _speed_runs(args.key, args.model)
            walls = [x["wall_s"] for x in runs] or [0.0]
            avg = round(sum(walls) / max(len(walls), 1), 1)
            api = [x["api_ms"] for x in runs]
            if problems:
                s.fail("答案错:" + "; ".join(problems))
            elif avg <= SPEED_TARGET_AVG and max(walls) <= SPEED_HARD_SINGLE:
                s.ok("avg=%ss walls=%s api_ms=%s (目标<=%ss)" % (avg, walls, api, SPEED_TARGET_AVG))
            elif max(walls) <= SPEED_HARD_SINGLE and any(int(a or 0) > 6000 for a in api):
                s.fail("avg=%ss 超标且 api_ms=%s 高=网关过载非 CLI 病·换时段复测" % (avg, api))
            else:
                s.fail("avg=%ss walls=%s api_ms=%s 超目标(<=%ss)·查根因" % (avg, walls, api, SPEED_TARGET_AVG))

    try:
        import harness_performance
        for run in runs:
            harness_performance.record(args.key, "快答", "single", ok=bool(run["ok"]),
                                       wall_s=float(run["wall_s"]), source="onboard")
    except Exception as exc:
        problems.append("performance_ledger:%s:%s" % (type(exc).__name__, exc))

    s = Step("5_tool"); steps.append(s)
    with s:
        if not is_answer_agent:
            tool_ok = True
            s.skip("execution_role=%s 无工具题(角色分流)" % role, reason="not_applicable")
        else:
            tool_ok, detail = _tool_run(args.key, args.model, REPORT_DIR.parent / "onboard_wd" / args.key)
            (s.ok if tool_ok else s.fail)(detail)

    s = Step("6_stability"); steps.append(s)
    with s:
        stable = ((bool(capability_runs) and all(x["ok"] for x in capability_runs))
                  if not is_answer_agent else
                  (bool(runs) and all(x["ok"] and x["final"] for x in runs)))
        probe = R.probe(args.key)
        runtime_ready = bool(probe.get("available")) if not is_answer_agent else bool(probe.get("runnable"))
        (s.ok if stable and runtime_ready else s.fail)(
            "runs_ok=%d/%d tool_ok=%s probe=%s" %
            (sum(1 for x in (capability_runs if not is_answer_agent else runs) if x["ok"]),
             len(capability_runs if not is_answer_agent else runs), tool_ok, probe.get("status")))

    s = Step("7_modes"); steps.append(s)
    with s:
        bench = pathlib.Path(__file__).resolve().parents[2] / "dev" / "benchmark_execution_modes.py"
        if not is_answer_agent:
            s.skip("execution_role=%s 不进答题模式矩阵(角色分流)" % role, reason="not_applicable")
        elif not bench.is_file():
            s.skip("当前发行包未附带模式矩阵；本环未运行，前序结果保留", reason="not_included")
        else:
            try:
                import tempfile
                REPORT_DIR.mkdir(parents=True, exist_ok=True)
                output = pathlib.Path(tempfile.mkdtemp(prefix="modes-", dir=REPORT_DIR)) / "matrix.json"
                command_ok, diagnostic = R._run_probe_command(
                    [sys.executable, str(bench), "--keys", args.key,
                     "--question", "用一句话解释什么是缓存", "--model", args.model,
                     "--output", str(output)], cwd=bench.parent.parent, timeout_s=600)
                matrix = (json.loads(output.read_text())
                          if output.is_file() and not output.is_symlink() else {})
                modes = {row.get("mode") for row in matrix.get("runs") or []}
                required = {"single_harness", "parallel_pk", "serial_relay", "orchestrate"}
                if command_ok and required <= modes:
                    s.ok("modes=%s report=%s" % (sorted(modes), output))
                else:
                    s.fail("模式矩阵未通过 command_ok=%s modes=%s diagnostic=%s report=%s" %
                           (command_ok, sorted(modes), diagnostic[-120:], output))
            except Exception as exc:
                s.fail("%s:%s" % (type(exc).__name__, exc))

    passed = sum(1 for x in steps if x.status == "PASS")
    failed = sum(1 for x in steps if x.status == "FAIL")
    skipped = sum(1 for x in steps if x.status == "SKIP")
    incomplete = any(x.status == "SKIP" and x.reason != "not_applicable" for x in steps)
    verdict = "FAIL" if failed else "PARTIAL" if incomplete else "PASS"

    print()
    print("%-14s%-7s%9s  说明" % ("环节", "结果", "耗时"))
    for x in steps:
        print("%-14s%-7s%7sms  %s" % (x.name, x.status, x.ms, x.detail))
    print()
    print("结论:%s  PASS=%d FAIL=%d SKIP=%d" % (verdict, passed, failed, skipped))
    print("速度线:avg<=%ss(用户钉死5-10s)·单轮硬顶%ss·工具题必须真落盘" % (SPEED_TARGET_AVG, SPEED_HARD_SINGLE))

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / ("%s-%d.json" % (args.key, time.time_ns()))
    out.write_text(json.dumps({
        "key": args.key, "model": args.model, "verdict": verdict,
        "steps": [{"name": x.name, "status": x.status, "reason": x.reason,
                   "ms": x.ms, "detail": x.detail} for x in steps],
        "speed_runs": runs,
        "capability_runs": capability_runs,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print("报告:%s" % out)
    return 1 if failed else 2 if incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
