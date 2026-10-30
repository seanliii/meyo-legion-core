"""
服务端搜索引擎 v6.0 — 15源深度搜索 + Exa语义搜索
ArXiv / GitHub / README / HackerNews / DEV.to / Bing / CSDN / 学城 /
觅游Skill / 觅游热帖 / Bing近30天 / Google近30天 / 全文提取 / LLM / Exa
"""
import os
import requests
import json
import time
import re
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import quote_plus
from llm_client import LLMClient


class SearchEngine:
    def __init__(self, aisa_key: str = "", exa_key: str = ""):
        self.llm = LLMClient(aisa_key)
        self.meyo_base = "https://www.meyo123.com/api/v1"
        # Exa API key: 优先环境变量，其次传入参数，最后从config读
        self.exa_key = os.environ.get("EXA_API_KEY", "") or exa_key
        if not self.exa_key:
            try:
                import json
                cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
                with open(cfg_path) as f:
                    self.exa_key = json.load(f).get("exa_api_key", "")
            except Exception:
                pass

    def _get(self, url, **kw):
        # v8.4: 默认 6 秒（原 10s），避免慢源拖累整体搜索
        kw.setdefault("timeout", 6)
        try:
            r = requests.get(url, **kw)
            return r if r.status_code == 200 else None
        except Exception:
            return None

    # === 1. ArXiv论文 ===
    def search_arxiv(self, query: str, num: int = 3) -> list:
        """搜索最新AI Agent相关论文"""
        try:
            r = self._get("https://export.arxiv.org/api/query", params={
                "search_query": f"all:{query}",
                "max_results": num,
                "sortBy": "relevance",
                "sortOrder": "descending"
            })
            if not r:
                return []
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            root = ET.fromstring(r.text)
            results = []
            for entry in root.findall("atom:entry", ns):
                title = entry.find("atom:title", ns).text.strip().replace("\n", " ")
                link = entry.find("atom:id", ns).text
                summary = entry.find("atom:summary", ns).text.strip()[:200]
                published = entry.find("atom:published", ns).text[:10]
                results.append({
                    "title": title, "url": link,
                    "content": summary, "date": published,
                    "source": "arxiv"
                })
            if results:
                print(f"  ✓ ArXiv: {len(results)} 论文 ({results[0]['title'][:40]}...)")
            return results
        except Exception as e:
            print(f"  ✗ ArXiv: {e}")
            return []

    # === 2. GitHub ===
    def search_github(self, query: str, num: int = 3) -> list:
        try:
            r = self._get("https://api.github.com/search/repositories",
                params={"q": query, "sort": "stars", "order": "desc", "per_page": num},
                headers={"Accept": "application/vnd.github.v3+json"})
            if not r:
                return []
            results = [{
                "title": i.get("full_name", ""), "url": i.get("html_url", ""),
                "content": i.get("description", "") or "",
                "stars": i.get("stargazers_count", 0), "source": "github"
            } for i in r.json().get("items", [])[:num]]
            if results:
                print(f"  ✓ GitHub: {len(results)} ({results[0]['title']} ⭐{results[0]['stars']})")
            return results
        except Exception as e:
            print(f"  ✗ GitHub: {e}")
            return []

    # === v8.1: GitHub trending（近30天新建+高星，国外最新风向）===
    def search_github_trending(self, query: str, num: int = 3) -> list:
        try:
            from datetime import datetime as _dt, timedelta as _td
            since = (_dt.utcnow() - _td(days=30)).strftime("%Y-%m-%d")
            r = self._get("https://api.github.com/search/repositories",
                params={"q": f"{query} created:>{since}", "sort": "stars", "order": "desc", "per_page": num},
                headers={"Accept": "application/vnd.github.v3+json"})
            if not r:
                return []
            results = [{
                "title": i.get("full_name", ""), "url": i.get("html_url", ""),
                "content": i.get("description", "") or "",
                "stars": i.get("stargazers_count", 0), "source": "github_trending"
            } for i in r.json().get("items", [])[:num]]
            if results:
                print(f"  ✓ GitHub近30天: {len(results)} ({results[0]['title']} ⭐{results[0]['stars']})")
            return results
        except Exception as e:
            print(f"  ✗ GitHub近30天: {e}")
            return []

    # === v8.2: ClawHub skills（OpenClaw 生态 skill 市场，最新发布）===
    def search_clawhub_skills(self, query: str, num: int = 5) -> list:
        try:
            r = self._get("https://clawhub.ai/api/v1/skills",
                headers={"Accept": "application/json", "User-Agent": "shrimp-bot/8.2"})
            if not r:
                return []
            items = r.json().get("items", [])
            # 按 updatedAt 倒排（最新的在前）
            items.sort(key=lambda x: x.get("updatedAt", 0), reverse=True)
            kw = (query or "").lower().split()
            results = []
            for s in items:
                slug = s.get("slug", "")
                name = s.get("displayName", "") or slug
                summary = s.get("summary", "") or ""
                changelog = (s.get("latestVersion", {}) or {}).get("changelog", "") or ""
                # 关键词软匹配：name/summary 命中任一词即收录
                text = (name + " " + summary).lower()
                if kw and not any(w in text for w in kw if len(w) > 2):
                    continue
                results.append({
                    "title": f"[ClawHub Skill] {name}",
                    "url": f"https://clawhub.ai/skills/{slug}",
                    "content": (summary + ("\n更新: " + changelog if changelog else ""))[:300],
                    "stars": (s.get("stats", {}) or {}).get("stars", 0),
                    "version": (s.get("latestVersion", {}) or {}).get("version", ""),
                    "source": "clawhub"
                })
                if len(results) >= num:
                    break
            # 关键词没匹配到时回退给前 num 个最新 skill
            if not results:
                for s in items[:num]:
                    slug = s.get("slug", "")
                    name = s.get("displayName", "") or slug
                    summary = s.get("summary", "") or ""
                    results.append({
                        "title": f"[ClawHub Skill] {name}",
                        "url": f"https://clawhub.ai/skills/{slug}",
                        "content": summary[:300],
                        "stars": (s.get("stats", {}) or {}).get("stars", 0),
                        "source": "clawhub"
                    })
            if results:
                print(f"  ✓ ClawHub: {len(results)} ({results[0]['title'][:50]})")
            return results
        except Exception as e:
            print(f"  ✗ ClawHub: {e}")
            return []

    # === v8.2: Reddit r/ClaudeAI + r/codex + r/OpenAI 多sub汇总 ===
    def search_reddit_aihubs(self, query: str, num: int = 5) -> list:
        """专挑 Claude/Codex/AI 相关 subreddit，最近一周高赞"""
        subs = ["ClaudeAI", "codex", "OpenAI", "LocalLLaMA", "singularity"]
        all_results = []
        try:
            for sub in subs[:3]:  # 先打 3 个最相关的，避免太慢
                try:
                    url = f"https://www.reddit.com/r/{sub}/top.json?t=week&limit=10"
                    r = self._get(url, headers={"User-Agent": "Mozilla/5.0 shrimp-bot/8.2"})
                    if not r:
                        continue
                    for c in r.json().get("data", {}).get("children", [])[:5]:
                        d = c.get("data", {})
                        if d.get("over_18") or d.get("score", 0) < 20:
                            continue
                        all_results.append({
                            "title": f"[r/{sub}] {d.get('title','')}",
                            "url": f"https://reddit.com{d.get('permalink','')}",
                            "content": (d.get("selftext", "") or "")[:200],
                            "score": d.get("score", 0),
                            "subreddit": sub,
                            "source": "reddit_aihubs"
                        })
                except Exception:
                    continue
            # 按score倒排，取前num
            all_results.sort(key=lambda x: x["score"], reverse=True)
            results = all_results[:num]
            if results:
                print(f"  ✓ Reddit AI圈: {len(results)} (r/{results[0]['subreddit']} {results[0]['score']}分)")
            return results
        except Exception as e:
            print(f"  ✗ Reddit AI圈: {e}")
            return []

    # === v8.3: Lobsters — 程序员社区 hottest.json（Claude/AI 高质量讨论）===
    def search_lobsters(self, query: str, num: int = 3) -> list:
        try:
            r = self._get("https://lobste.rs/hottest.json",
                headers={"User-Agent": "shrimp-bot/8.3"})
            if not r:
                return []
            items = r.json()
            kw = [w.lower() for w in (query or "").split() if len(w) > 2]
            hits = []
            for x in items:
                title = x.get("title", "") or ""
                tags = " ".join(x.get("tags", []) or [])
                text = (title + " " + tags).lower()
                # 优先关键词匹配，兜底取热门条目
                if kw and any(w in text for w in kw):
                    hits.append(x)
            if not hits:
                hits = items[:num*2]
            results = [{
                "title": x.get("title", ""),
                "url": x.get("url") or x.get("short_id_url") or f"https://lobste.rs/s/{x.get('short_id','')}",
                "content": (x.get("description", "") or "")[:200],
                "score": x.get("score", 0),
                "tags": ",".join(x.get("tags", []) or []),
                "source": "lobsters"
            } for x in hits[:num]]
            if results:
                print(f"  ✓ Lobsters: {len(results)} ({results[0]['title'][:50]} {results[0]['score']}分)")
            return results
        except Exception as e:
            print(f"  ✗ Lobsters: {e}")
            return []

    # === v8.3: Anthropic 官方博客（Claude 权威一手更新）===
    def search_anthropic_news(self, query: str = "", num: int = 3) -> list:
        try:
            r = self._get("https://www.anthropic.com/news",
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
            if not r:
                return []
            html = r.text
            # 抽 news 卡片: <a href="/news/xxx"> ... <h3>title</h3>
            entries = re.findall(r'href="(/news/[^"]+)"[^>]*>[\s\S]{0,600}?<h[23][^>]*>([^<]{5,180})</h[23]>', html)
            seen = set()
            results = []
            for path, title in entries:
                if path in seen:
                    continue
                seen.add(path)
                title = re.sub(r"\s+", " ", title).strip()
                results.append({
                    "title": f"[Anthropic] {title}",
                    "url": f"https://www.anthropic.com{path}",
                    "content": "",
                    "source": "anthropic_news"
                })
                if len(results) >= num:
                    break
            if results:
                print(f"  ✓ Anthropic官方: {len(results)} ({results[0]['title'][:60]})")
            return results
        except Exception as e:
            print(f"  ✗ Anthropic官方: {e}")
            return []

    # === v8.4: SkillHub.cn（腾讯云中文 skill 社区，与 ClawHub 同源），只能拿匿名可读的 showcase/paid ===
    def search_skillhub_cn(self, query: str, num: int = 3) -> list:
        try:
            r = self._get("https://api.skillhub.cn/api/v1/showcase/paid",
                headers={"Accept": "application/json", "User-Agent": "shrimp-bot/8.4"})
            if not r:
                return []
            data = r.json()
            merchants = data.get("featured_merchants", []) or []
            skills = data.get("featured_paid_skills", []) or []
            results = []
            for s in skills[:num*2]:
                name = s.get("displayName") or s.get("name") or s.get("slug", "")
                slug = s.get("slug", "")
                summary = s.get("summary", "") or s.get("description", "") or ""
                if not name:
                    continue
                results.append({
                    "title": f"[SkillHub精选] {name}",
                    "url": f"https://skillhub.cn/skills/{slug}" if slug else "https://skillhub.cn",
                    "content": summary[:250],
                    "source": "skillhub_cn"
                })
                if len(results) >= num:
                    break
            # 兜底：featured skills 空时把商家也算上
            if not results and merchants:
                for m in merchants[:num]:
                    name = m.get("displayName") or m.get("name", "")
                    if not name:
                        continue
                    results.append({
                        "title": f"[SkillHub商家] {name}",
                        "url": "https://skillhub.cn",
                        "content": (m.get("description", "") or "")[:250],
                        "source": "skillhub_cn"
                    })
            if results:
                print(f"  ✓ SkillHub.cn: {len(results)} ({results[0]['title'][:50]})")
            return results
        except Exception as e:
            print(f"  ✗ SkillHub.cn: {e}")
            return []

    # === v8.3: HN 按时间倒排（最新的 Claude/Codex/Agent 讨论）+ v8.4 空结果 fallback ===
    def search_hn_recent(self, query: str, num: int = 3) -> list:
        def _fetch(q):
            r = self._get("https://hn.algolia.com/api/v1/search_by_date",
                params={"query": q, "tags": "story", "hitsPerPage": num*2})
            if not r:
                return []
            return r.json().get("hits", [])
        try:
            hits = _fetch(query)
            # v8.4: 首次为空 → 用宽泛热词兜底（保证HN每轮都有素材）
            if not hits:
                for fb in ["claude code", "AI agent", "codex cli", "LLM"]:
                    hits = _fetch(fb)
                    if hits:
                        break
            results = []
            for h in hits:
                title = h.get("title", "") or ""
                if not title:
                    continue
                results.append({
                    "title": f"[HN最新] {title}",
                    "url": h.get("url", "") or f"https://news.ycombinator.com/item?id={h.get('objectID','')}",
                    "content": (h.get("story_text", "") or "")[:200],
                    "points": h.get("points", 0),
                    "source": "hn_recent"
                })
                if len(results) >= num:
                    break
            if results:
                print(f"  ✓ HN最新: {len(results)} ({results[0]['title'][:60]})")
            return results
        except Exception as e:
            print(f"  ✗ HN最新: {e}")
            return []

    def search_github_readme(self, repo: str) -> str:
        try:
            r = self._get(f"https://api.github.com/repos/{repo}/readme",
                headers={"Accept": "application/vnd.github.v3.raw"})
            if r:
                print(f"  ✓ README: {repo} ({len(r.text[:2000])}字)")
                return r.text[:2000]
        except Exception:
            pass
        return ""

    # === 3. HackerNews ===
    def search_hackernews(self, query: str, num: int = 3) -> list:
        try:
            r = self._get("https://hn.algolia.com/api/v1/search",
                params={"query": query, "tags": "story", "hitsPerPage": num})
            if not r:
                return []
            results = [{
                "title": h.get("title", ""),
                "url": h.get("url", "") or f"https://news.ycombinator.com/item?id={h.get('objectID','')}",
                "points": h.get("points", 0), "source": "hackernews"
            } for h in r.json().get("hits", [])[:num]]
            if results:
                print(f"  ✓ HN: {len(results)} ({results[0]['title'][:40]})")
            return results
        except Exception as e:
            print(f"  ✗ HN: {e}")
            return []

    # === 4. DEV.to技术博客 ===
    def search_devto(self, query: str, num: int = 3) -> list:
        try:
            r = self._get("https://dev.to/api/articles",
                params={"tag": query.replace(" ", "-").lower(), "top": 30, "per_page": num})
            if not r:
                # fallback搜索
                r = self._get("https://dev.to/api/articles",
                    params={"tag": "ai", "top": 7, "per_page": num})
                if not r:
                    return []
            results = [{
                "title": a.get("title", ""),
                "url": f"https://dev.to{a.get('path','')}",
                "content": a.get("description", "")[:150],
                "reactions": a.get("positive_reactions_count", 0),
                "source": "devto"
            } for a in r.json()[:num]]
            if results:
                print(f"  ✓ DEV.to: {len(results)} ({results[0]['title'][:40]})")
            return results
        except Exception as e:
            print(f"  ✗ DEV.to: {e}")
            return []

    # === 5. Bing搜索 ===
    def search_bing(self, query: str, num: int = 5) -> list:
        """Bing搜索（实测稳定可用）"""
        try:
            r = requests.get(f"https://www.bing.com/search?q={quote_plus(query)}&count={num}",
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
                timeout=8)
            if r.status_code != 200:
                return []
            titles = re.findall(r'<h2[^>]*><a[^>]*>(.*?)</a></h2>', r.text)
            links = re.findall(r'<h2[^>]*><a[^>]*href="(https?://[^"]+)"', r.text)
            results = []
            for i in range(min(len(titles), len(links), num)):
                title = re.sub(r'<[^>]+>', '', titles[i]).strip()
                if title and 'bing.com' not in links[i]:
                    results.append({"title": title, "url": links[i], "source": "bing"})
            if results:
                print(f"  ✓ Bing: {len(results)} ({results[0]['title'][:40]})")
            return results
        except Exception as e:
            print(f"  ✗ Bing: {e}")
            return []

    # === 6. CSDN技术文章 ===
    def search_csdn(self, query: str, num: int = 5) -> list:
        """CSDN搜索（中文AI实战文章最丰富的来源）"""
        try:
            r = requests.get(
                f"https://so.csdn.net/api/v3/search?q={quote_plus(query)}&t=all&p=1&s=0&tm=0",
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                timeout=8)
            if r.status_code != 200:
                return []
            items = r.json().get("result_vos", [])
            results = []
            for item in items[:num]:
                title = re.sub(r'<[^>]+>', '', item.get("title", "")).strip()
                url = item.get("url", "")
                # 清理CSDN的tracking参数
                if "?" in url:
                    url = url.split("?")[0]
                desc = re.sub(r'<[^>]+>', '', item.get("description", "")).strip()[:150]
                if title and url:
                    results.append({"title": title, "url": url, "content": desc, "source": "csdn"})
            if results:
                print(f"  ✓ CSDN: {len(results)} ({results[0]['title'][:40]})")
            return results
        except Exception as e:
            print(f"  ✗ CSDN: {e}")
            return []

    # === 7. 觅游Skill ===
    def search_meyo_skills(self, keyword: str = "") -> list:
        try:
            r = self._get(f"{self.meyo_base}/skills", params={"limit": 50})
            if not r:
                return []
            seen, results = set(), []
            for s in r.json().get("data", {}).get("list", []):
                name = s.get("name", "")
                if name in seen:
                    continue
                seen.add(name)
                if keyword and keyword.lower() not in (name + " " + s.get("description", "")).lower():
                    continue
                results.append({"title": name, "alias": s.get("alias", ""),
                    "content": s.get("description", "")[:120],
                    "downloads": s.get("downloadCount", 0), "source": "meyo_skill"})
            if results:
                print(f"  ✓ Skills: {len(results)}")
            return results[:6]
        except Exception:
            return []

    # === 8. 觅游热帖 ===
    def search_meyo_hot(self, limit: int = 5) -> list:
        try:
            r = self._get(f"{self.meyo_base}/feeds", params={"limit": limit, "sort": "hot"})
            if not r:
                return []
            results = [{"title": f.get("title", ""), "upvotes": f.get("upvotes", 0), "source": "meyo"}
                       for f in r.json().get("data", {}).get("list", []) if f.get("upvotes", 0) >= 3]
            if results:
                print(f"  ✓ 热帖: {len(results)}")
            return results
        except Exception:
            return []

    # === 9. 学城(公司内部知识库) ===
    def search_citadel(self, query: str, num: int = 3) -> list:
        """搜索学城文档（内部技术知识）"""
        try:
            r = subprocess.run(
                ["oa-skills", "citadel", "searchContent",
                 "--keyword", query, "--limit", str(num), "--mis", "lixuan54"],
                capture_output=True, text=True, timeout=15
            )
            if r.returncode != 0:
                return []
            results = []
            # 解析输出中的标题和链接
            for line in r.stdout.split("\n"):
                if "collabpage" in line:
                    url_match = re.search(r'(https://km\.sankuai\.com/collabpage/\d+)', line)
                    if url_match:
                        title = line.split("》")[0].split("《")[-1] if "《" in line else line[:50]
                        results.append({"title": title.strip(), "url": url_match.group(1), "source": "citadel"})
            if results:
                print(f"  ✓ 学城: {len(results)} ({results[0]['title'][:30]})")
            return results[:num]
        except Exception as e:
            print(f"  ✗ 学城: {e}")
            return []

    # === 10. Bing近30天搜索 ===
    def search_bing_recent(self, query: str, num: int = 5) -> list:
        """Bing搜索限制近30天结果（高时效性素材）"""
        try:
            # 使用 filters=ex1:"ez5" 限制近30天
            url = f"https://www.bing.com/search?q={quote_plus(query)}&count={num}&filters=ex1%3a%22ez5%22"
            r = requests.get(url,
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
                timeout=8)
            if r.status_code != 200:
                return []
            titles = re.findall(r'<h2[^>]*><a[^>]*>(.*?)</a></h2>', r.text)
            links = re.findall(r'<h2[^>]*><a[^>]*href="(https?://[^"]+)"', r.text)
            results = []
            for i in range(min(len(titles), len(links), num)):
                title = re.sub(r'<[^>]+>', '', titles[i]).strip()
                if title and 'bing.com' not in links[i]:
                    results.append({"title": title, "url": links[i],
                                    "source": "bing_recent", "recency": "30days"})
            if results:
                print(f"  ✓ Bing近30天: {len(results)} ({results[0]['title'][:40]})")
            return results
        except Exception as e:
            print(f"  ✗ Bing近30天: {e}")
            return []

    # === 11. Google近30天搜索 ===
    def search_google_recent(self, query: str, num: int = 5) -> list:
        """Google搜索限制近30天结果"""
        try:
            # tbs=qdr:m 限制近一个月
            url = f"https://www.google.com/search?q={quote_plus(query)}&num={num}&tbs=qdr:m"
            r = requests.get(url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
                },
                timeout=8)
            if r.status_code != 200:
                return []
            # Google结果解析
            titles = re.findall(r'<h3[^>]*>(.*?)</h3>', r.text)
            links = re.findall(r'<a[^>]*href="/url\?q=(https?://[^&"]+)', r.text)
            if not links:
                links = re.findall(r'href="(https?://(?!www\.google)[^"]+)"', r.text)
            results = []
            for i in range(min(len(titles), len(links), num)):
                title = re.sub(r'<[^>]+>', '', titles[i]).strip()
                if title and 'google.com' not in links[i]:
                    results.append({"title": title, "url": links[i],
                                    "source": "google_recent", "recency": "30days"})
            if results:
                print(f"  ✓ Google近30天: {len(results)} ({results[0]['title'][:40]})")
            return results
        except Exception as e:
            print(f"  ✗ Google近30天: {e}")
            return []

    # === 12. last30days搜索（跨平台社交+技术内容，近30天）===
    def search_last30days(self, query: str) -> list:
        """调用last30days skill的核心搜索（GitHub+Exa+Web）"""
        try:
            script = os.path.join(os.path.expanduser("~"),
                                  ".claude/skills/last30days/scripts/last30days.py")
            if not os.path.exists(script):
                return []
            env = {**os.environ, "EXA_API_KEY": self.exa_key} if self.exa_key else os.environ.copy()
            proc = subprocess.run(
                ["python3", script, query, "--emit", "json", "--quick",
                 "--web-backend", "exa" if self.exa_key else "none"],
                capture_output=True, text=True, timeout=30, env=env
            )
            if proc.returncode != 0:
                return []
            # 解析JSON输出
            output = proc.stdout
            first = output.find('{')
            if first == -1:
                return []
            data = json.loads(output[first:])
            results = []
            for item in data.get("items", data.get("results", []))[:5]:
                title = item.get("title", "") or item.get("headline", "")
                url = item.get("url", "") or item.get("link", "")
                body = item.get("body", "") or item.get("summary", "") or item.get("text", "")
                if title:
                    results.append({
                        "title": title, "url": url,
                        "content": body[:200],
                        "source": "last30days",
                        "recency": "30days",
                    })
            if results:
                print(f"  ✓ last30days: {len(results)} ({results[0]['title'][:40]})")
            return results
        except subprocess.TimeoutExpired:
            print(f"  ✗ last30days: 超时")
            return []
        except Exception as e:
            print(f"  ✗ last30days: {e}")
            return []

    # === 13. Exa语义搜索（高质量AI内容，近30天）===
    def search_exa(self, query: str, num: int = 5) -> list:
        """Exa语义搜索 — 找到真正相关的高质量页面（含全文摘要）"""
        if not self.exa_key:
            return []
        try:
            from exa_py import Exa
            exa = Exa(api_key=self.exa_key)
            from datetime import timedelta as _td
            response = exa.search(
                query,
                num_results=num,
                start_published_date=(datetime.now() - _td(days=30)).strftime("%Y-%m-%dT00:00:00.000Z"),
                contents={"text": {"max_characters": 500}},
            )
            results = []
            for r in response.results:
                results.append({
                    "title": r.title or "",
                    "url": r.url or "",
                    "content": (r.text or "")[:300],
                    "source": "exa",
                    "recency": "30days",
                })
            if results:
                print(f"  ✓ Exa: {len(results)} ({results[0]['title'][:40]})")
            return results
        except ImportError:
            print(f"  ✗ Exa: exa_py未安装")
            return []
        except Exception as e:
            print(f"  ✗ Exa: {e}")
            return []

    # === 13. 全文内容提取（Scrapling优先，requests fallback）===
    def fetch_article_content(self, url: str, max_chars: int = 2000) -> str:
        """抓取文章全文（Scrapling反指纹 > requests fallback）"""
        # 方案1: Scrapling（反指纹，更不容易被封）
        try:
            from scrapling import Fetcher
            f = Fetcher()
            r = f.get(url, timeout=10)
            if r.status and r.html_content:
                text = re.sub(r'<script[^>]*>.*?</script>', '', r.html_content, flags=re.DOTALL)
                text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
                text = re.sub(r'<[^>]+>', ' ', text)
                text = re.sub(r'\s+', ' ', text).strip()
                if len(text) > 500:
                    text = text[200:200+max_chars]
                return text[:max_chars]
        except Exception:
            pass

        # 方案2: requests fallback
        try:
            r = requests.get(url,
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
                timeout=10)
            if r.status_code != 200:
                return ""
            text = re.sub(r'<script[^>]*>.*?</script>', '', r.text, flags=re.DOTALL)
            text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()
            if len(text) > 500:
                text = text[200:200+max_chars]
            return text[:max_chars]
        except Exception:
            return ""

    # === 13. LLM深度分析 ===
    def search_llm_deep(self, query: str, references: list) -> str:
        """让LLM基于已收集的引用做深度分析"""
        ref_text = "\n".join([
            f"- [{r.get('title','')}]({r.get('url','')}) [{r.get('source','')}]"
            for r in references[:10] if r.get("title")
        ])
        prompt = f"""基于以下搜索到的真实资料，针对「{query}」做一个深度技术分析。

## 已搜集的资料
{ref_text}

## 要求
1. 综合所有资料，提炼出3-4个最关键的技术要点
2. 每个要点要引用具体的资料来源（用[来源名]标注）
3. 指出最常见的3个坑和对应的解法
4. 给出一个可操作的最小实践步骤（5步以内）
5. 不确定的内容标注"待验证"
6. 中文回答，800字以内"""

        content = self.llm.generate(prompt, max_tokens=2000)
        if content:
            print(f"  ✓ LLM深度分析: {len(content)}字")
        return content or ""

    # === 统一搜索入口（v4.0: 并行搜索加速）===
    def run_topic_search(self, topic: dict) -> dict:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        queries = topic.get("queries", [])
        topic_name = topic.get("name", "")

        print(f"\n🔍 搜索: {topic_name[:50]}")
        result = {
            "topic_id": topic.get("id", ""),
            "topic_name": topic_name,
            "timestamp": datetime.now().isoformat(),
            "target_user_types": topic.get("target_user_types", []),
            "web_results": [], "github_repos": [], "meyo_skills": [],
            "meyo_hot_posts": [], "arxiv_papers": [],
            "perplexity_insight": {"content": "", "citations": []},
        }

        en_q = queries[0] if queries else "AI agent"
        cn_q = topic_name if any('一' <= c <= '鿿' for c in topic_name) else ""
        en_words = " ".join(w for w in en_q.split() if w.isascii() and len(w) > 2)[:50] or "AI agent"
        bing_q = cn_q or en_q
        csdn_q = cn_q or en_q

        # v6.2: 注入成长搜索查询（如果有的话）
        try:
            from growth_searcher import get_growth_queries
            growth_q = get_growth_queries(topic_name, "all")
            if growth_q:
                queries = list(queries) + growth_q[:2]
        except Exception:
            pass

        all_refs = []

        # === 并行搜索（所有网络请求同时发出）===
        search_tasks = {
            "arxiv": lambda: self.search_arxiv(en_words.replace(" ", "+"), 3),
            "github": lambda: self.search_github(en_words, 3),
            "github_trending": lambda: self.search_github_trending(en_words, 3),  # v8.1: 国外最新风向
            "hackernews": lambda: self.search_hackernews(en_q, 3),                # v8.1: HN搜索
            "clawhub": lambda: self.search_clawhub_skills(en_words, 5),           # v8.2: OpenClaw skill市场
            "csdn_claude": lambda: self.search_csdn(f"{csdn_q} Claude Code skill", 3),  # v8.2: CC实战
            "csdn_codex": lambda: self.search_csdn(f"{csdn_q} Codex CLI 工作流", 3),    # v8.2: Codex实战
            "lobsters": lambda: self.search_lobsters(en_q, 3),                    # v8.3: 程序员社区高质量讨论（替代reddit）
            "anthropic_news": lambda: self.search_anthropic_news("", 3),          # v8.3: Claude官方最新
            "hn_recent": lambda: self.search_hn_recent(f"claude code {en_q}"[:60], 3),  # v8.3: HN按时间搜Claude/Codex
            "skillhub_cn": lambda: self.search_skillhub_cn(en_words, 3),          # v8.4: 腾讯云SkillHub中文精选
            "bing": lambda: self.search_bing(f"{bing_q} AI Agent 实战 2026", 3),
            "csdn": lambda: self.search_csdn(f"{csdn_q} AI Agent", 3),
            "bing_recent": lambda: self.search_bing_recent(f"{bing_q} AI 实战 经验", 3),
            "google_recent": lambda: self.search_google_recent(f"{bing_q} AI Agent 2026", 3),
            "exa": lambda: self.search_exa(f"{en_q} practical tutorial", 5),
            "devto": lambda: self.search_devto("ai-agents", 3),
            "last30days": lambda: self.search_last30days(en_q),
            "meyo_skills": lambda: self.search_meyo_skills(topic.get("id", "").replace("_", " ")),
            "meyo_hot": lambda: self.search_meyo_hot(5),
        }

        # v8.4: 硬性 deadline — 单个 topic 的所有并行搜索最多等 15s（原来是等最慢那路，可能 30s+）
        search_results = {name: [] for name in search_tasks}
        SEARCH_DEADLINE_SEC = 15
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = {executor.submit(fn): name for name, fn in search_tasks.items()}
            try:
                for future in as_completed(futures, timeout=SEARCH_DEADLINE_SEC):
                    name = futures[future]
                    try:
                        search_results[name] = future.result()
                    except Exception as e:
                        print(f"  ✗ {name}: {e}")
                        search_results[name] = []
            except Exception:
                # deadline 到了 → 放弃剩下没跑完的
                incomplete = [futures[f] for f in futures if not f.done()]
                if incomplete:
                    print(f"  ⏱️  搜索deadline到，跳过未完成: {','.join(incomplete[:5])}")

        # 整理结果
        result["arxiv_papers"] = search_results.get("arxiv", [])
        all_refs.extend(result["arxiv_papers"])

        result["github_repos"] = search_results.get("github", [])
        all_refs.extend(result["github_repos"])

        # README（依赖github结果，串行）
        readme = ""
        if result["github_repos"]:
            readme = self.search_github_readme(result["github_repos"][0]["title"])

        # v8.3: 拆reddit两路（网络不通）+ 加 lobsters/anthropic_news/hn_recent 三路可达源
        for key in ["bing", "csdn", "bing_recent", "google_recent", "exa", "devto", "last30days",
                    "hackernews", "github_trending",
                    "clawhub", "csdn_claude", "csdn_codex",
                    "lobsters", "anthropic_news", "hn_recent", "skillhub_cn"]:
            items = search_results.get(key, [])
            result["web_results"].extend(items)
            all_refs.extend(items)

        result["meyo_skills"] = search_results.get("meyo_skills", [])
        result["meyo_hot_posts"] = search_results.get("meyo_hot", [])

        bing_recent = search_results.get("bing_recent", [])
        google_recent = search_results.get("google_recent", [])
        exa_results = search_results.get("exa", [])

        # 全文提取（对Exa或近30天最佳结果读全文）
        article_contents = []
        fetch_targets = exa_results + bing_recent + google_recent
        for target in fetch_targets[:2]:
            url = target.get("url", "")
            if url and "bing.com" not in url and "google.com" not in url:
                content = self.fetch_article_content(url, 1500)
                if content and len(content) > 200:
                    article_contents.append({
                        "title": target.get("title", ""),
                        "url": url,
                        "content": content,
                    })
                    print(f"  ✓ 全文: {target['title'][:30]}... ({len(content)}字)")

        # LLM深度分析（串行，依赖所有搜索结果）
        llm_analysis = self.search_llm_deep(en_q, all_refs)

        # === 汇总素材 + 带链接引用 ===
        parts = []

        if result["arxiv_papers"]:
            papers = "\n".join([
                f"- 📄 [{p['title']}]({p['url']}) ({p.get('date','')}) — {p.get('content','')[:100]}"
                for p in result["arxiv_papers"]])
            parts.append(f"### 最新论文\n{papers}")

        if result["github_repos"]:
            repos = "\n".join([
                f"- 🔧 [{r['title']}]({r['url']}) ⭐{r.get('stars',0)} — {r['content']}"
                for r in result["github_repos"]])
            parts.append(f"### GitHub项目\n{repos}")

        if readme:
            parts.append(f"### 核心项目详情\n{readme[:1200]}")

        # 各搜索源结果汇总（从并行结果中取）
        bing_items = search_results.get("bing", [])
        csdn_items = search_results.get("csdn", [])
        devto_items = search_results.get("devto", [])

        if devto_items:
            devs = "\n".join([f"- 📝 [{d['title']}]({d['url']}) ❤️{d.get('reactions',0)}" for d in devto_items])
            parts.append(f"### 技术博客\n{devs}")

        if bing_items:
            bings = "\n".join([f"- 🔎 [{b['title']}]({b.get('url','')})" for b in bing_items if b.get("url")])
            if bings:
                parts.append(f"### Bing搜索\n{bings}")

        if csdn_items:
            csdns = "\n".join([f"- 📖 [{c['title']}]({c['url']}) — {c.get('content','')[:60]}" for c in csdn_items])
            parts.append(f"### CSDN技术文章\n{csdns}")

        # 近30天搜索结果（高时效性）
        recent_results = [r for r in result["web_results"] if r.get("recency") == "30days"]
        if recent_results:
            recents = "\n".join([f"- 🕐 [{r['title']}]({r.get('url','')})" for r in recent_results if r.get("url")])
            if recents:
                parts.append(f"### 近30天最新内容\n{recents}")

        if result["meyo_hot_posts"]:
            hots = "\n".join([f"- 🔥 {p['title'][:50]} (赞{p['upvotes']})" for p in result["meyo_hot_posts"][:5]])
            parts.append(f"### 社区热帖\n{hots}")

        # 全文内容（真实案例细节）
        if article_contents:
            for ac in article_contents:
                parts.append(f"### 📄 全文: {ac['title'][:40]}\n来源: {ac['url']}\n{ac['content'][:1200]}")

        if llm_analysis:
            parts.append(f"### 深度分析\n{llm_analysis}")

        combined = "\n\n".join(parts)
        citations = [r["url"] for r in all_refs if r.get("url")]

        result["perplexity_insight"] = {"content": combined, "citations": citations}

        total = (len(result["arxiv_papers"]) + len(result["github_repos"]) +
                 len(result["web_results"]) + len(result["meyo_skills"]) +
                 len(result["meyo_hot_posts"]) + (1 if llm_analysis else 0))
        print(f"  📦 完成: {total}来源 {len(combined)}字 {len(citations)}引用")
        return result
