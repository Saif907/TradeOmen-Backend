# backend/app/services/plan_services.py

import time
import logging
from typing import Dict, Any
from supabase import Client

logger = logging.getLogger(__name__)

# Simple in-memory cache
# Format: { "user_uuid": { "plan": "PRO", "expires_at": 1700000000 } }
_PLAN_CACHE: Dict[str, Dict[str, Any]] = {}

# How long to trust the cache (in seconds)
# 60s is the sweet spot: Fast UI, but updates quickly after payment
CACHE_TTL = 60 

class PlanService:
    @staticmethod
    def get_user_plan(user_id: str, supabase: Client) -> str:
        now = time.time()
        
        # 1. Check Cache
        cached_data = _PLAN_CACHE.get(user_id)
        if cached_data and cached_data["expires_at"] > now:
            return cached_data["plan"]

        # 2. Cache Miss - Fetch from DB (The "Slow" Call)
        try:
            # We use .single() for speed
            res = supabase.table("user_profiles").select("plan_tier").eq("id", user_id).single().execute()
            
            real_plan = "FREE"
            if res.data and res.data.get("plan_tier"):
                real_plan = res.data.get("plan_tier").upper()
            
            # 3. Save to Cache
            _PLAN_CACHE[user_id] = {
                "plan": real_plan,
                "expires_at": now + CACHE_TTL
            }
            
            # Debug log to verify it's working
            logger.info(f"Refreshed plan cache for {user_id}: {real_plan}")
            
            return real_plan

        except Exception as e:
            logger.error(f"Failed to fetch plan for {user_id}: {e}")
            # On error, fallback to FREE to be safe, but don't crash
            return "FREE"

    @staticmethod
    def clear_cache(user_id: str):
        """Call this immediately after a successful payment webhook"""
        if user_id in _PLAN_CACHE:
            del _PLAN_CACHE[user_id]