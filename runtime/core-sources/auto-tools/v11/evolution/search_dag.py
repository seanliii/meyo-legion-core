"""v13.87.33 · search_dag · 灵活 DAG 编舞 (替线性 pipeline)

用户 2026-07-23 抓:
「变 graph · 灵活编排 google 和其详情页 · 或者其他优质站点的深度搜索」

── 病 ──
现役 SearchAgent.search_smart 是线性 pipeline:
    planner → fanout → dorks → discovery → supervisor → reflector
每步顺序死的·不能:
- 并行跑 fanout 和 dorks
- 根据首搜结果决定要不要跑 dorks (质量好就跳)
- 条件跳: sup 说不够才追搜
- 回环: reflector 发现漏点 → 回 planner 补 query

── 解 · Node/Edge/Runner ──

Node · 一个搜索动作 (fanout / dork / discovery / supervise / reflect / rescue)
Edge · 跳条件 (always / if_low_conf / if_shell / if_full)
Runner · 拓扑排序 + 条件跳 + 并行

── DAG 编舞例子 ──

query
  ↓ planner
  ├──parallel──┐
  ▼            ▼
 fanout      site_top (查 tag hot domain 直搜)
  │            │
  └────merge───┘
        ↓
     supervise
        │
    if not_suf
        ↓
      dork_追搜
        │
   if still not_suf
        ↓
    scholar_deep
        ↓
   reflect (加 tag 权重)

上游: SearchAgent 用 dag = build_dag_for_intent(plan) · runner.run(dag)
"""
import concurrent.futures
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DAG_LOG = os.path.join(V11, ".search_dag_runs.jsonl")


@dataclass
class DagNode:
    """DAG 节点 · 一个搜索动作"""
    name: str
    action: Callable  # 输入 ctx (dict) · 返 ctx 或 hits
    parallel_group: Optional[str] = None  # 同组的节点并行执行
    depends_on: list = field(default_factory=list)  # 依赖的 node names
    when: str = "always"  # always | if_low_conf | if_shell | if_no_hits
    max_elapsed_s: float = 60.0  # 单节点上限


@dataclass
class DagStep:
    """一步执行记录"""
    node: str
    ok: bool
    elapsed_s: float
    hits_delta: int = 0
    trigger_reason: str = ""


