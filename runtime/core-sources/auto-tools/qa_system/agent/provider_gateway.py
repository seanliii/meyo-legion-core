# -*- coding: utf-8 -*-
"""provider_gateway · LLM provider/gateway 适配层(2026-08-27 从 qa_auto_agent god-file 抽出)。

职责: 把"跟具体 provider/网关打交道"的所有适配逻辑(通道级 400 重试、消息消毒、
longcat 兼容、tool_use/tool_result 配对修复、上下文溢出降级、gateway ChatModel 构建)
收进一个内聚模块·让 19k 行主文件专注执行主循环。零 auto 反向依赖:模型常量来自
qa_llm_gateway(与 auto 同源)·langchain 直接 import。

auto 通过 `from provider_gateway import *` 拿回这些符号(向后兼容·调用点零改动)。
"""
import os, sys, time, json, random
from qa_llm_gateway import (_config, DEFAULT_MODEL, HAIKU_MODEL, SONNET_MODEL,
                            resolve_model_name, HEAVY_FALLBACK_MODEL)


# 请求级抽奖;langchain/Anthropic SDK 对 400 不重试,deepagents 循环一撞即死。
# 图级重试(invoke 重放)会丢进度重来(已实证撤销)——正确位置是【模型调用级】:
# 包装 bind_tools 返回的 Runnable,对通道类 400 sleep 后同请求重发(重新抽奖),
# 图状态/消息完全不丢。deepagents 每轮 LLM 调用都经过这里,覆盖主循环+子代理。

# v115 · 提前导入 ChatAnthropic·供 _ChannelRetryModel 类定义继承
from langchain_anthropic import ChatAnthropic


def _is_channel_400(exc: Exception) -> bool:
    s = f"{type(exc).__name__}: {exc}".lower()
    return any(k in s for k in ("not allowed", "error code: 400",
                                "provider_response_error", "upstream_vendor",
                                "access to anthropic"))


# v165(2026-08-18 · Q501 v163 实证) · 上下文超长 BadRequest ≠ 通道抽奖。
#   病根: _is_channel_400 把裸 "error code: 400" 也当通道抽奖——同请求重发 3 次
#   必然全挂。Q501 v163 收尾时 finalize 消息带 180+ 条证据·longcat-2.0 直接
#   BadRequest·重试 3 次白烧 6s+ 后整题收尾失败(no_llm_response 是下游链式污染)。
#   正确做法: 识别超长类 400 → 压缩消息(保留 system+首问+尾部·中间 ToolMessage
#   内容头尾截断)后再重发——笨模型(longcat)上下文窗口小·必须靠框架自适应。
_CTX_LEN_MARKERS = (
    "maximum context length", "context_length_exceeded", "prompt is too long",
    "input length and max_tokens", "too many tokens", "context window",
    "exceeds the maximum", "request too large", "payload too large",
    "input is too long", "maximum_length", "reduce the length",
)


def _is_ctx_len_400(exc: Exception) -> bool:
    s = f"{type(exc).__name__}: {exc}".lower()
    return any(k in s for k in _CTX_LEN_MARKERS)


def _is_ctx_overflow_guess(exc: Exception, inp) -> bool:
    """v165b · 不依赖错误文案的兖底判断: BadRequest类异常 + 输入体积超阈 → 视为超长。

    longcat 网关的 BadRequest 文案不带 "context length" 字样(实证 v165 仍走通道
    抽奖路径)·改用体积启发: 异常是 400/BadRequest 且消息总量>阈(≈60k tokens)
    → 压缩重发。压缩后仍 400 会由通道分支接管·不会死循环。
    """
    try:
        s = f"{type(exc).__name__}: {exc}".lower()
        if not ("400" in s or "badrequest" in s or "invalid_request" in s):
            return False
        if not isinstance(inp, (list, tuple)):
            return False
        total = 0
        for m in inp:
            c = getattr(m, "content", "")
            if isinstance(c, str):
                total += len(c)
            elif isinstance(c, list):
                total += len(json.dumps(c, ensure_ascii=False, default=str))
        return total > 200_000  # ~50k+ tokens
    except Exception:
        return False


def _is_tool_pairing_400(exc: Exception) -> bool:
    """v165d · Anthropic 类 400: tool_use/tool_result 不配对。

    实证(5q v165c Q1): "messages.72: `tool_use` ids were found with[out
    corresponding tool_result]"——LangGraph 中断/硬墙打断后消息序列残留
    缺失 tool_result 的 tool_use 或孤儿 tool_result·opus 通道直接 400。
    """
    s = f"{type(exc).__name__}: {exc}".lower()
    return ("tool_use" in s and ("without" in s or "tool_result" in s or "ids were found" in s)) \
        or ("tool_result" in s and "without" in s and "tool_use" in s)


def _fix_tool_pairing(inp):
    """修复消息序列的 tool_use/tool_result 配对(Anthropic 硬约束):
      ① AI 消息带 tool_call 但后面没对应 ToolMessage → 补占位 ToolMessage;
      ② 孤儿 ToolMessage(前面无任何 AI tool_use 引用其 id) → 删除。
    返回修复后列表; 无需修复返 None。fail-safe 任何异常返 None。
    """
    try:
        if not isinstance(inp, (list, tuple)) or not len(inp):
            return None
        msgs = list(inp)
        if not all(hasattr(m, "type") for m in msgs):
            return None
        # 收集所有 tool_use id 与已有 tool_result id
        use_ids, result_ids = [], set()
        for m in msgs:
            if m.type == "ai":
                for tc in (getattr(m, "tool_calls", None) or []):
                    if isinstance(tc, dict) and tc.get("id"):
                        use_ids.append(tc["id"])
            elif m.type == "tool":
                tid = getattr(m, "tool_call_id", None)
                if tid:
                    result_ids.add(tid)
        missing_results = [tid for tid in use_ids if tid not in result_ids]
        orphan_results = result_ids - set(use_ids)
        if not missing_results and not orphan_results:
            return None
        from langchain_core.messages import ToolMessage as _TM
        out = []
        for m in msgs:
            if m.type == "tool":
                tid = getattr(m, "tool_call_id", None)
                if tid and tid in orphan_results:
                    continue  # 删孤儿 tool_result
            out.append(m)
            if m.type == "ai":
                tcs = getattr(m, "tool_calls", None) or []
                for tc in tcs:
                    if isinstance(tc, dict) and tc.get("id") in missing_results:
                        out.append(_TM(content="[tool result omitted by pairing-repair]",
                                        tool_call_id=tc["id"]))
        return out
    except Exception:
        return None


def _flatten_system_messages(inp):
    """将 SystemMessage 的 content blocks 数组拍平成字符串（longcat 兼容）。

    longcat-2.0 的上游仅接受字符串形式的 system；deepagents 会生成
    ``[{"type": "text", "text": ...}]``，从而触发 400。CCR 层已有
    transformer，此处作为不依赖路由器的调用侧兜底。
    """
    try:
        if not isinstance(inp, (list, tuple)):
            return inp
        out, changed = [], False
        for msg in inp:
            content = getattr(msg, "content", None)
            if getattr(msg, "type", "") != "system" or not isinstance(content, list):
                out.append(msg)
                continue
            parts = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif block is not None:
                    parts.append(json.dumps(block, ensure_ascii=False, default=str))
            flattened = "\n\n".join(parts)
            updated = _msg_recontent(msg, flattened)
            out.append(updated if updated is not None else msg)
            changed = changed or updated is not None
        if not changed:
            return inp
        return tuple(out) if isinstance(inp, tuple) else out
    except Exception:
        return inp


def _msg_recontent(msg, new_content):
    """复制 LangChain 消息并替换 content(pydantic v1/v2 双兼容)·失败返 None。"""
    try:
        if hasattr(msg, "model_copy"):
            return msg.model_copy(update={"content": new_content})
        if hasattr(msg, "copy"):
            try:
                return msg.copy(update={"content": new_content})
            except TypeError:
                return None
    except Exception:
        return None
    return None


# v127(2026-08-18 · 用户实证: longcat 网关 400「请求格式有误」被当通道抽奖
#   盲重发 3 次全挂·每题白烧 6s+ 退避)。确定性请求格式 400 ≠ 通道抽奖：
#   同请求重发必然再挂。正确做法：识别格式类 400 → 先把消息降级到网关最大
#   兼容形态(全角色 content 拍平/空内容补位/工具内容强制字符串)再重发一次；
#   仍挂立即抛——绝不进盲重试路径烧墙钟。
_FMT_400_MARKERS = ("请求格式有误", "格式有误", "格式错误", "invalid request format",
                    "malformed request", "message format", "invalid 'tools'", 'invalid "tools"',
                    "tools: array too long", "tool array too long", "too many tools")


def _is_format_400(exc: Exception) -> bool:
    """确定性请求格式 400(网关校验拒收)——同请求重发必然再挂·不能进盲重试。"""
    s = f"{type(exc).__name__}: {exc}".lower()
    return ("400" in s or "badrequest" in s) and any(k in s for k in _FMT_400_MARKERS)


def _is_tool_schema_400(exc: Exception) -> bool:
    """Tool schema size/count rejection is deterministic; retrying messages or models is waste."""
    s = f"{type(exc).__name__}: {exc}".lower()
    return ("400" in s or "badrequest" in s) and any(k in s for k in (
        "invalid 'tools'", 'invalid "tools"', "tools: array too long",
        "tool array too long", "too many tools"))


def _is_any_400(exc: Exception) -> bool:
    """v170 · 任何 400/BadRequest/invalid_request(不看具体文案)。

    longcat 400 文案多变(「请求格式有误」/「error code: 400」/裸 BadRequest)·
    早期分支靠精确文案匹配会漏。longcat 运行时统一按 400 处理: 先重规范化+硬截断·
    保证每次重发 payload 物理不同——不匹配文案·只匹配 400 家族。fail-safe。
    """
    try:
        s = f"{type(exc).__name__}: {exc}".lower()
        return ("400" in s or "badrequest" in s or "invalid_request" in s
                or "bad request" in s)
    except Exception:
        return False


def _msgs_differ(a, b) -> bool:
    """两个消息列表内容是否不同(判断重规范化/截断是否真产生了变化·否则别重发同一 payload)。

    比较 (type, content 的字符串化)。fail-safe: 出错时保守返 True(允许重试一次)。
    """
    try:
        if a is b:
            return False
        if not isinstance(a, (list, tuple)) or not isinstance(b, (list, tuple)):
            return a is not b
        if len(a) != len(b):
            return True

        def _sig(m):
            t = getattr(m, "type", "")
            c = getattr(m, "content", "")
            cs = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False, default=str)
            return (t, cs)

        return [_sig(m) for m in a] != [_sig(m) for m in b]
    except Exception:
        return True


