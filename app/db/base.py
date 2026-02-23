from sqlalchemy import (
    create_engine, Column, Integer, String, Boolean, DateTime,
    Text, ForeignKey, JSON, Enum as SAEnum
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy.sql import func
import enum
from app.config import get_settings

settings = get_settings()

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI dependency that yields a DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create all tables then safely add any new columns that may not exist yet."""
    Base.metadata.create_all(bind=engine)
    _migrate_columns()


def _migrate_columns():
    """
    Safe, idempotent column additions for SQLite (which doesn't support
    IF NOT EXISTS in ALTER TABLE).  Each ALTER is wrapped in a try/except so
    that re-running on an already-migrated DB is harmless.
    """
    migrations = [
        "ALTER TABLE job_vacancies ADD COLUMN is_edited BOOLEAN NOT NULL DEFAULT 0",
        "ALTER TABLE job_vacancies ADD COLUMN edited_at DATETIME",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(__import__("sqlalchemy").text(sql))
                conn.commit()
            except Exception:
                pass  # column already exists – skip silently

