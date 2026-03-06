"""
Job Seeker conversation handler (state machine).
Manages the full job seeker lifecycle:
  - Apply link tap → check if registered
  - New seeker: register or callback
  - Registered: check active plan → show job + apply/update CV buttons
  - Handle flow completions for registration and CV update
  - Application submission
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (
    Candidate, CandidateApplication, CallbackRequest,
    ConversationState, JobVacancy, SubscriptionPlan, SubscriptionPlanName
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

# WhatsApp Flow IDs – replace after creating in Meta Flow Builder
FLOW_ID_SEEKER_REGISTER = "778992404800985"
FLOW_ID_SELECT_PLAN = "YOUR_SELECT_PLAN_FLOW_ID"
FLOW_ID_CV_UPDATE = "1830313444154607"
FLOW_ID_MY_APPLICATIONS = "YOUR_MY_APPLICATIONS_FLOW_ID"


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
        # Unregistered – show register / callback buttons
        await wa_client.send_buttons(
            to=wa_number,
            header_text="JobInfo – Apply for Jobs via WhatsApp",
            body_text=(
                f"📋 *{vacancy.title}* | {vacancy.location}\n\n"
                "To apply, you need to register first. It's quick and free!\n\n"
                "Tap *Register Now* to continue or *Call Back* if you need help."
            ),
            buttons=[
                {"id": f"btn_register_{job_code}", "title": "Register Now"},
                {"id": "btn_callback", "title": "Call Back"},
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
            body=f"ℹ️ You have already applied for *{vacancy.title}*. Status: _{existing.status.value}_",
        )
        return

    await wa_client.send_buttons(
        to=wa_number,
        header_text="JobInfo – Job Details",
        body_text=seeker_job_detail_body(vacancy),
        buttons=[
            {"id": f"btn_apply_now_{vacancy.id}", "title": "Apply Now"},
            {"id": f"btn_update_cv_{vacancy.id}", "title": "Update CV"},
        ],
    )
    _set_state(
        wa_number,
        "seeker_viewing_job",
        {"vacancy_id": vacancy.id, "job_code": vacancy.job_code},
        db,
    )


async def handle_callback_button(wa_number: str, db: Session) -> None:
    """Save a callback request when user taps 'Call Back'."""
    req = CallbackRequest(wa_number=wa_number)
    db.add(req)
    db.commit()
    await wa_client.send_text(
        to=wa_number,
        body=(
            "📞 *Callback Requested!*\n\n"
            "Our team will contact you shortly to help you register.\n_JobInfo_"
        ),
    )
    _set_state(wa_number, "idle", {}, db)


async def handle_register_button(wa_number: str, job_code: str, db: Session) -> None:
    """Launch the registration WhatsApp Flow."""
    await wa_client.send_flow(
        to=wa_number,
        flow_id=FLOW_ID_SEEKER_REGISTER,
        flow_cta="Register Now",
        body_text=(
            "📝 *Quick Registration*\n\n"
            "Fill in your details and upload your CV.\n"
            "Takes less than 2 minutes!"
        ),
        header_text="JobInfo – Job Seeker Registration",
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
    flow_data keys: name, pin_code, post_office, category, sub_category, age, alt_phone, cv_file, pending_job_code
    """
    # Save CV
    cv_path = None
    raw_media = flow_data.get("cv_file")
    
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
            pin_code=flow_data.get("pin_code"),
            post_office=flow_data.get("post_office"),
            category=flow_data.get("category"),
            sub_category=flow_data.get("sub_category"),
            age=int(flow_data["age"]) if flow_data.get("age") else None,
            alt_phone=flow_data.get("alt_phone"),
            cv_path=cv_path,
            registration_complete=False,
        )
        db.add(candidate)
    else:
        candidate.name = flow_data.get("name", candidate.name)
        candidate.pin_code = flow_data.get("pin_code", candidate.pin_code)
        candidate.post_office = flow_data.get("post_office", candidate.post_office)
        candidate.category = flow_data.get("category", candidate.category)
        candidate.sub_category = flow_data.get("sub_category", candidate.sub_category)
        if flow_data.get("age"):
            candidate.age = int(flow_data["age"])
        candidate.alt_phone = flow_data.get("alt_phone", candidate.alt_phone)
        if cv_path:
            candidate.cv_path = cv_path

    db.commit()
    db.refresh(candidate)

    if settings.subscription_enabled:
        # Offer plan selection
        await _send_plan_selection(wa_number, db)
    else:
        # Skip subscription during launch phase
        candidate.registration_complete = True
        db.commit()
        await wa_client.send_text(
            to=wa_number,
            body=registration_confirmation_body(candidate.name, "candidate"),
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


async def handle_update_cv_button(
    wa_number: str, vacancy_id: int, db: Session
) -> None:
    """Prompt to upload a new CV via WhatsApp Flow."""
    await wa_client.send_flow(
        to=wa_number,
        flow_id=FLOW_ID_CV_UPDATE,
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
    """Process CV update from WhatsApp Flow."""
    raw_media = flow_data.get("media_id")

    if not raw_media:
        await wa_client.send_text(to=wa_number, body="⚠️ No file received. Please try again.")
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
        await wa_client.send_text(
            to=wa_number,
            body="❌ Invalid file format. Please upload a PDF or CSV file.",
        )
        return

    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
    if candidate:
        candidate.cv_path = cv_path
        candidate.cv_updates_used = (candidate.cv_updates_used or 0) + 1
        db.commit()
        await wa_client.send_text(
            to=wa_number,
            body=cv_update_confirmation_body(candidate),
        )
    _set_state(wa_number, "idle", {}, db)


async def handle_view_applications_button(wa_number: str, db: Session) -> None:
    """Show candidate's application history."""
    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
    if not candidate:
        return

    apps = (
        db.query(CandidateApplication)
        .filter_by(candidate_id=candidate.id)
        .order_by(CandidateApplication.applied_at.desc())
        .limit(10)
        .all()
    )

    if not apps:
        await wa_client.send_text(to=wa_number, body="You haven't applied for any jobs yet.")
        return

    status_emoji = {"applied": "⏳", "shortlisted": "🌟", "rejected": "❌"}
    lines = [f"📂 *Your Applications ({len(apps)}):*\n"]
    for app in apps:
        emoji = status_emoji.get(app.status.value, "❓")
        lines.append(
            f"{emoji} *{app.vacancy.title}* | {app.vacancy.location} – {app.status.value}"
        )
    lines.append("\n_Full details at jobinfo.club_")

    await wa_client.send_text(to=wa_number, body="\n".join(lines))


# ═══════════════════════════════════════════════════════════════════════════════
# Seeker Main Menu & New Button Handlers
# ═══════════════════════════════════════════════════════════════════════════════

WHATSAPP_CHANNEL_URL = "https://whatsapp.com/channel/0029VbBrkDB8fewxd9QIMA2k"
DASHBOARD_URL = "https://jobinfo.club/"

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
        header_text="JobInfo — Your Career Partner 🌟",
        body_text=(
            "👋 *Welcome to JobInfo!*\n\n"
            "We're thrilled to help you take the next step in your career. "
            "Whether you're looking for your dream job or just exploring options — we've got you covered.\n\n"
            "Choose how you'd like to start 👇"
        ),
        buttons=[
            {"id": "ACTION_SUGGEST_JOBS", "title": "Suggest Jobs"},
            {"id": "ACTION_EXPLORE_JOBS", "title": "Explore Jobs"},
            {"id": "ACTION_MY_APPLICATIONS", "title": "My Applications"},
        ],
        footer_text="Powered by JobInfo.club",
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
    Show My Applications: if registered → dashboard link, else → registration flow.
    """
    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()

    if candidate and candidate.registration_complete:
        await wa_client.send_cta_url(
            to=wa_number,
            header_text="📊 Your Applications Dashboard",
            body_text=(
                "Great news — your profile is all set up! 🎉\n\n"
                "Tap below to view your application history, check status updates, "
                "and manage your profile on the JobInfo dashboard.\n\n"
                "Stay on top of every opportunity! 💼"
            ),
            button_text="Login to Dashboard",
            url=DASHBOARD_URL,
            footer_text="Secure login via your registered number",
        )
    else:
        await wa_client.send_flow(
            to=wa_number,
            flow_id=FLOW_ID_SEEKER_REGISTER,
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
            flow_id=FLOW_ID_SEEKER_REGISTER,
            flow_cta="Register Now",
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
    category = (candidate.category or "").strip().lower()
    keywords = CATEGORY_KEYWORDS.get(category, [])

    matching_jobs: list[JobVacancy] = []

    if keywords:
        # Build an OR query: title or description contains any keyword
        from sqlalchemy import or_, func
        filters = []
        for kw in keywords:
            filters.append(func.lower(JobVacancy.title).contains(kw))
            filters.append(func.lower(JobVacancy.description).contains(kw))

        matching_jobs = (
            db.query(JobVacancy)
            .filter(JobVacancy.status == "approved")
            .filter(or_(*filters))
            .order_by(JobVacancy.approved_at.desc())
            .limit(3)
            .all()
        )

    # Fallback: if no keyword matches or "other" category, show latest jobs
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
                "😔 *We're currently sourcing new roles in your field.*\n\n"
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
            "Tap *Apply Now* on any job that excites you! 👇"
        ),
    )

    for job in matching_jobs:
        salary_line = f"💰  *Salary:* {job.salary_range}\n" if job.salary_range else ""
        exp_line = f"📋  *Experience:* {job.experience_required}\n" if job.experience_required else ""

        body = (
            f"🏢  *{job.title}*\n"
            f"📍  {job.company or 'Company'} — {job.location}\n"
            f"{salary_line}"
            f"{exp_line}"
            f"\n_{job.description[:120] + '…' if job.description and len(job.description) > 120 else job.description or ''}_"
        )

        await wa_client.send_buttons(
            to=wa_number,
            body_text=body.strip(),
            buttons=[
                {"id": f"btn_apply_now_{job.id}", "title": "Apply Now"},
            ],
            footer_text=f"Job Code: {job.job_code}",
        )

