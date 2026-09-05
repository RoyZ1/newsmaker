from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.image_audit import audit_image_candidate
from app.image_history import find_latest_generated_slot_image
from app.storage import load_items


HEADING_RE = re.compile(r"^(#{1,4})\s+(.+)$")


def refresh_draft_image_candidates(draft: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = build_image_candidates_for_draft(draft)
    draft["image_candidate_pool"] = candidates
    ensure_image_slots(draft)
    return candidates


def build_image_candidates_for_draft(draft: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    removed = removed_image_urls(draft)

    for url in draft.get("image_candidates", []) or []:
        add_candidate(candidates, seen, make_candidate(url, "official", "官方图", publishable=True))

    for item in related_source_items(draft):
        raw = item.get("raw") or {}
        for url in raw.get("preview_local_images") or []:
            add_candidate(candidates, seen, make_candidate(url, "media_preview", "媒体图", publishable=False, source_item=item))
        for url in item.get("local_images") or []:
            source_type = "official" if item.get("image_usage") == "publishable_candidate" else "media_preview"
            label = "官方图" if source_type == "official" else "媒体图"
            add_candidate(candidates, seen, make_candidate(url, source_type, label, publishable=source_type == "official", source_item=item))

    cover = draft.get("cover_image")
    if isinstance(cover, dict) and cover.get("url"):
        add_candidate(candidates, seen, record_to_candidate(cover, selected=True))

    for image in draft.get("final_images", []) or []:
        if isinstance(image, dict) and image.get("url"):
            add_candidate(candidates, seen, record_to_candidate(image, selected=False))

    existing_pool = draft.get("image_candidate_pool")
    if isinstance(existing_pool, list):
        for image in existing_pool:
            if isinstance(image, dict) and image.get("url"):
                normalized = normalize_candidate(image)
                add_candidate(candidates, seen, normalized)

    selected_urls = selected_image_urls(draft)
    for candidate in candidates:
        candidate["selected"] = candidate.get("url") in selected_urls

    return [candidate for candidate in candidates if str(candidate.get("url") or "") not in removed]


def ensure_image_slots(draft: dict[str, Any]) -> list[dict[str, Any]]:
    sections = extract_draft_sections(draft)
    existing_slots = {
        str(slot.get("slot_id")): slot
        for slot in draft.get("image_slots", []) or []
        if isinstance(slot, dict) and slot.get("slot_id")
    }
    base_candidates = build_flat_candidate_pool(draft)

    slots: list[dict[str, Any]] = []
    used_urls: set[str] = set()
    cover_slot = merge_slot(draft, existing_slots.get("cover"), "cover", "cover", "封面/开头配图", 0, base_candidates)
    slots.append(cover_slot)
    remember_selected_url(cover_slot, used_urls)
    for position, section in enumerate(sections, start=1):
        slot = merge_slot(
            draft,
            existing_slots.get(section["slot_id"]),
            section["slot_id"],
            "section",
            section["title"],
            position,
            base_candidates,
        )
        replace_duplicate_selection(slot, used_urls)
        slots.append(slot)
        remember_selected_url(slot, used_urls)

    resolve_duplicate_slot_selections(slots)
    draft["image_slots"] = slots
    sync_final_images_from_slots(draft)
    mark_candidate_selection(draft)
    return slots


def remember_selected_url(slot: dict[str, Any], used_urls: set[str]) -> None:
    selected = slot.get("selected_image")
    if isinstance(selected, dict) and selected.get("url"):
        used_urls.add(str(selected["url"]))


def replace_duplicate_selection(slot: dict[str, Any], used_urls: set[str]) -> None:
    selected = slot.get("selected_image")
    if not isinstance(selected, dict) or not selected.get("url") or selected.get("url") not in used_urls:
        return
    if selected.get("manual_selected"):
        return
    replacement = next(
        (
            candidate
            for candidate in slot.get("candidate_pool", []) or []
            if candidate.get("publishable") and candidate.get("url") and candidate.get("url") not in used_urls
        ),
        None,
    )
    if not replacement:
        return
    slot["selected_image"] = candidate_to_record(replacement, slot.get("slot_id"))
    selected_url = replacement.get("url")
    for candidate in slot.get("candidate_pool", []) or []:
        candidate["selected"] = candidate.get("url") == selected_url


def extract_draft_sections(draft: dict[str, Any]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    for line in str(draft.get("body_markdown") or "").splitlines():
        match = HEADING_RE.match(line.strip())
        if not match:
            continue
        title = strip_heading_prefix(match.group(2).strip())
        if title:
            sections.append(
                {
                    "slot_id": f"section-{len(sections) + 1}",
                    "title": title,
                }
            )
    return sections


def strip_heading_prefix(title: str) -> str:
    return re.sub(r"^[一二三四五六七八九十0-9]+[、.．]\s*", "", title).strip()


def merge_slot(
    draft: dict[str, Any],
    existing: dict[str, Any] | None,
    slot_id: str,
    kind: str,
    label: str,
    position: int,
    base_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    slot_candidates = build_slot_candidate_pool(base_candidates, slot_id, kind, position)
    selected = selected_record_for_slot(draft, existing, slot_id, kind, slot_candidates)
    if selected and not any(candidate.get("url") == selected.get("url") for candidate in slot_candidates):
        slot_candidates.insert(0, record_to_candidate(selected, selected=True, slot_id=slot_id))

    selected_url = selected.get("url") if isinstance(selected, dict) else ""
    for candidate in slot_candidates:
        if selected_url and candidate.get("url") == selected_url and isinstance(selected, dict):
            candidate["caption"] = selected.get("caption", "")
        candidate["selected"] = bool(selected_url and candidate.get("url") == selected_url)
        candidate["slot_id"] = slot_id

    return {
        "slot_id": slot_id,
        "kind": kind,
        "label": label,
        "position": position,
        "selected_image": selected or None,
        "candidate_pool": slot_candidates,
    }


def build_flat_candidate_pool(draft: dict[str, Any]) -> list[dict[str, Any]]:
    pool = build_image_candidates_for_draft_without_slots(draft)
    selected_urls = selected_image_urls(draft)
    for candidate in pool:
        candidate["selected"] = candidate.get("url") in selected_urls
    return pool


def build_image_candidates_for_draft_without_slots(draft: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    removed = removed_image_urls(draft)

    for url in draft.get("image_candidates", []) or []:
        add_candidate(candidates, seen, make_candidate(url, "official", "官方图", publishable=True))

    for item in related_source_items(draft):
        raw = item.get("raw") or {}
        for url in raw.get("preview_local_images") or []:
            add_candidate(candidates, seen, make_candidate(url, "media_preview", "媒体图", publishable=False, source_item=item))
        for url in item.get("local_images") or []:
            source_type = "official" if item.get("image_usage") == "publishable_candidate" else "media_preview"
            add_candidate(
                candidates,
                seen,
                make_candidate(url, source_type, "官方图" if source_type == "official" else "媒体图", publishable=source_type == "official", source_item=item),
            )

    for record in existing_image_records(draft):
        add_candidate(candidates, seen, record_to_candidate(record, selected=False))

    existing_pool = draft.get("image_candidate_pool")
    if isinstance(existing_pool, list):
        for image in existing_pool:
            if isinstance(image, dict) and image.get("url"):
                add_candidate(candidates, seen, normalize_candidate(image))

    return [candidate for candidate in candidates if str(candidate.get("url") or "") not in removed]


def existing_image_records(draft: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    removed = removed_image_urls(draft)
    cover = draft.get("cover_image")
    if isinstance(cover, dict) and cover.get("url") and str(cover.get("url")) not in removed:
        records.append(cover)
    for image in draft.get("final_images", []) or []:
        if isinstance(image, dict) and image.get("url") and str(image.get("url")) not in removed:
            records.append(image)
    for slot in draft.get("image_slots", []) or []:
        if not isinstance(slot, dict):
            continue
        selected = slot.get("selected_image")
        if isinstance(selected, dict) and selected.get("url") and str(selected.get("url")) not in removed:
            records.append(selected)
        for candidate in slot.get("candidate_pool", []) or []:
            if isinstance(candidate, dict) and candidate.get("url") and str(candidate.get("url")) not in removed:
                records.append(candidate_to_record(candidate, slot.get("slot_id")))
    return records


def build_slot_candidate_pool(
    base_candidates: list[dict[str, Any]],
    slot_id: str,
    kind: str,
    position: int,
) -> list[dict[str, Any]]:
    pool: list[dict[str, Any]] = []
    seen: set[str] = set()
    generated = [candidate for candidate in base_candidates if str(candidate.get("source_type", "")).startswith("generated")]
    official = [candidate for candidate in base_candidates if candidate.get("source_type") == "official"]
    screenshots = [candidate for candidate in base_candidates if candidate.get("source_type") == "official_screenshot"]
    manual = [candidate for candidate in base_candidates if candidate.get("source_type") == "manual_upload"]
    opinion = [candidate for candidate in base_candidates if candidate.get("source_type") == "opinion_screenshot"]
    media = [candidate for candidate in base_candidates if candidate.get("source_type") == "media_preview"]

    ordered: list[dict[str, Any]] = []
    if kind == "cover":
        ordered.extend(slot_scoped_candidates(manual, slot_id))
        ordered.extend(slot_scoped_candidates(opinion, slot_id))
        ordered.extend(slot_scoped_candidates(screenshots, slot_id))
        ordered.extend(official)
        ordered.extend(generated)
        ordered.extend(media)
    else:
        rotated = rotate_candidates([*official, *media], max(position - 1, 0))
        ordered.extend(slot_scoped_candidates(manual, slot_id))
        ordered.extend(slot_scoped_candidates(opinion, slot_id))
        ordered.extend(slot_scoped_candidates(screenshots, slot_id))
        ordered.extend(generated)
        ordered.extend(rotated)

    for candidate in ordered:
        candidate_copy = normalize_candidate(candidate)
        candidate_copy["slot_id"] = slot_id
        add_candidate(pool, seen, candidate_copy)
    return pool[:12]


def slot_scoped_candidates(candidates: list[dict[str, Any]], slot_id: str) -> list[dict[str, Any]]:
    scoped: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_slot = str(candidate.get("slot_id") or "")
        if not candidate_slot or candidate_slot == slot_id:
            scoped.append(candidate)
    return scoped


def rotate_candidates(candidates: list[dict[str, Any]], offset: int) -> list[dict[str, Any]]:
    if not candidates:
        return []
    offset = offset % len(candidates)
    return [*candidates[offset:], *candidates[:offset]]


def selected_record_for_slot(
    draft: dict[str, Any],
    existing: dict[str, Any] | None,
    slot_id: str,
    kind: str,
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if slot_id in disabled_image_slots(draft):
        return None
    removed = removed_image_urls(draft)
    if existing:
        selected = existing.get("selected_image")
        if isinstance(selected, dict) and selected.get("url") and str(selected.get("url")) not in removed and is_publishable_record(selected, kind):
            return selected
    if kind == "cover":
        cover = draft.get("cover_image")
        if isinstance(cover, dict) and cover.get("url") and str(cover.get("url")) not in removed and is_publishable_record(cover, kind):
            return cover
    for image in draft.get("final_images", []) or []:
        if (
            isinstance(image, dict)
            and image.get("slot_id") == slot_id
            and image.get("url")
            and str(image.get("url")) not in removed
            and is_publishable_record(image, kind)
        ):
            return image
    history = find_latest_generated_slot_image(draft, slot_id)
    if history and str(history.get("url") or "") not in removed and is_publishable_record(history, kind):
        return history
    return first_publishable_candidate(candidates, kind)


def is_publishable_record(record: dict[str, Any], kind: str = "") -> bool:
    if record.get("manual_selected"):
        return True
    source_type = str(record.get("type") or record.get("source_type") or "")
    if kind == "cover":
        return source_type in {"official", "official_screenshot", "generated_cover", "generated_section", "generated", "manual_upload"}
    return source_type in {"official", "official_screenshot", "generated_cover", "generated_section", "generated", "manual_upload", "opinion_screenshot"}


def first_publishable_candidate(candidates: list[dict[str, Any]], kind: str = "") -> dict[str, Any] | None:
    candidate = first_publishable_candidate_item(candidates, kind)
    if candidate:
        return candidate_to_record(candidate, candidate.get("slot_id"))
    return None


def first_publishable_candidate_item(candidates: list[dict[str, Any]], kind: str = "") -> dict[str, Any] | None:
    for candidate in candidates:
        source_type = str(candidate.get("source_type") or candidate.get("type") or "")
        if kind == "cover" and source_type == "generated_section":
            continue
        if candidate.get("publishable"):
            return candidate
    return None


def resolve_duplicate_slot_selections(slots: list[dict[str, Any]]) -> None:
    manual_urls = {
        str((slot.get("selected_image") or {}).get("url") or "")
        for slot in slots
        if isinstance(slot, dict)
        and isinstance(slot.get("selected_image"), dict)
        and slot["selected_image"].get("manual_selected")
        and slot["selected_image"].get("url")
    }
    used_urls: set[str] = set()
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        selected = slot.get("selected_image")
        if not isinstance(selected, dict) or not selected.get("url"):
            continue
        url = str(selected["url"])
        keep_manual = bool(selected.get("manual_selected"))
        if url in used_urls or (url in manual_urls and not keep_manual):
            replace_slot_selection(slot, used_urls | manual_urls)
            selected = slot.get("selected_image")
            if isinstance(selected, dict) and selected.get("url"):
                used_urls.add(str(selected["url"]))
            continue
        used_urls.add(url)


def replace_slot_selection(slot: dict[str, Any], blocked_urls: set[str]) -> None:
    replacement = next(
        (
            candidate
            for candidate in slot.get("candidate_pool", []) or []
            if candidate.get("publishable")
            and candidate.get("url")
            and str(candidate.get("url")) not in blocked_urls
        ),
        None,
    )
    if replacement:
        slot["selected_image"] = candidate_to_record(replacement, str(slot.get("slot_id") or ""))
        selected_url = replacement.get("url")
    else:
        slot["selected_image"] = None
        selected_url = ""
    for candidate in slot.get("candidate_pool", []) or []:
        if isinstance(candidate, dict):
            candidate["selected"] = bool(selected_url and candidate.get("url") == selected_url)


def select_image_candidate(draft: dict[str, Any], image_url: str) -> dict[str, Any]:
    return select_slot_image_candidate(draft, "cover", image_url)


def select_slot_image_candidate(draft: dict[str, Any], slot_id: str, image_url: str) -> dict[str, Any]:
    enable_image_slot(draft, slot_id)
    slots = ensure_image_slots(draft)
    slot = find_slot(slots, slot_id)
    selected = next((candidate for candidate in slot.get("candidate_pool", []) if candidate.get("url") == image_url), None)
    if not selected:
        selected = find_candidate_in_draft(draft, image_url)
        if selected:
            selected = normalize_candidate(selected)
            selected["slot_id"] = slot_id
            upsert_candidate(slot.get("candidate_pool", []), selected, prepend=True)
        else:
            raise ValueError("没有找到这张候选图。")

    record = candidate_to_record(selected, slot_id)
    record["manual_selected"] = True
    record["selected_at"] = datetime.now(timezone.utc).isoformat()
    slot["selected_image"] = record
    for candidate in slot.get("candidate_pool", []):
        candidate["selected"] = candidate.get("url") == image_url
    move_candidate_to_front(slot.get("candidate_pool"), image_url)
    selected["selected"] = True
    selected["slot_id"] = slot_id
    upsert_candidate(draft_candidate_pool(draft), selected, prepend=True)
    if slot_id == "cover":
        draft["cover_image"] = record
    sync_final_images_from_slots(draft)
    mark_candidate_selection(draft)
    return record


def update_slot_image_caption(draft: dict[str, Any], slot_id: str, caption: str) -> dict[str, Any]:
    slots = ensure_image_slots(draft)
    slot = find_slot(slots, slot_id)
    selected = slot.get("selected_image")
    if not isinstance(selected, dict) or not selected.get("url"):
        raise ValueError("这个位置还没有选中的配图。")

    image_url = str(selected.get("url") or "")
    normalized_caption = normalize_image_caption(caption)
    selected["caption"] = normalized_caption

    for candidate in slot.get("candidate_pool", []) or []:
        if isinstance(candidate, dict) and candidate.get("url") == image_url:
            candidate["caption"] = normalized_caption

    for candidate in draft_candidate_pool(draft):
        if isinstance(candidate, dict) and candidate.get("url") == image_url:
            candidate["caption"] = normalized_caption

    if slot_id == "cover" and isinstance(draft.get("cover_image"), dict):
        draft["cover_image"]["caption"] = normalized_caption

    sync_final_images_from_slots(draft)
    mark_candidate_selection(draft)
    return {"slot_id": slot_id, "selected_image": dict(selected), "caption": normalized_caption}


def normalize_image_caption(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def delete_slot_image_candidate(draft: dict[str, Any], slot_id: str, image_url: str) -> dict[str, Any]:
    if not image_url:
        raise ValueError("缺少要删除的图片地址。")
    slots = ensure_image_slots(draft)
    slot = find_slot(slots, slot_id)
    target = find_candidate_or_selected(slot, image_url) or find_candidate_in_draft(draft, image_url)
    if not target:
        raise ValueError("没有找到这张候选图。")

    mark_image_url_removed(draft, image_url)
    remove_url_from_pool(draft.get("image_candidate_pool"), image_url)
    for current_slot in draft.get("image_slots", []) or []:
        if isinstance(current_slot, dict):
            remove_url_from_pool(current_slot.get("candidate_pool"), image_url)

    was_selected = isinstance(slot.get("selected_image"), dict) and slot["selected_image"].get("url") == image_url
    if was_selected:
        disabled = draft.get("disabled_image_slots")
        if not isinstance(disabled, list):
            disabled = []
        if slot_id not in [str(value) for value in disabled]:
            disabled.append(slot_id)
        draft["disabled_image_slots"] = disabled
        slot["selected_image"] = None
        if slot_id == "cover":
            draft.pop("cover_image", None)
    if slot_id == "cover" and isinstance(draft.get("cover_image"), dict) and draft["cover_image"].get("url") == image_url:
        draft.pop("cover_image", None)

    sync_final_images_from_slots(draft)
    mark_candidate_selection(draft)
    return {
        "deleted_url": image_url,
        "deleted_image": candidate_to_record(target, slot_id) if target.get("url") else target,
        "selected_image": slot.get("selected_image") or None,
        "slot_id": slot_id,
    }


def find_candidate_in_draft(draft: dict[str, Any], image_url: str) -> dict[str, Any] | None:
    for candidate in draft.get("image_candidate_pool", []) or []:
        if isinstance(candidate, dict) and candidate.get("url") == image_url:
            return candidate
    for current_slot in draft.get("image_slots", []) or []:
        if not isinstance(current_slot, dict):
            continue
        found = find_candidate_or_selected(current_slot, image_url)
        if found:
            return found
    cover = draft.get("cover_image")
    if isinstance(cover, dict) and cover.get("url") == image_url:
        return record_to_candidate(cover, selected=True, slot_id="cover")
    for image in draft.get("final_images", []) or []:
        if isinstance(image, dict) and image.get("url") == image_url:
            return record_to_candidate(image, selected=True, slot_id=str(image.get("slot_id") or ""))
    return None


def append_generated_candidate(draft: dict[str, Any], record: dict[str, Any], slot_id: str = "cover") -> None:
    enable_image_slot(draft, slot_id)
    candidate = record_to_candidate(record, selected=True, slot_id=slot_id)
    pool = draft.get("image_candidate_pool")
    if not isinstance(pool, list):
        pool = []
    upsert_candidate(pool, candidate, prepend=True)
    draft["image_candidate_pool"] = pool

    slots = ensure_image_slots(draft)
    slot = find_slot(slots, slot_id)
    upsert_candidate(slot["candidate_pool"], candidate, prepend=True)
    slot["selected_image"] = record
    for item in slot["candidate_pool"]:
        item["selected"] = item.get("url") == record.get("url")
    if slot_id == "cover":
        draft["cover_image"] = record
    sync_final_images_from_slots(draft)


def append_slot_candidate(draft: dict[str, Any], record: dict[str, Any], slot_id: str, selected: bool = True) -> None:
    if selected:
        enable_image_slot(draft, slot_id)
    candidate = record_to_candidate(record, selected=selected, slot_id=slot_id)
    pool = draft.get("image_candidate_pool")
    if not isinstance(pool, list):
        pool = []
    upsert_candidate(pool, candidate, prepend=True)
    draft["image_candidate_pool"] = pool

    slots = ensure_image_slots(draft)
    slot = find_slot(slots, slot_id)
    upsert_candidate(slot["candidate_pool"], candidate, prepend=True)
    if selected:
        slot["selected_image"] = record
        for item in slot["candidate_pool"]:
            item["selected"] = item.get("url") == record.get("url")
        if slot_id == "cover":
            draft["cover_image"] = record
        sync_final_images_from_slots(draft)


def find_slot(slots: list[dict[str, Any]], slot_id: str) -> dict[str, Any]:
    for slot in slots:
        if slot.get("slot_id") == slot_id:
            return slot
    raise ValueError("没有找到这个配图位置。")


def sync_final_images_from_slots(draft: dict[str, Any]) -> None:
    images: list[dict[str, Any]] = []
    disabled_slots = disabled_image_slots(draft)
    for slot in draft.get("image_slots", []) or []:
        if not isinstance(slot, dict):
            continue
        slot_id = str(slot.get("slot_id") or "")
        if slot_id in disabled_slots:
            continue
        image = slot.get("selected_image")
        if not isinstance(image, dict) or not image.get("url"):
            continue
        record = dict(image)
        record["slot_id"] = slot_id
        record["slot_label"] = slot.get("label", "")
        images.append(record)
    draft["final_images"] = images
    cover = next((image for image in images if image.get("slot_id") == "cover"), None)
    if cover:
        draft["cover_image"] = cover
    elif "cover_image" in draft:
        draft.pop("cover_image", None)


def mark_candidate_selection(draft: dict[str, Any]) -> None:
    selected_urls = selected_image_urls(draft)
    for candidate in draft.get("image_candidate_pool", []) or []:
        if isinstance(candidate, dict):
            candidate["selected"] = candidate.get("url") in selected_urls
    for slot in draft.get("image_slots", []) or []:
        if not isinstance(slot, dict):
            continue
        selected = slot.get("selected_image") or {}
        selected_url = selected.get("url") if isinstance(selected, dict) else ""
        for candidate in slot.get("candidate_pool", []) or []:
            if isinstance(candidate, dict):
                candidate["selected"] = bool(selected_url and candidate.get("url") == selected_url)


def selected_image_urls(draft: dict[str, Any]) -> set[str]:
    urls: set[str] = set()
    disabled_slots = disabled_image_slots(draft)
    cover = draft.get("cover_image")
    if "cover" not in disabled_slots and isinstance(cover, dict) and cover.get("url"):
        urls.add(str(cover["url"]))
    for image in draft.get("final_images", []) or []:
        if isinstance(image, dict) and str(image.get("slot_id") or "") in disabled_slots:
            continue
        if isinstance(image, dict) and image.get("url"):
            urls.add(str(image["url"]))
    for slot in draft.get("image_slots", []) or []:
        if not isinstance(slot, dict):
            continue
        if str(slot.get("slot_id") or "") in disabled_slots:
            continue
        selected = slot.get("selected_image")
        if isinstance(selected, dict) and selected.get("url"):
            urls.add(str(selected["url"]))
    return urls


def candidate_to_record(candidate: dict[str, Any], slot_id: str | None = None) -> dict[str, Any]:
    return {
        "type": candidate.get("source_type", "image"),
        "prompt": candidate.get("prompt", ""),
        "visual_angle": candidate.get("visual_angle", ""),
        "entities": candidate.get("entities", []),
        "safety_notes": candidate.get("reasons", []),
        "local_path": candidate.get("local_path", ""),
        "url": candidate["url"],
        "generated_at": candidate.get("created_at", ""),
        "slot_id": slot_id or candidate.get("slot_id", ""),
        "source_url": candidate.get("source_url", ""),
        "source_name": candidate.get("source_name", ""),
        "source_title": candidate.get("source_title", ""),
        "source_domain": candidate.get("source_domain", ""),
        "caption": candidate.get("caption", ""),
    }


def record_to_candidate(record: dict[str, Any], selected: bool = False, slot_id: str | None = None) -> dict[str, Any]:
    source_type = str(record.get("type") or record.get("source_type") or "generated_cover")
    label = source_type_label(source_type)
    return {
        "id": stable_candidate_id(str(record["url"])),
        "url": record["url"],
        "local_path": record.get("local_path", ""),
        "source_type": source_type,
        "label": label,
        "publishable": source_type in {"official", "official_screenshot", "generated_cover", "generated_section", "generated", "manual_upload", "opinion_screenshot"},
        "selected": selected,
        "reasons": record.get("safety_notes", []) if isinstance(record.get("safety_notes"), list) else [],
        "prompt": record.get("prompt", ""),
        "visual_angle": record.get("visual_angle", ""),
        "entities": record.get("entities", []),
        "created_at": record.get("generated_at", datetime.now(timezone.utc).isoformat()),
        "slot_id": slot_id or record.get("slot_id", ""),
        "source_url": record.get("source_url", ""),
        "source_name": record.get("source_name", ""),
        "source_title": record.get("source_title", ""),
        "source_domain": record.get("source_domain", ""),
        "caption": record.get("caption", ""),
    }


def normalize_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    source_type = str(candidate.get("source_type") or candidate.get("type") or "image")
    return {
        "id": str(candidate.get("id") or stable_candidate_id(str(candidate.get("url") or ""))),
        "url": candidate.get("url", ""),
        "local_path": candidate.get("local_path", ""),
        "source_type": source_type,
        "label": str(candidate.get("label") or source_type_label(source_type)),
        "publishable": bool(candidate.get("publishable")),
        "selected": bool(candidate.get("selected")),
        "reasons": candidate.get("reasons", []) if isinstance(candidate.get("reasons"), list) else [],
        "prompt": candidate.get("prompt", ""),
        "visual_angle": candidate.get("visual_angle", ""),
        "entities": candidate.get("entities", []),
        "created_at": candidate.get("created_at", ""),
        "slot_id": candidate.get("slot_id", ""),
        "source_url": candidate.get("source_url", ""),
        "source_name": candidate.get("source_name", ""),
        "source_title": candidate.get("source_title", ""),
        "source_domain": candidate.get("source_domain", ""),
        "caption": candidate.get("caption", ""),
    }


def source_type_label(source_type: str) -> str:
    if source_type == "official":
        return "官方图"
    if source_type == "official_screenshot":
        return "官网截图"
    if source_type in {"generated_cover", "generated_section", "generated"}:
        return "生成图"
    if source_type == "manual_upload":
        return "导入图"
    if source_type == "opinion_screenshot":
        return "舆论图"
    if source_type == "media_preview":
        return "媒体图"
    return "候选图"


def upsert_candidate(candidates: list[dict[str, Any]], candidate: dict[str, Any], prepend: bool = False) -> None:
    url = str(candidate.get("url") or "")
    if not url:
        return
    for index, current in enumerate(candidates):
        if isinstance(current, dict) and current.get("url") == url:
            merged = {**current, **candidate}
            candidates.pop(index)
            if prepend:
                candidates.insert(0, merged)
            else:
                candidates.insert(index, merged)
            return
    if prepend:
        candidates.insert(0, candidate)
    else:
        candidates.append(candidate)


def move_candidate_to_front(candidates: Any, image_url: str) -> None:
    if not isinstance(candidates, list):
        return
    for index, candidate in enumerate(candidates):
        if isinstance(candidate, dict) and candidate.get("url") == image_url:
            candidates.insert(0, candidates.pop(index))
            return


def draft_candidate_pool(draft: dict[str, Any]) -> list[dict[str, Any]]:
    pool = draft.get("image_candidate_pool")
    if not isinstance(pool, list):
        pool = []
        draft["image_candidate_pool"] = pool
    return pool


def find_candidate_or_selected(slot: dict[str, Any], image_url: str) -> dict[str, Any] | None:
    for candidate in slot.get("candidate_pool", []) or []:
        if isinstance(candidate, dict) and candidate.get("url") == image_url:
            return candidate
    selected = slot.get("selected_image")
    if isinstance(selected, dict) and selected.get("url") == image_url:
        return record_to_candidate(selected, selected=True, slot_id=str(slot.get("slot_id") or ""))
    return None


def clear_slot_image_selection(draft: dict[str, Any], slot_id: str) -> dict[str, Any]:
    slots = ensure_image_slots(draft)
    slot = find_slot(slots, slot_id)
    disabled = draft.get("disabled_image_slots")
    if not isinstance(disabled, list):
        disabled = []
    if slot_id not in [str(value) for value in disabled]:
        disabled.append(slot_id)
    draft["disabled_image_slots"] = disabled
    slot["selected_image"] = None
    for candidate in slot.get("candidate_pool", []) or []:
        if isinstance(candidate, dict):
            candidate["selected"] = False
    if slot_id == "cover":
        draft.pop("cover_image", None)
    sync_final_images_from_slots(draft)
    mark_candidate_selection(draft)
    return {"slot_id": slot_id, "selected_image": None, "disabled_image_slots": list(disabled)}


def enable_image_slot(draft: dict[str, Any], slot_id: str) -> None:
    disabled = draft.get("disabled_image_slots")
    if not isinstance(disabled, list):
        return
    draft["disabled_image_slots"] = [value for value in disabled if str(value) != slot_id]


def disabled_image_slots(draft: dict[str, Any]) -> set[str]:
    values = draft.get("disabled_image_slots")
    if not isinstance(values, list):
        return set()
    return {str(value) for value in values if str(value).strip()}


def remove_url_from_pool(pool: Any, image_url: str) -> None:
    if not isinstance(pool, list):
        return
    pool[:] = [candidate for candidate in pool if not (isinstance(candidate, dict) and candidate.get("url") == image_url)]


def mark_image_url_removed(draft: dict[str, Any], image_url: str) -> None:
    removed = draft.get("removed_image_urls")
    if not isinstance(removed, list):
        removed = []
    if image_url not in removed:
        removed.append(image_url)
    draft["removed_image_urls"] = removed


def removed_image_urls(draft: dict[str, Any]) -> set[str]:
    return {str(url) for url in draft.get("removed_image_urls", []) or [] if url}


def local_path_for_deleted_image(record: dict[str, Any]) -> str:
    source_type = str(record.get("type") or record.get("source_type") or "")
    if source_type not in {"generated_cover", "generated_section", "generated", "manual_upload", "official_screenshot", "opinion_screenshot"}:
        return ""
    local_path = str(record.get("local_path") or "")
    if local_path:
        return local_path
    url = str(record.get("url") or "")
    if url.startswith("/static-data/generated-images/"):
        return str(Path(__file__).resolve().parents[1] / "data" / "generated_images" / url.removeprefix("/static-data/generated-images/"))
    if url.startswith("/static-data/imported-images/"):
        return str(Path(__file__).resolve().parents[1] / "data" / "imported_images" / url.removeprefix("/static-data/imported-images/"))
    if url.startswith("/static-data/official-screenshots/"):
        return str(Path(__file__).resolve().parents[1] / "data" / "official_screenshots" / url.removeprefix("/static-data/official-screenshots/"))
    if url.startswith("/static-data/opinion-imports/"):
        return str(Path(__file__).resolve().parents[1] / "data" / "opinion_imports" / url.removeprefix("/static-data/opinion-imports/"))
    return ""


def related_source_items(draft: dict[str, Any]) -> list[dict[str, Any]]:
    urls = {item.get("url") for item in draft.get("source_links", []) if isinstance(item, dict)}
    if not urls:
        return []
    return [item for item in load_items() if item.get("url") in urls]


def make_candidate(url: str, source_type: str, label: str, publishable: bool, source_item: dict[str, Any] | None = None) -> dict[str, Any]:
    audit = audit_image_candidate(url, official=source_type == "official")
    source_item = source_item or {}
    caption = media_caption(source_type, source_item)
    return {
        "id": stable_candidate_id(url),
        "url": url,
        "local_path": audit.local_path,
        "source_type": source_type,
        "label": label,
        "publishable": publishable and audit.usable,
        "selected": False,
        "reasons": audit.reasons if audit.reasons else ([] if publishable else ["媒体/自媒体来源，需人工确认版权和水印。"]),
        "prompt": "",
        "visual_angle": "",
        "entities": [],
        "created_at": "",
        "slot_id": "",
        "source_url": source_item.get("url", ""),
        "source_name": source_item.get("source_name", ""),
        "source_title": source_item.get("title", ""),
        "source_domain": "",
        "caption": caption,
    }


def media_caption(source_type: str, source_item: dict[str, Any]) -> str:
    if source_type != "media_preview":
        return ""
    source_name = str(source_item.get("source_name") or "").strip()
    if source_name:
        return f"图片来源：{source_name}"
    return "图片来源：媒体公开页面"


def add_candidate(candidates: list[dict[str, Any]], seen: set[str], candidate: dict[str, Any]) -> None:
    url = str(candidate.get("url") or "")
    if not url or url in seen:
        return
    seen.add(url)
    candidates.append(candidate)


def stable_candidate_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
