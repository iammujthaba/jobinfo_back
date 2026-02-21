"""
Root-level conftest — sets environment variables BEFORE pydantic-settings
loads any module, and clears the lru_cache so get_settings() re-reads them.
"""
import os

# Must be set before any app module is imported
os.environ["VERIFY_TOKEN"] = "testtoken"
os.environ["APP_SECRET"] = ""
os.environ["DATABASE_URL"] = "sqlite:///./test.db"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin"
