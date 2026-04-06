"""
Recruiter conversation handler (state machine).
Manages the full recruiter lifecycle:
  - Start: check if number is registered
  - New: send registration WhatsApp Flow
  - Returning: send welcome template with Post Vacancy / My Vacancies
  - Handle flow completions
  - Handle button presses
"""
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import ConversationState, JobVacancy, Recruiter, CandidateApplication
from app.services.job_code import generate_job_code
from app.whatsapp.client import wa_client
from app.whatsapp.templates import (
    admin_vacancy_alert_body,
    recruiter_welcome_components,
    vacancy_confirmation_body,
    registration_confirmation_body,
    vacancy_approved_body,
    vacancy_rejected_body,
    job_alert_text_body,
)

logger = logging.getLogger(__name__)
settings = get_settings()

# Template name for the utility template (create and approve on Meta)

# Template name for the utility template (create and approve on Meta)
TEMPLATE_RECRUITER_WELCOME = "jobinfo_welcome_recruiter_v2"


def _get_or_create_state(wa_number: str, db: Session) -> ConversationState:
    state = db.query(ConversationState).filter_by(wa_number=wa_number).first()
    if not state:
        state = ConversationState(wa_number=wa_number, state="idle", context={})
        db.add(state)
        db.commit()
        db.refresh(state)
    return state


def _generate_magic_token(recruiter: Recruiter, db: Session) -> str:
    import secrets
    from datetime import datetime, timedelta, timezone
    from app.db.models import MagicLink
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=365)
    magic = MagicLink(
        token=token,
        wa_number=recruiter.wa_number,
        role="recruiter",
        expires_at=expires,
    )
    db.add(magic)
    db.commit()
    return token

def _generate_magic_dashboard_url(recruiter: Recruiter, db: Session) -> str:
    token = _generate_magic_token(recruiter, db)
    return f"https://jobinfo.pro/recruiter.html?magic_token={token}"


def _generate_admin_magic_url(db: Session) -> str:
    """Generate a one-time magic login URL for the admin panel (role='admin')."""
    import secrets
    from app.db.models import MagicLink
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(minutes=30)  # 30-min window for admin
    magic = MagicLink(
        token=token,
        wa_number=settings.admin_wa_number,
        role="admin",
        expires_at=expires,
    )
    db.add(magic)
    db.commit()
    return f"https://jobinfo.pro/admin.html?magic_token={token}"


async def start(wa_number: str, db: Session) -> None:
    """
    Entry point: called when a recruiter sends 'My Vacancy' (or taps the menu button).
    Checks if they are registered and routes accordingly.
    """
    recruiter = db.query(Recruiter).filter_by(wa_number=wa_number).first()

    if recruiter:
        # Returning recruiter – send utility template with buttons
        token = _generate_magic_token(recruiter, db)
        await wa_client.send_template(
            to=wa_number,
            template_name=TEMPLATE_RECRUITER_WELCOME,
            components=recruiter_welcome_components(recruiter, token),
            language_code="en"
        )
        _set_state(wa_number, "recruiter_idle", {}, db)
        return
    else:
        # New recruiter – launch registration Flow
        await wa_client.send_flow(
            to=wa_number,
            flow_id=settings.FLOW_ID_RECRUITER_REGISTER,
            flow_cta="Register as Recruiter",
            body_text=(
                "*⏳Complete your registration!*\n\n"
                "*JobInfo* – Post Jobs via WhatsApp, Kerala's top WhatsApp autmated placement network!\n\n"
                "Hire the best talent instantly. To start posting your job vacancies, please complete a quick 1-minute registration.\n\n"
                "_Tap the button below to begin 👇_"
            ),
        )
        _set_state(wa_number, "recruiter_registering", {}, db)
        return


async def handle_registration_flow_completion(
    wa_number: str, flow_data: dict, db: Session
) -> None:
    """
    Called when Meta sends the WhatsApp Flow completion event for recruiter registration.
    flow_data keys (from Flow): company_name, business_type, location, business_contact
    """
    recruiter = Recruiter(
        wa_number=wa_number,
        company_name=flow_data.get("company_name", ""),
        business_type=flow_data.get("business_type", ""),
        location=flow_data.get("location", ""),
        business_contact=flow_data.get("business_contact", ""),
    )
    db.add(recruiter)
    db.commit()
    db.refresh(recruiter)

    # Confirmation message with CTA button
    await wa_client.send_buttons(
        to=wa_number,
        body_text=registration_confirmation_body(recruiter.company_name, "recruiter"),
        buttons=[
            {"id": "btn_post_vacancy", "title": "Post Vacancy"}
        ]
    )
    _set_state(wa_number, "recruiter_idle", {}, db)
    return


