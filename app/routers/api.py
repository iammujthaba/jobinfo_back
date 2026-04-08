"""
Public REST API for jobinfo.pro website.
Handles OTP auth, vacancy listing/detail, recruiter vacancy posting,
job seeker registration, and job applications.
"""
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.base import get_db
from app.db.models import (
    ApplicationStatus, Candidate, CandidateApplication, JobVacancy,
    Recruiter, SubscriptionPlan, UserQuestion, MagicLink
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
from app.handlers.recruiter import _generate_admin_magic_url

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/api", tags=["api"])


# ─── Pydantic schemas ─────────────────────────────────────────────────────────

class UserQuestionRequest(BaseModel):
    name: str | None = None
    wa_number: str | None = None
    question: str
    source: str | None = None
    website: str | None = None


class OTPSendRequest(BaseModel):
    wa_number: str
    role: str = "recruiter"


class OTPVerifyRequest(BaseModel):
    wa_number: str
    otp_code: str
    role: str = "recruiter"


class MagicTokenGenerateRequest(BaseModel):
    wa_number: str
    role: str = "seeker"


class MagicTokenVerifyRequest(BaseModel):
    token: str


class CheckRecruiterRequest(BaseModel):
    wa_number: str


class RegisterRecruiterRequest(BaseModel):
    wa_number: str
    otp_code: str
    company_name: str
    business_type: str
    location: str
    business_contact: str


class RecruiterVacancyRequest(BaseModel):
    wa_number: str
    session_token: str   # from OTP verify step
    job_category: str
    company_name: str | None = None
    district_region: str
    exact_location: str
    job_title: str
    job_description: str | None = None
    job_mode: str
    salary_range: str | None = None
    experience_required: str | None = None


class CandidateRegisterRequest(BaseModel):
    wa_number: str
    session_token: str
    name: str
    district: str | None = None
    exact_location: str | None = None
    category: str | None = None
    sub_category: str | None = None
    age: int | None = None
    alt_phone: str | None = None
    gender: str | None = None
    # CV is uploaded as a separate multipart request (see /api/candidates/upload-cv)


class CandidateUpdateRequest(BaseModel):
    wa_number: str
    session_token: str
    name: str | None = None
    district: str | None = None
    exact_location: str | None = None
    category: str | None = None
    sub_category: str | None = None
    age: int | None = None
    alt_phone: str | None = None
    gender: str | None = None


class CandidateApplyRequest(BaseModel):
    wa_number: str
    session_token: str
    vacancy_id: int


# ─── Simple in-memory session store for OTP-verified sessions ─────────────────
# For production use Redis or DB-backed sessions.
_sessions: dict[str, dict] = {}  # token → {"wa_number": wa_number, "role": role}


def _create_session(wa_number: str, role: str = "recruiter") -> str:
    import secrets
    token = secrets.token_urlsafe(32)
    _sessions[token] = {"wa_number": wa_number, "role": role}
    return token


def _get_session_data(token: str) -> dict | None:
    return _sessions.get(token)


def _require_session(wa_number: str, session_token: str, expected_role: str = "recruiter"):
    session_data = _get_session_data(session_token)
    if not session_data or session_data["wa_number"] != wa_number:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if session_data["role"] != expected_role:
        raise HTTPException(status_code=403, detail="Insufficient permissions")


# ─── OTP endpoints ────────────────────────────────────────────────────────────

@router.post("/otp/send")
async def send_otp(body: OTPSendRequest, db: Session = Depends(get_db)):
    """Generate OTP and send it via WhatsApp."""
    import httpx
    try:
        if body.role == "seeker":
            candidate = db.query(Candidate).filter_by(wa_number=body.wa_number).first()
            if not candidate:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="not_registered")

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
                language_code="en",
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
    """Verify OTP and return a session token. Supports role-based routing."""
    if not otp_service.verify_otp(db, body.wa_number, body.otp_code):
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")
    
    token = _create_session(body.wa_number, body.role)
    
    is_new_user = False
    if body.role == "seeker":
        candidate = db.query(Candidate).filter_by(wa_number=body.wa_number).first()
        if not candidate:
            is_new_user = True

    return {
        "session_token": token, 
        "wa_number": body.wa_number,
        "role": body.role,
        "is_new_user": is_new_user
    }


