# 后续 TODO（2026-04-19 整理 · 04-19 晚大批更新）

本文件 = 04-17/04-18/04-19 两天压测+修复后的**全部待办**，按优先级 + 工时排。

> **04-19 晚进度**：P0 #1、P1 #4+#4a、P1 #5、P1 #6、P1 #7 都处理了，见各项 ✅ 标记。
> **04-21 晚进度**: P5a/5c (上传统一 + 反代兜底) done. Issues #1-#4 from `agent_frontend_issues_repro.md` done (commit `48b0968` + `c61027e` + `c3ffd3d`). Issues #5/#6 见下方分析.

---

## 🆕 Chat `+` 上传图片失败 → 根因不是 Letta，是 WebUI 路径分流（2026-04-22 立项, 2026-05-05 重判）

**2026-05-05 重新认定**: 之前 04-22 把"chat 看图"和"knowledge 入库"两条路径混了。Spike 验证 (`adapter/spike-multimodal/` + DashScope qwen3.6-plus 端到端) 实证: **Letta 原生支持图片**, 用 Anthropic 风格 schema (`type=image, source.base64/url`), 内部转 OpenAI image_url 给 LLM provider, 端到端通. **不需要 OCR.**

详见 `docs/multimodal-passthrough-design.md` v3 + memory `project_letta_image_native_anthropic_schema.md`.

**真症状**: WebUI chat `+` 上传图把图当 knowledge 附件走 admin upload 路径, 被 `file_processor.IMAGE_EXTS` 400 拒 (那是对的, knowledge 入库语义).

**真修法 (1.5 工日, 待 .46 回来上线)**:
1. WebUI Svelte `MessageInput.svelte`: chat `+` 选图分流到 message.content (不上传 knowledge), 1d
2. adapter `main.py:566/200/698` 协议转换 (OpenAI image_url ↔ Letta image+source.base64), 0.3d
3. 单测 + e2e, 0.5d

**保留不动**: `file_processor.py:22 IMAGE_EXTS` 400 拒 (admin upload 路径仍按知识库语义拒图).

**真要 OCR 的场景 (单独 P3, 不在本 ticket)**: 用户把图传到知识库做 RAG 检索. 那条路径 Letta 不收 binary image, 要 OCR 或 multimodal embedding. 不阻塞 chat 看图.

---

## 🆕 agent_frontend_issues_repro.md 追踪（2026-04-21）

Cursor 生成的 `agent_frontend_issues_repro.md` 列 6 个 issue:

| # | Severity | 状态 | Fix 说明 |
|---|---|---|---|
| 1 | high | ✅ done | `type=chat` 引用静默丢弃 → 加 `_load_chat_ref_context` 读 webui.db 注入历史消息 (commit `48b0968`) |
| 2 | high | ✅ done | 历史引用挤占当前 → adapter ref 注入加 `【本轮当前引用 开始/结束】` 外层包裹 + persona 指令模型区分 + per-(agent,ref) 1h dedup 防重复 (commit `c61027e` + `c3ffd3d`) |
| 3 | medium | ✅ done | agent over-claim 工具使用 → persona 加"工具调用诚信: DOCS_USED=N 必须等真实调用文件数" (commit `c3ffd3d`) |
| 4 | medium | ✅ done | 严格步骤未遵循 → persona 加"用户明示步骤顺序时必须按序逐步调用" (commit `c3ffd3d`) |
| 5 | medium (test) | ⏳ 非代码 bug | 同 chat 多标签并发测试框架不稳定. 后端 preflight session lock 是预期行为(防分叉),probe 脚本撞上锁超时. doc 自己说 "this does not prove backend can't handle concurrency". **后端无 action**, 建议用 "one fresh chat per worker" 测并发. |
| 6 | low | ⏳ Phase 5b | `#` / `@` / `/` 没提示引用入口; 只 `+` 菜单里才有"引用对话/知识库". 需 Svelte 改: `#` 下拉加 "引用对话" 项; `+` 按钮加 tooltip. 明天 Phase 5b 弹窗 rebuild 时一起改. |

**persona 刷新**: 运行了 `scripts/update_personas_kb.py`, 32 agent 都更新到新 persona (1580 → 2021 chars).

