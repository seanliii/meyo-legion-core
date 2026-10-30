#!/usr/bin/env python3
"""process_coverage · 2026-08-18 · 记忆轨道-过程覆盖验证(双轨制第三层·bc 专用)

击穿本质(KM 2781429277·"任务感知型分层验证引擎"):
  GAIA 98% 但 BrowseComp 崩·根本【不是验证器不够好】·而是【用验证"推理/工具"的
  裁判去验证"记忆/搜索"的质量】——像用数学考试标准评判文献综述。

  · 推理↔工具(过程正确性): GAIA 这类·验【答案准确性】(逻辑/计算/工具链)。
    → 已有推理轨道: cross_verify(多模型交叉) + identity_coverage(四态/候选判别器/
      claim 覆盖率)。**本模块不碰它·别重造。**
  · 记忆↔搜索(信息覆盖度): bc 这类·验【搜索轨迹覆盖度】非答案对错——找到足够多、
    足够分散的信息了吗?纠缠点(既是 X 又是 Y 的 Z)每个条件都触及证据了吗?
    → **本模块 = 记忆轨道**·全新。

本模块只验【搜索轨迹质量】·从不碰 gold、从不判答案对错(那是推理轨道的活):

  1) 轨迹 grounding(trajectory grounding):
     每步搜索 query 的关键实体是否来自【前一步检索结果】(证据驱动·像人顺藤摸瓜)
     ·而非模型内部知识【裸猜】(假设驱动)。假设驱动 query 比例 > 阈值 → 告警
     ("可能在用已知知识验证·而非真发现")。

  2) 纠缠点覆盖(entanglement coverage):
     题面多个【必须同时满足】的约束(找既是 X 又是 Y 的 Z)·检查搜索轨迹是否
     分别触及每个约束的证据·未触及标 uncovered。复用 identity_coverage 的
     claim 拆解 + compute_coverage(轨迹证据 vs 约束)。

  3) 过早终止检测(premature termination):
     覆盖型任务·agent 找到第一个看似合理的答案就停? coverage_score < 阈值时
     触发【继续搜索】信号(非交卷)——记忆瓶颈用"继续搜"解·不是用推理裁判判对错。

接入(协调层·短板决定上限):
  覆盖型任务 finalize 前·记忆轨道验证不通过(覆盖不足)→ 触发继续搜/补证据·
  【而非】用推理裁判判答案对错。与推理轨道取 min 置信度: 信息没找全(记忆瓶颈)→
  继续搜; 答案与证据矛盾(推理瓶颈)→ 重生成(推理轨道的活)。

全部 fail-safe: 任何异常都不崩 run、不拦交卷(返回放行/空)。env MEYO_MEMORY_TRACK
默认开·=0 退回(覆盖型任务也只走推理轨道·完全等价旧行为)。不针对具体题·
不透 gold(引擎内根本没有 gold·只对搜索轨迹↔约束做覆盖判定)。
"""
import os
import re

# 覆盖分阈值(通用默认·非题目特判)
COVERAGE_CONTINUE = 0.60      # 过程覆盖 < 此门 → 覆盖型任务建议继续搜(非交卷)
HYPOTHESIS_WARN_RATIO = 0.30  # 假设驱动 query 比例 > 此门 → 告警(可能裸猜验证)

# 判"这是一次搜索/检索类工具调用"的工具名前缀/关键词(通用·非写死具体工具)
_SEARCH_TOOL_HINTS = (
    "search", "query", "fetch", "recall", "traverse", "browse", "lookup",
    "scholar", "pubmed", "wiki", "crossref", "openalex", "wikidata",
    "web", "google", "retrieve", "find",
)
# 空手/无结果的迹象(用于过早终止/无进展判定·与主链 _CHANNEL_EMPTY_MARKERS 同族)
_EMPTY_MARKERS = (
    "没搜到", "无结果", "not found", "no result", "0 result", "空",
    "未找到", "no match", "nothing found", "查无", "失败", "超时", "timeout",
    # ★真实生产标记(实证漏接): run_parallel_search 空搜返回"新增 0 条证据"·
    #   fetch 空/工具错误串——不补进来 cold_start/premature 的空手判定就抓不到真空搜。
    "新增 0 条", "新增0条", "累计 0 条", "[工具", "抓取未完成", "无新证据",
)


def _enabled():
    """env MEYO_MEMORY_TRACK 默认开·=0 退回(fail-safe·完全等价旧行为·只走推理轨道)。"""
    return os.environ.get("MEYO_MEMORY_TRACK", "1").strip().lower() \
        not in ("0", "false", "off", "no")


