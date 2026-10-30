# -*- coding: utf-8 -*-
"""multimodal_trigger · 检索链自动触发多模态(烧进引擎·文本检索↔视觉融合)

用户 /goal: 检索链不该只处理文本。搜到网页含【关联图片/视频】时·agent 该像人一样
"发现关联的画就切图看图·发现视频就抽帧看"——把视觉信息也并进证据参与联想收敛。

现状(真数据): 多模态工具齐全且 native agent 能调(see_image_vlm/see_image_zoom/
see_video_frame/read_image_ocr)·但【我的五层文本检索机制没和它们融合】——搜到含图
网页不会自动看图。本模块补这条融合:检测"该看视觉"信号→强提示 agent 用视觉工具。

触发信号(同时): ①题面需要【视觉细节】(图表/照片/画面/logo/地图/截图等)或搜索
撞墙(文本搜不到);②搜到的内容里【有图片/视频 URL】(可看的素材)。命中→注入强提示
让 agent 调 see_image_zoom(切图看)/see_video_frame(抽帧)。

铁律: 只【提示+给出可看的URL】·不自己硬调视觉工具(看图/抽帧贵·agent 按需调)·
但提示是【强触发】(明确给 URL + 该看什么)。fail-safe·env MEYO_MULTIMODAL_TRIGGER=0 关。
"""
import os
import re

# 题面需要视觉的信号词(通用·非题目特判)
_VISUAL_NEED = (
    "image", "photo", "picture", "figure", "chart", "graph", "diagram", "map",
    "logo", "screenshot", "painting", "drawing", "video", "footage", "frame",
    "shown in", "depicted", "pictured", "visual", "看图", "图片", "照片", "图表",
    "画面", "视频", "截图", "地图", "标志", "画", "如图",
)
# 图片/视频 URL 模式
_IMG_URL = re.compile(
    r"https?://[^\s\"'<>]+?\.(?:jpg|jpeg|png|gif|webp|svg|bmp)", re.I)
_VIDEO_URL = re.compile(
    r"https?://(?:[^\s\"'<>]*?(?:youtube\.com/watch|youtu\.be/|vimeo\.com/|"
    r"[^\s\"'<>]+?\.(?:mp4|webm|mov)))[^\s\"'<>]*", re.I)


def _enabled():
    return os.environ.get("MEYO_MULTIMODAL_TRIGGER", "1") != "0"


def needs_visual(question):
    """题面是否需要视觉细节。fail-safe → False。"""
    try:
        ql = str(question or "").lower()
        return any(w in ql for w in _VISUAL_NEED)
    except Exception:
        return False


def extract_media_urls(text, *, max_n=3):
    """从搜到的内容抽图片/视频 URL(可看的素材)。返回 {"images":[...],"videos":[...]}。"""
    out = {"images": [], "videos": []}
    try:
        t = str(text or "")
        out["videos"] = list(dict.fromkeys(_VIDEO_URL.findall(t)))[:max_n]
        # 图片:过滤掉明显的图标/像素点/占位(尺寸小的常是 UI 图标)
        imgs = []
        for u in _IMG_URL.findall(t):
            if not any(x in u.lower() for x in ("icon", "logo-", "sprite", "1x1",
                                                "pixel", "avatar", "thumb/1", "favicon")):
                imgs.append(u)
        out["images"] = list(dict.fromkeys(imgs))[:max_n]
        return out
    except Exception:
        return out


def maybe_trigger(question, recent_text, *, stalled=False):
    """判断是否该看视觉·给强提示。
    触发: (题面需视觉 或 文本撞墙) 且 搜到的内容有图/视频 URL。
    返回注入串(含具体 URL + 该看什么)或 ''。fail-safe → ''。"""
    try:
        if not _enabled():
            return ""
        _need = needs_visual(question) or stalled
        if not _need:
            return ""
        media = extract_media_urls(recent_text)
        if not (media["images"] or media["videos"]):
            return ""
        parts = ["[多模态融合·该看视觉了] 检索到的内容里有【可看的视觉素材】·"
                 "别只读文字——像人一样把关联的画/视频看了(视觉信息常含文字搜不到的关键细节):"]
        if media["images"]:
            parts.append("  · 图片(用 see_image_zoom 切图放大看关键区域·或 see_image_vlm "
                         "整体看·或 read_image_ocr 提图里文字): %s"
                         % "  ".join(media["images"][:2]))
        if media["videos"]:
            parts.append("  · 视频(用 see_video_frame 抽关键帧看·可指定时间点): %s"
                         % "  ".join(media["videos"][:2]))
        parts.append("看到的视觉信息也算证据·并进你的推理链再判断。")
        return "\n".join(parts)
    except Exception:
        return ""
