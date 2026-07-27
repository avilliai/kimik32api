from fastapi import APIRouter
router = APIRouter()

SUPPORTED_MODELS = ["kimi-k3", "gpt-3.5-turbo", "gpt-4"]
DEFAULT_MODEL = "kimi-k3"

@router.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "created": 1700000000, "owned_by": "kimik3"} for m in SUPPORTED_MODELS]
    }