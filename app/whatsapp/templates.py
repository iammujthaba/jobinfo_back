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
    "10k_15k":          "\u20b910,000 - \u20b914,999",
    "15k_20k":          "\u20b915,000 - \u20b919,999",
    "20k_25k":          "\u20b920,000 - \u20b924,999",
    "25k_30k":          "\u20b925,000 - \u20b929,999",
    "30k_35k":          "\u20b930,000 - \u20b934,999",
    "35k_40k":          "\u20b935,000 - \u20b939,999",
    "40k_45k":          "\u20b940,000 - \u20b944,999",
    "45k_50k":          "\u20b945,000 - \u20b949,999",
    "50k_60k":          "\u20b950,000 - \u20b959,999",
    "60k_70k":          "\u20b960,000 - \u20b969,999",
    "70k_80k":          "\u20b970,000 - \u20b979,999",
    "80k_90k":          "\u20b980,000 - \u20b989,999",
    "90k_100k":         "\u20b990,000 - \u20b999,999",
    "100k_125k":        "\u20b91,00,000 - \u20b91,24,999",
    "125k_150k":        "\u20b91,25,000 - \u20b91,49,999",
    "150k_175k":        "\u20b91,50,000 - \u20b91,74,999",
    "175k_200k":        "\u20b91,75,000 - \u20b91,99,999",
    "above_200k":       "Above \u20b92,00,000",
    "above_250k":       "Above \u20b92,50,000",
    "above_300k":       "Above \u20b93,00,000",
    # Legacy fallbacks
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
    "company":          "Company / Pvt Ltd",
    "shop_retail":      "Shop / Supermarket / Textiles",
    "hotel_bakery":     "Hotel / Restaurant / Bakery",
    "healthcare":       "Hospital / Clinic / Pharmacy",
    "education":        "School / College / Coaching",
    "salon_spa":       "Salon / Beauty Parlour / Spa",
    "finance_bank":     "Finance / Co-operative Bank",
    "it_media":         "IT / Media / Printing Studio",
    "contractor":       "Contractor / Builder",
    "transport":        "Travels / Transport / Logistics",
    "workshop_garage":  "Workshop / Garage",
    "petrol_pump":      "Petrol Pump / Gas Station",
    "agency":           "HR / Recruitment / Consultancy",
    "individual":       "Individual / Household",
    "other":            "Other"
}

REGISTRANT_ROLE_LABELS: dict[str, str] = {
    "founder":          "Founder / Owner",
    "hr":               "HR / Recruiter",
    "manager":          "Manager",
    "employee":         "Employee",
    "other":            "Other"
}


def _label(mapping: dict[str, str], raw_value: str | None, fallback: str = "—") -> str:
    """Translate a raw DB slug to a human-readable label.

    Falls back to the raw value itself if the slug is not in the mapping,
    and to *fallback* if *raw_value* is None/empty.
    """
    if not raw_value:
        return fallback
    return mapping.get(raw_value, raw_value)


def _truncate(text: str | None, max_len: int = 600) -> str:
    """Return *text* safely truncated to *max_len* characters.

    Appends '...' when the text is cut.  Returns '—' for None/empty input.
    """
    if not text:
        return "—"
        
    # Safety rail: Meta API restricts template parameters to 1024 characters.
    # We strictly enforce a ceiling of 1000 characters here to prevent API rejection.
    if max_len > 1000:
        max_len = 1000

    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


# ─── Recruiter templates ─────────────────────────────────────────────────────

