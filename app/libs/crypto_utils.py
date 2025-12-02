# backend/app/libs/crypto_utils.py

import os
import base64
from cryptography.fernet import Fernet
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

# --- Configuration (Security/Privacy) ---
try:
    ENCRYPTION_KEY_BASE64 = os.getenv("ENCRYPTION_KEY")
    if not ENCRYPTION_KEY_BASE64:
        raise ValueError("ENCRYPTION_KEY environment variable is not set.")
    
    # Initialize Fernet (AES-256 GCM) with the master key
    FERNET_KEY = ENCRYPTION_KEY_BASE64.encode()
    FERNET_CIPHER = Fernet(FERNET_KEY)
    logger.info("CryptoUtils: Fernet cipher initialized.")

except Exception as e:
    logger.error(f"FATAL: Failed to initialize Cryptographic Utilities. All sensitive data is unprotected: {e}")
    # Setting to None means encryption/decryption functions will fail safely
    FERNET_CIPHER = None
    raise RuntimeError("Encryption key failure.") from e

# --- Core Logic (Modular & Secure) ---

def encrypt_data(data: str) -> str:
    """
    Encrypts a plaintext string using the application's master key (Policy 1.B/1.C).
    
    Raises:
        HTTPException or RuntimeError if encryption fails (Non-breakable).
    """
    if not FERNET_CIPHER:
        logger.error("Encryption failed: Cipher not initialized.")
        raise RuntimeError("Data encryption service unavailable.")
        
    try:
        data_bytes = data.encode('utf-8')
        encrypted_bytes = FERNET_CIPHER.encrypt(data_bytes)
        # Store as standard string in Postgres TEXT column
        return encrypted_bytes.decode('utf-8')
    except Exception as e:
        logger.error(f"Encryption error: {e}")
        raise RuntimeError("Data encryption failed during processing.") from e


def decrypt_data(encrypted_data: str) -> str:
    """
    Decrypts a base64 encoded string back into plaintext for display or processing.
    
    Returns:
        The plaintext string or a clear error message if decryption fails.
    """
    if not FERNET_CIPHER:
        return "[DECRYPTION_ERROR: Service Unavailable]" 

    try:
        encrypted_bytes = encrypted_data.encode('utf-8')
        decrypted_bytes = FERNET_CIPHER.decrypt(encrypted_bytes)
        return decrypted_bytes.decode('utf-8')
    except Exception as e:
        logger.error(f"Decryption error (data corruption or key mismatch): {e}")
        # Robustly return an error message instead of crashing
        return f"[DECRYPTION_ERROR: Data Corrupted or Key Mismatch]"