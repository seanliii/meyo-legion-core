"""Resolve existing personal credentials and separately issued password sessions.

No engine imports, account/token issuance here, query-uid trust or anonymous fallback.
Password issuance lives in personal_accounts; it never overwrites the legacy store.
LEGION_PERSONAL_TOKENS_FILE selects an operator-provisioned existing store. The
legacy default is ~/.meyo/agent_web_tokens.json. Never ship it in a release bundle.
"""
from __future__ import annotations

import hashlib
import hmac
from http.cookies import SimpleCookie
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import parse_qs, quote, quote_plus, unquote, unquote_plus, urlsplit

MAX_STORE_BYTES = 1024 * 1024
MAX_STORE_RECORDS = 10000
COOKIE_NAME = 'legion_personal'
_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _origin_parts(value: str, *, allow_root: bool = False) -> tuple[str, str, str]:
    """Parse one origin, never a URL with credentials, path or multiple values."""
    if (not isinstance(value, str) or not value or len(value) > 2048
            or re.search(r"[\x00-\x20\x7f\s\\?#,]", value)):
        raise ValueError("入口必须是无路径和凭据的单一来源")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.netloc
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in (("", "/") if allow_root else ("",))
            or parsed.netloc.endswith(":")):
        raise ValueError("入口必须是无路径和凭据的单一来源")
    host = str(parsed.hostname or "").lower()
    port = parsed.port  # Raises for malformed or out-of-range ports.
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("入口端口无效")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if (parsed.netloc.startswith("[") or len(host) > 253 or not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
                r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*", host)
                or re.fullmatch(r"[0-9.]+", host)):
            raise ValueError("入口主机名无效")
        authority = host
    else:
        if parsed.netloc.startswith("[") and address.version != 6:
            raise ValueError("方括号主机只支持 IPv6 地址")
        host = str(address)
        authority = f"[{host}]" if address.version == 6 else host
    default_port = 443 if parsed.scheme == "https" else 80
    if port is not None and port != default_port:
        authority += f":{port}"
    return parsed.scheme, authority, host


def canonical_public_origin(value: str) -> str:
    """Operator-configured HTTPS origin; no DNS/network lookup or wildcard."""
    scheme, authority, host = _origin_parts(value, allow_root=True)
    if (scheme != "https" or host in _LOCAL_HOSTS
            or host.endswith((".localhost", ".local"))):
        raise ValueError("公网入口必须使用非回环 HTTPS 来源")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            raise ValueError("公网入口需要完整主机名")
    else:
        if not address.is_global:
            raise ValueError("公网入口不能使用私有或保留地址")
    return f"{scheme}://{authority}"


def public_request_origin_allowed(public_origin: str, remote_addr: str,
                                  bound_host: str, host_header: str,
                                  origin_header: str = "") -> bool:
    """Only the local proxy hop for one saved HTTPS origin; identity is separate."""
    try:
        expected = canonical_public_origin(public_origin)
        if remote_addr not in _LOCAL_HOSTS or bound_host not in _LOCAL_HOSTS:
            return False
        _, authority, host = _origin_parts("https://" + host_header)
        if host in _LOCAL_HOSTS:
            # Local health/CLI operations remain possible. Public proxies must
            # reject these Host values instead of forwarding external clients.
            if not origin_header:
                return True
            local = _origin_parts("http://" + host_header)
            return _origin_parts(origin_header)[:2] == local[:2]
        if f"https://{authority}" != expected:
            return False
        if not origin_header:
            return True  # Existing personal authentication is still mandatory.
        scheme, origin_authority, _ = _origin_parts(origin_header)
        return f"{scheme}://{origin_authority}" == expected
    except (ValueError, TypeError):
        return False  # Invalid explicit configuration must not fall back.


def _operator_store_path() -> Path:
    return Path(os.environ.get('LEGION_PERSONAL_TOKENS_FILE') or
                Path.home() / '.meyo/agent_web_tokens.json')


