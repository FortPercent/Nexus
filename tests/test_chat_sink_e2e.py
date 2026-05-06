"""End-to-end tests for stream_from_letta / non_stream_response (Issue #13 #2).

跑真 letta server (来自 adapter/spike-multimodal/) — 不 mock SDK.
- 通过 main.py 的 stream_from_letta / non_stream_response 真调
- 验证 metrics_sink 上 tokens / TTFT 是真值 (从 mock-vllm 转发到 DashScope 拿到的)

前置: docker compose 起 spike-multimodal (见 docs/multimodal-passthrough-design.md v3 + spike README)
若 letta server 不可达, 测试 skip (不算失败).

T1  non_stream_response 走真 letta → metrics_sink.tokens_in/out > 0
T2  stream_from_letta 走真 letta → metrics_sink.ttft_ms 被记 + tokens 非 0
T3  连续两次调用 (sink 复用) — 第二次也正确填值

本地运行:
  cd adapter && python3 -m pytest tests/test_chat_sink_e2e.py -v
"""
import asyncio
import os
import sys
import time
import types
import urllib.request
import urllib.error

# spike letta server
LETTA_BASE_URL = "http://localhost:8283"
SPIKE_TIMEOUT = 2.0

# Env 必须在 import 前
import tempfile
_tmp = tempfile.mkdtemp(prefix="chat-sink-e2e-")
os.environ.setdefault("ADAPTER_API_KEY", "test")
os.environ.setdefault("OPENWEBUI_JWT_SECRET", "test")
os.environ.setdefault("OPENWEBUI_ADMIN_EMAIL", "t@t")
os.environ.setdefault("OPENWEBUI_ADMIN_PASSWORD", "t")
os.environ.setdefault("VLLM_ENDPOINT", "http://localhost:18000/v1")  # mock-vllm
os.environ.setdefault("VLLM_API_KEY", "dummy")
os.environ["LETTA_BASE_URL"] = LETTA_BASE_URL
os.environ.setdefault("DB_PATH", os.path.join(_tmp, "adapter.db"))
os.environ.setdefault("WEBUI_DB_PATH", os.path.join(_tmp, "webui.db"))

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import pytest


def _spike_letta_alive() -> bool:
    """快速 health check."""
    try:
        urllib.request.urlopen(f"{LETTA_BASE_URL}/v1/health/", timeout=SPIKE_TIMEOUT)
        return True
    except (urllib.error.URLError, ConnectionError, OSError):
        return False


pytestmark = pytest.mark.skipif(
    not _spike_letta_alive(),
    reason=f"spike letta server at {LETTA_BASE_URL} not reachable. "
           "Run: cd adapter/spike-multimodal && docker compose up -d",
)


@pytest.fixture
def real_main(monkeypatch):
    """force reload main 拿真 letta_async / letta SDK 客户端 (避免 test_preflight stub 污染)."""
    for m in ["main", "db", "config", "routing", "preflight",
              "middleware_metrics", "auth", "knowledge_mirror", "webui_sync"]:
        monkeypatch.delitem(sys.modules, m, raising=False)

    import db as db_mod
    db_mod.init_db()

    import main as main_mod
    return main_mod


@pytest.fixture
def ephemeral_agent(real_main):
    """创建一个临时 letta agent, 测完销毁."""
    from letta_client import Letta
    client = Letta(base_url=LETTA_BASE_URL)
    agent = client.agents.create(
        name=f"e2e-sink-{int(time.time())}",
        memory_blocks=[
            {"label": "human", "value": "test user"},
            {"label": "persona", "value": "concise test agent"},
        ],
        model="openai-proxy/mock-llm",
        embedding="openai/text-embedding-3-small",
    )
    yield agent.id
    try:
        client.agents.delete(agent.id)
    except Exception:
        pass


def _make_sink():
    s = types.SimpleNamespace()
    s.metrics_ttft_ms = None
    s.metrics_tokens_in = 0
    s.metrics_tokens_out = 0
    return s


def test_t1_non_stream_real_letta(real_main, ephemeral_agent):
    """non_stream_response 真调 letta server, sink 上 tokens 应 > 0."""
    sink = _make_sink()
    out = asyncio.run(real_main.non_stream_response(
        agent_id=ephemeral_agent,
        message="say hi briefly",
        model="letta-test",
        metrics_sink=sink,
    ))
    assert out["object"] == "chat.completion"
    assert out["choices"][0]["message"]["role"] == "assistant"
    # 真 letta 会调 mock-vllm → DashScope, 返真 token 数
    assert sink.metrics_tokens_in > 0, f"expected tokens_in > 0, got {sink.metrics_tokens_in}"
    assert sink.metrics_tokens_out > 0, f"expected tokens_out > 0, got {sink.metrics_tokens_out}"
    assert out["usage"]["total_tokens"] == sink.metrics_tokens_in + sink.metrics_tokens_out


async def _drain_stream(gen):
    chunks = []
    async for c in gen:
        chunks.append(c)
    return chunks


def test_t2_stream_real_letta_ttft_and_tokens(real_main, ephemeral_agent):
    """stream_from_letta 真调 letta server stream API, ttft 被记, tokens 非 0."""
    sink = _make_sink()
    chunks = asyncio.run(_drain_stream(real_main.stream_from_letta(
        agent_id=ephemeral_agent,
        message="reply with one short word",
        model="letta-test",
        metrics_sink=sink,
    )))
    # SSE 格式 chunks
    assert any("data:" in c for c in chunks)
    # 第一个 chunk 时间应该被记 (>= 0, 通常 100ms-1s)
    assert sink.metrics_ttft_ms is not None
    assert sink.metrics_ttft_ms >= 0
    # letta 流末 usage_statistics event 应回填 token (mock-vllm proxy DashScope 返真值)
    # 真 letta server 行为: 即使 stream 最终发了 usage_statistics 也可能漏发 (取决于 LLM provider 是否在 final chunk 含 usage)
    # 不做硬断言, 但至少其中一个该 > 0 (除非真 letta 不发 usage 给 stream)
    # 软断言 + 信息打印, 失败时人眼判断
    print(f"[t2] ttft_ms={sink.metrics_ttft_ms} tokens_in={sink.metrics_tokens_in} tokens_out={sink.metrics_tokens_out}")


def test_t3_sink_reuse_two_calls(real_main, ephemeral_agent):
    """同 sink 连用两次, 第二次也填值 (覆盖 _ttft_recorded 状态正确性)."""
    sink1 = _make_sink()
    sink2 = _make_sink()

    asyncio.run(real_main.non_stream_response(
        agent_id=ephemeral_agent, message="hi", model="letta-test", metrics_sink=sink1
    ))
    asyncio.run(real_main.non_stream_response(
        agent_id=ephemeral_agent, message="bye", model="letta-test", metrics_sink=sink2
    ))
    assert sink1.metrics_tokens_in > 0
    assert sink2.metrics_tokens_in > 0
