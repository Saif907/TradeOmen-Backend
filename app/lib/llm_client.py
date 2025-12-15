# backend/app/lib/llm_client.py
import httpx
from typing import List, Dict, Any, Optional
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import logging
import time
import json

from app.core.config import settings
from app.lib.data_sanitizer import sanitizer 

logger = logging.getLogger(__name__)

class LLMClient:
    """
    Async client for orchestrating calls to multiple LLM providers.
    Includes automatic PII sanitization (Privacy by Design).
    """
    
    def __init__(self):
        # Shared async client for connection pooling
        # Explicitly trust_env=False to ignore potentially malformed system proxy settings during dev
        self.client = httpx.AsyncClient(timeout=60.0, trust_env=True)
        
    async def close(self):
        await self.client.aclose()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.ConnectError, httpx.ReadTimeout, httpx.HTTPStatusError))
    )
    async def generate_response(
        self, 
        messages: List[Dict[str, str]], 
        model: str = "gemini-2.5-flash", 
        provider: str = "gemini",
        temperature: float = 0.7,
        max_tokens: int = 1000,
        response_format: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Main orchestration method. Routes request to the specific provider handler.
        """
        start_time = time.time()
        
        # ✅ PRIVACY STEP: Sanitize inputs
        safe_messages = []
        for msg in messages:
            clean_msg = msg.copy()
            if "content" in clean_msg and isinstance(clean_msg["content"], str):
                clean_msg["content"] = sanitizer.sanitize(clean_msg["content"])
            safe_messages.append(clean_msg)

        try:
            if provider == "openai":
                response = await self._call_openai(safe_messages, model, temperature, max_tokens, response_format)
            elif provider == "perplexity":
                response = await self._call_perplexity(safe_messages, model, temperature, max_tokens)
            elif provider == "gemini":
                response = await self._call_gemini(safe_messages, model, temperature, max_tokens)
            else:
                raise ValueError(f"Unsupported LLM provider: {provider}")

            duration = time.time() - start_time
            
            return {
                "content": response.get("content", ""),
                "usage": response.get("usage", {}),
                "model": response.get("model", model),
                "duration": duration,
                "provider": provider
            }

        except httpx.UnsupportedProtocol as e:
            logger.critical(f"🚨 Protocol Error calling {provider}: {str(e)}")
            raise HTTPException(status_code=500, detail="Internal Configuration Error: Invalid API URL")
        except httpx.HTTPStatusError as e:
            logger.error(f"LLM Provider Error ({provider}): {e.response.status_code} - {e.response.text}")
            raise e
        except Exception as e:
            logger.error(f"LLM Internal Error: {str(e)}")
            raise e

    # --- Provider Implementations ---

    async def _call_openai(self, messages, model, temperature, max_tokens, response_format):
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format

        resp = await self.client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        
        return {
            "content": data["choices"][0]["message"]["content"],
            "usage": data.get("usage", {}),
            "model": data.get("model")
        }

    async def _call_perplexity(self, messages, model, temperature, max_tokens):
        url = "https://api.perplexity.ai/chat/completions"
        headers = {
            "Authorization": f"Bearer {settings.PERPLEXITY_API_KEY}",
            "Content-Type": "application/json"
        }
        
        # ✅ Logic to handle 'sonar' model request
        if model == "sonar":
            pplx_model = "sonar" # Use the generic latest model
        elif "sonar" in model:
            pplx_model = model
        else:
            pplx_model = "sonar-medium-online" # Fallback
            
        payload = {
            "model": pplx_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        logger.info(f"📤 Calling Perplexity API: {url} with model {pplx_model}")
        resp = await self.client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()

        return {
            "content": data["choices"][0]["message"]["content"],
            "usage": data.get("usage", {}),
            "model": data.get("model")
        }

    async def _call_gemini(self, messages, model, temperature, max_tokens):
        gemini_model = "gemini-2.5-flash" if "gpt" in model or "sonar" in model else model
        
        # Ensure Key is present
        api_key = settings.GEMINI_API_KEY
        if not api_key:
            logger.error("❌ GEMINI_API_KEY is missing!")
            raise ValueError("GEMINI_API_KEY is not set")

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent?key={api_key}"
        
        # Clean URL (just in case of whitespace injection)
        url = url.strip()

        contents = []
        system_instruction = None
        
        for msg in messages:
            role = msg["role"]
            content = msg["content"]
            
            if role == "system":
                system_instruction = {"parts": [{"text": content}]}
            elif role == "user":
                contents.append({"role": "user", "parts": [{"text": content}]})
            elif role == "assistant":
                contents.append({"role": "model", "parts": [{"text": content}]})

        payload = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens
            }
        }
        
        if system_instruction:
            payload["systemInstruction"] = system_instruction

        logger.info(f"📤 Calling Gemini API: {url.split('?')[0]}...") # Log without key
        
        resp = await self.client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        
        try:
            content = data["candidates"][0]["content"]["parts"][0]["text"]
            usage = {"total_tokens": data.get("usageMetadata", {}).get("totalTokenCount", 0)}
        except (KeyError, IndexError):
            content = ""
            usage = {}

        return {
            "content": content,
            "usage": usage,
            "model": gemini_model
        }

# Singleton instance
llm_client = LLMClient()