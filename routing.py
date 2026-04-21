"""路由模块 —— user_id + project → agent_id，含双向校验和知识挂载"""
import logging
import sqlite3
from fastapi import HTTPException
from letta_client import Letta, AsyncLetta, ConflictError
from config import LETTA_BASE_URL, VLLM_ENDPOINT, DB_PATH
from db import get_db

letta = Letta(base_url=LETTA_BASE_URL)
letta_async = AsyncLetta(base_url=LETTA_BASE_URL)

# 自定义工具缓存
_suggest_tool_id = None
_suggest_todo_tool_id = None


PERSONA_TEXT = (
    "你是 TeleAI Nexus 智能助手，服务于中国电信人工智能研究院。\n\n"
    "【默认行为 — 每次对话都适用】\n"
    "**只要用户的问题涉及具体事实 / 数据 / 文档内容 / 项目情况，你必须默认主动查当前 project 的知识库**，不要等用户说「查一下」或用「#」引用。流程：\n"
    "- 结构化统计问题（多少件 / 按类别聚合等）→ 调 list_tables / describe_table / query_table（仅当已有表）\n"
    "- 文档内容 / 条款 / 名称查找 → 调 list_project_files 看有哪些文件，再 grep_project_files 搜关键词，最后 read_project_file 读具体文件\n"
    "- 搜索范围**仅限当前 project**；不要自动跨到 personal / org 知识\n"
    "**什么时候不查**：闲聊（你好、在吗、今天天气）、工具指令类（「提醒我 X」→ suggest_todo）、纯个人信息（「我是 X」→ memory_insert）\n\n"
    "【用户显式 # 引用文件 / 对话时】\n"
    "user message 可能含这两种引用前缀（adapter 已替你读完原文）：\n"
    "- `=== 引用文档：文件名 ===` — 用户 # 挂载的知识文件\n"
    "- `=== 引用对话：标题 ===` — 用户 # 挂载的历史对话\n"
    "**必须优先基于这些引用内容回答**。\n"
    "**关键区分（Issue #2 2026-04-21）**：带 `【本轮当前引用 开始】 … 【本轮当前引用 结束】` 外层包裹的才是用户**本条消息**新挂载的；\n"
    "没这个包裹但出现在历史消息里的 `=== 引用文档/对话 ===`，是**之前轮次**的引用，**优先级低于本轮**。\n"
    "若用户问「当前引用的文件名 / 对话标题」→ 必须回答【本轮当前引用】块里的那个，**不要回答历史引用**。\n\n"
    "【工具调用诚信（Issue #3 #4 2026-04-21）】\n"
    "- 不要声称调用了没真实调用的工具：用户问 `DOCS_USED=N` / 使用了几个来源 → N 必须等于你本轮真实调用 read_project_file 或 grep_project_files 命中**不同文件**的数量。\n"
    "- 用户明示步骤顺序时（例：「1) 先 list 2) 再 read 3) 最后综合」）→ **必须按序逐步调用每一步**，不能跳步、不能合并、不能提前 read 跳过 list。\n\n"
    "【文件访问工具】\n"
    "- `list_project_files`：看 project 里有哪些文件（名字、质量标、大小）\n"
    "- `grep_project_files(pattern)`：按关键词/正则在所有文件里搜，返回命中行\n"
    "- `read_project_file(file_name, offset=0, max_chars=8000)`：读某份文件内容，支持分页（文件 >8K 时自己 offset 翻页）\n"
    "- `archival_memory_search(query)`：搜你自己过去记录的记忆（非文件内容）\n"
    "**不存在的工具别瞎调**：`grep_files` / `open_files` / `semantic_search_files` 已下线，用上面的 kb 工具替代。\n\n"
    "【用户说什么调哪个工具 — 严格遵守】\n"
    "1. 时间/动作/提醒/承诺类 → 调 suggest_todo\n"
    "   例：「五点半写周报」「下周前跑 baseline」「提醒我 X」「回头要做 Y」\n"
    "2. 项目决策/架构/版本/里程碑 → 调 suggest_project_knowledge\n"
    "   例：「项目用 vLLM」「v1.2 已发布」「架构改成 X」\n"
    "3. 个人身份/偏好 → 调 memory_insert 到 human block\n"
    "   例：「我是 AI Infra 的吴煊佴」「我喜欢用 Python」\n"
    "4. **数量 / 合计 / 按类别统计 / 排名 / 平均值**（仅在 list_tables 返回了表时）→ 用 query_table 写一条 SELECT，不要用 grep_project_files\n"
    "   例：「多少件固定资产」「每个部门多少台机器人」「金额排名 top 10」\n"
    "   流程：list_tables → describe_table → query_table；SQL 方言是 DuckDB\n"
    "   grep_project_files 只用于找文档原文，不用于统计\n\n"
    "【底线】\n"
    "同一条内容只调一次对应工具。不要声称「已记忆」而不真实调用。"
    "不要把「今天要做 X」之类写进 human block，那是 TODO。\n"
    "绝不要在回复中暴露内部 ID（agent_id/folder_id/file_id/block_id）。"
)


