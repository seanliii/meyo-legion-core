#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Omnigent server/host 生命周期：产品自动托管，用户无需执行 CLI。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class RuntimeStatus:
    ready: bool
    server_url: str
    host_online: bool
    error: str | None = None


class OmnigentRuntimeManager:
    def __init__(self, *, omnigent_root: Path, orchestrator_root: Path,
                 server_url: str | None = None,
                 server_ensure: Callable[[], str] | None = None,
                 popen: Callable[..., subprocess.Popen] = subprocess.Popen) -> None:
        self.omnigent_root = Path(omnigent_root)
        self.orchestrator_root = Path(orchestrator_root)
        self.server_url = (server_url or "").rstrip("/")
        self.server_ensure = server_ensure
        self.popen = popen
        self._host_proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def _sync_agent_runtime(self) -> str:
        """同步动态 registry，并把 bundle 指纹纳入 Omnigent 服务复用签名。"""
        import omnigent_registry_bridge as bridge

        bridge.sync()
        paths = bridge.builtin_agent_env()
        fingerprint = bridge.bundle_fingerprint()
        os.environ["OMNIGENT_BUILTIN_AGENT_DIRS"] = paths
        os.environ["MEYO_OMNIGENT_AGENT_BUNDLE_FINGERPRINT"] = fingerprint
        return fingerprint

    def ensure_ready(self, timeout_s: float = 30.0) -> RuntimeStatus:
        with self._lock:
            try:
                self._sync_agent_runtime()
                if not self.server_url:
                    if self.server_ensure is None:
                        self.server_url = self._ensure_local_server()
                    else:
                        self.server_url = self.server_ensure().rstrip("/")
                if self._host_online():
                    return RuntimeStatus(True, self.server_url, True)
                self._ensure_host_process()
            except Exception as exc:
                return RuntimeStatus(False, self.server_url, False,
                                     f"{type(exc).__name__}:{exc}")
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._host_online():
                return RuntimeStatus(True, self.server_url, True)
            if self._host_proc is not None and self._host_proc.poll() is not None:
                return RuntimeStatus(False, self.server_url, False,
                                     f"host_exited:{self._host_proc.returncode}")
            time.sleep(0.5)
        return RuntimeStatus(False, self.server_url, False, "host_start_timeout")

    def stop(self) -> None:
        with self._lock:
            proc, self._host_proc = self._host_proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _ensure_local_server(self) -> str:
        sys.path.insert(0, str(self.omnigent_root))
        from omnigent.host.local_server import ensure_local_omnigent_server
        return ensure_local_omnigent_server().url.rstrip("/")

    def _host_online(self) -> bool:
        if not self.server_url:
            return False
        try:
            with urllib.request.urlopen(self.server_url + "/v1/hosts", timeout=3) as response:
                hosts = json.load(response).get("hosts") or []
            return any(host.get("status") == "online" for host in hosts)
        except (OSError, ValueError, urllib.error.URLError):
            return False

    def _ensure_host_process(self) -> None:
        if self._host_proc is not None and self._host_proc.poll() is None:
            return
        log_dir = self.orchestrator_root / ".runtime"
        log_dir.mkdir(parents=True, exist_ok=True)
        log = open(log_dir / "omnigent-host.log", "ab", buffering=0)
        self._host_proc = self.popen(
            [sys.executable, "-m", "omnigent.cli", "host", self.server_url],
            cwd=str(self.omnigent_root), stdout=log, stderr=log,
            start_new_session=True,
        )
