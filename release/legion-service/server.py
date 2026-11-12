#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""军团服务 (legion-service) —— 一个文件启动的可上线 HTTP 服务

沿用 agent_web.py 的成熟做法:纯 http.server + 零前端依赖,一个文件起服务。
只暴露军团需要的接口,壳干净、好打包、明天可直接上线。

接口:
  GET  /                       军团前端页面(web/index_v2.html·唯一前端)
  GET  /health                 健康检查(探针用·不认证)
  GET  /api/legion/harnesses   列出军团全部成员(10 个真 harness；能力后端不混入)
  POST /api/legion/dispatch    一句话派活·并行军团出击(SSE 实时推每家每一步)
                               body: {"question": "...", "plan": {"tasks":{...},"hierarchy":{...}}}
  POST /run                    简易别名(通用验收模板)·body {"query": "..."} → 全军团出击

启动: python3.11 server.py [--port 8000]  (云原生运行时使用 Python 3.11；端口兼容 LEGION_PORT / PORT)
"""
from __future__ import annotations
import os
import sys
import hmac
import hashlib
import ipaddress
import json
import re
import secrets
import socket
import time
import argparse
import threading
import traceback
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, parse_qsl, urlencode, quote

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import legion_paths as PATHS  # noqa: E402
import legion_core as LEGION  # noqa: E402
import sessions as SESSIONS  # noqa: E402
import cron_jobs as CRON  # noqa: E402
import crawler as CRAWLER  # noqa: E402
import toolbox as TOOLBOX  # noqa: E402
import media_tools as MEDIA  # noqa: E402
import legion_tool as LEGION_TOOL  # noqa: E402  # 统一工具执行入口(媒体+search_github+opencli)
import harness_profiles as PROFILES  # noqa: E402
import model_registry as MODELS  # noqa: E402
import install_registry as INSTALL  # noqa: E402
import trace_view as TRACE  # noqa: E402
import opencli_bridge as OPENCLI  # noqa: E402
import artifact_delivery as ARTIFACTS  # noqa: E402
import desktop_fs as DESKTOP_FS  # noqa: E402
import team_state as TEAMS  # noqa: E402
import workspace_view as WORKSPACE  # noqa: E402
import approval_state as APPROVALS  # noqa: E402
import skill_factory as SKILLFACTORY  # noqa: E402
import desktop_workspace as DESKTOP_WORKSPACE  # noqa: E402
import objective_workspace as OBJECTIVE_WORKSPACE  # noqa: E402
import desktop_worker as DESKTOP_WORKER  # noqa: E402
import human_handoff as HUMAN_HANDOFF  # noqa: E402
import desktop_software as DESKTOP_SOFTWARE  # noqa: E402
import desktop_recipe as DESKTOP_RECIPE  # noqa: E402
import desktop_transfer as DESKTOP_TRANSFER  # noqa: E402
import mobile_apps as MOBILE_APPS  # noqa: E402
import mobile_tasks as MOBILE_TASKS  # noqa: E402
import mobile_companion as MOBILE_COMPANION  # noqa: E402

# 云原生平台通常注入 PORT；本地及既有部署仍可用 LEGION_PORT 覆盖。
PORT_DEFAULT = int(os.environ.get("LEGION_PORT") or os.environ.get("PORT") or "8000")
WEB_DIR = os.path.join(HERE, "web")
MAX_BODY = 2 * 1024 * 1024  # 2MB 上限,防内存被打爆
_HTML_CACHE: dict[str, tuple[int, str]] = {}
_LOOPBACKS = {"127.0.0.1", "::1", "localhost"}
# 服务器部署模式(0.0.0.0 绑定·显式 opt-in):放行同源浏览器访问,
# 鉴权交由 _authorized 的 token 门(页面加载自动发 HttpOnly cookie;CLI 走 Bearer)。
_REMOTE_BIND_OK = os.environ.get("LEGION_ALLOW_REMOTE_BIND") == "1"
# Explicit personal deployment mode: same-machine TLS proxy, one configured
# origin, unchanged personal identity checks and loopback listener.
_PUBLIC_ORIGIN = os.environ.get("LEGION_PUBLIC_ORIGIN")
# API token 持久化(2026-09-07实锤修复):此前每次启动随机生成——server 重启后所有已打开
# 页面的旧 cookie 全部 403(用户云手机/云电脑面板"启动失败:forbidden"),违反铁律
# "server重启后session/model_health/交付物都在"。改为:env 优先,否则磁盘持久化文件
# (~/.legion 下),没有才首启生成并落盘;重启复用同一 token,旧页面无需重新登录。
def _load_or_create_api_token() -> str:
    env_token = str(os.environ.get("LEGION_API_TOKEN") or "").strip()
    if env_token:
        return env_token
    try:
        import legion_paths as _lp
        _path = _lp.under("api_token")
        _os_path = __import__("pathlib").Path(_path)
        if _os_path.is_file():
            _saved = _os_path.read_text(encoding="utf-8").strip()
            if _saved:
                return _saved
        _fresh = secrets.token_urlsafe(32)
        _os_path.parent.mkdir(parents=True, exist_ok=True)
        _tmp = _os_path.with_suffix(".tmp")
        _tmp.write_text(_fresh, encoding="utf-8")
        _tmp.replace(_os_path)
        return _fresh
    except Exception:
        return secrets.token_urlsafe(32)


_API_TOKEN = _load_or_create_api_token()
_LEGACY_NO_ORIGIN = str(os.environ.get("LEGION_ALLOW_UNAUTHENTICATED_LOCAL_CLI") or "1").lower() \
    not in {"0", "false", "no", "off"}


def _uid_from_request(handler, qs=None, body=None) -> str | None:
    """兼容原路由调用；身份只取前置校验后的同一主体，不二次消费票据。"""
    return handler._personal_user()


def _readonly_instance_environment(user_id: str) -> dict:
    """Bind local owner observations without reloading login/model credentials.

    Only an authenticated subject and the trusted launcher's persisted instance
    mapping may supply this namespace; never a request body or a public instance.
    """
    source = os.environ
    if (not user_id or source.get("LEGION_PERSONAL_OWNER") != user_id
            or not source.get("MEYO_PERSONAL_STATE_DIR")
            or source.get("LEGION_PUBLIC_ORIGIN") is not None
            or source.get("LEGION_ALLOW_REMOTE_BIND", "0") != "0"):
        return {}
    import personal_runtime
    try:
        root = personal_runtime._root(source["MEYO_PERSONAL_STATE_DIR"])
        config = personal_runtime._configuration(root)  # config.json only, not environment()
    except (OSError, ValueError, TypeError):
        return {}
    if (config["user"] != user_id or "public_origin" in config
            or source.get("MEYO_LEGION_DIR") != str(root / "data")
            or source.get("LEGION_HOME_DIR") != str(root / "legion-home")):
        return {}
    return {"MEYO_LEGION_DIR": str(root / "data")}


def request_origin_allowed(remote_addr: str, bound_host: str, host_header: str,
                           origin_header: str = "") -> bool:
    """强执行 API 只接受回环来源；浏览器 Origin 必须与 Host（含端口）同源。

    服务器部署(LEGION_ALLOW_REMOTE_BIND=1)扩展：非回环请求放行【同源浏览器】
    (Origin 与 Host 匹配·CSRF 防护不变)，鉴权由 _authorized 的 token 门负责；
    无 Origin 的裸 CLI 请求在远程模式下必须带 Bearer token。
    """
    if _PUBLIC_ORIGIN is not None:
        from personal_identity import public_request_origin_allowed
        return public_request_origin_allowed(
            _PUBLIC_ORIGIN, remote_addr, bound_host, host_header, origin_header)
    try:
        host = urlparse("//" + str(host_header or ""))
        origin = urlparse(str(origin_header or "")) if origin_header else None
    except ValueError:
        return False
    loopback = (str(remote_addr or "").lower() in _LOOPBACKS
                and str(bound_host or "").lower() in _LOOPBACKS
                and str(host.hostname or "").lower() in _LOOPBACKS)
    if not loopback and not _REMOTE_BIND_OK:
        return False
    if origin is None:
        return True
    same_site = str(origin.netloc or "").lower() == str(host.netloc or "").lower()
    if loopback:
        return same_site and bool(origin.hostname) and str(origin.hostname).lower() in _LOOPBACKS
    return same_site


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                         default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _public_source_url(value: object) -> str:
    """持久化去凭证、非回环的 HTTP(S) 可回访地址；不在请求热路径做 DNS。"""
    text = str(value or "").strip()[:4000]
    try:
        parsed = urlparse(text)
        host = str(parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
            return ""
        if host in _LOOPBACKS or host.endswith((".local", ".localhost")):
            return ""
        literal = None
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            # 拦截 2130706433 / 0x7f000001 / 0177.0.0.1 等历史 IPv4 写法；
            # inet_aton 仅做本地字面量解析，不触发 DNS。
            if re.fullmatch(r"[0-9a-fx.]+", host, re.I):
                try:
                    literal = ipaddress.ip_address(socket.inet_aton(host))
                except OSError:
                    literal = None
        if literal is not None and not literal.is_global:
            return ""
        from artifact_content_safety import sensitive_key as sensitive
        safe_query = urlencode([
            (key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
            if not sensitive(key)
        ], doseq=True)
        fragment = parsed.fragment
        fragment_fields = parse_qsl(fragment, keep_blank_values=True) if fragment else []
        if (fragment and sensitive(fragment)) or any(sensitive(key) for key, _val in fragment_fields):
            fragment = ""
        return parsed._replace(query=safe_query, fragment=fragment).geturl()
    except (TypeError, ValueError):
        return ""


def _query_source_ref(value: object) -> dict:
    """保留可回访来源；浏览器字段仅是报告事实，不能冒充服务端已核验。"""
    raw = value if isinstance(value, dict) else {}
    source_type = str(raw.get("type") or "user_direct").strip().lower()[:80]
    allowed_types = {"user_direct", "session_retry", "session_continue", "user_accepted",
                     "dogfood", "github_issue", "public_web", "regression", "synthetic_stress"}
    source_type = source_type if source_type in allowed_types else "public_web" if raw.get("url") else "user_direct"
    url = _public_source_url(raw.get("url"))
    if raw.get("url") and not url:
        source_type = "user_direct"
    api_url = _public_source_url(raw.get("api_url"))
    snapshot_ref = _public_source_url(raw.get("snapshot_ref"))
    digest = str(raw.get("content_sha256") or "").strip().lower()
    digest = digest if re.fullmatch(r"[0-9a-f]{64}", digest) else ""
    fetch_status = str(raw.get("fetch_status") or "not_reported").strip().lower()[:80]
    reported_statuses = {"reported_not_fetched", "reported_fetched", "reported_partial", "reported_failed"}
    if fetch_status not in {"not_reported", "not_fetched", "fetched", "partial", "failed", *reported_statuses}:
        fetch_status = "not_reported"
    normalized_fetch_status = (fetch_status if fetch_status in reported_statuses
                               else "reported_" + fetch_status if fetch_status != "not_reported"
                               else fetch_status)
    result = {
        "type": source_type, "url": url, "api_url": api_url,
        "title": str(raw.get("title") or "")[:500],
        "author": str(raw.get("author") or "")[:200],
        "fetch_status": normalized_fetch_status,
        "content_sha256": digest, "snapshot_ref": snapshot_ref,
        "attestation": "caller_supplied",
    }
    claims = raw.get("supports_claims") if isinstance(raw.get("supports_claims"), list) else []
    result["supports_claims"] = [str(item)[:1000] for item in claims[:20] if str(item).strip()]
    try:
        captured = float(raw.get("captured_at") or time.time())
        result["captured_at"] = captured if captured == captured and abs(captured) != float("inf") else time.time()
    except (TypeError, ValueError):
        result["captured_at"] = time.time()
    if url:
        result["source_id"] = "query-" + hashlib.sha256(url.encode()).hexdigest()[:12]
    return result


def _source_version_identity() -> dict:
    """从工作树元数据读取版本，不在用户请求热路径运行 git/status。"""
    repo = os.path.abspath(os.path.join(HERE, "..", ".."))
    git_dir = os.path.join(repo, ".git")
    commit = branch = ""
    try:
        head = open(os.path.join(git_dir, "HEAD"), encoding="utf-8").read().strip()
        if head.startswith("ref: "):
            ref = head[5:].strip()
            branch = ref.rsplit("/", 1)[-1]
            commit = open(os.path.join(git_dir, ref), encoding="utf-8").read().strip()
        else:
            commit = head
    except OSError:
        pass
    files = {}
    for name in ("server.py", "legion_core.py", "sessions.py", "project_context.py", "team_state.py"):
        try:
            files[name] = hashlib.sha256(open(os.path.join(HERE, name), "rb").read()).hexdigest()
        except OSError:
            pass
    return {"commit": commit, "branch": branch, "service": "LegionService/1.0",
            "working_files_sha256": files}


def _quality_context_with_source(question: str, plan: dict, source_ref: dict) -> dict:
    """把原始订单来源放进现有 quality_context；只编译事实，不替 Harness 设计答案。"""
    context = dict(plan.get("quality_context") or {})
    context.setdefault("schema_version", 1)
    context.setdefault("revision", 1)
    context.setdefault("original_query", question)
    context["source_ref"] = dict(source_ref)
    if source_ref.get("url"):
        source = {
            "source_id": source_ref.get("source_id"), "title": source_ref.get("title"),
            "url": source_ref.get("url"), "kind": "user_query",
            "source_type": source_ref.get("type"), "fetch_status": source_ref.get("fetch_status"),
            "content_sha256": source_ref.get("content_sha256"),
            "snapshot_ref": source_ref.get("snapshot_ref") or source_ref.get("api_url"),
            "supports_claims": list(source_ref.get("supports_claims") or []),
            "visited_at": source_ref.get("captured_at"), "decision": "query_origin",
        }
        previous = [dict(item) for item in (context.get("sources") or []) if isinstance(item, dict)]
        context["sources"] = [source, *[item for item in previous
                                        if item.get("source_id") != source.get("source_id")]][:40]
    return context


def _cached_html(path: str) -> str:
    """按 mtime 缓存大 HTML；浏览器仍 no-store，开发改文件会立即失效。"""
    try:
        stamp = os.stat(path).st_mtime_ns
        cached = _HTML_CACHE.get(path)
        if cached and cached[0] == stamp:
            return cached[1]
        with open(path, encoding="utf-8") as f:
            html = f.read()
        _HTML_CACHE[path] = (stamp, html)
        return html
    except Exception as e:
        return f"<h1>军团前端缺失</h1><pre>{path}\n{e}</pre>"


class LegionServer(ThreadingHTTPServer):
    """上线级并发:默认 ThreadingHTTPServer 的 socket backlog 只有 5,高并发会
    大量 Connection reset(实测 20 并发只成 7)。调大 backlog + daemon 线程 +
    端口复用,让服务扛得住真实并发。"""
    request_queue_size = 128       # socket listen backlog(默认 5 → 128)
    daemon_threads = True          # 请求线程随主进程退出,不残留
    allow_reuse_address = True     # 重启不被 TIME_WAIT 卡住

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 测试、桌面嵌入和其它程序化启动同样必须拥有服务实例 token；此前仅 CLI
        # main() 赋值，导致页面 GET 正常、首个 POST 却必然 403。
        self.api_token = _API_TOKEN


def _read_index_html() -> str:
    # 唯一前端 index_v2(用户 2026-08-30 删 v1 index·统一一份)
    return _cached_html(os.path.join(WEB_DIR, "index_v2.html"))


def _read_html(name: str) -> str:
    path = os.path.join(WEB_DIR, name)
    html = _cached_html(path)
    return _read_index_html() if html.startswith("<h1>军团前端缺失</h1>") else html


def _peer_stop_pilot(result: dict, rounds: int) -> dict:
    """Project an actual peer stop without relabelling the author's saved draft."""
    from error_envelope import (is_provider_authorization_denial, requires_execution_pause,
                                requires_native_settlement, requires_output_recovery_pause,
                                requires_request_budget_pause)
    if result.get("peer_input_error"):
        return {"round": rounds, "action": "stop", "kind": "peer_execution_error"}
    paused = result.get("peer_input_pause")
    if not requires_execution_pause(paused):
        return {}
    kind = ("request_budget_exhausted" if requires_request_budget_pause(paused) else
            "provider_authorization_required" if is_provider_authorization_denial(
                paused.get("error_envelope")) else
            "native_execution_unconfirmed" if requires_native_settlement(paused) else
            "output_recovery_required" if requires_output_recovery_pause(paused) else
            "provider_backoff")
    return {"round": rounds, "action": "stop", "kind": kind}


