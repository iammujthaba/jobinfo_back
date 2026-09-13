import subprocess
import json

ssh_cmd = [
    "ssh",
    "-i", "E:\\jobinfo\\Resorces\\Oracle\\ssh-key-2026-07-27.key",
    "-o", "StrictHostKeyChecking=no",
    "ubuntu@140.245.255.228",
    "PGPASSWORD=13217208 psql -U jobinfo_user -d jobinfo_db -h localhost -t -A -c \"SELECT json_agg(t) FROM (SELECT cs.wa_number, cs.state, cs.context, cs.last_user_message_at FROM conversation_states cs WHERE cs.state = 'idle' AND cs.wa_number NOT IN (SELECT wa_number FROM candidate_table) AND cs.wa_number NOT IN (SELECT wa_number FROM recruiter_table)) t;\""
]

res = subprocess.run(ssh_cmd, capture_output=True, text=True)
if res.returncode == 0 and res.stdout.strip():
    data = json.loads(res.stdout.strip())
    print(f"Total idle unregistered: {len(data)}")
    for row in data[:10]:
        print(row)
else:
    print("Error:", res.stderr)
