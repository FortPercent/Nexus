# Nexus 本地（私有化）部署规划

> 编写: 2026-05-05  状态: planning  作者: Claude

## 范围

"本地部署" = 给政务客户私有化交付到客户机房（也含：把现 .46 服务器迁移给上海大数据中心做 PoC 之用）。
现状 .46 是单机部署，对内部 30 用户够用，对千万政务用户不够。本规划给出从 PoC → 生产规模的分级。

---

## 分级目标

### Level 1: PoC（单机, 50-100 用户）

跟当前 .46 同档，部署到客户提供的单台高配服务器即可。
- 价值：验收 / 演示 / 试点
- 时间线：2-3 天部署 + 1 周适配验收

### Level 2: 部门级（3-5 节点, 500-1000 用户）

加 vLLM 推理节点 + 主从 PostgreSQL + Ollama embedding 集群。
- 价值：单委办局生产
- 时间线：2-3 周部署 + 适配

### Level 3: 市级（10+ 节点, 万级并发）

K8s + 多 vLLM 节点 + 跨可用区 + 灾备。
- 价值：上海市政务智能应用建设项目目标态
- 时间线：1-2 月部署 + 6 个月稳定期

---

## Level 1 PoC 部署

### 硬件 BOM

| 组件 | 规格 | 估价（元） |
|---|---|---|
| 推理服务器 | 8× H800 80G GPU / 256C / 1TB RAM / 8TB NVMe | 80-150 万（国产替代另算） |
| 应用服务器 | 64C / 256G RAM / 4TB NVMe | 8-15 万 |
| 存储 | 共享文件存储 50TB（Ceph 推荐） | 20-40 万 |
| 网络 | 万兆交换机 + 防火墙 | 5-10 万 |

国产化替代（信创要求时）：
- GPU: 寒武纪 MLU370/590, 华为昇腾 910B（4 卡跑 Kimi-K2.6 MoE）
- CPU: 鲲鹏 920 / 海光 / 飞腾
- OS: 麒麟 / 统信
- DB: 达梦 / 人大金仓（替代 PostgreSQL）

### 软件栈（基于现有 docker-compose 4 服务）

```
┌─────────────────────────────────────────┐
│ 接入层: nginx + 国密 SSL                 │
├─────────────────────────────────────────┤
│ 应用层: docker-compose                   │
│   ├── teleai-nginx     (反向代理)        │
│   ├── teleai-adapter   (FastAPI x4 worker)│
│   ├── teleai-letta     (Letta server)   │
│   ├── teleai-postgres  (Letta + adapter) │
│   ├── teleai-ollama    (embedding)       │
│   ├── teleai-webui     (Open WebUI 改)   │
│   └── teleai-vllm      (Kimi-K2.6 / Qwen)│
├─────────────────────────────────────────┤
│ 存储: 本地盘 + Ceph (项目知识 / 原文件)   │
└─────────────────────────────────────────┘
```

注：现 docker-compose 没含 vLLM（vLLM 在临港远端）。私有化时 vLLM 必须本地化，单 8 卡 H800 节点跑 Kimi-K2.6 即可。

### 部署流程

```bash
# 1. 准备物料（一次性）
docker save teleai-adapter:vX > adapter.tar
docker save letta/letta:vY > letta.tar
docker save vllm/vllm-openai:vZ > vllm.tar
docker save open-webui:custom > webui.tar
# 模型权重单独 rsync（Kimi-K2.6 ~600GB / Qwen3 ~150GB）

# 2. 客户机房上线
docker load < adapter.tar
... (逐镜像 load)
docker compose -f docker-compose.prod.yml up -d
./scripts/post-deploy-check.sh    # 6 项 health check + 数据初始化
./scripts/regression.py           # 59/59 通过即合格

# 3. 数据初始化
- root org 建立（客户主体）
- admin 用户建立
- 16 类政务智能体 project + persona seed
- 8 类共性知识库 seed（法规/政策/FAQ）
```

### 预估 PoC 工程量

| 阶段 | 工时 |
|---|---|
| 镜像打包 + 离线物料（含模型权重缓存） | 2d |
| 客户机房上架 + 网络 + 联调 | 3-5d |
| 16 类智能体 persona 初版（政务高频场景拆） | 5-10d |
| 8 类知识库 seed（要客户提供原始材料） | 5-10d |
| 验收 + 回归 | 2-3d |

**总 17-30 工日 / 2 工程师 ≈ 2-3 周**

---

## Level 2 部门级

### 拓扑变化

