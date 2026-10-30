#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""legion_paths · 军团运行时路径【单一权威】(归一复用)

用户 2026-08-28 复盘钉死:测试的副作用绝不能落到用户真实家目录。此前各模块
各自 os.path.expanduser("~/.meyo/legion/...") 硬编码,测试隔离要一处一处补 env,
永远漏——official_registry / cron / crawl 都因此被测试假数据污染过。

根治:所有写用户家目录的路径【只在这里】定义,统一读环境变量:
  - MEYO_LEGION_DIR  → ~/.meyo/legion 系(registry/media/uploads/repos/memory/model_*)
  - LEGION_HOME_DIR  → ~/.legion 系(sessions/exports/cron/crawl;各子目录仍保留
                       自己的专用 env 以兼容既有测试,但缺省都落到这棵树下)

测试只需在 import 任何产品模块【之前】设好这两个 env 到临时目录,即全隔离,
不用再逐个模块打补丁。新增写家目录的模块也必须从这里取路径,禁止再手写
expanduser("~/.meyo") / expanduser("~/.legion")。

★ 设计依据(师从最强 harness·三证合一):
  1. 源码证 —— codex 把运行时根目录解析单独拆成一个 crate `utils/home-dir`:
     `harness_baselines/codex/codex-rs/utils/home-dir/src/lib.rs:13 find_codex_home()`
     = 读 CODEX_HOME env·未设则退默认 ~/.codex。本模块 legion_dir()/legion_home()
     与之完全同构:单一权威函数·env 优先·否则默认家目录。codex 全项目所有需要根
     目录的地方都调这一个函数(`core/src/config/mod.rs:4709` 再导出),不各自 expanduser。
  2. 测试隔离范式证 —— codex `rmcp-client/src/oauth/test_support.rs:12 TempCodexHome`:
     测试建 TempDir → set_var("CODEX_HOME", dir) → 用完 Drop 里 remove_var·并用
     进程级 Mutex 串行化(防并发测试指向不同 home)。我们测试顶部"import 前设 env 到
     tmp 根"就是这一范式的 Python 版(见 tests/test_legion_service.py 隔离块)。
  3. 佐证 —— deepseek-harness 测试同样走 env 注入临时根:
     `harness_baselines/deepseek-harness/python/sdk/tests/test_bundled_runtime.py:62`
     DSH_SESSION_ROOT=tmp_path/sessions、DSH_CWD=tmp_path。多家共识=运行时路径必须
     可 env 重定向,测试注入 tmp,绝不写真实家目录。
"""
import os


def legion_dir() -> str:
    """~/.meyo/legion 根(可 MEYO_LEGION_DIR 重定向)。"""
    return os.environ.get("MEYO_LEGION_DIR") or os.path.expanduser("~/.meyo/legion")


def legion_home() -> str:
    """~/.legion 根(可 LEGION_HOME_DIR 重定向;sessions/exports/cron/crawl 落这)。"""
    return os.environ.get("LEGION_HOME_DIR") or \
        os.path.join(os.path.expanduser("~"), ".legion")


def under(*parts) -> str:
    """~/.meyo/legion 下的子路径。"""
    return os.path.join(legion_dir(), *parts)


def home_under(*parts) -> str:
    """~/.legion 下的子路径。"""
    return os.path.join(legion_home(), *parts)


# ── 常用具名子路径(各模块直接调用,保持单一来源)──────────────
def media_dir() -> str:
    return under("media")


def uploads_dir() -> str:
    return under("uploads")


def repos_dir() -> str:
    return under("repos")
