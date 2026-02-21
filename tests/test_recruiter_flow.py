"""Tests for recruiter flow: new recruiter triggers registration flow."""
import pytest


NEW_RECRUITER_NUMBER = "917001000001"


def _make_text_payload(wa_number: str, text: str) -> dict:
    return {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "from": wa_number,
                        "type": "text",
                        "text": {"body": text},
                    }]
                }
            }]
        }]
    }


def _make_button_payload(wa_number: str, button_id: str) -> dict:
    return {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "from": wa_number,
                        "type": "interactive",
                        "interactive": {
                            "type": "button_reply",
                            "button_reply": {"id": button_id, "title": "Test"},
                        },
                    }]
                }
            }]
        }]
    }


def test_new_recruiter_sends_flow(client, mock_wa_client):
    """Posting 'My Vacancy' from unknown number should trigger registration Flow."""
    resp = client.post("/webhook", json=_make_text_payload(NEW_RECRUITER_NUMBER, "My Vacancy"))
    assert resp.status_code == 200
    mock_wa_client.send_flow.assert_called_once()
    call_kwargs = mock_wa_client.send_flow.call_args
    assert call_kwargs.kwargs["to"] == NEW_RECRUITER_NUMBER


def test_my_vacancies_variant_triggers_recruiter(client, mock_wa_client):
    """'My Vacancies' (plural) should also trigger recruiter flow."""
    resp = client.post("/webhook", json=_make_text_payload(NEW_RECRUITER_NUMBER, "My Vacancies"))
    assert resp.status_code == 200
    mock_wa_client.send_flow.assert_called()


def test_unknown_text_sends_help_menu(client, mock_wa_client):
    """Random text should send the help menu buttons."""
    resp = client.post("/webhook", json=_make_text_payload(NEW_RECRUITER_NUMBER, "random text here"))
    assert resp.status_code == 200
    mock_wa_client.send_buttons.assert_called_once()
