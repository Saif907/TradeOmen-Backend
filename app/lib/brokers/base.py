# backend/app/lib/brokers/base.py
from abc import ABC, abstractmethod
from typing import List, Dict, Any
from decimal import Decimal

class BrokerAdapter(ABC):
    """
    Base contract for all broker integrations.

    Every broker adapter MUST:
    - Authenticate with the broker's API
    - Fetch recent trades (raw format)
    - Normalize trades into TradeOmen’s canonical format:
        {
          symbol: str,
          instrument_type: str,
          direction: str,
          entry_price: Decimal,
          quantity: Decimal,
          fees: Decimal,
          entry_time: ISO str,
          status: "CLOSED" | "OPEN",
          metadata: Dict
        }
    """

    def __init__(self, credentials: Dict[str, str]):
        # Contains access_token OR api_key + api_secret
        self.credentials = credentials

    @abstractmethod
    async def authenticate(self) -> bool:
        """Validate credentials (token) with a lightweight API request."""
        pass

    @abstractmethod
    async def fetch_recent_trades(self, days: int = 30) -> List[Dict[str, Any]]:
        """Fetch broker-specific raw trade data."""
        pass

    @abstractmethod
    def normalize_trades(self, raw_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Normalize raw broker data into canonical TradeOmen trade format."""
        pass
