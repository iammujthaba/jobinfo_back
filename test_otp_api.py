import urllib.request
import json
data = json.dumps({"wa_number": "1234567890"}).encode('utf-8')
req = urllib.request.Request('http://localhost:8000/api/otp/send', data=data, headers={'Content-Type': 'application/json'})
try:
    response = urllib.request.urlopen(req)
    print(response.read().decode())
except Exception as e:
    print(e)
    if hasattr(e, 'read'):
        print(e.read().decode())
