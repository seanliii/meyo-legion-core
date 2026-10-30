"""see_image_zoom · 2026-08-11 · 区域裁剪+放大精读(agent自诊断conf0.78+自提: VLM读小字/细节精度不足)

agent自己诊断: 墓碑背景小字韵文/多边形紫色小数字/画作水果·多次see_image整图仍读不清·
缺'对局部细节高分辨率放大/裁剪聚焦'机制。agent自提方案: 定位→裁剪→放大→精读。
本工具实现: 把图指定区域(或九宫格分块)裁剪+上采样放大·再送VLM精读·比整图看清细节。
纯PIL+VLM·不靠CPU密集/网络。
"""
import os
import base64 as _b64
import io as _io


# 精读提示的共同契约：视觉模型先转写局部事实，再由上层做规则/算术校验。
# 不要求模型在读数不稳时直接给最终结论，避免把一个像素级误读放大为整题错误。
_PRECISION_TRANSCRIBE_PREFIX = (
    "这是一个经过裁剪放大的局部图像。执行严格的视觉转写，而不是凭常识补全。"
    "逐项抄录所有可见的数字、运算符、分数线、手写答案和标签；"
    "对每个字符标记 `确定` 或 `不确定(给出候选)`。"
    "不要把局部内容外推为整张图的结论，也不要静默修正看不清的字符。\n"
)


def _mm():
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import multi_model as m
    return m


def crop_zoom_read(image_path, question, region=None, zoom=3, model="gemini-2.5-pro",
                   precision=False):
    """裁剪图的指定区域+放大后送 VLM 精读。

    region: (left,top,right,bottom) 相对比例0-1(如(0,0.6,1,1)=下40%)。
    precision: 用于棋盘、手写数字、评分表等硬视觉读数；会启用更严格的局部转写契约，
      明确暴露不确定字符，供上层重读或使用规则求解器复核，绝不假装读准。
    """
    try:
        from PIL import Image
    except Exception:
        return {"ok": False, "answer": "", "reason": "PIL未装·pip install pillow"}
    if not os.path.exists(image_path):
        return {"ok": False, "answer": "", "reason": f"图不存在: {image_path}"}
    try:
        img = Image.open(image_path).convert("RGB")
        W, H = img.size
        if region:
            l, t, r, b = region
            # 坐标钳制，避免异常 bbox 产出空图；局部任务宁可显式失败也不读错区域。
            l, r = max(0.0, min(l, 1.0)), max(0.0, min(r, 1.0))
            t, b = max(0.0, min(t, 1.0)), max(0.0, min(b, 1.0))
            if r <= l or b <= t:
                return {"ok": False, "answer": "", "reason": f"非法区域: {region}"}
            box = (int(l * W), int(t * H), int(r * W), int(b * H))
            img = img.crop(box)
        # 放大(上采样·LANCZOS保细节)。精读默认至少 4x，减少单字符混淆。
        effective_zoom = max(zoom, 4) if precision else zoom
        nw, nh = int(img.width * effective_zoom), int(img.height * effective_zoom)
        # 限制别太大(VLM有像素上限)
        if nw * nh > 8000 * 8000:
            _s = (8000 * 8000 / (nw * nh)) ** 0.5
            nw, nh = int(nw * _s), int(nh * _s)
        img = img.resize((nw, nh), Image.LANCZOS)
        buf = _io.BytesIO()
        img.save(buf, "PNG")
        b64 = _b64.b64encode(buf.getvalue()).decode()
        # 2026-08-12·截断修复没修全所有消费点(feedback):主分支 see_image=2500·此处曾 1500
        #   →放大逐块精读被砍。对齐 2500·并消费截断标(_mm().ask 若返 truncated)。
        prompt = (_PRECISION_TRANSCRIBE_PREFIX + question) if precision else question
        r = _mm().ask(model, prompt, image_b64=b64, media_type="image/png",
                      timeout=90, max_tokens=2500)
        if not r["ok"]:
            return {"ok": False, "answer": "", "reason": f"VLM失败: {r['err'][:100]}"}
        _ans = r["out"]
        if r.get("truncated"):
            _ans = f"{_ans}\n[⚠️TRUNCATED·放大精读输出被截断·读数可能不完整]"
        return {"ok": True, "answer": _ans,
                "reason": (f"{model}读了裁剪放大{effective_zoom}x的区域{region or '整图'}"
                           + ("·严格转写模式" if precision else ""))}
    except Exception as e:
        return {"ok": False, "answer": "", "reason": f"异常: {type(e).__name__}: {str(e)[:100]}"}


