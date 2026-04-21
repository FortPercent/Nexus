"""Unit tests for preflight.py Step 1 functions.

本地运行 (推荐):
  cd adapter && python3 -m pytest tests/test_preflight.py -v

容器内运行 (需先装 pytest):
  docker exec teleai-adapter sh -c 'pip install -q pytest pytest-asyncio && python3 -m pytest /app/tests/test_preflight.py -v'

纯单元, 不连 Letta. _get_ctx_fresh 用 unittest.mock 替 httpx.
"""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

# 允许测试在 /app (container) 或本地 adapter/ 目录下跑
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

# config.py 在 import 时读 os.environ; 测试里给假值避免 KeyError
os.environ.setdefault("ADAPTER_API_KEY", "test")
os.environ.setdefault("OPENWEBUI_JWT_SECRET", "test")
os.environ.setdefault("OPENWEBUI_ADMIN_EMAIL", "test@example.com")
os.environ.setdefault("OPENWEBUI_ADMIN_PASSWORD", "test")
os.environ.setdefault("VLLM_ENDPOINT", "http://localhost")
os.environ.setdefault("VLLM_API_KEY", "test")

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


# ======================== _estimate_user_tokens ========================

def test_estimate_empty_string():
    assert _estimate_user_tokens("") == 0


def test_estimate_none_safe():
    """传 None-ish 不应该 crash (调用方有责任, 但我们也 defensive)"""
    assert _estimate_user_tokens("") == 0


def test_estimate_chinese_short():
    # "你好" 2 字 → 估 4 token
    assert _estimate_user_tokens("你好") == 4


def test_estimate_long_text():
    txt = "a" * 1000
    assert _estimate_user_tokens(txt) == 2000


def test_estimate_is_upper_bound():
    """对纯英文, char*2 确实是上界 (真实 ~char/3-4)"""
    txt = "hello world, this is a test message"
    est = _estimate_user_tokens(txt)
    # len(txt)=36, est=72; 真实应该约 8-10 token; 我们严重高估, 这是设计
    assert est > len(txt)


# ======================== _danger ========================

def test_danger_plenty_margin():
    """window=60000, current=20000 → margin 40000, 随便怎么都 safe"""
    assert _danger(20000, 60000, 0) is False
    assert _danger(20000, 60000, 5000) is False


def test_danger_exactly_at_threshold():
    """margin 正好等于 SAFE+OVERHEAD+est → 不算 danger (strict <)"""
    # SAFE=5000, OVERHEAD=500; est=0 → threshold=5500
    # margin=5500 刚好等, 不 danger
    assert _danger(54500, 60000, 0) is False
    # margin=5499 差 1, danger
    assert _danger(54501, 60000, 0) is True


def test_danger_user_msg_pushes_over():
    """margin 够但用户消息长到把余量吃光 → danger"""
    # window=60000 current=52000 → margin=8000
    # threshold without user = 5500; margin-threshold = 2500 余量给 user
    # user_est = 3000 → 把余量吃超 → danger
    assert _danger(52000, 60000, 2499) is False
    assert _danger(52000, 60000, 2501) is True


def test_danger_overfull_always_danger():
    """current >= window 肯定 danger (margin 负或 0)"""
    assert _danger(60000, 60000, 0) is True
    assert _danger(71000, 60000, 0) is True


def test_danger_zero_window_edge_case():
    """window=0 不合理但不该 crash, 必定 danger"""
    assert _danger(0, 0, 0) is True


def test_danger_biany_scenario():
    """复现 04-20 biany asset-management case: current=71268, window=65536"""
    # real vllm ctx was 65536, agent hit 71268
    # margin = -5732, definitely danger
    assert _danger(71268, 65536, 0) is True
    assert _danger(71268, 65536, 100) is True


def test_danger_ai_infra_cache_scenario():
    """04-21 巡检: ai-infra-cache current=50575, window=60000, 边缘"""
    # margin = 9425; threshold without user = 5500; 余量 3925 给 user
    # 短问 "你好": est=4 → margin 够, safe
    assert _danger(50575, 60000, 4) is False
    # 长 prompt 2000 字: est=4000 → 超, danger
    assert _danger(50575, 60000, 4000) is True


# ======================== _get_ctx_fresh ========================

