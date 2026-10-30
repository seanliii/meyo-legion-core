# -*- coding: utf-8 -*-
"""文档诚实性回归 —— 防止任何文档宣称与真实评测矛盾的结论。"""
import json
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def test_key_docs_exist():
    for f in ("AUDIT_2026-08-26.md", "PLAIN_LANGUAGE_OVERVIEW.md",
              "ARCHITECTURE_AND_ROADMAP.md"):
        assert (_HERE / f).is_file(), f


def test_readme_points_to_audit():
    """README 顶部必须指向诚实审计,避免读者误以为已是成品赢家。"""
    txt = (_HERE / "README.md").read_text()
    assert "AUDIT_2026-08-26.md" in txt
    assert "未超过" in txt          # 聚合无效结论仍在
    assert "21/40" in txt           # 级联有效结论也在(诚实分层)


def test_posthoc_aggregation_not_exceeding_but_cascade_does():
    """事实分层:post-hoc 聚合未超单家;但置信级联(cascade)真实超过单家。"""
    d = json.loads((_HERE / "captain_traces" / "team_vs_single_eval.json").read_text())
    best10 = d["best_single_hits"]
    agg = max(d["team_v1_hits"], d["team_v2_hits"], d.get("team_v3_evidence_hits", 0))
    assert agg <= best10, (agg, best10)              # 聚合无效
    c = json.loads((_HERE / "captain_traces" / "cascade_eval.json").read_text())
    assert c["cascade_total"] > c["cc_base"], c      # 级联有效


def test_historical_docs_marked_superseded():
    """旧文档必须带过时标注并指向 AUDIT,避免误读为当前结论。"""
    for f in ("ARCHITECTURE_AND_ROADMAP.md", "AUTONOMY_FINDINGS.md"):
        txt = (_HERE / f).read_text()
        assert "AUDIT_2026-08-26.md" in txt, f
        assert ("历史" in txt or "过时" in txt), f


def test_no_doc_falsely_claims_surpassing_single():
    """审计/人话文档不得出现"组队已超过单家"这类与评测矛盾的正面断言。"""
    banned = ["组队已超过", "已超过所有单家", "已超过最强单家", "超过所有单 Agent 更强"]
    for f in ("AUDIT_2026-08-26.md", "PLAIN_LANGUAGE_OVERVIEW.md"):
        txt = (_HERE / f).read_text()
        for b in banned:
            assert b not in txt, (f, b)
