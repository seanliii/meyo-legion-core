"""作品标准：用户要的是给人玩、给人看的互动作品时，军团写明“做到什么才算成品”。

没有这份标准时，成员只收到“完整可用”的通用标尺，独立评审也只核对文件是否存在，
能运行的低完成度作品就会被当成交付。这里只描述成品应达到的效果和必须留下的证据，
不规定具体实现，也不替成员选技术。LEGION_PRODUCT_BAR=0 可关闭。
"""
from __future__ import annotations

import os
import re

_MAKE = (r"(?:做|制作|开发|写|生成|创作|实现|搭建|打造|重做|续作|继续|完善|升级"
         r"|make|build|create|develop|remake)")
_THING = (r"(?:小游戏|游戏|可玩|3d|三维|开放世界|互动作品|互动体验|互动故事|交互作品|交互体验"
          r"|\bgames?\b)")
_REQUEST = re.compile(_MAKE + r"[^。！？!?\n]{0,40}?" + _THING, re.I)
# “写一份游戏行业分析报告”要的是文字，不是作品：看紧跟在作品词后面的那一小段。
_ABOUT = re.compile(r"报告|分析|调研|研究|文档|方案|策划|介绍|解释|总结|评测|攻略|新闻|论文|文章|教程|讲解")
_GOAL_FIELDS = ("user_goal", "goal", "root_goal", "original_goal", "question")

CRITERIA = (
    "对标：动手前写下同类顶尖作品在画面、声音、手感上的 3–5 条具体做法作为本作目标；"
    "用户给了参照作品时以参照为准，交付时逐条对照说明差距。",
    "先成品后铺开：先把一小块（主角、一类对手或关键物件、一小片环境）做到最终画质和手感并截图自查，"
    "再扩展内容；不接受铺满内容但处处是半成品。",
    "画面：主视觉不能是未加工的基础几何体或纯色块拼装。角色与关键物件有可辨认的造型细节，"
    "表面有贴图或法线并区分材质；有阴影、环境光、大气或雾、色调映射，并有至少两种后期处理"
    "（如泛光、环境光遮蔽、体积光、景深、调色）。二维作品对应为成体系的造型、配色、动效与光感。",
    "声音：有配乐和关键动作音效，并随游戏状态变化。",
    "玩法与手感：至少一条从目标、操作、反馈到胜负、重开的完整流程；移动有加减速，镜头跟随，"
    "命中与受伤有明确反馈。",
    "自带素材：模型、贴图、音频由代码生成或随成品自带，离线打开即可运行，真实浏览器无报错。",
    "证据：交付说明附真实浏览器里的标题、游玩中、关键事件截图，并对以上各条写明达到或未达到及依据；"
    "只写“已验证”不算。",
    "评审：评审者自己在真实浏览器打开成品、操作并截图看图后逐条判定，作者自述不作为依据；"
    "任何一条未达到即判需修改，并写明具体改什么。",
)


def _texts(question: str, plan: dict | None) -> list[str]:
    texts = [str(question or "")]
    context = plan.get("project_context") if isinstance(plan, dict) else None
    if isinstance(context, dict):
        texts.extend(str(context[name]) for name in _GOAL_FIELDS
                     if isinstance(context.get(name), str) and context[name].strip())
    return texts


def is_product_request(question: str, plan: dict | None = None) -> bool:
    """请求是否要一件可玩、可看的互动作品（续作时也看原项目目标）。"""
    if os.environ.get("LEGION_PRODUCT_BAR", "1").strip() == "0":
        return False
    for text in _texts(question, plan):
        for match in _REQUEST.finditer(text[:4000]):
            following = re.split(r"[，。,；;：:\n！？!?]", text[match.end():match.end() + 12])[0]
            if not _ABOUT.search(following):
                return True
    return False


def is_default(acceptance) -> bool:
    return isinstance(acceptance, (list, tuple)) and list(acceptance) == list(CRITERIA)


def brief() -> str:
    """写进每个成员任务说明里的全文；不截断。"""
    lines = ["- 作品标准（这是要给人玩、给人看的作品，不是代码作业。按此制作并自检；"
             "独立评审会打开成品逐条核对，未达到会退回修改）："]
    lines.extend(f"  {index}. {item}" for index, item in enumerate(CRITERIA, 1))
    return "\n".join(lines)


def apply(question: str, plan: dict, *, reviewers_available: bool = False) -> bool:
    """给作品类请求补上成品标准；有不参与创作的成员可用时，收口前按标准评审实物。

    用户或规划器已经给出的验收条件、审计设置保持原样。
    """
    if not isinstance(plan, dict) or not is_product_request(question, plan):
        return False
    if not plan.get("acceptance"):
        plan["acceptance"] = list(CRITERIA)
    if reviewers_available and not isinstance(plan.get("success_audit"), dict):
        plan["success_audit"] = {"enabled": True}
    return True
