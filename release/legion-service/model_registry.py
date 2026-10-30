#!/usr/bin/env python3
"""
model_registry · 军团模型选择(用户要"agent自选/用户切换/固定默认/展示当前")

三层选择(给控制权·不加墙):
  1. 智能默认:agent 按任务方向自选可用模型(select_usable_model·已有)
  2. 每家偏好:不同 harness 默认走不同方向(编程家→programming·视觉→vision)
  3. 用户覆盖:用户为某家固定某模型 → 存 ~/.meyo/legion/model_prefs.json → dispatch 透传

模型候选默认来自 ~/.claude/skills/aigc-model-router/models.json(各方向已验证候选)。
操作者可用 MEYO_MODEL_CATALOG_PATH 指定只读文件或含 models.json 的目录；
显式引用不可读时不回落 HOME，也不补用另一个目录的授权矩阵。
展示:每家用了哪个模型在 dispatch 结果的 model_used 里(网关自愈已记录)。
"""
import hashlib
import json
import os
import threading
import time

import legion_paths as _paths
# model_prefs.json 是【共享单文件】·set_pref 是 load→改→save 无锁·并发写全丢(实测 8 并发只剩 0)。
# 加进程内锁保护偏好读改写。
_PREFS_LOCK = threading.RLock()

_MODELS_PATH = os.path.expanduser("~/.claude/skills/aigc-model-router/models.json")
_IMPORTED_MODELS_PATH = _MODELS_PATH
_PREFS_PATH = _paths.under("model_prefs.json")
_CATALOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verified_model_catalog.json")
_DISCOVERY_PATH = _paths.under("model_discovery_state.json")

# 每家默认走的方向(决定它的候选池)。内置/外部都从统一运行时 manifest 读取；
# loader 不可用时保留旧兜底，不能因画像文件损坏阻断交付。
def _load_manifest() -> dict:
    try:
        import runtime_manifest
        return runtime_manifest.load_definitions()
    except Exception:
        return {}

_MF = _load_manifest()

_HARNESS_DIRECTION = {k: m.get("direction") or "programming" for k, m in _MF.items()} or {
    "codex": "programming", "cc": "programming",
    "jcode": "programming", "deepseek": "programming", "penguin": "programming",
    "prime": "programming", "hermes": "programming", "deepagents": "programming",
}


class ModelCatalogError(ValueError):
    """固定错误码，不把私有目录、配置正文或凭证放进异常消息。"""


def model_catalog_path(environment: dict | None = None) -> str:
    """调用时读取操作者声明；保留 _MODELS_PATH 作为默认兼容/测试接口。

    子进程启动前可用此函数把相对路径/~/目录引用固定为文件绝对路径，
    避免后续更换 HOME 或 cwd 改变同一声明的含义。这里只定位，不复制配置。
    """
    env = os.environ if environment is None else environment
    if "MEYO_MODEL_CATALOG_PATH" not in env:
        # Keep the legacy override seam, but the unmodified default follows
        # the current runtime HOME rather than an unrelated import-time HOME.
        return (os.path.expanduser("~/.claude/skills/aigc-model-router/models.json")
                if _MODELS_PATH == _IMPORTED_MODELS_PATH else _MODELS_PATH)
    raw = env["MEYO_MODEL_CATALOG_PATH"]
    if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
        raise ModelCatalogError("model_catalog_unavailable")
    path = raw.strip()
    if path == "~" or path.startswith("~/"):
        home = env.get("HOME") or os.path.expanduser("~")
        path = os.path.join(home, path[2:]) if path != "~" else home
    else:
        path = os.path.expanduser(path)
    path = os.path.abspath(path)
    return os.path.join(path, "models.json") if os.path.isdir(path) else path


def _load_models() -> dict:
    explicit = "MEYO_MODEL_CATALOG_PATH" in os.environ
    try:
        path = model_catalog_path()
        if explicit and not os.path.isfile(path):
            raise ModelCatalogError("model_catalog_unavailable")
        with open(path, "r", encoding="utf-8") as stream:
            models = json.load(stream)
        if explicit and not isinstance(models, dict):
            raise ModelCatalogError("model_catalog_unavailable")
        return models
    except Exception:
        if explicit:
            raise ModelCatalogError("model_catalog_unavailable") from None
        return {}


# appid×model 交叉矩阵(552次真实HTTP实测·比 models.json 手配的 appids 更全)。
# 根因(2026-09-01 真跑揪出):models.json 里最强模型 gpt-5.6-sol 只手配 1 个 appid,
# 但 matrix 实测它能在 8 个 appid 上跑。resolve_assignments 的"独立 appid 分流"若只看
# 手配的 1 个,会误判 sol 稀缺→把后面几家降级到 appid 多但网关会拒/限流的 gpt-5.4→
# 简单题满屏接力跑 85s。这里补全 appid 认知(与 legion_core._appids_for_model_full 同源),
# 让 sol/opus 也有 8 个独立 appid,首轮各家都能拿最强模型+独立 appid,不再被逼降级。
_APPID_MATRIX_CACHE = {"data": None}


def _load_appid_matrix() -> dict:
    if "MEYO_MODEL_MATRIX_PATH" in os.environ:
        try:
            raw = os.environ["MEYO_MODEL_MATRIX_PATH"]
            if not raw.strip() or "\0" in raw:
                raise ValueError
            path = os.path.abspath(os.path.expanduser(raw.strip()))
            with open(path, "r", encoding="utf-8") as stream:
                value = json.load(stream)
            if not isinstance(value, dict) or not isinstance(value.get("by_model"), dict):
                raise ValueError
            return value
        except (OSError, ValueError, TypeError):
            raise ModelCatalogError("model_matrix_unavailable") from None
    # 显式目录是独立的授权声明，不能从默认 HOME 或此前缓存追加其它凭证。
    # 只授权一个 models.json 不等于同时授权它的邻接文件；默认行为不变。
    if "MEYO_MODEL_CATALOG_PATH" in os.environ:
        return {}
    if _APPID_MATRIX_CACHE["data"] is None:
        mp = os.path.expanduser("~/.claude/skills/aigc-model-router/appid_matrix.json")
        try:
            _APPID_MATRIX_CACHE["data"] = json.load(open(mp, "r", encoding="utf-8"))
        except Exception:
            _APPID_MATRIX_CACHE["data"] = {}
    return _APPID_MATRIX_CACHE["data"]


import re as _re

