"""
Public REST API for jobinfo.pro website.
Handles OTP auth, vacancy listing/detail, recruiter vacancy posting,
job seeker registration, and job applications.
"""
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.base import get_db
from app.db.models import (
    ApplicationStatus, Candidate, CandidateApplication, JobVacancy,
    Recruiter, SubscriptionPlan, UserQuestion, MagicLink, CandidateResume,
    WebLoginSession
)
from app.services import otp as otp_service
from app.services.job_code import generate_job_code
from app.whatsapp.client import wa_client
from app.whatsapp.templates import (
    application_confirmation_body,
    registration_confirmation_body,
    vacancy_confirmation_body,
    vacancy_poster_preview_body,
    admin_vacancy_alert_body,
)
from app.handlers.recruiter import _generate_admin_magic_url
from app.services.milestone import dispatch_milestone_notification

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


class PinCreateRequest(BaseModel):
    role: str = "seeker"       # "seeker" | "recruiter"
    wa_number: str              # Captured from the website login form


class PinBotVerifyRequest(BaseModel):
    """Called internally by the bot dispatcher only."""
    otp_code: str               # The plain 6-digit number the user sent
    wa_number: str
    role: str = "seeker"


class CheckRecruiterRequest(BaseModel):
    wa_number: str


class CheckSeekerRequest(BaseModel):
    wa_number: str


class RegisterRecruiterRequest(BaseModel):
    wa_number: str
    otp_code: str
    company_name: str
    business_type: str
    location: str
    business_contact: str
    registrant_role: str = "other"


class RegisterSeekerRequest(BaseModel):
    wa_number: str
    otp_code: str
    name: str
    district: str
    category: str
    sub_category: str | None = None
    exact_location: str | None = None
    gender: str | None = "male"
    age: int | None = None
    alt_phone: str | None = None


class RegisterSeekerVerifiedRequest(BaseModel):
    session_token: str
    wa_number: str
    name: str
    district: str
    category: str
    sub_category: str | None = None
    exact_location: str | None = None
    gender: str | None = "male"
    age: int | None = None
    alt_phone: str | None = None


class RecruiterVacancyRequest(BaseModel):
    wa_number: str
    session_token: str   # from OTP verify step
    job_category: str
    district_region: str
    exact_location: str
    job_title: str
    job_description: str | None = None
    job_mode: str
    salary_range: str | None = None
    experience_required: str | None = None
    cv_required: bool = False


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
    resume_id: int | None = None


# ─── Simple in-memory session store for OTP-verified sessions ─────────────────
# For production use Redis or DB-backed sessions.
# Each entry: token → {"wa_number": str, "role": str, "created_at": datetime}
_sessions: dict[str, dict] = {}
_SESSION_TTL_SECONDS = 86400  # 24 hours


def _create_session(wa_number: str, role: str = "recruiter") -> str:
    import secrets
    token = secrets.token_urlsafe(32)
    _sessions[token] = {
        "wa_number": wa_number,
        "role": role,
        "created_at": datetime.now(timezone.utc),
    }
    # Lazy cleanup: purge expired sessions when new ones are created
    _cleanup_expired_sessions()
    return token


def _get_session_data(token: str) -> dict | None:
    session = _sessions.get(token)
    if not session:
        return None
    # Check TTL
    created = session.get("created_at")
    if created:
        age = (datetime.now(timezone.utc) - created).total_seconds()
        if age > _SESSION_TTL_SECONDS:
            _sessions.pop(token, None)
            return None
    return session


def _cleanup_expired_sessions() -> None:
    """Remove all sessions older than _SESSION_TTL_SECONDS."""
    now = datetime.now(timezone.utc)
    expired = [
        t for t, s in _sessions.items()
        if s.get("created_at") and (now - s["created_at"]).total_seconds() > _SESSION_TTL_SECONDS
    ]
    for t in expired:
        _sessions.pop(t, None)


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
            return {"message": "OTP sent", "within_24h": True}
        else:
            try:
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
                return {"message": "OTP sent via template", "within_24h": True}
            except Exception as e:
                logger.warning(f"Template OTP send failed for {body.wa_number} (likely outside 24h window or template unapproved): {e}")
                return {"message": "outside_24h", "within_24h": False}
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

    # Send WhatsApp confirmation to user
    try:
        if not is_new_user:
            if body.role == "recruiter":
                buttons = [
                    {"id": "btn_post_vacancy", "title": "Post Vacancy"},
                    {"id": "btn_my_vacancies", "title": "My Vacancies"},
                    {"id": "btn_my_dashboard", "title": "My Dashboard"},
                ]
            else:
                buttons = [
                    {"id": "ACTION_SUGGEST_JOBS", "title": "Suggest Jobs"},
                    {"id": "ACTION_MY_APPLICATIONS", "title": "My Applications"},
                    {"id": "btn_my_dashboard", "title": "My Dashboard"},
                ]

            await wa_client.send_buttons(
                to=body.wa_number,
                body_text=(
                    "✅ *OTP verification successful!*\n\n"
                    "You are now logged in on the website. 🎉"
                ),
                buttons=buttons,
                footer_text="Powered by JobInfo.pro",
            )
    except Exception as e:
        logger.warning(f"Could not send OTP verification confirmation to {body.wa_number}: {e}")

    return {
        "session_token": token, 
        "wa_number": body.wa_number,
        "role": body.role,
        "is_new_user": is_new_user
    }


