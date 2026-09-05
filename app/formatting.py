from __future__ import annotations

import html
import re
from typing import Any


HEADING_RE = re.compile(r"^(#{1,4})\s+(.+)$")
IMAGE_MARKER_RE = re.compile(r"^\[(配图|图片)\d*[^\]]*\]$")
SOURCE_NOTE_RE = re.compile(
    r"^(?:（|\()?[\s]*(?:相关链接|参考链接|资料来源|信息来源|来源|原文链接)[^。！？\n]*(?:见文末|文末|如下|：|:)?[\s]*(?:）|\))?[。！？]?$"
)


def enrich_draft_layout(draft: dict[str, Any]) -> dict[str, Any]:
    body = clean_body_markdown(str(draft.get("body_markdown") or ""))
    draft["body_markdown"] = body
    html_body = markdown_to_wechat_html(body, draft)
    paragraphs = [line.strip() for line in body.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", body))
    selected_images = selected_images_by_slot(draft)
    draft["formatted_html"] = html_body
    draft["layout"] = {
        "word_count_zh": chinese_chars,
        "paragraph_count": len(paragraphs),
        "has_cover_image": bool(draft.get("cover_image", {}).get("url")),
        "section_count": len(re.findall(r"^#{1,4}\s+", body, flags=re.M)),
        "image_count": len([image for image in selected_images.values() if image and image.get("url")]),
    }
    return draft


def markdown_to_wechat_html(markdown: str, draft: dict[str, Any] | None = None) -> str:
    blocks: list[str] = []
    paragraph_lines: list[str] = []
    inserted_cover = False
    draft = draft or {}
    disabled_slots = disabled_image_slots(draft)
    slot_images = selected_images_by_slot(draft)
    cover = slot_images.get("cover") or (None if "cover" in disabled_slots else select_final_image(draft))
    heading_index = 0

    def flush_paragraph() -> None:
        nonlocal inserted_cover
        if not paragraph_lines:
            return
        text = " ".join(line.strip() for line in paragraph_lines if line.strip())
        paragraph_lines.clear()
        if not text or is_source_note(text):
            return
        if IMAGE_MARKER_RE.match(text):
            if cover and not inserted_cover:
                blocks.append(render_article_image(cover, "cover"))
                inserted_cover = True
        else:
            blocks.append(f"<p>{render_inline_rich_text(text)}</p>")

    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            continue
        heading = HEADING_RE.match(line)
        if heading:
            flush_paragraph()
            heading_index += 1
            level = 2 if len(heading.group(1)) <= 2 else 3
            blocks.append(f"<h{level}>{render_inline_rich_text(heading.group(2).strip())}</h{level}>")
            section_image = slot_images.get(f"section-{heading_index}")
            if section_image:
                blocks.append(render_article_image(section_image, f"section-{heading_index}"))
            continue
        paragraph_lines.append(line)

    flush_paragraph()
    if cover and not inserted_cover:
        blocks.insert(0, render_article_image(cover, "cover"))
    return "\n".join(blocks)


def clean_body_markdown(markdown: str) -> str:
    lines = []
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if is_source_note(line):
            continue
        lines.append(raw_line)
    return "\n".join(lines).strip()


def is_source_note(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    if SOURCE_NOTE_RE.match(normalized):
        return True
    source_note_phrases = (
        "相关链接见文末",
        "参考链接见文末",
        "资料来源见文末",
        "信息来源见文末",
        "原文链接见文末",
    )
    return any(phrase in normalized for phrase in source_note_phrases) and len(normalized) <= 40


def enrich_drafts_layout(drafts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [enrich_draft_layout(draft) for draft in drafts]


def select_final_image(draft: dict[str, Any]) -> dict[str, Any] | None:
    cover = draft.get("cover_image")
    if isinstance(cover, dict) and cover.get("url"):
        return cover
    final_images = draft.get("final_images")
    if isinstance(final_images, list):
        for image in final_images:
            if isinstance(image, dict) and image.get("url"):
                return image
    return None


def selected_images_by_slot(draft: dict[str, Any]) -> dict[str, dict[str, Any]]:
    images: dict[str, dict[str, Any]] = {}
    disabled_slots = disabled_image_slots(draft)
    for slot in draft.get("image_slots", []) or []:
        if not isinstance(slot, dict):
            continue
        slot_id = str(slot.get("slot_id") or "")
        if slot_id in disabled_slots:
            continue
        selected = slot.get("selected_image")
        if isinstance(selected, dict) and selected.get("url"):
            images[slot_id] = selected
    if "cover" not in images and "cover" not in disabled_slots:
        cover = draft.get("cover_image")
        if isinstance(cover, dict) and cover.get("url"):
            images["cover"] = cover
    for image in draft.get("final_images", []) or []:
        if not isinstance(image, dict) or not image.get("url"):
            continue
        slot_id = str(image.get("slot_id") or "")
        if slot_id and slot_id not in images and slot_id not in disabled_slots:
            images[slot_id] = image
    return images


def disabled_image_slots(draft: dict[str, Any]) -> set[str]:
    values = draft.get("disabled_image_slots")
    if not isinstance(values, list):
        return set()
    return {str(value) for value in values if str(value).strip()}


def render_article_image(image: dict[str, Any], slot_id: str = "") -> str:
    url = html.escape(str(image.get("url") or ""))
    escaped_slot = html.escape(slot_id)
    caption = media_image_caption(image)
    caption_html = f'<figcaption>{html.escape(caption)}</figcaption>' if caption else ""
    return (
        f'<figure class="wechat-article-image" data-slot-id="{escaped_slot}">'
        f'<img src="{url}" alt="">'
        f"{caption_html}"
        "</figure>"
    )


def media_image_caption(image: dict[str, Any]) -> str:
    source_type = str(image.get("type") or image.get("source_type") or "")
    caption = str(image.get("caption") or "").strip()
    if caption:
        return caption
    if source_type == "opinion_screenshot":
        return "评论截图，仅作舆论观察素材。"
    if source_type != "media_preview":
        return ""
    source_name = str(image.get("source_name") or "").strip()
    if source_name:
        return f"图片来源：{source_name}"
    return "图片来源：媒体公开页面"


def render_inline_rich_text(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(
        r"&lt;red&gt;(.+?)&lt;/red&gt;",
        r'<span style="color:#c5221f;font-weight:700;">\1</span>',
        escaped,
    )
    escaped = re.sub(
        r"==(.+?)==",
        r'<span style="color:#c5221f;font-weight:700;">\1</span>',
        escaped,
    )
    return escaped


def strip_inline_rich_markers(text: str) -> str:
    cleaned = re.sub(r"</?red>", "", text)
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"==(.+?)==", r"\1", cleaned)
    return cleaned
