#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部 Agent 可重复部署命令行。"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import pathlib
import sys
import time

import external_agent_registry as registry


def main() -> int:
    parser = argparse.ArgumentParser(description="安装、探测并验收锁定版本的外部 Agent")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list")
    probe_parser = sub.add_parser("probe")
    probe_parser.add_argument("key", nargs="?", default="")
    install_parser = sub.add_parser("install")
    install_parser.add_argument("keys", nargs="+")
    install_parser.add_argument("--confirm", action="store_true")
    install_parser.add_argument("--timeout", type=int, default=1800)
    install_parser.add_argument("--jobs", type=int, default=3, help="并行安装数；默认3，避免重构建互抢资源")
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--report", default="")
    discover_parser = sub.add_parser("discover")
    discover_parser.add_argument("source", help="本地仓库目录")
    discover_parser.add_argument("--url", default="", help="GitHub URL；提供后会解析不可变 revision")
    discover_parser.add_argument("--ref", default="HEAD", help="tag/branch/ref")
    discover_parser.add_argument("--output", default="", help="候选 manifest 输出路径")
    updates_parser = sub.add_parser("updates")
    updates_parser.add_argument("key", nargs="?", default="")
    args = parser.parse_args()

    if args.action == "list":
        result = {"catalog": registry.validate_catalog(), **registry.probe()}
    elif args.action == "probe":
        result = registry.probe(args.key)
    elif args.action == "install":
        keys = [item["key"] for item in registry.load_catalog()] if args.keys == ["all"] else args.keys
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(args.jobs, len(keys)))) as pool:
            futures = {pool.submit(registry.install, key, args.confirm, args.timeout): key for key in keys}
            rows = []
            for future in concurrent.futures.as_completed(futures):
                row = future.result()
                rows.append(row)
                print(json.dumps({"event": "install_result", **row}, ensure_ascii=False), flush=True)
        rows.sort(key=lambda row: keys.index(row.get("key")))
        result = {"ok": all(row.get("ok") for row in rows), "results": rows}
    elif args.action == "discover":
        import agent_manifest
        result = agent_manifest.discover_local(args.source, url=args.url, ref=args.ref)
        if args.output and result.get("candidate"):
            path = pathlib.Path(args.output).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            result["output"] = str(path)
    elif args.action == "updates":
        result = registry.check_updates(args.key)
    else:
        result = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                  "catalog": registry.validate_catalog(), **registry.probe()}
        if args.report:
            path = pathlib.Path(args.report).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            result["report"] = str(path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok", result.get("catalog", {}).get("ok", True)) else 1


if __name__ == "__main__":
    sys.exit(main())
