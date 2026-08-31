import os
from pathlib import Path
from app.db.base import SessionLocal
from app.db.models import CandidateResume

def backfill():
    db = SessionLocal()
    try:
        resumes = db.query(CandidateResume).all()
        updated = 0
        for r in resumes:
            if not r.file_name and r.media_id:
                clean_name = os.path.basename(r.media_id.replace("\\", "/"))
                r.file_name = clean_name
                updated += 1
        db.commit()
        print(f"Backfilled {updated} resumes successfully.")
    finally:
        db.close()

if __name__ == "__main__":
    backfill()
