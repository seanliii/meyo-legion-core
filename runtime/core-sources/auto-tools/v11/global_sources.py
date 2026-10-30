#!/usr/bin/env python3
"""
v13.18 · global_sources · 全网真源搜索
────────────────────────────────────────────────────────────
用户诉求 (2026-07-09): "你可以搜搜 google 外网各种论坛 内容 reddit 这些
都可以还有 github,不止小红书那些"

现状: agent_search 主源只有 Bing CN/EN + Sogou 微信·太窄
本模块补齐:
- Hacker News (Algolia) · ✅ 直连通 · 1.5s
- GitHub Issues/Discussions · ✅ 直连通 · 0.9s
- 少数派 · ✅ 直连通 · 0.6s
- Reddit · ✅ CDP 通 (Chrome 翻墙)
- v2ex · ✅ CDP 通 (境内直连被墙但 CDP 走)
- ProductHunt · CDP 走
- Zhihu · CDP 走 (Chrome 已登录)
- Xiaohongshu · CDP 走 (Chrome 已登录)
- Weixin 公众号 · CDP 走 (待用户 Chrome 登录)
- Sogou 微信搜索 · ✅ 直连通 · 1-2s · top 3 走 read_url 抓公众号全文 (v13.84)

统一返回格式: [{'title', 'url', 'content', 'source', 'meta'}]
每个源都是独立可 fail-safe · 一个源挂了不影响其他源
"""
import os, sys, json, re, time
import subprocess
from urllib.parse import quote_plus
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
V11 = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(V11, "evolution"))

# v13.87 · 自动加载 .env · ZHIHU_ACCESS_SECRET 等 secret 生效
try:
    from env_loader import load_env
    load_env()
except Exception:
    pass

# v13.87 · 通用装饰器 · 所有 search_xxx 都能挂 health + completeness + cache
try:
    from fetcher_decor import instrument
except Exception:
    # 兜底 · 装饰器挂了不影响主流程
    def instrument(name, cache_min_len=200):
        def deco(fn):
            return fn
        return deco


UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"


# ═══════════════════════════════════════════════════════════════
# v13.88.9g · 退役死站短路 · 用户 07-31「天天撞死站刷屏」
# sspai 搜索接口下线 (/api/v1/search/page/get → 404·实测)
# v2ex API 境内 HTTP=000·CDP 也拿不到内容 (实测 LEN=0)
# 这两个源函数不删 (用户规则:没说删的别删)·只让入口优雅短路+记一次日志
# ═══════════════════════════════════════════════════════════════
_RETIRED_SOURCES = {
    "sspai": "sspai 搜索接口已下线 (/api/v1/search/page/get → 404·2026-07-31 实测)",
    "v2ex": "v2ex API 境内 HTTP=000·CDP 也拿不到内容 (2026-07-31 实测)",
}
_RETIRED_LOGGED = set()  # 每源只记一次·避免刷屏


def _retired_shortcircuit(name: str) -> bool:
    """退役源短路 · 只在首次调用时记一次日志·返回 True 表示应短路"""
    if name not in _RETIRED_SOURCES:
        return False
    if name not in _RETIRED_LOGGED:
        _RETIRED_LOGGED.add(name)
        reason = _RETIRED_SOURCES[name]
        # 记进 dead_link_gate 的 KB (幂等·count 累加·30 天内不重试)·并 info 级日志
        try:
            sys.path.insert(0, os.path.join(V11, "evolution"))
            from dead_link_gate import mark_dead
            mark_dead(f"source://{name}", kind="retired", detail=reason)
        except Exception:
            pass
        print(f"[retired] {name} 搜索源已退役·短路返空: {reason}")
    return True


