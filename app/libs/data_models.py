# backend/app/libs/data_models.py

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, condecimal, constr

# --- Utility Enums/Types ---

TradeDirection = constr(pattern=r"^(LONG|SHORT|OTHER)$")
TradeStatus = constr(pattern=r"^(OPEN|CLOSED|CANCELED)$")
ImportStatus = constr(pattern=r"^(PENDING|PROCESSING|FAILED|COMPLETED|CANCELLED)$")
ImportJobType = constr(pattern=r"^(CSV_IMPORT|BROKER_SYNC)$")
RegionCode = constr(pattern=r"^(IN|US|EU|OTHER)$")

# --- CORE USER MODELS ---

class UserToken(BaseModel):
    """Model representing the decoded Supabase JWT payload."""
    user_id: UUID = Field(..., alias="sub", description="Unique User ID (UUID) from Supabase.")

class UserProfileUpdate(BaseModel):
    """Schema for updating user profile settings."""
    region_code: Optional[RegionCode] = None
    default_currency: Optional[constr(min_length=3, max_length=3)] = None
    consent_ai_training: Optional[bool] = None

# --- STRATEGY MODELS ---

class StrategyRuleGroup(BaseModel):
    """A list of rules within a single group (e.g., Entry Rules)."""
    name: str = Field(..., description="Name of the rule group (e.g., 'Entry Rules')")
    rules: List[str] = Field(..., description="List of individual rules.")

class StrategyBase(BaseModel):
    """Base schema for a trading strategy."""
    name: constr(min_length=1, max_length=100)
    emoji: Optional[constr(max_length=5)] = None
    description: Optional[constr(max_length=500)] = None
    color_hex: Optional[constr(pattern=r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")] = None
    style: Optional[constr(max_length=50)] = None
    instrument_types: Optional[List[str]] = None
    track_missed_trades: Optional[bool] = False
    rules: Optional[List[StrategyRuleGroup]] = None

class StrategyCreate(StrategyBase):
    """Schema for creating a new strategy."""
    pass

class StrategyOut(StrategyBase):
    """Schema for returning a strategy from the API."""
    id: UUID
    user_id: UUID
    created_at: datetime

    class Config:
        from_attributes = True

# --- TRADE MODELS ---

class TradeBase(BaseModel):
    """Core trade data submitted by the user/importer."""
    symbol: constr(min_length=1, max_length=20)
    direction: TradeDirection
    status: Optional[TradeStatus] = "CLOSED"
    
    entry_price: condecimal(max_digits=10, decimal_places=4)
    exit_price: condecimal(max_digits=10, decimal_places=4)
    quantity: int = Field(..., gt=0)
    
    entry_time: datetime
    exit_time: Optional[datetime] = None
    
    raw_notes: Optional[constr(max_length=2000)] = None
    
    strategy_id: Optional[UUID] = None
    broker_account_id: Optional[UUID] = None

class TradeCreate(TradeBase):
    """Schema for creating a single manual trade."""
    pass

class TradeImport(TradeBase):
    """Schema for trades coming from bulk import (AI worker will populate tags later)."""
    pnl: Optional[condecimal(max_digits=12, decimal_places=2)] = None
    tags: Optional[List[str]] = None

class TradeOut(TradeCreate):
    """Schema for returning a trade from the API (encrypted fields will be hidden/transformed)."""
    id: UUID
    user_id: UUID
    created_at: datetime
    
    # This field holds the AES-encrypted data retrieved directly from the DB
    encrypted_notes: Optional[str] = None 
    
    # This field is populated by the decryption function for the frontend
    raw_notes: Optional[str] = None
    
    class Config:
        from_attributes = True
        
# --- AI CHAT MODELS ---

class ChatStart(BaseModel):
    """Request to start a new chat session (Input Model)."""
    topic: Optional[constr(max_length=100)] = None

class ChatSessionOut(BaseModel):
    """Schema for returning a chat session metadata (Response Model - Fix 3)."""
    id: UUID
    user_id: UUID
    topic: Optional[str]
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True

class ChatMessageIn(BaseModel):
    """Incoming user message for an active session."""
    session_id: UUID
    raw_message: constr(min_length=1, max_length=1000)

class ChatMessageOut(BaseModel):
    """Outgoing message (user or assistant) from the API."""
    id: int
    role: constr(pattern=r"^(user|assistant)$")
    content: str
    created_at: datetime
    
    class Config:
        from_attributes = True
        
# --- DATA IMPORT MODELS ---

class ImportJobStart(BaseModel):
    """Request to start a new data import job (CSV upload)"""
    job_type: ImportJobType = Field(..., description="Type of import: CSV_IMPORT or BROKER_SYNC")
    storage_path: Optional[str] = Field(None, description="Path to the uploaded file in Supabase Storage.")

class ImportJobOut(BaseModel):
    """Schema for returning import job status to the user."""
    id: UUID
    status: ImportStatus
    job_type: ImportJobType
    total_records: Optional[int] = None
    processed_records: Optional[int] = None
    error_message: Optional[str] = None
    updated_at: datetime

    class Config:
        from_attributes = True