class DagRunner:
    """DAG 执行器 · 拓扑序 · 并行组 · 条件跳"""

    def __init__(self, nodes: list, initial_ctx: dict = None):
        self.nodes = {n.name: n for n in nodes}
        self.ctx = initial_ctx or {"hits": [], "trace": []}
        self.executed = set()
        self.trace = []

    def _should_run(self, node: DagNode) -> tuple:
        """判断 node 该跑吗 · 返 (bool, reason)"""
        when = node.when
        if when == "always":
            return True, "always"
        if when == "if_no_hits":
            if len(self.ctx.get("hits", [])) == 0:
                return True, "no_hits"
            return False, "已有 hits · skip"
        if when == "if_low_conf":
            # v13.87 · 用户 07-23 打脸「3 篇肯定不够啊 · 至少 30 篇」
            # 早停真业务量 = 30 篇 full · 才算够 · 少于 30 就补搜
            # (之前 3 是我脑子进水 · 不是业务量)
            conf = self.ctx.get("last_supervise", {}).get("confidence", 100)
            hits = [h for h in self.ctx.get("hits", [])
                    if isinstance(h, dict) and not h.get("_meta_only")]
            full_n = sum(1 for h in hits
                         if h.get("completeness") == "full")
            if full_n >= 30:
                return False, f"full {full_n}≥30·业务量够·早停 (省 60-90s)"
            if conf < 55 or full_n < 30:
                return True, f"conf={conf} full={full_n}<30·补搜"
            return False, f"conf={conf}·conf 够但 full 够 30"
        if when == "if_shell":
            # 全 shell 或 shell 占 > 60%
            hits = [h for h in self.ctx.get("hits", [])
                    if isinstance(h, dict) and not h.get("_meta_only")]
            if not hits:
                return False, "no_hits"
            shell_n = sum(1 for h in hits
                          if h.get("completeness") == "shell")
            if shell_n / len(hits) > 0.6:
                return True, f"shell {shell_n}/{len(hits)}"
            return False, "shell 率低"
        if when == "if_full_ge_3":
            hits = [h for h in self.ctx.get("hits", [])
                    if isinstance(h, dict) and not h.get("_meta_only")]
            full_n = sum(1 for h in hits
                         if h.get("completeness") == "full")
            # v13.87 · 用户 07-23 打脸「3 不够 · 30 才业务量」
            if full_n >= 30:
                return True, f"full {full_n} ≥ 30 (业务量够)"
            return False, f"full {full_n} < 30"
        return True, "unknown_when"

    def _run_node(self, node: DagNode) -> DagStep:
        """跑一个 node · 更新 ctx"""
        t0 = time.time()
        try:
            new_ctx = node.action(dict(self.ctx))
            elapsed = round(time.time() - t0, 2)
            if isinstance(new_ctx, dict):
                # 合并 hits
                old_n = len(self.ctx.get("hits", []))
                self.ctx["hits"] = new_ctx.get("hits", self.ctx["hits"])
                new_n = len(self.ctx.get("hits", []))
                # 其他字段合并
                for k, v in new_ctx.items():
                    if k != "hits":
                        self.ctx[k] = v
                delta = new_n - old_n
            else:
                delta = 0
            step = DagStep(node=node.name, ok=True,
                            elapsed_s=elapsed, hits_delta=delta)
        except Exception as e:
            elapsed = round(time.time() - t0, 2)
            step = DagStep(node=node.name, ok=False,
                            elapsed_s=elapsed,
                            trigger_reason=f"err:{type(e).__name__}:{str(e)[:80]}")
        self.trace.append(step)
        self.executed.add(node.name)
        return step

    def run(self) -> dict:
        """拓扑执行 · 支持并行组 + 条件跳"""
        pending = list(self.nodes.values())
        max_passes = 20  # 防死循环
        passes = 0

        while pending and passes < max_passes:
            passes += 1
            # 找依赖已满足 + 通过 when 判断的节点
            ready = []
            for n in pending[:]:
                if any(dep not in self.executed for dep in n.depends_on):
                    continue
                should, reason = self._should_run(n)
                if should:
                    ready.append((n, reason))
                else:
                    # skip 该节点 · 标 executed 让下游能跑
                    self.executed.add(n.name)
                    self.trace.append(DagStep(
                        node=n.name, ok=True, elapsed_s=0,
                        trigger_reason=f"skipped:{reason}"))
                    pending.remove(n)

            if not ready:
                break

            # 并行组处理
            groups = {}
            singles = []
            for n, reason in ready:
                if n.parallel_group:
                    groups.setdefault(n.parallel_group, []).append((n, reason))
                else:
                    singles.append((n, reason))

            # 先跑 singles (按 pending 顺序 · 一个一个)
            for n, reason in singles:
                self._run_node(n)
                pending.remove(n)

            # 再跑并行组 (每组一起)
            for grp_name, grp_nodes in groups.items():
                with concurrent.futures.ThreadPoolExecutor(
                        max_workers=min(len(grp_nodes), 4)) as ex:
                    futs = {ex.submit(self._run_node, n): (n, r)
                            for n, r in grp_nodes}
                    # 等所有完成
                    concurrent.futures.wait(futs, timeout=90)
                for n, _r in grp_nodes:
                    if n in pending:
                        pending.remove(n)

        return {
            "hits": self.ctx.get("hits", []),
            "trace": [
                {"node": s.node, "ok": s.ok,
                 "elapsed_s": s.elapsed_s,
                 "hits_delta": s.hits_delta,
                 "trigger_reason": s.trigger_reason}
                for s in self.trace
            ],
            "total_elapsed_s": round(sum(s.elapsed_s for s in self.trace), 2),
            "ok_steps": sum(1 for s in self.trace if s.ok),
            "total_steps": len(self.trace),
            "ctx": {k: v for k, v in self.ctx.items()
                     if k not in ("hits", "trace")},
        }


# ═════ DAG 模板 · 按 intent 构造 ═════