# ── 便宜档本地登记(2026-09-01 真探网关确认·router 里还没这几个名字)────────────
# 用户 2026-09-01:"便宜的那些 LongCat-2.0/deepseek-v4-flash/GLM-5.3-flash 放一起,
# 就是小便宜的"。这仨真名已带 NO_PROXY 直打网关 /v1/openai/native 确认返回真 content
# (LongCat-2.0=14字·deepseek-v4-flash=21字·GLM-5.3-flash=73字),但 models.json/matrix
# 还没登记它们。为避免"表里有网关没有"(gpt-5.4 那类坑),这里【本地登记】它们的真名+
# 便宜标签+真实可用 appid,让 recommended_for 的过滤器认它们,不改全局 router。
# appids 用白名单(gpt-5全家桶通用面·实测这仨在首个白名单 appid 上真出 content)。
_CHEAP_LOCAL_MODELS = {
    "LongCat-2.0":       {"tag": "便宜", "note": "小便宜·真探可用"},
    "deepseek-v4-flash": {"tag": "便宜", "note": "小便宜·真探可用"},
    "GLM-5.3-flash":     {"tag": "便宜/中文", "note": "小便宜·中文·真探可用"},
}


# ── 按"活的性质"选模型(用户 2026-09-01 钉死映射)────────────────────────────────
# 核心诉求:让模型选择真正对应【这活是什么性质】——写代码/深度推理/中文/长文/快答/看图/生图。
# 规划器(总指挥模型·context not control)读现场判断活的性质→给出 scene→registry 按 scene 的
# 强档/性价比档/便宜档挑模型(配合独立 appid 分流)。每档按用户给的真实映射排序,全部真探可用。
#   strong=最强(复杂交付/难题)· value=性价比(日常主力)· cheap=便宜(快答/省钱)
# 用户映射原文:
#   写代码=强 sol/GLM-5.3 · 性价比 deepseek-v4-pro/kimi-k3 · 便宜 deepseek-v4-flash/LongCat-2.0/GLM-5.3-flash/GLM-5.2
#   深度推理=同写代码 · 长文=同写代码
#   看图=vision + 性价比补 MiniMax-M3/kimi-k3
#   中文=GLM/kimi/minimax/deepseek/longcat 都是中文·按强/性价比分档
#   快答=主力模型优先；低延迟来自一次调用/少编排，不以降模型换速度
#   生图=image_generation
_SCENE_MODELS = {
    # 写代码/深度推理/长文 —— 同一组(用户:深度推理跟编程一样·长文同写代码)
    "code": {
        "strong": ["GLM-5.3", "deepseek-v4-pro", "kimi-k3", "MiniMax-M3"],
        "value":  ["deepseek-v4-pro", "kimi-k3", "MiniMax-M3"],
        "cheap":  ["deepseek-v4-flash", "LongCat-2.0", "GLM-5.3-flash", "GLM-5.2"],
    },
    # 中文 —— GLM/kimi/minimax/deepseek/longcat 都是中文·按强/性价比/便宜分档
    "chinese": {
        "strong": ["GLM-5.3", "deepseek-v4-pro"],
        "value":  ["kimi-k3", "MiniMax-M3", "GLM-5.2"],
        "cheap":  ["GLM-5.3-flash", "deepseek-v4-flash", "LongCat-2.0"],
    },
    # 快答 —— 与正式主力一致。降时延靠一次调用与跳过编排，不把最终答案降级给便宜模型。
    "fast": {
        "strong": ["GLM-5.3", "MiniMax-M3", "deepseek-v4-pro"],
        "value":  ["GLM-5.3", "kimi-k3"],
        "cheap":  ["LongCat-2.0", "GLM-5.3-flash", "deepseek-v4-flash", "GLM-5.2"],
    },

    # 看图 —— vision + 性价比补 MiniMax-M3/kimi-k3(用户钉死:不要gpt/opus·MiniMax-M3多模态已验证)
    "vision": {
        "strong": ["MiniMax-M3", "kimi-k3"],
        "value":  ["MiniMax-M3", "kimi-k3", "GLM-5.3"],
        "cheap":  ["MiniMax-M3", "GLM-5.3"],
    },
    # 生图(2026-09-02 24query验收根修):scene 池选的是【chat 型 harness 的驱动模型】——生图
    # 本身走共享工具 gen_image(工具内部自己路由图像模型)·绝不能把 gpt-image-2 当对话模型塞给
    # harness(chat completions 物理不支持→HTTP 400 白烧一轮·hermes×gpt-image-2 bad:5 实锤)。
    # 驱动模型要会调工具+懂中文·与 code 同池(强→性价比→便宜)。
    "image": {
        "strong": ["GLM-5.3", "MiniMax-M3", "deepseek-v4-pro"],
        "value":  ["deepseek-v4-pro", "kimi-k3", "MiniMax-M3"],
        "cheap":  ["deepseek-v4-flash", "LongCat-2.0", "GLM-5.3-flash", "GLM-5.2"],
    },
}
# 深度推理/长文 = 写代码同组(用户明确);别名指向 code,不重复维护
_SCENE_ALIASES = {
    "code": "code", "coding": "code", "programming": "code", "写代码": "code",
    "deep_reasoning": "code", "reasoning": "code", "深度推理": "code",
    "long_form": "code", "long_context": "code", "长文": "code",
    "research": "code", "analysis": "code", "plan": "code",
    "chinese": "chinese", "中文": "chinese",
    "fast": "fast", "quick": "fast", "快答": "fast", "simple": "fast",
    "vision": "vision", "vision_understanding": "vision", "看图": "vision", "ocr": "vision",
    "image": "image", "image_generation": "image", "生图": "image",
}


# scene 名 → router 物理方向(给 extra 备胎池用·router 只有 5 个物理方向)
_SCENE_TO_ROUTER = {
    "code": "programming", "chinese": "programming", "fast": "programming",
    "vision": "vision_understanding", "image": "image_generation",
}


def _normalize_dir_for_router(direction: str) -> str:
    scene = _SCENE_ALIASES.get((direction or "").strip().lower(), "")
    if scene:
        return _SCENE_TO_ROUTER.get(scene, "programming")
    # 已经是 router 物理方向名就原样用,否则退 programming
    return direction if direction in (
        "programming", "vision_understanding", "image_generation",
        "video_generation", "general_chat_high_coverage") else "programming"


def scene_models(scene: str, tier: str = "") -> list:
    """按活的性质(scene)取模型列表。tier 空=强+性价比+便宜全给(降级备胎),给了只取该档。
    未知 scene 退 code(编程通用面最广)。返回真名列表(去重保序)。"""
    key = _SCENE_ALIASES.get((scene or "").strip().lower(), "code")
    grp = _SCENE_MODELS.get(key, _SCENE_MODELS["code"])
    if tier in ("strong", "value", "cheap"):
        return list(dict.fromkeys(grp.get(tier) or []))
    # 全给:强→性价比→便宜,作降级链
    out = []
    for t in ("strong", "value", "cheap"):
        out.extend(grp.get(t) or [])
    return list(dict.fromkeys(out))


