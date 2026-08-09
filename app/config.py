from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # WhatsApp Cloud API
    whatsapp_token: str = ""
    whatsapp_phone_id: str = ""
    whatsapp_business_account_id: str = ""
    app_secret: str = ""
    verify_token: str = ""

    # Admin
    admin_wa_number: str = ""      # kept for legacy references, but deprecated
    admin_submission_alert_numbers: str = ""   # comma-separated WA numbers; set in .env
    admin_approval_alert_numbers: str = ""     # comma-separated WA numbers; set in .env
    business_wa_number: str = ""   # the API-enabled number
    wa_channel_id: str = ""        # WhatsApp Channel phone-number ID for broadcasts
    admin_username: str = ""
    admin_password: str = ""

    # JobZon Admin — distinct role, separate panel (/jobzon)
    # Set these three values in .env to activate the JobZon admin panel.
    jobzon_admin_username: str = ""
    jobzon_admin_password: str = ""
    jobzon_admin_wa_number: str = ""    # Entering this WA number in recruiter login
                                        # triggers a redirect to /admin/login instead of OTP

    # Database
    database_url: str = ""

    # Feature flags
    subscription_enabled: bool = False
    debug_webhook_logging: bool = False

    # Storage
    media_upload_dir: str = ""

    # App — no hardcoded defaults; must be set in .env per environment
    secret_key: str = ""
    app_base_url: str = ""

    # WhatsApp Flows encryption
    flow_private_key_path: str = ""
    flow_private_key_passphrase: str = ""

    # WhatsApp Flow IDs
    FLOW_ID_SEEKER_REGISTER: str = ""
    FLOW_ID_SELECT_PLAN: str = ""
    FLOW_ID_CV_UPDATE: str = ""
    FLOW_ID_MY_APPLICATIONS: str = ""
    FLOW_ID_RECRUITER_REGISTER: str = ""
    FLOW_ID_MY_VACANCIES: str = ""
    FLOW_ID_POST_VACANCY: str = ""

    class Config:
        env_file = ".env"
        case_sensitive = False

    @property
    def submission_admins(self) -> list[str]:
        return [n.strip() for n in self.admin_submission_alert_numbers.split(",") if n.strip()]

    @property
    def approval_admins(self) -> list[str]:
        return [n.strip() for n in self.admin_approval_alert_numbers.split(",") if n.strip()]


@lru_cache()
def get_settings() -> Settings:
    return Settings()