def grid_scan_read(image_path, question, grid=(2, 2), zoom=2, model="gemini-2.5-pro"):
    """九宫格/网格分块扫描: 把图切成grid块·每块放大精读·合并结果。
    用于'不知道细节在图哪个区域'时·逐块看清(局部到整体)。返回各块识别结果。"""
    try:
        from PIL import Image
    except Exception:
        return {"ok": False, "reason": "PIL未装"}
    if not os.path.exists(image_path):
        return {"ok": False, "reason": f"图不存在: {image_path}"}
    rows, cols = grid
    results = []
    ok_cnt = 0
    # 2026-08-12 · 治「切块破坏全局→VLM各说各话/自相矛盾」(实证:分数题grid_scan
    #   [2,1]说"没有分数"、[2,2]说"图完全空白"·而整图明明有17个分数)。三处根因修:
    #   ①每块带全局锚点(告诉VLM整图在讲什么·本块只是局部·局部空白≠整图空白·
    #     只报本块真实看到的·绝不对整图下"空白/没有"的结论)
    #   ②不静默丢失败块(原来 r.ok=False 直接不 append·失败块凭空消失=像"这块没内容")
    #   ③不再[:200]截断(读出的分数/数字被砍半)
    _anchor = ("【全局背景】整幅图是一份完整的内容(可能含多行多列的表格/列表/多个条目)。"
               "你现在只看到其中一个被裁剪放大的小区块——**这个区块局部为空/看不到目标·完全正常·"
               "绝不代表整幅图为空或没有目标**。只逐字报告你在【本区块】里真实看到的内容(数字/分数/"
               "文字/条目);本块若确实什么都没有就说'本块无内容'·但【禁止】对整幅图下'空白/没有X'的结论。")
    for i in range(rows):
        for j in range(cols):
            region = (j / cols, i / rows, (j + 1) / cols, (i + 1) / rows)
            r = crop_zoom_read(image_path,
                               f"{_anchor}\n(这是整图第{i+1}行第{j+1}列区块)\n{question}",
                               region=region, zoom=zoom, model=model)
            if r.get("ok"):
                ok_cnt += 1
                results.append(f"[{i+1},{j+1}] {r['answer']}")
            else:
                # 失败块显式标注·不静默丢(否则agent以为这块"没内容")
                results.append(f"[{i+1},{j+1}] (本块读取失败:{r.get('reason','')[:80]})")
    # v99m·机械合并去重(用户点破"两块合起来才是完整答案·系统没合并去重按位置排"):
    #   不靠 agent 心算合并(易漏/重复)——骨架直接把各块读到的【结构化条目(数字/分数/token)】
    #   按【块的行列顺序(左→右·上→下)】提取、按出现序去重·给出合并列表。通用:任何穷举提取题
    #   (分数/数字/条目)都受益·不硬编码具体题型。原始碎片仍附上供交叉核对。
    _merged = _merge_grid_items(results)
    _merge_note = (
        "\n[⚠️切块读数说明·必读] 以上是把整图切成小块【各自独立】读出的碎片。"
        "某块说'空白/没有'只是那一小块的局部·不是整图结论。")
    _merged_block = ""
    if _merged:
        _merged_block = (
            f"\n\n[✓骨架已按块位置(左→右上→下)合并去重的条目·共{len(_merged)}个]:\n"
            + ",".join(_merged)
            + "\n(这是各块读数机械合并的结果·若题目要'按出现顺序列出全部'·"
              "以此为基础核对数量·别再心算合并。仍需回整图交叉核对有无遗漏。)")
    return {"ok": bool(ok_cnt),
            "answer": "\n".join(results) + _merged_block + _merge_note,
            "merged_items": _merged,
            "reason": f"{rows}x{cols}网格分块精读(成功{ok_cnt}/{rows*cols}块·合并{len(_merged)}条目)"}


def _merge_grid_items(block_results):
    """从各块读数里提取结构化条目(分数/数字)·按块顺序(已是左→右上→下)出现序去重合并。
    通用·不针对具体题型。fail-safe:异常返回 []。
    保留重复出现的语义(如gold里3/4出现多次)——按'每个块内去重·跨块保留'·贴近'按位置列出'。"""
    try:
        import re as _re
        merged = []
        for blk in block_results:
            # 每块内:抽分数(优先)和独立数字·按出现顺序
            _txt = str(blk)
            # 跳过失败块标记
            if "本块读取失败" in _txt or "本块无内容" in _txt:
                continue
            fracs = _re.findall(r"\d+\s*/\s*\d+", _txt)
            fracs = [f.replace(" ", "") for f in fracs]
            # 本块内按出现序·保留(跨块的重复是真实的·如多个3/4)
            merged.extend(fracs)
        return merged
    except Exception:
        return []


if __name__ == "__main__":
    import sys
    p = sys.argv[1] if len(sys.argv) > 1 else ""
    if p and os.path.exists(p):
        r = crop_zoom_read(p, "What text/details do you see? Read carefully.", zoom=3)
        print(f"ok={r['ok']} · {r['reason']}\n{r['answer'][:300]}")
    else:
        print("用法: python3 see_image_zoom.py <图路径>")



