"""
Admin dashboard router.
Provides protected routes for managing:
- Vacancies (approve / reject)
- Callback requests
- Abandoned subscription candidates

Auth Modes (all routes accept any of these):
  1. Cookie session  — set by POST /admin/login (role = 'super_admin')
  2. Bearer <token>  — magic-link session (role must be 'admin')
  3. Basic base64    — legacy username:password header

The JobZon admin panel lives at /jobzon/* (see routers/jobzon.py)
and uses its own require_jobzon_admin dependency.
"""
import hashlib
import hmac
import secrets
import logging
from typing import Optional
from datetime import datetime, timezone, timedelta, date

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
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

# ─── Cookie session helpers ───────────────────────────────────────────────────
# Simple HMAC-signed cookie: value = "<role>:<timestamp>:<hmac>"
# No DB needed — stateless verification with role encoded in the cookie.

_COOKIE_NAME = "ji_admin_session"
_COOKIE_TTL  = 8 * 3600   # 8 hours


def _sign_session(role: str) -> str:
    """Return a signed session string encoding the role and creation timestamp."""
    ts  = str(int(datetime.now(timezone.utc).timestamp()))
    raw = f"{role}:{ts}"
    sig = hmac.new(settings.secret_key.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}:{sig}"


def _verify_session(cookie_value: str) -> str | None:
    """
    Verify the cookie and return the role string, or None if invalid/expired.
    Returns: 'super_admin' | 'jobzon_admin' | None
    """
    try:
        parts = cookie_value.rsplit(":", 1)
        if len(parts) != 2:
            return None
        raw, sig = parts
        expected = hmac.new(settings.secret_key.encode(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        role_part, ts_part = raw.split(":", 1)
        created = int(ts_part)
        age = datetime.now(timezone.utc).timestamp() - created
        if age > _COOKIE_TTL:
            return None
        return role_part
    except Exception:
        return None


# ─── Login / Logout routes ────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request):
    """Render the shared admin login page (used by both super admin and JobZon admin)."""
    # If already logged in, redirect to the appropriate panel
    cookie = request.cookies.get(_COOKIE_NAME, "")
    role = _verify_session(cookie) if cookie else None
    if role == "super_admin":
        return RedirectResponse(url="/admin", status_code=302)
    if role == "jobzon_admin":
        return RedirectResponse(url="/jobzon", status_code=302)
    return templates.TemplateResponse("admin/login.html", {"request": request, "error": None})


@router.post("/login", include_in_schema=False)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    """
    Validate credentials and set an HMAC-signed session cookie.
    Redirects to /admin (super admin) or /jobzon (JobZon admin) based on role.
    """
    # Check super admin credentials
    ok_user = secrets.compare_digest(username, settings.admin_username)
    ok_pass = secrets.compare_digest(password, settings.admin_password)
    if ok_user and ok_pass and settings.admin_username:
        response = RedirectResponse(url="/admin", status_code=303)
        response.set_cookie(
            key=_COOKIE_NAME,
            value=_sign_session("super_admin"),
            httponly=True,
            samesite="lax",
            max_age=_COOKIE_TTL,
        )
        return response

    # Check JobZon admin credentials
    jz_ok_user = secrets.compare_digest(username, settings.jobzon_admin_username)
    jz_ok_pass = secrets.compare_digest(password, settings.jobzon_admin_password)
    if jz_ok_user and jz_ok_pass and settings.jobzon_admin_username:
        response = RedirectResponse(url="/jobzon", status_code=303)
        response.set_cookie(
            key=_COOKIE_NAME,
            value=_sign_session("jobzon_admin"),
            httponly=True,
            samesite="lax",
            max_age=_COOKIE_TTL,
        )
        return response

    # Bad credentials — re-render login page with error
    return templates.TemplateResponse(
        "admin/login.html",
        {"request": request, "error": "Invalid username or password."},
        status_code=401,
    )


@router.get("/logout", include_in_schema=False)
async def logout():
    """Clear the session cookie and redirect to the JobInfo home page."""
    response = RedirectResponse(url="https://jobinfo.pro/recruiter.html", status_code=302)
    response.delete_cookie(_COOKIE_NAME)
    return response


# ─── Auth dependencies ────────────────────────────────────────────────────────

def require_admin(request: Request, db: Session = Depends(get_db)) -> str:
    """
    Triple-mode super-admin auth (must NOT be jobzon_admin):
    1. Cookie session  — role == 'super_admin'
    2. Bearer <token>  — magic-link session (role must be 'admin')
    3. Basic base64    — classic username/password header
    """
    # ── Mode 1: Cookie session ─────────────────────────────────────────────────
    cookie = request.cookies.get(_COOKIE_NAME, "")
    if cookie:
        role = _verify_session(cookie)
        if role == "super_admin":
            return "admin"
        if role == "jobzon_admin":
            # JobZon admin tried to access a super-admin route — forbidden
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="JobZon admin cannot access this area.",
            )

    auth_header = request.headers.get("Authorization", "")

    # ── Mode 2: Bearer token (magic-link session) ──────────────────────────────
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        from app.routers.api import _get_session_data
        session = _get_session_data(token)
        if not session or session.get("role") != "admin":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired admin session token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return session.get("wa_number", "admin")

    # ── Mode 3: Basic Auth (username + password) ───────────────────────────────
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

    # ── No valid auth — redirect to login page ─────────────────────────────────
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Basic"},
    )


def require_jobzon_admin(request: Request) -> str:
    """
    Auth dependency exclusively for JobZon admin panel routes (/jobzon/*).
    Only accepts the signed session cookie with role == 'jobzon_admin'.
    Super admin cookies are intentionally rejected here — panel isolation is strict.
    Raises HTTP 302 redirect to /admin/login if not authenticated.
    """
    cookie = request.cookies.get(_COOKIE_NAME, "")
    if cookie:
        role = _verify_session(cookie)
        if role == "jobzon_admin":
            return "jobzon_admin"
    # Not authenticated as jobzon_admin — redirect to shared login
    raise HTTPException(
        status_code=status.HTTP_302_FOUND,
        headers={"Location": "/admin/login"},
    )


