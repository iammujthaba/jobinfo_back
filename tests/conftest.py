"""
Pytest configuration and fixtures.
Uses an in-memory SQLite DB and mocks WhatsAppClient so no real API calls are made.
"""
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Set env vars BEFORE importing app modules so Settings picks them up
os.environ.setdefault("VERIFY_TOKEN", "testtoken")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
os.environ.setdefault("APP_SECRET", "")  # skip HMAC in tests

from app.db.base import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.db.seed import PLANS  # noqa: E402
from app.db.models import SubscriptionPlan  # noqa: E402

TEST_DATABASE_URL = "sqlite:///./test.db"

engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(scope="session", autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    if db.query(SubscriptionPlan).count() == 0:
        db.add_all(PLANS)
        db.commit()
    db.close()
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db():
    connection = engine.connect()
    transaction = connection.begin()
    db = TestingSessionLocal(bind=connection)
    try:
        yield db
    finally:
        db.close()
        transaction.rollback()
        connection.close()


@pytest.fixture()
def client(db):
    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


def _make_mock_client():
    mock = MagicMock()
    mock.send_text = AsyncMock(return_value={"messages": [{"id": "fake_id"}]})
    mock.send_template = AsyncMock(return_value={"messages": [{"id": "fake_id"}]})
    mock.send_buttons = AsyncMock(return_value={"messages": [{"id": "fake_id"}]})
    mock.send_flow = AsyncMock(return_value={"messages": [{"id": "fake_id"}]})
    mock.send_list = AsyncMock(return_value={"messages": [{"id": "fake_id"}]})
    mock.get_media_url = AsyncMock(return_value="https://fake-url/media")
    mock.download_media = AsyncMock(return_value=b"%PDF-1.4 fake pdf content")
    return mock


@pytest.fixture(autouse=True)
def mock_wa_client():
    """
    Patch wa_client in every module that imports it directly by name.
    This ensures handler functions use the mock regardless of import style.
    """
    mock = _make_mock_client()
    targets = [
        "app.whatsapp.client.wa_client",
        "app.handlers.recruiter.wa_client",
        "app.handlers.seeker.wa_client",
        "app.handlers.global_handler.wa_client",
        "app.routers.api.wa_client",
    ]
    patchers = [patch(t, mock) for t in targets]
    for p in patchers:
        p.start()
    yield mock
    for p in patchers:
        p.stop()