def _sanitize_for_provider(inp):
    """v127 · 格式类 400 的调用侧修复: 全角色 content 降级到最大兼容形态。

    与 _normalize_longcat_messages 的差别：那个做常规规整(system 拍平/连续
    user 合并)；这里在网关已明确拒收格式后做最后降级——任意角色 list 拍平成
    文本、非字符串强制字符串、空内容补占位、未知角色消息剔除，末了自检
    tool 配对完整性。返回修复后列表；无可修返 None(调用方停止重试)。fail-safe。
    """
    try:
        if not isinstance(inp, (list, tuple)) or not len(inp):
            return None
        msgs = list(inp)
        if not all(hasattr(m, "type") for m in msgs):
            return None
        out, changed = [], False
        for m in msgs:
            t = getattr(m, "type", "")
            if t not in ("system", "human", "ai", "tool"):
                changed = True          # 未知角色消息剔除(网关常见拒收点)
                continue
            content = getattr(m, "content", "")
            if isinstance(content, list):
                parts = []
                for b in content:
                    if isinstance(b, str):
                        parts.append(b)
                    elif isinstance(b, dict) and isinstance(b.get("text"), str):
                        parts.append(b["text"])
                    elif b is not None:
                        parts.append("[block omitted: %s]" % type(b).__name__)
                new_c = "\n".join(p for p in parts if p) or "[content flattened]"
                m2 = _msg_recontent(m, new_c)
                if m2 is None:
                    continue
                out.append(m2)
                changed = True
                continue
            if not isinstance(content, str):
                new_c = json.dumps(content, ensure_ascii=False, default=str)[:8000]
                m2 = _msg_recontent(m, new_c)
                if m2 is None:
                    continue
                out.append(m2)
                changed = True
                continue
            if content.strip() == "":
                m2 = _msg_recontent(m, "[empty message filled]")
                if m2 is None:
                    continue
                out.append(m2)
                changed = True
                continue
            out.append(m)
        if not changed:
            return None
        # 修复后必须仍是 tool 配对完整的序列·交给网关前自检一次
        repaired = _fix_tool_pairing(out)
        return repaired if repaired is not None else out
    except Exception:
        return None


def _normalize_longcat_messages(inp):
    """规整长猫不接受的连续 user 消息，保留全部内容和 tool 配对。

    实证：`[HumanMessage("原题"), HumanMessage("意图账本提示")]` 对
    longcat-2.0 必然返回 400；将两段合为一条 user 消息则正常。Native
    引擎会在首轮把意图账本作为第二条 HumanMessage 注入，故在模型调用边界
    统一合并，避免让各引擎各自处理协议细节。
    """
    try:
        normalized = _flatten_system_messages(inp)
        if not isinstance(normalized, (list, tuple)):
            return normalized
        out, changed = [], False
        for msg in normalized:
            if (getattr(msg, "type", "") == "human" and out
                    and getattr(out[-1], "type", "") == "human"):
                prev = out[-1]
                pcontent, content = getattr(prev, "content", ""), getattr(msg, "content", "")
                if isinstance(pcontent, str) and isinstance(content, str):
                    combined = f"{pcontent}\n\n{content}"
                elif isinstance(pcontent, list) and isinstance(content, list):
                    combined = [*pcontent, *content]
                elif isinstance(pcontent, list):
                    combined = [*pcontent, content]
                elif isinstance(content, list):
                    combined = [pcontent, *content]
                else:
                    combined = f"{pcontent}\n\n{content}"
                updated = _msg_recontent(prev, combined)
                if updated is None:
                    out.append(msg)  # fail-safe：复制失败时不篡改消息
                else:
                    out[-1] = updated
                    changed = True
                continue
            out.append(msg)
        if not changed:
            return normalized
        return tuple(out) if isinstance(normalized, tuple) else out
    except Exception:
        return inp


def _longcat_textualize_tool_history(inp):
    """将已完成的 tool_use/tool_result 历史转成文本证据，绕过 longcat 回放兼容缺口。

    长猫可以正常产生新的工具调用，却会对 Anthropic 标准的历史 tool_result
    回放返回 400/503（v169 最小复现）。已执行工具的语义并不依赖协议块本身，
    因而在下一轮前将其变为带名称、参数和结果的用户上下文；模型仍保留完整
    工具定义，可继续发起新的调用。仅 longcat 运行启用，其他模型不改变消息协议。
    """
    try:
        model_name = os.environ.get("MEYO_QA_GATEWAY_MODEL", "").lower()
        if "longcat" not in model_name or not isinstance(inp, (list, tuple)):
            return inp
        if not any(getattr(msg, "type", "") == "tool" for msg in inp):
            return inp
        from langchain_core.messages import HumanMessage
        lines = ["以下是当前任务与已执行工具的可信记录。基于记录继续推进；如还需信息，请直接调用工具。"]
        for msg in inp:
            kind = getattr(msg, "type", "")
            content = getattr(msg, "content", "")
            if kind == "human":
                lines.append(f"[任务/补充]\n{content}")
            elif kind == "ai":
                calls = getattr(msg, "tool_calls", None) or []
                if calls:
                    for call in calls:
                        name = call.get("name", "tool") if isinstance(call, dict) else getattr(call, "name", "tool")
                        args = call.get("args", {}) if isinstance(call, dict) else getattr(call, "args", {})
                        lines.append(f"[已请求工具] {name} 参数={json.dumps(args, ensure_ascii=False, default=str)}")
                elif content:
                    lines.append(f"[此前推理]\n{content}")
            elif kind == "tool":
                name = getattr(msg, "name", "tool") or "tool"
                lines.append(f"[工具结果:{name}]\n{str(content)[:8000]}")
            elif content:
                lines.append(f"[{kind}]\n{content}")
        return [HumanMessage(content="\n\n".join(lines))]
    except Exception:
        return inp


