#!/usr/bin/env python3
"""云电脑前沿交付工作流：让军团自主画像、研究、执行、独立评审并按证据返工。"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Callable

import desktop_golden_task
import desktop_production_matrix
import desktop_quality_run
import desktop_quality_verifier
import desktop_reference_bundles
import desktop_workspace
import quality_context


def _json_object(value: object) -> dict:
    if isinstance(value, dict):
        return value
    text = str(value or "").strip()
    candidates = [text]
    if "```" in text:
        candidates.extend(part.split("```", 1)[0] for part in text.split("```json")[1:])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate.strip())
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            continue
    return {}


def _dispatch(dispatch: Callable[[str], dict], prompt: str) -> tuple[dict, dict]:
    result = dispatch(prompt) or {}
    final = result.get("final_result") or ""
    if not final:
        rows = result.get("results") or []
        final = "\n".join(str(row.get("final") or "") for row in rows if isinstance(row, dict))
    return result, _json_object(final)


def _dispatch_parallel(dispatch: Callable[[str], dict], prompts: list[str]) -> list[tuple[dict, dict]]:
    """独立 Scout/评委必须真并行，墙钟接近最慢一项而不是顺序相加。"""
    if len(prompts) < 2:
        return [_dispatch(dispatch, prompt) for prompt in prompts]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(prompts), thread_name_prefix="desktop-quality") as executor:
        futures = [executor.submit(_dispatch, dispatch, prompt) for prompt in prompts]
        return [future.result() for future in futures]


def _profile_prompt(task: dict) -> str:
    spec = task.get("execution_spec") or {}
    return f"""你是云电脑任务画像器，也是 Quality Context 的目标画像成员。保留用户原始 Query，只补齐真实用户、核心 Job、期望结果、留存理由、限制、风险、可验证完成标准和需要研究的未知项；不要替执行者规定网站、技术栈或视觉解法。
原始 Query：{task.get('goal') or ''}
已知执行现场：{json.dumps(spec, ensure_ascii=False)}
请只输出 JSON 对象：
{{"user_outcome":"", "target_user":"", "core_job":"", "retention_reason":"",
  "constraints":[], "success_criteria":[], "required_artifacts":[], "risk_boundaries":[],
  "open_questions":[], "initial_queries":[{{"query":"","why":""}}],
  "quality_dimensions":[{{"key":"domain_depth","weight":0.2,"criteria":[]}}]}}
required_artifacts 必须是项目相对路径；权重总和应为 1；信息不足要写 open_questions，不得凭空定方案。"""


def _scout_prompt(task: dict, profile: dict, root: Path) -> str:
    return f"""你是只读 Quality Context Frontier Scout。像资深产品研究者一样真搜索、逐个打开并阅读公开来源，而不是只列品牌名：
- 从任务画像的 initial_queries 开始，记录为什么搜索；结果若是广告、低价值聚合页、打不开或不适合目标用户，记录淘汰原因并改写 Query；
- 至少研究产品结构、关键用户旅程、具体页面/状态、视觉与内容层级、交互反馈、商业闭环；需要时查看公开视频或可合法访问的媒体；
- 每个事实观察绑定 source_id 和一手 URL；能截图/保存的原始证据写入 {root}/evidence/research/，不得绕过登录、付费或权限边界；
- 只有你能说明研究已足以定义强 MVP 时才收敛；不要因为搜到 3 个 URL 就停止，也不要无限搜索。
不要修改最终产品文件。
原始任务：{task.get('goal') or ''}
任务画像：{json.dumps(profile, ensure_ascii=False)}
只输出 JSON：
{{"query_ledger":[{{"query_id":"Q-001","query":"","why":"","derived_from":[],"result_source_ids":[]}}],
 "sources":[{{"source_id":"S-001","title":"","url":"https://...","kind":"website|video|app|article",
   "page_or_state":"","viewport":"","decision":"kept|rejected","decision_reason":"",
   "observations":[],"evidence_paths":[]}}],
 "reference_patterns":[{{"pattern_id":"P-001","dimension":"visual|content|interaction|information_architecture|commercial_loop|technical",
   "pattern":"","why_it_matters":"","source_ids":["S-001"],"adoption_note":""}}],
 "references":[{{"name":"具名团队","work":"具体作品","url":"https://...","strengths":[],"open_gap":"",
   "comparison":{{"same_golden_task":false,"input":"","constraints":"","expected_user_outcome":"",
                   "artifact_paths":[],"evidence_paths":[]}}}}],
 "golden_task":{{"input":"","constraints":"","expected_user_outcome":""}},
 "frontier_findings":[],"research_sufficiency":"为什么已足够/仍不足","open_questions":[]}}
