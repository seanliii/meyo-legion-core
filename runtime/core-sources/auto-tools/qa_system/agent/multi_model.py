"""multi_model · 2026-08-10 · 模型即工具(用户架构: 用appid接多家模型·按题型编排·沉淀经验)

网关实测(aigc.sankuai.com)支持三家:
  - claude(opus/sonnet/haiku): Anthropic /v1/messages 格式
  - gpt-5 / gpt-4o-mini: OpenAI /v1/openai/native/chat/completions 格式
  - gemini-2.5-pro: 同 OpenAI 格式(网关兼容)·强多模态

统一入口:
  ask(model, prompt, system=, image_b64=) -> {ok,out,err,elapsed,model}
  MODELS: 可用模型注册表(名字→(family, 是否支持vision, 成本档, 速度档))

设计: 每家格式差异在本模块内消化·调用方(工具/编排器)只认统一 ask()。
fail-safe: 任何模型失败返回 {ok:False}·不抛异常·编排器可换模型重试。
"""
import os
import time
import json as _json

# ── 通用+自动化(2026-08-10 用户: 别人换key/加新模型也要通用·不能硬编码) ──────
# 模型清单不写死在代码·三层来源(优先级从高到低):
#   1. 用户配置 ~/.meyo/models.conf(JSON·用户加自己的模型/key·换网关都改这里)
#   2. 探测缓存 ~/.meyo/.models_probed.json(probe_models() 自动探测的结果·带能力)
#   3. 内置默认 _DEFAULT_MODELS(开箱即用·美团网关实测通的)
# 新模型只要在 models.conf 声明{name,family,gw,endpoint}·probe 会自动验证+补能力。
_DEFAULT_MODELS = {
    "claude-opus":    {"family": "claude", "vision": True, "cost": 5, "speed": 3, "gw": "sankuai,aws.claude-opus-4.7"},  # 2026-09-02:4.8已死·唯一活Opus=4.7
    "claude-sonnet":  {"family": "claude", "vision": True, "cost": 3, "speed": 2, "gw": "sankuai,aws.claude-sonnet-4.6"},
    "claude-haiku":   {"family": "claude", "vision": True, "cost": 1, "speed": 1, "gw": "sankuai,aws.claude-haiku-4.5"},
    "longcat-2.0":    {"family": "openai", "vision": True, "cost": 1, "speed": 2, "gw": "longcat-2.0"},  # v15·2026-08-17·替换 haiku·实测通·1.2s
    "gpt-5":          {"family": "openai", "vision": True, "cost": 5, "speed": 4, "gw": "gpt-5"},
    "gpt-4o-mini":    {"family": "openai", "vision": True, "cost": 1, "speed": 1, "gw": "gpt-4o-mini"},
    "gemini-2.5-pro": {"family": "openai", "vision": True, "cost": 3, "speed": 3, "gw": "gemini-2.5-pro"},
}
_CONF_PATH = os.path.expanduser("~/.meyo/models.conf")
_PROBE_CACHE = os.path.expanduser("~/.meyo/.models_probed.json")
_GW_OPENAI_BASE = os.environ.get("MEYO_GW_OPENAI_BASE",
                                 "https://aigc.sankuai.com/v1/openai/native")
_GW_OPENAI = _GW_OPENAI_BASE + "/chat/completions"


def _load_models():
    """加载模型注册表: 探测缓存 > 用户配置 > 内置默认(合并·配置覆盖默认)。"""
    models = dict(_DEFAULT_MODELS)
    # 用户配置覆盖/新增
    try:
        if os.path.exists(_CONF_PATH):
            user = _json.load(open(_CONF_PATH, encoding="utf-8"))
            if isinstance(user, dict):
                models.update(user.get("models", user))  # 支持 {models:{...}} 或直接 {...}
    except Exception:
        pass
    # 探测缓存(标了哪些真通+能力)覆盖 available 标记
    try:
        if os.path.exists(_PROBE_CACHE):
            probed = _json.load(open(_PROBE_CACHE, encoding="utf-8"))
            for name, info in probed.items():
                if name in models:
                    models[name].update(info)
    except Exception:
        pass
    return models


# MODELS 动态加载(每次 import 读最新配置·用户改了 conf 立即生效)
MODELS = _load_models()


