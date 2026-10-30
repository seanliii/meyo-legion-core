# -*- coding: utf-8 -*-
"""finalize_gates · 交卷信号、提醒与收尾兼容层。

2026-08-27 从 qa_auto_agent god-file 抽出。默认 context not control:门检测信息写 hint,
不阻断模型交付；MEYO_FINALIZE_VETO=1 才恢复硬拦。依赖由 qa_auto_agent 在加载时
显式 configure 注入，模块不反向 import god-file，避免循环依赖。
"""
import json
import os
import re
import sys
import time


def configure(**dependencies):
    """注入共享 helper/常量；允许 reload 后重新绑定。"""
    globals().update(dependencies)


def _finalize_veto_on() -> bool:
    """finalize 门=context 还是 control 的总开关。
    (2026-08-27 上线瘦身·用户'finalize可坑了好多问题被拒绝') 默认改为 0=只提醒不拦:
      门检测的信号照发事件+记 hint 喂回模型·但【放行 finalize】·由模型自己判断是否处理。
      学 DeepSeek guard「只建议不否决」+ 元纲领「给 context 不加 control」。军团上线用户
      要的是"给答案"·不是被门反复拦。
    MEYO_FINALIZE_VETO=1: 恢复门可拦截(benchmark 严格口径/需要硬拦时)。
    fail-safe: 读不到 env → 默认 0(只提醒)。"""
    return os.environ.get("MEYO_FINALIZE_VETO", "0") == "1"


def _record_finalize_hint(session, gate: str, msg: str):
    """把一道门检测到的信号作为【非阻塞 hint】记进 session["_finalize_hints"]。
    这是"门变 context"的落点: 门原本 return block_msg 否决·现改为 append 到这里·
    finalize 放行时把 hints 作为附注呈现给模型(模型自己判断)。once-only 打标由各门保留(去重)。"""
    try:
        if not isinstance(session, dict) or not (msg or "").strip():
            return
        hints = session.setdefault("_finalize_hints", [])
        hints.append({"gate": str(gate or ""), "hint": str(msg)[:1600], "at": _now_iso()})
        del hints[:-12]  # 上限12条·防污染上下文
    except Exception:
        pass


def _drain_finalize_hints(session) -> str:
    """取出累积的 finalize hints·拼成一段"供参考的检测提醒"文本(非强制)。
    用于 VETO=0 时把门检测到的信号平静地喂回模型·取后清空(避免下轮重复)。"""
    try:
        hints = session.get("_finalize_hints") or []
        if not hints:
            return ""
        session["_finalize_hints"] = []
        lines = [f"· [{h.get('gate','')}] {h.get('hint','')}" for h in hints if h.get("hint")]
        if not lines:
            return ""
        return ("【交付前·系统检测到以下信号(仅供参考·不强制·你自己判断是否需要处理再交付)】\n"
                + "\n".join(lines))
    except Exception:
        return ""


def _gate_or_hint(session, block_msg, gate_name):
    """P2 平级门统一收口: 一道平级 finalize 门检测出 block_msg 后的分流。
    VETO=1(默认): 原样返回 block_msg(调用方据此拦截·现行为不变)。
    VETO=0: 把信号记进 session hint(context 透明给模型)·返回 "" (放行·不拦)。
    这样平级门的【检测照跑】·只是把"拦截"翻转成"告知"——满足'给完整上下文非加限制'。"""
    if not (block_msg or ""):
        return ""
    if _finalize_veto_on():
        return block_msg
    _record_finalize_hint(session, gate_name, block_msg)
    try:
        _emit_gate_event(session.get("qid", "") if isinstance(session, dict) else "",
                         gate_name, "hint", {"len": len(str(block_msg))})
    except Exception:
        pass
    return ""


def _maybe_block_finalize(session, task_level, final_markdown):
    """finalize 反向卫兵(P2 wrapper)。
    VETO=1(默认): 行为不变——内部门返回非空即拦截 finalize。
    VETO=0: 内部门检测到的"否决消息"降级为 hint(记进 _finalize_hints·放行 finalize)·
      检测逻辑/事件/once-only 打标全保留·只翻转"控制"为"告知"。
    真必要 control(全局收敛闸/fail-safe)不受开关影响。"""
    _block = _maybe_block_finalize_impl(session, task_level, final_markdown)
    if not _block:
        return ""
    if _finalize_veto_on():
        return _block  # 现行为:拦截
    # VETO=0: 降级为 hint·放行(context 不 control)
    try:
        _record_finalize_hint(session, "finalize_signal", _block)
        _emit_gate_event(session.get("qid", "") if isinstance(session, dict) else "",
                         "finalize_veto_downgrade", "hint", {"len": len(_block)})
    except Exception:
        pass
    return ""


