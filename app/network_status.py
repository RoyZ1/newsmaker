from __future__ import annotations

import re
from typing import Any

import httpx


IP_SERVICES = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
)


def detect_public_ip(timeout: float = 5.0) -> dict[str, Any]:
    errors: list[str] = []
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for url in IP_SERVICES:
            try:
                response = client.get(url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                errors.append(f"{url}: {exc}")
                continue
            value = response.text.strip()
            if is_ip_like(value):
                return {"ip": value, "source": url, "errors": errors}
            errors.append(f"{url}: invalid response")
    return {"ip": "", "source": "", "errors": errors}


def is_ip_like(value: str) -> bool:
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", value):
        return True
    if ":" in value and re.fullmatch(r"[0-9A-Fa-f:.]+", value):
        return True
    return False
