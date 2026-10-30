#!/usr/bin/env python3
"""云电脑前沿交付验证器：只按可复核事实给出窄面胜出、持平或落后。"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import desktop_workspace

VERDICT_WIN = "VERIFIED_NARROW_WIN"
VERDICT_PARITY = "PARITY_OR_INCONCLUSIVE"
VERDICT_BELOW = "BELOW_FRONTIER"
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(root: Path, value: str) -> Path | None:
    candidate = Path(str(value or ""))
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        candidate = candidate.resolve()
        if os.path.commonpath((str(root.resolve()), str(candidate))) != str(root.resolve()):
            return None
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() and not candidate.is_symlink() else None


def _validate_file(path: Path) -> dict:
    suffix = path.suffix.lower()
    base = {"ok": path.stat().st_size > 0, "bytes": path.stat().st_size,
            "sha256": _sha256(path), "kind": suffix.lstrip(".") or "file"}
    if not base["ok"]:
        return base
    try:
        if suffix == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            text = "\n".join(page.extract_text() or "" for page in reader.pages).strip()
            base.update({"ok": bool(reader.pages) and len(text) >= 20, "pages": len(reader.pages), "text_chars": len(text)})
        elif suffix in {".xlsx", ".xlsm"}:
            import openpyxl
            workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
            rows = sum(1 for _ in workbook.active.iter_rows(values_only=True))
            base.update({"ok": rows >= 1, "rows": rows, "sheet": workbook.active.title})
            workbook.close()
        elif suffix == ".pptx":
            from pptx import Presentation
            presentation = Presentation(str(path))
            base.update({"ok": len(presentation.slides) >= 1, "slides": len(presentation.slides)})
        elif suffix in _IMAGE_SUFFIXES:
            from PIL import Image
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                base.update({"ok": image.width > 1 and image.height > 1,
                             "width": image.width, "height": image.height})
        elif suffix in {".html", ".htm"}:
            text = path.read_text(encoding="utf-8", errors="ignore")
            base.update({"ok": "<" in text and len(text.strip()) >= 40, "text_chars": len(text)})
        elif suffix in {".py", ".js", ".ts", ".sh"}:
            text = path.read_text(encoding="utf-8", errors="ignore")
            base.update({"ok": len(text.strip()) >= 10, "text_chars": len(text)})
        elif suffix == ".svg":
            text = path.read_text(encoding="utf-8", errors="ignore")
            base.update({"ok": "<svg" in text and len(text.strip()) >= 40, "text_chars": len(text)})
        elif suffix == ".json":
            parsed = json.loads(path.read_text(encoding="utf-8"))
            base.update({"ok": isinstance(parsed, (dict, list)) and bool(parsed), "entries": len(parsed)})
        else:
            base["ok"] = path.stat().st_size >= 1
    except Exception as exc:
        base.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return base


def _reference_ok(reference: dict) -> bool:
    url = str(reference.get("url") or "")
    parsed = urlparse(url)
    local_file = parsed.scheme == "file" and Path(parsed.path).is_absolute() and Path(parsed.path).is_file()
    return bool(str(reference.get("name") or "").strip()
                and str(reference.get("work") or "").strip()
                and ((parsed.scheme in {"http", "https"} and parsed.netloc) or local_file))


def _comparison_location_ok(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    parsed = urlparse(text)
    return bool(parsed.scheme in {"http", "https"} and parsed.netloc) or Path(text).is_absolute()


def _reference_comparison_ok(references: list[dict], golden: dict) -> bool:
    required = {"input", "constraints", "expected_user_outcome"}
    for reference in references:
        comparison = reference.get("comparison")
        if not isinstance(comparison, dict) or comparison.get("same_golden_task") is not True:
            return False
        if not required.issubset(key for key, value in comparison.items() if str(value or "").strip()):
            return False
        if any(str(comparison.get(key) or "").strip() != str(golden.get(key) or "").strip()
               for key in required):
            return False
        artifact_paths = comparison.get("artifact_paths")
        evidence_paths = comparison.get("evidence_paths")
        if not isinstance(artifact_paths, list) or not any(str(value or "").strip() for value in artifact_paths):
            return False
        if not isinstance(evidence_paths, list) or not any(str(value or "").strip() for value in evidence_paths):
            return False
        if not all(_comparison_location_ok(value) for value in [*artifact_paths, *evidence_paths]):
            return False
    return bool(references)


def _blind_reviews(reviews: list, dimension_keys: set[str]) -> tuple[bool, bool, list[str]]:
    valid, gaps = [], []
    if not dimension_keys:
        return False, False, ["盲评前必须先声明至少一个质量维度"]
    for review in reviews:
        if not isinstance(review, dict):
            continue
        candidate = review.get("candidate_scores")
        reference = review.get("reference_scores")
        scored = isinstance(candidate, dict) and isinstance(reference, dict)
        complete = scored and set(candidate) == dimension_keys and set(reference) == dimension_keys
        scores_valid = complete and all(
            isinstance(value, (int, float)) and 0 <= float(value) <= 100
            for value in [*candidate.values(), *reference.values()]
        )
        if (str(review.get("judge") or "").strip() and review.get("author_hidden") is True
                and scores_valid
                and str(review.get("preference") or "") in {"candidate", "reference", "tie"}):
            valid.append(review)
    judges = {str(row.get("judge")) for row in valid}
    if len(judges) < 2:
        gaps.append("至少需要两个隐藏作者的独立盲评上下文，且均须覆盖全部评分维度")
    preferences = {str(row.get("preference") or "") for row in valid}
    decisive = len(judges) >= 2 and preferences == {"candidate"}
    return len(judges) >= 2, decisive, gaps


def verify(uid: str, project_id: str, task_id: str, run: dict) -> dict:
    root = desktop_workspace._project_root(uid, project_id)
    checks, gaps, evidence = {}, [], []

    references = [row for row in run.get("references") or [] if isinstance(row, dict)]
    checks["identifiable_references"] = len(references) >= 1 and all(_reference_ok(row) for row in references)
    if not checks["identifiable_references"]:
        gaps.append("缺少具名个人/团队的具体作品与一手 URL")

    golden = run.get("golden_task") if isinstance(run.get("golden_task"), dict) else {}
    checks["golden_task"] = all(str(golden.get(key) or "").strip()
                                for key in ("input", "constraints", "expected_user_outcome"))
    if not checks["golden_task"]:
        gaps.append("缺少 candidate/reference 共用的 golden task")
    checks["same_task_references"] = (checks["golden_task"]
                                      and _reference_comparison_ok(references, golden))
    if not checks["same_task_references"]:
        gaps.append("每个参照都必须显式证明使用与 candidate 完全相同的输入、限制和用户结果")

    declared = run.get("evidence") or []
    required = [str(value) for value in (run.get("profile") or {}).get("required_artifacts") or []]
    files = []
    for row in declared:
        if not isinstance(row, dict):
            continue
        path = _inside(root, str(row.get("path") or ""))
        if not path:
            continue
        validation = _validate_file(path)
        evidence.append({"path": path.relative_to(root).as_posix(), **validation})
        files.append(path.relative_to(root).as_posix())
    checks["machine_evidence"] = bool(evidence) and all(row.get("ok") for row in evidence)
    declared_rows = [row for row in declared if isinstance(row, dict)]
    declared_ids = [str(row.get("evidence_id") or "") for row in declared_rows]
    checks["evidence_identity"] = (bool(declared_ids) and all(declared_ids)
                                   and len(declared_ids) == len(set(declared_ids)))
    if not checks["evidence_identity"]:
        gaps.append("证据 ID 必须非空且在本次运行内唯一")
    declared_hashes = {
        str(row.get("path") or ""): str(row.get("sha256") or "")
        for row in declared_rows if row.get("path")
    }
    checks["evidence_integrity"] = bool(evidence) and all(
        declared_hashes.get(str(row.get("path") or ""))
        and declared_hashes[str(row.get("path") or "")] == row.get("sha256")
        for row in evidence
    )
    if not checks["machine_evidence"]:
        gaps.append("机器证据缺失、不可打开或格式无效")
    if not checks["evidence_integrity"]:
        gaps.append("证据哈希缺失或文件已在登记后发生变化")
    checks["required_artifacts"] = all(any(path == wanted or path.endswith("/" + wanted)
                                               for path in files) for wanted in required) if required else bool(files)
    if not checks["required_artifacts"]:
        gaps.append("任务声明的必需产物没有全部通过真实打开验证")

    dimensions = [row for row in run.get("dimensions") or [] if isinstance(row, dict)]
    screenshots = [row for row in evidence if Path(str(row.get("path") or "")).suffix.lower() in _IMAGE_SUFFIXES]
    visual_dimensions = [row for row in dimensions if str(row.get("key") or "") == "aesthetics"]
    visual_required = bool(visual_dimensions) or bool((run.get("profile") or {}).get("visual_delivery"))
    checks["visual_evidence"] = (not visual_required) or bool(screenshots)
    if not checks["visual_evidence"]:
        gaps.append("视觉型任务缺少真实桌面或结果截图")

    dimension_keys = {str(row.get("key") or "") for row in dimensions if row.get("key")}
    claims = [row for row in run.get("claims") or [] if isinstance(row, dict)]
    evidence_ids = {str(row.get("evidence_id") or "") for row in declared if isinstance(row, dict) and row.get("verified")}
    audited = []
    for claim in claims:
        refs = {str(value) for value in claim.get("evidence_ids") or []}
        dimension = str(claim.get("dimension") or "")
        audited.append(bool(str(claim.get("claim") or "").strip()) and dimension in dimension_keys
                       and bool(refs) and refs.issubset(evidence_ids))
    checks["claim_audit"] = bool(audited) and all(audited)
    if not checks["claim_audit"]:
        gaps.append("每个超越声明必须绑定有效质量维度并映射到已验证的原始证据")

    dimension_contract_ok = bool(dimension_keys) and len(dimension_keys) == len(dimensions) and all(
        isinstance(row.get("weight"), (int, float)) and float(row["weight"]) > 0
        for row in dimensions
    ) and abs(sum(float(row["weight"]) for row in dimensions) - 1.0) <= 0.001
    checks["dimension_contract"] = dimension_contract_ok
    if not dimension_contract_ok:
        gaps.append("质量维度必须唯一、权重大于零且总和为 1")
    review_ok, decisive, review_gaps = _blind_reviews(run.get("reviews") or [], dimension_keys)
    checks["blind_comparison"] = review_ok
    gaps.extend(review_gaps)
    correctness = next((row for row in dimensions if row.get("key") == "reliability"), {})
    correctness_ok = (correctness.get("candidate_score") is not None
                      and correctness.get("reference_score") is not None
                      and float(correctness["candidate_score"]) >= float(correctness["reference_score"]))
    winning_dimensions = {
        str(row.get("key") or "") for row in dimensions
        if row.get("candidate_score") is not None and row.get("reference_score") is not None
        and float(row["candidate_score"]) > float(row["reference_score"])
    }
    claim_dimensions = {str(claim.get("dimension") or "") for claim in claims}
    checks["no_correctness_regression"] = correctness_ok
    checks["decisive_dimensions"] = len(winning_dimensions) >= 2
    checks["winning_claim_coverage"] = bool(winning_dimensions) and winning_dimensions.issubset(claim_dimensions)
    if not correctness_ok:
        gaps.append("可靠性/正确性不得低于参照")
    if len(winning_dimensions) < 2:
        gaps.append("至少需要两个可判定维度胜出")
    if not checks["winning_claim_coverage"]:
        gaps.append("每个计入胜出的质量维度都必须有对应的证据声明")

    complete = all(checks[key] for key in (
        "identifiable_references", "golden_task", "same_task_references", "machine_evidence", "evidence_identity",
        "evidence_integrity", "required_artifacts", "visual_evidence", "claim_audit", "dimension_contract",
        "blind_comparison", "no_correctness_regression", "winning_claim_coverage",
    ))
    if complete and checks["decisive_dimensions"] and decisive:
        verdict = VERDICT_WIN
    elif (not checks["machine_evidence"] or not checks["evidence_identity"]
          or not checks["evidence_integrity"] or not checks["required_artifacts"]
          or not checks["no_correctness_regression"]):
        verdict = VERDICT_BELOW
    else:
        verdict = VERDICT_PARITY
    return {"rubric_version": 1, "verdict": verdict, "passed": complete,
            "proven": verdict == VERDICT_WIN, "checks": checks, "evidence": evidence,
            "remaining_gaps": list(dict.fromkeys(gaps))}


def write_report(uid: str, project_id: str, task_id: str, report: dict) -> dict:
    root = desktop_workspace._project_root(uid, project_id)
    target = root / "evidence" / "quality-report.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": target.relative_to(root).as_posix(), "bytes": target.stat().st_size,
            "sha256": _sha256(target)}
