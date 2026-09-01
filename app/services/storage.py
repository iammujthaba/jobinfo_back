"""
Media storage service.
Downloads CV files from WhatsApp Cloud API or FastAPI upload, validates format,
preserves user's original filenames safely, and saves to disk.
"""
import logging
import os
from pathlib import Path
import re
import uuid
from fastapi import UploadFile

from app.config import get_settings
from app.whatsapp.client import wa_client

logger = logging.getLogger(__name__)
settings = get_settings()

ALLOWED_EXTENSIONS = {".pdf", ".csv", ".doc", ".docx"}
ALLOWED_MIMETYPES = {
    "application/pdf",
    "text/csv",
    "application/vnd.ms-excel",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
MAX_CV_SIZE_BYTES = 1024 * 1024  # 1 MB maximum CV upload limit


def sanitize_cv_filename(raw_name: str | None, fallback_ext: str = ".pdf") -> str:
    """
    Sanitize an uploaded filename to prevent directory traversal and filesystem issues
    while preserving the user's original meaningful name and extension.
    """
    if not raw_name or not str(raw_name).strip():
        return f"CV_{uuid.uuid4().hex[:8]}{fallback_ext}"

    # Extract base filename (handles both Windows and Unix path separators)
    clean_name = os.path.basename(str(raw_name).replace("\\", "/")).strip()
    p = Path(clean_name)
    stem = p.stem.strip()
    ext = (p.suffix.lower() if p.suffix else "") or fallback_ext

    # Remove unsafe characters: allow alphanumeric, spaces, underscores, dashes, dots, parentheses
    stem = re.sub(r"[^\w\s\-.()]", "_", stem).strip()
    # Collapse multiple consecutive whitespace/underscores into single
    stem = re.sub(r"\s+", " ", stem)
    stem = re.sub(r"_+", "_", stem).strip()

    if not stem:
        stem = f"CV_{uuid.uuid4().hex[:8]}"

    # Limit stem length to 80 chars
    stem = stem[:80].strip()
    return f"{stem}{ext}"


def get_unique_destination(upload_dir: Path, target_filename: str) -> tuple[Path, str]:
    """
    Ensure the target destination does not overwrite an existing file.
    Appends (1), (2), etc. if a file with the same name already exists in the user folder.
    Returns (dest_path, final_filename).
    """
    dest = upload_dir / target_filename
    if not dest.exists():
        return dest, target_filename

    p = Path(target_filename)
    stem = p.stem
    ext = p.suffix
    counter = 1
    while True:
        candidate_name = f"{stem} ({counter}){ext}"
        candidate_dest = upload_dir / candidate_name
        if not candidate_dest.exists():
            return candidate_dest, candidate_name
        counter += 1


async def save_cv_from_whatsapp(
    wa_number: str,
    media_id: str,
    mime_type: str,
    original_filename: str | None = None,
) -> tuple[str | None, str | None]:
    """
    Download a CV document from WhatsApp, validate, and save to disk with original filename.
    Returns (saved_file_path, display_filename), or (None, None) if validation fails.
    """
    ext = _mime_to_ext(mime_type)
    if not ext and original_filename:
        ext = Path(original_filename).suffix.lower()

    if ext not in ALLOWED_EXTENSIONS:
        logger.warning("Rejected CV upload from %s – bad mime type: %s", wa_number, mime_type)
        return None, None

    try:
        media_url = await wa_client.get_media_url(media_id)
        raw_bytes = await wa_client.download_media(media_url)
    except Exception as e:
        logger.error("Failed to download WhatsApp media %s for %s: %s", media_id, wa_number, e)
        return None, None

    if len(raw_bytes) > MAX_CV_SIZE_BYTES:
        logger.warning("Rejected WhatsApp CV upload from %s – file size %d exceeds limit %d bytes", wa_number, len(raw_bytes), MAX_CV_SIZE_BYTES)
        return None, None

    upload_dir = Path(settings.media_upload_dir) / wa_number
    upload_dir.mkdir(parents=True, exist_ok=True)

    # Use original filename if provided, otherwise clean fallback
    fallback_name = f"CV_{wa_number[-4:]}_{uuid.uuid4().hex[:6]}{ext}"
    sanitized_name = sanitize_cv_filename(original_filename or fallback_name, fallback_ext=ext)
    dest, final_filename = get_unique_destination(upload_dir, sanitized_name)

    dest.write_bytes(raw_bytes)
    logger.info("Saved WhatsApp CV for %s → %s (display: %s, size: %d bytes)", wa_number, dest, final_filename, len(raw_bytes))
    return str(dest), final_filename


async def save_cv_from_upload_file(
    wa_number: str,
    upload_file: UploadFile,
    original_filename: str | None = None,
) -> tuple[str | None, str | None]:
    """
    Save a CV uploaded via FastAPI UploadFile with original filename preserved.
    Returns (saved_file_path, display_filename), or (None, None) if validation fails.
    """
    raw_name = original_filename or upload_file.filename
    ext = _mime_to_ext(upload_file.content_type)
    if not ext and raw_name:
        ext = Path(raw_name).suffix.lower()

    if ext not in ALLOWED_EXTENSIONS:
        logger.warning("Rejected web CV upload from %s – bad mime type: %s (file: %s)", wa_number, upload_file.content_type, raw_name)
        return None, None

    content = await upload_file.read()
    if len(content) > MAX_CV_SIZE_BYTES:
        logger.warning("Rejected web CV upload from %s – file size %d exceeds limit %d bytes", wa_number, len(content), MAX_CV_SIZE_BYTES)
        return None, None

    upload_dir = Path(settings.media_upload_dir) / wa_number
    upload_dir.mkdir(parents=True, exist_ok=True)

    sanitized_name = sanitize_cv_filename(raw_name, fallback_ext=ext or ".pdf")
    dest, final_filename = get_unique_destination(upload_dir, sanitized_name)

    dest.write_bytes(content)

    logger.info("Saved Web CV for %s → %s (display: %s, size: %d bytes)", wa_number, dest, final_filename, len(content))
    return str(dest), final_filename


def _mime_to_ext(mime_type: str | None) -> str:
    if not mime_type:
        return ""
    mapping = {
        "application/pdf": ".pdf",
        "text/csv": ".csv",
        "application/vnd.ms-excel": ".csv",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    }
    return mapping.get(mime_type.lower(), "")
