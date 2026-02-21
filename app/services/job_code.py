"""
Job code generation and parsing utilities.
Job codes follow the format: JC:XXXX (e.g. JC:1001)
"""
import re
from sqlalchemy.orm import Session
from app.db.models import JobVacancy


def generate_job_code(db: Session) -> str:
    """Generate the next sequential job code (JC:1001, JC:1002, ...)."""
    last = db.query(JobVacancy).order_by(JobVacancy.id.desc()).first()
    next_num = (last.id + 1) if last else 1001
    return f"JC:{next_num}"


def parse_job_code(text: str) -> str | None:
    """
    Extract a job code from an incoming message text.
    Accepts: 'Apply JC:1002', 'apply jc:1002', 'JC:1002', etc.
    Returns: 'JC:1002' (uppercased) or None if not found.
    """
    match = re.search(r"(JC:\d+)", text, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    return None
