#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""成员目录只读快照：把慢探活、模型解析和最近使用从 HTTP 请求线程移开。"""
from __future__ import annotations

import threading
import time
import ipaddress
import json
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

_CACHE_TTL = 60.0
# 快照含 last_used 等用户私有状态，必须按 user_id 分桶；单一全局缓存会把首个用户的历史
# 复用给随后用户。每桶仍保持原有 60s SWR，性能不变。
_CACHE: dict[str, dict] = {}
_GENERATION = 0
_LOCK = threading.RLock()


def _native_entry_config() -> tuple[list, str]:
    """读取部署方的入口事实；端口、源码 URL、探活成功都不能生成用户地址。

    MEYO_NATIVE_ENTRIES_FILE（默认 MEYO_LEGION_DIR/native_entries.json）：
    {"schema_version": 1, "entries": [{"harness": ..., "kind": "web|terminal|device",
      "url": ..., "status": "ready|needs_login|needs_setup|disconnected|configured",
      "authentication": "native|gateway",
      "verification": {"url": ..., "checked_at": ISO8601, "evidence": ...},
      "user_id": ..., "binding": {"session_id": ..., "project_id": ..., "device_id": ...}}]}

    本模块不探活、不产生认证、不创建设备；终端/设备及工程专属记录必须有用户归属。
    此文件由部署/访问验证方维护，不接收模型输出或浏览器写入。
    私有记录还要求调用方传入已认证的 verified_user_id；查询参数 uid 或服务 token
    不代表个人身份，不能据此公开某人的终端、设备或工程地址。
    """
    import legion_paths
    path = Path(os.environ.get("MEYO_NATIVE_ENTRIES_FILE")
                or legion_paths.under("native_entries.json"))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema_version") != 1 \
                or not isinstance(data.get("entries"), list):
            raise ValueError("invalid native entry configuration")
        return data["entries"], ""
    except FileNotFoundError:
        return [], ""
    except (OSError, ValueError, TypeError) as exc:
        # 不暴露路径/配置正文（可能包含凭证），也不让入口配置损坏拖垮成员列表。
        return [], type(exc).__name__


def _native_entry_url(value) -> str:
    """只接受可回访的同站路径或 Web 地址，拒绝服务器本地地址与凭证链接。"""
    if not isinstance(value, str) or not value or any(ord(c) <= 32 for c in value) \
            or "\\" in value:
        return ""
    try:
        parsed = urlsplit(value)
        # 原生登录凭证/邀请应由本人领取，不能随成员目录广播。
        for query in (parsed.query, parsed.fragment.rsplit("?", 1)[-1]):
            for name, _ in parse_qsl(query):
                name = name.lower().replace("_", "").replace("-", "")
                if "token" in name or name in {
                    "key", "apikey", "password", "secret", "authorization", "credential", "code", "ticket",
                }:
                    return ""
        if value.startswith("/") and not value.startswith("//") and not parsed.netloc:
            return value
        if parsed.scheme not in {"http", "https"} or not parsed.hostname \
                or parsed.username is not None or parsed.password is not None:
            return ""
        parsed.port  # 同时核对端口语法和范围。
        host = parsed.hostname.lower().rstrip(".")
        if "%" in host or host == "localhost" or host.endswith(".localhost"):
            return ""
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            try:
                # 浏览器还接受 127.1、整数、八/十六进制 IPv4，不能只挡 127.0.0.1。
                address = ipaddress.ip_address(socket.inet_aton(host))
            except OSError:
                address = None
        if address:
            address = getattr(address, "ipv4_mapped", None) or address
            if address.is_loopback or address.is_unspecified or address.is_link_local \
                    or address.is_multicast:
                return ""
        return value
    except ValueError:
        return ""


