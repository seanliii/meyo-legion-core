# -*- coding: utf-8 -*-
"""wallclock_optim · 墙钟优化引擎轻量落地 (2026-08-18 · doc: agent_wallclock_optimization)
================================================================================

病根(doc 实证): 2485s 墙钟里 1200-1500s 耗在【串行等待工具返回】——机制完备
(收敛分诊/硬墙收尾/超时自愈)解决的是"错误路径的收敛"·没解决"正确路径的加速"。
本模块落地三大策略的轻量版(零新依赖·全 fail-safe·不碰既有收敛链):

1) PASTE-lite · 推测性预取 (策略1·E2E延迟-48.5%的论文思想)
   微观模式: search 输出的 url 字段 ≈95% 成为下一步 fetch_url_smart 的参数
   (producer-consumer 模式)。→ 搜索结果落账后·后台线程立刻预热抓取 top URL·
   agent 真正调 fetch 时命中缓存=零等待。纯网络层(wallclock_optim 自己的
   fetcher)·不碰 session·不产生副作用·失败即无缓存(行为退回原样)。

2) BAVT-lite · 预算感知利用门 (策略2·剩余资源比例 r 缩放探索↔利用)
   r>0.35 → explore(默认·不动);  r≤0.35 → exploit: 已有锁定 URL(抓成功过的
   证据源)时·新的【广撒网搜索】(run_parallel_search/run_deep_research_swarm)
   先硬拦一次(附锁定 URL 清单·指路"深读这些别再撒网")·之后每次软提示。
   定向反查(query_web_archive/query_structured_db/archive_today_search)是
   枢轴扩散的正路·不拦(与 doc1 pivot-diffuser 协同·不冲突)。

3) 工具耗时遥测 (doc§一"时间黑洞分布"的可观测性落地)
   每次工具调用的耗时落 budget["_tool_time_hist"/"_tool_time_sum"]·
   复盘时一眼看出哪类工具吃掉了墙钟——后续调 per-tool timeout/预取优先级的
   依据·不再拍脑袋。

L1 per-tool 超时已有(run_delegated 15-180s)·L3 硬墙/诚实收尾已有·不重复造。
"""
import os
import re as _re
import threading
import time

# ── 预取存储(进程内·线程安全) ────────────────────────────────────────
_PREFETCH_LOCK = threading.Lock()
_PREFETCH = {}            # norm_url -> {"content": str, "via": str, "ts": float}
_PREFETCH_TTL_S = 900.0   # 15min: 同一题内有效·跨题不串(进程内生命周期足够)
_PREFETCH_MAX = 24        # 存储上限(防长跑题撑爆内存)
_INFLIGHT = set()         # 正在预取的 norm_url(防重复起线程)
_PREFETCH_SEM = threading.Semaphore(3)   # 并发上限(对目标站友好·防DDoS式预取)

_UAS = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
)
_SKIP_EXT = _re.compile(
    r"\.(jpg|jpeg|png|gif|webp|svg|ico|mp3|mp4|avi|mkv|wav|zip|rar|7z|gz|tgz|dmg|exe|dmg)(\?|$)",
    _re.I)
_MAX_CONTENT = 400_000    # 单 URL 内容上限(400KB·与证据链同量级)
_MIN_CONTENT = 500        # 低于此长度视为无效(与 google-cache 判据一致)

_STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state")
_DEAD_DOMAIN_FILE = os.path.join(_STATE_DIR, "dead_domains.json")


def norm_url(url: str) -> str:
    """URL 归一化: 去片段/去尾斜杠/scheme与host小写——预取命中判据。"""
    u = str(url or "").strip()
    if "#" in u:
        u = u.split("#", 1)[0]
    m = _re.match(r"^(https?)://([^/]+)(.*)$", u, _re.I)
    if not m:
        return u.rstrip("/")
    scheme, host, rest = m.group(1).lower(), m.group(2).lower(), m.group(3).rstrip("/")
    return f"{scheme}://{host}{rest}"


def _dead_domains() -> set:
    """读全局死域熔断表(读失败=空集·fail-safe)。预取不撞已知死域。"""
    try:
        import json as _json
        with open(_DEAD_DOMAIN_FILE, encoding="utf-8") as f:
            data = _json.load(f)
        doms = set()
        for k, v in (data or {}).items():
            if isinstance(v, dict) and (v.get("blocked") or (time.time() - float(v.get("ts") or 0) < 6 * 3600)):
                doms.add(str(k).lower())
        return doms
    except Exception:
        return set()


