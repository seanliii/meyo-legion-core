# -*- coding: utf-8 -*-
"""共享 skill 源测试:真读 clone 下来的 pm-skills。"""
import os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "meyo_harnesses"))
import skills


def test_pm_skills_live_catalog():
    rows = skills.list_skills("pm-skills")
    assert len(rows) >= 2, rows
    assert all(r["path"].endswith("SKILL.md") for r in rows)


def test_pm_skills_live_read():
    row = skills.list_skills("pm-skills")[0]
    out = skills.read_skill(row["source"], row["path"])
    assert "content" in out and len(out["content"]) > 50, out


def test_skill_path_traversal_blocked():
    out = skills.read_skill("pm-skills", "../../../../etc/passwd")
    assert out["error"] == "skill_path_not_allowed"


def test_unknown_source_safe():
    out = skills.read_skill("nope", "x/SKILL.md")
    assert out["error"] == "unknown_skill_source"
