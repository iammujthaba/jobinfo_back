"""
WhatsApp Flows data exchange endpoint with full AES/RSA encryption.

Meta sends encrypted payloads to this endpoint. We decrypt, process the
screen transition, and respond with an encrypted payload.
"""
import json
import logging

from fastapi import APIRouter, Request, Response

from app.config import get_settings
from app.whatsapp.flow_crypto import (
    load_private_key,
    decrypt_request,
    encrypt_response,
)

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/flows", tags=["flows"])

# ── Lazy-load RSA key ─────────────────────────────────────────────────────────
_private_key = None

def _get_private_key():
    global _private_key
    if _private_key is None:
        _private_key = load_private_key(
            settings.flow_private_key_path,
            passphrase=settings.flow_private_key_passphrase or None,
        )
        logger.info("Flow RSA private key loaded.")
    return _private_key


# ═══════════════════════════════════════════════════════════════════════════════
# Main endpoint
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/callback")
async def flow_data_exchange(request: Request):
    """
    Encrypted WhatsApp Flows data-exchange endpoint.
    Handles ping, INIT, and data_exchange actions.
    """
    body = await request.json()
    private_key = _get_private_key()

    # ── Decrypt ───────────────────────────────────────────────────────────
    try:
        decrypted, aes_key, iv = decrypt_request(body, private_key)
    except Exception as e:
        logger.error("Flow decryption failed: %s", e)
        return Response(status_code=421)

    action = decrypted.get("action")
    screen = decrypted.get("screen")
    data = decrypted.get("data", {})
    version = decrypted.get("version")

    logger.info("Flow: action=%s screen=%s version=%s", action, screen, version)

    # ── Health-check ping ─────────────────────────────────────────────────
    if action == "ping":
        resp = {"version": version, "data": {"status": "active"}}
        return Response(content=encrypt_response(resp, aes_key, iv), media_type="text/plain")

    # ── INIT: first screen load ───────────────────────────────────────────
    if action == "INIT":
        resp = _handle_init(screen, data)
        return Response(content=encrypt_response(resp, aes_key, iv), media_type="text/plain")

    # ── data_exchange: Screen 1 → Screen 2 ────────────────────────────────
    if action == "data_exchange":
        resp = await _handle_data_exchange(screen, data)
        return Response(content=encrypt_response(resp, aes_key, iv), media_type="text/plain")

    # ── Fallback ──────────────────────────────────────────────────────────
    logger.warning("Unknown flow action: %s", action)
    resp = {"version": version or "7.3", "data": {"status": "active"}}
    return Response(content=encrypt_response(resp, aes_key, iv), media_type="text/plain")


# ═══════════════════════════════════════════════════════════════════════════════
# Screen handlers
# ═══════════════════════════════════════════════════════════════════════════════

def _handle_init(screen: str, data: dict) -> dict:
    """Return initial data for the first screen."""
    if screen == "SEEKER_REGISTRATION":
        return {
            "screen": "SEEKER_REGISTRATION",
            "data": {
                "pending_job_code": data.get("pending_job_code", ""),
            },
        }
    return {"screen": screen, "data": {}}


