"""
Agent-Reach 多平台搜索适配器 v5.0
封装 agent-reach CLI 工具，提供 Twitter/小红书/Reddit/YouTube 搜索能力
GitHub: https://github.com/Panniantong/Agent-Reach
本地 Skill: /Users/xuan/clawd/skills/agent-reach/
"""
import json
import os
import subprocess
from typing import List, Dict, Optional


class AgentReachAdapter:
    """统一封装 agent-reach 各平台搜索，返回标准化结果"""

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self._available = self._check_available()

    def _check_available(self) -> dict:
        """检测各平台 CLI 工具可用性"""
        tools = {
            "xreach": self._cmd_exists("xreach"),
            "mcporter": self._cmd_exists("mcporter"),
            "jina_reader": True,
            "yt_dlp": self._cmd_exists("yt-dlp"),
        }
        return tools

    def _cmd_exists(self, cmd: str) -> bool:
        try:
            subprocess.run(["which", cmd], capture_output=True, timeout=5)
            return True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def _run_cmd(self, cmd: list, timeout: int = None) -> Optional[str]:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True, text=True,
                timeout=timeout or self.timeout
            )
            if result.returncode == 0:
                return result.stdout
            return None
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None

    def search_twitter(self, query: str, limit: int = 10) -> List[Dict]:
        """搜索 Twitter/X（通过 xreach CLI）"""
        if not self._available.get("xreach"):
            return []

        output = self._run_cmd(["xreach", "search", query, "-n", str(limit), "--json"])
        if not output:
            return []

        try:
            tweets = json.loads(output)
            return [{
                "source": "twitter",
                "title": t.get("text", "")[:100],
                "content": t.get("text", ""),
                "url": t.get("url", ""),
                "author": t.get("author", ""),
                "engagement": t.get("likes", 0) + t.get("retweets", 0),
            } for t in (tweets if isinstance(tweets, list) else [])][:limit]
        except json.JSONDecodeError:
            return []

    def search_xiaohongshu(self, query: str, limit: int = 5) -> List[Dict]:
        """搜索小红书（通过 mcporter MCP 调用）"""
        if not self._available.get("mcporter"):
            return []

        output = self._run_cmd([
            "mcporter", "call",
            f'xiaohongshu.search_feeds(keyword: "{query}")'
        ])
        if not output:
            return []

        try:
            results = json.loads(output) if output.strip().startswith("[") else []
            return [{
                "source": "xiaohongshu",
                "title": r.get("title", ""),
                "content": r.get("desc", ""),
                "url": r.get("url", ""),
                "author": r.get("nickname", ""),
                "engagement": r.get("likes", 0),
            } for r in results][:limit]
        except (json.JSONDecodeError, TypeError):
            return []

    def search_reddit(self, query: str, limit: int = 5) -> List[Dict]:
        """搜索 Reddit（通过 JSON API）"""
        import requests
        try:
            resp = requests.get(
                f"https://www.reddit.com/search.json",
                params={"q": query, "sort": "relevance", "limit": limit},
                headers={"User-Agent": "MeyoBot/1.0"},
                timeout=10
            )
            if resp.status_code != 200:
                return []

            posts = resp.json().get("data", {}).get("children", [])
            return [{
                "source": "reddit",
                "title": p["data"].get("title", ""),
                "content": p["data"].get("selftext", "")[:500],
                "url": f"https://reddit.com{p['data'].get('permalink', '')}",
                "author": p["data"].get("author", ""),
                "engagement": p["data"].get("score", 0),
            } for p in posts][:limit]
        except Exception:
            return []

    def read_url(self, url: str) -> Optional[str]:
        """通过 Jina Reader 读取任意网页内容"""
        import requests
        try:
            resp = requests.get(
                f"https://r.jina.ai/{url}",
                headers={"Accept": "text/plain"},
                timeout=15
            )
            if resp.status_code == 200:
                return resp.text[:3000]
        except Exception:
            pass
        return None

    def get_youtube_transcript(self, url: str) -> Optional[str]:
        """获取 YouTube 视频字幕"""
        if not self._available.get("yt_dlp"):
            return None

        output = self._run_cmd([
            "yt-dlp", "--skip-download", "--write-auto-sub",
            "--sub-lang", "en,zh-Hans",
            "--print", "%(subtitles)s",
            url
        ], timeout=60)
        return output[:3000] if output else None

    def search_v2ex(self, query: str, limit: int = 5) -> List[Dict]:
        """搜索 V2EX 热帖"""
        import requests
        try:
            resp = requests.get(
                "https://www.v2ex.com/api/v2/topics/hot",
                headers={"User-Agent": "MeyoBot/1.0"},
                timeout=10
            )
            if resp.status_code != 200:
                return []

            topics = resp.json()
            relevant = [t for t in topics if query.lower() in (t.get("title", "") + t.get("content", "")).lower()]
            return [{
                "source": "v2ex",
                "title": t.get("title", ""),
                "content": t.get("content", "")[:300],
                "url": t.get("url", ""),
                "author": t.get("member", {}).get("username", ""),
                "engagement": t.get("replies", 0),
            } for t in relevant][:limit]
        except Exception:
            return []

    def multi_platform_search(self, query: str, platforms: List[str] = None) -> List[Dict]:
        """跨平台统一搜索，返回合并结果"""
        if platforms is None:
            platforms = ["twitter", "reddit", "xiaohongshu"]

        all_results = []
        for platform in platforms:
            searcher = getattr(self, f"search_{platform}", None)
            if searcher:
                try:
                    results = searcher(query)
                    all_results.extend(results)
                except Exception as e:
                    print(f"  ⚠️ {platform} 搜索失败: {e}")

        return sorted(all_results, key=lambda x: x.get("engagement", 0), reverse=True)

    @property
    def available_platforms(self) -> List[str]:
        platforms = ["reddit", "jina_reader"]
        if self._available.get("xreach"):
            platforms.append("twitter")
        if self._available.get("mcporter"):
            platforms.extend(["xiaohongshu"])
        if self._available.get("yt_dlp"):
            platforms.append("youtube")
        return platforms


if __name__ == "__main__":
    adapter = AgentReachAdapter()
    print(f"Available platforms: {adapter.available_platforms}")
    print(f"Tool status: {adapter._available}")

    results = adapter.search_reddit("AI agent workflow automation", limit=3)
    print(f"\nReddit results: {len(results)}")
    for r in results:
        print(f"  [{r['engagement']}] {r['title'][:60]}")
