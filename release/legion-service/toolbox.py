#!/usr/bin/env python3
"""
toolbox · 军团统一工具箱清册(第0步地基)

用户 2026-08-27 要:把全仓库散落的工具/skill + 军团自己产生的,扫成一张
"可查看、可共享、彼此互看经验"的清单。后面所有能力(多媒体接入/上传交付/
灵感墙/trace查看)都从这张清单取数——一张清单喂全系统(想1步=想系统100步)。

三类来源,统一归一化:
  A. harness 自带 skill  —— 遍历所有 SKILL.md,解析 frontmatter(name/description)
  B. harness 代码工具    —— hermes/tools、OpenMontage/tools/video、dsh-vision 等
  C. 军团自产工具        —— v11/.tool_forge_log.jsonl(军团 forge 出来的)+ evolution/skills

设计原则(对齐纲领):
  - 只读不改:纯扫描,不动任何 harness 源码
  - 给能力不加墙:如实列出每样工具/技能 + 能力标签,让模型/用户自己挑
  - 共享优先:军团自产工具默认共享,带 origin(谁产的·解哪题),彼此可见可复用
  - 可缓存:结果落 ~/.meyo/legion/toolbox_index.json,大目录不必每次全扫
"""
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
# 仓库根:release/legion-service → release → mars-data
_REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
_HB = os.path.join(_REPO, "harness_baselines")
from capstack_bridge import source_root as _retained_source_root
_SEED = _retained_source_root()
_V11 = os.path.join(_SEED, "v11")

import legion_paths as _paths
_CACHE_DIR = _paths.legion_dir()
_CACHE = os.path.join(_CACHE_DIR, "toolbox_index.json")
_CACHE_TTL = 3600.0
_CACHE_LOCK = threading.RLock()
_CACHE_MEMORY: dict = {"data": None, "stamp": None}
_BOOTSTRAP_MEMORY: dict = {"data": None}
_REFRESHING = False
_FULL_SCAN_LOCK = threading.Lock()
_SUMMARY_MEMORY: dict = {"stamp": None, "data": None}

# ── 能力标签:关键词 → 面向用户的能力分类(灵感墙/筛选都用它) ──
_CAP_RULES = [
    ("image_gen",   ["image_gen", "image_generation", "text2image", "生图", "flux", "sdxl"]),
    ("video_gen",   ["video_gen", "video_generation", "veo", "kling", "hunyuan", "wan_video",
                     "minimax", "cogvideo", "jimeng", "ltx_video", "grok_video"]),
    ("video_edit",  ["video_trimmer", "video_stitch", "auto_reframe", "green_screen",
                     "caption_burn", "remotion", "剪辑", "montage"]),
    ("audio",       ["audio", "whisper", "transcribe", "speech", "语音", "voice", "tts"]),
    ("pdf_doc",     ["pdf", "ocr", "long_screenshot", "document", "docx", "ppt", "slides"]),
    ("vision",      ["vision", "视觉", "screenshot", "image-input", "image_qa"]),
    ("search",      ["search", "搜索", "tavily", "perplexity", "serp", "firecrawl", "anysearch"]),
    ("browse",      ["browse", "浏览", "browser", "chrome", "playwright", "web-access"]),
    ("data",        ["data-analysis", "数据", "analytics", "dataframe", "sql", "duckdb", "excel", "csv"]),
    ("writing",     ["prose", "humanizer", "write", "写作", "翻译", "translate", "blog", "narrative"]),
    ("code",        ["code-review", "software-engineering", "重构", "simplif", "codex", "quick_fix"]),
    ("product",     ["pm-", "product", "market", "growth", "go-to-market", "产品", "运营"]),
    ("web_app",     ["web-design", "frontend", "bento", "网页", "app", "site", "nocode"]),
    # ── 新增能力类(auto 主体工具靠这些才被检索到·此前全落 general) ──
    # 找工具/装工具闭环:search_github/install_github_repo/pip_install/cli_hub_*/skill_search/
    #   search_my_toolbox/write_scenario_test/deliver_tool_to_user → 用户"没现成的?去搜个装上"
    ("tool_discovery", ["search_github", "install_github_repo", "pip_install", "cli_hub_",
                        "scenario_test", "deliver_tool_to_user", "search_my_toolbox",
                        "找工具", "装工具", "现成轮子", "工具发现", "tool discovery"]),
    # 多agent编排/子agent:spawn_subagent/spawn_specialist/multi_expert_parallel/delegate/
    #   batch_map/compose_workflow/parallel_sandbox → 用户"派多个专家/子agent并行"
    ("orchestration",  ["subagent", "specialist", "multi_expert", "delegate", "batch_map",
                        "compose_workflow", "parallel_sandbox", "spawn", "子agent", "专家",
                        "并行", "编排", "workflow"]),
    # 自省/进化:self_diagnose/reflect/self_calibrate/recall_experience/remember_lesson/
    #   write_capability/evolution_* → 用户"卡住了自我诊断/反思/进化"
    ("self_improve",   ["self_diagnose", "reflect", "self_calibrate", "recall_experience",
                        "remember_lesson", "write_capability", "evolution", "诊断", "反思",
                        "进化", "自省", "calibrate"]),
    # 桌面自动化:desktop_screenshot/click/type/key/activate_app → 用户"操作桌面/点击窗口"
    ("desktop",        ["desktop_", "screenshot", "桌面", "点击窗口", "activate_app",
                        "自动化操作"]),
    ("mobile",         ["mobile", "android", "ios", "adb", "手机", "真机", "移动设备"]),
    # 计划/目标管理:write_plan/goal_*/insert_plan_step → 用户"制定计划/拆解目标"
    ("planning",       ["write_plan", "goal_create", "insert_plan", "plan_step", "计划",
                        "拆解", "目标管理"]),
]


def _tag_capabilities(*texts: str) -> list:
    """从 name/description/path 里推能力标签(可多个)。"""
    blob = " ".join(t.lower() for t in texts if t)
    tags = [cap for cap, kws in _CAP_RULES if any(k in blob for k in kws)]
    return tags or ["general"]


def _parse_frontmatter(path: str) -> dict:
    """解析 SKILL.md 顶部的 --- YAML frontmatter(只取 name/description,轻量不引 yaml)。"""
    out = {}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            head = f.read(4000)
    except Exception:
        return out
    m = re.match(r"^---\s*\n(.*?)\n---", head, flags=re.S)
    body = m.group(1) if m else head
    for key in ("name", "description", "short_description_zh", "short_description"):
        km = re.search(rf"(?m)^{key}:\s*(.+?)\s*$", body)
        if km:
            out[key] = km.group(1).strip().strip('"').strip("'")
    return out


def _harness_of(path: str) -> str:
    """从路径推它属于哪个 harness/来源。"""
    rel = os.path.relpath(path, _REPO)
    parts = rel.split(os.sep)
    if parts[0] == "harness_baselines":
        if len(parts) > 2 and parts[1] == "ecosystem":
            return f"ecosystem/{parts[2]}"
        return parts[1] if len(parts) > 1 else "harness_baselines"
    if parts[0] == "seed-users-system":
        return "auto(自研)"
    if parts[0] == "release":
        return "legion-service"
    return parts[0]


# ── A. 扫 harness 自带 skill(所有 SKILL.md) ──
# 军团共享安装目录(clone的skill库/repo装这·如anthropic-skills/cybersecurity-skills)
_LEGION_REPOS = _paths.repos_dir()


