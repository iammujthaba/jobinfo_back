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


# ─── Slug-to-label translation maps ──────────────────────────────────────────

EXPERIENCE_LABELS: dict[str, str] = {
    "no_experience":    "No Experience Required",
    "fresher_or_exp":   "Fresher or Experienced",
    "1_2_years":        "1-2 Years",
    "3_5_years":        "3-5 Years",
    "5_plus_years":     "5+ Years",
}

SALARY_LABELS: dict[str, str] = {
    "interview_based":  "Based on Interview",
    "not_mentioned":    "Not Mentioned",
    "stipend":          "Stipend",
    "below_10k":        "Below \u20b910,000",
    "10k_20k":          "\u20b910,000 - \u20b920,000",
    "20k_30k":          "\u20b920,000 - \u20b930,000",
    "30k_40k":          "\u20b930,000 - \u20b940,000",
    "40k_50k":          "\u20b940,000 - \u20b950,000",
    "above_50k":        "Above \u20b950,000",
}

JOB_MODE_LABELS: dict[str, str] = {
    "full_time":        "Full-Time",
    "part_time":        "Part-Time",
    "remote":           "Remote",
    "hybrid":           "Hybrid",
}

BUSINESS_TYPE_LABELS: dict[str, str] = {
    "shop_retail":      "Shop / Retail",
    "hotel_bakery":     "Hotel / Bakery",
    "contractor":       "Contractor / Builder",
    "individual":       "Individual / Household",
    "petrol_pump":      "Petrol Pump",
    "workshop_garage":  "Workshop / Garage",
    "transport":        "Transport / Logistics",
    "agency":           "Agency / Consultancy",
    "company":          "Company / Pvt Ltd",
    "other":            "Other",
}


def _label(mapping: dict[str, str], raw_value: str | None, fallback: str = "—") -> str:
    """Translate a raw DB slug to a human-readable label.

    Falls back to the raw value itself if the slug is not in the mapping,
    and to *fallback* if *raw_value* is None/empty.
    """
    if not raw_value:
        return fallback
    return mapping.get(raw_value, raw_value)


def _truncate(text: str | None, max_len: int = 200) -> str:
    """Return *text* safely truncated to *max_len* characters.

    Appends '...' when the text is cut.  Returns '—' for None/empty input.
    """
    if not text:
        return "—"
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


# ─── Recruiter templates ─────────────────────────────────────────────────────

