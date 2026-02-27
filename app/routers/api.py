"""
Public REST API for jobinfo.club website.
Handles OTP auth, vacancy listing/detail, recruiter vacancy posting,
job seeker registration, and job applications.
"""
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.base import get_db
from app.db.models import (
    ApplicationStatus, Candidate, CandidateApplication, JobVacancy,
    Recruiter, SubscriptionPlan, VacancyStatus
)
from app.services import otp as otp_service
from app.services.job_code import generate_job_code
from app.whatsapp.client import wa_client
from app.whatsapp.templates import (
    application_confirmation_body,
    registration_confirmation_body,
    vacancy_confirmation_body,
    admin_vacancy_alert_body,
)

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/api", tags=["api"])


# ─── Pydantic schemas ─────────────────────────────────────────────────────────

class OTPSendRequest(BaseModel):
    wa_number: str


class OTPVerifyRequest(BaseModel):
    wa_number: str
    otp_code: str


class RecruiterVacancyRequest(BaseModel):
    wa_number: str
    session_token: str   # from OTP verify step
    title: str
    company: str | None = None
    location: str
    description: str | None = None
    salary_range: str | None = None
    experience_required: str | None = None


class CandidateRegisterRequest(BaseModel):
    wa_number: str
    session_token: str
    name: str
    location: str | None = None
    skills: str | None = None
    # CV is uploaded as a separate multipart request (see /api/candidates/upload-cv)


class CandidateApplyRequest(BaseModel):
    wa_number: str
    session_token: str
    vacancy_id: int


# ─── Simple in-memory session store for OTP-verified sessions ─────────────────
# For production use Redis or DB-backed sessions.
_sessions: dict[str, str] = {}  # token → wa_number


def _create_session(wa_number: str) -> str:
    import secrets
    token = secrets.token_urlsafe(32)
    _sessions[token] = wa_number
    return token


def _get_wa_number_from_token(token: str) -> str | None:
    return _sessions.get(token)


def _require_session(wa_number: str, session_token: str):
    resolved = _get_wa_number_from_token(session_token)
    if resolved != wa_number:
        raise HTTPException(status_code=401, detail="Invalid or expired session")


# ─── OTP endpoints ────────────────────────────────────────────────────────────

@router.post("/otp/send")
async def send_otp(body: OTPSendRequest, db: Session = Depends(get_db)):
    """Generate OTP and send it via WhatsApp."""
    import httpx
    try:
        otp_code = otp_service.create_otp(db, body.wa_number)
        
        # Check 24-hour window
        from app.db.models import ConversationState
        state = db.query(ConversationState).filter_by(wa_number=body.wa_number).first()
        
        send_text = False
        if state and state.last_user_message_at:
            # Ensure last_user_message_at is offset-aware
            last_msg_at = state.last_user_message_at
            if last_msg_at.tzinfo is None:
                last_msg_at = last_msg_at.replace(tzinfo=timezone.utc)
            time_diff = datetime.now(timezone.utc) - last_msg_at
            if time_diff.total_seconds() <= 24 * 3600:
                send_text = True
                
        if send_text:
            await wa_client.send_text(
                to=body.wa_number,
                body=(
                    f"🔐 *JobInfo OTP*\n\n"
                    f"Your verification code is: *{otp_code}*\n\n"
                    f"Valid for 5 minutes. Do not share this code with anyone.\n_JobInfo_"
                ),
            )
        else:
            await wa_client.send_template(
                to=body.wa_number,
                template_name="jobinfo_otp_auth",
                components=[
                    {
                        "type": "body",
                        "parameters": [{"type": "text", "text": otp_code}]
                    },
                    {
                        "type": "button",
                        "sub_type": "url",
                        "index": "0",
                        "parameters": [{"type": "text", "text": otp_code}]
                    }
                ]
            )
            
        return {"message": "OTP sent"}
    except httpx.HTTPStatusError as e:
        logger.error(f"WhatsApp API Error: {e.response.text}")
        raise HTTPException(
            status_code=400,
            detail=f"WhatsApp API Error: {e.response.json().get('error', {}).get('message', 'Unknown Error')}"
        )


@router.post("/otp/verify")
async def verify_otp(body: OTPVerifyRequest, db: Session = Depends(get_db)):
    """Verify OTP and return a session token."""
    if not otp_service.verify_otp(db, body.wa_number, body.otp_code):
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")
    token = _create_session(body.wa_number)
    return {"session_token": token, "wa_number": body.wa_number}


# ─── Vacancies (public) ───────────────────────────────────────────────────────

