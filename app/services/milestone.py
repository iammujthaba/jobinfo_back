import asyncio
import logging
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.db.models import ConversationState, JobVacancy, Recruiter

logger = logging.getLogger(__name__)
DASHBOARD_URL = "https://jobinfo.pro/recruiter-dashboard.html"


def is_a_milestone(count):
    if count <= 0:
        return False
    if count in (1, 5):
        return True
    if count <= 50:
        return count % 10 == 0
    if count <= 500:
        return count % 50 == 0
    return count % 100 == 0


def _is_within_24h_window(state):
    if not state or not state.last_user_message_at:
        return False
    last = state.last_user_message_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds() < 86_400


def _ordinal(n):
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return str(n) + suffix


def _milestone_header(count: int) -> str:
    """Short header line shown above the message body (max 60 chars)."""
    if count == 1:
        return "🎉 First Application Received!"
    elif count == 5:
        return "🔥 5 Candidates Applied!"
    else:
        return f"🎯 Milestone: {count} Applications!"


def _milestone_body(vacancy: JobVacancy, count: int) -> str:
    """Body text for the CTA URL interactive message (max 1024 chars)."""
    title = (vacancy.job_title or "").strip()
    code = (vacancy.job_code or "").strip()
    loc_parts = [p.strip().title() for p in [vacancy.exact_location, vacancy.district_region] if p and p.strip()]
    location = ", ".join(loc_parts) if loc_parts else "Kerala"

    if count == 1:
        return (
            "Great news! You just received your *first applicant* for:\n\n"
            f"💼 *Role:* {title}\n"
            f"🔖 *Job Code:* {code}\n"
            f"📍 *Location:* {location}\n\n"
            "_Candidates who receive early responses are 2x more likely to accept interviews._\n\n "
            "Tap below to review their profile and contact them directly 👇"
        )
    elif count == 5:
        return (
            "Your job post is picking up strong momentum! 🚀\n\n"
            f"💼 *Role:* {title} ({code})\n"
            f"📍 *Location:* {location}\n"
            "👥 *Total Applicants:* 5 candidates\n\n"
            "_Ready to start shortlisting? Tap below to compare resumes and connect with your top candidates_ 👇"
        )
    else:
        return (
            "High interest alert! Your vacancy has reached a major milestone:\n\n"
            f"💼 *Role:* {title} ({code})\n"
            f"📍 *Location:* {location}\n"
            f"📈 *Total Applicants:* {count} candidates\n\n"
            "_Top talent gets hired quickly. We recommend reviewing your applicant queue immediately_"
            "_and connecting with shortlisted candidates_ 👇"
        )


def _get_recruiter_magic_url(wa_number: str, db: Session, expires_hours: int = 72) -> str:
    """Generate an authenticated single-sign-on magic link URL for the recruiter dashboard."""
    try:
        from app.handlers.dispatcher import _generate_magic_url
        return _generate_magic_url(
            wa_number=wa_number,
            role="recruiter",
            path="recruiter-dashboard.html",
            db=db,
            expires_hours=expires_hours,
        )
    except Exception as exc:
        logger.warning("Failed to generate magic URL for %s: %s", wa_number, exc)
        return DASHBOARD_URL


def _fire_cta_send(wa_number: str, header: str, body: str, url: str | None = None):
    """
    Schedule a CTA URL interactive message on the running asyncio event loop.
    Renders as a tappable 'Review Applicants' button with automatic magic link authentication.
    Safe to call from both async handlers and sync def endpoints.
    """
    from app.whatsapp.client import wa_client

    target_url = url or DASHBOARD_URL

    async def _send():
        try:
            await wa_client.send_cta_url(
                to=wa_number,
                header_text=header,
                body_text=body,
                button_text="Review Applicants",
                url=target_url,
                footer_text="🔒 Safe & private • Valid 72h",
            )
        except Exception as exc:
            logger.warning("Milestone CTA send failed to %s: %s", wa_number, exc)
            try:
                await wa_client.send_text(
                    to=wa_number,
                    body=f"{body}\n\n👉 Review Applicants: {target_url}",
                )
            except Exception as e2:
                logger.warning("Milestone fallback text send failed to %s: %s", wa_number, e2)

    try:
        loop = asyncio.get_running_loop()          # inside async context (uvicorn/FastAPI)
        loop.create_task(_send())
    except RuntimeError:
        # No running loop — called from a sync context; try get_event_loop as fallback
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_send())
            else:
                logger.debug("No running event loop; skipping milestone send to %s", wa_number)
        except RuntimeError:
            logger.debug("No event loop; skipping milestone send to %s", wa_number)


def dispatch_milestone_notification(vacancy, app_count, db):
    if not is_a_milestone(app_count):
        return
    recruiter_wa = vacancy.recruiter.wa_number if vacancy.recruiter else None
    if not recruiter_wa:
        logger.warning("Milestone triggered for vacancy %s but recruiter has no wa_number", vacancy.job_code)
        return
    if app_count > vacancy.milestone_pending_count:
        vacancy.milestone_pending_count = app_count
        db.commit()
    state = db.query(ConversationState).filter_by(wa_number=recruiter_wa).first()
    if not _is_within_24h_window(state):
        logger.info("Milestone %d for %s outside 24h window - deferred", app_count, vacancy.job_code)
        return
    url = _get_recruiter_magic_url(recruiter_wa, db)
    _fire_cta_send(recruiter_wa, _milestone_header(app_count), _milestone_body(vacancy, app_count), url=url)
    vacancy.milestone_notified_count = app_count
    db.commit()
    logger.info("Milestone %d sent for %s to %s", app_count, vacancy.job_code, recruiter_wa)


def check_and_send_catchup(wa_number, db):
    recruiter = db.query(Recruiter).filter_by(wa_number=wa_number).first()
    if not recruiter:
        return
    pending_vacancies = (
        db.query(JobVacancy)
        .filter(
            JobVacancy.recruiter_id == recruiter.id,
            JobVacancy.milestone_pending_count > JobVacancy.milestone_notified_count,
        )
        .all()
    )
    if not pending_vacancies:
        return
    url = _get_recruiter_magic_url(wa_number, db)
    for vacancy in pending_vacancies:
        count = vacancy.milestone_pending_count
        _fire_cta_send(wa_number, _milestone_header(count), _milestone_body(vacancy, count), url=url)
        vacancy.milestone_notified_count = count
    db.commit()
    logger.info("Catch-up: sent %d milestone notification(s) to %s", len(pending_vacancies), wa_number)