def _auth():
    import sys as _s, os as _o
    _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
    from qa_llm_gateway import _config
    return _config()  # (base_url, auth_token)


def available_models():
    return dict(MODELS)


def probe_models(save: bool = True, ts: float = None) -> dict:
    """自动探测每个注册模型是否真能调通(通用性核心: 别人换key/加新模型后跑一次·
    自动发现哪些可用)。对每个模型发一句极短请求·200且有输出=available=True。
    结果存 ~/.meyo/.models_probed.json 供 _load_models 读。返回探测报告。
    ts: 时间戳(脚本内不调time避免resume问题·由调用方传或用探测耗时)。"""
    report = {}
    probed = {}
    for name, spec in _load_models().items():
        r = ask(name, "Reply with exactly: OK", max_tokens=8, timeout=30)
        ok = r["ok"] and bool(r["out"].strip())
        report[name] = {"available": ok, "elapsed": r["elapsed"],
                        "err": r["err"][:80] if not ok else ""}
        probed[name] = {"available": ok, "family": spec.get("family"),
                        "vision": spec.get("vision", False),
                        "cost": spec.get("cost", 3), "speed": spec.get("speed", 3),
                        "gw": spec.get("gw", name)}
    if save:
        try:
            os.makedirs(os.path.dirname(_PROBE_CACHE), exist_ok=True)
            with open(_PROBE_CACHE, "w", encoding="utf-8") as f:
                _json.dump(probed, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return report


def usable_models(vision_only: bool = False) -> list:
    """返回【探测确认可用】的模型名(编排器用·只在真通的模型里选)。
    没探测过则回全部注册模型(乐观·首次用)。"""
    m = _load_models()
    usable = [n for n, s in m.items() if s.get("available", True)
              and (not vision_only or s.get("vision"))]
    return usable or list(m.keys())


# (2026-08-15·C 额度提醒) 从错误串判是不是【额度/限流】类(区别于普通网络错/超时)。
_QUOTA_MARKERS = ("quota", "insufficient", "429", "rate limit", "rate_limit",
                  "billing", "exceeded", "余额", "额度", "配额", "欠费", "too many requests",
                  "payment", "credit")


def is_quota_error(err: str) -> bool:
    """错误串是不是额度/限流类(该提醒用户换模型/充值·而非当普通失败重试)。"""
    e = str(err or "").lower()
    return any(m in e for m in _QUOTA_MARKERS)


def best_of_n(question: str, *, models: list = None, timeout: float = 90,
              judge_model: str = "claude-opus") -> dict:
    """最强模式·best-of-N:多个异构模型【各自独立解】同一题·再由裁判模型选/综合出最佳答案。
    成本换正确率(用户"最强模式")。适合高价值/难题·finalize 前想要最高把握。
    models 空=自动取 2-3 个可用异构模型(claude+非claude 混搭增独立性)。
    返回 {ok, best_answer, judge_reason, candidates:[{model,answer}], models_used}。fail-safe。"""
    import concurrent.futures as _cf
    try:
        pool = [m for m in (models or usable_models()) if m][:3]
        if not pool:
            return {"ok": False, "error": "no_usable_models"}
        # ① 多模型并行独立解(异构·各答各的)
        cands = []

        def _one(mdl):
            r = ask(mdl, question, timeout=timeout, max_tokens=1500)
            out = r.get("out", "") if isinstance(r, dict) else str(r)
            return {"model": mdl, "answer": out, "ok": bool(r.get("ok")) if isinstance(r, dict) else True}
        try:
            with _cf.ThreadPoolExecutor(max_workers=min(3, len(pool))) as ex:
                for c in ex.map(_one, pool):
                    if c["ok"] and c["answer"]:
                        cands.append(c)
        except Exception:
            for m in pool:  # 并发失败→串行兜底
                c = _one(m)
                if c["ok"] and c["answer"]:
                    cands.append(c)
        if not cands:
            return {"ok": False, "error": "all_models_failed"}
        if len(cands) == 1:
            return {"ok": True, "best_answer": cands[0]["answer"], "judge_reason": "只有1个模型出答案",
                    "candidates": cands, "models_used": pool}
        # ② 裁判模型选/综合最佳(看多个候选·挑最可靠的·或综合)
        _jp = ("你是裁判。下面是几个模型对同一问题的独立回答。选出【事实最可靠、推理最严谨】的那个"
               "作为最终答案;若多个一致则更可信;若有分歧指出并给你判断最对的。严格JSON:"
               '{"best_answer":"最终答案(可综合·但要给确定结论)","reason":"20字为什么"}\n\n'
               f"问题: {question[:800]}\n\n" +
               "\n".join(f"[{c['model']}]: {c['answer'][:800]}" for c in cands))
        _jr = ask(judge_model, _jp, timeout=timeout, max_tokens=1200)
        _jout = _jr.get("out", "") if isinstance(_jr, dict) else str(_jr)
        import re as _re, json as _j
        _m = _re.search(r"\{.*\}", _jout, _re.DOTALL)
        if _m:
            try:
                _d = _j.loads(_m.group(0))
                return {"ok": True, "best_answer": _d.get("best_answer", ""),
                        "judge_reason": _d.get("reason", ""), "candidates": cands, "models_used": pool}
            except Exception:
                pass
        # 裁判解析失败→回退第一个候选
        return {"ok": True, "best_answer": cands[0]["answer"], "judge_reason": "裁判解析失败·取首个",
                "candidates": cands, "models_used": pool}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}


