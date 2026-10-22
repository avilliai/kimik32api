import asyncio
import json
import logging
import threading
from typing import Optional
from curl_cffi.requests import AsyncSession

from app.config import TOKENS_FILE, KIMIK3_API, COMMON_HEADERS, ACTIVE_POOL_SIZE, PROXY
from app.models import AccountState

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
            raw_text = TOKENS_FILE.read_text(encoding="utf-8-sig")
            data = json.loads(raw_text) if raw_text.strip() else []
            with self._sync_lock:
                self.accounts.clear()
                for i, d in enumerate(data):
                    if not d.get("cookie_str"):
                        continue
                    acc = AccountState(d, i + 1)
                    self.accounts.append(acc)

            valid_indices = [a.index for a in self.accounts if a.is_alive]
            self.active_pool = valid_indices[:ACTIVE_POOL_SIZE]
            log.info(f"✅ 成功加载账号: {len(self.accounts)} 个，当前激活号池: {len(self.active_pool)}/{ACTIVE_POOL_SIZE} 个")
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
        total_usage = 0
        alive_count = 0

        with self._sync_lock:
            pool_set = set(self.active_pool)
            for acc in self.accounts:
                total_usage += acc.usage_count
                if acc.is_alive:
                    alive_count += 1
                tokens_list.append({
                    "index": acc.index,
                    "email": acc.email,
                    "token_hint": acc.token_hint,
                    "is_alive": acc.is_alive,
                    "disabled": acc.disabled,
                    "usage_count": acc.usage_count,
                    "in_active_pool": acc.index in pool_set,
                    "fail_count": acc.fail_count,
                })
        return {
            "tokens": tokens_list,
            "summary": {
                "total": len(self.accounts),
                "alive": alive_count,
                "total_usage": total_usage,
                "active_pool_size": len(self.active_pool),
                "active_pool_target": ACTIVE_POOL_SIZE,
                "last_refresh": None,
            }
        }

    def add_account(self, data: dict) -> bool:
        with self._sync_lock:
            for acc in self.accounts:
                if acc.email == data.get("email"):
                    return False
            new_idx = max([a.index for a in self.accounts] + [0]) + 1
            self.accounts.append(AccountState(data, new_idx))
        return True

    async def _replenish_active_pool(self) -> None:
        """检查并确保号池中刚好有 ACTIVE_POOL_SIZE 个可用账号"""
        if self._replenishing:
            return
        self._replenishing = True
        try:
            with self._sync_lock:
                valid_accs = [a for a in self.accounts if a.is_alive]
                need = ACTIVE_POOL_SIZE - len(valid_accs)

            if need > 0:
                log.info(f"🔄 当前有效账号仅 {len(valid_accs)} 个，正在注册 {need} 个新账号填满号池...")
                from app.kimik3_client import Kimik3Register
                for i in range(need):
                    try:
                        result = await Kimik3Register().run()
                        if result:
                            self.add_account(result)
                            log.info(f"✨ 补号成功 ({i+1}/{need}): {result.get('email')}")
                    except Exception as e:
                        log.warning(f"补号异常: {e}")

            with self._sync_lock:
                valid_accs = [a for a in self.accounts if a.is_alive]
                self.active_pool = [a.index for a in valid_accs][:ACTIVE_POOL_SIZE]

            self.save()
        finally:
            self._replenishing = False

    async def keepalive_tick(self) -> None:
        """探活与账号保活检测"""
        with self._sync_lock:
            candidates = [a for a in self.accounts if a.index in set(self.active_pool)]

        if len(candidates) < ACTIVE_POOL_SIZE:
            await self._replenish_active_pool()
            return

        async def _probe(acc: AccountState):
            try:
                async with AsyncSession(impersonate="chrome110", proxy=PROXY, timeout=15) as s:
                    s.cookies.update(acc.cookies_dict())
                    resp = await s.get(f"{KIMIK3_API}/auth/get-session", headers=COMMON_HEADERS)
                    if resp.status_code in (401, 403) or (resp.status_code == 200 and not resp.json()):
                        acc.fail_count += 1
                        if acc.fail_count >= 3:
                            acc.healthy = False
                            acc.disabled = True
                            log.warning(f"账号 [{acc.email}] Session 失效，已标记禁用")
                    else:
                        acc.healthy = True
                        acc.fail_count = 0
            except Exception as e:
                log.warning(f"探活账号 [{acc.email}] 失败: {e}")

        await asyncio.gather(*[_probe(a) for a in candidates])
        await self.async_save()

        # 如果探活后有效账号不足，触发自动补齐
        with self._sync_lock:
            valid_count = len([a for a in self.accounts if a.is_alive])
        if valid_count < ACTIVE_POOL_SIZE:
            await self._replenish_active_pool()

    async def get_next(self) -> AccountState:
        """轮询获取号池中的可用账号之一"""
        with self._sync_lock:
            pool_set = set(self.active_pool)
            valid = [a for a in self.accounts if a.index in pool_set and a.is_alive]

        if len(valid) < ACTIVE_POOL_SIZE:
            await self._replenish_active_pool()
            with self._sync_lock:
                valid = [a for a in self.accounts if a.index in set(self.active_pool) and a.is_alive]
            if not valid:
                raise RuntimeError("号池为空且自动注册新账号失败，请检查网络/代理或配置")

        async with self._idx_lock:
            acc = valid[self._idx % len(valid)]
            self._idx += 1

        return acc

    async def mark_failed_and_get_replacement(self, acc: AccountState, reason: str = "unknown") -> AccountState:
        """账号失败处理：累加失败次数并在真实失效或连续失败达到阈值时才换号废弃"""
        async with self._async_lock:
            acc.fail_count += 1
            log.warning(f"⚠️ 账号 [{acc.email}] 失败: {reason} | 连续失败: {acc.fail_count} | 累计已用: {acc.usage_count}次")

            # 仅在明确收到 session 失效/上游配额报错，或连续失败达到 3 次时，才废弃账号
            should_destroy = (
                reason in ("cookie_expired", "quota_exhausted")
                or acc.fail_count >= 3
            )

            if should_destroy:
                log.warning(f"💥 账号 [{acc.email}] 确定不可用({reason})，废弃并移出活跃池...")
                acc.healthy = False
                acc.disabled = True

                with self._sync_lock:
                    if acc.index in self.active_pool:
                        self.active_pool.remove(acc.index)
                self.save()

                # 补号确保池子饱和
                await self._replenish_active_pool()
            else:
                self.save()

        return await self.get_next()

    def record_usage(self, acc: AccountState) -> None:
        """请求成功时调用：记账并清空连续失败计数器"""
        acc.increment_usage()
        acc.fail_count = 0
        log.info(f"📈 [{acc.email}] 成功回复 | 累计调用: {acc.usage_count} 次")
        self.save()


cm = CookieManager()