def _curl_json(url: str, timeout: int = 10) -> dict:
    """通用 curl JSON · fail-safe 返 {}"""
    try:
        r = subprocess.run(
            ["curl", "-sSL", "--max-time", str(timeout), "-A", UA,
             "-H", "Accept: application/json", url],
            capture_output=True, text=True, timeout=timeout + 2,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return {}
        return json.loads(r.stdout)
    except Exception:
        return {}


def _curl_text(url: str, timeout: int = 10) -> str:
    """通用 curl 文本 · fail-safe 返 空串"""
    try:
        r = subprocess.run(
            ["curl", "-sSL", "--max-time", str(timeout), "-A", UA, url],
            capture_output=True, text=True, timeout=timeout + 2,
        )
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════
# 1 · Hacker News (Algolia · 直连) ⭐⭐⭐
# ═══════════════════════════════════════════════════════════════

@instrument("hackernews")
def search_hackernews(query: str, limit: int = 8) -> list:
    """HN 全文搜索 · Algolia API 免登录·1-2s
    返回帖 + 点数 + 时间 · 都是真实用户讨论"""
    url = f"https://hn.algolia.com/api/v1/search?query={quote_plus(query)}&hitsPerPage={limit}"
    data = _curl_json(url, timeout=8)
    hits = data.get("hits", []) or []
    results = []
    for h in hits:
        title = h.get("title") or h.get("story_title") or ""
        url_ = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID','')}"
        # content = title + comment text 摘要
        content_parts = []
        if h.get("story_text"):
            content_parts.append(h["story_text"][:800])
        if h.get("comment_text"):
            content_parts.append(h["comment_text"][:600])
        content = " · ".join(content_parts) or title
        results.append({
            "title": title[:150],
            "url": url_,
            "content": content[:2000],
            "source": "hackernews",
            "meta": {
                "points": h.get("points", 0),
                "num_comments": h.get("num_comments", 0),
                "author": h.get("author", ""),
                "created_at": h.get("created_at", ""),
            },
        })
    return results


# ═══════════════════════════════════════════════════════════════
# 2 · GitHub Issues / Discussions (直连) ⭐⭐⭐
# ═══════════════════════════════════════════════════════════════

@instrument("github_issues")
def search_github_issues(query: str, limit: int = 6) -> list:
    """GitHub Issues 搜索 · 真实开发者踩坑记录
    不用 token 也能搜·限速 10 rpm"""
    # 优先搜 open + comments>3 (有讨论的·信号密度高)
    url = (f"https://api.github.com/search/issues"
           f"?q={quote_plus(query)}+is:issue+comments:%3E3"
           f"&per_page={limit}&sort=reactions&order=desc")
    data = _curl_json(url, timeout=10)
    items = data.get("items", []) or []
    results = []
    for it in items:
        title = it.get("title", "")
        body = (it.get("body") or "")[:1500]
        results.append({
            "title": title[:150],
            "url": it.get("html_url", ""),
            "content": body if body else title,
            "source": "github_issues",
            "meta": {
                "comments": it.get("comments", 0),
                "reactions": it.get("reactions", {}).get("total_count", 0) if it.get("reactions") else 0,
                "state": it.get("state", ""),
                "repo": (it.get("repository_url") or "").split("/")[-1],
            },
        })
    return results


@instrument("github_repos")
def search_github_repos(query: str, limit: int = 5) -> list:
    """GitHub Repos 搜索 · 找具体工具/项目"""
    url = (f"https://api.github.com/search/repositories"
           f"?q={quote_plus(query)}&per_page={limit}&sort=stars&order=desc")
    data = _curl_json(url, timeout=10)
    items = data.get("items", []) or []
    results = []
    for it in items:
        results.append({
            "title": it.get("full_name", ""),
            "url": it.get("html_url", ""),
            "content": (it.get("description") or "")[:500],
            "source": "github_repos",
            "meta": {
                "stars": it.get("stargazers_count", 0),
                "language": it.get("language", ""),
                "updated": it.get("updated_at", ""),
            },
        })
    return results


# ═══════════════════════════════════════════════════════════════
# 3 · 少数派 (直连 · HTML 抽卡片) ⭐⭐ 国内高质
# ═══════════════════════════════════════════════════════════════

@instrument("sspai")
def search_sspai(query: str, limit: int = 5) -> list:
    """少数派站内搜索 · SPA 走 CDP
    v13.18 · sspai 是 SPA · curl 拿到只有 <div id="app"> · 必须 CDP 渲染
    v13.88.9g · 已退役: 搜索接口 /api/v1/search/page/get → 404 (2026-07-31 实测)·入口短路"""
    if _retired_shortcircuit("sspai"):
        return []
    from web_access_bridge import WebAccess
    wa = WebAccess()
    if not wa.cdp_available():
        return []
    url = f"https://sspai.com/search/post/{quote_plus(query)}"
    text = wa.cdp_navigate_and_read(url, wait_ms=4000)
    if not text or len(text) < 200:
        return []
    # sspai 搜索页每卡片有: 标题·作者·时间·点赞收藏数
    # 特征: 标题一般 10-60 字·后跟"· <作者名> · <XX 前发布>"
    results = []
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    seen = set()
    for i, line in enumerate(lines):
        # 匹配标题特征: 长度合适 · 含中文 · 不是导航
        if 10 <= len(line) <= 100 and re.search(r'[一-龥]', line) \
                and not any(k in line for k in ["首页", "登录", "注册", "少数派",
                                                  "会员", "APP", "分类",
                                                  "沪ICP", "Copyright"]):
            if line not in seen:
                results.append({
                    "title": line[:150],
                    "url": url,
                    "content": line,
                    "source": "sspai",
                    "meta": {},
                })
                seen.add(line)
        if len(results) >= limit:
            break
    return results[:limit]


# ═══════════════════════════════════════════════════════════════
# 4 · Reddit (需 CDP · Chrome 翻墙) ⭐⭐⭐
# ═══════════════════════════════════════════════════════════════

@instrument("reddit")
def search_reddit(query: str, limit: int = 6, subreddits: list = None) -> list:
    """Reddit · v13.28.1 · 用户 xhs 频控事件后同步预防
    - 加 5min 冷却
    - 若 subreddits 指定 · 读该 sub 的 /top/?t=week (真人常用)
    - 否则读全站搜索"""
    from web_access_bridge import WebAccess
    wa = WebAccess()
    if not wa.cdp_available():
        return []

    # v13.28.1 · 冷却
    global _LAST_REDDIT_CALL_AT
    now_ts = time.time()
    last = globals().get("_LAST_REDDIT_CALL_AT", 0)
    if now_ts - last < 300:
        wait_left = int(300 - (now_ts - last))
        return [{"title": f"[reddit 冷却中·还需 {wait_left}s]", "url": "",
                 "content": "", "source": "reddit",
                 "meta": {"cooldown": True}}]
    globals()["_LAST_REDDIT_CALL_AT"] = now_ts

    if subreddits:
        results = []
        for sr in subreddits[:3]:
            u = f"https://www.reddit.com/r/{sr}/search/?q={quote_plus(query)}&restrict_sr=1"
            text = wa.cdp_navigate_and_read(u, wait_ms=5000)
            if text:
                for m in re.finditer(r'(r/\w+)\s*\n[·•]\s*.*?ago\s*\n(.+?)(?=\n\s*Share|\n\s*Report|\Z)',
                                     text, re.S):
                    title = m.group(2).strip()[:200]
                    if 10 < len(title) < 300:
                        results.append({
                            "title": title, "url": u, "content": title,
                            "source": "reddit", "meta": {"subreddit": m.group(1)},
                        })
            if len(results) >= limit:
                break
        return results[:limit]

    # 全站搜索
    u = f"https://www.reddit.com/search/?q={quote_plus(query)}&sort=top"
    text = wa.cdp_navigate_and_read(u, wait_ms=6000)
    if not text:
        return []
    # v13.18 · Reddit 新版每帖 innerText 格式实测:
    #   <title>\n r/<sub>\n · \n <time> ago\n <title 再出现>\n <K> votes·<K> comments
    # 用"K votes" 或 "votes·...comments" 做锚点·往前抽标题
    results = []
    seen_titles = set()
    lines = text.split("\n")
    for i, line in enumerate(lines):
        line = line.strip()
        # 匹配 vote 行 · 如 "40K votes·1.4K comments" 或 "5 votes"
        if re.match(r'^[\d.]+[KMB]?\s+votes[·•]', line) or re.match(r'^\d+\s+votes\b', line):
            # 往前找标题 · 特征: 上一个非空行(即 title 再出现)前面还有一行 r/xxx
            # 或前 2-4 行里最长的一行是 title
            for back in range(1, 6):
                if i - back < 0: break
                cand = lines[i - back].strip()
                # 跳过 r/xxx 行·跳过 时间行 (如 "5mo ago")
                if not cand: continue
                if cand.startswith("r/"): continue
                if re.match(r'^·|^\d+[hmdy]?\s*ago', cand): continue
                # 找到候选 title
                if 10 < len(cand) < 300 and cand not in seen_titles:
                    # 抓 subreddit (向前找)
                    sub = ""
                    for b2 in range(back, back + 5):
                        if i - b2 < 0: break
                        cd = lines[i - b2].strip()
                        if cd.startswith("r/"):
                            sub = cd
                            break
                    results.append({
                        "title": cand[:250],
                        "url": u,
                        "content": cand,
                        "source": "reddit",
                        "meta": {"subreddit": sub, "votes_line": line},
                    })
                    seen_titles.add(cand)
                    break
        if len(results) >= limit:
            break
    return results[:limit]


# ═══════════════════════════════════════════════════════════════
# 5 · V2EX (已退役 · 境内 HTTP=000·CDP 也拿不到内容)
# v13.88.9g · 用户 07-31 认账: 昨天 commit 82cb73f 号称「改走纯HTTP」是假汇报,
#   主函数根本没改·还是 CDP·且实测 HTTP=000 (境内被墙)·CDP 也 LEN=0·两通道全死→退役
# ═══════════════════════════════════════════════════════════════

@instrument("v2ex")
def search_v2ex_hot(query: str = "", limit: int = 10) -> list:
    """V2EX 热帖 · 曾通过 CDP 拿 JSON API
    v13.88.9g · 已退役: 境内 HTTP=000·CDP navigate_and_read 也拿不到内容 (2026-07-31 实测)·入口短路
    query 是可选过滤词 (不给就返所有热帖)"""
    if _retired_shortcircuit("v2ex"):
        return []
    from web_access_bridge import WebAccess
    wa = WebAccess()
    if not wa.cdp_available():
        return []
    text = wa.cdp_navigate_and_read("https://www.v2ex.com/api/topics/hot.json", wait_ms=3000)
    if not text:
        return []
    try:
        data = json.loads(text)
    except Exception:
        # innerText 版是 preformatted·可能有换行
        try:
            data = json.loads(text.replace("\n", " "))
        except Exception:
            return []
    if not isinstance(data, list):
        return []
    q_low = query.lower() if query else ""
    results = []
    for item in data:
        title = item.get("title", "")
        content = item.get("content", "") or ""
        # query 过滤 (title/content 含关键词)
        if q_low and q_low not in (title + content).lower():
            continue
        results.append({
            "title": title[:200],
            "url": item.get("url", ""),
            "content": content[:1500] or title,
            "source": "v2ex",
            "meta": {
                "node": item.get("node", {}).get("title", ""),
                "member": item.get("member", {}).get("username", ""),
                "replies": item.get("replies", 0),
            },
        })
        if len(results) >= limit:
            break
    return results


# ═══════════════════════════════════════════════════════════════
# 5.5 · Sogou Weixin (直连列表 + CDP tab-内-click 拿全文) ⭐⭐⭐⭐
# ═══════════════════════════════════════════════════════════════
# 用户诉求 (2026-07-20): "加入搜狗微信搜索去增强搜索能力,他可以搜很多公众号
# 的内容和爬取全部文" → v13.84 首版关全文·v13.85 真跑通:
# ─── 全文抓取真解 ────────────────────────────────────
# 卡点根因: sogou /link 只在 Referer=weixin.sogou.com 时才不返验证码
#   curl 直连: 136 字 VerifyCode 页 (无 Referer)
#   /new sogou/link tab: 也拿不到 (相当于新开·无 Referer)
# 真解: web-access proxy 3457 打开 sogou 列表页 → 在 tab 内 JS click
#       链接 → 浏览器天然带 Referer → 302 到 mp.weixin.qq.com → 拿 innerText
# 真跑验证 (2026-07-20): 3s click + 2-4s 轮询 = 1500+ 字公众号全文·干净
# ─────────────────────────────────────────────────────

_SOGOU_PROXY = "http://localhost:3457"
_SOGOU_PROXY_PORT = 3457
# v13.86.27 · 2026-07-23 用户抓 daily health 挂 · 根因 3456 是 claude-code-router
# 注释一直写"避 claude-code-router 3456·用 3457" · 但常量误写 3456
# keepalive 拉的也是 3457 · 常量单独错·修
# v13.86 · proxy_down err 明确化 · 用户 2026-07-22 memory How-to-apply-3
# 之前 detail_err='?' 太模糊·排查 10min·现在直白抛端口
_SOGOU_PROXY_DOWN_ERR = f"proxy_down:localhost:{_SOGOU_PROXY_PORT}"


def _sogou_proxy_ok() -> bool:
    """web-access proxy 是否在跑 (CDP tab 服务)"""
    try:
        r = subprocess.run(
            ["curl", "-sS", "--max-time", "3", f"{_SOGOU_PROXY}/health"],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0 and (r.stdout or "").strip() != ""
    except Exception:
        return False


_SOGOU_LAST_TAB_ERR = ""  # v13.86.55 · 暴露 new_tab 真错·别静默吞


def _sogou_proxy_new_tab(url: str) -> str:
    """开后台 tab · 返 tid · fail 返 空 · v13.86.55 记 error 到 _SOGOU_LAST_TAB_ERR"""
    global _SOGOU_LAST_TAB_ERR
    _SOGOU_LAST_TAB_ERR = ""
    try:
        r = subprocess.run(
            ["curl", "-sS", "--max-time", "30", "-X", "POST", "--data-raw", url,
             f"{_SOGOU_PROXY}/new"],
            capture_output=True, text=True, timeout=35,
        )
        data = json.loads(r.stdout or "{}")
        tid = data.get("targetId") or data.get("target") or data.get("id") or ""
        if not tid:
            # v13.86.55 · 用户 2026-07-28 抓「chrome 没连上被吞成 ? 」
            err = data.get("error", "")
            if "chrome" in err.lower() or "没连上" in err:
                _SOGOU_LAST_TAB_ERR = "chrome_cdp_down"
            else:
                _SOGOU_LAST_TAB_ERR = f"no_tid:{(err or r.stdout)[:80]}"
        return tid
    except Exception as e:
        _SOGOU_LAST_TAB_ERR = f"exc:{type(e).__name__}"
        return ""


def _sogou_proxy_eval(tid: str, js: str, timeout: int = 15):
    """/eval · 返 value (可能是 dict/str/None)"""
    try:
        r = subprocess.run(
            ["curl", "-sS", "--max-time", str(timeout), "-X", "POST",
             "--data-binary", js, f"{_SOGOU_PROXY}/eval?target={tid}"],
            capture_output=True, text=True, timeout=timeout + 3,
        )
        try:
            return json.loads(r.stdout or "{}").get("value")
        except Exception:
            return r.stdout
    except Exception:
        return None


def _sogou_proxy_info(tid: str) -> dict:
    try:
        r = subprocess.run(
            ["curl", "-sS", "--max-time", "6", f"{_SOGOU_PROXY}/info?target={tid}"],
            capture_output=True, text=True, timeout=8,
        )
        return json.loads(r.stdout or "{}")
    except Exception:
        return {}


def _sogou_proxy_close(tid: str) -> None:
    try:
        subprocess.run(
            ["curl", "-sS", "--max-time", "5", f"{_SOGOU_PROXY}/close?target={tid}"],
            capture_output=True, text=True, timeout=6,
        )
    except Exception:
        pass


# mp.weixin 全文页典型顶栏噪音 · 从 innerText 头部剥掉
_MP_HEADER_NOISE = (
    "微信扫一扫使用小程序",
    "Scan to Follow",
    "预览时标签不可点",
    "取消 允许",
    "赞  分享  在看",
    "阅读原文",
)


def _sogou_clean_mp_body(text: str) -> str:
    """从 mp.weixin innerText 里剥掉顶部导航噪音·保留正文"""
    if not text:
        return ""
    lines = [ln for ln in text.split("\n") if ln.strip()]
    # 头部若含明显噪音关键词·跳过
    while lines and any(k in lines[0] for k in _MP_HEADER_NOISE):
        lines.pop(0)
    return "\n".join(lines).strip()


def _sogou_fetch_details_via_click(query: str, top_n: int) -> list:
    """在一个 sogou 搜索列表 tab 内 · 逐个 JS-click 前 top_n 条 · 拿 mp 全文
    返回: [{'idx': i, 'href': sogou_link, 'mp_url': ..., 'body': ..., 'ok': bool}]
    thread-unsafe · 只能被 search_sogou_weixin 串行调用 · 但 sogou 抓词一次开一 tab 是必然
    """
    if not _sogou_proxy_ok() or top_n <= 0:
        return []

    list_url = f"https://weixin.sogou.com/weixin?type=2&query={quote_plus(query)}"
    tid = _sogou_proxy_new_tab(list_url)
    if not tid:
        # v13.86.55 · 用户 2026-07-28 抓「detail_fail:['?','?'] 没根因」
        # 返回带 err 的占位·让上层 detail_err 拿到真因
        err = _SOGOU_LAST_TAB_ERR or "new_tab_empty"
        return [{"idx": i, "ok": False, "err": err} for i in range(top_n)]
    # 列表页 SPA 组件加载·再等 2.5s
    time.sleep(2.5)

    details = []
    try:
        for i in range(top_n):
            # 1. tab 内 JS click 第 i 条 (target=_self · 在当前 tab 跳)
            click_js = f"""(() => {{
              const links = document.querySelectorAll('ul.news-list li h3 a');
              if(!links || !links[{i}]) return {{ok:false, reason:'no_anchor', total: links ? links.length : 0}};
              const a = links[{i}];
              const href = a.href;
              a.setAttribute('target', '_self');
              a.click();
              return {{ok:true, href: href, idx:{i}}};
            }})()"""
            click_val = _sogou_proxy_eval(tid, click_js, timeout=10) or {}
            if not isinstance(click_val, dict) or not click_val.get("ok"):
                details.append({"idx": i, "ok": False,
                                  "err": f"click_fail:{click_val}"})
                continue
            sogou_href = click_val.get("href", "")

            # 2. 轮询等跳转到 mp + innerText 就绪
            mp_url = ""
            body = ""
            for tick in range(12):  # 最多 12s
                time.sleep(1)
                info = _sogou_proxy_info(tid)
                cur_url = info.get("url", "") or ""
                ready = info.get("ready", "")
                # 拿 innerText
                text = _sogou_proxy_eval(
                    tid, "document.body.innerText.slice(0,80000)", timeout=10,
                )
                if isinstance(text, str) and len(text) > 800:
                    body = text
                    if "mp.weixin.qq.com" in cur_url:
                        mp_url = cur_url
                    # ready=complete 或 body 足够长 · 结束轮询
                    if ready == "complete" or len(body) > 1200:
                        break

            if body and len(body) > 500:
                details.append({
                    "idx": i,
                    "ok": True,
                    "href": sogou_href,
                    "mp_url": mp_url,
                    "body": _sogou_clean_mp_body(body)[:10000],
                })
            else:
                details.append({
                    "idx": i, "ok": False,
                    "err": f"body_short:{len(body)}",
                    "href": sogou_href, "mp_url": mp_url,
                })

            # 3. 回列表页 · 准备 click 下一条
            if i < top_n - 1:
                _sogou_proxy_eval(tid, "history.back(); true", timeout=6)
                time.sleep(2)  # 等列表页 DOM 恢复
    finally:
        _sogou_proxy_close(tid)

    return details


def search_sogou_weixin(query: str, limit: int = 8,
                        fetch_detail: bool = True, max_detail: int = 3) -> list:
    """搜狗微信搜索 · 拿公众号文章
    - 直连列表页 · 无 CDP · 1-2s · 每条含公众号名+摘要 (100-200 字)
    - fetch_detail=True (默认) 时对 top max_detail 条走 CDP tab-内-click 抓全文
      (在 sogou 页 JS click → Referer 天然带 → 302 到 mp.weixin.qq.com)
    - fail-safe: 列表挂返 [] · 详情挂保列表摘要
    真跑验证 (2026-07-20 v13.85): 全文 1500+ 字·干净公众号正文
    """
    # v13.85 · 60s 内同 query 不重复打 · sogou 连打 5+ 次会限速到 timeout
    global _LAST_SOGOU_CALL_AT
    now_ts = time.time()
    last = globals().get("_LAST_SOGOU_CALL_AT", 0)
    if now_ts - last < 30:  # 30s 全局冷却 · 温和
        wait_left = int(30 - (now_ts - last))
        time.sleep(min(wait_left, 5))  # 短等·继续
    globals()["_LAST_SOGOU_CALL_AT"] = time.time()

    url = f"https://weixin.sogou.com/weixin?type=2&query={quote_plus(query)}"
    html = _curl_text(url, timeout=15)
    # 反爬 timeout · body 太短说明限速 · 等 20s 重试一次
    if not html or len(html) < 2000:
        time.sleep(20)
        html = _curl_text(url, timeout=15)
        if not html or len(html) < 2000:
            return []

    # 每个结果块: <li id="sogou_vr_..." d="...">...</li>
    blocks = re.findall(
        r'<li id="sogou_vr_\d+_box_\d+"[^>]*>(.*?)</li>',
        html, re.S,
    )
    if not blocks:
        return []

    def _clean(s: str) -> str:
        # 去 red_beg/red_end 高亮标记 · 去 <em>/<script>/&entity; · 压空白
        s = re.sub(r'<!--red_(beg|end)-->', '', s)
        s = re.sub(r'<script.*?</script>', '', s, flags=re.S)
        s = re.sub(r'<[^>]+>', '', s)
        s = (s.replace('&nbsp;', ' ').replace('&amp;', '&')
               .replace('&ldquo;', '"').replace('&rdquo;', '"')
               .replace('&hellip;', '…').replace('&#39;', "'")
               .replace('&mdash;', '—').replace('&ndash;', '–')
               .replace('&rarr;', '→').replace('&lt;', '<').replace('&gt;', '>')
               .replace('&quot;', '"'))
        return re.sub(r'\s+', ' ', s).strip()

    results = []
    for blk in blocks[:limit * 2]:  # 多抓一倍防去重后不够
        # 标题 + 跳转 URL
        m_a = re.search(
            r'<h3>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>\s*</h3>',
            blk, re.S,
        )
        if not m_a:
            continue
        href = m_a.group(1).replace('&amp;', '&')
        title = _clean(m_a.group(2))
        if not title:
            continue
        # sogou 相对路径 → 绝对
        if href.startswith('/'):
            href = 'https://weixin.sogou.com' + href

        # 摘要
        m_sum = re.search(
            r'<p class="txt-info"[^>]*>(.*?)</p>',
            blk, re.S,
        )
        summary = _clean(m_sum.group(1)) if m_sum else ""

        # 公众号名
        m_gzh = re.search(
            r'<span class="all-time-y2"[^>]*>(.*?)</span>',
            blk, re.S,
        )
        gzh_name = _clean(m_gzh.group(1)) if m_gzh else ""

        results.append({
            "title": title[:200],
            "url": href,
            "content": (summary or title)[:1500],
            "source": "sogou_weixin",
            "meta": {
                "gzh": gzh_name,
                "has_detail": False,
            },
        })
        if len(results) >= limit:
            break

    # v13.85 · top max_detail 条 · CDP tab-内-click 拿全文
    if fetch_detail and results and _sogou_proxy_ok():
        top_n = min(max_detail, len(results))
        try:
            details = _sogou_fetch_details_via_click(query, top_n)
        except Exception as e:
            details = []
            for r in results[:top_n]:
                r["meta"]["detail_err"] = f"click_exc:{str(e)[:100]}"
        # 按 idx 回写
        for d in details:
            i = d.get("idx", -1)
            if 0 <= i < len(results):
                r = results[i]
                if d.get("ok") and d.get("body"):
                    r["content"] = d["body"][:8000]
                    r["meta"]["has_detail"] = True
                    r["meta"]["detail_len"] = len(d["body"])
                    r["meta"]["detail_via"] = "cdp_click"
                    if d.get("mp_url"):
                        r["meta"]["mp_url"] = d["mp_url"]
                else:
                    r["meta"]["detail_err"] = d.get("err", "unknown")

    # v13.87 · 抄 wigolo 亮点 1 · content_completeness 自评 + 亮点 5 · 落 cache
    try:
        from search_cache import cache_put, score_completeness
        for r in results:
            r["completeness"] = score_completeness(r.get("content", ""))
            if r.get("url") and r.get("content"):
                cache_put(r["url"], r["content"], "sogou_weixin", {
                    "title": r.get("title", ""),
                    "gzh": r.get("meta", {}).get("gzh", ""),
                })
    except Exception:
        # search_cache 挂了不影响返回
        for r in results:
            n = len(r.get("content", ""))
            r["completeness"] = ("full" if n > 1000
                                 else "partial" if n >= 200 else "shell")

    return results


# ═══════════════════════════════════════════════════════════════
# 6 · Zhihu (CDP · 用户 Chrome 已登录)
# ═══════════════════════════════════════════════════════════════

def search_zhihu(query: str, limit: int = 5) -> list:
    """知乎搜索 dispatcher · v13.87
    - 优先走官方 API (zhihu_official_api.search_zhihu_official) · 秒返·带 vote/comment
    - 需 ZHIHU_ACCESS_SECRET · 未设 / API 挂 → 降 CDP legacy (v13.28.1 热榜)
    - 参数保持兼容 · 上游 agent_search / SOURCE_CATALOG 不用改
    """
    # v13.87 · 优先官方 API
    if os.environ.get("ZHIHU_ACCESS_SECRET", "").strip():
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                                             "evolution"))
            from zhihu_official_api import search_zhihu_official
            api_hits = search_zhihu_official(query, count=min(limit, 10))
            if api_hits:
                return api_hits
        except Exception:
            pass
    # 降级 · CDP 热榜 legacy (v13.28.1)
    return _search_zhihu_cdp_legacy(query, limit)


