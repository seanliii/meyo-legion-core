#!/usr/bin/env python3
"""
v13.20 · perf_guard · 电脑性能守护
────────────────────────────────────────────────────────────
用户 (2026-07-09 /goal): "你不要搜太快·限制一下·监控我的电脑性能·有时候太卡了"

核心机制:
1. 每次调重活 (CDP/deep_read/skill_executor) 前先 check_load()
   过载就 sleep 冷却 · 直到降到安全阈值
2. 全局并发闸门 · 限制同一时刻只允许 N 个 web-access CDP 会话
3. 日志记录 · 每次熔断/冷却都落 what_i_did · 用户看得到

阈值 (macOS 8 核参考):
- CPU idle < 20%     → 冷却 30s (系统冒烟)
- Load 1min > 12     → 冷却 20s (过载)
- Mem unused < 500M  → 冷却 20s (换页)
- 上面 3 项任一超阈值就冷却
"""
import os, sys, json, time, subprocess, threading
from datetime import datetime


# v13.88.79 · 静默监控统一接入 · 用户 07-28「所有静默监控都到齐了吗」
def _alert(severity: str, msg: str):
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
        from alert_bus import notify as _n
        _n(_o.path.basename(__file__).replace(".py",""), severity, msg)
    except Exception:
        pass


BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── 阈值 (macOS · v13.21 调整) ──
# 用户 mac 常态 mem_unused < 400MB · 老 400 阈值太苛刻永远过载
# 改看 swap 压力: 如果 unused < 50MB 且 load > 8 = 真过载
# v13.88.9L · 阈值支持 env 覆盖 (默认值不变·保护机制在)
#   用户跑 bench 时本机 load 基线就高(IDE+VM 20-40)·会把正常搜索全冷却掉
#   设 MEYO_PERF_RELAX=1 一键放宽 (load 15→40, idle 15→3)·或各项单独覆盖
_RELAX = os.environ.get("MEYO_PERF_RELAX") == "1"
CPU_IDLE_MIN = float(os.environ.get("MEYO_PERF_CPU_IDLE_MIN", 3 if _RELAX else 15))
LOAD_1MIN_MAX = float(os.environ.get("MEYO_PERF_LOAD_MAX", 40 if _RELAX else 15))
MEM_UNUSED_MIN_MB = float(os.environ.get("MEYO_PERF_MEM_MIN_MB", 50 if _RELAX else 100))
COOL_DOWN_SECONDS = float(os.environ.get("MEYO_PERF_COOL_SEC", 10 if _RELAX else 20))
MAX_WAIT_LOOPS = int(os.environ.get("MEYO_PERF_MAX_LOOPS", 2 if _RELAX else 4))

# ── 全局并发闸门 (进程内) ──
# 用户 goal · 限制 CDP 并发 · 之前 max_workers=6 电脑就要爆
# v13.88 · 分级闸门: 本地 CDP (Playwright/Browser Use) 限制 2
#           云端 API (browseract/Firecrawl) 限制 3 (不走本地 Chrome)
_CDP_SEMAPHORE = threading.Semaphore(2)         # 本地 CDP: Playwright, Browser Use
_CLOUD_SEMAPHORE = threading.Semaphore(3)       # 云端 API: browseract, Firecrawl (不挤本地)

# 不需要冷却的组件 (非本地资源 · 不挤 CPU/Mem)
_SKIP_COOL_COMPONENTS = {"friday", "curl", "firecrawl", "browseract",
                         "webclaw", "stealth_scraper", "jina", "bsk", "api"}

# v40 · 分级闸门 · 云 API 不再被本地 CDP 卡死 (用户 2026-07-24 P2)
# 本地 CDP (Playwright/browser_use/bsk) 共享 CDP_SEM · 云 API 各自独立
_PLAYWRIGHT_SEMAPHORE = threading.Semaphore(2)     # 本地 Chrome 9224
_BROWSERACT_SEMAPHORE = threading.Semaphore(3)     # 云 API · 不占本地
_FIRECRAWL_SEMAPHORE  = threading.Semaphore(3)     # 云 API · 不占本地
_BROWSER_USE_SEMAPHORE = threading.Semaphore(1)    # 本地独立 Chrome
_STEALTH_SEMAPHORE = threading.Semaphore(4)        # HTTP · 4 并发


def try_acquire_local_cdp(timeout: float = 0.0) -> bool:
    """v40 · 非阻塞尝试拿 CDP 本地闸门 (P1)
    timeout=0 → 立即返·拿不到就 False
    调用方: 拿不到就 skip 到下一个降级方法·不排队等
    """
    return _CDP_SEMAPHORE.acquire(blocking=(timeout > 0), timeout=timeout if timeout > 0 else None)


def release_local_cdp():
    try:
        _CDP_SEMAPHORE.release()
    except ValueError:
        pass


