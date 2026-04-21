# Chat Pre-flight Compact 架构方案（2026-04-21）

## 1. 背景

### 1.1 现状问题
Letta 把"对话历史压缩"做在 agent loop 内部（`letta_agent_v3.py:1439` proactive / `:1218` reactive），存在 3 个结构性缺陷：

1. **依赖运行时变量**：`context_token_estimate` 只在 LLM 成功返回 `usage.total_tokens` 后才被设置；对已超限 agent 开新会话，LLM 请求本身 400，这个变量永远 None，proactive 路径永远不走
2. **失败模式不透明**：反应式路径依赖 `ContextWindowExceededError` 映射 + summarizer 自己不超限 + FK 约束通过，任一环节挂都是静默 500
3. **架构耦合**：Letta 是外部 pip 依赖（0.16.7），我们通过 `letta-patches/*.py` bind-mount 打 monkey patch。每次 Letta 升级都要重新 patch、重测

### 1.2 实测故障案例
- 04-20：biany `asset-management` agent 撞 71268 tokens（>65K vLLM 上限），静默 400 停止服务直到手动 rebuild
- 04-21：巡检 31 agent，`ai-infra-cache` 50575 tokens 逼近 54000 阈值（90%）

## 2. 目标与非目标

### Goals
- **G1 杜绝静默 400**：chat 请求要么成功、要么给用户明确错误提示，不允许"发完消息没回音"
- **G2 不依赖 Letta 内部 patch**：Letta 升级随便升，我们的压缩逻辑不动
- **G3 压缩决策在我们能观测 / 测试的代码里**：指标、日志、单元测试都在 adapter 侧
- **G4 用户体验可预测**：失败时明确告诉用户"对话已压缩"或"对话已重置"

### Non-goals
- **不重写 Letta 内部压缩逻辑**（那是 Letta 的事）
- **不消除所有可能的 400**（系统 prompt 本身超限这种事情管不了）
- **不改聊天协议**（OpenAI 兼容 / SSE 保持不变）

## 3. 当前架构

```
User ── HTTP ──> nginx(9800) ──> adapter(/v1/chat/completions)
                                     │
                                     ├─ qwen-no-mem 分支 → 直接透传 vLLM
                                     │
                                     └─ letta-* 分支 → Letta /messages/stream
                                                          │
                                                          ├─ proactive compact (可能不触发)
                                                          └─ reactive compact (可能失败)
                                                          
                                                        ↓ LLM
                                                        vLLM
                                                        ↑ 400 if overfull
                                                        ↓
                                             静默失败 / 500 → 用户
```

## 4. 提议架构

在 adapter 的 letta-* 分支**入口**加 pre-flight 检查：

```
User ── HTTP ──> nginx(9800) ──> adapter(/v1/chat/completions)
                                     │
                                     └─ letta-* 分支
                                         │
                                         ├─ [新] _preflight_compact(agent_id)
                                         │    ├─ GET /v1/agents/{id}/context  (缓存 60s)
                                         │    ├─ size >= window×0.85 ?
                                         │    │   └─ Y → POST /v1/agents/{id}/summarize
                                         │    │         ├─ ok + size 降了 → 继续
                                         │    │         └─ 失败或仍超限 → _rebuild_agent_async
                                         │    │                              返回 {"rebuilt": true}
                                         │    └─ N → 继续 (fast path, <1ms)
                                         │
                                         └─ 正常转发 Letta
```

### 4.1 阈值策略

| 区间 | 动作 |
|---|---|
| `< 70%` window | fast path，直接转发 |
| `70-85%` window | 异步触发 summarize（不阻塞本次请求），本次正常转发 |
| `>= 85%` window | **同步** summarize + 等完成，然后转发 |
| summarize 失败 或 summarize 后仍 >= 85% | **rebuild**，用户看到"对话已重置"提示，本次消息作为新 agent 的首条 |

### 4.2 API 契约

#### `_preflight_compact(letta_client, agent_id, user_id, project_id, db) -> dict`

```python
async def _preflight_compact(letta, agent_id, user_id, project_id, db):
    """Returns: {"action": "noop"|"async_summarize"|"sync_summarize"|"rebuilt", ...}"""
    ctx = await _get_ctx_cached(letta, agent_id)
    ratio = ctx.current / ctx.window
    
    if ratio < 0.70:
        return {"action": "noop", "ratio": ratio}
    
    if ratio < 0.85:
        asyncio.create_task(_safe_summarize(letta, agent_id))
        return {"action": "async_summarize", "ratio": ratio}
    
    # >= 85%: 硬同步
    try:
        await asyncio.wait_for(letta.agents.summarize(agent_id=agent_id), timeout=30)
        ctx2 = await letta.agents.context.retrieve_async(agent_id)
        if ctx2.current / ctx.window < 0.85:
            return {"action": "sync_summarize", "ratio_before": ratio, "ratio_after": ctx2.current/ctx.window}
    except Exception as e:
        logging.warning(f"[preflight] summarize failed: {e}, rebuilding")
    
    # 最后兜底: rebuild
    new_agent_id = await _rebuild_agent_async(user_id, project_id, agent_id)
    return {"action": "rebuilt", "old_agent_id": agent_id, "new_agent_id": new_agent_id,
            "user_msg": "对话历史已压缩重置"}
```