**regression 建议加的测试** (repro doc 里的 Test Matrix, 计划明天补):
- `REF-CHAT-001`: 单元测 `_load_chat_ref_context` 返回非空且含 title 关键词
- `REF-FILE-001`: E2E 测 body.files 混合本轮+历史 kid 时, 只本轮被 dedup-skip 之外的注入被 `【本轮当前引用】` 包裹
- `RAG-001` / `TOOL-ORDER-001`: LLM 行为不确定, 规范持续性待观察, 不做硬断言

---

## P0 · 安全/正确性 Bug（真 bug，不是优化）

| # | 事项 | 定位 | 工时 | 状态 |
|---|---|---|---|---|
| 1 | `extract_user_from_admin` 不验 user_id 存在性 | `auth.py:104` | 0.5d | ✅ **04-19 done**：查不到用户直接 401（bench_jwt_auth 验证通过）|
| 2 | `OPENWEBUI_JWT_SECRET` 仅 16 字节 | `.env` | 1h | ⏳ 待做，用户今晚没人用可直接做 |
| 3 | 需求 "把 user_id show 出来" | 待明确 | ? | ⏳ 场景未明确 |
| 26 | **`_rebuild_agent_async` 不删 Letta agent 残留孤儿** | `admin_api.py::_rebuild_agent_async` | 0.5d | 04-20 发现：wuxn5 一人累积 9 个孤儿（msgs 1-63 不等），biany 也有 1 个（project 重命名时 adapter 删了 map，Letta agent 留了）。已手动清 10 个。应在 rebuild 成功后显式 `letta.agents.delete(old_agent_id)`（先 detach 共享 block 防级联）。压测里 `bench_clear_conv` 不查 Letta agent 总数所以没暴露。**04-20 监控补丁**：`regression.py` 加断言 "Letta agents 无孤儿漂移 (≤5)"，diff > 5 就 fail，自动预警。**root fix 仍 pending**（只是加了监控） |
| 27 | **Open WebUI 原生 `/api/v1/knowledge/create` 上传不触发 adapter ingest hook** | `admin_api.py::_process_and_upload` 是唯一 ingest 入口 | 1-2d | 04-20 发现：biany 在 WebUI 原生 knowledge UI 传 16 个 asset-management 文件，`asset-management.duckdb` 根本不创建。L2 M4 真实验收失败。方案 A：reconcile_mirrors 发现新 xlsx/csv 时 warning + 请求重传；方案 B：改 Open WebUI 把 knowledge 上传转到 adapter；方案 C：把 L1 fallback grep 做回来。设计文档 §8 漏掉了这条路径 |
| 28 | **regression.py letta 聊天只问"你好"，不触发 semantic_search/grep** | `scripts/regression.py` | 1h | 04-20 发现：正因如此 embedding_config=None bug 隐藏 4 天 25/29 agent 都中招。加一条"对 letta agent 发足够 trigger semantic/grep 的 query，断言返回非空 + 非错误字串" |
| 40 | **Letta source_passages FK race condition** | Letta 内部 `insert source_passages` 和 `delete FileMetadata` 没序列化 | 待 Letta 上游修 / 或本地 patch | 04-20 bench_mixed_100 第一轮 minute 3 chat 62% 成功率 p99=65s，第二轮同 bench 99.4%（intermittent）。日志：`ForeignKeyConstraintViolationError: source_passages_file_id_fkey`，两轮分别 2580/N 次。bench 快速 upload（embed 异步启动）与后续 upload 产生的 file_metadata 状态漂移，source_passages insert 时引用已失效 file_id。Letta transaction rollback 大量发生拖慢整体。生产路径触发概率低（用户不会秒速 upload），记 todo 等 Letta 上游修或本地 patch（读 `letta/services/file_processor/` 源码在 insert 前 check file_metadata 是否 still 存在）|
| 41 | **xlsx/csv 全量物化再裁剪造成 RSS 峰值** | `file_processor.py::_xlsx_to_markdown` (openpyxl `list(ws.iter_rows(...))`) 和 `_csv_to_markdown` (`rows = list(reader)`) 先把所有行读进内存再 `MAX_ROWS_PER_SHEET = 5000` 切。50MB 上传 × 并发时 RSS 峰值高 | 1-2h | 改 streaming: 读一行写一行 md，到 MAX_ROWS 直接 break。openpyxl 的 `iter_rows` 本身是 generator，只要不 `list()` 就是 streaming。csv 同理。目标：大表 RSS 常数级而非 O(rows) |

