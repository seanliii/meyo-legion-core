"""
v13.92 · see_image · OP-4 · VLM 视觉工具 (对标阿里云 AgenticSearch / Cloudflare Computer)

动机(2026-08-10 调研结论):
  阿里云 Ops-AgenticSearch + Cloudflare Computer 都把"原生多模态"当标配。
  GAIA 8 道视觉题(painting 水果 / blog 按钮 / YouTube #9 长度 / Tri-Rail
  时刻表 / headstone 字 / 截图等)目前全失分,因为我们只有 OCR + 音频转录,
  没有"看画面内容"的视觉能力。

设计(双路径 fallback,任何一步失败都优雅退到 OCR):
  1. vision(主):把 image(base64 or url) + question 发给 LLM vision 端点
     · 网关走 Anthropic /v1/messages 的 image content block
     · 失败/不支持 → 退到 OCR 文本路径
  2. ocr_fallback:用 read_image_ocr 拿文本后让 LLM 解读
  3. url 路径:本地没附件 → 调 web_access_bridge 下载 → 再 vision
  4. fail-safe:任何一步挂都返回 None,绝不阻塞主流程

用法(主入口):
  from see_image import see
  ans = see(path_or_url, "What fruit is in the painting?")
  # ans = "orange" or "" (失败)

环境开关:
  MEYO_VISION=off  → 整个工具直接返回 None(默认 on)
"""
from __future__ import annotations  # 兼容 Python 3.9 的 dict|None 等新式注解
import os, base64, mimetypes
from pathlib import Path

try:
    from qa_llm_gateway import call as _gw_call
except Exception:
    _gw_call = None

try:
    from qa_llm_gateway import text as _gw_text
except Exception:
    _gw_text = None

import re as _re_vc


# ═══════════════════════════════════════════════════════════════
# v99h(2026-08-13) · 关键视觉题多模型投票共识
#   病根(用户质疑"能换模型为何不换"):see() 主路径 claude 只要"成功返回非空"就
#   直接 return·永不换模型。换模型只在报错/超时/返空时触发。读手写图读崩时 claude
#   "成功返回一个残缺+编造的结果"(实证:分数图读出9项·后半 6/8、4/60 是图里没有的
#   瞎凑值)·系统当成功收下·不换模型 → 烂结果进下游打转。
#   修:换模型的开关从"报没报错"挪到"读得可不可信"——关键视觉题(手写/手绘/表格/
#   多值/几何)让 claude + 1 个异构模型都读同一张图·比对提取值重合度·分歧就标注不可信。
# ═══════════════════════════════════════════════════════════════

# 关键视觉题判据·词表只当 fast-path(守"硬编码词表不通用":不命中也可由下游 zoom/OCR
# 触发·这里只决定"要不要主动多花一次第二模型调用交叉验证")。
_CRITICAL_VISION_KWS = (
    # 手写/手绘(最易读崩)
    "handwritten", "handwriting", "hand-written", "drawn", "hand-drawn", "sketch",
    "手写", "手绘", "涂鸦",
    # 表格/多值提取(逐项读易漏易错)
    "worksheet", "quiz", "problem", "fraction", "table", "list", "spreadsheet",
    "分数", "表格", "逐项", "每一个", "每个", "列表", "清单",
    # 几何/精确读数
    "polygon", "angle", "coordinate", "vertex", "vertices", "measure", "perimeter",
    "边长", "坐标", "顶点", "面积", "周长", "角度",
    # 通用计数/多目标(读数敏感)
    "count", "how many", "计数", "数一下", "有几个",
)


def _is_critical_vision(question: str) -> bool:
    """判定是否"关键视觉题"(读数敏感·易读崩·值得多模型交叉验证)。
    纯函数·词表 fast-path。fail-safe:异常返回 False(退回单模型·不误伤)。"""
    try:
        _lowq = (question or "").lower()
        return any(k in _lowq for k in _CRITICAL_VISION_KWS)
    except Exception:
        return False


