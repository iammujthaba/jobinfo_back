"""
Job Seeker conversation handler (state machine).
Manages the full job seeker lifecycle:
  - Apply link tap → check if registered
  - New seeker: register or gethelp
  - Registered: check active plan → show job + apply/update CV buttons
  - Handle flow completions for registration and CV update
  - Application submission
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (
    Candidate, Recruiter, CandidateApplication, CandidateResume, GetHelpRequest,
    ConversationState, JobVacancy, SubscriptionPlan, SubscriptionPlanName,
    MAX_CANDIDATE_RESUMES, MagicLink
)
from app.services.storage import save_cv_from_whatsapp
from app.whatsapp.client import wa_client
from app.whatsapp.templates import (
    application_confirmation_body,
    _resume_display_name,
    cv_update_confirmation_body,
    plan_renewal_body,
    position_closed_body,
    registration_confirmation_body,
    seeker_job_detail_body,
    seeker_apply_sweet_spot_body,
    seeker_apply_smart_switch_body,
    seeker_apply_cv_recommendation_body,
    seeker_apply_cv_mismatch_optional_body,
    seeker_apply_cv_optional_body,
    seeker_apply_relocation_body,
    seeker_apply_no_cv_mandatory_body,
    _label,
    SALARY_LABELS,
    JOB_MODE_LABELS,
    EXPERIENCE_LABELS,
)
from app.services.milestone import dispatch_milestone_notification

logger = logging.getLogger(__name__)
settings = get_settings()

# Friendly display names for category keys

# Friendly display names for category keys
CATEGORY_DISPLAY_NAMES: dict[str, str] = {
    "retail": "Retail & Showrooms",
    "sales_business": "Sales & Business Executive",
    "hospitality": "Hospitality & Food Service",
    "healthcare": "Healthcare & Caretaking",
    "education": "Education & Academic Advisor",
    "office_data_entry": "Office Admin & Data Entry",
    "front_office": "Receptionist & Front Office",
    "finance_accounts": "Accountant & Billing Staff",
    "hr_management": "HR, Branch Manager & Team Lead",
    "telecalling": "Telecaller & Customer Support",
    "it_digital_marketing": "IT & Digital Marketing",
    "logistics_store": "Driving, Logistics & Store Keeper",
    "beauty_wellness": "Beauty & Wellness",
    "maintenance_technician": "Maintenance & Technician",
    "construction_labor": "Construction & Manual Labor",
    "gulf_abroad": "Gulf / Abroad Jobs",
    "other": "Other / General",
}

WHATSAPP_CHANNEL_URL = "https://whatsapp.com/channel/0029VbBrkDB8fewxd9QIMA2k"


async def send_position_closed_message(wa_number: str) -> None:
    """Send standard 'Position No Longer Available' message with next action buttons."""
    await wa_client.send_buttons(
        to=wa_number,
        header_text="Position No Longer Available",
        body_text=position_closed_body(),
        buttons=[
            {"id": "ACTION_SUGGEST_JOBS", "title": "🎯 Suggest Jobs"},
            {"id": "ACTION_EXPLORE_JOBS", "title": "🌐 Explore Channel"},
        ],
    )

DISTRICT_ALIAS_MAP: dict[str, str] = {
    "trivandrum": "Thiruvananthapuram",
    "tvm": "Thiruvananthapuram",
    "thiruvananthapuram": "Thiruvananthapuram",
    "kochi": "Ernakulam",
    "ernakulam": "Ernakulam",
    "kozhikode": "Kozhikode",
    "calicut": "Kozhikode",
    "kollam": "Kollam",
    "thrissur": "Thrissur",
    "malappuram": "Malappuram",
    "kottayam": "Kottayam",
    "palakkad": "Palakkad",
    "alappuzha": "Alappuzha",
    "kannur": "Kannur",
    "kasaragod": "Kasaragod",
    "idukki": "Idukki",
    "wayanad": "Wayanad",
    "pathanamthitta": "Pathanamthitta",
    "gcc": "Other",
    "gulf": "Other",
}

FLOW_VALID_DISTRICTS: set[str] = {
    "Thiruvananthapuram", "Kollam", "Pathanamthitta", "Alappuzha",
    "Kottayam", "Idukki", "Ernakulam", "Thrissur", "Palakkad",
    "Malappuram", "Kozhikode", "Wayanad", "Kannur", "Kasaragod", "Other"
}

CATEGORY_ALIAS_MAP: dict[str, str] = {
    "office_admin": "office_data_entry",
}

FLOW_VALID_CATEGORIES: set[str] = set(CATEGORY_DISPLAY_NAMES.keys())


def normalize_district_for_flow(raw: str | None) -> str | None:
    """Normalize a DB district_region value to a valid Flow dropdown ID."""
    if not raw:
        return None
    mapped = DISTRICT_ALIAS_MAP.get(raw.strip().lower())
    if mapped:
        return mapped
    titled = raw.strip().title()
    if titled in FLOW_VALID_DISTRICTS:
        return titled
    return None


def normalize_category_for_flow(raw: str | None) -> str | None:
    """Normalize a DB job_category value to a valid Flow dropdown ID."""
    if not raw:
        return None
    cleaned = raw.strip().lower()
    mapped = CATEGORY_ALIAS_MAP.get(cleaned, cleaned)
    if mapped in FLOW_VALID_CATEGORIES:
        return mapped
    return None


def _normalize_district(raw: str | None) -> str:
    """Return lowercase normalized district name or raw stripped lowercase."""
    if not raw:
        return ""
    norm = normalize_district_for_flow(raw)
    return norm.lower() if norm else raw.strip().lower()


def _normalize_category(raw: str | None) -> str:
    """Return lowercase normalized category key."""
    if not raw:
        return ""
    norm = normalize_category_for_flow(raw)
    return norm.lower() if norm else raw.strip().lower()


def _get_or_create_state(wa_number: str, db: Session) -> ConversationState:
    state = db.query(ConversationState).filter_by(wa_number=wa_number).first()
    if not state:
        state = ConversationState(wa_number=wa_number, state="idle", context={})
        db.add(state)
        db.commit()
        db.refresh(state)
    return state


def _set_state(wa_number: str, state: str, context: dict, db: Session) -> None:
    rec = _get_or_create_state(wa_number, db)
    rec.state = state
    rec.context = context
    db.commit()


def _has_active_plan(candidate: Candidate) -> bool:
    """Check if subscription is enforced and whether candidate has a valid plan."""
    if not settings.subscription_enabled:
        return True  # Free-for-all during launch phase
    if not candidate.plan_expiry:
        return False
    now = datetime.now(timezone.utc)
    if candidate.plan_expiry < now:
        return False
    plan = candidate.plan
    if plan and plan.max_applications is not None:
        if candidate.applications_used >= plan.max_applications:
            return False
    return True


async def _send_cv_required_message(
    wa_number: str,
    vacancy: JobVacancy,
    job_code: str,
) -> None:
    """
    Sent when vacancy.cv_required is True but the seeker has no CV.
    Prompts them to upload a CV before they can complete the application.
    """
    await wa_client.send_buttons(
        to=wa_number,
        header_text="📄 CV Required for This Role",
        body_text=(
            f"The recruiter requires a CV for the *{vacancy.job_title.strip()}* role.\n\n"
            "Please upload your CV to complete your application. "
            "It only takes a moment and dramatically boosts your chances! 🚀"
        ),
        buttons=[
            {"id": f"UPLOAD_NEW_CV_{job_code}", "title": "📤 Upload New CV"},
            {"id": f"MANAGE_CV_{job_code}", "title": "📁 Choose Existing"},
        ],
        footer_text="Upload once — apply to multiple roles with the same CV",
    )


async def start(wa_number: str, job_code: str, db: Session) -> None:
    """
    Entry point: called when a user taps an apply link (e.g. Apply JC:1002).
    """
    from app.services.ad_lifecycle import ensure_ad_active
    vacancy = db.query(JobVacancy).filter_by(job_code=job_code).first()
    if not vacancy or not ensure_ad_active(vacancy, db):
        await send_position_closed_message(wa_number)
        return

    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()

    if not candidate or not candidate.registration_complete:
        # Fix 0: Direct Flow Launch on Job Apply
        salary = _label(SALARY_LABELS, vacancy.salary_range)
        prefill_district = normalize_district_for_flow(vacancy.district_region)
        prefill_category = normalize_category_for_flow(vacancy.job_category)

        flow_data = {
            "pending_job_code": job_code,
        }
        if prefill_district:
            flow_data["prefill_district"] = prefill_district
        if prefill_category:
            flow_data["prefill_category"] = prefill_category

        company_name = vacancy.recruiter.company_name if vacancy.recruiter else "—"
        location_str = f"{vacancy.exact_location or '—'}, {vacancy.district_region or '—'}"

        await wa_client.send_flow(
            to=wa_number,
            flow_id=settings.FLOW_ID_SEEKER_REGISTER,
            flow_token="seeker_register",
            flow_cta="⚡ Apply Now",
            body_text=(
                f"🚀 *Quick Job Application*\n\n"
                f"_You are applying for:_\n"
                f"💼 *Role:* {vacancy.job_title.strip()}\n"
                f"🏢 *Company:* {company_name}\n"
                f"📍 *Location:* {location_str}\n"
                f"💰 *Salary:* {salary}\n\n"
                "_Fill your basic details below so the employer can review your profile and reach out for an interview!_\n\n"
                "*Why JobInfo?*\n"
                "• 🔒 100% Free & spam-free\n"
                "• ⚡ 1-Minute quick profile setup\n"
                "• 📲 1-Tap apply to all vacancies\n"
                "• 🎯 Directly from the recruiter\n"
                "• 🔔 Job matches for your profile\n\n"
                "Tap the Button below to setup your profile & submit application 👇"
            ),
            flow_action_payload={
                "screen": "SEEKER_REGISTRATION",
                "data": flow_data,
            },
            footer_text="JobInfo.pro - Made for Kerala",
        )
        _set_state(
            wa_number,
            "seeker_registering",
            {
                "pending_job_code": job_code,
                "flow_sent_at": datetime.now(timezone.utc).isoformat(),
            },
            db,
        )
    else:
        await _show_job_apply_prompt(wa_number, candidate, vacancy, db)



async def _show_job_apply_prompt(
    wa_number: str,
    candidate: Candidate,
    vacancy: JobVacancy,
    db: Session,
) -> None:
    """Show registered candidate the job details + Apply Now / Update CV buttons."""
    if not _has_active_plan(candidate):
        # No active plan → renewal message
        await wa_client.send_text(to=wa_number, body=plan_renewal_body(candidate))
        return

    # Already applied?
    existing = (
        db.query(CandidateApplication)
        .filter_by(candidate_id=candidate.id, vacancy_id=vacancy.id)
        .first()
    )
    if existing:
        status_value = str(getattr(existing.status, 'value', existing.status)).title()
        await wa_client.send_buttons(
            to=wa_number,
            body_text=(
                f"ℹ️ *Already Applied*\n\n"
                f"You have already submitted an application for the *{vacancy.job_title.strip()}* position.\n\n"
                f"📌 *Current Status:* _{status_value}_\n\n"
                "Would you like to explore other roles that match your profile?"
            ),
            buttons=[
                {"id": "ACTION_SUGGEST_JOBS", "title": "🎯 Suggest Jobs"},
            ],
        )
        return

    # ── Fetch Candidate Resumes ─────────────────────────────────────────────
    resumes = (
        db.query(CandidateResume)
        .filter_by(candidate_id=candidate.id)
        .order_by(CandidateResume.is_default.desc(), CandidateResume.uploaded_at.desc())
        .all()
    )

    # Safety net: if candidate.cv_path exists but no CandidateResume, backfill one
    if not resumes and candidate.cv_path:
        backfill = CandidateResume(
            candidate_id=candidate.id,
            media_id=candidate.cv_path,
            category_tag=candidate.category or "other",
            is_default=True,
        )
        db.add(backfill)
        db.commit()
        resumes = [backfill]

    # ── Branch A: Zero CVs on file ──────────────────────────────────────────
    if not resumes:
        if vacancy.cv_required:
            # Case 4: CV Required & Zero CVs
            await wa_client.send_buttons(
                to=wa_number,
                body_text=seeker_apply_no_cv_mandatory_body(candidate, vacancy),
                buttons=[
                    {"id": f"UPLOAD_NEW_CV_{vacancy.job_code}", "title": "📤 Upload CV"},
                    {"id": "SUGGEST_JOBS_NO_CV", "title": "🔍 Jobs Without CV"},
                ],
                footer_text=f"Job Code: {vacancy.job_code}",
            )
            _set_state(
                wa_number,
                "seeker_uploading_cv",
                {"vacancy_id": vacancy.id, "job_code": vacancy.job_code},
                db,
            )
            return
        else:
            # Case 5: CV Optional & Zero CVs
            await wa_client.send_buttons(
                to=wa_number,
                body_text=seeker_apply_cv_optional_body(candidate, vacancy),
                buttons=[
                    {"id": f"CONFIRM_APPLY_{vacancy.job_code}", "title": "⚡ Apply Directly"},
                    {"id": f"UPLOAD_NEW_CV_{vacancy.job_code}", "title": "📤 Change / Upload CV"},
                ],
                footer_text=f"Job Code: {vacancy.job_code}",
            )
            _set_state(
                wa_number,
                "seeker_no_cv",
                {"vacancy_id": vacancy.id, "job_code": vacancy.job_code},
                db,
            )
            return

    # ── Branch B: Has CV(s) on file ─────────────────────────────────────────
    default_resume = next((r for r in resumes if r.is_default), resumes[0])

    inferred_cat = _normalize_category(vacancy.job_category or _infer_job_category(vacancy))
    default_cv_cat = _normalize_category(default_resume.category_tag if default_resume.category_tag else candidate.category)

    if not inferred_cat or inferred_cat == "other":
        cv_matches = True
    else:
        cv_matches = (default_cv_cat == inferred_cat)

    cand_norm_dist = _normalize_district(candidate.district)
    vac_norm_dist = _normalize_district(vacancy.district_region)
    is_remote = (vacancy.job_mode or "").lower() in ("remote", "work_from_home", "wfh")
    if is_remote or not cand_norm_dist or not vac_norm_dist:
        district_matches = True
    else:
        district_matches = (cand_norm_dist == vac_norm_dist)

    # ── Case 1: Sweet Spot (CV matches & District matches) ──────────────────
    if cv_matches and district_matches:
        await wa_client.send_buttons(
            to=wa_number,
            body_text=seeker_apply_sweet_spot_body(candidate, vacancy, default_resume),
            buttons=[
                {"id": f"CONFIRM_APPLY_{vacancy.job_code}", "title": "📄 Submit Application"},
                {"id": f"MANAGE_CV_{vacancy.job_code}", "title": "📤 Change / Upload CV"},
            ],
            footer_text=f"Job Code: {vacancy.job_code}",
        )
        _set_state(
            wa_number,
            "seeker_viewing_job",
            {"vacancy_id": vacancy.id, "job_code": vacancy.job_code},
            db,
        )
        return

    # ── Priority 1: Default CV does not match ──────────────────────────────
    if not cv_matches:
        matching_resume = next(
            (r for r in resumes if _normalize_category(r.category_tag) == inferred_cat),
            None,
        )
        if matching_resume:
            # Case 2A: Found matching saved CV in profile
            await wa_client.send_buttons(
                to=wa_number,
                body_text=seeker_apply_smart_switch_body(candidate, vacancy, default_resume, matching_resume),
                buttons=[
                    {"id": f"USE_MATCHING_CV_{matching_resume.id}_{vacancy.job_code}", "title": "🎯 Use Matching CV"},
                    {"id": f"CONFIRM_APPLY_{vacancy.job_code}", "title": "⚡ Use Current CV"},
                    {"id": f"MANAGE_CV_{vacancy.job_code}", "title": "📤 Change / Upload CV"},
                ],
                footer_text=f"Job Code: {vacancy.job_code}",
            )
            _set_state(
                wa_number,
                "seeker_cv_mismatch",
                {"vacancy_id": vacancy.id, "job_code": vacancy.job_code},
                db,
            )
            return

        if not vacancy.cv_required:
            # Case 2B (CV Optional with Mismatched CV):
            await wa_client.send_buttons(
                to=wa_number,
                body_text=seeker_apply_cv_mismatch_optional_body(candidate, vacancy, default_resume),
                buttons=[
                    {"id": f"APPLY_NO_CV_{vacancy.job_code}", "title": "⚡ Apply without CV"},
                    {"id": f"CONFIRM_APPLY_{vacancy.job_code}", "title": "📄 Apply with this CV"},
                    {"id": f"MANAGE_CV_{vacancy.job_code}", "title": "📤 Change / Upload CV"},
                ],
                footer_text=f"Job Code: {vacancy.job_code}",
            )
            _set_state(
                wa_number,
                "seeker_cv_mismatch",
                {"vacancy_id": vacancy.id, "job_code": vacancy.job_code},
                db,
            )
            return
        else:
            # Case 2B (CV Mandatory with Mismatched CV):
            await wa_client.send_buttons(
                to=wa_number,
                body_text=seeker_apply_cv_recommendation_body(candidate, vacancy, default_resume),
                buttons=[
                    {"id": f"CONFIRM_APPLY_{vacancy.job_code}", "title": "📄 Apply with this CV"},
                    {"id": f"MANAGE_CV_{vacancy.job_code}", "title": "📤 Change / Upload CV"},
                ],
                footer_text=f"Job Code: {vacancy.job_code}",
            )
            _set_state(
                wa_number,
                "seeker_cv_mismatch",
                {"vacancy_id": vacancy.id, "job_code": vacancy.job_code},
                db,
            )
            return

    # ── Priority 2: District Mismatch (CV matched, District differs) ────────
    if not district_matches:
        # Case 3: Relocation / Location check
        await wa_client.send_buttons(
            to=wa_number,
            body_text=seeker_apply_relocation_body(candidate, vacancy, default_resume),
            buttons=[
                {"id": f"CONFIRM_APPLY_{vacancy.job_code}", "title": "✈️ Yes, Apply Now"},
                {"id": "SUGGEST_JOBS_NEAR_ME", "title": "🔍 Jobs Near Me"},
                {"id": f"MANAGE_CV_{vacancy.job_code}", "title": "📤 Change / Upload CV"},
            ],
            footer_text=f"Job Code: {vacancy.job_code}",
        )
        _set_state(
            wa_number,
            "seeker_relocation_check",
            {"vacancy_id": vacancy.id, "job_code": vacancy.job_code},
            db,
        )
        return


async def handle_gethelp_button(wa_number: str, db: Session) -> None:
    """Save a gethelp request when user taps 'Get Help'."""
    req = GetHelpRequest(wa_number=wa_number)
    db.add(req)
    db.commit()

    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
    recruiter = db.query(Recruiter).filter_by(wa_number=wa_number).first()

    if candidate and recruiter:
        status = "Job Seeker & Recruiter"
        name = f"{candidate.name} / {recruiter.company_name}"
        loc_parts = [candidate.exact_location, candidate.district]
        location = f"{', '.join(p for p in loc_parts if p)} / {recruiter.location}"
        context = f"{candidate.category} / {recruiter.business_type}"
    elif recruiter:
        status = "Recruiter"
        name = recruiter.company_name
        location = recruiter.location
        context = recruiter.business_type
    elif candidate:
        status = "Job Seeker"
        name = candidate.name
        loc_parts = [candidate.exact_location, candidate.district]
        location = ", ".join(p for p in loc_parts if p) or "Unknown"
        context = candidate.category
    else:
        status = "Unregistered User"
        name = "Unknown"
        location = "Unknown"
        context = "N/A"

    admin_alert = (
        "🚨 *New Help Request*\n"
        f"Number: {wa_number}\n"
        f"User Type: {status}\n"
        f"Name: {name}\n"
        f"Location: {location}\n"
        f"Context: {context}"
    )

    admin_targets = settings.submission_admins or ([settings.admin_wa_number] if settings.admin_wa_number else ["917025962179"])
    for admin_num in admin_targets:
        try:
            await wa_client.send_text(
                to=admin_num,
                body=admin_alert
            )
        except Exception as e:
            logger.warning("Failed to deliver help request alert to admin %s: %s", admin_num, e)

    await wa_client.send_buttons(
        to=wa_number,
        body_text=(
            "📩 *Help Request Received!*\n\n"
            "Thank you for reaching out! We have received your request, "
            "and a member of our support team will contact you shortly on this WhatsApp chat to assist you.\n\n"
            "While you wait, feel free to browse live vacancies or return to the main menu 👇"
        ),
        buttons=[
            {"id": "btn_explore_jobs", "title": "🌐 Explore all Jobs"},
            {"id": "btn_main_menu", "title": "🏠 Main Menu"},
        ],
        footer_text="JobInfo.pro • Made for Kerala",
    )
    _set_state(wa_number, "idle", {}, db)


async def handle_register_button(wa_number: str, job_code: str, db: Session) -> None:
    """Launch the registration WhatsApp Flow."""
    await wa_client.send_flow(
        to=wa_number,
        flow_id=settings.FLOW_ID_SEEKER_REGISTER,
        flow_token="seeker_register",
        flow_cta="Register Now",
        body_text=(
            "⏳ *Job Seeker Registration*\n\n"
            "_One Step Away from Your Dream Job!_\n\n"
            "Tap the button below to set up your profile and apply instantly.\n\n"
            "✅ *100% Free & Spam Free*\n"
            "✅ *Simple & Easy to Use*\n"
            "✅ *WhatsApp-autmated Application*\n"
            "✅ *Kerala's best placement network*\n\n"
            "Takes less than 1 minute! Let’s get started. ✨"
        ),
        # 👇 We wrapped the variable in 'data' and added a 'screen' name
        flow_action_payload={
            "screen": "SEEKER_REGISTRATION",
            "data": {
                "pending_job_code": job_code
            }
        },
    )
    _set_state(
        wa_number,
        "seeker_registering",
        {
            "pending_job_code": job_code,
            "flow_sent_at": datetime.now(timezone.utc).isoformat(),
        },
        db,
    )

async def handle_registration_flow_completion(
    wa_number: str, flow_data: dict, db: Session
) -> None:
    """
    Called when the seeker registration Flow completes.
    Saves candidate, optionally offers plan selection.
    flow_data keys: name, district, exact_location, category, sub_category, age, alt_phone, gender, cv_file, pending_job_code
    """
    # Save CV
    cv_path = None
    filename = None
    raw_media = flow_data.get("media_id")

    
    if raw_media:
        # Meta's File Upload returns a list of dictionaries
        actual_filename = None
        if isinstance(raw_media, list) and len(raw_media) > 0:
            actual_media_id = raw_media[0].get("id")
            actual_mime = raw_media[0].get("mime_type", "application/pdf")
            actual_filename = (
                raw_media[0].get("file_name")
                or raw_media[0].get("filename")
                or raw_media[0].get("name")
                or flow_data.get("file_name")
            )
        elif isinstance(raw_media, dict):
            actual_media_id = raw_media.get("id")
            actual_mime = raw_media.get("mime_type", "application/pdf")
            actual_filename = (
                raw_media.get("file_name")
                or raw_media.get("filename")
                or raw_media.get("name")
                or flow_data.get("file_name")
            )
        else:
            actual_media_id = raw_media
            actual_mime = flow_data.get("mime_type", "application/pdf")
            actual_filename = flow_data.get("file_name")
            
        if actual_media_id:
            cv_path, filename = await save_cv_from_whatsapp(
                wa_number=wa_number,
                media_id=actual_media_id,
                mime_type=actual_mime,
                original_filename=actual_filename,
            )
    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
    if not candidate:
        candidate = Candidate(
            wa_number=wa_number,
            name=flow_data.get("name", ""),
            district=flow_data.get("district"),
            exact_location=flow_data.get("exact_location"),
            category=flow_data.get("category"),
            sub_category=flow_data.get("sub_category"),
            age=int(flow_data["age"]) if flow_data.get("age") else None,
            alt_phone=flow_data.get("alt_phone"),
            gender=flow_data.get("gender"),
            cv_path=cv_path,
            registration_complete=False,
        )
        db.add(candidate)
    else:
        candidate.name = flow_data.get("name", candidate.name)
        if flow_data.get("district"):
            candidate.district = flow_data["district"]
        if flow_data.get("exact_location"):
            candidate.exact_location = flow_data["exact_location"]
        candidate.category = flow_data.get("category", candidate.category)
        candidate.sub_category = flow_data.get("sub_category", candidate.sub_category)
        if flow_data.get("age"):
            candidate.age = int(flow_data["age"])
        candidate.alt_phone = flow_data.get("alt_phone", candidate.alt_phone)
        if flow_data.get("gender"):
            candidate.gender = flow_data["gender"]
        if cv_path:
            candidate.cv_path = cv_path

    db.commit()
    db.refresh(candidate)

    # Sync CV to CandidateResume table for Smart Interceptor
    if candidate.cv_path:
        already_has = db.query(CandidateResume).filter_by(
            candidate_id=candidate.id,
        ).first()
        if not already_has:
            new_resume = CandidateResume(
                candidate_id=candidate.id,
                media_id=candidate.cv_path,
                file_name=filename or None,
                category_tag=candidate.category or "other",
                is_default=True,
            )
            db.add(new_resume)
            db.commit()

    if settings.subscription_enabled:
        # Offer plan selection
        await _send_plan_selection(wa_number, db)
    else:
        # Skip subscription during launch phase
        candidate.registration_complete = True
        db.commit()

        # If they were in the middle of applying, resume directly with the Job Apply Prompt
        state = _get_or_create_state(wa_number, db)
        pending_code = (state.context or {}).get("pending_job_code") or flow_data.get(
            "pending_job_code"
        )
        if pending_code:
            vacancy = db.query(JobVacancy).filter_by(job_code=pending_code).first()
            if vacancy:
                await _show_job_apply_prompt(wa_number, candidate, vacancy, db)
                return

        # General registration (no pending job) → Welcome menu card
        name = candidate.name.split()[0].title() if candidate.name else "there"

        # Location string
        if candidate.exact_location and candidate.district:
            loc_str = f"{candidate.exact_location.strip().title()}, {candidate.district.strip().title()}"
        elif candidate.district:
            loc_str = candidate.district.strip().title()
        elif candidate.exact_location:
            loc_str = candidate.exact_location.strip().title()
        else:
            loc_str = "Kerala"

        # Target Field
        cat_str = CATEGORY_DISPLAY_NAMES.get(
            (candidate.category or "").lower(),
            (candidate.category or "General").replace("_", " ").title(),
        )

        # CV Status: uploaded CV name or "No CV attached"
        resume = (
            db.query(CandidateResume)
            .filter_by(candidate_id=candidate.id)
            .order_by(CandidateResume.uploaded_at.desc())
            .first()
        )
        if resume and resume.file_name:
            cv_str = resume.file_name
        elif resume and resume.category_tag:
            tag_label = CATEGORY_DISPLAY_NAMES.get(
                resume.category_tag.lower(),
                resume.category_tag.replace("_", " ").title(),
            )
            cv_str = f"{tag_label} CV"
        elif candidate.cv_path:
            cv_str = "Uploaded CV"
        else:
            cv_str = "No CV attached"

        body_text = (
            f"🎉 *Congratulations, {name}!*\n\n"
            f"_Your Job Seeker profile is active —_\n"
            f"_you can now apply to any job across Kerala with just 1 tap!_\n\n"
            "📋 *Your Job Preferences:*\n"
            f"• 📍 {loc_str}\n"
            f"• 💼 {cat_str}\n"
            f"• 📄 {cv_str}\n\n"
            "We'll match you with opportunities tailored to your preferences.\n\n"
            "What would you like to do? 👇"
        )

        await wa_client.send_buttons(
            to=wa_number,
            body_text=body_text,
            buttons=[
                {"id": "ACTION_SUGGEST_JOBS", "title": "🎯 Suggest Jobs"},
                {"id": "ACTION_EXPLORE_JOBS", "title": "🌐 Explore all Jobs"},
            ],
            footer_text="JobInfo.pro • Made for Kerala",
        )


async def _send_plan_selection(wa_number: str, db: Session) -> None:
    """Send subscription plan options to the candidate."""
    plans = db.query(SubscriptionPlan).all()
    sections = [
        {
            "title": "Choose a Plan",
            "rows": [
                {
                    "id": f"plan_{p.name.value}",
                    "title": f"{p.display_name} – ₹{p.price_inr}",
                    "description": f"{p.duration_days} days | {p.max_applications or 'Unlimited'} apps",
                }
                for p in plans
            ],
        }
    ]
    await wa_client.send_list(
        to=wa_number,
        body_text=(
            "🎉 Registration info saved!\n\n"
            "Choose a subscription plan to start applying for jobs.\n\n"
            "*(Free Trial available – 15 days, 3 applications)*"
        ),
        button_label="View Plans",
        sections=sections,
        header_text="JobInfo – Select Plan",
    )
    _set_state(wa_number, "seeker_selecting_plan", {}, db)


async def handle_plan_selection(
    wa_number: str, plan_name: str, db: Session
) -> None:
    """Activate the chosen subscription plan."""
    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
    if not candidate:
        return

    plan = db.query(SubscriptionPlan).filter_by(name=plan_name).first()
    if not plan:
        return

    # Block free trial if already used
    if plan.name == SubscriptionPlanName.free_trial and candidate.free_trial_used:
        await wa_client.send_text(
            to=wa_number,
            body="⚠️ You've already used the Free Trial. Please choose a paid plan.",
        )
        await _send_plan_selection(wa_number, db)
        return

    candidate.subscription_plan_id = plan.id
    candidate.plan_expiry = datetime.now(timezone.utc) + timedelta(days=plan.duration_days)
    candidate.applications_used = 0
    candidate.registration_complete = True
    if plan.name == SubscriptionPlanName.free_trial:
        candidate.free_trial_used = True
    db.commit()

    await wa_client.send_text(
        to=wa_number,
        body=registration_confirmation_body(candidate.name, "candidate"),
    )

    # Resume pending application if any
    state = _get_or_create_state(wa_number, db)
    pending_code = (state.context or {}).get("pending_job_code")
    if pending_code:
        vacancy = db.query(JobVacancy).filter_by(job_code=pending_code).first()
        if vacancy:
            await _show_job_apply_prompt(wa_number, candidate, vacancy, db)


async def handle_apply_now_button(
    wa_number: str, vacancy_id: int, db: Session, bypass_cv_gate: bool = False
) -> None:
    """Save the job application and send confirmation."""
    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
    vacancy = db.query(JobVacancy).filter_by(id=vacancy_id).first()

    if not candidate or not vacancy:
        return

    from app.services.ad_lifecycle import ensure_ad_active
    if not ensure_ad_active(vacancy, db):
        await send_position_closed_message(wa_number)
        return

    # Double-check plan is still active
    if not _has_active_plan(candidate):
        await wa_client.send_text(to=wa_number, body=plan_renewal_body(candidate))
        return

    # ── Check if already applied ──────────────────────────────────────────
    existing = (
        db.query(CandidateApplication)
        .filter_by(candidate_id=candidate.id, vacancy_id=vacancy.id)
        .first()
    )
    if existing:
        status_value = str(getattr(existing.status, 'value', existing.status)).title()
        await wa_client.send_buttons(
            to=wa_number,
            body_text=(
                f"ℹ️ *Already Applied*\n\n"
                f"You have already submitted an application for the *{vacancy.job_title.strip()}* position.\n\n"
                f"📌 *Current Status:* _{status_value}_\n\n"
                "Would you like to explore other roles that match your profile?"
            ),
            buttons=[
                {"id": "ACTION_SUGGEST_JOBS", "title": "🎯 Suggest Jobs"},
            ],
        )
        return

    # ── CV-required gate (CRIT-1) ──────────────────────────────────────────
    # If the vacancy strictly requires a CV, never allow bypass if candidate has no CV
    if vacancy.cv_required:
        resume_count = db.query(CandidateResume).filter_by(candidate_id=candidate.id).count()
        has_cv = resume_count > 0 or bool(candidate.cv_path)
        if not has_cv:
            await _send_cv_required_message(wa_number, vacancy, vacancy.job_code)
            return

    application = CandidateApplication(
        candidate_id=candidate.id,
        vacancy_id=vacancy.id,
    )
    db.add(application)
    candidate.applications_used = (candidate.applications_used or 0) + 1
    db.commit()

    # Smart milestone notification (non-blocking; 24h window checked inside)
    app_count = db.query(CandidateApplication).filter_by(vacancy_id=vacancy.id).count()
    dispatch_milestone_notification(vacancy, app_count, db)

    # Confirmation with 'My Applications' button
    default_resume = (
        db.query(CandidateResume)
        .filter_by(candidate_id=candidate.id, is_default=True)
        .first()
    )
    if not default_resume:
        default_resume = (
            db.query(CandidateResume)
            .filter_by(candidate_id=candidate.id)
            .first()
        )

    if default_resume:
        cv_name = _resume_display_name(default_resume)
        scenario = "standard"
    elif candidate.cv_path:
        raw_p = candidate.cv_path.replace("\\", "/")
        cv_name = raw_p.split("/")[-1] if "/" in raw_p else "Your Saved CV"
        scenario = "standard"
    else:
        cv_name = None
        scenario = "no_cv"

    await wa_client.send_buttons(
        to=wa_number,
        body_text=application_confirmation_body(
            candidate, vacancy, scenario=scenario, cv_name=cv_name
        ),
        buttons=[
            {"id": "btn_view_applications", "title": "📑 My Applications"},
            {"id": "btn_suggest_more_jobs", "title": "🎯 Suggest Jobs"},
        ],
        footer_text="JobInfo • Kerala's Trusted Career Network",
    )
    _set_state(wa_number, "idle", {}, db)


async def handle_confirm_apply_button(
    wa_number: str, job_code: str, db: Session
) -> None:
    """
    User chose 'Apply Anyway' / 'Submit Application' from the CV prompt.
    Runs the CV-required check again before creating the application record
    (guards the case where the recruiter requires a CV and the seeker
    deliberately taps 'Apply Without CV').
    """
    vacancy = db.query(JobVacancy).filter_by(job_code=job_code).first()
    if not vacancy:
        await wa_client.send_text(to=wa_number, body="❌ This vacancy is no longer available.")
        return

    # ── CV-required gate ───────────────────────────────────────────────────
    if vacancy.cv_required:
        candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
        if candidate:
            resume_count = db.query(CandidateResume).filter_by(candidate_id=candidate.id).count()
            has_cv = resume_count > 0 or bool(candidate.cv_path)
            if not has_cv:
                await _send_cv_required_message(wa_number, vacancy, job_code)
                return

    await handle_apply_now_button(wa_number, vacancy.id, db)


async def handle_apply_no_cv(wa_number: str, job_code: str, db: Session) -> None:
    """
    User explicitly chose to apply without a CV.
    If the recruiter has made a CV mandatory, block and prompt upload.
    """
    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
    vacancy = db.query(JobVacancy).filter_by(job_code=job_code).first()

    if not candidate or not vacancy:
        await wa_client.send_text(to=wa_number, body="❌ This vacancy is no longer available.")
        return

    from app.services.ad_lifecycle import ensure_ad_active
    if not ensure_ad_active(vacancy, db):
        await send_position_closed_message(wa_number)
        return

    if not _has_active_plan(candidate):
        await wa_client.send_text(to=wa_number, body=plan_renewal_body(candidate))
        return

    # ── CV-required gate ───────────────────────────────────────────────────
    if vacancy.cv_required:
        await _send_cv_required_message(wa_number, vacancy, job_code)
        return

    existing = (
        db.query(CandidateApplication)
        .filter_by(candidate_id=candidate.id, vacancy_id=vacancy.id)
        .first()
    )
    if existing:
        status_value = str(getattr(existing.status, 'value', existing.status)).title()
        await wa_client.send_buttons(
            to=wa_number,
            body_text=(
                f"ℹ️ *Already Applied*\n\n"
                f"You have already submitted an application for the *{vacancy.job_title.strip()}* position.\n\n"
                f"📌 *Current Status:* _{status_value}_\n\n"
                "Would you like to explore other roles that match your profile?"
            ),
            buttons=[
                {"id": "ACTION_SUGGEST_JOBS", "title": "🎯 Suggest Jobs"},
            ],
        )
        return

    application = CandidateApplication(candidate_id=candidate.id, vacancy_id=vacancy.id)
    db.add(application)
    candidate.applications_used = (candidate.applications_used or 0) + 1
    db.commit()

    # Smart milestone notification (non-blocking; 24h window checked inside)
    app_count = db.query(CandidateApplication).filter_by(vacancy_id=vacancy.id).count()
    dispatch_milestone_notification(vacancy, app_count, db)

    await wa_client.send_buttons(
        to=wa_number,
        body_text=application_confirmation_body(candidate, vacancy, scenario="no_cv"),
        buttons=[
            {"id": "btn_view_applications", "title": "📑 My Applications"},
            {"id": "btn_suggest_more_jobs", "title": "🎯 Suggest Jobs"},
        ],
        footer_text="JobInfo • Kerala's Trusted Career Network",
    )
    _set_state(wa_number, "idle", {}, db)


# ── Smart CV Manager handlers ────────────────────────────────────────────────

async def handle_manage_cv(wa_number: str, job_code: str, db: Session) -> None:
    """Show an interactive list of saved CVs + optional Upload New option."""
    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
    if not candidate:
        return

    # Fast-forward: if zero CVs, skip the list and go straight to upload
    resume_count = db.query(CandidateResume).filter_by(candidate_id=candidate.id).count()
    if resume_count == 0 and not candidate.cv_path:
        await handle_upload_new_cv(wa_number, job_code, db)
        return

    resumes = (
        db.query(CandidateResume)
        .filter_by(candidate_id=candidate.id)
        .order_by(CandidateResume.uploaded_at.desc())
        .all()
    )

    sections: list[dict] = []

    # Section 1: Saved CVs
    if resumes:
        rows = []
        for r in resumes:
            tag_label = CATEGORY_DISPLAY_NAMES.get(
                (r.category_tag or "").lower(),
                (r.category_tag or "General").replace("_", " ").title(),
            )
            default_marker = " ★" if r.is_default else ""
            date_str = r.uploaded_at.strftime("%d %b %Y") if r.uploaded_at else "Recently"
            raw_fn = r.file_name or (r.media_id.split("/")[-1].split("\\")[-1] if r.media_id else "")
            
            display_title = (raw_fn[:24 - len(default_marker)] + default_marker) if raw_fn else f"{tag_label} CV{default_marker}"[:24]
            description_text = f"{tag_label} · {date_str}"[:72]
            rows.append({
                "id": f"SELECT_CV_{r.id}_{job_code}",
                "title": display_title[:24],
                "description": description_text,
            })
        sections.append({"title": "Your Saved CVs", "rows": rows})

    # Section 2: Upload New CV option
    if resume_count < MAX_CANDIDATE_RESUMES:
        sections.append({
            "title": "Need a Different CV?",
            "rows": [
                {
                    "id": f"UPLOAD_NEW_CV_{job_code}",
                    "title": "➕ Upload New CV",
                    "description": "Upload a tailored CV for this role",
                }
            ],
        })

    # Fallback if somehow no saved CVs exist despite earlier check
    if not sections:
        await handle_upload_new_cv(wa_number, job_code, db)
        return

    await wa_client.send_list(
        to=wa_number,
        header_text="📂 Smart CV Manager",
        body_text=(
            "🌟 *Let's put your best foot forward!*\n\n"
            "Select the CV that best matches this role, "
            "or upload a newly tailored one to maximize your chances of getting shortlisted.\n\n"
            "A targeted CV can make all the difference! 🚀"
        ),
        button_label="Choose CV",
        sections=sections,
        footer_text="Maximum allowed size: 1MB",
    )


async def handle_select_cv(
    wa_number: str, resume_id: int, job_code: str, db: Session
) -> None:
    """User selected an existing CV from the list — set as default and apply."""
    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
    if not candidate:
        return

    resume = db.query(CandidateResume).filter_by(id=resume_id, candidate_id=candidate.id).first()
    if not resume:
        await wa_client.send_text(to=wa_number, body="❌ CV not found. Please try again.")
        return

    # Set this CV as default, unset all others
    db.query(CandidateResume).filter(
        CandidateResume.candidate_id == candidate.id,
        CandidateResume.id != resume.id,
    ).update({"is_default": False})
    resume.is_default = True
    candidate.cv_path = resume.media_id  # keep legacy field in sync
    db.commit()

    # Proceed with application
    vacancy = db.query(JobVacancy).filter_by(job_code=job_code).first()
    if not vacancy:
        await wa_client.send_text(to=wa_number, body="❌ This vacancy is no longer available.")
        return

    # Check for duplicate
    existing = (
        db.query(CandidateApplication)
        .filter_by(candidate_id=candidate.id, vacancy_id=vacancy.id)
        .first()
    )
    if existing:
        status_value = str(getattr(existing.status, 'value', existing.status)).title()
        await wa_client.send_buttons(
            to=wa_number,
            body_text=(
                f"ℹ️ *Already Applied*\n\n"
                f"You have already submitted an application for the *{vacancy.job_title.strip()}* position.\n\n"
                f"📌 *Current Status:* _{status_value}_\n\n"
                "Would you like to explore other roles that match your profile?"
            ),
            buttons=[
                {"id": "ACTION_SUGGEST_JOBS", "title": "🎯 Suggest Jobs"},
            ],
        )
        return

    application = CandidateApplication(candidate_id=candidate.id, vacancy_id=vacancy.id)
    db.add(application)
    candidate.applications_used = (candidate.applications_used or 0) + 1
    db.commit()

    # Smart milestone notification (non-blocking; 24h window checked inside)
    app_count = db.query(CandidateApplication).filter_by(vacancy_id=vacancy.id).count()
    dispatch_milestone_notification(vacancy, app_count, db)

    cv_name = _resume_display_name(resume)

    await wa_client.send_buttons(
        to=wa_number,
        body_text=application_confirmation_body(
            candidate, vacancy, scenario="switched", cv_name=cv_name
        ),
        buttons=[
            {"id": "btn_view_applications", "title": "📑 My Applications"},
            {"id": "btn_suggest_more_jobs", "title": "🎯 Suggest Jobs"},
        ],
        footer_text="JobInfo • Kerala's Trusted Career Network",
    )
    _set_state(wa_number, "idle", {}, db)


async def handle_upload_new_cv(wa_number: str, job_code: str, db: Session) -> None:
    """Launch the CV Update Flow with the job_code attached for post-upload application."""
    await wa_client.send_flow(
        to=wa_number,
        flow_id=settings.FLOW_ID_CV_UPDATE,
        flow_token="cv_update",
        flow_cta="Upload CV",
        body_text=(
            "📄 *Upload Your Tailored CV*\n\n"
            "Tap the button below to securely upload your newly tailored CV. "
            "We accept files up to 1MB.\n\n"
            "A targeted CV = more interview calls! 🚀"
        ),
        footer_text = "Maximum 4 CVs allowed per user",
        flow_action_payload={
            "screen": "CV_UPDATE_SCREEN",
            "data": {
                "job_code": job_code,
            },
        },
    )
    _set_state(wa_number, "seeker_uploading_cv", {"job_code": job_code}, db)


async def handle_update_cv_button(
    wa_number: str, vacancy_id: int, db: Session
) -> None:
    """Prompt to upload a new CV via WhatsApp Flow."""
    await wa_client.send_flow(
        to=wa_number,
        flow_id=settings.FLOW_ID_CV_UPDATE,
        flow_token="cv_update",
        flow_cta="Upload New CV",
        body_text=(
            "📄 *Update Your CV*\n\n"
            "Upload your latest CV (PDF or CSV) in the form below."
        ),
        header_text="JobInfo – Update CV",
        # 👇 Wrapped in 'data' and added 'screen' name here too
        flow_action_payload={
            "screen": "CV_UPDATE_SCREEN",
            "data": {
                "return_vacancy_id": vacancy_id
            }
        },
    )
    _set_state(wa_number, "seeker_updating_cv", {"vacancy_id": vacancy_id}, db)


async def handle_cv_update_flow_completion(
    wa_number: str, flow_data: dict, db: Session
) -> None:
    """Process CV update from WhatsApp Flow (legacy and Smart CV Manager)."""
    new_cv_category = flow_data.get("new_cv_category")
    job_code = flow_data.get("job_code")
    raw_media = flow_data.get("media_id")

    # ── Missing-file safety check ─────────────────────────────────────────
    if not raw_media:
        if job_code:
            await wa_client.send_buttons(
                to=wa_number,
                header_text="📄 File Not Attached",
                body_text=(
                    "Oops! 📄 It looks like you forgot to attach your CV file.\n\n"
                    "Would you like to try uploading it again, or would you "
                    "prefer to proceed with your CV right now?"
                ),
                buttons=[
                    {"id": f"UPLOAD_NEW_CV_{job_code}", "title": "🔄 Retry Upload"},
                    {"id": f"CONFIRM_APPLY_{job_code}", "title": "🚀 Apply Without CV"},
                ],
            )
        else:
            await wa_client.send_text(
                to=wa_number,
                body=(
                    "Oops! 📄 It looks like you forgot to attach your PDF file.\n\n"
                    "Please tap the *Upload CV* menu again and make sure your file "
                    "is selected before hitting submit. We're here to help! 💪"
                ),
            )
        return

    # Extract ID and filename from Meta's list or dict structure
    actual_filename = None
    if isinstance(raw_media, list) and len(raw_media) > 0:
        actual_media_id = raw_media[0].get("id")
        actual_mime = raw_media[0].get("mime_type", "application/pdf")
        doc_size = raw_media[0].get("file_size")
        if doc_size and int(doc_size) > 1024 * 1024:
            btn_id = f"UPLOAD_NEW_CV_{job_code}" if job_code else "UPLOAD_NEW_CV_"
            await wa_client.send_buttons(
                to=wa_number,
                header_text="❌ File Too Large",
                body_text=(
                    f"Your CV file size ({int(doc_size) // 1024} KB) exceeds the *1MB* limit.\n\n"
                    "Please compress your CV (e.g. using a free tool like smallpdf.com or ilovepdf.com) and try uploading again."
                ),
                buttons=[{"id": btn_id, "title": "🔄 Try Again"}],
            )
            return

        actual_filename = (
            raw_media[0].get("file_name")
            or raw_media[0].get("filename")
            or raw_media[0].get("name")
            or flow_data.get("file_name")
        )
    elif isinstance(raw_media, dict):
        actual_media_id = raw_media.get("id")
        actual_mime = raw_media.get("mime_type", "application/pdf")
        doc_size = raw_media.get("file_size")
        if doc_size and int(doc_size) > 1024 * 1024:
            btn_id = f"UPLOAD_NEW_CV_{job_code}" if job_code else "UPLOAD_NEW_CV_"
            await wa_client.send_buttons(
                to=wa_number,
                header_text="❌ File Too Large",
                body_text=(
                    f"Your CV file size ({int(doc_size) // 1024} KB) exceeds the *1MB* limit.\n\n"
                    "Please compress your CV and try uploading again."
                ),
                buttons=[{"id": btn_id, "title": "🔄 Try Again"}],
            )
            return

        actual_filename = (
            raw_media.get("file_name")
            or raw_media.get("filename")
            or raw_media.get("name")
            or flow_data.get("file_name")
        )
    else:
        actual_media_id = raw_media
        actual_mime = flow_data.get("mime_type", "application/pdf")
        actual_filename = flow_data.get("file_name")

    cv_path, filename = await save_cv_from_whatsapp(
        wa_number, actual_media_id, actual_mime, original_filename=actual_filename
    )
    if not cv_path:
        btn_id = f"UPLOAD_NEW_CV_{job_code}" if job_code else "UPLOAD_NEW_CV_"
        await wa_client.send_buttons(
            to=wa_number,
            header_text="❌ CV Upload Failed",
            body_text=(
                "We could not accept this file.\n\n"
                "• Maximum allowed file size: *1MB*\n"
                "• Allowed formats: *PDF, Word (.doc, .docx)*\n\n"
                "Please compress your CV and try uploading again."
            ),
            buttons=[
                {"id": btn_id, "title": "📤 Upload CV"}
            ]
        )
        return

    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
    if not candidate:
        return

    # ── Smart CV Manager flow (new_cv_category present) ───────────────────
    if new_cv_category and job_code:
        # Unset all existing defaults
        db.query(CandidateResume).filter(
            CandidateResume.candidate_id == candidate.id,
        ).update({"is_default": False})

        # Enforce max limit — remove oldest if at limit
        existing_count = db.query(CandidateResume).filter_by(candidate_id=candidate.id).count()
        if existing_count >= MAX_CANDIDATE_RESUMES:
            oldest = (
                db.query(CandidateResume)
                .filter_by(candidate_id=candidate.id)
                .order_by(CandidateResume.uploaded_at.asc())
                .first()
            )
            if oldest:
                db.delete(oldest)

        # Save new resume record
        new_resume = CandidateResume(
            candidate_id=candidate.id,
            media_id=cv_path,
            file_name=filename,
            category_tag=new_cv_category,
            is_default=True,
        )
        db.add(new_resume)
        candidate.cv_path = cv_path  # keep legacy field in sync
        candidate.cv_updates_used = (candidate.cv_updates_used or 0) + 1
        db.commit()

        # Re-evaluate with Decision Tree (Case 1 if match, Case 2B if mismatch, Case 3 if relocation)
        vacancy = db.query(JobVacancy).filter_by(job_code=job_code).first()
        if not vacancy:
            await wa_client.send_text(to=wa_number, body="❌ This vacancy is no longer available.")
            _set_state(wa_number, "idle", {}, db)
            return

        # Check for duplicate
        existing_app = (
            db.query(CandidateApplication)
            .filter_by(candidate_id=candidate.id, vacancy_id=vacancy.id)
            .first()
        )
        if existing_app:
            await wa_client.send_text(
                to=wa_number,
                body=f"ℹ️ You have already applied for *{vacancy.job_title.strip()}*. Status: _{getattr(existing_app.status, 'value', existing_app.status)}_",
            )
            _set_state(wa_number, "idle", {}, db)
            return

        await _show_job_apply_prompt(wa_number, candidate, vacancy, db)
        return

    # ── Legacy flow (no category/job_code) ────────────────────────────────
    candidate.cv_path = cv_path
    candidate.cv_updates_used = (candidate.cv_updates_used or 0) + 1
    existing_default = db.query(CandidateResume).filter_by(candidate_id=candidate.id, is_default=True).first()
    if existing_default:
        existing_default.media_id = cv_path
        existing_default.file_name = filename
    else:
        new_resume = CandidateResume(
            candidate_id=candidate.id,
            media_id=cv_path,
            file_name=filename,
            category_tag=candidate.category or "other",
            is_default=True,
        )
        db.add(new_resume)
    db.commit()
    await wa_client.send_text(
        to=wa_number,
        body=cv_update_confirmation_body(candidate),
    )
    _set_state(wa_number, "idle", {}, db)


async def handle_view_applications_button(wa_number: str, db: Session) -> None:
    """Show candidate's application summary (delegates to shared helper)."""
    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
    if not candidate:
        return
    await _send_application_summary_cta(wa_number, candidate, db)


