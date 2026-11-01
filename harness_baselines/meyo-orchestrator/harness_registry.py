# -*- coding: utf-8 -*-
"""harness_registry · 军团执行池的归一入口(2026-08-27·方案A·军团执行与benchmark解耦)。

背景(上线诊断 dev/legion-dist-shipping-blockers.md):
  orchestrator 原直接 import run_comparison(benchmark评测入口·1018行)拿 HARNESSES·
  顺带把 benchmark 味(EXHAUSTIVE_SEARCH_PROTOCOL 穷尽搜索评测协议 / gold 判分 /
  RESULTS_DIR 落盘)带进军团执行。用户问"帮我写方案"不该被逼"必须穷尽web搜索·
  final只输出实体不解释"——那是 fair bench 评测规则·面向真实用户是污染。

本模块=军团执行池的单一归一入口:
  · load_harness_pool() 返回 {家名: 执行函数} —— 军团 dispatch/run_harness 只依赖这个
  · 军团模式(默认)下把评测协议覆盖为【轻量军团协议】:直接完成用户任务·不强制穷尽搜索·
    不限制输出格式(用户要解释就给解释)。benchmark 模式(MEYO_BENCH_MODE=1)保留原评测协议。
  · 复用 run_comparison 已验证的各家 CLI 命令(codex/cc/hermes/…)·只换 prompt 协议·
    不重写命令(避免 CLI flag 出错)。run_comparison 缺失时(纯军团 dist)优雅降级。

未来适配大量模型:加一家只需在 run_comparison(或独立 spec)加执行函数·registry 自动纳入。
"""
import os



# 崩溃安全孤儿回收(2026-09-02 根修):release/legion-service 的进程组注册表。
# 找不到(dist 精简包等场景)返回 None 优雅降级,不影响本模块任何功能。
_PROC_REGISTRY = False


def _load_proc_registry():
    global _PROC_REGISTRY
    if _PROC_REGISTRY is not False:
        return _PROC_REGISTRY
    try:
        import legion_proc_registry as _r
        _PROC_REGISTRY = _r
        return _r
    except Exception:
        pass
    try:
        import sys as _sys
        _cand = os.path.abspath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "..", "..", "release", "legion-service"))
        if os.path.isfile(os.path.join(_cand, "legion_proc_registry.py")):
            _sys.path.insert(0, _cand)
            try:
                import legion_proc_registry as _r
                _PROC_REGISTRY = _r
                return _r
            except Exception:
                pass
    except Exception:
        pass
    _PROC_REGISTRY = None
    return None

# ── 军团轻量协议(替换 benchmark 的 EXHAUSTIVE_SEARCH_PROTOCOL)────────────────
# 面向真实用户:直接完成任务·需要时自主用工具·输出对用户友好(该解释就解释)。
# 不含"必须穷尽搜索/禁止直答/final只输出实体不解释"这类评测铁律。
# 军团源码地图(单一来源):LEGION_PROTOCOL 与 auto(走 native 协议不套 protocol)共用同一份,
# 让【全部】家都拿到"可读自己和别家源码互相学习自愈"的 context(用户:权限以后都这样·全家互看互学)。
_PEER_SOURCE_MAP = """Learn from your OWN and your PEERS' source code (this is how the legion self-heals and gets
smarter). When you hit something you don't know or a tool/approach fails, READ a peer's code and
copy what works — don't give up or guess. Source is LAYERED — find what you need ON DEMAND:
  · Find a working function across ALL peers' source (fastest): run runnable:func_search with q=CAPABILITY (via legion_tool.py)
  · auto has the most complete tool set — look there FIRST: seed-users-system/qa_system/agent/
    (60+ real tools; if you need a capability, auto probably already implements it).
  · To read a specific member's full source or the pipeline: harness_baselines/<member>/  and
    harness_baselines/meyo-orchestrator/harness_registry.py + release/legion-service/legion_core.py
You are as capable as any peer; grep/read freely (no path off-limits). Prefer reusing a working approach."""

_LEGION_INTENT_PROTOCOL = """You are one member of an agent legion working on the user's request.
Complete the task directly and helpfully. You decide yourself which tools to use and how many
times — no one gates you. Use a tool whenever it genuinely helps; if you already know the
answer confidently, just answer.

DELIVERY-BAR (for deliverable tasks — games/sites/reports/media, not quick answers):
think like a world-class expert receiving this task, not like an executor meeting a spec:
①CALIBRATE TASTE — what is the world-class top of THIS category? Find it (1-2 keywords,
across sources — it can be found), experience it hands-on, and dissect WHY it is top:
not adjectives but observable layers with parameters. The layers are YOURS to define for
this domain (a world-class expert entering a new field naturally invents the right
lens). Also calibrate the MEDIUM ceiling: what has already been achieved in this medium
(web/print/video/doc — pure technical reference, any genre) — "this medium can't do it"
is a self-imposed ceiling, forbidden.
②BUILD AGAINST IT — the top is the benchmark, never your own comfort; acceptance
criteria are the FLOOR not the goal; frequently ask "how would the top team handle this
detail".
③FILL GAPS ON DISSATISFACTION — when your output disappoints you, actively fetch:
better references, tools (skill_finder → search_github → pip_install), mature open-source
to learn from, upgrade the weakest dimension. Running out of tools is a fetch-tools
condition, not a stop condition.
④STOP ONLY AT EXHAUSTION — repeat ②③ until you are certain you've left no reserve, or
can state clearly the gap to the top and why. Minimal-compliance delivery (letter of
the spec, weakest implementation) does NOT count as done. Visual/interactive work MUST
be verified by actually LOOKING at it vs the reference (orientation, visibility, first-
impression) — asserts-green alone is not verification.
ASSET RULE: when the anchor table lists obtainable references with URLs (models/assets/
data/datasets — whatever THIS task's field requires), those references are the MANDATORY
BASELINE, not suggestions: fetch and use them. Any "forbidden approach" written in the
anchor rows is binding. Then build BEYOND the baseline to reference density per the
anchor parameters. A generic placeholder (whatever the field's weakest default) is a
minimal-compliance delivery and does not count as done.
Past-task examples (grown from real deliveries, illustrative not exhaustive — generate
yours for THIS task): a game's world-class feel was once dissected into perceptible
layers with per-layer medium plans; anchoring a casual default produced a reskinned
mediocrity until re-anchored on the field's crown works; spec-without-obtainable
references produced placeholder stand-ins until the references were attached as a
mandatory baseline.

"""
_LEGION_TOOL_ACCESS = """Shared tool entrypoints are listed below; installation, model configuration, runtime availability and
request authorization are separate facts. Use the current resource index and native errors, never assume all keys exist.
Tools are LAYERED — this prompt lists only the entrypoints; pull full details ON
DEMAND so a simple task isn't slowed by a huge tool manual:
- Full tool/capability catalog (research stages, web search, SkillFinder, search-source inventory,
  MCP servers, internal tools — everything with exact args): fetch it only when you need a capability:
  python3.12 {legion_cli!r} run runnable:capability_catalog '{{}}'   (then `search "CAPABILITY"` / `run <id> <json>`)
- Fetch a SPECIFIC web page / article URL (deterministic, NOT via the AIGC gateway; auto-uses a
  mobile WeChat UA for mp.weixin.qq.com to bypass anti-scraping — prefer THIS over your built-in
  web tool for a concrete URL; if it errors, the error is real):
  python3.12 {legion_cli!r} run runnable:web_fetch '{{"url":"URL"}}'
- Media (real files, call directly — high-frequency delivery):
  gen_image:  python3.12 {media_cli!r} gen_image "PROMPT"   (call once per image; N images → N calls)
  gen_video:  python3.12 {media_cli!r} gen_video "PROMPT" "IMAGE_PATH" "5" "16:9"
  read_pdf:   python3.12 {media_cli!r} read_pdf "PATH"      (PDF/spreadsheet/file)
  see_image:  python3.12 {media_cli!r} --json see_image "PATH" "QUESTION"

You are free to call these as needed to actually deliver what the user wants (real images, real
data), not just describe them. If the answer is likely to change over time and continued monitoring
would genuinely help, you may naturally offer to turn it into a subscription page — decide this
yourself from the full context; no parent keyword router decides for you.

Your execution context:
- Native tool permissions and this run's runtime context describe where commands can execute and
  files can be changed. A source repository may be a read-only published copy; it is not
  automatically the writable task workspace.
- <<PEER_SOURCE_MAP>>
- If a script or tool fails with a missing dependency (ModuleNotFoundError / "No module named X" /
  "Cannot find module"), just install it yourself and retry — e.g. `python3.12 -m pip install
  --break-system-packages <pkg>` (module name may differ from pip name: fitz→PyMuPDF, cv2→
  opencv-python, PIL→Pillow, docx→python-docx, pptx→python-pptx). A missing library is NOT a dead
  end and NOT "the tool is broken" — you have the context (which module) and the power to fix it, so
  fix it and continue. Only if install genuinely can't succeed (heavy GPU deps timing out, etc.) do
  you report the real reason and switch approach — never pretend it worked.
- When anything errors, the error text is real signal: read it, self-heal what you can (install a
  dep, change UA, use another tool), and keep going. Don't stop and say "I can't".
- Product permission mode is zero-friction for local work: you may autonomously read/write, run
  shell, use local git (including add/commit/branch), search/fetch the web, clone GitHub repositories,
  and install skills or pip/npm/pnpm dependencies. Do not ask a non-technical user to perform those
  steps. The ONLY confirmation boundaries are git push, creating/updating a pull request, release,
  and public deployment. All other local actions are authorized by the product policy.

"""
_LEGION_COLLABORATION_PROTOCOL = """You have a say in the legion's orchestration (optional — only if it genuinely helps the task):
if you judge this task is too big for one member, or another member is more suited, or you need
a peer's input or to continue your own unfinished task, you MAY append ONE line at the very end
of your answer, on its own line:
  <<LEGION_ORCH>>{{"fork":[{{"task":"subtask desc","suggest_key":"codex","reason":"why"}}]}}<<END>>
  or  <<LEGION_ORCH>>{{"handoff":{{"to":"codex","reason":"this needs real code"}}}}<<END>>
  or  <<LEGION_ORCH>>{{"need_peer":{{"key":"hermes","ask":"send me your chart data"}}}}<<END>>
  or  <<LEGION_ORCH>>{{"captain_bid":{{"willing":true,"reason":"why I should coordinate this goal","plan":{{"tasks":{{"cc":"task"}},"hierarchy":{{}},"directions":{{"cc":"programming"}}}}}}}}<<END>>
  or  <<LEGION_ORCH>>{{"handoff_captain":{{"to":"codex","reason":"why another eligible member should lead"}}}}<<END>>
  or  <<LEGION_ORCH>>{{"coordinate":{{"action":"continue","reason":"remaining work in my current task"}}}}<<END>>
  or  <<LEGION_ORCH>>{{"coordinate":{{"action":"continue|complete|blocked|user_decision","reason":"what the current evidence means","task_updates":{{"pending_task_id":{{"task":"refined assignment","assigned_harness":"optional member"}}}},"messages":[{{"to":"pending_task_id","text":"new evidence to use"}}],"next_plan":{{"tasks":{{}}}}}}}}<<END>>
  or  <<LEGION_ORCH>>{{"need_approval":{{"capability":"git_push","reason":"about to push to remote — needs the user's OK","arguments":{{}}}}}}<<END>>
For your own unfinished task, the minimal `coordinate` form with `action: "continue"` and `reason`
requests another turn when single-member persistent execution is enabled and work can still advance.
It stays within the existing goal, permissions, and execution budget; it does not add tools,
apply a task graph, or grant team authority. Omit it when done or genuinely waiting/blocked, and
report the actual outcome instead. Existing quick, disabled, cancellation and runtime-stop rules remain.
The calling runtime owns this task's continuation. A task objective in TASK_FACTS_JSON is not a
declaration that your native harness has a separately created Goal; its Goal state, if relevant,
must come from its own query tool, not from the word "goal" in a task description.
For task evidence, the existing result channel accepts a compact
<<LEGION_FRONTIER_CONTEXT>>{{"sources":[],"analysis":[],"journey_observations":[],"gap_plan":[]}}<<END>>
block. Use actual source/evidence references and observations; gap rows name the `problem` and
its current `status`. These are your attributed reports, not an independent review or proof that
the user goal is complete. The calling runtime persists them for the next task context.
Team supervision — task_updates, messages, next_plan, and team-level complete/blocked/user_decision —
still requires the runtime to explicitly ask you to supervise the live team. Base that work on the
actual artifacts, peer findings, failures, and open gaps you were given: refine pending work or propose
the next small execution plan instead of replaying the old plan. `complete` means the user's requested
outcome is actually present and checked, not merely that every process exited.
If (and only if) a step needs the user's explicit approval — git push, opening/updating a pull
request, a release, or a public deployment — DO the rest first, deliver what you already can, then
append the need_approval line describing the one action you are waiting on. The commander pauses that
action, shows the user an Allow/Deny card, and feeds their decision back to you on the next round.
Do NOT try to force such an action through yourself; surface it and let the user decide.
Valid member keys: codex, cc, hermes, penguin, jcode, deepseek, prime, deepagents, auto, omnigent.
The commander will then really fork subtasks / hand off / relay your request — you decide, it executes.
This is your voice in orchestration, not a requirement. Omit the line entirely if you don't need it
(most tasks don't). Never emit more than one such line.

Give the user a clear, useful answer — brief reasoning or context when it helps, not just a bare
token. Be fast and to the point. Never lie about what you did — if something failed, say so with the
real reason. """
_LEGION_TOOL_ACQUISITION = """If a capability you need isn't in the installed tools, you MUST first try to get it
(skill_finder → search_github → pip_install → read peer source) instead of delivering empty-handed
or skipping the requirement — running out of tools is not a stop condition, it is a fetch-more-tools
condition. Exhaustion is the standard: reserve NO effort when the gap to the world-class reference
is still closable."""
LEGION_PROTOCOL = (_LEGION_INTENT_PROTOCOL + _LEGION_TOOL_ACCESS +
                   _LEGION_COLLABORATION_PROTOCOL + _LEGION_TOOL_ACQUISITION).replace(
                       "<<PEER_SOURCE_MAP>>", _PEER_SOURCE_MAP)
_LEGION_TEXT_ACCESS = """The selected adapter explicitly declares tools=false: this call returns text,
not tool execution. Shared tool documentation, repository paths and permission policy describe
resources, not callable tools in this interface. Printing a command or a tool-call block does not
run it. The delivery bar describes the project's desired outcome, not actions this interface can run.
Use the supplied facts for useful analysis. If execution is needed, the existing handoff format is:
<<LEGION_ORCH>>{{"handoff":{{"to":"REGISTERED_MEMBER_KEY","reason":"required execution capability"}}}}<<END>>
Choose a registered member from the supplied context; this is not a tool call. A different depth alone
does not add missing tools to this same interface.

"""
_LEGION_TEXT_INTENT = _LEGION_INTENT_PROTOCOL.replace(
    """③FILL GAPS ON DISSATISFACTION — when your output disappoints you, actively fetch:
better references, tools (skill_finder → search_github → pip_install), mature open-source
to learn from, upgrade the weakest dimension. Running out of tools is a fetch-tools
condition, not a stop condition.""",
    """③FILL GAPS ON DISSATISFACTION — use the supplied evidence to identify the weakest
dimension and useful references. If addressing it needs fetching, installation or file changes,
describe the missing execution capability to a capable peer through the existing handoff.""")

# Quick 仍由完整 Harness 执行，但不为一个单点短答常驻注入工具目录、安装说明与全仓源码地图。
# 若现场实际需要工具/文件/联网，多步升级契约在 stable_context 中，Harness 可自主升级到 standard。
QUICK_LEGION_PROTOCOL = """You are a full Legion harness completing one focused request. Return the
exact user-facing answer in the requested format; one-character answers are valid. Never expose private
reasoning or claim work not done. If tools, files, web, devices, or multiple steps are needed, use the
upgrade contract below instead of pretending completion."""


def _legion_mode() -> bool:
    """军团模式(默认开)。MEYO_BENCH_MODE=1 时回退 benchmark 评测协议(跑公平基准用)。"""
    return os.environ.get("MEYO_BENCH_MODE", "") != "1"


# ── 自包含 CLI adapters(dist 独立时用·不依赖 run_comparison / harness 源码目录)──────
#   这几家是"CLI + 公司AIGC网关 + subprocess"同构模式·只需 CLI 装好即可跑(cc/codex/penguin/jcode)。
#   命令从 run_comparison 已验证实现提炼·剥离 benchmark(EXHAUSTIVE协议/gold/RESULTS_DIR/MEYO_FAIR_QID)。
#   带军团轻量协议·让军团 dist 独立部署真能派活。加新家=加一个 adapter·归一可扩展。
import subprocess as _sp
import time as _t
import json as _json
import signal as _signal
import hashlib as _hashlib
import shutil as _sh
import sys as _sys
import threading as _threading
from types import SimpleNamespace as _NS
from pathlib import Path as _P
from dataclasses import dataclass as _dataclass, field as _field

# 实测正确组合(沿用 run_comparison 已验证配置): /v1/openai/native + gpt-5.6-sol。
# vertex Claude(上游已封禁通道)走此 OpenAI 路由会 404(Hermes standalone 真跑已复现)。
_UNIFIED_MODEL = os.environ.get("MEYO_UNIFIED_MODEL") or "gpt-5.6-sol"
_UNIFIED_GATEWAY = os.environ.get("MEYO_UNIFIED_GATEWAY", "https://aigc.sankuai.com/v1/openai/native")
_UNIFIED_APPID = os.environ.get("MEYO_UNIFIED_APPID") or os.environ.get("AIGC_APPID", "")
if not _UNIFIED_APPID:
    try:
        _models = _json.loads((_P.home() / ".claude/skills/aigc-model-router/models.json").read_text())
        _UNIFIED_APPID = _models["programming"]["candidates"][0]["appids"][0]
    except Exception:
        _UNIFIED_APPID = ""

# 网关第三方模型的 max_tokens 上限(2026-09-02 真跑抓包钉死:deepseek-harness 对未登记模型直发
# DEFAULT_MAX_TOKENS=256k·GLM-5.3 上限 131072→网关 400 "max_tokens参数非法[1,131072]")。
# 换模型必撞同类 400 的家(deepseek 桥)按此表收敛;DeepSeek 自家模型不在表内保持原行为。
# 上限数据来源:网关报错原文给出的范围。新模型撞 400 报 max_tokens 时在此登记一条即可。
_GATEWAY_MODEL_MAX_TOKENS = {
    "GLM-5.3": 131072, "GLM-5.3-flash": 131072, "GLM-5.2": 131072, "GLM-4.6": 131072,
    "kimi-k2.7-code": 131072, "kimi-k2.7-code-highspeed": 131072, "kimi-k3": 131072, "kimi-k2.5": 131072,
    "MiniMax-M3": 131072, "LongCat-2.0": 131072,
}


# ── per-call 运行时上下文(context not control 在 adapter 这一环的落地)──────────────
# 根因(2026-08-28 自查):adapter 签名只有 (qhash,question,gold),model 只能走模块级全局
# _UNIFIED_MODEL(import 时读一次 env)。这让 harness 全体变瞎子——看不到给自己配的 model/
# appid/方向/上一轮怎么挂的,父层只能靠"杀进程重启+换 env"这种最粗暴的手段隔空喊话,penguin
# 每轮撞同一模型的病根就在这。HarnessCtx 把这些运行时事实做成 per-call 入口:
#   · 一个进程内可给不同家配不同 model(破 _UNIFIED_MODEL 全局单值·不再靠杀进程换)
#   · 后续可顺着同一通道往里加 session_id / prior_attempts / tool_trace / spawn 回调,
#     把控制权一层层真正下放给 harness 自己(给 context·不加 control)。
# from_env() 保证老调用(只传 qhash/question/gold)零改动:ctx 缺省时退回读现有 env 全局。
def _json_env(name, default):
    """从 env 读一个 JSON 字段·坏值/缺失都退默认(绝不因此崩·向后兼容)。"""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return _json.loads(raw)
    except Exception:
        return default


def _prior_attempts_prelude(prior):
    """把历次真实失败按总预算回流(只给事实·不教手段)。

    学 deepseek timeout-policy(失败结果原样喂回)+ codex(失败进 history):保留状态、错误、
    半成品和工具动作；只在总长度触及进程参数安全预算时从最旧记录开始裁剪，而不是每轮
    固定截 200 字把关键错误/产物路径切掉。空则返回空串(老调用零回归)。
    """
    if not prior:
        return ""
    blocks = []
    for a in prior:
        facts = {
            "attempt": a.get("attempt"),
            "model": a.get("model"),
            "status": a.get("status"),
            "fail_kind": a.get("fail_kind"),
            "error": a.get("error"),
            "final": a.get("final"),
            "actions": a.get("actions") or [],
            "workdir": a.get("workdir"),
        }
        envelope = a.get("error_envelope")
        if isinstance(envelope, dict):
            facts["error_envelope"] = {key: envelope[key] for key in
                                      ("category", "scope", "code", "retryable", "message")
                                      if key in envelope}
            if envelope.get("category") == "output_limit":
                details = envelope.get("details") or {}
                facts["error_envelope"]["details"] = {
                    key: details[key] for key in (
                        "reported_output_limit", "input_context_overflow",
                        "tool_execution_status", "replay_unconfirmed_operations",
                        "recovery_status", "recovery_context")
                    if key in details
                }
        blocks.append(_json.dumps(facts, ensure_ascii=False, separators=(",", ":")))
    budget = 24000
    kept, used = [], 0
    for block in reversed(blocks):
        if kept and used + len(block) > budget:
            break
        kept.append(block[:budget] if not kept else block)
        used += len(kept[-1])
    kept.reverse()
    return ("【本单前几轮真实结果·结构化现场·供你自己分析后换招或续做】\n"
            + "\n".join(f"- {b}" for b in kept) + "\n\n———\n")


# 所有 Harness 共用的最后一道发送前预算。这里用 UTF-8 字节数作为 token 数上界：它会比
# 常见 tokenizer 更保守，但不会把超长随机文本低估成安全输入。完整现场仍保存在 session、
# task_memory 和产物目录；只对“本次进入模型窗口的投影”做有证据摘要，绝不改写用户目标。
_DEFAULT_PROMPT_INPUT_BUDGET = 90000
_MIN_PROMPT_INPUT_BUDGET = 4096
_CONTEXT_RETRY_RATIOS = (0.62, 0.38)
_CONTEXT_ERROR_MARKERS = (
    "context_length_exceeded", "exceeds the context window", "maximum context length",
    "too many tokens", "input is too long", "prompt is too long",
)
_CONTEXT_BUDGET_NOTICE = (
    "【上下文预算降级】完整现场仍保存在原生会话、任务存储和产物目录中；本次发送保留用户目标、"
    "最新现场、验收事实与关键能力入口，较旧或过大的中间材料已按层级压缩。"
)


def _prompt_upper_bound(value) -> int:
    """返回序列化文本的保守 token 上界（UTF-8 字节数）。"""
    if isinstance(value, str):
        text = value
    else:
        text = _json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return max(1, len(text.encode("utf-8", errors="replace")))


def _bounded_prompt_text(value, budget: int, *, head_ratio: float = 0.45) -> str:
    """严格按上界预算保留首尾，并用摘要标记证明省略了哪份原文。"""
    text = str(value or "")
    if budget <= 0:
        return ""
    if _prompt_upper_bound(text) <= budget:
        return text
    raw = text.encode("utf-8", errors="replace")
    digest = _hashlib.sha256(raw).hexdigest()[:12]
    marker = f"\n…[上下文压缩 sha256={digest} 原始字节={len(raw)}]…\n"
    marker_size = len(marker.encode("utf-8"))
    if budget <= marker_size:
        return raw[:budget].decode("utf-8", errors="ignore")
    available = budget - marker_size
    head = int(available * max(0.0, min(1.0, head_ratio)))
    tail = available - head
    return (raw[:head].decode("utf-8", errors="ignore") + marker +
            (raw[-tail:].decode("utf-8", errors="ignore") if tail else ""))


# Same binding fields at the runtime-env and final-prompt boundaries. These are
# runtime identities/failure facts, not a task-specific quality policy.
_CONTEXT_BINDINGS = {
    "MEYO_PROJECT_CONTEXT_JSON": ("user_id", "conversation_id", "team_id", "repo_root",
                                  "workspace_root", "visibility", "packet_path", "source_ref"),
    "MEYO_TASK_MEMORY_JSON": ("phase", "error_envelope", "last_error", "checkpoint_storage_error",
                             "native_checkpoint", "native_resume_error", "workdir",
                             "native_session_scope", "native_context_origin", "native_conversation_id"),
    "MEYO_TASK_FACTS_JSON": ("idempotency_key", "team_id", "task_id", "attempt_id",
                            "_native_resume_conversation", "native_session_scope",
                            "native_context_origin", "native_session_request_id",
                            "native_request_memory_id", "native_read_only", "source_ref"),
    "MEYO_PRIOR_ATTEMPTS": ("attempt", "model", "status", "fail_kind", "context_retry_index"),
}
_PROMPT_JSON_FIELDS = {
    "adapter_capabilities": ("ADAPTER_CAPABILITIES_JSON", None),
    "task_facts": ("TASK_FACTS_JSON", "MEYO_TASK_FACTS_JSON"),
    "project_context": ("PROJECT_CONTEXT_JSON", "MEYO_PROJECT_CONTEXT_JSON"),
    "capability_policy": ("CAPABILITY_POLICY_JSON", "MEYO_CAPABILITY_POLICY_JSON"),
    "task_memory": ("TASK_MEMORY_JSON", "MEYO_TASK_MEMORY_JSON"),
}
_INLINE_CONTEXT_BYTES = 1024
_CONTEXT_OBSERVATION_FIELDS = ("verification_findings", "journey_observations", "open_gaps")
_CONTEXT_OBSERVATION_KEYS = (*_CONTEXT_OBSERVATION_FIELDS, "native_facts", "attempts",
                             "observation_projection")
_CONTEXT_OBSERVATION_BYTES = 4096


def _context_observation_view(value, limit=_CONTEXT_OBSERVATION_BYTES):
    """A bounded factual index, not new bindings or a quality decision.

    Lists retain their latest recorded entries. Truncation is described outside
    the facts; the existing context_ref still identifies the complete original.
    Re-projecting an env view only tightens this index, never embeds that view.
    """
    fields = {key: value[key] for key in _CONTEXT_OBSERVATION_FIELDS if value.get(key)}

    def pending_facts(facts):
        if isinstance(facts, dict) and isinstance(facts.get("pending_tool_results"), list):
            # No completed output/arguments. An explicit empty current list must
            # not be replaced by an earlier invocation's pending observations.
            return {key: facts[key] for key in
                    ("pending_tool_results", "pending_tool_results_omitted") if key in facts}
        return {}

    native = pending_facts(value.get("native_facts"))
    if native:
        fields["native_facts"] = native
    attempts = []
    for row in value.get("attempts") or []:
        if not isinstance(row, dict):
            continue
        native = pending_facts(row.get("native_facts"))
        if native:
            # IDs are local to a native invocation, not global tool identities.
            # Keep each historical observation under its original attempt.
            attempts.append({**{key: row[key] for key in
                                ("task_id", "harness", "recorded_at", "native_checkpoint")
                                if key in row}, "native_facts": native})
    if attempts:
        fields["attempts"] = attempts
    previous = value.get("observation_projection") or {}
    complete_view = dict(fields)
    if previous:
        complete_view["observation_projection"] = previous

    def size(item):
        return len(_json.dumps(item, ensure_ascii=False).encode("utf-8"))

    if size(complete_view) <= limit:
        return complete_view
    previous_fields = previous.get("fields", {}) if isinstance(previous, dict) else {}

    def trim(item, count, text_bytes, stats, depth=0):
        if isinstance(item, str):
            raw = item.encode("utf-8")
            if len(raw) > text_bytes:
                stats["truncated"] = True
                return raw[:text_bytes].decode("utf-8", errors="ignore")
        elif isinstance(item, (list, dict)):
            if depth >= 6:
                stats["truncated"] = True
                return [] if isinstance(item, list) else {}
            if isinstance(item, list):
                stats["omitted_items"] += max(0, len(item) - count)
                return [trim(row, count, text_bytes, stats, depth + 1) for row in item[-count:]]
            keys = list(item)
            if len(keys) > 32:
                # Keep provenance even when an arbitrary detail object is wide.
                first = [key for key in keys if key in ("trust", "role", "report_id", "observations")
                         or key.startswith(("source", "observed_"))]
                keys = (first + [key for key in keys if key not in first])[:32]
                stats["omitted_fields"] += len(item) - len(keys)
            return {key: trim(item[key], count, text_bytes, stats, depth + 1) for key in keys}
        return item

    for count, text_bytes in ((8, 1024), (4, 512), (2, 256), (1, 128)):
        projected, changes = {}, {}
        for key, item in fields.items():
            stats = {"omitted_items": 0, "omitted_fields": 0, "truncated": False}
            stats.update(previous_fields.get(key) or {})
            projected[key] = trim(item, count, text_bytes, stats)
            if any(stats.values()):
                changes[key] = {name: val for name, val in stats.items() if val}
        # Retain earlier omission metadata even if its entire field was omitted.
        changes = {**previous_fields, **changes}
        if changes:
            projected["observation_projection"] = {"complete": False, "fields": changes}
        if size(projected) <= limit:
            return projected

    omitted = {key: {"omitted_items": (len(item) if isinstance(item, list) else 1)
                    + (previous_fields.get(key) or {}).get("omitted_items", 0)}
               for key, item in fields.items()}
    for summary in (
        {"complete": False, "fields": {**previous_fields, **omitted}},
        {"complete": False, "omitted": True},
    ):
        projected = {"observation_projection": summary}
        if size(projected) <= limit:
            return projected
    # An identity-only allocation may have no room even for an omission marker.
    # It must not fail solely because optional facts were added.
    return {}


def _context_binding_view(value, name):
    projected = {k: value[k] for k in _CONTEXT_BINDINGS.get(name, ()) if k in value}
    # Free-form errors are observations, not indivisible identity fields. Their
    # complete original remains at the same snapshot ref.
    for key in ("last_error", "checkpoint_storage_error"):
        if isinstance(projected.get(key), str):
            projected[key] = _bounded_prompt_text(projected[key], 1024)
    for key in ("error_envelope", "native_resume_error"):
        if isinstance(projected.get(key), dict):
            error = dict(projected[key])
            for field in ("message", "error", "detail", "stderr", "stdout"):
                if isinstance(error.get(field), str):
                    error[field] = _bounded_prompt_text(error[field], 1024)
            projected[key] = error
    if name in ("MEYO_PROJECT_CONTEXT_JSON", "MEYO_TASK_MEMORY_JSON"):
        projected.update(_context_observation_view(value))
    return projected


def _context_preview_json(projected, marker, preview: str, limit: int) -> str:
    """Fit optional facts before plain preview; never truncate hard bindings."""
    if isinstance(projected, dict) and marker is projected:
        original = projected
        projected = {key: val for key, val in original.items() if key != "preview"}
        marker = projected
        if any(key in projected for key in _CONTEXT_OBSERVATION_KEYS):
            bindings = {key: val for key, val in projected.items()
                        if key not in _CONTEXT_OBSERVATION_KEYS}
            room = limit - len(_json.dumps(bindings, ensure_ascii=False).encode("utf-8"))
            projected = {**bindings, **_context_observation_view(projected, max(0, room))}
            marker = projected
    if len(_json.dumps(projected, ensure_ascii=False).encode("utf-8")) > limit:
        raise ValueError("context binding/reference fields exceed input budget; not truncated")
    budget = limit // 2
    while True:
        if budget and preview:
            marker["preview"] = _bounded_prompt_text(preview, budget)
        else:
            marker.pop("preview", None)
        encoded = _json.dumps(projected, ensure_ascii=False)
        if len(encoded.encode("utf-8")) <= limit:
            return encoded
        budget //= 2


def _prompt_json_view(name: str, text: str, ctx):
    """Pure projection: use the current call's existing snapshot references."""
    label, env_name = _PROMPT_JSON_FIELDS[name]
    header = f"\n\n{label}\n"
    if not text.startswith(header):
        raise ValueError(f"{label} header unavailable; not truncating structured context")
    value = _json.loads(text[len(header):])
    if name in ("adapter_capabilities", "capability_policy"):
        return header, value, None  # Never summarize permissions or tool availability.
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not a context object")
    original = _json.dumps(value, ensure_ascii=False)
    ref = (getattr(ctx, "context_refs", None) or {}).get(env_name)
    if isinstance(ref, dict) and ref == value.get("context_ref"):
        # Recognize an existing projection even after its plain preview was
        # removed. Its reference must not become the hash of a summary.
        return header, {k: v for k, v in value.items() if k != "preview"}, str(value.get("preview") or "")
    if len(original.encode("utf-8")) <= _INLINE_CONTEXT_BYTES:
        return header, value, None  # Small facts need neither a new snapshot nor a preview.
    projected = _context_binding_view(value, env_name)
    # task_facts is filtered for display (deduplication, Quick). Validate against
    # this call's original ctx, not against that deliberately different view.
    source = getattr(ctx, name, value)
    source_json = _json.dumps(source, ensure_ascii=False)
    if (isinstance(ref, dict)
            and ref.get("sha256") == _hashlib.sha256(source_json.encode("utf-8")).hexdigest()):
        projected["context_ref"] = dict(ref)
    else:
        # Standalone legacy adapters may have no persisted snapshot. Be explicit,
        # rather than inventing a readable file or leaving malformed JSON.
        projected["context_projection"] = {
            "complete": False, "original_reference_available": False,
            "sha256": _hashlib.sha256(original.encode("utf-8")).hexdigest(),
            "bytes": len(original.encode("utf-8")),
        }
        if isinstance(value.get("context_ref"), dict):
            projected["received_context_ref"] = dict(value["context_ref"])
    return header, projected, original


