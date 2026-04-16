"""SQLite 数据库初始化和连接"""
import sqlite3
import os
from config import DB_PATH

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    db = get_db()

    db.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            project_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            desc TEXT DEFAULT '',
            created_by TEXT NOT NULL,
            project_block_id TEXT,
            project_folder_id TEXT,
            folder_quota_mb INTEGER DEFAULT 1024,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS project_members (
            user_id TEXT,
            project_id TEXT,
            role TEXT DEFAULT 'member',
            added_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, project_id)
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS user_agent_map (
            user_id TEXT,
            project_id TEXT,
            agent_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, project_id)
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS user_cache (
            user_id TEXT PRIMARY KEY,
            name TEXT DEFAULT '',
            email TEXT DEFAULT '',
            personal_folder_id TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS org_resources (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            org_block_id TEXT,
            org_folder_id TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            content TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            reviewed_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TIMESTAMP
        )
    """)

    db.commit()
    db.close()
