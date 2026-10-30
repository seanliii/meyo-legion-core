"""Local password accounts and private BYOK settings, not an execution sandbox.

The existing owner and legacy credentials remain unchanged. Registered subjects
have a separate namespace and cannot execute shared-host tools. Saving a model
profile is not activating it, verifying it, or allowing use of the owner's key.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import ipaddress
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
import time
from urllib.parse import urlsplit

SESSION_TTL = 7 * 24 * 3600
USER_ATTEMPT_LIMIT = 8
_WINDOW = 15 * 60
_LOCK = threading.RLock()
_HASH_SLOTS = threading.BoundedSemaphore(2)
_PREFIX = "acct_"
_NOTE = "独立执行环境尚未接通；当前仅可管理账号和自己的模型 Key，不会调用站长额度。"
_PROVIDERS = {
    "deepseek": ("api.deepseek.com",),
    "moonshot": ("api.moonshot.cn", "api.moonshot.ai"),
    "openrouter": ("openrouter.ai",),
    "custom": (),
}


class AccountError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def enabled():
    return bool(os.environ.get("LEGION_PERSONAL_ACCOUNTS_DIR")
                and os.environ.get("LEGION_PERSONAL_OWNER"))


@contextmanager
def _store(create=True):
    """A private SQLite transaction; no implicit ~/.meyo fallback or key cache."""
    if not enabled():
        raise AccountError("account_settings_unavailable", 503)
    conn = None
    try:
        with _LOCK:
            root = Path(os.environ["LEGION_PERSONAL_ACCOUNTS_DIR"])
            if not root.is_absolute():
                raise AccountError("account_store_unavailable", 503)
            if create:
                root.mkdir(mode=0o700, exist_ok=True)
            elif not root.exists() and not root.is_symlink():
                yield None
                return
            info = root.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077
                    or info.st_uid != os.getuid()):
                raise AccountError("account_store_unavailable", 503)
            path = root / "accounts.sqlite3"
            if not create and not path.exists() and not path.is_symlink():
                yield None
                return
            flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            fd = os.open(path, flags | (os.O_CREAT if create else 0), 0o600)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                        or info.st_uid != os.getuid() or info.st_nlink != 1):
                    raise AccountError("account_store_unavailable", 503)
            finally:
                os.close(fd)
            conn = sqlite3.connect(str(path), timeout=3)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA secure_delete=ON")
            if create:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS accounts (
                      user_id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                      salt BLOB NOT NULL, password_hash BLOB NOT NULL);
                    CREATE TABLE IF NOT EXISTS sessions (
                      digest TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires REAL NOT NULL);
                    CREATE TABLE IF NOT EXISTS profiles (
                      user_id TEXT NOT NULL, id TEXT NOT NULL, provider TEXT NOT NULL,
                      model TEXT NOT NULL, base_url TEXT NOT NULL, api_key TEXT NOT NULL,
                      PRIMARY KEY (user_id, id));
                    CREATE TABLE IF NOT EXISTS limits (
                      name TEXT PRIMARY KEY, started REAL NOT NULL, count INTEGER NOT NULL);
                """)
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
    except AccountError:
        raise
    except (OSError, sqlite3.Error, RuntimeError):
        raise AccountError("account_store_unavailable", 503) from None
    finally:
        if conn is not None:
            conn.close()


def _username(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,47}", value):
        raise AccountError("invalid_username")
    return value.lower()


def _password(value):
    if (not isinstance(value, str) or not 6 <= len(value) <= 128
            or not value.strip() or any(ord(c) < 32 for c in value)):
        raise AccountError("invalid_password")
    return value


def _login_username(value):
    owner = os.environ.get("LEGION_PERSONAL_OWNER", "")
    if isinstance(value, str) and owner and value.lower() == owner.lower():
        return owner.lower()  # Preserve the original 1–64 character owner namespace.
    return _username(value)


def settings_only(user):
    return user.startswith(_PREFIX) and user != os.environ.get("LEGION_PERSONAL_OWNER")


def _derive(password, salt):
    if not _HASH_SLOTS.acquire(blocking=False):
        raise AccountError("account_service_busy", 429)
    try:
        # 32 MiB, p=3; work is bounded and never done while holding a DB lock.
        return hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=3,
                              maxmem=64 * 1024 * 1024, dklen=32)
    finally:
        _HASH_SLOTS.release()


def _attempt(username, *, register=False):
    now = time.time()
    name = hashlib.sha256(username.encode()).hexdigest()
    buckets = [("user:" + name, USER_ATTEMPT_LIMIT)]
    if register:
        buckets.append(("all-register", 20))
    # Only completed failures count against a user. The bounded hash semaphore
    # limits global work without an anonymous request locking all logins for 15m.
    with _store() as db:
        db.execute("DELETE FROM limits WHERE started < ?", (now - _WINDOW,))
        for bucket, limit in buckets:
            row = db.execute("SELECT count FROM limits WHERE name=?", (bucket,)).fetchone()
            if row is not None and row["count"] >= limit:
                raise AccountError("too_many_attempts", 429)


