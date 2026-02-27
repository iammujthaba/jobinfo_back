import sqlite3

try:
    conn = sqlite3.connect('e:/jobinfo/jobinfo_back_1.0/jobinfo.db')
    cursor = conn.cursor()
    cursor.execute("ALTER TABLE conversation_states ADD COLUMN last_user_message_at DATETIME")
    conn.commit()
    print("Successfully added last_user_message_at to conversation_states.")
except sqlite3.OperationalError as e:
    print(f"Error (maybe column already exists): {e}")
finally:
    conn.close()