def _location_options_for() -> list[dict]:
    """Return a master list of all available regions across all states."""
    return [
        # Kerala
        {"id": "trivandrum", "title": "Trivandrum"},
        {"id": "kollam", "title": "Kollam"},
        {"id": "pathanamthitta", "title": "Pathanamthitta"},
        {"id": "alappuzha", "title": "Alappuzha"},
        {"id": "kottayam", "title": "Kottayam"},
        {"id": "idukki", "title": "Idukki"},
        {"id": "ernakulam", "title": "Ernakulam"},
        {"id": "thrissur", "title": "Thrissur"},
        {"id": "palakkad", "title": "Palakkad"},
        {"id": "malappuram", "title": "Malappuram"},
        {"id": "kozhikode", "title": "Kozhikode"},
        {"id": "wayanad", "title": "Wayanad"},
        {"id": "kannur", "title": "Kannur"},
        {"id": "kasaragod", "title": "Kasaragod"},
        # Karnataka
        {"id": "bangalore", "title": "Bangalore"},
        {"id": "mangalore", "title": "Mangalore"},
        {"id": "mysore", "title": "Mysore"},
        {"id": "hubli", "title": "Hubli"},
        # GCC
        {"id": "uae", "title": "Dubai"},
        {"id": "saudi_arabia", "title": "Riyadh"},
        {"id": "qatar", "title": "Doha"},
        {"id": "oman", "title": "Muscat"},
        {"id": "kuwait", "title": "Kuwait"},
        {"id": "bahrain", "title": "Manama"},
        # Other
        {"id": "other_location", "title": "Other Location"}
    ]


async def handle_post_vacancy_flow_completion(
    wa_number: str, flow_data: dict, db: Session
) -> None:
    """
    Called when the Post Vacancy WhatsApp Flow completes.
    Saves the vacancy, notifies admin, sends confirmation to recruiter.
    flow_data keys: title, company, location, description, salary_range,
                    experience_required, contact_info
    """
    recruiter = db.query(Recruiter).filter_by(wa_number=wa_number).first()
    if not recruiter:
        logger.error("Post vacancy flow completed but recruiter %s not found", wa_number)
        return

    job_code = generate_job_code(db)

    # The WhatsApp Flow dropdown sends the *id* string ("yes" or "no")
    cv_required_raw = flow_data.get("cv_required", "no")
    cv_required: bool = str(cv_required_raw).strip().lower() == "yes"

    vacancy = JobVacancy(
        job_code=job_code,
        recruiter_id=recruiter.id,
        job_category=flow_data.get("job_category", ""),
        company_name=flow_data.get("company_name") or recruiter.company_name,
        district_region=flow_data.get("district_region", ""),
        exact_location=flow_data.get("exact_location", ""),
        job_title=flow_data.get("job_title", ""),
        job_description=flow_data.get("job_description", ""),
        job_mode=flow_data.get("job_mode", ""),
        experience_required=flow_data.get("experience_required", ""),
        salary_range=flow_data.get("salary_range", ""),
        cv_required=cv_required,
    )
    db.add(vacancy)
    db.commit()
    db.refresh(vacancy)

    magic_url = _generate_magic_dashboard_url(recruiter, db)

    # Notify recruiter
    await wa_client.send_interactive_cta_url(
        to=wa_number,
        body_text=vacancy_confirmation_body(vacancy),
        button_display_text="View Dashboard",
        button_url=magic_url
    )

    # Notify admin (personal WA number) — interactive CTA with magic link
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
            logger.warning("Admin CTA alert failed, falling back to text: %s", e)
            await wa_client.send_text(
                to=settings.admin_wa_number,
                body=admin_vacancy_alert_body(vacancy, recruiter),
            )

    _set_state(wa_number, "recruiter_idle", {}, db)


async def handle_my_vacancies_button(wa_number: str, db: Session) -> None:
    """
    Show the recruiter a mini dashboard summary of their recent vacancies via WhatsApp.
    """
    recruiter = db.query(Recruiter).filter_by(wa_number=wa_number).first()
    if not recruiter:
        await wa_client.send_text(to=wa_number, body="⚠️ You are not registered as a recruiter.")
        return

    # Total applications across all time for this recruiter's jobs
    total_apps = (
        db.query(CandidateApplication)
        .join(JobVacancy, CandidateApplication.vacancy_id == JobVacancy.id)
        .filter(JobVacancy.recruiter_id == recruiter.id)
        .count()
    )

    # Last 7 days vacancies
    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
    recent_vacancies = (
        db.query(JobVacancy)
        .filter(JobVacancy.recruiter_id == recruiter.id)
        .filter(JobVacancy.created_at >= seven_days_ago)
        .order_by(JobVacancy.created_at.desc())
        .all()
    )

    # Focus Areas
    categories = list(set([v.job_category for v in recent_vacancies if v.job_category]))
    focus_areas_str = ", ".join(categories) if categories else "No vacancies submitted in the last 7 days"

    # Most Recent Job
    latest_job = recent_vacancies[0] if recent_vacancies else None

    # Build body text
    lines = [
        "📊 *Your Summary in Last 7 Days*\n",
        f"🎯 *Focus Areas:* {focus_areas_str}\n",
        f"📥 *Applications Received:* {total_apps}\n",
        "📌 *Most Recent Vacancy:*"
    ]

    if latest_job:
        status_emoji = {"approved": "✅", "pending": "⏳", "rejected": "❌"}.get(latest_job.status, "❓")
        status_label = latest_job.status.capitalize() if latest_job.status else "Unknown"
        lines.append(f"💼 *{latest_job.job_title.strip()}* ({latest_job.job_code})")
        lines.append(f"📍 {latest_job.exact_location}, {latest_job.district_region}")
        lines.append(f"📊 Status: {status_emoji} {status_label}")
    else:
        lines.append("No new vacancies posted in the last 7 days.")

    summary_text = "\n".join(lines)

    magic_url = _generate_magic_dashboard_url(recruiter, db)

    await wa_client.send_interactive_cta_url(
        to=wa_number,
        body_text=summary_text,
        button_display_text="View Full Dashboard",
        button_url=magic_url
    )


