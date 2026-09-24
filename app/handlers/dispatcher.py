"""
Central dispatcher for incoming WhatsApp messages.
This module replaces N8N's routing logic.

It parses the raw Meta webhook payload, extracts the relevant event
(text message, button reply, flow completion, etc.) and calls the
appropriate handler.
"""
import logging
import re
import time

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import Candidate, JobVacancy

from app.db.models import ConversationState
from app.handlers import global_handler
from app.whatsapp.client import wa_client

logger = logging.getLogger(__name__)


from fastapi import BackgroundTasks

# ─── Webhook Message Deduplication (CRIT-2) ──────────────────────────────────
# Prevents processing duplicate webhook events re-sent by Meta or rapid duplicates
_SEEN_MSG_WINDOW_SECONDS = 1800.0  # 30 minutes retention
_seen_message_ids: dict[str, float] = {}
_last_msg_cleanup: float = 0.0


def _is_duplicate_message(msg_id: str | None) -> bool:
    """Returns True if message id was already received recently; otherwise records it."""
    if not msg_id:
        return False
    now = time.time()
    global _last_msg_cleanup
    if now - _last_msg_cleanup > 300.0:  # Prune every 5 minutes
        _last_msg_cleanup = now
        cutoff = now - _SEEN_MSG_WINDOW_SECONDS
        to_del = [mid for mid, ts in _seen_message_ids.items() if ts < cutoff]
        for mid in to_del:
            _seen_message_ids.pop(mid, None)

    if msg_id in _seen_message_ids:
        return True
    _seen_message_ids[msg_id] = now
    return False


# ─── Anti-Spam & Rate Limiter ────────────────────────────────────────────────
_SPAM_WINDOW_SECONDS = 10.0       # Time window to monitor rapid messaging
_SPAM_MAX_MESSAGES = 5            # Max messages allowed within the window

# Tracks per wa_number:
# {
#     "timestamps": list[float],
#     "cooldown_until": float,
#     "last_cooldown_ended": float,
#     "strike": int,
#     "normal_count": int,
#     "warned": bool,
# }
_spam_tracker: dict[str, dict] = {}
_last_spam_cleanup: float = 0.0


def _cleanup_old_spam_entries(now: float) -> None:
    """Removes expired entries from _spam_tracker to prevent memory leaks."""
    global _last_spam_cleanup
    if now - _last_spam_cleanup < 300.0:  # Run at most once every 5 minutes
        return
    _last_spam_cleanup = now
    to_delete = [
        num for num, data in _spam_tracker.items()
        if data.get("cooldown_until", 0) < now
        and (not data.get("timestamps") or now - data["timestamps"][-1] > 600.0)
    ]
    for num in to_delete:
        _spam_tracker.pop(num, None)


async def _is_rate_limited(wa_number: str) -> bool:
    """
    Checks if a wa_number is flooding messages with tiered escalating cooldowns:
    - Strike 1: 1 minute (60s)
    - Strike 2: 3 minutes (180s)
    - Strike 3+: 10 minutes (600s) each time
    - If user messages normally (3 normal spaced interactions or 5 min clean), strikes reset to 0.
    """
    from app.whatsapp.client import wa_client
    now = datetime.now(timezone.utc).timestamp()
    _cleanup_old_spam_entries(now)

    data = _spam_tracker.setdefault(wa_number, {
        "timestamps": [],
        "cooldown_until": 0.0,
        "last_cooldown_ended": 0.0,
        "strike": 0,
        "normal_count": 0,
        "warned": False,
    })

    # 1. Currently in active cooldown?
    if now < data["cooldown_until"]:
        # Drop silently without querying DB or Meta API
        return True

    # Cooldown has expired; record when it ended and reopen the window
    if data["cooldown_until"] > 0:
        data["last_cooldown_ended"] = data["cooldown_until"]
        data["cooldown_until"] = 0.0
        data["warned"] = False
        data["timestamps"] = []
        data["normal_count"] = 0

    # If 5 minutes of clean behavior passed since last cooldown, reset strike count
    if data["strike"] > 0 and data["last_cooldown_ended"] > 0 and (now - data["last_cooldown_ended"] > 300.0):
        data["strike"] = 0
        data["normal_count"] = 0

    # 2. Record new message timestamp and prune timestamps outside the window
    data["timestamps"].append(now)
    cutoff = now - _SPAM_WINDOW_SECONDS
    data["timestamps"] = [ts for ts in data["timestamps"] if ts >= cutoff]

    # 3. Check if rate threshold is breached
    if len(data["timestamps"]) > _SPAM_MAX_MESSAGES:
        data["strike"] += 1
        data["normal_count"] = 0

        # Escalating cooldown tiers
        if data["strike"] == 1:
            cooldown_seconds = 60.0
            wait_text = "1 minute"
        elif data["strike"] == 2:
            cooldown_seconds = 180.0
            wait_text = "3 minutes"
        else:
            cooldown_seconds = 600.0
            wait_text = "10 minutes"

        data["cooldown_until"] = now + cooldown_seconds

        if not data["warned"]:
            data["warned"] = True
            try:
                await wa_client.send_text(
                    to=wa_number,
                    body=(
                        "⚠️ *Too Many Messages*\n\n"
                        "You are sending messages too quickly. To protect our jobinfo system from spam, "
                        "responses are temporarily paused.\n\n"
                        f"Please wait {wait_text} to try again. ⏳"
                    ),
                )
            except Exception as e:
                logger.warning("Failed to deliver spam warning to %s: %s", wa_number, e)
        return True

    # 4. Message is normal: if user has prior strikes, increment normal counter
    if data["strike"] > 0:
        if len(data["timestamps"]) == 1:
            # Standalone spaced-out normal message
            data["normal_count"] += 1
            if data["normal_count"] >= 3:
                data["strike"] = 0
                data["normal_count"] = 0
        else:
            data["normal_count"] = 0

    return False


