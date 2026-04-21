import sys
from sqlalchemy.orm import Session
from app.db.base import SessionLocal
from app.db.models import (
    Recruiter, JobVacancy, Candidate, CandidateResume, CandidateApplication,
    GetHelpRequest, OTPRecord, ConversationState, UserQuestion, MagicLink
)

def purge_test_data():
    target_numbers = ['7025962175', '917025962175']
    session: Session = SessionLocal()
    
    try:
        print(f"--- DRY RUN / SAFETY CHECK ---")
        print(f"Target numbers: {target_numbers}\n")
        
        # 1. Base users
        recruiters = session.query(Recruiter).filter(Recruiter.wa_number.in_(target_numbers)).all()
        candidates = session.query(Candidate).filter(Candidate.wa_number.in_(target_numbers)).all()
        
        recruiter_ids = [r.id for r in recruiters]
        candidate_ids = [c.id for c in candidates]
        
        # 2. Associated primary data
        vacancies = []
        if recruiter_ids:
            vacancies = session.query(JobVacancy).filter(JobVacancy.recruiter_id.in_(recruiter_ids)).all()
        vacancy_ids = [v.id for v in vacancies]
        
        resumes = []
        if candidate_ids:
            resumes = session.query(CandidateResume).filter(CandidateResume.candidate_id.in_(candidate_ids)).all()
            
        # 3. Applications
        # We must delete applications made BY these candidates, AND applications made TO these recruiters' vacancies
        apps_to_delete = []
        if candidate_ids or vacancy_ids:
            # Avoid passing empty list to .in_()
            c_ids_filter = candidate_ids if candidate_ids else [-1]
            v_ids_filter = vacancy_ids if vacancy_ids else [-1]
            
            apps_to_delete = session.query(CandidateApplication).filter(
                (CandidateApplication.candidate_id.in_(c_ids_filter)) |
                (CandidateApplication.vacancy_id.in_(v_ids_filter))
            ).all()
            
        # 4. Independent tables linked only by wa_number
        help_requests = session.query(GetHelpRequest).filter(GetHelpRequest.wa_number.in_(target_numbers)).all()
        otp_records = session.query(OTPRecord).filter(OTPRecord.wa_number.in_(target_numbers)).all()
        conv_states = session.query(ConversationState).filter(ConversationState.wa_number.in_(target_numbers)).all()
        user_quests = session.query(UserQuestion).filter(UserQuestion.wa_number.in_(target_numbers)).all()
        magic_links = session.query(MagicLink).filter(MagicLink.wa_number.in_(target_numbers)).all()
        
        # Summary
        print("Records found to delete:")
        print(f"- Recruiters: {len(recruiters)}")
        print(f"- Candidates: {len(candidates)}")
        print(f"- Job Vacancies (by these recruiters): {len(vacancies)}")
        print(f"- Candidate Resumes (by these candidates): {len(resumes)}")
        print(f"- Candidate Applications (by these candidates OR to these vacancies): {len(apps_to_delete)}")
        print(f"- GetHelpRequests: {len(help_requests)}")
        print(f"- OTPRecords: {len(otp_records)}")
        print(f"- ConversationStates: {len(conv_states)}")
        print(f"- UserQuestions: {len(user_quests)}")
        print(f"- MagicLinks: {len(magic_links)}")
        
        total_records = (len(recruiters) + len(candidates) + len(vacancies) + 
                         len(resumes) + len(apps_to_delete) + len(help_requests) + 
                         len(otp_records) + len(conv_states) + len(user_quests) + len(magic_links))
                         
        if total_records == 0:
            print("\nNo records found for the target phone numbers. Exiting.")
            return

        print(f"\nTotal records to delete: {total_records}")
        print("-" * 30)
        
        # We explicitly flush stdout to ensure the user sees the prompt correctly
        sys.stdout.flush()
        confirm = input("\nType 'CONFIRM' to execute deletion, or anything else to cancel: ")
        
        if confirm == 'CONFIRM':
            print("\nDeleting records...")
            # Order of deletion is critical to avoid FK constraint violations
            
            # 1. Delete Applications (Children of Vacancy and Candidate/Resume)
            for app in apps_to_delete:
                session.delete(app)
                
            # 2. Delete Resumes (Children of Candidate)
            for res in resumes:
                session.delete(res)
                
            # 3. Delete Vacancies (Children of Recruiter)
            for vac in vacancies:
                session.delete(vac)
                
            # 4. Delete Candidates
            for cand in candidates:
                session.delete(cand)
                
            # 5. Delete Recruiters
            for rec in recruiters:
                session.delete(rec)
                
            # 6. Delete unassociated tables
            for item in help_requests + otp_records + conv_states + user_quests + magic_links:
                session.delete(item)
                
            session.commit()
            print("Successfully deleted all selected records!")
        else:
            print("\nCancellation requested. Rolling back and exiting cleanly.")
            session.rollback()
            
    except Exception as e:
        session.rollback()
        print(f"\nAn error occurred: {e}")
        print("Rolled back all changes.")
    finally:
        session.close()

if __name__ == "__main__":
    purge_test_data()
