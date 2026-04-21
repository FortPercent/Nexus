# Pre-flight Compact v1 实现清单（2026-04-21）

> 本文覆盖你的 4 个主要修正 + 3 个次要修正，替代 `compact-preflight-design.md` 作为 v1 落地规范。
> 原设计文档保留作"初版提议"留底，不作为执行依据。

## 1. 变更范围（v1 只做这些）

- ✅ **同步** pre-flight（请求进来就查，超阈值先处理再转发）
- ✅ **per-agent 锁**（同 worker 内）+ **延迟删除旧 agent**（跨 worker 兜底）
- ✅ **绝对余量**阈值（基于"最终发给 Letta 的 user_text"，不是原始 body.messages）
- ✅ summarize 失败 → rebuild
- ✅ **原子** rebuild（旧 agent 不被同步删，留宽限期；map 切换单行原子）
- ❌ **不做** 70-85% 异步 summarize（删掉，v2 再说）
- ❌ **不做** Phase 2 回退 letta-patches（双保险留 3-5 天）
- ❌ **不做** preflight_events 观测表（v1 用日志，regression 不加 rebuild rate 断言）

## 1.5 目标（v1 能保证 / 不能保证）

### G1（保证）
**不再因 Letta 自身 compact 缺陷（v3 post-step 依赖 `usage.total_tokens`、反应式 summarize FK 等）导致 agent 静默卡死**。这类故障是今天观察到的、可复现的、根因已定位的。

### G1 不等于"所有 context 400 都消失"
preflight 只管**输入侧**预判，以下场景仍可能 LLM 侧 400、本方案不覆盖：
- 工具调用返回超长（grep 命中大量文档、DuckDB SELECT * 百万行）
- 系统 prompt 本身已接近 window 上限（`SystemPromptTokenExceededError`）
- 单条用户消息粘贴了巨量文本（我们估算了但估不准）

这些属于另外的问题域，需要独立的护栏（工具输出截断、system prompt 分块、上传时 client-side 提示）。本方案**不处理**，别把 v1 当银弹。

## 2. 阈值公式（v1 用"绝对余量"）

### 2.1 估算对象必须锁定成"最终 user_text"

**不能**把 request.body.messages 整个传进来估。adapter 在转发 Letta 前会：
- 展开 `#filename` 引用（把引用的 md 全文 prepend 到 user message）
- 内联附件正文（如果用户上传了 xlsx，实际传给 Letta 的可能是 DuckDB 表摘要）
- 注入一些系统标记（例如 user_id、project 元信息）

所以估算函数**只吃一个字符串**：最终要发给 Letta 的那条 user message 完整 content。调用方先把 # 引用/附件展开完，拿到 `final_user_text`，再调 preflight。

```python
# config.py
CTX_SAFE_MARGIN = 5000          # 和 regression t_agent_prompt_under_vllm_limit 一致
CTX_USER_MSG_OVERHEAD = 500     # tool schema / letta 侧 system injection 的常数开销

def _estimate_user_tokens(final_user_text: str) -> int:
    """估算最终发给 Letta 的 user message content 的 token 数。

    **调用约定**: 传入的是已经展开 # 引用 / 内联附件 / 注入系统标记之后
    的 *最终 user_text*, 不是原始 request body 里的 messages 数组.
    若传错 (比如把整个 body.messages 丢进来), 会过度 summarize / rebuild.

    估算方法: 中文字符占主 → char × 2. 比真实 tokenizer 通常高估 30%,
    v1 宁可高估触发 summarize 也不要漏过去让 LLM 400."""
    return len(final_user_text) * 2

def _danger(ctx_current: int, ctx_window: int, est_user: int) -> bool:
    margin = ctx_window - ctx_current
    return margin < CTX_SAFE_MARGIN + CTX_USER_MSG_OVERHEAD + est_user
```

### 2.2 使用示例

ctx_window=60000 current=52000，用户发 "帮我分析这个 300 字的文档"（**无 # 引用、无附件**）：
- final_user_text = "帮我分析..."（300 字左右）
- margin = 8000
- est_user ≈ 300 × 2 = 600
- threshold = 5000 + 500 + 600 = 6100
- **8000 ≥ 6100 → safe**，直接转发

