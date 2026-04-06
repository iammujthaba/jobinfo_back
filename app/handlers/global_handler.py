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
    "Connecting Kerala's job seekers and recruiters via WhatsApp.\n\n"
    "What would you like to do?\n\n"
    "🏢 *I am a Recruiter* – Post a job vacancy\n"
    "🔍 *I am a Job Seeker* – Find jobs and apply\n"
    "ℹ️ *Help/Support* – Get support from our team\n\n"
    "_JobInfo – Kerala's First WhatsApp powered Career Portal_"
)

HOW_IT_WORKS_TEXT = (
    "ℹ️ *How JobInfo Works*\n\n"
    "*For Recruiters:*\n"
    "1️⃣ Send *My Vacancy* to this number\n"
    "2️⃣ Register once (takes < 2 minutes)\n"
    "3️⃣ Post vacancies directly from WhatsApp\n"
    "4️⃣ We broadcast to thousands of job seekers\n\n"
    "*For Job Seekers:*\n"
    "1️⃣ Join our WhatsApp Channel\n"
    "2️⃣ See daily job posts with apply links\n"
    "3️⃣ Tap the link → register once → apply!\n"
    "4️⃣ Track your applications right here\n\n"
    "_JobInfo – Connecting Kerala's talent_"
)


async def send_help_menu(wa_number: str) -> None:
    """Send the main help / menu message with 3 quick-reply buttons."""
    await wa_client.send_buttons(
        to=wa_number,
        body_text=HELP_MENU_TEXT,
        buttons=[
            {"id": "menu_recruiter", "title": "I am a Recruiter"},
            {"id": "menu_seeker", "title": "I am a Job Seeker"},
            {"id": "help_support", "title": "Help/Support"},
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
            {"id": "btn_gethelp", "title": "Get Help"},
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
        # Check if they have an active registration by importing seeker_handler
        from app.handlers import seeker as seeker_handler
        await seeker_handler.send_seeker_greeting_menu(wa_number)
        return True

    return False


def _reset_state(wa_number: str, db: Session) -> None:
    state = db.query(ConversationState).filter_by(wa_number=wa_number).first()
    if state:
        state.state = "idle"
        state.context = {}
        db.commit()
