#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""legion_client:任何端【一行接入】军团的统一客户端(用户2026-08-30:桌面/IM/任何端都等同万物最强).

诉求:桌面端/IM端/任何脚本都能一句话调最强 agent 当首脑、编排其他家——任何端接入军团都等同于
"万物最强"。之前只有裸 HTTP /api/legion/dispatch + SSE·各端要自己拼协议。这里封装成一行:

    from legion_client import ask
    print(ask("帮我做一份社区团购冷启动方案"))            # 默认最强编排·拿最终结果
    for ev in ask_stream("...", mode="orchestrate"): ...    # 要过程流(桌面/IM实时显示)

设计:①极简(一个函数)②默认走最强(mode=orchestrate 选适任度最高的当首脑编排)③快(直连本地
server·SSE 首字节即返)④任何端通用(纯 stdlib·无第三方依赖·python/桌面/IM bot 都能 import)。
IM/桌面端只要能跑 python 或发 HTTP 就接得上;不能跑 python 的端照这个 HTTP 契约发请求即可。
"""
from __future__ import annotations
import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlencode

# 默认端口对齐 server.py 的 LEGION_PORT(默认 8000);任何端可用 LEGION_BASE_URL 覆盖。
_DEFAULT_BASE = os.environ.get(
    "LEGION_BASE_URL", f"http://127.0.0.1:{os.environ.get('LEGION_PORT', '8000')}")


def _endpoint(base: str) -> str:
    return base.rstrip("/") + "/api/legion/dispatch"


class LegionClientError(RuntimeError):
    """Stable error codes only; never retain raw headers, token, URL or HTTP body."""
    def __init__(self, code: str, status: int | None = None):
        self.code, self.status = code, status
        super().__init__(code if status is None else f"{code} (HTTP {status})")


class _NoCredentialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Do not forward a personal Bearer to another endpoint.


def _personal_headers(base: str, personal_token: str | None) -> dict:
    token = os.environ.get("LEGION_PERSONAL_TOKEN", "") if personal_token is None else personal_token
    token = str(token).strip()
    if not token or "\r" in token or "\n" in token:
        raise LegionClientError("auth_required")
    parsed = urlsplit(base)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.scheme not in {"http", "https"}
            or parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"}):
        raise LegionClientError("secure_endpoint_required")
    return {"Authorization": "Bearer " + token}


def _open(request, timeout):
    try:
        return urllib.request.build_opener(_NoCredentialRedirect()).open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        code = "auth_required" if exc.code in (401, 403) else "http_error"
        if exc.code == 403:
            try:
                data = json.loads(exc.read(8192))
                if data.get("error") == "personal_owner_mismatch":
                    code = "owner_mismatch"
            except (ValueError, OSError, AttributeError):
                pass
        status = exc.code
        exc.close()
        raise LegionClientError(code, status) from None
    except (urllib.error.URLError, OSError):
        raise LegionClientError("server_unreachable") from None


def harnesses(base_url: str = "", *, personal_token: str | None = None,
              uid: str | None = None, timeout: int = 10) -> dict:
    """Read the real directory using a caller-owned token, not the instance token."""
    base = base_url or os.environ.get("LEGION_BASE_URL") or _DEFAULT_BASE
    headers = _personal_headers(base, personal_token)
    url = base.rstrip("/") + "/api/legion/harnesses"
    if uid is not None:
        url += "?" + urlencode({"uid": uid})
    with _open(urllib.request.Request(url, headers=headers), timeout) as response:
        try:
            data = json.load(response)
        except (ValueError, UnicodeError):
            raise LegionClientError("invalid_response") from None
    if isinstance(data, list):
        return {"harnesses": data}
    if not isinstance(data, dict) or not isinstance(data.get("harnesses"), list):
        raise LegionClientError("invalid_response")
    return data


def ask_stream(question: str, mode: str = "orchestrate", base_url: str = "",
               plan: dict | None = None, timeout: int = 900, *,
               personal_token: str | None = None, uid: str | None = None,
               session_id: str | None = None):
    """流式调军团·yield 每个过程事件(桌面/IM 端实时显示"军团在干嘛")。

    mode: orchestrate(默认·选最强当首脑编排其他)/ pk(多家并行比最佳)/ relay(串行接力)。
    Personal credential: explicit personal_token takes precedence over
    LEGION_PERSONAL_TOKEN. No fallback to LEGION_API_TOKEN or a shared cookie.
    Omitted uid is left to the server's authenticated subject.
    session_id: use the sid from an earlier meta event to continue the same
    owned project/native session. Omitted means an ordinary new conversation.
    """
    base = base_url or os.environ.get("LEGION_BASE_URL") or _DEFAULT_BASE
    headers = _personal_headers(base, personal_token)
    if session_id is not None and (
            not isinstance(session_id, str) or not session_id
            or session_id.strip() != session_id
            or any(ord(char) < 32 or ord(char) == 127 for char in session_id)):
        raise LegionClientError("invalid_session_id")
    body = json.dumps({"question": question, "mode": mode,
                       **({"uid": uid} if uid is not None else {}),
                       **({"session_id": session_id} if session_id is not None else {}),
                       **({"plan": plan} if plan else {})}).encode()
    req = urllib.request.Request(_endpoint(base), data=body,
                                 headers={**headers, "Content-Type": "application/json",
                                          "Accept": "text/event-stream"})
    with _open(req, timeout) as resp:
        ev_type = None
        for raw in resp:
            line = raw.decode("utf-8", "ignore").rstrip("\n")
            if line.startswith("event:"):
                ev_type = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
                try:
                    payload = json.loads(data)
                except Exception:
                    payload = {"raw": data}
                yield {"event": ev_type or "message", "data": payload}


def _delivery_text(result) -> str:
    """Read declared result fields, without importing an engine or judging quality.

    Dispatch uses winner as a task/member key and results as the content source.
    Keep its explicit winner/final_result and execution-order fallback. Legacy
    direct final/answer/synthesis and winner-object responses remain supported.
    """
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return ""
    for field in ("answer", "final", "synthesis"):
        value = result.get(field)
        if isinstance(value, str) and value.strip():
            return value
    winner = result.get("winner")
    if isinstance(winner, dict):
        value = winner.get("final")
        if isinstance(value, str) and value.strip():
            return value
    raw_rows = result.get("results")
    rows = [row for row in raw_rows if isinstance(row, dict)
            and not row.get("supporting_context")
            and isinstance(row.get("final"), str) and row["final"].strip()
            ] if isinstance(raw_rows, list) else []
    pool = [row for row in rows if row.get("status") == "done"] or rows
    audit = result.get("success_audit")
    if not isinstance(winner, str) or not winner:
        winner = audit.get("winner") if isinstance(audit, dict) else None
    if isinstance(winner, str) and winner:
        for row in pool:
            if winner in (row.get("task_id"), row.get("key")):
                return row["final"]
    final_result = result.get("final_result")
    if isinstance(final_result, dict) and final_result in pool:
        return final_result["final"]
    return pool[-1]["final"] if pool else ""


def ask(question: str, mode: str = "orchestrate", base_url: str = "",
        plan: dict | None = None, timeout: int = 900, *,
        personal_token: str | None = None, uid: str | None = None,
        session_id: str | None = None) -> str:
    """返回原样交付文本；传 session_id 可继续本人会话，不将协议 JSON 冒充答案。

    没有文本的终态以 no_final_text 明确报告；文件等结构化结果使用 ask_stream 获取。
    """
    final = ""
    final_seen = False
    parts = []
    for ev in ask_stream(question, mode=mode, base_url=base_url, plan=plan, timeout=timeout,
                         personal_token=personal_token, uid=uid, session_id=session_id):
        d = ev.get("data") or {}
        if ev["event"] == "final":
            final_seen = True
            r = d.get("result") if isinstance(d, dict) else None
            final = _delivery_text(r if r and isinstance(r, (dict, str)) else d)
        elif ev["event"] in ("result", "answer"):
            if isinstance(d, dict) and d.get("x"):
                parts.append(str(d["x"]))
    if final_seen and not final:
        raise LegionClientError("no_final_text")
    return final if final_seen else "\n".join(parts)


def health(base_url: str = "", *, personal_token: str | None = None, uid: str | None = None) -> dict:
    """探军团在不在(任何端接入前先探活)。"""
    try:
        data = harnesses(base_url, personal_token=personal_token, uid=uid)
        return {"ok": True, "harnesses": len(data["harnesses"])}
    except LegionClientError as exc:
        return {"ok": False, "error": exc.code, "http_status": exc.status,
                "hint": ("提供调用方已有个人凭证（LEGION_PERSONAL_TOKEN）；共享实例token不可替代"
                         if exc.code == "auth_required" else
                         "显式uid必须属于调用方本人" if exc.code == "owner_mismatch" else
                         "检查服务地址与网络；未修改或重启服务")}


if __name__ == "__main__":
    import argparse
    import sys
    parser = argparse.ArgumentParser(description="军团HTTP客户端；个人凭证仅从LEGION_PERSONAL_TOKEN读取，不放argv")
    parser.add_argument("--uid", default=None, help="可选本人owner；未指定由服务认证决定")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--mode", default="orchestrate")
    parser.add_argument("--session-id", default=None, help="可选已有本人会话sid；省略则创建新会话")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("question", nargs="*")
    args = parser.parse_args()
    if args.question == ["health"]:
        result = health(args.base_url, uid=args.uid)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0 if result["ok"] else 1)
    if args.question:
        try:
            for ev in ask_stream(" ".join(args.question), mode=args.mode, base_url=args.base_url,
                                 timeout=args.timeout, uid=args.uid, session_id=args.session_id):
                print(f"[{ev['event']}]", json.dumps(ev["data"], ensure_ascii=False)[:120])
        except LegionClientError as exc:
            print(json.dumps({"ok": False, "error": exc.code, "http_status": exc.status}))
            sys.exit(1)
