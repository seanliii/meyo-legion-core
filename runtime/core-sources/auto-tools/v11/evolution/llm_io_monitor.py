"""v13.88.74 · llm_io_monitor · 模型 IO 全场景监控

用户 2026-07-28: 「覆盖全场景监控 (模型 IO + 对话双向 + 执行过程)」
                「监控: LLM IO / 用户对话 / AI 自己对话 / 执行过程 / corrections / alert」

8 环里补齐「模型 IO」缺口:
  · 之前有: turn_hook (用户对话) · conversation_history_index (历史)
  · 缺: 每次 LLM 调用的 IO 层监控 (输入/输出/延迟/首字节/异常)

记录维度:
  · in_len (输入 token 粗估) · out_len (输出)
  · elapsed_s · first_byte_s (首字节)
  · model (haiku/opus/sonnet)
  · caller (哪个 agent 调的)
  · ok / err
  · anomaly (空返回·超时·异常大输出)

用法:
  from llm_io_monitor import record_call, monitor  # 装饰器
  @monitor(caller="qa_agent")
  def my_llm_call(...): ...

  或手动:
  record_call(caller="x", model="haiku", in_len=100, out_len=50, elapsed_s=6.2, ok=True)

CLI:
  python3 llm_io_monitor.py --stats 24   · 近 24h 统计
  python3 llm_io_monitor.py --anomalies  · 异常调用
  python3 llm_io_monitor.py --heartbeat
"""
import os
import sys
import json
import time
import functools
import argparse
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
V11 = HERE.parent
IO_LOG = V11 / ".llm_io_monitor.jsonl"

# 异常阈值
SLOW_THRESHOLD_S = 60      # >60s = 慢
EMPTY_OUT_MAX = 2          # 输出 < 2 字 = 疑似空返回
HUGE_OUT_CHARS = 50000     # 输出 > 50k = 异常大


def _detect_anomaly(in_len: int, out_len: int, elapsed_s: float,
                    ok: bool) -> list:
    """检测这次调用的异常"""
    anomalies = []
    if not ok:
        anomalies.append("call_failed")
    if elapsed_s and elapsed_s > SLOW_THRESHOLD_S:
        anomalies.append(f"slow_{int(elapsed_s)}s")
    if ok and out_len is not None and out_len < EMPTY_OUT_MAX:
        anomalies.append("empty_output")
    if out_len and out_len > HUGE_OUT_CHARS:
        anomalies.append(f"huge_output_{out_len}")
    return anomalies


