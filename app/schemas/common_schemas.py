from enum import Enum

class UserRole(str, Enum):
    USER = "user"
    SUPPORT = "support"
    ADMIN = "admin"
    SUPER_ADMIN = "super_admin"

class PlanTier(str, Enum):
    FREE = "FREE"
    PRO = "PRO"
    FOUNDER = "FOUNDER"

class InstrumentType(str, Enum):
    STOCK = "STOCK"
    CRYPTO = "CRYPTO"
    FOREX = "FOREX"
    FUTURES = "FUTURES"

class TradeSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"

class TradeStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"