def _maybe_block_finalize_impl(session, task_level, final_markdown):
    """finalize 反向卫兵:重型/需检索题想放弃(UNKNOWN)但没充分用工具时,返回
    一段"逼它继续"的提示(拦截本次 finalize);否则返回 "" (放行 finalize)。
    通用判据,不特判任何具体题目。fail-safe:任何异常都放行(返回 "")。"""
    try:
        budget = session.get("_budget") or {}
        answer = str(final_markdown or "")  # 统一别名·修后续分支裸用 answer 的隐式NameError
        # Global convergence is a harness invariant and must precede semantic auditors;
        # otherwise independent gates can each consume another turn after the cap.
        _finalize_cap = 4
        _attempts = int(budget.get("_finalize_attempt_total", 0) or 0) + 1
        budget["_finalize_attempt_total"] = _attempts
        if _attempts > _finalize_cap and not _looks_like_giving_up(final_markdown):
            budget["_finalize_forced_converge"] = True
            _emit_gate_event(session.get("qid", ""), "finalize_converge", "escape",
                             {"attempts": _attempts, "cap": _finalize_cap})
            return ""
        # ── ★交付契约门(2026-08-20 用户重大判断·换位思考·治G1契约破)──────────────
        #   用户视角判: 交付的答案【回应了题目问的那个类型吗】?治"题问real name却交
        #   stage name(艺名)、题问哪年却交事件名"——即使指向同一实体也没回应题问。
        #   ★带已有信息(题面+答案)·只判类型是否匹配·不判对错·防幻觉。只拦一次·墙钟到点让路。
        #   env MEYO_PERSPECTIVE_ENGINE=0 关。fail-safe。
        try:
            if (final_markdown and not _wall_past(session)
                    and not budget.get("_delivery_contract_checked")):
                import perspective_engine as _pe_dc
                if _pe_dc._enabled():
                    _q_dc = session.get("question") or ""
                    # ★优雅收敛:走统一 decide(PHASE_DELIVER)(底层复用check_delivery_contract)
                    _snap_dc = _pe_dc.ContextSnapshot(_q_dc, session=session,
                                                      llm_call=session.get("_llm_call"))
                    _dc = _pe_dc.decide(_snap_dc, _pe_dc.PHASE_DELIVER, answer=final_markdown)
                    if not _dc.get("ok") and _dc.get("issue"):
                        budget["_delivery_contract_checked"] = True
                        try:
                            _emit_gate_event(session.get("qid", ""), "delivery_contract",
                                             "type_mismatch", {"expected": _dc.get("expected")})
                        except Exception:
                            pass
                        return ("[交付契约·未回应题问] %s 题目要的输出类型是【%s】。"
                                "你现在交的答案【类型不符·没回应题目所问】——即使它指向了正确的"
                                "实体/相关信息·也要【转成题目要求的形式】再交(如题问真名就给真名·"
                                "别给艺名;题问年份就给年份)。请基于已查到的证据补上那个正确类型的"
                                "答案再 finalize。" % (_dc.get("issue", ""), _dc.get("expected", "")))
                    budget["_delivery_contract_checked"] = True
        except Exception:
            pass
        # v-eff2c(2026-08-19·test4 三题全错复盘) · 低置信度候选相似实体反查
        #   (只拦一次)。实证:test4 三题都在"检索够了但锁错相邻实体"上翻车——
        #   Solanum laciniatum vs 正确的 capsicoides(同属邻近种)、Muskogee vs
        #   正确的 Muskegon(拼写/发音相近的另一支球队)——都是模型自己在
        #   Confidence 里写了 <50% 却直接交卷，没有做"是否存在名称相似的
        #   其他候选"这一步排除性核验。这不是覆盖率不足(_identity_coverage_gate
        #   测不出来)，是"证据支持了一个错误但相似的候选"。修:低置信度自认+
        #   墙钟未到点时，拦一次逼它反查相似候选；墙到点则放行(遵循 _wall_past
        #   统一让路原则，不重复烧时间)。
        if (not _wall_past(session)
                and not budget.get("_similar_entity_recheck_done")):
            import re as _re_conf
            _conf_m = _re_conf.search(
                r"confidence\**[:：]?\**\s*(\d{1,3})\s*%", str(final_markdown or ""),
                _re_conf.I)
            _low_conf = bool(_conf_m and int(_conf_m.group(1)) < 50)
            # v127(2026-08-19 · 用户核心诉求接线) · 除了"低置信度自认"这个【模型
            # 自己说的】信号，现在还接入 _progressive_candidate_check 在候选形成
            # 【当时】就做的轻量核验结果——如果那次早期核验已经标记 questionable
            # (证据没明确支持这个候选/证据里提到了更贴合的邻近候选)，即使模型
            # 这次 finalize 时自信满满没写低置信度，也该触发同样的排除性反查。
            # 这就是"渐进式"落地的关键一环：不再只依赖 agent 自报的置信度，而是
            # 用【候选形成当时、独立于主模型的观察】去补足自评盲区。
            _fm_final = (final_markdown or "").strip().lower()
            _prog_log = session.get("_progressive_candidate_log") or []
            _prog_flag = None
            for _pe in reversed(_prog_log):
                _pe_ans = str(_pe.get("answer") or "").strip()
                # v127 · 用统一的 _candidate_similarity(而非粗糙前缀截断)判断
                # final_markdown 里当前交付的候选，是否就是渐进日志里记录过的
                # 那个候选(整词判据，避免 Solanum laciniatum/hermannii 这类
                # 只共享属名前缀的邻近候选被误判为"同一个")。final_markdown 是
                # 长文本，这里直接做整词/子串包含判断即可(候选词本身通常较短)。
                if _pe_ans and (_candidate_similarity(_pe_ans, _fm_final)
                                or _pe_ans.lower() in _fm_final):
                    _prog_flag = _pe
                    break
            _prog_questionable = bool(_prog_flag and _prog_flag.get("verdict") == "questionable")
            if _low_conf or _prog_questionable:
                budget["_similar_entity_recheck_done"] = True
                _emit_gate_event(session.get("qid", ""), "similar_entity_recheck",
                                 "block", {"confidence": int(_conf_m.group(1)) if _conf_m else None,
                                           "progressive_flag": _prog_questionable})
                _prog_hint = ""
                if _prog_questionable:
                    _alt = str(_prog_flag.get("better_alt") or "").strip()
                    _reason = str(_prog_flag.get("verdict_reason") or "").strip()
                    _prog_hint = (
                        f"\n（渐进式早期核验在这个候选刚形成时就标记过疑点: {_reason}"
                        + (f"，并提示可能更贴合的候选是『{_alt}』" if _alt else "")
                        + "——这不是这次临时想到的，是候选形成当下就有的信号，别忽略。）"
                    )
                _conf_hint = f"你标注的置信度低于 50%，" if _low_conf else "虽然这次没标低置信度，但"
                return (
                    f"[候选反查·只拦这一次] {_conf_hint}先别交——低置信度/早期预警往往不是"
                    "'证据不够'，而是'锁定了一个拼写/发音/分类相近的邻近候选'(实证:同属邻近种"
                    "被当成目标种、拼写相近的另一支球队被当成目标队)。" + _prog_hint + "请做一次"
                    "**排除性核验**再交：\n"
                    "  ① 把你当前候选的名字/关键特征单独拿出来，搜一次"
                    "'与之名称/拼写/分类相近的其他候选'(如同属的其他种、"
                    "同名不同队的其他球队/城市);\n"
                    "  ② 逐条对照题面约束，看是否有另一个相近候选比你现在这个"
                    "**更贴合某个你之前忽略的细节**(尤其是最后一两条约束，"
                    "往往是排歧点);\n"
                    "  ③ 如果反查后确认没有更贴合的候选，原样交付并说明'已排查"
                    "相近候选，未发现更优解'；如果找到更贴合的，换成新候选交付。"
                )
        # v118 · P1 审计判据(env MEYO_FINALIZE_AUDITOR=1·默认关)·LH 完成判定
        # 去耦合:先于旧门链跑·violation 才拦·同因不二拦·fail-safe 全放行。
        if os.environ.get("MEYO_FINALIZE_AUDITOR", "") == "1":
            _aud = _auditor_evidence_check(session, final_markdown)
            if _aud:
                return _aud
        # 在推理/数值验证之前就可能遗漏了题干的输入槽位。不能让模型自行填数后
        # 把“硬币数”等中间量伪装为“金额”等最终量；这是题面契约缺失，优先于
        # 墙钟放行。只拦一次，后续必须诚实收尾，避免因原题确实残缺而死锁。
        _contract_gaps = _question_contract_gaps(session.get("question", ""))
        # v108(M6a) · 附件已消费则不再拦: 占位型缺口若对应的是【附件槽位】·而
        # tool_log 里已有成功的附件读取(file_read/read_pdf/shell cat/fetch_gaia_file
        # 等)→输入实际上已获得·拦截只会制造"想收又不敢收"的空转(bec7实证)。
        if _contract_gaps:
            _tl = session.get("tool_log") or []
            _attach_ok = any(
                (t.get("tool") in ("file_read", "read_pdf", "read_table",
                                    "query_table",
                                    "read_image_ocr", "see_image_vlm")
                 and not any(b in str(t.get("summary", ""))[:200]
                             for b in ("失败", "不存在", "异常", "error")))
                or (t.get("tool") == "shell_exec"
                    and any(k in str(t.get("args", "")) for k in ("cat ", "head ", ".csv", ".json", ".xlsx", ".txt"))
                    and "exit_code=0" in str(t.get("summary", ""))[:120])
                for t in _tl)
            if _attach_ok:
                _contract_gaps = [g for g in _contract_gaps if "占位符" not in g]
        if _contract_gaps and not budget.get("_question_contract_gap_notified"):
            budget["_question_contract_gap_notified"] = True
            _emit_gate_event(session.get("qid", ""), "question_contract", "block",
                             {"gaps": _contract_gaps})
            return (
                "[题干契约缺口] 当前题干本身缺少决定交付量的输入："
                + "；".join(_contract_gaps)
                + "。不要把占位符自行补成常数，也不要把中间量当最终量。"
                  "请先回到原始题目/附件确认缺失值；若原始资料不可得，只能明确给出"
                  "依赖该参数的符号化结果或说明无法唯一确定。"
            )
        # 硬顶已触发(墙钟真耗尽)→直接收尾。
        # (2026-08-27 瘦身) 原 worksheet 超时共识门已删:特定GAIA混排图题型过拟合·
        #   gate_events真实仅触发1次;hard_block 后再拦=撞墙后夺答案。视觉读取信号仍保留
        #   在 perception_votes/context 中供模型自主判断,不由父进程否决交付。
        if budget.get("hard_block_active"):
            return ""
        # 只对"需检索"的题设这道门:重型/标准。琐事/简单题一律放行(不拖累快答)。
        # 也尊重用户手动选的档位:选了"琐事/快速"就别拦。
        level = session.get("force_task_level") or task_level or "标准"
        if level not in ("重型", "标准"):
            return ""

        # (2026-08-27 瘦身) worksheet结构化共识硬门已删:特定GAIA混排图题型过拟合·
        #   gate_events真实仅触发1次·且用 _forced_tool 强制read_image_ocr/see_image_zoom
        #   夺模型自主(违反给context不加control)。视觉多轮读取仍记入perception_votes供
        #   模型自主判断·不由父进程强制指定下一步工具。

        # ── v99g(2026-08-13 · 分数图 trace 实证 · 治空子3:finalize 反复被拒无收敛出口) ──
        # 病根:下面有多道 finalize 拦截门(共识门/反直觉门/推导链痕迹门/优化题门/交卷前验证…),
        #   每道各自 _MAX_BLOCKS 计数。分数图那次 finalize×6 全被轮番拦→每次拦完不收敛而是
        #   回去 zoom/ocr 重读→读↔拒打转 57 步撞 recursion_limit,交半成品答错。
        # 修:加一个【全局 finalize 尝试总计数】·跨所有子门。累计到硬上限就跳过全部拦截门·
        #   直接放行"交付当前最佳"(带低置信标由下游 answer 自己承载)。这是防打转的最后闸·
        #   不改各子门自身逻辑·只在总次数爆表时兜底放行·尊重"拦够就放行防死锁"的既有设计哲学。
        if not _looks_like_giving_up(answer):
            # P2(2026-08-27·数据说话): 对抗反制门(_adversarial_counter_strategy_audit·
            #   独立LLM多模型投票二次否决)+ 反直觉过度论证门(_looks_like_overthinking·
            #   关键词密度计数否决)已删。均命中已证伪黑名单(独立LLM二次否决/关键词计数否决)。
            #   删除·不转hint(信号本身可疑·且烧墙钟)。
            # v76(2026-08-09)交卷前"回头验证"闸(把 agent 当人:人交卷前会核一遍)。
            #   治标准差题(gold=17.056 算错)这类:给了精确数值/事实答案·但可能没验证。
            #   命中条件:重型/标准题 + 答案含精确数值/短事实 + 本题还没触发过自检。
            import re as _re76
            _has_number = bool(_re76.search(r"\d", answer))
            _short_fact = len(answer.strip()) < 400  # 精确答案通常短(数值/名字/编号)

            # ── v110(2026-08-16 · 双口径账本 S1 快错实证) 口径轴审计门 ─────────────
            # 病根(账本双口径量化): first_shot 65/165 → 重试上限 164/165 的 99 道胜局里
            #   【70 道是快错】——首跑中位 ~180s 就自信交卷·胜局中位 313s。错因不是不会做·
            #   是读法/口径轴(时间点/包含标准/否定方向/单位精度/分母范围)没探索就锁死。
            #   错题本只治重考·首问没有历史可翻。但 review_intent(独立异构模型只核对题意
            #   理解·不给答案)是历史无关的——问题在它是自选工具·快错型 agent 恰恰不会想
            #   起来调(自选机制的系统性盲区: 最需要核对的人最不觉得自己需要)。
            # 修法: finalize 时若【答案短且含数值 + 全程没调过 review_intent】→ 拦一次·
            #   强制调它核对读法轴·核对结果与当前答案不一致就必须先改。已调过=自证免拦。
            #   只拦一次·位于全局硬闸之后·不新增死循环面·fail-safe。
            if (_has_number and _short_fact
                    and not budget.get("intent_axis_audit_done")):
                budget["intent_axis_audit_done"] = True
                _called_ri = any((e.get("tool") == "review_intent")
                                 for e in (session.get("tool_log") or []))
                if not _called_ri:
                    try:
                        _emit_gate_event(session.get("qid", ""), "finalize_gate",
                                         "intent_axis_audit", {})
                    except Exception:
                        pass
                    return (
                        f"[交卷前口径轴审计 · 必须先核题意] 你给了一个短而精确的数值/事实答案·"
                        f"但全程还没调用过 review_intent(独立异构模型只核对题意理解·不给答案)"
                        f"——快错型错误(读法/口径轴锁死)恰恰出在这一步·而且越是自信越容易跳过。"
                        f"**先别 finalize**,现在就调一次 review_intent·重点核对五条通用读法轴:\n"
                        f"  ① 时间点口径: 题目要的是【哪个时间点/版本】的数据?历史时点还是当前?\n"
                        f"  ② 包含标准: 哪些条目【算入/不算入】?(全部vs仅某些·含边界vs不含边界)\n"
                        f"  ③ 否定词方向: 否定/排除条件的逻辑方向读反了吗?(A且非B vs 非(A或B))\n"
                        f"  ④ 单位与精度: 单位对吗?舍入要求(位数/方向)对吗?\n"
                        f"  ⑤ 分母范围: 比例/平均的分母是【哪个总体】?\n"
                        f"核对结果与当前答案不一致→先改再交;一致→带着核对记录再 finalize。"
                    )

            # (2026-08-27 瘦身) 否定辖域枚举门已删:8d46企鹅题(BC否定辖域题型)过拟合·
            #   真实触发0次·强制枚举四组合=BC特例·真实用户不遇。

            # P2(2026-08-27): 辖域仲裁门整段已删(gold_reading反查+三视角投票门控)。
            #   该门直接拿外层评分gold读法对比agent主答并否决·不具备真实场景泛化性；
            #   无gold时又退化为已证伪的多模型投票。否定辖域信号仍由上方枚举门提供。

            # ── FixM 终答值优先门(2026-08-17 · bec7/384d/0b26 实证: 终答最后一公里) ──
            #   病根: 仲裁门/活库门都拦得住"该不该改"·但拦不住"答案以什么打头"。
            #   三题尸检都死在答案第一个 token 不是题目要的那个数——子串匹配
            #   命中不到 → hit=0。这里加一道最末端的【值优先】门: 题面要数 +
            #   答案不以数打头 → 拦一次逼它重排版(把最后算出的数前置)。once-only·
            #   抽不到数也提示(可能是没真算出来·不算交卷)。零特判·无 gold 泄漏。
            #   修(同日·8d46 占位符实证): 原条件带 _has_number(答案含数字才查)——
            #   但"占位符/工具回显当答案"(如『还没建答案契约·建议先 build...』)
            #   恰恰没有任何数字·被门漏过。改为纯看题面: 题面要数+答案不以数
            #   打头就拦·不管答案里有没有数字。
            # (2026-08-27 瘦身) 终答值优先门已删:注释自承"为评分器子串匹配·答案数字打头才不得0分"
            #   =纯benchmark评分器格式过拟合·面向用户答案数字/句子打头无所谓。连同_KIND_HINT
            #   (硬写PubChem CID/to 3 decimals题型特征)+_question_answer_kind一并删。

            # ── v121 修②(0b26 实证) 数值出处门: 主答值必须在工具输出里有出处 ──
            #   病根: _looks_like_giving_up 只认话术放弃——0b26 交「6.523(重建)」
            #   这类带数字的无据重建骗过全部检测(比拒答危险·复盘误记「算错」)。
            #   主答数(答案开头第一个数)在 tool_log 里找不到出处 → 拦一次:
            #   要么真算一遍(工具可记录)要么诚实交无法算出。once-only·fail-safe
            #   (工具记录里没有任何数值时无从比对·放行)。位于值优先门之后——
            #   值优先门先把数值逼到开头·本门再验它的出处。
            if (_has_number and not budget.get("_num_provenance_checked")):
                try:
                    budget["_num_provenance_checked"] = True
                    _tok_pr = _leading_answer_number(final_markdown or answer)
                    if _tok_pr and len(str(_tok_pr).strip(".,")) >= 2:
                        _ss_pr, _fl_pr = _tool_log_numbers(session)
                        if _fl_pr:  # 工具记录里有数值才可比(空=无从比对·放行)
                            if not _number_backed(str(_tok_pr), _ss_pr, _fl_pr):
                                try:
                                    _emit_gate_event(session.get("qid", ""), "finalize_gate",
                                                     "num_provenance", {"unbacked": str(_tok_pr)})
                                except Exception:
                                    pass
                                return (
                                    f"[数值出处 · 主答值在工具记录中无出处] 你的答案以"
                                    f"【{_tok_pr}】开头·但整个 tool_log 的工具输出里找不到"
                                    f"这个数(或其近似值)——它像是 narrative 里的重建/推想值"
                                    f"(实证: 有题撞墙后交了从未计算过的『重建』数字·比"
                                    f"诚实拒答更危险·复盘会把编造误记成算错)。三选一后再 finalize:\n"
                                    f"  ① 若该值真算得出来: 用 shell_exec/python 把计算"
                                    f"实际跑一遍·让数值出现在工具输出里·再交;\n"
                                    f"  ② 若数值出自论文/文献(某论文摘要里的年代/数量等): "
                                    f"别靠网页前几千字符——关键数字常在摘要后半被截(实证: "
                                    f"某题网页拓 8000 字符仍没拿到关键数·凭背景知识编了个"
                                    f"近似数交卷答错)。用结构化 API 拿【完整摘要】: Crossref "
                                    f"api.crossref.org/works/<DOI> 的 abstract 字段·或 "
                                    f"Semantic Scholar api.semanticscholar.org/graph/v1/"
                                    f"paper/DOI:<doi>?fields=abstract·或 PMC/ PubMed 全文;\n"
                                    f"  ③ 若算不出来(数据不可达/口径缺失): 诚实交"
                                    f"『无法从可获取的数据算出』并说明缺什么——别交无据数值。"
                                )
                except Exception:
                    pass

            # ── v121 修③(00d579ea 实证) 专家单源门: 主答实体仅凭专家记忆 ──────
            #   病根: 视频题 SSL 全墙后改问专家——双专家凭记忆一致答同一流行实体·
            #   第三专家明说无法验证被忽略·gold 是另一人。流行度偏见跨模型
            #   共享·一致投票不增信息量。主答实体只在 ask_expert_model 输出出现、
            #   无任何独立文本证据命中 → 拦一次逼找转录/字幕/档案文本佐证。
            #   once-only·fail-safe·不要求 _has_number(人名答案无数字)。
            if not budget.get("_expert_solo_checked"):
                try:
                    budget["_expert_solo_checked"] = True
                    _ent_es = _expert_solo_sourced(session, final_markdown or answer)
                    if _ent_es:
                        try:
                            _emit_gate_event(session.get("qid", ""), "finalize_gate",
                                             "expert_solo", {"entity": str(_ent_es)[:60]})
                        except Exception:
                            pass
                        return (
                            f"[专家单源 · 主答仅凭专家模型记忆] 你的主答实体【{_ent_es}】在"
                            f"全部工具输出里只出现在 ask_expert_model 的回答中——没有任何"
                            f"独立的文本证据(网页/字幕/档案/数据库)命中它。两个专家一致≠对:"
                            f"不同模型共享同样的流行度偏见·在【具体事实】上的一致只是"
                            f"同一份记忆投了两票(实证: 视频内容题双专家凭记忆一致答 A·"
                            f"gold 落在被冷落的 B·第三专家明说『无法验证』却被忽略)。"
                            f"交卷前二选一:\n"
                            f"  ① 找文本佐证: 该内容的原始/转录文本——字幕站(subtitlecat/"
                            f"amara)、Internet Archive 副本、wiki/fandom 专题页、新闻报道"
                            f"全文、学术引用·用 fetch_url_smart/shell_exec 拿到【含该断言"
                            f"的原文】再交;\n"
                            f"  ② 找不到: 答案降置信(明确标注『依据为模型记忆·无文本佐证』)"
                            f"或诚实交 UNKNOWN。\n"
                            f"别再问第三个专家——他只会在同样的记忆上投票。"
                        )
                except Exception:
                    pass

            # ── v121 修④(04a04a9b 实证) 基数口径门: 计数题多源分歧审计 ──────────
            #   病根: "多少篇×系数"题——两家聚合库基数已分歧 >10%·
            #   引擎仍挑一个直接算·gold 是出版方官方口径(差数倍)。题面是计数题 +
            #   工具记录里 ≥2 个分歧 >10% 的基数 → 拦一次逼"先消歧口径(出版方
            #   官方分类优先)再算"。once-only·fail-safe。
            if (_has_number and not budget.get("_basecount_checked")):
                try:
                    _q_bc = session.get("question") or ""
                    if _QCOUNT_PAT.search(_q_bc):
                        budget["_basecount_checked"] = True
                        _div = _base_counts_divergent(session)
                        if _div:
                            try:
                                _emit_gate_event(session.get("qid", ""), "finalize_gate",
                                                 "basecount_diverge",
                                                 {"counts": [str(v) for v in _div[:6]]})
                            except Exception:
                                pass
                            return (
                                "[基数口径审计 · 多源分歧] 题面要计数·你的答案=基数×"
                                "系数·而工具记录里已出现互相分歧的基数: "
                                + " / ".join(str(v) for v in _div[:6])
                                + "(最大分歧 "
                                + str(round(_div[-1] / max(1, _div[0]) * 100 - 100))
                                + "%)——答案对基数高度敏感·先消歧口径再算(实证: 某题"
                                "聚合库宽口径与出版方官方窄口径差数倍·直接挑一个算"
                                "导致答错)。聚合库(OpenAlex/CrossRef/"
                                "PubMed)的 type=article 是【宽口径】——news&views/"
                                "editorials/letters/obituaries 全被算进去。交卷前:\n"
                                "  ① 消歧: 题目问的『articles』指什么?优先用【出版方官方"
                                "分类统计】(期刊官网 article-type 筛选页/官方出版统计·"
                                "如 nature.com 按 Article 类型过滤的年度列表)·或对聚合库"
                                "结果按官方 Article 列表交叉过滤;\n"
                                "  ② 消歧后多源一致(±2%)才用它;拿不到官方口径就答案里"
                                "注明基数来源+口径+时点·并把最可能的窄口径重算一遍"
                                "对比——两口径答案都写在答案里会得 0 分·必须选定一个。"
                            )
                except Exception:
                    pass

            # (2026-08-27 瘦身) 活库时点门(PubChem/ORCID库名硬正则)+空间关系门(空间词硬正则)已删:
            #   为特定GAIA题型硬写的正则匹配门·gate_events真实触发0次·benchmark过拟合。

            # ── 推导链痕迹门(v99e · 2026-08-13 · 感知≠计算·强化) ──────────────
            # 病根(多边形题实证·第八次边界误判):VLM 把所有边长都读对了(数字准确
            # 落进 _vlm_extracted_values),但没有任何机制强制"读出数字后必须去代码里
            # 算"——answer 是这些提取值的【计算结果】(面积/计数/求和…),却是 VLM/心算
            # 目测出来的,没跑过一次 shell_exec。CC 把这诊断成"VLM 精度边界"是错的:
            # 精度没问题,是【计算发生了没有】没人管。
            # 这道门与【证据门】同构:证据门管"有没有证据",这门管"计算有没有发生"。
            # 判据【结构化抽值 + 语义/结构,不靠关键词表】:
            #   ① 答案含数值 且 本题 VLM 读图取过数(_vlm_extracted_values 非空);
            #   ② 答案里的数值【不是任一提取值的原样复制】=答案是提取值的某种函数(需计算)
            #      ——若答案就等于某个读出的数字(如"边长是10"),那没计算、不该拦;
            #   ③ 本题推导链里没有结构化 calculation_trace——不靠 shell_exec_total==0
            #      (太弱:agent 可能跑了 curl 但没算)，也不猜命令文本。能力工具/执行器必须
            #      写入 engine、inputs、result 三元痕迹，语义判据·不限措辞。
            # 命中就拦 finalize,把 agent 推回【自主】去算(不给公式·守"给机制不代劳"红线),
            # 拦够上限仍放行(尊重自主·防死锁)。通用:worksheets/tables/receipts/forms 同治。
            _CALC_TRACE_MAX_BLOCKS = 2
            try:
                _ev = budget.get("_vlm_extracted_values") or []
                # v99f: 计算不是"命令里看着像有 python/area/sum"，而是结构化痕迹。
                # capability_stack 的符号计算会写 calculation_trace；当前自由 shell
                # 没有受信的 result 协议，不能仅凭命令文本计入，避免 curl/描述文字误放行。
                _calc_trace = session.get("calculation_trace") or []
                _has_calc_step = any(
                    isinstance(_step, dict) and _step.get("engine") and _step.get("result") is not None
                    for _step in _calc_trace
                )
                # v100b(2026-08-14 · 推导链痕迹门认 shell_exec Python 计算) ──
                # 病根(GameGrumps): agent 用 shell_exec 跑了精确 Python 计算(从 mkwrs.com
                # HTML 解析 91 条 WR 记录·整数毫秒运算)·但痕迹门只认 calculation_trace
                # (sympy_calc 写的)·不认 shell_exec → 误拦 2 次浪费 120s 预算。
                # 修法: 检查 tool_log 里的 shell_exec·若命令含 python3 + 数值计算/解析模式
                # 且 exit_code=0·也计入 _has_calc_step。
                if not _has_calc_step:
                    for _e in (session.get("tool_log") or []):
                        if _e.get("tool") != "shell_exec":
                            continue
                        _cmd = str((_e.get("args") or {}).get("command", ""))
                        _summ = str(_e.get("summary", ""))
                        if ("python3" in _cmd or "python -c" in _cmd or "python <<" in _cmd
                                and any(kw in _cmd for kw in ("import ", "print(", "= ", "for ", "if "))
                                and "exit_code=0" in _summ):
                            _has_calc_step = True
                            break
                if _has_number and _ev and not _has_calc_step:
                    _ans_nums = set(_re76.findall(r"-?\d+(?:\.\d+)?", answer))
                    _ev_set = set(str(x) for x in _ev)
                    # 答案里至少有一个数值不是"读出来原样搬运"的 → 说明它是提取值的函数(算出来的)
                    _answer_is_derived = bool(_ans_nums - _ev_set)
                    _ct_blocks = int(budget.get("_calc_trace_blocks", 0) or 0)
                    if _answer_is_derived and _ct_blocks >= _CALC_TRACE_MAX_BLOCKS:
                        # 拦满上限 → 按设计放行(尊重自主·防死锁)·打 incomplete_derivation 标签存证
                        budget["_calc_trace_incomplete"] = True
                        _emit_gate_event(session.get("qid", ""), "calc_trace", "escape",
                                         {"ct_blocks": _ct_blocks, "n_extracted": len(_ev),
                                          "has_calc_step": _has_calc_step,
                                          "tag": "incomplete_derivation"})
                    if _answer_is_derived and _ct_blocks < _CALC_TRACE_MAX_BLOCKS:
                        budget["_calc_trace_blocks"] = _ct_blocks + 1
                        _emit_gate_event(session.get("qid", ""), "calc_trace", "block",
                                         {"block_n": _ct_blocks + 1, "max": _CALC_TRACE_MAX_BLOCKS,
                                          "n_extracted": len(_ev), "has_calc_step": _has_calc_step})
                        return (
                            f"[推导链痕迹门 · 拦截目测交卷(第 {_ct_blocks + 1}/{_CALC_TRACE_MAX_BLOCKS} 次)] "
                            f"你从图里读出了一组数字,而你现在给的答案是【由这些数字算出来的结果】——"
                            f"但本题推导链里【没有计算步】,这个结果是 VLM/心算目测出来的。"
                            f"实证:复杂几何/多值聚合(如多边形面积、逐项计数求和)VLM 边看边算几乎必错——"
                            f"读得准 ≠ 算得对,感知和计算是两件事。**先别 finalize**,像人交卷前那样:\n"
                            f"  ① 把你从图里读出的那组数字,用 sympy_calc 或 shell_exec 写 python **精确计算**一遍;\n"
                            f"  ② 先核对数字有没有噪声(如图里的标注/marker 不是真正参与计算的量)再算;\n"
                            f"  ③ 用代码算出的值替换你目测的答案,再来 finalize。\n"
                            f"怎么建模、用什么公式由你自己判断——这道门只负责拦下'读了没算就交卷'。"
                        )
            except Exception:
                pass  # fail-safe:痕迹门出任何岔子都不拦,绝不因它卡死交付

            # v96c(2026-08-10) · 自检从"软提示一次"升级为"真验证过才放行"。
            #   病根(实证 Freon-12 题):v76 只提示一次,agent 收到提示后可以
            #   直接原样再 finalize 一次(没真的换方法重算),answer_selfcheck_done
            #   一旦置 True 就永久放行——等于"说了句复核建议"但没人拦它没做。
            #   实测:CoolProp 液相方程算出 193mL,但 gold=55mL(反推密度
            #   5673kg/m³,液相不可能这么高——大概率是"peak temperature"
            #   口径理解错/该走另一种相态或算法),agent 只用了一种方法/一次
            #   工具链就自信 finalize,没有真正"换算法/换假设"交叉验证。
            #   修法:提示注入后记录当时的 n_investigative_calls 快照,下次
            #   还想 finalize 时检查有没有新的独立验证动作(至少 1 次新的
            #   search/shell_exec)——没有就继续拦(有上限 CALC_VERIFY_MAX_BLOCKS
            #   防死循环),逼它真的用不同方法/参数假设重算一遍再交。
            _CALC_VERIFY_MAX_BLOCKS = 2

            # (2026-08-14) 原 v13.95"博弈论/优化题专检"硬编码词表门已删除。
            # 病根:硬词表(optimal strategy/game show/最优策略...)既误触(GameGrumps"shows'
            #   hosts")又漏判(题面没这些词的建模歧义题)·且只做"框架内穷举验证"抓不到"框架选错"。
            # 改由上游 _hypothesis_comparison_audit 的【多模型建模歧义门】统一覆盖(博弈/优化/物理
            #   口径/统计定义都是同一类"建模框架歧义")·语义判定+经验驱动·不写死题型词表。
            # 用户明确:不写死·让模型基于经验库自己判。

            if _has_number and _short_fact:
                if not budget.get("answer_selfcheck_done"):
                    budget["answer_selfcheck_done"] = True
                    budget["selfcheck_inv_snapshot"] = budget.get("n_investigative_calls", 0)
                    budget["selfcheck_verify_blocks"] = 0
                    return (
                        f"[交卷前回头验证 · 必须真做一次] 你给了一个精确的数值/事实答案。"
                        f"像人交卷前会回头核一遍——**先别急着 finalize**,这次不是走过场提示,"
                        f"你必须真的**再调一次工具**做交叉验证才能通过,自检要点:\n"
                        f"  ① 这个数/事实的**来源**可靠吗?是从权威来源查到的·还是自己算/猜的?\n"
                        f"  ② **换一个独立信息源/独立工具复现**它——**注意:同一个工具/同一套"
                        f"自己推的公式只是换个参数重算，不算真正的独立验证**(你对该工具的用法/"
                        f"前提假设本身可能就错了，换参数重算只会在同一个错误框架里绕圈子，"
                        f"发现不了系统性错误)。正确做法优先级:\n"
                        f"     a) 涉及专业计算(物性数据、化学/生化反应动力学、单位换算、统计分布等)"
                        f"时,先 **skill_search / search_github** 找有没有现成的专业计算库/工具"
                        f"(比如物性查询用 CoolProp/PubChem，不确定自己调用方式对不对时，"
                        f"找该库的官方文档/示例代码核对参数含义，而不是凭猜测传参)；\n"
                        f"     a2) **最简模型先估数量级**:在用专业库之前,先用人手能算的最简"
                        f"公式/最简模型(零阶近似)估一个数量级。如果最简模型和专业库结果差>2倍,"
                        f"说明你的前提假设(如相态、边界条件、模型适用范围)可能有问题——"
                        f"回头查证而非自信交付。\n"
                        f"     b) 找不到专业库，就换一个完全独立的信息源/网页/数据库去核对"
                        f"这个数字，而不是自己再心算或重跑同一段代码；\n"
                        f"     c) 如果能找到 **ask_expert_model** 换一个不同家的模型独立复算，"
                        f"两边结果不一致就是信号，说明思路本身有问题。\n"
                        f"  ③ 有没有**口径/单位/范围**错?(平均是算哪些项·四舍五入几位·"
                        f"包含还是不包含边界·单位对不对·题目的关键词是不是理解错了方向·"
                        f"用到的物理/化学状态假设——比如相态、温度压力条件——是否重新核实过)\n"
                        f"真做完这次**独立信息源**的交叉验证再 finalize——如果两个独立来源/工具"
                        f"结果差异很大,说明思路可能有问题(很可能是对某个专业工具的调用方式"
                        f"/前提假设理解错了),该回头查证该工具的正确用法，而不是自信交付。"
                    )
                # 已经提示过 → 检查有没有真的做过新的独立验证动作
                _snap = budget.get("selfcheck_inv_snapshot", 0)
                _now_inv = budget.get("n_investigative_calls", 0)
                _blocks = budget.get("selfcheck_verify_blocks", 0)
                if _now_inv <= _snap and _blocks < _CALC_VERIFY_MAX_BLOCKS:
                    budget["selfcheck_verify_blocks"] = _blocks + 1
                    return (
                        f"[交卷前回头验证 · 还没真做(第{_blocks + 1}/{_CALC_VERIFY_MAX_BLOCKS}次拦)] "
                        f"上一条提示要你**真的调一次工具**做交叉验证,但你还没有新的"
                        f"search/shell_exec 调用——别只是嘴上说'已验证'就直接再 finalize。"
                        f"真去 skill_search/search_github 找个独立的专业工具核对，或换个"
                        f"独立来源查证，再来交卷(不是在同一个工具里换参数重算)。"
                    )
            return ""
        # 真检索类工具已经用够了 → 先过 v112 万物皆工具·通道枚举门再放行
        n_inv = budget.get("n_investigative_calls", 0)
        # ── v112(2026-08-16 · 0b26 ScienceDirect 三轮诚实拒答实证) 通道枚举门 ──
        # 病根: agent 检索次数够了(过难题卫兵的量)·但只从【一条正面路】硬闯
        #   (ScienceDirect 官网)·被反爬/无数据挡住就诚实 UNKNOWN——而"拿不到
        #   当前数据"≠"数据不存在": 历史快照/学术镜像/结构化API/别的agent都是
        #   通道。万物皆工具: 断一条路就枚举其它路·不是枚举理由。
        # 修法: 答案像放弃 + 检索已够量 + tool_log 里【零】替代通道痕迹
        #   (wayback/archive.org/webcache/crossref/api/delegate/子agent)
        #   → 拦一次给通道菜单。once-only·fail-safe。
        try:
            if n_inv >= FINALIZE_MIN_INVESTIGATIVE and not budget.get("_channel_enum_done"):
                _tl = session.get("tool_log") or []
                _alt_tried = False
                for _e in _tl:
                    _blob = (str(_e.get("tool")) + " " + str(_e.get("summary", ""))[:200]
                             + " " + str((_e.get("args") or {}))).lower()
                    if any(k in _blob for k in (
                            "wayback", "archive.org", "webcache", "cache:",
                            "crossref", "api.", "doi.org", "scholar",
                            "delegate", "spawn_subagent", "ollama", "claude",
                            "dataset", "mirror", "镜像", "快照")):
                        _alt_tried = True
                        break
                if not _alt_tried:
                    budget["_channel_enum_done"] = True
                    _emit_gate_event(session.get("qid", ""), "finalize_gate",
                                     "channel_enumerate", {})
                    return (
                        "[万物皆工具 · 断路枚举门] 你想以『拿不到数据』收场·但你只从正面"
                        "一条路闯过。**当前拿不到 ≠ 数据不存在**——先别 finalize·把下面"
                        "替代通道真试一遍(每条都真调工具·别嘴上列举):\n"
                        "  ① 历史快照: web.archive.org/wayback 查出题时点前后的该页面快照"
                        "(shell_exec curl 'http://archive.org/wayback/available?url=…&timestamp=…');\n"
                        "  ② 结构化接口: 该数据源常有 API/导出端点(官方api.*、crossref、"
                        "数据集下载、sitemap)·search 一下『<源名> API/export dataset』;\n"
                        "  ③ 学术镜像/第三方汇编: 同一数据常被论文/统计年鉴/聚合站引用过——"
                        "搜数字本身或表格名而非只搜官网;\n"
                        "  ④ 别的脑子也是工具: delegate/spawn_subagent 换个模型独立试·或 "
                        "scan_environment 看本机还有谁能帮上;\n"
                        "  ⑤ 邻近数据反推: 若目标值可由相邻粒度(总数-其他项/逐年累加)推出·"
                        "用可达数据推算并注明推算链。\n"
                        "五条里至少真试 2 条·仍拿不到再来 finalize(那才是穷尽后的诚实 UNKNOWN)。"
                    )
        except Exception:
            pass
        # 替代通道也试过/门没命中 → 尊重"多方真尝试过拿不到才 UNKNOWN",放行
        if n_inv >= FINALIZE_MIN_INVESTIGATIVE:
            return ""
        # 已经拦够上限次数 → 别无限逼它,放行(交付诚实 UNKNOWN 也比死循环强)
        n_blocked = budget.get("finalize_block_count", 0)
        if n_blocked >= FINALIZE_MAX_BLOCKS:
            return ""
        # ── 命中:重型/需检索题 + 想放弃 + 没充分用工具 + 还没拦够 → 拦 ──
        budget["finalize_block_count"] = n_blocked + 1
        return (
            f"[难题卫兵 · 拦截放弃] 这是一道【需检索/多跳考据】的题,你才用了 {n_inv} 次"
            f"真检索类工具就想 finalize 答『查不到/UNKNOWN』——这不是"
            f"『多方真尝试过拿不到』,而是过早放弃。**先别 finalize**,按下面的多跳解法"
            f"真动手(这是一次机会,已拦第 {n_blocked + 1}/{FINALIZE_MAX_BLOCKS} 次):\n"
            f"  ① 分解:把问题拆成几个子问题——先定位【来源】(论文/仓库/文档),"
            f"再从来源里提取【要素】,最后对要素查【最终属性】(ID/编号/日期等)。"
            f"用 write_plan 把这几步列出来。\n"
            f"  ② 定位来源:run_parallel_search / search_github 找到那篇论文/那个仓库;"
            f"是论文就 shell_exec 用 curl 把 PDF 下下来(pip_install pdfplumber 或 PyPDF2 解析"
            f"正文/方法学章节),别只看摘要。\n"
            f"  ③ 提取要素:从来源内容里读出题目问的那几个具体东西"
            f"(比如某方法用了哪几种试剂/某仓库哪几个贡献者)。\n"
            f"  ④ 查最终属性:对每个要素,shell_exec 用 curl 查权威库/接口"
            f"(EC 号→ExPASy ENZYME/BRENDA/KEGG/UniProt;CAS→PubChem;"
            f"日期→GitHub API),或先 search 到『这类数据从哪个官方接口拿』再 curl。"
            f"至少两处交叉印证。\n"
            f"每一步都真调工具、看真实返回,别跳步猜。多试几种来源/几条命令后,"
            f"仍拿不到再来 finalize(那时才是诚实的 UNKNOWN)。"
        )
    except Exception:
        return ""  # fail-safe:出任何岔子都放行,绝不因这道门卡死交付


