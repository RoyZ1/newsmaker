from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import ROOT_DIR


PLATFORM_CHOICES_PATH = ROOT_DIR / "data" / "platform_choices.json"
VALID_VARIANTS = {"long", "short"}
VARIANT_LABELS = {
    "long": "长文版",
    "short": "短文版",
}
LEGACY_VARIANTS = {"wechat": "long", "heybox": "short"}


def load_platform_choices(path: Path = PLATFORM_CHOICES_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_platform_choices(choices: dict[str, Any], path: Path = PLATFORM_CHOICES_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(choices, handle, ensure_ascii=False, indent=2)


def normalize_variant(value: str, default: str = "long") -> str:
    raw = str(value or "").strip()
    mapped = LEGACY_VARIANTS.get(raw, raw)
    if mapped in VALID_VARIANTS:
        return mapped
    return LEGACY_VARIANTS.get(default, default) if LEGACY_VARIANTS.get(default, default) in VALID_VARIANTS else "long"


def selected_platform_variant(draft_id: str, platform: str, default: str = "long", path: Path = PLATFORM_CHOICES_PATH) -> str:
    choice = platform_choice(draft_id, platform, default=default, path=path)
    return str(choice.get("variant") or default)


def selected_article_variant(draft_id: str, default: str = "long", path: Path = PLATFORM_CHOICES_PATH) -> str:
    choice = article_variant_choice(draft_id, default=default, path=path)
    return str(choice.get("variant") or default)


def article_variant_choice(draft_id: str, default: str = "long", path: Path = PLATFORM_CHOICES_PATH) -> dict[str, Any]:
    choice = platform_choice(draft_id, "article", default=default, path=path)
    if not choice.get("is_default"):
        return choice
    legacy_choice = platform_choice(draft_id, "heybox", default=default, path=path)
    if not legacy_choice.get("is_default"):
        return {
            **legacy_choice,
            "platform": "article",
            "draft_id": draft_id,
        }
    return choice


def platform_choice(draft_id: str, platform: str, default: str = "long", path: Path = PLATFORM_CHOICES_PATH) -> dict[str, Any]:
    variant = normalize_variant(default)
    choices = load_platform_choices(path)
    platform_choices = choices.get(platform)
    if isinstance(platform_choices, dict):
        raw_choice = platform_choices.get(draft_id)
        if isinstance(raw_choice, dict):
            raw_variant = normalize_variant(str(raw_choice.get("variant") or ""), default=variant)
            if raw_variant in VALID_VARIANTS:
                return {
                    **raw_choice,
                    "platform": platform,
                    "draft_id": draft_id,
                    "variant": raw_variant,
                    "label": VARIANT_LABELS.get(raw_variant, raw_variant),
                }
        elif isinstance(raw_choice, str):
            variant = normalize_variant(raw_choice, default=variant)
    return {
        "platform": platform,
        "draft_id": draft_id,
        "variant": variant,
        "label": VARIANT_LABELS.get(variant, variant),
        "is_default": True,
    }


def set_platform_variant(
    draft_id: str,
    platform: str,
    variant: str,
    draft_index: int | None = None,
    title: str = "",
    path: Path = PLATFORM_CHOICES_PATH,
) -> dict[str, Any]:
    if not draft_id:
        raise ValueError("缺少草稿 ID，无法保存平台版本选择。")
    if platform != "article":
        raise ValueError("当前只支持保存文章版本选择。")
    variant = normalize_variant(variant)
    if variant not in VALID_VARIANTS:
        raise ValueError("请选择长文版或短文版。")

    choices = load_platform_choices(path)
    platform_choices = choices.get(platform)
    if not isinstance(platform_choices, dict):
        platform_choices = {}
        choices[platform] = platform_choices

    record = {
        "platform": platform,
        "draft_id": draft_id,
        "draft_index": draft_index,
        "title": title,
        "variant": variant,
        "label": VARIANT_LABELS.get(variant, variant),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    platform_choices[draft_id] = record
    save_platform_choices(choices, path)
    return record
