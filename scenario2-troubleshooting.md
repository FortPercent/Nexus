# 场景 2 排查指南：加入项目后看不到模型

> 编写时间: 2026-04-15
> 问题描述: 项目 admin 添加成员后，成员刷新/重登 Open WebUI，模型下拉框里没有新增的 "AI 助手 (项目名)"

## 一、背景

### `grant_model_access` 的作用

Open WebUI 用 SQLite 中的 `access_grant` 表控制模型对用户的可见性：

| access_grant 状态 | 谁能看到模型 |
|-------------------|-------------|
| 模型有授权记录 | 只有被授权的用户 |
| 模型没有任何记录 | **只有 admin**（非 admin 看不到！） |

**当添加成员时**，`add_member` API 调用 `grant_model_access(user_id, model_id)` 插入 `access_grant` 记录。如果插入失败，用户就看不到模型。

### 当前代码的隐患

```python
# admin_api.py - add_member()
try:
    grant_model_access(new_user_id, f"letta-{project_id}")
except Exception as e:
    logging.warning(f"sync grant failed for add_member {new_user_id} to {project_id}: {e}")
```

**失败只打 warning，不阻塞添加成员** → 成员添加成功但看不到模型，且前端无报错提示。

---

## 二、排查步骤

### Step 1: 查看适配层日志，有没有 grant 失败

```bash
docker logs teleai-adapter 2>&1 | grep -i "grant failed\|sync"
```

**期望结果：**
- ✅ 无 warning → 说明 `grant_model_access` 执行成功，继续排查 Step 2
- ❌ 有 `sync grant failed` → 说明写入失败，跳到 **Step 4: 定位失败原因**

**常见失败原因：**
- `database is locked` → SQLite 并发锁超时（Open WebUI 正在写）
- `no such table: access_grant` → Open WebUI 版本升级改了表结构
- `WEBUI_DB_PATH 路径不对` → 容器没 mount 到 Open WebUI 的 volume

---

### Step 2: 确认 access_grant 表里有没有记录

```bash
docker exec open-webui sqlite3 /app/backend/data/webui.db \
  "SELECT id, resource_id, principal_id, permission, created_at FROM access_grant WHERE resource_id LIKE 'letta-%' ORDER BY created_at DESC LIMIT 10;"
```

**期望结果：**
- ✅ 能看到 `letta-{project_id}` 的记录，且 `principal_id` 是被添加用户的 ID
- ❌ 没有记录 → `grant_model_access` 没写入成功，跳到 **Step 4**

---

### Step 3: 确认 user_id 是否匹配

适配层写入的 `user_id` 必须和 Open WebUI 内部的用户 ID **完全一致**，否则授权无效。

#### 3.1 查看 Open WebUI 的用户 ID 格式

```bash
docker exec open-webui sqlite3 /app/backend/data/webui.db \
  "SELECT id, email, name FROM user LIMIT 5;"
```

#### 3.2 查看适配层 project_members 表里的 user_id 格式

```bash
docker exec teleai-adapter sqlite3 /data/serving/adapter/adapter.db \
  "SELECT user_id, project_id, role FROM project_members LIMIT 5;"
```

#### 3.3 对比

- 如果两边的 user_id **格式不同**（例如一个是 UUID，一个是邮箱），说明 **ID 映射有问题**
- 如果两边的 user_id **完全一致** → 继续排查 Step 4

---

### Step 4: 定位 grant 失败原因

#### 4.1 检查 WEBUI_DB_PATH 配置

```bash
docker exec teleai-adapter env | grep WEBUI_DB_PATH
```

**期望值：** `/data/open-webui/webui.db`

如果不对，检查 `.env` 和 `docker-compose.yml` 里是否 mount 了 `open-webui-data` volume。

#### 4.2 检查 Open WebUI volume 是否 mount 到适配层

```bash
docker inspect teleai-adapter | grep -A 5 "Mounts"
```

**期望结果：** 能看到 `open-webui-data` 或类似 volume mount 到 `/data/open-webui`。

#### 4.3 手动测试写入

进入适配层容器，手动执行 Python 测试：

```bash
docker exec -it teleai-adapter python3 -c "
from webui_sync import grant_model_access
try:
    grant_model_access('测试user_id', 'letta-测试project')
    print('SUCCESS')
except Exception as e:
    print(f'FAILED: {e}')
"
```

**如果失败** → 输出会直接告诉你原因（数据库路径、表不存在、锁超时等）。

---

### Step 5: 检查 Open WebUI 版本是否升级

```bash
docker exec open-webui python3 -c "import webui; print(webui.__version__)" 2>/dev/null || \
docker exec open-webui cat /app/package.json | grep version
```

**如果 Open WebUI 版本变了**，`access_grant` 表结构可能变了，需要回归测试。

---

## 三、快速修复

### 手动触发全量对账

如果确认是 `access_grant` 缺失，可以手动触发全量对账修复：

```bash
# 获取 admin JWT token
ADMIN_JWT=$(curl -s http://localhost:8642/admin/api/me \
  -H "Authorization: Bearer <你的JWT>" | jq -r .id)

# 触发对账（会同步所有项目模型的 access_grant）
curl -s -X POST http://localhost:8642/admin/api/reconcile \
  -H "Authorization: Bearer $ADMIN_JWT"
```

### 直接在 Open WebUI 数据库插入（应急）

```bash
# 进入 Open WebUI 容器
docker exec -it open-webui sqlite3 /app/backend/data/webui.db

# 插入授权记录（替换 YOUR_USER_ID 和 YOUR_PROJECT_ID）
INSERT INTO access_grant (id, resource_type, resource_id, principal_type, principal_id, permission, created_at)
VALUES ('manual-fix-001', 'model', 'letta-YOUR_PROJECT_ID', 'user', 'YOUR_USER_ID', 'read', strftime('%s','now'));

# 验证
SELECT * FROM access_grant WHERE resource_id = 'letta-YOUR_PROJECT_ID';
```

---

## 四、根因分析总结

| 可能原因 | 概率 | 排查方法 | 修复方式 |
|---------|------|---------|---------|
| `grant_model_access` 写入失败 | **高** | Step 1 日志、Step 4 手动测试 | 修复 DB 路径/权限/锁问题 |
| `user_id` 格式不匹配 | 中 | Step 3 对比两边 user_id | 统一 ID 来源 |
| Open WebUI 版本升级改了表结构 | 低 | Step 5 查版本 | 适配 `access_grant` 新结构 |
| 模型本身未在 Open WebUI 注册 | 低 | 检查 `/v1/models` 返回 | 确保适配层 `/v1/models` 正常 |

---

## 五、后续改进建议

1. **失败应报错** → `add_member` 中 `grant_model_access` 失败应上抛错误，让前端提示用户
2. **添加健康检查** → 定期检测 `access_grant` 表和 `project_members` 表的一致性
3. **减少对 Open WebUI 私有 DB 的依赖** → 长期方案是等 Open WebUI 官方支持外部模型权限 API
