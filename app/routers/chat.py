import asyncio
import copy
import json
import logging
import random
import re
import time
import traceback
import uuid
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.cookie_manager import cm
from app.kimik3_client import (
    stream_kimik3_response,
    QuotaExhaustedError,
    EmptyResponseError,
    ServerError,
)
from app.models import AccountState
from app.routers.model_list import resolve_model, DEFAULT_MODEL

log = logging.getLogger("kimik3_proxy.chat")
router = APIRouter()

MAX_RETRIES     = 4
RETRY_DELAY     = 1.0
RETRY_DELAY_MAX = 3.0

# ══════════════════════════════════════════════════════════
# Function Calling 提示词工程注入
# ══════════════════════════════════════════════════════════
_TC_BEGIN   = "@@TOOL_CALL_BEGIN@@"
_TC_END     = "@@TOOL_CALL_END@@"
_TC_PATTERN = re.compile(
    rf"{re.escape(_TC_BEGIN)}\s*(.*?)\s*{re.escape(_TC_END)}",
    re.DOTALL,
)


def _backoff(attempt: int) -> float:
    delay = min(RETRY_DELAY * (2 ** (attempt - 1)), RETRY_DELAY_MAX)
    return delay + random.uniform(0, delay * 0.1)


# ── 消息预处理 ────────────────────────────────────────────

def _convert_messages(messages: list[dict]) -> list[dict]:
    """
    标准化 messages：
    - tool 角色 → user（带工具返回标注）
    - 保留 list 类型 content
    """
    result = []
    for msg in messages:
        role    = msg.get("role", "user")
        content = msg.get("content", "")
        name    = msg.get("name", "")
        if role == "tool":
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            result.append({
                "role":    "user",
                "content": f"[工具 {name} 返回结果]\n{text}\n请基于这个结果直接回复用户，不要再次调用工具。",
            })
        else:
            result.append({"role": role, "content": content})
    return result


def messages_to_text(messages: list[dict]) -> str:
    """提取 messages 内容转为上游 prompt 格式"""
    parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
        parts.append(f"[{role.capitalize()}]: {content}")
    return "\n".join(parts)


# ── Function Calling 提示词与工具构造 ──────────────────────────

def _annotate_tool_declarations(tools: list[dict]) -> list[dict]:
    """在工具参数 description 里注入【必填】/【可选】标签，提升模型遵从率"""
    annotated = copy.deepcopy(tools)
    for tool in annotated:
        func         = tool if "parameters" in tool else tool.get("function", {})
        params       = func.get("parameters", {})
        required_set = set(params.get("required", []))
        props        = params.get("properties", {})
        for param_name, param_def in props.items():
            tag = "【必填】" if param_name in required_set else "【可选】"
            existing = param_def.get("description", "")
            param_def["description"] = f"{tag} {existing}".strip()
        if required_set:
            params["_required_params_notice"] = (
                f"以下参数为必填，绝对不能省略或传空值：{sorted(required_set)}"
            )
    return annotated


def _validate_tool_call(tc: dict, tool_declarations: list[dict]) -> tuple[bool, list[str]]:
    name = tc.get("name", "")
    args = tc.get("arguments", {})
    for tool in tool_declarations:
        func      = tool if "parameters" in tool else tool.get("function", {})
        func_name = func.get("name", "")
        if func_name != name:
            continue
        params  = func.get("parameters", {})
        required = params.get("required", [])
        missing  = [r for r in required if r not in args or args[r] in (None, "", {}, [])]
        if missing:
            log.warning("tool call [%s] 缺少必填参数: %s", name, missing)
            return False, missing
        return True, []
    log.warning("tool call [%s] 未在工具列表中找到，跳过校验", name)
    return True, []