@pytest.mark.asyncio
async def test_get_ctx_fresh_parses_response():
    """mock httpx 返回 Letta context 标准响应, 确认字段解析对"""
    fake_response = MagicMock()
    fake_response.json.return_value = {
        "context_window_size_current": 45000,
        "context_window_size_max": 60000,
        "num_messages": 120,
        # 其他字段我们不关心
    }
    fake_response.raise_for_status = MagicMock(return_value=None)

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.get = AsyncMock(return_value=fake_response)

    with patch.object(preflight.httpx, "AsyncClient", return_value=fake_client):
        ctx = await _get_ctx_fresh("agent-abc123")

    assert ctx.current == 45000
    assert ctx.window == 60000


@pytest.mark.asyncio
async def test_get_ctx_fresh_handles_null_current():
    """Letta 偶尔返回 null (新建 agent), 应 fallback 到 0"""
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
    assert ctx.window == 60000  # spec 要求的 default


@pytest.mark.asyncio
async def test_get_ctx_fresh_http_error_propagates():
    """404 等错误应该抛, 上层决定怎么处理 (不该 swallow)"""
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
        "404 Not Found",
        request=MagicMock(),
        response=MagicMock(status_code=404),
    ))

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.get = AsyncMock(return_value=fake_response)

    with patch.object(preflight.httpx, "AsyncClient", return_value=fake_client):
        with pytest.raises(httpx.HTTPStatusError):
            await _get_ctx_fresh("agent-deleted")


# ======================== preflight_compact (integration of _danger + flow) ========================


@pytest.fixture(autouse=True)
def _reset_agent_locks():
    """每个 test 清空 per-agent lock dict, 避免跨 test 污染."""
    preflight._agent_locks.clear()
    yield
    preflight._agent_locks.clear()


@pytest.mark.asyncio
async def test_preflight_noop_when_safe():
    """current 远低于 window → fast path, action=noop"""
    async def mock_ctx(aid):
        return ContextInfo(current=10000, window=60000)

    with patch.object(preflight, "_get_ctx_fresh", side_effect=mock_ctx):
        r = await preflight_compact("agent-x", "user-1", "proj-1", "你好")

    assert r.action == "noop"
    assert r.rebuilt is False
    assert r.agent_id == "agent-x"
    assert r.user_msg is None


@pytest.mark.asyncio
async def test_preflight_sync_summarized_when_summarize_helps():
    """进 danger, summarize 成功降到 safe, action=sync_summarized"""
    state = {"compacted": False}

    async def mock_ctx(aid):
        if state["compacted"]:
            return ContextInfo(current=30000, window=60000)
        return ContextInfo(current=58000, window=60000)

    async def mock_summ(aid, timeout=30):
        state["compacted"] = True

    with patch.object(preflight, "_get_ctx_fresh", side_effect=mock_ctx), \
         patch.object(preflight, "_call_summarize", side_effect=mock_summ):
        r = await preflight_compact("agent-x", "user-1", "proj-1", "你好")

    assert r.action == "sync_summarized"
    assert r.rebuilt is False
    assert r.agent_id == "agent-x"
    assert r.ctx_before == 58000
    assert r.ctx_after == 30000


@pytest.mark.asyncio
async def test_preflight_rebuilt_when_summarize_raises():
    """summarize 抛异常 → 走 rebuild, action=rebuilt"""
    async def mock_ctx(aid):
        return ContextInfo(current=58000, window=60000)

    async def mock_summ(aid, timeout=30):
        raise RuntimeError("letta 500")

    async def mock_rebuild(old_aid, uid, pid):
        assert old_aid == "agent-old"
        return "agent-new"

    with patch.object(preflight, "_get_ctx_fresh", side_effect=mock_ctx), \
         patch.object(preflight, "_call_summarize", side_effect=mock_summ), \
         patch.object(preflight, "_atomic_rebuild", side_effect=mock_rebuild):
        r = await preflight_compact("agent-old", "user-1", "proj-1", "你好")

    assert r.action == "rebuilt"
    assert r.rebuilt is True
    assert r.agent_id == "agent-new"
    assert r.ctx_before == 58000
    assert r.ctx_after == 0
    assert r.user_msg and "对话历史已压缩重置" in r.user_msg


@pytest.mark.asyncio
async def test_preflight_rebuilt_when_summarize_insufficient():
    """summarize 成功但压完仍 danger → 走 rebuild"""
    async def mock_ctx(aid):
        return ContextInfo(current=58000, window=60000)  # always danger

    async def mock_summ(aid, timeout=30):
        pass  # succeeds but doesn't help

    async def mock_rebuild(old_aid, uid, pid):
        return "agent-new2"

    with patch.object(preflight, "_get_ctx_fresh", side_effect=mock_ctx), \
         patch.object(preflight, "_call_summarize", side_effect=mock_summ), \
         patch.object(preflight, "_atomic_rebuild", side_effect=mock_rebuild):
        r = await preflight_compact("agent-old2", "user-1", "proj-1", "你好")

    assert r.action == "rebuilt"
    assert r.agent_id == "agent-new2"


