"""适配层主入口 —— 聊天 API（/v1/*）"""
import json
import asyncio
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from fastapi.responses import HTMLResponse
from config import ADAPTER_API_KEY, VLLM_ENDPOINT, VLLM_API_KEY
from db import init_db
from auth import get_current_user
from routing import get_or_create_agent, get_or_create_org_resources, sync_org_resources_to_all_agents, letta
from webui_sync import reconcile_all
from knowledge_mirror import reconcile_mirrors

app = FastAPI(title="TeleAI Adapter")


@app.get("/knowledge", response_class=HTMLResponse)
async def admin_page():
    """知识管理页面"""
    try:
        with open("/app/admin-dashboard.html", "r") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(content="<h1>管理页面未部署</h1>", status_code=404)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    init_db()
    try:
        get_or_create_org_resources()
        sync_org_resources_to_all_agents()
    except Exception as e:
        logging.error(f"startup org resource init failed: {e}")
    # 启动时全量对账
    try:
        reconcile_all()
    except Exception as e:
        logging.error(f"startup reconcile failed: {e}")
    try:
        reconcile_mirrors()
    except Exception as e:
        logging.error(f"startup mirror reconcile failed: {e}")


async def _reconcile_loop():
    """每 5 分钟全量对账，覆盖新注册用户和增量同步失败的情况"""
    while True:
        await asyncio.sleep(300)
        try:
            reconcile_all()
        except Exception as e:
            logging.error(f"periodic reconcile failed: {e}")
        try:
            reconcile_mirrors()
        except Exception as e:
            logging.error(f"periodic mirror reconcile failed: {e}")


@app.on_event("startup")
async def start_reconcile_loop():
    asyncio.create_task(_reconcile_loop())


# ===== 聊天 API（在下方 /v1/models 之后定义）=====