def _fetch_one(url: str):
    """预取器：复用可插拔抓取注册表，无法加载时才使用轻量兜底。

    预取不再自己写死 ``google cache → requests`` 的工具顺序；它与正式抓取
    共用 ``FetchToolRegistry``，因此已注册的新工具会自动参与预热。兜底保留是
    为了保证演进目录不可导入时不影响主 Agent 的正常运行。
    """
    try:
        import sys
        _evolution = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "v11", "evolution")
        if _evolution not in sys.path:
            sys.path.insert(0, _evolution)
        from search_fabric_fetch import fetch_with_gradient
        result = fetch_with_gradient(url, timeout=12, max_tier=2)
        content = str(result.get("content") or "")
        if result.get("ok") and len(content) >= _MIN_CONTENT:
            return content[:_MAX_CONTENT], str(result.get("via") or "fetch_registry")
    except Exception:
        pass

    # 最小兜底：注册表不可用时仍可预取公开的静态文本；失败即视为未预取。
    try:
        import requests as _rq
        ua = _UAS[hash(url) % len(_UAS)]
        r = _rq.get(url, timeout=12, headers={"User-Agent": ua}, allow_redirects=True)
        ct = str(r.headers.get("content-type") or "")
        if r.status_code == 200 and ("text" in ct or "html" in ct or "json" in ct) \
                and len(r.text) >= _MIN_CONTENT:
            return r.text[:_MAX_CONTENT], "requests_fallback"
    except Exception:
        pass
    return None, None


def _prefetch_worker(url: str):
    """后台预取线程(daemon·信号量限流·结果入 store)。"""
    with _PREFETCH_SEM:
        try:
            content, via = _fetch_one(url)
            if content:
                with _PREFETCH_LOCK:
                    if len(_PREFETCH) >= _PREFETCH_MAX:
                        # 淘汰最老条目
                        _oldest = min(_PREFETCH.items(), key=lambda kv: kv[1].get("ts", 0))
                        _PREFETCH.pop(_oldest[0], None)
                    _PREFETCH[norm_url(url)] = {"content": content, "via": via,
                                                "ts": time.time()}
        except Exception:
            pass
        finally:
            with _PREFETCH_LOCK:
                _INFLIGHT.discard(norm_url(url))


def prefetch_urls(urls, max_n: int = 2):
    """对候选 URL 起后台预取线程(去重/死域/二进制后缀过滤)。返回起的线程数。"""
    started = 0
    try:
        dead = _dead_domains()
        seen = set()
        for raw in (urls or []):
            if started >= max_n:
                break
            u = str(raw or "").strip()
            if not u.startswith(("http://", "https://")):
                continue
            nu = norm_url(u)
            if nu in seen:
                continue
            seen.add(nu)
            if _SKIP_EXT.search(u):
                continue
            dom = _re.sub(r"^https?://", "", nu).split("/", 1)[0].lower()
            if dom in dead or any(dom.endswith("." + d) for d in dead):
                continue
            with _PREFETCH_LOCK:
                if nu in _INFLIGHT or (nu in _PREFETCH and
                                       time.time() - _PREFETCH[nu]["ts"] < _PREFETCH_TTL_S):
                    continue
                _INFLIGHT.add(nu)
            t = threading.Thread(target=_prefetch_worker, args=(u,), daemon=True)
            t.start()
            started += 1
    except Exception:
        pass
    return started


def take_prefetched(url: str):
    """取走预取内容(一次性·命中即弹出防重复)。TTL 过期返回 None。"""
    try:
        nu = norm_url(url)
        with _PREFETCH_LOCK:
            hit = _PREFETCH.pop(nu, None)
        if hit and time.time() - hit.get("ts", 0) <= _PREFETCH_TTL_S:
            return hit
    except Exception:
        pass
    return None


def _is_search_hit(e) -> bool:
    src = str((e or {}).get("source") or "")
    return ("search" in src or "swarm" in src) and (e or {}).get("url")


def _is_fetched(e) -> bool:
    src = str((e or {}).get("source") or "")
    return ("fetch" in src or "traverse" in src) and (e or {}).get("url")