def _deployed_skill_sources() -> dict[str, str]:
    """Only catalog-declared source roots; never walk the runtime store."""
    try:
        import external_agent_registry as registry
        catalog = registry.load_catalog()
    except Exception:
        return {}
    sources = {}
    for item in catalog:
        runtime = item.get("runtime")
        if not isinstance(runtime, dict) or "deployment_directory" not in runtime:
            continue
        try:
            root = os.path.realpath(registry.source_dir(item))
            if os.path.isdir(root):
                sources.setdefault(root, str(item["key"]))
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            # A broken deployment binding is not a failure of other sources.
            continue
    return sources


def _skill_source_roots(repo_root: str | None = None) -> list[str]:
    repo = os.path.abspath(repo_root or _REPO)
    candidates = [repo, _LEGION_REPOS,
                  os.environ.get("LEGION_EXTERNAL_SOURCE_ROOT", ""),
                  os.path.join(repo, "runtime", "core-sources"),
                  *_deployed_skill_sources()]
    roots, seen = [], set()
    for root in candidates:
        if not root:
            continue
        root = os.path.abspath(root)
        resolved = os.path.realpath(root)
        if resolved not in seen:
            roots.append(root)
            seen.add(resolved)
    return roots


def skill_source_identity(repo_root: str | None = None) -> dict:
    """Bind the existing index to declared roots and immutable release inputs."""
    repo = os.path.abspath(repo_root or _REPO)
    roots = []
    for root in _skill_source_roots(repo):
        try:
            modified = os.stat(root).st_mtime_ns
        except OSError:
            modified = None
        roots.append({"path": os.path.realpath(root), "mtime_ns": modified})
    versions = {}
    for name in ("image-payload.json", "runtime-ref.json"):
        try:
            with open(os.path.join(repo, name), "rb") as stream:
                raw = stream.read(1_000_001)
            versions[name] = (hashlib.sha256(raw).hexdigest()
                              if len(raw) <= 1_000_000 else "oversized")
        except OSError:
            versions[name] = None
    return {"schema": 1, "agent_tool_projection": 2, "roots": roots, "release_inputs": versions}


def _cache_source_valid(data: dict) -> bool:
    return isinstance(data, dict) and data.get("skill_sources") == skill_source_identity()


def _scan_skills() -> list:
    items = []
    # Both the program and configured retained source trees belong in the one
    # existing toolbox index; a timestamp alone cannot establish this coverage.
    roots = [root for root in _skill_source_roots() if os.path.isdir(root)]
    deployed_sources = _deployed_skill_sources()
    seen_ids = set()
    for scan_root in roots:
        for root, dirs, files in os.walk(scan_root):
            dirs[:] = [d for d in dirs if d not in
                       (".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build",
                        ".worktrees", ".playwright-mcp", ".pytest_cache", ".catpaw",
                        "tests", "test", "legion-dist",
                        # 测试数据/夹具目录:里面的 SKILL.md 是桩(bad/foo/bar/nope)不是可用工具
                        # (2026-08-28功能性验证实测:ongrid testdata/skill_registry 混进6个假skill)
                        "testdata", "test_data", "fixtures", "__fixtures__")]
            rel_low = os.path.relpath(root, scan_root).lower()
            if ("/tests/" in rel_low or "/solutions/" in rel_low or "/testdata/" in rel_low
                    or "/fixtures/" in rel_low or "legion-dist" in rel_low
                    or rel_low.startswith("release/legion-dist")):
                continue
            if "evolution/skills" in rel_low or "evolution\\skills" in rel_low:
                continue
            if "SKILL.md" in files:
                p = os.path.join(root, "SKILL.md")
                physical_path = os.path.realpath(p)
                if os.path.commonpath((physical_path, os.path.realpath(scan_root))) != os.path.realpath(scan_root):
                    continue
                fm = _parse_frontmatter(p)
                name = fm.get("name") or os.path.basename(root)
                desc = fm.get("short_description_zh") or fm.get("description") or fm.get("short_description") or ""
                # 桩/模板/占位过滤(功能性验证实测虚报):明显的测试名或模板占位不是可用工具。
                # 判据保守:只拦确定是桩的(桩名单 + 正文极短),不误伤正常短 skill。
                if str(name).strip().strip("\"'").lower() in (
                        "bad", "good", "foo", "bar", "nope", "test", "example",
                        "skill", "template-skill", "template", "placeholder"):
                    try:
                        _body_len = len(open(p, encoding="utf-8", errors="ignore").read())
                    except Exception:
                        _body_len = 0
                    if _body_len < 400:   # 桩名 + 内容极短 = 确认是桩
                        continue
                # Explicit deployment provenance comes from the same catalog,
                # not the release directory name; preserve its physical path.
                declared = next((key for source, key in deployed_sources.items()
                                 if Path(physical_path).is_relative_to(source)), None)
                if declared:
                    src, path_str = declared, physical_path
                elif scan_root == _LEGION_REPOS:
                    parts = os.path.relpath(p, _LEGION_REPOS).split(os.sep)
                    src = f"装备库/{parts[0]}" if parts else "装备库"
                    path_str = p  # 绝对路径(不在_REPO下)
                elif os.path.commonpath((os.path.realpath(p), os.path.realpath(_REPO))) != os.path.realpath(_REPO):
                    parts = os.path.relpath(p, scan_root).split(os.sep)
                    src = f"retained/{parts[0]}"
                    path_str = os.path.abspath(p)
                else:
                    src = _harness_of(p)
                    path_str = os.path.relpath(p, _REPO)
                iid = f"skill:{src}:{name}"
                if iid in seen_ids:
                    continue
                seen_ids.add(iid)
                items.append({
                    "id": iid,
                    "name": name, "kind": "skill", "source_harness": src,
                    "category": "skill", "capability_tags": _tag_capabilities(name, desc, root),
                    "path": path_str, "description": desc[:280],
                    "shared": True, "origin": None,
                })
    return items


# ── B. 扫 harness 代码工具(多媒体重点:hermes/OpenMontage/dsh-vision) ──
# 只挑对军团有直接价值的目录,不把上万个 .py 全铺(那是噪声)
_CODE_TOOL_DIRS = [
    ("hermes-agent/tools", "hermes"),
    ("ecosystem/OpenMontage/tools/video", "OpenMontage"),
    ("ecosystem/dsh-vision-toolkit/vendor/agent-vision-toolkit/skills/vision-tools/scripts", "dsh-vision"),
    ("codex/tools", "codex"),
    ("deepseek-harness/packages/core/tools", "deepseek"),
    # ecosystem 真能力工具(武器库更新 2026-08-27·只指精确 tools 目录·不递归扫巨仓)
    ("ecosystem/cua/libs/python/agent/cua_agent/tools", "cua(电脑操作)"),
    ("ecosystem/OpenMontage/tools/audio", "OpenMontage-audio"),
    ("ecosystem/OpenMontage/tools/image", "OpenMontage-image"),
]


def _scan_code_tools() -> list:
    items = []
    for reldir, src in _CODE_TOOL_DIRS:
        d = os.path.join(_HB, reldir)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".py") or fn.startswith("_") or fn.startswith("test"):
                continue
            stem = fn[:-3]
            p = os.path.join(d, fn)
            # 取模块 docstring 首行当描述(不 import,纯正则,安全)
            desc = ""
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    txt = f.read(1500)
                dm = re.search(r'"""(.*?)(?:\n|""")', txt, flags=re.S)
                if dm:
                    desc = dm.group(1).strip().splitlines()[0][:200] if dm.group(1).strip() else ""
            except Exception:
                pass
            items.append({
                "id": f"tool:{src}:{stem}",
                "name": stem, "kind": "code_tool", "source_harness": src,
                "category": "code_tool", "capability_tags": _tag_capabilities(stem, desc, reldir),
                "path": os.path.relpath(p, _REPO), "description": desc,
                "shared": True, "origin": None,
            })
    return items