async def _send_application_summary_cta(
    wa_number: str, candidate: Candidate, db: Session
) -> None:
    """
    Direct 1-tap gateway to candidate's career dashboard on dashboard.html.
    Called from both handle_view_applications_button and handle_my_applications_menu.
    """
    name = candidate.name.split()[0].title() if candidate.name else "there"
    dashboard_url = _generate_magic_dashboard_url(wa_number, db)

    # Check if candidate has submitted any applications
    latest = (
        db.query(CandidateApplication)
        .filter_by(candidate_id=candidate.id)
        .order_by(CandidateApplication.applied_at.desc())
        .first()
    )

    if not latest:
        await wa_client.send_cta_url(
            to=wa_number,
            header_text="📂 Your Applications",
            body_text=(
                f"Hi {name}, you haven't applied for any jobs yet!\n\n"
                "Explore live openings on our web portal or tap *Suggest Jobs* "
                "to discover roles tailored to your profile 👇"
            ),
            button_text="Browse All Jobs ↗",
            url="https://jobinfo.pro/jobs.html",
            footer_text="Updated daily with new opportunities",
        )
        return

    # Direct 1-tap Dashboard Launcher
    body_text = (
        f"Hi {name}, your secure 1-tap login link is ready.\n\n"
        "Track all your submitted applications, view recruiter updates, and manage your CVs on your web dashboard 👇"
    )

    await wa_client.send_cta_url(
        to=wa_number,
        header_text="📊 Your Career Dashboard",
        body_text=body_text,
        button_text="Open Dashboard ↗",
        url=dashboard_url,
        footer_text="⏳ Button expires in 24h",
    )


