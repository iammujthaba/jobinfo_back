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
    GetHelpRequest, Candidate, JobVacancy, ConversationState, UserQuestion
)
from app.handlers import recruiter as recruiter_handler

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/admin", tags=["admin"])
security = HTTPBasic()
templates = Jinja2Templates(directory="app/templates")


# ─── Auth dependency ──────────────────────────────────────────────────────────

def require_admin(request: Request, db: Session = Depends(get_db)) -> str:
    """
    Dual-mode admin auth:
    1. Bearer <session_token>  — issued by magic-link verify (role must be 'admin')
    2. Basic base64(user:pass)  — classic username/password login
    """
    auth_header = request.headers.get("Authorization", "")

    # ── Mode 1: Bearer token (magic-link session) ──────────────────────────────
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        # Import lazily to avoid circular imports
        from app.routers.api import _get_session_data
        session = _get_session_data(token)
        if not session or session.get("role") != "admin":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired admin session token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return session.get("wa_number", "admin")

    # ── Mode 2: Basic Auth (username + password) ───────────────────────────────
    import base64
    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            username, password = decoded.split(":", 1)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Malformed Basic auth header",
                headers={"WWW-Authenticate": "Basic"},
            )
        correct_user = secrets.compare_digest(username, settings.admin_username)
        correct_pass = secrets.compare_digest(password, settings.admin_password)
        if not (correct_user and correct_pass):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect credentials",
                headers={"WWW-Authenticate": "Basic"},
            )
        return username

    # ── No valid auth header ───────────────────────────────────────────────────
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Basic"},
    )


