"""Real loopback HTTP: the raw Responses bridge must not invent decoder support."""
from contextlib import contextmanager
import gzip
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading

import pytest

from test_codex_output_budget_transport import runtime, MODEL, KEY


SSE = ('event: response.output_text.delta\ndata: ' + json.dumps({
    'type': 'response.output_text.delta', 'delta': '真实压缩边界'}, ensure_ascii=False)
    + '\n\nevent: response.completed\ndata: {"type":"response.completed",'
      '"response":{"id":"fixture","status":"completed","output":[]}}\n\n').encode()


@contextmanager
def encoded_provider():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers['Content-Length']))
            accepted = self.headers.get('Accept-Encoding')
            compressed = 'gzip' in (accepted or '') and 'gzip;q=0' not in (accepted or '')
            wire = gzip.compress(SSE, mtime=0) if compressed else SSE
            seen.append({'accept_encoding': accepted, 'body': raw, 'compressed': compressed})
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(wire)))
            if compressed:
                self.send_header('Content-Encoding', 'gzip')
            self.send_header('ETag', '"same-representation"')
            self.end_headers()
            # Deliberately split the gzip header / UTF-8 stream across writes.
            for part in (wire[:1], wire[1:7], wire[7:]):
                self.wfile.write(part)
                self.wfile.flush()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def raw_client(active, raw, accept_encoding):
    from urllib.parse import urlsplit
    target = urlsplit(active['client_base_url'])
    conn = http.client.HTTPConnection(target.hostname, target.port, timeout=10)
    try:
        # http.client normally adds identity; explicitly omit it for the native
        # client case. Read actual wire bytes without HTTPX's automatic decoder.
        conn.putrequest('POST', target.path + '/responses', skip_accept_encoding=True)
        conn.putheader('Authorization', 'Bearer ' + active['client_api_key'])
        conn.putheader('Content-Type', 'application/json')
        conn.putheader('Content-Length', str(len(raw)))
        if accept_encoding is not None:
            conn.putheader('Accept-Encoding', accept_encoding)
        conn.endheaders(raw)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


@pytest.mark.parametrize('accepted', [None, '', 'identity', 'gzip;q=0, identity'])
def test_no_native_gzip_support_does_not_advertise_httpx_decoders(runtime, accepted):
    with encoded_provider() as (base, seen):
        binding = {'model': MODEL, 'base_url': base, 'api_key': KEY, 'http_headers': {},
                   'stamp': {'sha256': runtime.mr.ccr_binding_key(MODEL, KEY, base)},
                   'image_file_cache': {'profile': 'deepseek-files-v1', 'ttl_seconds': 3600},
                   'image_file_cache_user': 'fixture', 'image_file_cache_workdir': str(runtime.work)}
        raw = json.dumps({'model': MODEL, 'stream': True, 'input': []}).encode()
        with runtime.owned.transparent_bridge(binding, dict(os.environ)) as active:
            status, headers, wire = raw_client(active, raw, accepted)
        # A native eventsource reader consumes bytes as UTF-8; a compressed wire
        # must fail here before the fix, even though a normal HTTPX test decodes it.
        decoded = wire.decode('utf-8')
        assert status == 200 and decoded == SSE.decode('utf-8')
        assert len(seen) == 1 and seen[0]['body'] == raw
        assert seen[0]['accept_encoding'] == ('identity' if accepted is None else accepted)
        assert not seen[0]['compressed']
        assert 'content-encoding' not in {k.lower() for k in headers}


def test_explicit_gzip_client_keeps_encoding_and_wire_bytes(runtime):
    with encoded_provider() as (base, seen):
        binding = {'model': MODEL, 'base_url': base, 'api_key': KEY, 'http_headers': {},
                   'stamp': {'sha256': runtime.mr.ccr_binding_key(MODEL, KEY, base)},
                   'max_output_tokens': 128}
        raw = json.dumps({'model': MODEL, 'stream': True, 'input': [], 'max_output_tokens': 128}).encode()
        with runtime.owned.transparent_bridge(binding, dict(os.environ)) as active:
            status, headers, wire = raw_client(active, raw, 'gzip')
        assert status == 200 and wire == gzip.compress(SSE, mtime=0)
        assert gzip.decompress(wire) == SSE
        headers = {k.lower(): v for k, v in headers.items()}
        assert headers['content-encoding'] == 'gzip'
        assert headers['etag'] == '"same-representation"'
        assert seen[0]['accept_encoding'] == 'gzip'


def test_files_rewrite_and_cache_restart_keep_native_encoding_contract(runtime):
    from test_responses_image_files import provider, owned_binding, payload, PNG
    with provider(sse_gzip_if_accepted=True) as (base, seen, *_):
        raw = json.dumps(payload(), ensure_ascii=False).encode()
        for _ in range(2):
            with runtime.owned.transparent_bridge(owned_binding(runtime, base), dict(os.environ)) as active:
                status, headers, wire = raw_client(active, raw, None)
            assert status == 200 and 'response.completed' in wire.decode('utf-8')
            assert 'content-encoding' not in {k.lower() for k in headers}
        uploads = [r for r in seen if r['method'] == 'POST' and r['path'] == '/v1/files']
        responses = [r for r in seen if r['path'] == '/v1/responses']
        assert len(uploads) == 1 and uploads[0]['parts']['file'] == PNG
        assert len(responses) == 2 and responses[0]['body'] == responses[1]['body']
        for row in responses:
            assert not row['response_compressed']
            assert {k.lower(): v for k, v in row['headers'].items()}['accept-encoding'] == 'identity'
            assert row['body']['input'][0]['content'][1]['file_id'] == 'file-api-1'
        assert json.loads(raw) == payload()


def test_real_native_direct_then_files_resume_with_compression_capable_provider(runtime):
    import copy
    from test_codex_output_budget_transport import NATIVE, declare, context
    from test_responses_image_files import provider, enable
    assert NATIVE.is_file(), '需要真实本地 CLI，不静默跳过'
    with provider(sse_gzip_if_accepted=True) as (base, seen, *_):
        declare(runtime, base=base, cap=None)
        first = runtime.hr._codex_adapter('__ut_encoding_direct', 'Acknowledge this fixture; no tools.',
                                         ctx=context(runtime, base=base))
        assert first['status'] == 'ok', first
        checkpoint = copy.deepcopy(first['native_checkpoint'])
        enable(runtime, base=base)
        resumed = runtime.hr._codex_adapter('__ut_encoding_files_resume', 'Acknowledge again; no tools.',
            ctx=context(runtime, base=base, task_memory={'native_checkpoint': checkpoint}))
        assert resumed['status'] == 'ok' and resumed['resumed_native'], resumed
        assert resumed['native_checkpoint'] == checkpoint == first['native_checkpoint']
        assert len(seen) == 2, '本地原生重放不应重试'
        assert all(not row['response_compressed'] for row in seen)
        assert {k.lower(): v for k, v in seen[1]['headers'].items()}['accept-encoding'] == 'identity'
