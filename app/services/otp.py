"""
OTP generation and verification service.
OTPs are stored in the otp_records table with a 5-minute TTL.
"""
import random
import string
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.db.models import OTPRecord


OTP_TTL_MINUTES = 5


def generate_otp() -> str:
    return "".join(random.choices(string.digits, k=6))


def create_otp(db: Session, wa_number: str) -> str:
    """Generate and persist a new OTP for the given WhatsApp number."""
    otp_code = generate_otp()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_TTL_MINUTES)

    # Invalidate any previous unused OTPs for this number
    db.query(OTPRecord).filter(
        OTPRecord.wa_number == wa_number,
        OTPRecord.used == False,  # noqa: E712
    ).update({"used": True})

    record = OTPRecord(wa_number=wa_number, otp_code=otp_code, expires_at=expires_at)
    db.add(record)
    db.commit()
    return otp_code


def verify_otp(db: Session, wa_number: str, otp_code: str) -> bool:
    """Return True if the OTP is valid and mark it as used."""
    now = datetime.now(timezone.utc)
    record = (
        db.query(OTPRecord)
        .filter(
            OTPRecord.wa_number == wa_number,
            OTPRecord.otp_code == otp_code,
            OTPRecord.used == False,  # noqa: E712
            OTPRecord.expires_at > now,
        )
        .first()
    )
    if record:
        record.used = True
        db.commit()
        return True
    return False
