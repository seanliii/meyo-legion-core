#!/usr/bin/env python3
"""
harness_profiles · 军团各家介绍 + 评价(用户要"每家给介绍和评价·各不相同")

介绍(intro/strengths/style):基于各家原始文档 + 已读源码的理解(非正则抽 README 噪声)。
评价(eval_note):基于 comparison/current_results_summary.json 的真实历史效果(如实标样本量·
  小样本不夸大)+ 各家实测特点。best_for 帮用户/agent 选家。

给 context 不加墙:把每家的真实特点/实测表现如实告诉用户,让他自己选,不替他圈定。
"""
import json
import os
import time
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
_SUMMARY = os.path.join(_REPO, "harness_baselines", "comparison", "current_results_summary.json")

# ★画像单一事实源(用户 2026-09-01"统一成manifest动态发现"):profile 优先读
#   harness_manifest.json·下面手写 _PROFILES 退为【manifest 缺失/该家没登记时的兜底】
#   (铁律0:文件坏了/新家没写 manifest 也不崩·get_profiles 仍有兜底画像·见 _default_profile)。
def _load_profiles_from_manifest() -> dict:
    """从统一运行时 manifest 读取内置和外部成员画像；手写画像只作故障兜底。"""
    try:
        import runtime_manifest
        return runtime_manifest.profiles()
    except Exception:
        return {}

_MANIFEST_PROFILES = _load_profiles_from_manifest()

