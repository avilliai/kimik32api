ACCOUNT_QUOTA = 10

class AccountState:
    def __init__(self, d: dict, index: int = 1):
        self.index = index
        self.email = d.get("email", "")
        self.password = d.get("password", "")
        self.name = d.get("name", "")
        self.cookie_str = d.get("cookie_str", "")
        self.user_id = d.get("user_id", "")
        self.disabled = d.get("disabled", False)
        self.healthy = d.get("healthy", True)
        self.usage_count = d.get("usage_count", 0)
        self.fail_count = d.get("fail_count", 0)

    @property
    def remaining(self) -> int:
        return max(0, ACCOUNT_QUOTA - self.usage_count)

    @property
    def is_alive(self) -> bool:
        return self.healthy and not self.disabled and self.remaining > 0

    @property
    def quota_exhausted(self) -> bool:
        return self.usage_count >= ACCOUNT_QUOTA

    @property
    def token_hint(self) -> str:
        if not self.cookie_str:
            return "N/A"
        for part in self.cookie_str.split(";"):
            part = part.strip()
            if part.startswith("next-auth.session-token=") or part.startswith("__Secure-authjs.session-token="):
                val = part.split("=", 1)[1]
                if len(val) > 20: return f"{val[:10]}...{val[-10:]}"
                return val
        # 如果找不到特定键，返回截断的全部 Cookie
        return f"{self.cookie_str[:10]}...{self.cookie_str[-10:]}" if len(self.cookie_str)>20 else self.cookie_str

    def cookies_dict(self) -> dict:
        if not self.cookie_str: return {}
        res = {}
        for part in self.cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                res[k.strip()] = v.strip()
        return res

    def increment_usage(self) -> None:
        self.usage_count += 1
        if self.quota_exhausted:
            self.disabled = True

    def to_dict(self) -> dict:
        return {
            "email": self.email,
            "password": self.password,
            "name": self.name,
            "cookie_str": self.cookie_str,
            "user_id": self.user_id,
            "disabled": self.disabled,
            "healthy": self.healthy,
            "usage_count": self.usage_count,
            "fail_count": self.fail_count,
        }