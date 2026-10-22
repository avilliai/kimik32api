from fastapi import APIRouter
from app.config import SUPPORTED_MODELS, DEFAULT_MODEL, MODEL_MAPPING

router = APIRouter()


def resolve_model(requested_model: str | None) -> str:
    """将客户端传入的模型名映射到上游支持的真实模型"""
    if not requested_model:
        return DEFAULT_MODEL
    req_lower = requested_model.lower().strip()
    return MODEL_MAPPING.get(req_lower, req_lower if req_lower in SUPPORTED_MODELS else DEFAULT_MODEL)


@router.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": m,
                "object": "model",
                "created": 1700000000,
                "owned_by": "kimik3-proxy",
                "permission": [],
                "root": m,
                "parent": None,
            }
            for m in SUPPORTED_MODELS
        ],
    }