@router.post("/auth/check-recruiter")
async def check_recruiter(body: CheckRecruiterRequest, db: Session = Depends(get_db)):
    """Check if a recruiter exists. If yes, trigger OTP."""
    recruiter = db.query(Recruiter).filter_by(wa_number=body.wa_number).first()
    if recruiter:
        # Trigger OTP internally
        otp_request = OTPSendRequest(wa_number=body.wa_number, role="recruiter")
        await send_otp(otp_request, db)
        return {"exists": True}
    return {"exists": False}


@router.post("/auth/recruiter/register")
async def register_recruiter(body: RegisterRecruiterRequest, db: Session = Depends(get_db)):
    """Verify OTP and register a new recruiter, returning a session token."""
    if not otp_service.verify_otp(db, body.wa_number, body.otp_code):
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")
        
    recruiter = db.query(Recruiter).filter_by(wa_number=body.wa_number).first()
    if recruiter:
        raise HTTPException(status_code=400, detail="Recruiter already exists")
        
    recruiter = Recruiter(
        wa_number=body.wa_number,
        company_name=body.company_name,
        business_type=body.business_type,
        location=body.location,
        business_contact=body.business_contact
    )
    db.add(recruiter)
    db.commit()
    db.refresh(recruiter)
    
    token = _create_session(body.wa_number, "recruiter")
    return {
        "session_token": token,
        "wa_number": body.wa_number,
        "role": "recruiter",
        "is_new_user": True
    }


# ─── Magic Links ──────────────────────────────────────────────────────────────

@router.post("/auth/magic/generate")
def generate_magic_link(body: MagicTokenGenerateRequest, db: Session = Depends(get_db)):
    """Internal use: generates a short-lived magic token for a user."""
    import secrets
    token = secrets.token_urlsafe(32)
    # 365 days expiry for persistent usage
    expires = datetime.now(timezone.utc) + timedelta(days=365)

    magic = MagicLink(
        token=token,
        wa_number=body.wa_number,
        role=body.role,
        expires_at=expires,
        is_used=False
    )
    db.add(magic)
    db.commit()

    return {"token": token, "expires_at": expires}


@router.post("/auth/magic/verify")
def verify_magic_link(body: MagicTokenVerifyRequest, db: Session = Depends(get_db)):
    """Public use: verifies a magic token and issues a session."""
    magic = db.query(MagicLink).filter_by(token=body.token).first()
    if not magic:
        raise HTTPException(status_code=400, detail="Invalid or expired link")
    
    # Ensure timezone awareness for comparison
    now = datetime.now(timezone.utc)
    expires_at = magic.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
        
    if now > expires_at:
        raise HTTPException(status_code=400, detail="Magic link has expired")

    # Removed marking the token as used so it remains endlessly reusable.

    # Create session
    session_token = _create_session(magic.wa_number, magic.role)
    
    # Check if new user
    is_new_user = False
    if magic.role == "seeker":
        candidate = db.query(Candidate).filter_by(wa_number=magic.wa_number).first()
        if not candidate:
            is_new_user = True

    return {
        "session_token": session_token,
        "wa_number": magic.wa_number,
        "role": magic.role,
        "is_new_user": is_new_user
    }


# ─── Vacancies (public) ───────────────────────────────────────────────────────