async def dispatch(payload: dict, db: Session, background_tasks: "BackgroundTasks") -> None:
    """
    Main entry point called by the webhook POST handler.
    Parses the WhatsApp Cloud API payload and routes to the right handler.
    """
    try:
        entry = payload["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]

        # ── Incoming message events ──────────────────────────────────────────
        if "messages" in value:
            message = value["messages"][0]
            wa_number = message["from"]
            msg_type = message.get("type")
            msg_id = message.get("id")

            # ── Message Deduplication (CRIT-2) ──────────────────────────────
            if _is_duplicate_message(msg_id):
                logger.info("Duplicate webhook message %s from %s suppressed", msg_id, wa_number)
                return

            # ── Anti-Spam Rate Limiter & Flood Protection ────────────────────
            if await _is_rate_limited(wa_number):
                logger.warning("Spam flood suppressed for %s (rate limited / on cooldown)", wa_number)
                return

            _track_user_message(wa_number, db)
            await _check_and_send_admin_catchup(wa_number, db)
            background_tasks.add_task(send_delayed_session_menu, wa_number)

            logger.info("Incoming %s from %s", msg_type, wa_number)

            if msg_type == "text":
                await _handle_text(wa_number, message["text"]["body"], db)

            if msg_type == "interactive":
                interactive = message.get("interactive", {})
                inter_type = interactive.get("type")
                
                if inter_type == "nfm_reply":
                    await _handle_flow_reply(wa_number, interactive["nfm_reply"], db)
                    return
                    
                elif inter_type == "button_reply":
                    button_id = interactive.get("button_reply", {}).get("id")
                    if button_id:
                        await _handle_button(wa_number, button_id, db)
                elif inter_type == "list_reply":
                    list_id = interactive.get("list_reply", {}).get("id")
                    if list_id:
                        await _handle_list_reply(wa_number, list_id, db)

            elif msg_type == "document":
                # Direct document upload (CV)
                doc = message["document"]
                await _handle_document(wa_number, doc, db)

            elif msg_type == "button":
                # Catch button clicks from pre-approved Meta Templates
                button_payload = message["button"]["payload"]
                
                if button_payload == "Post Vacancy":
                    button_payload = "btn_post_vacancy"
                elif button_payload == "My Vacancies":
                    button_payload = "btn_my_vacancies"
                    
                await _handle_button(wa_number, button_payload, db)

            elif msg_type in ("audio", "voice", "image", "video", "sticker", "contacts", "location"):
                await _handle_unsupported_media(wa_number, msg_type, db)

        # ── Status updates (read receipts, delivered, etc.) – skip ──────────
        elif "statuses" in value:
            status = value["statuses"][0]
            logger.debug("Status update: %s for msg %s", status.get("status"), status.get("id"))

    except (KeyError, IndexError) as exc:
        logger.warning("Unexpected payload structure: %s | %s", exc, payload)

    except Exception as exc:
        logger.exception("Unhandled error in dispatch(): %s", exc)
        # Try to notify the user so they don't experience complete silence.
        # wa_number is only available if the crash happened after the messages block.
        try:
            _wa = payload["entry"][0]["changes"][0]["value"]["messages"][0]["from"]
        except (KeyError, IndexError, TypeError):
            _wa = None
        if _wa:
            try:
                from app.whatsapp.client import wa_client
                await wa_client.send_text(
                    to=_wa,
                    body=(
                        "⚠️ Something went wrong on our end. "
                        "Please try again in a moment, or type *menu* to start over."
                    ),
                )
            except Exception as notify_err:
                logger.warning("Failed to send error fallback message to %s: %s", _wa, notify_err)


# ─── Routing helpers ──────────────────────────────────────────────────────────

def _track_user_message(wa_number: str, db: Session) -> None:
    """Updates the last_user_message_at timestamp for a given wa_number."""
    state = db.query(ConversationState).filter_by(wa_number=wa_number).first()
    if not state:
        state = ConversationState(wa_number=wa_number, state="idle")
        db.add(state)
    state.last_user_message_at = datetime.now(timezone.utc)
    db.commit()

    # Catch-up: if this sender is a recruiter with deferred milestone
    # or ad-stop notifications, deliver them now that the 24h window is guaranteed open.
    try:
        from app.services.milestone import check_and_send_catchup
        from app.services.ad_lifecycle import check_and_send_ad_stop_catchup
        check_and_send_catchup(wa_number, db)
        check_and_send_ad_stop_catchup(wa_number, db)
    except Exception as catchup_err:
        logger.warning("Milestone/Ad-stop catch-up failed for %s: %s", wa_number, catchup_err)

async def _check_and_send_admin_catchup(wa_number: str, db: Session) -> None:
    """Processes pending admin notifications and lazily cleans old queue items."""
    try:
        from app.config import get_settings
        settings = get_settings()
        admin_numbers = set(settings.submission_admins + settings.approval_admins)
        if settings.admin_wa_number:
            admin_numbers.add(settings.admin_wa_number)
        if wa_number not in admin_numbers:
            return

        from app.db.models import AdminNotificationQueue, JobVacancy
        from datetime import datetime, timedelta, timezone
        from app.whatsapp.templates import admin_vacancy_alert_body, job_alert_text_body
        from app.handlers.recruiter import _generate_admin_magic_url
        from app.whatsapp.client import wa_client

        # 1. Lazy Cleanup: delete records older than 10 days
        ten_days_ago = datetime.now(timezone.utc) - timedelta(days=10)
        db.query(AdminNotificationQueue).filter(
            AdminNotificationQueue.created_at < ten_days_ago
        ).delete(synchronize_session="fetch")
        db.commit()

        # 2. Process pending items for THIS admin number
        pending = db.query(AdminNotificationQueue).filter_by(wa_number=wa_number).all()
        if not pending:
            return

        settings = get_settings()
        items_to_delete = []

        for item in pending:
            vacancy = db.query(JobVacancy).filter_by(id=item.vacancy_id).first()
            if not vacancy:
                # Vacancy was deleted — orphan queue item, always clean it up
                items_to_delete.append(item)
                continue

            sent = False

            if item.notification_type == "new_submission":
                admin_url = _generate_admin_magic_url(db)
                try:
                    await wa_client.send_interactive_cta_url(
                        to=wa_number,
                        body_text=admin_vacancy_alert_body(vacancy, vacancy.recruiter),
                        button_display_text="Review Vacancy",
                        button_url=admin_url,
                    )
                    sent = True
                except Exception as e:
                    logger.warning("Admin catch-up CTA failed for %s, falling back to text: %s", wa_number, e)
                    try:
                        await wa_client.send_text(
                            to=wa_number,
                            body=admin_vacancy_alert_body(vacancy, vacancy.recruiter),
                        )
                        sent = True
                    except Exception as e2:
                        logger.error("Admin catch-up text fallback also failed for %s: %s", wa_number, e2)

            elif item.notification_type == "approved_vacancy":
                admin_card = job_alert_text_body(
                    vacancy,
                    apply_url=f"https://wa.me/{settings.business_wa_number}?text=Apply%20{vacancy.job_code}",
                    is_admin=True,
                )
                try:
                    await wa_client.send_text(to=wa_number, body=admin_card)
                    sent = True
                except Exception as e:
                    logger.error("Admin catch-up approved alert failed for %s: %s", wa_number, e)

            # Only remove from queue after confirmed delivery
            if sent:
                items_to_delete.append(item)

        for item in items_to_delete:
            db.delete(item)
        db.commit()
        logger.info(
            "Admin catch-up for %s: %d pending, %d delivered/cleaned",
            wa_number, len(pending), len(items_to_delete),
        )

    except Exception as e:
        logger.error("Error in admin catch-up logic for %s: %s", wa_number, e)


async def _handle_text(wa_number: str, text: str, db: Session) -> None:
    """Route a plain text message."""
    from app.handlers import recruiter as recruiter_handler
    from app.handlers import seeker as seeker_handler
    from app.services.job_code import parse_job_code

    normalized = text.strip()

    # ── Reverse OTP: plain 6-digit message from a number with a pending session ──
    import re
    if re.match(r"^\d{6}$", normalized):
        from app.db.models import WebLoginSession as _WLS
        from datetime import datetime as _dt, timezone as _tz
        _now = _dt.now(_tz.utc)
        _pending = (
            db.query(_WLS)
            .filter(
                _WLS.wa_number == wa_number,
                _WLS.status == "pending",
                _WLS.expires_at > _now,
            )
            .first()
        )
        if _pending:
            await _handle_login_otp(wa_number, normalized, db)
            return

    normalized = normalized.lower()
    clean_text = re.sub(r"[^\w\s]", "", normalized).strip()

    # 1. Recruiter commands ("Post Vacancy", "My Vacancy", "I am Hiring")
    if clean_text in ("my vacancy", "my vacancies"):
        await recruiter_handler.start(wa_number, db)
        return

    if clean_text in (
        "post vacancy", "post vacancies", "post a vacancy",
        "post job", "post jobs", "post a job",
        "i am hiring", "im hiring", "hiring", "hire"
    ):
        from app.db.models import Recruiter
        recruiter = db.query(Recruiter).filter_by(wa_number=wa_number).first()
        if recruiter:
            await recruiter_handler.handle_post_vacancy_button(wa_number, db, from_workspace=False)
        else:
            await recruiter_handler.start(wa_number, db)
        return

    # 2. Seeker prefilled link & button keywords ("Suggest Jobs", "Looking for Job")
    if clean_text in (
        "suggest jobs", "suggest job", "suggest vacancies", "suggest vacancy",
        "job suggestions", "jobs suggestion"
    ):
        await seeker_handler.handle_suggest_jobs(wa_number, db)
        return

    if clean_text in (
        "looking for job", "looking for a job", "looking for jobs",
        "find jobs", "find a job", "find job", "i am job seeker", "job seeker"
    ):
        candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
        if candidate and candidate.registration_complete:
            await seeker_handler.handle_suggest_jobs(wa_number, db)
        else:
            await seeker_handler.handle_create_general_profile(wa_number, db)
        return

    if clean_text in ("explore jobs", "explore job"):
        await seeker_handler.handle_explore_jobs(wa_number)
        return

    # Seeker apply link text (e.g. "Apply JC:1002")
    job_code = parse_job_code(text)
    if job_code:
        await seeker_handler.start(wa_number, job_code, db)
        return

    # RENEW keyword
    if normalized == "renew":
        await candidate_handler_renew(wa_number, db)
        return


    # ── Plan A: Seeker Interceptors & Smart Routing ─────────────────────────
    from app.db.models import ConversationState, Candidate, JobVacancy, CandidateApplication, Recruiter
    from datetime import datetime, timezone

    conv_state = db.query(ConversationState).filter_by(wa_number=wa_number).first()
    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
    is_registered = candidate and candidate.registration_complete

    # ── Global Navigation Commands ─────────────────────────────────────────
    if clean_text in ("menu", "main menu", "home"):
        await global_handler.send_help_menu(wa_number)
        if conv_state:
            conv_state.state = "idle"
            conv_state.context = {}
            db.commit()
        return

    if clean_text in ("help", "support"):
        await global_handler.send_help_support_menu(wa_number)
        if conv_state:
            conv_state.state = "idle"
            conv_state.context = {}
            db.commit()
    ctx = (conv_state.context or {}) if conv_state else {}
    pending_job_code = ctx.get("pending_job_code")

    # 1. Registered Seeker Guard (Fix 1)
    if is_registered:
        last_active = conv_state.last_user_message_at if conv_state else None
        if last_active and last_active.tzinfo is None:
            last_active = last_active.replace(tzinfo=timezone.utc)
        days_since = (datetime.now(timezone.utc) - last_active).days if last_active else 999

        if pending_job_code and days_since <= 14:
            vacancy = db.query(JobVacancy).filter_by(job_code=pending_job_code).first()
            from app.services.ad_lifecycle import ensure_ad_active
            if vacancy and ensure_ad_active(vacancy, db):
                # Check if already applied
                has_applied_this = db.query(CandidateApplication).filter_by(
                    candidate_id=candidate.id, vacancy_id=vacancy.id
                ).first() is not None
                if not has_applied_this:
                    # Case 1: 1-Tap Quick Apply Card
                    await seeker_handler.handle_registered_quick_apply(wa_number, candidate, vacancy)
                    return
                else:
                    # Case 2: Already applied -> silently clear pending code and route to Dashboard
                    if conv_state:
                        conv_state.context = {}
                        db.commit()
            else:
                # Case 3: Vacancy closed or stale -> silently clear pending code
                if conv_state:
                    conv_state.context = {}
                    db.commit()

        # Route to Fix 3: Smart Seeker Hub / Career Dashboard
        is_recruiter = db.query(Recruiter).filter_by(wa_number=wa_number).first() is not None
        if not is_recruiter:
            has_applied = db.query(CandidateApplication).filter_by(candidate_id=candidate.id).first() is not None
            if not has_applied:
                await seeker_handler.send_seeker_nudge_with_jobs(wa_number, candidate, db)
                return
            else:
                await seeker_handler.send_applied_seeker_dashboard(wa_number, candidate, db)
                return

    # 2. Unregistered Seeker Flow Dropout Interceptor (Fix 1)
    if conv_state and conv_state.state == "seeker_registering":
        flow_sent_at_str = ctx.get("flow_sent_at")
        flow_sent_dt = None
        if flow_sent_at_str:
            try:
                flow_sent_dt = datetime.fromisoformat(flow_sent_at_str)
                if flow_sent_dt.tzinfo is None:
                    flow_sent_dt = flow_sent_dt.replace(tzinfo=timezone.utc)
            except Exception:
                flow_sent_dt = None

        if not flow_sent_dt:
            # Fallback to conv_state.updated_at or last_user_message_at
            flow_sent_dt = conv_state.updated_at or conv_state.last_user_message_at
            if flow_sent_dt and flow_sent_dt.tzinfo is None:
                flow_sent_dt = flow_sent_dt.replace(tzinfo=timezone.utc)

        hours_since = (datetime.now(timezone.utc) - flow_sent_dt).total_seconds() / 3600.0 if flow_sent_dt else 9999.0
        days_since = hours_since / 24.0

        if pending_job_code:
            vacancy = db.query(JobVacancy).filter_by(job_code=pending_job_code).first()
            from app.services.ad_lifecycle import ensure_ad_active
            if vacancy and ensure_ad_active(vacancy, db):
                if hours_since < 24.0:
                    # Within 24 hours: user messaged during active session before 24h
                    # Show standard Help Menu without awkward "Welcome back" recall template
                    await global_handler.send_help_menu(wa_number)
                    return
                elif days_since <= 14.0:
                    # Returning visit: >= 24 hours up to 14 days
                    await seeker_handler.handle_resume_recent(wa_number, vacancy)
                    return
                else:
                    # Stale (>14 days) -> silently clear context and fall through to welcome menu
                    conv_state.state = "idle"
                    conv_state.context = {}
                    db.commit()
            else:
                # Vacancy closed or missing -> silently clear context and fall through to welcome menu
                conv_state.state = "idle"
                conv_state.context = {}
                db.commit()
        else:
            # General profile registration flow (no pending job code)
            if hours_since < 24.0:
                await global_handler.send_help_menu(wa_number)
                return
            else:
                conv_state.state = "idle"
                conv_state.context = {}
                db.commit()

    # 3. Recruiter Registration Flow Dropout: reset state so user is not trapped
    if conv_state and conv_state.state == "recruiter_registering":
        from app.db.models import Recruiter
        recruiter = db.query(Recruiter).filter_by(wa_number=wa_number).first()
        conv_state.state = "recruiter_idle" if recruiter else "idle"
        conv_state.context = {}
        db.commit()

    # Default: personalized routing
    await global_handler.route_unrecognized_message(wa_number, db)



def _generate_magic_url(wa_number: str, role: str, path: str, db: Session, expires_hours: int = 24) -> str:
    """Generate an authenticated single-sign-on magic link URL for the user."""
    import secrets
    from datetime import datetime, timezone, timedelta
    from app.db.models import MagicLink

    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=expires_hours)
    magic = MagicLink(
        token=token,
        wa_number=wa_number,
        role=role,
        expires_at=expires,
        is_used=False,
    )
    db.add(magic)
    db.commit()
    base = "https://jobinfo.pro"
    if path:
        return f"{base}/{path}?magic_token={token}"
    return f"{base}/?magic_token={token}"


