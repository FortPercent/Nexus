"""重新 embed 所有 error/pending 状态的文件

读 file_contents 文本 → 调 Ollama embedding → 写入 source_passages
"""
import psycopg2
import httpx
import json
import uuid
import sys

conn = psycopg2.connect("postgresql://letta:letta2026@teleai-postgres:5432/letta")
cur = conn.cursor()

cur.execute("SELECT f.id, f.file_name, f.source_id FROM files f WHERE f.processing_status IN (%s, %s)", ("error", "pending"))
files = cur.fetchall()
print(f"files to process: {len(files)}")

processed = 0
failed = 0
for file_id, file_name, source_id in files:
    cur.execute("SELECT id, text FROM file_contents WHERE file_id = %s", (file_id,))
    chunks = cur.fetchall()
    if not chunks:
        print(f"  SKIP {file_name}: no chunks")
        continue

    ok = 0
    for chunk_id, text in chunks:
        if not text or not text.strip():
            continue
        try:
            resp = httpx.post("http://ollama:11434/v1/embeddings",
                json={"model": "nomic-embed-text", "input": text[:2000]}, timeout=30)
            if resp.status_code != 200:
                continue
            emb = resp.json()["data"][0]["embedding"]  # 768 dim
            # 补零到 4096 维（Letta 表是 vector(4096)）
            emb = emb + [0.0] * (4096 - len(emb))

            passage_id = f"passage-{uuid.uuid4()}"
            embedding_config = json.dumps({
                "embedding_endpoint_type": "openai",
                "embedding_endpoint": "http://ollama:11434/v1",
                "embedding_model": "nomic-embed-text",
                "embedding_dim": 768,
                "embedding_chunk_size": 300,
                "batch_size": 32
            })
            cur.execute(
                "INSERT INTO source_passages (id, text, embedding, embedding_config, file_id, source_id, file_name, organization_id, is_deleted, metadata_, tags) "
                "VALUES (%s, %s, %s::vector, %s::json, %s, %s, %s, %s, false, '{}'::json, '[]'::json)",
                (passage_id, text[:2000], str(emb), embedding_config, file_id, source_id, file_name,
                 "org-00000000-0000-4000-8000-000000000000"))
            ok += 1
        except Exception as e:
            print(f"    error: {str(e)[:80]}")

    if ok > 0:
        cur.execute("UPDATE files SET processing_status = 'completed', chunks_embedded = %s WHERE id = %s", (ok, file_id))
        conn.commit()
        processed += 1
        print(f"  OK {file_name}: {ok}/{len(chunks)} passages")
    else:
        failed += 1
        print(f"  FAIL {file_name}")

print(f"\ndone: {processed} ok, {failed} failed, {len(files)} total")
conn.close()
