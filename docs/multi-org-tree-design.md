# 多组织树与跨部门授权设计 (Issue #14)

> 编写: 2026-05-05  状态: design  作者: Claude

## 背景

现有 `projects` 平面化 + `project_members` 多对多，无部门概念。
政务投标场景"市城运 / 市科委 / ... 多委办局"，每委办局下还有处室，需要：
- 部门树（任意层级）
- 用户挂部门
- project 共享给部门（可下行继承）
- 跨部门联合 project
- 三级 Block (人/项目/组织) 扩展为 (人/项目/部门-递归)

---

## 数据模型

### organizations

```sql
CREATE TABLE organizations (
  id TEXT PRIMARY KEY,
  parent_id TEXT,                          -- 自引用，根 org NULL
  name TEXT NOT NULL,
  code TEXT NOT NULL UNIQUE,               -- 'sh-chengyun' / 'sh-chengyun-yunyingchu'
  org_type TEXT DEFAULT 'department',      -- 'bureau'/'department'/'division'
  letta_block_id TEXT,                     -- 该 org 的共享 Block，子部门递归继承
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (parent_id) REFERENCES organizations(id)
);

CREATE INDEX idx_org_parent ON organizations(parent_id);
```

### org_members

```sql
CREATE TABLE org_members (
  org_id TEXT,
  user_id TEXT,
  role TEXT DEFAULT 'member',              -- 'admin'/'member'
  PRIMARY KEY (org_id, user_id)
);

CREATE INDEX idx_orgmem_user ON org_members(user_id);
```

### project_orgs（替代部分 project_members）

```sql
CREATE TABLE project_orgs (
  project_id TEXT,
  org_id TEXT,
  access_level TEXT DEFAULT 'shared_read', -- 'owner'/'shared_read'/'shared_write'
  PRIMARY KEY (project_id, org_id)
);

CREATE INDEX idx_projorg_org ON project_orgs(org_id);
```

`project_members` 保留（个人成员授权仍可用），是 `project_orgs` 的并集。

---

## 权限解析

### 单 SQL CTE 解决"用户能访问哪些 project"

```sql
WITH RECURSIVE
  -- step 1: 用户直接所属 org
  user_orgs(org_id) AS (
    SELECT org_id FROM org_members WHERE user_id = ?
  ),
  -- step 2: 递归向上找祖先 org（部门 → 局 → 委办局根）
  ancestor_orgs(org_id) AS (
    SELECT org_id FROM user_orgs
    UNION
    SELECT o.parent_id FROM organizations o
      JOIN ancestor_orgs a ON o.id = a.org_id
      WHERE o.parent_id IS NOT NULL
  )
SELECT DISTINCT po.project_id, po.access_level
  FROM project_orgs po
  JOIN ancestor_orgs ao ON po.org_id = ao.org_id

UNION

SELECT pm.project_id, pm.role AS access_level
  FROM project_members pm WHERE pm.user_id = ?;
```

缓存层：`(user_id, project_id) → access_level` LRU 5min（org 树变化频率低）。

### 反向：project 可见用户（下行继承）

```sql
WITH RECURSIVE descendant_orgs(org_id) AS (
  SELECT org_id FROM project_orgs WHERE project_id = ?
  UNION
  SELECT o.id FROM organizations o
    JOIN descendant_orgs d ON o.parent_id = d.org_id
)
SELECT DISTINCT om.user_id
  FROM org_members om
  JOIN descendant_orgs d ON om.org_id = d.org_id;
```

—— "下行继承"在递归 SQL 里实现：project 挂市城运，自动让所有市城运下属处室成员可见。

---

## 三种典型授权场景

| 场景 | 配置 |
|---|---|
| 私有项目 | `project_orgs(owner_org_id, owner)` + `project_members` 限定个人 |
| 下行共享（市城运的项目，下属处室继承可见） | `project_orgs(市城运 org_id, shared_read)`，处室成员走递归命中 |
| 平级联合（市城运 + 市科委联合项目） | `project_orgs` 两行 (`shared_write`)，互不继承 |

---

## org_block 扩展

agent 系统提示组装顺序改成（深度优先 root → leaf）：

```
human_block (用户私人)
+ 用户所有祖先 org_blocks (root → leaf)：市政府 > 市城运 > 运营处
+ project_block (当前 project)
```

实现：`routing.py::_attach_agent_blocks_only` 改成读 `ancestor_orgs` CTE，按 depth 排序 attach。

block_id 列表上限 Letta 是 N 个（具体值待查 Letta SDK），超额时只挂 user 直接所属 org 的 block + project_block。

---

## admin UI

### 组织管理 tab（新）

- 树形展示（el-tree 类似组件，拖拽改 parent_id）
- 成员管理矩阵（org × user × role）
- project 共享配置矩阵（project × org × access_level）

### 用户管理改造

- 列表加"所属组织"列
- 用户详情 tab 加"组织成员关系"

---

## SSO/LDAP 同步

政务环境通常组织在外部 OA / 政务网。同步 worker：

```python
# scripts/sync_org_tree.py
# 1. 拉外部 org tree (SOAP/REST/LDAP)
# 2. diff 本地 organizations 表
# 3. UPSERT (按 code 主键)
# 4. 拉 user-org 关系 → UPSERT org_members
# 5. 删除外部已没的 org（先迁移其下成员到 parent）
```

接 OIDC / CAS 单点登录：现有 Open WebUI 支持 OAuth2，扩配置即可。

---

## 数据迁移

存量 32 个 project 平迁：
1. 建一个 root org "AI 研究院"
2. 每个 project 一行 `project_orgs (root_org_id, owner)`
3. 现有 `project_members` 保留
4. 跑 e2e 验证权限解析结果一致

---

## 工时

| 模块 | 工时 |
|---|---|
| schema + 迁移脚本 | 1-2d |
| 权限解析 CTE + 缓存 + 现有 endpoint 全改 | 2-3d |
| org_block 扩展（per-org + 递归注入） | 2d |
| admin UI 树形管理 + 共享配置矩阵 | 3-4d |
| SSO/LDAP 同步（视目标系统） | 2-3d |
| 联调 + e2e + 文档 | 2-3d |

**总 12-17 工日 ≈ 2.5-3.5 周**

---

## 测试策略

- unit: 递归 CTE 各种树形（链状/分支/双亲）下能正确解析；缓存失效；唯一约束
- e2e: 三种典型授权场景跑通；存量数据迁移后无权限漂移
- 安全: 跨 org 越权测（用户 A 通过伪造 org_id 访问 B 的 project）

---

## 不做的事（留 V2）

- 临时授权 (TTL 7d)：先静态授权
- 部门级配额（各部门 quota）
- 多租户硬隔离（每委办局独立数据库）：先共享 schema 软隔离