同样 current，用户发 "帮我看看 #product-spec.md" 且 product-spec.md 是 5000 字：
- adapter 展开后 final_user_text ≈ "帮我看看 [product-spec.md 内容]..."（5300 字）
- est_user ≈ 10600
- threshold = 16100
- **8000 < 16100 → danger**，进锁后走 summarize / rebuild

## 3. Per-agent 锁策略

### 3.1 同一 worker 内：`asyncio.Lock`
```python
_agent_locks: dict[str, asyncio.Lock] = {}
_agent_locks_guard = asyncio.Lock()  # 保护 _agent_locks 自己的创建

async def _acquire_agent_lock(agent_id: str) -> asyncio.Lock:
    async with _agent_locks_guard:
        if agent_id not in _agent_locks:
            _agent_locks[agent_id] = asyncio.Lock()
        return _agent_locks[agent_id]
```

### 3.2 跨 worker（gunicorn -w 4）：**延迟删除 + 幂等**

不加跨进程锁（redis / advisory lock 都重），但**必须处理 B 在 worker 2 上拿着旧 agent_id 往前跑、A 在 worker 1 上 rebuild 并删旧的场景**。

**关键改动**：v1 `_atomic_rebuild` **不在热路径删旧 agent**。改成：
- 切换 map 指向新 agent → 旧 agent 从这一刻起变成"孤儿"
- **旧 agent 先不删**，依赖今天上的 `reconcile_orphan_agents.py`（`min_age_hours=1`）几小时后兜底扫
- 宽限期内（1h+），任何持有旧 agent_id 的 in-flight 请求仍能正常发消息，因为旧 agent 在 Letta 里还活着

这样即使 B 在 (a)(b) 步骤之前就读过了旧 map，它转发到旧 agent 时**不会 404**。代价：旧 agent 占 Letta 存储几小时（可忽略）。

竞态结果分析：
| 场景 | 结果 |
|---|---|
| A/B 都进 preflight danger 区 → 各自 rebuild | 两次 DELETE map + 两次 get_or_create_agent：会有 2 个新 agent，map 指向竞态 winner，另一个成孤儿（reconcile 扫）|
| A rebuild 完成，B 已持旧 id 发消息 | 旧 agent 1h 内仍活，B 正常完成本轮 |
| A/B 都进 danger 区，A summarize 成功（没 rebuild），B 随后进来 | B 锁内重查，发现 margin 足够（summarize 生效了），直接转发 |

**记成 todo**：v2 上 sqlite advisory lock 彻底消除"2 个新 agent"的浪费。v1 接受每次少量冗余（≤1 个孤儿 agent / 竞态事件），reconcile 兜底。

## 4. 核心函数签名

所有函数落在 `adapter/preflight.py`（新文件）。

