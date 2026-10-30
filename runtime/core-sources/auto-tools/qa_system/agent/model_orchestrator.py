"""model_orchestrator · 2026-08-10 · 模型编排器(用户架构: 有人编排不同模型·像skill·沉淀经验)

不靠 agent 自觉调多模型(实证prompt软引导拉不动)·而是【硬编排】:
  1. route(question, kind): 按题型+成本+速度选最合适的模型(用usable_models动态)
  2. cross_verify(question, answer, kind): finalize前强制第二模型独立复核·不一致第三仲裁
  3. 经验沉淀: 记"哪类题哪模型答对了"到 ~/.meyo/model_experience.jsonl·越用越准

题型kind: vision(看图) / calc(数值计算) / research(检索考据) / reason(纯推理) / general
路由策略(可被经验库覆盖):
  vision  → 优先 gemini-2.5-pro(多模态最强)·备 gpt-5/claude-opus
  calc    → 优先 gpt-5(数学最强)·备 claude-opus
  research→ 优先 claude-opus(考据严谨)·备 gemini-2.5-pro(长文理解)
  reason  → 优先 gpt-5·备 claude-opus
成本/速度: 轻题用便宜快的(longcat-2.0/gpt-4o-mini)·难题才上贵的(opus/gpt-5)
"""
import os
import time
import json as _json

_EXP_LOG = os.path.expanduser("~/.meyo/model_experience.jsonl")

# 题型 → 优先模型序(第一个是首选·后面是备选/交叉验证用)。可被经验库动态调整。
_ROUTE = {
    "vision":   ["gemini-2.5-pro", "gpt-5", "claude-opus"],
    "calc":     ["gpt-5", "claude-opus", "gemini-2.5-pro"],
    "research": ["claude-opus", "gemini-2.5-pro", "gpt-5"],
    "reason":   ["gpt-5", "claude-opus", "gemini-2.5-pro"],
    "general":  ["claude-opus", "gpt-5", "gemini-2.5-pro"],
}


def _mm():
    import sys as _s
    _s.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import multi_model as m
    return m


def _pick(kind, vision_only=False):
    """按题型+可用性挑模型序(只在探测可用的里选·经验库优先)。"""
    mm = _mm()
    usable = set(mm.usable_models(vision_only=vision_only))
    pref = _experience_pref(kind)  # 经验库学到的偏好(可能为空)
    ordered = pref + [m for m in _ROUTE.get(kind, _ROUTE["general"]) if m not in pref]
    picked = [m for m in ordered if m in usable]
    return picked or list(usable)