---

## P1 · 性能/容量（用户体感相关，有实测数据支持）

| # | 事项 | 结果 | 状态 |
|---|---|---|---|
| 4+4a | adapter 切 gunicorn `-w 4` + fcntl singleton leader | C=200 p99 5.3s → 2.5s (~2×)，C=100 无明显变化（同步 SQLite 仍阻塞 event loop；真要 4× 得换 aiosqlite）| ✅ **04-19 done** |
| 5 | **对话清空 C=5+ 并发优化** | 单 worker 7.7s → gunicorn 4 workers 3.4s → 进一步 async `_rebuild_agent` + detach 并行（刚上线待测）| ✅ **04-19 done**（主要靠 gunicorn，加上 async 改造） |
| 6 | ~~Ollama → TEI~~ | **已被 Ollama AMD GPU 24 embed/s 解决**（04-17 晚）。TEI 只在 CPU 时代有意义，没 ROCm 镜像强上反而降级 | ❌ **不做** |
| 7 | 大文件上传优化 | profile 发现 `_csv_to_markdown` 只占 206ms/4300ms（5%），瓶颈在 Letta 侧 file upload pipeline。**adapter 侧无可优化**，ROI 低 | ✅ **04-19 调查完，不动代码** |

**aiosqlite 迁移（04-19 晚尝试）**：

| 6a | 切 `aiosqlite` 异步 DB 驱动 | 目的：消除 sync sqlite3 阻塞 event loop | — | ✅ **04-19 晚完成但性能无改善** |

**实测结论**（C=100 /admin/api/me bench 对比）：

| 配置 | p99 | QPS |
|---|---|---|
| sync use_db() + gunicorn 4w | 1.74s | 155 |
| aiosqlite + WAL + gunicorn 4w | 1.75s | 150 |
| aiosqlite + WAL + gunicorn 8w | 1.73s | 153 |

**发现 C=100 p99 瓶颈不是 event loop 阻塞**，是 Python per-request 固定 CPU 开销（JWT 解码 + HTTP 解析 + JSON 序列化）。aiosqlite 在简单 endpoint 上无收益。

**但改造仍保留**，原因：
- 未来重 DB 工作端点（N 次 JOIN、批量更新）会受益
- 消除"async def 调 sync I/O"反模式
- WAL 模式对写多场景利好（写不阻塞读）

**tech debt**：admin_api.py 还有 44 处 sync `use_db()` 调用未迁。**不着急**，能工作，下次遇到具体性能问题再 case-by-case 迁。

---

## Excel 处理优化（等用户反馈再做）

04-19 晚调研过，当前实现有性能和准确度两类改进空间，**但不主动做**——等真实用户用 xlsx 上传场景下反馈再针对性优化。

**profile 结果**（10k 行 × 10 列 xlsx）：
- `_xlsx_to_markdown` 总耗时 512ms
- 99% 时间在 `openpyxl.iter_rows`（XML 解析）
- `_fmt_cell` / `join` 等后处理都 <20ms

**可选优化清单**（用户反馈触发时参考）：