async def _handle_login_otp(wa_number: str, otp_code: str, db: Session) -> None:
    """
    Called when a WhatsApp user sends a plain 6-digit message and has a
    pending WebLoginSession. Validates the OTP and replies accordingly.
    """
    from app.whatsapp.client import wa_client
    from app.routers.api import bot_verify_pin, PinBotVerifyRequest

    req = PinBotVerifyRequest(otp_code=otp_code, wa_number=wa_number)
    result = bot_verify_pin(req, db)

    if result.get("success"):
        is_new = result.get("is_new_user")
        role = result.get("role", "seeker")

        if is_new:
            if role == "seeker":
                body_text = (
                    "✅ *OTP verification successful!*\n\n"
                    "Please switch back to your browser to complete your registration, "
                    "or tap *Create My Profile* below to complete it here on WhatsApp."
                )
                buttons = [
                    {"id": "btn_wa_reg_seeker", "title": "Create My Profile"},
                ]
            else:
                body_text = (
                    "✅ *OTP verification successful!*\n\n"
                    "Please switch back to your browser to complete your registration, "
                    "or tap *Create My Profile* below to complete it here on WhatsApp."
                )
                buttons = [
                    {"id": "btn_wa_reg_recruiter", "title": "Create My Profile"},
                ]

            await wa_client.send_buttons(
                to=wa_number,
                body_text=body_text,
                buttons=buttons,
                footer_text="JobInfo.pro • Made for Kerala",
            )
        else:
            if role == "recruiter":
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
                to=wa_number,
                body_text=(
                    "✅ *OTP verification successful!*\n\n"
                    "Please switch back to the website to access your account. 🎉"
                ),
                buttons=buttons,
                footer_text="JobInfo.pro • Made for Kerala",
            )
    else:
        reason = result.get("reason", "unknown")
        if reason == "wrong_otp":
            await wa_client.send_text(
                to=wa_number,
                body=(
                    "❌ *Incorrect OTP.*\n\n"
                    "You have entered a wrong OTP. Please check the OTP shown on the website "
                    "and try again. The OTP expires in 5 minutes."
                ),
            )
        elif reason == "no_pending_session":
            await wa_client.send_text(
                to=wa_number,
                body=(
                    "❌ *OTP expired or not found.*\n\n"
                    "This code is no longer valid. Please visit the website "
                    "and request a new OTP. 🔁"
                ),
            )
        else:
            await wa_client.send_text(
                to=wa_number,
                body="❌ Something went wrong. Please try again from the website.",
            )


