"""QA Agent 的统一美团 LLM 网关，避免依赖个人 Claude CLI 登录态。

v58(2026-08-06) · 借鉴 Mars/Hermes(electron_mars resources/hermes-agent) 的
error_classifier.py + retry_utils.py 思路，给网关加结构化重试:
  - 不再是"失败就返回 ok=False 让上游 Step 自己兜底"，而是按错误类型
    (限流/过载/超时 vs 认证/格式错误) 分别决定"退避重试"还是"直接放弃"。
  - jittered backoff：多个并行 Step(比如 step2_parallel_search 4 路并发)
    同时撞到限流时，退避时间加随机抖动，避免大家在同一时刻再次重试
    (thundering herd)，这是 Hermes retry_utils.jittered_backoff 的原样思路。
  - 保留原有返回值 dict 结构 (ok/out/err/elapsed/first_byte_s/truncated)，
    对调用方(qa_agent_main.py 的 200+ 处 _call_llm)完全透明，不用动一行。
"""
from __future__ import annotations  # v13.93.2 · 兼容 cron 环境 /usr/bin/python3 (3.9) · 见下方 int|None 等新式注解
import base64
import glob
import json
import os
import random
import time

import requests

# v113 曾因 opus-4.7 间歇 400 短暂切更高档 Opus·2026-09-02 直连实测该档
#   Bedrock 账号无权限(400)已死·当前唯一活着的 Opus=aws.claude-opus-4.7。降级链见 call()。
# v15.2(2026-08-17 · 用户决策「就用 longcat 2.0 跑」) 默认主模型切 longcat-2.0:
#   同题库(seed=42·wall=480)实测 longcat 全家桶 0%→40%→60% 三连跳·已追平并
#   反超 opus v15.1(2/5)。MEYO_QA_GATEWAY_MODEL 显式指定仍优先生效(fail-safe·
#   随时可回切 opus)。注: light 档(HAIKU_MODEL)随 DEFAULT_MODEL 自适应·
#   v15 工具纪律 prompt 也按主模型条件化(qa_auto_agent.py)·切默认后两者自动就位。
DEFAULT_MODEL = os.environ.get("MEYO_QA_GATEWAY_MODEL", "longcat-2.0")
# 主模型回退链: longcat 挂了回唯一活着的 aws-opus-4.7·不再只在 claude 系内降级
if "longcat" in DEFAULT_MODEL.lower():
    HEAVY_FALLBACK_MODEL = os.environ.get("MEYO_QA_HEAVY_FALLBACK", "sankuai,aws.claude-opus-4.7")
else:
    HEAVY_FALLBACK_MODEL = os.environ.get("MEYO_QA_HEAVY_FALLBACK", "sankuai,aws.claude-opus-4.7")


# G-dist(2026-08-08 · 用户决策「先用我们的也行，同事自己没申请到 key 时先用这个
# 兜底跑通，后续再各自换成自己的；但不能让同事直接看到明文 key」) ·
# 团队共享兜底 key，仅在同事本机既没设环境变量、也没配好自己的 CCR 时才会
# 被用到(优先级最低)。
#
# ⚠️ 凭证【绝不进代码库】(2026-08-19 · mars-data 公开分发改造):
#   之前这里硬编码了一个 base64 兜底 key——但这份代码要 push 到共享/公开仓,
#   任何能 clone 的人都能解码拿到付费凭证。改为【只从部署环境读】:
#     ① 环境变量 MEYO_FALLBACK_KEY(部署方在服务器 env 里设·首选)
#     ② 本地文件 ~/.meyo/fallback_key(gitignore·同事本机放·不进仓)
#   仓库里【没有】任何明文/编码后的 key。部署方把自己的 appid key 放进上面任一处,
#   外部调用方永远不碰 key(只调 HTTP 服务·凭证在服务端)。
#   —— 这就是"如果调用方没配,就用部署方在服务器上放的那个"的正确落地:
#      凭证跟着【部署】走(服务器 env/本地文件),不跟着【代码】走(仓库)。
_FALLBACK_KEY_FILE = os.path.expanduser("~/.meyo/fallback_key")


def _team_shared_fallback_key():
    """兜底凭证·只从部署环境读(env > 本地文件)·仓库内无明文。fail-safe → ''。"""
    k = os.environ.get("MEYO_FALLBACK_KEY", "").strip()
    if k:
        return k
    try:
        with open(_FALLBACK_KEY_FILE, encoding="utf-8") as _f:
            return _f.read().strip()
    except Exception:
        return ""