| 维度 | 问题 | 修 | 预期 |
|---|---|---|---|
| 性能 | openpyxl XML 解析慢 | 换 `python-calamine`（Rust 后端）| 10-20× 快 |
| 准确度 | 浮点 `:g` 格式 6 位有效精度丢失 | 改 `str(v)` 保原精度 | 财务/科学数据正确 |
| 准确度 | 大整数变科学计数（`1.23e+12`）| 同上 | 工号/订单号正确 |
| 准确度 | 货币/百分比格式丢失（`¥1234` → `1234`、`12%` → `0.12`）| 读 `cell.number_format` 保显示 | AI 不再误算 |
| 准确度 | 合并单元格只主格有值，其他空 | 用 `ws.merged_cells.ranges` 填充 | 合并表可读 |
| 准确度 | 多 sheet 合成单 .md 后 chunk 可能跨 sheet 边界 | 每个 sheet 加醒目分隔标记 | AI 不混 sheet |
| 准确度 | 公式缓存值可能 None（Python 生成 xlsx 从未打开过 Excel）| `data_only=False` fallback 读公式文本 | 空格减少 |
| 准确度 | 隐藏行/列原样暴露 | 按 `cell.hidden` / `ws.row_dimensions[i].hidden` 过滤 | 保护用户草稿数据 |
| 可观测 | 5000 行超限静默截断 | 现有"…另有 N 行已省略"提示够了，但可加 adapter 日志 | 运维排查方便 |

**触发条件**：
- 有用户反馈"AI 读我 xlsx 读错了数字"——先查是精度问题还是格式问题
- 有用户上传 >50MB xlsx 卡住——上 calamine
- 否则**不动**，避免过度工程

---

## P2 · 运维基础设施（长期，一次投入多次回报）

| # | 事项 | 影响 | 工时 |
|---|---|---|---|
| 8 | **每日 cron 回归 + 飞书告警** | 59/59 变化 / 5xx 超阈值自动通知 | 2h |
| 9 | **请求维度 metric 埋点** | 没 p50/p95 按 user/project/model 分布 | 1d，最低限度在 adapter middleware 写 JSON 行日志 |
| 10 | **压测基线 DB** | 无历史对比，性能劣化不可见 | 0.5d，每次 bench 结果写 `bench_history.json`|
| 11 | **Ollama 纳入 docker-compose** | 当前是 `docker run` 独立起，灾备配置不可追踪 | 1h |
| 12 | **adapter SQLite + letta PG 每日备份** | 备份方案不明确 | 0.5d，cron 写 `.44` 的 ceph |
| 13 | **staging 环境**（最小版）| 无 staging 导致破坏性测试只能凌晨做 | 2-3d，临港 VM 或搞 `.47` |

---

## P3 · 产品功能（功能 TODO，非压测相关）

| # | 事项 | 来源 | 工时 |
|---|---|---|---|
| 14 | 对话记忆 P1/P2：背景摘要卡 + 固化身份 + 聊天内指示灯 | 04-17 待办 | 3-5d |
| 15 | 项目巡检 V0：daily cron 扫僵尸 TODO / 积压建议 | 04-17 待办 | 2d |
| 16 | 聊天搜索 | 04-17 待办 | 2-3d |
| 17 | ~~图片 OCR~~ → **降级 P3**, 仅 knowledge 入库需要 (chat 看图本身不需要, 见 `docs/multimodal-passthrough-design.md` v3) | 04-17 → 05-05 重判 | 1-3d (knowledge 入库 only) |
| 18 | 知识架构 wiki 化（Karpathy）| 长期 | 长期 |
| 19 | 项目级 skill 维护 | `requirements-tracker.md #73` | 2-3d |
| 25 | **L3 原始文件归档体系**：L2 结构化数据查询上线后，存量 xlsx/csv 无法回填（原始 bytes 已转 md 后丢弃）。要有原始文件副本体系（ceph 或 adapter_data volume），才能支持"历史文件一键进 DuckDB"。见 `docs/structured-data-query-design.md §12 L3` | L2 + 04-20 | 2-3d |
| ~~42~~ | ~~管理面板展示 user_id + API 使用示例~~ | ✅ **04-20 done**：`/knowledge` header 加 "🔑 API" 按钮 → 弹 modal 展示 user_id/API key/base URL/letta-*curl/qwen-no-mem curl，全部一键复制 |

---

## P4 · 还没做的测试（大多需要 staging）

| # | 事项 | 为何重要 | 障碍 |
|---|---|---|---|
| 20 | 混沌工程（每天随机 kill）| 长期保 SLA | 需 staging |
| 21 | 真实用户录放 | 最贴近生产 | 需录流量 + staging |
| 22 | 跨租户隔离活体（A 试图搜 B 的文件）| 权限代码已写但缺对抗测试 | 可在生产做，1d |
| 23 | 弱网测试（100ms RTT + 5% 丢包）| 移动用户体验 | 需 tc netem 环境 |
| 24 | Open WebUI 启动时的全量 reconcile 压力 | adapter 启动时 "deleted 5 mirrors" 出现过 | 低优，ok 做 |

