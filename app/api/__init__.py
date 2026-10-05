"""API layer.

Namespaced routers:
/api/auth /api/documents /api/rag /api/chat /api/prompts
/api/agent /api/approvals /api/intelligence /api/products /api/analytics
/api/comfyui /api/generated /api/datasets /api/training /api/admin
/system health, metrics, tools

`create` and `knowledge` are deliberately NOT imported here. They are the
feature-flagged experimental routers, and importing them reaches
sentence-transformers and faiss -- which is why the image was 9.8 GB. A package
`__init__` that re-exports them undoes the lazy import in `app/main.py`,
because `from app.api import health` executes this file first. They are
imported inside the feature-flag branches in `app/main.py` instead.
"""

from app.api import admin as admin_api  # noqa: F401
from app.api import agent_api, auth, ecommerce, health, intelligence  # noqa: F401