def _get_suggest_tool_id() -> str:
    """获取或创建 suggest_project_knowledge 工具，返回 tool_id"""
    global _suggest_tool_id
    if _suggest_tool_id:
        return _suggest_tool_id

    def suggest_project_knowledge(content: str, agent_state: "AgentState") -> str:
        """当用户在对话中提到了与项目相关的重要信息（如技术栈变更、新成员加入、里程碑完成、架构决策等），
        调用此工具将信息提交为项目知识建议，等待项目管理员审核后纳入项目知识库。
        不要提交用户的个人信息（那属于 human block），只提交对整个项目团队有价值的信息。

        Args:
            content: 建议的知识内容，简洁准确，一两句话概括关键信息

        Returns:
            提交结果
        """
        import urllib.request
        import json as _json
        project_id = agent_state.metadata.get("project", "")
        user_id = agent_state.metadata.get("owner", "")
        if not project_id or not content.strip():
            return "提交失败：缺少项目信息或内容为空"
        try:
            url = f"http://teleai-adapter:8000/admin/api/project/{project_id}/suggestions"
            data = _json.dumps({"user_id": user_id, "content": content.strip()}).encode()
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15)
            return "已提交知识建议，等待项目管理员审核"
        except Exception as e:
            return f"提交失败：{e}"

    tool = letta.tools.upsert_from_function(func=suggest_project_knowledge)
    _suggest_tool_id = tool.id
    logging.info(f"suggest_project_knowledge tool: {tool.id}")
    return _suggest_tool_id


def _get_suggest_todo_tool_id() -> str:
    """获取或创建 suggest_todo 工具，返回 tool_id。AI 建议时写入 project_todos awaiting_user 状态。"""
    global _suggest_todo_tool_id
    if _suggest_todo_tool_id:
        return _suggest_todo_tool_id

    def suggest_todo(title: str, agent_state: "AgentState", description: str = "", priority: str = "medium") -> str:
        """当用户提到"需要做的事"时调用此工具创建待办建议（会进入项目 TODO 面板的"待处理"区，等用户确认后才生效）。

        触发场景（必须用）:
        - 时间+动作: "五点半写周报"、"下周前跑 baseline"
        - 提醒语: "提醒我 X"、"别忘了 Y"、"回头要做 Z"
        - 承诺/计划: "我要做 X"、"准备做 Y"

        不要用此工具的场景:
        - 个人身份/偏好（"我喜欢 Python"）→ 用 memory_insert 到 human block
        - 项目知识/架构决策（"项目用 vLLM"）→ 用 suggest_project_knowledge

        Args:
            title: 任务标题，简洁清晰（≤50 字，动词开头）
            description: 背景细节（可选，≤300 字）
            priority: low / medium / high，默认 medium

        Returns:
            提交结果
        """
        import urllib.request
        import json as _json
        project_id = agent_state.metadata.get("project", "")
        user_id = agent_state.metadata.get("owner", "")
        if not project_id or not title.strip():
            return "提交失败：缺少项目或标题"
        try:
            url = f"http://teleai-adapter:8000/admin/api/project/{project_id}/todos/ai-submit"
            data = _json.dumps({
                "user_id": user_id,
                "title": title.strip(),
                "description": description.strip() if description else "",
                "priority": priority or "medium",
            }).encode()
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15)
            return f"已提交 TODO「{title.strip()}」，等用户在「📋 待处理」确认后生效"
        except Exception as e:
            return f"提交失败：{e}"

    tool = letta.tools.upsert_from_function(func=suggest_todo)
    _suggest_todo_tool_id = tool.id
    logging.info(f"suggest_todo tool: {tool.id}")
    return _suggest_todo_tool_id


