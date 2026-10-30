#!/usr/bin/env python3
"""Named frontier references for high-value artifact families.

The catalog is context, not certification.  A famous public example helps an auditor inspect the
right qualities, but it never satisfies the same-golden-task evidence required by
``success_auditor.frontier_contract``.
"""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any

SCHEMA_VERSION = 1

REFERENCE_ATLAS = {
    "status": "context_index_only",
    "repo_path": "dev/world-class-delivery-context-atlas.md",
    "km_url": "https://km.sankuai.com/collabpage/2785470941",
    "selection_policy": "harness_selects_relevant_sources_from_experience_intent",
    "categories": [
        "research", "presentation_document", "data_analysis", "investment_strategy",
        "web_software", "game_3d_simulation", "visual_design", "video_audio",
        "business_automation", "hardware_device",
    ],
    "instructions": [
        "先按当前用户结果选择最相关的一至三个品类，再打开原始来源",
        "可继续搜索更贴题或更强的一手参照，Atlas 不是封闭榜单",
        "自主决定参考组合、Harness 与编排方式，不按品类硬编码路由",
        "信息足以改变或支持当前作品决策时停止搜索并回到交付",
    ],
}

BENCHMARKS: dict[str, dict[str, Any]] = {
    "presentation": {
        "label": "演示文稿 / 发布叙事",
        "formats": ["pptx", "key", "pdf"],
        "references": [
            {"name": "Apple", "work": "Apple Events / Keynote",
             "url": "https://www.apple.com/apple-events/", "source": "official_primary_url"},
        ],
        "dimensions": ["narrative_arc", "information_hierarchy", "visual_rhythm",
                       "speaker_readiness", "accessibility"],
    },
    "report": {
        "label": "专业报告 / 咨询交付",
        "formats": ["pdf", "docx", "xlsx"],
        "references": [
            {"name": "McKinsey & Company", "work": "Featured Insights",
             "url": "https://www.mckinsey.com/featured-insights", "source": "official_primary_url"},
        ],
        "dimensions": ["executive_clarity", "evidence_traceability", "decision_usefulness",
                       "quantitative_rigor", "editorial_craft"],
    },
    "image": {
        "label": "视觉设计 / 品牌图像",
        "formats": ["png", "jpg", "jpeg", "webp", "svg"],
        "references": [
            {"name": "Pentagram", "work": "Pentagram Work",
             "url": "https://www.pentagram.com/work", "source": "official_primary_url"},
        ],
        "dimensions": ["concept_originality", "composition", "typography", "production_finish",
                       "brand_coherence"],
    },
    "video": {
        "label": "视频 / 动态叙事",
        "formats": ["mp4", "mov", "webm", "gif"],
        "references": [
            {"name": "Vimeo", "work": "Vimeo Staff Picks",
             "url": "https://vimeo.com/channels/staffpicks", "source": "official_primary_url"},
        ],
        "dimensions": ["storytelling", "pacing", "motion_craft", "audio_visual_coherence",
                       "technical_finish"],
    },
    "web": {
        "label": "网页 / 数字产品体验",
        "formats": ["html"],
        "references": [
            {"name": "Awwwards", "work": "Sites of the Day",
             "url": "https://www.awwwards.com/websites/sites_of_the_day/",
             "source": "independent_awards_primary_url"},
            {"name": "Linear", "work": "Linear product website",
             "url": "https://linear.app/", "source": "official_primary_url"},
        ],
        "dimensions": ["task_completion", "interaction_quality", "visual_system",
                       "responsive_accessibility", "performance_reliability"],
    },
    "code": {
        "label": "代码 / 软件工程交付",
        "formats": ["py", "js", "ts", "tsx", "go", "rs", "java", "patch", "diff"],
        "references": [
            {"name": "SWE-bench", "work": "SWE-bench Verified",
             "url": "https://www.swebench.com/SWE-bench/", "source": "official_benchmark_url"},
        ],
        "dimensions": ["correctness", "test_evidence", "maintainability", "scope_discipline",
                       "operational_reliability"],
    },
}


