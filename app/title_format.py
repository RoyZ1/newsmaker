from __future__ import annotations

import os
import re

from app.formatting import strip_inline_rich_markers


DEFAULT_TITLE_PREFIX = "\u6bcf\u65e5\u5feb\u8baf"
TITLE_PREFIX_ENV_KEYS = ("TITLE_PREFIX", "ARTICLE_TITLE_PREFIX")
DISABLED_VALUES = {"", "0", "false", "off", "none", "no"}


def configured_title_prefix() -> str:
    value: str | None = None
    for key in TITLE_PREFIX_ENV_KEYS:
        if key in os.environ:
            value = os.getenv(key, "")
            break
    if value is None:
        value = DEFAULT_TITLE_PREFIX
    value = normalize_prefix_value(value)
    if value.lower() in DISABLED_VALUES:
        return ""
    return value[:12]


def normalize_prefix_value(value: str) -> str:
    text = strip_inline_rich_markers(str(value or "")).strip()
    text = re.sub(r"^\s*[【\[]\s*", "", text)
    text = re.sub(r"\s*[】\]]\s*$", "", text)
    return re.sub(r"\s+", "", text)


def title_prefix_bracketed(prefix: str | None = None) -> str:
    value = configured_title_prefix() if prefix is None else normalize_prefix_value(prefix)
    return f"\u3010{value}\u3011" if value else ""


def strip_title_prefix(title: str, prefix: str | None = None) -> str:
    text = strip_inline_rich_markers(str(title or "")).strip()
    known_prefixes = [
        normalize_prefix_value(value)
        for value in (
            prefix,
            configured_title_prefix(),
            DEFAULT_TITLE_PREFIX,
        )
        if value is not None
    ]
    seen: set[str] = set()
    for value in known_prefixes:
        if not value or value in seen:
            continue
        seen.add(value)
        bracketed = f"\u3010{value}\u3011"
        while text.startswith(bracketed):
            text = text[len(bracketed) :].strip()
    return text


def format_title_with_prefix(title: str, prefix: str | None = None) -> str:
    clean_title = strip_title_prefix(title, prefix).strip()
    if not clean_title:
        return ""
    bracketed = title_prefix_bracketed(prefix)
    return f"{bracketed}{clean_title}" if bracketed else clean_title


def title_prefix_context() -> dict[str, str | bool]:
    prefix = configured_title_prefix()
    return {
        "enabled": bool(prefix),
        "label": prefix,
        "bracketed": title_prefix_bracketed(prefix),
    }
