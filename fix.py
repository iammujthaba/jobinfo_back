import sqlite3

conn = sqlite3.connect('jobinfo.db')
c = conn.cursor()
c.execute("UPDATE alembic_version SET version_num='910cda367e81'")
conn.commit()
conn.close()
print("Fixed.")