# ─────────────────────────────────────────────────────────────────────────
# 从 tool_log 抽【搜索轨迹】: 每步 (query 文本, 结果摘要, 是否空手)。
# ─────────────────────────────────────────────────────────────────────────
def extract_search_trajectory(session):
    """从 session.tool_log 抽出【搜索步序列】(时间序)。

    每步 {"step":i, "tool":名, "query":query文本, "summary":结果摘要,
          "empty":bool 空手, "entities":该 query 里的关键实体 token}。
    只保留检索类工具(名字含 search/fetch/query/... 或 args 里有 query/q/question)。
    fail-safe → []。"""
    traj = []
    try:
        log = (session or {}).get("tool_log") or []
        for i, it in enumerate(log):
            if not isinstance(it, dict):
                continue
            name = str(it.get("tool") or it.get("name") or "").lower()
            args = it.get("args") if isinstance(it.get("args"), dict) else {}
            # query 文本: args 里 query/q/question/url·退回工具名
            qtext = ""
            for k in ("query", "q", "question", "url", "file"):
                v = args.get(k)
                if v and str(v).strip():
                    qtext = str(v).strip()
                    break
            # 是不是检索类
            is_search = (any(h in name for h in _SEARCH_TOOL_HINTS)
                         or bool(qtext))
            if not is_search:
                continue
            summ = str(it.get("summary") or "")
            empty = any(m in summ for m in _EMPTY_MARKERS) and len(summ) < 400
            traj.append({
                "step": i, "tool": name, "query": qtext[:300],
                "summary": summ[:2000], "empty": bool(empty),
                "entities": _salient_tokens(qtext),
            })
    except Exception:
        pass
    return traj


_STOP = frozenset((
    "the", "and", "was", "were", "who", "that", "which", "with", "for", "from",
    "what", "when", "where", "how", "why", "did", "does", "his", "her", "their",
    "this", "these", "those", "into", "onto", "over", "under", "about", "http",
    "https", "www", "com", "org", "net", "search", "find", "list", "name",
))


def _salient_tokens(text):
    """从一段 query/证据抽【显著 token】(专名词/多字母词/数字/引号短语)——
    grounding 判定的锚。通用启发·非题目特判。返回小写 set。fail-safe → set()。"""
    toks = set()
    try:
        t = str(text or "")
        # 引号短语整体入锚
        for q in re.findall(r'"([^"]{2,60})"', t) + re.findall(r"'([^']{2,60})'", t):
            toks.add(q.lower().strip())
        # 显著数字(年份/编号)
        for m in re.findall(r"\d{2,}", t):
            toks.add(m.lower())
        # 首字母大写专名(单词及连续专名短语)
        for m in re.findall(r"\b([A-Z][a-zA-Z]{2,}(?:\s+[A-Z][a-zA-Z]{2,}){0,3})\b", t):
            ml = m.lower().strip()
            if ml and ml not in _STOP:
                toks.add(ml)
                for w in ml.split():
                    if len(w) >= 4 and w not in _STOP:
                        toks.add(w)
        # 普通长词兜底(query 常无大写·如全小写搜索词)
        for w in re.findall(r"[a-z][a-z\-]{4,}", t.lower()):
            if w not in _STOP:
                toks.add(w)
    except Exception:
        pass
    return {x for x in toks if x}


# ─────────────────────────────────────────────────────────────────────────
# 机制1: 轨迹 grounding —— query 关键实体是否来自前一步检索结果(证据驱动)?
# ─────────────────────────────────────────────────────────────────────────
def grounding_analysis(trajectory):
    """判每步 query 是【证据驱动(grounded)】还是【假设驱动(裸猜)】。

    grounded: 该 query 的显著 token 至少一个出现在【此前某步的结果摘要】里
      (顺藤摸瓜·像人从上一条线索接着查)。
    hypothesis(裸猜): query 的显著 token 全都没在此前任何结果里出现过——要么
      来自题面(第一步·正常)·要么来自模型内部知识(裸猜验证·可疑)。

    第一步永远算 grounded(题面驱动·正常起点)。返回:
      {"total":n, "grounded":n, "hypothesis":n, "hypothesis_ratio":float,
       "warn":bool, "per_step":[{step,query,kind,matched}]}。
    warn = 假设驱动比例 > HYPOTHESIS_WARN_RATIO(且轨迹够长)。fail-safe → 空统计。"""
    out = {"total": 0, "grounded": 0, "hypothesis": 0, "hypothesis_ratio": 0.0,
           "warn": False, "per_step": []}
    try:
        traj = [t for t in (trajectory or []) if t.get("query")]
        out["total"] = len(traj)
        if not traj:
            return out
        # 累积的"已见证据 token 池"(前面所有步的结果摘要里的显著 token)
        seen_pool = set()
        for idx, step in enumerate(traj):
            q_toks = step.get("entities") or set()
            if idx == 0:
                # 第一步: 题面驱动·正常起点
                kind, matched = "grounded", []
                out["grounded"] += 1
            elif not q_toks:
                # query 没有显著实体(如纯功能词/翻页)——不计入 grounding 判定
                kind, matched = "neutral", []
            else:
                matched = sorted(t for t in q_toks if t in seen_pool)[:5]
                if matched:
                    kind = "grounded"
                    out["grounded"] += 1
                else:
                    kind = "hypothesis"
                    out["hypothesis"] += 1
            out["per_step"].append({"step": step.get("step"),
                                    "query": step.get("query", "")[:80],
                                    "kind": kind, "matched": matched})
            # 把本步结果摘要的 token 并进池(供后续步 grounding 判定)
            seen_pool |= _salient_tokens(step.get("summary", ""))
        judged = out["grounded"] + out["hypothesis"]
        if judged >= 3:  # 轨迹够长才判(2 步内噪声大·不告警)
            out["hypothesis_ratio"] = out["hypothesis"] / float(judged)
            out["warn"] = out["hypothesis_ratio"] > HYPOTHESIS_WARN_RATIO
        else:
            out["hypothesis_ratio"] = (out["hypothesis"] / float(judged)
                                       if judged else 0.0)
    except Exception:
        pass
    return out