class Handler(BaseHTTPRequestHandler):
    server_version = "LegionService/1.0"

    def _same_origin(self) -> bool:
        if _PUBLIC_ORIGIN is not None:
            hosts = self.headers.get_all("Host", [])
            origins = self.headers.get_all("Origin", [])
            if len(hosts) != 1 or len(origins) > 1 or (origins and not origins[0]):
                return False
        return request_origin_allowed(
            str((self.client_address or ("",))[0]),
            str((self.server.server_address or ("",))[0]),
            str(self.headers.get("Host") or ""), str(self.headers.get("Origin") or ""))

    def _authorized(self) -> bool:
        if not self._same_origin():
            return False
        return bool(self._personal_user())

    def _personal_user(self) -> str:
        ticket_identity = getattr(self, "_request_ticket_identity", None)
        if ticket_identity:
            return ticket_identity
        import personal_identity
        token = personal_identity.personal_token(self.headers, str(getattr(self.server, "api_token", "") or ""))
        return personal_identity.resolve_token(token) or ""

    def _prepare_request_identity(self, *, read_json=False) -> bool:
        import personal_identity
        from meyo_ticket_identity import TicketAuthError
        self._request_ticket_identity = None
        self._request_upload_ticket = None
        self._request_json_payload = None
        self._shared_file_scope = None
        payload = None
        try:
            try:
                ticket = personal_identity.request_ticket(self.headers, self.path)
            finally:
                # Also redact before malformed body/error responses can be logged.
                self._redact_ticket_transport()
            if read_json:
                payload = self._parse_json_payload()
                if payload is None:
                    return False
                self._request_json_payload = payload
                try:
                    body_ticket = personal_identity.request_ticket(self.headers, self.path, payload)
                finally:
                    payload.pop("ticket", None)
                if ticket is not None and body_ticket is not None and ticket != body_ticket:
                    raise TicketAuthError("meyo_ticket_conflicting_credentials")
                ticket = ticket if ticket is not None else body_ticket
            if ticket is not None:
                if not self._same_origin():
                    raise TicketAuthError("meyo_ticket_origin_not_allowed", 403)
                personal_identity.reject_ticket_in_content(payload, ticket)
                personal_identity.reject_ticket_in_content(self.path, ticket)
                self._request_ticket_identity = personal_identity.ticket_user(
                    ticket, self.headers, str(getattr(self.server, "api_token", "") or ""))
                if self._is_multipart_upload():
                    # Retain only for this request's post-parse filename check.
                    self._request_upload_ticket = ticket
            return True
        except TicketAuthError as error:
            self.close_connection = True  # A rejected transport may leave an unread POST body.
            self._send_json({"error": error.code}, error.status)
            return False

    def _redact_ticket_transport(self):
        import personal_identity
        self.path = personal_identity.without_ticket_query(self.path)
        self.requestline = f"{self.command} {self.path} {self.request_version}"
        self.raw_requestline = (self.requestline + "\r\n").encode("iso-8859-1", errors="replace")
        if "X-Legion-Ticket" in self.headers:
            del self.headers["X-Legion-Ticket"]

    def _clear_request_identity(self):
        self._request_ticket_identity = None
        self._request_upload_ticket = None
        self._request_json_payload = None
        self._shared_file_scope = None

    def _account_request_allowed(self):
        import personal_accounts
        if personal_accounts.request_allowed(self._personal_user(), self.command, urlparse(self.path).path):
            return True
        self._send_json({"ok": False, "error": "account_execution_unavailable",
                         "message": "独立执行环境尚未接通；不会借用站长的模型额度、文件或设备。"}, 403)
        return False

    def _send_account_session(self, result, token, *, logout=False):
        import personal_identity
        import personal_accounts
        body = json.dumps(result, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        secure = "" if _PUBLIC_ORIGIN is None and str(self.client_address[0]) in _LOOPBACKS and str(
            urlparse("//" + self.headers.get("Host", "")).hostname or "") in _LOOPBACKS else "; Secure"
        age = 0 if logout else personal_accounts.SESSION_TTL
        self.send_header("Set-Cookie", f"{personal_identity.COOKIE_NAME}={token}; Path=/; "
                         f"HttpOnly; SameSite=Strict; Max-Age={age}{secure}")
        self.end_headers()
        self.wfile.write(body)

    def _account_post(self):
        """Narrow public login/registration exceptions, after origin/body checks.

        No user-supplied uid, owner, execution flag, endpoint or key can alter the
        service environment. Secrets go only into the private account store.
        """
        import personal_accounts as accounts
        import personal_identity
        path = urlparse(self.path).path
        routes = {
            "/api/legion/auth/register", "/api/legion/auth/login", "/api/legion/auth/logout",
            "/api/legion/auth/password", "/api/legion/my/models", "/api/legion/my/models/delete",
        }
        if path not in routes:
            return False
        try:
            if not accounts.enabled():
                raise accounts.AccountError("account_settings_unavailable", 503)
            # Browser credential mutations require an explicit same-origin POST.
            # Host alone must not permit login-CSRF or saving a key via a form.
            if (not self.headers.get("Origin") or not self._same_origin()
                    or self.headers.get_content_type() != "application/json"
                    or getattr(self, "_request_ticket_identity", None)):
                raise accounts.AccountError("account_origin_required", 403)
            body = getattr(self, "_request_json_payload", None)
            if not isinstance(body, dict):
                raise accounts.AccountError("bad_request_body")
            if path.endswith("/register") or path.endswith("/login"):
                # These are credential exchange operations, not delegated user-id
                # calls. A stale browser cookie does not nominate the new subject.
                if set(body) - {"username", "password"}:
                    raise accounts.AccountError("unexpected_account_fields")
                fn = accounts.register if path.endswith("/register") else accounts.login
                result, token = fn(body.get("username"), body.get("password"))
                self._send_account_session(result | {"method": "password"}, token)
            elif path.endswith("/logout"):
                token = personal_identity.personal_token(
                    self.headers, str(getattr(self.server, "api_token", "") or ""))
                accounts.logout(token)
                self._send_account_session({"ok": True, "authenticated": False}, "", logout=True)
            else:
                if not self._bind_personal_request(body):
                    return True
                user = body["uid"]
                if path.endswith("/password"):
                    result, token = accounts.set_password(
                        user, body.get("password"), body.get("current_password", ""))
                    self._send_account_session(result | {"method": "password"}, token)
                elif path.endswith("/delete"):
                    self._send_json(accounts.delete_model(user, body.get("id")))
                else:
                    self._send_json(accounts.save_model(user, body))
        except accounts.AccountError as error:
            self._send_json({"ok": False, "error": error.code}, error.status)
        return True

    def _bind_personal_request(self, values: dict, *, allow_new: bool = False) -> bool:
        import personal_identity
        user = self._personal_user()
        if not user:
            # Do not enumerate whether a private session exists.
            code = 404 if urlparse(self.path).path in {"/api/sessions", "/api/legion/session"} else 401
            self._send_json({"error": "personal_auth_required"}, code)
            return False
        if getattr(self, "_request_ticket_identity", None):
            # The ticket protocol's top-level uid may be an old client placeholder.
            # Only replace that field; all resource/nested owner checks still run.
            values["uid"] = user
        if (not personal_identity.normalize_resource_aliases(values, urlparse(self.path).path)
                or not personal_identity.check_request(values, user, allow_new=allow_new)):
            self._send_json({"error": "personal_owner_mismatch"}, 403)
            return False
        values["uid"] = user
        return True

    def _file_owner_allowed(self, path: str) -> bool:
        import personal_identity
        query = parse_qs(urlparse(self.path).query)
        scope = getattr(self, "_shared_file_scope", None)
        user = "" if scope else self._personal_user()
        sid, share = scope or (str((query.get("sid") or [""])[0]), str((query.get("share") or [""])[0]))
        if personal_identity.file_allowed(path, user, sid, share):
            return True
        if ("text/html" in str(self.headers.get("Accept") or "")
                or self.headers.get("Sec-Fetch-Dest") in {"document", "iframe"}):
            self._send_artifact_notice(ARTIFACTS.artifact_access_notice(), 403)
            return False
        self._send_json({"error": "artifact_permission_denied"}, 403)
        return False

    def _project_access_allowed(self, context: dict, user_id: str, conversation_id: str) -> bool:
        """Recheck inherited roots too; an old canonical record is not a grant."""
        import personal_identity
        roots = [context[name] for name in ("repo_root", "workspace_root") if context.get(name)]
        if not roots:
            return True
        if (any(root != roots[0] for root in roots)
                or not personal_identity.authorized_project_path(
                    user_id, roots[0], conversation_id=conversation_id)):
            self._send_json({"error": "project_workspace_not_authorized"}, 403)
            return False
        return True

    def _export_bytes_match(self, path: str, body: bytes) -> bool:
        """Check the bytes about to be returned, before any preview wrapper."""
        export_root = os.path.realpath(PATHS.under("outputs", "exports"))
        if not os.path.realpath(path).startswith(export_root + os.sep):
            return True
        import personal_identity
        query = parse_qs(urlparse(self.path).query)
        scope = getattr(self, "_shared_file_scope", None)
        sid, share = scope or (str((query.get("sid") or [""])[0]),
                               str((query.get("share") or [""])[0]))
        expected = personal_identity.expected_export_hash(
            path, "" if scope else self._personal_user(), sid, share)
        if expected and hashlib.sha256(body).hexdigest() == expected:
            return True
        self._send_json({"error": "artifact_version_mismatch",
                         "reason": "导出字节与已登记版本不一致，未返回未验证的新内容。"}, 409)
        return False

    def _send_artifact_notice(self, body: bytes, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _require_authorized(self) -> bool:
        if self._authorized():
            return True
        self._send_json({"error": "forbidden", "reason": "same origin and existing personal token required"}, 403)
        return False

    def _authorize_mobile_request(self, path: str, payload: dict | None = None) -> bool:
        """Keep the verified personal subject; a cloud phone never falls back to Redroid."""
        if not self._require_authorized():
            return False
        import mobile_device
        try:
            mobile_device.authorize_cloud_owner(_uid_from_request(self) or "")
        except PermissionError:
            self._send_json({"ok": False, "error": "mobile_owner_forbidden"}, 403)
            return False
        if not mobile_device.cloud_configured():
            return True
        supported = {
            "GET": {"/api/legion/mobile/status", "/api/legion/mobile/screenshot"},
            "POST": {"/api/legion/mobile/ensure", "/api/legion/mobile/stop",
                     "/api/legion/mobile/action", "/api/legion/mobile/actions/batch"},
        }
        body = payload or {}
        actions = body.get("actions") if isinstance(body.get("actions"), list) else []
        # The old task action enters mobilerun, including from a batch. Reject
        # the whole batch before executing even its first otherwise-valid step.
        old_task = body.get("action") == "task" or any(
            isinstance(spec, dict) and spec.get("action") == "task" for spec in actions)
        if path not in supported.get(self.command, set()) or old_task:
            self._send_json({"ok": False, "provider": "agentbay",
                             "error": "mobile_operation_not_supported",
                             "reason": "云手机未接入旧 APK、应用安装或 mobilerun 任务接口"}, 409)
            return False
        return True

    def _mobile_screenshot_url(self, shot: dict, uid: str) -> str:
        import mobile_device
        path = str(shot.get("path") or "")
        if mobile_device.cloud_configured():
            import base64
            # The provider owns path/owner/no-follow/size checks. Keep the PNG
            # only in this authenticated response, never grant generic file access.
            png = mobile_device.read_cloud_screenshot(uid, path)
            return "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        return "/api/legion/artifact?path=" + quote(path)

    def _cors_origin(self) -> str:
        origin = str(self.headers.get("Origin") or "").strip()
        return origin if origin and self._same_origin() else ""

    # ── 响应辅助(照抄 agent_web 风格) ──────────────────────────────
    def _send_json(self, obj, code=200, *, cors=True):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        origin = self._cors_origin() if cors else ""
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Content-Length", str(len(body)))
        started = getattr(self, "_request_started", None)
        if started is not None:
            self.send_header("Server-Timing", f'app;dur={(time.perf_counter() - started) * 1000:.1f}')
        if getattr(self, "close_connection", False):
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _sse_open(self):
        # SSE 一次性流:发完 final/error 就关连接,让客户端(浏览器 ReadableStream)收到 EOF、
        # 循环正常结束。之前用 keep-alive 导致 final 已到但连接不关,前端 reader 一直等、
        # 按钮/状态卡转——真实用户体验 bug(E2E 回测发现)。
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()

    def _sse_send(self, event: str, data: dict, event_id: int | None = None) -> bool:
        try:
            if ARTIFACTS._is_private_artifact_event(data):
                return True  # Filter native/early references as well as collector-produced ones.
            ident = f"id: {int(event_id)}\n" if event_id is not None else ""
            payload = f"{ident}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            self.wfile.write(payload.encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False
        except Exception:
            import sys as _sys3, traceback as _tb3; _tb3.print_exc(file=_sys3.stderr)   # 2026-09-02 诊断:SSE 发送失败根因不再静默
            return False

    # 静默访问日志(避免污染上线控制台;真出错走 do_* 的兜底)
    def log_message(self, *a):
        pass

    def _preview_transport_allowed(self, *, opaque=False) -> bool:
        """Keep the existing host/proxy boundary, including for opaque frames."""
        import personal_identity
        hosts = self.headers.get_all("Host", [])
        origins = self.headers.get_all("Origin", [])
        if len(hosts) != 1 or len(origins) > 1 or (origins and not origins[0]):
            return False
        origin = origins[0] if origins else ""
        try:
            local_origin = personal_identity._origin_parts("http://" + hosts[0])
            if origin and not (opaque and origin == "null"):
                supplied_origin = personal_identity._origin_parts(origin)
                # This listener is HTTP. HTTPS exists only behind the explicit
                # public-origin configuration, never a caller's forwarded header.
                if _PUBLIC_ORIGIN is None and supplied_origin[:2] != local_origin[:2]:
                    return False
        except (ValueError, TypeError):
            return False
        return request_origin_allowed(
            str((self.client_address or ("",))[0]),
            str((self.server.server_address or ("",))[0]), hosts[0],
            "" if opaque and origin == "null" else origin)

    def _preview_error(self, error):
        payload = {"ok": False, "error_code": error.code}
        if error.code == "preview_unsupported":
            payload["unsupported"] = True
        self._send_json(payload, error.status, cors=False)

    def _artifact_preview_get(self):
        """Only a scoped, expiring read grant can cross the opaque-frame boundary."""
        self._redact_ticket_transport()  # Never exchange upstream identity tickets here.
        if not self._preview_transport_allowed(opaque=True):
            self._preview_error(ARTIFACTS.PreviewError("preview_origin_denied"))
            return
        try:
            parsed = urlparse(self.path)
            parts = parsed.path.split("/", 3)
            if len(parts) != 4 or not parts[3].startswith("home-outputs/exports/"):
                raise ARTIFACTS.PreviewError("preview_invalid_resource", 404)
            resource = ARTIFACTS.read_preview_resource(parts[2], "/" + parts[3])
            # A grant belongs to the parent's actual origin, not an arbitrary
            # Host accepted by another local listener or proxy configuration.
            import personal_identity
            parent = personal_identity._origin_parts(resource["preview_parent_origin"])
            requested = personal_identity._origin_parts(parent[0] + "://" + self.headers["Host"])
            if requested[:2] != parent[:2]:
                raise ARTIFACTS.PreviewError("preview_origin_denied")
            import mimetypes
            body, name = resource["body"], resource["name"]
            content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            download = parse_qs(parsed.query).get("download") == ["1"]
            html = name.lower().endswith((".html", ".htm"))
            if html and not download:
                body = ARTIFACTS.prepare_html_preview(
                    body, preview_nonce=resource["preview_nonce"],
                    preview_parent_origin=resource["preview_parent_origin"])
                content_type = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            # No credentials, no wildcard on ordinary APIs or denied reads.
            # The capability authorizes only the registered immutable snapshot.
            self.send_header("Access-Control-Allow-Origin", "*")
            if not download:
                # SVG/XHTML/XML may be navigated as active documents too.
                # Subresource loads retain their original bytes and MIME type.
                self.send_header("Content-Security-Policy",
                                 "sandbox allow-scripts allow-forms allow-modals allow-pointer-lock")
            if download:
                self.send_header("Content-Disposition",
                                 "attachment; filename=\"download\"; filename*=UTF-8''" + quote(name))
            self.end_headers()
            self.wfile.write(body)
        except ARTIFACTS.PreviewError as error:
            self._preview_error(error)
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception:
            self._preview_error(ARTIFACTS.PreviewError("preview_unavailable", 503))

    def _artifact_preview_post(self, path: str):
        """The authenticated parent records facts; the artifact cannot dispatch."""
        self._clear_request_identity()
        self._redact_ticket_transport()
        if (len(self.headers.get_all("Origin", [])) != 1
                or not self._preview_transport_allowed()):
            self.close_connection = True
            self._preview_error(ARTIFACTS.PreviewError("preview_origin_denied"))
            return
        if not self._require_authorized() or not self._account_request_allowed():
            self.close_connection = True
            return
        content_types = self.headers.get_all("Content-Type", [])
        lengths = self.headers.get_all("Content-Length", [])
        if (len(content_types) != 1
                or content_types[0].partition(";")[0].strip().lower() != "application/json"):
            self.close_connection = True
            self._preview_error(ARTIFACTS.PreviewError("preview_json_required", 415))
            return
        if (self.headers.get("Transfer-Encoding") or len(lengths) != 1
                or not lengths[0].isdigit() or int(lengths[0]) > 16384):
            self.close_connection = True
            self._preview_error(ARTIFACTS.PreviewError("preview_body_too_large", 413))
            return
        body = self._parse_json_payload()
        if body is None:
            return
        observe = path.endswith("-observation")
        fields = {"sid", "url", "observation_token", "observation"} if observe else {"sid", "url"}
        # The shared browser fetch wrapper pins uid; accept it only through
        # the existing owner checker below, never as the authenticated subject.
        if (set(body) - {"uid"} != fields
                or not all(isinstance(body.get(k), str) and body[k] for k in ("sid", "url"))
                or (observe and (not isinstance(body.get("observation_token"), str)
                                 or not isinstance(body.get("observation"), dict)))):
            self._preview_error(ARTIFACTS.PreviewError("preview_invalid_request", 400))
            return
        if not self._bind_personal_request(body, allow_new=False):
            return
        try:
            if observe:
                result = ARTIFACTS.record_preview_observation(
                    body["uid"], body["sid"], body["url"],
                    body["observation_token"], body["observation"])
            else:
                result = ARTIFACTS.create_preview_grant(
                    body["uid"], body["sid"], body["url"],
                    parent_origin=self.headers["Origin"])
            self._send_json(result, cors=False)
        except ARTIFACTS.PreviewError as error:
            self._preview_error(error)
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception:
            self._preview_error(ARTIFACTS.PreviewError("preview_unavailable", 503))

    def do_OPTIONS(self):
        if not self._same_origin():
            self._send_json({"error": "forbidden origin"}, 403)
            return
        self.send_response(204)
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Legion-Token, X-Legion-Ticket")
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.end_headers()

    # ── GET ────────────────────────────────────────────────────────
    def do_GET(self):
        self._request_started = time.perf_counter()
        try:
            if urlparse(self.path).path.startswith("/artifact-preview/"):
                self._artifact_preview_get()
                return
            if _PUBLIC_ORIGIN is not None and not self._same_origin():
                self._redact_ticket_transport()
                self.close_connection = True
                self._send_json({"error": "forbidden", "reason": "configured origin required"}, 403)
                return
            if not self._prepare_request_identity(
                    read_json=self.headers.get("Content-Length", "0") != "0"):
                return
            if not self._account_request_allowed():
                return
            self._do_GET_inner()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)
            except Exception:
                pass
        finally:
            self._clear_request_identity()

    def _do_GET_inner(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path.startswith("/shared/"):
            # Share scope is in the path, so browser-relative CSS/JS/images retain
            # the same grant. Every resource still needs an exact session reference.
            parts = parsed.path.split("/", 4)
            if (len(parts) != 5 or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", parts[2])
                    or not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", parts[3])
                    or not SESSIONS.load_shared_session(parts[2], parts[3])):
                self.send_response(404); self.end_headers(); return
            self._shared_file_scope = (parts[2], parts[3])
            resource = "/" + parts[4]
            roots = {"/outputs/": ARTIFACTS.OUTPUTS, "/home-outputs/": PATHS.under("outputs"),
                     "/media/": PATHS.media_dir(), "/desktop-outputs/": PATHS.under("users"),
                     "/auto-outputs/": ARTIFACTS.AUTO_SANDBOX_ROOT}
            for prefix, root in roots.items():
                if resource.startswith(prefix):
                    if prefix == "/media/":
                        self._serve_media(resource[len(prefix):], download=("1" in qs.get("download", [])))
                    else:
                        self._serve_output(resource[len(prefix):], root=root,
                                           download=("1" in qs.get("download", [])))
                    return
            self.send_response(404); self.end_headers(); return
        if parsed.path == "/api/legion/cloud_desktop/display/auth":
            # Caddy authorizes both noVNC HTTP resources and WebSocket upgrades here.
            # There are no client-supplied resource/uid arguments or proxy targets.
            if not self._require_authorized():
                return
            bound = {}
            if not self._bind_personal_request(bound):
                return
            import cloud_desktop
            if not cloud_desktop.display_authorized(bound["uid"]):
                self._send_json({"ok": False, "error": "desktop_display_forbidden"}, 403)
                return
            self._send_json({"ok": True})
            return
        if parsed.path.startswith("/api/"):
            # All personal APIs consume a verified user, including default-less
            # callers. Query uid is never the source of authentication.
            # 例外（2026-09-22）：成员目录公开——未登录用户渲染「指派哪家 Agent」需要它。
            # 隐私不靠 HTTP 鉴权兜底：entry_points（云真机等私有入口）在 harness_view
            # 数据层已要求 verified_user_id 匹配归属，无票请求拿到的是空入口列表；
            # 其余返回只是成员画像/公共运营统计。带票请求照常验证并拿到自己的入口。
            _PUBLIC_GET = {"/api/legion/harnesses"}
            bound = dict(qs)
            if parsed.path not in _PUBLIC_GET or self._personal_user():
                if not self._bind_personal_request(bound):
                    return
                qs = {key: value if isinstance(value, list) else [value] for key, value in bound.items()}
            if parsed.path == "/api/legion/auth/status":
                import personal_accounts
                import personal_identity
                token = personal_identity.personal_token(self.headers, str(getattr(self.server, "api_token", "") or ""))
                try:
                    info = personal_accounts.account_info(bound["uid"])
                except personal_accounts.AccountError:
                    # Account settings failure must not invalidate an existing
                    # owner credential or obscure their original history.
                    info = {"authenticated": True, "user_id": bound["uid"], "sso_verified": False,
                            "username": bound["uid"], "password_set": False,
                            "execution_enabled": not personal_accounts.settings_only(bound["uid"]),
                            "account_settings_available": False}
                method = ("meyo_ticket" if self._request_ticket_identity else
                          "password" if personal_accounts.resolve_token(token) else "existing_personal_token")
                self._send_json(info | {"method": method})
                return
            if parsed.path == "/api/legion/my/models":
                import personal_accounts
                try:
                    self._send_json(personal_accounts.models(bound["uid"]))
                except personal_accounts.AccountError as error:
                    self._send_json({"ok": False, "error": error.code}, error.status)
                return
        if parsed.path.startswith("/api/legion/mobile/") and not self._authorize_mobile_request(parsed.path):
            return
        if parsed.path == "/favicon.ico":
            # 无 favicon 会每次 404 污染浏览器 console(用户看到满屏红报错以为坏了)。返 204 空内容消除。
            self.send_response(204)
            self.send_header("Cache-Control", "public, max-age=604800")
            self.end_headers()
            return
        if parsed.path in ("/desktop_production.js", "/desktop_production.css",
                           "/game_workbench.js", "/game_workbench.css"):
            name = os.path.basename(parsed.path)
            target = os.path.join(WEB_DIR, name)
            try:
                body = open(target, "rb").read()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8" if name.endswith(".js") else "text/css; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path in ("/", "/index.html", "/legacy", "/legacy.html"):
            self._send_html(_read_index_html())  # 唯一前端 index_v2(v1 已删)
        elif parsed.path in ("/health", "/monitor/alive"):
            # fast=True:探针/首屏专用·绝不同步触发冷探活(进程刚起预热未完成时立即返 warming·
            # 端口秒连不卡·真实 runnable 等后台预热填好下次就有)。用户 2026-09-01 loop 揪出:
            # 旧 /health 首个请求撞冷缓存同步等 24s→"新开页面"卡死。
            h = LEGION.health(fast=True)
            # 不能只看"模块能 import"就说健康:真出答案还依赖 AIGC appid + 至少一家 CLI。
            ready = bool(h.get("ready_to_serve"))
            # 预热中(warming)不是故障·返 200 让前端知道"在启动"而非报错;真未就绪(非warming且缺依赖)才 503。
            code = 200 if (ready or h.get("warming")) else 503
            self._send_json({"ok": ready, **h}, code)
        elif parsed.path == "/api/legion/workspace" and not (qs.get("team_id") or qs.get("id")):
            # 用户配置和查看自己的:memory(md)/沙盒文件/pilot/云电脑是否在
            try:
                import my_workspace
                uid = qs.get("uid", ["default"])[0]
                self._send_json(my_workspace.workspace(uid))
            except Exception as e:
                self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        elif parsed.path == "/api/legion/cloud_desktop/workspace/projects":
            uid = qs.get("uid", ["default"])[0]
            try:
                limit = int(qs.get("limit", ["50"])[0])
            except ValueError:
                limit = 50
            self._send_json(DESKTOP_WORKSPACE.list_projects(uid, limit))
        elif parsed.path == "/api/legion/cloud_desktop/workspace/project":
            self._send_json(DESKTOP_WORKSPACE.get_project(
                qs.get("uid", ["default"])[0], qs.get("project_id", [""])[0]))
        elif parsed.path == "/api/legion/objective/project":
            self._send_json(OBJECTIVE_WORKSPACE.build_project_view(
                qs.get("uid", ["default"])[0], qs.get("project_id", [""])[0]))
        elif parsed.path == "/api/legion/cloud_desktop/workspace/task":
            self._send_json(DESKTOP_WORKSPACE.get_task(
                qs.get("uid", ["default"])[0], qs.get("project_id", [""])[0],
                qs.get("task_id", [""])[0]))
        elif parsed.path == "/api/legion/cloud_desktop/workspace/recoverable":
            self._send_json(DESKTOP_WORKSPACE.recoverable_tasks(qs.get("uid", ["default"])[0]))
        elif parsed.path == "/api/legion/cloud_desktop/quality":
            import desktop_quality_run
            result = desktop_quality_run.get(
                qs.get("uid", ["default"])[0], qs.get("project_id", [""])[0],
                qs.get("task_id", [""])[0])
            self._send_json(result, 200 if result.get("ok") else 404)
        elif parsed.path == "/api/legion/cloud_desktop/handoffs":
            self._send_json(HUMAN_HANDOFF.list_handoffs(
                qs.get("uid", ["default"])[0], qs.get("project_id", [""])[0],
                qs.get("task_id", [""])[0], qs.get("pending_only", ["0"])[0] == "1"))
        elif parsed.path == "/api/legion/cloud_desktop/software":
            self._send_json(DESKTOP_SOFTWARE.search(
                qs.get("uid", ["default"])[0], qs.get("q", [""])[0], qs.get("scene", [""])[0]))
        elif parsed.path == "/api/legion/cloud_desktop/software/history":
            self._send_json(DESKTOP_SOFTWARE.history(qs.get("uid", ["default"])[0]))
        elif parsed.path == "/api/legion/cloud_desktop/recipes":
            self._send_json(DESKTOP_RECIPE.list_recipes(qs.get("uid", ["default"])[0]))
        elif parsed.path == "/api/legion/evolve/config":
            # 自进化配置(用户 8-30 澄清:用户自己配置·不默认开启)。GET 读·POST 存
            try:
                import evolve_config
                uid = qs.get("uid", ["default"])[0]
                self._send_json({"ok": True, "config": evolve_config.load_config(uid)})
            except Exception as e:
                self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        elif parsed.path == "/api/legion/cloud_desktop/status":
            # 云端桌面状态(用户 8-30:装 cua 让'是否在'变✅)
            try:
                import cloud_desktop
                self._send_json(cloud_desktop.status(qs.get("uid", ["default"])[0]))
            except Exception as e:
                self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        elif parsed.path == "/api/legion/mobile/status":
            try:
                import mobile_device
                self._send_json(mobile_device.status(uid=_uid_from_request(self, qs=qs)))
            except Exception as e:
                self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        elif parsed.path == "/api/legion/mobile/screenshot":
            try:
                import mobile_device
                uid = _uid_from_request(self, qs=qs)
                shot = mobile_device.screenshot(uid=uid)
                if shot.get("ok"):
                    shot["url"] = self._mobile_screenshot_url(shot, uid)
                self._send_json(shot, 200 if shot.get("ok") else 503)
            except Exception as e:
                self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        elif parsed.path == "/api/legion/mobile/apps/search":
            self._send_json(MOBILE_APPS.search(qs.get("q", [""])[0],
                                               limit=int(qs.get("limit", ["20"])[0]),
                                               refresh=qs.get("refresh", ["0"])[0] == "1"))
        elif parsed.path == "/api/legion/mobile/apps/inventory":
            self._send_json(MOBILE_APPS.inventory())
        elif parsed.path == "/api/legion/mobile/tasks":
            self._send_json(MOBILE_TASKS.list_tasks(uid=qs.get("uid", ["default"])[0],
                                                     state=qs.get("state", [""])[0],
                                                     limit=int(qs.get("limit", ["100"])[0])))
        elif parsed.path == "/api/legion/mobile/task":
            self._send_json(MOBILE_TASKS.get(qs.get("id", [""])[0],
                                             uid=qs.get("uid", [""])[0]))
        elif parsed.path == "/api/legion/mobile/companion/inbox":
            self._send_json(MOBILE_COMPANION.inbox(
                qs.get("uid", ["default"])[0], include_read=qs.get("all", ["0"])[0] == "1"))
        elif parsed.path == "/api/legion/mobile/companion/summary":
            self._send_json(MOBILE_COMPANION.daily_summary(
                qs.get("uid", ["default"])[0], day=qs.get("day", [""])[0]))
        elif parsed.path == "/api/legion/mobile/companion/health":
            self._send_json(MOBILE_COMPANION.health(qs.get("uid", ["default"])[0]))
        elif parsed.path == "/api/legion/mobile/companion/preferences":
            self._send_json(MOBILE_COMPANION.preferences(qs.get("uid", ["default"])[0]))
        elif parsed.path == "/api/legion/cloud_desktop/install_guide":
            # 云端桌面装机向导(可复制的 pip/docker 命令+文档)
            try:
                import cloud_desktop
                self._send_json(cloud_desktop.install_guide(qs.get("uid", ["default"])[0]))
            except Exception as e:
                self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        elif parsed.path == "/api/legion/cloud_desktop/suggest":
            # 关键词路由建议(用户 8-30 P2c:brief 说 GUI 词→建议派云桌面·透明推荐不硬派)
            try:
                import cloud_desktop
                brief = qs.get("brief", [""])[0]
                self._send_json(cloud_desktop.suggest_for_brief(brief, qs.get("uid", ["default"])[0]))
            except Exception as e:
                self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        elif parsed.path == "/api/legion/cloud_desktop/session/open":
            # 云桌面会话:装完能'开一个云桌面'看见(用户 8-30 P2b)
            try:
                import cloud_desktop
                uid = qs.get("uid", ["default"])[0]
                self._send_json(cloud_desktop.session_open(uid))
            except Exception as e:
                self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        elif parsed.path in ("/api/legion/cloud_desktop/fs/list", "/api/legion/cloud_desktop/fs/sessions", "/api/legion/cloud_desktop/fs/projects", "/api/legion/cloud_desktop/fs/evolution", "/api/legion/cloud_desktop/fs/memory", "/api/legion/cloud_desktop/fs/personal"):
            uid = qs.get("uid", ["default"])[0]
            path = qs.get("path", [""])[0]
            fn = {"/api/legion/cloud_desktop/fs/list": DESKTOP_FS.list_files, "/api/legion/cloud_desktop/fs/sessions": DESKTOP_FS.list_sessions, "/api/legion/cloud_desktop/fs/projects": DESKTOP_FS.list_projects, "/api/legion/cloud_desktop/fs/evolution": DESKTOP_FS.list_evolution, "/api/legion/cloud_desktop/fs/memory": DESKTOP_FS.list_memory, "/api/legion/cloud_desktop/fs/personal": DESKTOP_FS.list_personal}[parsed.path]
            out = fn(uid, path)
            self._send_json(out, 200 if out.get("ok") else 400)
        elif parsed.path == "/api/legion/cloud_desktop/route":
            # 智能意图路由(学 Lumi):简单指令本机秒回·复杂任务派军团·带屏幕场景感知
            import cloud_desktop as _cd_router
            body = self._read_json_body()
            if body is None:
                return
            out = _cd_router.route_intent(
                str(body.get("text") or ""), str(body.get("uid") or "default"),
                action=str(body.get("action") or ""), params=body.get("params"))
            self._send_json(out, 200 if out.get("ok") else 400)
        elif parsed.path == "/api/legion/cloud_desktop/fs/session_archive":
            # 会话归档可视化:每个 ~/Meyo/sessions/<sid> 汇总(标题/时间/交付物清单)
            uid = qs.get("uid", ["default"])[0]
            try:
                limit = int(qs.get("limit", ["60"])[0])
            except ValueError:
                limit = 60
            out = DESKTOP_FS.session_archive(uid, limit)
            self._send_json(out, 200 if out.get("ok") else 400)
        elif parsed.path == "/api/legion/cloud_desktop/fs/read":
            uid = qs.get("uid", ["default"])[0]
            out = DESKTOP_FS.read_file(uid, qs.get("path", [""])[0])
            self._send_json(out, 200 if out.get("ok") else 400)
        elif parsed.path == "/api/legion/evolve/list":
            # 列进化记录(倒序·最新在前·可见 is_active)
            try:
                import evolve_config
                uid = qs.get("uid", ["default"])[0]
                self._send_json(evolve_config.list_evolutions(uid))
            except Exception as e:
                self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        elif parsed.path == "/api/legion/evolve/detail":
            # 看单次进化完整 diff
            try:
                import evolve_config
                uid = qs.get("uid", ["default"])[0]
                eid = qs.get("id", [""])[0]
                self._send_json(evolve_config.get_evolution(uid, eid))
            except Exception as e:
                self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        elif parsed.path == "/api/legion/wiki/view":
            # WikiSkill 持久知识库:看 index/log/skill-impact/patterns(用户「可查看」)
            try:
                import evolve_config
                uid = qs.get("uid", ["default"])[0]
                self._send_json(evolve_config.wiki_view(uid))
            except Exception as e:
                self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        elif parsed.path == "/api/legion/wiki/pattern":
            # 看某个 pattern 详情
            try:
                import evolve_config
                uid = qs.get("uid", ["default"])[0]
                name = qs.get("name", [""])[0]
                self._send_json(evolve_config.wiki_pattern(uid, name))
            except Exception as e:
                self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        elif parsed.path == "/api/legion/me":
            # 因人而异:摘要走轻量快照；detail=1 保留完整旧视图。
            try:
                uid = qs.get("uid", ["default"])[0]
                if (qs.get("detail") or ["0"])[0] in ("1", "true", "yes"):
                    import user_view
                    out = user_view.my_view(uid)
                else:
                    import performance_views
                    out = performance_views.mine_summary(uid)
                self._send_json(out)
            except Exception as e:
                self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        elif parsed.path == "/api/legion/session-index":
            # 首屏/最近产物一次拿齐，替代前端 list + N 个完整 session 请求。
            try:
                import performance_views
                uid = qs.get("uid", ["default"])[0]
                limit = int((qs.get("limit") or ["12"])[0])
                include = (qs.get("artifacts") or ["1"])[0] not in ("0", "false", "no")
                self._send_json(performance_views.session_index(uid, limit, include))
            except Exception as e:
                self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        elif parsed.path == "/api/legion/my-files":
            try:
                import performance_views
                uid = qs.get("uid", ["default"])[0]
                limit = int((qs.get("limit") or ["50"])[0])
                self._send_json(performance_views.file_index(uid, limit))
            except Exception as e:
                self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        elif parsed.path == "/api/legion/catalog":
            # 统一目录(harness届OpenRouter):agents三类/skills/tools/functions 全带分类·前端浏览入口
            try:
                import catalog_api
                self._send_json(catalog_api.catalog())
            except Exception as e:
                self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        elif parsed.path == "/api/legion/harnesses":
            # 成员目录走 SWR 快照：首次先渲染 manifest 画像，后台补探活/模型/实测状态。
            import harness_view
            import personal_accounts
            user = self._personal_user()
            if not user or personal_accounts.settings_only(user):
                # Dropping a cookie must never make a caller's uid authoritative.
                self._send_json(harness_view.public_snapshot())
            else:
                force = (qs.get("force") or ["0"])[0] in ("1", "true", "yes")
                self._send_json(harness_view.snapshot(user, force=force, verified_user_id=user))
        elif parsed.path == "/api/legion/models":
            # 某方向可选模型清单(供切换/固定)+ 当前用户偏好
            direction = (qs.get("direction") or ["programming"])[0]
            harness_key = (qs.get("harness") or [""])[0]
            self._send_json({**MODELS.list_models(direction, harness_key=harness_key),
                             "prefs": MODELS.get_prefs(),
                             "directions": MODELS.all_directions()})
        elif parsed.path == "/api/legion/models/catalog":
            # KM 2781925462 真测89款·按家族分组(高级探索·不塞普通模型下拉·不含appid)
            self._send_json(MODELS.verified_catalog())
        elif parsed.path == "/api/legion/models/discovery":
            # 自动发现 models.json+KM catalog 新模型/新厂商/更高代→标NEW
            self._send_json(MODELS.discover_new_models())
        elif parsed.path == "/api/legion/install/registry":
            # 安装注册中心:全部可装项(装没装/共享/场景)· ?scene= 按场景筛。
            # summary=1 给导航摘要卡，避免只显示一个数字却下载整份 100KB+ 清单。
            scene = (qs.get("scene") or [""])[0].strip() or None
            data = INSTALL.scan(scene)
            if (qs.get("summary") or ["0"])[0] in ("1", "true", "yes"):
                data = {k: data.get(k) for k in ("summary", "scene") if k in data}
            self._send_json(data)
        elif parsed.path == "/api/legion/external-agents":
            import external_agent_registry as external_agents
            self._send_json(external_agents.probe())
        elif parsed.path == "/api/legion/external-agents/updates":
            import external_agent_registry as external_agents
            key = (qs.get("key") or [""])[0]
            self._send_json(external_agents.check_updates(key))
        elif parsed.path == "/api/legion/external-agents/promotion-review":
            # 外聘转正评估(用户 2026-09-02 定·固定化机制):数据驱动判谁够格写专门 adapter
            # (战绩/场景/独门能力三层)·纯读账本零副作用·不自动改代码。
            try:
                import promotion_review
                self._send_json(promotion_review.review((qs.get("key") or [""])[0]))
            except Exception as exc:
                self._send_json({"ok": False, "error": f"{type(exc).__name__}:{exc}"})
        elif parsed.path == "/api/legion/external-agents/auto-update":
            # 无人值守自动更新状态(开关/上次运行/窗口);POST 端点可 force 立即跑一轮
            import external_agent_registry as external_agents
            self._send_json(external_agents.auto_update_status())
        elif parsed.path == "/api/legion/install/discover":
            # 彻底盘点:自动发现仓库里的开源agent/项目/skill包(只读·不自动装)
            self._send_json(INSTALL.discover_repo())
        elif parsed.path == "/api/legion/install/search_sources":
            # 全部搜索源(几十个·含friday/学术/社交/存档·免费or需key·领域分组)
            # P1-7 附用户开关偏好(搜索源自定义),前端据此渲染开/关
            data = INSTALL.discover_search_sources()
            try:
                import search_prefs
                data["prefs"] = search_prefs.get((qs.get("uid") or ["default"])[0])
            except Exception:
                data["prefs"] = {"disabled": []}
            self._send_json(data)
        elif parsed.path == "/api/legion/install/environment":
            # 全域发现本机已装:pip顶层/npm全局/brew/GitHub仓库(用户'把所有装的注册好')
            force = (qs.get("force") or ["0"])[0] in ("1", "true", "yes")
            self._send_json(INSTALL.discover_environment(force=force))
        elif parsed.path == "/api/legion/capabilities":
            # 各能力源独立 SWR；慢生态探针不能堵住整个能力总线。
            import capability_view
            self._send_json(capability_view.snapshot())
        elif parsed.path == "/api/legion/capabilities/health":
            import capability_view
            self._send_json(capability_view.health())
        elif parsed.path == "/api/legion/upgrades":
            # 官方军团升级检测:有没有新版本·changelog·待装项(按用户记状态)
            uid = (qs.get("uid") or [None])[0]
            self._send_json(INSTALL.check_upgrades(uid))
        elif parsed.path == "/api/legion/activity":
            # 军团动态:升级记录按天聚合·可持续查看·每日最多提醒一次
            uid = (qs.get("uid") or [None])[0]
            self._send_json(INSTALL.get_activity_feed(uid))
        elif parsed.path == "/api/legion/tools":
            # 工具体检:军团各家 CLI + 公用工具就绪 + 缺的安装建议(用户"该装都装·有个数")
            self._send_json(LEGION.tool_inventory())
        elif parsed.path == "/api/legion/frontier-benchmarks":
            # 具名世界级参照只提供比较方向；同题证据和双盲评仍由 success_auditor 强制。
            import frontier_benchmarks
            self._send_json(frontier_benchmarks.catalog())
        elif parsed.path == "/api/legion/toolbox":
            # 统一工具箱清册:全仓 skill + 代码工具 + 军团自产,可按能力筛(?cap=video_gen)
            #   ?force=1 强制重扫;默认读缓存(1h)。用户"一个可查看的工具箱·彼此共享经验"。
            force = (qs.get("force") or ["0"])[0] in ("1", "true", "yes")
            cap = (qs.get("cap") or [""])[0].strip()
            q = (qs.get("q") or [""])[0].strip()
            summary_only = ((qs.get("summary") or ["0"])[0] in ("1", "true", "yes")
                            and not q and not cap)
            # 摘要必须是纯快照读：无快照返 warming 并后台重建，绝不在用户请求线程全仓冷扫。
            if summary_only:
                tb = TOOLBOX.toolbox_summary()
                self._send_json(tb)
                return
            if force:
                tb = TOOLBOX.scan_toolbox(force=True)
            else:
                limit = max(1, min(500, int((qs.get("limit") or ["50"])[0] or 50)))
                offset = max(0, int((qs.get("offset") or ["0"])[0] or 0))
                tb = TOOLBOX.toolbox_page(limit=limit, offset=offset, capability=cap)
            # 新功能协调(2026-08-30):附 harness 工具总线真直调/派单借用统计,让前端展示新接入是否真能用
            try:
                import harness_tool_bus
                tb["tool_bus"] = harness_tool_bus.all_tools().get("summary", {})
            except Exception:
                tb["tool_bus"] = {}
            if q:
                # 搜索需要全量候选；分页快照只用于无查询的首屏浏览。
                tb = TOOLBOX.scan_toolbox(force=False)
                # ?q= 自由文本搜索(loop dogfood 揪出:前端 catFnSearch 发 ?q= 但后端只认 ?cap=
                # →忽略 q 返回全部 2598 项·前端 slice(0,15) 拿到不对口的头部→"搜了像没搜")。
                # 用 legion_tool 的通用 BM25(依据 Hermes tool_search)排序·不写关键词枚举(纲领:
                # 给模型/BM25 初筛·不在父层写意图词表)。中↔英语义匹配交模型·此处只做字面初筛。
                try:
                    import legion_tool
                    ranked = legion_tool._rank_bm25(tb["items"], q)
                    items = [it for _score, it in ranked][:50]
                except Exception:
                    items = []
                self._send_json({"summary": tb["summary"], "items": items,
                                 "tool_bus": tb.get("tool_bus", {}),
                                 "query": q, "match": bool(items), "scanned_at": tb["scanned_at"]})
            elif cap:
                items = [it for it in tb["items"] if cap in it["capability_tags"]]
                self._send_json({"summary": tb["summary"], "items": items,
                                 "tool_bus": tb.get("tool_bus", {}),
                                 "filtered_by": cap, "scanned_at": tb["scanned_at"]})
            else:
                self._send_json(tb)
        elif parsed.path == "/api/legion/media_tools":
            # 军团可真执行的多媒体工具清单(生图/识图/读PDF/视频)· 供灵感墙 + 任意家调用
            self._send_json({"tools": MEDIA.list_media_tools()})
        elif parsed.path == "/api/legion/skill_factory/search":
            # Skill 工厂:多源发现(Meyo/skills.sh/skillfish)。先查已装再发现·有现成先用(纲领:先查Skill)
            q = (qs.get("q") or [""])[0]
            uid = (qs.get("uid") or ["default"])[0]
            self._send_json(SKILLFACTORY.search(q, uid=uid) if q else {"ok": False, "error": "缺 q"})
        elif parsed.path == "/api/legion/skill_factory/active":
            # 已装 Skill 清单(用户隔离):任务现场可见"我军团装了哪些能力"
            uid = (qs.get("uid") or ["default"])[0]
            self._send_json(SKILLFACTORY.list_active(uid=uid))
        elif parsed.path == "/api/legion/trace":
            # 真 trace 查看:打开某家跑过的 ckpt,还原工具链+访问URL+思考(替死写演示)
            tp = (qs.get("path") or [""])[0]
            self._send_json(TRACE.read_trace(tp))
        elif parsed.path == "/api/legion/subscribe_suggest":
            # 信息流(不硬塞):判断这个问题是否值得变订阅页,值得才追问用户
            q = (qs.get("q") or [""])[0]
            self._send_json(LEGION.suggest_subscription(q))
        elif parsed.path == "/api/legion/inspire":
            # 动态运营化"新对话灵感墙"(用户 2026-08-30 截图#16):个性化+时新的可玩任务卡·点击就玩
            uid = (qs.get("uid") or ["default"])[0]
            try:
                n = int((qs.get("n") or ["6"])[0])
            except Exception:
                n = 6
            try:
                import inspire as INSPIRE
                self._send_json(INSPIRE.suggest(uid, n))
            except Exception as exc:
                self._send_json({"ok": False, "cards": [], "error": f"{type(exc).__name__}: {exc}"}, 200)
        elif parsed.path == "/api/legion/pilot":
            # "持续运行直到完成"按钮的设置(说人话·每用户可覆盖·全军团共享纲领)
            uid = (qs.get("uid") or ["default"])[0]
            self._send_json(LEGION.pilot_settings(uid))
        elif parsed.path == "/api/legion/captain":
            uid = (qs.get("uid") or ["default"])[0]
            try:
                import captain_preferences as _cp
                self._send_json({**_cp.load(uid), "available": LEGION._captain_candidates()})
            except Exception as exc:
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        elif parsed.path == "/api/legion/opencli/doctor":
            # OpenCLI 就绪自检(装没装·浏览器桥通不通·用户goal第一步点名的工具)
            self._send_json(OPENCLI.doctor())
        elif parsed.path == "/api/legion/experience":
            # 群体经验飞轮(用户点名"军团三内核之一:群体互相进化")·此前 legion_memory 无 HTTP 暴露·
            # 让前端看到军团沉淀了什么/搜历史经验/展示置信度+采纳票→可视化飞轮。
            try:
                import legion_memory as _lm
                q = (qs.get("q") or [""])[0].strip()
                uid = (qs.get("uid") or ["default"])[0]
                top = int((qs.get("top") or ["30"])[0])
                if q:
                    # 搜索命中经验
                    r = _lm.search(q, top=top, user_id=uid)
                    self._send_json({"query": q, "total": r.get("recalled", 0),
                                     "experiences": r.get("experiences", [])})
                else:
                    # 摘要 + 最近经验(直接读全量取 top N;经验层本身按时序 append)
                    st = _lm.stats()
                    recs = _lm._load()[-top:]
                    recs.reverse()
                    self._send_json({"total": st.get("total", 0),
                                     "by_source": st.get("by_source", {}),
                                     "available": st.get("available", False),
                                     "experiences": recs})
            except Exception as exc:
                self._send_json({"error": f"{type(exc).__name__}: {exc}",
                                 "total": 0, "experiences": []}, 200)
        elif parsed.path.startswith("/media/"):
            # 军团产出的媒体文件(图片等)· 只放行 ~/.meyo/legion/media 内,防路径穿越
            self._serve_media(parsed.path[len("/media/"):], download=("1" in (qs.get("download") or [])))
        elif parsed.path.startswith("/outputs/"):
            # harness生成的代码/表格/PDF/压缩包等交付物·安全限定service/outputs
            self._serve_output(parsed.path[len("/outputs/"):], download=("1" in (qs.get("download") or [])))
        elif parsed.path.startswith("/home-outputs/"):
            # 家目录 ~/.meyo/legion/outputs 的交付物(codex 等家持久工作目录产物·路径穿越安全)
            self._serve_output(parsed.path[len("/home-outputs/"):], root=PATHS.under("outputs"), download=("1" in (qs.get("download") or [])))
        elif parsed.path.startswith("/auto-outputs/"):
            # auto 自研家 per-user sandbox 的交付物(~/.meyo/users/...·路径穿越安全)
            self._serve_output(parsed.path[len("/auto-outputs/"):], root=ARTIFACTS.AUTO_SANDBOX_ROOT, download=("1" in (qs.get("download") or [])))
        elif parsed.path.startswith("/desktop-outputs/"):
            # 云真机容器交付物(users/<uid>/desktop/Meyo=容器 /root/Meyo·2026-09-02 跨域产物收集)
            self._serve_output(parsed.path[len("/desktop-outputs/"):], root=os.path.realpath(PATHS.under("users")), download=("1" in (qs.get("download") or [])))
        elif parsed.path.startswith("/mobile-evidence/"):
            # 云手机任务、截图、轨迹、宏和 App 生命周期证据；仅放行统一 mobile 根。
            self._serve_output(parsed.path[len("/mobile-evidence/"):], root=os.path.realpath(PATHS.under("mobile")), download=("1" in (qs.get("download") or [])))
        elif parsed.path == "/api/verification/evidence/file":
            # 统一机器证据日志与截图；路径仅相对于受控 evidence 根。
            import evidence_schema
            self._serve_output((qs.get("path") or [""])[0], root=os.path.realpath(evidence_schema.default_store_root()), download=("1" in (qs.get("download") or [])))
        elif parsed.path == "/api/verification/exam/file":
            # 终极大题产物只允许从对应题目的受控交付目录读取。
            import verification_center
            try:
                target = verification_center.exam_file(int((qs.get("seq") or ["0"])[0]),
                                                       (qs.get("path") or [""])[0])
            except (TypeError, ValueError):
                target = None
            if target:
                self._serve_output(target.name, root=str(target.parent),
                                   download=("1" in (qs.get("download") or [])))
            else:
                self._send_json({"error": "exam artifact not found"}, 404)
        elif parsed.path == "/convert":
            # 交付物后端渲染兜底(用户2026-09-01·CDN不通时前端回退到这):按 to 参数把
            # excel/csv→html表格·md→html·pdf→首页png·让内网无CDN也能容器内直渲染。
            # rel 是产物相对路径(同 /outputs 系列·多根回退+路径穿越安全)。
            self._serve_convert((qs.get("path") or [""])[0], (qs.get("to") or ["auto"])[0])
        elif parsed.path == "/api/legion/cron":
            # cron 定时任务列表(默认过滤已过期·?all=1 含过期;按 uid 过滤防跨用户泄漏)
            inc = (qs.get("all") or ["0"])[0] in ("1", "true", "yes")
            _uid = (qs.get("uid") or [""])[0]
            self._send_json({"jobs": CRON.list_jobs(include_expired=inc,
                                                    user_id=(_uid or None))})
        elif parsed.path == "/api/legion/cron/history":
            # 定时任务运行历史(网页恢复"上次跑成什么样"的真账本)
            try:
                import cron_dispatch
                rows = []
                if os.path.isfile(cron_dispatch._HISTORY):
                    try:
                        rows = json.load(open(cron_dispatch._HISTORY, encoding="utf-8"))
                    except Exception:
                        rows = []
                limit = 60
                try:
                    limit = int((qs.get("limit") or ["60"])[0])
                except ValueError:
                    pass
                self._send_json({"ok": True, "history": rows[-max(1, limit):]})
            except Exception as e:
                self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        elif parsed.path == "/api/legion/crawl/history":
            # 当前用户全部历史订阅：只遍历其可见 session，再读对应 crawl 文件，防跨用户泄漏。
            uid = (qs.get("uid") or ["default"])[0]
            history = []
            for session in SESSIONS.list_sessions(limit=200, user_id=uid):
                sid = str(session.get("sid") or "")
                for source in CRAWLER.list_sources(sid):
                    history.append({
                        "sid": sid, "session_title": session.get("title") or "(空会话)",
                        "session_updated": session.get("updated", 0),
                        "url": source.get("url", ""), "note": source.get("note", ""),
                        "added": source.get("added"), "last_crawled": source.get("last_crawled"),
                        "crawl_count": source.get("crawl_count", 0),
                    })
            history.sort(key=lambda item: item.get("added") or item.get("session_updated") or 0,
                         reverse=True)
            self._send_json({"uid": uid, "sources": history, "total": len(history)})
        elif parsed.path == "/api/legion/crawl":
            # session 订阅源列表(持续爬取)
            sid = (qs.get("sid") or [""])[0]
            self._send_json({"sid": sid, "sources": CRAWLER.list_sources(sid)})
        elif parsed.path.startswith("/api/verification/"):
            # 跨 UID / 原生产物只读入口独立放在绿区模块；该模块统一执行 localhost 门禁。
            import verification_http
            verification_http.handle_get(self, parsed.path, qs)
        elif parsed.path == "/api/sessions":
            # 觅游用户身份：ticket 换真实 uid（不再信任 query 自称的 uid）
            uid = _uid_from_request(self, qs=qs)
            if not uid:
                self._send_json({"error": "unauthorized",
                                 "reason": "登录状态已失效，请刷新页面重新进入"}, 401)
                return
            self._send_json({"whoami": uid, "sessions": SESSIONS.list_sessions(user_id=uid)})
        elif parsed.path == "/api/legion/session":
            # 读单 session 完整历史(前端切换session后回填对话·多轮持续上下文)·按uid隔离
            # 觅游用户身份：ticket 换真实 uid
            sid = (qs.get("sid") or [""])[0]
            uid = _uid_from_request(self, qs=qs)
            if not uid:
                self._send_json({"error": "unauthorized",
                                 "reason": "登录状态已失效，请刷新页面重新进入"}, 401)
                return
            sess = SESSIONS.load_session(sid, user_id=uid)
            self._send_json(sess or {"error": "session not found"}, 200 if sess else 404)
        elif parsed.path == "/api/legion/team":
            # 持久团队状态(Task DAG/成员/attempt/mailbox)·供指挥图恢复和重启回放
            if not self._require_authorized():
                return
            # 觅游用户身份：ticket 换 uid + team 归属校验
            _team_uid = _uid_from_request(self, qs=qs)
            if not _team_uid:
                self._send_json({"error": "unauthorized",
                                 "reason": "登录状态已失效，请刷新页面重新进入"}, 401)
                return
            team_id = (qs.get("team_id") or qs.get("id") or [""])[0]
            _owner = str((TEAMS.load(team_id) or {}).get("user_id") or "")
            if _owner and _owner != _team_uid:
                self._send_json({"error": "forbidden",
                                 "reason": "该任务不属于当前用户"}, 403)
                return
            TEAMS.recover_expired_attempts(team_id)
            team = TEAMS.load(team_id)
            if team:
                team["recoverable_tasks"] = TEAMS.recoverable_tasks(team_id)
                # 仅展开本次磁盘快照中已完成任务的引用，不接受调用方的 path。
                # read_text 已有存储根/哈希校验；这里进一步限定到本 team/task，
                # 并拒绝读上限导致的截断。只补 HTTP 响应，不回写权威状态。
                import artifact_store
                from pathlib import Path
                for task_id, task in (team.get("tasks") or {}).items():
                    result = task.get("result") or {}
                    if task.get("status") != "done" or result.get("status") != "done":
                        continue
                    ref = result.get("final_ref")
                    if not ref:
                        continue  # 旧内联全文照常返回；绝不把 final_preview 提升为全文。
                    result["final"] = ""
                    try:
                        root = artifact_store.ROOT.resolve()
                        folder = (root / artifact_store._safe(team["team_id"])
                                  / artifact_store._safe(task_id)).resolve()
                        path = Path(ref["path"]).resolve()
                        if not folder.is_relative_to(root) or path.parent != folder:
                            raise ValueError("result reference outside task")
                        text = artifact_store.read_text(ref)
                        size = len(text.encode("utf-8"))
                        if size != path.stat().st_size or (ref.get("bytes") is not None
                                                          and size != int(ref["bytes"])):
                            raise ValueError("incomplete result reference")
                        result["final"] = text
                    except (OSError, ValueError, TypeError, KeyError):
                        result["final_read_error"] = "完整结果引用暂不可读取"
            self._send_json(team or {"error": "team not found"}, 200 if team else 404)
        elif parsed.path == "/api/legion/workspace":
            team_id = (qs.get("team_id") or qs.get("id") or [""])[0]
            view = WORKSPACE.build(team_id)
            self._send_json(view, 200 if view.get("ok") else 404)
        elif parsed.path == "/api/legion/approvals":
            if not self._require_authorized():
                return
            team_id = (qs.get("team_id") or [""])[0]
            pending_only = (qs.get("pending_only") or ["0"])[0] in ("1", "true", "yes")
            result = APPROVALS.list_approvals(team_id, include_terminal=not pending_only)
            self._send_json(result, 200 if result.get("ok") else 404)
        elif parsed.path == "/api/legion/team/messages":
            team_id = (qs.get("team_id") or [""])[0]
            recipient = (qs.get("recipient") or ["commander"])[0]
            team = TEAMS.load(team_id)
            if not team:
                self._send_json({"error": "team not found"}, 404)
            else:
                self._send_json({"team_id": team_id, "recipient": recipient,
                                 "messages": TEAMS.read_messages(team_id, recipient)})
        elif parsed.path == "/api/legion/session/teams":
            # 会话同步(2026-09-07实锤):其它客户端(CLI/API/另一页面)往同一会话派单,
            # 已打开的页面一无所知——openSession 一次性拉取·SSE 只覆盖本页发起的派单,
            # 用户在另一入口重放/补单后页面看不到任何过程。本端点让前端发现该会话
            # 正在跑的 team 实时接入;已完成的 turn 由前端拉 session 补渲染。
            sid = (qs.get("sid") or [""])[0]
            if not sid:
                self._send_json({"error": "missing sid"}, 400)
                return
            try:
                import team_state as _ts
                _teams = _ts.list_by_session(sid, limit=5, user_id=(qs.get("uid") or [""])[0])
            except Exception:
                _teams = []
            self._send_json({"ok": True, "sid": sid, "teams": _teams})
        elif parsed.path == "/api/legion/events":
            # 断线续播：JSON 增量读取与标准 SSE 共用同一持久事件源。浏览器 EventSource 可带
            # Last-Event-ID 自动续接；fetch 客户端也可传 after。?stream=1 返回 SSE 快照。
            # 觅游用户身份：ticket 换 uid + team 归属校验（team.user_id 必须匹配）
            _ticket_uid = _uid_from_request(self, qs=qs)
            if not _ticket_uid:
                self._send_json({"error": "unauthorized",
                                 "reason": "登录状态已失效，请刷新页面重新进入"}, 401)
                return
            team_id = (qs.get("team_id") or [""])[0]
            _team_owner = ""
            try:
                _team_snapshot = TEAMS.load(team_id) or {}
                _team_owner = str(_team_snapshot.get("uid")
                                  or _team_snapshot.get("user_id") or "")
            except Exception:
                _team_owner = ""
            if team_id and _team_owner and _team_owner != _ticket_uid:
                self._send_json({"error": "forbidden",
                                 "reason": "该任务不属于当前用户"}, 403)
                return
            raw_after = (qs.get("after") or [self.headers.get("Last-Event-ID", "0")])[0]
            try:
                after = int(raw_after or 0)
            except ValueError:
                after = 0
            if not team_id or TEAMS.load(team_id) is None:
                self._send_json({"error": "team not found"}, 404)
            else:
                evs = TEAMS.read_events(team_id, after=after)
                last_id = evs[-1]["event_id"] if evs else after
                evs = [rec for rec in evs if not ARTIFACTS._is_private_artifact_event(rec.get("data") or {})]
                if (qs.get("stream") or ["0"])[0] in ("1", "true", "yes"):
                    self._sse_open()
                    for rec in evs:
                        if not self._sse_send(rec.get("event", "step"), rec.get("data") or {},
                                              event_id=rec.get("event_id")):
                            break
                else:
                    self._send_json({"team_id": team_id, "after": after,
                                     "last_event_id": last_id, "events": evs})
        elif parsed.path == "/api/legion/export":
            # 导出为单文件可部署 HTML(写本机·返回路径·用户"他们还想部署")
            sid = (qs.get("sid") or [""])[0]
            # Existing exporter has no owner parameter and writes a new public
            # file. Use the authorized session/share download surface instead.
            self._send_json({"sid": sid, "url": "/s/" + quote(str(sid)) + "?download=1"})
        elif parsed.path.startswith("/s/"):
            # 每个 session 一个可分享的交付物网页(?download=1 下载单文件·可部署)
            sid = parsed.path[len("/s/"):].strip("/")
            import personal_identity
            uid = self._personal_user()
            share_token = (qs.get("share") or [""])[0]
            session = personal_identity.session_grant(sid, uid, share_token)
            html = (SESSIONS.render_session_html(sid, user_id=str(session.get("user_id") or "default"),
                                                share_token=share_token) if session else None)
            if html is not None and share_token and SESSIONS.load_shared_session(sid, share_token):
                # Scope embedded asset links to this share grant, never a user uid.
                import html as _html
                prefix = "/shared/" + quote(sid, safe="") + "/" + quote(share_token, safe="")
                for artifact in personal_identity.artifact_rows(session):
                    url = str(artifact.get("url") or "")
                    if url.startswith("/") and not url.startswith("//"):
                        scoped = prefix + url
                        html = html.replace(_html.escape(url, quote=True), _html.escape(scoped, quote=True))
            if html is None:
                self.send_response(404)
                self.end_headers()
            elif (qs.get("download") or ["0"])[0] in ("1", "true", "yes"):
                # 下载单文件:媒体内联成 data URI,离开服务器/部署到静态托管也能显示图
                html, _ = SESSIONS._inline_media(html)
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="legion-{sid}.html"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send_html(html)
        elif parsed.path == "/api/legion/gateway":
            # 网关就绪 + 降级建议:网关通不通·当下该用哪个模型/appid(?probe=1 真探网关·慢)
            probe = (qs.get("probe") or ["0"])[0] in ("1", "true", "yes")
            self._send_json(LEGION.gateway_status(probe=probe) if probe
                            else {"gateway_url": "https://aigc.sankuai.com",
                                  "fallback_model": LEGION.select_usable_model("programming")})
        else:
            self.send_response(404)
            self.end_headers()

    # ── POST ───────────────────────────────────────────────────────
    def _is_multipart_upload(self) -> bool:
        return (urlparse(self.path).path == "/api/legion/upload"
                and str(self.headers.get("Content-Type") or "").partition(";")[0]
                .strip().lower() == "multipart/form-data")

    def do_POST(self):
        self._request_started = time.perf_counter()
        try:
            preview_path = urlparse(self.path).path
            if preview_path in {"/api/legion/artifact/preview",
                                "/api/legion/artifact/preview-observation"}:
                self._artifact_preview_post(preview_path)
                return
            if not self._same_origin():
                self._redact_ticket_transport()
                self.close_connection = True
                self._send_json({"error": "forbidden", "reason": "same origin required"}, 403)
                return
            account_path = urlparse(self.path).path
            import personal_accounts
            if (personal_accounts.enabled()
                    and account_path.startswith(("/api/legion/auth/", "/api/legion/my/models"))):
                lengths = self.headers.get_all("Content-Length", [])
                if (self.headers.get("Transfer-Encoding") or len(lengths) > 1
                        or (lengths and (not lengths[0].isdigit() or int(lengths[0]) > 16384))):
                    self.close_connection = True
                    self._send_json({"error": "account_body_too_large"}, 413)
                    return
            # Only the upload route owns multipart. Other routes still parse
            # body identity; personal account exchanges retain their own limit.
            if not self._prepare_request_identity(read_json=not self._is_multipart_upload()):
                return
            if self._account_post():
                return
            if not self._require_authorized():
                self.close_connection = True  # An unauthenticated multipart body is unread.
                return
            if not self._account_request_allowed():
                return
            self._do_POST_inner()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)
            except Exception:
                pass
        finally:
            self._clear_request_identity()

    # ── 多媒体:上传 + 静态服务 ────────────────────────────────────
    def _serve_media(self, name: str, download: bool = False):
        """serve ~/.meyo/legion/media 或 uploads 内的媒体文件(只按 basename·防穿越)。

        兜底(用户2026-08-30实测下载404根因):历史会话把 workdir 产物的 url 错存成 /media/<name>,
        但文件其实在 home-outputs 的某个 workdir 里。media/uploads 找不到时,去 home-outputs 递归找
        同名文件(有界扫描防慢),让历史会话的下载也能成功。新会话已用 _url_for 存对前缀。
        """
        import mimetypes
        from urllib.parse import quote, unquote
        name = unquote(name)  # 中文文件名 URL 编码解码(否则匹配不上真实文件名→404)
        if "/" in name:
            # Newly uploaded files are user-scoped; keep legacy basename lookup
            # for historical artifacts, which still requires an ownership grant.
            if not re.fullmatch(r"personal/[0-9a-f]{64}/[A-Za-z0-9_.-]+", name):
                self.send_response(404); self.end_headers(); return
            self._serve_output(name, root=PATHS.uploads_dir(), download=download)
            return
        base_dirs = [PATHS.media_dir(), PATHS.uploads_dir()]
        found = None
        for d in base_dirs:
            p = os.path.join(d, name)
            if os.path.isfile(p) and os.path.abspath(p).startswith(os.path.abspath(d)):
                found = p
                break
        if not found:  # 兜底:在 home-outputs 的 workdir 里找同名文件(按mtime新→旧·outputs堆积海量历史目录)
            ho = os.path.realpath(PATHS.under("outputs"))
            try:
                subdirs = sorted(
                    (os.path.join(ho, d) for d in os.listdir(ho) if os.path.isdir(os.path.join(ho, d))),
                    key=lambda p: os.path.getmtime(p), reverse=True)  # 新 workdir 在前
            except OSError:
                subdirs = []
            # 先在 workdir 顶层直接找(交付文件通常在 workdir 根),再有界递归少量最近目录
            for d in subdirs:
                cand = os.path.join(d, name)
                if os.path.isfile(cand):
                    found = cand
                    break
            if not found:  # 顶层没有再递归最近 30 个 workdir(防慢)
                for d in subdirs[:30]:
                    for dp, dns, fns in os.walk(d):
                        dns[:] = [x for x in dns if x not in ("__pycache__", ".git", "node_modules")]
                        if name in fns:
                            found = os.path.join(dp, name)
                            break
                    if found:
                        break
        if not found or not os.path.isfile(found) or ARTIFACTS._is_internal_artifact_path(found):
            self.send_response(404); self.end_headers(); return
        if not self._file_owner_allowed(found):
            return
        from artifact_content_safety import read_public_file, ArtifactUnsafe
        try:
            body = read_public_file(found)
        except ArtifactUnsafe as exc:
            self._send_json({"error": str(exc), "scan_status":
                             "blocked" if str(exc) == "artifact_sensitive_content" else "not_scanned"}, 403)
            return
        if not self._export_bytes_match(found, body):
            return
        ctype = mimetypes.guess_type(found)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        disp = "attachment" if download else "inline"
        fn = os.path.basename(found)
        fn_ascii = fn.encode("ascii", "ignore").decode() or "download"
        self.send_header("Content-Disposition", f"{disp}; filename=\"{fn_ascii}\"; filename*=UTF-8''{quote(fn)}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _serve_output(self, rel: str, root: str = None, download: bool = False):
        """Serve nested harness artifacts (path traversal safe). root 默认 service/outputs;
        传 HOME_OUTPUTS 时 serve 家目录 ~/.meyo/legion/outputs(codex 等家的持久工作目录产物)。
        download=True(前端下载链接传 ?download=1)→ attachment 强制下载;否则 inline 供预览。"""
        import mimetypes
        import pathlib
        from urllib.parse import unquote, quote
        want_download = bool(download)
        root = os.path.realpath(root or os.path.join(HERE, "outputs"))
        if ".sessions" in pathlib.Path(unquote(rel)).parts:
            self.send_response(404); self.end_headers(); return
        path = os.path.realpath(os.path.join(root, unquote(rel)))
        # 回退根(用户2026-08-31实测揪出:同一产物被发两条 artifact_ready·前缀 /outputs/ 与
        # /home-outputs/ 不一致→其中一条 404→用户点开空白/下载失败)。文件真实可能在任一 outputs
        # 根:主 root 未命中时,按 home/auto/service 顺序找同 rel·任一存在就 serve(路径穿越仍逐根校验)。
        if not (path.startswith(root + os.sep) and os.path.isfile(path)):
            _fallback_roots = [os.path.realpath(r) for r in
                               (PATHS.under("outputs"), ARTIFACTS.AUTO_SANDBOX_ROOT,
                                os.path.join(HERE, "outputs")) if r]
            for _r in _fallback_roots:
                if _r == root:
                    continue
                _p = os.path.realpath(os.path.join(_r, unquote(rel)))
                if _p.startswith(_r + os.sep) and os.path.isfile(_p):
                    root, path = _r, _p
                    break
            else:
                self.send_response(404); self.end_headers(); return
        if ARTIFACTS._is_internal_artifact_path(path):
            self.send_response(404); self.end_headers(); return
        if not self._file_owner_allowed(path):
            return
        from artifact_content_safety import read_public_file, ArtifactUnsafe
        try:
            body = read_public_file(path)
        except ArtifactUnsafe as exc:
            self._send_json({"error": str(exc), "scan_status":
                             "blocked" if str(exc) == "artifact_sensitive_content" else "not_scanned"}, 403)
            return
        if not self._export_bytes_match(path, body):
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/html") and not want_download:
            if ARTIFACTS.is_html_template(path):
                import personal_identity
                query = parse_qs(urlparse(self.path).query)
                scope = getattr(self, "_shared_file_scope", None)
                sid, share = scope or (str((query.get("sid") or [""])[0]),
                                       str((query.get("share") or [""])[0]))
                user = "" if scope else self._personal_user()
                rows = personal_identity.related_artifacts(path, user, sid, share)
                candidates = []
                seen = set()
                for row in rows:
                    target = personal_identity._local_reference(row)
                    if (not target or not target.lower().endswith((".html", ".htm"))
                            or ARTIFACTS.is_html_template(target)
                            or ARTIFACTS._is_private_artifact(row)):
                        continue
                    url = ARTIFACTS._url_for(target)
                    if not url or url in seen:
                        continue
                    if scope:
                        url = "/shared/" + quote(sid, safe="") + "/" + quote(share, safe="") + url
                    seen.add(url)
                    candidates.append({"name": os.path.basename(target), "url": url})
                download_url = self.path + ("&" if "?" in self.path else "?") + "download=1"
                self._send_artifact_notice(ARTIFACTS.template_notice(
                    os.path.basename(path), download_url, candidates))
                return
            body = ARTIFACTS.prepare_html_preview(body)
        if path.lower().endswith((".md", ".markdown")):
            ctype = "text/markdown; charset=utf-8"
        elif path.lower().endswith((".txt", ".json", ".jsonl", ".log", ".py", ".js", ".ts",
                                    ".css", ".csv", ".tsv", ".yaml", ".yml", ".sh")):
            ctype = ctype + "; charset=utf-8" if "charset=" not in ctype else ctype
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # HTML 产物按浏览器顶级页/新标签页直接打开（用户要求「等同打开 url」），但产物是
        # Agent 生成的不受信内容且代理后与主站同源——CSP sandbox 把 origin 变 opaque：
        # 脚本照跑、表单/弹窗可用，但读不到主站 localStorage(mars_token)/发不了同源请求，
        # 等同画布 iframe 的沙箱语义，地址栏仍显示真实 URL。下载(attachment)无脚本语义不加。
        if ctype.startswith("text/html") and not want_download:
            self.send_header("Content-Security-Policy",
                             "sandbox allow-scripts allow-forms allow-modals allow-pointer-lock")
        # main 新增的响应头（合并保留）：产物不可缓存、不带 referrer
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        # 下载失败根因(用户2026-08-30实测截图"无法从网站提取文件"):中文文件名 + inline 让 <a download>
        # 在 Chrome 失败。修:①?download=1 时用 attachment 强制下载 ②中文名走 RFC5987 filename*(UTF-8)
        # 让浏览器正确解析,不再"提取失败"。默认(html预览)仍 inline 供 iframe/在线访问渲染。
        fn = os.path.basename(path)
        fn_ascii = fn.encode("ascii", "ignore").decode() or "download"
        disp = "attachment" if want_download else "inline"
        self.send_header("Content-Disposition",
                         f"{disp}; filename=\"{fn_ascii}\"; filename*=UTF-8''{quote(fn)}")
        self.end_headers(); self.wfile.write(body)

    def _resolve_artifact_path(self, rel: str) -> str | None:
        """把产物相对路径(带 /outputs//home-outputs/ 前缀或裸 rel)解析成真实磁盘文件。
        复用 _serve_output 的多根回退+路径穿越安全。找不到返回 None。"""
        from urllib.parse import unquote
        if not rel:
            return None
        exam_match = re.match(r"^/api/verification/exam/file\?(.+)$", rel)
        if exam_match:
            from urllib.parse import parse_qs
            import verification_center
            query = parse_qs(exam_match.group(1))
            try:
                target = verification_center.exam_file(int((query.get("seq") or ["0"])[0]),
                                                       (query.get("path") or [""])[0])
            except (TypeError, ValueError):
                target = None
            return str(target) if target else None
        for pfx in ("/outputs/", "/home-outputs/", "/auto-outputs/", "/desktop-outputs/", "/media/"):
            if rel.startswith(pfx):
                rel = rel[len(pfx):]
                break
        rel = unquote(rel)
        roots = [os.path.realpath(r) for r in
                 (os.path.join(HERE, "outputs"), PATHS.under("outputs"),
                  ARTIFACTS.AUTO_SANDBOX_ROOT, ARTIFACTS.MEDIA) if r]
        for _r in roots:
            _p = os.path.realpath(os.path.join(_r, rel))
            if _p.startswith(_r + os.sep) and os.path.isfile(_p):
                return _p
        return None

    def _serve_convert(self, rel: str, to: str):
        """后端渲染兜底:把交付物转成前端可直渲的 html/png。整体兜底:任何异常→502 让前端
        退回原始下载(不崩)。各格式依赖须独立核验，不能用本机安装外推发布环境。"""
        path = self._resolve_artifact_path(rel)
        if not path:
            self.send_response(404); self.end_headers(); return
        if ARTIFACTS._is_internal_artifact_path(path):
            self.send_response(404); self.end_headers(); return
        if not self._file_owner_allowed(path):
            return
        from artifact_content_safety import read_public_file, ArtifactUnsafe
        try:
            content = read_public_file(path)
        except ArtifactUnsafe as exc:
            self._send_json({"error": str(exc), "scan_status":
                             "blocked" if str(exc) == "artifact_sensitive_content" else "not_scanned"}, 403)
            return
        ext = os.path.splitext(path)[1].lstrip(".").lower()
        if to == "auto":
            to = {"xlsx": "table", "xls": "table", "csv": "table",
                  "md": "md_html", "markdown": "md_html", "pdf": "pdf_png",
                  "ansi": "ansi_html", "cast": "ansi_html"}.get(ext, "")
        try:
            if to == "ansi_html":
                # TUI 家终端快照 → 黑底终端 HTML(用户 2026-09-02 需求:TUI 家的原生样子在
                # 对话流渲染)。轻量 ANSI 解释器:CSI 颜色/粗体/暗色 + 备用屏序列忽略。
                raw = content.decode("utf-8", errors="replace")
                self._send_html(self._ansi_to_html(raw))
            elif to == "md_html":
                from artifact_markdown import render_markdown
                html = render_markdown(content.decode("utf-8", errors="replace"))
                self._send_html(f'<div class="md-body">{html}</div>')
            elif to == "table":
                self._send_html(self._artifact_to_table(path, ext, content=content))
            elif to == "pdf_png":
                import pymupdf  # type: ignore
                with pymupdf.open(stream=content, filetype="pdf") as doc:
                    pages = doc.page_count
                    pg = doc.load_page(0)
                    png = pg.get_pixmap(dpi=110).tobytes("png")
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png)))
                self.send_header("X-PDF-Pages", str(pages))
                self.send_header("Cache-Control", "no-store")
                self.end_headers(); self.wfile.write(png)
            else:
                self.send_response(415); self.end_headers()
        except Exception as exc:
            # 转换失败不崩·502 让前端回退原始下载/在线访问
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"convert failed: {type(exc).__name__}".encode())

    @staticmethod
    def _ansi_to_html(raw: str) -> str:
        """原始 ANSI 流 → 黑底终端 HTML(轻量解释器·不引 xterm.js 大件)。

        支持:SGR 颜色(30-37/90-97 前景·1 粗体·2 暗·7 反色·0 重置);其余 CSI(光标移动/
        清屏/进出备用屏)一律忽略(渲染成顺序文本——快照是末态流,重放光标语义没必要)。
        HTML 转义先做再套 span,防注入。
        """
        import html as _h
        import re as _re
        colors = {"30": "#555", "31": "#e06c75", "32": "#98c379", "33": "#e5c07b",
                  "34": "#61afef", "35": "#c678dd", "36": "#56b6c2", "37": "#abb2bf",
                  "90": "#7f848e", "91": "#ff7b86", "92": "#b5e890", "93": "#ffd479",
                  "94": "#9cd6ff", "95": "#e2b8ff", "96": "#a3e5e9", "97": "#ffffff"}
        text = _h.escape(raw)
        # 先剥非 SGR 的 CSI/OSC/控制序列(光标/清屏/备用屏),保留 SGR(\x1b[...m)待解析
        text = _re.sub(r"\x1b\[[0-9;?]*[a-ln-zA-LN-Z]", "", text)
        text = _re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
        text = _re.sub(r"\x1b[()][0-9A-B]", "", text)
        text = _re.sub(r"\x1b[=>78MDEHc]", "", text)
        out, style = [], []
        token = _re.compile(r"\x1b\[([0-9;]*)m")

        def _close(span_open: bool) -> str:
            return "</span>" if span_open else ""

        pos, span_open = 0, False
        for m in token.finditer(text):
            chunk = text[pos:m.start()]
            if chunk:
                out.append(chunk)
            codes = [c for c in (m.group(1) or "0").split(";") if c != ""]
            if not codes:
                codes = ["0"]
            out.append(_close(span_open)); span_open = False
            css = []
            for c in codes:
                if c == "0":
                    css = []
                elif c == "1":
                    css.append("font-weight:700")
                elif c == "2":
                    css.append("opacity:.6")
                elif c == "7":
                    css.append("background:#abb2bf;color:#282c34")
                elif c in colors:
                    css.append(f"color:{colors[c]}")
            if css:
                out.append(f'<span style="{";".join(css)}">')
                span_open = True
            pos = m.end()
        out.append(text[pos:])
        out.append(_close(span_open))
        body = "".join(out)
        # 压掉连续空帧:保留换行结构·多余空行折一行(快照常带整屏重绘残留)
        body = _re.sub(r"\n{3,}", "\n\n", body)
        return (f'<div class="term-snap"><pre style="margin:0;white-space:pre-wrap;'
                f'font:12px/1.5 ui-monospace,Menlo,monospace;color:#abb2bf">{body}</pre></div>')

    def _artifact_to_table(self, path: str, ext: str, *, content: bytes | None = None) -> str:
        """excel/csv → html <table>(前 200 行·防超大表卡浏览器)。"""
        import html as _h
        import io
        if content is None:
            from artifact_content_safety import read_public_file
            content = read_public_file(path)
        rows = []
        if ext in ("xlsx", "xls"):
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            ws = wb.active
            for i, r in enumerate(ws.iter_rows(values_only=True)):
                if i >= 200:
                    break
                rows.append([("" if c is None else str(c)) for c in r])
            wb.close()
        else:  # csv
            import csv as _csv
            with io.StringIO(content.decode("utf-8", errors="replace"), newline="") as f:
                for i, r in enumerate(_csv.reader(f)):
                    if i >= 200:
                        break
                    rows.append(r)
        if not rows:
            return '<div class="md-body"><em>(空表)</em></div>'
        head = rows[0]
        th = "".join(f"<th>{_h.escape(c)}</th>" for c in head)
        trs = []
        for r in rows[1:]:
            tds = "".join(f"<td>{_h.escape(c)}</td>" for c in r)
            trs.append(f"<tr>{tds}</tr>")
        return (f'<table class="art-table"><thead><tr>{th}</tr></thead>'
                f'<tbody>{"".join(trs)}</tbody></table>')

    def _send_html(self, html: str):
        # 生产工作台独立维护，避免继续膨胀唯一 HTML；注入固定同源资源。
        if "desktop_production.js" not in html:
            html = html.replace("</head>", '<link rel="stylesheet" href="/desktop_production.css"></head>')
            html = html.replace("</body>", '<script src="/desktop_production.js"></script></body>')
        # The game studio is another view of this same authenticated session,
        # upload, native execution and artifact pipeline; it has no second runner.
        if "game_workbench.js" not in html:
            html = html.replace("</head>", '<link rel="stylesheet" href="/game_workbench.css"></head>')
            html = html.replace("</body>", '<script src="/game_workbench.js"></script></body>')
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers(); self.wfile.write(body)

    def _handle_upload(self):
        """JSON 保留单文件旧响应；浏览器 multipart 返回 files[]，请求体上限 25MiB。

        存 ~/.meyo/legion/uploads,逐文件过 file_hub 识别类型,返回
        {ok, files:[{name,size,url,path,kind,error?}]}——前端按 error 逐文件提示,
        url 供对话内渲染,path 供 dispatch 附件引用(军团识图/读PDF)。
        """
        import base64 as _b64
        import uuid as _uuid
        import personal_identity
        from meyo_ticket_identity import TicketAuthError
        user = self._personal_user()
        if not user:
            self._send_json({"error": "personal_auth_required"}, 401)
            return
        try:
            is_multipart = self._is_multipart_upload()
            if is_multipart:
                files = self._read_multipart_upload()
                if files is None:
                    return
            else:
                payload = self._read_json_body()
                if payload is None:
                    return
                fn = str(payload.get("filename") or "upload")
                data_b64 = str(payload.get("data_base64") or "")
                if "," in data_b64 and data_b64.strip().startswith("data:"):
                    data_b64 = data_b64.split(",", 1)[1]  # 剥 data:...;base64, 前缀
                blob = _b64.b64decode(data_b64) if data_b64 else b""
                if not blob:
                    self.close_connection = True
                    self._send_json({"ok": False, "error": "空文件或 data_base64 缺失"}, 400)
                    return
                files = [(fn, blob)]
            ticket = getattr(self, "_request_upload_ticket", None)
            if ticket is not None:
                # Validate the whole batch before even the first file is saved.
                personal_identity.reject_ticket_in_content([fn for fn, _blob in files], ticket)
            out = []
            for fn, blob in files:
                ext = os.path.splitext(fn)[1].lower().lstrip(".") or "bin"
                up_dir = personal_identity.upload_root(user)
                os.makedirs(up_dir, exist_ok=True)
                safe = f"{_uuid.uuid4().hex[:10]}.{ext}"
                path = os.path.join(up_dir, safe)
                with open(path, "wb") as f:
                    f.write(blob)
                # 两种协议共用原有个人目录、URL 和文件类型识别。
                import file_hub
                public_url = "/media/" + os.path.relpath(path, PATHS.uploads_dir()).replace(os.sep, "/")
                meta = file_hub.describe(path, url=public_url)
                if not is_multipart:
                    self._send_json({"ok": True, "url": public_url, "path": path,
                                     "kind": meta.get("kind", "file"), "bytes": len(blob),
                                     "orig_name": fn, "preview": meta.get("preview"),
                                     "renderable": meta.get("renderable", False)})
                    return
                out.append({"name": fn, "size": len(blob), "url": public_url,
                            "path": path, "kind": meta.get("kind", "file")})
            self._send_json({"ok": True, "files": out})
        except TicketAuthError as error:
            self.close_connection = True
            self._send_json({"error": error.code}, error.status)
        except Exception as e:
            self.close_connection = True
            self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 400)

    def _read_multipart_upload(self) -> list[tuple[str, bytes]] | None:
        """Parse and bind the entire form before any file is written."""
        from email import policy
        from email.errors import UndecodableBytesDefect
        from email.parser import BytesParser
        raw = self._read_request_body()
        if raw is None:
            return None
        try:
            if len(self.headers.get_all("Content-Type", [])) != 1:
                raise ValueError("ambiguous content type")
            ctype = self.headers["Content-Type"]
            message = BytesParser(policy=policy.default.clone(raise_on_defect=True)).parsebytes(
                b"Content-Type: " + ctype.encode("ascii") + b"\r\n\r\n" + raw)
            boundary = message.get_boundary()
            if (not message.is_multipart() or message["Content-Type"].defects or not boundary
                    or not re.fullmatch(r"[0-9A-Za-z'()+_,./:=? -]{1,70}", boundary)
                    or boundary.endswith(" ")):
                raise ValueError("invalid multipart boundary")
            # The shipped fetch wrapper pins uid in the URL, not in FormData.
            # Optional scalar form fields use the same ownership checks as JSON.
            # Nested JSON-in-text and multipart ticket login are not new APIs.
            scalar_fields = {"uid", "user_id", "sid", "session_id", "conversation_id",
                             "team_id", "qhash"}
            values = {}
            for name, items in parse_qs(urlparse(self.path).query, keep_blank_values=True).items():
                if name not in scalar_fields or len(items) != 1:
                    raise ValueError("unsupported or repeated query field")
                values[name] = items[0]
            files, form_fields = [], set()
            for part in message.iter_parts():
                if (part.is_multipart()
                        or len(part.get_all("Content-Disposition", [])) != 1
                        or part.get_content_disposition() != "form-data"
                        or len(part.get_all("Content-Type", [])) > 1
                        or part.get_all("Content-Transfer-Encoding", [])
                        or any(not isinstance(defect, UndecodableBytesDefect)
                               for header in part.values() for defect in header.defects)):
                    raise ValueError("invalid multipart part")
                # Browser FormData permits raw UTF-8 filenames; email's ASCII
                # header warning is not corruption. Invalid UTF-8 still fails.
                for _key, value in part.raw_items():
                    value.encode("utf-8", errors="surrogateescape").decode("utf-8")
                name = part.get_param("name", header="content-disposition")
                filename = part.get_filename()
                blob = part.get_payload(decode=True)
                if filename is not None:
                    # files is the frontend field; file is the existing HTTP
                    # acceptance probe's single-file spelling.
                    if name not in {"files", "file"} or not blob:
                        raise ValueError("missing or empty file")
                    files.append((filename or "upload", blob))
                else:
                    if name not in scalar_fields or name in form_fields:
                        raise ValueError("unsupported or repeated form field")
                    value = (blob or b"").decode("utf-8")
                    if name in values and values[name] != value:
                        raise ValueError("conflicting query and form field")
                    values[name] = value
                    form_fields.add(name)
            if not files:
                raise ValueError("missing files")
            if not self._bind_personal_request(values, allow_new=True):
                self.close_connection = True
                return None
            return files
        except Exception:
            self.close_connection = True
            self._send_json({"ok": False, "error": "bad multipart body"}, 400)
            return None

    def _read_request_body(self) -> bytes | None:
        """One bounded reader for JSON and multipart; never drain rejected bodies."""
        try:
            lengths = self.headers.get_all("Content-Length", [])
            value = lengths[0].strip() if lengths else "0"
            if (len(lengths) > 1 or self.headers.get_all("Transfer-Encoding", [])
                    or not re.fullmatch(r"[0-9]+", value)):
                raise ValueError("invalid content length")
            length = int(value)
            limit = 25 * 1024 * 1024 if urlparse(self.path).path == "/api/legion/upload" else MAX_BODY
            if length > limit:
                # A body ticket requires parsing before authentication; do not
                # wait for an unauthenticated oversized body to be drained.
                self.close_connection = True
                self._send_json({"error": f"请求体过大({length} > {limit})"}, 413)
                return None
            previous_timeout = self.connection.gettimeout()
            try:
                deadline = time.monotonic() + 5.0
                remaining, chunks = length, []
                while remaining:
                    timeout = deadline - time.monotonic()
                    if timeout <= 0:
                        raise TimeoutError("request body timeout")
                    self.connection.settimeout(timeout)
                    chunk = self.rfile.read1(min(remaining, 65536))
                    if not chunk:
                        raise ValueError("truncated request body")
                    chunks.append(chunk)
                    remaining -= len(chunk)
                return b"".join(chunks)
            finally:
                self.connection.settimeout(previous_timeout)
        except Exception:
            self.close_connection = True
            self._send_json({"error": "bad request body"}, 400)
            return None

    def _parse_json_payload(self) -> dict | None:
        raw = self._read_request_body()
        if raw is None:
            return None
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
            # 校验是 JSON 对象(军团 deepseek dogfood 揪出:合法但非对象的 JSON 如 "hi"/[1,2]/123
            # 会让下游所有 payload.get(...) 抛 AttributeError→兜底成 500 且回显内部异常·应回 400)。
            # 一处修复覆盖全部 POST 端点。
            if not isinstance(payload, dict):
                self.close_connection = True
                self._send_json({"error": "请求体必须是 JSON 对象"}, 400)
                return None
            return payload
        except Exception:
            self.close_connection = True
            self._send_json({"error": "bad request body"}, 400)
            return None

    def _read_json_body(self) -> dict | None:
        payload = getattr(self, "_request_json_payload", None)
        if payload is None:
            payload = self._parse_json_payload()
        if payload is None or not self._bind_personal_request(payload, allow_new=True):
            return None
        return payload

    def _tool_runtime_context(self, payload: dict, params: dict) -> dict | None:
        """Bind checked HTTP identity and existing project facts, never arbitrary env."""
        uid = str(payload["uid"])
        context = {"MEYO_USER_ID": uid}
        # A native-tool's outer params already declare parent-call fields.
        # Other tools may have an unrelated remote session_id in their params.
        parent_sid = (params.get("session_id")
                      if str(payload.get("tool") or "").startswith("agent_tool:") else "")
        sid = str(payload.get("session_id") or parent_sid or "")
        project = {}
        if sid:
            if SESSIONS.load_session(sid, user_id=uid) is None:
                self._send_json({"ok": False, "error": "session_not_found"}, 404)
                return None
            context["MEYO_SESSION_ID"] = sid
            project = SESSIONS.continuation_project_context(sid, user_id=uid)
        if project:
            project = dict(project)
            project["user_id"] = uid
            if sid:
                project.setdefault("conversation_id", sid)
            context["MEYO_PROJECT_CONTEXT_JSON"] = json.dumps(project, ensure_ascii=False)
            root = project.get("repo_root") or project.get("workspace_root")
            if root:
                context["MEYO_WORKDIR"] = root
            # Historical team/task/native IDs stay references, not the new
            # borrowed execution's identity. The provider already allocates its slot.
            facts = {"project_context": project}
            if project.get("source_ref"):
                facts["source_ref"] = project["source_ref"]
            context["MEYO_TASK_FACTS_JSON"] = json.dumps(facts, ensure_ascii=False)
        if payload.get("capability_policy") is not None:
            policy = payload["capability_policy"]
            if not isinstance(policy, dict):
                self._send_json({"ok": False, "error": "capability_policy must be an object"}, 400)
                return None
            context["MEYO_CAPABILITY_POLICY_JSON"] = json.dumps(policy, ensure_ascii=False)
        if (str(payload.get("tool") or "") in {
                "external_capability:dsh-superagent:status",
                "external_capability:dsh-superagent:settings",
                "external_capability:dsh-superagent:sources",
            } and _PUBLIC_ORIGIN is None and not _REMOTE_BIND_OK
                and str(self.client_address[0]).lower() in _LOOPBACKS
                and str(self.server.server_address[0]).lower() in _LOOPBACKS
                and not getattr(self, "_request_ticket_identity", None)):
            context.update(_readonly_instance_environment(self._personal_user()))
        return context

    def _do_POST_inner(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/legion/mobile/"):
            mobile_payload = self._read_json_body()
            if mobile_payload is None or not self._authorize_mobile_request(parsed.path, mobile_payload):
                return
        if parsed.path == "/api/legion/auth/session":
            import personal_identity
            # Exchange only the caller's already-issued personal Bearer. No
            # credential is generated and the instance cookie cannot be promoted.
            auth = str(self.headers.get("Authorization") or "")
            token = personal_identity.personal_token(self.headers, str(getattr(self.server, "api_token", "") or ""))
            user = personal_identity.resolve_token(token) if auth.lower().startswith("bearer ") else None
            if not user or not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", token):
                self._send_json({"error": "personal_auth_required"}, 401)
                return
            body = json.dumps({"authenticated": True, "user_id": user, "sso_verified": False}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            secure = "" if _PUBLIC_ORIGIN is None and str(self.client_address[0]) in _LOOPBACKS and str(
                urlparse("//" + self.headers.get("Host", "")).hostname or "") in _LOOPBACKS else "; Secure"
            self.send_header("Set-Cookie", f"{personal_identity.COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict{secure}")
            self.end_headers(); self.wfile.write(body)
        elif parsed.path == "/api/verification/review":
            import verification_http
            verification_http.handle_review_post(self)
        elif parsed.path == "/api/legion/dispatch":
            payload = self._read_json_body()
            if payload is None:
                return
            # 觅游用户身份：ticket 换真实 uid（主站回调）。换不出 → 401。
            # uid 写回 payload，_handle_dispatch 与下游 save_turn 全链路用真实身份。
            _ticket_uid = _uid_from_request(self, body=payload)
            if not _ticket_uid:
                self._send_json({"error": "unauthorized",
                                 "reason": "请先登录觅游账号"}, 401)
                return
            payload["uid"] = _ticket_uid
            self._handle_dispatch(payload)
        elif parsed.path == "/api/legion/run_one":
            # 手动 fork 子 agent 的真执行接口(SSE):用户在树上选家+填任务后立刻调用
            payload = self._read_json_body()
            if payload is None:
                return
            self._handle_run_one(payload)
        elif parsed.path == "/api/legion/parse_plan":
            # 总指挥模型解析自然语言派活/层级(context not control)·失败前端才退正则
            payload = self._read_json_body()
            if payload is None:
                return
            self._send_json(LEGION.parse_dispatch_with_model(str(payload.get("instruction") or "")))
        elif parsed.path == "/api/legion/tool":
            # 真执行一个可运行工具· 军团任意家共享的工具箱入口。
            # 走统一入口 legion_tool.run_tool(不只媒体工具):它内部先试媒体工具(生图/识图/读PDF)、
            # 再 search_github(军团级搜github装新工具·"能检索到就能用"闭环)、再 opencli,最后给
            # reference。之前这里只调 run_media_tool,军团发现了 search_github 却通过 HTTP 调不到——
            # 检索/执行闭环断在端点层。改走 run_tool 后与 CLI 一致(媒体调用向后兼容·run_tool第一步就是试媒体)。
            payload = self._read_json_body()
            if payload is None:
                return
            tool_id = str(payload.get("tool") or "")
            if not tool_id:
                self._send_json({"ok": False, "error": "缺 tool 字段"}, 400)
                return
            raw_params = payload.get("params")
            if raw_params is not None and not isinstance(raw_params, dict):
                self._send_json({"ok": False, "error": "params must be an object"}, 400)
                return
            params = dict(raw_params or {})
            if payload.get("capability_policy") is not None:
                params["_capability_policy"] = payload.get("capability_policy")
            context = self._tool_runtime_context(payload, params)
            if context is None:
                return
            import agent_tool_provider
            with agent_tool_provider._runtime_scope(
                    context, None, inherit_process_context=False):
                self._send_json(LEGION_TOOL.run_tool(tool_id, params))
        elif parsed.path == "/api/legion/upload":
            # 用户上传图片/PDF 到对话(存 ~/.meyo/legion/uploads)· 返回可引用 url + 本机路径
            self._handle_upload()
        elif parsed.path == "/api/legion/opencli":
            # 军团调 OpenCLI(把网站变CLI·JSON喂harness)· body: {"args": ["hackernews","top"]}
            payload = self._read_json_body()
            if payload is None:
                return
            self._send_json(OPENCLI.run(payload.get("args") or [],
                                        want_json=payload.get("json", True) is not False))
        elif parsed.path == "/api/legion/install":
            # 安装:{id / scene, confirm}。confirm=false只回命令(dry-run)·true才真装
            payload = self._read_json_body()
            if payload is None:
                return
            confirm = bool(payload.get("confirm"))
            if payload.get("scene"):
                self._send_json(INSTALL.install_scene(str(payload["scene"]), confirm=confirm))
            elif payload.get("id"):
                self._send_json(INSTALL.install(str(payload["id"]), confirm=confirm))
            else:
                self._send_json({"ok": False, "error": "缺 id 或 scene"}, 400)
        elif parsed.path == "/api/legion/cloud_desktop/workspace/project/create":
            payload = self._read_json_body()
            if payload is None:
                return
            result = DESKTOP_WORKSPACE.create_project(
                str(payload.get("uid") or "default"), str(payload.get("title") or "新项目"),
                str(payload.get("brief") or ""), str(payload.get("project_id") or ""),
                str(payload.get("objective") or ""), payload.get("success_criteria") or [],
                str(payload.get("owner") or ""))
            self._send_json(result, 201 if result.get("ok") else 400)
        elif parsed.path == "/api/legion/objective/link":
            payload = self._read_json_body()
            if payload is None:
                return
            result = OBJECTIVE_WORKSPACE.link(
                str(payload.get("uid") or "default"), str(payload.get("project_id") or ""),
                str(payload.get("task_id") or ""), str(payload.get("source_type") or ""),
                str(payload.get("source_id") or ""), metadata=payload.get("metadata") or {})
            self._send_json(result, 201 if result.get("ok") else 400)
        elif parsed.path == "/api/legion/cloud_desktop/workspace/task/create":
            payload = self._read_json_body()
            if payload is None:
                return
            result = DESKTOP_WORKSPACE.create_task(str(payload.get("uid") or "default"), str(payload.get("project_id") or ""), str(payload.get("goal") or ""), str(payload.get("task_id") or ""), str(payload.get("team_id") or ""), str(payload.get("session_id") or ""), str(payload.get("mode") or "auto"), payload.get("execution_spec") if isinstance(payload.get("execution_spec"), dict) else None)
            if result.get("ok"):
                DESKTOP_WORKER.wake()
            self._send_json(result, 201 if result.get("ok") else 400)
        elif parsed.path == "/api/legion/cloud_desktop/quality/create":
            payload = self._read_json_body()
            if payload is None:
                return
            import desktop_quality_run
            result = desktop_quality_run.create(
                str(payload.get("uid") or "default"), str(payload.get("project_id") or ""),
                str(payload.get("task_id") or ""),
                payload.get("profile") if isinstance(payload.get("profile"), dict) else None,
                max_rounds=int(payload.get("max_rounds") or 3),
                wall_clock_s=int(payload.get("wall_clock_s") or 7200))
            self._send_json(result, 201 if result.get("ok") else 400)
        elif parsed.path == "/api/legion/cloud_desktop/workspace/task/update":
            payload = self._read_json_body()
            if payload is None:
                return
            result = DESKTOP_WORKSPACE.update_task(str(payload.get("uid") or "default"), str(payload.get("project_id") or ""), str(payload.get("task_id") or ""), state=payload.get("state"), phase=payload.get("phase"), checkpoint=payload.get("checkpoint"), completed_steps=payload.get("completed_steps"), artifacts=payload.get("artifacts"), screenshots=payload.get("screenshots"), failure=payload.get("failure"), expected_version=int(payload["version"]) if payload.get("version") is not None else None)
            self._send_json(result, 200 if result.get("ok") else 409 if result.get("error") == "task version conflict" else 400)
        elif parsed.path == "/api/legion/cloud_desktop/workspace/task/resume":
            payload = self._read_json_body()
            if payload is None:
                return
            result = DESKTOP_WORKSPACE.request_resume(str(payload.get("uid") or "default"), str(payload.get("project_id") or ""), str(payload.get("task_id") or ""))
            if result.get("ok"):
                DESKTOP_WORKER.wake()
            self._send_json(result, 200 if result.get("ok") else 400)
        elif parsed.path == "/api/legion/cloud_desktop/workspace/task/finalize":
            payload = self._read_json_body()
            if payload is None:
                return
            result = DESKTOP_WORKSPACE.finalize(str(payload.get("uid") or "default"), str(payload.get("project_id") or ""), str(payload.get("task_id") or ""), str(payload.get("status") or "completed"), str(payload.get("failure_summary") or ""))
            self._send_json(result, 200 if result.get("state") in ("completed", "partial") else 400)
        elif parsed.path == "/api/legion/cloud_desktop/handoff/request":
            payload = self._read_json_body()
            if payload is None:
                return
            result = HUMAN_HANDOFF.request(str(payload.get("uid") or "default"), str(payload.get("project_id") or ""), str(payload.get("task_id") or ""), str(payload.get("kind") or "other"), str(payload.get("title") or "需要你处理"), str(payload.get("instructions") or ""), app=str(payload.get("app") or ""), target=str(payload.get("target") or ""), consequence=str(payload.get("consequence") or ""), checkpoint=payload.get("checkpoint") or {}, completion_check=payload.get("completion_check") or {}, ttl_s=int(payload.get("ttl_s") or 86400))
            self._send_json(result, 201 if result.get("ok") else 400)
        elif parsed.path == "/api/legion/cloud_desktop/handoff/decide":
            payload = self._read_json_body()
            if payload is None:
                return
            result = HUMAN_HANDOFF.decide(str(payload.get("uid") or "default"), str(payload.get("project_id") or ""), str(payload.get("handoff_id") or ""), str(payload.get("decision") or ""), actor=str(payload.get("actor") or "user"), note=str(payload.get("note") or ""), expected_version=int(payload["version"]) if payload.get("version") is not None else None)
            if result.get("resume_requested"):
                DESKTOP_WORKER.wake()
            code = 200 if result.get("ok") or result.get("status") == "unchanged" else 409 if "conflict" in str(result.get("error")) else 400
            self._send_json(result, code)
        elif parsed.path == "/api/legion/cloud_desktop/software/transact":
            payload = self._read_json_body()
            if payload is None:
                return
            result = DESKTOP_SOFTWARE.transact(str(payload.get("uid") or "default"), str(payload.get("software_id") or ""), str(payload.get("action") or "install"), confirm=bool(payload.get("confirm")), rollback_on_failure=payload.get("rollback_on_failure") is not False)
            self._send_json(result, 200 if result.get("ok") else 400)
        elif parsed.path == "/api/legion/cloud_desktop/software/rollback":
            payload = self._read_json_body()
            if payload is None:
                return
            result = DESKTOP_SOFTWARE.rollback(str(payload.get("uid") or "default"), str(payload.get("transaction_id") or ""), confirm=bool(payload.get("confirm")))
            self._send_json(result, 200 if result.get("ok") else 400)
        elif parsed.path == "/api/legion/cloud_desktop/recipe/create":
            payload = self._read_json_body()
            if payload is None:
                return
            result = DESKTOP_RECIPE.create_from_task(str(payload.get("uid") or "default"), str(payload.get("project_id") or ""), str(payload.get("task_id") or ""), str(payload.get("name") or "云电脑流程"), str(payload.get("recipe_id") or ""))
            self._send_json(result, 201 if result.get("ok") else 400)
        elif parsed.path == "/api/legion/cloud_desktop/recipe/replay":
            payload = self._read_json_body()
            if payload is None:
                return
            result = DESKTOP_RECIPE.replay_context(str(payload.get("uid") or "default"), str(payload.get("recipe_id") or ""), payload.get("inputs") or {})
            self._send_json(result, 200 if result.get("ok") else 400)
        elif parsed.path == "/api/legion/cloud_desktop/recipe/export":
            payload = self._read_json_body()
            if payload is None:
                return
            result = DESKTOP_RECIPE.export_skill(str(payload.get("uid") or "default"), str(payload.get("recipe_id") or ""))
            self._send_json(result, 200 if result.get("ok") else 400)
        elif parsed.path == "/api/legion/cloud_desktop/transfer/upload":
            payload = self._read_json_body()
            if payload is None:
                return
            result = DESKTOP_TRANSFER.upload(str(payload.get("uid") or "default"), str(payload.get("project_id") or ""), str(payload.get("filename") or ""), content_base64=str(payload.get("content_base64") or ""), destination=str(payload.get("destination") or "inputs"))
            self._send_json(result, 201 if result.get("ok") else 400)
        elif parsed.path == "/api/legion/cloud_desktop/transfer/download":
            payload = self._read_json_body()
            if payload is None:
                return
            result = DESKTOP_TRANSFER.download(str(payload.get("uid") or "default"), str(payload.get("path") or ""))
            self._send_json(result, 200 if result.get("ok") else 400)
        elif parsed.path == "/api/legion/cloud_desktop/clipboard":
            payload = self._read_json_body()
            if payload is None:
                return
            action = str(payload.get("action") or "read")
            result = DESKTOP_TRANSFER.clipboard_write(str(payload.get("uid") or "default"), str(payload.get("text") or "")) if action == "write" else DESKTOP_TRANSFER.clipboard_read(str(payload.get("uid") or "default"))
            self._send_json(result, 200 if result.get("ok") else 400)
        elif parsed.path == "/api/legion/cloud_desktop/route":
            # 智能意图路由(学 Lumi):简单指令本机秒回·复杂任务派军团·带屏幕场景感知
            import cloud_desktop as _cd_router
            body = self._read_json_body()
            if body is None:
                return
            out = _cd_router.route_intent(
                str(body.get("text") or ""), str(body.get("uid") or "default"),
                action=str(body.get("action") or ""), params=body.get("params"))
            self._send_json(out, 200 if out.get("ok") else 400)
        elif parsed.path == "/api/legion/cloud_desktop/ensure":
            payload = self._read_json_body()
            if payload is None:
                return
            import cloud_desktop
            self._send_json(cloud_desktop.ensure_desktop(str(payload.get("uid") or "default")))
        elif parsed.path == "/api/legion/mobile/ensure":
            payload = self._read_json_body()
            if payload is None:
                return
            import mobile_device
            out = mobile_device.ensure(payload.get("timeout_s") or 120,
                                       uid=_uid_from_request(self, body=payload))
            self._send_json(out, 200 if out.get("ok") else 503)
        elif parsed.path == "/api/legion/mobile/stop":
            payload = self._read_json_body()
            if payload is None:
                return
            import mobile_device
            out = mobile_device.stop(uid=_uid_from_request(self, body=payload))
            self._send_json(out, 200 if out.get("ok") else 400)
        elif parsed.path == "/api/legion/mobile/action":
            payload = self._read_json_body()
            if payload is None:
                return
            import mobile_device
            uid = _uid_from_request(self, body=payload)
            action_name = str(payload.get("action") or "")
            started = time.perf_counter()
            out = mobile_device.action(action_name, payload.get("params") or {}, uid=uid)
            out["action_ms"] = round((time.perf_counter() - started) * 1000, 1)
            if out.get("ok") and action_name == "screenshot":
                out["url"] = self._mobile_screenshot_url(out, uid)
            # 默认动作完成即返回；只有调用方明确要求视觉确认时才同步截图。
            # UI 可随后单独 GET screenshot，避免每次 tap/type 都白付一次 3-12s screencap。
            if out.get("ok") and action_name != "screenshot" and payload.get("verify_visual") is True:
                shot_started = time.perf_counter()
                shot = mobile_device.screenshot(uid=uid)
                out["screenshot_ms"] = round((time.perf_counter() - shot_started) * 1000, 1)
                if shot.get("ok"):
                    shot["url"] = self._mobile_screenshot_url(shot, uid)
                    out["screenshot"] = shot
                else:
                    out["visual_verification"] = {"ok": False,
                                                  "error": shot.get("error") or "screenshot failed",
                                                  "failure_kind": shot.get("failure_kind") or "screenshot_error"}
            self._send_json(out, 200 if out.get("ok") else 400)
        elif parsed.path == "/api/legion/mobile/actions/batch":
            payload = self._read_json_body()
            if payload is None:
                return
            import mobile_device
            uid = _uid_from_request(self, body=payload)
            actions = payload.get("actions") if isinstance(payload.get("actions"), list) else []
            for spec in actions[:50]:
                # check_request already handles nested params, but does not walk
                # arbitrary lists. Reuse it for every batch item before effects.
                if isinstance(spec, dict) and not self._bind_personal_request({"params": spec}, allow_new=True):
                    return
            started = time.perf_counter()
            results = []
            for index, spec in enumerate(actions[:50]):
                if not isinstance(spec, dict):
                    continue
                step_started = time.perf_counter()
                result = mobile_device.action(str(spec.get("action") or ""), spec.get("params") or {}, uid=uid)
                if result.get("ok") and spec.get("action") == "screenshot":
                    result["url"] = self._mobile_screenshot_url(result, uid)
                results.append({"index": index, "action": spec.get("action"),
                                "elapsed_ms": round((time.perf_counter() - step_started) * 1000, 1),
                                **result})
                if not result.get("ok") and payload.get("continue_on_error") is not True:
                    break
            shot = None
            if payload.get("verify_visual") is True:
                shot = mobile_device.screenshot(uid=uid)
                if shot.get("ok"):
                    shot["url"] = self._mobile_screenshot_url(shot, uid)
            out = {"ok": bool(results) and all(row.get("ok") for row in results),
                   "results": results, "steps_completed": len(results),
                   "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                   "screenshot": shot}
            self._send_json(out, 200 if out.get("ok") else 400)
        elif parsed.path == "/api/legion/mobile/apk/inspect":
            payload = self._read_json_body()
            if payload is None:
                return
            import mobile_device
            out = mobile_device.inspect_apk(str(payload.get("source") or ""),
                                            timeout_s=int(payload.get("timeout_s") or 120))
            self._send_json(out, 200 if out.get("ok") else 400)
        elif parsed.path == "/api/legion/mobile/apk/install":
            payload = self._read_json_body()
            if payload is None:
                return
            import mobile_device
            out = mobile_device.install_apk(str(payload.get("token") or ""),
                                            confirm=bool(payload.get("confirm")))
            self._send_json(out, 200 if out.get("ok") else 400)
        elif parsed.path == "/api/legion/mobile/apps/plan":
            payload = self._read_json_body()
            if payload is None:
                return
            out = MOBILE_APPS.plan(payload.get("app") or {}, operation=str(payload.get("operation") or "install"),
                                   timeout_s=int(payload.get("timeout_s") or 180))
            self._send_json(out, 200 if out.get("ok") else 400)
        elif parsed.path == "/api/legion/mobile/apps/apply":
            payload = self._read_json_body()
            if payload is None:
                return
            out = MOBILE_APPS.apply_plan(str(payload.get("token") or ""), confirm=bool(payload.get("confirm")))
            self._send_json(out, 200 if out.get("ok") else 400)
        elif parsed.path == "/api/legion/mobile/apps/uninstall":
            payload = self._read_json_body()
            if payload is None:
                return
            out = MOBILE_APPS.uninstall(str(payload.get("package") or ""), confirm=bool(payload.get("confirm")),
                                        keep_data=payload.get("keep_data") is not False)
            self._send_json(out, 200 if out.get("ok") else 400)
        elif parsed.path == "/api/legion/mobile/apps/rollback":
            payload = self._read_json_body()
            if payload is None:
                return
            out = MOBILE_APPS.rollback(str(payload.get("package") or ""), str(payload.get("snapshot_id") or ""),
                                       confirm=bool(payload.get("confirm")))
            self._send_json(out, 200 if out.get("ok") else 400)
        elif parsed.path == "/api/legion/mobile/tasks/create":
            payload = self._read_json_body()
            if payload is None:
                return
            out = MOBILE_TASKS.create(str(payload.get("goal") or ""), uid=str(payload.get("uid") or "default"),
                                      required_apps=payload.get("required_apps") or [],
                                      context=payload.get("context") or {}, task_id=str(payload.get("task_id") or ""))
            self._send_json(out, 200 if out.get("ok") else 400)
        elif parsed.path == "/api/legion/mobile/tasks/confirm":
            payload = self._read_json_body()
            if payload is None:
                return
            out = MOBILE_TASKS.decide(str(payload.get("task_id") or ""),
                                      str(payload.get("confirmation_id") or ""),
                                      str(payload.get("decision") or ""), actor=str(payload.get("actor") or "user"),
                                      note=str(payload.get("note") or ""),
                                      expected_version=int(payload["version"]) if payload.get("version") is not None else None)
            self._send_json(out, 200 if out.get("ok") else 409)
        elif parsed.path == "/api/legion/mobile/tasks/resume":
            payload = self._read_json_body()
            if payload is None:
                return
            out = MOBILE_TASKS.resume(str(payload.get("task_id") or ""), steps=int(payload.get("steps") or 20),
                                      timeout_s=int(payload.get("timeout_s") or 600), model=str(payload.get("model") or ""))
            self._send_json(out, 200 if out.get("ok") else 400)
        elif parsed.path == "/api/legion/mobile/tasks/replay":
            payload = self._read_json_body()
            if payload is None:
                return
            out = MOBILE_TASKS.replay(str(payload.get("task_id") or ""),
                                      on_mismatch=str(payload.get("on_mismatch") or "agent"),
                                      start_from=int(payload.get("start_from") or 1),
                                      max_steps=int(payload.get("max_steps") or 0))
            self._send_json(out, 200 if out.get("ok") else 400)
        elif parsed.path == "/api/legion/mobile/companion/read":
            payload = self._read_json_body()
            if payload is None:
                return
            self._send_json(MOBILE_COMPANION.mark_read(
                str(payload.get("uid") or "default"), payload.get("ids") or []))
        elif parsed.path == "/api/legion/mobile/companion/preferences":
            payload = self._read_json_body()
            if payload is None:
                return
            out = MOBILE_COMPANION.save_preferences(str(payload.get("uid") or "default"),
                                                    payload.get("preferences") or {})
            self._send_json(out, 200 if out.get("ok") else 400)
        elif parsed.path == "/api/legion/mobile/companion/control":
            payload = self._read_json_body()
            if payload is None:
                return
            out = MOBILE_COMPANION.control(str(payload.get("task_id") or ""),
                                           str(payload.get("action") or ""),
                                           uid=str(payload.get("uid") or "default"),
                                           reason=str(payload.get("reason") or ""))
            self._send_json(out, 200 if out.get("ok") else 400)
        elif parsed.path == "/api/legion/cloud_desktop/fs/write":
            payload = self._read_json_body()
            if payload is None:
                return
            out = DESKTOP_FS.write_file(str(payload.get("uid") or "default"), str(payload.get("path") or ""), payload.get("content") or "", append=bool(payload.get("append")), content_base64=str(payload.get("content_base64") or ""))
            self._send_json(out, 200 if out.get("ok") else 400)
        elif parsed.path == "/api/legion/cloud_desktop/fs/mkdir":
            payload = self._read_json_body()
            if payload is None:
                return
            out = DESKTOP_FS.make_dir(str(payload.get("uid") or "default"), str(payload.get("path") or ""))
            self._send_json(out, 200 if out.get("ok") else 400)
        elif parsed.path == "/api/legion/cloud_desktop/exec":
            payload = self._read_json_body()
            if payload is None:
                return
            out = DESKTOP_FS.desktop_exec(str(payload.get("uid") or "default"), str(payload.get("command") or ""), payload.get("timeout_s") or 60)
            self._send_json(out, 200 if out.get("ok") else 400)
        elif parsed.path == "/api/legion/cloud_desktop/app":
            payload = self._read_json_body()
            if payload is None:
                return
            out = DESKTOP_FS.desktop_app(str(payload.get("uid") or "default"), str(payload.get("action") or "list"), str(payload.get("app") or ""), payload.get("args"))
            self._send_json(out, 200 if out.get("ok") else 400)
        elif parsed.path == "/api/legion/cloud_desktop/fs/open_file":
            # 把 ~/Meyo 交付物在真机桌面真实打开(网页控制真机·按类型选 GUI 应用)
            payload = self._read_json_body()
            if payload is None:
                return
            out = DESKTOP_FS.open_in_desktop(str(payload.get("uid") or "default"), str(payload.get("path") or ""), str(payload.get("app") or ""))
            self._send_json(out, 200 if out.get("ok") else 400)
        elif parsed.path == "/api/legion/cloud_desktop/fs/sync_personal":
            # 记忆/配置/会话等个人数据同步进用户卷(真机里看得见自己的全部数据)
            payload = self._read_json_body()
            if payload is None:
                return
            try:
                out = DESKTOP_FS.sync_personal_data(str(payload.get("uid") or "default"))
            except Exception as error:
                out = {"ok": False, "error": f"{type(error).__name__}: {error}"}
            self._send_json(out, 200 if out.get("ok") else 400)
        elif parsed.path == "/api/legion/cloud_desktop/screenshot":
            # 真机当前桌面截屏 → 持久卷 personal/screenshots(网页可见真机画面)
            payload = self._read_json_body()
            if payload is None:
                return
            out = DESKTOP_FS.desktop_screenshot(str(payload.get("uid") or "default"))
            self._send_json(out, 200 if out.get("ok") else 400)

        elif parsed.path == "/api/legion/cloud_desktop/install":
            payload = self._read_json_body()
            if payload is None:
                return
            import cloud_desktop
            self._send_json(cloud_desktop.install_control(str(payload.get("uid") or "default")))
        elif parsed.path == "/api/legion/cloud_desktop/session/start":
            payload = self._read_json_body()
            if payload is None:
                return
            import cloud_desktop
            self._send_json(cloud_desktop.start_session(str(payload.get("uid") or "default")))
        elif parsed.path == "/api/legion/cloud_desktop/session/stop":
            payload = self._read_json_body()
            if payload is None:
                return
            import cloud_desktop
            self._send_json(cloud_desktop.stop_session(str(payload.get("uid") or "default")))
        elif parsed.path == "/api/legion/external-agents/probe":
            payload = self._read_json_body()
            if payload is None:
                return
            import external_agent_registry as external_agents
            self._send_json(external_agents.probe(str(payload.get("key") or "")))
        elif parsed.path == "/api/legion/external-agents/apply":
            # 一键升级某外部 agent:升级→重装→probe 验证→失败自动回滚(验收=代码可自主运行)
            payload = self._read_json_body()
            if payload is None:
                return
            import external_agent_registry as external_agents
            key = str(payload.get("key") or "").strip()
            if not key:
                self._send_json({"ok": False, "error": "缺 key"})
                return
            try:  # 升级成功后编排池/harness 可用集立即生效
                LEGION.invalidate_harness_keys_cache()
                LEGION.invalidate_aigc_ready_cache()
            except Exception:
                pass
            self._send_json(external_agents.apply_update(
                key, str(payload.get("tag") or ""), str(payload.get("revision") or "")))
        elif parsed.path == "/api/legion/external-agents/auto-update":
            # 立即跑一轮自动更新(force 绕过周二/五窗口);结果含每家升级/回滚明细
            payload = self._read_json_body()
            if payload is None:
                return
            import external_agent_registry as external_agents
            self._send_json(external_agents.auto_update_all(force=bool(payload.get("force"))))
        elif parsed.path == "/api/legion/install/register":
            # 注册新可装项:默认用户私有(scope=user·不污染官方);scope=official进共享扩展
            payload = self._read_json_body()
            if payload is None:
                return
            item = payload.get("item") or payload
            scope = str(payload.get("scope") or "user")
            uid = payload.get("uid")
            self._send_json(INSTALL.register_item(item, scope=scope, uid=uid))
        elif parsed.path == "/api/legion/install/unregister":
            # 只允许删用户私有项;官方项保护不可删
            payload = self._read_json_body()
            if payload is None:
                return
            self._send_json(INSTALL.unregister_item(str(payload.get("id") or ""), payload.get("uid")))
        elif parsed.path == "/api/legion/upgrades/publish":
            # 官方发布新能力→bump版本+changelog→所有用户可见升级
            payload = self._read_json_body()
            if payload is None:
                return
            self._send_json(INSTALL.publish_official(payload.get("item") or {},
                                                     payload.get("changelog") or ""))
        elif parsed.path == "/api/legion/upgrades/apply":
            # 用户升级:{id / all, confirm, uid}·confirm=false只回计划
            payload = self._read_json_body()
            if payload is None:
                return
            confirm = bool(payload.get("confirm"))
            uid = payload.get("uid")
            if payload.get("all"):
                self._send_json(INSTALL.apply_all_upgrades(confirm=confirm, uid=uid))
            elif payload.get("id"):
                self._send_json(INSTALL.apply_upgrade(str(payload["id"]), confirm=confirm, uid=uid))
            else:
                self._send_json({"ok": False, "error": "缺 id 或 all"}, 400)
        elif parsed.path == "/api/legion/upgrades/ack":
            # 用户"知道了"·不再重复提示
            payload = self._read_json_body()
            if payload is None:
                return
            self._send_json(INSTALL.ack_upgrades(payload.get("uid") if payload else None))
        elif parsed.path == "/api/legion/activity/seen":
            # 标记今日动态已读(每日节流·当天不再提醒)
            payload = self._read_json_body()
            if payload is None:
                return
            self._send_json(INSTALL.mark_activity_seen(payload.get("uid") if payload else None))
        elif parsed.path == "/api/legion/models/discovery/ack":
            # 用户看过NEW模型后清标(模型仍保留在catalog)
            payload = self._read_json_body()
            if payload is None:
                return
            self._send_json(MODELS.ack_new_models())
        elif parsed.path == "/api/legion/model_pref":
            # 用户为某家固定模型;model空=清除回智能默认
            payload = self._read_json_body()
            if payload is None:
                return
            key = str(payload.get("harness") or "")
            if not key:
                self._send_json({"error": "缺 harness"}, 400)
                return
            # 校验 harness 必须是真实成员(否则任意字符串会污染 model_prefs·如误传路径/脏值)。
            # 清除偏好(model 为空)放行任意 key——允许清掉历史遗留的脏 key。
            model = payload.get("model")
            if model and key not in set(LEGION._harness_keys()):
                self._send_json({"error": f"未知 harness: {key}"}, 400)
                return
            result = MODELS.set_pref(key, model)
            if result.get("ok"):
                import harness_view
                harness_view.update_model(key, MODELS.resolve_model(key))
            self._send_json(result)
        elif parsed.path == "/api/legion/search_source_toggle":
            # P1-7 搜索源自定义:用户开/关某个搜索源(存每用户偏好·军团搜索时读)
            payload = self._read_json_body()
            if payload is None:
                return
            try:
                import search_prefs
                self._send_json(search_prefs.toggle(
                    str(payload.get("uid") or "default"),
                    str(payload.get("source_id") or ""),
                    bool(payload.get("enabled", True))))
            except Exception as exc:
                self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 200)
        elif parsed.path == "/api/legion/cron":
            # cron 创建/删除(本机原型:登记+过期·不真触发)
            payload = self._read_json_body()
            if payload is None:
                return
            action = str(payload.get("action") or "create")
            if action == "delete":
                self._send_json(CRON.delete_job(str(payload.get("id") or "")))
            elif action == "purge_expired":
                self._send_json(CRON.purge_expired())
            else:
                out = CRON.create_job(
                    str(payload.get("question") or ""), str(payload.get("cron") or ""),
                    mode=str(payload.get("mode") or "pk"),
                    ttl_seconds=int(payload.get("ttl_seconds") or 0) or None,
                    expires_at=float(payload["expires_at"]) if payload.get("expires_at") else None,
                    note=str(payload.get("note") or ""),
                    user_id=str(payload.get("uid") or payload.get("user_id") or "default"),
                    project_id=str(payload.get("project_id") or ""),
                    task_id=str(payload.get("task_id") or ""))
                # 挂项目(2026-09-07 打通做项目/盯变化):定时任务归属项目·执行结果与状态
                # 通过 objective_workspace 的 source 投影回流项目详情。
                _project_id = str(payload.get("project_id") or "")
                if _project_id and not out.get("error"):
                    try:
                        import objective_workspace as _ow
                        _linked = _ow.link_cron(
                            str(payload.get("uid") or "default"), _project_id,
                            str(payload.get("task_id") or ""), str(out.get("id") or ""))
                        if _linked.get("ok"):
                            out["project_id"] = _project_id
                    except Exception as _exc:
                        out["link_error"] = f"{type(_exc).__name__}: {_exc}"
                self._send_json(out, 400 if out.get("error") else 201)
        elif parsed.path == "/api/legion/cron/run_now":
            # 立即真触发一次(用户点"现在就跑"·走军团 dispatch·结果进 history)
            payload = self._read_json_body()
            if payload is None:
                return
            try:
                import cron_dispatch
                job_id = str(payload.get("id") or "")
                jobs = cron_dispatch._read_jobs()
                job = next((j for j in jobs if str(j.get("id") or "") == job_id), None)
                if not job:
                    self._send_json({"ok": False, "error": "job not found"}, 404)
                    return
                def _run_one(q, mode, user_id):
                    import legion_core
                    return legion_core.dispatch(q, mode=mode)
                # 强制到期触发一次:临时把 next_fire_at 归零再调 run_due
                with cron_dispatch._LOCK:
                    job["next_fire_at"] = 0.0
                    cron_dispatch._write_jobs(jobs)
                out = cron_dispatch.run_due(runner=_run_one)
                self._send_json(out)
            except Exception as e:
                self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        elif parsed.path == "/api/legion/session_continuous":
            # 标记 session 为持续生成型(网页每30秒自动刷新看最新·可挂 cron 持续续写)
            payload = self._read_json_body()
            if payload is None:
                return
            sid = str(payload.get("sid") or "")
            r = SESSIONS.set_continuous(sid, bool(payload.get("on", True)),
                                        str(payload.get("interval_hint") or ""),
                                        user_id=str(payload.get("uid") or "default"))
            self._send_json(r, 404 if r.get("error") else 200)
        elif parsed.path == "/api/legion/pilot":
            # 保存"持续运行"设置(开关/强度/拦哪些·每用户覆盖·只存白名单字段)
            payload = self._read_json_body()
            if payload is None:
                return
            uid = str(payload.get("uid") or "default")
            self._send_json(LEGION.pilot_save_settings(uid, payload.get("config") or {}))
        elif parsed.path == "/api/legion/evolve/config":
            # 保存自进化配置(用户 8-30 澄清:用户自己配置·enabled/targets/schedule/reflector/auto_apply)
            payload = self._read_json_body()
            if payload is None:
                return
            try:
                import evolve_config
                self._send_json(evolve_config.save_config(str(payload.get("uid") or "default"),
                                                          payload.get("config") or {}))
            except Exception as e:
                self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        elif parsed.path == "/api/legion/evolve/run":
            # 触发一次进化(用户点"立即进化"·产出 diff 存进 evolutions/·不自动应用除非 auto_apply)
            payload = self._read_json_body()
            if payload is None:
                return
            try:
                import evolve_config
                self._send_json(evolve_config.run_evolution(str(payload.get("uid") or "default")))
            except Exception as e:
                self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        elif parsed.path == "/api/legion/wiki/consolidate":
            # WikiSkill:手动触发一次经验沉淀(Wiki Maintainer·把 recent 轨迹整合进持久 wiki)
            payload = self._read_json_body()
            if payload is None:
                return
            try:
                import evolve_config
                self._send_json(evolve_config.wiki_consolidate(str(payload.get("uid") or "default")))
            except Exception as e:
                self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        elif parsed.path == "/api/legion/evolve/apply":
            # 应用某次进化产物(用户面板点"应用"·把 diff.after 写入 active_pref)
            payload = self._read_json_body()
            if payload is None:
                return
            try:
                import evolve_config
                self._send_json(evolve_config.apply_evolution(
                    str(payload.get("uid") or "default"), str(payload.get("id") or "")))
            except Exception as e:
                self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        elif parsed.path == "/api/legion/evolve/rollback":
            # 回滚(用户点"回滚"·清空 active_pref·退回未进化状态)
            payload = self._read_json_body()
            if payload is None:
                return
            try:
                import evolve_config
                self._send_json(evolve_config.rollback(str(payload.get("uid") or "default")))
            except Exception as e:
                self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        elif parsed.path == "/api/legion/memory/delete":
            # 配置自己的md:用户删除自己的某条记忆(按ts+q定位·只删属于该uid的·不能删别人的)
            payload = self._read_json_body()
            if payload is None:
                return
            try:
                import legion_memory
                self._send_json(legion_memory.delete(int(payload.get("ts") or 0),
                                                     str(payload.get("q") or ""),
                                                     str(payload.get("uid") or "default")))
            except Exception as e:
                self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        elif parsed.path == "/api/legion/captain":
            payload = self._read_json_body()
            if payload is None:
                return
            uid = str(payload.get("uid") or "default")
            try:
                import captain_preferences as _cp
                # 前端(index_v2.html:2048/1858)把 mode/preferred_captain/candidates 放在 payload
                # 顶层·不套 config;旧代码只读 payload["config"]→永远存空→设首脑静默失效(loop dogfood
                # 揪出:面板能开·点保存无反应)。兼容两种形状:有 config 用 config·否则用顶层字段。
                config = payload.get("config")
                if not isinstance(config, dict) or not config:
                    config = {k: payload[k] for k in ("mode", "preferred_captain", "candidates")
                              if k in payload}
                self._send_json(_cp.save(uid, config, LEGION._harness_keys()))
            except Exception as exc:
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 400)
        elif parsed.path == "/api/legion/pilot_check":
            # 一轮结束调它:该继续推进目标还是可以停(军团按钮开着时用)
            payload = self._read_json_body()
            if payload is None:
                return
            uid = str(payload.get("uid") or "default")
            self._send_json(LEGION.pilot_check(str(payload.get("reply") or ""),
                                               payload.get("state") or {}, uid))
        elif parsed.path == "/api/legion/approval/request":
            payload = self._read_json_body()
            if payload is None:
                return
            result = APPROVALS.request(str(payload.get("team_id") or ""),
                                       str(payload.get("agent") or ""),
                                       str(payload.get("capability") or payload.get("tool") or ""),
                                       payload.get("arguments") or {}, str(payload.get("task_key") or ""),
                                       str(payload.get("reason") or ""), int(payload.get("ttl_s") or 3600))
            self._send_json(result, 200 if result.get("ok") else 400)
        elif parsed.path == "/api/legion/approval/decide":
            payload = self._read_json_body()
            if payload is None:
                return
            expected = payload.get("version")
            result = APPROVALS.decide(str(payload.get("team_id") or ""),
                                      str(payload.get("approval_id") or ""),
                                      str(payload.get("decision") or ""),
                                      str(payload.get("actor") or "user"),
                                      str(payload.get("reason") or ""),
                                      int(expected) if expected is not None else None)
            # 状态码按失败类型区分(语义正确·前端可据此分流):成功/幂等unchanged=200·
            # 输入非法(decision不是approve/deny)=400·审批不存在=404·版本冲突=409。
            if result.get("ok") or result.get("status") == "unchanged":
                code = 200
            elif result.get("error") == "decision must be approve/deny":
                code = 400
            elif result.get("error") == "approval not found":
                code = 404
            else:
                code = 409
            self._send_json(result, code)
        elif parsed.path == "/api/legion/skill_factory/install":
            # 装一个 Skill(用户隔离·安全审计)。confirm=false 先给审计预览·true 才真装(产品policy默认可自主)
            payload = self._read_json_body()
            if payload is None:
                return
            r = SKILLFACTORY.install_local(str(payload.get("source") or ""),
                                           str(payload.get("name") or "skill"),
                                           uid=str(payload.get("uid") or "default"),
                                           revision=str(payload.get("revision") or "local"),
                                           confirm=bool(payload.get("confirm", False)))
            self._send_json(r, 200 if r.get("ok") else 400)
        elif parsed.path == "/api/legion/skill_factory/feedback":
            # Skill 使用反馈进经验飞轮(用没用/效果好不好·学习优化上下文)
            payload = self._read_json_body()
            if payload is None:
                return
            r = SKILLFACTORY.feedback(str(payload.get("name") or ""), str(payload.get("task") or ""),
                                      str(payload.get("result") or ""), str(payload.get("agent") or "user"),
                                      uid=str(payload.get("uid") or "default"))
            self._send_json(r, 200 if r.get("ok") else 400)
        elif parsed.path == "/api/legion/team/message":
            # 用户/总指挥/成员给团队邮箱发消息·持久化到 team JSON。
            payload = self._read_json_body()
            if payload is None:
                return
            team_id = str(payload.get("team_id") or "")
            recipient = str(payload.get("recipient") or "")
            text = str(payload.get("text") or "").strip()
            sender = str(payload.get("sender") or "commander")
            if not team_id or not recipient or not text:
                self._send_json({"error": "缺 team_id/recipient/text"}, 400)
                return
            state = TEAMS.load(team_id)
            if state is None:
                self._send_json({"error": "team not found"}, 404)
                return
            uid = str(_uid_from_request(self) or "")
            sid = str(state.get("conversation_id") or team_id)
            if not self._project_owner_allowed(team_id, uid, sid):
                return
            execute = payload.get("continue_execution") is True
            request_id = str(payload.get("request_id") or "").strip()
            if execute and (not request_id or recipient != "commander"):
                self._send_json({"error": "继续执行需要 request_id 和原团队 commander"}, 400)
                return
            try:
                message = TEAMS.send_message(
                    team_id, "user" if execute else sender, recipient, text,
                    execution_request_id=request_id if execute else "", user_id=uid)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 409)
                return
            self._send_json(message, 201)
            if execute and (message.get("continuation") or {}).get("status") == "pending":
                _schedule_feedback_resume(team_id, uid, sid)
        elif parsed.path == "/api/legion/team/resume":
            payload = self._read_json_body()
            if payload is None:
                return
            team_id = str(payload.get("team_id") or "").strip()
            state = TEAMS.load(team_id)
            if not state:
                self._send_json({"ok": False, "error": "team not found"}, 404)
                return
            _resume_uid = str(payload.get("uid") or state.get("user_id") or "default")
            _resume_sid = str(payload.get("session_id") or state.get("conversation_id") or team_id)
            if not self._project_owner_allowed(team_id, _resume_uid, _resume_sid):
                return
            try:
                response = _resume_saved_team(
                    team_id, explicit=True, user_id=_resume_uid, conversation_id=_resume_sid)
            except PermissionError as exc:
                self._send_json({"ok": False, "error": "permission_denied", "reason": str(exc)}, 403)
                return
            self._send_json(response)
        elif parsed.path == "/api/legion/cancel":
            # 真取消(军团自审 cc/penguin/codex P0):整单+单成员两级。
            # key空=停止整单；key有值=只停该成员。API只设取消令牌+持久标记·拥有Popen的执行线程
            # 在下一次监控tick自己安全SIGTERM→宽限→SIGKILL,不从HTTP线程跨线程硬杀(避免竞态/误杀)。
            payload = self._read_json_body()
            if payload is None:
                return
            # 觅游用户身份：ticket 换 uid + team 归属校验（只能停自己的任务）
            _cancel_uid = _uid_from_request(self, body=payload)
            if not _cancel_uid:
                self._send_json({"ok": False, "error": "unauthorized",
                                 "reason": "登录状态已失效，请刷新页面重新进入"}, 401)
                return
            team_id = str(payload.get("team_id") or payload.get("qhash") or "").strip()
            try:
                _cancel_team = TEAMS.load(team_id) or {}
                _cancel_owner = str(_cancel_team.get("user_id") or "")
            except Exception:
                _cancel_owner = ""
            if _cancel_owner and _cancel_owner != _cancel_uid:
                self._send_json({"ok": False, "error": "forbidden",
                                 "reason": "该任务不属于当前用户"}, 403)
                return
            key = str(payload.get("key") or "").strip()
            reason = str(payload.get("reason") or "用户停止").strip()[:200]
            if not team_id:
                self._send_json({"ok": False, "error": "缺 team_id"}, 400)
                return
            if TEAMS.load(team_id) is None:
                self._send_json({"ok": False, "error": "team not found"}, 404)
                return
            state = TEAMS.load(team_id) or {}
            requested_scope = str(payload.get("scope") or "")
            if key and requested_scope not in {"task", "harness"}:
                requested_scope = "task" if key in (state.get("tasks") or {}) else "harness"
            TEAMS.request_cancel(team_id, key=key, reason=reason, scope=requested_scope)
            out = LEGION.request_cancel(team_id, key or None, reason, scope=requested_scope)
            # A new Stop also reaches an admitted feedback invocation, including
            # the interval before its dispatcher has created the child state.
            cancelled_state = TEAMS.load(team_id) or {}
            for message in cancelled_state.get("mailboxes", {}).get("commander", []):
                request = message.get("continuation") or {}
                if request.get("status") != "running" or not request.get("team_id"):
                    continue
                child = request["team_id"]
                for stop in _feedback_cancel_requests(cancelled_state, message):
                    stop_key = str(stop.get("key") or "")
                    stop_key = "" if stop_key == "__all__" else stop_key
                    stop_reason, stop_scope = stop.get("reason") or "用户停止", stop.get("scope") or ""
                    TEAMS.request_cancel(child, key=stop_key, reason=stop_reason, scope=stop_scope,
                                         source_team_id=team_id, source_sequence=int(stop.get("sequence") or 0))
                    LEGION.request_cancel(child, stop_key or None, stop_reason, scope=stop_scope)
            self._send_json({**out, "team_id": team_id, "key": key or None})
        elif parsed.path == "/api/legion/session_append":
            # 对已有 session 再跑一轮军团并追加(持续生成内容·继续对话的服务端形式)
            payload = self._read_json_body()
            if payload is None:
                return
            sid = str(payload.get("sid") or "")
            question = str(payload.get("question") or "").strip()
            if not sid or not question:
                self._send_json({"error": "missing sid/question"}, 400)
                return
            _uid = str(payload.get("uid") or "default")
            if SESSIONS.load_session(sid, user_id=_uid) is None:
                self._send_json({"error": "session not found"}, 404)
                return
            mode = str(payload.get("mode") or "pk")
            _t0 = time.time()
            _append_plan = dict(payload.get("plan") or {})
            _append_plan["_user_selected_mode"] = True
            import personal_identity
            try:
                personal_identity.bind_plan_attachments(_append_plan, _uid, sid)
            except PermissionError:
                self._send_json({"error": "attachment_not_authorized"}, 403)
                return
            out = LEGION.dispatch(question, plan=_append_plan,
                                  cb=None, mode=mode if mode in ("pk", "relay") else "pk",
                                  user_id=_uid, conversation_id=sid)
            # 产物隔离(S3 根因②):只扫本单各成员 workdir·并发时不收别单文件
            _wds = [str(r.get("workdir") or "") for r in out.get("results", [])]
            out.setdefault("question", question)
            ARTIFACTS.enrich(out, since_ts=_t0 - 2, workdirs=_wds or None)
            try:
                import task_outcome as _task_outcome
                _task_outcome.attach(out, out.get("delivery_contract") or _append_plan.get("delivery_contract"))
            except Exception:
                pass
            info = SESSIONS.save_turn(sid, question, out, mode=mode, user_id=_uid)
            try:
                __import__("performance_views").invalidate(_uid)
                __import__("user_view").invalidate(_uid)
            except Exception:
                pass
            try:
                desktop_session = DESKTOP_FS.materialize_session(_uid, sid, out)
                try:
                    DESKTOP_FS.sync_personal_data(_uid)
                except Exception:
                    pass
            except Exception as error:
                desktop_session = {"ok": False, "error": f"{type(error).__name__}: {error}"}
            self._send_json({"session": info, "appended": True,
                             "results": out.get("results", []), "desktop_session": desktop_session})
        elif parsed.path == "/api/legion/crawl":
            # 持续爬取:订阅URL/移除/立即爬(变化则追加进session交付物网页)
            payload = self._read_json_body()
            if payload is None:
                return
            action = str(payload.get("action") or "add")
            sid = str(payload.get("sid") or "")
            _uid = str(payload.get("uid") or "default")
            if not sid:
                self._send_json({"error": "missing sid"}, 400)
                return
            if action == "remove":
                self._send_json(CRAWLER.remove_source(sid, str(payload.get("url") or "")))
            elif action == "crawl_now":
                # 用 sessions.save_turn 作为 append_cb:变化内容追加进 session
                def _append(q, res, mode):
                    SESSIONS.save_turn(sid, q, res, mode=mode, user_id=_uid)
                self._send_json(CRAWLER.crawl_now(sid, append_cb=_append))
            else:  # add
                r = CRAWLER.add_source(sid, str(payload.get("url") or ""),
                                       str(payload.get("note") or ""))
                self._send_json(r, 400 if r.get("error") else 201)
        elif parsed.path == "/run":
            # 通用验收模板别名:body {"query": "..."} → 全军团出击(JSON 一次性返回)
            payload = self._read_json_body()
            if payload is None:
                return
            question = str(payload.get("query") or payload.get("question") or "").strip()
            if not question:
                self._send_json({"error": "missing query"}, 400)
                return
            out = LEGION.dispatch(question, plan=None, cb=None, user_id=self._personal_user())
            self._send_json(out)
        else:
            self.send_response(404)
            self.end_headers()

    def _project_owner_allowed(self, team_id: str, user_id: str, conversation_id: str) -> bool:
        """在写 Team/Project/事件及调用模型前核对已有归属，不创建新的身份系统。"""
        import project_context
        try:
            project_context.require_owner(TEAMS.load(team_id), user_id, conversation_id)
            project_context.require_owner(project_context.load(team_id), user_id, conversation_id)
        except PermissionError as exc:
            self._send_json({"ok": False, "error": "permission_denied", "reason": str(exc)}, 403)
            return False
        return True

    def _single_continuation_context(self, sid: str, uid: str, key: str) -> dict:
        """单家续接沿用户会话找原生已接受的目录，不采纳发送前拒绝的临时现场。"""
        context = SESSIONS.continuation_project_context(sid, user_id=uid)
        session = SESSIONS.load_session(sid, user_id=uid) or {}
        import project_context
        target = None
        for turn in reversed(session.get("turns") or []):
            team_id = str(turn.get("team_id") or "")
            packet = project_context.load(team_id) if team_id else {}
            if (not packet or str(packet.get("conversation_id") or "") != sid
                    or str(packet.get("user_id") or "") not in {uid, "sess:" + sid}):
                continue
            if (team_id == context.get("continued_from_team_id")
                    and (packet.get("mode") == "pk" or packet.get("candidate_tasks")
                         or (packet.get("visibility") or {}).get("candidate_isolation") == "strict")):
                # PK 总包的目录不能代表任一候选的原生现场；只能由下面同成员、
                # 同 checkpoint 的结果提供目录，不能借别家或总包路径续接。
                context = {name: value for name, value in context.items()
                           if name not in ("repo_root", "workspace_root")}
            for result in reversed(turn.get("results") or []):
                if not isinstance(result, dict) or str(result.get("key") or result.get("harness")) != key:
                    continue
                checkpoint = result.get("native_checkpoint") or {}
                if not isinstance(checkpoint, dict) or not checkpoint.get("session_id"):
                    continue
                # SQLite 等原生现场由数据库路径和会话 ID 共同定位，不能只比 SID。
                identity = {name: checkpoint.get(name) for name in
                            ("kind", "session_id", "path", "request_binding")}
                if target is None:
                    target = identity
                if identity != target or result.get("native_resume_error"):
                    continue
                cwd = result.get("workdir")
                if not isinstance(cwd, str) or not cwd:
                    continue
                # 不拼目录、不扫描别人的 team、不更改 checkpoint；既有 workdir 解析器
                # 在执行前仍检查绝对路径、可访问性和受管目录符号链接边界。
                return {**context, "repo_root": cwd, "workspace_root": cwd,
                        "continued_from_team_id": team_id}
        if target is not None:
            # 已有原生 checkpoint 却找不到可信目录，不能退回被拒绝轮的临时目录。
            # 保留 checkpoint，让原生绑定检查如实拒绝；不冒用另一会话的目录。
            context = {name: value for name, value in context.items()
                       if name not in ("repo_root", "workspace_root")}
        return context

    def _handle_run_one(self, payload: dict):
        key = str(payload.get("key") or "").strip()
        task = str(payload.get("task") or "").strip()
        parent_ctx = str(payload.get("parent_context") or "").strip()
        node_id = str(payload.get("node_id") or key)
        if not key or not task:
            self._send_json({"error": "missing key/task"}, 400)
            return
        full_task = task + (f"\n上级结论(供你核对/深化)：{parent_ctx[:500]}" if parent_ctx else "")
        _uid = str(payload.get("uid") or "default")
        # ★run_one 也要多轮上下文(2026-09-02 L3 验收揪出·同 dispatch solo 修法):run_one
        #   不拼 conversation_context → 同 session_id 续跑每轮裸问题 → 第二轮答不出第一轮
        #   刚说的暗号(agent.log 实锤 history=0·模型被迫开 terminal 翻文件找)。
        #   把 dispatch 同款 conversation_context 拼进任务文(context not control·续接不重问)。
        _single_sid = str(payload.get("session_id") or SESSIONS.new_session_id(task))
        _single_qhash = str(payload.get("team_id") or f"runone_{_single_sid}_{int(time.time())}")
        if not self._project_owner_allowed(_single_qhash, _uid, _single_sid):
            return
        if _single_sid:
            try:
                _hctx = str(SESSIONS.conversation_context(_single_sid, user_id=_uid) or "").strip()
            except Exception:
                _hctx = ""
            if _hctx:
                full_task += ("\n\n【本对话最近历史·只读上下文·请自然续接,不要重新问已回答的信息】\n\n"
                              + _hctx)
        # 单家路径也构建与 dispatch 相同的前置上下文：经验召回、交付现场、会话现场都要真到本轮。
        # 以前这里只补 runtime_brief，导致 `_recall_experience` 挂着但 `/run-one` 永远不调用。
        _single_plan = {"user_profile": payload.get("user_profile"),
                        "acceptance": payload.get("acceptance"),
                        "deadline": payload.get("deadline"),
                        "delivery_contract": dict(payload.get("delivery_contract") or {}),
                        "tasks": {key: task}, "hierarchy": {},
                        "project_context": (dict(payload["project_context"])
                            if isinstance(payload.get("project_context"), dict) else
                            self._single_continuation_context(_single_sid, _uid, key))}
        if not self._project_access_allowed(_single_plan["project_context"], _uid, _single_sid):
            return
        for _context_field in ("input_snapshot", "version_identity", "resource_budget",
                               "experience_intent", "golden_task", "quality_context",
                               "model_overrides", "directions", "capability_policy"):
            if payload.get(_context_field) not in (None, "", [], {}):
                _single_plan[_context_field] = payload[_context_field]
        if "attachments" in payload:
            _single_plan["attachments"] = payload["attachments"]
        import personal_identity
        try:
            personal_identity.bind_plan_attachments(_single_plan, _uid, _single_sid)
        except PermissionError:
            self._send_json({"error": "attachment_not_authorized"}, 403)
            return
        _single_plan["input_snapshot"] = {
            **dict(_single_plan.get("input_snapshot") or {}),
            "attachments": deepcopy(_single_plan.get("attachments") or []),
        }
        _single_source_ref = _query_source_ref(payload.get("source_ref"))
        _single_role = str(payload.get("execution_role") or "champion").strip().lower()
        _single_role = _single_role if _single_role in {"champion", "challenger"} else "champion"
        _single_plan["source_ref"] = _single_source_ref
        _single_plan["execution_role"] = _single_role
        if isinstance(payload.get("team_policy"), dict):
            _single_plan["team_policy"] = dict(payload["team_policy"])
        if payload.get("native_session_scope") is not None:
            _single_plan["team_policy"] = {**dict(_single_plan.get("team_policy") or {}),
                                           "native_session_scope": payload["native_session_scope"]}
        full_task += LEGION._build_prelude(task, _single_plan, user_id=_uid)
        # 运行时简报下放(军团复验揪出的真缺口:run_one 单家路径漏传·6家反馈"没收到简报值"·
        # 双路径不对称)。单家也该拿到机器/网络/用户画像/验收(分工单家为空自然不拼)。
        _brief = LEGION._build_runtime_brief(task, _single_plan, user_id=_uid)
        self._sse_open()
        alive = {"ok": True}
        _single_stream = {"team_id": "", "milestone": None}
        _single_attempt = None
        _single_attempt_started = None
        _single_session_ready = False
        _single_turn_id = secrets.token_hex(16)  # 父层请求身份，不是新执行代次或 native UUID。

        def _single_send(event_type: str, data: dict) -> None:
            if ARTIFACTS._is_private_artifact_event(data):
                return
            rec = None
            if _single_stream["team_id"]:
                try:
                    rec = TEAMS.append_event(_single_stream["team_id"], data, event_type)
                except Exception:
                    rec = None
            if alive["ok"] and not self._sse_send(
                    event_type, data, event_id=(rec or {}).get("event_id")):
                alive["ok"] = False

        def cb(ev):
            if ARTIFACTS._is_private_artifact_event(ev):
                return
            # 与 dispatch 相同：业务进展续租，界面心跳不能掩盖卡死。
            if _single_attempt is not None and LEGION._event_advances_attempt(ev):
                try:
                    TEAMS.renew_attempt(_single_qhash, key, _single_attempt)
                except Exception:
                    pass
            enriched = {**ev, "node_id": node_id, "task_id": key,
                        "team_id": _single_qhash, "attempt": _single_attempt}
            milestone = _single_stream.get("milestone")
            if milestone and ev.get("t") == "artifact_ready":
                try:
                    milestone.observe_artifact(enriched)
                except Exception:
                    pass
            _single_send("step", enriched)

        def _single_claim():
            """只记账；实际执行仍走下面原有的 run_one。"""
            state = TEAMS.load(_single_qhash) or {}
            node = (state.get("tasks") or {}).get(key) or {}
            if _single_cancel() or TEAMS._cancel_request_for(state, key, node):
                TEAMS.cancel_queued(_single_qhash, key, "用户停止")
                return None
            personal_identity.bind_plan_attachments(_single_plan, _uid, _single_sid)
            if (node.get("active_attempt") is None and node.get("result")
                    and node.get("status") in
                    {"done", "partial", "error", "timeout", "interrupted", "incomplete"}):
                if not TEAMS.reopen_task(_single_qhash, key, reason="single_continuation"):
                    raise RuntimeError("single task is already running")
            try:
                return TEAMS.begin_attempt(_single_qhash, key, owner=f"server:{os.getpid()}")
            except RuntimeError:
                # 取消可发生在读取/重开与原子领取之间；不复活，也不伪造一个执行代次。
                state = TEAMS.load(_single_qhash) or {}
                node = (state.get("tasks") or {}).get(key) or {}
                if _single_cancel() or TEAMS._cancel_request_for(state, key, node):
                    TEAMS.cancel_queued(_single_qhash, key, "用户停止")
                    return None
                raise

        def _single_finish(result):
            nonlocal _single_attempt
            result.update(key=key, task_id=key, assigned_harness=key)
            result.setdefault("harness", key)
            # 与 TeamState 的规范化一致；缺失模型不是另一份不相等的身份。
            result["model_used"] = str(result.get("model_used") or result.get("model") or "")
            if _single_attempt is None:
                return True  # 领取前取消，不造 begin/finish 记录。
            accepted = TEAMS.finish_attempt(_single_qhash, key, _single_attempt, result)
            _single_attempt = None
            return accepted

        def _single_save_result(result):
            # 正常返回与异常共用同一身份投影；只在 owned session 占位成功后调用。
            try:
                _save_out = {"results": [dict(result)], "team_id": _single_qhash,
                             "delivered_count": 1 if result.get("status") == "done" else 0,
                             "total": 1, "all_failed": result.get("status") != "done",
                             "cancelled": (result.get("status") == "cancelled"
                                           or result.get("cancelled") is True),
                             "delivery_contract": dict(_single_plan.get("delivery_contract") or {})}
                for field in ("pilot", "persistent_rounds", "persistent_diagnostics", "persistent_capped"):
                    if field in result:
                        _save_out[field] = result[field]
                import task_outcome as _task_outcome
                _task_outcome.attach(_save_out, _save_out["delivery_contract"])
                for field in ("outcome", "delivery_contract", "partial", "contract_incomplete",
                              "cancelled", "delivered_count", "total", "all_failed"):
                    result[field] = _save_out[field]
                result["source_ref"] = dict(_single_source_ref)
                result["version_identity"] = dict(_single_plan.get("version_identity") or {})
                result["execution_role"] = _single_role
                result["session"] = SESSIONS.save_turn(
                    _single_sid, task, _save_out, mode="single", user_id=_uid,
                    source_ref=_single_source_ref, turn_id=_single_turn_id,
                    input_snapshot=dict(_single_plan.get("input_snapshot") or {}),
                    outcome_contract=dict(_single_plan.get("delivery_contract") or {}),
                    version_identity=dict(_single_plan.get("version_identity") or {}))
            except Exception:
                pass  # 保持原有会话落盘失败不吞掉执行结果的行为。

        # 单家自循环(双路径对称·补 dispatch 已有、run_one 漏的自循环缺口):pilot 开启时,
        # run_one 跑完→pilot 判没完成→带上一轮结果续跑同一家·直到 done/阻塞/上限(防跑飞)。
        # 手动 fork 的子 agent 也能咬住目标多轮推进·不再答一轮就停。context not control:
        # pilot 只提醒不否决·续跑是多给机会·pilot 关就退化单轮(与 dispatch_persistent 一致)。
        try:
            _pilot_on = bool(LEGION.pilot_settings(_uid).get("enabled"))
        except Exception:
            _pilot_on = False
        # quick 是单轮原生快答：若它已 status=done，合法短答案就是完成，不再让 Pilot 因
        # “太短”误判并连续重跑 6 轮。quick 失败/请求升级仍会走下面正常续跑或上层升级链。
        _requested_depth = str(payload.get("execution_depth") or "standard")
        try:
            import legion_pilot as _lp
            # 单家接口同样区分逻辑会话和本轮执行；没传 session_id 时每次生成新槽，不能让
            # 所有匿名 run-one 请求共享 `run-one:<key>` 缓存/原生会话。
            # (_single_sid 已在函数头统一解析:外层拼 conversation_context 也用它。)
            _single_stream["team_id"] = _single_qhash
            # standalone run_one 也进入可取消 TeamState；否则公开 cancel API 会 404，
            # 用户只能关 SSE，后台 Harness 仍会继续运行。
            try:
                TEAMS.ensure(_single_qhash, task,
                             {key: {"task": task, "assigned_harness": key}}, {key: []},
                             orchestration_spec={
                                 "pattern": "single", "auto_resume": False,
                                 **({"team_policy": dict(_single_plan["team_policy"])}
                                    if _single_plan.get("team_policy") else {})},
                             delivery_contract=dict(_single_plan.get("delivery_contract") or {}),
                             user_id=_uid, conversation_id=_single_sid,
                             source_ref=_single_source_ref,
                             input_snapshot=dict(_single_plan.get("input_snapshot") or {}),
                             version_identity=dict(_single_plan.get("version_identity") or {}),
                             quality_context=dict(_single_plan.get("quality_context") or {}),
                             execution_role=_single_role,
                             # 校验与初始化共用 ensure 的锁，不能先预检再重绑旧邮箱。
                             owner_check=lambda state: LEGION._require_mail_context_owner(
                                 state, _single_qhash, _uid, _single_sid, bound_team=True))
            except PermissionError:
                raise
            except Exception:
                pass
            _single_cancel = lambda: LEGION._request_cancelled(_single_qhash, key, key)
            _single_project_context = dict(_single_plan.get("project_context") or {})
            try:
                import project_context as _project_context
                import legion_sandbox as _sandbox
                # Existing project roots keep their original resolver. A NEW
                # default directory is allocated by shared run_one admission,
                # after the owned live session and current attempt are bound.
                if _single_project_context.get("repo_root") or _single_project_context.get("workspace_root"):
                    _single_project_context["workspace_root"] = _sandbox.workdir(
                        key, _single_qhash, _single_project_context, _uid)
                _single_plan["project_context"] = _single_project_context
                _single_project_context = _project_context.initialize(
                    _single_qhash, task, _single_plan, mode="single", user_id=_uid,
                    conversation_id=_single_sid)
                _single_project_context = _project_context.snapshot(
                    _single_qhash, key, packet=_single_project_context)
            except PermissionError:
                raise
            except Exception:
                pass
            try:
                from delivery_progress import MilestoneEmitter
                _single_stream["milestone"] = MilestoneEmitter(
                    lambda event: _single_send("step", {**event, "node_id": node_id}),
                    dict(_single_plan.get("delivery_contract") or {}))
                _single_stream["milestone"].announce_plan(_single_plan, _single_project_context)
            except Exception:
                _single_stream["milestone"] = None
            _single_attempt = _single_claim()
            _single_attempt_started = time.monotonic()
            # Bind the live team to the owned logical session before any artifact
            # event. Completion replaces this request's slot, not a newer one.
            _live_session = SESSIONS.save_turn(
                _single_sid, task, {"status": "running", "team_id": _single_qhash, "results": []},
                mode="single", user_id=_uid, source_ref=_single_source_ref,
                turn_id=_single_turn_id)
            if _live_session.get("error"):
                raise PermissionError("session ownership unavailable")
            _single_session_ready = True

            # run_one reads bound mail at the actual native-input boundary,
            # shared with feedback dispatch and persistent continuations.
            if _single_attempt is None:
                r = {"status": "cancelled", "final": "", "error": "用户停止",
                     "cancelled": True, "elapsed": 0.0, "attempts": 0, "execution_calls": 0}
            else:
                r = LEGION.run_one(key, full_task, qhash=_single_qhash, cb=cb, user_id=_uid,
                                   conversation_id=_single_sid, runtime_brief=_brief,
                                   task_facts={"goal": task, "role": task, "task_id": key,
                                               **LEGION._native_scope_facts(_single_plan, request_id=_single_qhash),
                                               "team_id": _single_qhash, "attempt_id": _single_attempt,
                                               "delivery_contract": dict(_single_plan.get("delivery_contract") or {}),
                                               "project_context": _single_project_context},
                                   preferred_model=str(payload.get("model") or "") or None,
                                   preferred_appid=str(payload.get("appid") or "") or None,
                                   cancel_check=_single_cancel,
                                   execution_depth=_requested_depth)
            rounds = 1
            from artifact_provenance import merge_artifacts as _merge_round_artifacts
            _round_artifacts = list(r.get("artifacts") or [])
            _round_elapsed = float(r.get("elapsed") or 0)
            _round_usage = [dict(r)]
            _round_peer_results = []
            # 同一请求的前轮运行事实不能被后一次 run_one 覆盖；沿用原列表和记录。
            _round_attempts = {
                field: list(r[field]) if isinstance(r.get(field), list) else []
                for field in ("prior_attempts", "native_attempts")}
            def _recovery_reference(result):
                """Keep only this member's existing cursor, not a resume claim."""
                envelope = result.get("error_envelope")
                if (result.get("native_resume_error")
                        or result.get("fail_kind") == "native_session_unavailable"
                        or (isinstance(envelope, dict)
                            and envelope.get("category") == "native_session_unavailable")
                        or result.get("harness", key) != key):
                    return {}
                checkpoint = result.get("native_checkpoint")
                if checkpoint is None:
                    return None
                if (not isinstance(checkpoint, dict)
                        or not isinstance(checkpoint.get("session_id"), str)
                        or not checkpoint["session_id"]):
                    return {}
                from copy import deepcopy
                return {field: deepcopy(result[field]) for field in
                        ("native_checkpoint", "native_conversation_id", "workdir")
                        if result.get(field) not in (None, "")}

            _round_recovery = _recovery_reference(r) or {}
            cur_task = full_task
            _prev_final = ""
            while _pilot_on and rounds < 6:
                final = str(r.get("final") or "")
                # 用户取消是运行终态，不能再交给 Pilot 当“未完成”续跑；否则手动单家任务
                # 被停止后会被下一轮复活。这里与 dispatch_persistent 的取消语义保持对称。
                if r.get("status") == "cancelled" or _single_cancel():
                    if r.get("status") != "cancelled":
                        r.update(status="cancelled", error="用户停止", cancelled=True)
                    r["pilot"] = {"round": rounds, "action": "stop", "kind": "user_cancelled"}
                    break
                # 消费提供方已声明的暂停原因，不让 Pilot 把运行边界当作质量未完成。
                # 授权拒绝不是限流，也不承诺等待后自动恢复。
                from error_envelope import requires_provider_pause, is_provider_authorization_denial
                if requires_provider_pause(r):
                    _authorization_denied = is_provider_authorization_denial(r.get("error_envelope"))
                    r["pilot"] = {"round": rounds, "action": "stop",
                                  "kind": ("provider_authorization_required" if _authorization_denied
                                           else "provider_backpressure")}
                    if _authorization_denied:
                        r["pilot"]["reason"] = "提供方已拒绝本次授权；需管理员处理，本请求不自动重试。"
                    if alive["ok"]:
                        cb({"agent": key, "t": "reason", "node_id": node_id,
                            "x": (r["pilot"]["reason"] if _authorization_denied else
                                  "模型服务返回限流，已暂停本次自动续跑；原错误、产物和会话恢复状态保留。")})
                    break
                # 其它非重试终态仍按原路径处理，不重新解释错误文本。
                _run_error = r.get("error_envelope")
                if (r.get("status") != "done" and isinstance(_run_error, dict)
                        and _run_error.get("retryable") is False):
                    break
                if _requested_depth == "quick" and r.get("status") == "done" and final.strip():
                    break
                # Consume only a member's explicit request; ordinary short answers do no extra work.
                if isinstance((r.get("orchestration_request") or {}).get("need_peer"), dict):
                    peer_result = LEGION._run_requested_peer(
                        task, _single_plan, r, task_id=key, team_id=_single_qhash,
                        user_id=_uid, conversation_id=_single_sid,
                        cb=lambda event: _single_send("step", event), runtime_brief=_brief,
                        project_packet=_single_project_context,
                        _provider_results=lambda: [*_round_usage, *_round_peer_results],
                        cancel_check=_single_cancel)
                    if peer_result:
                        _round_peer_results.append(peer_result)
                        if not peer_result.get("retained_peer_input"):
                            _round_elapsed += float(peer_result.get("elapsed") or 0)
                    if r.get("peer_input_error"):
                        r["pilot"] = _peer_stop_pilot(r, rounds)
                        break
                    if r.get("peer_input_pause") or (peer_result and
                            __import__("error_envelope").requires_execution_pause(peer_result)):
                        r["peer_input_pause"] = r.get("peer_input_pause") or peer_result
                        r["pilot"] = _peer_stop_pilot(r, rounds)
                        break
                    if _single_cancel():
                        r.update(status="cancelled", cancelled=True)
                        break
                # 根修(2026-09-02):run_state 漏 scope_goal_met 是短答案被误判续跑 5 轮的
                # 真根因——统一走 LEGION._pilot_run_state(_is_delivered 结构化事实·全档位
                # 覆盖,不止 quick;quick 早停快路径保留在上方)。
                rs = LEGION._pilot_run_state(r)
                verdict = _lp.evaluate(final, rs, _uid)
                if verdict.get("action") != "continue":
                    # 保存本次已有停止事实，不带 shared_goals/owner_proxy，也不再作判断。
                    r["pilot"] = {"round": rounds, "action": verdict.get("action"),
                                  "kind": verdict.get("kind"),
                                  "reason": str(verdict.get("reason") or "")[:500]}
                    break
                _terminal_absent = rs.get("native_terminal_message_absent") or {}
                if _terminal_absent and not _terminal_absent.get("checkpoint_saved"):
                    r["pilot"] = {
                        "round": rounds, "action": "stop",
                        "kind": "native_terminal_message_unrecoverable",
                        "reason": "本轮原生结束正文为空，且恢复位置未可靠保存；保留原任务和产物，不从头重跑。"}
                    break
                # 转圈检测(双路径对称·同 dispatch_persistent):这轮和上轮完全相同=无新进展·提前停
                if _prev_final and LEGION._no_progress(final, _prev_final):
                    r["persistent_stalled"] = True
                    break
                _prev_final = final
                rounds += 1
                nxt = verdict.get("next_action") or "上一轮还没真达成·请继续推进到完成"
                if _single_cancel():
                    r = {**r, "status": "cancelled", "final": "",
                         "error": "用户停止", "cancelled": True, "attempts": rounds - 1,
                         "pilot": {"round": rounds, "action": "stop", "kind": "user_cancelled"}}
                    break
                if not _single_finish(r):
                    return  # 与 dispatch 相同：失效代次不能发布或续跑。
                _single_attempt = _single_claim()
                if _single_attempt is None:
                    # 已 finish 的原生回执仍然有效；只停止请求，不伪造下一轮或抹掉事实。
                    rounds -= 1
                    r = {**r, "status": "cancelled", "error": "用户停止", "cancelled": True,
                         "pilot": {"round": rounds, "action": "stop", "kind": "user_cancelled"}}
                    break
                _single_attempt_started = time.monotonic()
                if alive["ok"]:
                    cb({"agent": key, "t": "reason", "node_id": node_id,
                        "x": f"🔄 pilot 判本轮未达成 → 自动续跑第 {rounds} 轮(带上一轮成果·不失忆)"})
                cur_task = (full_task + f"\n\n【上一轮你的产出·在此基础上继续·别重头再来】\n{final[:1500]}"
                            + f"\n\n【续跑指引】{nxt}")
                if _terminal_absent:
                    cur_task += LEGION._native_terminal_message_context({key: _terminal_absent})
                previous_result = r
                r = LEGION.run_one(key, cur_task, qhash=_single_qhash, cb=cb, user_id=_uid,
                                   conversation_id=_single_sid, runtime_brief=_brief,
                                   task_facts={"goal": task, "role": task, "task_id": key,
                                               **LEGION._native_scope_facts(_single_plan, request_id=_single_qhash),
                                               "team_id": _single_qhash, "attempt_id": _single_attempt,
                                               "delivery_contract": dict(_single_plan.get("delivery_contract") or {}),
                                               "project_context": _single_project_context},
                                   preferred_model=str(payload.get("model") or "") or None,
                                   preferred_appid=str(payload.get("appid") or "") or None,
                                   cancel_check=_single_cancel, _continuation_result=previous_result,
                                   _provider_results=lambda: [*_round_usage, *_round_peer_results],
                                   execution_depth=_requested_depth)
                _round_usage.append(dict(r))
                _current_recovery = _recovery_reference(r)
                if _current_recovery is not None:
                    _round_recovery = _current_recovery
                _round_artifacts = _merge_round_artifacts(_round_artifacts, r.get("artifacts"))
                _round_elapsed += float(r.get("elapsed") or 0)
                for field, records in _round_attempts.items():
                    if isinstance(r.get(field), list):
                        records.extend(r[field])
            if (_single_attempt is not None
                    and r.get("status") == "cancelled" and r.get("native_checkpoint") is None
                    and not r.get("native_resume_error") and _round_recovery
                    and all(not r.get(field) or r.get(field) == _round_recovery.get(field)
                            for field in ("native_conversation_id", "workdir"))):
                # A cancellation between calls, or before the next CLI starts,
                # must not erase the previous call's bound recovery reference.
                for field, value in _round_recovery.items():
                    if r.get(field) in (None, ""):
                        r[field] = value
                r.setdefault("resumed_native", False)
            for field, records in _round_attempts.items():
                if records:
                    r[field] = records
            if _round_artifacts:
                # 续跑可能只复验、不再改文件；保留本请求前轮产物，但不交付已被后轮删除的文件。
                r["artifacts"] = [a for a in _round_artifacts
                                  if not a.get("path") or os.path.isfile(a["path"])]
            r["elapsed"] = _round_elapsed
            if _round_peer_results:
                import token_telemetry
                r["peer_results"] = list(_round_peer_results)
                r["usage"] = token_telemetry.summarize([
                    *_round_usage, *(row for row in _round_peer_results
                                      if not row.get("retained_peer_input"))])
            r["persistent_rounds"] = rounds
            if not _single_finish(r):
                return  # 不让迟到结果覆盖 ProjectContext、session 或 final。
            milestone = _single_stream.get("milestone")
            if milestone:
                try:
                    import project_context as _project_context
                    _single_project_context = _project_context.record_result(
                        _single_qhash, key, r, assigned_harness=key)
                    milestone.observe_context(
                        _single_project_context, include_gaps=False,
                        include_journey=bool(r.get("journey_observations")))
                    milestone.update([r])
                except Exception:
                    pass
            _single_save_result(r)
            _single_send("final", {"node_id": node_id, "result": r})
        except BaseException as exc:
            # 仅收尾本请求确实领取的代次；异常不是新的 native checkpoint 回执。
            if _single_attempt is not None:
                failed = {"status": "error", "final": "",
                          "error": f"{type(exc).__name__}: {exc}",
                          "elapsed": max(0.0, time.monotonic() - _single_attempt_started)
                                     if _single_attempt_started is not None else 0.0}
                try:
                    if _single_finish(failed) and _single_session_ready:
                        _single_save_result(failed)
                except Exception:
                    pass  # 清理失败不能替换原异常。
            if isinstance(exc, PermissionError):
                # 并发归属拒绝只回复当前连接；未领取时不能写别人的团队或 session。
                self._sse_send("error", {"node_id": node_id, "error": "permission_denied",
                                       "reason": str(exc)})
            elif isinstance(exc, Exception):
                _single_send("error", {"node_id": node_id, "error": f"{type(exc).__name__}: {exc}"})
            else:
                raise

    def _handle_dispatch(self, payload: dict):
        question = str(payload.get("question") or payload.get("query") or "").strip()
        plan = payload.get("plan") or {}
        # auto 模式允许正式主力模型在一次调用中同时完成分级与短答；调用方可显式关闭。
        # 其它所有任务仍进入 Harness，裸模型不能继承文件/工具/设备/持续会话能力。
        _direct_model_allowed = payload.get("allow_direct_model") is not False
        _route_benchmark_enabled = str(os.environ.get("LEGION_ENABLE_ROUTE_BENCHMARK") or "").lower() \
            in {"1", "true", "yes", "on"}
        _direct_model_forced = bool(_route_benchmark_enabled and
            str(payload.get("execution_route") or "").strip() == "direct_model")
        # pk=各军团并行比最佳(无首脑·默认) · relay=串行协作(无首脑) · orchestrate=首脑编排
        # (用户2026-08-30:PK无首脑并行·编排才用首脑·可默认pk·用户可选orchestrate·对话指令切换)。
        mode = str(payload.get("mode") or "auto")   # 默认智能:军团按场景自动选队形(用户2026-08-31)
        if mode not in ("pk", "relay", "orchestrate", "auto"):
            mode = "auto"
        _user_selected_mode = mode != "auto" and "mode" in payload
        if not question and not (plan.get("tasks")):
            self._send_json({"error": "missing question"}, 400)
            return
        # 多session持续上下文:dispatch前读同session历史→完整context喂给各家模型
        # (Hermes sessions/messages单一事实源同款·context not control·不是只存同个sid假多轮)
        _uid = str(payload.get("uid") or "default")
        sid = payload.get("session_id") or SESSIONS.new_session_id(question)
        plan = dict(plan)
        _raw_source_ref = payload.get("source_ref") if isinstance(payload.get("source_ref"), dict) else {}
        _source_ref = _query_source_ref(_raw_source_ref)
        if _source_ref.get("type") in {"session_continue", "session_retry"} and not _source_ref.get("url"):
            _origin_sid = str(_raw_source_ref.get("origin_sid") or sid)
            _inherited_source = SESSIONS.latest_source_ref(_origin_sid, user_id=_uid)
            if _inherited_source:
                _source_ref = _query_source_ref(_inherited_source)
                _source_ref["continued_from_session"] = _origin_sid[:60]
        _source_ref.setdefault("source_id", "query-" + hashlib.sha256(
            f"{_uid}\0{sid}\0{question}".encode()).hexdigest()[:12])
        plan["source_ref"] = dict(_source_ref)
        plan["execution_role"] = "champion"
        plan["quality_context"] = _quality_context_with_source(question, plan, _source_ref)
        # API 调用方可显式提供交付契约；与规划器生成的契约合并时，用户/调用方要求是底线，
        # 不能被模型改写成“这些格式任选其一”。真实矩阵和产品内部补救链因此消费同一事实源。
        if isinstance(payload.get("delivery_contract"), dict):
            plan["delivery_contract"] = dict(payload["delivery_contract"])
        if isinstance(payload.get("required_artifacts"), list):
            plan["required_artifacts"] = list(payload["required_artifacts"])
        # 顶层 attachments(前端 dispatch 协议字段·上传返回的 {path,kind} 列表)并入 plan——
        # 下游 _media_prelude 只认 plan.get("attachments"),不并则附件永远进不了军团。
        if isinstance(payload.get("attachments"), list):
            plan["attachments"] = list(payload["attachments"])
        if isinstance(payload.get("project_context"), dict):
            plan["project_context"] = dict(payload["project_context"])
        elif not plan.get("project_context"):
            plan["project_context"] = SESSIONS.continuation_project_context(sid, user_id=_uid)
        if not self._project_access_allowed(plan.get("project_context") or {}, _uid, str(sid)):
            return
        import personal_identity
        try:
            personal_identity.bind_plan_attachments(plan, _uid, str(sid))
        except PermissionError:
            self._send_json({"error": "attachment_not_authorized"}, 403)
            return
        plan["_user_selected_mode"] = _user_selected_mode
        history_ctx = SESSIONS.conversation_context(sid, user_id=_uid)
        if history_ctx:
            plan["conversation_context"] = history_ctx
        # 稳定 team_id(军团自审 #1·断线续播):server 自己定 team_id 并作为 dispatch 的 qhash·让
        # team_id=qhash=注册表键=事件流键全链一致。前端断线/刷新用它 GET /api/legion/events?after=
        # 增量补回过程(旧版 _evq 是请求内内存队列·断了就永久丢过程)。每次派活唯一(带轮次戳防复用)。
        _team_id = str(payload.get("team_id") or f"web_{sid}_{int(time.time())}")
        plan["team_id"] = _team_id
        if not self._project_owner_allowed(_team_id, _uid, str(sid)):
            return
        # 鉴权后、模型调用前冻结调用方实际声明的键；空字典不是模型默认人数。
        _request_team_policy = (payload.get("plan") or {}).get("team_policy")
        if not isinstance(_request_team_policy, dict):
            _request_team_policy = {}
        # 与人数限制来源分开保留；False 是明确选择，None 表示未指定。
        _request_continuous_coordination = deepcopy(
            _request_team_policy.get("continuous_coordination"))
        plan["input_snapshot"] = {
            "team_id": _team_id, "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
            "initial_plan_sha256": _canonical_sha256(plan), "captured_at": time.time(),
            "attachments": deepcopy(plan.get("attachments") or []),
            "request_team_policy": {
                key: deepcopy(_request_team_policy[key])
                for key in ("min_agents", "max_agents", "candidate_harnesses", "max_parallel",
                            "max_failover", "contract_repair_rounds", "continuous_coordination")
                if key in _request_team_policy
            },
        }
        _version_identity = _source_version_identity()
        _version_identity["team_id"] = _team_id
        _version_identity["source_id"] = _source_ref.get("source_id") or ""
        plan["version_identity"] = dict(_version_identity)

        def _save_query_turn(result: dict, selected_mode: str, effective_plan: dict) -> dict:
            result["team_id"] = _team_id
            result["source_ref"] = dict(_source_ref)
            result["version_identity"] = dict(_version_identity)
            result["execution_role"] = "champion"
            snapshot = dict(plan.get("input_snapshot") or {})
            snapshot["effective_plan_sha256"] = _canonical_sha256(effective_plan)
            return SESSIONS.save_turn(
                sid, question, result, mode=selected_mode, user_id=_uid,
                source_ref=_source_ref, input_snapshot=snapshot,
                outcome_contract=dict(effective_plan.get("delivery_contract") or {}),
                version_identity=_version_identity)

        # SSE:边跑边把每家每一步推给前端(全透明·全过程流水)。先把 team_id 告诉前端(SSE 头事件)·
        # 让它立刻记住·断线后能凭它续播。
        self._sse_open()
        # 先建 team，保证 meta 也是可恢复事件流的第一个有序事件；规划前没有任务时先建空任务表，
        # dispatch 稍后 ensure 会补齐，不会丢 attempt/mailbox。
        try:
            TEAMS.ensure(
                _team_id, question, {}, {}, user_id=_uid, conversation_id=str(sid),
                source_ref=_source_ref, input_snapshot=dict(plan.get("input_snapshot") or {}),
                version_identity=_version_identity,
                quality_context=dict(plan.get("quality_context") or {}), execution_role="champion")
            _meta_rec = TEAMS.append_event(_team_id, {"team_id": _team_id, "sid": sid}, "meta")
        except PermissionError as exc:
            self._sse_send("error", {"error": "permission_denied", "reason": str(exc)})
            return
        except Exception:
            _meta_rec = None
        self._sse_send("meta", {"team_id": _team_id, "sid": sid},
                       event_id=(_meta_rec or {}).get("event_id"))

        # ★ 断线保活(用户 2026-08-28 goal-D·防"任务突然停/白干"):把【任务执行】放进独立后台
        #   线程·SSE 只是"旁听"它。客户端关标签/断网只停止旁听(SSE),后台线程照样跑完 + save_turn,
        #   用户回来能在 session 列表看到结果。这是【执行与连接解耦】的 UX 解·不是硬墙钟杀任务。
        #   依据:CC 后台任务(任务不随前台连接生死)。防 bug 关键:①后台线程 daemon=False 保证跑完
        #   ②save_turn 在后台线程内(脱离 SSE)③线程安全队列转发事件·断线只是不再取事件。
        import queue as _queue
        _evq: _queue.Queue = _queue.Queue()
        _DONE = object()

        # meta 发出后，预判/选家也可能受机器调度或网关影响。用户看到的“最大静默”从 meta
        # 开始算，所以给整条 worker 前置一条 2.5s 保活；后续真实事件到来后仍只作为状态脉冲，
        # 前端按同一 heartbeat key 原位更新，不增长 DOM。
        import threading as _dispatch_threading
        _dispatch_hb_stop = _dispatch_threading.Event()
        _dispatch_phase = {"stage": "accepted", "label": "军团已接单·准备判断队形"}
        # 2026-09-07:dispatch SSE 里程碑发射器(首产物卡等)·与 run_one 路径对称;事件经
        # _single/_evq 转发链正常落盘+在线推送。
        _dispatch_milestone = None
        try:
            from delivery_progress import MilestoneEmitter as _ME
            def _milestone_send(_ev):
                _rec = None
                try:
                    _rec = TEAMS.append_event(_team_id, _ev, "step")
                except Exception:
                    _rec = None
                _evq.put(("step", _ev, (_rec or {}).get("event_id")))
            _dispatch_milestone = _ME(
                _milestone_send, dict((plan or {}).get("delivery_contract") or {}))
        except Exception:
            _dispatch_milestone = None

        def cb(ev):
            if ARTIFACTS._is_private_artifact_event(ev):
                return
            # 2026-09-07里程碑零记录修通:太阳系真跑实锤——dispatch 内部 milestone 只看 DAG
            # worker 路径的 artifact_ready,cc 超时→hermes 接力全走 failover/relay,产物以
            # artifact_recovered 上报,首产物里程碑从未触发(样机12:03就绪但对话流无卡片)。
            # 在 SSE 汇聚点补挂:两条路径的产物都喂 milestone,first_artifact_reported 必出。
            if _dispatch_milestone and ev.get("t") in ("artifact_ready", "artifact_recovered"):
                try:
                    _dispatch_milestone.observe_artifact(ev)
                except Exception:
                    pass
            _stage = str(ev.get("stage") or "")
            _kind = str(ev.get("t") or "")
            if _kind == "task_queued" or _stage == "queued":
                _dispatch_phase.update(stage="queued", label="队形已确定·等待可用执行槽")
            elif _stage == "context_loaded":
                _dispatch_phase.update(stage="context_loaded", label="用户结果已对齐·正在选择参照与分工")
            elif _stage == "classified" or _kind in ("mode_selected", "captain_selected"):
                _dispatch_phase.update(stage="executing", label="队形已确定·准备执行")
            elif _stage == "waiting_user" or ev.get("status") == "waiting_approval":
                _dispatch_phase.update(stage="waiting_user", label="正在等待用户处理待办")
            elif _stage == "verifying" or _kind.startswith("verification"):
                _dispatch_phase.update(stage="verifying", label="成员已产出·正在验证交付")
            elif _stage == "artifact_ready" or _kind in (
                    "artifact_ready", "first_artifact_reported", "delivery_contract_ready", "deliverable_ready"):
                _dispatch_phase.update(stage="artifact_ready", label="已有候选产物·继续验证与收口")
            elif _kind in ("task_started", "tool", "tool_started", "harness_step", "queue_released"):
                _dispatch_phase.update(stage="executing", label="成员正在执行任务")
            # 持久化先于在线转发：队列携带同一个 event_id，断线补播与在线流不会各自编号/乱序。
            # 保活心跳(2.5s 滴答)例外:不落盘——阶段流转由真实事件(task_started/verifying 等)
            # 承载可回放,心跳只服务"在线时让用户看见还活着"(前端同一行原位刷新时间,
            # 用户 2026-09-07:没有动作就不新增行)。否则一单跑 5 分钟 = 事件存储多 120 条滴答。
            if _kind == "heartbeat":
                rec = None
            else:
                try:
                    rec = TEAMS.append_event(_team_id, ev, "step")
                except Exception:
                    rec = None
            _evq.put(("step", ev, (rec or {}).get("event_id")))

        def _dispatch_hb():
            elapsed = 0.0
            while not _dispatch_hb_stop.wait(2.5):
                elapsed += 2.5
                cb({"agent": "legion", "t": "heartbeat", "status": "running",
                    "stage": _dispatch_phase["stage"],
                    "x": f"{_dispatch_phase['label']}…(已 {elapsed:g}s)"})
        _dispatch_threading.Thread(target=_dispatch_hb, daemon=True,
                                   name=f"dispatch-heartbeat-{_team_id[-12:]}").start()

        def _finish_cancelled(stage: str, reason: str, effective_plan: dict):
            """排队/规划取消都经原事件队列收尾，不从后台线程直接写SSE。"""
            result = {"results": [], "solo": False, "status": "cancelled",
                      "cancelled": True, "team_id": _team_id,
                      "mode_reason": {"mode": stage, "triggers": [], "reason": reason},
                      "execution_mode": f"{stage}_cancelled",
                      "delivery_contract": dict(effective_plan.get("delivery_contract") or {})}
            import task_outcome
            task_outcome.attach(result, result["delivery_contract"])
            try:
                result["session"] = _save_query_turn(result, f"{stage}_cancelled", effective_plan)
            except Exception as exc:
                result["session_persistence"] = {
                    "ok": False, "error": f"{type(exc).__name__}:{exc}"}
            try:
                record = TEAMS.append_event(_team_id, result, "final")
            except Exception:
                record = None
            _evq.put(("final", result, (record or {}).get("event_id")))

        def _finish_planning_failure(planning_result: dict, effective_plan: dict,
                                     selected_mode: str):
            """复用原规划失败出口；拒绝不会被备用路径变成新制作任务。"""
            _planner_error = str(planning_result.get("error") or "planner_unavailable")[:160]
            _planner_attempts = list(planning_result.get("planner_attempts") or [])
            cb({"agent": "legion", "t": "reason", "status": "degraded",
                "planner_error": _planner_error, "planner_attempts": _planner_attempts,
                "x": "自主规划未成功，未派出默认成员；保留原项目与失败现场供继续。"})
            # There is no executable plan. Do not turn transport failure into
            # a fabricated PK plan. Keep the existing recovery endpoints:
            # resume an authorized unfinished team, otherwise re-plan through
            # this conversation's normal auto entry. No additional model call,
            # task cloning, old-team mutation or resume-budget consumption here.
            import project_context as _failed_projects
            from legion_contracts import resume_plan_from_state as _resume_plan
            _original_context = dict(effective_plan.get("project_context") or {})
            _recovery = {"endpoint": "/api/legion/dispatch", "session_id": str(sid),
                         "mode": selected_mode, "automatic_started": False}
            _previous_team = str(_original_context.get("continued_from_team_id") or "")
            if _previous_team:
                _previous_state = TEAMS.load(_previous_team) or {}
                try:
                    _failed_projects.require_owner(
                        {**_previous_state,
                         "user_id": _previous_state.get("user_id") or "default"},
                        _uid, str(sid))
                    if (str(_previous_state.get("conversation_id") or "") == str(sid)
                            and not any(t.get("status") == "running"
                                        for t in (_previous_state.get("tasks") or {}).values())):
                        _resume_candidate, _ = _resume_plan(_previous_state)
                        if _resume_candidate.get("tasks"):
                            _recovery = {"endpoint": "/api/legion/team/resume",
                                         "team_id": _previous_team, "session_id": str(sid),
                                         "automatic_started": False}
                except PermissionError:
                    pass  # An untrusted reference grants no right to the old team.
            _planning_cancelled = bool(LEGION.is_cancelled(_team_id)
                                       or planning_result.get("cancelled")
                                       or planning_result.get("status") == "cancelled")
            _failure_plan = {**effective_plan, "project_context": {
                **_original_context,
                "current_stage": "cancelled" if _planning_cancelled else "planning_failed"}}
            _failed_packet = _failed_projects.initialize(
                _team_id, question, _failure_plan, mode=selected_mode,
                user_id=_uid, conversation_id=str(sid))
            _failure_contract = dict(_failed_packet.get("delivery_contract") or {})
            _failure_plan["delivery_contract"] = _failure_contract
            _planning_out = {
                "ok": False, "status": "cancelled" if _planning_cancelled else "error",
                "error": "cancelled" if _planning_cancelled else _planner_error,
                "cancelled": _planning_cancelled,
                "planner_error": _planner_error, "planner_attempts": _planner_attempts,
                "team_id": _team_id, "question": question, "execution_mode": selected_mode,
                "results": [], "all_failed": not _planning_cancelled,
                "delivered_count": 0, "total": 0,
                "delivery_contract": _failure_contract, "recovery": _recovery,
                "note": ("本轮已取消；原项目和规划诊断已保留，未派出成员。"
                         if _planning_cancelled else
                         "自主规划未完成；原项目和规划失败现场已保留，未派出默认成员。"),
                # sessions already persists outcome; do not add another store.
                "outcome": {"status": "cancelled" if _planning_cancelled else "failed",
                            "failure_stage": "planning",
                            "planner_error": _planner_error,
                            "planner_attempts": _planner_attempts, "recovery": _recovery},
            }
            for _field in ("planner_transport", "planner_request_id", "native_facts",
                           "blocked_before_native", "execution_calls", "planner_usage"):
                if _field in planning_result:
                    _planning_out[_field] = deepcopy(planning_result[_field])
            if planning_result.get("error_envelope"):
                _planning_out["error_envelope"] = deepcopy(planning_result["error_envelope"])
                _planning_out["outcome"]["error_envelope"] = deepcopy(planning_result["error_envelope"])
            from error_envelope import requires_execution_pause, is_provider_authorization_denial
            if not _planning_cancelled and requires_execution_pause(planning_result):
                _authorization_denied = is_provider_authorization_denial(
                    planning_result.get("error_envelope"))
                _planning_out.update(
                    status="blocked",
                    note="本轮规划执行需暂停；原项目和运行事实已保留，未派出默认成员。")
                _planning_out["outcome"].update(
                    status="blocked", kind=("provider_authorization_required" if
                                           _authorization_denied else "planning_execution_paused"))
            try:
                _planning_out["session"] = _save_query_turn(
                    _planning_out, selected_mode, _failure_plan)
            except Exception as _save_error:
                _planning_out["session_error"] = type(_save_error).__name__
            try:
                _failed_event = TEAMS.append_event(_team_id, _planning_out, "final")
            except Exception:
                _failed_event = None
                _planning_out["event_persistence"] = {"ok": False}
            _evq.put(("final", _planning_out, (_failed_event or {}).get("event_id")))
            return

        def _worker():
            nonlocal mode
            _plan = plan
            _slot_token = None
            _slot_held = False
            _last_queue_note = 0.0
            # 开跑即落session(2026-09-08用户钉死'全部都得能在session看·好几个404'):
            # 旧链save_turn只在整单结束时写→长跑单(20-40min)进行中侧栏点开就是404。
            # 开跑先写进行中turn,用户随时可点开;单结束由末尾save_turn覆盖为完整结果。fail-open。
            # 2026-09-08晚用户实锤'进行中点开啥也没有':开跑快照results是空的+没任何过程信息,
            # 页面渲染空气泡。修:进行中turn携带team_id(前端可接入实时过程)与阶段说明。
            _early_team_id = str(_team_id or "")
            try:
                SESSIONS.save_turn(sid, question, {"ok": True, "status": "running",
                                                  "team_id": _early_team_id,
                                                  "results": [],
                                                  "note": ("本单已接收：排队和实时过程可在军团页面"
                                                           f"查看(team: {_early_team_id})；"
                                                           "结束后自动更新为完整结果")},
                                   mode=mode if mode in ("pk", "relay", "orchestrate") else "pk",
                                   user_id=_uid)
            except Exception:
                pass
            _candidate_harnesses = (((_plan.get("team_policy") or {}).get("candidate_harnesses"))
                                    if isinstance(_plan.get("team_policy"), dict) else None)
            _candidate_pool = ({str(key) for key in _candidate_harnesses if str(key)}
                               if isinstance(_candidate_harnesses, (list, tuple, set)) else set())
            try:
                # 每次只尝试一次名额；等待留在worker，使取消和排队事件始终可达。
                _slot_token = SESSIONS.user_slot_token(_uid)
                while not _slot_held:
                    if LEGION.is_cancelled(_team_id):
                        _finish_cancelled("queued", "用户在排队阶段停止", _plan)
                        return
                    _slot_held, _ahead = SESSIONS.user_slot_wait(_slot_token)
                    if _slot_held:
                        break
                    now = time.time()
                    if now - _last_queue_note >= 20.0:
                        _last_queue_note = now
                        cb({"agent": "legion", "t": "task_queued", "status": "queued",
                            "stage": "user_queue",
                            "x": f"本单排队中，前面还有 {_ahead} 个任务；可随时停止"})
                    with _slot_token["cond"]:
                        _slot_token["cond"].wait(timeout=0.25)
                if LEGION.is_cancelled(_team_id):
                    _finish_cancelled("queued", "用户在排队阶段停止", _plan)
                    return
                # An explicit sole member already answers "who owns this
                # request". Keep the ordinary one-node runner (leases, task
                # state, native resume and full context), not the quick-answer
                # shortcut or an extra model-generated solution plan.
                _owner_binding = {"bound": False}
                if not _direct_model_forced:
                    import collaboration_runtime as _owner_runtime
                    _owner_binding = _owner_runtime.bind_explicit_owner_request(
                        question, _plan, mode=mode,
                        default_owner=os.environ.get("MEYO_PERSONAL_DEFAULT_HARNESS", ""))
                if _owner_binding.get("bound"):
                    _owner = str(_owner_binding["owner"])
                    if _owner not in set(LEGION._harness_keys()):
                        raise ValueError(f"selected Harness is not in the answer pool: {_owner}")
                    _plan = dict(_owner_binding["plan"])
                    _candidate_pool = {_owner}
                    _owner_source = str(_owner_binding["source"])
                    cb({"agent": "legion", "t": "mode_selected", "stage": "classified",
                        "status": "done", "mode": "single",
                        "owner": _owner, "selection_source": _owner_source,
                        "planner_attempts": [],
                        "reason": ("个人实例默认成员接手，完整目标交给该 Harness 原生处理"
                                   if _owner_source == "personal_runtime_owner_binding" else
                                   "用户已选择唯一成员，完整目标交给该 Harness 原生处理"),
                        "x": f"{_owner} 原生接手整单；工具、推理深度和自主规划能力保持不变"})
                # ★智能队形自动决定(用户2026-08-31:不用用户点·按场景自动选 pk/relay/orchestrate)。
                # mode=auto 时先让规划器读现场判断最合适队形+分工,把 mode 落定成三态之一再往下走。
                # context not control:军团自己判断该并行/接力/统筹·不要用户先在下拉框选。
                if mode == "auto":
                    _planned_cancelled = False  # 规划期取消标记（2026-09-19，评估期可中断）
                    if _plan.get("tasks"):
                        # 用户已在 plan 里显式给了 tasks(如 relay 链)——尊重,不再规划;队形按 pipeline 有无定
                        mode = ("single" if _owner_binding.get("bound") else
                                "relay" if (_plan.get("pipeline") and len(_plan["pipeline"]) >= 2)
                                else "pk")
                        # tasks 已是最终分工(不再规划)→ _auto_plan 置 ok=False,让下方统一消费点
                        # 自然跳过,走既有 dispatch 主路径。(真 bug 根修 2026-09-02:此前此分支不赋值
                        # _auto_plan,而 1732 消费点在三分支汇合处无条件读 → mode=auto+显式 tasks
                        # 必 UnboundLocalError 整单挂。)
                        _auto_plan = {"ok": False, "reason": (
                            _owner_binding["source"] if _owner_binding.get("bound")
                            else "tasks explicit")}
                    else:
                        _route_policy_reader = __import__("harness_performance")
                        try:
                            _micro_policy = _route_policy_reader.route_policy("fast_micro")
                            _general_policy = _route_policy_reader.route_policy("fast_general")
                        except Exception:
                            _micro_policy = _general_policy = {"route": "strongest_single", "verified": False}
                        _verified_direct_models = [str(policy.get("model") or "") for policy in
                            (_micro_policy, _general_policy) if policy.get("verified") is True
                            and policy.get("route") == "direct_model" and policy.get("model")]
                        _preferred_direct_model = (str(payload.get("direct_model") or "")
                                                   if _direct_model_forced else
                                                   next(iter(dict.fromkeys(_verified_direct_models)), ""))
                        _should_try_direct = bool(_direct_model_forced or _preferred_direct_model)
                        _quick_result = (__import__("quick_pred").predict_simple(
                            question, has_attachments=bool(_plan.get("attachments")), plan=_plan,
                            preferred_model=_preferred_direct_model,
                            preferred_appid=str(payload.get("direct_appid") or ""))
                            if (_should_try_direct and (_direct_model_forced or
                                __import__("quick_pred").fastpath_healthy()))
                            else {"verdict": "needs_harness",
                                  "reason": "当前画像尚无主力模型直答胜出证据，进入 Harness"})
                        _route_scene = "fast_micro" if _quick_result.get("profile") == "micro" else "fast_general"
                        _route_policy = _micro_policy if _route_scene == "fast_micro" else _general_policy
                        _direct_model_enabled = bool(_direct_model_forced or (
                            _direct_model_allowed and _route_policy.get("verified") is True
                            and _route_policy.get("route") == "direct_model"
                            and str(_route_policy.get("model") or "") == str(_quick_result.get("model") or "")))
                        if (_direct_model_enabled and _quick_result.get("verdict") == "simple"
                                and str(_quick_result.get("answer") or "").strip()):
                            _answer = str(_quick_result["answer"]).strip()
                            _model = str(_quick_result.get("model") or "")
                            _elapsed = float(_quick_result.get("elapsed_s") or 0)
                            cb({"agent": "direct-model", "t": "classification", "stage": "classified",
                                "status": "done", "decision": "simple", "mode": "direct_model",
                                "model": _model, "x": "简单任务·一次模型调用直接完成，不启动 Harness"})
                            _direct_row = {"key": "direct-model", "task_id": "direct-answer",
                                           "harness": "direct-model", "assigned_harness": "direct-model",
                                           "status": "done", "final": _answer, "elapsed": _elapsed,
                                           "attempts": 1, "model_used": _model, "execution_depth": "quick"}
                            _direct_out = {"results": [_direct_row], "solo": True, "direct_model": True,
                                           "execution_mode": "direct_model", "selected_mode": "direct_model",
                                           "team_id": _team_id,
                                           "mode_reason": {"mode": "direct_model", "triggers": [],
                                                           "delivered": 1, "total": 1,
                                                           "reason": "simple_answer_from_classifier"},
                                           "delivery_contract": dict(_plan.get("delivery_contract") or {})}
                            try:
                                import task_outcome as _task_outcome
                                _task_outcome.attach(_direct_out, _direct_out["delivery_contract"])
                            except Exception:
                                _direct_out.update({"delivered_count": 1, "total": 1,
                                                    "all_failed": False, "partial": False})
                            try:
                                __import__("quick_pred").record_fastpath(ok=True)
                            except Exception:
                                pass
                            try:
                                _session_info = _save_query_turn(
                                    _direct_out, "direct_model", _plan)
                                if _session_info.get("error"):
                                    raise RuntimeError(str(_session_info["error"]))
                                _direct_out["session"] = _session_info
                            except Exception as _session_error:
                                _direct_out["session_persistence"] = {
                                    "ok": False, "error": f"{type(_session_error).__name__}:{_session_error}"}
                                cb({"agent": "legion", "t": "session_persist_failed", "status": "warning",
                                    "x": "答案已交付，但会话落盘失败；已保留团队事件供恢复",
                                    "error": _direct_out["session_persistence"]["error"]})
                            try:
                                _fr = TEAMS.append_event(_team_id, _direct_out, "final")
                            except Exception:
                                _fr = None
                            _evq.put(("final", _direct_out, (_fr or {}).get("event_id")))
                            return
                        if _direct_model_forced:
                            _reason = str(_quick_result.get("reason") or "direct model did not return a simple answer")
                            _direct_error = {"results": [{"key": "direct-model", "task_id": "direct-answer",
                                "harness": "direct-model", "assigned_harness": "direct-model",
                                "status": "error", "final": "", "error": _reason,
                                "elapsed": float(_quick_result.get("elapsed_s") or 0), "attempts": 1,
                                "model_used": str(_quick_result.get("model") or payload.get("direct_model") or "")}],
                                "direct_model": True, "execution_mode": "direct_model",
                                "selected_mode": "direct_model", "delivered_count": 0, "total": 1,
                                "all_failed": True, "delivery_contract": dict(_plan.get("delivery_contract") or {})}
                            try:
                                __import__("quick_pred").record_fastpath(ok=False)
                            except Exception:
                                pass
                            try:
                                _fr = TEAMS.append_event(_team_id, _direct_error, "final")
                            except Exception:
                                _fr = None
                            _evq.put(("final", _direct_error, (_fr or {}).get("event_id")))
                            return
                        if (_quick_result.get("verdict") == "simple"
                                and str(_quick_result.get("answer") or "").strip()):
                            try:
                                _am0 = LEGION._autonomy_matrix()
                                _ready0 = set(LEGION._aigc_ready().get("runnable_clis", []))
                                if _candidate_pool:
                                    _ready0 &= _candidate_pool
                                _fast0 = next((key for key in ("cc", "hermes", "penguin")
                                               if str(_am0.get(key, {}).get("cost_class")) == "light"
                                               and key in _ready0), "")
                                if not _fast0 and _ready0:
                                    _fast0 = sorted(_ready0)[0]
                            except Exception:
                                _fast0 = ""
                            cb({"agent": "legion", "t": "classification", "stage": "classified",
                                "status": "done", "decision": "simple", "mode": "single",
                                "x": "简单任务·裸模型策略未验证，保守交给单 Harness 快答"})
                            _auto_plan = {"ok": True, "suggested_mode": "pk",
                                          "tasks": {_fast0 or "cc": question},
                                          "directions": {_fast0 or "cc": "快答"},
                                          "reason": "预判:simple·单家快答",
                                          "via": "quick_pred_fastpath"}
                        else:
                            cb({"agent": "legion", "t": "classification", "stage": "classified",
                                "status": "done", "decision": "needs_harness", "mode": "auto",
                                "reason": str(_quick_result.get("reason") or "需要 Harness 完成")[:200],
                                "x": "任务需要原生工具或持续工作·先由适任主创实做"})
                            import collaboration_runtime as _author_runtime
                            # A declared candidate scope belongs to the existing
                            # planner and owner binding. Only an unscoped request
                            # may start directly with the initial author.
                            _declared_scope = ((_plan.get("team_policy") or {}).get("candidate_harnesses")
                                               if isinstance(_plan.get("team_policy"), dict) else None)
                            _author_binding = ({} if _declared_scope is not None else
                                _author_runtime.bind_initial_author_request(
                                    question, _plan, valid=set(LEGION._harness_keys()),
                                    runnable=set(LEGION._aigc_ready().get("runnable_clis") or [])))
                            if _author_binding.get("bound"):
                                _plan = dict(_author_binding["plan"])
                                mode = "single"
                                _auto_plan = {"ok": False, "reason": _author_binding["source"]}
                                cb({"agent": "legion", "t": "mode_selected", "stage": "classified",
                                    "status": "done", "mode": "single", "owner": _author_binding["owner"],
                                    "selection_source": _author_binding["source"], "planner_attempts": [],
                                    "reason": "先执行完整用户目标；组织方式由主创依据实际现场提出，不预先选队形",
                                    "x": f"{_author_binding['owner']} 接手实做；保留本单可增援成员范围"})
                            elif _author_binding.get("error"):
                                _finish_planning_failure(
                                    {"ok": False, "error": _author_binding["error"],
                                     "blocked_before_native": True, "execution_calls": 0}, _plan, "auto")
                                return
                            else:
                                # An explicit organization retains its original planning path.
                                import threading as _th2
                                _plan_hb = _th2.Event()
                                def _plan_hb_thread():
                                    _pi = 0
                                    while not _plan_hb.wait(2.5):
                                        _pi += 1
                                        try:
                                            cb({"agent": "legion", "t": "reason", "status": "running",
                                                "x": f"读取显式组织现场…(已 {_pi*2.5:.0f}s)"})
                                        except Exception:
                                            pass
                                _th2.Thread(target=_plan_hb_thread, daemon=True).start()
                                _plan_box = {}
                                def _plan_worker():
                                    try:
                                        _plan_box["plan"] = LEGION.plan_request_with_model(
                                            question, _plan.get("attachments") or [],
                                            candidate_harnesses=list(_candidate_pool) or None,
                                            request_context=_plan,
                                            user_id=_uid, conversation_id=str(sid),
                                            cancel_check=lambda: LEGION.is_cancelled(_team_id))
                                    except Exception as _e:
                                        _plan_box["plan"] = {"ok": False,
                                                             "error": f"planner_call:{type(_e).__name__}"}
                                _plan_thread = _th2.Thread(target=_plan_worker, daemon=True)
                                _plan_thread.start()
                                while True:
                                    _plan_thread.join(timeout=0.5)
                                    if not _plan_thread.is_alive():
                                        break
                                    if LEGION.is_cancelled(_team_id):
                                        _planned_cancelled = True
                                        cb({"agent": "legion", "t": "reason", "status": "cancelled",
                                            "x": "用户已停止·评估中止"})
                                        break
                                if _planned_cancelled:
                                    _auto_plan = {"ok": False, "error": "用户停止"}
                                    _plan["planning_cancelled"] = True
                                else:
                                    _auto_plan = _plan_box.get("plan") or {"ok": False,
                                                                           "error": "planner_no_result"}
                                _plan_hb.set()
                    if _planned_cancelled:
                        _finish_cancelled("planning", "用户在评估阶段停止", _plan)
                        return
                    if _auto_plan.get("ok"):
                        mode = _auto_plan.get("suggested_mode") or "pk"
                        cb({"agent": "legion", "t": "mode_selected", "stage": "classified",
                            "status": "done", "mode": mode,
                            "planner_attempts": list(_auto_plan.get("planner_attempts") or []),
                            "reason": str(_auto_plan.get("reason") or "军团已完成队形与分工判断")})
                        if mode not in ("pk", "relay", "orchestrate"):
                            mode = "pk"
                        _explicit_contract = dict(_plan.get("delivery_contract") or {})
                        _explicit_team_policy = dict(_plan.get("team_policy") or {})
                        _plan.update({k: v for k, v in _auto_plan.items()
                                      if k in ("tasks", "directions", "hierarchy", "pipeline", "timeout_s",
                                               "delivery_contract", "required_artifacts", "acceptance",
                                               "verification", "team_policy", "result_policy", "pattern",
                                               "experience_intent", "golden_task", "quality_profile",
                                               "north_star", "critical_dimensions", "claim_ceiling",
                                               "frontier_research_contract", "quality_protocol")})
                        # 调用方显式 team_policy 是本请求的运行 Context 边界（候选池/队伍上限/
                        # 调度与补位预算）。规划器只补未指定字段，不能静默改写实验条件或用户组队。
                        if _explicit_team_policy:
                            _plan["team_policy"] = {
                                **dict(_plan.get("team_policy") or {}), **_explicit_team_policy}
                        # 调用方显式契约是底线；规划器只能补充验收说明，不能把 MD+JSON 改成任选其一。
                        if _explicit_contract:
                            _generated_contract = dict(_plan.get("delivery_contract") or {})
                            _merged_contract = {**_generated_contract, **_explicit_contract}
                            if _explicit_contract.get("required_artifacts") is not None:
                                _merged_contract["required_artifacts"] = list(_explicit_contract["required_artifacts"])
                            _plan["delivery_contract"] = _merged_contract
                        _plan["_planner_generated"] = True
                        # ★简单题 solo 快路径(用户 2026-09-01 loop 揪出:一句话简单题走 pk 多家并行
                        # 竞选/PK·67s;单家run_one才4-6s)。规划器判本单只需 1 家(简单题)→直接单家直答·
                        # 跳过 pk 多家并行 + 首脑竞选(那对简单题纯浪费)。规划器已判该谁最合适·尊重它。
                        _pt = _auto_plan.get("tasks") or {}
                        _solo_bound = {"ok": False}
                        if isinstance(_pt, dict) and len(_pt) == 1:
                            try:
                                import collaboration_runtime as _collaboration
                                _solo_valid = set(LEGION._harness_keys())
                                _solo_runnable = set(LEGION._aigc_ready().get("runnable_clis", []))
                                if _candidate_pool:
                                    _solo_valid &= _candidate_pool
                                    _solo_runnable &= _candidate_pool
                                _solo_bound = _collaboration.bind_single_task(
                                    _plan, _solo_valid, runnable=_solo_runnable)
                            except Exception:
                                _solo_bound = {"ok": False}
                        if (isinstance(_pt, dict) and len(_pt) == 1 and _solo_bound.get("ok")
                                and _solo_bound.get("use_solo_fastpath")):
                            _plan = dict(_solo_bound.get("plan") or _plan)
                            try:
                                personal_identity.bind_plan_attachments(_plan, _uid, str(sid))
                            except PermissionError:
                                _finish_planning_failure({
                                    "ok": False, "error": "attachment_not_authorized",
                                    "blocked_before_native": True, "execution_calls": 0,
                                }, _plan, mode)
                                return
                            # solo 快答路补发 tasklist（2026-09-18）：多成员路径走
                            # legion_core 的 tasklist 播报，快答路此前不发——前端成员
                            # 未注册、过程事件被丢、执行动态空白。字段对齐 legion_core
                            # 的播报契约（key/task_id/harness/task），前端 tasklist 分支
                            # 直接消费注册成员。
                            cb({"agent": _solo_bound.get("harness") or "commander", "t": "tasklist",
                                "captain": _solo_bound.get("harness") or "", "mode": "solo",
                                "tasks": [{"key": _solo_bound.get("harness") or "",
                                           "task_id": _solo_bound.get("task_id") or "",
                                           "harness": _solo_bound.get("harness") or "",
                                           "task": _solo_bound.get("task") or "回答问题",
                                           "parent": None, "dependencies": [], "status": "pending"}]})
                            try:
                                from delivery_progress import MilestoneEmitter as _SoloMilestoneEmitter
                                _solo_milestone = _SoloMilestoneEmitter(
                                    cb, dict(_plan.get("delivery_contract") or {}))
                                _solo_milestone.announce_plan(
                                    _plan, dict(_plan.get("project_context") or {}))
                            except Exception:
                                _solo_milestone = None

                            def _solo_progress(event):
                                if _solo_milestone and event.get("t") == "artifact_ready":
                                    try:
                                        _solo_milestone.observe_artifact(event)
                                    except Exception:
                                        pass
                                cb(event)

                            _solo_task_id = str(_solo_bound["task_id"])
                            _solo_key = str(_solo_bound["harness"])
                            _solo_task = str(_solo_bound.get("task") or question)
                            _solo_task += LEGION._media_prelude(
                                question, _plan.get("attachments") or [], cb)
                            _solo_dir = str(_solo_bound.get("direction") or "programming")
                            # ★solo 也要多轮上下文(2026-09-02 d30 三连问真跑揪出):run_one 不走
                            #   _build_prelude(那是 dispatch 多家路径的)·solo 只看到裸问题→
                            #   round2 答不出 round1 刚说的暗号("当前会话未发现"·像记忆断链其实是
                            #   上下文没喂到)。把同一份 conversation_context 拼进 solo 任务文
                            #   (context not control·同 _build_prelude 格式)·续接对话不重问。
                            _hctx = str(_plan.get("conversation_context") or "").strip()
                            if _hctx:
                                _solo_task = _solo_task + (
                                    "\n\n【本对话最近历史·只读上下文·请自然续接,不要重新问已回答的信息】\n\n"
                                    + _hctx)
                            # The planner/binder selected this member from the request's candidate
                            # context. Quick describes depth, not permission to replace that choice
                            # with a hardcoded light member outside the candidate/model configuration.
                            # An unlocked quick suggestion may still use the established fast remap,
                            # restricted to the actual candidate pool and runnable declarations.
                            if (not bool(((_plan.get("tasks") or {}).get(_solo_task_id) or {}).get("assignment_locked"))
                                    and str(_solo_dir).strip() in ("快答", "fast", "quick")):
                                try:
                                    _quick_matrix = LEGION._autonomy_matrix()
                                    _quick_ready = set(LEGION._aigc_ready().get("runnable_clis", []))
                                    if _candidate_pool:
                                        _quick_ready &= _candidate_pool
                                    if str(_quick_matrix.get(_solo_key, {}).get("cost_class")) != "light":
                                        _quick_member = next((key for key in ("cc", "hermes", "penguin")
                                            if str(_quick_matrix.get(key, {}).get("cost_class")) == "light"
                                            and key in _quick_ready), "")
                                        if _quick_member:
                                            _solo_key = _quick_member
                                except Exception:
                                    pass
                            # ★按活的性质选模型也要落到 solo(用户 2026-09-01 真跑揪出:solo 只传
                            #   direction 不传 model→run_one 走 select_usable_model(快答→归一
                            #   programming→坏的gpt-5.4)→场景选模型全失效·简单题 66s)。这里显式
                        #   走 resolve_assignments 拿 scene 对口模型(快答→正式主力 gpt-5.6-sol·
                        #   中文→GLM)+独立appid·传 preferred_model，避免场景模型选择纸面生效。

                            try:
                                import model_registry as _mr
                                _solo_as = _mr.resolve_assignments(
                                    {**_plan, "directions": {
                                        **dict(_plan.get("directions") or {}), _solo_key: _solo_dir}},
                                    [_solo_key]).get(_solo_key, {})
                            except Exception:
                                _solo_as = {}
                            try:
                                import runtime_manifest as _rm_depth
                                _solo_depth = "quick" if str(_solo_dir).strip() in ("快答", "fast", "quick") \
                                    and _rm_depth.supports_depth(_solo_key, "quick") else "standard"
                            except Exception:
                                _solo_depth = "standard"
                            cb({"agent": "legion", "t": "reason",
                                "x": f"简单题·{_solo_key} Harness原生{_solo_depth}档直答(跳过多家PK/竞选)",
                                "execution_depth": _solo_depth})
                            _uid_s = str(payload.get("uid") or f"sess:{sid}")
                            # 与 DAG 节点共用同一 Project Context / TaskFacts 通道。旧 solo 只传裸 task，
                            # 会让已经理解正确的目标、验收和现有产物在真正执行时消失。
                            _solo_context = None
                            _solo_packet = {}
                            try:
                                import project_context as _solo_context
                                from legion_contracts import CapabilityPolicy as _SoloCapabilityPolicy
                                from legion_contracts import TaskFacts as _SoloTaskFacts
                                _solo_packet = _solo_context.initialize(
                                    _team_id, question, _plan, mode="single", user_id=_uid_s,
                                    conversation_id=str(sid))
                                _solo_snapshot = _solo_context.snapshot(
                                    _team_id, _solo_task_id, packet=_solo_packet,
                                    execution_depth=_solo_depth)
                                _solo_facts = _SoloTaskFacts(
                                    goal=question, role=_solo_task, task_id=_solo_task_id,
                                    team_id=_team_id,
                                    delivery_contract=dict(_plan.get("delivery_contract") or {}),
                                    orchestration={"pattern": "single", "assignment": {
                                        "task_id": _solo_task_id, "harness": _solo_key,
                                        "source": "solo_fastpath"}},
                                    deliverables=list(_plan.get("deliverables") or []),
                                    acceptance=list(_plan.get("acceptance") or []) if isinstance(
                                        _plan.get("acceptance"), list) else
                                        ([str(_plan.get("acceptance"))] if _plan.get("acceptance") else []),
                                    project_context=_solo_snapshot,
                                ).to_dict()
                                _solo_capability = _SoloCapabilityPolicy.from_value(
                                    _plan.get("capability_policy") or {}).to_dict()
                            except PermissionError:
                                raise
                            except Exception:
                                _solo_snapshot = dict(_plan.get("project_context") or {})
                                _solo_facts = {"goal": question, "role": _solo_task,
                                    "task_id": _solo_task_id, "team_id": _team_id,
                                    "delivery_contract": dict(_plan.get("delivery_contract") or {}),
                                    "project_context": _solo_snapshot}
                                _solo_capability = dict(_plan.get("capability_policy") or {})
                            _solo_brief = LEGION._build_runtime_brief(question, _plan, user_id=_uid_s)
                            # 快执行不跳过任务事实：初次和补位都用同一逻辑节点的 begin/finish，
                            # 与普通执行路径共享代次、结果和取消语义，不留空 team 或假 pending。
                            _solo_runs = []
                            def _run_solo_attempt(_owner, _assignment, continuation=None):
                                _node = (_plan.get("tasks") or {}).get(_solo_task_id)
                                _node = dict(_node) if isinstance(_node, dict) else {}
                                _bound_node = {**_node, "task": _solo_task,
                                               "assigned_harness": _owner}
                                _attempt_facts = dict(_solo_facts)
                                _orchestration = dict(_attempt_facts.get("orchestration") or {})
                                _orchestration["assignment"] = {
                                    **dict(_orchestration.get("assignment") or {}),
                                    "task_id": _solo_task_id, "harness": _owner}
                                _attempt_facts["orchestration"] = _orchestration
                                if _solo_context:
                                    # Remapping/relay changes the real executor, not the
                                    # logical task. Sync only that binding under the
                                    # project lock; don't replay stale plan/grant facts.
                                    _bound_packet = _solo_context.record_task_bindings(
                                        _team_id, {_solo_task_id: _bound_node},
                                        user_id=_uid_s, conversation_id=str(sid))
                                    _attempt_facts["project_context"] = _solo_context.snapshot(
                                        _team_id, _solo_task_id, packet=_bound_packet,
                                        execution_depth=_solo_depth)
                                TEAMS.ensure(
                                    _team_id, question,
                                    {_solo_task_id: _bound_node},
                                    {_solo_task_id: list(_node.get("depends_on") or
                                                        _node.get("dependencies") or [])},
                                    delivery_contract=dict(_plan.get("delivery_contract") or {}),
                                    user_id=_uid_s, conversation_id=str(sid))
                                personal_identity.bind_plan_attachments(_plan, _uid_s, str(sid))
                                _attempt = TEAMS.begin_attempt(_team_id, _solo_task_id,
                                                               owner=f"server:{os.getpid()}")
                                try:
                                    _result = LEGION.run_one(
                                        _owner, _solo_task, cb=_solo_progress, user_id=_uid_s,
                                        conversation_id=str(sid), direction=_solo_dir, qhash=_team_id,
                                        preferred_model=_assignment.get("model"),
                                        preferred_appid=_assignment.get("appid"),
                                        runtime_brief=_solo_brief,
                                        task_facts={**_attempt_facts, "attempt_id": _attempt},
                                        capability_policy=_solo_capability,
                                        cancel_check=lambda: LEGION._request_cancelled(
                                            _team_id, _solo_task_id, _owner),
                                        execution_depth=_solo_depth, _continuation_result=continuation,
                                        _model_selection_source=str(_assignment.get("source") or ""),
                                        _provider_results=lambda: list(_solo_runs))
                                except BaseException as _exc:
                                    TEAMS.finish_attempt(
                                        _team_id, _solo_task_id, _attempt,
                                        {"status": "error", "final": "",
                                         "error": f"{type(_exc).__name__}: {_exc}"})
                                    raise
                                _result = {**_result, "task_id": _solo_task_id}
                                TEAMS.finish_attempt(_team_id, _solo_task_id, _attempt, _result)
                                _solo_runs.append(_result)
                                return _result

                            _r = _run_solo_attempt(_solo_key, _solo_as)
                            # ★solo 失败补位接力(2026-09-02 真跑揪出:cc router 半死→solo 直答失败
                            #   →"已试 N 个模型仍未通"直接交白卷·用户拿到 error·没有换家兜底)。solo 是
                            #   单家·没走 dispatch 多家路径的补位接力——失败时串一个轻量 fallback:换下一个
                            #   light 快家接力(hermes/penguin)·最多试 2 家·用户仍拿到答案(铁律0"一家坏了
                            #   别家补位"·solo 不能是白卷豁免口)。前置:run_one 内部已先穷尽换模型自愈。
                            _solo_tried = [_solo_key]
                            if (_r.get("status") not in {"done", "cancelled"} and not _r.get("cancelled")
                                    and not __import__("error_envelope").requires_execution_pause(_r)):
                                try:
                                    _am = LEGION._autonomy_matrix()
                                    _ready = set(LEGION._aigc_ready().get("runnable_clis", []))
                                    if _candidate_pool:
                                        _ready &= _candidate_pool
                                    _fb = [k for k, v in _am.items()
                                           if str(v.get("cost_class")) == "light" and k in _ready
                                           and k != _solo_key]
                                    for _fbk in _fb[:2]:
                                        cb({"agent": "legion", "t": "reason",
                                            "x": f"{_solo_key} 未能交付·{_fbk} 补位接力(solo 兜底)"})
                                        # ★接力家用【自己的精选模型】·不继承原家的 preferred(2026-09-02
                                        #   真跑揪出:补位把 cc 的 preferred=deepseek-v4-flash 传给 penguin/
                                        #   prime→penguin 用不对口模型也 incomplete 失败·浪费一轮)。
                                        #   各家有自己的推荐清单(resolve_assignments 按 key 给对口模型)。
                                        try:
                                            import model_registry as _mr2
                                            _fb_as = _mr2.resolve_assignments(
                                                {**_plan, "directions": {
                                                    **dict(_plan.get("directions") or {}), _fbk: _solo_dir}},
                                                [_fbk]).get(_fbk, {})
                                        except Exception:
                                            _fb_as = {}
                                        # 原运行已结束，补位是同一节点的新代次，不覆盖旧结果历史。
                                        TEAMS.reopen_task(_team_id, _solo_task_id, agent=_fbk,
                                                          reason="solo_failover")
                                        _r2 = _run_solo_attempt(_fbk, _fb_as)
                                        _solo_tried.append(_fbk)
                                        if (_r2.get("status") in {"done", "cancelled"}
                                                or _r2.get("cancelled")
                                                or __import__("error_envelope").requires_execution_pause(_r2)):
                                            _r = _r2
                                            break
                                        _r = _r2
                                except Exception:
                                    pass
                            _r = dict(_r)
                            _solo_owner = str(_r.get("harness") or _r.get("key") or _solo_tried[-1])
                            _r.update({"key": _solo_task_id, "task_id": _solo_task_id,
                                       "harness": _solo_owner, "assigned_harness": _solo_owner})
                            if _solo_context:
                                try:
                                    _solo_packet = _solo_context.record_result(
                                        _team_id, _solo_task_id, _r,
                                        assigned_harness=_solo_owner)
                                    if _solo_milestone:
                                        _solo_milestone.observe_context(
                                            _solo_packet, include_gaps=False,
                                            include_journey=bool(_r.get("journey_observations")))
                                        _solo_milestone.update([_r])
                                except Exception:
                                    pass
                            _solo_peer_results = []
                            _solo_as = _fb_as if len(_solo_tried) > 1 else _solo_as
                            import legion_pilot as _solo_pilot
                            try:
                                _solo_pilot_on = bool(LEGION.pilot_settings(_uid_s).get("enabled"))
                            except Exception:
                                _solo_pilot_on = False
                            for _peer_round in range(1, 6):
                                if (not _solo_pilot_on or _solo_depth == "quick"
                                        or not isinstance((_r.get("orchestration_request") or {}).get("need_peer"), dict)
                                        or __import__("error_envelope").requires_execution_pause(_r)
                                        or LEGION._request_cancelled(_team_id, _solo_task_id, _solo_owner)):
                                    break
                                _peer_result = LEGION._run_requested_peer(
                                    question, _plan, _r, task_id=_solo_task_id, team_id=_team_id,
                                    user_id=_uid_s, conversation_id=str(sid), cb=cb,
                                    runtime_brief=_solo_brief, project_packet=_solo_packet,
                                    _provider_results=lambda: list(_solo_runs),
                                    cancel_check=lambda: LEGION._request_cancelled(
                                        _team_id, _solo_task_id, _solo_owner))
                                if _peer_result:
                                    _solo_peer_results.append(_peer_result)
                                    if not _peer_result.get("retained_peer_input"):
                                        _solo_runs.append(_peer_result)
                                if _r.get("peer_input_error"):
                                    _r["pilot"] = _peer_stop_pilot(_r, _peer_round)
                                    break
                                if (_r.get("peer_input_pause") or (_peer_result and
                                        __import__("error_envelope").requires_execution_pause(_peer_result))):
                                    _r["peer_input_pause"] = _r.get("peer_input_pause") or _peer_result
                                    _r["pilot"] = _peer_stop_pilot(_r, _peer_round)
                                    break
                                if _solo_pilot.evaluate(str(_r.get("final") or ""),
                                                       LEGION._pilot_run_state(_r), _uid_s).get("action") != "continue":
                                    break
                                if not _peer_result or LEGION._request_cancelled(
                                        _team_id, _solo_task_id, _solo_owner):
                                    break
                                TEAMS.reopen_task(_team_id, _solo_task_id, agent=_solo_owner,
                                                  reason="requested_peer_input_returned")
                                _previous = _r
                                _r = _run_solo_attempt(_solo_owner, _solo_as, continuation=_previous)
                                from artifact_provenance import merge_artifacts as _merge_solo_artifacts
                                _r["artifacts"] = _merge_solo_artifacts(
                                    _previous.get("artifacts"), _r.get("artifacts"))
                                _r.update(key=_solo_task_id, task_id=_solo_task_id,
                                          harness=_solo_owner, assigned_harness=_solo_owner)
                                if _solo_context:
                                    _solo_packet = _solo_context.record_result(
                                        _team_id, _solo_task_id, _r, assigned_harness=_solo_owner)
                            if _solo_peer_results:
                                _r["peer_results"] = _solo_peer_results
                            _solo_out = {"results": [_r], "solo": True, "captain": _solo_owner,
                                         "team_id": _team_id,
                                         "task_assignments": {_solo_task_id: _solo_owner},
                                         "assignment_decisions": list(_solo_bound.get("decisions") or []),
                                         "execution_depth": _solo_depth, "solo_tried": _solo_tried,
                                         "delivery_contract": dict(_plan.get("delivery_contract") or {})}
                            _peer_stop = _peer_stop_pilot(_r, _peer_round)
                            if _peer_stop:
                                _solo_out["pilot"] = _peer_stop
                            # 共用普通 dispatch 的原生用量汇总；补位消耗同样保留，不把未知报成零。
                            import token_telemetry as _solo_usage
                            _solo_out["usage"] = _solo_usage.summarize(_solo_runs)
                            try:
                                import task_outcome as _task_outcome
                                _task_outcome.attach(_solo_out, _solo_out["delivery_contract"])
                            except Exception:
                                _solo_out.update({"delivered_count": 1 if _r.get("status") == "done" else 0,
                                                  "total": 1, "all_failed": _r.get("status") != "done",
                                                  "timed_out": 0, "partial": False})
                            _solo_out["completion"] = {"delivered": _solo_out["delivered_count"],
                                                       "total": _solo_out["total"],
                                                       "status": (_solo_out.get("outcome") or {}).get("status")}
                            if _peer_stop:
                                _solo_out["status"] = (_solo_out.get("outcome") or {}).get("status", "partial")
                            # 阶段2·快路径账本回写(P0-A·2026-09-02):预判 simple 的 solo 落定后记账
                            # (一次自交付=good;失败/靠补位=bad)。连败≥3零成功→fastpath_healthy 转 False
                            # →预判自动停用退规划器(数据长出来的阈值·预判器恢复即重启)。
                            try:
                                __import__("quick_pred").record_fastpath(
                                    ok=((_solo_out.get("outcome") or {}).get("status") == "completed"
                                        and len(_solo_tried) == 1))
                            except Exception:
                                pass
                            try:
                                _si = _save_query_turn(_solo_out, "solo", _plan)
                                _solo_out["session"] = _si
                                try:
                                    __import__("performance_views").invalidate(_uid_s)
                                    __import__("user_view").invalidate(_uid_s)
                                except Exception:
                                    pass
                            except Exception:
                                pass
                            cb({"agent": "legion", "t": "legion_done", **_solo_out})
                            # ★solo 快路径 final 补发(2026-09-02 q30d 真跑揪出·本 session 最深 bug):
                            #   旧代码 legion_done 后直接 return→final 事件从未落盘/从未推给客户端→断线
                            #   续播拿不到结果·验收等 360s 判死(实际已交付·d17/d19/简单题全中·d16 走 pk
                            #   路径有 final 才 PASS)。修=对齐 captain-solo:快照隔离+append final+put final。
                            try:
                                _solo_final = json.loads(json.dumps(_solo_out, ensure_ascii=False))
                            except Exception:
                                _solo_final = dict(_solo_out)
                            try:
                                _fr = TEAMS.append_event(_team_id, _solo_final, "final")
                            except Exception:
                                _fr = None
                            _evq.put(("final", _solo_final, (_fr or {}).get("event_id")))
                            # 慢同步(会话桌面物化·docker cp 级)后台化·不挡交付(同主路径修法·d17 实测拖 360s)
                            def _solo_post_sync(_u=_uid_s, _s=sid, _o=_solo_out, _t=_team_id):
                                try:
                                    _ds = DESKTOP_FS.materialize_session(_u, _s, _o)
                                except Exception:
                                    _ds = None
                                if isinstance(_ds, dict):
                                    try:
                                        _o["desktop_session"] = _ds
                                        TEAMS.append_event(_t, {"t": "desktop_session_ready", "desktop_session": _ds}, "step")
                                    except Exception:
                                        pass
                            try:
                                threading.Thread(target=_solo_post_sync, daemon=True,
                                                 name=f"postsync-{sid[:8]}").start()
                            except Exception:
                                pass
                            return
                        cb({"agent": "legion", "t": "reason",
                            "x": f"自动选定队形:{ {'pk':'多家并行比最佳','relay':'流水线接力','orchestrate':'首脑统筹编排'}.get(mode,mode) }·{_auto_plan.get('reason','')[:60]}"})
                    elif not _plan.get("tasks"):
                        _finish_planning_failure(_auto_plan, _plan, "auto")
                        return
                # ★模式决定要不要首脑(用户2026-08-30):PK模式=各军团并行直接开始·【无首脑】;
                # 编排模式(orchestrate)=首脑竞选编排。relay=串行协作也无首脑。
                # 用户可传 mode=orchestrate 走首脑·或对话里说"帮我编排"切到编排(前端映射mode)。
                _need_captain = (mode == "orchestrate")
                # 未显式指定队形且【是编排模式】时由可运行 harness 动态竞选首脑。选举放后台线程。
                if _need_captain and not (_plan.get("tasks")):
                    _captain_plan_accepted = False
                    cb({"agent": "legion", "t": "captain_election", "status": "running",
                        "x": "军团成员正在基于目标和现场自主竞选本单首脑…"})
                    elected = LEGION.elect_captain(
                        question, _plan, cb=cb, qhash=_team_id,
                        user_id=str(payload.get("uid") or f"sess:{sid}"),
                        conversation_id=str(sid))
                    # ★简单题快速直答短路(用户 2026-08-29:"吃什么"卡在多家竞选)。候选判定
                    # solo→答案已在竞选轮生成·直接交付·不再走 dispatch 二次派活(省一整轮网关)。
                    if elected.get("ok") and elected.get("solo"):
                        _cap = elected["captain"]
                        _ans = str(elected.get("plan", {}).get("_solo_answer") or "")
                        _original = elected.get("original_result")
                        _candidate_scope = elected.get("candidate_scope") or {}
                        if (not isinstance(_original, dict) or not _candidate_scope.get("task_id")
                                or _candidate_scope.get("team_id") != _team_id
                                or _candidate_scope.get("harness") != _cap):
                            _finish_planning_failure({
                                "ok": False, "status": "error",
                                "error": "captain_solo_result_unavailable"}, _plan, mode)
                            return
                        # Adopt the actual candidate execution, without replay
                        # or replacing its native cursor/usage/checked exports.
                        _solo_task_id = str(_candidate_scope["task_id"])
                        _r = deepcopy(_original)
                        _r.update(key=_solo_task_id, task_id=_solo_task_id,
                                  harness=_cap, assigned_harness=_cap)
                        if _r.get("status") == "done":
                            _r["final"] = _ans  # Strip election protocol only.
                        _plan = deepcopy(elected["plan"])
                        import project_context as _selected_project
                        _selected_project.record_result(
                            _team_id, _solo_task_id, _r, assigned_harness=_cap,
                            continuation={"action": "captain_solo_selected", **_candidate_scope})
                        TEAMS.replace_result(
                            _team_id, _solo_task_id, _r, reason="captain_solo_selected")
                        # solo 短路补发 tasklist（2026-09-18，与 solo 快答路同因）：
                        # 前端成员注册缺 tasklist 则过程事件全被丢。
                        cb({"agent": _cap, "t": "tasklist", "captain": _cap, "mode": "solo",
                            "tasks": [{"key": _solo_task_id, "task_id": _solo_task_id, "harness": _cap,
                                       "task": question[:500], "parent": None,
                                       "dependencies": [], "status": _r.get("status", "error")}]})
                        _solo_out = {"results": [_r],
                                     "mode_reason": {"mode": "simple", "triggers": []},
                                     "solo": True, "captain": _cap,
                                     "team_id": _team_id,
                                     "task_assignments": {_solo_task_id: _cap},
                                     "dependencies": {_solo_task_id: []},
                                     "candidate_scope": dict(_candidate_scope),
                                     "usage": deepcopy(_r.get("usage") or {}),
                                     "execution_mode": mode,
                                     "delivery_contract": dict(_plan.get("delivery_contract") or {})}
                        try:
                            import task_outcome as _task_outcome
                            _task_outcome.attach(_solo_out, _solo_out["delivery_contract"])
                        except Exception:
                            _solo_out.update({"delivered_count": int(_r.get("status") == "done"), "total": 1,
                                              "all_failed": _r.get("status") != "done", "partial": False})
                        try:
                            _si = _save_query_turn(_solo_out, mode, _plan)
                            _solo_out["session"] = _si
                            try:
                                __import__("performance_views").invalidate(_uid)
                                __import__("user_view").invalidate(_uid)
                            except Exception:
                                pass
                            _solo_out["desktop_session"] = DESKTOP_FS.materialize_session(_uid, sid, _solo_out)
                            try:
                                DESKTOP_FS.sync_personal_data(_uid)
                            except Exception:
                                pass
                        except Exception:
                            pass
                        try:
                            _fr = TEAMS.append_event(_team_id, _solo_out, "final")
                        except Exception:
                            _fr = None
                        _evq.put(("final", _solo_out, (_fr or {}).get("event_id")))
                        return   # finally 会统一 put(_DONE)·此处不重复
                    if elected.get("ok"):
                        _plan = elected["plan"]
                        _captain_plan_accepted = True
                        _plan["captain_bids"] = elected.get("bids") or []
                        _plan["captain_reason"] = elected.get("reason") or ""
                        cb({"agent": elected["captain"], "t": "captain_selected", "status": "done",
                            "captain": elected["captain"], "bids": elected.get("bids") or [],
                            "x": f"{elected['captain']} 当选本单首脑：{elected.get('reason') or ''}"})
                    else:
                        # 选举基础设施失败才走兼容规划器，保证用户请求不白卷；Kimi 只是 fallback，
                        # 不再拥有固定总指挥身份。
                        try:
                            planned = LEGION.plan_request_with_model(
                                question, _plan.get("attachments") or [],
                                candidate_harnesses=list(_candidate_pool) or None,
                                request_context=_plan,
                                user_id=_uid, conversation_id=str(sid),
                                cancel_check=lambda: LEGION.is_cancelled(_team_id),
                                _provider_results=lambda: list(elected.get("provider_results") or []))
                        except Exception as _e:
                            planned = {"ok": False, "error": f"planner_call:{type(_e).__name__}"}
                        if (planned.get("cancelled") or planned.get("status") == "cancelled"
                                or LEGION.is_cancelled(_team_id)):
                            _finish_planning_failure(planned, _plan, mode)
                            return
                        if planned.get("ok"):
                            _fallback_team_policy = dict(_plan.get("team_policy") or {})
                            if _fallback_team_policy.get("continuous_coordination") is None:
                                _fallback_team_policy.pop("continuous_coordination", None)
                            _plan.update(planned)
                            _captain_plan_accepted = True
                            if _fallback_team_policy:
                                _plan["team_policy"] = {
                                    **dict(_plan.get("team_policy") or {}), **_fallback_team_policy}
                            _runtime_captain = LEGION._appoint_runtime_captain(
                                _plan, candidate_harnesses=list(_candidate_pool) or None)
                            if _runtime_captain:
                                _plan["captain"] = _runtime_captain
                                _plan["captain_reason"] = "竞选超时后按当前方向实测适任度任命持续首脑"
                            cb({"agent": _runtime_captain or "legion", "t": "captain_selected",
                                "status": "fallback", "captain": _runtime_captain or "unselected",
                                "x": (f"{_runtime_captain} 接任持续首脑；" if _runtime_captain else "未选出持续首脑；")
                                     + f"兼容规划已就绪：{planned.get('reason') or ''}"})
                        elif (LEGION._personal_responses_planner_enabled()
                              or __import__("error_envelope").requires_execution_pause(planned)):
                            _finish_planning_failure(planned, _plan, mode)
                            return
                        else:
                            cb({"agent": "legion", "t": "reason",
                                "x": f"首脑竞选与兼容规划均未就绪（{elected.get('error','')[:50]}）→ 默认成员继续，不让整单丢失"})
                            _plan["_fallback_direction"] = "general_chat_high_coverage"
                    if _captain_plan_accepted:
                        if _request_continuous_coordination is not None:
                            _plan["team_policy"] = {
                                **dict(_plan.get("team_policy") or {}),
                                "continuous_coordination": _request_continuous_coordination}
                        # 采纳真实首脑计划后启用既有协调，不借用会影响绑定的 planner 标记。
                        # 显式策略仍由 _coordination_enabled 优先处理；solo/手工计划不走这里。
                        _plan.setdefault("_continuous_coordination", True)
                # 自动规划已经给出 tasks 时，旧逻辑会跳过上面的竞选，导致复杂任务 captain 一直是
                # legion/unselected：没有任何真实 Harness 在派单后持续消费结果和调整军团。这里低延迟
                # 任命当前实测最适合的真实成员为运行期首脑；不重做首批计划、不阻塞开工，后续协调循环
                # 才由它基于真实结果提出下一轮任务。
                if (_need_captain and str(_plan.get("captain") or "").strip() not in
                        set(LEGION._harness_keys()) and _plan.get("tasks")):
                    _runtime_captain = LEGION._appoint_runtime_captain(
                        _plan, candidate_harnesses=list(_candidate_pool) or None)
                    if _runtime_captain:
                        _plan["captain"] = _runtime_captain
                        _plan["captain_reason"] = "已有首批计划·按当前方向实测适任度任命持续首脑"
                        cb({"agent": _runtime_captain, "t": "captain_selected", "status": "done",
                            "captain": _runtime_captain,
                            "x": f"{_runtime_captain} 接任持续首脑·首批任务立即开工，结果返回后动态调队"})
                # 首脑或兼容规划器可决定执行方案，但不能改写订单来源、输入快照和版本身份。
                _plan["source_ref"] = dict(_source_ref)
                _plan["input_snapshot"] = dict(plan.get("input_snapshot") or {})
                _plan["version_identity"] = dict(_version_identity)
                _plan["execution_role"] = "champion"
                _plan["quality_context"] = _quality_context_with_source(question, _plan, _source_ref)
                # 跨轮自主循环(用户 goal·军团最想要):pilot 开启→走 dispatch_persistent,
                # 一轮没达成 pilot 自动带轨迹续跑直到完成/阻塞/上限;pilot 关→退化单轮 dispatch。
                # uid 在请求入口已冻结；worker 内不得再次赋值，否则 Python 会把它视为局部变量，
                # 让前面的 direct/solo 提前返回分支读取未初始化 `_uid`，静默丢失 session。
                try:
                    _pilot_on = bool(LEGION.pilot_settings(_uid).get("enabled"))
                except Exception:
                    _pilot_on = False
                # user_id 贯穿(军团自审 codex/deepseek P0):有真实 uid 用 uid;匿名用稳定
                # session_id 作 uid→同一会话的长期记忆连续、又不与别人共用 memory/default。
                _eff_uid = _uid if _uid != "default" else f"sess:{sid}"
                try:
                    personal_identity.bind_plan_attachments(_plan, _uid, str(sid))
                except PermissionError:
                    _finish_planning_failure({
                        "ok": False, "error": "attachment_not_authorized",
                        "blocked_before_native": True, "execution_calls": 0,
                    }, _plan, mode)
                    return
                _dispatch_t0 = time.time()   # 记派活起点·enrich 据此扫本次窗口新增文件产物
                _continuous_team = bool(mode == "orchestrate" and _plan.get("captain")
                                        and len(_plan.get("tasks") or {}) > 1)
                if _pilot_on or _continuous_team:
                    out = LEGION.dispatch_persistent(question, plan=_plan, cb=cb, mode=mode,
                                                     qhash=_team_id, user_id=_eff_uid,
                                                     conversation_id=str(sid))
                else:
                    out = LEGION.dispatch(question, plan=_plan, cb=cb, mode=mode,
                                          qhash=_team_id, user_id=_eff_uid,
                                          conversation_id=str(sid))
                # 对外保留用户选的执行模式。orchestrate 在 core 内以首脑计划+DAG波次执行，
                # 不能因底层复用并行执行器而在最终结果里丢成 pk。
                out["execution_mode"] = mode
                if isinstance(_plan.get("initial_author"), dict):
                    out["initial_author"] = deepcopy(_plan["initial_author"])
                out.setdefault("question", question)
                out.setdefault("team_id", _team_id)
                # since_ts:主动扫本次派活期间新产生的文件(codex等写了代码/文件但final只回摘要
                # 没写路径的·也能被收集展示·治"171字假交付"其实真建了文件夹的坑)。留2秒余量防时钟抖动。
                # 产物隔离(S3 根因②·2026-09-02):只扫本单各成员真实 workdir·多单并发时
                # 别的单/别的用户在窗口内新写的文件不再混进本单交付(12家并发1325s复盘揪出的污染)。
                _wds = [str(r.get("workdir") or "") for r in out.get("results", [])]
                ARTIFACTS.enrich(out, since_ts=_dispatch_t0 - 2, workdirs=_wds or None)
                # 产物扫描完成后统一重算 TaskOutcome。否则 core 在文件出现前判 completed，
                # server enrich 后仍沿用旧 outcome，会把“要求图片但只有文字”错误亮绿。
                try:
                    import task_outcome as _task_outcome
                    _task_outcome.attach(
                        out, out.get("delivery_contract") or _plan.get("delivery_contract"))
                except Exception:
                    pass
                # ★ 落盘在后台线程内·脱离 SSE 连接:客户端早断了这里照样存,任务不白干。
                try:
                    info = _save_query_turn(out, mode, _plan)
                    out["session"] = info
                    try:
                        __import__("performance_views").invalidate(_uid)
                        __import__("user_view").invalidate(_uid)
                    except Exception:
                        pass
                except Exception as _se:
                    out["session_error"] = str(_se)[:100]
                # ★桌面会话物化+个人目录同步移后台线程(速度底线·2026-09-02 q30d 真跑揪出):这两步
                #   是 docker cp 级慢操作(d17 实测拖 360s)·旧顺序把 final 事件阻塞在它们后面→军团
                #   267s 交付·用户 627s 才收到·SSE 静默期还被代理掐断(final 丢失+假 FAIL)。final 必须
                #   save_turn 后立即发出;同步在后台跑完把 desktop_session 字段补进 out(状态读取可见)。
                # ★final 快照(2026-09-02 final 丢失双根因收口):postsync 后台线程并发给 out 加
                #   desktop_session 键→json.dumps 迭代中字典变形→append_event/_sse_send 双双静默
                #   失败(d19/d20/简单题全复现"legion_done 后无 final")。修=final 用 json 往返快照
                #   (保证可序列化+与后台线程完全隔离)·postsync 结果另发 step 事件(刷新续播可见)。
                _final_out = None
                try:
                    _final_out = json.loads(json.dumps(out, ensure_ascii=False))
                except Exception:
                    _final_out = dict(out)
                def _post_desktop_sync(_uid=_uid, _sid=sid, _out=out, _team=_team_id):
                    try:
                        _ds = DESKTOP_FS.materialize_session(_uid, _sid, _out)
                    except Exception as _de:
                        _ds = {"ok": False, "error": str(_de)[:160]}
                    try:
                        DESKTOP_FS.sync_personal_data(_uid)
                    except Exception:
                        pass
                    try:
                        _out["desktop_session"] = _ds
                        TEAMS.append_event(_team, {"t": "desktop_session_ready", "desktop_session": _ds}, "step")
                    except Exception:
                        pass
                try:
                    threading.Thread(target=_post_desktop_sync, daemon=True,
                                     name=f"postsync-{sid[:8]}").start()
                except Exception:
                    pass
                # 断线续播:final 也持久化(带 event_id)·刷新后能补到最终结果·不只补到中途过程。
                try:
                    _final_rec = TEAMS.append_event(_team_id, _final_out, "final")
                except Exception:
                    import sys as _sys4, traceback as _tb4; _tb4.print_exc(file=_sys4.stderr)   # 2026-09-02 诊断:final 落盘失败根因不再静默
                    _final_rec = None
                _evq.put(("final", _final_out, (_final_rec or {}).get("event_id")))
            except PermissionError as exc:
                _evq.put(("error", {"error": "permission_denied", "reason": str(exc)}, None))
            except BaseException as e:   # 2026-09-02 诊断+根治:SystemExit 等被 except Exception 漏掉→线程静默死→final/error 双丢。BaseException 全接·任何死亡必须回流 error 事件(绝不静默)。
                import sys as _sys2; traceback.print_exc(file=_sys2.stderr)
                _err = {"error": f"{type(e).__name__}: {e}",
                        "trace": traceback.format_exc()[-600:]}
                try:
                    _err_rec = TEAMS.append_event(_team_id, _err, "error")
                except Exception:
                    _err_rec = None
                _evq.put(("error", _err, (_err_rec or {}).get("event_id")))
            finally:
                # 用户单量闸释放(门二):终态(完成/失败/取消都算)让位,唤醒该用户排队单
                if _slot_token is not None:
                    try:
                        SESSIONS.user_slot_release(_uid, _slot_token)
                    except Exception:
                        pass
                _dispatch_hb_stop.set()
                _evq.put((_DONE, None, None))

        # daemon=False:进程正常运行期不被回收·保证任务真跑完落盘(不随前台连接死)。
        _t = threading.Thread(target=_worker, name=f"dispatch-{sid[:8]}", daemon=False)
        _t.start()
        # 前台只负责把后台事件转发给 SSE;客户端断开→_sse_send 返 False→停止转发但【不动后台线程】。
        client_alive = True
        _terminal_since = 0.0
        while True:
            try:
                kind, data, event_id = _evq.get(timeout=5.0)
            except _queue.Empty:
                # 成员状态已经全部终态但 core 收尾长期卡住时，不能让调用方无限等 SSE。
                # 给正常产物收集/验证 60s 宽限；随后发结构化 error，让考试驱动可重试本题。
                try:
                    _state = TEAMS.load(_team_id) or {}
                    _tasks = list((_state.get("tasks") or {}).values())
                    _all_terminal = bool(_tasks) and all(str(row.get("status") or "") in
                        {"done", "error", "failed", "cancelled", "blocked", "skipped"} for row in _tasks)
                except Exception:
                    _all_terminal = False
                if _all_terminal:
                    _terminal_since = _terminal_since or time.monotonic()
                    if time.monotonic() - _terminal_since >= 60:
                        _err = {"error": "orchestration finalization timed out after all members became terminal",
                                "retryable": True, "team_id": _team_id}
                        try:
                            _rec = TEAMS.append_event(_team_id, _err, "error")
                        except Exception:
                            _rec = None
                        self._sse_send("error", _err, event_id=(_rec or {}).get("event_id"))
                        break
                else:
                    _terminal_since = 0.0
                continue
            if kind is _DONE:
                break
            if not client_alive:
                continue  # 客户端已断·后台继续跑·我们只是不再往断掉的连接写(排空队列到 DONE)
            if kind == "step":
                if not self._sse_send("step", data, event_id=event_id):
                    client_alive = False
            elif kind == "final":
                self._sse_send("final", data, event_id=event_id)
            elif kind == "error":
                self._sse_send("error", data, event_id=event_id)


