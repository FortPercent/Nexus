# Agent 直接产生文件能力设计

> 编写: 2026-05-05  状态: design  作者: Claude

## 背景

Nexus 当前是 chat 模式：用户问 → AI 文本答 → 用户复制粘贴到 Word/Excel 自己排版。
政务场景（公文写作、报表生成、汇报材料、纪要整理）需要 **AI 直接产出 docx / xlsx / pdf / pptx 文件**，用户点下载即用。

设计原则：
- **不 walkaround**：不要"AI 输出 markdown 让用户自己转 Word"这种间接方式
- **agent 工具调用直产**：复用 routing.py kb 三件套套路，新增 4 个 create_* 工具
- **走标准链路**：file 落盘 → 写表索引 → 下载链接挂 chat → 用户点击下载

---

## 架构

```
user: "把刚才的会议要点整理成请示报告，发给我"
      ↓
agent (Letta) 决定调用工具
      ↓
create_docx(filename="关于X的请示.docx", template="qingshi", content=...)
      ↓ tool body 内 urllib POST
adapter /internal/agent/{aid}/output-doc
      ↓
1. python-docx 渲染 (markdown → docx，按模板套样式)
2. 写盘 /data/serving/adapter/projects/<slug>/outputs/<uuid>-<filename>
3. INSERT agent_outputs 表
4. 返 file_uuid
      ↓ tool 返回值
"已生成: 关于X的请示.docx [下载: agent-output://abc123]"
      ↓
WebUI Svelte patch 把 `agent-output://abc123` 渲染成下载卡片
      ↓
用户点击 → /admin/api/agent-outputs/{uuid}/download → binary stream
```

---

## 工具定义（4 个）

写在 `routing.py`，套现有 `suggest_project_knowledge` 的 `letta.tools.upsert_from_function` 模式。

### create_docx

```python
def create_docx(
    filename: str,
    content_md: str,
    agent_state: "AgentState",
    template: str = "default"
) -> str:
    """生成 Word 文档 (.docx)。content_md 是 markdown 格式，会自动转换成 Word
    样式（标题/列表/表格/粗体等）。template 可选: default / qingshi (请示) /
    tongzhi (通知) / baogao (报告) / jiyao (纪要)。

    Args:
        filename: 文件名，必须以 .docx 结尾
        content_md: markdown 格式的正文
        template: 模板，缺省 'default' 是无模板纯样式

    Returns:
        生成结果 + 下载链接，形如 "已生成: xxx.docx [下载: agent-output://uuid]"
    """
    # body 内 urllib POST adapter /internal/agent/{aid}/output-doc
    # 参考 suggest_project_knowledge 套路
```

### create_xlsx

```python
def create_xlsx(
    filename: str,
    sheets_json: str,
    agent_state: "AgentState"
) -> str:
    """生成 Excel 表格 (.xlsx)。sheets_json 格式：
    {"sheet1": {"headers": ["列1","列2"], "rows": [["a","b"],["c","d"]]},
     "sheet2": {...}}

    Args:
        filename: 文件名，必须以 .xlsx 结尾
        sheets_json: JSON 字符串，描述多 sheet 数据

    Returns:
        生成结果 + 下载链接
    """
```

### create_pdf

```python
def create_pdf(
    filename: str,
    content_md: str,
    agent_state: "AgentState",
    page_size: str = "A4"
) -> str:
    """生成 PDF 文件。content_md 是 markdown，weasyprint 转 HTML→PDF。
    支持中文（用思源/华文字体）。"""
```

### create_pptx

```python
def create_pptx(
    filename: str,
    slides_json: str,
    agent_state: "AgentState",
    template: str = "default"
) -> str:
    """生成 PPT (.pptx)。slides_json 格式：
    [{"layout":"title","title":"X","subtitle":"Y"},
     {"layout":"bullets","title":"X","bullets":["a","b","c"]},
     {"layout":"image","title":"X","image_url":"..."}]
    """
```

---

## adapter Sidecar Endpoint

`adapter/agent_output_api.py`（新文件）。

```
POST /internal/agent/{agent_id}/output-doc
  body: {filename, format, content_md|sheets_json|slides_json, template?}
  -> {file_uuid, file_path, file_size, download_url}

