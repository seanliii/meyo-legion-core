# -*- coding: utf-8 -*-
"""Minimal Cordis-style hook registry for the thin loop.

Cordis keeps the agent loop tiny and moves policy/interception onto a few typed
event points (pre-step waterfall, turn-stopping serial). This is the Python-minimal
equivalent: strategies register as listeners instead of being hardwired inside the
loop, so a gate can be added/removed without touching loop control flow.

Two hook points mirror Cordis:
- pre_step (waterfall): each listener may append guidance/context messages before a
  model call. Listeners run in registration order; each returns a list of extra
  message dicts (role/content) or None.
- turn_stopping (serial): before accepting a plain-text answer as final, each listener
  votes. Returning ("block", reason) vetoes the stop; ("allow", _) or None permits it.
  First veto wins (short-circuit), matching Cordis single-decision semantics.

No global side effects; the registry is per-process and starts empty. Enabled only in
thin mode; legacy path is untouched.
"""
from __future__ import annotations

_PRE_STEP: list = []
_TURN_STOPPING: list = []


def register_pre_step(fn) -> None:
    if fn not in _PRE_STEP:
        _PRE_STEP.append(fn)


def register_turn_stopping(fn) -> None:
    if fn not in _TURN_STOPPING:
        _TURN_STOPPING.append(fn)


def clear() -> None:
    _PRE_STEP.clear()
    _TURN_STOPPING.clear()


def run_pre_step(session: dict, step: int) -> list:
    """Waterfall: collect extra context messages from all listeners (in order)."""
    out = []
    for fn in list(_PRE_STEP):
        try:
            msgs = fn(session, step)
            if msgs:
                out.extend(msgs)
        except Exception:
            continue
    return out


def run_turn_stopping(session: dict, answer: str) -> tuple:
    """Serial + short-circuit: first listener that returns ('block', reason) vetoes.
    Returns (should_stop: bool, reason: str)."""
    for fn in list(_TURN_STOPPING):
        try:
            verdict = fn(session, answer)
        except Exception:
            continue
        if isinstance(verdict, tuple) and verdict and verdict[0] == "block":
            return False, (verdict[1] if len(verdict) > 1 else "blocked")
    return True, ""


# ── Default listeners (opt-in) — the accumulated gates as removable hooks ───────
def _anchor_pre_step(session: dict, step: int):
    """pre-step: after some evidence exists, remind the model of distinctive source
    anchors so it converges on exact titles/names rather than paraphrasing. Fires at
    most once, only when evidence is present. Reversible via clear()."""
    if session.get("_anchor_hint_done"):
        return None
    ev = session.get("evidence") or []
    if len(ev) < 3 or step < 1:
        return None
    session["_anchor_hint_done"] = True
    return [{"role": "user", "content":
             "If a result named a distinctive title/person/quote, search it verbatim "
             "(quoted) to converge; do not keep paraphrasing."}]


def _no_progress_pre_step(session: dict, step: int):
    """pre-step: if the loop keeps calling tools without the evidence set growing, nudge it
    to converge and answer. Generic state signal only (evidence count vs steps) — no domain
    or benchmark cues. Fires at most a few times, spaced out, then stays quiet."""
    ev = session.get("evidence") or []
    n_ev = len(ev)
    last_n = session.get("_np_last_ev_n")
    stale = session.get("_np_stale_rounds", 0)
    session["_np_last_ev_n"] = n_ev
    if last_n is not None and n_ev <= last_n:
        stale += 1
    else:
        stale = 0
    session["_np_stale_rounds"] = stale
    # Only after real work has happened, and only when genuinely stalling.
    fired = session.get("_np_nudge_fired", 0)
    if step >= 8 and stale >= 3 and n_ev >= 3 and fired < 2:
        session["_np_nudge_fired"] = fired + 1
        session["_np_stale_rounds"] = 0
        return [{"role": "user", "content":
                 "Recent tool calls added no new evidence. Do not repeat similar searches. "
                 "Decide now from the evidence you have: give the exact answer entity, or make "
                 "one decisively different move (a competing hypothesis or a verbatim anchor)."}]
    return None