```python
from dataclasses import dataclass
from typing import Literal

Action = Literal["noop", "sync_summarized", "rebuilt"]

@dataclass
class PreflightResult:
    action: Action
    agent_id: str          # 最终该用哪个 agent_id (rebuild 后可能变)
    rebuilt: bool          # 是否发生了 rebuild
    ctx_before: int | None
    ctx_after: int | None
    user_msg: str | None   # 若 rebuilt, 提示文本; 否则 None


async def preflight_compact(
    letta_async,           # letta_client AsyncLetta
    agent_id: str,
    user_id: str,
    project_id: str,
    final_user_text: str,  # 调用方负责把 # 引用 / 附件展开后的最终 user content 传进来 (见 §2.1)
) -> PreflightResult:
    """入口函数. 所有竞态/锁/阈值/rebuild 逻辑在这里面."""
    # 步骤 1: 无锁快速路径
    ctx = await _get_ctx_fresh(letta_async, agent_id)  # 不用 cache, 见 §5
    est = _estimate_user_tokens(final_user_text)
    if not _danger(ctx.current, ctx.window, est):
        return PreflightResult(action="noop", agent_id=agent_id, rebuilt=False,
                               ctx_before=ctx.current, ctx_after=ctx.current, user_msg=None)

    # 步骤 2: 加锁重检
    lock = await _acquire_agent_lock(agent_id)
    async with lock:
        ctx = await _get_ctx_fresh(letta_async, agent_id)  # 锁内再查一次 (A B 同时进, B 等到 A 出锁后看到已压缩的状态)
        if not _danger(ctx.current, ctx.window, est):
            return PreflightResult(action="noop", agent_id=agent_id, rebuilt=False,
                                   ctx_before=ctx.current, ctx_after=ctx.current, user_msg=None)

        # 步骤 3: 同步 summarize
        ctx_before = ctx.current
        try:
            await asyncio.wait_for(
                letta_async.agents.summarize(agent_id=agent_id),
                timeout=30,
            )
            ctx2 = await _get_ctx_fresh(letta_async, agent_id)
            if not _danger(ctx2.current, ctx.window, est):
                return PreflightResult(action="sync_summarized", agent_id=agent_id,
                                       rebuilt=False, ctx_before=ctx_before,
                                       ctx_after=ctx2.current, user_msg=None)
            logging.warning(f"[preflight] summarize kept {ctx2.current} still danger, fallback rebuild")
        except asyncio.TimeoutError:
            logging.warning(f"[preflight] summarize timeout 30s for {agent_id}, fallback rebuild")
        except Exception as e:
            logging.warning(f"[preflight] summarize error: {e}, fallback rebuild")

        # 步骤 4: rebuild (原子流程见 §6)
        new_agent_id = await _atomic_rebuild(letta_async, agent_id, user_id, project_id)
        return PreflightResult(action="rebuilt", agent_id=new_agent_id, rebuilt=True,
                               ctx_before=ctx_before, ctx_after=0,
                               user_msg="⚠️ 对话历史已压缩重置 (超出上下文限制)")
```

## 5. Context 查询：不用 cache

初版只服务 `preflight_compact`，它只在 danger zone 再查一次。每次 chat 多一次 Letta HTTP 调用（~50ms），先接受。**cache 是 v2 优化**，v1 不做是为了避免 "stale cache 放过 overfull 请求" 这种正确性漏洞。

```python
async def _get_ctx_fresh(letta_async, agent_id) -> ContextInfo:
    r = await letta_async.agents.context.retrieve(agent_id=agent_id)
    return ContextInfo(
        current=r.context_window_size_current or 0,
        window=r.context_window_size_max or 60000,
    )
```

若 Letta SDK 还没 `agents.context` 命名空间（前面实测报过 `'AgentsResource' object has no attribute 'context'`），**用 httpx 直连**：

```python
async def _get_ctx_fresh(_, agent_id) -> ContextInfo:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{LETTA_URL}/v1/agents/{agent_id}/context")
    r.raise_for_status()
    d = r.json()
    return ContextInfo(
        current=d.get("context_window_size_current") or 0,
        window=d.get("context_window_size_max") or 60000,
    )
```

## 6. 原子 rebuild 流程（v1 不同步删旧 agent）

```python
# adapter/preflight.py
from routing import get_or_create_agent  # 本地 helper, 不是 Letta SDK 方法
from db import use_db

async def _atomic_rebuild(letta_async, old_agent_id: str,
                          user_id: str, project_id: str) -> str:
    """返回新 agent_id. 失败抛异常 (上层会转 HTTP 500, 但用户看到的是明确错误,
    不是 '没回音').

    关键: v1 不在热路径删旧 agent, 仅切 map. 旧 agent 变孤儿后由
    scripts/reconcile_orphan_agents.py (min_age_hours=1, 每 4h 扫) 兜底删.
    这样 cross-worker 的 in-flight 请求 B (持旧 agent_id) 仍有 1h+ 宽限期
    正常完成本轮."""

    # (a) 删 map 行
    #     routing.get_or_create_agent 内部逻辑: 先查 user_agent_map, 命中则 return old id;
    #     为了强制它走"新建"分支, 必须先 DELETE map
    with use_db() as db:
        db.execute(
            "DELETE FROM user_agent_map WHERE user_id=? AND project_id=?",
            (user_id, project_id),
        )

    # (b) 调我们自己的 helper 重建 (同步函数, 用 to_thread 不阻塞 event loop)
    #     内部会: 创建 Letta agent + attach blocks/tools/kb + 写新 map 行
    new_agent_id = await asyncio.to_thread(
        get_or_create_agent, user_id, project_id
    )

    # (c) 校验 map 确实指向新 agent
    with use_db() as db:
        row = db.execute(
            "SELECT agent_id FROM user_agent_map WHERE user_id=? AND project_id=?",
            (user_id, project_id),
        ).fetchone()
        assert row and row[0] == new_agent_id, f"map row mismatch {row} vs {new_agent_id}"

    # (d) 旧 agent **不删**. 从这一刻起它变成孤儿, 1h 宽限期后被 reconcile 扫掉
    #     这保证了跨 worker 的 in-flight 请求 (持 old_agent_id 直接发送消息的) 不会 404
    logging.info(f"[preflight] rebuild ok: {old_agent_id} -> {new_agent_id} (old left for reconcile)")
    return new_agent_id
```