def _build_tool_system_prompt(tool_declarations: list[dict]) -> str:
    annotated = _annotate_tool_declarations(tool_declarations)
    tool_json = json.dumps(annotated, ensure_ascii=False, indent=2)
    return f"""## 核心设定：外部工具调用系统

你被赋予了调用外部真实系统（工具）的能力。当用户的需求需要真实执行（如画图、查询、搜索）时，你必须精确输出工具调用代码块，而不能假装执行。

### 🚨 绝对禁止的错误模式

❌ 只演戏，不调用（没有输出 `{_TC_BEGIN}` 代码块）
❌ 假装已经拿到结果（没有真正调用工具就给出答案）
❌ 需要参数却传空对象：`"arguments": {{}}` —— 这等于没有调用！

### 🔒 调用前必须完成的自检

在输出任何 `{_TC_BEGIN}` 块之前，逐项确认：
1. 工具名称来自下方【可用工具】列表
2. 每个【必填】参数是否都有具体的、非空的值
3. 如果某个【必填】参数还不知道 → **先向用户提问，不要调用**

### ✅ 正确的调用格式

过渡语（可选）……
{_TC_BEGIN}
{{
  "name": "工具名",
  "arguments": {{
    "参数名": "参数值"
  }}
}}
{_TC_END}

**注意：**
- `{_TC_BEGIN}` 和 `{_TC_END}` 必须独占一行
- 之间只能有纯净的 JSON，不能有 Markdown 代码块标记
- 输出完 `{_TC_END}` 后立即停止，等待系统返回结果
- 【必填】参数不能是空字符串 `""`、`null` 或空对象 `{{}}`

### 📌 当前【可用工具】列表

{tool_json}

### ⚠️ 系统最终警告

- 假装执行而不输出代码块 → 严重惩罚
- 输出代码块但【必填】参数为空或缺失 → 视同未调用，严重惩罚"""


def _parse_tool_calls(text: str) -> tuple[str, list[dict]]:
    """从文本中提取所有工具调用，返回 (去除调用块后的纯文本, tool_calls列表)"""
    tool_calls: list[dict] = []
    for m in _TC_PATTERN.finditer(text):
        raw = m.group(1).strip()
        try:
            obj = json.loads(raw)
            tc  = {
                "id":        f"call_{uuid.uuid4().hex[:12]}",
                "name":      obj.get("name", ""),
                "arguments": obj.get("arguments", {}),
            }
            tool_calls.append(tc)
            log.info("解析到工具调用: %s", tc)
        except json.JSONDecodeError:
            log.warning("无法解析工具调用块: %r", raw)
    clean = _TC_PATTERN.sub("", text).strip()
    return clean, tool_calls


def _check_truncation(
    full_text: str, original_tools: list[dict]
) -> tuple[bool, str, list[dict]]:
    """检测工具调用块是否被上游截断（有 BEGIN 但没有 END）"""
    begin_idx = full_text.find(_TC_BEGIN)
    end_idx   = full_text.find(_TC_END)
    if begin_idx != -1 and end_idx == -1:
        fallback_text = full_text[:begin_idx].strip()
        name_match    = re.search(r'"name"\s*:\s*"([^"]+)"', full_text[begin_idx:])
        next_tools    = original_tools
        if name_match:
            target_name = name_match.group(1)
            filtered    = [
                t for t in original_tools
                if t.get("name") == target_name
                or t.get("function", {}).get("name") == target_name
            ]
            if filtered:
                next_tools = filtered
        return True, fallback_text, next_tools
    return False, "", original_tools


# ── 响应格式化 ────────────────────────────────────────────

def _make_plain_response(text: str, model: str) -> dict:
    return {
        "id":      f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object":  "chat.completion",
        "created": int(time.time()),
        "model":   model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage":   None,
    }


def _make_tool_call_response(tool_calls: list[dict], clean_text: str, model: str) -> dict:
    openai_tool_calls = [
        {
            "id":   tc["id"],
            "type": "function",
            "function": {
                "name":      tc["name"],
                "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
            },
        }
        for tc in tool_calls
    ]
    message: dict[str, Any] = {
        "role":       "assistant",
        "content":    clean_text or None,
        "tool_calls": openai_tool_calls,
    }
    return {
        "id":      f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object":  "chat.completion",
        "created": int(time.time()),
        "model":   model,
        "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}],
        "usage":   None,
    }


