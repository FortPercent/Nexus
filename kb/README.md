# 知识层重构 PoC（v0.6）

> **分支**: `feat/kb-poc`（base: `51d9551` 服务器 snapshot，不 merge main 直到 PoC 结论）
> **范围**: 只做 list + read 两个工具 + 一个隔离 test agent，**不碰任何生产 agent / 生产文件 / 现有工具链**。
> **成败**: 用 3 条 security 规范类问题验收是否命中条款 + agent 主动调用。
> **失败处置**: `letta.agents.delete(test_agent_id)` + `rm -rf /data/serving/adapter/projects/security-management/` + 本分支整体删除。

---

## 1. 背景（为什么要重构）

**Letta 把 folder 的 directory tree 内联到 system prompt**，cpm project 有 51 份 PDF/pptx → directories 53K tokens → agent 地板 58K > compact goal 42K → **compact 跑了也白跑**（`num_tokens_directories` 不可压缩）。

除了 compact 事故，还有：
- **biany 16 份 asset 文件绕过 adapter**（WebUI 原生 knowledge create 旁路），不进 adapter 的 L2 DuckDB
- **同一文件存 3 处**（Letta pg 的 source_passages / WebUI 的 vector db / 可能还有 DuckDB）→ 同步 bug 频发
- **Letta folder 把知识库绑死**：想换掉 Letta 时知识层走不了

## 2. 原始目标（7 条）

1. 根治 cpm compact 事故
2. 修 biany 16 份 ingest 旁路
3. 知识层从 Letta 拆出来（adapter 自己管）
4. Claude Code 式"目录即知识库"
5. memory 继续用 Letta（human/persona block / archival_memory / 对话历史不动）
6. Letta 可替换（知识层剥离后 Letta 接口面缩到 ~3 方法）
7. 不重现 3 份副本的同步 bug

## 3. 核心架构决策

### 3.1 知识库路径 = adapter 独立 volume
```
/data/serving/adapter/projects/<slug>/    ← 挂 adapter_data volume
/data/serving/adapter/projects/.personal/<user_uuid>/
/data/serving/adapter/projects/.org/
```
**不**放在 `/data/open-webui/projects/`（跟 WebUI volume 耦合，换 WebUI 存储会丢知识库）。

代价：跨 volume 不能 hardlink，新上传 binary 需要 copy（磁盘 2×）。配额 5GB/project，总量可控。

### 3.2 存量 vs 新上传 物理隔离
```
/data/serving/adapter/projects/security-management/
  新规范.docx                    ← 新上传 binary（Phase 2 后）
  新规范.docx.md                 ← on-the-fly 转换或 cache
  新xlsx.xlsx
  .legacy/                       ← 存量 backfill 全进这儿
    旧规范.docx.md               ← 从 Letta file_contents.text 导
    旧pdf.pdf.md                 ← 可能有 (cid:XXX) 乱码
    .quality/cid_dirty.list      ← 质量标记
```
**好处**: 清理 legacy 一行命令 `rm -rf <slug>/.legacy/`；主目录永远是新的、干净的。

### 3.3 agent 不挂 folder，走 adapter 工具
Letta agent 挂的是：
- blocks: human + persona（不变）
- tools: list_project_files / read_project_file (PoC v0) + 后续 grep / # 引用 fallback（Phase 1）

不挂任何 folder。`semantic_search_files / grep_files / open_files` 这些 Letta built-in file 工具**不再使用**（folder detach 后它们会失效）。

### 3.4 工具 scope 硬限 `"project"`
persona 明确限定"搜索仅限当前 project"（见 `routing.py:15-23`）。personal / org 必须由用户或 agent 显式传 scope，不做 auto 合并。

## 4. 目录 / 文件布局（本分支 feat/kb-poc）

