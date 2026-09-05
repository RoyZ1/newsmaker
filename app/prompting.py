from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_prompt_json(path: Path) -> dict[str, str]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise RuntimeError(f"Prompt file must be a JSON object: {path}")

    system = normalize_prompt_text(payload.get("system"))
    user = normalize_prompt_text(payload.get("user"))
    if not system or not user:
        raise RuntimeError(f"Prompt file must contain system and user: {path}")
    return {"system": system, "user": user}


def normalize_prompt_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(normalize_prompt_line(item) for item in value).strip()
    return str(value or "")


def normalize_prompt_line(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(normalize_prompt_line(item) for item in value)
    return str(value or "")
