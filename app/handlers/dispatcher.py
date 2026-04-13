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


async def _handle_text(wa_number: str, text: str, db: Session) -> None:
    """Route a plain text message."""
    from app.handlers import recruiter as recruiter_handler
    from app.handlers import seeker as seeker_handler
    from app.services.job_code import parse_job_code

    normalized = text.strip().lower()

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

    # Default: show help menu
    await global_handler.send_help_menu(wa_number)


async def _handle_button(wa_number: str, button_id: str, db: Session) -> None:
    """Route a quick-reply button press."""
    from app.handlers import recruiter as recruiter_handler
    from app.handlers import seeker as seeker_handler

    # ── Global menu buttons ─────────────────────────────────────────────────
    handled = await global_handler.handle_global_button(wa_number, button_id, db)
    if handled:
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
        from app.services.storage import save_cv_from_whatsapp
        cv_path = await save_cv_from_whatsapp(
            wa_number=wa_number,
            media_id=doc.get("id", ""),
            mime_type=doc.get("mime_type", "application/pdf"),
        )
        if cv_path:
            from app.db.models import Candidate
            from app.whatsapp.templates import cv_update_confirmation_body
            from app.whatsapp.client import wa_client

            candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
            if candidate:
                candidate.cv_path = cv_path
                candidate.cv_updates_used = (candidate.cv_updates_used or 0) + 1
                db.commit()
                await wa_client.send_text(
                    to=wa_number,
                    body=cv_update_confirmation_body(candidate),
                )
        else:
            from app.whatsapp.client import wa_client
            await wa_client.send_text(
                to=wa_number,
                body="❌ Invalid file format. Please upload a PDF or CSV.",
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
            text = "_Hi there!_ Thank you for using JobInfo!🤝\n\nIt's look like your session is pausing.\nWhether you're looking to hire great talent or find your next job, you can jump right back into here anytime!"
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
            text = "_Hi there!_ Thank you for using JobInfo!🤝\n\nIt's look like your session is pausing,\nWhenever your are ready to review applicat application or post new vacancy. you can jump right back into here anytime!"
            await wa_client.send_buttons(
                to=wa_number,
                body_text=text,
                buttons=[
                    {"id": "menu_recruiter", "title": "Get start"}
                ]
            )
            
        # Condition B: Seeker Only
        elif is_seeker and is_seeker.registration_complete:
            text = "_Hi there!_ Thank you for using JobInfo!🤝\n\nIt's look like your session is pausing.\nWhenever your are ready to track your current applications or discover fresh job openings. you can jump right back into here anytime!"
            await wa_client.send_buttons(
                to=wa_number,
                body_text=text,
                buttons=[
                    {"id": "menu_seeker", "title": "Get start"}
                ]
            )
            
        # Condition D: Unregistered / None
        else:
            text = "Welcome to JobInfo! 🚀 We noticed you haven't set up your profile yet. It only takes a minute to get started. Let us know what you're looking for a job or hire great staff!"
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
