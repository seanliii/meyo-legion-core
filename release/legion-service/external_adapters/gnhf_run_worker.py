#!/usr/bin/env python3
"""Bind the native GNHF runner to an explicitly deployed CCR, then exec it."""
import ipaddress
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit


def native_invocation(binary, task, model, environment):
    env = dict(environment)
    base = env.get("LEGION_CCR_BASE_URL", "").strip().rstrip("/")
    if base:
        url = urlsplit(base)
        loopback = url.hostname == "localhost" or (
            url.hostname is not None and ipaddress.ip_address(url.hostname).is_loopback)
        if (url.scheme not in ("http", "https") or not loopback or url.username is not None
                or url.password is not None or url.query or url.fragment):
            raise ValueError("invalid deployed CCR loopback URL")
        url.port
        service = str(Path(__file__).resolve().parents[1])
        if service not in sys.path:
            sys.path.insert(0, service)
        import model_registry
        route = model_registry.ccr_route(
            model, env.get("MEYO_UNIFIED_APPID") or env.get("AIGC_APPID", ""),
            env.get("MEYO_UNIFIED_GATEWAY", ""), env)
        env["ANTHROPIC_BASE_URL"] = base
        env["ANTHROPIC_AUTH_TOKEN"] = env.get("LEGION_CCR_AUTH_TOKEN", "")
        env["ANTHROPIC_MODEL"] = route
        env.pop("ANTHROPIC_API_KEY", None)
    env["NO_PROXY"] = ",".join(filter(None, [env.get("NO_PROXY", ""), "127.0.0.1", "localhost", "::1"]))
    env["no_proxy"] = env["NO_PROXY"]
    command = [binary, "--agent", "claude", "--max-iterations", "3", "--stop-when",
               "The requested result is complete and has been verified; report completion.", task]
    return command, env


def main():
    if len(sys.argv) != 4:
        print("usage: gnhf_run_worker.py <native-binary> <task> <model>", file=sys.stderr)
        return 2
    try:
        command, env = native_invocation(*sys.argv[1:], os.environ)
    except (ValueError, TypeError):
        print("GNHF CCR configuration invalid: check loopback URL and model/provider mapping", file=sys.stderr)
        return 2
    # Preserve the process group already registered by the external CLI transport.
    os.execvpe(command[0], command, env)


if __name__ == "__main__":
    raise SystemExit(main())
