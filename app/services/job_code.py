"""
Job code generation and parsing utilities.
Job codes follow the format: JC:XXX or JC:XXXX (e.g. JC:472, JC:3819)

Strategy:
  1. Generate random 3-digit codes (100–999) first (~900 available slots).
  2. Once all 3-digit codes are exhausted, use random 4-digit codes (1000–9999).
  3. Existing codes of any digit count (including legacy 1-digit and 2-digit)
     are left untouched; only new codes are constrained to 3 or 4 digits.
"""
import random
import re
from sqlalchemy.orm import Session
from app.db.models import JobVacancy

# Digit-range tiers, tried in order
_TIERS = [
    (100, 999),     # 3-digit: 900 possible codes
    (1000, 9999),   # 4-digit: 9 000 possible codes
]


def _used_numbers(db: Session) -> set[int]:
    """Return the set of numeric parts already used in existing job codes."""
    rows = db.query(JobVacancy.job_code).all()
    nums: set[int] = set()
    for (code,) in rows:
        # code is like "JC:123"
        try:
            nums.add(int(code.split(":")[1]))
        except (IndexError, ValueError):
            continue
    return nums


def generate_job_code(db: Session) -> str:
    """
    Generate a random, unused job code.

    Picks a random number from the current tier (3-digit first, then 4-digit).
    Existing codes are loaded once and compared in-memory for efficiency.
    A final DB uniqueness check guards against concurrent inserts.
    """
    used = _used_numbers(db)

    for lo, hi in _TIERS:
        pool = set(range(lo, hi + 1)) - used
        if not pool:
            continue  # tier exhausted → try next

        # Pick a random unused number from this tier
        candidate_num = random.choice(list(pool))
        candidate_code = f"JC:{candidate_num}"

        # Double-check against the DB (race-condition guard)
        exists = (
            db.query(JobVacancy.id)
            .filter(JobVacancy.job_code == candidate_code)
            .first()
        )
        if not exists:
            return candidate_code

        # Extremely rare: lost the race — retry within the same tier
        pool.discard(candidate_num)
        if pool:
            candidate_num = random.choice(list(pool))
            candidate_code = f"JC:{candidate_num}"
            return candidate_code

    # Fallback: all 3-digit and 4-digit codes are taken (9 900 vacancies)
    raise RuntimeError("All 3-digit and 4-digit job codes are exhausted")


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
