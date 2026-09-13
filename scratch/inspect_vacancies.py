import subprocess
import json

ssh_cmd = [
    "ssh",
    "-i", "E:\\jobinfo\\Resorces\\Oracle\\ssh-key-2026-07-27.key",
    "-o", "StrictHostKeyChecking=no",
    "ubuntu@140.245.255.228",
    "PGPASSWORD=13217208 psql -U jobinfo_user -d jobinfo_db -h localhost -t -A -c \"SELECT json_agg(t) FROM (SELECT id, job_code, job_title, district_region, job_category, cv_required, status FROM job_vacancies WHERE job_code IN ('JC:3', 'JC:4', 'JC:9', 'JC:21', 'JC:23', 'JC:26', 'JC:240', 'JC:263', 'JC:298')) t;\""
]

res = subprocess.run(ssh_cmd, capture_output=True, text=True)
if res.returncode == 0 and res.stdout.strip():
    data = json.loads(res.stdout.strip())
    for row in data:
        print(row)
else:
    print("Error:", res.stderr)
