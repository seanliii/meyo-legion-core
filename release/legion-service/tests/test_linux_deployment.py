"""Offline deployment inputs and the real proxy; no models or cloud mutations."""
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


ROOT = Path(__file__).resolve().parents[3]
DEPLOY = ROOT / "deploy/linux"


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    for name in ("MEYO_LEGION_DIR", "LEGION_HOME_DIR"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))


def archive(path, members):
    with tarfile.open(path, "w:gz") as output:
        for name, body in members.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(body)
            output.addfile(entry, io.BytesIO(body))


def payload(tmp_path):
    bundle = tmp_path / "payload"
    bundle.mkdir()
    archive(bundle / "app.tar.gz", {
        "app/meyo.py": b"# fixture, never executed\n",
        "app/release/legion-service/requirements-core.txt": b"",
    })
    archive(bundle / "codex.tar.gz", {
        "codex-x86_64-unknown-linux-musl": b"offline fixture",
    })
    archive(bundle / "caddy.tar.gz", {"caddy": b"offline fixture"})
    (bundle / "SOURCE_REVISION").write_text("a" * 40 + "\n")
    (bundle / "PUBLIC_ORIGIN").write_text("https://198.51.100.21\n")
    checksums(bundle)
    return bundle


def checksums(bundle):
    (bundle / "SHA256SUMS").write_text("".join(
        f"{hashlib.sha256((bundle / name).read_bytes()).hexdigest()}  {name}\n"
        for name in ("app.tar.gz", "codex.tar.gz", "caddy.tar.gz")
    ))


def check(bundle):
    return subprocess.run(
        ["bash", str(DEPLOY / "bootstrap.sh"), "--check", str(bundle)],
        text=True, capture_output=True, timeout=15,
    )


def test_check_accepts_fixed_offline_inputs_without_installing(tmp_path):
    bundle = payload(tmp_path)
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    result = check(bundle)
    assert result.returncode == 0, result.stderr
    assert "未安装" in result.stdout
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before


@pytest.mark.parametrize("failure", [
    "hash", "traversal", "symlink", "origin_query", "revision", "extra_checksum",
])
def test_check_rejects_untrusted_inputs_before_installation(tmp_path, failure):
    bundle = payload(tmp_path)
    if failure == "hash":
        (bundle / "app.tar.gz").write_bytes(b"changed")
    elif failure == "traversal":
        archive(bundle / "caddy.tar.gz", {"../../escape": b"bad", "caddy": b"x"})
        checksums(bundle)
    elif failure == "symlink":
        with tarfile.open(bundle / "caddy.tar.gz", "w:gz") as output:
            entry = tarfile.TarInfo("caddy")
            entry.type = tarfile.SYMTYPE
            entry.linkname = "/etc/passwd"
            output.addfile(entry)
        checksums(bundle)
    elif failure == "origin_query":
        (bundle / "PUBLIC_ORIGIN").write_text("https://198.51.100.21/?token=x")
    elif failure == "revision":
        (bundle / "SOURCE_REVISION").write_text("../../other")
    else:
        with (bundle / "SHA256SUMS").open("a") as output:
            output.write("a" * 64 + "  /etc/passwd\n")
    result = check(bundle)
    assert result.returncode != 0
    assert "输入不可用" in result.stderr
    assert not (tmp_path.parent / "escape").exists()


def test_checks_cannot_be_removed_by_python_optimization(tmp_path, monkeypatch):
    bundle = payload(tmp_path)
    # Force -O on the interpreter itself, not only through an environment flag.
    wrappers = tmp_path / "bin"
    wrappers.mkdir()
    python = wrappers / "python3"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" -O "$@"\n')
    python.chmod(0o700)
    monkeypatch.setenv("PATH", str(wrappers) + os.pathsep + os.environ["PATH"])
    (bundle / "SHA256SUMS").write_text("".join(
        "0" * 64 + "  " + name + "\n"
        for name in ("app.tar.gz", "codex.tar.gz", "caddy.tar.gz")
    ))
    result = check(bundle)
    assert result.returncode != 0, "optimized Python accepted false checksums"
    assert "输入不可用" in result.stderr


def test_publish_software_permissions_keeps_instance_and_symlink_target_private(tmp_path):
    release, venv, state = (tmp_path / name for name in ("release", "venv", "state"))
    previous_umask = os.umask(0o077)
    try:
        release.mkdir()
        state.mkdir()
        (release / "meyo.py").write_text("# program\n")
        (state / "login-token.txt").write_text("offline fixture")
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(venv)],
            check=True, capture_output=True, timeout=20,
        )
    finally:
        os.umask(previous_umask)
    assert stat.S_IMODE(release.stat().st_mode) == 0o700
    assert stat.S_IMODE(venv.stat().st_mode) == 0o700
    private_target = state / "login-token.txt"
    (venv / "outside-link").symlink_to(private_target)
    source = (DEPLOY / "bootstrap.sh").read_text()
    helper = re.search(r"(?ms)^make_software_readable\(\) \{.*?^\}", source)
    assert helper, "no software-only permission publishing step for the private umask"
    # Execute the exact production helper, not the privileged installer.
    subprocess.run(
        ["bash", "-c", helper.group() + '\nmake_software_readable "$1" "$2"',
         "permission-unit", str(release), str(venv)],
        check=True, capture_output=True, timeout=15,
    )
    assert 'make_software_readable "$ROOT/releases/$REVISION" "$ROOT/venv"' in source
    for software in (release, venv):
        for path in [software, *software.rglob("*")]:
            if path.is_symlink():
                continue
            mode = stat.S_IMODE(path.stat().st_mode)
            assert not (mode & 0o022), (str(path), oct(mode))
            required = 0o005 if path.is_dir() else 0o004
            assert mode & required == required, (str(path), oct(mode))
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE(private_target.stat().st_mode) == 0o600