def quota_status(probe: bool = False) -> dict:
    """C · 模型额度/可用性总览(供 agent/UI:额度不够时知道还有哪些模型能用)。
    probe=True 会真探测一遍(慢·准);False 用上次探测缓存(快)。
    返回 {usable:[...], unavailable:[{model,reason}], vision_usable:[...], total_registered, note}。"""
    if probe:
        try:
            probe_models(save=True)
        except Exception:
            pass
    m = _load_models()
    usable, unavail = [], []
    for n, s in m.items():
        if s.get("available", True):
            usable.append(n)
        else:
            unavail.append({"model": n, "reason": str(s.get("error") or s.get("reason") or "探测不通")[:80]})
    vision = [n for n, s in m.items() if s.get("available", True) and s.get("vision")]
    return {
        "usable": usable,
        "unavailable": unavail,
        "vision_usable": vision,
        "total_registered": len(m),
        "note": (f"可用 {len(usable)}/{len(m)} 个模型" +
                 (f"·{len(unavail)} 个不通(可能额度/鉴权)" if unavail else "")),
    }


# v96j(2026-08-11 用户: "凭记忆不对吧") · 真根因追查到这里: ask_expert_model 那层
# 就算把完整证据原文拼进 question 传下来，一路传到这里的 prompt[:4000] 也会把证据
# 砍掉大半——GAIA 里一篇论文关键句常在 3 万字文档的文末，4000 字连开头背景都放不完，
# 更别提带上关键结论段。这才是导致"专家模型回答看似基于证据、实则可能是凭训练记忆
# 认出经典论文"这个疑点始终无法排除的真正瓶颈:不是 agent 没传证据，是这里悄悄把
# 证据截没了。GPT-5/Gemini/Claude 现在网关支持的上下文窗口远大于 4000 字符，改成
# 32000 字符(约几万 token，覆盖绝大多数论文全文+推导过程)，不再是这道题答案对不对
# 的瓶颈；仍保留一个上限而非完全不截断，避免误传超大文件把网关请求体撑爆或计费失控。
_MAX_PROMPT_CHARS = 32000


