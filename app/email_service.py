import os
import random
import string
import logging
import requests
from app.config import WORKER_DOMAIN, EMAIL_DOMAIN, ADMIN_PASSWORD, PROXY

log = logging.getLogger("kimik3_proxy.email_service")


class EmailService:
    def __init__(self):
        self.worker_domain = WORKER_DOMAIN
        self.email_domain = EMAIL_DOMAIN
        self.admin_password = ADMIN_PASSWORD
        self.proxies = {"http": PROXY, "https": PROXY} if PROXY else None

    @property
    def is_configured(self) -> bool:
        return bool(self.worker_domain and self.email_domain and self.admin_password)

    def _generate_random_name(self) -> str:
        letters1 = "".join(random.choices(string.ascii_lowercase, k=random.randint(4, 6)))
        numbers = "".join(random.choices(string.digits, k=random.randint(1, 3)))
        letters2 = "".join(random.choices(string.ascii_lowercase, k=random.randint(0, 5)))
        return letters1 + numbers + letters2

    def create_email(self) -> tuple[str | None, str | None]:
        if not self.is_configured:
            return None, None
        url = f"https://{self.worker_domain}/admin/new_address"
        try:
            name = self._generate_random_name()
            res = requests.post(
                url,
                json={
                    "enablePrefix": True,
                    "name": name,
                    "domain": self.email_domain,
                },
                headers={
                    "x-admin-auth": self.admin_password,
                    "Content-Type": "application/json",
                },
                proxies=self.proxies,
                timeout=12,
                verify=False
            )
            if res.status_code == 200:
                data = res.json()
                return data.get("jwt"), data.get("address")
            log.warning(f"[-] 创建域名邮箱接口返回错误: {res.status_code} - {res.text}")
            return None, None
        except Exception as e:
            log.warning(f"[-] 创建域名邮箱网络异常: {e}")
            return None, None

    def fetch_first_email(self, jwt: str) -> str | None:
        if not self.is_configured or not jwt:
            return None
        try:
            res = requests.get(
                f"https://{self.worker_domain}/api/mails",
                params={"limit": 10, "offset": 0},
                headers={
                    "Authorization": f"Bearer {jwt}",
                    "Content-Type": "application/json",
                },
                proxies=self.proxies,
                timeout=12,
                verify=False
            )
            if res.status_code == 200:
                data = res.json()
                if data.get("results"):
                    return data["results"][0].get("raw")
            return None
        except Exception as e:
            log.warning(f"[-] 获取域名邮件失败: {e}")
            return None
