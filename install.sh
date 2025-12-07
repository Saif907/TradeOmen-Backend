# backend/install.sh
#!/bin/bash

echo "--- TradeLM AI Backend Setup ---"

# 1. Create Python Virtual Environment
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
else
    echo "Virtual environment 'venv' already exists."
fi

# 2. Activate Virtual Environment
source venv/bin/activate

# 3. Install Dependencies from requirements.txt
echo "Installing dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# 4. Check for .env file
if [ ! -f ".env" ]; then
    echo "WARNING: .env file not found."
    echo "Copying .env.example to .env. Please edit .env with your secrets."
    # Create an example/template .env file content directly here (using content from previous step)
    cat << EOF > .env.example
# --- GENERAL APP SETTINGS ---
APP_NAME="TradeLM AI Backend"
APP_VERSION="1.0.0"
ENVIRONMENT="development"
SERVER_HOST="0.0.0.0"
SERVER_PORT=8080
CORS_ALLOWED_ORIGINS="*" 

# --- SUPABASE/DATABASE CONFIGURATION ---
SUPABASE_URL="https://pppdiotmrmeuzhdcpjzt.supabase.co" 
SUPABASE_SERVICE_ROLE_KEY="your_supabase_service_role_key_here"
DATABASE_DSN="your_asyncpg_database_dsn_here" 

# --- SECURITY CONFIGURATION ---
SECRET_KEY="a_very_long_and_random_secret_key_for_jwt_signing"
ENCRYPTION_KEY="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" 
ACCESS_TOKEN_EXPIRE_MINUTES=30

# --- LLM API KEYS (MANDATORY) ---
OPENAI_API_KEY="sk-your-openai-key"
PERPLEXITY_API_KEY="pplx-your-perplexity-key"
GEMINI_API_KEY="AIzaSy...your-gemini-key"

# --- WORKER CONFIGURATION ---
MAX_WORKER_THREADS=8
EOF
    cp .env.example .env
fi

echo "Setup complete. Run 'source venv/bin/activate' and then './run.sh' to start the server."