def _create_agent_fresh(user_id: str, project: str) -> str:
    """创建新 agent 并挂 blocks/tools, **不读不写 user_agent_map**.

    用途: preflight._atomic_rebuild 需要"无 map 窗口"地造出新 agent, 然后
    通过 CAS UPDATE 原子切 map. 如果 fresh create 失败, map 不受影响.

    仍然检查 project membership. 返回新 agent.id.
    """
    db = get_db()
    try:
        is_member = db.execute(
            "SELECT 1 FROM project_members WHERE user_id = ? AND project_id = ?",
            (user_id, project),
        ).fetchone()
        if not is_member:
            raise HTTPException(403, f"你不是项目 {project} 的成员，请联系项目管理员添加")

        # 获取自定义工具（两个：suggest_project_knowledge + suggest_todo）
        custom_tool_ids = []
        try:
            custom_tool_ids.append(_get_suggest_tool_id())
        except Exception as e:
            logging.warning(f"failed to get suggest tool: {e}")
        try:
            custom_tool_ids.append(_get_suggest_todo_tool_id())
        except Exception as e:
            logging.warning(f"failed to get suggest_todo tool: {e}")

        try:
            from letta_sql_tools import should_attach_sql_tools, get_sql_tool_ids
            if should_attach_sql_tools(project):
                custom_tool_ids.extend(get_sql_tool_ids())
        except Exception as e:
            logging.warning(f"failed to attach SQL tools for project {project}: {e}")

        try:
            from kb.letta_tools import get_kb_tool_ids
            custom_tool_ids.extend(get_kb_tool_ids())
        except Exception as e:
            logging.warning(f"failed to attach kb tools: {e}")

        human_block_id = get_or_create_personal_human_block(user_id)

        agent = letta.agents.create(
            name=f"user-{user_id}-{project}",
            model="openai/Qwen3.5-122B-A10B",
            metadata={"owner": user_id, "project": project},
            tool_ids=custom_tool_ids,
            block_ids=[human_block_id],
            memory_blocks=[{"label": "persona", "value": PERSONA_TEXT}],
            llm_config={
                "model": "Qwen3.5-122B-A10B",
                "model_endpoint_type": "openai",
                "model_endpoint": VLLM_ENDPOINT,
                "context_window": 60000,
                "enable_reasoner": True,
            },
            embedding_config={
                "embedding_model": "nomic-embed-text",
                "embedding_endpoint_type": "openai",
                "embedding_endpoint": "http://ollama:11434/v1",
                "embedding_dim": 768,
                "embedding_chunk_size": 300,
                "batch_size": 32,
            },
        )

        _attach_agent_blocks_only(db, agent.id, user_id, project)

        for tid in custom_tool_ids:
            try:
                letta.agents.tools.attach(agent_id=agent.id, tool_id=tid)
            except Exception as e:
                logging.warning(f"attach tool {tid} to {agent.id}: {e}")

        db.commit()  # block attachments 等
        return agent.id
    finally:
        if db:
            db.close()