def _config():
    """网关地址+鉴权 token 的解析优先级(G-dist 2026-08-08 · 分发给同事各自机器改造):

    1. 环境变量 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN(显式覆盖，最高优先级)
    2. CCR 官方标准配置 ~/.claude-code-router/config.json
       (之前是遍历 /var/folders/*/*/T/.../ccr-settings-*.json 这个系统临时目录
       猜配置在哪——那是 CCR 运行时为某次会话生成的临时快照，不是稳定路径，
       换一台机器/临时目录被系统清理后就找不到了。改读 CCR 自己文档化的标准
       配置文件，任何装了 CCR 的机器上路径都一致，才具备可分发性。)
    3. 兜底旧的临时目录扫描(向后兼容，不影响原有能跑的机器)
    4. 团队共享兜底 key(_team_shared_fallback_key()，最低优先级，代码里只存
       base64 编码，不出现明文，见上面函数注释)
       仅当前 3 层都没配到时才用，让新同事第一次跑起来不被网关权限卡住。

    正式使用建议:每个同事尽快换成自己申请的 CCR 配置(自己的
    aigc.sankuai.com 网关 api_key)，前 3 层任意一层配好都会自动优先于共享
    兜底——不需要改代码，装好自己的 CCR 配置就自然切换过去了。
    """
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "http://127.0.0.1:3456")
    auth = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    if auth:
        return base_url, auth

    ccr_config = os.path.expanduser("~/.claude-code-router/config.json")
    try:
        cfg = json.loads(open(ccr_config, encoding="utf-8").read())
        auth = cfg.get("APIKEY") or ""
        providers = cfg.get("Providers") or []
        if providers and not auth:
            auth = providers[0].get("api_key") or ""
        if auth:
            return base_url, auth
    except Exception:
        pass

    # 兜底：旧的临时目录扫描方式(部分老机器/老版本 CCR 可能只有这个)
    for path in glob.glob("/var/folders/*/*/T/claude-code-router/ccr-settings-*.json"):
        try:
            env = json.loads(open(path, encoding="utf-8").read()).get("env", {})
            base_url = env.get("ANTHROPIC_BASE_URL") or base_url
            auth = env.get("ANTHROPIC_AUTH_TOKEN") or auth
            if auth:
                break
        except Exception:
            continue
    if auth:
        return base_url, auth

    # 最后兜底: 团队共享 key(内存里解码，代码/日志都不出现明文)，
    # 让新同事没配好自己的网关前也能先跑通体验
    return base_url, _team_shared_fallback_key()


# v56h(2026-08-05) · 判决/预筛类轻步骤真走 light 档(之前 _call_llm 的 model 参数被吞·
#   全部走 opus-4.7·是 CPU 89% 的根因)。tier="light" → light 模型(快·省CPU)·默认 opus。
# v15.1(2026-08-17) · light 档【自适应主模型】·实测 5+5 BrowseComp:
#   - 主模型=longcat-2.0 → light 也走 longcat-2.0(全家桶同构: v14 0/5 → v15 2/5 ✓)
#   - 主模型=claude系顶配    → light 保持 claude-haiku-4.5(恢复 v14 同构: v15 曾被
#     longcat-light 预筛拖垮 60%→0%·预筛质量直接影响深读集与证据集)
if "longcat" in (DEFAULT_MODEL or "").lower():
    HAIKU_MODEL = os.environ.get("MEYO_QA_HAIKU_MODEL", "longcat-2.0")
else:
    # v134 · 主模型=vertex-opus-4.8 时 light 走 aws.claude-haiku-4.5(实测仍可用·快档)
    HAIKU_MODEL = os.environ.get("MEYO_QA_HAIKU_MODEL", "sankuai,aws.claude-haiku-4.5")

# 2026-08-07 · 中间档 sonnet(qa_auto_agent 引用)。之前缺定义致 import 失败(并发
# session 加了引用没加定义)。补上默认值 + 一个 tier→model 解析入口,保持向后兼容。
SONNET_MODEL = os.environ.get("MEYO_QA_SONNET_MODEL", "sankuai,aws.claude-sonnet-4.6")
# v134 注: aws.claude-sonnet-4.6 实测(2026-08-24)已 0/3 全挂(Bedrock 账号封禁波及)
#   ·仅作环境变量兼容保留·不再入 heavy 降级链(降级链改为 vertex-opus→gpt-5.6-sol)