---

## P5 · 代码结构 / Tech Debt（04-20 session code review 发现）

交付后做，不影响当前业务。review 发现按严重度排：

| # | 事项 | 严重度 | 工时 |
|---|---|---|---|
| 29 | **`admin_api.py` 拆分**：1544 行 / 51 route / 9 领域混成一团（users/projects/files/todos/suggestions/knowledge/folders/conversations/stats），拆成 `admin/` 子包（`admin/projects.py` / `admin/files.py` / `admin/todos.py` 等） | 🔴 High | 2-3h + 全回归 |
| 30 | **`routing.py` 职责过重**：PERSONA_TEXT 500 字硬编码 + `suggest_project_knowledge` / `suggest_todo` 两个 Letta 自定义工具定义 + agent 创建逻辑混在一起。拆 `custom_tools.py`（所有 Letta 工具集中）+ `personas/default.txt`（persona 外置） | 🔴 High | 1.5h + 回归 |
| 31 | **反向依赖消除**：`letta_sql_tools.py::should_attach_sql_tools` 调 `sql_endpoints._load_allowlist`（工具层反向依赖 endpoint 层）；`responses_endpoints.py::v2_models` 从 `main` import `list_models`（router 依赖 app）。把 `_load_allowlist` 移 `table_ingest.py`；`list_models` 提 `models.py` | 🟡 Medium | 30min |
| 32 | **`_pretty_tool` / `_pretty_return` 在 main.py 和 responses_endpoints.py 各一份**，重复代码。提到 `common.py::format_tool_call / format_tool_return` | 🟡 Medium | 15min |
| 33 | **test 脚本散落在 `/tmp/`**（本 session 有 10+ 个 `/tmp/test_*.py`：SQL / stop_reason / pptx / orphan audit / biany_walkthrough 等），重启就丢。迁到 `adapter/scripts/tests/` 纳入版本 | 🟡 Medium | 30min |
| 34 | **`OPENWEBUI_JWT_SECRET` fallback `6WYGSa8e7EBsSeG3` 硬编码在 4 处**（auth.py / regression.py / bench 脚本）。统一到 `config.py` | 🟢 Low | 15min |
| 35 | **`scripts/update_persona_for_sql.py` 名字误导**：它刷的是整个 persona 新版（不只 SQL 部分）。改名 `refresh_all_agent_personas.py` | 🟢 Low | 5min |
| 36 | **`bench_mixed_100.py` 本地有服务器 scripts/ 里缺**（04-20 scp 才补上）。查一下 git 同步哪里漏了 | 🟢 Low | 15min |

**推荐做法**：等下个迭代有 1-2 天空档集中做 #29/#30（最大收益），#31~#36 可以拆成若干 PR 滚动做。不要在业务高峰期做 #29/#30（会大量文件移动，PR 很难 review）。

---

## 今晚新发现的小事（已做完或可忽略）

| # | 事项 | 状态 |
|---|---|---|
| A | vLLM prefix caching 无效果 | 已验证，**不指望**靠统一 system prompt 省 TTFT |
| B | letta `_GLOBAL_EMBEDDING_SEMAPHORE = 3` 改 10 只 +15% | 已验证，回滚，不值得动 |
| C | letta `docker kill` 不触发 restart | 已验证是 Docker 预期行为（真崩溃会触发），不是 bug |
| D | 长对话 agent TTFT 基本不变（477 vs 18 消息）| ✅ Letta in-context buffer 有效 |
| E | 慢客户端 stream 不拖累快客户端 | ✅ adapter async 完美 |
| F | sustained 30min 零漏水 | ✅ letta 内存 7.18 → 7.17 GiB |
| G | core_memory_append 重复追加 | ✅ 已 patch 去重上线 |

---

## 下周头等（04-19 晚大批完成后重排）

**04-19 晚已完成**：P0 #1/#2、P1 #4+#4a、P1 #5、P1 #6（跳过）、P1 #6a、P1 #7（不需改）。

