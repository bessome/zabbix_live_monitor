"""FastAPI entry point for Zabbix Live Monitoring."""
import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app_config import ROOT, SECRET
from app_storage import init_db
from routes_account import router as account_router
from routes_devices import router as devices_router
from routes_settings import router as settings_router

app = FastAPI(title="Zabbix Live Monitoring", docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=SECRET, same_site="lax",
                   https_only=os.environ.get("APP_HTTPS") == "1")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
app.include_router(account_router)
app.include_router(devices_router)
app.include_router(settings_router)
init_db()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("APP_HOST", "127.0.0.1"),
                port=int(os.environ.get("APP_PORT", "8000")))
