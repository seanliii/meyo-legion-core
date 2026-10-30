#!/usr/bin/env python3
"""Cline 原生 CLI 的薄接线：复用现有 transport、沙箱和进程注册表。"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))

from external_adapters import cline_native
from external_agent_transports import _supervise_cli_process
from native_process_sandbox import prepare_native_process


def literal_cli_spec(command, workdir):
    """One template expansion only; inserted native argv is never scanned again."""
    context = {"workdir": workdir, **{f"cline_arg_{i}": word for i, word in enumerate(command)}}
    return {"command": [f"{{cline_arg_{i}}}" for i in range(len(command))],
            "snapshot_terminal": True}, context


def safe_output(value, credential):
    if isinstance(value, str):
        return value.replace(credential, "[redacted]") if credential else value
    if isinstance(value, dict):
        return {k: safe_output(v, credential) for k, v in value.items()}
    if isinstance(value, list):
        return [safe_output(v, credential) for v in value]
    return value


def attach_launch_failure(result, records, returncode):
    """No native events means the command failed before a Cline task ran.

    This is an execution-boundary fact, not a guess based on model/provider
    names. Native API failures with task events retain their normal diagnosis.
    """
    if returncode and not records and result.get("error"):
        from error_envelope import normalize
        result["error_envelope"] = normalize(
            "harness_runtime", result["error"], fail_kind="harness_runtime").to_dict()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--runtime-context", required=True)
    parser.add_argument("task")
    args = parser.parse_args()
    context = json.loads(args.runtime_context)
    checkpoint = (context.get("task_memory") or {}).get("native_checkpoint") or {}
    work = Path.cwd().resolve()
    key = os.environ.get("MEYO_CLINE_API_KEY") or ""
    result = {"type": "cline_native_result", "status": "harness_runtime", "final": "",
              "native_checkpoint": checkpoint or None, "resumed_native": False,
              "model_used": None, "requested_model": args.model}
    started = time.monotonic()
    native_started = False
    os.umask(0o077)
    try:
        package = args.runtime_dir.resolve(strict=True) / "node_modules/cline"
        if json.loads((package / "package.json").read_text()).get("version") != cline_native.VERSION:
            raise ValueError("Cline runtime version does not match the registered protocol")
        binary = package / "dist/cli.mjs"
        if not binary.is_file():
            raise ValueError("Cline installed native CLI is missing")
        home = cline_native.session_home(work, context, checkpoint)
        lock_path = home / ".meyo-config.lock"
        if lock_path.is_symlink():
            raise ValueError("Cline private lock must not be a symlink")
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            cline_native.configure(home, model=args.model, appid=key, gateway=args.gateway,
                                   session_id=checkpoint.get("session_id") or "")
            old_ts = []
            if checkpoint:
                _, messages, _ = cline_native.task_files(home, checkpoint["session_id"])
                old_ts = [row["ts"] for row in messages if isinstance(row, dict) and "ts" in row]
            native_home = home / "home"
            native_home.mkdir(exist_ok=True, mode=0o700)
            env = dict(os.environ)
            env.pop("MEYO_CLINE_API_KEY", None)
            env.update(HOME=str(native_home), CLINE_DIR=str(home),
                       CLINE_DATA_DIR=str(home / "data"), CLINE_LOG_DIR=str(home / "data/logs"),
                       CLINE_NO_AUTO_UPDATE="1")
            command = [str(binary), "--config", str(home), "--json", "--act", "--yolo",
                       "--cwd", str(work), "--model", args.model]
            if checkpoint:
                command += ["--taskId", checkpoint["session_id"]]
            command += ["--", args.task]
            # The shared execution boundary confines writes; Cline still performs
            # its own planning/tools. Missing backend never retries unsandboxed.
            protected = [env.get("MEYO_RUNTIME_ROOT"), str(args.runtime_dir), str(args.source), str(SERVICE)]
            with prepare_native_process(
                    env, str(work), backend="codex",
                    protected_roots=[p for p in protected if p],
                    writable_state_roots=[str(home)]) as launch:
                started_ms = time.time_ns() // 1_000_000
                last_activity = [0.0]
                received_ids = set()

                def event_cb(event):
                    sid = event.get("native_session_id")
                    if event.get("kind") == "task_started" and sid and sid not in received_ids:
                        cline_native._task_id(sid)
                        if not checkpoint or checkpoint.get("session_id") == sid:
                            received_ids.add(sid)
                            early = {
                                "type": "cline_native_checkpoint",
                                "native_checkpoint": {
                                    "kind": "cline_task", "session_id": sid,
                                    "config_dir": str(home), "workdir": str(work),
                                    "user_id": str(context.get("user_id") or "default")},
                                "resumed_native": bool(checkpoint),
                            }
                            print(json.dumps(early, ensure_ascii=False), flush=True)
                    # Raw Cline say/task/reasoning/api_req text can contain private
                    # context. Retain it in the existing private terminal log.
                    now = time.monotonic()
                    if now - last_activity[0] >= 1:
                        last_activity[0] = now
                        print(json.dumps({"type": "cline_activity",
                                          "message": "Cline 原生执行有新输出"}, ensure_ascii=False), flush=True)

                native_spec, native_context = literal_cli_spec(command, str(work))
                native_started = True
                native = _supervise_cli_process(
                    command, native_spec, native_context, env, 0,
                    event_cb=event_cb, launch=launch)
                terminal = native.get("terminal_snapshot") or {}
                stdout_ref = (terminal.get("streams") or {}).get("stdout") or {}
                records = []
                if stdout_ref.get("path"):
                    with Path(stdout_ref["path"]).open(encoding="utf-8") as stream:
                        for line in stream:
                            try:
                                row = json.loads(line)
                                if isinstance(row, dict):
                                    records.append(row)
                            except ValueError:
                                pass
                result = cline_native.result_from_native(
                    records, native["returncode"], home, work, context, checkpoint,
                    started_ms=started_ms, requested_model=args.model, old_message_ts=old_ts)
                result["native_facts"]["terminal_log"] = terminal
                if result["status"] != "ok" and native.get("error"):
                    result["error"] = (str(result.get("error") or "") + "; " + native["error"])[-4000:]
                attach_launch_failure(result, records, native["returncode"])
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        result["error"] = str(exc)[-4000:]
        envelope = getattr(exc, "error_envelope", None)
        if isinstance(envelope, dict):
            result["error_envelope"] = dict(envelope)
        elif not native_started:
            attach_launch_failure(result, [], 1)
    result["elapsed"] = round(time.monotonic() - started, 3)
    print(json.dumps(safe_output(result, key), ensure_ascii=False), flush=True)
    return 0 if result["status"] == "ok" else 1


def _cancel(signum, _frame):
    # Unwind native supervision and the one owned boundary on normal cancellation.
    raise SystemExit(128 + signum)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _cancel)
    signal.signal(signal.SIGINT, _cancel)
    raise SystemExit(main())