# 手写精准介绍(基于原始文档+源码理解)。每家不同·如实。manifest 优先覆盖·本 dict 兜底。
_PROFILES = {
    "cc": {
        "name": "Claude Code", "icon": "C",
        "intro": "Anthropic 官方 agent harness,擅长从题面锁定约束、深度检索+交叉核对真名/细节。",
        "strengths": ["最准", "深度检索", "严格约束核对"],
        "style": "先解析约束再多源核对·质量优先",
        "best_for": "要求准确、需要交叉核对的事实题·代码任务",
        "speed": "快",
    },
    "codex": {
        "name": "Codex", "icon": "X",
        "intro": "OpenAI 官方 CLI,靠 final schema 保证结构化交付,速度极快,先验直答+单次检索确认。",
        "strengths": ["最快", "100%交付不崩", "结构化"],
        "style": "快速直答·单次确认·不过度搜索",
        "best_for": "要速度的直答题·结构化输出·快速草稿",
        "speed": "快",
    },
    "hermes": {
        "name": "Hermes", "icon": "H",
        "intro": "Nous Research 的工具流 agent,工具链检索+读取角色档案,给出答案与别名。",
        "strengths": ["工具链丰富", "多媒体能力(生图/视频/音频)", "别名/档案检索"],
        "style": "工具流·一步步调工具收集证据",
        "best_for": "需要多种工具协作·多媒体产出的任务",
        "speed": "快",
    },
    "penguin": {
        "name": "Penguin", "icon": "P",
        "intro": "自进化 harness,'agents build agents'——拆解子目标、迭代检索、自我优化。",
        "strengths": ["自进化", "子目标拆解", "工具成本极低"],
        "style": "拆子目标→迭代→验证·越用越强",
        "best_for": "大任务分解·需要自主长出结构的复杂目标",
        "speed": "快",
    },
    "deepseek": {
        "name": "DeepSeek Harness", "icon": "S",
        "intro": "DeepSeek AI 的开源 harness(dsh),插件架构+比例压缩上下文+subagent-fork。",
        "strengths": ["上下文压缩", "插件生态", "subagent 并行"],
        "style": "分段上下文+增量压缩·稳扎稳打",
        "best_for": "长上下文任务·需要压缩管理的大检索",
        "speed": "慢",
    },
    "prime": {
        "name": "Prime", "icon": "R",
        "intro": "自进化 harness,IPython kernel 内核,规划-执行-复核链,可跑代码验证。",
        "strengths": ["代码执行验证", "规划-执行链", "自进化"],
        "style": "规划→执行(可跑代码)→复核",
        "best_for": "需要跑代码验证的计算/数据任务",
        "speed": "快",
    },
    "jcode": {
        "name": "JCode", "icon": "J",
        "intro": "轻量代码 agent,快速代码生成与执行。",
        "strengths": ["轻量", "代码生成"],
        "style": "快速代码优先",
        "best_for": "轻量代码任务",
        "speed": "快",
    },
    "deepagents": {
        "name": "DeepAgents", "icon": "D",
        "intro": "ReAct 范式 agent,搜索+多轮推理收敛。",
        "strengths": ["ReAct", "快且能用"],
        "style": "搜索→ReAct 推理→收敛",
        "best_for": "标准检索推理题",
        "speed": "中",
    },
    # ── 专用后端 + 编排放大器(2026-08-28 补 profile·让总指挥规划能选它们·此前缺profile=编排看不见) ──
    "omnigent": {
        "intro": "编排放大器(meta-harness)·一行调 cursor/kimi/goose/qwen 等18子家。",
        "strengths": ["纳入18子家", "多层嵌套", "军团没有的家经它开"],
        "style": "军团派它→它再调子家(openai-agents经网关bridge已验证真出)",
        "best_for": "需要军团现有家之外的能力·或想用cursor/goose/qwen等特定家",
        "speed": "中",
    },
    # ── 云端桌面(2026-08-30 用户澄清:跟其他端 agent 一样一键使用军团·包 cua 作运行时) ──
    "cloud-desktop": {
        "intro": "云端桌面·把 cua 桌面 SDK 包成'一个端'(装了本机 cua+Docker 就能派活)。",
        "strengths": ["官方 CUA sandbox", "浏览器/GUI 自动化", "按官方状态提供可视会话"],
        "style": "军团通过 CUA MCP 自行发现工具；只有控制端与官方 sandbox 均真实就绪才执行 GUI。",
        "best_for": "需要云端跑桌面软件/浏览器自动化/GUI 操作的任务",
        "needs_install": "cua",  # 前端识别:走 /cloud_desktop/status 决定 ready
    },
    # ── 以下4项是【能力后端】不是harness(2026-08-28用户指出·标kind让get_profiles默认不当军团成员列) ──
    "cua": {
        "kind": "capability",
        "intro": "Computer Use·真截屏+GUI自动化(装了本机运行时·实测真截1920×1248 PNG)。",
        "strengths": ["截屏", "桌面GUI操作", "computer-use MCP"],
        "style": "MCP桌面工具·captain自主调(不擅自点击)",
        "best_for": "需要看屏幕/操作桌面GUI/截图分析的任务",
    },
    "supervision": {
        "kind": "capability",
        "intro": "CV视觉分析(roboflow supervision·检测框/追踪/几何原语·实测真处理图)。",
        "strengths": ["目标检测处理", "ByteTrack追踪", "PolygonZone区域"],
        "style": "对图像做CV原语处理(常与cua截屏串联)",
        "best_for": "图像里的目标检测/框处理/视觉几何分析",
    },
    "herdr": {
        "kind": "capability",
        "intro": "常驻终端runtime(Rust·实测真起workspace+pane+跑命令rc=0)。",
        "strengths": ["常驻终端", "多路复用", "断线可续"],
        "style": "起终端工作台·pane里真执行命令",
        "best_for": "需要常驻/后台跑命令、多终端会话管理的任务",
    },
    "orca": {
        "kind": "capability",
        "intro": "并行agent编排CLI(Electron·实测build通·agent-context出232命令schema)。",
        "strengths": ["并行agent舰队", "worktree隔离", "232编排命令"],
        "style": "多agent各自worktree并行·统一编排",
        "best_for": "需要多个agent并行各干一摊、worktree隔离的大工程",
    },
}
# manifest 的 profile 覆盖同名家(单一事实源·改画像改 manifest·这里只兜底)。
for _k, _p in _MANIFEST_PROFILES.items():
    _PROFILES[_k] = {**_PROFILES.get(_k, {}), **_p}

