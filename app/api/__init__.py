"""API layer.

Namespaced routers:
/api/auth /api/documents /api/rag /api/chat /api/prompts
/api/agent /api/approvals /api/intelligence /api/products /api/analytics
/api/comfyui /api/generated /api/datasets /api/training /api/admin
/system health, metrics, tools
"""

from app.api import admin as admin_api  # noqa: F401
from app.api import agent_api, auth, create, ecommerce, health, intelligence, knowledge  # noqa: F401