@router.post("/auth/check-recruiter")
async def check_recruiter(body: CheckRecruiterRequest, db: Session = Depends(get_db)):
    """
    Check if a recruiter exists and whether they are within the 24h WhatsApp window.
    Special case: if the WA number matches the configured JobZon admin number,
    return is_jobzon_admin=true to signal the frontend to redirect to /admin/login.
    """
    from app.db.models import ConversationState
    state = db.query(ConversationState).filter_by(wa_number=body.wa_number).first()
    within_24h = False
    if state and state.last_user_message_at:
        last_msg_at = state.last_user_message_at
        if last_msg_at.tzinfo is None:
            last_msg_at = last_msg_at.replace(tzinfo=timezone.utc)
        time_diff = datetime.now(timezone.utc) - last_msg_at
        if time_diff.total_seconds() <= 24 * 3600:
            within_24h = True

    # ── JobZon admin detection ────────────────────────────────────────────────
    if (
        settings.jobzon_admin_wa_number
        and body.wa_number.strip() == settings.jobzon_admin_wa_number.strip()
    ):
        return {"exists": False, "is_jobzon_admin": True, "within_24h": within_24h}

    # ── Normal recruiter flow ─────────────────────────────────────────────────
    recruiter = db.query(Recruiter).filter_by(wa_number=body.wa_number).first()
    if recruiter:
        if within_24h:
            # Trigger OTP internally
            otp_request = OTPSendRequest(wa_number=body.wa_number, role="recruiter")
            await send_otp(otp_request, db)
            return {"exists": True, "is_jobzon_admin": False, "within_24h": True}
        else:
            return {"exists": True, "is_jobzon_admin": False, "within_24h": False}

    return {"exists": False, "is_jobzon_admin": False, "within_24h": within_24h}


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
        business_contact=body.business_contact,
        registrant_role=body.registrant_role or "other",
    )
    db.add(recruiter)
    db.commit()
    db.refresh(recruiter)
    
    token = _create_session(body.wa_number, "recruiter")

    # Send WhatsApp confirmation to recruiter
    try:
        buttons = [
            {"id": "btn_post_vacancy", "title": "Post Vacancy"},
            {"id": "btn_my_vacancies", "title": "My Vacancies"},
            {"id": "btn_my_dashboard", "title": "My Dashboard"},
        ]
        await wa_client.send_buttons(
            to=body.wa_number,
            body_text=(
                "✅ *Registration Successful!*\n\n"
                "Welcome to JobInfo! 🎉 Your recruiter profile has been created. You can now post vacancies and hire talent."
            ),
            buttons=buttons,
            footer_text="Powered by JobInfo.pro",
        )
    except Exception as e:
        logger.warning(f"Could not send recruiter registration confirmation to {body.wa_number}: {e}")

    return {
        "session_token": token,
        "wa_number": body.wa_number,
        "role": "recruiter",
        "is_new_user": True
    }


class RegisterRecruiterVerifiedRequest(BaseModel):
    """Used when the number was pre-verified via Reverse OTP — no OTP code needed."""
    session_token: str
    wa_number: str
    company_name: str
    business_type: str
    location: str
    business_contact: str
    registrant_role: str | None = "other"


