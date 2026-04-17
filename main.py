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
from routing import get_or_create_agent, get_or_create_org_resources, sync_org_resources_to_all_agents, letta, letta_async
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


def _extract_letta_response(response) -> str:
    """从 Letta 响应提取：reasoning 包 <think>，工具调用/返回人话化，最后拼 assistant_message"""
    parts = []
    in_think = False
    assistant_content = ""
    pending_tool = None  # (name, args)

    def close_think():
        nonlocal in_think
        if in_think:
            parts.append("</think>")
            in_think = False

    def open_think():
        nonlocal in_think
        if not in_think:
            parts.append("<think>")
            in_think = True

    for msg in response.messages:
        mtype = getattr(msg, "message_type", "")
        if mtype == "reasoning_message":
            text = getattr(msg, "reasoning", "") or ""
            if text.strip():
                open_think()
                parts.append(text)
        elif mtype == "tool_call_message":
            tc = getattr(msg, "tool_call", None)
            if tc:
                name = getattr(tc, "name", "") or ""
                args = getattr(tc, "arguments", "") or ""
                pending_tool = (name, args)
        elif mtype == "tool_return_message":
            close_think()
            if pending_tool:
                parts.append("\n" + _pretty_tool(*pending_tool))
                pending_tool = None
            parts.append("   → " + _pretty_return(getattr(msg, "tool_return", "")) + "\n")
        elif mtype == "assistant_message":
            close_think()
            if pending_tool:
                parts.append("\n" + _pretty_tool(*pending_tool) + "\n")
                pending_tool = None
            content = getattr(msg, "content", "")
            if isinstance(content, list):
                content = "".join([getattr(x, "text", "") or str(x) for x in content])
            if content:
                assistant_content = str(content)
    close_think()
    if pending_tool:
        parts.append("\n" + _pretty_tool(*pending_tool) + "\n")

    header = "".join(parts)
    return header + assistant_content


async def non_stream_response(agent_id: str, message: str, model: str):
    response = letta.agents.messages.create(
        agent_id=agent_id, messages=[{"role": "user", "content": message}]
    )
    assistant_content = _extract_letta_response(response)

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


def _pretty_tool(name: str, args_json: str) -> str:
    """把工具调用渲染成人话：已知工具特化展示，未知工具 fallback 名+主参数。"""
    try:
        args = json.loads(args_json) if args_json else {}
    except Exception:
        return f"🔧 {name}"
    if name == "suggest_todo":
        title = args.get("title", "")
        pri = args.get("priority", "")
        mark = {"high": " 🔴", "medium": "", "low": " 🔵"}.get(pri, "")
        return f"📝 建议 TODO:「{title}」{mark}"
    if name == "suggest_project_knowledge":
        return f"📚 提交项目知识建议:{(args.get('content') or '')[:60]}"
    if name in ("memory_insert", "core_memory_append", "memory_replace", "core_memory_replace"):
        label = args.get("label", "") or "记忆"
        content = args.get("content") or args.get("new_str") or ""
        return f"🧠 写入 {label} block:{content[:60]}"
    if name in ("archival_memory_search", "search_memory"):
        return f"🔍 归档搜索:{(args.get('query') or '')[:40]}"
    if name == "conversation_search":
        return f"🔍 对话搜索:{(args.get('query') or '')[:40]}"
    if name in ("open_files", "open_file"):
        fn = args.get("file_name") or args.get("file_path") or args.get("name") or ""
        return f"📄 打开文件:{fn}"
    if name == "grep_files":
        return f"🔎 grep:{(args.get('pattern') or '')[:40]}"
    if name == "semantic_search_files":
        return f"🔍 语义搜索文件:{(args.get('query') or '')[:40]}"
    vals = [v for v in args.values() if v]
    first = (str(vals[0])[:50]) if vals else ""
    return f"🔧 {name}" + (f":{first}" if first else "")


