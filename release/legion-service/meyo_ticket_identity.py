"""Request identity from an explicitly trusted Meyo consume endpoint.

No user/token issuance, developer uid fallback, engine import or persistent cache.
The 240s absolute cache window matches the supplied ticket adapter; it is not
renewed by access. Single-flight/caching are process-local, not distributed SSO.
"""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future, TimeoutError as FutureTimeout
import hashlib
import http.client
import json
import os
import ssl
import threading
import time
from urllib.parse import urlsplit, urlunsplit

CONSUME_PATH = "/api/v1/meyo-agent/internal/legion-ticket/consume"
CACHE_TTL_SECONDS = 240.0
UPSTREAM_TIMEOUT_SECONDS = 5.0
MAX_CACHE_ENTRIES = 256
MAX_INFLIGHT = 32
MAX_RESPONSE_BYTES = 65536
_cache_clock = time.monotonic
_lock = threading.Lock()
_cache: OrderedDict[tuple[str, bytes], tuple[float, str]] = OrderedDict()
_inflight: dict[tuple[str, bytes], Future] = {}


class TicketAuthError(Exception):
    """Public, credential-free error; never expose upstream messages or bodies."""
    def __init__(self, code: str, status: int = 401):
        self.code, self.status = code, status
        super().__init__(code)


def configured_endpoint() -> str:
    base = str(os.environ.get("MARS_SERVER_BASE") or "").strip()
    if not base:
        raise TicketAuthError("meyo_ticket_not_configured", 503)
    try:
        parsed = urlsplit(base)
        port = parsed.port
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment
                or "\\" in base or any(ord(char) < 33 for char in base)
                or any(part in (".", "..") for part in parsed.path.split("/"))):
            raise ValueError("invalid configured origin")
        if port is not None and not 0 < port < 65536:
            raise ValueError("invalid port")
        return urlunsplit((parsed.scheme, parsed.netloc,
                           parsed.path.rstrip("/") + CONSUME_PATH, "", ""))
    except ValueError:
        raise TicketAuthError("meyo_ticket_invalid_server_config", 503) from None


def _subject(value) -> str:
    if type(value) is int and value > 0:
        value = str(value)
    if (not isinstance(value, str) or not value or value.strip() != value
            or len(value) > 256 or any(ord(char) < 32 for char in value)):
        raise TicketAuthError("meyo_ticket_invalid_response", 502)
    return value


def _consume(endpoint: str, ticket: str) -> str:
    """Direct configured-origin HTTP; redirects and ambient proxies are not used."""
    target = urlsplit(endpoint)
    deadline = time.monotonic() + UPSTREAM_TIMEOUT_SECONDS

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError()
        return value

    cls = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
    kwargs = {"timeout": remaining()}
    if target.scheme == "https":
        kwargs["context"] = ssl.create_default_context()
    connection = cls(target.hostname, target.port, **kwargs)
    try:
        connection.connect()
        sock = connection.sock
        sock.settimeout(remaining())
        connection.request("POST", target.path,
                           body=json.dumps({"ticket": ticket}).encode(),
                           headers={"Content-Type": "application/json", "Accept": "application/json"})
        sock.settimeout(remaining())
        response = connection.getresponse()
        # http.client never follows a redirect, including a same-host redirect.
        if not 200 <= response.status < 300:
            raise TicketAuthError("meyo_ticket_rejected")
        raw = bytearray()
        while not response.isclosed():
            sock.settimeout(remaining())
            chunk = response.read1(min(8192, MAX_RESPONSE_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise TicketAuthError("meyo_ticket_invalid_response", 502)
        value = json.loads(raw)
        # Main-site ApiResponse.success uses HTTP status 200 as its business
        # code. Keep the existing zero-code producer; never accept arbitrary
        # truthy/2xx values or let a success code override failure flags/UID.
        if (not isinstance(value, dict) or type(value.get("code")) is not int
                or value["code"] not in (0, 200) or value.get("error")
                or value.get("success") is False or value.get("ok") is False):
            raise TicketAuthError("meyo_ticket_rejected")
        data = value.get("data")
        if not isinstance(data, dict):
            raise TicketAuthError("meyo_ticket_invalid_response", 502)
        user = _subject(data.get("uid"))
        if ticket in user:
            raise TicketAuthError("meyo_ticket_invalid_response", 502)
        return user
    except TicketAuthError:
        raise
    except (ValueError, TypeError, UnicodeError):
        raise TicketAuthError("meyo_ticket_invalid_response", 502) from None
    except (OSError, http.client.HTTPException):
        raise TicketAuthError("meyo_ticket_upstream_unavailable", 503) from None
    finally:
        connection.close()


def consume_ticket(ticket: str) -> str:
    if (not isinstance(ticket, str) or not ticket or len(ticket) > 4096
            or ticket.strip() != ticket or any(ord(char) < 32 for char in ticket)):
        raise TicketAuthError("meyo_ticket_invalid")
    endpoint = configured_endpoint()  # Before cache: disabling/changing trust takes effect immediately.
    try:
        key = endpoint, hashlib.sha256(ticket.encode()).digest()
    except UnicodeError:
        raise TicketAuthError("meyo_ticket_invalid") from None
    started = _cache_clock()
    with _lock:
        for expired in [name for name, (until, _) in _cache.items() if until <= started]:
            del _cache[expired]
        if key in _cache:
            return _cache[key][1]
        future = _inflight.get(key)
        owner = future is None
        if owner:
            if len(_inflight) >= MAX_INFLIGHT:
                raise TicketAuthError("meyo_ticket_verifier_busy", 503)
            future = Future()
            _inflight[key] = future
    if not owner:
        try:
            return future.result(timeout=UPSTREAM_TIMEOUT_SECONDS + 1)
        except FutureTimeout:
            raise TicketAuthError("meyo_ticket_upstream_unavailable", 503) from None
    try:
        user = _consume(endpoint, ticket)
    except Exception as error:
        failure = error if isinstance(error, TicketAuthError) else TicketAuthError(
            "meyo_ticket_upstream_unavailable", 503)
        with _lock:
            _inflight.pop(key, None)
            future.set_exception(failure)
        raise failure from None
    with _lock:
        until = started + CACHE_TTL_SECONDS
        if until > _cache_clock():
            _cache[key] = until, user
            while len(_cache) > MAX_CACHE_ENTRIES:
                _cache.popitem(last=False)
        _inflight.pop(key, None)
        future.set_result(user)
    return user


def _reset_for_tests():
    with _lock:
        assert not _inflight, "an authentication flight must finish before fixture teardown"
        _cache.clear()
