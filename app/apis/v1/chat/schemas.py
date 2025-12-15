from typing import List, Optional, Dict, Any, Union
from pydantic import BaseModel, Field
from datetime import datetime

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    model: str = "gemini-2.5-flash"
    provider: Optional[str] = None  # Optional override

class ChatUsage(BaseModel):
    total_tokens: Optional[int] = 0
    prompt_tokens: Optional[int] = 0
    completion_tokens: Optional[int] = 0
    
    # ✅ Fixed Validation Errors for Perplexity
    # Perplexity returns 'search_context_size' as int OR string (e.g. "low")
    search_context_size: Optional[Union[int, str]] = None
    
    # Perplexity returns 'cost' as a float OR a detailed dictionary
    cost: Optional[Union[float, Dict[str, float]]] = None 

    # Allow extra fields for future-proofing against different provider schemas
    class Config:
        extra = "allow"

class ToolCallData(BaseModel):
    type: str
    data: Dict[str, Any]

class ChatResponse(BaseModel):
    response: str
    session_id: str
    usage: Optional[ChatUsage] = None
    tool_call: Optional[ToolCallData] = None

class SessionSchema(BaseModel):
    id: str
    topic: str
    created_at: datetime

class MessageSchema(BaseModel):
    role: str
    content: str
    created_at: datetime