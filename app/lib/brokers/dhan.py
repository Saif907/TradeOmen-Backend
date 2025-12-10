# backend/app/lib/brokers/dhan.py
import httpx
from typing import List, Dict, Any
from datetime import datetime
from app.lib.brokers.base import BrokerAdapter
import logging

logger = logging.getLogger(__name__)

class DhanAdapter(BrokerAdapter):
    BASE_URL = "https://api.dhan.co"

    def __init__(self, credentials):
        super().__init__(credentials)
        # Frontend Mapping:
        # api_key -> Client ID
        # api_secret -> Access Token
        self.client_id = credentials.get("api_key")
        self.access_token = credentials.get("api_secret")
        
        self.headers = {
            "access-token": self.access_token,
            "client-id": self.client_id,
            "Content-Type": "application/json"
        }

    async def authenticate(self) -> bool:
        """
        Dhan tokens are long-lived, so we just check if a simple call works.
        """
        async with httpx.AsyncClient() as client:
            try:
                # Lightweight call to check connectivity (e.g., Get Fund Limits)
                res = await client.get(f"{self.BASE_URL}/fund-limits", headers=self.headers)
                return res.status_code == 200
            except Exception as e:
                logger.error(f"Dhan Auth Failed: {e}")
                return False

    async def fetch_recent_trades(self, days: int = 30) -> List[Dict[str, Any]]:
        """
        Fetches Trade History from Dhan.
        """
        async with httpx.AsyncClient() as client:
            try:
                # Dhan API for Trade History
                # Note: Dhan returns paginated data, simplified here for "Easiest" start
                url = f"{self.BASE_URL}/trades" 
                res = await client.get(url, headers=self.headers)
                
                if res.status_code != 200:
                    logger.error(f"Failed to fetch Dhan trades: {res.text}")
                    return []
                
                data = res.json()
                # Dhan response format: { "status": "success", "data": [...] }
                return data.get("data", [])
            except Exception as e:
                logger.error(f"Dhan Sync Error: {e}")
                return []

    def normalize_trades(self, raw_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        normalized = []
        
        for t in raw_data:
            try:
                # --- Dhan Spec Mapping ---
                # "transactionType": "BUY" or "SELL"
                # "tradingSymbol": "HDFC"
                # "tradedPrice": 1500.50
                # "tradedQuantity": 10
                # "tradeTime": "2023-10-25 10:30:00"
                # "exchangeSegment": "NSE_EQ"
                
                direction = "Long" if t.get("transactionType") == "BUY" else "Short"
                
                # Infer Instrument Type from Segment
                segment = t.get("exchangeSegment", "NSE_EQ")
                inst_type = "FUTURES" if "FUT" in segment else "STOCK"
                
                trade = {
                    "symbol": t.get("tradingSymbol"),
                    "instrument_type": inst_type,
                    "direction": direction,
                    "entry_price": float(t.get("tradedPrice", 0)),
                    "quantity": float(t.get("tradedQuantity", 0)),
                    "entry_time": t.get("tradeTime"), # Needs parsing to ISO in real app
                    "status": "CLOSED", # Historical trades are usually filled/closed orders
                    "fees": 0, # Dhan might provide brokerage in a separate field
                    "metadata": t # Store full raw object for debugging
                }
                
                # Basic validation
                if trade["entry_price"] > 0:
                    normalized.append(trade)
                    
            except Exception as e:
                logger.warning(f"Skipping malformed Dhan trade: {e}")
                continue
                
        return normalized