def _infer_job_category(vacancy: JobVacancy) -> str:
    """Infer a category for a job vacancy by matching its title/description against keywords."""
    text = f"{vacancy.job_title or ''} {vacancy.job_description or ''}".lower()
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if cat == "other":
            continue
        for kw in keywords:
            if kw in text:
                return cat
    return "other"


# ═══════════════════════════════════════════════════════════════════════════════
# Seeker Main Menu & New Button Handlers
# ═══════════════════════════════════════════════════════════════════════════════


def _generate_magic_dashboard_url(wa_number: str, db: Session) -> str:
    import secrets
    from datetime import datetime, timedelta, timezone
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=90)
    magic = MagicLink(
        token=token,
        wa_number=wa_number,
        role="seeker",
        expires_at=expires,
    )
    db.add(magic)
    db.commit()
    return f"https://jobinfo.pro/index.html?magic_token={token}"

# Keywords used to match a seeker's category to vacancy title/description
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "retail": ["retail", "showroom", "cashier", "store", "billing", "floor manager", "packing"],
    "sales_business": ["sales", "business executive", "field sales", "medical rep", "fmcg sales", "marketing executive"],
    "hospitality": ["hotel", "restaurant", "chef", "cook", "waiter", "kitchen", "housekeeping", "server", "food", "tea", "juice"],
    "healthcare": ["nurse", "caretaker", "clinic", "pharmacy", "lab", "hospital", "medical", "physioth", "ward boy"],
    "education": ["teacher", "academic", "lecturer", "professor", "tuition", "coaching", "school", "daycare"],
    "office_data_entry": ["data entry", "office admin", "clerk", "peon", "office helper"],
    "front_office": ["front office", "receptionist", "guest relation"],
    "finance_accounts": ["accountant", "tally", "audit", "finance assistant", "billing staff"],
    "hr_management": ["hr", "branch manager", "team leader", "operations manager", "admin executive"],
    "telecalling": ["telecaller", "customer care", "telesales", "bpo", "call center"],
    "it_digital_marketing": ["software", "developer", "graphic", "designer", "digital market", "it ", "video editor", "content writer", "programmer", "it hardware"],
    "logistics_store": ["driver", "delivery", "logistics", "forklift", "taxi", "store keeper", "warehouse", "heavy vehicle", "auto"],
    "beauty_wellness": ["beautician", "salon", "hair stylist", "spa", "makeup"],
    "maintenance_technician": ["electrician", "mechanic", "plumber", "welder", "fitter", "technician", "ac ", "cctv", "lift"],
    "construction_labor": ["construction", "site supervisor", "labor", "painter", "carpenter", "factory worker"],
    "gulf_abroad": ["gulf", "gcc", "abroad", "overseas", "dubai", "qatar", "saudi", "oman", "bahrain", "kuwait"],
    "other": [],
}


