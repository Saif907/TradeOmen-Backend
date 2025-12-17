from cryptography.fernet import Fernet, InvalidToken
import logging
from app.core.config import settings

logger = logging.getLogger(__name__)


class EncryptionHandler:
    """
    Handles encryption / decryption of sensitive text fields.
    Designed to be tolerant of legacy plaintext data.
    """

    def __init__(self):
        try:
            self.cipher_suite = Fernet(settings.ENCRYPTION_KEY)
        except Exception as e:
            logger.critical(
                "Failed to initialize EncryptionHandler. "
                "Check ENCRYPTION_KEY in environment variables."
            )
            raise e

    def encrypt(self, plain_text: str) -> str:
        """
        Encrypts a string using Fernet (AES-256).
        Always returns encrypted text or empty string.
        """
        if not plain_text:
            return ""

        try:
            encrypted_bytes = self.cipher_suite.encrypt(
                plain_text.encode("utf-8")
            )
            return encrypted_bytes.decode("utf-8")
        except Exception as e:
            # This IS a real error — never silently fail encryption
            logger.error(f"Encryption failed: {e}")
            raise e

    def decrypt(self, encrypted_text: str) -> str:
        """
        Decrypts a string.

        Behavior:
        - If value is encrypted → decrypt and return plaintext
        - If value is plaintext → return as-is
        - If value is invalid / corrupted → return as-is

        Decryption failure is NOT an error condition.
        """
        if not encrypted_text:
            return ""

        # Fast-path heuristic:
        # Fernet tokens always start with "gAAAAA"
        if not encrypted_text.startswith("gAAAAA"):
            return encrypted_text

        try:
            decrypted_bytes = self.cipher_suite.decrypt(
                encrypted_text.encode("utf-8")
            )
            return decrypted_bytes.decode("utf-8")

        except InvalidToken:
            # Expected during migration or legacy rows
            logger.debug("Decrypt skipped: value is not a valid Fernet token")
            return encrypted_text

        except Exception as e:
            # Unexpected but non-fatal
            logger.warning(f"Decrypt fallback used due to error: {e}")
            return encrypted_text


# Singleton instance
crypto = EncryptionHandler()