def ask(model: str, prompt: str, *, system: str = "", image_b64: str = "",
        media_type: str = "image/png", timeout: float = 90, max_tokens: int = 2000) -> dict:
    """统一调用任意注册模型。image_b64 非空 = 视觉调用(该模型须 vision=True)。
    返回 {ok, out, err, elapsed, model}。fail-safe。"""
    t0 = time.time()
    spec = MODELS.get(model)
    if not spec:
        # 2026-08-19 修: agent 常传 CCR 路由名(如 'sankuai,aws.claude-opus-4.7')·
        #   MODELS 注册的是短名(claude-opus/longcat-2.0/…)·直接 get 拿不到→"未注册模型"·
        #   导致 ask_expert_model 交叉验证【全部失败】(实证 Aberdeen run 专家调用全废)。
        #   归一化: 去 'sankuai,' 前缀 + 'aws.' 前缀 + 版本号·映射到注册短名。fail-safe。
        _norm = str(model or "").split(",")[-1].strip()   # 去 'sankuai,' 前缀
        _norm = _norm.replace("aws.", "").strip()          # 去 'aws.' 前缀
        _low = _norm.lower()
        _mapped = None
        if "opus" in _low or "claude-3" in _low or "claude-4" in _low:
            _mapped = "claude-opus"
        elif "sonnet" in _low:
            _mapped = "claude-sonnet"
        elif "haiku" in _low:
            _mapped = "claude-haiku"
        elif "longcat" in _low:
            _mapped = "longcat-2.0"
        elif "gpt-5" in _low or "gpt5" in _low:
            _mapped = "gpt-5"
        elif "gpt-4o" in _low or "4o-mini" in _low:
            _mapped = "gpt-4o-mini"
        elif "gemini" in _low:
            _mapped = "gemini-2.5-pro"
        elif _norm in MODELS:
            _mapped = _norm
        if _mapped and _mapped in MODELS:
            model = _mapped
            spec = MODELS.get(model)
    if not spec:
        return {"ok": False, "out": "", "err": f"未注册模型 {model}", "elapsed": 0, "model": model}
    if image_b64 and not spec["vision"]:
        return {"ok": False, "out": "", "err": f"{model} 不支持vision", "elapsed": 0, "model": model}
    try:
        import requests
        base_url, auth = _auth()
    except Exception as e:
        return {"ok": False, "out": "", "err": f"config err: {e}", "elapsed": 0, "model": model}

    try:
        if spec["family"] == "claude":
            # v113(2026-08-16 · 账号轮询间歇 400) 单发调用太脆: 网关多账号下
            # 同一模型时通时断(实证分钟级窗口翻转·opus4.8一轮3/3通·下轮0/2全灭)。
            # 这里给 claude 家族加同族降级池: 先试注册 gw·HTTP!=200 时按池序换
            # 邻近版本/档位再试(最多 3 个备选·间隔 1s)。判决/反思类调用全走这。
            _pool = [spec["gw"]]
            for _fb in ("sankuai,aws.claude-opus-4.7", "sankuai,aws.claude-sonnet-4.6",
                        "sankuai,aws.claude-sonnet-4.5"):
                if _fb not in _pool:
                    _pool.append(_fb)
            last_exc = None
            for _gi, _gw in enumerate(_pool[:4]):
                if _gi:
                    time.sleep(1.0)
                try:
                    out = _call_claude(base_url, auth, _gw, prompt, system, image_b64, media_type, timeout, max_tokens)
                    return {"ok": True, "out": out, "err": "", "elapsed": round(time.time()-t0, 1),
                            "model": model, "gw_used": _gw, "degraded": _gw != spec["gw"]}
                except Exception as ie:
                    last_exc = ie
            raise last_exc
        else:  # openai(gpt/gemini)
            # v14.3(2026-08-17) · openai-family 加 1 次同族 retry: 实测 gpt-5/4.1/gemini
            # 网关多账号轮询下 first-call 偶现 504/timeout(second-call 通过)。开 1 次
            # 自愈重试·不污染上层(上层只看到 ok=True)。代价: 失败时 90s→180s 上限·
            # 但 normal 路径仍一次命中。
            last_exc = None
            for _try in range(2):
                try:
                    out = _call_openai(auth, spec["gw"], prompt, system,
                                        image_b64, media_type, timeout, max_tokens)
                    return {"ok": True, "out": out, "err": "",
                            "elapsed": round(time.time()-t0, 1),
                            "model": model,
                            "gw_used": spec["gw"],
                            "retried": _try if _try else 0}
                except Exception as ie:
                    last_exc = ie
                    if _try == 0:
                        time.sleep(2.0)  # 同族 retry 前稍等 2s 让网关冷却
            raise last_exc
    except Exception as e:
        return {"ok": False, "out": "", "err": f"{type(e).__name__}: {str(e)[:200]}",
                "elapsed": round(time.time()-t0, 1), "model": model}


