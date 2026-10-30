#!/usr/bin/env python3
"""军团产物交付:本地静态文件地址映射,或经授权公开发布。

两种模式:
  · local  : 映射本服务的 /home-outputs/ 或 /media/ 静态文件 URL;
             不启动或托管应用进程,HTTP/业务可用性尚未测试,访问仍受既有服务授权约束。
  · public : 用 vercel(装了)或 gh(已登录)真部署到公网·给任何人可访问的 URL(对外发布·需授权)

context not control:军团自己判断是否使用、部署什么以及如何验证;父层只提供工具与事实。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

import legion_paths as _paths

_LEGION_DIR = _paths.legion_dir()
_PORT = os.environ.get("LEGION_SERVICE_PORT", "8000")
_HOST = os.environ.get("LEGION_PUBLIC_HOST", f"http://127.0.0.1:{_PORT}")


def _rel_to_url(path: str) -> str | None:
    """把落盘产物路径映射成本服务的静态路由 URL(只映射,不探活)。"""
    from urllib.parse import quote
    p = os.path.realpath(os.path.expanduser(path))
    media = os.path.realpath(_paths.media_dir())
    home_out = os.path.realpath(_paths.under("outputs"))
    for root, prefix in ((media, "/media/"), (home_out, "/home-outputs/")):
        if p == root or p.startswith(root + os.sep):
            rel = os.path.relpath(p, root)
            if prefix == "/media/":
                return _HOST + prefix + quote(os.path.basename(p))
            return _HOST + prefix + "/".join(quote(x) for x in rel.split(os.sep))
    return None


def deploy_local(path: str) -> dict:
    """本地静态文件地址映射:ok 只表示文件存在且已取得映射 URL。

    不启动或托管应用进程,不探测 HTTP/业务可用性;访问仍受既有服务授权约束。
    """
    p = os.path.realpath(os.path.expanduser(path))
    if not os.path.isfile(p):
        return {"ok": False, "error": f"文件不存在: {path}"}
    url = _rel_to_url(p)
    if not url:
        return {"ok": False, "error": "文件不在可 serve 的目录(需在 ~/.meyo/legion/outputs 或 media 内)",
                "hint": "把产物写到 MEYO_WORKDIR(~/.meyo/legion/outputs/<key>_<qhash>/) 再部署"}
    return {"ok": True, "mode": "local", "url": url, "path": p,
            "delivery_kind": "static_file_url_mapping", "runtime_managed": False,
            "http_verification": "not_tested", "business_verification": "not_tested",
            "note": "仅完成静态文件地址映射；ok 仅表示文件存在且 URL 已映射。"
                    "未托管应用进程，HTTP/业务可用性尚未测试；访问仍遵循既有服务授权。"
                    "公网发布用 mode=public，需 confirm 授权。"}


def _run(cmd: list, timeout: int = 300, cwd: str = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)


def _deploy_vercel_site(site: str, original_path: str) -> dict:
    try:
        r = _run(["vercel", "deploy", "--prod", "--yes", site], timeout=300)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "vercel 部署超时(300s)"}
    out = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"https://[^\s]+\.vercel\.app", out)
    if r.returncode == 0 and m:
        return {"ok": True, "mode": "public", "provider": "vercel", "url": m.group(0),
                "path": original_path, "note": "公网可访问的运行中页面·任何人可打开。"}
    return {"ok": False, "error": "vercel 部署失败", "detail": out[-800:],
            "hint": "首次用需 vercel login;或改用 mode=local 先给本机 URL。"}


def deploy_public_vercel(path: str) -> dict:
    """公网部署(vercel):把 html/静态目录部署到 vercel·返回公网 URL。对外发布·调用方需已授权。"""
    if not shutil.which("vercel"):
        return {"ok": False, "error": "vercel 未安装(npm i -g vercel)"}
    p = os.path.realpath(os.path.expanduser(path))
    if not os.path.exists(p):
        return {"ok": False, "error": f"路径不存在: {path}"}
    if os.path.isdir(p):
        return _deploy_vercel_site(p, p)
    # 单文件站点仅在调用作用域内存在；成功、失败和超时返回前都会自动回收。
    with tempfile.TemporaryDirectory(prefix="legion-deploy-") as site:
        shutil.copy(p, os.path.join(site, "index.html"))
        return _deploy_vercel_site(site, p)


def deploy_public_gist(path: str) -> dict:
    """公网部署(gh gist + htmlpreview):单 html 文件走 gist·返回可在线渲染的公网 URL。"""
    if not shutil.which("gh"):
        return {"ok": False, "error": "gh 未安装"}
    p = os.path.realpath(os.path.expanduser(path))
    if not os.path.isfile(p):
        return {"ok": False, "error": f"文件不存在: {path}"}
    try:
        r = _run(["gh", "gist", "create", "--public", p], timeout=120)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "gh gist 超时"}
    out = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"https://gist\.github\.com/[^\s]+", out)
    if r.returncode == 0 and m:
        gist_url = m.group(0)
        preview = f"https://htmlpreview.github.io/?{gist_url}/raw"
        return {"ok": True, "mode": "public", "provider": "gist", "url": gist_url,
                "rendered_url": preview, "path": p,
                "note": "gist 公网 URL·rendered_url 可直接在线渲染 html。"}
    return {"ok": False, "error": "gh gist 失败", "detail": out[-800:]}


def deploy(path: str, mode: str = "local", provider: str = "vercel", confirm: bool = False) -> dict:
    """统一入口。mode=local(默认·静态文件地址映射)/public(公网·需 confirm 授权对外发布)。

    公网发布是对外动作·未 confirm 只返回计划(dry-run)·confirm=True 才真部署(用户授权后)。
    """
    if mode == "local":
        return deploy_local(path)
    if mode == "public":
        if not confirm:
            return {"ok": True, "status": "planned", "mode": "public", "provider": provider,
                    "note": f"公网部署是对外发布动作·确认后执行。将用 {provider} 部署 {os.path.basename(path)}。"}
        if provider == "gist":
            return deploy_public_gist(path)
        return deploy_public_vercel(path)
    return {"ok": False, "error": f"未知 mode: {mode}(用 local 或 public)"}