def _entry_time(value) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return value if value.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def _public_native_entry(record: dict, user_id: str,
                         verified_user_id: str | None = None) -> dict | None:
    kind = record.get("kind")
    if kind not in {"web", "terminal", "device"}:
        return None
    owner = str(record.get("user_id") or "")
    binding = record.get("binding") if isinstance(record.get("binding"), dict) else {}
    binding = {key: str(binding[key]) for key in ("session_id", "project_id", "device_id")
               if binding.get(key)}
    if owner and (owner != user_id or not isinstance(verified_user_id, str)
                  or owner != verified_user_id):
        return None
    if (kind in {"terminal", "device"} or binding) and not owner:
        return None
    url = _native_entry_url(record.get("url"))
    verification = record.get("verification")
    verification = verification if isinstance(verification, dict) else {}
    checked = _entry_time(verification.get("checked_at"))
    now = datetime.now(timezone.utc)
    authenticated = record.get("authentication") in {"native", "gateway"}
    verified = bool(url and authenticated and verification.get("url") == url
                    and checked and checked <= now + timedelta(minutes=5)
                    and isinstance(verification.get("evidence"), str)
                    and verification["evidence"].strip())
    status = str(record.get("status") or "configured")
    expires = _entry_time(record.get("expires_at"))
    if record.get("expires_at") and (expires is None or expires <= now):
        status = "expired"
    if kind == "device" and not binding.get("device_id"):
        status = "disconnected"
    # 验证证据是访问状态，不是授权；已配置的安全入口允许本人主动打开确认。
    launchable = bool(url and authenticated and status in {
        "configured", "ready", "needs_login", "needs_setup"})
    return {
        "harness": str(record.get("harness") or ""), "kind": kind,
        "label": str(record.get("label") or ""), "status": status,
        "access_verified": verified, "url": url if launchable else "",
        "authentication": record.get("authentication") if authenticated else "unconfirmed",
        "binding": binding, "expires_at": record.get("expires_at") if expires else None,
        "verification": {"url": url, "checked_at": checked.isoformat(),
                         "evidence": verification["evidence"]} if verified else None,
    }


def _with_entry_points(data: dict, user_id: str, *,
                       verified_user_id: str | None = None) -> dict:
    """轻量当前配置叠加，不重跑慢探活，不改共享缓存中的历史证据。"""
    try:
        import runtime_manifest
        definitions = runtime_manifest.load_definitions()
    except Exception:
        definitions = {}
    records, error = _native_entry_config()
    by_harness: dict[str, list] = {}
    for record in records:
        if isinstance(record, dict):
            by_harness.setdefault(str(record.get("harness") or ""), []).append(record)
    pool = set(data.get("answer_pool") or [])
    rows = []
    for source in data.get("harnesses") or []:
        row = dict(source)
        key = str(row.get("key") or "")
        import model_registry
        explicit = model_registry.explicit_responses_status(key)
        if explicit is not None:
            row["model"] = explicit
            row["recommended"] = model_registry.recommended_for(key)
            previous = row.get("readiness") or {}
            row["readiness"] = {
                **previous, "configured": explicit["configured"], "provider_verified": False,
                "historical_verified": previous.get("historical_verified", previous.get("verified", False)),
                "historical_experienced": previous.get("historical_experienced", previous.get("experienced", False)),
                "verified": False, "experienced": False,
            }
        elif (key == "codex" and isinstance(row.get("model"), dict)
              and row["model"].get("source") == "operator_binding"):
            # A mode change can outlive this cache entry. Re-project only its
            # volatile model fields, retaining the installation/history cache.
            row["model"] = model_registry.resolve_model(key)
            row["recommended"] = model_registry.recommended_for(key)
        specs = (definitions.get(key) or {}).get("specs") or {}
        entries = {"execution": {
            "harness": key, "selectable": key in pool,
            "integration": str(specs.get("integration") or ""),
            "resume": str(specs.get("resume") or ""),
        }, "web": [], "terminal": [], "device": []}
        for record in by_harness.get(key, []):
            entry = _public_native_entry(record, user_id, verified_user_id)
            if entry:
                entries[entry["kind"]].append(entry)
        row["entry_points"] = entries
        rows.append(row)
    identity_verified = (isinstance(verified_user_id, str) and bool(verified_user_id)
                         and verified_user_id == user_id)
    return {**data, "harnesses": rows, "entry_configuration_error": error,
            "entry_identity_verified": identity_verified,
            # “已验证”不能区分两位都已登录的人；前端返回状态须绑定同一可信主体。
            "entry_identity_user_id": verified_user_id if identity_verified else None}