def _pretty_return(ret: str) -> str:
    """tool_return 精简:强制单行 + 截断 + grep/open 等大返回提炼摘要"""
    ret = (ret or "").strip()
    if not ret:
        return "✓ 完成"
    if ret.startswith("{") and ret.endswith("}"):
        try:
            j = json.loads(ret)
            ret = j.get("message") or j.get("status") or str(j)
        except Exception:
            pass
    first_line = ret.split("\n", 1)[0]
    if "matches" in first_line or "找到" in first_line or "showing" in first_line.lower():
        return first_line[:120]
    ret = ret.replace("\n", " ").strip()
    if len(ret) > 120:
        ret = ret[:120] + "…"
    return ret


def _assistant_delta_text(content) -> str:
    """assistant_message.content 可能是 str 或 [{text,...}] 列表，统一取字符串"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            t = getattr(p, "text", None)
            if t is None and isinstance(p, dict):
                t = p.get("text")
            if t:
                parts.append(t)
        return "".join(parts)
    return ""


async def stream_from_letta(agent_id: str, message: str, model: str):
    """真流式：边调 Letta 边转发 token。reasoning 片段用 <think></think> 包裹。"""
    chunk_id = f"chatcmpl-{agent_id[:8]}"

    def _sse(delta: dict, finish_reason=None) -> str:
        payload = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    in_thinking = False
    # tool_call 是流式 delta：name 先来一次，args 分多片。聚合后在 tool_return 或 flush 时整体输出。
    pending_tc = {"name": "", "args": ""}

    def flush_tool_call():
        if not pending_tc["name"] and not pending_tc["args"]:
            return None
        text = _pretty_tool(pending_tc["name"], pending_tc["args"]) + "\n"
        pending_tc["name"] = ""
        pending_tc["args"] = ""
        return text

    try:
        stream = await letta_async.agents.messages.stream(
            agent_id=agent_id,
            messages=[{"role": "user", "content": message}],
            stream_tokens=True,
            include_pings=False,
        )
        async for ev in stream:
            mtype = getattr(ev, "message_type", None)
            if mtype == "reasoning_message":
                text = getattr(ev, "reasoning", "") or ""
                if not text:
                    continue
                if not in_thinking:
                    if not text.strip():
                        continue
                    text = "<think>" + text
                    in_thinking = True
                yield _sse({"content": text})
            elif mtype == "assistant_message":
                text = _assistant_delta_text(getattr(ev, "content", ""))
                if not text:
                    continue
                # 先 flush 任何未结束的 tool_call
                pending = flush_tool_call()
                if pending:
                    prefix = "</think>\n" if in_thinking else ""
                    in_thinking = False
                    yield _sse({"content": prefix + pending})
                if in_thinking:
                    text = "</think>" + text
                    in_thinking = False
                yield _sse({"content": text})
            elif mtype == "tool_call_message":
                tc = getattr(ev, "tool_call", None)
                if not tc:
                    continue
                name = getattr(tc, "name", "") or ""
                args = getattr(tc, "arguments", "") or ""
                if name:
                    pending_tc["name"] = name
                if args:
                    pending_tc["args"] += args
            elif mtype == "tool_return_message":
                ret = _pretty_return(getattr(ev, "tool_return", ""))
                prefix = "</think>\n" if in_thinking else ""
                in_thinking = False
                pending = flush_tool_call()
                combined = (prefix or "") + (pending or "") + f"   → {ret}\n"
                yield _sse({"content": combined})
            elif mtype == "error_message":
                err_type = (getattr(ev, "error_type", "") or "").lower()
                msg = getattr(ev, "message", "") or ""
                if "rate_limit" in err_type or "429" in msg:
                    friendly = "⚠️ AI 模型限流，请稍等几秒重试"
                elif "timeout" in err_type:
                    friendly = "⚠️ AI 响应超时，请重试"
                else:
                    friendly = f"⚠️ {msg[:150] or err_type or '未知错误'}"
                prefix = "</think>\n" if in_thinking else "\n"
                in_thinking = False
                yield _sse({"content": prefix + friendly + "\n"})
            # ping/stop_reason/usage_statistics 等忽略

        # 流结束，flush 残留 tool_call
        pending = flush_tool_call()
        if pending:
            prefix = "</think>\n" if in_thinking else ""
            in_thinking = False
            yield _sse({"content": prefix + pending})
    except Exception as e:
        logging.exception(f"letta stream failed: {e}")
        err_text = ("</think>" if in_thinking else "") + f"\n\n[流式异常：{e}]"
        in_thinking = False
        yield _sse({"content": err_text})

    if in_thinking:
        yield _sse({"content": "</think>"})
    yield _sse({}, finish_reason="stop")
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
            "name": "Nexus Lite",
        },
    ]
    # Letta 项目模型
    for r in rows:
        models.append({
            "id": f"letta-{r['project_id']}",
            "object": "model",
            "owned_by": "ai-infra",
            "name": f"Nexus · {r['name']}",
        })

    return {"object": "list", "data": models}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()

    model = body.get("model", "")

    # qwen-no-mem: 直接透传给 vLLM，不走 Letta
    if model == "qwen-no-mem":
        import httpx

        _internal_keys = {"files", "_letta_files", "user_id", "user_name", "user_email", "user_role", "user"}
        vllm_body = {k: v for k, v in body.items() if k not in _internal_keys}
        vllm_body["model"] = "Qwen3.5-122B-A10B"
        vllm_body["chat_template_kwargs"] = {"enable_thinking": True}

        if body.get("stream", False):
            # 流式：把 vLLM 的 delta.reasoning 转成 <think> 标签包裹的 delta.content
            async def proxy_stream():
                in_thinking = False
                async with httpx.AsyncClient(timeout=300) as client:
                    async with client.stream(
                        "POST",
                        f"{VLLM_ENDPOINT}/chat/completions",
                        json=vllm_body,
                        headers={"Authorization": f"Bearer {VLLM_API_KEY}"},
                    ) as resp:
                        async for line in resp.aiter_lines():
                            if not line or not line.startswith("data: "):
                                continue
                            if line == "data: [DONE]":
                                yield line + "\n\n"
                                continue
                            try:
                                chunk = json.loads(line[6:])
                                delta = chunk["choices"][0].get("delta", {})
                                reasoning = delta.pop("reasoning", None)
                                if reasoning:
                                    if not in_thinking:
                                        reasoning = "<think>" + reasoning
                                        in_thinking = True
                                    delta["content"] = reasoning
                                elif in_thinking and delta.get("content") is not None:
                                    # reasoning 结束，content 开始
                                    delta["content"] = "</think>" + (delta["content"] or "")
                                    in_thinking = False
                                chunk["choices"][0]["delta"] = delta
                                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                            except (json.JSONDecodeError, KeyError, IndexError):
                                yield line + "\n\n"
            return StreamingResponse(proxy_stream(), media_type="text/event-stream")
        else:
            async with httpx.AsyncClient(timeout=300) as client:
                resp = await client.post(
                    f"{VLLM_ENDPOINT}/chat/completions",
                    json=vllm_body,
                    headers={"Authorization": f"Bearer {VLLM_API_KEY}"},
                )
                data = resp.json()
                # 非流式：把 reasoning 转成 <think> 标签拼入 content
                try:
                    msg = data["choices"][0]["message"]
                    reasoning = msg.pop("reasoning", None)
                    if reasoning:
                        msg["content"] = f"<think>{reasoning}</think>{msg.get('content') or ''}"
                except (KeyError, IndexError):
                    pass
                return data

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

    # 流式：调 Letta 的 streaming API 边收边转发
    if body.get("stream", False):
        return StreamingResponse(
            stream_from_letta(agent_id, user_message, model),
            media_type="text/event-stream",
        )
    else:
        return await non_stream_response(agent_id, user_message, model)


# ===== 挂载管理 API =====

from admin_api import router as admin_router
app.include_router(admin_router)