def _recent_harness_usage(max_sessions: int = 50, budget_s: float = 0.2,
                           user_id: str = "default") -> dict:
    """最近一单实际用到哪家(UI 四层状态第④层"本轮实际使用"·纯读·不给派单用)。

    扫该用户最近 max_sessions 个 session 的 turns(按 updated 倒序),取每个 harness key
    最后一次出现的 {ts, mode, question前40字}。速度底线:200ms 预算硬上限,超时立即返回
    已扫到的部分(如实缺席胜过拖慢 harnesses 端点)。上游断链铁律:任何异常返回 {}。
    """
    out: dict = {}
    try:
        deadline = time.time() + budget_s
        rows = SESSIONS.list_sessions(limit=max_sessions, user_id=user_id)
        for row in rows:
            if time.time() > deadline:
                break
            try:
                sid = str(row.get("sid") or "")
                d = SESSIONS.load_session(sid, user_id=user_id)
                if not d:
                    continue
                for turn in reversed(d.get("turns", [])):   # 从最新 turn 往回找
                    ts = float(turn.get("ts") or 0)
                    for r in (turn.get("results") or []):
                        k = str(r.get("key") or "")
                        if k and k not in out:              # 已有更新的记录就保留
                            out[k] = {"ts": ts,
                                      "mode": str(turn.get("mode") or ""),
                                      "question": str(turn.get("question") or "")[:40]}
                    if time.time() > deadline:
                        return out
            except Exception:
                continue
    except Exception:
        return out
    return out


