#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轻量分级预判:P0-A 真·分级直答运行时的第一级闸(2026-09-02·plan 见
dev/legion-remaining-p0-plans-2026-09-02.md P0-A 阶段1)。

定位:轻模型一次完成“是否需要 Harness”判断；只有它明确判 simple 且同时给出完整短答时，
该答案才可作为低延迟直答候选，避免先分类再冷启动 Harness 的双重成本。判 needs_harness、答案为空、
附件/工具/持续会话等现场不完整时，一律退回原生 Harness 与完整规划。

守界(context not control + 速度底线):
  ①失败即退:预判任何异常/超时 → 返回 needs_harness(走原路径·零回归);
  ②不写关键词规则:判断与最终短答交正式主力模型，本模块只包装一次调用管道;
  ③白名单只防误判下界:附件/显式多成员/长复杂题跳过预判直接走完整规划——这不是
    替模型判断"这是复杂题",是"预判没看到完整现场时不抢规划器的活"(预判只看题面)。
"""
from __future__ import annotations

import json
import os
import re
import time

_PRED_SCRIPT = os.path.expanduser(
    "~/.claude/skills/aigc-model-router/scripts/call_model.py")

# 预判超时(秒):比规划器(40-60s)小一个量级·超时=没预判=走原路径
_PRED_TIMEOUT_S = 8.0
# 阶段2·账本校准(2026-09-02):预判simple但solo失败/补位救回→记 bad;一次成→记 good。
# 连败≥_LEDGER_BAD_STREAK(且零近期成功)时快路径自动停用(退规划器零回归)——
# 阈值不是父层写死,是账本数据长出来的;预判器恢复(再次记 good)即自动重启。
_SCENE = "quick_pred"
_LEDGER_BAD_STREAK = 3


def fastpath_healthy() -> bool:
    """快路径账本健康检查:连败≥阈值且零成功 → 停用快路径(退规划器·零回归)。

    判据用 good/bad 原始计数(不用 success_lb——小样本 Wilson 会把 2/3 成功也压低):
    good>0 说明预判器对这类题仍有命中,不停;只有"全败"才说明 simple 预判系统性错误。
    """
    try:
        import harness_performance as _hp
        row = _hp.summarize_row(((_hp._load().get("quick_pred") or {}).get(_SCENE) or {}).get("solo") or {})
        return not (row["good"] == 0 and row["bad"] >= _LEDGER_BAD_STREAK)
    except Exception:
        return True   # 账本不可读=中性(不因账本故障关掉快路径)


def record_fastpath(ok: bool) -> None:
    """solo 落定后回写快路径账本(ok=该轮 solo 是否一次自交付)。永不抛。"""
    try:
        import harness_performance as _hp
        _hp.record("quick_pred", _SCENE, "solo", ok=ok, source="runtime")
    except Exception:
        pass


def _response_text(response: dict | None) -> str:
    message = (((response or {}).get("choices") or [{}])[0].get("message") or {})
    content = str(message.get("content") or "")
    if not content.strip():
        content = str(message.get("reasoning_content") or "")[-4000:]
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
    content = re.sub(r"^.*</think>", "", content, flags=re.DOTALL)
    return "" if "<think>" in content.lower() else content.strip()


def _decision(text: str) -> dict:
    raw = str(text or "").strip()
    candidates = [raw]
    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(value, dict):
            continue
        verdict = str(value.get("verdict") or "").strip().lower()
        answer = str(value.get("answer") or "").strip()
        profile = str(value.get("profile") or "general").strip().lower()
        if profile not in {"micro", "general"}:
            profile = "general"
        if verdict == "simple":
            return ({"verdict": "simple", "answer": answer, "profile": profile} if answer else
                    {"verdict": "needs_harness", "profile": profile,
                     "reason": "简单分类没有返回可交付答案"})
        if verdict == "needs_harness":
            return {"verdict": "needs_harness", "reason": str(value.get("reason") or "")[:200]}
    low = raw.lower()
    if "simple" in low and "needs" not in low:
        return {"verdict": "simple", "answer": ""}
    return {"verdict": "needs_harness", "raw": raw[:80]}


def _fast_candidates(cm, db: dict) -> list[dict]:
    trusted = [str(row.get("appid")) for row in (db.get("trusted_appids") or {}).get("list", [])
               if row.get("appid")]
    exhausted = set(str(value) for value in (db.get("quota_exhausted_appids") or {}).get("appids", []))
    trusted = [appid for appid in trusted if appid not in exhausted]
    try:
        import model_registry
        models = model_registry.scene_models("fast")
    except Exception:
        models = [str(row.get("model") or "") for row in cm.pick_candidates(
            db, "general_chat_high_coverage")]
    return [{"model": model, "appids": trusted, "status": "PASS"}
            for model in dict.fromkeys(models) if model and trusted]


def predict_simple(question: str, has_attachments: bool = False,
                   plan: dict | None = None, *, preferred_model: str = "",
                   preferred_appid: str = "") -> dict:
    """一次模型调用同时判断是否为简单题并生成短答；不确定时退回完整 Harness。"""
    q = str(question or "").strip()
    if not q:
        return {"verdict": "needs_harness", "reason": "空问题"}
    # 预判没看到完整现场(附件是视觉/文档现场·显式 tasks/pipeline 是用户已定的编排)——
    # 不抢规划器的活,直接交给完整规划。这是下界保护,不是替模型判复杂度。
    if has_attachments:
        return {"verdict": "needs_harness", "reason": "有附件·交完整规划"}
    if plan and (plan.get("tasks") or plan.get("pipeline")):
        return {"verdict": "needs_harness", "reason": "用户已显式编排"}
    plan_value = plan if isinstance(plan, dict) else {}
    if plan_value.get("project_context"):
        return {"verdict": "needs_harness", "reason": "需要持续项目现场"}
    conversation_context = str(plan_value.get("conversation_context") or "")
    # 正常 session 上游已按完整 turn 边界限制到 24k 字符；异常超大输入不在这里硬切，
    # 保守交给 Harness，避免把历史从消息中间裁断后误解用户。
    if len(conversation_context) > 24000:
        return {"verdict": "needs_harness", "reason": "会话历史超出直答上下文预算"}
    contract = plan_value.get("delivery_contract") if isinstance(plan_value.get("delivery_contract"), dict) else {}
    verification = plan_value.get("verification") if isinstance(plan_value.get("verification"), dict) else {}
    if contract.get("required_artifacts") or verification.get("enabled"):
        return {"verdict": "needs_harness", "reason": "需要真实产物或独立验证"}
    started = time.monotonic()
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_pred_cm", _PRED_SCRIPT)
        if not spec or not os.path.isfile(_PRED_SCRIPT):
            return {"verdict": "needs_harness", "reason": "预判脚本不可用"}
        cm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cm)
        db = cm.load_db()
        # 快答模型顺序由 model_registry 的场景事实给出；首选正式主力，不以降模型换速度。
        cands = _fast_candidates(cm, db)[:3]
        if preferred_model:
            if preferred_appid:
                cands = [{"model": str(preferred_model), "appids": [str(preferred_appid)],
                          "status": "PASS_EXPLICIT"}]
            else:
                cands = [row for row in cands if row.get("model") == str(preferred_model)]
        if not cands:
            return {"verdict": "needs_harness", "reason": "无候选模型"}
        prompt = (
            "判断用户请求能否由你在一次普通对话中直接、完整、可靠地回答，并只输出一个 JSON 对象。\n"
            "字段契约：verdict 只能是 simple 或 needs_harness；profile 只能是 micro 或 general；"
            "answer 必须是对用户请求的实际最终答复，不能复述本说明、字段名或占位文字。\n"
            "micro 指确定答案、极短格式化或一句命令式短答；翻译、改写、解释和开放问答属于 general。\n"
            "需要联网研究、文件/代码/设备工具、多步执行、持续上下文或多人协作时，使用 needs_harness，"
            "answer 置空并用 reason 简述原因。不要输出分析过程或 Markdown。\n\n"
            + (("最近会话上下文（只读，结合它理解当前请求）：\n" + conversation_context + "\n\n")
               if conversation_context else "")
            + f"用户当前请求：{q[:1200]}"
        )
        # ThreadPoolExecutor 的 context manager 会在离开时 wait=True；fut.result 虽 6s 超时，
        # 但底层 chat 若不响应，函数仍会在 __exit__ 等它几十秒，形成“超时是假、用户真等 90s”。
        # 显式 shutdown(wait=False)，预判超时必须真的立即退完整规划。
        import concurrent.futures as _cf
        ex = _cf.ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(cm.chat, prompt, cands[0]["model"],
                        cands[0]["appids"][0], max_tokens=128,
                        timeout=int(_PRED_TIMEOUT_S))
        try:
            model, _appid, resp = cm.run_with_retry(
                lambda m, a: fut.result(timeout=_PRED_TIMEOUT_S), cands[:1])
        except Exception:
            fut.cancel()
            return {"verdict": "needs_harness", "reason": "预判超时/失败·走原路径"}
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
        decision = _decision(_response_text(resp))
        decision.update({"model": model,
                         "elapsed_s": round(time.monotonic() - started, 3)})
        if decision["verdict"] == "simple" and decision.get("answer"):
            return decision
        invalid_payload = (not _response_text(resp) or decision.get("raw") is not None or
                           decision.get("reason") == "简单分类没有返回可交付答案")
        retry_candidates = (cands[1:2] if len(cands) > 1 else cands[:1]) if invalid_payload else []
        if retry_candidates:
            try:
                model, _appid, retry = cm.run_with_retry(
                    lambda m, a: cm.chat(prompt, m, a, max_tokens=128,
                                         timeout=int(_PRED_TIMEOUT_S)), retry_candidates)
                decision = _decision(_response_text(retry))
                decision.update({"model": model, "elapsed_s": round(time.monotonic() - started, 3),
                                 "retry": "invalid_structured_response"})
                return decision if decision.get("answer") or decision["verdict"] == "needs_harness" else {
                    "verdict": "needs_harness", "reason": "简单分类没有返回可交付答案"}
            except Exception:
                pass
        return {"verdict": "needs_harness", "reason": "简单分类没有返回可交付答案",
                "elapsed_s": round(time.monotonic() - started, 3)}
    except Exception as e:
        return {"verdict": "needs_harness", "reason": f"预判异常:{type(e).__name__}"}


if __name__ == "__main__":
    import sys
    r = predict_simple(sys.argv[1] if len(sys.argv) > 1 else "你好")
    print(json.dumps(r, ensure_ascii=False))