# 名称映射(summary 里的 name → key)·auto已彻底剔除军团(2026-09-08用户钉死)
_NAME2KEY = {
    "Codex": "codex", "DeepSeek Harness": "deepseek", "Hermes": "hermes",
    "Claude Code full-agent": "cc", "DeepAgents + SearchFabric": "deepagents",
}


# _load_eval_raw 每次 glob 扫 comparison/results/*.json 全量逐个 json.load·真实测 1.7s+
# (真实 server results 文件更多→更慢)。get_profiles() 每次调都触发·前端"军团各家"面板每次
# 点开白等。评测结果新增极慢(跑完一题才多一个文件),给 90s TTL 缓存→1.7s→~0s。
# 新结果落盘 90s 内自动纳入。同"限时+TTL"模式(见 legion_core list_harnesses)。
_EVAL_TTL = 90.0
_EVAL_CACHE = {"data": None, "ts": 0.0}
_EVAL_LOCK = threading.RLock()


def _load_eval() -> dict:
    with _EVAL_LOCK:
        cached = _EVAL_CACHE["data"]
        if cached is not None and (time.time() - _EVAL_CACHE["ts"] < _EVAL_TTL):
            return cached
    data = _load_eval_raw()
    with _EVAL_LOCK:
        _EVAL_CACHE["data"] = data
        _EVAL_CACHE["ts"] = time.time()
    return data


def _load_eval_raw() -> dict:
    """从 results 目录【全部已保存结果】动态算评价(不再用过时的5题快照)。

    为什么以前只有 n=5:current_results_summary.json 只是某次5题小批测快照;
    实际 results/ 里 cc/codex/hermes/... 各有80题。现在每次读盘扫描最新全量,
    以后新评测文件落进 results 就会自动增长/综合,无需手工更新。
    """
    import glob
    results_dir = os.path.join(_REPO, "harness_baselines", "comparison", "results")
    out = {}
    # 每家每题只计一次;若同题有多个文件,按 run_ts 取最新
    latest = {}
    for path in glob.glob(os.path.join(results_dir, "*.json")):
        try:
            d = json.load(open(path, "r", encoding="utf-8"))
        except Exception:
            continue
        ev = d.get("evaluation") or {}
        key = ev.get("harness") or d.get("harness")
        qid = ev.get("qhash") or (d.get("meta") or {}).get("qhash")
        if not key or not qid or "hit" not in ev:
            continue
        ts = d.get("run_ts") or os.path.getmtime(path)
        lk = (str(key), str(qid))
        if lk not in latest or ts > latest[lk][0]:
            latest[lk] = (ts, ev)
    for (key, _qid), (_ts, ev) in latest.items():
        prev = out.get(key, {"n": 0, "correct": 0, "elapsed_sum": 0.0})
        prev["n"] += 1
        prev["correct"] += int(bool(ev.get("hit")))
        prev["elapsed_sum"] += float(ev.get("elapsed") or 0)
        out[key] = prev

    # 没有 results 时才退回旧 summary(精简部署可能不带 results)
    if not out:
        try:
            d = json.load(open(_SUMMARY, "r", encoding="utf-8"))
        except Exception:
            d = {}
        for s in d.get("summaries", []):
            key = _NAME2KEY.get(s.get("name"))
            if not key:
                continue
            prev = out.get(key, {"n": 0, "correct": 0, "elapsed_sum": 0})
            prev["n"] += s.get("n", 0)
            prev["correct"] += s.get("correct", 0)
            prev["elapsed_sum"] += s.get("elapsed_seconds", 0)
            out[key] = prev

    evals = {}
    for key, v in out.items():
        n, c = v["n"], v["correct"]
        avg = v["elapsed_sum"] / max(n, 1)
        note = f"历史实测 {c}/{n} 命中·均耗时 {avg:.0f}s"
        if n < 10:
            note += "(小样本·仅供参考)"
        else:
            note += f"(覆盖{n}道真实任务·新结果自动纳入)"
        evals[key] = {"hit": c, "n": n, "avg_elapsed": round(avg, 1),
                      "accuracy": round(c / n, 4) if n else 0,
                      "note": note, "source": "results_scan"}
    return evals


