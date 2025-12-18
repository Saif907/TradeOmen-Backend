# backend/app/services/tagging_service.py
import logging
import json
from typing import Dict, Any, List

from app.lib.llm_client import llm_client
from app.lib.data_sanitizer import sanitizer
from app.services.quota_manager import QuotaManager
from app.services.metrics_engine import MetricsEngine

logger = logging.getLogger(__name__)

class TaggingService:
    """
    Service to handle AI-powered enrichment of trade data.
    """

    @staticmethod
    async def analyze_trade_notes(user_id: str, notes: str) -> Dict[str, List[str]]:
        """
        Uses AI to extract technical tags and psychological mistakes from trade notes.
        Updates user quota (tokens) and logs telemetry.
        """
        if not notes or len(notes) < 5:
            return {"tags": [], "mistakes": []}

        system_prompt = """
        You are a Trading Psychology Coach.
        Analyze the trader's notes.
        Extract:
        1. "tags": Technical keywords (e.g., 'Breakout', 'Reversal', 'EMA Cross').
        2. "mistakes": Psychological errors (e.g., 'FOMO', 'Revenge Trading', 'Overtrading', 'Hesitation').
        
        Return JSON: {"tags": ["..."], "mistakes": ["..."]}
        """
        
        try:
            # 1. Sanitize PII
            safe_notes = sanitizer.sanitize(notes)
            
            # 2. Call LLM
            response = await llm_client.generate_response(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": safe_notes}
                ],
                model="gemini-2.5-flash",
                provider="gemini",
                response_format={"type": "json_object"}
            )
            
            content = response.get("content", "{}")
            usage = response.get("usage", {})
            total_tokens = usage.get("total_tokens", 0)
            
            # 3. Parse JSON
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                # Fallback if strict JSON fails
                return {"tags": [], "mistakes": []}
                
            tags = [t.upper() for t in data.get("tags", [])]
            mistakes = [m.upper() for m in data.get("mistakes", [])]

            # 4. Update Quota (Tokens)
            # We track this as 'chat_message' type metric to pool token usage
            if total_tokens > 0:
                await QuotaManager.increment_usage(
                    user_id=user_id, 
                    metric_type="chat_message", 
                    extra_data={"tokens": total_tokens, "needs_reset": False}
                )
            
            # 5. Log Telemetry
            await MetricsEngine.log_telemetry(
                user_id=user_id,
                event_type="AI_TAGGING_SUCCESS",
                details={"tags_extracted": len(tags), "mistakes_extracted": len(mistakes)}
            )
            
            return {"tags": tags, "mistakes": mistakes}

        except Exception as e:
            logger.error(f"Tagging service failed: {e}")
            await MetricsEngine.log_telemetry(
                user_id=user_id,
                event_type="AI_TAGGING_ERROR",
                category="ERROR",
                details={"error": str(e)}
            )
            # Fail gracefully - do not block trade creation
            return {"tags": [], "mistakes": []}