import os
import time


PROVIDER = "typhoon"  
_TYPHOON_MODEL    = "typhoon-v2.5-30b-a3b-instruct"
_TYPHOON_BASE_URL = "https://api.opentyphoon.ai/v1"

_GEMINI_MODEL     = "gemini-2.0-flash"

MAX_TOKENS        = 10000
MAX_RETRIES       = 3

_client = None  
_genai  = None   

def _get_client():
    """Return the appropriate client, initialising once."""
    global _client, _genai

    if PROVIDER == "typhoon":
        if _client is None:
            from openai import OpenAI
            api_key = os.getenv("TYPHOON_API_KEY")
            if not api_key:
                raise EnvironmentError(
                    "TYPHOON_API_KEY is not set.\n"
                    "  Add it to your .env file: TYPHOON_API_KEY=your_key_here\n"
                    "  Get your free key at: https://playground.opentyphoon.ai/api-key"
                )
            _client = OpenAI(api_key=api_key, base_url=_TYPHOON_BASE_URL)
        return _client

    elif PROVIDER == "gemini":
        if _genai is None:
            import google.generativeai as genai
            api_key = os.getenv("GOOGLE_API_KEY")
            if not api_key:
                raise EnvironmentError(
                    "GOOGLE_API_KEY is not set.\n"
                    "  Add it to your .env file: GOOGLE_API_KEY=your_key_here\n"
                    "  Get your key at: https://aistudio.google.com/apikey"
                )
            genai.configure(api_key=api_key)
            _genai = genai
        return _genai

    else:
        raise ValueError(f"Unknown PROVIDER: '{PROVIDER}'. Must be 'typhoon' or 'gemini'.")


# ---------------------------------------------------------------------------
# Provider name (for display in the terminal banner)
# ---------------------------------------------------------------------------

def provider_name() -> str:
    """Human-readable name of the active provider."""
    return {
        "typhoon": "Typhoon v2.5 30B (SCB 10X)",
        "gemini":  "Gemini 2.5 Flash (Google)",
    }.get(PROVIDER, PROVIDER)

# Core call — both providers share this interface
def call(system: str, prompt: str) -> str:
    """
    Send (system, prompt) to the active LLM and return the response text.
    Retries up to MAX_RETRIES times with exponential backoff on rate limit
    or server errors.

    Raises RuntimeError with a friendly message after all retries are exhausted.
    """
    for attempt in range(MAX_RETRIES):
        try:
            return _dispatch(system, prompt)
        except Exception as e:
            recoverable, wait, message = _classify_error(e, attempt)
            if recoverable and attempt < MAX_RETRIES - 1:
                print(f"\r  {message} — retrying in {wait}s...", end="", flush=True)
                time.sleep(wait)
            else:
                raise RuntimeError(message) from e

    raise RuntimeError("Unexpected exit from retry loop.")


def _dispatch(system: str, prompt: str) -> str:
    """Single attempt — no retry logic here."""
    if PROVIDER == "typhoon":
        return _call_typhoon(system, prompt)
    elif PROVIDER == "gemini":
        return _call_gemini(system, prompt)
    else:
        raise ValueError(f"Unknown PROVIDER: '{PROVIDER}'")

# Typhoon (OpenAI-compatible)
def _call_typhoon(system: str, prompt: str) -> str:
    client = _get_client()
    response = client.chat.completions.create(
        model=_TYPHOON_MODEL,
        max_tokens=MAX_TOKENS,
        temperature=0.0,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ],
    )
    return response.choices[0].message.content.strip()


# Gemini (native google-generativeai)

def _call_gemini(system: str, prompt: str) -> str:
    genai = _get_client()
    model = genai.GenerativeModel(
        model_name=_GEMINI_MODEL,
        system_instruction=system,
        generation_config=genai.GenerationConfig(
            max_output_tokens=MAX_TOKENS,
            temperature=0.0,
        ),
    )
    response = model.generate_content(prompt)
    return response.text.strip()


# Error classification — shared across providers

def _classify_error(exc: Exception, attempt: int) -> tuple[bool, int, str]:
    """
    Returns (recoverable, wait_seconds, message).
    recoverable=True  → caller should retry after wait_seconds
    recoverable=False → caller should give up immediately
    """
    wait = 2 ** attempt   # 1s, 2s, 4s

    name = type(exc).__name__
    msg  = str(exc).lower()

    # Rate limit
    if "ratelimit" in name.lower() or "resource_exhausted" in name.lower() or "429" in msg:
        return True, wait, "Rate limit hit"

    # Transient server errors
    if "internalserver" in name.lower() or "500" in msg or "503" in msg:
        return True, wait, "Server error"

    # Non-recoverable (auth, bad request, etc.)
    if "autherror" in name.lower() or "401" in msg or "403" in msg:
        return False, 0, f"Authentication error — check your API key ({name})"

    # Unknown — retry just in case
    return True, wait, f"Unexpected error ({name})"