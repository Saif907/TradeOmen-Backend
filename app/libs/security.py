import base64
from passlib.context import CryptContext
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.backends import default_backend

from ..libs.config import settings
from ..libs import schemas

# --- 1. Password Hashing ---
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Checks a plain password against its hash."""
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    """Generates a secure hash for a given password."""
    return pwd_context.hash(password)


# --- 2. Symmetric Encryption (AES-256 for Sensitive Data like Notes) ---
fernet_cipher = None
try:
    # Key must be 44-character Base64 string (32 bytes)
    fernet_key_bytes = settings.DATA_ENCRYPTION_KEY.encode()
    if len(fernet_key_bytes) != 44:
        raise ValueError("DATA_ENCRYPTION_KEY must be a 44-character Base64 string.")
        
    fernet_cipher = Fernet(fernet_key_bytes)
except Exception as e:
    print(f"FATAL SECURITY ERROR: Failed to initialize Fernet cipher. {e}")
    fernet_cipher = None

def encrypt_data(data: str) -> str:
    """Encrypts plaintext data into a secure Base64 encoded string (JSON-safe)."""
    if not fernet_cipher or not data:
        return "" 
        
    # Encrypt returns bytes, we encode those bytes to a Base64 string
    encrypted_bytes = fernet_cipher.encrypt(data.encode())
    return base64.urlsafe_b64encode(encrypted_bytes).decode('utf-8')

def decrypt_data(encrypted_data_str: str | None) -> str:
    """Decrypts a Base64 string from the DB back into readable plaintext."""
    if not fernet_cipher or not encrypted_data_str:
        return ""
    try:
        # Decode Base64 string back to bytes before decrypting
        encrypted_bytes = base64.urlsafe_b64decode(encrypted_data_str.encode('utf-8'))
        return fernet_cipher.decrypt(encrypted_bytes).decode()
    except Exception:
        # Failsafe for corrupted data or wrong key
        return "Decryption Error: Data may be corrupted or key is incorrect."


# --- 3. JWT and Microservice Authentication ---

def validate_ai_service_secret(secret_key: str) -> bool:
    """Validates the shared secret key used for Microservice-to-Microservice communication."""
    return secret_key == settings.AI_SERVICE_SECRET_KEY