# 每家精选模型(用户:各家留2-3个最强+性价比·不塞全量·KM 2781925462 真可用为准)
#   每项 (model, 标签)·标签给用户看为什么选它。GLM-5.4 KM 无·最高 5.3。
# 各家 = 最强档 + 便宜档(小省钱模型·顶替原 gpt-5.4)。便宜档真探网关可用(见 _CHEAP_LOCAL_MODELS)。
# 用户 2026-09-01:LongCat-2.0/deepseek-v4-flash/GLM-5.3-flash 是"小便宜的",归一档。
# ★picks 也读 manifest(单一事实源)·下面手写 dict 退为兜底(manifest 缺失/该家没登记时)。
_MF_PICKS = {k: [tuple(p) for p in (m.get("model_picks") or [])] for k, m in _MF.items() if m.get("model_picks")}
_HARNESS_PICKS = {
    # 主力彻底换血(2026-09-08用户钉死:gpt系/opus系全退役·不好使了)。
    # 只用 GLM-5.3 / deepseek-v4-pro / kimi-k3 / MiniMax-M3 四强:
    # GLM-5.3(深核/工具/中文)·deepseek-v4-pro(思考型)·kimi-k3(长上下文推理)·MiniMax-M3(多模态)。
    # 观察器按战绩自动上下位,这是启动默认不是硬绑定。
    "codex":     [("GLM-5.3", "深核/中文"), ("deepseek-v4-pro", "思考型"), ("kimi-k3", "长上下文推理"), ("kimi-k2.7-code-highspeed", "最快代码")],
    "cc":        [("GLM-5.3", "深核/中文"), ("deepseek-v4-pro", "思考型"), ("MiniMax-M3", "多模态")],
    "hermes":    [("GLM-5.3", "中文/工具"), ("GLM-5.3-flash", "便宜/中文"), ("deepseek-v4-pro", "思考型")],
    "deepseek":  [("deepseek-v4-pro", "最强思考"), ("GLM-5.3", "中文"), ("deepseek-v4-flash", "便宜")],
    "penguin":   [("GLM-5.3", "最强"), ("kimi-k3", "思考"), ("LongCat-2.0", "便宜")],
    "prime":     [("GLM-5.3", "最强"), ("deepseek-v4-pro", "代码验证"), ("deepseek-v4-flash", "便宜")],
    "jcode":     [("GLM-5.3", "最强稳定"), ("deepseek-v4-flash", "便宜/多appid"), ("kimi-k2.7-code-highspeed", "最快代码")],
    "deepagents": [("GLM-5.3", "最强"), ("deepseek-v4-flash", "便宜"), ("deepseek-v4-pro", "思考")],
}
_HARNESS_PICKS.update(_MF_PICKS)
_DEFAULT_PICKS = [("GLM-5.3", "最强"), ("deepseek-v4-pro", "思考"), ("MiniMax-M3", "多模态")]
def recommended_for(harness_key: str) -> list:
    """某家精选 2-3 个推荐模型(带标签·display_name·稳定性)。前端默认只显示这几个·点'全部'才展开。"""
    if explicit_responses_status(harness_key) is not None:
        return list_models("programming", harness_key=harness_key)["candidates"]
    picks = _HARNESS_PICKS.get(harness_key, _DEFAULT_PICKS)
    # 只保留当下真可用的(在 models.json 候选里的·未坏)。便宜档本地登记模型(_CHEAP_LOCAL_MODELS·
    # 真探网关可用但 router 还没登记)也认——否则它们会被 router 池过滤掉,顶不了 gpt-5.4 的位。
    lm = list_models("programming", harness_key=harness_key)
    avail = {c["model"]: c for c in lm["candidates"]}
    out = []
    for model, tag in picks:
        c = avail.get(model)
        if c:
            out.append({**c, "pick_tag": tag})
        elif model in _CHEAP_LOCAL_MODELS:
            _meta = _CHEAP_LOCAL_MODELS[model]
            out.append({"model": model, "display_name": display_name(model),
                        "usable": True, "stable": True, "rank": 6,
                        "note": _meta["note"], "pick_tag": tag})
    return out


# 稳定性徽章(基于 status·给用户看的·不暴露内部机制)
_STABILITY = {
    "PASS": {"label": "稳定", "icon": "✅", "level": "stable"},
    "PASS_VERIFIED": {"label": "已验证", "icon": "✅", "level": "stable"},
    "PASS_UNSTABLE": {"label": "偶发失败·会自动重试", "icon": "⚠️", "level": "unstable"},
    "PASS_VERIFIED_UNSTABLE": {"label": "已验证·偶发失败", "icon": "⚠️", "level": "unstable"},
}


def _stars(rank) -> int:
    """rank 排名 → 智力星级(1=最强5星)。rank 是官方能力排序。"""
    try:
        r = int(rank)
    except (TypeError, ValueError):
        return 3
    return {1: 5, 2: 4, 3: 4}.get(r, 5 if r == 1 else (3 if r <= 6 else 2))


def _clean_note(note: str) -> str:
    """脱敏 note:去掉 appid/覆盖率等内部凭证统计(用户不该看),只留能力/速度/价格描述。"""
    s = str(note or "")
    # 去内部路由细节:"走vertex路径" "vertex路径" "aws路径"(用户不该看云路由)
    s = _re.sub(r"[,，。;；、\s]*走?\s*(vertex|aws|azure|gcp)\s*路径", "", s, flags=_re.I)
    # Python 的 \b 按 Unicode 单词边界判断：中文紧贴 Vertex（如“唯一活着的Opus·Vertex全灭后”）
    # 时两侧都算 word，\b 匹配不到，内部路由词会泄漏到用户 note。改用字母边界负向环视，
    # 既覆盖中英文紧贴，也不误删普通单词里偶然包含 aws/vertex 的片段。
    s = _re.sub(r"(?<![A-Za-z])(vertex|aws|azure|gcp)(?![A-Za-z])", "", s, flags=_re.I)
    # 去 appid 相关内部细节:"7个appid验证" "对全部157个appid" "appid可用"
    s = _re.sub(r"[（(]?\s*覆盖率[^,，。;；)）]*[)）]?", "", s)
    s = _re.sub(r"[（(]?\s*\d+\s*/\s*\d+\s*(个)?\s*(appid)?[^,，。;；)）]*[)）]?", "", s, flags=_re.I)
    s = _re.sub(r"[,，。;；、\s]*[（(]?[^,，。;；)）]*appid[^,，。;；)）]*[)）]?", "", s, flags=_re.I)
    # 收尾:去多余标点·截到句号边界更自然
    s = _re.sub(r"\s+", " ", s).strip("·,，。;；、() （） ")
    if len(s) > 60:
        cut = s[:60]
        # 尽量断在标点处
        for sep in ("，", "。", "、", ",", " "):
            idx = cut.rfind(sep)
            if idx > 30:
                cut = cut[:idx]
                break
        s = cut.strip("·,，。;；、 ") + "…"
    return s


def _stability(status: str) -> dict:
    return _STABILITY.get(status, {"label": "可用", "icon": "•", "level": "stable"})