```
teleai-adapter/                            ← repo 根
  kb/                                      ← 所有 PoC 新代码在此
    README.md                              ← 本文件
    endpoints.py                           ← FastAPI router: /internal/project/{pid}/kb/*
    letta_tools.py                         ← Letta 自定义工具: list_project_files / read_project_file
    backfill.py                            ← 一次性: Letta file_contents.text → 目录 .legacy/*.md
  scripts/
    create_test_agent.py                   ← PoC 创建隔离 test agent（不写 user_agent_map）
  main.py                                  ← 加 1 行 include_router(kb.endpoints.router)
  db.py                                    ← 加 project_files 表 schema（Phase 1 就建，PoC 暂不用）
  # 其他文件不动
```

## 5. PoC（Phase 0）明确**做什么 / 不做什么**

### 做
1. `kb/endpoints.py`：2 个 endpoint
   - `POST /internal/project/{slug}/kb/list-files` → `os.listdir` 扫目录 + `.legacy/` 合并显示 + `_display_name` 复用
   - `POST /internal/project/{slug}/kb/read` → 按 path 读文件（PoC 仅处理 .md，不做 on-the-fly 转换）
2. `kb/letta_tools.py`：照 `letta_sql_tools.py` 结构，2 个 `urllib` 薄壳工具
3. `kb/backfill.py`：一次性脚本，只跑 security-management
   - 查 Letta pg `file_contents.text` + `original_file_name`
   - 写到 `/data/serving/adapter/projects/security-management/.legacy/<name>.md`
4. `scripts/create_test_agent.py`：
   - owner=biany, project=security-management
   - **不写 user_agent_map**（生产入口看不到）
   - `tool_ids=[list_id, read_id]` + **显式 `agents.tools.attach()` 循环**（Letta bug 修正，见 `routing.py:233`）
   - persona 里硬加 "文档类问题必须先 list → read"
5. 发 3 条测试问题验收：
   - Q1: "DLP 安装卸载有什么要求？"
   - Q2: "对外交付时现场设备要注意什么？"
   - Q3: "安全开发规范里密码应用章节说了什么？"

### 验收标准（4 条）
1. agent 首轮主动调 `list_project_files`
2. 随后调 `read_project_file` 且选对文件
3. 答案命中标准条款
4. 未跨 scope（没试图查 personal / org）

### 不做（显式禁止）
- 不碰任何生产 agent
- 不改 nginx
- 不改 knowledge_mirror
- 不改 `main.py:541` 的 # 引用
- 不改 DuckDB / `__nexus_meta`
- 不做 grep / semantic_search
- 不做 Phase 1 的 backfill 全量（只 security-management）
- 不做 WebUI upload 拦截（Phase 2 才做）
- 不做 project_files 表的读依赖（PoC 用目录 scan；表可以建但不依赖）

## 6. Phase 顺序（PoC 通过后）

```
Phase 0 — PoC（本分支当前范围）           半天
Phase 1 — 灭火 & 保功能不退化              1 周
  · backfill 全量（所有 project + personal + org 的 .legacy/）
  · kb/ 工具增补: grep_project_files + main.py:541 # 引用 fallback
  · project_files 表第一天建 + backfill 同步写行
  · 分批生产 agent: detach folder + swap tools
  · 验 cpm agent system prompt 58K → <10K + regression 全过
Phase 2 — 增量闭环                          1 周
  · adapter 拦 WebUI Phase 2 (POST /{kid}/file/add)
  · 新 binary hardlink 不行, copy 到 projects/<slug>/
  · knowledge_mirror key: letta-mirror → nexus-file
Phase 3 — DuckDB 主键迁移                    3 天
  · __nexus_meta 加 webui_file_id + rel_path 列, 双键过渡
Phase 4 — 清理                               一周 later
  · 老 passages.search fallback 删
  · Letta folder 冻结（不写不读）
  · 本 README 内容并入 docs/knowledge-unification-v2.md 的 "v3 修正" 章节
```

## 7. Deferred 决策（PoC 不回答，Phase 1-2 要敲定）

