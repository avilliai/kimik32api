import asyncio
import imaplib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email import message_from_string
from pathlib import Path
from typing import Optional
import quopri
import requests

from app.config import PROXY, OUTLOOK_FILE, OUTLOOK_POINTER_FILE, OUTLOOK_TOKEN_URL, OUTLOOK_IMAP_HOSTS, OUTLOOK_IMAP_PORT

log = logging.getLogger("kimik3_proxy.outlook")


@dataclass
class OutlookAccount:
    address: str
    password: str
    client_id: str
    refresh_token: str
    index: int


def get_outlook_access_token(client_id: str, refresh_token: str, retries: int = 4) -> str:
    url = OUTLOOK_TOKEN_URL
    proxies = {"http": PROXY, "https": PROXY} if PROXY else None
    last_exc = None

    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(
                url,
                data={
                    "client_id": client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                proxies=proxies,
                timeout=25,
            )
            resp.raise_for_status()
            data = resp.json()
            if "access_token" in data:
                return data["access_token"]
        except Exception as e:
            last_exc = e
            if attempt < retries:
                time.sleep(1.5 * attempt)

    raise RuntimeError(f"Failed to obtain Outlook access token: {last_exc}")


def get_folder_highest_uids(
    email: str, client_id: str, refresh_token: str, access_token: str = None
) -> dict[str, int]:
    result = {'"Junk"': 0, "INBOX": 0}
    try:
        token = access_token or get_outlook_access_token(client_id, refresh_token)
    except Exception as e:
        log.warning(f"[{email}] 获取基线 UID 时换取 token 失败: {e}")
        return result

    hosts = [h.strip() for h in OUTLOOK_IMAP_HOSTS.replace(",", ";").split(";") if h.strip()]
    port = OUTLOOK_IMAP_PORT

    for host in hosts:
        c = None
        try:
            c = imaplib.IMAP4_SSL(host, port, timeout=25)
            auth = f"user={email}\x01auth=Bearer {token}\x01\x01".encode()
            typ, _ = c.authenticate("XOAUTH2", lambda _: auth)
            if typ != "OK":
                continue

            for folder in ['"Junk"', "INBOX"]:
                try:
                    typ, _ = c.select(folder, readonly=True)
                    if typ == "OK":
                        typ, ids = c.search(None, "ALL")
                        if typ == "OK" and ids[0].split():
                            result[folder] = int(ids[0].split()[-1])
                except Exception:
                    pass
            return result
        except Exception:
            pass
        finally:
            if c:
                try:
                    c.logout()
                except Exception:
                    pass

    return result


def extract_verification_code_or_url(raw_email: str) -> Optional[str]:
    """从邮件原始内容中提取验证码或验证链接"""
    if not raw_email:
        return None

    try:
        msg = message_from_string(raw_email)
        bodies = []
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype in ("text/plain", "text/html"):
                    p = part.get_payload(decode=True)
                    if p:
                        charset = part.get_content_charset() or "utf-8"
                        bodies.append(p.decode(charset, errors="ignore"))
        else:
            p = msg.get_payload(decode=True)
            if p:
                charset = msg.get_content_charset() or "utf-8"
                bodies.append(p.decode(charset, errors="ignore"))
            else:
                raw_p = msg.get_payload()
                if isinstance(raw_p, str):
                    bodies.append(raw_p)

        for body in bodies:
            # 1. 匹配验证链接
            url_match = re.search(r'https?://[^\s<>"\']+/verify-email[^\s<>"\']*', body)
            if url_match:
                return url_match.group(0)

            # 2. 匹配 6 位数字验证码
            m = re.search(r'\b([0-9]{6})\b', body)
            if m:
                return m.group(1)

            # 3. 匹配 4-8 位验证码
            m = re.search(r'verification\s+code[\s\S]{0,100}?([0-9]{4,8})', body, re.IGNORECASE)
            if m:
                return m.group(1)
    except Exception as e:
        log.warning(f"解析邮件失败: {e}")
    return None


class OutlookAccountPool:
    """管理 outlook_live_...txt 账号池，持久化当前指针，支持循环轮询"""

    def __init__(
        self,
        account_file: Path = OUTLOOK_FILE,
        pointer_file: Path = OUTLOOK_POINTER_FILE,
    ):
        self.account_file = Path(account_file)
        self.pointer_file = Path(pointer_file)
        self.accounts: list[tuple[str, str, str, str]] = []
        self._lock = asyncio.Lock()
        self._load_accounts()

    def _load_accounts(self) -> None:
        if not self.account_file.exists():
            log.warning(f"Outlook account file not found: {self.account_file}")
            return
        accs = []
        with open(self.account_file, encoding="utf-8-sig") as f:
            for line in f:
                parts = line.strip().split("----", 3)
                if len(parts) == 4:
                    accs.append((parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip()))
        self.accounts = accs
        log.info(f"Loaded {len(self.accounts)} accounts from {self.account_file}")

    def load_pointer(self) -> int:
        if self.pointer_file.exists():
            try:
                data = json.loads(self.pointer_file.read_text(encoding="utf-8"))
                return int(data.get("current_index", 0))
            except Exception as e:
                log.warning(f"Failed to read pointer file: {e}")
        return 0

    def save_pointer(self, index: int, last_email: str = "") -> None:
        try:
            data = {
                "current_index": index,
                "last_email": last_email,
                "total_accounts": len(self.accounts),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self.pointer_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            log.warning(f"Failed to save pointer file: {e}")

    async def get_next_account(self) -> Optional[OutlookAccount]:
        """获取下一个 Outlook 账号并递增指针"""
        if not self.accounts:
            return None
        async with self._lock:
            curr_idx = self.load_pointer()
            if curr_idx >= len(self.accounts):
                curr_idx = 0

            email, pwd, cid, rtok = self.accounts[curr_idx]
            next_idx = (curr_idx + 1) % len(self.accounts)
            self.save_pointer(next_idx, email)
            return OutlookAccount(
                address=email,
                password=pwd,
                client_id=cid,
                refresh_token=rtok,
                index=curr_idx,
            )


outlook_pool = OutlookAccountPool()
