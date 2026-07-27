import asyncio
import json
import logging
import random
import string
from typing import AsyncIterator, Optional
from curl_cffi.requests import AsyncSession

from app.config import KIMIK3_WEB, KIMIK3_API, COMMON_HEADERS
from app.models import AccountState

log = logging.getLogger("kimik3_proxy.client")
IMPERSONATE = "chrome110"


class EmptyResponseError(RuntimeError): pass


class ServerError(RuntimeError): pass


def _random_str(length: int = None) -> str:
    if length is None:
        length = random.randint(8, 11)
    return "".join(random.choices(string.digits, k=length))


# ── 注册客户端 ────────────────────────────────────────────
class Kimik3Register:
    async def run(self) -> Optional[dict]:
        name = _random_str(6)
        email = f"{name}@qq.com"
        password = "pwd" + _random_str(8)

        try:
            async with AsyncSession(impersonate=IMPERSONATE, timeout=30) as session:
                # 1. 注册账号
                reg_payload = {"name": name, "email": email, "password": password}
                reg_resp = await session.post(
                    f"{KIMIK3_API}/auth/sign-up/email",
                    headers={**COMMON_HEADERS, "content-type": "application/json"},
                    json=reg_payload
                )

                if reg_resp.status_code not in (200, 201):
                    log.warning(f"注册接口返回异常: {reg_resp.status_code} - {reg_resp.text}")
                    return None

                # 2. 模拟访问 /settings (关键步骤：促使服务端写入 session cookie)
                await session.get(
                    f"{KIMIK3_WEB}/settings",
                    headers={**COMMON_HEADERS, "priority": "u=0, i", "sec-fetch-dest": "document",
                             "sec-fetch-mode": "navigate"}
                )

                # 3. 验证 session 状态
                sess_resp = await session.get(
                    f"{KIMIK3_API}/auth/get-session",
                    headers=COMMON_HEADERS
                )
                user_id = ""
                if sess_resp.status_code == 200:
                    user_id = sess_resp.json().get("user", {}).get("id", "")

                # 4. 获取初始 credits 余额
                bal = 10
                cred_resp = await session.get(f"{KIMIK3_API}/credits", headers=COMMON_HEADERS)
                if cred_resp.status_code == 200:
                    bal = cred_resp.json().get("data", {}).get("balance", 10)

                # 拼接完整 Cookie 字符串
                cookies_dict = session.cookies.get_dict()
                cookie_str = "; ".join([f"{k}={v}" for k, v in cookies_dict.items()])

            log.info(f"🎉 注册成功: {email} | 初始 Credits: {bal}")
            return {
                "email": email,
                "password": password,
                "name": name,
                "cookie_str": cookie_str,
                "user_id": user_id,
                "healthy": True,
                "disabled": False,
                "usage_count": max(0, 10 - bal),
            }
        except Exception as e:
            log.error(f"Kimik3 注册过程异常: {e}")
            return None


# ── 对话流式 ────────────────────────────────────────────
async def stream_kimik3_response(text: str, acc: AccountState) -> AsyncIterator[str]:
    """向 Kimik3 发起对话请求"""
    async with AsyncSession(impersonate=IMPERSONATE, timeout=120) as session:
        session.cookies.update(acc.cookies_dict())

        # 1. 创建新会话
        chat_resp = await session.post(
            f"{KIMIK3_API}/chat",
            headers={**COMMON_HEADERS, "content-type": "application/json"},
            json={}
        )
        if chat_resp.status_code in (401, 403):
            raise PermissionError(f"cookie_expired:{acc.email}")

        chat_data = chat_resp.json()
        chat_id = chat_data.get("data", {}).get("chat", {}).get("id")
        if not chat_id:
            raise ServerError("Failed to create chat_id")

        # 2. 发送消息并接收 SSE 流
        msg_resp = await session.post(
            f"{KIMIK3_API}/chat/{chat_id}",
            headers={**COMMON_HEADERS, "content-type": "application/json"},
            json={"content": text},
            stream=True
        )

        if msg_resp.status_code in (401, 403):
            raise PermissionError(f"cookie_expired:{acc.email}")

        yielded_any = False

        async for raw_line in msg_resp.aiter_lines():
            line = raw_line.decode("utf-8", errors="ignore") if isinstance(raw_line, bytes) else raw_line
            if not line.startswith("data: "):
                continue

            payload = line[6:].strip()
            if not payload:
                continue

            try:
                data = json.loads(payload)
                if data.get("type") == "delta":
                    yielded_any = True
                    yield data.get("text", "")
                elif data.get("type") == "done":
                    break
            except json.JSONDecodeError:
                pass

        if not yielded_any:
            raise EmptyResponseError(f"empty_response:{acc.email}")