@router.post("/auth/recruiter/register-verified")
async def register_recruiter_verified(
    body: RegisterRecruiterVerifiedRequest, db: Session = Depends(get_db)
):
    """
    Register a new recruiter whose WhatsApp number was already verified via
    the Reverse OTP flow. Validates the existing session token (no OTP needed).
    """
    # Validate the session token issued during Reverse OTP verification
    session_data = _get_session_data(body.session_token)
    if not session_data or session_data.get("wa_number") != body.wa_number:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    if db.query(Recruiter).filter_by(wa_number=body.wa_number).first():
        raise HTTPException(status_code=400, detail="Recruiter already exists")

    recruiter = Recruiter(
        wa_number=body.wa_number,
        company_name=body.company_name,
        business_type=body.business_type,
        location=body.location,
        business_contact=body.business_contact,
        registrant_role=body.registrant_role or "other",
    )
    db.add(recruiter)
    db.commit()
    db.refresh(recruiter)

    # Send WhatsApp confirmation to recruiter
    try:
        buttons = [
            {"id": "btn_post_vacancy", "title": "Post Vacancy"},
            {"id": "btn_my_vacancies", "title": "My Vacancies"},
            {"id": "btn_my_dashboard", "title": "My Dashboard"},
        ]
        await wa_client.send_buttons(
            to=body.wa_number,
            body_text=(
                "✅ *Registration Successful!*\n\n"
                "Welcome to JobInfo! 🎉 Your recruiter profile has been created. You can now post vacancies and hire talent."
            ),
            buttons=buttons,
            footer_text="Powered by JobInfo.pro",
        )
    except Exception as e:
        logger.warning(f"Could not send recruiter registration confirmation to {body.wa_number}: {e}")

    # Reuse the existing session — no new token needed
    return {
        "session_token": body.session_token,
        "wa_number": body.wa_number,
        "role": "recruiter",
        "is_new_user": True,
    }


# ─── Seeker Authentication Endpoints ──────────────────────────────────────────

@router.post("/auth/check-seeker")
async def check_seeker(body: CheckSeekerRequest, db: Session = Depends(get_db)):
    """
    Check if a job seeker exists and whether they are within the 24h WhatsApp window.
    If they exist and are within 24h, automatically trigger OTP.
    """
    from app.db.models import ConversationState
    state = db.query(ConversationState).filter_by(wa_number=body.wa_number).first()
    within_24h = False
    if state and state.last_user_message_at:
        last_msg_at = state.last_user_message_at
        if last_msg_at.tzinfo is None:
            last_msg_at = last_msg_at.replace(tzinfo=timezone.utc)
        time_diff = datetime.now(timezone.utc) - last_msg_at
        if time_diff.total_seconds() <= 24 * 3600:
            within_24h = True

    candidate = db.query(Candidate).filter_by(wa_number=body.wa_number).first()
    if candidate:
        if within_24h:
            # Trigger OTP internally
            otp_request = OTPSendRequest(wa_number=body.wa_number, role="seeker")
            await send_otp(otp_request, db)
            return {"exists": True, "within_24h": True}
        else:
            return {"exists": True, "within_24h": False}

    return {"exists": False, "within_24h": within_24h}


@router.post("/auth/seeker/register")
async def register_seeker(body: RegisterSeekerRequest, db: Session = Depends(get_db)):
    """Verify OTP and register a new job seeker, returning a session token."""
    if not otp_service.verify_otp(db, body.wa_number, body.otp_code):
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    candidate = db.query(Candidate).filter_by(wa_number=body.wa_number).first()
    if not candidate:
        candidate = Candidate(
            wa_number=body.wa_number,
            name=body.name,
            district=body.district,
            category=body.category,
            sub_category=body.sub_category,
            gender=body.gender or "male",
            age=body.age,
            exact_location=body.exact_location,
            alt_phone=body.alt_phone,
            registration_complete=not settings.subscription_enabled,
        )
        db.add(candidate)
    else:
        candidate.name = body.name
        candidate.district = body.district
        candidate.category = body.category
        if body.sub_category:
            candidate.sub_category = body.sub_category
        candidate.gender = body.gender or "male"
        if body.age:
            candidate.age = body.age
        if body.exact_location:
            candidate.exact_location = body.exact_location
        if body.alt_phone:
            candidate.alt_phone = body.alt_phone
        candidate.registration_complete = not settings.subscription_enabled

    db.commit()
    db.refresh(candidate)

    token = _create_session(body.wa_number, "seeker")

    # Send WhatsApp confirmation to seeker
    try:
        buttons = [
            {"id": "ACTION_SUGGEST_JOBS", "title": "Suggest Jobs"},
            {"id": "ACTION_MY_APPLICATIONS", "title": "My Applications"},
            {"id": "btn_my_dashboard", "title": "My Dashboard"},
        ]
        await wa_client.send_buttons(
            to=body.wa_number,
            body_text=(
                "✅ *Registration Successful!*\n\n"
                "Welcome to JobInfo! 🎉 Your candidate profile has been created. You can now explore vacancies and apply to jobs."
            ),
            buttons=buttons,
            footer_text="Powered by JobInfo.pro",
        )
    except Exception as e:
        logger.warning(f"Could not send seeker registration confirmation to {body.wa_number}: {e}")

    return {
        "session_token": token,
        "wa_number": body.wa_number,
        "role": "seeker",
        "is_new_user": True,
    }