公开商业灵感与真正同题可运行参照必须分开；没有同题证据就把 same_golden_task 写 false，绝不能伪造。"""


def _synthesis_prompt(task: dict, profile: dict, scout: dict) -> str:
    return f"""你是 Quality Context 编译成员。基于原始 Query、任务画像和实际研究证据，定义强 MVP，而不是最少功能清单。结论必须引用现有 source_id；来源不足的部分写入 open_questions。
原始 Query：{task.get('goal') or ''}
任务画像：{json.dumps(profile, ensure_ascii=False)}
研究包：{json.dumps(scout, ensure_ascii=False)}
只输出 JSON：
{{"target_user":"","core_job":"","desired_outcome":"","retention_reason":"",
 "strong_mvp":{{"value_loop":[],"must_have":[],"deferred":[],"must_feel":[],"anti_goals":[]}},
 "golden_journey":[{{"step_id":"J-01","name":"","user_intent":"","expected_observation":"",
                      "failure_or_recovery":"","reference_pattern_ids":[]}}],
 "reference_patterns":[],"technical_context":[{{"option":"","tradeoff":"","source_ids":[]}}],
 "safety_context":[],"commercial_release_line":[],"open_questions":[]}}
不要要求固定框架、固定布局、固定文件数；只说明什么用户结果算强、什么现象说明达到商用可信水平。"""


def _context_audit_prompt(task: dict, context: dict) -> str:
    return f"""你是独立 Quality Context 审计者，只审计开工上下文，不设计成品。检查：原始 Query 是否保真；强 MVP 是否是完整用户价值闭环；黄金旅程是否可观察；视觉/内容/交互/商业参考是否来自实际访问证据；是否把漂亮首页、品牌名或弱原型误当同题商用参照；是否仍有会导致团队优化错误目标的关键未知项。
任务：{task.get('goal') or ''}
Context：{json.dumps(context, ensure_ascii=False)}
只输出 JSON：{{"ready":true,"findings":[],"open_questions":[],"recommended_research_queries":[]}}。
ready 是审计意见而非平台总判决；证据不足时如实写 false。"""


def _profile_golden_task(task: dict, profile: dict) -> dict:
    return desktop_golden_task.canonical(
        input_value=task.get("goal"), constraints=profile.get("constraints") or [],
        expected_user_outcome=profile.get("user_outcome"),
    )


def _captain_prompt(task: dict, run: dict) -> str:
    context = run.get("quality_context") or {}
    return f"""你是云电脑交付 Captain。基于完整 Quality Context 与真实研究证据，自主选择 single/PK/relay/orchestrate，并设计依赖图。只把能独立验收且接口冻结的节点并行；North Star、核心产品决策和视觉语言必须有唯一 owner。
原始目标：{task.get('goal') or ''}
Quality Context：{json.dumps(context, ensure_ascii=False)}
已有同题证据：{json.dumps({key: run.get(key) for key in ('references','golden_task')}, ensure_ascii=False)}
只输出 JSON：
{{"north_star":"", "mode":"single|pk|relay|orchestrate", "decisions":[{{"id":"D-01","choice":"","why":"","reversible":true}}],
 "contracts":[], "nodes":[{{"id":"","goal":"","depends_on":[],"owner_role":"","verification":[]}}],
 "delivery_shape":[], "revision_budget":{{"max_rounds":3}}}}