@router.get("/vacancies")
def list_vacancies(
    page: int = 1,
    page_size: int = 20,
    location: str | None = None,
    title: str | None = None,
    db: Session = Depends(get_db),
):
    query = db.query(JobVacancy).filter_by(status=VacancyStatus.approved)
    if location:
        query = query.filter(JobVacancy.location.ilike(f"%{location}%"))
    if title:
        query = query.filter(JobVacancy.title.ilike(f"%{title}%"))
    total = query.count()
    vacancies = query.order_by(JobVacancy.approved_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {
        "total": total,
        "page": page,
        "results": [
            {
                "id": v.id,
                "job_code": v.job_code,
                "title": v.title,
                "company": v.company,
                "location": v.location,
                "salary_range": v.salary_range,
                "experience_required": v.experience_required,
                "description": v.description,
                "apply_link": f"https://wa.me/{settings.business_wa_number}?text=Apply%20{v.job_code}",
            }
            for v in vacancies
        ],
    }


@router.get("/vacancies/{vacancy_id}")
def get_vacancy(vacancy_id: int, db: Session = Depends(get_db)):
    vacancy = db.query(JobVacancy).filter_by(id=vacancy_id, status=VacancyStatus.approved).first()
    if not vacancy:
        raise HTTPException(status_code=404, detail="Vacancy not found")
    return {
        "id": vacancy.id,
        "job_code": vacancy.job_code,
        "title": vacancy.title,
        "company": vacancy.company,
        "location": vacancy.location,
        "description": vacancy.description,
        "salary_range": vacancy.salary_range,
        "experience_required": vacancy.experience_required,
        "apply_link": f"https://wa.me/{settings.business_wa_number}?text=Apply%20{vacancy.job_code}",
    }


# ─── Recruiter actions ────────────────────────────────────────────────────────

@router.post("/recruiters/vacancy")
async def post_vacancy_web(
    body: RecruiterVacancyRequest,
    db: Session = Depends(get_db),
):
    _require_session(body.wa_number, body.session_token)

    # Ensure recruiter exists
    recruiter = db.query(Recruiter).filter_by(wa_number=body.wa_number).first()
    if not recruiter:
        recruiter = Recruiter(
            wa_number=body.wa_number,
            name=body.wa_number,  # update after registration
            company=body.company,
        )
        db.add(recruiter)
        db.commit()
        db.refresh(recruiter)

    job_code = generate_job_code(db)
    vacancy = JobVacancy(
        job_code=job_code,
        recruiter_id=recruiter.id,
        title=body.title,
        company=body.company or recruiter.company,
        location=body.location,
        description=body.description,
        salary_range=body.salary_range,
        experience_required=body.experience_required,
    )
    db.add(vacancy)
    db.commit()
    db.refresh(vacancy)

    # WhatsApp confirmation
    await wa_client.send_text(to=body.wa_number, body=vacancy_confirmation_body(vacancy))
    # Alert admin
    if settings.admin_wa_number:
        await wa_client.send_text(
            to=settings.admin_wa_number,
            body=admin_vacancy_alert_body(vacancy, recruiter),
        )

    return {"job_code": vacancy.job_code, "status": "pending_review"}


# ─── Candidate actions ────────────────────────────────────────────────────────

@router.post("/candidates/register")
async def register_candidate_web(
    body: CandidateRegisterRequest,
    db: Session = Depends(get_db),
):
    _require_session(body.wa_number, body.session_token)

    candidate = db.query(Candidate).filter_by(wa_number=body.wa_number).first()
    if not candidate:
        candidate = Candidate(
            wa_number=body.wa_number,
            name=body.name,
            location=body.location,
            skills=body.skills,
            registration_complete=not settings.subscription_enabled,
        )
        db.add(candidate)
    else:
        candidate.name = body.name
        candidate.location = body.location
        candidate.skills = body.skills
    db.commit()

    await wa_client.send_text(
        to=body.wa_number,
        body=registration_confirmation_body(candidate.name, "candidate"),
    )
    return {"registered": True, "subscription_required": settings.subscription_enabled}


@router.post("/candidates/apply")
async def apply_for_vacancy_web(
    body: CandidateApplyRequest,
    db: Session = Depends(get_db),
):
    _require_session(body.wa_number, body.session_token)

    candidate = db.query(Candidate).filter_by(wa_number=body.wa_number).first()
    if not candidate or not candidate.registration_complete:
        raise HTTPException(status_code=403, detail="Please complete registration first")

    vacancy = db.query(JobVacancy).filter_by(id=body.vacancy_id, status=VacancyStatus.approved).first()
    if not vacancy:
        raise HTTPException(status_code=404, detail="Vacancy not found")

    existing = db.query(CandidateApplication).filter_by(
        candidate_id=candidate.id, vacancy_id=vacancy.id
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Already applied")

    application = CandidateApplication(candidate_id=candidate.id, vacancy_id=vacancy.id)
    db.add(application)
    candidate.applications_used = (candidate.applications_used or 0) + 1
    db.commit()

    await wa_client.send_text(
        to=body.wa_number,
        body=application_confirmation_body(candidate, vacancy),
    )
    return {"applied": True, "vacancy": vacancy.title}


# ─── Recruiter Dashboard ──────────────────────────────────────────────────────

class RecruiterDashboardRequest(BaseModel):
    wa_number: str
    session_token: str


@router.post("/recruiters/dashboard")
def recruiter_dashboard(
    body: RecruiterDashboardRequest,
    db: Session = Depends(get_db),
):
    """
    Returns the authenticated recruiter's profile, vacancy stats, and full vacancy list
    including per-vacancy application counts.  Requires a valid OTP session token.
    """
    _require_session(body.wa_number, body.session_token)

    recruiter = db.query(Recruiter).filter_by(wa_number=body.wa_number).first()
    if not recruiter:
        raise HTTPException(status_code=404, detail="Recruiter not found. Please register first.")

    vacancies = (
        db.query(JobVacancy)
        .filter_by(recruiter_id=recruiter.id)
        .order_by(JobVacancy.created_at.desc())
        .all()
    )

    # Build full vacancy list with application counts
    vacancy_list = []
    for v in vacancies:
        app_count = db.query(CandidateApplication).filter_by(vacancy_id=v.id).count()
        vacancy_list.append({
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
            "approved_at": v.approved_at.isoformat() if v.approved_at else None,
            "application_count": app_count,
        })

    # Summary counts
    counts = {"total": len(vacancy_list), "approved": 0, "pending": 0, "rejected": 0}
    for v in vacancy_list:
        counts[v["status"]] = counts.get(v["status"], 0) + 1

    total_applications = sum(v["application_count"] for v in vacancy_list)

    return {
        "recruiter": {
            "name": recruiter.name,
            "company": recruiter.company or "",
            "location": recruiter.location or "",
            "email": recruiter.email or "",
            "wa_number": recruiter.wa_number,
        },
        "summary": {**counts, "total_applications": total_applications},
        "vacancies": vacancy_list,
    }


# ─── Application Management (Recruiter) ──────────────────────────────────────

class VacancyApplicationsRequest(BaseModel):
    wa_number: str
    session_token: str
    vacancy_id: int


class UpdateApplicationStatusRequest(BaseModel):
    wa_number: str
    session_token: str
    application_id: int
    status: str   # "applied" | "shortlisted" | "rejected"


@router.post("/recruiters/vacancy-applications")
def list_vacancy_applications(
    body: VacancyApplicationsRequest,
    db: Session = Depends(get_db),
):
    """
    Returns all applications for a specific vacancy the recruiter owns.
    Includes full candidate profile: name, location, skills, WA number, CV availability.
    Requires a valid OTP session token.
    """
    _require_session(body.wa_number, body.session_token)

    # Verify recruiter owns this vacancy
    recruiter = db.query(Recruiter).filter_by(wa_number=body.wa_number).first()
    if not recruiter:
        raise HTTPException(status_code=404, detail="Recruiter not found")

    vacancy = db.query(JobVacancy).filter_by(
        id=body.vacancy_id, recruiter_id=recruiter.id
    ).first()
    if not vacancy:
        raise HTTPException(status_code=404, detail="Vacancy not found or access denied")

    applications = (
        db.query(CandidateApplication)
        .filter_by(vacancy_id=vacancy.id)
        .order_by(CandidateApplication.applied_at.desc())
        .all()
    )

    results = []
    for app in applications:
        c = app.candidate
        results.append({
            "application_id": app.id,
            "status": app.status.value,
            "applied_at": app.applied_at.isoformat() if app.applied_at else None,
            "candidate": {
                "id": c.id,
                "name": c.name,
                "location": c.location or "",
                "skills": c.skills or "",
                "wa_number": c.wa_number,
                "has_cv": bool(c.cv_path),
                "cv_path": c.cv_path or None,
            },
        })

    return {
        "vacancy": {
            "id": vacancy.id,
            "job_code": vacancy.job_code,
            "title": vacancy.title,
            "location": vacancy.location,
        },
        "total": len(results),
        "applications": results,
    }


@router.post("/recruiters/application/update-status")
def update_application_status(
    body: UpdateApplicationStatusRequest,
    db: Session = Depends(get_db),
):
    """
    Update the status of a candidate application (shortlist / reject / reset to applied).
    Verifies the recruiter owns the vacancy the application belongs to.
    """
    _require_session(body.wa_number, body.session_token)

    recruiter = db.query(Recruiter).filter_by(wa_number=body.wa_number).first()
    if not recruiter:
        raise HTTPException(status_code=404, detail="Recruiter not found")

    app = db.query(CandidateApplication).filter_by(id=body.application_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    # Verify recruiter owns this vacancy
    if app.vacancy.recruiter_id != recruiter.id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Validate new status value
    valid = {s.value for s in ApplicationStatus}
    if body.status not in valid:
        raise HTTPException(status_code=400, detail=f"Invalid status. Use one of: {valid}")

    app.status = ApplicationStatus(body.status)
    db.commit()
    return {"success": True, "application_id": app.id, "status": app.status.value}


@router.post("/recruiters/vacancy-applications/export-csv")
def export_applications_csv(
    body: VacancyApplicationsRequest,
    db: Session = Depends(get_db),
):
    """
    Export applications for a vacancy as a CSV file download.
    Includes candidate name, location, skills, WA number, status.
    """
    import csv, io
    from fastapi.responses import StreamingResponse

    _require_session(body.wa_number, body.session_token)

    recruiter = db.query(Recruiter).filter_by(wa_number=body.wa_number).first()
    if not recruiter:
        raise HTTPException(status_code=404, detail="Recruiter not found")

    vacancy = db.query(JobVacancy).filter_by(
        id=body.vacancy_id, recruiter_id=recruiter.id
    ).first()
    if not vacancy:
        raise HTTPException(status_code=404, detail="Vacancy not found or access denied")

    applications = (
        db.query(CandidateApplication)
        .filter_by(vacancy_id=vacancy.id)
        .order_by(CandidateApplication.applied_at.desc())
        .all()
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["#", "Name", "Location", "Skills", "WhatsApp", "Status", "Applied On"])
    for i, app in enumerate(applications, 1):
        c = app.candidate
        writer.writerow([
            i,
            c.name,
            c.location or "",
            c.skills or "",
            f"+{c.wa_number}",
            app.status.value,
            app.applied_at.strftime("%d %b %Y") if app.applied_at else "",
        ])

    output.seek(0)
    filename = f"applications_{vacancy.job_code}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ─── Edit Rejected Vacancy ────────────────────────────────────────────────────

class EditVacancyRequest(BaseModel):
    wa_number: str
    session_token: str
    vacancy_id: int
    title: str
    company: str | None = None
    location: str
    description: str | None = None
    salary_range: str | None = None
    experience_required: str | None = None


@router.post("/recruiters/vacancy/edit")
def edit_rejected_vacancy(
    body: EditVacancyRequest,
    db: Session = Depends(get_db),
):
    """
    Allows a recruiter to edit a rejected vacancy and resubmit it for review.
    - Only works on vacancies with status=rejected
    - Resets status to pending and marks is_edited=True
    - Admin will see an 'Edited' badge to distinguish re-submissions
    """
    _require_session(body.wa_number, body.session_token)

    recruiter = db.query(Recruiter).filter_by(wa_number=body.wa_number).first()
    if not recruiter:
        raise HTTPException(status_code=404, detail="Recruiter not found")

    vacancy = db.query(JobVacancy).filter_by(
        id=body.vacancy_id, recruiter_id=recruiter.id
    ).first()
    if not vacancy:
        raise HTTPException(status_code=404, detail="Vacancy not found or access denied")

    if vacancy.status != VacancyStatus.rejected:
        raise HTTPException(
            status_code=400,
            detail=f"Only rejected vacancies can be edited. Current status: {vacancy.status.value}"
        )

    # Validate required fields
    body.title = body.title.strip()
    body.location = body.location.strip()
    if not body.title:
        raise HTTPException(status_code=422, detail="Title is required")
    if not body.location:
        raise HTTPException(status_code=422, detail="Location is required")

    # Apply edits
    vacancy.title = body.title
    vacancy.company = (body.company or "").strip() or None
    vacancy.location = body.location
    vacancy.description = (body.description or "").strip() or None
    vacancy.salary_range = (body.salary_range or "").strip() or None
    vacancy.experience_required = (body.experience_required or "").strip() or None

    # Reset to pending for re-review, clear rejection reason, mark as edited
    vacancy.status = VacancyStatus.pending
    vacancy.rejection_reason = None
    vacancy.is_edited = True
    vacancy.edited_at = datetime.now(timezone.utc)
    vacancy.approved_at = None

    db.commit()
    db.refresh(vacancy)

    return {
        "success": True,
        "vacancy_id": vacancy.id,
        "job_code": vacancy.job_code,
        "status": vacancy.status.value,
        "message": "Vacancy resubmitted for review. You will be notified via WhatsApp once reviewed.",
    }