# ─────────────────────────────────────────────────────────────────────────
# 机制1b: 确认锁定 —— 是否在同一个候选实体上连续做确认性搜索(缺竞争假设)?
# ─────────────────────────────────────────────────────────────────────────
CONFIRM_LOCK_STREAK = 4  # 同一主导实体连续出现于 ≥N 步 query → 判确认锁定

def confirmation_lock_analysis(trajectory):
    """检测【单假设确认偏误】:轨迹里同一个主导实体 token 连续出现在多步 query,
    像人认定一个答案后只反复找证据佐证它、不去检验竞争假设。

    2026-08-24 跨框架 trace 根因:六家全灭的 Minh 题里,deepseek 从第 4 步起
    连续 11 次 query 都锁 "Lil Mosey"(高知名度但错的候选),gold 从未进入搜索
    空间。这是候选选择/裁决问题,不是召回问题。本检测通用地识别该模式:不硬编码
    任何实体,只看"同一显著 token 是否在连续 ≥CONFIRM_LOCK_STREAK 步 query 中主导"。

    返回 {"locked":bool, "entity":str, "streak":int, "steps":[...],
          "advice":str}。fail-safe → 未锁定。"""
    out = {"locked": False, "entity": "", "streak": 0, "steps": [], "advice": ""}
    try:
        traj = [t for t in (trajectory or []) if t.get("entities")]
        if len(traj) < CONFIRM_LOCK_STREAK:
            return out
        # 候选实体 ≠ 题面约束词,也 ≠ 通用搜索动作词(biography/detail/interview…)。
        # 排除标准:①出现在题面(首步 query)的词是约束/描述词,不是新引入的候选;
        # ②通用检索脚手架词跨题高频、非实体。只有"轨迹中途新引入并被反复确认的
        # 专名型 token"才构成单假设锁定。全部通用,不硬编码任何实体。
        question_toks = set(traj[0].get("entities") or set()) if traj else set()
        _SCAFFOLD = {"biography", "detail", "details", "interview", "profile",
                     "wiki", "wikipedia", "official", "career", "history",
                     "record", "records", "source", "sources", "article",
                     "verify", "check", "confirm", "search", "result", "results"}
        from collections import Counter
        freq = Counter()
        for t in traj:
            for e in t["entities"]:
                freq[e] += 1
        best_ent, best_run, best_steps = "", 0, []
        for ent, cnt in freq.items():
            if cnt < CONFIRM_LOCK_STREAK:
                continue
            if ent in question_toks or ent in _SCAFFOLD or ent in _STOP:
                continue  # 题面约束词/脚手架词/停用词不算候选锁定
            run, run_steps = 0, []
            cur, cur_steps = 0, []
            for t in traj:
                if ent in t["entities"]:
                    cur += 1
                    cur_steps.append(t.get("step"))
                    if cur > run:
                        run, run_steps = cur, list(cur_steps)
                else:
                    cur, cur_steps = 0, []
            if run > best_run:
                best_ent, best_run, best_steps = ent, run, run_steps
        if best_run >= CONFIRM_LOCK_STREAK:
            out.update(locked=True, entity=best_ent, streak=best_run,
                       steps=best_steps[:12],
                       advice=("已在候选『%s』上连续 %d 步做确认性搜索却未收敛。"
                               "高知名度候选常是陷阱:请生成 ≥1 个竞争假设并主动"
                               "证伪当前候选,而不是继续找它的佐证。"
                               % (best_ent, best_run)))
    except Exception:
        pass
    return out


