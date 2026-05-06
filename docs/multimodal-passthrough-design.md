# 多模态图片透传设计 v3

> 编写: 2026-05-05  状态: spike-verified, ready to implement  作者: Claude
>
> ## 版本演进
> - v1 错: 以为要删 file_processor IMAGE_EXTS。grep 后发现 chat path 完全不走 file_processor。
> - v2 错: 以为 Letta SDK 用 OpenAI 标准 image_url。spike 实测 422 拒收, Letta 用 Anthropic 风格 schema。
> - **v3 (本版) 实证:** Letta 接受 Anthropic schema (`type=image, source.base64/url/letta`), 内部转 OpenAI image_url 给 LLM provider, 端到端通。工时下修到 **1.5d**.

---

## Spike 实证 (2026-05-05)

`adapter/spike-multimodal/` docker-compose 起 pg + letta + mock-vllm, mock-vllm 在 proxy 模式下转发到 DashScope qwen3.6-plus.

**完整链路 (实测):**

```
client (run_spike.py)
   ↓ POST /v1/agents/{aid}/messages
   ↓ content = [{type:"text",text:"图里写了什么字？"},
   ↓            {type:"image",
   ↓             source:{type:"base64",media_type:"image/png",data:"<b64>"}}]
Letta SDK 接受 (无 422)
   ↓ Letta 内部转换
   ↓ POST /v1/chat/completions to OPENAI_API_BASE
   ↓ content = [{type:"text",text:"..."},
   ↓            {type:"image_url",image_url:{url:"data:image/png;base64,<b64>"}}]
mock-vllm dump 收到的 body (✅ list 仍保留, image_url 保留, base64 1598 字节完整)
   ↓ proxy 到 DashScope
DashScope qwen3.6-plus
   ↓ usage.image_tokens=66, completion="图片里写的字是: AI Infra spike"
回到 Letta agent → 客户端
```

**spike 同时拿到的副产品观察:**
- Letta 默认 system prompt + memory blocks 占 ~2456 文本 token (qwen3.6-plus 用例)
- letta server 响应 multimodal 请求时会先调 `/v1/embeddings` (做 archival memory 索引), 再调 `/v1/chat/completions`. mock 时两个端点都要实现.

---

## 真正要做的事 (1.5 工日)

不需要: ❌ patch Letta / ❌ 多模态绕 qwen-no-mem / ❌ OCR / ❌ vision embedding.

需要: ✅ adapter 协议转换 (OpenAI ↔ Letta schema) + ✅ WebUI 路径分流.

---

### Layer 1: adapter 协议转换 (0.5 工日)

OpenAI 标准 chat completion 协议传进来 (WebUI / OpenAI 兼容客户端), adapter 把 user message 里的 `image_url` 段转成 Letta 风格 `image+source` schema 再调 letta SDK.

#### 关键改动 1: `main.py:563-567` user_message 取值

```python
# 现状 (假设 content 是 str, list 进来直接 line 698 TypeError)
user_message = None
for msg in reversed(body.get("messages", [])):
    if msg["role"] == "user":
        user_message = msg["content"]
        break

# v3 改造
user_text = ""
user_images = []  # Letta-native image content parts
for msg in reversed(body.get("messages", [])):
    if msg["role"] == "user":
        c = msg["content"]
        if isinstance(c, str):
            user_text = c
        elif isinstance(c, list):
            for part in c:
                if part.get("type") == "text":
                    user_text += part.get("text", "")
                elif part.get("type") == "image_url":
                    user_images.append(_convert_openai_to_letta_image(part))
        break
```

#### 关键改动 2: `_convert_openai_to_letta_image` helper

```python
def _convert_openai_to_letta_image(openai_part: dict) -> dict:
    """OpenAI image_url → Letta Anthropic schema. 支持 data: 和 https: 两种 url."""
    url = openai_part.get("image_url", {}).get("url", "")
    if url.startswith("data:"):
        # data:image/png;base64,XXX → split prefix + b64 data
        header, b64 = url.split(",", 1)
        media_type = header.split(";")[0].replace("data:", "")  # image/png
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": b64,
            },
        }
    elif url.startswith(("http://", "https://")):
        return {"type": "image", "source": {"type": "url", "url": url}}
    else:
        raise ValueError(f"unsupported image_url: {url[:80]}")
```

#### 关键改动 3: ref 注入只动 text 段 (`main.py:698`)

```python
# 现状: user_message 是 str 时直接拼
user_message = "【本轮当前引用 开始】\n" + context + "\n【本轮当前引用 结束】\n\n" + user_message

# v3: 只动 text
user_text = "【本轮当前引用 开始】\n" + context + "\n【本轮当前引用 结束】\n\n" + user_text
```

#### 关键改动 4: 调 letta 时按是否有图组装

```python
# 现状 (main.py:200 / stream 同款)
letta.agents.messages.create(
    agent_id=agent_id,
    messages=[{"role": "user", "content": user_message}]
)

# v3
if not user_images:
    letta_content = user_text  # 保持 str, 老路径回归不变
else:
    letta_content = [{"type": "text", "text": user_text}] + user_images
letta.agents.messages.create(
    agent_id=agent_id,
    messages=[{"role": "user", "content": letta_content}]
)
```

#### 关键改动 5: `preflight_compact` 接收

preflight 只用 user_text 估 token (图不计文本 token, 模型端实际 image_tokens 从 vLLM usage 字段反向回填. spike 实测 320×80 PNG = 66 image_tokens, 安全余量足够).

