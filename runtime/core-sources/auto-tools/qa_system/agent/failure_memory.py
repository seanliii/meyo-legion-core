# -*- coding: utf-8 -*-
"""失败模式记忆 · ERL(Episodic Replay Library)启发式条目 (2026-08-18 落地
《情报侦查思维引擎设计.md》"失败模式记忆"章节)。

病根(实测 longcat 死题等): agent 在同义改写死循环里能反复栽 20+ 步——
每道题都"现场重新发明轮子"，从不把"这条路走不通+为什么+该换哪条"沉淀成
跨题复用的记忆。反思脑只在【本题内】给建议，题目结束教训就丢了。

机制(与 strategy_library 互补·职责分明):
  strategy_library  = 【成功策略】沉淀("这样做成功了")
  failure_memory    = 【失败模式】沉淀("这个信号出现=此路不通·换X")·
                      更强的先验:卡点信号出现时【提前】注入·不等反思脑
三个入口:
  detect_failure_patterns(tool_log)  → 从工具流水识别失败模式签名(纯规则·零LLM)
  record_failure_pattern(...)        → 沉淀/去重累加一条 ERL 启发式条目
  match_failure_memory(patterns)     → 命中历史条目(记 hits·下次注入 prompt)
  format_memory_hint(patterns)       → 渲染成注入 prompt 的文字
存储: ~/.meyo/failure_memory.jsonl (append·有界 200 条·fail-safe 全静默)
"""
import json
import os
import re
import time

_PATH = os.path.expanduser("~/.meyo/failure_memory.jsonl")
_MAX_ENTRIES = 200

# ── 失败模式签名定义(检测规则 + 默认教训模板) ──────────────────────────────
# lesson 默认值只是"第一版口径"——反思脑产出的 lesson(更针对本题)会覆盖记录。
_PATTERNS = {
    "synonym_rewrite_loop": {
        "desc": "同义改写死循环: 连续多次搜索只换关键词表述·0 条新证据",
        "lesson": ("换关键词没用·目标内容不在通用搜索引擎覆盖面里。立即切工具: "
                   "历史下线网页/小众博客→query_web_archive(存档快照); "
                   "人物/化学/医学等结构化事实→query_structured_db; "
                   "都不适用→extract_cards 靠已有证据+通识诚实收口·别再重试"),
    },
    "dead_domain_reram": {
        "desc": "死域连撞: 死域熔断拦截后仍反复往同一域名撞",
        "lesson": ("该域名本环境已熔断·继续撞(含curl/存档前缀)纯烧预算。"
                   "按熔断消息里的替代清单换独立域名池源"
                   "(web.archive.org 死→archive_today·反之亦然)·"
                   "或 run_parallel_search 搜镜像/转述/二手源"),
    },
    "login_wall_hammer": {
        "desc": "登录墙/付费墙/验证码硬磕: 反复访问需要登录的源",
        "lesson": ("登录墙/验证码硬磕无解·不归你管。换路: 搜转载/镜像/维基引用/"
                   "新闻报道的二手转述·或 query_web_archive 查公开时期的存档快照"),
    },
    "tool_fail_streak": {
        "desc": "单工具连败: 同一工具连续失败 3+ 次",
        "lesson": ("同工具连败=工具/输入有问题·不是运气。write_capability 造替代·"
                   "search_github 换现成轮子·或彻底换工具类别"),
    },
}

_SEARCH_TOOLS = ("run_parallel_search", "web_search", "search_web")
_LOGIN_SIGS = ("登录", "验证码", "captcha", "paywall", "付费墙", "sign in",
               "log in required", "login required", "403")
_FAIL_SIGS = ("失败", "异常", "error", "超时", "timeout", "fail")


def detect_failure_patterns(tool_log, window=30):
    """从 tool_log( [{tool,summary,...}] )识别失败模式。纯规则零LLM·fail-safe []。

    返回 [{pattern, detail}]·按检出置信度排序(检测到才算·宁缺勿滥)。
    """
    try:
        entries = [e for e in (tool_log or []) if isinstance(e, dict)][-window:]
        out = []
        # ① 同义改写死循环: 搜索类工具在【搜索子序列】里尾部连续>=3次都 0 新证据。
        # 注意不能只看“尾部连续段”——真实场景搜索和 fetch 失败/门控拦截是交错的
        # (longcat 实迹: search→fetch被拦→search→fetch被拦…), 按连续段判会漏检。
        # 只看搜索子序列: 最后 3+ 次搜索全是“新增 0 条”= 只换措辞没换信息源。
        search_seq = [e for e in entries if str(e.get("tool") or "") in _SEARCH_TOOLS]
        zero_tail = 0
        for e in reversed(search_seq):
            if re.search(r"新增\s*0\s*条", str(e.get("summary") or "")):
                zero_tail += 1
            else:
                break
        if zero_tail >= 3:
            out.append({"pattern": "synonym_rewrite_loop",
                        "detail": f"最后{zero_tail}次搜索全部0新证据(只换措辞没换信息源)"})
        # ② 死域连撞: 近窗>=2条摘要含死域熔断标记
        dead_n = sum(1 for e in entries if "死域熔断" in str(e.get("summary") or ""))
        if dead_n >= 2:
            out.append({"pattern": "dead_domain_reram",
                        "detail": f"近{len(entries)}步死域熔断拦截{dead_n}次"})
        # ③ 登录墙硬磕: 近窗>=2条摘要含登录/验证码/付费墙信号
        login_n = sum(1 for e in entries
                      if any(s in str(e.get("summary") or "").lower() for s in _LOGIN_SIGS))
        if login_n >= 2:
            out.append({"pattern": "login_wall_hammer",
                        "detail": f"近{len(entries)}步登录墙/验证码信号{login_n}次"})
        # ④ 单工具连败: 同一工具尾部连续>=3条失败摘要
        streak_tool, streak_n = None, 0
        for e in reversed(entries):
            sm = str(e.get("summary") or "").lower()
            if any(s in sm for s in _FAIL_SIGS):
                streak_tool, streak_n = e.get("tool"), streak_n + 1
            else:
                break
        if streak_tool and streak_n >= 3:
            out.append({"pattern": "tool_fail_streak",
                        "detail": f"工具'{streak_tool}'尾部连败{streak_n}次"})
        return out
    except Exception:
        return []