def _operator_records() -> dict:
    """Read the existing provisioned store, never issue tokens or cache grants."""
    path = _operator_store_path()
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_STORE_BYTES or before.st_mode & 0o022:
                return {}
            raw = stream.read(MAX_STORE_BYTES + 1)
            after = os.fstat(stream.fileno())
            if len(raw) > MAX_STORE_BYTES or (before.st_ino, before.st_size, before.st_mtime_ns,
                    before.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                return {}
        records = json.loads(raw)
        if not isinstance(records, dict) or not records or len(records) > MAX_STORE_RECORDS:
            return {}
        return records
    except (OSError, ValueError, TypeError):
        return {}  # Do not keep a stale credential cache after revocation/store loss.


def resolve_token(token: str) -> str | None:
    if not isinstance(token, str) or not token or len(token) > 4096:
        return None
    if token.startswith("pa_"):
        import personal_accounts
        subject = personal_accounts.resolve_token(token)
        if subject:
            return subject
    wanted = hashlib.sha256(token.encode()).digest()
    subject = None
    for candidate, record in _operator_records().items():
        if not isinstance(candidate, str) or not isinstance(record, dict):
            continue
        if hmac.compare_digest(hashlib.sha256(candidate.encode()).digest(), wanted):
            user = record.get('user')
            if isinstance(user, str) and user.strip() == user and 0 < len(user) <= 256 \
                    and not any(ord(c) < 32 for c in user):
                subject = user
    return subject


def _absolute_unlinked_path(value: str, *, directory: bool = True) -> Path:
    """No filesystem access outside explicit paths and no link aliases."""
    if (not isinstance(value, str) or value != value.strip() or not value
            or any(ord(c) < 32 for c in value) or "\\" in value):
        raise ValueError("invalid project path")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path == Path(path.anchor):
        raise ValueError("invalid project path")
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        if stat.S_ISLNK(current.lstat().st_mode):
            raise ValueError("linked project path")
    mode = path.stat().st_mode
    if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
        raise ValueError("invalid project path type")
    return path


def project_grant(user_id: str, root: str) -> dict | None:
    """Return a public directory grant from the static operator token store.

    Authentication must already have resolved user_id. HTTP account settings,
    canonical project packets and task/shell environment are not grant sources.
    The service's operator-selected store must be outside native writable roots;
    this is admission validation, not a new OS sandbox or immutable storage.
    """
    if not isinstance(user_id, str) or not user_id:
        return None
    try:
        wanted = _absolute_unlinked_path(root)
        control = _absolute_unlinked_path(str(_operator_store_path()), directory=False)
        parent_info, info = control.parent.stat(), control.stat()
        if (parent_info.st_mode & 0o022 or info.st_mode & 0o077 or info.st_nlink != 1
                or info.st_uid not in (0, os.getuid()) or parent_info.st_uid not in (0, os.getuid())):
            return None
        # These are the existing native launcher's declared writable state roots.
        # A store below any of them cannot be treated as an independent grant.
        writable = [wanted]
        for name in ("HOME", "CODEX_HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR",
                     "TMPDIR", "LEGION_PROC_REGISTRY_DIR"):
            value = os.environ.get(name)
            if value:
                writable.append(Path(value).resolve())
        if any(control.is_relative_to(path) for path in writable):
            return None
        records = _operator_records()
        # Reject authority files contained by ANY declared project, not only
        # this request's root: another granted task must not edit the grant.
        for record in records.values():
            if not isinstance(record, dict):
                continue
            grants = record.get("project_grants") or []
            if not isinstance(grants, list):
                continue
            for grant in grants:
                candidate = grant.get("root") if isinstance(grant, dict) else None
                if isinstance(candidate, str) and Path(candidate).is_absolute():
                    if control.is_relative_to(Path(candidate).resolve()):
                        return None
        selections = []
        for record in records.values():
            if not isinstance(record, dict) or record.get("user") != user_id:
                continue
            grants = record.get("project_grants") or []
            if not isinstance(grants, list):
                return None
            for grant in grants:
                if not isinstance(grant, dict) or grant.get("root") != str(wanted):
                    continue
                exports = grant.get("export_paths")
                if not isinstance(exports, list) or len(exports) > 5000:
                    return None
                for value in exports:
                    if (not isinstance(value, str) or not value or "\\" in value
                            or any(ord(c) < 32 for c in value)):
                        return None
                    relative = Path(value)
                    if relative.is_absolute() or ".." in relative.parts or value != relative.as_posix():
                        return None
                    if value not in selections:
                        selections.append(value)
        # Empty export_paths still grants working-directory access, not export.
        matching = any(isinstance(record, dict) and record.get("user") == user_id
                       and isinstance(record.get("project_grants"), list)
                       and any(isinstance(g, dict) and g.get("root") == str(wanted)
                               for g in record["project_grants"])
                       for record in records.values())
        if matching:
            return {"source": "operator_project_grant", "user_id": user_id,
                    "root": str(wanted), "export_paths": selections}
    except (OSError, ValueError, TypeError, RuntimeError):
        pass
    return None


def authorized_project_path(user_id: str, root: str, *,
                            conversation_id: str = "") -> dict | None:
    """Operator project, or an exact owned default-workdir allocation/result.

    No root-prefix-only authorization and no canonical packet as allocation
    proof. New directories are bound by the trusted allocator to an owned live
    session/current task. Old workspaces still require their saved native result;
    an unbound historical directory is never claimed merely by exporting it.
    """
    grant = project_grant(user_id, root)
    if grant:
        return grant
    for record in _operator_records().values():
        if isinstance(record, dict) and record.get("user") == user_id:
            declared = record.get("project_grants")
            if isinstance(declared, list) and any(
                    isinstance(g, dict) and g.get("root") == root for g in declared):
                return None  # Invalid explicit grant must not gain a broader fallback.
    try:
        import legion_paths
        wanted = _absolute_unlinked_path(root)
        outputs = Path(legion_paths.under("outputs")).resolve()
        relative = wanted.relative_to(outputs)
        if (not relative.parts or relative.parts[0] == "exports"
                or not user_id or not conversation_id):
            return None
        session = owned_session(conversation_id, user_id)
        if (not session or session.get("user_id") != user_id
                or session.get("sid") != conversation_id):
            return None
        import legion_sandbox
        recorded_allocation = False
        for team_id in {turn.get("team_id") for turn in session.get("turns") or []
                        if isinstance(turn, dict) and isinstance(turn.get("team_id"), str)}:
            for fact in legion_sandbox.workdir_allocation_facts(team_id, user_id, conversation_id):
                if fact["allocation"].get("root") != str(wanted):
                    continue
                recorded_allocation = True
                if fact["current"]:
                    return {"source": "owned_task_default_workdir", "user_id": user_id,
                            "root": str(wanted), "export_paths": ["."]}
        if recorded_allocation:
            # A saved final/checkpoint cannot turn an invalid allocation into
            # legacy authority for unrelated bytes now occupying the same path.
            return None
        # Compatibility is only for old workspaces with no scoped allocation
        # receipt. Registered immutable snapshots have separate file authority.
        for turn in session.get("turns") or []:
            if not isinstance(turn, dict):
                continue
            block = turn.get("result") if isinstance(turn.get("result"), dict) else turn
            rows = block.get("results") or []
            for row in rows:
                checkpoint = row.get("native_checkpoint") if isinstance(row, dict) else None
                if (not isinstance(checkpoint, dict) or not checkpoint.get("session_id")
                        or not checkpoint.get("kind") or row.get("native_resume_error")
                        or row.get("workdir") != str(wanted)):
                    continue
                return {"source": "owned_native_default_workdir", "user_id": user_id,
                        "root": str(wanted), "export_paths": ["."]}
    except (OSError, ValueError, TypeError, RuntimeError):
        pass
    return None


def project_export_path_allowed(grant: dict, relative: str) -> bool:
    """A selection may narrow a granted subtree, never widen it to its parent."""
    if not isinstance(grant, dict) or not isinstance(relative, str):
        return False
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative or "\x00" in relative:
        return False
    return any(path == Path(allowed) or path.is_relative_to(Path(allowed))
               for allowed in grant.get("export_paths", []) if isinstance(allowed, str))


def personal_token(headers, instance_token: str = '') -> str:
    authorization = headers.get_all('Authorization', [])
    if authorization:
        if len(authorization) != 1 or not authorization[0].lower().startswith('bearer '):
            return ''
        value = authorization[0][7:].strip()
    else:
        try:
            cookie = SimpleCookie(); cookie.load(headers.get('Cookie', ''))
            value = cookie[COOKIE_NAME].value if COOKIE_NAME in cookie else ''
        except Exception:
            return ''
    if not value or (instance_token and hmac.compare_digest(value.encode(), instance_token.encode())):
        return ''
    return value


def request_ticket(headers, target: str, payload: dict | None = None) -> str | None:
    """Only the three explicit transport channels; no caller uid authentication."""
    from meyo_ticket_identity import TicketAuthError
    values = list(headers.get_all("X-Legion-Ticket", []))
    query = target.partition("?")[2].partition("#")[0]
    values.extend(parse_qs(query, keep_blank_values=True).get("ticket", []))
    if isinstance(payload, dict) and "ticket" in payload:
        values.append(payload["ticket"])
    if not values:
        return None
    if any(not isinstance(value, str) or not value or value.strip() != value
           or len(value) > 4096 or any(ord(char) < 32 for char in value) for value in values):
        raise TicketAuthError("meyo_ticket_invalid")
    if len(set(values)) != 1:
        raise TicketAuthError("meyo_ticket_conflicting_credentials")
    return values[0]


def ticket_user(ticket: str, headers, instance_token: str = '') -> str:
    """Verified upstream subject enters the same ownership checks as personal auth."""
    from meyo_ticket_identity import TicketAuthError, consume_ticket
    user = consume_ticket(ticket)
    personal = resolve_token(personal_token(headers, instance_token))
    if personal and personal != user:
        raise TicketAuthError("meyo_ticket_personal_identity_conflict")
    return user


def without_ticket_query(target: str) -> str:
    """Remove only ticket transport pairs; preserve all other query bytes."""
    path, separator, query = target.partition("?")
    if not separator:
        return target
    query, fragment_separator, fragment = query.partition("#")
    query = "&".join(part for part in query.split("&")
                     if unquote_plus(part.partition("=")[0]) != "ticket")
    return path + ("?" + query if query else "") + fragment_separator + fragment


def reject_ticket_in_content(value, ticket: str):
    """Do not rewrite task/source facts or forward a credential embedded in them."""
    from meyo_ticket_identity import TicketAuthError
    variants = {ticket, quote(ticket, safe=""), quote_plus(ticket)}

    def contains(item):
        if isinstance(item, str):
            return any(secret in item for secret in variants)
        if isinstance(item, dict):
            return any(contains(key) or contains(child) for key, child in item.items())
        if isinstance(item, list):
            return any(contains(child) for child in item)
        return False

    if contains(value):
        raise TicketAuthError("meyo_ticket_in_request_content", 400)


def owned_session(sid: str, user: str):
    import sessions
    return sessions.load_session(sid, user_id=user) if user else None


def session_grant(sid: str, user: str = '', share: str = '') -> dict | None:
    import sessions
    return owned_session(sid, user) or (sessions.load_shared_session(sid, share) if share else None)


def normalize_resource_aliases(values: dict, route: str = '') -> bool:
    """Resolve identifiers before authorization and dispatch; reject conflicting aliases.

    id has route-specific meaning; never interpret a software/task id as a team.
    qhash, unlike id, is the execution/team identifier throughout this API.
    """
    groups = [('team_id', ('team_id', 'qhash')),
              ('session_id', ('session_id', 'sid', 'conversation_id'))]
    if route in {'/api/legion/team', '/api/legion/workspace'}:
        groups[0] = ('team_id', ('team_id', 'qhash', 'id'))
    for canonical, names in groups:
        selected = []
        for name in names:
            if name not in values:
                continue
            item = values[name]
            if isinstance(item, list):
                if len(item) != 1:
                    return False
                item = item[0]
            if item is not None and not isinstance(item, str):
                return False
            item = (item or '').strip()
            if item:
                selected.append(item)
        if len(set(selected)) > 1:
            return False
        if selected:
            value = selected[0]
            values[canonical] = value
            for name in names:
                if name in values:
                    values[name] = value
            if canonical == 'session_id':
                values['sid'] = value  # Endpoints consuming sid see the same checked identifier.
    for name in ('project_context', 'plan', 'params'):
        nested = values.get(name)
        if isinstance(nested, dict) and not normalize_resource_aliases(nested):
            return False
    return True


def check_request(values: dict, user: str, *, allow_new: bool = False,
                  _conversation_id: str = "") -> bool:
    """Check identity and explicit filesystem grants before request persistence."""
    import sessions
    import project_context
    import team_state
    if not user:
        return False
    def value(name):
        item = values.get(name)
        if isinstance(item, list):
            if len(item) != 1:
                return None
            item = item[0]
        return str(item) if item is not None else ''
    conversation = (value("session_id") or value("sid") or value("conversation_id")
                    or _conversation_id)
    for name in ('uid', 'user_id'):
        if name in values and value(name) not in ('', user):
            return False
    for name in ('sid', 'session_id', 'conversation_id'):
        if name not in values:
            continue
        sid = value(name)
        if sid is None:
            return False
        if not sid:
            continue
        record = sessions._load_session_record(sid)
        if (record and str(record.get('user_id') or 'default') != user) or (not record and not allow_new):
            return False
    for name in ('team_id',):
        if name not in values:
            continue
        tid = value(name)
        if tid is None:
            return False
        if not tid:
            continue
        records = [r for r in (team_state.load(tid), project_context.load(tid)) if r]
        if not records:
            if allow_new:
                continue
            return False
        for record in records:
            if str(record.get('user_id') or '') != user:
                return False
            sid = str(record.get('conversation_id') or '')
            if sid and sessions._load_session_record(sid) and not owned_session(sid, user):
                return False
            if not conversation:
                conversation = sid
    requested = []
    for name in ("repo_root", "workspace_root"):
        root = values.get(name)
        if root in (None, ""):
            continue
        if not isinstance(root, str):
            return False
        grant = authorized_project_path(user, root, conversation_id=conversation)
        if not grant:
            return False
        requested.append(grant["root"])
    if len(set(requested)) > 1:
        return False
    for name in ('project_context', 'plan', 'params'):
        nested = values.get(name)
        if isinstance(nested, dict) and not check_request(
                nested, user, allow_new=allow_new, _conversation_id=conversation):
            return False
    return True


def _local_reference(artifact: dict) -> str:
    """Resolve only the exact published reference; never authorize an entire output tree."""
    path = artifact.get('path')
    if isinstance(path, str) and os.path.isabs(path):
        # A mutable symlink is not a durable ownership statement.
        normalized = os.path.abspath(path)
        return normalized if os.path.realpath(path) == normalized else ''
    url = str(artifact.get('url') or '')
    import artifact_delivery as artifacts
    import legion_paths
    parsed = urlsplit(url)
    if parsed.scheme or parsed.netloc:
        return ''
    roots = {'/home-outputs/': legion_paths.under('outputs'), '/outputs/': artifacts.OUTPUTS,
             '/media/': legion_paths.media_dir(), '/desktop-outputs/': legion_paths.under('users'),
             '/auto-outputs/': artifacts.AUTO_SANDBOX_ROOT}
    for prefix, root in roots.items():
        if parsed.path.startswith(prefix):
            candidate = os.path.realpath(os.path.join(root, unquote(parsed.path[len(prefix):])))
            return candidate if candidate.startswith(os.path.realpath(root) + os.sep) else ''
    return ''


def upload_root(user: str) -> str:
    import legion_paths
    return os.path.join(legion_paths.uploads_dir(), "personal",
                        hashlib.sha256(user.encode()).hexdigest())


def artifact_rows(session: dict):
    for turn in session.get('turns') or []:
        if not isinstance(turn, dict):
            continue
        block = turn.get('result') if isinstance(turn.get('result'), dict) else turn
        yield from (a for a in block.get('artifacts') or [] if isinstance(a, dict))
        for result in block.get('results') or []:
            if isinstance(result, dict):
                yield from (a for a in result.get('artifacts') or [] if isinstance(a, dict))


def related_artifacts(path: str, user: str = '', sid: str = '', share: str = '') -> list[dict]:
    """Read existing companions only from a session that actually grants this file."""
    import sessions
    wanted = os.path.realpath(path)
    if sid:
        granted = session_grant(sid, user, share)
        candidates = [granted] if granted else []
    elif user:
        candidates = (owned_session(row['sid'], user)
                      for row in sessions.list_sessions(limit=1000, user_id=user))
    else:
        return []
    for session in candidates:
        if not session:
            continue
        owner = bool(user and session.get('user_id') == user)
        rows = [a for a in artifact_rows(session) if owner or a.get('visibility') != 'private']
        if any(_local_reference(a) == wanted for a in rows):
            return rows
    return []


def _registered_file_rows(path: str, user: str = '', sid: str = '', share: str = ''):
    """Yield exact-file grants through the existing session/team ownership chain."""
    import sessions
    import team_state
    wanted = os.path.realpath(path)
    if sid:
        granted = session_grant(sid, user, share)
        candidates = [granted] if granted else []
    elif user:
        # Existing index is user-filtered. Old sessions can be addressed explicitly
        # with sid; this fallback is bounded, not a global scan of files or users.
        candidates = (owned_session(row['sid'], user) for row in sessions.list_sessions(limit=1000, user_id=user))
    else:
        return
    for session in candidates:
        if not session:
            continue
        is_owner = bool(user and str(session.get('user_id') or 'default') == user)
        for artifact in artifact_rows(session):
            if (_local_reference(artifact) == wanted
                    and (is_owner or artifact.get("visibility") != "private")):
                yield artifact
        # Live single/dispatch sessions may only have a placeholder turn. Use its
        # owned team events, not directory naming conventions or caller uid.
        for turn in session.get('turns') or []:
            tid = str(turn.get('team_id') or '')
            team = team_state.load(tid) if tid else None
            if (not team or str(team.get('conversation_id') or '') != str(session.get('sid') or '')
                    or str(team.get('user_id') or '') != str(session.get('user_id') or 'default')):
                continue
            # read_events is a paged stream, not the complete ownership record.
            # A late artifact must remain readable without granting its directory.
            # Pin modern live teams to this snapshot so new heartbeats cannot
            # make an unsuccessful lookup chase a continually growing stream.
            upper = team.get('event_seq')
            if not isinstance(upper, int) or isinstance(upper, bool) or upper <= 0:
                upper = max((rec.get('event_id', 0)
                             for rec in team.get('stream_events') or []
                             if isinstance(rec, dict)
                             and type(rec.get('event_id')) is int), default=0)
            if upper <= 0:
                continue
            after = 0
            while after < upper:
                try:
                    page = team_state.read_events(tid, after=after)
                except OSError:
                    break
                if not page:
                    break
                next_after = after
                for rec in page:
                    if not isinstance(rec, dict):
                        continue
                    event_id = rec.get('event_id')
                    if type(event_id) is not int or event_id <= after:
                        continue
                    next_after = max(next_after, event_id)
                    if event_id > upper:
                        continue
                    row = rec.get('data') or {}
                    if (isinstance(row, dict)
                            and row.get('t') in {'artifact_ready', 'artifact_recovered'}
                            and (is_owner or row.get("visibility") != "private")
                            and _local_reference(row) == wanted):
                        yield row
                if next_after <= after:
                    break
                after = next_after


def file_allowed(path: str, user: str = '', sid: str = '', share: str = '') -> bool:
    wanted = os.path.realpath(path)
    if user and wanted.startswith(os.path.realpath(upload_root(user)) + os.sep):
        return True
    return next(_registered_file_rows(path, user, sid, share), None) is not None


def expected_export_hash(path: str, user: str = '', sid: str = '', share: str = '') -> str | None:
    """Expected SHA from owned registered export rows, never from file contents.

    Only /home-outputs/exports/<scope>/<snapshot>/<file> has this contract.
    Missing, malformed, conflicting or unauthorized registration returns None.
    The serving caller must DENY an export path on None, not treat it as a
    legacy file; compare with the actual read-back bytes before preview changes
    or sending headers. This does not claim OS-level immutability.
    """
    import legion_paths

    if not isinstance(path, str) or not os.path.isabs(path):
        return None
    wanted = os.path.abspath(path)
    if os.path.realpath(wanted) != wanted:
        return None
    try:
        relative = Path(wanted).relative_to(
            Path(legion_paths.under("outputs")).resolve() / "exports")
        if (len(relative.parts) < 3
                or not all(re.fullmatch(r"[0-9a-f]{64}", p) for p in relative.parts[:2])):
            return None
        snapshot = relative.parts[1]
        expected = set()
        for row in _registered_file_rows(wanted, user, sid, share):
            if row.get("export_snapshot_id") != snapshot:
                return None
            provenance = row.get("provenance")
            provenance = provenance if isinstance(provenance, dict) else {}
            hashes = [value for value in (row.get("sha256"), provenance.get("sha256"))
                      if value is not None]
            if not hashes or any(not isinstance(value, str)
                                 or not re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes):
                return None
            expected.update(hashes)
            if len(expected) > 1:
                return None
        return next(iter(expected)) if expected else None
    except (OSError, ValueError, TypeError, RuntimeError):
        return None