def maybe_prefetch_after_search(session, max_n: int = 2):
    """搜索类工具落账后的钩子: 从最近搜索命中里挑还没抓过的 top URL 后台预热。
    PASTE 的 producer-consumer 模式落地——search 的 url 字段就是下一步 fetch
    的参数(95%)·趁 LLM 还在思考的时间重叠把网络 I/O 提前做掉。fail-safe。"""
    try:
        ev = (session or {}).get("evidence") or []
        if not ev:
            return 0
        fetched = {norm_url(e.get("url")) for e in ev if _is_fetched(e)}
        cands = []
        for e in reversed(ev[-25:]):     # 最近的搜索命中优先
            if _is_search_hit(e):
                nu = norm_url(e.get("url"))
                if nu not in fetched:
                    cands.append(str(e.get("url")))
        return prefetch_urls(cands, max_n=max_n)
    except Exception:
        return 0


# ── BAVT-lite · 预算感知利用门 ────────────────────────────────────────
_BROAD_SEARCH_TOOLS = {"run_parallel_search", "run_deep_research_swarm"}
_EXPLOIT_RATIO = 0.35      # 剩余<35% → explore 切 exploit


def budget_ratio(session) -> float:
    """剩余墙钟比例 r(BAVT 的资源缩放指数)。未知返回 1.0(=全额探索)。"""
    try:
        b = (session or {}).get("_budget") or {}
        hard = float(b.get("hard_wall_s") or 0)
        t0 = float(b.get("t_start") or 0)
        if hard <= 0 or t0 <= 0:
            return 1.0
        return max(0.0, (hard - (time.time() - t0)) / hard)
    except Exception:
        return 1.0


def locked_urls(session, max_n: int = 4):
    """锁定 URL = 抓取成功过的证据源(fetch/traverse 落过账的)·exploit 模式的
    深读目标清单。按最近优先·按 url 去重。"""
    try:
        ev = (session or {}).get("evidence") or []
        out, seen = [], set()
        for e in reversed(ev):
            if _is_fetched(e):
                u = str(e.get("url") or "")
                nu = norm_url(u)
                if nu and nu not in seen:
                    seen.add(nu)
                    out.append(u)
                    if len(out) >= max_n:
                        break
        return out
    except Exception:
        return []


def exploit_gate(session, tool_name: str):
    """BAVT-lite 利用门: exploit 模式下对【广撒网搜索】先硬拦一次·后软提示。

    返回 None=不管(放行原逻辑) 或 (allow, msg) 交给 _check_budget 返回。
    fail-safe: 任何异常返回 None。
    """
    try:
        if tool_name not in _BROAD_SEARCH_TOOLS:
            return None
        s = session or {}
        b = s.setdefault("_budget", {})
        if budget_ratio(s) > _EXPLOIT_RATIO:
            return None
        locked = locked_urls(s)
        if not locked:
            return None  # 没有可利用的锁定源·探索仍是唯一路·别拦
        _rem_s = budget_ratio(s)
        _lst = "\n".join(f"  · {u[:110]}" for u in locked)
        _core = (f"[BAVT·利用模式] 墙钟剩余 {_rem_s:.0%}·从【探索】切换到【利用】(预算感知"
                 f"值树的 r→0 收紧):你已锁定这些抓取成功的证据源:\n{_lst}\n"
                 f"正确动作不是再撒网新 query·而是:①深读/解析上述已锁定源"
                 f"(extract_cards/run_factual_extract/fetch_url_smart 复读关键段) "
                 f"②定向反查不受限(query_web_archive/query_structured_db/"
                 f"archive_today_search 枢轴扩散照常) ③证据够了立即 finalize_answer。")
        if not b.get("_exploit_blocked_once"):
            b["_exploit_blocked_once"] = True
            return False, _core
        # 硬拦过一次后转软提示(随结果附加·不剥夺探索权·防真需要的关键一搜被饿死)
        return True, f"\n\n{_core}\n(若这确是关键一搜·简述理由后可继续)"
    except Exception:
        return None


