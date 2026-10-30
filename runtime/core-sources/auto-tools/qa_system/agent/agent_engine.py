#!/usr/bin/env python3
"""agent_engine · Agent 引擎接口层。

自研 NativeEngine(thin loop·model-driven·单墙钟·每步ckpt·前缀缓存)是唯一后端。
(2026-08-27 瘦身) deepagents 后端已删:生产 97% 走 native·deepagents 只当从未触发的
fallback·还拖 9 个 langchain 包+黑盒(曾致 tool编译崩/recursion撞满)。军团=主线·
auto 走 thin·不再保留旧引擎兜底。

  create_agent_engine(model, tools, system_prompt) → AgentEngine
  engine.invoke({"messages":[...]}, config={...})
"""
import os


class AgentEngine:
    """引擎统一接口·对底层 NativeEngine 的薄包装。"""

    def __init__(self, backend_agent, backend_name="native"):
        self._agent = backend_agent
        self.backend = backend_name

    def invoke(self, payload, config=None):
        if config is not None:
            return self._agent.invoke(payload, config=config)
        return self._agent.invoke(payload)

    def __getattr__(self, name):
        # 透传底层 agent 的其它属性/方法(stream 等)
        return getattr(self._agent, name)


def create_agent_engine(model, tools, system_prompt, *, backend=None, session_qid=""):
    """创建引擎(唯一后端=native NativeEngine)。backend 参数保留兼容·恒为 native。"""
    import native_agent_loop as _nal
    return AgentEngine(
        _nal.create(model, tools, system_prompt, session_qid=session_qid), "native")


if __name__ == "__main__":
    print("agent_engine 接口层 OK · 后端: native(唯一)")
