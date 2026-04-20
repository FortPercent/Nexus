"""把 source_passages 链接到 archival 系统

Letta 搜索走 archival_passages（通过 archives_agents），
不走 source_passages。正常上传流程会自动建立链接，
但因 embedding 失败导致链接缺失。本脚本手动补上。

逻辑：
1. 查所有 source → agent 的关联（sources_agents）
2. 为每个 source 创建一个 archive
3. 把 source_passages 复制到 archival_passages（关联到 archive）
4. 建立 archives_agents 关联
"""
import psycopg2
import uuid
import json

conn = psycopg2.connect("postgresql://letta:letta2026@teleai-postgres:5432/letta")
cur = conn.cursor()

ORG_ID = "org-00000000-0000-4000-8000-000000000000"

EMBEDDING_CONFIG = json.dumps({
    "embedding_endpoint_type": "openai",
    "embedding_endpoint": "http://ollama:11434/v1",
    "embedding_model": "nomic-embed-text",
    "embedding_dim": 768,
    "embedding_chunk_size": 300,
    "batch_size": 32
})

# 查所有有 passages 的 source
cur.execute("""
    SELECT DISTINCT sp.source_id, s.name
    FROM source_passages sp
    JOIN sources s ON sp.source_id = s.id
""")
sources_with_passages = cur.fetchall()
print(f"sources with passages: {len(sources_with_passages)}")

# 查 source → agent 关联
cur.execute("SELECT source_id, agent_id FROM sources_agents")
source_agent_map = {}
for source_id, agent_id in cur.fetchall():
    source_agent_map.setdefault(source_id, []).append(agent_id)

# 查已有的 archival_passages 列的向量维度
cur.execute("SELECT embedding FROM archival_passages LIMIT 1")
existing = cur.fetchone()
if existing:
    print(f"archival_passages already has data, checking dim...")
else:
    # 表已经是 4096 了，不需要改
    pass

for source_id, source_name in sources_with_passages:
    agents = source_agent_map.get(source_id, [])
    if not agents:
        print(f"  SKIP {source_name}: no agents linked")
        continue

    # 创建 archive
    archive_id = f"archive-{uuid.uuid4()}"
    cur.execute("""
        INSERT INTO archives (id, name, description, organization_id, embedding_config,
                              is_deleted, metadata_, vector_db_provider, _vector_db_namespace)
        VALUES (%s, %s, %s, %s, %s::json, false, '{}'::json, 'NATIVE', %s)
    """, (archive_id, source_name, f"Mirror of source {source_id}", ORG_ID,
          EMBEDDING_CONFIG, archive_id))
    # 注意：vector_db_provider 改为 NATIVE

    # 复制 source_passages → archival_passages
    cur.execute("""
        SELECT id, text, embedding, embedding_config, metadata_, organization_id, tags
        FROM source_passages WHERE source_id = %s
    """, (source_id,))
    passages = cur.fetchall()

    for p_id, text, embedding, emb_config, metadata, org_id, tags in passages:
        ap_id = f"passage-{uuid.uuid4()}"
        ec = json.dumps(emb_config) if isinstance(emb_config, dict) else (emb_config or EMBEDDING_CONFIG)
        md = json.dumps(metadata) if isinstance(metadata, dict) else (metadata or '{}')
        tg = json.dumps(tags) if isinstance(tags, (dict, list)) else (tags or '[]')
        cur.execute("""
            INSERT INTO archival_passages (id, text, embedding, embedding_config, metadata_,
                                           organization_id, archive_id, is_deleted, tags)
            VALUES (%s, %s, %s, %s::json, %s::json, %s, %s, false, %s::json)
        """, (ap_id, text, embedding, ec, md, org_id or ORG_ID, archive_id, tg))

    # 关联 archive → agents
    for agent_id in agents:
        cur.execute("""
            INSERT INTO archives_agents (archive_id, agent_id, is_owner)
            VALUES (%s, %s, false)
            ON CONFLICT DO NOTHING
        """, (archive_id, agent_id))

    conn.commit()
    print(f"  OK {source_name}: {len(passages)} passages → {len(agents)} agents")

print("\ndone")
conn.close()
