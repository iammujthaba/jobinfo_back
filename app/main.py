"""
FastAPI application entry point.
Mounts all routers, adds CORS, and initialises the database on startup.
"""
import logging
import os # NEW: required for file path handling
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException # NEW: added HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse # NEW: required to send files to the browser

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

# ── CORS ──────────────────────────────────────────────────────────────────────
# Allow the website and localhost dev to call the API.
# Tighten origins to ["https://jobinfo.club", "https://www.jobinfo.club"] in production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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


# ── File Serving Route ────────────────────────────────────────────────────────
# NEW: This entire block handles the CV download requests from the recruiter dashboard
@app.get("/files/cv/{file_path:path}", include_in_schema=False)
async def serve_cv(
    file_path: str,
    candidate_name: str | None = None,
    job_code: str | None = None,
):
    """
    Serves a candidate CV file as a download.
    Accepts optional query params to rename the file for the recruiter:
      ?candidate_name=John+Doe&job_code=JC123
    → downloads as  "John_Doe_JC123_CV.pdf"
    """
    import re

    clean_path = file_path.replace("\\", "/")
    if ".." in clean_path:
        raise HTTPException(status_code=403, detail="Invalid path")
    if not os.path.exists(clean_path):
        raise HTTPException(status_code=404, detail="CV file not found on server")

    # Build a meaningful filename when name/job_code are supplied
    ext = os.path.splitext(clean_path)[1] or ".pdf"
    if candidate_name and job_code:
        # Slug-safe: keep alphanumeric + spaces, replace spaces with underscores
        safe_name = re.sub(r"[^\w\s]", "", candidate_name).strip().replace(" ", "_")
        safe_code = re.sub(r"[^\w]", "", job_code)
        filename = f"{safe_name}_{safe_code}_CV{ext}"
    elif candidate_name:
        safe_name = re.sub(r"[^\w\s]", "", candidate_name).strip().replace(" ", "_")
        filename = f"{safe_name}_CV{ext}"
    else:
        filename = os.path.basename(clean_path)

    return FileResponse(
        path=clean_path,
        filename=filename,
        media_type="application/pdf",
        content_disposition_type="attachment",
    )
