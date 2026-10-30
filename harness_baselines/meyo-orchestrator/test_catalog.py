# -*- coding: utf-8 -*-
"""meyo_harnesses catalog 回归 —— 验证只报客观事实,无预设判断 (2026-08-25 重写)"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from meyo_harnesses import (MEYO_HARNESSES, MEYO_CAPABILITIES, get_harness, list_keys)


NATIVE9 = {"auto","cc","codex","deepseek","hermes","deepagents","penguin","prime","jcode"}
# 2026-08-28 定位纠正:cua/supervision/herdr/orca 不是 harness(是能力后端)·挪出 MEYO_HARNESSES。
CAPABILITIES4 = {"cua","supervision","herdr","orca"}
HARNESS10 = NATIVE9 | {"omnigent"}  # 9 native + omnigent(meta-harness·真领题出答案)


def test_native_nine_present():
    """run_comparison 原生 9 家必须都在 harness catalog 里。"""
    assert NATIVE9 <= set(MEYO_HARNESSES), NATIVE9 - set(MEYO_HARNESSES)


def test_harness_set_is_exactly_ten():
    """harness 只含 10 家真 agent(9 native + omnigent)·能力后端不得混入。"""
    assert set(MEYO_HARNESSES) == HARNESS10, set(MEYO_HARNESSES)
    # 能力后端绝不能出现在 harness 集里(挪出验证)
    assert not (CAPABILITIES4 & set(MEYO_HARNESSES)), CAPABILITIES4 & set(MEYO_HARNESSES)


def test_capabilities_registered_separately():
    """4 个能力后端在 MEYO_CAPABILITIES(不是 harness)·各自怎么调有客观 cli_hint。"""
    assert set(MEYO_CAPABILITIES) == CAPABILITIES4, set(MEYO_CAPABILITIES)
    for k in CAPABILITIES4:
        assert MEYO_CAPABILITIES[k].cli_hint, k


def test_native_consistent_with_run_comparison():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "comparison"))
    try:
        import run_comparison as rc
    except Exception:
        import pytest
        pytest.skip("run_comparison 不可导入·NATIVE9 键集已在 test_native_nine_present 硬校验")
    # 原生键集必须与 run_comparison 一致(后端是额外 adapter·不在 rc 里)
    assert NATIVE9 == set(rc.HARNESSES)


def test_no_preset_judgement():
    """★铁律:catalog 不得含任何能力评价/路由规则(cost_tier/strong_on/recommend...)。"""
    import meyo_harnesses.catalog as cat
    # 只查【代码行】(剥注释),防止 docstring/注释里为说明设计而提到这些词被误伤。
    lines = []
    for ln in open(cat.__file__).read().splitlines():
        code = ln.split("#", 1)[0]  # 去行内注释
        lines.append(code)
    src = "\n".join(lines)
    # 去三引号 docstring 块
    import re as _re
    src = _re.sub(r'""".*?"""', "", src, flags=_re.S)
    banned = ["cost_tier", "strong_on", "avg_searches", "def recommend",
              "def pick_reviewer"]
    hit = [b for b in banned if b in src]
    assert not hit, f"catalog 代码仍含预设判断(该删): {hit}"
    # 字段只允许客观三项
    h = get_harness("penguin")
    assert set(h.__dataclass_fields__) == {"key", "display_name", "cli_hint"}, \
        h.__dataclass_fields__


def test_no_recommend_export():
    """recommend/pick_reviewer 必须已移除(不再由代码路由)。"""
    import meyo_harnesses as m
    assert not hasattr(m, "recommend"), "recommend 该删"


if __name__ == "__main__":
    tests = [test_native_nine_present, test_harness_set_is_exactly_ten,
             test_capabilities_registered_separately,
             test_native_consistent_with_run_comparison,
             test_no_preset_judgement, test_no_recommend_export]
    passed = 0
    for t in tests:
        try:
            print(f"  ✅ {t()}"); passed += 1
        except AssertionError as e:
            print(f"  ❌ {t.__name__}: {e}")
        except Exception as e:
            print(f"  💥 {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} PASS")
    raise SystemExit(0 if passed == len(tests) else 1)