async def _handle_button(wa_number: str, button_id: str, db: Session) -> None:
    """Route a quick-reply button press."""
    from app.handlers import recruiter as recruiter_handler
    from app.handlers import seeker as seeker_handler
    from app.whatsapp.client import wa_client

    # ── Global menu buttons ─────────────────────────────────────────────────
    handled = await global_handler.handle_global_button(wa_number, button_id, db)
    if handled:
        return

    # ── Login / OTP Verification Action Buttons ──────────────────────────────
    if button_id == "btn_wa_reg_seeker":
        await seeker_handler.handle_my_applications_menu(wa_number, db)
        return

    if button_id == "btn_wa_reg_recruiter":
        await recruiter_handler.start(wa_number, db)
        return

    if button_id == "btn_my_dashboard":
        from app.db.models import Recruiter
        is_recruiter = db.query(Recruiter).filter_by(wa_number=wa_number).first() is not None
        role = "recruiter" if is_recruiter else "seeker"
        path = "recruiter-dashboard.html" if is_recruiter else "dashboard.html"
        url = _generate_magic_url(wa_number, role, path, db)

        if is_recruiter:
            body_text = (
                "🎉 *Access Your Dashboard*\n\n"
                "Welcome! Your web session is active and ready.\n\n"
                "Manage your hiring, track applicant responses, download candidate CVs and schedule interviews with full desktop & mobile convenience.\n\n"
                "Tap below to enter your workspace 👇"
            )
        else:
            body_text = (
                "🎉 *Access Your Dashboard*\n\n"
                "Welcome! Your web session is active and ready.\n\n"
                "Track your job applications, view status updates, and manage your profile with full desktop & mobile convenience.\n\n"
                "Tap below to enter your career portal 👇"
            )

        await wa_client.send_cta_url(
            to=wa_number,
            body_text=body_text,
            button_text=" Open Dashboard",
            url=url,
            footer_text="⏳ Button expires in 24h",
        )
        return

    if button_id in ("btn_continue_web", "btn_complete_registration", "btn_complete_profile"):
        await wa_client.send_text(
            to=wa_number,
            body=(
                "🌐 *Session Active!*\n\n"
                "Please switch back to your browser window on the website to continue. 🎉"
            ),
        )
        return

    # ── Recruiter buttons ───────────────────────────────────────────────────
    if button_id == "btn_post_vacancy":
        await recruiter_handler.handle_post_vacancy_button(wa_number, db, from_workspace=True)
        return

    if button_id == "btn_my_vacancies":
        await recruiter_handler.handle_my_vacancies_button(wa_number, db)
        return

    # ── Seeker main menu buttons ──────────────────────────────────────────────
    if button_id == "ACTION_SUGGEST_JOBS":
        await seeker_handler.handle_suggest_jobs(wa_number, db)
        return

    if button_id in ("ACTION_EXPLORE_JOBS", "btn_whatsapp_channel", "btn_channel", "btn_explore_jobs"):
        await seeker_handler.handle_explore_jobs(wa_number)
        return

    if button_id == "ACTION_MY_APPLICATIONS":
        await seeker_handler.handle_my_applications_menu(wa_number, db)
        return

    # ── Seeker buttons ──────────────────────────────────────────────────────
    if button_id == "btn_gethelp":
        await seeker_handler.handle_gethelp_button(wa_number, db)
        return

    if button_id == "btn_view_applications":
        await seeker_handler.handle_view_applications_button(wa_number, db)
        return

    # "btn_register_JC:1002"
    if button_id.startswith("btn_register_"):
        job_code = button_id.removeprefix("btn_register_")
        await seeker_handler.handle_register_button(wa_number, job_code, db)
        return

    # "btn_apply_now_42" — route through Smart Interceptor
    if button_id.startswith("btn_apply_now_"):
        vacancy_id = int(button_id.removeprefix("btn_apply_now_"))
        vacancy = db.query(JobVacancy).filter_by(id=vacancy_id).first()
        from app.services.ad_lifecycle import ensure_ad_active
        if not vacancy or not ensure_ad_active(vacancy, db):
            await wa_client.send_buttons(
                to=wa_number,
                header_text="Position No Longer Available",
                body_text=(
                    "Sorry, this position is no longer accepting applications.\n"
                    "The role may have been filled, or the ad has been removed.\n\n"
                    "Browse latest open roles on the JobInfo channel for fresh opportunities!"
                ),
                buttons=[
                    {"id": "ACTION_SUGGEST_JOBS", "title": "Suggest Jobs"},
                    {"id": "ACTION_EXPLORE_JOBS", "title": "Explore Channel"},
                ],
            )
            return

        candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
        if not candidate:
            await seeker_handler.start(wa_number, vacancy.job_code, db)
            return
        await seeker_handler._show_job_apply_prompt(wa_number, candidate, vacancy, db)
        return

    # "btn_update_cv_42"
    if button_id.startswith("btn_update_cv_"):
        vacancy_id = int(button_id.removeprefix("btn_update_cv_"))
        await seeker_handler.handle_update_cv_button(wa_number, vacancy_id, db)
        return

    # "CONFIRM_APPLY_JC:1002" — user chose "Apply Anyway" from mismatch warning
    if button_id.startswith("CONFIRM_APPLY_"):
        job_code = button_id.removeprefix("CONFIRM_APPLY_")
        await seeker_handler.handle_confirm_apply_button(wa_number, job_code, db)
        return

    # "MANAGE_CV_JC:1002" — user chose to update/select CV
    if button_id.startswith("MANAGE_CV_"):
        job_code = button_id.removeprefix("MANAGE_CV_")
        await seeker_handler.handle_manage_cv(wa_number, job_code, db)
        return

    # "APPLY_NO_CV_JC:1002" — apply explicitly without CV
    if button_id.startswith("APPLY_NO_CV_"):
        job_code = button_id.removeprefix("APPLY_NO_CV_")
        await seeker_handler.handle_apply_no_cv(wa_number, job_code, db)
        return

    # "UPLOAD_NEW_CV_JC:1002" — upload a new CV (button variant)
    if button_id.startswith("UPLOAD_NEW_CV_"):
        job_code = button_id.removeprefix("UPLOAD_NEW_CV_")
        await seeker_handler.handle_upload_new_cv(wa_number, job_code, db)
        return

    # "USE_MATCHING_CV_5_JC:1002" — user chose to apply with matching saved CV
    if button_id.startswith("USE_MATCHING_CV_"):
        parts = button_id.removeprefix("USE_MATCHING_CV_").split("_", 1)
        if len(parts) == 2:
            try:
                resume_id = int(parts[0])
                job_code = parts[1]
                await seeker_handler.handle_select_cv(wa_number, resume_id, job_code, db)
                return
            except ValueError:
                logger.error("Failed to parse resume_id from button '%s' for %s", button_id, wa_number)
                await wa_client.send_text(to=wa_number, body="❌ Something went wrong processing your CV selection. Please try again.")
                return
        logger.warning("Malformed USE_MATCHING_CV button '%s' for %s", button_id, wa_number)
        await wa_client.send_text(to=wa_number, body="❌ Something went wrong processing your CV selection. Please try again.")
        return

    if button_id == "SUGGEST_JOBS_NO_CV":
        await seeker_handler.handle_suggest_jobs_no_cv(wa_number, db)
        return

    if button_id == "SUGGEST_JOBS_NEAR_ME":
        await seeker_handler.handle_suggest_jobs_near_me(wa_number, db)
        return

    if button_id in ("btn_suggest_more_jobs", "SUGGEST_WEIGHTED_JOBS"):
        await seeker_handler.handle_suggest_weighted_jobs(wa_number, db)
        return

    if button_id.startswith("btn_suggest_other_"):
        try:
            exclude_id = int(button_id.removeprefix("btn_suggest_other_"))
        except ValueError:
            exclude_id = None
        await seeker_handler.handle_suggest_weighted_jobs(wa_number, db, exclude_vacancy_id=exclude_id)
        return

    # ── Plan A Button Handlers ──────────────────────────────────────────────
    if button_id.startswith("btn_resume_apply_"):
        job_code = button_id.removeprefix("btn_resume_apply_")
        await seeker_handler.start(wa_number, job_code, db)
        return

    if button_id == "btn_explore_website":
        await seeker_handler.handle_explore_website_cta(wa_number)
        return

    if button_id == "btn_not_interested_unreg":
        await seeker_handler.handle_not_interested_unregistered(wa_number, db)
        return

    if button_id == "btn_not_interested_reg":
        await seeker_handler.handle_not_interested_registered(wa_number, db)
        return

    if button_id.startswith("btn_apply_instantly_"):
        job_code = button_id.removeprefix("btn_apply_instantly_")
        await seeker_handler.handle_apply_instantly_button(wa_number, job_code, db)
        return

    if button_id.startswith("btn_apply_rescue_"):
        job_code = button_id.removeprefix("btn_apply_rescue_")
        await seeker_handler.handle_apply_rescue_button(wa_number, job_code, db)
        return

    if button_id.startswith("view_job_"):
        job_code = button_id.removeprefix("view_job_")
        await seeker_handler.handle_view_job_card(wa_number, job_code, db)
        return

    if button_id == "btn_fresh_openings":
        candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
        if candidate:
            await seeker_handler.handle_fresh_openings(wa_number, candidate, db)
        return

    if button_id == "btn_my_profile":
        candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
        if candidate:
            await seeker_handler.handle_my_profile_button(wa_number, candidate)
        return

    if button_id == "btn_create_profile":
        await seeker_handler.handle_create_general_profile(wa_number, db)
        return

    logger.warning("Unhandled button_id '%s' from %s", button_id, wa_number)