**剩下优先级**：

1. **P2 #8 每日 cron + 飞书告警**（2h）—— 零代码成本，先知一步发现问题
2. **P0 #3 user_id 显示** —— 待用户明确场景
3. **P4 #22 跨租户隔离活体**（1d）—— 权限层代码已写但缺对抗测试
4. **P2 #9-12 运维基础设施**（度量/备份/staging）—— 长期
5. **P3 产品功能**（对话记忆 P1/P2 / 项目巡检 V0 / 聊天搜索 / 图片 OCR）—— 产品侧决定

**性能路径上已到极限**：
- vLLM 200 并发（临港网关放开后）
- 上传 10 ops/s（Ollama GPU + async + Letta 4w + embedding semaphore）
- 对话清空 5 人 2s（gunicorn + async 并行）
- 管理 API C=100 p99 1.7s（aiosqlite 证明这是 Python CPU 极限，不是 I/O 瓶颈）
再提性能只能：**加物理 CPU 核 / 换语言（Rust/Go）/ 减少 endpoint 工作量**（缓存 me 响应等）

---

## 🆕 P0 · Phase 5: WebUI 原生 knowledge 上传的 scope 弹窗（2026-04-20 立项）

### 背景

Phase 1-4 完成后，用户通过 **adapter admin dashboard** 上传 project 文件走通（reconcile_mirrors 自动建 # 下拉镜像）。但用户直接通过 **Open WebUI 原生 Knowledge UI** 上传的文件**不会自动被归到正确的 scope**：
- 文件进 `webui.file` 表 ✅
- 文件不进 Letta folder ❌
- reconcile_mirrors 不建镜像 ❌
- # 下拉看不到 ❌

**问题**：biany 04-20 事故的复现路径。现在 biany 的 4 份 PDF 是通过 `scripts/phase2_backfill_webui.py` 手动补的，下次她再传新文件又会落空。

### 目标

用户在 WebUI Knowledge UI 上传文件时，弹一个 modal 问 scope：
```
┌──────────────────────────────────────┐
│ 上传 "xxx.pdf"                        │
│                                      │
│ 分享范围：                            │
│   ⦿ Asset Management (当前项目) ← 默认  │
│   ○ 只给自己（个人）                  │
│   ○ 组织共享（admin 限定）            │
│                                      │
│ [取消]              [确认上传]        │
└──────────────────────────────────────┘
```

确认后 adapter 接管上传，落 `/data/serving/adapter/projects/<slug>/`，自动建 mirror + DuckDB ingest + project_files 行。**用户零额外步骤**（除了每次多 1 秒确认 scope）。

### 架构

```
用户点 [+] 上传
     ↓
AddContentMenu → onUpload → 【新弹窗 ScopePickerModal】
     ↓ 用户选 scope
file blob + scope + project_id (从当前 URL / context)
     ↓
POST /admin/api/upload-with-scope   ← adapter 新 endpoint
     ↓ adapter:
     1) Save binary to /data/open-webui/uploads/ (走 WebUI Phase 1 兼容)
     2) 同时 os.link hardlink 到 /data/serving/adapter/projects/<slug>/
     3) file_processor / Letta pg fallback 生成 .md 派生
     4) project_files 行 source='current' webui_file_id
     5) xlsx/csv → table_ingest
     6) 建 WebUI knowledge_file link (让 # 下拉能找到)
     ↓
UI 刷新文件列表
```

### 代码位置（server: `/home/infra46/open-webui-custom/`）

**Svelte 改动（3 个文件）**：
1. `src/lib/components/workspace/Knowledge/KnowledgeBase/AddContentMenu.svelte`
   - 现状：点"Upload files"直接触发 onUpload
   - 改为：先显示 ScopePickerModal，用户确认后再 onUpload（携带 scope）
2. `src/lib/components/workspace/Knowledge/KnowledgeBase/ScopePickerModal.svelte`（新）
   - Radio 组 3 个选项（project 默认 / personal / org）
   - 从 `$page.url` 或 context 读取当前 project slug 作默认
   - Confirm / Cancel 按钮
