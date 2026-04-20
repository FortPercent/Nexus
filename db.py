"""SQLite 数据库初始化和连接。

sync `use_db()` 用于 startup / 非 async 脚本（init_db、migrate 脚本等）。
async `use_db_async()` 用于 FastAPI async 路由，避免同步 I/O 阻塞 event loop。
两者共享同一个 DB_PATH，底层都是 SQLite 文件。
"""
import sqlite3
import os
from contextlib import contextmanager, asynccontextmanager
from config import DB_PATH

import aiosqlite

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@contextmanager
def use_db():
    """同步 context manager：用于 init_db / 启动钩子 / 非 async 脚本。
    异常时 rollback；commit 在 __exit__ 自动做。"""
    conn = get_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@asynccontextmanager
async def use_db_async():
    """异步 context manager：用于 FastAPI async 路由，不阻塞 event loop。
    默认启用 WAL 模式 + 更大 cache，读写可并行。
    用法：
        async with use_db_async() as db:
            async with db.execute("SELECT ...") as cur:
                rows = await cur.fetchall()
    """
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    # WAL 模式让读不阻塞写，大幅提升并发读性能
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.execute("PRAGMA cache_size=-20000")  # 20MB page cache
    try:
        yield conn
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.close()


# 同步版也启 WAL（init_db 里全局启一次就够了，后续连接继承）
_wal_enabled = False


def _ensure_wal():
    global _wal_enabled
    if _wal_enabled:
        return
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.commit()
    finally:
        conn.close()
    _wal_enabled = True

def init_db():
    _ensure_wal()
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

    # 知识层重构 Phase 1 新表: 目录即知识库的索引 (非真相源, 真相是盘上文件)
    # source='legacy' = backfill 从 Letta file_contents.text 导的存量
    # source='current' = Phase 2 后用户新上传, adapter 拦 WebUI Phase 2 落盘的
    # quality='clean' / 'cid_dirty' (pdf 解析乱码) / 'legacy_dirty' 通用过渡态
    db.execute("""
        CREATE TABLE IF NOT EXISTS project_files (
            project_id    TEXT NOT NULL,
            scope         TEXT NOT NULL,      -- 'project' / 'personal' / 'org'
            scope_id      TEXT DEFAULT '',    -- personal 时是 user_id, 其他空
            file_name     TEXT NOT NULL,      -- 盘上实际文件名 (可能带 .md 后缀)
            display_name  TEXT NOT NULL,      -- UI / agent 看到的名字 (foo.docx.md → foo.docx)
            source        TEXT NOT NULL,      -- 'legacy' / 'current'
            quality       TEXT DEFAULT 'clean',
            size_bytes    INTEGER DEFAULT 0,
            webui_file_id TEXT DEFAULT '',    -- 新上传才有, 关联 webui.file.id
            uploaded_by   TEXT DEFAULT '',
            uploaded_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (project_id, scope, scope_id, file_name)
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_pfiles_project ON project_files(project_id, scope)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_pfiles_source ON project_files(source)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_pfiles_webui ON project_files(webui_file_id)")

    db.commit()
    db.close()