def _cache_for(user_id: str) -> dict:
    with _LOCK:
        return _CACHE.setdefault(user_id, {"data": None, "ts": 0.0, "refreshing": False})


def invalidate(user_id: str = "") -> None:
    global _GENERATION
    with _LOCK:
        _GENERATION += 1
        if user_id:
            _CACHE.pop(str(user_id), None)
        else:
            _CACHE.clear()


def update_model(key: str, model: dict) -> None:
    """模型偏好写入后原位更新所有用户快照，不重跑慢探活。"""
    global _GENERATION
    with _LOCK:
        _GENERATION += 1  # 已在跑的旧模型快照不得回写覆盖本次偏好
        for state in _CACHE.values():
            state["refreshing"] = False
            data = state.get("data") or {}
            for harness in data.get("harnesses") or []:
                if harness.get("key") == key:
                    harness["model"] = dict(model or {})


def _profile_snapshot() -> dict:
    try:
        import runtime_manifest
        definitions = runtime_manifest.load_definitions()
        return {key: dict(row.get("profile") or {}) for key, row in definitions.items()}
    except Exception:
        return {}


def _performance_snapshot() -> dict:
    try:
        import harness_performance
        rows = harness_performance.table().get("rows") or []
    except Exception:
        rows = []
    by_harness = {}
    for row in rows:
        by_harness.setdefault(row.get("harness"), []).append(row)
    return by_harness


def _last_used_snapshot(user_id: str) -> dict:
    try:
        import performance_views
        index = performance_views.session_index(user_id, limit=20, include_artifacts=False)
        out = {}
        import sessions
        deadline = time.perf_counter() + 0.12
        for summary in index.get("sessions") or []:
            if time.perf_counter() >= deadline:
                break
            detail = sessions.load_session(summary.get("sid") or "", user_id=user_id) or {}
            for turn in reversed(detail.get("turns") or []):
                for result in turn.get("results") or []:
                    key = str(result.get("key") or result.get("harness") or "")
                    if key and key not in out:
                        out[key] = {"ts": float(turn.get("ts") or summary.get("updated") or 0),
                                    "mode": str(turn.get("mode") or ""),
                                    "question": str(turn.get("question") or "")[:40]}
        return out
    except Exception:
        return {}


def _build(user_id: str) -> dict:
    import legion_core
    import model_registry

    harnesses = legion_core.list_harnesses()
    # 答题池全量名单(用户 2026-09-07:成员自选不能只看到写死的 8 家):
    # _harness_keys 是真正的派单答题池(能力/平台面家已被 _answer_keys_from_catalog 滤掉),
    # 前端小队/接力选择器以此为准,选到的每家后端 elect_captain 都真能用。
    try:
        answer_pool = sorted(legion_core._harness_keys())
    except Exception:
        answer_pool = []
    profiles = _profile_snapshot()
    performance = _performance_snapshot()
    last_used = _last_used_snapshot(user_id)
    for harness in harnesses:
        key = harness.get("key") or ""
        harness["profile"] = profiles.get(key)
        harness["model"] = model_registry.resolve_model(key)
        harness["recommended"] = model_registry.recommended_for(key)
        rows = performance.get(key) or []
        good = sum(int(row.get("good", 0)) for row in rows)
        bad = sum(int(row.get("bad", 0)) for row in rows)
        # category(2026-09-13·§1 契约):目录分组·内置主力/外部接入/角色类。
        # 判定:运营推荐位(主力)·模型带 gateway/外部源(外部)·其余(内置)。
        _model = harness.get("model") or {}
        harness["category"] = (
            "role" if str(harness.get("role") or "") else
            "external" if "gateway" in str(_model.get("source") or "") or _model.get("external") else
            "builtin")
        # win_count:PK 胜出回流(成功交付次数·与 readiness.good 同源·前端精选排序第二权重)
        harness["win_count"] = good
        lower_bound = max((float(row.get("success_lb") or 0) for row in rows), default=0.0)
        harness["readiness"] = {
            "declared": bool(harness.get("profile")),
            "connected": bool(harness.get("available", True)),
            "verified": good > 0,
            "experienced": good + bad >= 3 and lower_bound > 0 and good > 0,
            "samples": good + bad,
            "success_lb": round(lower_bound, 3),
            "p95_s": next((row.get("p95_s") for row in rows if row.get("p95_s")), None),
            "last_used": last_used.get(key),
        }
    return {"harnesses": harnesses, "answer_pool": answer_pool, "updated_at": time.time(), "warming": False}


