"""Responses API (OpenAI v2) 适配层。

Open WebUI 的 Experimental Responses connection type 会发:
  POST /v2/responses   Responses API schema
  GET  /v2/models      列模型

支持的模型:
  - letta-* : 走 Letta agent (有记忆, 带工具), 翻译 Letta stream → Responses 事件
  - qwen-no-mem: 走 vLLM 直连, 翻译 Chat Completions stream → Responses 事件

请求字段支持面 (MVP):
  ✓ model, input (string 或 structured message array), stream, user_id/email/name
  ✗ instructions: 显式 400 (Letta 通过 persona block 管理 system prompt)
  ✗ tools:        显式 400 (Letta 通过 agent.tools.attach 管理)
  ✗ previous_response_id: 显式 400 (Letta 自带对话历史, 不需要 response chaining)
  ✗ 非 input_text 的 content (input_image 等): 显式 400 (多模态是未来工作)

事件翻译规则:
  reasoning_message   → reasoning item + response.reasoning_text.delta/done
  assistant_message   → message item + response.output_text.delta/done
  tool_call + return  → 展示成 message item 里的 output_text (MVP, 不做 function_call item)
  stop_reason!=end_turn → 友好提示塞进 output_text

收尾发 response.completed 带完整 output 数组和 usage。

鉴权顺序 (P3 审查补丁): API key → model 校验 → 未支持字段 → input 解析 → 业务逻辑
"""
from __future__ import annotations

import json
import logging
import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse

from config import ADAPTER_API_KEY
from auth import get_current_user
from routing import get_or_create_agent, letta_async

router = APIRouter(prefix="/v2", tags=["responses-api"])


def _event(etype: str, **kw) -> str:
    """Responses API SSE 事件。"""
    payload = {"type": etype, **kw}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _extract_user_message(input_items) -> str:
    """Responses API 的 input 既可以是字符串简单形式，也可以是 structured array:
       str:  "帮我写份通知"
       list: [{type:'message', role:'user', content:[{type:'input_text', text:...}]}, ...]
    返回最后一个 user message 的拼接文本。Letta 自带记忆, 不需要往前拼历史。

    P2.2 (2026-04-20 审查补丁): 非文本 content type (input_image 等) 显式 400 拒绝,
    不再 silently 丢. MVP 只支持文本; 多模态透传是未来工作.
    """
    if isinstance(input_items, str):
        return input_items
    if not isinstance(input_items, list):
        return ""
    last_user = None
    for item in input_items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message" and item.get("role") == "user":
            last_user = item
    if not last_user:
        return ""
    content = last_user.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if not isinstance(p, dict):
                continue
            ptype = p.get("type", "")
            if ptype in ("input_text", "text"):
                parts.append(p.get("text", ""))
            else:
                # P2.2: 未支持的 content 类型 (input_image / file_url 等) 显式拒绝
                raise HTTPException(
                    400,
                    f"unsupported input content type '{ptype}' (MVP only accepts input_text; "
                    f"multimodal passthrough not implemented)"
                )
        return "".join(parts)
    return str(content)


def _pretty_tool_line(name: str, args: str) -> str:
    """同 main.py::_pretty_tool 的简化版; 工具调用显示成一行文字给用户看。"""
    name = name or "?"
    try:
        parsed = json.loads(args) if args else {}
        if parsed:
            kvs = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)[:60]}" for k, v in parsed.items())
            return f"🔧 {name}({kvs})"
    except Exception:
        pass
    return f"🔧 {name}({(args or '')[:80]})"


def _pretty_ret_line(ret: str) -> str:
    if not ret:
        return ""
    # 尝试解 JSON 取主字段
    try:
        obj = json.loads(ret)
        if isinstance(obj, dict):
            for k in ("message", "text", "result", "status"):
                if k in obj:
                    return f"   → {str(obj[k])[:200]}"
            return f"   → {json.dumps(obj, ensure_ascii=False)[:200]}"
    except Exception:
        pass
    return f"   → {str(ret)[:200]}"


