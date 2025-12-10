# backend/app/lib/brokers/base.py
from abc import ABC, abstractmethod
from typing import List, Dict, Any
from datetime import datetime

class BrokerAdapter(ABC):
    """
    Abstract Base Class that enforces a standard interface for all brokers.
    """
    def __init__(self, credentials: Dict[str, str]):
        self.credentials = credentials

    @abstractmethod
    async def authenticate(self) -> bool:
        """
        Validates the credentials (and performs login/refresh if needed).
        """
        pass

    @abstractmethod
    async def fetch_recent_trades(self, days: int = 30) -> List[Dict[str, Any]]:
        """
        Fetches trades and returns them in a RAW format.
        """
        pass

    @abstractmethod
    def normalize_trades(self, raw_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Converts broker-specific raw data into our TradeOmen 'Trade' dictionary format.
        Mapping: 'Symbol' -> 'symbol', 'BuyPrice' -> 'entry_price', etc.
        """
        pass