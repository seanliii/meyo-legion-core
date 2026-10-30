# -*- coding: utf-8 -*-
"""HarnessCtx per-call 模型传递回归 (2026-08-28 自查·先改 penguin 验证 model 传递通)。

验证根因修复:adapter 不再依赖模块级 _UNIFIED_MODEL 全局单值,改用 per-call ctx.model。
判据(context not control 在 adapter 这一环):
  ① 一个进程内可给同一家配【不同】model → 破 _UNIFIED_MODEL 全局单值(penguin 撞墙根因)
  ② 老 9 家(不传 ctx)零回归 → run_harness 的 inspect 分支正确走"老家"路径
  ③ penguin 真实 cmd 里 --model-id 是 ctx.model,不是全局默认
  ④ 不传 ctx 时 from_env() 退回读 env → 与旧行为等价
"""
import sys, os
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_registry as HR
import orchestrator as O


def test_ctx_from_env_reads_model():
    """from_env() 真读 MEYO_UNIFIED_MODEL(legion_core 塞 env → worker from_env 拿到)。"""
    with mock.patch.dict(os.environ, {"MEYO_UNIFIED_MODEL": "modelX",
                                      "MEYO_UNIFIED_APPID": "appidX"}):
        ctx = HR.HarnessCtx.from_env()
    assert ctx.model == "modelX", ctx.model
    assert ctx.appid == "appidX", ctx.appid
    return "from_env 读到 per-call model/appid"


def test_penguin_cmd_uses_ctx_model_not_global():
    """★核心:同一进程两次调 penguin,分别给不同 ctx.model → cmd 的 --model-id 分别不同。
    这直接证明破了 _UNIFIED_MODEL 全局单值:一进程可多 model,不用杀进程换。"""
    captured = []

    def _fake_run_live(cmd, timeout, **kw):
        captured.append(cmd)
        from types import SimpleNamespace as NS
        return NS(returncode=0, stdout="penguin 答案", stderr="")

    with mock.patch.object(HR, "_run_live", _fake_run_live), \
         mock.patch.object(HR._sh, "which", return_value="/bin/penguin"):
        HR._penguin_adapter("qh", "问题A", "", ctx=HR.HarnessCtx(model="model-AAA"))
        HR._penguin_adapter("qh", "问题B", "", ctx=HR.HarnessCtx(model="model-BBB"))

    def _model_id(cmd):
        return cmd[cmd.index("--model-id") + 1]
    assert _model_id(captured[0]) == "model-AAA", captured[0]
    assert _model_id(captured[1]) == "model-BBB", captured[1]
    return "同进程两次 penguin 用了不同 model(破全局单值)"


def test_penguin_no_ctx_falls_back_to_env():
    """④ 不传 ctx → from_env() 退回读 env,与旧行为等价(零回归)。"""
    captured = []

    def _fake_run_live(cmd, timeout, **kw):
        captured.append(cmd)
        from types import SimpleNamespace as NS
        return NS(returncode=0, stdout="ok", stderr="")

    with mock.patch.object(HR, "_run_live", _fake_run_live), \
         mock.patch.object(HR._sh, "which", return_value="/bin/penguin"), \
         mock.patch.dict(os.environ, {"MEYO_UNIFIED_MODEL": "env-model-Z"}):
        HR._penguin_adapter("qh", "问题", "")   # 不传 ctx
    mid = captured[0][captured[0].index("--model-id") + 1]
    assert mid == "env-model-Z", captured[0]
    return "不传 ctx 时退回读 env(旧行为等价)"


def test_run_harness_passes_ctx_to_migrated_adapter():
    """② run_harness 对已 ctx 化的 adapter(签名含 ctx=)按 kwarg 透传 ctx。"""
    seen = {}

    def _mig(qhash, question, gold="", ctx=None):
        seen["ctx"] = ctx
        return {"final": "migrated", "status": "ok"}

    with mock.patch.object(O, "_load_harnesses", return_value={"penguin": _mig}):
        out = O.run_harness("penguin", "task", "qh", "", ctx=HR.HarnessCtx(model="M9"))
    assert out["final"] == "migrated"
    assert seen["ctx"] is not None and seen["ctx"].model == "M9", seen
    return "run_harness 把 ctx 透传给已迁移 adapter"


def test_run_harness_legacy_adapter_zero_regression():
    """② 老家(签名不含 ctx)零改动:run_harness 传了 ctx 也不会把它塞给老家。"""
    def _legacy(qhash, question, gold=""):
        return {"final": "legacy-ok", "status": "ok"}

    with mock.patch.object(O, "_load_harnesses", return_value={"cc": _legacy}):
        # 即便外层给了 ctx,老家也不该收到(否则 TypeError)
        out = O.run_harness("cc", "task", "qh", "", ctx=HR.HarnessCtx(model="M9"))
    assert out["final"] == "legacy-ok", out
    return "老家零回归(inspect 分支不误传 ctx)"


def test_run_harness_no_ctx_still_works():
    """② 完全不传 ctx(旧调用签名)→ 走老路径,行为不变。"""
    def _legacy(qhash, question, gold=""):
        return {"final": "x", "status": "ok"}
    with mock.patch.object(O, "_load_harnesses", return_value={"prime": _legacy}):
        out = O.run_harness("prime", "task?", "qh1", "Mando")   # 旧 4 参调用
    assert out["harness"] == "prime" and out["final"] == "x"
    return "旧 4 参 run_harness 调用不变"


if __name__ == "__main__":
    tests = [test_ctx_from_env_reads_model,
             test_penguin_cmd_uses_ctx_model_not_global,
             test_penguin_no_ctx_falls_back_to_env,
             test_run_harness_passes_ctx_to_migrated_adapter,
             test_run_harness_legacy_adapter_zero_regression,
             test_run_harness_no_ctx_still_works]
    passed = 0
    for t in tests:
        try:
            print(f"  ✅ {t.__name__}: {t()}"); passed += 1
        except AssertionError as e:
            print(f"  ❌ {t.__name__}: {e}")
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"  💥 {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} PASS")
    raise SystemExit(0 if passed == len(tests) else 1)
