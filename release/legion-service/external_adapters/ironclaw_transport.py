#!/usr/bin/env python3
"""IronClaw(Reborn) 原生 `run -m` 单发 worker。

每轮 home 始终是作用域临时目录；只有调用方未提供持久 workdir 时才临时创建 workspace。
两个目录都由 TemporaryDirectory 保证正常、异常和超时路径自动回收。
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile
from contextlib import contextmanager
import http.server
import secrets
import threading
import socket
import urllib.request
import urllib.error
from urllib.parse import urlsplit


@contextmanager
def endpoint(gateway, appid, timeout_s):
    url = urlsplit(gateway)
    if url.hostname != "aigc.sankuai.com":
        yield gateway, appid
        return
    if url.scheme != "https" or url.username or url.password or url.query or url.fragment:
        raise ValueError("invalid configured gateway URL")
    token = secrets.token_urlsafe(32)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    connections = set()
    connection_lock = threading.Lock()
    stopping = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(min(timeout_s, 10))
            with connection_lock:
                connections.add(self.request)
            super().setup()

        def finish(self):
            try:
                super().finish()
            finally:
                with connection_lock:
                    connections.discard(self.request)

        def log_message(self, *args):
            pass

        def do_POST(self):
            self.connection.settimeout(min(timeout_s, 60))
            if not secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.send_error(401)
                return
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if length < 0 or length > 16 * 1024 * 1024 or self.headers.get("Transfer-Encoding"):
                    self.send_error(413)
                    return
                body = self.rfile.read(length)
                if len(body) != length:
                    self.send_error(400)
                    return
                request = urllib.request.Request(gateway.rstrip("/") + "/chat/completions", data=body,
                    headers={"Authorization": "Bearer " + appid, "Content-Type": "application/json", "Accept-Encoding": "identity"})
                try:
                    response = opener.open(request, timeout=min(timeout_s, 60))
                except urllib.error.HTTPError as error:
                    response = error
                upstream_socket = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                with connection_lock:
                    if upstream_socket is not None:
                        connections.add(upstream_socket)
                timer = None
                if upstream_socket is not None:
                    def close_upstream():
                        try:
                            upstream_socket.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                    timer = threading.Timer(min(timeout_s, 60), close_upstream)
                    timer.daemon = True
                    timer.start()
                with response:
                    try:
                        if stopping.is_set():
                            raise OSError("task ended")
                        data = response.read(32 * 1024 * 1024 + 1)
                    finally:
                        if timer:
                            timer.cancel()
                        with connection_lock:
                            connections.discard(upstream_socket)
                    if len(data) > 32 * 1024 * 1024:
                        raise ValueError("gateway response exceeds transport buffer")
                    # This native provider uses buffered Chat Completions; reject
                    # incomplete JSON rather than converting truncated TLS data to success.
                    if 200 <= response.code < 300:
                        json.loads(data)
                    self.send_response(response.code)
                    self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
            except (OSError, ValueError):
                try:
                    self.send_error(502, "upstream transport failed")
                except OSError:
                    pass

    class Server(http.server.ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            # Local disconnects during task cleanup must not dump private context.
            pass

    server = Server(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .1}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", token
    finally:
        stopping.set()
        server.shutdown()
        with connection_lock:
            active = list(connections)
        for connection in active:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    import sys
    settings = json.loads(sys.stdin.readline())
    with endpoint(settings["gateway"], settings["appid"], settings["timeout"]) as result:
        print(json.dumps(result), flush=True)
        # Parent closes stdin on completion. Parent also owns a process-group kill
        # deadline, so blocked upstream threads cannot outlive this task.
        sys.stdin.read()