def resolve_model_name(tier: str = "heavy") -> str:
    """tier → 模型名。light=longcat-2.0(快省) / mid=sonnet(均衡) / heavy=opus(默认最强)。
    未知 tier 一律回默认 opus,不抛异常(fail-safe)。"""
    return {"light": HAIKU_MODEL, "mid": SONNET_MODEL,
            "sonnet": SONNET_MODEL, "heavy": DEFAULT_MODEL}.get(tier, DEFAULT_MODEL)


# ═══════════════════════════════════════════════════════════════════════
# v58 · 精简版错误分类 + 抖动退避重试
# (完整版见 Hermes agent/error_classifier.py 的 8 优先级分类管线，
#  我们只对接一个 CCR 网关、一种协议(Anthropic /v1/messages)，
#  不需要它那套多 provider/多 SDK 异常类型的兼容层，抓核心几类即可)
# ═══════════════════════════════════════════════════════════════════════

MAX_RETRIES = int(os.environ.get("MEYO_QA_GATEWAY_MAX_RETRIES", "3"))


import threading as _threading
_jitter_counter = 0
_jitter_lock = _threading.Lock()


def _jittered_backoff(attempt: int, *, base_delay: float = 3.0, max_delay: float = 30.0,
                       jitter_ratio: float = 0.5) -> float:
    """抖动指数退避。多个并行 Step / 多用户 session 同时撞限流时，加随机抖动
    避免大家同一秒重试(thundering-herd)。

    G-align(2026-08-07) · 补齐 Hermes agent/retry_utils.jittered_backoff 的
    多进程去相关计数器:之前版本去掉了它,只适合单进程内 4-5 路并发;现在
    agent_web.py 要做多用户入口,多个 session(甚至多进程)会同时重试,必须
    有跨调用的去相关 seed,否则粗时钟下 random 仍可能撞同一窗口。seed 用
    time_ns ^ (counter * 黄金比常数),和 hermes 原样一致。
    """
    global _jitter_counter
    with _jitter_lock:
        _jitter_counter += 1
        tick = _jitter_counter
    exponent = max(0, attempt - 1)
    if exponent >= 63 or base_delay <= 0:
        delay = max_delay
    else:
        delay = min(base_delay * (2 ** exponent), max_delay)
    seed = (time.time_ns() ^ (tick * 0x9E3779B9)) & 0xFFFFFFFF
    rng = random.Random(seed)
    jitter = rng.uniform(0, jitter_ratio * delay)
    return delay + jitter


def _classify_gateway_error(status_code: int | None, err_text: str) -> dict:
    """把网关调用失败归类为 {retryable, reason}，决定要不要重试。

    参考 Hermes FailoverReason 的分类骨架，精简到我们唯一网关(CCR→美团
    Bedrock 代理)实际会遇到的几类:
      429/503/502/500 · 限流/过载/网关错误 → retryable
      超时/连接错误                          → retryable
      401/403                              → 不重试(鉴权问题重试没用，
                                               直接暴露给用户去重开 CCR)
      400                                  → 不重试(请求本身有问题，
                                               比如 prompt 格式错误)
      其他未知                              → retryable(保守重试一次)
    """
    msg = (err_text or "").lower()
    if status_code in (429, 500, 502, 503, 529):
        reason = {429: "rate_limit", 503: "overloaded", 529: "overloaded"}.get(status_code, "server_error")
        return {"retryable": True, "reason": reason}
    if status_code in (401, 403):
        return {"retryable": False, "reason": "auth"}
    if status_code == 400:
        # v113(2026-08-16 · opus 间歇 400 事故) 上游 Bedrock 多账号轮询下
        #   "Access to Anthropic models is not allowed for this account" 是
        #   账号级间歇错误——下一分钟换到有权限的账号就通·和请求格式无关。
        #   实证: 守护探测同一通道时通时断·8d46 跑 7 步被打断/384d 起跑即 400。
        #   这类错误换时点重试有效→判 retryable(限常规重试次数)。
        if "not allowed" in msg or "access to anthropic" in msg:
            return {"retryable": True, "reason": "upstream_account_transient"}
        return {"retryable": False, "reason": "format_error"}
    if status_code is not None and status_code >= 500:
        return {"retryable": True, "reason": "server_error"}
    # 无状态码 = requests 抛的异常(超时/连接失败等)
    if any(p in msg for p in ("timeout", "timed out", "connection", "reset by peer", "eof occurred")):
        return {"retryable": True, "reason": "timeout"}
    return {"retryable": True, "reason": "unknown"}