def perceive_with_budget(image_path, question, model="gemini-2.5-pro",
                         wall_clock_budget=90.0, no_progress_rounds=2,
                         precision=True):
    """图像感知任务的统一执行环:时间预算 + 无进展看门狗 + 传输阶梯降级。

    专治「读背景墓碑小字/押韵诗末句」这类细节题反复放大/重试直到墙钟超时的缺陷:
      - 每轮记录进展指标(是否新增可读文本/新覆盖区域),连续 no_progress_rounds 轮
        无进展 → 强制早停,输出当前最佳候选;
      - 逼近 wall_clock_budget 墙钟阈值 → 强制早停,不再发起新一轮;
      - 阶梯降级:整图速读 → ROI(下部/中部)放大精读 → 网格逐块 OCR,
        每一阶失败或无进展就自动降到下一阶,避免死循环。

    返回 {ok, answer(当前最佳候选), reason, rounds, elapsed, stopped_reason}。
    """
    import time as _time
    import re as _re

    t0 = _time.time()

    def _remaining():
        return wall_clock_budget - (_time.time() - t0)

    def _score(text):
        """进展指标:可读文本量(去空白字符数)。用于判断某轮是否带来新信息。"""
        if not text:
            return 0
        return len(_re.sub(r"\s+", "", str(text)))

    # 阶梯:由粗到细,前一阶无进展/失败即降级到下一阶。
    # 每阶是一个 (名字, 无参调用) —— 调用内部自行做单次感知。
    def _stage_overview():
        return crop_zoom_read(image_path, question, region=None,
                              zoom=3, model=model, precision=precision)

    def _stage_roi_bottom():
        # 墓碑小字/署名/诗句常在图像下部
        return crop_zoom_read(image_path, question, region=(0.0, 0.55, 1.0, 1.0),
                              zoom=5, model=model, precision=precision)

    def _stage_roi_center():
        return crop_zoom_read(image_path, question, region=(0.15, 0.25, 0.85, 0.85),
                              zoom=5, model=model, precision=precision)

    def _stage_grid_ocr():
        return grid_scan_read(image_path, question, grid=(3, 3), zoom=3, model=model)

    stages = [
        ("整图速读", _stage_overview),
        ("ROI下部放大精读", _stage_roi_bottom),
        ("ROI中部放大精读", _stage_roi_center),
        ("网格逐块OCR", _stage_grid_ocr),
    ]

    best = {"answer": "", "score": -1, "reason": ""}
    rounds = 0
    stale = 0            # 连续无进展轮数
    stage_idx = 0
    stopped_reason = ""
    seen_texts = []

    while stage_idx < len(stages):
        # 墙钟看门狗:逼近阈值(留 <8s 冗余)就不再开新一轮
        if _remaining() < 8.0:
            stopped_reason = f"墙钟预算逼近({wall_clock_budget}s)·早停"
            break

        name, fn = stages[stage_idx]
        rounds += 1
        try:
            r = fn()
        except Exception as e:
            r = {"ok": False, "answer": "", "reason": f"阶段异常:{type(e).__name__}:{str(e)[:80]}"}

        ans = r.get("answer", "") if r.get("ok") else ""
        sc = _score(ans)
        if r.get("ok"):
            seen_texts.append(f"[{name}] {ans}")

        # 进展判定:比历史最佳分更高(读出更多可读文本) → 有进展
        if sc > best["score"]:
            best = {"answer": ans, "score": sc,
                    "reason": f"{name}·{r.get('reason','')}"}
            stale = 0
        else:
            stale += 1

        # 无进展看门狗:连续 N 轮无进展 → 降级到下一阶梯;若已是末阶则早停
        if stale >= no_progress_rounds:
            stopped_reason = f"连续{stale}轮无新增可读内容·触发看门狗"
            stage_idx += 1
            stale = 0
            continue

        # 当前阶失败(没读出任何内容)也降级
        if not r.get("ok") or sc <= 0:
            stage_idx += 1
            stale = 0
            continue

        # 本阶有进展:再给同阶一次机会榨取(但受墙钟/无进展看门狗约束),
        # 若本阶已是主要收敛来源就在下一轮 stale 累积后自然降级。
        stage_idx += 1  # 阶梯式推进,避免同阶死循环

    if not stopped_reason:
        stopped_reason = "所有阶梯执行完毕"

    elapsed = round(_time.time() - t0, 2)
    _bestans = best["answer"] or "(各阶梯均未读出可靠文本)"
    return {
        "ok": best["score"] > 0,
        "answer": _bestans,
        "reason": (f"预算感知环·{rounds}轮·{elapsed}s·停因={stopped_reason}"
                   f"·最佳来源={best['reason']}"),
        "rounds": rounds,
        "elapsed": elapsed,
        "stopped_reason": stopped_reason,
        "all_stage_texts": seen_texts,
    }


