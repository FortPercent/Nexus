# 驾驶舱与运营 Metrics 设计 (Issue #13)

> 编写: 2026-05-05  状态: design  作者: Claude

## 背景

`admin-dashboard.html` 当前治理向偏强（Nexus 2.0 决策追溯/冲突/Safety），缺运营向。
政务投标 + 多委办局个性化场景下，需要：
- 调用量/响应时长/成功率多维聚合看板
- 用户/项目/智能体维度排行
- A/B 测试分桶 + 灰度
- 满意度反馈闭环
- prompt 版本管理 + 回滚
- 离线评估集 + LLM-as-judge

数据底座共用一张 `metrics` 表，所有功能都从它衍生。

---

## 数据模型

### metrics（事件粒度，每请求 1 行）

```sql
CREATE TABLE metrics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  user_id TEXT NOT NULL,
  project_id TEXT,                         -- letta-* 路径才有
  agent_id TEXT,                           -- letta-* 路径才有
  model TEXT NOT NULL,                     -- letta-cpm / qwen-no-mem 等
  endpoint TEXT NOT NULL,                  -- /v1/chat/completions / /admin/api/upload-with-scope
  method TEXT NOT NULL DEFAULT 'POST',
  status INTEGER NOT NULL,
  latency_ms INTEGER NOT NULL,
  ttft_ms INTEGER,                         -- streaming 才有
  tokens_in INTEGER DEFAULT 0,
  tokens_out INTEGER DEFAULT 0,
  cost_micro_cny INTEGER DEFAULT 0,        -- 1/1000000 元，留口子
  variant_id TEXT,                         -- A/B 实验分桶
  feedback_score INTEGER,                  -- 1=👍 / -1=👎，反馈同步后回填
  request_id TEXT,                         -- 关联反馈/审计的全局 id
  err_class TEXT                           -- vllm_timeout / letta_500 / ...
);

CREATE INDEX idx_metrics_ts ON metrics(ts);
CREATE INDEX idx_metrics_user_ts ON metrics(user_id, ts DESC);
CREATE INDEX idx_metrics_project_ts ON metrics(project_id, ts DESC);
CREATE INDEX idx_metrics_request_id ON metrics(request_id);
```

体量预估: 30 用户 × 200 请求/天 × 365 天 = 220 万行/年, SQLite 完全扛得住。
500 用户量级 → DuckDB 列存切档（已有 L2 DuckDB 基础设施可复用）。

### experiments

```sql
CREATE TABLE experiments (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  variant_a_persona TEXT NOT NULL,
  variant_b_persona TEXT NOT NULL,
  traffic_split INTEGER DEFAULT 50,        -- B 桶占比 0-100
  status TEXT DEFAULT 'draft',             -- draft/running/paused/concluded
  primary_metric TEXT DEFAULT 'feedback_score',
  started_at TIMESTAMP,
  concluded_at TIMESTAMP,
  conclusion TEXT,                         -- a_wins / b_wins / no_diff
  created_by TEXT NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### persona_versions

```sql
CREATE TABLE persona_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  persona_text TEXT NOT NULL,
  status TEXT DEFAULT 'draft',             -- draft/active/archived
  deployed_at TIMESTAMP,
  deployed_by TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(agent_id, version)
);

-- 每 agent 至多 1 条 active
CREATE UNIQUE INDEX idx_persona_active
  ON persona_versions(agent_id) WHERE status='active';
```

### eval_cases / eval_runs

```sql
CREATE TABLE eval_cases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL,
  query TEXT NOT NULL,
  expected_keywords TEXT,                  -- JSON array
  expected_outcome TEXT,                   -- 给 judge LLM 用
  dataset_tag TEXT DEFAULT 'main',
  created_by TEXT NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE eval_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL,
  agent_id TEXT NOT NULL,
  persona_version INTEGER,
  variant_id TEXT,
  response_text TEXT,
  keyword_hits INTEGER,
  judge_score INTEGER,                     -- 1-5
  judge_reasoning TEXT,
  latency_ms INTEGER,
  ran_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## 采集

### adapter middleware（新增）

`adapter/middleware_metrics.py`：

```python
@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    if not request.url.path.startswith(("/v1/", "/admin/api/")):
        return await call_next(request)

    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    request.state.request_id = request_id
    request.state.metrics_ttft_ms = None
    request.state.metrics_tokens_in = 0
    request.state.metrics_tokens_out = 0
    started = time.perf_counter()

    err_class = None
    try:
        response = await call_next(request)
        status = response.status_code
    except Exception as e:
        status = 500
        err_class = type(e).__name__
        raise
    finally:
        latency_ms = int((time.perf_counter() - started) * 1000)
        # fire-and-forget，不阻塞响应
        asyncio.create_task(_persist_metrics(
            request, status, latency_ms, request_id, err_class
        ))
    return response
```

`_persist_metrics` 从 `request.state` 读 user_id / project_id / agent_id / variant_id / tokens（流式路径 stream wrapper 在 chunk 完毕后回填）。

### TTFT 采集

`_stream_letta_chat` 在第一个非空 token chunk 时记 `request.state.metrics_ttft_ms`，metrics middleware 直接读。