def _strip_dead_proxy_env():
    # 死代理免疫(机制保障·2026-09-01 桌面任务38步全 APIConnectionError 的根修):
    # shell 常带着指向已死本地端口的 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY,军团子进程继承后
    # 连 aigc 网关全部 Connection error。启动时探测代理端口,死了就地剔除并留痕——
    # 不依赖任何人记得手动清理。
    import socket
    from urllib.parse import urlparse
    removed = []
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        raw = os.environ.get(key)
        if not raw:
            continue
        try:
            parsed = urlparse(raw if "//" in raw else "//" + raw)
            host, port = parsed.hostname, parsed.port
            if not host or not port:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.4)
                alive = probe.connect_ex((host, port)) == 0
            if not alive:
                os.environ.pop(key, None)
                removed.append(key)  # Proxy URLs may contain credentials; log the variable only.
        except Exception:
            pass
    if removed:
        print("[legion] ⚠️ 检测到死代理已自动剔除(曾致军团 LLM 全断): " + "; ".join(removed))


_feedback_workers: set[str] = set()
_feedback_workers_lock = threading.Lock()


def _feedback_cancel_requests(state: dict, message: dict) -> list[dict]:
    """Forward only Stops recorded after this owned invocation was accepted."""
    import hashlib
    request = message.get("continuation") or {}
    team_id = str(state.get("team_id") or "")
    suffix = hashlib.sha256((team_id + "\0" + str(request.get("request_id") or "")).encode()).hexdigest()[:20]
    if (request.get("source_team_id") != team_id
            or request.get("user_id") != state.get("user_id")
            or request.get("conversation_id") != state.get("conversation_id")
            or request.get("team_id") != "feedback_" + suffix):
        return []
    child = TEAMS.load(request["team_id"])
    if child and (child.get("user_id") != state.get("user_id")
                  or child.get("conversation_id") != state.get("conversation_id")):
        return []
    boundary = (request.get("cancel_boundaries") or {}).get(team_id)
    return [stop for stop in state.get("cancel_requests") or []
            if boundary is None or int(stop.get("sequence") or 0) > int(boundary.get("sequence") or 0)]


