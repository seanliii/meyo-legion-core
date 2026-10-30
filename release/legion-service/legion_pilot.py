"""军团“持续运行直到完成”能力。

共享两样东西:
1. pilot_kernel.classify —— 和 Claude Code hook 同一套续航判断;
2. 实例明确采用的 pilot-goals.md（旧仓布局兼容 dev/）—— 目标/铁律/红线。

每个用户只覆盖自己的开关和强度,不复制整份纲领,避免各家军团越跑越分叉。
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from pilot_kernel import DEFAULT_CONFIG, classify, merge_config

HERE = Path(__file__).resolve().parent


def _find_goals() -> Path:
    """定位全军团共享纲领:env → 仓库内 dev/ → dist 自包含同目录。"""
    env = os.environ.get("LEGION_PILOT_GOALS")
    if env:
        return Path(env)
    for cand in (HERE.parent.parent / "dev" / "pilot-goals.md",  # 仓库布局
                 HERE / "pilot-goals.md"):                        # dist 自包含
        if cand.is_file():
            return cand
    return HERE / "pilot-goals.md"  # 兜底(读不到时 read_shared_goals 有安全默认)


# Legacy import compatibility only. Live reads/settings resolve the path again.
DEFAULT_GOALS = _find_goals()
DEFAULT_USER_DIR = Path(os.environ.get(
    "LEGION_USER_CONFIG_DIR",
    Path.home() / ".meyo" / "legion" / "users",
))


def read_shared_goals(path: Path | None = None) -> dict[str, Any]:
    """读全军团共享 md 的 MANUAL 区。文件可直接手改,保存即生效。"""
    return _shared_goals_snapshot(path)[0]


def _shared_goals_snapshot(path: Path | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read content and its observation together; an invalid direction is not a stop decision."""
    path = Path(path) if path is not None else _find_goals()
    empty = {"prime": "完成用户目标", "owner_proxy": [], "laws": [],
             "phases": [], "redlines": []}
    observation = {"path": str(path), "status": "not_configured",
                   "source": "standing_user_direction", "is_user_message": False}
    try:
        if os.environ.get("MEYO_PERSONAL_STATE_DIR"):
            # Use the same file and MANUAL contract as the instance launcher.
            # Import in this direction only: provisioning must not import Pilot
            # before the instance HOME has been selected.
            from personal_runtime import _manual_goals, _private
            text = _manual_goals(_private(path)).decode("utf-8")
        else:
            # Legacy explicit/shared documents can be readable (0644). Keep
            # their API without applying the private-instance permission rule.
            if path.is_symlink() or path.parent.is_symlink():
                raise OSError("invalid direction path")
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 32768:
                    raise OSError("invalid direction file")
                value = stream.read(32769)
            if len(value) > 32768:
                raise OSError("oversized direction file")
            text = value.decode("utf-8")
    except FileNotFoundError:
        return empty, observation
    except (OSError, ValueError):
        observation.update(status="invalid", reason="goals_file_unreadable_or_invalid")
        return empty, observation
    observation["status"] = "configured"
    match = re.search(r"\[MANUAL\](.*?)\[/MANUAL\]", text, re.S)
    body = match.group(1) if match else text

    def items(label: str) -> list[str]:
        found = re.search(label + r"[^\n]*\n((?:\s*[-*]\s*.+\n?)+)", body)
        return ([re.sub(r"^\s*[-*]\s*", "", line).split("#")[0].strip()
                 for line in found.group(1).splitlines() if line.strip()] if found else [])

    prime = re.search(r"头号纲领[:：]\s*(.+)", body)
    return {
        "prime": prime.group(1).strip() if prime else "完成用户目标",
        "owner_proxy": items(r"Owner代理"),
        "laws": items(r"铁律"),
        "phases": items(r"当前阶段"),
        "redlines": items(r"红线"),
    }, observation


def _tokens(text: str) -> set[str]:
    latin = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", str(text or "").lower())
    han_text = "".join(re.findall(r"[一-鿿]", str(text or "")))
    han = [han_text[index:index + size] for size in (2, 3, 4)
           for index in range(max(0, len(han_text) - size + 1))]
    return set(latin + han)