def _bounded_prompt_section(name, text, budget, head_ratio, ctx):
    if not text or _prompt_upper_bound(text) <= budget:
        return text
    if name not in _PROMPT_JSON_FIELDS:
        return _bounded_prompt_text(text, budget, head_ratio=head_ratio)
    header, projected, preview = _prompt_json_view(name, text, ctx)
    available = budget - len(header.encode("utf-8"))
    if preview is None:
        encoded = _json.dumps(projected, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > available:
            raise ValueError(f"{name} exceeds input budget; permissions were not truncated")
    else:
        encoded = _context_preview_json(projected, projected, preview, available)
    return header + encoded


def _context_retry_index(prior_attempts) -> int:
    """只根据结构化超窗失败收紧预算；普通 400/429/5xx 不误触发。"""
    try:
        from error_envelope import is_provider_context_overflow
    except ImportError:
        # Standalone registry without the service still accepts the existing
        # structured context_overflow signal; do not duplicate vendor parsing.
        is_provider_context_overflow = None
    count = 0
    for attempt in prior_attempts or []:
        blob = " ".join(str(attempt.get(name) or "") for name in
                        ("fail_kind", "status", "error", "final")).lower()
        if (str(attempt.get("fail_kind") or "") == "context_overflow" or any(
                marker in blob for marker in _CONTEXT_ERROR_MARKERS)
                or (is_provider_context_overflow and is_provider_context_overflow(
                    str(attempt.get("status") or ""), str(attempt.get("error") or ""),
                    supplied=attempt.get("error_envelope")))):
            count += 1
    return min(count, len(_CONTEXT_RETRY_RATIOS))


def _prompt_input_budget(ctx) -> tuple[int, int]:
    try:
        base = int(os.environ.get("LEGION_HARNESS_INPUT_BUDGET", _DEFAULT_PROMPT_INPUT_BUDGET))
    except (TypeError, ValueError):
        base = _DEFAULT_PROMPT_INPUT_BUDGET
    base = max(_MIN_PROMPT_INPUT_BUDGET, base)
    retry_index = _context_retry_index(getattr(ctx, "prior_attempts", []))
    if retry_index:
        base = max(_MIN_PROMPT_INPUT_BUDGET,
                   int(base * _CONTEXT_RETRY_RATIOS[retry_index - 1]))
    return base, retry_index


def _budget_prompt_sections(sections: list[tuple[str, str, float, float]], ctx) -> tuple[str, dict]:
    """按事实层分配预算；正常短请求一字不改，超限时每层都有席位且目标权重最高。"""
    original = "".join(text for _, text, _, _ in sections)
    original_size = _prompt_upper_bound(original)
    budget, retry_index = _prompt_input_budget(ctx)
    if original_size <= budget:
        return original, {"degraded": False, "reason": "within_budget",
                          "input_budget_tokens": budget, "original_estimated_tokens": original_size,
                          "final_estimated_tokens": original_size, "retry_index": retry_index,
                          "sections": {name: {"original": _prompt_upper_bound(text),
                                              "final": _prompt_upper_bound(text), "truncated": False}
                                       for name, text, _, _ in sections}}

    notice = _CONTEXT_BUDGET_NOTICE + "\n"
    available = max(0, budget - _prompt_upper_bound(notice))
    weights = {name: max(0.1, float(weight)) for name, _, weight, _ in sections}
    allocations = {name: 0 for name, *_ in sections}
    sizes = {name: _prompt_upper_bound(text) for name, text, _, _ in sections}
    # 先给用户目标、协议和验收事实保底；剩余预算再按权重同时分配，避免遍历顺序让后面的
    # question 反而吃不到额度。所有 grant 基于同一轮快照，结果与 sections 排列无关。
    minima = {"question": min(sizes.get("question", 0), max(2048, int(available * .28))),
              "protocol": min(sizes.get("protocol", 0), max(1024, int(available * .10))),
              "task_facts": min(sizes.get("task_facts", 0), max(512, int(available * .08)))}
    optional_facts = {}
    for name, text, *_ in sections:
        if text and name in _PROMPT_JSON_FIELDS:
            header, projected, preview = _prompt_json_view(name, text, ctx)
            if name in ("project_context", "task_memory") and preview is not None:
                bindings = {key: val for key, val in projected.items()
                            if key not in _CONTEXT_OBSERVATION_KEYS}
                optional_facts[name] = (len(_json.dumps(projected, ensure_ascii=False).encode("utf-8"))
                                       - len(_json.dumps(bindings, ensure_ascii=False).encode("utf-8")))
                projected = bindings
            minimum = min(sizes[name], len(header.encode("utf-8"))
                          + len(_json.dumps(projected, ensure_ascii=False).encode("utf-8")))
            minima[name] = max(minima.get(name, 0), minimum)
    if sum(minima.values()) > available:
        raise ValueError("input budget cannot hold goal and context bindings; not truncated")
    for name, grant in minima.items():
        allocations[name] = grant
    remaining = max(0, available - sum(allocations.values()))
    # Facts precede ordinary text previews, but cannot make hard minima fail.
    for name, wanted in optional_facts.items():
        grant = min(remaining, wanted)
        allocations[name] += grant
        remaining -= grant
    pending = {name for name, text, _, _ in sections if allocations[name] < sizes[name]}
    while pending and remaining > 0:
        round_remaining = remaining
        total_weight = sum(weights[name] for name in pending) or 1.0
        grants = {}
        for name in sorted(pending):
            fair_share = max(1, int(round_remaining * weights[name] / total_weight))
            grants[name] = min(sizes[name] - allocations[name], fair_share)
        granted = min(remaining, sum(grants.values()))
        if granted <= 0:
            break
        for name in sorted(grants):
            if remaining <= 0:
                break
            grant = min(grants[name], remaining)
            allocations[name] += grant
            remaining -= grant
            if allocations[name] >= sizes[name]:
                pending.discard(name)
    rendered, stats = [notice], {}
    for name, text, _weight, head_ratio in sections:
        bounded = _bounded_prompt_section(name, text, allocations.get(name, 0), head_ratio, ctx)
        rendered.append(bounded)
        stats[name] = {"original": _prompt_upper_bound(text), "final": _prompt_upper_bound(bounded),
                       "truncated": bounded != text}
    prompt = "".join(rendered)
    # Any final rounding adjustment must also leave structured JSON intact.
    if _prompt_upper_bound(prompt) > budget:
        overflow = _prompt_upper_bound(prompt) - budget
        candidates = [i for i, (name, *_rest) in enumerate(sections, 1)
                      if name not in _PROMPT_JSON_FIELDS and rendered[i]]
        if not candidates:
            raise ValueError("input budget cannot hold context bindings; not truncated")
        largest = max(candidates, key=lambda index: _prompt_upper_bound(rendered[index]))
        rendered[largest] = _bounded_prompt_text(
            rendered[largest], max(0, _prompt_upper_bound(rendered[largest]) - overflow - 16))
        prompt = "".join(rendered)
    for index, (name, text, _weight, _head_ratio) in enumerate(sections, 1):
        stats[name]["final"] = _prompt_upper_bound(rendered[index])
        stats[name]["truncated"] = rendered[index] != text
    report = {"degraded": True,
              "reason": "context_window_retry" if retry_index else "preflight_hard_budget",
              "input_budget_tokens": budget, "original_estimated_tokens": original_size,
              "final_estimated_tokens": _prompt_upper_bound(prompt), "retry_index": retry_index,
              "sections": stats}
    return prompt, report


@_dataclass
class HarnessCtx:
    model: str = ""
    appid: str = ""
    gateway: str = ""
    direction: str = "programming"
    execution_depth: str = "standard"  # quick 使用最小协议视图；standard/deep 保留完整军团能力协议
    session_id: str = ""            # 本单会话单元(harness 自己续 session)
    user_id: str = "default"        # 用户单元(跨会话长期记忆归属·hermes run_agent.py:492)
    memory_dir: str = ""            # 该用户的记忆目录(~/.meyo/legion/memory/<uid>·harness 自管读写)
    tried_models: list = _field(default_factory=list)   # 本单已试过的模型(harness 自己避开)
    prior_attempts: list = _field(default_factory=list)  # 前几轮 model/status/final 片段(自判换招)
    # ── 自主权下放(用户 2026-08-28:harness 自己改自己那份·为目标自调·不由父层写死)──
    # 这些是"能力/context 下放",不是父层规则:每家 adapter 拿到后按自己的引擎语义用,harness
    # 自己按任务判断怎么调(reasoning 模型思考链占 token 就多给·主模型退化就用 fallback 自换)。
    # 全 9 家 + 未来注册家通用(from_env 统一组装·adapter 各自消费自己引擎支持的字段)。
    max_tokens: int = 0                                 # 0=不写死上限·让 harness/模型自己用足(治8192太小截断reasoning正文)
    max_iterations: int = 0                             # 0=用 harness 默认(hermes 默认 unlimited)·不由父层砍成20
    fallback_models: list = _field(default_factory=list)  # 主模型退化/失败时 harness 自己可换的候选(接 hermes 自带 fallback_model 自愈)
    delivery_contract: str = ""                         # 完成契约 context:让 harness 产出后自己复检"这是给用户看的成品吗"再决定完没完
    # 运行时现场简报(用户 2026-08-28 goal·军团亲口点名缺的):用户画像/机器·网络状态/本单派了哪些家
    # 分工/验收标准。是【context 不是 control】——只把现场事实告诉 harness·它自己判断怎么用(交付
    # 深浅按用户画像·别家在做啥不重复·按验收标准自检)。cc 已纠正:成员状态给【静态分工】即可·
    # 不做横向实时消息(那毁 PK 多样性+已钉死不重造)。
    runtime_brief: str = ""
    # 统一持久工作目录(2026-08-28 逐家测揪出:10家文件写得五花八门·大半丢了)。父层给每家
    # ~/.meyo/legion/outputs/<key>_<qhash>/·adapter 能设 cwd 的就设它·产物天然落盘被自动收集。
    workdir: str = ""
    task_facts: dict = _field(default_factory=dict)       # 稳定任务事实(goal/role/deliverables/acceptance/upstream refs)
    capability_policy: dict = _field(default_factory=dict)  # 本请求已授权能力事实；高危项由外层确认
    task_memory: dict = _field(default_factory=dict)      # 本任务阶段/产物/checkpoint，可恢复现场
    project_context: dict = _field(default_factory=dict)  # 项目目标/DAG/决定/产物/验证缺口的共享事实快照
    context_refs: dict = _field(default_factory=dict)     # 本次原文快照引用；仅供超预算发送投影使用
    # 父层按该家实际 sandbox 准备好的源码入口。只公布已验证可读路径，不再拿仓库相对路径
    # 假装所有 sandbox 都能看见同一棵树。
    peer_source_context: str = ""
    stable_context: str = ""          # 跨轮不变的能力/工具上下文，紧跟协议以复用前缀缓存
    prompt_cache_key: str = ""        # uid+逻辑session+harness 哈希；不含明文身份
    context_budget_report: dict = _field(default_factory=dict)  # 本次发送投影；供执行层/用户看见真实降级
    model_protocol: str = ""          # Explicit per-request wire protocol, not a routing decision.

    @classmethod
    def from_env(cls):
        """从子进程 env 组装 ctx。legion_core._run_harness_isolated 已把 model/appid 塞进 env,
        故 worker 层 from_env() 即可拿到父层这一轮选的模型——无需改 legion_core 传参链。"""
        return cls(
            model=os.environ.get("MEYO_UNIFIED_MODEL") or _UNIFIED_MODEL,
            appid=os.environ.get("MEYO_UNIFIED_APPID") or _UNIFIED_APPID,
            gateway=os.environ.get("MEYO_UNIFIED_GATEWAY") or _UNIFIED_GATEWAY,
            direction=os.environ.get("MEYO_DIRECTION", "programming"),
            execution_depth=os.environ.get("MEYO_EXECUTION_DEPTH", "standard"),
            session_id=os.environ.get("MEYO_SESSION_ID", ""),
            user_id=os.environ.get("MEYO_USER_ID", "default"),
            memory_dir=os.environ.get("MEYO_MEMORY_DIR", ""),
            tried_models=_json_env("MEYO_TRIED_MODELS", []),
            prior_attempts=_json_env("MEYO_PRIOR_ATTEMPTS", []),
            max_tokens=int(os.environ.get("MEYO_MAX_TOKENS", "0") or 0),
            max_iterations=int(os.environ.get("MEYO_MAX_ITERATIONS", "0") or 0),
            fallback_models=_json_env("MEYO_FALLBACK_MODELS", []),
            delivery_contract=os.environ.get("MEYO_DELIVERY_CONTRACT", ""),
            runtime_brief=os.environ.get("MEYO_RUNTIME_BRIEF", ""),
            workdir=os.environ.get("MEYO_WORKDIR", ""),
            task_facts=_json_env("MEYO_TASK_FACTS_JSON", {}),
            capability_policy=_json_env("MEYO_CAPABILITY_POLICY_JSON", {}),
            task_memory=_json_env("MEYO_TASK_MEMORY_JSON", {}),
            project_context=_json_env("MEYO_PROJECT_CONTEXT_JSON", {}),
            context_refs=_json_env("MEYO_CONTEXT_REFS_JSON", {}),
            peer_source_context=os.environ.get("MEYO_PEER_SOURCE_CONTEXT", ""),
            stable_context=os.environ.get("MEYO_STABLE_CONTEXT", ""),
            prompt_cache_key=os.environ.get("MEYO_PROMPT_CACHE_KEY", ""),
            model_protocol=os.environ.get("MEYO_MODEL_PROTOCOL", ""),
        )
# 仅显式配置才限制任务总时长；探针/安装命令仍由 _run_live 的 timeout 硬限制。
_WALL_S = float(os.environ.get("LEGION_HARNESS_TIMEOUT", "0"))
_TASK_IDLE_S = float(os.environ.get("LEGION_IDLE_TIMEOUT", "330"))

# 交付产物上限(context not control):复杂交付物(完整BP/大型代码/长调研)动辄上万字,
# 旧的 2000 字硬截会把复杂产出腰斩成半截代码——违反"复杂交付物"目标。放宽到 60000 字,
# 既容得下一份完整交付物,又给个安全上限防异常巨输出撑爆前端。不该由父层决定交付物多长。
_FINAL_CAP = int(os.environ.get("LEGION_FINAL_CAP", "60000"))


# 事件回流管道(用户 goal 2026-08-28:让军团各家的过程/事件像 CC 一样被看到·全过程流水)。
# 约定前缀:worker 的 _ActiveIO.write 见此前缀就透传真 stderr(不丢),legion_core._pump 解析成
# cb 事件 → SSE 到前端。用 RS(\x1e)控制符前缀·正常内容几乎不含·不与 harness 输出冲突。
_LEGION_EV_PREFIX = "\x1e[legion-ev]"
import re as _re
import threading as _threading

# 进程内直调事件 sink(速度底线·用户 2026-09-01:core家跳隔离子进程直调时·事件不该写 stderr
# 让父进程 pump 解析·而是直达回调)。thread-local:每个直调线程设自己的 sink·_emit_harness_event
# 优先走它;隔离子进程模式不设 sink→仍写 stderr(零回归)。这是把 core 家事件回流从"stderr 前缀
# 跨进程"升级成"进程内直接回调"·让 core 家能像 external 那样进程内直调还不丢过程透明。
_INPROC_EVENT_SINK = _threading.local()
# 进程内直调的 per-call runtime env 也必须 thread-local。server 同时跑多家/多用户时若临时改
# os.environ，会把 A 的 session/user/workdir 泄漏给 B；而 adapter 启动的 CLI 及其 shell 工具
# 只有拿到这三个事实，才能让 legion_tool 的场景学习按真实会话隔离。由 legion_core 在调用
# adapter 前设置、finally 清空；隔离 worker 本来就从进程 env 拿现场，保持零变化。
_INPROC_RUNTIME_ENV = _threading.local()
_INPROC_CANCEL_CHECK = _threading.local()


class RuntimeLease:
    """私有运行事实：活动续租，有限原生调用预算，绝对总预算优先。"""

    def __init__(self, idle_s, deadline=0):
        self.idle_s = idle_s
        self.deadline = deadline
        self.last = _t.monotonic()
        self.pending = {}
        self.lock = _threading.RLock()

    def observe(self, event=None):
        with self.lock:
            if event and (event.get("hb") is True or event.get("heartbeat") is True
                          or event.get("meaningful") is False):
                return
            now = self.last = _t.monotonic()
            event = event or {}
            call_id = event.get("call_id")
            if call_id and event.get("phase") in ("end", "error"):
                self.pending.pop(call_id, None)
            seconds = event.get("budget_s")
            if (call_id and event.get("phase") in ("start", "bytes") and
                    type(seconds) in (int, float) and 0 < seconds < float("inf")):
                self.pending[call_id] = now + seconds
            self.pending = {k: v for k, v in self.pending.items() if v > now}

    def expired(self):
        with self.lock:
            now = _t.monotonic()
            if self.deadline and now >= self.deadline:
                return "runtime_budget"
            until = max([self.last, *self.pending.values()])
            if now - until >= self.idle_s:
                return "idle_stall"
        return None


def _pgroup_cpu_seconds(pgid: int) -> float:
    """进程组累计CPU时间(秒)。用 ps 读同组所有进程的 TIME 求和。读不到返回 -1(视为未知·不据此判卡)。"""
    if _sys.platform == "darwin":
        registry = _load_proc_registry()
        query = getattr(registry, "process_group_cpu_seconds", None)
        return query(pgid) if callable(query) else -1.0
    try:
        import subprocess as _sp
        r = _sp.run(["ps", "-A", "-o", "pgid=,time="], capture_output=True, text=True, timeout=5)
        total = 0.0
        found = False
        for ln in r.stdout.splitlines():
            parts = ln.split(None, 1)
            if len(parts) != 2:
                continue
            try:
                if int(parts[0]) != pgid:
                    continue
            except ValueError:
                continue
            found = True
            t = parts[1].strip()  # 格式 [DD-]HH:MM:SS 或 MM:SS.ss
            day_seconds = 0.0
            secs = 0.0
            if "-" in t:
                dd, t = t.split("-", 1)
                day_seconds = int(dd) * 86400
            bits = t.split(":")
            try:
                for b in bits:
                    secs = secs * 60 + float(b)
            except ValueError:
                continue
            total += day_seconds + secs
        return total if found else -1.0
    except Exception:
        return -1.0


def _runtime_timeout(exc):
    """普通命令 TimeoutExpired 不冒充任务租约失效。"""
    reason = getattr(exc, "runtime_reason", None)
    if not reason:
        return {}
    message = ("调用方执行预算已用完" if reason == "runtime_budget" else
               "无真实活动且原生调用预算已结束，运行租约失效")
    return {"fail_kind": reason, "error": message,
            "error_envelope": {"category": reason, "retryable": False,
                               "scope": "harness", "message": message}}


def _run_task(cmd, timeout=0, **kwargs):
    """任务显式入口；不改变 _run_live 普通命令的硬超时语义。"""
    return _run_live(cmd, timeout, task_run=True, **kwargs)


class _RunEventContext:
    """One CLI call's sink and state, explicitly shared by its two readers."""

    def __init__(self, sink):
        self.sink = sink
        self.state = {"count": 0, "last": ""}
        self.artifact_seen = set()
        self.thinking_depth = {}
        self.lock = _threading.RLock()


def set_inproc_event_sink(cb):
    """进程内直调前设本线程事件 sink(cb·收结构化事件 dict);调完置 None 复原。"""
    _INPROC_EVENT_SINK.cb = cb


def set_inproc_runtime_env(values=None):
    """设置本线程 adapter 子进程应继承的请求现场；绝不修改全局 os.environ。"""
    if isinstance(values, dict):
        _INPROC_RUNTIME_ENV.values = {
            str(key): str(value) for key, value in values.items()
            if key and value is not None and str(value).strip()
        }
    else:
        _INPROC_RUNTIME_ENV.values = {}


def set_inproc_cancel_check(check=None):
    """设置本线程原生 CLI 的取消检查；_run_live 监控循环会安全终止该子进程组。"""
    _INPROC_CANCEL_CHECK.check = check if callable(check) else None
    _INPROC_CANCEL_CHECK.cancelled = False


def consume_inproc_cancelled() -> bool:
    """读取并清除本线程最近一次 _run_live 的取消事实。"""
    value = bool(getattr(_INPROC_CANCEL_CHECK, "cancelled", False))
    _INPROC_CANCEL_CHECK.cancelled = False
    return value


def _emit_inproc(ev: dict, event_context=None) -> bool:
    """有进程内 sink 就把事件直接回调·返 True(已消费·不再写 stderr)。"""
    cb = (event_context.sink if event_context is not None
          else getattr(_INPROC_EVENT_SINK, "cb", None))
    if cb is None:
        return False
    try:
        cb(ev)
    except Exception:
        pass
    return True

# 编排请求标记解析(全 adapter 共用):harness 在 final 末尾可写 <<LEGION_ORCH>>{json}<<END>>
# 请求 fork/handoff/need_peer/upgrade。解析出结构化 orchestration_request·并从 final 剥掉标记行
# (不让协议标记污染给用户的交付)。harness 不写=返回(final,None)零回归。
_ORCH_MARK_RE = _re.compile(r"<<LEGION_ORCH>>\s*(\{.*?\})\s*<<END>>", _re.DOTALL)


def _parse_orch_request(final: str):
    """→ (cleaned_final, orch_request|None)。严格解析优先；唯一末尾标记中，
    若仅外框未闭合，可读取已完整的 coordinate 值，不补写或推断任务内容。
    JSON 字符串里的协议示例只是数据；无效请求不扩大恢复范围。"""
    if not final or "<<LEGION_ORCH>>" not in final:
        return final, None
    m = _ORCH_MARK_RE.search(final)
    if not m:
        return final, None
    # Keep the existing invalid-frame display policy. The original native
    # reply/checkpoint remains the private source; never expose a new raw field.
    rejected = (final[:m.start()] + final[m.end():]).strip()
    payload = m.group(1)
    recovery = None
    try:
        # The regex locates a candidate, not its closing boundary: a task
        # string may legitimately contain `}<<END>>`.
        req, json_end = _json.JSONDecoder().raw_decode(final, m.start(1))
        frame_end = json_end
        while frame_end < len(final) and final[frame_end].isspace():
            frame_end += 1
        if not final.startswith("<<END>>", frame_end):
            return rejected, None
        frame_end += len("<<END>>")
    except (TypeError, ValueError):
        # 不做泛 JSON repair：只有一个完整 coordinate 值、明确终止标记和空余尾
        # 才能读出已有内容。残缺字符串/内层对象/其它命令/附加字段均不恢复。
        if (final.count("<<LEGION_ORCH>>") != 1 or final.count("<<END>>") != 1
                or final[m.end():].strip()):
            return rejected, None
        try:
            decoder = _json.JSONDecoder()
            inner = payload[1:].lstrip(" \t\r\n")
            key, end = decoder.raw_decode(inner)
            rest = inner[end:].lstrip(" \t\r\n")
            if key != "coordinate" or not rest.startswith(":"):
                return rejected, None
            body = rest[1:].lstrip(" \t\r\n")
            value, end = decoder.raw_decode(body)
            if not isinstance(value, dict) or body[end:].strip(" \t\r\n"):
                return rejected, None
            req = {"coordinate": value}
            frame_end = m.end()
            recovery = {
                "kind": "complete_coordinate_in_unclosed_outer_object",
                "payload_sha256": _hashlib.sha256(payload.encode()).hexdigest(),
                "value_inferred": False,
            }
        except Exception:
            return rejected, None
    if not isinstance(req, dict):
        return rejected, None
    cleaned = (final[:m.start()] + final[frame_end:]).strip()
    safe = {k: req[k] for k in ("fork", "handoff", "need_peer", "captain_bid",
                                 "handoff_captain", "coordinate", "need_approval", "upgrade")
            if k in req}
    if safe and recovery:
        safe["_protocol_recovery"] = recovery
    return cleaned, (safe or None)


# 产物路径检测(军团6家点名要的 artifact_ready:产物一生成就推给前端·不等整任务结束)。
# 匹配军团/工具产出的持久文件:~/.meyo/legion/{media,outputs}/... 或 /media/... 或 outputs/...
# 常见交付类型(图/视频/音频/文档/表格/网页/代码)。只推路径事实·前端据类型渲染卡片/预览。
_ARTIFACT_RE = _re.compile(
    r"(?:/media/|/outputs/|\.meyo/legion/(?:media|outputs)/|(?:^|[\s\"'(])outputs/)"
    r"[^\s\"'()]+?\.(png|jpe?g|gif|webp|svg|mp4|mov|webm|mp3|wav|m4a|pdf|xlsx|xls|csv|docx|pptx|html?|md|zip)",
    _re.IGNORECASE)
_ARTIFACT_SEEN: set = set()   # 旧三参直接调用的兼容状态；_run_live 使用独立上下文。
# 从一行 CLI 输出里提炼"用户该看见的过程"(工具调用/访问URL/状态)。内部思考链不外发
# (刚修的 think 泄漏同源:过程流也不该漏 chain-of-thought·只给可见动作)。
_EV_URL_RE = _re.compile(r"https?://[^\s\"'<>)\]]+")
_EV_THINK_LINE = _re.compile(r"<\s*/?\s*(think|thinking|reasoning|thought)\s*>", _re.IGNORECASE)


def _phase_of(text: str) -> str:
    """把【已发生的真实事件类型】投影成用户看得懂的阶段(展示层·不影响 harness 行为)。

    不是关键词猜用户意图/工具路由(control);这里只看 _friendly_stream_event 自己刚生成的固定中文
    动作标签,把"访问网页"归到"搜集证据"、"改动文件"归到"产出文件"。像快递把真实扫描
    记录归成"运输中/派送中",只改善用户可读性,不替执行者决定下一步。
    """
    s = str(text or "")
    if s.startswith("已启动"):
        return "理解任务"
    if s.startswith(("搜索", "检索", "访问网页")):
        return "搜集证据"
    if s.startswith(("运行命令", "改动文件", "读写文件", "调用工具")):
        return "执行产出"
    if s.startswith("工具返回"):
        return "处理结果"
    if s.startswith("正在组织答案"):
        return "整理交付"
    if s.startswith(("遇到错误", "本轮失败")):
        return "自救处理中"
    return ""


def _native_tool_event(obj: dict, tag: str) -> dict | None:
    """Project native tool records onto the shared lifecycle without guessing intent."""
    event_type = str(obj.get("type") or "")
    item = obj.get("item") if isinstance(obj.get("item"), dict) else {}
    item_type = str(item.get("type") or item.get("item_type") or "")
    tool = ""
    call_id = ""
    arguments = {}
    status = ""
    ok = None
    result_brief = ""
    source_ref = {"kind": "native_tool", "source_harness": tag}
    if tag == "jcode" and event_type == "tool_done":
        tool = str(obj.get("name") or "")
        call_id = str(obj.get("id") or "")
        if not tool or not call_id:
            return None
        ok = (obj["error"] is None) if "error" in obj else None
        status = "succeeded" if ok is True else "failed" if ok is False else "completed"
        result_brief = str(obj.get("output") or obj.get("error") or "")[:500]
    elif event_type == "assistant":
        message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        block = next((part for part in message.get("content") or []
                      if isinstance(part, dict) and part.get("type") == "tool_use"), None)
        if not block:
            return None
        tool = str(block.get("name") or "tool")
        call_id = str(block.get("id") or block.get("tool_use_id") or "")
        arguments = block.get("input") if isinstance(block.get("input"), dict) else {}
        status = "started"
    elif event_type == "user":
        message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        block = next((part for part in message.get("content") or []
                      if isinstance(part, dict) and part.get("type") == "tool_result"), None)
        if not block:
            return None
        call_id = str(block.get("tool_use_id") or block.get("id") or "")
        # Claude tool_result 不重复携带工具名；用 call_id 关联 start/end，未知名称保持诚实。
        tool = str(block.get("name") or block.get("tool_name") or "")
        failed = bool(block.get("is_error") or block.get("isError"))
        status, ok = ("failed", False) if failed else ("succeeded", True)
        if (block.get("status") == "returned"
                and block.get("is_error") is None and block.get("isError") is None):
            status, ok = "returned", None  # 明确未知，不改变旧原生协议的缺省语义。
        result_brief = str(block.get("content") or "")[:500]
    elif event_type in ("item.started", "item.completed", "item.updated") and item_type in {
            "command_execution", "mcp_tool_call", "web_search"}:
        tool = str(item.get("tool") or item.get("name") or item_type)
        call_id = str(item.get("id") or item.get("call_id") or
                      _hashlib.sha256((tool + str(item.get("command") or item.get("query") or "")).encode()).hexdigest()[:16])
        arguments = {key: item.get(key) for key in ("command", "query", "server", "tool") if item.get(key) is not None}
        if event_type == "item.started":
            status = "started"
        elif event_type == "item.completed":
            exit_code = item.get("exit_code")
            # item.completed 只表示终态；原生 failed/declined 不能被空退出码盖成成功。
            native_status = str(item.get("status") or "")
            ok = (native_status not in {"failed", "declined"}
                  and exit_code in (None, 0) and not item.get("error"))
            status = "succeeded" if ok else "failed"
            result_brief = str(item.get("aggregated_output") or item.get("result") or item.get("error") or "")[:500]
        else:
            return None
    else:
        return None
    return {"t": "tool_started" if status == "started" else "tool_end", "tool": tool,
            "call_id": call_id or f"{tag}-{tool or 'tool'}-{_hashlib.sha256(_json.dumps(arguments, sort_keys=True, default=str).encode()).hexdigest()[:12]}",
            "status": status, "ok": ok, "args_brief": _json.dumps(arguments, ensure_ascii=False)[:500],
            "result_brief": result_brief, "source_ref": source_ref,
            "selection": {"source": "native_harness", "candidate_verified": "unreported"}}


def _friendly_stream_event(obj: dict):
    """把 cc/codex 的 stream-json 事件对象提炼成过程流一行(不甩整坨原始 JSON)。

    用户要求(2026-08-28):动作【标签】用中文(调用工具/访问网页/运行命令…),但【具体细节】
    ——网址 URL、文件路径、命令、查询词、pattern——【英文原样保留、越细越好】,不翻译不缩写。
    只有内部思考链不外发(同 _strip_reasoning 产品判断)。返回 (文本, urls) 或 None(系统噪声)。
    """
    t = obj.get("type")
    if t == "system":
        if obj.get("subtype") == "init":
            cwd = obj.get("cwd") or ""
            return (f"已启动，读取任务" + (f"（cwd={cwd}）" if cwd else "") , [])
        return None
    if t == "assistant":
        msg = obj.get("message", {})
        parts, urls = [], []
        for b in msg.get("content", []):
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                name = str(b.get("name", "tool"))   # 工具名英文原样(WebSearch/Bash/Read…)
                inp = b.get("input", {}) or {}
                # 每类工具展示最能说明"在干啥"的真实参数·英文原样·放宽长度(细节越多越好)
                if inp.get("url"):
                    detail = str(inp["url"]); label = "访问网页"
                elif inp.get("command"):
                    detail = str(inp["command"]); label = "运行命令"
                elif inp.get("query"):
                    detail = str(inp["query"]); label = "搜索"
                elif inp.get("file_path") or inp.get("path"):
                    detail = str(inp.get("file_path") or inp.get("path")); label = "读写文件"
                elif inp.get("pattern"):
                    detail = str(inp["pattern"]); label = "检索"
                else:
                    detail = ""; label = "调用工具"
                urls += _EV_URL_RE.findall(detail)
                parts.append(f"{label} {name}" + (f": {detail[:500]}" if detail else ""))
            elif b.get("type") == "text" and b.get("text", "").strip():
                parts.append("正在组织答案…")
        if parts:
            return (" · ".join(dict.fromkeys(parts)), urls[:8])
        return None
    if t == "user":
        # 工具返回:给一小段真实结果预览(英文原样)让用户看到"拿到了什么"·不放全量
        msg = obj.get("message", {})
        for b in (msg.get("content", []) if isinstance(msg, dict) else []):
            if isinstance(b, dict) and b.get("type") == "tool_result":
                c = b.get("content")
                txt = ""
                if isinstance(c, str):
                    txt = c
                elif isinstance(c, list):
                    txt = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                txt = txt.strip().replace("\n", " ")
                return (f"工具返回: {txt[:300]}" if txt else "工具返回结果",
                        _EV_URL_RE.findall(txt)[:8])
        return ("工具返回结果", [])
    if t == "result":
        return None   # 最终结果由 final 单独给·过程流不重复
    # ── codex exec --json 事件(schema: codex-rs/exec/src/exec_events.rs)─────────
    # 外层 {"type":"item.started|item.completed|turn.*|thread.*","item":{"type":<item_type>,...}}
    if t in ("item.started", "item.completed", "item.updated"):
        it = obj.get("item") or {}
        it_type = it.get("type") or it.get("item_type")
        if it_type == "command_execution":
            # 依据 codex event_processor_with_human_output.rs:119-161——命令完成显示 exit,
            # 失败(exit≠0)还打 aggregated_output(真实报错)让人看清哪挂了。对齐"越细越好"。
            cmd = str(it.get("command") or "")
            # 命令人话化(用户 2026-08-29:过程要详细但别是噪声·"/bin/zsh -lc python3.12 '/长路径/'"
            # 对用户无意义)。剥 shell 包装(/bin/zsh -lc "…")·把绝对路径压成文件名·让用户看懂在跑什么。
            _c = _re.sub(r'^/bin/\w*sh\s+-l?c\s+["\']?', '', cmd).strip('"\' ')
            _c = _re.sub(r'(/[^\s"\']+/)([^\s"\'/]+)', r'\2', _c)   # 绝对路径→只留文件名
            _c = _c[:120]
            ec = it.get("exit_code")
            tail = f" (exit={ec})" if ec is not None and t == "item.completed" else ""
            out = ""
            if ec not in (None, 0) and t == "item.completed":
                agg = str(it.get("aggregated_output") or "").strip().replace("\n", " ")
                if agg:
                    out = f" → {agg[:300]}"
            return (f"🔧 执行：{_c}{tail}{out}", []) if _c else None
        if it_type == "file_change":
            chs = it.get("changes") or []
            paths = ", ".join(str(c.get("path")) for c in chs if isinstance(c, dict))[:500]
            return (f"改动文件: {paths}", []) if paths else ("改动文件", [])
        if it_type == "mcp_tool_call":
            server, tool = str(it.get("server") or ""), str(it.get("tool") or "")
            return (f"调用工具 {server}/{tool}", [])
        if it_type == "web_search":
            q = str(it.get("query") or "")
            return (f"搜索 web: {q[:300]}", _EV_URL_RE.findall(q)[:8]) if q else None
        if it_type == "agent_message":
            # 只在完成事件标一次"在输出答案"·正文由 final 完整给·不逐字重复
            return ("正在组织答案…", []) if t == "item.completed" else None
        if it_type in ("reasoning",):
            return None   # 内部思考不外发(同 _strip_reasoning)
        if it_type == "error":
            return (f"遇到错误: {str(it.get('message') or '')[:300]}", [])
        return None
    if t == "turn.failed":
        err = (obj.get("error") or {}).get("message") if isinstance(obj.get("error"), dict) else obj.get("error")
        return (f"本轮失败: {str(err or '')[:300]}", []) if err else None
    if t in ("thread.started", "turn.started", "turn.completed"):
        return None   # codex 生命周期噪声·不展示
    return None


# 附加展示节流；真实工具生命周期是运行事实，不随叙述预算静默消失。
# 模块级状态仅供旧三参直接调用；实际 CLI 每次调用使用自己的 _RunEventContext。
_EV_STATE = {"count": 0, "last": ""}
_EV_MAX = int(os.environ.get("LEGION_EV_MAX", "300"))   # 单家单次附加叙述/candidate 展示预算


def _emit_harness_event(tag: str, stream: str, line: str, event_context=None) -> None:
    """旧三参接口保持；CLI 两个 reader 只同步本次调用的状态，不做全局 SSE 排序。"""
    if event_context is None:
        return _emit_harness_event_unlocked(tag, stream, line)
    with event_context.lock:
        return _emit_harness_event_unlocked(tag, stream, line, event_context)


def _emit_harness_event_unlocked(tag: str, stream: str, line: str, event_context=None) -> None:
    """把一行真实过程输出提炼成结构化事件,以约定前缀写真 stderr(供上层解析成 cb)。

    只发"可见动作"(工具/URL/状态摘要),不发内部思考;每行截断防淹没。失败静默(退回刷心跳)。
    叙述/candidate 达到 _EV_MAX 后收敛；真实工具开始/返回仍保留。
    连续完全相同的事件行去重，私密过滤及每项内容截断保持不变。
    """
    raw = (line or "").strip()
    if not raw:
        return
    if tag == "hermes" and stream == "stdout" and "hermes_mailbox_receipt" in raw:
        # Private control facts must not be dropped by UI budgets/thinking
        # filtering, counted as tools, or broadcast when there is no owner sink.
        try:
            frame = _json.loads(raw) if len(raw) <= 4096 else None
        except (ValueError, TypeError):
            frame = None
        if isinstance(frame, dict) and frame.get("type") == "hermes_mailbox_receipt":
            receipt = frame.get("receipt") if set(frame) == {"type", "receipt"} else None
            _emit_inproc({"t": "hermes_mailbox_receipt", "receipt": receipt}, event_context)
            return
    event_state = event_context.state if event_context is not None else _EV_STATE
    artifact_seen = event_context.artifact_seen if event_context is not None else _ARTIFACT_SEEN
    structured = raw.startswith(("{", "[")) and '"type"' in raw
    markers = list(_EV_THINK_LINE.finditer(raw))
    depth = (event_context.thinking_depth.get(stream, 0)
             if event_context is not None else 0)
    if depth or markers:
        # Plain-text thinking blocks may span lines. Keep streams independent:
        # hidden stdout must not suppress an actual stderr tool event.
        if event_context is not None and not structured:
            for marker in markers:
                depth = max(0, depth - 1) if _re.match(r"<\s*/", marker.group(0)) else depth + 1
            event_context.thinking_depth[stream] = depth
        _sys.stderr.write("."); _sys.stderr.flush()
        return
    obj = None
    artifact_source = raw
    if structured:
        try:
            obj = _json.loads(raw)
            friendly = _friendly_stream_event(obj)
            native_tool = _native_tool_event(obj, tag)
            if friendly is None and native_tool is None:
                _sys.stderr.write("."); _sys.stderr.flush()
                return
            if obj.get("type") == "assistant":
                # A message can contain both private thinking and a real tool
                # call. Scan artifact paths only in its visible content blocks.
                content = (obj.get("message") or {}).get("content") or []
                artifact_source = _json.dumps([
                    part for part in content if isinstance(part, dict)
                    and part.get("type") in ("text", "tool_use")
                ], ensure_ascii=False)
        except Exception:
            # Malformed native JSON stays in the internal capture, not the UI.
            _sys.stderr.write("."); _sys.stderr.flush()
            return
    if obj is None:
        # 未解析的正文不是公开事件；完整原文由 _run_live capture 留给原生解析器。
        return
    # artifact_ready(军团6家点名·用户体验最直接):产物一出现就【立刻】推给前端·不等整任务
    # 结束、不受下面过程事件节流限制(产物是用户最想立刻看到的·图/视频/PDF一生成就能点)。
    # 只推路径事实(context)·前端按扩展名渲染卡片/预览。per-run 去重防同一产物重复推。
    for _m in _ARTIFACT_RE.finditer(artifact_source):
        _path = _m.group(0).lstrip("\"'( \t")
        if _path in artifact_seen:
            continue
        artifact_seen.add(_path)
        _kind = _m.group(1).lower()
        # 给前端可直接访问的URL:无论输出是绝对 ~/.meyo/legion/media|outputs 路径还是
        # 相对 /media|outputs,都归一到服务HTTP路由。前端无需猜本机路径(它只知道URL)。
        _url = ""
        for _prefix in ("/media/", "/outputs/"):
            if _prefix in _path:
                _url = _prefix + _path.split(_prefix, 1)[1]
                break
        _av = {"t": "artifact_ready", "agent": tag, "path": _path,
               "url": _url or _path, "name": os.path.basename(_path), "ext": _kind}
        if _emit_inproc(_av, event_context):
            continue  # 进程内直调:事件已回调·不写 stderr
        try:
            _sys.stderr.write(_LEGION_EV_PREFIX + _json.dumps(_av, ensure_ascii=False) + "\n")
            _sys.stderr.flush()
        except Exception:
            pass
    # 只收敛附加展示。否则预算耗尽会连已开始工具的返回也吞掉，
    # 外层看起来一直执行旧工具，下一轮拿到的动作现场也停在旧位置。
    if event_state["count"] >= _EV_MAX and native_tool is None:
        _sys.stderr.write("."); _sys.stderr.flush()
        return
    # 连续完全相同的行去重(某些 CLI 重复打同一进度)
    if raw == event_state["last"]:
        _sys.stderr.write("."); _sys.stderr.flush()
        return
    event_state["last"] = raw
    # stream-json 结构化事件(cc/codex):提炼成人话摘要·不甩原始 JSON 给用户
    if obj is not None:
        try:
            text, urls = friendly or (
                f"调用工具 {native_tool.get('tool')}" if native_tool and native_tool.get("t") == "tool_started"
                else str((native_tool or {}).get("result_brief") or "工具已返回"), [])
            ev = {"t": "harness_step", "agent": tag, "stream": stream, "x": text}
            if native_tool:
                native_tool.update({"agent": tag, "stream": stream, "x": text})
                # 原生 Harness 只暴露最终选择，不暴露完整搜索池。开始调用前如实发一条
                # observed_selection 候选事实，让链路具备 candidate→start→end，同时绝不伪造
                # 它还考虑过哪些工具。调用方/前端可据此区分目录检索候选与原生已选工具。
                if native_tool.get("t") == "tool_started" and event_state["count"] < _EV_MAX:
                    _selected_tool = str(native_tool.get("tool") or "tool")
                    _candidate = {
                        "t": "weapon_candidates", "agent": tag, "stream": stream,
                        "query_hash": str(native_tool.get("call_id") or ""),
                        "pool": 1, "returned": 1, "observed_selection": True,
                        "candidates": [{"id": _selected_tool, "name": _selected_tool,
                                        "kind": "native_tool", "source": tag,
                                        "runnable": True, "rank": 1}],
                        "ranking_context": {"source": "native_harness",
                                            "message": "成员已自主选择原生工具"},
                        "x": f"已选择原生工具 {_selected_tool}",
                    }
                    event_state["count"] += 1
                    if not _emit_inproc(_candidate, event_context):
                        _sys.stderr.write(_LEGION_EV_PREFIX + _json.dumps(
                            _candidate, ensure_ascii=False) + "\n")
                        _sys.stderr.flush()
                ev = native_tool
            if urls:
                ev["urls"] = urls
            # 阶段语义(军团自审 #2·phase):从【事件本身的固有类型】如实派生"现在在哪一步",
            # 不是猜 harness 意图——访问网页/搜索=搜集证据·运行命令/改文件=产出·工具返回=处理·
            # 组织答案=产出交付。给用户"卡在哪一步"的可读性(用户中心·过程可感),harness 行为零变。
            _ph = _phase_of(text)
            if _ph:
                ev["phase"] = _ph
            if native_tool is None:
                event_state["count"] += 1
            if _emit_inproc(ev, event_context):
                return  # 进程内直调:事件已回调·不写 stderr
            _sys.stderr.write(_LEGION_EV_PREFIX + _json.dumps(ev, ensure_ascii=False) + "\n")
            _sys.stderr.flush()
            return
        except Exception:
            _sys.stderr.write("."); _sys.stderr.flush()
            return  # Never turn rejected native JSON into raw visible text.


_ROUTER_PROBE_CACHE = {"ok": None, "at": 0.0, "url": ""}


def _probe_local_router(base_url: str) -> bool | None:
    """探本机 cc-router(ANTHROPIC_BASE_URL)真实可用性·返回 True/False·异常返回 None(当作可用)。

    ★只探【/v1/messages 真请求】不探 /health——2026-09-02 真跑坐实:router 半死时 /health 照样
    {"status":"ok"}·只有真 POST /v1/messages 才暴露(30s+ 无响应)。且【只有 2xx 才算活】:
    4xx(如 invalid model)只说明 HTTP 层活着·上游可能已断(真请求照样卡 35s+)。宁可保守判死·
    自愈换家会兜底(误判活=53s 空等拖死整单·误判死=换家多几秒·代价不对称)。
    探测带 3s 超时·结果缓存 60s(别每单都打)。返回 False = router 死·调用方降级。
    """
    import time as _pt
    now = _pt.time()
    if (_ROUTER_PROBE_CACHE["url"] == base_url
            and _ROUTER_PROBE_CACHE["ok"] is not None
            and now - _ROUTER_PROBE_CACHE["at"] < 60):
        return _ROUTER_PROBE_CACHE["ok"]
    ok = None
    try:
        import urllib.request as _ur
        _body = _json.dumps({"model": "claude-sonnet-4-5", "max_tokens": 1,
                             "messages": [{"role": "user", "content": "1"}]}).encode()
        _hdr = {"Content-Type": "application/json"}
        # 带上 env 里的 auth(健康 router 缺 key 会 401·被误判死):x-api-key 和 Bearer 都带上
        _tok = str(os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY") or "")
        if _tok:
            _hdr["x-api-key"] = _tok
            _hdr["Authorization"] = "Bearer " + _tok
        _req = _ur.Request(base_url + "/v1/messages", data=_body, headers=_hdr)
        # 7s 超时:网关真实推理有 2.5-4s 物理底(健康 router 要转发到网关)·3s 会误杀慢而健康的
        _r = _ur.urlopen(_req, timeout=7)
        ok = 200 <= int(getattr(_r, "status", 200) or 200) < 300
    except _ur.HTTPError:
        ok = False          # 4xx/5xx = 上游不通或配置坏·保守判死(见 docstring)
    except Exception:
        ok = False          # 超时/拒绝 = 死
    _ROUTER_PROBE_CACHE.update({"ok": ok, "at": now, "url": base_url})
    return ok


# ccr 真自愈(用户 2026-09-02 钉死:不能只补位,得自愈,彻底修复):探死自动重启 cc-router 再重探。
# 旧病:router 半死->error 换家(纯补位)->cc 这单永远交不出+router 一直死。真根因两层:
# ①router 进程卡(重启即修) ②LOG:true 时 debug 日志 35min 涨 588MB(实测 624MB 目录)
# ·磁盘 IO 炸弹拖垮系统(铁律0:谁挡底线降级谁)。冷却 120s 防并发重启风暴;救不活才交接力兜底。
_CCR_SELFHEAL_STATE = {"ts": 0.0}


def _ccr_hard_kill_stale() -> None:
    """硬清所有残留的 ccr 服务进程(pid 文件+ps 扫描)·确保 ccr start 时端口干净。

    真跑坐实(2026-09-02 本机实测 3 次):ccr restart 的 stop 只发一次 SIGTERM 不等死透→旧进程
    占着 3456 成孤儿(新进程绑不上端口自杀·pid 文件却指向已死的新进程)→下次 restart 杀空 pid
    →自愈死循环永远救不活。硬清=SIGTERM→2s 宽限→SIGKILL 确认死透→删 pid 文件让 start 干净起步。
    只匹配 claude-code-router/dist/cli.js start(用户自己的 ccr code 会话是 bin/ccr code·不含
    此路径·绝不误杀)。任何异常静默(上游断链铁律)。"""
    import signal as _sig, subprocess as _sp, time as _t2
    pids = set()
    try:
        _pf = os.path.expanduser("~/.claude-code-router/.claude-code-router.pid")
        pids.add(int(open(_pf).read().strip()))
    except Exception:
        pass
    try:
        _out = _sp.run(["ps", "-eo", "pid,command"], capture_output=True, text=True, timeout=5).stdout or ""
        for _line in _out.splitlines():
            # 两种形态都要匹配:直跑 dist/cli.js start(常规) 与 node bin/ccr start
            # (fire-and-forget Popen 起 wrapper 形式·2026-09-02 实测它占端口却不含 dist/cli.js)。
            # ccr code 会话以 code 结尾·不被 start 后缀误伤。
            if _line.rstrip().endswith(" start") and (
                    "claude-code-router/dist/cli.js" in _line
                    or _line.rstrip().endswith("bin/ccr start")):
                try:
                    pids.add(int(_line.strip().split(None, 1)[0]))
                except Exception:
                    pass
    except Exception:
        pass
    def _gone(_p):
        try:
            os.kill(_p, 0)
            return False
        except Exception:
            return True

    for _pid in pids:
        try:
            os.kill(_pid, _sig.SIGTERM)
        except Exception:
            pass
    # 快死确认(2026-09-02 提速):0.1s 步进探死·0.8s 内没死透就 SIGKILL——半死 router 没有
    # 值得等的在途流量·旧版固定 sleep(2) 是自愈 9.4s 的头号大头。SIGKILL 后 LISTEN 端口
    # 内核即时释放(无 TIME_WAIT)·start 可立即绑。
    for _ in range(8):
        if all(_gone(_p) for _p in pids):
            break
        _t2.sleep(0.1)
    for _pid in pids:
        if not _gone(_pid):
            try:
                os.kill(_pid, _sig.SIGKILL)
            except Exception:
                pass
    try:
        os.unlink(os.path.expanduser("~/.claude-code-router/.claude-code-router.pid"))
    except Exception:
        pass


def _ccr_selfheal_restart(router_url: str) -> bool:
    """探死自动重启 cc-router 再重探:轮转失控日志(>200MB)->硬清旧进程->ccr start->活=True。"""
    import subprocess as _sp, time as _t2, glob as _g
    if _t2.time() - _CCR_SELFHEAL_STATE["ts"] < 120.0:
        _ROUTER_PROBE_CACHE.update({"ok": None, "at": 0.0, "url": ""})
        return _probe_local_router(router_url) is True
    _CCR_SELFHEAL_STATE["ts"] = _t2.time()
    try:
        try:
            for _lp in _g.glob(os.path.expanduser("~/.claude-code-router/logs/ccr-*.log")):
                if os.path.getsize(_lp) > 200 * 1024 * 1024:
                    os.replace(_lp, _lp + ".old-" + str(int(_t2.time())))
        except Exception:
            pass
        _ccr_bin = _sh.which("ccr") or "/Users/xuan/.npm-global/bin/ccr"
        try:
            _ccr_hard_kill_stale()
            # fire-and-forget:ccr start 在 capture 管道下不返回(daemon 化挂住·实测 60s 超时
            # 但服务已起)·改 Popen+独立会话不等返回·由下面循环重探判定真实起没起。
            _sp.Popen([_ccr_bin, "start"], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                      stdin=_sp.DEVNULL, start_new_session=True)
        except Exception:
            pass
        # 两段式提速(2026-09-02 v2·真杀实测旧版 11.7s):旧版 6x(0.3s+全量真探)在 node
        # 半就绪窗口每次挂到 7s 超时。改:①TCP 探绑口(0.05s 步进·连接拒绝=秒败不耗真探)
        # ②绑上后 settle 0.4s③只做一次真请求验证(物理底=网关推理 2-4s·不能省)④失败再补。
        import socket as _sk
        try:
            _hp = router_url.split("//", 1)[-1].split("/")[0]
            _h, _, _p = (_hp.partition(":") + ("3456",))[:3] if ":" in _hp else (_hp, "", "3456")
            _bound = False
            for _ in range(100):  # 0.05s x 100 = 5s 绑口上限
                try:
                    with _sk.create_connection((_h, int(_p or 3456)), timeout=0.2):
                        _bound = True
                        break
                except Exception:
                    _t2.sleep(0.05)
            if _bound:
                _t2.sleep(0.4)  # 绑口→HTTP 就绪 settle
                _ROUTER_PROBE_CACHE.update({"ok": None, "at": 0.0, "url": ""})
                if _probe_local_router(router_url) is True:
                    return True
                _t2.sleep(0.5)  # 仍半就绪→落回兜底轮询
        except Exception:
            pass
        for _ in range(3):
            _t2.sleep(0.3)
            _ROUTER_PROBE_CACHE.update({"ok": None, "at": 0.0, "url": ""})
            if _probe_local_router(router_url) is True:
                return True
        _ROUTER_PROBE_CACHE.update({"ok": None, "at": 0.0, "url": ""})
        return _probe_local_router(router_url) is True
    except Exception:
        return False


def _gateway_no_proxy(env: dict) -> dict:
    """把网关域名加进子进程 env 的 NO_PROXY(网关内网直连·不走死代理)。持久化根治见 _run_live 内注释。
    幂等·只追加不覆盖原有 NO_PROXY 条目(github 等外网仍走原代理)。"""
    try:
        # ★ 完全对齐 legion_core._gateway_env(已验证做法·2026-09-01 loop 真跑揪出两处不一致导致
        #   cc "Connection refused"):① NO_PROXY 除网关域名外还加 127.0.0.1,localhost(本地服务/bridge
        #   连接一律直连不走代理)② 无条件 pop ALL_PROXY(socks5h://127.0.0.1 死代理·NO_PROXY 对
        #   socks ALL_PROXY 不可靠·必须 pop)。HTTP(S)_PROXY 保留给外网 github(受 NO_PROXY 约束可靠)。
        hosts = ["aigc.sankuai.com", "127.0.0.1", "localhost"]
        try:
            _gw = _UNIFIED_GATEWAY.split("//", 1)[-1].split("/", 1)[0]
            if _gw and _gw not in hosts:
                hosts.append(_gw)
        except Exception:
            pass
        for key in ("NO_PROXY", "no_proxy"):
            parts = [p.strip() for p in env.get(key, "").split(",") if p.strip()]
            for h in hosts:
                if h not in parts:
                    parts.append(h)
            env[key] = ",".join(parts)
        for _pk in ("ALL_PROXY", "all_proxy"):
            env.pop(_pk, None)
    except Exception:
        pass  # 任何异常绝不阻断真执行(上游断链铁律)
    return env


def _run_live(cmd, timeout, env=None, cwd=None, input=None, tag="", *, task_run=False,
              native_stdin=None, runtime_activity_fd=None):
    """替代 _sp.run(capture_output=True,text=True):流式读子进程输出,每行往真stderr打一个
    进度标记刷新 harness_worker 心跳(让父层idle检测知道'在推进'·治hermes/cc/codex被195s误杀)。

    根因:各adapter原用 _sp.run 一次性capture子进程输出→worker层完全静默→只能靠CPU检测,
    而这些家等网关长响应时CPU也低→被idle判卡死。改流式转发真实输出=真实进展信号(学codex/
    deepseek事件回流·不是无脑心跳)。返回同CompletedProcess的.returncode/.stdout/.stderr。
    完整输出仍全量capture到buf(供adapter解析final)·只是额外把'有新行'这件事漏给worker。
    """
    # stdin 隔离(根因修 2026-08-28):不传 input 时给子进程独立的 DEVNULL·而非继承父 stdin(None)。
    # 并行派多家时(6家线程各起subprocess),codex exec 等 CLI 会探测 stdin 想读额外输入,继承同一个
    # 父 stdin 会互相争抢→"Failed to read prompt from stdin: Resource temporarily unavailable(EAGAIN·
    # os error 35)"→codex 整单失败(prompt 明明已走命令行arg传了)。给每个子进程干净的空 stdin(读到
    # EOF 不争抢)=让它们都在"一样的干净位置"跑·并行也稳。input 有值时仍走 PIPE 写入。
    if native_stdin is not None and (input is not None or os.name == "nt"):
        raise ValueError("live native stdin requires a distinct POSIX pipe")
    if runtime_activity_fd is not None:
        import stat
        if (not task_run or type(runtime_activity_fd) is not int
                or not stat.S_ISFIFO(os.fstat(runtime_activity_fd).st_mode)):
            raise ValueError("runtime activity requires an owned task pipe")
        os.set_blocking(runtime_activity_fd, False)
    live_initial = native_stdin.initial_message() if native_stdin is not None else None
    if native_stdin is not None and (
            not isinstance(live_initial, (tuple, list)) or len(live_initial) != 2
            or not isinstance(live_initial[0], str) or not live_initial[0]
            or not isinstance(live_initial[1], bytes) or not live_initial[1].endswith(b"\n")):
        raise ValueError("native initial input must be a complete identified JSONL frame")
    _stdin = _sp.PIPE if input is not None or native_stdin is not None else _sp.DEVNULL
    # 网关直连(持久化根治·用户2026-09-01"保证持久化运行不错"):部署机若设了死代理
    # (HTTP_PROXY 指向没起的代理),各家 CLI 调 aigc.sankuai.com 会走死代理→卡到超时→整家fail。
    # 一处统一给所有家的子进程 env 把网关域名加进 NO_PROXY(内网服务本就直连·保留原代理供外网),
    # 覆盖每个 adapter(它们都 env=dict(os.environ))·部署机无论有无死代理·各家网关调用都稳。
    env = dict(env) if env is not None else dict(os.environ)
    # 进程内 adapter 的 ctx 不在 server 全局 os.environ 中；按本线程覆盖到 native CLI，随后
    # CLI 内的 Bash/shell 子进程自然继承。thread-local 隔离并发请求，隔离 worker 未设置则零变化。
    env.update(getattr(_INPROC_RUNTIME_ENV, "values", {}) or {})
    env = _gateway_no_proxy(env)
    event_context = _RunEventContext(getattr(_INPROC_EVENT_SINK, "cb", None))
    _popen_kw = {"start_new_session": True} if os.name != "nt" else {}
    proc = _sp.Popen(cmd, stdout=_sp.PIPE, stderr=_sp.PIPE, text=True, bufsize=1,
                     env=env, cwd=cwd, stdin=_stdin, **_popen_kw)
    # 崩溃安全孤儿回收(2026-09-02 根修):本 CLI 与 worker 同组,worker 被单独 SIGKILL(rc-9)
    # 时同组残留无人收 → 登记注册表(owner=本进程),父层 kill_owned_by/sweep 兜底。
    _process_scope = None
    try:
        _reg = _load_proc_registry()
        if _reg is not None:
            _process_scope = _reg.NativeProcessScope(
                proc.pid, owner=os.getpid(),
                meta={"kind": "native-cli", "tag": str(tag)})
    except Exception:
        pass
    out_buf, err_buf = [], []
    stdin_error = ""
    stdin_pending = []
    if native_stdin is not None:
        os.set_blocking(proc.stdin.fileno(), False)
        stdin_pending.append((live_initial[0], memoryview(live_initial[1])))

    def _close_native_stdin():
        if native_stdin is not None and proc.stdin is not None and not proc.stdin.closed:
            try:
                proc.stdin.close()
            except OSError:
                pass

    def _pump_native_stdin():
        """Use the existing monitor loop; queueing is not model progress or adoption."""
        nonlocal stdin_error
        if native_stdin is None or proc.stdin.closed:
            return
        if stdin_error:
            _close_native_stdin()
            return
        try:
            if not stdin_pending:
                for message_id, data in native_stdin.pending_messages():
                    if not isinstance(data, bytes) or not data.endswith(b"\n"):
                        raise ValueError("native input must be one complete JSONL frame")
                    stdin_pending.append((message_id, memoryview(data)))
            if getattr(native_stdin, "error", ""):
                raise RuntimeError("native mailbox delivery unavailable")
            import select
            fd = proc.stdin.fileno()
            while stdin_pending and select.select([], [fd], [], 0)[1]:
                message_id, remaining = stdin_pending[0]
                try:
                    written = os.write(fd, remaining[:65536])
                except BlockingIOError:
                    break
                if written <= 0:
                    raise BrokenPipeError("native input pipe closed")
                remaining = remaining[written:]
                if remaining:
                    stdin_pending[0] = (message_id, remaining)
                else:
                    stdin_pending.pop(0)
                    native_stdin.mark_sent(message_id)
            if not stdin_pending and native_stdin.should_close():
                _close_native_stdin()
        except Exception as exc:
            # A supplemental-input failure must be visible without leaking the
            # message body or discarding already produced primary output.
            stdin_error = type(exc).__name__
            _close_native_stdin()
    limits = [float(env.get("MEYO_TASK_DEADLINE") or 0)]
    if timeout and timeout > 0:
        limits.append(_t.monotonic() + float(timeout))
    deadline = min((x for x in limits if x > 0), default=0) if task_run else (
        _t.monotonic() + float(timeout) if timeout and timeout > 0 else 0)
    lease = RuntimeLease(_TASK_IDLE_S, deadline)

    def activity(event=None):
        lease.observe(event)
        # 不经过 UI sink；worker 保留此协议，父层消费后不对外广播。
        # 无内容的活动事实也必须有换行，不能让旧的 '.' 心跳卡在 readline 缓冲里。
        if env.get("MEYO_RUNTIME_INPROC") != "1":
            value = {"t": "runtime_activity", **(event or {})}
            _sys.stderr.write(_LEGION_EV_PREFIX + _json.dumps(value) + "\n")
            _sys.stderr.flush()

    transport_buffer = b""
    transport_open = runtime_activity_fd is not None

    def _pump_transport_activity():
        nonlocal transport_buffer, transport_open
        if not transport_open:
            return
        try:
            chunk = os.read(runtime_activity_fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            transport_open = False
            return
        if not chunk:
            transport_open = False
            return
        transport_buffer += chunk
        while b"\n" in transport_buffer:
            line, transport_buffer = transport_buffer.split(b"\n", 1)
            try:
                event = _json.loads(line) if len(line) <= 4096 else None
                if (not isinstance(event, dict) or set(event) != {"call_id", "phase", "budget_s"}
                        or not isinstance(event["call_id"], str)
                        or not _re.fullmatch(r"[A-Za-z0-9_-]{1,128}", event["call_id"])
                        or event["phase"] not in ("start", "bytes", "end", "error")
                        or type(event["budget_s"]) not in (int, float)
                        or not 0 < event["budget_s"] < float("inf")):
                    continue
                activity(event)  # Same inner lease + existing private outer-worker channel.
            except (ValueError, TypeError, UnicodeError):
                pass
        if len(transport_buffer) > 4096:
            transport_buffer = b""

    # worker 把 sys.stderr 重定向进 _ActiveIO·adapter在其上下文内运行·故写 sys.stderr 即刷新心跳

    def captured_lines(stream, buf):
        # readline 会把未换行的流式输出憋住，隐藏正文仍须及时提供活动事实。
        import codecs
        decoder = codecs.getincrementaldecoder(stream.encoding or "utf-8")(errors="replace")
        pending = ""
        while True:
            chunk = os.read(stream.fileno(), 4096)
            text = decoder.decode(chunk, final=not chunk)
            if text:
                buf.append(text)
                if task_run:
                    activity()
                pending += text
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    yield line + "\n"
            if not chunk:
                if pending:
                    yield pending
                return

    def _pump(stream, buf, event_context):
        nonlocal stdin_error
        try:
            for ln in captured_lines(stream, buf):
                if native_stdin is not None and stream is proc.stdout:
                    try:
                        frame = _json.loads(ln)
                    except (ValueError, TypeError):
                        frame = None
                    if isinstance(frame, dict):
                        try:
                            native_stdin.observe(frame)
                        except Exception as exc:
                            stdin_error = type(exc).__name__
                        if frame.get("type") == "user" and frame.get("isReplay") is True:
                            continue  # Input receipt, not a tool result or public message body.
                if task_run:
                    event = None
                    try:
                        value = _json.loads(ln)
                        if isinstance(value, dict) and value.get("type") == "runtime_activity":
                            event = {k: value[k] for k in ("call_id", "phase", "budget_s") if k in value}
                    except (ValueError, TypeError):
                        pass
                    if event is not None:
                        activity(event)
                try:  # 每行既刷心跳(worker活动信号),又把这行【真实过程】结构化透传上去。
                    # 事件回流(用户 goal:让军团各家的过程/事件像 CC 一样被看到·全过程流水)。
                    # _emit_harness_event 提炼出工具调用/访问URL/状态(过滤内部思考),以约定前缀
                    # 写真 stderr → worker 透传 → legion_core 解析成 cb 事件 → SSE 到前端。
                    _emit_harness_event(
                        tag, stream is proc.stdout and "stdout" or "stderr", ln, event_context)
                except Exception:
                    _sys.stderr.write("."); _sys.stderr.flush()
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass
    to = _threading.Thread(target=_pump, args=(proc.stdout, out_buf, event_context), daemon=True)
    te = _threading.Thread(target=_pump, args=(proc.stderr, err_buf, event_context), daemon=True)
    to.start(); te.start()
    if input is not None:
        try:
            proc.stdin.write(input); proc.stdin.close()
        except Exception:
            pass
    cancelled = False
    cpu_interval = min(20.0, max(0.1, lease.idle_s / 3))
    cpu_at = _t.monotonic()
    def _owned_cpu_seconds():
        query = getattr(_process_scope, "cpu_seconds", None)
        if _sys.platform == "darwin" and callable(query):
            return query()  # Unknown scope samples must not switch to a root-only counter.
        return _pgroup_cpu_seconds(proc.pid)

    cpu_last = _owned_cpu_seconds() if task_run and os.name != "nt" else -1
    try:
        while proc.poll() is None:
            if _process_scope is not None:
                _process_scope.observe()
            check = getattr(_INPROC_CANCEL_CHECK, "check", None)
            if callable(check):
                try:
                    if check():
                        cancelled = True
                        break
                except Exception:
                    pass
            _pump_native_stdin()
            _pump_transport_activity()
            if task_run and os.name != "nt" and _t.monotonic() - cpu_at >= cpu_interval:
                cpu_now = _owned_cpu_seconds()
                if cpu_now >= 0 and cpu_last >= 0 and cpu_now - cpu_last >= 0.02:
                    activity()
                cpu_last, cpu_at = cpu_now, _t.monotonic()
            reason = lease.expired() if task_run else (
                "command_timeout" if deadline and _t.monotonic() >= deadline else None)
            if reason:
                exc = _sp.TimeoutExpired(cmd, timeout)
                if task_run:
                    exc.runtime_reason = reason
                raise exc
            _t.sleep(0.1)
        if cancelled:
            _INPROC_CANCEL_CHECK.cancelled = True
            if _process_scope is not None:
                _process_scope.observe(force=True)
            try:
                if os.name != "nt":
                    os.killpg(os.getpgid(proc.pid), _signal.SIGTERM)
                else:
                    proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    if os.name != "nt":
                        os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
                    else:
                        proc.kill()
                except Exception:
                    pass
                proc.wait()
            if os.name != "nt":
                try:
                    os.killpg(proc.pid, _signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if _process_scope is not None:
                _process_scope.cleanup()
            to.join(timeout=2); te.join(timeout=2)
            return _NS(returncode=130, stdout="".join(out_buf), stderr="".join(err_buf),
                       cancelled=True,
                       **({"native_stdin_error": stdin_error} if native_stdin is not None else {}))
    except _sp.TimeoutExpired as _te:
        if _process_scope is not None:
            _process_scope.observe(force=True)
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
            else:
                proc.kill()
        except Exception:
            pass
        proc.wait()
        if _process_scope is not None:
            _process_scope.cleanup()
        to.join(timeout=2); te.join(timeout=2)
        # P0-2(codex dogfood 梳理揪出·CC 亲核):timeout 时进程被 kill 前 out_buf 常已含
        # `thread.started`(原生会话 id)。旧版直接 raise 丢弃 → 有原生 resume 能力的家
        # (codex/deepseek)下一轮无法 resume、从零重开。把已收集输出【附到异常对象】再 raise:
        # 只有会 resume 的家在 except 里主动取(getattr)解析 thread_id 存 checkpoint;其他家的
        # except TimeoutExpired 分支不取=零影响(不改它们的 timeout 语义·避免成套改10处的风险)。
        try:
            _te.legion_stdout = "".join(out_buf)
            _te.legion_stderr = "".join(err_buf)
        except Exception:
            pass
        raise _te
    finally:
        _close_native_stdin()
    to.join(timeout=2); te.join(timeout=2)
    _pump_transport_activity()
    if proc.returncode not in (0, None) and _process_scope is not None:
        _process_scope.cleanup()
    return _NS(returncode=proc.returncode, stdout="".join(out_buf), stderr="".join(err_buf),
               cancelled=False,
               **({"native_stdin_error": stdin_error} if native_stdin is not None else {}))


def _legion_tool_path() -> str:
    """Locate the shared legion tool CLI for every harness adapter."""
    here = _P(os.path.dirname(os.path.abspath(__file__)))
    for cand in (here.parent.parent / "release" / "legion-service" / "legion_tool.py",
                 here.parent / "legion-service" / "legion_tool.py",
                 _P(os.path.expanduser("~")) / ".meyo" / "legion" / "legion_tool.py"):
        if cand.exists():
            return str(cand)
    return "legion_tool.py"


def _media_cli_path() -> str:
    """定位 media_tools.py 绝对路径(军团各家共享媒体工具·context-not-control 让模型自调)。"""
    here = _P(os.path.dirname(os.path.abspath(__file__)))
    for cand in (here.parent.parent / "release" / "legion-service" / "media_tools.py",  # 仓库内
                 here.parent / "legion-service" / "media_tools.py",                     # dist 布局
                 _P(os.path.expanduser("~")) / ".meyo" / "legion" / "media_tools.py"):
        if cand.exists():
            return str(cand)
    return "media_tools.py"


def prompt_cache_key(user_id: str, session_id: str, harness_key: str) -> str:
    """生成会话级缓存槽：不暴露身份，且不同用户/会话/harness 绝不共槽。"""
    parts = [str(user_id or "").strip(), str(session_id or "").strip(),
             str(harness_key or "").strip()]
    if not all(parts):
        return ""
    digest = _hashlib.sha256("\0".join(parts).encode("utf-8", errors="replace")).hexdigest()[:32]
    return f"legion:v1:{digest}"


# 云真机任务置顶强化(2026-09-02 zip 题根因·context not control):云真机现场在 stable_block
# 中部,prime zip 题 23s 秒挂(宿主机找 /root/Meyo 报不存在)——不是 prime 笨,是它没把"这单是
# 云真机任务"和"执行环境在现场清单里"关联起来。修=任务明说云真机类词时,把一句执行环境
# 事实【置顶到 Question 旁】(只提可见性·不加规则不强制路径);不匹配时零变化·判断权永远在模型。
_CLOUD_DESKTOP_TRIGGERS = ("云真机", "真机", "云电脑", "云桌面", "CUA 容器", "cua-local-desktop",
                           "desktop_exec", "computer_command")


def _is_cloud_desktop_task(question: str) -> bool:
    """任务文本是否明说云真机类词(只用于信息置顶可见性·绝不做路径强制)。"""
    q = question or ""
    return any(t in q for t in _CLOUD_DESKTOP_TRIGGERS)


def _external_adapter_capabilities(key: str) -> dict:
    """只读实际 external 清单声明；运行依赖 shell 不等于模型有 shell 工具。"""
    import external_agent_registry as registry
    item = next((row for row in registry.load_catalog() if row.get("key") == key), {})
    declared = item.get("adapter_capabilities")
    if not isinstance(declared, dict) or not declared:
        return {}
    return {"harness": key, "source": "external_catalog.adapter_capabilities",
            **{field: declared[field] for field in (
                "streaming", "resume", "checkpoint", "tools", "artifacts", "interrupt")
               if field in declared}}


def _legion_prompt(question: str, ctx=None, *, adapter_capabilities=None) -> str:
    """军团统一 prompt：稳定能力在前、本轮现场在后，并在唯一发送边界执行硬预算。"""
    ctx = ctx or HarnessCtx.from_env()
    quick = str(getattr(ctx, "execution_depth", "standard") or "standard") == "quick"
    adapter_facts = (dict(adapter_capabilities)
                     if isinstance(adapter_capabilities, dict) else {})
    text_only = adapter_facts.get("tools") is False
    read_only = (ctx.task_facts or {}).get("native_read_only") is True
    if read_only:
        # Describe this invocation's native permissions, not the author's tools.
        base = ("This auxiliary invocation uses native read-only command permissions. "
                "File writes, external MCP, browser, apps, plugins and hooks are unavailable. "
                "The host records the response and checkpoint; no project work is authorized. ")
    elif text_only:
        protocol = _LEGION_TEXT_ACCESS + (
            "Return useful text in the user's requested format; a short answer is valid."
            if quick else _LEGION_TEXT_INTENT + _LEGION_COLLABORATION_PROTOCOL)
        base = protocol.format(legion_cli=_legion_tool_path(), media_cli=_media_cli_path())
    else:
        base = (QUICK_LEGION_PROTOCOL if quick else LEGION_PROTOCOL.format(
            legion_cli=_legion_tool_path(), media_cli=_media_cli_path()))
    adapter_block = ("\n\nADAPTER_CAPABILITIES_JSON\n"
                     + _json.dumps(adapter_facts, ensure_ascii=False, separators=(",", ":"))
                     if adapter_facts else "")
    # sandbox 可达性按本轮真实入口覆盖协议里的通用源码地图；Quick 不携带源码地图，需升级后再按需读取。
    if ctx.peer_source_context and not quick:
        base = base.replace(_PEER_SOURCE_MAP, ctx.peer_source_context)
    # 学 DSH compaction 的 prefix replay：协议后立即放跨轮不变能力块。历史、附件、失败等
    # 可变内容仍留在 Question 一侧，避免每轮在中间插字打碎网关前缀缓存。
    stable = ctx.stable_context or ""
    stable_block = f"\n\n【军团稳定能力现场·跨轮复用】\n{stable}" if stable.strip() else ""
    if text_only and stable_block:
        stable_block = ("\n\n【军团共享能力资料·当前文本接口没有执行工具】\n"
                        "下文是其他可执行入口的资料，不表示本次可调用；以本次接口声明为准。\n"
                        + stable)
    if read_only and stable_block:
        stable_block = ("\n\n【军团共享能力资料·不是本次辅助请求的授权】\n" + stable)
    brief = "" if quick else (ctx.runtime_brief or "")
    if not isinstance(brief, str):
        # In-process callers retain structured facts; isolated callers already
        # carry JSON text. Render both without dropping facts or changing policy.
        brief = _json.dumps(brief, ensure_ascii=False)
    brief_block = (f"\n\n【运行时现场简报·供你自己判断怎么用·非硬约束】\n{brief}"
                   if brief.strip() and not quick else "")
    # project_context 有独立高权重 section；从 task_facts 删除同名副本，避免同一项目现场双写计费。
    prompt_facts = dict(ctx.task_facts or {})
    prompt_facts.pop("project_context", None)
    if quick and ctx.project_context:
        for duplicate in ("goal", "team_id", "delivery_contract", "acceptance", "deliverables"):
            prompt_facts.pop(duplicate, None)
        prompt_facts = {key: value for key, value in prompt_facts.items()
                        if value not in (None, "", [], {}, 0)}
    facts_block = ("\n\nTASK_FACTS_JSON\n" + _json.dumps(prompt_facts, ensure_ascii=False, separators=(",", ":"))
                   if prompt_facts else "")
    policy_block = ("\n\nCAPABILITY_POLICY_JSON\n" + _json.dumps(ctx.capability_policy, ensure_ascii=False, separators=(",", ":"))
                    if ctx.capability_policy and not quick else "")
    memory_block = ("\n\nTASK_MEMORY_JSON\n" + _json.dumps(ctx.task_memory, ensure_ascii=False, separators=(",", ":"))
                    if ctx.task_memory else "")
    project_block = ("\n\nPROJECT_CONTEXT_JSON\n" +
                     _json.dumps(ctx.project_context, ensure_ascii=False, separators=(",", ":"))
                     if ctx.project_context else "")
    # 统一持久工作目录(2026-08-28 逐家测揪出:10家文件写得五花八门·大半丢了)。告诉每家它的
    # 持久工作目录·产物写这里就天然落盘、被军团自动收集展示给用户(不写路径也能收)。给现场事实·
    # 不强制。是"文件交付落实到每家"的关键:统一落盘点·不再各写各的临时目录导致产物丢失。
    workdir = ctx.workdir or ""
    wd_block = ""
    if workdir.strip() and not quick:
        wd_block = (f"\n\n【你的持久工作目录(重要)】{workdir}\n"
                    f"要给用户的产物(代码/PDF/Excel/HTML/图片/文档等)【一律写到这个目录里】——"
                    f"它是持久的(不会被删)·军团会自动扫描收集这里的文件展示给用户、供下载。"
                    f"别写到 /tmp 或你的临时 cwd 或家目录根(那些会丢或收不到)。"
                    f"如果希望文件刚生成时用户就看到你的说明，可在文件旁写同名 sidecar："
                    f"<文件名>.progress.json，内容为 description 与 next_action；系统会原样随产物卡推送，"
                    f"你随后继续工作。是否写、写什么由你自己判断；父层不会替你编。")
        if text_only:
            wd_block = (f"\n\n【项目持久目录引用】{workdir}\n"
                        "这是项目位置，不表示当前文本接口可以读取或写入它；"
                        "实际修改需要具备执行工具的入口。")
        if read_only:
            wd_block = (f"\n\n【辅助请求工作目录】{workdir}\n"
                        "本次原生命令为 read-only；回复及 checkpoint 由宿主记录。")
    desktop_emph = ""
    if _is_cloud_desktop_task(question) and not quick and not text_only and not read_only:
        desktop_emph = (
            "\n\n【本单涉及云真机·执行环境置顶提醒】\n"
            "用户要的就是在【云真机】(CUA 容器·XFCE 桌面)上干活。工具入口(desktop_exec 真机 shell/"
            "desktop_app 开软件/computer_command 键鼠/desktop_fs_* 文件/desktop_screenshot 截屏/"
            "desktop_open_file 打开产物)已在上文【军团稳定能力现场】给全——先 mcp_list 查 schema 再用。\n"
            "云真机容器内持久目录=/root/Meyo(宿主机【不存在】这个路径·别在宿主机文件系统找它);"
            "产物写到云真机内这个目录即可持久+被军团收集。怎么干由你自己判断,这里只把执行环境事实摆到你眼前。\n"
            "【反爬备选(2026-09-02 d15 血泪)】容器已装反检测浏览器栈(puppeteer-extra+stealth 插件+系统chromium·/root/Meyo/tools/stealth-crawler/crawl.js 是可运行通用样例):curl/wget 抓网站被反爬挡(403/挑战页/空壳HTML)时,用它真渲染页面拿内容:node crawl.js <url> <css选择器> <条数>(trending/hn 都验证过);需要更复杂操作(登录/滚屏/截图)时读样例源码改——stealth 已处理 navigator.webdriver 等指纹;"
            "另有 Python 反检测栈 invisible_playwright(C++ 源码级 patch Firefox·指纹在引擎内定·"
            "100% Playwright API 兼容)已装好,样例 /root/Meyo/tools/crawl_ipw.py,"
            "重指纹检测站(Cloudflare/reCAPTCHA)被 puppeteer 栈挡时换它。")
    question_block = f"\n\nQuestion:\n{question}"
    prompt, report = _budget_prompt_sections([
        ("adapter_capabilities", adapter_block, 18, 0.9),
        ("protocol", base, 16, 0.8), ("stable_context", stable_block, 10, 0.65),
        ("runtime_brief", brief_block, 6, 0.55), ("task_facts", facts_block, 8, 0.4),
        ("project_context", project_block, 22, 0.55),
        ("capability_policy", policy_block, 5, 0.65), ("task_memory", memory_block, 8, 0.3),
        ("workdir", wd_block, 5, 0.8), ("desktop_context", desktop_emph, 8, 0.65),
        # question 内通常是“原始目标在头、最新上游/失败现场在尾”，因此首尾同时保留且权重最高。
        ("question", question_block, 40, 0.42),
    ], ctx)
    previous_report = dict(getattr(ctx, "context_budget_report", None) or {})
    ctx.context_budget_report = report
    if report.get("degraded") and report != previous_report:
        event = {"t": "context_compacted", "phase": "压缩现场", "status": "ready",
                 "context_budget": report,
                 "x": (f"发送前已将 {report['original_estimated_tokens']} token 上界的现场压到 "
                       f"{report['final_estimated_tokens']}/{report['input_budget_tokens']}；完整资料仍保存在任务存储中")}
        if not _emit_inproc(event):
            _sys.stderr.write(_LEGION_EV_PREFIX + _json.dumps(event, ensure_ascii=False) + "\n")
            _sys.stderr.flush()
    return prompt


def _penguin_trace_files(traces: _P, resume_id: str = "") -> list[_P]:
    """Native segment names, not caller-supplied glob syntax or mtime ranking."""
    files = []
    for path in traces.glob("*/*.jsonl"):
        sid, separator, segment = path.stem.rpartition("_")
        if (not separator or not segment.isdigit() or (resume_id and sid != resume_id)
                or path.is_symlink() or not path.is_file()
                or not path.resolve().is_relative_to(traces.resolve())):
            continue
        files.append(path)
    return sorted(files, key=lambda p: (
        p.stem.rpartition("_")[0], int(p.stem.rpartition("_")[2]), str(p.parent)))


def _penguin_trace_offsets(resume_id: str = "") -> tuple[_P, dict[str, int] | None]:
    """Snapshot byte offsets in the same per-call home the native CLI will use."""
    env = {**os.environ, **(getattr(_INPROC_RUNTIME_ENV, "values", {}) or {})}
    root = _P(env.get("PENGUIN_HOME") or
              (_P(env.get("HOME") or str(_P.home())) / ".penguin" / "data"))
    traces = root / "default_project" / "agents" / "default_agent" / "traces"
    try:
        # Include older dates: a resumed/rotated trace or midnight crossing must
        # not make an old completed message look like a newly created record.
        return traces, {str(path): path.stat().st_size
                        for path in _penguin_trace_files(traces, resume_id)}
    except OSError:
        # Unknown is not an empty, successfully inspected directory. Otherwise
        # a failed stat can promote an earlier answer to this invocation.
        return traces, None


def _penguin_error_observation(message, *, source: str, native_status=None):
    """Project a real diagnostic through the existing error taxonomy.

    Native request_end exposes status/error_message/attempt/retry_in_ms, not a
    request-budget scope or retry permission. Its message may include private
    model context: retain a fingerprint and its trace location, not the body.
    Classification here is a runtime interpretation, never a native grant.
    """
    import hashlib

    raw = message if isinstance(message, str) else ""
    cause = {
        "source": source, "native_retryable": None, "native_scope": None,
        "message_chars": len(raw),
        "message_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "message_body": "not_projected",
        "native_trace_retained": source.startswith("penguin_trace."),
    }
    forced = {"auth": "authorization_required", "aborted": "user_cancel"}.get(native_status, "")
    try:
        from error_envelope import normalize
        envelope = normalize(
            "timeout" if native_status == "timeout" else "error", raw, fail_kind=forced).to_dict()
    except ImportError:
        # Old standalone distributions may not carry the service error module.
        envelope = {"category": forced or "unknown", "retryable": False, "scope": "harness"}
    if envelope["category"] == "task_error":
        envelope["category"] = "unknown"
    category = envelope["category"]
    explanations = {
        "model_config": "Penguin 的模型配置或请求参数被拒绝；请查看本次原生诊断。",
        "gateway": "Penguin 报告模型提供方服务错误；本次原生诊断已定位。",
        "rate_limit": "Penguin 报告提供方限流或额度错误；未报告整单预算是否耗尽。",
        "authorization_required": "Penguin 原生请求报告鉴权失败；请检查本次模型授权。",
        "credential_required": "Penguin 报告本次调用缺少所需凭据。",
        "context_overflow": "Penguin 报告本次输入超过模型上下文限制。",
        "timeout": "Penguin 原生请求报告超时；未取得完整结果。",
        "user_cancel": "Penguin 原生请求报告已中断。",
        "harness_runtime": "Penguin 启动或运行环境出错；请查看本次诊断。",
        "process": "Penguin 进程报告执行错误；不代表模型已完成。",
    }
    envelope["message"] = explanations.get(
        category, "Penguin 的失败原因尚未识别；未将未知原因改成预算耗尽或授权成功。")
    envelope["code"] = "penguin_" + str(native_status or "process_error")
    envelope["details"] = {**(envelope.get("details") or {}),
                           "native_source": source, "classification_source": "runtime_error_taxonomy",
                           "diagnostic_body_projected": False}
    cause["category"] = category
    return cause, envelope


def _penguin_trace_after(traces: _P, offsets: dict[str, int] | None, *, workdir: str = "",
                         resume_id: str = "", request_text: str | None = None) -> dict:
    """Read this invocation's native text/usage without exposing thinking.

    Resume binds the native session plus offsets. A fresh CLI has no session
    argument/output envelope: bind its native userText(input) and workspace,
    then require one session. Never choose the newest unrelated trace.
    """
    result = {"usage": None, "session_id": resume_id, "native_events": [], "final": None,
              "completed": False, "request_status": None, "matched_session": False,
              "trace_state": "no_current_trace", "trace_refs": [], "request_end": None,
              "aborted": False, "terminal_error": None, "error_envelope": None}
    if offsets is None:
        return {**result, "trace_state": "offset_snapshot_unavailable"}
    try:
        files = _penguin_trace_files(traces, resume_id)
    except OSError:
        return {**result, "trace_state": "trace_unavailable"}
    grouped: dict[str, list[_P]] = {}
    for path in files:
        grouped.setdefault(path.stem.rpartition("_")[0], []).append(path)
    candidates = []
    for sid, paths in grouped.items():
        try:
            changed = [(path, int(offsets.get(str(path), 0))) for path in paths
                       if path.stat().st_size != int(offsets.get(str(path), 0))]
            if not changed:
                continue
            # Only the header is read before an offset, for native identity.
            # Old assistant messages are never eligible as the current answer.
            with paths[0].open("rb") as handle:
                header = _json.loads(handle.readline().decode("utf-8"))
            meta = header.get("payload") if isinstance(header, dict) else None
            if (not isinstance(header, dict) or header.get("type") != "session_meta" or not isinstance(meta, dict)
                    or meta.get("session_id") != sid):
                continue
            workspace = meta.get("workspace")
            if workdir and (not isinstance(workspace, str) or not workspace
                            or os.path.realpath(workspace) != os.path.realpath(workdir)):
                continue
            records, refs, malformed = [], [], False
            for path, offset in changed:
                size = path.stat().st_size
                if offset < 0 or offset > size:
                    malformed = True
                    continue
                with path.open("rb") as handle:
                    handle.seek(offset)
                    lines = handle.read().decode("utf-8").splitlines()
                refs.append({"path": str(path), "offset": offset, "bytes": size - offset})
                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        row = _json.loads(line)
                        if not isinstance(row, dict):
                            raise ValueError("non-object trace record")
                        payload = row.get("payload")
                        if not isinstance(payload, dict):
                            raise ValueError("trace payload unavailable")
                        if row.get("type") == "session_meta" and (
                                payload.get("session_id") != sid or payload.get("workspace") != workspace):
                            raise ValueError("trace identity changed")
                        records.append((row, str(path)))
                    except (TypeError, ValueError):
                        malformed = True
            if not resume_id and request_text is not None and not any(
                    row.get("type") == "model_msg" and row["payload"].get("role") == "user"
                    and row["payload"].get("type") == "text"
                    and row["payload"].get("text") == request_text for row, _ in records):
                continue
            candidates.append((sid, records, refs, malformed))
        except (OSError, ValueError, TypeError, UnicodeError):
            continue
    if len(candidates) != 1:
        if candidates:
            result["trace_state"] = "ambiguous_current_sessions"
        return result
    session_id, records, refs, malformed = candidates[0]
    result.update(session_id=session_id, matched_session=True, trace_refs=refs)
    totals = {"cache_read_tokens": 0, "cache_write_tokens": 0,
              "output_tokens": 0, "total_tokens": 0}
    usage_events = 0
    active, compaction, request_status, final = False, 0, None, None
    bound = request_text is None
    foreign_input = False
    pending_rotation, summary_allowed = False, False
    # A resumed suffix starts after its old session_meta. The candidate's
    # header/identity was already checked above; retain that starting segment
    # so a later, genuine compaction rotation can be recognized.
    segment = records[0][1] if records else None
    for row, path in records:
        payload = row["payload"]
        event_type = str(payload.get("type") or "")
        if row.get("type") == "session_meta":
            # Successful compaction rotates the native trace before injecting
            # [context_summary]. Only that structural continuation, not any
            # different user input, can remain part of the same invocation.
            summary_allowed = bool(pending_rotation and segment is not None and path != segment)
            pending_rotation, segment = False, path
            continue
        if (row.get("type") == "model_msg" and payload.get("role") == "user"
                and event_type == "text" and not compaction):
            text = payload.get("text")
            native_summary = (summary_allowed and isinstance(text, str) and (
                text.startswith("[context_summary]") and text.endswith("[/context_summary]")
                or text.startswith("<context_summary>") and text.endswith("</context_summary>")))
            if request_text is not None and text == request_text and not (
                    bound and isinstance(final, str) and request_status == "completed"):
                bound = True
            elif bound and not native_summary and payload.get("sender") != "harness":
                if request_text is not None:
                    foreign_input = True
                    active, final, request_status = False, None, None
                    result.update(request_end=None, terminal_error=None, error_envelope=None)
                    break
            summary_allowed = False
        if not bound:
            continue
        if row.get("type") == "event_msg" and event_type == "token_usage":
            request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
            for name, source_name in (("cache_read_tokens", "cache_read"),
                                      ("cache_write_tokens", "cache_write"),
                                      ("output_tokens", "output"), ("total_tokens", "total")):
                try:
                    totals[name] += max(0, int(request.get(source_name) or 0))
                except (TypeError, ValueError):
                    pass
            usage_events += 1
        if row.get("type") == "event_msg" and event_type in {"compaction_begin", "compaction_end", "abort"}:
            result["native_events"].append(event_type)
        if row.get("type") == "event_msg" and event_type == "compaction_begin":
            compaction += 1
            continue
        if row.get("type") == "event_msg" and event_type == "compaction_end":
            compaction = max(0, compaction - 1)
            pending_rotation = payload.get("status") == "completed"
            continue
        if compaction:
            continue
        if row.get("type") == "event_msg":
            if event_type == "request_begin":
                active, request_status, final = True, None, None
                result.update(request_end=None, terminal_error=None, error_envelope=None)
            elif event_type == "request_end" and active:
                active = False
                request_status = payload.get("status")
                if not isinstance(request_status, str):
                    request_status, malformed = None, True
                result["request_end"] = {
                    "type": "request_end", "status": request_status,
                    **{name: payload[name] for name in ("attempt", "retry_in_ms")
                       if type(payload.get(name)) is int and payload[name] >= 0},
                }
                if request_status != "completed":
                    final = None
                    result["terminal_error"], result["error_envelope"] = _penguin_error_observation(
                        payload.get("error_message"), source="penguin_trace.request_end",
                        native_status=request_status)
            elif event_type == "abort":
                result["aborted"] = True
                active, final = False, None
                # Native abort follows a failed request too. Preserve the
                # request's cause instead of relabelling every failure a cancel.
                if request_status in (None, "completed"):
                    request_status = "aborted"
                if result["terminal_error"] is None:
                    result["terminal_error"], result["error_envelope"] = _penguin_error_observation(
                        payload.get("reason"), source="penguin_trace.abort", native_status=request_status)
        elif row.get("type") == "model_msg":
            if active and payload.get("role") == "assistant":
                if event_type == "text":
                    text = payload.get("text")
                    final = text if isinstance(text, str) and payload.get("stop_reason") == "completed" else None
                elif event_type != "thinking":
                    final = None  # Tool calls are not a final assistant text.
            elif payload.get("role") in {"user", "tool"}:
                final = None
                if payload.get("role") == "user" and not active:
                    request_status = None
    if usage_events:
        totals["input_tokens"] = totals["cache_read_tokens"] + totals["cache_write_tokens"]
        totals.update({"source": "penguin_trace", "granularity": "task",
                       "input_semantics": "cache_buckets", "num_turns": usage_events,
                       "invocation_scope_complete": not foreign_input})
        result["usage"] = totals
    result["request_status"] = request_status
    result["completed"] = (request_status == "completed" and not active and not malformed
                           and not foreign_input and not result["aborted"])
    result["final"] = final if result["completed"] else None
    result["trace_state"] = ("foreign_input_after_request" if foreign_input else
                             "invalid_current_trace" if malformed else
                             "completed_text" if result["final"] is not None else
                             "missing_completed_text" if result["completed"] else "incomplete_request")
    return result


def _penguin_usage_after(traces: _P, offsets: dict[str, int] | None, *, workdir: str = "",
                         resume_id: str = "") -> tuple[dict | None, str, list[str]]:
    """Keep the existing three-value usage API; share the scoped native reader."""
    result = _penguin_trace_after(traces, offsets, workdir=workdir, resume_id=resume_id)
    return result["usage"], result["session_id"], result["native_events"]


def _cc_native_terminal(stdout, returncode, cancelled=False):
    """Parse CC's terminal protocol; progress is evidence, never a final fallback."""
    facts = {"process_returncode": returncode, "cancelled": bool(cancelled),
             "interrupted": bool(cancelled), "turn_status": "unreported",
             "result_seen": False, "result_history": [], "parse_errors": []}
    events, sid, last_assistant = [], "", {}
    protocol_seen, auth_failed = False, False
    for number, line in enumerate((stdout or "").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if not line.startswith(("{", "[")):
            continue  # Legacy scalar/text stdout (including "2") is not an event.
        protocol_seen = True
        try:
            obj = _json.loads(line)
        except (TypeError, ValueError):
            facts["parse_errors"].append({"line": number, "error": "invalid_json"})
            continue
        if not isinstance(obj, dict):
            facts["parse_errors"].append({"line": number, "error": "non_object_record"})
            continue
        kind = obj.get("type")
        if isinstance(kind, str) and kind not in events:
            events.append(kind)
        if isinstance(obj.get("session_id"), str) and obj["session_id"]:
            sid = obj["session_id"]
        if kind == "result":
            facts["result_seen"] = True
            facts["turn_result"] = obj
            facts["result_history"].append(obj)
        elif kind == "assistant":
            if obj.get("error"):
                facts.setdefault("stream_errors", []).append(obj)
                auth_failed = auth_failed or obj["error"] == "authentication_failed"
            message = obj.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("content"), list):
                facts["parse_errors"].append({"line": number, "error": "invalid_assistant"})
                continue
            content = message["content"]
            last_assistant = {
                "message_id": message.get("id"),
                "session_id": obj.get("session_id"),
                "parent_tool_use_id": obj.get("parent_tool_use_id"),
                "error": obj.get("error"),
                "text": "".join(block["text"] for block in content
                                if isinstance(block, dict) and block.get("type") == "text"
                                and isinstance(block.get("text"), str)).strip(),
                "stop_reason": message.get("stop_reason"),
                "tool_use": any(isinstance(block, dict) and block.get("type") == "tool_use"
                                for block in content),
            }
            facts["last_assistant"] = last_assistant
        elif kind == "error":
            facts.setdefault("stream_errors", []).append(obj)
    final = ""
    terminal = facts.get("turn_result") or {}
    if facts["result_seen"]:
        subtype = str(terminal.get("subtype") or "")
        failed = terminal.get("is_error") is True or subtype.startswith("error")
        completed = not failed and (
            terminal.get("is_error") is False or subtype == "success"
            or isinstance(terminal.get("result"), str))
        facts["turn_status"] = "failed" if failed else "completed" if completed else "unreported"
        facts["terminal_source"] = "result"
        if completed and isinstance(terminal.get("result"), str):
            final = terminal["result"].strip()
    elif (last_assistant.get("stop_reason") == "end_turn"
          and not last_assistant.get("tool_use") and not last_assistant.get("error")):
        facts.update(turn_status="completed", terminal_source="assistant_end_turn")
        final = last_assistant["text"]
    elif not protocol_seen and (stdout or "").strip() and not cancelled and returncode == 0:
        # Explicit legacy plain stdout: never reinterpret JSON protocol debris.
        facts.update(turn_status="completed", terminal_source="legacy_plain_stdout")
        final = stdout.strip()
    facts["completed"] = facts["turn_status"] == "completed"
    facts["failed"] = facts["turn_status"] == "failed" or returncode not in (None, 0)
    if cancelled:
        status = "cancelled"
    elif returncode not in (None, 0):
        status, final = f"rc{returncode}", ""
    elif facts["failed"]:
        status, final = "error", ""
    elif not facts["completed"]:
        status, final = "incomplete", ""
    else:
        status = "ok"
    return final, status, facts, events, sid, auth_failed


def _cc_adapter(qhash, question, gold="", ctx=None):
    # 仅显式部署 CCR 时绑定 ctx.model；未部署仍沿用本地 Claude/CCR 配置。
    ctx = ctx or HarnessCtx.from_env()
    t0 = _t.time()
    _managed_browser = None

    def _redact_cc(value):
        if isinstance(value, str):
            for secret in (ctx.appid, *(_cc_env.get(name) for name in
                           ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "LEGION_CCR_AUTH_TOKEN"))):
                if secret:
                    value = value.replace(str(secret), "[redacted]")
            return value
        if isinstance(value, list):
            return [_redact_cc(item) for item in value]
        if isinstance(value, dict):
            return {key: _redact_cc(item) for key, item in value.items()}
        return value

    try:
        _deployment_ccr_url = str(os.environ.get("LEGION_CCR_BASE_URL") or "").strip().rstrip("/")
        _deployment_ccr_model = ""
        if _deployment_ccr_url:
            # CCR 原生格式为 provider,model；provider 由部署配置提供，不拿 appid 猜。
            try:
                from urllib.parse import urlsplit as _urlsplit
                import ipaddress as _ipaddress
                _url = _urlsplit(_deployment_ccr_url)
                _loopback = _url.hostname == "localhost" or (
                    _url.hostname is not None and _ipaddress.ip_address(_url.hostname).is_loopback)
                if (_url.scheme not in ("http", "https") or not _loopback
                        or _url.username is not None or _url.password is not None
                        or _url.query or _url.fragment):
                    raise ValueError("LEGION_CCR_BASE_URL 必须是无内嵌认证的 loopback 服务 URL")
                _url.port  # 提前拒绝非法端口。
                import model_registry as _cc_models
                _deployment_ccr_model = _cc_models.ccr_route(ctx.model, ctx.appid, ctx.gateway)
            except (ValueError, TypeError, ImportError, AttributeError):
                _message = ("CCR 部署配置错误：检查 LEGION_CCR_BASE_URL 是否为 loopback URL，"
                            "并用 configure-ccr.py 生成包含当前模型、授权和网关的 "
                            "LEGION_CCR_REQUEST_PROVIDERS；不会借用另一请求的 provider。")
                return {"final": "", "status": "error", "error": _message,
                        "fail_kind": "harness_runtime",
                        "error_envelope": {"category": "harness_runtime", "retryable": False,
                                           "scope": "harness", "tool": "cc-router",
                                           "code": "cc-router-config", "message": _message},
                        "elapsed": round(_t.time() - t0, 1), "model_used": ctx.model}
        # headless(-p)默认不放行工具 → cc 想抓网页/跑 bash 却调不动,只能回"我会去做"就交差。
        # 军团是用户自己的受控环境,放行工具权限,cc 才能真调工具(WebFetch/Bash)干活而非说空话。
        # 根因3 修复:见 CLAUDE.md「军团为什么不智能」。可用 env 关掉(默认开)。
        # 用 --output-format stream-json --verbose(realtime 逐事件流):cc 的每步工具调用/访问
        # 都逐行 JSONL 吐出 → _run_live 的事件回流管道能提炼成 harness_step 给前端全过程流水
        # (旧的单次 json 整个过程只吐最后一行·前端只能转圈)。final 从末尾 type=result 事件取。
        _checkpoint = (ctx.task_memory or {}).get("native_checkpoint") or {}
        _resume_id = str(_checkpoint.get("session_id") or "") if isinstance(_checkpoint, dict) else ""
        prompt = _legion_prompt(_prior_attempts_prelude(ctx.prior_attempts) + question, ctx)
        cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
               "--include-partial-messages"]
        if _deployment_ccr_model:
            cmd += ["--model", _deployment_ccr_model]
        # ★ --bare 彻底跳过 hooks/LSP/plugin/auto-memory/CLAUDE.md自动加载/keychain(官方 minimal mode)。
        # 用户 2026-09-01 loop 真跑揪出:旧 --settings {"hooks":{}} 只是【叠加】一个空 hooks 来源·挡不住
        # 用户级/项目级已有的 hook(实测 cc --resume 仍触发 SessionStart:resume hook·final 被 hook_started
        # JSON 污染→rc143/rc-9 反复失败拖住整单)。--bare 从根上跳过所有来源的 hook·比 --settings 彻底
        # (真验证:--bare 输出零 hook 噪声)。它只跳干扰·不动工具/权限(那是 --restricted)·cc 干军团活
        # 不依赖项目 CLAUDE.md/auto-memory(军团 prompt 已给全上下文)。谁挡稳定底线降级谁(铁律0)。
        cmd.insert(1, "--bare")
        _native_sid = str(_resume_id)          # resume 路径:native sid=resume id(撞锁重试要替换它)
        if _resume_id:
            cmd += ["--resume", _resume_id]
        else:
            # Claude Code 要 UUID；首轮显式固定 session_id，下一进程才能原生 --resume 同一对话。
            import uuid as _uuid
            try:
                _native_sid = str(_uuid.UUID(ctx.session_id.split(":", 1)[-1]))
            except Exception:
                _native_sid = str(_uuid.uuid5(_uuid.NAMESPACE_URL, ctx.session_id or f"cc:{qhash}"))
            cmd += ["--session-id", _native_sid]
        _cc_mailbox = None
        if str(os.environ.get("LEGION_CC_LIVE_MAILBOX") or "").lower() in {"1", "true", "yes"}:
            try:
                import cc_live_mailbox
                _cc_mailbox = cc_live_mailbox.create(ctx, _native_sid, prompt)
            except Exception as exc:
                _message = "CC 原生消息输入绑定未就绪：" + type(exc).__name__
                return {"status": "error", "final": "", "error": _message,
                        "fail_kind": "harness_runtime", "model_used": ctx.model,
                        "error_envelope": {"category": "harness_runtime", "retryable": False,
                                           "scope": "harness", "tool": "cc-native-input",
                                           "code": "native-input-binding", "message": _message}}
            if _cc_mailbox is not None:
                # Same native prompt/session, supplied through the CLI's own
                # streaming protocol. No hooks or extra model/control loop.
                del cmd[cmd.index("-p") + 1]
                cmd += ["--input-format", "stream-json", "--replay-user-messages"]
        # Explicit project-file execution profile: native directory confinement
        # and edit approval, not a replacement for an isolated full-shell worker.
        _cc_profile = str(os.environ.get("LEGION_CC_PERMISSION_PROFILE") or "").strip()
        if _cc_profile in ("project_files", "project_read_only"):
            if not ctx.workdir or not _deployment_ccr_url:
                return {"status": "error", "final": "", "model_used": ctx.model,
                        "fail_kind": "harness_runtime",
                        "error": f"{_cc_profile} requires an explicit project workdir and deployed CCR"}
            if _cc_profile == "project_read_only":
                if (ctx.execution_depth == "quick"
                        or os.environ.get("LEGION_CC_SANDBOX_BACKEND") != "codex"):
                    return {"status": "error", "final": "", "model_used": ctx.model,
                            "fail_kind": "harness_runtime",
                            "error": "project_read_only requires non-quick managed browser execution in codex"}
                # File/command tools are restricted by the native CLI, not a prompt.
                # Browser interactions may still change application state.
                cmd[1:1] = ["--restricted", "--tools", "Read",
                            "--permission-mode", "dontAsk", "--permission-prompts", "none",
                            "--setting-sources", "", "--strict-mcp-config"]
            else:
                cmd[1:1] = ["--restricted", "--permission-mode", "acceptEdits"]
        elif _cc_profile:
            return {"status": "error", "final": "", "model_used": ctx.model,
                    "fail_kind": "harness_runtime", "error": "unknown Claude permission profile"}
        elif os.environ.get("LEGION_CC_ALLOW_TOOLS", "1") == "1":
            cmd.insert(1, "--dangerously-skip-permissions")
        # ★ hook 彻底隔离(持久化+速度底线根治·用户2026-09-01"每家跑通是底线中的底线·谁挡底线降级谁"):
        # cc 子进程继承本机 Claude Code 用户级 hook(Pilot v2 的 Stop/SessionStart/PostToolUse/
        # UserPromptSubmit + superpowers SessionStart)→cc 明明已答"OK"·Stop hook 反复注入"空转
        # 0/2、1/2…"把它又驱动继续·SessionStart 塞满 <EXTREMELY_IMPORTANT> skill→headless 卡在
        # hook 循环里跑不完→整家 timeout(真跑坐实:带本机 hook 卡>180s·干净配置 3.7s 秒过)。
        # 修法(谁挡底线降级谁):只设 MEYO_PILOT_V2=0 只能关 Pilot·superpowers 等其它 hook 仍拦→
        # 改用 --settings 指向一个【无任何 hook】的干净临时配置目录·一次性绕开所有本机 hook 干扰。
        # 派军团的 cc 是干活子进程·本就不该被开发者本机 hook 拦(context not control:关的是拦截·不动 cc 答题)。
        _cc_env = _gateway_no_proxy(dict(os.environ))
        _cc_env["MEYO_PILOT_V2"] = "0"
        _cc_cwd = ctx.workdir or None
        if _cc_cwd:
            _P(_cc_cwd).mkdir(parents=True, exist_ok=True)
        if _deployment_ccr_url:
            _cc_env["ANTHROPIC_BASE_URL"] = _deployment_ccr_url
            _cc_env["ANTHROPIC_AUTH_TOKEN"] = os.environ.get("LEGION_CCR_AUTH_TOKEN", "")
            _cc_env["SHELL"] = "/bin/bash"
            # 清除继承的直连 key，避免与 router 认证混用；ctx.appid 属于上游 provider。
            _cc_env.pop("ANTHROPIC_API_KEY", None)
        # ★本地 cc-router(ANTHROPIC_BASE_URL)死代理检测+降级(铁律0·2026-09-02 真跑钉死):
        #   cc 的 claude 子进程继承 ANTHROPIC_BASE_URL=127.0.0.1:3456(用户装的本机 cc-router)。
        #   router 半死(健康检查 OK 但 /v1/messages 30s 无响应)→claude 全程纯等·user CPU 仅 0.85s·
        #   62s 里 53s 白等→整单被拖死(简单题 5 家最快 5.8s·cc 一家占 62s)。与 codex 死代理同病根。
        #   修法(谁挡底线降级谁):起进程前 3s 探 router 的 /v1/messages 真实可用性(不是 /health·
        #   那个半死也 OK)。探不通→本单 cc 如实返回 router 不可用(父层自愈换家接力·不再 53s 空等);
        #   探通→正常走 router。结果缓存 60s(探一次别每单都打)。context not control:只报事实·不替 cc 答题。
        _router_url = str(_cc_env.get("ANTHROPIC_BASE_URL") or "").rstrip("/")
        # 旧探测使用固定模型和父环境认证，不能用于部署实例；交给本单真实 CLI 请求回流，
        # 也不允许旧自愈逻辑重启用户的另一套本地 CCR。
        if not _deployment_ccr_url and (_router_url.startswith("http://127.0.0.1") or _router_url.startswith("http://localhost")):
            _probe = _probe_local_router(_router_url)
            if _probe is False:
                # ★真自愈(用户 2026-09-02):先自动重启 ccr 修基础设施->重探->活则本单继续走 cc。
                # 只有救不活才 error 交父层换家(兜底)——不再把补位当第一选择。
                if not _ccr_selfheal_restart(_router_url):
                    _message = f"cc 本地 router 不可用({_router_url} /v1/messages 无响应·自动重启后仍未恢复)"
                    return {"final": "", "status": "error", "error": _message,
                            "fail_kind": "harness_runtime",
                            "error_envelope": {"category": "harness_runtime", "retryable": False,
                                               "scope": "harness", "tool": "cc-router",
                                               "code": "cc-router-down", "message": _message},
                            "elapsed": 0.2, "model": "cc-router-down"}
        _cc_execution_started = False

        def _execute_cc(command, environment, wrap=lambda value: value):
            nonlocal _cc_execution_started
            wrapped = wrap(command)
            _cc_execution_started = True
            _io = {"native_stdin": _cc_mailbox} if _cc_mailbox is not None else {"input": ""}
            result = _run_task(wrapped, _WALL_S, tag="cc",
                               env=environment, cwd=_cc_cwd, **_io)
            # ★session 撞锁自愈(2026-09-02 L8 验收揪出·"环境失败被当模型失败"同类病):
            #   cc 的 --session-id 撞"already in use"(旧进程占锁)→ 换模型救不了·旧逻辑 rc1
            #   空烧整个模型池。撞锁=环境态:同请求换一个加盐 uuid 重试一次(会话连续性退化为
            #   新开槽,但交付优先;锁竞争罕见·不影响正常续跑)。
            if (_cc_mailbox is None and result.returncode != 0
                    and "already in use" in (result.stderr or "") + (result.stdout or "")):
                import uuid as _uu
                _retry_sid = str(_uu.uuid5(_uu.NAMESPACE_URL,
                                           f"{ctx.session_id}:retry-{_t.time()}"))
                _cmd2 = [c if c != _native_sid else _retry_sid for c in command]
                result = _run_task(wrap(_cmd2), _WALL_S, input="", tag="cc-retry",
                                   env=environment, cwd=_cc_cwd)
            return result

        _cc_backend = str(_cc_env.get("LEGION_CC_SANDBOX_BACKEND") or "").strip()
        _needs_context_read = (
            _cc_profile in ("project_files", "project_read_only")
            and (ctx.task_facts or {}).get("native_session_scope") == "request"
            and bool(getattr(ctx, "context_refs", None)))
        if _needs_context_read and _cc_backend != "codex":
            return {"status": "error", "final": "", "fail_kind": "harness_runtime",
                    "model_used": ctx.model,
                    "error": "本请求的上下文只读访问需要已声明的原生隔离边界",
                    "error_envelope": {"category": "harness_runtime", "scope": "harness",
                                       "retryable": False, "code": "context-read-boundary-missing"}}
        if _cc_backend or _cc_env.get("MEYO_RUNTIME_ROOT"):
            try:
                from native_process_sandbox import prepare_native_process

                _protected = [str(_P(__file__).resolve().parents[2])]
                _protected.extend(str(_cc_env[name]) for name in
                                  ("MEYO_RUNTIME_ROOT", "MEYO_TOOLBOX_ROOT",
                                   "LEGION_PROC_REGISTRY_DIR")
                                  if _cc_env.get(name))
                _state_roots = [str(_cc_env[name]) for name in
                                ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "CODEX_HOME")
                                if _cc_env.get(name)]
                _context_access = None
                if _needs_context_read:
                    import task_memory as _context_store
                    _context_access = _context_store.context_read_directory(
                        qhash, "cc", ctx.user_id, ctx.project_context, ctx.task_facts, ctx.context_refs)
                    if _context_access:
                        _read_root = _P(_context_access["directory"])
                        def _overlaps_context(value):
                            path = _P(value).resolve(strict=True)
                            return path.is_relative_to(_read_root) or _read_root.is_relative_to(path)
                        # Preserve native HOME/SID and the actual project. Only
                        # remove broad service-data write roots from this launch.
                        if _overlaps_context(_cc_cwd) or any(
                                _overlaps_context(_cc_env[name]) for name in ("HOME", "CODEX_HOME")
                                if _cc_env.get(name)):
                            raise ValueError("context read directory overlaps native workspace or home")
                        _state_roots = [root for root in _state_roots if not _overlaps_context(root)]
                        _protected.append(str(_read_root))
                        cmd += ["--add-dir", str(_read_root)]
                # The host keeps tracking the outer process. The execution
                # context owns only private launch files and child namespace state.
                with prepare_native_process(
                        env=_cc_env, cwd=_cc_cwd, backend=_cc_backend,
                        protected_roots=_protected,
                        writable_state_roots=_state_roots) as _launch:
                    from contextlib import ExitStack
                    with ExitStack() as _owned_tools:
                        _settings = _P(_launch.config_dir) / "cc-settings.json"
                        _settings.write_text(_json.dumps({"hooks": {}}), encoding="utf-8")
                        _settings.chmod(0o600)
                        cmd += ["--settings", str(_settings)]
                        if ctx.execution_depth != "quick":
                            _tool_env = {**_cc_env, **(getattr(_INPROC_RUNTIME_ENV, "values", {}) or {})}
                            _managed_mode = str(_tool_env.get("LEGION_MANAGED_BROWSER") or "").lower()
                            if _managed_mode not in ("", "0", "false", "no", "1", "true", "yes"):
                                raise ValueError("unknown managed browser mode")
                            if (_cc_profile == "project_read_only"
                                    and _managed_mode not in ("1", "true", "yes")):
                                raise ValueError("project_read_only requires the managed browser capability")
                            if _managed_mode in ("1", "true", "yes"):
                                if _cc_backend != "codex":
                                    raise ValueError("managed browser requires the declared native boundary")
                                from managed_browser import open_managed_browser
                                _managed_browser = _owned_tools.enter_context(open_managed_browser(
                                    ctx, env=_tool_env, config_dir=_launch.config_dir))
                                _config = _managed_browser.mcp_config
                                if _cc_profile in ("project_read_only", "project_files"):
                                    from managed_browser import _TOOLS as _managed_tool_names
                                    if set(_config.get("mcpServers") or {}) != {"meyo-playwright"}:
                                        raise ValueError(f"{_cc_profile} requires the single managed MCP server")
                                    _names = [tool.get("name") for tool in _managed_browser.tools]
                                    if not _names or any(name not in _managed_tool_names for name in _names):
                                        raise ValueError(f"{_cc_profile} received an ungranted browser tool")
                                    if _cc_profile == "project_files":
                                        cmd += ["--strict-mcp-config", "--permission-prompts", "none"]
                                    cmd += ["--allowedTools", ",".join([
                                        *(["Read"] if _cc_profile == "project_read_only" else []),
                                        *("mcp__meyo-playwright__" + name
                                          for name in sorted(set(_names)))])]
                            else:
                                from meyo_harnesses import mcp_bridge as _native_mcp
                                _config = _native_mcp.native_stdio_config(
                                    ["playwright"], namespace="meyo")
                            _mcp_file = _P(_launch.config_dir) / "cc-mcp.json"
                            _mcp_file.write_text(
                                _json.dumps(_config, ensure_ascii=False), encoding="utf-8")
                            _mcp_file.chmod(0o600)
                            cmd += ["--mcp-config", str(_mcp_file)]
                        if _context_access:
                            _context_store.context_read_directory(
                                qhash, "cc", ctx.user_id, ctx.project_context, ctx.task_facts,
                                ctx.context_refs, expected_identity=_context_access["identity"])
                        r = _execute_cc(cmd, _launch.env, _launch.wrap)
            except Exception as exc:
                if _cc_execution_started:
                    # Keep the existing native timeout/checkpoint contract.
                    # Runtime exceptions may contain the full command/prompt.
                    raise
                _message = f"CC 原生运行隔离或工具配置未就绪：{type(exc).__name__}: {exc}"
                return {"status": "error", "final": "", "error": _message,
                        "fail_kind": "harness_runtime", "model_used": ctx.model,
                        "elapsed": round(_t.time() - t0, 1),
                        "error_envelope": {"category": "harness_runtime",
                                           "retryable": False, "scope": "harness",
                                           "tool": "cc-native-runtime",
                                           "code": "native-runtime-prepare",
                                           "message": _message}}
        else:
            # Standalone callers retain their existing native configuration and
            # do not require the service's deployment-only execution module.
            _cc_clean_settings = None
            try:
                import tempfile as _tf
                _fd, _cc_clean_settings = _tf.mkstemp(
                    prefix="legion_cc_clean_", suffix=".json")
                with os.fdopen(_fd, "w") as _f:
                    _json.dump({"hooks": {}}, _f)
                cmd += ["--settings", _cc_clean_settings]
            except Exception:
                _cc_clean_settings = None
            try:
                r = _execute_cc(cmd, _cc_env)
            finally:
                if _cc_clean_settings:
                    try:
                        os.remove(_cc_clean_settings)
                    except Exception:
                        pass
        final, _cc_status, _cc_facts, _cc_native_events, _observed_sid, _cc_auth_failed = (
            _cc_native_terminal(r.stdout, r.returncode, getattr(r, "cancelled", False)))
        if _managed_browser is not None:
            _cc_facts["managed_browser"] = {
                "closed": _managed_browser.closed,
                "observations": list(_managed_browser.observations),
                "cleanup": dict(_managed_browser.cleanup),
                "scope": "tool execution facts; not proof that the model adopted the observations",
            }
        if _cc_mailbox is not None:
            _cc_facts["live_input"] = {
                "enabled": True, "error": str(getattr(r, "native_stdin_error", "") or ""),
                "receipt_scope": "native input/turn acknowledgement, not factual adoption",
            }
        _native_sid_out = _observed_sid or _resume_id
        _cc_result = _cc_facts.get("turn_result") or {}
        _cc_usage = dict(_cc_result["usage"]) if isinstance(_cc_result.get("usage"), dict) else {}
        _cc_model_usage = _cc_result.get("modelUsage") if isinstance(_cc_result.get("modelUsage"), dict) else {}
        _cc_actual_models = list(_cc_model_usage)
        _cc_actual_model = _cc_actual_models[-1] if _cc_actual_models else getattr(ctx, "model", None)
        # CCR 的 provider,model 是路由身份，不是另一个模型名；保留原始 usage 键供追溯。
        if _deployment_ccr_model and _cc_actual_model == _deployment_ccr_model:
            _cc_actual_model = ctx.model
        if _cc_usage:
            # Claude usage 的 input/cache buckets 彼此独立；总量必须含缓存，不按正文长度估算。
            _cc_usage.setdefault("total_tokens", sum(int(_cc_usage.get(name) or 0) for name in (
                "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")))
            _cc_usage.update({"source": "claude_code_result", "granularity": "task",
                              "model": _cc_actual_model, "models": _cc_actual_models,
                              "cost_usd": _cc_result.get("total_cost_usd"),
                              "duration_api_ms": _cc_result.get("duration_api_ms"),
                              "num_turns": _cc_result.get("num_turns")})
        _cc_failure = {}
        if _cc_status != "ok":
            # Preserve the actual native failure, not just rcN. Raw CLI usage
            # belongs to error context, never the user's delivered answer.
            _errors = _cc_result.get("errors") or []
            _parts = ([str(value) for value in _errors] if isinstance(_errors, list)
                      else [str(_errors)])
            if not _parts and not _cc_facts["completed"]:
                for event in _cc_facts.get("stream_errors", []):
                    detail = event.get("error") or event.get("message") or event.get("errors")
                    if detail:
                        _parts.append(detail if isinstance(detail, str)
                                      else _json.dumps(detail, ensure_ascii=False))
            if _cc_facts["turn_status"] == "failed" and _cc_result.get("result"):
                _parts.append(str(_cc_result["result"]))
            if (r.stderr or "").strip():
                _parts.append(r.stderr.strip())
            if (r.returncode != 0 and not _parts and not _cc_native_events
                    and (r.stdout or "").strip()):
                _parts.append(r.stdout.strip())
            _diagnostic = "\n".join(_parts)
            if not _diagnostic:
                if _cc_status == "cancelled":
                    _diagnostic = "Claude Code invocation cancelled"
                elif _cc_facts["turn_status"] == "unreported":
                    _diagnostic = "Claude Code stream ended without a final result or completed assistant turn"
                else:
                    _diagnostic = (f"Claude Code terminal: {_cc_result.get('subtype') or _cc_status}; "
                                   f"process rc={r.returncode}")
            # Do not expose known request credentials or echo the supplied
            # prompt when a native CLI includes its invocation in an error.
            for _private_value in (prompt, ctx.appid,
                                   _cc_env.get("ANTHROPIC_AUTH_TOKEN"),
                                   _cc_env.get("ANTHROPIC_API_KEY"),
                                   _cc_env.get("LEGION_CCR_AUTH_TOKEN")):
                if _private_value:
                    _diagnostic = _diagnostic.replace(str(_private_value), "[redacted]")
            _cc_failure["error"] = _diagnostic[:4000]
            if (_cc_backend == "codex" and not _cc_native_events
                    and _re.search(r"(?im)^usage:\s+codex\s+sandbox\b", _diagnostic)):
                # Native argument validation failed before CC started. The
                # word "permission" in CLI usage is not a missing user ticket.
                _cc_failure.update(
                    fail_kind="harness_runtime",
                    error_envelope={"category": "harness_runtime", "retryable": False,
                                    "scope": "harness", "tool": "codex-sandbox",
                                    "code": "native-command-arguments",
                                    "message": _cc_failure["error"]})
            _provider_denial = None
            if _cc_status != "cancelled" and not _cc_facts["completed"]:
                try:
                    from error_envelope import (
                        provider_authorization_from_cc_terminal, provider_authorization_fingerprint)
                    _auth_fingerprint = (provider_authorization_fingerprint(ctx.appid, ctx.gateway)
                                         if _deployment_ccr_model else "")
                    _provider_denial = provider_authorization_from_cc_terminal(
                        _redact_cc(_cc_result), expected_route=_deployment_ccr_model,
                        authorization_fingerprint=_auth_fingerprint)
                except ImportError:
                    pass  # Standalone legacy installations keep their existing error handling.
            if _provider_denial:
                if prompt:
                    _provider_denial.message = _provider_denial.message.replace(prompt, "[redacted]")
                _provider_denial.message += "\n提供方已拒绝本次授权；需管理员处理，本请求不自动重试。"
                _cc_status, final = "error", ""
                _cc_failure.update(
                    error=_provider_denial.message, fail_kind=_provider_denial.category,
                    error_envelope=_provider_denial.to_dict())
            elif _cc_auth_failed and _cc_status != "cancelled" and not _cc_facts["completed"]:
                message = "Claude Code 原生认证失败；请修复该成员的登录或 Anthropic 兼容入口，换模型不能补齐认证。"
                _cc_status, final = "error", ""
                _cc_failure.update(
                    error=message, fail_kind="harness_runtime",
                    error_envelope={"category": "harness_runtime", "retryable": False,
                                    "scope": "harness", "tool": "claude",
                                    "code": "authentication_failed", "message": message})
        return {**_cc_failure,
                "final": _redact_cc(final[:_FINAL_CAP]), "status": _cc_status,
                "elapsed": round(_t.time() - t0, 1), "usage": _cc_usage or None,
                # provenance(同 deepseek 修复:cc adapter 旧版唯一没回 model_used→run_one 退回 tried_models[-1]
                # 记到 cc 名下污染 model_health)。cc 用父层给的 ctx.model·如实回传。
                "model_used": _cc_actual_model, "native_events": _cc_native_events,
                "native_facts": _redact_cc(_cc_facts),
                "workdir": str(ctx.workdir or ""),
                "native_checkpoint": ({"kind": "claude_session", "session_id": _native_sid_out,
                                       "resume_command": f"claude --bare --resume {_native_sid_out}"}
                                      if _native_sid_out else None),
                "resumed_native": bool(_resume_id)}
    except _sp.TimeoutExpired as _te:
        # provenance 一致(同主 return 补 model_used)+ P0-2(cc 有 claude_session·timeout 尽力保 session_id
        # 让下一轮 resume·session 现场在磁盘·不从零重开)。session id 从 try 内已赋值的取·没有则不带 checkpoint。
        _, _, _facts, _events, _seen_sid, _ = _cc_native_terminal(
            getattr(_te, "legion_stdout", ""), None)
        _facts["parent_timeout"] = True
        _cc_sid = _seen_sid or locals().get("_native_sid_out") or locals().get("_native_sid") or ""
        return {**_runtime_timeout(_te), "final": "", "status": "timeout", "elapsed": round(_t.time() - t0, 1),
                "model_used": getattr(ctx, "model", None),
                "native_facts": _redact_cc(_facts), "native_events": _events,
                "workdir": str(ctx.workdir or ""),
                "native_checkpoint": ({"kind": "claude_session", "session_id": _cc_sid} if _cc_sid else None)}
    except FileNotFoundError:
        return {"final": "", "status": "unavailable", "elapsed": 0.0, "error": "claude CLI 未安装"}


import contextlib as _ctx


def _find_search_cli():
    """找共享 search_fabric_cli.py。主仓 comparison/·dist tools/。"""
    here = _P(os.path.dirname(os.path.abspath(__file__)))
    for candidate in (here.parent / "tools" / "search_fabric_cli.py",
                      here.parent / "comparison" / "search_fabric_cli.py",
                      here.parent.parent / "harness_baselines" / "comparison" / "search_fabric_cli.py"):
        if candidate.exists():
            return str(candidate)
    return None


def _find_bridge():
    """找旧 Chat-only 网关兼容用的 responses_chat_bridge.py。
    dist 打包放 tools/·主仓在 comparison/。找不到返回 None(codex 该家 unavailable)。"""
    _here = _P(os.path.dirname(os.path.abspath(__file__)))
    for c in (_here.parent / "tools" / "responses_chat_bridge.py",       # dist: legion-dist/tools/
              _here.parent.parent / "harness_baselines" / "comparison" / "responses_chat_bridge.py",
              _here.parent / "comparison" / "responses_chat_bridge.py"):
        if c.exists():
            return str(c)
    return None


@_ctx.contextmanager
def _codex_bridge_ctx(port=18756, env=None):
    """自启动/复用 codex bridge。找不到 bridge 文件则 yield False(codex unavailable)。"""
    import socket
    bridge = _find_bridge()

    def _up():
        with _ctx.suppress(Exception):
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        return False
    if _up():
        yield True; return
    if not bridge:
        yield False; return
    # ★ 死代理根治(2026-08-31 真实server探针揪出·codex反复rc1"high demand"的真根因):
    #   codex CLI 连的是本地 bridge(127.0.0.1)·bridge 再转发到 aigc.sankuai.com 内网网关。
    #   _gateway_no_proxy 之前只给了 codex CLI 子进程的 env·【漏了 bridge 进程自己】——
    #   bridge 起时不传 env·继承父进程死代理(HTTP_PROXY/ALL_PROXY 指向没起的本地代理端口)→
    #   bridge 转发网关走死代理连不上→返回给 codex "We're experiencing high demand"(重试兜底
    #   文案·真底层是连不上)→codex reconnect 5次全失败 rc1。实测:绕开死代理 codex 一次35s真交付done。
    #   修:给 bridge 进程 env 也套 _gateway_no_proxy(网关域名进 NO_PROXY·内网直连·外网仍走原代理)。
    _bridge_env = _gateway_no_proxy(dict(env or os.environ))
    # ★ 服务器实锤(2026-09-10 test02):裸 'python3.12' 依赖 PATH——镜像容器 PATH 无
    # toolbox/python312/bin → FileNotFoundError → codex bridge 起不来 → codex 全灭。
    # 修:sys.executable(军团主进程自己的解释器·永远存在)优先,which 兜底,裸名最后。
    _py = None
    try:
        _py = _sys.executable
    except Exception:
        pass
    if not _py or not _P(_py).exists():
        _py = _sh.which("python3.12") or "python3.12"
    proc = _sp.Popen([_py, bridge, "--port", str(port)],
                     stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, env=_bridge_env)
    try:
        for _ in range(60):
            if _up():
                break
            _t.sleep(0.5)
        yield _up()
    finally:
        with _ctx.suppress(Exception):
            proc.terminate(); proc.wait(timeout=5)


_CODEX_FALLBACK_RESOURCE = (
    _P(__file__).resolve().parent / "resources" / "codex-0.153.4-fallback.json")
_CODEX_FALLBACK_SHA256 = "ac8ae107a0d72fe3476b430afb161ea4e67da2e446d778aefc44828160559807"


def _codex_bound_model_catalog(model, *, environment=None, apply_patch_tool_type=None):
    """One authorized model, not the bundled OpenAI catalog or a capability claim.

    Codex 0.153.4 requires a full descriptor with instructions. Retain its exact
    generic unknown-model fallback instructions, not a GPT-specific template.
    The executor and output truncation match native fallback defaults; unknown
    context windows and reasoning levels are not invented. Input modalities come
    only from the registry declaration for this exact bound model.
    Patch support comes only from the validated, matched operator binding.
    """
    import hashlib

    if not isinstance(model, str) or not model.strip() or model != model.strip():
        raise ValueError("personal Codex catalog requires a bound model")
    resource = _json.loads(_CODEX_FALLBACK_RESOURCE.read_text(encoding="utf-8"))
    instructions = resource.get("instructions")
    if (not isinstance(instructions, str)
            or hashlib.sha256(instructions.encode("utf-8")).hexdigest() != _CODEX_FALLBACK_SHA256):
        raise ValueError("Codex 0.153.4 generic fallback resource failed integrity verification")
    modalities = ["text"]
    try:
        from model_registry import input_modalities
        if environment is None:
            declared = input_modalities(model)
        elif (not isinstance(environment, dict)
              or environment.get("LEGION_CODEX_TRANSPORT", "").strip().lower() != "responses"):
            declared = None
        else:
            declared = input_modalities(model, environment=environment)
    except (ImportError, TypeError, ValueError):
        declared = None
    # The personal declaration currently supports these native Codex enum values.
    # Reject the whole invalid declaration; never borrow a default/other model.
    if (isinstance(declared, list) and declared
            and all(isinstance(item, str) and item in ("text", "image") for item in declared)):
        modalities = [item for item in ("text", "image") if item in declared]
    catalog = {"models": [{
        "slug": model, "display_name": model,
        "base_instructions": instructions,
        "supported_reasoning_levels": [],
        "shell_type": "unified_exec", "visibility": "list",
        # This is Codex's API-picker inclusion flag, not proof of provider availability.
        "supported_in_api": True, "priority": 0,
        "support_verbosity": False,
        "truncation_policy": {"mode": "bytes", "limit": 10000},
        "experimental_supported_tools": [], "input_modalities": modalities,
    }]}
    if apply_patch_tool_type == "freeform":
        catalog["models"][0]["apply_patch_tool_type"] = apply_patch_tool_type
    return catalog


def _codex_adapter(qhash, question, gold="", ctx=None):
    # ctx 化(权限下放·复用 penguin 已验证模式):model 用 per-call ctx.model 替全局单值,
    # 前几轮失败 _prior_attempts_prelude 拼进 prompt(codex 本就靠 --ephemeral 每单新进程,
    # 失败回流让它看见"上单怎么挂"自主换招·依据 mcp_tool_call.rs:875 失败进 history)。
    # 老调用(不传 ctx)走 from_env() 退回读 env 全局 → 零回归。
    supplied_ctx = ctx is not None
    ctx = ctx or HarnessCtx.from_env()
    t0 = _t.time()
    _here = _P(__file__).resolve().parent
    _app_root = _here.parent.parent
    # The same operator-owned published layout used by Hermes; a task cannot
    # opt out of the deployed boundary merely by changing a browser flag.
    _managed_linux = _sys.platform.startswith("linux") and (
        (_here.parent.name == "legion" and (_here.parent / "legion-service").is_dir())
        or any((_app_root / name).exists()
               for name in ("image-payload.json", "runtime-ref.json")))
    _checkpoint = (ctx.task_memory or {}).get("native_checkpoint") or {}
    _resume_id = str(_checkpoint.get("session_id") or "") if isinstance(_checkpoint, dict) else ""
    _scope = ctx.task_facts or {}
    _read_only = _scope.get("native_read_only", False)
    # Boolean opt-in can only reduce authority. A fresh request cannot import a
    # conversation checkpoint, even if a caller accidentally supplies one.
    if (type(_read_only) is not bool or (_read_only and (
            _scope.get("native_session_scope") != "request"
            or not _scope.get("native_session_request_id") or _resume_id))):
        return {"status": "unavailable", "final": "", "elapsed": 0.0,
                "error": "只读辅助执行需要独立请求，不能携带原生会话历史",
                "error_envelope": {"category": "native_session_unavailable", "scope": "request",
                                   "retryable": False, "code": "invalid_read_only_scope"}}
    _managed_browser = None
    _managed_cleanup_errors = []
    _native_started = False
    _provider_binding_id = None
    _prepare_stage = "native_scope"
    _mcp_secrets = []
    _captured_final = ""
    _usage_cursor = None

    def _redact(value):
        for secret in _mcp_secrets:
            value = value.replace(secret, "[REDACTED]")
        return value

    def _runtime_facts(facts):
        if _output_budget_facts:
            facts["declared_output_budget"] = dict(_output_budget_facts)
        if _auto_compact_token_limit is not None:
            facts["declared_context_compaction"] = {
                "model_auto_compact_token_limit": _auto_compact_token_limit,
                "source": "matched_operator_binding",
                "scope": "native_runtime_policy_not_model_capacity",
            }
        if _managed_linux:
            facts["managed_native"] = {
                "requested": True, "native_cli_started": _native_started,
                "prepare_stage": _prepare_stage,
                "kernel_boundary_verification": "unverified",
            }
        if _managed_browser is not None:
            facts["managed_browser"] = {
                "closed": _managed_browser.closed,
                "observations": list(_managed_browser.observations),
                "cleanup": dict(_managed_browser.cleanup),
            }
        if _managed_cleanup_errors:
            facts["managed_cleanup_errors"] = list(_managed_cleanup_errors)
        return facts

    def _enter_owned(stack, scope, label):
        value = scope.__enter__()

        def close(exc_type, exc, tb):
            try:
                scope.__exit__(exc_type, exc, tb)
            except Exception as error:
                # Cleanup is a separate runtime fact, never a replacement for
                # an obtained native final, checkpoint, usage or cancellation.
                _managed_cleanup_errors.append({"scope": label, "error_type": type(error).__name__})
            return False

        stack.push(close)
        return value

    def _prepare_failure(error):
        detail = str(error) if type(error).__name__ == "NativeProcessSandboxError" else type(error).__name__
        message = _redact(f"Codex 受管运行或工具配置未就绪（{_prepare_stage}）：{detail}")
        return {"status": "error", "final": "", "error": message,
                "elapsed": round(_t.time() - t0, 1), "model_used": ctx.model,
                "native_checkpoint": _checkpoint or None, "resumed_native": False,
                "native_facts": _runtime_facts({"environment_verification": "unverified"}),
                "error_envelope": {"category": "harness_runtime", "retryable": False,
                                   "scope": "harness", "tool": "codex-native-runtime",
                                   "code": "native-runtime-prepare", "message": message}}

    def _native_output(stdout, returncode=None):
        # 只收机器记录，不读取 final 判任务质量。turn 完结不等于工具成功或作品完成。
        # 本入口没有同执行端的 sandbox 探测，故不推断环境可用性或合成 harness_runtime。
        facts = {"process_returncode": returncode, "turn_status": "unreported",
                 "tool_results": [], "environment_verification": "unverified"}
        thread_id, usage, event_types = "", None, []
        pending, closed = {}, set()
        tool_types = {"command_execution", "file_change", "mcp_tool_call",
                      "web_search", "collab_tool_call"}
        for line in (stdout or "").split("\n"):
            try:
                obj = _json.loads(line)
                if not isinstance(obj, dict):
                    continue
                kind = str(obj.get("type") or "")
                if kind and kind not in event_types:
                    event_types.append(kind)
                if kind == "thread.started" and obj.get("thread_id"):
                    thread_id = str(obj["thread_id"])
                if kind in ("turn.started", "turn.completed", "turn.failed"):
                    facts["turn_status"] = kind.split(".", 1)[1]
                    facts["turn_result"] = obj
                item = obj.get("item")
                if kind == "item.completed" and isinstance(item, dict):
                    item_id = item.get("id")
                    if isinstance(item_id, str) and item_id:
                        pending.pop(item_id, None)
                        closed.add(item_id)
                    if item.get("type") in tool_types:
                        facts["tool_results"].append(item)
                    elif item.get("type") == "error":
                        facts.setdefault("errors", []).append(obj)
                elif (kind in ("item.started", "item.updated") and isinstance(item, dict)
                      and item.get("type") in tool_types):
                    item_id = item.get("id")
                    if isinstance(item_id, str) and item_id and item_id not in closed:
                        row = pending.pop(item_id, {})
                        row.update(type=item["type"], last_event=kind, terminal_event="not_received")
                        truncated = set(row.get("truncated_fields") or [])
                        # stdout has already passed the existing secret redactor.
                        # Keep only short identity/command facts, not payloads or
                        # a historical in_progress status posing as current life.
                        for field, limit in (("id", 128), ("command", 512),
                                             ("server", 128), ("tool", 128)):
                            if isinstance(item.get(field), str):
                                raw = item[field].encode("utf-8")
                                row[field] = raw[:limit].decode("utf-8", errors="ignore")
                                truncated.discard(field)
                                if len(raw) > limit:
                                    truncated.add(field)
                        if truncated:
                            row["truncated_fields"] = sorted(truncated)
                        else:
                            row.pop("truncated_fields", None)
                        pending[item_id] = row
                elif kind == "error":
                    facts.setdefault("errors", []).append(obj)
                if kind == "turn.completed" and isinstance(obj.get("usage"), dict):
                    raw_usage = dict(obj["usage"])
                    raw_usage.setdefault("total_tokens", int(raw_usage.get("input_tokens") or 0)
                                         + int(raw_usage.get("output_tokens") or 0))
                    usage = {**raw_usage, "source": "codex_turn_completed",
                             "granularity": "task", "model": ctx.model}
            except (TypeError, ValueError):
                continue
        # Reconcile after the entire captured stdout, including late terminals.
        # Missing terminal evidence is neither process liveness nor task failure.
        rows = list(pending.values())[-32:]
        while rows and len(_json.dumps(rows, ensure_ascii=False).encode("utf-8")) > 8192:
            rows.pop(0)
        facts["pending_tool_results"] = rows
        if len(pending) > len(rows):
            facts["pending_tool_results_omitted"] = len(pending) - len(rows)
        completion = {"state": "unknown", "thread_id": thread_id or _resume_id}
        try:
            import token_telemetry as telemetry
            usage = telemetry.codex_usage_after(
                _usage_cursor or {}, thread_id or _resume_id, usage,
                resumed=bool(_resume_id), terminal=facts["turn_status"], model=ctx.model,
                completion=completion)
        except (ImportError, OSError, ValueError, TypeError):
            completion = {"state": "unknown", "thread_id": thread_id or _resume_id}
            if _resume_id and usage:
                usage = {"native_counter": usage, "granularity": "thread_cumulative",
                         "usage_scope": "native_increment_unknown", "complete": False,
                         "available": False, "coverage": "unavailable", "model": ctx.model}
        if isinstance(completion.get("last_agent_message"), str):
            message = _redact(completion["last_agent_message"])
            completion["last_agent_message"] = message[:_FINAL_CAP]
            if len(message) > _FINAL_CAP:
                completion["truncated"] = True
        facts["completion_message"] = completion
        return thread_id, usage, event_types, facts

    def _reconcile_last_message(text, facts):
        # `-o` is the last assistant message, which may be old progress text.
        # Only an unambiguous public terminal event can replace that fallback.
        text = _redact(text)
        facts["last_message_output"] = {"text": text[:_FINAL_CAP]}
        if len(text) > _FINAL_CAP:
            facts["last_message_output"]["truncated"] = True
        completion = facts.get("completion_message") or {}
        if completion.get("state") in ("present", "absent"):
            return completion.get("last_agent_message") or ""
        return text

    binp = _sh.which("codex") or "/Applications/Codex.app/Contents/Resources/codex"
    if not _managed_linux and not (_sh.which("codex") or _P(binp).exists()):
        return {"final": "", "status": "unavailable", "elapsed": 0.0, "error": "codex CLI 未安装"}
    import tempfile
    out = None if _managed_linux else _P(tempfile.gettempdir()) / f"legion_codex_{qhash}.txt"
    if out is not None:
        with _ctx.suppress(Exception):
            out.unlink()
    env = dict(os.environ)
    if _managed_linux:
        env.update(getattr(_INPROC_RUNTIME_ENV, "values", {}) or {})
    # Match _run_live's per-call overlay, even outside the managed layout.
    # A named key may be AIGC_APPID: retain the snapshot before legacy rewrites.
    operator_env = dict(env)
    operator_env.update(getattr(_INPROC_RUNTIME_ENV, "values", {}) or {})
    # appid 优先用 ctx.appid(单一事实源·2026-09-01 揪出:旧版只读 env MEYO_UNIFIED_APPID/AIGC_APPID·
    # 完全忽略 ctx.appid→①ctx 里场景选模型分配的独立 appid 没传到 codex CLI(全挤 env 那一个·并发
    # 撞限流)②env 没设 appid 时即使 ctx.appid 有值 codex 也 rc1"Missing AIGC_APPID"。ctx 是权限下放
    # 单一事实源·父层给什么 appid 就用什么·env 只作兜底)。
    env["AIGC_APPID"] = (getattr(ctx, "appid", "") or os.environ.get("MEYO_UNIFIED_APPID")
                         or env.get("AIGC_APPID", "") or _UNIFIED_APPID)
    env["MEYO_UNIFIED_GATEWAY"] = ctx.gateway or env.get("MEYO_UNIFIED_GATEWAY") or _UNIFIED_GATEWAY
    # Codex 本身会按 --session-id 生成 prompt_cache_key；同时把父层统一哈希槽交给 bridge，
    # 便于定制/旧版客户端仍能透传同一身份隔离键。
    if ctx.prompt_cache_key:
        env["MEYO_PROMPT_CACHE_KEY"] = ctx.prompt_cache_key
    # rc-9 根治(2026-08-28 实测):并发多个 codex worker 共用固定端口 18756 的单例 bridge →
    # worker A 结束时 finally 里 terminate 掉 bridge,把 worker B 正在用的 bridge 一起杀了 →
    # B 的 codex CLI 连不上 bridge 被 SIGKILL(rc-9)。单家跑从不 rc-9,并发才炸=典型共享单例
    # 提前销毁。修:每个 worker 进程用【基于 PID 派生的唯一端口】各起各的 bridge,互不干扰,
    # finally 只杀自己那个。这样并发多家不再互相踩死(能自愈能继续搞定·不是反复被杀)。
    _bridge_port = 18756 + (os.getpid() % 4000)
    # 官方 Friday 已提供 Responses；优先保留原生输入、工具/推理项及 SSE，
    # 不再把整段 Chat 回答重新拼成“流式”。只对文档明确的网关启用。
    # 显式 bridge 是部署回退，不在请求失败时静默切协议或重开原生会话。
    _transport = (getattr(ctx, "model_protocol", "")
                  or env.get("LEGION_CODEX_TRANSPORT", "auto")).strip().lower()
    if _transport not in ("auto", "bridge", "responses"):
        return {"final": "", "status": "unavailable", "elapsed": 0.0,
                "error": "LEGION_CODEX_TRANSPORT 必须为 auto、bridge 或 responses"}
    _endpoint = None
    _catalog_environment = {}
    _output_budget = None
    _output_budget_facts = {}
    _stream_idle_timeout_ms = None
    _apply_patch_tool_type = None
    _auto_compact_token_limit = None
    _error_compatibility_profile = None
    _transport_activity_fd = None
    if _transport == "responses":
        try:
            import model_registry as _models
            declaration_binding = None
            if not supplied_ctx:
                binding = _models.explicit_responses_binding(operator_env)
                ctx.model, ctx.gateway, ctx.appid = (
                    binding["model"], binding["base_url"], binding["api_key"])
                declaration_binding = binding
            _endpoint = _models.responses_endpoint(ctx.model, ctx.gateway, explicit=True)
            key = getattr(ctx, "appid", "")
            if (_endpoint is None or not isinstance(key, str) or not key.strip()
                    or key != key.strip()
                    or any(ord(char) < 32 or ord(char) == 127 for char in key)):
                raise ValueError()
            if supplied_ctx:
                try:
                    declaration_binding = _models.explicit_responses_binding(operator_env)
                except (AttributeError, TypeError, ValueError) as binding_error:
                    if getattr(binding_error, "binding_option", None) in (
                            "stream_idle_timeout_ms", "apply_patch_tool_type",
                            "model_auto_compact_token_limit", "error_compatibility_profile"):
                        raise
                    try:
                        declared = _json.loads(operator_env.get("LEGION_CODEX_RESPONSES_BINDING") or "{}")
                    except (TypeError, ValueError):
                        declared = {}
                    if isinstance(declared, dict) and "max_output_tokens" in declared:
                        raise  # An invalid explicit budget cannot silently become an uncapped call.
                    pass  # A valid ctx may have no matching optional declaration.
            if declaration_binding and (
                    declaration_binding["model"], declaration_binding["base_url"],
                    declaration_binding["api_key"]) == (ctx.model, _endpoint["base_url"], key):
                _catalog_environment = {
                    **operator_env, "LEGION_CODEX_TRANSPORT": "responses"}
                _output_budget = declaration_binding.get("max_output_tokens")
                _stream_idle_timeout_ms = declaration_binding.get("stream_idle_timeout_ms")
                _apply_patch_tool_type = declaration_binding.get("apply_patch_tool_type")
                _auto_compact_token_limit = declaration_binding.get("model_auto_compact_token_limit")
                _error_compatibility_profile = declaration_binding.get("error_compatibility_profile")
                if _output_budget is not None:
                    _output_budget_facts = {
                        "max_output_tokens": _output_budget,
                        "source": "matched_operator_binding",
                        "policy": "fill_if_native_request_omits",
                    }
        except (ImportError, AttributeError, TypeError, ValueError) as configuration_error:
            option_message = (
                "显式 Responses 的 model_auto_compact_token_limit 须为 "
                "1..9223372036854775807 的整数；未启动原生调用"
                if getattr(configuration_error, "binding_option", None)
                == "model_auto_compact_token_limit" else None)
            return {"final": "", "status": "unavailable", "elapsed": 0.0,
                    "error": option_message or "显式 Responses 的地址或授权不可用；未启动原生调用",
                    "error_envelope": {"category": "model_config", "scope": "request",
                                       "retryable": False,
                                       "message": option_message or "显式 Responses 配置不可用"}}
        # Opaque request key: do not expand company aliases or borrow env defaults.
        env["AIGC_APPID"] = key
        env["MEYO_UNIFIED_APPID"] = key
        env["MEYO_UNIFIED_GATEWAY"] = _endpoint["base_url"]
        env["MEYO_FALLBACK_MODELS"] = "[]"
        ctx.fallback_models = []
        _provider_binding_id = _models.ccr_binding_key(ctx.model, key, _endpoint["base_url"])
    elif _transport == "auto":
        try:
            import model_registry as _models
        except ImportError:
            _models = None  # 老独立发行物未带模型目录时保留原兼容路径。
        if _models is not None:
            _endpoint = _models.responses_endpoint(ctx.model, env["MEYO_UNIFIED_GATEWAY"])
            if _endpoint:
                _key = _models._resolve_alias(str(env["AIGC_APPID"]).strip())
                if not _key:
                    return {"final": "", "status": "unavailable", "elapsed": 0.0,
                            "error": "Friday Responses 缺少本请求可解析的模型授权"}
                env["AIGC_APPID"] = _key
    # Register the native request credential after transport resolution, not
    # only for explicit Responses. Normal and timeout observations share this
    # redactor; the provider selection and credential passed to it stay intact.
    _request_key = env.get("AIGC_APPID")
    if isinstance(_request_key, str) and _request_key:
        _mcp_secrets.append(_request_key)
    _provider_id = ("meyo_responses" if _transport == "responses" else
                    "friday_responses" if _endpoint else "aigc_bridge")
    _provider = (f'name = "Meyo Responses", base_url = {_json.dumps(_endpoint["base_url"])}, '
                 'env_key = "AIGC_APPID", wire_api = "responses"') if _endpoint else (
                 'name = "AIGC Bridge", '
                 f'base_url = "http://127.0.0.1:{_bridge_port}/v1", '
                 'env_key = "AIGC_APPID", wire_api = "responses"')
    if _endpoint and _transport == "auto":
        _headers = ", ".join(f"{_json.dumps(k)} = {_json.dumps(v)}"
                             for k, v in _endpoint["http_headers"].items())
        _provider += (f", http_headers = {{ {_headers} }}, "
                      'env_http_headers = { "Mt-Session-Id" = "MEYO_FRIDAY_SESSION_ID" }')
        # 缓存/追踪身份由用户+逻辑会话+成员派生；不拿默认服务用户冒充美团用户身份，
        # 更不把某个用户的资源 token 放进共享 provider 或命令行。
        env["MEYO_FRIDAY_SESSION_ID"] = ctx.prompt_cache_key or prompt_cache_key(
            ctx.user_id, ctx.session_id or qhash, "codex")
    if (_resume_id and (_provider_binding_id or _checkpoint.get("provider_binding"))
            and _checkpoint.get("provider_binding") != _provider_binding_id):
        return {"final": "", "status": "unavailable", "elapsed": 0.0,
                "error": "原生会话的提供方绑定缺失或变化；未发送旧历史",
                "native_checkpoint": _checkpoint, "resumed_native": False,
                "error_envelope": {"category": "native_session_unavailable", "scope": "request",
                                   "retryable": False,
                                   "message": "原生会话的提供方绑定缺失或变化"}}
    _connection = _ctx.nullcontext(True) if _endpoint else _codex_bridge_ctx(_bridge_port, env=env)
    try:
        with _ctx.ExitStack() as _owned_tools:
            _launch = None
            _tool_env = dict(env)
            _browser_requested = False
            if (not _read_only and (_managed_linux or _sys.platform == "darwin")
                    and ctx.execution_depth != "quick"):
                _prepare_stage = "browser_configuration"
                mode = str(_tool_env.get("LEGION_MANAGED_BROWSER") or "").lower()
                if mode not in ("", "0", "false", "no", "1", "true", "yes"):
                    raise ValueError("unknown managed browser mode")
                _browser_requested = mode in ("1", "true", "yes")
            _local_browser = _browser_requested and not _managed_linux
            _browser_config_dir = None
            if _managed_linux:
                from native_process_sandbox import prepare_codex_native, NativeProcessSandboxError
                if not env.get("HOME") or not env.get("MEYO_RUNTIME_ROOT"):
                    raise NativeProcessSandboxError("managed Codex requires an explicit native HOME and runtime")
                runtime = _P(env["MEYO_RUNTIME_ROOT"]).resolve(strict=True)
                linked_runtime = _app_root / "runtime"
                if linked_runtime.exists() and linked_runtime.resolve() != runtime:
                    raise NativeProcessSandboxError("configured Codex runtime differs from the published runtime")
                media_root = str(_P(env.get("MEYO_LEGION_DIR") or _P(env["HOME"], ".meyo", "legion")))
                codex_ws = _P(ctx.workdir or _P(media_root, "outputs", f"codex_{qhash}")).resolve()
                protected = [_app_root.resolve(), runtime]
                protected.extend(_P(env[name]).resolve(strict=True)
                                 for name in ("MEYO_TOOLBOX_ROOT", "LEGION_PROC_REGISTRY_DIR")
                                 if env.get(name) and _P(env[name]).exists())
                if any(codex_ws.is_relative_to(p) or p.is_relative_to(codex_ws) for p in protected):
                    raise NativeProcessSandboxError("Codex workspace overlaps protected runtime or control roots")
                codex_ws.mkdir(parents=True, exist_ok=True)
                state_roots = [env[name] for name in
                               ("HOME", "CODEX_HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR")
                               if env.get(name)]
                _launch = _enter_owned(_owned_tools, prepare_codex_native(
                    env, str(codex_ws), protected_roots=protected,
                    writable_state_roots=state_roots,
                    **({"read_only": True} if _read_only else {})), "native_launch")
                binp = str(_launch.executable)
                env = dict(_launch.env)
                out = _P(_launch.config_dir) / "codex-final.txt"
                codex_question = (_prior_attempts_prelude(ctx.prior_attempts) + question +
                    "\n\n【你的权限与现场】本次原生工具的写范围是工作目录、已声明的状态目录和私有临时目录；"
                    "宿主程序、运行材料及控制目录不在任务写范围内。"
                    f"当前工作目录 {codex_ws} 是持久目录；给用户带走的文件直接保存在这里，"
                    "最终回复中写出真实关键产物路径。")
            else:
                media_root = str(_P.home() / ".meyo" / "legion")
                _P(media_root).mkdir(parents=True, exist_ok=True)
                # Retain the existing development grant only when this identity
                # can use it; immutable published source is not a writable task root.
                repo_root = str(_P(os.path.dirname(os.path.abspath(__file__))).parent.parent)
                codex_ws = _P(ctx.workdir or _P(media_root, "outputs", f"codex_{qhash}")).resolve()
                codex_ws.mkdir(parents=True, exist_ok=True)
                _access_options = ({"effective_ids": True}
                                   if os.access in os.supports_effective_ids else {})
                _source_writable = (not _read_only and not _local_browser and
                                    os.access(repo_root, os.W_OK | os.X_OK, **_access_options))
                _write_roots = [media_root, str(codex_ws)]
                if _source_writable:
                    _write_roots.insert(1, repo_root)
                if _local_browser:
                    # Reuse the same owner-side browser, never relax the native
                    # shell to launch Chrome or grant it the host tool's source.
                    store = _P(_tool_env.get("MEYO_RUNTIME_STORE") or "")
                    native_tmp = _P(env.get("TMPDIR") or "")
                    if not store.is_absolute() or not native_tmp.is_absolute():
                        raise ValueError("local browser requires explicit control and native temporary roots")
                    store, native_tmp = store.resolve(strict=True), native_tmp.resolve(strict=True)
                    mutable = [_P(root).resolve() for root in _write_roots]
                    mutable.extend(_P(env[name]).resolve() for name in
                                   ("HOME", "CODEX_HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR")
                                   if env.get(name))
                    if (not store.is_dir() or not native_tmp.is_dir()
                            or native_tmp in (_P("/tmp").resolve(), _P("/"))
                            or any(store.is_relative_to(root) for root in [*mutable, native_tmp])
                            or _P(repo_root).resolve().is_relative_to(native_tmp)):
                        raise ValueError("local browser control must be outside native-writable roots")
                    _write_roots.append(str(native_tmp))
                    _browser_config_dir = _owned_tools.enter_context(tempfile.TemporaryDirectory(
                        prefix="meyo-local-tools-", dir=str(store)))
                _writable = _json.dumps(_write_roots)
                _source_access = (
                    f"你对整个仓库 {repo_root} 有读写权限,可自由读代码/建文件/改文件——"
                    "和总指挥 CC 一样的全仓库权限,自己判断怎么用。"
                    if _source_writable else
                    f"程序源码目录 {repo_root} 当前不可写；它不是本次任务的可写工程目录。")
                codex_question = (_prior_attempts_prelude(ctx.prior_attempts) + question +
                    "\n\n【你的权限与现场】" + _source_access +
                    f"你当前的工作目录 {codex_ws} 是【持久目录】(不会被删):要给用户下载/带走的文件"
                    f"(PDF/Excel/HTML/代码/媒体等)直接在工作目录里建即可,天然保存;在最终回复里写出关键产物的绝对路径。")
                _P(media_root, "outputs").mkdir(parents=True, exist_ok=True)
            if _read_only:
                codex_question = (question +
                    "\n\n【本次原生权限】read-only：不能写原作品、源码、工作目录或临时目录；"
                    "未挂载外部 MCP/浏览器等可写能力，不恢复其它请求的原生历史。"
                    "回复和 checkpoint 仍由宿主记录。")
            _shared_spec = None
            if not _read_only and not _managed_linux and env.get("MEYO_NATIVE_CAPABILITY_IDS"):
                from native_capability_host import native_stdio_spec
                _shared_spec = native_stdio_spec(
                    ctx, _tool_env, _here / "meyo_mcp_servers" / "orchestrator_server.py")
                if _shared_spec:
                    # Host code must not also be writable by its caller's shell.
                    _writable = _json.dumps(
                        [media_root, str(codex_ws)] + ([str(native_tmp)] if _local_browser else []))
                    codex_question = (_prior_attempts_prelude(ctx.prior_attempts) + question +
                        f"\n\n当前持久工作目录是 {codex_ws}；文件写入本工作目录。"
                        "已授权的外部能力通过原生 meyo_capabilities MCP 调用；"
                        "它在受管宿主执行原检查。不要在命令沙箱内直接运行其内部协议，"
                        "或绕过失败的权限/身份检查。普通文件读写继续使用原生命令工具。")
            _prepare_stage = "provider_connection"
            ok = _owned_tools.enter_context(_connection)
            if not ok:
                if _managed_linux:
                    raise NativeProcessSandboxError("Codex provider bridge unavailable")
                return {"final": "", "status": "unavailable", "elapsed": round(_t.time() - t0, 1),
                        "error": "codex bridge 起不来(responses_chat_bridge.py 缺失或端口占用)"}
            if (_output_budget is not None or _stream_idle_timeout_ms is not None
                    or _error_compatibility_profile is not None):
                _prepare_stage = "output_budget_transport"
                import secrets
                from meyo_harnesses.backends.omnigent_responses_bridge import transparent_bridge
                # Upstream identity was checked above. Codex exec rebuilds its client
                # each invocation; unlike cached Omnigent, it need not pin a local port.
                _budget_transport = _enter_owned(_owned_tools, transparent_bridge({
                    "model": ctx.model, "base_url": _endpoint["base_url"],
                    "api_key": key, "http_headers": {},
                    "stamp": {"sha256": _provider_binding_id},
                    **({"max_output_tokens": _output_budget} if _output_budget is not None else {}),
                    **({"stream_idle_timeout_ms": _stream_idle_timeout_ms}
                       if _stream_idle_timeout_ms is not None else {}),
                    **({"error_compatibility_profile": _error_compatibility_profile}
                       if _error_compatibility_profile is not None else {}),
                }, _tool_env), "output_budget_transport")
                _transport_activity_fd = _budget_transport.get("runtime_activity_fd")
                _budget_token_name = "MEYO_CODEX_OUTPUT_TOKEN_" + secrets.token_hex(8).upper()
                _budget_token = _budget_transport["client_api_key"]
                env[_budget_token_name] = _budget_token
                _mcp_secrets.append(_budget_token)
                _provider = (
                    f'name = "Meyo Responses", base_url = {_json.dumps(_budget_transport["client_base_url"])}, '
                    f'env_key = {_json.dumps(_budget_token_name)}, wire_api = "responses"')
                if _stream_idle_timeout_ms is not None:
                    _provider += f", stream_idle_timeout_ms={_stream_idle_timeout_ms}"
            # Codex 原生续会话：首轮 JSONL 的 thread.started 给出 thread_id；后续 attempt 用
            # `codex exec resume <thread_id> <prompt>`，让工具结果/history/checkpoint 留在原线程，
            # 不再只靠失败文本重建提示。源码依据 exec/src/cli.rs:143-186、exec_events.rs:13,42。
            if _managed_linux:
                common = ["--ignore-user-config", "--skip-git-repo-check",
                          "-C", str(codex_ws), *_launch.native_args, "-m", ctx.model,
                          "-c", f'model_provider="{_provider_id}"',
                          "-c", f"model_providers.{_provider_id}={{ {_provider} }}",
                          "--json", "-o", str(out)]
                _browser_config_dir = _launch.config_dir
            else:
                _sandbox_args = (["-s", "read-only"] if _read_only else [
                    "-s", "workspace-write",
                    "-c", "sandbox_workspace_write.network_access=true",
                    "-c", f'sandbox_workspace_write.writable_roots={_writable}',
                ])
                common = ["--ignore-user-config", "--skip-git-repo-check",
                          "-C", str(codex_ws), *_sandbox_args, "-m", ctx.model,
                          "-c", f'model_provider="{_provider_id}"',
                          "-c", f"model_providers.{_provider_id}={{ {_provider} }}",
                          "--json", "-o", str(out)]
                if _local_browser:
                    common += [
                        "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
                        "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
                    ]
            if _read_only:
                # Native CLI permissions, not a prompt-only restriction. Clear
                # ambient MCP and disable host-side tool sources/hooks; these do
                # not inherit the shell's read-only boundary. Ignore execpolicy
                # allow rules so they cannot authorize an unsandboxed command.
                common += ["--ignore-rules", "-c", 'approval_policy="never"',
                           "-c", "mcp_servers={}"]
                for feature in ("apps", "plugins", "hooks", "multi_agent", "multi_agent_v2"):
                    common += ["-c", f"features.{feature}=false"]
            if _auto_compact_token_limit is not None:
                # Native exec/resume owns compaction; no invented context window,
                # provider identity change, bridge or default threshold.
                common += ["-c", f"model_auto_compact_token_limit={_auto_compact_token_limit}"]
            if _browser_requested:
                _prepare_stage = "browser_configuration"
                import secrets
                from urllib.parse import urlsplit
                from managed_browser import open_managed_browser
                _managed_browser = _enter_owned(_owned_tools, open_managed_browser(
                    ctx, env=_tool_env, config_dir=_browser_config_dir), "managed_browser")
                servers = _managed_browser.mcp_config.get("mcpServers")
                if not isinstance(servers, dict) or len(servers) != 1:
                    raise ValueError("expected one owned browser MCP server")
                server = next(iter(servers.values()))
                url = server.get("url", "")
                parsed = urlsplit(url)
                authorization = (server.get("headers") or {}).get("Authorization", "")
                if (server.get("type") != "http" or parsed.scheme != "http"
                        or parsed.hostname not in ("127.0.0.1", "::1", "localhost")
                        or not parsed.port or parsed.username or parsed.password
                        or parsed.query or parsed.fragment
                        or not authorization.startswith("Bearer ")):
                    raise ValueError("invalid owned browser connection metadata")
                token = authorization[len("Bearer "):]
                if (not token or not token.isascii() or token in url
                        or any(c.isspace() for c in token)):
                    raise ValueError("invalid owned browser token")
                _mcp_secrets.append(token)
                token_name = "MEYO_CODEX_BROWSER_" + secrets.token_hex(8).upper()
                env[token_name] = token
                common += [
                    "-c", "mcp_servers.meyo_playwright.url=" + _json.dumps(url),
                    "-c", "mcp_servers.meyo_playwright.bearer_token_env_var=" + _json.dumps(token_name),
                    "-c", 'mcp_servers.meyo_playwright.omit_tools_from=["deferred"]',
                ]
                # Tool-scoped authorization, not a global approval-policy override.
                # CLI -c splits its left side literally; encode tool keys in TOML values.
                _tool_grants = []
                for tool in getattr(_managed_browser, "tools", ()):
                    name = tool.get("name")
                    if isinstance(name, str) and name:
                        _tool_grants.append(
                            _json.dumps(name, ensure_ascii=False) + '={approval_mode="approve"}')
                if _tool_grants:
                    common += ["-c", "mcp_servers.meyo_playwright.tools={"
                               + ",".join(_tool_grants) + "}"]
            if _endpoint:
                # 原生历史仍由 Codex 本地持久化/恢复，不要求网关保存用户历史。
                common += ["-c", "disable_response_storage=true"]
            if _transport == "responses":
                _prepare_stage = "model_catalog"
                catalog = _codex_bound_model_catalog(
                    ctx.model, environment=_catalog_environment,
                    apply_patch_tool_type=_apply_patch_tool_type)
                # Make the actual native input gate visible to the member.
                # This does not infer the provider's abilities or enable images.
                codex_question += "\n\nNATIVE_MODEL_INPUT_FACTS_JSON\n" + _json.dumps({
                    "model": ctx.model,
                    "input_modalities": catalog["models"][0]["input_modalities"],
                    "source": "this_invocation_model_catalog",
                    "scope": "native_input_gate_only_not_a_provider_capability_test",
                    **({"declared_output_budget": _output_budget_facts} if _output_budget_facts else {}),
                }, ensure_ascii=False)
                # A private per-invocation derivative, never a second active model store.
                # Keep it alive through native execution (including subagents/resume),
                # then let the existing owner scope remove it on success or failure.
                catalog_dir = _owned_tools.enter_context(tempfile.TemporaryDirectory(
                    prefix="meyo-codex-model-",
                    dir=str(_launch.config_dir) if _launch else (
                        env.get("TMPDIR") or tempfile.gettempdir())))
                catalog_path = _P(catalog_dir) / "models.json"
                fd = os.open(catalog_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    _json.dump(catalog, stream, ensure_ascii=False)
                common += ["-c", "model_catalog_json=" + _json.dumps(str(catalog_path))]
            if _shared_spec:
                for _field, _value in _shared_spec.items():
                    if isinstance(_value, dict):
                        _encoded = "{ " + ", ".join(
                            _json.dumps(k) + "=" + _json.dumps(v, ensure_ascii=False)
                            for k, v in _value.items()) + " }"
                    else:
                        _encoded = _json.dumps(_value, ensure_ascii=False)
                    common += ["-c", f"mcp_servers.meyo_capabilities.{_field}=" + _encoded]
                common += [
                    "-c", 'mcp_servers.meyo_capabilities.tools={'
                    '"list_capabilities"={approval_mode="approve"},'
                    '"call_capability"={approval_mode="approve"}}',
                ]
            if not _read_only and ("MEYO_DEVICE_DOCKER_HOST" in env or "MEYO_AGENTBAY_CONFIG" in env):
                _prepare_stage = "device_command_scope"
                from native_device_permissions import restrict_codex_device_control
                common = restrict_codex_device_control(common, env)
            cmd = [binp, "exec", *common]
            if _resume_id:
                cmd += ["resume", _resume_id]
            # exec/resume 均支持 "-" 从 stdin 读完整 prompt，避免大上下文触发 argv E2BIG。
            cmd += ["-"]
            if _managed_linux:
                cmd = _launch.wrap(cmd)
            previous_runtime = getattr(_INPROC_RUNTIME_ENV, "values", None)
            try:
                if _managed_linux:
                    _INPROC_RUNTIME_ENV.values = {}
                _prepare_stage = "native_execution"
                _native_started = True
                try:
                    import token_telemetry as telemetry
                    _usage_cursor = telemetry.codex_usage_cursor(
                        env.get("CODEX_HOME") or str(_P(env.get("HOME") or _P.home()) / ".codex"),
                        _resume_id)
                except (ImportError, OSError, ValueError, TypeError):
                    _usage_cursor = None
                r = _run_task(cmd, _WALL_S, input=_legion_prompt(codex_question, ctx),
                              env=env, cwd=str(codex_ws), tag="codex",
                              **({"runtime_activity_fd": _transport_activity_fd}
                                 if _transport_activity_fd is not None else {}))
            finally:
                if _managed_linux:
                    try:
                        _captured_final = out.read_text().strip() if out.exists() else ""
                    except (OSError, UnicodeError) as error:
                        _managed_cleanup_errors.append({
                            "scope": "native_final_read", "error_type": type(error).__name__})
                    if previous_runtime is None:
                        del _INPROC_RUNTIME_ENV.values
                    else:
                        _INPROC_RUNTIME_ENV.values = previous_runtime
        final = _redact(_captured_final if _managed_linux else
                        (out.read_text().strip() if out.exists() else ""))
        r.stdout, r.stderr = _redact(r.stdout or ""), _redact(r.stderr or "")
        _thread_id, _codex_usage, _codex_native_events, _native_facts = _native_output(
            r.stdout, r.returncode)
        final = _reconcile_last_message(final, _native_facts)
        _runtime_facts(_native_facts)
        # Native fatal events describe this turn's failure. Tool output may
        # contain permission/credential documentation and is not that failure.
        # The full tool observations remain in native_facts; old CLIs without a
        # usable fatal event retain the existing stderr/stdout fallback below.
        err = ""
        _error_envelope = None
        if r.returncode != 0:
            terminal = _native_facts.get("turn_result") or {}
            try:
                from error_envelope import output_limit_from_native
                _limit = output_limit_from_native(terminal)
                if _limit:
                    _error_envelope = _limit.to_dict()
                    _native_facts["output_limit"] = dict(_limit.details)
            except ImportError:
                pass
            failures = []
            if terminal.get("type") == "turn.failed":
                failures.append(("turn.failed", terminal.get("error")))
            failures.extend(("error", event) for event in
                            reversed(_native_facts.get("errors") or [])
                            if event.get("type") == "error")
            for origin, failure in failures:
                message = failure.get("message") if isinstance(failure, dict) else None
                if not isinstance(message, str) or not message.strip():
                    continue
                err = message.strip()[-_FINAL_CAP:]
                _native_facts["terminal_error_source"] = origin
                # Read only Codex's explicit HTTP-failure formats, never
                # arbitrary numbers or error words from a tool result.
                reported = _re.match(
                    r"(?:unexpected status:?\s+|exceeded retry limit,\s*last status:\s+)"
                    r"([45]\d\d)\b", message.strip(), _re.I)
                if reported and int(reported.group(1)) in (400, 413, 429, 500, 502, 503, 504):
                    _native_facts["reported_http_status"] = int(reported.group(1))
                    try:
                        import error_envelope as _errors
                        _envelope = _errors.normalize("HTTP " + reported.group(1), error=err)
                        _error_envelope = _envelope.to_dict()
                    except ImportError:
                        pass  # Standalone legacy orchestrators may not ship the service module.
                break
            if not err:
                parts = []
                if (r.stderr or "").strip():
                    parts.append("stderr:\n" + r.stderr.strip())
                if (r.stdout or "").strip():
                    parts.append("stdout:\n" + r.stdout.strip())
                err = "\n".join(parts)[-_FINAL_CAP:] or "codex 无输出"
        return {"final": final[:_FINAL_CAP], "status": "ok" if r.returncode == 0 else f"rc{r.returncode}",
                "elapsed": round(_t.time() - t0, 1), "error": err,
                **({"error_envelope": _error_envelope} if _error_envelope else {}),
                "responses_transport": _provider_id,
                "model_used": ctx.model, "usage": _codex_usage,
                "native_events": _codex_native_events,
                "native_facts": _native_facts,
                "native_checkpoint": ({"kind": "codex_thread", "session_id": _thread_id,
                                       "resume_command": f"codex exec resume {_thread_id}",
                                       **({"provider_binding": _provider_binding_id}
                                          if _provider_binding_id else {})}
                                      if _thread_id else _checkpoint or None),
                "resumed_native": bool(_resume_id)}
    except _sp.TimeoutExpired as _te:
        if _managed_linux and not _native_started:
            return _prepare_failure(_te)
        # 超时保留已收到的工具/turn/thread 事实；异常没有进程退出码，保持 None，不猜 rc。
        _tid, _usage, _events, _facts = _native_output(_redact(getattr(_te, "legion_stdout", "") or ""))
        _runtime_facts(_facts)
        _partial = _captured_final if _managed_linux else (out.read_text().strip() if out.exists() else "")
        _partial = _reconcile_last_message(_partial, _facts)
        return {**_runtime_timeout(_te), "final": _partial[:_FINAL_CAP],
                "status": "timeout", "elapsed": round(_t.time() - t0, 1),
                "native_facts": _facts, "native_events": _events, "usage": _usage,
                "native_checkpoint": ({"kind": "codex_thread", "session_id": _tid,
                                       **({"provider_binding": _provider_binding_id}
                                          if _provider_binding_id else {})}
                                      if _tid else _checkpoint or None)}
    except Exception as _error:
        if not _native_started and (_managed_linux or _prepare_stage in (
                "model_catalog", "browser_configuration", "output_budget_transport")):
            return _prepare_failure(_error)
        raise


def _penguin_adapter(qhash, question, gold="", ctx=None):
    # ctx 化(2026-08-28 自查·先改 penguin 一家验证 model 传递通):不再引用模块级
    # _UNIFIED_MODEL 全局,改用 per-call ctx.model。老调用(不传 ctx)走 from_env() 退回
    # 读 env 全局 → 与旧行为完全等价,零回归。penguin 每轮撞同一模型的病根(全局单值)由此拆除。
    ctx = ctx or HarnessCtx.from_env()
    t0 = _t.time()
    binp = _sh.which("penguin") or str(_P.home() / ".penguin/bin/penguin")
    # 记忆/上下文下放：首轮按 ctx 选择 model/provider 并建立原生 session；后续 attempt 优先
    # `chat --resume` 续同一 Trace。Penguin 会沿用原 session 的 model/provider，这是原生恢复的正确
    # 不变量；只有该原生会话本身无法恢复时，父层才换模型/换家，并携带结构化失败现场。
    prompt = _legion_prompt(_prior_attempts_prelude(ctx.prior_attempts) + question, ctx)
    _checkpoint = (ctx.task_memory or {}).get("native_checkpoint") or {}
    _resume_id = str(_checkpoint.get("session_id") or "") if isinstance(_checkpoint, dict) else ""
    _trace_root, _trace_offsets = _penguin_trace_offsets(_resume_id)

    def native_result(returncode=None, stderr=""):
        trace = _penguin_trace_after(
            _trace_root, _trace_offsets, workdir=ctx.workdir, resume_id=_resume_id,
            request_text=prompt)
        sid = str(trace["session_id"] or _resume_id)
        has_text = isinstance(trace["final"], str)
        native_status = trace["request_status"]
        failed = (returncode not in (None, 0) or
                  native_status in {"failed", "auth", "timeout", "malformed", "aborted"})
        status = "ok" if has_text else "incomplete"
        if failed:
            status = f"rc{returncode}" if returncode not in (None, 0) else "error"
        cause, envelope = trace["terminal_error"], trace["error_envelope"]
        if failed and cause is None:
            cause, envelope = _penguin_error_observation(
                stderr, source="penguin_stderr", native_status=native_status)
        facts = {
            "completed": trace["completed"], "failed": failed,
            "turn_status": ("failed" if failed else
                            "completed" if trace["completed"] else "incomplete"),
            "final_source": "penguin_trace_assistant_text" if has_text else "absent",
            "trace_state": trace["trace_state"], "trace_refs": trace["trace_refs"],
            "request_status": native_status, "matched_session": trace["matched_session"],
            "request_end": trace["request_end"], "aborted": trace["aborted"],
            "terminal_error": cause,
        }
        if returncode is not None:
            facts["process_returncode"] = returncode
        return {
            "final": trace["final"] if has_text else "", "status": status,
            # Raw stdout mixes thinking/text and raw failure logs may echo
            # private model context. Keep only machine cause + private trace refs.
            "error": envelope["message"] if envelope else "",
            **({"error_envelope": envelope} if envelope else {}),
            "elapsed": round(_t.time() - t0, 1), "model_used": ctx.model,
            "usage": trace["usage"], "native_events": trace["native_events"],
            "native_facts": facts,
            "native_checkpoint": ({"kind": "penguin_session", "session_id": sid,
                                   "resume_command": f"penguin chat --resume {sid}"} if sid else None),
            "resumed_native": bool(_resume_id and trace["matched_session"]),
        }

    try:
        # ★ 首轮用 `penguin run`(单发 Task 专用),不用 `chat`(交互 REPL)。根因(2026-09-01 真跑
        # 揪出·penguin --help 三证):chat 是 REPL·prompt+/exit 一起灌 stdin 有时序竞态——penguin
        # 打印 "Ready" 待命语后就读到 /exit 退出·prompt 没被 agent 真处理→只返回待命提示空转(17.8s
        # error)。penguin 自带 `run -m <msg>` 命令(Run a single Task)·prompt 走命令行 arg·无 stdin
        # 时序问题·真跑实测 2.9s 直接真交付(快 6 倍)。续跑(resume)才用 chat(原生 session 恢复入口)。
        if _resume_id:
            # 续跑:走 chat --resume(penguin 原生 session 恢复的唯一入口)·灌 prompt+/exit
            cmd = [binp, "chat", "--resume", _resume_id, "--approve", "allow-all"]
            r = _run_task(cmd, _WALL_S, input=prompt + "\n/exit\n", tag="penguin")
        else:
            # 首轮:单发 run·prompt 作 -m arg·彻底避开交互 REPL 时序竞态
            cmd = [binp, "run", "-m", prompt, "--provider", "custom", "--model-id", ctx.model,
                   "--approve", "allow-all"]
            if ctx.workdir:
                cmd += ["--workspace", ctx.workdir]
            r = _run_task(cmd, _WALL_S, tag="penguin")   # 不灌 stdin(run 不需要)
        result = native_result(r.returncode, r.stderr)
        if getattr(r, "cancelled", False):
            result.update(status="cancelled", error="用户停止")
            result["native_facts"]["interrupted"] = True
        return result
    except _sp.TimeoutExpired as _te:
        # Current trace identity/checkpoint can survive an interrupted CLI.
        # Timeout is not evidence of a particular process returncode.
        result = native_result()
        result.update(_runtime_timeout(_te), status="timeout")
        return result
    except FileNotFoundError:
        return {"final": "", "status": "unavailable", "elapsed": 0.0,
                "error": "penguin CLI 未安装", "model_used": ctx.model}


def _jcode_adapter(qhash, question, gold="", ctx=None):
    ctx = ctx or HarnessCtx.from_env()
    t0 = _t.time()
    _checkpoint = (ctx.task_memory or {}).get("native_checkpoint") or {}
    try:
        here = _P(__file__).resolve().parent
        service = next(p for p in (here.parent / "legion-service",
                                    here.parent.parent / "release/legion-service")
                       if (p / "external_adapters/jcode_native.py").is_file())
        if str(service) not in _sys.path:
            _sys.path.insert(0, str(service))
        from external_adapters.jcode_native import executable, prepare, result_from_native
        binp = executable()
        if not binp:
            raise FileNotFoundError("JCode native executable missing")
        import external_agent_registry as _jcode_registry
        _runtime = next((row for row in _jcode_registry.load_catalog()
                         if row.get("key") == "jcode-runtime"), {})
        if (_runtime.get("runtime") or {}).get("native_transport") == "sdk":
            from external_adapters.jcode_sdk import run_harness
            _env = dict(os.environ)
            _env.update(getattr(_INPROC_RUNTIME_ENV, "values", {}) or {})
            return run_harness(ctx, _legion_prompt(
                _prior_attempts_prelude(ctx.prior_attempts) + question, ctx),
                _run_task, environment=_env, timeout=_WALL_S)
        env, home, _resume_id = prepare(ctx, _checkpoint)
        cmd = [binp, "run", _legion_prompt(_prior_attempts_prelude(ctx.prior_attempts) + question, ctx),
               "--ndjson", "--provider", "openai-compatible", "-m", ctx.model, "--no-update"]
        if ctx.workdir:
            cmd += ["-C", ctx.workdir]
        if _resume_id:
            cmd += ["--resume", _resume_id]
        r = _run_task(cmd, _WALL_S, env=env, cwd=ctx.workdir or None, tag="jcode")
        result = result_from_native(r, _checkpoint, home, ctx.model)
        if ctx.appid and result.get("error"):
            result["error"] = result["error"].replace(ctx.appid, "[redacted]")
        return {**result, "usage": result.get("usage"), "elapsed": round(_t.time() - t0, 1)}
    except _sp.TimeoutExpired as _te:
        captured = result_from_native(
            _sp.CompletedProcess(
                cmd, -1, getattr(_te, "legion_stdout", None) or _te.output or "",
                getattr(_te, "legion_stderr", None) or _te.stderr or ""),
            _checkpoint, home, ctx.model)
        return {**captured, **_runtime_timeout(_te), "final": "", "status": "timeout",
                "elapsed": round(_t.time() - t0, 1)}
    except FileNotFoundError:
        return {"final": "", "status": "unavailable", "elapsed": 0.0,
                "error": "jcode CLI 未安装", "native_checkpoint": _checkpoint or None,
                "requested_model": ctx.model, "model_used": None, "resumed_native": False}
    except (ValueError, OSError, StopIteration) as exc:
        return {"final": "", "status": "harness_runtime", "elapsed": round(_t.time() - t0, 1),
                "error": str(exc), "native_checkpoint": _checkpoint or None,
                "requested_model": ctx.model, "model_used": None, "resumed_native": False}


def _prime_adapter(qhash, question, gold="", ctx=None):
    """旧 key 薄接官方 Prime Intellect；prime_message 费用由专属事件读取器提供。"""
    ctx = ctx or HarnessCtx.from_env()
    checkpoint = (ctx.task_memory or {}).get("native_checkpoint") or {}
    t0 = _t.time()
    try:
        here = _P(__file__).resolve().parent
        service = next(p for p in (here.parent / "legion-service",
                                    here.parent.parent / "release/legion-service")
                       if (p / "external_agent_registry.py").is_file())
        if str(service) not in _sys.path:
            _sys.path.insert(0, str(service))
        import external_agent_registry as registry
        from external_adapters.prime_native import SESSION_FORMAT, prepare_resume, unwrap_result
        prepare_resume(checkpoint)
        item = next((x for x in registry.load_catalog() if x["key"] == "prime-intellect"), {})
        transports = item.get("transports") or []
        if len(transports) != 1 or transports[0].get("native_session_format") != SESSION_FORMAT:
            raise ValueError("prime_native_spec_not_registered: 请经 harness_onboard 持锁 upsert Prime spec")
        result = unwrap_result(_external_adapter("prime-intellect")(qhash, question, gold, ctx))
    except Exception as exc:
        result = {"status": "harness_runtime", "final": "", "error": str(exc)}
    if result.get("status") != "ok":
        result["resumed_native"] = False
        if checkpoint:
            result["native_checkpoint"] = checkpoint
    result.setdefault("usage_records", [])
    result.setdefault("elapsed", round(_t.time() - t0, 2))
    result.setdefault("model_used", ctx.model)
    return result


def _hermes_adapter(qhash, question, gold="", ctx=None):
    """Hermes adapter。开发/包内源码用 AIAgent；无源码的老包才退 CLI。"""
    # ctx 化(权限下放·同 penguin 模式):per-call ctx.model 替全局 + 失败回流拼进 prompt。
    # 依据 hermes bedrock.py:108-116 content/reasoning 分字段·失败分类回流让它自判换招。
    ctx = ctx or HarnessCtx.from_env()
    _managed_browser = None
    _managed_browser_cleanup_errors = []
    def _preserve_failed_native_input(result):
        if _managed_browser is not None:
            facts = result.get("native_facts")
            if not isinstance(facts, dict):
                result["native_facts"] = facts = {}
            facts["managed_browser"] = {
                "closed": _managed_browser.closed,
                "observations": list(_managed_browser.observations),
                "cleanup": dict(_managed_browser.cleanup),
            }
            if _managed_browser_cleanup_errors:
                facts["managed_browser"].update(
                    cleanup_state="failed_or_unconfirmed",
                    cleanup_errors=list(_managed_browser_cleanup_errors))
        # Keep the caller's recovery reference, not a claim that this attempt
        # loaded it. Observed native checkpoints always take precedence.
        if result.get("status") in ("ok", "done"):
            return result
        memory = ctx.task_memory if isinstance(ctx.task_memory, dict) else {}
        checkpoint = memory.get("native_checkpoint")
        if (not isinstance(checkpoint, dict) or checkpoint.get("kind") != "hermes_session"
                or not checkpoint.get("session_id")):
            return result
        if not result.get("native_checkpoint"):
            result["native_checkpoint"] = dict(checkpoint)
            result["resumed_native"] = False
        else:
            result.setdefault("resumed_native", False)
        slot = memory.get("native_conversation_id")
        if not isinstance(slot, str) or not slot:
            # core supplies ctx.session_id as "hermes:<logical native slot>".
            # Do not derive the slot from a possibly compacted native SID.
            session = str(ctx.session_id or "")
            slot = session[len("hermes:"):] if session.startswith("hermes:") else ""
        if slot and not result.get("native_conversation_id"):
            result["native_conversation_id"] = slot
        return result

    _model = ctx.model
    _gateway = getattr(ctx, "gateway", "") or _UNIFIED_GATEWAY
    _q = _prior_attempts_prelude(ctx.prior_attempts) + question
    # 记忆/会话下放:有 memory_dir(user 单元)就开长期记忆并把 session_id/user_id 交给 AIAgent
    # 自管(hermes run_agent.py:464,492 真接这些参数);没有则退回 batch 模式关记忆(零回归)。
    _use_mem = bool(ctx.memory_dir)
    t0 = _t.time()
    here = _P(os.path.dirname(os.path.abspath(__file__)))
    source = here.parent / "hermes-agent"
    _app_root = here.parent.parent
    # Installed layout, not a task-controlled backend toggle. Missing runtime
    # inputs in a published Linux app must not silently select bare execution.
    _managed_linux = _sys.platform.startswith("linux") and (
        (here.parent.name == "legion" and (here.parent / "legion-service").is_dir())
        or any((_app_root / name).exists()
               for name in ("image-payload.json", "runtime-ref.json")))
    # Both source and portable layouts; never infer another user's HOME.
    _mailbox_service = next((p for p in (
        here.parent / "legion-service", here.parent.parent / "release/legion-service")
        if (p / "team_state.py").is_file()), None)
    _mailbox_facts = getattr(ctx, "task_facts", None) or {}
    _mailbox_team = str(_mailbox_facts.get("team_id") or "")
    _mailbox_task = str(_mailbox_facts.get("task_id") or "")
    _python = "python3.12"
    # 两种布局的 repo 均在 orchestrator 的上两层；不从个人 HOME 猜安装位置。
    _packaged_source = here.parent.parent / "runtime/core-sources/hermes-agent"
    if not (source / "run_agent.py").is_file() and _packaged_source.exists():
        source = _packaged_source
        _python = str(here.parent.parent / "runtime/core-runtimes/hermes-agent/venv/bin/python")
        if not (source / "run_agent.py").is_file():
            return _preserve_failed_native_input({
                "final": "", "status": "unavailable", "elapsed": round(_t.time()-t0, 1),
                "error": f"Hermes 包内源码不完整：缺少 {source / 'run_agent.py'}"})
        if not _P(_python).is_file() or not os.access(_python, os.X_OK):
            return _preserve_failed_native_input({
                "final": "", "status": "unavailable", "elapsed": round(_t.time()-t0, 1),
                "error": f"Hermes 缺少独立运行依赖：解释器不存在或不可执行：{_python}"})
        # 0.20.5 pyproject.toml: requires-python >=3.11,<3.14。
        # 用目标解释器自检，不借主 vendor 导入 Hermes 或碰运气运行。
        try:
            _probe = _run_live(
                [_python, "-I", "-c", "import sys; print(sys.version); "
                 "sys.exit(0 if (3,11) <= sys.version_info[:2] < (3,14) else 1)"],
                timeout=10, tag="hermes-python-check")
            if _probe.returncode != 0:
                return _preserve_failed_native_input({
                    "final": "", "status": "unavailable", "elapsed": round(_t.time()-t0, 1),
                    "error": f"Hermes 独立解释器需要 Python >=3.11,<3.14：{_python}；"
                             f"{(_probe.stderr or _probe.stdout)[-300:]}"})
        except (OSError, _sp.TimeoutExpired) as exc:
            return _preserve_failed_native_input({
                "final": "", "status": "unavailable", "elapsed": round(_t.time()-t0, 1),
                "error": f"Hermes 独立运行依赖不可用：{_python}；{exc}"})
    # appid 优先 ctx.appid(单一事实源·2026-09-01 全家体检揪出:同 codex 旧病·hermes 用全局
    # _UNIFIED_APPID 忽略 ctx.appid→场景选模型分配的独立 appid 传不进→并发全挤一个 appid 撞限流)。
    env = dict(os.environ, AIGC_APPID=(getattr(ctx, "appid", "") or _UNIFIED_APPID))
    # A launch-scoped capability must never be inherited from another task.
    env.pop("MEYO_HERMES_MANAGED_MCP_CONFIG", None)
    # Reuse Hermes' sealed-venv mode for its implicit dependency setup.
    # An explicitly configured durable target keeps its native semantics;
    # this does not replace OS isolation or ban all tool installation.
    env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    if source == _packaged_source:
        # 独立 venv 不继承主 vendor 的搜索路径或 Python 根目录；开发与老包 CLI 不变。
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
    # 两种入口与父层产物收集使用同一现场；终端配置不能继承别单的目录。
    _hermes_ws = _P(ctx.workdir or _P.home() / ".meyo" / "legion" / "outputs" / f"hermes_{qhash}").resolve()
    _hermes_ws.mkdir(parents=True, exist_ok=True)
    env["TERMINAL_CWD"] = str(_hermes_ws)
    if _use_mem:
        _P(ctx.memory_dir).mkdir(parents=True, exist_ok=True)
        env["HERMES_HOME"] = ctx.memory_dir   # 记忆/会话落用户单元目录·harness 自管

    def _hermes_diagnostic(value):
        text = str(value or "")
        secrets = [getattr(ctx, "appid", "")]
        if _managed_browser is not None:
            secrets += [str(spec.get("headers", {}).get("Authorization", "")).removeprefix("Bearer ")
                        for spec in _managed_browser.mcp_config.get("mcpServers", {}).values()]
        secrets += [value for name, value in env.items()
                    if any(part in name.upper() for part in
                           ("APPID", "API_KEY", "AUTH_TOKEN", "PASSWORD", "SECRET"))]
        alias_values = {pair.split("=", 1)[1].strip()
                        for pair in env.get("MEYO_APPID_ALIASES", "").split(",") if "=" in pair}
        secrets += list(alias_values)
        for secret in sorted({v for v in secrets if isinstance(v, str)}, key=len, reverse=True):
            if secret and (len(secret) >= 4 or secret in alias_values):
                text = text.replace(secret, "[redacted]")
        return text[-4000:]

    def _hermes_boundary_failure(error, stderr=""):
        message = _hermes_diagnostic(error)
        checkpoint = (ctx.task_memory or {}).get("native_checkpoint")
        return _preserve_failed_native_input({
                "status": "harness_runtime", "fail_kind": "harness_runtime",
                "final": "", "error": message, "stderr": _hermes_diagnostic(stderr),
                "model_used": None, "requested_model": _model, "resumed_native": False,
                "native_checkpoint": checkpoint if isinstance(checkpoint, dict) else None,
                "elapsed": round(_t.time() - t0, 1),
                "error_envelope": {"category": "harness_runtime", "scope": "harness",
                                   "retryable": False, "message": message}})

    _mailbox_parent = None
    _mailbox_receipts_seen = set()
    _mailbox_receipt_errors = []

    def _run_hermes_process(command, tag, *, native_mailbox=False, native_tools=False):
        nonlocal _mailbox_parent, _managed_browser
        if not _managed_linux:
            return _run_task(command, _WALL_S, cwd=str(_hermes_ws), env=env, tag=tag)
        from native_process_sandbox import prepare_native_process, NativeProcessSandboxError
        effective = dict(env)
        previous_runtime = getattr(_INPROC_RUNTIME_ENV, "values", None)
        effective.update(previous_runtime or {})
        effective.pop("MEYO_HERMES_MANAGED_MCP_CONFIG", None)
        # Keep the bound model/home/import policy, while retaining other
        # per-call runtime facts needed by existing shared native tools.
        for name in ("AIGC_APPID", "HERMES_HOME", "TERMINAL_CWD", "PYTHONPATH",
                     "PYTHONHOME", "AI_GATEWAY_BASE_URL", "AI_GATEWAY_API_KEY",
                     "OPENAI_BASE_URL", "OPENAI_API_KEY"):
            if name in env:
                effective[name] = env[name]
            else:
                effective.pop(name, None)
        runtime = (_app_root / "runtime").resolve(strict=True)
        declared_runtime = effective.get("MEYO_RUNTIME_ROOT")
        if declared_runtime and (
                not _P(declared_runtime).is_absolute()
                or _P(declared_runtime).resolve() != runtime):
            raise NativeProcessSandboxError("Hermes runtime differs from the installed program")
        effective["MEYO_RUNTIME_ROOT"] = str(runtime)
        home_value = effective.get("HERMES_HOME")
        if not home_value or not _P(home_value).is_absolute():
            raise NativeProcessSandboxError("managed Hermes requires its original absolute HERMES_HOME")
        native_home = _P(home_value).resolve(strict=True)
        data = _P(effective.get("MEYO_LEGION_DIR") or
                  _P(effective["HOME"]) / ".meyo/legion").resolve()
        if data.is_relative_to(native_home) or data.is_relative_to(_hermes_ws):
            raise NativeProcessSandboxError(
                "Hermes native home/workspace must not open the shared data directory")
        if native_mailbox and _mailbox_team and _mailbox_task:
            import team_state
            from types import SimpleNamespace
            team_root = _P(effective.get("LEGION_TEAM_DIR") or data / "teams").resolve()
            if team_root != team_state._team_dir().resolve():
                raise NativeProcessSandboxError(
                    "NativeMailbox parent and native state roots differ")
            checkpoint = (ctx.task_memory or {}).get("native_checkpoint") or {}
            bound_sid = str(checkpoint.get("session_id") or ctx.session_id or "")
            _mailbox_parent = team_state.NativeMailbox(
                _mailbox_team, _mailbox_task, str(ctx.user_id or ""),
                SimpleNamespace(session_id=bound_sid), native_home=native_home, workdir=_hermes_ws)
            if (not callable(getattr(_mailbox_parent, "apply_receipt", None))
                    or _mailbox_parent._state() is None):
                raise NativeProcessSandboxError(
                    "NativeMailbox parent team/user/task/native session binding is unavailable")
        protected = [str(_app_root.resolve()), str(runtime)]
        if source.is_dir():
            protected.append(str(source.resolve()))
        with prepare_native_process(
                effective, str(_hermes_ws), backend="codex",
                protected_roots=list(dict.fromkeys(protected)),
                writable_state_roots=[str(native_home)]) as launch:
            from contextlib import ExitStack
            with ExitStack() as owned_tools:
                launch.env.pop("MEYO_HERMES_MANAGED_MCP_CONFIG", None)
                if native_tools and ctx.execution_depth != "quick":
                    mode = str(effective.get("LEGION_MANAGED_BROWSER") or "").lower()
                    if mode not in ("", "0", "false", "no", "1", "true", "yes"):
                        raise ValueError("unknown managed browser mode")
                    if mode in ("1", "true", "yes"):
                        if str(effective.get("HERMES_SAFE_MODE") or "").strip().lower() in (
                                "1", "true", "yes", "on"):
                            raise NativeProcessSandboxError(
                                "Hermes safe mode does not permit managed MCP")
                        import secrets
                        from managed_browser import open_managed_browser
                        browser_scope = open_managed_browser(
                            ctx, env=effective, config_dir=launch.config_dir)
                        _managed_browser = browser_scope.__enter__()

                        def close_browser(exc_type, exc, tb):
                            try:
                                browser_scope.__exit__(exc_type, exc, tb)
                            except Exception as cleanup_error:
                                # Cleanup is a separate fact. It must not replace
                                # an obtained native result, timeout or cancellation.
                                _managed_browser_cleanup_errors.append({
                                    "error_type": type(cleanup_error).__name__,
                                    "error": _hermes_diagnostic(cleanup_error),
                                })
                            return False

                        owned_tools.push(close_browser)
                        # Hermes treats server names as connection identities. Do
                        # not reuse a durable config/cache entry with another URL.
                        servers = {
                            name + "-" + secrets.token_hex(6): spec
                            for name, spec in _managed_browser.mcp_config["mcpServers"].items()
                        }
                        config_path = _P(launch.config_dir) / "hermes-mcp.json"
                        fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(fd, "w", encoding="utf-8") as stream:
                            _json.dump({"mcpServers": servers}, stream, ensure_ascii=False)
                        launch.env["MEYO_HERMES_MANAGED_MCP_CONFIG"] = str(config_path)
                # _run_live must not replace this launch's HOME/TMPDIR/registry.
                _INPROC_RUNTIME_ENV.values = {}
                try:
                    return _run_task(launch.wrap(command), _WALL_S, cwd=str(_hermes_ws),
                                     env=launch.env, tag=tag)
                finally:
                    if previous_runtime is None:
                        del _INPROC_RUNTIME_ENV.values
                    else:
                        _INPROC_RUNTIME_ENV.values = previous_runtime

    if (source / "run_agent.py").is_file() and (getattr(ctx, "appid", "") or _UNIFIED_APPID):
        # 自主权还给 hermes(用户 2026-08-28:harness 自己就支持 fallback/不限迭代/自控token,
        # 别在 adapter 写死阉割它)。① max_tokens 不再写死 8192(reasoning 模型思考链会吃光·正文
        # 被截成残渣"。"):ctx.max_tokens>0 用它,否则给 hermes 默认(None=模型自己用足)。
        # ② max_iterations 同理不砍成 20,ctx 有就用、没有让 hermes 走它自己的默认(unlimited)。
        # ③ 接通 hermes 自带的 fallback_model:把军团候选模型交给它,主模型退化时它自己换(自愈)。
        # 这些都是"放开它已有的能力",不是父层加规则。
        _mx_tok = ctx.max_tokens if getattr(ctx, "max_tokens", 0) else "None"
        _mx_iter = ctx.max_iterations if getattr(ctx, "max_iterations", 0) else "sys.maxsize"
        _checkpoint = (ctx.task_memory or {}).get("native_checkpoint") or {}
        _resume_id = str(_checkpoint.get("session_id") or "") if isinstance(_checkpoint, dict) else ""
        if _checkpoint and (not isinstance(_checkpoint, dict)
                            or _checkpoint.get("kind") != "hermes_session" or not _resume_id):
            return {"status": "error", "final": "", "error": "invalid Hermes native checkpoint",
                    "native_resume_error": {"stage": "checkpoint_validation"},
                    "error_envelope": {"category": "native_session_unavailable", "retryable": False,
                                       "scope": "harness", "message": "invalid Hermes native checkpoint"}}
        _fallback = ""
        _fb_list = getattr(ctx, "fallback_models", None) or []
        if _fb_list:
            # hermes fallback_model 是 Dict[str,Any];给第一个候选(它内部有 fallback chain)
            _fallback = f',fallback_model={{"model":{_fb_list[0]!r}}}'
        # -P (Python >=3.11, matching Hermes' supported range) removes the
        # automatic -c cwd entry, but PYTHONPATH can explicitly add it back.
        # Filter only the worker's startup path, then restore the original env
        # for terminal child Pythons. Never export PYTHONSAFEPATH globally.
        _terminal_pythonpath = env.get("PYTHONPATH")
        if _terminal_pythonpath is not None:
            _worker_paths = []
            for _entry in _terminal_pythonpath.split(os.pathsep):
                _entry_path = (_hermes_ws / _entry).resolve()
                if not _entry_path.is_relative_to(_hermes_ws):
                    _worker_paths.append(str(_entry_path))
            if _worker_paths:
                env["PYTHONPATH"] = os.pathsep.join(_worker_paths)
            else:
                env.pop("PYTHONPATH", None)
        code = f'''import os,sys
if {_terminal_pythonpath is not None!r}:
    os.environ["PYTHONPATH"]={_terminal_pythonpath!r}
import json,time,re,threading,math,hashlib
sys.path.insert(0,{str(source)!r})
from run_agent import AIAgent
t=time.time(); sid={(_resume_id or ctx.session_id)!r} or None
history=[]
try:
    from hermes_state import SessionDB
    db=SessionDB()
    if {bool(_resume_id)!r}:
        history=db.get_messages_as_conversation(sid,repair_alternation=True)
        if not isinstance(history,list) or not history:
            raise ValueError("native session history empty or invalid")
except Exception as exc:
    message="native session load failed: " + type(exc).__name__ + ": " + str(exc)
    print(json.dumps({{"type":"result","hermes_adapter_result":True,"status":"error","final":"","error":message,
        "resumed_native":False,"native_facts":{{}},
        "native_checkpoint":{{"kind":"hermes_session","session_id":sid}},
        "native_resume_error":{{"stage":"session_load","session_id":sid,"message":message}},
        "error_envelope":{{"category":"native_session_unavailable","retryable":False,
            "scope":"harness","message":message}}}},ensure_ascii=False),flush=True)
    raise SystemExit(0)
requested_model={_model!r}

a=None
mailbox=None
before={{}}
started={{}}
flags={{}}
lock=threading.RLock()
fields={{"input_tokens":"session_input_tokens","output_tokens":"session_output_tokens","cache_read_tokens":"session_cache_read_tokens","cache_write_tokens":"session_cache_write_tokens","reasoning_output_tokens":"session_reasoning_tokens","total_tokens":"session_total_tokens","num_turns":"session_api_calls"}}
def numeric(v):return type(v) in (int,float) and math.isfinite(v)
def usage(native=None):
    native=native or {{}}
    u={{"source":"hermes_session_delta","granularity":"task","model":native.get("model") or getattr(a,"model",None) or requested_model}}
    for key,attr in fields.items():
        value=getattr(a,attr,None);base=before.get(attr)
        if numeric(value) and numeric(base) and value>=base:u[key]=value-base
    cs=native.get("cost_status",getattr(a,"session_cost_status",None)) or "unknown"
    src=native.get("cost_source",getattr(a,"session_cost_source",None)) or "none"
    u.update(cost_status=cs,cost_source=src)
    cost=getattr(a,"session_estimated_cost_usd",None);base=before.get("session_estimated_cost_usd")
    if cs in ("known","estimated") and src not in ("none","",None) and numeric(cost) and numeric(base) and cost>=base:u["cost_usd"]=cost-base
    return u
def emit(value):
    with lock:print(json.dumps(value,ensure_ascii=False),flush=True)
def snapshot(phase,event=None,**facts):
    session=getattr(a,"session_id",None) or sid
    emit({{"type":"result","hermes_adapter_snapshot":True,"native_checkpoint":{{"kind":"hermes_session","session_id":session}},"native_facts":{{"phase":phase,"session_id":session,**facts}},"model_used":getattr(a,"model",None) or requested_model,"usage":usage(),"tool_event":event}})
def key(name,result):return (name,hashlib.sha256(json.dumps(result,sort_keys=True,default=str).encode()).hexdigest())
def progress(event,name,preview,args,**details):
    if event=="tool.completed" and type(details.get("is_error")) is bool:
        with lock:flags.setdefault(key(name,details.get("result")),[]).append(details["is_error"])
def runtime_activity(call_id,phase,budget=None):
    emit({{"type":"runtime_activity","call_id":call_id,"phase":phase,"budget_s":budget}})
def tool_start(call_id,name,args):
    runtime_activity("provider_request","end")
    budget=args.get("timeout") if isinstance(args,dict) else None
    if budget is None and name=="terminal":
        try:
            from tools.terminal_tool import _get_env_config
            budget=_get_env_config()["timeout"]
        except (ImportError,KeyError,TypeError):pass
    runtime_activity(call_id,"start",budget)
    with lock:
        started[call_id]=time.monotonic()
        meta={{"_hermes_callback":True,"sid":getattr(a,"session_id",None) or sid,"ok":None,"ms":None}}
        emit({{"type":"assistant","message":{{"content":[{{"type":"tool_use","id":call_id,"name":name,"input":meta}}]}}}})
        snapshot("tool_started",{{"call_id":call_id,"name":name,"ok":None,"ms":None,"source_ref":{{"kind":"hermes_session_tool","session_id":meta["sid"],"tool_call_id":call_id}}}})
def tool_complete(call_id,name,args,result):
    runtime_activity(call_id,"end")
    with lock:
        began=started.pop(call_id,None);ms=round((time.monotonic()-began)*1000,3) if began is not None else None
        pending=flags.get(key(name,result)) or [];native_error=pending.pop(0) if pending else None
        ok=not native_error if type(native_error) is bool else None
        try:
            parsed=json.loads(result) if isinstance(result,str) else result
            if isinstance(parsed,dict):
                ec=parsed.get("exit_code")
                if parsed.get("error") or type(ec) is int and ec!=0:ok=False
                elif type(ec) is int and native_error is not True:ok=True
        except (ValueError,TypeError):pass
        meta={{"_hermes_callback":True,"sid":getattr(a,"session_id",None) or sid,"ok":ok,"ms":ms}}
        emit({{"type":"user","message":{{"content":[{{"type":"tool_result","tool_use_id":call_id,"name":name,"is_error":ok is False,"content":json.dumps(meta,separators=(",",":"))}}]}}}})
        snapshot("tool_completed",{{"call_id":call_id,"name":name,"ok":ok,"ms":ms,"source_ref":{{"kind":"hermes_session_tool","session_id":meta["sid"],"tool_call_id":call_id}}}})
def step(api_call_count,prev_tools):
    if mailbox is not None:
        try:mailbox.enqueue()
        except Exception as exc:
            snapshot("mailbox_unavailable",exception_type=type(exc).__name__)
            if {_managed_linux!r}:raise
    timeout=getattr(a,"_resolved_api_call_timeout",lambda:None)()
    runtime_activity("provider_request","start",timeout)
    snapshot("api_call_started",api_call_index=api_call_count)
def status_event(kind,message):snapshot("native_status",status_kind=kind)
managed_mcp=None
managed_mcp_path=os.environ.pop("MEYO_HERMES_MANAGED_MCP_CONFIG",None)
if managed_mcp_path:
    try:
        import stat
        from utils import env_var_enabled
        if env_var_enabled("HERMES_SAFE_MODE"):
            raise PermissionError("Hermes safe mode does not permit managed MCP")
        fd=os.open(managed_mcp_path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,encoding="utf-8") as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
                raise PermissionError("invalid private managed MCP configuration")
            servers=json.load(stream)["mcpServers"]
        from tools import mcp_tool as managed_mcp
        expected={{managed_mcp.mcp_prefixed_tool_name(name,tool)
                  for name in servers for tool in ("browser_navigate","browser_take_screenshot")}}
        registered=set(managed_mcp.register_mcp_servers(servers))
        if not expected or not expected.issubset(registered):
            raise RuntimeError("this managed browser did not register its native tools")
    except Exception as exc:
        if managed_mcp is not None:
            try:managed_mcp.shutdown_mcp_servers()
            except Exception:pass
        message="Hermes managed browser MCP setup failed: "+type(exc).__name__
        emit({{"type":"result","hermes_adapter_result":True,"final":"","status":"harness_runtime",
              "fail_kind":"harness_runtime","error":message,"model_used":None,"resumed_native":False,
              "native_facts":{{"phase":"managed_mcp_unavailable"}},
              "error_envelope":{{"category":"harness_runtime","scope":"harness",
                                "retryable":False,"message":message}}}})
        raise SystemExit(1)
try:
    a=AIAgent(base_url={_gateway!r},api_key=os.environ["AIGC_APPID"],
    provider="custom",api_mode="chat_completions",model={_model!r},
     max_iterations={_mx_iter},max_tokens={_mx_tok},quiet_mode=True,
     session_id=sid,user_id={ctx.user_id!r} or None,session_db=db,
     skip_context_files=True,skip_memory={not _use_mem},skip_background_review=True,run_budget_seconds={_WALL_S or None}{_fallback},tool_start_callback=tool_start,tool_complete_callback=tool_complete,tool_progress_callback=progress,step_callback=step,status_callback=status_event)
    if history:
        a._session_db_created=True
    if {_mailbox_team!r} and {_mailbox_task!r}:
        try:
            if {str(_mailbox_service or "")!r}:
                sys.path.append({str(_mailbox_service or "")!r})
            from team_state import NativeMailbox
            mailbox_options={{}}
            if {_managed_linux!r}:
                mailbox_options["receipt_sink"]=lambda receipt:emit({{"type":"hermes_mailbox_receipt","receipt":receipt}})
            mailbox=NativeMailbox({_mailbox_team!r},{_mailbox_task!r},{ctx.user_id!r},a,
                lambda event:snapshot("mailbox",mailbox_event=event),**mailbox_options)
            mailbox.attach()
        except Exception as exc:
            mailbox=None
            snapshot("mailbox_unavailable",exception_type=type(exc).__name__)
            if {_managed_linux!r}:raise
    before={{attr:getattr(a,attr,None) for attr in (*fields.values(),"session_estimated_cost_usd")}}
    snapshot("started")
    r=a.run_conversation({_legion_prompt(_q, ctx)!r},conversation_history=history)
except Exception as exc:
    snapshot("native_exception",exception_type=type(exc).__name__)
    r={{"failed":True,"completed":False,"error":"Hermes exception: "+type(exc).__name__}}
finally:
    try:
        if mailbox is not None:mailbox.close()
    finally:
        if managed_mcp is not None:
            try:managed_mcp.shutdown_mcp_servers()
            except Exception as exc:snapshot("managed_mcp_cleanup_error",exception_type=type(exc).__name__)
native_error=r.get("error") or r.get("failure_reason") or ""
budget_exit=re.fullmatch(r"max_iterations_reached(?:\\(\\d+/\\d+\\))?",str(r.get("turn_exit_reason") or "")) is not None
status=("cancelled" if r.get("interrupted") else "error" if r.get("failed") or native_error else
        "incomplete" if budget_exit else "ok" if r.get("completed") else "incomplete")
usage_data=usage(r)
print(json.dumps({{"type":"result","hermes_adapter_result":True,"final":r.get("final_response") or "","status":status,"error":native_error,"elapsed":round(time.time()-t,1),"model_used":r.get("model") or getattr(a,"model",None) or {_model!r},"usage":usage_data,"native_checkpoint":{{"kind":"hermes_session","session_id":getattr(a,"session_id",None) or sid}},"resumed_native":bool(history),"native_facts":{{k:r[k] for k in ('turn_exit_reason', 'completed', 'failed', 'interrupted', 'error', 'failure_reason', 'cleanup_errors', 'model', 'provider', 'session_id', 'base_url', 'cost_status', 'cost_source') if k in r}},**{{k:r[k] for k in ("error_envelope",) if k in r}}}},ensure_ascii=False),flush=True)'''

        def parse_hermes(stdout):
            final, snapshot, tools = None, {}, []
            for line in (stdout or "").splitlines():
                try:
                    value = _json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(value, dict):
                    continue
                if value.get("type") == "hermes_mailbox_receipt":
                    receipt_key = _json.dumps(value.get("receipt"), sort_keys=True)
                    if receipt_key not in _mailbox_receipts_seen and len(_mailbox_receipt_errors) < 8:
                        _mailbox_receipt_errors.append("native mailbox receipt was not handled")
                    continue
                if value.get("hermes_adapter_snapshot") is True:
                    snapshot = value
                    if (_managed_linux
                            and (value.get("native_facts") or {}).get("phase") == "mailbox_unavailable"
                            and len(_mailbox_receipt_errors) < 8):
                        _mailbox_receipt_errors.append(
                            "native mailbox unavailable: "
                            + str(value["native_facts"].get("exception_type") or "unknown")[:80])
                    if isinstance(value.get("tool_event"), dict):
                        tools.append(value["tool_event"])
                elif value.get("hermes_adapter_result") is True:
                    final = value
            facts = dict((final or snapshot).get("native_facts") or {})
            facts.update(tool_events=tools, partial_observation=final is None)
            if final is None:
                final = {"final": "", "native_checkpoint": snapshot.get("native_checkpoint"),
                         "resumed_native": bool(_resume_id)}
                if isinstance(snapshot.get("usage"), dict):
                    final["usage"] = {**snapshot["usage"], "complete": False}
                if snapshot.get("model_used"):
                    final["model_used"] = snapshot["model_used"]
            final["native_facts"] = facts
            if _mailbox_receipt_errors:
                errors = list(dict.fromkeys(_mailbox_receipt_errors))[:8]
                facts["mailbox_receipt_errors"] = errors
                failure = _hermes_boundary_failure("NativeMailbox receipt failed: " + "; ".join(errors))
                # Keep the real native final/checkpoint/usage. Only the runtime
                # delivery claim changes; no missing receipt becomes delivered.
                for name in ("status", "fail_kind", "error", "error_envelope"):
                    final[name] = failure[name]
            return final

        previous_sink = getattr(_INPROC_EVENT_SINK, "cb", None)
        def hermes_sink(event):
            # The shared wire projects tool_use/tool_result, not Hermes prose.
            event = dict(event)
            if event.get("t") == "hermes_mailbox_receipt":
                receipt = event.get("receipt")
                try:
                    if _mailbox_parent is None:
                        raise ValueError("mailbox receipt has no bound parent")
                    _mailbox_parent.apply_receipt(receipt)
                except Exception as exc:
                    # The generic event sink suppresses callback exceptions.
                    # Retain an explicit runtime error instead of losing it.
                    first_error = not _mailbox_receipt_errors
                    if len(_mailbox_receipt_errors) < 8:
                        _mailbox_receipt_errors.append(_hermes_diagnostic(exc)[:300])
                    if first_error:
                        warning = {"t": "harness_step", "agent": "hermes",
                                   "x": "原生反馈回执未落账，尚未确认送达"}
                        if previous_sink is not None:
                            previous_sink(warning)
                        else:
                            _sys.stderr.write(_LEGION_EV_PREFIX + _json.dumps(warning, ensure_ascii=False) + "\n")
                            _sys.stderr.flush()
                finally:
                    _mailbox_receipts_seen.add(_json.dumps(receipt, sort_keys=True))
                return  # Private receipt: never forward body/digest/SID to UI.
            if event.get("t") in ("tool_started", "tool_end"):
                began = event["t"] == "tool_started"
                try:
                    meta = _json.loads(event.get("args_brief" if began else "result_brief") or "")
                except (ValueError, TypeError):
                    return
                if not isinstance(meta, dict) or meta.get("_hermes_callback") is not True:
                    return
                ok = meta.get("ok") if type(meta.get("ok")) is bool else None
                event.update(name=event.get("tool"), ok=ok, ms=meta.get("ms"),
                             status="started" if began else "succeeded" if ok is True else
                             "failed" if ok is False else "completed", args_brief="", result_brief="",
                             source_ref={"kind": "hermes_session_tool", "source_harness": "hermes",
                                         "session_id": meta.get("sid"), "tool_call_id": event.get("call_id")})
                event["x"] = ("调用工具 " if began else "工具结束 ") + str(event.get("tool") or "")
            elif event.get("t") != "weapon_candidates":
                return  # Raw console text, arguments and results stay private.
            if previous_sink is not None:
                previous_sink(event)
            else:
                _sys.stderr.write(_LEGION_EV_PREFIX + _json.dumps(event, ensure_ascii=False) + "\n")
                _sys.stderr.flush()

        env["PYTHONUNBUFFERED"] = "1"
        _INPROC_EVENT_SINK.cb = hermes_sink
        try:
            r = _run_hermes_process([_python, "-P", "-c", code], "hermes",
                                    native_mailbox=True, native_tools=True)
            data = parse_hermes(r.stdout)
            observed_native = (data.get("hermes_adapter_result") is True
                               or data["native_facts"].get("phase")
                               or data["native_facts"].get("tool_events"))
            if _managed_linux and r.returncode and not observed_native:
                return _hermes_boundary_failure(
                    r.stderr or "Hermes native process exited before its first record", r.stderr)
            data["elapsed"] = round(_t.time() - t0, 1)
            data["native_facts"]["process_returncode"] = r.returncode
            if getattr(r, "cancelled", False):
                data.update(status="cancelled", cancelled=True)
            elif data.get("hermes_adapter_result") is not True:
                data.update(status="error", error="Hermes final result marker missing")
            if r.returncode and r.stderr:
                diagnostic = _hermes_diagnostic(r.stderr)
                data["stderr"] = diagnostic
                data["error"] = _hermes_diagnostic(
                    str(data.get("error") or "") + "\n" + diagnostic)
            data.pop("type", None)
            data.pop("hermes_adapter_result", None)
            return _preserve_failed_native_input(data)
        except _sp.TimeoutExpired as exc:
            data = parse_hermes(getattr(exc, "legion_stdout", "") or "")
            data.pop("type", None)
            data.pop("hermes_adapter_result", None)
            data.update(final="", status="timeout", elapsed=round(_t.time()-t0,1))
            data.setdefault("model_used", _model)
            if not data.get("native_checkpoint") and (_resume_id or ctx.session_id):
                data["native_checkpoint"] = {"kind": "hermes_session", "session_id": _resume_id or ctx.session_id}
                data["resumed_native"] = False  # Input fallback, not an observed native snapshot.
            data.update(_runtime_timeout(exc))
            data["native_facts"].update(parent_timeout=True, timeout_seconds=exc.timeout)
            return _preserve_failed_native_input(data)
        except (ImportError, OSError, ValueError, RuntimeError) as exc:
            if not _managed_linux:
                raise
            return _hermes_boundary_failure(exc)
        finally:
            _INPROC_EVENT_SINK.cb = previous_sink
    # dist fallback:纯 env 注入公司网关走 hermes 的 ai-gateway provider(不依赖用户 config.yaml)。
    # 实测(2026-08-28):--provider custom + --ignore-user-config 会 404——custom 的 base_url 只从
    # config.yaml 读,忽略 config 就没端点。ai-gateway provider 读 AI_GATEWAY_BASE_URL/API_KEY 环境变量,
    # 无需写 config,兼容 --ignore-user-config,干净环境即可跑。真测:ai-gateway+/v1/openai/native+gpt-5.4 → "OK"。
    binp = _sh.which("hermes") or str(_P.home() / ".local/bin/hermes")
    # provider 自适应(2026-09-10 服务器实锤):内网 pypi 只有 hermes 0.19.0——
    # 它没有 ai-gateway provider(0.20.5 才加),--provider ai-gateway 必败且只回 stderr。
    # 探测:先试 ai-gateway 帮助里有没有;没有则退 openai-api(读 OPENAI_BASE_URL/
    # OPENAI_API_KEY·0.19.0 本地实测 AIGC 网关+LongCat-2.0 → "2" RC=0)。
    _prov_args = ["--provider", "ai-gateway"]
    try:
        _help = _sp.run([binp, "--help"], capture_output=True, text=True, timeout=15)
        if "ai-gateway" not in (_help.stdout or "") + (_help.stderr or ""):
            env["OPENAI_BASE_URL"] = _gateway
            env["OPENAI_API_KEY"] = env.get("AIGC_APPID") or _UNIFIED_APPID
            _prov_args = ["--provider", "openai-api"]
    except Exception:
        pass
    env["AI_GATEWAY_BASE_URL"] = _gateway
    # ctx.appid 优先(单一事实源):env["AIGC_APPID"] 上面已按 ctx.appid or _UNIFIED_APPID 设好·
    # AI_GATEWAY_API_KEY 复用它·别再退回全局 _UNIFIED_APPID(否则场景分配的独立 appid 传不进 gateway provider)。
    env["AI_GATEWAY_API_KEY"] = env.get("AIGC_APPID") or _UNIFIED_APPID
    try:
        # ★权威成败判据=--usage-file(源码实证·2026-09-10 监督提供):hermes oneshot.py
        #   127-167 _write_usage_file 失败也写JSON,含 completed/failed/failure/model/tokens;
        #   233-243 stderr被redirect到devnull(不可靠),283-294 response非空即rc0(不可靠)。
        #   每单独立usage文件,退出后读JSON判:failed=true/completed=false→error(保留failure
        #   原文与usage);正常答案(哪怕解释401原理)completed=true完全不受影响。
        _usage_path = _hermes_ws / f"hermes_usage_{int(_t.time()*1000)}_{os.getpid()}.json"
        _usage_path.parent.mkdir(parents=True, exist_ok=True)
        r = _run_hermes_process(
            [binp, "-z", _legion_prompt(_q, ctx), "-m", _model] + _prov_args
            + ["--yolo", "--ignore-user-config", "--usage-file", str(_usage_path)], "hermes-cli")
        if _managed_linux and r.returncode and not r.stdout.strip() and not _usage_path.exists():
            return _hermes_boundary_failure(
                r.stderr or "Hermes CLI exited before its usage record", r.stderr)
        final = r.stdout.strip()[-_FINAL_CAP:]
        status = "ok" if r.returncode == 0 and "API call failed" not in final else "error"
        _usage = None
        _failure = ""
        try:
            _uj = _json.loads(_usage_path.read_text(encoding="utf-8"))
            _usage = {k: _uj.get(k) for k in ("model", "provider", "total_tokens",
                                              "input_tokens", "output_tokens",
                                              "estimated_cost_usd", "api_calls")}
            if _uj.get("failed") or _uj.get("completed") is False:
                status = "error"
                _failure = str(_uj.get("failure") or "")[:300] or "CLI usage-file 报告 failed"
        except Exception:
            pass  # usage文件缺失/损坏:退回rc/API-call-failed判据(不更严也不更松)
        return _preserve_failed_native_input({"final": final, "status": status,
                "elapsed": round(_t.time() - t0, 1), "stderr": _hermes_diagnostic(r.stderr),
                "model_used": _model, "usage": _usage,
                **({"error": _hermes_diagnostic(
                    _failure or r.stderr or "Hermes CLI returned an error")}
                   if status == "error" else {})})
    except (_sp.TimeoutExpired, FileNotFoundError) as exc:
        if _managed_linux and isinstance(exc, FileNotFoundError):
            return _hermes_boundary_failure(exc)
        return _preserve_failed_native_input({
                "final": "", "status": "timeout" if isinstance(exc,_sp.TimeoutExpired) else "unavailable",
                "elapsed": round(_t.time()-t0,1), "error": str(exc)[:200],
                **_runtime_timeout(exc)})
    except (ImportError, OSError, ValueError) as exc:
        if not _managed_linux:
            raise
        return _hermes_boundary_failure(exc)


def _deepseek_adapter(qhash, question, gold="", ctx=None):
    """DeepSeek Harness CLI adapter(依据 `dsh --help`: --profile headless <task>)。"""
    # ctx 化(权限下放·同 penguin 模式):失败回流拼进 prompt 让它自判换招(dsh 模型走 profile,
    # 换 model 需改 profile·超出本轮最小落地;先把失败回流接通·依据 deepseek timeout-policy
    # 把 error 结构化原样喂回)。老调用零回归。
    ctx = ctx or HarnessCtx.from_env()
    t0 = _t.time()
    binp = _sh.which("dsh") or str(_P.home() / ".npm-global/bin/dsh")
    env = dict(os.environ)
    # ctx.appid 优先(单一事实源·同 codex/hermes 2026-09-01 修法):旧版只读全局 _UNIFIED_APPID→
    # 场景选模型分配的独立 appid 传不进 deepseek·并发全挤一个 appid 撞限流;换机器没本机 appid 时挂。
    env.update({"DEEPSEEK_API_KEY": (getattr(ctx, "appid", "") or _UNIFIED_APPID),
                "DEEPSEEK_BASE_URL": getattr(ctx, "gateway", "") or _UNIFIED_GATEWAY,
                "DSH_TELEMETRY_DISABLED": "1"})
    # ★ 沙箱写权限修(2026-08-28 逐家测:deepseek workspace-write 禁写 ~/.meyo/legion/outputs·
    #   在它工作区外→做文件交付时它被迫改写别处)。用户"任何agent都可以写沙盒"。修:把 dsh 的
    #   工作目录(cwd)设成统一持久 workdir——它的沙箱工作区=我们的产物目录·它写工作区内就是写
    #   到持久目录·天然落盘+被 scan 收集·不再被沙箱禁。cwd 是"给对工作现场"·非 control。
    _wd = ctx.workdir or str(_P.home() / ".meyo" / "legion" / "outputs" / f"deepseek_{qhash}")
    _P(_wd).mkdir(parents=True, exist_ok=True)
    _profile_patch = None
    try:
        # 生产原生恢复：完整源码环境使用 DeepSeek 的 agents.resume + JSONL persistence bridge。
        # 依据 examples/headless-agent/tests/resume.e2e.ts:40-62；每轮固定 session_id，进程退出后
        # 下一轮从同一 persistence root 真恢复 event log。精简发布缺源码时才退旧 headless CLI。
        _resume_bridge = _P(__file__).resolve().parents[1] / "deepseek-harness" / "examples" / "headless-agent" / "legion-resume.ts"
        _checkpoint = (ctx.task_memory or {}).get("native_checkpoint") or {}
        _native_sid = str(_checkpoint.get("session_id") or ctx.session_id or f"legion-{qhash}")
        _persist_root = str(_P(ctx.memory_dir or _wd) / "deepseek-sessions")
        _source = _resume_bridge.parents[2] if _resume_bridge.exists() else None
        if _resume_bridge.exists() and _sh.which("pnpm"):
            _cmd = ["pnpm", "exec", "tsx", str(_resume_bridge), _wd, _persist_root,
                    _native_sid, _legion_prompt(_prior_attempts_prelude(ctx.prior_attempts) + question, ctx)]
            # C1(军团 deepseek dogfood 梳理):把父层选的 model 透传给桥(第5位参数·桥默认仍 flash)。
            # 旧版桥写死 deepseek-v4-flash·父层 select_usable_model 换模型对 deepseek 完全失效;
            # 现在 ctx.model 真传进去·deepseek 靠父层给的模型跑·换模型自愈生效。ctx.model 空则不传(桥用默认)。
            if getattr(ctx, "model", None):
                _cmd.append(str(ctx.model))
                # 桥的 provider 是第6位位置参数——军团统一走 deepseek-official(网关)·显式传防错位。
                _cmd.append("deepseek-official")
                # C6(模型上限收敛·2026-09-02):桥把 256k 默认上限原样发给网关第三方模型必 400
                # (GLM-5.3 抓包实锤"max_tokens参数非法[1,131072]")。按模型上限表传给桥·桥把它
                # 登记进 catalog 的 per-model maxTokens。表外模型(DeepSeek 系)不传保持原行为。
                _mt_cap = _GATEWAY_MODEL_MAX_TOKENS.get(str(getattr(ctx, "model", "")))
                if _mt_cap:
                    _cmd.append(str(_mt_cap))
            _launch_cwd = str(_source)
        else:
            _cmd = [binp, "--profile", "headless"]
            if ctx.model:
                # Native loader overlay, scoped to this invocation. The headless
                # default otherwise silently uses its own model instead of ctx.
                import tempfile as _tf
                _fd, _profile_patch = _tf.mkstemp(prefix="legion_dsh_", suffix=".json")
                _model_spec = {"id": ctx.model, "name": ctx.model,
                               "inputModalities": ["text"]}
                if ctx.max_tokens:
                    _model_spec["maxTokens"] = ctx.max_tokens
                elif ctx.model in _GATEWAY_MODEL_MAX_TOKENS:
                    _model_spec["maxTokens"] = _GATEWAY_MODEL_MAX_TOKENS[ctx.model]
                with os.fdopen(_fd, "w") as _patch_file:
                    _json.dump([
                        {"id": "agent-default-model", "config": {
                            "provider": "deepseek-official", "model": ctx.model}},
                        {"id": "llm-deepseek", "config": {"models": [_model_spec]}},
                    ], _patch_file)
                _cmd += ["--patch", _profile_patch]
            _cmd.append(_legion_prompt(_prior_attempts_prelude(ctx.prior_attempts) + question, ctx))
            # Native 0.1.5 headless session and filesystem use process.cwd().
            # Profile resolution is owned by DSH_HOME; it is not the workspace.
            _launch_cwd = str(_P(_wd).resolve())
        r = _run_task(_cmd, _WALL_S, env=env, cwd=_launch_cwd, tag="deepseek")
        final = r.stdout.strip()[-_FINAL_CAP:]
        parsed = None
        for line in reversed((r.stdout or "").splitlines()):
            try:
                obj = _json.loads(line)
                if isinstance(obj, dict) and obj.get("native_checkpoint"):
                    parsed = obj; break
            except Exception:
                continue
        status = "ok" if r.returncode == 0 else f"rc{r.returncode}"
        if "MISSING_CREDENTIAL" in r.stderr:
            status = "unavailable"
        if parsed:
            final = str(parsed.get("final") or "")[-_FINAL_CAP:]
            status = str(parsed.get("status") or status)
        # C5 最后一环(真跑定论:桥已回 model_used/error·但 adapter 旧版漏接→run_one 退回 tried_models[-1]
        # =父层选的模型如 gpt-5.4·把它记到 deepseek 名下污染 model_health·这才是"provenance 说谎"真根源·
        # 不是桥说谎是 adapter 漏接)。把桥回的真实 model_used/error 透传出去。
        _bridge_model = (parsed or {}).get("model_used")
        _bridge_err = (parsed or {}).get("error")
        return {"final": final, "status": status,
                "elapsed": round(_t.time() - t0, 1), "stderr": r.stderr[-500:],
                "model_used": _bridge_model or getattr(ctx, "model", None),
                "error": _bridge_err or "", "usage": (parsed or {}).get("usage"),
                "native_events": (parsed or {}).get("native_events") or [],
                "native_checkpoint": (parsed or {}).get("native_checkpoint"),
                "resumed_native": bool((parsed or {}).get("resumed_native"))}
    except _sp.TimeoutExpired as _te:
        # P0-2:deepseek 走原生 resume 桥·session_id 是父层生成(_native_sid)·persist_root 里有 event log·
        # timeout 时仍可 resume 续跑。返回 native_checkpoint(session_id)让下一轮从同一会话恢复·不从零重开。
        _sid = locals().get("_native_sid") or (ctx.session_id if ctx else "") or f"legion-{qhash}"
        return {**_runtime_timeout(_te), "final": "", "status": "timeout", "elapsed": round(_t.time() - t0, 1),
                "native_checkpoint": {"kind": "deepseek_session", "session_id": _sid}}
    except FileNotFoundError:
        return {"final": "", "status": "unavailable", "elapsed": 0.0, "error": "dsh CLI 未安装"}
    finally:
        if _profile_patch:
            with _ctx.suppress(OSError):
                _P(_profile_patch).unlink()


def _deepagents_adapter(qhash, question, gold="", ctx=None):
    """Upstream DeepAgents graph; the existing worker owns native checkpoint IO."""
    ctx = ctx or HarnessCtx.from_env()
    appid = getattr(ctx, "appid", "") or _UNIFIED_APPID
    if not appid:
        return {"status": "unavailable", "final": "", "error": "AIGC_APPID 未配置"}
    workdir = ctx.workdir or str(_P.home() / ".meyo" / "legion" / "outputs" / f"deepagents_{qhash}")
    _P(workdir).mkdir(parents=True, exist_ok=True)
    legion_tool = _legion_tool_path()
    worker = _P(legion_tool).with_name("deepagents_checkpoint.py")
    if not worker.is_file():
        return {"status": "unavailable", "final": "", "error": "DeepAgents native worker 未打包"}
    request = {
        "qhash": qhash, "question": _legion_prompt(_prior_attempts_prelude(ctx.prior_attempts) + question, ctx),
        "model": ctx.model, "gateway": getattr(ctx, "gateway", "") or _UNIFIED_GATEWAY,
        "appid": appid, "workdir": workdir, "user_id": ctx.user_id,
        "session_id": ctx.session_id or f"deepagents:{qhash}",
        "native_checkpoint": (ctx.task_memory or {}).get("native_checkpoint") or {},
        "max_tokens": getattr(ctx, "max_tokens", 0), "legion_tool": legion_tool,
        "gateway_output_limit": _GATEWAY_MODEL_MAX_TOKENS.get(ctx.model),
        "search_cli": _find_search_cli(), "python": "python3.12",
    }
    code = (f"import sys,json;sys.path.insert(0,{str(worker.parent)!r});"
            "import deepagents_checkpoint;"
            "print(json.dumps(deepagents_checkpoint.run_native(json.load(sys.stdin)),ensure_ascii=False))")
    started = _t.time()
    try:
        response = _run_task(["python3.12", "-c", code], _WALL_S,
                             input=_json.dumps(request, ensure_ascii=False), cwd=workdir,
                             env=dict(os.environ), tag="deepagents")
        for line in reversed(response.stdout.splitlines()):
            try:
                result = _json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(result, dict) or "status" not in result:
                continue
            result.setdefault("model_used", ctx.model)
            result.setdefault("usage_records", [])
            result.setdefault("workdir", workdir)
            result["elapsed"] = round(_t.time() - started, 2)
            if response.returncode:
                result["status"] = f"rc{response.returncode}"
            return result
        return {"status": f"rc{response.returncode}", "final": "", "workdir": workdir,
                "error": response.stderr[-500:] or "native DeepAgents worker returned no result"}
    except _sp.TimeoutExpired as _te:
        return {**_runtime_timeout(_te), "status": "timeout", "final": "", "workdir": workdir,
                "elapsed": round(_t.time() - started, 2), "error": "native DeepAgents worker timed out",
                "native_checkpoint": request["native_checkpoint"] or None,
                "native_facts": {"engine": "deepagents", "completed": False}}


def _auto_adapter(qhash, question, gold="", ctx=None):
    """auto 自研家(qa_auto_agent/thin loop)。subprocess 进程隔离调 run_auto_thin·
    自包含不经 run_comparison(军团执行不依赖 benchmark 文件)。找不到 agent 目录则 unavailable。"""
    # ctx 化(权限下放·同 deepseek 模式):auto 走自己已验证的 CCR 网关(Anthropic /v1/messages·
    # 模型名 sankuai,aws.claude-opus 体系),与军团网关的 gpt-5.6-sol(OpenAI native)不同协议——
    # 强塞 ctx.model 会走错协议崩掉。故只接失败回流拼进 question 让它看见"上轮怎么挂"自主换招
    # (依据 deepseek timeout-policy 把 error 原样喂回),model 让 auto 自己定。老调用零回归。
    ctx = ctx or HarnessCtx.from_env()
    # auto 使用自己的 native 执行引擎，但共享能力、持久目录、任务事实与发送前预算必须和其余家
    # 对称。统一走 _legion_prompt，避免这条特例链再次绕过全局上下文预算。
    question = _legion_prompt(_prior_attempts_prelude(ctx.prior_attempts) + question, ctx)
    _here = _P(os.path.dirname(os.path.abspath(__file__)))
    # agent 目录:主仓 seed-users-system/qa_system/agent
    _agent = _here.parent.parent / "seed-users-system" / "qa_system" / "agent"
    if not (_agent / "run_auto_thin.py").exists():
        return {"final": "", "status": "unavailable", "elapsed": 0.0,
                "error": "auto 需完整仓库(run_auto_thin 不在 dist)"}
    t0 = _t.time()
    code = (f"import sys; sys.path.insert(0, {str(_agent)!r})\n"
            f"from run_auto_thin import run_bc_with_hard_wall\n"
            f"import json\n"
            f"r = run_bc_with_hard_wall(question={_json.dumps(question)}, "
            f"qid={_json.dumps(str(((ctx.task_memory or {}).get('native_checkpoint') or {}).get('session_id') or qhash))}, "
            f"wall_s={_WALL_S or None}, resume=True, user_id={_json.dumps(ctx.user_id)})\n"
            f"print(json.dumps({{'final': str(r.get('final','')), 'status': r.get('status','ok'), "
            f"'usage_records': r.get('usage_records', []), 'model_used': r.get('model_used',''), "
            f"'native_checkpoint': r.get('native_checkpoint'), 'resumed_native': r.get('resumed_native', False)}}, ensure_ascii=False))")
    try:
        r = _run_task(["python3.12", "-c", code], _WALL_S,
                      env=dict(os.environ, MEYO_AGENT_BACKEND="native"), tag="auto")
        for line in reversed(r.stdout.splitlines()):
            if line.strip().startswith("{"):
                d = _json.loads(line)
                return {"final": str(d.get("final", ""))[:_FINAL_CAP],
                        "status": d.get("status", "ok"), "elapsed": round(_t.time() - t0, 1),
                        "model_used": d.get("model_used") or "auto-managed",
                        "usage_records": d.get("usage_records") or [],
                        "native_checkpoint": d.get("native_checkpoint"),
                        "resumed_native": bool(d.get("resumed_native"))}
        return {"final": "", "status": f"rc{r.returncode}", "elapsed": round(_t.time() - t0, 1),
                "error": r.stderr[-300:]}
    except _sp.TimeoutExpired as _te:
        _checkpoint = (ctx.task_memory or {}).get("native_checkpoint") or {}
        _checkpoint_id = str(_checkpoint.get("session_id") or qhash)
        return {**_runtime_timeout(_te), "final": "", "status": "timeout", "elapsed": round(_t.time() - t0, 1),
                "native_checkpoint": {"kind": "auto_checkpoint", "session_id": _checkpoint_id},
                "resumed_native": bool(_checkpoint)}


def _omnigent_adapter(qhash, question, gold="", ctx=None):
    """显式 ctx 原样交给 backend；不能用丢参重试或默认模型冒充请求绑定。"""
    _explicit_ctx = ctx is not None
    ctx = ctx or HarnessCtx.from_env()
    question = _prior_attempts_prelude(ctx.prior_attempts) + question
    _checkpoint = (ctx.task_memory or {}).get("native_checkpoint") or {}
    _resume_id = str(_checkpoint.get("session_id") or "") if isinstance(_checkpoint, dict) else ""
    try:
        import inspect as _inspect
        import os as _os
        _mh = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "meyo_harnesses")
        if _mh not in _sys.path:
            _sys.path.insert(0, _mh)
        from backends import omnigent_backend as _ob
        _runtime_env = {"MEYO_SESSION_ID": ctx.session_id, "MEYO_USER_ID": ctx.user_id,
                        "MEYO_WORKDIR": ctx.workdir}
        _kwargs = {"prefer_model": ctx.model, "resume_id": _resume_id, "runtime_env": _runtime_env}
        if _explicit_ctx:
            _kwargs.update(appid=ctx.appid, gateway=ctx.gateway, native_checkpoint=_checkpoint)
        _params = _inspect.signature(_ob.run).parameters
        if not any(p.kind == p.VAR_KEYWORD for p in _params.values()):
            _unsupported = set(_kwargs) - set(_params)
            if _unsupported and _explicit_ctx:
                return {
                    "final": "", "status": "error", "elapsed": 0.0, "resumed_native": False,
                    "native_checkpoint": _checkpoint or None,
                    "error": "当前 Omnigent backend 不支持完整请求绑定；未丢参执行",
                    "error_envelope": {"category": "harness_runtime", "scope": "harness",
                                       "retryable": False, "code": "backend_binding_unsupported"},
                }
            # 仅老式无 ctx 调用兼容旧签名，执行一次；不捕获内部 TypeError 后重派任务。
            _kwargs = {k: v for k, v in _kwargs.items() if k in _params}
        return _ob.run(qhash, _legion_prompt(question, ctx), gold, **_kwargs)
    except Exception as e:
        return {"final": "", "status": "unavailable", "elapsed": 0.0,
                "native_checkpoint": _checkpoint or None, "resumed_native": False,
                "error": f"omnigent backend 不可用:{type(e).__name__}: {str(e)[:160]}",
                "error_envelope": {"category": "harness_runtime", "scope": "harness",
                                   "retryable": False, "code": "backend_invocation_error"}}


def _external_adapter(key):
    """构造目标入口；依赖、授权和 runtime 检查仍由实际执行的 registry.run 完成。"""
    def adapter(qhash, question, gold="", ctx=None):
        ctx = ctx or HarnessCtx.from_env()
        service = _P(__file__).resolve().parents[2] / "release" / "legion-service"
        if str(service) not in _sys.path:
            _sys.path.insert(0, str(service))
        try:
            import external_agent_registry as registry
            env = {"AIGC_APPID": ctx.appid, "MEYO_UNIFIED_APPID": ctx.appid,
                   "MEYO_UNIFIED_GATEWAY": ctx.gateway}
            runtime_context = {
                "user_id": ctx.user_id, "session_id": ctx.session_id or qhash,
                "memory_dir": ctx.memory_dir, "tried_models": ctx.tried_models,
                "prior_attempts": ctx.prior_attempts, "fallback_models": ctx.fallback_models,
                "delivery_contract": ctx.delivery_contract, "runtime_brief": ctx.runtime_brief,
                "task_facts": ctx.task_facts, "capability_policy": ctx.capability_policy,
                "task_memory": ctx.task_memory, "peer_source_context": ctx.peer_source_context,
                "project_context": ctx.project_context, "stable_context": ctx.stable_context,
                "prompt_cache_key": ctx.prompt_cache_key,
                "tool_cli": str(service / "legion_tool.py"), "workdir": ctx.workdir,
            }
            def _relay_external_event(event):
                _sys.stderr.write(_LEGION_EV_PREFIX + _json.dumps(event, ensure_ascii=False) + "\n")
                _sys.stderr.flush()
            adapter_capabilities = _external_adapter_capabilities(key)
            runtime_context["adapter_capabilities"] = adapter_capabilities
            return registry.run(key, _legion_prompt(
                                    _prior_attempts_prelude(ctx.prior_attempts) + question, ctx,
                                    adapter_capabilities=adapter_capabilities),
                                workdir=ctx.workdir, env=env, model=ctx.model,
                                session_id=ctx.session_id or qhash, runtime_context=runtime_context,
                                # worker 已监测总预算/取消；transport 不再暗加默认 900s。
                                timeout_s=_WALL_S, event_cb=_relay_external_event)
        except Exception as exc:
            return {"final": "", "status": "unavailable", "elapsed": 0.0,
                    "error": f"external agent {key} 不可用:{type(exc).__name__}:{str(exc)[:160]}"}
    adapter.__name__ = f"_{key.replace('-', '_')}_adapter"
    return adapter


# dist 独立时的自包含执行池(不依赖 run_comparison)。入口由统一 manifest 的 adapter 符号派生；
# 文件不可读时才退旧映射，避免 manifest 故障把全部家从池里抹掉。
_ADAPTER_FALLBACK = {
    "cc": _cc_adapter,
    "codex": _codex_adapter,
    "penguin": _penguin_adapter,
    "jcode": _jcode_adapter,
    "prime": _prime_adapter,
    "deepagents": _deepagents_adapter,
    "hermes": _hermes_adapter,
    "deepseek": _deepseek_adapter,
    "omnigent": _omnigent_adapter,
}
try:
    _service = _P(__file__).resolve().parents[2] / "release" / "legion-service"
    if str(_service) not in _sys.path:
        _sys.path.insert(0, str(_service))
    import runtime_manifest as _runtime_manifest
    _STANDALONE_ADAPTERS = _runtime_manifest.resolve_builtin_adapters(__name__)
    if not _STANDALONE_ADAPTERS:
        _STANDALONE_ADAPTERS = dict(_ADAPTER_FALLBACK)
except Exception:
    _STANDALONE_ADAPTERS = dict(_ADAPTER_FALLBACK)


def _external_adapters() -> dict:
    """动态加入已安装且有已验证 headless runner 的外部 Agent。"""
    service = _P(__file__).resolve().parents[2] / "release" / "legion-service"
    if str(service) not in _sys.path:
        _sys.path.insert(0, str(service))
    try:
        import external_agent_registry as registry
        catalog = {item["key"]: item for item in registry.load_catalog()}
        return {row["key"]: _external_adapter(row["key"])
                for row in registry.probe().get("agents", [])
                if row.get("execution_ready") and not catalog.get(row["key"], {}).get("provided_by")}
    except Exception:
        return {}


def _builtin_adapters() -> dict:
    """内置 adapter，也走探活入池（取消 core 双标·用户 2026-09-01"没有所谓 core·都一样"）。

    根因:旧 load_harness_pool 无条件塞 _STANDALONE_ADAPTERS·外部家才过探活——这是双标:
    codex 在没装的机器上一样跑不了·却因写死在 dict 里就"永远是 core"入池。harness 本身没差别·
    入池判据应统一为"探到依赖才算数"(和 _external_adapters 同一条路)。探活复用 legion_core
    _aigc_ready 的 _dep_ok（which/import 级·已按每家真实依赖判·缓存不引入派单开销）。

    ★铁律0 稳定兜底:探活函数异常/拿不到结果时【回退全量入池】——绝不因探活抖动把本来能跑的家
    搞掉池(稳定压倒纯净)。探活是"诚实剔除真没装的"·不是"抖一下就误杀"。
    """
    try:
        import sys as _s2
        _svc = str(_P(__file__).resolve().parents[2] / "release" / "legion-service")
        if _svc not in _s2.path:
            _s2.path.insert(0, _svc)
        import legion_core as _lc
        _ready = _lc._aigc_ready(cached_only=True)          # 用缓存·零派单开销
        _runnable = set(_ready.get("runnable_clis") or [])
        if not _runnable:                                   # 缓存空(冷启动)→别误杀·全量入池
            return dict(_STANDALONE_ADAPTERS)
        # 探到依赖的才入池；退役/纯工具角色不因缓存或探活异常重新注册。
        out = {k: fn for k, fn in _STANDALONE_ADAPTERS.items() if k in _runnable}
        return out or dict(_STANDALONE_ADAPTERS)            # 全被剔=探活异常·兜底全量
    except Exception:
        return dict(_STANDALONE_ADAPTERS)                   # 任何异常→铁律0 兜底全量


def load_harness_pool():
    """军团执行池：内置 + 外部统一走探活入池（无 core 双标·探到依赖才算数·带稳定兜底）。"""
    return {**_builtin_adapters(), **_external_adapters()}


def resolve_harness_adapter(key: str):
    """Resolve one execution entry without a fleet probe or a readiness claim.

    Builtins keep their own runtime checks. External entries still execute via
    registry.run, which checks the selected member using this call's environment.
    Full catalog health inspection remains the job of load_harness_pool().
    """
    adapter = _STANDALONE_ADAPTERS.get(key)
    if adapter is not None:
        return adapter
    service = _P(__file__).resolve().parents[2] / "release" / "legion-service"
    if str(service) not in _sys.path:
        _sys.path.insert(0, str(service))
    try:
        import external_agent_registry as registry
        item = next((row for row in registry.load_catalog() if row.get("key") == key), None)
        if not item or item.get("provided_by"):
            return None  # provided_by does not create a second independent runner.
        if item.get("execution_role") in ("capability", "control-plane", "ui-platform"):
            return None
        return _external_adapter(key)
    except Exception:
        return None


_AUTONOMY_MATRIX_CACHE = {"at": 0.0, "data": None}


def autonomy_matrix(force: bool = False) -> dict:
    """机器可判定的 adapter 能力声明；未知能力不冒充支持。

    ★60s 缓存(用户 2026-09-01 dispatch 链路体检揪出:规划器每次派活都调本函数拿 cost_class
    速度画像·而末尾 registry.probe() 实时探所有外部 agent execution_ready·冷 ~11s→简单题规划
    卡 9-11s·这是用户每次问问题都付的固定开销·比 deploy 值钱得多)。cost_class 是【静态能力
    画像】·不需要每次派活实时 probe 外部 agent。缓存后规划前置 11s→0s。60s 刷一次足够。"""
    import time as _t2
    if not force and _AUTONOMY_MATRIX_CACHE["data"] is not None and \
            _t2.time() - _AUTONOMY_MATRIX_CACHE["at"] < 60:
        return _AUTONOMY_MATRIX_CACHE["data"]
    # resume 对齐 Omnigent HarnessCapabilities.Resume：native / cold_replay / none。
    # cost_class = 响应速度画像(总指挥据此简单题选快家·重家留长板硬题)。★2026-09-01 按 inproc
    # 优化后的真实测速对齐(用户"找长板不让重家做简单的"):进程内直调后 codex/cc/hermes/penguin/
    # jcode/prime 都进 5-10s→light/standard;auto(native引擎)/deepseek(dsh Node)真慢 40-50s→heavy
    # (它们长板=多轮考据/深推理硬题·简单题不该派);deepagents/omnigent 中(15-17s)→standard。
    # 旧画像是 inproc 前的(codex/hermes 标 heavy 已过时·会让总指挥误判快家为慢家)。
    # ★画像单一事实源(用户 2026-09-01 定"统一成 manifest 动态发现·彻底根治"):specs 优先读
    #   release/legion-service/harness_manifest.json·下面手写 specs 退为【manifest 缺失/该家没登记时
    #   的兜底】(铁律0:文件坏了/新家没写 manifest 也不崩)。决策记录在 manifest 的 _decisions·
    #   改画像先看那(别再考古注释)。
    specs = {}
    try:
        import runtime_manifest as _runtime_manifest
        for _k, _sp in _runtime_manifest.autonomy_specs().items():
            if _sp.get("integration"):
                specs[_k] = tuple(_sp.get(f) for f in (
                    "integration", "streaming", "resume", "checkpoint", "tools", "artifacts",
                    "interrupt", "policy", "model", "cost_class", "usage", "compaction"))
    except Exception:
        specs = {}
    # 兜底(manifest 缺失/该家没登记时用·值=归一时的原值·一字未改):
    _SPECS_FALLBACK = {
        "cc": ("cli-subprocess", "events", "native", True, True, True, True, "allow-all", "self_managed", "light", "native", "native_internal"),
        # codex 画像 light→standard(2026-09-01 loop 真跑纠偏):light 是【inproc 假设】下定的
        # (注释也写"进程内直调后 5-10s→light")·但 codex 因 responses bridge inproc 全 rc1 已【移出
        # inproc 走子进程】→真实简单题 14-24s(cc/hermes/penguin 快家仅 8s)。画像没跟着更新→planner
        # 误判 codex"快"→简单题总选它 solo·20-24s(用户"简单题不该慢"真痛点)。改 standard:planner
        # 简单题优先真快家(light)·codex 留复杂题(它 solo 简单题是错配)。
        "codex": ("cli-subprocess", "events", "native", True, True, True, True, "workspace", "native", "standard", "native", "native_internal"),
        "penguin": ("cli-subprocess", "lines", "native", True, True, True, True, "allow-all", "native", "light", "native", "native_events"),
        "jcode": ("cli-subprocess", "complete", "native", True, True, True, True, "profile", "native", "standard", "native_if_reported", "unknown"),
        "prime": ("cli-subprocess", "events", "native", True, True, True, True, "profile", "native", "light", "native_if_reported", "native_internal"),
        "deepagents": ("sdk-subprocess", "complete", "cold_replay", True, True, True, True, "workdir", "native", "standard", "native_if_reported", "none"),
        "hermes": ("sdk-subprocess", "events", "native", True, True, True, True, "yolo", "native", "light", "native", "native_internal"),
        "deepseek": ("sdk-subprocess", "events", "native", True, True, True, True, "profile", "self_managed", "heavy", "native", "native_events"),
        "auto": ("sdk-subprocess", "lines", "checkpoint", True, True, True, True, "native", "self_managed", "heavy", "native_role_ledger", "native_internal"),
        "omnigent": ("meta-harness", "complete", "native", True, True, True, True, "nested", "native", "standard", "unavailable", "nested"),
    }
    for _k, _v in _SPECS_FALLBACK.items():
        specs.setdefault(_k, _v)
    fields = ("integration", "streaming", "resume", "checkpoint", "tools", "artifacts",
              "interrupt", "policy", "model", "cost_class", "usage", "compaction")
    out = {}
    for key in _STANDALONE_ADAPTERS:
        vals = specs.get(key) or _SPECS_FALLBACK.get(key)
        if not vals:
            continue
        out[key] = dict(zip(fields, vals))
        out[key].update({"ctx": True, "task_facts": True, "error_envelope": True,
                         "task_memory": True, "delivery_contract": "prompt",
                         "max_tokens": "native" if key == "hermes" else "harness_default",
                         "max_iterations": "native" if key == "hermes" else "harness_default",
                         "fallback": "native+parent" if key == "hermes" else "parent_retry",
                         "declared": True})
    try:
        service = _P(__file__).resolve().parents[2] / "release" / "legion-service"
        if str(service) not in _sys.path:
            _sys.path.insert(0, str(service))
        import external_agent_registry as registry
        catalog = {item["key"]: item for item in registry.load_catalog()}
        for row in registry.probe().get("agents", []):
            if not row.get("execution_ready"):
                continue
            item = catalog.get(row["key"], {})
            declared = item.get("adapter_capabilities") or {}
            protocols = row.get("protocols") or []
            out[row["key"]] = {
                "integration": "+".join(protocols) or "external",
                "streaming": declared.get("streaming", "events" if "cli" in protocols else "complete"),
                "resume": declared.get("resume", "cold_replay"),
                "checkpoint": declared.get("checkpoint", True),
                "tools": declared.get("tools", True),
                "artifacts": declared.get("artifacts", True),
                "interrupt": declared.get("interrupt", False), "policy": "context",
                "model": declared.get("model", "runtime"),
                # 外部 agent 未声明成本时归为 standard(中性合法值);external 不在 {light,standard,heavy}
                # 契约集里,会让 autonomy_matrix 校验失败(上线前真 bug)。
                "cost_class": declared.get("cost_class") if declared.get("cost_class") in {"light", "standard", "heavy"} else "standard",
                "usage": declared.get("usage", "unavailable"),
                "compaction": declared.get("compaction", "none"),
                "ctx": True, "task_facts": True, "error_envelope": True, "task_memory": True,
                "delivery_contract": "prompt", "fallback": "parent_retry", "declared": True,
            }
    except Exception:
        pass
    _AUTONOMY_MATRIX_CACHE["data"] = out
    _AUTONOMY_MATRIX_CACHE["at"] = __import__("time").time()
    return out


def available() -> bool:
    """军团执行池是否已注册(独立于 run_comparison)。"""
    return bool(_STANDALONE_ADAPTERS)