- vLLM 双节点（active/active 跨节点 load balance）
- PostgreSQL 主从（Letta 数据 + adapter SQLite 切换 PG）
- Ollama 双节点（embedding，无状态可水平扩）
- 应用层 adapter 4 worker × 3 节点
- 共享存储 Ceph（项目知识盘文件）

### 改造点

| 项 | 现状 | 部门级要求 | 工时 |
|---|---|---|---|
| adapter SQLite | 单文件 | 切 PostgreSQL，迁移所有表 | 5-7d |
| Ollama 单点 | docker run 独立起 | 进 compose + 双节点 + nginx upstream | 1d |
| Letta agent map | 单进程 + fcntl 锁 | 切 Redis 分布式锁 | 2-3d |
| vLLM 单点 | 单实例 | 双实例 + 负载均衡 + 健康检查 | 2-3d |
| 监控 | 无 | Prometheus + Grafana + 告警 | 3-5d |
| 备份 | 无 | 每日 PG dump + Ceph 快照 + 跨机房 rsync | 2-3d |

### 总工程量

约 **3-4 周 / 2 工程师**。

---

## Level 3 市级

### 拓扑

- K3s/K8s 集群（10+ 节点）
- vLLM 多节点 + 模型 sharding（MoE 8 卡 × 4 节点）
- PostgreSQL 高可用（Patroni）
- Letta server 水平扩（无状态化改造，session affinity）
- 多组织树 / 多租户（已在 Issue #14 设计）
- 跨可用区灾备

### 关键改造（已在 Nexus V2 留白）

| 项 | 工程量 |
|---|---|
| Letta server 无状态化（Letta 内部状态外置） | **大** (4-8 周) |
| adapter K8s deployment + service + configmap | 1-2 周 |
| 多租户硬隔离（每委办局独立 schema 或独立库） | 2-3 周 |
| 等保三级 / 密评准备 | 4-8 周（大量审计材料 + 渗透测试） |
| 国产化栈替换（GPU/CPU/OS/DB） | 因栈而异，2-3 月 |

### 总工程量

约 **3-6 个月 / 4-6 工程师**，跟"上海政务智能应用建设项目" 6598 万预算的实施方匹配（一般是大集成商主导 + Nexus 作为分包能力提供方）。

---

## 商务路径选择

| 路径 | 节奏 | 风险 |
|---|---|---|
| 直接竞标主标（6598 万） | 2 周内提交，资质/信创/政务领域全缺 | 极高（基本不可能） |
| **作为分包参与（推荐）** | 联系总包候选方（国资集成商），提供"治理 + 多 scope + 智能体集约"能力分包，200-500 万规模 | 中等 |
| **委办局级试点（推荐）** | 找单一委办局（如市科委）做 Level 1-2 试点，500-2000 万规模 | 低 |
| 等下次小标 | 区级 / 委办局级独立标 | 低 |

---

## 时间线

```
Now      → 2026-05-15 : 完成 #13 #14 # 图片透传 三个工程开始（5w 内交付里程碑 1）
2026-05-15 → 2026-06-01: 16 类智能体 persona 初版 + 8 类知识库 seed（政务通用版）
2026-06-01 → 2026-06-15: PoC 镜像打包 + 离线物料 + 内部压测（Level 1 ready）
2026-06-15 → 2026-07-01: 找 1 个委办局做试点机房上线
2026-07-01 → 2026-09-01: 部门级（Level 2）改造（PG 切换 + 双节点 + 监控）
2026-09-01 → 2027-01-01: 等保过审 + 国产化栈替换（如客户要求）
2027-01-01+            : 市级（Level 3）商务推进
```

---

## 关键风险点

| 风险 | 缓解 |
|---|---|
| Kimi 不在国产合规栈，政务底座可能强制国产模型 | 模型层抽象已做，可换 GLM4/Qwen3-Instruct/DeepSeek-V3，4 文件 + sync 脚本搞定 |
| 等保三级 / 密评 / 信创认证 AI Infra 团队没有 | 找有资质的总包合作分包，或租用国资云的合规底座 |
| 没投标主体资质（ICP / CMMI / 政务集成） | 同上，分包 |
| Letta upstream 节奏 vs 政务交付节奏 | letta-patches 已有 3 patch 经验，必要时 fork |
| 单点 vLLM 流量瓶颈（max-num-seqs=8） | Level 2 改造时升级到双节点 + 升 max-num-seqs |

---

## 不在本规划范围

- 商务报价（按客户机房预算实际谈，硬件 + 软件订阅 + 实施 + 运维 4 项分开报）
- 法律合规（政府采购流程、合同条款、知识产权归属）
- 销售线索（找哪些大集成商做总包）