# ─────────────────────────────────────────────────────────────────────────
# 机制2: 纠缠点覆盖 —— 每个"必须同时满足"的约束·搜索轨迹分别触及证据了吗?
# ─────────────────────────────────────────────────────────────────────────
def entanglement_coverage(claims, trajectory, evidence=None):
    """题面多约束(找既是 X 又是 Y 的 Z)·检查搜索轨迹是否分别触及每个约束的证据。

    与 identity_coverage.compute_coverage 的区别(不重复):
      · compute_coverage 验【候选答案对每条约束是否有证据】(答案维度·推理轨道用)。
      · 本函数验【搜索轨迹整体是否触及每条约束的证据】(过程维度·不看某个候选)——
        即使还没锁定候选·也能判"你搜了 X 的证据、搜了 Y 的证据了吗"。

    判定: 把轨迹里所有 query+结果摘要 + evidence 合成一个"已探索文本池"·对每条
    约束的锚 token 是否在池里出现。覆盖=触及过。返回:
      {"total":n, "covered":n, "coverage":float,
       "per_claim":[{id,text,covered,where}], "uncovered":[text,...]}。
    fail-safe → 空统计(coverage=0·由调用方决定放不放行)。"""
    out = {"total": 0, "covered": 0, "coverage": 0.0,
           "per_claim": [], "uncovered": []}
    try:
        claims = claims or []
        out["total"] = len(claims)
        if not claims:
            return out
        # 已探索文本池: 每步 query + 结果摘要(整个搜索过程触及的文本)
        pool_blocks = []
        for t in (trajectory or []):
            blk = (str(t.get("query") or "") + " \n " + str(t.get("summary") or "")).lower()
            if blk.strip():
                pool_blocks.append(blk)
        # evidence 也算探索到的信息(有些证据来自 fetch 正文·不在 query 里)
        for e in (evidence or []):
            if isinstance(e, dict):
                blk = str(e.get("text") or e.get("content") or "").lower()
                if blk.strip():
                    pool_blocks.append(blk[:4000])
        for c in claims:
            toks = c.get("tokens") or []
            where = ""
            covered = False
            if toks:
                for bi, blk in enumerate(pool_blocks):
                    if any(t in blk for t in toks):
                        covered = True
                        where = "探索步#%d" % bi
                        break
            rec = {"id": c.get("id"), "text": c.get("text"),
                   "covered": covered, "where": where}
            out["per_claim"].append(rec)
            if covered:
                out["covered"] += 1
            else:
                out["uncovered"].append(c.get("text"))
        out["coverage"] = (out["covered"] / float(out["total"])
                           if out["total"] else 0.0)
    except Exception:
        pass
    return out


# 效率门槛(可 env 调): 搜索步数超过这个数才评估"撒网"(短轨迹不冤枉)。
_EFFICIENCY_MIN_STEPS = 6


def _search_efficiency(grounding, entanglement):
    """★把指导思想"搜索次数≠推理质量"落成可执行信号(KM 2781429277)。

    诊断"撒网": 搜索步数多(记忆维度投入大)·但 grounded 比例低(推理链没顺着证据
    推进·在裸猜/各搜各的)·且纠缠点没覆盖全 → 【继续加搜索次数无益·应换向】。

    返回 {"steps":n, "grounded_ratio":float, "casting_net":bool, "verdict":str}。
      casting_net=True → 撒网态: 建议换向(顺藤摸瓜/换渠道/用已有证据推理)而非同层再搜。
    fail-safe → 非撒网(不干扰)。"""
    out = {"steps": 0, "grounded_ratio": 1.0, "casting_net": False,
           "verdict": "ok"}
    try:
        steps = int((grounding or {}).get("total") or 0)
        out["steps"] = steps
        judged = int((grounding or {}).get("grounded") or 0) + \
            int((grounding or {}).get("hypothesis") or 0)
        gratio = ((grounding or {}).get("grounded") or 0) / float(judged) \
            if judged else 1.0
        out["grounded_ratio"] = round(gratio, 3)
        cov = float((entanglement or {}).get("coverage") or 0.0)
        import os as _os
        min_steps = int(_os.environ.get("MEYO_EFFICIENCY_MIN_STEPS",
                                        str(_EFFICIENCY_MIN_STEPS))
                        or _EFFICIENCY_MIN_STEPS)
        # 撒网判据: 步数够多 + grounded 比例低(<0.5·多数 query 在裸猜非顺藤摸瓜)
        #   + 覆盖仍不足(<0.6)。三者同时=典型"搜得多却没找对"。
        if steps >= min_steps and gratio < 0.5 and cov < 0.6:
            out["casting_net"] = True
            out["verdict"] = ("撒网(%d步·grounded仅%d%%·覆盖%d%%): 增加搜索次数无益·"
                              "应换向(顺藤摸瓜/换渠道/用已有证据推理)非同层再搜"
                              % (steps, int(gratio * 100), int(cov * 100)))
    except Exception:
        pass
    return out


