# -*- coding: utf-8 -*-
"""tool_factory · auto 工具装配、toolset 与 progressive disclosure。

2026-08-27 从 qa_auto_agent god-file 抽出。依据 Hermes Footprint Ladder:
core 是窄腰；其余工具由 tool_search/tool_describe/tool_call 按需发现调用。
共享工具工厂和状态 helper 由 auto 显式 configure 注入，本模块不反向 import god-file。
"""
import inspect
import json
import os
import re
import time


def configure(**dependencies):
    globals().update(dependencies)


def _make_tools(qid, task_level="标准", urgency="normal"):
    """把 qa_tools_registry 里注册的全部工具，转成绑定了 qid 的可调用函数列表。
    task_level/urgency 用于 retryable 工具的自适应重试预算判断。
    v59: 同时桥接 evolution_tool_registry 的延迟工具(ToolSearch + Execute)。
    """
    specs = get_all_tools()
    tools = []

    for spec in specs:
        def _build(spec=spec):  # 闭包陷阱：默认参数捕获当前循环的 spec
            def _tool_fn(question: str, final_markdown: str = "") -> str:
                t0 = time.time()
                s = _sess(qid)

                # 预算/循环卫兵检查放在最前面：即便是首次调用，也要先看
                # 墙钟硬顶和"同一工具连续调用"计数——这是 CCB Agentic Loop
                # 终止状态机在我们架构下的等价落点(工具执行前置检查)。
                # v108(M1a) · 传 content_key=question(=query/URL):循环卫兵粗分支
                #   靠它区分"穷举"(参数在变)和"空转"(参数重复)。不传时粗分支
                #   只能按工具名计数——f2fe 就是这样被误拦 10 次的。
                allow, guard_msg = _check_budget(s, spec.name, content_key=question)
                if not allow:
                    _log_tool(qid, spec.name, {"question": question}, guard_msg, time.time() - t0)
                    return guard_msg

                # v106 · 死域熔断·执行前: 携带已熔断域名的网络调用直接拦截·省 30-60s/次
                # (实证: web.archive.org TCP 不通·agent 反复撞·624 烧 ~3min·0b26 烧更多)。
                _dd_hosts, _dd_msg = _dead_domain_precheck(spec.name, question)
                if _dd_msg:
                    # ── v2026-08-18(doc·resilient_search §5.3) · 死域自动转存档链 ──
                    # 病根(musiccolumbus 实证): 域名被熔断后 agent 只拿到"换路建议"
                    # 文本·却继续同义改写硬搜(换词不换访问方式)。修: fetch_url_smart
                    # 撞死域时【代码层自动改走存档链】(t_query_web_archive 内部
                    # CDX→archive.ph 多源降级·自带死域感知)·拿到快照即落 evidence
                    # 返回·不依赖 LLM 听话。每域名每题只自动救一次(有界执行)·
                    # 救不回才回退原拦截消息。fail-safe: 任何异常回退原行为。
                    if spec.name == "fetch_url_smart":
                        try:
                            _rescue_host = sorted(_dd_hosts or [""])[0]
                            _tried = s.setdefault("_auto_archive_tried", set())
                            if _rescue_host and _rescue_host not in _tried:
                                _tried.add(_rescue_host)
                                import qa_tool_impls as _qti_r
                                _url_r = str(question or "").strip()
                                _ev_before = len(s.get("evidence") or [])
                                _rescue_out, _rescue_meta = _qti_r.run_delegated(
                                    _qti_r.t_query_web_archive, _url_r, s, timeout_s=75)
                                _ev_after = len(s.get("evidence") or [])
                                if _rescue_out and _ev_after > _ev_before:
                                    _summary_r = (
                                        f"[fetch_url_smart→自动转存档] 目标域 {_rescue_host} 在死域"
                                        f"熔断中(连接层连续失败)·已自动改走存档链拿到历史快照并落 evidence"
                                        f"(via=query_web_archive 多源降级)·零额外步数。\n" + str(_rescue_out))
                                    _log_tool(qid, spec.name,
                                              {"question": question,
                                               "auto_archive_rescue": True, "host": _rescue_host},
                                              _summary_r, time.time() - t0)
                                    return _summary_r
                        except Exception:
                            pass
                    _log_tool(qid, spec.name, {"question": question, "dead_domain_block": True},
                              _dd_msg, time.time() - t0)
                    return _dd_msg

                # v2026-08-18(doc2·Reflector) · 登录墙/验证码硬阻断·执行前:
                # 登录墙熔断中的域名直接拦截——重试不解决身份问题·换路(转载/存档快照)。
                _wl_msg = _wall_precheck(spec.name, question)
                if _wl_msg:
                    _log_tool(qid, spec.name, {"question": question, "wall_block": True},
                              _wl_msg, time.time() - t0)
                    return _wl_msg

                # ── v72 · 难题不许 0 步/极少步放弃 · finalize 反向卫兵 ──
                # 模型想 finalize,但这是【重型/标准(=需检索)】题、答案却是"放弃
                # /UNKNOWN"、且真检索类工具调用数不够——把它推回去先真用工具查。
                # 硬顶已触发(墙钟耗尽)时不拦(那是真到点了该收尾);被拦够上限
                # 次数后也放行(多方真尝试过拿不到,交付诚实 UNKNOWN)。
                if spec.name == "finalize_answer":
                    # v-eff2(病5 墙钟硬门) · 到点后所有质检门让路——治 6e8c0ae1
                    #   "900s 墙被 5 道门轮拦烧到 2313s"。到点=先交卷。
                    _wall_due = _wall_past(s)
                    if _wall_due:
                        try:
                            _emit_gate_event(qid, "wall_yield", "pass",
                                             {"elapsed": round(time.time() - (s.get("_budget") or {}).get("t_start", 0), 1)})
                        except Exception:
                            pass
                    # v13.97(2026-08-20·优化2·门开销分级) · 预算已紧张(未到硬墙但用掉比例高)·
                    #   非关键门(单源对账叙事/格式守卫/Self-Consistency 投票)让路·只留关键门
                    #   (约束对账/grounding/实体漂移)。关键门永远跑(除非硬墙 _wall_due 到点)。
                    _gate_tight = _gate_budget_tight(s)
                    if _gate_tight and not _wall_due:
                        try:
                            _emit_gate_event(qid, "gate_tier_skip", "pass",
                                             {"skip": "noncritical", "reason": "budget_tight"})
                        except Exception:
                            pass
                    # (2026-08-16·韧性"必交卷"根因修) 存下这次 finalize 想交的答案·
                    #   即使下面被门拦(建模歧义/映射表/反向卫兵)·撞墙兜底 _force_degraded_final
                    #   也能把它捞出来落 final——治"有正确答案却因门反复拦+墙钟耗尽→final空"(17b5a6a3
                    #   实证:答案34689在finalize文本却被门拦到墙钟耗尽·final空·deep/native都栽这)。
                    # v15.7(Q0 白卷实证) · final_markdown 空时试 question 参数: agent 常把
                    #   答案文本塞 question;但题面复述不是答案——无问号且不以题面动词开头才存。
                    _fm_try = str(final_markdown or "").strip()
                    if not _fm_try:
                        _q2 = str(question or "").strip()
                        if (_q2 and "?" not in _q2 and not _q2.lower().startswith(
                                ("find", "identify", "what", "which", "who", "list",
                                 "how", "verify", "name the", "in what"))):
                            _fm_try = _q2
                    # ★2026-08-20(Plastic Man 实证) · 别把 agent echo 回来的收尾 hint
                    #   checklist(①②③取反/字面最贴合…)存进 _last_finalize_attempt——
                    #   撞墙兜底会优先取它当答案交出。堵源头(存入点)+ 兜底处(_force_degraded_final)
                    #   双重过滤。fail-safe。
                    if _fm_try and len(_fm_try) >= 2 and not _looks_like_finalize_noise(_fm_try):
                        s["_last_finalize_attempt"] = _fm_try[:_CKPT_TEXT_MAX]
                    # Canonical lifecycle replaces the stack of legacy finalize gates. Once
                    # a run has candidate events, DeliveryService is the only authority;
                    # duplicate gates add latency and can discard an already selected answer.
                    _canonical_delivery = False
                    try:
                        _event_path_delivery = str(s.get("_event_log_path") or "")
                        if _event_path_delivery and os.path.exists(_event_path_delivery):
                            import qa_event_stream as _qes_delivery
                            _canonical_delivery = bool(
                                _qes_delivery.replay_file(_event_path_delivery).candidates)
                    except Exception:
                        _canonical_delivery = False
                    # RC1: in thin mode the model owns finalize. The legacy finalize-blocking
                    # gates below (block_finalize / constraint-mapping / identity-coverage /
                    # self-consistency / cross_verify hard block) are the same veto that the
                    # audit found discarding correct/near-complete answers (XML, the 17-slot
                    # fraction list) until wall-time. Treat thin-mode finalize like canonical
                    # delivery: skip the gates. Quality is judged externally, not by a veto.
                    if bool(s.get("_thin_mode")) or os.environ.get(
                            "MEYO_NATIVE_THIN_LOOP", "") == "1":
                        _canonical_delivery = True
                    # P2(2026-08-27·门=context): 平级门检测【照跑】(不再整段 bypass·否则
                    #   信号既不拦也不告知=丢失)。检测出的 block 经 _gate_or_hint 分流:
                    #   VETO=1 拦截(现行为)·VETO=0 转 hint 进 session·放行(context 透明给模型)。
                    block_msg = "" if (_wall_due or _canonical_delivery) else \
                        _gate_or_hint(s, _maybe_block_finalize(s, task_level, final_markdown),
                                      "block_finalize")
                    if block_msg:
                        # v2026-08-18(doc·anti_false_positive L4) · Pre-Gate 答案存在性校验:
                        #   vault 已锁答案时·拦截消息附"保留答案行"守卫——防 agent 按
                        #   "别硬交"指令把答案改写成纯元评论(Q501 实证: 到手的分被 gate 扔掉)。
                        try:
                            import answer_vault as _av1
                            block_msg = _av1.append_vault_guard(s, block_msg)
                        except Exception:
                            pass
                        _log_tool(qid, "finalize_answer", {"blocked": True}, block_msg, time.time() - t0)
                        return block_msg
                    # v108(M3) · 约束↔实现映射表审计: 用过代码且有口径约束时·
                    # finalize 叙事里必须有映射表(只拦一次·补表即放)。
                    _cm_block = "" if (_wall_due or _canonical_delivery) else \
                        _gate_or_hint(s, _constraint_mapping_gate(s, final_markdown), "constraint_mapping")
                    if _cm_block:
                        try:
                            import answer_vault as _av2
                            _cm_block = _av2.append_vault_guard(s, _cm_block)
                        except Exception:
                            pass
                        _log_tool(qid, "finalize_answer", {"blocked": True, "gate": "constraint_mapping"},
                                  _cm_block, time.time() - t0)
                        return _cm_block
                    # v2026-08-18(用户拍板·四态身份关系 + claim 级证据覆盖率)·
                    #   直接对症 BrowseComp 锁错实体: finalize 前把题面拆成 N 条约束·
                    #   对候选答案(实体)统计 claim 级证据覆盖率·写四态身份关系进
                    #   session["identity_states"]。覆盖率过低(凭部分匹配)→ 不能当
                    #   confirmed 交·拦一次逼补证据(治锁错实体);probable~confirmed
                    #   之间不拦(标注不确定度而非弃权)。与映射表门配合不重复(那门查
                    #   约束↔实现映射存在性·这门查候选实体每条约束到底有没有证据)。
                    #   只拦一次(打标)·fail-safe 放行。
                    # (2026-08-27 context not control·实体核验变可选) identity_coverage 是
                    #   BC"锁错实体"专治的重型核验(2138行·每次finalize跑=耗时)。VETO默认关后
                    #   它只提醒不拦→跑重逻辑却不拦=浪费。默认旁路(LEAN_MODE默认1):实体核验降级为
                    #   模型可主动调的能力·不再每次finalize强制跑。MEYO_LEAN_MODE=0 恢复强制(BC攻坚)。
                    _lean = os.environ.get("MEYO_LEAN_MODE", "1") == "1"
                    if _lean:
                        _ic_block = ""
                    else:
                        _ic_block = "" if (_wall_due or _canonical_delivery) else \
                            _gate_or_hint(s, _identity_coverage_gate(s, question, final_markdown), "identity_coverage")
                    if _ic_block:
                        try:
                            import answer_vault as _av_ic
                            _ic_block = _av_ic.append_vault_guard(s, _ic_block)
                        except Exception:
                            pass
                        _log_tool(qid, "finalize_answer",
                                  {"blocked": True, "gate": "identity_coverage"},
                                  _ic_block, time.time() - t0)
                        return _ic_block
                    # v108b(M3c) · 叙事级单源对账门: 叙事里单一域名支配全部数据出处且
                    # 无对账结论 → 打回一次(与 M3b 累计器互补·抓 sandbox 复用/本地文件场景)。
                    # v13.97(优化2) · 非关键门·预算紧张(_gate_tight)时让路·省重跑开销。
                    _nr_block = "" if (_wall_due or _gate_tight or _canonical_delivery) else \
                        _gate_or_hint(s, _narrative_single_source_gate(s, final_markdown), "narrative_single_source")
                    if _nr_block:
                        try:
                            import answer_vault as _av3
                            _nr_block = _av3.append_vault_guard(s, _nr_block)
                        except Exception:
                            pass
                        _log_tool(qid, "finalize_answer", {"blocked": True, "gate": "reconcile_narrative"},
                                  _nr_block, time.time() - t0)
                        return _nr_block
                    # v2026-08-18(doc·Q3 Trolltunga) · 提交前格式验证层:
                    #   答案对但形式错(题面要数值而答案行无数字/引号包裹)会被评分器
                    #   判错——内容对·形式错=沉默丢分。只拦明确失配·每题最多一次。
                    #   wants_number 用 _question_wants_number 单一真相源。fail-safe。
                    try:
                        import answer_vault as _av4
                        _fmt_block = "" if (_wall_due or _canonical_delivery) else _av4.format_guard(
                            s, final_markdown,
                            wants_number=_question_wants_number(question))
                        # v13.97(优化2) · 格式门为低价值门:预算紧张(_gate_tight)时【降级为
                        #   只警告不拦截】——把格式提示追加进 final(评分器仍能看到答案)·不再
                        #   打回重跑烧墙钟。预算宽裕时保持原拦截行为(每题最多一次)。
                        if _fmt_block and _fmt_block.strip().startswith("[格式验证"):
                            if _gate_tight:
                                final_markdown = final_markdown + _fmt_block
                                try:
                                    _emit_gate_event(qid, "format_guard", "warn_only",
                                                     {"reason": "budget_tight"})
                                except Exception:
                                    pass
                            else:
                                _log_tool(qid, "finalize_answer",
                                          {"blocked": True, "gate": "format_guard"},
                                          _fmt_block, time.time() - t0)
                                return _fmt_block
                        elif _fmt_block:
                            final_markdown = final_markdown + _fmt_block
                    except Exception:
                        pass

                    # ── 机制2: Self-Consistency Vote·震荡题多次采样投票(治轮间波动)──
                    #   只对【震荡/低置信】题启用(判别器判平局 / 覆盖率中等无 confirmed /
                    #   ALP partial)——非震荡题不跑 N 次·省耗时。触发时对【最终答案生成】
                    #   跑 N 次独立采样(_neutral_stepwise_finalize 作 sampler)·实体级
                    #   投票 + 证据过滤·consensus>=0.6 锁定·不足标低置信交最高票。fail-safe。
                    try:
                        import identity_coverage as _icv
                        # v13.97(优化2) · Self-Consistency 多次采样投票=高开销门(跑 N 次 LLM)·
                        #   预算紧张(_gate_tight)时【跳过】·省多次采样开销。非震荡题本就不跑。
                        if not _wall_due and not _canonical_delivery and not _gate_tight and _icv._sc_enabled() and not (s.get("_budget") or {}).get("_sc_voted"):
                            _ic_res = s.get("_ic_last_result") or {}
                            _alp_res = s.get("_alp_last") or {}
                            _na = s.get("_ic_next_action") or {}
                            if _icv.should_self_consistency(
                                    ic_result=_ic_res, next_action=_na,
                                    alp_result=_alp_res):
                                (_rs_sessions.ensure_budget(s))["_sc_voted"] = True
                                # 证据文本(供 sampler 重生成最终答案)·从 session 证据集汇总
                                try:
                                    _ev_list = _icv.collect_evidence(s)
                                    _ev_txt = "\n".join(
                                        "- %s" % str(e.get("text") or "")[:500]
                                        for e in _ev_list[:20])[:8000]
                                except Exception:
                                    _ev_txt = ""

                                def _sc_sampler():
                                    return _neutral_stepwise_finalize(
                                        question, _ev_txt, timeout=45)
                                _vote = _icv.self_consistency_vote(
                                    s, question, _sc_sampler,
                                    base_answer=final_markdown or "",
                                    vote_key=_vote_key)
                                _emit_gate_event(qid, "self_consistency",
                                                 "low_conf" if _vote.get("low_confidence")
                                                 else "consensus",
                                                 {"consensus": _vote.get("consensus"),
                                                  "n": _vote.get("n_samples"),
                                                  "valid": _vote.get("n_valid"),
                                                  "triggered": _vote.get("triggered")})
                                if _vote.get("triggered"):
                                    # 共识达标→取共识答案(可能与单次不同·抗轮间波动);
                                    #   低置信→保留原答案并追加不确定度标注(不弃权)。
                                    if not _vote.get("low_confidence") and _vote.get("answer"):
                                        final_markdown = _vote["answer"]
                                    final_markdown = _icv.sc_annotate(
                                        final_markdown, _vote)
                    except Exception:
                        pass

                # ── G3(2026-08-12 错题归因) · 感知冲突仲裁 · 交卷前用"证据最强候选" ──
                # 病根(分数图题):多次 VLM 读取被 agent 捏合成错版本交卷·而某一次其实读对了。
                # 交卷时若答案与"证据最强的历史感知候选"明显不同·软提示 agent 复核(非强改·
                # 保自主性)。截断/收尾时这条尤其重要:提交读得最全那次·别提交捏合的。
                if spec.name == "finalize_answer":
                    try:
                        _cand, _meta = _strongest_perception_candidate(s)
                        if _cand and _meta and _meta.get("n_good", 0) >= 2:
                            _fm = (final_markdown or "").strip()
                            # 简易差异判断:最强候选的关键片段没出现在当前答案里
                            _cand_head = _cand.strip()[:80]
                            if _cand_head and _cand_head[:40] not in _fm:
                                s["_g3_note"] = (
                                    f"\n\n[G3感知仲裁] 你对同一图做了 {_meta['n_good']} 次有效读取——"
                                    f"多次读取是【独立样本】·不是拿来捏合的原料。当前答案若是"
                                    f"把多次结果拼接/融合而成·很可能拼错顺序或混入错版本。"
                                    f"请核对:读得【最全/信息最多】的那一次(strength={_meta['strength']})"
                                    f"内容是『{_cand_head}...』——若它更可信·直接用它·别用捏合版。"
                                    f"两次读取冲突时应回源重读争议区(裁剪放大)·而非 blend。"
                                )
                    except Exception:
                        s["_g3_note"] = ""

                # ── v13.92/v13.95 · OP-3 cross_verify · 高风险答案多模型交叉验证 ──
                # v13.95 升级:disagree 不再只是软 hint，而是硬拦截第一次 finalize，
                # 逼 agent 真正重新验算（最多拦 1 次，防死循环）。
                # 原理:cross_verify 第二模型不同意 → 说明答案有实质性争议 →
                # 应该重新用独立方法验算而不是直接交付。被拦第 2 次时放行（防无限循环）。
                # fail-safe: 任何异常都静默跳过，不破坏主流程。
                if spec.name == "finalize_answer" and not _canonical_delivery:
                    try:
                        from cross_verify import maybe_cross_verify as _mcv
                        _cv = _mcv(
                            question=question,
                            main_answer=final_markdown or "",
                            evidence=s.get("evidence") or [],
                        )
                        # 不管触发与否,都把 reason 记到 session 供后续 trace
                        s["_last_cross_verify"] = _cv
                        if _cv.get("triggered") and _cv.get("agreed") is False:
                            # v166 · Conservative Mode(文档《anti_false_positive_agent
                            #   engine 最后一公里门》L5·When in doubt, preserve the
                            #   answer): vault 已锁定【强答案】(strength>=2·行首答案
                            #   标签断言)时·cross_verify 的 disagree 不再硬拦截、
                            #   不作废前沿、不触发题意重假设——只附元注释放行。
                            # 病根(v165f·Q1 弃权实证): disagree→intent_rehypo→
                            #   "旧前沿作废"→路径发散→硬墙强制收尾→诚实弃权/
                            #   答错——把 agent 已挖到的答案被治理层自毁。
                            # 代价权衡: 误杀一个真答案的代价 ≥ 放行一个错答案
                            #   (文档: 15-52% 幻觉率中相当一部分不是模型编错·
                            #   而是治理架构让正确答案无法到达评分器)。
                            try:
                                import answer_vault as _av_cv
                                _v_cv = _av_cv.get(s)
                            except Exception:
                                _v_cv = None
                            # v127(2026-08-19 · 用户实证修复) · 病根: 原判据只看
                            # "vault 里有没有强答案"·完全不检查【vault 锁的答案是不是
                            # 就是本次被 cross_verify 反对的这同一个候选】。二者一旦
                            # 相同·这条 Conservative Mode 就变成了"用我已经认定它是对
                            # 的这件事·去豁免对它的核验"——逻辑自证循环，等价于
                            # disagree 永远不可能生效(只要 agent 先在中间过程随口留下
                            # 一句"answer is X"就能让后续对 X 本身的一切质疑全部失效)。
                            # 修复: Conservative Mode 只在【vault 答案与当前被反对的
                            # final_markdown 候选不同】时才生效(说明 vault 锁的是一个
                            # 独立、更早形成的强答案，值得保护不被新一轮验证扰乱)；
                            # 如果两者就是同一个候选，则不豁免，走正常硬拦截逻辑。
                            _fm_now = (final_markdown or "").strip().lower()
                            _vault_ans_now = str((_v_cv or {}).get("answer") or "").strip()
                            # v127 · 用统一的 _candidate_similarity 判据(而非粗糙前缀
                            # 截断)判断 vault 锁的答案是否就是当前被反对的候选本身——
                            # 避免"只共享属名前缀的邻近候选"(如 Solanum laciniatum
                            # vault vs hermannii 当前候选)被误判成"同一个"，从而错误
                            # 地豁免掉本该生效的硬拦截。
                            _vault_is_the_disputed_candidate = bool(
                                _vault_ans_now and _fm_now
                                and (_vault_ans_now.lower() in _fm_now
                                     or _candidate_similarity(_vault_ans_now, _fm_now))
                            )
                            if (_v_cv and int(_v_cv.get("strength") or 0) >= 1
                                    and not _vault_is_the_disputed_candidate):
                                # v166b · 门槛放宽 2→1: 实测工具摘要常带前缀文字·
                                # "Exact Answer:" 行首强标记常退化成句中 mid 标记;
                                # 而 cross_verify 拦截的实证代价(路径发散+弃权)
                                # 远大于放行(第二模型也不知道真值·只是异见)。
                                s["_cv_conservative_note"] = True  # v166 · 防后续分支覆盖
                                s["_cross_verify_note"] = (
                                    f"\n\n[cross_verify · conservative] 第二模型不同意"
                                    f"(原因: {_cv.get('reason', '?')[:100]})·但答案保险箱"
                                    f"已锁定强答案『{str(_v_cv.get('answer'))[:60]}』——按保守模式"
                                    f"保留答案交付(元注释记录异见·不改写答案)。若你有充分新证据"
                                    f"可在下次 finalize 主动修正·但不会被强制重验。")
                                _log_tool(qid, "finalize_answer",
                                          {"cv_conservative": True,
                                           "vault": str(_v_cv.get("answer"))[:60]},
                                          s["_cross_verify_note"].strip(), time.time() - t0)
                                _cv_block_count = 99  # 哨兵: 跳过硬拦截·落入放行分支
                            else:
                                _cv_block_count = s.get("_cv_block_count", 0)
                            # v127(2026-08-19 · 用户实证:候选震荡修复) · 病根: 原逻辑
                            # "只拦一次·第二次无脑放行"——若 agent 换的第二个候选和
                            # 第一个被拦的候选本质是【同一簇】(如同属邻近种/拼写相近
                            # 的另一支队伍的变体)·这次放行就是把一个刚被拒绝过的错误
                            # 判断原样放过去，等价于没拦过。用【当前候选是否与上次被
                            # 拦的候选高度重叠】区分"真的换了新候选"vs"换汤不换药":
                            # 前者才计入放行配额，后者继续硬拦(但设总上限 3 次防真死循环)。
                            _prev_blocked_answer = str(s.get("_cv_last_blocked_answer") or "").strip()
                            _fm_cur = (final_markdown or "").strip().lower()
                            # v127 · 同样改用统一 _candidate_similarity 判据，避免
                            # "solanum laciniatum"(上次被拦) vs "solanum hermannii"
                            # (本次候选)因共享属名前缀被误判成"同一簇/换汤不换药"，
                            # 从而错误地继续硬拦一个其实【已经换了新候选】的答案。
                            _same_cluster_as_last_block = bool(
                                _prev_blocked_answer and _fm_cur
                                and (_prev_blocked_answer.lower() in _fm_cur
                                     or _candidate_similarity(_prev_blocked_answer, _fm_cur))
                            )
                            _cv_hard_cap = int(os.environ.get("MEYO_CV_BLOCK_CAP", "3"))
                            if _cv_block_count < 1 or (_same_cluster_as_last_block
                                                        and _cv_block_count < _cv_hard_cap):
                                # v13.95 · disagree → 硬拦截，逼重新验算(真换了新候选只拦
                                # 一次；换汤不换药/候选和上次被拦的高度重叠则继续拦，
                                # 直到总次数达上限才放行，防止在同一错误候选簇里空转)
                                s["_cv_block_count"] = _cv_block_count + 1
                                s["_cv_last_blocked_answer"] = _fm_cur[:200]
                                _block_reason = _cv.get('reason', '?')[:150]
                                _alt_ans = _cv.get('alt_answer', '')
                                _alt_hint = f"第二模型的答案是『{_alt_ans[:80]}』。" if _alt_ans else ""
                                # v99t·机制B(用户:发现答案可能不对·在不知正确答案前提下·
                                #   能不能自己反思'题意理解错了吗'·生成多个可能意图重试)。
                                #   答案存疑不只"换方法重算"(那假设题意对了)·还要回头【质疑题意本身】·
                                #   从题面歧义生成【其他合理理解】各验一次。不知正确答案·只从歧义生成假设。
                                _alt_intents = ""
                                if not s.get("_intent_rehypo_done"):
                                    s["_intent_rehypo_done"] = True
                                    try:
                                        _axes2 = {k: v for k, v in
                                                  ((s.get("triage") or {}).get("ambiguity_axes") or {}).items()
                                                  if v and str(v).strip()}
                                        _rehypo_prompt = (
                                            "第二模型不认同这个答案。在【不知道正确答案】的前提下·"
                                            "帮解题者回头质疑:是不是【题意理解】有另一种可能?\n"
                                            "从题面歧义生成2-3个【其他合理理解】(不同实体/时间窗/集合范围/口径/"
                                            "聚合方式)·每个一句话。不要给答案·只给'题目可能是这个意思'的候选。\n"
                                            f"题目: {(s.get('question') or '')[:280]}\n"
                                            + (f"已知歧义轴: {_axes2}\n" if _axes2 else "")
                                            + f"当前答案(存疑): {str(answer)[:100]}\n"
                                            "输出JSON: {\"alt_interpretations\":[\"理解1\",\"理解2\"]}·只输出JSON。")
                                        import reflective_mind as _RM_rh
                                        _rh_raw = _RM_rh._mm().ask("claude-sonnet", _rehypo_prompt,
                                                                   timeout=50, max_tokens=350)
                                        import json as _json_rh
                                        _ri, _rj = _rh_raw.find("{"), _rh_raw.rfind("}")
                                        if _ri >= 0 and _rj > _ri:
                                            _alts = (_json_rh.loads(_rh_raw[_ri:_rj+1])
                                                     .get("alt_interpretations") or [])
                                            if _alts:
                                                _rehypothesized = _register_intent_hypotheses(
                                                    s, _alts, reason=_block_reason)
                                                _alt_intents = (
                                                    "\n  4. **回头质疑题意**(可能不是算错·是题意理解偏了):"
                                                    "题目也许是这些意思之一——\n     "
                                                    + "\n     ".join(f"· {a[:120]}" for a in _alts[:3])
                                                    + ("\n     已把这些理解登记为待区分假设：旧前沿已作废，下一步必须找能区分它们的证据/计算。"
                                                       if _rehypothesized else "\n     按这些其他理解各验一次·看哪个更自洽/更像出题人要的·再定答案。"))
                                                _emit_gate_event(qid, "intent_rehypothesis", "cross_verify",
                                                                 {"n_alts": len(_alts), "replanned": bool(_rehypothesized)})
                                    except Exception:
                                        _alt_intents = ""
                                _cluster_hint = (
                                    "\n  ⚠️ 你这次的候选和上次被拦的候选高度重叠(疑似同一簇邻近候选/换汤不换药)"
                                    "——换个措辞不算真正重新验证，必须真正跳出这个候选簇，去找与题面约束"
                                    "更贴合的其他候选。"
                                ) if _same_cluster_as_last_block else ""
                                _cv_block_msg = (
                                    f"[cross_verify · 硬拦截] 第二模型不同意当前答案。"
                                    f"原因: {_block_reason}。{_alt_hint}"
                                    f"\n**必须重新验证**（不能直接交付有争议的答案）:"
                                    f"\n  1. 用完全不同的方法独立算一遍（如换公式/换角度/反向验证）"
                                    f"\n  2. 把两个模型的分歧点明确找出来：'哪一步推理不同'"
                                    f"\n  3. 验证后如仍坚持原答案，且是【真正不同】的新候选，"
                                    f"下次 finalize_answer 会放行；若换汤不换药会继续被拦"
                                    f"（总上限 {_cv_hard_cap} 次，到点强制放行防死循环）"
                                    + _cluster_hint
                                    + _alt_intents
                                    + f"\n  不要原样重新 finalize，必须先做独立验证。"
                                )
                                _log_tool(qid, "finalize_answer", {"cv_blocked": True}, _cv_block_msg, time.time() - t0)
                                return _cv_block_msg
                            else:
                                # 已拦够次数(真换了新候选算1次·同簇空转不计入放行·
                                # 或已达硬上限) → 放行，但给 hint
                                _cv_note = (
                                    f"\n\n[cross_verify] 第二模型不同意·已拦过{_cv_block_count}次·本次放行。"
                                    f"原因: {_cv.get('reason', '?')[:100]}。"
                                    f"(你已重新验证过，放心交付你认为正确的版本)"
                                )
                                if not s.get("_cv_conservative_note"):  # v166 · 保守模式 note 不被覆盖
                                    s["_cross_verify_note"] = _cv_note
                        elif _cv.get("triggered") and _cv.get("agreed") is True:
                            _cv_note = (
                                f"\n\n[cross_verify] 第二模型 ✅ 同意主答案·{_cv.get('reason','')[:100]}"
                                f" · 放心交付。"
                            )
                            s["_cross_verify_note"] = _cv_note
                        else:
                            s["_cross_verify_note"] = ""  # 未触发 / verifier 不可用, 不打扰
                    except Exception as _cve:
                        # cross_verify 任何异常 → 静默,不破坏 finalize
                        s["_cross_verify_note"] = ""

                attempt_map = s["_attempt_count"]
                # ── v108(M1b·GAIA-165复盘) · 重试预算按【参数指纹】计数 ──
                # 病根实证(f2fe/0b26): attempt_map 只按工具名计数——第一个 query
                # 失败耗尽预算后·更精确的新 query(完全不同的检索意图)被连坐拦截;
                # 0b26 里两个更精确的 domain 分学科 query 全被旧预算拦掉。修:
                # 不同 query/URL = 新尝试·不占旧 query 的重试预算;同一 query 重试
                # 仍受预算约束。另加【每工具独立 query 数硬顶】防无限换词枚举
                # (步数/墙钟之外的第二道总量核·宽松=12·远大于旧实际可用数)。
                _qfp = _arg_fingerprint(question)
                _retry_key = f"{spec.name}::{_qfp}" if _qfp else spec.name
                try:
                    _seen_fp = s.setdefault("_attempt_fingerprints", {}).setdefault(spec.name, {})
                    if _qfp and _qfp not in _seen_fp:
                        _seen_fp[_qfp] = True
                        _nd = len(_seen_fp)
                        if _nd > _DISTINCT_QUERY_HARD:
                            _log_tool(qid, spec.name, {"question": question},
                                      f"[query多样性硬顶] 本题已用 {_nd} 个不同 query 调 {spec.name}·"
                                      "超过上限——继续换词是在撒网不是在解题。改用:①已有证据"
                                      "extract_cards/run_factual_extract 收敛 ②换能力(抓已知URL/"
                                      "算/看图) ③证据够就 finalize_answer(可诚实低置信)。",
                                      time.time() - t0)
                            return (f"[query多样性硬顶] 本题已用 {_nd} 个不同 query 调 {spec.name}·"
                                    "超过上限——继续换词是在撒网不是在解题。改用:①已有证据"
                                    "extract_cards/run_factual_extract 收敛 ②换能力(抓已知URL/"
                                    "算/看图) ③证据够就 finalize_answer(可诚实低置信)。")
                except Exception:
                    pass
                attempt = attempt_map.get(_retry_key, 0)

                # retryable 工具先过一遍自适应重试预算：资源紧张/任务不紧急
                # 到了预算上限，就不再真正执行，直接告诉 LLM 别再试了。
                if spec.retryable and attempt > 0:
                    last_empty = s.get(f"_last_empty_{_retry_key}", False)
                    decision = qa_retry_policy.decide_retry_budget(
                        task_level=task_level, urgency=urgency, cost_tier=spec.cost_tier,
                        attempt=attempt, last_result_empty=last_empty,
                    )
                    if not decision["allow_retry"]:
                        # v127 · 固定阈值拦截也是一次自主策略决策——入账本供效果归因:
                        # 拦下重试到底是有动敆f还是在关键证据上过早止损·由本题收尾
                        # (_record_frontier_outcome 回填 outcome)和后续同类题统计检验。
                        _record_policy_decision(
                            s, "retry_budget_block",
                            state_features={
                                "tool": spec.name, "attempt": attempt,
                                "budget": decision.get("budget"),
                                "last_result_empty": bool(last_empty),
                                "task_level": task_level, "urgency": urgency,
                            },
                            reason=str(decision.get("reason") or "")[:200],
                            expected_gain="save_wallclock_pivot_to_alternative",
                        )
                        summary = (f"已达重试预算上限(已试{attempt}次 · {decision['reason']})，"
                                   "不再执行，请改用已有结果/通识继续往下走，不要再调用这个工具")
                        _log_tool(qid, spec.name, {"question": question}, summary, 0.0)
                        return summary

                attempt_map[_retry_key] = attempt + 1

                # ── v-eff(2026-08-18·病3 抓取缓存) · 同 URL 抓过就复用不重抓 ──
                # 对照实验:引擎对同页 fetch 4-5 次浪费墙钟。修:session 级 URL→正文
                #   缓存(fetch_url_smart 且 question 是纯 URL 时)。命中直接返回缓存正文·
                #   零网络 I/O。fail-safe:任何异常退回真抓。env MEYO_ENGINE_EFFICIENCY 关。
                _cache_key = None
                if (MEYO_ENGINE_EFFICIENCY and spec.name == "fetch_url_smart"):
                    try:
                        _u = str(question or "").strip()
                        if _u.startswith("http") and " " not in _u and len(_u) < 2000:
                            _cache_key = _u
                            _fc = s.setdefault("_fetch_cache", {})
                            _hit = _fc.get(_cache_key)
                            if _hit is not None:
                                _summary_c = (
                                    "[抓取缓存命中·零网络] 本题此前已抓过该 URL·直接复用已抓正文"
                                    "(不重抓·省墙钟)。若需要最新内容才重抓·换个 URL/加锚点。\n"
                                    + str(_hit))
                                _log_tool(qid, spec.name,
                                          {"question": question, "fetch_cache_hit": True},
                                          _summary_c, time.time() - t0)
                                return _summary_c
                    except Exception:
                        _cache_key = None
                # executor 签名兼容: finalize_answer 多一个 final_markdown 关键字参数，
                # 其余工具只吃 (question, session)。用 inspect 按需透传，避免
                # TypeError。
                sig = inspect.signature(spec.executor)
                kwargs = {}
                if "final_markdown" in sig.parameters:
                    kwargs["final_markdown"] = final_markdown
                _calc_trace_before = len(s.get("calculation_trace") or [])
                try:
                    summary = spec.executor(question, s, **kwargs)
                except Exception as e:
                    # 降级响亮化(2026-08-12·最高等级事故修复·治self-heal静默吞崩溃):
                    # 事故中judge闭包崩→静默降级→任务"完成"exit0→回测在残废系统跑污染数据。
                    # 规则:任何降级路径打 [DEGRADED] 标·并记 session._degraded_runs·
                    # 回测/评分统计据此自动排除污染 run(否则每个指标都在被骗)。
                    summary = f"[DEGRADED:工具执行异常] {type(e).__name__}: {str(e)[:150]}"
                    try:
                        s.setdefault("_degraded_runs", []).append(
                            {"tool": spec.name, "err": f"{type(e).__name__}:{str(e)[:100]}", "ts": _now_iso()})
                    except Exception:
                        pass

                # 确定性路由要求“本题的数值参与了这次计算”，而不是任意计算即可解锁。
                # 否则模型可用单位正方形等无关算式伪造 calculation_trace 后继续盲看图。
                if (spec.name == "sympy_calc"
                        and s.get("_budget", {}).get("_forced_tool") == "sympy_calc"
                        and s.get("_budget", {}).get("_vlm_extracted_values")):
                    _new_trace = (s.get("calculation_trace") or [])[_calc_trace_before:]
                    if _new_trace and not _calculation_trace_uses_extracted_values(s, _new_trace[-1]):
                        del s["calculation_trace"][_calc_trace_before:]
                        s["_budget"]["_calc_trace_rejected"] = "inputs_do_not_use_task_extracted_values"
                        summary = (
                            "[确定性计算路由·无关计算拒绝] 这次计算没有使用至少两项本题从图中提取的"
                            "非零数值，不能作为本题推导链。已撤销 calculation_trace；请用已确认的"
                            "边长/坐标建模计算，或调 document_wall 说明拓扑结构为何无法确定。"
                        )

                # 2026-08-13 · 工具输出信号驱动自主工具触发(Signal-Driven Tool Routing):
                # 某些工具输出的特殊信号，意味着下一步有明确的"正确动作"，而不应等 agent 自己猜。
                # 原则: 只在信号极明确时设 _forced_tool，避免过度干预 agent 自主性。
                if isinstance(summary, str) and s.get("_budget") is not None:
                    _bgt = s["_budget"]
                    # (2026-08-27 瘦身) 原 S1 ortho_polygon→solve_open_constraint 强制路由已删:
                    #   纯 GAIA 多边形题过拟合·全历史 tool 日志真实调用 0 次·且是硬编码 _forced_tool
                    #   夺自主(违反给context不加control)。整条 polygon 专用链一并删。
                    # S2: 连续 shell_exec > 25 次且还在找答案 → 提示应用 write_capability 写一次性脚本
                    if (spec.name == "shell_exec" and
                            _bgt.get("shell_exec_total", 0) > 25 and
                            not _bgt.get("_write_cap_suggested") and
                            "exit_code=0" not in str(summary)[:50]):
                        _bgt["_write_cap_suggested"] = True
                        summary = summary + (
                            "\n[⚠️ shell循环检测] 已 shell_exec 超过25次未解决。"
                            "建议: 调 `write_capability(problem_desc='描述你要解决的计算/处理任务')` "
                            "让LLM写专用代码工具，比反复手写 shell 脚本更可靠。"
                            "或调 `self_diagnose` 看是否有系统性策略缺陷。"
                        )
                    # S3 v2: 工具返回 error/exception 信号 → 提示用 auto_diagnose_and_solve
                    elif ("Traceback" in summary or "error:" in summary.lower() or
                          "exception" in summary.lower() or "failed" in summary.lower()):
                        _err_count = _bgt.get("_tool_error_count", 0) + 1
                        _bgt["_tool_error_count"] = _err_count
                        if _err_count >= 3 and not _bgt.get("_auto_diag_suggested"):
                            _bgt["_auto_diag_suggested"] = True
                            summary = summary + (
                                "\n[★自动路由] 连续工具错误检测到→建议调 "
                                "`auto_diagnose_and_solve(question='用户问题', why_failed='失败原因')` "
                                "让系统自动诊断需要什么算法→造工具→解题。"
                                "不再靠反复试错·直接创建专用工具。"
                            )
                    # S4 v2: budget 快耗尽但仍无 calculation_trace → 强制 write_capability
                    elif (_bgt.get("round", 0) >= 8 and
                          not s.get("calculation_trace") and
                          not _bgt.get("_force_cap_write")):
                        _bgt["_force_cap_write"] = True
                        _bgt["_forced_tool"] = "write_capability"
                        _bgt["_force_gate_count"] = 0
                        summary = summary + (
                            "\n[★自动路由] 预算快耗尽但无计算痕迹→强制下一步调 "
                            "`write_capability(problem_desc='描述解题需要的计算')` "
                            "让 LLM 生成专用算法代码·别再靠搜索/泛读了。"
                        )
                    # S5 v2: finalize_answer 但答案为空 → 强制 auto_diagnose_and_solve
                    elif (spec.name == "finalize_answer" and
                          not str(summary).strip() and
                          not _bgt.get("_force_auto_diag")):
                        _bgt["_force_auto_diag"] = True
                        summary = (
                            "[★自动路由] finalize_answer 返回空→强制调 "
                            "`auto_diagnose_and_solve` 自动诊断+造工具+解题。"
                            "别再空答·用工具解题。"
                        )

                # v13.92 · cross_verify_note 追加(若上方已标记)
                if isinstance(summary, str) and s.get("_cross_verify_note"):
                    summary = summary + s["_cross_verify_note"]
                    s["_cross_verify_note"] = ""  # 用一次就清,避免重复
                # G3 · 感知仲裁 note 追加(若上方已标记)
                if isinstance(summary, str) and s.get("_g3_note"):
                    summary = summary + s["_g3_note"]
                    s["_g3_note"] = ""
                # G4 · 失败签名熔断(2026-08-12): 同一类故障(SSL/429/quota/404)连发达阈值
                # → 追加"切策略维度"提示·治 YouTube三连/ScienceDirect 同姿势死磕。
                if isinstance(summary, str):
                    _fs_note = _track_failure_signature(s, spec.name, summary)
                    if _fs_note:
                        summary = summary + _fs_note

                # v108b(M3b) · 通用分发尾部的单源对账(累计器+一次性提醒)。
                # fetch_url_smart 等无专属 handler 的数据工具走这里;完整
                # question 文本只在此刻可得(落盘会截断)。
                if (isinstance(summary, str) and
                        spec.name in _DATA_FETCH_TOOLS_M3B and
                        "http" in str(question)):
                    try:
                        _sb_m3b = s.get("_budget")
                        if _sb_m3b is None:
                            _sb_m3b = s["_budget"] = {}
                        _m3b_record_host(_sb_m3b, spec.name, str(question), summary)
                        _rc_note = _single_source_reconcile_nudge(s)
                        if _rc_note:
                            summary = summary + _rc_note
                    except Exception:
                        pass

                # 记录这次搜索类工具是不是"空转"(0 新增)，给下一次重试判断用
                # v108(M1b): 按 fingerprint 键记——query A 空转不该收紧 query B 的预算。
                if spec.category in ("search", "deep_research") and isinstance(summary, str):
                    s[f"_last_empty_{_retry_key}"] = ("新增 0" in summary)

                # ── v-eff(2026-08-18·病3) · 抓取成功→写入 session 缓存 ──
                #   只缓存看起来真拿到正文的结果(非报错/非拦截)·下次同 URL 命中。
                if (_cache_key and isinstance(summary, str) and len(summary) > 200
                        and not summary[:40].startswith(("[工具", "[已拦截", "[gate", "[死域",
                                                          "[循环卫兵", "[fetch_url_smart→"))):
                    try:
                        _fc2 = s.setdefault("_fetch_cache", {})
                        if len(_fc2) < int(os.environ.get("MEYO_FETCH_CACHE_MAX", "64")):
                            _fc2[_cache_key] = summary[:20000]
                    except Exception:
                        pass

                # ── v-eff(2026-08-18·病2) · 无效检索路径及时换【结构性不同渠道】 ──
                # 对照实验铁证:CC 遇 2 次空手立刻从 dblp 换到 crossref;引擎在同渠道反复
                #   猜 PID 撒网 60+ 次。修:按【检索意图】(query 归一化)记同一意图连续空手
                #   次数·达阈(默认 2)且没换过结构性不同渠道→注入"换渠道"硬提示(dblp→
                #   crossref→wikidata→web·换访问方式而非换关键词再撒网)。触发比旧机制敏感。
                #   通用·fail-safe·env MEYO_CHANNEL_SWITCH_THRESH 可调·MEYO_ENGINE_EFFICIENCY 关。
                if (MEYO_ENGINE_EFFICIENCY and isinstance(summary, str)
                        and spec.category in ("search", "deep_research", "retrieve")
                        and spec.name in ("run_parallel_search", "run_deep_research_swarm",
                                          "run_factual_extract", "fetch_url_smart",
                                          "traverse_web")):
                    try:
                        _cs_note = _maybe_channel_switch_nudge(s, spec.name, question, summary)
                        if _cs_note:
                            summary = summary + _cs_note
                    except Exception:
                        pass

                # Candidate observations are committed by the canonical adapter in
                # _log_tool; AnswerVault is projection-only for new runs.

                # v13.97(2026-08-20·优化1·bc实证bc_1786949292) · 【已锁定实体+问属性】
                #   最快路径【前置默认引导】——基于 canonical projection 的 selected 候选·
                #   锁定实体后【立即】(不等连续空手)把 agent 推向"run_parallel_search 直搜
                #   『实体 属性关键词』(AnySearch 二手源一步命中)"·别先 curl 维基/wikidata。
                #   与上面的换渠道 nudge(事后触发)互补;多源分歧时附投票提示治卡死。
                #   通用·每题最多一次·fail-safe·env MEYO_ANYSEARCH_FIRST=0 可关。
                if (isinstance(summary, str)
                        and spec.name in ("run_parallel_search", "run_deep_research_swarm",
                                          "run_factual_extract", "fetch_url_smart",
                                          "traverse_web")):
                    try:
                        _af_note = _maybe_anysearch_first_hint(s, spec.name, question, summary)
                        if _af_note:
                            summary = summary + _af_note
                    except Exception:
                        pass

                # v2026-08-18(doc·wallclock PASTE-lite+遥测) · 搜索落账→后台预热 top URL:
                #   search 的 url 字段≈95% 成为下一步 fetch 参数(producer-consumer 模式)·
                #   趁 LLM 思考的时间重叠提前做网络 I/O·fetch 命中预取缓存=零等待。
                #   纯网络层不碰 session·限流 3 并发·死域/二进制后缀过滤。同时记工具
                #   耗时遥测(时间黑洞定位)。fail-safe。
                try:
                    import wallclock_optim as _wco_p
                    _wco_p.record_tool_time(s, spec.name, time.time() - t0)
                    if spec.category in ("search", "deep_research"):
                        _wco_p.maybe_prefetch_after_search(s)
                except Exception:
                    pass

                # 软顶 nudge 附加在正常摘要之后，不打断这次工具本身的结果，
                # 只是顺带提醒模型"该收敛了"——对应 CCB token_budget_continuation
                # "不腰斩、只提醒"的语义。
                if guard_msg:
                    summary = f"{summary}{guard_msg}"

                _log_tool(qid, spec.name, {"question": question}, summary, time.time() - t0)
                return summary

            _tool_fn.__name__ = spec.name
            _tool_fn.__doc__ = spec.description
            return _tool_fn

        tools.append(_build())

    # v59: 追加 evolution 桥接工具 (ToolSearch + ExecuteTool)
    tools.extend(_make_evolution_tools(qid))

    # v60: 追加通用执行工具 (shell_exec/file_read/file_write)，默认直接
    # 注册——用户明确要求"就地升级默认行为"，不做 opt-in 开关。
    tools.extend(_make_execution_tools(qid))

    # G-cli(2026-08-08): 追加 CLI-Anything 生态桥接工具，放在 desktop_gui 之前
    # 体现优先级(能用现成CLI封装就不用坐标点击兜底)。
    tools.extend(_make_cli_hub_tools(qid))

    # G-gui(2026-08-08): 追加系统级桌面 GUI 工具(截图/点击/输入/按键)，
    # 对标 LongHorizon-Harness computer-use。不可用(非macOS/无权限)时
    # _make_desktop_gui_tools 内部会返回空列表，不影响其余工具正常注册。
    tools.extend(_make_desktop_gui_tools(qid))

    # v60: 追加 NoCode 建站工具 (nocode_create/send/screenshot/deploy)，
    # 让"帮我做个网站/官网/活动页"这类任务能端到端交付，而不是只给代码。
    tools.extend(_make_nocode_tools(qid))

    # G-align(2026-08-07): 追加显式计划工具 + 子代理委托工具。
    tools.extend(_make_plan_tool(qid))       # 维度 C · write_plan
    tools.extend(_make_goal_tools(qid))      # 2026-08-11 · goal-as-tool(自主用/创建持久目标)
    tools.extend(_make_self_evolve_tools(qid))  # 2026-08-11 · self_diagnose(agent自己调教自己·遇卡自诊断机制缺陷)
    tools.extend(_make_metacognition_tools(qid))  # 2026-08-15 · 元认知即工具(reflect_now/recall_experience/remember_lesson/review_intent·agent主动调)
    tools.extend(_make_local_and_reasoning_tools(qid))  # 2026-08-15 · 第2批(search_local_files/deduce/cross_verify_now)
    tools.extend(_make_delegate_tool(qid))   # 维度 B · delegate → task_dispatch

    # 统一(2026-08-07 · 「彻底做完」): 自由子 agent + 整条 pipeline 当子 agent。
    tools.extend(_make_subagent_tool(qid))   # 统一2 · spawn_subagent(独立 system_prompt)
    tools.extend(_make_skill_tools(qid))     # 统一4 · skill_search/skill_load(对标真缺口)
    tools.extend(_make_tool_install_tools(qid))  # P4 · pip_install(搜工具→装→用)
    tools.extend(_make_tool_discovery_tools(qid))  # P6 · github/toolbox 搜索→安装→场景化验证→per-user交付

    # capability_writer: agent 自主生成算法组件（2026-08-13）
    # 让 agent 遇到 open_constraint/VLM盲区/无现成工具时能自己写代码解题
    tools.extend(_make_capability_writer_tools(qid))

    # 注: 曾有【病3 元工具整体下架】(MEYO_TRIM_META_TOOLS)一版·已回滚(2026-08-19·
    #   用户拍板)。回滚原因: native 的本质是"控制编排/持续验证逼近目标的大脑"——
    #   校验不是纯开销·是编排本身。整体删掉 self_calibrate/review_intent/assess_info
    #   _sufficiency 会把"循环内该做的校验能力"一起砍掉·让 loop 退化成裸 ReAct 盲搜
    #   (违背 native 本质)。正解已由【_goal_approach 每步自适应目标逼近自评】
    #   (native_agent_loop·env MEYO_NATIVE_GOAL_APPROACH)承接: 把校验融进每一步
    #   (证据够没够/锁定/矛盾/停或换路·随题灵活·驱动模型而非删工具让它盲选)。
    #   故此处不再下架任何工具——速度靠"循环内验证驱动的及时收敛/换路"治·不靠删校验。

    # ── Progressive Disclosure(学 Hermes tools/tool_search.py:1-45) ───────────
    # 工具多时不把全部 schema 塞给模型·core 只暴露三座桥:search/describe/call。
    # catalog 持有本次 qid 已绑定的完整工具函数；tool_call 复用原函数内的预算/权限/日志，
    # 不绕过 guard。能力仍全在，只在模型需要时按名展开(context not control)。
    _catalog = {}
    for _tool in tools:
        _name = str(getattr(_tool, "name", getattr(_tool, "__name__", "")))
        if _name:
            _catalog.setdefault(_name, _tool)

    def tool_search(query: str, limit: int = 8) -> str:
        """搜索当前会话可用的隐藏工具。返回名称+一句话用途；找到后用 tool_describe 查看参数。"""
        words = [w for w in re.findall(r"[a-zA-Z0-9_一-鿿]+", str(query).lower()) if len(w) > 1]
        ranked = []
        for name, fn in _catalog.items():
            doc = str(getattr(fn, "description", getattr(fn, "__doc__", "")) or "")
            hay = (name + " " + doc).lower()
            score = (4 if str(query).lower() in name.lower() else 0) + sum(1 for w in words if w in hay)
            if score:
                ranked.append((score, name, " ".join(doc.strip().split())[:180]))
        ranked.sort(key=lambda row: (-row[0], row[1]))
        return json.dumps([{"name": n, "description": d} for _, n, d in ranked[:max(1, min(int(limit), 20))]],
                          ensure_ascii=False)

    def tool_describe(name: str) -> str:
        """查看一个隐藏工具的完整参数签名和说明。"""
        fn = _catalog.get(str(name))
        if fn is None:
            return json.dumps({"error": "工具不存在", "name": name}, ensure_ascii=False)
        import inspect as _inspect_td
        try:
            sig = str(_inspect_td.signature(fn))
        except Exception:
            sig = "(参数见说明)"
        return json.dumps({"name": name, "signature": sig,
                           "description": str(getattr(fn, "description", getattr(fn, "__doc__", "")) or "")[:3000]},
                          ensure_ascii=False)

    def tool_call(name: str, arguments_json: str = "{}") -> str:
        """调用 tool_search 找到的隐藏工具。arguments_json 是 JSON 对象；原工具的安全/预算/日志照常生效。"""
        fn = _catalog.get(str(name))
        if fn is None:
            return json.dumps({"error": "工具不存在", "name": name}, ensure_ascii=False)
        try:
            args = json.loads(arguments_json or "{}")
            if not isinstance(args, dict):
                return json.dumps({"error": "arguments_json 必须是 JSON 对象"}, ensure_ascii=False)
            result = fn.invoke(args) if hasattr(fn, "invoke") else fn(**args)
            if isinstance(result, (dict, list)):
                return json.dumps(result, ensure_ascii=False, default=str)[:12000]
            return str(result)[:12000]
        except Exception as exc:
            # 错误原样给模型判断·不加语义门。
            return json.dumps({"error": f"{type(exc).__name__}: {str(exc)[:500]}",
                               "tool": name, "arguments": args if 'args' in locals() else {}},
                              ensure_ascii=False, default=str)

    for _bridge in (tool_search, tool_describe, tool_call):
        tools.append(_bridge)

    # ── Footprint Ladder(2026-08-27·学 hermes AGENTS.md:24/182 "core is a narrow
    #    waist·capability lives at edges") ────────────────────────────────────
    #   病根:132 工具全常驻 → 模型每轮吃 132 个 schema(上下文膨胀+选择混乱+慢)·
    #   而历史 tool_log 只 51 个真被调用过·81 个从未用。学 hermes/cc:core 一小撮
    #   常驻·其余按需(skill_search/tool_search/github_search 发现后调用)。
    #   MEYO_TOOLSET=full 回退全量(benchmark/高级场景)。core 白名单=真实高频(tool_log
    #   top)+ 军团必需(spawn_subagent/finalize)+ 发现入口(skill_search/tool_search)。
    _toolset = os.environ.get("MEYO_TOOLSET", "core").strip().lower()
    if _toolset == "full":
        return tools
    # Auto 是产品 agent，不再把 benchmark 的题型分诊/证据评分元工具常驻给模型。
    # 这些工具曾让已经产出并 Playwright 验证通过的网页仍被判“证据摘要为空”，重复搜索、
    # 换十个模型。产品完成事实来自真实文件/命令/测试，不来自 BC/GAIA 证据计数。
    _CORE = {
        # 检索(真实最高频)
        "run_parallel_search", "fetch_url_smart", "predict_then_search",
        "query_structured_db", "search_github", "query_web_archive", "extract_cards",
        "run_factual_extract", "extract_source_anchors", "wayback_fetch",
        # 执行/文件/计算
        "shell_exec", "file_read", "file_write", "sympy_calc",
        # 感知
        "read_image_ocr", "see_image_vlm", "read_pdf", "read_table",
        # 编排能力。产品模式由模型自然结束本轮，真实文件/测试/artifact 是完成事实；
        # 不再常驻 benchmark 的 finalize_answer“交卷”工具或证据评分元工具。
        "spawn_subagent", "spawn_research_routes", "delegate", "write_plan",
        "ask_expert_model", "request_more_budget", "document_wall",
        # 按需发现入口(其余工具经这些找到再调·narrow waist 的"边缘接入")
        "skill_search", "skill_load", "tool_search", "tool_describe", "tool_call",
        "search_written_tools", "search_local_files",
    }
    # 用户明确本轮 no_search 时，不把网络搜索 schema 暴露给模型；这不是内容路由，而是
    # 用户亲自给出的工具权限边界。search_local_files 仍保留，其他任务仍有完整网络能力。
    _network_tools = {"run_parallel_search", "fetch_url_smart", "predict_then_search",
                      "query_structured_db", "search_github", "query_web_archive", "wayback_fetch"}
    _triage = (_sess(qid).get("triage") or {})
    _allowed_core = _CORE - _network_tools if _triage.get("search_route") == "no_search" else _CORE
    _kept = [t for t in tools
             if str(getattr(t, "name", getattr(t, "__name__", ""))) in _allowed_core]
    # fail-safe:过滤后异常空(名字取不到)→退回全量·绝不让 auto 无工具。
    return _kept if len(_kept) >= 10 else tools

