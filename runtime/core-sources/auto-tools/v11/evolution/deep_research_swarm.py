#!/usr/bin/env python3
"""五子 Agent 深度研究编排 · v56c 真多跳迭代检索。

角色固定为五个，Coordinator 只负责编排，不产出研究结论：
1. Planner：把目标改写为可验证的研究子问题和验收条件；
2. Scout：多源发现候选来源，优先找一手资料；
3. Evidence：深读候选来源，抽取可引用的原文证据；
4. Skeptic：主动寻找反例、时效性风险和相反结论；
5. Judge：合并证据与反证，给出可追溯结论及未解决项。

该模块不直接操作浏览器。需要站内交互时，由 deep_research_agent 的浏览器循环
消费本模块返回的 unresolved_questions / candidate_urls 继续处理。

用户 2026-08-04 抓「这本质是单跳并行搜索(Planner 拆问题→Scout/Evidence/Skeptic
并行搜一次→Judge 收口)，不是真正 multi-hop(多轮迭代检索、根据上一跳结果决定
下一跳查询)」。修: 加 HopJudge 跳间判决——每跳搜完先问「这些证据够回答目标吗？
还缺什么？」，不够就让 HopJudge 依据*已发现的具体线索*（不是重复原 query）生成
下一跳查询，再开一轮 Scout/Evidence/Skeptic，直到够了或到跳数上限（默认 3 跳）
或连续两跳零新增证据（收敛判定，防止空转）。全程每跳真实上 agent_comm 总线。
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import sys
import time
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    from audit_core import call_llm, try_parse_json
except Exception:  # 便于离线测试注入替身
    call_llm = None

    def try_parse_json(raw: str, default: dict) -> dict:
        try:
            return json.loads(raw)
        except Exception:
            return default


ROLE_NAMES = ("planner", "scout", "evidence", "skeptic", "judge")

PLANNER_SYSTEM = """你是深度研究 Planner。先判断查询复杂度并选路由（AQR 自适应查询重构），
再把研究目标拆成 2-4 个可验证子问题。
路由规则：
- 简单实体查询（问一个名字/数字/日期）→ 1-2 个子问题，不过度拆解；
- 多约束识别（找满足 N 条约束的实体）→ 按约束分组拆解，每组独立可验证，另出一个消歧子问题（哪个候选同时满足全部约束）；
- 有同名歧义实体 → 第一个子问题必须是消歧（列出同名候选再用约束筛）；
- 多跳链（A 的信息藏在 B 的资料里）→ 按跳序拆解，后跳 query 显式引用前跳预期的实体名。
每个子问题必须给出：claim、query、acceptance（需要什么证据才能成立）。
只返回 JSON：{"complexity":"simple|multi_constraint|ambiguous|multihop","questions":[{"claim":"...","query":"...","acceptance":"..."}]}。"""

JUDGE_SYSTEM = """你是深度研究 Judge。只能依据给定的支持证据和反证输出结论，不能用自身知识补充。
每个结论必须包含 claim、verdict（verified|partial|contradicted|unresolved）、reason、source_urls。
最后列 unresolved_questions。只返回 JSON：
{"conclusions":[{"claim":"...","verdict":"...","reason":"...","source_urls":["..."]}],"unresolved_questions":["..."]}。"""

# v56c · 跳间判决：真 multi-hop 的核心——不是重复原 query，而是依据*本跳已发现的
# 具体线索*（某个专有名词、某个未展开的机制、证据里提到但没查到细节的东西）生成
# 下一跳查询。找不到具体缺口就诚实说 sufficient=true，不允许为了凑跳数瞎编查询。
INTEL_PIVOT_SYSTEM = """你是开源情报侦查员，不是普通关键词改写器。目标网页可能已失效、未被索引或被反爬；你必须由已发现的线索扩展证据图谱，而不是重复原问题。

给出不超过 4 条下一跳查询，每条必须属于下列一种关系，并说明所用的具体节点：
1. identity：作者、编辑、站长、用户名的个人页、社媒、其他站点或署名变体；
2. site_archive：同一域名的年份/月日归档、RSS/Atom、sitemap、友情链接、旧域名或站点迁移；
3. citation：精确标题/曲名/罕见短语的转载、评论、引用、播放列表、网页缓存；
4. neighborhood：同栏目、同日期、同一榜单或相邻文章，用于恢复缺失原帖的上下文。

身份规则：显示名或文风相似只能产生 candidate，不能认定同一人；只有作者自述互链、稳定账号 ID、个人主页互链、或两个独立页面指向同一个人域名时才能生成 confirmed 线索。Blogger profile、Mastodon/Bluesky/GitHub 等个人页 URL 中的稳定 ID 是高优先锚点。社群/聚合页只能用于发现候选，不能单独证明作者归属。

规则：查询必须显式包含输入证据中真实出现的人名、域名、标题、日期、稳定账号 ID 或罕见短语之一；不能凭空发明作者、网址或答案；优先精确引号和 site: 限定。只返回 JSON：
{"pivots":[{"query":"...","relation":"identity|site_archive|citation|neighborhood","node":"来自证据的节点","purpose":"要验证的缺口"}]}。"""


HOP_JUDGE_SYSTEM = """你是深度研究的跳间判决官（HopJudge）。你在多轮迭代检索中间做判断：
当前已收集的证据，是否已经足够验证研究目标？如果不够，具体还缺什么？

规则：
1. 只依据给定证据判断，不能靠自身知识脑补"应该差不多够了"。
2. 如果证据里出现了值得深挖的具体线索（人名/产品名/数据点/未展开的机制/相互矛盾的说法），
   必须把它写成**具体、可搜索**的下一跳查询——不是重复第一跳的原问题，而是针对这个新线索。
   例如证据提到"引入了 Gaia2 评测集"但没说细节 → 下一跳查询 "Gaia2 评测集 具体任务类型 评分标准"。
3. 如果反证（counter_evidence）里出现质疑或版本变化，下一跳查询应验证"这个质疑是否仍然成立/是否已修复"。
4. 最多给 3 个下一跳查询，每个必须标注 gap（这条查询要补的具体缺口是什么）。
5. 如果证据已经交叉印证、没有值得深挖的具体缺口，sufficient=true，next_queries 留空数组。
6. 不允许因为"跳数还没到上限"就编造查询——没有真缺口就诚实收敛。