async def _handle_list_reply(wa_number: str, row_id: str, db: Session) -> None:
    """Route a list (interactive menu) selection."""
    from app.handlers import seeker as seeker_handler

    # "plan_free_trial", "plan_basic", etc.
    if row_id.startswith("plan_"):
        plan_name = row_id.removeprefix("plan_")
        await seeker_handler.handle_plan_selection(wa_number, plan_name, db)
        return

    # "SELECT_CV_5_JC:1002" — user picked an existing CV from the list
    if row_id.startswith("SELECT_CV_"):
        # Format: SELECT_CV_{resume_id}_{job_code}
        parts = row_id.removeprefix("SELECT_CV_").split("_", 1)
        if len(parts) == 2:
            resume_id = int(parts[0])
            job_code = parts[1]
            await seeker_handler.handle_select_cv(wa_number, resume_id, job_code, db)
            return

    # "UPLOAD_NEW_CV_JC:1002" — user wants to upload a new CV
    if row_id.startswith("UPLOAD_NEW_CV_"):
        job_code = row_id.removeprefix("UPLOAD_NEW_CV_")
        await seeker_handler.handle_upload_new_cv(wa_number, job_code, db)
        return

    logger.warning("Unhandled list row_id '%s' from %s", row_id, wa_number)


async def _handle_flow_reply(wa_number: str, flow_data: dict, db: Session) -> None:
    """
    Route WhatsApp Flow completion callbacks safely and robustly (MINOR-2).
    Disambiguation priority:
      1. Explicit flow identifiers (flow_token, flow_name, form_id, flow_id)
      2. Active ConversationState in the database
      3. Mutually exclusive, robust key heuristics fallback
    """
    import json
    from app.config import get_settings
    settings = get_settings()
    from app.handlers import recruiter as recruiter_handler
    from app.handlers import seeker as seeker_handler

    raw_json = flow_data.get("response_json", "{}")
    try:
        submitted: dict = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
    except json.JSONDecodeError:
        submitted = {}

    # 1. Explicit flow tokens or form identifiers
    flow_token = str(
        submitted.get("flow_token")
        or flow_data.get("flow_token")
        or submitted.get("flow_name")
        or submitted.get("form_id")
        or submitted.get("flow_id")
        or ""
    ).strip().lower()

    if flow_token in ("post_vacancy", "post_job") or (settings.FLOW_ID_POST_VACANCY and flow_token == settings.FLOW_ID_POST_VACANCY.lower()):
        await recruiter_handler.handle_post_vacancy_flow_completion(wa_number, submitted, db)
        return

    if flow_token in ("recruiter_register", "recruiter_registration") or (settings.FLOW_ID_RECRUITER_REGISTER and flow_token == settings.FLOW_ID_RECRUITER_REGISTER.lower()):
        await recruiter_handler.handle_registration_flow_completion(wa_number, submitted, db)
        return

    if flow_token in ("seeker_register", "seeker_registration") or (settings.FLOW_ID_SEEKER_REGISTER and flow_token == settings.FLOW_ID_SEEKER_REGISTER.lower()):
        await seeker_handler.handle_registration_flow_completion(wa_number, submitted, db)
        return

    if flow_token in ("cv_update", "upload_cv", "cv_upload") or (settings.FLOW_ID_CV_UPDATE and flow_token == settings.FLOW_ID_CV_UPDATE.lower()):
        await seeker_handler.handle_cv_update_flow_completion(wa_number, submitted, db)
        return

    # 2. Inspect submitted data keys directly (ground truth)
    if ("job_title" in submitted and "job_category" in submitted) or ("job_description" in submitted and "job_title" in submitted):
        # Post Vacancy Flow
        await recruiter_handler.handle_post_vacancy_flow_completion(wa_number, submitted, db)
        return

    elif "company_name" in submitted and "business_type" in submitted:
        # Recruiter Registration Flow
        await recruiter_handler.handle_registration_flow_completion(wa_number, submitted, db)
        return

    elif "new_cv_category" in submitted or (
        "media_id" in submitted and "sub_category" not in submitted and "district" not in submitted and "name" not in submitted
    ):
        # CV Update Flow
        await seeker_handler.handle_cv_update_flow_completion(wa_number, submitted, db)
        return

    elif ("category" in submitted and "sub_category" in submitted) or (
        "name" in submitted and ("district" in submitted or "age" in submitted or "gender" in submitted)
    ):
        # Seeker Registration Flow
        await seeker_handler.handle_registration_flow_completion(wa_number, submitted, db)
        return

    # 3. State-driven tiebreaker only if payload keys were completely unrecognized
    conv_state = db.query(ConversationState).filter_by(wa_number=wa_number).first()
    active_state = conv_state.state if conv_state else ""

    if active_state == "recruiter_posting_vacancy":
        await recruiter_handler.handle_post_vacancy_flow_completion(wa_number, submitted, db)
    elif active_state == "recruiter_registering":
        await recruiter_handler.handle_registration_flow_completion(wa_number, submitted, db)
    elif active_state in ("seeker_uploading_cv", "seeker_updating_cv"):
        await seeker_handler.handle_cv_update_flow_completion(wa_number, submitted, db)
    elif active_state == "seeker_registering":
        await seeker_handler.handle_registration_flow_completion(wa_number, submitted, db)
    else:
        logger.warning(
            "Could not identify flow from payload: %s from %s (state=%s)",
            submitted, wa_number, active_state
        )

