"""适配层配置 —— 从环境变量读取，敏感值无默认值"""
import os

ADAPTER_API_KEY = os.environ["ADAPTER_API_KEY"]
OPENWEBUI_JWT_SECRET = os.environ["OPENWEBUI_JWT_SECRET"]
LETTA_BASE_URL = os.getenv("LETTA_BASE_URL", "http://localhost:8283")
ORG_ADMIN_EMAILS = [e.strip() for e in os.getenv("ORG_ADMIN_EMAILS", "").split(",") if e.strip()]
DEFAULT_FOLDER_QUOTA_MB = int(os.getenv("DEFAULT_FOLDER_QUOTA_MB", "1024"))
DB_PATH = os.getenv("DB_PATH", "/data/serving/adapter/adapter.db")

# Open WebUI admin 凭证（用于查用户详情）
OPENWEBUI_URL = os.getenv("OPENWEBUI_URL", "http://172.17.0.1:3000")
OPENWEBUI_ADMIN_EMAIL = os.environ["OPENWEBUI_ADMIN_EMAIL"]
OPENWEBUI_ADMIN_PASSWORD = os.environ["OPENWEBUI_ADMIN_PASSWORD"]

# vLLM 配置
VLLM_ENDPOINT = os.environ["VLLM_ENDPOINT"]
VLLM_API_KEY = os.environ["VLLM_API_KEY"]
