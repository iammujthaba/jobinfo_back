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

from app.db.models import ConversationState
from app.handlers import global_handler

logger = logging.getLogger(__name__)


async def dispatch(payload: dict, db: Session) -> None:
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

            logger.info("Incoming %s from %s", msg_type, wa_number)

            if msg_type == "text":
                await _handle_text(wa_number, message["text"]["body"], db)

            elif msg_type == "interactive":
                interactive = message["interactive"]
                sub_type = interactive.get("type")

                if sub_type == "button_reply":
                    button_id = interactive["button_reply"]["id"]
                    await _handle_button(wa_number, button_id, db)

                elif sub_type == "list_reply":
                    row_id = interactive["list_reply"]["id"]
                    await _handle_list_reply(wa_number, row_id, db)

                elif sub_type == "nfm_reply":
                    # WhatsApp Flow completion
                    flow_data = interactive["nfm_reply"]
                    await _handle_flow_reply(wa_number, flow_data, db)

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

    # ── Seeker buttons ──────────────────────────────────────────────────────
    if button_id == "btn_callback":
        await seeker_handler.handle_callback_button(wa_number, db)
        return

    if button_id == "btn_view_applications":
        await seeker_handler.handle_view_applications_button(wa_number, db)
        return

    # "btn_register_JC:1002"
    if button_id.startswith("btn_register_"):
        job_code = button_id.removeprefix("btn_register_")
        await seeker_handler.handle_register_button(wa_number, job_code, db)
        return

    # "btn_apply_now_42"
    if button_id.startswith("btn_apply_now_"):
        vacancy_id = int(button_id.removeprefix("btn_apply_now_"))
        await seeker_handler.handle_apply_now_button(wa_number, vacancy_id, db)
        return

    # "btn_update_cv_42"
    if button_id.startswith("btn_update_cv_"):
        vacancy_id = int(button_id.removeprefix("btn_update_cv_"))
        await seeker_handler.handle_update_cv_button(wa_number, vacancy_id, db)
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
    if "title" in submitted and "description" in submitted:
        # Post Vacancy Flow
        await recruiter_handler.handle_post_vacancy_flow_completion(wa_number, submitted, db)

    elif "skills" in submitted:
        # Seeker Registration Flow
        await seeker_handler.handle_registration_flow_completion(wa_number, submitted, db)

    elif "media_id" in submitted and "skills" not in submitted:
        # CV Update Flow
        await seeker_handler.handle_cv_update_flow_completion(wa_number, submitted, db)

    elif "company" in submitted and "location" in submitted:
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