只返回 JSON：
{"sufficient": true|false, "gap_summary": "20-40字·当前最大的证据缺口是什么(没有缺口写'无')",
 "next_queries": [{"query": "...", "gap": "这条要补什么具体缺口"}]}"""


# U1(2026-08-07) · 蜂群 load 自适应降级 —— 治「多agent很卡」。
# 病根:蜂群一跳起 3 路 lane(Scout/Evidence/Skeptic)各 spawn 一个 claude LLM 进程·
# 固定 max_workers=3 不管系统 load 多高都并发拉满 → 一个深研任务瞬间 6-9 个 claude·
# CPU 吃满卡顿。这里按实时 load 自适应降并发:load 越高·同时跑的 lane 越少(串行化)·
# 不再无脑拉满。低 load 时行为完全不变(不牺牲质量)。旋钮 MEYO_SWARM_MAX_LANES 可强制。
def _adaptive_lane_workers(n_lanes: int) -> int:
    """按当前系统 load 决定蜂群同时跑几路 lane。返回 [1, n_lanes]。
    load<8 全并发(不影响质量);8-15 降到 2;>15 串行(1·排队跑·慢但不卡死机器)。
    MEYO_SWARM_MAX_LANES 环境变量可强制上限(调试/低配机用)。拿不到 load→保守全并发。"""
    try:
        forced = os.environ.get("MEYO_SWARM_MAX_LANES")
        if forced:
            return max(1, min(int(forced), n_lanes))
        load1 = os.getloadavg()[0]
        if load1 > 15:
            cap = 1
        elif load1 > 8:
            cap = 2
        else:
            cap = n_lanes
        return max(1, min(cap, n_lanes))
    except Exception:
        return n_lanes  # 拿不到 load 保守全并发(不误伤质量)


def _safe_llm(system: str, prompt: str, model: str, timeout: int) -> dict:
    if call_llm is None:
        return {"ok": False, "raw": "", "error": "llm_unavailable"}
    try:
        return call_llm(system, prompt, model_override=model, timeout=timeout)
    except Exception as exc:
        return {"ok": False, "raw": "", "error": f"{type(exc).__name__}:{exc}"}


def _report(task_id: str, step: int, pct: int, note: str, done: bool = False) -> None:
    try:
        from agent_comm import report
        report(task_id, step_idx=step, pct=pct, note=note, done=done)
    except Exception:
        pass


def _register(task_id: str, goal: str, max_hops: int = 3) -> None:
    try:
        from agent_comm import register_task
        # v56c · steps 按 hop 展开(每跳一个 step)+ 最终 judge 一个 step，
        # 让总线上能看出这次跑了几跳，而不是固定 5 个角色名。
        hop_steps = [f"hop{i}_scout_evidence_skeptic" for i in range(max_hops)]
        register_task(task_id, goal, eta_sec=120 * max_hops, steps=hop_steps + ["final_judge"])
    except Exception:
        pass


def _goal_card_directive(goal_card: dict | None) -> str:
    """把上游 step0.25 解析好的 goal_card 拼成一段「目标对齐」指引·空则返回空串。

    防御式：goal_card 为 None/空 dict 时返回 ""（行为与旧版完全一致）；
    每个字段用 .get 取，缺字段则跳过该字段，容忍任意缺失。
    """
    if not goal_card or not isinstance(goal_card, dict):
        return ""
    parts = []
    at = str(goal_card.get("answer_type", "") or "").strip()
    ke = str(goal_card.get("key_entities", "") or "").strip()
    sc = str(goal_card.get("success_criteria", "") or "").strip()
    sh = str(goal_card.get("search_hint", "") or "").strip()
    if at:
        parts.append(f"要的答案类型: {at}")
    if ke:
        parts.append(f"核心实体: {ke}")
    if sc:
        parts.append(f"判定对错: {sc}")
    if sh:
        parts.append(f"核心关键词: {sh}")
    if not parts:
        return ""
    return "\n【目标对齐(必须围绕这个拆子问题)】" + " · ".join(parts) + "\n"


def _is_simple_fact(goal_card: dict | None) -> bool:
    """v57 · 简单事实题判定(精确值 且 非多跳 且 非需推理)。

    profile 铁证(2026-08-06·Nature 2020发文数·323s): Planner opus 拆子问题 30s、
    Evidence deep_read_batch 231s(71%黑洞·爬8个重页)。对简单事实题(问一个数字/日期/
    人名)·opus 深拆 + 深爬8页 是过重投入。用 goal_card 判「简单事实」→ 走轻路径
    (haiku 拆 + 少爬)。goal_card 缺失/非精确值/多跳/需推理 一律走原重路径(fail-safe·
    不阉割复杂研究题的深度)。
    """
    if not goal_card or not isinstance(goal_card, dict):
        return False
    at = str(goal_card.get("answer_type", "") or "").strip()
    is_mh = str(goal_card.get("is_multihop", "") or "").strip().lower() in ("true", "1", "yes")
    is_rz = str(goal_card.get("is_reasoning", "") or "").strip().lower() in ("true", "1", "yes")
    # 序位/极值题需要看完整列表·不算简单事实(仍走深路径)
    has_ordinal = bool(str(goal_card.get("ordinal_constraint", "") or "").strip())
    is_precise = at.startswith("精确值") or at.startswith("精确")
    return is_precise and not is_mh and not is_rz and not has_ordinal


def _implicit_intent_constraints(goal: str) -> list[str]:
    """《情报搜寻优化·搜索设计》第7章 Intent-Aware Retrieval 落地(规则版·0 LLM)。

    Amazon Science 结论:约 20% 查询带隐式意图(未在查询文本里显式表达)。这里用
    离散规则从题面抽 browsecomp 高频隐式约束(时间窗/序位/极值/数值锚点/历史网页)，
    注入 Planner 指令·让拆解出的子查询显式携带这些约束·而不是拆完就丢。
    fail-safe:任何异常返回空列表(行为退化为无补全·与旧版一致)。
    """
    try:
        cons: list[str] = []
        years = sorted(set(re.findall(r"\b(?:19|20)\d{2}\b", goal or "")))
        if years:
            span = years[0] if len(years) == 1 else f"{years[0]}-{years[-1]}"
            cons.append(f"时间窗锚点 {span}：子查询应显式带年份限定；若目标网页年代久远，"
                        f"优先存档检索(wayback_cdx)而非常规搜索")
        if re.search(r"\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
                     r"last|final|\d+(?:st|nd|rd|th)\b|第[一二三四五六七八九十])", goal or "", re.I):
            cons.append("序位约束：必须拿到完整有序列表本身(而非片段)才能定位序位·子查询"
                        "应含 list|full|complete 等限定词")
        if re.search(r"\b(most|largest|oldest|newest|highest|longest|smallest|first ever)\b",
                     goal or "", re.I):
            cons.append("极值约束：需要可比较的全量候选集支撑·先枚举同类实体清单再逐条对约束筛")
        if re.search(r"\d+(?:\.\d+)?\s*(?:x|×|times|%)", goal or "", re.I):
            cons.append("数值锚点：先用已知量反算隐含值·再以隐含值作检索词验证(不得拿候选硬凑)")
        return cons
    except Exception:
        return []


def _plan(goal: str, llm: Callable[..., dict] = _safe_llm, goal_card: dict | None = None) -> dict:
    directive = _goal_card_directive(goal_card)
    # 第7章意图补全:把规则抽取的隐式意图约束拼进 Planner 指令·子查询显式携带约束。
    _intents = _implicit_intent_constraints(goal)
    if _intents:
        directive += "\n【隐式意图约束(拆子问题时必须显式携带)】\n- " + "\n- ".join(_intents) + "\n"
    # v57 · 简单事实题用 haiku 拆(opus 30s→haiku ~5s)·复杂/多跳题仍用 opus 深拆。
    #   profile: Planner opus 单次 30s·对「Nature 2020 发了几篇」这种一句话事实题过重。
    _model = "haiku" if _is_simple_fact(goal_card) else "opus"
    response = llm(PLANNER_SYSTEM, f"研究目标：{goal}{directive}\n规划 JSON：", _model, 75)
    parsed = try_parse_json(response.get("raw", ""), {"questions": []}) if response.get("ok") else {"questions": []}
    questions = parsed.get("questions") if isinstance(parsed, dict) else []
    clean = []
    for item in questions or []:
        if not isinstance(item, dict):
            continue
        claim = str(item.get("claim", "")).strip()
        query = str(item.get("query", claim or goal)).strip()
        if query:
            clean.append({"claim": claim or query, "query": query, "acceptance": str(item.get("acceptance", "至少一条可追溯来源"))})
    if not clean:
        clean = [{"claim": goal, "query": goal, "acceptance": "至少两条不同来源的可追溯证据"}]
    # v57 · 简单事实题限制子问题数(≤2)·避免 haiku 拆出 4 个子问题各自触发一轮
    #   scout/evidence/skeptic 检索(乘 8-URL deep_read)。复杂题保留 4 个的广度。
    _cap = 2 if _is_simple_fact(goal_card) else 4
    return {"questions": clean[:_cap], "planner_error": response.get("error", "") if not response.get("ok") else ""}


def _normalize_hit(hit: dict, lane: str) -> dict:
    return {
        "url": str(hit.get("url", "")),
        "title": str(hit.get("title", ""))[:240],
        "content": str(hit.get("content", ""))[:2500],
        "source": str(hit.get("source", "")),
        "lane": lane,
    }


def _search(query: str, mode: str, top_k: int, lane: str, search_factory: Callable[[], Any]) -> list[dict]:
    try:
        agent = search_factory()
        # v56e(2026-08-04) · 收口: 各 lane(scout/skeptic/evidence)都传 "balanced"→
        #   走 cdp_google 撞 perf_guard 卡分钟级。在此一处注入 http_only=True 覆盖全 lane
        #   (CDP google 质量 0.395 vs friday_bing 0.891·又慢又差)。深读 CDP 能力在
        #   下游独立保留。MEYO_SWARM_ALLOW_CDP=1 可放行(低负载想用 CDP 时)。
        allow_cdp = os.environ.get("MEYO_SWARM_ALLOW_CDP") == "1"
        try:
            hits = agent.search(query, mode=mode, top_k=top_k, http_only=not allow_cdp)
        except TypeError as exc:
            # 测试替身和旧 SearchAgent 协议可能尚未接入 http_only；仅对该兼容缺口回退，
            # 不吞掉后端真实错误，保证蜂群始终能消费标准三参 search 协议。
            if "http_only" not in str(exc) and "unexpected keyword" not in str(exc):
                raise
            hits = agent.search(query, mode=mode, top_k=top_k)
        normalized = [_normalize_hit(hit, lane) for hit in (hits or []) if isinstance(hit, dict)]
        # 即使正文稍后深读失败，已有 URL 仍是浏览器研究可消费的候选，不能被
        # 内容合约过滤成空列表；以此保留确定性 fallback，防止无界重搜。
        return normalized
    except Exception as exc:
        return [{"url": "", "title": "", "content": "", "source": "", "lane": lane, "error": f"{type(exc).__name__}:{exc}"}]


def _scout(plan: dict, search_factory: Callable[[], Any]) -> dict:
    hits: list[dict] = []
    for item in plan["questions"]:
        hits.extend(_search(item["query"], "balanced", 5, "scout", search_factory))
    return {"role": "scout", "hits": hits}


def _evidence(plan: dict, search_factory: Callable[[], Any], simple_fact: bool = False) -> dict:
    hits: list[dict] = []
    for item in plan["questions"]:
        hits.extend(_search(f"{item['query']} 官方 文档 原始来源", "deep", 4, "evidence", search_factory))
    # v57 · deep_read_batch 是蜂群最大黑洞(profile: Evidence 231s/71%·爬 8 个重页各 5-10s)。
    #   简单事实题(问一个数字/日期/人名): fanout 返回的 snippet(~2500字)通常已含答案·
    #   深爬 8 个大页收益低。→ 简单题只深爬 top 3 URL·并发拉满(max_workers=len·不再限 3);
    #   复杂题保留 8 URL 深爬(深度不阉割)。fail-safe: 判不出简单题(simple_fact=False)走原行为。
    _url_cap = 3 if simple_fact else 8
    urls = [h["url"] for h in hits if h.get("url")][:_url_cap]
    if urls:
        try:
            agent = search_factory()
            # 原 max_workers=min(3,len)·把 8 URL 串成 3 并发批次(2-3 轮=慢主因之一)。
            #   放宽到 min(6,len)·同批并发爬完·deep_read_batch 内部有超时保护。
            read_results = agent.deep_read_batch(urls, max_workers=min(6, len(urls)))
            by_url = {str(x.get("url", "")): x for x in read_results if isinstance(x, dict)}
            for hit in hits:
                read = by_url.get(hit.get("url", ""))
                if read and read.get("ok") and read.get("content"):
                    hit["content"] = str(read["content"])[:6000]
                    hit["deep_read"] = True
        except Exception as exc:
            hits.append({"url": "", "title": "", "content": "", "source": "", "lane": "evidence", "error": f"deep_read:{type(exc).__name__}:{exc}"})
    return {"role": "evidence", "hits": hits}


def _skeptic(plan: dict, search_factory: Callable[[], Any]) -> dict:
    hits: list[dict] = []
    for item in plan["questions"]:
        query = f"{item['query']} 缺点 限制 反例 争议 版本变化"
        hits.extend(_search(query, "balanced", 4, "skeptic", search_factory))
    return {"role": "skeptic", "hits": hits}


def _run_one_hop(plan: dict, search_factory: Callable[[], Any], task_id: str,
                  hop_idx: int, base_pct: int, skip_support: bool = False,
                  simple_fact: bool = False) -> dict:
    """跑一整跳的 Scout/Evidence/Skeptic 三路并发·返 {scout, evidence, skeptic}。

    hop_idx=0 是初始跳（用 Planner 的 questions）；hop_idx>=1 是 HopJudge 生成的
    追问跳（用 next_queries 转成的 questions）。两者共用同一套 worker，
    唯一区别是 plan 里 query 的来源不同。

    v56g · skip_support=True(第0跳有 step2 种子证据时): 跳过 scout+evidence
    (本质是 step2 已做的多角度找证据·重复)·只跑 skeptic(找反例·step2没做的互补价值)。
    直接砍掉蜂群最大的重复检索开销。
    """
    # v57 · evidence 绑定 simple_fact(控制 deep_read 的 URL 数)·scout/skeptic 无此参数。
    import functools as _ft
    _evidence_bound = _ft.partial(_evidence, simple_fact=simple_fact)
    if skip_support:
        workers = {"skeptic": _skeptic}
    else:
        workers = {"scout": _scout, "evidence": _evidence_bound, "skeptic": _skeptic}
    results: dict[str, dict] = {}
    # U1 · load 自适应:高 load 时少并发/串行(治卡)·低 load 全并发(不牺牲质量)
    _lane_cap = _adaptive_lane_workers(len(workers))
    if _lane_cap < len(workers):
        _report(task_id, hop_idx, base_pct,
                 f"hop{hop_idx} 系统负载高·蜂群降并发到 {_lane_cap} 路(防卡)")
    with concurrent.futures.ThreadPoolExecutor(max_workers=_lane_cap, thread_name_prefix=f"deep-research-hop{hop_idx}") as pool:
        futures = {pool.submit(worker, plan, search_factory): name for name, worker in workers.items()}
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as exc:
                results[name] = {"role": name, "hits": [], "error": f"{type(exc).__name__}:{exc}"}
            step_pct = base_pct + {"scout": 3, "evidence": 6, "skeptic": 9}[name]
            _report(task_id, hop_idx, step_pct,
                     f"hop{hop_idx}/{name} 完成：{len(results[name].get('hits', []))} 条候选")
    return results


def _hop_judge(goal: str, hop_idx: int, all_support: list[dict], all_counter: list[dict],
               llm: Callable[..., dict] = _safe_llm, goal_card: dict | None = None) -> dict:
    """跳间判决：证据够不够？不够就依据具体线索生成下一跳查询（真 multi-hop 的核心）。

    goal_card 非空时把 success_criteria 塞进 payload·让「够不够」的判断对齐
    「什么算答对」而非机械看证据条数。空则行为与旧版一致。
    """
    support_brief = [{"url": h.get("url", ""), "title": h.get("title", ""),
                       "content": str(h.get("content", ""))[:800]} for h in all_support[:14]]
    counter_brief = [{"url": h.get("url", ""), "title": h.get("title", ""),
                       "content": str(h.get("content", ""))[:500]} for h in all_counter[:8]]
    payload_obj: dict[str, Any] = {
        "goal": goal,
        "hop_index": hop_idx,
        "supporting_evidence": support_brief,
        "counter_evidence": counter_brief,
    }
    if goal_card and isinstance(goal_card, dict):
        _sc = str(goal_card.get("success_criteria", "") or "").strip()
        if _sc:
            payload_obj["success_criteria"] = _sc
    payload = json.dumps(payload_obj, ensure_ascii=False)
    response = llm(HOP_JUDGE_SYSTEM, f"当前材料（第 {hop_idx} 跳后）：\n{payload[:22000]}\n\nHopJudge JSON：",
                    "haiku", 45)
    fallback = {"sufficient": True, "gap_summary": "HopJudge 调用失败·保守收敛", "next_queries": []}
    parsed = try_parse_json(response.get("raw", ""), fallback) if response.get("ok") else fallback
    if not isinstance(parsed, dict):
        parsed = fallback
    parsed.setdefault("sufficient", True)
    parsed.setdefault("gap_summary", "")
    raw_queries = parsed.get("next_queries") or []
    clean_queries = []
    if isinstance(raw_queries, list):
        for q in raw_queries:
            if isinstance(q, dict) and str(q.get("query", "")).strip():
                clean_queries.append({"query": str(q["query"]).strip(), "gap": str(q.get("gap", "")).strip()})
            elif isinstance(q, str) and q.strip():
                clean_queries.append({"query": q.strip(), "gap": ""})
    parsed["next_queries"] = clean_queries[:3]
    if not parsed["next_queries"]:
        parsed["sufficient"] = True
    parsed["hop_judge_error"] = response.get("error", "") if not response.get("ok") else ""
    return parsed


def _queries_to_plan(next_queries: list[dict]) -> dict:
    """把 HopJudge/情报枢轴生成的查询转成搜索车道可消费的 plan 格式。"""
    questions = [{"claim": q["query"], "query": q["query"],
                  "acceptance": f"补缺口: {q.get('gap') or q.get('purpose', '')}" or "至少一条可追溯来源",
                  "relation": q.get("relation", "follow_up"), "node": q.get("node", "")}
                 for q in next_queries]
    return {"questions": questions}


def _investigation_nodes(hits: list[dict], limit: int = 10) -> list[dict]:
    """从已验证的网页命中抽取可追溯关系节点，绝不以模型记忆虚构身份/站点。

    节点是下一跳的硬约束：域名能追归档与迁移，署名能追身份与其他站点，标题/引号
    短语能追转载引用。这样即使原帖不存在，也能沿网页关系恢复其上下文。
    """
    nodes: list[dict] = []
    seen: set[tuple[str, str]] = set()
    author_patterns = (
        r"(?:by|author|written by|posted by|editor)\s*[:\-]?\s*([A-Z][A-Za-z'-]*(?:\s+[A-Z][A-Za-z'-]*){0,4})",
        r"(?:作者|撰文|编辑)\s*[:：]?\s*([^\n，,。]{2,30})",
        r"@([A-Za-z][A-Za-z0-9_]{2,30})",
    )
    url_pattern = re.compile(r"https?://[^\s<>\]\[\]\)\(\"']+", re.I)
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        url, title = str(hit.get("url", "")), str(hit.get("title", "")).strip()
        domain = urlparse(url).netloc.lower().removeprefix("www.") if url else ""
        candidates: list[tuple[str, str]] = []
        if domain:
            candidates.append(("domain", domain))
        if title and len(title) >= 8:
            candidates.append(("title", title[:140]))
        text = (title + "\n" + str(hit.get("content", ""))[:2500])
        # 账号个人页通常比显示名更稳定。将其作为节点而非直接 same_as：后续查询
        # 必须寻找自述/互链来确认，防止把同名作者或搬运号错误合并。
        for linked_url in url_pattern.findall(text):
            linked_url = linked_url.rstrip(".,;:!?")
            parsed_link = urlparse(linked_url)
            profile_match = re.search(
                r"(?:blogger\.com/profile/|github\.com/|bsky\.app/profile/|mastodon[^/]*/@)([^/?#]+)",
                linked_url, re.I)
            if profile_match:
                candidates.append(("profile_id", f"{parsed_link.netloc.lower()}:{profile_match.group(1)}"))
            elif parsed_link.netloc and parsed_link.netloc.lower().removeprefix("www.") != domain:
                candidates.append(("linked_site", parsed_link.netloc.lower().removeprefix("www.")))
        for pattern in author_patterns:
            for value in re.findall(pattern, text):
                value = " ".join(str(value).split()).strip(" -:|")
                if 3 <= len(value) <= 60 and not value.lower().startswith(("the ", "this ")):
                    candidates.append(("author", value))
        for kind, value in candidates:
            key = (kind, value.lower())
            if key not in seen:
                seen.add(key)
                nodes.append({"kind": kind, "value": value, "url": url})
                if len(nodes) >= limit:
                    return nodes
    return nodes


def _intel_pivots(goal: str, hits: list[dict], llm: Callable[..., dict] = _safe_llm) -> list[dict]:
    """把实体/站点/作者关系图转成下一跳侦查任务；失败时走确定性域名归档兜底。"""
    nodes = _investigation_nodes(hits)
    if not nodes:
        return []
    payload = json.dumps({"goal": goal, "nodes": nodes}, ensure_ascii=False)
    response = llm(INTEL_PIVOT_SYSTEM, f"目标与可追溯节点：\n{payload[:12000]}\n\nJSON：", "haiku", 45)
    parsed = try_parse_json(response.get("raw", ""), {"pivots": []}) if response.get("ok") else {"pivots": []}
    pivots: list[dict] = []
    allowed_nodes = {n["value"].lower() for n in nodes}
    for pivot in (parsed.get("pivots", []) if isinstance(parsed, dict) else []):
        if not isinstance(pivot, dict):
            continue
        query, node = str(pivot.get("query", "")).strip(), str(pivot.get("node", "")).strip()
        if query and node.lower() in allowed_nodes:
            pivots.append({"query": query, "node": node, "relation": str(pivot.get("relation", "citation")),
                           "purpose": str(pivot.get("purpose", ""))})
    # LLM 不可用时仍能沿站点关系前进，避免失败即停止。
    if not pivots:
        for node in nodes:
            if node["kind"] == "domain":
                pivots.append({"query": f"site:{node['value']} (archive OR sitemap OR RSS OR Atom OR author)",
                               "node": node["value"], "relation": "site_archive",
                               "purpose": "定位站点归档、迁移与作者入口"})
            elif node["kind"] == "profile_id":
                pivots.append({"query": f'"{node["value"]}" (author OR profile OR blog)', "node": node["value"],
                               "relation": "identity", "purpose": "以稳定账号 ID 查找自述和跨站互链"})
            elif node["kind"] == "author":
                pivots.append({"query": f'"{node["value"]}" (blog OR author OR website)', "node": node["value"],
                               "relation": "identity", "purpose": "定位署名人的其他公开站点；仅作为候选，需自述互链确认"})
            elif node["kind"] == "linked_site":
                pivots.append({"query": f"site:{node['value']} (author OR about OR archive OR RSS)", "node": node["value"],
                               "relation": "neighborhood", "purpose": "检查友情链接/聚合站是否提供作者自述或原帖引用"})
            if len(pivots) >= 4:
                break
    return pivots[:4]


def _contract_check_hits(hits: list[dict], goal: str = "") -> tuple[list[dict], int]:
    """v13.90(2026-08-10 调研落地·08-09 阶段A①)每路产出【即时轻量合约校验】。

    调研诊断:现状证据汇总到 _hop_judge 才被检验一致性·噪声一路带到最后放大成本。
    改:每路 Scout/Evidence 产出后·先跑离散判据当场滤掉明显噪声·别带入汇总:
      ① 内容非空且长度 > 阈值(纯标题无正文的当噪声)
      ② 有来源(url)——无来源的孤立断言不可考据
      ③ (可选)与 goal 关键词沾边(全不沾的多半是跑偏结果·如搜"numbers"搜出无关页)
    纯离散规则·0 LLM 调用·0 额外成本。返回 (过滤后 hits, 被滤掉数)。fail-safe:
    异常一律返回原 hits(不因校验自身故障丢证据)。
    """
    try:
        # goal 关键词(去停用词的长词)·用于"沾边"判据
        import re as _re
        _kw = set(w.lower() for w in _re.findall(r"[A-Za-z0-9一-鿿]{3,}", goal or ""))
        kept, dropped = [], 0
        for h in hits:
            content = str(h.get("content", "") or h.get("snippet", "") or "").strip()
            url = str(h.get("url", "") or "").strip()
            title = str(h.get("title", "") or "").strip()
            # ① 内容太短(纯骨架/空) + ② 无来源 → 噪声
            if len(content) < 40 and not url:
                dropped += 1
                continue
            # ③ 与 goal 完全不沾边(有 goal 关键词时才判·避免误杀)
            if _kw and (title or content):
                blob = (title + " " + content[:400]).lower()
                if not any(k in blob for k in _kw):
                    dropped += 1
                    continue
            kept.append(h)
        # 全被滤光则保守退回原 hits(宁可留噪声也别把这一跳清零)
        return (kept, dropped) if kept else (hits, 0)
    except Exception:
        return hits, 0


IDENTITY_RELATION_STATES = ("confirmed", "probable", "candidate", "rejected")


def _identity_relation_state(hit: dict) -> str:
    """以可审计信号标注身份关系，避免显示名/文风相似被升级为同一人。"""
    meta = hit.get("meta") if isinstance(hit.get("meta"), dict) else {}
    state = str(hit.get("identity_state") or meta.get("identity_state") or "").lower()
    return state if state in IDENTITY_RELATION_STATES else "candidate"


def _build_identity_relations(hits: list[dict]) -> list[dict]:
    """将身份线索显式写成四态图关系；只保留有来源的可追溯边。"""
    relations, seen = [], set()
    for hit in hits:
        if not isinstance(hit, dict) or not hit.get("url"):
            continue
        for node in _investigation_nodes([hit], limit=8):
            if node["kind"] not in {"author", "profile_id", "linked_site"}:
                continue
            key = (node["kind"], node["value"].lower(), hit["url"])
            if key in seen:
                continue
            seen.add(key)
            state = _identity_relation_state(hit)
            relations.append({
                "subject": node["value"], "predicate": "identity_clue", "object": hit["url"],
                "state": state, "evidence_urls": [hit["url"]],
                "reason": "稳定账号/署名/外链仅是候选；需自述互链或独立锚点才能确认",
            })
    return relations


# ═══ 《情报搜寻优化·搜索设计》第5章 Link Rot 与历史数据恢复 ═══
# Pew 统计 2013 年网页 38% 十年后不可访问；Ahrefs 口径 66.5% 链接已死。历史网页题
# (2010s 博客/已下线站点)常规搜索引擎是系统性盲区·唯一可靠入口是存档网。
# 落地:① CDX 直查(免 quota 公开 API)拿域名历史快照清单；② 快照 URL 用带完整
# 时间戳直链(web.archive.org/web/<14位ts>/<原URL>·不用年份中转·慢)；③ 轻量抓
# 快照正文前缀·让 HopJudge/浏览器循环能直接消费；④ URL 解构回溯(逐层向上找迁移)。
_CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"


def _wayback_cdx_lookup(url_prefix: str, date_range: str = "", limit: int = 6,
                        timeout: int = 8) -> list[dict]:
    """查 Wayback CDX 拿历史快照清单。date_range 如 '2011-2014' 或 '2013'。

    返回 [{timestamp, original_url, status}]·只留 200。任何异常返回 [](fail-safe)。
    """
    try:
        import urllib.parse as _up
        import urllib.request as _ur
        params = {"url": url_prefix, "output": "json", "collapse": "urlkey",
                  "limit": str(max(1, limit)), "fl": "timestamp,original,statuscode"}
        years = [int(y) for y in re.findall(r"(?:19|20)\d{2}", date_range or "")]
        if years:
            params["from"] = f"{min(years)}0101"
            params["to"] = f"{max(years)}1231"
        req = _ur.Request(f"{_CDX_ENDPOINT}?{_up.urlencode(params)}",
                          headers={"User-Agent": "meyo-deep-research/1.0 (linkrot-recovery)"})
        with _ur.urlopen(req, timeout=timeout) as resp:
            rows = json.loads(resp.read().decode("utf-8", errors="ignore") or "[]")
        out: list[dict] = []
        for row in (rows[1:] if isinstance(rows, list) and len(rows) > 1 else []):
            if not isinstance(row, list) or len(row) < 3:
                continue
            ts, original, status = str(row[0]), str(row[1]), str(row[2])
            if status == "200" and original.startswith("http"):
                out.append({"timestamp": ts, "original_url": original, "status": status})
        return out[:limit]
    except Exception:
        return []


def _fetch_snapshot_brief(snapshot_url: str, timeout: int = 8, max_bytes: int = 196608,
                          max_chars: int = 2200) -> str:
    """轻量抓存档快照正文前缀(strip HTML 后取前 max_chars)。失败返空串。

    v165 · 第4章反爬: 同域粘性身份(域名哈希确定性选身份·进程内稳定)·
    避免固定 UA 被 web.archive.org 限流。fail-safe。
    """
    try:
        import urllib.request as _ur
        if "/web/" in snapshot_url and "id_" not in snapshot_url:
            parts = snapshot_url.split("/web/")
            snapshot_url = f"{parts[0]}/web/{parts[1]}id_/".replace("//id_", "/id_")
        _dom = urlparse(snapshot_url).netloc.lower()
        _uas = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) CriOS/126.0.6478.114 Mobile/15E148 Safari/604.1",
        )
        _ua = _uas[hash(_dom) % len(_uas)]
        req = _ur.Request(snapshot_url, headers={"User-Agent": _ua})
        with _ur.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(max_bytes).decode("utf-8", errors="ignore")
        text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>|<[^>]+>", " ", raw)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]
    except Exception:
        return ""


def _url_backtrack_candidates(url: str) -> list[str]:
    """第5章策略② URL 解构回溯：从死链逐层向上回溯目录·找内容迁移后的新位置。"""
    try:
        parsed = urlparse(url)
        if not parsed.scheme:
            return []
        base = f"{parsed.scheme}://{parsed.netloc}"
        segs = [s for s in parsed.path.split("/") if s]
        if not segs:
            return []
        out = [base]
        for i in range(1, len(segs)):
            out.append(base + "/" + "/".join(segs[:i]) + "/")
        return out[-2:][::-1]  # 最近的父目录优先·最多 2 层
    except Exception:
        return []


def _archive_recovery_hits(hits: list[dict], goal: str, max_domains: int = 3,
                           per_domain: int = 4, fetch_top: int = 2) -> list[dict]:
    """Link-Rot 恢复车道：对本跳证据稀少的域名跑 CDX 直查·把历史快照注入 evidence。

    候选域名排序:抓取失败/内容过短的弱域名优先(它们最可能是死链)·其次全部域名。
    年份窗从题面抽取。只对前 fetch_top 个快照抓正文前缀(控制延迟)。全部 fail-safe。
    """
    try:
        weak, strong, seen = [], [], set()
        for h in hits:
            if not isinstance(h, dict):
                continue
            url = str(h.get("url", "") or "")
            if not url:
                continue
            domain = urlparse(url).netloc.lower().removeprefix("www.")
            if not domain or domain in seen or "web.archive.org" in domain or "archive.today" in domain:
                continue
            seen.add(domain)
            content = str(h.get("content", "") or "")
            is_weak = bool(h.get("error")) or len(content) < 120
            (weak if is_weak else strong).append(domain)
        targets = (weak + strong)[:max_domains]
        if not targets:
            return []
        years = sorted(set(re.findall(r"\b(?:19|20)\d{2}\b", goal or "")))
        date_range = (f"{years[0]}-{years[-1]}" if len(years) > 1
                      else (years[0] if years else ""))
        lanes: list[dict] = []
        fetched = 0
        for domain in targets:
            snaps = _wayback_cdx_lookup(f"{domain}/*", date_range=date_range, limit=per_domain)
            for snap in snaps:
                snap_url = f"https://web.archive.org/web/{snap['timestamp']}/{snap['original_url']}"
                lane_hit = {
                    "url": snap_url, "title": f"[archive] {snap['original_url'][:180]}",
                    "content": "", "source": "wayback_cdx", "lane": "archive",
                    "archived_at": snap["timestamp"], "original_url": snap["original_url"],
                }
                if fetched < fetch_top:
                    brief = _fetch_snapshot_brief(snap_url)
                    if brief:
                        lane_hit["content"] = brief
                        fetched += 1
                lanes.append(lane_hit)
            if len(lanes) >= per_domain * 2:
                break
        return lanes
    except Exception:
        return []


# v164 · 数值锚点反算执行器(第3章 AQR「数值锚点先反算」+第5章存档检索的前置破冰)。
# Q501 实证(v163 0/1): agent 自述「未能在预算内锁定候选域名→无法进入 Wayback CDX
# 存档检索这条本应奏效的路径」——死结在没有锚点。而题面给了硬数值约束
# 「1986 年发行、17 曲的专辑」——MusicBrainz 有精确的曲目数字段(免费公开 API)，
# 一发查询就能拿到全部候选专辑清单→专辑名/歌名成为下一跳搜博客的检索锚点。
_MB_STOP_ANCHORS = frozenset((
    "which", "what", "where", "when", "specific", "includes", "included", "compiled",
    "posted", "post", "before", "after", "between", "that", "this", "with", "from",
    "about", "their", "there", "these", "those", "mentioned", "mentions", "introduced",
))


def _numeric_anchor_probe(goal: str, limit: int = 10, timeout: int = 8) -> list[dict]:
    """从题面抽「<年份>专辑且恰好<N>曲」模式·查 MusicBrainz 拿候选专辑清单。

    返回 hits(lane=numeric_anchor·url=release 页)。题面无该模式或 API 失败返 []。
    fail-safe: 任何异常返空·不影响主流程。
    """
    try:
        text = goal or ""
        m = (re.search(r"(?:released in|from|in)\s+((?:19|20)\d{2})[^\n]{0,50}?(\d{1,2})\s*(?:tracks?|songs?)\b", text, re.I)
             or re.search(r"(\d{1,2})\s*(?:tracks?|songs?)\b[^\n]{0,50}?((?:19|20)\d{2})", text, re.I))
        if not m:
            return []
        year, n_tracks = m.group(1), int(m.group(2))
        if not (1900 < int(year) <= 2030 and 1 <= n_tracks <= 40):
            return []
        import urllib.parse as _up
        import urllib.request as _ur
        query = _up.quote(f"date:{year} AND tracks:{n_tracks}")
        url = (f"https://musicbrainz.org/ws/2/release/?query={query}"
               f"&fmt=json&limit={limit}")
        req = _ur.Request(url, headers={"User-Agent": "meyo-deep-research/1.0 (numeric-anchor)"})
        with _ur.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore") or "{}")
        out = []
        for rel in (data.get("releases") or [])[:limit]:
            title = str(rel.get("title", ""))
            artist = ""
            acr = rel.get("artist-credit") or []
            if acr and isinstance(acr[0], dict):
                artist = str(acr[0].get("name", ""))
            if not title:
                continue
            out.append({
                "url": f"https://musicbrainz.org/release/{rel.get('id', '')}",
                "title": f"[numeric_anchor] {artist} - {title} ({year}, {n_tracks} tracks)",
                "content": (f"MusicBrainz: {artist} - {title} · {rel.get('date', '')} · "
                             f"{rel.get('track-count', n_tracks)} tracks · country={rel.get('country', '')}. "
                             f"候选专辑锚点:可用『专辑名/艺人名 + year-end list + blog』作下一跳检索"),
                "source": "musicbrainz", "lane": "numeric_anchor",
                "anchor": {"year": year, "tracks": n_tracks},
            })
        return out
    except Exception:
        return []


def _offtarget_ratio(hits: list[dict], goal: str) -> float:
    """本跳证据跑偏率:题面罕见长词在 hits(title+content前缀)中零命中的比例。

    v164 实证:Q501 每跳都有几十条证据但全部跑偏·归档车道的「证据<6」触发条件
    永不满足。补此判据:跑偏率≥0.7 且题面带老年份(≤2016)信号→同样触发恢复车道。
    """
    try:
        anchors = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9'-]{5,}", goal or "")
                   if w.lower() not in _MB_STOP_ANCHORS]
        if not anchors or not hits:
            return 0.0
        anchors_l = [a.lower() for a in anchors][:12]
        off = 0
        for h in hits:
            blob = (str(h.get("title", "")) + " " + str(h.get("content", ""))[:500]).lower()
            if not any(a in blob for a in anchors_l):
                off += 1
        return off / len(hits)
    except Exception:
        return 0.0


def _claim_evidence_coverage(plan: dict, conclusions: list[dict], hits: list[dict]) -> dict:
    """按 claim 统计支持、反证、独立来源与覆盖率，作为继续检索而非无限爬取的停止依据。"""
    all_urls = {str(h.get("url", "")).strip() for h in hits if isinstance(h, dict) and h.get("url")}
    coverage = {}
    for question in plan.get("questions", []):
        claim = str(question.get("claim") or question.get("query") or "").strip()
        if not claim:
            continue
        matched = next((c for c in conclusions if str(c.get("claim", "")).strip() == claim), {})
        urls = {str(u).strip() for u in matched.get("source_urls", []) if str(u).strip()}
        verdict = str(matched.get("verdict", "unresolved")).lower()
        # Judge 明示的 URL 是该 claim 的直接证据；同时保留它是否出现在本轮抓取集，
        # 用于区分“已核入材料”与“需回访复核”的引用，不能把可追溯结论误计为零覆盖。
        cited = len(urls)
        materialized = len(urls & all_urls)
        required = 2 if "2" in str(question.get("acceptance", "")) else 1
        coverage[claim] = {
            "claim": claim, "verdict": verdict, "cited_source_urls": sorted(urls),
            "cited_sources": cited, "materialized_sources": materialized, "required_sources": required,
            "coverage": min(1.0, cited / required),
            "covered": cited >= required and verdict in {"verified", "partial", "contradicted"},
        }
    return coverage


def _dedupe_hits(hits: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for hit in hits:
        key = hit.get("url") or f"{hit.get('title')}:{hit.get('content', '')[:120]}"
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(hit)
    return out


# ═══ 《情报搜寻优化·搜索设计》第8章 证据融合层(Rank-Score Fusion 轻量落地) ═══
# 多源结果去重(已有 _dedupe_hits)之上·补三件事:
#   ① 来源可信度评分(域名权威性:维基/官方/存档/edu/gov 高·内容农场低);
#   ② 交叉印证(标题关键长词在其他独立域名出现→加分;单一来源孤证→标记给 Judge);
#   ③ 数值冲突检测(不同来源对同一题面数值锚点给出矛盾主张→标记待 Skeptic 深挖)。
# 纯离散规则·0 LLM 调用。评分写入 hit['trust_score']/hit['trust_reasons']·排序后
# 供 _judge 优先取高可信证据·orphan/corroboration 统计进报告层。
_DOMAIN_TRUST_RULES = (
    (r"\.wikipedia\.org$|\.wikidata\.org$", 0.85, "维基系公开可考"),
    (r"web\.archive\.org$|archive\.(today|ph|is|fo)$", 0.80, "存档网快照(不可篡改)"),
    (r"\.gov$|\.edu$|\.edu\.[a-z]{2}$|\.ac\.[a-z]{2}$", 0.78, "政府/学术域名"),
    (r"\.org$", 0.68, "组织域名"),
    (r"^(docs?|support|developer|official|www?)\.", 0.65, "疑似官方子域"),
)


def _domain_trust(domain: str) -> tuple[float, str]:
    d = (domain or "").lower()
    for pattern, score, reason in _DOMAIN_TRUST_RULES:
        if re.search(pattern, d):
            return score, reason
    return 0.55, "普通来源"


def _fuse_evidence(hits: list[dict]) -> dict:
    """第8章 Rank-Score Fusion：可信度评分 + 交叉印证 + 数值冲突检测。

    返回 {hits_ranked, source_diversity, orphan_urls, corroborated_n, numeric_conflicts}。
    fail-safe:任何异常返回未评分原序(不影响主流程)。
    """
    try:
        scored = [h for h in hits if isinstance(h, dict)]
        domains = [urlparse(str(h.get("url", "") or "")).netloc.lower().removeprefix("www.")
                   for h in scored]
        domain_set = {d for d in domains if d}
        # 标题关键长词索引(交叉印证用):词 → 出现过的独立域名集
        key_index: dict[str, set[str]] = {}
        for h, d in zip(scored, domains):
            if not d:
                continue
            words = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9'-]{4,}",
                                                   str(h.get("title", "")) or "")}
            for w in words:
                key_index.setdefault(w, set()).add(d)
        orphan_urls, corroborated_n = [], 0
        for h, d in zip(scored, domains):
            trust, reason = _domain_trust(d)
            reasons = [f"domain:{reason}={trust:.2f}"]
            # 交叉印证:该 hit 标题关键长词被 ≥2 个其他独立域名承载
            words = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9'-]{4,}",
                                                   str(h.get("title", "")) or "")}
            cross = max((len(key_index.get(w, set()) - {d}) for w in words), default=0)
            if cross >= 1:
                corroborated_n += 1
                reasons.append(f"cross_source:{cross}个独立域名印证")
                trust = min(1.0, trust + 0.10 * min(cross, 2))
            else:
                reasons.append("single_source:孤证待核")
                trust = max(0.2, trust - 0.05)
            if h.get("deep_read"):
                trust = min(1.0, trust + 0.05)
                reasons.append("deep_read:+0.05")
            h["trust_score"] = round(trust, 3)
            h["trust_reasons"] = reasons
            if cross == 0 and h.get("url"):
                orphan_urls.append(h["url"])
        hits_ranked = sorted(scored, key=lambda x: -float(x.get("trust_score", 0)))
        return {
            "hits_ranked": hits_ranked,
            "source_diversity": len(domain_set),
            "orphan_urls": orphan_urls,
            "corroborated_n": corroborated_n,
            "numeric_conflicts": _detect_numeric_conflicts(scored),
        }
    except Exception:
        return {"hits_ranked": hits, "source_diversity": 0, "orphan_urls": [],
                "corroborated_n": 0, "numeric_conflicts": []}


def _detect_numeric_conflicts(hits: list[dict]) -> list[dict]:
    """数值冲突检测(轻量):同一关键实体词附近的年份数值在不同域名出现不同主张。

    只检测年份(最高频 browsecomp 锚点)·其他数值噪声大不值得离散规则硬判。返回
    [{entity, values:{year:domains}, urls}]·空列表=无冲突。交给 Skeptic/Judge 深挖。
    """
    try:
        claims: dict[str, dict[int, set[str]]] = {}
        url_of: dict[str, list[str]] = {}
        for h in hits:
            if not isinstance(h, dict) or not h.get("url"):
                continue
            domain = urlparse(str(h["url"])).netloc.lower().removeprefix("www.")
            if not domain:
                continue
            blob = f"{h.get('title','')} {str(h.get('content',''))[:600]}"
            entities = {w.lower() for w in re.findall(r"\b[A-Z][a-z]{4,}\b", str(h.get("title", "")) or "")}
            years = {int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", blob)}
            for ent in entities:
                slot = claims.setdefault(ent, {})
                for y in years:
                    slot.setdefault(y, set()).add(domain)
                    url_of.setdefault(f"{ent}|{y}", []).append(str(h["url"]))
        conflicts = []
        for ent, year_map in claims.items():
            if len(year_map) < 2:
                continue
            # 同一实体两个年份各被 ≥2 个独立域名主张·且两主张域名集互斥 → 认为冲突
            strong_years = {y: ds for y, ds in year_map.items() if len(ds) >= 2}
            if len(strong_years) >= 2:
                values = {str(y): sorted(ds) for y, ds in list(strong_years.items())[:3]}
                conflicts.append({
                    "entity": ent,
                    "values": values,
                    "urls": url_of.get(f"{ent}|{list(strong_years)[0]}", [])[:3],
                    "note": "不同独立域名对同一实体给出不同年份主张·需 Skeptic 深挖",
                })
        return conflicts[:4]
    except Exception:
        return []


def _build_evidence_chain(goal: str, hops_log: list[dict], conclusions: list[dict],
                          coverage: dict, fused: dict) -> dict:
    """《搜索设计》第8章报告生成层：线索链 + 溯源标注 + 缺失标注。

    结构化输出「结论怎么来的」:goal → 每跳查询(含缺口/关系枢轴) → claim 判决 →
    证据 URL(带 trust) ·以及「还有什么没验证」:coverage<1 或 unresolved 的 claims。
    """
    try:
        trace = [{
            "hop": r.get("hop_index"),
            "queries": r.get("queries", [])[:4],
            "new_hits": r.get("new_hits", 0),
            "stop_reason": r.get("stop_reason", ""),
            "gap": (r.get("hop_judge") or {}).get("gap_summary", ""),
            "archive_recovery_n": r.get("archive_recovery", 0),
        } for r in hops_log]
        claims_out = []
        for claim, cov in coverage.items():
            claims_out.append({
                "claim": claim,
                "verdict": cov.get("verdict", "unresolved"),
                "coverage": cov.get("coverage", 0),
                "sources": cov.get("cited_source_urls", [])[:4],
                "covered": cov.get("covered", False),
            })
        source_index = [{
            "url": h.get("url", ""), "domain": urlparse(str(h.get("url", ""))).netloc,
            "trust": h.get("trust_score"), "lane": h.get("lane", ""),
        } for h in (fused.get("hits_ranked") or [])[:12] if h.get("url")]
        missing = [c["claim"] for c in claims_out if not c["covered"]]
        return {
            "goal": goal,
            "trace": trace,
            "claims": claims_out,
            "source_index": source_index,
            "source_diversity": fused.get("source_diversity", 0),
            "orphan_urls": (fused.get("orphan_urls") or [])[:6],
            "numeric_conflicts": fused.get("numeric_conflicts", []),
            "missing_annotations": missing,
            "unresolved_questions": [c for c in conclusions if isinstance(c, dict)
                                     and c.get("verdict") == "unresolved"][:4],
        }
    except Exception:
        return {"goal": goal, "trace": [], "claims": [], "source_index": [],
                "missing_annotations": []}


def _judge(goal: str, plan: dict, scout: dict, evidence: dict, skeptic: dict,
           llm: Callable[..., dict] = _safe_llm, goal_card: dict | None = None,
           fusion_note: str = "") -> dict:
    support = _dedupe_hits(scout.get("hits", []) + evidence.get("hits", []))[:12]
    counter = _dedupe_hits(skeptic.get("hits", []))[:8]
    payload = {
        "goal": goal,
        "questions": plan.get("questions", []),
        "supporting_evidence": support,
        "counter_evidence": counter,
    }
    # 智能压缩 payload：按 lane 优先级分块截断，避免硬截断丢失关键证据
    support_json = json.dumps(support, ensure_ascii=False)
    counter_json = json.dumps(counter, ensure_ascii=False)
    questions_json = json.dumps(plan.get("questions", []), ensure_ascii=False)
    # 支持证据优先（最多 18k），反证次之（最多 8k），子问题不截
    if len(support_json) > 18000:
        support = support[:8]  # 保留前 8 条完整
        for h in support[8:]:
            if h.get("url"):
                support.append({"url": h["url"], "title": h.get("title", "")[:120], "content": "(截断)"})
        support_json = json.dumps(support, ensure_ascii=False)
    if len(counter_json) > 8000:
        counter = counter[:4]
        counter_json = json.dumps(counter, ensure_ascii=False)
    # goal_card 非空时把 success_criteria / answer_type 前置进材料·让最终收口对齐目标。
    goal_line = ""
    if goal_card and isinstance(goal_card, dict):
        _sc = str(goal_card.get("success_criteria", "") or "").strip()
        _at = str(goal_card.get("answer_type", "") or "").strip()
        _gp = []
        if _at:
            _gp.append(f"要的答案类型: {_at}")
        if _sc:
            _gp.append(f"判定对错: {_sc}")
        if _gp:
            goal_line = "目标对齐(结论必须满足这个): " + " · ".join(_gp) + "\n"
    payload_str = f"目标: {goal}\n{goal_line}子问题: {questions_json}\n支持证据: {support_json}\n反证: {counter_json}"
    # 第8章证据融合提示:孤证降权/冲突待核/交叉印证数·让 Judge 按可信度取舍。
    if fusion_note:
        payload_str += f"\n证据可信度提示(来自 Rank-Score Fusion): {fusion_note}"
    response = llm(JUDGE_SYSTEM, f"研究材料：\n{payload_str[:30000]}\n\nJudge JSON：", "opus", 90)
    fallback = {
        "conclusions": [{
            "claim": item["claim"],
            "verdict": "partial" if support else "unresolved",
            "reason": "未获得可验证的 Judge 结论；保留候选证据供后续浏览器研究。",
            "source_urls": [h["url"] for h in support[:3] if h.get("url")],
        } for item in plan.get("questions", [])],
        "unresolved_questions": [item["claim"] for item in plan.get("questions", [])],
    }
    parsed = try_parse_json(response.get("raw", ""), fallback) if response.get("ok") else fallback
    if not isinstance(parsed, dict):
        parsed = fallback
    parsed.setdefault("conclusions", fallback["conclusions"])
    parsed.setdefault("unresolved_questions", fallback["unresolved_questions"])
    parsed["role"] = "judge"
    parsed["judge_error"] = response.get("error", "") if not response.get("ok") else ""
    return parsed


def run_five_agent_research(goal: str, search_factory: Callable[[], Any] | None = None,
                            llm: Callable[..., dict] = _safe_llm,
                            max_hops: int = 3,
                            seed_evidence: list | None = None,
                            goal_card: dict | None = None) -> dict:
    """执行五子 Agent 研究 · v56c 真 multi-hop，返回可供浏览器深研继续消费的结构化状态。

    与旧版单跳并行的区别：
      旧版 = Planner 拆问题 → Scout/Evidence/Skeptic 并行搜一次 → Judge 收口。
      新版 = 上述流程作为「第 0 跳」，跳完先过 HopJudge 判决「够不够、还缺什么」；
             不够则 HopJudge 依据*本跳已发现的具体线索*生成下一跳查询，再开一轮
             Scout/Evidence/Skeptic（第 1 跳、第 2 跳……），直到：
               a) HopJudge 判定 sufficient=true，或
               b) 达到 max_hops（默认 3），或
               c) 连续两跳新增有效证据数为 0（收敛判定，防止空转/瞎编查询）。
      最终 Judge 基于*全部跳*累积的证据给出总结论，返回结构里带 hops 明细
      （每跳的 query 来源、新增证据数、HopJudge 判决理由），可直接验证多跳真实发生。
    """
    if not goal or not goal.strip():
        raise ValueError("goal is required")
    if search_factory is None:
        from search_agent import SearchAgent
        # v57 · 默认关 supervisor。蜂群自带 HopJudge/Judge 做质量把关·搜索层再发一次
        #   LLM 监督是重复。注: 蜂群所有 lane 走 http_only=True→search() 强制 mode=fast·
        #   fast 本就不触发 supervisor(见 search_agent.py:1207 仅 balanced/deep)·故此改在
        #   http_only 路径下是「明确意图+防御」(若未来某 lane 改走 balanced/deep 也不会
        #   意外多发监督)。真正省时来自下面 simple_fact 轻路径。fail-safe·不改深度。
        search_factory = lambda: SearchAgent(use_cache=True, use_forger=True, use_supervisor=False)
    max_hops = max(1, int(max_hops))
    # v57 · 简单事实题(精确值·非多跳·非推理)→ Planner haiku + Evidence 少爬。
    #   判不出/复杂题 → 走原重路径(opus 深拆 + 8-URL 深爬)·不阉割深度。
    _simple_fact = _is_simple_fact(goal_card)

    session_id = f"swarm_{int(time.time())}"
    task_id = f"deep_swarm_{session_id}"
    _register(task_id, goal, max_hops=max_hops)
    started = time.time()
    _report(task_id, 0, 5, "Planner 正在拆解可验证子问题")
    plan = _plan(goal, llm, goal_card=goal_card)
    _report(task_id, 0, 10, f"Planner 完成：{len(plan['questions'])} 个子问题")

    hops_log: list[dict] = []
    all_scout_hits: list[dict] = []
    all_evidence_hits: list[dict] = []
    all_skeptic_hits: list[dict] = []
    last_results: dict[str, dict] = {}
    current_plan = plan
    prev_total_hits = 0
    stall_count = 0
    # 第5章 Link-Rot 恢复车道:每会话至多触发一次·MEYO_SWARM_ARCHIVE_LANE=0 可关。
    _archive_lane_on = os.environ.get("MEYO_SWARM_ARCHIVE_LANE", "1") == "1"
    _archive_lane_used = False

    # v56g(2026-08-05) · P2 优化: 复用 step2 已搜的 evidence 作「第0跳」种子·
    #   治「蜂群对同 question 重搜 scout/evidence/skeptic·和 step2 大量重叠」(266s大头)。
    #   种子进 all_evidence_hits·HopJudge 基于已有证据判够不够·多数题第0跳就 sufficient·
    #   省掉重复检索跳。不破坏多跳能力(不够仍会继续跳)。
    if seed_evidence:
        _seeded = [_normalize_hit(h, "seed_step2") for h in seed_evidence
                   if isinstance(h, dict) and (h.get("content") or h.get("url"))]
        all_evidence_hits.extend(_seeded)
        _report(task_id, 0, 8, f"复用 step2 证据 {len(_seeded)} 条作种子·减少重复检索")

    for hop_idx in range(max_hops):
        base_pct = 10 + hop_idx * (70 // max_hops)
        _report(task_id, hop_idx, base_pct,
                 f"第 {hop_idx} 跳：Scout/Evidence/Skeptic 并行检索 "
                 f"{len(current_plan.get('questions', []))} 个查询")
        # v56g · 第0跳且有 step2 种子证据 → 跳 scout/evidence(重复)·只跑 skeptic(互补)
        _skip_support = (hop_idx == 0 and bool(seed_evidence))
        hop_results = _run_one_hop(current_plan, search_factory, task_id, hop_idx,
                                   base_pct, skip_support=_skip_support,
                                   simple_fact=_simple_fact)
        last_results = hop_results

        hop_scout = hop_results.get("scout", {}).get("hits", [])
        hop_evidence = hop_results.get("evidence", {}).get("hits", [])
        hop_skeptic = hop_results.get("skeptic", {}).get("hits", [])
        # v13.90(2026-08-10 调研落地)每路产出即时合约校验·当场滤噪声再汇总·
        #   别让空/无来源/跑偏的 hit 一路带到 _hop_judge 放大成本(治撞墙题)。
        hop_scout, _d1 = _contract_check_hits(hop_scout, goal)
        hop_evidence, _d2 = _contract_check_hits(hop_evidence, goal)
        if (_d1 + _d2) > 0:
            _report(task_id, hop_idx, base_pct,
                    f"合约校验滤除 {_d1 + _d2} 条噪声证据(scout {_d1}/evidence {_d2})")
        # 《搜索设计》第5章 Link-Rot 恢复车道:常规检索证据稀少(有效命中<6)时·
        # 对弱域名(抓取失败/内容过短·最可能是死链)直接查 Wayback CDX 拿历史快照。
        # 2010s 博客/已下线站点是常规搜索的系统性盲区·存档网是唯一可靠入口。
        # v164 补强触发:Q501 实证「证据几十条但全部跑偏」——证据<6 永不满足·
        # 补判据「跑偏率≥0.7 且题面带老年份(≤2016)信号」同样触发。
        _archive_n = 0
        _old_web_signal = bool(re.search(r"\b(?:19[89]\d|200\d|201[0-6])\b", goal))
        _offtarget = _offtarget_ratio(hop_scout + hop_evidence, goal)
        if (_archive_lane_on and not _archive_lane_used
                and ((len(hop_scout) + len(hop_evidence)) < 6
                     or (_old_web_signal and _offtarget >= 0.7))):
            _archive_lane_used = True
            _archive_hits = _archive_recovery_hits(hop_scout + hop_evidence, goal)
            _archive_n = len(_archive_hits)
            if _archive_hits:
                hop_evidence.extend(_archive_hits)
                _report(task_id, hop_idx, base_pct,
                        f"Link-Rot 恢复:CDX 直查注入 {_archive_n} 条历史快照进 evidence(跑偏率{_offtarget:.0%})")
        # v164 数值锚点反算探针(第0跳一次性):题面「<年份>专辑且恰好N曲」→MusicBrainz
        # 拿候选专辑清单作下一跳检索锚点·治「无域名锚点→进不了存档检索」的死结。
        _anchor_n = 0
        if _archive_lane_on and hop_idx == 0:
            _anchor_hits = _numeric_anchor_probe(goal)
            _anchor_n = len(_anchor_hits)
            if _anchor_hits:
                hop_evidence.extend(_anchor_hits)
                _report(task_id, hop_idx, base_pct,
                        f"数值锚点反算:MusicBrainz 注入 {_anchor_n} 条候选专辑锚点")
        all_scout_hits.extend(hop_scout)
        all_evidence_hits.extend(hop_evidence)
        all_skeptic_hits.extend(hop_skeptic)

        support_so_far = _dedupe_hits(all_scout_hits + all_evidence_hits)
        counter_so_far = _dedupe_hits(all_skeptic_hits)
        total_hits = len(support_so_far) + len(counter_so_far)
        new_hits = total_hits - prev_total_hits

        is_last_allowed_hop = hop_idx == max_hops - 1
        hop_record = {
            "hop_index": hop_idx,
            "queries": [q["query"] for q in current_plan.get("questions", [])],
            "new_hits": new_hits,
            "total_hits_after": total_hits,
        }
        if _archive_n:
            hop_record["archive_recovery"] = _archive_n
        if _anchor_n:
            hop_record["numeric_anchor"] = _anchor_n

        if is_last_allowed_hop:
            hop_record["stop_reason"] = "max_hops_reached"
            hops_log.append(hop_record)
            _report(task_id, hop_idx, base_pct + 10,
                     f"第 {hop_idx} 跳完成·已达跳数上限 {max_hops}·新增 {new_hits} 条证据")
            break

        if new_hits <= 0:
            stall_count += 1
        else:
            stall_count = 0
        if stall_count >= 2:
            hop_record["stop_reason"] = "converged_no_new_evidence"
            hops_log.append(hop_record)
            _report(task_id, hop_idx, base_pct + 10,
                     f"第 {hop_idx} 跳完成·连续 2 跳零新增证据·收敛停止")
            break

        _report(task_id, hop_idx, base_pct + 5, f"第 {hop_idx} 跳：HopJudge 判决是否需要继续深挖")
        hop_judge = _hop_judge(goal, hop_idx, support_so_far, counter_so_far, llm, goal_card=goal_card)
        # 只在证据不足、或题面明确要求恢复网页/博客/存档时启动关系侦查。
        # 普通事实题尊重 HopJudge 的“充分”结论，保持单跳兼容；失效网页题才允许
        # 即使普通判决过早收敛，也沿真实 URL/署名/标题继续追 identity/site_archive 等关系。
        _web_recovery_goal = bool(re.search(
            r"\b(blog|post|website|site|archive|webpage|article|author)\b|博客|原帖|网页|站点|存档|作者",
            goal, flags=re.I))
        intel_pivots = (_intel_pivots(goal, support_so_far + counter_so_far, llm)
                        if _web_recovery_goal else [])
        existing_queries = {str(q.get("query", "")).strip().lower() for q in hop_judge.get("next_queries", [])}
        next_queries = list(hop_judge.get("next_queries", []))
        # 关系枢轴是网页恢复专用补强，普通多跳查询必须完全保留 HopJudge 的原语义。
        if _web_recovery_goal:
            for pivot in intel_pivots:
                if pivot["query"].lower() not in existing_queries:
                    next_queries.append(pivot)
                    existing_queries.add(pivot["query"].lower())
            next_queries = next_queries[:4]
        hop_record["hop_judge"] = {
            "sufficient": hop_judge.get("sufficient"),
            "gap_summary": hop_judge.get("gap_summary", ""),
            "next_queries": hop_judge.get("next_queries", []),
            "intel_pivots": intel_pivots,
        }
        hops_log.append(hop_record)

        # 仅网页恢复题可由关系节点推翻过早收敛；其余题严格遵从 HopJudge，避免无谓多跳。
        if (hop_judge.get("sufficient") and (not _web_recovery_goal or not intel_pivots)) or not next_queries:
            hop_record["stop_reason"] = "sufficient"
            _report(task_id, hop_idx, base_pct + 10,
                    f"第 {hop_idx} 跳完成·证据已足够且无可扩展关系：{hop_judge.get('gap_summary','')}")
            break

        _report(task_id, hop_idx, base_pct + 10,
                f"第 {hop_idx} 跳完成·缺口「{hop_judge.get('gap_summary','')}」"
                f"→ 下一跳追问 {len(next_queries)} 条（关系枢轴 {len(intel_pivots)}）")
        prev_total_hits = total_hits
        current_plan = _queries_to_plan(next_queries)


    judge_pct = 10 + len(hops_log) * (70 // max_hops) + 5
    _report(task_id, len(hops_log), min(judge_pct, 92), "Judge 正在交叉全部跳次的支持证据与反证")
    # 《搜索设计》第8章证据融合(Rank-Score Fusion)先行:全量去重→来源可信度评分→
    # 交叉印证→数值冲突检测。高可信证据排前进 Judge 视野(top12 按信任分优先)·
    # 孤证/冲突统计拼进 Judge 材料提示·让收口按可信度取舍而非按到达顺序。
    all_hits = _dedupe_hits(all_scout_hits + all_evidence_hits + all_skeptic_hits)
    fused = _fuse_evidence(all_hits)
    _rank = {str(h.get("url", "")): i for i, h in enumerate(fused.get("hits_ranked") or [])}
    all_evidence_hits.sort(key=lambda h: _rank.get(str(h.get("url", "")), 10**6))
    all_scout_hits.sort(key=lambda h: _rank.get(str(h.get("url", "")), 10**6))
    _fn_parts = []
    if fused.get("corroborated_n"):
        _fn_parts.append(f"{fused['corroborated_n']} 条已获独立域名交叉印证(优先采信)")
    if fused.get("orphan_urls"):
        _fn_parts.append(f"{len(fused['orphan_urls'])} 条为单一来源孤证(引用时降权·标注待核)")
    if fused.get("numeric_conflicts"):
        _fn_parts.append(f"{len(fused['numeric_conflicts'])} 处数值主张冲突(见 conflicts·结论需消解)")
    fusion_note = "; ".join(_fn_parts)
    final_scout = {"role": "scout", "hits": all_scout_hits}
    final_evidence = {"role": "evidence", "hits": all_evidence_hits}
    final_skeptic = {"role": "skeptic", "hits": all_skeptic_hits}
    judge = _judge(goal, plan, final_scout, final_evidence, final_skeptic, llm,
                   goal_card=goal_card, fusion_note=fusion_note)
    _report(task_id, len(hops_log), 100,
             f"Judge 完成：{len(judge.get('conclusions', []))} 条结论 · 共 {len(hops_log)} 跳", done=True)

    # 最终候选同时采集三路，并在异常/过滤路径下回退到 Judge 明示的来源；
    # candidate_urls 是“可继续验证的 URL 集”，不是“必须已抓到正文”的集合。
    all_hits = _dedupe_hits(all_scout_hits + all_evidence_hits + all_skeptic_hits)
    candidate_urls = list(dict.fromkeys(
        str(hit.get("url", "")).strip() for hit in all_hits
        if isinstance(hit, dict) and str(hit.get("url", "")).strip()
    ))
    if not candidate_urls:
        candidate_urls = list(dict.fromkeys(
            str(url).strip()
            for conclusion in judge.get("conclusions", []) if isinstance(conclusion, dict)
            for url in conclusion.get("source_urls", []) if str(url).strip()
        ))
    # 三级兜底:三路命中与 Judge 溯源都空(全 lane 搜索报错/命中 url 字段全空)时,
    # 别静默返回空 candidate_urls 让下游(deep_research_agent start_url)瞎跑,
    # 而是拿目标 + 试过的 query 直连再搜一轮拿 URL,只作"可继续验证的候选",不深读。
    candidate_urls_degraded = False
    if not candidate_urls:
        candidate_urls_degraded = True
        _fallback_queries = [goal] + [
            q for hop in hops_log
            for q in (hop.get("queries") or [])
            if isinstance(q, str) and q.strip()
        ]
        _seen_q: set[str] = set()
        for _q in _fallback_queries:
            _q = str(_q).strip()
            if not _q or _q in _seen_q:
                continue
            _seen_q.add(_q)
            try:
                _fb_hits = _search(_q, "balanced", 5, "fallback", search_factory)
            except Exception as _fb_exc:
                _report(task_id, len(hops_log), 100,
                        f"[DEGRADED] candidate_urls 兜底搜索异常:{type(_fb_exc).__name__}")
                continue
            for _h in _fb_hits:
                _u = str(_h.get("url", "")).strip() if isinstance(_h, dict) else ""
                if _u and _u not in candidate_urls:
                    candidate_urls.append(_u)
            if candidate_urls:
                break
        _report(task_id, len(hops_log), 100,
                (f"[DEGRADED] 三路+Judge 均无候选 URL·目标直搜兜底捞到 {len(candidate_urls)} 条"
                 if candidate_urls
                 else "[DEGRADED] candidate_urls 仍为空·下游需靠 goal 自行找入口(不断链·已告警)"))
    identity_relations = _build_identity_relations(all_hits)
    claim_coverage = _claim_evidence_coverage(plan, judge.get("conclusions", []), all_hits)
    # 第8章报告生成层:线索链(goal→每跳→claim→溯源) + 缺失标注(coverage<1/unresolved)。
    evidence_chain = _build_evidence_chain(goal, hops_log, judge.get("conclusions", []),
                                           claim_coverage, fused)
    return {
        "session_id": session_id,
        "task_id": task_id,
        "goal": goal,
        "started_at": datetime.now().isoformat(),
        "elapsed_s": round(time.time() - started, 2),
        "plan": plan,
        "hop_count": len(hops_log),  # 真实跑了几跳搜索（不含最终 Judge 收口那一步）
        "hops": hops_log,
        "agents": {"planner": plan, **last_results, "judge": judge},
        "conclusions": judge.get("conclusions", []),
        "unresolved_questions": judge.get("unresolved_questions", []),
        "candidate_urls": candidate_urls,
        "candidate_urls_degraded": candidate_urls_degraded,
        "identity_relations": identity_relations,
        "claim_evidence_coverage": claim_coverage,
        "fused_evidence_stats": {
            "source_diversity": fused.get("source_diversity", 0),
            "corroborated_n": fused.get("corroborated_n", 0),
            "orphan_n": len(fused.get("orphan_urls") or []),
            "numeric_conflicts": fused.get("numeric_conflicts", []),
        },
        "evidence_chain": evidence_chain,
        "evidence": all_hits,
    }
