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
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import ConversationState, JobVacancy, Recruiter
from app.services.job_code import generate_job_code
from app.whatsapp.client import wa_client
from app.whatsapp.templates import (
    admin_vacancy_alert_body,
    recruiter_welcome_components,
    vacancy_confirmation_body,
    registration_confirmation_body,
    vacancy_approved_body,
    vacancy_rejected_body,
)

logger = logging.getLogger(__name__)
settings = get_settings()

# IDs of WhatsApp Flows – create these in Meta's Flow Builder, paste IDs here
FLOW_ID_RECRUITER_REGISTER = "1453423649718282"
FLOW_ID_POST_VACANCY = "4438148733098809"
FLOW_ID_MY_VACANCIES = "YOUR_MY_VACANCIES_FLOW_ID"

# Template name for the utility template (create and approve on Meta)
TEMPLATE_RECRUITER_WELCOME = "jobinfo_welcome_recruiter"


def _get_or_create_state(wa_number: str, db: Session) -> ConversationState:
    state = db.query(ConversationState).filter_by(wa_number=wa_number).first()
    if not state:
        state = ConversationState(wa_number=wa_number, state="idle", context={})
        db.add(state)
        db.commit()
        db.refresh(state)
    return state


async def start(wa_number: str, db: Session) -> None:
    """
    Entry point: called when a recruiter sends 'My Vacancy' (or taps the menu button).
    Checks if they are registered and routes accordingly.
    """
    recruiter = db.query(Recruiter).filter_by(wa_number=wa_number).first()

    if recruiter:
        # Returning recruiter – send utility template with buttons
        await wa_client.send_template(
            to=wa_number,
            template_name=TEMPLATE_RECRUITER_WELCOME,
            components=recruiter_welcome_components(recruiter),
        )
        _set_state(wa_number, "recruiter_idle", {}, db)
    else:
        # New recruiter – launch registration Flow
        await wa_client.send_flow(
            to=wa_number,
            flow_id=FLOW_ID_RECRUITER_REGISTER,
            flow_cta="Register as Recruiter",
            body_text=(
                "👋 Welcome to *JobInfo*!\n\n"
                "To post vacancies, please complete a quick one-time registration.\n"
                "It takes less than 2 minutes. Tap below to begin."
            ),
            header_text="JobInfo – Post Jobs via WhatsApp",
        )
        _set_state(wa_number, "recruiter_registering", {}, db)


async def handle_registration_flow_completion(
    wa_number: str, flow_data: dict, db: Session
) -> None:
    """
    Called when Meta sends the WhatsApp Flow completion event for recruiter registration.
    flow_data keys (from your Flow): name, company, location, email
    """
    recruiter = Recruiter(
        wa_number=wa_number,
        name=flow_data.get("name", ""),
        company=flow_data.get("company"),
        location=flow_data.get("location"),
        email=flow_data.get("email"),
    )
    db.add(recruiter)
    db.commit()
    db.refresh(recruiter)

    # Confirmation message
    await wa_client.send_text(
        to=wa_number,
        body=registration_confirmation_body(recruiter.name, "recruiter"),
    )

    # Follow up with the welcome template showing both buttons
    await wa_client.send_template(
        to=wa_number,
        template_name=TEMPLATE_RECRUITER_WELCOME,
        components=recruiter_welcome_components(recruiter),
    )
    _set_state(wa_number, "recruiter_idle", {}, db)


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
    vacancy = JobVacancy(
        job_code=job_code,
        recruiter_id=recruiter.id,
        title=flow_data.get("title", ""),
        company=flow_data.get("company") or recruiter.company,
        location=flow_data.get("location", ""),
        description=flow_data.get("description"),
        salary_range=flow_data.get("salary_range"),
        experience_required=flow_data.get("experience_required"),
        contact_info=flow_data.get("contact_info"),
    )
    db.add(vacancy)
    db.commit()
    db.refresh(vacancy)

    # Notify recruiter
    await wa_client.send_text(
        to=wa_number,
        body=vacancy_confirmation_body(vacancy),
    )

    # Notify admin (personal WA number)
    if settings.admin_wa_number:
        await wa_client.send_text(
            to=settings.admin_wa_number,
            body=admin_vacancy_alert_body(vacancy, recruiter),
        )

    _set_state(wa_number, "recruiter_idle", {}, db)


async def handle_my_vacancies_button(wa_number: str, db: Session) -> None:
    """
    Show the recruiter their vacancies list via a WhatsApp Flow.
    """
    recruiter = db.query(Recruiter).filter_by(wa_number=wa_number).first()
    if not recruiter:
        await wa_client.send_text(to=wa_number, body="⚠️ You are not registered as a recruiter.")
        return

    vacancies = (
        db.query(JobVacancy)
        .filter_by(recruiter_id=recruiter.id)
        .order_by(JobVacancy.created_at.desc())
        .limit(10)
        .all()
    )

    if not vacancies:
        await wa_client.send_text(to=wa_number, body="You haven't posted any vacancies yet.")
        return

    # If you have a dedicated "My Vacancies" Flow, launch it with vacancy data injected.
    # Otherwise, send a formatted text summary.
    lines = ["📋 *Your Vacancies:*\n"]
    for v in vacancies:
        status_emoji = {"approved": "✅", "pending": "⏳", "rejected": "❌"}.get(v.status, "❓")
        lines.append(f"{status_emoji} *{v.title}* ({v.job_code}) – {v.status.value}")
    lines.append("\n_Visit jobinfo.club for full details_")

    await wa_client.send_text(to=wa_number, body="\n".join(lines))


async def handle_post_vacancy_button(wa_number: str, db: Session) -> None:
    """Launch the post vacancy WhatsApp Flow."""
    await wa_client.send_flow(
        to=wa_number,
        flow_id=FLOW_ID_POST_VACANCY,
        flow_cta="Post Vacancy",
        body_text=(
            "📝 *Post a New Vacancy*\n\n"
            "Fill in your job details in the form below.\n"
            "It only takes a minute!"
        ),
        header_text="JobInfo – Post Vacancy",
    )
    _set_state(wa_number, "recruiter_posting_vacancy", {}, db)


async def notify_recruiter_approval(vacancy_id: int, db: Session) -> None:
    """Called by admin panel when a vacancy is approved."""
    vacancy = db.query(JobVacancy).filter_by(id=vacancy_id).first()
    if not vacancy:
        return
    vacancy.status = "approved"
    vacancy.approved_at = datetime.now(timezone.utc)
    db.commit()

    recruiter = vacancy.recruiter
    if recruiter:
        await wa_client.send_text(
            to=recruiter.wa_number,
            body=vacancy_approved_body(vacancy),
        )


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
