"""
Admin dashboard router.
Provides HTTP Basic Auth protected routes for managing:
- Vacancies (approve / reject)
- Callback requests
- Abandoned subscription candidates
"""
import secrets
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.base import get_db
from app.db.models import (
    CallbackRequest, Candidate, JobVacancy, VacancyStatus
)
from app.handlers import recruiter as recruiter_handler

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/admin", tags=["admin"])
security = HTTPBasic()
templates = Jinja2Templates(directory="app/templates")


# ─── Auth dependency ──────────────────────────────────────────────────────────

def require_admin(credentials: HTTPBasicCredentials = Depends(security)):
    correct_user = secrets.compare_digest(credentials.username, settings.admin_username)
    correct_pass = secrets.compare_digest(credentials.password, settings.admin_password)
    if not (correct_user and correct_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# ─── Dashboard ────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def admin_home(
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    pending_count = db.query(JobVacancy).filter_by(status=VacancyStatus.pending).count()
    callback_count = db.query(CallbackRequest).filter_by(resolved=False).count()
    abandoned_count = (
        db.query(Candidate).filter_by(registration_complete=False).count()
    )
    return templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request,
            "pending_count": pending_count,
            "callback_count": callback_count,
            "abandoned_count": abandoned_count,
        },
    )


# ─── Vacancies ────────────────────────────────────────────────────────────────

@router.get("/vacancies", response_class=HTMLResponse)
async def list_vacancies(
    request: Request,
    status_filter: str = "pending",
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    status_enum = VacancyStatus(status_filter) if status_filter in VacancyStatus._value2member_map_ else VacancyStatus.pending
    vacancies = (
        db.query(JobVacancy)
        .filter_by(status=status_enum)
        .order_by(JobVacancy.created_at.desc())
        .all()
    )
    return templates.TemplateResponse(
        "admin/vacancies.html",
        {
            "request": request,
            "vacancies": vacancies,
            "current_filter": status_filter,
        },
    )


@router.post("/vacancies/{vacancy_id}/approve")
async def approve_vacancy(
    vacancy_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    await recruiter_handler.notify_recruiter_approval(vacancy_id, db)
    return RedirectResponse(url="/admin/vacancies?status_filter=pending", status_code=303)


@router.post("/vacancies/{vacancy_id}/reject")
async def reject_vacancy(
    vacancy_id: int,
    reason: str = Form(...),
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    await recruiter_handler.notify_recruiter_rejection(vacancy_id, reason, db)
    return RedirectResponse(url="/admin/vacancies?status_filter=pending", status_code=303)


# ─── Callbacks ────────────────────────────────────────────────────────────────

@router.get("/callbacks", response_class=HTMLResponse)
async def list_callbacks(
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    callbacks = (
        db.query(CallbackRequest)
        .filter_by(resolved=False)
        .order_by(CallbackRequest.created_at.desc())
        .all()
    )
    return templates.TemplateResponse(
        "admin/callbacks.html",
        {"request": request, "callbacks": callbacks},
    )


@router.post("/callbacks/{callback_id}/resolve")
async def resolve_callback(
    callback_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    req = db.query(CallbackRequest).filter_by(id=callback_id).first()
    if req:
        req.resolved = True
        db.commit()
    return RedirectResponse(url="/admin/callbacks", status_code=303)


# ─── Abandoned candidates ─────────────────────────────────────────────────────

@router.get("/abandoned", response_class=HTMLResponse)
async def list_abandoned(
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    abandoned = (
        db.query(Candidate)
        .filter_by(registration_complete=False)
        .order_by(Candidate.created_at.desc())
        .all()
    )
    return templates.TemplateResponse(
        "admin/abandoned.html",
        {"request": request, "candidates": abandoned},
    )


# ─── JSON API (for the frontend admin.html panel) ─────────────────────────────


@router.get("/api/vacancies")
async def api_list_vacancies(
    status_filter: str = "pending",
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Returns vacancies as JSON (used by admin.html frontend panel)."""
    valid = {s.value for s in VacancyStatus}
    status_enum = VacancyStatus(status_filter) if status_filter in valid else VacancyStatus.pending

    vacancies = (
        db.query(JobVacancy)
        .filter_by(status=status_enum)
        .order_by(JobVacancy.created_at.desc())
        .all()
    )

    results = []
    for v in vacancies:
        results.append({
            "id": v.id,
            "job_code": v.job_code,
            "title": v.title,
            "company": v.company or "",
            "location": v.location,
            "description": v.description or "",
            "salary_range": v.salary_range,
            "experience_required": v.experience_required,
            "status": v.status.value,
            "rejection_reason": v.rejection_reason,
            "created_at": v.created_at.isoformat() if v.created_at else None,
            "recruiter": {
                "name": v.recruiter.name,
                "wa_number": v.recruiter.wa_number,
                "company": v.recruiter.company,
            } if v.recruiter else None,
        })

    return {"total": len(results), "results": results}


@router.post("/api/vacancies/{vacancy_id}/approve")
async def api_approve_vacancy(
    vacancy_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Approve a vacancy and notify the recruiter via WhatsApp."""
    await recruiter_handler.notify_recruiter_approval(vacancy_id, db)
    return {"success": True, "vacancy_id": vacancy_id}


@router.post("/api/vacancies/{vacancy_id}/reject")
async def api_reject_vacancy(
    vacancy_id: int,
    reason: str = Form(...),
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Reject a vacancy with a reason and notify the recruiter via WhatsApp."""
    await recruiter_handler.notify_recruiter_rejection(vacancy_id, reason, db)
    return {"success": True, "vacancy_id": vacancy_id}

