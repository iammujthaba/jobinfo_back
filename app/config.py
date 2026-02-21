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
    admin_wa_number: str = ""      # personal number for vacancy alerts
    business_wa_number: str = ""   # the API-enabled number
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
    app_base_url: str = "http://localhost:8000"

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    return Settings()
