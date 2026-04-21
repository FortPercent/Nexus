# Phase 5: 上传统一路径 + scope 弹窗（2026-04-21）

## 背景

biany 事故暴露三条上传路径各走各的，**没有一条**同时把文件落到所有下游：

| 路径 | Letta folder | # mirror | 盘层 `projects/<slug>/` | DuckDB | project_files | **agent kb 工具可见** |
|---|---|---|---|---|---|---|
| WebUI Knowledge UI 原生 | ❌ | 手动补 | ❌ | ❌ | ❌ | ❌ |
| adapter admin 上传 | ✅ | ✅ | **❌** | ✅ | **❌** | **❌** |
| `phase2_backfill_webui.py` | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ |

目标：**合并成一条路径**，任何上传都同时落 5 个下游 → agent 看得到、UI 看得到、DuckDB 有、mirror 能 # 引用。

## 方案总览

```
用户点 WebUI Knowledge UI [+] 上传
  → 弹窗 ScopePickerModal 选 scope (project / personal / org)
  → POST /admin/api/upload-with-scope (file + scope + scope_id)
     │
     └─ adapter._process_and_upload (统一写入函数, 改造后落 5 个下游):
          ├─ 1. 盘层落地: projects/<slug>/<binary> + <binary>.md 派生
          ├─ 2. Letta folder upload (原 # 引用兼容)
          ├─ 3. knowledge_mirror 建条目 (# 下拉)
          ├─ 4. project_files INSERT OR REPLACE (索引)
          └─ 5. xlsx/csv → DuckDB ingest
```

adapter admin dashboard 上传自动享受同样路径（现有 endpoint 也走 `_process_and_upload`）。

## 分阶段实施

### Phase 5a · adapter 侧核心改造（今晚 2-3h）

**5a-1 `_process_and_upload` 加盘层 + project_files**（核心改动，~1h）
- 在 `for letta_name, content, mime in processed:` 循环里每次：
  - 算目标目录 `kb/ingest.py::_target_dir(scope, scope_id or owner_id)`
  - `os.makedirs(target_dir, exist_ok=True)`
  - 写原 binary 到 `<target>/<original_filename>`（只在第一个 processed 文件时写，zip 展开后每个 processed 都独立写就行）
  - 写 .md 派生到 `<target>/<letta_name>`（如果 letta_name 以 .md 结尾）
  - `project_files INSERT OR REPLACE (project_id, scope, scope_id, file_name, display_name, source='current', size_bytes, webui_file_id='', uploaded_by)`
- 失败不阻塞（log warning），保持"即使盘写失败，Letta 上传仍完成"语义
- 对 zip 展开后的多文件逐个写

**5a-2 新 endpoint `/admin/api/upload-with-scope`**（~30min，已起草）
- 已加 Form 参数 scope + scope_id
- project → require_project_member
- personal → extract_user_from_admin + owner_id = user.id
- org → require_org_admin
- 全部走 `_process_and_upload`

**5a-3 测试**（~30min）
- unit: 对 `_process_and_upload` mock file_processor + letta_async + 断言 project_files + 盘文件路径
- integration (server): curl `/upload-with-scope` 三种 scope 各跑一次，ls 盘 + 查 project_files + 查 Letta folder + 查 mirror 全 ✓
- regression 36/36 仍 PASS

**5a-4 部署**
- git push → server git pull → docker compose up -d --build adapter
- 手动测一份 xlsx 走 project scope → 看 list_project_files 工具能读出来

---

### Phase 5b · WebUI 弹窗（明天 1-1.5 天）

**5b-1 Svelte 源码审计**（~1h）
- `ssh infra46 "find /home/infra46/open-webui-custom/src -name '*.svelte' | xargs grep -l 'POST.*files'"` 找所有上传入口
- 预期：AddContentMenu / Files / Chat drag-drop / KnowledgeBase 里 4-5 个点
- **必须**全改。任一漏网就产生 biany 那种绕路数据