async def _handle_document(wa_number: str, doc: dict, db: Session) -> None:
    """Handle a raw document upload (CV sent directly in chat)."""
    from app.handlers import seeker as seeker_handler

    state_rec = db.query(ConversationState).filter_by(wa_number=wa_number).first()
    if state_rec and state_rec.state in ("seeker_updating_cv", "seeker_uploading_cv"):
        from app.services.storage import MAX_CV_SIZE_BYTES, save_cv_from_whatsapp
        from app.whatsapp.client import wa_client
        doc_size = doc.get("file_size", 0)
        if doc_size and int(doc_size) > MAX_CV_SIZE_BYTES:
            await wa_client.send_text(
                to=wa_number,
                body=(
                    f"❌ Your CV file size ({int(doc_size) // 1024} KB) exceeds the *1MB* limit.\n\n"
                    "Please compress your CV (e.g. using a free tool like smallpdf.com or ilovepdf.com) and send it again."
                ),
            )
            return

        original_doc_name = doc.get("filename")
        cv_path, filename = await save_cv_from_whatsapp(
            wa_number=wa_number,
            media_id=doc.get("id", ""),
            mime_type=doc.get("mime_type", "application/pdf"),
            original_filename=original_doc_name,
        )
        if cv_path:
            from app.db.models import Candidate, CandidateResume
            from app.whatsapp.templates import cv_update_confirmation_body

            candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
            if candidate:
                candidate.cv_path = cv_path
                candidate.cv_updates_used = (candidate.cv_updates_used or 0) + 1
                existing_default = db.query(CandidateResume).filter_by(candidate_id=candidate.id, is_default=True).first()
                if existing_default:
                    existing_default.media_id = cv_path
                    existing_default.file_name = filename
                else:
                    new_res = CandidateResume(
                        candidate_id=candidate.id,
                        media_id=cv_path,
                        file_name=filename,
                        category_tag=candidate.category or "other",
                        is_default=True,
                    )
                    db.add(new_res)
                db.commit()

                if state_rec and state_rec.state == "seeker_uploading_cv":
                    # Apply flow: CV just uploaded — auto-submit the application now
                    job_code = (state_rec.context or {}).get("job_code")
                    vacancy = db.query(JobVacancy).filter_by(job_code=job_code).first() if job_code else None
                    if vacancy:
                        state_rec.state = "idle"
                        state_rec.context = {}
                        db.commit()
                        await seeker_handler.handle_apply_now_button(
                            wa_number, vacancy.id, db, bypass_cv_gate=False
                        )
                    else:
                        # Job code missing/vacancy gone — just confirm CV saved
                        await wa_client.send_text(
                            to=wa_number,
                            body=cv_update_confirmation_body(candidate),
                        )
                else:
                    # General CV update flow — just show confirmation
                    await wa_client.send_text(
                        to=wa_number,
                        body=cv_update_confirmation_body(candidate),
                    )

        else:
            await wa_client.send_text(
                to=wa_number,
                body=(
                    "❌ Could not accept this CV.\n\n"
                    "• Maximum allowed file size: *1MB*\n"
                    "• Allowed formats: *PDF, Word (.doc, .docx)*\n\n"
                    "Please compress your document and try again."
                ),
            )
    else:
        from app.whatsapp.client import wa_client
        await wa_client.send_text(
            to=wa_number,
            body="📎 Got your file! To update your CV, please tap an apply link first.",
        )