def _call_claude(base_url, auth, gw_model, prompt, system, image_b64, media_type, timeout, max_tokens):
    import requests
    content = []
    if image_b64:
        content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}})
    content.append({"type": "text", "text": prompt[:_MAX_PROMPT_CHARS]})
    payload = {"model": gw_model, "max_tokens": max_tokens,
               "messages": [{"role": "user", "content": content}]}
    if system:
        payload["system"] = system
    # v99k:标量timeout→(connect,read)元组·治VLM慢速吐数据永不超时挂起(见see_image.py同源修)
    r = requests.post(f"{base_url}/v1/messages", json=payload,
                      headers={"Content-Type": "application/json", "Authorization": f"Bearer {auth}",
                               "anthropic-version": "2023-06-01"}, timeout=(10, float(timeout)))
    if r.status_code != 200:
        raise RuntimeError(f"claude HTTP {r.status_code}: {r.text[:200]}")
    body = r.json()
    return "".join(b.get("text", "") for b in body.get("content", []))


def _call_openai(auth, gw_model, prompt, system, image_b64, media_type, timeout, max_tokens):
    import requests
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    if image_b64:
        msgs.append({"role": "user", "content": [
            {"type": "text", "text": prompt[:_MAX_PROMPT_CHARS]},
            {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{image_b64}"}},
        ]})
    else:
        msgs.append({"role": "user", "content": prompt[:_MAX_PROMPT_CHARS]})
    payload = {"model": gw_model, "messages": msgs, "max_tokens": max_tokens}
    # v99k:标量timeout→(connect,read)元组·治慢速吐数据永不超时挂起
    r = requests.post(_GW_OPENAI, json=payload,
                      headers={"Content-Type": "application/json", "Authorization": f"Bearer {auth}"},
                      timeout=(10, float(timeout)))
    if r.status_code != 200:
        raise RuntimeError(f"openai HTTP {r.status_code}: {r.text[:200]}")
    body = r.json()
    # 防御性解析(2026-08-12 · 多边形题实证 bug):推理型模型(gemini-2.5-pro)在
    #   max_tokens 太小时·reasoning_tokens 吃光预算·网关返回 HTTP 200 但
    #   choices[0].message=null + finish_reason="length"→旧代码
    #   body["choices"][0]["message"]["content"] 直接 None 下标 TypeError 崩·
    #   ask() 把它当"未知异常"·see_image_expert 的模型轮转也被这个脏错误带偏。
    #   改成:message 为空/无 content 时·抛清晰错误(含 finish_reason)让 ask 返回
    #   ok=False+可读原因·上层据此轮转到别的模型或提示调大 max_tokens。
    _choices = (body or {}).get("choices") or []
    if not _choices:
        raise RuntimeError(f"openai 空 choices: {_json.dumps(body)[:200]}")
    _msg = _choices[0].get("message")
    _fr = _choices[0].get("finish_reason")
    # 2026-09-02 reasoning 兼容(全系列铁律):GLM 系推理模型答案可能落 reasoning_content
    # 而 content 为 None/空·先兜底再判错·与 autoloop/_extract_content/call_model.py 同款。
    if _msg and not (_msg.get("content") or "").strip() and (_msg.get("reasoning_content") or "").strip():
        _msg["content"] = _msg["reasoning_content"]
    if not _msg or _msg.get("content") is None:
        # 推理模型 max_tokens 太小(reasoning 吃光)最常见:finish_reason=length
        raise RuntimeError(
            f"openai 返回空内容(message={_msg}, finish_reason={_fr})"
            + ("·max_tokens 可能太小被 reasoning 吃光" if _fr == "length" else ""))
    return _msg["content"]


if __name__ == "__main__":
    import sys as _sys
    if "--probe" in _sys.argv:
        # 自动探测所有注册模型(别人换key/加新模型后跑这个)
        print("探测所有注册模型…")
        rep = probe_models()
        for name, info in rep.items():
            mark = "✅" if info["available"] else "❌"
            print(f"  {mark} {name}: {info['elapsed']}s {info['err']}")
        print(f"\n可用模型: {usable_models()}")
        print(f"可用视觉模型: {usable_models(vision_only=True)}")
    else:
        for m in ["claude-haiku", "gpt-5", "gemini-2.5-pro"]:
            r = ask(m, "Reply with exactly: OK", max_tokens=10, timeout=30)
            print(f"{m}: ok={r['ok']} out={r['out'][:40]!r} {r['elapsed']}s {r['err'][:60]}")