#### 集成点（`main.py` letta 分支）
```python
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    model = body.get("model", "")
    if model.startswith("letta-"):
        agent_id = await _resolve_agent(user_id, project_id)
        preflight = await _preflight_compact(letta_async, agent_id, user_id, project_id, db)
        
        if preflight["action"] == "rebuilt":
            agent_id = preflight["new_agent_id"]
            # 把 rebuild 提示注入 SSE 首帧
            yield _sse({"role": "assistant", "content": f"⚠️ {preflight['user_msg']}\n\n"})
        
        # 正常转发 ...
```

### 4.3 context 缓存

`GET /v1/agents/{id}/context` 本身是毫秒级的 pg 查询，但每次 chat 都调一次会线性放大 Letta 的 DB QPS。用 60s TTL in-process cache：

```python
_ctx_cache: dict[str, tuple[float, ContextInfo]] = {}

async def _get_ctx_cached(letta, agent_id, ttl=60):
    now = time.time()
    hit = _ctx_cache.get(agent_id)
    if hit and now - hit[0] < ttl:
        return hit[1]
    ctx = await letta.agents.context.retrieve_async(agent_id)
    _ctx_cache[agent_id] = (now, ctx)
    return ctx
```

压缩后主动 `_ctx_cache.pop(agent_id)` 强制下一次刷新。

### 4.4 user_msg 注入点

对**流式**请求：SSE 首帧插入一个 assistant 文本 chunk 带 rebuild 提示，然后才转发 Letta 的流。

对**非流式**请求：把 rebuild 提示 prepend 到最终 `content` 里。

两种都不破坏 OpenAI 响应格式。

## 5. 实施计划

### Phase 1（0.5 天）：基础 pre-flight
- [ ] `adapter/preflight.py` 新模块：`_preflight_compact`、`_get_ctx_cached`、`_safe_summarize`
- [ ] main.py letta 分支入口挂钩
- [ ] unit test：mock Letta 覆盖 4 种路径（noop / async / sync / rebuild）
- [ ] 部署，跑 regression

### Phase 2（0.5 天）：回退 Letta patch
- [ ] 从 docker-compose 卸载 `letta-patches/letta_agent_v3.py` 挂载
- [ ] 保留文件作为参考代码，不再用
- [ ] 重跑 regression + bench，确认 pre-flight 完全取代内部 patch

### Phase 3（可选，0.5 天）：观测
- [ ] 在 adapter DB 加 `preflight_events` 表：`(ts, agent_id, action, ratio_before, ratio_after, dur_ms, err)`
- [ ] `/admin/api/personal/me` 返回里加"你的 agent 上次压缩时间 / 当前水位"
- [ ] regression 加断言："过去 1h preflight rebuild 次数 < N"

## 6. 测试方案

### 6.1 单元（adapter/tests/test_preflight.py）
- mock Letta returning ctx ratio 0.5 → assert noop
- mock ratio 0.75 → assert async_summarize（任务起了）
- mock ratio 0.9 + summarize ok → assert sync_summarize，返回的 agent_id 不变
- mock ratio 0.9 + summarize 超时 → assert rebuilt，返回新 agent_id

### 6.2 集成
- 用 `/tmp/force_overfull.py` 推 ai-infra-cache 到 >85% → 下一个 chat 应该看到 "对话已压缩" 或 rebuild
- 验证 rebuild 后新 agent 有 human block（跨 agent 共享不丢）

### 6.3 压测
- bench_mixed_100，对比 pre-flight 前后 p50/p99：预期 p50 不变（fast path），p99 可能 +50-100ms（少量命中 85% 阈值）
- 如果 rebuild 率 > 5%，说明阈值太低或用户使用模式激进，调参

## 7. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 每次 chat 多一次 Letta HTTP | Letta DB QPS 线性放大 | 60s context cache，每 agent 每分钟最多 1 次查 |
| rebuild 误触发让用户丢上下文 | 用户抱怨 | 阈值留 15% 余量（85%），summarize 成功率实测 > 95%；rebuild 前清楚提示用户 |
| async summarize 任务堆积 | Letta 后台压力 | `asyncio.Semaphore(3)` 限并发 |
| Letta /summarize endpoint 不可靠 | pre-flight 降级到 rebuild | rebuild 有兜底，不会完全挂 |
| cache 把过期状态返回 | 误判 fast path，又撞 400 | TTL 60s 够短；compact 后主动 invalidate |

## 8. 回退

如果 pre-flight 上线后出问题：
1. 5 秒回退：注释掉 main.py 的 `await _preflight_compact(...)` 一行
2. 30 秒回退：`git revert <commit>` + redeploy
3. letta-patches 文件还在，docker-compose 改回来 1 分钟恢复旧架构

## 9. 为什么不是别的方案

### 不选"adapter 全接管 message 存储"
- 工作量 3-5 天（重写 Letta message 存储层）
- 失去 Letta 的 archival / passage / block 等周边能力
- 收益和代价不匹配

### 不选"硬 N 条消息截断"
- 无摘要 = 丢所有前文上下文
- 用户感知"agent 突然失忆"

### 不选"只改 Letta patch"
- G2 违反（还是耦合 Letta 内部）
- 边缘 case 永无止境（今天踩了 FK，明天踩别的）

### 选 pre-flight 的唯一缺点
- 每请求 +50ms 最好路径（fast path 命中率 >90% 时可忽略）

## 10. 下一步决策

请确认：
- [ ] 整体方向是否 OK（adapter 边界接管 vs 修 Letta 内部）
- [ ] 阈值 70/85% 是否合适（可改）
- [ ] Phase 2 回退 letta_agent_v3 patch 是否立即做（还是保留作双保险几周）
- [ ] 是否要 Phase 3 观测（不急可推后）

确认后我从 Phase 1 开始动手。
