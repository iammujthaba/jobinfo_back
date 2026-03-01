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
    CallbackRequest, Candidate, JobVacancy, VacancyStatus, ConversationState, UserQuestion
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
            "is_edited": bool(getattr(v, "is_edited", False)),
            "edited_at": v.edited_at.isoformat() if getattr(v, "edited_at", None) else None,
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


@router.post("/api/vacancies/{vacancy_id}/share-to-channel")
async def api_share_vacancy_to_channel(
    vacancy_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    """
    Share an approved vacancy as a formatted broadcast to the WhatsApp Channel.
    Formats vacancy details as a rich text message with an apply link.
    Checks if the channel phone number has interacted within the last 24 hours.
    """
    from app.whatsapp.client import wa_client

    channel_wa_number = getattr(settings, "wa_channel_id", None)
    if not channel_wa_number:
        raise HTTPException(status_code=500, detail="WA_CHANNEL_ID is not configured in environment settings.")

    conv_state = db.query(ConversationState).filter_by(wa_number=channel_wa_number).first()
    
    if not conv_state or not conv_state.last_user_message_at:
        raise HTTPException(
            status_code=400,
            detail="The 24-hour free message window has expired. Please send a message from the designated channel number to the bot to reactivate it."
        )

    now_utc = datetime.now(timezone.utc)
    last_msg_time = conv_state.last_user_message_at
    if last_msg_time.tzinfo is None:
        last_msg_time = last_msg_time.replace(tzinfo=timezone.utc)

    time_diff = (now_utc - last_msg_time).total_seconds()
    if time_diff > 86400:  # 24 hours in seconds
        raise HTTPException(
            status_code=400,
            detail="The 24-hour free message window has expired. Please send a message from the designated channel number to the bot to reactivate it."
        )

    vacancy = db.query(JobVacancy).filter_by(id=vacancy_id, status=VacancyStatus.approved).first()
    if not vacancy:
        raise HTTPException(status_code=404, detail="Approved vacancy not found")

    apply_link = f"https://wa.me/{settings.business_wa_number}?text=Apply%20{vacancy.job_code}"

    lines = [
        f"🚀 *New Job Alert – JobInfo*",
        f"",
        f"🏷️ *{vacancy.title}*",
    ]
    if vacancy.company:
        lines.append(f"🏢 Company: {vacancy.company}")
    lines.append(f"📍 Location: {vacancy.location}")
    if vacancy.salary_range:
        lines.append(f"💰 Salary: {vacancy.salary_range}")
    if vacancy.experience_required:
        lines.append(f"🎓 Experience: {vacancy.experience_required}")
    if vacancy.description:
        lines.append(f"")
        lines.append(f"📋 *About the Role:*")
        lines.append(vacancy.description[:400] + ("…" if len(vacancy.description) > 400 else ""))
    lines += [
        f"",
        f"📲 *Apply now:* {apply_link}",
        f"🔖 Job Code: {vacancy.job_code}",
        f"",
        f"_JobInfo – Kerala's WhatsApp Job Platform_",
        f"🌐 jobinfo.club | 📢 Follow our channel for daily jobs",
    ]

    try:
        await wa_client.send_to_channel(body="\n".join(lines))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"WhatsApp API error: {e}")

    return {"success": True, "vacancy_id": vacancy_id, "job_code": vacancy.job_code}


