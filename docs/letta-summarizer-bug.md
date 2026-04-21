# Letta Summarizer 失效诊断（2026-04-21）

## 现象
- biany `asset-management` agent `num_tokens_current=71268`（>65K vLLM 上限），新消息必 400
- 其他 28 agent 对话历史单调递增、不自动压缩（本次巡检 ai-infra-cache 达 50575 / 65K）
- Letta `/v1/agents/{id}/summarize` 调用后生成 summary 但 messages 数不减

## 根因（源码级）

### Letta 版本: 0.16.7
实际聊天路径走 `LettaAgentV3`（确认来源：`server/rest_api/routers/v1/conversations.py:1061/1079` + `routers/v1/agents.py:2444`）

### 关键发现 1: v3 **proactive compaction 全被注释**

`/app/letta/agents/letta_agent_v3.py`:
- **lines 368–382** — 原本"step 后检查 total_tokens > context_window × SUMMARIZATION_TRIGGER_MULTIPLIER 就触发"的预防性压缩，**整块注释**
- **lines 628–638** — 原本"loop 结束后 safety-net rebuild context"的兜底压缩，**整块注释**

意味着：**agent 步进过程中从不主动压缩**。只有在 LLM 请求抛 `ContextWindowExceededError` 后才反应式补救（line 1218 retry 分支）。

### 关键发现 2: 反应式路径能触发但易失败

`/app/letta/agents/letta_agent_v3.py:1218`:
```python
except Exception as e:
    if isinstance(e, ContextWindowExceededError) and llm_request_attempt < summarizer_settings.max_summarizer_retries:
        summary_message, messages, summary_text = await self.compact(...)
        await self._checkpoint_messages(new_messages=[summary_message], in_context_messages=messages)
        continue
```

vLLM 400 映射为 `ContextWindowExceededError` 的条件（`llm_api/error_utils.py:8`）：
```python
return (
    "exceeds the context window" in msg
    or "This model's maximum context length is" in msg
    or "maximum context length" in msg
    or "context_length_exceeded" in msg
    or ...
)
```

vLLM 实际返回 `"This model's maximum context length is 65536 tokens. However, you requested 71268 tokens..."` —— 理论上能匹配 `"maximum context length"`，**应**进 retry 分支。

但反应式路径有两个隐患：
1. `compact()` 内部会再次调 LLM 生成 summary。如果传给 summarizer 的消息子集仍超 65K → summarizer 本身 400 → 三次 retry 耗尽 → 抛原异常，**agent 状态不变**
2. 用户看到一次 5xx/400（根据 adapter 怎么 proxy），**下次再聊仍是同一状态，继续失败**

### 关键发现 3: compact 成功才持久化

`services/summarizer/compact.py::compact_messages` (line 135):
- 返回 `CompactResult(summary_message, compacted_messages, ...)`
- **自己不写 DB**，靠调用方 `_checkpoint_messages` 更新 `agent.message_ids`

如果 compact 抛异常，`_checkpoint_messages` 不被调，**DB 状态毫无改变**。观察到的"messages 单调递增"现象吻合。

## 修法建议（三条路，按风险 / 收益排）

### 路线 A：**重新启用 v3 proactive compaction**（首选）
本地 patch `letta_agent_v3.py` 取消两段注释：
- lines 368–382（step 后检查 + 主动 compact）
- lines 628–638（loop 结束后 safety-net）

修改后 agent 每一步结束都会检查 `last_step_usage.total_tokens > context_window × SUMMARIZATION_TRIGGER_MULTIPLIER`，在**还没撞墙前**主动压缩。

**风险**：
- 这段代码被 Letta 团队注释必有原因（性能？递归死循环？）—— 先查 git blame / PR
- `SUMMARIZATION_TRIGGER_MULTIPLIER` 值若为 0.7 则每次聊天都试 compact，可能增加 token 成本
- 兜底逻辑本地生效但 Letta 升级时会丢失（需维护 patch 到 adapter 启动脚本）

**工期**：patch 写 + 测试 0.5 天；提 upstream PR 附 reproduction 1 天

### 路线 B：**adapter 侧 auto_rebuild_overfull_agents.py**（兜底保底）
定时扫 `num_tokens_current > 60000` → 触发 rebuild（= 删 agent 重建，丢历史）。
这是本次会话原方案，**不依赖修 Letta**。

**优劣**：
- 稳（不动 Letta 内部）
- 粗（整段对话丢）
- 与路线 A **不互斥**。A 先做，B 作为 A 失败时的最后防线

### 路线 C：**让 adapter 在 compact 失败时再 rebuild**
改 `letta_agent_v3.py` 的 except 分支：retry 全败后调 adapter `_rebuild_agent_async` 而非抛异常。
**风险高**：耦合 adapter 和 letta，动 letta 内部代码，回归面大。

**不建议现阶段做**。

## 决策

今天这个 session 做**路线 B**（`auto_rebuild_overfull_agents.py`），0.5–1 天能落、不动 letta。

下一步做**路线 A 的验证**：
1. 查 Letta repo `letta_agent_v3.py` 近期 git blame：**为什么**这两段被注释（故意 vs 忘了删）？如果是故意（比如递归问题），得换思路
2. 查 Letta issue tracker 是否已有人报告 "agent context unbounded growth" 类问题
3. 若 upstream 没修过也没讨论过 → 提 PR；我们本地先 patch

## 补充数据（2026-04-21 巡检）

```
top 5 by tokens (ceiling 60000):
  50575  ai-infra-cache     msgs=149  CLOSE
  49809  ai-infra           msgs=269
  37280  01                 msgs=115
  35835  security-mgmt      msgs= 35
  35135  ai-infra           msgs=100
```

`ai-infra-cache` 距 65K 只差 15K tokens，预计下一周内将撞墙，**B 方案在此之前必须上**。
