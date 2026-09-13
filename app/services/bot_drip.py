"""
Automated WhatsApp Bot Drip Service.
Detects abandoned job-seeker leads and executes intelligent 1-tap WhatsApp recovery nudges.
"""
import logging
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.db.models import ConversationState, Candidate, Recruiter, JobVacancy
from app.whatsapp.client import wa_client

logger = logging.getLogger(__name__)


def _get_unregistered_leads_query(db: Session):
    """Base query for unregistered visitors who interacted with the bot."""
    cand_subq = db.query(Candidate.wa_number).subquery("c_sub")
    rec_subq = db.query(Recruiter.wa_number).subquery("r_sub")

    return (
        db.query(ConversationState)
        .outerjoin(cand_subq, ConversationState.wa_number == cand_subq.c.wa_number)
        .outerjoin(rec_subq, ConversationState.wa_number == rec_subq.c.wa_number)
        .filter(cand_subq.c.wa_number.is_(None))
        .filter(rec_subq.c.wa_number.is_(None))
    )


def get_bot_drip_stats(db: Session) -> dict:
    """Calculates statistics for automated drip follow-ups."""
    now = datetime.now(timezone.utc)
    all_states = _get_unregistered_leads_query(db).all()

    total_drips_sent = 0
    golden_window_leads = 0
    eligible_leads = 0

    for s in all_states:
        ctx = s.context or {}
        if ctx.get("drip_sent_at") or ctx.get("help_messaged_at"):
            total_drips_sent += 1
            continue

        last_active = s.last_user_message_at or s.updated_at
        if not last_active:
            continue
        if last_active.tzinfo is None:
            last_active = last_active.replace(tzinfo=timezone.utc)

        idle_seconds = (now - last_active).total_seconds()
        # Golden window: inactive between 2 hours and 24 hours
        if 7200 <= idle_seconds <= 86400:
            golden_window_leads += 1
            eligible_leads += 1
        elif idle_seconds > 86400:
            eligible_leads += 1

    return {
        "total_drips_sent": total_drips_sent,
        "golden_window_leads": golden_window_leads,
        "eligible_leads": eligible_leads,
    }


async def execute_bot_drip(db: Session, max_batch: int = 25) -> dict:
    """
    Executes automated follow-up messages for eligible abandoned leads.
    Prioritizes leads inside Meta's 24-hour session window (< 24h) for free interactive delivery.
    """
    now = datetime.now(timezone.utc)
    all_unregistered = (
        _get_unregistered_leads_query(db)
        .order_by(ConversationState.updated_at.desc())
        .all()
    )

    vacancies_map = {v.job_code: v for v in db.query(JobVacancy).all()}
    sent_leads = []
    skipped_leads = []

    for s in all_unregistered:
        if len(sent_leads) >= max_batch:
            break

        ctx = dict(s.context or {})
        # Skip if already nudged or marked messaged
        if ctx.get("drip_sent_at") or ctx.get("help_messaged_at"):
            continue

        last_active = s.last_user_message_at or s.updated_at
        if not last_active:
            continue
        if last_active.tzinfo is None:
            last_active = last_active.replace(tzinfo=timezone.utc)

        idle_seconds = (now - last_active).total_seconds()
        # Require at least 30 minutes of inactivity before dripping (in prod: 2 hours)
        if idle_seconds < 1800:
            continue

        p_code = ctx.get("pending_job_code") or ctx.get("job_code")
        v_obj = vacancies_map.get(p_code) if p_code else None

        wa_number = s.wa_number
        is_golden = idle_seconds <= 86400

        try:
            if v_obj:
                job_title = v_obj.job_title.strip()
                loc_str = f" in {v_obj.district_region.title()}" if v_obj.district_region else ""
                body = (
                    f"Hi! 👋 We noticed you started applying for *{job_title}*{loc_str} on JobInfo.\n\n"
                    "Good news: You can now complete your application in under 30 seconds without a CV!\n\n"
                    "Would you like to finish and submit your application now? ✨"
                )
                buttons = [
                    {"id": f"btn_register_{p_code}", "title": "⚡ Complete Now"},
                    {"id": "ACTION_SUGGEST_JOBS", "title": "🔍 Browse Other Jobs"},
                ]
            else:
                body = (
                    "Hi! 👋 Welcome to JobInfo Kerala.\n\n"
                    "Looking for verified job openings in your district? You can set up your job alert profile in 30 seconds (no CV required)!\n\n"
                    "Tap below to get started:"
                )
                buttons = [
                    {"id": "btn_register_GENERAL", "title": "✨ Set Up Profile"},
                    {"id": "ACTION_SUGGEST_JOBS", "title": "🔍 View Hot Jobs"},
                ]

            # In testing or live, call wa_client
            await wa_client.send_buttons(
                to=wa_number,
                header_text="Job Application Follow-up 🚀",
                body_text=body,
                buttons=buttons,
                footer_text="JobInfo Kerala • Free Job Network",
            )

            # Mark state context
            ctx["drip_sent_at"] = now.isoformat()
            ctx["drip_job_code"] = p_code or "general"
            ctx["help_messaged_at"] = now.isoformat()
            s.context = ctx
            db.commit()

            sent_leads.append({
                "wa_number": wa_number,
                "job_title": v_obj.job_title if v_obj else "General Visitor",
                "is_golden_window": is_golden,
                "drip_time": now.isoformat(),
            })
            logger.info("Bot drip successfully sent to %s (job: %s)", wa_number, p_code)

        except Exception as exc:
            logger.warning("Bot drip failed for %s: %s", wa_number, exc)
            skipped_leads.append({
                "wa_number": wa_number,
                "error": str(exc),
                "is_golden_window": is_golden,
            })

    return {
        "success": True,
        "sent_count": len(sent_leads),
        "sent_leads": sent_leads,
        "skipped_leads": skipped_leads,
    }


async def bot_drip_background_worker():
    """
    Periodic background loop that automatically scans and drips abandoned leads.
    Checks 'bot_drip_auto_enabled' setting before each cycle.
    """
    import asyncio
    logger.info("Bot Drip background worker started.")
    while True:
        try:
            from app.db.base import SessionLocal
            from app.db.models import get_system_setting

            db = SessionLocal()
            try:
                auto_enabled = get_system_setting(db, "bot_drip_auto_enabled", "true").lower() in ("true", "1", "yes")
                if auto_enabled:
                    logger.info("Auto Bot Drip scheduled cycle running...")
                    result = await execute_bot_drip(db, max_batch=20)
                    if result.get("sent_count", 0) > 0:
                        logger.info("Auto Bot Drip successfully nudged %s leads.", result["sent_count"])
            finally:
                db.close()
        except asyncio.CancelledError:
            logger.info("Bot Drip background worker stopped.")
            break
        except Exception as exc:
            logger.error("Error in Bot Drip background worker cycle: %s", exc)

        # Run cycle every 30 minutes (1800 seconds)
        try:
            await asyncio.sleep(1800)
        except asyncio.CancelledError:
            break

