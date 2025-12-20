from typing import List, Optional, Dict
from datetime import datetime
from pydantic import BaseModel, Field
# Import from the sibling "common_schemas" file
from .common_schemas import InstrumentType 

class StrategyBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    emoji: str = Field("📈", max_length=4)
    color_hex: str = Field("#8b5cf6", pattern="^#([A-Fa-f0-9]{6}|[A-Fa-f0-9]{3})$")
    style: Optional[str] = Field(None, description="Day Trading, Swing, etc.")
    instrument_types: List[str] = []
    rules: Dict[str, List[str]] = Field(default_factory=dict)
    track_missed_trades: bool = True

class StrategyCreate(StrategyBase):
    pass

class StrategyUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    emoji: Optional[str] = None
    color_hex: Optional[str] = None
    style: Optional[str] = None
    instrument_types: Optional[List[str]] = None
    rules: Optional[Dict[str, List[str]]] = None
    track_missed_trades: Optional[bool] = None

class StrategyResponse(StrategyBase):
    id: str
    user_id: str
    created_at: datetime
    updated_at: Optional[datetime] = None