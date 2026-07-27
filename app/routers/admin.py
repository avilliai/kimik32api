import concurrent.futures
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from app.cookie_manager import cm
import logging

log = logging.getLogger("kimik3_proxy.admin")
router = APIRouter(prefix="/admin")

register_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)

def _register_worker():
    import asyncio
    from app.kimik3_client import Kimik3Register
    result = asyncio.run(Kimik3Register().run())
    if result:
        cm.add_account(result)
        cm.save()

@router.get("/tokens/detail")
async def get_all_tokens_detail():
    return cm.get_tokens_detail()

@router.post("/tokens/refresh")
async def tokens_refresh_all(background_tasks: BackgroundTasks):
    background_tasks.add_task(cm.keepalive_tick)
    return {"message": "活跃池保活已启动"}

@router.post("/tokens/bulk/delete")
async def tokens_bulk_delete(req: Request):
    indexes: list[int] = (await req.json()).get("indexes", [])
    with cm._sync_lock:
        before = len(cm.accounts)
        cm.accounts = [a for a in cm.accounts if a.index not in indexes]
        removed = before - len(cm.accounts)
    cm.save()
    return {"removed": removed}

@router.post("/register/start")
async def register_start(req: Request):
    data = await req.json()
    count = int(data.get("count", 1))
    for _ in range(count):
        register_executor.submit(_register_worker)
    return {"message": f"成功投递 {count} 个注册任务"}

@router.get("/register/status")
async def register_status():
    return {"total_accounts": len(cm.accounts)}

@router.post("/tokens/add")
async def tokens_add(req: Request):
    data = await req.json()
    tokens = data.get("tokens", [data])
    added = 0
    for t in tokens:
        if cm.add_account(t): added += 1
    cm.save()
    return {"added": added}