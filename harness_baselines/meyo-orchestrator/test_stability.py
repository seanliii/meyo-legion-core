# -*- coding: utf-8 -*-
"""稳定性故障注入回归:网关404/400/降级、熔断/恢复、PID僵尸清理、健康快照。"""
import os, sys
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE / "meyo_harnesses"))
import autoloop as A
import health as H


class HttpErr(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(f"http {code}")


def test_gateway_404_retries_then_recovers():
    good = {"choices": [{"message": {"content": "ok"}}]}
    calls = [HttpErr(404), HttpErr(404), good]
    with mock.patch.object(A, "_post_once", side_effect=calls) as p, \
         mock.patch("time.sleep") as sl:
        out = A._post([])
    assert out == good and p.call_count == 3
    assert [x.args[0] for x in sl.call_args_list] == [2.0, 4.0]
    assert all(c.args[1] == A.MODEL for c in p.call_args_list)


def test_gateway_nonretryable_400_fails_fast():
    with mock.patch.object(A, "_post_once", side_effect=HttpErr(400)) as p, \
         mock.patch("time.sleep") as sl:
        try:
            A._post([])
            assert False, "expected HttpErr"
        except HttpErr as e:
            assert e.code == 400
    assert p.call_count == 1 and sl.call_count == 0


def test_gateway_three_failures_falls_back():
    good = {"choices": [{"message": {"content": "fallback-ok"}}]}
    with mock.patch.object(A, "_post_once",
                           side_effect=[HttpErr(503), HttpErr(503), HttpErr(503), good]) as p, \
         mock.patch("time.sleep"):
        out = A._post([])
    assert out == good and p.call_count == 4
    assert p.call_args_list[-1].args[1] == A.FALLBACK_MODEL


def _isolated_breaker(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(H, "_BREAKER_FILE", tmp_path / "breakers.json")


def test_circuit_opens_after_three_failures_and_recovers(tmp_path, monkeypatch):
    _isolated_breaker(tmp_path, monkeypatch)
    for i in range(2):
        s = H.record_result("slow", ok=False, now=100 + i)
        assert s["state"] == "closed"
    s = H.record_result("slow", ok=False, now=102)
    assert s["state"] == "open"
    assert H.circuit_open("slow", now=103)
    # 冷却到期→半开(允许一次探测);成功→关闭并清失败计数
    assert not H.circuit_open("slow", now=102 + H.BREAKER_COOLDOWN_S + 1)
    s = H.record_result("slow", ok=True, elapsed_s=1, now=300)
    assert s["state"] == "closed" and s["fails"] == 0


def test_run_harness_shortcircuits_open_circuit(tmp_path, monkeypatch):
    import time
    _isolated_breaker(tmp_path, monkeypatch)
    for _ in range(H.BREAKER_FAIL_THRESHOLD):
        H.record_result("doomed", ok=False, now=time.time())
    # 真执行函数若被调就炸;应在它之前被熔断短路
    with mock.patch.object(A.O, "run_harness", side_effect=AssertionError("不该真调")):
        out = A._run_harness_tool("doomed", "x")
    assert out["status"] == "circuit_open"


def test_pid_zombie_reaping(tmp_path):
    dead = tmp_path / "99999999.pid"
    dead.write_text("99999999 old-q")
    live = tmp_path / f"{os.getpid()}.pid"
    live.write_text(f"{os.getpid()} live-q")
    reaped = H.reap_zombies(tmp_path)
    assert 99999999 in reaped and not dead.exists()
    assert live.exists()


def test_register_and_cleanup_pid(tmp_path):
    pf = H.register_pid(tmp_path, "q123")
    assert pf and pf.exists() and "q123" in pf.read_text()
    pf.unlink()
    assert not pf.exists()


def test_judge_answer_hard_timeout(monkeypatch):
    """裁判卡死 → judge_answer 硬墙钟熔断(治 live 暴露的 auto 裁判 >210s 卡死)。"""
    import time as _t
    sys.path.insert(0, str(_HERE))
    import orchestrator as O
    def _hang(*a, **k):
        _t.sleep(10); return {"final": "late", "status": "ok"}
    monkeypatch.setattr(O, "run_harness", _hang)
    t0 = _t.time()
    out = O.judge_answer("q", "a", judge_harness="auto", timeout_s=1)
    assert out["status"] == "judge_timeout", out
    assert _t.time() - t0 < 5, "应快速熔断·不等满裁判耗时"


def test_health_snapshot(tmp_path, monkeypatch):
    import time
    _isolated_breaker(tmp_path, monkeypatch)
    for _ in range(H.BREAKER_FAIL_THRESHOLD):
        H.record_result("bad", ok=False, now=time.time())
    snap = H.health_snapshot(run_dir=tmp_path)
    assert "bad" in snap["circuits_open"]
    assert "breakers" in snap and "active_pids" in snap
