"""Chat pre-flight compact —— adapter 边界的上下文预检 / 压缩 / rebuild 调度.

设计见 docs/compact-preflight-v1-spec.md (+ 2026-04-21 的 v1.1 review 收敛).

v1.1 核心变化:
  - 锁键从 agent_id 改成 (user_id, project_id) — rebuild 后 agent_id 变,
    用 id 做锁键无法跨 rebuild 互斥同一逻辑会话
  - _atomic_rebuild 用 CAS UPDATE 原子切 map. 跨 worker 竞态时 loser 复用
    winner 的 agent_id, 不会分叉对话到多个 agent
  - _create_agent_fresh 解耦: 新建 agent 和写 map 分开, 消除 map-less 窗口
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

import httpx

from config import CTX_SAFE_MARGIN, CTX_USER_MSG_OVERHEAD, LETTA_BASE_URL


# ================= 纯函数: 估算 + 危险判定 =================

@dataclass
class ContextInfo:
    """Letta agent 当前上下文占用."""
    current: int   # context_window_size_current (tokens now)
    window: int    # context_window_size_max (hard cap for this agent's LLM)


def _estimate_user_tokens(final_user_text: str) -> int:
    """估算最终发给 Letta 的 user message content 的 token 数.

    **调用约定**: 传入的是已经展开 # 引用 / 内联附件 / 注入系统标记之后的
    *最终 user_text*, 不是原始 request body 里的 messages 数组.

    估算方法: 中文为主 → char × 2. 比真实 tokenizer 通常高估 ~30%,
    v1 宁可高估触发 summarize 也不要漏过去让 LLM 400.
    """
    if not final_user_text:
        return 0
    return len(final_user_text) * 2


def _danger(ctx_current: int, ctx_window: int, est_user: int) -> bool:
    """本次请求是否可能撞 LLM window.
    公式: margin = window - current, 若 margin < SAFE + OVERHEAD + est_user → danger."""
    margin = ctx_window - ctx_current
    return margin < CTX_SAFE_MARGIN + CTX_USER_MSG_OVERHEAD + est_user


# ================= HTTP 到 Letta =================

async def _get_ctx_fresh(agent_id: str, timeout: float = 10.0) -> ContextInfo:
    """实时查 Letta agent 的 context 占用. 无 cache (v1 不做)."""
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
    """POST /v1/agents/{id}/summarize. 非 2xx 抛, 上层 fallback 到 rebuild."""
    url = f"{LETTA_BASE_URL}/v1/agents/{agent_id}/summarize"
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(url)
    r.raise_for_status()


# ================= Per-session lock =================

SessionKey = tuple[str, str]  # (user_id, project_id)

_session_locks: dict[SessionKey, asyncio.Lock] = {}
_session_locks_guard = asyncio.Lock()


async def _acquire_session_lock(user_id: str, project_id: str) -> asyncio.Lock:
    """拿 (user_id, project_id) 的 per-process lock.

    注意锁键是会话标识, 不是 agent_id — rebuild 后 agent_id 会变, 但会话不变,
    所以锁键必须绑定到"逻辑会话"而不是"当前 agent".

    跨 worker (gunicorn -w 4) 靠 _atomic_rebuild 的 CAS 做最终一致.
    """
    key: SessionKey = (user_id, project_id)
    async with _session_locks_guard:
        if key not in _session_locks:
            _session_locks[key] = asyncio.Lock()
        return _session_locks[key]


# ================= DB helpers (同步, 用 to_thread 包装) =================

def _read_agent_id_from_map_sync(user_id: str, project_id: str) -> str | None:
    from db import use_db
    with use_db() as db:
        row = db.execute(
            "SELECT agent_id FROM user_agent_map WHERE user_id=? AND project_id=?",
            (user_id, project_id),
        ).fetchone()
    return row[0] if row else None


def _cas_swap_agent_sync(user_id: str, project_id: str,
                         old_agent_id: str, new_agent_id: str) -> int:
    """单行原子 CAS: 仅当 map 仍指 old 时切到 new. 返回 rowcount.
    1 = 我赢了 (真的 swap 了);  0 = 别人先切了 (loser).
    """
    from db import use_db
    with use_db() as db:
        cursor = db.execute(
            "UPDATE user_agent_map SET agent_id=? "
            "WHERE user_id=? AND project_id=? AND agent_id=?",
            (new_agent_id, user_id, project_id, old_agent_id),
        )
        return cursor.rowcount


# ================= 主流程 =================

Action = Literal["noop", "sync_summarized", "rebuilt"]


@dataclass
class PreflightResult:
    action: Action
    agent_id: str            # 最终该用的 agent_id. rebuilt 后是新的; CAS 失败时是 winner 的
    rebuilt: bool
    ctx_before: int | None
    ctx_after: int | None
    user_msg: str | None     # rebuilt 时的用户提示, 否则 None


_REBUILD_USER_MSG = "⚠️ 对话历史已压缩重置 (超出上下文限制)"


async def preflight_compact(
    user_id: str,
    project_id: str,
    final_user_text: str,
) -> PreflightResult:
    """预检 → summarize → rebuild 的主入口.

    签名: 不接收 agent_id, 自己从 map 读. 因为 rebuild 过程中 agent_id 会变,
    由 preflight 内部统一管理"当前逻辑会话对应哪个 agent"这件事.

    调用约定: final_user_text 是已展开 # 引用/附件的最终 content.
    """
    est = _estimate_user_tokens(final_user_text)

    # 步骤 1: 无锁 fast path
    agent_id = await asyncio.to_thread(_read_agent_id_from_map_sync, user_id, project_id)
    if not agent_id:
        # map 里没有 — 正常流程 main.py 早已调过 get_or_create_agent 不该出现
        raise RuntimeError(f"preflight: no agent_id in map for {user_id}/{project_id}")

    ctx = await _get_ctx_fresh(agent_id)
    if not _danger(ctx.current, ctx.window, est):
        return PreflightResult(
            action="noop", agent_id=agent_id, rebuilt=False,
            ctx_before=ctx.current, ctx_after=ctx.current, user_msg=None,
        )

    # 步骤 2: 拿 session 锁 (按 user_id+project_id, 不按 agent_id)
    lock = await _acquire_session_lock(user_id, project_id)
    async with lock:
        # 锁内重读 map — 可能同 worker 的另一个请求已经 rebuild 了
        agent_id = await asyncio.to_thread(_read_agent_id_from_map_sync, user_id, project_id)
        if not agent_id:
            raise RuntimeError(f"preflight: map row gone after lock for {user_id}/{project_id}")

        # 锁内重查 ctx
        ctx = await _get_ctx_fresh(agent_id)
        if not _danger(ctx.current, ctx.window, est):
            logging.info(
                f"[preflight] {agent_id[-12:]} safe after lock re-read: current={ctx.current}"
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

        # 步骤 4: rebuild (CAS 原子切 map, 跨 worker 收敛)
        final_agent_id = await _atomic_rebuild(agent_id, user_id, project_id)
        return PreflightResult(
            action="rebuilt", agent_id=final_agent_id, rebuilt=True,
            ctx_before=ctx_before, ctx_after=0, user_msg=_REBUILD_USER_MSG,
        )


async def _atomic_rebuild(old_agent_id: str, user_id: str, project_id: str) -> str:
    """原子 rebuild, 消除 map-less 窗口, 跨 worker 收敛到 winner.

    流程:
      1. _create_agent_fresh 造 candidate (不碰 map, 纯新建 agent)
      2. CAS UPDATE: WHERE agent_id = old_agent_id → 仅当 map 还指 old 时生效
         - rowcount=1: 我赢了, candidate 就是 winner
         - rowcount=0: 别人先切了, 我输了; 读 map 拿 winner
      3. 旧 agent (和 CAS loser 产生的 candidate orphan) 不同步删,
         留给 scripts/reconcile_orphan_agents.py 兜底清

    保证: CAS 失败时当前请求复用 winner 的 agent_id, 不分叉对话.
    """
    # Lazy import 避免 circular
    from routing import _create_agent_fresh

    # (1) 造 candidate
    candidate_id = await asyncio.to_thread(_create_agent_fresh, user_id, project_id)

    # (2) CAS 原子切
    rowcount = await asyncio.to_thread(
        _cas_swap_agent_sync, user_id, project_id, old_agent_id, candidate_id,
    )
    if rowcount == 1:
        logging.info(
            f"[preflight] CAS won: {old_agent_id[-12:]} → {candidate_id[-12:]} "
            f"(old+ any loser candidates left for reconcile)"
        )
        return candidate_id

    # (3) CAS 失败: 读 winner, candidate 成孤儿
    winner_id = await asyncio.to_thread(_read_agent_id_from_map_sync, user_id, project_id)
    if winner_id is None:
        # 罕见: map 行被删掉 (比如另一处真的删了 map). 用 candidate 插回去兜底.
        from db import use_db
        def _insert():
            with use_db() as db:
                db.execute(
                    "INSERT OR IGNORE INTO user_agent_map (user_id, project_id, agent_id) VALUES (?, ?, ?)",
                    (user_id, project_id, candidate_id),
                )
                row = db.execute(
                    "SELECT agent_id FROM user_agent_map WHERE user_id=? AND project_id=?",
                    (user_id, project_id),
                ).fetchone()
                return row[0] if row else None
        winner_id = await asyncio.to_thread(_insert)
        logging.warning(
            f"[preflight] CAS lost + map gone: fell back to INSERT candidate {candidate_id[-12:]}"
        )
        return winner_id or candidate_id

    logging.info(
        f"[preflight] CAS lost: candidate {candidate_id[-12:]} abandoned as orphan, "
        f"using winner {winner_id[-12:]}"
    )
    return winner_id


async def resolve_current_agent(user_id: str, project_id: str, preferred_agent_id: str) -> str:
    """Forward 前再 SELECT 一次 map, 把 fast-path 与 concurrent rebuild 之间的
    分叉窗口从 ~100ms 压到 ~10ms.

    场景:
      1. 请求 B 走 preflight fast path, 返 safe + agent_id = old
      2. 请求 A 在别的 worker 同时 rebuild, map 切到 new
      3. B 回到 main.py, 在 forward Letta 之前调这个函数 re-verify
      4. 发现 map 已变, 采用 current (new), 不会再把消息发给 old

    注意不能 100% 消灭 race: 在本函数返回后 → Letta HTTP 发出之间 (~10ms),
    仍可能有 rebuild 发生. 旧 agent 延迟删除 (1h+ 宽限) 兜住这个残余窗口.
    严格串行需要跨 worker advisory lock / generation fence, 挂 v2 todo.

    如果 map 行意外为空 (reconcile 扫到半成品状态, 极罕见), 返 preferred 兜底.
    """
    current = await asyncio.to_thread(_read_agent_id_from_map_sync, user_id, project_id)
    if current is None:
        logging.warning(
            f"[preflight] resolve_current_agent: map row gone for {user_id}/{project_id}, "
            f"fallback to preferred {preferred_agent_id[-12:]}"
        )
        return preferred_agent_id
    if current != preferred_agent_id:
        logging.info(
            f"[preflight] map shifted {preferred_agent_id[-12:]} → {current[-12:]} "
            f"between preflight and forward (adopting current)"
        )
    return current
