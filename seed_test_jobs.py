import sys
import os

sys.path.append(os.path.abspath("e:/jobinfo/jobinfo_back_1.0"))

from sqlalchemy.orm import Session
from app.db.base import SessionLocal
from app.db.models import Recruiter, JobVacancy
from app.services.job_code import generate_job_code
from datetime import datetime, timezone

def seed():
    db = SessionLocal()
    try:
        wa_number = "917025962175"
        recruiter = db.query(Recruiter).filter_by(wa_number=wa_number).first()
        if not recruiter:
            print(f"Creating test recruiter for {wa_number}...")
            # In case the new Recruiter model fields are used
            recruiter = Recruiter(
                wa_number=wa_number,
                company_name="Mega Recruiters Inc",
                business_type="Corporate / IT",
                location="Kerala",
                business_contact=wa_number,
            )
            db.add(recruiter)
            db.commit()
            db.refresh(recruiter)
            
        print(f"Using Recruiter ID: {recruiter.id}")
        
        jobs_data = [
            {
                "job_category": "it_professional",
                "company_name": "TechCore Solutions",
                "district_region": "Ernakulam",
                "exact_location": "Kakkanad IT Park",
                "job_title": "Senior Python Developer",
                "job_description": "We are seeking an experienced Backend Python Engineer. Must be proficient in FastAPI, SQLAlchemy, and Postgres. Great team environment.",
                "job_mode": "On-site",
                "experience_required": "3-5 Years",
                "salary_range": "₹60,000 - ₹90,000",
            },
            {
                "job_category": "it_professional",
                "company_name": "TechCore Solutions",
                "district_region": "Ernakulam",
                "exact_location": "Infopark, Kochi",
                "job_title": "Junior React Developer",
                "job_description": "Looking for an energetic UI developer proficient in React.js. Knowledge of modern CSS frameworks is a plus. Immediate joiners preferred.",
                "job_mode": "Hybrid",
                "experience_required": "1-2 Years",
                "salary_range": "₹25,000 - ₹40,000",
            },
            {
                "job_category": "healthcare",
                "company_name": "City Care Hospital",
                "district_region": "Thiruvananthapuram",
                "exact_location": "Pattom",
                "job_title": "Registered Staff Nurse",
                "job_description": "Urgent requirement for registered ICU nurses. Must have a valid Kerala nursing council registration. Free accommodation provided.",
                "job_mode": "On-site",
                "experience_required": "2+ Years",
                "salary_range": "₹30,000 - ₹45,000",
            },
            {
                "job_category": "healthcare",
                "company_name": "Wellness Clinic",
                "district_region": "Kollam",
                "exact_location": "Chinnakada",
                "job_title": "Experienced Pharmacist",
                "job_description": "Hiring a full-time pharmacist for our busy outpatient clinic. B.Pharm or D.Pharm required. Good communication skills needed.",
                "job_mode": "On-site",
                "experience_required": "1-3 Years",
                "salary_range": "₹18,000 - ₹25,000",
            },
            {
                "job_category": "retail",
                "company_name": "Fashion Galleria",
                "district_region": "Kozhikode",
                "exact_location": "Focus Mall",
                "job_title": "Showroom Sales Executive",
                "job_description": "Looking for enthusiastic and presentable sales staff for our incoming summer collection. Fluent Malayalam and basic English required.",
                "job_mode": "On-site",
                "experience_required": "Fresher / 1 Year",
                "salary_range": "₹15,000 + Incentives",
            },
            {
                "job_category": "retail",
                "company_name": "SuperMart Weekly",
                "district_region": "Thrissur",
                "exact_location": "MG Road",
                "job_title": "Billing Cashier",
                "job_description": "Supermarket looking for a trustworthy billing cashier. Must be quick with POS systems and have good customer interaction skills.",
                "job_mode": "On-site",
                "experience_required": "1+ Year",
                "salary_range": "₹15,000 - ₹18,000",
            },
            {
                "job_category": "office_admin",
                "company_name": "Vertex Corporate",
                "district_region": "Ernakulam",
                "exact_location": "MG Road, Kochi",
                "job_title": "Front Office Receptionist",
                "job_description": "Seeking a pleasant front office admin to manage calls, greet visitors, and handle basic data entry. Must be proficient in MS Office.",
                "job_mode": "On-site",
                "experience_required": "1-3 Years",
                "salary_range": "₹18,000 - ₹22,000",
            },
            {
                "job_category": "office_admin",
                "company_name": "Global Traders",
                "district_region": "Malappuram",
                "exact_location": "Kottakkal",
                "job_title": "Tally Accountant",
                "job_description": "Opening for an accountant proficient in Tally Prime. Duties include GST filing, bank reconciliation, and daily ledger entry.",
                "job_mode": "On-site",
                "experience_required": "3+ Years",
                "salary_range": "₹25,000 - ₹35,000",
            },
            {
                "job_category": "driving",
                "company_name": "FastTrack Logistics",
                "district_region": "Ernakulam",
                "exact_location": "Kalamassery",
                "job_title": "Heavy Vehicle Driver",
                "job_description": "Transporting goods across Kerala. Must hold a valid heavy vehicle driving license taking safety very seriously.",
                "job_mode": "On-site",
                "experience_required": "5+ Years",
                "salary_range": "₹35,000 + Batta",
            },
            {
                "job_category": "driving",
                "company_name": "FreshFoods Delivery",
                "district_region": "Thiruvananthapuram",
                "exact_location": "Kazhakootam",
                "job_title": "Delivery Executive",
                "job_description": "Two-wheeler delivery role for food parcels. Must have own bike and valid 2W license. Flexible shifts available.",
                "job_mode": "Field Work",
                "experience_required": "Fresher",
                "salary_range": "Earn up to ₹25,000/month",
            },
            {
                "job_category": "hospitality",
                "company_name": "OceanView Resort",
                "district_region": "Alappuzha",
                "exact_location": "Marari Beach",
                "job_title": "Continental Chef",
                "job_description": "Looking for an expert Chef specializing in Continental cuisine for our 4-star resort. Excellent culinary and hygiene standards required.",
                "job_mode": "On-site",
                "experience_required": "4+ Years",
                "salary_range": "₹45,000 - ₹60,000",
            },
            {
                "job_category": "hospitality",
                "company_name": "Spice Route Cafe",
                "district_region": "Kochi",
                "exact_location": "Fort Kochi",
                "job_title": "Waitstaff / Server",
                "job_description": "Friendly servers needed for a bustling heritage cafe. Will handle order taking, serving, and maintaining table cleanliness.",
                "job_mode": "On-site",
                "experience_required": "6 months - 1 Year",
                "salary_range": "₹12,000 + Daily Tips",
            },
            {
                "job_category": "maintenance_technician",
                "company_name": "CoolHomes AC Services",
                "district_region": "Palakkad",
                "exact_location": "Town Center",
                "job_title": "HVAC AC Technician",
                "job_description": "Expert in split and window AC installation, gas filling, and repairs. Must have own tool kit. Two-wheeler provided by company.",
                "job_mode": "Field Work",
                "experience_required": "2+ Years",
                "salary_range": "₹20,000 - ₹30,000",
            },
            {
                "job_category": "maintenance_technician",
                "company_name": "BuildRight Builders",
                "district_region": "Thrissur",
                "exact_location": "Puzhakkal",
                "job_title": "Experienced Electrician",
                "job_description": "Need an electrician for our ongoing residential project. Strong knowledge of home wiring, conduit laying, and safety protocols.",
                "job_mode": "On-site",
                "experience_required": "3+ Years",
                "salary_range": "₹25,000 - ₹32,000",
            },
            {
                "job_category": "gulf_abroad",
                "company_name": "Oasis Trading LLC",
                "district_region": "GCC",
                "exact_location": "Dubai, UAE",
                "job_title": "Warehouse Manager",
                "job_description": "Managing inventory, shipping, and receiving at a large auto-parts warehouse in Dubai. Visa and ticket provided.",
                "job_mode": "On-site",
                "experience_required": "5+ Years",
                "salary_range": "AED 4,000 - AED 6,000",
            }
        ]

        # Add all jobs
        created_count = 0
        now = datetime.now(timezone.utc)
        for data in jobs_data:
            code = generate_job_code(db)
            job = JobVacancy(
                job_code=code,
                recruiter_id=recruiter.id,
                job_category=data["job_category"],
                district_region=data["district_region"],
                exact_location=data["exact_location"],
                job_title=data["job_title"],
                job_description=data["job_description"],
                job_mode=data["job_mode"],
                experience_required=data["experience_required"],
                salary_range=data["salary_range"],
                status="approved",
                approved_at=now,
            )
            db.add(job)
            db.commit()
            created_count += 1
            
        print(f"✅ Successfully seeded {created_count} job vacancies for {wa_number}")
        
    except Exception as e:
        db.rollback()
        print(f"Error seeding jobs: {repr(e)}")
    finally:
        db.close()

if __name__ == "__main__":
    seed()
