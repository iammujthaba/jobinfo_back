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
    cv_update_confirmation_body,
    plan_renewal_body,
    registration_confirmation_body,
    seeker_job_detail_body,
)

logger = logging.getLogger(__name__)
settings = get_settings()

# Friendly display names for category keys

# Friendly display names for category keys
CATEGORY_DISPLAY_NAMES: dict[str, str] = {
    "retail": "Retail & Sales",
    "hospitality": "Hospitality & Food",
    "healthcare": "Healthcare",
    "driving": "Driving & Logistics",
    "office_admin": "Office & Admin",
    "maintenance_technician": "Maintenance & Technical",
    "it_professional": "IT & Professional",
    "gulf_abroad": "Gulf / Abroad",
    "other": "General",
}

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
    vacancy = db.query(JobVacancy).filter_by(job_code=job_code).first()
    if not vacancy or vacancy.status != "approved":
        await wa_client.send_text(
            to=wa_number,
            body="❌ This vacancy is no longer available. Browse latest jobs in our channel.",
        )
        return

    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()

    if not candidate or not candidate.registration_complete:
        # Unregistered – show register / gethelp buttons
        await wa_client.send_buttons(
            to=wa_number,
            body_text=(
                "*🚀Apply for this position via WhatsApp!*\n\n"
                f"📋 *{vacancy.job_title.strip()}*\n"
                f"🏢 {vacancy.company_name or '—'}\n"
                f"📍 {vacancy.exact_location or '—'}, {vacancy.district_region or '—'}\n\n"
                "To apply, you need to setup your profile. It's quick and free!\n\n"
                "Tap *Register Now* to complete application or *Get Help* if you need assistance."
            ),
            buttons=[
                {"id": f"btn_register_{job_code}", "title": "Register Now"},
                {"id": "help_support", "title": "Help/Support"},
            ],
        )
        # Save job_code in state so we know what to apply for after registration
        _set_state(wa_number, "seeker_pre_register", {"pending_job_code": job_code}, db)
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
        await wa_client.send_text(
            to=wa_number,
            body=f"ℹ️ You have already applied for *{vacancy.job_title.strip()}*. Status: _{existing.status.value}_",
        )
        return

    # ── Branch 1: Zero CVs on file ──────────────────────────────────────────
    resume_count = db.query(CandidateResume).filter_by(candidate_id=candidate.id).count()
    has_cv = resume_count > 0 or bool(candidate.cv_path)

    # Safety net: if candidate.cv_path exists but no CandidateResume, backfill one
    if resume_count == 0 and candidate.cv_path:
        backfill = CandidateResume(
            candidate_id=candidate.id,
            media_id=candidate.cv_path,
            category_tag=candidate.category or "other",
            is_default=True,
        )
        db.add(backfill)
        db.commit()
        resume_count = 1
        has_cv = True

    inferred_cat = _infer_job_category(vacancy)
    job_label = CATEGORY_DISPLAY_NAMES.get(inferred_cat, inferred_cat.replace("_", " ").title())

    if not has_cv:
        await wa_client.send_buttons(
            to=wa_number,
            header_text="🌟 Stand Out to the Recruiter!",
            body_text=(
                f"You're applying for an exciting *{job_label}* role — *{vacancy.job_title.strip()}*! "
                "We noticed you haven't added a CV to your profile yet.\n\n"
                "Uploading a CV gives recruiters a complete picture of your skills "
                "and dramatically boosts your chances of getting hired. 🚀"
            ),
            buttons=[
                {"id": f"MANAGE_CV_{vacancy.job_code}", "title": "📝 Upload a CV"},
                {"id": f"CONFIRM_APPLY_{vacancy.job_code}", "title": "🚀 Apply Without CV"},
            ],
            footer_text="Candidates with CVs get 5x more callbacks!",
        )
        _set_state(
            wa_number,
            "seeker_no_cv",
            {"vacancy_id": vacancy.id, "job_code": vacancy.job_code},
            db,
        )
        return

    # ── Branch 2: Has CV(s) + Category Mismatch ───────────────────────────
    candidate_cat = (candidate.category or "").strip().lower()

    if (
        candidate_cat
        and inferred_cat != "other"
        and candidate_cat != inferred_cat
    ):
        candidate_label = CATEGORY_DISPLAY_NAMES.get(candidate_cat, candidate_cat.replace("_", " ").title())
        job_label = CATEGORY_DISPLAY_NAMES.get(inferred_cat, inferred_cat.replace("_", " ").title())

        await wa_client.send_buttons(
            to=wa_number,
            header_text="🌟 Maximize Your Chances!",
            body_text=(
                f"We noticed your default CV is tailored for *{candidate_label.strip()}*, "
                f"but you're applying for an exciting *{job_label.strip()}* role!\n\n"
                "Sending a customized CV dramatically boosts your chances of "
                "getting shortlisted. Choose how you'd like to proceed below:"
            ),
            buttons=[
                {"id": f"UPLOAD_NEW_CV_{vacancy.job_code}", "title": "📤 Upload New CV"},
                {"id": f"MANAGE_CV_{vacancy.job_code}", "title": "📁 Choose Existing"},
                {"id": f"APPLY_NO_CV_{vacancy.job_code}", "title": "🚀 Apply Without CV"},
            ],
            footer_text="A tailored CV = 3x more callbacks!",
        )
        _set_state(
            wa_number,
            "seeker_cv_mismatch",
            {"vacancy_id": vacancy.id, "job_code": vacancy.job_code},
            db,
        )
        return

    # ── Standard apply prompt ─────────────────────────────────────────────
    candidate_label = CATEGORY_DISPLAY_NAMES.get(candidate_cat, candidate_cat.replace("_", " ").title()) if candidate_cat else ""
    
    await wa_client.send_buttons(
        to=wa_number,
        header_text="✅ Perfect Match!",
        body_text=(
            f"Your default CV is perfectly tailored for this {candidate_label} role. "
            "Ready to submit your application to the recruiter?\n\n"
            "Or would you want to change your current CV?"
        ),
        buttons=[
            {"id": f"CONFIRM_APPLY_{vacancy.job_code}", "title": "🚀 Submit Application"},
            {"id": f"UPLOAD_NEW_CV_{vacancy.job_code}", "title": "📤 Upload New CV"},
            {"id": f"MANAGE_CV_{vacancy.job_code}", "title": "📁 Choose Existing"},
        ],
    )
    _set_state(
        wa_number,
        "seeker_viewing_job",
        {"vacancy_id": vacancy.id, "job_code": vacancy.job_code},
        db,
    )


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

    await wa_client.send_text(
        to="917025962175",
        body=admin_alert
    )

    await wa_client.send_text(
        to=wa_number,
        body=(
            "📞 *Help Request Received!*\n\n"
            "Thanks for reaching out! We've notified our support team, and one of our dedicated agents will contact you shortly to assist you.\n\n"
            "_We appreciate your patience._ 😊\n"
            "– *Team JobInfo*"
        ),
    )
    _set_state(wa_number, "idle", {}, db)