def _extract_values_overlap(text_a: str, text_b: str) -> float:
    """两段文本里【数字/分数】提取值集合的 Jaccard 重合度(交并比·0~1)。
    读手写图崩=读出的数字序列会明显不同·重合低=两模型分歧=读数不可信。
    纯函数·可单测。fail-safe:异常/两边都空 → 返回 1.0(无从判分歧·不误报)。"""
    try:
        def _vals(t):
            t = t or ""
            # 分数(3/4、103/170)优先·再抽普通数值(整数/小数)
            fracs = set(_re_vc.findall(r"\d+\s*/\s*\d+", t))
            fracs = {f.replace(" ", "") for f in fracs}
            # 抽普通数值时排掉已在分数里的·避免把 3/4 的 3 和 4 重复计入
            _t_no_frac = _re_vc.sub(r"\d+\s*/\s*\d+", " ", t)
            nums = set(_re_vc.findall(r"-?\d+(?:\.\d+)?", _t_no_frac))
            return fracs | nums
        va, vb = _vals(text_a), _vals(text_b)
        if not va and not vb:
            return 1.0  # 两边都没数值·无从比·不判分歧
        if not va or not vb:
            return 0.0  # 一边有数值一边没有·明显分歧
        inter = len(va & vb)
        union = len(va | vb)
        return inter / union if union else 1.0
    except Exception:
        return 1.0


# 场景标签:从 question 判定视觉子场景·用于经验库细分挑最优模型(先埋点后生效)。
def _scene_of(question: str) -> str:
    """粗判视觉子场景(handwritten/table/geometry/count)·供 best_vision_models(scene=)。
    数据不足时 best_vision_models 会自动退回粗粒度 vision·所以这里判不准也不致命。"""
    try:
        _q = (question or "").lower()
        if any(k in _q for k in ("handwritten", "handwriting", "hand-written", "手写", "手绘", "drawn", "sketch")):
            return "handwritten"
        if any(k in _q for k in ("polygon", "angle", "coordinate", "vertex", "vertices", "perimeter",
                                 "边长", "坐标", "顶点", "面积", "周长", "角度")):
            return "geometry"
        if any(k in _q for k in ("worksheet", "table", "spreadsheet", "fraction", "quiz",
                                 "表格", "分数", "逐项", "列表")):
            return "table"
        if any(k in _q for k in ("count", "how many", "计数", "有几个", "数一下")):
            return "count"
        return ""
    except Exception:
        return ""


def _best_vision_models(n=2, scene=None):
    """v99i:按经验库【实战正确率】挑最优 vision 模型(不钦定 claude)。
    经验库铁证 gemini 90/90 > claude 4/4 > gpt-5 1/13。委托 model_orchestrator.best_vision_models。
    fail-safe:取不到 → 退回 multi_model usable 原序。返回 model 名列表(可能<n)。"""
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
        import model_orchestrator as _mo
        picked = _mo.best_vision_models(n=n, scene=scene)
        if picked:
            return picked
    except Exception:
        pass
    try:
        import multi_model as _mm
        return _mm.usable_models(vision_only=True)[:n]
    except Exception:
        return []


def _vision_consensus(primary_out: str, img_block: dict, question: str,
                      timeout: float) -> str:
    """v99i · 关键视觉题多模型共识·按经验库最优模型挑第二读者(不钦定 claude/gpt-5)。
    primary_out = 主路径(qa_llm_gateway·claude)已读结果。这里用【经验库正确率最高的
    异构模型】(现在=gemini 90/90)独立读同图·比对提取值:
    - 高重合 → primary_out + 共识标(高置信)
    - 低重合 → primary_out + 分歧标·并【附上更可信模型(经验库最优)所见】·让下游能改采信它
    - 第二模型失败 → 原样返回 primary_out(静默降级·不拖垮主路径)
    fail-safe:任何异常都返回原 primary_out。
    注:第二读者优先取经验库最优模型·哪怕它是 claude family 也用(不再强行排除 claude)——
    但通常主路径已是 claude·所以取 top-N 里第一个≠主路径口径的模型·天然是异构。"""
    _CONSENSUS_MIN = 0.6
    try:
        scene = _scene_of(question)
        # 取经验库最优的前2个·挑一个与主路径不同的当第二读者(优先正确率最高的异构模型)
        top = _best_vision_models(n=3, scene=scene) or []
        second = top[0] if top else None
        if not second:
            return primary_out
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
        import multi_model as _mm
        _src = (img_block or {}).get("source", {})
        _b64 = _src.get("data", "")
        _mt = _src.get("media_type", "image/jpeg")
        if not _b64:
            return primary_out
        r2 = _mm.ask(second, question, image_b64=_b64, media_type=_mt,
                     timeout=max(30, timeout - 10), max_tokens=2500)
        if not (r2.get("ok") and (r2.get("out") or "").strip()):
            return primary_out
        second_out = r2["out"].strip()
        overlap = _extract_values_overlap(primary_out, second_out)
        if overlap >= _CONSENSUS_MIN:
            return f"{primary_out}\n[✓视觉共识·{second}(经验库最优)读数一致(重合{overlap:.0%})·高置信]"
        # 分歧:主路径与经验库最优模型读数不一致·后者更可信·附上供下游改采信
        return (f"{primary_out}\n[⚠️视觉共识分歧·经验库最优模型({second})读出不同数值"
                f"(重合仅{overlap:.0%})·**主路径读数不可信·优先采信下方 {second} 版本**·"
                f"或 see_image_zoom 精读关键区域/代码校验。\n"
                f"{second} 所见:{second_out[:800]}]")
    except Exception:
        return primary_out