def _scan_ecosystem_manifest() -> list:
    """读取 ecosystem 自述能力；只收项目明确声明的入口，不猜源码里的任意函数。"""
    inventory_path = os.path.join(_HB, "ecosystem", "ECOSYSTEM_INVENTORY.json")
    try:
        inventory = json.load(open(inventory_path, encoding="utf-8"))
    except Exception:
        return []
    items = []
    for project, meta in inventory.items():
        if project.startswith("_") or not isinstance(meta, dict):
            continue
        entrypoints = list(meta.get("entrypoints") or [])
        for capability in meta.get("capabilities") or []:
            name = str(capability).strip()
            if not name:
                continue
            items.append({
                "id": f"ecosystem_capability:{project}:{name.lower().replace(' ', '_')}",
                "name": name,
                "kind": "ecosystem_capability",
                "source_harness": f"ecosystem/{project}",
                "category": "ecosystem_capability",
                "capability_tags": _tag_capabilities(name, meta.get("note", ""), project),
                "path": f"harness_baselines/ecosystem/{project}",
                "description": (meta.get("note") or
                                f"{project} 官方源码声明的能力；入口: {', '.join(entrypoints) or 'README'}")[:280],
                "shared": True,
                "origin": {"who": "ECOSYSTEM_INVENTORY.json"},
                "runnable": False,
                "entrypoints": entrypoints,
            })
    return items


# ── B3. auto 本体的注册工具(军团最强的搜索/深研/抓取工具·用户批评"auto里都有你没说") ──
#   auto 的 @register_tool 装饰器注册了 20+ 个真能力工具(run_parallel_search/
#   run_deep_research_swarm/fetch_url_smart/query_structured_db/query_web_archive...)。
#   之前工具箱漏扫了它们。正则解析(不 import·避开 auto 的 Python3.12 启动自检)。
_AUTO_TOOL_FILES = [
    ("qa_system/agent/qa_tool_impls.py", "auto·核心工具"),
    ("qa_system/agent/qa_reasoning_tools.py", "auto·推理工具"),
    ("qa_system/agent/capability_stack.py", "auto·能力栈"),
]

# auto 主体在 qa_auto_agent.py 的工厂函数(_make_*_tools)里用 `def name(args) -> str:`
# + docstring 定义了 60+ 个工具(工具发现安装闭环 search_github/install_github_repo/
# 六件套、子agent spawn_subagent/multi_expert_parallel、桌面自动化 desktop_*、无代码建站
# nocode_*、计划 write_plan/goal_*…)。这些是 auto 最强的能力,但之前军团工具箱只扫
# @register_tool 正则,把它们全漏了 → 军团其他家和用户根本发现不了。用户明确要求:
# 把 auto 全部工具都注册到让大家发现。这里补扫工厂函数 def 的工具(docstring 当描述)。
_AUTO_FACTORY_FILE = "qa_system/agent/qa_auto_agent.py"
# 只收真正对外的 agent 工具(工厂函数内、4空格缩进的 def、返回 str/dict/list)。
# 排除私有下划线开头(正则 [a-z] 开头已排)。第一遍抓 def 头,第二遍在函数体前若干行内
# 找 docstring(很多工具 docstring 不紧跟 def,前面有 t0=time.time()/_check_budget 等胶水行)。
_AUTO_FACTORY_TOOL_RE = re.compile(
    # 参数可跨多行(spawn_specialist/batch_map 等签名换行);非贪婪到首个 `) -> ... :`
    r'^    def ([a-z][a-z0-9_]*)\((.*?)\)\s*->\s*(?:str|dict|list|Any)?\s*:\s*\n',
    flags=re.M | re.S,
)
# 在 def 之后的函数体内找 docstring(限定前 ~1200 字符,避免跨到下一个函数)
_DOCSTR_RE = re.compile(r'"""(.*?)"""', flags=re.S)
# 工厂函数 def 的工具里,有些是内部胶水(不该当能力暴露)。白名单外的按 docstring 收,
# 但排掉这些纯内部名(它们不是给别家/用户用的能力)。
_AUTO_FACTORY_EXCLUDE = {
    "goal_tick", "goal_status", "goal_list",  # 内部计划状态查询(non-capability)
}

def _normalize_auto_caps(name: str, caps: list) -> list:
    """修正 auto 工具的能力标签(工具类型元数据·非猜用户意图)。
    军团 dogfood 揪出:nocode_create 挂了 product 泛标签,凡沾"我们的X/品牌/市场"的 query
    (SEO/竞品/舆情)首位全被它截胡。nocode 类本质是【建站/建应用】,不是"产品运营"能力,
    去掉 product,只留 web_app,避免它蹭 product hint 霸榜领域调研需求。"""
    caps = list(caps or [])
    if name.startswith("nocode_") or name in ("produce_nocode_tool",):
        caps = [c for c in caps if c != "product"] or ["web_app"]
    return caps


# auto【内部编排/流程工具】:是 auto 自己解题的脚手架(triage/finalize/自省/计划/契约校验/
# 图像前处理等),不是用户直接需求的"能力"。它们仍注册进工具箱(auto 会调·大家可见),
# 但在【用户检索】里必须降权——否则挂了泛标签(search/writing/vision/product)会霸占前排,
# 截胡领域工具(军团 dogfood 实测:写客服话术→image_preprocess、SEO/竞品/舆情→nocode_create、
# 翻译→finalize_answer 全霸首位)。这里打 internal_flow 标记,scorer 据此减分。
# 判据:这个工具用户会直接说"我要用它"吗?会=真能力(transcribe/search_github/desktop/ocr/
# sympy…不进这里);不会、是 auto 内部流程步骤=internal_flow。
_AUTO_INTERNAL_FLOW = {
    "judge_scope_and_triage", "judge_evidence_stance", "finalize_answer",
    "build_answer_contract", "check_answer_contract", "run_self_check", "review_intent",
    "assess_info_sufficiency", "check_tool_cost_first", "check_model_quota", "network_status",
    "generate_action_cone", "generate_clarify_questions", "cross_verify_now", "deduce",
    "fold_context", "solve_strongest", "search_until_satisfied", "predict_then_search",
    "goal_create", "goal_update", "insert_plan_step", "write_plan", "reflect_now",
    "self_diagnose", "self_calibrate", "recall_experience", "remember_lesson",
    "check_subagent", "image_preprocess", "extract_cards", "reader_check_and_fix",
    "list_written_tools", "search_written_tools", "run_written_tool", "evolution_search",
    "evolution_execute", "video_modal_exhaustion", "fetch_outlinks",
}


