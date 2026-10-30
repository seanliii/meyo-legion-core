#!/usr/bin/env python3
"""multi_model_panel · v13.88.9 · 2026-07-30

用户思路:「多模型并行走查·大家的最强模型都有能力·比环路里单模型 llm_judge 强。
这能力该沉到底层·搜索/演绎/监控/进化/社区五 agent 都能调。」

真探证实:美团 aigc.sankuai.com 网关(同一个 ANTHROPIC_AUTH_TOKEN·不用配新 key)
支持一大堆最强模型:
  gpt-5.6-luna/terra · kimi-k2.7-code · deepseek-v4-pro/flash ·
  glm-5.2 · MiniMax-M3 · LongCat-2.0 · claude(opus-4.8/sonnet-4.5/haiku-4.5)

为什么比单模型强(守 [[feedback-single-evidence-lying]] 单一证据=撒谎·多路交叉):
  单模型 llm_judge = 一个模型说了算·它的幻觉/偏见无人纠正。
  多模型交叉 = 几家最强模型各判一遍·多数一致才采信·分歧升级仲裁。
  实测:问「V2EX 有没有API」·Kimi 说没有·DeepSeek 说有社区API(更准)→交叉逼出真相。

一行调用(五 agent 都用):
  from multi_model_panel import panel_judge, panel_ask
  r = panel_judge("这个搜索结果说的抓法靠谱吗?", context=corpus)   # 交叉校验·返共识
  r = panel_ask("V2EX 怎么抓")                                    # 多模型各答·返全部+共识
"""
import os
import sys
import json
import re
import concurrent.futures
from datetime import datetime
from pathlib import Path

V11 = Path(__file__).resolve().parent.parent
LOG = V11 / ".multi_model_panel.jsonl"

GATEWAY = "https://aigc.sankuai.com/v1/openai/native/chat/completions"

# ── 韧性接入 (2026-08-20 · 治「LLM API 429/超时被 except 静默吞掉整票」)─────────
#   复用公共模块 resilient_fetch: 按 model 限流 + 429/timeout 退避重试(不缓存·要新鲜结果)
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
try:
    import resilient_fetch as _rf  # 同目录公共韧性模块
except Exception:  # pragma: no cover - 拿不到就退回裸调(不破坏原行为)
    _rf = None

_LLM_RATE_PER_SEC = 5          # 按 model 维度限流
_LLM_RETRY_BACKOFFS = (1.0, 2.0)
if _rf is not None:
    _LLM_RATE_LIMITER = _rf.RateLimiter(rate_root=_HERE / ".multi_model_ratelimit")
else:
    _LLM_RATE_LIMITER = None

# 真探确认可调的模型(2026-08-12 更新)· 按「厂商多样性」选默认走查阵容
# 跨厂商才有交叉价值(同厂模型偏见相近)·限流的(kimi-k3/claude-sonnet-5)不入默认
# 2026-08-12: 加入 Gemini-2.5-flash / doubao-1.5-pro-32k（实测均可通过网关调用）
PANEL_DEFAULT = [
    "deepseek-v4-pro",        # DeepSeek · 推理强
    "kimi-k2.7-code",         # Kimi · 代码/抓取
    "glm-5.2",                # 智谱 · 中文
    "gpt-5.6-terra",          # GPT
    "LongCat-2.0",            # 美团自家
    "gemini-2.5-flash",       # Google · 实测 OK · 多模态强
    "doubao-1.5-pro-32k",     # 字节豆包 · 中文理解强
]
# 仲裁模型(分歧时用最强的裁)
ARBITER = "gpt-5.6-luna"
# 快档(单模型够用时·省)
FAST_ONE = "deepseek-v4-flash"

# ── 模型原生知识检索（不是真实联网·但用多模型交叉有信息互补效果）─────────────
# 说明:这些模型通过美团网关调用·不带实时联网工具·但各模型训练截止时间不同·
# 多模型交叉问同一个问题能挖出"某模型知道但另一个不知道"的差异信息
# Gemini 系列训练数据比 GPT 更新·豆包中文语料更全·形成互补
SEARCH_CAPABLE_MODELS = [
    "gemini-2.5-pro",         # Google 最强·训练数据新·多模态·中英双强
    "gemini-2.5-flash",       # Google 快版·轻量高效
    "doubao-1.5-pro-32k",     # 字节豆包·中文深度·抖音/头条语料
    "deepseek-v4-pro",        # DeepSeek·推理/代码强
    "gpt-5.6-luna",           # GPT 最强版
    "MiniMax-M3",             # MiniMax·长上下文
]


