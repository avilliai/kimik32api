import asyncio
import json
import logging
import threading
from typing import Optional

from app.config import TOKENS_FILE, KIMIK3_WEB, COMMON_HEADERS, ACTIVE_POOL_SIZE
from app.models import AccountState, ACCOUNT_QUOTA

log = logging.getLogger("kimik3_proxy.cookie_manager")


class CookieManager:
    def __init__(self):
        self.accounts: list[AccountState] = []
        self._idx: int = 0
        self._async_lock = asyncio.Lock()
        self._idx_lock = asyncio.Lock()
        self._sync_lock = threading.Lock()

        self.active_pool: list[int] = []
        self._pool_lock = asyncio.Lock()
        self._replenishing = False

    def load(self) -> None:
        if not TOKENS_FILE.exists():
            self.save()
            return
        try:
            data = json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
            with self._sync_lock:
                for i, d in enumerate(data):
                    if not d.get("cookie_str"): continue
                    acc = AccountState(d, i + 1)
                    self.accounts.append(acc)
            valid_indices = [a.index for a in self.accounts if a.is_alive]
            self.active_pool = valid_indices[:ACTIVE_POOL_SIZE]
            log.info(f"✅ 成功加载账号: {len(self.accounts)} 个，当前激活号池: {len(self.active_pool)}/3 个")
        except Exception as e:
            log.error(f"解析 tokens 失败: {e}")

    def save(self) -> None:
        with self._sync_lock:
            raw = [acc.to_dict() for acc in self.accounts]
            TOKENS_FILE.write_text(json.dumps(raw, ensure_ascii=False, indent=4), encoding="utf-8")

    async def async_save(self) -> None:
        await asyncio.to_thread(self.save)

    def get_tokens_detail(self) -> dict:
        tokens_list = []
        total_remaining = 0
        alive_count = 0

        with self._sync_lock:
            pool_set = set(self.active_pool)
            for acc in self.accounts:
                total_remaining += acc.remaining
                if acc.is_alive: alive_count += 1
                tokens_list.append({
                    "index": acc.index,
                    "email": acc.email,
                    "token_hint": acc.token_hint,
                    "is_alive": acc.healthy,
                    "disabled": acc.disabled,
                    "remaining": acc.remaining,
                    "in_active_pool": acc.index in pool_set,
                    "fail_count": acc.fail_count,
                })
        return {
            "tokens": tokens_list,
            "summary": {
                "total": len(self.accounts),
                "alive": alive_count,
                "total_remaining": total_remaining,
                "active_pool_size": len(self.active_pool),
                "active_pool_target": ACTIVE_POOL_SIZE,
                "last_refresh": None,
            }
        }

    def add_account(self, data: dict) -> bool:
        with self._sync_lock:
            for acc in self.accounts:
                if acc.email == data.get("email"): return False
            new_idx = max([a.index for a in self.accounts] + [0]) + 1
            self.accounts.append(AccountState(data, new_idx))
        return True

    async def _replenish_active_pool(self) -> None:
        """检查并确保号池中刚好有 3 个可用账号"""
        if self._replenishing: return
        self._replenishing = True
        try:
            valid_accs = [a for a in self.accounts if a.is_alive]
            need = ACTIVE_POOL_SIZE - len(valid_accs)

            if need > 0:
                log.info(f"🔄 当前有效账号仅 {len(valid_accs)} 个，正在并发注册 {need} 个新账号填满号池...")
                from app.kimik3_client import Kimik3Register
                for _ in range(need):
                    result = await Kimik3Register().run()
                    if result:
                        self.add_account(result)

            with self._sync_lock:
                self.active_pool = [a.index for a in self.accounts if a.is_alive][:ACTIVE_POOL_SIZE]
            self.save()
        finally:
            self._replenishing = False

    async def keepalive_tick(self) -> None:
        """同步 credits 余额"""
        with self._sync_lock:
            candidates = [a for a in self.accounts if a.index in set(self.active_pool)]

        if len(candidates) < ACTIVE_POOL_SIZE:
            await self._replenish_active_pool()
            return

        from curl_cffi.requests import AsyncSession
        async def _probe(acc: AccountState):
            try:
                async with AsyncSession(impersonate="chrome110", timeout=15) as s:
                    resp = await s.get(f"{KIMIK3_WEB}/api/credits", headers=COMMON_HEADERS, cookies=acc.cookies_dict())
                    if resp.status_code in (401, 403):
                        acc.fail_count += 1
                    elif resp.status_code == 200:
                        bal = resp.json().get("data", {}).get("balance", 0)
                        acc.usage_count = max(0, ACCOUNT_QUOTA - bal)
            except Exception:
                pass

        await asyncio.gather(*[_probe(a) for a in candidates])
        await self.async_save()

    async def get_next(self) -> AccountState:
        """轮询获取号池中的 3 个账号之一"""
        with self._sync_lock:
            pool_set = set(self.active_pool)
            valid = [a for a in self.accounts if a.index in pool_set and a.is_alive]

        if len(valid) < ACTIVE_POOL_SIZE:
            await self._replenish_active_pool()
            with self._sync_lock:
                valid = [a for a in self.accounts if a.index in set(self.active_pool) and a.is_alive]
            if not valid: raise RuntimeError("号池为空且自动注册失败，请检查网络")

        async with self._idx_lock:
            acc = valid[self._idx % len(valid)]
            self._idx += 1

        return acc

    async def mark_failed_and_get_replacement(self, acc: AccountState, reason: str = "unknown") -> AccountState:
        """核心处理逻辑：累加失败次数，仅在符合条件时销毁号并补号"""
        async with self._async_lock:
            acc.fail_count += 1
            log.warning(
                f"⚠️ 账号 [{acc.email}] 失败: {reason} | 当前连续失败: {acc.fail_count}/3 | 剩余 Credits: {acc.remaining}")

            # 判定条件：连续失败 >= 3 次 且 额度 credits <= 0
            should_destroy = (acc.fail_count >= 3 and acc.remaining <= 0) or acc.quota_exhausted or acc.fail_count>5

            if should_destroy:
                log.error(f"💥 账号 [{acc.email}] 满足销毁条件(连续失败3次且额度为0)，正在彻底废弃并重新注册...")
                acc.healthy = False
                acc.disabled = True

                # 从账号列表和号池中移除
                with self._sync_lock:
                    self.accounts = [a for a in self.accounts if a.email != acc.email]
                    if acc.index in self.active_pool:
                        self.active_pool.remove(acc.index)
                self.save()

                # 重新注册补齐至 3 个
                await self._replenish_active_pool()
            else:
                self.save()

        # 切到号池中的下一个现有账号重试（不注册新号）
        return await self.get_next()

    def record_usage(self, acc: AccountState) -> None:
        """请求成功时调用：记账并清空连续失败计数器"""
        acc.increment_usage()
        acc.fail_count = 0  # <--- 关键：成功回复立刻重置连续失败次数
        log.info(f"📈 [{acc.email}] 成功回复 | 剩余 Credits: {acc.remaining}")
        self.save()


cm = CookieManager()