# ── 工具耗时遥测(doc§一 时间黑洞分布的可观测性) ──────────────────────
def record_tool_time(session, tool_name: str, dt_s: float):
    """每次工具调用耗时落账·给"哪类工具吃掉了墙钟"提供数据。fail-safe。"""
    try:
        b = (session or {}).setdefault("_budget", {})
        hist = b.setdefault("_tool_time_hist", [])
        hist.append({"tool": str(tool_name or "?")[:40], "dt": round(float(dt_s), 1),
                     "ts": round(time.time(), 1)})
        if len(hist) > 300:
            del hist[: len(hist) - 300]
        sums = b.setdefault("_tool_time_sum", {})
        k = str(tool_name or "?")[:40]
        sums[k] = round(float(sums.get(k) or 0) + float(dt_s), 1)
    except Exception:
        pass


if __name__ == "__main__":  # 冒烟自测(不发真实网络·只测逻辑·网络路径单测见下)
    # 1) norm_url
    assert norm_url("HTTPS://Example.COM/a/#frag") == "https://example.com/a"
    assert norm_url("https://x.com/") == "https://x.com"
    # 2) store 生命周期(手工塞·不联网)
    with _PREFETCH_LOCK:
        _PREFETCH[norm_url("https://good.example.com/page")] = {
            "content": "x" * 800, "via": "manual", "ts": time.time()}
    hit = take_prefetched("https://good.example.com/page#x")
    assert hit and hit["via"] == "manual"
    assert take_prefetched("https://good.example.com/page") is None  # 一次性
    assert take_prefetched("https://never.example.com/") is None
    # 过期不命中
    with _PREFETCH_LOCK:
        _PREFETCH[norm_url("https://old.example.com/")] = {
            "content": "y" * 800, "via": "direct", "ts": time.time() - 9999}
    assert take_prefetched("https://old.example.com/") is None
    # 3) budget_ratio / exploit_gate
    sess = {"_budget": {"hard_wall_s": 1000, "t_start": time.time() - 900}}
    assert budget_ratio(sess) <= 0.11
    sess2 = {"_budget": {"hard_wall_s": 1000, "t_start": time.time() - 100}}
    assert 0.85 <= budget_ratio(sess2) <= 0.95
    # exploit: 有锁定 URL·广搜→先硬拦·后软
    sess3 = {"_budget": {"hard_wall_s": 1000, "t_start": time.time() - 700},  # r≈0.3
             "evidence": [
                 {"url": "https://a.com/s?q=1", "source": "run_parallel_search"},
                 {"url": "https://locked.example.com/post", "source": "fetch_url_smart"},
                 {"url": "https://locked.example.com/post2", "source": "t_fetch_url_smart"},
             ]}
    g1 = exploit_gate(sess3, "run_parallel_search")
    assert g1 and g1[0] is False and "locked.example.com" in g1[1], g1
    g2 = exploit_gate(sess3, "run_parallel_search")
    assert g2 and g2[0] is True, g2
    # 定向反查不拦
    assert exploit_gate(sess3, "query_web_archive") is None
    assert exploit_gate(sess3, "query_structured_db") is None
    # 没锁定源不拦
    sess4 = {"_budget": sess3["_budget"],
             "evidence": [{"url": "https://a.com/s?q=1", "source": "run_parallel_search"}]}
    assert exploit_gate(sess4, "run_parallel_search") is None
    # explore 模式不拦
    assert exploit_gate(sess2, "run_parallel_search") is None
    # locked_urls 去重·最近优先
    lu = locked_urls(sess3)
    assert lu and all("locked.example.com" in u for u in lu) and len(lu) == 2
    # 4) 遥测
    record_tool_time(sess2, "fetch_url_smart", 12.3)
    record_tool_time(sess2, "fetch_url_smart", 4.2)
    assert sess2["_budget"]["_tool_time_sum"]["fetch_url_smart"] == 16.5
    assert len(sess2["_budget"]["_tool_time_hist"]) == 2
    # 5) 预取候选过滤(maybe_prefetch_after_search·不起真网络: 候选全为死链ext/inflight场景)
    sess5 = {"evidence": [
        {"url": "https://x.com/a.jpg", "source": "run_parallel_search"},
        {"url": "https://x.com/fetched", "source": "fetch_url_smart"},
        {"url": "https://x.com/candidate", "source": "run_parallel_search"},
    ]}
    # 注: 会为 candidate 起真实后台线程(daemon)·这里仅验证不抛+返回类型
    n = maybe_prefetch_after_search(sess5, max_n=1)
    assert isinstance(n, int) and n >= 0
    print("WALLCLOCK OPTIM SMOKE PASS")