def _finalize_escape_guard(s, qid, task_level_hint, final, trace,
                           agent=None, question="", hard_wall_s=0.0,
                           t_start=None, max_iterations=30):
    """v103(2026-08-15) · bug3 · 文字收尾绕门(finalize escape)检测+补门。

    病根(gaia7 实证): 全部 finalize 质检门(共识/建模歧义/反直觉/反向对策/…)
    只挂在 finalize_answer 工具路径上;agent 被门拦过一次后学会【不再调
    finalize·loop 正常结束时用最后一条文字消息直接作答】——final 从
    narrative/last-message 兜底提取·ckpt final 全空·一条 gate 事件都没有。
    修两层:
      层2(预算够·agent在): 对文字答案原样跑一遍 _maybe_block_finalize·被拦则
        注入拦截消息重新 invoke·逼它走 finalize_answer 正式交付(只 1 次·防打转);
      层1(没预算): 只记 finalize_escape gate 事件+trace 标记(可观测·不污染答案)。
    fail-safe: 任何异常都放行原 final·绝不因补门丢交付。返回(可能更新的)final。"""
    try:
        if not (final or "").strip():
            return final
        _qid = s.get("qid") or qid or ""
        _budget = _rs_sessions.ensure_budget(s)
        _can_rerun = (agent is not None and t_start is not None
                      and (hard_wall_s - (time.time() - t_start)) > 90
                      and not _budget.get("_esc_rerun_done"))
        _block = _maybe_block_finalize(s, task_level_hint, final)
        if not _block:
            _emit_gate_event(_qid, "finalize_escape", "pass",
                             {"note": "文字收尾补门通过"})
            trace["finalize_escape_checked"] = True
            return final
        _emit_gate_event(_qid, "finalize_escape", "block", {
            "note": "文字收尾未经finalize门·被补门拦下",
            "rerun": bool(_can_rerun)})
        trace["finalize_escape_blocked"] = True
        if not _can_rerun:
            trace["finalize_escape_note"] = (
                "预算不足或已重跑过·文字收尾答案未过 finalize 门(仅记录·不改答案)")
            return final
        _budget["_esc_rerun_done"] = True
        print("  🚪 [finalize-escape] 文字收尾被补门拦截·注入拦截消息逼正式finalize",
              file=sys.stderr, flush=True)
        _esc_msg = (
            f"{_block}\n\n"
            "【系统·交付方式整改】你刚才试图【不调用 finalize_answer·直接用文字"
            "作答收尾】——这绕过了全部交卷质检门·属于无效交付。请按上面拦截消息"
            "的要求补足验证后·【必须调用 finalize_answer 工具】正式交付;再次"
            "直接文字作答将被系统视为未完成。")
        agent.invoke(
            {"messages": [{"role": "user", "content": _esc_msg}]},
            config={"recursion_limit": max_iterations * 2 + 10},
        )
        _new_fin = (s.get("final") or "").strip()
        if _new_fin:
            trace["finalize_escape_recovered"] = True
            return _new_fin
        return final
    except Exception as _esc_e:
        print(f"  ⚠️ finalize-escape 补门异常(放行原答案): {_esc_e}",
              file=sys.stderr, flush=True)
        return final


