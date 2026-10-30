# -*- coding: utf-8 -*-
"""场景模板测试:目标实例化 + 缺参数如实标注 + 不绑定 harness(守 ADR-005)。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "meyo_harnesses"))
import templates as T


def test_ten_templates():
    ts = T.list_templates()
    assert len(ts) == 10, [t["key"] for t in ts]
    for t in ts:
        assert t["key"] and t["name"] and t["acceptance"]


def test_instantiate_fills_params():
    out = T.instantiate_template("research-brief", {"topic": "余额宝费率变化"})
    assert "余额宝费率变化" in out["goal"], out
    assert out["missing_params"] == [], out
    assert out["acceptance"]


def test_instantiate_reports_missing():
    out = T.instantiate_template("competitor-research", {})
    assert set(out["missing_params"]) == {"competitors", "dimension"}, out
    # 缺参数不猜·占位符保留
    assert "{competitors}" in out["goal"]


def test_unknown_template_safe():
    out = T.instantiate_template("nope", {})
    assert out["error"] == "unknown_template"
    assert "research-brief" in out["available"]


def test_templates_bind_no_harness():
    """★守护:模板不得指定/推荐具体 harness(执行家由模型自主选)。"""
    import json
    raw = json.loads((Path(__file__).resolve().parent / "meyo_harnesses"
                      / "scenario_templates.json").read_text())
    harness_keys = {"auto", "cc", "codex", "deepseek", "hermes", "deepagents",
                    "penguin", "prime", "jcode"}
    for t in raw["templates"]:
        # suggested_tools 只能是共享工具名·不能是 harness key
        assert not (set(t.get("suggested_tools", [])) & harness_keys), t