def _failure(username):
    with _store() as db:
        bucket = "user:" + hashlib.sha256(username.encode()).hexdigest()
        db.execute("INSERT INTO limits VALUES (?, ?, 1) ON CONFLICT(name) "
                   "DO UPDATE SET count=count+1", (bucket, time.time()))


def _success(db, username):
    db.execute("DELETE FROM limits WHERE name=?",
               ("user:" + hashlib.sha256(username.encode()).hexdigest(),))


def _issue(db, user):
    now = time.time()
    db.execute("DELETE FROM sessions WHERE expires <= ?", (now,))
    # Keep a small number of browsers, without unlimited token accumulation.
    rows = db.execute("SELECT digest FROM sessions WHERE user_id=? ORDER BY expires DESC",
                      (user,)).fetchall()
    for row in rows[7:]:
        db.execute("DELETE FROM sessions WHERE digest=?", (row["digest"],))
    token = "pa_" + secrets.token_urlsafe(32)
    db.execute("INSERT INTO sessions VALUES (?, ?, ?)",
               (hashlib.sha256(token.encode()).hexdigest(), user, now + SESSION_TTL))
    return token


def _info(user, username, password_set):
    return {"authenticated": True, "user_id": user, "username": username,
            "password_set": password_set, "execution_enabled": not settings_only(user),
            "execution_note": _NOTE if settings_only(user) else "",
            "account_settings_available": enabled(), "sso_verified": False}


def account_info(user):
    row = None
    if enabled():
        with _store(False) as db:
            if db is not None:
                row = db.execute("SELECT username FROM accounts WHERE user_id=?", (user,)).fetchone()
    return _info(user, row["username"] if row else user, bool(row))


def register(username, password):
    username, password = _username(username), _password(password)
    if (username == os.environ.get("LEGION_PERSONAL_OWNER", "").lower()
            or username in {"admin", "root", "system", "support"}
            or username.startswith(_PREFIX)):
        raise AccountError("username_unavailable", 409)
    _attempt(username, register=True)
    salt = secrets.token_bytes(16)
    derived = _derive(password, salt)
    user = _PREFIX + secrets.token_hex(16)
    with _store() as db:
        if db.execute("SELECT 1 FROM accounts WHERE username=?", (username,)).fetchone():
            raise AccountError("username_unavailable", 409)
        if db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] >= 10000:
            raise AccountError("registration_full", 503)
        quota = db.execute("SELECT count FROM limits WHERE name='all-register'").fetchone()
        if quota and quota["count"] >= 20:
            raise AccountError("too_many_attempts", 429)
        db.execute("INSERT INTO limits VALUES ('all-register', ?, 1) ON CONFLICT(name) "
                   "DO UPDATE SET count=count+1", (time.time(),))
        db.execute("INSERT INTO accounts VALUES (?, ?, ?, ?)", (user, username, salt, derived))
        _success(db, username)
        token = _issue(db, user)
    return _info(user, username, True), token


def login(username, password):
    try:
        username, password = _login_username(username), _password(password)
    except AccountError:
        raise AccountError("invalid_credentials", 401) from None
    _attempt(username)
    with _store(False) as db:
        row = db.execute("SELECT * FROM accounts WHERE username=?", (username,)).fetchone()
    derived = _derive(password, row["salt"] if row else bytes(16))
    if not row or not hmac.compare_digest(derived, row["password_hash"]):
        _failure(username)
        raise AccountError("invalid_credentials", 401)
    with _store() as db:
        # A password change during hashing cannot issue a stale session.
        current = db.execute("SELECT password_hash FROM accounts WHERE user_id=?",
                             (row["user_id"],)).fetchone()
        if not current or not hmac.compare_digest(derived, current["password_hash"]):
            raise AccountError("invalid_credentials", 401)
        _success(db, username)
        token = _issue(db, row["user_id"])
    return _info(row["user_id"], username, True), token


