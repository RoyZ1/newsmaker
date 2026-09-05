from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from PIL import Image, ImageOps

from app.config import ROOT_DIR
from app.draft_store import ensure_draft_identity
from app.formatting import enrich_drafts_layout
from app.image_candidates import ensure_image_slots
from app.wechat_client import WeChatAPIError, WeChatClient, load_wechat_config, test_wechat_access_token, wechat_status
from app.wechat_export import (
    apply_article_variant,
    build_image_manifest,
    build_plain_text_article,
    build_wechat_api_payload,
    clamp_text_bytes,
    render_wechat_content_html,
)
from app.platform_variants import selected_article_variant
from app.writer import load_drafts


WECHAT_SYNC_DIR = ROOT_DIR / "data" / "wechat_sync"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def sync_draft_to_wechat(draft_index: int, public_base_url: str = "http://127.0.0.1:5050") -> dict[str, Any]:
    drafts = load_drafts()
    if draft_index < 0 or draft_index >= len(drafts):
        raise IndexError("没有找到这篇草稿，可能已经被删除。")

    config = load_wechat_config()
    draft = enrich_drafts_layout([drafts[draft_index]])[0]
    draft_id = ensure_draft_identity(draft, draft_index)
    ensure_image_slots(draft)
    variant = selected_article_variant(draft_id, default="long")
    draft = apply_article_variant(draft, draft_index, variant)
    manifest = build_image_manifest(draft, public_base_url, include_extra_slots=variant == "short", max_images=4 if variant == "short" else None)
    if not manifest:
        raise RuntimeError("这篇草稿没有可同步的配图，请先生成或选择配图。")

    prepared = prepare_wechat_images(manifest, config.image_width, config.image_max_bytes)
    with WeChatClient(config) as client:
        uploaded_by_slot: dict[str, str] = {}
        for item in prepared:
            item["wechat_url"] = client.upload_article_image(Path(item["prepared_path"]))
            uploaded_by_slot[str(item["slot_id"])] = str(item["wechat_url"])

        cover = next((item for item in prepared if item.get("slot_id") == "cover"), prepared[0])
        thumb_media_id = client.upload_permanent_image_material(Path(cover["prepared_path"]))
        api_content = render_wechat_content_html(draft, manifest, image_mode="wechat-placeholder")
        api_content = replace_image_placeholders(api_content, manifest, uploaded_by_slot)
        payload = build_wechat_api_payload(draft, api_content, manifest)
        article = payload["draft_add_payload"]["articles"][0]
        article["author"] = config.author
        article["thumb_media_id"] = thumb_media_id
        article["digest"] = make_digest(draft)
        article_debug = {
            "title": article.get("title", ""),
            "title_bytes": len(str(article.get("title", "")).encode("utf-8")),
            "digest": article.get("digest", ""),
            "digest_bytes": len(str(article.get("digest", "")).encode("utf-8")),
        }
        try:
            result = client.add_draft(article)
        except WeChatAPIError as exc:
            raise RuntimeError(
                f"{exc}；当前提交标题：{article_debug['title']}（{article_debug['title_bytes']} 字节），"
                f"摘要 {article_debug['digest_bytes']} 字节。"
            ) from exc

    record = {
        "draft_index": draft_index,
        "draft_id": draft.get("draft_id", ""),
        "title": draft.get("title", ""),
        "variant": variant,
        "wechat_title": article.get("title", ""),
        "article_debug": article_debug,
        "synced_at": datetime.now(LOCAL_TZ).isoformat(),
        "wechat_media_id": result.get("media_id", ""),
        "thumb_media_id": thumb_media_id,
        "image_count": len(prepared),
        "prepared_images": prepared,
        "article_payload": article,
    }
    save_sync_record(record)
    return sanitize_sync_result(record)


def wechat_config_status(check_token: bool = False) -> dict[str, Any]:
    status = wechat_status()
    status["sync_dir"] = str(WECHAT_SYNC_DIR)
    if check_token:
        status["access_token_test"] = test_wechat_access_token()
    return status


