# backend/app/lib/brokers/factory.py
from typing import Dict
from app.lib.brokers.base import BrokerAdapter
from app.lib.brokers.dhan import DhanAdapter
from app.lib.brokers.binance import BinanceAdapter

def get_broker_adapter(broker_name: str, credentials: Dict[str, str]) -> BrokerAdapter:
    """
    Factory function to instantiate the correct broker adapter.
    """
    name = broker_name.lower().strip()
    
    if "dhan" in name:
        return DhanAdapter(credentials)
    
    elif "binance" in name:
        return BinanceAdapter(credentials)
        
    # Future Placeholders (easy to extend)
    # elif "zerodha" in name:
    #     return ZerodhaAdapter(credentials)
    # elif "angel" in name:
    #     return AngelOneAdapter(credentials)
        
    else:
        # Fallback error for unsupported brokers
        raise ValueError(f"Broker '{broker_name}' is not yet supported for auto-sync.")