GET  /admin/api/agent-outputs/{file_uuid}/download
  -> binary stream (Content-Disposition: attachment; filename=...)
  权限: file_uuid 关联的 user/project 成员才能下载

GET  /admin/api/agent-outputs?project_id=&user_id=&agent_id=&from=&to=
  -> 列表（filename, format, agent, project, created_at, size）

DELETE /admin/api/agent-outputs/{file_uuid}
  -> soft delete (deleted_at 写时间)，盘文件 7 天后清理（cron）
```

主要逻辑：
1. 校 agent_id 存在 + 取 metadata (project_id, user_id)
2. 路由到 format-specific renderer
3. 落盘 + 写表
4. 返下载链接

---

## 数据 Schema

```sql
CREATE TABLE agent_outputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_uuid TEXT UNIQUE NOT NULL,             -- 短 UUID 12 字
    filename TEXT NOT NULL,
    format TEXT NOT NULL,                       -- docx/xlsx/pdf/pptx
    file_path TEXT NOT NULL,                    -- 盘上绝对路径
    file_size INTEGER NOT NULL,
    template TEXT DEFAULT 'default',
    agent_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    project_id TEXT,                            -- letta-* 才有
    chat_id TEXT,                               -- 关联 webui.chat 表
    message_id TEXT,                            -- 关联 webui.message 表
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    deleted_at TIMESTAMP                        -- soft delete
);

CREATE INDEX idx_agout_user_ts  ON agent_outputs(user_id, created_at DESC);
CREATE INDEX idx_agout_project  ON agent_outputs(project_id, created_at DESC);
CREATE INDEX idx_agout_chat     ON agent_outputs(chat_id);
CREATE INDEX idx_agout_active   ON agent_outputs(deleted_at) WHERE deleted_at IS NULL;
```

---

## 文件存储路径

```
/data/serving/adapter/projects/<slug>/outputs/<uuid>-<filename>
/data/serving/adapter/projects/.personal/<uid>/outputs/<uuid>-<filename>
```

复用现有 adapter_data volume。

为什么 uuid 前缀：避免 filename 撞名 + 让 path 不可猜（防越权枚举）。

---

## 渲染实现

### docx：python-docx + mistune

```python
def render_docx(content_md: str, template: str) -> bytes:
    base_doc = load_template(f"templates/{template}.docx")  # 政务模板预设样式
    md_ast = mistune.create_markdown(renderer="ast")(content_md)
    for node in md_ast:
        if node["type"] == "heading":
            base_doc.add_heading(extract_text(node), level=node["attrs"]["level"])
        elif node["type"] == "paragraph":
            base_doc.add_paragraph(extract_text(node))
        elif node["type"] == "table":
            render_table(base_doc, node)
        # ... list / bold / image / etc.
    buf = BytesIO()
    base_doc.save(buf)
    return buf.getvalue()
```

### xlsx：openpyxl 直接构

```python
def render_xlsx(sheets_json: str) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    sheets = json.loads(sheets_json)
    for name, data in sheets.items():
        ws = wb.create_sheet(name)
        ws.append(data["headers"])
        for row in data["rows"]:
            ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
```

### pdf：weasyprint

```python
def render_pdf(content_md: str, page_size: str) -> bytes:
    html = mistune.html(content_md)
    css = f"@page {{ size: {page_size}; margin: 2cm }} body {{ font-family: 思源宋体 }}"
    return HTML(string=html).write_pdf(stylesheets=[CSS(string=css)])
```

中文字体：Dockerfile 装 `fonts-noto-cjk` (Linux 自带源开箱即用)。

### pptx：python-pptx + 模板

```python
def render_pptx(slides_json: str, template: str) -> bytes:
    prs = Presentation(f"templates/{template}.pptx")
    for slide_def in json.loads(slides_json):
        layout = prs.slide_layouts[LAYOUT_MAP[slide_def["layout"]]]
        slide = prs.slides.add_slide(layout)
        # 按 layout 填 placeholder
    buf = BytesIO()
    prs.save(buf)
    return buf.getvalue()
```

---

## 政务公文模板

`adapter/templates/` 目录预置：

| 模板 | 用途 | 关键样式 |
|---|---|---|
| `default.docx` | 通用 | 宋体小四、无页眉 |
| `qingshi.docx` | 请示 | 红头、机关字号、签发栏 |
| `tongzhi.docx` | 通知 | 红头、文号、附件标记 |
| `baogao.docx` | 报告 | 正文格式、目录、页码 |
| `jiyao.docx` | 会议纪要 | 时间地点列表、议题决定行动项 |

模板做法：用一个标准 docx 当 base，python-docx 加载后填正文，标题/页眉/字体已经在模板里。

工时：4 个模板 × 0.5d = 2d（含找参考样例 + 调字体页边距）。

---

## WebUI 集成（Svelte 渲染下载卡片）

### 当前 assistant message 显示

工具调用返回 `"已生成: xxx.docx [下载: agent-output://abc123]"`，这段会出现在 message 内容里。

### 改造目标

把 `agent-output://abc123` 模式自动识别，渲染成卡片：

```
┌─────────────────────────────────────────┐
│  📄 关于 X 的请示.docx                    │
│  Word 文档 · 18.4 KB · 刚刚生成            │
│  [下载]  [预览]  [发到云空间]              │
└─────────────────────────────────────────┘
```

### 实现

`web/src/lib/components/chat/Messages/Markdown/MarkdownTokens.svelte`（已有 markdown 渲染）加一段链接预处理：

```js
// 检测 [文本](agent-output://uuid) → 转成 OutputCard 组件
if (link.url.startsWith("agent-output://")) {
    return { type: "output_card", uuid: link.url.slice(17), text: link.text };
}
```

新组件 `OutputCard.svelte`：
- fetch `/admin/api/agent-outputs/{uuid}` 拿 metadata
- 渲染图标 + 文件名 + 大小
- 下载按钮直链 `/admin/api/agent-outputs/{uuid}/download`

工时：1-2d（Svelte + WebUI rebuild + 部署，套路同 Phase 5b）。

---

## admin Outputs Tab

admin-dashboard 加 "Outputs" tab：
- 列表：filename / agent / project / user / created_at / size / format
- filter: project / user / format / 时间范围
- 操作：下载 / 重命名 / 删除
- 容量统计：用户/项目维度的输出文件总大小，配合配额管理

工时：1-2d。

---

## 权限 + 安全

| 检查 | 实现 |
|---|---|
| 文件越权下载 | 下载时校验 caller user_id ∈ (output.user_id 自己 / project_members(output.project_id)) |
| 文件大小幻觉（LLM 写 1GB xlsx） | 渲染端硬限：docx<10MB, xlsx<50MB, pdf<20MB, pptx<30MB；超额 raise 给 agent |
| 文件名注入（`../etc/passwd`） | os.path.basename + 白名单字符 [\w.一-鿿-]，不合规 raise |
| 模板路径注入 | template 参数白名单（5 个模板名常量） |
| 跨用户枚举 | uuid 12 字 + 路径不暴露 + 列表 endpoint 默认按 caller 过滤 |
| 审计 | 每次生成/下载/删除写 `audit_log` |

---

## 测试矩阵

| 用例 | 输入 | 期望 |
|---|---|---|
| T1 docx 生成 | "把刚才的要点写成 .docx" | 文件可下载，打开 Word 正常显示 |
| T2 xlsx 生成 | "Q2 各部门预算列表，导出 Excel" | 多 sheet / 表头 / 数据正确 |
| T3 pdf 生成 | "把这份请示转 PDF" | 中文不乱码，A4 页边距合理 |
| T4 pptx 生成 | "做 5 页汇报 PPT" | 母版样式 + 标题列表 |
| T5 政务模板 | template="qingshi" | 红头 + 文号位 + 签发栏 |
| T6 大文件防御 | LLM 幻觉吐 100MB content | 渲染端 raise，agent 收到错误 retry |
| T7 文件名注入 | filename="../../etc/passwd" | basename 后落盘合法名 |
| T8 越权下载 | userA 用 userB 的 file_uuid 下载 | 403 |
| T9 软删除 7 天清理 | deleted_at + 7d cron | 盘文件物理删，记录保留 |
| T10 chat history 持久化 | 关 chat 重开，历史下载链接还能点 | OK |

