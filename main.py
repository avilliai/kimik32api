import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.routers import admin, chat, model_list
from app.cookie_manager import cm

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

API_KEY = os.getenv("PROXY_API_KEY", "kimik3")

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in {"/", "/health"}:
            return await call_next(request)
        if API_KEY:
            auth = request.headers.get("Authorization", "")
            if auth != f"Bearer {API_KEY}":
                return JSONResponse(status_code=401, content={"error": {"message": "Invalid API key"}})
        return await call_next(request)

async def keep_alive_task():
    while True:
        await asyncio.sleep(300) # 每5分钟检测一次
        try:
            await cm.keepalive_tick()
        except Exception as e:
            logging.error(f"Keepalive error: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    cm.load()
    logging.info(f"🚀 Proxy started, accounts: {len(cm.accounts)}")
    bg_task = asyncio.create_task(keep_alive_task())
    yield
    bg_task.cancel()

app = FastAPI(title="Kimik3 Proxy", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.add_middleware(AuthMiddleware)

app.include_router(chat.router)
app.include_router(model_list.router)
app.include_router(admin.router)

@app.get("/")
async def serve_dashboard():
    html_path = Path("dashboard.html")
    if html_path.exists():
        return FileResponse(html_path)
    return HTMLResponse("<h1>Kimik3 Proxy Running</h1>")

@app.get("/health")
async def health():
    return {"status": "ok", "accounts": len(cm.accounts)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8077, log_level="info")