async def _handle_unsupported_media(wa_number: str, msg_type: str, db: Session) -> None:
    """
    Polite guidance notice when a user sends unsupported media (audio, video, sticker, or image).
    Includes rate-limiting (30s cooldown) to prevent spam loops if multiple stickers are sent.
    """
    from app.db.models import ConversationState, Recruiter, Candidate
    from app.whatsapp.client import wa_client

    state_rec = db.query(ConversationState).filter_by(wa_number=wa_number).first()
    now_ts = datetime.now(timezone.utc).timestamp()

    # Rate limiting: if a media notice was sent in the last 30 seconds, don't repeat
    ctx = dict(state_rec.context or {}) if state_rec else {}
    last_notice = ctx.get("last_unsupported_media_ts", 0)
    if (now_ts - last_notice) < 30:
        logger.info("Suppressing duplicate unsupported media notice for %s (cooldown active)", wa_number)
        return

    # Check special case: candidate sending a photo of a CV
    if msg_type == "image" and state_rec and state_rec.state == "seeker_updating_cv":
        ctx["last_unsupported_media_ts"] = now_ts
        if state_rec:
            state_rec.context = ctx
            db.commit()
        await wa_client.send_text(
            to=wa_number,
            body=(
                "📄 *PDF or Document Format Required*\n\n"
                "Please send your CV as a *PDF or Word document* (.pdf, .docx).\n\n"
                "Photos and image formats cannot be verified by employers."
            ),
        )
        return

    # Update cooldown timestamp
    ctx["last_unsupported_media_ts"] = now_ts
    if state_rec:
        state_rec.context = ctx
        db.commit()

    is_recruiter = db.query(Recruiter).filter_by(wa_number=wa_number).first() is not None
    is_seeker = db.query(Candidate).filter_by(wa_number=wa_number).first() is not None

    if is_recruiter and not is_seeker:
        body_text = (
            "🤖 *Automated Assistant*\n\n"
            "👋 I am an automated assistant and can only read text messages.\n\n"
            "If you need direct assistance or wish to speak with our team, you can message our Admin on WhatsApp:\n"
            "💬 *+91 70259 62179*\n\n"
            "Whenever you're ready to manage your hiring or post vacancies, tap below 👇"
        )
        buttons = [
            {"id": "menu_recruiter", "title": "🏢 My Workspace"},
            {"id": "help_support", "title": "ℹ️ Help & Support"},
        ]
    elif is_seeker and not is_recruiter:
        body_text = (
            "🤖 *Automated Assistant*\n\n"
            "👋 I am an automated assistant and can only read text messages or PDF documents.\n\n"
            "If you need direct assistance or wish to speak with our team, you can message our Admin on WhatsApp:\n"
            "💬 *+91 70259 62179*\n\n"
            "Whenever you're ready to explore jobs or view your applications, tap below 👇"
        )
        buttons = [
            {"id": "menu_seeker", "title": "💼 Job Menu"},
            {"id": "help_support", "title": "ℹ️ Help & Support"},
        ]
    else:
        body_text = (
            "🤖 *Automated Assistant*\n\n"
            "Welcome to JobInfo Kerala! 👋 \nI can only read text messages and action buttons.\n\n"
            "If you need direct assistance or wish to speak with our team, you can message our team on WhatsApp:\n"
            "💬 *+91 70259 62179*\n\n"
            "How can I help you today? 👇"
        )
        buttons = [
            {"id": "menu_seeker", "title": "💼 I Need a Job"},
            {"id": "menu_recruiter", "title": "🏢 I am Hiring"},
            {"id": "help_support", "title": "ℹ️ Help & Support"},
        ]

    await wa_client.send_buttons(
        to=wa_number,
        body_text=body_text,
        buttons=buttons,
        footer_text="JobInfo.pro • Made for Kerala",
    )


async def candidate_handler_renew(wa_number: str, db: Session) -> None:
    """Handle RENEW keyword – send plan selection list."""
    from app.handlers.seeker import _send_plan_selection
    await _send_plan_selection(wa_number, db)


async def send_delayed_session_menu(wa_number: str) -> None:
    """
    Waits 5 minutes, validates debounce,
    spins up an independent DB session, and dispatches the correct 'Session Closing'
    button menu based on their profile combinations.
    """
    import asyncio
    import logging
    from datetime import datetime, timezone
    from app.db.base import SessionLocal
    from app.db.models import ConversationState, Recruiter, Candidate, JobVacancy, CandidateResume
    from app.whatsapp.client import wa_client

    logger = logging.getLogger(__name__)

    await asyncio.sleep(300)

    # ── Phase 1: Check at 5 minutes ─────────────────────────────────────────
    needs_phase_2 = False
    saved_context: dict = {}

    db1 = SessionLocal()
    try:
        state = db1.query(ConversationState).filter_by(wa_number=wa_number).first()
        if not state or not state.last_user_message_at:
            return

        last_msg = state.last_user_message_at
        if last_msg.tzinfo is None:
            last_msg = last_msg.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        time_since_msg = (now - last_msg).total_seconds()

        # Debounce: user interacted within 5 min (< 300s) OR 24h customer window closed (>= 86400s)
        if time_since_msg < 300 or time_since_msg >= 86400:
            return

        # ── Check for Recruiter with Pending Vacancies ──────────────────
        is_recruiter = db1.query(Recruiter).filter_by(wa_number=wa_number).first()
        if is_recruiter:
            has_pending = (
                db1.query(JobVacancy)
                .filter_by(recruiter_id=is_recruiter.id, status="pending")
                .first()
                is not None
            )
            if has_pending:
                # Recruiter is waiting for admin verification – do not send session closing menu!
                return

        # ── Fix 4: CV Upload In Progress Check (Unified 5+5 Min Pipeline) ──
        if state.state in ("seeker_uploading_cv", "seeker_no_cv"):
            candidate = db1.query(Candidate).filter_by(wa_number=wa_number).first()
            # Early Exit: If CV uploaded or resume registered within 5 min, stop immediately
            if candidate:
                resume_count = db1.query(CandidateResume).filter_by(candidate_id=candidate.id).count()
                if bool(candidate.cv_path) or resume_count > 0:
                    return

            # Still stuck without a CV: Suppress generic menu and proceed to Phase 2 after sleep
            saved_context = dict(state.context or {})
            needs_phase_2 = True

        else:
            is_seeker = db1.query(Candidate).filter_by(wa_number=wa_number).first()

        # Condition C: Both Roles
        if is_recruiter and is_seeker and is_seeker.registration_complete:
            text = (
                "⏳ *Session Paused*\n\n"
                "Thank you for using JobInfo! 🤝\nIt looks like you stepped away.\n\n"
                "Whether you're looking to hire great talent or find your next job, "
                "you can jump right back in anytime by clicking below 👇"
            )
            await wa_client.send_buttons(
                to=wa_number,
                body_text=text,
                buttons=[
                    {"id": "menu_seeker", "title": "Start as Seeker"},
                    {"id": "menu_recruiter", "title": "Start as Recruiter"}
                ]
            )

        # Condition A: Recruiter Only
        elif is_recruiter:
            text = (
                "⏳ *Session Paused*\n\n"
                "Thank you for using JobInfo! 🤝\nIt looks like you stepped away.\n\n"
                "Whenever you're ready to review job applications or post a new vacancy, "
                "you can jump right back in anytime by clicking below 👇"
            )
            await wa_client.send_buttons(
                to=wa_number,
                body_text=text,
                buttons=[
                    {"id": "menu_recruiter", "title": "Get Started"}
                ]
            )

        # Condition B: Seeker Only
        elif is_seeker and is_seeker.registration_complete:
            text = (
                "⏳ *Session Paused*\n\n"
                "Thank you for using JobInfo! 🤝\nIt looks like you stepped away.\n\n"
                "Whenever you're ready to track your current applications or discover fresh job openings, "
                "you can jump right back in anytime by clicking below 👇"
            )
            await wa_client.send_buttons(
                to=wa_number,
                body_text=text,
                buttons=[
                    {"id": "menu_seeker", "title": "Get Started"}
                ]
            )

        # Condition D: Unregistered / None
        else:
            text = (
                "⏳ *Session Paused*\n\n"
                "We noticed you haven't set up your profile yet. It only takes a minute to get started and unlock Kerala's best job network.\n\n"
                "👇 *What brings you here today?*\n"
                "Please choose an option below to proceed.\n\n"
                "👉 _Tip: Follow our official channel for daily job alerts!_\n"
                "🔗 https://whatsapp.com/channel/0029VbBrkDB8fewxd9QIMA2k"
            )
            await wa_client.send_buttons(
                to=wa_number,
                body_text=text,
                buttons=[
                    {"id": "menu_seeker", "title": "🔍 Looking for Job"},
                    {"id": "menu_recruiter", "title": "📢 I am Hiring"},
                ]
            )

    except Exception as e:
        logger.error("Error in send_delayed_session_menu (phase 1): %s", e)
        return
    finally:
        db1.close()

    # ── Phase 2: At 10 Minutes (600s total, only for stuck CV upload) ──────
    if not needs_phase_2:
        return

    await asyncio.sleep(300)

    db2 = SessionLocal()
    try:
        state = db2.query(ConversationState).filter_by(wa_number=wa_number).first()
        if not state or state.state not in ("seeker_uploading_cv", "seeker_no_cv"):
            return  # State changed or application completed

        last_msg_10 = state.last_user_message_at
        if last_msg_10 and last_msg_10.tzinfo is None:
            last_msg_10 = last_msg_10.replace(tzinfo=timezone.utc)
        if last_msg_10 and (datetime.now(timezone.utc) - last_msg_10).total_seconds() < 600:
            return  # User interacted between min 5 and 10

        candidate = db2.query(Candidate).filter_by(wa_number=wa_number).first()
        if candidate:
            resume_count = db2.query(CandidateResume).filter_by(candidate_id=candidate.id).count()
            if not (bool(candidate.cv_path) or resume_count > 0):
                from app.handlers import seeker as seeker_handler
                await seeker_handler.send_cv_rescue_card(wa_number, candidate, state.context or saved_context, db2)
    except Exception as e:
        logger.error("Error in send_delayed_session_menu (phase 2): %s", e)
    finally:
        db2.close()


