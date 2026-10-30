# -*- coding: utf-8 -*-
"""Typed, model-owned, durable final answer (RC1/RC2/RC3/RC9).

The audit (auto_vs_winners_trace_audit_2026-08-26) found that Auto repeatedly *authored*
the correct answer and then lost it: a semantic finalize gate rejected it, a wall-kill left
the checkpoint `running` with an empty final, or a father-process圈选 fabricated a wrong one.
Meanwhile the delivered text mixed narration ("工具记录明确显示…", "答案:") with the entity.

This module makes the evidence->answer boundary a single typed commit:

  commit(session, text) -> Artifact   # authoritative, persisted immediately
  render(artifact)       -> str        # clean user-facing text
  current(session)       -> Artifact | None

Design rules (mirror finalize_governor's fail-safe stance, opposite authority stance):
  1. Pure/струct only — no model calls, no I/O beyond the session dict handed in.
  2. The MODEL decides correctness. This layer only strips narration wrappers and records
     typed slots + provenance. It NEVER vetoes on semantics and never invents an answer.
  3. Once committed, the artifact is the durable truth. Recovery may surface it verbatim but
     must not synthesize a new answer from fragments.
"""
from __future__ import annotations

import re
import time

# Narration prefixes that solvers leak in front of the real answer (RC9). Generic, not
# question-specific: these are meta/framing tokens, never answer content.
_PREFIXES = [
    r"^\s*答案\s*[:：]\s*",
    r"^\s*最终答案\s*[:：]\s*",
    r"^\s*the answer is\s*[:：]?\s*",
    r"^\s*my final answer\s*[:：]?\s*",
    r"^\s*final answer\s*[:：]?\s*",
    r"^\s*工具记录[^，,。\n]*[，,]\s*",
    r"^\s*根据[^，,。\n]*[，,]\s*",
    r"^\s*based on[^,\n]*,\s*",
    r"^\s*##\s*answer\s*[:：]?\s*",
    r"^\s*##\s*答案[^\n]*\n+",
]

_YEAR = re.compile(r"(?<!\d)(1[5-9]\d\d|20\d\d)(?!\d)")
_NUM = re.compile(r"-?\d[\d,]*\.?\d*%?")


def _strip_prefixes(text: str) -> str:
    t = str(text or "").strip()
    changed = True
    while changed and t:
        changed = False
        for pat in _PREFIXES:
            new = re.sub(pat, "", t, count=1, flags=re.I)
            if new != t:
                t, changed = new.strip(), True
    return t


def _display(text: str) -> str:
    """Clean display from model-authored structure without guessing semantics.

    If the model emitted an explicit final-answer marker on a later line, that payload is
    authoritative; otherwise strip only leading narration wrappers. Markdown emphasis is
    presentation, not answer content. Never synthesize a value that the model did not write.
    """
    raw = str(text or "").strip()
    # Prefer the last explicit marker: reasoning may itself mention an earlier "answer".
    marker = re.compile(r"(?:^|\n)\s*(?:答案|最终答案|my final answer|final answer)\s*[:：]\s*",
                        re.I)
    matches = list(marker.finditer(raw))
    t = raw[matches[-1].end():].strip() if matches else _strip_prefixes(raw)
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)  # unbold
    return t.strip() or raw


def extract_slots(text: str) -> dict:
    """Best-effort typed slots for downstream scoring/telemetry. Never authoritative over
    the model's own text — purely additive structural signal."""
    d = _strip_prefixes(text)
    slots = {"text": d[:400]}
    yrs = _YEAR.findall(d)
    if yrs:
        slots["years"] = sorted(set(yrs))
    nums = []
    for tok in _NUM.findall(d):
        raw = tok.rstrip("%").replace(",", "")
        try:
            nums.append(float(raw))
        except ValueError:
            pass
    if nums:
        slots["numbers"] = nums[:16]
    return slots


def make_artifact(text: str, *, evidence_ids=None, provenance="model_final",
                  now=None) -> dict:
    disp = _display(text)
    return {
        "schema": "answer-artifact-v1",
        "raw": str(text or "")[:4000],
        "display": disp,
        "slots": extract_slots(text),
        "evidence_ids": list(evidence_ids or [])[:64],
        "provenance": provenance,        # model_final | plain_text | unstructured_fallback
        "committed_at": float(now if now is not None else time.time()),
    }


def commit(session, text, *, evidence_ids=None, provenance="model_final",
           persist=None, now=None):
    """Commit the model's final as the authoritative artifact and persist immediately.

    `persist` is an optional callable(session) invoked right after the artifact is set, so a
    wall-kill after this point still leaves the answer on disk (RC2). Fail-safe: a persist
    error never loses the in-memory artifact.
    """
    art = make_artifact(text, evidence_ids=evidence_ids, provenance=provenance, now=now)
    if isinstance(session, dict):
        session["_answer_artifact"] = art
        # Keep the legacy string field in sync so every existing reader sees the CLEAN text.
        session["final"] = art["display"] or art["raw"]
        if persist is not None:
            try:
                persist(session)
            except Exception:
                pass
    return art


def current(session):
    return session.get("_answer_artifact") if isinstance(session, dict) else None


def render(artifact) -> str:
    if not isinstance(artifact, dict):
        return ""
    return artifact.get("display") or artifact.get("raw") or ""