def _scan_auto_registered_tools() -> list:
    items, seen = [], set()
    for rel, src in _AUTO_TOOL_FILES:
        p = os.path.join(_SEED, rel)
        if not os.path.isfile(p):
            continue
        # path 存相对 repo 根(seed-users-system/…)·不是相对 _SEED 的 rel——否则从仓库根
        # 定位不到文件(2026-08-28全量验证实测:99个auto_tool因path缺seed前缀被判path失败)。
        rel_repo = os.path.relpath(p, _REPO)
        try:
            txt = open(p, "r", encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        # 匹配 @register_tool( ... ) 块,抓 name= / description= / category=
        for m in re.finditer(r"register_tool\(\s*(.*?)\)\s*\n\s*def\b", txt, flags=re.S):
            block = m.group(1)
            nm = re.search(r"name\s*=\s*[\"']([^\"']+)[\"']", block)
            if not nm:
                continue
            name = nm.group(1)
            if name in seen:
                continue
            seen.add(name)
            dm = re.search(r"description\s*=\s*\(?\s*[\"'](.+?)[\"']\s*\)?\s*,\s*\n", block, flags=re.S)
            desc = re.sub(r"[\"'\s]+", " ", dm.group(1)).strip() if dm else ""
            cm = re.search(r"category\s*=\s*[\"']([^\"']+)[\"']", block)
            cat = cm.group(1) if cm else "auto"
            # ★capability_stack工厂工具(OCR/PDF/图像/sympy/wayback等)走capstack_bridge无状态桥
            # 直调→标runnable(用户2026-08-30:全部工厂工具映射给军团直接复用·绕开auto pipeline)。
            try:
                import capstack_bridge as _cb
                _cs_runnable = _cb.available() and name in set(_cb.list_bridged_tools())
            except Exception:
                _cs_runnable = False
            items.append({
                "id": f"auto_tool:{name}", "name": name, "kind": "code_tool",
                "source_harness": src, "category": cat,
                "capability_tags": _normalize_auto_caps(name, _tag_capabilities(name, desc, cat)),
                "path": rel_repo, "description": desc[:280], "shared": True, "origin": None,
                "runnable": _cs_runnable, "auto_native": not _cs_runnable,
                # 内部编排工具(image_preprocess 等来自 register_tool 分支)也打标降权
                "internal_flow": name in _AUTO_INTERNAL_FLOW,
            })
    # ── auto 主体工厂函数 def 的工具(用户要求:全部注册让大家发现) ──
    fp = os.path.join(_SEED, _AUTO_FACTORY_FILE)
    if os.path.isfile(fp):
        try:
            ftxt = open(fp, "r", encoding="utf-8", errors="ignore").read()
        except Exception:
            ftxt = ""
        for m in _AUTO_FACTORY_TOOL_RE.finditer(ftxt):
            name = m.group(1)
            if name in seen or name in _AUTO_FACTORY_EXCLUDE:
                continue
            seen.add(name)
            # 在函数体前 ~1200 字符内找 docstring(不紧跟 def 也能抓到);找不到用名字兜底
            body = ftxt[m.end():m.end() + 1200]
            # 只在遇到下一个同级 def 之前找(防跨函数误取)
            nxt = re.search(r"\n    def [a-z]", body)
            scope = body[:nxt.start()] if nxt else body
            dm = _DOCSTR_RE.search(scope)
            if dm:
                raw = dm.group(1).strip()
                desc = re.sub(r"\s+", " ", raw)[:280]
            else:
                desc = f"auto 内置工具 {name}(详见源码)"
            # ★auto拆解(用户2026-08-30):无状态工具(媒体/搜索/抓取)已在 legion_tool.run_tool
            # 映射到 media_tools+_run_* 真实现·标 runnable=True 让大家发现即可直接跑;
            # session依赖类(spawn_subagent/desktop等)仍 auto_native(派auto家)。
            # runnable白名单:①抓取/github类走legion_tool无状态入口 ②capability_stack工厂工具
            # (OCR/PDF/图像/sympy/wayback等)走capstack_bridge无状态桥直调(用户2026-08-30:
            # 全部工厂工具映射给军团直接复用·绕开auto完整pipeline·核心无状态·stub session承接)。
            _AUTO_RUNNABLE = {"search_github", "inspect_github_repo", "install_github_repo",
                              "fetch_url_smart", "fetch_outlinks"}
            try:
                import capstack_bridge as _cb
                if _cb.available():
                    _AUTO_RUNNABLE = _AUTO_RUNNABLE | set(_cb.list_bridged_tools())
            except Exception:
                pass
            _is_runnable = name in _AUTO_RUNNABLE
            items.append({
                "id": f"auto_tool:{name}", "name": name, "kind": "code_tool",
                "source_harness": "auto·主体能力", "category": "auto",
                "capability_tags": _normalize_auto_caps(name, _tag_capabilities(name, desc, "auto agent tool")),
                "path": os.path.relpath(fp, _REPO), "description": desc,
                "shared": True, "origin": None,
                # 无状态工具已接真实现→runnable;其余仍派auto家(auto运行时装配)或读源码照做。
                "runnable": _is_runnable,
                "auto_native": not _is_runnable,
                # 内部编排/流程工具:仍注册可见,但用户检索里降权(scorer 据此减分),
                # 别让 triage/finalize/image_preprocess 挂泛标签霸占领域需求前排。
                "internal_flow": name in _AUTO_INTERNAL_FLOW,
            })
    return items


def _scan_vision_tools() -> list:
    """dsh-vision-toolkit 脚本工具(源码在本仓库·PIL纯本地→无状态直调·阶段2)。
    走 vision_toolkit_bridge·offline=True 的标 runnable(军团 run vision_tool:<name> 直调不派家);
    offline=False(需云VISION配置)诚实标 auto_native·让军团知道要先配再用(不假装可用)。"""
    items = []
    try:
        import vision_toolkit_bridge as _vt
        if not _vt.available():
            return items
        for meta in _vt.tool_meta():
            name = meta["tool"]
            offline = bool(meta.get("offline"))
            items.append({
                "id": f"vision_tool:{name}", "name": name, "kind": "code_tool",
                "source_harness": "dsh-vision(ecosystem)", "category": "vision",
                "capability_tags": _tag_capabilities(name, meta["desc"], "vision image tool"),
                "path": "harness_baselines/ecosystem/dsh-vision-toolkit",
                "description": meta["desc"] + ("" if offline else "(需云VISION/glance配置)"),
                "shared": True, "origin": {"who": "dsh-vision-toolkit"},
                "runnable": offline,  # 纯本地PIL→可直调;需云配置的不谎报runnable
                "auto_native": not offline,
                "internal_flow": False,
            })
    except Exception:
        pass
    return items


# ── B2. 军团已经接通、可真执行的媒体工具(不是只读源码清单) ──
def _scan_opencli() -> list:
    """OpenCLI 接入军团工具箱(把100+网站变成CLI·复用登录态·JSON输出喂harness)。"""
    try:
        import opencli_bridge
        if not opencli_bridge.available():
            return [{
                "id": "runnable:opencli", "name": "opencli", "kind": "runnable_tool",
                "source_harness": "OpenCLI(网页→CLI)", "category": "web_tool",
                "capability_tags": ["browse", "search"],
                "path": "release/legion-service/opencli_bridge.py",
                "description": "把任意网站变成CLI(100+站·复用登录态·JSON输出)。未安装:npm i -g @jackwener/opencli",
                "shared": True, "origin": {"who": "OpenCLI社区"}, "runnable": True,
                "installed": False, "params": ["args"],
            }]
        return [{
            "id": "runnable:opencli", "name": "opencli", "kind": "runnable_tool",
            "source_harness": "OpenCLI(网页→CLI)", "category": "web_tool",
            "capability_tags": ["browse", "search"],
            "path": "release/legion-service/opencli_bridge.py",
            "description": "把任意网站变成CLI(100+站·复用浏览器登录态·结构化JSON输出喂harness)",
            "shared": True, "origin": {"who": "OpenCLI社区"}, "runnable": True,
            "installed": True, "params": ["args"],
        }]
    except Exception:
        return []


def _shared_model_tools() -> list:
    """冷态、旧缓存和完整扫描共用随包声明，不能把目录当实时可用性。"""
    import media_model_cli
    return [{
        **row, "id": f"runnable:{row['id']}", "name": row["id"], "kind": "runnable_tool",
        "source_harness": "legion-service(共享模型入口)", "category": "model",
        "path": "release/legion-service/media_model_cli.py",
        "shared": True, "runnable": True, "installed": True,
        "health": "unknown", "live_verified": False,
    } for row in media_model_cli.model_tool_specs()]


def _scan_shared_web_tools() -> list:
    """全军团共享的搜索/抓取入口；后端选择留给 SearchFabric 和各 harness。"""
    return [
        *_shared_model_tools(),
        {
            "id": "runnable:capability_catalog", "name": "capability_catalog", "kind": "runnable_tool",
            "source_harness": "legion-service(能力发现目录)", "category": "tool_discovery",
            "capability_tags": ["tool_discovery", "search", "code", "mcp"],
            "path": "release/legion-service/capability_catalog.py",
            "description": "默认列出搜索源、SkillFinder、GitHub MCP、内部工具、本地/远程代码搜索和MCP入口",
            "shared": True, "origin": {"who": "军团统一能力目录"},
            "runnable": True, "installed": True, "params": [],
        },
        {
            "id": "runnable:web_search", "name": "web_search", "kind": "runnable_tool",
            "source_harness": "legion-service(共享搜索总线)", "category": "web_search",
            "capability_tags": ["search", "research", "web"],
            "path": "release/legion-service/legion_tool.py",
            "description": "搜索当前互联网信息，返回来源URL、标题、摘要和实际后端；可指定Google或多源搜索",
            "shared": True, "origin": {"who": "SearchFabric + harness web providers"},
            "runnable": True, "installed": True,
            "params": ["query", "queries", "limit", "mode", "source"],
        },
        {
            "id": "runnable:research_capabilities", "name": "research_capabilities", "kind": "runnable_tool",
            "source_harness": "legion-service(研究能力层)", "category": "research",
            "capability_tags": ["research", "search", "web", "data", "pdf_doc", "vision"],
            "path": "release/legion-service/research_pipeline.py",
            "description": "列出搜索、抓取、结构化抽取、证据压缩、实体关系图、文档和多媒体处理的可组合阶段",
            "shared": True, "origin": {"who": "Codex + DeepSeek Harness 架构"},
            "runnable": True, "installed": True, "params": [],
        },
        *[{
            "id": f"runnable:research_{stage}", "name": f"research_{stage}", "kind": "runnable_tool",
            "source_harness": "legion-service(研究能力层)", "category": "research",
            "capability_tags": ["research", *( ["search", "web"] if stage in ("search", "fetch") else []),
                                *( ["data"] if stage in ("extract", "compress", "graph", "document") else []),
                                *( ["vision", "video_edit", "audio"] if stage == "media" else [])],
            "path": "release/legion-service/research_pipeline.py",
            "description": desc, "shared": True, "origin": {"who": "军团统一研究能力"},
            "runnable": True, "installed": True, "params": params,
        } for stage, desc, params in (
            ("search", "带来源的多查询搜索；模型根据结果自行决定下一跳", ["query", "queries", "limit", "mode", "source", "workdir"]),
            ("fetch", "抓取网页正文并持久化原文与来源", ["url", "timeout_s", "workdir"]),
            ("extract", "把网页或文档抽成字段、段落、链接和表格", ["text", "path", "fields", "workdir"]),
            ("compress", "完整原文落盘，给上下文保留有界首尾预览和取回路径", ["text", "path", "max_chars", "workdir"]),
            ("graph", "抽取带来源和置信度的实体关系图，并查询多跳路径", ["text", "path", "source", "start_entity", "max_hops", "workdir"]),
            ("document", "把研究内容整理成 Markdown、HTML 或 JSON 真文件", ["title", "sections", "text", "format", "workdir"]),
            ("media", "下载媒体并执行 OCR/视觉理解、视频抽帧、抽音频或 PDF 解析", ["operation", "url", "path", "question", "at_s", "timeout_s", "workdir"]),
        )],
        {
            "id": "runnable:skill_finder", "name": "SkillFinder", "kind": "runnable_tool",
            "source_harness": "军团 Skill 工厂", "category": "tool_discovery",
            "capability_tags": ["tool_discovery", "search", "install"],
            "path": "release/legion-service/skill_factory.py",
            "description": "从 Meyo、skills.sh、Skillfish 多源搜索并审计、安装、同步共享 Skill",
            "shared": True, "origin": {"who": "deep-skill-finder + skills.sh + skillfish"},
            "runnable": True, "installed": True, "params": ["action", "query", "source", "name", "uid", "confirm"],
        },
        {
            "id": "runnable:skill_factory_list", "name": "skill_factory_list", "kind": "runnable_tool",
            "source_harness": "军团 Skill 工厂", "category": "tool_discovery",
            "capability_tags": ["tool_discovery", "skills"], "path": "release/legion-service/skill_factory.py",
            "description": "列出当前用户已激活、所有军团成员可即时发现的共享 Skill",
            "shared": True, "runnable": True, "installed": True, "params": ["uid"],
        },
        {
            "id": "runnable:search_sources", "name": "search_sources", "kind": "runnable_tool",
            "source_harness": "legion-service(搜索源目录)", "category": "web_search",
            "capability_tags": ["search", "research", "web"],
            "path": "release/legion-service/search_sources.py",
            "description": "列出全部搜索体系、引擎及 YouTube/X/Reddit/搜狗微信等专线的当前健康状态",
            "shared": True, "origin": {"who": "SearchFabric + SOURCE_CATALOG"},
            "runnable": True, "installed": True, "params": [],
        },
        {
            "id": "runnable:search_source_probe", "name": "search_source_probe", "kind": "runnable_tool",
            "source_harness": "legion-service(搜索源探针)", "category": "web_search",
            "capability_tags": ["search", "research", "web"],
            "path": "release/legion-service/search_sources.py",
            "description": "实时探测指定平台；专线不可用时通过统一聚合入口做站点限定搜索",
            "shared": True, "origin": {"who": "SearchFabric + SOURCE_CATALOG"},
            "runnable": True, "installed": True,
            "params": ["source", "query", "limit", "timeout_s"],
        },
        {
            "id": "runnable:openmontage_demos", "name": "openmontage_demos", "kind": "runnable_tool",
            "source_harness": "OpenMontage", "category": "media",
            "capability_tags": ["video_edit", "media", "code"],
            "path": "harness_baselines/ecosystem/openmontage/render_demo.py",
            "description": "列出或渲染 OpenMontage 零密钥 Remotion 演示视频；产出真实 MP4",
            "shared": True, "origin": {"who": "OpenMontage"},
            "runnable": True, "installed": True, "params": ["demo", "list"],
        },
        {
            "id": "runnable:web_fetch", "name": "web_fetch", "kind": "runnable_tool",
            "source_harness": "legion-service(共享抓取总线)", "category": "web_fetch",
            "capability_tags": ["browse", "research", "web"],
            "path": "release/legion-service/media_tools.py",
            "description": "抓取指定网页正文并返回标题、文本、状态码和实际后端",
            "shared": True, "origin": {"who": "legion fetch_url + Fetch MCP"},
            "runnable": True, "installed": True, "params": ["url", "timeout_s"],
        },
        {
            "id": "runnable:mcp_list", "name": "mcp_list", "kind": "runnable_tool",
            "source_harness": "meyo-orchestrator(MCP桥)", "category": "mcp",
            "capability_tags": ["mcp", "tools"],
            "path": "harness_baselines/meyo-orchestrator/meyo_harnesses/mcp_bridge.py",
            "description": "按需列出军团共享MCP服务器及其真实工具schema",
            "shared": True, "origin": {"who": "军团MCP桥"},
            "runnable": True, "installed": True, "params": [],
        },
        {
            "id": "runnable:mcp_call", "name": "mcp_call", "kind": "runnable_tool",
            "source_harness": "meyo-orchestrator(MCP桥)", "category": "mcp",
            "capability_tags": ["mcp", "tools"],
            "path": "harness_baselines/meyo-orchestrator/meyo_harnesses/mcp_bridge.py",
            "description": "调用已注册MCP的具体工具；先用mcp_list读取真实schema",
            "shared": True, "origin": {"who": "军团MCP桥"},
            "runnable": True, "installed": True, "params": ["server", "name", "arguments"],
        },
    ]


def _scan_agent_tool_providers() -> list:
    try:
        import agent_tool_provider
        rows = []
        for provider in agent_tool_provider.inventory().get("declarations", []):
            ready = provider.get("execution_ready")
            for tool in provider.get("tools", []):
                rows.append({"id": tool["id"], "name": f"{provider['provider']}.{tool['name']}",
                             "kind": "agent_tool", "source_harness": provider["provider"],
                             "category": "agent_tool", "capability_tags": [tool["capability"], "tools"],
                             "path": "release/legion-service/agent_tool_provider.py",
                             "description": f"通过 {provider['provider']} 的原生 {tool['name']} 工具执行",
                             "shared": True, "runnable": ready, "installed": provider.get("installed"),
                             "execution_ready": ready, "execution_role": provider.get("execution_role"),
                             "health": "ready" if ready is True else "unavailable" if ready is False else "unknown",
                             "reason": provider.get("reason", ""),
                             "params": ["arguments", "task", "uid", "session_id"],
                             "permissions": tool.get("permissions") or []})
        return rows
    except Exception:
        return []


def _scan_external_capabilities(*, catalog=None) -> list:
    """Expose declarative external capability actions as shared runnable tools."""
    try:
        import external_agent_registry as registry
        rows = []
        declaration = (registry.capability_actions() if catalog is None
                       else registry.capability_actions(catalog=catalog))
        for action in declaration.get("actions", []):
            rows.append({
                "id": action["id"], "name": f"{action['key']}.{action['action']}",
                "kind": "runnable_tool", "source_harness": action["key"],
                "category": "external_capability",
                "capability_tags": action.get("capabilities") or ["general"],
                "path": "release/legion-service/external_agents.json",
                "description": action.get("description") or action["action"],
                "shared": True, "origin": {"who": action["key"]},
                "runnable": True, "installed": None, "health": "unknown",
                "params": action.get("params") or [],
            })
        return rows
    except Exception:
        if catalog is not None:
            raise
        return []


def _current_capability_view(snapshot: dict, fallback: dict | None = None) -> dict:
    """Overlay current local declarations, not provider probes or a new full scan."""
    def external(item):
        return str(item.get("id", "")).startswith("external_capability:")

    error = None
    source_ref = None
    try:
        import external_agent_registry as registry
        source_ref = {"path": str(registry.CATALOG), "field": "agents[].capability_actions"}
        document = json.loads(registry.CATALOG.read_text(encoding="utf-8"))
        catalog = document.get("agents") if isinstance(document, dict) else None
        if not isinstance(catalog, list) or any(
                not isinstance(item, dict) or not isinstance(item.get("key"), str) or not item["key"]
                for item in catalog):
            raise ValueError("invalid local capability declaration")
        current = _scan_external_capabilities(catalog=catalog)
    except Exception as exc:
        error = type(exc).__name__
        # A failed read is not an authoritative empty catalog. Keep the last
        # snapshot's declaration facts, while never upgrading their readiness.
        current = [{**item, "installed": None, "health": "unknown", "health_checked_at": None}
                   for item in (fallback or snapshot).get("items", []) if external(item)]
    old = snapshot.get("items") or []
    builtins = _shared_model_tools()
    builtin_ids = {item["id"] for item in builtins}
    previous = [item for item in old if external(item) or item.get("id") in builtin_ids]
    current += builtins
    items = [item for item in old if not external(item) and item.get("id") not in builtin_ids] + current
    summary = dict(snapshot.get("summary") or {})
    old_runnable = summary.get("runnable_tools", sum(bool(item.get("runnable")) for item in old))
    if type(old_runnable) is not int:
        old_runnable = sum(bool(item.get("runnable")) for item in old)
    by_capability = {}
    for item in items:
        for tag in item.get("capability_tags") or ["general"]:
            if not isinstance(tag, str):
                continue
            by_capability[tag] = by_capability.get(tag, 0) + 1
    summary.update(total=len(items), shared=sum(bool(item.get("shared")) for item in items),
                   runnable_tools=max(0, old_runnable - len(previous) + len(current)),
                   by_capability=dict(sorted(by_capability.items(), key=lambda pair: -pair[1])))
    return {**snapshot, "items": items, "summary": summary,
            "capability_declarations_current": error is None, "capability_declaration_error": error,
            "capability_declarations_ref": source_ref}


def _scan_runnable_media_tools() -> list:
    try:
        import media_tools
        return [{
            "id": f"runnable:{t['id']}", "name": t["id"], "kind": "runnable_tool",
            "source_harness": "legion-service(共享工具)", "category": "media_tool",
            "capability_tags": [t["cap"]], "path": "release/legion-service/media_tools.py",
            "description": t["desc"], "shared": True, "origin": {"who": "军团公共工具箱"},
            "runnable": True, "params": t["params"],
        } for t in media_tools.list_media_tools()]
    except Exception:
        return []


# ── C. 军团自产工具(forge log + evolution/skills) ──
def _scan_forged() -> list:
    items = []
    # C1. tool_forge_loop 锻造出来的工具
    flog = os.path.join(_V11, ".tool_forge_log.jsonl")
    if os.path.isfile(flog):
        seen = set()
        try:
            for line in open(flog, "r", encoding="utf-8", errors="ignore"):
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                name = r.get("name")
                if not name or name in seen:
                    continue
                seen.add(name)
                items.append({
                    "id": f"forged:{name}",
                    "name": name, "kind": "forged", "source_harness": "auto(军团自产)",
                    "category": r.get("category") or "forged",
                    "capability_tags": _tag_capabilities(name, r.get("description", ""),
                                                          r.get("source_pattern", "")),
                    "path": os.path.relpath(flog, _REPO),
                    "description": (r.get("description") or "")[:280],
                    "shared": True,  # 军团自产默认共享,彼此可见可复用
                    "origin": {"who": "tool_forge_loop", "for_pattern": r.get("source_pattern"),
                               "when": r.get("ts")},
                })
        except Exception:
            pass
    # C2. 每题自演化 skill(evolution/skills/_user_*)
    esk = os.path.join(_V11, "evolution", "skills")
    if os.path.isdir(esk):
        for sub in sorted(os.listdir(esk)):
            sp = os.path.join(esk, sub, "SKILL.md")
            if not os.path.isfile(sp):
                continue
            fm = _parse_frontmatter(sp)
            name = fm.get("name") or sub
            desc = fm.get("description") or ""
            # 从目录名反推它是为哪道题产的
            for_what = sub.replace("_user_bc_pilot__", "").replace("-", " ")
            items.append({
                "id": f"forged_skill:{name}",
                "name": name, "kind": "forged", "source_harness": "auto(军团自产)",
                "category": "evolved_skill",
                "capability_tags": _tag_capabilities(name, desc, sub),
                "path": os.path.relpath(sp, _REPO), "description": desc[:280],
                "shared": True,
                "origin": {"who": "evolution_pilot", "for_task": for_what[:80]},
            })
    return items


def _read_cached_toolbox() -> dict | None:
    """内存优先读取快照；磁盘仅在首次访问或文件变化时解析一次。"""
    try:
        stamp = (os.path.abspath(_CACHE), os.path.getmtime(_CACHE), os.path.getsize(_CACHE))
    except OSError:
        return None
    with _CACHE_LOCK:
        if _CACHE_MEMORY["data"] is not None and _CACHE_MEMORY["stamp"] == stamp:
            cached = _CACHE_MEMORY["data"]
            return cached if _cache_source_valid(cached) else None
    try:
        with open(_CACHE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    with _CACHE_LOCK:
        _CACHE_MEMORY.update({"data": data, "stamp": stamp})
    return data if _cache_source_valid(data) else None


def cached_toolbox() -> dict | None:
    """已有完整快照的当前声明视图；不触发扫描、不造 partial bootstrap。"""
    cached = _read_cached_toolbox()
    return _current_capability_view(cached) if cached is not None else None


def skill_inventory() -> list:
    """显式返回完整 Skill 清单；优先复用全量快照，无快照时只扫 Skill 而非全工具箱。"""
    cached = _read_cached_toolbox()
    if cached is not None and not cached.get("partial"):
        return [item for item in cached.get("items", []) if item.get("kind") == "skill"]
    return _scan_skills()


def toolbox_page(limit: int = 50, offset: int = 0, capability: str = "") -> dict:
    """快速读取工具箱首批；无完整快照先给核心武器并后台补齐。"""
    cached = _read_cached_toolbox()
    from_snapshot = cached is not None
    if cached is None:
        cached = _bootstrap_toolbox()
        _schedule_refresh()
    cached = _current_capability_view(cached)
    items = cached.get("items") or []
    if capability:
        items = [item for item in items if capability in (item.get("capability_tags") or [])]
    total = len(items)
    page = items[offset:offset + limit]
    return {"items": page, "summary": cached.get("summary") or {},
            "scanned_at": cached.get("scanned_at", 0),
            "from_cache": from_snapshot,
            "warming": bool(cached.get("warming")), "partial": bool(cached.get("partial")),
            "capability_declarations_current": cached.get("capability_declarations_current"),
            "capability_declaration_error": cached.get("capability_declaration_error"),
            "stale": time.time() - cached.get("scanned_at", 0) >= _CACHE_TTL,
            "total": total, "total_filtered": total if capability else None,
            "filtered_by": capability or None,
            "offset": offset, "limit": limit, "has_more": offset + len(page) < total}


def toolbox_summary() -> dict:
    """只读快照里的 summary；无快照立即返回 warming，绝不让摘要请求触发全仓扫描。"""
    data = _read_cached_toolbox()
    if data is not None:
        data = _current_capability_view(data)
        out = {"summary": data.get("summary") or {}, "scanned_at": data.get("scanned_at", 0),
               "from_cache": True, "stale": time.time() - data.get("scanned_at", 0) >= _CACHE_TTL,
               "capability_declarations_current": data.get("capability_declarations_current"),
               "capability_declaration_error": data.get("capability_declaration_error")}
        if out["stale"]:
            _schedule_refresh()
        return out
    partial = _current_capability_view(_bootstrap_toolbox())
    _schedule_refresh()
    return {"summary": partial["summary"], "scanned_at": partial["scanned_at"],
            "from_cache": False, "warming": True, "partial": True,
            "capability_declarations_current": partial.get("capability_declarations_current"),
            "capability_declaration_error": partial.get("capability_declaration_error")}


def _schedule_refresh() -> None:
    global _REFRESHING
    with _CACHE_LOCK:
        if _REFRESHING:
            return
        _REFRESHING = True
    threading.Thread(target=_refresh_after_user_response, daemon=True,
                     name="toolbox-refresh").start()


def _refresh_after_user_response() -> None:
    # 把 CPU/磁盘重扫让到首个用户查询之后；预热路径直接 force=True，不经过这里。
    # 1 秒足够核心 BM25 返回，避免后台 45s 全扫与首请求争 GIL/磁盘导致 0.8s→5s+。
    time.sleep(1.0)
    _refresh_in_background()


def _refresh_in_background() -> None:
    global _REFRESHING
    try:
        scan_toolbox(force=True)
    finally:
        with _CACHE_LOCK:
            _REFRESHING = False


def _assemble_toolbox(skills: list, code_tools: list, runnable_tools: list,
                      forged: list, *, warming: bool = False) -> dict:
    """统一去重和汇总；完整扫描与冷启动核心目录共享同一事实口径。"""
    items = skills + code_tools + runnable_tools + forged
    # 跨源按 name 去重且 runnable 优先，避免 reference 覆盖真可执行工具。
    by_name = {}
    for item in items:
        name = item.get("name")
        if not name:
            continue
        previous = by_name.get(name)
        if previous is None or (item.get("runnable") and not previous.get("runnable")):
            by_name[name] = item
    items = list(by_name.values())
    by_capability = {}
    for item in items:
        for tag in item.get("capability_tags") or ["general"]:
            by_capability[tag] = by_capability.get(tag, 0) + 1
    full_repo = os.path.isdir(_HB) and os.path.isdir(_SEED)
    result = {
        "items": items,
        "skill_sources": skill_source_identity(),
        "summary": {
            "total": len(items), "skills": len(skills), "code_tools": len(code_tools),
            "runnable_tools": len(runnable_tools), "forged": len(forged),
            "shared": sum(1 for item in items if item.get("shared")),
            "by_capability": dict(sorted(by_capability.items(), key=lambda pair: -pair[1])),
            "environment": "full_repo" if full_repo else "slim_deploy",
            "note": None if full_repo else
                    "精简部署:仅扫到本地可见工具;完整工具池(数百件harness skill/代码工具)需在完整 mars-data 仓库或设 MEYO_ORCH_DIR",
        },
        "scanned_at": time.time(), "from_cache": False,
    }
    if warming:
        result.update({"warming": True, "partial": True,
                       "note": "核心可执行工具已可用；完整 Skill/Agent 工具目录正在后台补齐"})
    return result


def _bootstrap_toolbox() -> dict:
    """常数时间核心目录：冷态不碰全仓/provider，完整事实由后台扫描补齐。"""
    with _CACHE_LOCK:
        if _BOOTSTRAP_MEMORY["data"] is not None:
            return _BOOTSTRAP_MEMORY["data"]
    # 只列运行时内置且 run_tool 能立即执行的核心武器；纯静态元数据保持冷态常数时间。
    # 完整 Skill/源码/外部 provider 目录仍由后台扫描补齐，首个用户无需为全仓扫描等待。
    specs = [
        ("web_fetch", "读取公开网页并提取正文", "web", ["web_fetch", "research", "web"]),
        ("web_search", "搜索互联网并返回来源、标题和摘要", "web", ["search", "research", "web"]),
        ("repo_search", "搜索当前代码仓库里的函数、类和工具", "code", ["code", "research"]),
        ("file_read", "读取文本、代码、表格预览；其他类型仅返回文件事实，不发布文件或验证URL", "file", ["file", "document"]),
        ("spreadsheet_inspect", "读取 Excel/CSV 表格并做数据分析统计", "data", ["data", "spreadsheet", "excel"]),
        ("media_inspect", "按扩展名和大小描述媒体文件；不解码或听看，不发布文件或验证URL", "media", ["image", "video", "audio", "pdf_doc"]),
        ("read_pdf", "解析 PDF 文档内容并可分块摘要", "media", ["pdf_doc", "document"]),
        ("gen_image", "根据文字生成配图或海报图片", "media", ["image_gen"]),
        ("see_image", "识别图片内容、OCR 文字和视觉理解", "media", ["vision", "ocr"]),
        ("gen_video", "生成视频或基于图片制作视频", "media", ["video_gen"]),
        ("trim_media", "剪辑视频和音频片段", "media", ["video_edit", "audio"]),
        ("extract_audio", "从视频抽取音轨", "media", ["audio", "video_edit"]),
        ("video_frame", "从视频抽取指定时刻画面", "media", ["vision", "video_edit"]),
        ("desktop_control", "操作桌面点击窗口自动化并控制云真机 GUI", "desktop", ["desktop", "cua", "gui"]),
        ("capability_catalog", "列出搜索、Skill、MCP 和源码函数等能力入口", "tool_discovery", ["tool_discovery", "search", "mcp"]),
    ]
    runnable_tools = [{
        "id": f"runnable:{name}", "name": name, "kind": "runnable_tool",
        "description": description, "category": category, "capability_tags": tags,
        "shared": True, "runnable": True, "installed": True,
        "source_harness": "legion-service(冷启动核心目录)",
    } for name, description, category, tags in specs]
    runnable_tools.extend(_shared_model_tools())
    # 本地 ASR 已由 capability_stack 的无状态桥执行；保留原始 auto_tool ID，避免伪造新入口。
    runnable_tools.append({
        "id": "auto_tool:transcribe_audio_local", "name": "transcribe_audio_local",
        "kind": "runnable_tool", "description": "本地 ASR，把语音或视频转成文字",
        "category": "audio", "capability_tags": ["audio", "speech-to-text", "transcription"],
        "shared": True, "runnable": True, "installed": True,
        "source_harness": "auto capability_stack",
    })
    result = _assemble_toolbox([], [], runnable_tools, [], warming=True)
    with _CACHE_LOCK:
        _BOOTSTRAP_MEMORY["data"] = result
    return result


def scan_toolbox(force: bool = False) -> dict:
    """扫全仓工具箱；冷启动先给核心目录，完整目录单飞后台刷新。"""
    global _REFRESHING
    cached = _read_cached_toolbox()
    if cached is not None and not force:
        fresh = time.time() - cached.get("scanned_at", 0) < _CACHE_TTL
        if not fresh:
            with _CACHE_LOCK:
                if not _REFRESHING:
                    _REFRESHING = True
                    threading.Thread(target=_refresh_in_background, daemon=True,
                                     name="toolbox-refresh").start()
        return _current_capability_view({**cached, "from_cache": True, "stale": not fresh})
    if not force:
        # 不同步等待全仓 Skill + 外部 provider 探活（实测约 45s）；先用约 0.5s 的核心武器，
        # 再后台补齐完整目录。这样首用既快，也不会把 warming 误报成“没有工具”。
        partial = _bootstrap_toolbox()
        _schedule_refresh()
        return _current_capability_view(partial)
    # 完整扫描 single-flight：后台预热与显式 force 同时到达时只扫一次；等待者复用刚落盘快照。
    waited = not _FULL_SCAN_LOCK.acquire(blocking=False)
    if waited:
        _FULL_SCAN_LOCK.acquire()
    try:
        if waited:
            refreshed = _read_cached_toolbox()
            if refreshed is not None and not refreshed.get("partial"):
                return _current_capability_view({**refreshed, "from_cache": True, "coalesced": True})
        source_before = skill_source_identity()
        skills = _scan_skills()
        code_tools = (_scan_code_tools() + _scan_ecosystem_manifest()
                      + _scan_auto_registered_tools() + _scan_vision_tools())
        runnable_tools = (_scan_runnable_media_tools() + _scan_shared_web_tools()
                          + _scan_external_capabilities() + _scan_agent_tool_providers() + _scan_opencli())
        forged = _scan_forged()
        result = _current_capability_view(
            _assemble_toolbox(skills, code_tools, runnable_tools, forged), fallback=cached)
        if result["skill_sources"] != source_before:
            # Never label an old scan with a source generation that changed
            # while it ran. The existing refresh owner can rebuild that input.
            result.update({"skill_sources": source_before,
                           "source_changed_during_scan": True, "stale": True})
        try:
            os.makedirs(_CACHE_DIR, exist_ok=True)
            tmp = _CACHE + f".tmp.{os.getpid()}.{threading.get_ident()}"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(result, handle, ensure_ascii=False)
            os.replace(tmp, _CACHE)
            stamp = (os.path.abspath(_CACHE), os.path.getmtime(_CACHE), os.path.getsize(_CACHE))
            with _CACHE_LOCK:
                _CACHE_MEMORY.update({"data": result, "stamp": stamp})
        except Exception:
            pass
        return result
    finally:
        _FULL_SCAN_LOCK.release()


def invalidate_cache() -> None:
    """删掉工具箱缓存,强制下次 scan_toolbox 重扫。

    军团 dogfood(deepseek 2026-08-29)揪出的真 bug:缓存只看 scanned_at 是否超1h、
    不看源变化,新安装/注册/锻造的工具1小时内对全军团隐形→成员误判"没现成工具"重复造轮子,
    击穿工具箱"彼此发现·避免重造"的设计初衷。修法(理念6想100步):所有会改变工具集的入口
    (install/register/forge/write_capability)完成后调这个,让新工具立即对全军团可见。
    """
    global _REFRESHING
    try:
        if os.path.isfile(_CACHE):
            os.remove(_CACHE)
    except Exception:
        pass
    with _CACHE_LOCK:
        _CACHE_MEMORY.update({"data": None, "stamp": None})
        _BOOTSTRAP_MEMORY["data"] = None
        _SUMMARY_MEMORY.update({"data": None, "stamp": None})
        _REFRESHING = False


def get_tool(tool_id: str) -> Optional[dict]:
    """按 id 精确取一条工具(供 run_tool / trace 引用)。"""
    tb = scan_toolbox()
    for it in tb["items"]:
        if it["id"] == tool_id:
            return it
    return None


if __name__ == "__main__":
    import sys
    tb = scan_toolbox(force="--force" in sys.argv)
    s = tb["summary"]
    print(f"军团工具箱:共 {s['total']} 件 "
          f"(skill {s['skills']} · 代码工具 {s['code_tools']} · 可执行 {s['runnable_tools']} "
          f"· 自产 {s['forged']} · 共享 {s['shared']})")
    print("按能力分布:")
    for cap, n in s["by_capability"].items():
        print(f"  {cap:12s} {n}")
