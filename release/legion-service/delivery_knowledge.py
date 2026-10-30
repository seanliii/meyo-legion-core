#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""军团世界级交付运营知识库：把每次交付学到的配方变成下次交付的起跑线。

解决的根本问题(2026-09-08用户钉死'不想B站变成特殊例子'):
模型/harness/军团是无状态的——每次交付学到的配方(B站标题公式/跑酷要锚bruno-simon)
随 session 结束死亡,下次从零开始,质量永远靠当次发挥。
本模块让知识三步自动闭环:
  ① capture: world_class 交付收尾时,从 packet 收割观察(锚点表/失败/gap/参照),
     按品类入持久库,标为待复核;
  ② match: 新订单先获得历史条目索引,不凭关键词判定适用;
  ③ inject: 本单显式选中的历史条目和近期收割进入上下文,完整记录仍可按需读取。

设计律:
  - context not control: 注入的是"历史观察+来源+各自审核状态",怎么用/要不要更新由成员自主;
  - 知识分级: seed(用户实锤验证) > capture(机器收割待复核),注入时如实标注;
  - 单一事实源: 每用户/品类一个 JSON,原子写,追加 provenance;公开种子独立;
  - fail-open: 库坏了/没命中绝不阻塞派活。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

import legion_paths as _paths

_LOCK = threading.RLock()
_MAX_ITEMS = 40


def _kb_dir() -> Path:
    return Path(os.environ.get("LEGION_DELIVERY_KB_DIR") or _paths.under("knowledge"))


def _owner_dir(user_id: str) -> Path:
    # Do not sanitize two different identities into the same storage namespace.
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    return _kb_dir() / "owners" / digest


def _path(category: str, user_id: str = "") -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(category or "misc"))[:60] or "misc"
    if user_id:
        category_hash = hashlib.sha256(str(category).encode("utf-8")).hexdigest()[:16]
        return _owner_dir(user_id) / f"{safe}-{category_hash}.json"
    # Read-only legacy location. Unknown ownership is never inferred from a
    # category, a packet, the default account, or an asserted review status.
    return _kb_dir() / f"{safe}.json"


# ── 种子知识(用户实锤验证的最高等级·2026-09-08 两场血泪)──────────────────────
# 这两条是从今天用户三次打脸+真浏览器逐帧测量+真AI重做对比中提炼的,
# status=user_verified 只有用户明确确认过的才能标。
_SEED_ENTRIES: dict[str, dict] = {
    "video_bilibili_oral": {
        "category": "video_bilibili_oral",
        "label": "B站口播/短视频成品",
        "triggers": ["B站", "bilibili", "口播", "投稿", "短视频", "哔哩哔哩", "vlog", "宣传片"],
        "crown_references": [
            {"name": "中国国家地理《终南山怪虫》566万播放",
             "url": "https://www.bilibili.com/video/BV1yuto66E8J",
             "measured": "第2秒画面78%有内容/17%彩色/三区铺满,竖屏,835秒"},
            {"name": "B站分区排行榜(总/知识/科技)",
             "url": "https://www.bilibili.com/v/popular/rank/all",
             "measured": "标题公式样本:悬念反转/冲突/具体数字+爆肝/全实拍实测"},
        ],
        "formula_anchors": [
            {"dimension": "hook_title", "is_crown": True,
             "concept": "标题=悬念反转('只有我们拍到了真相')/冲突('减负反变增负')/具体数字+爆肝"
                        "('爆肝2万字·22套·全实拍实测');零平淡描述,零内部视角文案"},
            {"dimension": "opening_3s", "is_crown": True,
             "concept": "第2秒画面≥78%有内容(真产品运行/实拍B-roll);第一句口播是钩子不是铺垫;"
                        "禁黑屏字幕卡开场"},
            {"dimension": "visual_density", "is_crown": False,
             "concept": "全程画面密度>70%、彩色>10%、每3-5秒画面变化;字幕花字样式贴画面"},
            {"dimension": "real_material", "is_crown": True,
             "concept": "'全实拍实测'写进标题就能火;产品口播必须展示产品真实运行画面占比>60%"},
            {"dimension": "duration_pacing", "is_crown": False,
             "concept": "时长服从内容密度(产品演示60-180s);每15-20秒一个信息爆点"},
        ],
        "verified_anti_patterns": [
            "静态图 Ken Burns 运镜冒充视频(用户判'还是PPT')",
            "黑屏字幕卡开场(实测98%纯黑被用户判死)",
            "灰白UI截图当B-roll(0%彩色)",
            "TTS平铺念产品说明书(无钩子无情绪)",
            "把'B站规格'(1080p/时长/封面尺寸)当目标而不是'能火'当目标",
        ],
        "tool_paths": [
            "gen_video=真AI文生视频(Seedance方舟·约3-5分钟/段·限流自动重试)——做视频优先它",
            "gen_image=文生图(gpt-image-2); trim_media=剪辑; video_frame=抽帧",
        ],
        "status": "user_verified",
        "provenance": "2026-09-08 用户三次实锤(v1黑屏PPT/v2仍PPT/v3真AI视频质变)+分区头部逐帧测量",
    },
    "game_3d_parkour": {
        "category": "game_3d_parkour",
        "label": "3D游戏/跑酷/可玩作品",
        "triggers": ["跑酷", "3D游戏", "parkour", "游戏", "可玩", "runner", "小游戏"],
        "crown_references": [
            {"name": "Bruno Simon《Folio》(Three.js 天花板)",
             "url": "https://bruno-simon.com/",
             "measured": "物理引擎自由驾驶+完整世界观+每帧皆可交互——用户钉死'跟它比',web小游戏天花板不够格"},
            {"name": "Ghostrunner(品类桂冠·机制)", "url": "https://ghostrunner.com/",
             "measured": "跑墙+钩锁高速路线/一击必杀+红色警示——core_loop必须锚它,不许锚三车道躲避"},
        ],
        "formula_anchors": [
            {"dimension": "crown_benchmark", "is_crown": True,
             "concept": "对标必须是该维度公认最强(bruno-simon/Ghostrunner),休闲大众款只可作辅助参照;"
                        "锚错天花板=全链自洽地平庸"},
            {"dimension": "core_loop", "is_crown": True,
             "concept": "核心循环要有物理感与自由度(自由转向/跳跃弧线/速度反馈),"
                        "'三车道自动前进+躲障'是Subway Surfers皮肤不是跑酷"},
            {"dimension": "character_identity", "is_crown": False,
             "concept": "角色有身份有轮廓有动画(骨骼/摆臂/转向侧倾),胶囊球+几何体=最小合规假完成"},
            {"dimension": "render_evidence", "is_crown": True,
             "concept": "'渲染验证通过'必须附画面密度证据——实测多家的游戏截图是黑的(0-5%密度)"
                        "却声称验证通过;WebGL截图需检查非空白像素占比"},
        ],
        "verified_anti_patterns": [
            "胶囊球+立方体拼'角色'(最小合规)",
            "'三车道自动前进'冒充跑酷(锚错天花板)",
            "黑屏截图声称'渲染验证通过'(effect-based done 实锤)",
            "web小游戏天花板当3A参照(用户:'跟3A游戏差不少')",
        ],
        "tool_paths": [],
        "status": "user_verified",
        "provenance": "2026-09-08 赛博跑酷竞技场10家对照+用户两次实锤('跟天花板比bruno-simon''跟3A差不少')",
    },
}