def recruiter_workspace_body(
    recruiter: Recruiter,
    db: Any = None,
    active_vacancies: int | None = None,
    paused_vacancies: int | None = None,
    total_applicants: int | None = None,
) -> str:
    """
    In-code interactive card for returning recruiters.
    Displays verified profile details, registered WhatsApp account ID,
    and live hiring metrics with interactive buttons.
    """
    company = recruiter.company_name.strip() if recruiter.company_name else "Employer"
    business = _label(BUSINESS_TYPE_LABELS, recruiter.business_type)
    location = recruiter.location or "Kerala"

    cleaned_num = (recruiter.wa_number or "").replace("+", "").replace(" ", "").strip()
    if cleaned_num.startswith("91") and len(cleaned_num) == 12:
        wa_display = cleaned_num[2:]
    elif cleaned_num:
        wa_display = cleaned_num
    else:
        wa_display = "—"

    if db is not None:
        if active_vacancies is None:
            active_vacancies = (
                db.query(JobVacancy)
                .filter_by(recruiter_id=recruiter.id, status="approved", is_active=True)
                .count()
            )
        if paused_vacancies is None:
            paused_vacancies = (
                db.query(JobVacancy)
                .filter(
                    JobVacancy.recruiter_id == recruiter.id,
                    (JobVacancy.is_active == False) | (JobVacancy.status == "rejected")
                )
                .count()
            )
        if total_applicants is None:
            total_applicants = (
                db.query(CandidateApplication)
                .join(JobVacancy, CandidateApplication.vacancy_id == JobVacancy.id)
                .filter(JobVacancy.recruiter_id == recruiter.id)
                .count()
            )
    else:
        active_vacancies = active_vacancies or 0
        paused_vacancies = paused_vacancies or 0
        total_applicants = total_applicants or 0

    return (
        f"👋 *Welcome Back, {company}!* \n\n"
        f"_Connecting you with Kerala's Premier WhatsApp Hiring Network_\n\n"
        f"📋 *Profile Details:*\n"
        f"• 💼 Business: {business}\n"
        f"• 📍 Location: {location}\n"
        f"• 🛡️ Status: Employer\n"
        f"• 📱 Account ID : {wa_display}\n\n"

        f"📊 *Live Dashboard Status:*\n"
        f"• 🟢 Active Vacancies: {active_vacancies}\n"
        f"• ⏸️ Paused / Closed: {paused_vacancies}\n"
        f"• 📥 Total Applicants: {total_applicants}\n\n"
        f"How can I assist you today? Tap an option below 👇"
    )


def recruiter_vacancies_overview_body(recruiter: Recruiter, db: Any) -> str:
    """
    Live vacancies overview card (Option 3 Spotlight).
    Displays real-time hiring snapshot and spotlights the recruiter's latest vacancy.
    """
    company = recruiter.company_name.strip() if recruiter.company_name else "Employer"

    all_vacancies = (
        db.query(JobVacancy)
        .filter(JobVacancy.recruiter_id == recruiter.id)
        .order_by(JobVacancy.created_at.desc())
        .all()
    )

    if not all_vacancies:
        return (
            f"📋 *Vacancies Overview — {company}*\n\n"
            f"You haven’t posted any job vacancies yet!\n\n"
            f"Post your vacancy in less than 1 minute to start receiving applications "
            f"from verified candidates across Kerala.\n\n"
            f"Tap the button below to view your dashboard 👇"
        )

    active_count = sum(1 for v in all_vacancies if v.status == "approved" and v.is_active)
    pending_count = sum(1 for v in all_vacancies if v.status == "pending")
    paused_count = sum(1 for v in all_vacancies if (not v.is_active and v.status == "approved") or v.status == "rejected")

    total_apps = (
        db.query(CandidateApplication)
        .join(JobVacancy, CandidateApplication.vacancy_id == JobVacancy.id)
        .filter(JobVacancy.recruiter_id == recruiter.id)
        .count()
    )

    latest_job = all_vacancies[0]

    if latest_job.status == "approved" and latest_job.is_active:
        status_line = "🟢 Status: Active & Broadcasting"
    elif latest_job.status == "approved" and not latest_job.is_active:
        status_line = "⏸️ Status: Paused / Closed"
    elif latest_job.status == "pending":
        status_line = "⏳ Status: Under Review (Admin Verification)"
    elif latest_job.status == "rejected":
        status_line = "❌ Status: Rejected (Needs Revision)"
    else:
        status_line = f"📊 Status: {latest_job.status.capitalize()}"

    loc_parts = [p.strip() for p in [latest_job.exact_location, latest_job.district_region] if p and p.strip()]
    loc_str = ", ".join(loc_parts) if loc_parts else "Kerala"

    latest_apps = (
        db.query(CandidateApplication)
        .filter(CandidateApplication.vacancy_id == latest_job.id)
        .count()
    )

    return (
        f"📋 *Vacancies Overview — {company}*\n\n"
        f"📊 *Hiring Snapshot:*\n"
        f"• 🟢 Active Vacancy: {active_count}\n"
        f"• ⏳ Pending Vacancy: {pending_count}\n"
        f"• ⏸️ Paused Vacancy: {paused_count}\n"
        f"• 📥 Total Applicants Received: {total_apps}\n\n"
        f"📌 *Latest Posted Vacancy:*\n"
        f"💼 *{latest_job.job_title.strip()}* ({latest_job.job_code})\n"
        f"📍 {loc_str}\n"
        f"{status_line}\n"
        f"📥 Applications: {latest_apps} candidates\n\n"
        f"Tap the button below to review all applicants details, their CVs and shortlist them for interview 👇"
    )