async def send_seeker_greeting_menu(wa_number: str) -> None:
    """Send the main 3-button seeker welcome menu."""
    await wa_client.send_buttons(
        to=wa_number,
        body_text=(
            "✨ *JobInfo — Your Career Partner!*\n\n"
            "We're thrilled to help you take the next step in your career. "
            "Whether you're looking for your dream job or just exploring options — we've got you covered.\n\n"
            "Choose how you'd like to start 👇"
        ),
        buttons=[
            {"id": "ACTION_SUGGEST_JOBS", "title": "Suggest Jobs"},
            {"id": "ACTION_EXPLORE_JOBS", "title": "Explore all Jobs"},
            {"id": "ACTION_MY_APPLICATIONS", "title": "My Applications"},
        ],
        footer_text="JobInfo.pro • Made for Kerala",
    )


async def handle_explore_jobs(wa_number: str) -> None:
    """Send a CTA URL inviting the seeker to the WhatsApp job channel and web portal."""
    await wa_client.send_cta_url(
        to=wa_number,
        body_text=(
            "📢 *JobInfo Jobs Network*\n\n"
            "We publish verified vacancies and walk-in interviews "
            "across all 14 districts in Kerala daily:\n\n"
            "🌐 *Browse Live on Website:*\n"
            "https://jobinfo.pro/jobs.html\n\n"
            "📲 *Instant WhatsApp Alerts:*\n"
            "Join our official channel below to receive new openings directly in your WhatsApp feed 👇"
        ),
        button_text="Join Channel ↗",
        url=WHATSAPP_CHANNEL_URL,
        footer_text="Free • Daily updates • No spam",
    )