def record_call(caller: str, model: str = "?",
                in_len: int = 0, out_len: int = 0,
                elapsed_s: float = None, first_byte_s: float = None,
                ok: bool = True, err: str = "") -> dict:
    """记录一次 LLM 调用 IO · 落 .llm_io_monitor.jsonl"""
    anomalies = _detect_anomaly(in_len, out_len, elapsed_s or 0, ok)
    entry = {
        "ts": datetime.now().isoformat(),
        "caller": caller,
        "model": model,
        "in_len": in_len,
        "out_len": out_len,
        "elapsed_s": round(elapsed_s, 2) if elapsed_s else None,
        "first_byte_s": round(first_byte_s, 2) if first_byte_s else None,
        "ok": ok,
        "err": err[:200] if err else "",
        "anomalies": anomalies,
    }
    try:
        with open(IO_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

    # 严重异常 · 报警 (throttle)
    if anomalies and any(a.startswith("slow_") or a == "empty_output"
                         or a == "call_failed" for a in anomalies):
        try:
            sys.path.insert(0, str(HERE))
            from alert_bus import notify
            notify(
                "llm_io_monitor", "WARN",
                f"LLM IO 异常: {caller}/{model} · {anomalies} · elapsed={elapsed_s}s",
                throttle_min=30,
                owner="MONITOR",
            )
        except Exception:
            pass

    return entry


def monitor(caller: str = "?", model: str = "?"):
    """装饰器 · 自动记录被包函数的 IO
    用法:
      @monitor(caller="qa_agent", model="opus")
      def gen_narrative(prompt): ...
    假设被包函数第一个 str 参数是输入·返回值是输出 (str 或 dict)
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            # 输入长度 (第一个 str 参数)
            in_len = 0
            for a in args:
                if isinstance(a, str):
                    in_len = len(a)
                    break
            t0 = time.time()
            ok = True
            err = ""
            result = None
            try:
                result = fn(*args, **kwargs)
                return result
            except Exception as e:
                ok = False
                err = f"{type(e).__name__}: {e}"
                raise
            finally:
                elapsed = time.time() - t0
                # 输出长度
                out_len = 0
                if isinstance(result, str):
                    out_len = len(result)
                elif isinstance(result, dict):
                    out_len = len(json.dumps(result, ensure_ascii=False))
                record_call(caller=caller, model=model,
                            in_len=in_len, out_len=out_len,
                            elapsed_s=elapsed, ok=ok, err=err)
        return wrapper
    return deco


def _load_recent(hours: int = 24) -> list:
    if not IO_LOG.exists():
        return []
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    out = []
    for line in IO_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
            if d.get("ts", "") >= cutoff:
                out.append(d)
        except Exception:
            continue
    return out


def stats(hours: int = 24) -> dict:
    """近 N 小时 LLM IO 统计"""
    rows = _load_recent(hours)
    if not rows:
        return {"n_calls": 0, "note": "无记录"}
    from collections import Counter
    by_caller = Counter(r["caller"] for r in rows)
    by_model = Counter(r["model"] for r in rows)
    n_ok = sum(1 for r in rows if r.get("ok"))
    n_anomaly = sum(1 for r in rows if r.get("anomalies"))
    elapsed_vals = [r["elapsed_s"] for r in rows if r.get("elapsed_s")]
    avg_elapsed = round(sum(elapsed_vals) / len(elapsed_vals), 1) if elapsed_vals else None
    return {
        "hours": hours,
        "n_calls": len(rows),
        "n_ok": n_ok,
        "n_fail": len(rows) - n_ok,
        "n_anomaly": n_anomaly,
        "avg_elapsed_s": avg_elapsed,
        "by_caller": dict(by_caller.most_common(10)),
        "by_model": dict(by_model),
    }


def anomalies(hours: int = 24) -> list:
    """近 N 小时的异常调用"""
    return [r for r in _load_recent(hours) if r.get("anomalies")]


def heartbeat() -> dict:
    entry = {
        "ts": datetime.now().isoformat(),
        "kind": "heartbeat",
        "ok": True,
        "n_calls_24h": len(_load_recent(24)),
    }
    try:
        with open(IO_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return entry


def selftest() -> dict:
    """v13.88.74 · 真录 3 类调用 (正常/慢/空) · 验异常检测"""
    checks = []
    # 正常
    r1 = record_call("selftest", "haiku", in_len=100, out_len=50,
                     elapsed_s=6.0, ok=True)
    checks.append({"case": "normal", "anomalies": r1["anomalies"],
                   "ok": r1["anomalies"] == []})
    # 慢
    r2 = record_call("selftest", "opus", in_len=200, out_len=100,
                     elapsed_s=90.0, ok=True)
    checks.append({"case": "slow", "anomalies": r2["anomalies"],
                   "ok": any("slow" in a for a in r2["anomalies"])})
    # 空返回
    r3 = record_call("selftest", "haiku", in_len=50, out_len=0,
                     elapsed_s=3.0, ok=True)
    checks.append({"case": "empty", "anomalies": r3["anomalies"],
                   "ok": "empty_output" in r3["anomalies"]})
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", type=int, nargs="?", const=24, metavar="HOURS")
    ap.add_argument("--anomalies", action="store_true")
    ap.add_argument("--heartbeat", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.heartbeat:
        r = heartbeat()
    elif args.selftest:
        r = selftest()
    elif args.anomalies:
        r = {"anomalies": anomalies(24)}
    elif args.stats is not None:
        r = stats(args.stats)
    else:
        r = selftest()

    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print("═══ llm_io_monitor ═══")
        print(json.dumps(r, ensure_ascii=False, indent=2)[:1000])

    sys.exit(0 if r.get("ok") is not False else 1)


if __name__ == "__main__":
    try:
        main()
    except Exception as _e:
        import traceback as _tb, sys as _s, os as _o
        _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
        try:
            from alert_bus import notify as _n
            _n(_o.path.basename(__file__).replace(".py",""), "CRITICAL",
               f"{_o.path.basename(__file__)} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        except Exception:
            pass
        _tb.print_exc()
        raise