# ═══════════════════════════════════════════════════════════════
# 工具主入口
# ═══════════════════════════════════════════════════════════════

def see(path_or_url: str, question: str, *, timeout: float = 60) -> str:
    """看图并回答 question。返回 str(模型回答),失败返回 ""。

    v99c(2026-08-12·D1·自主轮转):
      原来 claude(DEFAULT_MODEL) 超时 → 直接 OCR fallback(看不了图)。
      现在:claude 超时/失败 → 自动轮转到 multi_model 里其他可用视觉模型(gemini/gpt-5)。
      优先级:claude(主路径·Anthropic native) → multi_model 视觉轮转池 → OCR 兜底。
      失败原因打标透传(agent 能诊断)·不静默吞。
    """
    if os.environ.get("MEYO_VISION", "on").lower() in ("off", "0", "false", "no"):
        return ""
    if not path_or_url:
        return ""
    q = (question or "Describe what you see in this image.").strip()[:1000]

    # 1. 准备 image content block(base64 编码·一次准备·所有路径复用)
    img_block = _to_image_content_block(path_or_url)
    if img_block is None:
        if _DL_LAST_ERR:
            return f"[图片获取失败] {_DL_LAST_ERR}"
        if str(path_or_url).startswith(("http://", "https://")):
            return "[图片获取失败] URL 下载未成功(可能非真图片直链/超 5MB)"
        return "[图片获取失败] 本地路径不存在或不是文件"

    # 2. 主路径: claude Anthropic native vision(最快·支持原生 image block)
    _primary_ok = False
    _primary_err = ""
    if _gw_call is not None:
        try:
            r = _vision_call(q, img_block, timeout=timeout)
        except Exception as _e:
            r = None
            _primary_err = f"{type(_e).__name__}: {str(_e)[:80]}"
        if r and r.get("ok") and (r.get("out") or "").strip():
            out = r["out"].strip()
            # finish_reason 感知消费:截断→升级 max_tokens 重调一次
            if r.get("truncated"):
                try:
                    r2 = _vision_call(q, img_block, timeout=timeout, max_tokens=4096)
                except Exception:
                    r2 = None
                if r2 and r2.get("ok") and (r2.get("out") or "").strip() and not r2.get("truncated"):
                    out = r2["out"].strip()
                else:
                    base = (r2["out"].strip() if (r2 and r2.get("out")) else out)
                    out = f"{base}\n[⚠️TRUNCATED·VLM输出被max_tokens截断·读数可能不完整·别当完整结果用]"
            # v99h · 关键视觉题:claude 主路径"成功"了也别急着收下——读手写图易读崩却
            #   "成功返回"·所以关键题追加第二个异构模型交叉验证·分歧就标注不可信。
            #   (换模型的开关从"报没报错"挪到"读得可不可信")。fail-safe 在 _vision_consensus 内。
            if _is_critical_vision(q):
                out = _vision_consensus(out, img_block, q, timeout)
            return out
        else:
            _primary_ok = False
            if r:
                _primary_err = _primary_err or r.get("err", "") or "空返回"

    # 3. v99c 自主轮转: 主路径失败/超时 → 用 multi_model 里其他视觉模型接力
    #    不依赖 agent 主动发现并切换工具——感知路径自愈，不中断主流程。
    _fallback_out = _multi_model_vision_fallback(
        img_block, q, timeout=max(30, timeout - 10), primary_err=_primary_err)
    if _fallback_out:
        return _fallback_out

    # 4. 最终兜底: OCR 文本 + LLM 解读(读文字·不能真"看图")
    return _ocr_fallback(path_or_url, q, timeout=timeout)


