# backend/app/lib/csv_parser.py
import pandas as pd
import io
import json
from typing import Dict, List, Any
from app.lib.llm_client import llm_client

# The "Ideal" Schema we want to map TO
TARGET_SCHEMA = {
    "symbol": "The asset ticker (e.g., AAPL, NIFTY)",
    "direction": "LONG or SHORT",
    "entry_date": "Date/Time of entry",
    "entry_price": "Price at entry",
    "exit_date": "Date/Time of exit (optional)",
    "exit_price": "Price at exit (optional)",
    "quantity": "Number of units/shares",
    "pnl": "Profit or Loss (optional)",
    "fees": "Commissions or fees (optional)",
    "notes": "Text notes (optional)"
}

class CSVParser:
    @staticmethod
    def read_headers(file_content: bytes) -> List[str]:
        """Reads just the headers from a CSV byte stream."""
        try:
            # Read only the first few lines to get headers/sample
            df = pd.read_csv(io.BytesIO(file_content), nrows=5)
            return list(df.columns)
        except Exception as e:
            raise ValueError(f"Failed to parse CSV: {str(e)}")

    @staticmethod
    async def guess_mapping(headers: List[str], user_prompt: str = "") -> Dict[str, str]:
        """
        Uses LLM to map User Headers -> Target Schema.
        """
        system_prompt = f"""
        You are a Data Mapping Specialist.
        Map the user's CSV headers to our Target Schema.
        
        Target Schema: {json.dumps(TARGET_SCHEMA)}
        User Headers: {json.dumps(headers)}
        User Context: {user_prompt}
        
        Rules:
        1. Return JSON ONLY: {{ "target_field": "user_header" }}
        2. If no match found for a target field, do not include it in the keys.
        3. Use the User Context to resolve ambiguities (e.g., if user says "Date is entry time").
        """
        
        response = await llm_client.generate_response(
            messages=[{"role": "system", "content": system_prompt}],
            model="gemini-2.5-pro", # Use a smart model for this logic
            provider="gemini",
            response_format={"type": "json_object"}
        )
        
        try:
            return json.loads(response["content"])
        except:
            return {}

    @staticmethod
    def process_and_normalize(file_content: bytes, mapping: Dict[str, str]) -> List[Dict[str, Any]]:
        """
        Converts the CSV into a list of standardized Trade objects based on the mapping.
        """
        df = pd.read_csv(io.BytesIO(file_content))
        
        normalized_data = []
        
        for _, row in df.iterrows():
            trade = {}
            metadata = {}
            
            # 1. Extract Mapped Fields
            for target_field, csv_header in mapping.items():
                if csv_header in row:
                    val = row[csv_header]
                    # Basic cleaning
                    if isinstance(val, str):
                        val = val.strip()
                    trade[target_field] = val
            
            # 2. Store Unmapped Fields in Metadata (JSONB)
            mapped_headers = set(mapping.values())
            for header in df.columns:
                if header not in mapped_headers:
                    # Convert pandas types to python native for JSON serialization
                    val = row[header]
                    if pd.isna(val): continue
                    metadata[header] = str(val)
            
            # 3. Post-Processing / Normalization
            # (Example: Normalize Direction to LONG/SHORT)
            if "direction" in trade:
                d = str(trade["direction"]).upper()
                if d in ["BUY", "B", "LONG"]: trade["direction"] = "LONG"
                elif d in ["SELL", "S", "SHORT"]: trade["direction"] = "SHORT"
            
            # (Example: Clean Currency)
            for price_field in ["entry_price", "exit_price", "pnl", "fees"]:
                if price_field in trade:
                    try:
                        # Remove currency symbols if present
                        if isinstance(trade[price_field], str):
                            clean = trade[price_field].replace('$', '').replace('₹', '').replace(',', '')
                            trade[price_field] = float(clean)
                    except:
                        pass # Keep original if parse fails
            
            trade["metadata"] = metadata
            normalized_data.append(trade)
            
        return normalized_data

csv_parser = CSVParser()