@router.get("/api/analytics")
async def api_analytics(
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    """
    Returns comprehensive platform analytics for the admin dashboard.
    """
    # 👇 ADDED 'case' to this import
    from sqlalchemy import func as sqlfunc, case
    from app.db.models import (
        Candidate, CandidateApplication, JobVacancy, Recruiter, VacancyStatus
    )
    from datetime import date, timedelta

    # ── Platform totals ──────────────────────────────────────────────────────
    total_vacancies   = db.query(JobVacancy).count()
    total_recruiters  = db.query(Recruiter).count()
    total_candidates  = db.query(Candidate).count()
    total_applications = db.query(CandidateApplication).count()

    pending_count  = db.query(JobVacancy).filter_by(status=VacancyStatus.pending).count()
    approved_count = db.query(JobVacancy).filter_by(status=VacancyStatus.approved).count()
    rejected_count = db.query(JobVacancy).filter_by(status=VacancyStatus.rejected).count()

    # ── Daily vacancy submissions – last 30 days ─────────────────────────────
    today = date.today()
    period_start = today - timedelta(days=29)

    vacancy_daily_raw = (
        db.query(
            sqlfunc.date(JobVacancy.created_at).label("day"),
            sqlfunc.count(JobVacancy.id).label("cnt")
        )
        .filter(JobVacancy.created_at >= period_start)
        .group_by(sqlfunc.date(JobVacancy.created_at))
        .all()
    )
    vac_by_day = {str(row.day): row.cnt for row in vacancy_daily_raw}
    vacancy_daily = [
        {"date": str(period_start + timedelta(days=i)), "count": vac_by_day.get(str(period_start + timedelta(days=i)), 0)}
        for i in range(30)
    ]

    # ── Daily applications – last 30 days ────────────────────────────────────
    app_daily_raw = (
        db.query(
            sqlfunc.date(CandidateApplication.applied_at).label("day"),
            sqlfunc.count(CandidateApplication.id).label("cnt")
        )
        .filter(CandidateApplication.applied_at >= period_start)
        .group_by(sqlfunc.date(CandidateApplication.applied_at))
        .all()
    )
    app_by_day = {str(row.day): row.cnt for row in app_daily_raw}
    applications_daily = [
        {"date": str(period_start + timedelta(days=i)), "count": app_by_day.get(str(period_start + timedelta(days=i)), 0)}
        for i in range(30)
    ]

    # ── Vacancies per recruiter (top 15) ─────────────────────────────────────
    # 👇 FIXED: Used 'case()' instead of 'sqlfunc.case()'
    recruiter_vac_rows = (
        db.query(
            Recruiter.name.label("name"),
            Recruiter.company.label("company"),
            sqlfunc.count(JobVacancy.id).label("total"),
            sqlfunc.sum(case((JobVacancy.status == VacancyStatus.approved, 1), else_=0)).label("approved"),
            sqlfunc.sum(case((JobVacancy.status == VacancyStatus.pending, 1), else_=0)).label("pending"),
            sqlfunc.sum(case((JobVacancy.status == VacancyStatus.rejected, 1), else_=0)).label("rejected"),
        )
        .join(JobVacancy, JobVacancy.recruiter_id == Recruiter.id)
        .group_by(Recruiter.id)
        .order_by(sqlfunc.count(JobVacancy.id).desc())
        .limit(15)
        .all()
    )
    vacancies_per_recruiter = [
        {
            "recruiter": f"{r.name}" + (f" ({r.company})" if r.company else ""),
            "total": r.total, 
            "approved": int(r.approved or 0),
            "pending": int(r.pending or 0), 
            "rejected": int(r.rejected or 0),
        }
        for r in recruiter_vac_rows
    ]

    # ── Applications per vacancy (top 15 by apps) ────────────────────────────
    top_jobs_rows = (
        db.query(
            JobVacancy.title.label("title"),
            JobVacancy.job_code.label("job_code"),
            JobVacancy.location.label("location"),
            JobVacancy.status.label("status"),
            sqlfunc.count(CandidateApplication.id).label("apps"),
        )
        .outerjoin(CandidateApplication, CandidateApplication.vacancy_id == JobVacancy.id)
        .group_by(JobVacancy.id)
        .order_by(sqlfunc.count(CandidateApplication.id).desc())
        .limit(15)
        .all()
    )
    top_jobs = [
        {
            "title": r.title,
            "job_code": r.job_code,
            "location": r.location,
            "status": r.status.value if r.status else "",
            "applications": r.apps,
        }
        for r in top_jobs_rows
    ]

    # ── Recruiter registration trend – last 30 days ──────────────────────────
    rec_daily_raw = (
        db.query(
            sqlfunc.date(Recruiter.created_at).label("day"),
            sqlfunc.count(Recruiter.id).label("cnt")
        )
        .filter(Recruiter.created_at >= period_start)
        .group_by(sqlfunc.date(Recruiter.created_at))
        .all()
    )
    rec_by_day = {str(row.day): row.cnt for row in rec_daily_raw}
    recruiters_daily = [
        {"date": str(period_start + timedelta(days=i)), "count": rec_by_day.get(str(period_start + timedelta(days=i)), 0)}
        for i in range(30)
    ]

    return {
        "totals": {
            "vacancies": total_vacancies,
            "recruiters": total_recruiters,
            "candidates": total_candidates,
            "applications": total_applications,
        },
        "vacancy_status": {
            "pending": pending_count,
            "approved": approved_count,
            "rejected": rejected_count,
        },
        "vacancy_daily": vacancy_daily,
        "applications_daily": applications_daily,
        "recruiters_daily": recruiters_daily,
        "vacancies_per_recruiter": vacancies_per_recruiter,
        "top_jobs_by_applications": top_jobs,
    }


@router.get("/api/questions")
async def api_list_questions(
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Returns submitted user questions."""
    questions = db.query(UserQuestion).order_by(UserQuestion.created_at.desc()).all()
    results = [
        {
            "id": q.id,
            "name": q.name,
            "wa_number": q.wa_number,
            "question": q.question,
            "source": q.source,
            "is_resolved": q.is_resolved,
            "created_at": q.created_at.isoformat() if q.created_at else None,
        }
        for q in questions
    ]
    return {"total": len(results), "results": results}


@router.post("/api/questions/{question_id}/resolve")
async def api_resolve_question(
    question_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Marks a user question as resolved."""
    q = db.query(UserQuestion).filter_by(id=question_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="Question not found")
    q.is_resolved = True
    db.commit()
    return {"success": True, "question_id": question_id}
