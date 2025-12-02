#!/bin/bash
# install.sh
# Automated setup script for the TradeLM FastAPI backend service.

echo "--- Starting TradeLM Backend Installation ---"

# --- 1. Environment Check and Setup ---
# Check if Python is available
if ! command -v python3 &> /dev/null
then
    echo "Error: Python 3 is not installed. Please install Python 3.8+."
    exit 1
fi

# Create a virtual environment if it doesn't exist (industry standard for isolation)
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

# Activate the virtual environment
source .venv/bin/activate
echo "Virtual environment activated."

# --- 2. Install Dependencies ---
echo "Installing Python dependencies from requirements.txt..."
pip install --upgrade pip
pip install -r requirements.txt

if [ $? -ne 0 ]; then
    echo "Error: Failed to install Python dependencies. Check requirements.txt."
    exit 1
fi
echo "All dependencies installed successfully."

# --- 3. Configuration Check (CRITICAL FOR SECURITY) ---
echo "Checking for critical environment variables in .env..."

ENV_FILE=".env"
if [ ! -f "$ENV_FILE" ]; then
    echo "Warning: .env file not found. Creating a placeholder."
    echo "# Please fill in these values before running the service!" > "$ENV_FILE"
    echo "ENVIRONMENT=development" >> "$ENV_FILE"
    echo "SUPABASE_URL=http://your-supabase-url" >> "$ENV_FILE"
    echo "SUPABASE_SERVICE_KEY=your-supabase-service-key" >> "$ENV_FILE"
    echo "ENCRYPTION_KEY=a-secure-32-byte-base64-key-here" >> "$ENV_FILE"
    echo "REDIS_BROKER_URL=redis://localhost:6379/0" >> "$ENV_FILE"
    echo "AI_SERVICE_API_KEY=your-internal-ai-secret" >> "$ENV_FILE"
fi

# Validate critical secrets (non-breakable check)
if grep -q "your-supabase-service-key" "$ENV_FILE"; then
    echo "FATAL WARNING: SUPABASE_SERVICE_KEY placeholder found in .env."
    echo "The service will run but RLS will be compromised if not updated."
fi

if grep -q "a-secure-32-byte-base64-key-here" "$ENV_FILE"; then
    echo "FATAL WARNING: ENCRYPTION_KEY placeholder found in .env."
    echo "Trade notes and chat data will NOT be encrypted until this is fixed."
fi

# --- 4. Final Instructions ---
echo "--- Installation Complete ---"
echo "To run the server, use: source .venv/bin/activate && ./run.sh"
echo "Remember to customize your .env file with actual secrets!"