import os
from dotenv import load_dotenv

# Load .env from the folder where this file is running
load_dotenv()

print("🔥 DATABASE_DSN:", repr(os.getenv("DATABASE_DSN")))
print("🔥 SUPABASE_URL:", repr(os.getenv("SUPABASE_URL")))
print("🔥 SERVICE_KEY:", repr(os.getenv("SUPABASE_SERVICE_ROLE_KEY")))
