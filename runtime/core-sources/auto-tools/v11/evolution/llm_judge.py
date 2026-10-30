#!/usr/bin/env python3
"""
v13.66 · llm_judge · 独立上下文校验 LLM
────────────────────────────────────────────────────────────────
用户 2026-07-13 元洞察:
「你不需要用规则·而是用 llm 去理解·不如多 llm 去思考?一个是没有上下文的专门
校验?同时完成思考我的指令?」

── 核心 ──
主 LLM 有全部对话上下文 · 已经**倾向"我做完了"** (沉没成本 bias)
让同一个 LLM 自审 = 自证 · 没用
需要:**干净上下文的独立 LLM** · 只喂当前 turn · 像审稿人不是作者

── 3 层判断 (可扩) ──
Q1 · 回复是否真答了用户问的核心?
Q2 · 有没有漏点?漏了什么?
Q3 · 有没有过度自信/编造/糊弄?

── 用法 ──
judge(user_msg, my_reply)         · 并行跑 3 questions · 返回 3 verdict
judge_correction(user_msg)         · 只判 "这是纠正吗" · 替代规则派
judge_recent(last_n=5)             · cron 每 10min 扫最近 5 回复补校验

── 数据落盘 ──
v11/.llm_judge.jsonl · 每 verdict 一行 · verdict=pass/fail/warn + reason + missed

── 后端 ──
claude CLI · haiku (30s+ 太慢就换 --model haiku-4-5) · 独立进程 = 干净上下文
"""
import os, sys, json, re, time, subprocess, concurrent.futures
from datetime import datetime

# v13.88.9g · 2026-07-31 · 统一 spawn 咽喉 · 防雪崩核心
# 主进程验证发现 llm_judge 是主 spawn 源却绕过咽喉 → load 反复爆 100+ 根因。
# 所有直接 spawn claude CLI 的地方都改走 _spawn_gate (并发上限+fail-safe)。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from _spawn_gate import spawn_claude_cli as _spawn_claude_cli, spawn_popen as _spawn_popen
except Exception:
    # fail-safe 铁律:连闸门都 import 不了 → 一律不 spawn (返回像失败的对象)
    class _ImportBlocked:
        def __init__(self, *a, **k):
            self.returncode = 199; self.stdout = ""; self.blocked = True
            self.stderr = "[spawn_gate import failed] 不 spawn (fail-safe)"
    def _spawn_claude_cli(args, timeout=60, **kw):
        return _ImportBlocked()
    def _spawn_popen(args, **kw):
        return None


# v13.88.79 · 静默监控统一接入 · 用户 07-28「所有静默监控都到齐了吗」
def _alert(severity: str, msg: str):
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
        from alert_bus import notify as _n
        _n(_o.path.basename(__file__).replace(".py",""), severity, msg)
    except Exception:
        pass


BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
V11 = os.path.join(BASE, "v11")
JUDGE_LOG = os.path.join(V11, ".llm_judge.jsonl")

CLAUDE_CLI = os.environ.get("MEYO_CLAUDE_CLI", "claude")
JUDGE_MODEL = os.environ.get("MEYO_JUDGE_MODEL", "haiku")
JUDGE_TIMEOUT = int(os.environ.get("MEYO_JUDGE_TIMEOUT", "60"))

# v13.88.58 · 2026-07-28 · 用户抓「haiku CLI 16.4s > 15s · 为啥用 CLI 不用 API」
# 有 ANTHROPIC_API_KEY 就走 API · 3-5s vs CLI 12-16s
_ANTHROPIC_CLIENT = None
def _get_anthropic():
    """v13.88.65 · 2026-07-28 · 认 ANTHROPIC_AUTH_TOKEN + BASE_URL (proxy)
    用户走本地 claude-code proxy · 之前只认 API_KEY 所以永远退 CLI
    """
    global _ANTHROPIC_CLIENT
    if _ANTHROPIC_CLIENT is not None:
        return _ANTHROPIC_CLIENT
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    if not api_key and not auth_token:
        return None
    try:
        import anthropic
        kwargs = {}
        if api_key:
            kwargs["api_key"] = api_key
        elif auth_token:
            # AUTH_TOKEN 走 auth_token 头 · 通常配 BASE_URL proxy 用
            kwargs["auth_token"] = auth_token
        if base_url:
            kwargs["base_url"] = base_url
        _ANTHROPIC_CLIENT = anthropic.Anthropic(**kwargs)
        return _ANTHROPIC_CLIENT
    except Exception:
        return None


