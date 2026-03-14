"""
Template message builders.
Each function returns the 'components' list (or full kwargs) for wa_client.send_template()
or the arguments for wa_client.send_buttons().
These are plain Python dicts – no WhatsApp API call is made here.
"""
from typing import Any

from app.db.models import Candidate, JobVacancy, Recruiter, CandidateApplication
from app.config import get_settings

settings = get_settings()


# ─── Recruiter templates ─────────────────────────────────────────────────────

def recruiter_welcome_components(recruiter: Recruiter) -> list[dict]:
    """
    Utility template: shows recruiter business info + 2 buttons.
    Template name (on Meta): jobinfo_welcome_recruiter
    Variables: {{1}} = company_name, {{2}} = business_type, {{3}} = location
    """
    return [
        {
            "type": "body",
            "parameters": [
                {"type": "text", "text": recruiter.company_name},
                {"type": "text", "text": recruiter.business_type or "—"},
                {"type": "text", "text": recruiter.location or "—"},
            ],
        },
        {
            "type": "button",
            "sub_type": "quick_reply",
            "index": "0",
            "parameters": [{"type": "payload", "payload": "btn_post_vacancy"}],
        },
        {
            "type": "button",
            "sub_type": "quick_reply",
            "index": "1",
            "parameters": [{"type": "payload", "payload": "btn_my_vacancies"}],
        },
    ]


def vacancy_confirmation_body(vacancy: JobVacancy) -> str:
    return (
        f"✅ *Vacancy Posted Successfully!*\n\n"
        f"*Job Code:* {vacancy.job_code}\n"
        f"*Title:* {vacancy.title}\n"
        f"*Location:* {vacancy.location}\n\n"
        f"Your vacancy is under review. You'll be notified once it's approved.\n\n"
        f"_JobInfo – Connecting Kerala's talent_"
    )


def admin_vacancy_alert_body(vacancy: JobVacancy, recruiter: Recruiter) -> str:
    return (
        f"🔔 *New Vacancy Submitted – Action Required*\n\n"
        f"*Job Code:* {vacancy.job_code}\n"
        f"*Title:* {vacancy.title}\n"
        f"*Company:* {vacancy.company or recruiter.company_name or '—'}\n"
        f"*Location:* {vacancy.location}\n"
        f"*Recruiter:* {recruiter.company_name} ({recruiter.wa_number})\n\n"
        f"*Description:*\n{vacancy.description or '—'}\n\n"
        f"👉 Approve/Reject at: {settings.app_base_url}/admin/vacancies"
    )


def vacancy_approved_body(vacancy: JobVacancy) -> str:
    return (
        f"🎉 *Vacancy Approved!*\n\n"
        f"*{vacancy.title}* ({vacancy.job_code}) has been approved and is now live.\n\n"
        f"Job seekers can apply via:\n"
        f"https://wa.me/{settings.business_wa_number}?text=Apply%20{vacancy.job_code}\n\n"
        f"_JobInfo_"
    )


def vacancy_rejected_body(vacancy: JobVacancy) -> str:
    return (
        f"❌ *Vacancy Not Approved*\n\n"
        f"*{vacancy.title}* ({vacancy.job_code}) could not be approved.\n\n"
        f"*Reason:* {vacancy.rejection_reason}\n\n"
        f"Please review and resubmit via jobinfo.club or WhatsApp.\n_JobInfo_"
    )


# ─── Job seeker templates ────────────────────────────────────────────────────

def application_confirmation_body(
    candidate: Candidate,
    vacancy: JobVacancy,
) -> str:
    return (
        f"✅ *Application Submitted!*\n\n"
        f"Hi {candidate.name},\n\n"
        f"You have successfully applied for:\n"
        f"*{vacancy.title}* at *{vacancy.company or '—'}*\n"
        f"*Location:* {vacancy.location}\n\n"
        f"We'll notify you of any updates. Good luck! 🍀\n\n"
        f"_JobInfo_"
    )


def plan_renewal_body(candidate: Candidate) -> str:
    return (
        f"⚠️ *No Active Plan*\n\n"
        f"Hi {candidate.name}, your subscription has expired or you've used all "
        f"your applications.\n\n"
        f"Renew your plan to keep applying:\n\n"
        f"💰 *Basic* – ₹99 (30 days, 50 applications)\n"
        f"⭐ *Popular* – ₹299 (60 days, 100 applications)\n"
        f"🚀 *Advanced* – ₹499 (60 days, unlimited)\n\n"
        f"Reply with *RENEW* or visit jobinfo.club to upgrade.\n_JobInfo_"
    )


def registration_confirmation_body(name: str, user_type: str = "candidate") -> str:
    if user_type == "recruiter":
        return (
            f"🎉 *Welcome to JobInfo!*\n\n"
            f"*{name}* is now registered as a recruiter. You can post vacancies and "
            f"reach talent directly via WhatsApp.\n\n"
            f"_JobInfo – Connecting talent across Kerala, Karnataka & GCC_"
        )
    return (
        f"🎉 *Registration Successful, {name}!*\n\n"
        f"You're now part of JobInfo! Stay tuned to our WhatsApp channel "
        f"for the latest vacancies.\n\n"
        f"📢 Join the channel: https://whatsapp.com/channel/jobinfo\n\n"
        f"_JobInfo – Connecting Kerala's talent_"
    )


def seeker_job_detail_body(vacancy: JobVacancy) -> str:
    return (
        f"📋 *Job Details*\n\n"
        f"*Title:* {vacancy.title}\n"
        f"*Company:* {vacancy.company or '—'}\n"
        f"*Location:* {vacancy.location}\n"
        f"*Experience:* {vacancy.experience_required or '—'}\n"
        f"*Salary:* {vacancy.salary_range or 'Not disclosed'}\n\n"
        f"*Description:*\n{vacancy.description or '—'}\n\n"
        f"Ready to apply? Tap *Apply Now* below."
    )


def cv_update_confirmation_body(candidate: Candidate) -> str:
    return (
        f"✅ *CV Updated Successfully!*\n\n"
        f"Hi {candidate.name}, your CV has been updated.\n\n"
        f"Your new CV will be used for future applications.\n_JobInfo_"
    )
