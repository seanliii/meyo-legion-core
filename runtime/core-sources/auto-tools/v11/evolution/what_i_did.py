#!/usr/bin/env python3
"""
v13.15 · what_i_did · 真跑透明化日志
────────────────────────────────────────────────────────────
用户诉求 (2026-07-09 /goal): "你要告诉我你真干了啥"
                              "自我检测环节咋又没了?"
                              "每次执行时候都回看是否符合预期"

核心机制:
1. 每次执行动作 (search/deep_read/write_post/publish/evolution) 落一行 jsonl
2. 一份"人看的"每日汇总 (what_i_did_YYYY-MM-DD.md) · 用户随时看
3. 每次触发前跑 alignment_check 对照 CLAUDE.md 核心能力·打印哪些"符合预期"哪些"跑偏"

用法:
    from what_i_did import record, print_daily, alignment_check

    record("deep_read", {"url": "...", "via": "cdp", "chars": 8000})  # 每步落盘
    print_daily()   # 一屏看今天真干了啥
    alignment_check()  # 对照 CLAUDE.md 核心能力做健康检查
"""
import os, sys, json, re
from datetime import datetime, timedelta
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


BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOG_DIR = os.path.join(BASE, "v11", "what_i_did_logs")
os.makedirs(LOG_DIR, exist_ok=True)


def today_log_path() -> str:
    return os.path.join(LOG_DIR, f"{datetime.now().strftime('%Y-%m-%d')}.jsonl")


def record(action: str, details: dict = None, ok: bool = True):
    """落一条'真干了啥'
    action: 语义化动作名 ('search', 'deep_read', 'write_post', 'publish',
                          'jargon_reject', 'evolution_action', ...)
    details: 具体做了什么·如 {'url': ..., 'via': 'cdp', 'chars': 8000}
    ok: 成功还是失败·失败也要落·让用户看到"我尝试过什么"
    """
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "action": action,
        "ok": ok,
        "details": details or {},
    }
    try:
        with open(today_log_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def load_today() -> list:
    p = today_log_path()
    if not os.path.exists(p):
        return []
    rows = []
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return rows


def summarize_today() -> dict:
    """给用户一个'今天我真干了啥'的一屏摘要
    v13.88.68 · 用户 07-28 · opus 挖:『只统计触发次数·不看真活多久·假绿』
    治: 加 aliveness (last_ts 距今·process 真活)·crash 检测
    """
    rows = load_today()
    from collections import Counter
    by_action = Counter(r["action"] for r in rows)
    n_ok = sum(1 for r in rows if r.get("ok"))
    n_fail = sum(1 for r in rows if not r.get("ok"))
    first_ts = rows[0]["ts"] if rows else None
    last_ts = rows[-1]["ts"] if rows else None
    # 抓关键动作的细节样本
    samples = {}
    for a in ["search", "deep_read", "write_post", "publish", "jargon_reject",
              "evolution_action", "cdp_block", "sanity_check"]:
        matching = [r for r in rows if r["action"] == a]
        if matching:
            samples[a] = {
                "n": len(matching),
                "last": matching[-1]["details"],
            }

    # v13.88.68 · aliveness · 治「crash 后 cron 照跑没人发现」
    aliveness = {"stale_min": None, "warn": None}
    if last_ts:
        try:
            _last_dt = datetime.fromisoformat(last_ts)
            _stale_min = int((datetime.now() - _last_dt).total_seconds() / 60)
            aliveness["stale_min"] = _stale_min
            # 30 min 无新 action = 组件挂了 (cron 每 10 min 应有触发)
            if _stale_min > 30:
                aliveness["warn"] = f"最近 {_stale_min}min 无新动作·cron 或 v11_race 可能 crash"
        except Exception:
            pass

    # v13.88.68 · crash 抓 · fail 率 > 30% 报警
    fail_rate = n_fail / max(n_total := len(rows), 1)
    if fail_rate > 0.3:
        aliveness["fail_rate_warn"] = f"fail 率 {fail_rate*100:.0f}% · 有系统性问题"

    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "n_total": len(rows),
        "n_ok": n_ok,
        "n_fail": n_fail,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "aliveness": aliveness,  # v13.88.68
        "by_action": dict(by_action),
        "samples": samples,
    }