STOP_REASON_FRIENDLY = {
    "error": "⚠️ 对话因内部错误提前结束（常见原因：上下文过长 / 工具异常）。试着换一下问法，或清空对话重开。",
    "max_steps": "⚠️ 已达到单轮对话的工具调用上限，回答可能不完整。",
    "invalid_llm_response": "⚠️ AI 返回了无法解析的响应，请重试。",
    "invalid_tool_call": "⚠️ AI 生成的工具调用参数不合法，请换个问法重试。",
    "no_tool_call": "⚠️ AI 未调用任何工具直接结束（可能未理解意图）。",
}


async def stream_letta_as_responses(agent_id: str, user_msg: str, model: str):
    """Letta 流 → Responses API SSE 流。"""
    resp_id = f"resp_{uuid.uuid4().hex[:24]}"
    created_at = int(time.time())

    # 1. response.created + in_progress
    yield _event("response.created", response={
        "id": resp_id, "object": "response", "model": model,
        "created_at": created_at, "status": "in_progress",
        "output": [], "usage": None,
    })
    yield _event("response.in_progress", response={"id": resp_id})

    # 状态机: open_item 标识当前 output_item 是什么类型 (None / "reasoning" / "message")
    output_items: list[dict] = []  # 累积 final output (给 response.completed 用)
    open_type: str | None = None
    open_idx: int = 0
    open_text: str = ""
    # tool_call 累积: name 一次给, args 分片
    pending_tc = {"name": "", "args": ""}
    prompt_tokens = 0
    completion_tokens = 0

    async def close_open():
        """关闭当前打开的 output_item (reasoning / message), 发 done 事件, 累加 output_items。"""
        nonlocal open_type, open_text
        if open_type is None:
            return
        events = []
        if open_type == "message":
            part = {"type": "output_text", "text": open_text}
            events.append(_event("response.output_text.done",
                                 output_index=open_idx, content_index=0, text=open_text))
            events.append(_event("response.content_part.done",
                                 output_index=open_idx, content_index=0, part=part))
            item = {"type": "message", "role": "assistant", "content": [part], "status": "completed"}
            output_items.append(item)
            events.append(_event("response.output_item.done", output_index=open_idx, item=item))
        elif open_type == "reasoning":
            events.append(_event("response.reasoning_text.done",
                                 output_index=open_idx, content_index=0, text=open_text))
            item = {"type": "reasoning", "status": "completed",
                    "content": [{"type": "reasoning_text", "text": open_text}]}
            output_items.append(item)
            events.append(_event("response.output_item.done", output_index=open_idx, item=item))
        open_type = None
        open_text = ""
        return events

    async def open_message_item():
        """打开一个新的 message output_item, 发 added 事件。"""
        nonlocal open_type, open_text, open_idx
        close_events = await close_open() or []
        open_idx = len(output_items)
        open_type = "message"
        open_text = ""
        item = {"type": "message", "role": "assistant", "content": [], "status": "in_progress"}
        part = {"type": "output_text", "text": ""}
        return close_events + [
            _event("response.output_item.added", output_index=open_idx, item=item),
            _event("response.content_part.added", output_index=open_idx, content_index=0, part=part),
        ]

    async def open_reasoning_item():
        nonlocal open_type, open_text, open_idx
        close_events = await close_open() or []
        open_idx = len(output_items)
        open_type = "reasoning"
        open_text = ""
        item = {"type": "reasoning", "status": "in_progress", "content": []}
        return close_events + [
            _event("response.output_item.added", output_index=open_idx, item=item),
        ]

    def flush_tool_call_line() -> str:
        """把累积的 tool_call name+args 组成一行展示文本, 清空累积。"""
        line = ""
        if pending_tc["name"] or pending_tc["args"]:
            line = _pretty_tool_line(pending_tc["name"], pending_tc["args"]) + "\n"
            pending_tc["name"] = ""
            pending_tc["args"] = ""
        return line

    async def emit_text_delta(text: str):
        """往当前 message item 追加 output_text delta。若没 open message item, 先 open。"""
        nonlocal open_text
        events = []
        if open_type != "message":
            events.extend(await open_message_item())
        open_text += text
        events.append(_event("response.output_text.delta",
                             output_index=open_idx, content_index=0, delta=text))
        return events

    try:
        stream = await letta_async.agents.messages.stream(
            agent_id=agent_id,
            messages=[{"role": "user", "content": user_msg}],
            stream_tokens=True,
            include_pings=False,
        )
        async for ev in stream:
            mtype = getattr(ev, "message_type", None) or type(ev).__name__

            if mtype == "reasoning_message":
                text = getattr(ev, "reasoning", "") or ""
                if not text:
                    continue
                if open_type != "reasoning":
                    for e in (await open_reasoning_item()):
                        yield e
                open_text += text
                yield _event("response.reasoning_text.delta",
                             output_index=open_idx, content_index=0, delta=text)

            elif mtype == "assistant_message":
                raw = getattr(ev, "content", "") or ""
                if isinstance(raw, list):
                    text = "".join(getattr(p, "text", "") for p in raw if hasattr(p, "text"))
                else:
                    text = str(raw)
                if not text:
                    continue
                # 工具调用累积中? 先 flush 成文本行插入 message
                tc_line = flush_tool_call_line()
                if tc_line:
                    for e in (await emit_text_delta(tc_line)):
                        yield e
                for e in (await emit_text_delta(text)):
                    yield e

            elif mtype == "tool_call_message":
                tc = getattr(ev, "tool_call", None)
                if tc:
                    nm = getattr(tc, "name", "") or ""
                    ag = getattr(tc, "arguments", "") or ""
                    if nm:
                        pending_tc["name"] = nm
                    if ag:
                        pending_tc["args"] += ag

            elif mtype == "tool_return_message":
                ret = getattr(ev, "tool_return", "") or ""
                tc_line = flush_tool_call_line()
                ret_line = _pretty_ret_line(ret) + "\n"
                combined = tc_line + ret_line
                if combined:
                    for e in (await emit_text_delta(combined)):
                        yield e

            elif mtype == "error_message":
                emsg = getattr(ev, "message", "") or ""
                etype = (getattr(ev, "error_type", "") or "").lower()
                if "rate_limit" in etype or "429" in emsg:
                    friendly = "⚠️ AI 模型限流，请稍等几秒重试"
                elif "timeout" in etype:
                    friendly = "⚠️ AI 响应超时，请重试"
                else:
                    friendly = f"⚠️ {emsg[:150] or etype or '未知错误'}"
                for e in (await emit_text_delta("\n" + friendly + "\n")):
                    yield e

            elif mtype == "stop_reason":
                sr = (getattr(ev, "stop_reason", "") or "").lower()
                if sr and sr not in ("end_turn", "tool_continue"):
                    friendly = STOP_REASON_FRIENDLY.get(sr, f"⚠️ 对话异常结束 (stop_reason={sr})")
                    for e in (await emit_text_delta("\n" + friendly + "\n")):
                        yield e

            elif mtype == "usage_statistics":
                prompt_tokens = getattr(ev, "prompt_tokens", 0) or 0
                completion_tokens = getattr(ev, "completion_tokens", 0) or 0

        # flush 最后的 tool_call 残留
        tc_line = flush_tool_call_line()
        if tc_line:
            for e in (await emit_text_delta(tc_line)):
                yield e

        # 关闭最后一个 open item
        for e in (await close_open() or []):
            yield e

    except Exception as e:
        logging.exception(f"responses stream failed: {e}")
        # 发一个 message item 装错误
        if open_type != "message":
            for ev2 in (await open_message_item()):
                yield ev2
        err_text = f"\n[流式异常: {e}]"
        open_text += err_text
        yield _event("response.output_text.delta",
                     output_index=open_idx, content_index=0, delta=err_text)
        for ev2 in (await close_open() or []):
            yield ev2

    # 最终 response.completed
    final = {
        "id": resp_id, "object": "response", "model": model,
        "created_at": created_at, "status": "completed",
        "output": output_items,
        "usage": {
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
    yield _event("response.completed", response=final)


async def stream_vllm_as_responses(user_msg: str, model: str, enable_thinking: bool = False):
    """qwen-no-mem 路径: vLLM Chat Completions stream → Responses API events.

    vLLM stream 返 OpenAI Chat Completions delta chunks:
      delta.reasoning   → response.reasoning_text.delta (reasoning item)
      delta.content     → response.output_text.delta (message item)
    结束时发 response.completed.

    enable_thinking: 默认 False (简单问题直接答, 节省 tokens). True 时 Qwen3.5 走 reasoner
    模式, 复杂任务质量更好但 "你好" 这种琐碎输入会 overthink (实测 936 reasoning vs 12 output).
    """
    import httpx
    from config import VLLM_ENDPOINT, VLLM_API_KEY

    resp_id = f"resp_{uuid.uuid4().hex[:24]}"
    created_at = int(time.time())

    yield _event("response.created", response={
        "id": resp_id, "object": "response", "model": model,
        "created_at": created_at, "status": "in_progress",
        "output": [], "usage": None,
    })
    yield _event("response.in_progress", response={"id": resp_id})

    output_items: list[dict] = []
    open_type: str | None = None  # "reasoning" or "message"
    open_idx: int = 0
    open_text: str = ""
    prompt_tokens = 0
    completion_tokens = 0

    def close_open_events():
        """返回 close 当前 item 需要 yield 的事件列表 + 更新 output_items。"""
        nonlocal open_type, open_text
        evs = []
        if open_type == "message":
            part = {"type": "output_text", "text": open_text}
            evs.append(_event("response.output_text.done",
                              output_index=open_idx, content_index=0, text=open_text))
            evs.append(_event("response.content_part.done",
                              output_index=open_idx, content_index=0, part=part))
            item = {"type": "message", "role": "assistant", "content": [part], "status": "completed"}
            output_items.append(item)
            evs.append(_event("response.output_item.done", output_index=open_idx, item=item))
        elif open_type == "reasoning":
            evs.append(_event("response.reasoning_text.done",
                              output_index=open_idx, content_index=0, text=open_text))
            item = {"type": "reasoning", "status": "completed",
                    "content": [{"type": "reasoning_text", "text": open_text}]}
            output_items.append(item)
            evs.append(_event("response.output_item.done", output_index=open_idx, item=item))
        open_type = None
        open_text = ""
        return evs

    def open_message_events():
        nonlocal open_type, open_text, open_idx
        evs = close_open_events()
        open_idx = len(output_items)
        open_type = "message"
        open_text = ""
        item = {"type": "message", "role": "assistant", "content": [], "status": "in_progress"}
        part = {"type": "output_text", "text": ""}
        evs.append(_event("response.output_item.added", output_index=open_idx, item=item))
        evs.append(_event("response.content_part.added",
                          output_index=open_idx, content_index=0, part=part))
        return evs

    def open_reasoning_events():
        nonlocal open_type, open_text, open_idx
        evs = close_open_events()
        open_idx = len(output_items)
        open_type = "reasoning"
        open_text = ""
        item = {"type": "reasoning", "status": "in_progress", "content": []}
        evs.append(_event("response.output_item.added", output_index=open_idx, item=item))
        return evs

    vllm_body = {
        "model": "Qwen3.5-122B-A10B",
        "messages": [{"role": "user", "content": user_msg}],
        "stream": True,
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": enable_thinking},
    }

    try:
        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream(
                "POST", f"{VLLM_ENDPOINT}/chat/completions",
                json=vllm_body,
                headers={"Authorization": f"Bearer {VLLM_API_KEY}"},
            ) as r:
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    if line == "data: [DONE]":
                        break
                    try:
                        chunk = json.loads(line[6:])
                    except Exception:
                        continue
                    # usage (最后一包)
                    if chunk.get("usage"):
                        prompt_tokens = chunk["usage"].get("prompt_tokens", 0) or 0
                        completion_tokens = chunk["usage"].get("completion_tokens", 0) or 0
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {}) or {}
                    reasoning = delta.get("reasoning")
                    content = delta.get("content")
                    if reasoning:
                        if open_type != "reasoning":
                            for e in open_reasoning_events():
                                yield e
                        open_text += reasoning
                        yield _event("response.reasoning_text.delta",
                                     output_index=open_idx, content_index=0, delta=reasoning)
                    if content:
                        if open_type != "message":
                            for e in open_message_events():
                                yield e
                        open_text += content
                        yield _event("response.output_text.delta",
                                     output_index=open_idx, content_index=0, delta=content)
        for e in close_open_events():
            yield e
    except Exception as e:
        logging.exception(f"vllm stream failed: {e}")
        if open_type != "message":
            for e2 in open_message_events():
                yield e2
        err_text = f"\n[vLLM 流式异常: {e}]"
        open_text += err_text
        yield _event("response.output_text.delta",
                     output_index=open_idx, content_index=0, delta=err_text)
        for e2 in close_open_events():
            yield e2

    yield _event("response.completed", response={
        "id": resp_id, "object": "response", "model": model,
        "created_at": created_at, "status": "completed",
        "output": output_items,
        "usage": {
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    })


@router.get("/models")
async def v2_models(request: Request):
    """复用 /v1/models 的响应 (同一个 letta-* 列表)。"""
    # 鉴权: Open WebUI 侧传 Bearer ADAPTER_API_KEY
    from main import list_models  # 避免循环 import
    return await list_models(request)


@router.post("/responses")
async def v2_responses(request: Request):
    body = await request.json()

    # P3.1 (2026-04-20 审查补丁): API key 鉴权必须前置于任何业务/shape 校验,
    # 否则未鉴权方能通过 400 回包嗅出能力边界.
    # 注意: 不在这里调 get_current_user() —— 它会顺便 require body.user_id,
    # 但 qwen-no-mem 不走 Letta 不需要 user identity. API key 先单独验,
    # user identity 延后到 letta-* 分支再验.
    _tok = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if _tok != ADAPTER_API_KEY:
        raise HTTPException(401, "Invalid API key")

    model = body.get("model", "")

    # MVP 支持范围: letta-* (走 Letta agent) + qwen-no-mem (走 vLLM 直连)
    if not (model.startswith("letta-") or model == "qwen-no-mem"):
        raise HTTPException(400, f"/v2/responses unsupported model '{model}'; supported: letta-*, qwen-no-mem")

    # P2.1: 显式拒绝 MVP 未实现的 Responses API 字段 (不能 silently ignore).
    if body.get("instructions"):
        raise HTTPException(
            400,
            "field 'instructions' not supported: Letta manages system prompts via persona block. "
            "Use /admin/api endpoints to update persona."
        )
    if body.get("tools"):
        raise HTTPException(
            400,
            "field 'tools' not supported: Letta manages tool bindings via agent.tools.attach. "
            "Use /admin/api endpoints or Letta SDK directly."
        )
    if body.get("previous_response_id"):
        raise HTTPException(
            400,
            "field 'previous_response_id' not supported: Letta maintains its own conversation state "
            "per agent; multi-turn history is automatic and does not need response chaining."
        )

    user_msg = _extract_user_message(body.get("input", []))
    if not user_msg.strip():
        raise HTTPException(400, "empty input: cannot extract user message text")

    stream_flag = bool(body.get("stream", False))

    # qwen-no-mem: 绕过 Letta, 直连 vLLM
    if model == "qwen-no-mem":
        # 客户端可显式控制 thinking (默认关 = 节省 tokens, 简单问题秒答):
        #   {"enable_thinking": true}              → 开
        #   {"reasoning": {"effort": "high"}}      → 开 (兼容 OpenAI spec)
        #   {"reasoning": {"effort": "low"}}       → 关
        thinking = False
        if "enable_thinking" in body:
            thinking = bool(body["enable_thinking"])
        elif isinstance(body.get("reasoning"), dict):
            effort = (body["reasoning"].get("effort") or "").lower()
            if effort in ("medium", "high"):
                thinking = True

        if stream_flag:
            return StreamingResponse(
                stream_vllm_as_responses(user_msg, model, enable_thinking=thinking),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        # 非流式: 消费完流打包最终 response
        async for chunk in stream_vllm_as_responses(user_msg, model, enable_thinking=thinking):
            try:
                payload = json.loads(chunk[len("data: "):].strip())
                if payload.get("type") == "response.completed":
                    final = payload.get("response", {})
                    return JSONResponse(final)
            except Exception:
                continue
        raise HTTPException(500, "no completion event from vllm stream")

    # letta-*: 走有记忆的 agent, 此时才真正需要 user identity
    user = await get_current_user(request, body)
    project = model.replace("letta-", "", 1)
    agent_id = get_or_create_agent(user["id"], project)

    if stream_flag:
        return StreamingResponse(
            stream_letta_as_responses(agent_id, user_msg, model),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    last_event = None
    async for chunk in stream_letta_as_responses(agent_id, user_msg, model):
        try:
            payload = json.loads(chunk[len("data: "):].strip())
            if payload.get("type") == "response.completed":
                last_event = payload.get("response", {})
        except Exception:
            continue
    if not last_event:
        raise HTTPException(500, "no completion event from letta stream")
    return JSONResponse(last_event)