# ─────────────────────────────────────────────────────────────────────────
# 机制3: 过早终止检测 —— 找到第一个看似合理答案就停?
# ─────────────────────────────────────────────────────────────────────────
def premature_termination(entang, grounding, trajectory):
    """判是否【过早终止】(覆盖型任务·证据没找全就想交卷)。通用·fail-safe → False。

    信号(任一命中即算过早·给调用方"继续搜"建议):
      · 纠缠点覆盖 < COVERAGE_CONTINUE(有约束的证据根本没触及);
      · 搜索步数很少(< 有效约束数·平均每约束还不到一步)就想交;
      · 最近若干步全空手 —— 那不是"找够了停"·是"撞墙了停"(该换渠道·非交卷)。
    返回 {"premature":bool, "reasons":[...], "coverage":float}。"""
    out = {"premature": False, "reasons": [], "coverage": 0.0}
    try:
        cov = float((entang or {}).get("coverage") or 0.0)
        out["coverage"] = cov
        total = int((entang or {}).get("total") or 0)
        n_steps = len([t for t in (trajectory or []) if t.get("query")])
        reasons = []
        if total and cov < COVERAGE_CONTINUE:
            reasons.append("纠缠点覆盖不足(%.0f%%·%d/%d 约束触及证据)"
                           % (100 * cov, int((entang or {}).get("covered") or 0), total))
        # 平均每约束还不到一步搜索就想交(约束多但搜得浅)
        if total >= 3 and n_steps and n_steps < total:
            reasons.append("搜索步数(%d)少于约束数(%d)·多约束题探索过浅" % (n_steps, total))
        # 最近步全空手 = 撞墙非找够
        recent = [t for t in (trajectory or []) if t.get("query")][-3:]
        if len(recent) >= 3 and all(t.get("empty") for t in recent):
            reasons.append("最近 3 步搜索全空手·是撞墙非找够(该换渠道·非交卷)")
        out["reasons"] = reasons
        out["premature"] = bool(reasons)
    except Exception:
        pass
    return out


def cold_start_stall(trajectory, *, has_anchor=False):
    """★冷启动撞墙检测(治 B 类"纯描述无专名"题·真数据实证:高特异事件描述
    在免费搜索源反查不到)。区别于 _search_efficiency(那检测"搜多步后撒网"·门槛高)——
    这检测【最开始几步就连续空返回且还没拿到任何锚点实体】:此时继续换描述性 query 变体
    = 撒网(有害·指导思想)。应换向: 换结构化/属性检索源·或换"最特异单一特征"角度·
    或诚实标存疑。通用启发·fail-safe → 非撞墙(不干扰)。

    判据(同时满足):
      · 已搜 >= COLD_START_MIN 步(默认 4·避免第一步就误判);
      · 其中空返回(empty)占比 >= 0.6(多数在空转);
      · has_anchor=False(还没确证任何实体锚点·anchor_hop 无从顺藤)。
    返回 {"stalled":bool, "empty_ratio":float, "steps":int, "advice":str}。"""
    import os as _os
    out = {"stalled": False, "empty_ratio": 0.0, "steps": 0, "advice": ""}
    try:
        _min = int(_os.environ.get("MEYO_COLD_START_MIN", "4") or 4)
        steps = [t for t in (trajectory or []) if t.get("query")]
        n = len(steps)
        out["steps"] = n
        if n < _min or has_anchor:
            return out
        empty = sum(1 for t in steps if t.get("empty"))
        ratio = empty / float(n) if n else 0.0
        out["empty_ratio"] = round(ratio, 3)
        if ratio >= 0.6:
            out["stalled"] = True
            out["advice"] = (
                "[冷启动撞墙] 已搜 %d 步·%d%% 空返回·且还没确证任何实体锚点。"
                "继续换描述性 query 变体=撒网(无益)。换向: ①提取题面【最特异的单一特征】"
                "(精确数字/专有名/引号短语)单独作 query·别整句描述堆砌;②这类"
                "'纯特征无专名'题免费搜索源常覆盖不到·考虑换【结构化/属性检索源】"
                "(专门数据库按属性查)或换语言;③若确实无从锚定·诚实标存疑而非硬猜。"
                % (n, int(ratio * 100)))
    except Exception:
        pass
    return out


