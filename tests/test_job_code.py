"""Tests for job code utilities."""
from app.services.job_code import parse_job_code


def test_parse_job_code_standard():
    assert parse_job_code("Apply JC:1002") == "JC:1002"


def test_parse_job_code_case_insensitive():
    assert parse_job_code("apply jc:1002") == "JC:1002"


def test_parse_job_code_no_prefix():
    assert parse_job_code("JC:9999") == "JC:9999"


def test_parse_job_code_none():
    assert parse_job_code("hello world") is None


def test_parse_job_code_my_vacancy():
    assert parse_job_code("My Vacancy") is None
