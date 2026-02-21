"""
Media storage service.
Downloads CV files from WhatsApp Cloud API, validates format, saves to disk.
"""
import logging
import os
from pathlib import Path

from app.config import get_settings
from app.whatsapp.client import wa_client

logger = logging.getLogger(__name__)
settings = get_settings()

ALLOWED_EXTENSIONS = {".pdf", ".csv"}
ALLOWED_MIMETYPES = {"application/pdf", "text/csv", "application/vnd.ms-excel"}


async def save_cv_from_whatsapp(
    wa_number: str,
    media_id: str,
    mime_type: str,
) -> str | None:
    """
    Download a CV document from WhatsApp, validate, and save to disk.
    Returns the saved file path, or None if validation fails.
    """
    # Extension check via MIME type
    ext = _mime_to_ext(mime_type)
    if ext not in ALLOWED_EXTENSIONS:
        logger.warning("Rejected CV upload from %s – bad mime type: %s", wa_number, mime_type)
        return None

    media_url = await wa_client.get_media_url(media_id)
    raw_bytes = await wa_client.download_media(media_url)

    upload_dir = Path(settings.media_upload_dir) / wa_number
    upload_dir.mkdir(parents=True, exist_ok=True)

    filename = f"cv{ext}"
    dest = upload_dir / filename
    dest.write_bytes(raw_bytes)

    logger.info("Saved CV for %s → %s", wa_number, dest)
    return str(dest)


def _mime_to_ext(mime_type: str) -> str:
    mapping = {
        "application/pdf": ".pdf",
        "text/csv": ".csv",
        "application/vnd.ms-excel": ".csv",
    }
    return mapping.get(mime_type, "")