def display_name(model: str) -> str:
    """给用户看的干净模型名:去掉 vertex./aws. 等内部路由前缀 + claude- 前缀,美化。

    底层调用仍用真名(model 字段不变),这里只是展示名——同"不显示 appid"的道理:
    内部路由细节(走哪条云路径)用户不该看。
    如 aws.claude-opus-4.7 → Opus 4.7·aws.claude-haiku-4.5 → Haiku 4.5。
    """
    s = str(model or "")
    s = _re.sub(r"^(vertex|aws|azure|gcp)\.", "", s, flags=_re.I)   # 去云路由前缀
    s = _re.sub(r"^claude-", "", s, flags=_re.I)                     # 去 claude- 前缀
    # Opus/Haiku/Sonnet 首字母大写 + 型号前连字符转空格(Opus-4.8 → Opus 4.8)更像品牌名
    s = _re.sub(r"\b(opus|haiku|sonnet)-", lambda m: m.group(1).capitalize() + " ", s)
    s = _re.sub(r"\b(opus|haiku|sonnet)\b", lambda m: m.group(1).capitalize(), s)
    return s


def _catalog_models(direction: str = "programming") -> dict:
    """某方向候选模型(供前端下拉切换)。带智力星级 + 稳定性徽章 + 脱敏描述。

    绝不暴露 appid 等内部凭证细节(用户约束)。
    """
    d = _load_models()
    if not d:
        return {"direction": direction, "candidates": [], "error": "models.json 不可读"}
    broken = set(m.get("model") if isinstance(m, dict) else m
                 for m in (d.get("known_broken_models") or {}).get("models") or [])
    # 退役黑名单(2026-09-08用户钉死:gpt系/opus系全退役·不好使了)——不管models.json怎么排,
    # 前端下拉/候选池/自愈fallback都硬过滤,只留 GLM-5.3/deepseek/kimi/MiniMax/国产系。
    retired = {m for m in broken}
    if "MEYO_MODEL_CATALOG_PATH" not in os.environ:
        retired |= {m for m in (c.get("model") for c in (d.get(direction) or {}).get("candidates", []))
                    if m and (m.startswith("gpt-") or "opus" in str(m).lower())}
    cands = []
    for c in (d.get(direction) or {}).get("candidates", []):
        model = c.get("model")
        stab = _stability(c.get("status"))
        cands.append({
            "model": model,                              # 底层真名(调用用·前端不直接显示)
            "display_name": display_name(model),        # 用户显示名(Opus 4.8·无vertex/aws前缀)
            "rank": c.get("rank"),
            "stars": _stars(c.get("rank")),          # 智力星级(rank换算)
            "stability": stab["label"],               # 稳定性文字
            "stability_icon": stab["icon"],           # ✅/⚠️
            "stable": stab["level"] == "stable",
            "note": _clean_note(c.get("note")),        # 脱敏后的能力描述
            "usable": model not in retired,
        })
    cands = [c for c in cands if c["usable"]]
    # 展示排序:先按智力星级(高→低),同星级稳定的排前面
    cands.sort(key=lambda x: (-x["stars"], 0 if x["stable"] else 1, x.get("rank") or 99))
    return {"direction": direction, "candidates": cands,
            "default": cands[0]["model"] if cands else None,
            "count": len(cands)}


def list_models(direction: str = "programming", *, harness_key: str = "") -> dict:
    """Overlay the named member's configuration without making it a global default."""
    status = explicit_responses_status(harness_key or "codex")
    if harness_key == "codex" and status is not None:
        rows = ([{**status, "usable": True, "stable": False,
                  "stability": "已配置·未验证", "stability_icon": "•",
                  "note": "当前 Codex 配置；尚未验证模型调用"}]
                if status["configured"] and direction in
                ("programming", "general_chat_high_coverage") else [])
        return {"direction": direction, "harness": harness_key, "candidates": rows,
                "default": rows[0]["model"] if rows else None, "count": len(rows),
                "configured": status["configured"], "provider_verified": False}
    result = _catalog_models(direction)
    if not harness_key and status is not None:
        scoped = list_models(direction, harness_key="codex")["candidates"]
        if scoped:
            result = {**result, "candidates": [*result["candidates"], *scoped],
                      "count": len(result["candidates"]) + len(scoped)}
            # A missing old catalog is not an error for a configured member.
            # Its model remains scoped; do not promote it to the global default.
            result.pop("error", None)
    return result


def all_directions() -> list:
    d = _load_models()
    directions = [k for k in ("programming", "vision_understanding", "image_generation",
                              "general_chat_high_coverage") if k in d]
    if explicit_responses_status("codex") is not None and "programming" not in directions:
        directions.insert(0, "programming")
    return directions


def verified_catalog() -> dict:
    """KM 2781925462 真测可用89款·按家族分组(高级探索用·普通UI仍只显每家精选3款)。

    不含appid/内部凭证,只有模型/家族/实测延迟/日期。GLM最高5.3·没有5.4不编造。
    """
    try:
        d = json.load(open(_CATALOG_PATH, "r", encoding="utf-8"))
    except Exception:
        return {"count": 0, "families": [], "models": []}
    groups = {}
    for m in d.get("models", []):
        x = dict(m)
        x["display_name"] = display_name(x["model"])
        groups.setdefault(x["family"], []).append(x)
    families = [{"name": name, "count": len(ms),
                 "models": sorted(ms, key=lambda x: x.get("latency_s", 99))}
                for name, ms in groups.items()]
    return {"count": d.get("count", len(d.get("models", []))),
            "verified_at": d.get("verified_at"), "source": d.get("source"),
            "families": families}


# ── 用户偏好(固定某家用某模型) ──
def _load_prefs() -> dict:
    try:
        return json.load(open(_PREFS_PATH, "r", encoding="utf-8"))
    except Exception:
        return {}


def _vendor(model: str) -> str:
    """模型名→厂商家族(用于识别新厂商/更高代际)。"""
    low = str(model or "").lower()
    rules = [("OpenAI", ("gpt-", "o3-", "o4-")),
             ("Anthropic", ("claude", "opus", "sonnet", "haiku")),
             ("GLM", ("glm",)), ("Moonshot/Kimi", ("kimi", "moonshot")),
             ("DeepSeek", ("deepseek",)), ("Google", ("gemini",)),
             ("MiniMax", ("minimax",)), ("Doubao", ("doubao",)),
             ("LongCat", ("longcat",)), ("Qwen", ("qwen",))]
    for vendor, prefixes in rules:
        if low.startswith(prefixes) or any(("." + p) in low for p in prefixes):
            return vendor
    return str(model or "").split("-")[0].split(".")[0] or "Other"


def _generation(model: str) -> list:
    """抽版本数字序列(GLM-5.3→[5,3], gpt-5.6-sol→[5,6])供更高代判断。"""
    import re
    m = re.search(r"(?<!\d)(\d+)(?:\.(\d+))?", str(model or ""))
    return [int(m.group(1)), int(m.group(2) or 0)] if m else [0, 0]


