#!/usr/bin/env python3
"""
install_registry · 军团统一安装注册中心(基础建设)

用户 2026-08-27:彻底盘点所有工具/skill/组件/开源agent/库,支持:
  - 大家共用(shared) + 单独安装
  - 按场景安装(scene:search/vision/video/audio/data/web/code/research)
  - 下一步一堆新东西来时,不改代码就能 register 到持久化清单

统一数据模型:
  {id,name,kind,scenes,install:[argv],check:{method,target},shared,deps,size_hint,note,source}

安装安全:
  - 默认 dry-run(只告诉你会装什么),真装必须 confirm=True
  - 只放行 pip/npm/npx/brew/git 这些已知安装器,不执行任意 shell/curl|sh
  - 每步返回 stdout/stderr/exit_code,错误完整透明给调用方/模型判断
"""
import importlib.metadata
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Optional

# 隔离 Python 不搜索 cwd/脚本目录；CLI 只显式加入自身可信运行包，不加入被检源码。
if __name__ == "__main__" and sys.flags.isolated:
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

# 路径单一权威走 legion_paths(读 MEYO_LEGION_DIR 重定向):避免测试注册项(pip:__ut_* 等)
# 污染真实 ~/.meyo/legion/official_registry.json → 用户安装中心冒出一堆假项(实测发现的污染 bug)。
import legion_paths as _paths
_LEGION_DIR = _paths.legion_dir()


