# backend/app/core/limiter.py

from slowapi import Limiter
from slowapi.util import get_remote_address
from app.core.config import settings

# Initialize Limiter
# key_func: Uses IP address. Safe, fast, and handles unauthenticated DDOS too.
# storage_uri: Defaults to memory://, but switches to Redis if you change .env
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=settings.RATE_LIMIT_STORAGE_URL,
    strategy="fixed-window", # "fixed-window" is the fastest strategy for in-memory
    enabled=True
)