@router.post("/auth/seeker/register-verified")
async def register_seeker_verified(
    body: RegisterSeekerVerifiedRequest, db: Session = Depends(get_db)
):
    """
    Register a new job seeker whose WhatsApp number was already verified via
    the Reverse OTP flow. Validates the existing session token (no OTP needed).
    """
    session_data = _get_session_data(body.session_token)
    if not session_data or session_data.get("wa_number") != body.wa_number:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    candidate = db.query(Candidate).filter_by(wa_number=body.wa_number).first()
    if not candidate:
        candidate = Candidate(
            wa_number=body.wa_number,
            name=body.name,
            district=body.district,
            category=body.category,
            sub_category=body.sub_category,
            gender=body.gender or "male",
            age=body.age,
            exact_location=body.exact_location,
            alt_phone=body.alt_phone,
            registration_complete=not settings.subscription_enabled,
        )
        db.add(candidate)
    else:
        candidate.name = body.name
        candidate.district = body.district
        candidate.category = body.category
        if body.sub_category:
            candidate.sub_category = body.sub_category
        candidate.gender = body.gender or "male"
        if body.age:
            candidate.age = body.age
        if body.exact_location:
            candidate.exact_location = body.exact_location
        if body.alt_phone:
            candidate.alt_phone = body.alt_phone
        candidate.registration_complete = not settings.subscription_enabled

    db.commit()
    db.refresh(candidate)

    # Send WhatsApp confirmation to seeker
    try:
        buttons = [
            {"id": "ACTION_SUGGEST_JOBS", "title": "Suggest Jobs"},
            {"id": "ACTION_MY_APPLICATIONS", "title": "My Applications"},
            {"id": "btn_my_dashboard", "title": "My Dashboard"},
        ]
        await wa_client.send_buttons(
            to=body.wa_number,
            body_text=(
                "✅ *Registration Successful!*\n\n"
                "Welcome to JobInfo! 🎉 Your candidate profile has been created. You can now explore vacancies and apply to jobs."
            ),
            buttons=buttons,
            footer_text="Powered by JobInfo.pro",
        )
    except Exception as e:
        logger.warning(f"Could not send seeker registration confirmation to {body.wa_number}: {e}")

    return {
        "session_token": body.session_token,
        "wa_number": body.wa_number,
        "role": "seeker",
        "is_new_user": True,
    }


# ─── Magic Links ──────────────────────────────────────────────────────────────

@router.post("/auth/magic/generate")
def generate_magic_link(body: MagicTokenGenerateRequest, db: Session = Depends(get_db)):
    """Internal use: generates a short-lived magic token for a user."""
    import secrets
    token = secrets.token_urlsafe(32)
    # 90 days expiry
    expires = datetime.now(timezone.utc) + timedelta(days=90)

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


# ─── Reverse OTP — Number-aware Web Login ─────────────────────────────────────
_PIN_TTL_MINUTES = 5  # 5-minute window matches traditional OTP expectations


@router.post("/auth/pin/create")
def create_pin_session(body: PinCreateRequest, db: Session = Depends(get_db)):
    """
    Website calls this when the user enters their number and the 24h window is closed.
    Stores the wa_number alongside the generated OTP so the bot can match
    by sender identity alone — no message prefix required.
    Self-cleans expired rows and invalidates any previous pending session
    for the same number.
    """
    import secrets, random, string as _string

    now = datetime.now(timezone.utc)

    # ── Lazy cleanup: delete all expired rows on every create ─────────────────
    db.query(WebLoginSession).filter(WebLoginSession.expires_at < now).delete(
        synchronize_session="fetch"
    )

    # ── Invalidate any previous pending session for this wa_number ────────────
    db.query(WebLoginSession).filter(
        WebLoginSession.wa_number == body.wa_number,
        WebLoginSession.status == "pending",
    ).update({"status": "superseded"}, synchronize_session="fetch")

    db.commit()

    session_id = secrets.token_urlsafe(32)
    otp = "".join(random.choices(_string.digits, k=6))
    expires = now + timedelta(minutes=_PIN_TTL_MINUTES)

    ws = WebLoginSession(
        session_id=session_id,
        pin=otp,
        wa_number=body.wa_number,   # ← stored at creation time
        role=body.role,
        expires_at=expires,
    )
    db.add(ws)
    db.commit()

    return {"session_id": session_id, "otp": otp, "expires_at": expires}


@router.get("/auth/pin/status/{session_id}")
def poll_pin_status(session_id: str, db: Session = Depends(get_db)):
    """
    Frontend polls this every 5 s to check if the bot has verified the OTP.
    Returns { status: 'pending' | 'verified' | 'expired' } and on verified,
    includes session_token, wa_number, role, is_new_user.
    """
    ws = db.query(WebLoginSession).filter_by(session_id=session_id).first()
    if not ws:
        raise HTTPException(status_code=404, detail="Session not found")

    now = datetime.now(timezone.utc)
    expires_at = ws.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if now > expires_at:
        return {"status": "expired"}

    if ws.status == "verified":
        return {
            "status": "verified",
            "session_token": ws.session_token,
            "wa_number": ws.wa_number,
            "role": ws.role,
            "is_new_user": ws.is_new_user,
        }

    return {"status": "pending"}


