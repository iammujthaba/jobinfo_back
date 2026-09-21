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
from app.db.models import (
    Candidate,
    ConversationState,
    JobVacancy,
    Recruiter,
    AdminNotificationQueue,
    CandidateApplication,
)
from app.services.job_code import generate_job_code
from app.whatsapp.client import wa_client
from app.whatsapp.templates import (
    admin_vacancy_alert_body,
    recruiter_vacancies_overview_body,
    recruiter_welcome_components,
    recruiter_workspace_body,
    vacancy_confirmation_body,
    vacancy_poster_preview_body,
    registration_confirmation_body,
    recruiter_post_vacancy_card_body,
    vacancy_rejected_body,
    job_alert_text_body,
    _label,
    SALARY_LABELS,
)

logger = logging.getLogger(__name__)
settings = get_settings()

# Template name for the utility template (create and approve on Meta)

# Template name for the utility template (create and approve on Meta)
TEMPLATE_RECRUITER_WELCOME = "jobinfo_welcome_recruiter_v3"


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
    expires = datetime.now(timezone.utc) + timedelta(hours=24)
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
        # Returning recruiter – send in-code dynamic workspace card with live stats & quick buttons
        body_text = recruiter_workspace_body(recruiter, db)
        buttons = [
            {"id": "btn_post_vacancy", "title": "📢 Post Vacancy"},
            {"id": "btn_my_vacancies", "title": "📋 My Vacancies"},
            {"id": "btn_my_dashboard", "title": "🖥️ My Dashboard"},
        ]
        await wa_client.send_buttons(
            to=wa_number,
            body_text=body_text,
            buttons=buttons,
        )
        _set_state(wa_number, "recruiter_idle", {}, db)
        return
    else:
        # New recruiter / employer – launch registration Flow
        await wa_client.send_flow(
            to=wa_number,
            flow_id=settings.FLOW_ID_RECRUITER_REGISTER,
            flow_cta="💼 Start Hiring",
            body_text=(
                "*⏳ Employer & Hiring Setup!*\n\n"
                "_Find the right staff for your business or shop!_\n\n"
                "Tap the button below to set up your profile and start posting jobs.\n\n"
                "✅ *100% Free & No Spam Calls*\n"
                "✅ *Simple & Easy to Use*\n"
                "✅ *WhatsApp-Powered Hiring*\n"
                "✅ *Kerala's Best Local Talent Pool*\n\n"
                "Takes less than 1 minute! Let’s get started. ✨"
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
        registrant_role=flow_data.get("registrant_role", "other") or "other",
    )
    db.add(recruiter)
    db.commit()
    db.refresh(recruiter)

    # Launch post vacancy flow directly on the welcoming card (0 intermediate friction)
    loc_options = _location_options_for()
    await wa_client.send_flow(
        to=wa_number,
        flow_id=settings.FLOW_ID_POST_VACANCY,
        flow_cta="📢 Post Vacancy",
        body_text=registration_confirmation_body(recruiter.company_name, "recruiter"),
        flow_action_payload={
            "screen": "JOB_DETAILS_ONE",
            "data": {
                "location_options": loc_options
            }
        }
    )

    _set_state(wa_number, "recruiter_posting_vacancy", {}, db)
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

    # 1. Send live poster preview as full-width plain text (untruncated job description)
    try:
        await wa_client.send_text(
            to=wa_number,
            body=vacancy_poster_preview_body(vacancy),
        )
    except Exception as preview_err:
        logger.warning("Live preview send failed: %s", preview_err)

    # 2. Send status confirmation with 'View Dashboard' interactive CTA button
    await wa_client.send_interactive_cta_url(
        to=wa_number,
        body_text=vacancy_confirmation_body(vacancy),
        button_display_text="View Dashboard",
        button_url=magic_url,
        footer_text="⏳ Secure link • Valid for 24 hours",
    )

    # Notify admins for new submission
    admin_url = _generate_admin_magic_url(db)
    for admin_num in settings.submission_admins:
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
                await wa_client.send_interactive_cta_url(
                    to=admin_num,
                    body_text=admin_vacancy_alert_body(vacancy, recruiter),
                    button_display_text="Review Vacancy",
                    button_url=admin_url,
                )
            except Exception as e:
                logger.warning("Admin CTA alert failed for %s, falling back to text: %s", admin_num, e)
                await wa_client.send_text(
                    to=admin_num,
                    body=admin_vacancy_alert_body(vacancy, recruiter),
                )
        else:
            logger.info("Admin %s outside 24h window. Queueing new_submission alert for %s", admin_num, vacancy.job_code)
            queue_item = AdminNotificationQueue(
                wa_number=admin_num,
                notification_type="new_submission",
                vacancy_id=vacancy.id
            )
            db.add(queue_item)
    
    db.commit()

    _set_state(wa_number, "recruiter_idle", {}, db)


async def handle_my_vacancies_button(wa_number: str, db: Session) -> None:
    """
    Show the recruiter a mini dashboard summary of their recent vacancies via WhatsApp.
    """
    recruiter = db.query(Recruiter).filter_by(wa_number=wa_number).first()
    if not recruiter:
        await wa_client.send_text(to=wa_number, body="⚠️ You are not registered as a recruiter.")
        return

    summary_text = recruiter_vacancies_overview_body(recruiter, db)
    magic_url = _generate_magic_dashboard_url(recruiter, db)

    await wa_client.send_interactive_cta_url(
        to=wa_number,
        body_text=summary_text,
        button_display_text="Access Dashboard",
        button_url=magic_url,
        footer_text="⏳ Secure link • Valid for 24 hours",
    )


async def handle_post_vacancy_button(
    wa_number: str,
    db: Session,
    from_workspace: bool = False,
) -> None:
    """Launch the post vacancy WhatsApp Flow using the master recruiter card template."""
    recruiter = db.query(Recruiter).filter_by(wa_number=wa_number).first()
    company_name = recruiter.company_name if recruiter else ""
    vacancy_count = (
        db.query(JobVacancy).filter_by(recruiter_id=recruiter.id).count()
        if recruiter else 1
    )
    loc_options = _location_options_for()
    cta_title = "✍️ Fill Details" if from_workspace else "📢 Post Vacancy"

    await wa_client.send_flow(
        to=wa_number,
        flow_id=settings.FLOW_ID_POST_VACANCY,
        flow_cta=cta_title,
        body_text=recruiter_post_vacancy_card_body(
            company_name=company_name,
            is_new=False,
            vacancy_count=vacancy_count,
            from_workspace=from_workspace,
        ),
        flow_action_payload={
            "screen": "JOB_DETAILS_ONE",
            "data": {
                "location_options": loc_options
            }
        }
    )
    _set_state(wa_number, "recruiter_posting_vacancy", {}, db)
    return


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
    vacancy.last_enabled_at = vacancy.approved_at
    db.commit()

    recruiter = vacancy.recruiter
    if not recruiter:
        return

    # ── Message A: Private recruiter alert with magic dashboard link ────────
    magic_url = _generate_magic_dashboard_url(recruiter, db)
    private_body = (
        f"🎉 *Congratulations!*\n"
        f" *Your vacancy is now live.*\n\n"
        f"_Your job vacancy for *{vacancy.job_title.strip()}* (Code: *{vacancy.job_code}*) is now active on JobInfo Kerala!_ \n\n"
        f"📊 *Recruiter Dashboard:*\n"
        f"Tap the button below to manage your hiring:\n"
        f"• 📥 View incoming applicants & download CVs.\n"
        f"• 💬 Connect directly with shortlisted candidates.\n"
        f"• ⏳ Vacancy active for 30 days (pause or close anytime).\n"
        f"• 🔒 100% privacy – zero spam calls or messages to your phone.\n\n"
        f"👇 *Shareable Job Card:*\n"
        f"We have generated your official poster right below! Forward it to WhatsApp groups or your status to get more candidates."
    )
    try:
        await wa_client.send_interactive_cta_url(
            to=recruiter.wa_number,
            body_text=private_body,
            button_display_text="View Dashboard",
            button_url=magic_url,
            footer_text="⏳ Secure link • Valid for 24 hours",
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

    # ── Post-Approval 5-min follow-up (debounced, suppressed if 24h window closed) ──
    import asyncio
    from app.handlers.dispatcher import send_post_approval_session_menu
    asyncio.create_task(send_post_approval_session_menu(recruiter.wa_number, vacancy.id))

    # ── Message C: Admin/channel card — wa.me deep-link (native WA button) ───
    admin_card = job_alert_text_body(
        vacancy,
        apply_url=f"https://wa.me/{settings.business_wa_number}?text=Apply%20{vacancy.job_code}",
        is_admin=True,
    )
    
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
                await wa_client.send_text(to=admin_num, body=admin_card)
            except Exception as e:
                logger.warning("Failed to send approval alert to %s: %s", admin_num, e)
        else:
            logger.info("Admin %s outside 24h window. Queueing approved_vacancy alert for %s", admin_num, vacancy.job_code)
            queue_item = AdminNotificationQueue(
                wa_number=admin_num,
                notification_type="approved_vacancy",
                vacancy_id=vacancy.id
            )
            db.add(queue_item)
            
    db.commit()


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
        from app.handlers.dispatcher import _generate_magic_url
        url = _generate_magic_url(recruiter.wa_number, "recruiter", "recruiter-dashboard.html", db, expires_hours=72)
        body = vacancy_rejected_body(vacancy)
        try:
            await wa_client.send_cta_url(
                to=recruiter.wa_number,
                body_text=body,
                button_text="Fix & Resubmit",
                url=url,
                footer_text="⏳ Secure link • Valid for 72 hours",
            )
        except Exception as e:
            logger.warning("Rejection CTA send failed for %s, falling back to text: %s", recruiter.wa_number, e)
            await wa_client.send_text(
                to=recruiter.wa_number,
                body=f"{body}\n\n👉 {url}",
            )


def _set_state(wa_number: str, state: str, context: dict, db: Session) -> None:
    rec = _get_or_create_state(wa_number, db)
    rec.state = state
    rec.context = context
    db.commit()
