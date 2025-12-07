# backend/run.sh
#!/bin/bash

# Exit immediately if a command exits with a non-zero status (robustness)
set -e

# --- Configuration Loading and Check ---
# Source the .env file to load environment variables for Uvicorn
if [ -f ".env" ]; then
    export $(grep -v '^#' .env | xargs)
else
    echo "Error: .env file not found. Please create it or run ./install.sh"
    exit 1
fi

# 1. Activate the virtual environment
if [ -d "venv" ]; then
    source venv/bin/activate
else
    echo "Error: Virtual environment 'venv' not found. Please run ./install.sh first."
    exit 1
fi

echo "--- Starting TradeLM AI Backend (v${APP_VERSION:-1.0.0}) ---"
echo "Environment: ${ENVIRONMENT:-development}"
echo "Listening on http://${SERVER_HOST:-0.0.0.0}:${SERVER_PORT:-8080}"

# 2. Start Uvicorn server based on the environment (Efficiency & Standards)
if [ "$ENVIRONMENT" = "production" ]; then
    # Use --workers > 1 for multi-core performance in production
    # --factory is used to ensure clean app instantiation/shutdown across workers
    uvicorn main:app --factory --host 0.0.0.0 --port ${SERVER_PORT:-8080} --workers ${MAX_WORKER_THREADS:-4}
else
    # Use --reload for development for convenience
    uvicorn main:app --factory --reload --host ${SERVER_HOST:-0.0.0.0} --port ${SERVER_PORT:-8080}
fi