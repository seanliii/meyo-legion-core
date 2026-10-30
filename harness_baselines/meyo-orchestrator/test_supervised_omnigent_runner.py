from unittest import mock

from cloud_ui import omnigent_runner as runner


def test_run_via_supervised_omnigent_returns_attempts(monkeypatch):
    fake = mock.Mock()
    fake.run_child.return_value = {
        "status": "completed", "answer": "done", "attempts": [{"agent": "meyo-cc"}]
    }
    constructed = {}
    def factory(*args, **kwargs):
        constructed.update(kwargs)
        return fake
    monkeypatch.setattr("orchestrator_provider.DeterministicSupervisor", factory)
    sink = mock.Mock()
    result = runner.run_via_supervised_omnigent(
        "goal", parent_session_id="p", agent_id="a", agent_name="meyo-cc",
        base_url="http://localhost:1", timeout_s=2, event_sink=sink,
    )
    assert result["status"] == "completed" and result["executor"] == "omnigent"
    assert result["final"] == "done"
    fake.run_child.assert_called_once()
    assert fake.run_child.call_args is not None
    assert constructed["event_sink"] is sink


def test_run_via_omnigent_auto_resolves_session_scoped_agents(monkeypatch):
    monkeypatch.delenv("MEYO_OMNIGENT_PARENT_SESSION_ID", raising=False)
    monkeypatch.delenv("MEYO_OMNIGENT_DEFAULT_AGENT_ID", raising=False)
    provider = mock.Mock()
    provider.ensure_parent.return_value = {"id": "parent"}
    provider.resolve_agent.return_value = {"id": "worker", "name": "meyo-cc"}
    monkeypatch.setattr("orchestrator_provider.HttpOmnigentProvider", lambda url: provider)
    with mock.patch.object(runner, "run_via_supervised_omnigent", return_value={
        "status": "completed", "answer": "auto"
    }) as supervised:
        result = runner.run_via_omnigent("goal", runtime_url="http://local")
    assert result["answer"] == "auto"
    supervised.assert_called_once()
    assert supervised.call_args.kwargs["parent_session_id"] == "parent"
    assert supervised.call_args.kwargs["agent_id"] == "worker"


def test_run_via_omnigent_uses_supervisor_when_configured(monkeypatch):
    monkeypatch.setenv("MEYO_OMNIGENT_URL", "http://localhost:1")
    monkeypatch.setenv("MEYO_OMNIGENT_PARENT_SESSION_ID", "p")
    monkeypatch.setenv("MEYO_OMNIGENT_DEFAULT_AGENT_ID", "a")
    with mock.patch.object(runner, "run_via_supervised_omnigent", return_value={
        "status": "completed", "answer": "ok"
    }) as supervised:
        result = runner.run_via_omnigent("goal")
    assert result["answer"] == "ok"
    supervised.assert_called_once()