def get_profiles(include_capabilities: bool = False) -> list:
    """harness 家的介绍+评价(合并静态介绍 + 动态历史效果)。

    默认只返回真 harness(领题解题的 agent)。能力后端(kind=capability:cua/supervision/
    herdr/orca)不是军团成员·默认排除·别在军团面板当成员列(2026-08-28 定位纠正)。
    需要连能力后端一起拿(如安装中心/工具箱视图)传 include_capabilities=True。
    """
    evals = _load_eval()
    out = []
    seen = set()
    profiles = dict(_PROFILES)
    profiles.update(_MANIFEST_PROFILES)
    for key, p in profiles.items():
        if not include_capabilities and p.get("kind") == "capability":
            continue
        prof = {"key": key, **p}
        ev = evals.get(key)
        prof["eval"] = ev or {"note": "暂无历史实测样本(跑过后自动更新)", "n": 0}
        out.append(prof); seen.add(key)
    # ★彻底落实新家(用户 2026-09-01):在池能派单但 _PROFILES 没填画像的家(如外部探活入池的
    #   pi)——旧代码只遍历 _PROFILES→前端"军团各家"面板看不到它·虽然能跑=没彻底落实。补:把
    #   当前入池的家用兜底画像补进列表·让"能跑的家前端都认"。用缓存探活结果·不引入前端开销(铁律0)。
    try:
        import sys as _s3
        _mo = os.path.join(_REPO, "harness_baselines", "meyo-orchestrator")
        if _mo not in _s3.path:
            _s3.path.insert(0, _mo)
        import legion_core as _lc
        _runnable = set((_lc._aigc_ready(cached_only=True) or {}).get("runnable_clis") or [])
        for key in sorted(_runnable):
            if key in seen:
                continue
            prof = _default_profile(key)
            prof["eval"] = evals.get(key) or {"note": "暂无历史实测样本(跑过后自动更新)", "n": 0}
            out.append(prof); seen.add(key)
    except Exception:
        pass   # 探活拿不到→只列 _PROFILES(退回旧行为·不崩)
    return out


def get_capability_profiles() -> list:
    """只返回能力后端(cua/supervision/herdr/orca)·供工具箱/能力视图展示·非军团成员。"""
    return [p for p in get_profiles(include_capabilities=True)
            if p.get("kind") == "capability"]


def _default_profile(key: str) -> dict:
    """新 harness 只注册了 adapter、_PROFILES 没填画像时的兜底(彻底落实新家·用户 2026-09-01)。

    根因:旧 get_profile 对没填 _PROFILES 的家返回空 dict{}·前端拿不到 name/icon/intro→
    展示缺失/可能崩。这是"新 harness 要手改 5 处·漏一处降级"的具体一处。改为:漏填自动兜底·
    加一家就算只写 adapter 也能正常展示(名字用 key·图标取首字母大写·其余给通用文案)·
    不再静默缺字段。填了 _PROFILES 的家优先用它·这只是缺失时的安全网。"""
    return {
        "key": key,
        "name": key.replace("-", " ").replace("_", " ").title(),
        "icon": (key[:1] or "?").upper(),
        "intro": f"{key} · 军团成员(画像未填·仅注册 adapter)。",
        "strengths": [], "style": "", "best_for": "",
        "speed": "中", "_profile_fallback": True,
    }


def get_profile(key: str) -> dict:
    for p in get_profiles(include_capabilities=True):
        if p["key"] == key:
            return p
    # 没填 _PROFILES 的新家:兜底画像 + 动态 eval(和 get_profiles 一致)
    prof = _default_profile(key)
    prof["eval"] = _load_eval().get(key) or {"note": "暂无历史实测样本(跑过后自动更新)", "n": 0}
    return prof


if __name__ == "__main__":
    for p in get_profiles():
        print(f"【{p['name']}】{p['intro']}")
        print(f"  特点: {', '.join(p['strengths'])} · {p['style']}")
        print(f"  适合: {p['best_for']}")
        print(f"  评价: {p['eval']['note']}")
        print()
