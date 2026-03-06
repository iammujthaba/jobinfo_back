"""
WhatsApp Flows data exchange endpoint with full AES/RSA encryption.

Meta sends encrypted payloads to this endpoint. We decrypt, process the
screen transition (including the Indian Postal API lookup), and respond
with an encrypted payload.
"""
import json
import logging

import httpx
from fastapi import APIRouter, Request, Response

from app.config import get_settings
from app.whatsapp.flow_crypto import (
    load_private_key,
    decrypt_request,
    encrypt_response,
)

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/flows", tags=["flows"])

# ── Lazy-load RSA key ─────────────────────────────────────────────────────────
_private_key = None

def _get_private_key():
    global _private_key
    if _private_key is None:
        _private_key = load_private_key(
            settings.flow_private_key_path,
            passphrase=settings.flow_private_key_passphrase or None,
        )
        logger.info("Flow RSA private key loaded.")
    return _private_key


# ═══════════════════════════════════════════════════════════════════════════════
# Main endpoint
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/callback")
async def flow_data_exchange(request: Request):
    """
    Encrypted WhatsApp Flows data-exchange endpoint.
    Handles ping, INIT, and data_exchange actions.
    """
    body = await request.json()
    private_key = _get_private_key()

    # ── Decrypt ───────────────────────────────────────────────────────────
    try:
        decrypted, aes_key, iv = decrypt_request(body, private_key)
    except Exception as e:
        logger.error("Flow decryption failed: %s", e)
        return Response(status_code=421)

    action = decrypted.get("action")
    screen = decrypted.get("screen")
    data = decrypted.get("data", {})
    version = decrypted.get("version")

    logger.info("Flow: action=%s screen=%s version=%s", action, screen, version)

    # ── Health-check ping ─────────────────────────────────────────────────
    if action == "ping":
        resp = {"version": version, "data": {"status": "active"}}
        return Response(content=encrypt_response(resp, aes_key, iv), media_type="text/plain")

    # ── INIT: first screen load ───────────────────────────────────────────
    if action == "INIT":
        resp = _handle_init(screen, data)
        return Response(content=encrypt_response(resp, aes_key, iv), media_type="text/plain")

    # ── data_exchange: Screen 1 → Screen 2 ────────────────────────────────
    if action == "data_exchange":
        resp = await _handle_data_exchange(screen, data)
        return Response(content=encrypt_response(resp, aes_key, iv), media_type="text/plain")

    # ── Fallback ──────────────────────────────────────────────────────────
    logger.warning("Unknown flow action: %s", action)
    resp = {"version": version or "7.3", "data": {"status": "active"}}
    return Response(content=encrypt_response(resp, aes_key, iv), media_type="text/plain")


# ═══════════════════════════════════════════════════════════════════════════════
# Screen handlers
# ═══════════════════════════════════════════════════════════════════════════════

def _handle_init(screen: str, data: dict) -> dict:
    """Return initial data for the first screen."""
    if screen == "SEEKER_REGISTRATION":
        return {
            "screen": "SEEKER_REGISTRATION",
            "data": {
                "pending_job_code": data.get("pending_job_code", ""),
            },
        }
    return {"screen": screen, "data": {}}


async def _handle_data_exchange(screen: str, data: dict) -> dict:
    """
    Handle the Screen 1 → Screen 2 transition.
    Receives the PIN code, queries the postal API, and returns
    a response that directs the Flow to SEEKER_LOCATION.
    """
    # FIX: We must check the screen the user is CURRENTLY coming from
    if screen == "SEEKER_REGISTRATION":
        pin_code = str(data.get("pin_code", "")).strip()

        if not pin_code or len(pin_code) != 6 or not pin_code.isdigit():
            return {
                "screen": "SEEKER_LOCATION",
                "data": {
                    "post_offices": [
                        {"id": "invalid", "title": "Invalid PIN — go back and re-enter"},
                    ],
                },
            }

        post_offices = await _lookup_post_offices(pin_code)

        if not post_offices:
            return {
                "screen": "SEEKER_LOCATION",
                "data": {
                    "post_offices": [
                        {"id": "not_found", "title": f"No results for PIN {pin_code}"},
                    ],
                },
            }

        options = [{"id": name, "title": name} for name in post_offices]

        return {
            "screen": "SEEKER_LOCATION",
            "data": {
                "post_offices": options,
            },
        }

    return {"screen": screen, "data": {}}


async def _lookup_post_offices(pin_code: str) -> list[str]:
    """
    Query the Indian Postal PIN Code API.
    Returns a list of post office names for the given PIN.
    """
    url = f"https://api.postalpincode.in/pincode/{pin_code}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()

        data = resp.json()
        # API returns an array, check if first element has a successful status
        if not data or not isinstance(data, list) or data[0].get("Status") != "Success":
            logger.warning("Postal API: no results for PIN %s", pin_code)
            return []

        names = [
            po.get("Name", "")
            for po in data[0].get("PostOffice", [])
            if po.get("Name")
        ]
        logger.info("PIN %s → %d post offices", pin_code, len(names))
        return names

    except Exception as e:
        logger.error("Postal API error for PIN %s: %s", pin_code, e)
        return []