def _current_model_records() -> list:
    """聚合 models.json 各方向 + KM verified catalog,作为自动发现源。"""
    records = {}
    d = _load_models()
    for direction in all_directions():
        for c in (d.get(direction) or {}).get("candidates", []):
            model = c.get("model")
            if model:
                records.setdefault(model, {"model": model, "vendor": _vendor(model),
                                           "generation": _generation(model), "source": "models.json",
                                           "direction": direction})
    cat = verified_catalog()
    for fam in cat.get("families", []):
        for c in fam.get("models", []):
            model = c.get("model")
            if model:
                records.setdefault(model, {"model": model, "vendor": _vendor(model),
                                           "generation": _generation(model), "source": "KM verified catalog",
                                           "direction": "verified"})
    return list(records.values())


def discover_new_models(current_models: list | None = None, state_path: str | None = None,
                        initialize: bool = True) -> dict:
    """自动发现新模型/新厂商/更高代并标 NEW。

    每次扫描 models.json + KM真测catalog,与持久化known快照比较:
    - 新模型→is_new
    - 从未见过的厂商→new_vendor
    - 同厂商版本高于历史max→higher_generation
    首次运行把现状当baseline(不把89款全刷NEW);后续任何来源新增自动标记。
    """
    path = state_path or _DISCOVERY_PATH
    current = current_models if current_models is not None else _current_model_records()
    try:
        state = json.load(open(path, "r", encoding="utf-8"))
    except Exception:
        state = {}
    if not state and initialize:
        vendors = {}
        for r in current:
            g = r.get("generation") or [0, 0]
            if g > vendors.get(r.get("vendor"), [0, 0]):
                vendors[r.get("vendor")] = g
        state = {"known_models": sorted({r["model"] for r in current}),
                 "known_vendors": sorted({r.get("vendor") for r in current}),
                 "vendor_max_generation": vendors, "new": {},
                 "initialized_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            json.dump(state, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        except Exception:
            pass
        return {"new": [], "new_count": 0, "total": len(current), "initialized": True}

    known = set(state.get("known_models", [])); known_vendors = set(state.get("known_vendors", []))
    vmax = state.get("vendor_max_generation", {})
    fresh = []
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for r in current:
        if r["model"] in known:
            continue
        vendor, gen = r.get("vendor"), r.get("generation") or [0, 0]
        item = {**r, "is_new": True, "new_vendor": vendor not in known_vendors,
                "higher_generation": gen > vmax.get(vendor, [0, 0]), "first_seen": now}
        fresh.append(item)
        state.setdefault("new", {})[r["model"]] = item
        known.add(r["model"]); known_vendors.add(vendor)
        if gen > vmax.get(vendor, [0, 0]):
            vmax[vendor] = gen
    state["known_models"] = sorted(known); state["known_vendors"] = sorted(known_vendors)
    state["vendor_max_generation"] = vmax; state["last_scanned_at"] = now
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        json.dump(state, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception:
        pass
    # 未ack的新(含之前扫描发现但未ack)都返回NEW
    unacked = [v for v in state.get("new", {}).values() if not v.get("acked")]
    return {"new": unacked, "new_count": len(unacked), "discovered_now": fresh,
            "discovered_now_count": len(fresh), "total": len(current), "initialized": False}


def ack_new_models(state_path: str | None = None) -> dict:
    """用户看过 NEW 后清标(模型仍在catalog)。"""
    path = state_path or _DISCOVERY_PATH
    try:
        state = json.load(open(path, "r", encoding="utf-8"))
        for v in state.get("new", {}).values():
            v["acked"] = True
        json.dump(state, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        return {"ok": True, "acked": len(state.get("new", {}))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_prefs() -> dict:
    """用户设的 {harness_key: model} + 全局默认。"""
    return _load_prefs()


def set_pref(harness_key: str, model: str | None) -> dict:
    """为某家固定模型(model=None 清除·回到智能默认)。返回最新 prefs。"""
    with _PREFS_LOCK:  # load→改→save 全程持锁·防并发写丢更新(共享单文件竞态)
        prefs = _load_prefs()
        if model:
            prefs[harness_key] = model
        else:
            prefs.pop(harness_key, None)
        try:
            os.makedirs(os.path.dirname(_PREFS_PATH), exist_ok=True)
            json.dump(prefs, open(_PREFS_PATH, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception as e:
            return {"error": f"保存失败: {e}", "prefs": prefs}
        return {"ok": True, "prefs": prefs}


def resolve_model(harness_key: str, direction: str | None = None) -> dict:
    """解析某家最终该用的模型(优先级:用户固定 > 该家精选picks首档 > 该方向智能默认)。

    2026-09-08真跑实锤修复:旧链只看方向默认(models.json rank1·gpt-5.6-sol)——_HARNESS_PICKS
    主力换血(GLMs为首)只影响前端推荐,执行链根本不经过它→island复刻两家还是sol交出过曝/几何体。
    现在picks首档(真可用时)直接成为执行默认,榜单上下位仍由模型观察器按战绩调整。
    """
    explicit = explicit_responses_status(harness_key)
    if explicit is not None:
        return explicit
    prefs = _load_prefs()
    if prefs.get(harness_key):
        m = prefs[harness_key]
        return {"model": m, "display_name": display_name(m), "source": "user_fixed"}
    try:
        _rec = recommended_for(harness_key)
        if _rec:
            return {"model": _rec[0]["model"],
                    "display_name": _rec[0].get("display_name") or display_name(_rec[0]["model"]),
                    "source": "harness_pick"}
    except Exception:
        pass
    direction = direction or _HARNESS_DIRECTION.get(harness_key, "programming")
    lm = list_models(direction, harness_key=harness_key)
    m = lm.get("default")
    return {"model": m, "display_name": display_name(m) if m else None,
            "source": "smart_default", "direction": direction}


# ── per-harness 模型健康(2026-09-02 健康闭环根修)───────────────────────────────
# 病根(真跑实锤):resolve_assignments 组池时不看 model_health——penguin×deepseek-v4-flash
# 健康库早记 bad 3 次("Model is not in the Project config"·该家 provider 不认这个模型),
# 但"快答"场景池照样把它排池首塞给 penguin→白烧一整轮→靠接力兜底。健康数据记了不消费=记录
# 没闭环(与 :590 注释里 hermes×gpt-image-2 bad:5 同一个病)。
# 修法(context not control·不硬禁用):组池后按健康重排——这家跑某模型 bad 明显多于 good 的
# 降后(仍留在池里可被穷尽选到·治"忽好忽坏"同款·与 select_usable_model 的 _health_rank 同语义)。
# 依据:hermes cron/scheduler_provider.py:82-89(named provider 不可用→回退 built-in·健康感知排序)。
_HEALTH_CACHE = {"data": None, "mtime": 0.0}


def _load_health() -> dict:
    """读 model_health.json(每家×模型 good/bad 计数)。mtime 失效重读·读不了返回空 dict。"""
    try:
        import legion_paths as _lp
        path = _lp.home_under("model_health.json")
    except Exception:
        path = os.path.expanduser("~/.meyo/legion/model_health.json")
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        mtime = 0.0
    if _HEALTH_CACHE["data"] is None or _HEALTH_CACHE.get("mtime") != mtime:
        try:
            with open(path) as f:
                _HEALTH_CACHE["data"] = json.load(f)
        except Exception:
            _HEALTH_CACHE["data"] = {}
        _HEALTH_CACHE["mtime"] = mtime
    return _HEALTH_CACHE["data"] or {}


def _health_rank(harness_key: str, model: str) -> int:
    """该家跑该模型的健康分:good-bad(正=近期成功多·负=失败多·无记录=0 中性)。"""
    if not harness_key or not model:
        return 0
    rec = (_load_health().get(harness_key) or {}).get(model)
    if not rec:
        return 0
    return int(rec.get("good", 0)) - int(rec.get("bad", 0))


def resolve_overrides(plan: dict, keys: list) -> dict:
    """兼容旧调用:返回每家模型名。新代码优先用 resolve_assignments。"""
    return {k: v["model"] for k, v in resolve_assignments(plan, keys).items()}


def _raw_candidate(model: str):
    d = _load_models()
    for direction in all_directions():
        for c in (d.get(direction) or {}).get("candidates", []):
            if c.get("model") == model:
                return {**c, "direction": direction}
    return None


def input_modalities(model: str, environment: dict | None = None) -> list[str] | None:
    """Read declared model inputs; a compatible API protocol is not a capability."""
    source = os.environ if environment is None else environment
    if source.get("LEGION_CODEX_TRANSPORT", "").strip().lower() == "responses":
        binding = explicit_responses_binding(source)
        if binding["model"] != model:
            return None
        value = binding.get("input_modalities")
        return list(value) if value is not None else None
    value = (_raw_candidate(model) or {}).get("input")
    if value is None or value == []:
        return None
    if (not isinstance(value, list)
            or any(not isinstance(item, str) or not item.strip() for item in value)):
        raise ValueError("model input metadata must be a list of non-empty strings")
    return sorted({item.strip().lower() for item in value})


def endpoint_type(model: str) -> str:
    """从模型目录事实推导 endpoint 类型，避免把 image/video 模型塞给 chat harness。"""
    candidate = _raw_candidate(model) or {}
    endpoint = str(candidate.get("endpoint") or "").lower()
    direction = str(candidate.get("direction") or "")
    if endpoint in ("images", "image") or direction == "image_generation":
        return "image"
    if endpoint in ("videos", "video") or direction == "video_generation":
        return "video"
    if direction == "vision_understanding":
        return "vision"
    return "chat"


def responses_endpoint(model: str, gateway: str = "", *, explicit: bool = False) -> dict | None:
    """把已知 Friday 网关解析为官方 Responses 入口，不猜第三方协议。

    协议依据：KM 2720941091 §2、2779857423 §4.4。
    显式传入 Responses 地址时保留调用方选择；仅把既有 Friday Chat 默认入口
    转成文档规定的模型供应方入口。返回值不包含授权，也不改变所选模型。
    """
    from urllib.parse import urlsplit, urlunsplit

    if explicit:
        # An explicit provider is a protocol declaration, not a guessed vendor.
        # Never substitute the company default or attach company-only headers.
        if (not isinstance(gateway, str) or not gateway.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in gateway)):
            return None
        try:
            parsed = urlsplit(gateway.strip().rstrip("/"))
            port = parsed.port
        except ValueError:
            return None
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or "\\" in gateway
                or parsed.scheme not in ("https", "http")
                or (port is not None and not 1 <= port <= 65535)):
            return None
        if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "::1", "localhost"):
            return None
        path = parsed.path.rstrip("/")
        if path.endswith(("/chat/completions", "/completions")):
            return None
        path = path.removesuffix("/responses")
        return {"base_url": urlunsplit((parsed.scheme, parsed.netloc, path, "", "")),
                "http_headers": {}}

    value = (gateway or os.environ.get("MEYO_UNIFIED_GATEWAY")
             or "https://aigc.sankuai.com/v1/openai/native").strip().rstrip("/")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname != "aigc.sankuai.com"
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        return None
    path = parsed.path.rstrip("/")
    if path in ("", "/v1/openai/native", "/v1/openai/native/chat/completions"):
        path = "/v1" if _vendor(model) == "OpenAI" else "/agentic/v1"
    elif path in ("/v1/responses", "/agentic/v1/responses"):
        path = path.removesuffix("/responses")
    elif path not in ("/v1", "/agentic/v1"):
        return None
    headers = {"X-Source": "meyo-legion"}
    if path == "/v1" and _vendor(model) == "OpenAI":
        headers["x-context-provider"] = "Azure"
    return {"base_url": urlunsplit((parsed.scheme, parsed.netloc, path, "", "")),
            "http_headers": headers}


def explicit_responses_binding(environment: dict | None = None) -> dict:
    """Resolve one operator-declared pair; never consult the company key pool.

    Private return values belong only in execution arguments/environment, not
    task facts, prompts, public model catalogs or result records.
    """
    source = os.environ if environment is None else environment
    try:
        config = json.loads(source.get("LEGION_CODEX_RESPONSES_BINDING") or "")
        model, key_name = config["model"], config["api_key_env"]
        if (not isinstance(model, str) or not model.strip() or model != model.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in model)
                or not isinstance(key_name, str)
                or not _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_name)):
            raise ValueError()
        endpoint = responses_endpoint(model, config["base_url"], explicit=True)
        key = source.get(key_name)
        if (endpoint is None or not isinstance(key, str) or not key.strip()
                or key != key.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in key)):
            raise ValueError()
        extra = {}
        if "input_modalities" in config:
            inputs = config["input_modalities"]
            if (not isinstance(inputs, list) or not inputs or "text" not in inputs
                    or any(not isinstance(item, str) or item not in ("text", "image")
                           for item in inputs)):
                raise ValueError()
            extra["input_modalities"] = [item for item in ("text", "image") if item in inputs]
    except (KeyError, TypeError, ValueError):
        raise ValueError("显式 Responses 需要完整的 model/base_url/api_key_env 及对应授权；"
                         "如声明 input_modalities，须包含 text 且仅可包含 text/image") from None
    if "max_output_tokens" in config:
        limit = config["max_output_tokens"]
        if type(limit) is not int or limit <= 0:
            raise ValueError("显式 Responses 的 max_output_tokens 须为正整数")
        extra["max_output_tokens"] = limit
    if "model_auto_compact_token_limit" in config:
        limit = config["model_auto_compact_token_limit"]
        if type(limit) is not int or not 1 <= limit <= 2**63 - 1:
            error = ValueError(
                "显式 Responses 的 model_auto_compact_token_limit 须为 1..9223372036854775807 的整数")
            error.binding_option = "model_auto_compact_token_limit"
            raise error from None
        extra["model_auto_compact_token_limit"] = limit
    if "stream_idle_timeout_ms" in config:
        milliseconds = config["stream_idle_timeout_ms"]
        try:
            valid = (type(milliseconds) is int and milliseconds > 0
                     and milliseconds / 1000 < float("inf"))
        except OverflowError:
            valid = False
        if not valid:
            error = ValueError("显式 Responses 的 stream_idle_timeout_ms 须为正有限整数")
            error.binding_option = "stream_idle_timeout_ms"
            raise error from None
        extra["stream_idle_timeout_ms"] = milliseconds
    if "apply_patch_tool_type" in config:
        if config["apply_patch_tool_type"] != "freeform":
            error = ValueError("显式 Responses 的 apply_patch_tool_type 仅支持 freeform")
            error.binding_option = "apply_patch_tool_type"
            raise error from None
        extra["apply_patch_tool_type"] = "freeform"
    if "error_compatibility_profile" in config:
        if config["error_compatibility_profile"] != "invalid-prompt-max-length-v1":
            error = ValueError("显式 Responses 的 error_compatibility_profile 不受支持")
            error.binding_option = "error_compatibility_profile"
            raise error from None
        extra["error_compatibility_profile"] = config["error_compatibility_profile"]
    return {"model": model, "base_url": endpoint["base_url"], "api_key": key, **extra}