async def handle_my_applications_menu(wa_number: str, db: Session) -> None:
    """
    Show My Applications: if registered → rich summary, else → registration flow.
    """
    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()

    if candidate and candidate.registration_complete:
        await _send_application_summary_cta(wa_number, candidate, db)
    else:
        await wa_client.send_flow(
            to=wa_number,
            flow_id=settings.FLOW_ID_SEEKER_REGISTER,
            flow_token="seeker_register",
            flow_cta="Set Up Profile",
            header_text="JobInfo — Profile Required",
            body_text=(
                "📋 *One quick step before you can track applications!*\n\n"
                "To view your application history and get personalized job updates, "
                "we need to set up your profile first.\n\n"
                "It takes less than 2 minutes — tap below to get started! ✨"
            ),
            flow_action_payload={
                "screen": "SEEKER_REGISTRATION",
                "data": {"pending_job_code": ""},
            },
        )


def _get_preferred_role_title(candidate: Candidate) -> str:
    raw_sub = (candidate.sub_category or "").strip()
    if raw_sub:
        try:
            from app.routers.flows import CATEGORY_SUBCATEGORIES
            for sub_list in CATEGORY_SUBCATEGORIES.values():
                for item in sub_list:
                    if item["id"] == raw_sub:
                        return item["title"]
        except Exception:
            pass
        return raw_sub.replace("_", " ").title()

    raw_cat = (candidate.category or "").strip().lower()
    if raw_cat in CATEGORY_DISPLAY_NAMES:
        return CATEGORY_DISPLAY_NAMES[raw_cat]
    if raw_cat:
        return raw_cat.replace("_", " ").title()
    return "your field"