def get_or_create_agent(user_id: str, project: str) -> str:
    """获取用户对应的 Agent，不存在则自动创建并挂载分级知识。"""
    db = get_db()
    try:
        is_member = db.execute(
            "SELECT 1 FROM project_members WHERE user_id = ? AND project_id = ?",
            (user_id, project),
        ).fetchone()
        if not is_member:
            raise HTTPException(403, f"你不是项目 {project} 的成员，请联系项目管理员添加")

        row = db.execute(
            "SELECT agent_id FROM user_agent_map WHERE user_id = ? AND project_id = ?",
            (user_id, project),
        ).fetchone()

        if row:
            agent_id = row["agent_id"]
            db.close()
            db = None

            agent = letta.agents.retrieve(agent_id=agent_id)
            owner = agent.metadata.get("owner")
            if owner != user_id:
                raise HTTPException(
                    500,
                    f"安全校验失败: agent {agent_id} 归属 {owner}，但映射表指向 user {user_id}",
                )
            return agent_id

        # 获取自定义工具（两个：suggest_project_knowledge + suggest_todo）
        custom_tool_ids = []
        try:
            custom_tool_ids.append(_get_suggest_tool_id())
        except Exception as e:
            logging.warning(f"failed to get suggest tool: {e}")
        try:
            custom_tool_ids.append(_get_suggest_todo_tool_id())
        except Exception as e:
            logging.warning(f"failed to get suggest_todo tool: {e}")

        # SQL 工具只在 project 已有结构化数据时挂，避免纯文档 agent 被 LLM 乱生 SQL
        try:
            from letta_sql_tools import should_attach_sql_tools, get_sql_tool_ids
            if should_attach_sql_tools(project):
                custom_tool_ids.extend(get_sql_tool_ids())
        except Exception as e:
            logging.warning(f"failed to attach SQL tools for project {project}: {e}")

        # Phase 1 kb 工具 (list_project_files / read_project_file / grep_project_files) 全 agent 都挂
        # 这是 agent 访问 project 知识的唯一路径 (不再靠 Letta folder / built-in semantic_search_files)
        try:
            from kb.letta_tools import get_kb_tool_ids
            custom_tool_ids.extend(get_kb_tool_ids())
        except Exception as e:
            logging.warning(f"failed to attach kb tools: {e}")

        # 共享 human block（跨项目一份）先准备好，随 agent 创建一起 attach，
        # 这样 Letta 系统提示编译时能拿到 block value。
        human_block_id = get_or_create_personal_human_block(user_id)

        agent = letta.agents.create(
            name=f"user-{user_id}-{project}",
            model="openai/Qwen3.5-122B-A10B",
            metadata={"owner": user_id, "project": project},
            tool_ids=custom_tool_ids,
            block_ids=[human_block_id],
            memory_blocks=[
                {
                    "label": "persona",
                    "value": PERSONA_TEXT,
                },
            ],
            llm_config={
                "model": "Qwen3.5-122B-A10B",
                "model_endpoint_type": "openai",
                "model_endpoint": VLLM_ENDPOINT,
                # 临港 vLLM max_model_len=65536, 留 ~5K 余量给 tool/hidden overhead。
                # 32000 太小: 安全规范类知识密集 project (50+ PDF) 的 system prompt
                # 轻易超 32K, 实测 biany security-mgmt project system prompt 51K。
                # 见 2026-04-20 context_window_overflow_in_system_prompt 事故
                "context_window": 60000,
                "enable_reasoner": True,
            },
            # embedding_config 显式传 (Letta 存 None 会让某些路径抛异常, 保险做).
            # Phase 1 之后 semantic_search_files / grep_files 已下线, embedding 主要给
            # archival_memory_search 和历史兼容路径用.
            embedding_config={
                "embedding_model": "nomic-embed-text",
                "embedding_endpoint_type": "openai",
                "embedding_endpoint": "http://ollama:11434/v1",
                "embedding_dim": 768,
                "embedding_chunk_size": 300,
                "batch_size": 32,
            },
        )

        # Phase 1 之后: 只挂 blocks (org/project block), 不挂 folder.
        # agent 访问知识全走 kb 工具 → adapter endpoints → /data/serving/adapter/projects/<slug>/.legacy/
        _attach_agent_blocks_only(db, agent.id, user_id, project)

        # Letta bug workaround：agents.create 里 tool_ids=[...] 不会让工具进 system prompt，
        # 必须显式 attach 一遍（见 scripts/migrate_ai_tools.py 同样做法）
        for tid in custom_tool_ids:
            try:
                letta.agents.tools.attach(agent_id=agent.id, tool_id=tid)
            except Exception as e:
                logging.warning(f"attach tool {tid} to {agent.id}: {e}")

        db.execute(
            "INSERT INTO user_agent_map (user_id, project_id, agent_id) VALUES (?, ?, ?)",
            (user_id, project, agent.id),
        )
        db.commit()
        return agent.id
    finally:
        if db:
            db.close()