@router.post("/auth/pin/bot-verify")
def bot_verify_pin(body: PinBotVerifyRequest, db: Session = Depends(get_db)):
    """
    Internal endpoint — called by the bot dispatcher when it receives a
    plain 6-digit message from a number that has a pending web login session.
    Looks up the session by wa_number (not by OTP), then validates the code.
    """
    now = datetime.now(timezone.utc)

    # ── Find the pending session for this wa_number ───────────────────────────
    ws = (
        db.query(WebLoginSession)
        .filter(
            WebLoginSession.wa_number == body.wa_number,
            WebLoginSession.status == "pending",
            WebLoginSession.expires_at > now,
        )
        .first()
    )

    if not ws:
        return {"success": False, "reason": "no_pending_session"}

    # ── Validate the OTP ──────────────────────────────────────────────────────
    if ws.pin != body.otp_code:
        return {"success": False, "reason": "wrong_otp"}

    # ── Determine is_new_user (role was fixed at session-creation time) ───────
    role = ws.role
    is_new = False
    if role == "seeker":
        candidate = db.query(Candidate).filter_by(wa_number=body.wa_number).first()
        if not candidate:
            is_new = True
    elif role == "recruiter":
        recruiter = db.query(Recruiter).filter_by(wa_number=body.wa_number).first()
        if not recruiter:
            is_new = True

    session_token = _create_session(body.wa_number, role)

    ws.status = "verified"
    ws.session_token = session_token
    ws.is_new_user = is_new
    db.commit()

    return {"success": True, "is_new_user": is_new, "role": role}


# ─── Vacancies (public) ───────────────────────────────────────────────────────

@router.get("/vacancies")
def list_vacancies(
    page: int = 1,
    page_size: int = 20,
    district_region: str | None = None,
    job_title: str | None = None,
    location: str | None = None,
    title: str | None = None,
    keyword: str | None = None,
    q: str | None = None,
    db: Session = Depends(get_db),
):
    query = db.query(JobVacancy).filter_by(status="approved", is_active=True)

    loc_filter = (location or district_region or "").strip()
    if loc_filter:
        query = query.filter(
            or_(
                JobVacancy.district_region.ilike(f"%{loc_filter}%"),
                JobVacancy.exact_location.ilike(f"%{loc_filter}%"),
            )
        )

    title_filter = (title or job_title or keyword or q or "").strip()
    if title_filter:
        query = query.outerjoin(Recruiter, JobVacancy.recruiter_id == Recruiter.id).filter(
            or_(
                JobVacancy.job_title.ilike(f"%{title_filter}%"),
                JobVacancy.job_description.ilike(f"%{title_filter}%"),
                JobVacancy.job_category.ilike(f"%{title_filter}%"),
                JobVacancy.job_code.ilike(f"%{title_filter}%"),
                Recruiter.company_name.ilike(f"%{title_filter}%"),
            )
        )

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
                "company_name": v.recruiter.company_name if v.recruiter else "",
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
    q = (query or "").strip()
    if not q or len(q) < 1:
        return {"results": []}

    district_rows = (
        db.query(JobVacancy.district_region)
        .filter(JobVacancy.status == "approved", JobVacancy.is_active == True)
        .filter(JobVacancy.district_region.ilike(f"%{q}%"))
        .distinct()
        .all()
    )
    exact_rows = (
        db.query(JobVacancy.exact_location)
        .filter(JobVacancy.status == "approved", JobVacancy.is_active == True)
        .filter(JobVacancy.exact_location.ilike(f"%{q}%"))
        .distinct()
        .all()
    )

    seen = set()
    prefix_matches = []
    other_matches = []

    for row in district_rows + exact_rows:
        val = row[0]
        if not val or not val.strip():
            continue
        cleaned = val.strip()
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        if key.startswith(q.lower()):
            prefix_matches.append(cleaned)
        else:
            other_matches.append(cleaned)

    results = (prefix_matches + other_matches)[:10]
    return {"results": results}


