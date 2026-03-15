import sqlite3
conn = sqlite3.connect('jobinfo.db')
# Drop the old table completely
conn.execute("DROP TABLE IF EXISTS job_vacancies")
conn.commit()
conn.close()
print('Old Job Vacancy table dropped!')