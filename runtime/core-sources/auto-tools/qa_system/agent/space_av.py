"""space_av · 2026-08-13 · 音视频空间(时间轴)的 Space 适配器(挂 traversal_core)

音视频是【时间检索】的轴:地址 = 时间戳(区间)。这是用户说的"时间上的检索"。
  · indexer: label_sweep 稀疏扫帧+OCR文字密度 → 返回含文字的候选时间戳(时间轴的索引)
  · reader:  see_video_frame(按时间戳 seek 抽帧 VLM 读) / transcribe(音轨转写)
  · observe 真调 yt-dlp+ffmpeg+VLM(有成本/依赖网络) —— 能动式读取

地址方案:
  · "index"        → 先扫时间轴,拿候选时刻(label_sweep)
  · "t:{seconds}"  → 读该时刻的帧(see_video_frame)
  · "audio"        → 转写音轨(问台词/口述内容用)

遍历价值(挂骨架后):扫时间轴定位含文字时刻(index) → 跳到该时刻精读(t:) → 证据图登记
→ 不够再跳下一候选。补 AV 侧"先扫再精读"的遍历(原来靠 agent 手动两步调)。

诚实边界:视频需真下载(yt-dlp·依赖网络/VPN·本机SSL可能拦YouTube)——下不动时 observe
明确回传原因(引导换渠道),不静默。只做寻址,零新增遍历逻辑。
"""
from __future__ import annotations
import os, sys
import re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from traversal_core import Observation, Action

try:
    from video_frame import see_video_frame as _frame, label_sweep as _sweep
except Exception:
    _frame = _sweep = None


class AVSpace:
    name = "av"
    kind = "info"

    def __init__(self, url: str, *, audio_transcriber=None):
        self.url = url
        self._candidates_cached = None
        # 由 qa_auto_agent 闭包注入，复用其下载、预算、证据门控与审计。
        self._audio_transcriber = audio_transcriber

    def entry_addrs(self) -> list[str]:
        # 入口=先扫时间轴索引(label_sweep);audio 作备选(问台词时)
        return ["index", "audio"]

    def observe(self, addr: str, query: str = "") -> Observation:
        q = query or "What is shown in this frame?"
        if addr == "index":
            if _sweep is None:
                return Observation(addr=addr, content="", kind="empty",
                                   note="[DEGRADED]video_frame 未加载")
            r = _sweep(self.url, n_frames=12)
            if not r.get("ok"):
                return Observation(addr="index", content="", kind="empty",
                                   note=f"扫描失败:{r.get('reason','')[:80]}")
            hits = r.get("hits", [])
            # 候选时刻 → 可跳转地址 t:{sec}
            cands = [f"t:{h['time_sec']}" for h in hits]
            self._candidates_cached = cands
            preview = "; ".join(f"{h['ts']}:{h['text_preview'][:40]}" for h in hits[:6])
            return Observation(
                addr="index",
                content=f"时间轴含文字候选时刻: {preview}" if preview else "整片无明显文字(纯画面)",
                candidates=cands + ["audio"], kind="timeline",
                note=r.get("reason", "")[:80], addr_meta={"axis": "time", "source": "frame_index"},
            )
        if addr.startswith("t:"):
            if _frame is None:
                return Observation(addr=addr, content="", kind="empty", note="[DEGRADED]未加载")
            try:
                sec = int(addr[len("t:"):])
            except Exception:
                sec = 0
            res = _frame(self.url, at_time=sec, question=q)
            ok = res.get("ok")
            content = res.get("answer", "") if ok else ""
            note = res.get("reason", "")[:60]
            # 透传证据类型仪表(read/inferred)——inferred 结论别当查询键
            if res.get("evidence_type"):
                note = f"[{res['evidence_type']}] " + note
            return Observation(addr=addr, content=content, kind="frame", note=note,
                               addr_meta={"axis": "time", "seconds": sec, "source": "frame"})
        if addr == "audio":
            if self._audio_transcriber is None:
                return Observation(addr="audio", content="", kind="audio",
                                   note="[DEGRADED]未注入音轨转写器")
            raw = self._audio_transcriber(self.url)
            # qa 闭包返回 [start-end] text；将每段变成同时间轴可跳转地址。
            segments = []
            for start, end, text in re.findall(r"\[(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)\]\s*([^\n]+)", raw or ""):
                segments.append({"start": float(start), "end": float(end), "text": text.strip()})
            candidates = [f"audio:{item['start']:.2f}:{item['end']:.2f}" for item in segments]
            return Observation(addr="audio", content={"transcript": raw, "segments": segments},
                               candidates=candidates, kind="audio",
                               note=f"带时间戳转写{len(segments)}段",
                               addr_meta={"axis": "time", "source": "audio"})
        if addr.startswith("audio:"):
            try:
                _, start, end = addr.split(":", 2)
                start, end = float(start), float(end)
            except Exception:
                return Observation(addr=addr, content="", kind="empty", note=f"非法音频地址:{addr}")
            # 音频段内容已由 audio observation 保存；此地址提供精确的时序锚点给关系图和决策器。
            return Observation(addr=addr, content=f"音频时间段 {start:.2f}-{end:.2f}s；请结合 audio 转写证据读取。",
                               kind="audio_segment", note="与同时间帧可互证",
                               addr_meta={"axis": "time", "start": start, "end": end,
                                          "seconds": (start + end) / 2, "source": "audio"})
        return Observation(addr=addr, content="", kind="empty", note=f"地址不存在:{addr}")

    def act(self, action: Action) -> dict:
        return {"ok": True, "detail": f"av-space act {action.kind}"}


if __name__ == "__main__":
    from traversal_core import traverse
    import sys as _s
    # AV e2e 依赖网络下视频·给一个公开视频 dry 测遍历骨架能否驱动
    url = _s.argv[1] if len(_s.argv) > 1 else "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
    goal = _s.argv[2] if len(_s.argv) > 2 else "What text appears in the video?"
    sp = AVSpace(url)
    r = traverse(sp, goal, max_steps=4)
    print(f"ans={r.answer[:120]!r} steps={r.steps} stop={r.stop_reason}")
    for t in r.trace:
        print(f"  step{t.get('step')}: {t.get('action')} {t.get('target')}")