3. `src/lib/components/workspace/Knowledge/KnowledgeBase/Files.svelte`
   - 找 uploadFileHandler 函数（约 line N，需要 grep 确认）
   - 把 POST `/api/v1/files/` + `/knowledge/{kid}/file/add` 替换为 POST `/admin/api/upload-with-scope`（scope 从 modal 传进来）

**Adapter 改动（2 个文件）**：
1. `adapter/admin_api.py` 新加 endpoint：
```python
@router.post("/upload-with-scope")
async def upload_with_scope(
    file: UploadFile, scope: str = Form(...), scope_id: str = Form(""), 
    user: dict = Depends(get_current_user)
):
    # 1. check permission (project member / self / admin)
    # 2. save to webui uploads (hardlink possible)
    # 3. 调 kb.ingest.ingest_webui_file(file_id, scope, scope_id, user.id)
    # 4. 建 knowledge_file entry for # 下拉
    # 5. return {file_id, display_name}
```
2. `adapter/kb/ingest.py` 复用（已有 ingest_webui_file）

### 构建 + 部署流程

```bash
# 1. 改 Svelte 代码
cd /home/infra46/open-webui-custom
vi src/lib/components/workspace/Knowledge/KnowledgeBase/AddContentMenu.svelte
# 创建 ScopePickerModal.svelte
vi src/lib/components/workspace/Knowledge/KnowledgeBase/Files.svelte

# 2. 构建新镜像 (耗时 ~5-10 min, 需要 docker build 依赖 node_modules)
docker build -t open-webui-custom:v0.8.12-popup .

# 3. 停现有容器 + 起新的
docker stop open-webui
docker rm open-webui
docker run -d --name open-webui \
  --network teleai-adapter_default \
  -v open-webui-data:/app/backend/data \
  -p 3000:8080 \
  -e WEBUI_SECRET_KEY=... \
  open-webui-custom:v0.8.12-popup

# 4. 验证: 
#    - 打开 http://localhost:3000
#    - Knowledge UI 点上传，应该弹窗
#    - 选不同 scope 上传, 确认落到不同目录
#    - # 下拉能看到

# 5. Rollback: 切回旧镜像
docker stop open-webui && docker rm open-webui
docker run -d --name open-webui ... open-webui-custom:latest  # 旧 tag
```

### 验收

| 测试 | 期望 |
|---|---|
| 在 Asset Management 项目点 [+] 上传 xxx.pdf | 弹窗默认勾 "Asset Management" |
| 选 "Asset Management" 点确认 | 文件落 `/data/serving/adapter/projects/asset-management/xxx.pdf` + `.pdf.md` |
| # 下拉 5min 内出现 `[Asset Management] xxx.pdf` | ✅ |
| 改选 "只给自己" | 文件落 `/data/serving/adapter/projects/.personal/<uuid>/xxx.pdf` |
| 非 project 成员不能选该 project | 下拉只显示该用户有权限的 project |
| xlsx 文件选 project → DuckDB ingest | `query_table` 能查到表 |

### 预计工时

- Svelte 开发：0.5 天（读 Files.svelte 确认 upload 钩子 + 写 modal + 集成）
- Adapter endpoint：0.5 天（copy + permission check + 复用 ingest_webui_file）
- Rebuild + 部署 + 回归：0.5 天
- **合计：1.5-2 天**

### 前置依赖

- 服务器 `/home/infra46/open-webui-custom/` Svelte 源码 ✅（已确认存在）
- node / npm 装好可 `docker build` ✅（之前有人 build 过 open-webui-custom:latest）
- adapter kb/ingest.py 已有 ingest_webui_file ✅

### 风险

| 风险 | 缓解 |
|---|---|
| Svelte 改坏 WebUI 构建失败 | 先在 dev tag 构建, 验证通过再覆盖 latest |
| 新镜像起来但前端 hydrate 错 | 保留旧镜像 tag, 一条 docker 命令 rollback |
| 用户上传时 adapter endpoint 挂 | 保留 WebUI 原生路径作为 fallback（用户手动切换） |
| 权限 check 遗漏让非成员上传到 project | 单元测试覆盖 + 后端 enforce |

---

## 📝 关联：feat/kb-poc merge main 的 cherry-pick 选择（2026-04-20 分析）

