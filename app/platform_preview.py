from __future__ import annotations

import re
from typing import Any

from app.draft_store import ensure_draft_identity
from app.formatting import HEADING_RE, clean_body_markdown, enrich_drafts_layout, strip_inline_rich_markers
from app.heybox_export import apply_short_copy, render_heybox_clipboard_article, render_heybox_content_html
from app.heybox_writer import load_or_create_heybox_draft
from app.image_candidates import ensure_image_slots
from app.platform_variants import article_variant_choice
from app.wechat_export import (
    build_image_manifest,
    render_wechat_clipboard_article,
    render_wechat_content_html,
)
from app.writer import load_drafts


def build_draft_variant_previews(draft_index: int, public_base_url: str) -> dict[str, Any]:
    drafts = load_drafts()
    if draft_index < 0 or draft_index >= len(drafts):
        raise IndexError("没有找到这篇草稿，可能已经被删除。")

    source_draft = enrich_drafts_layout([drafts[draft_index]])[0]
    draft_id = ensure_draft_identity(source_draft, draft_index)
    ensure_image_slots(source_draft)

    long_manifest = build_image_manifest(source_draft, public_base_url)
    long_content = render_wechat_content_html(source_draft, long_manifest, image_mode="local-preview")
    long_html = render_wechat_clipboard_article(source_draft, long_content)

    short_copy = load_or_create_heybox_draft(draft_index)
    short_draft = apply_short_copy(source_draft, short_copy)
    short_manifest = build_image_manifest(short_draft, public_base_url, include_extra_slots=True, max_images=4)
    short_content = render_heybox_content_html(short_draft, short_manifest)
    short_html = render_heybox_clipboard_article(short_draft, short_content)

    selected = article_variant_choice(draft_id, default="long")
    return {
        "draft_index": draft_index,
        "draft_id": draft_id,
        "selected_variant": selected.get("variant", "long"),
        "selected_label": selected.get("label", ""),
        "choice": selected,
        "variants": {
            "long": variant_payload("long", "长文版", source_draft, long_html, long_manifest),
            "short": variant_payload("short", "短文版", short_draft, short_html, short_manifest),
        },
    }


def variant_payload(
    variant: str,
    label: str,
    draft: dict[str, Any],
    html: str,
    manifest: list[dict[str, Any]],
) -> dict[str, Any]:
    body = clean_body_markdown(str(draft.get("body_markdown") or ""))
    plain = strip_inline_rich_markers(
        "\n".join(
            [
                str(draft.get("title") or ""),
                str(draft.get("subtitle") or ""),
                body,
            ]
        )
    )
    return {
        "variant": variant,
        "label": label,
        "title": strip_inline_rich_markers(str(draft.get("title") or "")),
        "subtitle": strip_inline_rich_markers(str(draft.get("subtitle") or "")),
        "body_markdown": body,
        "html": html,
        "word_count_zh": zh_count(plain),
        "section_count": section_count(body),
        "image_count": len(manifest),
    }


def zh_count(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", text))


def section_count(body: str) -> int:
    return len([line for line in body.splitlines() if HEADING_RE.match(line.strip())])