def print_daily():
    """一屏打印 · 用户直接看"""
    s = summarize_today()
    print("═" * 64)
    print(f"v13.15 · 今日真跑透明化 · {s['date']}")
    print("═" * 64)
    if s["n_total"] == 0:
        print("  今天还没落盘任何动作 · 是否 cron/watchdog 没运行?")
        return
    print(f"  总动作: {s['n_total']} · 成功 {s['n_ok']} · 失败 {s['n_fail']}")
    print(f"  时段: {s['first_ts']} → {s['last_ts']}")
    print()
    print("  按动作类型:")
    for a, n in sorted(s["by_action"].items(), key=lambda x: -x[1]):
        print(f"    · {a:20s} · {n:4d} 次")
    print()
    if s["samples"]:
        print("  最近样本 (每类最新一条):")
        for a, info in s["samples"].items():
            d = info["last"]
            preview = json.dumps(d, ensure_ascii=False)[:120]
            print(f"    · {a}: {preview}")


# ═══════════════════════════════════════════════════════════════
#  Alignment Check · 对照 CLAUDE.md 核心能力·检查是否符合预期
# ═══════════════════════════════════════════════════════════════

# 核心能力清单 (跟 CLAUDE.md 里"核心能力"一节同步)
# 每条能力有 assert 函数 · 返回 (ok: bool, detail: str)
CORE_CAPABILITIES = []


def _cap(name: str):
    """装饰器·注册核心能力"""
    def deco(fn):
        CORE_CAPABILITIES.append((name, fn))
        return fn
    return deco


@_cap("C1 · v13 唯一产线 (v11 race)")
# v13.9-DEAD · 未被调用 · 保留待清理
def _c1():
    import subprocess
    r = subprocess.run(["pgrep", "-f", "v11_race.py"], capture_output=True, text=True, timeout=3)
    if r.stdout.strip():
        return True, f"v11_race 在跑 (PID {r.stdout.strip()[:20]})"
    # 或者最近 1h 有 race 完成日志
    log = os.path.join(BASE, "hourly_evolve.log")
    if os.path.exists(log):
        try:
            recent = "\n".join(open(log, errors="ignore").readlines()[-500:])
            if "[STEP-2 ✅] v11 race" in recent or "STEP-2 fork" in recent:
                return True, "最近有 v11 race 触发记录"
        except Exception:
            pass
    return False, "v11 race 既没在跑·近 500 行日志也没记录·产线卡住?"


# v13.9-DEAD · 未被调用 · 保留待清理
@_cap("C2 · 反爬升级 (web-access CDP 可用)")
def _c2():
    import subprocess
    r = subprocess.run(["curl", "-sS", "--max-time", "2",
                        "http://localhost:3457/health"],
                       capture_output=True, text=True, timeout=3)
    if r.returncode == 0 and '"status":"ok"' in (r.stdout or ""):
        return True, "cdp-proxy :3457 alive"
    # 检查 Chrome 9222
    r2 = subprocess.run(["curl", "-sS", "--max-time", "2",
                         "http://127.0.0.1:9222/json/version"],
                        capture_output=True, text=True, timeout=3)
    if r2.returncode == 0:
        return False, "Chrome 9222 开着但 cdp-proxy 死了·keepalive 应该拉起 (下次 cron)"
    return False, "Chrome 9222 未开·提醒用户勾 chrome://inspect/#remote-debugging"

# v13.9-DEAD · 未被调用 · 保留待清理

@_cap("C3 · T-9 黑话闸门 (writer 拦 handoff/pipeline 等 33 词)")
def _c3():
    p = os.path.join(BASE, "v11", "experience_writer.py")
    if not os.path.exists(p):
        return False, "writer 缺失"
    try:
        content = open(p).read()
        need = ["T-9", "handoff", "pipeline", "few-shot", "jargon_patterns"]
        missing = [n for n in need if n not in content]
        if missing:
            return False, f"T-9 表缺: {missing}"
        return True, "T-9 33 词全在"
    except Exception as e:
        return False, str(e)[:80]
# v13.9-DEAD · 未被调用 · 保留待清理


@_cap("C4 · 身份人设锁死 (T-13)")
def _c4():
    p = os.path.join(BASE, "v11", "experience_writer.py")
    if not os.path.exists(p):
        return False, "writer 缺失"
    content = open(p).read()
    if "T-13" in content and "身份人设锁死" in content:
        return True, "T-13 已激活"
# v13.9-DEAD · 未被调用 · 保留待清理
    return False, "T-13 未启用"


@_cap("C5 · 开头钩子多样性 (T-11 · 5 种钩子模板)")
def _c5():
    p = os.path.join(BASE, "v11", "experience_writer.py")
    content = open(p).read() if os.path.exists(p) else ""
    if "T-11" in content and "先甩一段" in content:  # T-11 里的禁用示例
