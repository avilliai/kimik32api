import asyncio
import json
import logging
import random
import string
from typing import AsyncIterator, Optional
from curl_cffi.requests import AsyncSession

from app.config import (
    KIMIK3_WEB,
    KIMIK3_API,
    COMMON_HEADERS,
    PROXY,
    REGISTER_EMAIL_TYPE,
    DEFAULT_MODEL,
    LOCAL_TURNSTILE_URL,
    YESCAPTCHA_KEY,
)
from app.models import AccountState
from app.email_service import EmailService
from app.outlook_email_service import outlook_pool

log = logging.getLogger("kimik3_proxy.client")
IMPERSONATE = "chrome110"


class EmptyResponseError(RuntimeError):
    pass


class ServerError(RuntimeError):
    pass


class QuotaExhaustedError(RuntimeError):
    pass


def _random_str(length: int = 8, only_number: bool = False) -> str:
    if only_number:
        return "".join(random.choices(string.digits, k=length))
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


async def _solve_turnstile_if_needed(sitekey: str, page_url: str) -> Optional[str]:
    """如果服务端要求 Turnstile，尝试通过本地 solver 求解"""
    if not sitekey:
        return None
    try:
        async with AsyncSession(timeout=15) as s:
            resp = await s.post(
                f"{LOCAL_TURNSTILE_URL}/turnstile",
                json={"sitekey": sitekey, "url": page_url},
            )
            if resp.status_code == 200:
                data = resp.json()
                token = data.get("token") or data.get("response")
                if token:
                    return token
    except Exception as e:
        log.warning(f"Turnstile 本地求解失败或未启动服务: {e}")
    return None


# ── 注册客户端 ────────────────────────────────────────────
class Kimik3Register:
    def __init__(self):
        self.email_service = EmailService()

    async def _pick_email(self) -> tuple[str, str]:
        """按策略选择邮箱：outlook 账号文件轮询 -> 域名邮箱 -> 随机合法邮箱"""
        # 1. 尝试使用真实的 Outlook 账号
        if REGISTER_EMAIL_TYPE in ("auto", "outlook"):
            try:
                acc = await outlook_pool.get_next_account()
                if acc and acc.address:
                    return acc.address, acc.password or ("pwd" + _random_str(8))
            except Exception as e:
                log.warning(f"获取 Outlook 账号失败: {e}")

        # 2. 尝试使用 Cloudflare Worker 域名邮箱
        if REGISTER_EMAIL_TYPE in ("auto", "domain"):
            try:
                jwt, address = await asyncio.to_thread(self.email_service.create_email)
                if address:
                    return address, "pwd" + _random_str(8)
            except Exception as e:
                log.warning(f"创建域名邮箱失败: {e}")

        # 3. 兜底方案：使用合法服务商前缀（绝不用已封禁的 qq.com）
        provider = random.choice(["outlook.com", "hotmail.com", "gmail.com"])
        fake_email = f"usr_{_random_str(8)}@{provider}"
        return fake_email, "pwd" + _random_str(8)

    async def run(self) -> Optional[dict]:
        email, password = await self._pick_email()
        name = f"user_{_random_str(6)}"

        try:
            async with AsyncSession(impersonate=IMPERSONATE, proxy=PROXY, timeout=30) as session:
                # 1. 检查服务端配置 (防变动/检测 turnstile 与邮箱验证需求)
                captcha_token = None
                try:
                    cfg_resp = await session.get(f"{KIMIK3_API}/config/public", headers=COMMON_HEADERS)
                    if cfg_resp.status_code == 200:
                        cfg_data = cfg_resp.json().get("data", {})
                        if cfg_data.get("turnstile_enabled") == "true":
                            sitekey = cfg_data.get("turnstile_sitekey")
                            captcha_token = await _solve_turnstile_if_needed(sitekey, f"{KIMIK3_WEB}/sign-up")
                except Exception as e:
                    log.warning(f"读取上游配置警告: {e}")

                # 2. 发起注册请求
                reg_payload = {"name": name, "email": email, "password": password}
                reg_headers = {**COMMON_HEADERS, "content-type": "application/json"}
                if captcha_token:
                    reg_headers["x-captcha-response"] = captcha_token

                reg_resp = await session.post(
                    f"{KIMIK3_API}/auth/sign-up/email",
                    headers=reg_headers,
                    json=reg_payload
                )

                if reg_resp.status_code not in (200, 201):
                    log.warning(f"注册接口返回异常: {reg_resp.status_code} - {reg_resp.text}")
                    return None

                reg_json = reg_resp.json()
                user_id = reg_json.get("user", {}).get("id", "")

                # 3. 访问 /api/auth/get-session 确保 session token 正常写入
                sess_resp = await session.get(f"{KIMIK3_API}/auth/get-session", headers=COMMON_HEADERS)
                if sess_resp.status_code == 200 and not user_id:
                    user_id = sess_resp.json().get("user", {}).get("id", "")

                # 4. 获取初始 credits 余额
                bal = 10
                try:
                    cred_resp = await session.get(f"{KIMIK3_API}/credits", headers=COMMON_HEADERS)
                    if cred_resp.status_code == 200:
                        bal = cred_resp.json().get("data", {}).get("balance", 10)
                except Exception:
                    pass

                # 拼接完整 Cookie 字符串
                cookies_dict = session.cookies.get_dict()
                cookie_str = "; ".join([f"{k}={v}" for k, v in cookies_dict.items()])

            log.info(f"🎉 注册成功: {email} | 初始 Credits: {bal} | Cookie 长度: {len(cookie_str)}")
            return {
                "email": email,
                "password": password,
                "name": name,
                "cookie_str": cookie_str,
                "user_id": user_id,
                "healthy": True,
                "disabled": False,
                "usage_count": 0,
            }
        except Exception as e:
            log.error(f"Kimik3 注册过程异常: {e}")
            return None


