# backend/app/lib/csv_parser.py
import io
import json
import logging
import re
import asyncio
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple, Iterable

import pandas as pd

from app.lib.llm_client import llm_client

logger = logging.getLogger(__name__)

# Target schema - documentation only
TARGET_SCHEMA = {
    "symbol": "The asset ticker (e.g., RELIANCE, TCS, NIFTY)",
    "direction": "LONG or SHORT (Buy/Sell)",
    "entry_date": "Date/Time of entry (ISO format preferred)",
    "entry_price": "Price at entry (Numeric)",
    "exit_date": "Date/Time of exit (optional)",
    "exit_price": "Price at exit (optional)",
    "quantity": "Number of units/shares",
    "pnl": "Profit or Loss (optional)",
    "fees": "Commissions or fees (optional)",
    "notes": "Text notes / strategy / emotions",
    "instrument_type": "Asset class (STOCK, CRYPTO, FOREX, FUTURES)"
}

# Conservative header name candidates
HEADER_NAME_MAP = {
    "symbol": ["symbol", "ticker", "asset", "instrument", "scrip"],
    "direction": ["side", "action", "type", "buy_sell", "buy/sell"],
    "entry_date": ["entry_date", "entry", "timestamp", "time", "open_time", "date"],
    "entry_price": ["entry_price", "price", "entryprice", "open_price"],
    "exit_date": ["exit_date", "close_time", "exit", "close_date"],
    "exit_price": ["exit_price", "close_price", "exitprice"],
    "quantity": ["qty", "quantity", "volume", "size"],
    "pnl": ["pnl", "profit", "loss", "pl"],
    "fees": ["fees", "commission", "comm"],
    "notes": ["notes", "remark", "note", "comments", "strategy"],
    "instrument_type": ["instrument_type", "asset_class", "assetclass", "instrumenttype"],
}

DIRECTION_POSITIVE = {"BUY", "B", "LONG", "L", "BUYER"}
DIRECTION_NEGATIVE = {"SELL", "S", "SHORT", "SH", "SELLER"}


# -------------------------
# Helpers
# -------------------------
def _find_json_substring(text: str) -> Optional[str]:
    """
    Find first balanced JSON object in arbitrary text.
    """
    if not text:
        return None
    start = None
    depth = 0
    for i, ch in enumerate(text):
        if ch == "{":
            if start is None:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    return text[start : i + 1]
    return None


def _is_parseable_date(value: Any) -> bool:
    try:
        if pd.isna(value):
            return False
        parsed = pd.to_datetime(str(value), errors="coerce")
        return not pd.isna(parsed)
    except Exception:
        return False


def _is_number_like(value: Any) -> bool:
    try:
        if pd.isna(value):
            return False
        s = str(value).strip().replace(",", "").replace("$", "")
        if s == "":
            return False
        Decimal(s)
        return True
    except Exception:
        return False


def normalize_instrument_type(raw: Any) -> Optional[str]:
    """
    Map a wide variety of real-world instrument strings to canonical:
    'STOCK', 'CRYPTO', 'FOREX', 'FUTURES'
    Returns None if unable to map confidently.
    """
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if not s:
        return None

    # direct token map
    token_map = {
        "STOCK": ["STOCK", "STOCKS", "SHARE", "SHARES", "EQUITY", "EQ", "STK", "STOCKS/SHARES"],
        "CRYPTO": ["CRYPTO", "CRYPTOCURRENCY", "COIN", "BTC", "ETH", "BCH", "LTC", "DOGE"],
        "FOREX": ["FOREX", "FX", "CURRENCY", "FOREXPAIR", "FOREX PAIR"],
        "FUTURES": ["FUTURES", "FUTURE", "FUT", "F&O", "FO", "FANDO", "F&O/OPTION", "OPTION", "OPTIONS", "OPTNS"],
    }

    for canon, toks in token_map.items():
        for t in toks:
            if s == t or s.startswith(t + " ") or s.startswith(t + "_") or s == t + "S":
                return canon

    # controlled substring matches
    if any(k in s for k in ["F&O", "F & O", "FUT", "FUTURE", "FUTURES", "OPTION", "OPTIONS"]):
        return "FUTURES"
    if any(k in s for k in ["CRYPTO", "COIN", "BTC", "ETH"]):
        return "CRYPTO"
    if any(k in s for k in ["FOREX", " FX", "FX ", "FX/"]):
        return "FOREX"
    if any(k in s for k in ["STOCK", "EQUITY", "SHARE", "INDEX", "NIFTY", "BANKNIFTY"]):
        return "STOCK"

    return None