### token 采集

- vLLM `/v1/chat/completions` stream 的 `usage` 字段在最后 chunk 出现（vLLM 1.0+），capture 即可
- 非流式 `response.usage.{prompt_tokens, completion_tokens}` 直读

---

## 物化视图（DuckDB）

DuckDB 已经被 L2 引入做 xlsx/csv 查询。复用同进程内 attach 一个 `metrics.duckdb`：

```sql
CREATE VIEW metrics_1m AS
SELECT
  date_trunc('minute', ts) AS bucket,
  user_id, project_id, model, endpoint,
  count(*) AS req_count,
  sum(case when status >= 500 then 1 else 0 end) AS err_5xx,
  sum(case when status >= 400 and status < 500 then 1 else 0 end) AS err_4xx,
  quantile_cont(latency_ms, 0.5) AS p50,
  quantile_cont(latency_ms, 0.99) AS p99,
  sum(tokens_in) AS tokens_in_sum,
  sum(tokens_out) AS tokens_out_sum
FROM metrics
GROUP BY 1, 2, 3, 4, 5;
```

每小时 cron 把 `metrics_1m` 视图聚合到 `metrics_1h` 物理表（避免实时全表扫）。

---

## REST API

```
GET  /admin/api/metrics/timeseries?from=&to=&group_by=&filter=
GET  /admin/api/metrics/leaderboard?dim=user|project|agent&metric=count|p99|err_rate&top=10
GET  /admin/api/metrics/realtime         (SSE, 1Hz, 最近 60s 滑窗)

POST /admin/api/experiments              (create)
GET  /admin/api/experiments              (list)
PATCH /admin/api/experiments/{id}        (start/pause/conclude)
GET  /admin/api/experiments/{id}/results (variant_a vs variant_b 对比)

GET  /admin/api/personas/{agent_id}/versions
POST /admin/api/personas/{agent_id}/versions          (草稿)
POST /admin/api/personas/{agent_id}/versions/{ver}/activate  (发布)
POST /admin/api/personas/{agent_id}/rollback          (回滚到上一活跃版本)

POST /admin/api/feedback                 (WebUI 反向同步 vote)
GET  /admin/api/feedback?score=-1&from=  (低分 case 审核队列)

POST /admin/api/eval/cases
POST /admin/api/eval/runs                (启动一次全集运行)
GET  /admin/api/eval/runs/{id}/results
```

---

## 满意度反馈闭环

Open WebUI 原生有 message vote 按钮（👍/👎），落 `webui.feedback` 表。
adapter 反向同步 worker：
1. 每 60s 扫 `webui.feedback WHERE created_at > last_synced`
2. 通过 chat_id + message_id 反查 `metrics.request_id`
3. `UPDATE metrics SET feedback_score WHERE request_id=...`

textarea 反馈要 Svelte patch（套路同 Phase 5b ScopePickerModal）。

---

## A/B 测试分桶

`routing.py::get_or_create_agent` 改造：
1. 查 `experiments WHERE agent_id=? AND status='running'`
2. `hash(user_id) % 100 < traffic_split` → variant_b，else variant_a
3. variant_id 写 `request.state`，metrics 落
4. agent persona 临时覆盖（不持久化，只在 LLM call 时注入）

---

## prompt 版本管理 UI

admin-dashboard 加 "Persona" tab：
- 列表（按 agent_id 分组，显示当前 active version + 历史）
- 编辑器（左原版 / 右草稿，diff 视图）
- 操作：保存草稿 / 预览（mini chat 试跑）/ 发布 / 回滚

---

## 离线评估集

`scripts/eval_runner.py`:
- 输入：dataset_tag
- 输出：写 eval_runs
- 流程：每 case → call agent → 关键词命中检查 → LLM-as-judge（Kimi 自评，prompt = "Q: {query}\nExpected: {expected}\nResponse: {response}\nScore 1-5 + reason"）

cron 每天凌晨跑 main 数据集，结果回 admin UI eval tab。

---

## 工时

| 模块 | 工时 |
|---|---|
| metrics 表 + middleware + DuckDB 物化 | 3-5d |
| timeseries / leaderboard / realtime API + 看板 | 3d |
| A/B experiments | 2-3d |
| feedback 同步 + UI patch | 2-3d |
| persona_versions + UI | 3-4d |
| eval_cases + runner + judge + UI | 3-5d |
| 联调 + e2e + 文档 | 2-3d |

**总 18-26 工日 / 一个工程师 ≈ 4-5 周**

---

## 测试策略

- unit: middleware 异常吞吐、metrics 字段完整性、experiment 分桶 hash 一致性
- e2e: 跑 100 chat → 落 100 行 metrics → 看板 query 应返回正确聚合
- bench: middleware overhead < 5ms/请求

---

## 不做的事（留 V2）

- 实时 anomaly detection / alerting：先看板可视化，告警下个迭代
- 用户级 token 计费：政府客户内部不计费，加 `cost_micro_cny` 字段留口子
- Prometheus / Grafana 接出：优先内置 admin UI，外部 exporter 留 V2
