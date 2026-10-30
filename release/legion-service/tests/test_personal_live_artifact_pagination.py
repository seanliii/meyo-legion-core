"""进行中文件授权的离线回归：真实会话、团队和 JSONL 分页，不启动服务或模型。"""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest


SERVICE = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _pin_isolation_env(tmp_path, monkeypatch):
    # 每项测试运行时重新指定，专用目录变量也不能继承其他测试或个人配置。
    root = tmp_path.resolve()
    for variable, relative in (
        ("HOME", "home"),
        ("MEYO_LEGION_DIR", "data"),
        ("LEGION_HOME_DIR", "legion"),
        ("LEGION_TEAM_DIR", "data/teams"),
        ("LEGION_SESSIONS_DIR", "legion/sessions"),
        ("LEGION_PROJECT_CONTEXT_DIR", "data/project_context"),
    ):
        directory = root / relative
        directory.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(variable, str(directory))
    monkeypatch.setenv("LEGION_PERSONAL_TOKENS_FILE", str(root / "unused-tokens.json"))
    monkeypatch.syspath_prepend(str(SERVICE))


def _register_at(live, path, event_id, *, team_id=None):
    """连填充事件也走真实 append_event；不模拟 read_events 或文件授权。"""
    tid = team_id or live.tid
    current = int(live.teams.load(tid).get("event_seq", 0))
    assert current < event_id
    for _ in range(current + 1, event_id):
        assert live.teams.append_event(tid, {"t": "heartbeat"}) is not None
    record = live.teams.append_event(
        tid,
        {
            "t": "artifact_ready",
            "path": str(path),
            "url": "/home-outputs/" + path.relative_to(live.outputs).as_posix(),
            "name": path.name,
        },
    )
    assert record["event_id"] == event_id
    assert path.is_file()
    # macOS 临时路径先 resolve，避免 /var 与 /private/var 导致无关的路径拒绝。
    assert live.identity._local_reference(record["data"]) == str(path)
    return record


def _save_running(live, sid, tid):
    saved = live.sessions.save_turn(
        sid,
        "离线文件授权分页复核",
        {"status": "running", "team_id": tid, "results": []},
        mode="single",
        user_id=live.user,
    )
    assert saved["sid"] == sid


@pytest.fixture
def live(_pin_isolation_env):
    # 产品模块只在隔离变量就绪后导入，且必须来自这一个源码树。
    import personal_identity
    import sessions
    import team_state

    for module in (personal_identity, sessions, team_state):
        assert Path(module.__file__).resolve().parent == SERVICE
    live = SimpleNamespace(
        identity=personal_identity,
        sessions=sessions,
        teams=team_state,
        user="__ut_page_owner",
        sid="ut-live-pagination",
        tid="ut-live-pagination-team",
        outputs=Path(os.environ["MEYO_LEGION_DIR"]) / "outputs",
    )
    directory = live.outputs / live.tid
    directory.mkdir(parents=True)
    live.early = directory / "vendor.js"
    live.target = directory / "index.html"
    live.sibling = directory / "unregistered.txt"
    for path in (live.early, live.target, live.sibling):
        path.write_text("isolated fixture: " + path.name, encoding="utf-8")
    live.teams.ensure(
        live.tid, "离线文件授权分页复核", {}, {},
        user_id=live.user, conversation_id=live.sid,
    )
    _save_running(live, live.sid, live.tid)
    session = live.sessions.load_session(live.sid, user_id=live.user)
    assert session["sid"] == live.sid and session["user_id"] == live.user
    assert session["turns"][0]["status"] == "running"
    assert list(live.identity.artifact_rows(session)) == []
    _register_at(live, live.early, 1)
    return live


def test_first_page_exact_file_is_allowed(live):
    rows = live.teams.read_events(live.tid)
    assert len(rows) == 1 and rows[0]["event_id"] == 1
    assert live.identity.file_allowed(str(live.early), live.user, live.sid)
    assert live.identity.file_allowed(str(live.early), live.user)