def _entry_from_seed(seed: dict) -> dict:
    entry = copy.deepcopy(seed)
    entry.setdefault("captures", [])
    entry.setdefault("updated_at", time.time())
    return entry


def _read_entry(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) and value.get("category") else None
    except (OSError, ValueError):
        return None


def load_entry(category: str, user_id: str = "") -> dict | None:
    """只读公开种子和当前绑定用户的观察；未知 owner 的旧收割保留在原处。"""
    category, uid = str(category), str(user_id or "").strip()
    seed = _SEED_ENTRIES.get(category)
    public = _entry_from_seed(seed) if seed else None
    if not uid:
        return public
    for path in (_path(category, uid), _path(category)):
        value = _read_entry(path)
        if not value or value.get("category") != category:
            continue
        # A disk entry cannot declare itself public by setting user_verified.
        # Top-level private labels/triggers require the same explicit ownership.
        if value.get("user_id") != uid:
            continue
        entry = {**(public or {}), **value}
        rows = value.get("captures")
        entry["captures"] = [row for row in (rows if isinstance(rows, list) else [])
                             if isinstance(row, dict) and row.get("user_id") == uid]
        entry["user_id"] = uid
        entry["updated_at"] = value.get("updated_at", entry.get("updated_at"))
        return entry
    return public


def load_kb(user_id: str = "") -> dict[str, dict]:
    uid = str(user_id or "").strip()
    kb = {cat: _entry_from_seed(seed) for cat, seed in _SEED_ENTRIES.items()}
    if not uid:
        return kb
    try:
        for directory in (_kb_dir(), _owner_dir(uid)):
            for path in directory.glob("*.json"):
                value = _read_entry(path)
                if value and value.get("user_id") == uid:
                    category = str(value["category"])
                    entry = load_entry(category, user_id=uid)
                    if entry is not None:
                        kb[category] = entry
    except OSError:
        pass
    return kb


def _atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def detect_category(text: str, user_id: str = "") -> str:
    """按触发词为收割选择现有品类,不决定新任务是否注入历史正文。"""
    haystack = str(text or "")
    best, best_hits = "", 0
    for cat, entry in load_kb(user_id=user_id).items():
        hits = sum(1 for kw in (entry.get("triggers") or []) if kw and kw in haystack)
        if hits > best_hits:
            best, best_hits = cat, hits
    return best