def explicit_responses_status(harness_key: str) -> dict | None:
    """Current allowlisted projection, never a cached credential or runtime verdict."""
    if harness_key != "codex" or os.environ.get("LEGION_CODEX_TRANSPORT", "").strip().lower() != "responses":
        return None
    status = {"model": None, "display_name": None, "source": "operator_binding",
              "harnesses": ["codex"], "configured": False, "verified": False,
              "provider_verified": False}
    try:
        binding = explicit_responses_binding()
    except ValueError:
        return {**status, "configuration_error": "explicit_responses_not_configured"}
    declared = ({"input_modalities": binding["input_modalities"]}
                if "input_modalities" in binding else {})
    return {**status, "model": binding["model"], "display_name": binding["model"],
            "configured": True, **declared}


def _compatible_with_harness(harness_key: str, model: str) -> bool:
    try:
        import harness_model_compat
        if harness_model_compat.is_static_incompatible(harness_key, model):
            return False
        import runtime_manifest
        return runtime_manifest.model_compatible(harness_key, endpoint_type(model))
    except Exception:
        return endpoint_type(model) == "chat"


def _resolve_alias(appid: str, environment: dict | None = None) -> str | None:
    """稳定别名→真实appid。服务器部署的随包配置只含别名(appid-xxxx);
    真实值由部署模板 env MEYO_APPID_ALIASES(格式: alias=值,逗号分隔)注入。
    未配置或别名缺失 → None(该授权如实不可用,不冒充)。"""
    if not str(appid).startswith("appid-"):
        return str(appid)  # 本地开发:配置本就是真实值,原样返回
    env_map = (os.environ if environment is None else environment).get("MEYO_APPID_ALIASES", "")
    for pair in env_map.split(","):
        pair = pair.strip()
        if "=" in pair:
            k, v = pair.split("=", 1)
            if k.strip() == str(appid):
                return v.strip() or None
    return None