def recruiter_welcome_components(recruiter: Recruiter, token: str) -> list[dict]:
    """
    Utility template: shows recruiter business info + 2 buttons.
    Template name (on Meta): jobinfo_welcome_recruiter_v3
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
    """
    Status confirmation and edit guidance sent with the 'View Dashboard' CTA button.
    """
    return (
        f"🛡️ *Quality & Spam Protection*\n"
        f"All vacancies undergo a quick verification before broadcasting, to keep the platform 100% genuine and spam-free.\n\n"
        f"✏️ *Spotted any typos?*\n"
        f"You can tap the button below to edit your vacancy before broadcasting.\n\n"
        f"⏳ _If everything looks good, sit back and relax! I will notify you once it goes live._ "
    )


def admin_vacancy_alert_body(vacancy: JobVacancy, recruiter: Recruiter) -> str:
    role_str = f"*Role:* {_label(REGISTRANT_ROLE_LABELS, getattr(recruiter, 'registrant_role', 'other'))}\n"
    return (
        f"🔔 *New Vacancy Submitted – Action Required*\n\n"
        f"*Job Code:* {vacancy.job_code}\n"
        f"*Position:* {vacancy.job_title}\n"
        f"*Company:* {recruiter.company_name or '—'}\n"
        f"*Location:* {vacancy.exact_location}, {vacancy.district_region}\n"
        f"*Recruiter:* {recruiter.company_name}\n"
        f"{role_str}"
        f"*Contact:* {recruiter.business_contact or 'None'}\n"
        f"*Whatsapp:* {recruiter.wa_number}\n\n"
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
    description = _truncate(vacancy.job_description, 600)
    cv_note     = "Yes – CV required" if vacancy.cv_required else "No – CV optional"

    link = apply_url or f"{settings.app_base_url}/api/apply/{vacancy.job_code}"

    return (
        f"🚀 *New Job Alert*\n\n"
        f"🏷️ Position: *{vacancy.job_title.strip()}*\n"
        f"🏢 Company: {vacancy.recruiter.company_name if vacancy.recruiter else '—'}\n"
        f"📍 Location: {vacancy.exact_location or '—'}, {vacancy.district_region or '—'}\n"
        f"💰 Salary: {salary}\n"
        f"💼 Mode: {job_mode}\n"
        f"🎓 Experience: {experience}\n"
        f"📄 CV Required: {cv_note}\n"
        f"🔖 Job Code: {vacancy.job_code}\n\n"
        f"📋 *About the Role:*\n{description}\n\n"
        f"👉 _Click *\"Start chatting\"* or use the link to Apply_: {link}\n\n"
        f"👥 Join WhatsApp Groups: https://chat.whatsapp.com/B55NA0tQ76Z0nP2tEoQtiR \n\n"
        f"_jobinfo - Kerala's First WhatsApp powered Career Portal_"
    )



def vacancy_rejected_body(vacancy: JobVacancy) -> str:
    title = (vacancy.job_title or "").strip()
    code = (vacancy.job_code or "").strip()
    reason_str = f"📝 *Feedback from Quality Team:*\n\"{vacancy.rejection_reason.strip()}\"\n\n" if vacancy.rejection_reason else ""
    return (
        "⚠️ *Update on Your Vacancy Submission*\n\n"
        f"Your vacancy posting for *{title}* ({code}) requires a few adjustments before it can be published:\n\n"
        f"{reason_str}"
        "💡 *Next Steps:*\n"
        "You can easily make the correction and resubmit your vacancy directly from your dashboard. "
        "Once updated, our team will re-verify your vacancy!\n\n"
        "💬 *Need to clarify?* \nIf you believe this was flagged by mistake or have questions, message our team directly:\n"
        "👉 *+91 70259 62179*\n\n"
        "Otherwise, tap below to edit and resubmit 👇"
    )


def vacancy_poster_preview_body(vacancy: JobVacancy) -> str:
    """
    Live Preview of the vacancy poster exactly as it will appear when published.
    Sent as a standard text message for full-width bubble rendering with
    untruncated full 'About the Role' job description.
    """
    salary = _label(SALARY_LABELS, vacancy.salary_range, fallback="Not disclosed")
    experience = _label(EXPERIENCE_LABELS, vacancy.experience_required)
    job_mode = _label(JOB_MODE_LABELS, vacancy.job_mode)
    company = vacancy.recruiter.company_name if vacancy.recruiter and vacancy.recruiter.company_name else "—"
    cv_note = "Yes – CV required" if vacancy.cv_required else "No – CV optional"
    description = vacancy.job_description.strip() if vacancy.job_description else "—"

    return (
        f"✅ *Vacancy Submitted Successfully!*\n\n"
        f"Your vacancy is *under review*. You'll be notified as soon as it is approved.\n\n"
        f"👀 *_Preview of Your Job Poster:_*\n"
        f"{'─' * 25}\n"
        f"🏷️ Position: *{vacancy.job_title.strip()}*\n"
        f"🏢 Company: {company}\n"
        f"📍 Location: {vacancy.exact_location or '—'}, {vacancy.district_region or '—'}\n"
        f"💰 Salary: {salary}\n"
        f"💼 Mode: {job_mode}\n"
        f"🎓 Experience: {experience}\n"
        f"📄 CV Required: {cv_note}\n"
        f"🔖 Job Code: {vacancy.job_code}\n\n"
        f"📋 *About the Role:*\n"
        f"{description}\n"
        f"{'─' * 25}\n"
        f"_📝 Click the 'View Dashboard' button below if you want to edit vacancy details._"
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
        f"*{vacancy.job_title.strip()}* Position at *{vacancy.recruiter.company_name.strip() if vacancy.recruiter and vacancy.recruiter.company_name else '—'}* in *{vacancy.exact_location or '—'}*, *{vacancy.district_region or '—'}*\n\n"
        f"We'll notify you of any updates. Good luck! 🍀\n\n"
        f"_JobInfo – Connecting Kerala's talent_"
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


def recruiter_post_vacancy_card_body(
    company_name: str = "",
    is_new: bool = False,
    vacancy_count: int = 0,
    from_workspace: bool = True,
) -> str:
    """
    Master template for recruiter post-vacancy flow card.
    Dynamically adjusts greeting and call-to-action between new onboarding
    ('first vacancy') and returning recruiters ('new vacancy').
    """
    company = company_name.strip() if company_name and company_name.strip() else "Employer"

    if is_new or vacancy_count == 0:
        header = f"*Welcome aboard, {company}!* 🏢"
        instruction = "Tap the button below to post your first vacancy. Takes less than 1 minute! Let’s get started.✨"
    elif from_workspace:
        header = f"🚀 *Awesome! Go ahead, {company}!*" if company and company != "Employer" else "🚀 *Awesome! Go ahead!*"
        instruction = "Tap the button below to fill your vacancy details. Takes less than 1 minute! Let’s get started.✨"
    else:
        header = f"*Post a New Vacancy, {company}!* 📢" if company and company != "Employer" else "*Post a New Vacancy!* 📢"
        instruction = "Tap the button below to post your vacancy. Takes less than 1 minute! Let’s get started.✨"

    return (
        f"{header}\n\n"
        f"_Hire Kerala's best talent directly on WhatsApp!_\n\n"
        f"✨ *100% Free Posting*\n"
        f"⚡ *Fast & Simple Hiring*\n"
        f"🔒 *Number Stays 100% Private*\n"
        f"🎯 *Smart Candidate Filtering*\n"
        f"📥 *Manage Applicants & CVs in Dashboard*\n\n"
        f"{instruction}"
    )


def registration_confirmation_body(name: str, user_type: str = "candidate") -> str:
    if user_type == "recruiter":
        return recruiter_post_vacancy_card_body(company_name=name, is_new=True, vacancy_count=0)
    return (
        f"🎉 *Registration Successful, {name}!* 🎓✨\n\n"
        f"You're now part of JobInfo Kerala!\n\n"
        f"Stay tuned to our verified WhatsApp channel for the latest daily job postings across Kerala.\n\n"
        f"📢 Join the channel: https://whatsapp.com/channel/0029VbBrkDB8fewxd9QIMA2k\n\n"
        f"_JobInfo – Connecting Kerala's talent_"
    )



def seeker_job_detail_body(vacancy: JobVacancy) -> str:
    salary      = _label(SALARY_LABELS,     vacancy.salary_range,       fallback="Not disclosed")
    experience  = _label(EXPERIENCE_LABELS, vacancy.experience_required)
    job_mode    = _label(JOB_MODE_LABELS,   vacancy.job_mode)
    description = _truncate(vacancy.job_description, 600)
    return (
        f"✅ *You're Ready to Apply!*\n\n"
        f"🏷️ Position: *{vacancy.job_title.strip()}*\n"
        f"🏢 Company: {vacancy.recruiter.company_name if vacancy.recruiter else '—'}\n"
        f"📍 Location: {vacancy.exact_location or '—'}, {vacancy.district_region or '—'}\n"
        f"💰 Salary: {salary}\n"
        f"💼 Mode: {job_mode}\n"
        f"🎓 Experience: {experience}\n\n"
        f"📋 *About the Role:*\n{description}\n\n"
        f"_JobInfo.pro – Kerala's First WhatsApp powered Career Portal_"
    )


def cv_update_confirmation_body(candidate: Candidate) -> str:
    return (
        f"✅ *CV Updated Successfully!*\n\n"
        f"Hi {candidate.name}, your CV has been updated.\n\n"
        f"Your new CV will be used for future applications.\n_JobInfo_"
    )
