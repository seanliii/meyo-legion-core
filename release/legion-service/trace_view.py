#!/usr/bin/env python3
"""
trace_view · 真 trace 查看器(第④批)

用户要"能查看 trace 文件":打开某家 agent 真实跑过的 ckpt,还原它的完整过程——
每一步工具调用(名+入参摘要+返回摘要+耗时)、访问过的每个网页 URL(可点)、思考/计划链。
替掉前端写死的 TRACES 演示样例,让用户看真过程,不是看假的。

安全:只放行 ~/.meyo 与本仓库内的 ckpt 路径(防路径穿越读任意文件)。
"""
import json
import os

_HOME = os.path.expanduser("~")
_ALLOW_PREFIXES = [
    os.path.join(_HOME, ".meyo"),
    os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")),  # 仓库根
]


def _path_allowed(path: str) -> bool:
    ap = os.path.abspath(path)
    return any(ap.startswith(os.path.abspath(p)) for p in _ALLOW_PREFIXES)


def read_trace(path: str) -> dict:
    """读一个 ckpt,还原成前端可渲染的过程流水。

    返回 {ok, qid, question, final, status, steps[], urls[], plan, error}
      steps: [{t, tool, summary, args_preview, elapsed_s}]  —— t=tool/reason
      urls:  [{title, url}]  —— agent 访问/引用过的网页(可点)
    """
    if not path:
        return {"ok": False, "error": "trace_path 为空"}
    if not _path_allowed(path):
        return {"ok": False, "error": "路径不在允许范围(仅 ~/.meyo 与仓库内)"}
    if not os.path.isfile(path):
        return {"ok": False, "error": f"trace 文件不存在: {path}"}
    try:
        d = json.load(open(path, "r", encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "error": f"trace 解析失败: {e}"}

    steps = []
    for r in (d.get("tool_log") or []):
        if not isinstance(r, dict):
            continue
        args = r.get("args") or {}
        args_preview = ""
        if isinstance(args, dict):
            # 取最能代表这步在干嘛的字段(query/question/url…),不整包倒出
            for k in ("query", "question", "url", "q", "keyword", "target"):
                if args.get(k):
                    args_preview = str(args[k])[:160]
                    break
            if not args_preview and args:
                args_preview = json.dumps(args, ensure_ascii=False)[:160]
        steps.append({
            "t": "tool",
            "tool": r.get("tool") or "?",
            "summary": str(r.get("summary") or "")[:400],
            "args_preview": args_preview,
            "elapsed_s": r.get("elapsed_s"),
        })

    # 访问过/引用的网页:evidence_digest.preview + evidence_anchors 汇总去重
    urls, seen = [], set()
    ed = d.get("evidence_digest") or {}
    for it in (ed.get("preview") or []):
        u = (it or {}).get("url")
        if u and u not in seen:
            seen.add(u)
            urls.append({"title": (it.get("title") or u)[:120], "url": u})
    for a in (d.get("evidence_anchors") or []):
        u = a.get("url") if isinstance(a, dict) else None
        if u and u not in seen:
            seen.add(u)
            urls.append({"title": (a.get("title") or u)[:120] if isinstance(a, dict) else u, "url": u})

    plan = d.get("plan")
    if isinstance(plan, (dict, list)):
        plan = json.dumps(plan, ensure_ascii=False)[:1200]

    return {
        "ok": True,
        "qid": d.get("qid"),
        "question": d.get("question") or d.get("goal"),
        "final": str(d.get("final") or "")[:2000],
        "status": d.get("status"),
        "steps": steps,
        "urls": urls,
        "plan": plan,
        "n_steps": len(steps),
        "n_urls": len(urls),
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(json.dumps(read_trace(sys.argv[1]), ensure_ascii=False, indent=1))