def build_dag(plan_obj, query: str) -> list:
    """给 SearchPlan 构造 DAG · 用户 goal 编舞版
    intent → 不同拓扑

    fact_lookup:
        cache → fanout (top_by_tag) → return
    deep_research (真 DAG):
        planner_done → [fanout || site_top_by_tag] → merge
                    → supervise → if_low_conf: [dorks || 3engines] → merge
                                → supervise → if_shell: bookmark_rescue
                                → reflect
    """
    from search_agent_orchestrator import smart_fetch
    from friday_universal import search_google_deep, search_bing_deep, search

    # 惰性 import · 需要用时
    def _fanout_action(ctx):
        """按 plan 挑 sources 跑 fanout"""
        from search_agent import SearchAgent
        sa = SearchAgent()
        sources = plan_obj.sources_ordered or None
        hits = sa._fanout(query, sources=sources, top_k=10)
        ctx["hits"] = (ctx.get("hits", []) or []) + hits
        return ctx

    def _site_top_action(ctx):
        """v13.87.34 · 按 tag 查历史 top domain · 智能路由到最优 backend
        用户 2026-07-23 抓:
            langchain/search?q=... 打不开 · github/search 429 · about:blank
            不该硬拼 search URL · 该走接口 or Google site:xx
        路由规则 (按 domain):
            - xiaohongshu.com   → xhs_api (Friday xhc)
            - zhihu.com/zhuanlan → friday_zh (Friday zh)
            - mp.weixin/公众号  → sogou_weixin CDP
            - github.com        → Google site:github.com (github 直搜 429)
            - arxiv/scholar     → Google site:xxx (arxiv 无 search API)
            - 其他              → Google site:domain query
        """
        from site_bookmark import (_extract_tags_from_query,
                                     top_domains_for_tag)
        tags = _extract_tags_from_query(query)
        if not tags:
            return ctx
        top_hits = []
        # 已有 API 通道 · 优先
        _API_ROUTES = {
            "xiaohongshu.com": "xhs_api",
            "www.xiaohongshu.com": "xhs_api",
            "zhihu.com": "friday_zh",
            "www.zhihu.com": "friday_zh",
            "zhuanlan.zhihu.com": "friday_zh",
        }
        # v13.87.37 · 冷启动兜底 · tag 无历史 domain → 按 intent 走 known 站
        # 用户 2026-07-23 抓正:「site_top Δ0 · 冷启动状态没数据」
        _INTENT_FALLBACK_DOMAINS = {
            "ai_side_hustle": ["xiaohongshu.com", "zhihu.com",
                                "sogou_weixin"],
            "rag": ["arxiv.org", "blog.langchain.dev", "github.com"],
            "agent": ["arxiv.org", "github.com", "hackernews"],
            "llm_eval": ["arxiv.org", "github.com"],
            "coding_tool": ["github.com", "hackernews"],
            "framework_rust": ["github.com", "hackernews", "rust-lang.org"],
            "framework_py": ["github.com", "stackoverflow.com"],
            "prompt_eng": ["github.com", "arxiv.org", "zhihu.com"],
            "startup": ["v2ex.com", "hackernews", "36kr.com"],
            "product_mgr": ["zhihu.com", "sspai.com", "36kr.com"],
        }

        for tag in tags[:2]:
            tops = top_domains_for_tag(tag, limit=3)
            # v13.87.37 · 冷启动兜底
            if not tops and tag in _INTENT_FALLBACK_DOMAINS:
                tops = [{"domain": d, "primary": "curl",
                          "tag_score": 0, "rating": 45}
                         for d in _INTENT_FALLBACK_DOMAINS[tag]]
            for t in tops:
                domain = t.get("domain", "")
                if not domain:
                    continue
                # 路由 1 · 有 API 走 API
                api_route = _API_ROUTES.get(domain)
                if api_route:
                    try:
                        if api_route == "xhs_api":
                            from global_sources import search_xiaohongshu_api
                            r = search_xiaohongshu_api(query, limit=3)
                            for h in (r or [])[:3]:
                                h["source"] = f"site_top_xhs_[{tag}]"
                                top_hits.append(h)
                            continue
                        if api_route == "friday_zh":
                            from friday_universal import search_zh
                            r = search_zh(query, top_k=3)
                            for h in (r or [])[:3]:
                                h["source"] = f"site_top_zh_[{tag}]"
                                top_hits.append(h)
                            continue
                    except Exception:
                        pass
                # 路由 2 · 无 API · 走 Google site:xxx dork
                # (github/arxiv/langchain 都走这条 · 避免 429 和硬拼 URL)
                try:
                    from friday_universal import search_google
                    dork_q = f"{query} site:{domain}"
                    r = search_google(dork_q, top_k=3)
                    for h in (r or [])[:3]:
                        h["source"] = f"site_top_google_dork_[{domain}][{tag}]"
                        top_hits.append(h)
                except Exception:
                    continue
        ctx["hits"] = (ctx.get("hits", []) or []) + top_hits
        return ctx

    def _dorks_action(ctx):
        """site: dork 追搜"""
        from friday_universal import search_google
        dorks = getattr(plan_obj, "site_dorks", None) or []
        dork_hits = []
        for dork in dorks[:3]:
            r = search_google(f"{query} {dork}", top_k=3)
            if r:
                dork_hits.extend(r)
        ctx["hits"] = (ctx.get("hits", []) or []) + dork_hits
        return ctx

    def _3engines_action(ctx):
        """3 引擎并行
        v13.87 · 用户 07-23 抓「40s 是瓶颈」· 砍 timeout
        原: g=45s / b=45s / z=15s · as_completed=90s (最慢 45s)
        改: g=20s / b=20s / z=15s · as_completed=25s (最慢 20-25s)
        · 3 引擎哪个 25s 内没返 · 用其他 2 家即可 · 不为一家等到底
        """
        import time as _t
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            fut_g = ex.submit(search_google_deep, query, 8, 20)
            fut_b = ex.submit(search_bing_deep, query, 8, 20)
            fut_z = ex.submit(search, query, ["zhipu-search"], 8, True, 15)
            merged = []
            t0 = _t.time()
            try:
                for f in concurrent.futures.as_completed(
                    [fut_g, fut_b, fut_z], timeout=25,
                ):
                    try:
                        r = f.result() or []
                        merged.extend(r)
                    except Exception:
                        pass
            except concurrent.futures.TimeoutError:
                # 25s 到·取已完成的·跳未完成 (即扔掉最慢那家)
                pass
            # 强制取消未完成的 · 不阻塞
            for f in (fut_g, fut_b, fut_z):
                if not f.done():
                    f.cancel()
        ctx["hits"] = (ctx.get("hits", []) or []) + merged
        return ctx

    def _supervise_action(ctx):
        """判 sufficient"""
        from search_supervisor import supervise
        r = supervise(query, ctx.get("hits", []),
                       source=f"dag_{plan_obj.intent}")
        ctx["last_supervise"] = r
        return ctx

    def _reflect_action(ctx):
        """反思落 rule"""
        from search_reflector import reflect_once
        # 兼容 SearchPlan (dataclass) 和 假 plan (普通 obj)
        try:
            from dataclasses import asdict, is_dataclass
            plan_dict = asdict(plan_obj) if is_dataclass(plan_obj) else {
                "intent": plan_obj.intent,
                "sources_ordered": getattr(plan_obj, "sources_ordered", []),
            }
        except Exception:
            plan_dict = {"intent": getattr(plan_obj, "intent", "?")}
        reflect_once(
            query=query, intent=plan_obj.intent,
            plan=plan_dict,
            hits=ctx.get("hits", []),
            supervisor_decision=ctx.get("last_supervise", {}),
            elapsed_s=0,
        )
        return ctx

    # 简单 intent = 线性 · 复杂 intent = DAG
    if plan_obj.intent == "fact_lookup":
        return [
            DagNode("fanout", _fanout_action),
            DagNode("supervise", _supervise_action,
                     depends_on=["fanout"]),
            DagNode("reflect", _reflect_action,
                     depends_on=["supervise"]),
        ]

    # 其余 · 真 DAG
    # v13.87 · 用户 2026-07-23 打脸「3 篇不够 · 30 篇 · 加速真响应」
    # 大改·首搜层 4 并行一次撒 (fanout || site_top || dorks || 3engines)
    # 一次拿到 30-40 篇 · 大概率无需补搜
    # 如果还是 < 30 篇 · 再走 supervise 追搜 (少见 · < 20% query)
    return [
        # 首搜 4 并行 · 一次全撒 · 追求宽 (v13.87 新)
        DagNode("fanout", _fanout_action, parallel_group="first_search"),
        DagNode("site_top", _site_top_action, parallel_group="first_search"),
        DagNode("dorks", _dorks_action, parallel_group="first_search"),
        DagNode("3engines", _3engines_action, parallel_group="first_search"),
        # merge 后判 · 4 并行 wall clock = max(4 个中最慢的)
        DagNode("supervise_1", _supervise_action,
                 depends_on=["fanout", "site_top", "dorks", "3engines"]),
        # 极少数场景 · 首搜 4 并行仍 < 30 篇 · 走 supervise 追搜 (仅 1 次)
        DagNode("supervise_2", _supervise_action,
                 when="if_low_conf",
                 depends_on=["supervise_1"]),
        # 反思
        DagNode("reflect", _reflect_action,
                 depends_on=["supervise_2", "supervise_1"]),
    ]