这是 context not control：根据任务自己选组织法和实现形态，不要机械凑角色，不要把研究参考误写成固定 UI 模板。"""


def _execute_prompt(task: dict, run: dict, feedback: list[str]) -> str:
    root = f"/root/Meyo/projects/{task['project_id']}"
    organization = run.get("organization") or {}
    role_contexts = desktop_quality_run.role_contexts_for_run(run)
    return f"""在真实云电脑项目 {root} 中完成下面任务，使用 IDE、Chromium、LibreOffice、设计软件或程序化工具的组合，以成果质量为先；需要 GUI 证明的环节必须真操作并截图。
原始 Query：{task.get('goal') or ''}
North Star：{run.get('north_star') or ''}
完整 Quality Context：{json.dumps(run.get('quality_context') or {}, ensure_ascii=False)}
组织图：{json.dumps(organization, ensure_ascii=False)}
各职责定向 Context：{json.dumps(role_contexts, ensure_ascii=False)}
接口与验收：{json.dumps(run.get('contracts') or [], ensure_ascii=False)}
必需产物：{json.dumps((run.get('profile') or {}).get('required_artifacts') or [], ensure_ascii=False)}
本轮评审缺口：{json.dumps(feedback, ensure_ascii=False)}

要求：
1. 先观察桌面与已有文件；checkpoint 要求 observe_before_resume 时绝不能盲重放。
2. 浏览研究用 Chromium 真访问；代码真构建测试；Office/图片真生成并打开；跨应用过程保存证据。
3. 成品放 {root}/deliverables，截图/轨迹/来源/验证结果放 {root}/evidence，README 写复现和打开方法。
4. 每个“超过参照”的声明必须在 evidence/claims.json 映射到原始证据；没有证据就写 remaining_gaps，不得自封。
5. 按 golden_journey 真走核心旅程，逐步记录 expected/observed/evidence；不能只截首页或只证明按钮存在。
6. 登录、验证码、MFA、隐私、付款、下单、发消息、公开发布或账户变更立即创建 human handoff。
7. 完成后只输出 JSON：{{"summary":"", "artifact_paths":[], "evidence_paths":[], "claims":[],
 "journey_observations":[{{"step_id":"J-01","expected":"","observed":"","status":"pass|issue|blocked","evidence_refs":[]}}],
 "remaining_gaps":[]}}。"""


def _review_prompt(task: dict, run: dict, evidence: list[dict], judge_index: int) -> str:
    return f"""你是 fresh-context 独立红队评委 #{judge_index}。不要相信作者解释，只看可打开成品、原始证据、Quality Context、真实用户旅程与参照。
任务：{task.get('goal') or ''}
Quality Context：{json.dumps(run.get('quality_context') or {}, ensure_ascii=False)}
旅程观察：{json.dumps(run.get('journey_observations') or [], ensure_ascii=False)}
参照：{json.dumps(run.get('references') or [], ensure_ascii=False)}
golden task：{json.dumps(run.get('golden_task') or {}, ensure_ascii=False)}
维度：{json.dumps(run.get('dimensions') or [], ensure_ascii=False)}
机器证据：{json.dumps(evidence, ensure_ascii=False)}

重要：作者身份、实现者名称、模型和组织方式均未提供；不得根据路径或文风猜测。candidate 与 reference 必须使用完全相同的 golden task、限制和逐维标准。商用灵感来源不是同题可运行参照时，只能帮助描述质量差距，不能支持胜出结论。只输出 JSON：
{{"judge":"independent-{judge_index}", "author_hidden":true, "candidate_scores":{{}}, "reference_scores":{{}},
 "preference":"candidate|reference|tie", "defects":[], "verified_strengths":[], "remaining_gaps":[],
 "gap_plan":[{{"problem":"","severity":"high|medium|low","observed":"","reference_behavior":"",
   "user_consequence":"","root_cause_hypothesis":"","recommended_outcome":"",
   "acceptance_observation":"","evidence_refs":[]}}]}}
