# backend/app/lib/llm_client.py
import httpx
from typing import List, Dict, Any, Optional
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import logging
import json
import time

from app.core.config import settings

logger = logging.getLogger(__name__)

class LLMClient:
    """
    Async client for orchestrating calls to multiple LLM providers.
    
    Supported Providers:
    1. OpenAI (Reasoning, General)
    2. Perplexity (Real-time Market Data, News)
    3. Gemini (High Context Window, Multimodal Fallback)
    
    Design:
    - Uses REST APIs directly (no heavy SDKs).
    - Unified 'generate_response' interface.
    - Automatic retries for network stability.
    """
    
    def __init__(self):
        # Shared async client for connection pooling
        self.client = httpx.AsyncClient(timeout=60.0)
        
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
        model: str = "gpt-4-turbo", 
        provider: str = "openai",
        temperature: float = 0.7,
        max_tokens: int = 1000,
        response_format: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Main orchestration method. Routes request to the specific provider handler.
        """
        start_time = time.time()
        
        try:
            if provider == "openai":
                response = await self._call_openai(messages, model, temperature, max_tokens, response_format)
            elif provider == "perplexity":
                response = await self._call_perplexity(messages, model, temperature, max_tokens)
            elif provider == "gemini":
                response = await self._call_gemini(messages, model, temperature, max_tokens)
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
        # Fallback to a valid sonar model if user selects generic GPT model
        pplx_model = model if "sonar" in model else "sonar-medium-online"
        
        payload = {
            "model": pplx_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        resp = await self.client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()

        return {
            "content": data["choices"][0]["message"]["content"],
            "usage": data.get("usage", {}),
            "model": data.get("model")
        }

    async def _call_gemini(self, messages, model, temperature, max_tokens):
        """
        Maps OpenAI-style messages to Gemini's 'contents' format.
        """
        # Default to Gemini 1.5 Flash if generic model passed
        gemini_model = "gemini-2.5-flash" if "gpt" in model or "sonar" in model else model
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent?key={settings.GEMINI_API_KEY}"
        
        # Transform messages:
        # 1. System prompts -> 'system_instruction' (or prepend to first user message for compatibility)
        # 2. User -> 'user', Assistant -> 'model'
        
        contents = []
        system_instruction = None
        
        for msg in messages:
            role = msg["role"]
            content = msg["content"]
            
            if role == "system":
                # Save system prompt to set context efficiently
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

        resp = await self.client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        
        # Extract text from Gemini structure
        try:
            content = data["candidates"][0]["content"]["parts"][0]["text"]
            # Estimate usage (Gemini response metadata varies)
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