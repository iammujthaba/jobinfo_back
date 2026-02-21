"""
Global interrupt handler.
Handles messages that don't match any specific flow (help menu, "how it works", etc.)
"""
import logging
from sqlalchemy.orm import Session

from app.whatsapp.client import wa_client
from app.db.models import ConversationState

logger = logging.getLogger(__name__)


HELP_MENU_TEXT = (
    "👋 *Welcome to JobInfo!*\n\n"
    "Connecting Kerala's job seekers and recruiters via WhatsApp.\n\n"
    "What would you like to do?\n\n"
    "🏢 *I am a Recruiter* – Post a job vacancy\n"
    "🔍 *I am a Job Seeker* – Find jobs and apply\n"
    "ℹ️ *How it works* – Learn about JobInfo\n\n"
    "_JobInfo – Kerala's WhatsApp Job Platform_"
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
    "Website: https://jobinfo.club\n"
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
            {"id": "menu_how_it_works", "title": "How it works"},
        ],
    )


async def send_how_it_works(wa_number: str) -> None:
    await wa_client.send_text(to=wa_number, body=HOW_IT_WORKS_TEXT)


async def handle_global_button(wa_number: str, button_id: str, db: Session) -> bool:
    """
    Handle top-level menu buttons.
    Returns True if the button was handled here (so dispatcher skips other handlers).
    """
    if button_id == "menu_how_it_works":
        await send_how_it_works(wa_number)
        _reset_state(wa_number, db)
        return True

    if button_id == "menu_recruiter":
        # Trigger recruiter flow – import here to avoid circular
        from app.handlers import recruiter as recruiter_handler
        await recruiter_handler.start(wa_number, db)
        return True

    if button_id == "menu_seeker":
        await wa_client.send_text(
            to=wa_number,
            body=(
                "🔍 To apply for a job, tap any job apply link from our WhatsApp Channel.\n\n"
                "📢 Join: https://whatsapp.com/channel/jobinfo\n\n"
                "Already have a link? Tap it and we'll guide you through!"
            ),
        )
        return True

    return False


def _reset_state(wa_number: str, db: Session) -> None:
    state = db.query(ConversationState).filter_by(wa_number=wa_number).first()
    if state:
        state.state = "idle"
        state.context = {}
        db.commit()
