#!/usr/bin/env python3
"""win_tactic_capture · 成功招法捕获 · 2026-08-13

用户意图:不该我手搓工具·是他的自主性——他自己从成功trace(vb_2靠"PIL切行+逐行OCR"
答对)提炼招法·自沉淀skill·同类够了自升工具。门槛降到1·每次成功都算·长进程持续积。

本模块=补缺的那一环:成功解题trace → 招法签名 + tool_seq → 写 .win_traces.jsonl。
下游 skill_from_win 再把它沉淀成 skill(纯招法·脱答案)。

**红线**(qa_auto实证:skill被load后agent会直接复述存的答案=背答案):
  只捕获【招法/工具序列/命令的方法特征】·**绝不存题目答案/gold值**。捕获前脱敏。

纯读+追加·fail-safe·不拖垮主流程。
"""
import os
import re
import json
import glob
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_V11 = os.path.dirname(_HERE)
_QA_CKPT = os.path.join(_V11, "..", "qa_system", "state", "auto_ckpt")
_WIN_TRACES = os.path.join(_V11, ".win_traces.jsonl")

# 招法特征:从 shell_exec 命令/工具序列里识别可复用的"解题动作模式"(语义·非硬编码单题)
_TACTIC_FEATURES = [
    ("row_slice_ocr", [r"PIL", r"Image\.open", r"\.crop", r"for y in", r"逐行", r"按行",
                       r"crop.*ocr", r"每一行"]),          # 切行逐行读(vb_2答对招法)
    ("grid_scan", [r"grid", r"网格", r"分块", r"tile"]),      # 网格分块扫
    ("code_compute", [r"sympy", r"Fraction", r"import math", r"sum\(", r"np\."]),  # 代码精算
    ("multi_model_vision", [r"gpt-5", r"gemini", r"claude.*看图"]),  # 多模型交叉读图
]


def _detect_tactics(tool_seq, shell_cmds) -> list:
    """从工具序列+shell命令里检测招法特征(可多个)。语义匹配·非硬编码答案。"""
    blob = " ".join(tool_seq) + "\n" + "\n".join(shell_cmds)
    low = blob.lower()
    found = []
    for name, pats in _TACTIC_FEATURES:
        if any(re.search(p, blob, re.I) for p in pats):
            found.append(name)
    return found


_ANSWER_LEAK_HINT = re.compile(r"\d+/\d+(,\s*\d+/\d+){2,}")  # 一串分数=疑似答案·脱敏


def _redact(text: str) -> str:
    """脱敏:去掉疑似答案(连续分数序列/长数字串)·只留方法描述。防背答案。"""
    if not text:
        return ""
    t = _ANSWER_LEAK_HINT.sub("[ANSWER_REDACTED]", str(text))
    return t[:200]