@router.get("/vacancies/titles/suggest")
def suggest_titles(
    query: str,
    db: Session = Depends(get_db),
):
    """Suggests job titles based on current approved and active vacancies."""
    q = (query or "").strip()
    if not q or len(q) < 1:
        return {"results": []}

    title_rows = (
        db.query(JobVacancy.job_title)
        .filter(JobVacancy.status == "approved", JobVacancy.is_active == True)
        .filter(JobVacancy.job_title.ilike(f"%{q}%"))
        .distinct()
        .all()
    )

    seen = set()
    prefix_matches = []
    other_matches = []

    for row in title_rows:
        val = row[0]
        if not val or not val.strip():
            continue
        cleaned = val.strip()
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        if key.startswith(q.lower()):
            prefix_matches.append(cleaned)
        else:
            other_matches.append(cleaned)

    results = (prefix_matches + other_matches)[:10]
    return {"results": results}


@router.get("/vacancies/{vacancy_id}")
def get_vacancy(vacancy_id: int, db: Session = Depends(get_db)):
    vacancy = db.query(JobVacancy).filter_by(id=vacancy_id, status="approved", is_active=True).first()
    if not vacancy:
        raise HTTPException(status_code=404, detail="Vacancy not found")
    return {
        "id": vacancy.id,
        "job_code": vacancy.job_code,
        "job_title": vacancy.job_title,
        "company_name": vacancy.recruiter.company_name if vacancy.recruiter else "",
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
        district_region=body.district_region,
        exact_location=body.exact_location,
        job_title=body.job_title,
        job_description=body.job_description,
        job_mode=body.job_mode,
        salary_range=body.salary_range,
        experience_required=body.experience_required,
        cv_required=body.cv_required,
    )
    db.add(vacancy)
    db.commit()
    db.refresh(vacancy)

    # WhatsApp confirmation to recruiter
    try:
        await wa_client.send_text(to=body.wa_number, body=vacancy_confirmation_body(vacancy))
    except Exception as e:
        logger.warning("Recruiter WhatsApp confirmation failed: %s", e)

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
            try:
                await wa_client.send_text(
                    to=settings.admin_wa_number,
                    body=admin_vacancy_alert_body(vacancy, recruiter),
                )
            except Exception as e2:
                logger.warning("Admin fallback text alert failed: %s", e2)

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

    from app.services.ad_lifecycle import ensure_ad_active
    if not ensure_ad_active(vacancy, db):
        raise HTTPException(status_code=403, detail="Position no longer available")

    existing = db.query(CandidateApplication).filter_by(
        candidate_id=candidate.id, vacancy_id=vacancy.id
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Already applied")

    application = CandidateApplication(candidate_id=candidate.id, vacancy_id=vacancy.id, resume_id=body.resume_id)
    db.add(application)
    candidate.applications_used = (candidate.applications_used or 0) + 1
    db.commit()

    # Smart milestone notification (non-blocking; 24h window checked inside)
    app_count = db.query(CandidateApplication).filter_by(vacancy_id=vacancy.id).count()
    dispatch_milestone_notification(vacancy, app_count, db)

    await wa_client.send_text(
        to=body.wa_number,
        body=application_confirmation_body(candidate, vacancy),
    )
    return {"applied": True, "vacancy": vacancy.title}


# ─── Recruiter Dashboard ──────────────────────────────────────────────────────

class RecruiterDashboardRequest(BaseModel):
    wa_number: str
    session_token: str

class ToggleAdRequest(BaseModel):
    wa_number: str
    session_token: str
    vacancy_id: int
    action: str   # "stop" or "rerun"


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
            "job_category": v.job_category,
            "job_title": v.job_title,
            "company_name": recruiter.company_name or "",
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
            "is_active": v.is_active,
            "last_enabled_at": v.last_enabled_at.isoformat() if v.last_enabled_at else None,
            "stopped_at": v.stopped_at.isoformat() if v.stopped_at else None,
            "application_count": app_count,
        })

    # Summary counts
    counts = {"total": len(vacancy_list), "rejected": 0}
    for v in vacancy_list:
        if v["status"] == "rejected":
            counts["rejected"] += 1

    total_applications = sum(v["application_count"] for v in vacancy_list)

    return {
        "recruiter": {
            "company_name": recruiter.company_name or "",
            "business_type": recruiter.business_type or "",
            "location": recruiter.location or "",
            "business_contact": recruiter.business_contact or "",
            "registrant_role": getattr(recruiter, "registrant_role", "other") or "other",
            "wa_number": recruiter.wa_number,
        },
        "summary": {**counts, "total_applications": total_applications},
        "vacancies": vacancy_list,
    }


# ─── Edit Recruiter Profile ───────────────────────────────────────────────────

class EditRecruiterProfileRequest(BaseModel):
    wa_number: str
    session_token: str
    registrant_role: str
    location: str
    business_contact: str