def prepare_wechat_images(manifest: list[dict[str, Any]], image_width: int, max_bytes: int) -> list[dict[str, Any]]:
    now = datetime.now(LOCAL_TZ)
    sync_dir = WECHAT_SYNC_DIR / now.date().isoformat() / now.strftime("%H%M%S")
    sync_dir.mkdir(parents=True, exist_ok=True)
    prepared: list[dict[str, Any]] = []
    for index, item in enumerate(manifest, start=1):
        source = source_path_from_manifest(item)
        if not source or not source.exists():
            raise FileNotFoundError(f"没有找到本地图片文件：{item.get('label') or item.get('slot_id')}")
        slot_id = safe_name(str(item.get("slot_id") or f"image-{index}"))
        output_path = sync_dir / f"{index:02d}-{slot_id}.jpg"
        export_wechat_image(source, output_path, image_width, max_bytes)
        prepared.append(
            {
                "slot_id": item.get("slot_id", ""),
                "label": item.get("label", ""),
                "source_path": str(source),
                "prepared_path": str(output_path),
                "prepared_bytes": output_path.stat().st_size,
            }
        )
    return prepared


def source_path_from_manifest(item: dict[str, Any]) -> Path | None:
    local_path = str(item.get("local_path") or "").strip()
    if local_path:
        return Path(local_path)
    preview_url = str(item.get("local_preview_url") or "")
    if "/static-data/images/" in preview_url:
        return ROOT_DIR / "data" / "images" / preview_url.rsplit("/static-data/images/", 1)[1]
    if "/static-data/generated-images/" in preview_url:
        return ROOT_DIR / "data" / "generated_images" / preview_url.rsplit("/static-data/generated-images/", 1)[1]
    if "/static-data/imported-images/" in preview_url:
        return ROOT_DIR / "data" / "imported_images" / preview_url.rsplit("/static-data/imported-images/", 1)[1]
    if "/static-data/official-screenshots/" in preview_url:
        return ROOT_DIR / "data" / "official_screenshots" / preview_url.rsplit("/static-data/official-screenshots/", 1)[1]
    return None


def export_wechat_image(source: Path, output_path: Path, image_width: int, max_bytes: int) -> None:
    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image)
        image = image.convert("RGB")
        if image.width > image_width:
            ratio = image_width / image.width
            image = image.resize((image_width, max(1, int(image.height * ratio))), Image.Resampling.LANCZOS)
        quality = 92
        while quality >= 55:
            image.save(output_path, format="JPEG", quality=quality, optimize=True)
            if output_path.stat().st_size <= max_bytes:
                return
            quality -= 8
        if output_path.stat().st_size > max_bytes:
            ratio = 0.86
            while output_path.stat().st_size > max_bytes and image.width > 360:
                image = image.resize((max(1, int(image.width * ratio)), max(1, int(image.height * ratio))), Image.Resampling.LANCZOS)
                image.save(output_path, format="JPEG", quality=60, optimize=True)


def replace_image_placeholders(content_html: str, manifest: list[dict[str, Any]], uploaded_by_slot: dict[str, str]) -> str:
    result = content_html
    for item in manifest:
        slot_id = str(item.get("slot_id") or "")
        placeholder = str(item.get("wechat_image_placeholder") or "")
        url = uploaded_by_slot.get(slot_id)
        if placeholder and url:
            result = result.replace(html.escape(placeholder), html.escape(url))
            result = result.replace(placeholder, url)
    return result


def make_digest(draft: dict[str, Any]) -> str:
    subtitle = str(draft.get("subtitle") or "").strip()
    if subtitle:
        return clamp_text_bytes(subtitle, 120)
    text = re.sub(r"[#*_`>\-\s]+", " ", build_plain_text_article(draft)).strip()
    return clamp_text_bytes(text, 120)


def save_sync_record(record: dict[str, Any]) -> Path:
    date_key = datetime.now(LOCAL_TZ).date().isoformat()
    raw = f"{record.get('draft_id', '')}:{record.get('synced_at', '')}:{record.get('wechat_media_id', '')}"
    digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:12]
    path = WECHAT_SYNC_DIR / date_key / f"{digest}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2)
    return path


def sanitize_sync_result(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "draft_index": record.get("draft_index"),
        "draft_id": record.get("draft_id"),
        "title": record.get("title", ""),
        "wechat_title": record.get("wechat_title", ""),
        "article_debug": record.get("article_debug", {}),
        "synced_at": record.get("synced_at", ""),
        "wechat_media_id": record.get("wechat_media_id", ""),
        "image_count": record.get("image_count", 0),
        "prepared_images": [
            {
                "slot_id": item.get("slot_id", ""),
                "label": item.get("label", ""),
                "prepared_bytes": item.get("prepared_bytes", 0),
            }
            for item in record.get("prepared_images", [])
        ],
    }


def safe_name(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]+", "-", value).strip("-") or "image"
