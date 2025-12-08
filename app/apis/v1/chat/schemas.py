# backend/app/apis/v1/chat/schemas.py
from pydantic import BaseModel
from typing import Dict, Optional, Any, List

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    model: str = "gemini-2.5-flash"
    provider: str = "gemini"

class SessionSchema(BaseModel):
    id: str
    topic: str
    created_at: str

class MessageSchema(BaseModel):
    role: str
    content: str
    created_at: str

class ChatResponse(BaseModel):
    response: str
    session_id: str
    usage: Dict[str, int]
    # ✅ NEW: Field to pass structured data (like trade details) to frontend
    tool_call: Optional[Dict[str, Any]] = None