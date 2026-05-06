"""Letta multimodal SDK spike.

目标: 验证 Letta SDK 在 messages.create 时是否完整透传 OpenAI multimodal
content 数组 (text + image_url) 给底层 LLM provider.

观察点 (mock-vllm 收到的 request body):
  R1. messages[*].content 还是 list 吗 (vs 被 letta 压成 string)?
  R2. image_url 段还在吗 (vs 被 letta 拆掉只留 text)?
  R3. image_url.url 完整吗 (data: base64 是否被截断)?

三种结果对应的后续动作:
  - PASS  完整透传 → multimodal-passthrough v2 Layer 1+2 直接落地, 0 patch
  - HALF  只保留 text → 需 letta-patches/multimodal_passthrough.py
  - FAIL  报 400 / list 不收 → 多模态走 qwen-no-mem 直连绕路

用法:
  pip install 'letta-client>=0.1.0' pillow
  python run_spike.py
"""
import base64
import io
import json
import sys
import time
from pathlib import Path

LETTA_BASE_URL = "http://localhost:8283"
MOCK_VLLM_LOG = Path(__file__).parent / "logs" / "requests.jsonl"
TEST_IMAGE = Path(__file__).parent / "fixtures" / "test.png"


def _ensure_test_image() -> bytes:
    """没有 test.png 就生成一张写着 'AI Infra spike' 的 PNG."""
    if TEST_IMAGE.exists():
        return TEST_IMAGE.read_bytes()
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        sys.exit("需要 pillow 生成 fixture: pip install pillow")
    img = Image.new("RGB", (320, 80), "white")
    d = ImageDraw.Draw(img)
    d.text((10, 30), "AI Infra spike", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    TEST_IMAGE.parent.mkdir(parents=True, exist_ok=True)
    TEST_IMAGE.write_bytes(buf.getvalue())
    return buf.getvalue()


def _wait_letta(retries=30):
    import urllib.request
    import urllib.error
    for i in range(retries):
        try:
            urllib.request.urlopen(f"{LETTA_BASE_URL}/v1/health/", timeout=2)
            return
        except (urllib.error.URLError, ConnectionError):
            time.sleep(2)
    sys.exit("letta server 没起来 (尝试 30 次共 60s)")


def main():
    print("[spike] checking letta server at", LETTA_BASE_URL)
    _wait_letta()

    print("[spike] preparing test image")
    img_bytes = _ensure_test_image()
    img_b64 = base64.b64encode(img_bytes).decode()

    print("[spike] truncating mock-vllm log to baseline")
    if MOCK_VLLM_LOG.exists():
        MOCK_VLLM_LOG.unlink()

    try:
        from letta_client import Letta
    except ImportError:
        sys.exit("需要 letta-client: pip install 'letta-client>=0.1.0'")

    client = Letta(base_url=LETTA_BASE_URL)

    print("[spike] creating ephemeral agent")
    # 注: llm_config 字段名可能随 letta sdk 版本变. 失败时 fallback 到默认 (server 端 OPENAI_API_BASE 也指向 mock-vllm)
    agent = client.agents.create(
        name=f"spike-mm-{int(time.time())}",
        memory_blocks=[
            {"label": "human", "value": "test user"},
            {"label": "persona", "value": "test agent for multimodal spike"},
        ],
        model="openai-proxy/mock-llm",
        embedding="openai/text-embedding-3-small",
    )
    print("[spike] agent_id =", agent.id)

    print("[spike] sending multimodal message (Letta-native image schema)")
    try:
        resp = client.agents.messages.create(
            agent_id=agent.id,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "图里写了什么字？"},
                    {"type": "image",
                     "source": {
                         "type": "base64",
                         "media_type": "image/png",
                         "data": img_b64,
                     }},
                ],
            }],
        )
        print("[spike] letta accepted image content (no 422)")
    except Exception as e:
        msg = str(e)[:300]
        print(f"[spike] letta rejected image content: {type(e).__name__}: {msg}")
        print("[spike] -> 结果: FAIL (要走兜底方案: 多模态走 qwen-no-mem 直连)")
        return

    # 给 mock-vllm 一点时间写日志
    time.sleep(1)

    if not MOCK_VLLM_LOG.exists():
        print("[spike] mock-vllm log 没出现, letta 可能压根没调 LLM (检查 letta 是否真连了 mock-vllm)")
        return

    print(f"\n[spike] === mock-vllm 收到的请求 (LOG: {MOCK_VLLM_LOG}) ===\n")
    with MOCK_VLLM_LOG.open() as f:
        records = [json.loads(line) for line in f if line.strip()]

    if not records:
        print("[spike] log 文件为空")
        return

    # 找含 user message 的那条 (可能有多条 — letta 内部还会调 LLM 做其他事)
    user_relevant = []
    for rec in records:
        for m in rec.get("messages_content_shapes", []):
            if m.get("role") == "user":
                content = m.get("content", {})
                user_relevant.append((rec, content))
                break

    if not user_relevant:
        print("[spike] 没找到 user message, dump 全部:")
        for rec in records:
            print(json.dumps(rec, ensure_ascii=False, indent=2)[:600])
        return

    print(f"[spike] 共找到 {len(user_relevant)} 条带 user message 的请求, 看最后一条:\n")
    last_rec, user_content = user_relevant[-1]
    print(json.dumps({
        "stream": last_rec["stream"],
        "model": last_rec["model"],
        "n_messages": last_rec["n_messages"],
        "user_content": user_content,
    }, ensure_ascii=False, indent=2))

    # 判定
    print("\n[spike] === 判定 ===\n")
    shape = user_content.get("shape")
    if shape == "list":
        parts = user_content.get("parts", [])
        types = [p.get("type") for p in parts]
        if "image_url" in types and "text" in types:
            print("✅ PASS: image_url + text 都被透传, content 仍是 list")
            print("    -> Layer 1+2 直接落地, 不需要 patch letta")
        elif "text" in types and "image_url" not in types:
            print("⚠️  HALF: image_url 段被 letta 拆掉, 只剩 text")
            print("    -> 需 letta-patches/multimodal_passthrough.py")
        else:
            print(f"❓ UNKNOWN: types={types}, 看上面 raw 自己判断")
    elif shape == "string":
        if "image_url" in user_content.get("preview", "") or "data:image" in user_content.get("preview", ""):
            print("⚠️  HALF: content 被压成 string 但 image_url 以文本形式残留 (vLLM 不会理解为图)")
        else:
            print("⚠️  HALF: content 被压成 string, image_url 完全丢失")
        print("    -> 需 letta-patches 或走 qwen-no-mem 直连绕路")
    else:
        print(f"❓ UNKNOWN shape={shape}, 自己看上面 raw")

    print("\n[spike] 完整 raw_body 在", MOCK_VLLM_LOG)
    print("[spike] 清理: client.agents.delete(agent.id)  # 留作排查可不清")


if __name__ == "__main__":
    main()