_MODEL_ALIAS_API = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-5-20250929",
    "opus":   "claude-opus-4-1-20250805",
}


def _judge_via_api(system: str, prompt: str, model_short: str):
    """API 直调 · 返回 raw text 或 None (无 client / 挂)"""
    client = _get_anthropic()
    if not client:
        return None
    model = _MODEL_ALIAS_API.get(model_short, _MODEL_ALIAS_API["haiku"])
    import time as _t
    _t0 = _t.time()
    _out, _ok, _err = "", True, ""
    try:
        # v13.88.65 · max_tokens 800→300 · 分类 JSON 只需 ~100 tokens
        # 用户抓「6.5s/句偏慢·haiku 应 <2s」
        resp = client.messages.create(
            model=model, max_tokens=300, system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        _out = resp.content[0].text if resp.content else ""
        return _out
    except Exception as _e:
        _ok, _err = False, f"{type(_e).__name__}: {_e}"
        return None
    finally:
        # v13.90 · L2-4 · 环9 接线 (根因: llm_io_monitor 零生产调用方·空壳)
        # _judge_via_api 是全库 LLM 调用主路径·每次调用落 IO 踪迹
        # (prompt/response 长度·耗时·成败)·fail-open 不阻塞判定。
        try:
            from llm_io_monitor import record_call as _rc
            _rc(caller=f"llm_judge:{model_short}", model=model,
                in_len=len(system or "") + len(prompt or ""),
                out_len=len(_out or ""), elapsed_s=_t.time() - _t0,
                ok=_ok, err=_err[:150])
        except Exception:
            pass

# v13.71 · 用户 2026-07-14 抓「为啥不用 opus 大脑」
# 真跑对比 haiku/sonnet/opus:
#   haiku 抓表面 · sonnet 抓核心 (钩子名不副实) · opus 抓工程手感 (手机端劝退)
#   3 模型时长都 ~11-12s · 没显著差异
# 分层:关键决策 (发帖 gate/元规则拆解) 用 opus/sonnet · 常规 (turn_hook 每次)用 haiku
# haiku 判 warn → 自动升 opus 复审 · 不放走深层漏帖
TIER_MODEL = {
    "routine": "haiku",       # turn_hook 每次调 · 每天 200 次 · 快
    "critical": "sonnet",     # publisher gate · scan_G · 决策关键
    "deep": "opus",           # 元规则拆 · correction 分类 · haiku warn 复审
}
ESCALATE_ON_WARN = os.environ.get("MEYO_JUDGE_ESCALATE", "1") == "1"  # haiku warn 自动升

# 3 类默认 question · 独立跑
DEFAULT_QUESTIONS = [
    ("core_answered",
     "助手回复是否真正回答了用户问的核心?· 只看这一轮·不看历史"),
    ("missing_points",
     "助手回复有没有明显漏点?列出漏的3件事(如果有)"),
    ("overconfidence",
     "助手回复有没有过度自信/编造数字/糊弄?列出可疑处"),
]

# 特殊 question · 判断是不是纠正 (替代规则派)
# v13.88.65 · 2026-07-28 用户抓「LLM 判 CHAT 但 regex 命中·真元规则解在 prompt」
# · 加追问识别规则 · 加 few-shot · 加不确定归 CORRECTION 偏向
CORRECTION_QUESTION = (
    "correction_class",
    """你要判定这条用户对 AI 助手说的话属于哪一类。这不是判"消息类型",而是判"AI 该不该 process/进化"。

【5 类定义 · 从严】

- CORRECTION: 抓包/纠正/追问/审视。包括:
  · 明确说错了/漏了/没做/方法不对
  · 追问式验证:「XX了吗」「打通了吗」「测了吗」「跑了吗」「真的做了吗」
  · 审视式追溯:「是啥来着」「之前说的呢」「上次那个」
  · 不满/催促:「你怎么还没」「就这样?」「够了吗」「够深吗」
  · 元批评:「你的方法有问题」「你没抓到点」

- REQUEST: 让 AI 做具体某件事。包括:
  · 命令:「跑一下」「测一下」「改一下」「加个 XX」「删掉 XX」
  · 疑问式命令:「能不能 X」「可以 Y 吗」「帮我 Z」
  · 走一遍/走一下/跑一遍类元指令

- META: 元层思想/系统升级/元规则。包括:
  · 「以后都 XX」「元规则是 XX」「LLM 主推」「不要 XX 派」
  · 判断风格/方法论/流程/策略级别的指令

- QUESTION: 纯技术/知识问询·答一下就完·不涉及 AI 上一轮做的事
  · 「Python 的 X 语法是什么」「A 和 B 区别」

- CHAT: 纯闲聊/纯确认/纯情感·不需要 AI 做任何事
  · 「好的」「谢谢」「辛苦了」「天气不错」「哈哈」

【元规则】
1. 判定 AI 该不该 process,不是判语法类型。追问 = CORRECTION,不是 QUESTION。
2. 只要用户在**验证 AI 上一轮做了没/做对了没** → CORRECTION,别归 CHAT/QUESTION
3. 只要用户在**指派新任务或走一遍某流程** → REQUEST
4. 不确定时归 CORRECTION (宁误报不漏报·漏抓代价更大)
5. **只有纯感谢/纯情感/纯确认/纯闲聊** 才是 CHAT

【典型 case · 之前误判的】
- 「测了吗」→ CORRECTION (追问验证·不是 QUESTION)
- 「真的打通了吗」→ CORRECTION (审视式验证)
- 「是啥来着」→ CORRECTION (追溯式验证)
- 「跑一下 6 环真回归」→ REQUEST (指派任务)
- 「能不能再检查一下?」→ REQUEST (疑问式命令·不是 QUESTION)
- 「LLM 派主推·别打补丁」→ META (元规则·不是 CHAT)
- 「你没做啊」→ CORRECTION
- 「今天天气不错」→ CHAT
- 「谢谢辛苦了」→ CHAT

【v13.88.77 · 07-28 selfcheck_learn 从 gate 事件补 · 均 CORRECTION·should_process=True】
- gate 抓 AI 常见术语误用 (feedback-no-ai-jargon 规则) → CORRECTION
- gate 抓版本代号误用 (memory 里说旧版·应说新版) → CORRECTION
- gate 15min 内同 topic 重复触发 → CORRECTION (漏抓信号)

只返回 JSON:
{"class": "CORRECTION|REQUEST|QUESTION|META|CHAT",
 "confidence": 0-100,
 "why": "一句话·点出用户意图关键词",
 "should_evolve": true 除非 CHAT/pure QUESTION}"""
)


def _load_forged_audit_prompts() -> str:
    """v13.82 · 加载 forged/audit_prompt/*/prompt_addition.txt · 反哺 judge"""
    try:
        forged_dir = os.path.join(V11, "forged", "audit_prompt")
        if not os.path.isdir(forged_dir):
            return ""
        additions = []
        for sub in os.listdir(forged_dir):
            snippet = os.path.join(forged_dir, sub, "prompt_addition.txt")
            if os.path.exists(snippet):
                try:
                    with open(snippet) as f:
                        additions.append(f.read().strip())
                except Exception:
                    pass
        if additions:
            return "\n\n## 已固化的历史判问 (meta_audit 建议加的)\n" + "\n".join(additions)
    except Exception:
        pass
    return ""


def _judge_one(user_msg: str, my_reply: str, question_key: str,
                question: str, model: str = None) -> dict:
    """跑一次独立 LLM judge · 干净 subprocess = 干净上下文
    只喂:user_msg 当前 · my_reply 当前 · question 一次
    v13.82: 加载 forged/audit_prompt/*/ 反哺 judge system
    """
    system = ("你是独立审稿人 · 只看你被喂的这一段对话 · 不假设任何历史 · "
              "客观判断 · 不奉承 · 返回 JSON")
    system += _load_forged_audit_prompts()
    prompt = f"""用户消息:
{user_msg[:3000]}

──

助手回复:
{my_reply[:3000] if my_reply else '(暂无回复·此次只判用户消息)'}

──

评估问题: {question}

只返回 JSON, 格式:
{{"verdict": "pass|fail|warn",
  "reason": "一句话为什么",
  "missed": ["漏的1", "漏的2"],
  "confidence": 0-100}}"""

    # v13.88.65 · 2026-07-28 用户抓「6.5s/句偏慢·haiku 应 <2s」
    # 优先级: API (若装) → daemon (常驻·3-5s) → CLI subprocess (12-16s)
    # 之前只在 judge() 里走 daemon · _judge_one/_judge_correction 都直接 CLI 冷启
    model_short = model or JUDGE_MODEL
    api_raw = _judge_via_api(system, prompt, model_short)
    if api_raw is not None:
        raw = api_raw.strip()
        m = re.search(r"\{[\s\S]+?\}", raw)
        if m:
            try:
                data = json.loads(m.group(0))
                data["question_key"] = question_key
                data["ok"] = True
                data["_backend"] = "api"
                return data
            except json.JSONDecodeError:
                pass
        return {"question_key": question_key, "ok": False,
                "err": "no_json_api", "raw": raw[:500], "_backend": "api"}

    # API 挂 → 试 daemon (常驻进程·3-5s vs subprocess 12-16s)
    daemon_raw = _judge_via_daemon(system, prompt, model_short)
    if daemon_raw is not None:
        raw = daemon_raw.strip()
        m = re.search(r"\{[\s\S]+?\}", raw)
        if m:
            try:
                data = json.loads(m.group(0))
                data["question_key"] = question_key
                data["ok"] = True
                data["_backend"] = "daemon"
                return data
            except json.JSONDecodeError:
                pass
        # daemon 有 raw 但没 JSON · 继续 fallback subprocess
    try:
        # v13.88.9g · 过 spawn 咽喉 (并发上限+fail-safe)·被拦 returncode=199
        r = _spawn_claude_cli(
            [CLAUDE_CLI, "--print",
             "--model", model_short,
             "--append-system-prompt", system,
             prompt],
            timeout=JUDGE_TIMEOUT,
        )
        raw = (r.stdout or "").strip()
        if r.returncode != 0:
            return {"question_key": question_key, "ok": False,
                    "err": (r.stderr or "")[:200], "raw": raw[:500]}
        # 抽 JSON
        m = re.search(r"\{[\s\S]+?\}", raw)
        if m:
            try:
                data = json.loads(m.group(0))
                data["question_key"] = question_key
                data["ok"] = True
                return data
            except json.JSONDecodeError:
                pass
        return {"question_key": question_key, "ok": False,
                "err": "no_json", "raw": raw[:500]}
    except subprocess.TimeoutExpired:
        return {"question_key": question_key, "ok": False, "err": "timeout"}
    except Exception as e:
        return {"question_key": question_key, "ok": False, "err": str(e)[:200]}


# v13.88.58 · 常驻 claude daemon · 复用用户 login 凭证 · 免 subprocess 冷启
# 用户 07-28 明说「复用现在跟你对话的 API」
# claude CLI --input-format=stream-json --output-format=stream-json = 持久流式协议
_DAEMONS = {}  # model → {"proc": Popen, "lock": Lock, "ts_last": float}

def _get_daemon(model_short: str):
    """启一个 claude CLI 常驻 · stream-json 协议 · 多 request 复用一进程
    v13.88.58 · 用户 07-28 追问后 · 确认 daemon 不写 conversation jsonl
      (`claude --print --input-format=stream-json` 是 stateless · 只 stdio)
    仍用隔离 cwd 保险 · 万一以后行为变了不污染主 session
    """
    import threading
    d = _DAEMONS.get(model_short)
    if d and d["proc"].poll() is None:
        return d
    isolated_cwd = "/tmp/llm_judge_workdir"
    try:
        os.makedirs(isolated_cwd, exist_ok=True)
    except Exception:
        isolated_cwd = None
    try:
        # v13.88.9g · daemon 长驻进程·过 spawn_popen 咽喉 (满/急停/异常返 None → 不启)
        proc = _spawn_popen(
            [CLAUDE_CLI, "--print",
             "--model", model_short,
             "--input-format", "stream-json",
             "--output-format", "stream-json",
             "--verbose"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
            cwd=isolated_cwd,
        )
        if proc is None:
            # 咽喉拦下 (并发满/急停/异常)·本次不用 daemon·上层自动 fallback
            return None
        _DAEMONS[model_short] = {
            "proc": proc, "lock": threading.Lock(), "ts_last": time.time(),
        }
        return _DAEMONS[model_short]
    except Exception:
        return None


def _judge_via_daemon(system: str, prompt: str, model_short: str):
    """走 daemon · 一次请求 · 复用进程 · 3-5s vs subprocess 12-16s
    stream-json 协议:
      入: 每行 JSON {"type":"user","message":{"role":"user","content":"..."}}
      出: 多个 JSON events · 找 {"type":"assistant"} 里的 message content
    stream-json 每 request 会自然开新对话 (claude cli 单进程支持多 turn 时·
    我们把每次 judge 当独立 turn·靠 --append-system-prompt 塞独立 system)
    """
    d = _get_daemon(model_short)
    if not d or d["proc"].poll() is not None:
        return None
    # 拼一次性 user 消息 (含 system 前置)
    full_prompt = f"[System]\n{system}\n\n[User]\n{prompt}"
    req = {"type": "user",
           "message": {"role": "user", "content": full_prompt}}
    try:
        with d["lock"]:
            proc = d["proc"]
            proc.stdin.write(json.dumps(req) + "\n")
            proc.stdin.flush()
            # 读到 result event · 或超时
            deadline = time.time() + JUDGE_TIMEOUT + 10
            text_parts = []
            while time.time() < deadline:
                line = proc.stdout.readline()
                if not line:
                    break
                try:
                    evt = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                # 找 assistant text
                if evt.get("type") == "assistant":
                    msg = evt.get("message", {})
                    for c in msg.get("content", []):
                        if isinstance(c, dict) and c.get("type") == "text":
                            text_parts.append(c.get("text", ""))
                if evt.get("type") == "result":
                    d["ts_last"] = time.time()
                    return "".join(text_parts) or (evt.get("result") or "")
            return None  # 超时
    except (BrokenPipeError, Exception):
        # daemon 挂了 · 下次自动重启
        try:
            d["proc"].kill()
        except Exception:
            pass
        _DAEMONS.pop(model_short, None)
        return None


def _judge_batch_via_cli(user_msg: str, my_reply: str, qs: list, model_short: str):
    """v13.88.58 · 一次 CLI 调用批判 N 个问题 · 省 (N-1) 次冷启
    优先走 daemon (常驻 · 3-5s) · 挂了 fallback subprocess
    """
    q_block = "\n".join(
        f"Q_{k}: {q}" for k, q in qs
    )
    system = (
        "你是独立审稿人 · 只看这一轮 · 不看历史。\n"
        "对下面 N 个问题一次性判断 · 每题返 1 行 JSON:\n"
        '{"question_key": "...", "verdict": "pass|fail|warn", '
        '"reason": "一句话", "missed": ["漏1"], "confidence": 0-100}\n'
        "N 行 JSON · 不要多余文字。"
    )
    prompt = f"""用户消息:
{user_msg[:3000]}

──

助手回复:
{(my_reply or '(暂无回复)')[:3000]}

──

请对以下 {len(qs)} 个问题各返 1 行 JSON:
{q_block}
"""
    # v13.88.58 · 先试 daemon (省冷启) · 挂了走 subprocess
    raw = _judge_via_daemon(system, prompt, model_short)
    if raw is None:
        try:
            # v13.88.9g · 过 spawn 咽喉 (并发上限+fail-safe)
            r = _spawn_claude_cli(
                [CLAUDE_CLI, "--print",
                 "--model", model_short,
                 "--append-system-prompt", system,
                 prompt],
                timeout=JUDGE_TIMEOUT + 20,
            )
            raw = (r.stdout or "").strip()
            if r.returncode != 0:
                return None
        except (subprocess.TimeoutExpired, Exception):
            return None
    # 抽 N 个 JSON 对象
    results = []
    for m in re.finditer(r'\{[^{}]+?\}', raw):
        try:
            d = json.loads(m.group(0))
            if "question_key" in d and "verdict" in d:
                d["ok"] = True
                d["_backend"] = "cli_daemon" if raw else "cli_batch"
                results.append(d)
        except json.JSONDecodeError:
            continue
    if len(results) == len(qs):
        return results
    return None


def judge(user_msg: str, my_reply: str = "",
          questions: list = None, parallel: bool = True,
          tier: str = "routine", model: str = None,
          escalate: bool = None) -> dict:
    """并行 judge · merged verdict
    v13.88.58: 加 CLI 批处理路径 · 一次调用判 N 问 · 省 (N-1) 冷启
    v13.71 · 用户 2026-07-14 抓「为啥不 opus」
    tier:
      routine  = haiku    (turn_hook 每次触发·每天 200 次·快)
      critical = sonnet   (publisher gate/scan_G/决策关键)
      deep     = opus     (元规则拆/correction 分类/复审)
    model 显式传参 > tier 映射 > JUDGE_MODEL 环境变量
    escalate:True 时 haiku warn 自动升 opus 复审
    """
    _model = model or TIER_MODEL.get(tier, JUDGE_MODEL)
    _escalate = ESCALATE_ON_WARN if escalate is None else escalate
    qs = questions or DEFAULT_QUESTIONS
    t0 = time.time()

    # v13.88.58 · CLI 无 KEY 时·批处理路径 (一次调用判 N 问·省冷启)
    # 有 KEY 交给下面 parallel · _judge_one 内部走 API
    _batched_results = None
    if not _get_anthropic() and len(qs) >= 2 and not os.environ.get("MEYO_NO_BATCH"):
        _batched_results = _judge_batch_via_cli(user_msg, my_reply, qs, _model)

    results = []
    if _batched_results is not None:
        results = _batched_results
    elif parallel and len(qs) > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(qs), 3)) as ex:
            futs = {ex.submit(_judge_one, user_msg, my_reply, k, q, _model): k
                    for k, q in qs}
            for f in concurrent.futures.as_completed(futs, timeout=JUDGE_TIMEOUT + 10):
                try:
                    results.append(f.result())
                except Exception as e:
                    results.append({"question_key": futs[f], "ok": False,
                                     "err": str(e)[:100]})
    else:
        for k, q in qs:
            results.append(_judge_one(user_msg, my_reply, k, q, _model))

    # 汇总
    verdicts = [r.get("verdict") for r in results if r.get("ok")]
    n_fail = verdicts.count("fail")
    n_warn = verdicts.count("warn")
    n_pass = verdicts.count("pass")
    # v13.88.58 · 用户 07-28 抓 · 3 个 _judge_one 全 err 时不能返 pass
    # 至少半数 ok 才算有效判断 · 否则 overall=error
    n_ok = sum(1 for r in results if r.get("ok"))
    if len(results) > 0 and n_ok < len(results) / 2:
        overall = "error"  # 静默失败守卫
    else:
        overall = "fail" if n_fail > 0 else ("warn" if n_warn > 0 else "pass")

    escalated = False
    escalated_results = None
    # v13.71 · haiku warn 自动升 opus 复审 (但不 escalate 已经 opus 的)
    if _escalate and overall == "warn" and _model in ("haiku", "sonnet"):
        upgrade_model = "opus" if _model == "haiku" else "opus"
        try:
            up_results = []
            if parallel and len(qs) > 1:
                with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(qs), 3)) as ex:
                    futs = {ex.submit(_judge_one, user_msg, my_reply, k, q, upgrade_model): k
                            for k, q in qs}
                    for f in concurrent.futures.as_completed(futs, timeout=JUDGE_TIMEOUT + 30):
                        try:
                            up_results.append(f.result())
                        except Exception as e:
                            up_results.append({"question_key": futs[f], "ok": False,
                                                 "err": str(e)[:100]})
            else:
                for k, q in qs:
                    up_results.append(_judge_one(user_msg, my_reply, k, q, upgrade_model))
            up_verdicts = [r.get("verdict") for r in up_results if r.get("ok")]
            up_n_fail = up_verdicts.count("fail")
            up_n_warn = up_verdicts.count("warn")
            up_n_pass = up_verdicts.count("pass")
            up_overall = "fail" if up_n_fail > 0 else ("warn" if up_n_warn > 0 else "pass")
            # opus 复审覆盖 · 但保留 haiku 原判
            escalated = True
            escalated_results = {
                "upgrade_model": upgrade_model,
                "original_verdict": overall,
                "original_n_pass": n_pass,
                "original_n_warn": n_warn,
                "original_n_fail": n_fail,
                "results": up_results,
            }
            # 用 opus verdict 覆盖 · 更可信
            overall = up_overall
            n_fail = up_n_fail
            n_warn = up_n_warn
            n_pass = up_n_pass
            results = up_results
        except Exception as e:
            escalated_results = {"err": str(e)[:200]}

    record = {
        "ts": datetime.now().isoformat(),
        "user_msg_preview": user_msg[:200],
        "my_reply_preview": (my_reply or "")[:200],
        "elapsed_s": round(time.time() - t0, 1),
        "tier": tier,
        "model_used": escalated_results.get("upgrade_model") if escalated else _model,
        "overall": overall,
        "n_pass": n_pass, "n_warn": n_warn, "n_fail": n_fail,
        "escalated": escalated,
        "escalation": escalated_results,
        "results": results,
    }
    # 落盘 · 保持原有 .llm_judge.jsonl 向后兼容
    with open(JUDGE_LOG, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # v13.74 · 同步落统一 audit_events.jsonl (fail 触发 correction)
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from audit_core import append_event, push_correction
        append_event({
            "agent": "llm_judge",
            "tier": tier,
            "verdict": overall,
            "reason": record.get("results", [{}])[0].get("reason", "")[:200] if record.get("results") else "",
            "model": record["model_used"],
            "elapsed_s": record["elapsed_s"],
            "meta": {
                "n_pass": n_pass, "n_warn": n_warn, "n_fail": n_fail,
                "escalated": escalated,
                "user_msg_preview": user_msg[:100],
            },
        })
        if overall == "fail":
            failed_reasons = "; ".join([
                (r.get("reason") or "")[:80] for r in results
                if r.get("verdict") == "fail"
            ][:3])
            push_correction(
                source="llm_judge",
                text=f"[llm_judge] user_msg={user_msg[:100]} · {failed_reasons}",
                markers=["llm_judge", "content_fail"],
                severity="medium",
            )
    except Exception:
        pass
    return record


def judge_correction(user_msg: str, tier: str = "deep",
                      model: str = None) -> dict:
    """单条 LLM 判断:这消息是不是纠正? · 替代规则派 marker 匹配
    v13.66.1 · 修:LLM 有时把 class 放 reason 而非顶层字段·抽 reason 里的类
    v13.71 · 用户 2026-07-14 抓「为啥不 opus」·元规则拆是关键决策 · tier=deep=opus
    """
    _m = model or TIER_MODEL.get(tier, JUDGE_MODEL)
    r = _judge_one(user_msg, "", CORRECTION_QUESTION[0], CORRECTION_QUESTION[1], model=_m)
    if r.get("ok"):
        # 优先直接字段 · 兜底从 reason 抽
        cls = r.get("class")
        if not cls:
            reason = (r.get("reason") or r.get("why") or "").upper()
            for c in ("CORRECTION", "META", "REQUEST", "QUESTION", "CHAT"):
                if c in reason:
                    cls = c; break
        cls = cls or "CHAT"
        # should_evolve 兜底
        se = r.get("should_evolve")
        if se is None:
            se = cls in ("CORRECTION", "META")
        return {
            "ok": True,
            "class": cls,
            "confidence": r.get("confidence", 50),
            "why": r.get("why") or r.get("reason", ""),
            "should_evolve": se,
            "raw": r,
        }
    return {"ok": False, "err": r.get("err", "?"), "raw": r}


def judge_recent(last_n: int = 5) -> dict:
    """cron 每 10min · 扫最近 last_n 条 what_i_did 里的 user↔reply · 补 judge
    只 judge 还没 judge 过的"""
    what_did = os.path.join(V11, ".what_i_did.jsonl")
    if not os.path.exists(what_did):
        return {"n_judged": 0, "reason": "no what_i_did"}
    with open(what_did) as f:
        lines = f.readlines()
    # 已 judge 过的 ts
    already = set()
    if os.path.exists(JUDGE_LOG):
        with open(JUDGE_LOG) as f:
            for line in f:
                try:
                    already.add(json.loads(line).get("user_msg_preview", "")[:100])
                except: pass
    # 找 user_reply pair
    to_judge = []
    for line in reversed(lines[-200:]):  # 只看最近 200 条
        try:
            r = json.loads(line)
            action = r.get("action", "")
            if action != "user_turn":  # 或其他标记 · 兜底所有含 user_msg 的
                continue
            payload = r.get("payload", {}) if isinstance(r.get("payload"), dict) else {}
            u = payload.get("user_msg", "")
            m = payload.get("my_reply", "")
            if u and u[:100] not in already:
                to_judge.append((u, m))
                if len(to_judge) >= last_n:
                    break
        except:
            continue
    results = []
    for u, m in to_judge:
        r = judge(u, m)
        results.append({"user_preview": u[:60], "overall": r["overall"]})
    return {"n_judged": len(results), "results": results}


def stats() -> dict:
    if not os.path.exists(JUDGE_LOG):
        return {"n_judgments": 0}
    n = 0
    verdicts = {"pass": 0, "warn": 0, "fail": 0}
    with open(JUDGE_LOG) as f:
        for line in f:
            try:
                r = json.loads(line)
                n += 1
                v = r.get("overall")
                if v in verdicts:
                    verdicts[v] += 1
            except: pass
    return {"n_judgments": n, "by_verdict": verdicts,
            "fail_rate": round(verdicts["fail"] / max(n, 1), 3)}


if __name__ == "__main__":
    try:
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("cmd", choices=["judge", "correction", "recent", "stats", "test"])
        ap.add_argument("--user", default="")
        ap.add_argument("--reply", default="")
        ap.add_argument("--n", type=int, default=5)
        args = ap.parse_args()
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise

    if args.cmd == "judge":
        r = judge(args.user, args.reply)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    elif args.cmd == "correction":
        r = judge_correction(args.user)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    elif args.cmd == "recent":
        print(json.dumps(judge_recent(last_n=args.n), ensure_ascii=False, indent=2))
    elif args.cmd == "stats":
        print(json.dumps(stats(), ensure_ascii=False, indent=2))
    elif args.cmd == "test":
        # 用户 2026-07-13 那句 抽象元观点
        sample = ("你不需要用规则,而是用llm去理解这样最简单,不如多llm去思考?"
                  "一个是没有上下文的专门校验?不知道可不可以?"
                  "同时完成思考我的指令?这种思路是不是更好,"
                  "是否可以自我演绎了? 你演绎我这种思想?")
        print(f"═ 输入 ═\n{sample}\n")
        print("═ LLM 判断 (correction_class) ═")
        r = judge_correction(sample)
        print(json.dumps(r, ensure_ascii=False, indent=2))
