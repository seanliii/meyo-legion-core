"""
统一LLM客户端 — 使用本地Anthropic proxy（替代AISA）
环境变量: ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN

v2.1: 加指数退避重试 + token动态刷新 + provider自动re-detect
"""
import os
import time
import requests
import json
import glob


class LLMClient:
    """统一LLM调用，优先用本地Anthropic proxy，fallback到AISA"""

    # 可重试的HTTP状态码
    _RETRYABLE = {429, 500, 502, 503, 504}
    # 需要重新检测provider的状态码
    _REDETECT = {401, 403}

    def __init__(self, aisa_key: str = ""):
        self.aisa_key = aisa_key
        self.anthropic_base = os.environ.get("ANTHROPIC_BASE_URL", "http://127.0.0.1:3456")
        self.model = "claude-sonnet-4-20250514"
        self._provider = None
        self._consecutive_failures = 0

    def _get_token(self) -> str:
        """每次调用动态读取token，不缓存。
        优先级: ccr-settings文件 > 环境变量 > 空
        """
        # 1. 先试 ccr-settings（Claude Code Router写的临时文件）
        for f in glob.glob("/var/folders/*/*/T/claude-code-router/ccr-settings-*.json"):
            try:
                env_block = json.load(open(f)).get("env", {})
                token = env_block.get("ANTHROPIC_AUTH_TOKEN", "")
                if token:
                    return token
            except Exception:
                pass
        # 2. 环境变量
        return os.environ.get("ANTHROPIC_AUTH_TOKEN", "")

    def _detect_provider(self) -> str:
        """检测可用的LLM provider"""
        if self._provider:
            return self._provider
        # 优先试Anthropic
        token = self._get_token()
        if token:
            try:
                resp = requests.post(
                    f"{self.anthropic_base}/v1/messages",
                    headers={
                        "Content-Type": "application/json",
                        "x-api-key": token,
                        "anthropic-version": "2023-06-01"
                    },
                    json={
                        "model": self.model,
                        "max_tokens": 10,
                        "messages": [{"role": "user", "content": "ping"}]
                    },
                    timeout=10
                )
                if resp.status_code == 200:
                    self._provider = "anthropic"
                    print("  🔌 LLM: Anthropic proxy (本地)")
                    return "anthropic"
                else:
                    print(f"  ⚠️ Anthropic探测失败: HTTP {resp.status_code}")
            except Exception as e:
                print(f"  ⚠️ Anthropic探测异常: {e}")
        # fallback AISA
        if self.aisa_key:
            self._provider = "aisa"
            print("  🔌 LLM: AISA (远程)")
            return "aisa"
        raise RuntimeError("没有可用的LLM provider")

    def generate(self, prompt: str, max_tokens: int = 4096) -> str:
        """生成文本，自动选择provider"""
        provider = self._detect_provider()

        if provider == "anthropic":
            result = self._call_anthropic(prompt, max_tokens)
        else:
            result = self._call_aisa(prompt, max_tokens)

        if result:
            self._consecutive_failures = 0
        else:
            self._consecutive_failures += 1
            # 连续失败3次，清除provider缓存，下次重新检测
            if self._consecutive_failures >= 3:
                print(f"  ⚠️ 连续{self._consecutive_failures}次失败，重新检测provider")
                self._provider = None
                self._consecutive_failures = 0

        return result

    def _call_anthropic(self, prompt: str, max_tokens: int) -> str:
        max_retries = 3
        for attempt in range(max_retries):
            token = self._get_token()  # 每次重试都重新读token
            if not token:
                print("  [Anthropic错误] token为空，跳过")
                return ""
            try:
                resp = requests.post(
                    f"{self.anthropic_base}/v1/messages",
                    headers={
                        "Content-Type": "application/json",
                        "x-api-key": token,
                        "anthropic-version": "2023-06-01"
                    },
                    json={
                        "model": self.model,
                        "max_tokens": max_tokens,
                        "messages": [{"role": "user", "content": prompt}]
                    },
                    timeout=(10, 120)  # (connect_timeout, read_timeout)
                )
                if resp.status_code == 200:
                    data = resp.json()
                    content_blocks = data.get("content", [])
                    return "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")

                # 401/403: token问题，清provider缓存让下次re-detect
                if resp.status_code in self._REDETECT:
                    print(f"  [Anthropic错误] HTTP {resp.status_code} (认证失败)，重置provider")
                    self._provider = None
                    return ""

                # 可重试的错误
                if resp.status_code in self._RETRYABLE and attempt < max_retries - 1:
                    wait = 2 ** (attempt + 1)  # 2, 4, 8秒
                    print(f"  [Anthropic重试] HTTP {resp.status_code}，{wait}s后重试({attempt+1}/{max_retries})")
                    time.sleep(wait)
                    continue

                print(f"  [Anthropic错误] HTTP {resp.status_code}: {resp.text[:200]}")
                return ""

            except requests.exceptions.Timeout:
                if attempt < max_retries - 1:
                    wait = 2 ** (attempt + 1)
                    print(f"  [Anthropic超时] {wait}s后重试({attempt+1}/{max_retries})")
                    time.sleep(wait)
                    continue
                print("  [Anthropic错误] 超时，已重试3次")
                return ""

            except requests.exceptions.ConnectionError:
                if attempt < max_retries - 1:
                    wait = 2 ** (attempt + 1)
                    print(f"  [Anthropic连接失败] {wait}s后重试({attempt+1}/{max_retries})")
                    time.sleep(wait)
                    continue
                print("  [Anthropic错误] 连接失败，proxy可能未运行")
                self._provider = None  # proxy挂了，下次re-detect
                return ""

            except Exception as e:
                print(f"  [Anthropic异常] {e}")
                return ""

        return ""

    def _call_aisa(self, prompt: str, max_tokens: int) -> str:
        try:
            resp = requests.post(
                "https://api.aisa.one/apis/v1/perplexity/sonar",
                headers={
                    "Authorization": f"Bearer {self.aisa_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "sonar",
                    "messages": [{"role": "user", "content": prompt}]
                },
                timeout=90
            )
            data = resp.json()
            return data.get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            print(f"  [AISA异常] {e}")
            return ""
