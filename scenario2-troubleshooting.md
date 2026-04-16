# 场景 2 排查指南：加入项目后非 admin 看不到模型

> 更新时间: 2026-04-16
> 适用版本: V1（SQLite 直连）

## 症状

- admin 账号（如 wuxn5）能看到所有模型
- 非 admin 成员加入项目后，刷新/重登 Open WebUI，模型下拉框里没有 `AI 助手 (项目名)`

## 排查链路

### Step 1: access_grant 表有没有写入

```bash
docker exec teleai-adapter python3 -c "
import sqlite3
conn = sqlite3.connect('/data/open-webui/webui.db', timeout=5)
rows = conn.execute(
    'SELECT resource_id, principal_id FROM access_grant WHERE resource_id LIKE ?',
    ('letta-%',)
).fetchall()
for r in rows:
    print(r[0] + ' -> ' + r[1])
if not rows:
    print('(empty)')
conn.close()
"
```

- 有记录 → **Step 2**
- 没记录 → **Step 4**

### Step 2: model 表有没有注册（最常见根因）

```bash
docker exec teleai-adapter python3 -c "
import sqlite3
conn = sqlite3.connect('/data/open-webui/webui.db', timeout=5)
conn.row_factory = sqlite3.Row
models = conn.execute('SELECT id, name FROM model').fetchall()
for m in models:
    print(m['id'] + ' | ' + m['name'])
conn.close()
"
```

**关键判断**：
- 如果只有 `qwen-no-mem`，没有 `letta-*` → **这就是根因**
- Open WebUI 只对 `model` 表中注册的模型检查 `access_grant`
- 未注册的模型是"连接模型"，仅 admin 可见，`access_grant` 写了也没用

**修复**：确认 `webui_sync.py` 的 `reconcile_project_model` 包含 `_ensure_model_registered` 调用，然后手动触发对账：

```bash
docker exec teleai-adapter python3 -c "
from webui_sync import reconcile_all
reconcile_all()
print('done')
"
```

### Step 3: user_id 是否匹配

```bash
# Open WebUI 用户 ID
docker exec teleai-adapter python3 -c "
import sqlite3
conn = sqlite3.connect('/data/open-webui/webui.db', timeout=5)
conn.row_factory = sqlite3.Row
for r in conn.execute('SELECT id, email FROM user').fetchall():
    print(r['id'] + ' | ' + r['email'])
conn.close()
"

# 适配层 project_members 里的 user_id
docker exec teleai-adapter python3 -c "
from db import get_db
db = get_db()
for r in db.execute('SELECT user_id, project_id FROM project_members').fetchall():
    print(r['user_id'] + ' | ' + r['project_id'])
db.close()
"
```

两边 user_id 格式不一致 → 授权无效。

### Step 4: grant_model_access 写入失败

查适配层日志：

```bash
docker logs teleai-adapter 2>&1 | grep -i "grant failed\|sync\|error"
```

手动测试写入：

```bash
docker exec teleai-adapter python3 -c "
from webui_sync import grant_model_access
try:
    grant_model_access('test-user-id', 'letta-test')
    print('SUCCESS')
except Exception as e:
    print('FAILED: ' + str(e))
"
```

常见失败原因：
- `database is locked` → SQLite 并发锁超时
- `no such table` → Open WebUI 版本升级改了表结构
- 文件不存在 → WEBUI_DB_PATH 配置错误或 volume 未挂载

### Step 5: 手动触发全量对账

```bash
# 方式 1: 容器内执行
docker exec teleai-adapter python3 -c "
from webui_sync import reconcile_all
reconcile_all()
print('done')
"

# 方式 2: API 调用（需要 org admin 的 JWT）
curl -X POST http://localhost:9800/admin/api/reconcile \
  -H "Authorization: Bearer <JWT>"
```

## 根因总结（2026-04-16 实际排查）

| 排查方向 | 结果 |
|---------|------|
| access_grant 写入 | 正常，4 条记录 |
| SQLite 路径/锁/权限 | 正常 |
| **model 表注册** | **缺失 — 根因** |

Open WebUI 的模型可见性有两层：
1. `model` 表：模型是否"存在"于 Open WebUI 的管理体系
2. `access_grant` 表：已注册模型对哪些用户可见

只写 `access_grant` 不往 `model` 表注册 → 模型对 Open WebUI 来说不存在 → 非 admin 看不到。
