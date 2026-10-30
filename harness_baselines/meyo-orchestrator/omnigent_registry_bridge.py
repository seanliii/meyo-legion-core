#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把军团 registry 投影成 Omnigent 可原生启动的 AgentSpec bundles。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import orchestrator

HERE = Path(__file__).resolve().parent
DEFAULT_OUTPUT = HERE / "generated" / "omnigent-agents"


def _safe_key(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value).strip("-")


def _yaml_scalar(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def agent_spec(row: dict[str, Any]) -> str:
    key = _safe_key(str(row["key"]))
    name = f"meyo-{key}"
    description = str(row.get("display_name") or key)
    return f"""spec_version: 1
name: {name}
description: {_yaml_scalar(description + '，由 Meyo 统一 registry 动态投影的军团成员。')}

executor:
  type: omnigent
  # 这一层只负责调用 MCP，把任务转给真实成员；沿用已认证的 Claude Code native CLI。
  # 真实成员自己的 model/appid 仍由 Meyo registry 按任务选择，不能把网关 model-id
  # 塞给外层 openai-agents provider，否则会在 MCP 调用前直接 404。
  config:
    harness: claude-native

interaction:
  conversational: true
  modalities:
    input: [text]
    output: [text]

prompt: |
  你是 Meyo 军团成员 {key}。收到任务后必须调用 run_assigned_harness，把完整任务原样交给
  registry 中 key={key} 的真实 harness 执行。返回其真实结果和产物，不自行伪造执行结果。

tools:
  meyo-member:
    type: mcp
    command: python3.12
    args: ["-m", "meyo_mcp_servers.member_server"]
    env:
      PYTHONPATH: {_yaml_scalar(str(HERE))}
      MEYO_MEMBER_KEY: {key}
      MEYO_FAIR_QID: {_yaml_scalar("adhoc")}
      HARNESS_OPENAI_AGENTS_GATEWAY_BASE_URL: {_yaml_scalar("http://127.0.0.1:18756/v1")}
      HARNESS_OPENAI_AGENTS_API_KEY: {_yaml_scalar("meyo-local-bridge")}

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
"""


def agent_paths(output_dir: str | Path = DEFAULT_OUTPUT, include_captain: bool = True) -> list[Path]:
    root = Path(output_dir)
    paths = sorted(path.resolve() for path in root.glob("meyo-*")
                   if (path / "config.yaml").is_file())
    if include_captain:
        paths.insert(0, (HERE / "agents" / "captain").resolve())
    return paths


def server_agent_args(output_dir: str | Path = DEFAULT_OUTPUT, include_captain: bool = True) -> list[str]:
    return [part for path in agent_paths(output_dir, include_captain)
            for part in ("--agent", str(path))]


def builtin_agent_env(output_dir: str | Path = DEFAULT_OUTPUT,
                      include_captain: bool = True) -> str:
    """返回 Omnigent 启动时可直接消费的动态成员目录清单。"""
    return os.pathsep.join(str(path) for path in agent_paths(output_dir, include_captain))


def bundle_fingerprint(output_dir: str | Path = DEFAULT_OUTPUT,
                       include_captain: bool = True) -> str:
    """内容指纹用于识别运行中服务是否仍挂着旧 Agent bundles。"""
    digest = hashlib.sha256()
    for path in agent_paths(output_dir, include_captain):
        digest.update(path.name.encode("utf-8"))
        for item in sorted(child for child in path.rglob("*") if child.is_file()):
            digest.update(str(item.relative_to(path)).encode("utf-8"))
            digest.update(item.read_bytes())
    return digest.hexdigest()[:16]


def sync(output_dir: str | Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    rows = orchestrator.list_harnesses()
    expected = set()
    written = []
    for row in rows:
        key = _safe_key(str(row["key"]))
        target = root / f"meyo-{key}"
        expected.add(target.name)
        target.mkdir(parents=True, exist_ok=True)
        config = target / "config.yaml"
        content = agent_spec(row)
        if not config.exists() or config.read_text(encoding="utf-8") != content:
            config.write_text(content, encoding="utf-8")
        written.append({"key": row["key"], "name": f"meyo-{key}", "path": str(target),
                        "available": bool(row.get("available"))})
    for path in root.iterdir():
        if path.is_dir() and path.name.startswith("meyo-") and path.name not in expected:
            shutil.rmtree(path)
    manifest = {"schema_version": 1, "source": "orchestrator.list_harnesses", "agents": written}
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                        encoding="utf-8")
    manifest["bundle_fingerprint"] = bundle_fingerprint(root)
    return manifest


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--print-server-args", action="store_true")
    args = parser.parse_args(argv)
    manifest = sync(args.output)
    if args.print_server_args:
        print(json.dumps(server_agent_args(args.output), ensure_ascii=False))
    else:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