def test_units_keep_the_existing_entry_and_do_not_disable_native_sandbox():
    service = (DEPLOY / "meyo-legion.service").read_text()
    assert "meyo.py serve --state-dir /var/lib/meyo-legion" in service
    assert "User=meyo-legion" in service
    assert "UMask=0077" in service
    assert "PrivateNetwork=" not in service
    assert "RestrictNamespaces=" not in service
    assert "NoNewPrivileges=" not in service
    assert "server.py" not in service
    proxy = (DEPLOY / "meyo-proxy.service").read_text()
    assert "User=meyo-proxy" in proxy
    assert "meyo-legion.service" in proxy


def test_real_proxy_host_streaming_and_access_log_redaction(tmp_path):
    binary = os.environ.get("MEYO_TEST_CADDY")
    if not binary:
        pytest.skip("Set MEYO_TEST_CADDY to the checksum-verified Caddy executable")
    observations = []
    release_second_event = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            observations.append({"get_path": self.path})
            is_share = self.path.startswith("/shared/sample-sid/PATH_SECRET/file.txt")
            self.send_response(200 if is_share else 418)
            self.end_headers()
            if is_share:
                self.wfile.write(b"shared fixture bytes")

        def do_POST(self):
            observations.append({
                "host": self.headers.get("Host"),
                "origin": self.headers.get("Origin"),
                "authorization": self.headers.get("Authorization"),
                "body": self.rfile.read(int(self.headers.get("Content-Length", "0"))),
            })
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Set-Cookie", "session=RESPONSE_SECRET; HttpOnly")
            self.end_headers()
            self.wfile.write(b"data: first\n\n")
            self.wfile.flush()
            release_second_event.wait(5)
            self.wfile.write(b"data: final\n\n")
            self.wfile.flush()

        def log_message(self, *args):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    access = tmp_path / "access.json"
    env = {
        **{k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL") if k in os.environ},
        "GOMAXPROCS": "1", "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path / "caddy-data"),
        "XDG_CONFIG_HOME": str(tmp_path / "caddy-config"),
        "MEYO_PUBLIC_HOST": "198.51.100.21",
        "MEYO_UPSTREAM": f"127.0.0.1:{upstream.server_port}",
        "MEYO_ACCESS_LOG": str(access),
    }
    process = None
    client = None
    try:
        adapted = subprocess.run(
            [binary, "adapt", "--config", str(DEPLOY / "Caddyfile")],
            env=env, text=True, capture_output=True, timeout=15, check=True,
        )
        config = json.loads(adapted.stdout)
        # Only change the test transport: no ACME, no TLS and no production port.
        server = next(iter(config["apps"]["http"]["servers"].values()))
        server["listen"] = ["127.0.0.1:0"]
        server["automatic_https"] = {"disable": True}
        server.pop("tls_connection_policies", None)
        config["apps"].pop("tls", None)
        # A bound test socket chooses an unused port, then Caddy must bind it.
        import socket
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server["listen"] = [f"127.0.0.1:{port}"]
        config_file = tmp_path / "proxy.json"
        config_file.write_text(json.dumps(config))
        with (tmp_path / "proxy.log").open("wb") as log:
            process = subprocess.Popen(
                [binary, "run", "--config", str(config_file)],
                env=env, stdout=log, stderr=log, start_new_session=True,
            )
            deadline = time.monotonic() + 10
            while True:
                if process.poll() is not None:
                    pytest.fail((tmp_path / "proxy.log").read_text())
                client = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                try:
                    client.request("GET", "/?ticket=WRONG_HOST_QUERY", headers={
                        "Host": "localhost", "Authorization": "Bearer WRONG_HOST_HEADER",
                    })
                    response = client.getresponse()
                    assert response.status == 404
                    response.read()
                    client.close()
                    break
                except ConnectionRefusedError:
                    client.close()
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
            assert observations == []
            client = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
            client.request(
                "POST", "/api/legion/dispatch?ticket=QUERY_SECRET",
                body=b"CHAT_BODY_SECRET",
                headers={
                    "Host": "198.51.100.21", "Origin": "https://198.51.100.21",
                    "Authorization": "Bearer HEADER_SECRET",
                    "Cookie": "session=COOKIE_SECRET",
                },
            )
            response = client.getresponse()
            assert response.status == 200
            assert response.readline() == b"data: first\n"
            assert not release_second_event.is_set()
            release_second_event.set()
            assert b"data: final" in response.read()
            client.close()
            assert observations == [{
                "host": "198.51.100.21", "origin": "https://198.51.100.21",
                "authorization": "Bearer HEADER_SECRET", "body": b"CHAT_BODY_SECRET",
            }]
            shared_path = "/shared/sample-sid/PATH_SECRET/file.txt?ticket=SHARE_QUERY_SECRET"
            client = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
            client.request("GET", shared_path, headers={"Host": "198.51.100.21"})
            shared_response = client.getresponse()
            assert shared_response.status == 200
            assert shared_response.read() == b"shared fixture bytes"
            client.close()
            assert observations[-1] == {"get_path": shared_path}
            process.terminate()
            process.wait(timeout=5)
        text = access.read_text()
        assert "/api/legion/dispatch" in text
        for secret in ("QUERY_SECRET", "HEADER_SECRET", "COOKIE_SECRET",
                       "CHAT_BODY_SECRET", "RESPONSE_SECRET",
                       "WRONG_HOST_QUERY", "WRONG_HOST_HEADER",
                       "PATH_SECRET", "SHARE_QUERY_SECRET"):
            assert secret not in text
    finally:
        release_second_event.set()
        if client:
            client.close()
        if process and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=3)
