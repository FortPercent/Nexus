"""Unit tests for stream_from_letta / non_stream_response metrics_sink (Issue #13 Day 2).

测试目标 (不需要真 letta server, 全 mock SDK):
  T1 non-stream: response.usage 被回填 metrics_sink.metrics_tokens_in/out
  T2 stream: 第一个非空 reasoning_message 触发 TTFT 记录
  T3 stream: usage_statistics event 回填 tokens
  T4 stream: notice_prefix 也算 TTFT 起点 (preflight rebuild 后立即有提示)
  T5 sink=None 兼容老调用点不崩

本地运行:
  cd adapter && python3 -m pytest tests/test_metrics_chat_sink.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import types
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

# 必要 env (config 模块导入时读)
import tempfile
_tmp_db_dir = tempfile.mkdtemp(prefix="metrics-day2-")
os.environ.setdefault("ADAPTER_API_KEY", "test")
os.environ.setdefault("OPENWEBUI_JWT_SECRET", "test")
os.environ.setdefault("OPENWEBUI_ADMIN_EMAIL", "test@example.com")
os.environ.setdefault("OPENWEBUI_ADMIN_PASSWORD", "test")
os.environ.setdefault("VLLM_ENDPOINT", "http://localhost")
os.environ.setdefault("VLLM_API_KEY", "test")
os.environ.setdefault("DB_PATH", os.path.join(_tmp_db_dir, "adapter.db"))
os.environ.setdefault("WEBUI_DB_PATH", os.path.join(_tmp_db_dir, "webui.db"))

import pytest


@pytest.fixture(autouse=True)
def _force_reload_main(monkeypatch):
    """test_chat_forward_wire / test_preflight 用 sys.modules.setdefault 装 stub.
    本文件需要真 main, 用 monkeypatch.delitem 临时移除关键模块强制 reload,
    pytest 在测试结束时自动 restore 到 setup 前的 sys.modules 状态 — 这样
    test_preflight 后续测试拿回它们 collect 阶段的模块对象引用, patch.object 才有效.
    """
    for m in ["main", "db", "config", "routing", "preflight",
              "middleware_metrics", "auth", "knowledge_mirror", "webui_sync"]:
        monkeypatch.delitem(sys.modules, m, raising=False)
    yield


def _make_sink():
    """模拟 request.state — 提供 SimpleNamespace 风格的属性容器."""
    s = types.SimpleNamespace()
    s.metrics_ttft_ms = None
    s.metrics_tokens_in = 0
    s.metrics_tokens_out = 0
    return s


class _FakeUsage:
    def __init__(self, prompt_tokens=0, completion_tokens=0):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeLettaResponse:
    """模拟 letta.agents.messages.create 返回."""
    def __init__(self, usage=None, messages=None):
        self.usage = usage or _FakeUsage()
        self.messages = messages or []


def _patch_main_letta(monkeypatch, fake_response):
    """patch main.letta + main._extract_letta_response 让 non_stream_response 不真调 letta."""
    import main as main_mod
    fake_letta = MagicMock()
    fake_letta.agents.messages.create = MagicMock(return_value=fake_response)
    monkeypatch.setattr(main_mod, "letta", fake_letta)
    monkeypatch.setattr(main_mod, "_extract_letta_response", lambda r: "fake assistant content")


def test_t1_non_stream_fills_tokens(monkeypatch):
    import main as main_mod
    fake_resp = _FakeLettaResponse(usage=_FakeUsage(prompt_tokens=123, completion_tokens=45))
    _patch_main_letta(monkeypatch, fake_resp)

    sink = _make_sink()
    out = asyncio.run(main_mod.non_stream_response(
        agent_id="agent-x", message="hi", model="letta-test", metrics_sink=sink
    ))
    assert sink.metrics_tokens_in == 123
    assert sink.metrics_tokens_out == 45
    assert out["usage"]["total_tokens"] == 168
    assert out["usage"]["prompt_tokens"] == 123


def test_t5_non_stream_sink_none_does_not_crash(monkeypatch):
    import main as main_mod
    _patch_main_letta(monkeypatch, _FakeLettaResponse())
    out = asyncio.run(main_mod.non_stream_response(
        agent_id="agent-x", message="hi", model="letta-test"
    ))
    # tokens 为 0 但不崩
    assert out["usage"]["total_tokens"] == 0


# ---------- streaming ----------

class _FakeStreamEvent:
    def __init__(self, message_type, **kw):
        self.message_type = message_type
        for k, v in kw.items():
            setattr(self, k, v)


async def _consume_stream(gen):
    """drain async generator → list of yielded chunks (用于 sse 流测试)."""
    out = []
    async for chunk in gen:
        out.append(chunk)
    return out


def _patch_main_letta_async_stream(monkeypatch, events):
    """patch main.letta_async.agents.messages.stream → 返一个 yields 给定 events 的 async generator."""
    import main as main_mod

    async def fake_stream_factory(**kwargs):
        async def gen():
            for ev in events:
                yield ev
        return gen()

    fake_letta_async = MagicMock()
    fake_letta_async.agents.messages.stream = fake_stream_factory
    monkeypatch.setattr(main_mod, "letta_async", fake_letta_async)


def test_t2_stream_ttft_on_first_reasoning(monkeypatch):
    import main as main_mod
    events = [
        # 模拟 letta 30ms 后才发第一个非空 chunk
        _FakeStreamEvent("ping"),
        _FakeStreamEvent("reasoning_message", reasoning="thinking..."),
        _FakeStreamEvent("assistant_message", content="reply"),
        _FakeStreamEvent("usage_statistics", prompt_tokens=10, completion_tokens=5),
    ]
    _patch_main_letta_async_stream(monkeypatch, events)
    sink = _make_sink()

    asyncio.run(_consume_stream(main_mod.stream_from_letta(
        agent_id="agent-x", message="hi", model="letta-test", metrics_sink=sink
    )))
    assert sink.metrics_ttft_ms is not None
    assert sink.metrics_ttft_ms >= 0
    # 应该在第一个 reasoning chunk 时记的, 不是 usage_statistics 时
    # 验证: ttft 应该比总流时间小很多 (但都很快, 难严格断言)


def test_t3_stream_usage_statistics_fills_tokens(monkeypatch):
    import main as main_mod
    events = [
        _FakeStreamEvent("assistant_message", content="hi back"),
        _FakeStreamEvent("usage_statistics", prompt_tokens=88, completion_tokens=22),
    ]
    _patch_main_letta_async_stream(monkeypatch, events)
    sink = _make_sink()

    asyncio.run(_consume_stream(main_mod.stream_from_letta(
        agent_id="agent-x", message="hi", model="letta-test", metrics_sink=sink
    )))
    assert sink.metrics_tokens_in == 88
    assert sink.metrics_tokens_out == 22


def test_t4_notice_prefix_counts_as_ttft(monkeypatch):
    import main as main_mod
    events = []  # 空流, 没有任何 letta event
    _patch_main_letta_async_stream(monkeypatch, events)
    sink = _make_sink()

    asyncio.run(_consume_stream(main_mod.stream_from_letta(
        agent_id="agent-x", message="hi", model="letta-test",
        notice_prefix="对话已重置", metrics_sink=sink,
    )))
    # notice_prefix 立即 yield, 算 TTFT 起点
    assert sink.metrics_ttft_ms is not None
    assert sink.metrics_ttft_ms >= 0


def test_t5_stream_sink_none_does_not_crash(monkeypatch):
    import main as main_mod
    events = [
        _FakeStreamEvent("assistant_message", content="hi"),
        _FakeStreamEvent("usage_statistics", prompt_tokens=1, completion_tokens=1),
    ]
    _patch_main_letta_async_stream(monkeypatch, events)
    # sink=None, 不应崩
    chunks = asyncio.run(_consume_stream(main_mod.stream_from_letta(
        agent_id="agent-x", message="hi", model="letta-test", metrics_sink=None,
    )))
    assert any("hi" in c for c in chunks)
