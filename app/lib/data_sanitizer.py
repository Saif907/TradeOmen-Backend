# backend/app/lib/data_sanitizer.py
import re
import logging
from typing import Pattern, Dict, Callable

logger = logging.getLogger(__name__)

# small helper lists for context detection
_TRADE_HINTS = [
    "pnl", "profit", "loss", "price", "avg", "average", "qty", "quantity", "shares",
    "lot", "lots", "volume", "entry", "exit", "sl", "tp", "leverage", "rate", "orders",
    "₹", "rs", "rs.", "inr", "$", "usd", "eur", "%"
]

# window (characters) to inspect around a numeric match for trade context
_CONTEXT_WINDOW = 40


class DataSanitizer:
    """
    Context-aware PII sanitizer that avoids redacting trading numbers.
    Conservative rules:
      - Strong identifier patterns (email, PAN, credit cards, crypto, IPs, IFSC) are redacted.
      - Account numbers and phones are redacted only when contextual hints exist (labels or international format).
      - Numeric matches are examined for nearby trading keywords or currency symbols; if found, they are preserved.
    """

    def __init__(self, enable_map: Dict[str, bool] = None):
        defaults = {
            "email": True,
            "phone": True,
            "credit_card": True,
            "crypto_address": True,
            "ipv4": True,
            "ipv6": True,
            "pan": True,
            "aadhaar": True,
            "ifsc": True,
            "account_contextual": True,
        }
        self.enabled = {**defaults, **(enable_map or {})}

        # precompiled patterns
        self.patterns: Dict[str, Pattern] = {
            "email": re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b", re.IGNORECASE),
            # labeled phone (phone: 123...), captures the number group
            "phone_labeled": re.compile(r"(?:(?:phone|tel|mobile|contact|m:|ph)\s*[:\-]\s*)(\+?\d[\d\s\-\(\)\.]{6,}\d)", re.IGNORECASE),
            # international-ish phone with + and separators (not plain small numbers)
            "phone_international": re.compile(r"\+\d{1,3}[\s\-]?\(?\d{1,4}\)?(?:[\s\-.]?\d{2,4}){2,}", re.IGNORECASE),
            # credit card-like groups
            "credit_card": re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
            "crypto_address": re.compile(r"\b(0x[a-fA-F0-9]{40}|bc1[a-zA-Z0-9]{25,39}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})\b"),
            "ipv4": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
            "ipv6": re.compile(r"\b([0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}\b"),
            "pan": re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b", re.IGNORECASE),
            "aadhaar": re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"),
            "ifsc": re.compile(r"\b[A-Za-z]{4}0[A-Za-z0-9]{6}\b", re.IGNORECASE),
            # account contextual: label followed by 6-20 digits
            "account_contextual": re.compile(r"(?:(?:account|acct|a/c|a c|acc|account number)\s*[:#\-\s]{0,3})(\d{6,20})", re.IGNORECASE),
        }

        self.placeholders = {
            "email": "[EMAIL_REDACTED]",
            "phone": "[PHONE_REDACTED]",
            "credit_card": "[CREDIT_CARD_REDACTED]",
            "crypto_address": "[CRYPTO_REDACTED]",
            "ipv4": "[IPV4_REDACTED]",
            "ipv6": "[IPV6_REDACTED]",
            "pan": "[PAN_REDACTED]",
            "aadhaar": "[AADHAAR_REDACTED]",
            "ifsc": "[IFSC_REDACTED]",
            "account_contextual": "[ACCOUNT_REDACTED]",
        }

    # ---------- utilities ----------
    def _nearby_text(self, text: str, start: int, end: int, window: int = _CONTEXT_WINDOW) -> str:
        s = max(0, start - window)
        e = min(len(text), end + window)
        return text[s:e].lower()

    def _has_trade_hint(self, nearby: str) -> bool:
        for hint in _TRADE_HINTS:
            if hint in nearby:
                return True
        return False

    # ---------- main sanitize ----------
    def sanitize(self, text: str) -> str:
        if not text:
            return ""

        cleaned = text
        try:
            # 1) Strong identifiers (safe to redact unconditionally)
            if self.enabled.get("email"):
                cleaned = self.patterns["email"].sub(self.placeholders["email"], cleaned)

            if self.enabled.get("pan"):
                cleaned = self.patterns["pan"].sub(self.placeholders["pan"], cleaned)

            if self.enabled.get("aadhaar"):
                # But avoid redacting if Aadhaar-like sequence appears next to currency or 'pnl' - extremely rare, still check
                def _aadhaar_repl(m):
                    start, end = m.span()
                    nearby = self._nearby_text(cleaned, start, end)
                    if self._has_trade_hint(nearby):
                        return m.group(0)  # keep as-is
                    return self.placeholders["aadhaar"]
                cleaned = self.patterns["aadhaar"].sub(_aadhaar_repl, cleaned)

            if self.enabled.get("ifsc"):
                cleaned = self.patterns["ifsc"].sub(self.placeholders["ifsc"], cleaned)

            if self.enabled.get("credit_card"):
                cleaned = self.patterns["credit_card"].sub(self.placeholders["credit_card"], cleaned)

            if self.enabled.get("crypto_address"):
                cleaned = self.patterns["crypto_address"].sub(self.placeholders["crypto_address"], cleaned)

            if self.enabled.get("ipv4"):
                cleaned = self.patterns["ipv4"].sub(self.placeholders["ipv4"], cleaned)

            if self.enabled.get("ipv6"):
                cleaned = self.patterns["ipv6"].sub(self.placeholders["ipv6"], cleaned)

            # 2) Account contextual: redact only when label present
            if self.enabled.get("account_contextual"):
                def _acct_repl(m):
                    # keep label but replace number
                    full = m.group(0)
                    # find digits in match and replace them
                    return re.sub(r"\d{6,20}", self.placeholders["account_contextual"], full)
                cleaned = self.patterns["account_contextual"].sub(_acct_repl, cleaned)

            # 3) Phone redaction - conservative:
            if self.enabled.get("phone"):
                # labeled phones (phone:, tel:, contact:) - safe to redact
                def _labeled_phone_repl(m):
                    start, end = m.span(1)  # group(1) is the captured number
                    nearby = self._nearby_text(cleaned, start, end)
                    # if nearby has trade hint (rare for labeled phones) keep it
                    if self._has_trade_hint(nearby):
                        return m.group(0)
                    # replace only the number part within the full match
                    return m.group(0).replace(m.group(1), self.placeholders["phone"])
                cleaned = self.patterns["phone_labeled"].sub(_labeled_phone_repl, cleaned)

                # international formatted phones with + are often real phones; redact but ensure not currency proximity
                def _intl_phone_repl(m):
                    start, end = m.span()
                    nearby = self._nearby_text(cleaned, start, end)
                    if self._has_trade_hint(nearby):
                        return m.group(0)
                    return self.placeholders["phone"]
                cleaned = self.patterns["phone_international"].sub(_intl_phone_repl, cleaned)

            # 4) Final: avoid any generic digit redaction. We intentionally do NOT remove standalone numbers that look like PnL/prices/quantities.
            # This sanitizer is conservative: it only removes numbers when a clear PII pattern or contextual label is present.

            return cleaned

        except Exception as e:
            logger.exception("Sanitization error")
            # fail-closed: redact everything if unexpected error
            return "[CONTENT_REDACTED_DUE_TO_ERROR]"


# singleton
sanitizer = DataSanitizer()
