"""Mock vLLM / OpenAI-compatible LLM endpoint for Letta multimodal spike.

任务: dump 收到的 request body 到 /logs/requests.jsonl, 返一个最简响应让 Letta 不卡死.
关键观察点: messages[*].content 是 string 还是 list, image_url 段是否被透传.

可选 proxy 模式 (env PROXY_TARGET + PROXY_API_KEY + PROXY_MODEL): 把请求转发到真上游
(如 DashScope), 同时仍 dump request body. 用于端到端验证多模态模型真能看图.
"""
import json
import os
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

PROXY_TARGET = os.getenv("PROXY_TARGET", "")  # e.g. https://coding.dashscope.aliyuncs.com/v1
PROXY_API_KEY = os.getenv("PROXY_API_KEY", "")
PROXY_MODEL = os.getenv("PROXY_MODEL", "")    # 强制覆盖 model 字段, e.g. qwen3.6-plus

app = FastAPI(title="mock-vllm")
LOG_PATH = Path("/logs/requests.jsonl")
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def _summarize_content(content):
    """把 messages content 摘要化，方便人眼快速看 image_url 是否还在."""
    if isinstance(content, str):
        return {"shape": "string", "len": len(content), "preview": content[:200]}
    if isinstance(content, list):
        parts = []
        for p in content:
            if not isinstance(p, dict):
                parts.append({"type": "non-dict", "value": str(p)[:80]})
                continue
            t = p.get("type", "?")
            if t == "text":
                parts.append({"type": "text", "len": len(p.get("text", "")),
                              "preview": p.get("text", "")[:120]})
            elif t == "image_url":
                url = p.get("image_url", {}).get("url", "")
                parts.append({"type": "image_url",
                              "url_kind": "data:" if url.startswith("data:") else "http",
                              "url_len": len(url),
                              "url_preview": url[:80]})
            else:
                parts.append({"type": t, "raw_keys": list(p.keys())})
        return {"shape": "list", "parts": parts}
    return {"shape": type(content).__name__, "raw": str(content)[:200]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()

    # Dump full body + content summary
    summary = {
        "ts": time.time(),
        "stream": body.get("stream", False),
        "model": body.get("model"),
        "n_messages": len(body.get("messages", [])),
        "messages_content_shapes": [
            {"role": m.get("role"), "content": _summarize_content(m.get("content"))}
            for m in body.get("messages", [])
        ],
        "tools_count": len(body.get("tools", [])),
        "raw_body": body,
    }
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")

    # Proxy mode: 转发到真上游, 让 letta 拿到真模型回答
    if PROXY_TARGET and PROXY_API_KEY:
        upstream_body = dict(body)
        if PROXY_MODEL:
            upstream_body["model"] = PROXY_MODEL
        if upstream_body.get("stream"):
            # Stream 模式: client 不能放 with 块, 因为 with 退出会关连接, 而 generator 还在 yield
            async def proxy_stream():
                client = httpx.AsyncClient(timeout=120)
                try:
                    async with client.stream(
                        "POST", f"{PROXY_TARGET}/chat/completions",
                        json=upstream_body,
                        headers={"Authorization": f"Bearer {PROXY_API_KEY}"},
                    ) as r:
                        if r.status_code != 200:
                            err = await r.aread()
                            yield f"data: {json.dumps({'error': {'message': err.decode('utf-8', 'replace')[:300]}})}\n\n"
                            yield "data: [DONE]\n\n"
                            return
                        # aiter_bytes 原样转发, 不做 SSE 行重组 (上游已是 SSE 格式)
                        async for chunk in r.aiter_bytes():
                            if chunk:
                                yield chunk
                except Exception as e:
                    yield f"data: {json.dumps({'error': {'message': f'proxy stream failed: {e}'}})}\n\n"
                    yield "data: [DONE]\n\n"
                finally:
                    await client.aclose()
            return StreamingResponse(proxy_stream(), media_type="text/event-stream")

        # Non-stream
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(
                    f"{PROXY_TARGET}/chat/completions",
                    json=upstream_body,
                    headers={"Authorization": f"Bearer {PROXY_API_KEY}"},
                )
                with LOG_PATH.open("a") as f:
                    f.write(json.dumps({"ts": time.time(), "_upstream_status": r.status_code,
                                        "_upstream_body": r.json() if r.headers.get("content-type","").startswith("application/json") else r.text[:500]}, ensure_ascii=False) + "\n")
                return JSONResponse(r.json(), status_code=r.status_code)
        except Exception as e:
            with LOG_PATH.open("a") as f:
                f.write(json.dumps({"ts": time.time(), "_proxy_error": str(e)[:300]}, ensure_ascii=False) + "\n")
            return JSONResponse({"error": {"message": f"proxy failed: {e}"}}, status_code=502)

    fake_id = f"chatcmpl-mock-{uuid.uuid4().hex[:8]}"
    if body.get("stream"):
        async def gen():
            chunk = {
                "id": fake_id, "object": "chat.completion.chunk",
                "created": int(time.time()), "model": body.get("model", "mock"),
                "choices": [{"index": 0,
                             "delta": {"role": "assistant", "content": "[mock reply]"},
                             "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
            chunk2 = {**chunk,
                      "choices": [{"index": 0, "delta": {},
                                   "finish_reason": "stop"}]}
            yield f"data: {json.dumps(chunk2)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    return JSONResponse({
        "id": fake_id, "object": "chat.completion",
        "created": int(time.time()), "model": body.get("model", "mock"),
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "[mock reply]"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
    })


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    """Letta 启动 / 索引也会调 embedding，给个 dummy 向量避免卡住."""
    body = await request.json()
    inputs = body.get("input", [])
    if isinstance(inputs, str):
        inputs = [inputs]
    return JSONResponse({
        "object": "list",
        "data": [{"object": "embedding", "index": i,
                  "embedding": [0.0] * 1536}
                 for i, _ in enumerate(inputs)],
        "model": body.get("model", "mock-embed"),
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    })


@app.get("/v1/models")
async def list_models():
    return JSONResponse({
        "object": "list",
        "data": [{"id": "mock-llm", "object": "model", "created": 0, "owned_by": "spike"}],
    })


@app.get("/health")
async def health():
    return {"ok": True}
