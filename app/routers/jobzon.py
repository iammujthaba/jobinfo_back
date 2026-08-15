"""
JobZon Admin Panel router — /jobzon/*

This router is exclusively for the JobZon admin role.
Authentication: signed session cookie with role == 'jobzon_admin'
               (set via POST /admin/login using jobzon admin credentials).

Access is intentionally read-only except for:
  - Creating recruiter profiles (no OTP — JobZon is a trusted intermediary)
  - Posting vacancies on behalf of recruiters (enters normal approval pipeline)

No data export endpoints are provided anywhere in this router.
"""
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.base import get_db
from app.db.models import (
    Candidate, CandidateApplication, CandidateResume,
    JobVacancy, Recruiter,
)
from app.routers.admin import require_jobzon_admin
from app.services.job_code import generate_job_code

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/jobzon", tags=["jobzon"])
templates = Jinja2Templates(directory="app/templates")


# ─── Dashboard ────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def jobzon_dashboard(
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(require_jobzon_admin),
):
    """JobZon Admin — overview dashboard with platform summary stats."""
    total_seekers     = db.query(Candidate).filter_by(registration_complete=True).count()
    total_recruiters  = db.query(Recruiter).count()
    approved_vacancies = db.query(JobVacancy).filter_by(status="approved").count()
    pending_vacancies  = db.query(JobVacancy).filter_by(status="pending").count()

    return templates.TemplateResponse(
        "jobzon/dashboard.html",
        {
            "request": request,
            "total_seekers": total_seekers,
            "total_recruiters": total_recruiters,
            "approved_vacancies": approved_vacancies,
            "pending_vacancies": pending_vacancies,
        },
    )


def _safe_int(val: Any, default: int = 0) -> int:
    if val is None or val == "":
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


# ─── Job Seeker Directory ─────────────────────────────────────────────────────