@router.post("/recruiters/profile/edit")
def edit_recruiter_profile(
    body: EditRecruiterProfileRequest,
    db: Session = Depends(get_db),
):
    """
    Allows an authenticated recruiter to update their registrant_role,
    location, and business_contact. WhatsApp number and company name
    are intentionally locked and cannot be changed here.
    """
    _require_session(body.wa_number, body.session_token)

    recruiter = db.query(Recruiter).filter_by(wa_number=body.wa_number).first()
    if not recruiter:
        raise HTTPException(status_code=404, detail="Recruiter not found.")

    # Validate
    allowed_roles = {"founder", "hr", "manager", "employee", "other"}
    role = (body.registrant_role or "").strip().lower()
    if role not in allowed_roles:
        raise HTTPException(status_code=422, detail="Invalid registrant role.")

    allowed_locations = {"Kerala", "Karnataka", "GCC", "Other"}
    location = (body.location or "").strip()
    if location not in allowed_locations:
        raise HTTPException(status_code=422, detail="Invalid location value.")

    contact = (body.business_contact or "").strip()
    if not contact:
        raise HTTPException(status_code=422, detail="Business contact number is required.")

    recruiter.registrant_role = role
    recruiter.location = location
    recruiter.business_contact = contact

    db.commit()
    db.refresh(recruiter)

    return {
        "message": "Profile updated successfully.",
        "registrant_role": recruiter.registrant_role,
        "location": recruiter.location,
        "business_contact": recruiter.business_contact,
    }