**5b-2 ScopePickerModal.svelte**（~2h）
- 位置 `src/lib/components/workspace/Knowledge/KnowledgeBase/ScopePickerModal.svelte`
- 3 个 radio：project（默认，从 URL 识别当前 project）/ personal / org（仅 admin 可选）
- 确认 / 取消按钮
- 从 `$page.url` + `/admin/api/projects` 拿可选 project 列表
- emit: `{scope, scope_id}` 给父组件

**5b-3 改上传 handler**（~3h）
- AddContentMenu.svelte: 把直接 onUpload 改成先 show modal → 确认后带 scope 调 onUpload
- Files.svelte: `uploadFileHandler` 换目标从 `/api/v1/files/` → `/admin/api/upload-with-scope`（带 scope/scope_id FormData）
- 其他入口：按 5b-1 审计结果逐个改

**5b-4 docker build + 部署**（~1h，含 rebuild 慢）
- `cd /home/infra46/open-webui-custom && docker build -t open-webui-custom:phase5-popup .`
- `docker stop open-webui && docker rm open-webui && docker run -d ... open-webui-custom:phase5-popup`
- 如果 build 失败回滚：`open-webui-custom:latest`（旧 tag 留着）

**5b-5 端到端验收**（~1h）
- biany 账号登录 WebUI
- 传一份 .pdf 到 Asset Management project → 弹窗确认 → adapter 处理 → kb 工具能读出来
- xlsx → DuckDB 能查
- # 引用下拉能找到
- 误选 personal → 落 personal 路径，其他人看不到

---

### Phase 5c · 保险网（可选，观察 1 周决定）

如果 5b 没覆盖完全部 WebUI 上传入口，加 nginx 反代兜底：
- 拦 `POST /api/v1/files/` 转发到 `/admin/api/upload-with-scope`（默认 scope=personal）
- 工期 1-2h
- **先不做**，观察 5b 上线后 1 周是否有文件"消失"（ingest missed）

---

### Phase 5d · 文档 + memory 收尾（~30min）

- 更新 `docs/knowledge-unification-v2.md`：统一路径图
- memory 更新 `project_webui_knowledge_api.md`：弹窗 + 新端点契约
- 更新 `docs/todo-next.md`：Phase 5 画掉 + 如果没做 5c 留 todo

## 决策点需要你确认

1. **`_process_and_upload` 改造**: OK 直接动现有函数吗？（影响所有 admin dashboard 上传，回归风险中）
   - 备选：新写 `_process_and_upload_v2` 只给新端点用，老端点暂不动；缺点是长期维护两套
   - **推荐**: 直接改现有函数，盘层写入失败不阻塞，保持老行为兼容
2. **`quality` 字段**: `project_files.quality` 对新上传写 `'clean'` 就行，老 legacy 文件保留 `'legacy_dirty'` / `'cid_dirty'`
3. **`webui_file_id` 字段**: 新上传路径没有 webui_file_id（绕过 WebUI API 直连 adapter），留空。用 `(project_id, scope, scope_id, file_name)` 作 upsert 主键
4. **5b Svelte 源码写权限**: 明天 docker build WebUI 需要服务器上 `/home/infra46/open-webui-custom/` 的写权限 + node 环境。之前有人 build 过 `open-webui-custom:latest` 所以环境就绪

## 时间预算

- 今晚：Phase 5a 完成 + 测试 + 部署（2-3h）
- 明天：Phase 5b 全流程（1-1.5 天）
- 一周后：决定是否 5c

## 风险

| 风险 | 缓解 |
|---|---|
| `_process_and_upload` 盘写破坏老 admin dashboard 上传 | 失败不阻塞（try/except log），老路径 Letta+mirror 仍工作 |
| Svelte 漏某个上传入口，仍有文件绕过弹窗 | 5a 保证 adapter 侧两条路径 (admin + upload-with-scope) 都统一落盘，UI 漏的是 WebUI 原生 `/api/v1/files/` — 再加 5c 反代或 reconcile 兜底 |
| WebUI docker build 挂 | 保留旧镜像 tag, 1 条命令回滚 |
| 用户在现有 knowledge collection 里加文件（走的不是 AddContentMenu）| 5b-1 审计时要找到那条入口并改 |
