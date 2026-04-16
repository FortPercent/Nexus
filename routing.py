"""路由模块 —— user_id + project → agent_id，含双向校验和知识挂载"""
import logging
import sqlite3
from fastapi import HTTPException
from letta_client import Letta, ConflictError
from config import LETTA_BASE_URL, VLLM_ENDPOINT, DB_PATH
from db import get_db

letta = Letta(base_url=LETTA_BASE_URL)

# 知识建议工具缓存
_suggest_tool_id = None


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
            resp = urllib.request.urlopen(req, timeout=5)
            return "已提交知识建议，等待项目管理员审核"
        except Exception as e:
            return f"提交失败：{e}"

    tool = letta.tools.upsert_from_function(func=suggest_project_knowledge)
    _suggest_tool_id = tool.id
    logging.info(f"suggest_project_knowledge tool: {tool.id}")
    return _suggest_tool_id


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

        # 获取知识建议工具
        suggest_tool_id = None
        try:
            suggest_tool_id = _get_suggest_tool_id()
        except Exception as e:
            logging.warning(f"failed to get suggest tool: {e}")

        agent = letta.agents.create(
            name=f"user-{user_id}-{project}",
            model="openai/Qwen3.5-122B-A10B",
            metadata={"owner": user_id, "project": project},
            tool_ids=[suggest_tool_id] if suggest_tool_id else [],
            memory_blocks=[
                {"label": "human", "value": "(新用户，信息未知)"},
                {
                    "label": "persona",
                    "value": (
                        "你是一个有记忆的AI办公助手。\n"
                        "- 记住用户告诉你的个人信息（存到 human block）\n"
                        "- 当用户提到对整个项目团队有价值的信息时（如技术栈变更、里程碑完成、架构决策），"
                        "用 suggest_project_knowledge 工具提交为项目知识建议\n"
                        "- 个人偏好存 human block，项目信息提交建议，不要搞混"
                    ),
                },
            ],
            llm_config={
                "model": "Qwen3.5-122B-A10B",
                "model_endpoint_type": "openai",
                "model_endpoint": VLLM_ENDPOINT,
                "context_window": 32000,
                "enable_reasoner": False,
            },
        )

        _attach_agent_resources(db, agent.id, user_id, project)

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

    attached = 0
    for row in rows:
        changed = False
        try:
            letta.agents.blocks.attach(agent_id=row["agent_id"], block_id=resources["block_id"])
            changed = True
        except Exception:
            pass
        try:
            letta.agents.folders.attach(agent_id=row["agent_id"], folder_id=resources["folder_id"])
            changed = True
        except Exception:
            pass
        if changed:
            attached += 1
    return attached


def _attach_agent_resources(db, agent_id: str, user_id: str, project: str):
    """挂载组织级、项目级、个人级知识到 Agent。"""
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
