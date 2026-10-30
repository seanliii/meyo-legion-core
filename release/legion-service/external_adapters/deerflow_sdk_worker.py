#!/usr/bin/env python3
"""DeerFlow embedded-client JSON-RPC worker for the legion transport."""
from __future__ import annotations

import json
import os
import pathlib
import sys
import hashlib
import tempfile
import shutil


def _publish_outputs(native_root, workdir, session_id):
    """Expose only this native thread's outputs, never its internal session files."""
    native_root = pathlib.Path(native_root)
    workdir = pathlib.Path(workdir).resolve()
    if not native_root.exists():
        return []
    if native_root.is_symlink() or not native_root.resolve().is_relative_to(workdir):
        raise ValueError("DeerFlow output root escapes task directory")
    for parent in native_root.parents:
        if parent == workdir:
            break
        if parent.is_symlink():
            raise ValueError("DeerFlow native output ancestor is a symlink")
    key = hashlib.sha256(str(session_id).encode()).hexdigest()[:16]
    destination = workdir / ("deerflow-output-" + key)
    published = []
    for directory, dirs, files in os.walk(native_root, followlinks=False):
        dirs[:] = [name for name in dirs if not pathlib.Path(directory, name).is_symlink()]
        for name in files:
            source = pathlib.Path(directory, name)
            if source.is_symlink() or not source.is_file():
                continue
            if not source.resolve().is_relative_to(native_root.resolve()):
                raise ValueError("DeerFlow output file escapes native root")
            target = destination / source.relative_to(native_root)
            for parent in [target, *target.parents]:
                if parent == workdir:
                    break
                if parent.is_symlink():
                    raise ValueError("DeerFlow delivery path is a symlink")
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                temporary = pathlib.Path(stream.name)
                with source.open("rb") as content:
                    shutil.copyfileobj(content, stream)
            try:
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            published.append(str(target))
    return published


def _session_config(params):
    import yaml
    workdir = pathlib.Path(params.get("workdir") or os.getcwd()).resolve()
    if not workdir.is_dir():
        raise ValueError("DeerFlow task workdir does not exist")
    session_id = str(params.get("session_id") or "external")
    key = hashlib.sha256(session_id.encode()).hexdigest()
    state = workdir / ".sessions" / "deerflow" / key
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not state.resolve().is_relative_to(workdir):
        raise ValueError("DeerFlow session state escapes task directory")
    config = yaml.safe_load(pathlib.Path(__file__).with_name("deerflow_config.yaml").read_text())
    # Keep the pinned upstream tool surface; a sandbox provider alone does not
    # register bash/file/web tools, including tools inherited by subagents.
    source = pathlib.Path(params.get("source") or "")
    upstream = yaml.safe_load((source / "config.example.yaml").read_text())
    for field in ("tools", "tool_groups"):
        config[field] = upstream[field]
    config["checkpointer"] = {"type": "sqlite", "connection_string": str(state / "checkpoints.sqlite")}
    config["sandbox"].setdefault("mounts", []).append({
        "host_path": str(workdir), "container_path": "/mnt/meyo-project", "read_only": False})
    target = state / "config.yaml"
    with tempfile.NamedTemporaryFile(mode="w", dir=state, delete=False) as stream:
        yaml.safe_dump(config, stream, allow_unicode=True)
        temporary = pathlib.Path(stream.name)
    temporary.replace(target)
    os.environ["DEER_FLOW_HOME"] = str(state / "home")
    return target, session_id


def _collect_chat(client, task: str, session_id: str) -> tuple[str, str]:
    """保留 DeerFlow stream 的结构化错误元数据，避免 chat() 把 fallback 文案当答案。"""
    chunks: dict[str, list[str]] = {}
    metadata: dict[str, dict] = {}
    last_id = ""
    for event in client.stream(task, thread_id=session_id):
        if getattr(event, "type", "") != "messages-tuple":
            continue
        data = getattr(event, "data", {}) or {}
        if data.get("type") != "ai":
            continue
        msg_id = str(data.get("id") or "")
        if not msg_id:
            continue
        last_id = msg_id
        if data.get("content"):
            chunks.setdefault(msg_id, []).append(str(data["content"]))
        if isinstance(data.get("additional_kwargs"), dict):
            metadata.setdefault(msg_id, {}).update(data["additional_kwargs"])
    final = "".join(chunks.get(last_id, ()))
    meta = metadata.get(last_id, {})
    if meta.get("deerflow_error_fallback"):
        reason = str(meta.get("error_reason") or "provider_error")
        detail = str(meta.get("error_detail") or final or "DeerFlow provider failed")
        return "", f"deerflow provider fallback:{reason}:{detail}"
    return final, ""


def main() -> int:
    request = json.loads(sys.stdin.readline() or "{}")
    params = request.get("params") or {}
    source = pathlib.Path(params.get("source") or "")
    backend = source / "backend" / "packages" / "harness"
    if not backend.is_dir():
        print(json.dumps({"jsonrpc": "2.0", "id": request.get("id"),
                          "error": {"message": f"DeerFlow harness package missing:{backend}"}}))
        return 2
    sys.path.insert(0, str(backend))
    for field, names in (("appid", ("OPENAI_API_KEY", "AIGC_APPID")),
                         ("gateway", ("OPENAI_BASE_URL",)), ("workdir", ("MEYO_WORKDIR",))):
        if params.get(field) is not None:
            for name in names:
                os.environ[name] = str(params[field])
    try:
        config_path, session_id = _session_config(params)
        os.environ["MEYO_UNIFIED_MODEL"] = str(params.get("model") or "gpt-5.6-sol")
        os.environ["DEER_FLOW_CONFIG_PATH"] = str(config_path)
        from deerflow.client import DeerFlowClient
        from deerflow.runtime.checkpointer.provider import checkpointer_context
        with checkpointer_context() as checkpointer:
            client = DeerFlowClient(config_path=str(config_path), checkpointer=checkpointer,
                                   model_name="meyo-unified", subagent_enabled=True,
                                   plan_mode=True, environment="meyo-legion")
            final, error = _collect_chat(client, str(params.get("task") or ""), session_id)
        from deerflow.config.paths import get_paths
        from deerflow.runtime.user_context import get_effective_user_id
        published = _publish_outputs(
            get_paths().sandbox_outputs_dir(session_id, user_id=get_effective_user_id()),
            params.get("workdir") or os.getcwd(), session_id)
        if error:
            print(json.dumps({"jsonrpc": "2.0", "id": request.get("id"),
                              "error": {"message": error}}, ensure_ascii=False))
            return 1
        payload = {"final": str(final), "session_id": session_id, "output_files": published,
                   "checkpoint": {"session_id": session_id}}
        print(json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "result": payload},
                         ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"jsonrpc": "2.0", "id": request.get("id"),
                          "error": {"message": f"{type(exc).__name__}:{exc}"}}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
