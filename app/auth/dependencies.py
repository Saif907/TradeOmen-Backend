from fastapi import Header, HTTPException, Depends, status
from typing import Optional, Annotated, Callable, Any
from supabase import create_client, Client
from ..libs.config import settings
from ..libs import schemas

# --- 1. DEPENDENCY: Authenticated Supabase Client for RLS ---
async def get_user_supabase_client(
    authorization: Optional[str] = Header(None)
) -> Client:
    """
    Creates a temporary Supabase client authenticated with the USER'S token.
    This is necessary for Row Level Security (RLS) policies (auth.uid()) to work.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token.",
        )
    
    token = authorization.split(" ")[1]
    
    # Initialize a new client instance
    client = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)
    
    # CRITICAL: Set the Auth header for Postgrest so RLS sees the user ID
    client.postgrest.auth(token)
    
    return client

# --- 2. DEPENDENCY: Supabase Service Role Client (for admin/upsert tasks) ---
def get_supabase_service_client() -> Client:
    """
    Returns the privileged Supabase Service Role client instance.
    Used for profile creation/management where RLS must be bypassed (e.g., in /auth/login).
    """
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)

# Dependency for Supabase Client authenticated as the user
AuthSupabaseClient = Depends(get_user_supabase_client)

# Dependency for Supabase Service Client (admin)
ServiceSupabaseClient = Depends(get_supabase_service_client)


# --- 3. DEPENDENCY: User Authentication & Plan Extraction ---
async def get_current_user(
    supabase_client: Annotated[Client, AuthSupabaseClient],
    authorization: Optional[str] = Header(None)
) -> schemas.UserInDB:
    """
    Validates the user's JWT from the 'Authorization' header, extracts the user ID,
    and returns the UserInDB schema object with their plan.
    """
    if not authorization or not authorization.startswith("Bearer "):
         raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated: Missing or invalid Bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    token = authorization.split(" ")[1]
    
    try:
        # Use the AUTH client to validate the token locally
        user_response = supabase_client.auth.get_user(token)
        user_id = user_response.user.id
        
        # MOCK USER DATA FOR DEVELOPMENT (In production, fetch 'plan' from DB)
        current_plan = schemas.Plan.PRO 
        
        return schemas.UserInDB(
            user_id=str(user_id),
            email=user_response.user.email,
            plan=current_plan,
            is_admin=False
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

# Dependency to get the authenticated user object
AuthUser = Depends(get_current_user)

# --- 4. DEPENDENCY: SaaS Tier Authorization (Feature Gating) ---

def check_plan_access(feature: schemas.PlanFeature) -> Callable[[schemas.UserInDB], schemas.UserInDB]:
    """
    Dependency factory to check if a user's current plan permits access to a feature 
    or enforces usage limits (e.g., number of trades, AI prompts).
    Raises HTTPException if access is denied.
    """
    def check_plan_logic(
        current_user: schemas.UserInDB = Depends(get_current_user),
        # NOTE: If checking a numerical limit (like MAX_TRADES_MONTH), 
        # the ServiceSupabaseClient would need to be injected here to query usage counts.
    ) -> schemas.UserInDB:
        user_plan = current_user.plan
        
        # 1. Check Feature Availability (Boolean Gates)
        if feature in [schemas.PlanFeature.AUTO_TAGGING, schemas.PlanFeature.ADVANCED_ANALYTICS, schemas.PlanFeature.EXPORT_CSV]:
            is_available = getattr(schemas.PLAN_GATES, feature.value).get(user_plan, True)
            if not is_available:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied: '{feature.value}' requires a paid plan.",
                )

        # 2. Check Usage Limits (Numerical Gates)
        if feature in [schemas.PlanFeature.AI_CHAT_PROMPT_LIMIT, schemas.PlanFeature.MAX_TRADES_MONTH]:
            # This is where the database query for current usage vs. limit would occur.
            pass

        return current_user
        
    return check_plan_logic


# --- 5. DEPENDENCY: Microservice Authentication ---

def verify_ai_microservice_auth(
    auth_key: Annotated[str, Header(alias="X-Microservice-Auth")]
) -> bool:
    """
    Verifies that the incoming request is from a trusted internal microservice 
    using the shared secret key.
    """
    if auth_key != settings.AI_SERVICE_SECRET_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid microservice authentication key.",
        )
    return True

# Simplified dependency for internal use
InternalAuth = Depends(verify_ai_microservice_auth)