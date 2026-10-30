"""v13.88.18 · dead_link_gate · 404 死链检测 + 记账·别浪费资源重跑

用户 2026-07-24 抓:「一直 404 自己发现不了吗·不知道优化和记录吗」

3 层检测:
1. HTTP status 直接 200/404/403
2. 页面 title/body 里含「404」「不存在」「已被删除」等文案 (软 404 · SPA 常见)
3. URL 里含「/404」「/whoops」「error_code=300031」等重定向路径

记账:
- .dead_link_kb.json · {url: {first_seen, last_check, kind, count}}
- 见过 3 次死 → 30 天内不再重试

用法:
    from dead_link_gate import check_url_alive, mark_dead, is_known_dead
"""
import os
import json
import re
import subprocess
import time
from datetime import datetime


# v13.88.79 · 静默监控统一接入 · 用户 07-28「所有静默监控都到齐了吗」
def _alert(severity: str, msg: str):
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
        from alert_bus import notify as _n
        _n(_o.path.basename(__file__).replace(".py",""), severity, msg)
    except Exception:
        pass


V11 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEAD_KB = os.path.join(V11, ".dead_link_kb.json")


def _load_kb() -> dict:
    if not os.path.exists(DEAD_KB):
        return {}
    try:
        return json.load(open(DEAD_KB))
    except Exception:
        return {}


def _save_kb(kb: dict):
    try:
        with open(DEAD_KB, "w") as f:
            json.dump(kb, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# 404 文案 · 中英文 SPA 常见
_DEAD_PATTERNS_TEXT = [
    r"页面不存在", r"已被删除", r"抱歉[，,].{0,20}不存在",
    r"404[^\d]", r"Page Not Found", r"Not Found",
    r"当前笔记暂时无法浏览",
    r"该内容已删除", r"内容已被作者删除",
]

# URL 重定向路径 · 一看就死
_DEAD_URL_PATTERNS = [
    r"/404[/?]", r"/whoops", r"error_code=300031",
    r"error_code=404", r"redirectPath=.*404",
]


def is_known_dead(url: str, kb: dict = None, min_count: int = 2,
                    ttl_days: int = 30) -> bool:
    """3 天内确认 ≥2 次死 · 直接 skip 不再试"""
    kb = kb or _load_kb()
    rec = kb.get(url)
    if not rec:
        return False
    if rec.get("count", 0) < min_count:
        return False
    # ttl 内的
    try:
        last = datetime.fromisoformat(rec.get("last_check", ""))
        days = (datetime.now() - last).days
        return days < ttl_days
    except Exception:
        return True


def mark_dead(url: str, kind: str = "unknown", detail: str = ""):
    """记账·+1"""
    kb = _load_kb()
    rec = kb.get(url, {"first_seen": datetime.now().isoformat(),
                          "count": 0, "kind": kind})
    rec["count"] = rec.get("count", 0) + 1
    rec["last_check"] = datetime.now().isoformat()
    rec["kind"] = kind
    if detail:
        rec["detail"] = detail[:200]
    kb[url] = rec
    _save_kb(kb)


def check_url_alive(url: str, timeout: int = 10) -> dict:
    """3 层 · 检死 · 别浪费资源

    Returns:
    {
      "alive": bool,
      "kind": "url_pattern|http_status|body_text|alive",
      "http_status": int,
      "final_url": str (redirect 后)
      "reason": str
    }
    """
    # 1. URL 模式 · 一看就死
    for pat in _DEAD_URL_PATTERNS:
        if re.search(pat, url):
            return {"alive": False, "kind": "url_pattern",
                    "reason": f"url 含死链 pattern: {pat}"}

    # 2. HTTP HEAD + follow redirect
    try:
        r = subprocess.run(
            ["curl", "-sI", "-L", "-m", str(timeout),
             "-w", "\n%{http_code}|%{url_effective}\n",
             "-A", "Mozilla/5.0 (Macintosh)", url],
            capture_output=True, text=True, timeout=timeout + 5,
        )
        stdout = r.stdout or ""
        # 最后一行是 code|final_url
        lines = [l for l in stdout.strip().split("\n") if l]
        last = lines[-1] if lines else ""
        if "|" in last:
            code_str, final_url = last.split("|", 1)
            try:
                code = int(code_str)
            except Exception:
                code = 0
        else:
            code = 0
            final_url = url

        # 检 final_url 有没有跳到死链
        for pat in _DEAD_URL_PATTERNS:
            if re.search(pat, final_url):
                return {"alive": False, "kind": "redirect_to_dead",
                        "http_status": code, "final_url": final_url,
                        "reason": f"跳转到 {pat}"}

        if code == 404 or code == 410:
            return {"alive": False, "kind": "http_404",
                    "http_status": code, "final_url": final_url,
                    "reason": f"HTTP {code}"}
        if code == 403:
            # 403 有可能是反爬·不算死
            return {"alive": True, "kind": "403_maybe_antibot",
                    "http_status": code, "final_url": final_url,
                    "reason": "403 (可能反爬·让降级链试)"}
        if code >= 400:
            return {"alive": False, "kind": "http_err",
                    "http_status": code, "final_url": final_url,
                    "reason": f"HTTP {code}"}

        # 3. GET body · 找软 404 文案 (SPA 常见)
        r_get = subprocess.run(
            ["curl", "-s", "-L", "-m", str(timeout),
             "-A", "Mozilla/5.0 (Macintosh)", url],
            capture_output=True, text=True, timeout=timeout + 5,
        )
        body = r_get.stdout or ""
        # 只查 <title> + 前 3000 字 · 避免正文含 404 误判
        title_m = re.search(r"<title[^>]*>([^<]+)</title>", body, re.I)
        title = title_m.group(1) if title_m else ""
        head_body = body[:3000]
        check_text = title + "\n" + head_body

        for pat in _DEAD_PATTERNS_TEXT:
            m = re.search(pat, check_text)
            if m:
                return {"alive": False, "kind": "soft_404",
                        "http_status": code, "final_url": final_url,
                        "title": title[:100],
                        "reason": f"body/title 含: {m.group()[:50]}"}

        return {"alive": True, "kind": "alive",
                "http_status": code, "final_url": final_url}

    except Exception as e:
        # 挂了不算死 · 让降级链继续
        return {"alive": True, "kind": "check_err",
                "reason": f"{type(e).__name__}:{e}"}


if __name__ == "__main__":
    try:
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("cmd", choices=["check", "list", "stats"], default="check", nargs="?")
        p.add_argument("--url", default="")
        args = p.parse_args()
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise

    if args.cmd == "check" and args.url:
        r = check_url_alive(args.url)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        if not r["alive"]:
            mark_dead(args.url, r["kind"], r.get("reason", ""))
            print(f"[记账] mark_dead: {args.url}")
    elif args.cmd == "list":
        kb = _load_kb()
        print(f"死链库: {len(kb)} 条")
        for u, rec in sorted(kb.items(), key=lambda x: -x[1].get("count", 0))[:20]:
            print(f"  x{rec.get('count',0)} [{rec.get('kind','?')}] {u[:80]}")
    elif args.cmd == "stats":
        kb = _load_kb()
        from collections import Counter
        c = Counter(rec.get("kind", "?") for rec in kb.values())
        print(f"总 {len(kb)} 条·分布: {dict(c)}")
