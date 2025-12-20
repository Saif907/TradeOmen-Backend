from typing import List, Optional, Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field, field_validator, model_validator
# Import Enums from common_schemas
from .common_schemas import InstrumentType, TradeSide, TradeStatus

class TradeBase(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20)
    instrument_type: InstrumentType = InstrumentType.STOCK
    direction: TradeSide
    status: TradeStatus = TradeStatus.OPEN

    entry_price: float = Field(..., gt=0)
    quantity: float = Field(..., gt=0)

    exit_price: Optional[float] = Field(None, gt=0)
    stop_loss: Optional[float] = Field(None, gt=0)
    target: Optional[float] = Field(None, gt=0)

    entry_time: datetime
    exit_time: Optional[datetime] = None

    fees: float = Field(0.0, ge=0)

    encrypted_notes: Optional[str] = None
    notes: Optional[str] = None

    tags: Optional[List[str]] = Field(default_factory=list)
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict)
    strategy_id: Optional[str] = None
    encrypted_screenshots: Optional[str] = None

    @field_validator("symbol")
    @classmethod
    def uppercase_symbol(cls, v):
        return v.upper().strip()

class TradeCreate(TradeBase):
    screenshots: Optional[List[str]] = None

    @model_validator(mode="after")
    def validate_logic(self):
        if self.exit_time and self.exit_time < self.entry_time:
            raise ValueError("Exit time cannot be before entry time.")
        
        if self.status == TradeStatus.CLOSED:
            if not self.exit_price or not self.exit_time:
                raise ValueError("Closed trades require exit price and time.")
        return self

    def calculate_pnl(self) -> Optional[float]:
        if not self.exit_price:
            return None
        multiplier = 1 if self.direction == TradeSide.LONG else -1
        gross_pnl = (self.exit_price - self.entry_price) * self.quantity * multiplier
        return gross_pnl - self.fees

class TradeUpdate(BaseModel):
    symbol: Optional[str] = None
    instrument_type: Optional[InstrumentType] = None
    direction: Optional[TradeSide] = None
    status: Optional[TradeStatus] = None
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    quantity: Optional[float] = None
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    entry_time: Optional[datetime] = None
    exit_time: Optional[datetime] = None
    fees: Optional[float] = None
    notes: Optional[str] = None
    tags: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None
    strategy_id: Optional[str] = None

class TradeResponse(TradeBase):
    id: str
    user_id: str
    pnl: Optional[float]
    created_at: str
    notes: Optional[str] = Field(None, alias="encrypted_notes")
    strategies: Optional[Dict[str, Any]] = None

class PaginatedTradesResponse(BaseModel):
    data: List[TradeResponse]
    total: int
    page: int
    size: int