# ─── Dashboard ────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def admin_home(
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    pending_count = db.query(JobVacancy).filter_by(status="pending").count()
    gethelp_count = db.query(GetHelpRequest).filter_by(resolved=False).count()
    abandoned_count = (
        db.query(Candidate).filter_by(registration_complete=False).count()
    )
    return templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request,
            "pending_count": pending_count,
            "gethelp_count": gethelp_count,
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
    valid = {"pending", "approved", "rejected"}
    status_filter = status_filter if status_filter in valid else "pending"
    vacancies = (
        db.query(JobVacancy)
        .filter_by(status=status_filter)
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


# ─── Get Help Requests ────────────────────────────────────────────────────────

@router.get("/gethelp", response_class=HTMLResponse)
async def list_gethelp(
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    gethelp_requests = (
        db.query(GetHelpRequest)
        .filter_by(resolved=False)
        .order_by(GetHelpRequest.created_at.desc())
        .all()
    )
    return templates.TemplateResponse(
        "admin/gethelp.html",
        {"request": request, "gethelp_requests": gethelp_requests},
    )


@router.post("/gethelp/{gethelp_id}/resolve")
async def resolve_gethelp(
    gethelp_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    req = db.query(GetHelpRequest).filter_by(id=gethelp_id).first()
    if req:
        req.resolved = True
        db.commit()
    return RedirectResponse(url="/admin/gethelp", status_code=303)


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
    valid = {"pending", "approved", "rejected"}
    status_filter = status_filter if status_filter in valid else "pending"

    vacancies = (
        db.query(JobVacancy)
        .filter_by(status=status_filter)
        .order_by(JobVacancy.created_at.desc())
        .all()
    )

    results = []
    for v in vacancies:
        results.append({
            "id": v.id,
            "job_code": v.job_code,
            "job_category": v.job_category,
            "job_title": v.job_title,
            "company_name": v.recruiter.company_name if v.recruiter else "",
            "district_region": v.district_region,
            "exact_location": v.exact_location,
            "job_description": v.job_description or "",
            "job_mode": v.job_mode,
            "salary_range": v.salary_range,
            "experience_required": v.experience_required,
            "status": v.status,
            "rejection_reason": v.rejection_reason,
            "is_edited": bool(getattr(v, "is_edited", False)),
            "edited_at": v.edited_at.isoformat() if getattr(v, "edited_at", None) else None,
            "created_at": v.created_at.isoformat() if v.created_at else None,
            "recruiter": {
                "name": v.recruiter.company_name,
                "wa_number": v.recruiter.wa_number,
                "company": v.recruiter.company_name,
                "business_type": v.recruiter.business_type,
                "location": v.recruiter.location,
                "business_contact": v.recruiter.business_contact,
                "created_at": v.recruiter.created_at.isoformat() if v.recruiter.created_at else None,
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

    vacancy = db.query(JobVacancy).filter_by(id=vacancy_id, status="approved").first()
    if not vacancy:
        raise HTTPException(status_code=404, detail="Approved vacancy not found")

    apply_link = f"https://wa.me/{settings.business_wa_number}?text=Apply%20{vacancy.job_code}"

    lines = [
        f"🚀 *New Job Alert – JobInfo*",
        f"",
        f"🏷️ *{vacancy.job_title}*",
    ]
    if vacancy.recruiter and vacancy.recruiter.company_name:
        lines.append(f"🏢 Company: {vacancy.recruiter.company_name}")
    lines.append(f"📍 Location: {vacancy.exact_location}, {vacancy.district_region}")
    if vacancy.salary_range:
        lines.append(f"💰 Salary: {vacancy.salary_range}")
    if vacancy.experience_required:
        lines.append(f"🎓 Experience: {vacancy.experience_required}")
    if vacancy.job_description:
        lines.append(f"")
        lines.append(f"📋 *About the Role:*")
        lines.append(vacancy.job_description[:400] + ("…" if len(vacancy.job_description) > 400 else ""))
    lines += [
        f"",
        f"📲 *Apply now:* {apply_link}",
        f"🔖 Job Code: {vacancy.job_code}",
        f"",
        f"_JobInfo – Kerala's WhatsApp Job Platform_",
        f"🌐 jobinfo.pro | 📢 Follow our channel for daily jobs",
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
        Candidate, CandidateApplication, JobVacancy, Recruiter
    )
    from datetime import date, timedelta

    # ── Platform totals ──────────────────────────────────────────────────────
    total_vacancies   = db.query(JobVacancy).count()
    total_recruiters  = db.query(Recruiter).count()
    total_candidates  = db.query(Candidate).count()
    total_applications = db.query(CandidateApplication).count()

    pending_count  = db.query(JobVacancy).filter_by(status="pending").count()
    approved_count = db.query(JobVacancy).filter_by(status="approved").count()
    rejected_count = db.query(JobVacancy).filter_by(status="rejected").count()

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
            Recruiter.company_name.label("company_name"),
            Recruiter.wa_number.label("wa_number"),
            sqlfunc.count(JobVacancy.id).label("total"),
            sqlfunc.sum(case((JobVacancy.status == "approved", 1), else_=0)).label("approved"),
            sqlfunc.sum(case((JobVacancy.status == "pending", 1), else_=0)).label("pending"),
            sqlfunc.sum(case((JobVacancy.status == "rejected", 1), else_=0)).label("rejected"),
        )
        .join(JobVacancy, JobVacancy.recruiter_id == Recruiter.id)
        .group_by(Recruiter.id)
        .order_by(sqlfunc.count(JobVacancy.id).desc())
        .limit(15)
        .all()
    )
    vacancies_per_recruiter = [
        {
            "recruiter": f"{r.company_name} ({r.wa_number})" if r.company_name else str(r.wa_number),
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
            JobVacancy.job_title.label("job_title"),
            JobVacancy.job_code.label("job_code"),
            JobVacancy.district_region.label("district_region"),
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
            "title": r.job_title,
            "job_code": r.job_code,
            "location": r.district_region,
            "status": r.status if r.status else "",
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


@router.get("/api/help-requests")
async def api_list_help_requests(
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Returns submitted Get Help requests."""
    from app.db.models import Candidate, Recruiter
    requests = db.query(GetHelpRequest).order_by(GetHelpRequest.created_at.desc()).all()
    results = []
    
    for req in requests:
        candidate = db.query(Candidate).filter_by(wa_number=req.wa_number).first()
        recruiter = db.query(Recruiter).filter_by(wa_number=req.wa_number).first()
        
        if candidate and recruiter:
            user_type = "Both"
            name = f"{candidate.name} / {recruiter.company_name}"
        elif recruiter:
            user_type = "Recruiter"
            name = recruiter.company_name
        elif candidate:
            user_type = "Seeker"
            name = candidate.name
        else:
            user_type = "Unregistered"
            name = "Unknown"
        
        results.append({
            "id": req.id,
            "wa_number": req.wa_number,
            "user_type": user_type,
            "name": name,
            "resolved": req.resolved,
            "created_at": req.created_at.isoformat() if req.created_at else None,
        })
    return {"total": len(results), "results": results}


@router.patch("/api/help-requests/{request_id}/resolve")
async def api_resolve_help_request(
    request_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Marks a help request as resolved."""
    req = db.query(GetHelpRequest).filter_by(id=request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Help request not found")
    req.resolved = True
    db.commit()
    return {"success": True, "request_id": request_id}


# ─── Users Phase 2 API Endpoints ─────────────────────────────────────────────

@router.get("/api/users/summary")
async def api_users_summary(
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Returns macro stats for the Users Section."""
    from sqlalchemy import not_
    from app.db.models import Candidate, Recruiter, ConversationState

    total_seekers = db.query(Candidate).count()
    total_recruiters = db.query(Recruiter).count()
    
    # Dual Users: in both Candidate and Recruiter
    dual_users = db.query(Candidate).join(Recruiter, Candidate.wa_number == Recruiter.wa_number).count()
    
    total_bot_visitors = db.query(ConversationState).count()
    
    # Unregistered: Conversation states that are not registered as Candidate or Recruiter
    seek_q = db.query(Candidate.wa_number)
    rec_q = db.query(Recruiter.wa_number)
    unregistered_users = db.query(ConversationState).filter(
        ~ConversationState.wa_number.in_(seek_q),
        ~ConversationState.wa_number.in_(rec_q)
    ).count()

    total_registered = total_seekers + total_recruiters - dual_users
    baseline = max(total_bot_visitors, total_registered)
    conversion_rate = round((total_registered / baseline * 100)) if baseline > 0 else 0

    return {
        "job_seekers": total_seekers,
        "recruiters": total_recruiters,
        "unregistered": unregistered_users,
        "bot_visitors": total_bot_visitors,
        "dual_users": dual_users,
        "conversion_rate": conversion_rate
    }


@router.get("/api/recruiters/stats")
async def api_recruiters_stats(
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Returns chart data and the overarching recruiter stats table."""
    from sqlalchemy import func as sqlfunc
    from app.db.models import JobVacancy, Recruiter, CandidateApplication

    # Chart Data: Vacancies by District
    district_rows = db.query(
        JobVacancy.district_region, sqlfunc.count(JobVacancy.id).label("count")
    ).group_by(JobVacancy.district_region).all()
    districts = {r.district_region: r.count for r in district_rows}
    
    # Chart Data: Vacancies by Category
    category_rows = db.query(
        JobVacancy.job_category, sqlfunc.count(JobVacancy.id).label("count")
    ).group_by(JobVacancy.job_category).all()
    categories = {r.job_category: r.count for r in category_rows}
    
    # Total apps mapped by recruiter
    apps_raw = (
        db.query(
            JobVacancy.recruiter_id,
            sqlfunc.count(CandidateApplication.id).label("apps")
        )
        .join(CandidateApplication, CandidateApplication.vacancy_id == JobVacancy.id)
        .group_by(JobVacancy.recruiter_id)
        .all()
    )
    apps_map = {r.recruiter_id: r.apps for r in apps_raw}

    # Recruiter rows
    rec_rows = (
        db.query(
            Recruiter.id,
            Recruiter.company_name,
            Recruiter.business_type,
            Recruiter.location,
            Recruiter.wa_number,
            Recruiter.created_at,
            sqlfunc.max(JobVacancy.created_at).label("last_activity"),
            sqlfunc.count(JobVacancy.id).label("total_vacancies"),
        )
        .outerjoin(JobVacancy, JobVacancy.recruiter_id == Recruiter.id)
        .group_by(Recruiter.id)
        .order_by(sqlfunc.count(JobVacancy.id).desc())
        .all()
    )
    
    recruiters_table = []
    for r in rec_rows:
        total_vac = r.total_vacancies
        tot_apps = apps_map.get(r.id, 0)
        avg_apps = round(tot_apps / total_vac, 1) if total_vac > 0 else 0.0
        
        last_act = r.last_activity if r.last_activity else r.created_at
        
        recruiters_table.append({
            "company_name": r.company_name,
            "business_type": r.business_type,
            "location": r.location,
            "wa_number": r.wa_number,
            "last_activity": last_act.isoformat() if last_act else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "total_vacancies": total_vac,
            "avg_apps_per_vacancy": avg_apps
        })
        
    return {
        "chart_data": {
            "districts": districts,
            "categories": categories
        },
        "recruiters_table": recruiters_table
    }


@router.get("/api/recruiters/{wa_number}/vacancies")
async def api_recruiter_vacancies(
    wa_number: str,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Returns the micro view stats for a specific recruiter's job postings."""
    from sqlalchemy import func as sqlfunc
    from app.db.models import JobVacancy, Recruiter, CandidateApplication

    # Allow exact match or with '+' prefix if they stored it that way
    recruiter = db.query(Recruiter).filter(Recruiter.wa_number.like(f"%{wa_number.replace('+','')} ")).first()
    if not recruiter:
        recruiter = db.query(Recruiter).filter_by(wa_number=wa_number).first()
        
    if not recruiter:
        raise HTTPException(status_code=404, detail="Recruiter not found")
        
    vac_rows = (
        db.query(
            JobVacancy.job_title,
            JobVacancy.status,
            JobVacancy.created_at,
            sqlfunc.count(CandidateApplication.id).label("apps")
        )
        .outerjoin(CandidateApplication, CandidateApplication.vacancy_id == JobVacancy.id)
        .filter(JobVacancy.recruiter_id == recruiter.id)
        .group_by(JobVacancy.id)
        .order_by(JobVacancy.created_at.desc())
        .all()
    )
    
    vac_list = []
    for v in vac_rows:
        vac_list.append({
            "job_title": v.job_title,
            "status": v.status,
            "created_at": v.created_at.isoformat() if v.created_at else None,
            "total_applications": v.apps
        })
        
    return {
        "recruiter_name": recruiter.company_name,
        "joined_at": recruiter.created_at.isoformat() if recruiter.created_at else None,
        "vacancies": vac_list
    }

@router.get("/api/seekers/stats")
async def api_seekers_stats(
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Returns chart data and the overarching seekers stats table."""
    from sqlalchemy import func as sqlfunc
    from app.db.models import Candidate, CandidateApplication

    # Chart Data: Seekers by Location
    location_rows = db.query(
        Candidate.district, sqlfunc.count(Candidate.id).label("count")
    ).group_by(Candidate.district).all()
    
    locations = {}
    for r in location_rows:
        loc = r.district if r.district else "Unknown"
        locations[loc] = locations.get(loc, 0) + r.count
    
    # Chart Data: Seekers by Job Category
    category_rows = db.query(
        Candidate.category, sqlfunc.count(Candidate.id).label("count")
    ).group_by(Candidate.category).all()
    
    categories = {}
    for r in category_rows:
        cat = r.category if r.category else "Unknown"
        categories[cat] = categories.get(cat, 0) + r.count
    
    # Seekers rows
    seek_rows = (
        db.query(
            Candidate.id,
            Candidate.name,
            Candidate.wa_number,
            Candidate.district,
            Candidate.category,
            Candidate.sub_category,
            Candidate.created_at,
            sqlfunc.count(CandidateApplication.id).label("total_applications"),
        )
        .outerjoin(CandidateApplication, CandidateApplication.candidate_id == Candidate.id)
        .group_by(Candidate.id)
        .order_by(sqlfunc.count(CandidateApplication.id).desc())
        .limit(200) # Optional limit to keep frontend snappy
        .all()
    )
    
    seekers_table = []
    for s in seek_rows:
        skills = s.category or ""
        if s.sub_category:
            skills += f" ({s.sub_category})"
            
        seekers_table.append({
            "name": s.name,
            "wa_number": s.wa_number,
            "location": s.district or "Unknown",
            "skills": skills or "Unknown",
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "total_applications": s.total_applications
        })
        
    return {
        "chart_data": {
            "locations": locations,
            "categories": categories
        },
        "seekers_table": seekers_table
    }


@router.get("/api/seekers/{wa_number}/applications")
async def api_seeker_applications(
    wa_number: str,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Returns the micro view stats for a specific seeker's applications."""
    from sqlalchemy import func as sqlfunc
    from app.db.models import Candidate, CandidateApplication, JobVacancy, Recruiter

    # Allow exact match or format edge cases
    candidate = db.query(Candidate).filter(Candidate.wa_number.like(f"%{wa_number.replace('+','')} ")).first()
    if not candidate:
        candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
        
    if not candidate:
        raise HTTPException(status_code=404, detail="Seeker not found")
        
    app_rows = (
        db.query(
            JobVacancy.job_title,
            Recruiter.company_name,
            CandidateApplication.status,
            CandidateApplication.applied_at
        )
        .join(JobVacancy, JobVacancy.id == CandidateApplication.vacancy_id)
        .join(Recruiter, JobVacancy.recruiter_id == Recruiter.id)
        .filter(CandidateApplication.candidate_id == candidate.id)
        .order_by(CandidateApplication.applied_at.desc())
        .all()
    )
    
    app_list = []
    for a in app_rows:
        # Evaluate enums
        status_val = a.status.value if hasattr(a.status, "value") else str(a.status)
        
        app_list.append({
            "job_title": a.job_title,
            "company_name": a.company_name or "Confidential",
            "status": status_val,
            "applied_at": a.applied_at.isoformat() if a.applied_at else None
        })
        
    return {
        "seeker_name": candidate.name,
        "joined_at": candidate.created_at.isoformat() if candidate.created_at else None,
        "applications": app_list
    }

@router.get("/api/visitors/stats")
async def api_visitors_stats(
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Returns overarching conversational breakdown charts and the visitors list."""
    from sqlalchemy import func as sqlfunc
    from app.db.models import ConversationState, Candidate, Recruiter

    total_candidates = db.query(sqlfunc.count(Candidate.id)).scalar() or 0
    total_recruiters = db.query(sqlfunc.count(Recruiter.id)).scalar() or 0
    
    cand_subq = db.query(Candidate.wa_number).subquery("c_sub")
    rec_subq = db.query(Recruiter.wa_number).subquery("r_sub")

    unregistered_count = (
        db.query(sqlfunc.count(ConversationState.id))
        .outerjoin(cand_subq, ConversationState.wa_number == cand_subq.c.wa_number)
        .outerjoin(rec_subq, ConversationState.wa_number == rec_subq.c.wa_number)
        .filter(cand_subq.c.wa_number.is_(None))
        .filter(rec_subq.c.wa_number.is_(None))
        .scalar() or 0
    )

    breakdown = {
        "Job Seekers": total_candidates,
        "Recruiters": total_recruiters,
        "Unregistered": unregistered_count
    }

    funnel_rows = (
        db.query(ConversationState.state, sqlfunc.count(ConversationState.id).label("cnt"))
        .outerjoin(cand_subq, ConversationState.wa_number == cand_subq.c.wa_number)
        .outerjoin(rec_subq, ConversationState.wa_number == rec_subq.c.wa_number)
        .filter(cand_subq.c.wa_number.is_(None))
        .filter(rec_subq.c.wa_number.is_(None))
        .group_by(ConversationState.state)
        .all()
    )
    
    funnel = {}
    for r in funnel_rows:
        state_val = r.state if r.state else "idle"
        funnel[state_val] = funnel.get(state_val, 0) + r.cnt

    recent_visitors = (
        db.query(
            ConversationState.wa_number,
            ConversationState.state,
            ConversationState.updated_at,
            ConversationState.last_user_message_at,
            cand_subq.c.wa_number.label("cand_reg"),
            rec_subq.c.wa_number.label("rec_reg")
        )
        .outerjoin(cand_subq, ConversationState.wa_number == cand_subq.c.wa_number)
        .outerjoin(rec_subq, ConversationState.wa_number == rec_subq.c.wa_number)
        .order_by(ConversationState.updated_at.desc())
        .limit(200)
        .all()
    )

    visitors_list = []
    for v in recent_visitors:
        is_registered = bool(v.cand_reg or v.rec_reg)
        last_active = v.last_user_message_at or v.updated_at
        visitors_list.append({
            "wa_number": v.wa_number,
            "state": v.state,
            "last_active": last_active.isoformat() if last_active else None,
            "is_registered": is_registered
        })

    return {
        "chart_data": {
            "breakdown": breakdown,
            "funnel": funnel
        },
        "visitors_table": visitors_list
    }

@router.get("/api/visitors/{wa_number}/details")
async def api_visitor_details(
    wa_number: str,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Fetches full state analysis, role, and the contextual tracking parameter JSON."""
    from app.db.models import ConversationState, Candidate, Recruiter

    state_obj = db.query(ConversationState).filter_by(wa_number=wa_number).first()
    if not state_obj:
        state_obj = db.query(ConversationState).filter(ConversationState.wa_number.like(f"%{wa_number.replace('+','')} ")).first()
    if not state_obj:
        raise HTTPException(status_code=404, detail="Visitor state not found")

    candidate = db.query(Candidate).filter(Candidate.wa_number.like(f"%{wa_number.replace('+','')} ")).first() or db.query(Candidate).filter_by(wa_number=wa_number).first()
    recruiter = db.query(Recruiter).filter(Recruiter.wa_number.like(f"%{wa_number.replace('+','')} ")).first() or db.query(Recruiter).filter_by(wa_number=wa_number).first()

    status = "Unregistered"
    account_date = None
    if candidate:
        status = "Job Seeker"
        account_date = candidate.created_at.isoformat() if candidate.created_at else None
    elif recruiter:
        status = "Recruiter"
        account_date = recruiter.created_at.isoformat() if recruiter.created_at else None

    return {
        "wa_number": state_obj.wa_number,
        "current_state": state_obj.state,
        "registration_status": status,
        "account_created_at": account_date,
        "context": state_obj.context or {}
    }

@router.get("/api/unregistered/recovery-list")
async def api_unregistered_recovery_list(
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Retrieves exclusively unregistered visitors bucketed by drop-off recency."""
    from datetime import datetime, timezone
    from app.db.models import ConversationState, Candidate, Recruiter

    cand_subq = db.query(Candidate.wa_number).subquery("c_sub")
    rec_subq = db.query(Recruiter.wa_number).subquery("r_sub")

    recent_unregistered = (
        db.query(
            ConversationState.wa_number,
            ConversationState.state,
            ConversationState.updated_at,
            ConversationState.last_user_message_at
        )
        .outerjoin(cand_subq, ConversationState.wa_number == cand_subq.c.wa_number)
        .outerjoin(rec_subq, ConversationState.wa_number == rec_subq.c.wa_number)
        .filter(cand_subq.c.wa_number.is_(None))
        .filter(rec_subq.c.wa_number.is_(None))
        .order_by(ConversationState.updated_at.desc())
        .limit(200)
        .all()
    )

    now = datetime.now(timezone.utc)
    buckets = {
        "< 24 Hours": 0,
        "1-3 Days": 0,
        "3-7 Days": 0,
        "7+ Days": 0
    }

    leads = []
    for r in recent_unregistered:
        last_active = r.last_user_message_at or r.updated_at
        
        # Determine lead temperature bucket
        if last_active:
            if last_active.tzinfo is None:
                last_active = last_active.replace(tzinfo=timezone.utc)
            delta = now - last_active
            days = delta.days
            
            if days < 1:
                buckets["< 24 Hours"] += 1
            elif 1 <= days <= 3:
                buckets["1-3 Days"] += 1
            elif 3 < days <= 7:
                buckets["3-7 Days"] += 1
            else:
                buckets["7+ Days"] += 1

        leads.append({
            "wa_number": r.wa_number,
            "state": r.state if r.state else "idle",
            "last_active": last_active.isoformat() if last_active else None
        })

    return {
        "chart_data": {
            "temperature": buckets
        },
        "table_data": leads
    }

@router.get("/api/dual-users/stats")
async def api_dual_users_stats(
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Correlates inner joins of wa_number to find dual registration users."""
    from sqlalchemy import func
    from app.db.models import Candidate, Recruiter, JobVacancy, CandidateApplication
    
    cand_subq = (
        db.query(
            Candidate.wa_number,
            Candidate.name,
            func.count(CandidateApplication.id).label("app_count")
        )
        .outerjoin(CandidateApplication, Candidate.id == CandidateApplication.candidate_id)
        .group_by(Candidate.wa_number, Candidate.name)
        .subquery("c_sub")
    )
    
    rec_subq = (
        db.query(
            Recruiter.wa_number,
            Recruiter.company_name,
            func.count(JobVacancy.id).label("vac_count")
        )
        .outerjoin(JobVacancy, Recruiter.id == JobVacancy.recruiter_id)
        .group_by(Recruiter.wa_number, Recruiter.company_name)
        .subquery("r_sub")
    )
    
    dual_rows = (
        db.query(
            cand_subq.c.wa_number,
            cand_subq.c.name,
            cand_subq.c.app_count,
            rec_subq.c.company_name,
            rec_subq.c.vac_count
        )
        .join(rec_subq, cand_subq.c.wa_number == rec_subq.c.wa_number)
        .all()
    )
    
    buckets = {
        "Primarily Recruiter": 0,
        "Primarily Seeker": 0,
        "Balanced": 0
    }
    
    table_data = []
    
    for r in dual_rows:
        vacs = r.vac_count or 0
        apps = r.app_count or 0
        
        if vacs > apps:
            buckets["Primarily Recruiter"] += 1
        elif apps > vacs:
            buckets["Primarily Seeker"] += 1
        else:
            buckets["Balanced"] += 1
            
        table_data.append({
            "wa_number": r.wa_number,
            "candidate_name": r.name,
            "company_name": r.company_name,
            "vacancies_posted": vacs,
            "applications_sent": apps
        })
        
    return {
        "chart_data": {
            "primary_persona": buckets
        },
        "table_data": table_data
    }
