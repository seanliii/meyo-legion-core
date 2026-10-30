#!/usr/bin/env python3
"""QA step/tool 实现库。

2026-08-27 架构定位:
- 用户/军团入口是 qa_auto_agent → NativeEngine；本文件不是入口。
- 旧 orchestrate 固定8-Step pipeline 与 CLI 已删除(军团不调用、run_pipeline真实0次)。
- 这里只保留 qa_tool_impls、cross_verify 等仍在复用的 step 实现；任务分析已拆到
  qa_task_analysis.py。后续按工具职责继续拆分，不再长成第二个编排器。
"""
import os
import sys
import json
import re
import time
import signal
import threading
import subprocess
import concurrent.futures
from datetime import datetime


class _WallDeadline(BaseException):
    """v70 · 统一 deadline 硬中断兜底信号。orchestrate 开头设 SIGALRM·到点(比总预算稍早)
    无论正在哪个环节·handler 抛此异常·被 orchestrate 主体 try/except 捕获→立即用已收集
    的全部证据强制抽答返回·绝不被外层 subprocess timeout 硬kill 返空。
    根治「分散检查点只管环节之间·管不住环节内部单次调用跑太久」的漏网。

    v70.1(实测修·关键)· 继承 BaseException 而非 Exception:
      实测 harlequin 题 step4_cards 单个 opus 调用跑 61s(174→235s)穿透 200s deadline·
      SIGALRM 确实在 200s 触发抛了本异常·但被 qa_llm_gateway.call() 里的
      `except Exception as exc`(以及全链路无数 except Exception)当普通网络错吞掉+重试·
      永远传不到 orchestrate 的 except。改继承 BaseException(同 KeyboardInterrupt/SystemExit)·
      让 `except Exception` 一律不拦·本控制流信号直穿到 orchestrate 的 except _WallDeadline。
      前提: 全链路无裸 `except:` / `except BaseException`(已 grep 核实 qa_agent_main + gateway 都没有)。"""
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
QA_ROOT = os.path.dirname(HERE)
# QA_ROOT 是 <repo>/seed-users-system/qa_system；旧代码向上两层后又重复拼
# seed-users-system，导致最新 v11 架构模块无法导入而被静默降级。
SEED_ROOT = os.path.dirname(QA_ROOT)
REPO_ROOT = os.path.dirname(SEED_ROOT)
V11_EVOLUTION = os.path.join(SEED_ROOT, "v11", "evolution")
if V11_EVOLUTION not in sys.path:
    sys.path.insert(0, V11_EVOLUTION)

from qa_llm_gateway import call as _gateway_call
from mission_state import MissionState

# QA 的生成统一走美团 Opus 网关；不依赖个人 Claude CLI 登录态。
ORCH_LOG = os.path.join(QA_ROOT, "state", "orchestrator_v31.jsonl")
os.makedirs(os.path.dirname(ORCH_LOG), exist_ok=True)


def _call_llm(model, prompt, timeout=120, system="", capture_first_byte=False):
    """统一网关调用。v56h · model 参数复活: "longcat-2.0" → 走 longcat-2.0(判决/预筛类·省CPU)·
    其余走 opus(写作)。之前 model 被吞·所有号称 longcat-2.0 的轻步骤全用 opus(CPU 89%根因)。"""
    # gateway 不提供 token 流事件，因此以首个完整响应耗时作为首字节近似值。
    _tier = "light" if str(model).lower() in ("longcat-2.0", "longcat-2.0", "light", "sonnet") else "heavy"
    return _gateway_call(prompt, timeout=timeout, system=system, tier=_tier)


def _parse_json(text):
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {}


