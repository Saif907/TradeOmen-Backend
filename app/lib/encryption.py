# backend/app/lib/encryption.py
from cryptography.fernet import Fernet
import logging
from app.core.config import settings

logger = logging.getLogger(__name__)

class EncryptionHandler:
    def __init__(self):
        # Fernet requires a 32-byte URL-safe base64-encoded key
        try:
            self.cipher_suite = Fernet(settings.ENCRYPTION_KEY)
        except Exception as e:
            logger.critical(f"Failed to initialize EncryptionHandler. Check ENCRYPTION_KEY in .env: {e}")
            raise e

    def encrypt(self, plain_text: str) -> str:
        """
        Encrypts a string using AES-256.
        Returns: URL-safe base64-encoded bytes as a string.
        """
        if not plain_text:
            return ""
        try:
            # Fernet encrypt expects bytes, returns bytes
            encrypted_bytes = self.cipher_suite.encrypt(plain_text.encode("utf-8"))
            return encrypted_bytes.decode("utf-8")
        except Exception as e:
            logger.error(f"Encryption failed: {e}")
            # In a strict system, raise error. For robustness, maybe return None?
            # We'll raise to ensure no unencrypted data leaks silently.
            raise e

    def decrypt(self, encrypted_text: str) -> str:
        """
        Decrypts a string.
        Returns: The original plain text string.
        """
        if not encrypted_text:
            return ""
        try:
            decrypted_bytes = self.cipher_suite.decrypt(encrypted_text.encode("utf-8"))
            return decrypted_bytes.decode("utf-8")
        except Exception as e:
            logger.error(f"Decryption failed: {e}")
            # Return original text if decryption fails (e.g. if data wasn't encrypted)
            # This is useful during migration or testing phases.
            return "[Decryption Error]"

# Singleton instance
crypto = EncryptionHandler()