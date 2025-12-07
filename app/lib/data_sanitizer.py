# backend/app/lib/data_sanitizer.py
import re
import logging

logger = logging.getLogger(__name__)

class DataSanitizer:
    """
    Scrubs sensitive PII from text before it is sent to external LLMs.
    """
    def __init__(self):
        # Pre-compile regex patterns for performance
        self.patterns = {
            "email": re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'),
            "phone": re.compile(r'\b(\+\d{1,2}\s?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b'),
            "credit_card": re.compile(r'\b(?:\d{4}[-\s]?){3}\d{4}\b'),
            "crypto_address": re.compile(r'\b(0x[a-fA-F0-9]{40}|bc1[a-zA-Z0-9]{25,39}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})\b')
        }

    def sanitize(self, text: str) -> str:
        """
        Replaces PII with safe placeholders.
        """
        if not text:
            return ""
            
        cleaned_text = text
        try:
            for key, pattern in self.patterns.items():
                replacement = f"[{key.upper()}_REDACTED]"
                cleaned_text = pattern.sub(replacement, cleaned_text)
            
            return cleaned_text
        except Exception as e:
            logger.error(f"Sanitization error: {e}")
            # Fail closed: if sanitization fails, return a generic error message 
            # rather than risking leaking raw data.
            return "[CONTENT_REDACTED_DUE_TO_ERROR]"

# Singleton instance
sanitizer = DataSanitizer()