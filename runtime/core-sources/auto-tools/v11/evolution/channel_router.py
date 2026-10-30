#!/usr/bin/env python3
"""channel_router · v13.88.9 · 2026-07-30

用户 2026-07-30 问:「(直连)只能 GitHub?靠多少规则?能不能抓更多网站?」

背景:系统里"哪个站走哪个通道(raw/API/curl/CDP)"的知识已存在·
但散在 global_sources.py 1400 行各处·没有统一入口。本组件把它收成
一张【可增长的路由表】:给任意 URL → 返回最优通道 + 具体拼法。

为什么这么做(而不是"只给 GitHub 加 raw"):
- raw 直连稳的本质 = 选了纯文本 URL(无反爬)+ 走 HTTP(不碰 CDP 卡顿)
- 这套规律不止 GitHub:任何有 API/raw/静态镜像的站都能免 CDP
- 规则表可增长·抓到新站就加一行·不用改抓取逻辑

通道优先级(高→低·守 [[shrimp-v13.86-xhs-meituan-api]] 官方API优先降CDP):
  raw  : 纯文本原文(github raw · 最稳·0 反爬)
  api  : 官方 API(github/zhihu/hn/掘金/b站 · 秒返·结构化)
  curl : 直连 HTML(少数派/sogou · 加 UA 池)
  cdp  : 真开浏览器(google/reddit/知乎登录态 · 最重·最后手段)

用法:
    from channel_router import route
    r = route("https://github.com/affaan-m/ECC")
    # → {"channel":"raw", "url":"https://raw.githubusercontent.com/affaan-m/ECC/main/README.md", ...}
"""
import re
from urllib.parse import urlparse

# ─────────────────────────────────────────────────────────────
# 路由规则表 · 每条: (域名匹配, 通道, URL 改写函数 or None, 备注)
# 抓到新站免 CDP 通道 → 往这里加一行即可
# ─────────────────────────────────────────────────────────────

def _github_raw(u: str) -> str:
    """github.com/{owner}/{repo}[/...] → raw README 或 raw 文件"""
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)", u)
    if m:  # 具体文件 blob 链接
        owner, repo, branch, path = m.groups()
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/?$", u)
    if m:  # 仓库根 → 试 main 分支 README
        owner, repo = m.group(1), m.group(2).replace(".git", "")
        return f"https://raw.githubusercontent.com/{owner}/{repo}/main/README.md"
    return u


# 域名 → 通道规则。按顺序匹配·第一个命中生效
RULES = [
    # (正则匹配 host+path, channel, rewrite_fn, 备注)
    (r"github\.com/[^/]+/[^/]+",        "raw",  _github_raw, "GitHub 仓库/文件 → raw 纯文本·最稳"),
    (r"raw\.githubusercontent\.com",    "raw",  None,        "已是 raw·直接抓"),
    (r"gist\.github\.com",              "raw",  None,        "gist raw"),
    (r"api\.github\.com",               "api",  None,        "GitHub 官方 API"),
    (r"news\.ycombinator\.com|hn\.algolia",  "api", None,    "HN → Algolia API 直连"),
    (r"zhihu\.com",                     "api",  None,        "知乎 → 官方 API(需 SECRET)·降 CDP"),
    (r"juejin\.cn|juejin\.im",          "api",  None,        "掘金直连 API"),
    (r"bilibili\.com|b23\.tv",          "api",  None,        "B站直连 API"),
    (r"artificialanalysis\.ai",         "cdp",  None,        "AA评测榜=SPA·curl只拿1030字空壳(全导航链接·08-02实测)·榜单JS渲染·需CDP"),
    (r"swebench\.com|swe-bench|terminal-bench|gaia-benchmark", "cdp", None, "评测榜多为SPA·JS渲染·curl拿空壳·需CDP"),
    (r"sspai\.com/post/",               "cdp",  None,        "少数派文章=SPA·curl只拿空壳(07-30实测2679字骨架)·需渲染"),
    (r"sspai\.com",                     "curl", None,        "少数派列表/首页 curl 可(详情页走上面 cdp)"),
    (r"weixin\.sogou\.com",             "curl", None,        "Sogou 微信直连列表"),
    (r"bing\.com/search|cn\.bing\.com", "curl", None,        "Bing 搜索直连·实测通(07-30·google挡但bing通)"),
    (r"raw\.|\.md$|\.txt$|\.json$",     "curl", None,        "纯文本资源·curl 直取"),
    # ↓ 这些无免 CDP 通道·必须真开浏览器
    (r"google\.com/search|/sorry",      "cdp",  None,        "Google 搜索·纯HTTP必挡(07-30实测返错误页)·必 CDP"),
    (r"reddit\.com",                    "cdp",  None,        "Reddit·需翻墙 CDP"),
    (r"v2ex\.com",                      "cdp",  None,        "V2EX·境内被墙走 CDP"),
    (r"twitter\.com|x\.com",            "cdp",  None,        "X·需登录态 CDP"),
    (r"xiaohongshu\.com|xhs\.",         "cdp",  None,        "小红书·API 优先失败降 CDP"),
]