async def send_post_approval_session_menu(wa_number: str, approved_vacancy_id: int) -> None:
    """
    Called after admin approves a vacancy and sends the approval messages.
    Waits 5 minutes (300s), validates debounce across multiple vacancies,
    verifies no other vacancy is still pending, and ensures the recruiter is
    strictly within Meta's 24-hour customer care window (<86400s) before sending
    the session follow-up menu.

    If the 24-hour window has expired, the message is dropped (NEVER sent or queued).
    """
    import asyncio
    import logging
    from datetime import datetime, timezone
    from app.db.base import SessionLocal
    from app.db.models import ConversationState, Recruiter, Candidate, JobVacancy
    from app.whatsapp.client import wa_client

    logger = logging.getLogger(__name__)

    await asyncio.sleep(300)

    db = SessionLocal()
    try:
        recruiter = db.query(Recruiter).filter_by(wa_number=wa_number).first()
        if not recruiter:
            return

        # 1. Multi-vacancy check: Is ANY other vacancy still pending review?
        has_pending = (
            db.query(JobVacancy)
            .filter_by(recruiter_id=recruiter.id, status="pending")
            .first()
            is not None
        )
        if has_pending:
            logger.info("Suppressing post-approval session menu for %s: recruiter has other pending vacancies.", wa_number)
            return

        # 2. Multi-vacancy debounce: Was another vacancy approved more recently?
        latest_approved = (
            db.query(JobVacancy)
            .filter_by(recruiter_id=recruiter.id, status="approved")
            .order_by(JobVacancy.approved_at.desc())
            .first()
        )
        if latest_approved and latest_approved.approved_at and latest_approved.id != approved_vacancy_id:
            latest_at = latest_approved.approved_at
            if latest_at.tzinfo is None:
                latest_at = latest_at.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            if (now - latest_at).total_seconds() < 290:
                # A newer vacancy was approved; that task will handle the 5-min follow-up.
                return

        # 3. Check conversation state & user activity
        state = db.query(ConversationState).filter_by(wa_number=wa_number).first()
        if not state or not state.last_user_message_at:
            return

        last_user_msg = state.last_user_message_at
        if last_user_msg.tzinfo is None:
            last_user_msg = last_user_msg.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        time_since_user_msg = (now - last_user_msg).total_seconds()

        # If user interacted during the 5 minutes (< 300s), debounce (active user)
        if time_since_user_msg < 300:
            return

        # 4. Strict 24-Hour WhatsApp Service Window Constraint:
        # If 24h window has closed (>= 86400s), NEVER send and NEVER queue!
        if time_since_user_msg >= 86400:
            logger.info(
                "Suppressing post-approval session menu for %s: 24h window expired (%ss elapsed).",
                wa_number,
                int(time_since_user_msg),
            )
            return

        # 5. Dispatch the appropriate session menu
        is_seeker = db.query(Candidate).filter_by(wa_number=wa_number).first()
        if is_seeker and is_seeker.registration_complete:
            text = (
                "⏳ *Session Paused*\n\n"
                "Thank you for using JobInfo! 🤝\nIt looks like you stepped away.\n\n"
                "Whether you're looking to hire great talent or find your next job, "
                "you can jump right back in anytime by clicking below 👇\n\n"
                "👉 _Tip: Follow our official channel for daily job alerts!_\n"
                "🔗 https://whatsapp.com/channel/0029VbBrkDB8fewxd9QIMA2k"
            )
            await wa_client.send_buttons(
                to=wa_number,
                body_text=text,
                buttons=[
                    {"id": "menu_seeker", "title": "Start as Seeker"},
                    {"id": "menu_recruiter", "title": "Start as Recruiter"},
                ],
            )
        else:
            text = (
                "⏳ *Session Paused*\n\n"
                "Thank you for using JobInfo! 🤝\nIt looks like you stepped away.\n\n"
                "Whenever you're ready to review job applications or post a new vacancy, "
                "you can jump right back in anytime by clicking below 👇"
            )
            await wa_client.send_buttons(
                to=wa_number,
                body_text=text,
                buttons=[
                    {"id": "menu_recruiter", "title": "Get Started"},
                ],
            )

    except Exception as e:
        logger.error(f"Error in send_post_approval_session_menu: {e}")
    finally:
        db.close()