def ccr_binding_key(model: str, appid: str, gateway: str) -> str:
    """CCR 配置与消费者共用的绑定标识；映射中不暴露上游密钥。"""
    value = json.dumps([str(model), str(appid), str(gateway).rstrip("/")],
                       ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def ccr_route(model: str, appid: str = "", gateway: str = "",
              environment: dict | None = None) -> str:
    """只选择与本次 ctx 一致的已配置 provider，不修改共享 router 配置。"""
    source = os.environ if environment is None else environment
    if not model or "," in model:
        raise ValueError("CCR 当前模型标识无效")
    raw = source.get("LEGION_CCR_REQUEST_PROVIDERS")
    if raw:
        bindings = json.loads(raw)
        key = appid or source.get("MEYO_UNIFIED_APPID") or source.get("AIGC_APPID") or _pick_appid("cc", model)
        key = _resolve_alias(str(key or ""), source if "MEYO_APPID_ALIASES" in source else None)
        base = gateway or source.get("MEYO_UNIFIED_GATEWAY") or "https://aigc.sankuai.com/v1/openai/native"
        provider = bindings.get(ccr_binding_key(model, key, base)) if isinstance(bindings, dict) and key else None
    else:
        # 老部署仅知道 model->provider，无法证明某个显式 appid 真被使用。
        # 无显式授权的旧调用可继续使用旧配置；显式选择须先升级私有配置。
        if appid:
            raise ValueError("CCR 缺少按请求授权与网关生成的 provider 映射")
        mapping = json.loads(source.get("LEGION_CCR_MODEL_PROVIDERS") or "{}")
        provider = mapping.get(model) if isinstance(mapping, dict) else None
    if not isinstance(provider, str) or not provider.strip() or "," in provider:
        raise ValueError("CCR 未配置当前模型、授权和网关的绑定")
    return provider.strip() + "," + model


def _pick_appid(harness_key: str, model: str) -> str | None:
    """同模型多个有效appid时按harness稳定散列分流(不再全挤第一key)。内部值绝不返回前端。"""
    d = _load_models(); c = _raw_candidate(model) or {}
    exhausted = set((d.get("quota_exhausted_appids") or {}).get("appids") or [])
    appids = [str(a) for a in (c.get("appids") or []) if str(a) not in exhausted]
    appids = [r for r in (_resolve_alias(a) for a in appids) if r]  # 别名解析;缺授权剔除
    if not appids:
        return None
    idx = int(hashlib.sha256(f"{harness_key}:{model}".encode()).hexdigest()[:8], 16) % len(appids)
    return appids[idx]


def _trusted_appids() -> list:
    d = _load_models()
    exhausted = set(str(a) for a in ((d.get("quota_exhausted_appids") or {}).get("appids") or []))
    out = []
    for t in (d.get("trusted_appids") or {}).get("list", []):
        if not t.get("appid") or str(t["appid"]) in exhausted:
            continue
        r = _resolve_alias(str(t["appid"]))
        if r:
            out.append(r)
    return out


def _appids_of(model: str) -> list:
    """某模型真实可用的appid(去掉耗尽的)。

    矩阵里没列appids的模型(如general_chat通用模型MiniMax-M3/deepseek-chat,覆盖率130/131·
    几乎所有appid都能用)→回退trusted白名单,让通用方向也能拿到独立appid做并发分流。
    """
    d = _load_models(); c = _raw_candidate(model) or {}
    exhausted = set(str(a) for a in ((d.get("quota_exhausted_appids") or {}).get("appids") or []))
    # ★耗尽集合统一过别名解析(2026-09-10 监督复现):配置里存别名时,池(_resolve_alias后)
    # 是真实值而 exhausted 还是别名 → 过滤永假 → 已耗尽凭证照样返回。统一口径后过滤才真生效。
    exhausted = set(r for r in (_resolve_alias(a) for a in exhausted) if r)
    # 手配 ∪ matrix 实测 pass_appids(去重保序)——sol/opus 手配只 1 个但实测 8 个可用,
    # 补全后"独立 appid 分流"才不会误判它稀缺、把后面几家降级到坏的 gpt-5.4。
    pool = [str(a) for a in (c.get("appids") or [])]
    try:
        mx = _load_appid_matrix().get("by_model", {}).get(model, {})
        for a in (mx.get("pass_appids") or []):
            if str(a) not in pool:
                pool.append(str(a))
    except Exception:
        pass
    pool = [r for r in (_resolve_alias(a) for a in pool) if r]  # 别名解析;缺授权剔除
    listed = [a for a in pool if a not in exhausted]
    if "MEYO_MODEL_CATALOG_PATH" in os.environ:
        return listed  # Explicit per-model authorization cannot borrow an unrelated trusted key.
    return listed or _trusted_appids()


def resolve_assignments(plan: dict, keys: list) -> dict:
    """为每家分配【适合任务的模型 + 本批次尽量独立的appid】(appid只内部传worker,不暴露UI)。

    优先级:本次model_overrides > 用户固定 > 任务方向候选。
    ★批次级独立appid分流(用户2026-08-28"共用一网关·分不同appid拿合适模型就解决了"):
    并行多家时,若最强模型只挂一个appid(如gpt-5.6-sol只有主力appid),后面的家会【降到有多
    appid的模型】(如gpt-5.4有7个appid)以换取一个本批未占用的独立appid——避免所有家挤同一个
    appid被网关同key并发限流(实测:3家全gpt-5.6-sol@主力→codex卡死)。宁可后家用次强模型+独立
    appid,也不让全批挤死。这不是猜意图:direction由总指挥模型给,父层只做连接池分流。
    """
    plan = plan or {}; temp = dict(plan.get("model_overrides") or {}); prefs = _load_prefs()
    dirs = dict(plan.get("directions") or {})
    used_appids: set = set()
    out = {}
    for pos, key in enumerate(keys):
        direction = dirs.get(key) or _HARNESS_DIRECTION.get(key, "programming")
        explicit = explicit_responses_status(key)
        if explicit is not None:
            requested = temp.get(key)
            selected = requested or explicit["model"]
            out[key] = {"model": selected, "appid": None, "direction": direction,
                        "source": "operator_binding"}
            if not explicit["configured"] or (requested and requested != explicit["model"]):
                out[key]["configuration_error"] = "explicit_responses_binding_conflict"
            # Execution resolves the private binding; do not put its key in the plan.
            continue
        # 候选模型池(按能力顺序):固定模型置顶,否则按【活的性质 scene】选(用户2026-09-01)。
        fixed = temp.get(key) or prefs.get(key)
        source = "request_override" if temp.get(key) else ("user_fixed" if prefs.get(key) else "task_auto")
        # ★ 按活的性质选模型:规划器给的 direction 可能是 scene 名(深度推理/中文/快答/
        #   长文/写代码…)→ scene_models 给出能力优先的降级链。快答也以正式主力开头，速度来自
        #   更短执行路径而不是降低模型质量；scene 未命中时退 code(编程通用)。
        scene_pool = scene_models(direction)   # 强→性价比→便宜降级链
        if fixed:
            # 用户显式固定仍尊重，但协议物理不兼容时不能拿它撞一次才学习；把事实回给调用方，
            # 自动请求则直接选择兼容模型。这里不判断答案，只防 endpoint 类型接错。
            pool_models = [fixed] if _compatible_with_harness(key, fixed) else []
            if not pool_models:
                source += "_incompatible_fallback"
                pool_models = [m for m in scene_pool if _compatible_with_harness(key, m)]
        else:
            # 根修(2026-09-02·24query验收):规划器把"带图表的看板网页"判给 hermes=生图→旧分支把
            # gpt-image-2 这类【文生图模型当对话模型】塞给 chat 型 harness→物理必 HTTP 400
            # "The requested operation is unsupported"(chat completions 不吃 image 模型)·白烧一整轮。
            # model_health 早记 hermes×gpt-image-2 bad:5·但这条路径不消费健康数据=记录没闭环。
            # 事实:军团成员全是 chat 型 harness;生图任务正确做法=正常对话模型+调共享工具 gen_image
            # (工具内部自己路由图像模型)·不把图像模型塞给 harness 当 chat 模型。统一走 scene 对话
            # 池+本家 recommended;router 物理方向(image/video_generation)候选是文生图/视频模型
            # (chat 不兼容)·剔除不混入。
            recs = [r["model"] for r in recommended_for(key)]
            _router_dir = _normalize_dir_for_router(direction)
            extra = []
            if _router_dir not in ("image_generation", "video_generation"):
                extra = [c["model"] for c in list_models(
                         _router_dir, harness_key=key).get("candidates", []) if c.get("usable")]
            pool_models = [m for m in dict.fromkeys(scene_pool + recs + extra)
                           if _compatible_with_harness(key, m)]
            # 能力排名优先：健康账本只把“明确模型失败且从未成功”的组合沉底，不能让累计调用量
            # 多的旧次强模型越级压过 gpt-5.6-sol。失败归因已在 legion_core 写入口收紧，环境、
            # Harness、工具和用户失败不会再进入模型坏账。
            _health = _load_health().get(key) or {}
            if _health:
                pool_models.sort(key=lambda m: (int((_health.get(m) or {}).get("good", 0)) == 0
                                                and int((_health.get(m) or {}).get("bad", 0)) > 0))

        # 从候选池里挑第一个"能拿到本批未占用appid"的模型;拿不到独立appid再退散列复用。
        chosen_model, chosen_appid = None, None
        for m in pool_models:
            if not m:
                continue
            appids = _appids_of(m)
            free = next((a for a in appids if a not in used_appids), None)
            if free:
                chosen_model, chosen_appid = m, free
                break
        if not chosen_model:  # 全被占用:退回首选模型+稳定散列(至少能跑)
            chosen_model = pool_models[0] if pool_models and pool_models[0] else next(
                (c.get("model") for c in list_models("programming", harness_key=key).get("candidates", [])
                 if c.get("usable") and _compatible_with_harness(key, str(c.get("model") or ""))), None)
            chosen_appid = _pick_appid(key, chosen_model) if chosen_model else None
        if chosen_appid:
            used_appids.add(chosen_appid)
        out[key] = {"model": chosen_model, "appid": chosen_appid,
                    "direction": direction, "source": source}
    return out


if __name__ == "__main__":
    print("programming 候选:")
    for c in list_models("programming")["candidates"]:
        print(f"  {c['rank']}. {c['model']} · {'可用' if c['usable'] else '坏'} · {c['note'][:40]}")
    print("\n各家解析:")
    for k in ["codex", "cc", "hermes"]:
        print(f"  {k}: {resolve_model(k)}")
