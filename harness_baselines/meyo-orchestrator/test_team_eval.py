# -*- coding: utf-8 -*-
"""组队评测安全/评分回归(不调用真实模型)。"""
import sys
from pathlib import Path
from unittest import mock

_HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(_HERE))
import team_eval as T


def test_keyword_hit_multi_part():
    assert T._keyword_hit("Eldorado Stadium — December 6, 2019",
                          "Eldorado Stadium; December 6, 2019")
    assert not T._keyword_hit("Eldorado Stadium", "Eldorado Stadium; December 6, 2019")


def test_selector_prompt_anonymizes_harnesses_and_hides_gold():
    captured={}
    def fake(msgs):
        captured["blob"]=str(msgs)
        return "Plastic Man"
    with mock.patch.object(T,"_post",side_effect=fake):
        out=T.team_select("who?",["Plastic Man","Batman"])
    assert out=="Plastic Man"
    assert "候选1" in captured["blob"] and "候选2" in captured["blob"]
    for h in T.HARNESSES:
        assert h not in captured["blob"], h


def test_v2_extracts_final():
    with mock.patch.object(T,"_post",return_value="分析...\nFINAL: University of Aberdeen"):
        assert T.team_select_v2("q",["A","B"])=="University of Aberdeen"


def test_extract_final_strips_bullet_marker():
    assert T._extract_final("分析...\n- Eldorado Stadium") == "Eldorado Stadium"


def test_agreement_calibration_proves_unreliable():
    """40 题校准:样本≥5 的家对里,没有一对精度≥0.6 → 一致门控不可靠(架构定论依据)。"""
    import calibrate_agreement as C
    r = C.calibrate()
    strong = [p for p in r["pairs"] if p["agree"] >= 5]
    assert strong, "无足够样本对"
    assert max(p["precision"] for p in strong) < 0.6, strong


def test_agreement_gate_accepts_consensus_and_rejects_disagreement():
    ans,ok=T.agreement_gate({'deepseek':'Plastic Man','codex':'Plastic Man','cc':'Batman'})
    assert ok and ans=='Plastic Man'
    ans,ok=T.agreement_gate({'deepseek':'Cambridge','codex':'Oxford'})
    assert not ok and ans=='UNKNOWN'


def test_v6_constraint_matrix_selects_supported_minority():
    plan='{"constraints":["c1"],"candidates":[{"id":0,"queries":["q0"]},{"id":1,"queries":["q1"]}]}'
    scores='{"scores":[{"id":0,"support":0,"contradict":1,"unknown":0,"score":20},{"id":1,"support":1,"contradict":0,"unknown":0,"score":90}]}'
    with mock.patch.object(T,"_post",side_effect=[plan,scores]), \
         mock.patch.object(T,"_search",return_value="evidence"):
        assert T.team_select_v6_constraint_matrix("q",["Wrong","Right"])=="Right"


def test_v5_minority_candidate_can_win_by_evidence_score():
    """多数错(5票) vs 少数对(1票):v5 按证据分而非票数,少数可胜。"""
    # 每个候选两次模型调用:query、score。Oxford 30, Aberdeen 90。
    replies=iter(["verify Oxford","30", "verify Aberdeen","90"])
    with mock.patch.object(T,"_post",side_effect=lambda m:next(replies)), \
         mock.patch.object(T,"_search",return_value="evidence"):
        out=T.team_select_v5_verify("q",["Oxford"]*5+["Aberdeen"])
    assert out=="Aberdeen"


def test_v4_reexec_searches_then_decides():
    """分歧未解→真搜索接口→证据回灌→FINAL(单测mock网络,状态机真实)。"""
    calls=[]
    def fake_post(msgs):
        calls.append(str(msgs))
        return "SEARCH: discriminating evidence" if len(calls)==1 else "FINAL: Right Answer"
    with mock.patch.object(T,"_post",side_effect=fake_post), \
         mock.patch.object(T,"_search",return_value="- source: evidence") as s:
        out=T.team_select_v4_reexec("q",["Wrong","Right Answer"],max_rounds=2)
    assert out=="Right Answer" and s.call_count==1
    assert "source: evidence" in calls[1]