def _call_one(model: str, system: str, user: str, timeout: int = 40,
              max_tokens: int = 500) -> dict:
    """调单个模型 · 返回 {model, ok, content, err}"""
    import urllib.request
    tok = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    body = json.dumps({
        "model": model,
        "messages": ([{"role": "system", "content": system}] if system else [])
                    + [{"role": "user", "content": user}],
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(GATEWAY, data=body, method="POST",
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})

    # 按 model 维度限流(阻塞式)·429 常发·避免多模型并发把网关刷爆
    if _LLM_RATE_LIMITER is not None:
        _LLM_RATE_LIMITER.acquire(f"llm:{model}", rate_per_sec=_LLM_RATE_PER_SEC, block=True)

    def _do_call():
        """单次 urlopen·把 429/超时归一成可被 is_transient 识别的错误串。"""
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                d = json.loads(resp.read().decode("utf-8"))
            content = d.get("choices", [{}])[0].get("message", {}).get("content", "")
            if content:
                return {"model": model, "ok": True, "content": content}
            return {"model": model, "ok": False, "err": "空返回"}
        except Exception as e:
            return {"model": model, "ok": False, "err": str(e)[:80]}

    def _is_transient(res):
        # 只对 429/限流/超时类失败重试·"空返回"等非通道故障不重试
        if not isinstance(res, dict) or res.get("ok"):
            return False
        return bool(res.get("err")) and _rf.is_transient_error(res.get("err"))

    try:
        if _rf is not None:
            return _rf.with_retry(_do_call, is_transient_fn=_is_transient,
                                  max_retries=2, backoffs=_LLM_RETRY_BACKOFFS)
        return _do_call()
    except Exception as e:  # 保底: 保留原有 except 语义·任何异常都成 err dict
        return {"model": model, "ok": False, "err": str(e)[:80]}


def panel_ask(question: str, system: str = "", models: list = None,
              timeout: int = 40) -> dict:
    """多模型并行各答一版 · 返回 {answers:[{model,content}], n_ok}
    五 agent 通用:搜索多角度、演绎多视角、监控多判根因。"""
    models = models or PANEL_DEFAULT
    answers = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(models)) as pool:
        futs = {pool.submit(_call_one, m, system, question, timeout): m for m in models}
        for f in concurrent.futures.as_completed(futs):
            r = f.result()
            if r.get("ok"):
                answers.append({"model": r["model"], "content": r["content"]})
    return {"question": question[:100], "answers": answers, "n_ok": len(answers)}


def panel_judge(question: str, context: str = "", models: list = None,
                timeout: int = 40) -> dict:
    """多模型交叉校验 · 每个模型返 verdict(yes/no/uncertain)+reason ·
    多数一致→共识;分歧→ARBITER 仲裁。返回 {verdict, agreement, votes, arbiter}
    比单模型 llm_judge 强:一家幻觉被其他家纠正(守单一证据=撒谎)。"""
    models = models or PANEL_DEFAULT
    system = ("你是严格评审。只返 JSON。读问题+材料·给判断。"
              '{"verdict":"yes|no|uncertain","reason":"20字内"}')
    user = f"问题: {question}\n\n材料:\n{context[:5000]}\n\n返 JSON。"

    votes = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(models)) as pool:
        futs = {pool.submit(_call_one, m, system, user, timeout, 200): m for m in models}
        for f in concurrent.futures.as_completed(futs):
            r = f.result()
            if not r.get("ok"):
                continue
            m = re.search(r"\{[\s\S]*?\}", r["content"])
            if not m:
                continue
            try:
                v = json.loads(m.group())
                votes.append({"model": r["model"],
                              "verdict": v.get("verdict", "uncertain"),
                              "reason": v.get("reason", "")[:40]})
            except Exception:
                continue

    # 统计共识
    from collections import Counter
    tally = Counter(v["verdict"] for v in votes)
    result = {"question": question[:100], "votes": votes,
              "tally": dict(tally), "n_voted": len(votes)}
    if not votes:
        result["verdict"] = "no_response"
        result["agreement"] = 0.0
        _save(result)
        return result

    top, top_n = tally.most_common(1)[0]
    agreement = top_n / len(votes)
    result["verdict"] = top
    result["agreement"] = round(agreement, 2)

    # 分歧大(共识<60%)→ 仲裁
    if agreement < 0.6:
        arb = _call_one(ARBITER, system + "·你是终裁·多个模型有分歧",
                        user + f"\n\n各模型投票:{json.dumps(votes, ensure_ascii=False)}",
                        timeout, 200)
        if arb.get("ok"):
            m = re.search(r"\{[\s\S]*?\}", arb["content"])
            if m:
                try:
                    av = json.loads(m.group())
                    result["verdict"] = av.get("verdict", top)
                    result["arbiter"] = {"model": ARBITER,
                                         "verdict": av.get("verdict"),
                                         "reason": av.get("reason", "")[:40]}
                except Exception:
                    pass
    _save(result)
    return result