async def handle_suggest_jobs(wa_number: str, db: Session, exclude_vacancy_id: int | None = None) -> None:
    """
    Suggest matching jobs: delegates to the smart tiered weightage algorithm.
    If unregistered, prompts registration flow first.
    """
    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()

    if not candidate or not candidate.registration_complete:
        await wa_client.send_flow(
            to=wa_number,
            flow_id=settings.FLOW_ID_SEEKER_REGISTER,
            flow_token="seeker_register",
            flow_cta="Set Up Profile",
            header_text="JobInfo — Let Us Know Your Preferences",
            body_text=(
                "🎯 *We'd love to suggest the perfect jobs for you!*\n\n"
                "To match you with the right opportunities, we need to know "
                "your preferred job area, location, and a few quick details.\n\n"
                "Set up your profile in under 2 minutes and start receiving "
                "tailored job suggestions! 🚀"
            ),
            flow_action_payload={
                "screen": "SEEKER_REGISTRATION",
                "data": {"pending_job_code": ""},
            },
        )
        return

    await handle_suggest_weighted_jobs(wa_number, db, exclude_vacancy_id=exclude_vacancy_id)


# ─── Plan A: Bot Friction Elimination Helpers ─────────────────────────────────

async def handle_resume_recent(wa_number: str, vacancy: JobVacancy) -> None:
    """Sub-case A: Recent flow dropout (<=14d) with an active vacancy."""
    salary = _label(SALARY_LABELS, vacancy.salary_range)
    company_name = vacancy.recruiter.company_name if vacancy.recruiter else "—"
    location_str = f"{vacancy.exact_location or '—'}, {vacancy.district_region or '—'}"
    await wa_client.send_buttons(
        to=wa_number,
        body_text=(
            "👋 *Welcome back to JobInfo!*\n\n"
            "When you last visited, you were looking at this opening:\n\n"
            f"💼 *Role:* {vacancy.job_title.strip()}\n"
            f"🏢 *Company:* {company_name}\n"
            f"📍 *Location:* {location_str}\n"
            f"💰 *Salary:* {salary}\n"
            f"🔖 *Job Code:* {vacancy.job_code}\n\n"
            "This role is still actively receiving applications! "
            "How would you like to proceed? 👇"
        ),
        buttons=[
            {"id": f"btn_resume_apply_{vacancy.job_code}", "title": "🚀 Apply Now"},
            {"id": "menu_recruiter", "title": "📢 I am Hiring"},
            {"id": "btn_main_menu", "title": "🏠 Main Menu"},
        ],
        footer_text="JobInfo.pro • Made for Kerala",
    )



async def handle_registered_quick_apply(wa_number: str, candidate: Candidate, vacancy: JobVacancy) -> None:
    """
    Registered Seeker Guard (Fix 1):
    Registered candidate who messaged bot while having a recent active pending job code.
    Shows 1-Tap Quick Apply Card with [⚡ Apply Now] (17 chars).
    """
    salary = _label(SALARY_LABELS, vacancy.salary_range)
    company_name = vacancy.recruiter.company_name if vacancy.recruiter else "—"
    location_str = f"{vacancy.exact_location or '—'}, {vacancy.district_region or '—'}"
    await wa_client.send_buttons(
        to=wa_number,
        body_text=(
            f"👋 *Welcome back, {candidate.name}!* \n\n"
            "You were applying for:\n"
            f"• 📋 *Role:* {vacancy.job_title.strip()}\n"
            f"• 🏢 *Company:* {company_name}\n"
            f"• 📍 *Location:* {location_str}\n"
            f"• 🔖 *Job Code:* {vacancy.job_code}\n\n"
            "If you are still interested in this posion, submit your application now 👇"
        ),
        buttons=[
            {"id": f"btn_apply_instantly_{vacancy.job_code}", "title": "⚡ Apply Now"},
            {"id": "ACTION_SUGGEST_JOBS", "title": "🎯 Suggest More"},
            {"id": "btn_main_menu", "title": "🏠 Main Menu"},
        ],
    )


async def handle_not_interested_registered(wa_number: str, db: Session) -> None:
    """Registered seeker clicked [❌ Not Interested]. Clears pending job and gives continuation."""
    _set_state(wa_number, "idle", {}, db)
    await wa_client.send_buttons(
        to=wa_number,
        body_text=(
            "Understood, no problem at all! 👍\n\n"
            "We've cleared that position from your profile.\n\n"
            "How would you like to proceed?"
        ),
        buttons=[
            {"id": "ACTION_SUGGEST_JOBS", "title": "🎯 Suggest More"},
            {"id": "btn_explore_website", "title": "🌐 More on Website"},
            {"id": "ACTION_MY_APPLICATIONS", "title": "📑 My Applications"},
        ],
    )


async def handle_not_interested_unregistered(wa_number: str, db: Session) -> None:
    """Unregistered seeker clicked [❌ Not Interested]. Clears state and gives helpful menu."""
    _set_state(wa_number, "idle", {}, db)
    await wa_client.send_buttons(
        to=wa_number,
        body_text=(
            "Understood, no problem at all! 👍\n\n"
            "We've cleared that position. We have new vacancies posted every week across Kerala!\n\n"
            "How can we help you today?"
        ),
        buttons=[
            {"id": "btn_explore_website", "title": "🌐 Explore Jobs"},
            {"id": "btn_create_profile", "title": "📝 Create Profile"},
            {"id": "help_support", "title": "💬 Get Help"},
        ],
    )


def get_seeker_recommended_vacancies(candidate: Candidate, db: Session) -> list[JobVacancy]:
    """Adaptive 1+2 blended matching: Tier 1 local match + Tier 2 broader Kerala match."""
    from sqlalchemy import func, or_

    tier1_jobs = db.query(JobVacancy).filter(
        JobVacancy.is_active == True,
        JobVacancy.status == "approved",
        JobVacancy.job_category == candidate.category,
        func.lower(JobVacancy.district_region) == candidate.district.lower() if candidate.district else True
    ).order_by(JobVacancy.created_at.desc()).limit(2).all()

    recommended = list(tier1_jobs)

    if len(recommended) < 2:
        needed = 2 - len(recommended)
        exclude_ids = [j.id for j in recommended]
        tier2_query = db.query(JobVacancy).filter(
            JobVacancy.is_active == True,
            JobVacancy.status == "approved",
            JobVacancy.id.notin_(exclude_ids) if exclude_ids else True,
        )
        filters = []
        if candidate.category:
            filters.append(JobVacancy.job_category == candidate.category)
        if candidate.district:
            filters.append(func.lower(JobVacancy.district_region) == candidate.district.lower())

        if filters:
            tier2_query = tier2_query.filter(or_(*filters))

        tier2_jobs = tier2_query.order_by(JobVacancy.created_at.desc()).limit(needed).all()
        recommended.extend(tier2_jobs)

    return recommended


async def send_seeker_nudge_with_jobs(wa_number: str, candidate: Candidate, db: Session) -> None:
    """Nudge registered seekers with top recommended jobs using tiered weightage format."""
    await handle_suggest_weighted_jobs(wa_number, db)


async def send_seeker_empty_nudge(wa_number: str, candidate: Candidate) -> None:
    """When 0 matching jobs exist anywhere in Kerala."""
    name = candidate.name.split()[0] if candidate.name else "there"
    await wa_client.send_buttons(
        to=wa_number,
        body_text=(
            f"👋 Welcome back, {name}!\n\n"
            "We don't have open positions in your prefferd field today, but fresh vacancies are added daily! 🎯\n\n"
            "You can search all live Kerala openings on our web portal or get instant alerts on WhatsApp:"
        ),
        buttons=[
            {"id": "btn_explore_website", "title": "🌐 Browse Portal"},
            {"id": "btn_whatsapp_channel", "title": "📢 Join Channel"},
            {"id": "btn_my_profile", "title": "👤 My Profile"},
        ],
    )


