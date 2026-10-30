# -*- coding: utf-8 -*-
"""通过真 Omnigent supervisor 执行目标(云 UI 的可选执行后端)。

调 run_omnigent_captain.sh(内含正确 PYTHONPATH/env),捕获输出。qhash 外层固定,gold 不传。
需 `omnigent setup` 配好 provider + Omnigent 运行时依赖装齐;否则返回结构化错误(不假成功)。
"""
from __future__ import annotations
import os, subprocess, time
from pathlib import Path

_ORCH_ROOT = Path(__file__).resolve().parent.parent
_LAUNCHER = _ORCH_ROOT / "run_omnigent_captain.sh"
_DIRECT_LAUNCHER = _ORCH_ROOT / "run_captain_mcp.sh"


def run_via_supervised_omnigent(goal: str, *, parent_session_id: str,
                                 agent_id: str, agent_name: str,
                                 base_url: str, timeout_s: int = 180,
                                 event_sink=None) -> dict:
    """通过统一 provider 运行一个受监督的 Omnigent child。"""
    from orchestrator_provider import DeterministicSupervisor, HttpOmnigentProvider

    started = time.time()
    supervisor = DeterministicSupervisor(
        HttpOmnigentProvider(base_url), child_timeout_s=timeout_s,
        max_replacements=1, event_sink=event_sink,
    )
    result = supervisor.run_child(
        parent_session_id, agent_id, agent_name,
        f"supervised-{time.time_ns()}", goal,
    )
    return {**result, "final": str(result.get("answer") or ""),
            "executor": "omnigent", "elapsed": round(time.time() - started, 1)}


def run_via_omnigent(goal: str, qhash: str = "adhoc", timeout_s: int = 600,
                     context_block: str = "", event_sink=None,
                     runtime_url: str = "") -> dict:
    if context_block:
        goal = goal + "\n\n" + context_block
    base_url = (runtime_url or os.environ.get("MEYO_OMNIGENT_URL", "")).strip()
    parent_id = os.environ.get("MEYO_OMNIGENT_PARENT_SESSION_ID", "").strip()
    agent_id = os.environ.get("MEYO_OMNIGENT_DEFAULT_AGENT_ID", "").strip()
    agent_name = os.environ.get("MEYO_OMNIGENT_DEFAULT_AGENT_NAME", "meyo-cc").strip()
    if base_url and not (parent_id and agent_id):
        from orchestrator_provider import HttpOmnigentProvider
        provider = HttpOmnigentProvider(base_url)
        workspace = os.environ.get("MEYO_WORKDIR", str(Path.cwd()))
        try:
            parent = provider.ensure_parent(
                "meyo-captain", workspace=workspace,
                title=f"captain-{time.time_ns()}",
            )
            worker = provider.resolve_agent(agent_name)
            if worker is not None:
                parent_id = str(parent["id"])
                agent_id = str(worker["id"])
        except Exception:
            # 无已注册 captain/worker 时保留现有 CLI 路径；调用方随后按交付门 fallback。
            pass
    if base_url and parent_id and agent_id:
        return run_via_supervised_omnigent(
            goal, parent_session_id=parent_id, agent_id=agent_id,
            agent_name=agent_name, base_url=base_url,
            timeout_s=min(timeout_s, 300), event_sink=event_sink,
        )
    t0 = time.time()
    if not _LAUNCHER.is_file():
        return {"final": "", "status": "launcher_missing"}
    try:
        r = subprocess.run(["bash", str(_LAUNCHER), goal, qhash],
                           capture_output=True, text=True, timeout=timeout_s)
        out = (r.stdout or "")[-8000:]
        ok = r.returncode == 0
        return {"final": out.strip()[-4000:], "status": "ok" if ok else f"rc{r.returncode}",
                "stderr": (r.stderr or "")[-2000:], "elapsed": round(time.time() - t0, 1),
                "executor": "omnigent"}
    except subprocess.TimeoutExpired:
        return {"final": "", "status": "timeout", "elapsed": round(time.time() - t0, 1),
                "executor": "omnigent"}
    except Exception as e:
        return {"final": "", "status": f"err:{type(e).__name__}:{str(e)[:150]}",
                "executor": "omnigent"}


def run_via_captain_mcp(goal: str, qhash: str = "adhoc", timeout_s: int = 300,
                        context_block: str = "") -> dict:
    if context_block:
        goal = goal + "\n\n" + context_block
    """真 Claude captain 直连我方 orchestrator MCP(已验证:真 LLM 真调我方工具)。

    这是 Omnigent native-Claude relay 未 materialize 外部 MCP 时的可用生产路径。
    """
    import json as _json
    t0 = time.time()
    if not _DIRECT_LAUNCHER.is_file():
        return {"final": "", "status": "launcher_missing", "executor": "captain"}
    try:
        r = subprocess.run(["bash", str(_DIRECT_LAUNCHER), goal],
                           capture_output=True, text=True, timeout=timeout_s,
                           cwd=str(_ORCH_ROOT))
        try:
            data = _json.loads(r.stdout)
            final = str(data.get("result", ""))
            ok = data.get("is_error") is False
        except Exception:
            final = (r.stdout or "")[-4000:]; ok = r.returncode == 0
        return {"final": final[-4000:], "status": "ok" if ok else "error",
                "executor": "captain", "elapsed": round(time.time() - t0, 1),
                "stderr": (r.stderr or "")[-1500:]}
    except subprocess.TimeoutExpired:
        return {"final": "", "status": "timeout", "executor": "captain",
                "elapsed": round(time.time() - t0, 1)}
    except Exception as e:
        return {"final": "", "status": f"err:{type(e).__name__}:{str(e)[:150]}",
                "executor": "captain"}
