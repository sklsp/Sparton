"""Apollo — ComfyUI generation domain."""

from app.generation.comfyui_client import ComfyUIClient, ComfyUIError
from app.generation.service import (
    ComfyUIService,
    WorkflowError,
    WorkflowInfo,
    infer_mapping,
    inject_inputs,
    validate_graph,
)

__all__ = [
    "ComfyUIError",
    "ComfyUIClient",
    "ComfyUIService",
    "WorkflowError",
    "WorkflowInfo",
    "infer_mapping",
    "inject_inputs",
    "validate_graph",
]
