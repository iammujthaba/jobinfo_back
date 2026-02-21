"""Tests for webhook GET verification and POST routing."""
import os
os.environ.setdefault("VERIFY_TOKEN", "testtoken")
os.environ.setdefault("APP_SECRET", "")
import pytest

TEST_VERIFY_TOKEN = "testtoken"




def test_webhook_get_valid_token(client):
    resp = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": TEST_VERIFY_TOKEN,
            "hub.challenge": "challenge_string_123",
        },
    )
    assert resp.status_code == 200
    assert resp.text == "challenge_string_123"


def test_webhook_get_invalid_token(client):
    resp = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "WRONG_TOKEN",
            "hub.challenge": "x",
        },
    )
    assert resp.status_code == 403


def test_webhook_post_valid_payload(client):
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "from": "919999999999",
                        "type": "text",
                        "text": {"body": "hello"},
                    }]
                }
            }]
        }]
    }
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200


def test_webhook_post_status_update(client):
    """Status updates (read receipts) should be accepted silently."""
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "statuses": [{
                        "id": "wamid.xxx",
                        "status": "delivered",
                        "timestamp": "1700000000",
                        "recipient_id": "919999999999",
                    }]
                }
            }]
        }]
    }
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200