async def handle_register_button(wa_number: str, job_code: str, db: Session) -> None:
    """Launch the registration WhatsApp Flow."""
    await wa_client.send_flow(
        to=wa_number,
        flow_id=settings.FLOW_ID_SEEKER_REGISTER,
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
    _set_state(wa_number, "seeker_registering", {"pending_job_code": job_code}, db)

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
    raw_media = flow_data.get("media_id")

    
    if raw_media:
        # Meta's File Upload returns a list of dictionaries
        if isinstance(raw_media, list) and len(raw_media) > 0:
            actual_media_id = raw_media[0].get("id")
            actual_mime = raw_media[0].get("mime_type", "application/pdf")
        else:
            actual_media_id = raw_media
            actual_mime = flow_data.get("mime_type", "application/pdf")
            
        if actual_media_id:
            cv_path = await save_cv_from_whatsapp(
                wa_number=wa_number,
                media_id=actual_media_id,
                mime_type=actual_mime,
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
        name = candidate.name.split()[0] if candidate.name else "there"
        await wa_client.send_buttons(
            to=wa_number,
            header_text="Welcome to JobInfo! 🎉",
            body_text=(
                f"🎉 *Congratulations, {name}!* Your professional profile is officially live!\n\n"
                "You're now part of Kerala's fastest-growing job network. "
                "We'll match you with opportunities tailored to your skills and preferences.\n\n"
                "What would you like to do next? 👇"
            ),
            buttons=[
                {"id": "ACTION_SUGGEST_JOBS", "title": "Suggest Jobs"},
                {"id": "ACTION_EXPLORE_JOBS", "title": "Explore all Jobs"},
            ],
            footer_text="Powered by JobInfo.pro",
        )
        # If they were in the middle of applying, resume
        state = _get_or_create_state(wa_number, db)
        pending_code = (state.context or {}).get("pending_job_code") or flow_data.get(
            "pending_job_code"
        )
        if pending_code:
            vacancy = db.query(JobVacancy).filter_by(job_code=pending_code).first()
            if vacancy:
                await _show_job_apply_prompt(wa_number, candidate, vacancy, db)


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
    wa_number: str, vacancy_id: int, db: Session
) -> None:
    """Save the job application and send confirmation."""
    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
    vacancy = db.query(JobVacancy).filter_by(id=vacancy_id).first()

    if not candidate or not vacancy:
        return

    # Double-check plan is still active
    if not _has_active_plan(candidate):
        await wa_client.send_text(to=wa_number, body=plan_renewal_body(candidate))
        return

    # ── CV-required gate ───────────────────────────────────────────────────
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

    # Confirmation with 'View Applications' button
    await wa_client.send_buttons(
        to=wa_number,
        body_text=application_confirmation_body(candidate, vacancy),
        buttons=[{"id": "btn_view_applications", "title": "View Applications"}],
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
                {"id": "ACTION_SUGGEST_JOBS", "title": "Suggest Jobs"},
            ],
        )
        return

    application = CandidateApplication(candidate_id=candidate.id, vacancy_id=vacancy.id)
    db.add(application)
    candidate.applications_used = (candidate.applications_used or 0) + 1
    db.commit()

    await wa_client.send_buttons(
        to=wa_number,
        header_text="✅ Application Submitted!",
        body_text=(
            f"We've sent your profile to the recruiter for *{vacancy.job_title.strip()}* "
            "without a CV attached.\n\n"
            "💡 *Pro tip:* Uploading a tailored CV for future applications "
            "can dramatically boost your chances. Best of luck! 🍀"
        ),
        buttons=[{"id": "btn_view_applications", "title": "View Applications"}],
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
            default_marker = " ★ Default" if r.is_default else ""
            date_str = r.uploaded_at.strftime("%d %b %Y") if r.uploaded_at else "Recently"
            rows.append({
                "id": f"SELECT_CV_{r.id}_{job_code}",
                "title": f"{tag_label} CV{default_marker}"[:24],
                "description": f"Uploaded on {date_str}",
            })
        sections.append({"title": "Your Saved CVs", "rows": rows})

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
                {"id": "ACTION_SUGGEST_JOBS", "title": "Suggest Jobs"},
            ],
        )
        return

    application = CandidateApplication(candidate_id=candidate.id, vacancy_id=vacancy.id)
    db.add(application)
    candidate.applications_used = (candidate.applications_used or 0) + 1
    db.commit()

    tag_label = CATEGORY_DISPLAY_NAMES.get(
        (resume.category_tag or "").lower(),
        (resume.category_tag or "General").replace("_", " ").title(),
    )

    await wa_client.send_buttons(
        to=wa_number,
        header_text="✅ Application Submitted!",
        body_text=(
            f"Excellent choice! We've updated your active CV to *{tag_label}* "
            f"and successfully submitted your tailored application for *{vacancy.job_title.strip()}*.\n\n"
            "The recruiter will review your profile shortly. Keep an eye on your dashboard for updates! 🎯"
        ),
        buttons=[{"id": "btn_view_applications", "title": "View Applications"}],
    )
    _set_state(wa_number, "idle", {}, db)


