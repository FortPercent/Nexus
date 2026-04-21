"""Chat pre-flight compact —— adapter 边界的上下文预检 / 压缩 / rebuild 调度.

设计见 docs/compact-preflight-v1-spec.md. 本模块只做 v1 的 Step 1:
  - _estimate_user_tokens   (纯函数)
  - _danger                 (纯函数)
  - _get_ctx_fresh          (HTTP 到 Letta, 无 cache)
主流程 preflight_compact / _atomic_rebuild / 锁 都在 Step 2 加.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from config import CTX_SAFE_MARGIN, CTX_USER_MSG_OVERHEAD, LETTA_BASE_URL


@dataclass
class ContextInfo:
    """Letta agent 当前上下文占用."""
    current: int   # context_window_size_current (tokens now)
    window: int    # context_window_size_max (hard cap for this agent's LLM)


def _estimate_user_tokens(final_user_text: str) -> int:
    """估算最终发给 Letta 的 user message content 的 token 数.

    **调用约定**: 传入的是已经展开 # 引用 / 内联附件 / 注入系统标记之后的
    *最终 user_text*, 不是原始 request body 里的 messages 数组. 若传错
    (比如把整个 body.messages 丢进来), 会过度 summarize / rebuild.

    估算方法: 中文字符占主 → char × 2. 比真实 tokenizer 通常高估 ~30%,
    v1 宁可高估触发 summarize 也不要漏过去让 LLM 400.
    """
    if not final_user_text:
        return 0
    return len(final_user_text) * 2


def _danger(ctx_current: int, ctx_window: int, est_user: int) -> bool:
    """本次请求是否可能撞 LLM window.

    公式: margin = window - current, 若 margin < SAFE + OVERHEAD + est_user → danger.
    """
    margin = ctx_window - ctx_current
    return margin < CTX_SAFE_MARGIN + CTX_USER_MSG_OVERHEAD + est_user


async def _get_ctx_fresh(agent_id: str, timeout: float = 10.0) -> ContextInfo:
    """实时查 Letta agent 的 context 占用. 无 cache (v1 故意不做, 避免 stale
    状态放过 overfull 请求; cache 是 v2 优化).

    用 httpx 直连 /v1/agents/{id}/context, 因为 letta_client SDK 目前
    没有 agents.context 命名空间 (AgentsResource object has no attribute 'context'
    实测报错).
    """
    url = f"{LETTA_BASE_URL}/v1/agents/{agent_id}/context"
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.get(url)
    r.raise_for_status()
    d = r.json()
    return ContextInfo(
        current=int(d.get("context_window_size_current") or 0),
        window=int(d.get("context_window_size_max") or 60000),
    )