def catalog() -> dict:
    return {"schema_version": SCHEMA_VERSION, "status": "reference_context_only",
            "same_golden_task_required": True, "reference_atlas": deepcopy(REFERENCE_ATLAS),
            "families": deepcopy(BENCHMARKS)}


def _artifact_formats(plan: dict | None) -> set[str]:
    source = plan if isinstance(plan, dict) else {}
    contract = source.get("delivery_contract") if isinstance(source.get("delivery_contract"), dict) else {}
    rows = contract.get("required_artifacts") or source.get("required_artifacts") or []
    formats: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        values = row.get("formats") or [row.get("format") or row.get("extension")]
        formats.update(str(value).lower().lstrip(".") for value in values if str(value or "").strip())
    return formats


def _text_list(value: Any, limit: int = 20) -> list[str]:
    if isinstance(value, str):
        value = [value]
    return list(dict.fromkeys(str(item).strip()[:500] for item in (value or [])
                              if str(item).strip()))[:limit]


def compile_experience_contract(plan: dict | None) -> dict:
    """把规划器理解出的用户体验意图编译为动态世界语境研究合同；不从关键词猜行业。"""
    source = plan if isinstance(plan, dict) else {}
    raw = source.get("experience_intent") if isinstance(source.get("experience_intent"), dict) else {}
    dimensions = []
    for index, item in enumerate(raw.get("experience_dimensions") or []):
        if isinstance(item, str):
            item = {"key": item, "user_outcome": item}
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or item.get("dimension") or f"dimension_{index + 1}").strip()[:80]
        outcome = str(item.get("user_outcome") or item.get("standard") or "").strip()[:500]
        if key and outcome:
            dimensions.append({"key": key, "user_outcome": outcome,
                               "minimum_standard": str(item.get("minimum_standard") or outcome)[:800],
                               "breakthrough_question": str(item.get("breakthrough_question") or
                                                              f"怎样在 {outcome} 上可验证地超过当前最佳？")[:800]})
    ambition = str(raw.get("ambition") or source.get("quality_profile") or "professional").lower()
    if ambition not in {"baseline", "professional", "world_class", "frontier"}:
        ambition = "professional"
    quality_profile = "frontier" if ambition in {"world_class", "frontier"} \
        or str(source.get("quality_profile") or "").lower() == "frontier" else \
        "artifact" if dimensions else str(source.get("quality_profile") or "answer")
    golden_raw = source.get("golden_task") if isinstance(source.get("golden_task"), dict) else {}
    golden = {key: str(golden_raw.get(key) or "").strip()[:4000]
              for key in ("input", "constraints", "expected_user_outcome")}
    intent = {
        "user_context": str(raw.get("user_context") or "")[:2000],
        "core_need": str(raw.get("core_need") or "")[:1200],
        "desired_outcome": str(raw.get("desired_outcome") or golden.get("expected_user_outcome") or "")[:1200],
        "audience": str(raw.get("audience") or "")[:800],
        "medium": str(raw.get("medium") or "")[:400],
        "ambition": ambition,
        "must_feel": _text_list(raw.get("must_feel")),
        "must_achieve": _text_list(raw.get("must_achieve")),
        "non_negotiables": _text_list(raw.get("non_negotiables")),
        "experience_dimensions": dimensions,
        "benchmark_queries": _text_list(raw.get("benchmark_queries"), 30),
    }
    parity_dimensions = [row["key"] for row in dimensions]
    default_weight = round(1.0 / len(dimensions), 6) if dimensions else 0.0
    critical_dimensions = [{
        "dimension": row["key"],
        "user_outcome": row["user_outcome"],
        "minimum_standard": row["minimum_standard"],
        "impact_weight": default_weight,
        "failure_mode": f"{row['user_outcome']} 未被真实用户观察证明",
        "sample_question": row["breakthrough_question"],
    } for row in dimensions]
    north_star = {
        "user": intent["audience"],
        "job": intent["core_need"],
        "observable_outcome": intent["desired_outcome"],
        "non_negotiables": intent["non_negotiables"],
    }
    research = {
        "required": False,
        "status": "context_only",
        "goal": "找到与用户需求世界语境匹配的人类顶级交付，并组合各作品的最强体验维度",
        "reference_atlas": deepcopy(REFERENCE_ATLAS),
        "experience_intent": intent,
        "golden_task": golden,
        "reference_requirements": {
            "primary_sources_required": True,
            "dimension_leaders_required": True, "same_task_evidence_required": True,
            "reference_artifacts_required": True,
            "rendered_state_evidence_required": True,
            "observed_interaction_evidence_required": True,
        },
        # 证据可支持相应声明，不因目标级别自动生成真人任务或阻止作品推进。
        "human_evidence_requirements": {
            "plan_as_explicit_tasks": False,
            "modes": ["expert_review", "novice_test", "blind_evaluation"],
            "required_fields": ["recruit_source", "sample_size", "scale", "evidence_artifact"],
            "fallback_policy": ("缺少真人证据时不得声称经过真人验证；保留缺口事实，"
                                 "其余可执行工作继续由成员决定"),
        },
        "comparison_policy": {
            "scope": "comparison_claims_only",
            "minimum_parity": parity_dimensions,
            "no_regression": ["correctness", "usability", "reliability"],
            "verified_breakthrough_dimensions": 2,
            "unknown_means": "PARITY_OR_INCONCLUSIVE",
            "implementation_brief_required": False,
            "dimension_acceptance_required": False,
            "release_requires_parity_evidence": False,
        },
    }
    # 用户或模型显式给出的研究/比较要求优先，不能被平台默认建议覆盖。
    if isinstance(source.get("frontier_research_contract"), dict):
        research.update(deepcopy(source["frontier_research_contract"]))
    if isinstance(source.get("north_star"), dict):
        north_star = {**north_star, **deepcopy(source["north_star"])}
    return {"quality_profile": quality_profile, "experience_intent": intent,
            "golden_task": golden, "north_star": north_star,
            "critical_dimensions": critical_dimensions,
            "claim_ceiling": "PARITY_OR_INCONCLUSIVE" if quality_profile == "frontier" else "UNVALIDATED",
            "frontier_research_contract": research,
            **{key: deepcopy(source[key]) for key, value_type in (
                ("quality_context", dict), ("source_ref", dict),
                ("critical_dimensions", list), ("claim_ceiling", str),
            ) if isinstance(source.get(key), value_type)}}


