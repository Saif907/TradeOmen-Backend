#!/bin/bash

# --- TradeLM Main Backend Execution Script ---

# 1. Activate the Python Virtual Environment
if [ -d "venv" ]; then
    source venv/bin/activate
else
    echo "Error: Virtual environment 'venv' not found. Please run ./install.sh first."
    exit 1
fi

# 2. Set environment variables (for pydantic-settings to load)
if [ -f .env ]; then
    echo "Loading environment variables from .env"
    export $(grep -v '^#' .env | xargs)
fi

# 3. Start the Uvicorn server, targeting main.py at the root of the backend folder
# The app is located at main:app
echo "Starting FastAPI server (http://0.0.0.0:8000)..."
uvicorn main:app --app-dir app --host 0.0.0.0 --port 8000 --reload

# Deactivate the environment when done
deactivate