def recruiter_welcome_components(recruiter: Recruiter, token: str) -> list[dict]:
    """
    Utility template: shows recruiter business info + 2 buttons.
    Template name (on Meta): jobinfo_welcome_recruiter_v2
    Variables: {{1}} = company_name, {{2}} = business_type, {{3}} = location
    """
    return [
        {
            "type": "body",
            "parameters": [
                {"type": "text", "text": recruiter.company_name},
                {"type": "text", "text": _label(BUSINESS_TYPE_LABELS, recruiter.business_type)},
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
            "sub_type": "url",
            "index": "2",
            "parameters": [{"type": "text", "text": token}],
        },
    ]


def vacancy_confirmation_body(vacancy: JobVacancy) -> str:
    return (
        f"✅ *Vacancy Posted Successfully!*\n\n"
        f"*Title:* {vacancy.job_title}\n"
        f"*Location:* {vacancy.exact_location},{vacancy.district_region}\n"
        f"*Job Code:* {vacancy.job_code}\n"
        f"*Status:* {vacancy.status}⏳\n\n"
        f"Your vacancy is *under review*. You'll be notified once it's approved.\n\n"
        f"_JobInfo – Connecting Kerala's talent_"
    )


def admin_vacancy_alert_body(vacancy: JobVacancy, recruiter: Recruiter) -> str:
    return (
        f"🔔 *New Vacancy Submitted – Action Required*\n\n"
        f"*Job Code:* {vacancy.job_code}\n"
        f"*Title:* {vacancy.job_title}\n"
        f"*Company:* {recruiter.company_name or '—'}\n"
        f"*Location:* {vacancy.exact_location}, {vacancy.district_region}\n"
        f"*Recruiter:* {recruiter.company_name}\n"
        f"*Contact:* {recruiter.business_contact or 'None'}\n"
        f"*Whatsapp:* {recruiter.wa_number}\n\n"
        f"*Description:*\n{vacancy.job_description or '—'}\n"
    )


def vacancy_approved_body(vacancy: JobVacancy) -> str:
    salary      = _label(SALARY_LABELS,     vacancy.salary_range)
    experience  = _label(EXPERIENCE_LABELS, vacancy.experience_required)
    job_mode    = _label(JOB_MODE_LABELS,   vacancy.job_mode)
    description = _truncate(vacancy.job_description, 200)
    return (
        f"🎉 *Vacancy Approved!*\n\n"
        f"*{vacancy.job_title.strip()}* ({vacancy.job_code}) has been approved and is now live.\n\n"
        f"💼 *Mode:* {job_mode}\n"
        f"🎓 *Experience:* {experience}\n"
        f"💰 *Salary:* {salary}\n\n"
        f"📋 *Description:*\n{description}\n\n"
        f"Job seekers can apply via:\n"
        f"{settings.app_base_url}/api/apply/{vacancy.job_code}\n\n"
        f"_JobInfo_"
    )


def job_alert_text_body(vacancy: JobVacancy, apply_url: str | None = None, is_admin: bool = False) -> str:
    """
    Forwardable plain-text job card sent on vacancy approval.

    ``apply_url`` lets the caller inject the right link per recipient:
      - Recruiter  → {settings.app_base_url}/api/apply/{job_code}  (clean redirect, survives forwarding)
      - Admin/Channel → wa.me deep-link  (triggers WhatsApp's native Apply button)

    Falls back to the redirect URL if not supplied.
    """
    salary      = _label(SALARY_LABELS,     vacancy.salary_range)
    experience  = _label(EXPERIENCE_LABELS, vacancy.experience_required)
    job_mode    = _label(JOB_MODE_LABELS,   vacancy.job_mode)
    description = _truncate(vacancy.job_description, 200)

    link = apply_url or f"{settings.app_base_url}/api/apply/{vacancy.job_code}"

    cta_text = (
        "_Tap the link below or click the 'Start Chatting' button to apply this job!_"
        if is_admin else
        "_Tap the link below to apply this position!_"
    )

    return (
        f"🚀 *Jobinfo - New Job Alert*\n\n"
        f"🏷️ Position: *{vacancy.job_title.strip()}*\n"
        f"🏢 Company: {vacancy.recruiter.company_name if vacancy.recruiter else '—'}\n"
        f"📍 Location: {vacancy.exact_location or '—'}, {vacancy.district_region or '—'}\n"
        f"💰 Salary: {salary}\n"
        f"💼 Mode: {job_mode}\n"
        f"🎓 Experience: {experience}\n"
        f"🔖 Job Code: {vacancy.job_code}\n\n"
        f"📋 *About the Role:*\n{description}\n\n"
        f"{cta_text}\n"
        f"📲 Apply now: {link}\n\n"
        f"_JobInfo.pro – Kerala's First WhatsApp powered Career Portal_"
    )



def vacancy_rejected_body(vacancy: JobVacancy) -> str:
    return (
        f"❌ Your vacancy for *{vacancy.job_title.strip()}* has been rejected.\n\n"
        f"Please contact support for details.\n_JobInfo_"
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
        f"*{vacancy.job_title.strip()}* at *{vacancy.recruiter.company_name.strip() if vacancy.recruiter and vacancy.recruiter.company_name else '—'}*\n"
        f"*Location:* {vacancy.district_region}\n\n"
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
        f"Reply with *RENEW* or visit jobinfo.pro to upgrade.\n_JobInfo_"
    )


def registration_confirmation_body(name: str, user_type: str = "candidate") -> str:
    if user_type == "recruiter":
        return (
            f"✅ *Registration Successful!*\n\n"
            f"*{name.strip()}* is now registered as a _recruiter_. You can post vacancies and "
            f"reach Kerala's talent directly via WhatsApp.\n\n"
            f"Tap the *Post Vacancy* button below to post your first vacancy and start hiring instantly."
        )
    return (
        f"🎉 *Registration Successful, {name}!*\n\n"
        f"You're now part of JobInfo! Stay tuned to our WhatsApp channel "
        f"for the latest vacancies.\n\n"
        f"📢 Join the channel: https://whatsapp.com/channel/jobinfo\n\n"
        f"_JobInfo – Connecting Kerala's talent_"
    )


def seeker_job_detail_body(vacancy: JobVacancy) -> str:
    salary      = _label(SALARY_LABELS,     vacancy.salary_range,       fallback="Not disclosed")
    experience  = _label(EXPERIENCE_LABELS, vacancy.experience_required)
    job_mode    = _label(JOB_MODE_LABELS,   vacancy.job_mode)
    description = _truncate(vacancy.job_description, 200)
    return (
        f"📋 *Job Details*\n\n"
        f"*Title:* {vacancy.job_title}\n"
        f"*Company:* {vacancy.recruiter.company_name if vacancy.recruiter else '—'}\n"
        f"*Location:* {vacancy.district_region}\n"
        f"*Mode:* {job_mode}\n"
        f"*Experience:* {experience}\n"
        f"*Salary:* {salary}\n\n"
        f"*Description:*\n{description}\n\n"
        f"Ready to apply? Tap *Apply Now* below."
    )


def cv_update_confirmation_body(candidate: Candidate) -> str:
    return (
        f"✅ *CV Updated Successfully!*\n\n"
        f"Hi {candidate.name}, your CV has been updated.\n\n"
        f"Your new CV will be used for future applications.\n_JobInfo_"
    )
