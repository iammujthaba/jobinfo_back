"""
Seed the subscription_plans table with the 4 plan types.
Run once: python -m app.db.seed
"""
from app.db.base import SessionLocal, init_db
from app.db.models import SubscriptionPlan, SubscriptionPlanName


PLANS = [
    SubscriptionPlan(
        name=SubscriptionPlanName.free_trial,
        display_name="Free Trial",
        price_inr=0,
        duration_days=15,
        max_applications=3,
        same_day_vacancy=False,
        max_cv_updates=0,
        interview_scheduling=False,
        vacancy_forward=False,
        location_filter=False,
        job_filter=False,
    ),
    SubscriptionPlan(
        name=SubscriptionPlanName.basic,
        display_name="Basic",
        price_inr=99,
        duration_days=30,
        max_applications=50,
        same_day_vacancy=False,
        max_cv_updates=10,
        interview_scheduling=False,
        vacancy_forward=False,
        location_filter=False,
        job_filter=False,
    ),
    SubscriptionPlan(
        name=SubscriptionPlanName.popular,
        display_name="Popular",
        price_inr=299,
        duration_days=60,
        max_applications=100,
        same_day_vacancy=True,
        max_cv_updates=None,  # unlimited
        interview_scheduling=False,
        vacancy_forward=True,
        location_filter=True,
        job_filter=True,
    ),
    SubscriptionPlan(
        name=SubscriptionPlanName.advanced,
        display_name="Advanced",
        price_inr=499,
        duration_days=60,
        max_applications=None,  # unlimited
        same_day_vacancy=True,
        max_cv_updates=None,
        interview_scheduling=True,
        vacancy_forward=True,
        location_filter=True,
        job_filter=True,
    ),
]


def seed():
    init_db()
    db = SessionLocal()
    try:
        existing = db.query(SubscriptionPlan).count()
        if existing == 0:
            db.add_all(PLANS)
            db.commit()
            print("✅ Seeded 4 subscription plans.")
        else:
            print("⚠️  Plans already seeded – skipping.")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