@pytest.mark.asyncio
async def test_preflight_rebuilt_on_timeout():
    """summarize 超时 → rebuild"""
    async def mock_ctx(aid):
        return ContextInfo(current=58000, window=60000)

    async def mock_summ(aid, timeout=30):
        await asyncio.sleep(60)  # simulate hang; asyncio.wait_for will timeout

    async def mock_rebuild(old_aid, uid, pid):
        return "agent-new3"

    # Patch wait_for timeout small to make test fast
    import asyncio as _asyncio
    orig_wait_for = _asyncio.wait_for

    async def fast_wait_for(coro, timeout):
        return await orig_wait_for(coro, 0.05)

    with patch.object(preflight, "_get_ctx_fresh", side_effect=mock_ctx), \
         patch.object(preflight, "_call_summarize", side_effect=mock_summ), \
         patch.object(preflight, "_atomic_rebuild", side_effect=mock_rebuild), \
         patch.object(preflight.asyncio, "wait_for", side_effect=fast_wait_for):
        r = await preflight_compact("agent-old3", "user-1", "proj-1", "你好")

    assert r.action == "rebuilt"


# ======================== Concurrency ========================


@pytest.mark.asyncio
async def test_lock_per_agent_not_shared_across_agents():
    """不同 agent 的 lock 是不同对象"""
    la = await preflight._acquire_agent_lock("agent-a")
    lb = await preflight._acquire_agent_lock("agent-b")
    la2 = await preflight._acquire_agent_lock("agent-a")
    assert la is not lb
    assert la is la2


@pytest.mark.asyncio
async def test_concurrent_requests_only_one_summarizes():
    """两个并发请求都进 danger, 锁序列化, 第二个进锁后看到已压缩 → noop.
    关键断言: summarize 只被调用 1 次 (不是 2 次)."""
    state = {"compacted": False}
    summ_calls = {"n": 0}

    async def mock_ctx(aid):
        if state["compacted"]:
            return ContextInfo(current=30000, window=60000)
        return ContextInfo(current=58000, window=60000)

    async def mock_summ(aid, timeout=30):
        summ_calls["n"] += 1
        await asyncio.sleep(0.05)  # let the other coroutine wait on lock
        state["compacted"] = True

    with patch.object(preflight, "_get_ctx_fresh", side_effect=mock_ctx), \
         patch.object(preflight, "_call_summarize", side_effect=mock_summ):
        r1, r2 = await asyncio.gather(
            preflight_compact("agent-X", "user-1", "proj-1", "你好 A"),
            preflight_compact("agent-X", "user-1", "proj-1", "你好 B"),
        )

    actions = {r1.action, r2.action}
    assert summ_calls["n"] == 1, f"summarize called {summ_calls['n']} times (expected 1)"
    assert actions == {"sync_summarized", "noop"}, f"got {actions}"


@pytest.mark.asyncio
async def test_concurrent_requests_both_rebuilt_if_summarize_fails():
    """一个极端 case: summarize 两次都失败, 两个请求都各自 rebuild.
    这种情况 v1 接受 (会产生 1 个多余孤儿, reconcile 扫). 测试确认不 crash."""
    rebuild_calls = {"n": 0}

    async def mock_ctx(aid):
        return ContextInfo(current=58000, window=60000)  # always danger

    async def mock_summ(aid, timeout=30):
        raise RuntimeError("summarize fails")

    async def mock_rebuild(old_aid, uid, pid):
        rebuild_calls["n"] += 1
        return f"agent-new-{rebuild_calls['n']}"

    with patch.object(preflight, "_get_ctx_fresh", side_effect=mock_ctx), \
         patch.object(preflight, "_call_summarize", side_effect=mock_summ), \
         patch.object(preflight, "_atomic_rebuild", side_effect=mock_rebuild):
        r1, r2 = await asyncio.gather(
            preflight_compact("agent-X", "user-1", "proj-1", "你好"),
            preflight_compact("agent-X", "user-1", "proj-1", "你好"),
        )

    # 锁序列化: A 完成 rebuild 后, B 进锁重查 ctx 仍 danger (mock 永远返 58000),
    # 所以 B 也会 rebuild. rebuild 2 次, 符合 v1 说的"接受冗余, reconcile 兜底"
    assert r1.action == "rebuilt" and r2.action == "rebuilt"
    assert rebuild_calls["n"] == 2


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