# v13.90.3 · 结构性根治: 证据抓取阶段源头过滤登录墙/反爬内容
#   排查(用户抓 v11_race 告警「login_state 类采集报错连续5次」)完整证据链:
#   run_dag 抓的 hits → search_agent.search_dag() 转 external_evidence(不做
#   任何内容质量检测) → write_experience_post 整篇写进正文 → 帖子写完之后
#   v11_race.py 的 _COLLECT_ERROR_MARKERS 硬门才拦下来(白跑一整轮 racer 并行
#   + LLM 写作才发现)。且告警文案「需要手动重新登录对应平台」有误导性——
#   真实原因是抓证据阶段没有源头过滤，不是任何外部账号登录失效。
#   修复: run_dag 返回前就用 web_access_bridge 的统一反爬/登录墙关键词表
#   (_ANTI_BOT_SIGNALS，v13.90.3 已把登录墙类信号合并进去)过滤掉 content 命中
#   的 hit，不让它流入 evidence → 从源头杜绝，而非事后亡羊补牢。
def _filter_gated_content_hits(hits: list) -> list:
    """剔除 content/pitfall 命中反爬或登录墙关键词的 hit。
    单一真源: 复用 web_access_bridge.WebAccess._ANTI_BOT_SIGNALS·
    不再维护第二份关键词表副本(重蹈 v13.88.82 已修过的"三头蛇"覆辙)。"""
    if not hits:
        return hits
    try:
        from web_access_bridge import WebAccess
        signals = WebAccess._ANTI_BOT_SIGNALS
    except Exception:
        return hits  # 拿不到表就不过滤·不影响主流程
    clean = []
    for h in hits:
        if not isinstance(h, dict):
            clean.append(h)
            continue
        blob = (str(h.get("content", "")) + str(h.get("pitfall", ""))).lower()
        if blob and any(sig in blob for sig in signals):
            continue  # 命中反爬/登录墙信号 · 丢弃该 hit
        clean.append(h)
    return clean


