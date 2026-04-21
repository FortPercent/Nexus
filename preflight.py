"""Chat pre-flight compact —— adapter 边界的上下文预检 / 压缩 / rebuild 调度.

设计见 docs/compact-preflight-v1-spec.md.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

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


async def _call_summarize(agent_id: str, timeout: float = 30.0) -> None:
    """POST /v1/agents/{id}/summarize. 上游 endpoint 在 letta rest_api
    routers/v1/agents.py:2430. 非 2xx 抛异常, 由上层 catch 后 fallback 到 rebuild."""
    url = f"{LETTA_BASE_URL}/v1/agents/{agent_id}/summarize"
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(url)
    r.raise_for_status()


# ================= Per-agent lock =================

_agent_locks: dict[str, asyncio.Lock] = {}
_agent_locks_guard = asyncio.Lock()


async def _acquire_agent_lock(agent_id: str) -> asyncio.Lock:
    """拿到该 agent 的 per-process lock. 同 worker 内保证 danger 区串行;
    跨 worker (gunicorn -w 4) 依赖延迟删除 + orphan reconcile 做最终一致."""
    async with _agent_locks_guard:
        if agent_id not in _agent_locks:
            _agent_locks[agent_id] = asyncio.Lock()
        return _agent_locks[agent_id]


# ================= Main flow =================

Action = Literal["noop", "sync_summarized", "rebuilt"]


@dataclass
class PreflightResult:
    action: Action
    agent_id: str            # rebuild 后是新的, 否则和入参一致
    rebuilt: bool
    ctx_before: int | None
    ctx_after: int | None
    user_msg: str | None     # rebuilt 时的提示, 否则 None


_REBUILD_USER_MSG = "⚠️ 对话历史已压缩重置 (超出上下文限制)"


async def preflight_compact(
    agent_id: str,
    user_id: str,
    project_id: str,
    final_user_text: str,
) -> PreflightResult:
    """主入口. 见 docs/compact-preflight-v1-spec.md §4.

    调用约定: final_user_text 必须是已经展开 # 引用 / 内联附件 / 系统注入之后
    的最终 user content. 传原始 body.messages 会严重高估 → 过度 summarize/rebuild.
    """
    est = _estimate_user_tokens(final_user_text)

    # 步骤 1: 无锁快速路径
    ctx = await _get_ctx_fresh(agent_id)
    if not _danger(ctx.current, ctx.window, est):
        return PreflightResult(
            action="noop", agent_id=agent_id, rebuilt=False,
            ctx_before=ctx.current, ctx_after=ctx.current, user_msg=None,
        )

    # 步骤 2: 加锁重检 (另一个请求可能已经压完)
    lock = await _acquire_agent_lock(agent_id)
    async with lock:
        ctx = await _get_ctx_fresh(agent_id)
        if not _danger(ctx.current, ctx.window, est):
            logging.info(
                f"[preflight] {agent_id[-12:]} saw safe after lock (another req compacted?): current={ctx.current}"
            )
            return PreflightResult(
                action="noop", agent_id=agent_id, rebuilt=False,
                ctx_before=ctx.current, ctx_after=ctx.current, user_msg=None,
            )

        ctx_before = ctx.current

        # 步骤 3: 同步 summarize
        try:
            await asyncio.wait_for(_call_summarize(agent_id), timeout=30)
            ctx2 = await _get_ctx_fresh(agent_id)
            if not _danger(ctx2.current, ctx.window, est):
                logging.info(
                    f"[preflight] {agent_id[-12:]} sync_summarized {ctx_before} → {ctx2.current}"
                )
                return PreflightResult(
                    action="sync_summarized", agent_id=agent_id, rebuilt=False,
                    ctx_before=ctx_before, ctx_after=ctx2.current, user_msg=None,
                )
            logging.warning(
                f"[preflight] {agent_id[-12:]} summarize insufficient ({ctx2.current} still danger), rebuild"
            )
        except asyncio.TimeoutError:
            logging.warning(f"[preflight] {agent_id[-12:]} summarize timeout 30s, rebuild")
        except Exception as e:
            logging.warning(f"[preflight] {agent_id[-12:]} summarize error: {e}, rebuild")

        # 步骤 4: rebuild (原子流程见 §6, 不同步删旧 agent)
        new_agent_id = await _atomic_rebuild(agent_id, user_id, project_id)
        logging.info(
            f"[preflight] {agent_id[-12:]} rebuilt → {new_agent_id[-12:]} (old left for reconcile)"
        )
        return PreflightResult(
            action="rebuilt", agent_id=new_agent_id, rebuilt=True,
            ctx_before=ctx_before, ctx_after=0, user_msg=_REBUILD_USER_MSG,
        )


async def _atomic_rebuild(old_agent_id: str, user_id: str, project_id: str) -> str:
    """创建新 agent + 切 map, 不删旧 agent. 见 docs §6.

    旧 agent 从 map 切换那一刻变成孤儿, 1h+ 宽限期后由
    scripts/reconcile_orphan_agents.py 兜底清. 这给跨 worker 的
    in-flight 请求 (可能还持有 old_agent_id) 留足时间完成本轮.
    """
    # Lazy import 避免 circular (routing 依赖多)
    from routing import get_or_create_agent
    from db import use_db

    # (a) 删 map 行 - get_or_create_agent 内部读 map, 必须先清掉
    with use_db() as db:
        db.execute(
            "DELETE FROM user_agent_map WHERE user_id=? AND project_id=?",
            (user_id, project_id),
        )

    # (b) 调本地 helper 重建 (同步, to_thread 不阻塞 event loop)
    #     内部会: 创建 Letta agent + attach blocks/tools/kb + 写新 map 行
    new_agent_id = await asyncio.to_thread(get_or_create_agent, user_id, project_id)

    # (c) 校验 map 指向新 agent
    with use_db() as db:
        row = db.execute(
            "SELECT agent_id FROM user_agent_map WHERE user_id=? AND project_id=?",
            (user_id, project_id),
        ).fetchone()
    assert row and row[0] == new_agent_id, \
        f"map row mismatch after rebuild: got {row}, expected {new_agent_id}"

    # (d) 旧 agent 不删, 等 reconcile_orphan_agents 扫
    return new_agent_id
