"""Read-only queued feedback for the existing managed-browser result boundary.

No runner, stdin injection, queue acknowledgement, capability grant or disk
cursor. Create once from the owning adapter's trusted ctx, not tool arguments.
prepare() leaves even display bookkeeping untouched; note_sent() is called only
after a successful HTTP write. Neither operation proves model adoption/execution.
Already-running workers do not acquire this code by editing files on disk.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
import uuid

import artifact_store
import project_context
import team_state


_REQUEST_FIELDS = ("request_id", "user_id", "conversation_id", "source_team_id",
                   "team_id", "text_sha256")
_HEADER = "MEYO_PENDING_FEEDBACK_JSON\n"
_NOTICE = ("Read-only visibility of authenticated user feedback, still queued. "
           "This is not a new execution or a consumption/completion receipt. "
           "Display deduplication does not guarantee execution deduplication.")
# Match the existing mailbox short-body / context-projection sizes. These are
# transport bounds, not model windows or product-quality requirements.
_INLINE_CHARS = 4000
_CONTEXT_CHARS = 12000


def _require(condition):
    if not condition:
        raise ValueError("feedback context binding or body unavailable")


def _identity(value):
    _require(isinstance(value, str) and bool(value) and value.strip() == value and "\0" not in value)
    return value


def _team_id(value):
    value = _identity(value)
    _require(value not in (".", "..") and team_state._safe(value) == value
             and project_context._safe(value) == value)
    return value


def _directory(value):
    _require(isinstance(value, str) and Path(value).is_absolute())
    path = Path(value).resolve(strict=True)
    _require(path.is_dir())
    return path


def _workspace(packet):
    roots = [packet[key] for key in ("repo_root", "workspace_root") if packet.get(key)]
    _require(bool(roots))
    paths = {_directory(root) for root in roots}
    _require(len(paths) == 1)
    return paths.pop()


def _link(packet):
    saved = packet.get("user_feedback_request") or {}
    _require(isinstance(saved, dict))
    return (packet.get("continued_from_team_id") or "",
            tuple(saved.get(key) for key in (*_REQUEST_FIELDS, "message_id")))


def create(ctx, *, native_session_id=None, readable_body_ref=None):
    """Return an optional view for an admitted, conversation-scoped Codex resume.

    native_session_id, if supplied by the adapter, must equal its original
    checkpoint. Without it, use that same trusted checkpoint; never ctx.session_id.
    readable_body_ref is an optional *parent-only* resolver for an already
    authorized native-readable reference. It receives a verified body ref and
    returns {uri|path, sha256, bytes}, or None. It must not grant access, copy
    files, fetch content, or execute work. With no resolver, long bodies are
    explicitly unavailable; internal store paths are not handed to the native.
    Missing/foreign bindings disable this optional context, not the browser.
    """
    try:
        return FeedbackContext(ctx, native_session_id, readable_body_ref)
    except Exception:
        return None


class FeedbackContext:
    def __init__(self, ctx, native_session_id, readable_body_ref):
        facts, memory, packet = ctx.task_facts, ctx.task_memory, ctx.project_context
        _require(all(isinstance(value, Mapping) for value in (facts, memory, packet)))
        self.user = _identity(ctx.user_id)
        self.conversation = _identity(packet.get("conversation_id"))
        self.team, self.task = _team_id(facts.get("team_id")), _identity(facts.get("task_id"))
        self.workdir = _directory(ctx.workdir)
        self._work_identity = self._inode(self.workdir.stat())
        self._bound(packet, self.team)
        _require(_workspace(packet) == self.workdir
                 and _directory(memory.get("workdir")) == self.workdir)
        # run_one namespaces the adapter slot; persisted memory keeps the
        # logical ID (legion_core.py:3597,4053). Neither is the native UUID.
        logical_session = _identity(memory.get("native_conversation_id"))
        _require(_identity(ctx.session_id) == "codex:" + logical_session)
        _require((facts.get("native_session_scope") or "conversation") == "conversation"
                 and (memory.get("native_session_scope") or "conversation") == "conversation"
                 and facts.get("native_context_origin") == memory.get("native_context_origin")
                 and facts.get("assigned_harness", "codex") == "codex")
        checkpoint = memory.get("native_checkpoint") or {}
        _require(checkpoint.get("kind") == "codex_thread")
        self.native_session_id = _identity(checkpoint.get("session_id"))
        _require(str(uuid.UUID(self.native_session_id)) == self.native_session_id
                 and native_session_id in (None, self.native_session_id))
        self.attempt = facts.get("attempt_id")
        _require(type(self.attempt) is int and self.attempt > 0)
        _require(readable_body_ref is None or callable(readable_body_ref))
        self._readable_body_ref = readable_body_ref
        # Large isolated-worker env retains binding fields, not ancestry. Read
        # the already-bound team's canonical packet in _lineage and pin its
        # fully proven links there; never dereference a supplied context path.
        ref = packet.get("context_ref")
        projected = (not any(key in packet for key in
                             ("continued_from_team_id", "user_feedback_request"))
                     and isinstance(ref, dict)
                     and ref == (getattr(ctx, "context_refs", None) or {}).get(
                         "MEYO_PROJECT_CONTEXT_JSON"))
        self._current_link = None if projected else _link(packet)
        self._roots = self._configured_roots()
        _require(all(not root.is_relative_to(self.workdir) for root in self._roots))
        self._shown = set()
        self._lock = threading.RLock()
        self._lineage_stamp = None
        _, self._lineage_stamp = self._lineage()

    @staticmethod
    def _inode(info):
        return info.st_dev, info.st_ino

    @staticmethod
    def _configured_roots():
        return (team_state._team_dir().resolve(), project_context._root().resolve(),
                artifact_store.ROOT.resolve())

    def _bound(self, record, team):
        _require(isinstance(record, Mapping)
                 and record.get("team_id") == team and record.get("user_id") == self.user
                 and record.get("conversation_id") == self.conversation)

    def _request(self, message, source):
        _require(isinstance(message, dict) and type(message.get("id")) is int
                 and message["id"] > 0 and message.get("to") == "commander")
        request = message.get("continuation")
        _require(isinstance(request, dict))
        request_id = _identity(request.get("request_id"))
        expected_child = "feedback_" + hashlib.sha256(
            (source + "\0" + request_id).encode()).hexdigest()[:20]
        sha = request.get("text_sha256")
        _require(request.get("user_id") == self.user
                 and request.get("conversation_id") == self.conversation
                 and request.get("source_team_id") == source
                 and request.get("team_id") == expected_child
                 and isinstance(sha, str) and len(sha) == 64
                 and all(char in "0123456789abcdef" for char in sha))
        return request

    def _body(self, message, source):
        """Reuse the mailbox full-body contract, with task-scoped no-follow reads.

        The older read_text helper checks the global artifact root, not the
        owning team/task. Do not reopen through it after validating a path.
        Ordinary attachment references are neither fetched nor exposed.
        """
        refs = message.get("artifact_refs") or []
        _require(isinstance(refs, list))
        refs = [ref for ref in refs if isinstance(ref, dict)
                and ref.get("name") == f"mail-{message['id']}.txt"]
        if not refs:
            body = message.get("text")
            _require(isinstance(body, str))
            return body, None
        ref = refs[-1]
        sha, size = ref.get("sha256"), ref.get("bytes")
        _require(isinstance(sha, str) and len(sha) == 64
                 and all(char in "0123456789abcdef" for char in sha)
                 and type(size) is int and 0 <= size <= artifact_store._MAX_READ)
        root = self._roots[2]
        parts = (artifact_store._safe(source), "commander", sha + ".txt")
        _require(isinstance(ref.get("path"), str)
                 and Path(ref["path"]) == root.joinpath(*parts))
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        with ExitStack() as stack:
            fd = os.open(root, flags | os.O_DIRECTORY)
            stack.callback(os.close, fd)
            for part in parts[:-1]:
                fd = os.open(part, flags | os.O_DIRECTORY, dir_fd=fd)
                stack.callback(os.close, fd)
            fd = os.open(parts[-1], flags | os.O_NONBLOCK, dir_fd=fd)
            stream = stack.enter_context(os.fdopen(fd, "rb"))
            before = os.fstat(stream.fileno())
            _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                     and before.st_size == size)
            raw = stream.read(size + 1)
            after = os.fstat(stream.fileno())
            _require((before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                     == (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                     and len(raw) == size and hashlib.sha256(raw).hexdigest() == sha)
        return raw.decode("utf-8"), {
            "path": str(root.joinpath(*parts)), "sha256": sha, "bytes": size,
            "name": f"mail-{message['id']}.txt", "mime": "text/plain",
        }

    def _lineage(self, locked_state=None):
        """Only the exact current->ancestor links; never list/search sibling teams."""
        _require(self._configured_roots() == self._roots
                 and self._inode(self.workdir.stat()) == self._work_identity)

        def load(team):
            return (locked_state if locked_state is not None
                    and locked_state.get("team_id") == team else team_state.load(team))

        team, sources, stamp, visited = self.team, [(self.team, None)], [], set()
        native_proven = False
        while team:
            _require(team not in visited and len(visited) < 32)
            visited.add(team)
            state, packet = load(team), project_context.load(team)
            self._bound(state, team)
            self._bound(packet, team)
            _require(_workspace(packet) == self.workdir)
            node = (state.get("tasks") or {}).get(self.task) or {}
            _require(node.get("assigned_harness") == "codex")
            if team == self.team:
                _require(node.get("active_attempt") == self.attempt
                         and (self._current_link is None or _link(packet) == self._current_link))
            checkpoint = (node.get("result") or {}).get("native_checkpoint")
            if checkpoint:
                _require(checkpoint.get("kind") == "codex_thread"
                         and checkpoint.get("session_id") == self.native_session_id)
                native_proven = True
            parent, saved_fields = _link(packet)
            stamp.append((team, parent, saved_fields))
            policy_parent = ((state.get("orchestration_spec") or {}).get("team_policy") or {}).get(
                "continuation_parent_team_id") or ""
            if not packet.get("user_feedback_request"):
                # Ordinary project history also uses continued_from_team_id.
                # It is NOT another mailbox edge. Stop at this proven team;
                # a declared feedback policy without its request is invalid.
                _require(not policy_parent)
                break
            _require(bool(parent))
            parent = _team_id(parent)
            _require(parent != team and policy_parent == parent)
            parent_state = load(parent)
            self._bound(parent_state, parent)
            saved = packet["user_feedback_request"]
            queue = (parent_state.get("mailboxes") or {}).get("commander") or []
            matches = [message for message in queue if message.get("id") == saved.get("message_id")]
            _require(len(matches) == 1)
            message = matches[0]
            request = self._request(message, parent)
            _require(request["team_id"] == team and request.get("status") in ("running", "completed")
                     and all(saved.get(field) == request.get(field) for field in _REQUEST_FIELDS))
            body, _ = self._body(message, parent)
            _require(hashlib.sha256(body.encode()).hexdigest() == request["text_sha256"])
            sources.append((parent, message["id"]))
            team = parent
        stamp = tuple(stamp)
        _require(native_proven and (self._lineage_stamp is None or stamp == self._lineage_stamp))
        return sources, stamp

    def _project(self, message, source):
        request = self._request(message, source)
        body, ref = self._body(message, source)
        sha = hashlib.sha256(body.encode()).hexdigest()
        _require(sha == request["text_sha256"])
        fact = {
            "source_team_id": source, "recipient": "commander", "message_id": message["id"],
            "request_id": request["request_id"], "queue_status": "pending",
            "sha256": sha, "bytes": len(body.encode()),
        }
        if len(body) <= _INLINE_CHARS:
            return {**fact, "text": body}
        readable = self._readable_body_ref(dict(ref)) if ref and self._readable_body_ref else None
        if readable is not None:
            _require(isinstance(readable, dict) and readable.get("sha256") == sha
                     and readable.get("bytes") == fact["bytes"]
                     and set(readable) <= {"uri", "path", "sha256", "bytes", "name", "mime"}
                     and bool(readable.get("uri") or readable.get("path")))
            return {**fact, "body_ref": dict(readable), "text_omitted": True}
        return {**fact, "text_omitted": True, "body_unavailable": True}

    @staticmethod
    def _display_key(fact):
        # If a previously unavailable body becomes readable, it is new context.
        mode = "text" if "text" in fact else "ref" if "body_ref" in fact else "unavailable"
        return fact["source_team_id"], fact["message_id"], fact["sha256"], mode

    def _snapshot(self, shown):
        sources, _ = self._lineage()
        view = {"visibility_only": True, "execution_deduplicated": False, "notice": _NOTICE,
                "native_session_id": self.native_session_id, "target_team_id": self.team,
                "task_id": self.task, "messages": []}
        for source, after_id in sources:
            messages = team_state.read_messages(
                source, "commander", mark_read=False,
                owner_check=lambda state: self._lineage(locked_state=state))
            ids = [message["id"] for message in messages]
            _require(len(ids) == len(set(ids)))
            start = ids.index(after_id) + 1 if after_id is not None else 0
            for message in messages[start:]:
                if (message.get("continuation") or {}).get("status") != "pending":
                    continue
                fact = self._project(message, source)
                if self._display_key(fact) in shown:
                    continue
                candidate = {**view, "messages": [*view["messages"], fact]}
                if len(_HEADER) + len(json.dumps(candidate, ensure_ascii=False)) > _CONTEXT_CHARS:
                    break  # Whole bodies only; remaining pending facts stay available.
                view = candidate
        self._lineage()  # A selection/read is not a lasting authorization.
        return view

    def snapshot(self):
        """Pure optional snapshot, including facts already displayed by this worker."""
        try:
            with self._lock:
                return self._snapshot(set())
        except Exception:
            return None

    def prepare(self, result):
        """Append one text block without changing result, mailbox or display cursor."""
        try:
            with self._lock:
                if not isinstance(result, dict) or not isinstance(result.get("content"), list):
                    return result, ()
                view = self._snapshot(self._shown)
                if not view["messages"]:
                    return result, ()
                keys = tuple(self._display_key(fact) for fact in view["messages"])
                return ({**result, "content": [*result["content"], {
                    "type": "text", "text": _HEADER + json.dumps(view, ensure_ascii=False)}]}, keys)
        except Exception:
            # Optional mail failures must not reach ManagedBrowserLease's
            # backend-failure handler, alter isError, or stop a healthy browser.
            return result, ()

    def note_sent(self, presentation_keys):
        """Worker-local display dedup ONLY; no read_at/native_delivery/queue write."""
        with self._lock:
            self._shown.update(presentation_keys)
