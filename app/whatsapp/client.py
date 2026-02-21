"""
WhatsApp Cloud API client.
Handles all outbound calls to Meta's Graph API.
"""
import hashlib
import hmac
import logging
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

BASE_URL = "https://graph.facebook.com/v20.0"


class WhatsAppClient:
    def __init__(self):
        self.phone_id = settings.whatsapp_phone_id
        self.headers = {
            "Authorization": f"Bearer {settings.whatsapp_token}",
            "Content-Type": "application/json",
        }

    # ─── Low-level sender ────────────────────────────────────────────────────

    async def _post(self, payload: dict) -> dict:
        url = f"{BASE_URL}/{self.phone_id}/messages"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload, headers=self.headers)
        if resp.status_code not in (200, 201):
            logger.error("WA API error %s: %s", resp.status_code, resp.text)
        resp.raise_for_status()
        return resp.json()

    # ─── Text message ────────────────────────────────────────────────────────

    async def send_text(self, to: str, body: str) -> dict:
        """Send a plain text message."""
        return await self._post({
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": body},
        })

    # ─── Template message ────────────────────────────────────────────────────

    async def send_template(
        self,
        to: str,
        template_name: str,
        language_code: str = "en",
        components: list[dict] | None = None,
    ) -> dict:
        """Send a pre-approved WhatsApp template message."""
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language_code},
            },
        }
        if components:
            payload["template"]["components"] = components
        return await self._post(payload)

    # ─── Interactive – list / buttons ────────────────────────────────────────

    async def send_buttons(
        self,
        to: str,
        body_text: str,
        buttons: list[dict],
        header_text: str | None = None,
        footer_text: str | None = None,
    ) -> dict:
        """
        Send an interactive button message (max 3 buttons).
        buttons = [{"id": "btn_id", "title": "Button Label"}, ...]
        """
        interactive: dict[str, Any] = {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b["id"], "title": b["title"]}}
                    for b in buttons
                ]
            },
        }
        if header_text:
            interactive["header"] = {"type": "text", "text": header_text}
        if footer_text:
            interactive["footer"] = {"text": footer_text}

        return await self._post({
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": interactive,
        })

    async def send_list(
        self,
        to: str,
        body_text: str,
        button_label: str,
        sections: list[dict],
        header_text: str | None = None,
    ) -> dict:
        """Send an interactive list message."""
        interactive: dict[str, Any] = {
            "type": "list",
            "body": {"text": body_text},
            "action": {"button": button_label, "sections": sections},
        }
        if header_text:
            interactive["header"] = {"type": "text", "text": header_text}

        return await self._post({
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": interactive,
        })

    # ─── WhatsApp Flow ───────────────────────────────────────────────────────

    async def send_flow(
        self,
        to: str,
        flow_id: str,
        flow_cta: str,
        body_text: str,
        flow_token: str = "unused",
        flow_action: str = "navigate",
        flow_action_payload: dict | None = None,
        header_text: str | None = None,
        footer_text: str | None = None,
    ) -> dict:
        """Launch a WhatsApp Flow."""
        action_payload: dict[str, Any] = {
            "flow_message_version": "3",
            "flow_token": flow_token,
            "flow_id": flow_id,
            "flow_cta": flow_cta,
            "flow_action": flow_action,
        }
        if flow_action_payload:
            action_payload["flow_action_payload"] = flow_action_payload

        interactive: dict[str, Any] = {
            "type": "flow",
            "body": {"text": body_text},
            "action": {
                "name": "flow",
                "parameters": action_payload
            },
        }
        if header_text:
            interactive["header"] = {"type": "text", "text": header_text}
        if footer_text:
            interactive["footer"] = {"text": footer_text}

        return await self._post({
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": interactive,
        })

    # ─── Media download (for CV upload) ──────────────────────────────────────

    async def get_media_url(self, media_id: str) -> str:
        """Resolve a media_id to a downloadable URL."""
        url = f"{BASE_URL}/{media_id}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=self.headers)
        resp.raise_for_status()
        return resp.json()["url"]

    async def download_media(self, media_url: str) -> bytes:
        """Download raw bytes from a WhatsApp media URL."""
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(media_url, headers=self.headers)
        resp.raise_for_status()
        return resp.content

    # ─── Webhook signature verification ──────────────────────────────────────

    @staticmethod
    def verify_signature(payload_bytes: bytes, x_hub_signature: str) -> bool:
        """Verify the X-Hub-Signature-256 header from Meta."""
        expected = hmac.new(
            settings.app_secret.encode(),
            payload_bytes,
            hashlib.sha256,
        ).hexdigest()
        received = x_hub_signature.removeprefix("sha256=")
        return hmac.compare_digest(expected, received)


# Singleton instance
wa_client = WhatsAppClient()