def capture_win(qid: str, gold: str = "", log_path: str = "",
                tool_log=None, ckpt_key: str = "") -> dict:
    """从某道答对题的 trace 抽招法·写 .win_traces.jsonl。
    数据源优先级:tool_log(调用方内存里的完整tool_log·最准·v99r自动接线主用)
    > log_path(完整log·若给) > auto ckpt(覆盖式·答对当下调才准)。
    ★ 关键:ckpt会被后续跑覆盖·所以生产里应【答对当下(hit==1)】就调·别事后补。
    ckpt_key: 找 ckpt 的文件名关键串(默认qid)。self_heal 复跑的 ckpt 按题文md5(tid)落盘
    与 GAIA qid 不同→调用方传 res.tid·否则 glob 不到 ckpt。
    只存 tool_seq + 招法签名 + 命令方法特征(脱敏)·不存答案。fail-safe。"""
    try:
        tool_seq = []
        shell_cmds = []
        # 源0:调用方直接给的完整 tool_log(答对当下从内存/归档ckpt传·不受ckpt覆盖影响)
        if tool_log:
            tool_seq = [e.get("tool") for e in tool_log if isinstance(e, dict) and e.get("tool")]
            for e in tool_log:
                if isinstance(e, dict) and e.get("tool") in ("shell_exec", "sympy_calc"):
                    a = e.get("args")
                    cmd = (a.get("command") or a.get("code") or a.get("question") or "") if isinstance(a, dict) else ""
                    shell_cmds.append(_redact(str(cmd))[:200])
        # 源1:log 文件(完整·从工具调用行+命令文本抽)
        elif log_path and os.path.exists(log_path):
            blob = open(log_path, encoding="utf-8", errors="ignore").read()
            tool_seq = re.findall(
                r"see_image_vlm|see_image_expert|see_image_zoom|read_image_ocr|"
                r"sympy_calc|shell_exec|finalize_answer|see_video_frame", blob)
            # 命令特征:直接从整段 log 里检测(已在 _detect_tactics 做)·脱敏抽样
            for m in re.finditer(r"(Image\.open|\.crop|for y in|PIL[^\n]{0,80})", blob):
                shell_cmds.append(_redact(m.group(0)))
        else:
            # 源2:auto ckpt(取最新·仅答对当下准)
            cands = glob.glob(os.path.join(_QA_CKPT, f"*{ckpt_key or qid}*.json"))
            cands = [c for c in cands if "stale" not in c and ".json" in c]
            if not cands:
                return {"ok": False, "reason": f"no ckpt/log for {qid}"}
            ckpt = max(cands, key=os.path.getmtime)
            d = json.load(open(ckpt, encoding="utf-8"))
            tl = d.get("tool_log") or []
            if not tl:
                return {"ok": False, "reason": "no tool_log in ckpt"}
            tool_seq = [e.get("tool") for e in tl if isinstance(e, dict) and e.get("tool")]
            for e in tl:
                if isinstance(e, dict) and e.get("tool") in ("shell_exec", "sympy_calc"):
                    a = e.get("args")
                    cmd = (a.get("command") or a.get("code") or a.get("question") or "") if isinstance(a, dict) else ""
                    shell_cmds.append(_redact(str(cmd))[:200])

        tactics = _detect_tactics(tool_seq, shell_cmds)
        # 招法签名:排序去重的招法组合(稳定key·供下游聚合同类)
        signature = "+".join(sorted(set(tactics))) or "generic"

        rec = {
            "ts": datetime.now().isoformat(),
            "qid": qid,
            "tactic_signature": signature,
            "tactics": tactics,
            "tool_seq": tool_seq[:40],
            "shell_feature_samples": shell_cmds[:5],   # 已脱敏
            "gold_redacted": True,                      # 明示:不存答案
        }
        # 去重:同 qid+signature 已在就不重复写
        seen = set()
        if os.path.exists(_WIN_TRACES):
            for l in open(_WIN_TRACES, encoding="utf-8"):
                if l.strip():
                    try:
                        o = json.loads(l)
                        seen.add((o.get("qid"), o.get("tactic_signature")))
                    except Exception:
                        pass
        if (qid, signature) in seen:
            return {"ok": True, "qid": qid, "tactics": tactics, "dedup": True,
                    "n_tools": len(tool_seq)}
        with open(_WIN_TRACES, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return {"ok": True, "qid": qid, "tactics": tactics,
                "signature": signature, "n_tools": len(tool_seq)}
    except Exception as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {str(e)[:120]}"}


# 招法→通用how_to(纯方法步骤·脱题目脱答案·description含触发词供agent skill_search匹配)
_TACTIC_SKILL = {
    "row_slice_ocr": {
        "desc": "读混排worksheet/表格/多行手写图时·别整图泛读(VLM会把行间、题目与答案混读)。"
                "触发词: worksheet 表格图 分数图 逐行 手写读数 多行混排 answer混排",
        "how_to": (
            "遇到【一张图混排文字/表格·尤其正文里嵌了分数、末尾有'sample problems'练习区】:\n"
            "1. shell_exec 整图 tesseract 先扫一遍拿正文——正文里明写的分数(印刷文本)tesseract 稳定读得出。\n"
            "   分数图实证:正文散落的 11 个分数(含'divisible/not divisible'那几例)整图OCR全读对。\n"
            "2. 末尾'sample problems'区往往是【图片渲染的分数】(不是文本)·tesseract 读成空·\n"
            "   → 必须 see_image_vlm 逐个读出每题的【分子/分母两个数】(别让VLM报'共几题'的计数)。\n"
            "3. ★关键分辨:sample problems 的 a/b 是【待化简的题目】·不是答案!\n"
            "   题目要求'化简/simplify'时·答案 = 对每题用 python Fraction 约分算出的结果·\n"
            "   例(与任何具体题无关的示范):题目 10/25 → Fraction(10,25)=2/5。\n"
            "   别把题目原式当答案抄(这是这类题最常见的错)。\n"
            "4. 拼接顺序 = 正文出现顺序的现成分数 + sample 各题化简后的答案。\n"
            "核心:①文本分数走OCR、图片分数走VLM ②区分'现成分数'vs'待化简题目' ③化简用Fraction算不靠读。\n"
            "已知边界:sample区是图片渲染·VLM 逐题读【题目数量】会波动(时读全时漏最后1题·分数图见7↔6)·\n"
            "   稳态做法=让VLM按'固定N格'逐格报分子分母(而非自由报列表)·或多次读投票取一致数量。"),
    },
    "grid_scan": {
        "desc": "读网格/表格类图·按网格分块逐格读。触发词: 网格 表格 分块 grid tile",
        "how_to": "把图按网格切成格子·逐格 see_image_zoom 精读·再按行列拼。",
    },
}


def skill_from_win(win_trace: dict) -> dict:
    """把一条成功招法沉淀成 skill(写到 skill_library 扫描的目录·agent能skill_search到)。
    只写【纯方法how_to】·脱敏守卫拦答案。门槛1:每次成功都可沉。fail-safe。
    ★ 不用 skill_synthesizer.write_skill——它写~/.meyo/skills·agent的skill_library扫的是
      v11/evolution/skills·两地不一致=写了扫不到。这里直写 library 目录。"""
    try:
        sig = win_trace.get("tactic_signature", "")
        if sig in ("generic", ""):
            return {"ok": False, "reason": "generic signature·无可复用招法·不沉淀"}
        tmpl = _TACTIC_SKILL.get(sig)
        if not tmpl:
            # 通用化(用户要求"尽量通用"):无预置模板的招法·也能沉成skill——
            #   用 trace 里的招法特征生成通用 how_to(描述这类题成功用了哪些工具组合)。
            #   只是不自动造工具(造工具需专门 executor 模板)·但招法经验不丢。
            _tacs = win_trace.get("tactics") or [sig]
            tmpl = {
                "desc": f"成功招法组合 [{'+'.join(_tacs)}]·从真实解题提炼。触发词: {' '.join(_tacs)}",
                "how_to": (f"这类题曾用【{'、'.join(_tacs)}】的工具组合成功解出。\n"
                           f"遇到相似题时·可参考按此思路:先感知/读取→按需用代码精算/切分→交叉验证。\n"
                           f"(具体步骤按题自定·这是从成功中提炼的通用打法·非固定脚本)"),
            }
        # 脱敏守卫:how_to/desc 不能含疑似答案(连续分数/长数字串)——防背答案
        for txt in (tmpl["how_to"], tmpl["desc"]):
            if _ANSWER_LEAK_HINT.search(txt):
                return {"ok": False, "reason": "REFUSED: 招法模板疑似含答案·拒写(防背答案)"}
        import skill_library as SL
        slug = f"win-tactic-{sig.replace('_', '-')}"
        skill_dir = os.path.join(SL.SKILLS_DIR, slug)
        skill_file = os.path.join(skill_dir, "SKILL.md")
        os.makedirs(skill_dir, exist_ok=True)
        # 不臃肿:同签名 skill 只一条·多次成功=升级它(计数累积体现复用强度)·不新建重复文件
        existed = os.path.exists(skill_file)
        _prev = 0
        if existed:
            try:
                _m = re.search(r"learned_from_wins:\s*(\d+)", open(skill_file, encoding="utf-8").read())
                _prev = int(_m.group(1)) if _m else 0
            except Exception:
                _prev = 0
        _wins = _prev + 1
        content = f"""---
name: {slug}
description: {tmpl['desc']}
category: vision-learned
metadata:
  created_at: {datetime.now().isoformat()}
  created_by: win_tactic_capture(成功招法自沉淀·升级式累积)
  tactic_signature: {sig}
  learned_from_wins: {_wins}
---

# {slug}

## 为什么需要这个
从真实成功解题中提炼的招法(不是答案·是方法)·已复用成功 {_wins} 次。适用触发场景见 description 触发词。

## 应对流程
{tmpl['how_to']}

## 注意
本 skill 只提供【方法】·不含任何具体题目的答案——遇到新题按方法自己读、自己算。
"""
        with open(skill_file, "w", encoding="utf-8") as f:
            f.write(content)
        return {"ok": True, "skill": slug, "path": skill_file,
                "action": "upgraded" if existed else "created",
                "learned_from_wins": _wins, "signature": sig}
    except Exception as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {str(e)[:120]}"}


def signature_counts() -> dict:
    """统计各招法签名在多少个不同题里成功过(供tool_forge判≥N升工具)。"""
    by_sig = {}
    try:
        if os.path.exists(_WIN_TRACES):
            for l in open(_WIN_TRACES, encoding="utf-8"):
                if l.strip():
                    o = json.loads(l)
                    sig = o.get("tactic_signature", "generic")
                    by_sig.setdefault(sig, set()).add(o.get("qid"))
    except Exception:
        pass
    return {k: len(v) for k, v in by_sig.items()}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="成功招法捕获")
    ap.add_argument("--qid")
    ap.add_argument("--log", default="", help="完整log文件路径(优先·ckpt会被覆盖)")
    ap.add_argument("--counts", action="store_true")
    ap.add_argument("--skill", action="store_true", help="把最新win_traces沉淀成skill")
    a = ap.parse_args()
    if a.counts:
        print(json.dumps(signature_counts(), ensure_ascii=False, indent=2))
    elif a.skill:
        # 把 win_traces 里每条(去重签名)沉淀成 skill
        seen_sig = set(); out = []
        if os.path.exists(_WIN_TRACES):
            for l in open(_WIN_TRACES, encoding="utf-8"):
                if l.strip():
                    wt = json.loads(l)
                    sig = wt.get("tactic_signature")
                    if sig in seen_sig:
                        continue
                    seen_sig.add(sig)
                    out.append(skill_from_win(wt))
        print(json.dumps(out, ensure_ascii=False, indent=2))
    elif a.qid:
        print(json.dumps(capture_win(a.qid, log_path=a.log), ensure_ascii=False, indent=2))
    else:
        ap.print_help()
