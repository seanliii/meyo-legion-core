"""Real macOS kernel-boundary regression; no model calls or user credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SERVICE = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for key in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "LEGION_PROC_REGISTRY_DIR"):
        monkeypatch.setenv(key, str(tmp_path / "work" / key.lower()))


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS libproc and Seatbelt")
def test_identity_cpu_and_owned_cleanup_work_inside_unchanged_file_boundary(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    private = tmp_path / "private.txt"
    private.write_text("a generated private fixture, not user data")
    profile = tmp_path / "outer.sb"
    profile.write_text(
        '(version 1)\n(allow default)\n'
        '(deny file-read* (subpath "/Users") (subpath "/private/tmp") '
        '(subpath "/private/var/folders") (subpath "/Volumes"))\n'
        '(allow file-read-metadata)\n(deny file-write*)\n'
        '(deny signal)\n(allow signal (target same-sandbox))\n'
        f'(allow file-read* (subpath {json.dumps(str(SERVICE))}) '
        f'(subpath {json.dumps(str(work))}))\n'
        f'(allow file-write* (subpath {json.dumps(str(work))}) (literal "/dev/null"))\n'
    )
    payload = r'''
import json,os,signal,subprocess,sys,time
from pathlib import Path
sys.path.insert(0,SERVICE_PATH)
import legion_proc_registry as reg
birth={}
comms=reg._comm_of_many([os.getpid()],start_times=birth)
facts={"own_identity_known":bool(comms.get(os.getpid()) and birth.get(os.getpid()))}
cpu=getattr(reg,"process_group_cpu_seconds",lambda _: -1)
before=cpu(os.getpgrp())
start=time.monotonic()
while time.monotonic()-start < .10: pass
after=cpu(os.getpgrp())
facts["cpu_progress_observed"]=before>=0 and after-before > .02
child_file=Path("grandchild.json")
code=("import subprocess,sys,time,json;from pathlib import Path;"
      "p=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(12)'],"
      "start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
      "stderr=subprocess.DEVNULL);"
      "Path('grandchild.json').write_text(json.dumps({'pid':p.pid}));time.sleep(12)")
parent=subprocess.Popen([sys.executable,"-I","-B","-c",code],start_new_session=True,
                        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
grand=None
try:
    scope=reg.NativeProcessScope(parent.pid,owner=os.getpid())
    until=time.monotonic()+5
    while not child_file.exists() and time.monotonic()<until: time.sleep(.01)
    grand=json.loads(child_file.read_text())["pid"]
    scope.observe(force=True)
    record=reg._read_entry(reg._entry_path(grand))
    facts["grandchild_identity_known"]=bool(record and record.get("start_time"))
    os.killpg(parent.pid,signal.SIGTERM);parent.wait(timeout=3)
    scope.cleanup()
    until=time.monotonic()+2
    while time.monotonic()<until:
        try: os.kill(grand,0)
        except ProcessLookupError: break
        time.sleep(.01)
    try: os.kill(grand,0);facts["owned_grandchild_stopped"]=False
    except ProcessLookupError: facts["owned_grandchild_stopped"]=True
    try: Path(PRIVATE_PATH).read_text();facts["private_read_denied"]=False
    except PermissionError: facts["private_read_denied"]=True
finally:
    for pid in (parent.pid,grand):
        if pid:
            try:
                if os.getpgid(pid)==pid: os.killpg(pid,signal.SIGKILL)
            except ProcessLookupError: pass
    parent.wait(timeout=3)
print(json.dumps(facts))
'''.replace("SERVICE_PATH", repr(str(SERVICE))).replace("PRIVATE_PATH", repr(str(private)))
    # This kernel/stdlib test intentionally denies the rest of /Users. A venv's
    # site bootstrap would read its unrelated pyvenv.cfg there before the test
    # starts. Use the same Python installation's base interpreter, without
    # expanding the file boundary or borrowing a developer site-packages tree.
    interpreter = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    result = subprocess.run(
        ["/usr/bin/sandbox-exec", "-f", str(profile), interpreter, "-I", "-B", "-c", payload],
        cwd=work, capture_output=True, text=True, timeout=20, start_new_session=True)
    assert result.returncode == 0, result.stderr
    facts = json.loads(result.stdout)
    assert all(facts.values()), facts