@router.get("/vacancies")
def list_vacancies(
    page: int = 1,
    page_size: int = 20,
    district_region: str | None = None,
    job_title: str | None = None,
    db: Session = Depends(get_db),
):
    query = db.query(JobVacancy).filter_by(status="approved")
    if district_region:
        query = query.filter(JobVacancy.district_region.ilike(f"%{district_region}%"))
    if job_title:
        query = query.filter(JobVacancy.job_title.ilike(f"%{job_title}%"))
    total = query.count()
    vacancies = query.order_by(JobVacancy.approved_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {
        "total": total,
        "page": page,
        "results": [
            {
                "id": v.id,
                "job_code": v.job_code,
                "job_category": v.job_category,
                "job_title": v.job_title,
                "company_name": v.company_name,
                "district_region": v.district_region,
                "exact_location": v.exact_location,
                "salary_range": v.salary_range,
                "experience_required": v.experience_required,
                "job_mode": v.job_mode,
                "job_description": v.job_description,
                "apply_link": f"https://wa.me/{settings.business_wa_number}?text=Apply%20{v.job_code}",
            }
            for v in vacancies
        ],
    }


@router.get("/vacancies/locations/suggest")
def suggest_locations(
    query: str,
    db: Session = Depends(get_db),
):
    """Suggests locations based on current approved and active vacancies."""
    if not query or len(query.strip()) < 1:
        return {"results": []}
    locations = (
        db.query(JobVacancy.district_region)
        .filter(JobVacancy.status == "approved")
        .filter(JobVacancy.district_region.ilike(f"{query.strip()}%"))
        .distinct()
        .limit(10)
        .all()
    )
    return {"results": [loc[0] for loc in locations]}


@router.get("/vacancies/titles/suggest")
def suggest_titles(
    query: str,
    db: Session = Depends(get_db),
):
    """Suggests job titles based on current approved and active vacancies."""
    if not query or len(query.strip()) < 1:
        return {"results": []}
    titles = (
        db.query(JobVacancy.job_title)
        .filter(JobVacancy.status == "approved")
        .filter(JobVacancy.job_title.ilike(f"{query.strip()}%"))
        .distinct()
        .limit(10)
        .all()
    )
    return {"results": [t[0] for t in titles]}


@router.get("/vacancies/{vacancy_id}")
def get_vacancy(vacancy_id: int, db: Session = Depends(get_db)):
    vacancy = db.query(JobVacancy).filter_by(id=vacancy_id, status="approved").first()
    if not vacancy:
        raise HTTPException(status_code=404, detail="Vacancy not found")
    return {
        "id": vacancy.id,
        "job_code": vacancy.job_code,
        "job_title": vacancy.job_title,
        "company_name": vacancy.company_name,
        "district_region": vacancy.district_region,
        "exact_location": vacancy.exact_location,
        "job_description": vacancy.job_description,
        "salary_range": vacancy.salary_range,
        "experience_required": vacancy.experience_required,
        "job_mode": vacancy.job_mode,
        "job_category": vacancy.job_category,
        "apply_link": f"https://wa.me/{settings.business_wa_number}?text=Apply%20{vacancy.job_code}",
    }



@router.post("/questions")
def submit_question(body: UserQuestionRequest, db: Session = Depends(get_db)):
    """Submit a user question. Uses an optional website field as a honeypot."""
    if body.website:
        logger.info("Spam bot detected via honeypot field during question submission")
        return {"status": "success", "message": "Question submitted successfully"}
    
    if not body.question or not body.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")
        
    q = UserQuestion(
        name=body.name.strip() if body.name else None,
        wa_number=body.wa_number.strip() if body.wa_number else None,
        question=body.question.strip(),
        source=body.source.strip() if body.source else None
    )
    db.add(q)
    db.commit()
    return {"status": "success", "message": "Question submitted successfully"}


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
        raise HTTPException(status_code=404, detail="Recruiter not found. Please register first.")

    job_code = generate_job_code(db)
    vacancy = JobVacancy(
        job_code=job_code,
        recruiter_id=recruiter.id,
        job_category=body.job_category,
        company_name=body.company_name or recruiter.company_name,
        district_region=body.district_region,
        exact_location=body.exact_location,
        job_title=body.job_title,
        job_description=body.job_description,
        job_mode=body.job_mode,
        salary_range=body.salary_range,
        experience_required=body.experience_required,
    )
    db.add(vacancy)
    db.commit()
    db.refresh(vacancy)

    # WhatsApp confirmation to recruiter
    await wa_client.send_text(to=body.wa_number, body=vacancy_confirmation_body(vacancy))

    # Alert admin with interactive CTA magic link
    if settings.admin_wa_number:
        try:
            admin_url = _generate_admin_magic_url(db)
            await wa_client.send_interactive_cta_url(
                to=settings.admin_wa_number,
                body_text=admin_vacancy_alert_body(vacancy, recruiter),
                button_display_text="Review Vacancy",
                button_url=admin_url,
            )
        except Exception as e:
            logger.warning("Admin CTA alert failed (web post), falling back to text: %s", e)
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
            district=body.district,
            exact_location=body.exact_location,
            category=body.category,
            sub_category=body.sub_category,
            age=body.age,
            alt_phone=body.alt_phone,
            gender=body.gender,
            registration_complete=not settings.subscription_enabled,
        )
        db.add(candidate)
    else:
        candidate.name = body.name
        if body.district is not None:
            candidate.district = body.district
        if body.exact_location is not None:
            candidate.exact_location = body.exact_location
        candidate.category = body.category
        candidate.sub_category = body.sub_category
        candidate.age = body.age
        candidate.alt_phone = body.alt_phone
        if body.gender is not None:
            candidate.gender = body.gender
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

    vacancy = db.query(JobVacancy).filter_by(id=body.vacancy_id, status="approved").first()
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
    total_shortlisted = 0
    for v in vacancies:
        app_count = db.query(CandidateApplication).filter_by(vacancy_id=v.id).count()
        shortlisted_count = db.query(CandidateApplication).filter_by(vacancy_id=v.id, status=ApplicationStatus.shortlisted).count()
        total_shortlisted += shortlisted_count
        vacancy_list.append({
            "id": v.id,
            "job_code": v.job_code,
            "job_category": v.job_category,
            "job_title": v.job_title,
            "company_name": v.company_name or "",
            "district_region": v.district_region,
            "exact_location": v.exact_location,
            "job_mode": v.job_mode,
            "job_description": v.job_description or "",
            "salary_range": v.salary_range,
            "experience_required": v.experience_required,
            "status": v.status,
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
            "company_name": recruiter.company_name or "",
            "business_type": recruiter.business_type or "",
            "location": recruiter.location or "",
            "business_contact": recruiter.business_contact or "",
            "wa_number": recruiter.wa_number,
        },
        "summary": {**counts, "total_applications": total_applications, "total_shortlisted": total_shortlisted},
        "vacancies": vacancy_list,
    }