def resolve_token(token):
    if not enabled() or not isinstance(token, str) or not re.fullmatch(r"pa_[A-Za-z0-9_-]{43}", token):
        return None
    try:
        with _store(False) as db:
            if db is None:
                return None
            row = db.execute("SELECT user_id FROM sessions WHERE digest=? AND expires>?",
                             (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
            return row["user_id"] if row else None
    except AccountError:
        return None


def logout(token):
    if enabled() and isinstance(token, str) and token.startswith("pa_"):
        with _store(False) as db:
            if db is not None:
                db.execute("DELETE FROM sessions WHERE digest=?",
                           (hashlib.sha256(token.encode()).hexdigest(),))


def set_password(user, password, current_password=""):
    password = _password(password)
    _attempt(user)
    with _store(False) as db:
        row = db.execute("SELECT * FROM accounts WHERE user_id=?", (user,)).fetchone()
    if row:
        try:
            current = _password(current_password)
        except AccountError:
            raise AccountError("current_password_required", 403) from None
        if not hmac.compare_digest(_derive(current, row["salt"]), row["password_hash"]):
            _failure(user)
            raise AccountError("invalid_credentials", 403)
        username = row["username"]
    elif user == os.environ.get("LEGION_PERSONAL_OWNER"):
        username = _login_username(user)
    else:
        raise AccountError("account_not_found", 403)
    salt = secrets.token_bytes(16)
    derived = _derive(password, salt)
    with _store() as db:
        current = db.execute("SELECT password_hash FROM accounts WHERE user_id=?", (user,)).fetchone()
        if (bool(current) != bool(row)
                or (row and not hmac.compare_digest(current["password_hash"], row["password_hash"]))):
            raise AccountError("account_changed_retry", 409)
        db.execute("INSERT INTO accounts VALUES (?, ?, ?, ?) ON CONFLICT(user_id) "
                   "DO UPDATE SET salt=excluded.salt, password_hash=excluded.password_hash",
                   (user, username, salt, derived))
        db.execute("DELETE FROM sessions WHERE user_id=?", (user,))
        _success(db, user)
        _success(db, username)
        token = _issue(db, user)
    return _info(user, username, True), token


def _profile_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9_-]{1,48}", value):
        raise AccountError("invalid_profile")
    return value


def _profile_metadata(row):
    return {key: row[key] for key in ("id", "provider", "model", "base_url")} | {
        "key_configured": True, "provider_verified": False,
    }


def models(user):
    with _store(False) as db:
        rows = db.execute("SELECT id,provider,model,base_url FROM profiles WHERE user_id=? ORDER BY id",
                          (user,)).fetchall() if db is not None else []
    return {"profiles": [_profile_metadata(row) for row in rows], "execution_enabled": False,
            "note": "仅保存个人配置，未验证或启用；不会替换当前生效的模型配置。"}


def save_model(user, values):
    profile_id = _profile_id(values.get("id"))
    provider, model, endpoint = (values.get(key) for key in ("provider", "model", "base_url"))
    key = values.get("api_key")
    if (not isinstance(provider, str) or provider not in _PROVIDERS
            or not isinstance(model, str) or not 1 <= len(model) <= 160
            or model.strip() != model or any(ord(c) < 32 for c in model)
            or not isinstance(endpoint, str) or len(endpoint) > 512
            or not isinstance(key, str) or not 1 <= len(key) <= 8192
            or any(c.isspace() or ord(c) < 32 for c in key)):
        raise AccountError("invalid_model_profile")
    try:
        parts = urlsplit(endpoint)
        host = parts.hostname or ""
        if (parts.scheme != "https" or not host or parts.username is not None
                or parts.password is not None or parts.query or parts.fragment
                or "\\" in endpoint or any(c.isspace() or ord(c) < 32 for c in endpoint)
                or parts.port not in (None, 443)
                or host.endswith((".local", ".localhost")) or "." not in host):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError
        if provider != "custom" and host not in _PROVIDERS[provider]:
            raise ValueError
    except ValueError:
        raise AccountError("invalid_model_endpoint") from None
    with _store() as db:
        count = db.execute("SELECT COUNT(*) FROM profiles WHERE user_id=?", (user,)).fetchone()[0]
        exists = db.execute("SELECT 1 FROM profiles WHERE user_id=? AND id=?", (user, profile_id)).fetchone()
        if count >= 16 and not exists:
            raise AccountError("too_many_profiles")
        db.execute("INSERT INTO profiles VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(user_id,id) "
                   "DO UPDATE SET provider=excluded.provider,model=excluded.model,"
                   "base_url=excluded.base_url,api_key=excluded.api_key",
                   (user, profile_id, provider, model, endpoint, key))
    return _profile_metadata({"id": profile_id, "provider": provider, "model": model, "base_url": endpoint})


def delete_model(user, profile_id):
    profile_id = _profile_id(profile_id)
    with _store(False) as db:
        if db is not None:
            db.execute("DELETE FROM profiles WHERE user_id=? AND id=?", (user, profile_id))
    return {"ok": True}


def request_allowed(user, method, path):
    """New accounts are settings-only until OS-level execution isolation exists.

    Use an allowlist, not a dispatch-only block: legacy tool, native UI, install,
    recovery, gateway probes and device routes otherwise reach shared credentials.
    """
    if not settings_only(user):
        return True
    if method == "GET":
        return path in {
            "/", "/index.html", "/favicon.ico", "/desktop_production.js", "/desktop_production.css",
            "/api/legion/auth/status", "/api/legion/my/models", "/api/sessions",
            "/api/legion/session", "/api/legion/harnesses",
        }
    return method == "POST" and path in {
        "/api/legion/auth/login", "/api/legion/auth/register", "/api/legion/auth/logout",
        "/api/legion/auth/password", "/api/legion/auth/session",
        "/api/legion/my/models", "/api/legion/my/models/delete",
    }
