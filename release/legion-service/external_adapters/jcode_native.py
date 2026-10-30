"""JCode 原生 CLI 的运行位置、请求授权和会话事实；不实现它的解题循环。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil


def executable() -> str:
    """先用受管理依赖；本地开发仍可使用已有原生安装。"""
    import external_agent_registry as registry
    item = next((r for r in registry.load_catalog() if r.get("key") == "jcode-runtime"), None)
    if item:
        path = Path(registry._context(item)["runtime_dir"]) / "bin/jcode"
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    local = shutil.which("jcode") or str(Path.home() / ".local/bin/jcode")
    return local if Path(local).is_file() and os.access(local, os.X_OK) else ""


def prepare(ctx, checkpoint: dict) -> tuple[dict, Path, str]:
    """只改变本请求的 provider 绑定；不覆盖原生配置、MCP 或能力开关。"""
    if not isinstance(checkpoint, dict) or checkpoint.get("kind") not in (None, "jcode_session"):
        raise ValueError("JCode checkpoint belongs to a different native runtime")
    resume_id = str(checkpoint.get("session_id") or "")
    if resume_id and (Path(resume_id).name != resume_id or resume_id in (".", "..")):
        raise ValueError("JCode native session id is not a file name")
    env = dict(os.environ)
    # 当前用户的执行 Context 是目录权威；旧 checkpoint 也不能借恢复跳去其他用户目录。
    # 无用户目录的本地调用仍沿用原生 JCODE_HOME/当前用户 .jcode。
    location = ctx.memory_dir or env.get("JCODE_HOME") or str(Path.home() / ".jcode")
    home = Path(location).expanduser()
    if not home.is_absolute():
        raise ValueError("JCode state home must be absolute")
    home = home.resolve()
    if checkpoint.get("jcode_home") and Path(checkpoint["jcode_home"]).expanduser().resolve() != home:
        raise ValueError("JCode checkpoint home differs from the current user context")
    if resume_id:
        saved = home / "sessions" / (resume_id + ".json")
        if saved.is_symlink() or not saved.resolve().is_relative_to(home) or not saved.is_file():
            raise ValueError("JCode native session file is missing or unsafe; original checkpoint preserved")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    # named-profile lock 会遮住显式 API base；只清连接选择，不改工具/用户配置。
    for name in tuple(env):
        if name.startswith("JCODE_OPENROUTER_") or name in {
            "JCODE_PROVIDER_PROFILE_ACTIVE", "JCODE_PROVIDER_PROFILE_NAME",
            "JCODE_NAMED_PROVIDER_PROFILE",
        }:
            env.pop(name)
    env.update(
        JCODE_HOME=str(home),
        JCODE_NO_TELEMETRY="1",
        JCODE_OPENAI_COMPAT_API_BASE=(ctx.gateway or env.get("MEYO_UNIFIED_GATEWAY")
                                     or "https://aigc.sankuai.com/v1/openai/native"),
        JCODE_OPENAI_COMPAT_API_KEY_NAME="OPENAI_COMPAT_API_KEY",
        JCODE_OPENAI_COMPAT_DEFAULT_MODEL=ctx.model,
        OPENAI_COMPAT_API_KEY=(ctx.appid or env.get("MEYO_UNIFIED_APPID")
                              or env.get("AIGC_APPID") or env.get("OPENAI_COMPAT_API_KEY", "")),
    )
    return env, home, resume_id


def result_from_native(process, checkpoint: dict, home: Path, requested_model: str) -> dict:
    """Consume native start/done/error and the matching session; never infer an ID."""
    result = {"final": "", "status": f"rc{process.returncode}" if process.returncode else "harness_runtime",
              "model_used": None, "requested_model": requested_model, "usage": None,
              "native_checkpoint": checkpoint or None, "resumed_native": False}
    def text(value):
        return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
    stdout, stderr = text(process.stdout), text(process.stderr)
    try:
        try:
            whole = json.loads(stdout)
        except ValueError:
            whole = None
        legacy = isinstance(whole, dict) and not whole.get("type") and "text" in whole
        records = [whole] if isinstance(whole, dict) else []
        if not records:
            for line in stdout.splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    records.append(row)
        identities = [row for row in records
                      if legacy or row.get("type") in {"start", "session", "done", "error"}]
        ids = {row.get("session_id") for row in identities
               if isinstance(row.get("session_id"), str) and row["session_id"]}
        if len(ids) != 1:
            raise ValueError("JCode native output has no single received session identity")
        sid = ids.pop()
        if not isinstance(sid, str) or not sid or Path(sid).name != sid or sid in (".", ".."):
            raise ValueError("JCode native JSON report has no valid session id")
        previous_id = checkpoint.get("session_id")
        if previous_id and sid != previous_id:
            raise ValueError("JCode native session changed during resume")
        session_path = home / "sessions" / (sid + ".json")
        if (session_path.is_symlink() or not session_path.resolve().is_relative_to(home)
                or not session_path.is_file()):
            raise ValueError("JCode reported session has no native file")
        stored = json.loads(session_path.read_text())
        if not isinstance(stored, dict) or stored.get("id") != sid:
            raise ValueError("JCode native session file identity mismatch")
        result.update(
            native_checkpoint={"kind": "jcode_session", "session_id": sid,
                               "jcode_home": str(home), "session_path": str(session_path)},
            resumed_native=bool(previous_id),
        )
        terminal = [row for row in records if legacy or row.get("type") in {"done", "error"}]
        report = terminal[-1] if terminal else identities[-1]
        reported_model = report.get("model")
        result.update(model_used=reported_model if isinstance(reported_model, str) and reported_model else None,
                      native_provider=report.get("provider"),
                      native_facts={"protocol": "json" if legacy else "ndjson",
                                    "terminal_event": report.get("type") if terminal else None,
                                    "session_received": True})
        if process.returncode or report.get("type") == "error":
            result["error"] = text(report.get("message") or stderr or "JCode native command failed")[-1500:]
            return result
        if not terminal or not isinstance(report.get("text"), str):
            raise ValueError("JCode native output ended before a complete done report")
        result.update(final=report["text"], status="ok", usage=report.get("usage"))
    except (OSError, ValueError, TypeError) as exc:
        result["error"] = (stderr or str(exc))[-1500:]
    return result
