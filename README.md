# JobInfo Backend — Python Automation System

> **Replaces N8N** with a fully Python-based automation backend for the JobInfo WhatsApp Job Platform.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Web framework | FastAPI |
| Database ORM | SQLAlchemy 2.0 |
| Database | PostgreSQL (default) / SQLite (dev) |
| WhatsApp API | Meta WhatsApp Cloud API via `httpx` |
| Admin UI | Jinja2 HTML templates |
| Migrations | Alembic |
| Tests | pytest |

---

## Quick Start

### 1. Clone and install dependencies

```bash
cd e:\jobinfo\jobinfo_back_1.0
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

### 2. Configure environment

```bash
copy .env.example .env
# Edit .env with your real values
```

Key variables to fill in:

| Variable | Description |
|---|---|
| `WHATSAPP_TOKEN` | Meta permanent access token |
| `WHATSAPP_PHONE_ID` | Phone number ID from Meta Developer Portal |
| `APP_SECRET` | Meta App Secret (for webhook signature verification) |
| `VERIFY_TOKEN` | Any random string you choose – set same in Meta Webhook config |
| `DATABASE_URL` | `postgresql://user:pass@host:5432/jobinfo_db` |
| `ADMIN_WA_NUMBER` | Your personal WhatsApp number (country code, no +) |
| `BUSINESS_WA_NUMBER` | The API-enabled WhatsApp number |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | Admin panel login |

### 3. Run the server

```bash
uvicorn app.main:app --reload --port 8000
```

The app auto-creates tables and seeds subscription plans on first startup.

---

## WhatsApp Flows Setup

WhatsApp Flows UI must be built in [Meta's Flow Builder](https://business.facebook.com/wa/manage/flows/).
You need to create **4 flows** and paste their IDs into the handler files:

| Flow | Handler file | Constant |
|---|---|---|
| Recruiter Registration | `app/handlers/recruiter.py` | `FLOW_ID_RECRUITER_REGISTER` |
| Post Vacancy | `app/handlers/recruiter.py` | `FLOW_ID_POST_VACANCY` |
| Seeker Registration | `app/handlers/seeker.py` | `FLOW_ID_SEEKER_REGISTER` |
| CV Update | `app/handlers/seeker.py` | `FLOW_ID_CV_UPDATE` |

Fields your flows must collect (and return in the Flow completion payload):

**Recruiter Registration**: `name`, `company`, `location`, `email`  
**Post Vacancy**: `title`, `company`, `location`, `description`, `salary_range`, `experience_required`, `contact_info`  
**Seeker Registration**: `name`, `location`, `skills`, `media_id` (CV file), `mime_type`  
**CV Update**: `media_id`, `mime_type`

---

## WhatsApp Templates Setup

Create and submit these templates for Meta approval:

| Template Name | Type | Variables |
|---|---|---|
| `jobinfo_recruiter_welcome` | Utility | `{{1}}` name, `{{2}}` company, `{{3}}` location + 2 quick-reply buttons |

---

## Webhook Setup (Meta Developer Portal)

1. Go to **Meta for Developers → Your App → WhatsApp → Configuration**
2. Callback URL: `https://your-server.com/webhook`
3. Verify Token: same value as `VERIFY_TOKEN` in `.env`
4. Subscribe to: `messages`

> **NGROK for local testing**: `ngrok http 8000` → use the HTTPS URL as your Callback URL.

---

## Admin Panel

Browse to: `http://localhost:8000/admin`  
Login with `ADMIN_USERNAME` / `ADMIN_PASSWORD` from `.env`

| Page | URL |
|---|---|
| Dashboard | `/admin` |
| Vacancies | `/admin/vacancies` |
| Callbacks | `/admin/callbacks` |
| Abandoned Signups | `/admin/abandoned` |

---

## API Docs

Auto-generated Swagger UI: `http://localhost:8000/docs`

---

## Running Tests

```bash
pytest tests/ -v
```

Tests use an in-memory SQLite DB and mock `WhatsAppClient` (no real API calls).

---

## Project Structure

```
jobinfo_back_1.0/
├── app/
│   ├── main.py              # FastAPI entry point
│   ├── config.py            # Settings (loads .env)
│   ├── db/
│   │   ├── base.py          # SQLAlchemy engine + session
│   │   ├── models.py        # All ORM models
│   │   └── seed.py          # Seeds subscription plans
│   ├── whatsapp/
│   │   ├── client.py        # WhatsApp Cloud API client
│   │   └── templates.py     # Message body builders
│   ├── handlers/
│   │   ├── dispatcher.py    # Central webhook router (replaces N8N)
│   │   ├── recruiter.py     # Recruiter state machine
│   │   ├── seeker.py        # Job seeker state machine
│   │   └── global_handler.py# Help menu / interrupts
│   ├── routers/
│   │   ├── webhook.py       # GET+POST /webhook
│   │   ├── admin.py         # /admin/* (dashboard)
│   │   ├── api.py           # /api/* (website REST API)
│   │   └── flows.py         # /flows/callback (Flows data exchange)
│   ├── services/
│   │   ├── otp.py           # OTP generation & verification
│   │   ├── storage.py       # CV file download & validation
│   │   └── job_code.py      # JC:XXXX generation & parsing
│   └── templates/
│       └── admin/           # Jinja2 HTML templates
│           ├── base.html
│           ├── dashboard.html
│           ├── vacancies.html
│           ├── callbacks.html
│           └── abandoned.html
├── tests/
│   ├── conftest.py
│   ├── test_webhook.py
│   ├── test_recruiter_flow.py
│   ├── test_seeker_flow.py
│   ├── test_otp.py
│   └── test_job_code.py
├── uploads/cvs/             # CV storage (auto-created)
├── requirements.txt
├── .env.example
└── README.md
```

---

## Subscription Feature Flag

Subscription enforcement is **disabled by default** (`SUBSCRIPTION_ENABLED=false`).  
This lets you run the platform free-for-all during the launch phase.  
When ready, set `SUBSCRIPTION_ENABLED=true` in `.env` and restart — no code changes needed.