分数必须逐维给出 0–100 且覆盖维度中的每个 key；正确性回退不能被创新性总分遮住；Gap 写用户可观察结果，不替实现者指定框架或布局。"""


def _artifact_evidence(root: Path, output: dict) -> list[dict]:
    declared = []
    paths = list(output.get("artifact_paths") or []) + list(output.get("evidence_paths") or [])
    for value in paths:
        candidate = Path(str(value))
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            candidate = candidate.resolve()
            if not candidate.is_file() or not candidate.is_relative_to(root.resolve()):
                continue
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
            declared.append({"evidence_id": f"E-{len(declared)+1:03d}",
                             "path": candidate.relative_to(root).as_posix(), "verified": True,
                             "sha256": digest, "kind": "artifact" if "deliverables" in candidate.parts else "evidence"})
        except (OSError, ValueError):
            continue
    return declared


def _dimensions(profile: dict, fallback: list) -> list:
    rows = profile.get("quality_dimensions") if isinstance(profile, dict) else None
    if not isinstance(rows, list) or not rows:
        return fallback
    cleaned = []
    for row in rows:
        if isinstance(row, dict) and row.get("key"):
            cleaned.append({"key": str(row["key"])[:80], "name": str(row.get("name") or row["key"])[:120],
                            "weight": float(row.get("weight") or 0), "criteria": list(row.get("criteria") or []),
                            "candidate_score": None, "reference_score": None})
    total = sum(max(0.0, row["weight"]) for row in cleaned)
    if not cleaned or total <= 0:
        return fallback
    for row in cleaned:
        row["weight"] = round(max(0.0, row["weight"]) / total, 6)
    return cleaned


def run(task: dict, dispatch: Callable[[str], dict]) -> dict:
    uid, project_id, task_id = str(task.get("uid") or "default"), str(task["project_id"]), str(task["task_id"])
    current = desktop_quality_run.get(uid, project_id, task_id, include_events=False)
    if not current.get("ok"):
        current = desktop_quality_run.create(uid, project_id, task_id,
                                             max_rounds=int((task.get("execution_spec") or {}).get("max_quality_rounds") or 3),
                                             wall_clock_s=int((task.get("execution_spec") or {}).get("quality_wall_clock_s") or 7200))
    run_state = current["run"]
    root = desktop_workspace._project_root(uid, project_id)

    if not run_state.get("profile"):
        _raw, profile = _dispatch(dispatch, _profile_prompt(task))
        if not profile:
            return {"ok": False, "state": "queued", "phase": "任务画像未形成，等待重试",
                    "checkpoint": {"stage": "profiling"}, "next_run_at": time.time() + 5}
        run_state = desktop_quality_run.update(uid, project_id, task_id, state="scouting",
                                               patch={"profile": profile,
                                                      "dimensions": _dimensions(profile, run_state["dimensions"])})["run"]

    scenario = str((task.get("execution_spec") or {}).get("scenario") or "")
    if (not run_state.get("quality_context") and not scenario and
            str(run_state.get("state") or "") in {"profiling", "scouting"}):
        _raw, scout = _dispatch(dispatch, _scout_prompt(task, run_state["profile"], root))
        if not scout:
            return {"ok": False, "state": "queued", "phase": "商业参考研究未形成，等待继续搜索",
                    "checkpoint": {"stage": "scouting"}, "next_run_at": time.time() + 5}
        _raw, synthesized = _dispatch(dispatch, _synthesis_prompt(task, run_state["profile"], scout))
        context = quality_context.normalize({
            **synthesized,
            "query_ledger": scout.get("query_ledger") or [],
            "sources": scout.get("sources") or [],
            "reference_patterns": synthesized.get("reference_patterns") or scout.get("reference_patterns") or [],
            "open_questions": list(dict.fromkeys([*(synthesized.get("open_questions") or []),
                                                   *(scout.get("open_questions") or [])])),
        }, str(task.get("goal") or ""), reason="initial commercial reference research",
           experience_intent=(task.get("execution_spec") or {}).get("experience_intent") or {})
        _raw, context_audit = _dispatch(dispatch, _context_audit_prompt(task, context))
        if context_audit.get("findings") or context_audit.get("open_questions"):
            context["open_questions"] = list(dict.fromkeys([
                *(context.get("open_questions") or []), *(context_audit.get("findings") or []),
                *(context_audit.get("open_questions") or []),
            ]))
        context["context_audit"] = context_audit
        run_state = desktop_quality_run.update(uid, project_id, task_id, state="scouting", patch={
            "quality_context": context,
            "context_history": [quality_context.history_entry(context)],
            "references": scout.get("references") if isinstance(scout.get("references"), list) else [],
            "golden_task": scout.get("golden_task") if isinstance(scout.get("golden_task"), dict) else {},
            "frontier_findings": scout.get("frontier_findings") or [],
        })["run"]

    if not run_state.get("references") or not run_state.get("golden_task"):
        scenario = str((task.get("execution_spec") or {}).get("scenario") or "")
        expected_golden = _profile_golden_task(task, run_state["profile"])
        local = desktop_reference_bundles.discover(uid, scenario=scenario, golden_task=expected_golden) \
            if scenario else {"references": []}
        local_references = [desktop_reference_bundles.as_quality_reference(row)
                            for row in local.get("references") or []]
        if local_references:
            scout = {"references": local_references, "golden_task": expected_golden,
                     "frontier_findings": ["复用经业务验收和哈希复核的本地同题生产参考"],
                     "candidate_breakthroughs": []}
        elif scenario in desktop_production_matrix.SCENARIOS:
            latest_task = desktop_workspace.get_task(uid, project_id, task_id, include_events=False)
            checkpoint = (latest_task.get("task") or {}).get("checkpoint") \
                if latest_task.get("ok") else task.get("checkpoint")
            checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
            reference_project_id = str(checkpoint.get("reference_project_id") or "")
            reference_task_id = str(checkpoint.get("reference_task_id") or "")
            reference_task = desktop_workspace.get_task(
                uid, reference_project_id, reference_task_id, include_events=False,
            ) if reference_project_id and reference_task_id else {"ok": False}
            if reference_task.get("ok") and reference_task["task"].get("state") in {"queued", "running"}:
                return {"ok": True, "state": "queued", "phase": "同题参考仍在生产，等待业务验收",
                        "checkpoint": checkpoint, "next_run_at": time.time() + 60}
            if reference_task.get("ok") and reference_task["task"].get("state") in {"partial", "failed", "blocked"}:
                gaps = (reference_task["task"].get("checkpoint") or {}).get("verification", {}).get("remaining_gaps", [])
                return {"ok": True, "state": "queued", "phase": "同题参考生产失败，等待按验收缺口返工",
                        "checkpoint": {**checkpoint, "reference_gaps": gaps}, "next_run_at": time.time() + 60}
            bootstrap = desktop_production_matrix.run_scenario(
                uid, scenario, golden_task=expected_golden,
                parent_quality_task={"uid": uid, "project_id": project_id, "task_id": task_id},
            )
            bootstrap_checkpoint = {"stage": "scouting", "reference_project_id": bootstrap["project_id"],
                                    "reference_task_id": bootstrap["task_id"]}
            latest_task = desktop_workspace.get_task(uid, project_id, task_id, include_events=False)
            if latest_task.get("ok"):
                desktop_workspace.update_task(
                    uid, project_id, task_id, state="queued", phase="已排队生产同题参考，等待业务验收",
                    checkpoint=bootstrap_checkpoint, expected_version=latest_task["task"].get("version"),
                )
            return {"ok": True, "state": "queued", "phase": "已排队生产同题参考，等待业务验收",
                    "checkpoint": bootstrap_checkpoint, "next_run_at": time.time() + 60}
        else:
            # 初始 Context 编译已经保存了真实研究包；此处只复用同一证据，避免为满足
            # frontier 合同重复搜索。没有可运行同题参照时如实停在商用品质 Context，
            # 不把公开灵感来源伪装成同题胜出证据。
            context = run_state.get("quality_context") or {}
            scout = {"references": run_state.get("references") or [],
                     "golden_task": run_state.get("golden_task") or _profile_golden_task(task, run_state["profile"]),
                     "frontier_findings": run_state.get("frontier_findings") or [],
                     "candidate_breakthroughs": []}
        references = scout.get("references") if isinstance(scout.get("references"), list) else []
        golden = scout.get("golden_task") if isinstance(scout.get("golden_task"), dict) else {}
        same_task = all(
            isinstance(reference.get("comparison"), dict)
            and reference["comparison"].get("same_golden_task") is True
            and all(str(reference["comparison"].get(key) or "").strip() == str(golden.get(key) or "").strip()
                    for key in ("input", "constraints", "expected_user_outcome"))
            and all(isinstance(reference["comparison"].get(key), list)
                    and any(str(value or "").strip() for value in reference["comparison"][key])
                    for key in ("artifact_paths", "evidence_paths"))
            for reference in references
        )
        if not references or not golden or not same_task:
            context = run_state.get("quality_context") or {}
            issues = quality_context.evidence_issues(context)
            return {"ok": True, "state": "queued",
                    "phase": "商用品质 Context 已形成；同题可运行参照仍不足，继续研究而不伪造胜出",
                    "checkpoint": {"stage": "scouting", "quality_context": context,
                                   "context_issues": issues,
                                   "reference_gap": "缺少完全相同输入、限制和用户结果的可运行参照"},
                    "next_run_at": time.time() + 60}
        run_state = desktop_quality_run.update(uid, project_id, task_id, state="executing",
                                               patch={"references": references, "golden_task": golden,
                                                      "frontier_findings": scout.get("frontier_findings") or [],
                                                      "candidate_breakthroughs": scout.get("candidate_breakthroughs") or []})["run"]

    if not run_state.get("organization"):
        _raw, captain = _dispatch(dispatch, _captain_prompt(task, run_state))
        if not captain:
            return {"ok": False, "state": "queued", "phase": "组织方案未形成，等待重试",
                    "checkpoint": {"stage": "planning"}, "next_run_at": time.time() + 5}
        run_state = desktop_quality_run.update(uid, project_id, task_id, state="executing", patch={
            "north_star": captain.get("north_star") or "", "decisions": captain.get("decisions") or [],
            "contracts": captain.get("contracts") or [], "organization": captain,
        })["run"]

    feedback = list(run_state.get("remaining_gaps") or [])
    _raw, execution = _dispatch(dispatch, _execute_prompt(task, run_state, feedback))
    declared = _artifact_evidence(root, execution)
    claims = execution.get("claims") if isinstance(execution.get("claims"), list) else []
    run_state = desktop_quality_run.update(uid, project_id, task_id, state="reviewing", patch={
        "evidence": declared, "claims": claims,
        "journey_observations": [item for item in execution.get("journey_observations") or []
                                 if isinstance(item, dict)],
        "remaining_gaps": list(execution.get("remaining_gaps") or []),
        "round": int(run_state.get("round") or 0) + 1,
    })["run"]

    preliminary = desktop_quality_verifier.verify(uid, project_id, task_id, run_state)
    reviews = [review for _raw, review in _dispatch_parallel(
        dispatch,
        [_review_prompt(task, run_state, preliminary.get("evidence") or [], index)
         for index in (1, 2)],
    ) if review]
    author_blinded = all(review.get("author_hidden") is True for review in reviews)
    reviews = [{**review, "author_hidden": author_blinded} for review in reviews]
    dimensions = []
    for dimension in run_state.get("dimensions") or []:
        row = dict(dimension)
        candidate = [review.get("candidate_scores", {}).get(row["key"]) for review in reviews
                     if review.get("candidate_scores", {}).get(row["key"]) is not None]
        reference = [review.get("reference_scores", {}).get(row["key"]) for review in reviews
                     if review.get("reference_scores", {}).get(row["key"]) is not None]
        if candidate:
            row["candidate_score"] = sum(float(value) for value in candidate) / len(candidate)
        if reference:
            row["reference_score"] = sum(float(value) for value in reference) / len(reference)
        dimensions.append(row)
    run_state = desktop_quality_run.update(uid, project_id, task_id, state="verifying",
                                           patch={"reviews": reviews, "dimensions": dimensions})["run"]
    report = desktop_quality_verifier.verify(uid, project_id, task_id, run_state)
    report_artifact = desktop_quality_verifier.write_report(uid, project_id, task_id, report)
    evidence = list(run_state.get("evidence") or [])
    evidence.append({"evidence_id": f"E-{len(evidence)+1:03d}", "path": report_artifact["path"],
                     "verified": True, "sha256": report_artifact["sha256"], "kind": "quality_report"})

    elapsed = time.time() - float((run_state.get("metrics") or {}).get("started_at") or time.time())
    can_retry = (report["verdict"] != desktop_quality_verifier.VERDICT_WIN
                 and int(run_state.get("round") or 0) < int(run_state.get("max_rounds") or 3)
                 and elapsed < float(run_state.get("wall_clock_s") or 7200))
    if can_retry:
        gaps = list(dict.fromkeys((report.get("remaining_gaps") or [])
                                  + [gap for review in reviews for gap in review.get("remaining_gaps") or []]
                                  + [defect for review in reviews for defect in review.get("defects") or []]))
        structured_gaps = quality_context.normalize_gaps([
            item for review in reviews for item in review.get("gap_plan") or []
        ] + [{"problem": gap} for gap in gaps])
        revised_context = quality_context.merge_gaps(
            run_state.get("quality_context") or {}, structured_gaps,
            reason=f"round {run_state.get('round')} independent review")
        history = list(run_state.get("context_history") or [])
        history.append(quality_context.history_entry(revised_context))
        revised = desktop_quality_run.update(uid, project_id, task_id, state="revising", patch={
            "evidence": evidence, "remaining_gaps": gaps, "gap_plan": structured_gaps,
            "quality_context": revised_context, "context_history": history[-20:],
        })["run"]
        return {"ok": True, "state": "queued", "phase": f"独立评审后进入第 {revised['round'] + 1} 轮改进",
                "checkpoint": {"stage": "revising", "quality": revised, "artifacts": [], "screenshots": []},
                "next_run_at": time.time() + 5}

    strengths = list(dict.fromkeys(value for review in reviews for value in review.get("verified_strengths") or []))
    finalized = desktop_quality_run.finalize(uid, project_id, task_id, report["verdict"],
                                             verified_strengths=strengths,
                                             remaining_gaps=report.get("remaining_gaps") or [], report=report)["run"]
    files = desktop_workspace._collect_project_files(root)
    artifacts = [{"name": row["name"], "path": str(root / row["path"]),
                  "relative_path": row["path"], "bytes": row["bytes"]}
                 for row in files if row["path"].startswith(("deliverables/", "evidence/"))]
    screenshots = [row["path"] for row in files if row["path"].startswith("evidence/")
                   and row["path"].lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
    state = "completed" if report["verdict"] in {desktop_quality_verifier.VERDICT_WIN,
                                                   desktop_quality_verifier.VERDICT_PARITY} else "partial"
    return {"ok": True, "state": state,
            "phase": {desktop_quality_verifier.VERDICT_WIN: "已验证窄面胜出",
                       desktop_quality_verifier.VERDICT_PARITY: "已交付，前沿比较持平或证据不足",
                       desktop_quality_verifier.VERDICT_BELOW: "已交付，但仍低于参照"}[report["verdict"]],
            "checkpoint": {"stage": "quality_finalized", "quality": finalized,
                           "artifacts": artifacts, "screenshots": screenshots}}
