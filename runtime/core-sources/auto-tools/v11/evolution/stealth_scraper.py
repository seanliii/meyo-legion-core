#!/usr/bin/env python3
"""
v13.61 · stealth_scraper · 免费自建版 Firecrawl/BrowserAct
────────────────────────────────────────────────────────────────
用户 2026-07-13 质问:
  "为啥 firecrawl 可以做的好·我们可以内化他的关键能力和代码吗?
   BrowserAct 为啥比直接 Playwright 强?
   毕竟我们不想花钱·搜索一下能不能自己搭建"

反思:我之前只会付费 API·没深挖原理

内化 Firecrawl 反爬核心 (从其开源 apps/playwright-service-ts 抄):
  1. playwright + UA 轮换 (fake-useragent · 免费)
  2. playwright-stealth (社区免费)
  3. --disable-blink-features=AutomationControlled (关掉 navigator.webdriver 标识)
  4. viewport 随机化
  5. 请求头补齐 (accept-language / sec-ch-ua)
  6. HTML→Markdown (html2text · 免费)

内化 BrowserAct stealth 核心:
  1. 隐私模式 (context = browser.new_context() incognito)
  2. 每次会话新指纹
  3. 请求间随机 sleep 模拟人类

差别:
  Firecrawl/BrowserAct 云端有代理池 (钱)
  我们只能用本地 IP · 无法轮换 IP
  但对**非 IP 反爬**的站(大部分内容站)·我们已经够
"""
import os
import sys
import time
import random
from urllib.parse import urlparse


# v13.88.79 · 静默监控统一接入 · 用户 07-28「所有静默监控都到齐了吗」
def _alert(severity: str, msg: str):
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
        from alert_bus import notify as _n
        _n(_o.path.basename(__file__).replace(".py",""), severity, msg)
    except Exception:
        pass


BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
V11 = os.path.join(BASE, "v11")


# 真实主流 UA 池 (fake_useragent 的稳定 fallback)
UA_POOL = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]

# 真实 accept-language 池
ACCEPT_LANG_POOL = [
    "zh-CN,zh;q=0.9,en;q=0.8",
    "en-US,en;q=0.9",
    "zh-CN,zh;q=0.9,zh-TW;q=0.7,en;q=0.6",
]

# 常见 viewport (真实设备比例)
VIEWPORTS = [
    (1920, 1080), (1440, 900), (1366, 768),
    (1536, 864), (1680, 1050),
]


def get_random_headers() -> dict:
    """随机浏览器指纹 headers"""
    try:
        from fake_useragent import UserAgent
        ua = UserAgent().random
    except Exception:
        ua = random.choice(UA_POOL)

    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": random.choice(ACCEPT_LANG_POOL),
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "max-age=0",
        "Sec-Ch-Ua": '"Chromium";v="150", "Google Chrome";v="150", "Not_A Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }


def stealth_curl_read(url: str, timeout: int = 15) -> str:
    """免费第一档:高级 curl · UA 轮换 + 完整浏览器 headers
    对轻反爬站(60%)够用·比裸 curl 强很多"""
    import subprocess
    headers = get_random_headers()
    args = ["curl", "-sS", "--max-time", str(timeout), "-L", "--compressed"]
    for k, v in headers.items():
        args.extend(["-H", f"{k}: {v}"])
    args.append(url)
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout + 3)
        if r.returncode == 0 and len(r.stdout or "") > 300:
            return r.stdout
    except Exception:
        pass
    return ""


