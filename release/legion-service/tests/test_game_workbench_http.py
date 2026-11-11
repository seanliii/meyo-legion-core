"""The game view uses the existing public entry, identity and static resources."""
from pathlib import Path

import pytest

from test_public_origin_entry import isolated, http_entry


@pytest.mark.parametrize("path", ["/", "/index.html?studio=game"])
def test_game_entry_includes_same_origin_view_without_starting_work(http_entry, path):
    status, headers, body = http_entry.request("GET", path, origin=None, authenticated=False)
    assert status == 200
    assert body.count(b'<script src="/game_workbench.js"></script>') == 1
    assert body.count(b'<link rel="stylesheet" href="/game_workbench.css">') == 1
    assert "Set-Cookie" not in headers
    assert http_entry.seen == []


@pytest.mark.parametrize("name,mime", [
    ("game_workbench.js", "application/javascript"),
    ("game_workbench.css", "text/css"),
])
def test_anonymous_game_view_resources_are_exact_and_do_not_grant_execution(http_entry, name, mime):
    status, headers, body = http_entry.request("GET", "/" + name, origin=None, authenticated=False)
    assert status == 200
    assert mime in headers["Content-Type"]
    assert "no-store" in headers["Cache-Control"]
    assert body == (Path(http_entry.server.WEB_DIR) / name).read_bytes()
    assert "Set-Cookie" not in headers
    status, _, _ = http_entry.request("POST", "/api/legion/dispatch", authenticated=False,
                                     body={"question": "must not execute"})
    assert status in (401, 403)
    assert http_entry.seen == []


def test_game_view_does_not_create_a_general_static_file_root(http_entry):
    status, _, _ = http_entry.request("GET", "/game_workbench/../server.py",
                                     origin=None, authenticated=False)
    assert status in (401, 403, 404)
