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
            print("  • Evaluates `send_delayed_session_menu` (welcome back vs suppression if pending context).")
        elif choice == "2":
            print("  • User idle for 30+ minutes.")
            print("  • Triggers `execute_bot_drip` (m_bot_drip abandoned lead recovery message).")
        elif choice == "3":
            print("  • User idle for >24 hours.")
            print("  • Free interactive 24-hour Meta session window is now closed.")
        elif choice == "4":
            print("  • User idle for 7+ days.")
            print("  • Eligible for weekly summary digest.")
        elif choice == "5":
            print("  • Fresh active interaction right now.")
        print("=" * 65)

        # 5. Immediate trigger execution
        if choice in ("1", "2"):
            action_name = "5-minute inactivity evaluator" if choice == "1" else "30-minute automated bot drip (m_bot_drip)"
            exec_now = input(f"\n🚀 Would you like to immediately execute the {action_name} now? [Y/n]: ").strip().lower()
            if exec_now != "n":
                import asyncio
                if choice == "1":
                    from app.handlers.dispatcher import send_delayed_session_menu
                    current_ctx = dict(state.context or {})
                    pending_job = current_ctx.get("pending_job_code") or current_ctx.get("job_code")
                    in_progress = state.state in ("seeker_registering", "seeker_no_cv", "seeker_upload_cv", "seeker_cv_mismatch")

                    print("\n" + "─" * 65)
                    print("🔍 Running 5-Minute Inactivity Evaluator...")
                    if pending_job or in_progress:
                        print(f"🛡️ [Evaluator Decision - m_5m_inactivity_check]")
                        print(f"  • Candidate State: {state.state}")
                        print(f"  • Pending Vacancy Context: {pending_job}")
                        print("  • Decision: Generic 'Welcome back' menu is SUPPRESSED to protect candidate focus.")
                        print("  • Result: No message sent to WhatsApp (as designed by the suppression rule).")
                        print("  • Next trigger: At 30 minutes, m_bot_drip will fire if candidate remains idle.")
                    else:
                        print("📨 Candidate has no pending application. Sending Welcome Back menu...")
                        asyncio.run(send_delayed_session_menu(wa_number, bypass_sleep=True))
                        print("✅ Sent 'Welcome back to JobInfo!' menu to WhatsApp.")
                    print("─" * 65)

                elif choice == "2":
                    from app.services.bot_drip import execute_bot_drip
                    print("\n" + "─" * 65)
                    print("🚀 Executing Automated WhatsApp Bot Drip Follow-up...")
                    res = asyncio.run(execute_bot_drip(session, max_batch=1, target_wa=wa_number))
                    if res.get("sent_count", 0) > 0:
                        item = res["sent_leads"][0]
                        print(f"✅ Bot drip successfully SENT to {item['wa_number']}!")
                        print(f"  • Job Code: {item.get('job_code')}")
                        print("  • Message: 'Hi! 👋 We noticed you started applying for...'")
                        print("  • Buttons: [⚡ Complete Now] [🔍 Browse Other Jobs]")
                        print("📱 Please check WhatsApp now!")
                    else:
                        print("⚠️ Bot drip did not send. Diagnostics:")
                        if res.get("skipped_leads"):
                            for sk in res["skipped_leads"]:
                                print(f"  • Error: {sk.get('error')}")
                        else:
                            idle = (datetime.now(timezone.utc) - new_time).total_seconds()
                            print(f"  • Idle seconds: {idle:.0f}s (requires >= 1800s / 30m)")
                            print(f"  • Context drip_sent_at: {state.context.get('drip_sent_at') if state.context else None}")
                    print("─" * 65)

    except Exception as e:
        print(f"\n❌ Error updating ConversationState: {e}")
        session.rollback()
    finally:
        session.close()


if __name__ == "__main__":
    run_time_skip()