def relevant_owner_proxy(goals: dict[str, Any], goal: str = "") -> str:
    """选择一条最相关的长期 owner 指引；它是带来源 Context，不是用户新消息。"""
    rows = [str(item).strip() for item in goals.get("owner_proxy") or [] if str(item).strip()]
    if not rows:
        return ""
    query = _tokens(goal)
    ranked = sorted(enumerate(rows), key=lambda pair: (-len(query & _tokens(pair[1])), pair[0]))
    return ranked[0][1][:500]


def user_config_path(user_id: str, root: Path | None = None) -> Path:
    root = root or DEFAULT_USER_DIR  # 运行时取,支持测试 monkeypatch
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", user_id or "default")[:80]
    return root / safe / "pilot.json"


def load_user_config(user_id: str, root: Path | None = None) -> dict[str, Any]:
    """每用户覆盖;没配就用共享默认。"""
    try:
        return merge_config(json.loads(user_config_path(user_id, root).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return merge_config()


def save_user_config(user_id: str, config: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    """保存用户设置。只存白名单字段,陌生字段不进盘。"""
    clean = merge_config(config)
    path = user_config_path(user_id, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    return clean


def evaluate(reply: str, run_state: dict[str, Any], user_id: str = "default",
             config: dict[str, Any] | None = None, root: Path | None = None) -> dict[str, Any]:
    """军团侧统一入口:按钮开着时,每个 agent 一轮结束都调它决定继续还是停。"""
    cfg = merge_config(config) if config is not None else load_user_config(user_id, root)
    verdict = classify(reply, run_state, cfg)
    shared = read_shared_goals()
    proxy = relevant_owner_proxy(
        shared, str(run_state.get("goal") or run_state.get("question") or reply))
    verdict["shared_goals"] = shared
    if proxy:
        verdict["owner_proxy"] = {
            "source": "standing_user_direction", "is_user_message": False, "text": proxy}
        if verdict.get("action") == "continue":
            next_action = str(verdict.get("next_action") or "继续推进当前真实用户目标")
            verdict["next_action"] = (
                f"{next_action}；owner_proxy（已确认长期方向，非本轮用户新发言）：{proxy}")
    return verdict


def settings_for_humans(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """给前端按钮/设置面板的说人话元数据。"""
    cfg = merge_config(config)
    shared, observation = _shared_goals_snapshot()
    return {
        "enabled": cfg["enabled"],
        "title": "持续运行直到完成",
        "description": "打开后,军团会盯着目标继续干。只查了一半、光说不做、把小事甩给你决定时,会自己接着做。",
        "intensity": cfg["intensity"],
        "intensity_options": [
            {"value": "focused", "label": "盯紧点(推荐)", "help": "目标没完成就继续,除非真卡住或确实需要你决定。"},
            {"value": "gentle", "label": "少打扰", "help": "只拦明显偷懒,普通停顿不追。"},
        ],
        # 续航范围(goal-C·按 turn/query/session 自选·范围越大对"完成"越严·防单轮假完成骗停)
        "scope": cfg.get("scope", "query"),
        "scope_options": [
            {"value": "turn", "label": "盯这一轮", "help": "每轮说没完就继续·最省心但可能这一单没真拿到东西就停。"},
            {"value": "query", "label": "盯这一单(推荐)", "help": "盯到这一单目标真达成才停·单轮说完但没真交付会继续。"},
            {"value": "session", "label": "盯整会话", "help": "盯到整个会话大目标达成才停·跨多单最持久。"},
        ],
        "catch_options": [
            {"key": "ask_instead_of_act", "label": "别拿小事问我", "help": "能自己决定的先做,做完给结果。"},
            {"key": "investigation_only", "label": "别只查一半", "help": "查到上下游和根因,别看一个文件就停。"},
            {"key": "promise_without_action", "label": "别光说要做", "help": "说了下一步就立刻执行。"},
            {"key": "decision_without_test", "label": "先自测再让我决定", "help": "先给真实测试或AB数据,再把必须由我决定的事交给我。"},
        ],
        "shared_goals_path": observation["path"],
        "shared_goals_observation": observation,
        "shared_goals_help": "本实例采用的长期方向。保存后下一轮读取；不是新的用户指令或额外授权。",
        "owner_proxy": shared.get("owner_proxy") or [],
        "owner_proxy_help": "用户已确认的长期方向，只作为带来源的纠偏 Context，不伪装成本轮用户发言。",
    }