def call(prompt: str, *, timeout: float = 120, system: str = "", max_tokens: int = 16000,
         tier: str = "heavy", max_retries: int | None = None) -> dict:
    """统一网关生成文本。tier='light' 走 longcat-2.0(判决/预筛类·省CPU)·'heavy'(默认)走 opus(写作)。

    v58 · 429/5xx/超时会按分类结果自动退避重试(默认最多 MAX_RETRIES=3 次)，
    401/403/400 判定为不可重试直接返回失败，不浪费重试次数。
    """
    started_at = time.time()
    retries = MAX_RETRIES if max_retries is None else max_retries
    base_url, auth = _config()
    payload = {
        "model": HAIKU_MODEL if tier == "light" else DEFAULT_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        payload["system"] = system

    last_err = ""
    # v113 通道韧性: 多档降级链(账号轮询下不同模型/版本挂在不同账号·权限互补):
    #   heavy: [aws-opus-4.7 → sonnet] light 不降级(旧顶配已死·2026-09-02)。
    #   每档耗尽自身重试后换下一档·带 degraded_model 标记供调用方感知。
    _models = [payload["model"]]
    if tier != "light":
        for _fb in (HEAVY_FALLBACK_MODEL, SONNET_MODEL):
            if _fb and _fb not in _models:
                _models.append(_fb)
    _degraded_to = ""
    for _mi, _model in enumerate(_models):
        if _mi > 0:
            _degraded_to = _model
            payload["model"] = _model
        for attempt in range(1, retries + 2):  # 第一次尝试 + 最多 retries 次重试
            try:
                response = requests.post(
                    f"{base_url}/v1/messages",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {auth}",
                        "anthropic-version": "2023-06-01",
                    },
                    json=payload,
                    timeout=timeout,
                )
                if response.status_code != 200:
                    last_err = f"gateway HTTP {response.status_code}: {response.text[:300]}"
                    classified = _classify_gateway_error(response.status_code, response.text)
                    if classified["retryable"] and attempt <= retries:
                        time.sleep(_jittered_backoff(attempt))
                        continue
                    elapsed = time.time() - started_at
                    # 非可重试错误且还有降级档可换→不直接失败·换下一档再试
                    if _mi + 1 < len(_models):
                        break
                    return {"ok": False, "out": "", "err": last_err, "elapsed": elapsed,
                            "first_byte_s": None, "truncated": False,
                            "retry_reason": classified["reason"], "attempts": attempt,
                            "degraded_model": _degraded_to}

                elapsed = time.time() - started_at
                body = response.json()
                content = body.get("content", [])
                text = "".join(block.get("text", "") for block in content if isinstance(block, dict)).strip()
                # stop_reason == "max_tokens" 说明模型是被截断的，而不是自然收尾；
                # 调用方需要据此决定是否续写，禁止把半截文本当完整答案静默使用。
                truncated = body.get("stop_reason") == "max_tokens"
                out = {"ok": bool(text), "out": text, "err": "", "elapsed": elapsed,
                       "first_byte_s": elapsed, "truncated": truncated, "attempts": attempt}
                if _degraded_to:
                    out["degraded_model"] = _degraded_to
                return out
            except Exception as exc:
                last_err = f"gateway {type(exc).__name__}: {exc}"
                classified = _classify_gateway_error(None, str(exc))
                if classified["retryable"] and attempt <= retries:
                    time.sleep(_jittered_backoff(attempt))
                    continue
                if _mi + 1 < len(_models):
                    break
                return {"ok": False, "out": "", "err": last_err, "elapsed": time.time() - started_at,
                        "first_byte_s": None, "truncated": False,
                        "retry_reason": classified["reason"], "attempts": attempt,
                        "degraded_model": _degraded_to}

    # 理论上不会走到这里(循环内每条路径都 return)，兜底保留旧行为
    return {"ok": False, "out": "", "err": last_err, "elapsed": time.time() - started_at,
            "first_byte_s": None, "truncated": False, "attempts": retries + 1,
            "degraded_model": _degraded_to}


