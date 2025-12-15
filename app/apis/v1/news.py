import logging
import httpx
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Optional

from app.core.config import settings
from app.auth.dependency import get_current_user

router = APIRouter()
logger = logging.getLogger(__name__)

# --- Schemas ---
class NewsRequest(BaseModel):
    query: str
    focus: str = "financial" # can be 'financial', 'general', etc.

class Source(BaseModel):
    title: str
    url: str
    snippet: Optional[str] = None

class NewsResponse(BaseModel):
    answer: str
    sources: List[Source]
    related_questions: List[str]

# --- Endpoint ---
@router.post("/search", response_model=NewsResponse)
async def search_news(
    request: NewsRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    Queries Perplexity AI for real-time market news and analysis.
    """
    if not settings.PERPLEXITY_API_KEY:
        raise HTTPException(status_code=501, detail="Perplexity API Key not configured")

    headers = {
        "Authorization": f"Bearer {settings.PERPLEXITY_API_KEY}",
        "Content-Type": "application/json"
    }

    # Prompt Engineering for Financial Context
    system_prompt = (
        "You are a sophisticated financial research assistant. "
        "Provide a concise, data-driven summary of the requested topic. "
        "Focus on market impact, key levels, and institutional sentiment. "
        "Format with markdown (bolding key figures)."
    )
    
    payload = {
        "model": "sonar-reasoning-pro", # or "sonar" for faster/cheaper results
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": request.query}
        ],
        "temperature": 0.1,
        "return_citations": True,
        "return_related_questions": True
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.perplexity.ai/chat/completions",
                json=payload,
                headers=headers
            )
            response.raise_for_status()
            data = response.json()

            # Extract content
            choice = data["choices"][0]
            answer = choice["message"]["content"]
            citations = data.get("citations", [])
            
            # Perplexity returns citations as a list of URLs. 
            # We map them to the text based on [1], [2] indices manually if needed, 
            # or just return the list of links.
            # Ideally, we try to extract metadata if possible, but Perplexity standard API 
            # often just gives the list of URLs in `citations`.
            
            sources = []
            for url in citations:
                # Basic parsing or metadata fetching could happen here
                # For now, we return the URL as the title/source
                sources.append(Source(title=url.split('/')[2], url=url))

            # Extract related questions (if available in specific models)
            # Note: Standard chat completion might not return 'related_questions' explicitly 
            # unless using specific beta features. We'll default to empty if not found.
            related = data.get("related_questions", [])

            return NewsResponse(
                answer=answer,
                sources=sources,
                related_questions=related
            )

    except httpx.HTTPStatusError as e:
        logger.error(f"Perplexity API Error: {e.response.text}")
        raise HTTPException(status_code=e.response.status_code, detail="Failed to fetch news")
    except Exception as e:
        logger.error(f"News search error: {e}")
        raise HTTPException(status_code=500, detail=str(e))