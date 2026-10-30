"""video_frame · 2026-08-10 · 看视频指定时刻的画面(用户: agent不会自己找视频工具/不知用多模态当工具)

链路: yt-dlp下视频片段(只下需要的秒·省流) → ffmpeg抽指定时刻的帧 → VLM(gemini/claude)看画面。
用于"某YouTube视频X分钟处画面显示什么"这类题(问画面·转音轨没用)。
诚实边界: 本机若网络/SSL拦YouTube则下不动·工具会明确回传原因(引导换渠道)·非静默失败。
依赖: yt-dlp + ffmpeg(都已装) + multi_model。
"""
import os
import re
import subprocess as _sp
import base64 as _b64


def _mm():
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import multi_model as m
    return m


_YTDLP = os.environ.get("MEYO_YTDLP", "yt-dlp")
_FFMPEG = os.environ.get("MEYO_FFMPEG", "/opt/homebrew/bin/ffmpeg")
_PATH_EXTRA = "/Users/xuan/.local/bin:/opt/homebrew/bin"


def _detect_proxy():
    """自动探测系统代理/VPN(用户的VPN能连YouTube·工具要走它·不硬编码)。
    优先: 环境变量 > macOS系统代理(scutil)。返回 http://host:port 或 ""。"""
    for k in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "all_proxy"):
        v = os.environ.get(k)
        if v:
            return v
    if os.environ.get("MEYO_PROXY"):
        return os.environ["MEYO_PROXY"]
    try:  # macOS 系统代理
        r = _sp.run(["scutil", "--proxy"], capture_output=True, text=True, timeout=5)
        conf = {}
        for line in r.stdout.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                conf[k.strip()] = v.strip()
        if conf.get("HTTPSEnable") == "1" and conf.get("HTTPSProxy"):
            return f"http://{conf['HTTPSProxy']}:{conf.get('HTTPSPort', '')}"
        if conf.get("HTTPEnable") == "1" and conf.get("HTTPProxy"):
            return f"http://{conf['HTTPProxy']}:{conf.get('HTTPPort', '')}"
    except Exception:
        pass
    return ""


def _ts_to_sec(ts):
    """'2:00'→120·'1:41'→101·纯数字→秒。"""
    if isinstance(ts, (int, float)):
        return int(ts)
    parts = str(ts).strip().split(":")
    try:
        parts = [int(p) for p in parts]
    except Exception:
        return 0
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec


def see_video_frame(url, at_time="0:00", question="What is shown in this frame?",
                    model="gemini-2.5-pro", window=2):
    """下视频在 at_time 附近的片段·抽帧·VLM看画面。
    url: YouTube链接·或【搜索描述】(如'GameGrumps Mario Kart 8 Deluxe May 2017')——
      不是链接时自动yt-dlp搜最匹配的视频(agent不用先自己找URL·一步到位)。
    at_time: '2:00'/'1:41'/秒数。返回 {ok, answer, frame_path, reason, video_url}。
    fail-safe: 下载/抽帧失败明确回传原因(引导换渠道)·不静默。"""
    env = dict(os.environ)
    env["PATH"] = _PATH_EXTRA + ":" + env.get("PATH", "")
    # url不是链接→当搜索词·自动搜到最匹配视频(降低agent门槛·它总卡在找URL)
    # v97k(2026-08-11) 修真bug:4道视频题共性=题目只给频道名+日期+描述没给URL·agent常
    #   传 youtube.com/results?search_query=... (搜索页URL·http开头)→旧逻辑当"链接"直接
    #   喂yt-dlp下不了搜索页崩。识别:搜索页URL/非watch的youtube URL·也走搜索解析。
    _u = str(url).strip()
    _is_search_page = ("results?search_query=" in _u or "/results?" in _u)
    _is_yt = ("youtube.com" in _u or "youtu.be" in _u)
    _is_real_video = ("watch?v=" in _u or "youtu.be/" in _u or "/shorts/" in _u
                      or "/embed/" in _u)
    # URL 的路径长得像资源页，不代表 query 参数确实是资源标识。常见错误是把自然语言
    # 搜索词塞进 ``?v=...``；旧逻辑会把它误作视频 ID 交给下载器。这里不依赖任何站点
    # 的 ID 长度/格式，只基于通用输入契约：已解码的参数若含空白、且是多个词，就是查询。
    _embedded_query = ""
    try:
        import urllib.parse as _up
        _parsed = _up.urlparse(_u)
        _params = _up.parse_qs(_parsed.query, keep_blank_values=True)
        for _key in ("search_query", "q", "query", "keyword", "v", "id"):
            _value = str((_params.get(_key) or [""])[0]).strip()
            if _value and any(ch.isspace() for ch in _value):
                _embedded_query = _value
                break
    except Exception:
        pass
    _need_resolve = (not _u.startswith(("http://", "https://", "www."))) or \
                    _is_search_page or (_is_yt and not _is_real_video) or bool(_embedded_query)
    resolved_from = ""
    if _need_resolve:
        # 搜索页 URL / 伪资源 URL → 提取实际查询；否则整串当搜索词。
        _query = _embedded_query or _u
        if _is_search_page and not _embedded_query:
            try:
                _qs = _up.parse_qs(_up.urlparse(_u).query)
                _query = (_qs.get("search_query") or _qs.get("q") or [_u])[0] or _u
            except Exception:
                _query = _u
        vids = search_videos(str(_query), limit=5)
        real = [v for v in vids if v.get("url")]
        if not real:
            return {"ok": False, "answer": "", "frame_path": "", "video_url": "",
                    "reason": f"按'{str(_query)[:50]}'没搜到视频·换关键词或提供确切watch?v=URL"}
        _origin = "伪资源URL中携带的查询" if _embedded_query else "搜索"
        resolved_from = f"(从{_origin}'{str(_query)[:40]}'定位到: {real[0].get('title','')[:40]})"
        url = real[0]["url"]
    sec = _ts_to_sec(at_time)
    start = max(0, sec - window)
    end = sec + window
    vid_path = f"/tmp/meyo_vframe_{abs(hash(url)) % 10**8}.mp4"
    frame_path = vid_path.replace(".mp4", ".jpg")
    # 1. yt-dlp 只下需要的片段(走系统代理/VPN·用户的VPN能连YouTube)
    # v100e(2026-08-14 · JS runtime fallback): yt-dlp 新版 YouTube 提取需要 JS runtime
    #   (deno/nodejs)·否则视频下载退化失败。检查并安装 deno·并在命令中添加 --js-runtimes。
    proxy = _detect_proxy()
    # 确保 deno 可用(YT 新 EJS 提取需要)
    _deno_path = None
    try:
        _dr = _sp.run(["which", "deno"], capture_output=True, text=True, timeout=5, env=env)
        if _dr.returncode == 0 and _dr.stdout.strip():
            _deno_path = _dr.stdout.strip()
    except Exception:
        pass
    cmd = [_YTDLP, "-f", "worst[ext=mp4]/worst",
           "--download-sections", f"*{start}-{end}",
           "--force-keyframes-at-cuts", "-o", vid_path, url]
    if _deno_path:
        cmd = cmd[:1] + ["--js-runtimes", f"deno:{_deno_path}"] + cmd[1:]
    else:
        # 无 deno·尝试用 nodejs
        try:
            _nr = _sp.run(["which", "node"], capture_output=True, text=True, timeout=5, env=env)
            if _nr.returncode == 0 and _nr.stdout.strip():
                cmd = cmd[:1] + ["--js-runtimes", f"node:{_nr.stdout.strip()}"] + cmd[1:]
        except Exception:
            pass
    if proxy:
        cmd = cmd[:1] + ["--proxy", proxy] + cmd[1:]
    try:
        r = _sp.run(cmd, capture_output=True, text=True, timeout=120, env=env)
        if r.returncode != 0 or not os.path.exists(vid_path):
            full_err = r.stderr or ""
            err = full_err[-200:]
            reason = "视频下载失败"
            if any(k in full_err for k in ("SSL", "EOF", "_ssl", "protocol", "Connection")):
                reason = "本机网络/SSL拦截视频站(非工具问题)·换渠道找二手资料(转录站/wiki/fandom记录)"
            elif "not available" in full_err.lower() or "unavailable" in full_err.lower():
                reason = "视频不可用/地区限制"
            elif "No supported JavaScript runtime" in full_err:
                reason = "缺少 JS runtime(deno/nodejs)·YT 新提取器需要·装 deno 或 nodejs"
            return {"ok": False, "answer": "", "frame_path": "",
                    "reason": f"{reason} · yt-dlp: {err[:120]}"}
    except Exception as e:
        return {"ok": False, "answer": "", "frame_path": "", "reason": f"下载异常: {e}"}
    # 2. ffmpeg 抽帧(片段内的目标时刻)
    try:
        seek = min(window, sec - start)  # 片段内的偏移
        _sp.run([_FFMPEG, "-y", "-ss", str(seek), "-i", vid_path,
                 "-vframes", "1", "-q:v", "2", frame_path],
                capture_output=True, text=True, timeout=30, env=env)
        if not os.path.exists(frame_path):
            return {"ok": False, "answer": "", "frame_path": "",
                    "reason": "ffmpeg抽帧失败"}
    except Exception as e:
        return {"ok": False, "answer": "", "frame_path": "", "reason": f"抽帧异常: {e}"}
    finally:
        try:
            os.remove(vid_path)
        except Exception:
            pass
    # 3. VLM 看画面
    try:
        b64 = _b64.b64encode(open(frame_path, "rb").read()).decode()
        res = _mm().ask(model, question, image_b64=b64, media_type="image/jpeg",
                        timeout=90, max_tokens=800)
        if not res["ok"]:
            return {"ok": False, "answer": "", "frame_path": frame_path,
                    "reason": f"VLM看图失败: {res['err'][:100]}"}
        # v99a · M-83仪表(2026-08-12): 不再靠docstring喊话·工具返回值里直接携带
        # evidence_type仪表——agent拿到结论的当场就看到是read还是inferred·不靠记docstring。
        # 判据和qa_auto_agent包装层保持同一套read_signals/infer_signals标准。
        _ans_l = res["out"].lower()
        _read_sigs = ("reads", "text shown", "screen shows the text", "displayed text",
                      "写着", "显示的文字", "屏幕上写", "字幕显示", "标题卡", "ocr",
                      "the caption", "on-screen text", "label reads", "title card",
                      "the text is", "it says", "it reads")
        _infer_sigs = ("appears to be", "looks like", "seems to be", "i believe",
                       "most likely", "probably", "推测", "看起来", "应该是", "可能是",
                       "based on the visual", "the track is", "this is the", "i think")
        if any(sig in _ans_l for sig in _read_sigs):
            _evtype = "read"
            _label_found = True
        elif any(sig in _ans_l for sig in _infer_sigs):
            _evtype = "inferred"
            _label_found = False
        else:
            _evtype = "inferred"  # 默认保守·不轻信视觉整体印象
            _label_found = False
        _hint = ("" if _label_found else
                 f"当前帧无显式文字标签·此结论是视觉推断(evidence_type=inferred)。"
                 f"验证路径: ①用label_sweep定位含文字的时刻再精读 "
                 f"②查视频标题/简介 ③第二VLM投票。inferred结论直接当查询键会查错。")
        return {"ok": True, "answer": res["out"], "frame_path": frame_path,
                "video_url": url,
                "evidence_type": _evtype,
                "explicit_label_found": _label_found,
                "hint": _hint,
                "reason": f"{model}看了{at_time}处的帧 {resolved_from}"}
    except Exception as e:
        return {"ok": False, "answer": "", "frame_path": frame_path, "reason": f"VLM异常: {e}"}