def _multi_model_vision_fallback(img_block: dict, question: str,
                                  timeout: float, primary_err: str = "") -> str:
    """v99c: claude 主路径失败后，轮转 multi_model 里的其他视觉模型。
    fail-safe: 任何 import/调用失败都静默返回 ""，让 OCR fallback 兜底。
    """
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
        import multi_model as _mm
        import base64 as _b64
        # 从 img_block 拿回 base64(已经编好·不重复 IO)
        _src = img_block.get("source", {})
        _b64_data = _src.get("data", "")
        _mt = _src.get("media_type", "image/jpeg")
        if not _b64_data:
            return ""
        # v99k:轮转顺序改用 best_vision_models(按经验库正确率·含claude·让数据决定该换谁)·
        #   不再硬排除 claude family(经验库若显示claude在某场景最优就该用它)。
        _all_usable = _mm.usable_models(vision_only=True)
        _pool = list(_all_usable)
        try:
            import model_orchestrator as _mo
            _ranked = _mo.best_vision_models(n=len(_all_usable) or 3,
                                             scene=_scene_of(question))
            _pool = [m for m in _ranked if m in _all_usable] + \
                    [m for m in _all_usable if m not in _ranked]
        except Exception:
            pass
        _cur_to = timeout
        _tried = []
        for _fb_model in _pool:
            _tried.append(_fb_model)
            _r = _mm.ask(_fb_model, question, image_b64=_b64_data, media_type=_mt,
                         timeout=_cur_to, max_tokens=2500)
            if _r.get("ok") and (_r.get("out") or "").strip():
                # 回写经验库: 此路成功返回(stage=returned·真答对由finalize回填solved)
                try:
                    _mo.record_experience("vision", _fb_model, correct=True,
                                          ts=__import__("time").time(), stage="returned")
                except Exception:
                    pass
                _prefix = f"[{_fb_model}·vlm_fallback]" + (
                    f"[主路径{primary_err[:40]}]" if primary_err else "")
                return f"{_prefix} {_r['out'].strip()}"
            # 4xx 立即停(模型配置错误·换也没用)
            _fb_err = str(_r.get("err", ""))
            if any(k in _fb_err.lower() for k in ("404", "401", "403", "not found", "invalid model")):
                break
            # 递减剩余超时(给后续模型留时间)
            _cur_to = max(20, _cur_to - 10)
        return ""
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════
# 内部:vision 调用
# ═══════════════════════════════════════════════════════════════