def panel_search(query: str, models: list = None, timeout: int = 30,
                 max_tokens: int = 400) -> list[dict]:
    """多模型知识互补搜索 · 用于 search_fabric 的 panel_search 源

    原理：各模型训练截止时间不同、语料侧重不同
      - Gemini: Google 知识图谱 + 训练数据较新
      - 豆包:   中文/字节系语料丰富
      - DeepSeek: 推理/代码/学术
      - GPT:    英文知识深度
    多模型各答一版 → 去重合并 → 返回 hit 列表供 fabric 使用

    注意: 这不是真实联网搜索·是知识检索·适合"历史/定义/解释/方法"类查询
    时效性问题由 search_fabric 的 ddgs/wikipedia 层覆盖
    """
    models = models or SEARCH_CAPABLE_MODELS[:4]  # 默认取前4个省配额
    system = ("你是知识检索助手。根据查询直接提供你所知道的信息，"
              "格式: 先给要点，再给来源/依据（如有）。不要说'我不知道'，"
              "尽力提供相关信息即可。300字内。")
    results = panel_ask(query, system=system, models=models, timeout=timeout)
    hits = []
    seen = set()
    for ans in results.get("answers", []):
        content = ans.get("content", "").strip()
        model = ans.get("model", "")
        if not content or len(content) < 20:
            continue
        key = content[:100]
        if key in seen:
            continue
        seen.add(key)
        hits.append({
            "url": f"[panel:{model}]",
            "title": f"[{model}] {query[:60]}",
            "content": content[:4000],
            "source": f"panel_{model}",
            "cost_usd": 0.0,
            "meta": {
                "model_native_search": True,
                "panel_model": model,
                "engine": "multi_model_panel",
                "knowledge_retrieval": True,  # 标记: 知识检索非实时联网
            },
        })
    return hits


def _save(rec: dict):
    try:
        rec2 = dict(rec)
        rec2["ts"] = datetime.now().isoformat()
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec2, ensure_ascii=False) + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--ask", help="多模型各答一版")
    ap.add_argument("--judge", help="多模型交叉校验(配 --context)")
    ap.add_argument("--context", default="")
    ap.add_argument("--probe", action="store_true", help="探网关支持哪些模型")
    args = ap.parse_args()

    if args.probe:
        cands = ["gpt-5.6-luna", "gpt-5.6-terra", "kimi-k2.7-code", "deepseek-v4-pro",
                 "deepseek-v4-flash", "glm-5.2", "MiniMax-M3", "LongCat-2.0", "glm-5v-turbo"]
        for m in cands:
            r = _call_one(m, "", "回:通", timeout=12, max_tokens=8)
            print(f"  {'✅' if r['ok'] else '❌'} {m}" + ("" if r["ok"] else f" · {r.get('err','')[:40]}"))
    elif args.ask:
        r = panel_ask(args.ask)
        print(f"═══ {r['n_ok']} 模型各答 ═══")
        for a in r["answers"]:
            print(f"\n── {a['model']} ──\n{a['content'][:200]}")
    elif args.judge:
        r = panel_judge(args.judge, args.context)
        print(f"═══ 交叉校验 ═══\n共识: {r['verdict']} (一致度 {r.get('agreement')})")
        print(f"投票: {r.get('tally')}")
        for v in r.get("votes", []):
            print(f"  {v['model']}: {v['verdict']} · {v['reason']}")
        if r.get("arbiter"):
            print(f"仲裁({r['arbiter']['model']}): {r['arbiter']['verdict']}")
    else:
        ap.print_help()