@router.get("/seekers", response_class=HTMLResponse)
async def jobzon_seekers(
    request: Request,
    district: str = "",
    category: str = "",
    gender: str = "",
    age_min: str = "",
    age_max: str = "",
    page: str = "1",
    db: Session = Depends(get_db),
    _: str = Depends(require_jobzon_admin),
):
    """Job Seeker Directory with server-side filtering and pagination."""
    PAGE_SIZE = 50

    age_min_int = _safe_int(age_min, 0)
    age_max_int = _safe_int(age_max, 0)
    page_int = max(1, _safe_int(page, 1))

    query = db.query(Candidate).filter_by(registration_complete=True)

    if district:
        query = query.filter(Candidate.district.ilike(f"%{district}%"))
    if category:
        query = query.filter(Candidate.category.ilike(f"%{category}%"))
    if gender:
        query = query.filter(Candidate.gender.ilike(f"%{gender}%"))
    if age_min_int > 0:
        query = query.filter(Candidate.age >= age_min_int)
    if age_max_int > 0:
        query = query.filter(Candidate.age <= age_max_int)

    total = query.count()
    seekers = (
        query
        .order_by(Candidate.created_at.desc())
        .offset((page_int - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
        .all()
    )
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    # Build filter options from distinct DB values
    districts  = [r[0] for r in db.query(Candidate.district).filter(
        Candidate.district.isnot(None)).distinct().order_by(Candidate.district).all()]
    categories = [r[0] for r in db.query(Candidate.category).filter(
        Candidate.category.isnot(None)).distinct().order_by(Candidate.category).all()]
    genders    = [r[0] for r in db.query(Candidate.gender).filter(
        Candidate.gender.isnot(None)).distinct().order_by(Candidate.gender).all()]

    return templates.TemplateResponse(
        "jobzon/seekers.html",
        {
            "request": request,
            "seekers": seekers,
            "total": total,
            "page": page_int,
            "total_pages": total_pages,
            "filter_district": district,
            "filter_category": category,
            "filter_gender": gender,
            "filter_age_min": age_min_int if age_min_int > 0 else "",
            "filter_age_max": age_max_int if age_max_int > 0 else "",
            "districts": districts,
            "categories": categories,
            "genders": genders,
        },
    )


@router.get("/seekers/cv/{candidate_id}", response_class=HTMLResponse)
async def jobzon_cv_viewer(
    candidate_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(require_jobzon_admin),
):
    """
    Secure CV viewer for JobZon.
    Resolves the candidate's default CV path server-side; the raw file path is
    never exposed to browser JavaScript — only the /files/cv/* serve URL is
    injected into the Jinja2 template.
    """
    candidate = db.query(Candidate).filter_by(id=candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    # Try to find the default resume first
    resume = (
        db.query(CandidateResume)
        .filter_by(candidate_id=candidate_id, is_default=True)
        .first()
    )
    # Fall back to the most recent resume
    if not resume:
        resume = (
            db.query(CandidateResume)
            .filter_by(candidate_id=candidate_id)
            .order_by(CandidateResume.uploaded_at.desc())
            .first()
        )
    # Fall back to legacy cv_path on the Candidate row
    if not resume and candidate.cv_path:
        cv_serve_url = f"/files/cv/{candidate.cv_path}"
    elif resume:
        cv_serve_url = f"/files/cv/{resume.media_id}"
    else:
        raise HTTPException(status_code=404, detail="No CV found for this candidate")

    return templates.TemplateResponse(
        "jobzon/cv_viewer.html",
        {
            "request": request,
            "candidate": candidate,
            "cv_serve_url": cv_serve_url,
        },
    )


# ─── Recruiter Directory ──────────────────────────────────────────────────────

@router.get("/recruiters", response_class=HTMLResponse)
async def jobzon_recruiters(
    request: Request,
    business_type: str = "",
    has_jobs: str = "",   # "yes" | "no" | ""
    page: int = 1,
    db: Session = Depends(get_db),
    _: str = Depends(require_jobzon_admin),
):
    """Recruiter Directory with vacancy-count and filter support."""
    PAGE_SIZE = 50

    # Base query: recruiter + vacancy count
    base_q = (
        db.query(
            Recruiter,
            sqlfunc.count(JobVacancy.id).label("vacancy_count"),
        )
        .outerjoin(JobVacancy, JobVacancy.recruiter_id == Recruiter.id)
        .group_by(Recruiter.id)
    )

    if business_type:
        base_q = base_q.filter(Recruiter.business_type.ilike(f"%{business_type}%"))
    if has_jobs == "yes":
        base_q = base_q.having(sqlfunc.count(JobVacancy.id) > 0)
    elif has_jobs == "no":
        base_q = base_q.having(sqlfunc.count(JobVacancy.id) == 0)

    total = base_q.count()
    rows  = (
        base_q
        .order_by(sqlfunc.count(JobVacancy.id).desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
        .all()
    )
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    business_types = [
        r[0] for r in
        db.query(Recruiter.business_type).filter(
            Recruiter.business_type.isnot(None)
        ).distinct().order_by(Recruiter.business_type).all()
    ]

    # Flatten to dicts for the template
    recruiters = [
        {
            "id": rec.id,
            "company_name": rec.company_name,
            "business_type": rec.business_type,
            "location": rec.location,
            "wa_number": rec.wa_number,
            "created_at": rec.created_at,
            "vacancy_count": count,
        }
        for rec, count in rows
    ]

    return templates.TemplateResponse(
        "jobzon/recruiters.html",
        {
            "request": request,
            "recruiters": recruiters,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "filter_business_type": business_type,
            "filter_has_jobs": has_jobs,
            "business_types": business_types,
        },
    )


# ─── Create Recruiter (by JobZon — no OTP) ───────────────────────────────────

@router.get("/recruiters/create", response_class=HTMLResponse)
async def jobzon_recruiter_create_form(
    request: Request,
    _: str = Depends(require_jobzon_admin),
):
    """Render the Create Recruiter form."""
    return templates.TemplateResponse(
        "jobzon/recruiter_form.html",
        {"request": request, "error": None, "success": None},
    )


@router.post("/recruiters/create", response_class=HTMLResponse)
async def jobzon_recruiter_create_submit(
    request: Request,
    wa_number: str        = Form(...),
    company_name: str     = Form(...),
    business_type: str    = Form(...),
    registrant_role: str  = Form("other"),
    location: str         = Form(...),
    business_contact: str = Form(...),
    db: Session           = Depends(get_db),
    _: str                = Depends(require_jobzon_admin),
):
    """
    Create a recruiter profile on behalf of a client (no OTP required).
    JobZon is acting as a trusted intermediary.
    """
    wa_number = wa_number.strip()
    existing = db.query(Recruiter).filter_by(wa_number=wa_number).first()
    if existing:
        return templates.TemplateResponse(
            "jobzon/recruiter_form.html",
            {
                "request": request,
                "error": f"A recruiter with WhatsApp number '{wa_number}' already exists in the system.",
                "existing_recruiter": existing,
                "success": None,
                "form_data": {
                    "wa_number": wa_number,
                    "company_name": company_name,
                    "business_type": business_type,
                    "registrant_role": registrant_role,
                    "location": location,
                    "business_contact": business_contact,
                },
            },
            status_code=400,
        )

    recruiter = Recruiter(
        wa_number=wa_number,
        company_name=company_name.strip(),
        business_type=business_type.strip(),
        registrant_role=registrant_role.strip() if registrant_role else "other",
        location=location.strip(),
        business_contact=business_contact.strip(),
    )
    db.add(recruiter)
    db.commit()
    db.refresh(recruiter)
    logger.info("JobZon created recruiter profile: %s (%s)", recruiter.company_name, wa_number)

    return templates.TemplateResponse(
        "jobzon/recruiter_form.html",
        {
            "request": request,
            "error": None,
            "success": f"Recruiter '{recruiter.company_name}' created successfully (ID: {recruiter.id}).",
            "form_data": None,
        },
    )


# ─── Approved Vacancies Directory ─────────────────────────────────────────────

@router.get("/vacancies", response_class=HTMLResponse)
async def jobzon_vacancies(
    request: Request,
    category: str = "",
    district: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
    _: str = Depends(require_jobzon_admin),
):
    """Centralised list of all approved vacancies across the platform."""
    PAGE_SIZE = 50

    query = db.query(JobVacancy).filter_by(status="approved")
    if category:
        query = query.filter(JobVacancy.job_category.ilike(f"%{category}%"))
    if district:
        query = query.filter(JobVacancy.district_region.ilike(f"%{district}%"))

    total = query.count()
    vacancies = (
        query
        .order_by(JobVacancy.approved_at.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
        .all()
    )
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    # Application counts per vacancy (batch query)
    vac_ids = [v.id for v in vacancies]
    app_counts_raw = (
        db.query(CandidateApplication.vacancy_id, sqlfunc.count(CandidateApplication.id))
        .filter(CandidateApplication.vacancy_id.in_(vac_ids))
        .group_by(CandidateApplication.vacancy_id)
        .all()
    )
    app_map = {vid: cnt for vid, cnt in app_counts_raw}

    # Distinct filter option values
    categories = [
        r[0] for r in db.query(JobVacancy.job_category).filter_by(status="approved").filter(
            JobVacancy.job_category.isnot(None)).distinct().order_by(JobVacancy.job_category).all()
    ]
    districts = [
        r[0] for r in db.query(JobVacancy.district_region).filter_by(status="approved").filter(
            JobVacancy.district_region.isnot(None)).distinct().order_by(JobVacancy.district_region).all()
    ]

    return templates.TemplateResponse(
        "jobzon/vacancies.html",
        {
            "request": request,
            "vacancies": vacancies,
            "app_map": app_map,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "filter_category": category,
            "filter_district": district,
            "categories": categories,
            "districts": districts,
        },
    )


# ─── Post Vacancy on Behalf of Recruiter ──────────────────────────────────────

@router.get("/vacancies/create", response_class=HTMLResponse)
async def jobzon_vacancy_create_form(
    request: Request,
    recruiter_id: str = "",
    db: Session = Depends(get_db),
    _: str = Depends(require_jobzon_admin),
):
    """Render the Post Vacancy form with a recruiter selector."""
    recruiters = db.query(Recruiter).order_by(Recruiter.company_name).all()
    selected_recruiter_id = _safe_int(recruiter_id, 0)
    return templates.TemplateResponse(
        "jobzon/vacancy_form.html",
        {
            "request": request,
            "recruiters": recruiters,
            "selected_recruiter_id": selected_recruiter_id,
            "error": None,
            "success": None,
        },
    )


@router.post("/vacancies/create", response_class=HTMLResponse)
async def jobzon_vacancy_create_submit(
    request: Request,
    recruiter_id: int    = Form(...),
    job_category: str    = Form(...),
    district_region: str = Form(...),
    exact_location: str  = Form(...),
    job_title: str       = Form(...),
    job_description: str = Form(...),
    job_mode: str        = Form(...),
    salary_range: str    = Form(...),
    experience_required: str = Form(...),
    cv_required: str     = Form("off"),   # checkbox
    db: Session          = Depends(get_db),
    _: str               = Depends(require_jobzon_admin),
):
    """
    Post a vacancy on behalf of an existing recruiter.
    Enters the normal pending → approval pipeline.
    Super admin will receive a vacancy alert notification.
    """
    recruiter = db.query(Recruiter).filter_by(id=recruiter_id).first()
    all_recruiters = db.query(Recruiter).order_by(Recruiter.company_name).all()

    if not recruiter:
        return templates.TemplateResponse(
            "jobzon/vacancy_form.html",
            {
                "request": request,
                "recruiters": all_recruiters,
                "error": "Selected recruiter not found.",
                "success": None,
            },
            status_code=400,
        )

    job_code = generate_job_code(db)
    vacancy = JobVacancy(
        job_code=job_code,
        recruiter_id=recruiter.id,
        job_category=job_category.strip(),
        district_region=district_region.strip(),
        exact_location=exact_location.strip(),
        job_title=job_title.strip(),
        job_description=job_description.strip(),
        job_mode=job_mode.strip(),
        salary_range=salary_range.strip(),
        experience_required=experience_required.strip(),
        cv_required=(str(cv_required).strip().lower() in ("on", "yes", "true", "1")),
        status="pending",
    )
    db.add(vacancy)
    db.commit()
    db.refresh(vacancy)

    # Notify super admin (non-blocking — we don't await WA calls from a form POST)
    logger.info(
        "JobZon posted vacancy '%s' (code: %s) for recruiter '%s'",
        vacancy.job_title, vacancy.job_code, recruiter.company_name
    )

    return templates.TemplateResponse(
        "jobzon/vacancy_form.html",
        {
            "request": request,
            "recruiters": all_recruiters,
            "error": None,
            "success": (
                f"Vacancy '{vacancy.job_title}' (code: {vacancy.job_code}) submitted for "
                f"approval on behalf of {recruiter.company_name}."
            ),
        },
    )


# ─── Candidate Discovery & Auto-Match Engine ──────────────────────────────────

@router.get("/discover", response_class=HTMLResponse)
async def jobzon_discover(
    request: Request,
    district: str = "",
    category: str = "",
    sub_category: str = "",
    gender: str = "",
    age_min: str = "",
    age_max: str = "",
    page: str = "1",
    db: Session = Depends(get_db),
    _: str = Depends(require_jobzon_admin),
):
    """
    Candidate Discovery dashboard — advanced multi-filter search across all
    registered job seekers. Designed to help JobZon find suitable candidates
    for specific job opportunities.
    """
    PAGE_SIZE = 50

    age_min_int = _safe_int(age_min, 0)
    age_max_int = _safe_int(age_max, 0)
    page_int = max(1, _safe_int(page, 1))

    query = db.query(Candidate).filter_by(registration_complete=True)

    if district:
        query = query.filter(Candidate.district.ilike(f"%{district}%"))
    if category:
        query = query.filter(Candidate.category.ilike(f"%{category}%"))
    if sub_category:
        query = query.filter(Candidate.sub_category.ilike(f"%{sub_category}%"))
    if gender:
        query = query.filter(Candidate.gender.ilike(f"%{gender}%"))
    if age_min_int > 0:
        query = query.filter(Candidate.age >= age_min_int)
    if age_max_int > 0:
        query = query.filter(Candidate.age <= age_max_int)

    total = query.count()
    candidates = (
        query
        .order_by(Candidate.created_at.desc())
        .offset((page_int - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
        .all()
    )
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    # Filter option sets
    districts     = [r[0] for r in db.query(Candidate.district).filter(
        Candidate.district.isnot(None)).distinct().order_by(Candidate.district).all()]
    categories    = [r[0] for r in db.query(Candidate.category).filter(
        Candidate.category.isnot(None)).distinct().order_by(Candidate.category).all()]
    sub_categories = [r[0] for r in db.query(Candidate.sub_category).filter(
        Candidate.sub_category.isnot(None)).distinct().order_by(Candidate.sub_category).all()]
    genders       = [r[0] for r in db.query(Candidate.gender).filter(
        Candidate.gender.isnot(None)).distinct().order_by(Candidate.gender).all()]

    # Load all approved vacancies for the auto-match selector
    vacancies = (
        db.query(JobVacancy)
        .filter_by(status="approved")
        .order_by(JobVacancy.approved_at.desc())
        .limit(100)
        .all()
    )

    return templates.TemplateResponse(
        "jobzon/discover.html",
        {
            "request": request,
            "candidates": candidates,
            "total": total,
            "page": page_int,
            "total_pages": total_pages,
            "filter_district": district,
            "filter_category": category,
            "filter_sub_category": sub_category,
            "filter_gender": gender,
            "filter_age_min": age_min_int if age_min_int > 0 else "",
            "filter_age_max": age_max_int if age_max_int > 0 else "",
            "districts": districts,
            "categories": categories,
            "sub_categories": sub_categories,
            "genders": genders,
            "vacancies": vacancies,
        },
    )


# ─── JSON API: Auto-Match for a Vacancy ───────────────────────────────────────

@router.get("/api/vacancies/{vacancy_id}/auto-match")
async def jobzon_auto_match(
    vacancy_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(require_jobzon_admin),
):
    """
    Auto-Match Engine: Given an approved vacancy, return a ranked list of
    matching candidates based on job_category ↔ candidate.category and
    district_region ↔ candidate.district (case-insensitive LIKE match).

    Ranking:
      1. Both category AND district match (strongest signal)
      2. Category match only
      3. District match only
    """
    vacancy = db.query(JobVacancy).filter_by(id=vacancy_id, status="approved").first()
    if not vacancy:
        raise HTTPException(status_code=404, detail="Approved vacancy not found")

    cat  = vacancy.job_category or ""
    dist = vacancy.district_region or ""

    # Query all registered candidates matching at least one criterion
    from sqlalchemy import or_
    matched = (
        db.query(Candidate)
        .filter_by(registration_complete=True)
        .filter(
            or_(
                Candidate.category.ilike(f"%{cat}%"),
                Candidate.district.ilike(f"%{dist}%"),
            )
        )
        .all()
    )

    def _rank(c: Candidate) -> int:
        cat_ok  = bool(c.category and cat and cat.lower() in (c.category or "").lower())
        dist_ok = bool(c.district and dist and dist.lower() in (c.district or "").lower())
        if cat_ok and dist_ok:
            return 1
        if cat_ok:
            return 2
        return 3

    results = sorted(matched, key=_rank)

    return {
        "vacancy": {
            "id": vacancy.id,
            "job_code": vacancy.job_code,
            "job_title": vacancy.job_title,
            "job_category": vacancy.job_category,
            "district_region": vacancy.district_region,
        },
        "total_matches": len(results),
        "candidates": [
            {
                "id": c.id,
                "name": c.name,
                "district": c.district,
                "category": c.category,
                "sub_category": c.sub_category,
                "age": c.age,
                "gender": c.gender,
                "wa_number": c.wa_number,
                "match_rank": _rank(c),
                "cv_viewer_url": f"/jobzon/seekers/cv/{c.id}",
            }
            for c in results
        ],
    }