def _vision_call(question: str, image_block: dict, *, timeout: float, max_tokens: int = 2500) -> dict | None:
    """直接构造 Anthropic /v1/messages 的 multimodal payload。"""
    try:
        import requests, time
        from qa_llm_gateway import _config, _classify_gateway_error, _jittered_backoff, DEFAULT_MODEL
    except Exception:
        return None
    try:
        base_url, auth = _config()
    except Exception:
        return None
    payload = {
        # v97c(2026-08-11) 800→2500:踩过的坑(记忆 v13.92 "max_tokens800截断=0/3假失败")。
        #   实测多边形面积题——手动连调有时返1781字有时返空,真根因=遇到要摆坐标/逐边计算的
        #   题,模型思考长,800 token 全耗在推理上→最终答案被截成空→表现为"网关抖动返空"。
        #   这不是网关不稳,是输出预算太小。放到2500给足视觉推理+作答空间。
        "model": DEFAULT_MODEL,
        "max_tokens": max_tokens,  # 默认 2500·截断时 see() 用 4096 重调(finish_reason 消费)
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", **image_block},
                {"type": "text", "text": question},
            ],
        }],
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {auth}",
        "anthropic-version": "2023-06-01",
    }
    try:
        # v99k(2026-08-13·治VLM调用挂起500s卡死):标量timeout只管"连接+两次读间隔"·
        #   服务器慢速持续吐数据时永不触发→挂到撞wall。改 (connect, read) 元组·read=总预算·
        #   连接10s封顶·让慢速挂起也能被 read timeout 打断·不再死等。
        resp = requests.post(f"{base_url}/v1/messages",
                             json=payload, headers=headers,
                             timeout=(10, float(timeout)))
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    try:
        body = resp.json()
        content = body.get("content", [])
        text = "".join(b.get("text", "") for b in content if isinstance(b, dict)).strip()
        # finish_reason 感知消费(2026-08-12·截断静默吞掉是老病·多边形题实证):
        #   Anthropic 输出撞 max_tokens → stop_reason=="max_tokens"。以前硬编码 truncated:False
        #   从不读→VLM 逐边 re-trace 被砍在中途·下游当完整读数用·感知链断了没人知道。
        #   现在读真值·截断=True·由 see() 打标传给消费层(触发续写/升级·不静默)。
        _stop = body.get("stop_reason") or ""
        truncated = (_stop == "max_tokens")
        return {"ok": bool(text), "out": text, "err": "",
                "elapsed": 0.0, "first_byte_s": 0.0, "truncated": truncated,
                "stop_reason": _stop, "attempts": 1}
    except Exception:
        return None


def _to_image_content_block(path_or_url: str) -> dict | None:
    """路径或 URL → Anthropic image content block；远程临时文件读完即删。"""
    p = path_or_url or ""
    if not p:
        return None
    downloaded = ""
    if p.startswith("http://") or p.startswith("https://"):
        downloaded = _download_to_tmp(p) or ""
        if not downloaded:
            return None  # 失败原因已写进 _DL_LAST_ERR
        p = downloaded
    path = Path(p)
    try:
        if not path.exists() or not path.is_file() or path.stat().st_size > 5 * 1024 * 1024:
            return None
        data = path.read_bytes()
        mt = mimetypes.guess_type(str(path))[0] or "image/jpeg"
        b64 = base64.standard_b64encode(data).decode("ascii")
        return {"source": {"type": "base64", "media_type": mt, "data": b64}}
    except Exception:
        return None
    finally:
        if downloaded:
            try:
                Path(downloaded).unlink(missing_ok=True)
            except OSError:
                pass


_DL_LAST_ERR = ""  # 最近一次下载失败原因(供 agent 诊断:是 URL 错还是网关错)


def _download_to_tmp(url: str) -> str | None:
    """下载远程图片到 /tmp,返回本地路径;失败 None(原因写进 _DL_LAST_ERR)。

    v13.92 修 3 病根(墓碑题 4 次 see_image 全"图片无法加载"):
      · 加浏览器 UA:很多站(benjerry/wiki/新闻)对无 UA 请求直接 403
      · 加 1 次重试:瞬时抖动不至于直接判死
      · 校验 content-type 真是图片:拿到 HTML 错误页/404 页不再当图存下去喂 VLM
      · 失败原因回传(_DL_LAST_ERR),agent 才能判断该换 URL 还是换策略
    """
    global _DL_LAST_ERR
    _DL_LAST_ERR = ""
    try:
        import requests, tempfile
    except Exception as e:
        _DL_LAST_ERR = f"import requests 失败: {e}"
        return None
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/125.0 Safari/537.36"),
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    last_exc = ""
    for attempt in range(2):  # 原始 + 1 次重试
        try:
            r = requests.get(url, timeout=30, stream=True,
                             headers=headers, allow_redirects=True)
        except Exception as e:
            last_exc = f"请求异常({type(e).__name__}): {e}"
            continue
        if r.status_code != 200:
            _DL_LAST_ERR = f"HTTP {r.status_code}(URL 可能失效/无权限)"
            return None  # 状态码明确,不重试
        ct = (r.headers.get("content-type") or "").lower()
        # content-type 校验:必须是图片,否则大概率是 HTML 错误页/防盗链页
        if "image/" not in ct and not any(
            url.lower().split("?")[0].endswith(ext)
            for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif")
        ):
            _DL_LAST_ERR = (f"content-type={ct!r} 非图片"
                            f"(URL 大概率返回 HTML/错误页,不是真图片直链)")
            return None
        suffix = ".jpg"
        if "png" in ct: suffix = ".png"
        elif "webp" in ct: suffix = ".webp"
        elif "gif" in ct: suffix = ".gif"
        fd, out = tempfile.mkstemp(prefix="see_image_", suffix=suffix)
        os.close(fd)
        try:
            n = 0
            with open(out, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk); n += len(chunk)
            if n == 0:
                _DL_LAST_ERR = "下载内容为空(0 字节)"
                os.unlink(out)
                return None
            return out
        except Exception as e:
            last_exc = f"写文件异常: {e}"
            try:
                os.unlink(out)
            except OSError:
                pass
            continue
    _DL_LAST_ERR = last_exc or "未知下载失败"
    return None


