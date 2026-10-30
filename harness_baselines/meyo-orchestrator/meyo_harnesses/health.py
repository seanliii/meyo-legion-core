# -*- coding: utf-8 -*-
"""稳定性/健康底座 —— 把散落在 autoloop 的容错逻辑收敛成可测模块 (2026-08-25)。

覆盖:
- 网关错误分类 + 退避序列(治 opus4.8 长会话 404/5xx/限流)。
- harness 熔断器(连续失败/超慢 → 短路一段时间;成功即恢复)。状态持久化,可复盘。
- 子进程 PID 注册 + 死僵尸清理。
- 健康快照:harness/MCP/子进程 的当前状态聚合。

设计原则:这些是【安全/成本护栏】,不是编排路由。熔断只在"客观连续失败/卡死"时短路,
不含任何"该选谁"的偏好;模型仍自主决定用不用某家(被熔断的家会被如实标 circuit_open)。
"""
from __future__ import annotations
import json, os, time
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_STATE_DIR = _ROOT / ".health"
_BREAKER_FILE = _STATE_DIR / "breakers.json"

# 可重试的网关错误码(其余如 400 参数错直接抛)。
RETRYABLE_CODES = {404, 408, 425, 429, 500, 502, 503, 504}
# 熔断阈值:连续失败次数 + 冷却秒数。
BREAKER_FAIL_THRESHOLD = 3
BREAKER_COOLDOWN_S = 120


def is_retryable(code: int | None) -> bool:
    """网关错误是否值得退避重试。code=None(网络层异常)按可重试处理。"""
    if code is None:
        return True
    return int(code) in RETRYABLE_CODES


def backoff_schedule(attempts: int, base: float = 2.0, cap: float = 30.0) -> list[float]:
    """线性退避序列(与现有 autoloop 行为一致:2,4,6...·封顶 cap)。"""
    return [min(cap, base * (i + 1)) for i in range(max(0, attempts))]


# ── 熔断器 ────────────────────────────────────────────────────────
def _load_breakers() -> dict[str, dict]:
    try:
        return json.loads(_BREAKER_FILE.read_text())
    except Exception:
        return {}


def _save_breakers(d: dict[str, dict]):
    try:
        _STATE_DIR.mkdir(exist_ok=True)
        _BREAKER_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2))
    except Exception:
        pass


def circuit_open(harness: str, now: float | None = None) -> bool:
    """该 harness 当前是否处于熔断(短路)状态。冷却到期自动半开(返回 False)。"""
    now = time.time() if now is None else now
    b = _load_breakers().get(harness)
    if not b:
        return False
    if b.get("state") == "open" and now < b.get("open_until", 0):
        return True
    return False


def record_result(harness: str, ok: bool, elapsed_s: float | None = None,
                  slow_threshold_s: float = 1260.0, now: float | None = None) -> dict:
    """记录一次调用结果,更新熔断状态。超慢(>=slow_threshold)按失败计。返回该家最新状态。"""
    now = time.time() if now is None else now
    d = _load_breakers()
    b = d.setdefault(harness, {"fails": 0, "state": "closed", "open_until": 0})
    treat_fail = (not ok) or (elapsed_s is not None and elapsed_s >= slow_threshold_s)
    if treat_fail:
        b["fails"] = b.get("fails", 0) + 1
        b["last_fail_at"] = now
        if b["fails"] >= BREAKER_FAIL_THRESHOLD:
            b["state"] = "open"
            b["open_until"] = now + BREAKER_COOLDOWN_S
    else:
        # 成功即恢复(半开→关闭)
        b["fails"] = 0
        b["state"] = "closed"
        b["open_until"] = 0
        b["last_ok_at"] = now
    _save_breakers(d)
    return dict(b)


def reset_breaker(harness: str):
    d = _load_breakers()
    if harness in d:
        del d[harness]
        _save_breakers(d)


# ── 子进程 PID / 僵尸清理 ─────────────────────────────────────────
def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True     # 存在但无权限
    except Exception:
        return False


def reap_zombies(run_dir: Path) -> list[int]:
    """清理 run_dir 下已死进程的 .pid 注册。返回被清理的 pid 列表。"""
    reaped: list[int] = []
    try:
        for f in Path(run_dir).glob("*.pid"):
            try:
                pid = int(f.read_text().strip().split()[0])
            except Exception:
                f.unlink(missing_ok=True)
                continue
            if not _pid_alive(pid):
                f.unlink(missing_ok=True)
                reaped.append(pid)
    except Exception:
        pass
    return reaped


def register_pid(run_dir: Path, qhash: str) -> Path | None:
    """注册当前进程 PID(先清死僵尸)。返回 pid 文件路径。"""
    try:
        Path(run_dir).mkdir(exist_ok=True)
        reap_zombies(run_dir)
        pf = Path(run_dir) / f"{os.getpid()}.pid"
        pf.write_text(f"{os.getpid()} {qhash}")
        return pf
    except Exception:
        return None


# ── 健康快照 ──────────────────────────────────────────────────────
def health_snapshot(run_dir: Path | None = None) -> dict[str, Any]:
    """聚合当前健康:熔断状态 + 活跃子进程 + 时间戳。供云 UI / 运维读。"""
    breakers = _load_breakers()
    now = time.time()
    open_list = [h for h in breakers if circuit_open(h, now)]
    active = []
    if run_dir:
        for f in Path(run_dir).glob("*.pid"):
            try:
                pid = int(f.read_text().strip().split()[0])
                if _pid_alive(pid):
                    active.append(pid)
            except Exception:
                continue
    return {"breakers": breakers, "circuits_open": open_list,
            "active_pids": active}
