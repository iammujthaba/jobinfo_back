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
        flow_action_payload={"pending_job_code": job_code},
    )
    _set_state(wa_number, "seeker_registering", {"pending_job_code": job_code}, db)


async def handle_registration_flow_completion(
    wa_number: str, flow_data: dict, db: Session
) -> None:
    """
    Called when the seeker registration Flow completes.
    Saves candidate, optionally offers plan selection.
    flow_data keys: name, location, skills, media_id (CV), mime_type, pending_job_code
    """
    # Save CV
    cv_path = None
    if flow_data.get("media_id"):
        cv_path = await save_cv_from_whatsapp(
            wa_number=wa_number,
            media_id=flow_data["media_id"],
            mime_type=flow_data.get("mime_type", "application/pdf"),
        )

    candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
    if not candidate:
        candidate = Candidate(
            wa_number=wa_number,
            name=flow_data.get("name", ""),
            location=flow_data.get("location"),
            skills=flow_data.get("skills"),
            cv_path=cv_path,
            registration_complete=False,
        )
        db.add(candidate)
    else:
        candidate.name = flow_data.get("name", candidate.name)
        candidate.location = flow_data.get("location", candidate.location)
        candidate.skills = flow_data.get("skills", candidate.skills)
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
        flow_action_payload={"return_vacancy_id": vacancy_id},
    )
    _set_state(wa_number, "seeker_updating_cv", {"vacancy_id": vacancy_id}, db)


async def handle_cv_update_flow_completion(
    wa_number: str, flow_data: dict, db: Session
) -> None:
    """Process CV update from WhatsApp Flow."""
    media_id = flow_data.get("media_id")
    mime_type = flow_data.get("mime_type", "application/pdf")

    if not media_id:
        await wa_client.send_text(to=wa_number, body="⚠️ No file received. Please try again.")
        return

    cv_path = await save_cv_from_whatsapp(wa_number, media_id, mime_type)
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