# ═══════════════════════════════════════════════════════════════
# 内部:OCR fallback
# ═══════════════════════════════════════════════════════════════

def _local_ocr(path_or_url: str) -> str:
    """自足 OCR:下载(若 URL)→ pytesseract 抽字。不依赖外部闭包。

    v13.92 修死 import 病根:原 `from qa_tool_impls import read_image_ocr`
    永远 ImportError(真实现是 qa_auto_agent 内层闭包·模块级拿不到)。
    改为本模块内直接跑 tesseract,worktree/异机都能用。
    """
    p = path_or_url or ""
    downloaded = ""
    if p.startswith("http://") or p.startswith("https://"):
        downloaded = _download_to_tmp(p) or ""
        if not downloaded:
            return ""
        p = downloaded
    path = Path(p)
    if not path.exists() or not path.is_file():
        return ""
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
    except Exception:
        if downloaded:
            try:
                Path(downloaded).unlink(missing_ok=True)
            except OSError:
                pass
        return ""  # OCR 库未装,静默退(vision 才是主路径)
    try:
        with Image.open(str(path)) as _img:
            _w, _h = _img.size
            if max(_w, _h) < 1000:  # 小图放大提升识别率(同主 OCR 闭包逻辑)
                _sc = min(3, 1000 // max(1, min(_w, _h)) + 1)
                _img = _img.resize((_w * _sc, _h * _sc))
            return (pytesseract.image_to_string(_img) or "").strip()
    except Exception:
        return ""
    finally:
        if downloaded:
            try:
                Path(downloaded).unlink(missing_ok=True)
            except OSError:
                pass


def _ocr_fallback(path_or_url: str, question: str, *, timeout: float) -> str:
    """vision 失败 → 走 OCR 拿文本 + LLM 解读。"""
    ocr_text = _local_ocr(path_or_url)

    if not ocr_text or not _gw_call:
        # OCR 也没拿到:回传诊断,别给空串(agent 才知道该换策略)
        if _DL_LAST_ERR:
            return f"[看图失败] VLM 未返回且 OCR 兜底失败:{_DL_LAST_ERR}"
        return ""
    prompt = (
        "以下是这张图的 OCR 文本(可能不完整 / 有错)。请基于这些文本+你的常识,"
        "尽量回答用户问题。如果 OCR 文本完全无法回答,就如实说'OCR 无法识别'。\n\n"
        f"【OCR 文本】\n{ocr_text[:2000]}\n\n"
        f"【用户问题】\n{question}"
    )
    try:
        # OCR 是文本兜底，不应因历史 text("opus", ...) 兼容接口而绕过
        # 当前运行的模型锁。显式 light，使 longcat 评测全链保持 longcat-2.0。
        result = _gw_call(prompt, timeout=timeout, tier="light")
        return (result.get("out") or "").strip()
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════
# 自测
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # 仅 dry-run:看 block 转换 + 路径判断是否正确
    tests = [
        "/tmp/xxx.jpg",
        "https://example.com/foo.png",
        "",
        "ftp://nope.jpg",
    ]
    for t in tests:
        b = _to_image_content_block(t)
        print(f"  [{t!r:50s}] → {'block ✓' if b else 'None ✗'} · "
              f"type={b.get('source',{}).get('media_type') if b else '-'}")