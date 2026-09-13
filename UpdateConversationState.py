"""
Interactive ConversationState Time-Skipping Tool for WhatsApp QA Testing.
Allows testers to artificially rewind or fast-forward `last_user_message_at`
to immediately test time-based triggers:
  1) 5 Minutes  -> 5-minute inactivity check (welcome-back or suppression)
  2) 30 Minutes -> 30-minute abandoned lead recovery drip (m_bot_drip)
  3) 25 Hours   -> Meta 24-hour free service conversation window closure
  4) 7 Days     -> Weekly application summary digest
  5) Reset/Now  -> Fresh active interaction right now
  6) Custom     -> Any custom time delta
"""
import sys
from datetime import datetime, timedelta, timezone

from app.db.base import SessionLocal
from app.db.models import ConversationState

DEFAULT_WA_NUMBER = "917025962175"

TIME_PRESETS = {
    "1": ("5 minutes ago", timedelta(minutes=5, seconds=10), "Tests 5-minute inactivity check (send_delayed_session_menu)."),
    "2": ("30 minutes ago", timedelta(minutes=30, seconds=30), "Tests 30-minute abandoned lead recovery drip (m_bot_drip)."),
    "3": ("25 hours ago", timedelta(hours=25), "Tests Meta 24-hour free messaging window closure."),
    "4": ("7 days ago", timedelta(days=7), "Tests 7-day application summary digest trigger."),
    "5": ("Just now (Reset to 0m)", timedelta(seconds=0), "Resets user to fresh active conversation right now."),
}


def run_time_skip():
    print("=" * 65)
    print(" ⏳ JobInfo WhatsApp Conversation Time-Skipping QA Tool")
    print("=" * 65)

    # 1. Ask for WhatsApp Number
    raw_num = input(f"Enter WhatsApp Number [{DEFAULT_WA_NUMBER}]: ").strip()
    wa_number = raw_num if raw_num else DEFAULT_WA_NUMBER

    session = SessionLocal()
    try:
        state = session.query(ConversationState).filter_by(wa_number=wa_number).first()
        if not state:
            print(f"\n⚠️ No ConversationState found for {wa_number}. Creating a new one...")
            state = ConversationState(wa_number=wa_number, state="idle", context={})
            session.add(state)
            session.commit()
            session.refresh(state)

        ctx = dict(state.context or {})
        last_msg_str = (
            state.last_user_message_at.strftime("%Y-%m-%d %H:%M:%S UTC")
            if state.last_user_message_at
            else "None (never messaged)"
        )
        print(f"\n📱 User: {wa_number}")
        print(f"📌 Current State: {state.state}")
        print(f"🕒 Current last_user_message_at: {last_msg_str}")
        if ctx.get("pending_job_code"):
            print(f"💼 Pending Job Code in Context: {ctx.get('pending_job_code')}")
        if ctx.get("drip_sent_at"):
            print(f"⚠️ drip_sent_at is currently set: {ctx.get('drip_sent_at')}")

        # 2. Select Time Skip Preset
        print("\n" + "-" * 65)
        print("Choose how much time to artificially skip:")
        for key, (label, _, desc) in TIME_PRESETS.items():
            print(f"  [{key}] {label:<25} -> {desc}")
        print(f"  [6] Custom minutes/hours")
        print("-" * 65)

        choice = input("Select an option (1-6) [2]: ").strip()
        if not choice:
            choice = "2"

        now = datetime.now(timezone.utc)
        target_delta = None
        scenario_label = ""

        if choice in TIME_PRESETS:
            scenario_label, target_delta, _ = TIME_PRESETS[choice]
        elif choice == "6":
            unit = input("Enter unit (m=minutes, h=hours, d=days) [m]: ").strip().lower() or "m"
            amount = input("Enter amount of time to skip back: ").strip()
            try:
                val = float(amount)
                if unit.startswith("d"):
                    target_delta = timedelta(days=val)
                elif unit.startswith("h"):
                    target_delta = timedelta(hours=val)
                else:
                    target_delta = timedelta(minutes=val)
                scenario_label = f"Custom: {val} {unit} ago"
            except ValueError:
                print("❌ Invalid number entered. Aborting.")
                return
        else:
            print("❌ Invalid choice. Aborting.")
            return

        # 3. Calculate new last_user_message_at
        new_time = now - target_delta
        state.last_user_message_at = new_time
        state.updated_at = new_time

        # 4. Check for drip_sent_at flag if testing bot drip
        cleared_drip_flag = False
        if ctx.get("drip_sent_at") and choice in ("1", "2", "6"):
            clear_opt = input("\nClear previous 'drip_sent_at' flag so the bot drip can re-trigger? (Y/n): ").strip().lower()
            if clear_opt != "n":
                ctx.pop("drip_sent_at", None)
                ctx.pop("help_messaged_at", None)
                state.context = ctx
                cleared_drip_flag = True

        session.commit()

        print("\n" + "=" * 65)
        print(" ✅ Time Skip Successfully Applied!")
        print("=" * 65)
        print(f"Scenario: {scenario_label}")
        print(f"Updated last_user_message_at to: {new_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        if cleared_drip_flag:
            print("✨ Cleared previous drip_sent_at flag in context (ready to re-trigger drip).")
        print("\nWhat this simulates:")
        if choice == "1":
            print("  • User idle for 5+ minutes.")
            print("  • Test `send_delayed_session_menu` (welcome back vs suppression if pending context).")
        elif choice == "2":
            print("  • User idle for 30+ minutes.")
            print("  • Test `execute_bot_drip` or let the background daemon trigger m_bot_drip.")
        elif choice == "3":
            print("  • User idle for >24 hours.")
            print("  • Free interactive 24-hour Meta session window is now closed.")
        elif choice == "4":
            print("  • User idle for 7+ days.")
            print("  • Eligible for weekly summary digest.")
        elif choice == "5":
            print("  • Fresh active interaction right now.")
        print("=" * 65)

    except Exception as e:
        print(f"\n❌ Error updating ConversationState: {e}")
        session.rollback()
    finally:
        session.close()


if __name__ == "__main__":
    run_time_skip()
