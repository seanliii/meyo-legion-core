#!/usr/bin/env python3
"""session_store · 2026-08-12 · 通用化 Phase4 · 多轮会话记忆

用户方向:真实用户输入残缺、指代、依赖上下文("那第二个呢")·GAIA是单轮自足。
诊断:前三稳了再动·别和接待层同时动。

好消息:内核 run_auto_agent 已支持多轮(conversation_history 参数·消费最近6轮)——
Phase4 不碰内核多轮机制·只在 run_task 层管:
  ① 会话存储(按 session_id 存/取多轮)
  ② 指代消解(resolve_reference:"那第二个呢"+上文 → 自足问题)
  ③ 透传 conversation_history 给内核(复用已有多轮)

GAIA(无 session_id)→单轮·conversation_history=None·行为不变(165等价严格不破)。
全在应用层·纯合同+管道·fail-safe(会话失败不阻断交付)。
"""
import os
import sys
import json
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_SEED = os.path.dirname(os.path.dirname(_HERE))
_SESS_DIR = os.path.join(_SEED, "v11", ".sessions")

# 指代词(有这些+有history=可能需要指代消解)
_REFERENCE_MARKERS = ("那", "这", "它", "上面", "上述", "刚才", "第二个", "第三个", "另一个",
                      "第一个", "前面", "继续", "接着", "再", "them", "it", "that", "the second",
                      "the first", "the other", "above")


def _safe_sid(session_id):
    return "".join(c if (c.isalnum() or c in "_-") else "_" for c in str(session_id or "default"))[:64]


def _sess_path(session_id):
    return os.path.join(_SESS_DIR, f"{_safe_sid(session_id)}.jsonl")


def append_turn(session_id, question, answer):
    """存一轮到会话历史。fail-safe(存失败不阻断交付)。"""
    if not session_id:
        return False
    try:
        os.makedirs(_SESS_DIR, exist_ok=True)
        from datetime import datetime
        ts = ""
        try:
            ts = datetime.now().isoformat()
        except Exception:
            pass
        with open(_sess_path(session_id), "a", encoding="utf-8") as f:
            f.write(json.dumps({"question": str(question)[:1000],
                                "final": str(answer)[:2000], "ts": ts},
                               ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def load_history(session_id, max_turns=6):
    """取最近 max_turns 轮 [{question, final}](内核 conversation_history 格式)。fail-safe。"""
    if not session_id:
        return []
    try:
        p = _sess_path(session_id)
        if not os.path.exists(p):
            return []
        turns = []
        for l in open(p, encoding="utf-8"):
            if l.strip():
                try:
                    d = json.loads(l)
                    turns.append({"question": d.get("question", ""), "final": d.get("final", "")})
                except Exception:
                    pass
        return turns[-max_turns:]
    except Exception:
        return []


def _has_reference(text):
    """粗判 raw_input 是否含指代(需要上文才自足)。纯字符串·不调LLM。"""
    t = str(text or "")
    return any(m in t for m in _REFERENCE_MARKERS) and len(t) < 200  # 短且带指代词=大概率依赖上文


def resolve_reference(raw_input, history):
    """指代消解:raw_input 含指代且有 history → LLM 解成自足问题。
    ("那第二个呢" + 上文"推荐了3个工具" → "第二个推荐的工具的详情")。
    无 history / 无指代 / LLM 挂 → 原样返回(fail-safe·不阻断)。"""
    if not history or not _has_reference(raw_input):
        return raw_input
    try:
        sys.path.insert(0, _HERE)
        from qa_clarify import _call_llm  # 复用现成 LLM 通道
        hist_str = "\n".join(f"轮{i+1} 问:{t.get('question','')[:120]} 答:{t.get('final','')[:150]}"
                             for i, t in enumerate(history[-3:]))
        prompt = f"""用户在多轮对话中·这轮的问法依赖上文(含指代如"那/第二个/它")。
把这轮问题改写成【不依赖上文也能独立理解】的自足问题(解开指代)。

# 最近几轮上下文
{hist_str}

# 这轮(依赖上文·需解开指代)
{raw_input}

# 输出严格JSON
{{"resolved": "解开指代后的自足问题"}}
只输出JSON。"""
        raw = _call_llm("longcat-2.0", prompt, timeout=25)
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            d = json.loads(m.group(0))
            r = str(d.get("resolved", "")).strip()
            if r:
                return r
    except Exception:
        pass
    return raw_input


if __name__ == "__main__":
    # 自测(用临时会话目录·不污染)
    import tempfile
    _SESS_DIR = tempfile.mkdtemp()
    sid = "test_sess"
    append_turn(sid, "推荐3个用户调研工具", "1.Maze 2.Dovetail 3.UserTesting")
    append_turn(sid, "第一个怎么用", "Maze用法...")
    h = load_history(sid)
    assert len(h) == 2 and h[0]["question"] == "推荐3个用户调研工具", "会话往返"
    print("✅ 会话存储:存2轮取2轮·顺序对")
    # 指代判定(纯逻辑)
    assert _has_reference("那第二个呢") is True, "含指代词该判True"
    assert _has_reference("What is the area of the green polygon in the file") is False, "GAIA长自足问题无指代"
    print("✅ 指代判定:'那第二个呢'→需消解·GAIA自足问题→不需")
    # 无history原样返回(fail-safe)
    assert resolve_reference("那第二个呢", []) == "那第二个呢", "无history原样返回"
    print("✅ 指代消解:无history原样返回(fail-safe)")
    # 有history的消解(调LLM)
    r = resolve_reference("那第二个呢", h)
    print("✅ 指代消解(LLM):", r[:60])
    print("\n✅✅ session_store 全通(会话往返+指代判定+消解fail-safe)")