def match_context(question: str, plan: dict | None = None, user_id: str = "") -> str:
    """Offer a source index; only an explicit per-task choice expands a recipe.

    Keyword matches cannot establish applicability: a request saying "not just
    gamification" used to receive a platformer recipe as mandatory-looking facts.
    Keep original historical entries intact, available through load_entry, and
    let the member inspect/select them rather than inventing a semantic router.
    """
    try:
        source = plan if isinstance(plan, dict) else {}
        requested = source.get("delivery_knowledge_categories")
        if requested is None and isinstance(source.get("project_context"), dict):
            requested = source["project_context"].get("delivery_knowledge_categories")
        categories = list(dict.fromkeys(
            item for item in (requested if isinstance(requested, list) else [])
            if isinstance(item, str) and item))
        if not categories:
            entries = list(load_kb(user_id=user_id).items())
            if not entries:
                return ""
            rows = [{"category": category, "label": str(entry.get("label") or category),
                     "historical_status": entry.get("status", "unverified")}
                    for category, entry in entries[:8]]
            return (
                "\n\n【历史交付案例索引·本题未选择、未判定适用】\n"
                + json.dumps(rows, ensure_ascii=False)
                + f"\n来源模块：{Path(__file__).resolve()}；"
                f"需要时可读取 load_entry(category, user_id={user_id!r}) 的原始案例和来源。"
                "历史案例不是本题要求，标签或关键词相似不构成适用证据；"
                "是否采用、比较哪一维度及如何创新，由你结合本题与实际研究决定。\n")
        blocks = []
        for cat in categories:
            entry = load_entry(cat, user_id=user_id)
            if entry:
                blocks.append(_selected_entry_context(entry, cat, user_id=user_id))
        return "".join(blocks)
    except Exception:
        return ""


def _selected_entry_context(entry: dict, cat: str, user_id: str = "") -> str:
    """Render a selected original record, without rewriting its historical claims."""
    try:
        if not isinstance(entry, dict):
            return ""
        rows = []
        for anchor in (entry.get("formula_anchors") or [])[:8]:
            if not isinstance(anchor, dict) or not anchor.get("dimension"):
                continue
            rows.append(f"  - [{anchor.get('dimension')}]{'(桂冠)' if anchor.get('is_crown') else ''}: "
                        f"{anchor.get('concept') or ''}")
        refs = [f"    · {r.get('name')}: {r.get('url')} (实测:{r.get('measured') or ''})"
                for r in (entry.get("crown_references") or [])[:4] if isinstance(r, dict)]
        anti = [f"  - {a}" for a in (entry.get("verified_anti_patterns") or [])[:6]]
        tools = "; ".join(str(t) for t in (entry.get("tool_paths") or [])[:4])
        level = "用户实锤验证（原记录范围）" if entry.get("status") == "user_verified" else "机器收割待复核"
        block = (f"\n\n【本单显式选择的历史案例·{entry.get('label') or cat}·历史线索({level})】\n"
                 "以下是过去交付的观察，不是本题必须沿用的实现，也不代表本题质量已经验证:\n")
        if rows:
            block += "配方锚点:\n" + "\n".join(rows) + "\n"
        if refs:
            block += "桂冠参照:\n" + "\n".join(refs) + "\n"
        if anti:
            block += "历史失败事实（是否同因需对照当前现场）:\n" + "\n".join(anti) + "\n"
        if tools:
            block += f"记录中的工具路径（当前可用性需实查）: {tools}\n"
        block += ("可把这些线索与当前失败、真实来源及新发现比较，自主选择复用、扩展或替换底座；"
                  "保留原目标、会话和有效资产，不等于锁定旧实现。是否采用由你判断。\n"
                  f"来源: {entry.get('provenance') or ''}")
        captures = entry.get("captures")
        captures = ([row for row in captures if isinstance(row, dict)]
                    if isinstance(captures, list) else [])
        if captures:
            # Preserve each capture's sources and review state, not the seed's
            # status. This is a recent preview; load_entry keeps the full record.
            recent = [{**row, "status": row.get("status") or "unverified"}
                      for row in captures[-3:]]
            block += (
                f"\n近期交付收割观察（展示最近{len(recent)}条，库内保留{len(captures)}条）:\n"
                "以下记录的审核状态各自独立，不继承上方历史条目的验证等级；"
                "pending_review 表示待复核，unverified 表示未记录审核状态，均不代表本题适用。\n"
                + json.dumps(recent, ensure_ascii=False) + "\n")
        block += (f"\n来源模块：{Path(__file__).resolve()}；"
                  f"load_entry({cat!r}, user_id={user_id!r}) 可读本人可见的完整条目"
                  "（含持久保留的 captures 历史）。\n")
        return block
    except Exception:
        return ""


