# 排查指南：常见问题

> 更新时间: 2026-04-16

---

## 问题 1：加入项目后非 admin 看不到模型

### 症状

- admin 账号能看到所有模型
- 非 admin 成员加入项目后，刷新 Open WebUI，模型下拉框里没有 `AI 助手 (项目名)`

### 根因

Open WebUI 的模型可见性有两层：
1. `model` 表：模型是否"存在"于 Open WebUI 的管理体系
2. `access_grant` 表：已注册模型对哪些用户可见

只写 `access_grant` 不往 `model` 表注册 → 非 admin 看不到。

### 排查链路

**Step 1: access_grant 表有没有写入**

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

**Step 2: model 表有没有注册（最常见根因）**

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

- 只有 `qwen-no-mem`，没有 `letta-*` → **根因确认**，触发对账修复（Step 5）
- `letta-*` 也在 → 继续 Step 3

**Step 3: user_id 是否匹配**

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

# 适配层 project_members
docker exec teleai-adapter python3 -c "
from db import get_db
db = get_db()
for r in db.execute('SELECT user_id, project_id FROM project_members').fetchall():
    print(r['user_id'] + ' | ' + r['project_id'])
db.close()
"
```

两边 user_id 不一致 → 授权无效。

**Step 4: grant_model_access 写入失败**

```bash
docker logs teleai-adapter 2>&1 | grep -i "grant failed\|sync\|error"
```

常见原因：
- `database is locked` → SQLite 并发锁超时
- `no such table` → Open WebUI 版本升级改了表结构
- 文件不存在 → WEBUI_DB_PATH 配置错误或 volume 未挂载

**Step 5: 手动触发全量对账**

```bash
# 容器内执行
docker exec teleai-adapter python3 -c "
from webui_sync import reconcile_all
reconcile_all()
print('done')
"

# 或 API 调用（需 org admin JWT）
curl -X POST http://localhost:9800/admin/api/reconcile \
  -H "Authorization: Bearer <JWT>"
```

---

## 问题 2：知识管理页面 /knowledge 打开后跳回 Open WebUI 首页

### 症状

浏览器访问 `http://192.168.151.46:9800/knowledge`，页面闪一下后跳转到 Open WebUI 聊天界面。

### 根因

知识管理页面加载后调 `/admin/api/me`，如果 JWT token 无效返回 401，前端 JS 执行 `window.location.href = "/"` 跳回首页。

### 排查链路

**Step 1: 确认 nginx 返回的是知识管理页面**

```bash
curl -s http://localhost:9800/knowledge | head -3
```

期望看到 `<html lang="zh">`（知识管理页面），不是 `<html lang="en">`（Open WebUI）。

**Step 2: 确认 JWT token 是否有效**

```bash
# 查 nginx 日志，看 /admin/api/me 返回什么
docker logs teleai-nginx 2>&1 | grep "admin/api/me" | tail -5
```

- 返回 401 → **JWT 失效**，继续 Step 3
- 返回 200 → token 没问题，检查前端 JS 逻辑

**Step 3: 确认 WEBUI_SECRET_KEY 是否一致**

```bash
# Open WebUI 的 secret
docker exec open-webui env | grep WEBUI_SECRET_KEY

# adapter 的 secret
docker exec teleai-adapter env | grep OPENWEBUI_JWT_SECRET
```

**两边必须一致。** 如果不一致，Open WebUI 签的 JWT 在 adapter 验证不过。

常见原因：重建 Open WebUI 容器时**漏了 `WEBUI_SECRET_KEY` 环境变量**，Open WebUI 生成了新 secret，旧 token 全部失效。

**修复：**

```bash
# 重建容器，带上正确的 WEBUI_SECRET_KEY
docker stop open-webui && docker rm open-webui
docker run -d \
  --name open-webui \
  --network teleai-adapter_default \
  -p 3000:8080 \
  -v open-webui-data:/app/backend/data \
  -e OPENAI_API_BASE_URL=http://teleai-adapter:8000/v1 \
  -e OPENAI_API_KEY=teleai-adapter-key-2026 \
  -e "WEBUI_NAME=TeleAI Nexus" \
  -e WEBUI_SECRET_KEY=6WYGSa8e7EBsSeG3 \
  -e USE_OLLAMA_DOCKER=false \
  --restart unless-stopped \
  open-webui-custom:latest
```

用户需要**重新登录** Open WebUI 获取新 token。

**Step 4: 用户重新登录**

退出 Open WebUI 再登录，然后访问 `/knowledge`。

---

## 问题 3：Open WebUI 容器重建后的检查清单

重建 Open WebUI 容器后，必须确认以下环境变量都设置了：

| 环境变量 | 值 | 漏掉的后果 |
|---------|-----|----------|
| `WEBUI_SECRET_KEY` | `6WYGSa8e7EBsSeG3` | JWT 失效，知识管理页面打不开 |
| `OPENAI_API_BASE_URL` | `http://teleai-adapter:8000/v1` | 聊天无法发送 |
| `OPENAI_API_KEY` | `teleai-adapter-key-2026` | 聊天请求被拒绝 |
| `WEBUI_NAME` | `TeleAI Nexus` | 标题显示错误 |
| `USE_OLLAMA_DOCKER` | `false` | 启动时尝试连 Ollama 报错 |

完整启动命令见问题 2 的修复部分。
