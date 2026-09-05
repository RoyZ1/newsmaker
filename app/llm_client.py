from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.env_loader import load_dotenv


@dataclass(slots=True)
class LLMConfig:
    api_key: str
    model: str
    base_url: str
    timeout: float
    max_retries: int
    retry_delay_seconds: float


def load_llm_config() -> LLMConfig:
    load_dotenv()
    api_key = os.getenv("LLM_API_KEY", "").strip()
    model = os.getenv("LLM_MODEL_ID", "").strip()
    base_url = os.getenv("LLM_BASE_URL", "").strip().rstrip("/")
    timeout = float(os.getenv("LLM_TIMEOUT", "120") or 120)
    max_retries = int(os.getenv("LLM_MAX_RETRIES", "2") or 2)
    retry_delay_seconds = float(os.getenv("LLM_RETRY_DELAY_SECONDS", "2") or 2)
    if not api_key:
        raise RuntimeError("Missing LLM_API_KEY in .env")
    if not model:
        raise RuntimeError("Missing LLM_MODEL_ID in .env")
    if not base_url:
        raise RuntimeError("Missing LLM_BASE_URL in .env")
    return LLMConfig(
        api_key=api_key,
        model=model,
        base_url=base_url,
        timeout=timeout,
        max_retries=max(0, max_retries),
        retry_delay_seconds=max(0.0, retry_delay_seconds),
    )


def chat_completion(messages: list[dict[str, str]], temperature: float = 0.7) -> str:
    config = load_llm_config()
    url = f"{config.base_url}/chat/completions"
    payload: dict[str, Any] = {
        "model": config.model,
        "messages": messages,
        "temperature": temperature,
    }
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }
    last_exc: Exception | None = None
    attempts = config.max_retries + 1
    for attempt in range(attempts):
        try:
            with httpx.Client(timeout=config.timeout, follow_redirects=True) as client:
                response = client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
            return data["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            if not should_retry_http_status(exc) or attempt >= attempts - 1:
                raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            if attempt >= attempts - 1:
                raise
        time.sleep(config.retry_delay_seconds * (attempt + 1))
    if last_exc:
        raise last_exc
    raise RuntimeError("LLM request failed without an exception.")


def should_retry_http_status(exc: httpx.HTTPStatusError) -> bool:
    return exc.response.status_code in {408, 429, 500, 502, 503, 504}