def _search_zhihu_cdp_legacy(query: str, limit: int = 5) -> list:
    """v13.28.1 legacy · 官方 API 缺 secret / 挂时兜底
    - 走热榜 `/hot` · 5min 冷却"""
    from web_access_bridge import WebAccess
    wa = WebAccess()
    if not wa.cdp_available():
        return []
    # v13.28.1 · 冷却
    global _LAST_ZHIHU_CALL_AT
    now_ts = time.time()
    last = globals().get("_LAST_ZHIHU_CALL_AT", 0)
    if now_ts - last < 300:
        wait_left = int(300 - (now_ts - last))
        return [{"title": f"[zhihu 冷却中·还需 {wait_left}s]", "url": "",
                 "content": "", "source": "zhihu",
                 "meta": {"cooldown": True}}]
    globals()["_LAST_ZHIHU_CALL_AT"] = now_ts

    # 优先热榜 (稳定拿到真讨论)
    u = "https://www.zhihu.com/hot"
    text = wa.cdp_navigate_and_read(u, wait_ms=4500)
    if not text or len(text) < 800:
        return []
    # 抽卡片·知乎每卡片格式约:
    # <question title>\n<answer 摘要>\n<upvotes> 赞同 · <comments> 评论
    results = []
    lines = text.split("\n")
    cur_title = None
    for i, line in enumerate(lines):
        line = line.strip()
        if not line: continue
        # 标题 heuristic: 中文 ≥10 字·未含"赞同/评论/发布"
        if 10 <= len(line) <= 100 and re.search(r'[一-龥]', line) \
                and not any(k in line for k in ["赞同", "评论", "发布", "关注", "收藏"]):
            if cur_title:
                results.append({
                    "title": cur_title,
                    "url": u,
                    "content": cur_title,
                    "source": "zhihu",
                    "meta": {},
                })
            cur_title = line
        if len(results) >= limit:
            break
    if cur_title and len(results) < limit:
        results.append({"title": cur_title, "url": u, "content": cur_title,
                       "source": "zhihu", "meta": {}})
    return results[:limit]


