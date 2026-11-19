"""Local LLM backend — OpenAI or Gemini, selected by LLM_PROVIDER."""
import time

from ..config import (
    GEMINI_FALLBACK_MODELS,
    GEMINI_MODEL,
    GEMINI_TIMEOUT_SECONDS,
    LLM_PROVIDER,
    OPENAI_MODEL,
    require_gemini_key,
    require_openai_key,
)


def call_openai_llm(prompt: str) -> str:
    """OpenAI Chat Completions backend used by --mode local."""
    try:
        from openai import OpenAI  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "openai is required for --mode local. Install it with:\n"
            "    pip install -r requirements-local.txt"
        ) from e

    client = OpenAI(api_key=require_openai_key())
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        temperature=0.7,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content or ""


def call_gemini_llm(prompt: str, images: list = None) -> str:
    """Gemini backend used by --mode local when LLM_PROVIDER=gemini."""
    try:
        from google import genai  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "google-genai is required for LLM_PROVIDER=gemini. Install it with:\n"
            "    pip install -r requirements-local.txt"
        ) from e

    from google.genai import errors, types  # type: ignore

    client = genai.Client(
        api_key=require_gemini_key(),
        http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_SECONDS * 1000),
    )
    # Try the configured model first, then fallbacks (retired / overloaded
    # models return 404 / 5xx; quota exhaustion returns 429).
    models = [GEMINI_MODEL] + [m for m in GEMINI_FALLBACK_MODELS if m != GEMINI_MODEL]
    contents = [prompt] + [types.Part.from_bytes(data=img, mime_type="image/jpeg") for img in images or []]
    failures = []
    for model in models:
        for attempt in range(1, 4):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=contents,
                    config={
                        "temperature": 0.2,
                        "response_mime_type": "application/json",
                        "max_output_tokens": 8192,
                    },
                )
                if model != GEMINI_MODEL:
                    print(f"[llm/gemini] used fallback model {model}", flush=True)
                return response.text or ""
            except errors.APIError as e:
                failures.append(f"{model}: HTTP {e.code} {str(e.message or '')[:160]}")
                if e.code in (500, 502, 503, 504) and attempt < 3:
                    print(f"[llm/gemini] {model} busy (HTTP {e.code}); retrying in {20 * attempt}s", flush=True)
                    time.sleep(20 * attempt)  # "high demand" spikes are usually gone within a minute
                    continue
                if e.code == 429:
                    print(f"[llm/gemini] {model} quota/rate limit hit (429); trying next model", flush=True)
                break  # 4xx/5xx from the API: move on to the next model
            except Exception as e:  # network/TLS hiccups: retry once, then next model
                failures.append(f"{model}: {type(e).__name__} {str(e)[:160]}")
                time.sleep(3)
    raise RuntimeError(
        "All Gemini models failed. If you see 429, the free-tier quota is used up — "
        "wait for it to reset (no billing needed). Details:\n  " + "\n  ".join(failures)
    )


def call_local_llm(prompt: str) -> str:
    """Dispatch to the configured local LLM provider."""
    provider = (LLM_PROVIDER or "openai").strip().lower()
    if provider == "openai":
        return call_openai_llm(prompt)
    if provider == "gemini":
        return call_gemini_llm(prompt)
    raise RuntimeError(
        f"Unknown LLM_PROVIDER={provider!r}. Use 'openai' or 'gemini'."
    )