def _schedule_feedback_resume(team_id: str, user_id: str, conversation_id: str):
    """Wake the existing recovery entry; at most one local waiter per team."""
    with _feedback_workers_lock:
        if team_id in _feedback_workers:
            return
        _feedback_workers.add(team_id)

    def work():
        registered = True
        try:
            while True:
                # Producers register under the same lock after persisting mail.
                # Reading an empty queue and retiring must be one atomic handoff.
                with _feedback_workers_lock:
                    pending = TEAMS.pending_continuation(TEAMS.load(team_id) or {})
                    if not pending:
                        _feedback_workers.discard(team_id)
                        registered = False
                        return
                request_id = pending["continuation"]["request_id"]
                try:
                    response = _resume_saved_team(
                        team_id, explicit=False, user_id=user_id, conversation_id=conversation_id)
                except Exception as exc:
                    TEAMS.record_continuation(
                        team_id, request_id, status="held", reason=type(exc).__name__)
                    continue
                reason = response.get("reason", "")
                if response.get("status") == "unchanged":
                    if reason in {"active_resume_lease", "active_task_lease", "resume_not_admitted"}:
                        time.sleep(0.5)  # Wait for the existing owner; do not dispatch concurrently.
                        continue
                    TEAMS.record_continuation(
                        team_id, request_id, status="held", reason=reason)
        finally:
            if registered:
                with _feedback_workers_lock:
                    _feedback_workers.discard(team_id)

    threading.Thread(target=work, name=f"feedback:{team_id}", daemon=True).start()


