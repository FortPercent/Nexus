"""适配层主入口 —— 聊天 API（/v1/*）"""
import fcntl
import json
import asyncio
import logging
import os
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
from fastapi import FastAPI, Request, HTTPException
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


# 多 worker 下的 singleton leader 选举 —— 只让一个 worker 跑对账/资源初始化
# fcntl.flock 是进程级锁，进程退出自动释放；首个 worker 拿到锁成为 leader
_SINGLETON_LOCK_PATH = "/tmp/adapter_singleton.lock"
_singleton_lock_fd = None


def _try_become_singleton_leader() -> bool:
    global _singleton_lock_fd
    try:
        fd = os.open(_SINGLETON_LOCK_PATH, os.O_CREAT | os.O_WRONLY, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _singleton_lock_fd = fd  # 持有到进程退出
        os.write(fd, f"{os.getpid()}\n".encode())
        return True
    except (OSError, BlockingIOError):
        return False


@app.on_event("startup")
def startup():
    init_db()  # 幂等，每个 worker 都跑

    if not _try_become_singleton_leader():
        logging.info(f"worker pid={os.getpid()} NOT singleton leader, skipping reconcile")
        return

    logging.info(f"worker pid={os.getpid()} IS singleton leader, running startup singletons")
    # 清 libreoffice 残留 (上次转换崩溃 / kill -9 留的 /tmp/lo_* + zombie soffice)
    try:
        from file_processor import _cleanup_stale_lo_tempdirs
        _cleanup_stale_lo_tempdirs()
    except Exception as e:
        logging.warning(f"startup lo cleanup failed: {e}")
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
    _iter = 0
    while True:
        await asyncio.sleep(300)
        _iter += 1
        # 每 12 次循环 = 1 小时做一次 libreoffice 残留清理
        if _iter % 12 == 0:
            try:
                from file_processor import _cleanup_stale_lo_tempdirs
                _cleanup_stale_lo_tempdirs()
            except Exception as e:
                logging.warning(f"periodic lo cleanup failed: {e}")
        try:
            reconcile_all()
        except Exception as e:
            logging.error(f"periodic reconcile failed: {e}")
        try:
            reconcile_mirrors()
        except Exception as e:
            logging.error(f"periodic mirror reconcile failed: {e}")
        try:
            from letta_sql_tools import reconcile_sql_tool_attachments
            stats = reconcile_sql_tool_attachments()
            logging.info(f"sql tools reconcile: {stats}")
        except Exception as e:
            logging.error(f"periodic sql tools reconcile failed: {e}")
        # 每 48 次循环 = 4h 扫一次孤儿 Letta agent (rebuild 失败/rename 残留)
        if _iter % 48 == 0:
            try:
                from scripts.reconcile_orphan_agents import reconcile_orphans
                stats = await asyncio.to_thread(reconcile_orphans, False, 1.0)
                logging.info(f"orphan agents reconcile: {stats}")
            except Exception as e:
                logging.error(f"periodic orphan reconcile failed: {e}")


@app.on_event("startup")
async def start_reconcile_loop():
    # 非 leader 不启动循环任务
    if _singleton_lock_fd is None:
        return
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


async def non_stream_response(agent_id: str, message: str, model: str, notice_prefix: str | None = None):
    response = letta.agents.messages.create(
        agent_id=agent_id, messages=[{"role": "user", "content": message}]
    )
    assistant_content = _extract_letta_response(response)
    if notice_prefix:
        assistant_content = f"{notice_prefix}\n\n{assistant_content}"

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


async def stream_from_letta(agent_id: str, message: str, model: str, notice_prefix: str | None = None):
    """真流式：边调 Letta 边转发 token。reasoning 片段用 <think></think> 包裹。

    notice_prefix: 若非空 (比如 preflight rebuild 后的 '对话已重置' 提示),
    作为首个 SSE content chunk 发出, 再开始 Letta stream."""
    chunk_id = f"chatcmpl-{agent_id[:8]}"

    def _sse(delta: dict, finish_reason=None) -> str:
        payload = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    if notice_prefix:
        yield _sse({"role": "assistant", "content": notice_prefix + "\n\n"})

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
            elif mtype == "stop_reason":
                # 非 end_turn 的终止原因通常意味着 agent loop 异常结束（context 爆、工具错、
                # LLM 拒答等）。不提示用户就会看到"流静默结束没下文"。
                sr = (getattr(ev, "stop_reason", "") or "").lower()
                if sr and sr not in ("end_turn", "tool_continue"):
                    friendly_map = {
                        "error": "⚠️ 对话因内部错误提前结束（常见原因：上下文过长 / 工具异常）。试着换一下问法，或清空对话重开。",
                        "max_steps": "⚠️ 已达到单轮对话的工具调用上限，回答可能不完整。",
                        "invalid_llm_response": "⚠️ AI 返回了无法解析的响应，请重试。",
                        "invalid_tool_call": "⚠️ AI 生成的工具调用参数不合法，请换个问法重试。",
                        "no_tool_call": "⚠️ AI 未调用任何工具直接结束（可能未理解意图）。",
                    }
                    friendly = friendly_map.get(sr, f"⚠️ 对话异常结束 (stop_reason={sr})")
                    prefix = "</think>\n" if in_thinking else "\n"
                    in_thinking = False
                    yield _sse({"content": prefix + friendly + "\n"})
            # ping/usage_statistics 等继续忽略

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
                        # P1-A (2026-04-20 审查补丁): 上游非 200 必须显式透传错误,
                        # 不要只读 data: 行导致客户端看到空流.
                        if resp.status_code != 200:
                            err_body = await resp.aread()
                            err_text = err_body.decode("utf-8", errors="replace")[:500]
                            logging.warning(f"vLLM stream upstream {resp.status_code}: {err_text}")
                            # 发一个标准 SSE error chunk + DONE 让客户端收到明确信号
                            err_payload = {
                                "error": {
                                    "message": f"upstream vLLM returned {resp.status_code}: {err_text[:200]}",
                                    "type": "upstream_error",
                                    "code": resp.status_code,
                                }
                            }
                            yield f"data: {json.dumps(err_payload, ensure_ascii=False)}\n\n"
                            yield "data: [DONE]\n\n"
                            return
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
                # P1-A (2026-04-20 审查补丁): 上游非 200 必须显式 raise,
                # 不要伪装成 adapter 的 200 把错误藏掉.
                if resp.status_code != 200:
                    err_text = resp.text[:500]
                    logging.warning(f"vLLM non-stream upstream {resp.status_code}: {err_text}")
                    raise HTTPException(
                        resp.status_code,
                        f"upstream vLLM returned {resp.status_code}: {err_text[:200]}",
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
    user = await get_current_user(request, body)
    project = model.replace("letta-", "") if model.startswith("letta-") else "default"
    agent_id = get_or_create_agent(user["id"], project)

    user_message = None
    for msg in reversed(body.get("messages", [])):
        if msg["role"] == "user":
            user_message = msg["content"]
            break

    # 拦截 # 引用的 Letta 镜像文件
    # Open WebUI 会 pop 掉 files 做自己的 RAG，Pipeline Filter 提前备份到 _letta_files
    # Phase 1 优先走盘上直读 (read_project_file 同款路径), 失败才 fallback 到老 passages.search
    ref_files = body.get("_letta_files", []) or body.get("files", [])
    if ref_files:
        logging.info(f"# ref: {json.dumps(ref_files, ensure_ascii=False, default=str)}")
    if ref_files and user_message:
        from knowledge_mirror import get_letta_file_id_by_knowledge
        KB_ROOT = "/data/serving/adapter/projects"
        MAX_REF_CHARS = 8000
        ref_context_parts = []
        for rf in ref_files:
            kid = rf.get("id") or rf.get("collection_name") or ""
            if not kid:
                continue
            mirror = get_letta_file_id_by_knowledge(kid)
            if not mirror:
                continue
            file_name = mirror.get("display_name") or rf.get("name", kid)
            scope = mirror.get("scope", "project")
            scope_id = mirror.get("scope_id", "") or ""

            # Phase 1 新路径: 从盘上读文件 (不依赖 Letta folder / passages)
            kb_content = None
            try:
                if scope == "project":
                    base = os.path.join(KB_ROOT, scope_id)
                elif scope == "personal":
                    base = os.path.join(KB_ROOT, ".personal", mirror.get("owner_id") or scope_id)
                elif scope == "org":
                    base = os.path.join(KB_ROOT, ".org")
                else:
                    base = None
                if base:
                    legacy_dir = os.path.join(base, ".legacy")
                    # file_name 是 display name, 盘上多半带 .md 后缀
                    candidates = [file_name]
                    if not file_name.endswith(".md"):
                        candidates.append(file_name + ".md")
                    for cand in candidates:
                        full_path = os.path.join(legacy_dir, cand)
                        if os.path.isfile(full_path):
                            with open(full_path, encoding="utf-8", errors="replace") as rfp:
                                content = rfp.read()
                            if len(content) > MAX_REF_CHARS:
                                kb_content = content[:MAX_REF_CHARS] + (
                                    f"\n...(原文共 {len(content)} 字, 已截前 {MAX_REF_CHARS} 字, "
                                    f"如需后文可调 read_project_file(file_name=\"{cand}\", offset={MAX_REF_CHARS}))"
                                )
                            else:
                                kb_content = content
                            break
            except Exception as e:
                logging.warning(f"# ref kb read fallback: {e}")

            if kb_content:
                ref_context_parts.append(f"=== 引用文档：{file_name} ===\n{kb_content}")
                continue

            # 老 fallback: passages.search (folder 已 detach 时大概率返空, 但对未 backfill 的文件仍可能有效)
            letta_file_id = mirror["letta_file_id"]
            try:
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

            # 两条路都失败：引导 agent 用 kb 工具
            ref_context_parts.append(
                f"[用户引用了文档「{file_name}」, 盘上和 passages 都没找到, "
                f"请用 list_project_files 看看有没有名字近的文件再 read_project_file]"
            )
        if ref_context_parts:
            context = "\n".join(ref_context_parts)
            user_message = f"{context}\n\n{user_message}"

    if not user_message:
        return {"error": "No user message found"}

    # Pre-flight compact: 在调 Letta 前预检上下文占用, 必要时 summarize 或 rebuild.
    # 见 docs/compact-preflight-v1-spec.md. user_message 此时已经展开 # 引用/附件,
    # 是最终发给 Letta 的 content.
    # v1.1: 不吞异常 — 明确失败 > 表面成功但会话裂开.
    from preflight import preflight_compact, resolve_current_agent
    from fastapi import HTTPException
    notice_prefix = None
    try:
        pf = await preflight_compact(user["id"], project, user_message)
        if pf.rebuilt:
            logging.info(
                f"[preflight] chat {user['id'][:8]}/{project}: rebuilt "
                f"{agent_id[-12:]} → {pf.agent_id[-12:]} ({pf.ctx_before} → 0)"
            )
            agent_id = pf.agent_id
            notice_prefix = pf.user_msg
        elif pf.action == "sync_summarized":
            logging.info(
                f"[preflight] chat {user['id'][:8]}/{project}: summarized "
                f"{agent_id[-12:]} ({pf.ctx_before} → {pf.ctx_after})"
            )
        else:
            agent_id = pf.agent_id
    except Exception as e:
        logging.error(f"[preflight] chat {user['id'][:8]}/{project} failed: {e}")
        raise HTTPException(
            status_code=503,
            detail=f"对话预处理失败 ({type(e).__name__}), 请稍后重试",
        )

    # v1.1.1 race 修补: forward 前再 re-read map, 防 fast-path 判 safe 后
    # 别 worker 立刻 rebuild 把 map 切走 — 仍往旧 agent 发消息导致会话分叉.
    # 10ms 残余窗口由旧 agent 延迟删除兜住.
    agent_id = await resolve_current_agent(user["id"], project, agent_id)

    # 流式：调 Letta 的 streaming API 边收边转发
    if body.get("stream", False):
        return StreamingResponse(
            stream_from_letta(agent_id, user_message, model, notice_prefix=notice_prefix),
            media_type="text/event-stream",
        )
    else:
        return await non_stream_response(agent_id, user_message, model, notice_prefix=notice_prefix)


# ===== 挂载管理 API =====

from admin_api import router as admin_router
app.include_router(admin_router)

from sql_endpoints import router as sql_router
app.include_router(sql_router)

from responses_endpoints import router as responses_router
app.include_router(responses_router)

from kb.endpoints import router as kb_router
app.include_router(kb_router)
