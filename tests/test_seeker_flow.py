"""Tests for job seeker flow: apply link triggers correct response."""
import pytest
from app.db.models import JobVacancy, VacancyStatus, Recruiter


NEW_SEEKER_NUMBER = "917001000099"


def _make_apply_payload(wa_number: str, job_code: str) -> dict:
    text = f"Apply {job_code}"
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


@pytest.fixture()
def approved_vacancy(db):
    recruiter = Recruiter(wa_number="917000000001", name="Test Recruiter")
    db.add(recruiter)
    db.flush()
    vacancy = JobVacancy(
        job_code="JC:1001",
        recruiter_id=recruiter.id,
        title="Software Engineer",
        location="Kochi",
        status=VacancyStatus.approved,
    )
    db.add(vacancy)
    db.commit()
    db.refresh(vacancy)
    return vacancy


def test_new_seeker_apply_shows_register_buttons(client, mock_wa_client, approved_vacancy):
    """Unregistered seeker tapping apply link gets register/callback buttons."""
    resp = client.post("/webhook", json=_make_apply_payload(NEW_SEEKER_NUMBER, "JC:1001"))
    assert resp.status_code == 200
    mock_wa_client.send_buttons.assert_called_once()
    call_body = mock_wa_client.send_buttons.call_args.kwargs["body_text"]
    assert "register" in call_body.lower() or "Register" in call_body


def test_apply_nonexistent_job_sends_error(client, mock_wa_client):
    """Applying to a nonexistent job code sends an error message."""
    resp = client.post("/webhook", json=_make_apply_payload(NEW_SEEKER_NUMBER, "JC:9999"))
    assert resp.status_code == 200
    mock_wa_client.send_text.assert_called_once()
    call_body = mock_wa_client.send_text.call_args.kwargs["body"]
    assert "no longer available" in call_body.lower() or "❌" in call_body
