"""Real Caddy HTTP/WS traffic with fake identities; no cloud, model or browser."""
import base64
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest

DEPLOY = Path(__file__).resolve().parents[3] / "deploy/linux"


def test_alias_config_directory_is_readable_despite_private_install_umask(tmp_path):
    lines = (DEPLOY / "bootstrap.sh").read_text().splitlines()
    prepare = next((line for line in lines
                    if line.startswith("install -d -m 0755 ")
                    and "/etc/meyo-legion/aliases" in line), None)
    assert prepare is not None, "root 可读不代表代理账号能展开别名配置目录"
    root = tmp_path / "etc-meyo-legion"
    subprocess.run(["/bin/sh", "-c", "umask 077\n" +
                    prepare.replace("/etc/meyo-legion", str(root))],
                   check=True, capture_output=True, timeout=5)
    assert (root / "aliases").stat().st_mode & 0o777 == 0o755


def test_desktop_http_and_websocket_share_the_owner_gate_and_strip_credentials(tmp_path):
    binary = os.environ.get("MEYO_TEST_CADDY")
    if not binary:
        pytest.skip("Use the existing checksum-verified Caddy binary")
    observed = {"auth": [], "desktop": []}

    class Auth(BaseHTTPRequestHandler):
        def do_GET(self):
            observed["auth"].append(self.path)
            cookie = self.headers.get("Cookie", "")
            status = 200 if cookie == "session=owner-fixture" else (
                403 if cookie == "session=other-fixture" else 401)
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args):
            pass

    class Desktop(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            observed["desktop"].append((self.path, dict(self.headers)))
            if self.headers.get("Upgrade", "").lower() == "websocket":
                digest = hashlib.sha1(
                    (self.headers["Sec-WebSocket-Key"] +
                     "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", base64.b64encode(digest).decode())
                self.end_headers()
                self.wfile.write(b"\x81\x07DESKTOP")
                self.wfile.flush()
                self.close_connection = True
            else:
                content = b"desktop-client-fixture"
                self.send_response(200)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

        def log_message(self, *_args):
            pass

    auth = ThreadingHTTPServer(("127.0.0.1", 0), Auth)
    desktop = ThreadingHTTPServer(("127.0.0.1", 0), Desktop)
    threads = []
    for server in (auth, desktop):
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        threads.append(thread)
    with socket.socket() as port_probe:
        port_probe.bind(("127.0.0.1", 0))
        port = port_probe.getsockname()[1]
    config = tmp_path / "Caddyfile"
    config.write_text(
        "{\nadmin off\nauto_https off\n}\n"
        f"http://127.0.0.1:{port} {{\n"
        f"import {DEPLOY / 'desktop.caddy'}\n"
        'handle {\nrespond "Not Found" 404\n}\n}\n'
    )
    env = {
        "PATH": os.defpath, "HOME": str(tmp_path), "GOMAXPROCS": "1",
        "XDG_DATA_HOME": str(tmp_path / "caddy-data"),
        "XDG_CONFIG_HOME": str(tmp_path / "caddy-config"),
        "MEYO_DESKTOP_PUBLIC_PATH": "/cloud-desktop",
        "MEYO_UPSTREAM": f"127.0.0.1:{auth.server_port}",
        "MEYO_DESKTOP_UPSTREAM": f"127.0.0.1:{desktop.server_port}",
    }
    process = None
    try:
        with (tmp_path / "caddy.log").open("wb") as log:
            process = subprocess.Popen(
                [binary, "run", "--config", str(config), "--adapter", "caddyfile"],
                env=env, stdout=log, stderr=log, start_new_session=True,
            )
            deadline = time.monotonic() + 8
            while True:
                if process.poll() is not None:
                    pytest.fail((tmp_path / "caddy.log").read_text())
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    if time.monotonic() > deadline:
                        pytest.fail("owned proxy did not listen")
                    time.sleep(0.05)

            for cookie, expected in (("", 401), ("session=other-fixture", 403),
                                     ("session=owner-fixture", 200)):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                connection.request("GET", "/cloud-desktop/vnc.html?autoconnect=true", headers={
                    "Cookie": cookie, "Authorization": "Bearer fixture-only",
                    "X-Legion-Ticket": "fixture-ticket", "X-Legion-Token": "fixture-token",
                })
                response = connection.getresponse()
                assert response.status == expected
                body = response.read()
                if expected == 200:
                    assert body == b"desktop-client-fixture"
                connection.close()

            assert len(observed["desktop"]) == 1
            path, headers = observed["desktop"][0]
            assert path == "/vnc.html?autoconnect=true"
            assert not any(k.lower() in ("cookie", "authorization", "x-legion-ticket",
                                         "x-legion-token") for k in headers)
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request(
                "GET", "/cloud-desktop/vnc.html?autoconnect=true&ticket=fixture-ticket"
                "&%74icket=encoded-fixture&path=cloud-desktop%2Fwebsockify",
                headers={"Cookie": "session=owner-fixture"},
            )
            response = connection.getresponse()
            assert response.status == 200
            response.read()
            connection.close()
            routed_query = parse_qs(urlsplit(observed["desktop"][-1][0]).query)
            assert routed_query == {"autoconnect": ["true"],
                                    "path": ["cloud-desktop/websockify"]}

            for cookie, expected in (("", 401), ("session=other-fixture", 403),
                                     ("session=owner-fixture", 101)):
                with socket.create_connection(("127.0.0.1", port), timeout=3) as ws:
                    ws.settimeout(3)
                    ws.sendall((
                        "GET /cloud-desktop/websockify HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{port}\r\n"
                        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                        "Sec-WebSocket-Version: 13\r\n"
                        "Sec-WebSocket-Key: Zml4dHVyZS13ZWJzb2NrZXQ=\r\n"
                        f"Cookie: {cookie}\r\n\r\n"
                    ).encode())
                    data = b""
                    while b"\r\n\r\n" not in data:
                        data += ws.recv(4096)
                    assert int(data.split(b" ", 2)[1]) == expected
                    if expected == 101:
                        while b"DESKTOP" not in data:
                            chunk = ws.recv(4096)
                            assert chunk, data
                            data += chunk
            assert len(observed["desktop"]) == 3
            assert observed["desktop"][-1][0] == "/websockify"
            assert observed["auth"] == ["/api/legion/cloud_desktop/display/auth"] * 7
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", "/websockify")
            assert connection.getresponse().status == 404
            connection.close()
            assert len(observed["desktop"]) == 3
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
        for server, thread in zip((auth, desktop), threads):
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)