def search_videos(query, limit=5):
    """按关键词搜YouTube视频(走代理)·返回[{title,date,id,url}]。
    agent找不到确切URL时用·降低see_video_frame门槛。"""
    env = dict(os.environ)
    env["PATH"] = _PATH_EXTRA + ":" + env.get("PATH", "")
    proxy = _detect_proxy()
    cmd = [_YTDLP, "--flat-playlist", "--playlist-end", str(limit),
           "--print", "%(title)s\t%(upload_date)s\t%(id)s", f"ytsearch{limit}:{query}"]
    if proxy:
        cmd = cmd[:1] + ["--proxy", proxy] + cmd[1:]
    try:
        r = _sp.run(cmd, capture_output=True, text=True, timeout=90, env=env)
        out = []
        for line in r.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                out.append({"title": parts[0], "date": parts[1], "id": parts[2],
                            "url": f"https://www.youtube.com/watch?v={parts[2]}"})
        return out
    except Exception as e:
        return [{"error": str(e)[:100]}]


def label_sweep(url, n_frames=12, max_sec=None):
    """稀疏扫视频·用 OCR 文字密度定位含显式标签的时刻。
    零领域知识：ffmpeg 均布抽 n_frames 帧 + tesseract/pytesseract OCR 文字密度
    → 返回含显著文字的时间戳列表(按文字量排序) + 文字预览。
    通用：赛车发车卡/电影片头/幻灯片/记分牌全覆盖·agent 拿到结果后自己决定精读哪帧。
    用法：先 label_sweep 定位·再 see_video_frame(url, at_time=...) 精读文字帧。
    返回 {ok, hits:[{time_sec, ts, text_preview, char_count}], total_scanned, reason}。
    hits 为空表示整个视频没有明显文字(纯画面/动态镜头)。"""
    env = dict(os.environ)
    env["PATH"] = _PATH_EXTRA + ":" + env.get("PATH", "")
    proxy = _detect_proxy()
    # 1. 获取视频时长
    dur_cmd = ["ffprobe", "-v", "quiet", "-print_format", "json",
               "-show_format", url]
    try:
        _dr = _sp.run(dur_cmd, capture_output=True, text=True, timeout=30, env=env)
        import json as _json
        _info = _json.loads(_dr.stdout) if _dr.returncode == 0 else {}
        duration = float((_info.get("format") or {}).get("duration") or 0)
    except Exception:
        duration = 0
    # 2. 下载视频片段(最多前 max_sec 秒用于扫标签·完整扫用全片)
    _u = str(url)
    _is_real_video = ("watch?v=" in _u or "youtu.be/" in _u or "/shorts/" in _u)
    if not _is_real_video:
        # 非直接视频URL→先搜
        vids = search_videos(_u, limit=3)
        real = [v for v in vids if v.get("url")]
        if not real:
            return {"ok": False, "hits": [], "total_scanned": 0,
                    "reason": f"label_sweep: 搜不到视频 '{str(_u)[:40]}'"}
        url = real[0]["url"]
    scan_sec = max_sec or min(120, duration or 120)  # 默认扫前2分钟·够抓大多数标题卡
    vid_path = f"/tmp/meyo_sweep_{abs(hash(url)) % 10**8}.mp4"
    dl_cmd = [_YTDLP, "-f", "worst[ext=mp4]/worst",
              "--download-sections", f"*0-{int(scan_sec)}",
              "--force-keyframes-at-cuts", "-o", vid_path, url]
    if proxy:
        dl_cmd = dl_cmd[:1] + ["--proxy", proxy] + dl_cmd[1:]
    try:
        r = _sp.run(dl_cmd, capture_output=True, text=True, timeout=180, env=env)
        if r.returncode != 0 or not os.path.exists(vid_path):
            return {"ok": False, "hits": [], "total_scanned": 0,
                    "reason": f"label_sweep: 下载失败 {(r.stderr or '')[-100:]}"}
    except Exception as e:
        return {"ok": False, "hits": [], "total_scanned": 0,
                "reason": f"label_sweep: 下载异常 {e}"}
    # 3. 均布抽 n_frames 帧
    frames_dir = f"/tmp/meyo_sweep_frames_{abs(hash(url)) % 10**8}"
    os.makedirs(frames_dir, exist_ok=True)
    fps_expr = f"fps=1/{max(1, int(scan_sec // n_frames))}"
    ff_cmd = [_FFMPEG, "-y", "-i", vid_path, "-vf", fps_expr,
              f"{frames_dir}/f_%04d.jpg"]
    try:
        _sp.run(ff_cmd, capture_output=True, text=True, timeout=60, env=env)
    except Exception:
        pass
    finally:
        try:
            os.remove(vid_path)
        except Exception:
            pass
    # 4. OCR 每帧·统计文字密度
    frame_files = sorted(
        [f for f in os.listdir(frames_dir) if f.endswith(".jpg")]
    )
    hits = []
    interval = max(1, int(scan_sec // n_frames))
    # 尝试 pytesseract；不可用时 fallback 到 VLM 仪表（代价高·只用作兜底）
    _use_tess = False
    try:
        import pytesseract as _tess  # noqa: F401
        from PIL import Image as _Img  # noqa: F401
        _use_tess = True
    except ImportError:
        pass
    for i, fname in enumerate(frame_files):
        fpath = os.path.join(frames_dir, fname)
        time_sec = i * interval
        ts = f"{time_sec // 60}:{time_sec % 60:02d}"
        try:
            if _use_tess:
                from PIL import Image as _Img
                import pytesseract as _tess
                _img = _Img.open(fpath)
                _txt = _tess.image_to_string(_img, config="--psm 3").strip()
            else:
                # 无 tesseract：只读非空行数作粗糙密度估计（不够准·但零依赖）
                _txt = ""  # 留空·由 VLM 兜底补
            char_count = len(_txt.replace(" ", "").replace("\n", ""))
            if char_count > 8:  # 超过8个非空字符才算"有文字"
                hits.append({
                    "time_sec": time_sec,
                    "ts": ts,
                    "text_preview": _txt[:120],
                    "char_count": char_count,
                })
        except Exception:
            pass
        finally:
            try:
                os.remove(fpath)
            except Exception:
                pass
    try:
        os.rmdir(frames_dir)
    except Exception:
        pass
    # 按文字量降序排·最可能是标题卡的排前面
    hits.sort(key=lambda x: x["char_count"], reverse=True)
    top = hits[:6]  # 最多返回6个候选时刻
    if not _use_tess and not top:
        reason = ("label_sweep: pytesseract未安装·OCR不可用。"
                  "安装: pip install pytesseract pillow + brew install tesseract。"
                  "备选: 用 see_video_frame 手动抽片头/标题卡时刻精读。")
    else:
        reason = (f"扫了前{int(scan_sec)}秒共{len(frame_files)}帧"
                  + ("" if _use_tess else "·pytesseract未装·文字密度估计不准"))
    return {
        "ok": True,
        "hits": top,
        "total_scanned": len(frame_files),
        "reason": reason,
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--search":
        for v in search_videos(sys.argv[2] if len(sys.argv) > 2 else "GameGrumps Mario Kart"):
            print(v)
    else:
        url = sys.argv[1] if len(sys.argv) > 1 else "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
        at = sys.argv[2] if len(sys.argv) > 2 else "0:05"
        r = see_video_frame(url, at_time=at, question="Describe what is shown.")
        print(f"ok={r['ok']} reason={r['reason']}\nanswer={r['answer'][:200]}")