def stealth_playwright_read(url: str, wait_ms: int = 2000,
                             max_chars: int = 30000) -> str:
    """免费第二档:playwright + stealth + 完整指纹伪装
    对中等反爬站(csdn/juejin)通用"""
    try:
        from playwright.sync_api import sync_playwright
        try:
            from playwright_stealth import Stealth
            stealth_ok = True
        except Exception:
            stealth_ok = False

        headers = get_random_headers()
        vw, vh = random.choice(VIEWPORTS)

        with sync_playwright() as p:
            # v13.81 · macOS 不需要 --no-sandbox (会显示不安全警告)
            # Linux docker 才需要 · 用平台检测
            import platform
            args = [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                f"--window-size={vw},{vh}",
            ]
            if platform.system() == "Linux":
                args.append("--no-sandbox")
            browser = p.chromium.launch(
                headless=True,
                args=args,
            )
            ctx = browser.new_context(
                user_agent=headers["User-Agent"],
                locale=random.choice(["zh-CN", "en-US"]),
                timezone_id=random.choice(["Asia/Shanghai", "America/New_York"]),
                viewport={"width": vw, "height": vh},
                extra_http_headers={
                    k: v for k, v in headers.items()
                    if k not in ("User-Agent",)
                },
                java_script_enabled=True,
            )

            # 应用 stealth 补丁 · 关键
            if stealth_ok:
                try:
                    Stealth().apply_stealth_sync(ctx)
                except Exception:
                    pass

            page = ctx.new_page()

            # 随机小 sleep 模拟人
            time.sleep(random.uniform(0.3, 1.2))

            try:
                page.goto(url, timeout=25000, wait_until="domcontentloaded")
                page.wait_for_timeout(wait_ms + random.randint(0, 1500))
                # 模拟人:滚动
                try:
                    page.mouse.wheel(0, random.randint(200, 600))
                    page.wait_for_timeout(random.randint(300, 800))
                except Exception:
                    pass

                content = page.locator("body").inner_text(timeout=5000)
                return content[:max_chars]
            finally:
                try: page.close()
                except: pass
                try: ctx.close()
                except: pass
                try: browser.close()
                except: pass
    except Exception as e:
        return f"[stealth_playwright_err: {e}]"


def html_to_markdown(html: str) -> str:
    """免费 markdown 转换 · 抄 Firecrawl 用 html2text"""
    try:
        import html2text
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = False
        h.body_width = 0  # 不换行
        return h.handle(html)
    except ImportError:
        return html


def stealth_scrape(url: str, output_format: str = "markdown") -> dict:
    """免费自建 Firecrawl scrape · 一个 API 搞定
    返回: {ok, content, via, format, time_s}"""
    t0 = time.time()

    # 步 1 · 域名分级选战术
    domain = urlparse(url).netloc.lower()
    hard = any(d in domain for d in [
        "zhihu.com", "xiaohongshu.com", "weibo.com",
        "csdn.net", "juejin.cn", "mp.weixin.qq.com",
        "google.com", "bing.com",
    ])

    if hard:
        raw = stealth_playwright_read(url)
        via = "stealth_playwright"
    else:
        raw = stealth_curl_read(url)
        via = "stealth_curl"
        if not raw or len(raw) < 500:
            raw = stealth_playwright_read(url)
            via = "stealth_playwright_fallback"

    if not raw or raw.startswith("[stealth"):
        return {"ok": False, "reason": raw or "empty",
                "time_s": round(time.time() - t0, 1)}

    # 步 2 · 格式转换
    if output_format == "markdown":
        # 如果拿到的是 HTML 就转 · 如果已是文本就返回
        if "<" in raw and ">" in raw:
            content = html_to_markdown(raw)
        else:
            content = raw
    else:
        content = raw

    return {
        "ok": True,
        "content": content,
        "via": via,
        "format": output_format,
        "time_s": round(time.time() - t0, 1),
    }


if __name__ == "__main__":
    try:
        import argparse
        import json
        ap = argparse.ArgumentParser()
        ap.add_argument("url")
        ap.add_argument("--format", default="markdown")
        args = ap.parse_args()
        r = stealth_scrape(args.url, output_format=args.format)
        # 只 print stats · 内容单独输出防淹没
        stats = {k: v for k, v in r.items() if k != "content"}
        stats["content_len"] = len(r.get("content", ""))
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        if r.get("ok"):
            print("--- CONTENT (first 500) ---")
            print(r["content"][:500])
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise
