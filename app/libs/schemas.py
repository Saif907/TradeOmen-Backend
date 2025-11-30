from pydantic import BaseModel, Field, condecimal, constr
from datetime import datetime
from typing import List, Optional, Dict, Any
from enum import Enum

# =====================================================================
# 1. ENUMS (Used to define fixed types)
# =====================================================================

class Plan(str, Enum):
    """Defines the subscription tiers of the SaaS application."""
    FREE = "free"
    PRO = "pro"
    PRO_PLUS = "pro_plus"

class TradeDirection(str, Enum):
    LONG = "Long"
    SHORT = "Short"

class TradeInstrument(str, Enum):
    STOCK = "Stock"
    OPTIONS = "Options"
    FUTURES = "Futures"
    FOREX = "Forex"
    CRYPTO = "Crypto"

class MessageRole(str, Enum):
    """Roles in a chat conversation (like ChatGPT roles)."""
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"

# =====================================================================
# 2. CORE MODELS (Internal representations and Base classes)
# =====================================================================

class UserBase(BaseModel):
    """Base model for a user, containing essential data."""
    user_id: str = Field(..., description="Supabase Auth UUID")
    email: str
    plan: Plan = Plan.FREE
    
class UserInDB(UserBase):
    """Internal model for the authenticated user, used for dependencies."""
    is_admin: bool = False

# =====================================================================
# 3. TRADE MODELS (Partial for brevity, using ellipsis for omitted fields)
# =====================================================================
# ... (TradeCreate, TradeInDB, TradeResponse, StrategyBase, StrategyResponse models remain the same) ...

# -----------------
# TRADE MODELS
# -----------------
class TradeCreate(BaseModel):
    """Schema for creating a new Trade entry (Input from frontend)."""
    symbol: constr(min_length=1, max_length=10)
    direction: TradeDirection
    instrument: TradeInstrument
    account: Optional[str] = None
    strategy_id: Optional[str] = None
    entry_datetime: datetime
    exit_datetime: Optional[datetime] = None
    entry_price: condecimal(max_digits=10, decimal_places=4)
    exit_price: Optional[condecimal(max_digits=10, decimal_places=4)] = None
    quantity: int = Field(..., gt=0)
    fees: condecimal(max_digits=6, decimal_places=2) = 0.00
    notes: Optional[str] = None
    emotion: Optional[str] = None
    mistakes: Optional[List[str]] = Field(default_factory=list)
    tags: Optional[List[str]] = Field(default_factory=list)
    screenshot_urls: Optional[List[str]] = Field(default_factory=list)

class TradeResponse(TradeCreate):
    """Schema for the API response (After decryption and formatting)."""
    id: str
    user_id: str
    created_at: datetime
    pnl: Optional[condecimal(max_digits=10, decimal_places=2)] = None
    r_multiple: Optional[condecimal(max_digits=5, decimal_places=2)] = None
    return_pct: Optional[condecimal(max_digits=5, decimal_places=2)] = None

    class Config:
        from_attributes = True

# -----------------
# STRATEGY MODELS
# -----------------
class StrategyBase(BaseModel):
    """Base fields for a Strategy (Playbook)."""
    name: constr(min_length=1, max_length=50)
    emoji: Optional[str] = None
    description: Optional[str] = None
    color: Optional[str] = None
    is_public: bool = False
    rules: Dict[str, Any] = Field(default_factory=dict)

class StrategyResponse(StrategyBase):
    """The full Strategy model returned by the API."""
    id: str
    user_id: str
    created_at: datetime
    
    class Config:
        from_attributes = True

# =====================================================================
# 4. CHAT SESSION & MESSAGE MODELS
# =====================================================================

class ChatSessionCreate(BaseModel):
    """Schema for creating a new chat session (Input)."""
    title: str = "New Chat"

class ChatSessionResponse(ChatSessionCreate):
    """The full chat session model returned by the API."""
    id: str
    user_id: str
    created_at: datetime
    
    class Config:
        from_attributes = True

class ChatMessage(BaseModel):
    """Base model for a single message in a chat session."""
    role: MessageRole
    content: str
    
    # Optional fields for tool calls/responses
    tool_calls: Optional[List[Dict[str, Any]]] = None 
    tool_call_id: Optional[str] = None

class ChatMessageInDB(ChatMessage):
    """Schema for a chat message as stored in the database."""
    id: str
    session_id: str
    user_id: str
    created_at: datetime

    class Config:
        from_attributes = True

# =====================================================================
# 5. FEATURE GATING MODELS (SAAS Tier Management)
# =====================================================================

class PlanFeature(str, Enum):
    AI_CHAT_PROMPT_LIMIT = "AI_CHAT_PROMPT_LIMIT"
    AUTO_TAGGING = "AUTO_TAGGING"
    MAX_TRADES_MONTH = "MAX_TRADES_MONTH"
    ADVANCED_ANALYTICS = "ADVANCED_ANALYTICS"
    EXPORT_CSV = "EXPORT_CSV"

class PlanGate(BaseModel):
    """A model used to define the actual limits for each plan/feature."""
    MAX_TRADES_MONTH: Dict[Plan, Optional[int]] = {
        Plan.FREE: 50,
        Plan.PRO: None,
        Plan.PRO_PLUS: None,
    }
    AI_CHAT_PROMPT_LIMIT: Dict[Plan, int] = {
        Plan.FREE: 5,
        Plan.PRO: 50,
        Plan.PRO_PLUS: 200,
    }
    AUTO_TAGGING: Dict[Plan, bool] = {
        Plan.FREE: False,
        Plan.PRO: True,
        Plan.PRO_PLUS: True,
    }

PLAN_GATES = PlanGate()