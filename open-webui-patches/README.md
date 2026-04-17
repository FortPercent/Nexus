# Open WebUI 自定义补丁

Open WebUI 源码在 `infra46:/home/infra46/open-webui-custom/`，**无 git remote**。
这个目录保存我们改过的文件副本，便于版本管控 + 灾备恢复。

## 部署流程

每次改完文件：

```bash
# 本地 → 服务器
scp <file> infra46@192.168.151.46:/home/infra46/open-webui-custom/<path>

# 服务器：构建 + 部署
ssh infra46@192.168.151.46 "cd /home/infra46/open-webui-custom && docker build -t open-webui-custom:latest ."
ssh infra46@192.168.151.46 "docker stop open-webui && docker rm open-webui && docker run -d --name open-webui --network teleai-adapter_default --restart unless-stopped -p 3000:8080 -v open-webui-data:/app/backend/data -e OPENAI_API_BASE_URL=http://teleai-adapter:8000/v1 -e OPENAI_API_KEY=teleai-adapter-key-2026 -e WEBUI_NAME='TeleAI Nexus' -e WEBUI_SECRET_KEY=6WYGSa8e7EBsSeG3 open-webui-custom:latest"
```

## 文件清单

- `Knowledge.svelte` → `src/lib/components/workspace/Knowledge.svelte`
  - 2026-04-17 晚：加 Letta embedding 索引状态徽章
  - 调 `/admin/api/file-statuses` 批量获取状态
  - 每 5 秒 poll 一次（只在有 pending 条目时）
  - 行内显示 `索引中 87/125` / `索引失败`，`completed` 不显示
