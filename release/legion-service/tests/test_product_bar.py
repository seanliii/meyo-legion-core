"""作品类请求要带成品标准：成员拿到全文，评审拿到同一份条件。"""
import importlib
from pathlib import Path
import sys

import pytest

SERVICE = Path(__file__).resolve().parents[1]
if str(SERVICE) not in sys.path:
    sys.path.insert(0, str(SERVICE))

import product_bar  # noqa: E402
import success_auditor  # noqa: E402


@pytest.mark.parametrize("question", [
    "帮我做一个塞尔达风格的 电子乐3d世界游戏",
    "制作一个可玩的3D游戏：用这三段参考视频做同一个作品。",
    "继续原作品：继续这份塞尔达风格、电子乐驱动的3D世界游戏",
    "做一个单文件网页小游戏：接住下落的星星",
    "Build a small 3D game about a lighthouse",
    "做一个3D游戏，参考这篇教程里的光照做法",
])
def test_requests_for_a_playable_work_are_recognised(question):
    assert product_bar.is_product_request(question)


@pytest.mark.parametrize("question", [
    "写一份游戏行业分析报告",
    "写一篇3D游戏开发教程",
    "解释一下3D渲染管线",
    "5 加 4 等于多少",
    "帮我把这个接口的超时改成 30 秒",
    "",
])
def test_other_requests_are_left_alone(question):
    assert not product_bar.is_product_request(question)


def test_continuation_uses_the_original_project_goal():
    plan = {"project_context": {"user_goal": "帮我做一个塞尔达风格的 电子乐3d世界游戏"}}
    assert product_bar.is_product_request("继续", plan)
    assert not product_bar.is_product_request("继续", {"project_context": {"user_goal": "整理发票"}})


def test_switch_turns_it_off(monkeypatch):
    monkeypatch.setenv("LEGION_PRODUCT_BAR", "0")
    assert not product_bar.is_product_request("做一个3D游戏")


def test_apply_adds_the_bar_and_an_independent_review():
    plan = {}
    assert product_bar.apply("做一个3D游戏", plan, reviewers_available=True)
    assert plan["acceptance"] == list(product_bar.CRITERIA)
    assert plan["success_audit"] == {"enabled": True}


def test_apply_keeps_what_the_user_already_decided():
    plan = {"acceptance": ["必须支持手柄"], "success_audit": {"enabled": False}}
    assert product_bar.apply("做一个3D游戏", plan, reviewers_available=True)
    assert plan == {"acceptance": ["必须支持手柄"], "success_audit": {"enabled": False}}


def test_apply_does_not_ask_for_a_review_nobody_can_do():
    plan = {}
    assert product_bar.apply("做一个3D游戏", plan, reviewers_available=False)
    assert "success_audit" not in plan and plan["acceptance"]


def test_apply_ignores_other_requests():
    plan = {}
    assert not product_bar.apply("5 加 4 等于多少", plan, reviewers_available=True)
    assert plan == {}


def test_members_receive_the_whole_bar(monkeypatch):
    core = importlib.import_module("legion_core")
    monkeypatch.setattr(core, "_selection_resource_context", lambda plan, user_id="": {})
    plan = {}
    product_bar.apply("做一个3D游戏", plan, reviewers_available=True)
    text = core._build_runtime_brief("做一个3D游戏", plan)
    for item in product_bar.CRITERIA:
        assert item in text
    assert "验收标尺（无明确标准时的通用基准" not in text
    assert text.count(product_bar.CRITERIA[0]) == 1

    custom = core._build_runtime_brief("做一个3D游戏", {"acceptance": ["必须支持手柄"]})
    assert product_bar.CRITERIA[2] in custom and "必须支持手柄" in custom

    other = core._build_runtime_brief("5 加 4 等于多少", {})
    assert product_bar.CRITERIA[2] not in other
    assert "验收标尺（无明确标准时的通用基准" in other


def test_reviewer_is_given_the_same_criteria():
    plan = {}
    product_bar.apply("做一个3D游戏", plan, reviewers_available=True)
    candidates = [{"key": "codex", "harness": "codex", "final": "已交付 index.html"}]
    prompt, _aliases = success_auditor.build_prompt("做一个3D游戏", plan, candidates)
    assert product_bar.CRITERIA[2][:40] in prompt
    assert product_bar.CRITERIA[-1][:30] in prompt

    prompt, _aliases = success_auditor.build_prompt("x", {"acceptance": "必须支持手柄"}, candidates)
    assert "必须支持手柄" in prompt
