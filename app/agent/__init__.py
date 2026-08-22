"""Unified Athena agent runtime.

One agent for all domains. Tools are namespaced by domain
(documents.*, ecommerce.*, research.*, generation.*, datasets.*,
training.*, models.*, projects.*) and enforced server-side:
authentication, RBAC, tenant isolation, project access, approval
requirements, argument validation, bounded retry, strict grounding.
The LLM is not authoritative; the backend is.
"""
