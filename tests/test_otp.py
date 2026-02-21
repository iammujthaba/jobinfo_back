"""Tests for OTP generation and verification."""
import pytest
from datetime import datetime, timedelta, timezone

from app.services.otp import create_otp, verify_otp
from app.db.models import OTPRecord


def test_create_and_verify_otp(db):
    wa_number = "919000000001"
    code = create_otp(db, wa_number)
    assert len(code) == 6
    assert code.isdigit()
    assert verify_otp(db, wa_number, code) is True


def test_otp_single_use(db):
    wa_number = "919000000002"
    code = create_otp(db, wa_number)
    assert verify_otp(db, wa_number, code) is True
    # Second use should fail
    assert verify_otp(db, wa_number, code) is False


def test_otp_wrong_code(db):
    wa_number = "919000000003"
    create_otp(db, wa_number)
    assert verify_otp(db, wa_number, "000000") is False


def test_otp_expired(db):
    wa_number = "919000000004"
    code = create_otp(db, wa_number)
    # Manually expire it
    record = db.query(OTPRecord).filter_by(wa_number=wa_number, used=False).first()
    record.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()
    assert verify_otp(db, wa_number, code) is False


def test_new_otp_invalidates_old(db):
    wa_number = "919000000005"
    old_code = create_otp(db, wa_number)
    _new_code = create_otp(db, wa_number)
    # Old code should now be invalid
    assert verify_otp(db, wa_number, old_code) is False
