"""
Global interrupt handler.
Handles messages that don't match any specific flow (help menu, "how it works", etc.)
"""
import logging
from sqlalchemy.orm import Session

from app.whatsapp.client import wa_client
from app.db.models import ConversationState
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


HELP_MENU_TEXT = (
    "👋 *Welcome to JobInfo!*\n\n"
    "Connecting Kerala's talent and recruiters directly via WhatsApp.\n\n"
    "What brings you here today?\n\n"
    "🔍 *Looking for Job* – Discover openings & apply in 1 tap\n"
    "📢 *I am Hiring* – Post vacancies & connect with qualified candidates\n"
    "ℹ️ *Help & Support* – Learn how it works or chat with us\n\n"
    "_JobInfo – Kerala's First WhatsApp powered Career Portal_"
)

HOW_IT_WORKS_TEXT = (
    "ℹ️ *How JobInfo Works* 🚀\n\n"
    "*For Job Seekers:* 🎓\n"
    "1️⃣ Tap *Looking for Job* to set up your profile in 1 minute.\n"
    "2️⃣ Get matched with top job openings across Kerala on WhatsApp.\n"
    "3️⃣ Apply with 1 tap — upload a CV only when required.\n"
    "4️⃣ Track your applications anytime via your Career Dashboard.\n\n"
    "*For Employers & Recruiters:* 🏢\n"
    "1️⃣ Tap *I am Hiring* to set up your recruiter profile in seconds.\n"
    "2️⃣ Post jobs instantly via WhatsApp or jobinfo.pro.\n"
    "3️⃣ Review verified candidate applications on WhatsApp or Web.\n"
    "4️⃣ Connect with candidates directly via WhatsApp or phone call!\n\n"
    "_JobInfo – Connecting Kerala's Talent, Instantly._ 🤝"
)


async def send_help_menu(wa_number: str) -> None:
    """Send the main help / menu message with 3 quick-reply buttons."""
    await wa_client.send_buttons(
        to=wa_number,
        body_text=HELP_MENU_TEXT,
        buttons=[
            {"id": "menu_seeker", "title": "🔍 Looking for Job"},
            {"id": "menu_recruiter", "title": "📢 I am Hiring"},
            {"id": "help_support", "title": "ℹ️ Help & Support"},
        ],
    )


async def send_how_it_works(wa_number: str) -> None:
    await wa_client.send_cta_url(
        to=wa_number,
        body_text=HOW_IT_WORKS_TEXT,
        button_text="🌐 Visit Website",
        url="https://jobinfo.pro"
    )


async def send_help_support_menu(wa_number: str) -> None:
    """Send a sub-menu containing How It Works and Get Help buttons."""
    await wa_client.send_buttons(
        to=wa_number,
        body_text=(
            "🤔 *Need Help?*\n\n"
            "Choose an option below to learn how JobInfo works, or connect directly with our support team."
        ),
        buttons=[
            {"id": "menu_how_it_works", "title": "How it works"},
            {"id": "btn_gethelp", "title": "📩 Request Help"},
        ],
    )

async def handle_global_button(wa_number: str, button_id: str, db: Session) -> bool:
    """
    Handle top-level menu buttons.
    Returns True if the button was handled here (so dispatcher skips other handlers).
    """
    if button_id == "menu_how_it_works":
        await send_how_it_works(wa_number)
        _reset_state(wa_number, db)
        return True

    if button_id == "help_support":
        await send_help_support_menu(wa_number)
        _reset_state(wa_number, db)
        return True

    if button_id == "menu_recruiter":
        # Trigger recruiter flow – import here to avoid circular
        from app.handlers import recruiter as recruiter_handler
        await recruiter_handler.start(wa_number, db)
        return True

    if button_id == "menu_seeker":
        from app.handlers import seeker as seeker_handler
        from app.db.models import Candidate, CandidateApplication
        candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
        if candidate and candidate.registration_complete:
            has_applied = db.query(CandidateApplication).filter_by(candidate_id=candidate.id).first() is not None
            if not has_applied:
                await seeker_handler.send_seeker_nudge_with_jobs(wa_number, candidate, db)
            else:
                await seeker_handler.send_applied_seeker_dashboard(wa_number, candidate, db)
        else:
            await seeker_handler.handle_create_general_profile(wa_number, db)
        return True


    # ── OTP verification CTA buttons ─────────────────────────────────────────
    # Sent after successful OTP entry; user taps to open the relevant website page.

    if button_id == "otp_complete_registration":
        await wa_client.send_cta_url(
            to=wa_number,
            body_text=(
                "Tap the button below to open the registration page in your browser👇."
            ),
            button_text="Complete Profile",
            url="https://jobinfo.pro/recruiter.html",
        )
        return True

    if button_id == "otp_my_dashboard":
        await wa_client.send_cta_url(
            to=wa_number,
            body_text=(
                "👆 Tap the button below to open your recruiter dashboard."
            ),
            button_text="My Dashboard",
            url="https://jobinfo.pro/recruiter-dashboard.html",
        )
        return True

    return False


def _reset_state(wa_number: str, db: Session) -> None:
    state = db.query(ConversationState).filter_by(wa_number=wa_number).first()
    if state:
        state.state = "idle"
        state.context = {}
        db.commit()

async def route_unrecognized_message(wa_number: str, db: Session) -> None:
    """
    Personalized fallback router.
    Routes registered users directly to their respective menus,
    otherwise falls back to the generic help menu.
    """
    from app.db.models import Recruiter, Candidate
    from app.handlers import recruiter as recruiter_handler
    from app.handlers import seeker as seeker_handler

    # Quick indexed DB lookups
    is_recruiter = db.query(Recruiter).filter_by(wa_number=wa_number).first() is not None
    is_seeker = db.query(Candidate).filter_by(wa_number=wa_number).first() is not None

    if is_recruiter and not is_seeker:
        await recruiter_handler.start(wa_number, db)
    elif is_seeker and not is_recruiter:
        candidate = db.query(Candidate).filter_by(wa_number=wa_number).first()
        if candidate and candidate.registration_complete:
            from app.db.models import CandidateApplication
            has_applied = db.query(CandidateApplication).filter_by(candidate_id=candidate.id).first() is not None
            if not has_applied:
                await seeker_handler.send_seeker_nudge_with_jobs(wa_number, candidate, db)
            else:
                await seeker_handler.send_applied_seeker_dashboard(wa_number, candidate, db)
        else:
            await seeker_handler.handle_create_general_profile(wa_number, db)
    else:
        # Either unregistered, or dual-role (give them a choice)
        await send_help_menu(wa_number)