async def send_applied_seeker_dashboard(wa_number: str, candidate: Candidate, db: Session) -> None:
    """Personalized Career Hub for registered job seekers."""
    name = candidate.name.split()[0].title() if candidate.name else "there"
    preferred_field = CATEGORY_DISPLAY_NAMES.get(candidate.category, candidate.category or "General")
    loc_str = (candidate.district or "Kerala").strip().title()

    cleaned_wa = (candidate.wa_number or "").replace("+", "").replace(" ", "").strip()
    if cleaned_wa.startswith("91") and len(cleaned_wa) == 12:
        wa_disp = cleaned_wa[2:]
    elif cleaned_wa:
        wa_disp = cleaned_wa
    else:
        wa_disp = "—"
    contact_no = (candidate.alt_phone or "").strip() or wa_disp

    apps = (
        db.query(CandidateApplication)
        .filter_by(candidate_id=candidate.id)
        .order_by(CandidateApplication.applied_at.desc())
        .all()
    )

    if apps:
        latest = apps[0]
        vac = db.query(JobVacancy).filter_by(id=latest.vacancy_id).first()
        role_name = vac.job_title.strip() if vac and vac.job_title else "Position"
        jc = f" ({vac.job_code})" if vac and vac.job_code else ""
        company_name = vac.recruiter.company_name.strip() if vac and vac.recruiter and vac.recruiter.company_name else "Verified Employer"

        loc_parts = []
        if vac:
            if vac.exact_location and vac.exact_location.strip():
                loc_parts.append(vac.exact_location.strip().title())
            if vac.district_region and vac.district_region.strip():
                loc_parts.append(vac.district_region.strip().title())
        job_loc = ", ".join(loc_parts) if loc_parts else "Kerala"

        raw_status = str(getattr(latest.status, 'value', latest.status)).lower()
        status_map = {
            "applied": "🟡 Under Review",
            "viewed": "👀 Viewed by Employer",
            "shortlisted": "⭐ Shortlisted",
            "interview_scheduled": "📅 Interview Scheduled",
            "rejected": "⏸️ Position Closed",
            "hired": "🎉 Hired",
        }
        status_display = status_map.get(raw_status, "🟡 Under Review")

        status_block = (
            f"📊 *Latest Application Status:*\n"
            f"• 💼 Role: *{role_name}*{jc}\n"
            f"• 🏢 Company: {company_name}\n"
            f"• 📍 Location: {job_loc}\n"
            f"• ⏳ Status: {status_display}"
        )
        buttons = [
            {"id": "btn_fresh_openings", "title": "🎯 Fresh Openings"},
            {"id": "ACTION_MY_APPLICATIONS", "title": "📋 My Applications"},
            {"id": "btn_my_dashboard", "title": "🖥️ My Dashboard"},
        ]
    else:
        status_block = (
            "🎯 *Start Your Job Search:*\n"
            "You haven’t applied to any vacancies yet! Tap *Fresh Openings* below to discover jobs tailored for you and apply in seconds."
        )
        buttons = [
            {"id": "btn_fresh_openings", "title": "🎯 Fresh Openings"},
            {"id": "btn_my_dashboard", "title": "🖥️ My Dashboard"},
            {"id": "help_support", "title": "ℹ️ Help & More"},
        ]

    body_text = (
        f"👋 *Welcome Back, {name}!*\n\n"
        f"_Your Personal Career Hub & Job Assistant 🌴_\n\n"
        f"📋 *Your Profile:*\n"
        f"• 🎯 Field: {preferred_field}\n"
        f"• 📍 Location: {loc_str}\n"
        f"• 🛡️ Status: Candidate ✅\n"
        f"• 📱 Contact: {contact_no}\n\n"
        f"{status_block}\n\n"
        f"✨ _Fresh vacancies matching your profile are updated daily — choose an option below to get started 👇_"
    )

    await wa_client.send_buttons(
        to=wa_number,
        body_text=body_text,
        buttons=buttons,
        footer_text="JobInfo.pro • Made for Kerala",
    )


async def handle_explore_website_cta(wa_number: str) -> None:
    """Opens jobinfo.pro/jobs.html in WhatsApp's in-app browser via send_cta_url."""
    await wa_client.send_cta_url(
        to=wa_number,
        header_text="🌐 Live Jobs Across Kerala",
        body_text=(
            "*Explore 100+ Live Jobs Across Kerala*\n\n"
            "Visit our fast, mobile-friendly job portal to:\n"
            "• Filter vacancies by District, Role & Salary\n"
            "• Discover direct walk-in interviews\n"
            "• Apply to multiple positions in seconds"
        ),
        button_text="Open Job Portal ↗",
        url="https://jobinfo.pro/jobs.html",
    )


async def handle_fresh_openings(wa_number: str, candidate: Candidate, db: Session) -> None:
    """Finds matching jobs using the smart weighted recommendation algorithm for rich presentation."""
    await handle_suggest_weighted_jobs(wa_number, db)


async def handle_suggest_jobs_no_cv(wa_number: str, db: Session) -> None:
    """Finds jobs that do not require a CV, prioritizing candidate preferences."""
    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
    applied_ids = []
    if candidate:
        applied_ids = [
            a.vacancy_id for a in db.query(CandidateApplication.vacancy_id)
            .filter_by(candidate_id=candidate.id).all()
        ]

    from sqlalchemy import func, case
    query = db.query(JobVacancy).filter(
        JobVacancy.is_active == True,
        JobVacancy.status == "approved",
        JobVacancy.cv_required == False,
    )
    if applied_ids:
        query = query.filter(JobVacancy.id.notin_(applied_ids))

    if candidate and (candidate.category or candidate.district):
        cat = (candidate.category or "").strip().lower()
        dist = (candidate.district or "").strip().lower()
        match_score = case(
            (func.lower(JobVacancy.job_category) == cat, 2),
            (func.lower(JobVacancy.district_region) == dist, 1),
            else_=0,
        )
        jobs = query.order_by(match_score.desc(), JobVacancy.created_at.desc()).limit(2).all()
    else:
        jobs = query.order_by(JobVacancy.created_at.desc()).limit(2).all()

    if not jobs:
        await wa_client.send_buttons(
            to=wa_number,
            body_text=(
                "ℹ️ *No CV-Optional Jobs Right Now*\n\n"
                "All current vacancies in this category require a CV.\n\n"
                "Uploading a CV takes less than a minute and unlocks 100% of open positions!"
            ),
            buttons=[
                {"id": "btn_explore_website", "title": "🌐 View Other Jobs"},
            ],
        )
        return

    name = (candidate.name.split()[0].title()) if candidate and candidate.name else "there"
    job_blocks = []
    buttons = []
    for i, j in enumerate(jobs):
        num_prefix = f"{i+1}️⃣ " if len(jobs) > 1 else "1️⃣ "
        role_title = j.job_title.strip()
        loc = (j.district_region or "Kerala").strip().title()
        if j.exact_location:
            exact = j.exact_location.strip().title()
            candidate_loc = f"{exact}, {loc}"
            if len(candidate_loc) <= 25:
                loc = candidate_loc

        sal = _label(SALARY_LABELS, j.salary_range, fallback="")

        lines = [
            f"{num_prefix}*{role_title}*",
            f"• 🔖 Job Code: {j.job_code}",
            f"• 📍 {loc}",
        ]
        if sal:
            lines.append(f"• 💰 {sal}")
        job_blocks.append("\n".join(lines))
        buttons.append({"id": f"view_job_{j.job_code}", "title": f"📋 View {j.job_code}"[:20]})

    buttons.append({"id": "btn_explore_website", "title": "🌐 View all Jobs"})

    body_text = (
        "🎯 *Jobs Without CV Required:*\n\n"
        + "\n\n".join(job_blocks)
        + f"\n\n_{name}, you don't need a CV for this position! Recruiters review your profile directly and will contact you for next steps._\n\n"
        "Tap a button below to view details\nand apply directly 👇"
    )

    await wa_client.send_buttons(
        to=wa_number,
        body_text=body_text,
        buttons=buttons,
        footer_text="Explore 50+ more on website",
    )


async def handle_suggest_weighted_jobs(
    wa_number: str,
    db: Session,
    exclude_vacancy_id: int | None = None,
) -> None:
    """
    Tiered weightage job suggestion algorithm:
    Tier 1 (5 pts): Same category as last applied job + Candidate's home district
    Tier 2 (4 pts): Same category as last applied job + Other districts
    Tier 3 (3 pts): Candidate profile preferred category + Home district
    Tier 4 (2 pts): Candidate profile preferred category + Other districts
    Tier 5 (1 pt):  Any category + Home district
    Tier 6 (0 pts): Newest available active openings anywhere in Kerala
    Always returns top 2 best available vacancies (excludes already applied and current viewing job).
    """
    from sqlalchemy import func, case, and_
    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
    if not candidate:
        return

    # Exclude all vacancies candidate has already applied to + currently viewed vacancy (if any)
    applied_ids = [
        a.vacancy_id
        for a in db.query(CandidateApplication.vacancy_id)
        .filter_by(candidate_id=candidate.id)
        .all()
    ]
    excluded_ids = set(applied_ids)
    if exclude_vacancy_id:
        excluded_ids.add(exclude_vacancy_id)

    # Find the most recently applied vacancy for category context
    applied_cat = ""
    latest_app = (
        db.query(CandidateApplication)
        .filter_by(candidate_id=candidate.id)
        .order_by(CandidateApplication.applied_at.desc())
        .first()
    )
    if latest_app:
        recent_vac = db.query(JobVacancy).filter_by(id=latest_app.vacancy_id).first()
        if recent_vac and recent_vac.job_category:
            applied_cat = _normalize_category(recent_vac.job_category)

    cand_cat = _normalize_category(candidate.category) if candidate.category else ""
    cand_dist = _normalize_district(candidate.district) if candidate.district else ""

    query = db.query(JobVacancy).filter(
        JobVacancy.is_active == True,
        JobVacancy.status == "approved",
    )
    if excluded_ids:
        query = query.filter(JobVacancy.id.notin_(excluded_ids))

    conditions = []
    # Tier 1: Same category as applied job + Home district
    if applied_cat and cand_dist:
        conditions.append(
            (
                and_(
                    func.lower(JobVacancy.job_category) == applied_cat,
                    func.lower(JobVacancy.district_region).like(f"%{cand_dist}%"),
                ),
                5,
            )
        )
    # Tier 2: Same category as applied job (any district)
    if applied_cat:
        conditions.append((func.lower(JobVacancy.job_category) == applied_cat, 4))
    # Tier 3: Profile preferred category + Home district
    if cand_cat and cand_dist:
        conditions.append(
            (
                and_(
                    func.lower(JobVacancy.job_category) == cand_cat,
                    func.lower(JobVacancy.district_region).like(f"%{cand_dist}%"),
                ),
                3,
            )
        )
    # Tier 4: Profile preferred category (any district)
    if cand_cat:
        conditions.append((func.lower(JobVacancy.job_category) == cand_cat, 2))
    # Tier 5: Any other category in Home district
    if cand_dist:
        conditions.append((func.lower(JobVacancy.district_region).like(f"%{cand_dist}%"), 1))

    if conditions:
        match_score = case(*conditions, else_=0)
        jobs = query.order_by(match_score.desc(), JobVacancy.created_at.desc()).limit(2).all()
    else:
        jobs = query.order_by(JobVacancy.created_at.desc()).limit(2).all()

    if not jobs:
        name = candidate.name.split()[0].title() if candidate.name else "there"
        await wa_client.send_buttons(
            to=wa_number,
            body_text=(
                f"ℹ️ *No More Openings Right Now, {name}*\n\n"
                "You have applied to all current matching vacancies in your area!\n\n"
                "Fresh vacancies are posted daily. Check out the latest roles on our website 👇"
            ),
            buttons=[
                {"id": "btn_explore_website", "title": "🌐 View all Jobs"},
            ],
            footer_text="JobInfo.pro • Made for Kerala",
        )
        return

    name = candidate.name.split()[0].title() if candidate.name else "there"
    job_blocks = []
    buttons = []
    for i, j in enumerate(jobs):
        num_prefix = f"{i+1}️⃣ " if len(jobs) > 1 else "1️⃣ "
        role_title = j.job_title.strip()
        loc = (j.district_region or "Kerala").strip().title()
        if j.exact_location:
            exact = j.exact_location.strip().title()
            candidate_loc = f"{exact}, {loc}"
            if len(candidate_loc) <= 25:
                loc = candidate_loc

        sal = _label(SALARY_LABELS, j.salary_range, fallback="")

        lines = [
            f"{num_prefix}*{role_title}*",
            f"• 🔖 Job Code: {j.job_code}",
            f"• 📍 {loc}",
        ]
        if sal:
            lines.append(f"• 💰 {sal}")
        job_blocks.append("\n".join(lines))
        buttons.append({"id": f"view_job_{j.job_code}", "title": f"📋 View {j.job_code}"[:20]})

    buttons.append({"id": "btn_explore_website", "title": "🌐 View all Jobs"})

    if latest_app:
        context_sub = f"_{name}, based on your recent applications and profile, here are top opportunities you can apply for right now._"
    else:
        context_sub = f"_{name}, based on your profile and preferences, here are top opportunities you can apply for right now._"

    body_text = (
        "🎯 *Recommended Jobs for You:*\n\n"
        + "\n\n".join(job_blocks)
        + f"\n\n{context_sub}\n\n"
        "Tap a job below for details 👇"
    )

    await wa_client.send_buttons(
        to=wa_number,
        body_text=body_text,
        buttons=buttons,
        footer_text="JobInfo.pro • Made for Kerala",
    )


