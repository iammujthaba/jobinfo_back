"""
Central dispatcher for incoming WhatsApp messages.
This module replaces N8N's routing logic.

It parses the raw Meta webhook payload, extracts the relevant event
(text message, button reply, flow completion, etc.) and calls the
appropriate handler.
"""
import logging
import re

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import Candidate, JobVacancy

from app.db.models import ConversationState
from app.handlers import global_handler

logger = logging.getLogger(__name__)


from fastapi import BackgroundTasks

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

        # ── Status updates (read receipts, delivered, etc.) – skip ──────────
        elif "statuses" in value:
            status = value["statuses"][0]
            logger.debug("Status update: %s for msg %s", status.get("status"), status.get("id"))

    except (KeyError, IndexError) as exc:
        logger.warning("Unexpected payload structure: %s | %s", exc, payload)


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
        from app.db.models import AdminNotificationQueue, JobVacancy
        from datetime import datetime, timedelta, timezone
        from app.whatsapp.templates import admin_vacancy_alert_body, job_alert_text_body
        from app.handlers.recruiter import _generate_admin_magic_url
        from app.whatsapp.client import wa_client
        from app.config import get_settings

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

    # Recruiter entry point
    if normalized == "my vacancy" or normalized == "my vacancies":
        await recruiter_handler.start(wa_number, db)
        return

    # Seeker apply link text (e.g. "Apply JC:1002")
    job_code = parse_job_code(text)
    if job_code:
        await seeker_handler.start(wa_number, job_code, db)
        return

    # RENEW keyword
    if normalized == "renew":
        candidate_handler_renew(wa_number, db)
        return

    # Default: personalized routing
    await global_handler.route_unrecognized_message(wa_number, db)


