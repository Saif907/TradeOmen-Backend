# backend/app/apis/billing.py

from fastapi import APIRouter, Depends, HTTPException, status, Request
from loguru import logger
from supabase import Client
from typing import Dict, Any, Optional
from pydantic import BaseModel

from app.auth.dependency import AuthenticatedUser, DBClient, UserProfile
from app.libs.config import settings

router = APIRouter()

# --- Data Models for Billing ---

class CheckoutSessionRequest(BaseModel):
    """Request to start a subscription checkout process."""
    plan_id: str # "BASIC" or "PRO"
    cycle: str = "MONTHLY" # "MONTHLY" or "YEARLY"

class CheckoutSessionResponse(BaseModel):
    """Returns the URL to redirect the user to (Stripe/Razorpay)."""
    url: str

class PortalSessionResponse(BaseModel):
    """Returns the URL for the self-serve billing portal."""
    url: str

# --- Configuration & Constants ---

PRICES = {
    "IN": {
        "BASIC_MONTHLY": "price_in_basic_m",
        "BASIC_YEARLY": "price_in_basic_y",
        "PRO_MONTHLY": "price_in_pro_m",
        "PRO_YEARLY": "price_in_pro_y"
    },
    "US": {
        "BASIC_MONTHLY": "price_us_basic_m",
        "BASIC_YEARLY": "price_us_basic_y",
        "PRO_MONTHLY": "price_us_pro_m",
        "PRO_YEARLY": "price_us_pro_y"
    }
}

# --- Endpoints ---

@router.post("/checkout", response_model=CheckoutSessionResponse, summary="Create a checkout session for upgrade")
async def create_checkout_session(
    request: CheckoutSessionRequest,
    user: AuthenticatedUser,
    profile: UserProfile,
    db: DBClient # Correct usage: No default value needed with Annotated
):
    """
    Generates a payment gateway checkout URL based on the user's region and selected plan.
    """
    region = profile.get('region_code', 'US')
    if region not in PRICES:
        region = 'US'
    
    plan_key = f"{request.plan_id.upper()}_{request.cycle.upper()}"
    price_id = PRICES[region].get(plan_key)
    
    if not price_id:
        logger.warning(f"BILLING_ERROR: Invalid plan/cycle requested by user {user.user_id}: {plan_key}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid plan or billing cycle selected.")

    logger.info(f"BILLING_INIT: Creating {region} checkout session for User {user.user_id} -> {plan_key}")
    
    mock_url = f"https://checkout.stripe.com/pay/{price_id}?client_reference_id={user.user_id}"
    
    return CheckoutSessionResponse(url=mock_url)


@router.post("/portal", response_model=PortalSessionResponse, summary="Manage existing subscription")
async def create_portal_session(
    user: AuthenticatedUser,
    db: DBClient
):
    """
    Generates a URL for the customer portal where they can update payment methods or cancel.
    """
    logger.info(f"BILLING_PORTAL: Generating portal session for User {user.user_id}")
    return PortalSessionResponse(url="https://billing.stripe.com/p/session/test_portal_123")


@router.post("/webhook", include_in_schema=False)
async def billing_webhook(
    request: Request,
    # In webhooks, we don't have a user token, so DBClient (which depends on get_supabase_client)
    # will fall back to the Service Client if no token is found, or fail if strict auth is enforced.
    # We use a specific dependency here to ensure we get a service client for webhooks.
    # For now, assuming DBClient handles unauthenticated access via Service Key fallback as implemented in supabase_client.py
    db: DBClient 
):
    """
    Handles asynchronous events from payment gateways.
    """
    payload_bytes = await request.body()
    sig_header = request.headers.get("stripe-signature")
    
    if not sig_header and settings.ENVIRONMENT == "production":
        logger.warning("WEBHOOK_SECURITY: Missing signature in production webhook.")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid signature")

    try:
        event = await request.json()
        event_type = event.get("type")
        data_object = event.get("data", {}).get("object", {})
        
        logger.info(f"WEBHOOK_RECEIVED: {event_type}")

        if event_type == "checkout.session.completed":
            user_id = data_object.get("client_reference_id")
            customer_id = data_object.get("customer")
            
            if user_id:
                # Using internal helper to ensure service-level update
                await _update_user_plan_status(user_id, "PRO", customer_id)
                
        elif event_type == "customer.subscription.deleted":
            pass
            
    except Exception as e:
        logger.error(f"WEBHOOK_FAIL: Error processing event: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Webhook processing failed")

    return {"status": "success"}

# --- Helper for Webhook (Internal) ---

async def _update_user_plan_status(user_id: str, plan_id: str, customer_id: str):
    """Updates the user's profile in Supabase using the Service Role."""
    from app.libs.supabase_client import get_supabase_service_client
    
    try:
        admin_db = get_supabase_service_client()
        admin_db.table('user_profiles').update({
            "active_plan_id": plan_id,
            "gateway_customer_id": customer_id
        }).eq('id', user_id).execute()
        logger.success(f"WEBHOOK_UPDATE: Upgraded User {user_id} to {plan_id}.")
    except Exception as e:
        logger.error(f"WEBHOOK_DB_ERROR: Failed to update user {user_id}: {e}")
        raise