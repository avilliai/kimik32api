import time
import uuid
import json
import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.cookie_manager import cm
from app.kimik3_client import stream_kimik3_response
from app.models import AccountState
from app.routers.model_list import DEFAULT_MODEL

log = logging.getLogger("kimik3_proxy.chat")
router = APIRouter()


def messages_to_text(messages: list[dict]) -> str:
    parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
        parts.append(f"[{role.capitalize()}]: {content}")
    return "\n".join(parts)


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    stream = body.get("stream", False)
    model = body.get("model", DEFAULT_MODEL)

    if not messages:
        raise HTTPException(400, "messages 不能为空")

    prompt_text = messages_to_text(messages)

    # 无限重试外壳 (最多试3个号)
    async def _execute_with_retry() -> tuple[Optional[AccountState], any]:
        acc = await cm.get_next()
        for attempt in range(6):
            try:
                # 只获取生成器并不消费
                gen = stream_kimik3_response(prompt_text, acc)
                # 读取第一口判断是否成功
                first_chunk = await gen.__anext__()

                async def _merged_gen():
                    yield first_chunk
                    async for chunk in gen: yield chunk

                return acc, _merged_gen()
            except PermissionError:
                acc = await cm.mark_failed_and_get_replacement(acc, "cookie_expired")
            except StopAsyncIteration:
                acc = await cm.mark_failed_and_get_replacement(acc, "empty_response")
            except Exception as e:
                log.warning(f"Request error on {acc.email}: {e}")
                acc = await cm.mark_failed_and_get_replacement(acc, "unknown_error")
        raise RuntimeError("尝试了多个账号均失败")

    if stream:
        async def sse_generator():
            try:
                acc, gen = await _execute_with_retry()
                resp_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
                now = int(time.time())

                def _sse(obj):
                    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

                yield _sse({"id": resp_id, "object": "chat.completion.chunk", "created": now, "model": model,
                            "choices": [
                                {"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]})

                async for text in gen:
                    yield _sse({"id": resp_id, "object": "chat.completion.chunk", "created": now, "model": model,
                                "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]})

                yield _sse({"id": resp_id, "object": "chat.completion.chunk", "created": now, "model": model,
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                yield "data: [DONE]\n\n"
                cm.record_usage(acc)
            except Exception as e:
                yield f'data: {{"error": "{e}"}}\n\n'

        return StreamingResponse(sse_generator(), media_type="text/event-stream")

    # 非流式
    try:
        acc, gen = await _execute_with_retry()
        parts = []
        async for chunk in gen: parts.append(chunk)
        full_text = "".join(parts)
        cm.record_usage(acc)
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": full_text}, "finish_reason": "stop"}]
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": {"message": str(e)}})