# v13.9-DEAD · 未被调用 · 保留待清理
        return True, "T-11 开头钩子闸门在"
    return False, "T-11 未启用"


@_cap("C6 · 反馈基因加载 (evolution 读 feedback-*.md)")
def _c6():
    p = os.path.join(BASE, "v11", "evolution", "feedback_ingester.py")
    if not os.path.exists(p):
        return False, "feedback_ingester 缺失"
    # 跑一下·看能加载多少条
    import subprocess
    try:
        r = subprocess.run(["python3", "-c",
                           "import sys; sys.path.insert(0, '{}');"
                           "from feedback_ingester import load_all_feedback;"
                           "print(len(load_all_feedback()))".format(os.path.dirname(p))],
                          capture_output=True, text=True, timeout=10)
        n = int((r.stdout or "0").strip())
        if n >= 8:
            return True, f"加载 {n} 条反馈基因"
# v13.9-DEAD · 未被调用 · 保留待清理
        return False, f"只加载 {n} 条·应 ≥8"
    except Exception as e:
        return False, str(e)[:80]


@_cap("C7 · 真上网调研 (agent_search + deep_read)")
def _c7():
    p = os.path.join(BASE, "v11", "agent_search.py")
    content = open(p).read() if os.path.exists(p) else ""
    if "def deep_read" in content and "web_access_bridge" in content:
        return True, "deep_read 已挂"
    return False, "deep_read 未接入"


@_cap("C8 · 限流自学习 (blocked_domains)")
def _c8():
    p = os.path.join(BASE, "v11", "evolution", "web_access_bridge.py")
    if not os.path.exists(p):
        return False, "bridge 缺失"
    content = open(p).read()
    if "get_blocked_domains" in content and "search_incidents" in content:
        return True, "限流自学习组件在"
    return False, "限流自学习缺失"


@_cap("C9 · sanity_check 每次触发都跑")
def _c9():
    p = os.path.join(BASE, "cron_loop.sh")
    if not os.path.exists(p):
        return False, "cron_loop.sh 缺失"
    content = open(p).read()
    if "sanity_check.py" in content:
        return True, "cron_loop 有 sanity_check 触发点"
    return False, "cron_loop 未挂 sanity_check"


@_cap("C10 · 真跑透明化 (今日 what_i_did 有记录)")
def _c10():
    rows = load_today()
    if rows:
        return True, f"今日 {len(rows)} 条动作已落盘"
    return False, "今日 what_i_did 空 · 组件可能没接入调用点"


@_cap("C12 · 全网真源接入 (12 源·+v13.85 sogou 微信全文)")
def _c12():
    p = os.path.join(BASE, "v11", "global_sources.py")
    if not os.path.exists(p):
        return False, "global_sources.py 缺失"
    content = open(p).read()
    need = ["search_google", "search_twitter", "search_hackernews",
            "search_reddit", "search_github_issues",
            "search_v2ex_hot", "search_zhihu", "search_xiaohongshu",
            "search_sspai",
            # v13.85 · 搜狗微信搜索 + tab 内 click 拿全文
            "search_sogou_weixin", "_sogou_fetch_details_via_click",
            # v13.86 · xhs API (美团 universal-search) · dispatcher + legacy
            "search_xiaohongshu_api", "_search_xiaohongshu_cdp_legacy",
            "_xhs_api_health_log", "_XHS_API_URL",
            "SOURCE_CATALOG"]
    missing = [n for n in need if n not in content]
    if missing:
        return False, f"缺: {missing}"
    ap = os.path.join(BASE, "v11", "agent_search.py")
    ac = open(ap).read() if os.path.exists(ap) else ""
    if "gs.search_hackernews" not in ac:
        return False, "全网源未接入 agent_search fanout"
    # v13.85 · 也要 assert sogou_weixin 进了 fanout src_map
    if "sogou_weixin" not in ac:
        return False, "sogou_weixin 未接入 agent_search fanout src_map"
    return True, "12 源+主流水线接入 · +v13.85 sogou 微信全文 (CDP tab click)"


