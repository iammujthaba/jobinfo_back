import sys
from datetime import datetime, timedelta, timezone

from app.db.base import SessionLocal
from app.db.models import ConversationState

TEST_WA_NUMBER = "917025962175"

def close_24h_window():
    print(f"=== Closing 24-hour window for {TEST_WA_NUMBER} ===")
    
    session = SessionLocal()
    try:
        state = session.query(ConversationState).filter_by(wa_number=TEST_WA_NUMBER).first()
        
        if not state:
            print("No ConversationState found for this number. Creating one...")
            state = ConversationState(wa_number=TEST_WA_NUMBER, state="idle")
            session.add(state)
            
        # Set the last message to 3 days ago (well outside the 24-hour window)
        old_time = datetime.now(timezone.utc) - timedelta(days=3)
        state.last_user_message_at = old_time
        session.commit()
        
        print(f"✅ Success! The 24-hour window is now CLOSED.")
        print(f"last_user_message_at has been artificially set to: {old_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print("\nYou can now manually test your features as if the user hasn't interacted with the bot in 3 days.")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        session.rollback()
    finally:
        session.close()

if __name__ == "__main__":
    close_24h_window()
