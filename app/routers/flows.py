"""
WhatsApp Flows data exchange endpoint.
Meta calls this endpoint during flow rendering to get dynamic data,
and again when the flow is submitted.

See: https://developers.facebook.com/docs/whatsapp/flows/guides/implementingcustomerserver
"""
import base64
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db.base import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/flows", tags=["flows"])


@router.post("/callback")
async def flow_data_exchange(request: Request, db: Session = Depends(get_db)):
    """
    WhatsApp Flows Data Exchange Endpoint.

    Meta posts encrypted flow payloads here. For non-sensitive flows you can
    configure the flow to use 'data_channel_uri' pointing here.

    The decryption below assumes you have set up your business's RSA private key.
    For simple flows that don't need server-side data, you can use
    'mode: "published"' in Meta's Flow Builder without this endpoint.
    """
    body = await request.json()

    # In production, decrypt the payload using your RSA private key.
    # For now, log and return a health-check style response.
    # Replace with real decryption logic from Meta's sample code.
    logger.info("Flow callback received: %s", json.dumps(body)[:200])

    # Standard health-check ping from Meta
    if body.get("action") == "ping":
        return {"data": {"status": "active"}}

    # Screen data request (populate dropdowns, pre-fill fields, etc.)
    if body.get("action") == "data_exchange":
        screen = body.get("screen", "")
        data = body.get("data", {})
        return _handle_data_exchange(screen, data, db)

    return {"data": {}}


def _handle_data_exchange(screen: str, data: dict, db: Session) -> dict:
    """
    Return server-side data for a given Flow screen.
    Add cases here for each screen that needs dynamic data.
    """
    from app.db.models import SubscriptionPlan

    if screen == "SELECT_PLAN":
        plans = db.query(SubscriptionPlan).all()
        return {
            "data": {
                "plans": [
                    {
                        "id": p.name.value,
                        "title": p.display_name,
                        "description": f"₹{p.price_inr} | {p.duration_days} days | {p.max_applications or 'Unlimited'} apps",
                    }
                    for p in plans
                ]
            }
        }

    # Default: empty data
    return {"data": {}}
