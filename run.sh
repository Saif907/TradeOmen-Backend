#!/bin/bash
# run.sh
# Executes the FastAPI application using Uvicorn with performance settings.

# --- 1. Environment and Performance Configuration ---

# Ensure the virtual environment is sourced
if [ -z "$VIRTUAL_ENV" ]; then
    echo "Activating virtual environment..."
    source .venv/bin/activate
fi

# Determine the number of CPU cores for Uvicorn workers
# Using a common heuristic: (2 * number of cores) + 1, or just 4-8 workers for cloud deployments
WORKERS=$(nproc --all)
if [ "$WORKERS" -gt 8 ]; then
    WORKERS=8 # Cap the number of workers to prevent memory exhaustion on typical cloud instances
fi

# Set host and port based on standard configuration
HOST="0.0.0.0"
PORT=8000

# --- 2. Execution ---
echo "--- Starting TradeLM Backend Server ---"
echo "Host: $HOST:$PORT | Workers: $WORKERS | Environment: $(cat .env | grep ENVIRONMENT | cut -d '=' -f2)"
echo "Using high-performance Uvicorn configuration (uvloop/httptools)."

# Execute Uvicorn in production mode (or standard mode if not found)
# --log-level info: Standard logging level for production
# --factory: Tells uvicorn to look for a creatable app instance (if needed in future refactoring)
# --workers: Enables process parallelism for multi-core performance
exec uvicorn main:app \
    --host $HOST \
    --port $PORT \
    --log-level info \
    --workers $WORKERS \
    --factory 

# --- 3. Error Handling ---
# If the exec command fails (e.g., Uvicorn not installed or fatal import error)
if [ $? -ne 0 ]; then
    echo "Error: Uvicorn server failed to start. Check dependencies and logs."
    exit 1
fi