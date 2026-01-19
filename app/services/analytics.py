# app/services/analytics.py

import posthog
from app.core.config import settings

class Analytics:
    _initialized = False

    @classmethod
    def init(cls):
        if not cls._initialized and settings.POSTHOG_API_KEY:
            posthog.project_api_key = settings.POSTHOG_API_KEY
            posthog.host = settings.POSTHOG_HOST
            # Enable debug mode in development to see events in console
            posthog.debug = settings.ENVIRONMENT == "development"
            cls._initialized = True

    @staticmethod
    def capture(user_id: str, event_name: str, properties: dict = None):
        """
        Captures an event. PostHog handles the batching/queueing 
        automatically in a separate thread, so it won't block your API.
        """
        if Analytics._initialized:
            posthog.capture(
                distinct_id=user_id,
                event=event_name,
                properties=properties or {}
            )

    @staticmethod
    def identify(user_id: str, properties: dict):
        """
        Link a user to specific traits (e.g., their current plan).
        """
        if Analytics._initialized:
            posthog.identify(user_id, properties)