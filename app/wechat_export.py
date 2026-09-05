from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

from PIL import Image

from app.config import ROOT_DIR
from app.database import sync_wechat_export
from app.draft_store import ensure_draft_identity, safe_slug
from app.formatting import (
    HEADING_RE,
    IMAGE_MARKER_RE,
    clean_body_markdown,
    enrich_drafts_layout,
    is_source_note,
    render_inline_rich_text,
    selected_images_by_slot,
    strip_inline_rich_markers,
)
from app.image_candidates import ensure_image_slots
from app.heybox_writer import load_or_create_heybox_draft
from app.platform_variants import selected_article_variant
from app.title_format import format_title_with_prefix, strip_title_prefix, title_prefix_bracketed
from app.writer import load_drafts


WECHAT_EXPORT_DIR = ROOT_DIR / "data" / "exports" / "wechat"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
WECHAT_DOCS = [
    "https://developers.weixin.qq.com/doc/offiaccount/Draft_Box/Add_draft.html",
    "https://developers.weixin.qq.com/doc/offiaccount/Asset_Management/Adding_Permanent_Assets.html",
]
WECHAT_TITLE_LIMIT = 64


def export_draft_for_wechat(draft_index: int, public_base_url: str = "http://127.0.0.1:5050") -> dict[str, Any]:
    drafts = load_drafts()
    if draft_index < 0 or draft_index >= len(drafts):
        raise IndexError("没有找到这篇草稿，可能已经被删除。")

    draft = enrich_drafts_layout([drafts[draft_index]])[0]
    draft_id = ensure_draft_identity(draft, draft_index)
    ensure_image_slots(draft)
    variant = selected_article_variant(draft_id, default="long")
    draft = apply_article_variant(draft, draft_index, variant)
    manifest = build_image_manifest(draft, public_base_url, include_extra_slots=variant == "short", max_images=4 if variant == "short" else None)
    manual_content = render_wechat_content_html(draft, manifest, image_mode="local-preview")
    api_content = render_wechat_content_html(draft, manifest, image_mode="wechat-placeholder")
    payload = build_wechat_api_payload(draft, api_content, manifest)

    now = datetime.now(LOCAL_TZ)
    date_key = now.date().isoformat()
    export_id = make_export_id(draft, now)
    title_slug = safe_slug(str(draft.get("title") or "draft"), max_length=36) or "draft"
    base_name = f"{now.strftime('%H%M%S')}-{title_slug}-{export_id}"
    export_dir = WECHAT_EXPORT_DIR / date_key
    export_dir.mkdir(parents=True, exist_ok=True)
    image_dir = export_dir / f"{base_name}-images"
    prepare_download_images(manifest, image_dir, f"/exports/wechat/{date_key}/{image_dir.name}")

    html_path = export_dir / f"{base_name}.html"
    json_path = export_dir / f"{base_name}.json"
    html_path.write_text(build_export_page(draft, manual_content, manifest, payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    export = {
        "draft_index": draft_index,
        "draft_id": draft.get("draft_id"),
        "title": draft.get("title", ""),
        "variant": variant,
        "exported_at": now.isoformat(),
        "html_path": str(html_path),
        "json_path": str(json_path),
        "html_url": f"/exports/wechat/{date_key}/{html_path.name}",
        "json_url": f"/exports/wechat/{date_key}/{json_path.name}",
        "image_dir": str(image_dir),
        "image_downloads": [
            {
                "slot_id": item.get("slot_id", ""),
                "label": item.get("label", ""),
                "download_url": item.get("download_url", ""),
                "download_path": item.get("download_path", ""),
            }
            for item in manifest
            if item.get("download_url")
        ],
        "image_count": len(manifest),
        "docs": WECHAT_DOCS,
    }
    sync_wechat_export(export)
    return export


def build_wechat_clipboard_payload(draft_index: int, public_base_url: str = "http://127.0.0.1:5050") -> dict[str, Any]:
    drafts = load_drafts()
    if draft_index < 0 or draft_index >= len(drafts):
        raise IndexError("没有找到这篇草稿，可能已经被删除。")

    draft = enrich_drafts_layout([drafts[draft_index]])[0]
    draft_id = ensure_draft_identity(draft, draft_index)
    ensure_image_slots(draft)
    variant = selected_article_variant(draft_id, default="long")
    draft = apply_article_variant(draft, draft_index, variant)
    manifest = build_image_manifest(draft, public_base_url, include_extra_slots=variant == "short", max_images=4 if variant == "short" else None)
    content_html = render_wechat_content_html(draft, manifest, image_mode="local-preview")
    article_html = render_wechat_clipboard_article(draft, content_html)
    return {
        "draft_index": draft_index,
        "draft_id": draft.get("draft_id", ""),
        "title": draft.get("title", ""),
        "display_title": format_title_with_prefix(str(draft.get("title") or "")),
        "variant": variant,
        "html": article_html,
        "plain_text": build_plain_text_article(draft),
        "image_count": len(manifest),
        "images": [
            {
                "slot_id": item.get("slot_id", ""),
                "label": item.get("label", ""),
                "local_preview_url": item.get("local_preview_url", ""),
            }
            for item in manifest
        ],
    }


def apply_article_variant(draft: dict[str, Any], draft_index: int, variant: str) -> dict[str, Any]:
    if variant != "short":
        draft["article_variant"] = "long"
        draft["platform_variant"] = "long"
        return draft
    short_copy = load_or_create_heybox_draft(draft_index)
    merged = dict(draft)
    merged["title"] = draft.get("title", "")
    merged["subtitle"] = short_copy.get("subtitle") or draft.get("subtitle", "")
    merged["body_markdown"] = short_copy.get("body_markdown") or draft.get("body_markdown", "")
    merged["short_copy"] = short_copy
    merged["heybox_copy"] = short_copy
    merged["article_variant"] = "short"
    merged["platform_variant"] = "short"
    return merged


def build_wechat_api_payload(draft: dict[str, Any], content_html: str, manifest: list[dict[str, Any]]) -> dict[str, Any]:
    title = make_wechat_safe_title(format_title_with_prefix(str(draft.get("title") or "")))
    digest = clamp_text_bytes(str(draft.get("subtitle") or ""), 120)
    article = {
        "title": title,
        "author": "",
        "digest": digest,
        "content": content_html,
        "content_source_url": "",
        "thumb_media_id": "{{WECHAT_THUMB_MEDIA_ID}}",
        "show_cover_pic": 0,
        "need_open_comment": 0,
        "only_fans_can_comment": 0,
    }
    material_article = {**article}
    return {
        "format": "wechat_official_account_article",
        "notes": [
            "正文 content 使用内联样式 HTML。",
            "正文图片需要先用公众号接口 /cgi-bin/media/uploadimg 上传，拿到微信 URL 后替换 WECHAT_IMAGE_URL_* 占位符。",
            "封面图需要上传为素材并取得 thumb_media_id 后替换 WECHAT_THUMB_MEDIA_ID。",
        ],
        "image_manifest": manifest,
        "draft_add_payload": {"articles": [article]},
        "material_add_news_payload": {"articles": [material_article]},
        "official_docs": WECHAT_DOCS,
    }


def build_image_manifest(
    draft: dict[str, Any],
    public_base_url: str,
    *,
    include_extra_slots: bool = False,
    max_images: int | None = None,
) -> list[dict[str, Any]]:
    selected_by_slot = selected_images_by_slot(draft)
    content_slots = content_image_slot_ids(draft, selected_by_slot, include_extra_slots=include_extra_slots, max_images=max_images)
    slots = draft.get("image_slots") or []
    if not slots and selected_by_slot:
        slots = [{"slot_id": slot_id, "kind": "section", "label": slot_id, "position": index} for index, slot_id in enumerate(selected_by_slot, 1)]

    manifest: list[dict[str, Any]] = []
    seen: set[str] = set()
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        slot_id = str(slot.get("slot_id") or "")
        if slot_id not in content_slots:
            continue
        image = selected_by_slot.get(slot_id)
        if not slot_id or not image or not image.get("url"):
            continue
        key = normalize_placeholder_key(slot_id)
        source_type = str(image.get("type") or image.get("source_type") or "")
        manifest.append(
            {
                "slot_id": slot_id,
                "label": slot.get("label") or ("封面/开头配图" if slot_id == "cover" else slot_id),
                "kind": slot.get("kind") or ("cover" if slot_id == "cover" else "section"),
                "local_preview_url": absolute_url(str(image.get("url") or ""), public_base_url),
                "local_path": image.get("local_path", ""),
                "source_type": source_type,
                "source_name": image.get("source_name", ""),
                "source_url": image.get("source_url", ""),
                "source_title": image.get("source_title", ""),
                "caption": image.get("caption", ""),
                "wechat_image_placeholder": f"{{{{WECHAT_IMAGE_URL_{key}}}}}",
                "wechat_thumb_media_placeholder": "{{WECHAT_THUMB_MEDIA_ID}}" if slot_id == "cover" else "",
                "upload_hint": "封面需上传为素材获取 thumb_media_id；若正文也使用这张图，还需 uploadimg 获取正文图片 URL。"
                if slot_id == "cover"
                else "正文配图需 uploadimg 获取微信图片 URL。",
            }
        )
        seen.add(slot_id)

    for slot_id, image in selected_by_slot.items():
        if slot_id in seen or not isinstance(image, dict) or not image.get("url"):
            continue
        if slot_id not in content_slots:
            continue
        key = normalize_placeholder_key(slot_id)
        manifest.append(
            {
                "slot_id": slot_id,
                "label": slot_id,
                "kind": "section",
                "local_preview_url": absolute_url(str(image.get("url") or ""), public_base_url),
                "local_path": image.get("local_path", ""),
                "source_type": str(image.get("type") or image.get("source_type") or ""),
                "source_name": image.get("source_name", ""),
                "source_url": image.get("source_url", ""),
                "source_title": image.get("source_title", ""),
                "caption": image.get("caption", ""),
                "wechat_image_placeholder": f"{{{{WECHAT_IMAGE_URL_{key}}}}}",
                "wechat_thumb_media_placeholder": "",
                "upload_hint": "正文配图需 uploadimg 获取微信图片 URL。",
            }
        )
    return manifest


def content_image_slot_ids(
    draft: dict[str, Any],
    selected_by_slot: dict[str, dict[str, Any]] | None = None,
    *,
    include_extra_slots: bool = False,
    max_images: int | None = None,
) -> set[str]:
    body = clean_body_markdown(str(draft.get("body_markdown") or ""))
    section_count = len([line for line in body.splitlines() if HEADING_RE.match(line.strip())])
    slots = ["cover", *[f"section-{index}" for index in range(1, section_count + 1)]]
    selected_by_slot = selected_by_slot or selected_images_by_slot(draft)
    if include_extra_slots:
        for slot in draft.get("image_slots", []) or []:
            if not isinstance(slot, dict):
                continue
            slot_id = str(slot.get("slot_id") or "")
            if slot_id and slot_id in selected_by_slot and slot_id not in slots:
                slots.append(slot_id)
    if isinstance(max_images, int) and max_images > 0:
        counted: list[str] = []
        for slot_id in slots:
            if slot_id not in selected_by_slot:
                continue
            counted.append(slot_id)
            if len(counted) >= max_images:
                break
        slots = counted
    return set(slots)


def prepare_download_images(manifest: list[dict[str, Any]], image_dir: Path, url_prefix: str) -> None:
    image_dir.mkdir(parents=True, exist_ok=True)
    for index, item in enumerate(manifest, start=1):
        source = source_image_path(item)
        slot_id = safe_slug(str(item.get("slot_id") or f"image-{index}"), max_length=24) or f"image-{index}"
        file_name = f"{index:02d}-{slot_id}.png"
        output_path = image_dir / file_name
        try:
            if not source or not source.exists():
                raise FileNotFoundError("未找到本地图片文件，无法生成下载 PNG。")
            export_image_as_png(source, output_path)
            item["download_path"] = str(output_path)
            item["download_url"] = f"{url_prefix.rstrip('/')}/{file_name}"
            item["download_error"] = ""
        except Exception as exc:  # noqa: BLE001
            item["download_path"] = ""
            item["download_url"] = ""
            item["download_error"] = str(exc)


def source_image_path(item: dict[str, Any]) -> Path | None:
    local_path = str(item.get("local_path") or "").strip()
    if local_path:
        return Path(local_path)
    preview_url = str(item.get("local_preview_url") or "")
    if preview_url.startswith("http://") or preview_url.startswith("https://"):
        parsed = urlparse(preview_url)
        return local_url_to_path(unquote(parsed.path))
    if preview_url.startswith("/"):
        return local_url_to_path(unquote(preview_url))
    return None


def local_url_to_path(url: str) -> Path | None:
    if url.startswith("/static-data/images/"):
        return ROOT_DIR / "data" / "images" / url.removeprefix("/static-data/images/")
    if url.startswith("/static-data/generated-images/"):
        return ROOT_DIR / "data" / "generated_images" / url.removeprefix("/static-data/generated-images/")
    if url.startswith("/static-data/imported-images/"):
        return ROOT_DIR / "data" / "imported_images" / url.removeprefix("/static-data/imported-images/")
    if url.startswith("/static-data/official-screenshots/"):
        return ROOT_DIR / "data" / "official_screenshots" / url.removeprefix("/static-data/official-screenshots/")
    if url.startswith("/static-data/opinion-imports/"):
        return ROOT_DIR / "data" / "opinion_imports" / url.removeprefix("/static-data/opinion-imports/")
    return None


def export_image_as_png(source: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() == ".png":
        shutil.copy2(source, output_path)
        return
    with Image.open(source) as image:
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
        image.save(output_path, format="PNG")


def render_wechat_content_html(draft: dict[str, Any], manifest: list[dict[str, Any]], image_mode: str) -> str:
    body = clean_body_markdown(str(draft.get("body_markdown") or ""))
    manifest_by_slot = {str(item["slot_id"]): item for item in manifest}
    extra_images = extra_manifest_images(manifest, body)
    blocks: list[str] = []
    paragraph_lines: list[str] = []
    heading_index = 0
    inserted_cover = False
    paragraph_count = 0

    def flush_paragraph() -> None:
        nonlocal inserted_cover, paragraph_count
        if not paragraph_lines:
            return
        text = " ".join(line.strip() for line in paragraph_lines if line.strip())
        paragraph_lines.clear()
        if not text or is_source_note(text):
            return
        if IMAGE_MARKER_RE.match(text):
            if not inserted_cover:
                image_block = render_inline_image(manifest_by_slot.get("cover"), image_mode)
                if image_block:
                    blocks.append(image_block)
                    inserted_cover = True
            return
        blocks.append(render_inline_paragraph(text))
        paragraph_count += 1
        if extra_images and paragraph_count % 2 == 0:
            image_block = render_inline_image(extra_images.pop(0), image_mode)
            if image_block:
                blocks.append(image_block)

    cover_block = render_inline_image(manifest_by_slot.get("cover"), image_mode)
    if cover_block:
        blocks.append(cover_block)
        inserted_cover = True

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            continue
        heading = HEADING_RE.match(line)
        if heading:
            flush_paragraph()
            heading_index += 1
            blocks.append(render_inline_heading(heading.group(2).strip()))
            image_block = render_inline_image(manifest_by_slot.get(f"section-{heading_index}"), image_mode)
            if image_block:
                blocks.append(image_block)
            continue
        paragraph_lines.append(line)

    flush_paragraph()
    for image in extra_images:
        image_block = render_inline_image(image, image_mode)
        if image_block:
            blocks.append(image_block)
    return "\n".join(blocks)


def render_wechat_clipboard_article(draft: dict[str, Any], content_html: str) -> str:
    title = html.escape(format_title_with_prefix(str(draft.get("title") or "")))
    subtitle = html.escape(strip_inline_rich_markers(str(draft.get("subtitle") or "")))
    subtitle_html = (
        f'<p style="margin:0 0 22px;font-size:14px;line-height:1.8;color:#6b7280;">{subtitle}</p>'
        if subtitle
        else ""
    )
    return (
        '<section data-source="wechat-tech-agent" '
        'style="max-width:677px;margin:0 auto;padding:0 8px;color:#25313b;font-family:-apple-system,BlinkMacSystemFont,'
        '\'Segoe UI\',\'PingFang SC\',\'Microsoft YaHei\',Arial,sans-serif;">'
        f'<h1 style="margin:0 0 14px;font-size:22px;line-height:1.45;font-weight:700;color:#182129;">{title}</h1>'
        f"{subtitle_html}"
        f"{content_html}"
        "</section>"
    )


def build_plain_text_article(draft: dict[str, Any]) -> str:
    parts = [format_title_with_prefix(str(draft.get("title") or "")).strip()]
    subtitle = strip_inline_rich_markers(str(draft.get("subtitle") or "")).strip()
    if subtitle:
        parts.append(subtitle)
    body = strip_inline_rich_markers(clean_body_markdown(str(draft.get("body_markdown") or "")))
    if body:
        parts.append(body)
    return "\n\n".join(part for part in parts if part)


def render_inline_heading(text: str) -> str:
    return (
        '<section style="margin:28px 0 14px;padding:0 0 0 12px;border-left:4px solid #0f766e;">'
        f'<strong style="font-size:18px;line-height:1.55;color:#182129;">{html.escape(strip_inline_rich_markers(text))}</strong>'
        "</section>"
    )


def render_inline_paragraph(text: str) -> str:
    return f'<p style="margin:0 0 16px;font-size:16px;line-height:1.9;color:#25313b;">{render_inline_rich_text(text)}</p>'


def render_inline_image(image_item: dict[str, Any] | None, image_mode: str) -> str:
    if not image_item:
        return ""
    src = image_item["local_preview_url"] if image_mode == "local-preview" else image_item["wechat_image_placeholder"]
    label = html.escape(str(image_item.get("label") or "配图"))
    caption = media_image_caption_from_manifest(image_item)
    caption_html = (
        f'<p style="margin:6px 0 0;font-size:12px;line-height:1.6;color:#8a94a6;text-align:center;">{html.escape(caption)}</p>'
        if caption
        else ""
    )
    return (
        '<section style="margin:16px 8px 22px;text-align:center;">'
        f'<img src="{html.escape(src)}" alt="{label}" '
        'style="width:100%;max-width:578px;height:auto;display:inline-block;object-fit:contain;border-radius:0;" />'
        f"{caption_html}"
        "</section>"
    )


def media_image_caption_from_manifest(image_item: dict[str, Any]) -> str:
    caption = str(image_item.get("caption") or "").strip()
    if caption:
        return caption
    if str(image_item.get("source_type") or "") == "opinion_screenshot":
        return "评论截图，仅作舆论观察素材。"
    if str(image_item.get("source_type") or "") != "media_preview":
        return ""
    source_name = str(image_item.get("source_name") or "").strip()
    if source_name:
        return f"图片来源：{source_name}"
    return "图片来源：媒体公开页面"


def extra_manifest_images(manifest: list[dict[str, Any]], body: str) -> list[dict[str, Any]]:
    heading_count = len([line for line in clean_body_markdown(body).splitlines() if HEADING_RE.match(line.strip())])
    inline_slots = {"cover"} | {f"section-{index}" for index in range(1, heading_count + 1)}
    return [item for item in manifest if str(item.get("slot_id") or "") not in inline_slots]


def build_export_page(
    draft: dict[str, Any],
    content_html: str,
    manifest: list[dict[str, Any]],
    payload: dict[str, Any],
) -> str:
    title = html.escape(format_title_with_prefix(str(draft.get("title") or "未命名草稿")))
    subtitle = html.escape(strip_inline_rich_markers(str(draft.get("subtitle") or "")))
    manifest_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('label') or ''))}</td>"
        f"<td>{html.escape(str(item.get('source_type') or ''))}</td>"
        f"<td><code>{html.escape(str(item.get('wechat_image_placeholder') or ''))}</code></td>"
        f"<td>{render_download_link(item)}</td>"
        f"<td>{html.escape(str(item.get('upload_hint') or item.get('download_error') or ''))}</td>"
        "</tr>"
        for item in manifest
    )
    payload_preview = html.escape(json.dumps(payload["draft_add_payload"], ensure_ascii=False, indent=2))
    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title} - 公众号导出</title>
    <style>
      body {{ margin:0; background:#f4f6f8; color:#182129; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif; }}
      main {{ max-width:860px; margin:0 auto; padding:28px 18px 56px; }}
      .notice,.article,.meta {{ background:#fff; border:1px solid #d8e0e6; border-radius:8px; padding:18px; margin-bottom:14px; }}
      h1 {{ margin:0 0 10px; font-size:28px; line-height:1.35; }}
      .subtitle {{ margin:0 0 18px; color:#63707b; line-height:1.7; }}
      table {{ width:100%; border-collapse:collapse; font-size:13px; }}
      td,th {{ padding:8px; border-bottom:1px solid #e6edf1; text-align:left; vertical-align:top; }}
      code,pre {{ white-space:pre-wrap; overflow-wrap:anywhere; }}
      pre {{ background:#eef2f5; border-radius:8px; padding:12px; }}
    </style>
  </head>
  <body>
    <main>
      <section class="notice">
        <strong>公众号格式导出</strong>
        <p>下面的正文为可复制 HTML 预览版。正式接口发布时，请先把图片上传到微信，替换 JSON 文件里的图片 URL 和 thumb_media_id 占位符。</p>
      </section>
      <article class="article">
        <h1>{title}</h1>
        {f'<p class="subtitle">{subtitle}</p>' if subtitle else ''}
        {content_html}
      </article>
      <section class="meta">
        <h2>图片上传清单</h2>
        <table>
          <thead><tr><th>位置</th><th>类型</th><th>占位符</th><th>下载</th><th>处理方式</th></tr></thead>
          <tbody>{manifest_rows}</tbody>
        </table>
      </section>
      <section class="meta">
        <h2>草稿箱接口 JSON 预览</h2>
        <pre>{payload_preview}</pre>
      </section>
    </main>
  </body>
</html>
"""


def render_download_link(item: dict[str, Any]) -> str:
    download_url = str(item.get("download_url") or "")
    if download_url:
        label = html.escape(f"{item.get('slot_id') or 'image'}.png")
        return f'<a href="{html.escape(download_url)}" download>{label}</a>'
    error = str(item.get("download_error") or "无本地 PNG")
    return f"<span>{html.escape(error)}</span>"


def absolute_url(url: str, public_base_url: str) -> str:
    if not url:
        return ""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("/"):
        return f"{public_base_url.rstrip('/')}{url}"
    return url


def normalize_placeholder_key(value: str) -> str:
    key = re.sub(r"[^0-9A-Za-z]+", "_", value).strip("_").upper()
    return key or "IMAGE"


def clamp_text(text: str, max_length: int) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    return normalized if len(normalized) <= max_length else f"{normalized[: max_length - 1]}…"


def clamp_text_bytes(text: str, max_bytes: int, suffix: str = "...") -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized.encode("utf-8")) <= max_bytes:
        return normalized
    suffix_bytes = len(suffix.encode("utf-8"))
    budget = max(0, max_bytes - suffix_bytes)
    result = ""
    used = 0
    for char in normalized:
        char_bytes = len(char.encode("utf-8"))
        if used + char_bytes > budget:
            break
        result += char
        used += char_bytes
    return f"{result.rstrip()}{suffix}" if result else normalized.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def make_wechat_safe_title(text: str, max_chars: int = WECHAT_TITLE_LIMIT) -> str:
    normalized = re.sub(r"\s+", " ", strip_inline_rich_markers(text)).strip()
    if not normalized:
        return "未命名草稿"

    if len(normalized) <= max_chars:
        return normalized

    clean_normalized = strip_title_prefix(normalized)
    prefix = title_prefix_bracketed()
    if prefix and clean_normalized:
        available = max_chars - len(prefix)
        if available >= 8:
            return f"{prefix}{compact_wechat_title_segment(clean_normalized, available)}"
    return compact_wechat_title_segment(normalized, max_chars)

    candidates = [normalized]
    clean_normalized = strip_title_prefix(normalized)
    if clean_normalized != normalized:
        prefix = title_prefix_bracketed()
        budget = max_bytes - len(prefix.encode("utf-8"))
        if prefix and budget >= 9:
            candidates.append(f"{prefix}{clamp_text_bytes(clean_normalized, budget, suffix='')}")
        candidates.append(clean_normalized)
    for separator in ("：", ":", "，", ",", "；", ";", "｜", "|", " - ", " — "):
        if separator in normalized:
            prefix = normalized.split(separator, 1)[0].strip()
            if prefix:
                candidates.insert(0, prefix)

    for candidate in candidates:
        if len(candidate) >= 4 and len(candidate.encode("utf-8")) <= max_bytes:
            return candidate
    return clamp_text_bytes(normalized, max_bytes, suffix="")


def compact_wechat_title_segment(title: str, limit: int) -> str:
    cleaned = re.sub(r"\s+", " ", strip_inline_rich_markers(title)).strip()
    if len(cleaned) <= limit:
        return cleaned
    if limit <= 0:
        return ""
    if limit == 1:
        return cleaned[:1]
    return cleaned[: limit - 1].rstrip("，。！？、；,.!?;: ") + "…"


def make_export_id(draft: dict[str, Any], exported_at: datetime) -> str:
    raw = f"{draft.get('draft_id', '')}:{draft.get('title', '')}:{exported_at.isoformat()}"
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:10]
