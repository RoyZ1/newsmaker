from __future__ import annotations

import json
import hashlib
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from app.config import ROOT_DIR
from app.categories import classify_item, category_label
from app.database import sync_news_items
from app.models import NewsItem


DATA_DIR = ROOT_DIR / "data"
ITEMS_PATH = DATA_DIR / "items.json"
DAILY_DIR = DATA_DIR / "daily"
DAILY_COLLECTED_DIR = DATA_DIR / "daily_collected"
SEEN_ITEMS_PATH = DATA_DIR / "seen_items.json"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def save_items(items: Iterable[NewsItem], path: Path = ITEMS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [item.to_dict() for item in items]
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    save_daily_items(payload)
    save_collected_daily_items(payload)
    update_seen_items(payload)
    sync_news_items(payload)


def load_items(path: Path = ITEMS_PATH) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        items = json.load(handle)
    for item in items:
        category = classify_item(item)
        item["category"] = category
        item["category_label"] = category_label(category)
    return items


def save_item_dicts(items: list[dict], path: Path = ITEMS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(items, handle, ensure_ascii=False, indent=2)
    save_daily_items(items)
    save_collected_daily_items(items)
    sync_news_items(items)


def delete_item(item_id: str, path: Path = ITEMS_PATH) -> bool:
    items = load_items(path)
    next_items = [item for item in items if item.get("id") != item_id]
    if len(next_items) == len(items):
        return False
    save_item_dicts(next_items, path)
    return True


def delete_item_image(item_id: str, image: str, path: Path = ITEMS_PATH) -> bool:
    items = load_items(path)
    changed = False
    for item in items:
        if item.get("id") != item_id:
            continue
        for field in ("local_images", "images"):
            values = item.get(field)
            if isinstance(values, list) and image in values:
                item[field] = [value for value in values if value != image]
                changed = True
        if changed and not item.get("local_images") and not item.get("images"):
            item["image_usage"] = "none"
        break
    if changed:
        save_item_dicts(items, path)
    return changed


def save_daily_items(items: list[dict]) -> None:
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        date_key = local_date_key(item.get("published_at"))
        if date_key:
            grouped[date_key].append(item)

    for date_key, day_items in grouped.items():
        path = DAILY_DIR / f"{date_key}.json"
        with path.open("w", encoding="utf-8") as handle:
            json.dump(day_items, handle, ensure_ascii=False, indent=2)


def save_collected_daily_items(items: list[dict]) -> None:
    DAILY_COLLECTED_DIR.mkdir(parents=True, exist_ok=True)
    date_key = datetime.now(LOCAL_TZ).date().isoformat()
    path = DAILY_COLLECTED_DIR / f"{date_key}.json"
    with path.open("w", encoding="utf-8") as handle:
        json.dump(items, handle, ensure_ascii=False, indent=2)


def load_seen_items(path: Path = SEEN_ITEMS_PATH) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def update_seen_items(items: list[dict], path: Path = SEEN_ITEMS_PATH) -> None:
    seen = load_seen_items(path)
    now = datetime.now(timezone.utc).isoformat()
    for item in items:
        fingerprint = item_fingerprint(item)
        if not fingerprint:
            continue
        seen.setdefault(
            fingerprint,
            {
                "first_seen_at": now,
                "first_seen_date": datetime.now(LOCAL_TZ).date().isoformat(),
                "title": item.get("title"),
                "url": item.get("url"),
                "source_id": item.get("source_id"),
                "published_at": item.get("published_at"),
            },
        )
        seen[fingerprint]["last_seen_at"] = now
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(seen, handle, ensure_ascii=False, indent=2)


def item_fingerprint(item: dict) -> str:
    url = str(item.get("url") or "").strip().lower().split("#", 1)[0].rstrip("/")
    title = normalize_title(str(item.get("title") or ""))
    source_id = str(item.get("source_id") or "")
    raw = url or f"{source_id}:{title}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def normalize_title(title: str) -> str:
    return "".join(title.lower().split())


def local_date_key(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return parsed.astimezone(LOCAL_TZ).date().isoformat()