def text(model: str, prompt: str, timeout: float = 120, system: str = "") -> str:
    """保留 model 参数兼容旧调用；实际模型由统一网关配置决定。"""
    return call(prompt, timeout=timeout, system=system)["out"]


def probe_channel(model: str = "", timeout: float = 15) -> bool:
    """v113b · 裸探测(单发·无重试无降级链): 只测这个模型当前通不通。

    call() 自带三档降级链会自愈——用它探测会误报 ok(降级成功≠原模型活)。
    run 级路由决策(如 heavy 探测失败降 k3)必须用这个: 只信原模型单发结果。
    """
    try:
        base_url, auth = _config()
        r = requests.post(
            f"{base_url}/v1/messages",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {auth}",
                     "anthropic-version": "2023-06-01"},
            json={"model": model or DEFAULT_MODEL, "max_tokens": 8,
                  "messages": [{"role": "user", "content": "ok"}]},
            timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════
# v13.92 · vision() · 多模态看图 (OP-4)
#
# 直接构造 Anthropic /v1/messages 的 image content block,不破坏现有 text()
# 调用方。fail-safe: 网关不支持/格式错 → 返回 None,由 see_image 工具 fallback
# 到 OCR + 文本解读路径。
# ═══════════════════════════════════════════════════════════════
def vision(image_b64: str, question: str, *, media_type: str = "image/jpeg",
           timeout: float = 60, max_tokens: int = 800,
           system: str = "你是看图助手。直接、精确回答问题。") -> dict | None:
    """看图 + 回答。返回与 call() 同结构的 dict(ok/out/err/elapsed/...)
    或 None(网关不可用 / 不支持 vision)。"""
    try:
        import requests
        base_url, auth = _config()
    except Exception:
        return None
    payload = {
        "model": DEFAULT_MODEL,
        "max_tokens": max_tokens,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": media_type,
                            "data": image_b64}},
                {"type": "text", "text": (question or "").strip()[:1500]},
            ],
        }],
    }
    if system:
        payload["system"] = system
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {auth}",
        "anthropic-version": "2023-06-01",
    }
    started_at = time.time()
    try:
        r = requests.post(f"{base_url}/v1/messages", json=payload,
                          headers=headers, timeout=timeout)
    except Exception as exc:
        return {"ok": False, "out": "", "err": f"vision {type(exc).__name__}: {exc}",
                "elapsed": time.time() - started_at, "first_byte_s": None,
                "truncated": False, "attempts": 1}
    if r.status_code != 200:
        return {"ok": False, "out": "",
                "err": f"vision HTTP {r.status_code}: {r.text[:300]}",
                "elapsed": time.time() - started_at, "first_byte_s": None,
                "truncated": False, "attempts": 1}
    try:
        body = r.json()
        content = body.get("content", [])
        text_out = "".join(b.get("text", "") for b in content
                           if isinstance(b, dict)).strip()
        return {"ok": bool(text_out), "out": text_out, "err": "",
                "elapsed": time.time() - started_at, "first_byte_s": None,
                "truncated": body.get("stop_reason") == "max_tokens", "attempts": 1}
    except Exception as exc:
        return {"ok": False, "out": "",
                "err": f"vision parse {type(exc).__name__}: {exc}",
                "elapsed": time.time() - started_at, "first_byte_s": None,
                "truncated": False, "attempts": 1}


# ═══════════════════════════════════════════════════════════════════════
# v57 · Auto Agent 模式 · 原生 tool-calling 走官方 deepagents 库
# (pip/uv: deepagents + langchain-anthropic，装在 qa_system/.venv_autoagent)
# 而不是自己再手写一套 tool_use/tool_result 协议解析。
#
# 已验证: qa_llm_gateway 本身就是 Anthropic /v1/messages 的直通代理
# (CCR)，langchain_anthropic.ChatAnthropic 只需把 base_url 指到网关、
# 把鉴权 token 塞进 default_headers["Authorization"]，就能被
# deepagents.create_deep_agent() 直接当 model 用，完整 tool-calling
# loop（含多轮工具调用、sub-agent 委派）全部由官方库处理。
# 见 qa_auto_agent.py::build_gateway_chat_model()。
# ═══════════════════════════════════════════════════════════════════════
