#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试隔离 env 守卫(2026-09-02 血泪固化:外部家体检被 MEYO_LEGION_DIR 误伤)。

病根:MEYO_LEGION_DIR 会把 external_agent_registry.source_dir 的 repo 根一起重定向——
测试里设了它再 probe/run 真外部家(qwen-code/ironclaw…),repo 不在隔离目录→
ready 家被误判 not_installed/运行探针未通过(我实测"秒error"·以为是家病差点误修)。

守卫:静态扫描 tests/*.py——凡文件顶部设了 MEYO_LEGION_DIR(隔离)且文件内出现
【真 catalog 家 key】的 probe/run_one 调用 → 红·指名文件行。
用假 key(__ut_前缀)或显式子进程 unset 的测试不受影响(零误伤设计)。
"""
import json
import os
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
SERVICE = HERE.parent
sys.path.insert(0, str(SERVICE))


def _real_catalog_keys() -> set:
    try:
        import external_agent_registry as R
        os.environ.pop("MEYO_LEGION_DIR", None)
        return {str(x["key"]) for x in R.load_catalog()}
    except Exception:
        return set()


def test_isolated_tests_must_not_probe_real_external_keys():
    # 豁免:守卫自身/明确声明豁免的文件
    EXEMPT = {"test_isolation_env_guards.py"}
    keys = _real_catalog_keys()
    assert keys, "catalog 不可读·守卫自身失效"
    offenders = []
    for tf in sorted(HERE.glob("test_*.py")):
        if tf.name in EXEMPT:
            continue
        text = tf.read_text(encoding="utf-8", errors="replace")
        if "MEYO_LEGION_DIR" not in text:
            continue   # 没设隔离env·不适用
        # 找真家key的调用:R.probe("<key>") / run_one("<key>", ...) / R.run("<key>", ...)
        for m in re.finditer(r'(?:probe|run_one|R\.run)\(\s*[\'"]([a-z0-9_.-]+)[\'"]', text):
            key = m.group(1)
            if key in keys:
                line_no = text[:m.start()].count("\n") + 1
                offenders.append(f"{tf.name}:{line_no} probe/run 隔离env下的真家 {key}")
    assert not offenders, (
        "隔离env测试里探测真外部家(会被repo重定向误伤·ready家误判not_installed):\n  "
        + "\n  ".join(offenders)
        + "\n修法:用 __ut_ 前缀假家做单测;要测真家就在子进程 unset MEYO_LEGION_DIR 后跑。"
    )


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    ok = 0
    for fn in fns:
        try:
            fn(); ok += 1; print(f"  ✅ {fn.__name__}")
        except AssertionError as e:
            print(f"  ❌ {fn.__name__}: {e}")
        except Exception as e:
            print(f"  💥 {fn.__name__}: {type(e).__name__} {e}")
    print(f"{ok}/{len(fns)} passed")
    sys.exit(0 if ok == len(fns) else 1)