@router.post("/recruiters/vacancy/toggle-ad")
def toggle_vacancy_ad(
    body: ToggleAdRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Start or stop an ad."""
    _require_session(body.wa_number, body.session_token)

    recruiter = db.query(Recruiter).filter_by(wa_number=body.wa_number).first()
    if not recruiter:
        raise HTTPException(status_code=404, detail="Recruiter not found")

    vacancy = db.query(JobVacancy).filter_by(id=body.vacancy_id, recruiter_id=recruiter.id).first()
    if not vacancy:
        raise HTTPException(status_code=404, detail="Vacancy not found")

    if body.action == "stop":
        if vacancy.status != "approved":
            raise HTTPException(status_code=403, detail="Only live approved ads can be stopped")
        if not vacancy.is_active:
            raise HTTPException(status_code=409, detail="Ad is already stopped")
    elif body.action == "rerun":
        if vacancy.status != "approved":
            raise HTTPException(status_code=403, detail="This vacancy is not approved")
        if vacancy.is_active:
            raise HTTPException(status_code=409, detail="Ad is already running")
    else:
        raise HTTPException(status_code=400, detail="Invalid action")

    from app.services.ad_lifecycle import toggle_ad
    toggle_ad(vacancy, db, body.action, background_tasks=background_tasks)

    return {
        "is_active": vacancy.is_active,
        "last_enabled_at": vacancy.last_enabled_at.isoformat() if vacancy.last_enabled_at else None,
        "stopped_at": vacancy.stopped_at.isoformat() if vacancy.stopped_at else None,
        "message": f"Ad {'stopped' if body.action == 'stop' else 'running'}",
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
    status: str   # "applied" | "shortlisted"


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
            "status": vacancy.status,
            "is_active": vacancy.is_active,
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
    Update the status of a candidate application (shortlist / reset to applied).
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
    district_region: str
    exact_location: str
    job_title: str
    job_description: str | None = None
    job_mode: str
    salary_range: str | None = None
    experience_required: str | None = None
    cv_required: bool | None = None  # Optional — preserves existing value if omitted


@router.post("/recruiters/vacancy/edit")
def edit_rejected_vacancy(
    body: EditVacancyRequest,
    db: Session = Depends(get_db),
):
    """
    Allows a recruiter to edit and resubmit a vacancy that is not yet approved.
    - Works on vacancies with status: pending, rejected, or revoked
    - Approved vacancies are fully locked (returns 403)
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

    if vacancy.status == "approved":
        raise HTTPException(
            status_code=403,
            detail="Approved vacancies cannot be edited."
        )

    # Validate required fields
    if not body.job_title or not body.job_title.strip():
        raise HTTPException(status_code=422, detail="Job title is required")
    if not body.exact_location or not body.exact_location.strip():
        raise HTTPException(status_code=422, detail="Exact location is required")
    if not body.district_region or not body.district_region.strip():
        raise HTTPException(status_code=422, detail="District / Region is required")
    if not body.job_category or not body.job_category.strip():
        raise HTTPException(status_code=422, detail="Job category is required")
    if not body.job_mode or not body.job_mode.strip():
        raise HTTPException(status_code=422, detail="Job mode is required")

    # Apply all field edits to the ORM object (in-memory; not yet committed)
    vacancy.job_category        = body.job_category.strip()
    vacancy.job_title           = body.job_title.strip()
    vacancy.district_region     = body.district_region.strip()
    vacancy.exact_location      = body.exact_location.strip()
    vacancy.job_description     = (body.job_description or "").strip() or None
    vacancy.job_mode            = body.job_mode.strip()
    vacancy.salary_range        = (body.salary_range or "").strip() or None
    vacancy.experience_required = (body.experience_required or "").strip() or None
    if body.cv_required is not None:          # Preserve existing value if field was omitted
        vacancy.cv_required = body.cv_required

    # ── Concurrency safeguard ────────────────────────────────────────────────
    # Expire only the 'status' column so SQLAlchemy issues a fresh SELECT on
    # next access — catching any admin approval that occurred while this
    # request was in-flight, without nested savepoints or row-locking.
    db.expire(vacancy, ["status"])
    if vacancy.status == "approved":
        raise HTTPException(
            status_code=409,
            detail="This vacancy was approved while you were editing. Your changes were not saved."
        )

    # All clear — stamp edit metadata and commit
    vacancy.status           = "pending"
    vacancy.rejection_reason = None
    vacancy.is_edited        = True
    vacancy.edited_at        = datetime.now(timezone.utc)
    vacancy.approved_at      = None

    db.commit()
    db.refresh(vacancy)

    return {
        "success": True,
        "vacancy_id": vacancy.id,
        "job_code": vacancy.job_code,
        "status": vacancy.status,
        "message": "Vacancy updated and queued for review. You will be notified via WhatsApp once reviewed.",
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
        
    resume_count = db.query(CandidateResume).filter_by(candidate_id=candidate.id).count()
    
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
        "has_cv": resume_count > 0 or bool(candidate.cv_path),
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


@router.get("/candidates/cvs")
def list_candidate_cvs(
    wa_number: str,
    session_token: str,
    db: Session = Depends(get_db)
):
    _require_session(wa_number, session_token, expected_role="seeker")
    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
        
    resumes = db.query(CandidateResume).filter_by(candidate_id=candidate.id).order_by(CandidateResume.uploaded_at.desc()).all()
    return {
        "cvs": [
            {
                "id": r.id,
                "filename": r.media_id.split("/")[-1].split("\\")[-1],
                "uploaded_at": r.uploaded_at.isoformat() if r.uploaded_at else None,
                "is_default": r.is_default
            } for r in resumes
        ]
    }


@router.post("/candidates/cvs")
async def upload_candidate_cv(
    wa_number: str = Form(...),
    session_token: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    _require_session(wa_number, session_token, expected_role="seeker")
    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
        
    resume_count = db.query(CandidateResume).filter_by(candidate_id=candidate.id).count()
    from app.db.models import MAX_CANDIDATE_RESUMES
    if resume_count >= MAX_CANDIDATE_RESUMES:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_CANDIDATE_RESUMES} CVs allowed.")
        
    from app.services.storage import save_cv_from_upload_file
    cv_path = await save_cv_from_upload_file(wa_number, file)
    if not cv_path:
        raise HTTPException(status_code=400, detail="Invalid file format. Only PDF and Document formats allowed.")
        
    is_default = (resume_count == 0)
    resume = CandidateResume(
        candidate_id=candidate.id,
        media_id=cv_path,
        is_default=is_default
    )
    db.add(resume)
    if is_default and not candidate.cv_path:
        candidate.cv_path = cv_path
    db.commit()
    return {"message": "CV uploaded successfully"}


@router.delete("/candidates/cvs/{resume_id}")
def delete_candidate_cv(
    resume_id: int,
    wa_number: str,
    session_token: str,
    db: Session = Depends(get_db)
):
    _require_session(wa_number, session_token, expected_role="seeker")
    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
        
    resume = db.query(CandidateResume).filter_by(id=resume_id, candidate_id=candidate.id).first()
    if not resume:
        raise HTTPException(status_code=404, detail="CV not found")
        
    # Check if used in any application
    used_in_app = db.query(CandidateApplication).filter_by(resume_id=resume.id).first()
    if used_in_app:
        raise HTTPException(status_code=400, detail="Cannot delete this CV because it has been used for a job application.")
        
    import os
    if os.path.exists(resume.media_id):
        try:
            os.remove(resume.media_id)
        except Exception:
            pass
            
    db.delete(resume)
    
    # Check if we deleted the candidate's core default cv_path
    if candidate.cv_path == resume.media_id:
        candidate.cv_path = None
        # Promote another to default if available
        next_resume = db.query(CandidateResume).filter_by(candidate_id=candidate.id).first()
        if next_resume:
            candidate.cv_path = next_resume.media_id
            
    db.commit()
    return {"message": "CV deleted successfully"}


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
            "company": vac.recruiter.company_name if vac.recruiter else "",
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
