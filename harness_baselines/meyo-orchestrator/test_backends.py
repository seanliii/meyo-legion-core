# -*- coding: utf-8 -*-
"""后端 adapter 契约 + 诚实探测测试。

不要求外部 CLI 已装:未装的家必须诚实返回 unavailable(+reason),不得假跑。
已装的家(supervision pip / herdr build)则真跑连通性动作。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "meyo_harnesses"))
from backends import BACKEND_HARNESSES, BACKEND_PROBES


def test_all_backends_have_runner_and_probe():
    assert set(BACKEND_HARNESSES) == {"cua", "supervision", "herdr", "orca"}
    assert set(BACKEND_PROBES) == set(BACKEND_HARNESSES)


def test_runner_contract():
    """每个 runner 返回 dict·含 harness/status/final·不抛异常。"""
    for key, fn in BACKEND_HARNESSES.items():
        out = fn("test-qid", "connectivity check", "")
        assert isinstance(out, dict), (key, out)
        assert out.get("harness") == key, (key, out)
        assert "status" in out and "final" in out, (key, out)


def test_probe_honesty():
    """probe 必须给 available 布尔;不可用时给 reason,不谎报可用。"""
    for key, probe in BACKEND_PROBES.items():
        p = probe()
        assert isinstance(p.get("available"), bool), (key, p)
        if not p["available"]:
            assert p.get("reason"), (key, p)


def test_unavailable_backends_do_not_fake():
    """未装的家 run() 必须 status=unavailable + 空 final(诚实·不假跑)。"""
    for key, fn in BACKEND_HARNESSES.items():
        p = BACKEND_PROBES[key]()
        if not p["available"]:
            out = fn("q", "x", "")
            assert out["status"] == "unavailable", (key, out)
            assert out["final"] == "", (key, out)


def test_pool_merge_exposes_backends():
    """orchestrator._load_harnesses 合并后应能看到后端(未装也在池里·调用返回unavailable)。"""
    import orchestrator as O
    pool = O._load_harnesses()
    for key in BACKEND_HARNESSES:
        assert key in pool, key


def test_herdr_live_runtime_and_socket_api():
    """Herdr binary 在位时:真起 headless server→snapshot→真实 terminal IO→prompt/wait API。"""
    from backends import herdr_backend as h
    if not h.probe()["available"]:
        import pytest
        pytest.skip("herdr binary 未构建")
    assert h.ensure_server(), "herdr headless server 启动失败"
    snap = h.api_snapshot()
    assert "snapshot" in snap.get("result", {}), snap
    ws = h.create_workspace("/tmp", "meyo-pytest")
    pane = ws.get("result", {}).get("root_pane", {}).get("pane_id")
    assert pane, ws
    marker = "HERDR_PYTEST_42"
    h.pane_send_text(pane, f"echo {marker}\n")
    waited = h.pane_wait_output(pane, marker, 5000)
    # 真实终端里该 marker 至少出现在输出快照中(命令行 + 执行结果)
    text = waited.get("result", {}).get("read", {}).get("text", "")
    assert marker in text and marker in waited.get("result", {}).get("matched_line", ""), waited
    # shell pane 不是 agent,但 prompt/wait 已真走 socket API,应返回结构化 agent_not_found
    pr = h.agent_prompt(pane, "hi", wait=False)
    wt = h.agent_wait(pane, "unknown", 1000)
    assert pr.get("error", {}).get("code") == "agent_not_found", pr
    assert wt.get("error", {}).get("code") == "agent_not_found", wt
