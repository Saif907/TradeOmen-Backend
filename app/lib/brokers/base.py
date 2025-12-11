# backend/app/lib/brokers/base.py
from abc import ABC, abstractmethod
from typing import List, Dict, Any
from decimal import Decimal

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
        Fetches trades and returns them in a RAW format (broker-specific JSON).
        """
        pass

    @abstractmethod
    def normalize_trades(self, raw_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Converts broker-specific raw data into our TradeOmen 'Trade' dictionary format.
        
        CRITICAL: Financial fields MUST be converted to Decimal.
        
        Expected Output Keys:
        - symbol: str (Uppercase)
        - direction: str ("Long" or "Short")
        - entry_price: Decimal
        - quantity: Decimal
        - fees: Decimal
        - entry_time: str (ISO 8601)
        - status: str ("CLOSED" for spot/filled orders)
        """
        pass