def _refresh(user_id: str, generation: int) -> None:
    try:
        data = _build(user_id)
        with _LOCK:
            if generation == _GENERATION:
                _cache_for(user_id).update({"data": data, "ts": time.time()})
    finally:
        with _LOCK:
            if generation == _GENERATION:
                _cache_for(user_id)["refreshing"] = False


def _schedule(user_id: str) -> None:
    with _LOCK:
        state = _cache_for(user_id)
        if state["refreshing"]:
            return
        state["refreshing"] = True
        generation = _GENERATION
    threading.Thread(target=_refresh, args=(user_id, generation), daemon=True,
                     name=f"harness-view-refresh-{user_id[:24]}").start()


def public_snapshot() -> dict:
    """Public catalog only: no user caches, histories, models, probes or private URLs."""
    rows = [{"key": key, "display_name": profile.get("name") or key,
             "profile": profile, "available": None, "model": None, "recommended": [],
             "entry_points": [],
             "readiness": {"declared": True, "connected": False, "verified": False,
                           "experienced": False, "samples": 0, "success_lb": 0,
                           "p95_s": None, "last_used": None}}
            for key, profile in _profile_snapshot().items()]
    return {"harnesses": rows, "answer_pool": [], "updated_at": 0, "warming": False,
            "public_catalog_only": True}


def snapshot(user_id: str = "default", force: bool = False, *,
             verified_user_id: str | None = None) -> dict:
    user_id = str(user_id or "default")
    with _LOCK:
        state = _cache_for(user_id)
        cached = state["data"]
        fresh = cached is not None and time.time() - state["ts"] < _CACHE_TTL
    if not force and fresh:
        return _with_entry_points({**cached, "from_cache": True}, user_id,
                                  verified_user_id=verified_user_id)
    if not force:
        _schedule(user_id)
        if cached is not None:
            return _with_entry_points({**cached, "from_cache": True, "stale": True}, user_id,
                                      verified_user_id=verified_user_id)
        # 首次也不让用户等探活；manifest 画像先渲染，后台补真实连接与模型状态。
        profiles = _profile_snapshot()
        try:
            import model_registry
            models = {key: model_registry.resolve_model(key) for key in profiles}
        except Exception:
            models = {}
        rows = [{"key": key, "display_name": profile.get("name") or key,
                 "profile": profile, "available": None, "model": models.get(key),
                 "readiness": {"declared": True, "connected": False, "verified": False,
                               "experienced": False, "samples": 0, "success_lb": 0,
                               "p95_s": None, "last_used": None}}
                for key, profile in profiles.items()]
        try:
            import legion_core
            warm_pool = sorted(legion_core._harness_keys())
        except Exception:
            warm_pool = []
        return _with_entry_points(
            {"harnesses": rows, "answer_pool": warm_pool, "updated_at": 0,
             "warming": True, "from_cache": False}, user_id,
            verified_user_id=verified_user_id)
    data = _build(user_id)
    with _LOCK:
        _cache_for(user_id).update({"data": data, "ts": time.time()})
    return _with_entry_points(data, user_id, verified_user_id=verified_user_id)
