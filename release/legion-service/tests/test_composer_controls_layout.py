"""Structural regression; actual browser hit testing is separate evidence."""
from html.parser import HTMLParser
from pathlib import Path


HTML = Path(__file__).resolve().parents[1] / "web/index_v2.html"
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}


class Controls(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.controls = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "button" and attrs.get("id") in {"advToggle", "send"}:
            self.controls.setdefault(attrs["id"], []).append((attrs, list(self.stack)))
        if tag not in VOID:
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


def controls():
    parser = Controls()
    parser.feed(HTML.read_text())
    return parser.controls


def test_advanced_control_uses_sidebar_flow_instead_of_overlaying_send():
    rows = controls()["advToggle"]
    assert len(rows) == 1
    attrs, ancestors = rows[0]
    assert any(tag == "aside" for tag, _ in ancestors), "advanced control still floats over the composer"
    assert any("sb-foot" in a.get("class", "").split() for _, a in ancestors)
    assert attrs.get("type") == "button"
    assert "chip" in attrs.get("class", "").split()
    assert "adv-toggle" not in attrs.get("class", "").split()


def test_primary_send_remains_in_the_composer_with_its_existing_identity():
    rows = controls()["send"]
    assert len(rows) == 1
    _, ancestors = rows[0]
    assert any("comp-tools" in a.get("class", "").split() for _, a in ancestors)
    assert not any(tag == "aside" for tag, _ in ancestors)