def _plain_text_to_sse(text: str, model: str) -> str:
    now     = int(time.time())
    resp_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    def _sse(obj: dict) -> str:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    return "".join([
        _sse({"id": resp_id, "object": "chat.completion.chunk", "created": now, "model": model,
              "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}),
        _sse({"id": resp_id, "object": "chat.completion.chunk", "created": now, "model": model,
              "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}),
        _sse({"id": resp_id, "object": "chat.completion.chunk", "created": now, "model": model,
              "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
        "data: [DONE]\n\n",
    ])


def _make_tool_call_sse(tool_calls: list[dict], clean_text: str, model: str) -> str:
    now     = int(time.time())
    resp_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    def _sse(obj: dict) -> str:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    chunks = [
        _sse({"id": resp_id, "object": "chat.completion.chunk", "created": now, "model": model,
              "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}),
    ]
    if clean_text:
        chunks.append(
            _sse({"id": resp_id, "object": "chat.completion.chunk", "created": now, "model": model,
                  "choices": [{"index": 0, "delta": {"content": clean_text}, "finish_reason": None}]})
        )
    openai_tool_calls_delta = [
        {
            "index": i,
            "id":    tc["id"],
            "type":  "function",
            "function": {
                "name":      tc["name"],
                "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
            },
        }
        for i, tc in enumerate(tool_calls)
    ]
    chunks.append(
        _sse({"id": resp_id, "object": "chat.completion.chunk", "created": now, "model": model,
              "choices": [{"index": 0, "delta": {"tool_calls": openai_tool_calls_delta}, "finish_reason": None}]})
    )
    chunks.append(
        _sse({"id": resp_id, "object": "chat.completion.chunk", "created": now, "model": model,
              "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
    )
    chunks.append("data: [DONE]\n\n")
    return "".join(chunks)


# ══════════════════════════════════════════════════════════
# 主路由
# ══════════════════════════════════════════════════════════

@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "无效的 JSON 请求体")

    messages        = body.get("messages", [])
    stream          = body.get("stream", False)
    requested_model = body.get("model", DEFAULT_MODEL)
    tools           = body.get("tools", [])
    has_tools       = bool(tools)

    if not messages:
        raise HTTPException(400, "messages 不能为空")

    upstream_model = resolve_model(requested_model)

    # 1. 标准化消息
    clean_messages = _convert_messages(messages)

    # 2. 如果携带 tools，注入提示词
    def _prepare_prompt(current_tools: list[dict]) -> str:
        msgs = list(clean_messages)
        if has_tools and current_tools:
            tool_sys   = _build_tool_system_prompt(current_tools)
            insert_pos = next((i + 1 for i, m in enumerate(msgs) if m.get("role") == "system"), 0)
            msgs.insert(insert_pos, {"role": "system", "content": tool_sys})
        return messages_to_text(msgs)

    log.info(
        "chat | model=%s→%s stream=%s msgs=%d tools=%d",
        requested_model, upstream_model, stream, len(clean_messages), len(tools)
    )

    # ══ 流式且不带 tools：实时透明转发 ══════════════════════════
    if stream and not has_tools:
        async def direct_sse_generator():
            acc = None
            gen = None
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    acc = await cm.get_next()
                    prompt_text = _prepare_prompt([])
                    gen = stream_kimik3_response(prompt_text, acc, model=upstream_model)
                    first_chunk = await gen.__anext__()

                    resp_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
                    now = int(time.time())

                    def _sse(obj):
                        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

                    yield _sse({
                        "id": resp_id,
                        "object": "chat.completion.chunk",
                        "created": now,
                        "model": requested_model,
                        "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
                    })
                    yield _sse({
                        "id": resp_id,
                        "object": "chat.completion.chunk",
                        "created": now,
                        "model": requested_model,
                        "choices": [{"index": 0, "delta": {"content": first_chunk}, "finish_reason": None}],
                    })

                    async for chunk in gen:
                        yield _sse({
                            "id": resp_id,
                            "object": "chat.completion.chunk",
                            "created": now,
                            "model": requested_model,
                            "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
                        })

                    yield _sse({
                        "id": resp_id,
                        "object": "chat.completion.chunk",
                        "created": now,
                        "model": requested_model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    })
                    yield "data: [DONE]\n\n"

                    cm.record_usage(acc)
                    return
                except (PermissionError, QuotaExhaustedError) as e:
                    reason = "cookie_expired" if isinstance(e, PermissionError) else "quota_exhausted"
                    log.warning(f"stream attempt {attempt} 账号 [{acc.email if acc else '?'}] {reason}: {e}")
                    if acc:
                        acc = await cm.mark_failed_and_get_replacement(acc, reason)
                except Exception as e:
                    log.warning(f"stream attempt {attempt}/{MAX_RETRIES} 失败: {e}")
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(_backoff(attempt))
                    else:
                        err_payload = {"error": {"message": str(e), "type": "server_error"}}
                        yield f"data: {json.dumps(err_payload, ensure_ascii=False)}\n\n"
                        return

        return StreamingResponse(
            direct_sse_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ══ 携带 tools 的流式处理 / 非流式处理 ═══════════════════════
    # 当有 tools 时，需要收集全量响应文本以解析 @@TOOL_CALL_BEGIN@@ 块
    last_error: Exception = RuntimeError("未知错误")
    current_tools         = list(tools)
    acc: Optional[AccountState] = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            acc = await cm.get_next()
        except HTTPException as e:
            if stream:
                return StreamingResponse(
                    iter([f'data: {{"error": "无可用账号: {e.detail}"}}\n\n']),
                    media_type="text/event-stream"
                )
            return JSONResponse(status_code=500, content={"error": {"message": f"无可用账号: {e.detail}"}})

        try:
            prompt_text = _prepare_prompt(current_tools)
            gen = stream_kimik3_response(prompt_text, acc, model=upstream_model)

            parts = []
            async for chunk in gen:
                parts.append(chunk)
            full_text = "".join(parts)

            if has_tools:
                # 截断检测
                is_truncated, fallback_text, next_tools = _check_truncation(full_text, tools)
                if is_truncated:
                    if attempt < MAX_RETRIES:
                        current_tools = next_tools
                        log.warning("tools attempt %d 工具块被截断，缩减 tools 重试", attempt)
                        continue
                    else:
                        log.error("tools 所有尝试均被截断，降级返回过渡语")
                        cm.record_usage(acc)
                        if stream:
                            return StreamingResponse(
                                iter([_plain_text_to_sse(fallback_text, requested_model)]),
                                media_type="text/event-stream"
                            )
                        return JSONResponse(content=_make_plain_response(fallback_text, requested_model))

                clean_text, tool_calls = _parse_tool_calls(full_text)

                if tool_calls:
                    invalid = []
                    for tc in tool_calls:
                        valid, missing = _validate_tool_call(tc, tools)
                        if not valid:
                            invalid.append((tc["name"], missing))
                    if invalid:
                        if attempt < MAX_RETRIES:
                            log.warning("tools attempt %d 参数不完整 %s，重试", attempt, invalid)
                            continue
                        else:
                            log.error("tools 参数仍不完整 %s，降级返回文本", invalid)
                            cm.record_usage(acc)
                            if stream:
                                return StreamingResponse(
                                    iter([_plain_text_to_sse(clean_text or full_text, requested_model)]),
                                    media_type="text/event-stream"
                                )
                            return JSONResponse(content=_make_plain_response(clean_text or full_text, requested_model))

                cm.record_usage(acc)

                if stream:
                    if tool_calls:
                        return StreamingResponse(
                            iter([_make_tool_call_sse(tool_calls, clean_text, requested_model)]),
                            media_type="text/event-stream"
                        )
                    else:
                        return StreamingResponse(
                            iter([_plain_text_to_sse(clean_text or full_text, requested_model)]),
                            media_type="text/event-stream"
                        )
                else:
                    if tool_calls:
                        return JSONResponse(content=_make_tool_call_response(tool_calls, clean_text, requested_model))
                    else:
                        return JSONResponse(content=_make_plain_response(clean_text or full_text, requested_model))

            # 非流式且无 tools 的普通场景
            cm.record_usage(acc)
            return JSONResponse(content=_make_plain_response(full_text, requested_model))

        except (PermissionError, QuotaExhaustedError) as e:
            last_error = e
            reason = "cookie_expired" if isinstance(e, PermissionError) else "quota_exhausted"
            log.warning("attempt %d 账号 [%s] %s，换号重试", attempt, acc.email if acc else "?", reason)
            if acc:
                try:
                    acc = await cm.mark_failed_and_get_replacement(acc, reason)
                except Exception:
                    pass
        except Exception as e:
            last_error = e
            log.warning("attempt %d/%d failed: %s", attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(_backoff(attempt))

    traceback.print_exc()
    if stream:
        return StreamingResponse(
            iter([f'data: {{"error": "{last_error}"}}\n\n']),
            media_type="text/event-stream"
        )
    return JSONResponse(status_code=500, content={"error": {"message": str(last_error)}})