def _continue_feedback(state: dict, packet: dict, message: dict, *, owner: str,
                       user_id: str, conversation_id: str, require_project_access) -> dict:
    """A new user turn in the original native lane, using the existing executor."""
    from copy import deepcopy
    import project_context
    from legion_contracts import resume_plan_from_state, retained_resume_results

    team_id, request = state["team_id"], message["continuation"]
    child, request_id = request["team_id"], request["request_id"]
    session = SESSIONS.load_session(conversation_id, user_id=user_id) or {}
    original = next((str(turn.get("question") or "") for turn in session.get("turns") or []
                     if turn.get("question")), str(packet.get("objective") or state.get("question") or ""))
    feedback = TEAMS.resolve_message_text(message)
    import hashlib
    if hashlib.sha256(feedback.encode()).hexdigest() != request["text_sha256"]:
        # A missing long-body reference cannot silently become a truncated instruction.
        raise ValueError("continuation_feedback_body_unavailable")
    question = (f"原始用户需求：\n{original}\n\n"
                f"用户在原作品上明确提交的新反馈（{request_id}）：\n{feedback}")
    # The latest owned project facts may include work completed while mail waited.
    latest = SESSIONS.continuation_project_context(conversation_id, user_id=user_id)
    if latest and all(not latest.get(name) or latest[name] == packet.get(name)
                      for name in ("repo_root", "workspace_root")):
        source_findings = packet.get("verification_findings") or []
        packet = {**packet, **latest}
        # A review can arrive on an older card after the newest turn was saved.
        # Carry both observations; choosing which one matters is the harness's job.
        packet["verification_findings"] = list(packet.get("verification_findings") or [])
        for finding in source_findings:
            if finding not in packet["verification_findings"]:
                packet["verification_findings"].append(finding)
    packet = deepcopy(packet)
    packet["continued_from_team_id"] = team_id
    packet["user_feedback_request"] = {
        **request, "message_id": message["id"],
        "source_runtime_version": packet.get("version_identity", {})}
    require_project_access(packet)
    child_state = TEAMS.load(child)
    if child_state:
        project_context.require_owner(child_state, user_id, conversation_id)
        terminal = TEAMS._resume_terminal_result(child_state)
        if terminal and not terminal.get("_resume_receipt_unavailable"):
            out = terminal  # Crash after native return: replay the receipt, not the execution.
        else:
            recovered = _resume_saved_team(
                child, explicit=False, user_id=user_id, conversation_id=conversation_id)
            if "result" not in recovered:
                TEAMS.record_continuation(
                    team_id, request_id, owner=owner, status="held",
                    reason=recovered.get("reason", "child_execution_unsettled"))
                return recovered
            out = recovered["result"]
    else:
        plan, mode = resume_plan_from_state(state, include_completed=True,
                                           feedback=message if request.get("cancel_boundaries") else None)
        tasks = state.get("tasks") or {}
        captain = str(state.get("captain") or "")
        persistent = mode == "orchestrate" and captain not in ("", "unselected")
        retained = retained_resume_results(state, include_completed=True) if persistent else {}
        if retained:
            # Team summaries omit workspace/lane metadata. Recover only those
            # fields from this owner's exact team/node receipt, not the current
            # memory or a harness-wide guess. run_one still validates the cursor.
            source_turn = next((turn for turn in reversed(session.get("turns") or [])
                                if isinstance(turn, dict) and turn.get("team_id") == team_id
                                and turn.get("status") != "running"), {})
            for task_id, previous in retained.items():
                matches = [row for row in source_turn.get("results") or []
                           if isinstance(row, dict)
                           and str(row.get("task_id") or row.get("key") or "") == task_id]
                if len(matches) != 1:
                    continue
                source = matches[0]
                if (not previous.get("native_checkpoint")
                        or str(source.get("assigned_harness") or source.get("harness") or "")
                        != previous.get("assigned_harness")
                        or any(source.get(field) != previous.get(field) for field in (
                            "model_used", "native_checkpoint", "checkpoint_writes",
                            "native_session_scope", "native_context_origin",
                            "checkpoint_storage_error", "native_resume_error"))):
                    continue
                for field in ("workdir", "native_conversation_id"):
                    if field not in previous and isinstance(source.get(field), str):
                        previous[field] = source[field]
        if not persistent:
            if len(tasks) != 1:
                raise ValueError("original_executor_ambiguous")
            task_id, previous = next(iter(tasks.items()))
            harness = str(previous.get("assigned_harness") or "")
            if not harness:
                raise ValueError("original_executor_unavailable")
            node = deepcopy(plan["tasks"].get(task_id) or {})
            node.update(task=question, assigned_harness=harness, assignment_locked=True,
                        depends_on=[], excluded_harnesses=[])
            plan.update(tasks={task_id: node}, captain="", _continuous_coordination=False)
            mode = "relay"
        else:
            plan["_continuous_coordination"] = True
        # Persistent history already travels as per-node retained results.
        # Promoting it to a harness-wide request override would collapse two
        # native cursors' models and outrank a current user-fixed selection.
        overrides = dict(plan.get("model_overrides") or {})
        if not persistent:
            import model_registry
            # Keep the existing single-author fallback, below explicit choices.
            if (not overrides.get(harness)
                    and not model_registry.get_prefs().get(harness)
                    and model_registry.explicit_responses_status(harness) is None):
                model = (previous.get("result") or {}).get("model_used")
                if model:
                    overrides.setdefault(harness, str(model))
        stored_policy = deepcopy((state.get("orchestration_spec") or {}).get("team_policy") or {})
        stored_policy["continuation_parent_team_id"] = team_id
        plan.update(team_id=child, project_context=packet, model_overrides=overrides,
                    team_policy=stored_policy, conversation_context=question)
        require_project_access(packet)  # Grants may be revoked while the request is queued.
        import personal_identity
        personal_identity.bind_plan_attachments(plan, user_id, conversation_id)
        if not TEAMS.record_continuation(
                team_id, request_id, owner=owner, status="running", started_at=time.time()):
            return {"ok": True, "status": "unchanged", "team_id": team_id,
                    "reason": "feedback_not_admitted"}
        SESSIONS.save_turn(
            conversation_id, feedback, {"team_id": child, "status": "running", "results": []},
            mode=mode, user_id=user_id)
        stream_lock = threading.Lock()
        stream_started = False

        def persist_event(event, event_type):
            nonlocal stream_started
            # The dispatcher creates the owned team. An early callback cannot
            # create it, and a failed append must not consume its stream start.
            with stream_lock:
                if not stream_started:
                    started = TEAMS.append_event(
                        child, {"team_id": child, "sid": conversation_id, "selected_mode": mode}, "meta")
                    if started is None:
                        return None
                    stream_started = True
                return TEAMS.append_event(child, event, event_type)

        forwarded_stops = set()

        def forward_stops():
            parent = TEAMS.load(team_id) or {}
            if request.get("cancel_boundaries"):
                # The parent's old in-memory flag is not a Stop for this round.
                # Persisted sequence, not HTTP completion order, decides.
                for stop in _feedback_cancel_requests(parent, message):
                    sequence = stop.get("sequence")
                    if sequence not in forwarded_stops:
                        key = str(stop.get("key") or "")
                        LEGION.request_cancel(child, None if key == "__all__" else key,
                                              stop.get("reason") or "用户停止", scope=stop.get("scope") or "")
                        if TEAMS.request_cancel(
                                child, key="" if key == "__all__" else key,
                                reason=stop.get("reason") or "用户停止", scope=stop.get("scope") or "",
                                source_team_id=team_id, source_sequence=int(sequence or 0)):
                            forwarded_stops.add(sequence)
            elif LEGION.is_cancelled(team_id):
                LEGION.request_cancel(child)

        def progress(event):
            TEAMS.renew_resume(team_id, owner)
            forward_stops()
            if (not isinstance(event, dict) or event.get("t") == "runtime_activity"
                    or LEGION._private_transport_output(event)
                    or ARTIFACTS._is_private_artifact_event(event)):
                return
            persist_event(event, "step")

        try:
            out = LEGION.dispatch_persistent(
                question, plan=plan, cb=progress, qhash=child, mode=mode,
                user_id=user_id, conversation_id=conversation_id,
                **({"_retained_results": retained} if retained else {}))
            out["team_id"] = child
            forward_stops()  # A pre-start Stop may return before any callback.
            persist_event(out, "final")
        except Exception as exc:
            try:
                persist_event({"team_id": child, "error": type(exc).__name__,
                               "phase": "反馈续作异常"}, "error")
            except Exception:
                pass  # Event-store failure must not replace the original error.
            raise
    session = SESSIONS.load_session(conversation_id, user_id=user_id) or {}
    if not any(turn.get("team_id") == child and turn.get("status") != "running"
               for turn in session.get("turns") or []):
        saved_packet = project_context.load(child) or packet
        out["session"] = SESSIONS.save_turn(
            conversation_id, feedback, out, mode=str(saved_packet.get("mode") or "relay"),
            user_id=user_id, source_ref=dict(saved_packet.get("source_ref") or {}),
            input_snapshot=dict(saved_packet.get("input_snapshot") or {}),
            version_identity=dict(saved_packet.get("version_identity") or {}))
    TEAMS.record_continuation(
        team_id, request_id, owner=owner, status="completed", returned_at=time.time(),
        reason="runner_returned_not_quality_acceptance")
    return {"ok": True, "team_id": child, "source_team_id": team_id, "result": out}