# -------------------------
# CSV Parser
# -------------------------
class CSVParser:
    def __init__(self, llm_retries: int = 2, llm_timeout: int = 8):
        """
        llm_retries: number of retries after initial attempt (0 = try once)
        llm_timeout: seconds per LLM call
        """
        self.llm_retries = llm_retries
        self.llm_timeout = llm_timeout

    # ---------------
    # Structure analysis
    # ---------------
    def analyze_structure(self, file_content: bytes, peek_rows: int = 5) -> Dict[str, Any]:
        """
        Return {"headers": [...], "sample": [...], "has_header": bool}
        If headerless, headers are Column_0..Column_N.
        """
        try:
            df_peek = pd.read_csv(io.BytesIO(file_content), nrows=peek_rows, header=None, dtype=str)
            df_peek = df_peek.dropna(how="all")
            if df_peek.empty:
                return {"headers": [], "sample": [], "has_header": True}

            first = df_peek.iloc[0].astype(str).tolist()
            second = df_peek.iloc[1].astype(str).tolist() if len(df_peek) > 1 else []

            def stats(row_vals: List[str]) -> Tuple[int, int]:
                return (
                    sum(1 for v in row_vals if _is_parseable_date(v)),
                    sum(1 for v in row_vals if _is_number_like(v)),
                )

            first_date_count, first_num_count = stats(first)
            second_date_count, second_num_count = stats(second) if second else (0, 0)

            num_cols = len(first)
            first_rate = (first_date_count + first_num_count) / max(1, num_cols)
            second_rate = (second_date_count + second_num_count) / max(1, num_cols)

            # conservative: if first row largely numeric/date => headerless
            has_header = not (first_rate >= 0.6)

            if not has_header:
                df = pd.read_csv(io.BytesIO(file_content), header=None, dtype=str)
                headers = [f"Column_{i}" for i in range(len(df.columns))]
                sample = df.iloc[0].astype(str).tolist() if not df.empty else []
                logger.info("Detected headerless CSV; using generated Column_X headers.")
            else:
                df_full = pd.read_csv(io.BytesIO(file_content), dtype=str)
                headers = list(df_full.columns)
                sample = df_full.iloc[0].astype(str).tolist() if not df_full.empty else []

            return {"headers": headers, "sample": sample, "has_header": has_header}
        except Exception:
            logger.exception("analyze_structure failed")
            return {"headers": [], "sample": [], "has_header": True}

    def read_headers(self, file_content: bytes) -> List[str]:
        return self.analyze_structure(file_content)["headers"]

    # ---------------
    # LLM mapping w/ retries & JSON extraction
    # ---------------
    async def guess_mapping(self, headers: List[str], sample_row: List[str], user_prompt: str = "") -> Dict[str, str]:
        """
        Ask LLM to map CSV headers -> TARGET_SCHEMA. Falls back to heuristics on failure.
        Returns mapping: { target_field: csv_header_name }
        """
        system_instruction = (
            "You are a Data Mapping Specialist. Map the user's CSV columns to the Target Schema.\n"
            f"Target Schema: {json.dumps(TARGET_SCHEMA)}\n\n"
            "Output JSON only: e.g. {\"symbol\":\"Column_2\",\"entry_price\":\"price\"}. Omit uncertain fields."
        )
        user_message = f"Headers: {json.dumps(headers)}\nSample Data: {json.dumps(sample_row)}\nUser Note: {user_prompt}"

        attempt = 0
        last_exc: Optional[Exception] = None
        while attempt <= self.llm_retries:
            try:
                resp = await asyncio.wait_for(
                    llm_client.generate_response(
                        messages=[
                            {"role": "system", "content": system_instruction},
                            {"role": "user", "content": user_message},
                        ],
                        model="gemini-2.5-flash",
                        provider="gemini",
                        response_format={"type": "json_object"},
                    ),
                    timeout=self.llm_timeout,
                )

                content = None
                if isinstance(resp, dict):
                    content = resp.get("content") or resp.get("output") or None
                elif isinstance(resp, str):
                    content = resp
                else:
                    content = str(resp)

                if not content or not isinstance(content, str) or not content.strip():
                    raise ValueError("LLM returned empty content")

                # try direct parse
                try:
                    mapping = json.loads(content)
                except Exception:
                    json_sub = _find_json_substring(content)
                    if not json_sub:
                        raise
                    mapping = json.loads(json_sub)

                # sanitize mapping
                sanitized: Dict[str, str] = {}
                for tgt, cand in mapping.items():
                    if not isinstance(cand, str):
                        continue
                    cand = cand.strip()
                    if cand in headers or re.fullmatch(r"Column_\d+", cand):
                        sanitized[tgt] = cand

                if sanitized:
                    logger.info("LLM mapping accepted: %s", sanitized)
                    return sanitized
                raise ValueError("LLM mapping returned no valid header names")

            except Exception as exc:
                last_exc = exc
                logger.warning("LLM mapping attempt %s failed: %s", attempt + 1, exc)
                await asyncio.sleep(2 ** attempt)
                attempt += 1

        logger.exception("LLM mapping exhausted retries; falling back to heuristics. Last error: %s", last_exc)
        return self._heuristic_mapping(headers, sample_row)

    # ---------------
    # Heuristic (deterministic, conservative)
    # ---------------
    def _heuristic_mapping(self, headers: List[str], sample_row: List[str]) -> Dict[str, str]:
        mapping: Dict[str, str] = {}
        header_norm = [h.lower() if isinstance(h, str) else "" for h in headers]

        # exact / prefix / underscore-normalized matches
        for tgt, candidates in HEADER_NAME_MAP.items():
            for idx, hn in enumerate(header_norm):
                for cand in candidates:
                    cand = cand.lower()
                    if hn == cand or hn.startswith(cand) or hn.replace("_", "") == cand:
                        mapping[tgt] = headers[idx]
                        break
                if tgt in mapping:
                    break

        # sample-driven inference
        for idx, raw in enumerate(sample_row):
            if idx >= len(headers):
                continue
            header = headers[idx]
            if header in mapping.values():
                continue
            v = str(raw) if raw is not None else ""
            v_up = v.strip().upper()
            if v_up in DIRECTION_POSITIVE.union(DIRECTION_NEGATIVE) and "direction" not in mapping:
                mapping["direction"] = header
                continue
            if _is_parseable_date(v) and "entry_date" not in mapping:
                mapping["entry_date"] = header
                continue
            if _is_number_like(v):
                if "entry_price" not in mapping:
                    mapping["entry_price"] = header
                    continue
                if "quantity" not in mapping:
                    mapping["quantity"] = header
                    continue
            if re.fullmatch(r"[A-Z0-9\.\-]{2,20}", v_up) and any(c.isalpha() for c in v_up) and "symbol" not in mapping:
                mapping["symbol"] = header
                continue

        logger.info("Heuristic mapping produced: %s", mapping)
        return mapping

    # ---------------
    # Resolve mapping values to actual df columns
    # ---------------
    @staticmethod
    def _resolve_csv_column_name(requested: str, df_columns: Iterable[str]) -> Optional[str]:
        cols = list(df_columns)
        if requested in cols:
            return requested
        if re.fullmatch(r"Column_\d+", requested) and requested in cols:
            return requested
        if requested.isdigit():
            idx = int(requested)
            if 0 <= idx < len(cols):
                return cols[idx]
        return None

    # ---------------
    # Process & normalize (returns list[dict])
    # ---------------
    def process_and_normalize(self, file_content: bytes, mapping: Dict[str, str]) -> List[Dict[str, Any]]:
        """
        Read CSV bytes and return normalized trade dicts.
        Numeric fields -> Decimal or None; dates -> ISO string or None.
        Unmapped fields stored under _metadata.
        """
        use_header_none = False
        if mapping:
            try:
                sample_val = next(iter(mapping.values()))
                if re.fullmatch(r"Column_\d+", sample_val):
                    use_header_none = True
            except StopIteration:
                use_header_none = False

        try:
            df = pd.read_csv(io.BytesIO(file_content), header=None if use_header_none else 0, dtype=str)
        except Exception as exc:
            logger.exception("Failed to read CSV content: %s", exc)
            raise ValueError(f"Failed to read CSV content: {exc}")

        if use_header_none:
            df.columns = [f"Column_{i}" for i in range(len(df.columns))]

        df = df.fillna("")

        # resolve mapping to actual dataframe columns
        resolved_mapping: Dict[str, str] = {}
        for tgt, csv_header in mapping.items():
            resolved = self._resolve_csv_column_name(csv_header, df.columns)
            if resolved:
                resolved_mapping[tgt] = resolved
            else:
                logger.debug("Mapping for target '%s' referenced unknown header '%s'", tgt, csv_header)

        mapped_headers = set(resolved_mapping.values())
        normalized_rows: List[Dict[str, Any]] = []
        numeric_fields = ["entry_price", "exit_price", "quantity", "fees", "pnl"]

        for idx, row in df.iterrows():
            trade: Dict[str, Any] = {}
            metadata: Dict[str, Any] = {}

            # Extract mapped fields using resolved headers
            for target_field, resolved_header in resolved_mapping.items():
                if resolved_header not in df.columns:
                    continue
                raw_val = row[resolved_header]
                if raw_val is None or str(raw_val).strip() == "":
                    continue
                trade[target_field] = str(raw_val).strip()

            # Unmapped columns -> metadata
            for col in df.columns:
                if col in mapped_headers:
                    continue
                val = row[col]
                if val is not None and str(val).strip() != "":
                    metadata[col] = str(val).strip()

            # Direction normalization
            if "direction" in trade:
                d = str(trade["direction"]).strip().upper()
                if d in DIRECTION_POSITIVE:
                    trade["direction"] = "Long"
                elif d in DIRECTION_NEGATIVE:
                    trade["direction"] = "Short"
                else:
                    trade["direction"] = trade["direction"].capitalize()

            # Instrument type normalization: preserve raw in metadata and set canonical or None
            if "instrument_type" in trade:
                raw_it = trade.get("instrument_type")
                normalized_it = normalize_instrument_type(raw_it)
                # keep original raw for traceability
                if metadata is None:
                    metadata = {}
                metadata["instrument_type_raw"] = raw_it
                if normalized_it:
                    trade["instrument_type"] = normalized_it
                else:
                    # set None to avoid backend Pydantic pattern mismatch; original kept in metadata
                    trade["instrument_type"] = None

            # Numeric conversions -> Decimal
            for field in numeric_fields:
                if field in trade:
                    raw = str(trade[field]).replace(",", "").replace("$", "").strip()
                    if raw == "":
                        trade[field] = None
                    else:
                        try:
                            trade[field] = Decimal(raw)
                        except (InvalidOperation, ValueError):
                            logger.debug("Numeric parse failed for row %s field %s value=%s", idx, field, raw)
                            trade[field] = None

            # Date normalization -> ISO
            for date_field in ["entry_date", "exit_date"]:
                if date_field in trade:
                    parsed = pd.to_datetime(trade[date_field], errors="coerce")
                    if pd.isna(parsed):
                        trade[date_field] = None
                    else:
                        trade[date_field] = parsed.isoformat()

            if metadata:
                trade["_metadata"] = metadata

            # Minimal validation: require symbol or entry_price
            if trade.get("symbol") or trade.get("entry_price"):
                normalized_rows.append(trade)
            else:
                logger.debug(
                    "Dropping row %s: missing symbol & entry_price; trade_preview=%s; metadata_keys=%s",
                    idx,
                    {k: trade.get(k) for k in ("symbol", "entry_price", "direction")},
                    list(metadata.keys()),
                )

        return normalized_rows


# Single instance
csv_parser = CSVParser()


