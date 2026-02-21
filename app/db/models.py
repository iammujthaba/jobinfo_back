"""
All ORM models for the JobInfo platform.
Tables: Recruiter, JobVacancy, Candidate, CandidateApplication,
        SubscriptionPlan, CallbackRequest, OTPRecord, ConversationState
"""
import enum
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Enum as SAEnum,
    ForeignKey, Integer, JSON, String, Text
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base


# ─── Enums ────────────────────────────────────────────────────────────────────

class VacancyStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class ApplicationStatus(str, enum.Enum):
    applied = "applied"
    shortlisted = "shortlisted"
    rejected = "rejected"


class SubscriptionPlanName(str, enum.Enum):
    free_trial = "free_trial"
    basic = "basic"
    popular = "popular"
    advanced = "advanced"


# ─── Models ───────────────────────────────────────────────────────────────────

class Recruiter(Base):
    __tablename__ = "recruiter_table"

    id = Column(Integer, primary_key=True, index=True)
    wa_number = Column(String(20), unique=True, nullable=False, index=True)
    name = Column(String(120), nullable=False)
    company = Column(String(200))
    location = Column(String(200))
    email = Column(String(200))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    vacancies = relationship("JobVacancy", back_populates="recruiter")


class JobVacancy(Base):
    __tablename__ = "job_vacancies"

    id = Column(Integer, primary_key=True, index=True)
    job_code = Column(String(20), unique=True, nullable=False, index=True)
    recruiter_id = Column(Integer, ForeignKey("recruiter_table.id"), nullable=False)

    title = Column(String(200), nullable=False)
    company = Column(String(200))
    location = Column(String(200), nullable=False)
    description = Column(Text)
    salary_range = Column(String(100))
    experience_required = Column(String(100))
    contact_info = Column(String(200))  # hidden from public; for admin use

    status = Column(
        SAEnum(VacancyStatus, name="vacancy_status"),
        default=VacancyStatus.pending,
        nullable=False,
    )
    rejection_reason = Column(Text)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    approved_at = Column(DateTime(timezone=True))

    recruiter = relationship("Recruiter", back_populates="vacancies")
    applications = relationship("CandidateApplication", back_populates="vacancy")


class SubscriptionPlan(Base):
    """Static plan definitions – seeded once at startup."""
    __tablename__ = "subscription_plans"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(SAEnum(SubscriptionPlanName, name="plan_name"), unique=True, nullable=False)
    display_name = Column(String(50))
    price_inr = Column(Integer, default=0)          # ₹
    duration_days = Column(Integer, nullable=False)
    max_applications = Column(Integer)               # None = unlimited
    same_day_vacancy = Column(Boolean, default=False)
    max_cv_updates = Column(Integer)                 # None = unlimited
    interview_scheduling = Column(Boolean, default=False)
    vacancy_forward = Column(Boolean, default=False)
    location_filter = Column(Boolean, default=False)
    job_filter = Column(Boolean, default=False)


class Candidate(Base):
    __tablename__ = "candidate_table"

    id = Column(Integer, primary_key=True, index=True)
    wa_number = Column(String(20), unique=True, nullable=False, index=True)
    name = Column(String(120), nullable=False)
    location = Column(String(200))
    skills = Column(Text)
    cv_path = Column(String(500))   # local path or object-storage URL
    cv_updates_used = Column(Integer, default=0)

    subscription_plan_id = Column(Integer, ForeignKey("subscription_plans.id"))
    plan_expiry = Column(DateTime(timezone=True))
    applications_used = Column(Integer, default=0)
    free_trial_used = Column(Boolean, default=False)

    registration_complete = Column(Boolean, default=False)  # False if abandoned before plan
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    plan = relationship("SubscriptionPlan")
    applications = relationship("CandidateApplication", back_populates="candidate")


class CandidateApplication(Base):
    __tablename__ = "candidate_applications"

    id = Column(Integer, primary_key=True, index=True)
    candidate_id = Column(Integer, ForeignKey("candidate_table.id"), nullable=False)
    vacancy_id = Column(Integer, ForeignKey("job_vacancies.id"), nullable=False)
    status = Column(
        SAEnum(ApplicationStatus, name="application_status"),
        default=ApplicationStatus.applied,
        nullable=False,
    )
    applied_at = Column(DateTime(timezone=True), server_default=func.now())

    candidate = relationship("Candidate", back_populates="applications")
    vacancy = relationship("JobVacancy", back_populates="applications")


class CallbackRequest(Base):
    __tablename__ = "callback_requests"

    id = Column(Integer, primary_key=True, index=True)
    wa_number = Column(String(20), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    resolved = Column(Boolean, default=False)


class OTPRecord(Base):
    __tablename__ = "otp_records"

    id = Column(Integer, primary_key=True, index=True)
    wa_number = Column(String(20), nullable=False, index=True)
    otp_code = Column(String(10), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ConversationState(Base):
    """
    Tracks the current step in a multi-step WhatsApp conversation per number.
    This replaces N8N's workflow state.
    """
    __tablename__ = "conversation_states"

    id = Column(Integer, primary_key=True, index=True)
    wa_number = Column(String(20), unique=True, nullable=False, index=True)
    state = Column(String(100), default="idle")   # e.g. 'recruiter_registration', 'seeker_apply'
    context = Column(JSON, default={})             # arbitrary data for the current step
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())