# ─── Dashboard ────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def admin_home(
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    from app.db.models import UserQuestion
    pending_count = db.query(JobVacancy).filter_by(status="pending").count()
    gethelp_count = db.query(GetHelpRequest).filter_by(resolved=False).count()
    question_count = db.query(UserQuestion).filter_by(is_resolved=False).count()
    return templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request,
            "pending_count": pending_count,
            "inbox_count": gethelp_count + question_count,
        },
    )


# ─── Shared sidebar context helper ────────────────────────────────────────────

def _sidebar_ctx(db) -> dict:
    """Returns badge counts for the sidebar — called by every page route."""
    from app.db.models import UserQuestion
    pending = db.query(JobVacancy).filter_by(status="pending").count()
    help_open = db.query(GetHelpRequest).filter_by(resolved=False).count()
    q_open = db.query(UserQuestion).filter_by(is_resolved=False).count()
    return {"pending_count": pending, "inbox_count": help_open + q_open}


# ─── Vacancies page ───────────────────────────────────────────────────────────

@router.get("/vacancies", response_class=HTMLResponse)
async def page_vacancies(
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    return templates.TemplateResponse(
        "admin/vacancies.html", {"request": request, **_sidebar_ctx(db)}
    )


# ─── Vacancy Insights page ────────────────────────────────────────────────────

@router.get("/vacancy-insights", response_class=HTMLResponse)
async def page_vacancy_insights(
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    return templates.TemplateResponse(
        "admin/vacancy_insights.html", {"request": request, **_sidebar_ctx(db)}
    )


# ─── Recruiters page ──────────────────────────────────────────────────────────

@router.get("/recruiters", response_class=HTMLResponse)
async def page_recruiters(
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    return templates.TemplateResponse(
        "admin/recruiters.html", {"request": request, **_sidebar_ctx(db)}
    )


# ─── Seekers page ─────────────────────────────────────────────────────────────

@router.get("/seekers", response_class=HTMLResponse)
async def page_seekers(
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    return templates.TemplateResponse(
        "admin/seekers.html", {"request": request, **_sidebar_ctx(db)}
    )


# ─── Bot Visitors / Analytics page ───────────────────────────────────────────

@router.get("/visitors", response_class=HTMLResponse)
async def page_visitors(
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    return templates.TemplateResponse(
        "admin/visitors.html", {"request": request, **_sidebar_ctx(db)}
    )


# ─── Unregistered Leads page ──────────────────────────────────────────────────

@router.get("/leads", response_class=HTMLResponse)
async def page_leads(
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    return templates.TemplateResponse(
        "admin/leads.html", {"request": request, **_sidebar_ctx(db)}
    )


# ─── User Dynamics page ───────────────────────────────────────────────────────

@router.get("/user-dynamics", response_class=HTMLResponse)
async def page_user_dynamics(
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    return templates.TemplateResponse(
        "admin/user_dynamics.html", {"request": request, **_sidebar_ctx(db)}
    )


# ─── Dual Users page ──────────────────────────────────────────────────────────

@router.get("/dual-users", response_class=HTMLResponse)
async def page_dual_users(
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    return templates.TemplateResponse(
        "admin/dual_users.html", {"request": request, **_sidebar_ctx(db)}
    )


# ─── Inbox (Help + Questions) page ───────────────────────────────────────────

@router.get("/inbox", response_class=HTMLResponse)
async def page_inbox(
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    return templates.TemplateResponse(
        "admin/inbox.html", {"request": request, **_sidebar_ctx(db)}
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
                "registrant_role": getattr(v.recruiter, "registrant_role", "other") or "other",
                "location": v.recruiter.location,
                "business_contact": v.recruiter.business_contact,
                "created_at": v.recruiter.created_at.isoformat() if v.recruiter.created_at else None,
            } if v.recruiter else None,
        })


    return {"total": len(results), "results": results}


@router.post("/api/vacancies/{vacancy_id}/approve")
@router.post("/api/vacancies/{vacancy_id}/re-verify")
async def api_approve_vacancy(
    vacancy_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Approve or re-verify a vacancy and notify the recruiter via WhatsApp."""
    vacancy = db.query(JobVacancy).filter_by(id=vacancy_id).first()
    if not vacancy:
        raise HTTPException(status_code=404, detail="Vacancy not found")
    
    vacancy.status = "approved"
    vacancy.is_active = True
    vacancy.rejection_reason = None
    vacancy.approved_at = datetime.now(timezone.utc)
    vacancy.last_enabled_at = vacancy.approved_at
    db.commit()

    await recruiter_handler.notify_recruiter_approval(vacancy_id, db)
    return {"success": True, "vacancy_id": vacancy_id, "job_code": vacancy.job_code}


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
    from app.whatsapp.templates import _label, SALARY_LABELS

    vacancy = db.query(JobVacancy).filter_by(id=vacancy_id, status="approved").first()
    if not vacancy:
        raise HTTPException(status_code=404, detail="Approved vacancy not found")

    apply_link = f"https://wa.me/{settings.business_wa_number}?text=Apply%20{vacancy.job_code}"

    salary = _label(SALARY_LABELS, vacancy.salary_range)
    job_mode = vacancy.job_mode or "—"
    experience = vacancy.experience_required or "—"
    description = vacancy.job_description[:400] + ("…" if len(vacancy.job_description) > 400 else "") if vacancy.job_description else "—"
    cv_note     = "Yes – CV required" if vacancy.cv_required else "No – CV optional"
    
    lines = [
        f"🚀 *New Job Alert*",
        f"",
        f"🏷️ Position: *{vacancy.job_title.strip()}*",
        f"🏢 Company: {vacancy.recruiter.company_name if vacancy.recruiter and vacancy.recruiter.company_name else '—'}",
        f"📍 Location: {vacancy.exact_location or '—'}, {vacancy.district_region or '—'}",
        f"💰 Salary: {salary}",
        f"💼 Mode: {job_mode}",
        f"🎓 Experience: {experience}",
        f"📄 CV Required: {cv_note}\n"
        f"🔖 Job Code: {vacancy.job_code}",
        f"",
        f"📋 *About the Role:*",
        description,
        f"",
        f"📲 Apply now: {apply_link}",
        f"",
        f"_Kerala's First WhatsApp powered Career Portal_"
    ]

    body_text = "\n".join(lines)

    from app.db.models import AdminNotificationQueue

    success_count = 0
    queued_count = 0

    for admin_num in settings.approval_admins:
        admin_state = db.query(ConversationState).filter_by(wa_number=admin_num).first()
        is_active = False
        if admin_state and admin_state.last_user_message_at:
            last = admin_state.last_user_message_at
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - last).total_seconds() < 86_400:
                is_active = True

        if is_active:
            try:
                # Using send_text instead of send_to_channel just in case
                await wa_client.send_text(to=admin_num, body=body_text)
                success_count += 1
            except Exception as e:
                # Log but continue
                pass
        else:
            queue_item = AdminNotificationQueue(
                wa_number=admin_num,
                notification_type="approved_vacancy",
                vacancy_id=vacancy.id
            )
            db.add(queue_item)
            queued_count += 1
            
    db.commit()

    return {"success": True, "vacancy_id": vacancy_id, "job_code": vacancy.job_code}


@router.get("/api/analytics")
async def api_analytics(
    period: str = "last_30_days",
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    """
    Returns comprehensive platform analytics for the admin dashboard.
    Supports time periods: last_30_days, this_month, prev_month, prev_month_2, this_week, prev_week, prev_week_2.
    """
    from sqlalchemy import func as sqlfunc
    from app.db.models import (
        Candidate, CandidateApplication, JobVacancy, Recruiter, ConversationState
    )

    # ── Platform totals ──────────────────────────────────────────────────────
    total_recruiters = db.query(Recruiter).count()
    total_candidates = db.query(Candidate).count()
    total_applications = db.query(CandidateApplication).count()

    # Unregistered users: bot conversations not registered as Candidate or Recruiter
    seek_wa_q = db.query(Candidate.wa_number)
    rec_wa_q = db.query(Recruiter.wa_number)
    unregistered_users = db.query(ConversationState).filter(
        ~ConversationState.wa_number.in_(seek_wa_q),
        ~ConversationState.wa_number.in_(rec_wa_q)
    ).count()

    # ── Vacancy status counts ────────────────────────────────────────────────
    pending_count = db.query(JobVacancy).filter_by(status="pending").count()
    approved_count = db.query(JobVacancy).filter(JobVacancy.status == "approved", JobVacancy.is_active == True).count()
    stopped_count = db.query(JobVacancy).filter(JobVacancy.is_active == False, JobVacancy.status != "rejected").count()
    total_vac_status = pending_count + approved_count + stopped_count

    # ── Date calculation for selected period ─────────────────────────────────
    today = date.today()

    if period == "this_month":
        start_date = date(today.year, today.month, 1)
        if today.month == 12:
            next_m = date(today.year + 1, 1, 1)
        else:
            next_m = date(today.year, today.month + 1, 1)
        end_date = next_m - timedelta(days=1)
        period_label = today.strftime("%B %Y")
    elif period == "prev_month":
        first_this_month = date(today.year, today.month, 1)
        end_date = first_this_month - timedelta(days=1)
        start_date = date(end_date.year, end_date.month, 1)
        period_label = end_date.strftime("%B %Y")
    elif period == "prev_month_2":
        first_this_month = date(today.year, today.month, 1)
        prev_m_end = first_this_month - timedelta(days=1)
        prev_m_start = date(prev_m_end.year, prev_m_end.month, 1)
        end_date = prev_m_start - timedelta(days=1)
        start_date = date(end_date.year, end_date.month, 1)
        period_label = end_date.strftime("%B %Y")
    elif period == "this_week":
        start_date = today - timedelta(days=today.weekday())
        end_date = start_date + timedelta(days=6)
        period_label = f"This Week ({start_date.strftime('%b %d')} - {end_date.strftime('%b %d')})"
    elif period == "prev_week":
        this_mon = today - timedelta(days=today.weekday())
        start_date = this_mon - timedelta(days=7)
        end_date = start_date + timedelta(days=6)
        period_label = f"Previous Week ({start_date.strftime('%b %d')} - {end_date.strftime('%b %d')})"
    elif period == "prev_week_2":
        this_mon = today - timedelta(days=today.weekday())
        start_date = this_mon - timedelta(days=14)
        end_date = start_date + timedelta(days=6)
        period_label = f"Week Before Prev ({start_date.strftime('%b %d')} - {end_date.strftime('%b %d')})"
    else:  # "last_30_days" (default: rolling active 30-day window)
        period = "last_30_days"
        start_date = today - timedelta(days=29)
        end_date = today
        period_label = f"Last 30 Days ({start_date.strftime('%b %d')} - {end_date.strftime('%b %d')})"

    num_days = (end_date - start_date).days + 1
    dates = [start_date + timedelta(days=i) for i in range(num_days)]
    next_day = end_date + timedelta(days=1)

    # 1. Daily vacancies
    vac_daily_raw = (
        db.query(
            sqlfunc.date(JobVacancy.created_at).label("day"),
            sqlfunc.count(JobVacancy.id).label("cnt")
        )
        .filter(JobVacancy.created_at >= start_date, JobVacancy.created_at < next_day)
        .group_by(sqlfunc.date(JobVacancy.created_at))
        .all()
    )
    vac_by_day = {str(row.day): row.cnt for row in vac_daily_raw}
    vacancy_daily = [{"date": str(d), "day": d.day, "count": vac_by_day.get(str(d), 0)} for d in dates]

    # 2. Daily applications
    app_daily_raw = (
        db.query(
            sqlfunc.date(CandidateApplication.applied_at).label("day"),
            sqlfunc.count(CandidateApplication.id).label("cnt")
        )
        .filter(CandidateApplication.applied_at >= start_date, CandidateApplication.applied_at < next_day)
        .group_by(sqlfunc.date(CandidateApplication.applied_at))
        .all()
    )
    app_by_day = {str(row.day): row.cnt for row in app_daily_raw}
    applications_daily = [{"date": str(d), "day": d.day, "count": app_by_day.get(str(d), 0)} for d in dates]

    # 3. Daily recruiters
    rec_daily_raw = (
        db.query(
            sqlfunc.date(Recruiter.created_at).label("day"),
            sqlfunc.count(Recruiter.id).label("cnt")
        )
        .filter(Recruiter.created_at >= start_date, Recruiter.created_at < next_day)
        .group_by(sqlfunc.date(Recruiter.created_at))
        .all()
    )
    rec_by_day = {str(row.day): row.cnt for row in rec_daily_raw}
    recruiters_daily = [{"date": str(d), "day": d.day, "count": rec_by_day.get(str(d), 0)} for d in dates]

    # 4. Daily job seekers
    seek_daily_raw = (
        db.query(
            sqlfunc.date(Candidate.created_at).label("day"),
            sqlfunc.count(Candidate.id).label("cnt")
        )
        .filter(Candidate.created_at >= start_date, Candidate.created_at < next_day)
        .group_by(sqlfunc.date(Candidate.created_at))
        .all()
    )
    seek_by_day = {str(row.day): row.cnt for row in seek_daily_raw}
    seekers_daily = [{"date": str(d), "day": d.day, "count": seek_by_day.get(str(d), 0)} for d in dates]

    period_totals = {
        "vacancies": sum(x["count"] for x in vacancy_daily),
        "applications": sum(x["count"] for x in applications_daily),
        "recruiters": sum(x["count"] for x in recruiters_daily),
        "seekers": sum(x["count"] for x in seekers_daily),
    }

    return {
        "totals": {
            "recruiters": total_recruiters,
            "candidates": total_candidates,
            "unregistered": unregistered_users,
            "applications": total_applications,
        },
        "vacancy_status": {
            "pending": pending_count,
            "approved": approved_count,
            "stopped": stopped_count,
            "total": total_vac_status,
        },
        "period": period,
        "period_label": period_label,
        "labels": [d.day for d in dates],
        "vacancy_daily": vacancy_daily,
        "applications_daily": applications_daily,
        "recruiters_daily": recruiters_daily,
        "seekers_daily": seekers_daily,
        "period_totals": period_totals,
    }


# ─── Vacanciesys (Approved & Stopped Vacancies with LIFO, Filters & Apps) ──────

@router.get("/api/vacanciesys")
async def api_vacanciesys(
    limit: int = 8,
    offset: int = 0,
    district: Optional[str] = None,
    category: Optional[str] = None,
    sort_by: str = "lifo",
    min_apps: Optional[int] = None,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    """
    Returns Approved and Stopped vacancies in LIFO (newest first) or filtered by application count.
    Supports district, category, application thresholds, and pagination for 'Load More'.
    """
    from sqlalchemy import func as sqlfunc, or_
    from app.db.models import JobVacancy, CandidateApplication

    base_filter = or_(JobVacancy.status == "approved", JobVacancy.is_active == False)

    # Subquery for application counts per vacancy
    app_subq = (
        db.query(
            CandidateApplication.vacancy_id.label("vac_id"),
            sqlfunc.count(CandidateApplication.id).label("apps_cnt"),
        )
        .group_by(CandidateApplication.vacancy_id)
        .subquery()
    )

    query = (
        db.query(
            JobVacancy,
            sqlfunc.coalesce(app_subq.c.apps_cnt, 0).label("apps_count"),
        )
        .outerjoin(app_subq, app_subq.c.vac_id == JobVacancy.id)
        .filter(base_filter)
    )

    if district:
        query = query.filter(JobVacancy.district_region == district)
    if category:
        query = query.filter(JobVacancy.job_category == category)
    if min_apps is not None and min_apps > 0:
        query = query.filter(sqlfunc.coalesce(app_subq.c.apps_cnt, 0) >= min_apps)

    total_count = query.count()

    if sort_by == "apps_desc":
        query = query.order_by(sqlfunc.coalesce(app_subq.c.apps_cnt, 0).desc(), JobVacancy.created_at.desc())
    elif sort_by == "apps_asc":
        query = query.order_by(sqlfunc.coalesce(app_subq.c.apps_cnt, 0).asc(), JobVacancy.created_at.desc())
    else:  # "lifo" - newest first
        query = query.order_by(JobVacancy.created_at.desc())

    rows = query.offset(offset).limit(limit).all()

    # Distinct districts & categories for filter selectors
    districts = [
        r[0] for r in db.query(JobVacancy.district_region)
        .filter(base_filter)
        .distinct()
        .order_by(JobVacancy.district_region)
        .all()
        if r[0]
    ]
    categories = [
        r[0] for r in db.query(JobVacancy.job_category)
        .filter(base_filter)
        .distinct()
        .order_by(JobVacancy.job_category)
        .all()
        if r[0]
    ]

    items = []
    for v, apps in rows:
        rec = v.recruiter
        items.append({
            "id": v.id,
            "job_code": v.job_code,
            "job_title": v.job_title,
            "job_category": v.job_category,
            "district_region": v.district_region,
            "exact_location": v.exact_location,
            "status": "stopped" if not v.is_active else v.status,
            "is_active": v.is_active,
            "created_at": v.created_at.isoformat() if v.created_at else None,
            "approved_at": v.approved_at.isoformat() if v.approved_at else None,
            "stopped_at": v.stopped_at.isoformat() if v.stopped_at else None,
            "job_mode": v.job_mode,
            "experience_required": v.experience_required,
            "salary_range": v.salary_range,
            "job_description": v.job_description or "",
            "applications": int(apps or 0),
            "recruiter": {
                "id": rec.id if rec else None,
                "company_name": rec.company_name if rec else "Unknown",
                "wa_number": rec.wa_number if rec else "",
                "business_type": rec.business_type if rec else "",
                "location": rec.location if rec else "",
                "business_contact": rec.business_contact if rec else "",
            } if rec else None,
        })

    return {
        "items": items,
        "total": total_count,
        "has_more": (offset + limit) < total_count,
        "districts": districts,
        "categories": categories,
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


@router.post("/api/questions/{question_id}/reopen")
async def api_reopen_question(
    question_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Marks a user question as reopened (pending)."""
    q = db.query(UserQuestion).filter_by(id=question_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="Question not found")
    q.is_resolved = False
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


@router.patch("/api/help-requests/{request_id}/reopen")
async def api_reopen_help_request(
    request_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Marks a help request as reopened (pending)."""
    req = db.query(GetHelpRequest).filter_by(id=request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Help request not found")
    req.resolved = False
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


@router.get("/api/vacancy-insights/stats")
async def api_vacancy_insights_stats(
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Returns vacancies breakdown by district and category (excluding rejected) for Vacancy Insights."""
    from sqlalchemy import func as sqlfunc
    from app.db.models import JobVacancy
    from app.handlers.seeker import CATEGORY_DISPLAY_NAMES

    # Chart Data: Vacancies by District
    district_rows = (
        db.query(JobVacancy.district_region, sqlfunc.count(JobVacancy.id).label("count"))
        .filter(JobVacancy.status != "rejected")
        .group_by(JobVacancy.district_region)
        .all()
    )
    districts: dict[str, int] = {}
    for r in district_rows:
        raw_d = (r.district_region or "Other").strip()
        d_name = "GCC" if raw_d.upper() == "GCC" else raw_d.title()
        districts[d_name] = districts.get(d_name, 0) + r.count
    
    # Chart Data: Vacancies by Category
    legacy_cat_map = {
        "driving": "Driving, Logistics & Store Keeper",
        "logistics": "Driving, Logistics & Store Keeper",
        "it_professional": "IT & Digital Marketing",
        "office_admin": "Office Admin & Data Entry",
        "hospitality_service": "Hospitality & Food Service",
    }
    categories: dict[str, int] = {disp: 0 for disp in CATEGORY_DISPLAY_NAMES.values()}
    
    category_rows = (
        db.query(JobVacancy.job_category, sqlfunc.count(JobVacancy.id).label("count"))
        .filter(JobVacancy.status != "rejected")
        .group_by(JobVacancy.job_category)
        .all()
    )
    for r in category_rows:
        raw_k = r.job_category or "other"
        disp = CATEGORY_DISPLAY_NAMES.get(raw_k) or legacy_cat_map.get(raw_k) or raw_k.replace("_", " ").title()
        categories[disp] = categories.get(disp, 0) + r.count

    return {
        "chart_data": {
            "districts": districts,
            "categories": categories
        }
    }


@router.get("/api/recruiters/stats")
async def api_recruiters_stats(
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Returns chart data and the overarching recruiter stats table."""
    from sqlalchemy import func as sqlfunc
    from app.db.models import JobVacancy, Recruiter, CandidateApplication
    from app.handlers.seeker import CATEGORY_DISPLAY_NAMES

    # Chart Data: Recruiters by Location
    location_rows = (
        db.query(Recruiter.location, sqlfunc.count(Recruiter.id).label("count"))
        .group_by(Recruiter.location)
        .all()
    )
    locations: dict[str, int] = {}
    for r in location_rows:
        raw_loc = (r.location or "Other").strip()
        loc_name = "GCC" if raw_loc.upper() == "GCC" else raw_loc.title()
        locations[loc_name] = locations.get(loc_name, 0) + r.count

    # Chart Data: Recruiters by Business Type
    btype_rows = (
        db.query(Recruiter.business_type, sqlfunc.count(Recruiter.id).label("count"))
        .group_by(Recruiter.business_type)
        .all()
    )
    business_types: dict[str, int] = {}
    for r in btype_rows:
        raw_bt = (r.business_type or "Other").strip()
        bt_name = raw_bt.replace("_", " ").title()
        business_types[bt_name] = business_types.get(bt_name, 0) + r.count

    # Chart Data: Vacancies by District (retained for backward compatibility)
    district_rows = (
        db.query(JobVacancy.district_region, sqlfunc.count(JobVacancy.id).label("count"))
        .filter(JobVacancy.status != "rejected")
        .group_by(JobVacancy.district_region)
        .all()
    )
    districts: dict[str, int] = {}
    for r in district_rows:
        raw_d = (r.district_region or "Other").strip()
        d_name = "GCC" if raw_d.upper() == "GCC" else raw_d.title()
        districts[d_name] = districts.get(d_name, 0) + r.count
    
    # Chart Data: Vacancies by Category (retained for backward compatibility)
    legacy_cat_map = {
        "driving": "Driving, Logistics & Store Keeper",
        "logistics": "Driving, Logistics & Store Keeper",
        "it_professional": "IT & Digital Marketing",
        "office_admin": "Office Admin & Data Entry",
        "hospitality_service": "Hospitality & Food Service",
    }
    categories: dict[str, int] = {disp: 0 for disp in CATEGORY_DISPLAY_NAMES.values()}
    
    category_rows = (
        db.query(JobVacancy.job_category, sqlfunc.count(JobVacancy.id).label("count"))
        .filter(JobVacancy.status != "rejected")
        .group_by(JobVacancy.job_category)
        .all()
    )
    for r in category_rows:
        raw_k = r.job_category or "other"
        disp = CATEGORY_DISPLAY_NAMES.get(raw_k) or legacy_cat_map.get(raw_k) or raw_k.replace("_", " ").title()
        categories[disp] = categories.get(disp, 0) + r.count
    
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

    # Recruiter rows ordered by most recently joined first
    rec_rows = (
        db.query(
            Recruiter.id,
            Recruiter.company_name,
            Recruiter.business_type,
            Recruiter.registrant_role,
            Recruiter.location,
            Recruiter.wa_number,
            Recruiter.created_at,
            sqlfunc.max(JobVacancy.created_at).label("last_activity"),
            sqlfunc.count(JobVacancy.id).label("total_vacancies"),
        )
        .outerjoin(JobVacancy, JobVacancy.recruiter_id == Recruiter.id)
        .group_by(Recruiter.id)
        .order_by(Recruiter.created_at.desc())
        .all()
    )
    
    recruiters_table = []
    for r in rec_rows:
        total_vac = r.total_vacancies
        tot_apps = apps_map.get(r.id, 0)
        avg_apps = round(tot_apps / total_vac, 1) if total_vac > 0 else 0.0
        
        last_act = r.last_activity if r.last_activity else r.created_at
        
        recruiters_table.append({
            "id": r.id,
            "company_name": r.company_name,
            "business_type": r.business_type,
            "registrant_role": r.registrant_role or "other",
            "location": r.location,
            "wa_number": r.wa_number,
            "last_activity": last_act.isoformat() if last_act else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "total_vacancies": total_vac,
            "total_applications": tot_apps,
            "avg_apps_per_vacancy": avg_apps
        })
        
    return {
        "chart_data": {
            "locations": locations,
            "business_types": business_types,
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

    clean_wa = wa_number.replace('+', '').strip()
    recruiter = db.query(Recruiter).filter(
        (Recruiter.wa_number == wa_number) |
        (Recruiter.wa_number == clean_wa) |
        (Recruiter.wa_number.like(f"%{clean_wa}%"))
    ).first()
        
    if not recruiter:
        raise HTTPException(status_code=404, detail="Recruiter not found")
        
    vac_rows = (
        db.query(
            JobVacancy.id,
            JobVacancy.job_code,
            JobVacancy.job_title,
            JobVacancy.status,
            JobVacancy.is_active,
            JobVacancy.district_region,
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
            "id": v.id,
            "job_code": v.job_code,
            "job_title": v.job_title,
            "status": v.status,
            "is_active": v.is_active,
            "district_region": v.district_region,
            "created_at": v.created_at.isoformat() if v.created_at else None,
            "total_applications": v.apps
        })
        
    return {
        "recruiter_name": recruiter.company_name,
        "registrant_role": getattr(recruiter, "registrant_role", "other") or "other",
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
    
    locations: dict[str, int] = {}
    for r in location_rows:
        raw_loc = (r.district or "Other").strip()
        loc_name = "GCC" if raw_loc.upper() == "GCC" else raw_loc.title()
        locations[loc_name] = locations.get(loc_name, 0) + r.count
    
    # Chart Data: Seekers by Job Category
    from app.handlers.seeker import CATEGORY_DISPLAY_NAMES
    legacy_cat_map = {
        "driving": "Driving, Logistics & Store Keeper",
        "logistics": "Driving, Logistics & Store Keeper",
        "it_professional": "IT & Digital Marketing",
        "office_admin": "Office Admin & Data Entry",
        "hospitality_service": "Hospitality & Food Service",
    }
    categories: dict[str, int] = {disp: 0 for disp in CATEGORY_DISPLAY_NAMES.values()}
    
    category_rows = db.query(
        Candidate.category, sqlfunc.count(Candidate.id).label("count")
    ).group_by(Candidate.category).all()
    
    for r in category_rows:
        raw_k = r.category or "other"
        disp = CATEGORY_DISPLAY_NAMES.get(raw_k) or legacy_cat_map.get(raw_k) or raw_k.replace("_", " ").title()
        categories[disp] = categories.get(disp, 0) + r.count
    
    # Seekers rows (LIFO order: most recently joined first)
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
        .order_by(Candidate.created_at.desc())
        .limit(200) # Keep snappy
        .all()
    )
    
    seekers_table = []
    for s in seek_rows:
        raw_cat = s.category or ""
        cat_disp = CATEGORY_DISPLAY_NAMES.get(raw_cat) or legacy_cat_map.get(raw_cat) or raw_cat.replace("_", " ").title()
        skills = cat_disp
        if s.sub_category:
            skills += f" ({s.sub_category})"
            
        seekers_table.append({
            "id": s.id,
            "name": s.name,
            "wa_number": s.wa_number,
            "location": (s.district or "Unknown").title(),
            "skills": skills or "General",
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

    clean_wa = wa_number.replace('+', '').strip()
    candidate = db.query(Candidate).filter(
        (Candidate.wa_number == wa_number) |
        (Candidate.wa_number == clean_wa) |
        (Candidate.wa_number.like(f"%{clean_wa}%"))
    ).first()
        
    if not candidate:
        raise HTTPException(status_code=404, detail="Seeker not found")
        
    app_rows = (
        db.query(
            JobVacancy.job_code,
            JobVacancy.job_title,
            JobVacancy.district_region,
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
            "job_code": a.job_code,
            "job_title": a.job_title,
            "district_region": a.district_region,
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
            ConversationState.context,
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
        ctx = v.context or {}
        visitors_list.append({
            "wa_number": v.wa_number,
            "state": v.state,
            "last_active": last_active.isoformat() if last_active else None,
            "is_registered": is_registered,
            "is_messaged": bool(ctx.get("help_messaged_at"))
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
            ConversationState.last_user_message_at,
            ConversationState.context
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

        ctx = r.context or {}
        leads.append({
            "wa_number": r.wa_number,
            "state": r.state if r.state else "idle",
            "last_active": last_active.isoformat() if last_active else None,
            "is_messaged": bool(ctx.get("help_messaged_at"))
        })

    return {
        "chart_data": {
            "temperature": buckets
        },
        "table_data": leads
    }

@router.patch("/api/users/{wa_number}/help_status")
async def api_update_help_status(
    wa_number: str,
    payload: dict,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    """Toggles the 'help_messaged_at' state in the visitor's JSON context."""
    from datetime import datetime, timezone
    from app.db.models import ConversationState

    state_obj = db.query(ConversationState).filter_by(wa_number=wa_number).first()
    if not state_obj:
        state_obj = db.query(ConversationState).filter(ConversationState.wa_number.like(f"%{wa_number.replace('+','')} ")).first()
    if not state_obj:
        raise HTTPException(status_code=404, detail="Visitor state not found")

    ctx = dict(state_obj.context or {})
    is_messaged = payload.get("is_messaged", False)

    if is_messaged:
        ctx["help_messaged_at"] = datetime.now(timezone.utc).isoformat()
    else:
        ctx.pop("help_messaged_at", None)

    state_obj.context = ctx  # reassign to trigger SQLAlchemy mutation tracking
    db.commit()

    return {"success": True, "help_messaged": is_messaged}

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
            func.max(Candidate.created_at).label("created_at"),
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
            func.max(Recruiter.created_at).label("rec_created_at"),
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
            cand_subq.c.created_at,
            cand_subq.c.app_count,
            rec_subq.c.company_name,
            rec_subq.c.rec_created_at,
            rec_subq.c.vac_count
        )
        .join(rec_subq, cand_subq.c.wa_number == rec_subq.c.wa_number)
        .order_by(cand_subq.c.created_at.desc())
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
            
        join_date = r.created_at or r.rec_created_at
        table_data.append({
            "wa_number": r.wa_number,
            "candidate_name": r.name,
            "company_name": r.company_name,
            "vacancies_posted": vacs,
            "applications_sent": apps,
            "created_at": join_date.isoformat() if join_date else None
        })
        
    return {
        "chart_data": {
            "primary_persona": buckets
        },
        "table_data": table_data
    }


# ─── Stopped Ads API ──────────────────────────────────────────────────────────

@router.get("/api/vacancies/stopped")
async def api_list_stopped_vacancies(
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    """
    Returns all vacancies where is_active=False, regardless of approval status.
    Includes recruiter info, stopped_at, application count, and stop_reason:
      - 'auto_expired' when the ad ran for ≥ 30 days before being stopped
      - 'manual'       for all other admin/system stops
    """
    from sqlalchemy import func as sqlfunc
    from app.db.models import CandidateApplication

    stopped = (
        db.query(JobVacancy)
        .filter(JobVacancy.is_active == False, JobVacancy.status != "rejected")  # noqa: E712
        .order_by(JobVacancy.stopped_at.desc().nullslast())
        .all()
    )

    results = []
    for v in stopped:
        # Count applications for this vacancy
        app_count = (
            db.query(sqlfunc.count(CandidateApplication.id))
            .filter(CandidateApplication.vacancy_id == v.id)
            .scalar()
        ) or 0

        # Determine stop reason
        stop_reason = "manual"
        if v.stopped_at and v.last_enabled_at:
            s = v.stopped_at
            l = v.last_enabled_at
            if s.tzinfo is None:
                s = s.replace(tzinfo=timezone.utc)
            if l.tzinfo is None:
                l = l.replace(tzinfo=timezone.utc)
            if (s - l).days >= 30:
                stop_reason = "auto_expired"

        results.append({
            "id": v.id,
            "job_code": v.job_code,
            "job_title": v.job_title,
            "job_category": v.job_category,
            "district_region": v.district_region,
            "exact_location": v.exact_location,
            "job_description": v.job_description or "",
            "job_mode": v.job_mode,
            "salary_range": v.salary_range,
            "experience_required": v.experience_required,
            "status": v.status,
            "is_active": v.is_active,
            "stopped_at": v.stopped_at.isoformat() if v.stopped_at else None,
            "last_enabled_at": v.last_enabled_at.isoformat() if v.last_enabled_at else None,
            "created_at": v.created_at.isoformat() if v.created_at else None,
            "stop_reason": stop_reason,
            "application_count": app_count,
            "recruiter": {
                "name": v.recruiter.company_name,
                "wa_number": v.recruiter.wa_number,
                "company": v.recruiter.company_name,
                "business_type": v.recruiter.business_type,
                "location": v.recruiter.location,
            } if v.recruiter else None,
        })

    return {"total": len(results), "results": results}


@router.post("/api/vacancies/{vacancy_id}/stop")
async def api_stop_vacancy(
    vacancy_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    """
    Manually stop an active vacancy.
    Sets is_active=False and records stopped_at timestamp.
    Only acts on vacancies that are currently active.
    """
    vacancy = db.query(JobVacancy).filter_by(id=vacancy_id).first()
    if not vacancy:
        raise HTTPException(status_code=404, detail="Vacancy not found")
    if not vacancy.is_active:
        raise HTTPException(status_code=400, detail="Vacancy is already stopped")

    vacancy.is_active = False
    vacancy.stopped_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("Admin manually stopped vacancy %s (id=%s)", vacancy.job_code, vacancy_id)
    return {"success": True, "vacancy_id": vacancy_id, "job_code": vacancy.job_code}


@router.post("/api/vacancies/{vacancy_id}/restart")
async def api_restart_vacancy(
    vacancy_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    """
    Restart a stopped vacancy.
    Sets is_active=True, resets last_enabled_at to now() (restarts the 30-day clock),
    and clears stopped_at.
    Only approved vacancies can be restarted.
    """
    vacancy = db.query(JobVacancy).filter_by(id=vacancy_id).first()
    if not vacancy:
        raise HTTPException(status_code=404, detail="Vacancy not found")
    if vacancy.is_active:
        raise HTTPException(status_code=400, detail="Vacancy is already active")
    if vacancy.status != "approved":
        raise HTTPException(
            status_code=400,
            detail="Only approved vacancies can be restarted. Please approve the vacancy first.",
        )

    now = datetime.now(timezone.utc)
    vacancy.is_active = True
    vacancy.last_enabled_at = now
    vacancy.stopped_at = None
    db.commit()
    logger.info("Admin restarted vacancy %s (id=%s)", vacancy.job_code, vacancy_id)
    return {"success": True, "vacancy_id": vacancy_id, "job_code": vacancy.job_code}


# ─── Temporary Feature: JobZon Insights (Decoupled) ──────────────────────────
# Note: This is an isolated, temporary module. If removing this tab in future,
# simply delete this block, admin/jobzon_insights.html, and the nav link in base.html.

@router.get("/jobzon-insights", response_class=HTMLResponse)
async def page_jobzon_insights(
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    return templates.TemplateResponse(
        "admin/jobzon_insights.html", {"request": request, **_sidebar_ctx(db)}
    )


@router.get("/api/jobzon-insights/data")
async def api_jobzon_insights_data(
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    """
    Reads JobZon admin contact logs from jobzon_contacts.json,
    enriches with Candidate and Recruiter details in a safe read-only manner,
    and returns aggregated statistics and itemized contact log entries.
    """
    from pathlib import Path
    import json
    from app.db.models import Candidate, Recruiter
    from app.handlers.seeker import CATEGORY_DISPLAY_NAMES

    contacts_file = Path(__file__).parent.parent / "data" / "jobzon_contacts.json"
    contacts_raw = {}
    if contacts_file.exists():
        try:
            contacts_raw = json.loads(contacts_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to read jobzon_contacts.json: %s", exc)
            contacts_raw = {}

    seeker_ids = set()
    recruiter_ids = set()
    parsed_entries = []

    for key, ts_str in contacts_raw.items():
        parts = key.split("_")
        # formats: "wa_seeker_14", "call_seeker_14", "wa_recruiter_2", etc.
        if len(parts) >= 3:
            channel = parts[0].lower()  # 'wa' or 'call'
            target_type = parts[1].lower()  # 'seeker' or 'recruiter'
            try:
                target_id = int(parts[2])
            except ValueError:
                continue

            if target_type == "seeker":
                seeker_ids.add(target_id)
            elif target_type == "recruiter":
                recruiter_ids.add(target_id)

            parsed_entries.append({
                "key": key,
                "channel": channel,
                "target_type": target_type,
                "target_id": target_id,
                "contacted_at": ts_str,
            })

    # Query Candidate info
    seekers_map = {}
    if seeker_ids:
        try:
            cands = db.query(Candidate).filter(Candidate.id.in_(seeker_ids)).all()
            for c in cands:
                cat_display = CATEGORY_DISPLAY_NAMES.get(c.category, (c.category or "").replace("_", " ").title())
                seekers_map[c.id] = {
                    "name": c.name or f"Job Seeker #{c.id}",
                    "wa_number": c.wa_number or "",
                    "phone": c.alt_phone or c.wa_number or "",
                    "district": c.district or "Unknown",
                    "category": cat_display,
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                }
        except Exception as exc:
            logger.warning("Error loading candidates for Jobzon Insights: %s", exc)

    # Query Recruiter info
    recruiters_map = {}
    if recruiter_ids:
        try:
            recs = db.query(Recruiter).filter(Recruiter.id.in_(recruiter_ids)).all()
            for r in recs:
                recruiters_map[r.id] = {
                    "name": r.company_name or f"Recruiter #{r.id}",
                    "contact_person": getattr(r, "registrant_role", "") or "",
                    "wa_number": r.wa_number or "",
                    "phone": r.business_contact or r.wa_number or "",
                    "district": r.location or "Unknown",
                    "category": (r.business_type or "").replace("_", " ").title(),
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
        except Exception as exc:
            logger.warning("Error loading recruiters for Jobzon Insights: %s", exc)

    now = datetime.now(timezone.utc)
    one_week_ago = now - timedelta(days=7)
    recent_7d_count = 0
    recent_7d_seekers = 0
    recent_7d_recruiters = 0
    recent_7d_wa = 0
    recent_7d_call = 0

    districts_count = {}
    wa_count = 0
    call_count = 0
    wa_seeker_count = 0
    wa_recruiter_count = 0
    call_seeker_count = 0
    call_recruiter_count = 0
    items = []

    for entry in parsed_entries:
        key = entry["key"]
        ch = entry["channel"]
        tt = entry["target_type"]
        tid = entry["target_id"]
        ts_str = entry["contacted_at"]

        if ch == "wa":
            wa_count += 1
            if tt == "seeker":
                wa_seeker_count += 1
            elif tt == "recruiter":
                wa_recruiter_count += 1
        elif ch == "call":
            call_count += 1
            if tt == "seeker":
                call_seeker_count += 1
            elif tt == "recruiter":
                call_recruiter_count += 1

        try:
            dt_contacted = datetime.fromisoformat(ts_str)
            if dt_contacted.tzinfo is None:
                dt_contacted = dt_contacted.replace(tzinfo=timezone.utc)
            if dt_contacted >= one_week_ago:
                recent_7d_count += 1
                if tt == "seeker":
                    recent_7d_seekers += 1
                elif tt == "recruiter":
                    recent_7d_recruiters += 1
                if ch == "wa":
                    recent_7d_wa += 1
                elif ch == "call":
                    recent_7d_call += 1
        except Exception:
            pass

        info = seekers_map.get(tid) if tt == "seeker" else recruiters_map.get(tid)
        if not info:
            info = {
                "name": f"{'Job Seeker' if tt == 'seeker' else 'Recruiter'} #{tid}",
                "wa_number": "",
                "phone": "",
                "district": "Unknown",
                "category": "N/A",
                "created_at": None,
            }

        dist = info.get("district") or "Unknown"
        districts_count[dist] = districts_count.get(dist, 0) + 1

        items.append({
            "key": key,
            "channel": ch,
            "target_type": tt,
            "target_id": tid,
            "contacted_at": ts_str,
            "name": info.get("name"),
            "wa_number": info.get("wa_number"),
            "phone": info.get("phone"),
            "district": dist,
            "category": info.get("category"),
        })

    items.sort(key=lambda x: x.get("contacted_at") or "", reverse=True)

    return {
        "stats": {
            "total_contacts": len(parsed_entries),
            "total_seekers": len(seeker_ids),
            "total_recruiters": len(recruiter_ids),
            "wa_count": wa_count,
            "call_count": call_count,
            "wa_seeker_count": wa_seeker_count,
            "wa_recruiter_count": wa_recruiter_count,
            "call_seeker_count": call_seeker_count,
            "call_recruiter_count": call_recruiter_count,
            "recent_7d_count": recent_7d_count,
            "recent_7d_seekers": recent_7d_seekers,
            "recent_7d_recruiters": recent_7d_recruiters,
            "recent_7d_wa": recent_7d_wa,
            "recent_7d_call": recent_7d_call,
            "districts": districts_count,
        },
        "contacts": items,
    }