**并发请求收敛（v1 关键保证）**：

| 场景 | 结果 |
|---|---|
| 请求 A 在 worker 1 完成 rebuild，请求 B 在 worker 2 发起前查 `user_agent_map` | B 拿到 new_agent_id，走新 agent，OK |
| A 完成 rebuild，B 在 worker 2 **已经在** (a) 之前查过 map 拿到 old_agent_id，现在正往旧 agent 发消息 | 旧 agent 还在 Letta 里（没删），B 正常完成；旧 agent 1h+ 后被 reconcile 清 |
| A/B 同时都进 danger 区（锁在各自 worker 内独立） | 两次 rebuild，产生 2 个新 agent；map 指向竞态 winner；另一个也成孤儿，reconcile 扫 |
| A 删 map 但 (b) 崩了 | map 行被删但没重写。下次请求走 get_or_create_agent → 建新的。旧 agent 和崩掉的那次建到一半的 agent 都孤儿（reconcile 扫）|

所有路径：**要么正常、要么产生可被清理的冗余**。**不存在 "用户发完消息没回音"**。

## 7. SSE / 非流式里的 user_msg 注入

### 7.1 产品语义确认
rebuild 后注入的 `"⚠️ 对话历史已压缩重置"` 是**货真价实的 assistant 消息**，不是 toast。用户看到的第一帧 = 系统提示 + 换行，然后才是 LLM 对用户消息的真实回答。

**这不是技术 hack**：Letta 那边新 agent 的 messages 表第一条 = 用户这次的输入；"对话重置提示"只在 HTTP 响应里加，不进 Letta。下次 chat 时用户不会"看到这句话两次"。

### 7.2 流式注入
```python
async def _inject_preflight_notice(notice: str):
    """先发一条 SSE chunk 带 notice, 格式和 Letta 正常流式兼容."""
    chunk = {
        "id": "chatcmpl-preflight",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": notice + "\n\n"},
                     "finish_reason": None}],
    }
    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

# 使用:
if preflight.rebuilt:
    async for chunk in _inject_preflight_notice(preflight.user_msg):
        yield chunk
async for chunk in forward_to_letta(new_agent_id, body):
    yield chunk
```

### 7.3 非流式注入
```python
resp = await forward_to_letta_nonstream(new_agent_id, body)
if preflight.rebuilt:
    resp["choices"][0]["message"]["content"] = (
        preflight.user_msg + "\n\n" + resp["choices"][0]["message"]["content"]
    )
return resp
```

## 8. 文件落点

| 文件 | 新建/改 | 内容 |
|---|---|---|
| `adapter/preflight.py` | 新建 | `preflight_compact`, `_atomic_rebuild`, `_acquire_agent_lock`, 阈值常量 |
| `adapter/config.py` | 改 | `CTX_SAFE_MARGIN=5000`, `CTX_USER_MSG_OVERHEAD=500` |
| `adapter/main.py` | 改 1 处 | letta 分支入口调 `await preflight_compact(...)` |
| `adapter/tests/test_preflight.py` | 新建 | mock-based 单元测试覆盖 3 种路径 + 1 种并发 |
| `docker-compose.yml` | **不改** | letta-patches 保留，双保险 3-5 天 |
| `scripts/regression.py` | **不改** | v1 不加 rebuild rate 断言 (需要 preflight_events 表才靠谱, 避免靠解析日志做脆弱断言) |

