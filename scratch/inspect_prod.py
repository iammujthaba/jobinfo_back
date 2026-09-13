import subprocess
import json

ssh_cmd = [
    "ssh",
    "-i", "E:\\jobinfo\\Resorces\\Oracle\\ssh-key-2026-07-27.key",
    "-o", "StrictHostKeyChecking=no",
    "ubuntu@140.245.255.228",
    "PGPASSWORD=13217208 psql -U jobinfo_user -d jobinfo_db -h localhost -t -A -c \"SELECT json_agg(t) FROM (SELECT wa_number, state, context, last_user_message_at FROM conversation_states WHERE state != 'idle') t;\""
]

res = subprocess.run(ssh_cmd, capture_output=True, text=True)
if res.returncode == 0 and res.stdout.strip():
    data = json.loads(res.stdout.strip())
    print(f"Total non-idle states: {len(data)}")
    for row in data:
        print(row)
else:
    print("Error:", res.stderr)