#### 改动总量

`adapter/main.py` 约 30 行 (含 helper). `tests/test_chat_multimodal.py` 约 50 行单测.

---

### Layer 2: WebUI Svelte 分流 (1 工日)

`web/src/lib/components/chat/MessageInput.svelte` chat `+` 选文件时按 mime 分流:

```js
async function handleFile(file) {
    if (file.type.startsWith("image/")) {
        // 嵌进 message content, 不上传到 knowledge
        const b64 = await fileToBase64(file);  // strip "data:..;base64," 前缀
        pendingImages.push({
            type: "image_url",  // OpenAI 标准, adapter 那边再转 Letta schema
            image_url: { url: `data:${file.type};base64,${b64}` }
        });
    } else {
        // 走 admin upload + ScopePickerModal (现有 Phase 5b 逻辑, 不动)
        await openScopePicker(file);
    }
}

function buildSendPayload(text) {
    if (pendingImages.length === 0) {
        return { content: text };  // 老路径不变
    }
    return { content: [{ type: "text", text }, ...pendingImages] };
}
```

部署: WebUI 镜像 rebuild + restart, 套现有 Phase 5b 套路.

---

### Layer 3: 不动 file_processor

`adapter/file_processor.py:22` 的 `IMAGE_EXTS` 400 拒保留. 这条只针对 admin upload 路径 (knowledge 入库语义), 跟 chat path 严格分开. 删掉会让用户上传图到知识库时静默失败.

---

## 测试矩阵

| 用例 | 输入 | 期望 |
|---|---|---|
| T1 单图 (base64) | message: text+image_url | adapter 转 Letta image+source.base64 → letta 转回 OpenAI image_url → vLLM 看到图 |
| T2 文 + 图混合 | text 长 + image_url | text 段+ref 注入完整, 图段独立透传 |
| T3 多图 (>1 image_url) | text + 2x image_url | adapter 输出 1 个 text + 2 个 image content part |
| T4 图 + ref 引用混合 | ref_files + image_url | ref 注入 prepend 到 text 段, image 段不被污染 |
| T5 纯文回归 | content: str (老路径) | 现状不变, content 保持 str (关键回归) |
| T6 URL 形式 image | image_url.url=https://... | adapter 转 Letta source.type=url |
| T7 Letta agent 多轮含图 | 第 1 轮发图, 第 2 轮纯文 | 第 2 轮 in-context 仍能引用第 1 轮图 (取决于 letta archival 处理) |
| T8 admin upload 拒图回归 | /admin/api/upload-with-scope 传 png | 仍 400 拒 (file_processor IMAGE_EXTS 不动) |
| T9 multimodal + preflight rebuild | 含图 message 触发 ctx > 阈值 | rebuild 后新 agent 仍能看图 |
| T10 不支持的 image format | content_type 不在 image/png/jpeg | adapter raise 400 friendly |

T1-T6 用 spike-multimodal docker stack 跑, mock-vllm proxy 模式连真模型 (DashScope / 临港 / ModelScope 任选).

---

## 工时

| 模块 | 工时 |
|---|---|
| Layer 1 adapter 协议转换 + helper + ref 兼容 | 0.3d |
| 单测 (T1-T6 base64 + URL + 多图 + ref + 纯文回归) | 0.2d |
| Layer 2 WebUI Svelte 分流 + rebuild + 部署 | 1d |
| 联调 e2e (T1-T10 全过) | 0.3d (.46 上线时跑) |

**总 1.8 工日 ≈ 2 个工作日, 比 v2 估的 3-5d 省一半.**

---

## 风险

| 风险 | 缓解 |
|---|---|
| WebUI 改坏 chat `+` 上传逻辑 | 先做 image 分流, 不动其它文件分支; 灰度发 1 用户 |
| 旧 chat 历史含 base64 image 后 in-context 累积爆 ctx | letta 自己 summarize 应该处理; 失败时观察 preflight 是否触发 rebuild |
| LLM provider 切换 (DashScope → ModelScope → 临港 vLLM Kimi) 多模态行为不一致 | spike 链路与 provider 解耦, 切换只改 OPENAI_API_BASE; 政务底座选型时验证一次目标 provider |
| 图过大 (>10MB base64) 撑爆 ctx | adapter 上传时硬限 5MB; 超额前端 Pillow 压缩或 raise 400 |

---

## 决策点 (执行前确认)

1. **Layer 1 单独可上**: adapter 改完只在 spike 工程测, 不需要 .46. 现在做不阻塞.
2. **Layer 2 必须 .46**: WebUI rebuild 要服务器, 等 .46 回来再做.
3. **图大小限制**: 默认 5MB. 是否需要前端 canvas 压缩 (1080p / 80% jpeg)? 政务公文截图通常远小于 5MB, 我倾向不上压缩.

---

## 不做的事

- knowledge 入库 OCR (单独立 ticket; chat 看图不需要)
- 视频 / 多图聚合 (V2)
- 图片 PII 脱敏 (V2)
- vision embedding (RAG 检索图片) — 替代 OCR 的方案, 但是 V2 工程

---

## 后续维护

- Memory `project_letta_image_native_anthropic_schema.md` 已建 (覆盖错的 `project_letta_image_unsupported.md`)
- `docs/todo-next.md` 第 17 项 OCR 降级到 P3 (only knowledge 入库, chat 看图不需要)
- spike 工程 `adapter/spike-multimodal/` 留着, 改 adapter Layer 1 代码时直接 docker compose up + python run_spike.py 重测