@_cap("C14 · 性能守护 (perf_guard 冷却+并发限流)")
def _c14():
    p = os.path.join(BASE, "v11", "evolution", "perf_guard.py")
    if not os.path.exists(p):
        return False, "perf_guard.py 缺失"
    content = open(p).read()
    need = ["wait_until_cool", "sample_perf", "is_overloaded", "_CDP_SEMAPHORE"]
    missing = [n for n in need if n not in content]
    if missing:
        return False, f"缺: {missing}"
    # 接入检查
    for hook in [os.path.join(BASE, "cron_loop.sh"),
                 os.path.join(BASE, "v11", "evolution", "web_access_bridge.py"),
                 os.path.join(BASE, "v11", "skill_executor.py")]:
        if os.path.exists(hook):
            c = open(hook).read()
            if "perf_guard" not in c and "wait_until_cool" not in c:
                return False, f"未接入: {os.path.basename(hook)}"
    return True, "perf_guard 挂 cron_loop/web_access/skill_executor · CDP 并发闸门 2"


@_cap("C18 · 灵魂常驻工程 (画像扩+空闲琢磨+12 维深抽+饥饿调度)")
def _c18():
    p = os.path.join(BASE, "v11", "evolution", "persistent_soul.py")
    if not os.path.exists(p):
        return False, "persistent_soul.py 缺失"
    content = open(p).read()
    need = ["grow_atlas", "daily_persona_ponder", "deep_extract_from_url",
            "pick_hungry_personas", "CURIOSITY_DIMENSIONS", "mark_persona_fed"]
    missing = [n for n in need if n not in content]
    if missing:
        return False, f"缺: {missing}"
    # cron_loop 挂点
    cron = open(os.path.join(BASE, "cron_loop.sh")).read()
    if "persistent_soul.py" not in cron:
        return False, "cron_loop 未挂 persistent_soul"
    return True, "4 层常驻(grow/ponder/deep-12d/hungry) · cron 每 10min 累加"


@_cap("C17 · 灵魂研究 (3 轮 loop + 站内画像驱动)")
def _c17():
    p = os.path.join(BASE, "v11", "evolution", "persona_driven_research.py")
    if not os.path.exists(p):
        return False, "persona_driven_research.py 缺失"
    content = open(p).read()
    need = ["load_personas", "pick_personas_for_topic", "craft_queries_from_persona",
            "judge_results", "deep_read_and_extract", "run_persona_loop"]
    missing = [n for n in need if n not in content]
    if missing:
        return False, f"缺: {missing}"
    # persona_atlas.json 存在
    atlas = os.path.join(BASE, "persona_atlas.json")
    if not os.path.exists(atlas):
        return False, "persona_atlas.json 缺失 · 无站内画像"
    # orchestrator 接入
    orch = open(os.path.join(BASE, "v11", "evolution", "orchestrator.py")).read()
    if "run_persona_loop" not in orch:
        return False, "orchestrator 未接入 run_persona_loop"
    return True, "灵魂研究 6 层组件 · 25 画像库 · orchestrator 阶段 3.5 挂接"


@_cap("C16 · 真人搜索模仿 (plan_queries 结构化+平台派发)")
def _c16():
    p = os.path.join(BASE, "v11", "agent_search.py")
    if not os.path.exists(p):
        return False, "agent_search.py 缺失"
    content = open(p).read()
    need = ["_fanout_by_target", "target_source", "plan_queries_flat",
            "cdp_sources", "human_search_patterns"]
    missing = []
    # human_search_patterns 是 .md 存在与否
    hsp = os.path.join(BASE, "v11", "evolution", "human_search_patterns.md")
    if not os.path.exists(hsp):
        return False, "human_search_patterns.md 缺失"
    for n in need[:-1]:
        if n not in content:
            missing.append(n)
    if missing:
        return False, f"缺: {missing}"
    return True, "plan_queries dict 输出+target_source 派发+CDP 串行+人类搜索模式库"


@_cap("C15 · 自造能力 (evolution 可创建 tool/skill/plugin)")
def _c15():
    p = os.path.join(BASE, "v11", "evolution", "code_evolver.py")
    if not os.path.exists(p):
        return False, "code_evolver.py 缺失"
    content = open(p).read()
    if "action_create_new_component" not in content:
        return False, "create_new_component handler 未加"
    if '"create_new_component"' not in content:
        return False, "handler 未注册到 ACTION_HANDLERS"
    return True, "python_tool/claude_skill/meyo_skill 3 类可自造 · wire_into 可挂钩子"


