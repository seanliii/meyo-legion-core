#!/usr/bin/env python3
"""Prime 透明 CLI 引导器：只恢复原生身份/配置，再调用现有 CLI transport。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from external_agent_registry import _safe_key, _session_socket_dir
from external_adapters.prime_native import (
    REVISION, PrimeSessionError, checkpoint_details, prepare_resume, run_native,
    validate_native_runtime,
)
from external_adapters.prime_daemon import OwnedPrimeDaemon, PrimeDaemonError


def _write_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        name = stream.name
        os.chmod(name, 0o600)
        json.dump(value, stream, ensure_ascii=False)
    try:
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _native_tmpdir(socket_path: str) -> str:
    # 固定源码 daemon-socket.ts:279 / daemon-supervisor.ts:659：
    # os.tmpdir()/prime-agent-<uid>/worker-<descriptorKey12>-<workerId12>.sock。
    # 不复用调用方可能很长的 TMPDIR；沿已有 resolver 建独立、属主正确的 0700 目录。
    directory = _session_socket_dir(str(Path(socket_path).parent), "prime-intellect-tmp", socket_path)
    longest_worker = Path(directory) / f"prime-agent-{os.getuid()}" / ("worker-" + "0" * 12 + "-" + "0" * 12 + ".sock")
    if len(os.fsencode(longest_worker)) > 103:  # 同时容纳 macOS(104含NUL) 与 Linux(108含NUL)。
        raise PrimeSessionError("prime_private_tmpdir_too_long")
    return directory


def guide(args, event_cb=None) -> dict:
    runtime = json.loads(args.runtime_context)
    raw_checkpoint = (runtime.get("task_memory") or {}).get("native_checkpoint") or {}
    result = {"status": "harness_runtime", "final": "", "usage_records": [],
              "native_checkpoint": raw_checkpoint or None, "resumed_native": False}
    daemon = None
    try:
        # The new foreground spawn must have the same source identity check as
        # run_native, before executing any supplied CLI path.
        if (args.source_url.rstrip("/") != "https://github.com/PrimeIntellect-ai/prime-agent"
                or args.revision != REVISION):
            raise PrimeSessionError("prime_source_revision_mismatch")
        native_path, _ = prepare_resume(raw_checkpoint)
        checkpoint = checkpoint_details(raw_checkpoint)
        logical = (checkpoint.get("logical_session_id") or runtime.get("session_id")
                   or os.environ.get("MEYO_SESSION_ID"))
        if not logical:
            raise PrimeSessionError("prime_logical_session_missing")
        logical = _safe_key(str(logical))
        if not logical:
            raise PrimeSessionError("prime_logical_session_empty")
        command = list(args.native_command)
        if command and command[0] == "--":
            command.pop(0)
        cwd = command[command.index("--cwd") + 1]
        config = Path(checkpoint.get("config_dir") or
                      Path(args.session_root) / "prime-intellect" / logical / "config")
        if not config.is_absolute():
            raise PrimeSessionError("prime_config_path_not_absolute")
        # donor 由原有 registry config_template 生成；只把配置送到原会话，不改变安装流程。
        model_config = json.loads(Path(args.models_json).read_text())
        socket_path = checkpoint.get("daemon_socket")
        if not socket_path:
            socket_path = str(Path(_session_socket_dir(cwd, "prime-intellect", logical)) / "prime.sock")
        if not Path(socket_path).is_absolute() or Path(socket_path).name != "prime.sock":
            raise PrimeSessionError("prime_daemon_socket_invalid")
        native_tmpdir = _native_tmpdir(socket_path)
        socket_present = os.path.lexists(socket_path)
        command[command.index("--daemon-socket") + 1] = socket_path
        sessions = (Path(native_path).parent if native_path else
                    Path(args.session_root) / "prime-intellect" / logical / "sessions")
        command[command.index("--session-dir") + 1] = str(sessions)
        if native_path:
            index = command.index("--")
            command[index:index] = ["--resume", native_path]
        context = {
            "source": args.source, "node": command[0], "runtime_dir": args.runtime_dir,
            "runtime_python": args.runtime_python, "session_dir": args.session_root,
            "session_id": logical, "workdir": cwd, "prime_config_dir": str(config),
            "gateway": os.environ.get("MEYO_UNIFIED_GATEWAY") or os.environ.get("OPENAI_BASE_URL", ""),
            "appid": (os.environ.get("MEYO_UNIFIED_APPID") or os.environ.get("AIGC_APPID")
                      or os.environ.get("MEYO_PRIME_INTELLECT_API_KEY", "")),
            "model": command[command.index("--model") + 1],
            "runtime_context_json": args.runtime_context,
        }
        validate_native_runtime(command, context, model_config)
        native_env = {
            **os.environ, "TMPDIR": native_tmpdir, "TMP": native_tmpdir, "TEMP": native_tmpdir,
            "PRIME_AGENT_CODING_AGENT_DIR": str(config),
            "PRIME_AGENT_SESSION_DIR": str(sessions),
            "PRIME_AGENT_KERNEL_PYTHON": args.runtime_python,
            "PRIME_AGENT_KERNEL_VENV": str(Path(args.runtime_dir) / "venv"),
            "MEYO_PRIME_INTELLECT_API_KEY": context["appid"],
        }
        daemon = OwnedPrimeDaemon(command, args.source, native_env, config.parent / "native-logs")
        # The scope lock covers config, native execution and shutdown. An
        # occupied/unknown endpoint is not overwritten or adopted.
        with daemon:
            _write_json(config / "models.json", model_config)
            daemon.start()
            result = run_native(
                {"url": args.source_url, "revision": args.revision},
                {"type": "cli", "command": command, "resolved_argv": True},
                context, native_env, 0, event_cb=event_cb)
            cp = result.get("native_checkpoint") or {}
            if cp.get("session_path") and cp.get("daemon_socket"):
                # Persist the original identity before releasing this native slot.
                receipt = Path(cp["session_path"]).parent.parent / "session-artifacts" / cp["session_id"] / "meyo-prime-runtime.json"
                _write_json(receipt, cp)
        result["native_facts"] = {**(result.get("native_facts") or {}),
                                  "requested_tmpdir": native_tmpdir,
                                  "tmpdir_scope": "owned_supervisor_and_native_client",
                                  "daemon_socket_present_before_launch": socket_present,
                                  "existing_daemon_environment": "not_reused"}
    except (OSError, ValueError, TypeError, KeyError, IndexError, PrimeDaemonError) as exc:
        result.update(status="harness_runtime", final="", error=str(exc), resumed_native=False,
                      native_checkpoint=raw_checkpoint or None)
    finally:
        if daemon is not None:
            result["native_facts"] = {**(result.get("native_facts") or {}),
                                      "daemon_lifecycle": dict(daemon.facts)}
    return result


def envelope(result: dict) -> dict:
    cp = result.get("native_checkpoint") or {}
    rows = result.get("usage_records") or []
    usage = {key: sum(row.get(key, 0) or 0 for row in rows)
             for key in ("input", "output", "cacheRead", "cacheWrite", "totalTokens", "cost_usd")}
    usage.update(source="prime_message", model=next((r.get("model") for r in rows if r.get("model")), None))
    return {"type": "prime_native_result", "status": result["status"], "final": result["final"],
            "session_id": cp.get("session_path") or cp.get("session_id"),
            "usage": usage, "prime_native_result": result}


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    for name in ("runtime-context", "source", "source-url", "revision", "runtime-dir",
                 "runtime-python", "session-root", "models-json"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("native_command", nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def main():
    args = parse_args()
    def stop(signum, _frame):
        # Each owning scope unwinds its own processes. A blanket registry kill
        # here would override a daemon's explicit identity-mismatch/no-signal result.
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    def emit(event):
        print(json.dumps(event, ensure_ascii=False), flush=True)
    result = guide(args, event_cb=emit)
    emit(envelope(result))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