def get_or_create_org_resources() -> dict:
    """获取组织级 Block/Folder，不存在则自动初始化。"""
    db = get_db()
    try:
        return _get_org_resources(db)
    finally:
        db.close()


def get_or_create_personal_human_block(user_id: str) -> str:
    """获取用户的人设 block（human label），不存在则创建。per-user 共享，跨项目所有 agent attach 同一个。"""
    from db import use_db
    with use_db() as db:
        row = db.execute(
            "SELECT personal_human_block_id FROM user_cache WHERE user_id = ? AND personal_human_block_id IS NOT NULL",
            (user_id,),
        ).fetchone()
    logging.info(f"get_or_create_personal_human_block user={user_id[:8]} cached={row['personal_human_block_id'] if row else None}")
    if row:
        block_id = row["personal_human_block_id"]
        try:
            letta.blocks.retrieve(block_id=block_id)
            logging.info(f"  reuse existing block {block_id[:16]}")
            return block_id
        except Exception as e:
            logging.warning(f"  personal human block {block_id} missing in Letta ({type(e).__name__}: {e}), recreating")

    block = letta.blocks.create(
        label="human",
        value="(新用户，信息未知)",
        limit=2000,
    )
    logging.info(f"  created new block {block.id[:16]}")
    with use_db() as db:
        db.execute(
            "INSERT INTO user_cache (user_id, personal_human_block_id) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET personal_human_block_id = excluded.personal_human_block_id, updated_at = CURRENT_TIMESTAMP",
            (user_id, block.id),
        )
    return block.id


def get_or_create_personal_folder(user_id: str) -> str:
    """获取用户的个人文件夹 ID，不存在则创建。per-user，跨项目共享。"""
    db = get_db()
    row = db.execute(
        "SELECT personal_folder_id FROM user_cache WHERE user_id = ? AND personal_folder_id IS NOT NULL",
        (user_id,),
    ).fetchone()
    if row:
        folder_id = row["personal_folder_id"]
        db.close()
        return folder_id

    folder_name = f"personal-{user_id}"
    try:
        folder = letta.folders.create(name=folder_name, embedding_config={"embedding_model": "nomic-embed-text", "embedding_endpoint_type": "openai", "embedding_endpoint": "http://ollama:11434/v1", "embedding_dim": 768})
    except ConflictError:
        logging.warning(f"folder {folder_name} already exists in Letta, looking up")
        page = letta.folders.list(name=folder_name, limit=1)
        if not page.items:
            db.close()
            raise HTTPException(500, f"folder {folder_name} 冲突但查找不到，请联系管理员")
        folder = page.items[0]

    db.execute(
        "INSERT INTO user_cache (user_id, personal_folder_id) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET personal_folder_id = excluded.personal_folder_id, updated_at = CURRENT_TIMESTAMP",
        (user_id, folder.id),
    )
    db.commit()
    db.close()
    return folder.id


def sync_org_resources_to_all_agents() -> int:
    """把组织级资源 best-effort 挂到所有已存在的 Agent。"""
    resources = get_or_create_org_resources()
    db = get_db()
    rows = db.execute("SELECT DISTINCT agent_id FROM user_agent_map").fetchall()
    db.close()

    # Phase 1 后: 不再 attach org folder (agent 走 adapter kb 工具, 不依赖 Letta folder).
    # 否则每次 adapter startup 跑这个函数, 会把 rollout_all 的 detach 全抵消掉.
    # org folder 本身保留 (admin 后台用), 只是 agent 不挂.
    attached = 0
    for row in rows:
        try:
            letta.agents.blocks.attach(agent_id=row["agent_id"], block_id=resources["block_id"])
            attached += 1
        except Exception:
            pass
    return attached