@_cap("C13 · 搜索技巧自我进化 (search_meta_learning)")
def _c13():
    """meta-learning 组件: DNA log + LLM 反思 + 技巧库落盘"""
    p = os.path.join(BASE, "v11", "evolution", "search_meta_learning.py")
    if not os.path.exists(p):
        return False, "search_meta_learning.py 缺失"
    content = open(p).read()
    need = ["extract_dna", "log_query_effect", "analyze_patterns",
            "reflect_with_llm", "get_techniques_for_planner"]
    missing = [n for n in need if n not in content]
    if missing:
        return False, f"缺函数: {missing}"
    # DNA log 存在吗
    dna_log = os.path.join(BASE, "v11", ".query_dna_log.jsonl")
    if not os.path.exists(dna_log):
        return False, "DNA log 未生成 · 跑一次真研究后触发"
    n_dna = sum(1 for _ in open(dna_log))
    # 技巧库存在吗
    tech = os.path.join(BASE, "v11", ".search_techniques_learned.md")
    tech_ok = os.path.exists(tech) and os.path.getsize(tech) > 500
    if not tech_ok:
        return False, f"技巧库还没建 (DNA {n_dna} 条 · 等 evolution 反思)"
    return True, f"DNA log {n_dna} 条 · 技巧库 {os.path.getsize(tech)}B"


@_cap("C11 · 每轮日志反思 (tick_reflect 每 10min)")
def _c11():
    """看今日 tick_reflect 是否真在跑 · 至少 3 次 (30min 内)"""
    p = os.path.join(BASE, "v11", "evolution", "tick_reflect.py")
    if not os.path.exists(p):
        return False, "tick_reflect.py 缺失"
    # 看今天日志目录
    log_dir = os.path.join(BASE, "v11", "evolution", "logs",
                          datetime.now().strftime("%Y-%m-%d"), "tick_reflections")
    if not os.path.exists(log_dir):
        return False, "tick_reflections 目录不存在 · cron 还没触发过"
    n = len([f for f in os.listdir(log_dir) if f.endswith(".json")])
    # cron_loop 里也要挂点
    cron_content = ""
    cron_path = os.path.join(BASE, "cron_loop.sh")
    if os.path.exists(cron_path):
        cron_content = open(cron_path).read()
    hooked = "tick_reflect.py" in cron_content
    if not hooked:
        return False, f"tick_reflect 未挂进 cron_loop"
    if n == 0:
        return False, f"tick_reflect 挂了但今天还没跑过 · 等下轮 cron"
    return True, f"今日已反思 {n} 次 · cron 挂点在"


def alignment_check(verbose: bool = True) -> dict:
    """对照 CLAUDE.md 核心能力做健康检查
    每条能力返回 (ok, detail)·收集不通过项供用户查"""
    passed = []
    failed = []
    for name, fn in CORE_CAPABILITIES:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"[异常] {type(e).__name__}: {e}"
        if ok:
            passed.append((name, detail))
        else:
            failed.append((name, detail))

    if verbose:
        print("═" * 64)
        print(f"v13.15 · alignment_check · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("═" * 64)
        for name, detail in passed:
            print(f"  ✅ {name} · {detail}")
        for name, detail in failed:
            print(f"  ❌ {name} · {detail}")
        print()
        total = len(passed) + len(failed)
        print(f"═══ {len(passed)}/{total} 核心能力符合预期 ═══")
        if failed:
            print("⚠️  有能力偏离预期·请查上方 ❌ 项")
        else:
            print("✅ 全部符合 CLAUDE.md 定义的核心能力")

    return {
        "ts": datetime.now().isoformat(),
        "total": len(CORE_CAPABILITIES),
        "passed": len(passed),
        "failed": len(failed),
        "passed_list": [n for n, _ in passed],
        "failed_list": [{"name": n, "why": d} for n, d in failed],
    }


if __name__ == "__main__":
    try:
        import argparse
        ap = argparse.ArgumentParser(description="v13.15 · 真跑透明化 + 对齐检查")
        ap.add_argument("--daily", action="store_true", help="打印今日真跑摘要")
        ap.add_argument("--align", action="store_true", help="跑 alignment_check")
        ap.add_argument("--both", action="store_true", help="align + daily")
        ap.add_argument("--record-test", action="store_true", help="落一条测试记录")
        args = ap.parse_args()
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise

    if args.record_test:
        record("test", {"note": "手动测试"}, ok=True)
        print("✅ 测试记录已落盘")
    elif args.both or (not args.daily and not args.align):
        result = alignment_check(verbose=True)
        print()
        print_daily()
    elif args.daily:
        print_daily()
    elif args.align:
        alignment_check(verbose=True)