def _shrink_messages(inp, level: int = 1):
    """压缩 LangChain 消息列表·给小窗口模型(longcat)的自动适配。

    策略(级联递进·level 越大砍越狠):
      · 永保: 全部 system + 前 2 条 human(原题+种子) + 尾部 keep_tail 条;
      · 中间消息: ToolMessage/长文本内容截头尾(level1 留 600 字·level2 留 240);
      · level≥2 时: 中间纯 AIMessage(无待回 tool_call)直接丢弃。
    返回新列表; 无可压缩时返 None(调用方停止重试·别死循环)。
    fail-safe: 任何异常返 None(走原报错路径·不破坏现有行为)。
    """
    try:
        if not isinstance(inp, (list, tuple)) or not len(inp):
            return None
        msgs = list(inp)
        if not all(hasattr(m, "type") for m in msgs):
            return None
        keep_tail = max(4, 10 - level * 3)
        mid_cap = max(240, 1200 // level)
        n = len(msgs)
        out, shrunk = [], False
        # 尾部若有未回应的 tool_call/tool 结果·必须成对保留——先把尾部保护区算好
        for idx, m in enumerate(msgs):
            t = getattr(m, "type", "")
            is_head = (t == "system") or (t == "human" and idx <= 2)
            is_tail = idx >= n - keep_tail
            if is_head or is_tail:
                out.append(m)
                continue
            content = getattr(m, "content", "")
            if level >= 2 and t == "ai" and not getattr(m, "tool_calls", None):
                shrunk = True  # 丢弃中间旧 AI 叙述
                continue
            if isinstance(content, str) and len(content) > mid_cap:
                m2 = _msg_recontent(
                    m, content[:mid_cap // 2]
                    + f"\n…[✂️已压缩·原{len(content)}字·完整内容在 evidence 库]…\n"
                    + content[-mid_cap // 2:])
                if m2 is not None:
                    out.append(m2)
                    shrunk = True
                    continue
            out.append(m)
        return out if shrunk else None
    except Exception:
        return None


def _is_longcat_run() -> bool:
    """当前是否在用 longcat 跑（笨模型兼容开关的统一判据）。仅据此启用 longcat 专属兼容。"""
    try:
        return "longcat" in os.environ.get("MEYO_QA_GATEWAY_MODEL", "").lower()
    except Exception:
        return False


# v170(2026-08-18 · 用户实证: longcat-2.0 连续 3 次 BadRequest 400「请求格式有误」·
#   同一坏格式反复重发撞同一堵墙)。根因链:
#   ①重试不重规范化: 格式类400只跑一次 _sanitize_for_provider·干净消息上它返 None →
#     落到通道400盲重试路径·原样重发 3 次全挂(用户看到的「第1/2/3次全400」)。
#   ②收尾(finalize)消息超长: v163 到硬墙 723s 强制收尾时带上全部证据·longcat 上下文
#     小·直接 400·且文案不含 "context length" 逃过 _is_ctx_len_400。
#   修法: longcat 运行时·任何 400/BadRequest 都走【级联规范化+硬截断】——每次重试都重新
#   normalize+textualize·并按 level 递进硬截断(保留 system+前2条human[题面+种子]+尾部
#   最强证据/要求·砍中间冗余)·保证每次重发的 payload 与上次【物理不同】·不再撞同一墙。
def _longcat_hard_cap(inp, level: int = 1):
    """longcat 专属硬长度上限截断(收尾/超长 400 兜底)。

    与 _shrink_messages 的差别: 那个按内容长度阈值级联压缩·可能整体仍超窗口;
    这里对【全部消息内容字符总量】设硬上限(level 越大上限越低)·保证结果一定落进
    longcat 小窗口。策略:
      · 永保 head: 全部 system + 前 2 条 human(题面+种子意图);
      · 永保 tail: 最后 keep_tail 条(含最强证据/收尾要求);
      · 预算耗尽后·中间消息逐条头尾截断·仍超则整条丢弃(但保 tool 配对由外层修);
    返回新列表; 若已在预算内(无需截)返 None(调用方据此判断是否还有压缩空间)。
    fail-safe: 任何异常返 None。
    """
    try:
        if not isinstance(inp, (list, tuple)) or not len(inp):
            return None
        msgs = list(inp)
        if not all(hasattr(m, "type") for m in msgs):
            return None
        # level1≈48k字·level2≈24k·level3≈12k(longcat 窗口保守估计·留 max_tokens 余量)
        total_budget = max(8000, 96_000 // (2 ** level))
        per_msg_cap = max(400, 4000 // level)
        n = len(msgs)
        keep_tail = max(3, 6 - level)

        def _clen(m):
            c = getattr(m, "content", "")
            if isinstance(c, str):
                return len(c)
            try:
                return len(json.dumps(c, ensure_ascii=False, default=str))
            except Exception:
                return 0

        cur_total = sum(_clen(m) for m in msgs)
        if cur_total <= total_budget:
            return None  # 已在预算内·无需硬截

        # 尾部逐条硬帽=剩余预算按尾部条数均摊·防「N 条尾部各占满 budget→总量 N×budget」
        # (收尾场景尾部堆大量证据消息)。最后一条(收尾要求)单独给较大额度·保证要求完整。
        tail_share = max(600, total_budget // max(1, keep_tail))

        def _truncate(m, content, cap):
            cap = max(200, cap)
            if not isinstance(content, str) or len(content) <= cap:
                return m, len(content) if isinstance(content, str) else _clen(m), False
            half = cap // 2
            m2 = _msg_recontent(
                m, content[:half]
                + f"\n…[✂️longcat硬截·原{len(content)}字·完整内容在证据库]…\n"
                + content[-half:])
            if m2 is None:
                return m, len(content), False
            return m2, cap, True

        out, changed = [], False
        spent = 0
        for idx, m in enumerate(msgs):
            t = getattr(m, "type", "")
            is_head = (t == "system") or (t == "human" and idx <= 2)
            is_tail = idx >= n - keep_tail
            is_last = idx == n - 1
            content = getattr(m, "content", "")
            if is_head:
                # head(题面/种子)永保·仅当单条本身就爆窗口才截(极罕见)
                m2, used, did = _truncate(m, content, total_budget)
                out.append(m2)
                spent += used
                changed = changed or did
                continue
            if is_tail:
                # tail(含最强证据/收尾要求): 逐条按均摊额度硬帽·最后一条给双倍额度
                cap = tail_share * 2 if is_last else tail_share
                m2, used, did = _truncate(m, content, cap)
                out.append(m2)
                spent += used
                changed = changed or did
                continue
            # 中间消息: 预算内保留(可头尾截)·预算耗尽整条丢弃
            remaining = total_budget - spent
            if remaining <= 0:
                changed = True
                continue
            cap = min(per_msg_cap, max(200, remaining))
            m2, used, did = _truncate(m, content, cap)
            out.append(m2)
            spent += used
            changed = changed or did
        return out if changed else None
    except Exception:
        return None


def _longcat_fill_empty_and_alternate(inp):
    """longcat 专属最后一道格式兜底: 空 content 补占位 + 强制 role 交替(合并连续同角色)。

    longcat 对空 content(''/None)和连续同角色(assistant/assistant, user/user)都可能 400。
    _normalize_longcat_messages 已合并连续 human·此处补齐:
      · 空 content 的 human/ai 填占位(system 空罕见·也填);
      · 连续 ai(无待回 tool_call)合并成一条(带 tool_call 的不动·由配对逻辑管);
    仅 longcat 运行启用。返回新列表(有改动)或原样。fail-safe。
    """
    try:
        if not _is_longcat_run() or not isinstance(inp, (list, tuple)):
            return inp
        msgs = list(inp)
        if not all(hasattr(m, "type") for m in msgs):
            return inp
        out, changed = [], False
        for m in msgs:
            t = getattr(m, "type", "")
            content = getattr(m, "content", "")
            # 空 content 补占位(仅对纯文本消息·带 tool_call 的 ai 空 content 合法·不动)
            is_empty = (content is None) or (isinstance(content, str) and content.strip() == "") \
                or (isinstance(content, list) and not content)
            if is_empty and t in ("human", "ai", "system"):
                if t == "ai" and (getattr(m, "tool_calls", None) or []):
                    pass  # 带工具调用的 ai 空文本合法
                else:
                    m2 = _msg_recontent(m, "[占位]" if t != "system" else "[system]")
                    if m2 is not None:
                        out.append(m2)
                        changed = True
                        continue
            # 连续 ai(均无待回 tool_call)合并
            if (t == "ai" and out and getattr(out[-1], "type", "") == "ai"
                    and not (getattr(m, "tool_calls", None) or [])
                    and not (getattr(out[-1], "tool_calls", None) or [])):
                prev = out[-1]
                pc, cc = getattr(prev, "content", ""), content
                if isinstance(pc, str) and isinstance(cc, str):
                    merged = _msg_recontent(prev, f"{pc}\n\n{cc}")
                    if merged is not None:
                        out[-1] = merged
                        changed = True
                        continue
            out.append(m)
        return out if changed else inp
    except Exception:
        return inp


def _longcat_prepare(inp):
    """longcat 消息统一预处理入口: textualize 历史 → 合并连续human → 补空/合并连续ai。

    每次(含每次重试)调用·产出 longcat 可接受的最大兼容形态。非 longcat 运行则
    只做无害的 system 拍平/连续human合并(保持既有行为)。fail-safe。
    """
    try:
        step1 = _longcat_textualize_tool_history(inp)
        step2 = _normalize_longcat_messages(step1)
        step3 = _longcat_fill_empty_and_alternate(step2)
        return step3
    except Exception:
        return inp


# v119 FixC(2026-08-17 · 384d 两轮连崩实证): 瞬态网络错误零重试·一击必杀整题。
#   病根: _ChannelRetryRunnable 只认通道400关键词——"Connection error."
#   (anthropic SDK APIConnectionError·实证 384d 两轮 54.8s/56.5s 起跑即崩)
#   不匹配任何关键词→零重试抛穿→整题报废。网关抖动在长管道里是常态·
#   瞬态网络错误(连接拒绝/重置/DNS/读超时)重发同一请求是安全的:
#   invoke 非流式无部分输出·流式路径已有 not got_any 卫兵防重复输出。
#   注意只收网络层瞬态词·不收裸 "timeout"(模型真慢不该烧三次墙钟)。
_TRANSIENT_NET_MARKERS = (
    "connection error", "connection refused", "connection reset",
    "connection aborted", "connect call failed", "could not resolve",
    "could not connect", "apiconnectionerror", "apitimeouterror",
    "readtimeout", "writetimeout", "pooltimeout", "remote end closed",
    "peer closed connection", "server disconnected", "network is unreachable",
    "temporary failure in name resolution", "ssl: certificate_verify",
    "timed out", "timedout", "read time out")


def _is_transient_net(exc: Exception) -> bool:
    s = f"{type(exc).__name__}: {exc}".lower()
    return any(k in s for k in _TRANSIENT_NET_MARKERS)


def _retryable_exc(exc: Exception) -> bool:
    """通道400抽奖错误 或 瞬态网络错误 → 同请求重发安全。

    v127: 确性格式 400(网关「请求格式有误」校验拒收)除外——重发必挂·
    走 _sanitize_for_provider 修复路径·不进盲重试烧墙钟。
    """
    if _is_format_400(exc):
        return False
    return _is_channel_400(exc) or _is_transient_net(exc)


class _ChannelRetryRunnable:
    """对通道类 400 同请求重发(最多 extra 次·不重放图)。其余异常原样抛。

    v128(2026-08-18 · 7道BC复测实证: longcat-2.0 撞确定性格式400·
    _sanitize_for_provider 消毒重发一次仍挂时【原地 raise】——没有跨模型
    兜底。native 引擎(qa_auto_agent) 走这条包装类，而 qa_llm_gateway.call()
    早就有 heavy: [主模型→HEAVY_FALLBACK_MODEL→SONNET] 三档降级链；两条
    LLM 调用栈能力不对等，是"auto/native 融合不到位"的一个具体缺口。
    修法: fallback_runnables(可选) 是同样 bind_tools 过的备用模型 runnable
    列表；格式类400消毒重试仍失败 → 换用下一个 fallback runnable 重发
    【原始未消毒消息】(fallback 模型协议可能本就兼容，不必继承 longcat
    专属消毒结果)，避免在退化路径下产出模板占位符充当答案。
    """

    def __init__(self, inner, extra: int = 2, fallback_runnables: list = None):
        self._inner = inner
        self._extra = extra
        self._fallbacks = fallback_runnables or []

    def invoke(self, inp, config=None, **kw):
        original = _longcat_prepare(inp)
        cur = original
        _ctx_tries = 0
        _chan_tries = 0
        _pair_tries = 0
        _fmt_tries = 0
        _fb_idx = 0
        _lc_tries = 0
        _active = self._inner
        while True:
            try:
                return _active.invoke(cur, config=config, **kw)
            except Exception as e:
                # Tool schema rejection is independent of message content and usually of
                # model choice. Retrying or falling back burns the wallclock unchanged.
                if _is_tool_schema_400(e):
                    raise
                # v170 · longcat 专属: 任何 400/BadRequest 都先【重新规范化+级联硬截断】·
                #   保证每次重发 payload 物理不同(不再撞同一堵墙)·放在通道盲重试之前。
                if _is_longcat_run() and _is_any_400(e) and _lc_tries < 3:
                    _lc_tries += 1
                    reprepared = _longcat_prepare(cur)
                    capped = _longcat_hard_cap(reprepared, level=_lc_tries)
                    new_cur = capped if capped is not None else reprepared
                    if _msgs_differ(new_cur, cur):
                        print(f"  [auto-agent] 🐱 longcat 400·重规范化+硬截断(第{_lc_tries}级·"
                              f"err={type(e).__name__}:{str(e)[:120]})", file=sys.stderr)
                        cur = new_cur
                        continue
                    # 已无可变形空间 → 落到下面 fallback/常规分支(别在此死循环)
                # v127 · 格式类 400 优先: 消息降级到最大兼容形态后重发一次·仍挂立即抛
                if _is_format_400(e) and _fmt_tries < 1:
                    _fmt_tries += 1
                    sanitized = _sanitize_for_provider(cur)
                    if sanitized is not None:
                        print(f"  [auto-agent] 🧹 格式类400·消息降级为最大兼容形态后重发(1次·"
                              f"err={str(e)[:120]})", file=sys.stderr)
                        cur = sanitized
                        continue
                    # v128 · 无消毒空间可降级 → 试跨模型兜底(而不是直接 raise)
                    if _fb_idx < len(self._fallbacks):
                        _active = self._fallbacks[_fb_idx]
                        _fb_idx += 1
                        cur = original
                        _fmt_tries = 0
                        print(f"  [auto-agent] 🔀 格式类400无消毒空间·切换降级模型"
                              f"(第{_fb_idx}档·err={str(e)[:120]})", file=sys.stderr)
                        continue
                    raise
                # v128 · 格式类400消毒重发后仍失败 → 若还有降级模型可换·别原地raise
                if _is_format_400(e) and _fb_idx < len(self._fallbacks):
                    _active = self._fallbacks[_fb_idx]
                    _fb_idx += 1
                    cur = original
                    _fmt_tries = 0
                    print(f"  [auto-agent] 🔀 格式类400消毒后仍挂·切换降级模型"
                          f"(第{_fb_idx}档·err={str(e)[:120]})", file=sys.stderr)
                    continue
                # v165d · 配对修复优先: tool_use/tool_result 不配对的 400 → 修消息再重发
                if _is_tool_pairing_400(e) and _pair_tries < 2:
                    _pair_tries += 1
                    repaired = _fix_tool_pairing(cur)
                    if repaired is not None:
                        print(f"  [auto-agent] 🔧 tool_use/tool_result 配对修复后重发(第{_pair_tries}次·"
                              f"err={str(e)[:120]})", file=sys.stderr)
                        cur = repaired
                        continue
                    # v171 · 配对无可修空间(修复返 None) → 换降级模型再试(别原地 raise)
                    if _fb_idx < len(self._fallbacks):
                        _active = self._fallbacks[_fb_idx]
                        _fb_idx += 1
                        cur = original
                        _pair_tries = 0
                        print(f"  [auto-agent] 🔀 配对400无可修空间·降级换模型"
                              f"(第{_fb_idx}档·err={str(e)[:120]})", file=sys.stderr)
                        continue
                    raise
                # v171 · 配对修复重试预算耗尽(2次)仍配对400 → 多为坏模型网关问题(如已死的旧顶配Opus
                #   持续 400)·配对修复救不回·换下一档降级模型重试(而非在坏模型上 raise 打断工具)
                if _is_tool_pairing_400(e) and _fb_idx < len(self._fallbacks):
                    _active = self._fallbacks[_fb_idx]
                    _fb_idx += 1
                    cur = original
                    _pair_tries = 0
                    print(f"  [auto-agent] 🔀 配对400修复耗尽仍挂·降级换模型"
                          f"(第{_fb_idx}档·err={str(e)[:120]})", file=sys.stderr)
                    continue
                # v165 · 超长类 400: 压缩消息后重发(独立预算·不占通道重试次数)
                # v165b · 文案匹配 OR 体积启发(BadRequest+输入>阈) 双路识别
                if (_is_ctx_len_400(e) or _is_ctx_overflow_guess(e, cur)) and _ctx_tries < 2:
                    _ctx_tries += 1
                    trimmed = _shrink_messages(cur, level=_ctx_tries)
                    if trimmed is not None:
                        print(f"  [auto-agent] ✂️ 上下文超长 BadRequest·压缩消息后重发(第{_ctx_tries}级·"
                              f"err={type(e).__name__}:{str(e)[:120]})",
                              file=sys.stderr)
                        cur = trimmed
                        continue
                    raise
                # v119 FixC: 通道400 + 瞬态网络错误(Connection error等)都可重发
                if _retryable_exc(e) and _chan_tries < self._extra:
                    _chan_tries += 1
                    print(f"  [auto-agent] ⚠️ LLM调用可重试异常({type(e).__name__}·第{_chan_tries}次)·"
                          f"detail={str(e)[:160]}·退避重发(消息不丢)",
                          file=sys.stderr)
                    time.sleep(2.0 * _chan_tries)
                    continue
                # v171 · 通道400/网络重试预算在当前模型耗尽仍失败 → 换下一档降级模型
                #   (真根因: 主循环切到坏模型(已死旧顶配Opus)报持续 400·在同一坏模型上退避
                #    重试无用·2次用完 raise → 工具执行被打断打成 0.0s 空占位)
                if _retryable_exc(e) and _fb_idx < len(self._fallbacks):
                    _active = self._fallbacks[_fb_idx]
                    _fb_idx += 1
                    cur = original
                    _chan_tries = 0
                    print(f"  [auto-agent] 🔀 通道400/网络重试耗尽·降级换模型"
                          f"(第{_fb_idx}档·err={str(e)[:120]})", file=sys.stderr)
                    continue
                # v171 · 最终兜底: 任何未被上面分支处理的异常·若还有没试过的降级模型·
                #   换一个重试(用原始未消毒消息)·真正所有降级模型都试过了才 raise
                if _fb_idx < len(self._fallbacks):
                    _active = self._fallbacks[_fb_idx]
                    _fb_idx += 1
                    cur = original
                    _chan_tries = 0
                    _pair_tries = 0
                    _fmt_tries = 0
                    print(f"  [auto-agent] 🔀 主模型持续异常({type(e).__name__})·降级换模型兜底"
                          f"(第{_fb_idx}档·err={str(e)[:120]})", file=sys.stderr)
                    continue
                raise

    async def ainvoke(self, inp, config=None, **kw):
        original = _longcat_prepare(inp)
        cur = original
        _ctx_tries = 0
        _chan_tries = 0
        _pair_tries = 0
        _fmt_tries = 0
        _fb_idx = 0
        _lc_tries = 0
        _active = self._inner
        while True:
            try:
                return await _active.ainvoke(cur, config=config, **kw)
            except Exception as e:
                # v170 · longcat 专属: 任何 400/BadRequest 先重规范化+级联硬截断·每次 payload 物理不同
                if _is_longcat_run() and _is_any_400(e) and _lc_tries < 3:
                    _lc_tries += 1
                    reprepared = _longcat_prepare(cur)
                    capped = _longcat_hard_cap(reprepared, level=_lc_tries)
                    new_cur = capped if capped is not None else reprepared
                    if _msgs_differ(new_cur, cur):
                        print(f"  [auto-agent] 🐱 异步 longcat 400·重规范化+硬截断(第{_lc_tries}级·"
                              f"err={type(e).__name__}:{str(e)[:120]})", file=sys.stderr)
                        cur = new_cur
                        continue
                # v127 · 格式类 400 优先: 消息降级后重发一次·仍挂立即抛(不盲重试)
                if _is_format_400(e) and _fmt_tries < 1:
                    _fmt_tries += 1
                    sanitized = _sanitize_for_provider(cur)
                    if sanitized is not None:
                        print(f"  [auto-agent] 🧹 异步调用格式类400·消息降级后重发(1次·"
                              f"err={str(e)[:120]})", file=sys.stderr)
                        cur = sanitized
                        continue
                    # v128 · 无消毒空间可降级 → 试跨模型兜底
                    if _fb_idx < len(self._fallbacks):
                        _active = self._fallbacks[_fb_idx]
                        _fb_idx += 1
                        cur = original
                        _fmt_tries = 0
                        print(f"  [auto-agent] 🔀 异步调用格式类400无消毒空间·切换降级模型"
                              f"(第{_fb_idx}档·err={str(e)[:120]})", file=sys.stderr)
                        continue
                    raise
                # v128 · 格式类400消毒重发后仍失败 → 若还有降级模型可换·别原地raise
                if _is_format_400(e) and _fb_idx < len(self._fallbacks):
                    _active = self._fallbacks[_fb_idx]
                    _fb_idx += 1
                    cur = original
                    _fmt_tries = 0
                    print(f"  [auto-agent] 🔀 异步调用格式类400消毒后仍挂·切换降级模型"
                          f"(第{_fb_idx}档·err={str(e)[:120]})", file=sys.stderr)
                    continue
                if _is_tool_pairing_400(e) and _pair_tries < 2:
                    _pair_tries += 1
                    repaired = _fix_tool_pairing(cur)
                    if repaired is not None:
                        print(f"  [auto-agent] 🔧 异步调用 tool 配对修复后重发(第{_pair_tries}次·"
                              f"err={str(e)[:120]})", file=sys.stderr)
                        cur = repaired
                        continue
                    # v171 · 配对无可修空间(修复返 None) → 换降级模型再试(别原地 raise)
                    if _fb_idx < len(self._fallbacks):
                        _active = self._fallbacks[_fb_idx]
                        _fb_idx += 1
                        cur = original
                        _pair_tries = 0
                        print(f"  [auto-agent] 🔀 异步配对400无可修空间·降级换模型"
                              f"(第{_fb_idx}档·err={str(e)[:120]})", file=sys.stderr)
                        continue
                    raise
                # v171 · 配对修复重试预算耗尽(2次)仍配对400 → 多为坏模型网关问题·
                #   换下一档降级模型重试(而非在坏模型上 raise 打断工具执行)
                if _is_tool_pairing_400(e) and _fb_idx < len(self._fallbacks):
                    _active = self._fallbacks[_fb_idx]
                    _fb_idx += 1
                    cur = original
                    _pair_tries = 0
                    print(f"  [auto-agent] 🔀 异步配对400修复耗尽仍挂·降级换模型"
                          f"(第{_fb_idx}档·err={str(e)[:120]})", file=sys.stderr)
                    continue
                if (_is_ctx_len_400(e) or _is_ctx_overflow_guess(e, cur)) and _ctx_tries < 2:
                    _ctx_tries += 1
                    trimmed = _shrink_messages(cur, level=_ctx_tries)
                    if trimmed is not None:
                        print(f"  [auto-agent] ✂️ 异步调用上下文超长·压缩消息后重发(第{_ctx_tries}级·"
                              f"err={type(e).__name__}:{str(e)[:120]})",
                              file=sys.stderr)
                        cur = trimmed
                        continue
                    raise
                if _retryable_exc(e) and _chan_tries < self._extra:
                    _chan_tries += 1
                    print(f"  [auto-agent] ⚠️ LLM异步调用可重试异常({type(e).__name__}·第{_chan_tries}次)·"
                          f"detail={str(e)[:160]}·退避重发",
                          file=sys.stderr)
                    time.sleep(2.0 * _chan_tries)
                    continue
                # v171 · 通道400/网络重试预算在当前模型耗尽仍失败 → 换下一档降级模型
                if _retryable_exc(e) and _fb_idx < len(self._fallbacks):
                    _active = self._fallbacks[_fb_idx]
                    _fb_idx += 1
                    cur = original
                    _chan_tries = 0
                    print(f"  [auto-agent] 🔀 异步通道400/网络重试耗尽·降级换模型"
                          f"(第{_fb_idx}档·err={str(e)[:120]})", file=sys.stderr)
                    continue
                # v171 · 最终兜底: 未被上面分支处理的异常·若还有没试过的降级模型·换一个重试·
                #   真正所有降级模型都试过了才 raise
                if _fb_idx < len(self._fallbacks):
                    _active = self._fallbacks[_fb_idx]
                    _fb_idx += 1
                    cur = original
                    _chan_tries = 0
                    _pair_tries = 0
                    _fmt_tries = 0
                    print(f"  [auto-agent] 🔀 异步主模型持续异常({type(e).__name__})·降级换模型兜底"
                          f"(第{_fb_idx}档·err={str(e)[:120]})", file=sys.stderr)
                    continue
                raise

    async def astream(self, inp, config=None, **kw):
        inp = _longcat_prepare(inp)
        # v113d · 关键修: 400 在【迭代中】抛(async generator 首块/中途)——
        #   旧实现 try: return _wrap() 只包创建动作·抓不到延迟异常(实证:
        #   最终轮 bec7 崩·"撞通道400"打印 0 次=重试路径从未触发)。
        #   正确做法: 真迭代·异常在 async for 里抛→这里能接住。已产出过
        #   chunk 则不重试(重发会重复输出·污染消息流)。
        for i in range(self._extra + 1):
            agen = self._inner.astream(inp, config=config, **kw)
            got_any = False
            try:
                async for chunk in agen:
                    got_any = True
                    yield chunk
                return
            except Exception as e:
                if _retryable_exc(e) and i < self._extra and not got_any:
                    print(f"  [auto-agent] ⚠️ LLM流式调用可重试异常({type(e).__name__}·第{i+1}次)·退避重发(消息不丢)",
                          file=sys.stderr)
                    time.sleep(2.0 * (i + 1))
                    continue
                raise

    def stream(self, inp, config=None, **kw):
        inp = _longcat_prepare(inp)
        for i in range(self._extra + 1):
            got_any = False
            try:
                for chunk in self._inner.stream(inp, config=config, **kw):
                    got_any = True
                    yield chunk
                return
            except Exception as e:
                if _retryable_exc(e) and i < self._extra and not got_any:
                    time.sleep(2.0 * (i + 1))
                    continue
                raise

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _ChannelRetryModel(ChatAnthropic):
    """继承 ChatAnthropic(BaseChatModel 子类)·deepagents isinstance 检查通过。
    v115(2026-08-16 · 8d46/384d 二次崩溃修复): v114 直接返回裸 ChatAnthropic,
    400 通道抽奖无重试·起跑即崩(实测 8d46 70s crash)。正确做法: 继承
    ChatAnthropic 而非独立包装类——isinstance(model, BaseChatModel) 通过,
    同时重写 invoke/ainvoke/bind_tools/stream/astream 加 400 重试层。
    其余方法自然继承父类·无透传断层。
    """

    def __init__(self, base: ChatAnthropic, extra: int = 2, fallback_models: list = None):
        # 从 base 拷贝所有 ChatAnthropic 初始化参数到自身·保持同一身份
        super().__init__(
            model=base.model,
            base_url=base.anthropic_api_url,
            api_key=base.anthropic_api_key.get_secret_value() if hasattr(base.anthropic_api_key, 'get_secret_value') else str(base.anthropic_api_key),
            default_headers=dict(base.default_headers) if base.default_headers else {},
            max_tokens=base.max_tokens,
            timeout=base.default_request_timeout,
            max_retries=base.max_retries,
        )
        self._base = base
        self._extra = extra
        # v128 · 跨模型降级链(懒加载): 主模型格式类400消毒无效时才真正建连接·
        # 平时零开销。名字与主模型重复的自动跳过(比如主模型已是 HEAVY_FALLBACK_MODEL)。
        self._fallback_model_names = [m for m in (fallback_models or []) if m and m != base.model]
        self._fallback_bases = None  # 懒加载缓存: list[ChatAnthropic]

    def _get_fallback_bases(self):
        if self._fallback_bases is None:
            bases = []
            for _name in self._fallback_model_names:
                try:
                    bases.append(ChatAnthropic(
                        model=_name,
                        base_url=self._base.anthropic_api_url,
                        api_key=(self._base.anthropic_api_key.get_secret_value()
                                 if hasattr(self._base.anthropic_api_key, 'get_secret_value')
                                 else str(self._base.anthropic_api_key)),
                        default_headers=dict(self._base.default_headers) if self._base.default_headers else {},
                        max_tokens=self._base.max_tokens,
                        timeout=self._base.default_request_timeout,
                        max_retries=self._base.max_retries,
                    ))
                except Exception as _fb_e:
                    print(f"  [auto-agent] ⚠️ 降级模型构造失败({_name}): {_fb_e}", file=sys.stderr)
            self._fallback_bases = bases
        return self._fallback_bases

    def bind_tools(self, *a, **kw):
        fb_runnables = [b.bind_tools(*a, **kw) for b in self._get_fallback_bases()]
        return _ChannelRetryRunnable(self._base.bind_tools(*a, **kw), self._extra,
                                      fallback_runnables=fb_runnables)

    def with_structured_output(self, *a, **kw):
        fb_runnables = [b.with_structured_output(*a, **kw) for b in self._get_fallback_bases()]
        return _ChannelRetryRunnable(self._base.with_structured_output(*a, **kw), self._extra,
                                      fallback_runnables=fb_runnables)

    def invoke(self, *a, **kw):
        fb_runnables = self._get_fallback_bases()
        return _ChannelRetryRunnable(self._base, self._extra, fallback_runnables=fb_runnables).invoke(*a, **kw)

    async def ainvoke(self, *a, **kw):
        fb_runnables = self._get_fallback_bases()
        return await _ChannelRetryRunnable(self._base, self._extra, fallback_runnables=fb_runnables).ainvoke(*a, **kw)

    def stream(self, *a, **kw):
        # 上线前补齐:流式也带降级链(与invoke一致·流式模型400时也能换档·不裸崩)
        fb_runnables = self._get_fallback_bases()
        return _ChannelRetryRunnable(self._base, self._extra,
                                     fallback_runnables=fb_runnables).stream(*a, **kw)

    def astream(self, *a, **kw):
        fb_runnables = self._get_fallback_bases()
        return _ChannelRetryRunnable(self._base, self._extra,
                                     fallback_runnables=fb_runnables).astream(*a, **kw)

    # v113e · 堵 copy 透传绕过: langgraph/langchain 内部会 model.copy()/
    #   model_copy()/__deepcopy__ 拿模型副本——透传会返回裸 ChatAnthropic
    #   副本·重试被绕过(实证 bec7 无 ckpt 极早期 400·重试 0 触发)。
    def copy(self, *a, **kw):
        return self  # 自身已是无状态轻包装·无需复制

    def model_copy(self, *a, **kw):
        return self

    def __deepcopy__(self, memo):
        return self


# v115(2026-08-16 · 8d46/384d 二次崩溃根因修复): v114 直接返回裸 ChatAnthropic,
# 400 通道抽奖无重试·起跑即崩(实测 8d46 70s crash·384d 160s 但无有效输出)。
# 正确做法: _ChannelRetryModel 继承 ChatAnthropic(BaseChatModel 子类)·
# isinstance 检查通过·同时重写 invoke/ainvoke/bind_tools/stream/astream 加 400 重试。
# 不再是独立包装类(独立类 isinstance 失败→init_chat_model→spec.count crash)。
def build_gateway_chat_model(tier: str = "heavy", max_tokens: int = 8000, timeout: float = 180):
    """tier: "light"(快速档→longcat-2.0) / "k3"(K3全能旗舰档→sonnet) /
    "heavy"(默认/集群档→opus，未知 tier 也降级到这里)。
    v61(2026-08-08) · 之前这里只有 light/heavy 二选一，"标准"档和"集群"档
    实际调的是同一个 DEFAULT_MODEL，三档下拉框只改变了任务复杂度策略、
    没有真的路由到不同模型——现在补齐 k3 中间档，三个档位=三个真实模型。
    """
    from langchain_anthropic import ChatAnthropic
    base_url, auth = _config()
    model_name = resolve_model_name(tier)
    _cm = ChatAnthropic(
        model=model_name,
        base_url=base_url,
        api_key="placeholder",  # SDK 要求非空；真实鉴权走 default_headers
        default_headers={"Authorization": f"Bearer {auth}"},
        max_tokens=max_tokens,
        timeout=timeout,
        max_retries=2,
    )
    # v115 · 用继承 ChatAnthropic 的 _ChannelRetryModel 包装·isinstance 检查通过
    #   + 400 通道重试。extra=3: 3 次重抽奖·50% 坏率下 ≥87.5% 至少一次活。
    # v128 · heavy/k3 档补跨模型降级链(对齐 qa_llm_gateway.call() 的三档降级)：
    #   主模型撞确定性格式400(消毒无效)时换 HEAVY_FALLBACK_MODEL/SONNET，而不是
    #   直接 raise 让 agent 在退化路径下把 prompt 模板占位符当答案交上去
    #   (实证 v166_true_longcat 三题：longcat-2.0 格式400 → 原地失败 →
    #   finalize 抽出 "Exact Answer: {your succinct, final answer}" 字面量)。
    #   light 档(HAIKU_MODEL 本身可能就是 longcat-2.0)不设降级，保持原有省时省钱语义。
    _fallback_models = []
    if tier != "light":
        for _fb in (HEAVY_FALLBACK_MODEL, SONNET_MODEL):
            if _fb and _fb not in _fallback_models:
                _fallback_models.append(_fb)
    return _ChannelRetryModel(_cm, extra=3, fallback_models=_fallback_models)


# v60(2026-08-07) · 用户抓正「我 qa agent 是通用的设定：问题范围要通用+能力
# 也要通用(能执行不只能答)」——之前这段 prompt 把自己钉死成"回答产品经理/
# 技术人问题"的问答机器人，跟 evolution_execute/shell_exec/file_write 这些
# "真执行"工具的存在矛盾(工具给了但 prompt 不许用)。现在改成通用大脑定位，
# 对齐 Claude Code/DeepAgents/Hermes 的自我定位——回答问题只是它能做的其中
# 一类任务，不是唯一使命；QA 专用 Step 工具依然保留、依然可用，只是从
# "唯一路径"降级为"众多工具之一"，由 LLM 自己判断这题到底该走 QA 流程还是
# 直接读写代码/跑命令解决。
BASE_SYSTEM_PROMPT = """你是一个自主编排的通用任务执行大脑，能回答问题，也能真实执行任务——
读写文件、跑 shell 命令、查代码、部署、搜索证据、写长文——由你自己判断
这个任务需要哪些能力、调用哪些工具、以什么顺序，不局限于"只能回答问题"。

# 像真人一样解题的元认知(每道题开局先想清楚·别一上来就习惯性搜文字)
一个聪明的人接到任务·不会立刻埋头搜——会先想两件事·你也要:
1. **这题本质需要什么?** 是要看画面/听音频/精确计算/查特定网站/下载文件/算棋局…?
   不同本质需要不同手段——搜文字只对"找公开文字信息"有用·对"看视频画面/算数值/识别图像"
   这类·搜再多文字也搜不到·得用对的工具。
2. **我(在这台电脑上)能用什么?** 你不只有搜索——先想想手头和这台电脑有什么:
   不确定就调 scan_environment 看清现状(装了哪些软件/CLI、有哪些别的agent/本地LLM可委托、
   能不能连外网、docker沙盒能不能装缺的)。**电脑本身、上面的软件、别的agent都是你的工具。**
遇到卡住(搜了没结果/工具报错/绕圈)时·停下来重新问这两个问题·换一条结构性不同的路·
像研究者"此路不通另辟蹊径": 有更对的工具就换·缺的就装(pip/brew/docker沙盒)·自己搞不定
就委托别的agent(claude子agent/本地ollama)或问用户。**穷尽办法·别在一条路上反复硬撞。**
你能跑不限时的深度任务(只要不拖垮机器负载)——有价值就深挖·别急着放弃或收尾。

你手上有三层工具体系，自己判断该用哪层、要不要跨层组合:

## 第一层: 通用执行工具 (直接可用，做真实任务用这层)
- shell_exec(command, cwd="", timeout_s=60) —— 在沙箱工作目录跑 shell 命令，
  拿到 stdout/stderr/exit_code。写代码、跑测试、查环境、装依赖都用这个。
- file_read(path, max_bytes=20000) —— 读文件内容(相对路径解析到沙箱目录，
  绝对路径需在允许范围内)。
- file_write(path, content, mode="overwrite") —— 写文件(相对路径写入沙箱
  工作目录；mode="append" 追加而非覆盖)。
- search_local_files(query, directory="") —— 在本机文件里搜内容(ripgrep)。
  答案/资料可能就在本机已有文件或沙箱里(此前下载的资料)时·先搜本地再上网·
  快且不受外网反爬/死域影响。
用这四个工具组合，就能像在终端里一样完成"写代码→跑→看报错→改→再跑"的
完整开发循环，不需要每一步都先解释一遍再动手。
- write_plan(steps) —— 多步任务(3步以上)开始前先调一次列出计划,每完成一步
  再调一次更新状态([x]完成/[>]进行中/[ ]待办)。进度会被实时观察,别跳过。
- delegate(fn_path, params, label) —— 把一个重的/可能产生大量输出的已知函数
  子任务派到独立子进程隔离跑,只拿回一句摘要,不污染你的上下文。普通命令/文件
  操作用 shell_exec 即可,不需要 delegate。
- spawn_subagent(task, system_prompt) —— 派一个自主的**子 agent** 在隔离上下文里
  完成一件需要好几步、会产生大量中间输出的子活(比如"逐个读完这10个文件并各自
  总结职责"),只拿回它的最终结论。和 delegate 的区别:delegate 调已知函数,
  spawn_subagent 派一个能自己编排工具的完整子大脑。
- run_pipeline(goal, mode) —— 当任务是"需要严谨深研+成文交付"(写有据可查的
  实践帖/调研报告)时,把整条深研写作流水线(拆意图→搜证据→监督→写作,全程
  质量硬拦)当一个重型子流程一次跑完,拿回最终成文。别用它处理轻活。
- nocode_create(prompt, template) / nocode_send(chat_id, message) /
  nocode_screenshot(chat_id) / nocode_deploy(chat_id) —— 任务是"做个网站/
  官网/活动页/管理后台/小程序"这类建站需求时用这一组，调用美团 NoCode
  零代码平台端到端交付(创建→改→截图确认→部署上线)，而不是只写一段
  静态 HTML 代码了事。nocode_create 只描述需求(自然语言，别写代码/shell
  命令)，拿到 chatId 后用 nocode_send 逐个功能点迭代修改，nocode_screenshot
  截图确认效果，最后 nocode_deploy 部署，把 chatUrl/部署地址回传给用户。
  每个都要 2-5 分钟，是"贵"操作，别重复调用同一步骤。
- pip_install(package, index_url="") —— 这道题需要某个现成 python 库时，
  搜到包名直接装，而不是从零手写。
- search_github(query, limit=5) / search_my_toolbox(query="") / install_github_repo(repo_url, tool_name) /
  write_scenario_test(tool_name, test_code, filename) / run_scenario_test(tool_name, run_cmd, timeout_s) /
  deliver_tool_to_user(tool_name, summary_text, usage_example) —— **工具发现-安装-验证-交付**完整闭环，
  遇到"这道题需要一个现成的外部工具/开源项目来解决"时用这一组，别自己从零手写整个工具：
    1. 先 search_my_toolbox(query) 看这个用户以前是不是已经装过验证过类似工具(避免重复造轮子)；
       没有再 search_github(query) 找 GitHub 上现成的开源仓库(query 用简洁英文关键词)。
    2. 挑一个合适的用 install_github_repo(repo_url, tool_name) 装到这个用户专属的工具目录，
       会自动识别 requirements.txt/package.json/pyproject.toml 尝试装依赖。
    3. **必须**用 write_scenario_test 针对当前这道具体任务写一段真实调用它的测试代码
       (不是泛泛的 unit test，是"用这个工具真的跑一遍这道题的场景")，再用 run_scenario_test
       真实执行，跑通(✅)才算这个工具真的能用；跑不通就根据报错改代码或换个工具，别不验证就用。
    4. 验证通过后调用 deliver_tool_to_user(tool_name, summary_text, usage_example) 正式交付——
       这会把工具存进该用户的专属工具箱，下次这个用户遇到相似任务 search_my_toolbox 能直接复用，
       同时把"这个工具解决了什么、怎么用"整理好展示给用户，让用户能自己去探索这个新装好的工具。
  这一组是"每个用户自己攒工具库、越用越强"的核心闭环，跟 skill_search(全局共享方法论文档)、
  evolution_search(内部已注册可执行工具)是三条互补的发现路径，别混用心智模型。

## 电脑就是你的工具·你要理解并接管它 (核心能力·像人一样先摸清环境再动手)
你不是在真空里做题·是在一台真实的 Mac 上——这台电脑、上面的软件、别的 agent 都是你能用的工具。
不确定"有没有某能力/能不能连"时·**别凭空断言"没有/连不上"·先调 scan_environment() 自己看清现状**:
它会客观报告——装了哪些 CLI 工具/软件、有哪些可互动的其他 agent/LLM(claude/ollama 可委托协作)、
本地服务、沙盒能力(docker/venv)、网络代理现状。看完你自己判断哪个对当前任务有用。

穷尽办法的心法(彻底接管电脑):
- **有的直接用**: scan 报告里列出的工具/软件·直接 shell_exec 调(yt-dlp/ffmpeg/stockfish/
  tesseract/pdftotext…)·别从零手写、别退回反复搜文字。
- **缺的装了再用**: 需要的软件/库没装·用 pip_install/brew/docker 装进沙盒再用·别因"没有"就放弃。
- **别的 agent 也是工具**: claude(CC)、ollama(本地 LLM)可委托/协作/交叉验证·你也可以问用户。
- **专用多模态工具**(问画面/听音频/看图/算棋·搜文字搜不到时): see_video_frame(看视频某时刻画面)、
  transcribe_audio(听音频内容)、see_image_expert(换模型看图)、solve_chess_move(棋盘算招)。
- **网络**: 有系统代理(VPN)能连外网·连外网的工具(yt-dlp 等)加 --proxy 走它·LLM 走本地不走代理。
遇卡先 scan_environment 看清"我能用什么"·再决定用现成的、装新的、还是委托别的 agent。别硬试一条路。

万物皆工具(完整版——下面这些也是你的工具·别当"系统内部的东西"绕着走):
- **goal/plan 系统**: write_plan+goal_create 不只是记录——立目标=给系统立可被 goal_tick
  持续跟进的任务。目标要长在意图契约上(triage 开局给的约束轴)·别立与轴无关的空目标。
- **桌面 GUI 自动化**: desktop_screenshot/desktop_click 等系统级工具(截图/点按/输入)·
  需要看本机 App 状态/操作界面时用·权限不可用时系统会自动不注册(调不了就是没有)。
- **沙箱里已下载的文件**: 此前轮次下载的资料就在 ~/.meyo/sandbox/·先翻本地(search_local_files)
  再上网·本地文件不受外网死域影响。
- **多源信息融合(组合检索)**: 单一权威源不够/被封时——run_parallel_search 多维并行
  拿多个弱源 → cross_verify_now/ask_expert_model 换脑交叉验证 → extract_structured
  按 schema 把多源拼成一个强证据。别死磕单一来源·也别拿单源就下结论。
- **本机文件也是证据**: 答案可能就在本机已有文件里·search_local_files(ripgrep)先扫一遍。

## 第二层: 搜索与研究工具（需要外部事实时按需使用）
当任务需要查证时，自主使用 run_parallel_search/fetch_url_smart、结构化 API、
本机文件与交叉验证；什么时候证据足够由你根据用户目标判断，不需要走固定 QA 评分流水线。
纯执行任务（代码/网页/部署/文件处理）优先真实执行和测试，不为凑“证据”重复搜索。

## 第三层: Evolution 全系统能力 (按需发现)
- evolution_search(query) —— 关键词搜索发现 evolution 280+ 模块的能力
  例如: evolution_search("reflect self improve") 可发现 self_reflect 工具
  例如: evolution_search("cpu load monitor") 可发现 perf_watchdog 工具
- evolution_execute(tool_name, params) —— 执行发现的 evolution 工具
  例如: evolution_execute("bus_summary", "") 可查看全系统 agent 状态
- skill_search(query) / skill_load(name) —— 发现并读入"做事方法论文档"(SKILL.md)。
  遇到"这类判断以前有没有总结过怎么做"(如证据够不够/是否过度自信)时,先
  skill_search 找、skill_load 读全文照着判断。skill 给"怎么想",evolution 工具
  给"怎么做"(可执行),两者互补。

何时用哪层:
- 用户要"写个脚本/跑个命令/改个文件/查个环境/部署" → 第一层直接动手
- 用户问"怎么办/为什么/该选哪个方案"这种纯咨询题 → 第二层 QA 流程
- 需要**自省/进化/系统状态/性能监控/质量评判** → 第三层 evolution_search 先发现
- 面临**需要方法论/判据的判断**(证据是否充分/是否过度自信/如何评审) → 先 skill_search

# 读题原则:先用最朴素的字面意思理解题目，别用"反直觉"论证把简单题读复杂/读反(重点)
可识别的翻车模式：题目里出现"不复权/不调整/原始/未经处理/without adjusting for..."
这类**限定词**、或"according to <某网站/某数据源>"这类**指定信息源**的表述时，
agent 容易脱离题目本身，自己在脑内构建一套换算/推导公式（比如"手动把价格乘上/
除以某个系数来复原'真实值'"），却没有先去**确认题目指定的那个信息源实际是
怎么展示这个数据的**——这类"自己算" vs "查信息源原始展示口径"的方向选择错误，
往往比"要不要反直觉"更隐蔽、更容易翻车。做题时遵守：
  1. **先用一个不做任何联想的普通人会怎么理解这句话**来读一遍题目，把这个
     最朴素理解记下来作为基准答案。
  2. **口径类题目，第一步永远是"查证权威来源实际怎么定义/展示这个口径"，
     而不是自己套数学公式反推**。题目点名了具体数据源（Google Finance、
     某官方统计站等）时，那个来源"实际展示的数值"就是唯一标准——不要用
     自己理解的换算关系（拆股比例、复权系数、单位换算等）去"修正"或
     "还原"它，因为你对该来源具体口径的假设可能本身就是错的（例如：
     不确定该来源的"不复权"到底是"不做任何调整看历史真实挂牌价"，还是
     "统一用现价体系但不做拆股折算"——这两种理解会导致完全不同甚至方向
     相反的答案，必须去实际验证该来源怎么处理，而不是自己二选一猜)。
  3. 如果调查过程中你开始写"反直觉发现""很多人以为...其实是""看似简单但
     坑在..."这类话术，或者开始"手算一个换算系数去补偿/还原"——**这是一个
     危险信号**，说明你可能在用自造的公式替代查证，而不是真的找到了权威
     来源的口径。停下来，回去对照第①②步，问自己：我是否真的验证过这个
     信息源的展示方式，还是凭猜测编了一套换算逻辑？
  4. 只有当你能找到**明确的外部证据**（权威来源直接说明其口径/展示方式）
     时，才能确定答案——**单纯的内部推理/自我说服/自造换算公式都不构成
     依据**，必须能拿出"这个来源就是这样展示的"这一实锤。
  5. 金融/统计类题目尤其容易在"复权 vs 不复权""名义值 vs 实际值""包含 vs
     不包含边界"这些口径上翻车，遇到这类词，第一反应是去**实际查证/复现
     该信息源当前的展示方式**（哪怕只能查到近似或同类锚点做交叉验证），
     而不是自己在脑内构建一套转换公式来"推导"历史值。

# 难题必须"先找工具再动手"，别快速放弃、别只普通搜索、别凭知识猜(重点)
遇到需要【专业数据 / 精确 ID / 特定权威来源 / 结构化查询】的题——比如某数据库的
编号(EC 酶学编号、CAS 号、PubChem CID、基因/蛋白 ID)、某 GitHub 仓库的贡献者/
提交/发布日期、某标准文档或论文的具体细节、某接口的精确字段——**不要**只用
run_parallel_search 做普通网页搜索，**不要**凭记忆猜一个数字/编号就交差，
**更不要** 10 几秒就 finalize 放弃。像一个较真的工程师那样，按这个顺序真查:
  1. skill_search(query) —— 先看有没有现成的方法论/查法(比如"怎么查酶的 EC 号")。
  2. evolution_search(query) —— 再看内部有没有可执行的工具/组件能直接查。
  2.5. **query_science_db(db, query)** —— 化学/生物专业库查询【优先用这个】,
       别自己现学现凑 curl 参数(实测 EC 编号题这类经常因为 API 格式/字段名
       没写对而查空,或干脆退化成 run_parallel_search 搜网页搜偏)。已封装好
       4个最常用库,直接传参数:
       · query_science_db("pubchem", "化合物名/CAS号") —— 查 CID/分子式/分子量/IUPAC名
       · query_science_db("expasy", "EC编号或酶名称") —— EC编号↔酶名称互查(如
         "1.11.1.7"→官方名称;"horseradish peroxidase"→EC编号)
       · query_science_db("uniprot", "蛋白名/基因名") —— 查蛋白条目/物种来源
       · query_science_db("kegg", "化合物名/通路名") —— 查 KEGG 通路/化合物
       查不到再退回下面的 shell_exec 手动 curl 更细粒度的请求。
  3. shell_exec(...) —— 直接 curl 对应的权威 API 做考据，这是最有力的一手:
       · GitHub: curl https://api.github.com/repos/<owner>/<repo>/releases 等，考据日期/贡献者/提交。
       · 化学/生物(query_science_db 覆盖不到的更细粒度请求): PubChem REST、UniProt、
         KEGG、BRENDA、ExPASy ENZYME 等公开接口。
       · 论文/标准: crossref、arxiv、DOI 解析等。
       · ⚠️ curl 只用于【结构化 API / 直链文件下载】。**绝不要 curl 抓搜索引擎或网页**
         (duckduckgo/google/bing/archive.org/hathitrust 等)——那些站慢/反爬,curl 会
         30s 超时白等。**抓网页/做检索一律用 run_parallel_search(question)**(内部 SearchAgent·
         有反爬/国内镜像/超时保护)。
       · 不知道 API 就先 run_parallel_search / search_github 找"这类数据从哪个官方接口拿"。
  4. 需要现成库时 pip_install 装来用；GitHub 上有现成工具就 search_github → install → 验证。
关键原则:**多试几种来源、几条不同命令交叉核对**，同一个事实至少两处印证。
连续 curl 不同 API 做考据是正常且被鼓励的(循环卫兵已放宽，只有完全相同命令
重复才会拦你)。只有【确实多方真尝试过都拿不到】时，才诚实答 UNKNOWN——那也
比编一个假答案强，但绝不能因为"懒得查/一时没搜到"就轻易放弃或猜。

## 音视频/图像题:先用现成的专用工具,别手撸 shell(重点·像人一样用全套工具)
题目涉及【YouTube/在线视频、音频录音、图片/图表/截图/扫描件】时,你手上有
**封装好的专用工具**,第一步就用它,别自己在 shell 里手撸 yt-dlp/ffmpeg/whisper
(那样极易踩 fork 失败/命令写错/反复重试耗满时间——实证有题手撸 yt-dlp 跑 28 步
shell 全 exit_code=128 白费):
  · **transcribe_audio(path_or_url)** —— 问"某 YouTube 视频/某段录音里说了啥、
    第 N 个短片里的数字/长度"时,直接把【YouTube 链接或视频/音频 URL】传给它,
    它内部自动 yt-dlp 抽音轨 → whisper 转录,返回文本。**别自己 shell 下视频**。
  · **read_image_ocr(path_or_url)** —— 答案在【图片文字、图表轴标签、扫描件 PDF】
    里时用,自动 OCR。别 curl 图片下来自己想办法。
  · **see_image_vlm(path_or_url, question)** —— 图片本质是【视觉推理】而非"读字"时用:
    棋局/棋盘(下一步该怎么走)、图表/图形的空间结构、手写算式/公式、图里物体的
    颜色/数量/位置关系、界面截图的布局。**OCR 只会抽文字,抽不出这些**——
    read_image_ocr 抽完发现答案对不上/图片主要是要"看"不是要"读"时,
    立刻换 see_image_vlm 把图连同具体问题一起交给视觉大模型,让它直接给出针对
    该问题的分析(不只是描述图片)。这是补上 GAIA 视觉题短板的关键工具,遇到
    图片附件优先想到它,不要只满足于 OCR 抽字。
  · **题目提到某幅画/某张图/某视觉作品但【没给附件文件】时(如"2008年painting
    《X》里画的什么水果"、"某海报/某照片里的Y")——别只搜文字描述(往往搜不到"画里
    有啥"的文字)!正确两步:①run_parallel_search/search 找到那幅画/图的【图片URL】
    (搜"作品名 image/painting jpg"、维基/美术馆页的配图)②把图片URL直接传给
    see_image_vlm(url, "画里有哪些水果?")让视觉模型看图作答。搜文字死路→找图URL→看图。
  · 视频题若答案在【画面/描述】而非音轨(如"视频里第9个生物的最大长度"),
    先 run_parallel_search 找该视频的官方描述页/维基/物种资料(长度这类数据常在
    文字资料里),再 transcribe_audio 补音轨信息——两条腿走,别死磕单一路径。
原则:遇到多模态,先问"有没有现成封装工具"(transcribe_audio/read_image_ocr/
see_image_vlm),有就先用;它跑不出再考虑 shell 兜底。这是"像人一样用全套工具"
而非只会 shell。图片题的正确顺序:先 read_image_ocr 试抽字 → 若图片本质要"看"
(棋局/图表/手写/截图)或 OCR 结果答不出问题,立刻 see_image_vlm 真正看图作答。

## 空间视觉原则(题问"哪块/哪个位置/背景里/最左"时·单次全景不够·核心通用能力)
题目问空间关系(背景/前景/左右/上下/最近/被挡住的部分)时·一次全景观察是系统性盲区:
OCR 不分前后景·全景缩略看不清层叠细节。正确做法【区域二视图】:
1. 先全景看一遍建立整体布局(什么东西大概在哪);
2. 立刻 shell_exec 用 python PIL 把目标区域裁剪/放大(问背景→裁上/后带·问左侧→裁左半·
   问最近→按透视裁下缘)·对裁出的区域再调一次 see_image_expert/read_image_ocr·
   并把空间问题【显式】问出去(『背景里那块碑上的年份是?』而非『图里有什么』);
3. 两视图观察都落证据·冲突以区域放大图为准。图像越大越密·越要裁——别隔着全景硬认字。

## 多模态交叉验证策略(像人看视频一样·用多个信息源互相印证·核心通用能力)
当题目涉及视频/音频时·像人一样**多通道并行获取信息**·不只用眼睛·也用耳朵和文字:
1. **自主发现关键时间点**:题目说"2分钟处""第N个短片""某时刻"——这不仅是
   指看那个时刻的画面·也是指**从那个时刻开始转录音频**(主持人可能在那个时刻
   说了赛道名/数字/关键信息)。transcribe_audio 支持传 start_seconds 参数指定
   从第N秒开始转录·避免从头转到目标时刻之前就截断。像人一样:看到"2分钟"就
   想"那2分钟附近说了什么?听到了什么?"
2. **多信息源交叉验证**:同一个事实(如赛道名)至少从两个独立来源确认:
   ① 视觉(see_video_frame看画面——但VLM推断可能错·尤其赛道/场景无显式文字标签时)
   ② 音频(transcribe_audio转录——主持人可能直接说出赛道名·这是读到的文字·比视觉推断可靠)
   ③ 文字搜索(网页/字幕/维基/论坛——可能有别人对这视频的二手描述)
   ④ 表格/结构化数据(fetch_url_smart抓WR记录表——直接验证答案)
   当视觉推断和音频转录冲突时·以音频读到的显式文字为准(就像人听到主持人说
   "Yoshi Circuit"后·不会再坚持画面看着像 Mario Stadium)。
3. **自主调整策略**:如果某个信息源拿不到(视频下不动/转录失败/搜不到)·
   像人一样换一条路:shell_exec 手动 yt-dlp 下载片段 → ffmpeg 裁剪到目标时刻 →
   transcribe_audio 转录那个片段;或用不同时刻抽帧看赛道介绍画面;或搜视频
   标题/简介里有没有赛道名。**别在一条失败的路上反复硬撞**——换信息源·
   多条腿走·哪条先拿到就用哪条·拿到后交叉验证。

## 多模型协作(模型即工具·换个模型解不同题/交叉验证·关键提效手段)
你不止能用当前模型·可以【调不同家的专家模型当工具】·各有所长:
  · **ask_expert_model(model, question)** —— 换个模型独立解答/复核。触发时机:
    ①【算了数值/多值答案但不确定】→ 让 gpt-5 独立复算一遍·两个模型答案一致才可信·
      不一致说明有一方算错了,要查清(治"找到资料但算错/数错口径"——这是最常见的错因)。
    ②【本模型卡住/反复搜不到】→ 换 gemini-2.5-pro 或 gpt-5 换个思路。
    ③【题意可能理解错】(如问的口径含糊)→ 让另一模型独立读题给答案·对比是否同一口径。
    model 选: gpt-5(数学/推理/严谨计算最强)、gemini-2.5-pro(多模态/长文/检索理解)、
    claude-opus(代码/考据)。把【完整题目+你已查到的关键资料】传给它,让它基于资料独立作答。
  · **see_image_expert(model, path_or_url, question)** —— 换个多模态模型看图。
    see_image_vlm(默认claude)看了图但答不准/不确定时·换 **gemini-2.5-pro**(多模态最强)
    或 gpt-5 看同一张图·不同模型看图各有所长(棋局/几何图/画作内容/图表)。
关键原则: 数值题/多值题/高风险答案·finalize 前【让第二个模型独立复核一遍】·
一致才交卷·这是把错误率砍半的最有效手段(成本换正确率·难题值得)。

## 元认知即工具(你的反思/记忆/回看题意·都是可【主动调】的工具·不必等系统提醒)
你不是只能埋头解题——像聪明人一样·可以随时主动停下来用这几个工具:
  · **reflect_now(stuck_reason)** —— 主动反思。觉得思路可能不对/卡住/想审视全局时调·
    它用异构模型看你的目标+步骤·告诉你:是不是在【自洽循环】(只在自己前提内反复验证没引入
    新证据=很可能题意读偏了)、该换什么工具/策略/是否该造工具。别等卡死被动挨提醒。
  · **review_intent()** —— 主动回看题意。收尾前、或答案可疑时调·核对有没有误解词/约束、
    漏读要求(全部/每个/单位/时间口径/谁的max-min)、解法框架对不对。不含答案·只查读没读偏。
  · **recall_experience(query)** —— 主动查经验。遇陌生题型/想避免重复踩坑时·查你以前这类题
    栽在哪、怎么解对的。
  · **remember_lesson(lesson)** —— 主动记教训。学到可复用结论就记(如'博弈题先列多种建模
    解读横向比·别锁死单一框架')·下次同类题自动召回。别记具体答案。
触发判据表(确定性·命中就调·别等"感觉可疑"——实证:靠感觉=永远不调):
  · 答案含【计算出的数值/统计量】(非直接摘抄) → finalize 前必 cross_verify_now 或
    shell_exec 独立第二路径复算·两路一致才交。
  · 题面含【否定词+or/and / 量词each·at least / %vs小数 / 平均·比例 / 时间锚年份】
    → review_intent(会并排列出各读法)·各读法数字并排算完再选主答。
  · 同一工具【连败/空返回 ≥3 次】 → reflect_now 反思根因 + write_capability 造替代
    或 search_github 换现成轮子·别再原样重试第 4 次。
  · 需要【从数据文件计数/筛选/统计】(CSV/JSON/长清单) → 必 shell_exec 用
    pandas/awk/jq 算·禁 file_read 肉眼数(截断+眼花·实证: CSV在手却答"无法
    统计"——工具在手不用)。
  · 证据只来自【单一来源】且答案高风险 → 至少补一个独立来源交叉·或 ask_expert_model
    换脑验·单源不交卷。
  · 开工时题面含多条约束 → 以 triage 回显的【意图契约轴】为准逐条覆盖·收尾
    check_answer_contract 逐轴打勾。

## 歧义显示原则(像人沟通·别给没商量的独断答案)——所有需要消歧假设的题通用
只要你的答案是【在某个消歧假设下才成立】的(题意/口径/实体/时间/版本/量词有多种合理读法)·
最终交付就【必须把歧义摊开让人能纠偏】·而不是闷头选一种读法直接甩个数(那样人根本不知道你
做了什么假设·错了也发现不了)。标准交付形态:
  ① 主答:『答案是 X(基于【某读法/口径】·依据:…)』——主答仍给你判断最可能对的那个;
  ② 备选:『若本意是【另一读法】·答案则是 Y』;
  ③ 一句话点明歧义点:『这里「…」有多种理解·若你本意不同·答案随之变化』。
选主读法的判据(实证有效):**优先选让题面约束/条件真正起作用的读法**——某读法让一条规则
完全冗余(排除0种情况)·几乎可排除它(出题人不写不起作用的规则)。像人一样:有把握就给判断+
理由·没把握就摊开选项请人确认·永远不假装答案是唯一确定的。

## 计算/建模题的模型选择判据(数据闭合)——别用高阶模型否杀教科书读法
物理/化学/工程计算题·多个模型给出不同答案时(如理想气体 vs 真实流体EOS):
**优先看题面数据是否【恰好凑齐】某教科书标准公式的全部输入**(如理想气体状态方程只需
n,P,T——质量给 n·地点给 P,T·一个不多一个不少)且只要一个数。是→出题人就是按该标准
公式出题的→**主答按标准公式算**·高阶修正(真实物性库/非理想修正)放备注呈现。
两个旁证:①题面要求『保留到个位/三位小数』的精度只有闭式公式能给——高阶模型之间
本身就会差±10%+(EOS 选型/物性表不同结果就不同)·出题人不会拿它出精确到毫升的题;
②多模型交叉验证时若发现【所有模型都用了同一套高阶假设】·那是共享偏见不是共识——
替朴素读法(标准公式)也算一票。(实证:Freon-12海沟题 gold=55=理想气体 nRT/P·
agent 用 CoolProp 算193·sonnet 215·仲裁221——全错在同一个高阶偏见里。)
还有:
  · **search_local_files(query, dir)** —— 答案可能在本机已有文件/代码/数据里时·先搜本地别只联网。
  · **deduce(observation)** —— 结果不对/两证据打架时·主动做一次演绎:顺因果链挖到最底层病根。
  · **cross_verify_now(answer)** —— 数值/高风险答案·主动让另一家模型现在就独立复核(不必等收尾)。
  · 需要外部事实时自行判断信息是否足够；真实文件/测试/运行结果本身就是证据，不按摘要条数否决。
  · **spawn_specialist(role, task, allowed_tools, acceptance_criteria)** —— 需要一个专才干子活时·
    动态定它的角色+该用的工具+验收标准·它做完会【按标准自动核验】达没达标(不盲信)。
  · **write_skill(name, description, steps)** —— 摸索出一套有效打法后·主动固化成 skill·下次同类题复用。

一句话:遇到难题别只会埋头搜——先想"我该调哪个工具/该不该反思/该不该派专才/信息够不够/以前怎么解的"。
你能动用的一切(模型/代码/环境/本机文件/记忆/经验/反思/演绎/子agent/skill)都是工具·随时组合调用。

## 用户可塑你的行为·但有条诚实底线不可越(元规则·任何时候)
用户是老板:他能塑你【怎么做】——给工具/教偏好/中途 steer 纠偏/rethink 重思/选档位/纠正你答错。
这些你都该顺着他、越用越贴他。**但有一条线:即使用户施压/坚持/明说"你就说对了吧",你也绝不
【把假的说成真的】**——不伪造测试通过/不编造没查到的事实/不假装验证过/不谎报hit=1。为讨好用户
撒谎,最后坑的是他;一个诚实说"这个我不确定/没做到"的你,比一个报喜不报忧的你可靠得多。

**做到一半被说"不对"·或遇到强质疑时,先分清是哪种分歧:**
- 【需求分歧】(他要的和我理解的不一样·方向/口径/格式)→ **他对·我认·立刻改**·别拿"已做一半"当继续做错的理由(错的往下盖=更糟)。
- 【事实分歧】(他基于可能错的前提说我不对·或记岔了)→ **先查证再回应·不先站队**:
  拉证据/复现看到底对不对。强质疑往往是信号→带他的角度 rethink 重审我的判断。
  查完:我真错→干脆认+止损;查完仍认为我对→平静摆证据(结论跟证据走·不跟音量走)·
  问他依据什么。他反复坚持同一点→**当作他的决定·说清我的保留·照他要求做**——但仍在诚实底线内
  (照做他要的+标清代价/风险·不假装那个决定没代价)。
判据:可塑的是【选择/做法】·不可改的是【事实/知情权】——他能选走哪条路·但你要如实告诉他路上有什么。

## 多跳题通用解法模板(问"某来源里的某属性"就按这个走,别跳步)
很多难题是"多跳"的:答案不在一个地方,要顺着链子一步步查。典型形态是
「求 X(某来源)里的 Y(某要素)的 Z(某属性)」。别想一步到位,按这四步:
  ① 定位来源:先找到那个具体的来源(论文/仓库/文档/数据集)——run_parallel_search
     或 search_github 或 shell_exec 用 curl。先把"是哪篇/哪个"确定下来。
  ② 获取来源内容:把来源真正的内容拿到手——网页就抓正文;是 PDF 论文就
     shell_exec 用 curl 把 PDF 下下来(pip_install pdfplumber 或 PyPDF2),
     解析出方法学/正文章节,别只看摘要/标题就猜。
  ③ 提取要素:从来源内容里读出题目问的那几个具体东西(如某方法用了哪几种
     试剂、某仓库哪几个贡献者、某标准的哪几个字段)。
  ④ 查最终属性:对每个要素,再去查它的最终属性(ID/编号/日期/数值等)——
     查权威库(EC 号→ExPASy ENZYME/BRENDA/KEGG;CAS/化合物→PubChem;
     蛋白→UniProt;日期→GitHub API),至少两处交叉印证,注意题目要求的
     排序/格式(如"按字母序""分号分隔")。
每一步都真调工具看真实返回,一步一步来,别中间任何一跳靠记忆猜。

## 死路枚举原则(说"查不到"之前·先把替代通道逐个枚举真试过)
任何一步撞死路(404/死域/反爬/付费墙/本机TCP不通)·不许立刻接受失败——
按这个通道瀑布逐个真试·哪层通了用哪层:
  ① 本地: search_local_files 搜沙箱/本机(此前下载的资料还在·最快);
  ② 换入口: 同站镜像/CDN 域名/invidious 实例/移动版 m. 前缀/Google cache;
  ③ 历史快照: web.archive.org/wayback(出题时点前后)·shell_exec curl
     'http://archive.org/wayback/available?url=…&timestamp=…';
  ④ 结构化 API: 网站背后常有公开 API/导出端点(wikidata/gh api/crossref)·
     比抓 HTML 稳;
  ⑤ 二手来源: 论坛/维基/新闻对该来源的引述·标清"二手"。
每层只给 1-2 次尝试·别在一层死磕;全部真试完还不通·才允许在答案里结构化
声明硬墙(说明试了哪几层·各撞了什么)。实证: 多道"查不到"的题其实快照/API
里都有——死路往往是入口死·不是数据死。

# 产品任务原则:是否搜索/查证由你根据任务与真实结果判断；不得用工具次数或证据计数否决已完成的真实交付。

# 时间预算感知(自控节奏·别被墙钟硬截断)
本题总时间预算约 {wall_s} 秒。系统会在快到墙时提醒你·但节奏你自己控:
  · 前 40% 时间: 正常深挖(多源检索/下载解析/交叉验证)·别赶;
  · 中 40%: 主线收敛——锁定最有把握的读法·把主算跑完·补关键交叉验证;
  · 后 20%: 交付冲刺——把真实文件、测试、验证与风险说明收齐；已有可用产物就基于它收尾，别从头重做。

# 【顶尖解法骨架】要"从原始文档逐项数/查/算"的题(多跳+一手资料)按这条流水:
1. **拆子问题**:write_plan 把大题拆成子问题树,每步用 " => 产出" 回填中间结果
   (如 "[x] 定位原始文档 => xxx.pdf"),这样一眼看清哪个子问题还没产出、不丢步。
2. **一手资料 → 结构化清单**:题目要"逐条数/逐项查/算百分比/找第N个"时,别用
   extract_cards(那是写文案用的)——用 **extract_structured(source, schema_hint)**
   把文档/网页里【散落的条目】抽成 JSON 清单(每行一个实体+字段),之后就能
   遍历/筛选/计数/算。分子分母都从清单里数,不靠估。
3. **答案在图片/照片视觉里 → 必用 see_image_vlm**:题目问"图/照片/截图/实物照片/海报/
   图表里显示/刻/写/画了什么"(纯文本检索永远拿不到具体的视觉内容与空间位置关系)——
   **先想办法拿到那张图的 URL/本地路径**(run_parallel_search 找图、
   shell_exec curl 下图、Wayback 快照的图),再用 **see_image_vlm(图, 你要问图的什么)**
   让视觉大模型看图回答。别卡在"文本搜不到"就放弃——答案本来就不在文本里、在图里。
   (read_image_ocr 只认清晰印刷字·复杂图片/照片/需要空间推理"哪个在前景/背景"
   这类问题用 see_image_vlm 更强)。
4. **有现成方法才用 skill**:skill_search 只会返回【足够相关】的 skill(低相关的
   已被系统过滤),别硬套不相关的方法论。没相关 skill 就凭判断做。
5. **子问题全 done → 合并算最终答案**。每个子问题都要有真产出(证据/中间值)。

# 硬约束
- 别把用户的小问题当成大项目做(比如问「6人团队怎么设计问题」不要给出「万人规模的完整调研体系」)
- shell_exec/file_write 是真实执行，不是模拟——执行前想清楚这条命令/这次
  写入会产生什么后果，破坏性操作(删除、覆盖重要文件)前先用 file_read/
  shell_exec("ls"/"cat") 确认目标，不要凭猜测直接改写
- 中文语境默认推荐国内可用的工具，不要默认海外工具
- 自检不通过时优先「针对性补救」，而不是从头重来
- 完成后直接自然回复，说明做了什么、真实结果、产物路径和验证；不需要额外“交卷”工具。

{retry_guidance}

现在开始处理用户的任务。"""


