#!/usr/bin/env python3
"""
source_health.py · v13.72 · 数据源健康度总账
────────────────────────────────────────────────────────────────
用户 2026-07-15 元规则:「少数派搜索结果空 · 你要看整条链路为啥没有校验器」

不再让每个 fetcher 自己 print '✗ fail' 吞异常
所有数据源 · 抓取动作 · 统一走 record_fetch() 上报

三层反馈:
  · 数据层:      .source_health.jsonl · 每次 fetch (source/status/count/err/ts)
  · 反思层:      连续 3 次 empty/error → 触发 pattern_reflector 生成 gene
  · 修复层:      pattern_reflector 生成的 gene 落到 memory · 供 evolution 读

调用协议:
  from source_health import record_fetch
  record_fetch(source="少数派 RSS", url=..., count=len(items), err=None)
  record_fetch(source="Reddit", url=..., count=0, err=None)  # 空返回也算
  record_fetch(source="小红书", url=..., count=None, err="429 blocked")

数据格式:
  {"ts": iso, "source": str, "url": str,
   "count": int (0=empty/正常空 · None=异常),
   "err": str (None=成功),
   "consec_empty": int (连续空多少次)}
"""
import json
import os
import time
from datetime import datetime
from pathlib import Path


# v13.88.79 · 静默监控统一接入 · 用户 07-28「所有静默监控都到齐了吗」
def _alert(severity: str, msg: str):
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
        from alert_bus import notify as _n
        _n(_o.path.basename(__file__).replace(".py",""), severity, msg)
    except Exception:
        pass


ROOT = Path(__file__).resolve().parents[2]
V11 = ROOT / "v11"
LOG = V11 / ".source_health.jsonl"
STATE = V11 / ".source_health_state.json"

# 触发反思的阈值
EMPTY_STREAK_TRIGGER = 3  # 连续 3 次空 → 反思
ERROR_STREAK_TRIGGER = 2  # 连续 2 次错 → 反思

# v13.88.9L · 真自愈阈值 (用户: 报「必须自愈」233遍没人自愈·加真动作·别只反思)
# 连续 empty/error 超此阈值 → 临时降级该源 (写.search_disabled_sources)·fanout自动换等价源
# 阈值比反思高·确认是持续性问题才降级 (github临时空≠死站·不轻易禁)
DISABLE_STREAK_TRIGGER = 8
_DISABLED_SOURCES = V11 / ".search_disabled_sources.json"


