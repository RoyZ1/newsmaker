from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import ROOT_DIR


IMAGE_HISTORY_PATH = ROOT_DIR / "data" / "image_history.json"


def remember_slot_image(draft: dict[str, Any], slot_id: str, record: dict[str, Any]) -> None:
    if not record.get("url"):
        return
    history = load_image_history()
    key = history_key(draft, slot_id)
    item = {
        "slot_id": slot_id,
        "topic_id": draft.get("topic_id", ""),
        "draft_id": draft.get("draft_id", ""),
        "title": draft.get("title", ""),
        "record": record,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    values = [entry for entry in history.get(key, []) if entry.get("record", {}).get("url") != record.get("url")]
    history[key] = [item, *values][:20]
    save_image_history(history)


def find_latest_generated_slot_image(draft: dict[str, Any], slot_id: str) -> dict[str, Any] | None:
    for entry in load_image_history().get(history_key(draft, slot_id), []):
        record = entry.get("record")
        if not isinstance(record, dict) or not record.get("url"):
            continue
        source_type = str(record.get("type") or record.get("source_type") or "")
        if source_type not in {"generated_cover", "generated_section", "generated"}:
            continue
        local_path = str(record.get("local_path") or "")
        if local_path and not Path(local_path).exists():
            continue
        return dict(record)
    return None


def history_key(draft: dict[str, Any], slot_id: str) -> str:
    topic_id = str(draft.get("topic_id") or "").strip()
    draft_id = str(draft.get("draft_id") or "").strip()
    title = "".join(str(draft.get("title") or "").lower().split())
    base = topic_id or draft_id or title or "draft"
    return f"{base}:{slot_id}"


def load_image_history(path: Path = IMAGE_HISTORY_PATH) -> dict[str, list[dict[str, Any]]]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_image_history(history: dict[str, list[dict[str, Any]]], path: Path = IMAGE_HISTORY_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(history, handle, ensure_ascii=False, indent=2)


def backfill_image_history_from_drafts(drafts: list[dict[str, Any]]) -> int:
    count = 0
    for draft in drafts:
        for image in draft.get("final_images", []) or []:
            if not isinstance(image, dict) or not image.get("url"):
                continue
            source_type = str(image.get("type") or image.get("source_type") or "")
            if source_type not in {"generated_cover", "generated_section", "generated", "manual_upload"}:
                continue
            slot_id = str(image.get("slot_id") or "")
            if not slot_id:
                continue
            remember_slot_image(draft, slot_id, image)
            count += 1
    return count