def _experience_pref(kind):
    """从经验库读该题型历史表现最好的模型。
    v99c(2026-08-12·D3): 排序 = 答对率 × 速度惩罚。
      相同答对率时·响应慢(p50 latency 高)的模型排后面——bandit 越用越聪明不只看对错·也看效率。
      latency 从 experience 日志里的 elapsed_s 字段读(v99c 起 record_experience 可传)。
      fail-safe: 任何解析异常 → 退化到纯 win-count 排序 → 再失败 → 空。
    """
    try:
        if not os.path.exists(_EXP_LOG):
            return []
        from collections import defaultdict
        stats = defaultdict(lambda: {"win": 0, "total": 0, "elapsed_sum": 0.0, "elapsed_n": 0})
        for l in open(_EXP_LOG, encoding="utf-8"):
            if not l.strip():
                continue
            try:
                r = _json.loads(l)
            except Exception:
                continue
            if r.get("kind") != kind:
                continue
            m = r.get("model")
            if not m:
                continue
            stats[m]["total"] += 1
            if r.get("correct"):
                stats[m]["win"] += 1
            if r.get("elapsed_s") and r["elapsed_s"] > 0:
                stats[m]["elapsed_sum"] += float(r["elapsed_s"])
                stats[m]["elapsed_n"] += 1
        if not stats:
            return []
        # 计算 score = win_rate × latency_factor
        # latency_factor: median latency 越小 → 越接近 1.0; 越大 → 越小(最低 0.5)
        all_elapsed = [s["elapsed_sum"] / s["elapsed_n"]
                       for s in stats.values() if s["elapsed_n"] > 0]
        median_lat = sorted(all_elapsed)[len(all_elapsed) // 2] if all_elapsed else 30.0
        scored = []
        for m, s in stats.items():
            win_rate = s["win"] / max(1, s["total"])
            if s["elapsed_n"] > 0:
                avg_lat = s["elapsed_sum"] / s["elapsed_n"]
                # 比中位数快 → factor > 1; 比中位数慢 2x → factor ≈ 0.67
                lat_factor = min(1.5, max(0.5, median_lat / max(1.0, avg_lat)))
            else:
                lat_factor = 1.0  # 没有延迟数据·不惩罚
            scored.append((m, win_rate * lat_factor, s["win"]))
        # 主排 score 降序·同分再按 win 数量降序
        scored.sort(key=lambda x: (-x[1], -x[2]))
        return [m for m, _, _ in scored]
    except Exception:
        # 退化到纯 win-count
        try:
            from collections import Counter
            win = Counter()
            for l in open(_EXP_LOG, encoding="utf-8"):
                if not l.strip():
                    continue
                r = _json.loads(l)
                if r.get("kind") == kind and r.get("correct"):
                    win[r.get("model")] += 1
            return [m for m, _ in win.most_common()]
        except Exception:
            return []


def best_models(kind, n=2, scene=None, min_samples=3):
    """v99n(2026-08-13·用户'全场景都该按解题能力挑最优'):按经验库实战正确率挑任意场景的
    最优模型(vision 只是特例)。calc/research/reason/general 都能用——让所有场景'用户有啥好模型
    就按实战准度用哪个'·而非钦定固定 claude。
    盘点铁证:基建齐全(_pick/_experience_pref/_ROUTE 都支持任意kind)·但只有vision喂过数据·
    非视觉全跑_ROUTE死表。此函数+非视觉埋点(resolve_qid_outcome扩到全kind)=补上'按准度学该用谁'。

    - scene细分优先(数据够)·退回粗粒度kind。
    - 经验库空/异常: fail-safe 退回 _ROUTE[kind] 顺序里的可用模型(不退化·至少走死表)。
    """
    try:
        mm = _mm()
        # vision场景限定vision_only·其余场景全模型可选
        _vonly = kind == "vision" or (kind or "").startswith("vision")
        usable = set(mm.usable_models(vision_only=_vonly))
        if not usable:
            return [_ROUTE.get(kind, _ROUTE["general"])[0]]
        ranked = []
        if scene:
            _sp = _pick_by_winrate(f"{kind}:{scene}", min_samples=min_samples)
            ranked = [m for m in _sp if m in usable]
        if len(ranked) < n:
            for m in _pick_by_winrate(kind, min_samples=1):
                if m in usable and m not in ranked:
                    ranked.append(m)
        # 经验库没覆盖到的·按 _ROUTE 死表顺序垫底(有数据学数据·没数据走合理默认)
        for m in _ROUTE.get(kind, _ROUTE["general"]):
            if m in usable and m not in ranked:
                ranked.append(m)
        for m in usable:  # 再兜底
            if m not in ranked:
                ranked.append(m)
        return ranked[:n] if ranked else [_ROUTE.get(kind, _ROUTE["general"])[0]]
    except Exception:
        try:
            return [m for m in _ROUTE.get(kind, _ROUTE["general"])][:n] or ["claude-opus"]
        except Exception:
            return ["claude-opus"]


def best_vision_models(n=2, scene=None, min_samples=3):
    """v99i(2026-08-13) · 按经验库【实战正确率】挑最优 vision 模型·A/B两条读图路径共用。
    治用户质疑"谁说一定是claude":经验库铁证 gemini 90/90=100% > claude 4/4 > gpt-5 1/13=7.7%。
    别钦定任何模型·让实战数据决定用谁。

    返回:经验库正确率(×速度)排序的前 n 个可用 vision 模型名。
    - scene 非空(如"handwritten"/"table")且该场景样本 >= min_samples: 用细分经验 kind="vision:{scene}"
      不足则退回粗粒度 kind="vision"(场景数据先埋点后生效·避免场景空时挑不准)。
    - 经验库为空/异常: fail-safe 退回 usable_models 原序(再不行退 claude-opus)·绝不崩。
    - 只返回【当前可用】的 vision 模型(经验库里可能有已下线的)。
    """
    try:
        mm = _mm()
        usable = set(mm.usable_models(vision_only=True))
        if not usable:
            return ["claude-opus"]
        ranked = []
        # 场景细分优先(数据够才用)
        if scene:
            _scene_pref = _pick_by_winrate(f"vision:{scene}", min_samples=min_samples)
            ranked = [m for m in _scene_pref if m in usable]
        # 粗粒度 vision 经验补齐(或场景数据不足时的主力)
        if len(ranked) < n:
            _coarse = _pick_by_winrate("vision", min_samples=1)
            for m in _coarse:
                if m in usable and m not in ranked:
                    ranked.append(m)
        # 经验库没覆盖到的可用模型垫底(保证有得选)
        for m in usable:
            if m not in ranked:
                ranked.append(m)
        return ranked[:n] if ranked else ["claude-opus"]
    except Exception:
        try:
            return _mm().usable_models(vision_only=True)[:n] or ["claude-opus"]
        except Exception:
            return ["claude-opus"]


def _pick_by_winrate(kind, min_samples=1):
    """从经验库按【正确率优先·样本量达标】排序返回模型名。
    与 _experience_pref 区别:那个是 winrate×latency 综合;这个纯正确率优先(读图准度第一·速度其次),
    且过滤样本不足的(gpt-5 只1次对不算稳)。
    v99j:优先用 stage=solved(真答对)数据·某模型有 solved 样本就只算 solved(真答对率);
      没有 solved 才退回 returned(返回率)。让'真答对率'压倒'返回率'——治 gemini 90/90 其实是返回率。
    fail-safe 返回 []。"""
    try:
        if not os.path.exists(_EXP_LOG):
            return []
        from collections import defaultdict
        # 分 stage 统计:solved(真答对) 与 returned(返回) 分开算
        st = defaultdict(lambda: {"solved_win": 0, "solved_tot": 0,
                                  "ret_win": 0, "ret_tot": 0})
        for l in open(_EXP_LOG, encoding="utf-8"):
            if not l.strip():
                continue
            try:
                r = _json.loads(l)
            except Exception:
                continue
            if r.get("kind") != kind:
                continue
            m = r.get("model")
            if not m:
                continue
            _stage = r.get("stage", "returned")
            if _stage == "solved":
                st[m]["solved_tot"] += 1
                if r.get("correct"):
                    st[m]["solved_win"] += 1
            else:
                st[m]["ret_tot"] += 1
                if r.get("correct"):
                    st[m]["ret_win"] += 1
        # 每个模型:有 solved 样本达标就用真答对率·否则退回返回率
        qualified = []
        for m, s in st.items():
            if s["solved_tot"] >= min_samples:
                rate, tot, basis = s["solved_win"] / s["solved_tot"], s["solved_tot"], 2  # solved 优先级高
            elif s["ret_tot"] >= min_samples:
                rate, tot, basis = s["ret_win"] / s["ret_tot"], s["ret_tot"], 1
            else:
                continue
            qualified.append((m, rate, tot, basis))
        # 先按"有无真答对数据(basis)"降序·再正确率·再样本量——真答对数据的模型优先
        qualified.sort(key=lambda x: (-x[3], -x[1], -x[2]))
        return [m for m, _, _, _ in qualified]
    except Exception:
        return []


def route(question, kind="general", tier="hard", vision_only=False):
    """按题型/难度选一个模型名。tier=easy用便宜快的·hard用最强的。"""
    if tier == "easy":
        # 轻题(如一致性判断): 从【所有可用模型】里挑成本最低的·不限题型序
        mm = _mm()
        avail = mm.available_models()
        usable = mm.usable_models(vision_only=vision_only)
        if usable:
            return sorted(usable, key=lambda m: avail.get(m, {}).get("cost", 3))[0]
        return "claude-haiku"
    picked = _pick(kind, vision_only=vision_only)
    return picked[0] if picked else "claude-opus"


def cross_verify(question, answer, kind="general", image_b64="", media_type="image/png",
                 evidence=""):
    """硬编排交叉验证: 让另一个(不同家)模型基于【相同资料】独立复核·返回:
      {agree: bool, second_answer, second_model, verdict}
    不一致时调第三个模型仲裁。用于 finalize 前对高风险答案强制复核。
    evidence: agent已查到的关键资料(必传·否则第二模型裸算·检索类题必错)。"""
    mm = _mm()
    picked = _pick(kind, vision_only=bool(image_b64))
    if len(picked) < 2:
        return {"agree": None, "second_answer": "", "second_model": "",
                "verdict": "无第二可用模型·跳过验证"}
    # 真异构: 第二模型优先选和主答案【不同家】的(主答案默认claude·第二优先gpt/gemini)。
    # 同家模型错法相似·交叉验证价值低;异构才能互相纠错。
    avail = mm.available_models()
    main_family = "claude"  # auto 主答案默认走 claude
    hetero = [m for m in picked if avail.get(m, {}).get("family") != main_family]
    second = hetero[0] if hetero else picked[1]
    picked = [second] + [m for m in picked if m != second]  # 重排·仲裁也异构优先
    _ev = f"\n\n已查到的资料(基于这些独立作答):\n{evidence[:3000]}" if evidence else ""
    prompt = (f"基于给定资料独立解答这道题(只给最终答案·简短)。\n问题: {question[:2000]}{_ev}")
    r2 = mm.ask(second, prompt, image_b64=image_b64, media_type=media_type,
                timeout=90, max_tokens=800)
    if not r2["ok"]:
        return {"agree": None, "second_answer": "", "second_model": second,
                "verdict": f"第二模型调用失败: {r2['err'][:80]}"}
    a2 = r2["out"].strip()
    # 一致性判断: 让便宜快的模型判两个答案是否表达同一事实
    judge = mm.ask(route("", "general", tier="easy"),
                   f"这两个答案是否表达同一事实/同一数值?只答一个词 YES 或 NO。\n"
                   f"答案1: {answer[:500]}\n答案2: {a2[:500]}",
                   timeout=30, max_tokens=100)
    # fail-safe: judge挂了→退化到字符串归一化比对(别因judge失败误判不一致)
    if judge.get("ok") and judge.get("out", "").strip():
        agree = "yes" in judge["out"].lower()
    else:
        na = "".join(answer.lower().split())
        nb = "".join(a2.lower().split())
        agree = na and (na in nb or nb in na)
    result = {"agree": agree, "second_answer": a2, "second_model": second, "verdict": ""}
    if agree:
        result["verdict"] = f"✅两模型一致(claude+{second})·答案可信"
    else:
        # 不一致→第三模型仲裁
        if len(picked) >= 3:
            third = picked[2]
            r3 = mm.ask(third, prompt, image_b64=image_b64, media_type=media_type,
                        timeout=90, max_tokens=800)
            result["third_model"] = third
            result["third_answer"] = r3["out"].strip() if r3["ok"] else ""
            result["verdict"] = (f"⚠️两模型不一致·第三模型({third})仲裁: "
                                 f"{result['third_answer'][:200]}")
        else:
            result["verdict"] = f"⚠️两模型不一致(答案1 vs {second}=答案2)·需人工核查"
    return result


def record_experience(kind, model, correct, qid="", ts=None, elapsed_s=None,
                      stage="returned"):
    """沉淀经验。fail-safe。ts 由调用方传(不调time)。
    v99j(2026-08-13) 新增 stage 区分信号语义(治"correct=True其实只是'返回了内容'非'答对'"):
      stage="returned"(默认·旧语义): correct 表示【模型成功返回了内容】(gemini 90/90 就是这个)。
      stage="solved": correct 表示【这题最终真答对/答错】——由 finalize 评分后经 resolve_qid_outcome 回填。
    挑模型(_pick_by_winrate)优先用 solved 数据·没有才退回 returned·让'真答对率'而非'返回率'决定用谁。"""
    try:
        os.makedirs(os.path.dirname(_EXP_LOG), exist_ok=True)
        rec = {"kind": kind, "model": model, "correct": bool(correct),
               "qid": qid, "ts": ts or 0, "stage": stage}
        if elapsed_s is not None and elapsed_s > 0:
            rec["elapsed_s"] = round(float(elapsed_s), 2)
        with open(_EXP_LOG, "a", encoding="utf-8") as f:
            f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def _answer_value_overlap(text_a, text_b):
    """两段文本数字/分数集合的 Jaccard 重合度(0~1)·用于证据驱动归因。fail-safe→0。"""
    try:
        import re as _re
        def _vals(t):
            t = t or ""
            fr = {f.replace(" ", "") for f in _re.findall(r"\d+\s*/\s*\d+", t)}
            nums = set(_re.findall(r"-?\d+(?:\.\d+)?", _re.sub(r"\d+\s*/\s*\d+", " ", t)))
            return fr | nums
        va, vb = _vals(text_a), _vals(text_b)
        if not va or not vb:
            return 0.0
        return len(va & vb) / len(va | vb)
    except Exception:
        return 0.0


def resolve_qid_outcome(qid, correct, kind_prefix=None,
                        final_answer=None, model_readings=None):
    """v99j/v99k/v99n · finalize 评分后回填【真答对信号】·支持证据驱动的精确归因。

    v99n(全场景推广):kind_prefix=None(默认)时回填本题【所有场景】的模型记录
      (vision/calc/reason/research...)·让非视觉场景也按实战准度学该用谁——治盘点发现的
      "只有vision接了线·其余全钦定claude"。传具体prefix(如"vision")则只回填该场景。
    v99j 基础:为本题参与过的模型补 stage=solved 记录(correct=本题是否答对)。
    v99k 精确归因(agent自主·证据驱动·非硬规则):若传入 final_answer + model_readings
      ([{model, text}...]·各模型读数)·则按【答案与哪个模型读数最接近(数字重合度)】决定归因:
      - 答对: 只有读数与最终答案最接近的模型记 solved=True(它的读数被采纳了·功劳归它);
              读数明显不同的模型不记(它的读数没被用·不该沾光)。
      - 答错: 同理·被采纳的那个模型记 solved=False(它读错导致答错);其余不记。
      没传 model_readings 时退回 v99j 行为(所有参与模型记同一结果)。
    fail-safe:任何异常静默返回 False。"""
    try:
        if not qid or not os.path.exists(_EXP_LOG):
            return False
        # 收集本题参与过的 (kind, model)·去重(从 returned 记录)
        seen = set()
        for l in open(_EXP_LOG, encoding="utf-8"):
            if not l.strip():
                continue
            try:
                r = _json.loads(l)
            except Exception:
                continue
            if r.get("qid") != qid:
                continue
            _k = r.get("kind", "")
            # kind_prefix=None → 全场景回填(推广);指定则只回填该场景(及其:子场景)
            if kind_prefix is not None and not (
                    _k == kind_prefix or _k.startswith(kind_prefix + ":")):
                continue
            if r.get("stage", "returned") != "returned":
                continue
            m = r.get("model")
            if m:
                seen.add((_k, m))
        if not seen:
            return False

        # v99k 精确归因:找出读数与最终答案最接近的模型(它的读数被采纳)
        _attributed = None
        if final_answer and model_readings:
            try:
                _best_ov, _best_m = -1.0, None
                for _mr in model_readings:
                    _m, _txt = _mr.get("model"), _mr.get("text", "")
                    if not _m or (_mr.get("failed")):
                        continue
                    _ov = _answer_value_overlap(final_answer, _txt)
                    if _ov > _best_ov:
                        _best_ov, _best_m = _ov, _m
                # 只有重合度够高(答案确实源自该模型读数)才精确归因·否则退回全体
                if _best_m and _best_ov >= 0.5:
                    _attributed = _best_m
            except Exception:
                _attributed = None

        n = 0
        for (_k, m) in seen:
            # 精确归因命中:只给被采纳的模型记(功劳/锅都归它);其余模型这题不记 solved
            if _attributed is not None and m != _attributed:
                continue
            if record_experience(_k, m, correct, qid=qid, ts=0, stage="solved"):
                n += 1
        return n > 0
    except Exception:
        return False


def experience_summary():
    """经验库统计: 每题型每模型的答对率(供人看学到了啥)。"""
    if not os.path.exists(_EXP_LOG):
        return {}
    from collections import defaultdict
    stat = defaultdict(lambda: defaultdict(lambda: [0, 0]))  # kind→model→[对,总]
    for l in open(_EXP_LOG, encoding="utf-8"):
        if not l.strip():
            continue
        r = _json.loads(l)
        s = stat[r.get("kind")][r.get("model")]
        s[1] += 1
        if r.get("correct"):
            s[0] += 1
    return {k: {m: f"{v[0]}/{v[1]}" for m, v in mv.items()} for k, mv in stat.items()}


if __name__ == "__main__":
    import sys
    if "--route" in sys.argv:
        for kind in ["vision", "calc", "research", "reason"]:
            print(f"{kind}: 首选={route('', kind)} · 序={_pick(kind)}")
    elif "--exp" in sys.argv:
        print(_json.dumps(experience_summary(), ensure_ascii=False, indent=2))
    else:
        # 冒烟: 交叉验证一道简单计算题
        r = cross_verify("What is 17 * 23?", "391", kind="calc")
        print("交叉验证测试:", _json.dumps(r, ensure_ascii=False)[:300])
