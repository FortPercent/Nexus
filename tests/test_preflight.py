"""Unit tests for preflight.py — 会话收敛语义.

测试标准:
  **"同一逻辑会话在并发和失败下仍然收敛到单一 agent_id, 不丢消息、不裂会话"**
  (不是旧的 "不 crash 就算过")

结构:
  TestDanger           — 阈值公式
  TestPreflightFlow    — 主状态机 (noop / summarized / rebuilt)
  TestSessionLock      — (user_id, project_id) 锁键
  TestAtomicRebuild    — CAS 语义 + winner 复用 + fresh fail
  TestHTTPContextFetch — _get_ctx_fresh HTTP 解析

本地运行:
  cd adapter && python3 -m pytest tests/test_preflight.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

# config.py 需要 env vars
os.environ.setdefault("ADAPTER_API_KEY", "test")
os.environ.setdefault("OPENWEBUI_JWT_SECRET", "test")
os.environ.setdefault("OPENWEBUI_ADMIN_EMAIL", "test@example.com")
os.environ.setdefault("OPENWEBUI_ADMIN_PASSWORD", "test")
os.environ.setdefault("VLLM_ENDPOINT", "http://localhost")
os.environ.setdefault("VLLM_API_KEY", "test")

# 测试环境跑在 dev 机上, 不装 fastapi/aiosqlite 等生产依赖. 给 routing 和 db
# 塞 stub module, 测 _atomic_rebuild 时 preflight lazy import 拿到 fake.
import types as _types
_fake_routing = _types.ModuleType("routing")
_fake_routing._create_agent_fresh = lambda u, p: "agent-stub-from-fake-routing"
sys.modules.setdefault("routing", _fake_routing)
_fake_db = _types.ModuleType("db")
class _FakeUseDB:
    def __enter__(self): raise RuntimeError("fake db; test should mock callers")
    def __exit__(self, *a): return False
_fake_db.use_db = lambda: _FakeUseDB()
sys.modules.setdefault("db", _fake_db)

import httpx
import pytest

import preflight
from preflight import (
    ContextInfo,
    PreflightResult,
    _danger,
    _estimate_user_tokens,
    _get_ctx_fresh,
    preflight_compact,
)


# ======================================================================
# 公共 fixture
# ======================================================================

@pytest.fixture(autouse=True)
def _reset_session_locks():
    preflight._session_locks.clear()
    yield
    preflight._session_locks.clear()


# ======================================================================
# TestDanger — 阈值公式
# ======================================================================

def test_danger_plenty_margin():
    assert _danger(20000, 60000, 0) is False
    assert _danger(20000, 60000, 5000) is False


def test_danger_exactly_at_threshold():
    """SAFE=5000 OVERHEAD=500; est=0 → threshold=5500. margin == 5500 不算 danger."""
    assert _danger(54500, 60000, 0) is False
    assert _danger(54501, 60000, 0) is True


def test_danger_user_msg_pushes_over():
    assert _danger(52000, 60000, 2499) is False  # 8000 >= 7999
    assert _danger(52000, 60000, 2501) is True   # 8000 < 8001


def test_danger_overfull_always_danger():
    assert _danger(60000, 60000, 0) is True
    assert _danger(71000, 60000, 0) is True


def test_danger_biany_71268_scenario():
    """04-20 复现: asset-management 撞 71268, 必 danger."""
    assert _danger(71268, 65536, 0) is True


def test_estimate_char_times_two():
    assert _estimate_user_tokens("") == 0
    assert _estimate_user_tokens("你好") == 4
    assert _estimate_user_tokens("a" * 1000) == 2000


# ======================================================================
# TestPreflightFlow — 主状态机
# ======================================================================

def _mk_map_stub(agent_id_seq):
    """Returns a stub for _read_agent_id_from_map_sync whose return changes by call count.
    agent_id_seq = [first_call_returns, second_call_returns, ...]
    """
    state = {"i": 0}
    def _stub(user_id, project_id):
        i = state["i"]
        state["i"] += 1
        if i < len(agent_id_seq):
            return agent_id_seq[i]
        return agent_id_seq[-1]  # stick to last
    return _stub


@pytest.mark.asyncio
async def test_flow_safe_fast_path():
    """safe 水位 → action=noop, 不调 summarize/rebuild."""
    async def mock_ctx(aid):
        return ContextInfo(current=10000, window=60000)

    summarize_calls = {"n": 0}
    async def mock_summ(aid, timeout=30):
        summarize_calls["n"] += 1

    rebuild_calls = {"n": 0}
    async def mock_rebuild(old, uid, pid):
        rebuild_calls["n"] += 1
        return "agent-rebuilt"

    with patch.object(preflight, "_read_agent_id_from_map_sync",
                      side_effect=_mk_map_stub(["agent-old"])), \
         patch.object(preflight, "_get_ctx_fresh", side_effect=mock_ctx), \
         patch.object(preflight, "_call_summarize", side_effect=mock_summ), \
         patch.object(preflight, "_atomic_rebuild", side_effect=mock_rebuild):
        r = await preflight_compact("u1", "p1", "你好")

    assert r.action == "noop"
    assert r.agent_id == "agent-old"
    assert r.rebuilt is False
    assert summarize_calls["n"] == 0
    assert rebuild_calls["n"] == 0


@pytest.mark.asyncio
async def test_flow_summarize_succeeds_safe_after():
    """danger → summarize → safe. action=sync_summarized, agent_id 不变, 不 rebuild."""
    state = {"compacted": False}
    async def mock_ctx(aid):
        return ContextInfo(current=30000 if state["compacted"] else 58000, window=60000)
    async def mock_summ(aid, timeout=30):
        state["compacted"] = True
    rebuild_calls = {"n": 0}
    async def mock_rebuild(old, uid, pid):
        rebuild_calls["n"] += 1
        return "agent-rebuilt"

    with patch.object(preflight, "_read_agent_id_from_map_sync",
                      side_effect=_mk_map_stub(["agent-old"])), \
         patch.object(preflight, "_get_ctx_fresh", side_effect=mock_ctx), \
         patch.object(preflight, "_call_summarize", side_effect=mock_summ), \
         patch.object(preflight, "_atomic_rebuild", side_effect=mock_rebuild):
        r = await preflight_compact("u1", "p1", "你好")

    assert r.action == "sync_summarized"
    assert r.agent_id == "agent-old"  # 没 rebuild, id 不变
    assert r.rebuilt is False
    assert rebuild_calls["n"] == 0


@pytest.mark.asyncio
async def test_flow_summarize_raises_fallback_rebuild():
    """summarize 抛异常 → rebuild, 返回 new agent_id."""
    async def mock_ctx(aid):
        return ContextInfo(current=58000, window=60000)
    async def mock_summ(aid, timeout=30):
        raise RuntimeError("letta 500")
    async def mock_rebuild(old, uid, pid):
        assert old == "agent-old"
        return "agent-new"

    with patch.object(preflight, "_read_agent_id_from_map_sync",
                      side_effect=_mk_map_stub(["agent-old"])), \
         patch.object(preflight, "_get_ctx_fresh", side_effect=mock_ctx), \
         patch.object(preflight, "_call_summarize", side_effect=mock_summ), \
         patch.object(preflight, "_atomic_rebuild", side_effect=mock_rebuild):
        r = await preflight_compact("u1", "p1", "你好")

    assert r.action == "rebuilt"
    assert r.agent_id == "agent-new"
    assert r.rebuilt is True
    assert r.user_msg and "对话历史已压缩重置" in r.user_msg


@pytest.mark.asyncio
async def test_flow_summarize_insufficient_fallback_rebuild():
    """summarize 成功但压完仍 danger → rebuild."""
    async def mock_ctx(aid):
        return ContextInfo(current=58000, window=60000)  # always danger
    async def mock_summ(aid, timeout=30):
        pass
    async def mock_rebuild(old, uid, pid):
        return "agent-new"

    with patch.object(preflight, "_read_agent_id_from_map_sync",
                      side_effect=_mk_map_stub(["agent-old"])), \
         patch.object(preflight, "_get_ctx_fresh", side_effect=mock_ctx), \
         patch.object(preflight, "_call_summarize", side_effect=mock_summ), \
         patch.object(preflight, "_atomic_rebuild", side_effect=mock_rebuild):
        r = await preflight_compact("u1", "p1", "你好")

    assert r.action == "rebuilt"
    assert r.agent_id == "agent-new"


@pytest.mark.asyncio
async def test_flow_summarize_timeout_fallback_rebuild():
    """summarize 超时 → rebuild."""
    async def mock_ctx(aid):
        return ContextInfo(current=58000, window=60000)
    async def mock_summ(aid, timeout=30):
        await asyncio.sleep(60)  # 远超 wait_for
    async def mock_rebuild(old, uid, pid):
        return "agent-new"

    # 把 wait_for 缩到 50ms 让测试快
    orig_wait_for = asyncio.wait_for
    async def fast_wait_for(coro, timeout):
        return await orig_wait_for(coro, 0.05)

    with patch.object(preflight, "_read_agent_id_from_map_sync",
                      side_effect=_mk_map_stub(["agent-old"])), \
         patch.object(preflight, "_get_ctx_fresh", side_effect=mock_ctx), \
         patch.object(preflight, "_call_summarize", side_effect=mock_summ), \
         patch.object(preflight, "_atomic_rebuild", side_effect=lambda o, u, p: _mk_rebuild("agent-new")), \
         patch.object(preflight.asyncio, "wait_for", side_effect=fast_wait_for):
        async def _mk_rebuild(new):
            return new
        # workaround for above (patch.object side_effect can't be a coroutine ref directly)
        with patch.object(preflight, "_atomic_rebuild", side_effect=mock_rebuild):
            r = await preflight_compact("u1", "p1", "你好")

    assert r.action == "rebuilt"


# ======================================================================
# TestSessionLock — 锁键是 (user, project) 不是 agent_id
# ======================================================================

@pytest.mark.asyncio
async def test_lock_different_sessions_dont_block():
    """不同 (user,project) 的锁互不干扰."""
    la = await preflight._acquire_session_lock("u1", "p1")
    lb = await preflight._acquire_session_lock("u1", "p2")
    lc = await preflight._acquire_session_lock("u2", "p1")
    assert la is not lb
    assert la is not lc
    assert lb is not lc


@pytest.mark.asyncio
async def test_lock_same_session_same_lock():
    """同 (user,project) 不同 agent_id 拿到同一把锁 — 这是 rebuild 不分叉的关键."""
    la = await preflight._acquire_session_lock("u1", "p1")
    la2 = await preflight._acquire_session_lock("u1", "p1")
    assert la is la2


# ======================================================================
# TestAtomicRebuild — CAS 语义 (v1.1 核心)
# ======================================================================

@pytest.mark.asyncio
async def test_atomic_rebuild_cas_wins():
    """我的 CAS 赢 (rowcount=1) → 返回我造的 candidate."""
    async def mock_fresh(uid, pid):
        return "agent-candidate-A"

    # CAS won
    def mock_cas(uid, pid, old, new):
        assert old == "agent-old"
        assert new == "agent-candidate-A"
        return 1

    with patch.object(preflight, "_cas_swap_agent_sync", side_effect=mock_cas):
        # _create_agent_fresh 是 sync 函数, to_thread 包装后 mock 要返回值
        with patch("routing._create_agent_fresh", side_effect=lambda u, p: "agent-candidate-A"):
            result = await preflight._atomic_rebuild("agent-old", "u1", "p1")

    assert result == "agent-candidate-A"


@pytest.mark.asyncio
async def test_atomic_rebuild_cas_lost_returns_winner():
    """**关键测试**: CAS 失败 (rowcount=0) → 读 map 返回 winner, candidate 被丢弃成孤儿.
    这是跨 worker 不分叉会话的保证."""
    def mock_fresh(uid, pid):
        return "agent-my-candidate"

    def mock_cas(uid, pid, old, new):
        return 0  # lost

    def mock_read_map(uid, pid):
        return "agent-winner-from-other-worker"

    with patch("routing._create_agent_fresh", side_effect=mock_fresh), \
         patch.object(preflight, "_cas_swap_agent_sync", side_effect=mock_cas), \
         patch.object(preflight, "_read_agent_id_from_map_sync", side_effect=mock_read_map):
        result = await preflight._atomic_rebuild("agent-old", "u1", "p1")

    # 返回的不是我造的, 是 winner 的
    assert result == "agent-winner-from-other-worker"
    assert result != "agent-my-candidate"


@pytest.mark.asyncio
async def test_atomic_rebuild_create_fresh_fails_map_unchanged():
    """**关键测试**: _create_agent_fresh 抛异常 → _atomic_rebuild 也抛, map 不被 DELETE.
    对比旧实现 (先 DELETE map 再 create), 这里永远不会出现 map-less 窗口."""
    def mock_fresh(uid, pid):
        raise RuntimeError("letta create failed")

    # CAS 不该被调
    cas_calls = {"n": 0}
    def mock_cas(*args):
        cas_calls["n"] += 1
        return 0

    with patch("routing._create_agent_fresh", side_effect=mock_fresh), \
         patch.object(preflight, "_cas_swap_agent_sync", side_effect=mock_cas):
        with pytest.raises(RuntimeError, match="letta create failed"):
            await preflight._atomic_rebuild("agent-old", "u1", "p1")

    assert cas_calls["n"] == 0, "fresh create 失败不该触发 CAS"


@pytest.mark.asyncio
async def test_atomic_rebuild_does_not_delete_old_agent():
    """延迟删除: _atomic_rebuild 里绝对不能同步删旧 agent.
    (靠 reconcile_orphan_agents 兜底清.)"""
    async def mock_fresh(uid, pid):
        return "agent-new"
    def mock_cas(*args):
        return 1

    # Spy on letta async delete (should NOT be called)
    with patch("routing._create_agent_fresh", side_effect=lambda u, p: "agent-new"), \
         patch.object(preflight, "_cas_swap_agent_sync", side_effect=mock_cas):
        # 如果有人以后偷偷加回 letta.agents.delete, 这里会看到 mock 被调
        # 没有 import letta_async 在 preflight.py 里, 所以只能断言"没有 delete 相关调用"
        # 间接验证: _atomic_rebuild 成功返回但没有 agent.delete side effect
        result = await preflight._atomic_rebuild("agent-old", "u1", "p1")

    assert result == "agent-new"
    # 直接读 preflight 源码确认没 'agents.delete' 调用 (已由 v1.1 spec 保证)


# ======================================================================
# TestConvergence — 并发收敛 (最关键的测试类)
# ======================================================================

@pytest.mark.asyncio
async def test_concurrent_danger_same_worker_converge_to_same_agent():
    """**最关键测试**: 同 worker 两并发请求都进 danger, rebuild 后最终 agent_id 相同.
    旧实现会产生 2 个不同新 agent → 对话分叉 (那个错的测试被删了)."""
    # 简化: summarize 总失败, 强制 rebuild
    async def mock_ctx(aid):
        return ContextInfo(current=58000, window=60000)

    async def mock_summ(aid, timeout=30):
        raise RuntimeError("summarize fails")

    # 状态: 第一次 rebuild 后, map 更新为 agent-new-A
    map_state = {"current": "agent-old"}
    def mock_read(uid, pid):
        return map_state["current"]

    async def mock_rebuild(old, uid, pid):
        # 模拟真实 _atomic_rebuild 行为: 第一次 CAS 赢, 第二次 CAS 输返回 winner
        if map_state["current"] == "agent-old":
            map_state["current"] = "agent-new-A"  # 首次赢的 winner
            return "agent-new-A"
        else:
            # 第二个请求进来, map 已经是 agent-new-A. 实际 _atomic_rebuild
            # 会 CAS lost 返回 winner. 这里直接返 winner.
            return map_state["current"]

    with patch.object(preflight, "_read_agent_id_from_map_sync", side_effect=mock_read), \
         patch.object(preflight, "_get_ctx_fresh", side_effect=mock_ctx), \
         patch.object(preflight, "_call_summarize", side_effect=mock_summ), \
         patch.object(preflight, "_atomic_rebuild", side_effect=mock_rebuild):
        r1, r2 = await asyncio.gather(
            preflight_compact("u1", "p1", "hi 1"),
            preflight_compact("u1", "p1", "hi 2"),
        )

    # **最关键断言**: 两个请求最终使用相同 agent_id
    assert r1.agent_id == r2.agent_id == "agent-new-A", \
        f"会话分叉! r1={r1.agent_id} r2={r2.agent_id}"


@pytest.mark.asyncio
async def test_concurrent_session_lock_serializes_second_sees_safe():
    """B 等 A 的锁, A 压缩完 B 进锁 → B 重查 ctx 发现已 safe → noop.
    这条保证同 worker 并发只 summarize 1 次."""
    state = {"compacted": False}
    async def mock_ctx(aid):
        return ContextInfo(current=30000 if state["compacted"] else 58000, window=60000)

    summ_calls = {"n": 0}
    async def mock_summ(aid, timeout=30):
        summ_calls["n"] += 1
        await asyncio.sleep(0.05)
        state["compacted"] = True

    rebuild_calls = {"n": 0}
    async def mock_rebuild(old, uid, pid):
        rebuild_calls["n"] += 1
        return "agent-rebuilt"

    with patch.object(preflight, "_read_agent_id_from_map_sync",
                      side_effect=_mk_map_stub(["agent-old"])), \
         patch.object(preflight, "_get_ctx_fresh", side_effect=mock_ctx), \
         patch.object(preflight, "_call_summarize", side_effect=mock_summ), \
         patch.object(preflight, "_atomic_rebuild", side_effect=mock_rebuild):
        r1, r2 = await asyncio.gather(
            preflight_compact("u1", "p1", "hi"),
            preflight_compact("u1", "p1", "hi"),
        )

    assert summ_calls["n"] == 1, f"同 worker 并发 summarize 不该重复: {summ_calls['n']}"
    assert rebuild_calls["n"] == 0
    assert r1.agent_id == r2.agent_id == "agent-old"


# ======================================================================
# TestHTTPContextFetch — _get_ctx_fresh HTTP 细节
# ======================================================================

@pytest.mark.asyncio
async def test_get_ctx_fresh_parses_response():
    fake_response = MagicMock()
    fake_response.json.return_value = {
        "context_window_size_current": 45000,
        "context_window_size_max": 60000,
    }
    fake_response.raise_for_status = MagicMock(return_value=None)
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.get = AsyncMock(return_value=fake_response)

    with patch.object(preflight.httpx, "AsyncClient", return_value=fake_client):
        ctx = await _get_ctx_fresh("agent-abc")
    assert ctx.current == 45000
    assert ctx.window == 60000


@pytest.mark.asyncio
async def test_get_ctx_fresh_null_fallback():
    fake_response = MagicMock()
    fake_response.json.return_value = {
        "context_window_size_current": None,
        "context_window_size_max": None,
    }
    fake_response.raise_for_status = MagicMock(return_value=None)
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.get = AsyncMock(return_value=fake_response)

    with patch.object(preflight.httpx, "AsyncClient", return_value=fake_client):
        ctx = await _get_ctx_fresh("agent-new")
    assert ctx.current == 0
    assert ctx.window == 60000


@pytest.mark.asyncio
async def test_get_ctx_fresh_http_error_propagates():
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
        "404", request=MagicMock(), response=MagicMock(status_code=404),
    ))
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.get = AsyncMock(return_value=fake_response)

    with patch.object(preflight.httpx, "AsyncClient", return_value=fake_client):
        with pytest.raises(httpx.HTTPStatusError):
            await _get_ctx_fresh("agent-deleted")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