# ─── Application Management (Recruiter) ──────────────────────────────────────

class VacancyApplicationsRequest(BaseModel):
    wa_number: str
    session_token: str
    vacancy_id: int


class AllApplicationsRequest(BaseModel):
    wa_number: str
    session_token: str


class UpdateApplicationStatusRequest(BaseModel):
    wa_number: str
    session_token: str
    application_id: int
    status: str   # "applied" | "shortlisted" | "rejected"


@router.post("/recruiters/all-applications")
def list_all_applications(
    body: AllApplicationsRequest,
    db: Session = Depends(get_db),
):
    """
    Returns all applications across all vacancies owned by the recruiter.
    Includes job_code and job_title for frontend filtering.
    """
    _require_session(body.wa_number, body.session_token)

    recruiter = db.query(Recruiter).filter_by(wa_number=body.wa_number).first()
    if not recruiter:
        raise HTTPException(status_code=404, detail="Recruiter not found")

    applications = (
        db.query(CandidateApplication)
        .join(JobVacancy)
        .filter(JobVacancy.recruiter_id == recruiter.id)
        .order_by(CandidateApplication.applied_at.desc())
        .all()
    )

    results = []
    for app in applications:
        c = app.candidate
        v = app.vacancy
        results.append({
            "application_id": app.id,
            "status": app.status.value,
            "applied_at": app.applied_at.isoformat() if app.applied_at else None,
            "job_code": v.job_code,
            "job_title": v.job_title,
            "candidate": {
                "id": c.id,
                "name": c.name,
                "district": c.district or "",
                "exact_location": c.exact_location or "",
                "category": c.category or "",
                "sub_category": c.sub_category or "",
                "age": c.age,
                "alt_phone": c.alt_phone or "",
                "gender": c.gender or "",
                "wa_number": c.wa_number,
                "has_cv": bool(c.cv_path),
                "cv_path": c.cv_path or None,
            },
        })

    return {
        "total": len(results),
        "applications": results,
    }


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
                "district": c.district or "",
                "exact_location": c.exact_location or "",
                "category": c.category or "",
                "sub_category": c.sub_category or "",
                "age": c.age,
                "alt_phone": c.alt_phone or "",
                "gender": c.gender or "",
                "wa_number": c.wa_number,
                "has_cv": bool(c.cv_path),
                "cv_path": c.cv_path or None,
            },
        })

    return {
        "vacancy": {
            "id": vacancy.id,
            "job_code": vacancy.job_code,
            "job_title": vacancy.job_title,
            "district_region": vacancy.district_region,
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
    writer.writerow(["#", "Name", "Gender", "District", "Exact Location", "Category", "Role", "Age", "Alt Phone", "WhatsApp", "Status", "Applied On"])
    for i, app in enumerate(applications, 1):
        c = app.candidate
        writer.writerow([
            i,
            c.name,
            c.gender or "",
            c.district or "",
            c.exact_location or "",
            c.category or "",
            c.sub_category or "",
            c.age or "",
            c.alt_phone or "",
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


@router.post("/recruiters/all-applications/export-csv")
def export_all_applications_csv(
    body: AllApplicationsRequest,
    db: Session = Depends(get_db),
):
    """
    Export all applications across all vacancies for a recruiter as a CSV file download.
    Includes job code, job title, candidate name, location, skills, WA number, status.
    """
    import csv, io
    from fastapi.responses import StreamingResponse

    _require_session(body.wa_number, body.session_token)

    recruiter = db.query(Recruiter).filter_by(wa_number=body.wa_number).first()
    if not recruiter:
        raise HTTPException(status_code=404, detail="Recruiter not found")

    applications = (
        db.query(CandidateApplication)
        .join(JobVacancy)
        .filter(JobVacancy.recruiter_id == recruiter.id)
        .order_by(CandidateApplication.applied_at.desc())
        .all()
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["#", "Job Code", "Job Title", "Name", "Gender", "District", "Exact Location", "Category", "Role", "Age", "WhatsApp", "Status", "Applied On"])
    for i, app in enumerate(applications, 1):
        c = app.candidate
        v = app.vacancy
        writer.writerow([
            i,
            v.job_code,
            v.job_title,
            c.name,
            c.gender or "",
            c.district or "",
            c.exact_location or "",
            c.category or "",
            c.sub_category or "",
            c.age or "",
            f"+{c.wa_number}",
            app.status.value,
            app.applied_at.strftime("%d %b %Y") if app.applied_at else "",
        ])

    output.seek(0)
    filename = "all_applications.csv"
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
    job_category: str
    company_name: str | None = None
    district_region: str
    exact_location: str
    job_title: str
    job_description: str | None = None
    job_mode: str
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

    if vacancy.status != "rejected":
        raise HTTPException(
            status_code=400,
            detail=f"Only rejected vacancies can be edited. Current status: {vacancy.status}"
        )

    # Validate required fields
    if not body.job_title:
        raise HTTPException(status_code=422, detail="Title is required")
    if not body.exact_location:
        raise HTTPException(status_code=422, detail="Location is required")

    # Apply edits
    vacancy.job_category = body.job_category
    vacancy.job_title = body.job_title
    vacancy.company_name = (body.company_name or "").strip() or None
    vacancy.district_region = body.district_region
    vacancy.exact_location = body.exact_location
    vacancy.job_description = (body.job_description or "").strip() or None
    vacancy.job_mode = body.job_mode
    vacancy.salary_range = (body.salary_range or "").strip() or None
    vacancy.experience_required = (body.experience_required or "").strip() or None

    # Reset to pending for re-review, clear rejection reason, mark as edited
    vacancy.status = "pending"
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
        "status": vacancy.status,
        "message": "Vacancy resubmitted for review. You will be notified via WhatsApp once reviewed.",
    }


# ─── Candidate actions ────────────────────────────────────────────────────────

@router.get("/candidates/me")
def get_candidate_profile(
    wa_number: str,
    session_token: str,
    db: Session = Depends(get_db)
):
    _require_session(wa_number, session_token, expected_role="seeker")
    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    
    return {
        "id": candidate.id,
        "name": candidate.name,
        "wa_number": candidate.wa_number,
        "district": candidate.district,
        "exact_location": candidate.exact_location,
        "category": candidate.category,
        "sub_category": candidate.sub_category,
        "age": candidate.age,
        "alt_phone": candidate.alt_phone,
        "gender": candidate.gender,
        "cv_path": candidate.cv_path,
        "registration_complete": candidate.registration_complete,
        "created_at": candidate.created_at
    }


@router.put("/candidates/me")
def update_candidate_profile(
    body: CandidateUpdateRequest,
    db: Session = Depends(get_db)
):
    _require_session(body.wa_number, body.session_token, expected_role="seeker")
    candidate = db.query(Candidate).filter_by(wa_number=body.wa_number).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
        
    if body.name is not None:
        candidate.name = body.name
    if body.district is not None:
        candidate.district = body.district
    if body.exact_location is not None:
        candidate.exact_location = body.exact_location
        
    if body.category is not None:
        candidate.category = body.category
    if body.sub_category is not None:
        candidate.sub_category = body.sub_category
    if body.age is not None:
        candidate.age = body.age
    if body.alt_phone is not None:
        candidate.alt_phone = body.alt_phone
    if body.gender is not None:
        candidate.gender = body.gender
        
    db.commit()
    db.refresh(candidate)
    return {"message": "Profile updated successfully"}


@router.get("/candidates/applications")
def get_candidate_applications(
    wa_number: str,
    session_token: str,
    days: int | None = None,
    status: str | None = None,
    db: Session = Depends(get_db)
):
    _require_session(wa_number, session_token, expected_role="seeker")
    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
        
    query = (
        db.query(CandidateApplication, JobVacancy)
        .join(JobVacancy, CandidateApplication.vacancy_id == JobVacancy.id)
        .filter(CandidateApplication.candidate_id == candidate.id)
    )

    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        query = query.filter(CandidateApplication.applied_at >= cutoff)
        
    if status is not None and status != "":
        # Handle cases where status might not match exactly, or just exact match
        query = query.filter(CandidateApplication.status == status)

    apps = query.order_by(CandidateApplication.applied_at.desc()).all()
    
    results = []
    for app, vac in apps:
        results.append({
            "application_id": app.id,
            "status": app.status.value,
            "applied_at": app.applied_at,
            "job_title": vac.job_title,
            "company": vac.company_name,
            "location": vac.district_region,
            "job_code": vac.job_code
        })
        
    return {"applications": results}


@router.get("/candidates/analytics")
def get_candidate_analytics(
    wa_number: str,
    session_token: str,
    db: Session = Depends(get_db)
):
    _require_session(wa_number, session_token, expected_role="seeker")
    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    try:
        from sqlalchemy import func
        # Group by JobVacancy.job_category
        stats = (
            db.query(JobVacancy.job_category, func.count(CandidateApplication.id).label("count"))
            .join(CandidateApplication, JobVacancy.id == CandidateApplication.vacancy_id)
            .filter(CandidateApplication.candidate_id == candidate.id)
            .group_by(JobVacancy.job_category)
            .all()
        )

        total_apps = sum(count for _, count in stats)
        
        analytics = []
        for title, count in stats:
            pct = (count / total_apps * 100) if total_apps > 0 else 0
            analytics.append({
                "category": title or "Uncategorized",
                "count": count,
                "percentage": round(pct, 1)
            })

        # Sort by descending count
        analytics.sort(key=lambda x: x["count"], reverse=True)

        return {
            "total_applications": total_apps,
            "focus_areas": analytics
        }
    except Exception as e:
        logger.error(f"Analytics Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))



# ─── Public Apply Redirect ────────────────────────────────────────────────────

@router.get("/apply/{job_code}", response_class=RedirectResponse)
async def apply_redirect(job_code: str):
    """
    Public redirect bridge: instantly forwards the browser to the WhatsApp
    deep-link for a given job_code.  Used in template URL buttons so Meta's
    restrictions on custom domains don't block the flow.

    Example: GET /api/apply/JB001
      → 302 → https://wa.me/917025962176?text=Apply%20JB001
    """
    wa_url = f"https://wa.me/917025962176?text=Apply%20{job_code}"
    return RedirectResponse(url=wa_url, status_code=302)
