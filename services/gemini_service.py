"""Centralised Gemini access (official ``google-genai`` SDK)."""
import logging
import time

from config import Config

log = logging.getLogger(__name__)


class GeminiError(Exception):
    """User-presentable error from the LLM layer (never contains the API key)."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


class GeminiService:
    RETRY_CODES = {429, 500, 502, 503, 504}

    def __init__(self, api_key=None, model=None, timeout=None, max_retries=None):
        self.api_key = (Config.GEMINI_API_KEY if api_key is None else api_key).strip()
        self.model = model or Config.GEMINI_MODEL
        self.timeout = timeout or Config.GEMINI_TIMEOUT_SECONDS
        self.max_retries = max_retries or Config.GEMINI_MAX_RETRIES
        self._client = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _get_client(self):
        if not self.api_key:
            raise GeminiError(
                "GEMINI_API_KEY is not set. Copy .env.example to .env and add a key "
                "from https://aistudio.google.com/apikey, then restart the server.",
                status=503,
            )
        if self._client is None:
            from google import genai
            from google.genai import types

            self._client = genai.Client(
                api_key=self.api_key,
                http_options=types.HttpOptions(timeout=self.timeout * 1000),
            )
        return self._client

    def _translate(self, exc) -> GeminiError:
        code = getattr(exc, "code", None)
        message = str(getattr(exc, "message", "") or "")
        if code in (401, 403) or "api key" in message.lower():
            return GeminiError("Gemini rejected the API key or the request was not permitted. "
                               "Check GEMINI_API_KEY in your .env file.", 502)
        if code == 404:
            return GeminiError(f"Gemini model '{self.model}' was not found. Check GEMINI_MODEL.", 502)
        if code == 429:
            return GeminiError("Gemini rate limit or quota reached. Wait a moment and try again.", 429)
        if code and code >= 500:
            return GeminiError("The Gemini service is temporarily unavailable. Try again shortly.", 502)
        safe = message.replace(self.api_key, "***")[:200] if self.api_key else message[:200]
        return GeminiError(f"Gemini API error ({code}): {safe}", 502)

    def generate(self, prompt: str, system=None, json_mode=False, temperature=0.2, purpose="") -> str:
        client = self._get_client()
        from google.genai import errors, types

        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            response_mime_type="application/json" if json_mode else None,
        )
        for attempt in range(1, self.max_retries + 1):
            try:
                response = client.models.generate_content(
                    model=self.model, contents=prompt, config=config)
                text = response.text
                if not text or not text.strip():
                    raise GeminiError("Gemini returned an empty or blocked response. "
                                      "Try rephrasing the task.", 502)
                log.info("gemini call ok purpose=%s attempt=%d", purpose, attempt)
                return text
            except GeminiError:
                raise
            except errors.APIError as exc:
                if getattr(exc, "code", None) in self.RETRY_CODES and attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 10))
                    continue
                raise self._translate(exc) from None
            except Exception:  # network / timeout / SDK internals
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 10))
                    continue
                raise GeminiError("Could not reach the Gemini API (connection problem or timeout). "
                                  "Check your network and try again.", 502) from None
        raise GeminiError("Gemini request failed.", 502)  # pragma: no cover