def _atomic_write_json(path: str, data) -> None:
    """原子写 JSON:tmp+fsync+os.replace,防并发覆盖时留下截断/损坏文件。

    军团 hermes dogfood 揪出:registry/manifest/state 用 open(w) 直接覆盖写,两进程并发注册
    会互相覆盖,进程中途崩溃还会留截断 JSON。原子替换保证:要么旧内容要么新内容,永不半截。
    (文件锁需 fcntl·跨平台复杂;先上原子替换根治"截断损坏"这个最严重的·并发覆盖概率低。)
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as h:
        json.dump(data, h, ensure_ascii=False, indent=1)
        h.flush()
        os.fsync(h.fileno())
    os.replace(tmp, path)
# 三层注册表:官方内置(代码BUILTINS·只读) < 官方扩展(共享·管理员) < 用户私有(各自一份)
_OFFICIAL_PATH = os.path.join(_LEGION_DIR, "official_registry.json")
_REG_PATH = os.path.join(_LEGION_DIR, "install_registry.json")  # 兼容旧路径=官方扩展
_MANIFEST_PATH = os.path.join(_LEGION_DIR, "official_manifest.json")  # 官方发布清单·带版本(升级广播)


def _load_manifest() -> dict:
    try:
        return json.load(open(_MANIFEST_PATH, "r", encoding="utf-8"))
    except Exception:
        return {"version": 0, "released_at": None, "changelog": [], "items": []}


def _upgrade_state_path(uid: Optional[str] = None) -> str:
    uid = (uid or os.environ.get("MEYO_LEGION_UID") or "default").strip() or "default"
    safe = "".join(c for c in uid if c.isalnum() or c in "-_")[:60] or "default"
    return os.path.join(_LEGION_DIR, "users", safe, "upgrade_state.json")


def _user_path(uid: Optional[str] = None) -> str:
    """用户私有注册表路径。uid 无则用 default(单机),多用户各自一份。"""
    uid = (uid or os.environ.get("MEYO_LEGION_UID") or "default").strip() or "default"
    safe = "".join(c for c in uid if c.isalnum() or c in "-_")[:60] or "default"
    return os.path.join(_LEGION_DIR, "users", safe, "registry.json")

# 内置清单:覆盖现有军团最常用5类。以后用户给的新东西走 register_item,不需改代码。
# install 是 argv 数组(不用 shell=True·避免注入)。
BUILTINS = [
    # ── 搜索/网页(共享) ──
    {"id":"pip:ddgs","name":"DDGS 多引擎搜索","kind":"pip","scenes":["search","research"],
     "install":[sys.executable,"-m","pip","install","--break-system-packages","ddgs"],
     "check":{"method":"import","target":"ddgs"},"shared":True,"source":"PyPI","size_hint":"small"},
    {"id":"pip:tavily","name":"Tavily 搜索","kind":"pip","scenes":["search","research"],
     "install":[sys.executable,"-m","pip","install","--break-system-packages","tavily-python"],
     "check":{"method":"import","target":"tavily"},"shared":True,"source":"PyPI","size_hint":"small","note":"需要TAVILY_API_KEY才增强;无key自动跳过"},
    {"id":"pip:wikipedia","name":"Wikipedia 直连","kind":"pip","scenes":["search","research"],
     "install":[sys.executable,"-m","pip","install","--break-system-packages","wikipedia"],
     "check":{"method":"import","target":"wikipedia"},"shared":True,"source":"PyPI","size_hint":"small"},
    {"id":"pip:browser-use","name":"Browser Use 网页自动化","kind":"pip","scenes":["web","browse","research"],
     "install":[sys.executable,"-m","pip","install","--break-system-packages","browser-use"],
     "check":{"method":"import","target":"browser_use"},"shared":True,"source":"PyPI","size_hint":"medium"},
    {"id":"pip:cua-cli-mcp","name":"CUA Computer Use 控制端","kind":"pip","scenes":["computeruse","desktop","agent"],
     "install":["python3.12","-m","pip","install","--break-system-packages","--extra-index-url","https://wheels.cua.ai/simple","cua-cli[mcp]"],
     "check":{"method":"cli","target":"cua"},"shared":True,"source":"CUA","size_hint":"medium",
     "note":"官方 MCP 指令为 cua serve-mcp。本地桌面需 Docker daemon；云端 sandbox 需先执行 cua auth login。"},
    {"id":"npm:opencli","name":"OpenCLI(100+网站→CLI)","kind":"npm","scenes":["web","browse","social"],
     "install":["npm","install","-g","@jackwener/opencli"],
     "check":{"method":"cli","target":"opencli"},"shared":True,"source":"OpenCLI","size_hint":"medium","note":"需Node>=20·复用浏览器登录态·无需API key"},
    {"id":"cli:playwright-chromium","name":"Chromium 浏览器运行时","kind":"cli","scenes":["web","browse","test","vision"],
     "install":[sys.executable,"-m","playwright","install","chromium"],
     "check":{"method":"playwright_browser","target":"chromium"},"shared":True,"source":"Playwright","size_hint":"large"},
    # ── 数据/Excel/PDF ──
    {"id":"pip:openpyxl","name":"Excel(xlsx)处理","kind":"pip","scenes":["data","spreadsheet","office"],
     "install":[sys.executable,"-m","pip","install","--break-system-packages","openpyxl"],
     "check":{"method":"import","target":"openpyxl"},"shared":True,"source":"PyPI","size_hint":"small"},
    {"id":"pip:pandas","name":"表格数据分析","kind":"pip","scenes":["data","spreadsheet","analysis"],
     "install":[sys.executable,"-m","pip","install","--break-system-packages","pandas"],
     "check":{"method":"import","target":"pandas"},"shared":True,"source":"PyPI","size_hint":"medium"},
    {"id":"pip:pypdf","name":"PDF文本解析","kind":"pip","scenes":["pdf","document","office"],
     "install":[sys.executable,"-m","pip","install","--break-system-packages","pypdf"],
     "check":{"method":"import","target":"pypdf"},"shared":True,"source":"PyPI","size_hint":"small"},
    # ── 多媒体 ──
    {"id":"brew:ffmpeg","name":"FFmpeg视频/音频剪辑","kind":"cli","scenes":["video","audio","media"],
     "install":["brew","install","ffmpeg"],"check":{"method":"cli","target":"ffmpeg"},
     "shared":True,"source":"FFmpeg","size_hint":"large"},
    {"id":"repo:openmontage","name":"OpenMontage视频工作流","kind":"repo","scenes":["video","media","generation"],
     "install":[],"check":{"method":"path","target":""},
     "shared":True,"source":"https://github.com/calesthio/OpenMontage","size_hint":"large",
     "note":"按需获取锁定源码；provider依赖、FFmpeg和账号另配，不等于视频生成就绪"},
    {"id":"repo:qwen-audio","name":"Qwen Audio Agent","kind":"repo","scenes":["audio","voice"],
     "install":[],"check":{"method":"path","target":"harness_baselines/ecosystem/qwen-audio-agent"},
     "shared":True,"source":"ecosystem/qwen-audio-agent","size_hint":"large"},
    # ── agent/harness CLI ──
    {"id":"cli:codex","name":"Codex CLI","kind":"agent","scenes":["code","research","agent"],
     "install":[],"check":{"method":"cli","target":"codex"},"shared":False,"source":"OpenAI","size_hint":"medium","note":"按官方渠道安装/登录"},
    {"id":"cli:claude","name":"Claude Code CLI","kind":"agent","scenes":["code","research","agent"],
     "install":[],"check":{"method":"cli","target":"claude"},"shared":False,"source":"Anthropic","size_hint":"medium","note":"按官方渠道安装/登录"},
    {"id":"cli:hermes","name":"Hermes Agent CLI","kind":"agent","scenes":["research","media","agent"],
     "install":[],"check":{"method":"cli","target":"hermes"},"shared":False,"source":"Nous Research","size_hint":"medium"},
    {"id":"cli:dsh","name":"DeepSeek Harness","kind":"agent","scenes":["research","long-context","agent"],
     "install":["npx","@deepseek-ai/dsh"],"check":{"method":"cli","target":"dsh"},
     "shared":False,"source":"DeepSeek AI","size_hint":"large"},
    # ── 本地共享能力 ──
    {"id":"builtin:file-hub","name":"通用文件管道","kind":"component","scenes":["file","data","code","media"],
     "install":[],"check":{"method":"import","target":"file_hub"},"shared":True,"source":"legion-service","size_hint":"builtin"},
    {"id":"builtin:toolbox","name":"军团共享工具箱","kind":"component","scenes":["agent","all"],
     "install":[],"check":{"method":"import","target":"toolbox"},"shared":True,"source":"legion-service","size_hint":"builtin"},
]

_ALLOWED_INSTALLERS = {"pip", "pip3", "npm", "npx", "brew", "git",
                       "python", "python3", "python3.12", "python3.11", "python3.13",
                       sys.executable, os.path.basename(sys.executable)}


def _load_json_list(path: str) -> list:
    try:
        d = json.load(open(path, "r", encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:
        return []


_ARSENAL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "arsenal_catalog.json")
_ARSENAL_CACHE = {"items": None}

# scan() 探活很慢(148 项串行·每项跑 import/which/npm/子进程探针·15 个 external agent
# 走 registry.probe 单家不缓存)——真测端到端 20-30s。前端页面加载即 fetch(index_v2.html:1676)
# 会让首屏白等 20s。修法学 external_agent_registry._PROBE_CACHE 的 TTL 模式:
#   ①scan 整体结果按 scene 缓存(_SCAN_CACHE)②单项 is_installed 探活结果缓存(_INSTALLED_CACHE)。
# install/register/unregister 成功后 invalidate_install_cache() 立即失效(装完前端重 fetch 拿新鲜值)。
_SCAN_TTL = 90.0          # scan 结果缓存 90s(装/卸后主动失效·不靠过期)
_INSTALLED_TTL = 90.0     # 单项探活缓存 90s
_SCAN_CACHE: dict = {}    # scene(None→"__all__") -> {"data": dict, "ts": float}
_INSTALLED_CACHE: dict = {}  # item_id -> {"val": bool, "ts": float}
_SCAN_LOCK = threading.RLock()
_SCAN_REFRESHING: set[str] = set()


def invalidate_install_cache() -> None:
    """装/卸/注册/注销后调——立即清空 scan + is_installed 缓存,让下次 fetch 拿真实结果。"""
    with _SCAN_LOCK:
        _SCAN_CACHE.clear()
        _SCAN_REFRESHING.clear()
    _INSTALLED_CACHE.clear()
    try:
        import external_agent_registry as _ear
        _ear.invalidate_probe_cache()  # external agent 探活缓存也一并失效
    except Exception:
        pass
    try:
        import legion_core as _lc
        _lc.invalidate_aigc_ready_cache()  # 装 harness 后 harnesses/captain 拿真实可用集
    except Exception:
        pass


def load_arsenal() -> dict:
    """加载 KM《Agent 军团装备总库》97 项完整清单(数据驱动·不硬编码进代码)。

    这是军团武器库的权威分类蓝本(源:KM 2783473557)。每项带 tier/priority/scenes/
    url/local_source/install_strategy。UI 的"官方军团升级"面板、军团按场景装都从这里取数。
    """
    try:
        return json.load(open(_ARSENAL_PATH, "r", encoding="utf-8"))
    except Exception:
        return {"catalog": "军团装备总库", "total": 0, "items": []}


def _arsenal_as_install_items() -> list:
    """把 catalog 97 项归一成 install_registry 的安装项结构(供 all_items 合并)。

    本地已有源码的 → check=path(可立即用);远程 repo/mcp → 标 note,按需真装再补 install argv。
    不给远程项填 install(避免误触发 clone 巨仓);真要装时走 register_item 补 argv 或用户确认。
    """
    if _ARSENAL_CACHE["items"] is not None:
        return _ARSENAL_CACHE["items"]
    out = []
    for it in load_arsenal().get("items", []):
        loc = it.get("local_source")
        # 优先用 catalog 显式 check(如A类pip项补了import探测)·否则本地源码path·再否则note占位
        if it.get("check"):
            check = it["check"]
        elif loc:
            check = {"method": "path", "target": loc}
        else:
            check = {"method": "note", "target": it.get("url") or it.get("name")}
        entry = {
            "id": it["id"], "name": it["name"], "kind": it.get("kind", "repo"),
            "scenes": it.get("scenes") or ["agent"], "check": check,
            "shared": True, "source": it.get("source"), "url": it.get("url"),
            "tier": it.get("tier"), "priority": it.get("priority"),
            "note": it.get("desc"), "install_strategy": it.get("install_strategy"),
            "km_num": it.get("km_num"),
        }
        # catalog 补了 install argv(A类可一键装)则透传·让 install()/面板能真装
        if it.get("install"):
            entry["install"] = it["install"]
        out.append(entry)
    _ARSENAL_CACHE["items"] = out
    return out


def _ecosystem_as_install_items(km_items: list) -> list:
    """把 ecosystem/ 下所有项目归一进安装中心(KM里已有同项目就不重复)。

    inventory 是源码旁的单一事实源;新增项目只要进 ecosystem + inventory,自动被军团看到,
    不需每次手改 Python。"""
    root = _repo_root()
    inventory_path = os.path.join(root, "harness_baselines", "ecosystem", "ECOSYSTEM_INVENTORY.json")
    try:
        inventory = json.load(open(inventory_path, "r", encoding="utf-8"))
    except Exception:
        return []
    # 已由 KM 条目覆盖的本地路径/名字(归一掉连字符、空格、大小写)
    def norm(s):
        return re.sub(r"[^a-z0-9]", "", str(s or "").lower())
    covered = {norm(x.get("name")) for x in km_items}
    out = []
    for name, meta in inventory.items():
        if name.startswith("_") or not isinstance(meta, dict):
            continue
        if norm(name) in covered:
            continue
        rel = f"harness_baselines/ecosystem/{name}"
        langs = meta.get("lang") or []
        kind = "skill" if "skill/markdown" in langs else "repo"
        scenes = ["agent"]
        low = name.lower()
        if "vision" in low or low == "cua": scenes += ["vision", "computeruse"]
        if "audio" in low or "speech" in low: scenes += ["audio", "voice"]
        if "web" in low or "sidebar" in low or "desktop" in low: scenes += ["web", "ui"]
        if "team" in low or low in ("omnigent", "orca", "herdr", "pilotdeck"): scenes += ["orchestration", "multiagent"]
        if "skill" in low or low == "superpowers": scenes += ["skill"]
        out.append({
            "id": f"ecosystem:{name.lower()}", "name": name, "kind": kind,
            "scenes": sorted(set(scenes)), "check": {"method": "path", "target": rel},
            "shared": True, "source": rel, "priority": "按需",
            "note": meta.get("note") or f"ecosystem 本地源码·{','.join(langs) or '组件'}",
            "install_strategy": "已随仓库(本地源码)",
        })
    return out


def _external_agents_as_install_items() -> list:
    """把用户指定的外部 agent 注册表并入安装中心；GitHub 项都能生成安全 clone 计划。"""
    try:
        import external_agent_registry as registry
    except Exception:
        return []
    out = []
    # 用全量 probe(有30s TTL缓存)一次拿所有状态·建 key→state 查表(loop dogfood 揪出:旧代码
    # 对15个agent逐个 probe(key=...)单探·单探不走缓存→install/registry端点6.38s→安装中心永久
    # "加载中")。全量 probe 缓存命中后这里几乎零耗时。
    _all = {row["key"]: row for row in registry.probe().get("agents", [])}
    for agent in registry.load_catalog():
        state = _all.get(agent["key"]) or registry.probe(agent["key"])
        url = str(agent.get("url") or "")
        out.append({
            "id": f"external-agent:{agent['key']}", "name": agent.get("name") or agent["key"],
            "kind": agent.get("kind") or "agent", "scenes": agent.get("capabilities") or ["agent"],
            "check": {"method": "path", "target": state.get("source")},
            "shared": True, "source": url, "url": url, "origin": "official",
            "priority": "按需", "install_strategy": "锁定提交→依赖安装→构建→健康探测",
            "note": ("已通过执行探测并进入军团池" if state.get("execution_ready") else
                     "只有版本、构建、凭证和运行探针全部通过才进入军团池"),
            "external_agent_key": agent["key"], "runnable": state.get("execution_ready", False),
            "runtime_probe": state, "runtime_status": state.get("status"),
            "version": state.get("version"), "revision": state.get("locked_revision"),
            "missing_credentials": (state.get("credentials") or {}).get("missing") or [],
            "credential_signup_url": (state.get("credentials") or {}).get("signup_url"),
            "credential_help": (state.get("credentials") or {}).get("help", ""),
            "protocols": state.get("protocols") or [], "execution_role": state.get("execution_role"),
            # install() 不能表达外部 registry 的确认安装语义，API/UI 通过 external_agent_key
            # 调 external_agent_registry.install；这里不伪造 shell argv。
            "install": [],
        })
    return out


_OPENMONTAGE_INSTALL_IDS = frozenset({
    "repo:openmontage", "km:24:openmontage",
    "external-agent:openmontage", "ecosystem:openmontage",
})
_OPENMONTAGE_SOURCE_FILES = (
    "tools/__init__.py", "tools/base_tool.py", "tools/tool_registry.py",
    "tools/video/video_selector.py", "lib/scoring.py",
)


def _openmontage_source_bytes(source: Path) -> dict[str, bytes]:
    """只读登记根内的普通关键文件，不沿源目录或文件的 symlink 读取。"""
    from source_audit import _read_regular_source_bytes
    paths = {}
    for name in _OPENMONTAGE_SOURCE_FILES:
        path = source / name
        if path.resolve(strict=True) != path or not path.is_file():
            raise ValueError("OpenMontage source file is not a regular in-root file")
        paths[name] = path
    return {name: _read_regular_source_bytes(path) for name, path in paths.items()}


def _openmontage_source_digest(files: dict[str, bytes]) -> str:
    hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
    return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()


def openmontage_source_status() -> dict:
    """安装中心与消费端共用的源码事实；不导入 provider、不安装，也不缓存路径。"""
    out = {"ok": False, "declared": False, "source": "", "status": "not_declared"}
    try:
        import external_agent_registry as registry
        entry = next((x for x in registry.load_catalog() if x["key"] == "openmontage"), None)
        if entry is None:
            return {**out, "error": "OpenMontage 未登记；请通过 harness_onboard 登记固定源码"}
        out.update(declared=True, revision=entry.get("revision"), version=entry.get("version"))
        source = registry.source_dir(entry)
        out["source"] = str(source)
        state = registry.probe("openmontage")  # 单家 probe 不走全量 TTL 缓存
        if not state.get("source_present"):
            return {**out, "status": "not_installed",
                    "error": "OpenMontage 源码未安装；安装中心选择 repo:openmontage"}
        if state.get("source") != str(source) or not state.get("revision_ok"):
            return {**out, "status": "source_unverified",
                    "error": "OpenMontage 源码路径或固定版本未通过校验；未回退其他目录"}
        # Git HEAD 相等不代表关键文件仍在或内容未变；消费缓存也绑定这些实际字节。
        try:
            if not state.get("available"):
                raise ValueError("source probe unavailable")
            source_digest = _openmontage_source_digest(_openmontage_source_bytes(source))
        except (OSError, ValueError, RuntimeError):
            return {**out, "status": "source_incomplete",
                    "error": "OpenMontage 关键源码缺失或探针未通过；请修复当前登记源码"}
        return {**out, "ok": True, "status": "source_ready", "source_digest": source_digest}
    except Exception as exc:
        # source-map 坏映射必须 fail closed；不能借旧工作树或另一份 .env 继续。
        return {**out, "status": "source_invalid",
                "error": f"OpenMontage 源码配置或校验失败({type(exc).__name__})；未回退其他目录"}


def check_openmontage_source(expected_source: str) -> dict:
    """当前可信运行包的只读检查入口；版本/路径失败后不再解析任何被检文件。"""
    fact = openmontage_source_status()
    if not fact["ok"]:
        return fact
    if expected_source != fact["source"]:
        return {**fact, "ok": False, "status": "source_invalid",
                "error": "OpenMontage 检查路径与当前登记源码不一致；未读取其他目录"}
    try:
        import ast
        files = _openmontage_source_bytes(Path(fact["source"]))
        if _openmontage_source_digest(files) != fact["source_digest"]:
            return {**fact, "ok": False, "status": "source_unverified",
                    "error": "OpenMontage 源码在检查期间变化；请重试当前登记源码"}
        for name, raw in files.items():
            ast.parse(raw, filename=name)
    except SyntaxError as exc:
        # 不回传 SyntaxError.text，避免把源码正文当作诊断泄露。
        return {**fact, "ok": False, "status": "source_invalid",
                "error": f"OpenMontage source syntax failed ({exc.filename}:{exc.lineno})"}
    except Exception as exc:
        return {**fact, "ok": False, "status": "source_invalid",
                "error": f"OpenMontage 源码只读检查失败({type(exc).__name__})"}
    return {**fact, "note": "OpenMontage source syntax OK; providers not tested"}


def _is_openmontage_item(item: dict) -> bool:
    return item.get("id") in _OPENMONTAGE_INSTALL_IDS or item.get("external_agent_key") == "openmontage"


def _openmontage_install_item(base: Optional[dict] = None) -> dict:
    fact = openmontage_source_status()
    item = {**(base or {}), "id": "repo:openmontage",
            "name": (base or {}).get("name") or "OpenMontage视频工作流", "kind": "repo",
            "scenes": ["video", "media", "generation"], "shared": True, "origin": "official",
            "source": "https://github.com/calesthio/OpenMontage",
            "check": {"method": "path", "target": fact["source"]}, "install": [],
            "execution_role": "capability", "runnable": False,
            "revision": fact.get("revision"), "version": fact.get("version"),
            "runtime_status": fact["status"], "runtime_probe": fact,
            "install_strategy": "锁定提交→源码校验；provider依赖与账号按需配置",
            "note": "源码就绪不等于视频可生成；provider依赖、FFmpeg和账号另配，"
                    "以 video_provider_status() 的实际结果为准"}
    item.pop("external_agent_key", None)
    if fact["declared"]:
        item["external_agent_key"] = "openmontage"
    return item


def all_items(uid: Optional[str] = None) -> list:
    """六层合并:内置 < KM < ecosystem < 外部agent < 官方扩展 < 用户私有。"""
    merged = {}
    for it in BUILTINS:
        merged[it["id"]] = {**it, "origin": "official"}
    # KM 装备总库 97 项(权威分类蓝本·数据驱动)
    km_items = _arsenal_as_install_items()
    for it in km_items:
        merged[it["id"]] = {**it, "origin": "official"}
    # ecosystem 19项目一个不漏;KM已有同名的不重复
    for it in _ecosystem_as_install_items(km_items):
        merged[it["id"]] = {**it, "origin": "official"}
    # 用户点名的外部 agent：独立单一清单，安装后可动态探测，不再只停留在 KM 展示项。
    for it in _external_agents_as_install_items():
        merged[it["id"]] = it
    # 官方扩展(旧 install_registry.json 也并到这层·共享)
    for path in (_OFFICIAL_PATH, _REG_PATH):
        for it in _load_json_list(path):
            if isinstance(it, dict) and it.get("id"):
                merged[it["id"]] = {**it, "origin": "official"}
    # 用户私有(最高优先·可覆盖官方·标 user)
    for it in _load_json_list(_user_path(uid)):
        if isinstance(it, dict) and it.get("id"):
            merged[it["id"]] = {**it, "origin": "user"}
    # 旧目录/KM/外部成员 ID 仍可安装，但展示与按场景安装只能有同一个源码项。
    openmontage = merged.get("repo:openmontage")
    for key in list(merged):
        if key != "repo:openmontage" and _is_openmontage_item(merged[key]):
            del merged[key]
    merged["repo:openmontage"] = _openmontage_install_item(openmontage)
    return list(merged.values())


def register_item(item: dict, scope: str = "user", uid: Optional[str] = None) -> dict:
    """注册/更新安装项。scope='user'(默认·存自己的一份·不污染官方) / 'official'(官方扩展·共享)。

    用户'官方一套·自己也有一套':默认进用户私有;下一步官方批量东西用 scope='official'。
    """
    required = ("id", "name", "kind", "scenes", "check")
    missing = [k for k in required if not item.get(k)]
    if missing:
        return {"ok": False, "error": f"缺字段: {missing}"}
    if not isinstance(item.get("scenes"), list) or not isinstance(item.get("check"), dict):
        return {"ok": False, "error": "scenes需列表·check需对象"}
    cmd = item.get("install") or []
    if cmd and (not isinstance(cmd, list) or os.path.basename(str(cmd[0])) not in _ALLOWED_INSTALLERS):
        return {"ok": False, "error": "install必须是白名单安装器argv(pip/npm/npx/brew/git),不接受任意shell"}
    path = _OFFICIAL_PATH if scope == "official" else _user_path(uid)
    cur = _load_json_list(path)
    cur = [x for x in cur if x.get("id") != item["id"]]
    cur.append(item)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _atomic_write_json(path, cur)
        # 新注册的工具立即对全军团可见(军团 dogfood 揪出:缓存不失效→新工具1h隐形→重复造轮子)
        try:
            import toolbox
            toolbox.invalidate_cache()
        except Exception:
            pass
        _ARSENAL_CACHE["items"] = None  # all_items 六层合并需重算(新项要出现在清单)
        invalidate_install_cache()
        return {"ok": True, "item": item, "scope": scope, "total": len(cur)}
    except Exception as e:
        return {"ok": False, "error": f"保存失败:{e}"}


def unregister_item(item_id: str, uid: Optional[str] = None) -> dict:
    """删除一个项——只能删用户私有层(官方内置/扩展不可删·保护)。"""
    path = _user_path(uid)
    cur = _load_json_list(path)
    if not any(x.get("id") == item_id for x in cur):
        return {"ok": False, "error": f"{item_id} 不在你的私有注册表(官方项不可删·只能覆盖)"}
    cur = [x for x in cur if x.get("id") != item_id]
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _atomic_write_json(path, cur)
        invalidate_install_cache()
        return {"ok": True, "removed": item_id, "remaining": len(cur)}
    except Exception as e:
        return {"ok": False, "error": f"删除失败:{e}"}


def _repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def is_installed(item: dict, use_cache: bool = True) -> bool:
    """探测某项是否已装。use_cache=True(默认)走 90s TTL 缓存(scan 场景批量调·避免重复探针);
    装/卸后 invalidate_install_cache() 清空。单独校验(装完即时确认)传 use_cache=False。"""
    if _is_openmontage_item(item):
        return bool(openmontage_source_status()["ok"])
    item_id = item.get("id")
    if use_cache and item_id:
        c = _INSTALLED_CACHE.get(item_id)
        if c is not None and (time.time() - c["ts"]) < _INSTALLED_TTL:
            return c["val"]
    val = _is_installed_probe(item)
    if item_id:
        _INSTALLED_CACHE[item_id] = {"val": val, "ts": time.time()}
    return val


def _is_installed_probe(item: dict) -> bool:
    if _is_openmontage_item(item):
        return bool(openmontage_source_status()["ok"])
    if item.get("external_agent_key"):
        try:
            import external_agent_registry as registry
            return bool(registry.probe(item["external_agent_key"]).get("installed"))
        except Exception:
            return False
    c = item.get("check") or {}
    method, target = c.get("method"), str(c.get("target") or "")
    if method == "cli":
        return bool(shutil.which(target))
    if method == "import":
        # 当前解释器(python3.12)能 import → 已装
        try:
            if importlib.util.find_spec(target) is not None:
                return True
        except Exception:
            pass
        try:
            importlib.metadata.version(item.get("id", "").split(":", 1)[-1])
            return True
        except Exception:
            pass
        # 回退1:同名 CLI 命令在?(如 yt-dlp 装在别的 python 但暴露了 CLI·之前漏检根因)
        for cli_name in {target, target.replace("_", "-"),
                         str(item.get("name") or ""),
                         str(item.get("name") or "").replace("_", "-")}:
            if cli_name and shutil.which(cli_name):
                return True
        # 回退2:别的 python(3.14/3.11)能 import?(多解释器·之前只看 3.12)
        for py in ("python3.14", "python3.11", "python3.13"):
            p = shutil.which(py)
            if not p:
                continue
            try:
                r = subprocess.run([p, "-c", f"import {target}"],
                                   capture_output=True, timeout=8)
                if r.returncode == 0:
                    return True
            except Exception:
                continue
        return False
    if method == "path":
        p = os.path.expanduser(target)
        if not os.path.isabs(p):
            p = os.path.join(_repo_root(), p)
        return os.path.exists(p)
    if method == "playwright_browser":
        import glob
        pats = [os.path.expanduser("~/Library/Caches/ms-playwright/chromium-*"),
                os.path.expanduser("~/.cache/ms-playwright/chromium-*")]
        return any(glob.glob(p) for p in pats)
    if method == "npm":
        try:
            r = subprocess.run(["npm", "list", "-g", target, "--depth=0"],
                               capture_output=True, timeout=20)
            return r.returncode == 0
        except Exception:
            return False
    if method == "note":
        # 装备总库远程项:无本地源码/无安装探针 → 视为未安装(用户按需真装再补 install)
        return False
    return False


def discover_repo() -> dict:
    """自动发现仓库里还没登记的 skill包/Node项目/Python项目/开源agent。

    用户要"彻底查看"——不靠手写清单。只读扫描,不自动安装;发现结果可由 register_item
    接纳。按项目聚合(不把796个SKILL逐个铺开造成噪声),每个skill包带count。
    """
    root = _repo_root()
    found = []
    # 1) ecosystem 每个目录=开源 agent/库/组件(只读顶层文件·不递归扫)
    eco = os.path.join(root, "harness_baselines", "ecosystem")
    if os.path.isdir(eco):
        for name in sorted(os.listdir(eco)):
            p = os.path.join(eco, name)
            if not os.path.isdir(p):
                continue
            has_npm = os.path.isfile(os.path.join(p, "package.json"))
            has_py = os.path.isfile(os.path.join(p, "pyproject.toml")) or os.path.isfile(os.path.join(p, "setup.py"))
            scenes = []
            low = name.lower()
            for sc, kws in {"video":["montage","video"],"audio":["audio","speech"],
                            "vision":["vision","cua"],"web":["web","browser"],
                            "agent":["agent","herdr","omnigent","openhuman","orca"],
                            "product":["pm-"],"skill":["skill","superpower"]}.items():
                if any(k in low for k in kws): scenes.append(sc)
            if not scenes: scenes=["agent"]
            found.append({"id": f"discovered:ecosystem:{name}", "name": name,
                          "kind": "repo", "scenes": scenes, "shared": True,
                          "source": os.path.relpath(p, root), "installed": True,
                          "discover": {"npm": has_npm, "python": has_py},
                          "note": f"开源项目·{'含npm ' if has_npm else ''}{'含python ' if has_py else ''}源码随仓库"})
    # 2) skill 按来源聚合——复用 toolbox 已扫结果(它有prune·不重复扫整仓)
    try:
        import toolbox
        # 这是用户显式要求“彻底发现”的入口：必须拿完整 Skill 清单，不能把冷启动 partial
        # 核心目录误当成“仓库没有 Skill”。只扫 Skill（实测约数秒），不触发45秒全工具箱构建。
        groups = {}
        inventory = getattr(toolbox, "skill_inventory", None) or toolbox._scan_skills
        for it in inventory():
            if it.get("kind") != "skill":
                continue
            src = it.get("source_harness") or "其它"
            groups[src] = groups.get(src, 0) + 1
        for src, n in sorted(groups.items(), key=lambda kv: -kv[1]):
            found.append({"id": f"discovered:skills:{src}", "name": f"{src} 技能包",
                          "kind": "skill_pack", "scenes": ["skill", "agent"], "shared": True,
                          "source": src, "installed": True, "discover": {"skills": n},
                          "note": f"{n} 个 skill·本地已存在·可被兼容 harness 共用"})
    except Exception:
        pass
    return {"items": found, "summary": {
        "projects": sum(1 for x in found if x["kind"] == "repo"),
        "skill_packs": sum(1 for x in found if x["kind"] == "skill_pack"),
        "skills_total": sum((x.get("discover") or {}).get("skills", 0) for x in found),
        "total": len(found)}}


# 搜索源领域归类(给用户看的分组·基于源名)
_SEARCH_DOMAIN = {
    "web": ["ddgs", "brave", "tavily", "anysearch", "sonar", "aisa", "bing", "exa", "jina", "baidu", "panel"],
    "academic": ["openalex", "arxiv", "crossref", "pubmed", "scholar", "musicbrainz", "pubchem"],
    "social": ["reddit", "xiaohongshu", "hackernews", "x_oembed", "blogspot"],
    "professional": ["courtlistener", "gdelt", "wikidata", "wikipedia"],
    "archive": ["wayback", "archive_today", "browser_history"],
}
# 需 key 的源 → 对应环境变量(用户配了才启用·不配自动跳过)
_SEARCH_KEY_ENV = {
    "sonar": "AISA_API_KEY", "sonar_pro": "AISA_API_KEY",
    "aisa_smart": "AISA_API_KEY", "aisa_verity": "AISA_API_KEY",
    "tavily": "TAVILY_API_KEY", "anysearch": "ANYSEARCH_API_KEY",
}


def _search_domain(name: str) -> str:
    low = name.lower()
    for dom, kws in _SEARCH_DOMAIN.items():
        if any(k in low for k in kws):
            return dom
    return "web"


def _read_source_registry() -> dict:
    """子进程隔离读 search_fabric.SOURCE_REGISTRY(不污染本进程)。"""
    sf_dir = os.path.join(_repo_root(), "seed-users-system", "v11", "evolution")
    probe = (
        "import sys,json; sys.path.insert(0,%r)\n"
        "import search_fabric as SF\n"
        "reg=getattr(SF,'SOURCE_REGISTRY',{})\n"
        "print(json.dumps({n:{'desc':(s.get('desc') or '')[:80],'tier':s.get('tier'),"
        "'needs_key':bool(s.get('needs_key'))} for n,s in reg.items()},ensure_ascii=False))\n"
    ) % sf_dir
    try:
        r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=30)
        for line in reversed((r.stdout or "").splitlines()):
            if line.strip().startswith("{"):
                return json.loads(line)
    except Exception:
        pass
    return {}


def _read_search_engine_methods() -> list:
    """正则读 search_engine.py 的 def search_* 方法名(不import·避副作用)。"""
    import re
    p = os.path.join(_repo_root(), "seed-users-system", "search_engine.py")
    try:
        txt = open(p, "r", encoding="utf-8", errors="ignore").read()
    except Exception:
        return []
    return sorted(set(re.findall(r"def (search_[a-z0-9_]+)\(", txt)))


def discover_search_sources() -> dict:
    """认真扒全 5 大搜索体系的所有引擎/源(用户批评'没认真看·friday是整体API·搜狗呢')。

    体系(整体API/入口)→ 引擎(底下多个)两级。源码是唯一事实源·扫全部入口文件·去重。
    """
    ZH_SOCIAL = "中文社区"
    systems = []

    # ① Friday universal API(美团 agi.sankuai.com·一个token多引擎·整体网关)
    friday_engines = [
        ("bing-search-v3", "web", "Bing v3·英文技术源强·$12.5/千·秒返", True),
        ("bing", "web", "Bing 标准", True),
        ("google-search", "web", "Google·全球覆盖·$5/千·日限1w", True),
        ("zhipu-search", ZH_SOCIAL, "智谱 search_std·中文强·秒返", True),
        ("baidu-search-v2", "web", "百度 v2·RPM未开(配额开即用)", False),
        ("tencent-search", "web", "腾讯搜索·RPM未开", False),
        ("bocha-search", "web", "博查搜索·RPM未开", False),
        ("quark-search", "web", "夸克搜索·RPM未开", False),
        ("xhc", ZH_SOCIAL, "小红书(内部源)", True),
        ("zh", ZH_SOCIAL, "知乎(内部源)", True),
        ("dpNote", ZH_SOCIAL, "大众点评笔记(内部源)", True),
        ("hn", "tech", "HackerNews(algolia直连)", True),
    ]
    systems.append({"system": "Friday universal API", "gateway": True,
                    "desc": "美团 agi.sankuai.com/universal-search·一个token多引擎",
                    "key_env": "MEITUAN_UNIVERSAL_SEARCH_TOKEN / FRIDAY_APP_ID",
                    "engines": [{"name": n, "domain": d, "desc": ds, "ready": rdy}
                                for n, d, ds, rdy in friday_engines]})

    # ② search_fabric(我方多源 fanout + bandit 路由)
    reg = _read_source_registry()
    fab_extra = ["openalex", "arxiv", "crossref", "pubmed", "pubchem", "gdelt",
                 "courtlistener", "musicbrainz", "wikidata", "x_oembed",
                 "wayback_cdx", "archive_today", "blogspot_feed", "browser_history"]
    fab_engines = []
    for n, m in reg.items():
        env = _SEARCH_KEY_ENV.get(n)
        fab_engines.append({"name": n, "domain": _search_domain(n), "desc": m.get("desc", ""),
                            "needs_key": bool(m.get("needs_key")), "key_env": env,
                            "ready": (not m.get("needs_key")) or bool(os.environ.get(env or "", "").strip())})
    for n in fab_extra:
        fab_engines.append({"name": n, "domain": _search_domain(n), "desc": n.replace("_", " "),
                            "needs_key": False, "ready": True})
    systems.append({"system": "search_fabric", "gateway": False,
                    "desc": "我方多源并行 fanout + bandit 智能路由 + 配额池",
                    "engines": fab_engines})

    # ③ search_engine.py(站内综合·含搜狗微信/csdn/github系列)
    se_methods = _read_search_engine_methods()
    se_engines = []
    for m in se_methods:
        name = m.replace("search_", "")
        dom = ZH_SOCIAL if any(k in name for k in ("sogou", "csdn", "juejin", "zhihu", "xiaohongshu", "sspai", "bilibili")) else _search_domain(name)
        desc = "搜狗微信·公众号干货长文" if "sogou" in name else name.replace("_", " ")
        se_engines.append({"name": name, "domain": dom, "desc": desc, "ready": True})
    systems.append({"system": "search_engine", "gateway": False,
                    "desc": "站内综合搜索(搜狗微信/CSDN/GitHub/学城/arxiv/exa…)",
                    "engines": se_engines})

    # ④ SOURCE_CATALOG(CDP/direct 社区源)
    cat = _read_source_catalog()
    systems.append({"system": "社区源(SOURCE_CATALOG)", "gateway": False,
                    "desc": "CDP/直连社区源(知乎/小红书/B站/V2EX/掘金/搜狗微信…)",
                    "engines": [{"name": n, "domain": ZH_SOCIAL if n in
                                 ("zhihu", "xiaohongshu", "sspai", "v2ex", "juejin", "bilibili", "sogou_weixin", "csdn")
                                 else _search_domain(n), "desc": (m.get("desc") or n), "ready": True}
                                for n, m in cat.items()]})

    # 汇总:去重统计(同名引擎跨体系合并计数)
    all_names = set()
    by_domain = {}
    total_engines = 0
    for sysd in systems:
        for e in sysd["engines"]:
            total_engines += 1
            all_names.add(e["name"])
            by_domain[e["domain"]] = by_domain.get(e["domain"], 0) + 1
    # 兼容旧字段(sources 扁平清单·前端旧逻辑仍可用)
    flat = []
    for sysd in systems:
        for e in sysd["engines"]:
            flat.append({"id": f"search:{sysd['system']}:{e['name']}", "name": e["name"],
                         "kind": "search_source", "system": sysd["system"],
                         "domain": e["domain"], "desc": e["desc"],
                         "needs_key": e.get("needs_key", False), "ready": e.get("ready", True),
                         "scenes": ["search", e["domain"]], "shared": True})
    return {"systems": systems, "sources": flat, "summary": {
        "systems": len(systems), "total_engines": total_engines,
        "unique_engines": len(all_names), "total": total_engines,
        "free": sum(1 for e in flat if not e["needs_key"]),
        "needs_key": sum(1 for e in flat if e["needs_key"]),
        "ready": sum(1 for e in flat if e["ready"]),
        "by_domain": by_domain}}


_ENV_CACHE = os.path.join(_LEGION_DIR, "env_discovery_cache.json")
# 场景分类关键词(按包名/描述自动归类·未知=general不瞎猜)
_ENV_SCENE_KW = {
    "search": ["search", "tavily", "exa", "firecrawl", "serp", "ddgs", "duckduckgo", "sonar", "aisa", "anysearch"],
    "web": ["browser", "playwright", "opencli", "crawl", "scrap", "http", "requests", "aiohttp", "webclaw"],
    "vision": ["vision", "opencv", "cv2", "image", "ocr", "tesseract", "pillow", "zbar"],
    "video": ["video", "ffmpeg", "montage", "moviepy"],
    "audio": ["audio", "speech", "whisper", "voice", "tts"],
    "data": ["pandas", "duckdb", "numpy", "openpyxl", "datasets", "plotly", "lifelines", "arrow"],
    "pdf": ["pdf", "pdfplumber", "pypdf"],
    "agent": ["agent", "deepagents", "hermes", "codex", "dsh", "penguin", "prime", "claude", "cua", "orca", "omnigent"],
    "mcp": ["mcp", "modelcontextprotocol", "fastmcp"],
    "devops": ["docker", "colima", "redis", "postgres", "mysql", "gh", "git", "gradle", "maven", "ollama", "node"],
    "code": ["ripgrep", "py-spy", "zig", "go", "openjdk", "stockfish"],
}


def _env_scene(name: str, desc: str = "") -> list:
    blob = f"{name} {desc}".lower()
    scenes = [sc for sc, kws in _ENV_SCENE_KW.items() if any(k in blob for k in kws)]
    return scenes or ["general"]


def _run_probe(cmd: list, timeout_s: int = 25) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        return r.stdout or ""
    except Exception:
        return ""


def discover_environment(force: bool = False) -> dict:
    """全域发现本机实际装的:pip顶层/npm全局/brew leaves/GitHub仓库(用户'把所有装的都注册好')。

    只列顶层(用户装的·非底层依赖噪声)。缓存5分钟(别每次开面板跑几十条命令)。
    GitHub 只扫白名单根目录·只读 .git/config remote(不含token)。
    """
    if not force and os.path.isfile(_ENV_CACHE):
        try:
            d = json.load(open(_ENV_CACHE, "r", encoding="utf-8"))
            if time.time() - d.get("scanned_at", 0) < 300:
                d["from_cache"] = True
                return d
        except Exception:
            pass
    items = []
    _seen_pip = {}  # name.lower() -> item(多 python 去重·合并 python 来源)

    # 1) pip 顶层包 —— 扫【所有】本机 python(3.12/3.14/3.11…),不止 sys.executable。
    #   之前只扫 python3.12 → 漏掉 3.14/--user 装的(如 yt-dlp 在 3.14)。这是漏检根因。
    _pythons = []
    for cand in (sys.executable, "python3.14", "python3.13", "python3.12", "python3.11", "python3"):
        p = shutil.which(cand) if not os.path.isabs(cand) else (cand if os.path.exists(cand) else None)
        if p and os.path.realpath(p) not in [os.path.realpath(x) for x in _pythons]:
            _pythons.append(p)
    for py in _pythons:
        pyver = os.path.basename(py)
        try:
            out = _run_probe([py, "-m", "pip", "list", "--not-required", "--format=json"], 30)
            for p in (json.loads(out) if out.strip().startswith("[") else []):
                nm, ver = p.get("name"), p.get("version")
                if not nm:
                    continue
                key = nm.lower()
                if key in _seen_pip:
                    # 已见过(别的 python)——记录多来源,不重复列
                    src = _seen_pip[key]["source"]
                    if pyver not in src:
                        _seen_pip[key]["source"] = f"{src}+{pyver}"
                    continue
                it = {"id": f"pip:{nm}", "name": nm, "kind": "pip", "version": ver,
                      "description": "", "scenes": _env_scene(nm), "installed": True,
                      "shared": True, "origin": "environment", "source": f"pip({pyver})",
                      "install": [py, "-m", "pip", "install", "--break-system-packages", nm]}
                _seen_pip[key] = it
                items.append(it)
        except Exception:
            continue

    # 2) npm 全局顶层
    try:
        out = _run_probe(["npm", "ls", "-g", "--depth=0", "--json"], 25)
        deps = (json.loads(out).get("dependencies", {}) if out.strip().startswith("{") else {})
        for nm, meta in deps.items():
            ver = meta.get("version") if isinstance(meta, dict) else ""
            items.append({"id": f"npm:{nm}", "name": nm, "kind": "npm", "version": ver,
                          "description": "", "scenes": _env_scene(nm), "installed": True,
                          "shared": True, "origin": "environment", "source": "npm 全局",
                          "install": ["npm", "install", "-g", nm]})
    except Exception:
        pass

    # 3) brew 顶层 formula
    try:
        out = _run_probe(["brew", "leaves"], 20)
        for line in out.splitlines():
            nm = line.strip()
            if not nm:
                continue
            items.append({"id": f"brew:{nm}", "name": nm.split("/")[-1], "kind": "brew", "version": "",
                          "description": "", "scenes": _env_scene(nm), "installed": True,
                          "shared": True, "origin": "environment", "source": "Homebrew",
                          "install": ["brew", "install", nm]})
    except Exception:
        pass

    # 3.5) ~/.local/bin 及常见 bin 目录的 CLI(pip --user / 独立脚本装的·之前完全没扫)
    #   yt-dlp/browser-act/tvly/uv… 都在这·漏了会误判"没装"。已被 pip/npm 收录的名字去重。
    _known = {it["name"].lower() for it in items}
    for bindir in (os.path.expanduser("~/.local/bin"),
                   os.path.expanduser("~/.npm-global/bin")):
        if not os.path.isdir(bindir):
            continue
        try:
            for fn in sorted(os.listdir(bindir)):
                fp = os.path.join(bindir, fn)
                # 只收可执行文件·跳过已知包/隐藏/明显非工具(python解释器本身/.json)
                if (fn.lower() in _known or fn.startswith(".") or fn.endswith(".json")
                        or fn.startswith("python") or not os.access(fp, os.X_OK)):
                    continue
                _known.add(fn.lower())
                items.append({"id": f"cli:{fn}", "name": fn, "kind": "cli", "version": "",
                              "description": f"本机CLI命令({bindir})", "scenes": _env_scene(fn),
                              "installed": True, "shared": True, "origin": "environment",
                              "source": f"CLI({os.path.basename(bindir)})", "install": []})
        except Exception:
            continue

    # 4) GitHub 仓库(白名单根目录·只读 remote)
    roots = [_repo_root(), os.path.expanduser("~/.claude/plugins")]
    seen_repo = set()
    for root in roots:
        if not os.path.isdir(root):
            continue
        for base, dirs, _f in os.walk(root):
            dirs[:] = [d for d in dirs if d not in ("node_modules", ".venv", "venv")]
            if ".git" in dirs:
                cfg = os.path.join(base, ".git", "config")
                remote = ""
                try:
                    for ln in open(cfg, "r", encoding="utf-8", errors="ignore"):
                        ln = ln.strip()
                        if ln.startswith("url ="):
                            remote = ln.split("=", 1)[1].strip()
                            break
                except Exception:
                    pass
                # 只收 github 来源(用户说"github装的")·去 token
                if "github" not in remote.lower():
                    continue
                remote = re.sub(r"https://[^@/]+@", "https://", remote)
                nm = os.path.basename(base)
                if nm in seen_repo:
                    continue
                seen_repo.add(nm)
                items.append({"id": f"github:{nm}", "name": nm, "kind": "github_repo", "version": "",
                              "description": remote, "scenes": _env_scene(nm), "installed": True,
                              "shared": True, "origin": "environment", "source": "GitHub 仓库",
                              "path": os.path.relpath(base, _repo_root()) if base.startswith(_repo_root()) else base,
                              "install": ["git", "clone", remote]})
                dirs[:] = []  # 是 git 仓库根·不再深入(避免子模块重复)

    by_kind = {}
    for it in items:
        by_kind[it["kind"]] = by_kind.get(it["kind"], 0) + 1
    by_scene = {}
    for it in items:
        for sc in it["scenes"]:
            by_scene[sc] = by_scene.get(sc, 0) + 1
    result = {"items": items, "summary": {"total": len(items), "by_kind": by_kind,
              "by_scene": dict(sorted(by_scene.items(), key=lambda kv: -kv[1]))},
              "scanned_at": time.time(), "from_cache": False}
    try:
        os.makedirs(_LEGION_DIR, exist_ok=True)
        _atomic_write_json(_ENV_CACHE, result)
    except Exception:
        pass
    return result


def _read_source_catalog() -> dict:
    """子进程读 global_sources.SOURCE_CATALOG。"""
    gs_dir = os.path.join(_repo_root(), "seed-users-system", "v11")
    probe = (
        "import sys,json; sys.path.insert(0,%r)\n"
        "import global_sources as G\n"
        "cat=getattr(G,'SOURCE_CATALOG',{})\n"
        "print(json.dumps({n:{'desc':(m.get('desc') or '')[:50],'reach':m.get('reach')} "
        "for n,m in cat.items() if isinstance(m,dict)},ensure_ascii=False))\n"
    ) % gs_dir
    try:
        r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=30)
        for line in reversed((r.stdout or "").splitlines()):
            if line.strip().startswith("{"):
                return json.loads(line)
    except Exception:
        pass
    return {}


def _refresh_scan(scene: Optional[str], key: str) -> None:
    try:
        scan(scene, use_cache=False)
    finally:
        with _SCAN_LOCK:
            _SCAN_REFRESHING.discard(key)


def _scanned_install_item(item: dict) -> dict:
    return {**item, "installed": is_installed(item),
            "installable": bool(item.get("install") or item.get("external_agent_key")),
            # 外部 registry 生成固定源码计划，不在这里伪造安装 shell argv。
            "install_command": (" ".join(str(a) for a in (item.get("install") or [])) or
                                (f"external_agent_registry.install({item['external_agent_key']!r})"
                                 if item.get("external_agent_key") else None))}


def scan(scene: Optional[str] = None, use_cache: bool = True) -> dict:
    key = scene or "__all__"
    out = None
    if use_cache:
        with _SCAN_LOCK:
            c = _SCAN_CACHE.get(key)
            if c is not None:
                fresh = (time.time() - c["ts"]) < _SCAN_TTL
                if not fresh and key not in _SCAN_REFRESHING:
                    _SCAN_REFRESHING.add(key)
                    threading.Thread(target=_refresh_scan, args=(scene, key), daemon=True,
                                     name=f"install-scan-{key}").start()
                out = dict(c["data"])
                out["from_cache"] = True
                out["stale"] = not fresh
        if out is not None:
            # 其余项目仍用原 SWR 缓存；OpenMontage 的换根/丢源码必须即时可见。
            out["items"] = [
                _scanned_install_item(_openmontage_install_item(item)) if _is_openmontage_item(item)
                else item for item in out["items"]
            ]
            out["summary"] = {**out["summary"],
                              "installed": sum(1 for x in out["items"] if x["installed"]),
                              "missing": sum(1 for x in out["items"] if not x["installed"])}
            return out
    items = []
    for it in all_items():
        if scene and scene not in (it.get("scenes") or []) and "all" not in (it.get("scenes") or []):
            continue
        items.append(_scanned_install_item(it))
    out = {"items": items, "summary": {"total": len(items),
           "installed": sum(1 for x in items if x["installed"]),
           "missing": sum(1 for x in items if not x["installed"]),
           "shared": sum(1 for x in items if x.get("shared")),
           "scenes": sorted({s for x in items for s in x.get("scenes", [])})},
           "scene": scene}
    with _SCAN_LOCK:
        _SCAN_CACHE[key] = {"data": out, "ts": time.time()}
    return dict(out)


def _safe_command(cmd: list) -> bool:
    return bool(cmd) and os.path.basename(str(cmd[0])) in _ALLOWED_INSTALLERS


def install(item_id: str, confirm: bool | None = None, timeout_s: int = 600,
            capability_policy: Optional[dict] = None) -> dict:
    """单独安装。

    产品军团默认允许白名单安装器自主安装；显式 ``confirm=False`` 仍可用于安装中心预览。
    只有 git push、PR、release、公开部署由外层确认；安装本身默认自治。
    """
    if item_id in _OPENMONTAGE_INSTALL_IDS:
        item_id = "repo:openmontage"
    if confirm is None:
        try:
            from legion_permissions import capability_policy as _default_policy
            policy = capability_policy or _default_policy()
        except Exception:
            policy = capability_policy or {}
        confirm = bool(policy.get("install"))
    item = next((x for x in all_items() if x.get("id") == item_id), None)
    if not item:
        return {"ok": False, "error": f"未知安装项:{item_id}"}
    if item.get("external_agent_key"):
        try:
            import external_agent_registry as registry
            return registry.install(item["external_agent_key"], confirm=confirm, timeout_s=timeout_s)
        except Exception as e:
            return {"ok": False, "status": "failed", "item": item_id,
                    "error": f"外部 agent 安装入口失败:{type(e).__name__}:{e}"}
    if is_installed(item):
        return {"ok": True, "status": "already_installed", "item": item_id}
    cmd = item.get("install") or []
    if not cmd:
        return {"ok": False, "status": "manual", "item": item_id,
                "error": "该项需按官方文档手动安装", "note": item.get("note")}
    if not _safe_command(cmd):
        return {"ok": False, "error": "安装命令不在安全白名单"}
    if not confirm:
        return {"ok": True, "status": "planned", "item": item_id,
                "command": " ".join(str(x) for x in cmd), "note": "dry-run·确认后才执行"}
    try:
        run_env = None
        if os.path.basename(str(cmd[0])) == "git":
            try:
                import github_access
                run_env = github_access.git_environment()
            except Exception:
                pass
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s,
                           cwd=_repo_root(), env=run_env)
        if r.returncode == 0:
            # 装成功→新工具立即对全军团可见(军团 dogfood 揪出的缓存不失效 bug)
            try:
                import toolbox
                toolbox.invalidate_cache()
            except Exception:
                pass
            invalidate_install_cache()  # scan/is_installed 缓存失效·下次 fetch 拿真实"已装"
        return {"ok": r.returncode == 0, "status": "installed" if r.returncode == 0 else "failed",
                "item": item_id, "exit_code": r.returncode,
                "stdout": (r.stdout or "")[-1200:], "stderr": (r.stderr or "")[-1200:],
                "installed": is_installed(item, use_cache=False)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "status": "timeout", "item": item_id,
                "error": f"安装超时({timeout_s}s)"}
    except Exception as e:
        return {"ok": False, "status": "failed", "item": item_id,
                "error": f"{type(e).__name__}:{e}"}


def install_scene(scene: str, confirm: bool | None = None,
                  capability_policy: Optional[dict] = None) -> dict:
    """按场景安装；产品军团默认按 policy 自主安装，显式 false 仍是 dry-run。"""
    candidates = [x for x in all_items()
                  if scene in (x.get("scenes") or []) and not is_installed(x)]
    results = []
    for item in candidates:
        results.append(install(item["id"], confirm=confirm, capability_policy=capability_policy))
    return {"scene": scene, "confirm": confirm, "count": len(candidates),
            "results": results, "ok": all(r.get("ok") for r in results) if results else True}


# ── 官方军团统一升级广播(用户'官方装新东西→其他人收通知+都装好→升级面板') ──
def publish_official(item: dict, changelog: str = "") -> dict:
    """官方发布一个新能力:写 official 层 + bump manifest version + 追加 changelog。

    发布后所有用户 check_upgrades 就能看到"军团升级了·新增X"。走 register_item 的安全校验。
    """
    reg = register_item(item, scope="official")
    if not reg.get("ok"):
        return reg
    mani = _load_manifest()
    mani["version"] = int(mani.get("version", 0)) + 1
    mani["released_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    items = [x for x in mani.get("items", []) if x.get("id") != item["id"]]
    entry = {k: item.get(k) for k in ("id", "name", "kind", "scenes", "install", "check",
                                      "shared", "note")}
    entry["since_version"] = mani["version"]
    items.append(entry)
    mani["items"] = items
    cl = mani.get("changelog", [])
    cl.append({"version": mani["version"], "at": mani["released_at"],
               "text": changelog or f"新增 {item.get('name')}", "item_id": item["id"]})
    mani["changelog"] = cl[-50:]
    try:
        os.makedirs(_LEGION_DIR, exist_ok=True)
        _atomic_write_json(_MANIFEST_PATH, mani)
    except Exception as e:
        return {"ok": False, "error": f"manifest 保存失败:{e}"}
    return {"ok": True, "version": mani["version"], "published": item["id"],
            "changelog": changelog or f"新增 {item.get('name')}"}


def _load_upgrade_state(uid: Optional[str] = None) -> dict:
    try:
        return json.load(open(_upgrade_state_path(uid), "r", encoding="utf-8"))
    except Exception:
        return {"acked_version": 0, "installed_ids": [], "failed_ids": []}


def _save_upgrade_state(st: dict, uid: Optional[str] = None):
    p = _upgrade_state_path(uid)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    _atomic_write_json(p, st)


def get_activity_feed(uid: Optional[str] = None) -> dict:
    """军团动态:升级记录按天聚合成可持续查看的时间线 + 今日是否有未读(每日一次提醒)。

    不论加什么(skill/agent/工具/搜索源)publish 时都进 changelog·动态流自动收录。
    简洁:当天多次加东西聚合成"今日N项";看过(mark_seen)当天不再提醒·隔天新增才再提醒。
    """
    mani = _load_manifest()
    st = _load_upgrade_state(uid)
    today = time.strftime("%Y-%m-%d")
    last_seen = st.get("last_seen_date", "")
    # changelog 按天分组(新→旧)
    days = {}
    for c in mani.get("changelog", []):
        day = (c.get("at") or "")[:10] or "更早"
        days.setdefault(day, []).append({"version": c.get("version"), "text": c.get("text"),
                                         "item_id": c.get("item_id"), "at": c.get("at")})
    feed = [{"date": d, "count": len(items), "items": items}
            for d, items in sorted(days.items(), reverse=True)]
    today_items = days.get(today, [])
    # 今日未读 = 有今日动态 且 今天还没标记看过
    today_unread = len(today_items) if last_seen != today else 0
    return {"feed": feed, "today": today, "today_count": len(today_items),
            "today_unread": today_unread, "has_unread": today_unread > 0,
            "total_days": len(feed), "official_version": mani.get("version", 0)}


def mark_activity_seen(uid: Optional[str] = None) -> dict:
    """标记今日动态已读(每日节流:当天不再提醒·隔天有新才再提醒)。"""
    st = _load_upgrade_state(uid)
    st["last_seen_date"] = time.strftime("%Y-%m-%d")
    _save_upgrade_state(st, uid)
    return {"ok": True, "seen_date": st["last_seen_date"]}


def check_upgrades(uid: Optional[str] = None) -> dict:
    """用户端升级检测:官方 manifest 版本 vs 用户已知版本 → 有几项新升级/changelog/装没装。"""
    mani = _load_manifest()
    st = _load_upgrade_state(uid)
    cur_ver = int(mani.get("version", 0))
    acked = int(st.get("acked_version", 0))
    has_new = cur_ver > acked
    pending = []
    for it in mani.get("items", []):
        installed = is_installed(it)
        if not installed:
            pending.append({"id": it["id"], "name": it["name"], "kind": it.get("kind"),
                            "scenes": it.get("scenes", []), "note": it.get("note", ""),
                            "since_version": it.get("since_version"),
                            "install_command": " ".join(str(a) for a in (it.get("install") or [])) or None,
                            "auto_ok": bool(it.get("install")) and _safe_command(it.get("install") or [])})
    new_changelog = [c for c in mani.get("changelog", []) if c.get("version", 0) > acked]
    return {"official_version": cur_ver, "user_acked_version": acked,
            "has_new": has_new, "new_count": len(new_changelog),
            "changelog": new_changelog, "pending_installs": pending,
            "pending_count": len(pending)}


def apply_upgrade(item_id: str, confirm: bool = False, uid: Optional[str] = None) -> dict:
    """用户装一个官方升级项(走 install 的白名单+confirm)·记状态。"""
    r = install(item_id, confirm=confirm)
    if confirm:
        st = _load_upgrade_state(uid)
        if r.get("ok") and r.get("status") in ("installed", "already_installed"):
            st.setdefault("installed_ids", [])
            if item_id not in st["installed_ids"]:
                st["installed_ids"].append(item_id)
            st["failed_ids"] = [x for x in st.get("failed_ids", []) if x != item_id]
        elif not r.get("ok"):
            st.setdefault("failed_ids", [])
            if item_id not in st["failed_ids"]:
                st["failed_ids"].append(item_id)
        _save_upgrade_state(st, uid)
    return r


def apply_all_upgrades(confirm: bool = False, uid: Optional[str] = None) -> dict:
    """一键升级:安全轻量项(可自动装)全装;重依赖/无命令的列出让用户单独确认。

    区分三种(避免'装0项'误导):新装 / 本机已满足(无需装·仍算升级到位) / 需手动。
    """
    up = check_upgrades(uid)
    mani = _load_manifest()
    results, needs_manual = [], []
    for p in up["pending_installs"]:
        if p["auto_ok"]:
            results.append(apply_upgrade(p["id"], confirm=confirm, uid=uid))
        else:
            needs_manual.append(p)
    # 官方 manifest 里本机已满足的项数(不用装但已到位)——让用户看清升级全貌
    already = sum(1 for it in mani.get("items", []) if is_installed(it))
    if confirm:
        st = _load_upgrade_state(uid)
        st["acked_version"] = up["official_version"]
        _save_upgrade_state(st, uid)
    installed_ok = sum(1 for r in results if r.get("ok"))
    return {"confirm": confirm, "installed": results, "needs_manual": needs_manual,
            "newly_installed": installed_ok, "already_satisfied": already,
            "acked_to_version": up["official_version"] if confirm else None,
            "note": f"新装 {installed_ok} · 本机已具备 {already} · 需手动 {len(needs_manual)}",
            "ok": all(r.get("ok") for r in results) if results else True}


def ack_upgrades(uid: Optional[str] = None) -> dict:
    """用户"知道了"——把已知版本推到当前官方版本(不再重复提示·未装项仍在pending)。"""
    mani = _load_manifest()
    st = _load_upgrade_state(uid)
    st["acked_version"] = int(mani.get("version", 0))
    _save_upgrade_state(st, uid)
    return {"ok": True, "acked_version": st["acked_version"]}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene")
    ap.add_argument("--install")
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--check-openmontage-source", metavar="SOURCE")
    args = ap.parse_args()
    if args.check_openmontage_source is not None:
        out = check_openmontage_source(args.check_openmontage_source)
        print(out["note"] if out["ok"] else out["error"],
              file=sys.stdout if out["ok"] else sys.stderr)
        raise SystemExit(0 if out["ok"] else 1)
    if args.install:
        out = install(args.install, confirm=args.confirm)
    elif args.scene:
        out = install_scene(args.scene, confirm=args.confirm)
    else:
        out = scan()
    print(json.dumps(out, ensure_ascii=False, indent=1))