async def non_stream_response(agent_id: str, message: str, model: str):
    response = letta.agents.messages.create(
        agent_id=agent_id, messages=[{"role": "user", "content": message}]
    )

    assistant_content = ""
    for msg in response.messages:
        if hasattr(msg, "content") and msg.content:
            msg_type = getattr(msg, "message_type", "")
            # 只取 assistant_message，跳过 system_alert / tool_call_message 等内部消息
            if msg_type and msg_type != "assistant_message":
                continue
            assistant_content = str(msg.content)

    return {
        "id": f"chatcmpl-{agent_id[:8]}",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": assistant_content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def fake_stream_from_letta(agent_id: str, message: str, model: str):
    """调 Letta 非流式，把结果转成 SSE chunk 格式返回（模拟流式）"""
    response = letta.agents.messages.create(
        agent_id=agent_id, messages=[{"role": "user", "content": message}]
    )

    assistant_content = ""
    for msg in response.messages:
        if hasattr(msg, "content") and msg.content:
            msg_type = getattr(msg, "message_type", "")
            # 只取 assistant_message，跳过 system_alert / tool_call_message 等内部消息
            if msg_type and msg_type != "assistant_message":
                continue
            assistant_content = str(msg.content)

    # 把完整回复按字符切成 chunk 发送（模拟打字效果）
    chunk_size = 4
    for i in range(0, len(assistant_content), chunk_size):
        text = assistant_content[i:i + chunk_size]
        chunk = {
            "id": f"chatcmpl-{agent_id[:8]}",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    final = {
        "id": f"chatcmpl-{agent_id[:8]}",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(final)}\n\n"
    yield "data: [DONE]\n\n"


# ===== 模型列表 =====


@app.get("/v1/models")
async def list_models(request: Request):
    """返回模型列表：vLLM 直连模型 + Letta 项目模型"""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if token != ADAPTER_API_KEY:
        from fastapi import HTTPException
        raise HTTPException(401, "Invalid API key")

    from db import get_db
    db = get_db()
    rows = db.execute("SELECT project_id, name FROM projects").fetchall()
    db.close()

    models = [
        # vLLM 直连（无记忆版）
        {
            "id": "qwen-no-mem",
            "object": "model",
            "owned_by": "vllm",
            "name": "Qwen3.5 无记忆版",
        },
    ]
    # Letta 项目模型
    for r in rows:
        models.append({
            "id": f"letta-{r['project_id']}",
            "object": "model",
            "owned_by": "ai-infra",
            "name": f"AI 助手 ({r['name']})",
        })

    return {"object": "list", "data": models}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()

    model = body.get("model", "")

    # qwen-no-mem: 直接透传给 vLLM，不走 Letta
    if model == "qwen-no-mem":
        import httpx

        # 关闭 thinking 模式，否则 content 为 null
        # 剔除 files 字段：qwen-no-mem 不处理 # 引用，避免无用数据透传
        _internal_keys = {"files", "_letta_files", "user_id", "user_name", "user_email", "user_role", "user"}
        vllm_body = {k: v for k, v in body.items() if k not in _internal_keys}
        vllm_body["model"] = "Qwen3.5-122B-A10B"
        vllm_body["chat_template_kwargs"] = {"enable_thinking": False}

        if body.get("stream", False):
            # 流式：直接透传 SSE
            async def proxy_stream():
                async with httpx.AsyncClient(timeout=300) as client:
                    async with client.stream(
                        "POST",
                        f"{VLLM_ENDPOINT}/chat/completions",
                        json=vllm_body,
                        headers={"Authorization": f"Bearer {VLLM_API_KEY}"},
                    ) as resp:
                        async for line in resp.aiter_lines():
                            if line:
                                yield line + "\n\n"
            return StreamingResponse(proxy_stream(), media_type="text/event-stream")
        else:
            async with httpx.AsyncClient(timeout=300) as client:
                resp = await client.post(
                    f"{VLLM_ENDPOINT}/chat/completions",
                    json=vllm_body,
                    headers={"Authorization": f"Bearer {VLLM_API_KEY}"},
                )
                return resp.json()

    # letta-* 模型: 走 Letta 记忆链路
    user = get_current_user(request, body)
    project = model.replace("letta-", "") if model.startswith("letta-") else "default"
    agent_id = get_or_create_agent(user["id"], project)

    user_message = None
    for msg in reversed(body.get("messages", [])):
        if msg["role"] == "user":
            user_message = msg["content"]
            break

    # 拦截 # 引用的 Letta 镜像文件
    # Open WebUI 会 pop 掉 files 做自己的 RAG，Pipeline Filter 提前备份到 _letta_files
    ref_files = body.get("_letta_files", []) or body.get("files", [])
    if ref_files:
        logging.info(f"# ref: {json.dumps(ref_files, ensure_ascii=False, default=str)}")
    if ref_files and user_message:
        from knowledge_mirror import get_letta_file_id_by_knowledge
        ref_context_parts = []
        for rf in ref_files:
            kid = rf.get("id") or rf.get("collection_name") or ""
            if not kid:
                continue
            mirror = get_letta_file_id_by_knowledge(kid)
            if not mirror:
                continue
            file_name = mirror.get("display_name") or rf.get("name", kid)
            letta_file_id = mirror["letta_file_id"]
            try:
                # 按 source_id（即 letta_file_id）过滤搜索该文件的 passages
                results = letta.agents.passages.search(
                    agent_id=agent_id, query=user_message, top_k=5,
                    source_id=letta_file_id,
                )
                texts = [getattr(r, "content", "") or getattr(r, "text", "") for r in getattr(results, "results", results) if getattr(r, "content", "") or getattr(r, "text", "")]
                if texts:
                    ref_context_parts.append(f"=== 引用文档：{file_name} ===\n" + "\n---\n".join(texts[:3]))
                    continue
            except Exception:
                pass
            # passages API 不支持 source_id 过滤时，提示 Agent 按文件名搜索
            ref_context_parts.append(f"[用户引用了文档「{file_name}」，请使用 archival memory search 搜索关键词「{file_name}」的相关内容来回答]")
        if ref_context_parts:
            context = "\n".join(ref_context_parts)
            user_message = f"{context}\n\n{user_message}"

    if not user_message:
        return {"error": "No user message found"}

    # Letta 目前用非流式调用，结果转成 SSE 格式返回（兼容 Open WebUI 的流式请求）
    if body.get("stream", False):
        return StreamingResponse(
            fake_stream_from_letta(agent_id, user_message, model),
            media_type="text/event-stream",
        )
    else:
        return await non_stream_response(agent_id, user_message, model)


# ===== 挂载管理 API =====

from admin_api import router as admin_router
app.include_router(admin_router)