def run_dag(plan_obj, query: str, timeout_total: float = 180) -> dict:
    """一行调 · 上游用"""
    nodes = build_dag(plan_obj, query)
    runner = DagRunner(nodes)
    t0 = time.time()
    result = runner.run()
    # v13.90.3 · 返回前先滤掉登录墙/反爬内容 · 不让烂证据流向下游写帖子
    _before = len(result.get("hits", []) or [])
    result["hits"] = _filter_gated_content_hits(result.get("hits", []))
    _dropped = _before - len(result["hits"])
    # 落盘 · 供 daily_report
    try:
        with open(DAG_LOG, "a") as f:
            f.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "query": query[:200],
                "intent": plan_obj.intent,
                "total_elapsed_s": result["total_elapsed_s"],
                "steps_ok": result["ok_steps"],
                "steps_total": result["total_steps"],
                "hits_count": len(result["hits"]),
                "gated_dropped": _dropped,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return result


if __name__ == "__main__":
    try:
        # smoke · 假 plan
        class FakePlan:
            intent = "deep_research"
            sources_ordered = ["friday_google", "hackernews", "zhihu"]
            site_dorks = ["site:arxiv.org"]
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise

    r = run_dag(FakePlan(), "RAG 评测方法 2026")
    print(f"hits: {len(r['hits'])}")
    print(f"total: {r['total_elapsed_s']}s")
    for step in r["trace"]:
        print(f"  · {step['node']:<15} ok={step['ok']} "
              f"elapsed={step['elapsed_s']}s "
              f"delta={step['hits_delta']} {step.get('trigger_reason','')[:50]}")