async def handle_upload_new_cv(wa_number: str, job_code: str, db: Session) -> None:
    """Launch the CV Update Flow with the job_code attached for post-upload application."""
    await wa_client.send_flow(
        to=wa_number,
        flow_id=settings.FLOW_ID_CV_UPDATE,
        flow_cta="Upload CV",
        body_text=(
            "📄 *Upload Your Tailored CV*\n\n"
            "Tap the button below to securely upload your newly tailored CV. "
            "We accept files up to 5MB.\n\n"
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

    # Extract ID from Meta's list structure
    if isinstance(raw_media, list) and len(raw_media) > 0:
        actual_media_id = raw_media[0].get("id")
        actual_mime = raw_media[0].get("mime_type", "application/pdf")
    else:
        actual_media_id = raw_media
        actual_mime = flow_data.get("mime_type", "application/pdf")

    cv_path = await save_cv_from_whatsapp(wa_number, actual_media_id, actual_mime)
    if not cv_path:
        btn_id = f"UPLOAD_NEW_CV_{job_code}" if job_code else "UPLOAD_NEW_CV_"
        await wa_client.send_buttons(
            to=wa_number,
            body_text="❌ Invalid file format. Please upload a PDF or CSV file.",
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
            category_tag=new_cv_category,
            is_default=True,
        )
        db.add(new_resume)
        candidate.cv_path = cv_path  # keep legacy field in sync
        candidate.cv_updates_used = (candidate.cv_updates_used or 0) + 1
        db.commit()

        # Auto-apply for the job
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

        application = CandidateApplication(candidate_id=candidate.id, vacancy_id=vacancy.id)
        db.add(application)
        candidate.applications_used = (candidate.applications_used or 0) + 1
        db.commit()

        tag_label = CATEGORY_DISPLAY_NAMES.get(
            new_cv_category.lower(),
            new_cv_category.replace("_", " ").title(),
        )

        await wa_client.send_buttons(
            to=wa_number,
            header_text="🎉 CV Uploaded & Application Sent!",
            body_text=(
                f"Your new *{tag_label}* CV has been securely uploaded Successfully!\n"
                f"Your application for *{vacancy.job_title.strip()}* has been submitted to the recruiter with *{tag_label}* CV!\n\n"
                "You're one step closer to landing your dream role. "
                "Keep the momentum going! 🚀"
            ),
            buttons=[{"id": "btn_view_applications", "title": "View Applications"}],
        )
        _set_state(wa_number, "idle", {}, db)
        return

    # ── Legacy flow (no category/job_code) ────────────────────────────────
    candidate.cv_path = cv_path
    candidate.cv_updates_used = (candidate.cv_updates_used or 0) + 1
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
    Reusable helper: 7-day summary + category breakdown + 1 latest job + dashboard CTA.
    Called from both handle_view_applications_button and handle_my_applications_menu.
    """
    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)

    # ── Most recent application ───────────────────────────────────────────
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
                "You haven't applied for any jobs yet — but that's about to change! 🚀\n\n"
                "Tap *Suggest Jobs* from the main menu to discover roles "
                "that match your profile, or browse our Jobs Channel for "
                "the latest walk-in openings.\n\n"
                "Your career journey starts with a single tap! 💪"
            ),
            button_text="Explore Dashboard",
            url=_generate_magic_dashboard_url(wa_number, db),
        )
        return

    # ── 7-day applications ────────────────────────────────────────────────
    week_apps = (
        db.query(CandidateApplication)
        .filter(
            CandidateApplication.candidate_id == candidate.id,
            CandidateApplication.applied_at >= seven_days_ago,
        )
        .all()
    )
    total_7d = len(week_apps)

    # ── Category breakdown ────────────────────────────────────────────────
    CATEGORY_LABELS = {
        "retail": ("🛍️", "Retail & Showrooms"),
        "hospitality": ("🍽️", "Hospitality & Food"),
        "healthcare": ("🏥", "Healthcare"),
        "driving": ("🚗", "Driving & Logistics"),
        "office_admin": ("🏢", "Office & Admin"),
        "maintenance_technician": ("🔧", "Maintenance & Tech"),
        "it_professional": ("💻", "IT & Professional"),
        "gulf_abroad": ("✈️", "Gulf / Abroad"),
        "other": ("📌", "Other"),
    }

    cat_counts: dict[str, int] = {}
    for app in week_apps:
        cat = _infer_job_category(app.vacancy)
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

    # ── Build message ─────────────────────────────────────────────────────
    name = candidate.name.split()[0] if candidate.name else "there"
    lines = [f"*Great momentum, {name}!✨*\n"]

    if total_7d > 0:
        lines.append(
            f"Over the last 7 days, you've applied for *{total_7d} "
            f"role{'s' if total_7d != 1 else ''}*! Keep it up — "
            "consistency is the key to landing the right opportunity.\n"
        )
        if cat_counts:
            parts = []
            for cat, count in sorted(cat_counts.items(), key=lambda x: -x[1]):
                emoji, label = CATEGORY_LABELS.get(cat, ("📌", cat.replace("_", " ").title()))
                parts.append(f"{emoji} {label}: *{count}*")
            lines.append("*Your Focus Areas:*")
            lines.append("\n".join(parts) + "\n")
    else:
        lines.append("No new applications this week — it's a perfect time to explore fresh openings!\n")

    # ── Latest application ────────────────────────────────────────────────
    status_emoji = {"applied": "✅", "shortlisted": "🌟", "rejected": "❌"}
    status_label = {"applied": "Applied", "shortlisted": "Shortlisted", "rejected": "Not Selected"}

    v = latest.vacancy
    emoji = status_emoji.get(latest.status.value, "❓")
    label = status_label.get(latest.status.value, latest.status.value.title())
    company = f" — {v.company_name}" if v.company_name else ""

    lines.append("*Your Latest Application:*")
    lines.append(f"  {emoji}  *{v.job_title.strip()}*{company} · _{label}_")

    lines.append(
        "\nTo view your profile strength and manage your CV's, "
        "log in to your dashboard below 👇"
    )

    await wa_client.send_cta_url(
        to=wa_number,
        header_text="📊 Your Application Summary",
        body_text="\n".join(lines),
        button_text="View Your Profile",
        url=_generate_magic_dashboard_url(wa_number, db),
        footer_text="Updated in real-time",
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

WHATSAPP_CHANNEL_URL = "https://whatsapp.com/channel/0029VbBrkDB8fewxd9QIMA2k"


def _generate_magic_dashboard_url(wa_number: str, db: Session) -> str:
    import secrets
    from datetime import datetime, timedelta, timezone
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=365)
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
    "retail": ["retail", "sales", "showroom", "cashier", "store", "billing", "floor manager", "packing"],
    "hospitality": ["hotel", "restaurant", "chef", "cook", "waiter", "kitchen", "housekeeping", "server", "food"],
    "healthcare": ["nurse", "caretaker", "clinic", "pharmacy", "lab", "hospital", "medical", "physioth"],
    "driving": ["driver", "delivery", "logistics", "forklift", "taxi", "auto", "vehicle"],
    "office_admin": ["receptionist", "data entry", "accountant", "tally", "office", "telecaller", "bpo", "admin", "front desk"],
    "maintenance_technician": ["electrician", "mechanic", "plumber", "welder", "fitter", "technician", "ac ", "cctv", "lift"],
    "it_professional": ["software", "developer", "graphic", "designer", "digital market", "it ", "video editor", "content writer", "programmer"],
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
        footer_text="Powered by JobInfo.pro",
    )


async def handle_explore_jobs(wa_number: str) -> None:
    """Send a CTA URL inviting the seeker to the WhatsApp job channel."""
    await wa_client.send_cta_url(
        to=wa_number,
        header_text="📢 JobInfo Jobs Channel",
        body_text=(
            "🔥 *Stay ahead of the crowd!*\n\n"
            "Our WhatsApp Channel is updated daily with the freshest walk-in interviews "
            "and urgent vacancies across Kerala and beyond.\n\n"
            "Join now so you never miss an opportunity — your next job could be one tap away! 🚀"
        ),
        button_text="Join Channel",
        url=WHATSAPP_CHANNEL_URL,
        footer_text="Free • Instant updates • No spam",
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


async def handle_suggest_jobs(wa_number: str, db: Session) -> None:
    """
    Suggest matching jobs: if registered → find jobs by category, else → registration flow.
    """
    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()

    if not candidate or not candidate.registration_complete:
        await wa_client.send_flow(
            to=wa_number,
            flow_id=settings.FLOW_ID_SEEKER_REGISTER,
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

    # ── Find matching jobs based on candidate's category ──────────────────
    from sqlalchemy import func
    category = (candidate.category or "").strip().lower()

    matching_jobs = (
        db.query(JobVacancy)
        .filter(JobVacancy.status == "approved")
        .filter(func.lower(JobVacancy.job_category) == category)
        .order_by(JobVacancy.approved_at.desc())
        .limit(5)
        .all()
    )

    # Fallback: if no category match, show latest jobs
    if not matching_jobs:
        matching_jobs = (
            db.query(JobVacancy)
            .filter(JobVacancy.status == "approved")
            .order_by(JobVacancy.approved_at.desc())
            .limit(3)
            .all()
        )

    if not matching_jobs:
        # No active jobs at all
        await wa_client.send_cta_url(
            to=wa_number,
            header_text="JobInfo — Job Suggestions",
            body_text=(
                "*We're currently sourcing new roles in your field.*\n\n"
                "Our team is working hard to bring fresh opportunities that match "
                f"your expertise. In the meantime, keep an eye on our Jobs Channel "
                "for daily walk-in interviews and urgent openings.\n\n"
                "We'll match you as soon as new roles come in — hang tight! 💪"
            ),
            button_text="Browse Jobs Channel",
            url=WHATSAPP_CHANNEL_URL,
            footer_text="Updated daily with new opportunities",
        )
        return

    # ── Send individual job cards ─────────────────────────────────────────
    await wa_client.send_text(
        to=wa_number,
        body=(
            f"🎯 *Great picks for you, {candidate.name.split()[0] if candidate.name else 'there'}!*\n\n"
            "Based on your profile, here are the top opportunities we've found. "
            "Tap *Apply Now* on any job that excites you! 👇\n\n"
            "_We suggest up to 3 jobs_"
        ),
    )

    for job in matching_jobs:
        salary_line = f"💰  *Salary:* {job.salary_range}\n" if job.salary_range else ""
        exp_line = f"📋  *Experience:* {job.experience_required}\n" if job.experience_required else ""

        body = (
            f"🏷️ *{job.job_title.strip()}*\n"
            f"🏢 {job.company_name or 'Company'}\n"
            f"📍 {job.exact_location or '—'}, {job.district_region or '—'}\n"
            f"💰 {salary_line}"
            f"🎓 {exp_line}"
            f"\n_{job.job_description[:120] + '…' if job.job_description and len(job.job_description) > 120 else job.job_description or ''}_"
        )

        await wa_client.send_buttons(
            to=wa_number,
            body_text=body.strip(),
            buttons=[
                {"id": f"btn_apply_now_{job.id}", "title": "Apply Now"},
            ],
            footer_text=f"Job Code: {job.job_code}",
        )