**Phase 2 删 letta-patches 的判据**：
- 3 天内 preflight rebuild 率 < 2/天
- 无"重复回复 / 消息丢失"用户抱怨
- regression `Letta agents 无孤儿漂移 (≤5)` 稳定通过
- 满足后 commit 删 `letta-patches/letta_agent_v3.py` 挂载行 + 文件

## 9. 实施顺序（每步单独可验证）

### Step 1（30 分钟）: 骨架 + unit test
- [ ] `preflight.py` 写 `_danger`, `_estimate_user_tokens`, `_get_ctx_fresh`
- [ ] `test_preflight.py` mock Letta 覆盖 `_danger` 的边界
- [ ] 不接入 main.py，纯单元

### Step 2（1 小时）: 主流程
- [ ] `preflight_compact` + `_atomic_rebuild` + `_acquire_agent_lock`
- [ ] unit test: mock Letta 覆盖 noop/sync_summarized/rebuilt 三条路径
- [ ] unit test: 两个 coroutine 同时进 danger 区，断言只有一个走 rebuild

### Step 3（30 分钟）: 接入 main.py
- [ ] letta 分支入口加 `preflight = await preflight_compact(...)`
- [ ] 如果 rebuilt，把 `preflight.agent_id` 喂给下游，首帧注入 user_msg
- [ ] 部署 + 看 regression 全绿

### Step 4（30 分钟）: 集成验证
- [ ] `/tmp/force_overfull.py` 推 ai-infra-cache 过危险线
- [ ] 下一条 chat 应该看到 `[preflight] rebuilt` 日志 + 用户看到"对话已重置"文本
- [ ] 验证 `letta agent 总数 == user_agent_map 行数`（或差 1 等 reconcile）

### Step 5（30 分钟）: 压测
- [ ] bench_mixed_100，对比 preflight 前后 p50 / p99
- [ ] 期望 p50 +30-50ms（fast path 加一次 context 查询），p99 +100ms 以内
- [ ] 如果 p99 涨 > 200ms，回退看哪条路径慢

### Step 6（next day）: 观察（人工，无自动断言）
- [ ] `docker logs teleai-adapter | grep '\[preflight\]' | tail -200` 人工看分布
- [ ] 重点：`action="rebuilt"` 次数 / 24h，`action="sync_summarized"` 次数 / 24h
- [ ] 期望：rebuild ≤ 2/天（2 天窗口观察），summarize ≤ 20/天；若明显超过，调阈值 / 查 summarize 失败率
- [ ] 想自动断言 → **先做 v2 的 preflight_events 表再加**，日志解析做断言会被 log rotate / 格式变化打挂

### Step 7（3-5 天后）: 删 letta-patches 双保险
- [ ] 判据见 §8 末尾
- [ ] 满足后 git commit 改 docker-compose + 删 patch 文件
- [ ] regression + bench 再跑一次确认

## 10. v2 挂 TODO（现在不做，现在就列）

- [ ] 跨 worker 的 sqlite advisory lock 真正消除竞态
- [ ] context 60s cache（先有实测的 Letta QPS 压力再做）
- [ ] 70-85% 异步 summarize 档（需要证明异步不破坏对话连续性才能上）
- [ ] preflight_events 指标表 + admin 面板卡片
- [ ] 用 real tokenizer 替代 `chars*2` 估算

## 11. 决策点

请确认这份清单 OK 再开干：
- [ ] 阈值公式 `margin < 5500 + est_user` 合适？ `CTX_SAFE_MARGIN` 想调大调小说一下
- [ ] per-agent 锁不跨 worker 接受吗？v2 挂 todo
- [ ] rebuild 提示文字 `"⚠️ 对话历史已压缩重置 (超出上下文限制)"` 可改，产品语义 OK？
- [ ] 双保险 3-5 天后删 letta-patches，还是我应该马上删？
