import httpx
from supabase import create_client, Client
from ..libs.config import settings

# Initialize the Supabase Client
# We use the Service Key for internal server-side operations where RLS might need to be bypassed 
# (e.g., when the AI microservice requests anonymized data, or for administrative tasks).
# For user-facing requests, we primarily rely on the user's JWT and RLS in the dependency layer.

try:
    # Initialize the global Supabase client instance
    # Note: supabase-py uses httpx internally, which is now isolated from the LLM service.
    supabase: Client = create_client(
        supabase_url=settings.SUPABASE_URL,
        supabase_key=settings.SUPABASE_KEY  # Using the public key for client initialization
    )
    
    # Initialize a client that uses the highly privileged Service Role Key 
    # to perform server-side operations (like inserting a user's initial plan)
    # or querying the database without RLS restrictions (ONLY used internally and securely).
    supabase_service_role: Client = create_client(
        supabase_url=settings.SUPABASE_URL,
        supabase_key=settings.SUPABASE_SERVICE_KEY
    )
    
except httpx.InvalidURL:
    # Handle case where SUPABASE_URL is malformed in .env
    print("FATAL ERROR: Invalid Supabase URL. Check backend/.env")
    exit(1)


# Dependency to be used in FastAPI endpoints for accessing the Supabase client
def get_supabase_client() -> Client:
    """Returns the main public Supabase client instance."""
    return supabase

def get_supabase_service_client() -> Client:
    """Returns the privileged Supabase Service Role client instance."""
    return supabase_service_role