# ── Category → Sub-category mapping ──────────────────────────────────────────
CATEGORY_SUBCATEGORIES: dict[str, list[dict[str, str]]] = {
    "retail": [
        {"id": "sales_executive", "title": "Sales Executive"},
        {"id": "cashier", "title": "Cashier / Billing"},
        {"id": "store_keeper", "title": "Store Keeper"},
        {"id": "floor_manager", "title": "Floor Manager"},
        {"id": "customer_support", "title": "Customer Support"},
        {"id": "packing_staff", "title": "Packing Staff"},
        {"id": "other", "title": "Other / General"},
    ],
    "hospitality": [
        {"id": "chef_cook", "title": "Chef / Cook (Master)"},
        {"id": "waiter_server", "title": "Waiter / Server"},
        {"id": "kitchen_helper", "title": "Kitchen Helper / Cleaner"},
        {"id": "restaurant_manager", "title": "Restaurant Manager"},
        {"id": "juice_tea_maker", "title": "Juice / Tea Maker"},
        {"id": "housekeeping_hotel", "title": "Housekeeping (Hotel)"},
        {"id": "other", "title": "Other / General"},
    ],
    "healthcare": [
        {"id": "home_nurse", "title": "Home Nurse / Caretaker"},
        {"id": "clinic_receptionist", "title": "Clinic Receptionist"},
        {"id": "pharmacy_staff", "title": "Pharmacy Staff"},
        {"id": "lab_technician", "title": "Lab Technician"},
        {"id": "ward_boy", "title": "Ward Boy / Helper"},
        {"id": "physiotherapist", "title": "Physiotherapist"},
        {"id": "other", "title": "Other / General"},
    ],
    "driving": [
        {"id": "two_wheeler_delivery", "title": "Two-Wheeler Delivery"},
        {"id": "heavy_vehicle_driver", "title": "Heavy Vehicle Driver"},
        {"id": "private_car_taxi", "title": "Private Car / Taxi Driver"},
        {"id": "auto_goods_driver", "title": "Auto Rickshaw / Goods Driver"},
        {"id": "forklift_operator", "title": "Forklift Operator"},
        {"id": "logistics_coordinator", "title": "Logistics Coordinator"},
        {"id": "other", "title": "Other / General"},
    ],
    "office_admin": [
        {"id": "receptionist", "title": "Receptionist / Front Desk"},
        {"id": "data_entry", "title": "Data Entry Operator"},
        {"id": "accountant_tally", "title": "Basic Accountant (Tally)"},
        {"id": "office_peon", "title": "Office Peon / Helper"},
        {"id": "telecaller_bpo", "title": "Telecaller / BPO"},
        {"id": "hr_admin", "title": "HR / Admin"},
        {"id": "other", "title": "Other / General"},
    ],
    "maintenance_technician": [
        {"id": "electrician", "title": "Electrician"},
        {"id": "ac_mechanic", "title": "AC Mechanic"},
        {"id": "plumber", "title": "Plumber"},
        {"id": "automobile_mechanic", "title": "Automobile Mechanic"},
        {"id": "welder_fitter", "title": "Welder / Fitter"},
        {"id": "lift_cctv_technician", "title": "Lift / CCTV Technician"},
        {"id": "other", "title": "Other / General"},
    ],
    "it_professional": [
        {"id": "software_developer", "title": "Software Developer"},
        {"id": "graphic_designer", "title": "Graphic Designer"},
        {"id": "digital_marketer", "title": "Digital Marketer"},
        {"id": "it_hardware_support", "title": "IT Hardware / Support"},
        {"id": "video_editor", "title": "Video Editor"},
        {"id": "content_writer", "title": "Content Writer"},
        {"id": "other", "title": "Other / General"},
    ],
    "gulf_abroad": [
        {"id": "construction_worker", "title": "Construction Worker"},
        {"id": "driver_gcc", "title": "Driver (GCC License)"},
        {"id": "nurse_medical", "title": "Nurse / Medical"},
        {"id": "retail_sales_gcc", "title": "Retail / Sales (GCC)"},
        {"id": "camp_boss", "title": "Camp Boss / Supervisor"},
        {"id": "it_professional_gcc", "title": "IT / Professional"},
        {"id": "office_admin_gcc", "title": "Office Admin (GCC)"},
        {"id": "chef_cook_gcc", "title": "Chef / Cook (Master)"},
        {"id": "waiter_server_gcc", "title": "Waiter / Server"},
        {"id": "kitchen_helper_gcc", "title": "Kitchen Helper / Cleaner"},
        {"id": "other", "title": "Other / General"},
    ],
    "other": [
        {"id": "beautician_salon", "title": "Beautician / Salon Staff"},
        {"id": "tailor_garment", "title": "Tailor / Garment Worker"},
        {"id": "petrol_pump", "title": "Petrol Pump Attendant"},
        {"id": "general_labor", "title": "General Labor / Helper"},
        {"id": "security_guard", "title": "Security Guard / Supervisor"},
        {"id": "housekeeping_cleaning", "title": "Housekeeping / Cleaning"},
        {"id": "factory_warehouse", "title": "Factory / Warehouse Worker"},
        {"id": "painter_carpenter", "title": "Painter / Carpenter"},
        {"id": "event_management", "title": "Event Management Staff"},
        {"id": "any_other", "title": "Any Other Role"},
    ],
}


async def _handle_data_exchange(screen: str, data: dict) -> dict:
    """
    Handle Screen 1 → Screen 2 transition.
    Receives district, exact_location, and category from SEEKER_REGISTRATION.
    Returns sub_categories (from mapping) for the chosen category.
    """
    if screen == "SEEKER_REGISTRATION":
        category = str(data.get("category", "")).strip()

        # ── Resolve sub-categories from the category ──────────────────────
        sub_categories = CATEGORY_SUBCATEGORIES.get(
            category,
            [{"id": "other", "title": "Other / General"}],
        )

        return {
            "screen": "SEEKER_LOCATION",
            "data": {
                "sub_categories": sub_categories,
            },
        }

    return {"screen": screen, "data": {}}