# 通道优先级(越小越优先·用于"未知站先试哪个")
CHANNEL_RANK = {"raw": 0, "api": 1, "curl": 2, "cdp": 3}


# v13.88.9 · 学习层:抓取失败→搜外部→校验→回写这里·下次直接用(闭环落到"学会")
import os as _os
import json as _json
_LEARNED_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                              "..", ".channel_router_learned.json")


def _load_learned() -> list:
    """学到的规则:[{host_pat, channel, note, ts, source}] · 先于硬编 RULES 匹配"""
    try:
        p = _os.path.abspath(_LEARNED_FILE)
        if _os.path.exists(p):
            return _json.load(open(p, encoding="utf-8"))
    except Exception:
        pass
    return []


def learn_rule(host_pat: str, channel: str, note: str, source: str = "auto") -> bool:
    """回写一条学到的通道规则(闭环终点)· 幂等:同 host_pat 覆盖
    channel: raw/api/curl/cdp · host_pat: 域名正则片段(如 'v2ex\\.com')"""
    if channel not in CHANNEL_RANK:
        return False
    learned = _load_learned()
    learned = [r for r in learned if r.get("host_pat") != host_pat]  # 去重覆盖
    learned.append({"host_pat": host_pat, "channel": channel,
                    "note": note[:120], "source": source})
    try:
        p = _os.path.abspath(_LEARNED_FILE)
        _json.dump(learned, open(p, "w", encoding="utf-8"),
                   ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def route(url: str) -> dict:
    """给一个 URL·返回最优通道决策
    {channel, url(可能改写), note, matched}
    优先级:学到的规则 > 硬编 RULES > 未知站默认 curl
    """
    if not url or not url.startswith("http"):
        return {"channel": "curl", "url": url, "note": "非法/相对 URL·默认 curl", "matched": False}

    try:
        parsed = urlparse(url)
        hostpath = (parsed.netloc + parsed.path).lower()
    except Exception:
        hostpath = url.lower()

    # ① 学到的规则先匹配(闭环:失败→搜→校验→回写→这里生效)
    for lr in _load_learned():
        try:
            if re.search(lr["host_pat"], hostpath):
                return {"channel": lr["channel"], "url": url,
                        "note": f"[学到] {lr.get('note','')}", "matched": True,
                        "learned": True}
        except Exception:
            continue

    # ② 硬编 RULES
    for pat, channel, rewrite, note in RULES:
        if re.search(pat, hostpath):
            new_url = rewrite(url) if rewrite else url
            return {"channel": channel, "url": new_url, "note": note, "matched": True}

    # ③ 未知站:默认先 curl 直连(比无脑 CDP 省)·调用方失败可降级
    return {"channel": "curl", "url": url,
            "note": "未知站·先 curl 直连(免 CDP)·失败再降 CDP", "matched": False}


def stats() -> dict:
    """规则表统计·答用户『多少规则』"""
    from collections import Counter
    c = Counter(ch for _, ch, _, _ in RULES)
    return {"total_rules": len(RULES), "by_channel": dict(c),
            "free_of_cdp": sum(1 for _, ch, _, _ in RULES if ch != "cdp"),
            "need_cdp": c.get("cdp", 0)}


if __name__ == "__main__":
    import sys, json
    if len(sys.argv) > 1 and sys.argv[1] == "--stats":
        print(json.dumps(stats(), ensure_ascii=False, indent=2))
    elif len(sys.argv) > 1:
        print(json.dumps(route(sys.argv[1]), ensure_ascii=False, indent=2))
    else:
        # demo:几个真实 URL
        print("═══ channel_router demo ═══")
        for u in [
            "https://github.com/affaan-m/ECC",
            "https://github.com/colbymchenry/codegraph/blob/main/install.sh",
            "https://news.ycombinator.com/item?id=123",
            "https://sspai.com/post/12345",
            "https://www.google.com/search?q=test",
            "https://某个没见过的站.com/article/1",
        ]:
            r = route(u)
            print(f"[{r['channel']}] {u[:50]}")
            print(f"    → {r['url'][:70]}")
            print(f"    ({r['note']})")
        print("\n规则表:", json.dumps(stats(), ensure_ascii=False))