| 决策 | 当前倾向 | 何时必须敲定 |
|---|---|---|
| on-the-fly extraction 的 cache 是 ephemeral / 半持久 / 全持久 | 倾向半持久（grep / semantic 每次冷启动会炸） | Phase 2 上 grep 前 |
| legacy 清理的 UX（用户怎么触发 rm .legacy/） | 倾向 admin 手动 / CLI 脚本 | Phase 4 前 |
| reconcile 逻辑（webui.file ↔ project_files ↔ 目录 三方对账） | 参考 knowledge_mirror.py 改方向 | Phase 2 |
| DuckDB 主键迁移脚本细节 | 双键过渡 + drop 支持两种 key | Phase 3 |

## 8. 对齐原始目标（当前 PoC 范围）

| 目标 | v0.6 PoC 覆盖 | 完整交付 |
|---|---|---|
| 1. 根治 cpm compact | ❌ PoC 不动生产 | Phase 1 |
| 2. 修 ingest 旁路 | ❌ PoC 不动上传路径 | Phase 2 |
| 3. 知识层从 Letta 拆 | ⚠️ 方向验证 | Phase 1-4 |
| 4. Claude Code 目录真相 | ⚠️ PoC 只有 legacy | Phase 2 新上传 |
| 5. memory 继续用 Letta | ✅ 零改动 | 已对齐 |
| 6. Letta 可替换 | ⚠️ 方向验证 | Phase 4 |
| 7. 不重现 3 份副本 | ⚠️ PoC 不触及上传 | Phase 2-4 |

**所以 PoC 不"解决问题"，只"证明方向可行"**。

## 9. 保留现有系统功能的风险点（Phase 1 启动前必须解）

| 功能 | PoC 影响 | Phase 1 必做 |
|---|---|---|
| # 引用 | 不动 | `main.py:541` fallback（优先 read_project_file，老 passages.search 兜底） |
| `semantic_search_files` / `grep_files` | 不动（PoC test agent 没挂 folder, 不相关） | 加 `grep_project_files`；semantic 延后 |
| L2 SQL 工具 | 不动 | Phase 3 双键过渡前不动 |
| 知识镜像 / # 下拉 | 不动 | Phase 2 改方向 |

## 10. 如何跑 PoC（代码写完后）

```bash
# 0. 部署新分支
cd /home/infra46/teleai-adapter
git checkout feat/kb-poc
docker compose build adapter && docker compose up -d adapter

# 1. 跑 backfill（只 security-management）
docker exec teleai-adapter python3 /app/kb/backfill.py --project security-management --dry-run
docker exec teleai-adapter python3 /app/kb/backfill.py --project security-management

# 2. 确认目录内容
docker exec teleai-adapter ls /data/serving/adapter/projects/security-management/.legacy/

# 3. 创建 test agent
docker exec teleai-adapter python3 /app/scripts/create_test_agent.py --owner biany --project security-management

# 4. 手工聊 3 个问题（Letta web UI 或 curl）

# 5. 打分 + 决定进 Phase 1 还是推倒
```

## 11. 如何彻底清理 PoC（失败时）

```bash
# 服务器上
docker exec teleai-adapter python3 -c "
from letta_client import Letta
import os
l = Letta(base_url=os.environ['LETTA_BASE_URL'])
for a in l.agents.list(query_text='test-kb'):
    l.agents.delete(agent_id=a.id)
"
rm -rf /data/serving/adapter/projects/security-management/
cd /home/infra46/teleai-adapter
git checkout main
git branch -D feat/kb-poc
# 生产不受影响
```

---

## 附：文档 drift 约束

这份 README 是 PoC 期间的**唯一**架构参考。**不要**在 `docs/*.md` 里新开文档（会制造三份并行真相）。PoC 结论出来后，这份内容并入 `docs/knowledge-unification-v2.md` 的 "v3 修正" 章节，本文件标记为 archived 并保留 commit history 可追溯。

---

**最后更新**: 2026-04-20
**下一步**: 写 `kb/endpoints.py` + `kb/letta_tools.py` + `kb/backfill.py` + `scripts/create_test_agent.py`
