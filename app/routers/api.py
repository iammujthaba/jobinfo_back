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
    Candidate, CandidateApplication, JobVacancy,
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
    otp_code = otp_service.create_otp(db, body.wa_number)
    await wa_client.send_text(
        to=body.wa_number,
        body=(
            f"🔐 *JobInfo OTP*\n\n"
            f"Your verification code is: *{otp_code}*\n\n"
            f"Valid for 5 minutes. Do not share this code with anyone.\n_JobInfo_"
        ),
    )
    return {"message": "OTP sent"}


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