origin/main 的 12 commits 并非全需合。分析结果：

**必合（5 条，真有价值）**：
- `d28f74a` core_memory_append 行级去重 Letta patch
- `889107b` 里 `scripts/sync_agent_endpoints.py` (vLLM endpoint 轮换必需)
- `4075fdc` P0+P1 批量修复（需 review diff 再决定）
- 7 个 bench/test 脚本（无生产风险，压测资产）

**可不合（2 条）**：
- `58b0ddd` aiosqlite — 实测 0 收益（见 `project_capacity_ceilings.md` §2）
- `ae51535` Knowledge 徽章 UI — 价值降低（embedding 进度对用户意义弱）

**折衷（1 条）**：
- `6fa1fc6` 上传 perf — admin dashboard 上传路径仍受益，kb/ingest.py 新路径用不上

**工作量**：2-3 小时完成 5 条 cherry-pick + 4 文件冲突解 + regression 验收。

---

## 🆕 P1 · Letta summarizer 兜底: auto_rebuild_overfull_agents.py（2026-04-21 立项）

### 背景

Letta upstream 的 `summarize` API 被调用后**不真压缩 messages**（生成 summary 但不删原始 messages），导致 agent 对话历史累积到 65K 后撞 vLLM 上限，`cur_tokens > 65536` 静默 400。

04-20 asset-management agent `msg_tok=71268` 已撞，手动 rebuild 才修好。其他 28 agent 都在持续累积，迟早撞。

### 目标

adapter 侧加定时任务，扫描所有 agent 的 `cur_tokens`（via Letta `/v1/agents/{id}/context`），超过阈值（默认 `vLLM_max - 5000 = 60000`）→ **自动触发 rebuild**（delete agent + reset user_agent_map），通知用户。

### 关键设计

1. **预警阶段（margin<10K）**: 只发通知"你的对话历史即将重置"，给用户 24-48 小时保存重要记忆到 human block
2. **硬触发（margin<0）**: 直接 rebuild（否则下次聊天必 400）
3. **注意 cascade**: delete agent 会级联删共享 block，**必须先逐 block detach**（已在 `scripts/rebuild_asset_agent.py` 原型验证）
4. **生效机制**: 下次用户打开 chat 触发 `get_or_create_agent` 走 Phase 1 新路径自动新建
5. **保留 human block**: 每 user 的 personal_human_block 是跨 agent 共享的, 不受影响

### 具体落地

```
scripts/auto_rebuild_overfull_agents.py:
  - 每 4 小时跑一次 (crontab 或 _reconcile_loop 内集成)
  - 扫 user_agent_map 所有 agent
  - 调 /v1/agents/{id}/context, 拿 num_tokens_current
  - 如果 > 60000: trigger rebuild
  - log + 可选 push notification

要做的事:
  1. 写脚本
  2. 在 main.py 的 _reconcile_loop 里加定时调用 (或独立 cron)
  3. 预警通知机制 (写用户 WebUI inbox 或邮件, 看要不要做)
  4. 测试 + 加 regression 断言
```

工期: 0.5-1 天

---

## 🆕 P2 · WebUI 对话 UI 按 project 分组（2026-04-21）

### 背景

当前 biany 在 WebUI 看自己所有历史对话时，**4 个 project 的对话混在一个 list**（按时间排序），看不清哪条属于哪个 project。用户直觉感受是"跨项目记忆错乱"（其实 messages 是各 agent 独立的，UI 混在一起展示而已）。

### 目标

WebUI 左栏 "Conversations" 区按 project 分组（或加 project tag），让用户一眼看出"这条对话是在 Asset Management 下发生的"。

### 改动点

Svelte 前端改:
- `src/lib/components/chat/Sidebar.svelte` 或 Conversations 相关组件
- 读 chat metadata 里的 `model` 字段（`letta-asset-management` / `letta-security-management` 等）
- 按 model（即 project）group 展示

工期: 0.5 天（需要 Svelte + webui rebuild + 部署，流程跟 Phase 5 弹窗同）

**建议跟 Phase 5 弹窗一起做**（反正都要 webui rebuild 一次）。