# ═══════════════════════════════════════════════════════════════
# 7 · Xiaohongshu (v13.86 · 美团 universal-search API 直通 + CDP fallback)
# ═══════════════════════════════════════════════════════════════
# 用户 2026-07-21 给 API: agi.sankuai.com/tools/universal-search sources=["xhc"]
# 官方通道·免 CDP 免 captcha 免冷却·真跑 topK=5 拿 5 条真笔记 full content
# 每条含: url (真 xhs discovery/item) · title · content (500-2000 字全文) · publish_time
# 老 CDP 版留作 fallback (API 挂 / 无 token 时兜底)

_XHS_API_URL = "http://agi.sankuai.com/tools/universal-search/api/v1"


def _xhs_api_token() -> str:
    """token 优先 env · 兜底用户 2026-07-21 提供的 dev token
    生产接入前请设 MEITUAN_UNIVERSAL_SEARCH_TOKEN 环境变量"""
    return (os.environ.get("MEITUAN_UNIVERSAL_SEARCH_TOKEN")
            or "")


def _xhs_api_health_log(record: dict) -> None:
    """v13.86 · hook · 每次 API 调用落盘·供 daily_report 消费"""
    try:
        path = os.path.join(V11, ".xhs_api_health.jsonl")
        with open(path, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def search_xiaohongshu_api(query: str, limit: int = 10,
                            timeout: int = 12) -> list:
    """v13.86 · 官方 API 通道 · 秒返真笔记全文
    - 无 CDP · 无冷却 · 无 captcha · 直返 JSON
    - topK 上限走 API 参数 · 单次 10 条稳
    - fail-safe: HTTP 挂 / status != 0 / results 空 → 返 []
    hook: 每次调用落 .xhs_api_health.jsonl (ok/latency/count/err)
    """
    t0 = time.time()
    health = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "query": query, "limit": limit,
        "ok": False, "count": 0, "elapsed_s": 0.0,
        "avg_content_len": 0, "err": "",
    }
    try:
        payload = json.dumps({
            "query": query,
            "topK": limit,
            "isFast": True,
            "timeout": timeout * 1000,
            "sources": ["xhc"],
        }, ensure_ascii=False).encode("utf-8")

        r = subprocess.run(
            ["curl", "-sS", "--max-time", str(timeout + 3),
             "--location", _XHS_API_URL,
             "--header", f"Authorization: Bearer {_xhs_api_token()}",
             "--header", "Content-Type: application/json",
             "--data-binary", "@-"],
            input=payload, capture_output=True, timeout=timeout + 5,
        )
        health["elapsed_s"] = round(time.time() - t0, 2)

        if r.returncode != 0 or not r.stdout:
            health["err"] = f"curl_fail:rc={r.returncode}"
            _xhs_api_health_log(health)
            return []

        data = json.loads(r.stdout.decode("utf-8", errors="replace"))
        # 双层状态·外层 status 是传输层·内层 data.code 是业务层
        if data.get("status", -1) != 0:
            health["err"] = f"api_status:{data.get('status')}·{data.get('message','')[:80]}"
            _xhs_api_health_log(health)
            return []
        inner = data.get("data") or {}
        inner_code = inner.get("code", 0)
        if inner_code != 0:
            # 区分「后端异常」vs「无数据 (industryRecall empty)」
            err_map = inner.get("internal_err_map") or {}
            err_text = "|".join(f"{k}:{v}" for k, v in err_map.items())[:200]
            if "industryRecall return empty" in err_text:
                # 无数据·不算 API 挂·记 empty 让 dispatcher 降级到 CDP
                health["err"] = "industry_recall_empty"
                health["ok"] = True  # API 通了·只是这个 query 无覆盖
            else:
                health["err"] = f"api_inner:{inner_code}·{err_text}"
            _xhs_api_health_log(health)
            return []

        raw_results = inner.get("results") or []
        results = []
        content_lens = []
        # v13.87 · 抄 wigolo 亮点 1 · content_completeness 自评 + 亮点 5 · 落 cache
        try:
            from search_cache import cache_put, score_completeness
            _has_cache = True
        except Exception:
            _has_cache = False
        for item in raw_results[:limit]:
            content = item.get("content") or ""
            # xhs 常见占位符 _nr_ = 换行 · 转真换行
            content = content.replace("_nr_", "\n").strip()
            content_lens.append(len(content))
            completeness = (score_completeness(content) if _has_cache
                            else ("full" if len(content) > 1000
                                  else "partial" if len(content) >= 200
                                  else "shell"))
            url_i = item.get("url", "")
            hit = {
                "title": (item.get("title") or "")[:200],
                "url": url_i,
                "content": content[:5000],
                "source": "xiaohongshu",
                "completeness": completeness,   # v13.87 亮点 1
                "meta": {
                    "via": "meituan_api",
                    "publish_time": item.get("publish_time", ""),
                    "rerank_score": item.get("rerank_score"),
                    "cited_by": item.get("cited_by", 0),
                    "content_len": len(content),
                    "has_detail": len(content) > 300,
                },
            }
            results.append(hit)
            # v13.87 亮点 5 · 落 search_cache
            if _has_cache and url_i and content:
                try:
                    cache_put(url_i, content, "xhs_api", {
                        "title": hit["title"],
                        "publish_time": item.get("publish_time", ""),
                    })
                except Exception:
                    pass

        health["ok"] = len(results) > 0
        health["count"] = len(results)
        if content_lens:
            health["avg_content_len"] = int(sum(content_lens) / len(content_lens))
        # v13.87 · 汇总 completeness 分布进 health
        comp_dist = {}
        for r in results:
            k = r.get("completeness", "shell")
            comp_dist[k] = comp_dist.get(k, 0) + 1
        health["completeness_dist"] = comp_dist
        _xhs_api_health_log(health)
        return results
    except Exception as e:
        health["elapsed_s"] = round(time.time() - t0, 2)
        health["err"] = f"exc:{type(e).__name__}:{str(e)[:120]}"
        _xhs_api_health_log(health)
        return []


