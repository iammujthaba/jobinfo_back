"""
Webhook router – receives all events from WhatsApp Cloud API.
GET  /webhook  – Meta verification handshake
POST /webhook  – Incoming events (messages, status, flow callbacks)
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.base import get_db
from app.handlers.dispatcher import dispatch
from app.whatsapp.client import WhatsAppClient

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter()


@router.get("/webhook")
async def verify_webhook(request: Request):
    """
    Meta sends a GET request to verify the webhook URL.
    We must echo back the hub.challenge value if the verify_token matches.
    """
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == settings.verify_token:
        logger.info("Webhook verified successfully.")
        return Response(content=challenge, media_type="text/plain")

    raise HTTPException(status_code=403, detail="Verification token mismatch")


@router.post("/webhook")
async def receive_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Receives WhatsApp Cloud API events.
    Verifies HMAC signature, then dispatches to business logic.
    """
    body_bytes = await request.body()

    # Verify signature (skip in dev if APP_SECRET is empty)
    if settings.app_secret:
        sig_header = request.headers.get("X-Hub-Signature-256", "")
        if not WhatsAppClient.verify_signature(body_bytes, sig_header):
            logger.warning("Invalid webhook signature – request rejected.")
            raise HTTPException(status_code=403, detail="Invalid signature")

    payload = await request.json()

    # --- START OF NEW ROUTING LOGIC ---
    try:
        # Safely drill down into Meta's nested JSON payload
        entries = payload.get("entry", [])
        if entries:
            changes = entries[0].get("changes", [])
            if changes:
                metadata = changes[0].get("value", {}).get("metadata", {})
                incoming_phone_id = metadata.get("phone_number_id")

                # If an ID is found, check it against our local .env file
                if incoming_phone_id and incoming_phone_id != settings.whatsapp_phone_id:
                    logger.info(f"🛑 Ignored message meant for another bot (ID: {incoming_phone_id})")
                    return {"status": "ignored", "reason": "Cross-environment webhook dropped"}
    except Exception as e:
        logger.warning(f"Error checking webhook phone ID: {e}")
    # --- END OF NEW ROUTING LOGIC ---

    # Dispatch asynchronously
    try:
        await dispatch(payload, db)
    except Exception as exc:
        logger.exception("Error dispatching webhook: %s", exc)

    return {"status": "ok"}