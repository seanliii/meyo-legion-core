"""v13.87 · 通用 fetcher 健康度模块 · 抄 wigolo 亮点 4
从 wigolo 那学的：engine_pool healthy/total/degraded 每次搜索都汇报

现役状态:
- .xhs_api_health.jsonl 已跑 (v13.86 · global_sources.py:713)
- .sogou_weixin_health.jsonl 单独跑 (sogou_weixin_health.py)

本模块做的:
- 泛化 log 落盘接口 (health_log(fetcher, record))
- 汇总各 fetcher 的健康度 (health_summary())
- 供 daily_report_agent 消费

设计:
- 一个 fetcher 一个 .{fetcher}_health.jsonl (append-only)
- 24h 窗口计: 调用总数 / 成功率 / 平均耗时 / degraded 阈值
- degraded 定义: 成功率 < 60% 或 全 fail
"""

import json
import os
import time
from typing import Optional


# v13.88.79 · 静默监控统一接入 · 用户 07-28「所有静默监控都到齐了吗」
def _alert(severity: str, msg: str):
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
        from alert_bus import notify as _n
        _n(_o.path.basename(__file__).replace(".py",""), severity, msg)
    except Exception:
        pass


V11 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEALTH_DIR = V11
WINDOW_S = 86400  # 24h


def health_log(fetcher: str, record: dict) -> None:
    """通用落盘 · 每次 fetcher 调用后调
    record 建议字段: ts / ok / count / elapsed_s / err / completeness / query|url
    """
    if not fetcher:
        return
    record.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))
    record["fetcher"] = fetcher
    try:
        path = os.path.join(HEALTH_DIR, f".{fetcher}_health.jsonl")
        with open(path, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _read_recent(fetcher: str, window_s: int = WINDOW_S) -> list:
    """读近 window_s 秒内的记录"""
    path = os.path.join(HEALTH_DIR, f".{fetcher}_health.jsonl")
    if not os.path.exists(path):
        return []
    cutoff = time.time() - window_s
    records = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    ts = r.get("ts", "")
                    # 解析 ISO 时间简化: 只用 unix ts 字段 or 全接
                    if "unix_ts" in r:
                        if r["unix_ts"] >= cutoff:
                            records.append(r)
                    else:
                        # 无 unix_ts 一律接 · 24h 数据本来就少
                        records.append(r)
                except Exception:
                    continue
    except Exception:
        pass
    return records


def health_summary(fetchers: Optional[list] = None,
                   window_s: int = WINDOW_S) -> dict:
    """汇总各 fetcher 健康度 · 供 daily_report 消费
    返 dict: {fetcher: {calls, ok_rate, avg_latency_s, degraded, last_err}}
    fetchers 为 None 时自动扫 HEALTH_DIR 下所有 .*_health.jsonl
    """
    if fetchers is None:
        fetchers = []
        try:
            for f in os.listdir(HEALTH_DIR):
                if f.startswith(".") and f.endswith("_health.jsonl"):
                    name = f[1:-len("_health.jsonl")]
                    fetchers.append(name)
        except Exception:
            pass

    summary = {}
    for fetcher in fetchers:
        records = _read_recent(fetcher, window_s)
        calls = len(records)
        if calls == 0:
            summary[fetcher] = {
                "calls": 0, "ok_rate": None,
                "avg_latency_s": None, "degraded": False,
                "last_err": "",
            }
            continue
        ok_count = sum(1 for r in records if r.get("ok"))
        ok_rate = ok_count / calls
        latencies = [r.get("elapsed_s", 0) for r in records
                     if isinstance(r.get("elapsed_s"), (int, float))]
        avg_lat = sum(latencies) / len(latencies) if latencies else 0
        degraded = ok_rate < 0.6 or ok_count == 0
        last_err = ""
        for r in reversed(records):
            if not r.get("ok") and r.get("err"):
                last_err = str(r["err"])[:120]
                break
        summary[fetcher] = {
            "calls": calls,
            "ok_rate": round(ok_rate, 3),
            "avg_latency_s": round(avg_lat, 2),
            "degraded": degraded,
            "last_err": last_err,
        }
    return summary


def pool_status(fetchers: Optional[list] = None) -> dict:
    """抄 wigolo engine_pool 格式 · {healthy, total, degraded}
    healthy = calls > 0 且非 degraded
    total = 有过调用记录的 fetcher 数
    degraded = 有 fetcher 处于 degraded 状态
    """
    summary = health_summary(fetchers)
    active = [name for name, s in summary.items() if s["calls"] > 0]
    healthy = [name for name in active
               if not summary[name]["degraded"]]
    return {
        "healthy": len(healthy),
        "total": len(active),
        "degraded": len(active) - len(healthy) > 0,
        "unhealthy_fetchers": [n for n in active
                               if summary[n]["degraded"]],
    }


if __name__ == "__main__":
    try:
        import sys
        if len(sys.argv) > 1 and sys.argv[1] == "summary":
            print(json.dumps(health_summary(), ensure_ascii=False, indent=2))
        elif len(sys.argv) > 1 and sys.argv[1] == "pool":
            print(json.dumps(pool_status(), ensure_ascii=False, indent=2))
        else:
            # smoke
            health_log("smoke_test", {"ok": True, "count": 3, "elapsed_s": 1.2})
            health_log("smoke_test", {"ok": False, "err": "timeout",
                                       "elapsed_s": 12.0})
            print("--- summary ---")
            print(json.dumps(health_summary(["smoke_test"]),
                             ensure_ascii=False, indent=2))
            print("--- pool ---")
            print(json.dumps(pool_status(["smoke_test"]),
                             ensure_ascii=False, indent=2))
            # cleanup
            try:
                os.remove(os.path.join(HEALTH_DIR, ".smoke_test_health.jsonl"))
            except Exception:
                pass
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise
