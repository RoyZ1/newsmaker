from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx

from app.categories import category_counts, category_options
from app.changelog import load_changelog
from app.collection_profiles import collection_profile_context
from app.config import ROOT_DIR, load_config
from app.cover_images import (
    generate_article_images,
    generate_cover_for_draft,
    generate_images_for_draft_index,
    generate_slot_image_for_draft,
    save_drafts_data,
)
from app.database import database_summary, sync_drafts, sync_topics
from app.draft_store import update_draft_from_payload
from app.formatting import enrich_drafts_layout
from app.image_history import remember_slot_image
from app.imported_images import save_imported_image
from app.image_candidates import (
    append_slot_candidate,
    clear_slot_image_selection,
    delete_slot_image_candidate,
    ensure_image_slots,
    find_slot,
    local_path_for_deleted_image,
    refresh_draft_image_candidates,
    select_image_candidate,
    select_slot_image_candidate,
    update_slot_image_caption,
)
from app.network_status import detect_public_ip
from app.official_screenshots import OfficialScreenshotError, capture_official_screenshots_for_draft_slot
from app.opinion_materials import load_opinion_items
from app.platform_preview import build_draft_variant_previews
from app.platform_variants import set_platform_variant
from app.publication import draft_already_published, mark_draft_published, publication_status_for_drafts, publication_summary
from app.storage import delete_item, delete_item_image, load_items
from app.title_format import title_prefix_context
from app.title_writer import apply_draft_title_choice, rewrite_draft_title
from app.topics import TOPICS_PATH, add_manual_topic, delete_topic, generate_topics, load_topics, update_topic
from app.wechat_export import build_wechat_clipboard_payload, export_draft_for_wechat
from app.heybox_export import build_heybox_clipboard_payload, export_draft_for_heybox
from app.heybox_writer import regenerate_heybox_draft
from app.wechat_sync import sync_draft_to_wechat, wechat_config_status
from app.writer import DRAFTS_PATH, generate_article_draft_dict, generate_article_drafts, load_drafts


GENERATED_IMAGE_DIR = ROOT_DIR / "data" / "generated_images"


class WorkflowError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def dashboard_context() -> dict[str, Any]:
    items = load_items()
    topics = load_topics()
    drafts = load_drafts()
    for draft in drafts:
        refresh_draft_image_candidates(draft)
    if drafts:
        drafts = enrich_drafts_layout(drafts)
    config = load_config()
    return {
        "items": items,
        "topics": topics,
        "drafts": drafts,
        "changelog": load_changelog(),
        "opinions": load_opinion_items(),
        "publication": publication_summary(),
        "database": database_summary(),
        "draft_publication_status": publication_status_for_drafts(drafts),
        "sources": config.get("sources", []),
        "settings": config.get("app", {}),
        "title_prefix": title_prefix_context(),
        "collection_profile": collection_profile_context(),
        "category_counts": category_counts(items),
        "category_options": category_options(),
        "status": workflow_status(items, topics, drafts),
    }


def workflow_status(items: list[dict[str, Any]], topics: list[dict[str, Any]], drafts: list[dict[str, Any]]) -> dict[str, Any]:
    generated_covers = sum(
        len([slot for slot in draft.get("image_slots", []) or [] if isinstance(slot, dict) and (slot.get("selected_image") or {}).get("url")])
        for draft in drafts
    )
    return {
        "has_items": bool(items),
        "has_topics": bool(topics),
        "has_drafts": bool(drafts),
        "item_count": len(items),
        "topic_count": len(topics),
        "draft_count": len(drafts),
        "generated_cover_count": generated_covers,
        "next_step": next_step(items, topics, drafts),
    }


def next_step(items: list[dict[str, Any]], topics: list[dict[str, Any]], drafts: list[dict[str, Any]]) -> str:
    if not items:
        return "先采集新闻"
    if not topics:
        return "生成选题"
    if not drafts:
        return "生成文章草稿"
    if any(not draft.get("cover_image", {}).get("url") for draft in drafts):
        return "生成或检查封面图"
    return "人工审稿"


def generate_topics_checked() -> list[Any]:
    if not load_items():
        raise WorkflowError("还没有采集信息，请先点击“采集新闻”。")
    topics = generate_topics()
    if not topics:
        raise WorkflowError("没有生成可用选题。请检查采集结果是否为空、是否都被去重或时间过滤。")
    return topics


def generate_drafts_checked(topic_ids: list[str] | None = None) -> list[Any]:
    topics = load_topics()
    if not topics:
        raise WorkflowError("还没有选题，请先点击“生成选题”。")
    selected_topics = select_topics_for_draft_generation(topics, topic_ids)
    generate_article_drafts(max_drafts=len(selected_topics), topics=selected_topics)
    drafts = load_drafts()
    duplicates = [draft for draft in drafts if draft_already_published(draft)]
    if duplicates:
        titles = "、".join(str(draft.get("title") or "") for draft in duplicates[:3])
        raise WorkflowError(f"草稿已生成，但检测到可能重复发布的文章：{titles}。请先查看发布记录或更换选题。", status_code=409)
    return drafts


def select_topics_for_draft_generation(topics: list[dict[str, Any]], topic_ids: list[str] | None = None) -> list[dict[str, Any]]:
    if topic_ids is None:
        return topics[:3]

    normalized_ids: list[str] = []
    seen: set[str] = set()
    for raw_id in topic_ids:
        topic_id = str(raw_id or "").strip()
        if not topic_id or topic_id in seen:
            continue
        normalized_ids.append(topic_id)
        seen.add(topic_id)

    if not normalized_ids:
        raise WorkflowError("请至少选择一个选题后再生成草稿。")

    by_id = {str(topic.get("id") or ""): topic for topic in topics}
    missing = [topic_id for topic_id in normalized_ids if topic_id not in by_id]
    if missing:
        raise WorkflowError("有选题已不存在，请刷新页面后重新选择。", status_code=404)
    return [by_id[topic_id] for topic_id in normalized_ids]


def add_topic_checked(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        topic = add_manual_topic(payload)
    except ValueError as exc:
        raise WorkflowError(str(exc), status_code=400) from exc
    clear_drafts_after_source_change()
    return topic


def update_topic_checked(topic_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        topic = update_topic(topic_id, payload)
    except KeyError as exc:
        raise WorkflowError("没有找到这个选题，可能已经被删除。", status_code=404) from exc
    except ValueError as exc:
        raise WorkflowError(str(exc), status_code=400) from exc
    clear_drafts_after_source_change()
    return topic


def delete_topic_checked(topic_id: str) -> None:
    if not delete_topic(topic_id):
        raise WorkflowError("没有找到这个选题，可能已经被删除。", status_code=404)
    clear_drafts_after_source_change()


def regenerate_single_draft_checked(draft_index: int) -> dict[str, Any]:
    topics = load_topics()
    if not topics:
        raise WorkflowError("还没有选题，请先点击“生成选题”。")

    drafts = load_drafts()
    if draft_index < 0 or draft_index >= len(drafts):
        raise WorkflowError("没有找到这篇草稿，可能已经被删除。", status_code=404)

    old_draft = drafts[draft_index]
    topic = topic_for_draft(old_draft, topics, draft_index)
    topic = attach_draft_context_to_topic(topic, old_draft, draft_index)
    new_draft = generate_article_draft_dict(topic)
    preserve_draft_image_state(old_draft, new_draft)
    drafts[draft_index] = new_draft
    drafts = enrich_drafts_layout(drafts)

    try:
        ensure_image_slots(drafts[draft_index])
    except Exception as exc:  # noqa: BLE001
        save_json(DRAFTS_PATH, drafts)
        raise WorkflowError(f"这篇文章文本已重新生成，但配图状态刷新失败：{format_exception(exc)}", status_code=502) from exc

    save_drafts_data(drafts)
    return {"draft": drafts[draft_index], "covers": []}


def preserve_draft_image_state(old_draft: dict[str, Any], new_draft: dict[str, Any]) -> None:
    for key in (
        "draft_id",
        "created_at",
        "cover_image",
        "final_images",
        "image_candidate_pool",
        "image_slots",
        "disabled_image_slots",
        "removed_image_urls",
    ):
        if key in old_draft:
            new_draft[key] = old_draft[key]


def rewrite_draft_title_checked(draft_index: int) -> dict[str, Any]:
    try:
        return rewrite_draft_title(draft_index)
    except IndexError as exc:
        raise WorkflowError(str(exc), status_code=404) from exc
    except Exception as exc:  # noqa: BLE001
        raise WorkflowError(f"标题重写失败：{format_exception(exc)}", status_code=502) from exc


def apply_draft_title_choice_checked(draft_index: int, payload: dict[str, Any]) -> dict[str, Any]:
    subtitle = str(payload.get("subtitle") or "") if "subtitle" in payload else None
    try:
        return apply_draft_title_choice(
            draft_index,
            title=str(payload.get("title") or ""),
            subtitle=subtitle,
        )
    except IndexError as exc:
        raise WorkflowError(str(exc), status_code=404) from exc
    except ValueError as exc:
        raise WorkflowError(str(exc), status_code=400) from exc
    except Exception as exc:  # noqa: BLE001
        raise WorkflowError(f"应用标题失败：{format_exception(exc)}", status_code=502) from exc


def topic_for_draft(old_draft: dict[str, Any], topics: list[dict[str, Any]], draft_index: int) -> dict[str, Any]:
    topic_id = str(old_draft.get("topic_id") or "")
    if topic_id:
        for topic in topics:
            if str(topic.get("id") or "") == topic_id:
                return topic
    if draft_index < len(topics):
        return topics[draft_index]
    raise WorkflowError("没有找到这篇草稿对应的选题，请重新生成选题后再试。", status_code=404)


def attach_draft_context_to_topic(topic: dict[str, Any], draft: dict[str, Any], draft_index: int) -> dict[str, Any]:
    enriched = dict(topic)
    enriched["draft_index"] = draft_index
    if draft.get("draft_id"):
        enriched["draft_id"] = str(draft.get("draft_id") or "")
    if draft.get("title"):
        enriched["draft_title"] = str(draft.get("title") or "")
    if draft.get("topic_id") and not enriched.get("topic_id"):
        enriched["topic_id"] = str(draft.get("topic_id") or "")
    return enriched


def generate_covers_checked(force: bool = False) -> list[dict[str, Any]]:
    if not load_drafts():
        raise WorkflowError("还没有文章草稿，请先点击“生成草稿”。")
    covers = generate_article_images(force=force)
    return covers


def regenerate_draft_image_candidate(draft_index: int) -> dict[str, Any]:
    drafts = load_drafts()
    if draft_index < 0 or draft_index >= len(drafts):
        raise WorkflowError("没有找到这篇草稿，可能已经被删除。", status_code=404)
    result = generate_cover_for_draft(drafts[draft_index], draft_index)
    save_drafts_data(drafts)
    return result


def regenerate_draft_slot_image_candidate(draft_index: int, slot_id: str) -> dict[str, Any]:
    drafts = load_drafts()
    if draft_index < 0 or draft_index >= len(drafts):
        raise WorkflowError("没有找到这篇草稿，可能已经被删除。", status_code=404)
    if not slot_id:
        raise WorkflowError("缺少要重新生成的配图位置。")
    try:
        ensure_image_slots(drafts[draft_index])
        result = generate_slot_image_for_draft(drafts[draft_index], draft_index, slot_id)
    except ValueError as exc:
        raise WorkflowError(str(exc), status_code=404) from exc
    save_drafts_data(drafts)
    return result


def select_draft_image_candidate(draft_index: int, image_url: str) -> dict[str, Any]:
    drafts = load_drafts()
    if draft_index < 0 or draft_index >= len(drafts):
        raise WorkflowError("没有找到这篇草稿，可能已经被删除。", status_code=404)
    if not image_url:
        raise WorkflowError("缺少要选择的图片地址。")
    try:
        record = select_image_candidate(drafts[draft_index], image_url)
    except ValueError as exc:
        raise WorkflowError(str(exc), status_code=404) from exc
    save_drafts_data(drafts)
    return record


def select_draft_slot_image_candidate(draft_index: int, slot_id: str, image_url: str) -> dict[str, Any]:
    drafts = load_drafts()
    if draft_index < 0 or draft_index >= len(drafts):
        raise WorkflowError("没有找到这篇草稿，可能已经被删除。", status_code=404)
    if not slot_id:
        raise WorkflowError("缺少要选择的配图位置。")
    if not image_url:
        raise WorkflowError("缺少要选择的图片地址。")
    try:
        record = select_slot_image_candidate(drafts[draft_index], slot_id, image_url)
    except ValueError as exc:
        raise WorkflowError(str(exc), status_code=404) from exc
    save_drafts_data(drafts)
    return record


def update_draft_slot_image_caption(draft_index: int, slot_id: str, caption: str) -> dict[str, Any]:
    drafts = load_drafts()
    if draft_index < 0 or draft_index >= len(drafts):
        raise WorkflowError("没有找到这篇草稿，可能已经被删除。", status_code=404)
    if not slot_id:
        raise WorkflowError("缺少要保存图解的配图位置。")
    try:
        result = update_slot_image_caption(drafts[draft_index], slot_id, caption)
    except ValueError as exc:
        raise WorkflowError(str(exc), status_code=404) from exc
    save_drafts_data(drafts)
    return result


def delete_draft_slot_image_candidate(draft_index: int, slot_id: str, image_url: str) -> dict[str, Any]:
    drafts = load_drafts()
    if draft_index < 0 or draft_index >= len(drafts):
        raise WorkflowError("没有找到这篇草稿，可能已经被删除。", status_code=404)
    if not slot_id:
        raise WorkflowError("缺少要删除图片的配图位置。")
    if not image_url:
        raise WorkflowError("缺少要删除的图片地址。")
    try:
        result = delete_slot_image_candidate(drafts[draft_index], slot_id, image_url)
    except ValueError as exc:
        raise WorkflowError(str(exc), status_code=404) from exc

    local_path = local_path_for_deleted_image(result.get("deleted_image") or {})
    result["local_file_deleted"] = remove_local_image_if_unreferenced(local_path, image_url, drafts)
    save_drafts_data(drafts)
    return result


def clear_draft_slot_image_selection(draft_index: int, slot_id: str) -> dict[str, Any]:
    drafts = load_drafts()
    if draft_index < 0 or draft_index >= len(drafts):
        raise WorkflowError("没有找到这篇草稿，可能已经被删除。", status_code=404)
    if not slot_id:
        raise WorkflowError("缺少要清空的配图位置。")
    try:
        result = clear_slot_image_selection(drafts[draft_index], slot_id)
    except ValueError as exc:
        raise WorkflowError(str(exc), status_code=404) from exc
    save_drafts_data(drafts)
    return result


def import_draft_slot_image(draft_index: int, slot_id: str, stream: Any, filename: str) -> dict[str, Any]:
    drafts = load_drafts()
    if draft_index < 0 or draft_index >= len(drafts):
        raise WorkflowError("没有找到这篇草稿，可能已经被删除。", status_code=404)
    if not slot_id:
        raise WorkflowError("缺少要导入图片的位置。")
    try:
        ensure_image_slots(drafts[draft_index])
        find_slot = next((slot for slot in drafts[draft_index].get("image_slots", []) if isinstance(slot, dict) and slot.get("slot_id") == slot_id), None)
        if not find_slot:
            raise ValueError("没有找到这个配图位置。")
        record = append_imported_image_record(drafts[draft_index], draft_index, slot_id, stream, filename, str(find_slot.get("label") or ""))
        selected = select_slot_image_candidate(drafts[draft_index], slot_id, record["url"])
        remember_slot_image(drafts[draft_index], slot_id, selected)
    except ValueError as exc:
        raise WorkflowError(str(exc), status_code=400) from exc
    save_drafts_data(drafts)
    return selected


def import_draft_slot_images(draft_index: int, slot_id: str, uploads: list[tuple[Any, str]]) -> dict[str, Any]:
    drafts = load_drafts()
    if draft_index < 0 or draft_index >= len(drafts):
        raise WorkflowError("没有找到这篇草稿，可能已经被删除。", status_code=404)
    if not slot_id:
        raise WorkflowError("缺少要导入图片的位置。")
    if not uploads:
        raise WorkflowError("请选择要导入的图片。")
    try:
        ensure_image_slots(drafts[draft_index])
        find_slot = next((slot for slot in drafts[draft_index].get("image_slots", []) if isinstance(slot, dict) and slot.get("slot_id") == slot_id), None)
        if not find_slot:
            raise ValueError("没有找到这个配图位置。")
        slot_label = str(find_slot.get("label") or "")
        records = [
            append_imported_image_record(drafts[draft_index], draft_index, slot_id, stream, filename, slot_label)
            for stream, filename in uploads
        ]
        selected = select_slot_image_candidate(drafts[draft_index], slot_id, records[0]["url"])
        remember_slot_image(drafts[draft_index], slot_id, selected)
    except ValueError as exc:
        raise WorkflowError(str(exc), status_code=400) from exc
    save_drafts_data(drafts)
    return {"selected_image": selected, "images": records, "count": len(records)}


def append_imported_image_record(
    draft: dict[str, Any],
    draft_index: int,
    slot_id: str,
    stream: Any,
    filename: str,
    slot_label: str = "",
) -> dict[str, Any]:
    record = save_imported_image(stream, filename, draft_index, slot_id)
    record["slot_label"] = slot_label
    pool = draft.get("image_candidate_pool")
    if not isinstance(pool, list):
        pool = []
    draft["image_candidate_pool"] = pool
    candidate = {
        "id": record["url"],
        "url": record["url"],
        "local_path": record["local_path"],
        "source_type": "manual_upload",
        "label": "导入图",
        "publishable": True,
        "selected": False,
        "reasons": record["safety_notes"],
        "prompt": "",
        "visual_angle": record["visual_angle"],
        "entities": [],
        "created_at": record["generated_at"],
        "slot_id": slot_id,
    }
    pool.insert(0, candidate)
    record.update(candidate)
    record["slot_label"] = slot_label
    return record


def capture_draft_slot_official_screenshot(draft_index: int, slot_id: str) -> dict[str, Any]:
    drafts = load_drafts()
    if draft_index < 0 or draft_index >= len(drafts):
        raise WorkflowError("没有找到这篇草稿，可能已经被删除。", status_code=404)
    if not slot_id:
        raise WorkflowError("缺少要截图的配图位置。")
    draft = drafts[draft_index]
    try:
        slots = ensure_image_slots(draft)
        slot = find_slot(slots, slot_id)
        records = capture_official_screenshots_for_draft_slot(draft, draft_index, slot, count=3)
        if not records:
            raise OfficialScreenshotError("没有截取到可用官网图片，请稍后重试或手动导入图片。")
        for index, record in enumerate(records):
            append_slot_candidate(draft, record, slot_id, selected=index == 0)
        selected = select_slot_image_candidate(draft, slot_id, records[0]["url"])
        remember_slot_image(draft, slot_id, selected)
    except OfficialScreenshotError as exc:
        raise WorkflowError(str(exc), status_code=400) from exc
    except ValueError as exc:
        raise WorkflowError(str(exc), status_code=404) from exc
    save_drafts_data(drafts)
    return {"selected_image": selected, "candidates": records}


def mark_draft_as_published(draft_index: int, channel: str = "manual", note: str = "") -> dict[str, Any]:
    drafts = load_drafts()
    if draft_index < 0 or draft_index >= len(drafts):
        raise WorkflowError("没有找到这篇草稿，可能已经被删除。", status_code=404)
    draft = enrich_drafts_layout([drafts[draft_index]])[0]
    try:
        return mark_draft_published(draft, channel=channel, note=note)
    except ValueError as exc:
        raise WorkflowError(str(exc), status_code=409) from exc


def save_draft_edits(draft_index: int, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return update_draft_from_payload(draft_index, payload)
    except IndexError as exc:
        raise WorkflowError(str(exc), status_code=404) from exc
    except ValueError as exc:
        raise WorkflowError(str(exc), status_code=400) from exc


def export_draft_wechat_format(draft_index: int, public_base_url: str) -> dict[str, Any]:
    try:
        return export_draft_for_wechat(draft_index, public_base_url=public_base_url)
    except IndexError as exc:
        raise WorkflowError(str(exc), status_code=404) from exc


def build_draft_wechat_clipboard(draft_index: int, public_base_url: str) -> dict[str, Any]:
    try:
        return build_wechat_clipboard_payload(draft_index, public_base_url=public_base_url)
    except IndexError as exc:
        raise WorkflowError(str(exc), status_code=404) from exc


def export_draft_heybox_format(draft_index: int, public_base_url: str) -> dict[str, Any]:
    try:
        return export_draft_for_heybox(draft_index, public_base_url=public_base_url)
    except IndexError as exc:
        raise WorkflowError(str(exc), status_code=404) from exc


def build_draft_heybox_clipboard(draft_index: int, public_base_url: str) -> dict[str, Any]:
    try:
        return build_heybox_clipboard_payload(draft_index, public_base_url=public_base_url)
    except IndexError as exc:
        raise WorkflowError(str(exc), status_code=404) from exc


def regenerate_draft_heybox_copy(draft_index: int) -> dict[str, Any]:
    try:
        return regenerate_heybox_draft(draft_index)
    except IndexError as exc:
        raise WorkflowError(str(exc), status_code=404) from exc
    except Exception as exc:  # noqa: BLE001
        raise WorkflowError(f"短文版生成失败：{format_exception(exc)}", status_code=502) from exc


def draft_variant_previews_checked(draft_index: int, public_base_url: str) -> dict[str, Any]:
    try:
        return build_draft_variant_previews(draft_index, public_base_url=public_base_url)
    except IndexError as exc:
        raise WorkflowError(str(exc), status_code=404) from exc
    except Exception as exc:  # noqa: BLE001
        raise WorkflowError(f"生成版本预览失败：{format_exception(exc)}", status_code=502) from exc


def select_draft_platform_variant(draft_index: int, platform: str, variant: str) -> dict[str, Any]:
    drafts = load_drafts()
    if draft_index < 0 or draft_index >= len(drafts):
        raise WorkflowError("没有找到这篇草稿，可能已经被删除。", status_code=404)
    draft = drafts[draft_index]
    from app.draft_store import ensure_draft_identity

    draft_id = ensure_draft_identity(draft, draft_index)
    try:
        return set_platform_variant(
            draft_id,
            platform=platform,
            variant=variant,
            draft_index=draft_index,
            title=str(draft.get("title") or ""),
        )
    except ValueError as exc:
        raise WorkflowError(str(exc), status_code=400) from exc


def wechat_status_checked(check_token: bool = False) -> dict[str, Any]:
    return wechat_config_status(check_token=check_token)


def sync_draft_to_wechat_checked(draft_index: int, public_base_url: str) -> dict[str, Any]:
    try:
        return sync_draft_to_wechat(draft_index, public_base_url=public_base_url)
    except IndexError as exc:
        raise WorkflowError(str(exc), status_code=404) from exc
    except Exception as exc:  # noqa: BLE001
        raise WorkflowError(append_public_ip_hint(format_exception(exc)), status_code=502) from exc


def refresh_all_draft_image_candidates() -> None:
    drafts = load_drafts()
    for draft in drafts:
        refresh_draft_image_candidates(draft)
    if drafts:
        save_json(DRAFTS_PATH, enrich_drafts_layout(drafts))


def delete_news_item(item_id: str) -> None:
    if not delete_item(item_id):
        raise WorkflowError("没有找到这条新闻，可能已经被删除。", status_code=404)
    regenerate_topics_after_item_change()
    clear_drafts_after_source_change()


def delete_news_image(item_id: str, image: str) -> None:
    if not delete_item_image(item_id, image):
        raise WorkflowError("没有找到这张图片，可能已经被删除。", status_code=404)
    regenerate_topics_after_item_change()
    clear_drafts_after_source_change()


def delete_draft(draft_index: int) -> None:
    drafts = load_drafts()
    if draft_index < 0 or draft_index >= len(drafts):
        raise WorkflowError("没有找到这篇草稿，可能已经被删除。", status_code=404)
    drafts.pop(draft_index)
    save_json(DRAFTS_PATH, enrich_drafts_layout(drafts))


def delete_draft_cover(draft_index: int) -> None:
    drafts = load_drafts()
    if draft_index < 0 or draft_index >= len(drafts):
        raise WorkflowError("没有找到这篇草稿，可能已经被删除。", status_code=404)
    draft = drafts[draft_index]
    cover = draft.pop("cover_image", None)
    for slot in draft.get("image_slots", []) or []:
        if isinstance(slot, dict) and slot.get("slot_id") == "cover":
            cover = cover or slot.get("selected_image")
            slot["selected_image"] = None
            for candidate in slot.get("candidate_pool", []) or []:
                if isinstance(candidate, dict):
                    candidate["selected"] = False
    if not cover:
        raise WorkflowError("这篇草稿没有生成封面。", status_code=404)
    local_path = cover.get("local_path")
    remove_local_generated_image(local_path)
    draft["final_images"] = [
        image
        for image in draft.get("final_images", []) or []
        if not (isinstance(image, dict) and image.get("slot_id") == "cover")
    ]
    draft["image_plan"] = [
        item
        for item in draft.get("image_plan", [])
        if not (isinstance(item, dict) and item.get("type") == "generated_cover" and item.get("position") == "article_cover")
    ]
    save_json(DRAFTS_PATH, enrich_drafts_layout(drafts))


def regenerate_topics_after_item_change() -> None:
    if load_items():
        generate_topics()
    elif TOPICS_PATH.exists():
        save_json(TOPICS_PATH, [])


def clear_drafts_after_source_change() -> None:
    if DRAFTS_PATH.exists():
        save_json(DRAFTS_PATH, [])


def remove_local_generated_image(local_path: str | None) -> None:
    if not local_path:
        return
    path = Path(local_path)
    try:
        resolved = path.resolve()
        image_dir = GENERATED_IMAGE_DIR.resolve()
    except OSError:
        return
    if image_dir in resolved.parents and resolved.exists():
        resolved.unlink()


def remove_local_image_if_unreferenced(local_path: str | None, image_url: str, drafts: list[dict[str, Any]]) -> bool:
    if not local_path:
        return False
    if image_still_referenced(image_url, local_path, drafts):
        return False
    path = Path(local_path)
    try:
        resolved = path.resolve()
        allowed_dirs = [
            (ROOT_DIR / "data" / "generated_images").resolve(),
            (ROOT_DIR / "data" / "imported_images").resolve(),
            (ROOT_DIR / "data" / "official_screenshots").resolve(),
        ]
    except OSError:
        return False
    if not any(directory in resolved.parents for directory in allowed_dirs):
        return False
    if not resolved.exists():
        return False
    resolved.unlink()
    return True


def image_still_referenced(image_url: str, local_path: str, drafts: list[dict[str, Any]]) -> bool:
    for draft in drafts:
        for record in draft_image_records(draft):
            if record.get("url") == image_url or (local_path and record.get("local_path") == local_path):
                return True
    return False


def draft_image_records(draft: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    cover = draft.get("cover_image")
    if isinstance(cover, dict):
        records.append(cover)
    for image in draft.get("final_images", []) or []:
        if isinstance(image, dict):
            records.append(image)
    for image in draft.get("image_candidate_pool", []) or []:
        if isinstance(image, dict):
            records.append(image)
    for slot in draft.get("image_slots", []) or []:
        if not isinstance(slot, dict):
            continue
        selected = slot.get("selected_image")
        if isinstance(selected, dict):
            records.append(selected)
        for image in slot.get("candidate_pool", []) or []:
            if isinstance(image, dict):
                records.append(image)
    return records


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    if path == DRAFTS_PATH and isinstance(payload, list):
        sync_drafts(payload)
    elif path == TOPICS_PATH and isinstance(payload, list):
        sync_topics(payload)


def format_exception(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        url = exc.request.url
        if status in {401, 403}:
            return f"API 鉴权失败（HTTP {status}），请检查 .env 中的 API Key、模型权限或 Base URL：{url}"
        if status == 404:
            return f"API 地址或模型不存在（HTTP 404），请检查 Base URL 和模型 ID：{url}"
        return f"API 请求失败（HTTP {status}）：{url}"
    if isinstance(exc, httpx.TimeoutException):
        return "API 请求超时，请检查网络，或调大 .env / config 中的 timeout 配置。"
    if isinstance(exc, httpx.RemoteProtocolError):
        return "模型 API 服务端中途断开连接，通常是上游接口临时不稳定或生成耗时过长。系统已支持自动重试；如果仍失败，请稍后再试或减少本次生成的选题数量。"
    if isinstance(exc, httpx.NetworkError):
        return "网络连接失败，请检查本机网络、代理、DNS 或 API Base URL。"
    if isinstance(exc, httpx.TransportError):
        return f"模型 API 连接异常：{str(exc).strip() or exc.__class__.__name__}。请检查网络、代理或 API Base URL。"
    if isinstance(exc, httpx.InvalidURL):
        return "API Base URL 格式错误，请检查 .env。"
    text = str(exc).strip()
    return text or exc.__class__.__name__


def append_public_ip_hint(message: str) -> str:
    if "40164" not in message and "invalid ip" not in message.lower() and "白名单" not in message:
        return message
    wechat_ips = extract_wechat_invalid_ips(message)
    if wechat_ips:
        ip_text = "、".join(wechat_ips)
        return f"{message} 微信接口实际识别到的出口 IP：{ip_text}。请优先把这个 IP 加到公众号后台 IP 白名单。"
    public_ip = detect_public_ip()
    ip = public_ip.get("ip") or ""
    if not ip:
        return f"{message} 当前公网 IP 检测失败，请在“发布记录”页点击“检测端口”重试。"
    return f"{message} 当前公网 IP：{ip}。请把这个 IP 加到公众号后台 IP 白名单。"


def extract_wechat_invalid_ips(message: str) -> list[str]:
    match = re.search(r"invalid ip\s+([0-9A-Fa-f:.]+)", message or "", flags=re.I)
    if not match:
        return []
    candidates = re.findall(r"(?:\d{1,3}\.){3}\d{1,3}|[0-9A-Fa-f:]{3,}", match.group(1))
    results: list[str] = []
    for candidate in candidates:
        if candidate.startswith("::ffff:"):
            candidate = candidate.removeprefix("::ffff:")
        if candidate and candidate not in results:
            results.append(candidate)
    return results