async def handle_suggest_jobs_near_me(wa_number: str, db: Session) -> None:
    """Finds matching jobs near candidate's preferred district."""
    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
    if not candidate or not candidate.district:
        if candidate:
            await handle_suggest_weighted_jobs(wa_number, db)
        else:
            await handle_create_general_profile(wa_number, db)
        return

    applied_ids = [
        a.vacancy_id for a in db.query(CandidateApplication.vacancy_id)
        .filter_by(candidate_id=candidate.id).all()
    ]

    from sqlalchemy import func
    cand_dist = _normalize_district(candidate.district).lower()
    query = db.query(JobVacancy).filter(
        JobVacancy.is_active == True,
        JobVacancy.status == "approved",
    )
    if applied_ids:
        query = query.filter(JobVacancy.id.notin_(applied_ids))

    query = query.filter(func.lower(JobVacancy.district_region).like(f"%{cand_dist}%"))

    if candidate.category:
        cat = candidate.category.strip().lower()
        jobs = query.order_by(
            (func.lower(JobVacancy.job_category) == cat).desc(),
            JobVacancy.created_at.desc(),
        ).limit(2).all()
    else:
        jobs = query.order_by(JobVacancy.created_at.desc()).limit(2).all()

    if not jobs:
        dist_display = candidate.district.strip().title()
        await wa_client.send_buttons(
            to=wa_number,
            body_text=(
                f"ℹ️ *No Openings in {dist_display} Today*\n\n"
                "We don't have fresh vacancies in your district at this moment.\n\n"
                "Check out all active openings across Kerala on our website!"
            ),
            buttons=[
                {"id": "btn_explore_website", "title": "🌐 View Other Jobs"},
            ],
        )
        return

    name = candidate.name.split()[0].title() if candidate.name else "there"
    job_lines = []
    buttons = []
    for i, j in enumerate(jobs):
        num_emoji = "1️⃣" if i == 0 else "2️⃣"
        dist = j.district_region.strip().title() if j.district_region else candidate.district.strip().title()
        job_lines.append(f"{num_emoji} 🏷️ {j.job_title.strip()} — {dist} ({j.job_code})")
        buttons.append({"id": f"view_job_{j.job_code}", "title": f"📋 View {j.job_code}"[:20]})

    buttons.append({"id": "btn_explore_website", "title": "🌐 View Other Jobs"})

    await wa_client.send_buttons(
        to=wa_number,
        body_text=(
            f"📍 *Openings Near You in {candidate.district.strip().title()}, {name}:*\n\n"
            + "\n".join(job_lines)
            + "\n\nTap a job below for details and 1-tap application 👇"
        ),
        buttons=buttons,
        footer_text="JobInfo.pro • Made for Kerala",
    )


async def handle_my_profile_button(wa_number: str, candidate: Candidate) -> None:
    """Shows current profile information with option to edit."""
    cat_name = CATEGORY_DISPLAY_NAMES.get(candidate.category, candidate.category or "—")
    dist_name = candidate.district or "—"
    await wa_client.send_buttons(
        to=wa_number,
        body_text=(
            f"👤 *Your Profile Details:*\n\n"
            f"• *Name:* {candidate.name or '—'}\n"
            f"• *District:* {dist_name}\n"
            f"• *Preferred Job:* {cat_name}\n"
            f"• *Location:* {candidate.exact_location or '—'}\n\n"
            "Want to update your district or preferred job area? Tap below 👇"
        ),
        buttons=[
            {"id": "btn_create_profile", "title": "Update Profile"},
            {"id": "ACTION_SUGGEST_JOBS", "title": "🎯 Suggest Jobs"},
            {"id": "btn_explore_website", "title": "🌐 More on Website"},
        ],
    )


async def handle_view_job_card(wa_number: str, job_code: str, db: Session) -> None:
    """Sends rich job detail card when seeker taps [📋 View JC:X]."""
    from app.services.ad_lifecycle import ensure_ad_active
    vacancy = db.query(JobVacancy).filter_by(job_code=job_code).first()
    if not vacancy or not ensure_ad_active(vacancy, db):
        await send_position_closed_message(wa_number)
        return

    _set_state(wa_number, "seeker_viewing_job", {"job_code": job_code, "pending_job_code": job_code}, db)

    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
    already_applied = False
    if candidate:
        already_applied = (
            db.query(CandidateApplication)
            .filter_by(candidate_id=candidate.id, vacancy_id=vacancy.id)
            .first()
            is not None
        )

    body_text = seeker_job_detail_body(vacancy, already_applied=already_applied)

    if already_applied:
        buttons = [
            {"id": "ACTION_MY_APPLICATIONS", "title": "📑 My Applications"},
            {"id": f"btn_suggest_other_{vacancy.id}", "title": "🎯 Suggest More"},
            {"id": "btn_explore_website", "title": "🌐 More on Website"},
        ]
    else:
        buttons = [
            {"id": f"btn_apply_now_{vacancy.id}", "title": "⚡ Apply Now"},
            {"id": f"btn_suggest_other_{vacancy.id}", "title": "🎯 Suggest More"},
            {"id": "btn_explore_website", "title": "🌐 More on Website"},
        ]

    await wa_client.send_buttons(
        to=wa_number,
        body_text=body_text,
        buttons=buttons,
    )


async def send_cv_rescue_card(
    wa_number: str,
    candidate: Candidate,
    context: dict,
    db: Session,
) -> None:
    """
    Delivered after 10 minutes of inactivity when stuck in seeker_uploading_cv / seeker_no_cv.
    Offers 1-tap submission without CV so recruiters still receive the candidate's profile.
    """
    job_code = context.get("job_code") if context else None
    vacancy = None
    if job_code:
        vacancy = db.query(JobVacancy).filter_by(job_code=job_code).first()
    elif context and context.get("vacancy_id"):
        vacancy = db.query(JobVacancy).filter_by(id=context["vacancy_id"]).first()

    if not vacancy:
        return

    company = vacancy.recruiter.company_name if vacancy.recruiter else "the employer"

    # CRIT-1: For mandatory-CV vacancies, do not offer bypass. Provide actionable upload/suggest options.
    if vacancy.cv_required:
        await wa_client.send_buttons(
            to=wa_number,
            body_text=(
                f"👋 Still interested in the *{vacancy.job_title.strip()}* role at {company}?\n\n"
                "📌 *Note:* The employer strictly requires a CV for this position.\n\n"
                "When you have your PDF resume ready, tap below to upload your CV and complete this application. "
                "Or explore jobs that don't require a CV! 🚀"
            ),
            buttons=[
                {"id": f"UPLOAD_NEW_CV_{vacancy.job_code}", "title": "📤 Upload CV"},
                {"id": "SUGGEST_JOBS_NO_CV", "title": "🔍 Jobs Without CV"},
                {"id": "ACTION_SUGGEST_JOBS", "title": "🎯 Suggest Jobs"},
            ],
            footer_text="JobInfo.pro • Made for Kerala",
        )
        return

    # Optional CV: Allow 1-tap submission without CV
    await wa_client.send_buttons(
        to=wa_number,
        body_text=(
            f"👋 Still interested in the *{vacancy.job_title.strip()}* role at {company}? ({vacancy.job_code})\n\n"
            "Don't have a PDF resume on your phone right now? No problem! 📄\n\n"
            "You can submit your application with your JobInfo profile details today so the employer receives your application immediately.\n\n"
            "_(Note: The employer may contact you directly for any additional details)._"
        ),
        buttons=[
            {"id": f"btn_apply_rescue_{vacancy.job_code}", "title": "⚡ Apply Without CV"},
            {"id": "ACTION_SUGGEST_JOBS", "title": "🎯 Suggest More"},
        ],
    )


async def handle_apply_rescue_button(wa_number: str, job_code: str, db: Session) -> None:
    """
    User tapped [⚡ Apply Without CV] on the 10-minute CV rescue card.
    Bypasses cv_required check and creates the application with profile data (optional CV only).
    """
    vacancy = db.query(JobVacancy).filter_by(job_code=job_code).first()
    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
    if not vacancy or not candidate:
        await wa_client.send_text(to=wa_number, body="❌ This vacancy is no longer available.")
        return

    # CRIT-1 Guard: If vacancy strictly requires a CV and user has no CV, do NOT bypass
    if vacancy.cv_required:
        resume_count = db.query(CandidateResume).filter_by(candidate_id=candidate.id).count()
        has_cv = resume_count > 0 or bool(candidate.cv_path)
        if not has_cv:
            await _send_cv_required_message(wa_number, vacancy, vacancy.job_code)
            return

    # Direct submission with bypass_cv_gate=True (for optional-CV vacancies)
    await handle_apply_now_button(wa_number, vacancy.id, db, bypass_cv_gate=True)


async def handle_apply_instantly_button(wa_number: str, job_code: str, db: Session) -> None:
    """
    Registered candidate tapped [⚡ Apply Instantly] on the Quick Apply Card.
    Submits application directly.
    """
    vacancy = db.query(JobVacancy).filter_by(job_code=job_code).first()
    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
    if not vacancy or not candidate:
        await wa_client.send_text(to=wa_number, body="❌ This vacancy is no longer available.")
        return

    await handle_apply_now_button(wa_number, vacancy.id, db, bypass_cv_gate=True)


async def handle_create_general_profile(wa_number: str, db: Session | None = None) -> None:
    """Launches general registration flow without a pending job code."""
    await wa_client.send_flow(
        to=wa_number,
        flow_id=settings.FLOW_ID_SEEKER_REGISTER,
        flow_token="seeker_register",
        flow_cta="⚡ Get Started",
        body_text=(
            "🚀 *Create Job Seeker Profile*\n\n"
            "_Fill your basic details below to unlock direct interview calls from verified employers across Kerala!_\n\n"
            "*Why JobInfo?*\n"
            "• 🔒 100% Free & spam-free\n"
            "• ⚡ 1-Minute quick profile setup\n"
            "• 📲 1-Tap apply to 100+ jobs\n"
            "• 📞 Direct calls from hiring team\n"
            "• 🔔 Job matches in your district\n\n"
            "Tap the Button below to setup your profile & start applying 👇"
        ),
        flow_action_payload={
            "screen": "SEEKER_REGISTRATION",
            "data": {"pending_job_code": ""},
        },
    )
    if db:
        _set_state(
            wa_number,
            "seeker_registering",
            {
                "pending_job_code": "",
                "flow_sent_at": datetime.now(timezone.utc).isoformat(),
            },
            db,
        )


