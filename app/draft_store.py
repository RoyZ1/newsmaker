from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.config import ROOT_DIR
from app.database import sync_draft_version, sync_drafts, sync_platform_draft
from app.formatting import clean_body_markdown, enrich_drafts_layout
from app.image_candidates import refresh_draft_image_candidates
from app.platform_variants import normalize_variant
from app.title_format import strip_title_prefix
from app.writer import DRAFTS_PATH, load_drafts


DRAFT_VERSION_DIR = ROOT_DIR / "data" / "draft_versions"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def update_draft_from_payload(draft_index: int, payload: dict[str, Any]) -> dict[str, Any]:
    variant = normalize_variant(str(payload.get("variant") or "long"))
    if variant == "short":
        return update_short_draft_from_payload(draft_index, payload)
    return update_long_draft_from_payload(draft_index, payload)


def update_long_draft_from_payload(draft_index: int, payload: dict[str, Any]) -> dict[str, Any]:
    drafts = load_drafts()
    if draft_index < 0 or draft_index >= len(drafts):
        raise IndexError("没有找到这篇草稿，可能已经被删除。")

    title = strip_title_prefix(str(payload.get("title") or "").strip())
    subtitle = str(payload.get("subtitle") or "").strip()
    body_markdown = clean_body_markdown(str(payload.get("body_markdown") or "")).strip()
    if not title:
        raise ValueError("标题不能为空。")
    if not body_markdown:
        raise ValueError("正文不能为空。")

    draft = drafts[draft_index]
    ensure_draft_identity(draft, draft_index)
    before_path = save_draft_version(draft, draft_index, "before-edit")

    draft["title"] = title
    draft["subtitle"] = subtitle
    draft["body_markdown"] = body_markdown
    draft["updated_at"] = datetime.now(timezone.utc).isoformat()
    ensure_draft_identity(draft, draft_index)
    refresh_draft_image_candidates(draft)

    saved_drafts = save_draft_dicts(drafts)
    after_path = save_draft_version(saved_drafts[draft_index], draft_index, "saved")
    return {
        "draft": saved_drafts[draft_index],
        "variant": "long",
        "version_before_path": str(before_path),
        "version_saved_path": str(after_path),
    }


def update_short_draft_from_payload(draft_index: int, payload: dict[str, Any]) -> dict[str, Any]:
    from app.heybox_writer import load_heybox_cache, save_heybox_cache, stable_source_hash

    drafts = load_drafts()
    if draft_index < 0 or draft_index >= len(drafts):
        raise IndexError("没有找到这篇草稿，可能已经被删除。")

    title = strip_title_prefix(str(payload.get("title") or "").strip())
    subtitle = str(payload.get("subtitle") or "").strip()
    body_markdown = clean_body_markdown(str(payload.get("body_markdown") or "")).strip()
    if not title:
        raise ValueError("标题不能为空。")
    if not body_markdown:
        raise ValueError("正文不能为空。")

    draft = drafts[draft_index]
    draft_id = ensure_draft_identity(draft, draft_index)
    before_path = save_draft_version(draft, draft_index, "before-short-edit")

    if title != str(draft.get("title") or ""):
        draft["title"] = title
        draft["updated_at"] = datetime.now(timezone.utc).isoformat()
        saved_drafts = save_draft_dicts(drafts)
        draft = saved_drafts[draft_index]
    else:
        saved_drafts = drafts

    cache = load_heybox_cache()
    source_hash = stable_source_hash(draft)
    existing = cache.get(draft_id) if isinstance(cache.get(draft_id), dict) else {}
    short_copy = {
        **existing,
        "title": str(draft.get("title") or title),
        "subtitle": subtitle,
        "body_markdown": body_markdown,
        "draft_index": draft_index,
        "draft_id": draft_id,
        "source_hash": source_hash,
        "source_title": str(draft.get("title") or title),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "platform": "heybox",
        "manual_edited": True,
    }
    cache[draft_id] = short_copy
    save_heybox_cache(cache)
    sync_platform_draft(short_copy)
    after_path = save_draft_version({"short_copy": short_copy, **draft}, draft_index, "saved-short")
    return {
        "draft": saved_drafts[draft_index],
        "short_copy": short_copy,
        "variant": "short",
        "version_before_path": str(before_path),
        "version_saved_path": str(after_path),
    }


def save_draft_dicts(drafts: list[dict[str, Any]], path: Path = DRAFTS_PATH) -> list[dict[str, Any]]:
    for index, draft in enumerate(drafts):
        ensure_draft_identity(draft, index)
        refresh_draft_image_candidates(draft)
    payload = enrich_drafts_layout(drafts)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    sync_drafts(payload)
    return payload


def ensure_draft_identity(draft: dict[str, Any], index: int = 0) -> str:
    draft_id = str(draft.get("draft_id") or "").strip()
    if draft_id:
        return draft_id
    created_at = str(draft.get("created_at") or datetime.now(timezone.utc).isoformat())
    draft["created_at"] = created_at
    raw = f"{index}:{draft.get('topic_id', '')}:{draft.get('title', '')}:{created_at}"
    digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]
    draft["draft_id"] = f"draft-{digest}"
    return str(draft["draft_id"])


def save_draft_version(draft: dict[str, Any], draft_index: int, reason: str) -> Path:
    now = datetime.now(LOCAL_TZ)
    date_key = now.date().isoformat()
    draft_id = ensure_draft_identity(draft, draft_index)
    safe_reason = safe_slug(reason) or "version"
    file_name = f"{draft_id}-{now.strftime('%H%M%S%f')}-{safe_reason}.json"
    path = DRAFT_VERSION_DIR / date_key / file_name
    payload = {
        "reason": reason,
        "saved_at": now.isoformat(),
        "draft_index": draft_index,
        "draft_id": draft_id,
        "title": draft.get("title", ""),
        "draft": draft,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    sync_draft_version(payload, path)
    return path


def safe_slug(value: str, max_length: int = 48) -> str:
    slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "-", value).strip("-_")
    return slug[:max_length].strip("-_")