def capture_from_packet(question: str, plan: dict, packet: dict | None,
                        results: list | None, team_id: str = "", user_id: str = "") -> dict:
    """world_class 交付收尾时收割待复核观察入库(追加 captures,不覆盖种子配方)。

    只收割结构化事实(锚点表/已证伪死路/gap/桂冠参照),不收割散文;
    未验证的收割标 status=pending_review,注入时如实降级展示。
    """
    out = {"ok": False, "category": "", "reason": ""}
    try:
        uid = str(user_id or "").strip()
        if not uid:
            out["reason"] = "owner_required"
            return out
        ambition = str(((plan or {}).get("experience_intent") or {}).get("ambition") or "").lower()
        if ambition not in ("world_class", "frontier") and not (plan or {}).get("frontier_research_contract"):
            out["reason"] = "not_world_class"
            return out
        cat = detect_category(
            f"{question}\n{json.dumps((plan or {}).get('experience_intent') or {}, ensure_ascii=False)}",
            user_id=uid)
        if not cat:
            # 新品类鸡生蛋:无既有条目可命中时,从问题派生新品类(下一个同类交付即可命中)。
            cat = _derive_category(question)
        if not cat:
            out["reason"] = "no_category_match"
            return out
        packet = packet if isinstance(packet, dict) else {}
        results = results if isinstance(results, list) else []
        anchors = [row for row in (packet.get("concept_anchors") or [])
                   if isinstance(row, dict) and row.get("dimension")][:_MAX_ITEMS]
        anti = [row.get("downgrade") for row in (packet.get("effect_downgrades") or [])
                if isinstance(row, dict) and row.get("downgrade")][:_MAX_ITEMS]
        crown_refs = [{"name": row.get("reference") or row.get("dimension"),
                       "url": row.get("evidence_url") or "",
                       "measured": row.get("concept") or ""}
                      for row in anchors if row.get("is_crown")][:6]
        gaps = [f"{row.get('dimension') or row.get('gap') or ''}:{row.get('status') or ''}"
                for row in (packet.get("gap_plan") or []) if isinstance(row, dict)][:_MAX_ITEMS]
        if not anchors and not anti and not crown_refs:
            out["reason"] = "nothing_to_capture"
            return out
        capture = {"user_id": uid, "team_id": str(team_id), "question": str(question)[:300],
                   "anchors": anchors, "anti_patterns": anti,
                   "crown_references": crown_refs, "gap_states": gaps,
                   "status": "pending_review", "captured_at": time.time()}
        with _LOCK:
            entry = load_entry(cat, user_id=uid) or {"category": cat, "label": cat,
                                        "triggers": _derive_triggers(question),
                                        "formula_anchors": [], "crown_references": [],
                                        "verified_anti_patterns": [], "tool_paths": [],
                                        "status": "pending_review", "captures": []}
            captures = [c for c in (entry.get("captures") or []) if isinstance(c, dict)]
            captures.append(capture)
            entry["user_id"] = uid
            entry["captures"] = captures[-_MAX_ITEMS:]
            entry["updated_at"] = time.time()
            _atomic_write(_path(cat, user_id=uid), entry)
        out.update({"ok": True, "category": cat, "anchors": len(anchors),
                    "anti_patterns": len(anti)})
        return out
    except Exception as exc:  # fail-open:知识收割绝不阻塞交付
        out["reason"] = f"{type(exc).__name__}:{exc}"
        return out


def _derive_triggers(question: str) -> list[str]:
    """新品类从问题里抽触发词(粗粒度·过滤停用词防过匹配)。"""
    words = [w for w in re.findall(r"[\u4e00-\u9fa5]{2,6}|[A-Za-z]{3,}", str(question or ""))
             if w.lower() not in _STOP_WORDS]
    return list(dict.fromkeys(words))[:8]


_STOP_WORDS = {"一个", "这个", "那个", "我们", "你们", "他们", "世界级", "顶级", "最好", "最强",
               "帮我", "给我", "做个", "做一个", "做一", "需要", "想要", "可以", "能够",
               "能够", "直接", "打开", "使用", "然后", "并且", "以及", "同时", "自己"}


def _derive_category(question: str) -> str:
    """从问题派生新品类 key:取最长的非停用词(品类主词通常是最具体的名词)。"""
    words = [w for w in re.findall(r"[\u4e00-\u9fa5]{2,6}|[A-Za-z]{3,}", str(question or ""))
             if w.lower() not in _STOP_WORDS]
    if not words:
        return ""
    best = max(words, key=len)
    safe = re.sub(r"[^A-Za-z0-9_\u4e00-\u9fa5]+", "_", best)[:40]
    return f"auto_{safe}"
