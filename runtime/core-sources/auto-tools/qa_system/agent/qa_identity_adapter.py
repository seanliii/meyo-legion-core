#!/usr/bin/env python3
"""v43 · 身份适配 · 用户画像 → 跨域嫁接
用户 2026-07-24 澄清「身份适配可能干扰评估链路 · 先接口留白 · 或读虾社区画像做提示」

设计:
  - **默认关** (不干扰评估) · CLI --identity-brief 显式开
  - 开时: 读 memory MEMORY.md 提取用户身份标签 (虾社区/agent 系统/PM)
  - 生成「跨域嫁接段」放思考过程尾 · opus 可选择用不用
  - 环境变量 MEYO_QA_IDENTITY=1 也可开
"""
import os
import re
from pathlib import Path

MEMORY_INDEX = Path(os.path.expanduser(
    "~/.claude/projects/-Users-xuan-Desktop-cursor-----/memory/MEMORY.md"
))


def enabled():
    """判断是否开启"""
    return os.environ.get("MEYO_QA_IDENTITY", "0") == "1"


def load_identity_brief():
    """读 memory 提取用户身份 · 返 dict"""
    if not MEMORY_INDEX.exists():
        return {}
    try:
        content = MEMORY_INDEX.read_text(encoding="utf-8")[:8000]
    except Exception:
        return {}

    brief = {
        "roles": [],
        "products": [],
        "expertise": [],
    }
    # 提取虾社区/觅游/agent 关键词
    if "虾社区" in content or "shrimp" in content.lower():
        brief["products"].append("虾社区 (Discord 类订阅社区)")
    if "觅游" in content or "meyo" in content.lower():
        brief["products"].append("觅游 (meyo)")
    if "agent" in content.lower():
        brief["expertise"].append("多 agent 系统架构")
    if "PM" in content or "产品经理" in content:
        brief["roles"].append("产品经理")

    # 提取 shrimp-v13.xx 系列 · 说明用户在做 agent 系统迭代
    if re.search(r'shrimp-v13\.\d+', content):
        brief["expertise"].append("agent 系统持续迭代 (13.xx)")

    return brief


def render_identity_hint_for_opus(brief):
    """给 opus prompt 用的身份提示段 · 用户身份数据 → 嫁接指令
    返: str · 拼进 step5 prompt · 空返 ""
    """
    if not brief or not (brief.get("products") or brief.get("expertise")):
        return ""

    lines = ["## v43 · 身份适配 (用户身份 → 跨域嫁接段)"]
    if brief.get("products"):
        lines.append(f"用户产品: {'、'.join(brief['products'])}")
    if brief.get("expertise"):
        lines.append(f"用户领域: {'、'.join(brief['expertise'])}")
    lines.append("")
    lines.append("**思考过程结尾**追加 1 段「跨域嫁接」·30-80 字·格式:")
    lines.append("> 如果你要把这套 [方法论/工具] 变成 [用户的产品] 的一个 agent · 拆成 X/Y/Z 三个子 agent: [具体拆分]")
    lines.append("")
    lines.append("**注意**: 只在方法论/系统设计题嫁接 · 概念题跳过 · 强融会显得刻意")
    return "\n".join(lines)


if __name__ == "__main__":
    b = load_identity_brief()
    print(f"[enabled] {enabled()}")
    print(f"[brief]   {b}")
    print("[hint]")
    print(render_identity_hint_for_opus(b))
