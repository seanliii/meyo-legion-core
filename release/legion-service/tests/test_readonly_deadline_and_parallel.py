"""Independent temp-only counterexamples; product sources remain untouched."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import pytest
from test_superagent_http_readonly import readonly_http, isolated, endpoint, OTHER_TOKEN

SERVICE=Path(__file__).resolve().parents[1]
KEY='dsh-superagent'

def configure(root, url):
    file=root/'external_agents/instances.json'
    file.parent.mkdir(parents=True)
    file.write_text(json.dumps({KEY:{'url':url}}))
    file.chmod(0o600)
    return file

def test_parallel_http_owner_and_other_user_do_not_share_namespace(readonly_http):
    scene=readonly_http
    def request(i):
        status,result=scene.request(**({'credential':OTHER_TOKEN} if i%2 else {}))
        return i,status,result
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows=list(pool.map(request,range(16)))
    for i,status,result in rows:
        assert status==200
        if i%2:
            assert result.get('code')=='instance_unconfigured', result
        else:
            assert result.get('ok') is True, result
    assert len(scene.endpoint['requests'])==8

def test_data_directory_symlink_is_refused_by_existing_configuration_validation(readonly_http):
    scene=readonly_http
    directory=scene.root/'data'
    original=scene.root/'data-original'
    directory.rename(original)
    directory.symlink_to(original,target_is_directory=True)
    status,result=scene.request()
    assert status==200 and result.get('code')=='instance_unconfigured',result
    assert scene.endpoint['requests']==[]

def test_fifo_instance_config_is_rejected_without_waiting_for_a_writer(tmp_path):
    root=tmp_path/'fifo-state'
    file=root/'external_agents/instances.json'
    file.parent.mkdir(parents=True)
    os.mkfifo(file,0o600)
    code="import json,external_agent_registry as r; print('entered',flush=True); print(json.dumps(r.run_capability('dsh-superagent','status',{},timeout_s=0.1,env={'MEYO_LEGION_DIR':"+repr(str(root))+"},isolated_environment=True)),flush=True)"
    started=time.monotonic()
    process=subprocess.Popen([sys.executable,'-B','-c',code],cwd=SERVICE,
                             env=dict(os.environ),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        stdout,stderr=process.communicate(timeout=0.7)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout,stderr=process.communicate(timeout=2)
        assert stdout.startswith('entered'),(stdout,stderr)
        pytest.fail(f'FIFO blocks before fstat: outer deadline killed child after {time.monotonic()-started:.3f}s; no HTTP request made')
    assert process.returncode==0,(stdout,stderr)
    assert json.loads(stdout.splitlines()[-1]).get('code')=='instance_config_invalid',stdout

@pytest.mark.parametrize("slow_phase", ["body", "headers"])
def test_slow_progress_body_obeys_total_read_timeout(tmp_path,monkeypatch,slow_phase):
    import external_agent_registry as registry
    monkeypatch.setattr(registry,'CATALOG',SERVICE/'external_agents.json')
    state={'requests':0}
    body=json.dumps({'paused':True}).encode()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state['requests']+=1
            try:
                if slow_phase == "headers":
                    stream = (b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
                              + b'Content-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
                else:
                    self.send_response(200)
                    self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length',str(len(body)))
                    self.end_headers()
                    stream = body
                for byte in stream:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(0.02)
            except (BrokenPipeError,ConnectionResetError):
                pass
        def log_message(self,*args):
            pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.daemon_threads=True
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':0.01},daemon=True)
    thread.start()
    root=tmp_path/'slow-body-state'
    configure(root,f'http://127.0.0.1:{server.server_port}')
    try:
        started=time.monotonic()
        result=registry.run_capability(KEY,'status',{},timeout_s=0.1,
                    env={'MEYO_LEGION_DIR':str(root)},isolated_environment=True)
        elapsed=time.monotonic()-started
        assert state['requests']==1
        assert result.get('status')=='timeout',f'timeout=0.1s, actual={elapsed:.3f}s, result={result}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()