# ─────────────────────────────────────────────────────────────────────────
# 编排入口: 记忆轨道过程覆盖验证(覆盖型任务 finalize 前调)。
# ─────────────────────────────────────────────────────────────────────────
def verify_process_coverage(session, question, *, claims=None, triage=None,
                            llm_call=None):
    """记忆轨道验证(覆盖型任务专用)。验搜索轨迹质量·非答案对错。

    返回 dict:
      {"enabled":bool, "coverage_score":float, "confidence":float,
       "grounding": <grounding_analysis>, "entanglement": <entanglement_coverage>,
       "premature": <premature_termination>,
       "should_continue_search":bool,   # True=覆盖不足·建议继续搜(非交卷)
       "advice":str}                     # 给 agent 的"继续搜什么"提示(不透答案)
    coverage_score = 纠缠点覆盖率(记忆轨道的核心置信度·信息覆盖度)。
    confidence 同 coverage_score(供协调层取 min 用)。
    fail-safe: 关闭/异常/拆不出约束 → should_continue_search=False(放行·退回推理轨道)。
    绝不透 gold: 只对搜索轨迹↔约束做覆盖判定·从不判候选实体谁对。"""
    res = {"enabled": False, "coverage_score": 1.0, "confidence": 1.0,
           "grounding": {}, "entanglement": {}, "premature": {},
           "should_continue_search": False, "advice": ""}
    try:
        if not _enabled():
            return res
        res["enabled"] = True
        # 拆约束: 复用 identity_coverage(题面语义驱动·triage 优先·LLM 兜底)
        if claims is None:
            try:
                import identity_coverage as _ic
                claims = _ic.extract_claims(question, existing_triage=triage,
                                            llm_call=llm_call)
            except Exception:
                claims = []
        if not claims:
            return res  # 拆不出约束(纯开放题) → 不介入·放行
        traj = extract_search_trajectory(session)
        # 证据(补 fetch 正文等不在 query 里的信息)
        evidence = []
        try:
            import identity_coverage as _ic
            evidence = _ic.collect_evidence(session)
        except Exception:
            evidence = (session or {}).get("evidence") or []
        gr = grounding_analysis(traj)
        en = entanglement_coverage(claims, traj, evidence)
        pm = premature_termination(en, gr, traj)
        cl = confirmation_lock_analysis(traj)
        res["grounding"] = gr
        res["entanglement"] = en
        res["premature"] = pm
        res["confirmation_lock"] = cl
        res["coverage_score"] = float(en.get("coverage") or 0.0)
        res["confidence"] = res["coverage_score"]
        # ★指导思想(KM 2781429277·GLM-5.3 控制变量实验): 【增加搜索次数(记忆维度)不能
        #   替代工具调用质量(推理维度)。bc 失败不是"搜索不够多"·而是"搜到的信息没被
        #   正确的推理链处理"】。落成可执行信号 search_efficiency:
        #   搜索步数多(记忆量大)但 grounded 比例低(推理链没顺着证据推进·在撒网裸猜)
        #   = 低效——此时【继续加搜索次数有害】·应【换向(顺藤摸瓜/换渠道)】而非同层再撒。
        eff = _search_efficiency(gr, en)
        res["search_efficiency"] = eff
        # ★冷启动撞墙检测(B类"纯描述无专名"题·真数据实证高特异描述在免费源反查不到):
        #   最开始几步就连续空返回且没拿到锚点→别继续描述撒网变体·换特异特征/结构化源。
        _has_anchor = int((en or {}).get("covered") or 0) >= 1
        cs = cold_start_stall(traj, has_anchor=_has_anchor)
        res["cold_start"] = cs
        # 继续搜信号: 纠缠点覆盖不足·或过早终止(但撞墙式空手不算"覆盖不足要继续同层搜")
        cov = res["coverage_score"]
        res["should_continue_search"] = bool(
            en.get("total") and cov < COVERAGE_CONTINUE and pm.get("premature"))
        res["advice"] = _coverage_advice(en, gr, pm, eff)
        # 冷启动撞墙优先提示(换向而非继续同层撒网变体·指导思想)
        if cs.get("stalled") and cs.get("advice"):
            res["advice"] = cs["advice"] + "\n" + (res["advice"] or "")
        # ★确认锁定优先提示(2026-08-24 跨框架根因·单假设确认偏误是六家全灭题的共病):
        #   同一候选被连续确认搜索却不收敛→必须引入竞争假设并主动证伪·优先级高于覆盖建议。
        if cl.get("locked") and cl.get("advice"):
            res["advice"] = ("⚠⚠ 确认锁定告警: " + cl["advice"] + "\n"
                             + (res["advice"] or ""))
            # 确认锁定时不应判定为"可交卷"·强制继续(换假设而非交当前候选)
            res["should_continue_search"] = True
        return res
    except Exception:
        return res


