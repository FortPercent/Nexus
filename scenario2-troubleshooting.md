# 场景 2 排查指南：加入项目后非 admin 看不到模型

> 更新时间: 2026-04-16
> 适用版本: V1（SQLite 直连）/ V2（HTTP API）

## 症状

- admin 账号（如 wuxn5）能看到所有模型
- 非 admin 成员加入项目后，刷新/重登 Open WebUI，模型下拉框里没有 `AI 助手 (项目名)`

## 根因（2026-04-16 实际排查）

Open WebUI 的模型可见性有两层：
1. `model` 表：模型是否"存在"于 Open WebUI 的管理体系
2. `access_grant` 表：已注册模型对哪些用户可见

只写 `access_grant` 不往 `model` 表注册 → 模型对 Open WebUI 来说不存在 → 非 admin 看不到。

---

## V1 排查链路（SQLite 直连，同机部署）

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

---

## V2 排查链路（HTTP API，分离部署）

### Step 1: 适配层能不能访问 Open WebUI

```bash
docker exec teleai-adapter python3 -c "
from webui_sync import _get_admin_token
token = _get_admin_token()
print('token: ' + token[:20] + '...' if token else 'FAILED: no token')
"
```

- 拿到 token → **Step 2**
- `FAILED` → 检查 `OPENWEBUI_URL`、`OPENWEBUI_ADMIN_EMAIL`、`OPENWEBUI_ADMIN_PASSWORD` 配置，以及网络连通性

### Step 2: Open WebUI API 返回的模型有没有 grants

```bash
docker exec teleai-adapter python3 -c "
from webui_sync import _get_model_grants
for mid in ['qwen-no-mem', 'letta-ai-infra']:
    grants = _get_model_grants(mid)
    if grants is None:
        print(mid + ': NOT FOUND')
    else:
        print(mid + ': ' + str(len(grants)) + ' grants')
"
```

- `letta-*` 显示 `NOT FOUND` → 模型未在 Open WebUI 注册，需要先通过 `access/update` API 创建（reconcile 会自动做）
- 显示 `0 grants` → grants 没写入，跳 **Step 3**
- 显示有 grants → 权限已设置，问题可能在 user_id 不匹配（参考 V1 Step 3）

### Step 3: reconcile 有没有跑、有没有报错

```bash
docker logs teleai-adapter 2>&1 | grep -i "reconcile\|grant\|sync\|error\|failed" | tail -20
```

常见失败原因：
- `login Open WebUI failed` → admin 凭证错误或 Open WebUI 不可达
- `API POST ... returned 4xx` → API 接口变了（Open WebUI 升级）
- `failed to update grants` → access/update API 调用失败

### Step 4: 手动触发全量对账

```bash
# 方式 1: 容器内执行
docker exec teleai-adapter python3 -c "
from webui_sync import reconcile_all
try:
    reconcile_all()
    print('done')
except Exception as e:
    print('FAILED: ' + str(e))
"

# 方式 2: API 调用
curl -X POST http://localhost:9800/admin/api/reconcile \
  -H "Authorization: Bearer <JWT>"
```

### Step 5: 确认 grants 生效

```bash
docker exec teleai-adapter python3 -c "
from webui_sync import _api_call
data = _api_call('GET', '/api/v1/models')
if data:
    for m in data.get('data', []):
        mid = m['id']
        grants = m.get('info', {}).get('access_grants', [])
        print(mid + ': ' + str(len(grants)) + ' grants')
"
```