def search_xiaohongshu(query: str, limit: int = 5) -> list:
    """小红书搜索 dispatcher · v13.86
    - 优先走 API (search_xiaohongshu_api) · 秒返全文·首选
    - API 覆盖以生活/美食/本地为主·「副业/AI」类返 industryRecall empty (无数据)
    - API 返空 → 降级 CDP 探索页老路
    - 参数保持兼容 · 上游 agent_search / SOURCE_CATALOG 不用改
    """
    api_hits = search_xiaohongshu_api(query, limit=limit)
    if api_hits:
        return api_hits
    # 降级 · CDP 探索页 (老路 · v13.28.1)
    # 触发场景: API industryRecall empty · 或 curl_fail · 或 token 挂
    return _search_xiaohongshu_cdp_legacy(query, limit)


def _search_xiaohongshu_cdp_legacy(query: str, limit: int = 5) -> list:
    """v13.28.1 legacy · 只在 API 挂时兜底
    - 完全放弃搜索页 (风控 + 无内容)
    - 只读探索页 (`/explore`) · 首页真笔记流
    - 5 分钟内至多访问一次冷却"""
    from web_access_bridge import WebAccess
    wa = WebAccess()
    if not wa.cdp_available():
        return []

    # v13.28.1 · 强制冷却 · 5 min 内最多一次
    global _LAST_XHS_CALL_AT
    now_ts = time.time()
    last = globals().get("_LAST_XHS_CALL_AT", 0)
    if now_ts - last < 300:  # 5 min
        wait_left = int(300 - (now_ts - last))
        return [{"title": f"[xhs 冷却中·还需 {wait_left}s]", "url": "",
                 "content": "小红书 5min 内已访问过·避免触发风控 captcha",
                 "source": "xiaohongshu",
                 "meta": {"cooldown": True, "wait_left_s": wait_left}}]
    globals()["_LAST_XHS_CALL_AT"] = now_ts

    results = []
    seen = set()

    def _parse_xhs_notes(text: str) -> list:
        """从 xhs innerText 里抽笔记 · 支持搜索页和探索页两种格式
        探索页格式约:
          [标题]\n[作者]\n[点赞数(带 emoji 心)]
        搜索页:
          [标题]\n作者信息...点赞
        """
        out = []
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        skip_words = ["首页", "点点", "RED", "直播", "发布", "通知", "沪ICP",
                       "登录", "注册", "更多", "关于我们", "举报", "网信算备",
                       "@Copyright", "©", "行吟信息科技", "地址", "电话",
                       "推荐", "穿搭", "美食", "彩妆", "影视", "职场", "情感",
                       "家居", "游戏", "旅行", "健身", "视频", "世界杯",
                       "问点点", "ai", "综合", "今日", "月", "日"]
        # 逐行找 · 标题特征:
        #  8-100 字 · 含中文 · 不在 skip_words · 下一行是作者名(短) · 再下一行可能是点赞数
        for i, line in enumerate(lines):
            if not (8 <= len(line) <= 100):
                continue
            if not re.search(r'[一-龥]', line):
                continue
            if any(k in line for k in skip_words):
                continue
            # 数字行跳过 (296 · 1505 这种)
            if re.match(r'^[\d.]+[Kk万]?$', line):
                continue
            # 后一行若是短(<15 字)且含中文可能是作者
            author = ""
            likes = ""
            if i + 1 < len(lines):
                nx = lines[i + 1]
                if len(nx) < 20 and re.search(r'[一-龥a-zA-Z]', nx):
                    author = nx[:30]
            if i + 2 < len(lines):
                nnx = lines[i + 2]
                if re.match(r'^[\d.]+[Kk万]?$', nnx):
                    likes = nnx
            out.append({"title": line[:150], "author": author, "likes": likes})
        return out

    # v13.28.1 · 只读探索页一次 · 不再走搜索页 (风控 + 无内容 · 二无)
    q_words = re.findall(r'[一-龥]{2,}|[a-zA-Z]{3,}', query)
    def _relevant(title: str) -> bool:
        if not q_words:
            return True
        return any(w.lower() in title.lower() for w in q_words)

    u_explore = "https://www.xiaohongshu.com/explore"
    text = wa.cdp_navigate_and_read(u_explore, wait_ms=5000)
    notes = _parse_xhs_notes(text) if text and len(text) > 800 else []

    if not notes:
        return []

    # 探索页笔记 · 相关性排序 · 相关的排前
    scored = [(n, 1 if _relevant(n["title"]) else 0) for n in notes]
    scored.sort(key=lambda x: -x[1])

    for n, is_rel in scored:
        if n["title"] in seen: continue
        results.append({
            "title": n["title"], "url": u_explore,
            "content": f"{n['title']} · 作者 {n['author']} · 赞 {n['likes']}",
            "source": "xiaohongshu",
            "meta": {"page_type": "explore", "relevant": bool(is_rel),
                      "author": n["author"], "likes": n["likes"]},
        })
        seen.add(n["title"])
        if len(results) >= limit: break

    return results[:limit]


# ═══════════════════════════════════════════════════════════════
# 8 · ProductHunt (CDP · 直连也可能通)
# ═══════════════════════════════════════════════════════════════

@instrument("producthunt")
def search_producthunt(query: str, limit: int = 4) -> list:
    """ProductHunt · 找当天/本周新工具·作者视角"""
    from web_access_bridge import WebAccess
    wa = WebAccess()
    u = f"https://www.producthunt.com/search?q={quote_plus(query)}"
    if wa.cdp_available():
        text = wa.cdp_navigate_and_read(u, wait_ms=5000)
    else:
        text = _curl_text(u, timeout=15)
    if not text or len(text) < 200:
        return []
    results = []
    # 产品名 + tagline · 抽 heuristic
    for line in text.split("\n"):
        line = line.strip()
        if 5 <= len(line) <= 120 and not line.startswith("©"):
            results.append({
                "title": line[:150],
                "url": u,
                "content": line,
                "source": "producthunt",
                "meta": {},
            })
        if len(results) >= limit:
            break
    return results[:limit]


# ═══════════════════════════════════════════════════════════════
# 9 · Google (核心中的核心 · CDP · Chrome 翻墙必备) ⭐⭐⭐⭐⭐
# ═══════════════════════════════════════════════════════════════

@instrument("google")
def search_google(query: str, limit: int = 10, lang: str = "auto",
                   try_both: bool = True) -> list:
    """Google 搜索 · 用户 (2026-07-10) 明令:'尽量英文·再尝试中文·模拟人类'
    lang:
      'en'/'zh' 硬指定
      'auto' (默认): 检测 query 是否含中文 + try_both=True 时**先英文搜一遍·不足再中文补**
    try_both: True 时 · 英文搜完 < limit/2 结果·就自动用中文再搜一遍合并
              (模拟人类真研究行为:先看海外一手源·中文补)"""
    from web_access_bridge import WebAccess
    wa = WebAccess()
    if not wa.cdp_available():
        return []

    has_cn = bool(re.search(r'[一-龥]', query))

    # v13.26 · 语言路径决策
    # 用户明令: **先英文·再中文**
    langs_to_try = []
    if lang == "en":
        langs_to_try = ["en"]
    elif lang == "zh":
        langs_to_try = ["zh"]
    else:  # auto
        if has_cn:
            # query 是中文·但我们先尝试翻译成英文关键词搜一遍 (跳过·因为要 LLM 翻译)
            # 直接双语兜底
            langs_to_try = ["zh"]  # 中文 query 走中文
            if try_both:
                langs_to_try = ["en", "zh"]  # 也允许英文 (跨语言场景)
        else:
            # query 是英文 · 优先英文·不够再中文
            langs_to_try = ["en"]
            if try_both:
                langs_to_try = ["en", "zh"]

    all_results = []
    for cur_lang in langs_to_try:
        hl = "zh-CN" if cur_lang == "zh" else "en"
        u = f"https://www.google.com/search?q={quote_plus(query)}&hl={hl}&num=20"
        text = wa.cdp_navigate_and_read(u, wait_ms=4500)
        if not text or len(text) < 300:
            continue
        # 用旧逻辑 parse
        parsed = _google_parse(text, u, cur_lang, limit)
        all_results.extend(parsed)
        # 够了就停·不做第二次
        if len(all_results) >= limit:
            break
        # 首次拿到 >= limit//2 结果·且已经试了 en · 就跳过中文
        if len(all_results) >= limit // 2 and cur_lang == "en":
            break

    # 去重 (按 URL)
    seen = set()
    dedup = []
    for r in all_results:
        u = r.get("url", "")
        if u and u not in seen:
            seen.add(u)
            dedup.append(r)
    return dedup[:limit]


