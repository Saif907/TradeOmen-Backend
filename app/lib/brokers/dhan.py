# backend/app/lib/brokers/dhan.py
from __future__ import annotations

import httpx
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import dateutil.parser
import uuid

from app.lib.brokers.base import BrokerAdapter
from app.core.config import settings

logger = logging.getLogger(__name__)


class DhanAdapter(BrokerAdapter):
    """
    Production-grade Dhan adapter.

    - Uses BASE_URL = https://api.dhan.co (mixed-versioning).
    - Use POST https://api.dhan.co/v2/token for exchanging tokenId -> access_token.
    - Use GET https://api.dhan.co/fund-limits to validate token (no /v2).
    - Use GET https://api.dhan.co/v2/trades for fetching trades.
    """

    BASE_URL = "https://api.dhan.co"

    def __init__(self, credentials: Dict[str, str]):
        """
        credentials must contain {"access_token": "<JWT>"} for sync-time usage.
        """
        super().__init__(credentials)
        self.access_token: Optional[str] = credentials.get("access_token")
        # Build headers lazily; only include access-token header when token exists.
        self.headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self.access_token:
            self.headers["access-token"] = self.access_token

    # -------------------------
    # Token exchange (server-side)
    # -------------------------
    @classmethod
    async def exchange_token(cls, token_id: str, *, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
        """
        Exchange tokenId for an access_token using the configured client id & secret.
        Returns dict: {"access_token": "...", "expires_at": "ISO"} on success, otherwise None.
        """
        if not settings.DHAN_CLIENT_ID or not settings.DHAN_CLIENT_SECRET:
            logger.error("Dhan client credentials not configured in settings.")
            return None

        url = f"{cls.BASE_URL}/v2/token"
        payload = {
            "tokenId": token_id,
            "clientId": settings.DHAN_CLIENT_ID,
            "clientSecret": settings.DHAN_CLIENT_SECRET,
        }

        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                resp = await client.post(url, json=payload)
                if resp.status_code != 200:
                    logger.error("Dhan token exchange failed %s: %s", resp.status_code, resp.text)
                    return None

                data = resp.json()
                # The response shape may vary; try a couple common shapes
                token = data.get("access_token") or (data.get("data") or {}).get("access_token")
                # Some implementations return expiry seconds or expires_at - handle both
                expires_in = data.get("expires_in") or (data.get("data") or {}).get("expires_in")
                expires_at = None
                if expires_in:
                    try:
                        expires_at = (datetime.utcnow() + timedelta(seconds=int(expires_in))).isoformat()
                    except Exception:
                        expires_at = None

                if not token:
                    logger.error("Dhan token exchange returned no access_token: %s", data)
                    return None

                return {"access_token": token, "expires_at": expires_at}
            except Exception as e:
                logger.exception("Exception during Dhan token exchange: %s", e)
                return None

    # -------------------------
    # Authentication (validate token)
    # -------------------------
    async def authenticate(self, *, timeout: float = 8.0) -> bool:
        """
        Validate access_token by calling a lightweight endpoint (fund-limits).
        Return True if token is valid (HTTP 200). Treat 401/403 as invalid token.
        Other statuses are logged and treated as non-auth success (return False).
        """
        if not self.access_token:
            logger.warning("DhanAdapter.authenticate called without access_token")
            return False

        url = f"{self.BASE_URL}/fund-limits"  # not under /v2
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                resp = await client.get(url, headers=self.headers)
                if resp.status_code == 200:
                    return True
                if resp.status_code in (401, 403):
                    logger.info("Dhan token invalid/expired: %s", resp.status_code)
                    return False
                logger.error("Dhan authenticate unexpected status %s: %s", resp.status_code, resp.text)
                return False
            except Exception as e:
                logger.exception("Dhan authenticate exception: %s", e)
                return False

    # -------------------------
    # Fetch trades
    # -------------------------
    async def fetch_recent_trades(self, days: int = 30, *, timeout: float = 15.0) -> List[Dict[str, Any]]:
        """
        Fetch trades for the authenticated user.
        Returns a list of broker-specific trade JSON objects (may be empty on error).
        """
        if not self.access_token:
            logger.error("fetch_recent_trades called without access_token")
            return []

        url = f"{self.BASE_URL}/v2/trades"
        params = {"days": days} if days else {}
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                resp = await client.get(url, headers=self.headers, params=params)
                if resp.status_code != 200:
                    logger.error("Dhan trades fetch failed %s: %s", resp.status_code, resp.text)
                    return []
                payload = resp.json()
                # Usually { "status": "success", "data": [...] }
                return payload.get("data") or payload.get("trades") or []
            except Exception as e:
                logger.exception("Dhan fetch_recent_trades exception: %s", e)
                return []

    # -------------------------
    # Normalize trades
    # -------------------------
    def normalize_trades(self, raw_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Convert Dhan trade objects into canonical TradeOmen format.
        Ensures Decimal types for financials, ISO datetimes, uppercase symbol.
        """
        normalized: List[Dict[str, Any]] = []

        def to_decimal(value) -> Decimal:
            try:
                if value is None:
                    return Decimal("0")
                return Decimal(str(value))
            except (InvalidOperation, ValueError, TypeError):
                return Decimal("0")

        for t in raw_data or []:
            try:
                txn = (t.get("transactionType") or "").upper()
                direction = "LONG" if txn == "BUY" else "SHORT"

                segment = (t.get("exchangeSegment") or "").upper()
                instrument_type = "FUTURES" if "FUT" in segment else "STOCK"

                raw_time = t.get("tradeTime") or t.get("orderTime") or t.get("timestamp")
                try:
                    dt = dateutil.parser.parse(str(raw_time)) if raw_time else datetime.utcnow()
                    entry_time = dt.isoformat()
                except Exception:
                    entry_time = datetime.utcnow().isoformat()

                symbol = (t.get("tradingSymbol") or t.get("symbol") or "").upper()

                entry_price = to_decimal(t.get("tradedPrice") or t.get("price") or 0)
                quantity = to_decimal(t.get("tradedQuantity") or t.get("quantity") or 0)
                fees = to_decimal(t.get("fees") or t.get("brokerage") or t.get("commission") or 0)

                trade = {
                    "symbol": symbol,
                    "instrument_type": instrument_type,
                    "direction": direction,
                    "entry_price": entry_price,
                    "quantity": quantity,
                    "fees": fees,
                    "entry_time": entry_time,
                    "status": "CLOSED",
                    "metadata": t,
                }

                if trade["symbol"] and trade["entry_price"] > 0 and trade["quantity"] > 0:
                    normalized.append(trade)
                else:
                    logger.debug("Skipping Dhan trade missing required fields: %s", t)

            except Exception as e:
                logger.warning("Skipping malformed Dhan trade: %s -- %s", e, t)
                continue

        return normalized
