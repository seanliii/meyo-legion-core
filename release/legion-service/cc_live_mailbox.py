"""Existing team mailbox -> Claude's native input/receipts, without a run loop.

The caller owns stdin, stdout, EOF, cancellation and process lifetime. Returning
an input frame is not delivery: call mark_sent only after writing the entire
frame. Lock order is always object RLock -> team_state._state_lock; this module
never calls back into the caller while holding either lock.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid

import team_state

_FACT_LABEL = "[Meyo 现有任务的补充事实；不是新用户任务]\n"
_PENDING_REFRESH_S = 0.5  # Quiet-file integrity refresh, not a task/quality limit.


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class _Input:
    uuid: str
    text: str
    digest: str
    message_id: int | None = None
    queued_at: float | None = None
    replayed_at: float | None = None
    completed_at: float | None = None
    wire_bytes: bytes | None = None


def create(ctx, native_sid: str, prompt: str) -> CCLiveMailbox | None:
    """No binding keys means standalone. Partial/foreign bindings fail closed."""
    facts = getattr(ctx, "task_facts", None)
    facts = {} if facts is None else facts
    if not isinstance(facts, Mapping):
        raise PermissionError("invalid CC mailbox task binding")
    project = getattr(ctx, "project_context", None)
    if "team_id" not in facts and "task_id" not in facts:
        if isinstance(project, Mapping) and ("team_id" in project or "task_id" in project):
            raise PermissionError("CC mailbox team/task binding is missing from task_facts")
        return None
    if not isinstance(project, Mapping):
        raise PermissionError("CC mailbox requires project conversation binding")
    values = (getattr(ctx, "user_id", None), facts.get("team_id"),
              facts.get("task_id"), project.get("conversation_id"))
    if any(not isinstance(v, str) or not v or v.strip() != v or "\0" in v for v in values):
        raise PermissionError("incomplete CC mailbox owner/team/task/conversation binding")
    try:
        if not isinstance(native_sid, str) or str(uuid.UUID(native_sid)) != native_sid:
            raise ValueError
    except (ValueError, AttributeError):
        raise PermissionError("CC mailbox requires a fixed canonical native UUID") from None
    if not isinstance(prompt, str):
        raise TypeError("CC initial prompt must be text")
    box = CCLiveMailbox(*values, native_sid, prompt)
    with box._state():
        pass
    return box


class CCLiveMailbox:
    def __init__(self, user_id, team_id, task_id, conversation_id, native_sid, prompt):
        self.user_id, self.team_id, self.task_id = user_id, team_id, task_id
        self.conversation_id, self._native_sid = conversation_id, native_sid
        self._lock = threading.RLock()
        self.error: str | None = None
        # Initial input identifies this invocation; durable mail UUIDs below are
        # stable across restarts. The original prompt is never wrapped/rewritten.
        self._initial = _Input(str(uuid.uuid4()), prompt, _digest(prompt))
        self._inputs = {self._initial.uuid: self._initial}
        self._result_ok = False
        self._terminal_failed = False
        self._queued_turn_count = None
        self._binding_stamp = None
        self._pending_stamp = None
        self._pending_until = 0.0
        self._pending_ids = ()

    @property
    def native_sid(self):
        return self._native_sid

    def _fail(self, code):
        if self.error is None:
            self.error = code  # Metadata only: never put message text in errors.

    def _file_stamp(self):
        path = team_state._path(self.team_id)
        try:
            info = path.stat()
            return (str(path.absolute()), info.st_dev, info.st_ino, info.st_size,
                    info.st_mtime_ns, info.st_ctime_ns)
        except OSError:
            return None

    @contextmanager
    def _state(self):
        with self._lock, team_state._state_lock(self.team_id):
            state = team_state.load(self.team_id)
            valid = (isinstance(state, dict) and state.get("team_id") == self.team_id
                     and state.get("user_id") == self.user_id
                     and state.get("conversation_id") == self.conversation_id)
            if valid:
                if self.task_id == "commander":
                    valid = state.get("captain") == "cc"
                else:
                    tasks = state.get("tasks")
                    task = tasks.get(self.task_id) if isinstance(tasks, dict) else None
                    valid = isinstance(task, dict) and task.get("assigned_harness") == "cc"
            if not valid:
                self._fail("mailbox_binding_changed")
                raise PermissionError("CC mailbox owner/conversation/task binding does not match")
            self._binding_stamp = self._file_stamp()
            yield state

    def _messages(self, state):
        mailboxes = state.get("mailboxes")
        messages = mailboxes.get(self.task_id, []) if isinstance(mailboxes, dict) else None
        if not isinstance(messages, list):
            self._fail("invalid_mailbox")
            return []
        return messages

    def _resolve(self, message):
        """Use the existing full-body/artifact hash verifier, not inline preview."""
        try:
            if (not isinstance(message, dict) or type(message.get("id")) is not int
                    or message["id"] <= 0 or message.get("to") != self.task_id):
                raise ValueError
            refs = message.get("artifact_refs") or []
            if not isinstance(refs, list) or any(not isinstance(r, dict) for r in refs):
                raise ValueError
            for ref in refs:
                if ref.get("name") != f"mail-{message['id']}.txt":
                    continue  # Ordinary external references remain references; no fetch.
                import artifact_store
                own = (artifact_store.ROOT.resolve() / artifact_store._safe(self.team_id)
                       / artifact_store._safe(self.task_id))
                path = Path(ref.get("path") or "").resolve()
                if own.resolve() != own or path == own or not path.is_relative_to(own):
                    raise ValueError  # A body reference cannot borrow another task's data.
            resolved = team_state.NativeMailbox._message_envelope(self, message)
            if resolved is None:
                raise ValueError
            text = _FACT_LABEL + resolved[0]
            digest = _digest(text)
            identity = json.dumps(
                [self.user_id, self.conversation_id, self.team_id, self.task_id,
                 self.native_sid, message["id"], digest], ensure_ascii=False)
            return _Input(str(uuid.uuid5(uuid.NAMESPACE_URL, identity)),
                          text, digest, message["id"])
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self._fail("mailbox_full_body_unavailable")
            return None

    def _current_message(self, state, item):
        if item.message_id is None:
            return None
        matches = [m for m in self._messages(state)
                   if isinstance(m, dict) and m.get("id") == item.message_id]
        if len(matches) == 1:
            current = self._resolve(matches[0])
            if current and current.uuid == item.uuid and current.digest == item.digest:
                return matches[0]
        self._fail("mailbox_body_changed")
        return None

    def _binding_fields(self, item):
        return {"uuid": item.uuid, "message_digest": item.digest,
                "user_id": self.user_id, "conversation_id": self.conversation_id,
                "task_id": self.task_id}

    def _completed(self, message, item):
        deliveries = message.get("native_delivery", {})
        if not isinstance(deliveries, dict):
            self._fail("invalid_native_delivery")
            return False
        saved = deliveries.get(self.native_sid, {})
        return (isinstance(saved, dict) and saved.get("phase") == "native_turn_completed"
                and all(type(saved.get(k)) in (int, float) and saved[k] > 0 for k in
                        ("queued_at", "input_replayed_at", "native_turn_completed_at"))
                and all(saved.get(k) == v for k, v in self._binding_fields(item).items()))

    def _persist(self, state, items):
        """Caller already holds both locks; no nested flock acquisition."""
        changed = False
        for item in items:
            if item.message_id is None or item.queued_at is None:
                continue
            message = self._current_message(state, item)
            if message is None:
                continue
            if self._completed(message, item):
                # A delayed writer from another object must not downgrade an
                # already durable completion of the same body/UUID/binding.
                prior = message["native_delivery"][self.native_sid]
                item.replayed_at = item.replayed_at or prior["input_replayed_at"]
                item.completed_at = item.completed_at or prior["native_turn_completed_at"]
                continue
            saved = {**self._binding_fields(item), "phase": "queued", "queued_at": item.queued_at}
            if item.replayed_at is not None:
                saved.update(phase="input_replayed", input_replayed_at=item.replayed_at)
            if item.completed_at is not None:
                saved.update(phase="native_turn_completed",
                             native_turn_completed_at=item.completed_at)
            deliveries = message.setdefault("native_delivery", {})
            if not isinstance(deliveries, dict):
                self._fail("invalid_native_delivery")
                continue
            if deliveries.get(self.native_sid) != saved:
                deliveries[self.native_sid] = saved
                changed = True
        if changed:
            try:
                team_state._write(state)
            except OSError:
                self._fail("mailbox_receipt_write_failed")

    def _wire(self, item):
        if item.wire_bytes is None:
            frame = {"type": "user", "uuid": item.uuid, "session_id": self.native_sid,
                     "parent_tool_use_id": None, "message": {"role": "user", "content": item.text}}
            item.wire_bytes = (json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")
        return item.uuid, item.wire_bytes

    def initial_message(self):
        with self._state():
            return self._wire(self._initial)

    def pending_messages(self):
        with self._lock:
            stamp, now = self._file_stamp(), time.monotonic()
            if stamp == self._pending_stamp and now < self._pending_until:
                if self._terminal_failed:
                    return []
                return [self._wire(self._inputs[key]) for key in self._pending_ids
                        if self._inputs[key].queued_at is None]
            return self._refresh_pending(now)

    def _refresh_pending(self, now):
        with self._state() as state:
            if self._terminal_failed:
                return []
            pending = []
            seen = set()
            for message in self._messages(state):
                candidate = self._resolve(message)
                if candidate is None:
                    continue
                if candidate.message_id in seen:
                    self._fail("duplicate_mailbox_message_id")
                    return []
                seen.add(candidate.message_id)
                if self._completed(message, candidate):
                    continue
                item = self._inputs.setdefault(candidate.uuid, candidate)
                if item.queued_at is None:
                    pending.append(self._wire(item))
            self._pending_ids = tuple(pair[0] for pair in pending)
            self._pending_stamp = self._binding_stamp
            self._pending_until = now + _PENDING_REFRESH_S
            return pending

    def mark_sent(self, message_uuid):
        """Called after the writer has flushed a complete JSONL frame."""
        with self._state() as state:
            item = self._inputs.get(message_uuid) if isinstance(message_uuid, str) else None
            if item is None:
                self._fail("unknown_sent_uuid")
                return False
            if item.message_id is not None and self._current_message(state, item) is None:
                return False
            if item.queued_at is None:
                item.queued_at = time.time()
            self._persist(state, [item])
            return self.error is None

    @staticmethod
    def _replay_text(message):
        if not isinstance(message, dict) or message.get("role") != "user":
            return None
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list) and all(
                isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
                for b in content):
            return "".join(b["text"] for b in content)
        return None

    def observe(self, frame_dict):
        # Token/progress frames and task errors are not mailbox receipts. Avoid
        # full state reads at stream-token frequency; runner owns native errors.
        frame = frame_dict
        if (not isinstance(frame, dict)
                or not (frame.get("type") == "result"
                        or (frame.get("type") == "user" and frame.get("isReplay") is True))):
            return False
        with self._state() as state:
            if (not isinstance(frame, dict) or frame.get("session_id") != self.native_sid
                    or frame.get("parent_tool_use_id") is not None):
                return False
            kind = frame.get("type")
            if kind == "user" and frame.get("isReplay") is True:
                value = frame.get("uuid")
                item = self._inputs.get(value) if isinstance(value, str) else None
                if item is None:
                    return False
                if self._replay_text(frame.get("message")) != item.text:
                    self._fail("mailbox_replay_text_mismatch")
                    return False
                if item.message_id is not None and self._current_message(state, item) is None:
                    return False
                item.replayed_at = item.replayed_at or time.time()
                # A fast reader may beat mark_sent. Keep that genuine receipt in
                # memory only; no persistent phase is written before mark_sent.
                self._persist(state, [item])
                return True
            if kind != "result":
                return False
            self._result_ok = frame.get("subtype") == "success" and frame.get("is_error") is False
            self._terminal_failed = not self._result_ok
            self._queued_turn_count = frame.get("queued_turn_count")
            if not self._result_ok:
                # Native task failure is a terminal transport fact, not delivery
                # unavailability. Close input normally; leave mail replayable.
                return False
            ids = frame.get("user_message_uuids")
            if ids is None:
                ids = [frame["user_message_uuid"]] if isinstance(frame.get("user_message_uuid"), str) else []
            if not isinstance(ids, list) or any(not isinstance(value, str) for value in ids):
                self._fail("invalid_native_result_uuids")
                return False
            completed = []
            for value in set(ids):
                item = self._inputs.get(value)
                if item is None or item.replayed_at is None:
                    continue
                if item.message_id is not None and self._current_message(state, item) is None:
                    continue
                item.completed_at = item.completed_at or time.time()
                completed.append(item)
            self._persist(state, completed)
            return bool(completed)

    def should_close(self):
        """Only native transport completion; never inspect answer content/quality."""
        with self._lock:
            if self._file_stamp() != self._binding_stamp:
                with self._state():
                    pass
            if self.error == "mailbox_binding_changed":
                raise PermissionError("CC mailbox binding no longer matches")
            if self._terminal_failed:
                return True
            if (self.error is not None or self._initial.queued_at is None or not self._result_ok
                    or type(self._queued_turn_count) is not int or self._queued_turn_count != 0
                    or any(x.queued_at is not None and x.completed_at is None
                           for x in self._inputs.values())):
                return False
            return self._close_success()

    def _close_success(self):
        with self._state() as state:
            # This locked read is the EOF cutoff. A peer may have committed new
            # mail after the monitor's last empty poll; previously sent inputs
            # being complete does not establish that this mailbox is empty.
            for message in self._messages(state):
                current = self._resolve(message)
                if current is None or not self._completed(message, current):
                    return False
            sent = [x for x in self._inputs.values() if x.queued_at is not None]
            for item in sent:
                if item.message_id is not None:
                    self._current_message(state, item)
            return (self.error is None and self._initial.queued_at is not None
                    and self._result_ok and type(self._queued_turn_count) is int
                    and self._queued_turn_count == 0
                    and all(x.replayed_at is not None and x.completed_at is not None for x in sent))
