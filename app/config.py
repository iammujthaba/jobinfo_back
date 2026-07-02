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
    admin_submission_alert_numbers: str = "917025962175,917560967682"
    admin_approval_alert_numbers: str = "917025962179,919400610270"
    business_wa_number: str = ""   # the API-enabled number
    wa_channel_id: str = ""        # WhatsApp Channel phone-number ID for broadcasts
    admin_username: str = "admin"
    admin_password: str = "admin"


    # Database
    database_url: str = "sqlite:///./jobinfo.db"

    # Feature flags
    subscription_enabled: bool = False

    # Storage
    media_upload_dir: str = "uploads/cvs"

    # App
    secret_key: str = "dev-secret-key"
    app_base_url: str = "http://localhost:8080"

    # WhatsApp Flows encryption
    flow_private_key_path: str = "keys/flow_private.pem"
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