@pytest.mark.parametrize(
    "event_id,page_sizes",
    [(500, [500]), (501, [500, 1]), (717, [500, 217]), (1001, [500, 500, 1])],
    ids=["event-500", "event-501", "event-717", "event-1001"],
)
def test_exact_registered_file_is_allowed_across_pages(live, event_id, page_sizes):
    record = _register_at(live, live.target, event_id)
    # 先证明真实持久层没有丢记录：717 在第二页，1001 在第三页。
    rows, after = [], 0
    for expected_size in page_sizes:
        page = live.teams.read_events(live.tid, after=after)
        assert len(page) == expected_size
        assert page[0]["event_id"] == after + 1
        rows.extend(page)
        after = page[-1]["event_id"]
    assert len(rows) == event_id and rows[-1] == record
    assert live.teams.read_events(live.tid, after=after) == []
    assert live.identity.file_allowed(str(live.early), live.user, live.sid)
    allowed = live.identity.file_allowed(str(live.target), live.user, live.sid)
    assert allowed, (
        f"event_id={event_id} 的精确文件已真实落盘并读回，分页条数={page_sizes}；"
        "同 owner/team/session 的 running 会话不应漏掉后页授权"
    )


def test_event_717_is_allowed_without_explicit_session_id(live):
    record = _register_at(live, live.target, 717)
    assert record not in live.teams.read_events(live.tid)
    assert live.teams.read_events(live.tid, after=500)[-1] == record
    assert [row["sid"] for row in live.sessions.list_sessions(user_id=live.user)] == [live.sid]
    assert live.identity.file_allowed(str(live.early), live.user)
    allowed = live.identity.file_allowed(str(live.target), live.user)
    assert allowed, "不带 sid 的已认证文件请求也必须读到 event_id=717 的精确登记"


def test_unregistered_sibling_is_not_authorized(live):
    _register_at(live, live.target, 717)
    assert live.sibling.parent == live.early.parent == live.target.parent
    assert live.identity.file_allowed(str(live.early), live.user, live.sid)
    assert not live.identity.file_allowed(str(live.sibling), live.user, live.sid)
    assert not live.identity.file_allowed(str(live.sibling), live.user)


def test_other_owner_cannot_use_first_or_later_page_grants(live):
    _register_at(live, live.target, 717)
    other_owner = "__ut_other_owner"
    assert live.sessions.load_session(live.sid, user_id=other_owner) is None
    for path in (live.early, live.target):
        assert not live.identity.file_allowed(str(path), other_owner, live.sid)
        assert not live.identity.file_allowed(str(path), other_owner)


@pytest.mark.parametrize("mismatch", ["owner", "conversation", "unlinked-team"])
def test_other_team_cannot_grant_files_to_this_session(live, mismatch):
    foreign_tid = "ut-foreign-team"
    live.teams.ensure(
        foreign_tid, "隔离的其他团队", {}, {},
        user_id="__ut_other_owner" if mismatch == "owner" else live.user,
        conversation_id="ut-other-session" if mismatch == "conversation" else live.sid,
    )
    if mismatch != "unlinked-team":
        # 只伪造占位 turn 的引用，不能越过真实 team 的 owner/conversation 检查。
        _save_running(live, live.sid, foreign_tid)
    _register_at(live, live.target, 1, team_id=foreign_tid)
    later = _register_at(live, live.sibling, 717, team_id=foreign_tid)
    assert live.teams.read_events(foreign_tid, after=500)[-1] == later
    assert live.identity.file_allowed(str(live.early), live.user, live.sid)
    for path in (live.target, live.sibling):
        assert not live.identity.file_allowed(str(path), live.user, live.sid)
        assert not live.identity.file_allowed(str(path), live.user)


def test_explicit_session_cannot_borrow_another_owned_sessions_grants(live):
    other_sid, other_tid = "ut-other-session", "ut-other-session-team"
    live.teams.ensure(
        other_tid, "同 owner 的另一个会话", {}, {},
        user_id=live.user, conversation_id=other_sid,
    )
    _save_running(live, other_sid, other_tid)
    _register_at(live, live.target, 1, team_id=other_tid)
    later = _register_at(live, live.sibling, 717, team_id=other_tid)
    assert live.sessions.load_session(other_sid, user_id=live.user)["sid"] == other_sid
    assert live.teams.read_events(other_tid, after=500)[-1] == later
    # 对方会话自己能读第一页；指定当前 sid 时不能借用它的登记。
    assert live.identity.file_allowed(str(live.target), live.user, other_sid)
    assert live.identity.file_allowed(str(live.target), live.user)
    for path in (live.target, live.sibling):
        assert not live.identity.file_allowed(str(path), live.user, live.sid)


def test_pagination_boundary_small_event_cap_still_reaches_717(live, monkeypatch):
    record = _register_at(live, live.target, 717)
    monkeypatch.setattr(live.teams, "_EVENT_CAP", 128)
    rows, after, sizes = [], 0, []
    for _ in range(6):
        page = live.teams.read_events(live.tid, after=after)
        assert page
        rows.extend(page)
        sizes.append(len(page))
        after = page[-1]["event_id"]
    assert sizes == [128, 128, 128, 128, 128, 77]
    assert rows[-1] == record
    assert live.identity.file_allowed(str(live.target), live.user, live.sid)


def test_pagination_boundary_new_grant_waits_for_next_request(live, monkeypatch):
    _register_at(live, live.target, 717)
    assert live.teams.load(live.tid)["event_seq"] == 717
    read_events = live.teams.read_events
    reads, appended = [], []

    def append_after_first_page(team_id, after=0, limit=500):
        reads.append(after)
        if len(reads) > 3:
            pytest.fail("固定快照的请求不应持续追赶新增事件")
        page = read_events(team_id, after=after, limit=limit)
        if not appended:
            # file_allowed 已取 event_seq=717；真实追加 718，第二页会同时含旧/新记录。
            appended.append(_register_at(live, live.sibling, 718))
        return page

    monkeypatch.setattr(live.teams, "read_events", append_after_first_page)
    assert not live.identity.file_allowed(str(live.sibling), live.user, live.sid)
    assert len(appended) == 1
    assert live.teams.load(live.tid)["event_seq"] == 718
    assert read_events(live.tid, after=717) == appended
    reads.clear()
    assert live.identity.file_allowed(str(live.sibling), live.user, live.sid)


def test_pagination_boundary_legacy_stream_without_event_seq(live):
    record = _register_at(live, live.target, 717)
    rows = live.teams.read_events(live.tid, limit=1000)
    assert len(rows) == 717 and rows[-1] == record
    legacy = live.teams.load(live.tid)
    legacy.pop("event_seq")
    legacy["stream_events"] = rows
    # 仅将本项临时存储改成真实旧格式，不替换 load/read_events。
    live.teams._write(legacy)
    live.teams._event_path(live.tid).unlink()
    assert "event_seq" not in live.teams.load(live.tid)
    assert live.teams.read_events(live.tid, after=500)[-1] == record
    assert live.identity.file_allowed(str(live.target), live.user, live.sid)
    assert not live.identity.file_allowed(str(live.sibling), live.user, live.sid)


def test_pagination_boundary_share_cannot_read_private_late_grant(live):
    _register_at(live, live.early, 716)
    private = live.teams.append_event(
        live.tid,
        {
            "t": "artifact_ready",
            "path": str(live.target),
            "url": "/home-outputs/" + live.target.relative_to(live.outputs).as_posix(),
            "name": live.target.name,
            "visibility": "private",
        },
    )
    assert private["event_id"] == 717
    assert live.teams.read_events(live.tid, after=500)[-1] == private
    session = live.sessions.load_session(live.sid, user_id=live.user)
    share = session["share_token"]  # 来自本项临时会话，不读取个人凭据。
    assert live.sessions.load_shared_session(live.sid, share)["sid"] == live.sid
    assert live.identity.file_allowed(str(live.early), sid=live.sid, share=share)
    assert live.identity.file_allowed(str(live.target), live.user, live.sid)
    assert not live.identity.file_allowed(str(live.target), sid=live.sid, share=share)