def augment_plan(plan: dict | None) -> dict:
    """补充体验/来源上下文；组织方式、任务正文与依赖只由本单计划决定。"""
    source = deepcopy(plan) if isinstance(plan, dict) else {}
    # 历史版本曾把缺固定质量角色作为执行门。保留旧观察，但不再赋予阻断权。
    legacy = source.get("world_context_incomplete")
    if isinstance(legacy, dict):
        source["world_context_incomplete"] = {
            **legacy, "blocking": False, "enforcement": "context_only",
        }
    raw_intent = source.get("experience_intent")
    if not isinstance(raw_intent, dict) or not raw_intent:
        return source
    compiled = compile_experience_contract(source)
    for key, value in compiled.items():
        # 缺省键可补齐；显式目标、来源与评审要求不被默认投影覆盖。
        if isinstance(value, dict) and isinstance(source.get(key), dict):
            source[key] = {**value, **source[key]}
        else:
            source.setdefault(key, value)
    return source


def context_for_plan(plan: dict | None) -> dict:
    """Return named references selected only from explicit plan facts, never prompt keywords."""
    source = plan if isinstance(plan, dict) else {}
    explicit = source.get("benchmark_families") or source.get("benchmark_family") or []
    if isinstance(explicit, str):
        explicit = [explicit]
    family_ids = [str(value) for value in explicit if str(value) in BENCHMARKS]
    if not family_ids:
        formats = _artifact_formats(source)
        family_ids = [key for key, value in BENCHMARKS.items()
                      if formats.intersection(value.get("formats") or [])]
    families = {key: deepcopy(BENCHMARKS[key]) for key in dict.fromkeys(family_ids)}
    return {
        "status": "reference_context_only",
        "reference_atlas": deepcopy(REFERENCE_ATLAS),
        "families": families,
        "same_golden_task_required": True,
        "warning": ("这些具名作品只定义世界级比较方向；只有相同输入、限制、预期结果、可打开参照产物"
                    "和独立盲评全部齐备，才能形成超越证据。"),
    }
