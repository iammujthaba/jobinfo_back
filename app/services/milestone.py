import asyncio
import logging
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.db.models import ConversationState, JobVacancy, Recruiter

logger = logging.getLogger(__name__)
DASHBOARD_URL = "https://jobinfo.club/recruiter-dashboard"


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


def _milestone_header(count):
    """Short header line shown above the message body (max 60 chars)."""
    return "🎯 Milestone Reached!"


def _milestone_body(vacancy, count):
    """Body text for the CTA URL interactive message (max 1024 chars)."""
    em = chr(8212)
    loc = (vacancy.exact_location or em) + ", " + (vacancy.district_region or em)
    return (
        "Your vacancy for *" + vacancy.job_title.strip() + "* ("
        + vacancy.job_code + ") just hit *" + str(count) + " applications*!\n\n"
        + "Job Location: " + loc + "\n"
        + "Total Applications: " + str(count) + "\n\n"
        + "Log in to your dashboard to review your candidates."
    )


def _fire_cta_send(wa_number, header, body):
    """
    Schedule a CTA URL interactive message on the running asyncio event loop.
    Renders as a tappable 'View Dashboard' button — no naked URL in the text.
    Safe to call from both async handlers and sync def endpoints.
    """
    from app.whatsapp.client import wa_client

    async def _send():
        try:
            await wa_client.send_cta_url(
                to=wa_number,
                header_text=header,
                body_text=body,
                button_text="View Dashboard",
                url=DASHBOARD_URL,
            )
        except Exception as exc:
            logger.warning("Milestone CTA send failed to %s: %s", wa_number, exc)

    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_send())
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
    _fire_cta_send(recruiter_wa, _milestone_header(app_count), _milestone_body(vacancy, app_count))
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
    for vacancy in pending_vacancies:
        count = vacancy.milestone_pending_count
        _fire_cta_send(wa_number, _milestone_header(count), _milestone_body(vacancy, count))
        vacancy.milestone_notified_count = count
    if pending_vacancies:
        db.commit()
        logger.info("Catch-up: sent %d milestone notification(s) to %s", len(pending_vacancies), wa_number)
