# backend/app/lib/brokers/binance.py
import time
import hmac
import hashlib
import httpx
from urllib.parse import urlencode
from typing import List, Dict, Any
from datetime import datetime
from app.lib.brokers.base import BrokerAdapter
import logging

logger = logging.getLogger(__name__)

class BinanceAdapter(BrokerAdapter):
    BASE_URL = "https://api.binance.com"

    def __init__(self, credentials):
        super().__init__(credentials)
        self.api_key = credentials.get("api_key")
        self.api_secret = credentials.get("api_secret")
        
    def _sign(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Binance requires request parameters to be signed with HMAC SHA256.
        """
        params["timestamp"] = int(time.time() * 1000)
        query_string = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        params["signature"] = signature
        return params

    async def authenticate(self) -> bool:
        """
        Checks if API Key is valid by fetching account status.
        """
        async with httpx.AsyncClient() as client:
            try:
                params = self._sign({})
                headers = {"X-MBX-APIKEY": self.api_key}
                res = await client.get(
                    f"{self.BASE_URL}/api/v3/account", 
                    params=params, 
                    headers=headers
                )
                return res.status_code == 200
            except Exception as e:
                logger.error(f"Binance Auth Error: {e}")
                return False

    async def fetch_recent_trades(self, days: int = 30) -> List[Dict[str, Any]]:
        """
        Fetches trades for major pairs. 
        NOTE: Binance REST API requires specifying a symbol.
        For this MVP, we default to syncing major pairs.
        """
        async with httpx.AsyncClient() as client:
            all_trades = []
            # In a production app, we would fetch user's non-zero balances first to know which symbols to query.
            # For MVP, we check the most common trading pairs.
            symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT"]
            headers = {"X-MBX-APIKEY": self.api_key}
            
            for symbol in symbols:
                try:
                    # 'limit': 50 trades per symbol
                    params = self._sign({"symbol": symbol, "limit": 50})
                    res = await client.get(
                        f"{self.BASE_URL}/api/v3/myTrades", 
                        params=params, 
                        headers=headers
                    )
                    if res.status_code == 200:
                        trades = res.json()
                        # Ensure symbol context is preserved
                        for t in trades:
                            if "symbol" not in t: 
                                t["symbol"] = symbol
                        all_trades.extend(trades)
                except Exception as e:
                    # Log but continue to next symbol
                    logger.warning(f"Failed to fetch Binance trades for {symbol}: {e}")
            
            return all_trades

    def normalize_trades(self, raw_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        normalized = []
        for t in raw_data:
            try:
                # Binance 'myTrades' response object:
                # {
                #   "symbol": "BTCUSDT",
                #   "id": 28457,
                #   "price": "4000.00",
                #   "qty": "1.00",
                #   "commission": "10.10",
                #   "time": 1499865549590,
                #   "isBuyer": true, ...
                # }
                
                direction = "Long" if t.get("isBuyer") else "Short"
                
                # Convert timestamp (ms) to ISO format
                entry_time = datetime.fromtimestamp(t["time"] / 1000).isoformat()
                
                trade = {
                    "symbol": t.get("symbol", "UNKNOWN"),
                    "instrument_type": "CRYPTO",
                    "direction": direction,
                    "entry_price": float(t["price"]),
                    "quantity": float(t["qty"]),
                    "fees": float(t.get("commission", 0)),
                    "entry_time": entry_time,
                    # Spot trades are immediate executions, so we log them as 'CLOSED' snapshots
                    "status": "CLOSED",
                    "metadata": t
                }
                
                normalized.append(trade)
            except Exception as e:
                logger.warning(f"Skipping malformed Binance trade: {e}")
                
        return normalized