def _generate_magic_url(wa_number: str, role: str, path: str, db: Session) -> str:
    """Generate an authenticated single-sign-on magic link URL for the user."""
    import secrets
    from datetime import datetime, timezone, timedelta
    from app.db.models import MagicLink

    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=24)
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
                footer_text="Powered by JobInfo.pro",
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
                footer_text="Powered by JobInfo.pro",
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
        await wa_client.send_cta_url(
            to=wa_number,
            body_text=(
                "🎉 *Open Your Dashboard*\n\n"
                "Tap the button below to open your dashboard directly in your browser."
            ),
            button_text="My Dashboard",
            url=url,
            footer_text="Link valid for 24 hours",
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
        await recruiter_handler.handle_post_vacancy_button(wa_number, db)
        return

    if button_id == "btn_my_vacancies":
        await recruiter_handler.handle_my_vacancies_button(wa_number, db)
        return

    # ── Seeker main menu buttons ──────────────────────────────────────────────
    if button_id == "ACTION_SUGGEST_JOBS":
        await seeker_handler.handle_suggest_jobs(wa_number, db)
        return

    if button_id == "ACTION_EXPLORE_JOBS":
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
        if not vacancy:
            return
        candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
        if not candidate:
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
    Route WhatsApp Flow completion callbacks by inspecting the payload keys.
    """
    import json
    from app.handlers import recruiter as recruiter_handler
    from app.handlers import seeker as seeker_handler

    raw_json = flow_data.get("response_json", "{}")
    try:
        submitted: dict = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
    except json.JSONDecodeError:
        submitted = {}

    # Inspect the submitted data keys to determine which form was filled out
    if "job_title" in submitted and "job_category" in submitted:
        # Post Vacancy Flow
        await recruiter_handler.handle_post_vacancy_flow_completion(wa_number, submitted, db)

    elif "category" in submitted and "sub_category" in submitted:
        # Seeker Registration Flow
        await seeker_handler.handle_registration_flow_completion(wa_number, submitted, db)

    elif "new_cv_category" in submitted:
        # CV Update Flow (with category tag + job_code for Smart CV Manager)
        await seeker_handler.handle_cv_update_flow_completion(wa_number, submitted, db)

    elif "media_id" in submitted and "category" not in submitted:
        # Legacy CV Update Flow (without category)
        await seeker_handler.handle_cv_update_flow_completion(wa_number, submitted, db)

    elif "company_name" in submitted and "business_type" in submitted:
        # Recruiter Registration Flow
        await recruiter_handler.handle_registration_flow_completion(wa_number, submitted, db)

    else:
        logger.warning("Could not identify flow from payload: %s from %s", submitted, wa_number)

async def _handle_document(wa_number: str, doc: dict, db: Session) -> None:
    """Handle a raw document upload (CV sent directly in chat)."""
    from app.handlers import seeker as seeker_handler

    state_rec = db.query(ConversationState).filter_by(wa_number=wa_number).first()
    if state_rec and state_rec.state == "seeker_updating_cv":
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


def candidate_handler_renew(wa_number: str, db: Session) -> None:
    """Placeholder: handle RENEW keyword – send plan selection list."""
    import asyncio
    from app.handlers.seeker import _send_plan_selection
    asyncio.create_task(_send_plan_selection(wa_number, db))


async def send_delayed_session_menu(wa_number: str) -> None:
    """
    Waits 5 minutes, validates debounce,
    spins up an independent DB session, and dispatches the correct 'Session Closing'
    button menu based on their profile combinations.
    """
    import asyncio
    from datetime import datetime, timezone
    from app.db.base import SessionLocal
    from app.db.models import ConversationState, Recruiter, Candidate
    from app.whatsapp.client import wa_client

    await asyncio.sleep(300)
    
    db = SessionLocal()
    try:
        state = db.query(ConversationState).filter_by(wa_number=wa_number).first()
        if not state or not state.last_user_message_at:
            return
            
        last_msg = state.last_user_message_at
        if last_msg.tzinfo is None:
            last_msg = last_msg.replace(tzinfo=timezone.utc)
            
        if (datetime.now(timezone.utc) - last_msg).total_seconds() < 300:
            # User sent another message during the 5min wait, debounce.
            return
            
        is_recruiter = db.query(Recruiter).filter_by(wa_number=wa_number).first()
        is_seeker = db.query(Candidate).filter_by(wa_number=wa_number).first()
        
        # Condition C: Both Roles
        if is_recruiter and is_seeker and is_seeker.registration_complete:
            text = (
                "👋 *Welcome back to JobInfo!*\n\n"
                "Thank you for using Jobinfo! 🤝 It looks like your session was paused.\n\n"
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
                "👋 *Welcome back to JobInfo!*\n\n"
                "Thank you for using Jobinfo! 🤝 It looks like your session was paused.\n\n"
                "Whenever you're ready to review job applications or post a new vacancy, "
                "you can jump right back in anytime by clicking below 👇"
            )
            await wa_client.send_buttons(
                to=wa_number,
                body_text=text,
                buttons=[
                    {"id": "menu_recruiter", "title": "Get Start"}
                ]
            )
            
        # Condition B: Seeker Only
        elif is_seeker and is_seeker.registration_complete:
            text = (
                "👋 *Welcome back to JobInfo!*\n\n"
                "Thank you for using Jobinfo! 🤝 It looks like your session was paused.\n\n"
                "Whenever you're ready to track your current applications or discover fresh job openings, "
                "you can jump right back in anytime by clicking below 👇"
            )
            await wa_client.send_buttons(
                to=wa_number,
                body_text=text,
                buttons=[
                    {"id": "menu_seeker", "title": "Get Start"}
                ]
            )
            
        # Condition D: Unregistered / None
        else:
            text = (
                "👋 *Welcome back to JobInfo!*\n\n"
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
                    {"id": "menu_recruiter", "title": "I am Recruiter"},
                    {"id": "menu_seeker", "title": "I am Job Seeker"}
                ]
            )
            
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Error in send_delayed_session_menu: {e}")
    finally:
        db.close()
