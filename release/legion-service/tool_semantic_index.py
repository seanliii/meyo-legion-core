#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工具语义召回：字符 n-gram 向量 + BM25 混排。

不维护领域词表，也不翻译用户意图。字符 n-gram 给中英描述各自稳定的模糊召回；若环境提供
sentence-transformers，则自动使用多语言 embedding，并按 catalog hash 缓存向量。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

import legion_paths as _paths

CACHE = Path(os.environ.get("LEGION_TOOL_EMBED_CACHE") or _paths.under("cache", "tool_embeddings.json"))
_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
_MODEL_INSTANCE = None
_MODEL_SNAPSHOT = ""


def _text(item: dict) -> str:
    return " ".join([str(item.get("name") or ""), str(item.get("description") or ""),
                     " ".join(item.get("capability_tags") or []), str(item.get("source_harness") or "")])


def _char_vector(text: str) -> dict[str, float]:
    s = " ".join(str(text or "").lower().split())
    grams = {}
    for n in (2, 3):
        for i in range(max(0, len(s) - n + 1)):
            g = s[i:i+n]
            grams[g] = grams.get(g, 0.0) + 1.0
    norm = math.sqrt(sum(v * v for v in grams.values())) or 1.0
    return {k: v / norm for k, v in grams.items()}


def _cos_sparse(a: dict, b: dict) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


def _cached_embedding_scores(items: list[dict], query: str) -> list[float] | None:
    """Use cached catalog vectors with a lightweight local query embedding process.

    Loading PyTorch in every short-lived legion_tool CLI takes tens of seconds. Keep the valuable
    multilingual index, but only use it in-process when explicitly requested; the normal CLI uses
    model query rewriting + BM25 and never blocks on model startup or download.
    """
    if os.environ.get("LEGION_TOOL_LOCAL_EMBEDDING", "0") != "1":
        return None
    return _embedding_scores(items, query)


def _embedding_scores(items: list[dict], query: str) -> list[float] | None:
    # 下载/修复 embedding 模型属于显式安装动作，绝不能在每次工具检索时偷偷联网重试。
    # 只有本地快照完整且缓存索引已存在时才启用；否则立即回退字符 n-gram。
    model_cache = Path.home() / ".cache" / "huggingface" / "hub" / \
        "models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2"
    snapshots = model_cache / "snapshots"
    required = ("modules.json", "config.json", "tokenizer.json", "model.safetensors")
    complete_snapshot = next((p for p in snapshots.glob("*")
                              if p.is_dir() and all((p / name).exists() for name in required)), None) \
        if snapshots.is_dir() else None
    if not CACHE.is_file() or complete_snapshot is None:
        return None
    try:
        from sentence_transformers import SentenceTransformer
    except Exception:
        return None
    texts = [_text(x) for x in items]
    digest = hashlib.sha256("\n".join(texts).encode()).hexdigest()
    vectors = None
    try:
        cached = json.loads(CACHE.read_text(encoding="utf-8"))
        if cached.get("digest") == digest and cached.get("model") == _MODEL:
            vectors = cached.get("vectors")
    except (OSError, json.JSONDecodeError):
        pass
    # 工具发现必须是本地快路径，不能因为模型未缓存就现场访问 Hugging Face 并重试数分钟。
    # Transformers 的嵌套 tokenizer/config loader 也遵循 offline env；传本地 snapshot 路径并
    # 临时设离线标志，保证缺可选文件时立即失败并退字符 n-gram。
    global _MODEL_INSTANCE, _MODEL_SNAPSHOT
    snapshot = str(complete_snapshot)
    if _MODEL_INSTANCE is None or _MODEL_SNAPSHOT != snapshot:
        old_offline = (os.environ.get("HF_HUB_OFFLINE"), os.environ.get("TRANSFORMERS_OFFLINE"))
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        try:
            _MODEL_INSTANCE = SentenceTransformer(snapshot, local_files_only=True)
            _MODEL_SNAPSHOT = snapshot
        except Exception:
            _MODEL_INSTANCE = None
            _MODEL_SNAPSHOT = ""
            return None
        finally:
            for name, value in zip(("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"), old_offline):
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
    model = _MODEL_INSTANCE
    if vectors is None:
        vectors = model.encode(texts, normalize_embeddings=True).tolist()
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps({"digest": digest, "model": _MODEL, "vectors": vectors}), encoding="utf-8")
    qv = model.encode([query], normalize_embeddings=True)[0]
    return [float(sum(float(a) * float(b) for a, b in zip(qv, v))) for v in vectors]


def scores(items: list[dict], query: str) -> tuple[list[float], str]:
    embedded = _cached_embedding_scores(items, query)
    if embedded is not None:
        return embedded, "multilingual_embedding"
    q = _char_vector(query)
    return [_cos_sparse(q, _char_vector(_text(item))) for item in items], "character_ngram_fallback"
