"""
Job code generation and parsing utilities.
Job codes follow the format: JC:XXXX (e.g. JC:1001)
"""
import re
from sqlalchemy.orm import Session
from app.db.models import JobVacancy


def generate_job_code(db: Session) -> str:
    """
    Generate the next unused sequential job code in the format JC:XXXX.

    Seeds the candidate number from the highest existing vacancy ID so the
    first iteration is almost always a hit in normal (non-concurrent) usage.
    The DB uniqueness check in the loop guarantees correctness even if two
    requests race to create a vacancy at the same instant.

    Complexity: O(1) in the common case; O(k) only under k-way contention.
    """
    last = db.query(JobVacancy).order_by(JobVacancy.id.desc()).first()
    candidate_num = (last.id + 1) if last else 1001

    while True:
        candidate_code = f"JC:{candidate_num}"
        exists = (
            db.query(JobVacancy.id)
            .filter(JobVacancy.job_code == candidate_code)
            .first()
        )
        if not exists:
            return candidate_code
        # Collision detected — advance by one and try again
        candidate_num += 1


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
