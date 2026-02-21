"""
FastAPI application entry point.
Mounts all routers and initialises the database on startup.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db.base import init_db
from app.db.seed import seed
from app.routers import webhook, admin, api, flows

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: initialise DB and seed plans."""
    init_db()
    seed()
    yield


app = FastAPI(
    title="JobInfo API",
    description="Backend automation for JobInfo – Kerala's WhatsApp Job Platform",
    version="1.0.0",
    lifespan=lifespan,
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(webhook.router)
app.include_router(admin.router)
app.include_router(api.router)
app.include_router(flows.router)


@app.get("/", include_in_schema=False)
async def root():
    return {
        "service": "JobInfo API",
        "docs": "/docs",
        "admin": "/admin",
    }
