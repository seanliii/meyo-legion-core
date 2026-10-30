from pathlib import Path

import orchestrator_provider as P


class FakeProvider:
    def __init__(self):
        self.created = []
        self.sent = []
        self.cancelled = []
        self.calls = {}

    def list_agents(self):
        return [
            {"id": "a1", "name": "meyo-cc"},
            {"id": "a2", "name": "meyo-codex"},
        ]

    def create_child(self, parent_id, agent_id, title):
        sid = f"s{len(self.created)+1}"
        self.created.append((parent_id, agent_id, title, sid))
        return {"id": sid}

    def send(self, session_id, message):
        self.sent.append((session_id, message))
        return {"queued": True}

    def get_status(self, session_id):
        return {"status": "running" if session_id == "s1" else "idle"}

    def get_history(self, session_id, limit=30):
        self.calls[session_id] = self.calls.get(session_id, 0) + 1
        if session_id == "s2":
            return [{"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "完成答案"}]}]
        return []

    def cancel(self, session_id):
        self.cancelled.append(session_id)
        return {"queued": False}


def test_http_provider_discovers_session_scoped_agents():
    provider = P.HttpOmnigentProvider("http://local")
    def request(path, data=None):
        if path.startswith("/v1/agents"):
            return {"data": [{"id": "template", "name": "polly"}]}
        if path.startswith("/v1/sessions?"):
            return {"data": [
                {"id": "s-new", "agent_id": "captain-new", "agent_name": "meyo-captain",
                 "updated_at": 2, "runner_online": True},
                {"id": "s-old", "agent_id": "captain-old", "agent_name": "meyo-captain",
                 "updated_at": 1, "runner_online": False},
            ]}
        if path == "/v1/sessions/s-new":
            return {"id": "s-new", "agent_id": "captain-new"}
        raise AssertionError(path)
    provider._request = request
    rows = provider.list_agents()
    captain = next(row for row in rows if row["name"] == "meyo-captain")
    assert captain["id"] == "captain-new" and captain["session_id"] == "s-new"
    assert provider.ensure_parent("meyo-captain", workspace="/tmp", title="x")["id"] == "s-new"


def test_delivery_contract_checks_files_and_terms(tmp_path):
    (tmp_path / "report.json").write_text("{}")
    contract = P.DeliveryContract(required_files=("report.json", "summary.md"),
                                  required_answer_terms=("结论",), min_answer_chars=4)
    failed = contract.evaluate("结论如下", tmp_path)
    assert not failed["passed"] and failed["missing_files"] == [str(tmp_path / "summary.md")]
    (tmp_path / "summary.md").write_text("ok")
    assert contract.evaluate("结论如下", tmp_path)["passed"]


def test_supervisor_times_out_and_replaces(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr(P.orchestrator, "list_harnesses", lambda: [
        {"key": "cc", "available": True}, {"key": "codex", "available": True}
    ])
    monkeypatch.setattr(P.health, "circuit_open", lambda key: False)
    clock = {"v": 0.0}
    monkeypatch.setattr(P.time, "monotonic", lambda: clock["v"])
    monkeypatch.setattr(P.time, "sleep", lambda value: clock.__setitem__("v", clock["v"] + value))
    supervisor = P.DeterministicSupervisor(provider, child_timeout_s=2, poll_s=1,
                                           max_replacements=1)

    result = supervisor.run_child("parent", "a1", "meyo-cc", "audit", "查清问题")

    assert result["status"] == "completed" and result["answer"] == "完成答案"
    assert provider.cancelled == ["s1"]
    assert [row[1] for row in provider.created] == ["a1", "a2"]


def test_original_agent_gets_self_heal_before_handoff(monkeypatch):
    provider = FakeProvider()
    provider.get_history = lambda session_id, limit=30: (
        [{"type": "message", "role": "assistant",
          "content": [{"text": "自愈完成"}]}]
        if len(provider.sent) >= 2 else []
    )
    values = iter([0, 0, 3, 4, 4])
    monkeypatch.setattr(P.time, "monotonic", values.__next__)
    monkeypatch.setattr(P.time, "sleep", lambda value: None)
    persisted_events = []
    supervisor = P.DeterministicSupervisor(provider, child_timeout_s=2, poll_s=1,
                                           max_replacements=1,
                                           event_sink=persisted_events.append)

    result = supervisor.run_child("parent", "a1", "meyo-cc", "audit", "查清问题")

    assert result["status"] == "completed" and result["answer"] == "自愈完成"
    assert len(provider.created) == 1 and provider.cancelled == []
    assert [event["stage"] for event in result["events"]] == [
        "run", "diagnose", "self_repair", "delivery"
    ]
    assert persisted_events == result["events"]
    assert result["scores"] == {
        "autonomy": 1.0, "context_integrity": 1.0, "flow_integrity": 1.0
    }
    assert "delivery_contract" in provider.sent[1][1]


def test_delivery_failure_triggers_self_heal(monkeypatch):
    provider = FakeProvider()
    def history(session_id, limit=30):
        text = "完整结论" if len(provider.sent) >= 2 else "草稿"
        return [{"type": "message", "role": "assistant", "content": [{"text": text}]}]
    provider.get_history = history
    supervisor = P.DeterministicSupervisor(provider, child_timeout_s=10, poll_s=1,
                                           max_replacements=0)
    result = supervisor.run_child(
        "parent", "a1", "meyo-cc", "delivery", "完成交付",
        {"required_answer_terms": ["结论"], "min_answer_chars": 4},
    )
    assert result["status"] == "completed" and result["answer"] == "完整结论"
    assert len(provider.sent) == 2
    assert "delivery_failed" in provider.sent[1][1]
    assert any(event["stage"] == "self_repair" for event in result["events"])


def test_handoff_packet_keeps_failure_attempts_and_contract(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr(P.orchestrator, "list_harnesses", lambda: [
        {"key": "cc", "available": True}, {"key": "codex", "available": True}
    ])
    monkeypatch.setattr(P.health, "circuit_open", lambda key: False)
    clock = {"v": 0.0}
    monkeypatch.setattr(P.time, "monotonic", lambda: clock["v"])
    monkeypatch.setattr(P.time, "sleep", lambda value: clock.__setitem__("v", clock["v"] + value))
    result = P.DeterministicSupervisor(provider, child_timeout_s=1, poll_s=1,
                                       max_replacements=1).run_child(
        "parent", "a1", "meyo-cc", "audit", "查清问题", {"minimum": "完整"}
    )
    successor_packet = next(message for sid, message in provider.sent if sid == "s2")
    assert result["status"] == "completed"
    assert all(term in successor_packet for term in
               ("failure", "evidence", "attempts", "delivery_contract"))
    assert any(event["stage"] == "handoff" for event in result["events"])


def test_successor_also_gets_self_heal_chance(monkeypatch):
    provider = FakeProvider()
    def history(session_id, limit=30):
        if session_id == "s2" and len([x for x in provider.sent if x[0] == "s2"]) >= 2:
            return [{"type": "message", "role": "assistant",
                     "content": [{"text": "替补自愈完成"}]}]
        return []
    provider.get_history = history
    monkeypatch.setattr(P.orchestrator, "list_harnesses", lambda: [
        {"key": "cc", "available": True}, {"key": "codex", "available": True}
    ])
    monkeypatch.setattr(P.health, "circuit_open", lambda key: False)
    clock = {"v": 0.0}
    monkeypatch.setattr(P.time, "monotonic", lambda: clock["v"])
    monkeypatch.setattr(P.time, "sleep", lambda value: clock.__setitem__("v", clock["v"] + value))
    result = P.DeterministicSupervisor(provider, child_timeout_s=1, poll_s=1,
                                       max_replacements=1).run_child(
        "parent", "a1", "meyo-cc", "audit", "查清问题"
    )
    assert result["status"] == "completed" and result["answer"] == "替补自愈完成"
    assert any(row.get("agent") == "meyo-codex" and row.get("heal") == 1
               for row in result["attempts"])
    assert [x[0] for x in provider.sent].count("s2") == 2


def test_assistant_text_ignores_non_assistant_items():
    assert P.assistant_text([
        {"type": "message", "role": "user", "content": [{"text": "no"}]},
        {"type": "message", "role": "assistant", "content": [{"text": "yes"}]},
    ]) == "yes"
