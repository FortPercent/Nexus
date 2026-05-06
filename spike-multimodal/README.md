# Multimodal Passthrough Spike

验证 Letta SDK 是否完整透传 OpenAI multimodal `content: [{type:text}, {type:image_url}]` 给底层 LLM provider。

详细背景见 `../../docs/multimodal-passthrough-design.md` (v2)。

---

## 一键跑

```bash
# 0. 启动 Docker Desktop（mac 上点图标即可）
# 验证: docker ps 能跑

# 1. 起服务（首次会 build mock-vllm 镜像 + pull pg/letta，约 3-5 分钟）
cd adapter/spike-multimodal
docker compose up -d --build

# 2. 等 letta 起来（约 30-60s，期间 letta 会做 schema migration）
docker compose logs -f letta
# 看到 "Application startup complete" 就 Ctrl+C 出来

# 3. 装 SDK 跑 spike
pip install 'letta-client>=0.1.0' pillow
python run_spike.py
```

输出会直接告诉你判定: **PASS / HALF / FAIL**。

---

## 判定结果与后续动作

| 结果 | 含义 | 后续 |
|---|---|---|
| ✅ **PASS** | image_url + text 都透传, content 仍是 list | multimodal-passthrough v2 Layer 1+2 直接落地, 不需要 patch letta |
| ⚠️ **HALF** | letta 拆掉 image_url 段, 只留 text | 写 `letta-patches/multimodal_passthrough.py` 在 letta 转换层把 list 完整透传 |
| ❌ **FAIL** | letta 拒收 list-form content (raise 异常) | 多模态走 `qwen-no-mem` 直连分支绕路（无记忆但能看图）|

---

## 排错

### `letta server 没起来`

```bash
docker compose logs letta
# 常见原因: 
#  - linux/amd64 emulation 慢, 多等 1 分钟再跑
#  - pg 没起好（healthcheck 通不过, 看 docker compose logs pg）
#  - LETTA_PG_URI 字符串错误
```

### `letta-client` 装不上 / 版本不匹配

```bash
# 用 letta server image 自带的 client 版本（避免 SDK 跟 server 不匹配）
docker compose exec letta pip show letta-client | grep Version
# 然后 host 端装同版本: pip install letta-client==<x.y.z>
```

### mock-vllm log 一直为空

说明 letta 没真把请求发到 mock-vllm。检查：

```bash
docker compose exec letta env | grep -E "OPENAI|LETTA"
# 应该看到 OPENAI_API_BASE=http://mock-vllm:8000/v1
```

如果 letta 用了别的 env var，去 letta 文档查。改 docker-compose.yml 里 letta service 的 environment 段。

---

## 清理

```bash
docker compose down -v   # -v 删数据卷
rm -rf logs/*.jsonl
```

---

## 关键文件

| 文件 | 作用 |
|---|---|
| `docker-compose.yml` | pg + mock-vllm + letta 三服务编排 |
| `mock-vllm/server.py` | 80 行 FastAPI, dump 所有 `/v1/chat/completions` body 到 `logs/requests.jsonl` |
| `run_spike.py` | letta SDK 客户端, 创建临时 agent → 发 multimodal message → 解析 mock log → 自动判定 |
| `fixtures/test.png` | 测试图（写着 "AI Infra spike"），首次跑会自动生成 |
| `logs/requests.jsonl` | mock-vllm 收到的所有请求 raw dump |