@instrument("google_click_all")
def search_google_click_all(query: str, limit: int = 10, lang: str = "auto",
                              max_click: int = 5) -> list:
    """v13.63 · 每条 Google 结果都点详情
    用户 2026-07-13:「google 的所有结果都要点击尽可能看看想那个」

    流程:
      1. search_google 拿 top N url+title+snippet
      2. 对 top max_click 条 · 用 web_access.read_url 真跑抓详情
      3. 每条返回 {..., detail_len, detail_via, detail_excerpt}
      4. 每条落 site_bookmark record
    """
    from web_access_bridge import WebAccess
    wa = WebAccess()
    hits = search_google(query, limit=limit, lang=lang)
    if not hits:
        return []
    import concurrent.futures
    def _click(hit):
        u = hit.get("url", "")
        if not u or not u.startswith("http"):
            return hit
        try:
            r = wa.read_url(u, prefer='auto', force_retry=False,
                            auto_click_listing=False)
            hit["detail_via"] = r.get("via")
            hit["detail_ok"] = r.get("ok")
            hit["detail_len"] = len(r.get("content", ""))
            hit["detail_excerpt"] = (r.get("content") or "")[:1500]
        except Exception as e:
            hit["detail_err"] = str(e)[:200]
        return hit

    # 只点前 max_click 条 · 剩下的保 snippet
    to_click = hits[:max_click]
    kept = hits[max_click:]
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        clicked = list(pool.map(_click, to_click))
    return clicked + kept


def _google_parse(text: str, u: str, lang: str, limit: int) -> list:
    """从 CDP innerText 里 parse Google 结果 (原 search_google 主体逻辑抽出)"""
    if not text or len(text) < 300:
        return []

    # Google innerText 每个结果块的结构约:
    # <site-name>\n<url path> · <breadcrumb>\n<Title>\n<snippet 摘要>
    # 或
    # <Title>\n<url>\n<snippet>
    # 抽 heuristic: 找带 "http" 或 "› ..." 的行·前面的行是 title·后面的行是 snippet
    results = []
    lines = text.split("\n")
    i = 0
    seen = set()
    # 跳过前 20 行导航
    start = 20
    while i < start and i < len(lines):
        if any(k in lines[i] for k in ["搜索结果", "Search Results", "网页搜索结果"]):
            start = i + 1
            break
        i += 1
    i = start

    while i < len(lines):
        line = lines[i].strip()
        # 匹配 URL 面包屑: "zhuanlan.zhihu.com › ..." 或 "https://..."
        is_url = re.search(r'https?://\S+', line) or ' › ' in line or '.com' in line
        if is_url and i > 0:
            # title 是前一非空行
            title = None
            for back in range(1, 4):
                if i - back < 0: break
                cand = lines[i - back].strip()
                if cand and 8 < len(cand) < 200 and cand not in ["网页搜索结果", "全部", "视频", "图片", "新闻"]:
                    title = cand
                    break
            if not title:
                i += 1
                continue
            # snippet 是后续 1-3 行连接
            snippet_parts = []
            for fwd in range(1, 5):
                if i + fwd >= len(lines): break
                cand = lines[i + fwd].strip()
                if not cand: continue
                # 停在下一个 URL 或明显结构
                if re.search(r'https?://', cand) or ' › ' in cand:
                    break
                snippet_parts.append(cand)
                if len(" ".join(snippet_parts)) > 400:
                    break
            snippet = " ".join(snippet_parts)[:600]
            # 提取真 URL (含 http 或从面包屑构造)
            url_m = re.search(r'https?://\S+', line)
            real_url = url_m.group(0) if url_m else line.split(" ")[0]
            if title and title not in seen:
                results.append({
                    "title": title[:200],
                    "url": real_url,
                    "content": snippet or title,
                    "source": "google",
                    "meta": {"lang": lang, "breadcrumb": line[:80]},
                })
                seen.add(title)
        if len(results) >= limit:
            break
        i += 1
    return results[:limit]


# ═══════════════════════════════════════════════════════════════
# 10 · Twitter/X (CDP · 用户 Chrome 已登录 @ChiVortex)
# ═══════════════════════════════════════════════════════════════

@instrument("twitter")
def search_twitter(query: str, limit: int = 8, tab: str = "top") -> list:
    """Twitter/X 搜索 · 用户已登录 @ChiVortex
    tab: 'top' (热门) / 'latest' (最新) / 'user' (账号)
    每帖含: 作者 + @handle + 时间 + 正文 + 数据(转/评/赞/展)"""
    from web_access_bridge import WebAccess
    wa = WebAccess()
    if not wa.cdp_available():
        return []
    f = {"top": "", "latest": "&f=live", "user": "&f=user"}.get(tab, "")
    u = f"https://x.com/search?q={quote_plus(query)}&src=typed_query{f}"
    text = wa.cdp_navigate_and_read(u, wait_ms=5500)
    if not text or len(text) < 300:
        return []

    # Twitter innerText 每帖结构约:
    # <author 名字>\n@<handle>\n·\n<time>\n<正文>\n<数据>
    results = []
    lines = text.split("\n")
    # 跳过导航前缀
    start = 0
    for i, line in enumerate(lines):
        if "搜索时间线" in line or "Search timeline" in line:
            start = i + 1
            break
    lines = lines[start:]

    # 逐块识别 @handle 触发一次帖抽取
    i = 0
    seen = set()
    while i < len(lines):
        line = lines[i].strip()
        # @handle 特征: @+字母数字_·长度 1-15
        if re.match(r'^@[A-Za-z0-9_]{1,15}$', line):
            handle = line
            # 找作者名 (@handle 前一行)
            author = lines[i - 1].strip() if i > 0 else ""
            # 找正文 · 跳过 · 和 time
            body_start = i + 1
            body_parts = []
            for fwd in range(1, 15):
                if i + fwd >= len(lines): break
                cand = lines[i + fwd].strip()
                if not cand: continue
                # 跳过时间行 "·" 或 "5月8日" 或 "12小时" 等
                if cand == "·" or re.match(r'^\d+[hm小时分]|\d+月\d+日|\d+年\d+月|翻译自|显示原文', cand):
                    continue
                # 跳过数据行 (纯数字 或 "K/万")
                if re.match(r'^\d+(\.\d+)?[Kk万M]?$', cand):
                    break
                # 跳过 "显示更多" / "Show more"
                if cand in ["显示更多", "Show more", "查看新帖子"]:
                    break
                body_parts.append(cand)
                if len(" ".join(body_parts)) > 500:
                    break
            body = " ".join(body_parts)[:600]
            key = (handle, body[:40])
            if body and len(body) > 15 and key not in seen:
                results.append({
                    "title": body[:150],
                    "url": u,
                    "content": body,
                    "source": "twitter",
                    "meta": {"author": author[:50], "handle": handle},
                })
                seen.add(key)
        if len(results) >= limit:
            break
        i += 1
    return results[:limit]


# ═══════════════════════════════════════════════════════════════
# 11 · 掘金 (直连 API · 国内开发者)
# ═══════════════════════════════════════════════════════════════

@instrument("juejin")
def search_juejin(query: str, limit: int = 5) -> list:
    """v13.28.3 · 掘金 API · 国内开发者一手内容·真跑测试通"""
    try:
        # 掘金 API 需要 POST · 拉推荐流 + 从 title 过滤 query 关键词
        body = json.dumps({
            "id_type": 2, "cursor": "0", "limit": 20, "sort_type": 200
        })
        r = subprocess.run([
            "curl", "-sS", "--max-time", "10",
            "-A", UA, "-H", "Content-Type: application/json",
            "-X", "POST", "-d", body,
            "https://api.juejin.cn/recommend_api/v1/article/recommend_all_feed"
            "?client_type=6587&aid=2608&cursor=0&limit=20&sort_type=200",
        ], capture_output=True, text=True, timeout=12)
        if r.returncode != 0:
            return []
        d = json.loads(r.stdout)
        if d.get("err_no") != 0:
            return []
        items = d.get("data", []) or []
    except Exception:
        return []

    q_words = re.findall(r'[一-龥]{2,}|[a-zA-Z]{3,}', (query or "").lower())

    def _match(text: str) -> bool:
        if not q_words: return True
        return any(w.lower() in text.lower() for w in q_words[:5])

    results = []
    for it in items:
        info = it.get("item_info", {}).get("article_info", {}) or {}
        title = info.get("title", "")
        brief = info.get("brief_content", "")
        if not title:
            continue
        combined = title + " " + brief
        # 相关的排前 · 无关也保留 (推荐流用)
        aid_ = info.get("article_id", "")
        results.append({
            "title": title[:200],
            "url": f"https://juejin.cn/post/{aid_}" if aid_ else "",
            "content": brief[:800] or title,
            "source": "juejin",
            "meta": {"digg_count": info.get("digg_count", 0),
                      "view_count": info.get("view_count", 0),
                      "relevant": _match(combined)},
        })
    # 相关的排前
    results.sort(key=lambda r: -(1 if r["meta"]["relevant"] else 0))
    return results[:limit]


