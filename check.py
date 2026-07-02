import sqlite3
import pprint

conn = sqlite3.connect('jobinfo.db')
c = conn.cursor()
c.execute("PRAGMA table_info('job_vacancies')")
pprint.pprint(c.fetchall())
conn.close()