def _converge_pre_step(session: dict, step: int):
    """pre-step convergence signal (advisory, model decides). Root cause of the dead legacy top-Opus
    0/38: the model kept searching to the wall and never chose to finalize (32/38 empty).
    After enough steps, remind it that it MAY finalize now with a best-effort exact answer.
    This does not block or force anything — it just tells the model finalizing is allowed,
    which the pure 'no gate at all' loop never signaled. Fires a couple times, spaced."""
    fired = session.get("_conv_nudge_fired", 0)
    # Fire after a handful of steps: with live search ~30s/step, a 200s wall only allows
    # ~6 steps, so a step>=12 threshold never triggers before timeout. Nudge from step 5,
    # spaced by 3, so the model gets the "you may finalize now" signal in time.
    if step >= 5 and fired < 4 and (step - session.get("_conv_last_step", -99)) >= 3:
        session["_conv_nudge_fired"] = fired + 1
        session["_conv_last_step"] = step
        return [{"role": "user", "content":
                 "You have searched enough. It is time to answer: call finalize_answer with "
                 "your single best exact entity/value now, based on the strongest evidence so "
                 "far. Do not keep searching indefinitely — a best-effort exact answer beats "
                 "running out of time with no answer. If genuinely undetermined, finalize with "
                 "your most likely candidate, not a status sentence."}]
    return None


def _readiness_turn_stopping(session: dict, answer: str):
    """turn-stopping: veto a stop when the answer is meta-noise or clearly empty, once.
    Keeps the loop from finalizing a status sentence. Bounded to one veto per session."""
    if session.get("_readiness_vetoed"):
        return ("allow", "")
    try:
        import qa_auto_agent as _qa
        if _qa._looks_like_finalize_noise(answer) or not str(answer).strip():
            session["_readiness_vetoed"] = True
            return ("block", "That looks like a status/meta line, not the answer. "
                             "Give the exact answer entity, or search once more if unsure.")
    except Exception:
        pass
    return ("allow", "")


def _context_ledger_pre_step(session: dict, step: int):
    """pre-step: 每步把【统一分类全局状态】(搜索路径/候选/证据/失败/约束)渲染给模型,
    让它看到完整过程自主判断方向(给 context 不加 control·用户 2026-08-26)。
    AB 已证:给搜索路径全景,00c 从瞎猜→命中。fail-safe→None。env MEYO_CONTEXT_LEDGER=0 关。"""
    try:
        import context_ledger as _cl
        block = _cl.render(session)
        if not block:
            return None
        return [{"role": "user", "content": block}]
    except Exception:
        return None


def _finalize_hints_pre_step(session: dict, step: int):
    """pre-step(P2 门=context 闭环): 把 finalize 门在 VETO=0 下检测到的信号
    (存在 session["_finalize_hints"])drain 出来注入为 context——让门检测的信号
    真到模型手里·由模型自主判断是否处理·而不是既不拦也不告知(信号丢失)。
    非阻塞·drain 后清空(不重复注入撑窗)。VETO=1 时门直接拦截·此处无 hint 可 drain。"""
    try:
        hints = session.get("_finalize_hints") or []
        if not hints:
            return None
        session["_finalize_hints"] = []
        lines = [f"· [{h.get('gate','')}] {str(h.get('hint',''))[:600]}"
                 for h in hints if h.get("hint")]
        if not lines:
            return None
        return [{"role": "user", "content":
                 "【交付前·系统检测信号(仅供参考·不强制·你自主判断是否需处理再交付)】\n"
                 + "\n".join(lines)}]
    except Exception:
        return None


def install_default_hooks() -> None:
    """Register the default listeners. Idempotent.

    Model-driven, but with ONE advisory convergence signal: the pure no-gate loop let the
    model search to the wall and never finalize (dead legacy top-Opus: 32/38 empty). _converge_pre_step
    tells the model, after enough steps, that finalizing now with a best-effort answer is
    allowed and preferable to timing out empty. It never blocks/forces — the model decides.
    The readiness floor still prevents committing literal meta-noise as the answer.

    _context_ledger_pre_step 给模型统一分类全局状态(给 context 不加 control)。
    _finalize_hints_pre_step 把 VETO=0 下门检测的信号透明喂回模型(P2 门=context 闭环)。"""
    register_pre_step(_context_ledger_pre_step)
    register_pre_step(_converge_pre_step)
    register_pre_step(_finalize_hints_pre_step)
    register_turn_stopping(_readiness_turn_stopping)