# ── 对话流式 ────────────────────────────────────────────
async def stream_kimik3_response(text: str, acc: AccountState, model: str = DEFAULT_MODEL) -> AsyncIterator[str]:
    """向 Kimik3 发起对话请求并返回流式文本"""
    async with AsyncSession(impersonate=IMPERSONATE, proxy=PROXY, timeout=120) as session:
        session.cookies.update(acc.cookies_dict())

        # 1. 创建新会话
        chat_resp = await session.post(
            f"{KIMIK3_API}/chat",
            headers={**COMMON_HEADERS, "content-type": "application/json"},
            json={}
        )
        if chat_resp.status_code in (401, 403):
            raise PermissionError(f"cookie_expired:{acc.email}")

        if chat_resp.status_code not in (200, 201):
            raise ServerError(f"创建对话会话失败: HTTP {chat_resp.status_code} - {chat_resp.text[:100]}")

        chat_data = chat_resp.json()
        chat_id = chat_data.get("data", {}).get("chat", {}).get("id")
        if not chat_id:
            raise ServerError("Failed to create chat_id: empty chat id in response")

        # 2. 发送消息并接收 SSE 流
        payload = {
            "content": text,
            "model": model,
            "attachments": []
        }

        msg_resp = await session.post(
            f"{KIMIK3_API}/chat/{chat_id}",
            headers={**COMMON_HEADERS, "content-type": "application/json"},
            json=payload,
            stream=True
        )

        if msg_resp.status_code in (401, 403):
            raise PermissionError(f"cookie_expired:{acc.email}")

        if msg_resp.status_code not in (200, 201):
            raise ServerError(f"发送消息失败: HTTP {msg_resp.status_code} - {msg_resp.text[:100]}")

        yielded_any = False

        try:
            async for raw_line in msg_resp.aiter_lines():
                line = raw_line.decode("utf-8", errors="ignore") if isinstance(raw_line, bytes) else raw_line
                if not line or not line.startswith("data:"):
                    continue

                line_payload = line[5:].strip()
                if not line_payload:
                    continue
                if line_payload == "[DONE]":
                    break

                try:
                    data = json.loads(line_payload)
                    msg_type = data.get("type") or data.get("t")

                    if msg_type == "delta":
                        chunk_text = data.get("text", "")
                        if chunk_text:
                            yielded_any = True
                            yield chunk_text

                    elif msg_type == "gate":
                        gate_status = data.get("status")
                        if gate_status in ("payment_required", "free_limit_reached"):
                            raise QuotaExhaustedError(f"gate:{gate_status}:{acc.email}")
                        elif gate_status == "login_required":
                            raise PermissionError(f"cookie_expired:{acc.email}")

                    elif msg_type == "error":
                        err_msg = data.get("message", "")
                        if "payment_required" in err_msg or "free_limit" in err_msg:
                            raise QuotaExhaustedError(f"quota_exhausted:{acc.email}")
                        elif "login_required" in err_msg or "unauthorized" in err_msg.lower():
                            raise PermissionError(f"cookie_expired:{acc.email}")
                        # 如果已有内容输出，且是上游 evolink 结尾的 null reading 报错，视为正常结束
                        if yielded_any and "Cannot read properties" in err_msg:
                            break
                        if not yielded_any:
                            raise ServerError(f"上游错误: {err_msg}")

                    elif msg_type == "done":
                        break

                except json.JSONDecodeError:
                    pass

        except (QuotaExhaustedError, PermissionError, ServerError):
            raise
        except Exception as e:
            if not yielded_any:
                raise EmptyResponseError(f"流异常中途中断: {e}") from e

        if not yielded_any:
            raise EmptyResponseError(f"empty_response:{acc.email}")