def _url_is_alive(url, timeout=8):
    """v56d · 用户 2026-08-04 抓「nocode 链接跳 SSO 登录页/失效，你根本没校验就塞给我」
    根因: step7_nocode_produce 的历史 fallback 只看「历史记录里有没有 URL 字段」·
    从不管这个 URL 现在还能不能真访问到、内容对不对。
    修: 真发一次 HTTP 请求，2xx/3xx 才算活；网络异常/4xx/5xx/跳转到 SSO 登录页都算死。
    """
    if not url:
        return False
    try:
        import urllib.request
        req = urllib.request.Request(url, method="HEAD",
                                      headers={"User-Agent": "Mozilla/5.0 (qa_agent nocode url check)"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            final_url = resp.geturl()
            # SSO 统一登录页兜底跳转 = 页面本身失效/无权限访问，不算真活
            if "sso" in final_url.lower() and "login" in final_url.lower():
                return False
            return 200 <= status < 400
    except Exception:
        # HEAD 被拒时退化试 GET（部分 nocode 静态站点不支持 HEAD）
        try:
            import urllib.request
            req = urllib.request.Request(url, method="GET",
                                          headers={"User-Agent": "Mozilla/5.0 (qa_agent nocode url check)"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                final_url = resp.geturl()
                if "sso" in final_url.lower() and "login" in final_url.lower():
                    return False
                return 200 <= status < 400
        except Exception:
            return False


# ═══════════════════════ Step 0 · Ask/Answer scope 判决 ═══════════════════════

def step0_scope_judge(question):
    """判用户问的 scope · 输出 target_scope + 禁扩规则"""
    prompt = f"""判断用户问题的 scope · 用户在问什么·不是「怎么完美做完这件事」。

题: {question}

# 5 症状警惕 (市面 GSB 元规则)
- A · 意图曲解: 「如何设计问题」不是「完整用户研究项目」
- B · 场景过配: 6 人团队问题别给企业级方案·8 篇/周别给 10000/天
- D · 本地化: 中文语境·别默认海外工具

# 输出严格 JSON
{{"target_scope": "50 字·用户真正想解决的",
  "ban_expand_rules": ["禁扩规则 1", "禁扩规则 2", "禁扩规则 3"],
  "user_scale": "个人 / 小团队 / 中型 / 大企业·猜的",
  "expected_length": "答案期望字数·200-500 / 500-1500 / 1500-3000"}}

只输出 JSON:"""
    r = _call_llm("longcat-2.0", prompt, timeout=25)
    return _parse_json(r["out"]) or {"target_scope": question,
                                       "ban_expand_rules": [],
                                       "user_scale": "个人",
                                       "expected_length": "500-1500"}


def step0_triage(question):
    """v44 · 闸门 0 意图分诊 (用户 2026-07-24 抓「三闸门融合」)

    输出三元组:
      - task_level: 琐事 / 标准 / 重型
      - search_route: no_search / verify / heavy
      - scenario_gap: 0-2 (关键场景缺口数·>0 自动触发 qa_clarify)
      - task_intent_type: 独立字段·qa_action_cone 用 (methodology/tool_selection/etc)
      - direct_answer: 若 task_level=琐事 · longcat-2.0 直接给答案·跳所有闸门
    """
    prompt = f"""判用户问题的意图分诊·输出三闸门参数。

题: {question}

# 三元组判决

## 1. task_level (决定后续闸门流程)
- 琐事: 【严格】纯概念解释·答案稳定不变·且绝不涉及具体代码/命令/工具实现 (例: 什么是 JTBD)
  ⚠️ 只要题目要「具体可执行的代码/命令/配置/技术实现/正确产出」→ 绝不是琐事·必须搜索
- 标准: 需要框架/对比/方法论/技术方案 · 一轮搜索够 (例: 续费访谈怎么设计/工具选哪个/某报错怎么修)
- 重型: 系统设计/多场景/技术难题/需要立体验证 · 需要多轮搜索 (例: 从零搭调研体系/冷门工具用法/polyglot代码)

⚠️ 搜索优先原则: 拿不准就判「标准+heavy」走搜索·别图省事判琐事。
   技术类题(代码/命令/工具/报错/实现/解题) 默认至少「标准」·几乎不判琐事。

## 2. search_route (搜索路由)
- no_search: 完全不用搜 · 概念解释/稳定知识
- verify: 需 1-2 查证 · 但主体靠通识 · 举例数据核对
- heavy: 完整多 agent 搜索 · 需要立体证据

## 3. scenario_gap (场景缺口·0-2)
- 0: 场景已明 (题里带角色 · 规模 · 阶段等) 无需澄清
- 1: 缺 1 个关键维度 (只缺角色 · 或只缺规模)
- 2: 缺 2 个关键维度 (角色+场景 · 场景+阶段)
- 上限 2 · 更多用户嫌烦

## 4. task_intent_type (工具锥类型 · 独立)
- methodology: 方法论/怎么设计 → 工具=脚本/SOP/追问树
- tool_selection: 工具选型 → 工具=推荐 + 部署链接
- decision: 决策题 → 工具=checklist/decision tree
- concept: 概念解释 → 无工具
- process: 系统设计 → 工具=agent 架构

## 5. direct_answer (只在 task_level=琐事时给·否则 null)
- 200 字内直答·代替走完整 8 step 流程

## 5b. constraints (这题的【口径约束】·你语义抽取·算之前必核对·防口径错位)
   题目里有没有限定: 时间窗(pre-2020/近30天)、分母/总体定义(占谁的百分比·总体是啥)、
   过滤条件(剔除退款/按人去重/某岛屿除外)? 抽成清单。算数据题时·先核对你用的数据范围是否
   匹配这些约束·不匹配就重新过滤·别用默认全量直接算(实证: 企鹅占比分母用错344行、
   OpenReview时间窗pre-2020算成pre-2022·都是口径没对准就算)。没约束填[]。

## 5c. ambiguity_axes (这题的【语义歧义轴】·★逐轴强制判定·防题意读偏)
   实证: 一批错题不是不会做·是【题意读偏了】。开局就把下面每条轴显式想清楚·填一句话
   (与题目无关的轴填""·别硬凑)。这不是套词表·是逐轴问自己"这题在这条轴上到底要哪个":
   - entity: 【问题指的到底是哪个实体·唯一确定吗】? 哪个游戏/哪个人/哪个"2019"(颁奖年vs发行年)?
     ★这是最上游的歧义——实体选错·后面怎么算都白算·先锁对实体再谈计数。(实证GoW:
     "2019 game won BAFTA"·agent锁定God of War(2019颁奖/2018发行)算477·题意其实是"2019
     发行的游戏"=Outer Wilds·gold=60。别在错实体上精算。)
   - time: 时间/区间是【累计还是单期】? 哪个时点/版本? (实证GoW: 算成"发布前累计477次修订"·
     题意其实是"发布月那一个月=60次")
   - set: 集合是【交/并/补】? De Morgan方向对吗? (实证企鹅: "不住Dream岛 或 喙>42mm"的补集·
     别算成"不住Dream 且 喙≤42")
   - version: 数据源用【哪个版本/哪张表】? (实证火车: 读了周末表·但5/27是周一该读工作日表)
   - perspective: 【谁的max/谁的min】? maximin还是别的? (实证Bob: 算成maximin保底·题意是另一机制)
   - aggregate: 多个片段【如何排序/去重/合并】? (实证分数图: 多次读取捏合成错顺序)
   - nominal: 标注者可能用了【简化口径】吗? (实证Freon: gold按理想气体·精确算反而"错")
   求解/交卷前对照这些轴核对一遍·发现读偏立即改。

## 6. modality_needs (这题本质需要什么【模态能力】·你自己判断·不是套词表)
   想清楚: 要拿到答案·本质需要「看画面/听音频/精确数值计算/看图识别/查文字资料」哪一种?
   - vision: 需看图像/视频某帧的**画面内容**(搜文字搜不到画面里有什么)
   - audio: 需听音视频**说了什么**(转录)
   - compute: 需精确数值计算/建模(该写代码算·非估)
   - text: 查公开文字资料就够(默认)
   一题可多个。这不是让你套关键词·是理解「这题要拿到答案·靠搜文字够不够·还是必须换个模态的工具」。

# 严格 JSON
{{
  "task_level": "琐事|标准|重型",
  "search_route": "no_search|verify|heavy",
  "scenario_gap": 0-2,
  "task_intent_type": "methodology|tool_selection|decision|concept|process",
  "constraints": ["口径约束清单·如'时间窗=pre-2020','分母=总企鹅population','剔除Dream岛'"] 或 [],
  "ambiguity_axes": {{"entity": "", "time": "", "set": "", "version": "", "perspective": "", "aggregate": "", "nominal": ""}},
  "modality_needs": ["text"] 或 ["vision"] 或 ["audio","compute"] 等,
  "unknown_entities": ["题里你【不确定是什么】的专有名词/产品/术语·如'Cheater Beater','Freon-12','GameGrumps'"] 或 [],
  "problem_archetype": "若这题是某类【有标准解法】的经典问题·归类它(如'博弈论-阶梯Nim','组合数学-穷举','热力学-物性计算','几何-多边形面积')·不确定填''",
  "direct_answer": null 或 "200 字内直答",
  "reason": "30 字为啥这么判"
}}
# unknown_entities/problem_archetype 说明(通用·非背答案):
#   unknown_entities = 你不认识、需先查"它到底是什么"才能解的名词(不是让你猜它是什么·是标出该先查的)。
#   problem_archetype = 只做【归类】(这题像哪类经典问题)·不给具体答案。目的:让 agent 先查
#   "这类问题标准怎么解"再动手·而非硬猜。判据:换任何同类题都通用——是通用引导·不是背本题答案。
只输 JSON:"""
    r = _call_llm("longcat-2.0", prompt, timeout=25)
    d = _parse_json(r["out"]) or {}
    # 根因1(2026-08-12·治静默默认掩盖失败): _parse_json 失败(d为空)时·"LLM说没约束"
    # 和"LLM调用整个失败"在旧代码里是同一个信号(都走 or []/or {})。加 triage_status
    # 区分二者·并响亮告警(不崩·仍走默认兜底保 fail-safe)——让上层/自检能看到"这题
    # 的 triage 其实没成·axes/constraints 的空不是真空"。
    _llm_failed = not d
    if _llm_failed:
        try:
            print(f"[triage_status=llm_failed] step0_triage 的 LLM 调用/解析失败·"
                  f"axes/constraints 走默认空值(不是真的没约束)·out_head={str(r.get('out',''))[:80]!r}",
                  file=sys.stderr)
        except Exception:
            pass
    return {
        "task_level": d.get("task_level", "标准"),
        "search_route": d.get("search_route", "heavy"),
        "scenario_gap": min(2, max(0, int(d.get("scenario_gap", 0)))),
        "task_intent_type": d.get("task_intent_type", "methodology"),
        "constraints": d.get("constraints") or [],
        "ambiguity_axes": d.get("ambiguity_axes") or {},  # G1(2026-08-12)·漏透传就等于没接
        "modality_needs": d.get("modality_needs") or ["text"],
        "unknown_entities": d.get("unknown_entities") or [],  # 2026-08-14·实体消歧:先查陌生实体
        "problem_archetype": d.get("problem_archetype") or "",  # 2026-08-14·题型归类:先查标准解法
        "direct_answer": d.get("direct_answer"),
        "reason": d.get("reason", ""),
        "triage_status": "llm_failed" if _llm_failed else "ok",  # 根因1·响亮区分"无"vs"失败"
    }


# ═══════════════════════ Step 1 · Cost-first 判决 ═══════════════════════

def step1_cost_first(question, scope):
    """算清成本·直接建议工具·别铺陈候选"""
    prompt = f"""你是精算师·先算清成本再挑工具。

# 用户问题
{question}

# 已判 scope
{json.dumps(scope, ensure_ascii=False, indent=2)}

# 输出 (务实·不铺陈 5 个候选·直接建议)
{{"cost_analysis": {{
    "manual_hours": "手工做要几小时",
    "tool_hours": "用工具要几小时",
    "break_even": "什么条件下值得上工具"
  }},
  "recommend": "直接建议·1-2 个工具·不列 5 个候选",
  "recommend_reason": "为什么这个·别的为什么不",
  "cn_first_tools": ["国内工具 1", "国内工具 2"],
  "en_backup_tools": ["英文补充·仅在国内没有时列"]}}

只输出 JSON:"""
    r = _call_llm("longcat-2.0", prompt, timeout=30)
    return _parse_json(r["out"]) or {}


# ═══════════════════════ Step 2 · 4 并行搜索 ═══════════════════════

def _light_prescreen(question: str, hits: list, top_k: int = 10, timeout: int = 60) -> list:
    """v34 · 从 qa_agent.py 搬回·longcat-2.0 从 50→top10 预筛
    v15.7 · 治两处蠢: ①LLM 返回空/坏JSON 直接静默弃排序 → 重试1次+词法回退;
    ②判据写死业务口味(真人经验>官方) → 事实型题改为权威一手源优先
    """
    if len(hits) <= top_k:
        return hits
    _zh = _cjk_ratio(question) > 0.25
    _criteria = ("真实从业者经验、具体数据、一手实操过程 > 官方文档 > 营销内容"
                 if _zh else
                 "primary/authoritative sources (official records, databases, encyclopedias, "
                 "original documents) > reputable secondary coverage > forums/blogs")
    items_desc = []
    for i, h in enumerate(hits[:80]):
        title = (h.get("title") or "")[:120]
        snippet = (h.get("snippet") or h.get("content") or "")[:200]
        url = h.get("url", "")
        items_desc.append(f"[{i}] {title} · {snippet}\n    URL: {url}")
    prompt = (
        f"用户问题: {question[:300]}\n\n"
        f"以下 {len(items_desc)} 条搜索结果·挑出 top {top_k} 条最值得深读全文的。\n"
        f"判断标准: 与问题的约束匹配度 + {_criteria}\n\n"
        f"素材:\n" + "\n".join(items_desc) + "\n\n"
        f'严格输出 JSON: {{"top_indices": [i1, i2, ...]}} · {top_k} 个数字·按重要性降序。只输 JSON。'
    )
    for _attempt in (1, 2):  # v15.7 · 网关抖动返回空 → 重试1次
        try:
            raw = _call_llm("longcat-2.0", prompt, timeout=timeout)["out"]
            if "```" in raw:
                raw = raw.split("```json")[-1].split("```")[0] if "```json" in raw else raw.split("```")[1]
            d = json.loads(raw.strip())
            top_ids = d.get("top_indices", [])[:top_k]
            if not top_ids:
                return hits
            chosen = [hits[i] for i in top_ids if 0 <= i < len(hits)]
            rest = [h for i, h in enumerate(hits) if i not in set(top_ids)]
            return chosen + rest
        except Exception as e:
            if _attempt == 1:
                print(f"  [light_prescreen] 首次失败({str(e)[:60]})·重试", file=sys.stderr)
                time.sleep(1.0)
            else:
                # v15.7 · 词法回退: 按查询锚词在标题+摘要的命中数排序·不齐乱序透传
                anchors = [t.strip('"\'.') for t in _compress_query(question).split()
                           if len(t) > 2][:10]
                def _lex(h):
                    blob = ((h.get("title") or "") + " " +
                            (h.get("snippet") or h.get("content") or "")).lower()
                    return sum(1 for a in anchors if a.lower() in blob)
                print(f"  [light_prescreen] LLM 两跳均失败({str(e)[:60]})·词法回退排序 anchors={len(anchors)}",
                      file=sys.stderr)
                return sorted(hits, key=_lex, reverse=True)
    return hits


def _parallel_deep_read(hits_to_read: list, max_workers: int = 5) -> list:
    """v34 · 从 qa_agent.py 搬回·5 并发抓全文
    v13.94(2026-08-12) · 优先走 search_fabric_fetch L2 梯度(httpx→curl_cffi·轻量快)
    fallback 到 web_access_bridge (CDP 重型路径)
    """
    if not hits_to_read:
        return hits_to_read
    urls = [h.get("url") for h in hits_to_read if h.get("url")]
    if not urls:
        return hits_to_read
    print(f"  [并行深读] {len(urls)} URLs · {max_workers} 并发", file=sys.stderr)

    # v13.95 · 订阅墙域名先走 OA 绕路（不浪费 L2/browseract 轮次）
    by_url: dict = {}
    try:
        v11_evo = V11_EVOLUTION
        if v11_evo not in sys.path:
            sys.path.insert(0, v11_evo)
        from search_agent_orchestrator import _is_paywall_domain, _extract_doi_from_url, fetch_oa_by_doi
        import concurrent.futures as _cf

        paywall_urls = [u for u in urls if _is_paywall_domain(u)]
        if paywall_urls:
            print(f"  [并行深读] 订阅墙 URL {len(paywall_urls)} 条·走 OA 绕路", file=sys.stderr)

            def _fetch_oa(u):
                doi = _extract_doi_from_url(u)
                if not doi:
                    return u, None
                r = fetch_oa_by_doi(doi)
                return u, r

            with _cf.ThreadPoolExecutor(max_workers=min(len(paywall_urls), 3)) as _ex:
                for u, r in _ex.map(_fetch_oa, paywall_urls):
                    if r and r.get("ok") and r.get("content"):
                        by_url[u] = {"content": r["content"],
                                     "via": f"oa:{r.get('via','?')}", "ok": True}
            _oa_ok = sum(1 for u in paywall_urls if u in by_url)
            print(f"  [并行深读] OA 绕路 ok={_oa_ok}/{len(paywall_urls)}", file=sys.stderr)
    except Exception as _pwe:
        print(f"  [并行深读] OA 绕路 fail({_pwe})·继续走 L2", file=sys.stderr)

    # v13.94 · 优先 L2 梯度(httpx→curl_cffi·合规·快)
    # 订阅墙 URL 已在上面处理，这里只处理非订阅墙的
    urls_for_l2 = [u for u in urls if u not in by_url]
    try:
        from search_fabric_fetch import scrape_and_extract_batch
        # 每个 URL 附带搜索维度作条件抽取 query（用源 dim 名辅助相关性）
        _url_to_hit = {h.get("url"): h for h in hits_to_read}
        pairs = [(url, _url_to_hit.get(url, {}).get("source_dim", ""))
                 for url in urls_for_l2]
        if pairs:
            fab_results = scrape_and_extract_batch(pairs, max_chars=3000,
                                                   max_workers=max_workers, max_tier=2)
            for url, r in zip(urls_for_l2, fab_results or []):
                if isinstance(r, dict) and r.get("ok") and r.get("content"):
                    by_url[url] = {"content": r["content"], "via": r.get("via", "fabric"),
                                   "ok": True}
        _ok_n = len(by_url)
        print(f"  [并行深读] L2 ok={_ok_n}/{len(urls)}", file=sys.stderr)
    except Exception as _fe:
        print(f"  [并行深读] search_fabric_fetch fail({_fe})·退 web_access_bridge", file=sys.stderr)

    # 没拿到的用 web_access_bridge 补（CDP/browseract 重型路径）
    _missing = [u for u in urls if u not in by_url]
    if _missing:
        try:
            from web_access_bridge import WebAccess
            wa = WebAccess()
            wb_results = wa.read_urls(_missing, max_workers=max_workers)
            for r in wb_results or []:
                if isinstance(r, dict) and r.get("url") and r.get("ok") and r.get("content"):
                    by_url[r["url"]] = r
            print(f"  [并行深读] WebAccess 补 {len(_missing)} · ok={len(by_url)}/{len(urls)}",
                  file=sys.stderr)
        except Exception as _we:
            print(f"  [并行深读] WebAccess 补读 fail: {_we}", file=sys.stderr)

    # 回写 hits (v15.7 · top5 6000字符·其余3000: 答案常在长页深处·统一3000会切掉)
    for _rank, h in enumerate(hits_to_read):
        u = h.get("url")
        r = by_url.get(u)
        if r and r.get("ok") and r.get("content"):
            _cap = 6000 if _rank < 5 else 3000
            h["content"] = r["content"][:_cap]
            h["completeness"] = "full"
            h["deep_via"] = r.get("via", "?")
    return hits_to_read


# ── v15.6(2026-08-18 · 用户抓「搜索词这么差·准确率和扩展性这么差」) 查询构造重写 ──
# 病根实证: 旧实现把【整段原题】直接拼搜索词·还加硬编码业务后缀
#   ("通义 飞书 腾讯 金数据"/"english medium reforge hbr")——中文长题/英文事实题/
#   Benchmark 题全炸: 引擎把后缀当关键词·原题里的模板噪声("Your response should
#   be in the following format: Explanation: ...")也一起搜。
# 修法(通用·零硬编码产品名·不额外耗 LLM):
#   ①剥模板噪声(BrowseComp QUERY_TEMPLATE/Explanation/Confidence 尾巴)
#   ②中英自适应: 中文→内容字裁剪; 英文→保引号短语/大写专有名词/数字年份优先·
#     去停词·cap 12 tokens/90 chars
#   ③五车道后缀按语言自适应: 中文业务题保留实战/踩坑/工具语义·英文事实题用
#     百科/存档/深解扩展词(存档词还能带出 wayback_cdx 路由)
from qa_task_analysis import (_QUERY_BOILER_RE, _EN_QSTOP, _ZH_QSTOP)
# 常量唯一归属 qa_task_analysis(auto/旧pipeline共用·不重复)

# v15.7 · LLM 查询规划缓存(同题多次 run_parallel_search 不重复规划)
_PLAN_CACHE: dict = {}


def _structured_lanes(question: str) -> list[str]:
    """v15.8 · 从题面提取【结构化反查车道】: 干净最小查询·让 fabric 路由直连垂直源。

    病根(2026-08-18 · Q501 实证): 规划器把结构约束(年份+曲目数/域名+日期)与
    泛搜词混在同一条长查询里·路由 LLM 认不出该走垂直源(musicbrainz/
    blogspot_feed/wayback_cdx_date)·全部落回通用引擎——结构化反查全军覆没。
    这里额外产出**只含约束本身**的最小查询·路由一眼命中垂直源。

    通用模式(非题目硬编码):
      a) 年份+曲目数(…released in 1986 with 17 tracks…) → '{年} album {数} tracks'
      b) 域名+精确日期 → '{域名} {日期}'
      c) 艺人名+年份 问作品年表 → '{艺人} discography {年}'
    """
    q = str(question or "")
    lanes: list[str] = []
    # a) 年份 + 曲目数 → musicbrainz
    m = (re.search(r"released in\s+(\d{4})\D{0,30}?(\d{1,3})\s*(?:tracks|songs)\b", q)
         or re.search(r"\b(\d{1,3})\s*(?:tracks|songs)\b\D{0,40}?album\D{0,30}?\b(?:in|from|released)\s+(\d{4})\b", q))
    if m:
        g1, g2 = m.group(1), m.group(2)
        y, n = (g1, g2) if len(g1) == 4 else (g2, g1)
        lanes.append(f"{y} album {n} tracks")
    # b) 域名 + 精确日期 → blogspot_feed / wayback_cdx_date
    m_dom = re.search(r"\b((?:[a-z0-9-]+\.)+(?:com|net|org|blogspot\.[a-z.]{2,10}|wordpress\.com))\b", q, re.I)
    m_day = re.search(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?[A-Z][a-z]+,?\s+\d{4}|[A-Z][a-z]+\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})\b", q)
    if m_dom and m_day:
        lanes.append(f"{m_dom.group(1)} {m_day.group(1)}")
    # c) 艺人+年份年表问法 → musicbrainz discography
    m = re.search(r"\b([A-Z][A-Za-z.]+(?:\s+[A-Z][A-Za-z.]+){1,3})\b[^\n]{0,60}?\b(?:released|album|discography|first album)\b[^\n]{0,40}?\b(19\d{2}|20[0-2]\d)\b", q)
    if m:
        lanes.append(f"{m.group(1)} discography {m.group(2)}")
    return lanes[:2]


def _plan_queries(question: str, base: str, zh: bool, n_queries: int = 4) -> list:
    """v15.7 · 搜索专家架构: 约束分解→多条独立查询·取代固定五车道后缀污染。

    三档自适应:
      ① agent 已精炼的短查询(≤120字符·无模板尾): 它本身就是规划结果 → 轻量变体
         (原查询 + 锚实体子集)·不再二次规划(治"agent 好查询被后缀再污染")
      ② 长原题(含模板尾): longcat 一次分解成 n 条约束聚焦查询(缓存)·
         每条只担一个子约束(实体A+年份 / 实体B+关键短语)·不带任何口味后缀
      ③ LLM 失败: 启发式回退(锚实体分组组合)
    """
    q = str(question or "").strip()
    # ① 短查询·agent 已规划过
    if 0 < len(q) <= 120 and "Your response" not in q and "你的回答" not in q:
        toks = base.split()
        anchors = [t for t in toks if len(t) > 1 and
                   (t[0].isupper() or t[0].isdigit() or t.startswith('"'))]
        # A compact relational query is already one constraint intersection. Splitting its
        # numbers/proper nouns into independent lanes destroys that intersection and produces
        # unrelated high-recall results. Keep one query unless a genuine entity anchor exists.
        variants = [q]
        named_anchors = [t for t in anchors if not t[0].isdigit()]
        if len(named_anchors) >= 2:
            variants.append(" ".join(named_anchors[:6]))
            if len(named_anchors) >= 4:
                variants.append(" ".join(named_anchors[:2] + named_anchors[-2:]))
        if not variants:
            variants.append(base)
        # v15.8 · 结构化反查车道追加(题面含年份+曲目数/域名+日期时)
        for _sl in _structured_lanes(question):
            # 不能用子串判重：泛搜长句常包含 '1986 album 17 tracks'，
            # 却仍会让路由把它当普通网页搜索。结构化车道只按整句去重。
            if _sl.lower() not in {v.strip().lower() for v in variants}:
                variants.append(_sl)
        return variants[:n_queries + 2]
    # ② 长原题·LLM 分解(缓存)
    import hashlib
    _key = hashlib.md5(q.encode("utf-8")).hexdigest()
    if _key in _PLAN_CACHE:
        return _PLAN_CACHE[_key]
    _lang = "中文" if zh else "English"
    prompt = (
        f"你是搜索专家。把下面的问题分解成 {n_queries} 条【互相独立】的搜索查询({_lang})。\n\n"
        f"问题: {q[:1200]}\n\n"
        "分解规则:\n"
        "· 每条查询只聚焦一个子约束(一个实体+年份 / 一个专有名词+关键属性 / 一个引号短语)\n"
        "· 保留问题里的专名、引号短语、年份、数字·不要改写它们\n"
        "· 不加任何口味词(如 tutorial/explained/best)·不加多余后缀\n"
        "· 不同查询覆盖问题的不同约束·能各自独立命中答案页\n\n"
        f'严格输出 JSON: {{"queries": ["q1", "q2", ...]}} · 只输 JSON。'
    )
    try:
        raw = _call_llm("longcat-2.0", prompt, timeout=30)["out"]
        if "```" in raw:
            raw = raw.split("```json")[-1].split("```")[0] if "```json" in raw else raw.split("```")[1]
        d = json.loads(raw.strip())
        qs = [x.strip() for x in (d.get("queries") or []) if x and x.strip()]
        if qs:
            # v15.8 · 结构化反查车道追加(垂直源专用干净查询)
            for _sl in _structured_lanes(q):
                # 结构化最小查询必须独立存在，不能因被长查询包含而丢失。
                if _sl.lower() not in {v.strip().lower() for v in qs}:
                    qs.append(_sl)
            _PLAN_CACHE[_key] = qs[:n_queries + 2]
            return qs[:n_queries + 2]
    except Exception as e:
        print(f"  [plan_queries] LLM 失败({str(e)[:60]})·启发式回退", file=sys.stderr)
    # ③ 启发式回退: 锚实体分组组合
    toks = base.split()
    anchors = [t for t in toks if t[0].isupper() or t[0].isdigit() or t.startswith('"')]
    qs = []
    if anchors:
        qs.append(" ".join(anchors[:6]))
        half = max(2, len(anchors) // 2)
        qs.append(" ".join(anchors[:half]))
        qs.append(" ".join(anchors[half:]))
    qs.append(base)
    _PLAN_CACHE[_key] = qs[:n_queries]
    return qs[:n_queries]


def _cjk_ratio(s: str) -> float:
    if not s:
        return 0.0
    n = sum(1 for c in s if '\u4e00' <= c <= '\u9fff')
    return n / max(1, len(s))


def _compress_query(question, max_tokens=12, max_chars=90):
    """把任意长题(含模板尾巴)压成干净短查询。压缩不动就不伤短题(fail-safe)。"""
    q = _QUERY_BOILER_RE.sub("", str(question or "")).strip()
    if _cjk_ratio(q) > 0.15:  # 中文题: 去停字+标点切段取内容最长的两段拼
        segs = [s.strip() for s in re.split(r"[··,。；;！!？?\n\t]+", q) if s.strip()]
        keep = []
        for s in segs:
            t = "".join(c for c in s if c not in _ZH_QSTOP)
            if len(t) >= 4:
                keep.append(t)
        out = " ".join(keep)[:max_chars]
        return out or q[:max_chars]
    # 英文题的判别性排序(2026-08-24 跨框架 trace 根因):
    #   deepseek 命中冷门题靠「精确引号短语 anchor + 专有名词」;auto 旧序把通用关系词
    #   (distance/church/theater/northeast…)排最前,搜索引擎当成「电影院/城堡旅游」关键词
    #   返回 SEO 噪音(instagram/facebook),证据 0 命中 gold。修正判别性优先级:
    #   引号精确短语 > 专有名词(实体 anchor) > 数字+相邻单位 > 稀有长内容词 > 通用关系词(仅补位)。
    #   关系词本身在任何题里都高频、低判别,只有在更强的 anchor 不足时才作填充。
    quotes = re.findall(r'"([^"]{3,60})"', q)
    # 判别性数字:4 位年份、带单位的量、数字区间(600 and 1100)。孤立无单位单数字
    # (如从 "1 kilometer" 拆出的 "1")跨题高频、零判别,过滤掉。不硬编码具体数值。
    keep_numeric = set()
    for m_num in re.finditer(r"\b(\d{4})\b", q):            # 年份
        keep_numeric.add(m_num.group(1))
    for m_num in re.finditer(r"\b(\d+(?:\.\d+)?)\s*[-–]?\s*(?:and\s+)?(\d+(?:\.\d+)?)?\s*"
                             r"(?:m|km|meters?|metres?|kilometers?|kilometres?|miles?|ft|feet)\b",
                             q, re.I):                       # 带单位的量 / 区间
        keep_numeric.add(m_num.group(1))
        if m_num.group(2):
            keep_numeric.add(m_num.group(2))
    toks = re.findall(r"[A-Za-z][A-Za-z'\-]+|\d+(?:\.\d+)?", q)
    # 通用空间/关系词:跨题高频、低判别。不硬编码任何 benchmark 实体,只列语言层面的
    # 泛化方位/距离/设施类停用型内容词,命中即降权到补位。
    relation_terms = {
        "distance", "walking", "meters", "metres", "kilometer", "kilometre",
        "kilometers", "miles", "north", "south", "east", "west",
        "northeast", "northwest", "southeast", "southwest", "entrance",
        "nearby", "near", "further", "away", "within", "between", "sea",
        "theater", "theaters", "theatre", "theatres", "church", "castle",
    }
    seen, proper, numeric, content, relational = set(), [], [], [], []
    quoted = [f'"{t}"' for t in quotes[:2]]
    for t in toks:
        tl = t.lower()
        if tl in _EN_QSTOP or tl in seen:
            continue
        seen.add(tl)
        if t.replace(".", "").isdigit():
            if t in keep_numeric:                            # 只保留判别性数字
                numeric.append(t)
        elif t[0].isupper():
            proper.append(t)
        elif tl in relation_terms:
            relational.append(t)
        else:
            content.append(t)
    # 精确短语与实体 anchor 最判别;通用关系词只在前面凑不满 max_tokens 时补位。
    picked = (quoted + proper + numeric + content + relational)[:max_tokens]
    out = " ".join(picked)[:max_chars]
    return out or q[:max_chars]


def step2_parallel_search(question, scope, on_hits=None, total_timeout=None,
                          snippets_only=False):
    """v34 · 4 并发 SearchAgent 拉粗池 + longcat-2.0 预筛 + 5 并发深读全文
    用户 2026-07-24 抓「orchestrator 没 longcat-2.0 预筛也没 5 并发深读」
    v13.94(2026-08-12) · 接入 search_fabric L0 多源(ddgs+wikipedia+垂直API)
    SearchAgent 作 fallback 保留·不删
    """
    # v13.94 · 优先用 search_fabric(免费多源·无 quota 断链风险)
    _SearchImpl = None
    _use_fabric = False
    try:
        if V11_EVOLUTION not in sys.path:
            sys.path.insert(0, V11_EVOLUTION)
        from search_fabric_verticals import auto_register_all as _sreg
        _sreg()
        from search_fabric import SearchFabric as _SF
        _SearchImpl = _SF(fanout=3, fallback_to_anysearch=True)  # v15.7 · 2→3 源多样性
        _use_fabric = True
        print("  [step2] search_fabric L0 已就绪(多源·免 quota)", file=sys.stderr, flush=True)
    except Exception as _sfe:
        from search_agent import SearchAgent
        _SearchImpl = SearchAgent()
        print(f"  [step2] fabric 不可用·退 SearchAgent: {_sfe}", file=sys.stderr, flush=True)

    # v15.7 · 维度构造重写(搜索专家架构·治五车道后缀污染):
    #   中文业务题: 保留实战/踩坑/工具车道(日常 QA 主链·已验证有效)
    #   英文/事实题: _plan_queries 约束分解 → 4 条独立查询·零口味后缀;
    #     agent 精炼过的短查询直接透传+轻量变体·不再被二次污染
    _base = _compress_query(question)
    _zh = _cjk_ratio(str(question)) > 0.25
    # All languages use clean planned queries. The old Chinese branch appended
    # workflow-flavor terms ("实战/踩坑/工具/复盘") that changed the retrieval intent,
    # produced SEO/forum noise, and leaked into English-question follow-up queries.
    _planned = _plan_queries(question, _base, _zh, n_queries=4)
    # No domain-shaped source forcing: a hardcoded "YYYY album N tracks → musicbrainz"
    # regex was a benchmark-specific heuristic. Let the Fabric semantic router pick sources
    # for every query; the model drives retrieval, not a pattern match. (MEMORY rule:
    # improvements must use generic signals, never domain/benchmark clues.)
    dimensions = [
        {"name": f"plan_{i+1}", "query": qq}
        for i, qq in enumerate(_planned)
    ]
    dimensions.append({"name": "forum", "query": _base, "_forum": True})
    print(f"  [step2] v15.7 查询规划: {[d['query'][:60] for d in dimensions[:-1]]}",
          file=sys.stderr, flush=True)

    def _run(dim):
        # v34 · mode="balanced" 拉粗池 (deep 慢 · 后面自己深读)
        # v15.6 · 瞬时故障(网络抖/后台进程抢锁)重试1次+err透出(之前静默吞掉难排查)
        def _once():
            # v13.88.47 · forum 维度走 fetch_reddit_and_twitter (google site: 硬命中)
            if dim.get("_forum"):
                import sys as _sys
                if V11_EVOLUTION not in _sys.path:
                    _sys.path.insert(0, V11_EVOLUTION)
                from forum_search import fetch_reddit_and_twitter
                hits = fetch_reddit_and_twitter(dim["query"], top_k=3)
                return {"dim": dim["name"], "hits": hits or [], "ok": True}
            # v13.94 · 兼容 fabric/agent 双接口
            if _use_fabric:
                # MEYO_FORCE_TAVILY=1:整条 auto 搜索走共享 Tavily 缓存源(与六家公平
                # 基准同源·同 query 命中缓存零额度),便于缓存复用与公平对齐。
                _forced = dim.get("_force_sources")
                if not _forced and os.environ.get("MEYO_FORCE_TAVILY", "") == "1":
                    _forced = ["tavily"]
                hits = _SearchImpl.search(
                    dim["query"], top_k=15,
                    force_sources=_forced,
                ) or []
            else:
                hits = _SearchImpl.search(dim["query"], mode="balanced", top_k=15) or []
            return {"dim": dim["name"], "hits": hits, "ok": True}
        try:
            return _once()
        except Exception as e1:
            try:
                time.sleep(1.5)
                r = _once()
                print(f"  [Parallel Search] {dim['name']}: 重试成功 · 首次异常 {str(e1)[:60]}",
                      file=sys.stderr, flush=True)
                return r
            except Exception as e:
                print(f"  [Parallel Search] {dim['name']}: FAIL {str(e)[:100]}",
                      file=sys.stderr, flush=True)
                return {"dim": dim["name"], "hits": [], "ok": False, "err": str(e)[:100]}

    t0 = time.time()
    # One absolute deadline covers fan-out, prescreen and deep-read. In bounded/preflight mode
    # snippets are published immediately and expensive deep-read is deferred until a named
    # candidate exists; otherwise worker threads outlive the caller and consume the whole wall.
    _deadline = (time.monotonic() + float(total_timeout)) if total_timeout is not None else None
    results = []
    # v13.87 · 用户 07-23 抓「q01 12min 卡在 search」
    # v31.1 的 max_workers=2 timeout=600 太保守 · 4 维度串 2 波 · 每波 max 300s
    # 改: 4 并发一次撒 · 每维度 timeout 180s · 总 200s 硬门 (原 600s)
    _N_DIMS = len(dimensions)
    _WORKERS = min(_N_DIMS, 4)
    _TOTAL_TIMEOUT = (float(total_timeout) if total_timeout is not None else
                      float(os.environ.get("MEYO_PARALLEL_SEARCH_TOTAL_TIMEOUT", "220") or 220))
    # v70 · 不用 `with`(其 __exit__ 会 shutdown(wait=True) 阻塞等 worker 跑完·若此时
    #   SIGALRM 到点抛 _WallDeadline·会被这里的阻塞 shutdown 吃掉·deadline 失效)。改显式
    #   管理 executor: 捕获 _WallDeadline → 非阻塞 shutdown(cancel_futures) 后原样上抛·让
    #   orchestrate 的大 try 立即接住去强制抽答。
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=_WORKERS)
    try:
        futures = {ex.submit(_run, d): d for d in dimensions}
        try:
            for f in concurrent.futures.as_completed(futures,
                                                       timeout=_TOTAL_TIMEOUT):
                r = f.result()
                results.append(r)
                if on_hits and r.get("hits"):
                    try:
                        on_hits([h for h in r["hits"] if isinstance(h, dict)], r["dim"])
                    except Exception as _publish_exc:
                        print(f"  [Parallel Search] {r['dim']}: 增量发布失败 {str(_publish_exc)[:100]}",
                              file=sys.stderr, flush=True)
                print(f"  [Parallel Search] {r['dim']}: {len(r['hits'])} 条 · {r.get('ok', False)}",
                      file=sys.stderr, flush=True)
        except concurrent.futures.TimeoutError:
            # 超时·收现有结果·取消未完成的
            done_dims = {r["dim"] for r in results}
            missed = [d["name"] for d in dimensions if d["name"] not in done_dims]
            print(f"  [Parallel Search] 超时 {_TOTAL_TIMEOUT}s·完成 {len(results)}/{_N_DIMS}·跳 {missed}",
                  file=sys.stderr, flush=True)
            # 强制取消未完成的
            for f in futures:
                if not f.done():
                    f.cancel()
    except _WallDeadline:
        # v70 · deadline 到点·别阻塞等 worker·非阻塞 shutdown 后上抛
        print("  [Parallel Search] v70 wall_deadline 命中·非阻塞取消未完成搜索·上抛兜底",
              file=sys.stderr, flush=True)
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        raise
    finally:
        # 正常路径: 非阻塞 shutdown(worker 已在 as_completed/超时逻辑里跑完或取消)
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    # 合并去重
    seen = set()
    merged = []
    for r in results:
        for h in r["hits"]:
            if not isinstance(h, dict):
                continue
            url = h.get("url", "")
            if not url or url in seen:
                continue
            seen.add(url)
            h["source_dim"] = r["dim"]
            merged.append(h)

    print(f"  [Parallel Search] {_WORKERS} 并发·{time.time()-t0:.1f}s·"
          f"完成 {len(results)}/{_N_DIMS}·合并 {len(merged)} 条 (去重后)",
          file=sys.stderr, flush=True)

    # LIGHT mode (codex-style atomic search): return ranked snippets fast, DO NOT run the
    # LLM prescreen or the 12-URL full-text deep-read inside the search call. The model
    # decides whether to deep-read a specific URL (via fetch_url_smart) — that is the
    # model-driven, fast path. The heavy prescreen+deepread was the dominant latency
    # (46-69s/call vs codex's few seconds) and made auto ~2x slower than codex.
    _light = os.environ.get("MEYO_LIGHT_SEARCH", "") == "1"
    top_k = int(os.environ.get("MEYO_QA_DEEP_TOP", "12"))
    _bounded = total_timeout is not None or snippets_only
    if _light:
        return merged[:max(top_k, 15)]
    if not _bounded and len(merged) > top_k:
        print(f"  [v34] light 预筛 {len(merged)} → top{top_k}", file=sys.stderr, flush=True)
        merged = _light_prescreen(question, merged, top_k=top_k)
    top_hits = merged[:top_k]

    # Bounded/preflight retrieval publishes snippets and returns. Deep-read is deferred to the
    # candidate-specific verification step; it must not outlive the preflight future.
    # Cache-only means "do not touch the live web". Deep-read fetches full text from live
    # URLs, so in cache-only mode it both contradicts the intent and is the dominant latency
    # (≈47s/batch) — skip it and use whatever the cache already returned.
    _cache_only = os.environ.get("MEYO_TAVILY_CACHE_ONLY", "") == "1"
    need_deep = ([] if (_bounded or _cache_only) else
                 [h for h in top_hits if not h.get("content") or len(h.get("content", "")) < 300])
    if need_deep and (_deadline is None or time.monotonic() < _deadline):
        print(f"  [v34] 5 并发深读 {len(need_deep)} 条", file=sys.stderr, flush=True)
        _parallel_deep_read(need_deep, max_workers=5)

    return merged


# ═══════════════════════ Step 3 · 立场判决 (longcat-2.0 并行) ═══════════════════════

def step3_stance_judge(evidence):
    """每源判独立/有倾向/广告"""
    if not evidence:
        return {}
    top = evidence[:30]
    ev_list = "\n".join([
        f"- [{i+1}] URL={e['url']} 标题={e.get('title','?')[:80]}"
        for i, e in enumerate(top)
    ])
    prompt = f"""判每源利益立场:

{ev_list}

三档:
- independent · 独立第三方 (HBR/学术/大厂博客/知乎第三方/媒体) → 可放心
- interested · 有利益倾向 (工具自家博客/带货博主/CS 咨询公司) → 引用需标注
- ad · 纯广告 (商品页/App Store/单一工具软文) → 弃用

JSON:
{{"verdicts": [{{"idx": 1, "stance": "independent|interested|ad", "reason": "10 字"}}]}}
只输出 JSON:"""
    r = _call_llm("longcat-2.0", prompt, timeout=45)
    data = _parse_json(r["out"])
    verdicts = {v["idx"] - 1: v for v in data.get("verdicts", [])}
    for i, e in enumerate(top):
        if i in verdicts:
            e["stance"] = verdicts[i]["stance"]
            e["stance_reason"] = verdicts[i].get("reason", "")
    stats = {
        "total": len(verdicts),
        "independent": sum(1 for v in verdicts.values() if v["stance"] == "independent"),
        "interested": sum(1 for v in verdicts.values() if v["stance"] == "interested"),
        "ad": sum(1 for v in verdicts.values() if v["stance"] == "ad"),
    }
    return stats


# ═══════════════════════ Step 4 · 观点重组 (opus) ═══════════════════════

def step4_extract_cards(question, evidence, scope):
    """opus 逐源提三件套 · 剔搜索污染 · 弃 ad · 有倾向源必标注"""
    kept = [e for e in evidence if e.get("stance") != "ad"][:20]
    ev_str = "\n\n".join([
        f"[证据 {i+1}] {e['url']}\n标题: {e.get('title','?')[:100]}\n"
        f"立场: {e.get('stance','?')} · 摘录: {(e.get('excerpt', e.get('content','')) or '')[:800]}"
        for i, e in enumerate(kept)
    ])
    prompt = f"""你是内容筛选官·逐源提炼可执行动作。

# 用户问题
{question}

# scope 判决
{json.dumps(scope, ensure_ascii=False)}

# 素材 ({len(kept)} 条·已剔 ad)
{ev_str}

# 你的任务
每条源提成一份三件套卡片:
```
## 卡片 N
- 站名·作者·日期·URL
- 立场标注 (仅 interested 时加): 「该源自身也做同类产品·观点参考」
- 原文摘录: 30-80 字·真源的话
- 翻译成用户动作: 50-100 字·具体到「你打开 X·填 Y·得 Z」或「访谈时不要问 A·改问 B」
```

# 元规则
- **别扩 scope**·用户问的是 X 就答 X·不扩成完整项目
- **中文语境**·工具默认国内 (通义/飞书/腾讯/金数据)·英文源仅在国内没时补充
- **禁学术黑话**·JTBD/Van Westendorp/SimHash 出现必翻译成人话
- 每源必须有可执行动作·不能是"论文总结"
- 保留 8-15 条最有价值的卡片

只输出卡片列表·不要开场白:"""
    r = _call_llm("opus", prompt, timeout=300)
    return r["out"]


# ═══════════════════════ Step 4.5 · 复杂度判 + 场景分层 (v34 补) ═══════════════════════

def _judge_complexity_v34(question):
    """v34 · 从 qa_agent 搬进·longcat-2.0 判题目复杂度"""
    try:
        prompt = f"""判用户问题复杂度·决定回答该多详细。

问题: {question}

三档:
- simple: 单点直答·200-400 字
- medium: 多方案选/工具选·500-800 字
- complex: 大题多场景·1000+ 字·分场景

同时判 need_scenario_split (要不要分 SaaS/C 端/B 端等场景):
- 需要: 问题模糊·可能对应多场景
- 不需要: 问题具体·一个场景够

严格 JSON:
{{"level":"simple|medium|complex","reason":"20 字","need_scenario_split":true|false,
  "scenarios":["建议分几个场景·例[\\"SaaS\\",\\"C 端订阅\\",\\"B 端企业\\",\\"教培\\"]"]}}
只输出 JSON:"""
        raw = _call_llm("longcat-2.0", prompt, timeout=25)["out"]
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception:
        pass
    return {"level": "medium", "reason": "default",
            "need_scenario_split": False, "scenarios": []}


# ═══════════ v36 · 骨架库动态增长 ═══════════
TEMPLATE_LIB = os.path.join(QA_ROOT, "state", ".delivery_templates.jsonl")


def _load_template_lib():
    if not os.path.exists(TEMPLATE_LIB):
        return []
    tmpls = []
    with open(TEMPLATE_LIB, encoding="utf-8") as f:
        for l in f:
            try:
                tmpls.append(json.loads(l))
            except Exception:
                pass
    return tmpls


def _save_template(tmpl):
    os.makedirs(os.path.dirname(TEMPLATE_LIB), exist_ok=True)
    with open(TEMPLATE_LIB, "a", encoding="utf-8") as f:
        f.write(json.dumps(tmpl, ensure_ascii=False) + "\n")


def template_lib_extract_or_pick(question, cards):
    """v36 · 用户抓「骨架库动态增长没接」·从证据学新骨架或复用历史"""
    lib = _load_template_lib()
    lib_desc = "\n".join([
        f"- [{i}] {t.get('name','?')[:40]}: {t.get('when','?')[:60]}"
        for i, t in enumerate(lib[-20:])
    ]) or "(库空·还没有历史骨架)"

    prompt = f"""判断: 从证据里提取新骨架 append 库 · 或复用历史骨架 · 或不用骨架自构。

题: {question}

现有骨架库 (最近 20):
{lib_desc}

证据卡片:
{cards[:2000]}

如果证据里有反复出现的清晰结构 (例:「问题设计的 3 个真心法, 访谈流程的骨架, 两个容易忽略的信号, 黄金窗口期」这样 4-6 段结构)·**提取它** append 库。

严格 JSON:
{{"action":"use_lib|extract_new|freestyle",
  "lib_idx":如果 use_lib·数字 (0 到 {max(0, len(lib)-1)}),
  "new_template":{{"name":"20 字名字","when":"适合什么题 20 字","skeleton":"骨架 md 200 字·带 3-5 个 ## 标题占位"}},
  "reason":"20 字·为什么这选"}}
只输出 JSON:"""
    r = _call_llm("opus", prompt, timeout=60)
    d = _parse_json(r["out"]) or {"action": "freestyle", "reason": "default"}

    if d.get("action") == "extract_new" and d.get("new_template"):
        tmpl = d["new_template"]
        tmpl["extracted_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        tmpl["from_question"] = question[:80]
        _save_template(tmpl)
        return {"action": "extract_new",
                "skeleton": tmpl.get("skeleton", ""),
                "name": tmpl.get("name", "?"),
                "reason": d.get("reason", "")}

    if d.get("action") == "use_lib" and d.get("lib_idx") is not None:
        idx = d["lib_idx"]
        if 0 <= idx < len(lib):
            return {"action": "use_lib",
                    "skeleton": lib[idx].get("skeleton", ""),
                    "name": lib[idx].get("name", "?"),
                    "reason": d.get("reason", "")}

    return {"action": "freestyle", "skeleton": "", "reason": d.get("reason", "自构")}


def _step_scenario_split_v34(question, cards, scenarios):
    """v34 · 从 qa_agent 搬进·complex 题分多场景铺开
    用户 2026-07-24 抓「Q1 v33 只有 B 端没 C 端」
    """
    if not scenarios:
        return ""
    scenarios_str = " · ".join(scenarios)
    prompt = f"""这题在多个场景下钩子不同·请拆场景讲。

# 问题
{question}

# 建议分场景
{scenarios_str}

# 观点卡片
{cards[:3000]}

# 输出格式
```
## 这题在几个场景下差异大

**{scenarios[0]}** (核心痛点: XX)
- 追问方式: XX
- 关键指标: XX

**{scenarios[1] if len(scenarios) > 1 else 'C 端订阅'}** (核心痛点: YY)
- ...

**{scenarios[2] if len(scenarios) > 2 else 'B 端企业'}** (核心痛点: ZZ)
- ...
```

每场景 100-200 字·带 1-2 条卡片引用支撑。中文标点·不用 `·` 当逗号。

只输出 md:"""
    r = _call_llm("opus", prompt, timeout=180)
    return r["out"]


# ═══════════════════════ Step 5 · 叙事重组 · v20 D 骨架 (v32) ═══════════════════════

def step5_narrative(question, cards, scope, cost_rec, evidence=None, real_queries=None, scen_display=None):
    """写答案 · 严格 v20 D 骨架 + 用户 07-23 示范格式
    v32 重写 (用户抓 v31 灾难: 丢思考过程/搜索过程/画面感标题/真交付)
    """
    # 搜索过程数据 (给 opus 写「我搜了什么」段用)
    evidence = evidence or []
    deep = [e for e in evidence if e.get("stance") != "ad"][:15]
    kept_urls = "\n".join([
        f"- {e.get('title','?')[:60]} · 立场={e.get('stance','?')} · {e['url']}"
        for e in deep[:8]
    ])
    dropped_urls = "\n".join([
        f"- {e.get('title','?')[:60]} · {e.get('stance','?')} · 为什么弃"
        for e in [e for e in evidence if e.get("stance") == "ad"][:5]
    ])

    # v43 · 身份适配 · 默认关 · 开时读 memory 生嫁接提示
    _identity_hint = ""
    try:
        from qa_identity_adapter import enabled as _id_on, load_identity_brief, render_identity_hint_for_opus
        if _id_on():
            _identity_hint = render_identity_hint_for_opus(load_identity_brief())
    except Exception:
        pass

    # v13.88.9L · 输入演绎注入 (task fix_C_intent_inject) · 从 scen_display 抽出成独立段·让 opus 显式看到
    _intent_hint = ""
    try:
        _ic = (scen_display or {}).get("input_intent_context", "")
        if _ic:
            _intent_hint = (
                "\n# 输入演绎上下文 (回复前先看·带全上下文再动笔)\n"
                "以下是用户长期/当下真需求和近期抓过我的错·写答时贴近这些真需求·"
                "别在回帖里重犯这些错 (仅作你写作时的内部约束·别原文写进帖子):\n"
                f"{_ic}\n"
            )
    except Exception:
        pass

    prompt = f"""你是响应态写手。**严格照 v20 D 叙事骨架 + 用户 07-23 示范格式**。

{_identity_hint}
{_intent_hint}

# 用户问题
{question}

# 观点卡片
{cards}

# cost-first 判决 (作开场第一段)
{json.dumps(cost_rec, ensure_ascii=False, indent=2)}

# v37 · 智能过程数据 (「我搜了什么」段必须真实列这些内容·别编)
scen_display: {json.dumps(scen_display or {}, ensure_ascii=False, indent=2)}
real_queries: {json.dumps(real_queries or {}, ensure_ascii=False, indent=2)}

# 搜索证据池 (供你写「我搜了什么」段·选进/弃用交代)
选进主线 ({len(kept_urls.splitlines()) if kept_urls else 0} 条):
{kept_urls}

弃用 (广告立场):
{dropped_urls}

═══════════════════════════════════════════════════
# 用户示范格式 (v13.86.31 示范·严格照抄骨架)
═══════════════════════════════════════════════════

「访谈录音转文字这件事最容易被低估的就是**时间账**。百度开发者中心的 Nicky 在 2026-05-20 (URL) 算过一笔账,人工听打 1 小时音频要 4-6 小时,方言和专业术语错误率 35%...得直接改用软件工具来做。

## 挑工具按四题过筛

秒听录音博客的 Dinel 在 2026-07-11 (URL) 提了四道自问题:普通话还是中英混、只要文字还是要 AI 摘要+待办、只转本地音频还是要拉 B 站/播客、要不要 PDF/DOCX/SRT 导出 (这家自己卖同类产品,推荐有利益相关,仅供参考)...

## 录前 30 秒不做后期哭三天

WiseMindAI 的 Chris 在 2026-07-20 提醒,开录前要检测麦克风、勾"保存录音"...讯飞听见在知乎 2023-07-19 (URL) 给的进阶操作是上传时先填 5-10 个关键词...

## 转出的文字稿只是素材,真正有用的是后续归类整理的结果

腾讯云开发者社区 2026-06-21 (URL) 指出多人访谈不开发言人分离会漏 30% 上下文...

## 做系列访谈要养词典

VoiceStream 在 App Store (URL) 提出 project-based workflow...Notta 在 App Store (URL) 补了历史堆积的处理方案...」

═══════════════════════════════════════════════════
# 严守 6 条元规则 (骨架不能偏)
═══════════════════════════════════════════════════

## 1. 开场响应态 (自主判断格式·不硬塞)
- **接住用户处境**·像 v20 D 那种「XX 这个访谈你肯定做过——一开口用户全是「太贵」」
- 如果**题目真需要挑工具/选方案** (Q2 转文字工具选型是) → 讲成本·带真源
- 如果**是方法论题** (Q1 问题设计 / Q3 框架搭建) → **别硬塞成本段**·直接进方法论
- **由你 opus 判断该不该讲成本**·不硬门

## 2. 3-5 段方法论 · 每段一个感情画面感小标题
- ✅ 用户示范:「录前 30 秒不做后期哭三天」「转出的文字稿只是素材」「做系列访谈要养词典」
- ❌ 禁 「A. 分两次问」「B. 提问设计」「① 时间锚定」这种冷板 A/B/C bullet
- 每段 100-200 字·1 观点 + 1 真源引用 + 具体动作翻译

## 2.5 标题必须具体化·读者一眼懂 (v34 · 用户 2026-07-24 抓)
- ❌ 「录前 30 秒偷懒·后期哭三天」→ **读者不知道偷懒偷什么**
- ✅ 「录音前 30 秒设置错·后期改错字改到怀疑人生」→ **具体到偷懒的动作**
- ❌ 「转前 5 分钟填热词表·校对省一半」→ **热词表是啥读者不懂**
- ✅ 「转文字前塞几个专有名词进词库·准确率立跳 10 个点」→ **动作+结果具体**
- **标题禁「·」标点**·用逗号或空格·例「录音前 30 秒设置错 后期改错字改到怀疑人生」
- 标题必须给**洞察 insight**·让读者「哇原来这才是关键」·而不是描述表面动作
- 例 (方法论题最佳): 「目标那栏不敢写具体假设·就是心里没底」比「第一栏没写清就往下走」更有洞察

## 2.6 术语必带解释 (v34 · 用户抓「热词表用户不懂」)
- 一切读者可能不懂的词首次出现必带括号解释
- 例:「热词表 (让工具认识你产品的专有名词·例如「私域/DAU」)」
- 例:「JTBD (Jobs to Be Done·用户雇产品完成什么任务)」
- 例:「峰值负体验 (最崩溃那一刻)」
- 例:「Answer/Ask 比 (答案 scope 除以用户问的 scope·大于 3 就是过度回答)」

## 3. 引用格式硬门
- 站名 · 作者名 · 日期 · URL 四件套
- 每次提站名/作者必须重复带完整 URL (哪怕前面出现过)
- 立场标注只加**有倾向 (interested)** 源: 「(这家自己卖同类产品·推荐有利益相关·仅供参考)」
- **independent 独立源不加立场标注** (不然读起来累赘)

## 4. 中文标点
- 逗号 、 句号 。 破折号 —— 冒号:
- **禁 `·` 当逗号** · 仅"站名 · 作者 · 日期"元数据可用

## 5. 结尾 = 想深入哪块 3 选项
- 具体到问题·不模糊
- 例:「A. 微调查那 8 类选项怎么改成你产品的具体分类」

## 6. 硬禁
- 禁「综合下来」「首先」「其次」「值得注意」「缝在一起」「A/A ≤ 2」「Mavis 风格」等
- 禁「我朋友」「我上次」「我做过」第一人称亲历
- 禁编造数字 (数字必须来自证据摘录)
- 禁独立"交付"段 A/B/C/D/E 平铺 (v31 v29 v27 都栽这)
- **v56q 精确答案优先**: 若证据里已有针对问题的精确答案(具体数字/日期/名称·尤其 [Tavily综合答案] 或标 high_cross 的证据),**必须原样采纳该精确值·禁止自行用公式重新推导覆盖它**(如证据说体积 0.1777·就用 0.1777·别自己算 V=m/ρ≈0.7 覆盖)。检索到的实测值 > 你的推导。

═══════════════════════════════════════════════════
# 输出结构 (严格·不偏)
═══════════════════════════════════════════════════

```
## 我搜了什么 (v38 · 前置·多 agent 立体搜索)

**理解题目**: [用一句话说清楚·带识别到的场景]
- 我识别到的场景: [scen_display.scenarios 列出 3-5 个具体场景]
- 查历史库找到相似题: [scen_display.similar_history 列 3 个 qid]
- 原 query 太抽象·**按难度我补了 [N] 个不同视角的 query 各发独立 agent**:
  1. **[角色视角]** [multi_completed_queries[0].query]
  2. **[痛点深挖]** [multi_completed_queries[1].query]
  3. **[场景切换]** [multi_completed_queries[2].query]
  4. **[反面/时间]** [multi_completed_queries[3].query 如有]

**每个 agent 内部再拆 2-4 差异化子 query** (角度自决 · 不硬套模板):
- Agent 1 (角色视角) → sub_dim query
- Agent 2 (痛点深挖) → sub_dim query
- Agent 3 (场景切换) → sub_dim query

**Agent 间临时 graph 互引**: [cross_hits] 条 URL 被 ≥2 agent 共命中 → **高置信证据** 优先用。

**同源分散** (v42): 同域最多 2 条·避免过度依赖单一站点 (用户 2026-07-24 抓「beefed.ai 3 篇观点单一嫌疑」)。

**搜索统计** (v42 · 真实拆解):
- 合并去重 [total_unique] 条
- 免费加: 已带全文/缓存 [already_full_cached] 条 (不占预筛预算)
- longcat-2.0 预筛剩余 top30·同源分散后深读 [deep_read] 条
- 深读方法: tier 预判 · 大部分 curl 秒返 · 不走 CDP

**主线用** (每条超链格式): [文章真标题](URL) — 站名 · 作者 · 日期 — 讲了啥 (10 字)
- 至少 10 条·中英混合·独立源+有倾向源都列
**弃用** (每条超链格式): [文章真标题](URL) — 弃因具体到 15 字

## ⚠️ 来源立场警示

**列出主线用里所有可能有利益相关的源** (自卖同类产品·广告嫌疑·官方 PR) · 每条格式:
- ⚠️ [文章标题](URL) — 站方是 XX (卖什么产品) · 观点核心是 YY · **可能立场**: 推销 ZZ / 广告 / 独立

如果全部主线源都独立·此段只写「本次证据来自 N 个独立源·无明显利益相关」

**为什么前置**: 让读者带着「哪些观点该打折」的判断去读方法论段·而不是读完发现「原来这段来自卖同类产品的公司」

## 思考过程

**开头 1-2 句强制格式**:
> 这题在 [具体识别到的 N 个场景] 差异大 · 我参考历史相似题 [q01/q03 等] 拆解经验 · 立体查了 [N 个补 query × M 个 sub_dim] 得到 [K 条证据 · X 条被 ≥2 agent 共命中的高置信]。反直觉的一点是...

**中间必带 2-3 个「反直觉发现」段** (v48 硬规·用户 2026-07-27 抓):
- 每个反直觉发现 100-200 字
- 例: 「大部分教程教怎么问·但从证据看真流失原因根本不是问出来的·是从后台数据+决策链结构里推出来的」
- 每个发现带 1-2 处 URL 引用

**结尾必须先写「段落逻辑」再写正文** (v48 · 用户 2026-07-27 抓「先规划后写」):
> 段落顺序我按「[你选的行动流]」拆·不按访谈提纲第 1 题第 2 题堆——因为读者不缺题目清单·缺的是每一步背后「为什么这么问」的判断。
>
> 具体拆:
> - 第 1 段: [段落标题] · [1 句原因·为什么放这里]
> - 第 2 段: [段落标题] · [1 句原因]
> - ...
> - 第 5 段: [段落标题] · [1 句原因]

**必写字数 500-800** · 密度对齐 v43-C · 禁 300 字草草收尾

## 回答

[开场响应态段 · 100-150 字 · 接用户处境 · 由你 opus 判是否讲成本 · 方法论题直接进方法论]

**段落逻辑段已在思考过程结尾说明** · 这里直接进方法论段。

═══════════ v46 元规则 · 段落标题 (描述特征·非硬规·用户 2026-07-24 抓「元规则都是真响应·加硬门毫无意义」) ═══════════

**好回复的段落标题一般具备这些特征** (opus 自决用不用·别硬套):
- **具体动作** · 例「开场第一句就得说不留你」比「什么时候问」有信息量
- **数字量化** · 例「访前第 1 步 不是写提纲,是拉这 5 个人的后台行为日志」里的「5 个人」
- **反直觉钩子** · 例「中场先扔 7 张候选原因牌」让读者好奇「哪 7 张」
- **禁忌具化** · 例「全程禁用『你觉得』和『满不满意』」直接告诉别用啥词

**v43-C 磁盘真样例** (参考·不照抄):
✅ `### 访前第 1 步 不是写提纲,是拉这 5 个人的后台行为日志`
✅ `### 开场第一句就得说"不留你"—— 把访谈从挽留切成诊断`
✅ `### 中场先扔 7 张候选原因牌,让受访者选最贴的 —— 防止 80% 甩锅价格`
✅ `### 全程禁用"你觉得"和"满不满意" 换成"上次你 X 是哪天"`
✅ `### 收尾必问 aha 追溯 + 替代方案 —— 判断是真流失还是可复活`

**v45 磁盘反例** (空洞抽象):
❌ `### 什么时候问 (黄金 72 小时窗口)` — 只有时间点·不告诉用户「打谁的电话」
❌ `### 数据打底交叉验证` — 6 字标签·无信息量

**核心元规则** · **具体感 > 结构感** · 具体动作+数字+反直觉钩子·不是「关键词 (方法论括号)」这种表面结构。

═══════════ 结构参考 (v48 · 用户 2026-07-27 抓「导语 longcat-2.0 自决」不硬规) ═══════════

### [段落标题 · 具体动作或反直觉观点·让读者扫读就知道该干啥]

*[可选斜体导语 · 只当标题不够自明时写·20-30 字·关键动作或核心结论·标题已够清就不写]*

[100-200 字,1 观点+1 真源引用+具体动作,术语加括号解释]

### [下一段标题]

[同上·导语按需自决]

### [3-5 段]

[同上]

## 想深入哪块? (v39 · 真交互多选题·渲染进正文)

[此段内容由 qa_clarify 生成的真多选题填入·opus 别再自己写模板·后处理会 inject]
```

# v35 新硬门 (2026-07-24 用户抓 v34 3 处崩)

## 硬门 A · URL 必须 markdown 超链格式 · 全场景
- ❌ 禁写:`Kevin 在 2026-03-04 (https://www.userintuition.ai/posts/xxx)`
- ✅ 必须:`Kevin 在 [Churn Analysis for SaaS](https://www.userintuition.ai/posts/xxx) 里 2026-03-04 讲`
- **[文字] 必须是文章真标题**,不是站名不是"点击" · 你没标题就用「文章要点 15 字概括」
- **每个 URL 都要变超链** · 单独裸 URL 不行

**v41 硬门 · URL 全场景 · 你正文里但凡提到任何搜索结果·必带 [标题](URL) 超链** (用户 2026-07-24 抓):
- 提到域名 (beefed.ai · diact.com · zhuanlan.zhihu.com) → 必超链
- 提到作者名 (Ava · Kevin · 尤里卡高 · William Gu) → 首次带 [标题](URL) 超链
- 提到站名 (数皆智能 · 觅游 · Reforge) → 必超链
- 提到某篇文章 (「那篇」「这篇」「里说」「直接说」) → 必超链
- **禁: 「beefed.ai 的 Ava 在 xxx 里直接说」不带超链** · 必须写成「[根因分析](https://beefed.ai/zh/xxx) 里 Ava 直接说」
- 每重复引用同 URL 至少前两次带超链·第三次可省 (读者已知)

## 硬门 B · 「## 回答」必须有此标题
- 正文主体前必须有 `## 回答` 二级标题
- 「## 回答」下面才是响应态开场 + 5 段方法论 (改用 `###` 三级标题)
- 结构: `## 我搜了什么` → `## 思考过程` → `## 回答` → `### 具体标题 1..5` → `## 想深入哪块?`

## 硬门 C · 正文 · 严格禁当逗号
- ❌ 禁: 「原话不分类·周五聚类·tag 过 3 进 roadmap」(3 个 · 当逗号)
- ✅ 改: 「原话不分类,周五聚类,tag 过 3 进 roadmap」
- **只有元数据可用 `·`**: 「站名 · 作者 · 日期」 或 「工具 A / 工具 B」列表
- **正文 100% 中文逗号 、、 句号 。、 破折号 ——**
- 输出前**自检**: 一个段落里出现 3+ 个 `·` 就是滥用 · 全改逗号

**别写「## 交付」独立段** · **别写「## 思考过程」独立段** · **v20 D 就是方法论叙事 = 交付**

**长度自定** · opus 判够不够 · 一般 1500-2500 字

只输出 md · 不加代码块包裹:"""
    # v41 · 拿 opus 真首字节时间
    r = _call_llm("opus", prompt, timeout=300, system="", capture_first_byte=True)
    # 存到模块级变量供 orchestrate 读
    global _STEP5_FIRST_BYTE_S
    _STEP5_FIRST_BYTE_S = r.get("first_byte_s")
    out = r["out"]

    # v56 · 用户抓「正文写到一半断了」· 根因: 长文一次生成撞 max_tokens 被硬切
    # 修: stop_reason=max_tokens 时不静默接受半截文本·续写补全 (最多 2 轮)
    _continue_rounds = 0
    while r.get("truncated") and _continue_rounds < 2:
        _continue_rounds += 1
        cont_prompt = (
            "你上一轮的回答在写到一半时被截断了 (未写完)。"
            "下面是已经写好的部分，请**直接接着写**，不要重复已有内容、不要加开场白："
            f"\n\n{out[-1200:]}\n\n继续写:"
        )
        r = _call_llm("opus", cont_prompt, timeout=180, system="")
        if r.get("out"):
            out = out.rstrip() + r["out"]
        if not r.get("truncated"):
            break
    if _continue_rounds > 0:
        print(f"  [step5_narrative] 触发 {_continue_rounds} 轮续写 (max_tokens 截断)", file=sys.stderr)
        # v56 · 截断本身就是一次真实缺陷·喂给统一演绎/进化闭环·别只在本地续写完就当没发生过
        try:
            from correction_evolver import ingest as _ce_ingest
            _ce_ingest(
                text=f"qa_agent step5_narrative 触发 max_tokens 截断，续写 {_continue_rounds} 轮",
                context="output_gate:qa_step5_truncation",
                markers=["qa_truncation", "max_tokens"],
            )
        except Exception:
            pass
    return out


# v41 · 首字节记录 · orchestrate 展示用
_STEP5_FIRST_BYTE_S = None


# ═══════════════════════ Step 6 · 读者代入 · 就地替换 ═══════════════════════

def step6_reader_check_replace(question, narrative):
    """opus 装读者·抓抽象词就地替换"""
    prompt = f"""你是刚打开这篇答案的读者·从没做过这题。逐句读·标出「我猜不出意思」的句子。

# 原问题
{question}

# 答案
{narrative[:8000]}

# 标什么
- 造词但没解释 (「工具广告缝在一起」「同频闭环」)
- 术语没翻译 (JTBD/Van Westendorp 直接出现)
- 前后指代不明

JSON:
{{"unclear_sentences": [
  {{"quote": "原句 30 字", "why": "为什么猜不出", "rewrite": "改成读者能懂的 30 字"}}
]}}
如果全懂·返 {{"unclear_sentences": []}}
只输出 JSON:"""
    r = _call_llm("opus", prompt, timeout=60)
    data = _parse_json(r["out"])
    unclear = data.get("unclear_sentences", [])
    for u in unclear[:15]:
        q = u.get("quote", "").strip()
        rw = u.get("rewrite", "").strip()
        if q and rw and q in narrative:
            narrative = narrative.replace(q, rw)
    return narrative, len(unclear)


# ═══════════════════════ v35 后处理 · 3 硬门兜底修 ═══════════════════════

def postprocess_v35_format(md):
    """v35 · 用户 2026-07-24 抓 3 处 · Python 兜底修
    A · URL 变 markdown 超链 [title](URL)
    B · 「## 回答」标题若缺则加
    C · 正文 · 当逗号强制转中文逗号
    """
    import re as _re

    # ══ 硬门 A · 裸 URL 转超链
    # 匹配 "text (https://xxx)" 或 "text https://xxx" 模式
    # 只转 (URL) 单独出现的 · 已经在 [](URL) 里的不动
    def _url_to_link(m):
        prefix = m.group(1) or ""  # 前面的文字
        url = m.group(2)
        # 从 URL 提"标题" · 用路径最后一段作为回退
        path = url.rstrip("/").split("/")[-1]
        # 清理成人话
        title_guess = path.replace("-", " ").replace("_", " ")[:50]
        if not title_guess or title_guess.isdigit() or len(title_guess) < 3:
            # 找 URL 前面 30 字作 title 参考
            title_guess = "原文链接"
        return f"{prefix}[{title_guess}]({url})"
    # 匹配 "(https://...)" 但排除已经 [](...) 中的
    # 简化: 只把 `(https://...)` 且前面不是 `]` 的转换
    pattern = _re.compile(r'([^\]])\s*\((https?://[^)]+)\)')
    md = pattern.sub(_url_to_link, md)

    # ══ 硬门 B · 检查「## 回答」标题
    if "## 回答" not in md:
        # 找思考过程段结束位置 · 插入 ## 回答
        m = _re.search(r'(##\s*思考过程.*?)(\n##[^#])', md, _re.DOTALL)
        if m:
            md = md[:m.end(1)] + "\n\n## 回答\n" + md[m.end(1):]
        else:
            # 没有思考过程 · 找第一个 ## 后插
            m2 = _re.search(r'(^# [^\n]+\n(?:\n|.)*?)(\n## )', md)
            if m2:
                md = md[:m2.end(1)] + "\n\n## 回答\n" + md[m2.end(1):]

    # ══ 硬门 F · v55.3 · 用户 2026-07-27 抓「内部版本号读者看不懂」
    # 扫所有 ## / ### 标题·删括号内的 v48 longcat-2.0 ... 这种内部备注
    # 保留原意标题·例「## 开始行动 (v48 · longcat-2.0 ...)」 → 「## 开始行动」
    # 例「## 想深入哪块? (v47 · clarify 后处理)」 → 「## 想深入哪块?」
    def _clean_heading(m):
        title = m.group(1)
        # 删括号内含 v数字·或含「后处理/longcat-2.0/元规则/硬规」这种内部词
        cleaned = _re.sub(
            r'\s*\((v?\d+[^)]*|.*?(?:后处理|longcat-2.0|opus|硬规|元规则|自决|判决|清单|示范).*?)\)',
            '',
            title,
        )
        return "## " + cleaned.strip() if m.group(0).startswith("##") and not m.group(0).startswith("###") else "### " + cleaned.strip()

    # 只匹 ## 二级标题 · 不动 ### 三级 (行动锥层标题可能有版本符号但 opus 自决)
    md = _re.sub(r'^##\s+([^\n#][^\n]*)$', lambda m: "## " + _re.sub(
        r'\s*\((v?\d+[^)]*|.*?(?:后处理|longcat-2.0|opus|硬规|元规则|自决|判决|示范).*?)\)',
        '',
        m.group(1),
    ).strip(), md, flags=_re.MULTILINE)

    # ══ 硬门 C' · v55.2 · 中文全角标点通用规则 (用户 2026-07-27 抓)
    # 规则:
    #   英文 , → 中文 ，  (中文字符相邻时)
    #   英文 . → 中文 。  (句尾·避免误伤 x.y 版本号)
    #   英文 ? → 中文 ？
    #   英文 ! → 中文 ！
    #   英文 : → 中文 ：  (中文字符前)
    #   英文 ; → 中文 ；
    # 保留场景:
    #   - 代码块 ``` 内
    #   - URL 内 (http://x.y)
    #   - 英文纯句 (Server Components 支持)
    #   - 版本号 (19.0.0)
    # 策略: 只在「中文-标点-中文」触发·避免误伤

    def _fullwidth_punct_line(line):
        """一行内·把中文段的英文标点转中文全角"""
        # 跳过代码块行 (` 开头) / 引用块 / URL 行
        if line.strip().startswith(("```", "|", "  ", "\t")):
            return line
        if "http" in line and "http" != line.strip().split()[0] if line.strip() else True:
            # 有 URL · 只在 URL 外转
            pass

        # v55.2 · 中文-,-中文 / 中文-,-英数 / emoji-,-中文
        line = _re.sub(r'([一-龥」』】])\s*,\s*([一-龥「『【])', r'\1，\2', line)
        line = _re.sub(r'([一-龥」』】\U0001F300-\U0001FAFF])\s*,\s*([一-龥「『【])', r'\1，\2', line)
        line = _re.sub(r'([一-龥」』】])\s*,\s*([a-zA-Z0-9])', r'\1，\2', line)
        # v55.4 · 用户 2026-07-27 抓「反引号后 , 别用」硬 case
        # `代码`,别用 X → `代码`，别用 X (反引号包代码后 · 半角 , 接中文)
        line = _re.sub(r'(`[^`\n]{1,50}`)\s*,\s*([一-龥「『【])', r'\1，\2', line)
        # 或英数-,-中文·左侧接受更宽的字符 () 】 “ ” 」 』 】 % 等
        line = _re.sub(r'([a-zA-Z0-9\)\]」』】"\'”』】%])\s*,\s*([一-龥「『【])', r'\1，\2', line)

        # 中文-:-任意 (中文/英数/引号)·避免误伤 URL http://·代码
        # 关键: 中文字符左侧 + : 一律转 (中文字符已排除 http 语境)
        # 但需避免 line 里如果 : 后紧跟 //·就是 URL·跳
        def _colon_repl(m):
            after = m.string[m.end():m.end()+2]
            if after.startswith("//"):
                return m.group(0)  # 是 URL·保留
            return m.group(1) + "：" + m.group(2)
        line = _re.sub(r'([一-龥」』】])\s*:\s*([一-龥「『【a-zA-Z0-9"「\("])', _colon_repl, line)

        # 中文-;-中文
        line = _re.sub(r'([一-龥」』】])\s*;\s*([一-龥「『【])', r'\1；\2', line)

        # 中文-?-句末/空格/换行 → 中文？
        line = _re.sub(r'([一-龥」』】])\s*\?(?=[\s\n]|$)', r'\1？', line)

        # 中文-!-句末/空格/换行 → 中文！
        line = _re.sub(r'([一-龥」』】])\s*!(?=[\s\n]|$)', r'\1！', line)

        # 中文-.-中文/空格中文 (句尾) → 中文。
        # 避免误伤 www.x.y / 19.0.0 / 3.5 版本
        # 只匹**中文字符 + . + 空格/换行/中文**·且**左侧不是数字**
        line = _re.sub(r'([一-龥」』】])\s*\.(?=[\s\n]|[一-龥]|$)', r'\1。', line)

        return line

    md = "\n".join(_fullwidth_punct_line(l) for l in md.split("\n"))

    # ══ 硬门 C · 正文 · 当逗号 → 中文逗号
    # 保留元数据 (「站名 · 作者 · 日期」结构 · 匹配 x · y · z 三段) 不动
    # v54 · 用户 2026-07-27 抓「大量 · 当逗号没转」
    # 修: 放宽·中文-·-中文 一律转逗号·除非在白名单 (日期/URL/@/路径)
    lines = md.split("\n")
    fixed = []
    for line in lines:
        # v55.1 · 用户 2026-07-27 hard rule · bold + · 一律转冒号 (即使含 URL 也转)
        # **这一步做什么** · X → **这一步做什么**：X
        # **参考工具** · [URL 标题](URL) → **参考工具**：[URL 标题](URL)
        # 这是**结构性规则**·必先跑·不受 is_meta 影响
        line = _re.sub(r'(\*\*[^*]{2,20}\*\*)\s*·\s*', r'\1：', line)

        # v55.4 · 用户 2026-07-27 hard rule · 中文-·-中文 也从 is_meta 提出来
        # 「模板·抄它」这种·不管行里有没有 URL 都转 (URL 前后段内的 · 不受影响)
        # 但保留「元数据 x · y · z」结构 (源名·作者·日期) - 那个 URL 直接在括号里·不受影响
        line = _re.sub(r'([一-龥」』】])·([一-龥「『【])', r'\1，\2', line)  # 无空格版·紧贴的
        # 「反引号内容·反引号」也保留 (代码单位)

        # 白名单: 元数据行 (URL/日期/邮箱/文件路径)
        is_meta = (
            _re.search(r'\d{4}-\d{2}-\d{2}', line) or
            "http" in line or "@" in line or
            _re.search(r'\.(md|py|json|jsonl|txt|log|sh|js|html)$', line) or
            "```" in line  # 代码块
        )
        # v54 · 硬性: 标题 / 引用块 / 性能行 都保留
        is_heading = line.strip().startswith(("#", ">"))

        if not is_meta:

            # v55 · 用户 2026-07-27 抓「半角 , 应该是中文 ，」+「介绍性场景应转 :」
            # 中文/数-·-中文/英数 → 中文，英数
            line = _re.sub(r'([一-龥」』】\)\d])\s*·\s*([一-龥「『【\(a-zA-Z])', r'\1，\2', line)
            # 英数-·-中文
            line = _re.sub(r'([a-zA-Z])\s*·\s*([一-龥「『【\(])', r'\1，\2', line)
            # 标题内也扫 (v55 · 用户抓 「### ▶ 长 PRD 不丢节点·分段喂」)
            if is_heading:
                if not line.strip().startswith(">"):
                    line = _re.sub(r'([一-龥」』】\)\d])\s*·\s*([一-龥「『【\(a-zA-Z])', r'\1，\2', line)
            # v55 · 中文段里的半角 , → 中文 ， (用户抓「逗号是中文输入法的」)
            # v55.1 · 扩范围 · 左侧接受 emoji (U+1F300 起) 和英数字母
            # 右侧只要是中文字符·就转 (说明是中文段·半角 , 一定错)
            line = _re.sub(r'([一-龥」』】a-zA-Z0-9\U0001F300-\U0001FAFF])\s*,\s*([一-龥「『【])', r'\1，\2', line)
            # 中文字-半角 ,-非中文 (数字/英文·如「3 天后,LLM」)
            line = _re.sub(r'([一-龥」』】])\s*,\s*([a-zA-Z0-9])', r'\1，\2', line)
        fixed.append(line)
    md = "\n".join(fixed)

    # ══ 硬门 D · v36 · 标题缺逗号 (space + 中文起头连接短句)
    # 用户抓「分两波打,当天抓情绪 两周后抓行为」中间该逗号
    # 用户抓「挑访谈名单别撒网,后台健康分正常突然走的必须约」中间该逗号
    def _fix_title_comma(m):
        line = m.group(0)
        # ### 标题 · 找 "短句 空格 短句" 补逗号
        # 模式: 中文 + 空格 + 3-15 字中文短句 (不含标点)
        line = _re.sub(
            r'([一-龥]{2,15})\s+([一-龥]{3,20})(?=\s|$)',
            r'\1，\2',
            line
        )
        return line
    md = _re.sub(r'^###\s+.*$', _fix_title_comma, md, flags=_re.MULTILINE)

    # ══ 硬门 E · v41 · 全场景 URL 超链 · 提到域名/作者名/站名/文章名一律必带 URL
    # 用户 2026-07-24 抓「不是同一处/同一家小场景·所有搜索结果引用都要」
    #
    # 检测 3 类引用 (前两版只覆盖第一类):
    #   1. 引用短语: "XX 那篇/提到/讲/说"
    #   2. 裸域名: "beefed.ai/diact.com/zhuanlan.zhihu.com/xxx"
    #   3. 站名+人名: "beefed.ai 的 Ava / DIA 数皆智能"
    naked_refs = []

    # 类 1 · 引用短语 (v37 老规则)
    citation_pattern = _re.compile(
        r'([一-龥A-Za-z][一-龥\w. ]{1,25})\s*(那篇|这篇|同一处|同一家|同一篇|上文|前述|前面提过|提到|讲过|讲道|说过|写道|指出|直接说|里说)'
    )
    for m in citation_pattern.finditer(md):
        after = md[m.end():m.end()+150]
        if not _re.search(r'\[[^\]]+\]\(https?://', after) and 'http' not in after[:150]:
            naked_refs.append(("引用短语", m.group(1)[-15:]))
        if len(naked_refs) >= 12:
            break

    # 类 2 · 裸域名 (v41 新) · 如 "beefed.ai 提到" "diact.com 那篇"
    domain_pattern = _re.compile(
        r'(?<![\/\w.])([a-zA-Z][a-zA-Z0-9-]{1,30}\.(?:com|net|org|cn|ai|io|co|dev|blog))(?![\/\w.])'
    )
    for m in domain_pattern.finditer(md):
        # 排除自己已在 [](URL) 内的
        start_ctx = md[max(0, m.start()-80):m.start()]
        end_ctx = md[m.end():m.end()+150]
        if 'http' in start_ctx[-60:] or '](' in start_ctx[-40:]:
            continue  # 已在超链内
        # 后 150 字有 http 也算 OK · 否则报警
        if 'http' not in end_ctx and not _re.search(r'\[[^\]]+\]\(https?://', end_ctx):
            naked_refs.append(("裸域名", m.group(1)))
        if len(naked_refs) >= 15:
            break

    # v44 · 用户抓「注释塞正文与题无关」·改 stderr log·md 不追加
    if naked_refs:
        import sys as _sys
        print(f"[v44.postprocess] 裸引用 {len(naked_refs)} 处 (例 {naked_refs[:3]})·不追加正文",
              file=_sys.stderr)

    return md


def step8_5_self_check(question, final):
    """v44 · step8.5 · 四问自检 (delegate to qa_self_check.run_self_check)"""
    try:
        from qa_self_check import run_self_check
        return run_self_check(question, final)
    except Exception as e:
        print(f"  [step8.5 self_check fail] {e}", file=sys.stderr)
        return final, {"skipped": str(e)[:60]}


def inject_real_clarify_block(md, question, scenarios):
    """v39 · 真接 qa_clarify · 「想深入哪块」段用 longcat-2.0 真生的多选题替换 opus 写的假模板
    用户 2026-07-24 抓「回复里的多选题·你说了多选题澄清模式·但是没做」
    """
    try:
        from qa_clarify import generate_clarify_questions
    except Exception as e:
        return md + f"\n\n<!-- v39.clarify_inject fail: import {e} -->"

    clarify = generate_clarify_questions(question, scenarios, max_qs=3)
    if not clarify.get("should_clarify"):
        # 无需澄清·把段整段替换成 opus 已生成的即可
        return md

    qs = clarify.get("questions", [])
    if not qs:
        return md

    # v42 · 删「不确定你的场景时先告诉我」·多选题自身已够清晰 (用户 2026-07-24 抓)
    block_lines = []
    block_lines.append("**下面 3 道多选题·你答完可粘 `1a,2b,3c` 回来·我针对性重生**:\n")
    for i, q in enumerate(qs, 1):
        multi = "可多选" if q.get("multi") else "单选"
        block_lines.append(f"**Q{i}. {q.get('text','')}** ({multi})\n")
        for j, opt in enumerate(q.get("options", []), 1):
            letter = chr(96 + j)
            block_lines.append(f"- `{i}{letter}` {opt}")
        block_lines.append("")
    block_lines.append(f"> 判断依据: {clarify.get('reason','')}")
    block_lines.append("> 回复格式: `1a,1c,2b` (逗号分隔·每项 = 题号+字母)")
    block_lines.append(f"> 或直接 CLI: `python3 agent/qa_agent_main.py qXX --question \"...\" --out xxx.md --clarify --auto-clarify-answer 1a,2b`")

    clarify_md = "\n".join(block_lines)

    import re as _re
    # v47 · 用户 2026-07-27 抓「v43-C 有两个『想深入哪块』」
    # 病根: opus 生正文时会重复生一次「想深入哪块」段 · 后处理只替第一处 · 第二处遗留
    # 修: 先删掉全部现有「想深入哪块」段 · 再统一 append 一次真多选题
    md = _re.sub(r'\n##\s*想深入哪块\??[^\n]*\n(.*?)(?=\n##\s|\Z)', '', md, flags=_re.DOTALL)
    # 再追加一次真 clarify block
    md = md.rstrip() + "\n\n## 想深入哪块?\n\n" + clarify_md + "\n"
    return md


def sanity_check_duplicated_sections(md, log=print):
    """v47 · 通用段落重复检测 · 用户 2026-07-27 抓「监控 qa agent 内部低级错误」
    扫描: 同一 ## 二级标题是否出现 >1 次
    返: dict{section_name: count} · count>1 的都算异常
    """
    import re as _re
    headings = _re.findall(r'^##\s+([^\n#]+)', md, flags=_re.MULTILINE)
    from collections import Counter
    ct = Counter(h.strip() for h in headings)
    dups = {k: v for k, v in ct.items() if v > 1}
    if dups:
        log(f"[v47.sanity] ⚠️ 检测到重复段: {dups}", file=sys.stderr)
    return dups


def sanity_check_url_liveness(md, max_check=15, log=print):
    """v54 · 用户 2026-07-27 抓「死链误伤·CSDN 521/CodingFleet 405 都是真活的」
    根因: curl HEAD 被 Cloudflare/反爬拒 5xx·但浏览器 GET 正常
    修:
      1. curl -X GET 而非 HEAD (Method Not Allowed 消除)
      2. 只标 4xx (真 404/410) 为死链·5xx 一律信任 (反爬伪装)
      3. 加 User-Agent 头模拟浏览器
    """
    import re as _re
    import subprocess as _sp
    urls = _re.findall(r'\[[^\]]+\]\((https?://[^\)]+)\)', md)
    seen = set()
    unique_urls = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique_urls.append(u)
    dead = []
    UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
    for u in unique_urls[:max_check]:
        try:
            # v54 · -X GET (不用 HEAD) + UA + 只读 header (Range 前 512)
            r = _sp.run(
                ["curl", "-s", "-L", "-m", "5",
                 "-o", "/dev/null",
                 "-w", "%{http_code}",
                 "-A", UA,
                 "-H", "Accept: text/html,*/*",
                 "--max-filesize", "10240",
                 u],
                capture_output=True, text=True, timeout=8,
            )
            code = r.stdout.strip()
            # v54 · 只标真死链: 400/401/403/404/410 (客户端错误·内容不存在)
            # 忽略 5xx (Cloudflare/反爬伪装) · 空/000 (连接失败·可能网络问题)
            if code in ["400", "401", "403", "404", "410"]:
                dead.append({"url": u[:80], "code": code})
        except Exception:
            pass
    if dead:
        log(f"[v54.url_liveness] ⚠️ {len(dead)}/{min(len(unique_urls), max_check)} 真死链: {dead[:3]}",
            file=sys.stderr)
    return dead


def _is_calc_or_short_answer(md):
    """v13.96 · 判断答案是否为数值/短答案类型(计算类题)·不需要 URL 和固定段落。
    判据: 纯数字+单位(<200字符) 或 总长<400且无 URL 超链 → 数值/短答案。
    搜索/考据类长答案有 URL 超链和 ## 段落结构·不受此影响。"""
    import re as _re
    text = (md or "").strip()
    if not text:
        return False
    # 纯数值+单位(允许 $/元/万/倍/%/°C 等)·<200 字符 → 数值题
    _num_unit = _re.compile(
        r'^\$?-?\d[\d,]*\.?\d*\s*(?:元|万|亿|倍|%|°[cf]|kg|km|h|min|s|年|月|日|个|条|篇|道|本|张|枚|usd)?$',
        _re.IGNORECASE)
    if len(text) < 200 and _num_unit.match(text.lstrip('*').strip()):
        return True
    # 无 URL 超链 且 总长 < 400 → 短答案(计算/推理/琐事)
    has_md_links = bool(_re.search(r'\[[^\]]+\]\(https?://', text))
    if not has_md_links and len(text) < 400:
        return True
    # v13.97(2026-08-20 · bc 实证 bc_1786949292) · 结构化短答案格式豁免:
    #   BrowseComp/结构化题的答案是 "Explanation:...(长解释) Exact Answer:X Confidence:Y%"——
    #   本质是【短答案+长解释】·核心答案(Exact Answer)是简短实体/年份·不是"搜索考据长答案"·
    #   不该被要求堆 10 个 URL 超链和 ## 回答/## 我搜了什么 固定段落。否则长 Explanation
    #   撑过 400 字 → 被误报 low_url_link_count + missing_required_sections → 触发无谓重跑
    #   (重跑丢已锁定实体漂走·真凶之一)。判据=含结构化答案标记·非针对某题(识别格式非答案)。
    _structured_answer = bool(_re.search(
        r'(?:^|\n)\s*(?:Exact Answer|Final Answer|最终答案|确切答案|Answer)\s*[:：]',
        text, _re.IGNORECASE)) and bool(_re.search(
        r'(?:^|\n)\s*(?:Confidence|Explanation|置信度|解释)\s*[:：]', text, _re.IGNORECASE))
    if _structured_answer:
        return True
    return False


def sanity_check_full(md, log=print):
    """v47 · 完整性全维扫 · 用户 2026-07-27 抓「监控 qa agent 内部低级错误」

    v13.96(2026-08-14) · 数值/短答案适配: 计算类题(如 "$89706.00")
    不需要 URL 超链和 ## 回答/## 我搜了什么 段落——只对搜索/考据类长答案检查这些。
    事故: 表格题答案 $89706.00 被报 low_url_link_count + missing_required_sections·
    数值题不需要 URL 和固定段落结构·误报噪音淹没真问题。

    扫描 6 维 (v48 加 URL 死链):
      1. 重复标题 (## X 出现 >1 次)
      2. 空段 (## X\n\n## Y · 段间无内容)
      3. 断裂 markdown (`[text](` 无匹配的 `)` · 或裸 `http://` 未包)
      4. URL 超链数低于阈值 (<10) [仅搜索/考据类答案]
      5. 缺失必要段 (## 回答 / ## 我搜了什么) [仅搜索/考据类答案]
      6. URL 死链 (v48 · curl HEAD 前 15 个)

    返 dict · issue_type → detail (list)
    """
    import re as _re
    issues = {}

    # v13.96 · 数值/短答案 → 跳过搜索类维度(URL 数/固定段落/死链)
    _is_short = _is_calc_or_short_answer(md)

    # 维 1 · 重复标题 (复用 sanity_check_duplicated_sections)
    dups = sanity_check_duplicated_sections(md, log=lambda *a, **kw: None)
    if dups:
        issues["duplicate_sections"] = dups

    # 维 2 · 空段
    empty_pat = _re.compile(r'^(##\s+[^\n]+)\n+(?=##\s)', flags=_re.MULTILINE)
    empty = [m.group(1) for m in empty_pat.finditer(md)]
    if empty:
        issues["empty_sections"] = empty

    # 维 3 · 断裂 markdown 链接
    # 找形如 `[text](URL` 但 URL 后无 `)` 的 · 必须真断裂 (regex 精确)
    # 用 negative lookahead 找 `[text](xxx` 后面从下一字符开始至换行内没 `)`
    broken = []
    for m in _re.finditer(r'\[[^\]]{1,120}\]\(([^\)\n]+)', md):
        # 检查匹配尾部到下一个换行之间有没有 `)`
        end = m.end()
        line_end = md.find('\n', end)
        if line_end == -1:
            line_end = len(md)
        rest_of_line = md[end:line_end]
        if ')' not in rest_of_line:
            broken.append(md[m.start():end][:80])
        if len(broken) >= 5:
            break
    if broken:
        issues["broken_markdown_links"] = broken
    # 裸 URL (不在 markdown 超链内)
    naked_urls = []
    for m in _re.finditer(r'(?<![\(\[])https?://[^\s\)\]]+', md):
        # 检查前 3 字符是否已在 markdown 语法内
        start = m.start()
        pre = md[max(0, start-3):start]
        if '](' not in pre and '(' != md[start-1:start]:
            naked_urls.append(m.group(0)[:80])
    if naked_urls and len(naked_urls) > 3:  # 少量裸 URL (nocode 直接给) 可接受
        issues["excessive_naked_urls"] = naked_urls[:5]

    # 维 4 · URL 超链数低于阈值 [v13.96: 仅搜索/考据类答案检查]
    if not _is_short:
        link_count = len(_re.findall(r'\[[^\]]+\]\(https?://', md))
        if link_count < 10:
            issues["low_url_link_count"] = link_count

    # 维 5 · 缺失必要段 [v13.96: 仅搜索/考据类答案检查]
    if not _is_short:
        required = ["## 回答", "## 我搜了什么"]
        missing = [r for r in required if r not in md]
        if missing:
            issues["missing_required_sections"] = missing

    # 维 6 · v48 · URL 死链 [v13.96: 数值/短答案跳过] (可关: MEYO_QA_URL_LIVENESS=0)
    if not _is_short and os.environ.get("MEYO_QA_URL_LIVENESS", "1") == "1":
        dead = sanity_check_url_liveness(md, max_check=15, log=log)
        if dead:
            issues["dead_urls"] = dead

    # 汇报
    if issues:
        summary = f"发现 {len(issues)} 类问题: {list(issues.keys())}"
        log(f"[v47.sanity] ⚠️ {summary}", file=sys.stderr)
    else:
        log("[v47.sanity] ✅ 完整性 5 维全通过", file=sys.stderr)
    return issues


def sanity_auto_fix(md, issues, log=print):
    """v47 · 完整性自愈 · 能自动修的就修·不能修的只 warn
    可自动修:
      · duplicate_sections: 保留第一处·后续删
      · empty_sections: 已由 opus 生成·不动
    不能自动修 (只 warn):
      · broken_markdown_links / excessive_naked_urls / low_url_link_count / missing_required
    """
    import re as _re

    # 修 · 重复段
    # v51 · 用户 2026-07-27 抓「sanity 删了含 URL 的 step7 tail 保留空占位段」
    # 修: 保留「内容最长」而非「第一处」·避免删掉真内容留下空占位
    for name, count in issues.get("duplicate_sections", {}).items():
        if count <= 1:
            continue
        pattern = _re.compile(
            r'(##\s+' + _re.escape(name) + r'\s*\n(?:.*?(?=\n##\s|\Z)))',
            _re.DOTALL,
        )
        matches = list(pattern.finditer(md))
        if len(matches) > 1:
            # v51 · 按内容长度排序·保留最长
            longest_idx = max(range(len(matches)),
                              key=lambda i: len(matches[i].group(0)))
            # 从最后一个开始删·避免 offset 错乱
            for i in reversed(range(len(matches))):
                if i == longest_idx:
                    continue
                m = matches[i]
                md = md[:m.start()] + md[m.end():]
            log(f"[v51.autofix] 已删 {count-1} 处重复段 · 保留最长第 {longest_idx+1} 处 '{name}' ({len(matches[longest_idx].group(0))} 字符)",
                file=sys.stderr)

    # v48 · 修 · 死链自动打标 (不删 · 让用户看到警告)
    for d in issues.get("dead_urls", []):
        u = d.get("url", "")
        # 匹配 [text](URL) · 加 ⚠️ 已失效标
        pat = _re.compile(r'\[([^\]]+)\]\(' + _re.escape(u), _re.MULTILINE)
        md = pat.sub(r'⚠️ [\1] 已失效链接·搜标题找 (' + u, md)
        log(f"[v48.autofix] 死链已打警告标 · {u}", file=sys.stderr)

    # v56 · 兜底清理内部批注泄漏
    # opus 生成思考过程段时偶尔会把内部字数要求/写作规范提示当成正文输出
    # (例: "(建议删除，这是内部字数要求不该出现在成品里)")
    # 这是硬清洗: 正则匹配包含「内部写作规范/建议删除/不该出现在成品」的
    # 整行 (含前后括号) 直接删掉，不要留给读者。
    _leak_patterns = [
        r'\([^)]*(?:内部写作规范|建议删除|不该出现在成品)[^)]*\)',
        r'\([^)]*(?:内部字数要求|不该写出来)[^)]*\)',
    ]
    for pat_str in _leak_patterns:
        pat = _re.compile(pat_str)
        new_md, n = pat.subn('', md)
        if n > 0:
            md = new_md
            log(f"[v56.autofix] 清理 {n} 处内部批注泄漏", file=sys.stderr)

    # v49 · hard gate · 编造的假 nocode 域名一律删段 (用户 2026-07-27 抓 meyo.app)
    # 真 nocode 域名只有 .mynocode.host · 其他 .meyo.app / .noc.dev 等一律假
    fake_nocode_patterns = [
        r'https?://[a-z0-9-]+\.meyo\.app',
        r'https?://[a-z0-9-]+\.noc\.dev',
        r'https?://[a-z0-9-]+\.nocode-fake\.[a-z]+',
    ]
    for pat_str in fake_nocode_patterns:
        # 找 `**nocode 部署** · [name](假 URL)` 整个 markdown 链接 · 替换为警告
        fake_pat = _re.compile(
            r'(\*\*nocode 部署\*\*[^\n]*\[)([^\]]+)(\]\()(' + pat_str + r')(\))',
            _re.MULTILINE,
        )
        for m in fake_pat.finditer(md):
            log(f"[v49.hard_gate] 假 nocode 域名删段: {m.group(4)}", file=sys.stderr)
        md = fake_pat.sub(r'**nocode 部署** · ⚠️ 未真部署 (跳过·请先 `nocode login`)', md)

    # v52 · hard gate · 同一 nocode URL 不能出现 2 层 (用户 2026-07-27 抓「同 URL 2 标题」)
    # 找所有 [title](URL) · 若 URL 相同但 title 不同 · 保留第一处·后续替换成「同上工具」
    nocode_links = _re.findall(r'\*\*nocode 部署\*\*[^\n]*\[([^\]]+)\]\((https://[a-z0-9-]+\.mynocode\.host)\)',
                                md)
    seen_urls = {}
    for title, url in nocode_links:
        seen_urls.setdefault(url, []).append(title)

    for url, titles in seen_urls.items():
        if len(titles) > 1:
            log(f"[v52.hard_gate] 同 URL {url} 出现 {len(titles)} 次 · 保留首个 '{titles[0]}' · 后续删",
                file=sys.stderr)
            # v52.1 · 从 **nocode 部署** 一直吃到下一 ### / ## / --- / 空行**行动指引**
            # 关键: 「打开怎么用」段和其后 1-N 步骤都要吃
            first_pat = _re.compile(
                r'\*\*nocode 部署\*\*[^\n]*\[([^\]]+)\]\(' + _re.escape(url) + r'\)'
                r'.*?'
                r'(?=\n###\s|\n##\s|\n---\s*\n|\n\*\*行动指引\*\*|\n\*\*参考|\Z)',
                _re.DOTALL,
            )
            matches = list(first_pat.finditer(md))
            if len(matches) > 1:
                for m in reversed(matches[1:]):
                    replacement = f"**已用工具** · 见前面「{titles[0]}」层内的 [{titles[0]}]({url}) · 本层直接跳部署"
                    md = md[:m.start()] + replacement + "\n" + md[m.end():]

    return md


# ═══════════════════════ Step 7 · nocode 真生产 ═══════════════════════

def step7_nocode_produce(question, scope, cost_rec, narrative, qid="", force_new=None):
    """v34 · 用户 2026-07-24 抓「nocode 2 天没更新·你一直骗我」
    根因: 之前逻辑见 state 有 URL 就复用·**从不真部署新页**
    修: force_new=True 强制真跑 nocode_delivery.py · 生成新 chatId 新 URL
    环境变量 MEYO_QA_NOCODE_FORCE_NEW=1 触发
    """
    if force_new is None:
        # v44 · 默认改开·用户 2026-07-24 抓「nocode 一直复用没真跑」
        force_new = os.environ.get("MEYO_QA_NOCODE_FORCE_NEW", "1") == "1"

    # v34 · 只在非 force_new 时才考虑复用
    # v56d · 用户 2026-08-04 抓「复用的 URL 跳 SSO 登录页/失效，根本没校验就塞给用户」
    # 修: 复用前必须真发请求校验 URL 还活着，死链直接放弃复用（走真部署或诚实告知没工具）
    state_file = os.path.join(QA_ROOT, "state", "nocode_state.json")
    if not force_new and qid and os.path.exists(state_file):
        try:
            state = json.load(open(state_file, encoding="utf-8"))
            hist = state.get(qid)
            if hist and hist.get("external_url") and not _url_is_alive(hist["external_url"]):
                print(f"  [nocode] ⚠️ 历史部署已失效(HTTP校验未过)·丢弃·{hist['external_url']}", file=sys.stderr)
                hist = None
            if hist and hist.get("external_url"):
                # 已有部署且校验存活·直接复用
                print(f"  [nocode] 复用历史部署(已校验存活)·{hist['external_url']}", file=sys.stderr)
                # 让 opus 写自然的过渡段·不裁 narrative 尾部
                transition_prompt = f"""为下面已写好的答案·加一段「用配套工具」结尾·自然承接。

# 答案末段
{narrative[-500:].strip()}

# 工具信息
URL: {hist['external_url']}
主要工具/方法: {cost_rec.get('recommend', '')}

# 输出格式 (200-300 字·自然承接·中文标点)
```
## 开始行动

[1 句自然过渡·承接上文最后一个观点·像跟朋友说「上面这些手动跑不容易·搭了个页面帮你跑」]

👉 {hist['external_url']}

[100-200 字·具体动作:「你打开填 X·点 Y·得 Z 结果·然后 W」·像用户示范里那种具体到「打印出来对着照 4-8 步补」]
```

- 禁「A. 通义听悟...B. 逐字稿」这种从 narrative 裁的碎片
- 禁「回复这条消息」客服口吻
- 只输出 md·不加代码块包裹:"""
                t_r = _call_llm("opus", transition_prompt, timeout=120)
                if t_r["ok"] and t_r["out"]:
                    return narrative.rstrip() + "\n\n" + t_r["out"].strip(), {"reused": True, "url": hist["external_url"]}
                # fallback · 短 tail
                return narrative.rstrip() + f"\n\n## 开始行动\n\n👉 {hist['external_url']}", {"reused": True, "url": hist["external_url"]}
        except Exception:
            pass

    # 判决要不要造
    prompt = f"""判断这题·**真造一个 nocode 工具** 是否比看 URL 更能帮读者。

# 用户问题
{question}

# cost-first 建议
{json.dumps(cost_rec, ensure_ascii=False)}

# 输出 JSON
{{"need_tool": true|false, "reason": "20 字·为什么要/不要造",
  "tool_spec": "如果要造·1-2 句描述工具功能·比如「输入 A/B/C·点生成·得 X 分组表」"}}
只输出 JSON:"""
    r = _call_llm("longcat-2.0", prompt, timeout=30)
    d = _parse_json(r["out"])
    # v46 · 用户 2026-07-24 抓「nocode-cli 是自动化部署 skill·工具锥每层都可 nocode 化」
    # 不再只在 need_tool=true 时跑·任何方法论题都可能配套 nocode 工具
    # 环境变量 MEYO_QA_NOCODE_ALWAYS=1 强制跑 (v46 默认 1)
    _always = os.environ.get("MEYO_QA_NOCODE_ALWAYS", "1") == "1"
    if not d.get("need_tool") and not _always:
        return narrative, {}
    if not d.get("need_tool") and _always:
        d = {"need_tool": True,
             "reason": "v46 · 方法论题也配套 nocode 工具",
             "tool_spec": d.get("tool_spec") or f"配套「{question[:30]}」的交互工具"}
    print(f"  [nocode 判决] 要造工具·{d.get('reason','')}", file=sys.stderr)

    # v32 · 真调 nocode_delivery.py 部署 (不是客服问答)
    tool_spec = d.get('tool_spec', '一个配套工具')
    try:
        # 内嵌 nocode 段·作为 nocode_delivery 的输入
        nd_script = os.path.join(HERE, "nocode_delivery.py")
        if os.path.exists(nd_script) and qid:
            print(f"  [nocode] 尝试真部署 {qid} · {tool_spec[:40]}", file=sys.stderr)
            # v46 · 先检 nocode CLI 登录态·避 Token 已过期
            try:
                status_r = subprocess.run(
                    ["nocode", "status"], capture_output=True, text=True, timeout=15,
                )
                if "Token 已过期" in status_r.stdout or "未登录" in status_r.stdout:
                    print(f"  [nocode] ⚠️ Token 已过期·跳过 CLI 部署 (需 `nocode login`)",
                          file=sys.stderr)
                    raise RuntimeError("nocode_login_expired")
            except FileNotFoundError:
                print(f"  [nocode] ⚠️ 无 nocode CLI 命令·跳过", file=sys.stderr)
                raise RuntimeError("nocode_cli_missing")

            nd = subprocess.run(
                ["python3", nd_script, qid],
                capture_output=True, text=True, timeout=600,
            )
            # v46 · 详细 log · 失败必输出 stdout/stderr 前 200 字
            if nd.returncode != 0 or "error" in nd.stdout.lower()[:500]:
                print(f"  [nocode] ⚠️ nocode_delivery 返 {nd.returncode} · stdout[:200]:{nd.stdout[:200]}",
                      file=sys.stderr)
                print(f"  [nocode] stderr[:200]:{nd.stderr[:200]}", file=sys.stderr)
            m = re.search(r"https://[a-z0-9-]+\.mynocode\.host", nd.stdout + nd.stderr)
            if m:
                external = m.group(0)
                print(f"  [nocode] ✅ 真部署 · {external}", file=sys.stderr)
                # v52 · 用户 2026-07-27 抓「3 个开始行动并存」
                # 修: 不 append 独立段·只 return URL 给 step7.3 融合进工具锥
                return narrative, {"deployed": True, "url": external,
                                    "tool_spec": tool_spec}
    except Exception as e:
        print(f"  [nocode] 部署失败 · {e}", file=sys.stderr)

    # v46 · 部署真失败·fallback 历史部署 URL (若有) · 不返空 · 用户 2026-07-24 抓「nocode 部署真跑失败没兜底」
    try:
        # 找同 qid 的历史部署
        history_json = os.path.join(HERE, "nocode_deploy_history.json")
        if os.path.exists(history_json):
            with open(history_json, encoding="utf-8") as fh:
                hist_all = json.load(fh)
            hist = None
            if isinstance(hist_all, list):
                # 找同 qid 的最新
                for h in reversed(hist_all):
                    if h.get("qid") == qid and h.get("external_url"):
                        hist = h
                        break
            elif isinstance(hist_all, dict):
                hist = hist_all.get(qid)
            if hist and hist.get("external_url"):
                print(f"  [nocode] v46 fallback 历史部署 · {hist['external_url']}", file=sys.stderr)
                # v52 · 只 return URL 不 append 段
                return narrative, {"fallback": True, "url": hist["external_url"],
                                    "tool_spec": tool_spec}
    except Exception as e2:
        print(f"  [nocode fallback] 读历史 fail · {e2}", file=sys.stderr)

    # v52 · fallback 也失败·只 return meta 给 step7.3 · 不 append 独立段
    return narrative, {"skipped_no_url": True, "tool_spec": tool_spec}


# ═══════════════════════ 输入演绎注入 (v13.88.9L) ═══════════════════════
# 断点修复 (task fix_C_intent_inject · 2026-08-02):
#   回复能力体检发现 qa_agent 主链路 orchestrate() 真串起来了·但「输入演绎注入」
#   没接进回复链——给社区用户回帖时·没带「历史打脸 + 长期意图 + 当下意图」上下文。
#   参考 unified_user_prompt._inject_deduce_context / intent_consumer_helper·复用现有函数。
#   注入点: step0.1 triage 之后·step2 搜索之前 → 让意图上下文能指导搜索和回答。
#   fail-safe: 任何异常都不阻断回复·返回空 dict。

_V11_DIR = os.path.join(SEED_ROOT, "v11")


def _recent_face_slaps(days=3, top=5):
    """读近 days 天真打脸 (过滤 test_only/uncategorized·去重)·复用 unified_user_prompt 的过滤逻辑。"""
    from datetime import timedelta
    fp = os.path.join(_V11_DIR, ".face_slaps.jsonl")
    if not os.path.exists(fp):
        return []
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    slaps, seen = [], set()
    try:
        for l in open(fp, encoding="utf-8", errors="ignore"):
            try:
                s = json.loads(l)
            except Exception:
                continue
            if str(s.get("ts", "")) < cutoff:
                continue
            cat = str(s.get("category", ""))[:30]
            rule = str(s.get("rule", ""))[:80]
            if not rule or cat in ("test_only", "uncategorized") or "test" in cat.lower():
                continue
            key = cat + rule[:20]
            if key in seen:
                continue
            seen.add(key)
            slaps.append(f"{cat} → {rule}")
    except Exception:
        return []
    return slaps[-top:]


def _qa_gateway_audit_call(system, prompt, model_override="longcat-2.0", timeout=30):
    """把 v11 监督器的 LLM 契约适配到 QA 统一网关。"""
    result = _gateway_call(prompt, timeout=timeout, system=system)
    return {
        "ok": result.get("ok", False),
        "raw": result.get("out", ""),
        "error": result.get("err", ""),
    }


def _run_latest_architecture_gates(question, evidence, final, trace, deep_result=None):
    """v54+ 强制消费深研、监督、演绎、进化四类资产；任一模块失败仅记录，不回退整条回答。"""
    report = {"deep_research": {}, "supervision": {}, "deduction": {}, "evolution": {}}

    # 五角色深研：Planner/Scout/Evidence/Skeptic/Judge。复用前置结果，避免重复搜索。
    deep = deep_result or {}
    if not deep:
        try:
            from deep_research_swarm import run_five_agent_research
            deep = run_five_agent_research(question, llm=_qa_gateway_audit_call,
                                           goal_card=(trace or {}).get("goal_card") or {})
        except Exception as exc:
            report["deep_research"] = {"error": f"{type(exc).__name__}: {exc}"}
            deep = {}
    if deep:
        report["deep_research"] = {
            "session_id": deep.get("session_id", ""),
            "conclusions": len(deep.get("conclusions", [])),
            "evidence": len(deep.get("evidence", [])),
            "unresolved": deep.get("unresolved_questions", [])[:4],
            "hop_count": deep.get("hop_count", 1),  # v56c · 真 multi-hop 实际跑了几跳
        }

    # 内容监督：网关调用，缺口或低置信会变成 trace 中可见的阻断信号。
    try:
        from content_supervisor import supervise_content
        merged_evidence = list(evidence or []) + list(deep.get("evidence", []))
        supervision = supervise_content(
            "reply", question, merged_evidence,
            context=json.dumps(report["deep_research"], ensure_ascii=False),
            draft=final, timeout=45, llm_call=_qa_gateway_audit_call,
        )
        report["supervision"] = supervision
    except Exception as exc:
        report["supervision"] = {"error": f"{type(exc).__name__}: {exc}"}

    # 统一演绎只处理“已观察到的缺口”，禁止空跑并制造伪问题。
    missing = str(report.get("supervision", {}).get("missing", "")).strip()
    if missing:
        try:
            from deduce_hub import deduce
            report["deduction"] = deduce(
                "output_review", missing, kind="qa_coverage_gap",
                context=f"question={question[:300]}", use_opus=False,
            )
        except Exception as exc:
            report["deduction"] = {"error": f"{type(exc).__name__}: {exc}"}

    # v56 · 「全接入」· 前面三环只做到“发现问题+演绎根因”，缺最后一环
    # 「进化」——把本轮真实观察到的缺口(监督缺口/截断/工具锥空)真正推进成
    # 可执行的进化任务，而不是只落盘等下次 cron 才被消费。
    # 只在“确有信号”时才触发 tick，避免空跑污染 corrections.jsonl。
    try:
        if missing or report.get("deep_research", {}).get("unresolved"):
            from correction_evolver import ingest as _ce_ingest, tick as _ce_tick
            if missing:
                _ce_ingest(
                    text=f"QA 监督发现缺口未闭环: {missing[:200]}",
                    context="output_gate:qa_supervision_gap",
                    markers=["qa_supervision_gap"],
                )
            tick_result = _ce_tick(max_process=3)
            report["evolution"] = {
                "triggered": True,
                "processed": tick_result.get("processed", 0),
                "goals_generated": sum(
                    r.get("n_goals_generated", 0) for r in tick_result.get("results", [])
                ),
            }
        else:
            report["evolution"] = {"triggered": False, "reason": "无新增缺口信号，跳过空跑"}
    except Exception as exc:
        report["evolution"] = {"error": f"{type(exc).__name__}: {exc}"}

    trace["latest_architecture"] = report
    return report


def deduce_input_context(question):
    """输入演绎注入 · 回复前先拿到「当下意图 + 长期意图 + 近期打脸」。
    复用: intent_deducer.classify_user_turn (当下意图判定) +
          intent_consumer_helper.get_user_focus (长期意图·读 .intent_current.json) +
          _recent_face_slaps (近 3 天打脸)。
    fail-safe: 任一子块失败都不阻断·缺的字段留空。
    返回 {current_turn, long_term, face_slaps, rendered}·rendered 是拼好的一段·可直接塞 prompt。
    """
    ctx = {"current_turn": {}, "long_term": {}, "face_slaps": [], "rendered": ""}

    # ① 当下意图 · classify_user_turn 判这句本身的属性 (kind/tags/是否真诉求)
    try:
        from intent_deducer import classify_user_turn
        ctx["current_turn"] = classify_user_turn(question or "")
    except Exception:
        pass

    # ② 长期意图 · 读 .intent_current.json 的 1h/1d 严厉段关键词 + LLM main_goal/unmet_needs
    try:
        from intent_consumer_helper import get_user_focus
        cur = get_user_focus("1h")
        longt = get_user_focus("1d")
        ctx["long_term"] = {
            "current_window": cur,
            "day_window": longt,
        }
    except Exception:
        pass

    # ③ 近 3 天打脸
    try:
        ctx["face_slaps"] = _recent_face_slaps(days=3, top=5)
    except Exception:
        pass

    # ④ 渲染成给 opus 的一段 (只在有真内容时才拼)
    try:
        lines = []
        cur = ctx["long_term"].get("current_window", {}) or {}
        day = ctx["long_term"].get("day_window", {}) or {}
        cur_kws = cur.get("top_kws") or []
        day_kws = day.get("top_kws") or []
        if cur_kws:
            lines.append(f"【用户当下想要】(1h): {' / '.join(str(k) for k in cur_kws[:4])}")
        if day_kws:
            lines.append(f"【用户长期在意】(1d): {' / '.join(str(k) for k in day_kws[:4])}")
        mg = day.get("main_goal") or cur.get("main_goal")
        if mg:
            lines.append(f"【用户核心目的】: {mg}")
        unmet = day.get("unmet_needs") or cur.get("unmet_needs") or []
        if unmet:
            lines.append(f"【用户未闭环 pain】: {' | '.join(str(u) for u in unmet[:3])}")
        if ctx["face_slaps"]:
            lines.append("【近 3 天用户抓过我·别在回帖里重犯】:")
            for s in ctx["face_slaps"]:
                lines.append(f"  - {s}")
        if lines:
            ctx["rendered"] = "\n".join(lines)
    except Exception:
        pass

    return ctx


# ═══════════════════════ Orchestrator 主流程 ═══════════════════════

def _factual_extract(question, evidence, goal_card, call_llm_fn=None,
                     multihop_evidence=None, log=None, mission_hint="",
                     best_effort=False):
    """v56z · 事实题精确抽答(从证据只抽不推导)。抽成 helper 供两处复用(DRY):
      ① step2.6 GAIA 轻量分叉(蜂群前·抽到就短路省 266s 蜂群)
      ② step4.9 事实题旁路(蜂群后·评测/最终答案参考)
    返回: 抽到的答案字符串(已去空白·[:200] 截断) 或 "" (未抽到/UNKNOWN/异常)。
    行为与 v56x 原 step4.9 抽答一致: 高可信(tavily/high_cross)+推理链末跳证据排前·
    回看 goal_card·opus 只抽不推导·证据里没有才 UNKNOWN。
    v59 · mission_hint: mission_state.render_for_prompt() 的渲染文本·带上全程累积的
    known_facts/open_gaps·治「step4.9 不知道蜂群/多跳已经查到过什么」。
    """
    call_llm_fn = call_llm_fn or _call_llm
    try:
        if not evidence:
            return ""
        # v56s · 喂证据放宽: 高可信优先排前·但也带普通证据(答案常在普通证据正文里)·
        #   每条 2500 字·总 22000。抽答用 opus(比longcat-2.0准·值得)。
        # v56x · 推理链末跳证据(multihop_evidence)排最前·它是"解开指代链后"搜到的·
        #   最可能含精确答案(Morarji题:先查出India再搜到India首相的证据)。
        _mh_ev = multihop_evidence or []
        _hi = [e for e in evidence if e.get("confidence") == "high_cross"
               or e.get("source") in ("tavily", "tavily_answer")]
        _lo = [e for e in evidence if e not in _hi]
        # v67 · best_effort(超时前兜底抽答)·把已收集的【全部】证据都喂进去·别只取前14条·
        #   别截断太狠(兜底常因证据被砍→答 UNKNOWN)。普通抽答仍 14 条/30000 字保时延。
        _n_items = 40 if best_effort else 14
        _ev_top = (_mh_ev + _hi + _lo)[:_n_items]  # 推理链证据+高可信排前
        # v61 · 序位题的「完整列表」证据(multihop_ordinal_fulllist)整份不截断到 2500·
        #   否则最早那一篇(常在列表尾部)被砍掉→抽不到(Pietro Murano 题真根因:gold 在
        #   第12300字·[:2500] 直接丢)。给全量列表 18000 字预算·普通证据仍 2500 保总长。
        # v67 · best_effort 时放宽每条与总长上限(证据全喂·别砍掉可能含答案的正文)。
        # v96i(2026-08-11) · 实证根因(米氏方程题):fetch_url_smart 抓到 32092 字符的
        #   论文全文，关键句"a better form of the Michaelis-Menten equation..."正好
        #   在第 27873 字处(接近文末)，18000 字预算仍然不够覆盖，被砍掉——抽答
        #   只能看到文章开头的历史背景，永远抽不到真正的公式。之前 v61 只给
        #   source=="multihop_ordinal_fulllist" 这一个来源开白名单还限 18000 字，
        #   属于"每撞一次新场景加一次头痛医头补丁"。改为通用判据+不设硬截断:
        #   任何被标记为"完整原文抓取"(fetch_url_smart/run_deep_research_swarm
        #   等抓到的完整网页/论文全文，而不是搜索引擎摘要片段)——不管它 source
        #   字段叫什么新名字，只要内容本身够长(单条超过 3000 字，说明是整页/
        #   整篇原文而非摘要)，就【不做单条截断】、直接把全文喂进去，让关键内容
        #   不论出现在文档哪个位置都不会被系统性丢弃。相应地把总预算上限也放宽，
        #   避免长文档进来后总长度反而被下面的 [:_total_cap] 砍掉。
        _FULLTEXT_SOURCES = {"multihop_ordinal_fulllist", "fetch_url_smart",
                              "run_deep_research_swarm"}
        _per_cap = 4000 if best_effort else 2500
        _total_cap = 90000 if best_effort else 60000
        def _ev_body(e):
            _src = e.get("source", "")
            _content = e.get("content") or e.get("title") or ""
            # 显式白名单来源，或内容本身够长(判定为完整原文而非摘要片段)，不做单条截断
            if _src in _FULLTEXT_SOURCES or len(_content) > 3000:
                return _content
            return _content[:_per_cap]
        _ev_txt = "\n\n".join(
            f"[{e.get('source','?')}] {_ev_body(e)}"
            for e in _ev_top)[:_total_cap]
        # v56v · 抽答回看目标卡(治「不回看目标机械执行」)·带上原题要什么+判定标准
        _gc = goal_card or {}
        _gc_hint = ""
        if _gc:
            _gc_hint = (f"\n【目标(必须对齐)】要的答案类型: {_gc.get('answer_type','')}"
                        f" · 核心实体(指代已解开): {_gc.get('key_entities','')}"
                        f" · 判定对错: {_gc.get('success_criteria','')}\n")
        _ms_hint = f"\n{mission_hint}\n" if mission_hint else ""
        # v62 · 序位抽答(语义化·治「关键词表换个说法就漏」)：不再靠关键词匹配·
        #   直接读 goal_card['ordinal_constraint']——它在 step0.25 已由 LLM 语义理解题意后
        #   产出「这题具体要哪个序位/极值项」的自然语言描述(如『时间最早的那一篇论文』
        #   『价格最高的那个』)·中英文/换说法(『学术生涯开端的作品』)都能抓到·关键词表抓不到。
        #   非空才拼序位指引:告诉抽答器要通读完整列表按序排、取指定项·别取最显眼的那个
        #   (Pietro Murano 题:证据全列表在·要取 2001 最早那篇非近期)。
        _ord_hint = ""
        try:
            _oc = str((_gc or {}).get("ordinal_constraint", "") or "").strip()
            if _oc:
                _ord_hint = ("\n【序位/极值要求·关键】本题要的是: 「" + _oc + "」。"
                             "证据里若有完整列表(如作者全部发表、全部候选项)·"
                             "注意列表可能按类别分组而非按目标序位排序·必须**通读全部条目**·"
                             "对全体逐一比较(时间序位比年份、极值比数值/价格)·找出满足上述约束的"
                             "**全局那一项**(如全局最早=最小年份、最贵=最大价格)·多项并列再按题目"
                             "其它条件挑·取其【完整名称/标题/值】·**别取最显眼、最近或排在最前的那个**。\n")
        except Exception:
            _ord_hint = ""
        # v65 · ID查询抽答(id_lookup 非空)：证据里已含各实体分别精准查到的专业编号
        #   (multihop_id_lookup)。提示抽答器:①对题目要的每个实体抽出其【ID类型】编号·
        #   ②严格按题目要求的顺序(如按化学名字母序)·③用题目指定分隔符(如分号)拼接输出。
        _id_hint = ""
        try:
            _idt = str((_gc or {}).get("id_lookup", "") or "").strip()
            if _idt:
                _id_hint = ("\n【专业ID抽答·关键】本题最终答案是相关实体的【" + _idt + "】。"
                            "步骤:①从证据里为题目涉及的【每个实体】分别抽出其 " + _idt + " 编号"
                            "(证据里 multihop_id_lookup 是对每个实体分别精准查的·编号常在其中)·"
                            "②严格按【题目要求的顺序】排列(如题目说按化学名字母序·就先字母序排实体再取其编号)·"
                            "③用【题目指定的分隔符】(如分号+空格 '; ')拼接所有编号输出·别加多余文字。"
                            "只有确实抽不到才 UNKNOWN。\n")
        except Exception:
            _id_hint = ""
        # v67 · best_effort 兜底抽答(超时前最后一次)·prompt 明确「尽最大努力·别轻易放弃」:
        #   兜底路径旧行为把有效率从100%拉到67%(用不完整证据轻易答 UNKNOWN)。改=喂全部证据
        #   +「这是超时前最后一次·基于目前所有证据尽力给最可能的精确答案·只有证据完全不支持
        #   才 UNKNOWN·别轻易放弃」。但守住底线:证据真没有仍 UNKNOWN·绝不编造。
        if best_effort:
            _fa_prompt = (
                "你是精确抽答器·这是【超时前最后一次抽答】。请基于下面【已收集的全部证据】"
                "尽最大努力给出问题最可能的精确答案(具体数字/日期/名称/人名)。\n"
                "要求:\n"
                "1. 答案可能埋在正文深处、多条证据交叉印证处·仔细通读所有证据再定。\n"
                "2. 若多条证据指向同一答案·或某条证据明确给出答案·就大胆抽出·别因为不是"
                "100%确定就放弃(这是最后机会·合理的最可能答案好过 UNKNOWN)。\n"
                "3. 严禁凭空编造:只有当【所有证据都完全不支持任何具体答案】时·才答 UNKNOWN。\n"
                "4. 严禁自行用公式推导出证据里没有的数(除非题目本就是给定数据的直接换算)。\n"
                "只输出答案本身·不要解释。\n"
                f"{_gc_hint}{_ms_hint}{_ord_hint}{_id_hint}\n"
                f"问题: {question}\n\n证据:\n{_ev_txt}\n\n最可能的精确答案:")
        else:
            _fa_prompt = (
                "你是精确抽答器。只从下面【证据】里抽出问题的精确答案(具体数字/日期/名称/人名)。"
                "严禁自行用公式推导·严禁编造·证据里有明确答案就原样抽出(即使埋在正文深处)。"
                "证据里确实没有才答 UNKNOWN。只输出答案本身·不要解释。\n"
                f"{_gc_hint}{_ms_hint}{_ord_hint}{_id_hint}\n"
                f"问题: {question}\n\n证据:\n{_ev_txt}\n\n精确答案:")
        _fa = call_llm_fn("opus", _fa_prompt, timeout=45)
        _fa_ans = (_fa.get("out") if isinstance(_fa, dict) else str(_fa)).strip()
        if _fa_ans and _fa_ans.upper() != "UNKNOWN":
            return _fa_ans[:200]
        return ""
    except Exception as _fae:
        if log:
            try:
                log(f"  [_factual_extract fail] {str(_fae)[:80]}")
            except Exception:
                pass
        return ""


def _deep_read_reextract(question, evidence, goal_card, call_llm_fn=None,
                         multihop_evidence=None, log=None, mission_hint="",
                         wall_left_fn=None, max_sources=2, min_wall_s=90):
    """v72 · 通用组件「抽不到 → 深读 top 源全文再抽」。

    治 GAIA 大量错题的【共性根因】(逐题分析 23 道仍错题得出):不是搜不到源·
    是答案埋在文档正文深处(剧情详述/章节正文/表格深处)·snippet(300-500字)级检索
    抽不到那个细节(如 Goldfinger 结尾 Bond 躲在橙白降落伞下·"Goldfinger hid color"
    的 snippet 根本不含 orange/white·细节在剧情详述深处)。

    现状缺口: 主链只对 content<300 字的 hits 深读补空·【已有 snippet 的高相关源不深读全文】。
    本组件补齐: 第一次抽答 UNKNOWN 时·挑 top 2-3 最相关(高可信/被多 agent 共命中/有 url)源·
    deep_read 全文(几千上万字)·截到 8000 字作新证据·再抽一次·prompt 强调「答案可能在
    正文深处·仔细通读定位·不是只看开头摘要」。抽到就用·抽不到落回原 UNKNOWN。

    这是【通用能力】·非特判任何题——对所有「源能定位但答案在文档正文深处」的题有效·真读真抽·不作弊。

    约束:
      - fail-safe: deep_read 异常/超时/无 url 一律不阻断·返 ""(落回原 UNKNOWN)。
      - 尊重 wall_budget: wall_left_fn() < min_wall_s 直接跳过(避免慢/被外层 timeout 硬 kill)。
      - 控量: 最多深读 max_sources(默认 3)个源。
    返回: 深读全文后重抽到的答案(字符串·[:200]) 或 ""(未抽到/余量不足/异常)。
    """
    call_llm_fn = call_llm_fn or _call_llm
    def _log(m):
        if log:
            try:
                log(m)
            except Exception:
                pass
    try:
        if not evidence:
            return ""
        # 尊重 wall_budget: 余量不足别深读(深读全文 + 再抽 opus 至少要 40-60s)
        if wall_left_fn is not None:
            try:
                _wl = wall_left_fn()
                if _wl < min_wall_s:
                    _log(f"  [deep_read_reextract] skip · wall_left={_wl:.1f}s < {min_wall_s}s")
                    return ""
            except Exception:
                pass  # 计时异常不阻断(视为有余量)
        # 挑 top 最相关且有 url 的源(推理链末跳证据 + 高可信 + 被多 agent 共命中优先·
        #   这些最可能真含精确答案)。同 url 去重。
        _mh_ev = multihop_evidence or []
        _hi = [e for e in evidence if e.get("confidence") == "high_cross"
               or e.get("source") in ("tavily", "tavily_answer")]
        _lo = [e for e in evidence if e not in _hi]
        _ranked = _mh_ev + _hi + _lo
        _seen_urls = set()
        _picked = []  # [(url, evidence_item)]
        for _e in _ranked:
            if not isinstance(_e, dict):
                continue
            _u = _e.get("url")
            if not _u or _u in _seen_urls:
                continue
            # 已经是深读全文(content 很长)的源没必要再深读一次
            if _e.get("completeness") == "full" and len(_e.get("content", "")) >= 6000:
                continue
            _seen_urls.add(_u)
            _picked.append((_u, _e))
            if len(_picked) >= max_sources:
                break
        if not _picked:
            _log("  [deep_read_reextract] skip · 无可深读的 url 源")
            return ""
        _urls = [u for u, _ in _picked]
        _log(f"  [deep_read_reextract] 抽不到 → 深读 top{len(_urls)} 源全文再抽: "
             + " | ".join(u[:70] for u in _urls))
        # 真深读全文(PDF/HTML)·并发·fail-safe
        # v13.94(2026-08-12) · 优先走 search_fabric_fetch L2 梯度(httpx→curl_cffi)
        #   原 SearchAgent.deep_read_batch → web_access_bridge → CDP 重，容易超时
        #   新路径: L2 更轻量·合规·快·失败率低·SearchAgent 作 fallback 保留
        _full_by_url = {}
        import concurrent.futures as _cf
        _dr_budget = 100
        if wall_left_fn is not None:
            try:
                _dr_budget = max(30, min(100, wall_left_fn() - 30))
            except Exception:
                pass
        try:
            if V11_EVOLUTION not in sys.path:
                sys.path.insert(0, V11_EVOLUTION)
            from search_fabric_fetch import scrape_and_extract_batch
            _pairs = [(_u, question) for _u in _urls]
            with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
                _fut = _ex.submit(scrape_and_extract_batch, _pairs,
                                  6000, min(3, len(_urls)), 2)
                try:
                    _fab_results = _fut.result(timeout=_dr_budget)
                except _cf.TimeoutError:
                    _log(f"  [deep_read_reextract] fabric 深读超时({_dr_budget}s)·退 SearchAgent")
                    _fab_results = []
            # 只收 ok 的；不够的 fallback 到 SearchAgent
            _ok_urls = set()
            for _u, _r in zip(_urls, _fab_results or []):
                if isinstance(_r, dict) and _r.get("ok") and _r.get("content"):
                    _full_by_url[_u] = _r["content"]
                    _ok_urls.add(_u)
            _missing = [u for u in _urls if u not in _ok_urls]
            if _missing:
                _log(f"  [deep_read_reextract] fabric 深读 {len(_ok_urls)}/{len(_urls)} ok·{len(_missing)} 退 SearchAgent")
                from search_agent import SearchAgent
                _sa = SearchAgent()
                with _cf.ThreadPoolExecutor(max_workers=1) as _ex2:
                    _fut2 = _ex2.submit(_sa.deep_read_batch, _missing, min(2, len(_missing)))
                    try:
                        _fallback_results = _fut2.result(timeout=max(20, _dr_budget - 20))
                    except _cf.TimeoutError:
                        _fallback_results = []
                for _r2 in (_fallback_results or []):
                    if isinstance(_r2, dict) and _r2.get("ok") and _r2.get("content") and _r2.get("url"):
                        _full_by_url[_r2["url"]] = _r2["content"]
            _log(f"  [deep_read_reextract] 最终深读成功 {len(_full_by_url)}/{len(_urls)} 篇")
        except Exception as _dre:
            _log(f"  [deep_read_reextract] deep_read fail(不阻断): {str(_dre)[:80]}")
            return ""
        if not _full_by_url:
            _log("  [deep_read_reextract] 深读 0 篇成功(死链/超时)·落回原 UNKNOWN")
            return ""
        # 组装深读全文作新证据(每篇截 8000 字·答案在正文深处·别砍太狠)
        _deep_ev = []
        for _u, _e in _picked:
            _content = _full_by_url.get(_u)
            if not _content:
                continue
            _deep_ev.append({
                "source": "deep_read_fulltext",
                "url": _u,
                "title": _e.get("title", ""),
                "content": _content[:8000],
            })
        if not _deep_ev:
            return ""
        _log(f"  [deep_read_reextract] 深读成功 {len(_deep_ev)} 篇·全文再抽(强调通读正文深处)")
        # 拼证据文本(深读全文排最前)
        _ev_txt = "\n\n".join(
            f"[全文·{(e.get('url') or '')[:80]}] {e.get('content', '')}"
            for e in _deep_ev)[:30000]
        _gc = goal_card or {}
        _gc_hint = ""
        if _gc:
            _gc_hint = (f"\n【目标(必须对齐)】要的答案类型: {_gc.get('answer_type','')}"
                        f" · 核心实体(指代已解开): {_gc.get('key_entities','')}"
                        f" · 判定对错: {_gc.get('success_criteria','')}\n")
        _ms_hint = f"\n{mission_hint}\n" if mission_hint else ""
        _prompt = (
            "你是精确抽答器。下面是几篇【深读到的源文档全文】(不是搜索摘要·是完整正文)。\n"
            "关键: **答案很可能不在开头摘要·而是埋在正文深处**(剧情详述/章节正文/表格/脚注里)。"
            "请**仔细通读全文**再定位那个精确答案(具体数字/日期/名称/人名/颜色等)·"
            "不要只看开头几句就下结论。\n"
            "严禁自行用公式推导·严禁编造·全文里确实没有才答 UNKNOWN。只输出答案本身·不要解释。\n"
            f"{_gc_hint}{_ms_hint}\n"
            f"问题: {question}\n\n源文档全文:\n{_ev_txt}\n\n精确答案:")
        _r = call_llm_fn("opus", _prompt, timeout=45)
        _ans = (_r.get("out") if isinstance(_r, dict) else str(_r)).strip()
        if _ans and _ans.upper() != "UNKNOWN":
            _log(f"  [deep_read_reextract] 深读全文后抽到: {_ans[:80]}")
            return _ans[:200]
        _log("  [deep_read_reextract] 深读全文后仍 UNKNOWN·落回原")
        return ""
    except Exception as _e:
        _log(f"  [deep_read_reextract] fail(不阻断): {str(_e)[:80]}")
        return ""


def _direct_answer(question, evidence, goal_card, call_llm_fn=None, log=None, mission_hint=""):
    """v57 · 第0级「内置知识直答」·让觉游在纯知识题上追平裸CC速度(实测裸CC快=没搜·纯
    模型内置知识直答)。opus 用内置知识 + 已有 step2 证据一次性直答·只有高置信精确答案
    才返回·否则答 DIRECT_UNKNOWN(交第1级证据抽答)。返回答案字符串 或 ""。
    与第1级 _factual_extract 区别: 允许用模型内置知识(非只抽证据)·但不确定必须让路·
    绝不编造(不确定=DIRECT_UNKNOWN·下游用检索证据兜)。
    v59 · mission_hint 非空时说明已登记过未解决的信息缺口(如多跳题指代链未解开)·
    提醒模型更谨慎(靠内置知识直答不可靠·应让路给检索)。
    """
    call_llm_fn = call_llm_fn or _call_llm
    try:
        _gc = goal_card or {}
        _gc_hint = ""
        if _gc:
            _gc_hint = (f"\n【目标(必须对齐)】要的答案类型: {_gc.get('answer_type','')}"
                        f" · 核心实体: {_gc.get('key_entities','')}"
                        f" · 判定对错: {_gc.get('success_criteria','')}\n")
        if mission_hint:
            _gc_hint += (f"\n【已知专题仍有未解决的信息缺口】若你的直答依赖这些缺口里的具体信息·"
                         "宁可答 DIRECT_UNKNOWN 让路给检索·别凭内置知识硬猜:\n" + mission_hint + "\n")
        # 附少量已有证据供参考(不强制·内置知识为主)·前6条·每条1200字
        _ev_txt = ""
        if evidence:
            _hi = [e for e in evidence if e.get("confidence") == "high_cross"
                   or e.get("source") in ("tavily", "tavily_answer")]
            _lo = [e for e in evidence if e not in _hi]
            _ev_top = (_hi + _lo)[:6]
            _ev_txt = "\n\n".join(
                f"[{e.get('source','?')}] {(e.get('content') or e.get('title') or '')[:1200]}"
                for e in _ev_top)[:7000]
        _da_prompt = (
            "先自检:这道题的答案·是不是【冷门的具体事实】(如某篇具体论文的作者/标题、"
            "某个具体统计数字、某年某机构的具体数据)——这类事实你【无法凭记忆确定】·极易编造。"
            "若是这类·或你对答案有任何不确定·【立即输出 DIRECT_UNKNOWN】·绝不猜。"
            "只有当答案是【广为人知的常识事实】(如元素符号、著名历史事件)且你【百分百确定】·才直答。\n\n"
            "你是快速直答器。若你**高度确信**知道下面问题的精确答案(具体数字/日期/名称/人名)·"
            "直接输出答案本身(不要解释)。可参考下方证据但主要用你的知识。\n"
            "关键: 只要有一丝不确定·或需要最新/时效信息·或答案可能已变·就只输出 DIRECT_UNKNOWN"
            "(交给检索环节)·**绝不编造·绝不猜**。\n"
            # v72补(2026-08-06)·完整性自检【通用·非针对某题】: 实证根因(Goldfinger降落伞题)=\n
            #   问题要「所有颜色」(橙+白两顶)·内置知识只记得一个(white)就高置信直答·漏掉另一个→\n
            #   部分正确但不完整。修=多值/列表/穷举类题·除非能确信穷举了全部项·否则让路给检索补全。\n
            "【完整性自检·重要】: 若问题要的是一个【列表 / 多个值 / 所有满足条件的X】"
            "(如「有哪些颜色」「列出全部」「几个名字」「哪些成员」)·你的内置记忆很可能【只记得其中一部分】·"
            "漏掉的项会让答案不完整。除非你能【确信已穷举全部项】·否则一律输出 DIRECT_UNKNOWN 交检索穷举·"
            "别用不完整的记忆直答。\n"
            f"{_gc_hint}\n"
            f"问题: {question}\n\n"
            + (f"参考证据(可选):\n{_ev_txt}\n\n" if _ev_txt else "")
            + "答案(精确答案 或 DIRECT_UNKNOWN):")
        _da = call_llm_fn("opus", _da_prompt, timeout=30)
        _da_ans = (_da.get("out") if isinstance(_da, dict) else str(_da)).strip()
        if _da_ans and "DIRECT_UNKNOWN" not in _da_ans.upper() \
                and _da_ans.upper() != "UNKNOWN":
            return _da_ans[:200]
        return ""
    except Exception as _dae:
        if log:
            try:
                log(f"  [_direct_answer fail] {str(_dae)[:80]}")
            except Exception:
                pass
        return ""



# (2026-08-27 瘦身) 旧 orchestrate 固定8-Step pipeline + CLI入口已删除。
# auto 已改用 NativeEngine；军团不调用此流水线；保留本文件仅作为 step/tool 实现库，
# 供 qa_tool_impls/cross_verify 等复用。