def _coverage_advice(entang, grounding, premature, efficiency=None):
    """给 agent 的记忆轨道建议文本(继续搜什么·怎么搜)。通用·不透答案。fail-safe → ''。"""
    try:
        lines = []
        cov = float((entang or {}).get("coverage") or 0.0)
        covered = int((entang or {}).get("covered") or 0)
        total = int((entang or {}).get("total") or 0)
        lines.append(
            "[记忆轨道·过程覆盖验证] 这是【覆盖型任务】(多约束找纠缠实体/深度调研)·"
            "验的是【搜索轨迹覆盖度】(信息找全了吗)·不是答案对错。当前搜索轨迹只触及 "
            "%d/%d 条约束的证据(覆盖率 %.0f%%)·未达可交卷门(%.0f%%)。"
            % (covered, total, 100 * cov, 100 * COVERAGE_CONTINUE))
        uncov = (entang or {}).get("uncovered") or []
        if uncov:
            lines.append("以下约束的证据【整个搜索过程都没触及】·请针对性各起一路检索(别撒网):")
            for i, u in enumerate(uncov[:5], 1):
                lines.append("  %d. %s" % (i, str(u)[:100]))
        # grounding 告警: 假设驱动 query 太多(裸猜验证)
        if (grounding or {}).get("warn"):
            lines.append(
                "⚠ 轨迹 grounding 告警: 你有 %.0f%% 的搜索 query 的关键词【没来自前一步检索"
                "结果】(假设驱动/裸猜)——像在用已知知识验证·而非顺着线索真发现。请从"
                "【已检索到的证据】里提取下一步 query 的实体·顺藤摸瓜(而非凭记忆猜实体去搜)。"
                % (100 * float((grounding or {}).get("hypothesis_ratio") or 0)))
        # 过早终止原因
        for r in (premature or {}).get("reasons") or []:
            lines.append("· " + str(r))
        # ★指导思想: 搜索次数≠推理质量。撒网态(搜得多但 grounded 低、覆盖仍不足)→
        #   明确告诉 agent【别再加搜索次数了·换向】——这是 bc 头号病(撒网发散)的解药。
        if efficiency and efficiency.get("casting_net"):
            lines.append(
                "⚠⚠ 效率诊断: %s。★核心: 增加搜索次数【不能】替代推理链质量——你不是"
                "搜得不够多·是搜到的信息没被顺着推理链处理。停止同层换词撒网·改为: "
                "①从已有证据里提取具体实体做下一跳(顺藤摸瓜) ②换检索渠道/换语种 "
                "③用已收集证据直接推理收敛·别再空转搜索。"
                % efficiency.get("verdict"))
        lines.append(
            "正确做法: 覆盖型任务【找到第一个看似合理的答案不等于找全了】——对未触及证据"
            "的约束逐条补检索(每条至少一个独立来源)·补齐纠缠点后再交(可标注不确定度·"
            "绝不弃权·绝不空交)。这【不是】判你答案错·而是记忆轨道说信息还没找全。")
        return "\n".join(lines)
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────
# 协调层: 短板决定上限(记忆轨道 × 推理轨道·取 min 置信度)。
# ─────────────────────────────────────────────────────────────────────────
def combine_tracks(memory_track, reasoning_confidence=None):
    """双轨制协调: 短板决定上限。返回 {"confidence":min, "bottleneck":str, "action":str}。

    · memory_track: verify_process_coverage 的返回(记忆轨道·信息覆盖度)。
    · reasoning_confidence: 推理轨道置信度 [0,1](如 identity_coverage 覆盖率/四态·
      调用方从 _ic_last_result 传入)·None 时只看记忆轨道。

    取 min(记忆置信, 推理置信) 作整体置信。bottleneck 指哪条轨道是短板:
      · memory  (记忆瓶颈·信息没找全) → action="continue_search"(继续搜);
      · reasoning(推理瓶颈·答案与证据矛盾) → action="regenerate"(重生成·推理轨道的活);
      · none    (两轨都够) → action="pass"。
    fail-safe → {"confidence":1.0,"bottleneck":"none","action":"pass"}(放行)。"""
    out = {"confidence": 1.0, "bottleneck": "none", "action": "pass",
           "memory_conf": 1.0, "reasoning_conf": 1.0}
    try:
        mconf = float((memory_track or {}).get("confidence") or 1.0)
        out["memory_conf"] = mconf
        rconf = 1.0 if reasoning_confidence is None else float(reasoning_confidence)
        out["reasoning_conf"] = rconf
        out["confidence"] = min(mconf, rconf)
        # 短板判定: 谁低谁是瓶颈(记忆瓶颈优先·覆盖型任务信息覆盖度是主轨)
        mem_short = (memory_track or {}).get("should_continue_search") \
            or mconf < COVERAGE_CONTINUE
        reason_short = (reasoning_confidence is not None
                        and rconf < COVERAGE_CONTINUE)
        if mem_short and (mconf <= rconf or not reason_short):
            out["bottleneck"] = "memory"
            out["action"] = "continue_search"
        elif reason_short:
            out["bottleneck"] = "reasoning"
            out["action"] = "regenerate"
        else:
            out["bottleneck"] = "none"
            out["action"] = "pass"
        return out
    except Exception:
        return out