# ═══════════════════════════════════════════════════════════════
# 12 · Bilibili 热门 (直连 API · 视频类真人内容)
# ═══════════════════════════════════════════════════════════════

@instrument("bilibili")
def search_bilibili_popular(query: str, limit: int = 5) -> list:
    """v13.28.3 · B站 热门视频 API · 真人 UGC 内容"""
    try:
        r = subprocess.run([
            "curl", "-sS", "--max-time", "8", "-A", UA,
            "https://api.bilibili.com/x/web-interface/popular?ps=20&pn=1",
        ], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return []
        d = json.loads(r.stdout)
        if d.get("code") != 0:
            return []
        items = d.get("data", {}).get("list", []) or []
    except Exception:
        return []

    q_words = re.findall(r'[一-龥]{2,}|[a-zA-Z]{3,}', (query or "").lower())
    def _match(text: str) -> bool:
        if not q_words: return True
        return any(w.lower() in text.lower() for w in q_words[:5])

    results = []
    for it in items:
        title = it.get("title", "")
        desc = it.get("desc", "")
        combined = title + " " + desc
        results.append({
            "title": title[:200],
            "url": it.get("short_link_v2") or f"https://www.bilibili.com/video/{it.get('bvid','')}",
            "content": desc[:600] or title,
            "source": "bilibili",
            "meta": {"view": it.get("stat", {}).get("view", 0),
                      "up": it.get("owner", {}).get("name", ""),
                      "relevant": _match(combined)},
        })
    results.sort(key=lambda r: -(1 if r["meta"]["relevant"] else 0))
    return results[:limit]


# ═══════════════════════════════════════════════════════════════
# 13 · GitHub Discussions (真跑用户讨论 · 直连 API)
# ═══════════════════════════════════════════════════════════════

@instrument("github_discussions")
def search_github_discussions(query: str, limit: int = 5) -> list:
    """v13.28.3 · GitHub Discussions · 比 issues 更多真讨论"""
    url = ("https://api.github.com/search/issues"
           f"?q={quote_plus(query)}+is:discussion+comments:%3E5"
           f"&per_page={limit}&sort=reactions&order=desc")
    data = _curl_json(url, timeout=12)
    items = data.get("items", []) or []
    results = []
    for it in items:
        results.append({
            "title": it.get("title", "")[:150],
            "url": it.get("html_url", ""),
            "content": (it.get("body", "") or "")[:1500],
            "source": "github_discussions",
            "meta": {"comments": it.get("comments", 0),
                      "reactions": it.get("reactions", {}).get("total_count", 0),
                      "state": it.get("state", "")},
        })
    return results


# ═══════════════════════════════════════════════════════════════
# v13.88.28 · 老 mars-data/search_engine.py 3 源迁入
# 用户 2026-07-27 演绎："老 topic_bank 埋 · 但 CSDN/ClawHub/Anthropic 3 源新的没有 · 搬"
# 老源已 curl 200 验证 · CSDN 175ms / ClawHub 2.3s / Anthropic 1.3s
# ═══════════════════════════════════════════════════════════════

@instrument("csdn")
def search_csdn(query: str, limit: int = 5) -> list:
    """CSDN 搜索 · 中文 AI 实战文章最丰富的来源
    老 search_engine.py:429 迁来 · so.csdn.net/api/v3/search 直连"""
    url = f"https://so.csdn.net/api/v3/search?q={quote_plus(query)}&t=all&p=1&s=0&tm=0"
    data = _curl_json(url, timeout=8)
    items = data.get("result_vos", []) or []
    results = []
    for item in items[:limit]:
        title = re.sub(r'<[^>]+>', '', item.get("title", "") or "").strip()
        url_ = item.get("url", "") or ""
        # 清理 CSDN tracking 参数
        if "?" in url_:
            url_ = url_.split("?")[0]
        desc = re.sub(r'<[^>]+>', '', item.get("description", "") or "").strip()[:400]
        if not (title and url_):
            continue
        results.append({
            "title": title[:150],
            "url": url_,
            "content": desc,
            "source": "csdn",
            "meta": {
                "nickname": item.get("nickname", ""),
                "created_time": item.get("created_time", ""),
                "view_num": item.get("view_num", 0),
            },
        })
    return results


@instrument("clawhub")
def search_clawhub(query: str, limit: int = 5) -> list:
    """ClawHub Skill 商店 · Claude skill 中文社区
    老 search_engine.py:117 迁来 · clawhub.ai/api/v1/skills 直连
    query 空时返回最新 N 个 · 有 query 时软关键词匹配 name/summary"""
    text = _curl_text("https://clawhub.ai/api/v1/skills", timeout=8)
    if not text:
        return []
    try:
        items = json.loads(text).get("items", []) or []
    except Exception:
        return []
    # updatedAt 倒排 · 最新在前
    items.sort(key=lambda x: x.get("updatedAt", 0), reverse=True)
    kw = [w for w in (query or "").lower().split() if len(w) > 2]
    results = []
    for s in items:
        slug = s.get("slug", "") or ""
        name = s.get("displayName", "") or slug
        summary = s.get("summary", "") or ""
        changelog = ((s.get("latestVersion") or {}).get("changelog") or "")
        # 关键词软匹配 · 命中任一即收
        haystack = (name + " " + summary).lower()
        if kw and not any(w in haystack for w in kw):
            continue
        results.append({
            "title": f"[ClawHub Skill] {name}"[:150],
            "url": f"https://clawhub.ai/skills/{slug}",
            "content": (summary + ("\n更新: " + changelog if changelog else ""))[:800],
            "source": "clawhub",
            "meta": {
                "stars": (s.get("stats") or {}).get("stars", 0),
                "version": (s.get("latestVersion") or {}).get("version", ""),
                "updated_at": s.get("updatedAt", 0),
            },
        })
        if len(results) >= limit:
            break
    # 无关键词命中 · 回退给前 N 个最新
    if not results and not kw:
        for s in items[:limit]:
            slug = s.get("slug", "") or ""
            name = s.get("displayName", "") or slug
            summary = s.get("summary", "") or ""
            results.append({
                "title": f"[ClawHub Skill] {name}"[:150],
                "url": f"https://clawhub.ai/skills/{slug}",
                "content": summary[:800],
                "source": "clawhub",
                "meta": {"stars": (s.get("stats") or {}).get("stars", 0)},
            })
    return results


@instrument("anthropic_news")
def search_anthropic_news(query: str = "", limit: int = 5) -> list:
    """Anthropic 官博 · Fable/Opus/Claude 官方发布
    老 search_engine.py:238 迁来 · anthropic.com/news HTML 抽卡
    query 空返回最新 · 有 query 时对 title 做包含过滤"""
    text = _curl_text("https://www.anthropic.com/news", timeout=10)
    if not text:
        return []
    # 抽 news 卡片: <a href="/news/xxx"> ... <h3>title</h3>
    entries = re.findall(
        r'href="(/news/[^"]+)"[^>]*>[\s\S]{0,600}?<h[23][^>]*>([^<]{5,180})</h[23]>',
        text,
    )
    q = (query or "").lower().strip()
    seen = set()
    matched = []  # query 命中
    latest = []   # 无 query 或未命中时的兜底：最新 N
    for path, title in entries:
        if path in seen:
            continue
        seen.add(path)
        title = re.sub(r"\s+", " ", title).strip()
        item = {
            "title": f"[Anthropic] {title}"[:180],
            "url": f"https://www.anthropic.com{path}",
            "content": title,  # 卡片没摘要 · 后续可 read_url 抓正文
            "source": "anthropic_news",
            "meta": {"path": path},
        }
        if q and q in title.lower():
            matched.append(item)
        if len(latest) < limit:
            latest.append(item)
    # 命中优先 · 未命中回退最新（老 search_engine.py 行为对齐 · 官博本来就低频）
    return (matched[:limit] if matched else latest)


# ═══════════════════════════════════════════════════════════════
# 全源聚合入口
# ═══════════════════════════════════════════════════════════════

# 源可用性 · 每次调 fanout_all 前会更新
SOURCE_CATALOG = {
    # v13.19 · Google 是核心 · priority 0 优先
    "google": {"fn": search_google, "reach": "cdp", "priority": 0},
    "twitter": {"fn": search_twitter, "reach": "cdp", "priority": 0},
    "hackernews": {"fn": search_hackernews, "reach": "direct", "priority": 1},
    "github_issues": {"fn": search_github_issues, "reach": "direct", "priority": 1},
    "github_repos": {"fn": search_github_repos, "reach": "direct", "priority": 2},
    # v13.88.9g · 退役 (fn 保留·别删)·retired=True → fanout 不选·priority 降到 99
    "sspai": {"fn": search_sspai, "reach": "cdp", "priority": 99, "retired": True},
    "reddit": {"fn": search_reddit, "reach": "cdp", "priority": 1},
    "v2ex": {"fn": search_v2ex_hot, "reach": "cdp", "priority": 99, "retired": True},
    # v13.87 · 知乎 dispatcher · 官方 API 优先 · CDP fallback (同 xhs v13.86 模式)
    "zhihu": {"fn": search_zhihu, "reach": "direct", "priority": 1},
    # v13.88.55 · 撤 zhihu_global 占位符 (fn=None · 从未实现)
    # 用户 07-28 抓测试·SOURCE_CATALOG 有 None 条目·下游调会 crash
    # 若将来真做知乎全网·加回来时必须给 fn
    # v13.86 · xiaohongshu dispatcher · API 优先(direct·秒返) · CDP fallback
    "xiaohongshu": {"fn": search_xiaohongshu, "reach": "direct", "priority": 1},
    "producthunt": {"fn": search_producthunt, "reach": "any", "priority": 3},
    # v13.28.3 · 用户 loop 演绎: "6 个平台漏了" · 补 3 个直连源
    "juejin": {"fn": search_juejin, "reach": "direct", "priority": 1},
    "bilibili": {"fn": search_bilibili_popular, "reach": "direct", "priority": 2},
    "github_discussions": {"fn": search_github_discussions, "reach": "direct", "priority": 1},
    # v13.85 · 搜狗微信 · 直连列表 + CDP tab-内-click 拿公众号全文
    # reach=direct · 列表 curl 无 CDP · 全文抓取时 fn 内部自己走 web-access proxy
    "sogou_weixin": {"fn": search_sogou_weixin, "reach": "direct", "priority": 1},
    # v13.88.28 · 老 mars-data 3 源迁入 · 补中文技术博客/Claude skill 商店/官方发布
    "csdn": {"fn": search_csdn, "reach": "direct", "priority": 2},
    "clawhub": {"fn": search_clawhub, "reach": "direct", "priority": 3},
    "anthropic_news": {"fn": search_anthropic_news, "reach": "direct", "priority": 3},
}


# v13.88.9i · 2026-07-31 · 源级健康记忆:近 1h 撞墙(captcha/cdp_empty)的 CDP 源本轮跳过
# 治用户抓「撞了还开·反复开 reddit/producthunt 撞 Cloudflare」。读 search_incidents 判断。
def _source_recently_dead(name: str, window_h: float = 1.0) -> bool:
    """某源近 window_h 小时内撞墙(captcha_or_rate_limit / cdp_empty)→ True 本轮跳过。"""
    fp = os.path.join(V11, ".search_incidents.jsonl")
    if not os.path.exists(fp):
        return False
    try:
        cutoff = time.time() - window_h * 3600
        bad_kinds = {"captcha_or_rate_limit", "cdp_empty", "cdp_exc", "cdp_no_tid"}
        # 源名 → 它的域名关键词(url 里匹配)
        dom = {"reddit": "reddit.com", "producthunt": "producthunt.com",
               "twitter": "x.com", "google": "google.com",
               "zhihu": "zhihu.com", "xiaohongshu": "xiaohongshu"}.get(name, name)
        hits = 0
        for line in open(fp, encoding="utf-8", errors="ignore"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            ts = d.get("ts", "")
            if dom in d.get("url", "") and d.get("kind") in bad_kinds:
                # 简单时间判断:用 unix_ts 若有·否则近似认最近的记录都算(保守跳过)
                u = d.get("unix_ts")
                if u is None or u >= cutoff:
                    hits += 1
        return hits >= 1
    except Exception:
        return False


def fanout_all(query: str, per_source: int = 5,
               sources: list = None, timeout: int = 60) -> dict:
    """并行搜所有可用源·返回 {source: [results]}
    v13.20 · 用户"电脑卡"投诉后重写:
    - 直连源 (hackernews/github_*/) · 并发 4 无压力
    - CDP 源 (google/twitter/reddit/zhihu/xhs/v2ex/sspai) · 严格串行·避免 Chrome 多 tab 爆
    """
    import concurrent.futures
    src_names = sources or list(SOURCE_CATALOG.keys())
    # v13.88.9g · 剔除退役源 (sspai/v2ex 等)·即使显式传入也不撞死站
    src_names = [n for n in src_names
                 if not SOURCE_CATALOG.get(n, {}).get("retired")]
    results_by_src = {}

    # v13.20 · 分组: 直连 vs CDP
    direct_names = [n for n in src_names if SOURCE_CATALOG.get(n, {}).get("reach") == "direct"]
    cdp_names = [n for n in src_names if SOURCE_CATALOG.get(n, {}).get("reach") in ("cdp", "any")]

    # 直连源 · 并发 4
    if direct_names:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(_safe_call, SOURCE_CATALOG[n]["fn"], query, per_source): n
                    for n in direct_names}
            for f in concurrent.futures.as_completed(futs, timeout=timeout):
                name = futs[f]
                try:
                    results_by_src[name] = f.result() or []
                except Exception:
                    results_by_src[name] = []

    # CDP 源 · 严格串行 · 每个之间等系统冷却
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "evolution"))
        from perf_guard import wait_until_cool
    except Exception:
        wait_until_cool = None

    # v13.88.9i · 2026-07-31 · 亲手真改(前几轮子agent声称改了实际主仓库没动·git diff空)
    # 用户反复抓: 一次开一堆 CDP 撞墙源(google/reddit/producthunt)·撞了还开·direct有货也开。
    # 根治: ① CDP 只兜底——direct 已有足够结果就不开 CDP ② 一次最多 1 个
    #        ③ 撞墙源(近1h captcha/cdp_empty)跳过 ④ 境外撞墙源(reddit/producthunt/twitter)降到最后
    _direct_hits = sum(len(v) for v in results_by_src.values())
    if _direct_hits >= max(3, per_source):
        # direct 够了·CDP 完全不开(治「有货也开一堆撞墙标签」)
        return results_by_src

    # priority: 国内能通的 CDP 优先·境外撞墙源(reddit/producthunt/twitter)最后
    priority_order = ["zhihu", "xiaohongshu", "google", "twitter", "reddit", "producthunt"]
    cdp_sorted = sorted(cdp_names, key=lambda n: priority_order.index(n) if n in priority_order else 99)
    # 撞墙源记忆: 近 1h 撞过 captcha/cdp_empty 的源本轮跳过(治「撞了还开」)
    try:
        cdp_sorted = [n for n in cdp_sorted if not _source_recently_dead(n)]
    except Exception:
        pass
    # v13.88.9i · 一次最多 1 个 CDP 源(原 3 个→开一堆标签炸负载+撞墙)
    cdp_cap = 1
    for name in cdp_sorted[:cdp_cap]:
        if wait_until_cool:
            wait_until_cool(f"cdp_{name}")
        try:
            results_by_src[name] = _safe_call(SOURCE_CATALOG[name]["fn"], query, per_source) or []
        except Exception:
            results_by_src[name] = []

    return results_by_src