def sample_perf() -> dict:
    """采一次性能样本 · 返回 {cpu_idle, load_1min, load_5min, mem_unused_mb}
    macOS: 用 top / vm_stat"""
    out = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "cpu_idle": None,
        "load_1min": None,
        "load_5min": None,
        "mem_unused_mb": None,
    }
    try:
        # top -l 1 · CPU 和 Load
        r = subprocess.run(["top", "-l", "1", "-n", "0", "-s", "0"],
                          capture_output=True, text=True, timeout=5)
        text = r.stdout
        # Load Avg: 25.40, 40.06, 25.74
        import re
        m = re.search(r'Load Avg:\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)', text)
        if m:
            out["load_1min"] = float(m.group(1))
            out["load_5min"] = float(m.group(2))
        # CPU usage: 36.23% user, 17.17% sys, 46.59% idle
        m = re.search(r'CPU usage:.*?([\d.]+)%\s*idle', text)
        if m:
            out["cpu_idle"] = float(m.group(1))
        # PhysMem: 15G used (…, …), 286M unused
        m = re.search(r'PhysMem:.*?([\d]+)M\s*unused', text)
        if m:
            out["mem_unused_mb"] = int(m.group(1))
        else:
            # 有时候是 "1G unused"
            m = re.search(r'PhysMem:.*?([\d.]+)G\s*unused', text)
            if m:
                out["mem_unused_mb"] = int(float(m.group(1)) * 1024)
    except Exception as e:
        out["_err"] = str(e)[:80]
    return out


def is_overloaded(sample: dict = None) -> tuple:
    """判断是否过载 · 返回 (bool, reason:str)
    v13.24 · 用户诚实反馈: Mac 常态 mem_unused < 100MB · 我永远被卡
    改用**双条件**触发:CPU 冒烟 + Load 超限 才算过载 · 单纯 mem 低不算
    (因为 macOS 压缩内存管理·mem_unused 低不等于系统卡)"""
    if sample is None:
        sample = sample_perf()
    reasons = []
    ci = sample.get("cpu_idle")
    lm = sample.get("load_1min")
    mu = sample.get("mem_unused_mb")

    cpu_hot = ci is not None and ci < CPU_IDLE_MIN
    load_hot = lm is not None and lm > LOAD_1MIN_MAX
    mem_critical = mu is not None and mu < 30  # v13.24 · 30MB 才是真崩溃

    if cpu_hot:
        reasons.append(f"cpu_idle={ci}%<{CPU_IDLE_MIN}%")
    if load_hot:
        reasons.append(f"load_1min={lm}>{LOAD_1MIN_MAX}")
    if mem_critical:
        reasons.append(f"mem_critical={mu}MB<30MB")

    # v13.24 · 只有 CPU 或 Load 真烧 · 或 mem 真崩才算过载
    # mem 100MB 以下但 CPU idle >20% · 不算过载 (macOS 压缩内存)
    is_over = cpu_hot or load_hot or mem_critical
    return (is_over, " · ".join(reasons) if reasons else "ok")


def wait_until_cool(component: str = "unknown", max_loops: int = MAX_WAIT_LOOPS) -> dict:
    """如果过载就 sleep 冷却 · 直到降下来 (或超时放行)
    返回诊断: {waited_s, loops, final_sample, gave_up}

    v13.88 · component 分级: 非本地资源组件 (friday/curl/firecrawl/browseract 等)
    直接跳过冷却·不阻塞·因为它们不挤本地 CPU/CDP。
    只有本地 CDP 组件 (playwright/browser_use/cdp) 才真等冷却。
    """
    # v13.88 · 非本地资源组件不冷却 · 直接放行
    if component.lower() in _SKIP_COOL_COMPONENTS:
        s = sample_perf()
        return {"waited_s": 0, "loops": 0, "final_sample": s,
                "gave_up": False, "component": component, "skipped": True}

    t0 = time.time()
    loops = 0
    last = None
    gave_up = False
    while loops < max_loops:
        s = sample_perf()
        overloaded, reason = is_overloaded(s)
        last = s
        if not overloaded:
            break
        loops += 1
        # 打印到 stderr · cron 日志能看到
        print(f"[perf_guard] {component} 冷却 {COOL_DOWN_SECONDS}s "
              f"({reason}) · loop {loops}/{max_loops}", file=sys.stderr, flush=True)
        time.sleep(COOL_DOWN_SECONDS)
    else:
        gave_up = True
    waited_s = time.time() - t0
    result = {
        "waited_s": round(waited_s, 1),
        "loops": loops,
        "final_sample": last,
        "gave_up": gave_up,
        "component": component,
    }
    if waited_s > 5:
        # 显著冷却过·落 what_i_did
        try:
            sys.path.insert(0, os.path.join(BASE, "v11", "evolution"))
            from what_i_did import record
            record("perf_cool_down", result, ok=not gave_up)
        except Exception:
            pass
    return result


def with_cdp_slot(fn, component: str = "cdp", cloud: bool = False):
    """全局 CDP 并发闸门 + 冷却
    Usage:
        result = with_cdp_slot(lambda: wa.cdp_navigate_and_read(url))
        result = with_cdp_slot(lambda: ba.read(url), cloud=True)  # 云端 API

    v13.88 · cloud=True 走 _CLOUD_SEMAPHORE (不挤本地 CDP)
    """
    wait_until_cool(component)
    sem = _CLOUD_SEMAPHORE if cloud else _CDP_SEMAPHORE
    with sem:
        return fn()


# ── 用户能查的 CLI ──
if __name__ == "__main__":
    try:
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("--sample", action="store_true", help="采一次样本")
        ap.add_argument("--wait", action="store_true", help="必要时冷却 · 冷却后打印")
        args = ap.parse_args()
        if args.wait:
            r = wait_until_cool("cli_test")
            print(json.dumps(r, ensure_ascii=False, indent=2))
        else:
            s = sample_perf()
            overloaded, reason = is_overloaded(s)
            print(f"性能样本: {json.dumps(s, ensure_ascii=False, indent=2)}")
            print(f"过载判定: {overloaded} · {reason}")
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise
