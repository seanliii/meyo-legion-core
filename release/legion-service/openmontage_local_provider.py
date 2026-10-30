"""Legion's always-available OpenMontage image-to-video provider.

This provider plugs into OpenMontage's ToolRegistry at runtime. It turns a still
image into a real playable MP4 with a deterministic FFmpeg zoom/pan effect, so
VideoSelector always has an offline image_to_video fallback when cloud providers
are unavailable.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool, Determinism, ExecutionMode, ResourceProfile, ResumeSupport,
    ToolResult, ToolRuntime, ToolStability, ToolTier,
)


class LegionStillMotionVideo(BaseTool):
    name = "legion_still_motion_video"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "local_ffmpeg_motion"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies = ["cmd:ffmpeg"]
    install_instructions = "Install FFmpeg: brew install ffmpeg (macOS) or apt install ffmpeg (Linux)"
    agent_skills = ["ffmpeg", "video-toolkit", "image-to-video"]
    capabilities = ["image_to_video", "reference_to_video"]
    supports = {"image_to_video": True, "offline_fallback": True, "reference_image": True}
    best_for = [
        "reliable offline image-to-video fallback",
        "Ken Burns zoom-and-pan motion from a still image",
        "fast preview video when cloud video providers are unavailable",
    ]
    input_schema = {
        "type": "object",
        "required": ["reference_image_path"],
        "properties": {
            "prompt": {"type": "string"},
            "operation": {"type": "string", "enum": ["image_to_video", "reference_to_video"],
                          "default": "image_to_video"},
            "reference_image_path": {"type": "string"},
            "output_path": {"type": "string"},
            "aspect_ratio": {"type": "string", "enum": ["16:9", "9:16", "1:1"], "default": "16:9"},
            "duration": {"type": "string", "default": "5"},
        },
    }
    resource_profile = ResourceProfile(cpu_cores=2, ram_mb=512, vram_mb=0, disk_mb=500,
                                       network_required=False)
    resume_support = ResumeSupport.FROM_START
    side_effects = ["writes MP4 video file to output_path"]
    user_visible_verification = ["Play the output MP4 and verify visible zoom/pan motion"]

    @staticmethod
    def _size(aspect_ratio: str) -> tuple[int, int]:
        if aspect_ratio == "9:16":
            return 720, 1280
        if aspect_ratio == "1:1":
            return 720, 720
        return 1280, 720

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        start = time.time()
        try:
            self.check_dependencies()
        except Exception as e:
            return ToolResult(success=False, error=str(e))
        src = Path(str(inputs.get("reference_image_path") or "")).expanduser().resolve()
        if not src.is_file():
            return ToolResult(success=False, error=f"Reference image not found: {src}")
        # 上游依据 OpenMontage tools/video/_shared.py:385 —— 参考图先用 PIL 打开+转 RGB。
        # 坏图/非图立即失败,避免 ffmpeg 对无效输入死磕到 timeout(实测坏图会卡满 180s)。
        try:
            from PIL import Image  # OpenMontage 已依赖 Pillow
            with Image.open(src) as _im:
                _im.verify()  # 校验文件结构完整,不解码全图(快)
        except ImportError:
            pass  # 无 Pillow 时退回 ffmpeg 自身超时兜底,不硬失败
        except Exception as e:
            return ToolResult(success=False,
                              error=f"Invalid or corrupt image (cannot decode): {src.name} — {type(e).__name__}")
        try:
            duration = max(1.0, min(float(inputs.get("duration") or 5), 30.0))
        except (TypeError, ValueError):
            duration = 5.0
        width, height = self._size(str(inputs.get("aspect_ratio") or "16:9"))
        out = Path(str(inputs.get("output_path") or src.with_name(f"{src.stem}_motion.mp4"))).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        fps, frames = 30, int(duration * 30)
        vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
              f"crop={width}:{height},"
              f"zoompan=z='min(zoom+0.0015,1.15)':d={frames}:s={width}x{height}:fps={fps},"
              "format=yuv420p")
        cmd = ["ffmpeg", "-y", "-loop", "1", "-i", str(src), "-vf", vf,
               "-t", str(duration), "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
               "-movflags", "+faststart", str(out)]
        try:
            # 90s 兜底:实测 30s 视频仅需 ~1.4s;坏图已被上面 PIL 校验前置拦截,
            # 此处 timeout 只兜"ffmpeg 对合法输入异常卡住"的极端情况,不再需要 180s。
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, error="FFmpeg image-to-video timed out")
        if proc.returncode != 0 or not out.is_file() or out.stat().st_size < 1000:
            return ToolResult(success=False, error=(proc.stderr or "FFmpeg image-to-video failed")[-800:])
        return ToolResult(success=True,
                          data={"operation": "image_to_video", "input": str(src), "output": str(out),
                                "duration": duration, "aspect_ratio": inputs.get("aspect_ratio", "16:9"),
                                "provider": self.provider, "motion": "ken_burns_zoom"},
                          artifacts=[str(out)], duration_seconds=round(time.time()-start, 2))