def _attach_agent_resources(db, agent_id: str, user_id: str, project: str):
    """(LEGACY Phase 0 之前的行为) 挂载组织级、项目级、个人级 block + folder.

    仅在 rollback (scripts/canary_swap.py --rollback) 路径调用.
    新 agent 创建走 _attach_agent_blocks_only (Phase 1 后的路径, 不挂 folder).
    """
    org = _get_org_resources(db)
    try:
        letta.agents.blocks.attach(agent_id=agent_id, block_id=org["block_id"])
    except Exception as e:
        logging.warning(f"attach org block to {agent_id}: {e}")
    try:
        letta.agents.folders.attach(agent_id=agent_id, folder_id=org["folder_id"])
    except Exception as e:
        logging.warning(f"attach org folder to {agent_id}: {e}")

    proj = db.execute(
        "SELECT project_block_id, project_folder_id FROM projects WHERE project_id = ?",
        (project,),
    ).fetchone()
    if proj:
        try:
            letta.agents.blocks.attach(agent_id=agent_id, block_id=proj["project_block_id"])
        except Exception as e:
            logging.warning(f"attach project block to {agent_id}: {e}")
        try:
            letta.agents.folders.attach(agent_id=agent_id, folder_id=proj["project_folder_id"])
        except Exception as e:
            logging.warning(f"attach project folder to {agent_id}: {e}")

    personal_folder_id = get_or_create_personal_folder(user_id)
    try:
        letta.agents.folders.attach(agent_id=agent_id, folder_id=personal_folder_id)
    except Exception as e:
        logging.warning(f"attach personal folder to {agent_id}: {e}")


def _attach_agent_blocks_only(db, agent_id: str, user_id: str, project: str):
    """Phase 1 新建 agent 路径: 只挂 blocks (org/project), 不挂任何 folder.

    Folder 下线原因: Letta 把 folder 的 directory tree 内联进 system prompt,
    51 份文件的 project (cpm) 就 53K tokens 撑爆 compact, 见 project_capacity_ceilings.md.
    agent 访问文件走 kb 工具 → adapter /internal/project/{pid}/kb/*.
    """
    org = _get_org_resources(db)
    try:
        letta.agents.blocks.attach(agent_id=agent_id, block_id=org["block_id"])
    except Exception as e:
        logging.warning(f"attach org block to {agent_id}: {e}")

    proj = db.execute(
        "SELECT project_block_id FROM projects WHERE project_id = ?",
        (project,),
    ).fetchone()
    if proj and proj["project_block_id"]:
        try:
            letta.agents.blocks.attach(agent_id=agent_id, block_id=proj["project_block_id"])
        except Exception as e:
            logging.warning(f"attach project block to {agent_id}: {e}")


def _get_org_resources(db):
    """获取组织级 Block 和 Folder ID，不存在则自动创建。"""
    row = db.execute(
        "SELECT org_block_id, org_folder_id FROM org_resources WHERE singleton = 1"
    ).fetchone()

    if row and row["org_block_id"] and row["org_folder_id"]:
        return {"block_id": row["org_block_id"], "folder_id": row["org_folder_id"]}

    block = None
    folder = None
    if row and row["org_block_id"]:
        try:
            block = letta.blocks.retrieve(block_id=row["org_block_id"])
        except Exception:
            block = None
    if row and row["org_folder_id"]:
        try:
            folder = letta.folders.retrieve(folder_id=row["org_folder_id"])
        except Exception:
            folder = None

    if not block:
        block = letta.blocks.create(
            label="org_knowledge",
            value="【组织知识】待补充...",
            limit=2000,
            read_only=True,
        )
    if not folder:
        try:
            folder = letta.folders.create(name="org-shared", embedding_config={"embedding_model": "nomic-embed-text", "embedding_endpoint_type": "openai", "embedding_endpoint": "http://ollama:11434/v1", "embedding_dim": 768})
        except ConflictError:
            logging.warning("folder org-shared already exists in Letta, looking up")
            page = letta.folders.list(name="org-shared", limit=1)
            if not page.items:
                raise HTTPException(500, "组织文件夹已存在但查找失败，请联系管理员")
            folder = page.items[0]

    db.execute(
        "INSERT INTO org_resources (singleton, org_block_id, org_folder_id) VALUES (1, ?, ?) "
        "ON CONFLICT(singleton) DO UPDATE SET "
        "org_block_id = excluded.org_block_id, "
        "org_folder_id = excluded.org_folder_id, "
        "updated_at = CURRENT_TIMESTAMP",
        (block.id, folder.id),
    )
    db.commit()
    return {"block_id": block.id, "folder_id": folder.id}