# ═════════════════════════════════════════════════════════════════════════
# 自测
# ═════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import identity_coverage as _ic
    print("== process_coverage 自测 ==")
    # 构造一道覆盖型题(找既满足 X 又满足 Y 的实体)+ 搜索轨迹
    q = ('Which mathematician was born in "Vienna" in 1911, '
         'won the "Wolf Prize", and founded a journal on graph theory?')
    claims = _ic.extract_claims(q, llm_call=lambda *_: "")
    # 轨迹1: 只搜了 Vienna/1911·没触及 Wolf Prize / graph theory journal
    sess1 = {"tool_log": [
        {"tool": "web_search", "args": {"query": "mathematician born Vienna 1911"},
         "summary": "Found: several mathematicians born in Vienna around 1911."},
        {"tool": "web_search", "args": {"query": "Vienna 1911 mathematician list"},
         "summary": "Vienna 1911 births include ..."},
    ]}
    r1 = verify_process_coverage(sess1, q, claims=claims)
    print("  轨迹1(纠缠点未全覆盖): coverage=%.2f continue=%s uncovered=%d" % (
        r1["coverage_score"], r1["should_continue_search"],
        len(r1["entanglement"].get("uncovered") or [])))
    assert r1["coverage_score"] < COVERAGE_CONTINUE, "应识别覆盖不足"
    assert r1["should_continue_search"], "覆盖不足+过早终止→应触发继续搜"

    # 轨迹2: 假设驱动 query 比例高(query 实体没来自前一步结果·裸猜)
    sess2 = {"tool_log": [
        {"tool": "web_search", "args": {"query": "mathematician Vienna 1911"},
         "summary": "no result found"},
        {"tool": "web_search", "args": {"query": "Kurt Wolfgang Zeilinger prize"},
         "summary": "no result"},
        {"tool": "web_search", "args": {"query": "Hans Bergstrom graph journal"},
         "summary": "no result"},
        {"tool": "web_search", "args": {"query": "Emil Fischer graph theory"},
         "summary": "no result"},
    ]}
    r2 = verify_process_coverage(sess2, q, claims=claims)
    print("  轨迹2(假设驱动裸猜): grounding.warn=%s ratio=%.2f" % (
        r2["grounding"].get("warn"), r2["grounding"].get("hypothesis_ratio")))
    assert r2["grounding"].get("warn"), "假设驱动 query 比例高→应告警"

    # 轨迹3: grounded(query 实体来自前一步结果·顺藤摸瓜)+ 全约束触及
    sess3 = {"tool_log": [
        {"tool": "web_search", "args": {"query": "mathematician born Vienna 1911 Wolf Prize"},
         "summary": "Ernst Muster, born Vienna 1911, later won the Wolf Prize in 1980."},
        {"tool": "web_search", "args": {"query": "Ernst Muster Wolf Prize"},
         "summary": "Ernst Muster received the Wolf Prize; he also founded a graph theory journal."},
        {"tool": "web_search", "args": {"query": "Ernst Muster graph theory journal founded"},
         "summary": "Ernst Muster founded the Journal of Graph Theory research."},
    ]}
    r3 = verify_process_coverage(sess3, q, claims=claims)
    print("  轨迹3(grounded+全覆盖): coverage=%.2f continue=%s warn=%s" % (
        r3["coverage_score"], r3["should_continue_search"],
        r3["grounding"].get("warn")))
    assert not r3["should_continue_search"], "覆盖足→不该继续搜(放行)"

    # 短板集成: 记忆瓶颈→continue_search·推理瓶颈→regenerate
    c1 = combine_tracks(r1, reasoning_confidence=0.9)
    print("  短板(记忆瓶颈): bottleneck=%s action=%s conf=%.2f" % (
        c1["bottleneck"], c1["action"], c1["confidence"]))
    assert c1["bottleneck"] == "memory" and c1["action"] == "continue_search"
    c2 = combine_tracks(r3, reasoning_confidence=0.2)
    print("  短板(推理瓶颈): bottleneck=%s action=%s conf=%.2f" % (
        c2["bottleneck"], c2["action"], c2["confidence"]))
    assert c2["bottleneck"] == "reasoning" and c2["action"] == "regenerate"
    print("== 全部通过 ==")