def _autoheal_disable(source: str, reason: str, streak: int):
    """真自愈: 连续失败超阈值 → 临时降级源 (可恢复·非永久标死)"""
    try:
        d = {}
        if _DISABLED_SOURCES.exists():
            d = json.loads(_DISABLED_SOURCES.read_text(encoding="utf-8"))
        if source not in d:
            d[source] = {"disabled_at": datetime.now().isoformat(),
                         "rate": 0.0,
                         "auto_by": "source_health",
                         "reason": f"连续{streak}次{reason}·临时降级·恢复即解禁"}
            _DISABLED_SOURCES.write_text(
                json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            _alert("warn", f"自愈降级: {source} 连续{streak}次{reason}·"
                           f"临时禁用·fanout换等价源·源恢复后自动解禁")
    except Exception:
        pass


def _autoheal_recover(source: str):
    """源恢复 ok → 从降级名单移除 (loop 动态健康管理·非写死禁用)"""
    try:
        if not _DISABLED_SOURCES.exists():
            return
        d = json.loads(_DISABLED_SOURCES.read_text(encoding="utf-8"))
        # 只解禁 source_health 自己降的·不碰用户/别的组件手动禁的
        if source in d and d[source].get("auto_by") == "source_health":
            del d[source]
            _DISABLED_SOURCES.write_text(
                json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            _alert("info", f"自愈恢复: {source} 已恢复正常·解除降级")
    except Exception:
        pass


def _load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"streaks": {}, "last_reflect_at": {}}


def _save_state(state: dict):
    try:
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _fire_reflection(source: str, reason: str, streak: int):
    """触发 feedback_pattern_reflector + correction_evolver.deduce · 双通道
    v13.72 · 修 · 不再只反射 · 让感官信号真走 deduce 5 问
    """
    import subprocess
    text = f"数据源 {source} 已连续 {streak} 次{reason}·链路缺校验器·必须自愈"
    ctx = f"source_health · source={source} · reason={reason} · streak={streak}"
    try:
        # 通道 1 · pattern_reflector (累计 pattern · 已有)
        subprocess.Popen(
            ["python3", "-c",
             f"import sys; sys.path.insert(0, '{V11 / 'evolution'}'); "
             f"from feedback_pattern_reflector import ingest_meta; "
             f"ingest_meta({json.dumps(text)}, {json.dumps(ctx)})"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass
    try:
        # 通道 2 · correction_evolver.ingest → tick 会走 deduce 5+ 问
        # 这才是"演绎" · 不是反射
        subprocess.Popen(
            ["python3", "-c",
             f"import sys; sys.path.insert(0, '{V11 / 'evolution'}'); "
             f"from correction_evolver import ingest as _ce; "
             f"_ce({json.dumps(text)}, {json.dumps(ctx)}, "
             f"[{json.dumps('source_health_signal')}, {json.dumps(source)}, {json.dumps(reason)}])"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def record_fetch(source: str, url: str = "", count=None, err: str = None,
                 min_expected: int = 1) -> dict:
    """记一次数据抓取 · 返回 streak 状态供上游 decision

    Args:
        source: 源名 (少数派 / Reddit / 知乎 / 小红书 ...)
        url: 抓的 URL
        count: 拿到多少条 · None=异常 · 0=空 · >=1=有
        err: 异常字符串
        min_expected: 最少期望条数 · 少于此判 low
    """
    ts = datetime.now().isoformat(timespec="seconds")
    state = _load_state()

    # 判 status
    if err:
        status = "error"
    elif count is None:
        status = "unknown"
    elif count == 0:
        status = "empty"
    elif count < min_expected:
        status = "low"
    else:
        status = "ok"

    # 更新 streak
    streaks = state.setdefault("streaks", {})
    key = source
    prev = streaks.get(key, {"empty": 0, "error": 0, "ok": 0})
    if status == "empty":
        prev["empty"] += 1
        prev["error"] = 0
        prev["ok"] = 0
    elif status == "error":
        prev["error"] += 1
        prev["empty"] = 0
        prev["ok"] = 0
    elif status == "ok":
        prev["ok"] += 1
        prev["empty"] = 0
        prev["error"] = 0
    else:  # low / unknown 不改 streak
        pass
    streaks[key] = prev

    # 落 log
    rec = {
        "ts": ts,
        "source": source,
        "url": url[:200],
        "count": count,
        "err": (err or "")[:200],
        "status": status,
        "streak": dict(prev),
    }
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass

    # 触发反思 · 但节流 (同一 source 6h 内只 fire 一次 reflection)
    fire = False
    reason = None
    if prev["empty"] >= EMPTY_STREAK_TRIGGER:
        fire = True
        reason = "empty"
    elif prev["error"] >= ERROR_STREAK_TRIGGER:
        fire = True
        reason = "error"

    # v13.88.9L · 真自愈动作 (不只反思): 用独立累计计数判降级·不受下面fire清streak影响
    # (fire在阈值3清streak·若复用streak永远到不了降级阈值8·故用独立cumulative)
    cum = state.setdefault("cumulative_fail", {})
    if status == "ok":
        cum[source] = 0
        _autoheal_recover(source)          # 恢复即解除降级 (动态健康·非写死禁用)
    elif status in ("empty", "error"):
        cum[source] = cum.get(source, 0) + 1
        if cum[source] >= DISABLE_STREAK_TRIGGER:
            _autoheal_disable(source, status, cum[source])  # 持续失败→真降级换源

    if fire:
        last = state.setdefault("last_reflect_at", {})
        last_key = f"{source}_{reason}"
        last_ts = last.get(last_key, 0)
        if time.time() - last_ts > 6 * 3600:
            _fire_reflection(source, reason, prev[reason])
            last[last_key] = time.time()
            # streak 清 0 · 避免连续触发
            prev[reason] = 0

    _save_state(state)
    return rec



def suggest_backoff(source: str) -> int:
    """v13.72 · 给 caller 建议下次调用等多久 (秒)
    根据 source 的 empty/error streak · 指数退避
    caller 使用: time.sleep(suggest_backoff("少数派"))
    """
    state = _load_state()
    streaks = state.get("streaks", {}).get(source, {})
    empty = streaks.get("empty", 0)
    error = streaks.get("error", 0)
    n = max(empty, error)
    if n == 0:
        return 0
    # 指数退避 · 上限 300s
    return min(300, 2 ** min(n, 8))


def status_report() -> dict:
    """给 dashboard / proactive_reporter 用的当前状态"""
    state = _load_state()
    streaks = state.get("streaks", {})
    problem = []
    for src, s in streaks.items():
        if s.get("empty", 0) >= 2 or s.get("error", 0) >= 1:
            problem.append({
                "source": src,
                "empty_streak": s.get("empty", 0),
                "error_streak": s.get("error", 0),
            })
    return {
        "total_sources": len(streaks),
        "problem_sources": problem,
    }


if __name__ == "__main__":
    try:
        import sys
        if len(sys.argv) > 1 and sys.argv[1] == "report":
            print(json.dumps(status_report(), ensure_ascii=False, indent=2))
        else:
            # 演示
            r = record_fetch("test-source", "http://example.com", count=0)
            print("recorded:", r)
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise
