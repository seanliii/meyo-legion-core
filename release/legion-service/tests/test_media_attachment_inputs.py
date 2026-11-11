"""Offline attachment ownership/context and real FFmpeg frame handoff."""
import ast
import base64
from copy import deepcopy
import importlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace
from typing import Optional
from urllib.parse import urlparse

import pytest

SERVICE = Path(__file__).resolve().parents[1]


@pytest.fixture
def case(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    for key in tuple(os.environ):
        if key.startswith(("MEYO_", "LEGION_", "AIGC_", "OPENAI_", "CODEX_")):
            monkeypatch.delenv(key)
    for key, leaf in (
        ("MEYO_LEGION_DIR", "data"), ("LEGION_HOME_DIR", "home"),
        ("LEGION_SESSIONS_DIR", "sessions"), ("LEGION_TEAM_DIR", "teams"),
        ("LEGION_PROJECT_CONTEXT_DIR", "projects"),
    ):
        directory = root / leaf
        directory.mkdir(mode=0o700)
        monkeypatch.setenv(key, str(directory))
    monkeypatch.syspath_prepend(str(SERVICE))
    def forbidden(*args, **kwargs):
        raise AssertionError("No model or network calls in attachment tests")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    identity = importlib.import_module("personal_identity")
    sessions = importlib.import_module("sessions")
    project = importlib.import_module("project_context")
    monkeypatch.setattr(project, "ROOT", root / "projects")
    for user in ("alice", "bob"):
        sessions.save_turn(f"fixture-{user}", "offline attachment fixture",
                           {"results": []}, user_id=user)
    own = Path(identity.upload_root("alice"))
    other = Path(identity.upload_root("bob"))
    own.mkdir(parents=True)
    other.mkdir(parents=True)
    rows = []
    for i in range(3):
        path = own / f"random-{i}.mp4"
        path.write_bytes(b"metadata fixture; not a decoded video")
        rows.append({"path": str(path), "orig_name": f"原参考{i}.mp4",
                     "description": f"用户对第{i}份参考的说明",
                     "source_ref": {"reference_id": f"ref-{i}", "origin": "user_upload"}})
    foreign = other / "foreign.mp4"
    foreign.write_bytes(b"another owner's file")
    work = root / "work"
    work.mkdir(mode=0o700)
    local = threading.local()
    monkeypatch.setitem(sys.modules, "harness_registry",
                        SimpleNamespace(_INPROC_RUNTIME_ENV=local))
    media = importlib.import_module("media_tools")
    monkeypatch.setattr(media, "_MEDIA_DIR", str(root / "legacy-media"))
    tree = ast.parse((SERVICE / "legion_core.py").read_text())
    names = {"_attachment_path", "_attachment_context_rows", "_media_prelude"}
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    ctx = {"os": os, "json": json, "Optional": Optional, "HERE": str(SERVICE)}
    exec(compile(ast.Module(body=functions, type_ignores=[]), "legion_core.py", "exec"), ctx)
    return SimpleNamespace(identity=identity, sessions=sessions, rows=rows, foreign=foreign,
                           root=root, work=work, local=local, media=media, ctx=ctx, mp=monkeypatch)


def bind_http(case, payload, route="/api/legion/dispatch"):
    tree = ast.parse((SERVICE / "server.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef)
              and n.name == "_bind_personal_request")
    scope = {"urlparse": urlparse}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "server.py", "exec"), scope)
    replies = []
    handler = SimpleNamespace(path=route, _personal_user=lambda: "alice",
                              _send_json=lambda body, status: replies.append((status, body)))
    return scope[fn.name](handler, payload, allow_new=True), replies


@pytest.mark.parametrize("mode", ["pk", "auto"])
@pytest.mark.parametrize("location", ["top", "plan", "input_snapshot", "project_context"])
def test_http_owned_multi_attachments_and_foreign_batch_rejection(case, mode, location):
    def packet(rows):
        body = {"mode": mode, "uid": "alice", "session_id": "fixture-alice"}
        if location == "top":
            body["attachments"] = rows
        elif location == "project_context":
            body[location] = {"input_snapshot": {"attachments": rows}}
        else:
            body[location] = {"attachments": rows}
        return body
    assert bind_http(case, packet(deepcopy(case.rows)))[0]
    mixed = deepcopy(case.rows) + [{"path": str(case.foreign), "name": "伪装成本人.mp4"}]
    ok, replies = bind_http(case, packet(mixed))
    assert not ok and replies[0][0] == 403
    assert case.foreign.read_bytes() == b"another owner's file"


@pytest.mark.parametrize("bad", ["relative", "missing", "directory", "symlink", "alias_conflict",
                                  "null", "not_list", "non_string_path"])
def test_invalid_attachment_paths_rejected_before_context(case, bad):
    own = Path(case.rows[0]["path"])
    if bad == "symlink":
        link = own.parent / "alias.mp4"
        link.symlink_to(case.foreign)
        rows = [str(link)]
    else:
        rows = {
            "relative": ["relative.mp4"], "missing": [str(own.parent / "missing.mp4")],
            "directory": [str(own.parent)],
            "alias_conflict": [{"path": str(own), "local_path": str(case.foreign)}],
            "null": None, "not_list": str(own), "non_string_path": [{"path": 123}],
        }[bad]
    assert not bind_http(case, {"attachments": rows})[0]


@pytest.mark.parametrize("source", ["attachments", "input_snapshot", "project_context"])
def test_recovery_rechecks_sources_preserves_names_and_explicit_clear(case, source):
    rows = deepcopy(case.rows)
    plan = ({"attachments": rows} if source == "attachments" else
            {"input_snapshot": {"attachments": rows}} if source == "input_snapshot" else
            {"project_context": {"input_snapshot": {"attachments": rows}}})
    original = deepcopy(plan)
    bound = case.identity.bind_plan_attachments(plan, "alice", "fixture-alice")
    assert [r["name"] for r in bound] == [r["orig_name"] for r in case.rows]
    assert all(r["description"] == o["description"] and r["source_ref"] == o["source_ref"]
               for r, o in zip(bound, case.rows))
    Path(rows[1]["path"]).unlink()
    with pytest.raises(PermissionError, match="attachment_not_authorized"):
        case.identity.bind_plan_attachments(original, "alice", "fixture-alice")
    cleared = {"attachments": [], "input_snapshot": {"attachments": rows}}
    assert case.identity.bind_plan_attachments(cleared, "alice", "fixture-alice") == []


def test_registered_artifact_keeps_existing_exact_file_scope(case):
    artifact = case.work / "published.png"
    artifact.write_bytes(b"registered artifact fixture")
    sibling = case.work / "unregistered.png"
    sibling.write_bytes(b"not granted")
    case.sessions.save_turn("fixture-alice", "artifact",
                            {"results": [{"key": "fixture", "status": "ok",
                                          "artifacts": [{"path": str(artifact)}]}]}, user_id="alice")
    assert case.identity.authorized_attachments([str(artifact)], "alice", "fixture-alice")
    for path, user in ((sibling, "alice"), (artifact, "bob")):
        with pytest.raises(PermissionError):
            case.identity.authorized_attachments([str(path)], user, f"fixture-{user}")


def test_planner_and_author_receive_same_original_source_descriptions(case):
    rows = case.identity.authorized_attachments(case.rows, "alice", "fixture-alice")
    # Execute the actual planner attachment construction, without its model/planning call.
    tree = ast.parse((SERVICE / "legion_core.py").read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == "plan_request_with_model")
    stmt = next(n for n in fn.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "att" for t in n.targets))
    scope = {**case.ctx, "attachments": rows}
    exec(compile(ast.Module(body=[stmt], type_ignores=[]), "planner-attachments", "exec"), scope)
    text = case.ctx["_media_prelude"]("自主参考写游戏", rows)
    for row in scope["att"]:
        assert row["name"] in text and row["description"] in text
        assert row["source_ref"]["reference_id"] in text
        assert row["content_read"] == "not_performed"
    assert "view_image" in text and "--json see_image" not in text
    assert "请求时刻与是否回退" in text and "实际时刻" not in text
    events = []
    text = case.ctx["_media_prelude"]("", rows * 4, events.append)
    assert len([line for line in text.splitlines() if line.startswith("- ")]) == 12
    Path(rows[0]["path"]).unlink()
    assert '"availability": "missing"' in case.ctx["_media_prelude"]("", rows)


@pytest.fixture
def real_video(case):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg not installed; real frame verification remains unverified")
    video = Path(case.rows[0]["path"])
    subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=96x64:rate=4", "-t", "1", "-c:v", "mpeg4", str(video)],
                   capture_output=True, check=True)
    return video


@pytest.mark.parametrize("binding", ["native_environment", "shared_thread"])
def test_real_frame_in_task_workspace_reaches_both_image_consumers(case, real_video, binding):
    env = {"MEYO_WORKDIR": str(case.work)}
    if binding == "native_environment":
        case.mp.setenv("MEYO_WORKDIR", str(case.work))
    else:
        case.local.values = env
        case.local.inherit_process_context = False
    result = case.media.video_frame(str(real_video), 0.5)
    assert result["ok"], result
    frame = Path(result["path"])
    assert frame.parent == case.work and not result["url"]
    assert result["source_path"] == str(real_video) and result["at_s"] == 0.5
    assert result["requested_at_s"] == 0.5 and result["fallback_to_first_frame"] is False
    # Native image consumer receives a real, readable local PNG; no model is invoked.
    raw = frame.read_bytes()
    assert raw.startswith(b"\x89PNG\r\n\x1a\n")
    # Shared vision consumer validates those same bytes and builds the actual image input.
    import media_model_cli as cli
    case.mp.setenv("MEYO_WORKDIR", str(case.work))
    captured = []
    def request(direction, endpoint, payload, timeout_s, **kwargs):
        captured.append(payload["messages"][0]["content"][0]["image_url"]["url"])
        return {"choices": [{"message": {"content": "OFFLINE TRANSPORT STUB"},
                             "finish_reason": "stop"}]}
    case.mp.setattr(cli, "_request", request)
    answer = cli._chat_result("fixture", "vision_understanding", str(frame), 2, workspace_bound=True)
    assert answer["ok"] and base64.b64decode(captured[0].split(",", 1)[1]) == raw
    assert not Path(case.media._MEDIA_DIR).exists()


def test_task_without_valid_workspace_never_falls_back_to_shared_media(case, real_video):
    case.local.values = {}
    case.local.inherit_process_context = False
    result = case.media.video_frame(str(real_video), 0)
    assert result == {"ok": False, "error": "media_image_workspace_invalid"}
    assert not Path(case.media._MEDIA_DIR).exists()


def test_unbound_operator_keeps_existing_media_url(case, real_video):
    result = case.media.video_frame(str(real_video), 0)
    assert result["ok"] and result["url"] == "/media/" + Path(result["path"]).name
    assert Path(result["path"]).parent == Path(case.media._MEDIA_DIR)


def test_frame_fallback_reports_request_and_seek_not_measured_pts(case, real_video):
    case.mp.setenv("MEYO_WORKDIR", str(case.work))
    result = case.media.video_frame(str(real_video), 99)
    assert result["ok"] and result["requested_at_s"] == 99
    assert result["fallback_to_first_frame"] is True and result["at_s"] == 0
    assert result["note"] and Path(result["path"]).parent == case.work