def _resume_saved_team(team_id: str, *, explicit: bool, user_id: str,
                       conversation_id: str) -> dict:
    """One admission path for HTTP and startup; execution stays in the existing runners."""
    import project_context
    import personal_identity
    from legion_contracts import (
        resume_facts_from_state, resume_plan_from_state, retained_resume_results)

    def require_project_access(context):
        roots = [context[name] for name in ("repo_root", "workspace_root") if context.get(name)]
        if roots and (any(root != roots[0] for root in roots)
                      or not personal_identity.authorized_project_path(
                          user_id, roots[0], conversation_id=conversation_id)):
            raise PermissionError("project_workspace_not_authorized")

    state = TEAMS.load(team_id) or {}
    project_context.require_owner(state, user_id, conversation_id)
    packet = project_context.load(team_id)
    project_context.require_owner(packet, user_id, conversation_id)
    if any(row.get("conversation_id") and row["conversation_id"] != conversation_id
           for row in (state, packet)):
        raise PermissionError("persisted recovery context belongs to another conversation")
    # Canonical ownership is not permission to access its recorded directory.
    require_project_access(packet)

    def unchanged(reason, recovered=None):
        return {"ok": True, "status": "unchanged", "team_id": team_id,
                "reason": reason, "recovered": recovered or []}

    personal_identity.bind_plan_attachments(
        {**state, "project_context": packet}, user_id, conversation_id)
    facts = resume_facts_from_state(state, explicit=explicit)
    if not facts["eligible"] and facts["reason"] != "no recoverable tasks":
        return unchanged(facts["reason"])
    recovered = TEAMS.recover_expired_attempts(team_id)
    state = TEAMS.load(team_id) or state
    facts = resume_facts_from_state(state, explicit=explicit)
    if not facts["eligible"]:
        return unchanged(facts["reason"], recovered)
    # Each HTTP call owns a distinct claim; a late finally cannot release a
    # subsequent recovery in the same long-lived server process.
    owner = f"server:{os.getpid()}:{threading.get_ident()}:{time.monotonic_ns()}"
    if not TEAMS.claim_resume(team_id, owner, explicit=explicit):
        return unchanged("resume_not_admitted", recovered)
    stream_started = False
    try:
        # Cancellation/new pause/another accepted completion may arrive while
        # claiming. Re-read without consuming a second recovery budget.
        state = TEAMS.load(team_id) or {}
        facts = resume_facts_from_state(state, explicit=explicit, lease_owner=owner)
        if not facts["eligible"]:
            return unchanged(facts["reason"], recovered)
        if facts.get("continuation"):
            return _continue_feedback(
                state, facts["project_context"], facts["continuation"], owner=owner,
                user_id=user_id, conversation_id=conversation_id,
                require_project_access=require_project_access)
        persistent = facts["persistent"]
        retained = retained_resume_results(state, include_completed=persistent)
        if persistent and any(task.get("status") == "done" and key not in retained
                              for key, task in (state.get("tasks") or {}).items()):
            return unchanged("retained_result_unavailable", recovered)
        plan, _ = resume_plan_from_state(state, include_completed=persistent)
        if not plan.get("tasks"):
            return unchanged("no recoverable tasks", recovered)
        packet = facts["project_context"]
        plan["project_context"] = packet
        if persistent:
            plan["_continuous_coordination"] = True
        if not explicit:
            plan.setdefault("resumed_recovery", True)
        question = str(packet.get("objective") or state.get("question") or "")
        runner = LEGION.dispatch_persistent if persistent else LEGION.dispatch

        def progress(_event):
            TEAMS.renew_resume(team_id, owner)
            if (not isinstance(_event, dict) or _event.get("t") == "runtime_activity"
                    or LEGION._private_transport_output(_event)
                    or ARTIFACTS._is_private_artifact_event(_event)):
                return
            from performance_contract import classify_event
            if not classify_event("step", _event)["heartbeat"]:
                TEAMS.append_event(team_id, _event, "step")

        # Existing meta starts a new stream even when every business task is
        # already done. Only admitted recovery reaches this point.
        require_project_access(packet)  # Re-read revocable grants after admission too.
        personal_identity.bind_plan_attachments(plan, user_id, conversation_id)
        TEAMS.append_event(team_id, {"team_id": team_id, "sid": conversation_id}, "meta")
        stream_started = True
        out = runner(question, plan=plan, cb=progress, qhash=team_id,
                     mode=facts["mode"], user_id=user_id, conversation_id=conversation_id,
                     **({"_retained_results": retained} if retained else {}))
        packet = project_context.load(team_id) or packet
        out["source_ref"] = dict(packet.get("source_ref") or {})
        out["version_identity"] = dict(packet.get("version_identity") or {})
        out["execution_role"] = str(packet.get("execution_role") or "champion")
        out["session"] = SESSIONS.save_turn(
            conversation_id, question, out, mode=facts["mode"], user_id=user_id,
            source_ref=dict(packet.get("source_ref") or {}),
            input_snapshot=dict(packet.get("input_snapshot") or {}),
            outcome_contract=dict(plan.get("delivery_contract") or {}),
            version_identity=dict(packet.get("version_identity") or {}))
        TEAMS.append_event(team_id, out, "final")
        return {"ok": True, "team_id": team_id, "recovered": recovered, "result": out}
    except Exception as exc:
        if stream_started:
            try:
                TEAMS.append_event(team_id, {
                    "team_id": team_id, "error": type(exc).__name__,
                    "phase": "恢复执行异常",
                }, "error")
            except Exception:
                pass  # A broken event store must not replace the original failure.
        raise
    finally:
        TEAMS.release_resume(team_id, owner)


