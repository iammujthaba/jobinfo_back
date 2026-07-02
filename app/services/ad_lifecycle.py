import asyncio
import logging
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.db.models import ConversationState, JobVacancy, Recruiter

logger = logging.getLogger(__name__)


def _make_aware(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _is_within_24h_window(state):
    if not state or not state.last_user_message_at:
        return False
    last = _make_aware(state.last_user_message_at)
    return (datetime.now(timezone.utc) - last).total_seconds() < 86_400


def _get_stop_body(vacancy, reason):
    reason_text = "automatically after 30 days" if reason == "auto" else "manually"
    return (
        f"Your vacancy for *{vacancy.job_title.strip()}* ({vacancy.job_code}) "
        f"has been stopped {reason_text}.\n\n"
        "You can re-run it at any time from your dashboard."
    )


def _fire_cta_send(wa_number, header, body, background_tasks=None):
    from app.whatsapp.client import wa_client

    if background_tasks is not None:
        background_tasks.add_task(
            wa_client.send_cta_url,
            to=wa_number,
            header_text=header,
            body_text=body,
            button_text="View Dashboard",
            url="https://jobinfo.pro/recruiter-dashboard",
        )
    else:
        async def _send():
            try:
                await wa_client.send_cta_url(
                    to=wa_number,
                    header_text=header,
                    body_text=body,
                    button_text="View Dashboard",
                    url="https://jobinfo.pro/recruiter-dashboard",
                )
            except Exception as exc:
                logger.warning("Ad lifecycle CTA send failed to %s: %s", wa_number, exc)

        try:
            loop = asyncio.get_event_loop()
            loop.create_task(_send())
        except RuntimeError:
            logger.debug("No event loop; skipping ad lifecycle send to %s", wa_number)


def _fire_stop_notification(vacancy, db, reason="auto", background_tasks=None):
    recruiter_wa = vacancy.recruiter.wa_number if vacancy.recruiter else None
    if not recruiter_wa:
        return
    state = db.query(ConversationState).filter_by(wa_number=recruiter_wa).first()
    if not _is_within_24h_window(state):
        logger.info("Ad stop (%s) for %s outside 24h window - deferred", reason, vacancy.job_code)
        return

    _fire_cta_send(recruiter_wa, "Ad Stopped", _get_stop_body(vacancy, reason), background_tasks=background_tasks)
    vacancy.ad_stop_notification_pending = False
    db.commit()


def _get_rerun_body(vacancy):
    return (
        f"Your vacancy for *{vacancy.job_title.strip()}* ({vacancy.job_code}) "
        f"has been successfully re-run.\n\n"
        "It is now live and will run for another 30 days."
    )


def _fire_rerun_notification(vacancy, db, background_tasks=None):
    recruiter_wa = vacancy.recruiter.wa_number if vacancy.recruiter else None
    if not recruiter_wa:
        return
    state = db.query(ConversationState).filter_by(wa_number=recruiter_wa).first()
    if not _is_within_24h_window(state):
        logger.info("Ad rerun for %s outside 24h window - skipping", vacancy.job_code)
        return

    _fire_cta_send(recruiter_wa, "Ad Re-run Successful", _get_rerun_body(vacancy), background_tasks=background_tasks)


def is_ad_running(vacancy) -> bool:
    if not vacancy.is_active:
        return False
    if vacancy.status != "approved":
        return False
    clock = vacancy.last_enabled_at or vacancy.approved_at
    if clock is None:
        return True   # no approval date = just submitted, not yet live
    
    clock = _make_aware(clock)
    return (datetime.now(timezone.utc) - clock).days < 30


def ensure_ad_active(vacancy, db: Session) -> bool:
    if not vacancy.is_active:
        return False   # already stopped manually — no change
    if vacancy.status != "approved":
        return False   # not live anyway

    clock = vacancy.last_enabled_at or vacancy.approved_at
    if clock and (datetime.now(timezone.utc) - _make_aware(clock)).days >= 30:
        # Auto-stop: 30-day clock expired
        vacancy.is_active = False
        vacancy.stopped_at = datetime.now(timezone.utc)
        vacancy.ad_stop_notification_pending = True
        db.commit()
        _fire_stop_notification(vacancy, db, reason="auto")
        return False

    return True   # ad is healthy


def toggle_ad(vacancy, db: Session, action: str, background_tasks=None) -> None:
    if action == "stop":
        vacancy.is_active = False
        vacancy.stopped_at = datetime.now(timezone.utc)
        vacancy.ad_stop_notification_pending = True
        db.commit()
        _fire_stop_notification(vacancy, db, reason="manual", background_tasks=background_tasks)
    elif action == "rerun":
        vacancy.is_active = True
        vacancy.last_enabled_at = datetime.now(timezone.utc)
        vacancy.stopped_at = None
        vacancy.ad_stop_notification_pending = False
        db.commit()
        _fire_rerun_notification(vacancy, db, background_tasks=background_tasks)


def check_and_send_ad_stop_catchup(wa_number: str, db: Session):
    recruiter = db.query(Recruiter).filter_by(wa_number=wa_number).first()
    if not recruiter:
        return
    pending_vacancies = (
        db.query(JobVacancy)
        .filter(
            JobVacancy.recruiter_id == recruiter.id,
            JobVacancy.ad_stop_notification_pending == True,
        )
        .all()
    )
    for vacancy in pending_vacancies:
        _fire_cta_send(wa_number, "Ad Stopped", _get_stop_body(vacancy, "auto"))
        vacancy.ad_stop_notification_pending = False
    
    if pending_vacancies:
        db.commit()
        logger.info("Catch-up: sent %d ad stop notification(s) to %s", len(pending_vacancies), wa_number)