def _safe_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        return []


def flatten_all(query: str, per_source: int = 5, sources: list = None) -> list:
    """扁平化返回·所有源统一为 [{'title','url','content','source','meta'}] 列表"""
    by_src = fanout_all(query, per_source, sources)
    out = []
    for src, rows in by_src.items():
        out.extend(rows)
    return out


# ═══════════════════════════════════════════════════════════════
# CLI 自测
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", default="AI agent hallucination")
    ap.add_argument("--source", default=None, help="hackernews/reddit/github_issues/...")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--doctor", action="store_true", help="测所有源可用性")
    args = ap.parse_args()

    if args.doctor:
        print(f"═══ 全网源可用性诊断 · query='{args.query}' ═══")
        for name in SOURCE_CATALOG:
            t0 = time.time()
            try:
                rs = SOURCE_CATALOG[name]["fn"](args.query, 3)
                el = time.time() - t0
                emoji = "✅" if rs else "⚠️"
                print(f"  {emoji} {name:15s} · {len(rs):2d} 结果 · {el:.1f}s")
                if rs:
                    print(f"      样本: {rs[0]['title'][:80]}")
            except Exception as e:
                el = time.time() - t0
                print(f"  ❌ {name:15s} · {type(e).__name__}: {str(e)[:60]} · {el:.1f}s")
    elif args.source:
        rs = SOURCE_CATALOG[args.source]["fn"](args.query, args.limit)
        for r in rs:
            print(f"[{r['source']}] {r['title']}")
            print(f"  {r['url']}")
            print(f"  {r['content'][:200]}")
            print()
    else:
        rs = flatten_all(args.query, per_source=args.limit)
        from collections import Counter
        by_src = Counter(r["source"] for r in rs)
        print(f"═══ 全源 fanout · 共 {len(rs)} 命中 ═══")
        for src, n in by_src.most_common():
            print(f"  {src}: {n}")