def _load():
    try:
        if not os.path.exists(_PATH):
            return []
        with open(_PATH, encoding="utf-8") as f:
            rows = [json.loads(ln) for ln in f
                    if ln.strip() and ln.lstrip().startswith("{")]
        return rows[-_MAX_ENTRIES:]
    except Exception:
        return []


def _save(rows):
    try:
        os.makedirs(os.path.dirname(_PATH), exist_ok=True)
        with open(_PATH, "w", encoding="utf-8") as f:
            for r in rows[-_MAX_ENTRIES:]:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 记忆写失败绝不影响主流程


def record_failure_pattern(pattern, lesson="", src="auto_detect", context=""):
    """沉淀/去重累加一条 ERL 条目。同 pattern+lesson 前缀已存在→hits+1·否则追加。
    lesson 空时用该 pattern 的默认教训模板。fail-safe False。"""
    try:
        if pattern not in _PATTERNS:
            return False
        lesson = str(lesson or "").strip() or _PATTERNS[pattern]["lesson"]
        rows = _load()
        for r in rows:
            if (r.get("pattern") == pattern
                    and str(r.get("lesson", "")).startswith(lesson[:40])):
                r["hits"] = int(r.get("hits") or 0) + 1
                r["last_hit"] = time.time()
                _save(rows)
                return True
        rows.append({"pattern": pattern, "lesson": lesson[:400],
                     "desc": _PATTERNS[pattern]["desc"], "src": str(src)[:60],
                     "context": str(context)[:160], "ts": time.time(), "hits": 1})
        _save(rows)
        return True
    except Exception:
        return False


def match_failure_memory(patterns, touch=True):
    """命中: patterns(检测出的模式名list或[{pattern,..}]) 对应的历史条目。
    touch=顺带 hits+1(该教训又被实际用到一次·排序依据)。fail-safe []。"""
    try:
        names = {p["pattern"] if isinstance(p, dict) else str(p) for p in (patterns or [])}
        rows = _load()
        hit = [r for r in rows if r.get("pattern") in names]
        if hit and touch:
            for r in rows:
                if r.get("pattern") in names:
                    r["hits"] = int(r.get("hits") or 0) + 1
                    r["last_hit"] = time.time()
            _save(rows)
        # 同 pattern 取 hits 最高的那条(去重后每 pattern 一条)
        best = {}
        for r in hit:
            k = r.get("pattern")
            if k not in best or int(r.get("hits") or 0) > int(best[k].get("hits") or 0):
                best[k] = r
        return sorted(best.values(), key=lambda r: -int(r.get("hits") or 0))
    except Exception:
        return []


def format_memory_hint(patterns):
    """渲染注入 prompt 的文字。未命中返回 ""(调用方无脑拼即可)。"""
    try:
        rows = match_failure_memory(patterns, touch=False)
        if not rows:
            return ""
        lines = ["⚠️历史失败模式记忆(ERL)——同类失败信号已出现过·这是栽过跟头换来的路:"]
        for r in rows[:3]:
            lines.append(f"  · [{r.get('pattern')}·栽过{int(r.get('hits') or 1)}次] "
                         f"{str(r.get('lesson') or '')[:220]}")
        return "\n".join(lines)
    except Exception:
        return ""


def ensure_defaults():
    """幂等初始化: 四种模式各预置一条默认教训条目(反思脑 lesson 会另存新条目)。
    评估环境首次跑(还没有任何历史)也能立刻吃到默认教训·不用先失败一轮。"""
    try:
        rows = _load()
        have = {r.get("pattern") for r in rows}
        changed = False
        for p in _PATTERNS:
            if p not in have:
                rows.append({"pattern": p, "lesson": _PATTERNS[p]["lesson"],
                             "desc": _PATTERNS[p]["desc"], "src": "preset",
                             "context": "", "ts": time.time(), "hits": 0})
                changed = True
        if changed:
            _save(rows)
        return changed
    except Exception:
        return False


if __name__ == "__main__":  # 冒烟自测(真实交错场景: 搜索与被拦 fetch 交替)
    ensure_defaults()
    log = [
        {"tool": "run_parallel_search", "summary": "新增 0 条证据，累计 3 条"},
        {"tool": "fetch_url", "summary": "[⚡死域熔断·本次调用已拦截] 域名 web.archive.org…"},
        {"tool": "run_parallel_search", "summary": "新增 0 条证据，累计 3 条"},
        {"tool": "fetch_url", "summary": "[⚡死域熔断·本次调用已拦截] 域名 web.archive.org…"},
        {"tool": "run_parallel_search", "summary": "新增 0 条证据，累计 3 条"},
    ]
    pats = detect_failure_patterns(log)
    print("检出:", pats)
    assert any(p["pattern"] == "synonym_rewrite_loop" for p in pats), "死循环漏检!"
    assert any(p["pattern"] == "dead_domain_reram" for p in pats), "死域连撞漏检!"
    print(format_memory_hint(pats))
    print("SMOKE PASS")
