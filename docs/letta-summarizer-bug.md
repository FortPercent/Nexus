# Letta Summarizer 失效诊断（2026-04-21, v2 修正版）

> ⚠️ v1 诊断误以为 "proactive compaction 全被注释"——**错了**。实际 proactive 在 `letta_agent_v3.py:1439` 是活的，但依赖一个仅在 LLM 成功后才被设置的运行时变量，对已超限 agent 永远不触发。

## 现象
- biany `asset-management` agent `num_tokens_current=71268`（>65K vLLM 上限），新消息必 400
- 其他 28 agent 对话历史单调递增、不自动压缩（本次巡检 ai-infra-cache 达 50575 tokens）
- 手动 rebuild 才能救

## 根因（源码级）

### Letta 版本: 0.16.7
实际聊天路径走 `LettaAgentV3`（确认来源：`server/rest_api/routers/v1/conversations.py:1061/1079` + `routers/v1/agents.py:2444`）

### Proactive compaction 活着，但触发条件有缺陷

`letta_agent_v3.py:1439`:
```python
if self.context_token_estimate is not None and self.context_token_estimate > compaction_trigger_threshold:
    ...
    summary_message, messages, summary_text = await self.compact(...)
    await self._checkpoint_messages(...)
```

`compaction_trigger_threshold = get_compaction_trigger_threshold(llm_config)` 当前实现（`services/summarizer/thresholds.py`）：
```python
return int(llm_config.context_window * SUMMARIZATION_TRIGGER_MULTIPLIER)  # 0.9
```
对我们 Qwen 60000 ctx_window = **54000 阈值**。

### 缺陷：`context_token_estimate` 何时被设置？

`letta_agent_v3.py:129`：`self.context_token_estimate: int | None = None`（__init__）
`letta_agent_v3.py:1306`：`self.context_token_estimate = llm_adapter.usage.total_tokens`（**LLM 成功返回 usage 后**）

`LettaAgentV3` **每个请求新实例化一次**（`conversations.py:1061` `agent_loop = LettaAgentV3(agent_state=agent, actor=actor)`）。所以：

**对已超限的 agent 打开新会话**：
1. `self.context_token_estimate = None` （构造器）
2. pre-step 检查在 line 935 只 log warning
3. LLM 请求失败（vLLM 400，prompt 已超 65K）
4. 异常从 line 1306 上面抛出，`context_token_estimate` 仍然 None
5. post-step line 1439 检查 `is not None` → 跳过
6. messages 表没变，next request 同样失败

**反应式路径** line 1218 依赖 `ContextWindowExceededError`，vLLM 400 理论能映射（`"This model's maximum context length is"` 匹配 `is_context_window_overflow_message`），但 compact 本身也调 LLM，如果摘要请求也超限，三次 retry 全败，**`_checkpoint_messages` 从不被调**，DB 不变。

### 为什么 biany 会撞 71K？

biany 一开始 agent 还没超 54000，但 proactive 只在 post-step 检查，依赖 `usage.total_tokens`。vLLM 返回的 usage 可能**低估**（不含 tool 定义、tokenizer 和 Letta 预估器不一致），导致 estimate < 54000 但实际 context 已到 55K+。下一次 LLM 调用就已经 60K+，post-step 拿到真实 usage 才意识到——但此时已经来不及了，从此一路漂到 71K，之后每次 LLM 调用都 400，反应式 compact 也撑不住。

## 修复（路线 A，已上线 2026-04-21）

`letta-patches/letta_agent_v3.py` 通过 docker-compose bind-mount 覆盖容器内文件。

### 补丁位置
`letta_agent_v3.py:938` 后，step 主逻辑前，插入 **pre-step 预估 + 强制 compact** 块：

```python
# 如果 context_token_estimate 为 None, 用 persisted messages + tools 预估
if self.context_token_estimate is None:
    _teleai_tools = await self._get_valid_tools()
    _teleai_estimate = await count_tokens_with_tools(
        actor=self.actor, llm_config=self.agent_state.llm_config,
        messages=messages, tools=_teleai_tools,
    )
    self.context_token_estimate = _teleai_estimate

# 超阈值就立刻 compact, 别等 LLM 先挂
if (self.context_token_estimate is not None
        and self.context_token_estimate > compaction_trigger_threshold
        and not self.agent_state.message_buffer_autoclear):
    _pre_step_id = generate_step_id()
    summary_msg, messages, _ = await self.compact(messages, ...)
    await self.agent_manager.rebuild_system_prompt_async(...)
    messages = await self._refresh_messages(messages, force_system_prompt_refresh=True)
    self.response_messages.append(summary_msg)
    await self._checkpoint_messages(
        run_id=run_id, step_id=_pre_step_id,
        new_messages=[summary_msg],
        in_context_messages=messages,
    )
```

### 异常处理
- `SystemPromptTokenExceededError` 透传（系统提示本身超限是另一个问题，非我们能修）
- 其他异常只 log 不阻塞，让原 post-step 兜底

### 部署
`docker-compose.yml` 加一行：
```yaml
- ./letta-patches/letta_agent_v3.py:/app/letta/agents/letta_agent_v3.py:ro
```
restart letta-server 即生效。

### 验证
- regression 36/36 PASS
- Letta log 出现 `[teleai-patch] pre-step token estimate: 36441` —— 预估路径真的进入
- 补丁影响面：每个 LLM 请求多一次 `count_tokens_with_tools` 调用，毫秒级，可忽略

## 备选方案（没做）

### 路线 B: adapter 侧 auto_rebuild_overfull_agents.py
定时扫 `num_tokens_current > 60000` → rebuild（删 agent、丢历史）。
- 优：零修改 Letta
- 劣：用户体验差（整段对话丢），属降级方案
- 决策：**不做**，路线 A 成立后没必要。但如果路线 A 有边缘 case 失败，B 可作最后兜底

### 路线 C: 改 Letta except 调 adapter rebuild
耦合 Letta 和 adapter，回归面大。**不做**。

## TODO

- [ ] 构造触发用例：人为推 agent 过 54K 验证 pre-step compact 真的生效
- [ ] 观察 1-2 周，若线上没有再现 71K 这种超限状态，关闭 #26 监控告警
- [ ] Letta 上游可能修改 compaction 策略（近期 commit 密集）：定期 sync / 决定是否保留本地 patch
- [ ] 若 Letta 上游改得更合理，上游 PR 反馈这个边缘情况（冷启动 overfull agent）