@pytest.mark.parametrize("fault", ["jsonl-read", "lock-open"])
def test_pagination_boundary_page_io_error_denies(live, monkeypatch, fault):
    _register_at(live, live.target, 717)
    event_path = live.teams._event_path(live.tid)
    lock_path = live.teams._locks_dir() / f"{live.tid}.lock"
    original = Path.read_text if fault == "jsonl-read" else Path.open
    target = event_path if fault == "jsonl-read" else lock_path
    reads = []

    def fail_on_second_page(path, *args, **kwargs):
        if path == target:
            reads.append(path)
            if len(reads) > 3:
                pytest.fail("读页持续失败时授权查询必须有限结束")
            if len(reads) >= 2:
                raise OSError("isolated second-page " + fault + " failure")
        return original(path, *args, **kwargs)

    # 在真实 read_events 下方注入 I/O 故障，第一页照常读出。
    monkeypatch.setattr(
        Path, "read_text" if fault == "jsonl-read" else "open", fail_on_second_page,
    )
    allowed = live.identity.file_allowed(str(live.target), live.user, live.sid)
    assert len(reads) >= 2, "必须实际走到第二页故障，不能因漏翻页而假通过"
    assert allowed is False


@pytest.mark.parametrize("fault", ["repeated-page", "stale-grant", "invalid-records"])
def test_pagination_boundary_nonadvancing_page_denies(live, monkeypatch, fault):
    record = _register_at(live, live.target, 717)
    read_events = live.teams.read_events
    first_page = read_events(live.tid)
    reads = []

    def nonadvancing_page(team_id, after=0, limit=500):
        reads.append(after)
        if len(reads) > 3:
            pytest.fail("页游标不前进，授权查询仍在循环")
        if len(reads) == 1:
            return read_events(team_id, after=after, limit=limit)
        if fault == "repeated-page":
            return first_page
        if fault == "stale-grant":
            # 路径、类型都匹配，但游标未前进，不能把陈旧页当新授权。
            return [{**record, "event_id": after}]
        return [
            None,
            [],
            {**record, "event_id": "717"},
            {**record, "event_id": True},
            {**record, "event_id": None},
        ]

    monkeypatch.setattr(live.teams, "read_events", nonadvancing_page)
    assert not live.identity.file_allowed(str(live.target), live.user, live.sid)
    assert len(reads) >= 2, "必须实际观察到坏页"


def test_pagination_boundary_missing_upper_rejects_continuously_growing_pages(live, monkeypatch):
    state = live.teams.load(live.tid)
    state.pop("event_seq")
    state["stream_events"] = []  # 没有可用上界；有有效内嵌 ID 的 legacy 用例保持不变。
    live.teams._write(state)
    stored = live.teams.load(live.tid)
    assert "event_seq" not in stored and stored["stream_events"] == []
    reads = []

    def continuously_growing_pages(team_id, after=0, limit=500):
        reads.append(after)
        if len(reads) > 3:
            pytest.fail("没有可信上界时仍追赶持续新增的心跳页")
        # 每页都满、游标一直前进；不能依赖空页或不前进条件侥幸终止。
        return [
            {"event_id": event_id, "event": "step", "data": {"t": "heartbeat"}}
            for event_id in range(after + 1, after + limit + 1)
        ]

    monkeypatch.setattr(live.teams, "read_events", continuously_growing_pages)
    assert live.identity.file_allowed(str(live.target), live.user, live.sid) is False
    assert reads == [], "无可信快照的团队应直接跳过，不开始无上界翻页"
