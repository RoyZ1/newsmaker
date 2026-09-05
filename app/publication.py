from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.config import ROOT_DIR
from app.database import sync_publications


PUBLICATIONS_PATH = ROOT_DIR / "data" / "publications.json"
PUBLISHED_DIR = ROOT_DIR / "data" / "published"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def load_publications(path: Path = PUBLICATIONS_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, list) else []


def save_publications(records: list[dict[str, Any]], path: Path = PUBLICATIONS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    records = sorted(records, key=lambda item: str(item.get("published_at") or ""), reverse=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(records, handle, ensure_ascii=False, indent=2)
    sync_publications(records)


def publication_summary() -> dict[str, Any]:
    records = load_publications()
    grouped: dict[str, int] = {}
    for record in records:
        date_key = str(record.get("published_date") or "")[:10]
        if not date_key:
            continue
        grouped[date_key] = grouped.get(date_key, 0) + 1
    dates = [{"date": date, "count": count} for date, count in sorted(grouped.items(), reverse=True)]
    return {
        "records": records,
        "dates": dates,
        "count": len(records),
    }


def mark_draft_published(
    draft: dict[str, Any],
    channel: str = "manual",
    note: str = "",
    published_at: str | None = None,
) -> dict[str, Any]:
    records = load_publications()
    fingerprint = draft_fingerprint(draft)
    duplicate = find_duplicate_publication(records, fingerprint)
    if duplicate:
        raise ValueError(f"这篇文章已经备案发布过：{duplicate.get('published_date')} / {duplicate.get('title')}")

    now = parse_or_now(published_at)
    date_key = now.astimezone(LOCAL_TZ).date().isoformat()
    record = build_publication_record(draft, fingerprint, now, date_key, channel, note)
    records.append(record)
    save_publications(records)
    save_daily_publication(record, draft)
    return record


def publication_status_for_drafts(drafts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    records = load_publications()
    by_fingerprint = {str(record.get("fingerprint")): record for record in records}
    status: dict[str, dict[str, Any]] = {}
    for index, draft in enumerate(drafts):
        fingerprint = draft_fingerprint(draft)
        record = by_fingerprint.get(fingerprint)
        status[str(index)] = {
            "fingerprint": fingerprint,
            "published": bool(record),
            "record": record,
        }
    return status


def published_fingerprints() -> set[str]:
    return {str(record.get("fingerprint")) for record in load_publications() if record.get("fingerprint")}


def draft_already_published(draft: dict[str, Any]) -> dict[str, Any] | None:
    fingerprint = draft_fingerprint(draft)
    return find_duplicate_publication(load_publications(), fingerprint)


def find_duplicate_publication(records: list[dict[str, Any]], fingerprint: str) -> dict[str, Any] | None:
    for record in records:
        if record.get("fingerprint") == fingerprint:
            return record
    return None


def build_publication_record(
    draft: dict[str, Any],
    fingerprint: str,
    published_at: datetime,
    date_key: str,
    channel: str,
    note: str,
) -> dict[str, Any]:
    source_urls = [
        str(item.get("url"))
        for item in draft.get("source_links", []) or []
        if isinstance(item, dict) and item.get("url")
    ]
    images = [
        image
        for image in draft.get("final_images", []) or []
        if isinstance(image, dict) and image.get("url")
    ]
    return {
        "id": make_publication_id(fingerprint, published_at),
        "fingerprint": fingerprint,
        "title": draft.get("title", ""),
        "subtitle": draft.get("subtitle", ""),
        "topic_id": draft.get("topic_id", ""),
        "channel": channel or "manual",
        "note": note,
        "published_at": published_at.isoformat(),
        "published_date": date_key,
        "word_count_zh": (draft.get("layout") or {}).get("word_count_zh", 0),
        "image_count": len(images),
        "source_count": len(source_urls),
        "source_urls": source_urls,
        "cover_url": (draft.get("cover_image") or {}).get("url", ""),
        "archive_path": str(daily_publication_path(date_key, fingerprint)),
    }


def save_daily_publication(record: dict[str, Any], draft: dict[str, Any]) -> None:
    date_key = str(record.get("published_date") or "")
    path = daily_publication_path(date_key, str(record.get("fingerprint") or ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "record": record,
        "draft": draft,
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def daily_publication_path(date_key: str, fingerprint: str) -> Path:
    safe_date = date_key or datetime.now(LOCAL_TZ).date().isoformat()
    return PUBLISHED_DIR / safe_date / f"{fingerprint[:12]}.json"


def draft_fingerprint(draft: dict[str, Any]) -> str:
    title = normalize_text(str(draft.get("title") or ""))
    body = normalize_text(str(draft.get("body_markdown") or ""))
    source_urls = sorted(
        str(item.get("url") or "").strip().lower().split("#", 1)[0].rstrip("/")
        for item in draft.get("source_links", []) or []
        if isinstance(item, dict) and item.get("url")
    )
    raw = json.dumps(
        {
            "title": title,
            "body": body[:2400],
            "sources": source_urls,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def normalize_text(text: str) -> str:
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[，。！？、；：,.!?;:\"'“”‘’（）()\[\]【】#*_`-]+", "", text)
    return text.lower()


def make_publication_id(fingerprint: str, published_at: datetime) -> str:
    raw = f"{fingerprint}:{published_at.isoformat()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def parse_or_now(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return parsed.astimezone(timezone.utc)
