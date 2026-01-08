# backend/app/lib/llm_client.py
import logging
import time
import json
from typing import List, Dict, Any, Optional, AsyncIterator, Tuple

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

from app.core.config import settings
from app.lib.data_sanitizer import sanitizer

logger = logging.getLogger("tradeomen.llm_client")


# -------------------------------------------------------------------
# Exceptions
# -------------------------------------------------------------------
class LLMError(Exception):
    pass


class ProviderError(LLMError):
    pass


class RateLimitError(LLMError):
    def __init__(self, retry_after: Optional[int] = None):
        self.retry_after = retry_after
        super().__init__("Rate limited by provider")


# -------------------------------------------------------------------
# Retry predicate (NON-STREAM ONLY)
# -------------------------------------------------------------------
def _should_retry(exc: Exception) -> bool:
    if isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return False


# -------------------------------------------------------------------
# LLM Client
# -------------------------------------------------------------------
class LLMClient:
    def __init__(self):
        timeout = getattr(settings, "LLM_REQUEST_TIMEOUT_SECONDS", 60.0)
        self.client = httpx.AsyncClient(timeout=timeout, trust_env=True)

    async def close(self):
        await self.client.aclose()

    # ===============================================================
    # NON-STREAMING (SAFE TO RETRY)
    # ===============================================================
    @retry(
        retry=retry_if_exception(_should_retry),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
    )
    async def generate_response(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        provider: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 1000,
        response_format: Optional[Dict] = None,
    ) -> Dict[str, Any]:

        start = time.time()
        safe_messages = self._sanitize(messages)

        provider = (provider or settings.LLM_PROVIDER).lower()
        model = model or settings.LLM_MODEL

        try:
            if provider == "openai":
                res = await self._call_openai(safe_messages, model, temperature, max_tokens, response_format)
            elif provider == "gemini":
                res = await self._call_gemini(safe_messages, model, temperature, max_tokens)
            else:
                raise LLMError(f"Unsupported provider: {provider}")

            return {
                "content": res["content"],
                "usage": self._normalize_usage(res.get("usage")),
                "model": res.get("model", model),
                "provider": provider,
                "duration": time.time() - start,
            }

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                raise RateLimitError(self._retry_after(e.response))
            raise ProviderError(f"Provider error {e.response.status_code}")
        except Exception as e:
            logger.exception("LLM error")
            raise LLMError(str(e))

    # ===============================================================
    # STREAMING (NO RETRIES)
    # ===============================================================
    async def stream_response(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        provider: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 1000,
    ) -> AsyncIterator[Tuple[str, Dict[str, Any]]]:

        safe_messages = self._sanitize(messages)
        provider = (provider or settings.LLM_PROVIDER).lower()
        model = model or settings.LLM_MODEL

        if provider == "openai":
            async for chunk in self._stream_openai(safe_messages, model, temperature, max_tokens):
                yield chunk, {"event": "delta"}
            yield "", {"event": "done"}
            return

        if provider == "gemini":
            async for chunk in self._stream_gemini(safe_messages, model, temperature, max_tokens):
                yield chunk, {"event": "delta"}
            yield "", {"event": "done"}
            return

        # fallback (single chunk)
        res = await self.generate_response(messages, model=model, provider=provider)
        yield res["content"], {"event": "final", "usage": res["usage"]}

    # ===============================================================
    # PROVIDERS
    # ===============================================================
    async def _call_openai(self, messages, model, temperature, max_tokens, response_format):
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {settings.OPENAI_API_KEY}"}
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            payload.update(response_format)

        r = await self.client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        j = r.json()
        return {
            "content": j["choices"][0]["message"]["content"],
            "usage": j.get("usage", {}),
            "model": j.get("model", model),
        }

    async def _call_gemini(self, messages, model, temperature, max_tokens):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={settings.GEMINI_API_KEY}"

        contents, system = [], None
        for m in messages:
            if m["role"] == "system":
                system = {"parts": [{"text": m["content"]}]}
            elif m["role"] == "user":
                contents.append({"role": "user", "parts": [{"text": m["content"]}]})
            elif m["role"] == "assistant":
                contents.append({"role": "model", "parts": [{"text": m["content"]}]})

        payload = {"contents": contents, "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens}}
        if system:
            payload["systemInstruction"] = system

        r = await self.client.post(url, json=payload)
        r.raise_for_status()
        j = r.json()

        text = j["candidates"][0]["content"]["parts"][0]["text"]
        usage = j.get("usageMetadata", {})
        return {"content": text, "usage": usage, "model": model}

    # ===============================================================
    # STREAMING PROVIDERS
    # ===============================================================
    async def _stream_openai(self, messages, model, temperature, max_tokens):
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {settings.OPENAI_API_KEY}"}
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }

        async with self.client.stream("POST", url, headers=headers, json=payload) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                j = json.loads(data)
                delta = j["choices"][0]["delta"].get("content")
                if delta:
                    yield delta

    async def _stream_gemini(self, messages, model, temperature, max_tokens):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?key={settings.GEMINI_API_KEY}"

        contents, system = [], None
        for m in messages:
            if m["role"] == "system":
                system = {"parts": [{"text": m["content"]}]}
            elif m["role"] == "user":
                contents.append({"role": "user", "parts": [{"text": m["content"]}]})
            elif m["role"] == "assistant":
                contents.append({"role": "model", "parts": [{"text": m["content"]}]})

        payload = {"contents": contents, "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens}}
        if system:
            payload["systemInstruction"] = system

        async with self.client.stream("POST", url, json=payload) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    parts = data["candidates"][0]["content"]["parts"]
                    for p in parts:
                        if "text" in p:
                            yield p["text"]
                except Exception:
                    continue

    # ===============================================================
    # HELPERS
    # ===============================================================
    def _sanitize(self, messages):
        out = []
        for m in messages:
            m2 = dict(m)
            if isinstance(m2.get("content"), str) and getattr(settings, "SANITIZE_PII", True):
                m2["content"] = sanitizer.sanitize(m2["content"])
            out.append(m2)
        return out

    def _normalize_usage(self, usage: Dict[str, Any]) -> Dict[str, int]:
        if not usage:
            return {"total_tokens": 0}
        if "totalTokenCount" in usage:
            return {"total_tokens": int(usage["totalTokenCount"])}
        if "total_tokens" in usage:
            return {"total_tokens": int(usage["total_tokens"])}
        return {"total_tokens": 0}

    def _retry_after(self, response: httpx.Response) -> Optional[int]:
        try:
            return int(response.headers.get("Retry-After"))
        except Exception:
            return None


# Singleton
llm_client = LLMClient()
