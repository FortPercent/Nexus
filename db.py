"""SQLite 数据库初始化和连接"""
import sqlite3
import os
from contextlib import contextmanager
from config import DB_PATH

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@contextmanager
def use_db():
    """Context manager：自动关闭连接，异常时 rollback"""
    conn = get_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

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
            personal_human_block_id TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # 已有库追加列（幂等）
    try:
        db.execute("ALTER TABLE user_cache ADD COLUMN personal_human_block_id TEXT")
    except sqlite3.OperationalError:
        pass  # 列已存在

    db.execute("""
        CREATE TABLE IF NOT EXISTS org_resources (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            org_block_id TEXT,
            org_folder_id TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_mirrors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            letta_file_id TEXT NOT NULL,
            letta_folder_id TEXT NOT NULL,
            knowledge_id TEXT NOT NULL UNIQUE,
            scope TEXT NOT NULL,
            scope_id TEXT DEFAULT '',
            owner_id TEXT DEFAULT '',
            for_user_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            sync_status TEXT DEFAULT 'synced',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(letta_file_id, for_user_id)
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

    db.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            action TEXT NOT NULL,
            scope TEXT DEFAULT '',
            details TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS project_todos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            status TEXT DEFAULT 'open',
            priority TEXT DEFAULT 'medium',
            source TEXT DEFAULT 'manual',
            created_by TEXT NOT NULL,
            assigned_to TEXT,
            due_date DATE,
            cancel_reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            done_at TIMESTAMP,
            done_by TEXT
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_todos_project ON project_todos(project_id, status)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_todos_assigned ON project_todos(assigned_to)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_todos_creator ON project_todos(created_by)")

    # projects 追加 todo_approval_mode 列（幂等）
    try:
        db.execute("ALTER TABLE projects ADD COLUMN todo_approval_mode TEXT DEFAULT 'ai_only'")
    except sqlite3.OperationalError:
        pass  # 列已存在

    db.commit()
    db.close()