---

## 工时

| 模块 | 工时 |
|---|---|
| schema + sidecar endpoint + 4 个 routing.py 工具 | 3d |
| 4 个 renderer (docx/xlsx/pdf/pptx) + mistune 转换 | 2d |
| 政务公文 4 类模板 (default + 4 政务) | 2d |
| WebUI Svelte 下载卡片渲染 + rebuild + 部署 | 1-2d |
| admin Outputs tab | 1-2d |
| 权限校验 + 容量限制 + 文件名净化 + 审计 | 1d |
| 测试 + e2e + LLM 行为调优 (persona few-shot) | 2-3d |

**总 12-15 工日 ≈ 2.5-3 周（一个工程师）**

---

## 关键风险

| 风险 | 缓解 |
|---|---|
| LLM 不调工具直接吐 markdown | persona few-shot 强化（"用户问'生成 Word'必须用 create_docx 工具"）；监控 metrics 统计调用率 |
| LLM 把整个文档塞 tool args 撑爆 ctx | content_md 内嵌"分块生成"工具：create_docx_open + create_docx_append + create_docx_close |
| 模板维护成本（政务格式变） | 模板版本化，agent_outputs 记 template_version 字段，老模板 keep 旧文件可重渲 |
| Letta tool args size limit | 测下 Letta SDK tool args 长度上限，超额走分块 |
| 中文字体在容器里缺 | Dockerfile 装 fonts-noto-cjk，CI build 时验证 |
| 文件存储无限增长 | 软删除 7d cron + 用户配额 1GB |
| 政务模板法律合规（公文格式国家标准 GB/T 9704-2012） | 找客户拿真实公文样例做 ground truth，不要拍脑袋 |

---

## 决策点（执行前要拍板）

1. **第一版做几种格式**：docx 单一 (MVP, 5d) / docx+xlsx (主流场景, 8d) / 4 全做 (12-15d)？
2. **要不要做政务模板**：不做模板 default 样式 90% 场景能用；做模板更"政务感"但加 2-3d
3. **WebUI 下载卡片是否必做**：不做也可以让用户点链接下载（体验差）；做要 Svelte rebuild 1-2d
4. **存储位置**：project outputs 共享给项目成员可见 / personal outputs 仅自己 / 是否允许"晋升"（personal → project）
5. **下载授权深度**：链接含签名 token (HMAC) 防重放 / 还是 session cookie 即可

---

## 推荐执行顺序

```
Day 1-2: schema + sidecar endpoint 骨架 + create_docx 工具 + default 模板
Day 3:   验证端到端（chat 命令 → 工具调用 → 文件下载）
Day 4-5: create_xlsx / create_pdf 加上
Day 6-7: 政务 4 模板
Day 8-9: WebUI 下载卡片
Day 10:  admin Outputs tab
Day 11-12: 安全加固 + 测试
Day 13-15: 联调 + persona 调优 + 文档
```

第一个里程碑（Day 3）能 demo "聊天里说'生成 docx' → 收到下载链接"，之后增量补完整。

---

## 不做的事（留 V2）

- canvas / 实时协同编辑（ChatGPT 风格，需重构 UI）
- 文件预览（在线浏览不下载）
- 批量生成（一次生成 100 份个性化报告）
- 导出已有 chat 历史为文档（这是另一个功能）
- 文件版本管理（同一文件多次 regenerate 时保留历史）
- 与飞书/钉钉/OneDrive 云空间集成（可作 V2 加发到云盘按钮）

---

## 与其它设计的协同

- **#13 driving 看板**：metrics 表加 `agent_outputs_created_count` 维度，看哪些 agent 高频产出
- **#14 多组织树**：output 权限按 project_orgs 递归继承（市城运成员能看市城运下属处室的 outputs）
- **multimodal-passthrough v2**：未来 create_pptx 支持嵌入用户上传的图（agent 引用 image_url 直接放进 PPT）
- **Nexus 2.0 治理**：agent_outputs 视为新一类 memory_history 事件，纳入 trace