async def handle_post_vacancy_button(wa_number: str, db: Session) -> None:
    """Launch the post vacancy WhatsApp Flow."""
    loc_options = _location_options_for()

    await wa_client.send_flow(
        to=wa_number,
        flow_id=settings.FLOW_ID_POST_VACANCY,
        flow_cta="Post Vacancy",
        body_text=(
            "📝 *Post a New Vacancy!*\n\n"
            "Reach thousands of active job seekers across Kerala instantly. 🚀\n\n"
            "Tap the button below to fill in your job details. It takes less than a minute and is 100% free!\n\n"
            "_Ready to hire? Click below to begin._ 👇"
        ),
        flow_action_payload={
            "screen": "JOB_DETAILS_ONE",
            "data": {
                "location_options": loc_options
            }
        }
    )
    _set_state(wa_number, "recruiter_posting_vacancy", {}, db)


async def notify_recruiter_approval(vacancy_id: int, db: Session) -> None:
    """Called by admin panel when a vacancy is approved.

    Sends three messages:
      A) Private CTA to recruiter – approval notice + magic dashboard link
      B) Marketing template (job_alert) to recruiter — shareable card
      C) Same marketing template to admin/channel WA number
    """
    vacancy = db.query(JobVacancy).filter_by(id=vacancy_id).first()
    if not vacancy:
        return
    vacancy.status = "approved"
    vacancy.approved_at = datetime.now(timezone.utc)
    db.commit()

    recruiter = vacancy.recruiter
    if not recruiter:
        return

    # ── Message A: Private recruiter alert with magic dashboard link ────────
    magic_url = _generate_magic_dashboard_url(recruiter, db)
    private_body = (
        f"✅ Your vacancy for *{vacancy.job_title.strip()}* ({vacancy.job_code}) is approved and now live at kerala's first whatsapp automated job listing platform!\n\n"
        f"👇 You can forward the following message to your contacts to gather more applicants!\n\n"
        f"Thank you for choosing *jobinfo*"
    )
    try:
        await wa_client.send_interactive_cta_url(
            to=recruiter.wa_number,
            body_text=private_body,
            button_display_text="View Dashboard",
            button_url=magic_url,
        )
    except Exception as e:
        logger.warning("Private approval CTA failed, falling back to text: %s", e)
        await wa_client.send_text(to=recruiter.wa_number, body=private_body)

    # ── Message B: Recruiter card — clean redirect link (survives forwarding) ─
    recruiter_card = job_alert_text_body(
        vacancy,
        apply_url=f"{settings.app_base_url}/api/apply/{vacancy.job_code}",
    )
    await wa_client.send_text(to=recruiter.wa_number, body=recruiter_card)

    # ── Message C: Admin/channel card — wa.me deep-link (native WA button) ───
    if settings.admin_wa_number:
        admin_card = job_alert_text_body(
            vacancy,
            apply_url=f"https://wa.me/{settings.business_wa_number}?text=Apply%20{vacancy.job_code}",
        )
        await wa_client.send_text(to=settings.admin_wa_number, body=admin_card)


async def notify_recruiter_rejection(
    vacancy_id: int, reason: str, db: Session
) -> None:
    """Called by admin panel when a vacancy is rejected."""
    vacancy = db.query(JobVacancy).filter_by(id=vacancy_id).first()
    if not vacancy:
        return
    vacancy.status = "rejected"
    vacancy.rejection_reason = reason
    db.commit()

    recruiter = vacancy.recruiter
    if recruiter:
        await wa_client.send_text(
            to=recruiter.wa_number,
            body=vacancy_rejected_body(vacancy),
        )


def _set_state(wa_number: str, state: str, context: dict, db: Session) -> None:
    rec = _get_or_create_state(wa_number, db)
    rec.state = state
    rec.context = context
    db.commit()