def main():
    _strip_dead_proxy_env()
    try:
        recovered = TEAMS.recover_all_expired_attempts()
        if recovered.get("attempts"):
            print(f"[legion] 已恢复 {recovered['attempts']} 个崩溃遗留 attempt 为待领取状态")
        import side_effect_ledger as _effect_ledger
        interrupted_effects = _effect_ledger.recover_expired()
        if interrupted_effects:
            print(f"[legion] 已释放 {len(interrupted_effects)} 个过期副作用 lease")
    except Exception as exc:
        print(f"[legion] 恢复扫描跳过:{type(exc).__name__}")
    # 硬崩溃留证(2026-09-02·24query验收:server 在 PK 收尾阶段无 traceback 硬死·死无对证):
    # faulthandler 让段错误/abort 等原生崩溃把 Python 栈写进 stderr(serve_guard 的 .server.out
    # 会接住)·下次再崩有证据可查根因。零成本·不改变任何行为。
    try:
        import faulthandler
        faulthandler.enable()
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT_DEFAULT)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    # 启动自检 banner【不阻塞】起监听(用户 2026-09-01 loop 揪出:health() 冷调 _aigc_ready,
    # 而 external_agent probe 起子进程冷启动最长 24-45s→旧代码在 serve_forever 前同步调 health()
    # +tool_inventory()→server 从执行到能连端口干等 24s+。命中稳定+速度底线:server 该【秒级可连】,
    # 自检信息交后台预热线程补打)。启动只打地址,立即 serve_forever;banner 由 _prewarm_loop 末尾打印。
    if str(args.host).lower() not in _LOOPBACKS and os.environ.get("LEGION_ALLOW_REMOTE_BIND") != "1":
        raise SystemExit("拒绝非回环监听；如需远程访问请放在带认证的反向代理后"
                         "（内网服务器部署可设 LEGION_ALLOW_REMOTE_BIND=1 显式放行）")
    print(f"[legion] 启动中 http://{args.host}:{args.port} …(自检在后台跑·端口已可连·Ctrl-C 停止)")
    srv = LegionServer((args.host, args.port), Handler)
    srv.api_token = _API_TOKEN

    def _resume_incomplete_teams():
        for state in TEAMS.list_auto_resumable():
            team_id = str(state.get("team_id") or "")
            if not team_id:
                continue
            try:
                if TEAMS.pending_continuation(state):
                    _schedule_feedback_resume(
                        team_id, str(state.get("user_id") or f"recovered:{team_id}"),
                        str(state.get("conversation_id") or team_id))
                    continue
                _resume_saved_team(
                    team_id, explicit=False,
                    user_id=str(state.get("user_id") or f"recovered:{team_id}"),
                    conversation_id=str(state.get("conversation_id") or team_id))
            except Exception as exc:
                print(f"[legion] 团队 {team_id} 自动续跑失败:{type(exc).__name__}:{exc}")
    threading.Thread(target=_resume_incomplete_teams, name="team-auto-resume", daemon=True).start()

    # 启动即后台预热 + 周期性刷新慢缓存(用户 2026-09-01 loop 揪出真根因:一次性预热只救
    # "启动后第一个用户",但 harness_keys/aigc/desktop 的 TTL 只有 30-90s→用户"隔一段时间
    # 继续使用/新开对话/新开页面"时缓存早过期→又撞冷探活付全款 7-8.8s。三层机制根治:
    #   ①【常驻刷新守护】(本段):每 50s 后台 force 重填高频探针(略小于 60s harness TTL·让
    #     缓存大多数时候新鲜);②【SWR】(legion_core._swr_bg_refresh):万一守护没跑到/缓存过期,
    #     读取时先返旧值 0 阻塞+后台异步重填→用户永不撞同步探活。有 SWR 兜底故守护间隔无需卡
    #     TTL 边界疯狂刷(实测 25s 一轮探活占空比 30%·自己成负担·违反稳定底线)·拉到 50s 降到 ~15%。
    #   desktop(TTL 30s 但探活便宜)不进高频组·靠 SWR 兜底(过期后台补·用户不阻塞)即可·不为
    #     30s 一项把整个守护拉到高频。低频组(ext-updates ~48s/capabilities/profiles·TTL 长/变化少)
    #     只首轮预热+每 5min 刷。全在 daemon 线程·不阻塞 serve_forever·失败只记录(纯加速·非功能)。
    _HOT_REFRESH_SEC = 50.0    # 略 < 60s harness TTL·配合 SWR 兜底·占空比 ~15% 不成负担
    _COLD_REFRESH_SEC = 300.0  # 慢且变化少的组·5min 刷一次足够

    def _warm_hot():
        # dispatch 每次派活的前置探针:harness_keys(探活CLI 8.8s)/aigc(~2.9s)。desktop 靠 SWR 兜底不入此组。
        for label, fn in (
            ("aigc", lambda: LEGION._aigc_ready(force=True)),
            ("harnesses", lambda: (LEGION.list_harnesses(force=True), LEGION._harness_keys(force=True),
                                     __import__("harness_view").snapshot("default", force=True))),
            # autonomy_matrix(规划器每次派活拿 cost_class 速度画像·冷 ~11s·内部 probe 外部agent)。
            # 60s TTL 缓存·进热组保持热(用户 2026-09-01 dispatch 体检:它是简单题规划卡 9-11s 真凶)。
            ("autonomy-matrix", lambda: __import__("harness_registry").autonomy_matrix(force=True)),
        ):
            try:
                fn()
            except Exception as exc:
                print(f"[legion] 热缓存刷新 {label} 跳过: {type(exc).__name__}")

    def _warm_cold():
        def _warm_updates():
            import external_agent_registry as _ear
            _ear.check_updates()  # 15 agent 各查 GitHub release·冷启动 ~48s·预热后用户命中缓存
        for label, fn in (
            # 首屏只需常数时间核心目录；完整工具箱由 toolbox 自己的 single-flight 后台补齐。
            # 这里若同步扫全仓，会与第一个 HTTP 请求争 GIL/磁盘，反而把 2ms 快照拖到十几秒。
            ("toolbox", lambda: TOOLBOX.toolbox_summary()),
            # 安装中心冷探 148 项约 2-4s；首屏摘要只读此快照，放在慢 catalog 前先热好。
            ("install-registry", lambda: INSTALL.scan()),
            # 统一目录 catalog(安装中心/harness目录高频入口)冷 ~30s(用户 2026-09-01 延展工具箱思路
            # 揪出:逐个冷测所有GET接口·catalog 30s最慢·比工具箱还狠)——内部聚合8万函数+scan_toolbox。
            # 排 toolbox 后面:toolbox 已预热·catalog 复用其缓存·只剩函数聚合。之前它没进预热·第一个
            # 点安装中心/目录的用户干等30s。有5min缓存·预热后秒回。
            ("catalog", lambda: __import__("catalog_api").catalog()),
            ("capabilities", lambda: LEGION.available_capabilities()),
            ("profiles", lambda: PROFILES.get_profiles()),
            # dispatch 前置 _build_prelude 首次触发 68 个模块懒加载(toolbox/legion_memory/file_hub/
            # ssl/socket…)冷 ~1.4s(用户 2026-09-01 loop 揪出)·dummy 调一次把这些 import 预热掉·
            # 第一个真实派活的用户不再付这 1.4s。纯 import 预热·一次性即可(放冷组)。
            ("prelude-imports", lambda: LEGION._build_prelude("warmup", {}, None, "default")),
            # desktop_status(探 Docker ~2.6s·TTL 30s)首轮预热+5min 刷·30s→过期靠 SWR 兜底不阻塞用户。
            ("desktop-status", lambda: LEGION._desktop_status_txt()),
            # my_workspace 云电脑状态(老用户"我的工作区/进化"面板·cloud_desktop.status 探 docker/noVNC
            # ~1.5s·my_workspace 加了 30s TTL 缓存)。预热它→老用户首次打开工作区就 0.08s(不付首次1.5s)。
            # 2026-09-01 loop 揪出:老用户回来主路径 workspace 面板每次 2.2s。
            ("my-workspace-compute", lambda: __import__("my_workspace").my_compute("default")),
            # ext-updates 最慢(15 agent 各查 GitHub release ~48s)·排最后·不挡前面高频项。
            ("ext-updates", _warm_updates),
        ):
            try:
                fn()
            except Exception as exc:
                print(f"[legion] 冷缓存刷新 {label} 跳过: {type(exc).__name__}")

    def _print_health_banner():
        # 预热后打(缓存已热·health()/tool_inventory() 命中缓存瞬间返回·不再是启动路径上的 24s 阻塞)
        try:
            h = LEGION.health()
            aigc = h.get("aigc", {})
            print(f"[legion] 编排层: {'OK' if h['orchestrator_ok'] else '不可用! '+h['orchestrator_error']}")
            print(f"[legion] 军团成员: {h['harness_count']} 家 · 本机可跑 CLI: {', '.join(aigc.get('runnable_clis', [])) or '无'}")
            print(f"[legion] AIGC 网关 appid: {'已配置('+str(aigc.get('appid_source'))+')' if aigc.get('appid_configured') else '未配置! 各家将无法出答案'}")
            ti = LEGION.tool_inventory().get("summary", {})
            print(f"[legion] 工具: harness CLI {ti.get('harness_ready')}/{ti.get('harness_total')} · 公用工具 {ti.get('tools_ready')}/{ti.get('tools_total')}" +
                  (f" · 缺 {ti.get('missing_harnesses') or ti.get('missing_tools')}" if ti.get('missing_harnesses') or ti.get('missing_tools') else " · 全就绪"))
            if not h.get("ready_to_serve"):
                print("[legion] ⚠️  未就绪:缺 appid 或无可跑 CLI —— 服务会起,但派活可能拿不到答案。")
        except Exception as exc:
            print(f"[legion] 自检 banner 跳过: {type(exc).__name__}")

    def _prewarm_loop():
        _warm_hot()
        _warm_cold()
        _print_health_banner()
        print("[legion] 慢缓存预热完成(harness-keys/aigc/desktop/capabilities/ext-updates/profiles/prelude-imports 已热·常驻刷新已启动)")
        _last_cold = time.time()
        while True:
            time.sleep(_HOT_REFRESH_SEC)
            _warm_hot()
            if time.time() - _last_cold >= _COLD_REFRESH_SEC:
                _warm_cold()
                _last_cold = time.time()
    threading.Thread(target=_prewarm_loop, name="prewarm", daemon=True).start()

    # 周期孤儿清扫(2026-09-02 根修):worker 被单独 SIGKILL(rc-9)时,其名下自立进程组 CLI
    # 由注册表在下一周期收掉(120s·无孤儿时近零成本·sweep 内部 ps 批量一发)。
    def _orphan_sweep_loop():
        import legion_proc_registry as _preg
        # ★启动即扫(OOM 根治收口·2026-09-02):上一实例被 macOS OOM SIGKILL 后·guard 2s 救活
        # 新实例·但名下已注册 CLI 全是孤儿(owner=死pid)。旧代码 sleep(120) 后才首次扫→最坏
        # 122s 僵尸窗口(烧 CPU/占 session 锁/攒内存)。启动先扫一次把窗口压到 ~0·再进周期。
        try:
            _preg.sweep(kill=True)
        except Exception:
            pass
        while True:
            time.sleep(120)
            try:
                _preg.sweep(kill=True)
            except Exception:
                pass
    threading.Thread(target=_orphan_sweep_loop, name="orphan-sweep", daemon=True).start()

    # 无人值守自动更新(用户 2026-09-01 定):每周二/五自动把外部 agent 升到最新并
    # probe 验证,失败自动回滚;LEGION_EXT_AUTO_UPDATE=0 可整体关闭。
    try:
        import external_agent_registry as _ear
        _ear.start_auto_update_scheduler()
        print("[legion] 外部 agent 自动更新调度器已启动(每周二/五·失败自动回滚)")
    except Exception as exc:
        print(f"[legion] 自动更新调度器启动失败: {type(exc).__name__}")

    # cron 真触发调度器(2026-09-03 /loop 手机生活场景伙伴收口):定时任务到点真跑军团
    # (旧实现只登记不触发,每天早报/周期打卡全是空转)。LEGION_CRON_DISPATCH=0 可关。
    try:
        if os.environ.get("LEGION_CRON_DISPATCH", "1") != "0":
            import cron_dispatch
            started = cron_dispatch.start_scheduler()
            print(f"[legion] cron 真触发调度器已启动(poll={started.get('poll_sec')}s·到期任务真跑军团·历史可回溯)")
    except Exception as exc:
        print(f"[legion] cron 调度器启动失败: {type(exc).__name__}")

    # 云电脑任务不再依赖浏览器页面存活；服务常驻消费者负责领取、续租和崩溃恢复。
    try:
        if os.environ.get("LEGION_DESKTOP_WORKER", "1") != "0":
            started = DESKTOP_WORKER.start()
            print(f"[legion] 云电脑持久任务消费者已启动(workers={started.get('workers')}·lease={started.get('lease_s')}s)")
    except Exception as exc:
        print(f"[legion] 云电脑任务消费者启动失败: {type(exc).__name__}: {exc}")

    # 退出兜底清理(子进程泄漏系统性根治·用户 2026-09-01):server 被 Ctrl-C/SIGTERM 时,正在跑的
    # external worker(进程内直调·各自 start_new_session 独立进程组)不会随 server 死→变孤儿(争CPU/
    # 占端口)。退出时遍历本进程所有存活后代·整组 SIGTERM→SIGKILL·一个不留。
    def _reap_children():
        try:
            import subprocess as _sp
            # 拿本进程所有后代 pid(pgrep -P 递归)
            def _descendants(pid):
                out = []
                try:
                    kids = _sp.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=3).stdout.split()
                    for k in kids:
                        out.append(int(k)); out += _descendants(int(k))
                except Exception:
                    pass
                return out
            import signal as _sig
            for cpid in _descendants(os.getpid()):
                try: os.kill(cpid, _sig.SIGTERM)
                except Exception: pass
            time.sleep(0.5)
            for cpid in _descendants(os.getpid()):
                try: os.kill(cpid, _sig.SIGKILL)
                except Exception: pass
        except Exception:
            pass
        # 注册表兜底(2026-09-02 根修):pgrep 后代之外的自立进程组 CLI(SIGKILL 场景
        # atexit 收不到的)也按注册表回收——防 CLI 逃逸成 PPID=1 孤儿。
        try:
            import legion_proc_registry as _preg
            _preg.sweep(kill=True)
        except Exception:
            pass

    # BaseServer.shutdown() 必须从 serve_forever() 之外的线程调用；在信号处理器所在
    # 主线程同步调用会等待自身退出而永久自锁。真正的回收与关 socket 统一在 finally。
    _shutdown_requested = threading.Event()

    def _on_signal(signum, frame):
        if _shutdown_requested.is_set():
            return
        _shutdown_requested.set()
        try:
            import external_agent_registry
            external_agent_registry.stop_probes()
            import skill_factory
            skill_factory.stop_scans()
        except ImportError:
            pass
        print(f"\n[legion] 收到信号 {signum}·请求优雅退出")
        threading.Thread(target=srv.shutdown, name="signal-shutdown", daemon=True).start()
    try:
        import signal as _sig
        _sig.signal(_sig.SIGTERM, _on_signal)
    except Exception:
        pass
    import atexit as _atexit
    _atexit.register(_reap_children)
    # 孤儿回收(2026-09-02 根修):启动即清扫历史任何一代 server/worker 被 SIGKILL(137)
    # 留下的自立进程组 CLI(守护也死了也能收;omnigent process_manager 同款启动扫 orphan)。
    try:
        import legion_proc_registry as _preg
        _sw = _preg.sweep(kill=True)
        _stale = _sw.get("removed_stale") or 0
        _reused = _sw.get("removed_reused") or 0
        if _sw.get("killed") or _stale or _reused:
            print("[legion] 启动清扫残留进程组:", _sw.get("killed"), "stale=", _stale, "reused=", _reused)
    except Exception as _swe:
        print("[legion] 孤儿清扫跳过:", type(_swe).__name__)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[legion] 已停止")
    finally:
        _reap_children()
        srv.server_close()


if __name__ == "__main__":
    main()
