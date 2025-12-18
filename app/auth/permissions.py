# app/auth/permissions.py
from fastapi import HTTPException, Depends
from app.auth.dependency import get_current_user

def check_ai_quota(current_user: dict = Depends(get_current_user)):
    """
    Blocks request if user exceeds daily AI limit.
    """
    plan = current_user.get("plan_tier", "FREE")
    usage = current_user.get("ai_chat_quota_used", 0) # This should be a daily/monthly counter
    
    limits = {
        "FREE": 5,
        "PRO": 100
    }
    
    if usage >= limits.get(plan, 5):
        raise HTTPException(
            status_code=402, # Payment Required
            detail="Daily AI quota exceeded. Please upgrade to Pro."
        )
    return True

def check_broker_sync_access(current_user: dict = Depends(get_current_user)):
    """
    Blocks request if user tries to sync but is on Free plan.
    """
    if current_user.get("plan_tier") == "FREE":
        raise HTTPException(
            status_code=403, 
            detail="Broker Auto-Sync is a Pro feature."
        )
    return True