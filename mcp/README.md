# Nexus MCP Bridge

把 Nexus 的记忆治理 API 通过 MCP 协议暴露给 Cursor / Claude Desktop / Continue / Cline 等 IDE。

## 工作原理

```
┌──────────┐   stdio   ┌─────────────────────┐  HTTP  ┌─────────────┐
│ Cursor   │ ─────────►│ nexus_mcp_stdio.py  │ ──────►│ Nexus on .46│
│ (你的电脑)│ ◄─────────│ (你的电脑)            │ ◄──────│ /memory/v1/*│
└──────────┘           └─────────────────────┘        └─────────────┘
```

`nexus_mcp_stdio.py` 是一个本地脚本,不是远程服务。Cursor 启动它作为子进程,通过 stdio 通信。脚本内部用你的 JWT 调 Nexus REST API。

## 快速开始

### 1. 拷脚本到本地

```bash
# 比如放到 ~/bin/
cp nexus_mcp_stdio.py ~/bin/
chmod +x ~/bin/nexus_mcp_stdio.py
```

### 2. 拿 JWT

1. 浏览器登录 Open WebUI(`http://192.168.151.46:3000`)
2. F12 → Application → Local Storage → 拷 `token` 字段值

JWT 默认有效期由 Open WebUI 决定(通常较长,可用数天)。

### 3. 配 Cursor

打开 Cursor → Settings → MCP → 编辑 mcp.json:

```json
{
  "mcpServers": {
    "nexus": {
      "command": "python3",
      "args": ["/Users/<you>/bin/nexus_mcp_stdio.py"],
      "env": {
        "NEXUS_URL": "http://192.168.151.46:9800",
        "NEXUS_TOKEN": "<paste your JWT here>"
      }
    }
  }
}
```

重启 Cursor。看右下角 MCP 状态变绿即接通。

### 4. 用

在 Cursor 里 chat,Cursor 会自动发现 `search_memory` tool。例子提示词:

> 用 search_memory 在 ai-infra 项目找跟 "Kimi-K2.6 推理" 相关的决策

Cursor 会调 search_memory(project_id="ai-infra", query="Kimi-K2.6 推理"),返带高亮 snippet 的结果。

## 可用工具(V1)

| 工具 | 用途 |
|---|---|
| `search_memory(project_id, query, kind?, limit?)` | 跨决策 + 记忆事件 FTS5 搜索 |

V2 会加 `get_decision` / `get_trace` / `list_conflicts` / `get_protection` 4 个 read-only 工具。

## 网络要求

- 需要能访问 `http://192.168.151.46:9800`(中国电信内网或 VPN)
- 不需要外网

## 排错

**MCP 状态红 / Cursor 报 server not responding**:
- 检查 NEXUS_TOKEN 是否填了 + 没过期(过期后重新 F12 拷 JWT)
- 测试脚本能否手动跑通:

```bash
NEXUS_TOKEN=<your-jwt> python3 nexus_mcp_stdio.py
# 然后粘 JSONRPC 测命令(单行):
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"search_memory","arguments":{"project_id":"ai-infra","query":"Kimi"}}}
```

**返回结果说 "你不是该项目的成员"**:
- 你登录的 user 没加入对应 project,或用了错的 project_id
- `org` 项目所有人可读;`personal:<your-user-id>` 只能你自己看;真实 project 要管理员加进 project_members

## V2 / V3 路线

- W5-2: 加 6 个 read-only tools(get_decision / get_trace / list_conflicts / get_protection / list_decisions / search_decisions)
- W5-3: 长效 API key(替代 JWT,不会过期)+ 防滥用限流

## 内部约定

- Tool 返回 content array,文本是 JSON.stringify 的结果(MCP 标准)
- 错误通过 `isError: true` 标记,具体在 content[0].text 里
- protocolVersion: 2024-11-05
