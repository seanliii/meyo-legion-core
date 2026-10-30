#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""依赖自愈(dependency autoheal)——给能力·不加门(用户 2026-08-28 定)。

用户核心:"缺的不会补吗?不是有上下文吗?别控制,让 context 自愈"。
工具/skill 执行时 ModuleNotFoundError 不是"工具坏"、更不该记账成"诚实失败"——
有报错上下文(缺哪个模块)就该【自动补装缺的库→重试】,让 harness 像 CC 一样
遇缺依赖自己解决,而不是停在那。

这不是 control(不拦不判答案),是 context 自愈:把"缺库"这个可自动解决的
障碍替 harness 扫掉,让它继续干活。重依赖(torch/cuda 级)装超时则如实回流,
让模型自己判断换招——仍不撒谎。

用法:
  from dep_autoheal import run_with_autoheal
  rc, out, healed = run_with_autoheal(["python3.12", script, "--help"], timeout=8)
  # 遇 ModuleNotFoundError 自动 pip install 缺库后重试·healed=装过的包列表
"""
from __future__ import annotations
import os
import re
import subprocess
import sys

# 模块名 → pip 包名(import 名和 pip 名不一致的常见库)。命中不了就用模块名本身试装。
_MODULE_TO_PKG = {
    "fitz": "PyMuPDF", "cv2": "opencv-python-headless", "PIL": "Pillow",
    "yaml": "PyYAML", "bs4": "beautifulsoup4", "sklearn": "scikit-learn",
    "docx": "python-docx", "pptx": "python-pptx", "OpenSSL": "pyOpenSSL",
    "dotenv": "python-dotenv", "Crypto": "pycryptodome", "serial": "pyserial",
    "usb": "pyusb", "gi": "PyGObject", "cairo": "pycairo", "magic": "python-magic",
    "wx": "wxPython", "MySQLdb": "mysqlclient", "psycopg2": "psycopg2-binary",
    "manim": "manim", "torch": "torch", "scapy": "scapy",
    "LnkParse3": "LnkParse3", "rekall": "rekall",
    "azure": "azure-identity", "jwt": "PyJWT", "attr": "attrs",
    "markdown": "Markdown", "requests_toolbelt": "requests-toolbelt",
    # 常见 import名≠pip名(2026-08-28 全量skill自愈实测补齐):
    "nmap": "python-nmap", "splunklib": "splunk-sdk", "Evtx": "python-evtx",
    "zeek": "zat", "win32api": "pywin32", "win32com": "pywin32", "OpenGL": "PyOpenGL",
    "google": "google-api-python-client", "boto3": "boto3", "botocore": "botocore",
    "kubernetes": "kubernetes", "paramiko": "paramiko", "redis": "redis",
    "elasticsearch": "elasticsearch", "pymongo": "pymongo", "kafka": "kafka-python",
}

# 重依赖:真正超大/需编译的(GPU/深度学习),给足超时【真尝试装】(2026-08-28 实测:manim/
# fitz/taxii 其实都能装·之前误判"装不上"是超时太短+批量并发拖慢·真给时间就装上了)。
# 只有装不上时才如实回流真实原因(如 python-nmap 的 metadata-generation-failed)·不假装。
_HEAVY = {"torch", "tensorflow", "cupy", "jax", "jaxlib", "tensorrt", "onnxruntime-gpu"}

_MODNOTFOUND_RE = re.compile(r"No module named ['\"]([\w.]+)['\"]")
_JS_MODNOTFOUND_RE = re.compile(r"Cannot find module ['\"]([^'\"]+)['\"]")


def _pkg_for(module: str) -> str:
    """模块名 → pip 包名(取顶层模块;查映射表·查不到用模块名本身)。"""
    top = module.split(".")[0]
    return _MODULE_TO_PKG.get(top, top)


def pip_install(pkg: str, timeout: float = 120.0) -> tuple[bool, str]:
    """真装一个 pip 包(python3.12 环境·--break-system-packages 兼容 brew python)。

    关键(2026-08-28 实测踩坑):默认 pip install 会升级依赖树——自愈装 kubernetes/splunklib
    时把 urllib3 升到 2.x,删掉了 urllib3.packages.six.moves,连锁搞坏 OpenMontage 等已装工具。
    自愈绝不能"补一个坏一片"。所以【优先 --no-deps 只装缺的顶层包·不动已装依赖】;--no-deps
    若导致该包自身缺子依赖,下一轮自愈会精准补那个子依赖(仍 --no-deps),逐个补而不整树升级。
    """
    heavy = pkg.split("==")[0].lower() in _HEAVY
    to = 600.0 if heavy else timeout
    base = [sys.executable, "-m", "pip", "install", "--break-system-packages",
            "--disable-pip-version-check", "-q"]
    try:
        # 第一优先:--no-deps(不升级/不动已装依赖·避免连锁污染)
        r = subprocess.run(base + ["--no-deps", pkg], capture_output=True, text=True, timeout=to)
        if r.returncode == 0:
            return True, "installed (--no-deps·未动已装依赖)"
        # --no-deps 失败(极少·如包本身不存在)→ 退普通装,但加 --upgrade-strategy only-if-needed
        # 尽量少动已装包(pip 默认就是 only-if-needed,这里显式声明意图)
        r2 = subprocess.run(base + ["--upgrade-strategy", "only-if-needed", pkg],
                            capture_output=True, text=True, timeout=to)
        return r2.returncode == 0, (r2.stderr or r2.stdout)[-300:]
    except subprocess.TimeoutExpired:
        return False, f"pip install {pkg} 超时(>{to}s·可能重依赖·如实回流让模型换招)"
    except Exception as e:
        return False, f"pip install {pkg} 异常:{type(e).__name__}:{e}"


def _top_importable(mod: str) -> bool:
    """顶层模块能否 import(判断"缺 X.Y 子模块"到底是缺包还是包内部路径问题)。"""
    import importlib.util
    try:
        return importlib.util.find_spec(mod.split(".")[0]) is not None
    except Exception:
        return False


def extract_missing_module(output: str) -> str | None:
    """从执行输出解析缺失模块名。缺 X.Y 但顶层 X 已可 import → 返回 None
    (那是包内部 import 路径问题·装包解决不了·别无限重装)。"""
    m = _MODNOTFOUND_RE.search(output or "")
    if m:
        mod = m.group(1)
        # 子模块缺失(a.b.c)且顶层已装 → 不是缺包·不重装(splunklib.client/six.moves 这类)
        if "." in mod and _top_importable(mod):
            return None
        return mod
    m = _JS_MODNOTFOUND_RE.search(output or "")
    if m and not m.group(1).startswith("."):   # 相对路径不是缺包
        return m.group(1)
    return None


def run_with_autoheal(cmd: list[str], timeout: float = 8.0, cwd: str | None = None,
                      env: dict | None = None, max_heals: int = 3) -> tuple:
    """执行 cmd;遇缺模块自动 pip 装缺库后重试。返回 (returncode, output, healed_pkgs)。

    max_heals:最多补装几个不同的库(防一个坏脚本无限装)。healed_pkgs=真装成功的包。
    """
    healed: list[str] = []
    tried_mods: set[str] = set()   # 按“缺失模块名”去重(不按 pip 包名):pywin32/google 等
    # 一个 pip 包提供多个顶层模块,按包名去重会把“下一个模块”误判成“装过还缺”提前放弃。
    while True:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout, cwd=cwd, env=env)
            out = (r.stdout + "\n" + r.stderr).strip()
            rc = r.returncode
        except subprocess.TimeoutExpired:
            return "timeout", "进程真启动后超时(服务型/等待输入)", healed
        except Exception as e:
            return "exc", f"{type(e).__name__}:{e}", healed

        missing = extract_missing_module(out)
        # 成功、或不是缺库问题、或补装次数用尽 → 返回真实结果(不撒谎)
        if rc == 0 or not missing or len(healed) >= max_heals:
            return rc, out, healed

        if missing in tried_mods:  # 同一“模块”装完还缺 → 真装不好·停(如实回流)
            return rc, out, healed
        tried_mods.add(missing)
        pkg = _pkg_for(missing)
        ok, msg = pip_install(pkg)
        if ok:
            healed.append(pkg)     # 装成功 → 循环重试执行
        else:
            # 装不上(重依赖超时/包名不对)→ 如实返回·不假装成功(不撒谎)
            return rc, out + f"\n[autoheal] 尝试装 {pkg} 失败:{msg}", healed


if __name__ == "__main__":
    # 自测:装一个缺库并验证
    import json
    demo = sys.argv[1:] or ["python3.12", "-c", "import markdown; print('ok')"]
    rc, out, healed = run_with_autoheal(demo, timeout=30)
    print(json.dumps({"rc": rc, "healed": healed, "out": out[-200:]}, ensure_ascii=False))
