from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.config import ROOT_DIR
from app.database import sync_platform_export
from app.draft_store import ensure_draft_identity, safe_slug
from app.formatting import (
    HEADING_RE,
    IMAGE_MARKER_RE,
    clean_body_markdown,
    enrich_drafts_layout,
    is_source_note,
    render_inline_rich_text,
    strip_inline_rich_markers,
)
from app.image_candidates import ensure_image_slots
from app.heybox_writer import load_or_create_heybox_draft
from app.platform_variants import selected_article_variant
from app.title_format import format_title_with_prefix
from app.wechat_export import absolute_url, build_image_manifest, prepare_download_images
from app.writer import load_drafts


HEYBOX_EXPORT_DIR = ROOT_DIR / "data" / "exports" / "heybox"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
HEYBOX_CREATOR_URL = "https://www.xiaoheihe.cn/creator/content_management/home"
HEYBOX_OPEN_PLATFORM_URL = "https://open.xiaoheihe.cn/"


def export_draft_for_heybox(draft_index: int, public_base_url: str = "http://127.0.0.1:5050") -> dict[str, Any]:
    draft, manifest = prepare_heybox_draft(draft_index, public_base_url)
    content_html = render_heybox_content_html(draft, manifest)
    article_html = render_heybox_clipboard_article(draft, content_html)
    plain_text = build_plain_text_article(draft)

    now = datetime.now(LOCAL_TZ)
    date_key = now.date().isoformat()
    export_id = make_export_id(draft, now)
    title_slug = safe_slug(str(draft.get("title") or "draft"), max_length=36) or "draft"
    base_name = f"{now.strftime('%H%M%S')}-{title_slug}-{export_id}"
    export_dir = HEYBOX_EXPORT_DIR / date_key
    export_dir.mkdir(parents=True, exist_ok=True)
    image_dir = export_dir / f"{base_name}-images"
    prepare_download_images(manifest, image_dir, f"/exports/heybox/{date_key}/{image_dir.name}")
    markdown = render_heybox_markdown(draft, manifest, public_base_url)

    payload = build_heybox_payload(draft, article_html, plain_text, markdown, manifest)
    html_path = export_dir / f"{base_name}.html"
    markdown_path = export_dir / f"{base_name}.md"
    json_path = export_dir / f"{base_name}.json"
    html_path.write_text(build_export_page(draft, article_html, manifest, payload), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    export = {
        "platform": "heybox",
        "draft_index": draft_index,
        "draft_id": draft.get("draft_id"),
        "title": draft.get("title", ""),
        "exported_at": now.isoformat(),
        "html_path": str(html_path),
        "markdown_path": str(markdown_path),
        "json_path": str(json_path),
        "html_url": f"/exports/heybox/{date_key}/{html_path.name}",
        "markdown_url": f"/exports/heybox/{date_key}/{markdown_path.name}",
        "json_url": f"/exports/heybox/{date_key}/{json_path.name}",
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
        "creator_url": HEYBOX_CREATOR_URL,
        "open_platform_url": HEYBOX_OPEN_PLATFORM_URL,
        "notes": platform_notes(),
    }
    sync_platform_export(export)
    return export


def build_heybox_clipboard_payload(draft_index: int, public_base_url: str = "http://127.0.0.1:5050") -> dict[str, Any]:
    draft, manifest = prepare_heybox_draft(draft_index, public_base_url)
    content_html = render_heybox_content_html(draft, manifest)
    article_html = render_heybox_clipboard_article(draft, content_html)
    return {
        "platform": "heybox",
        "draft_index": draft_index,
        "draft_id": draft.get("draft_id", ""),
        "title": draft.get("title", ""),
        "display_title": format_title_with_prefix(str(draft.get("title") or "")),
        "html": article_html,
        "plain_text": build_plain_text_article(draft),
        "image_count": len(manifest),
        "images": [
            {
                "slot_id": item.get("slot_id", ""),
                "label": item.get("label", ""),
                "local_preview_url": item.get("local_preview_url", ""),
                "download_url": item.get("download_url", ""),
            }
            for item in manifest
        ],
        "creator_url": HEYBOX_CREATOR_URL,
        "open_platform_url": HEYBOX_OPEN_PLATFORM_URL,
        "notes": platform_notes(),
    }


def prepare_heybox_draft(draft_index: int, public_base_url: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    drafts = load_drafts()
    if draft_index < 0 or draft_index >= len(drafts):
        raise IndexError("没有找到这篇草稿，可能已经被删除。")

    draft = enrich_drafts_layout([drafts[draft_index]])[0]
    draft_id = ensure_draft_identity(draft, draft_index)
    ensure_image_slots(draft)
    variant = selected_article_variant(draft_id, default="long")
    if variant == "short":
        short_copy = load_or_create_heybox_draft(draft_index)
        draft = apply_short_copy(draft, short_copy)
    else:
        draft = dict(draft)
        draft["article_variant"] = "long"
        draft["platform_variant"] = "long"
    manifest = build_image_manifest(draft, public_base_url, include_extra_slots=variant == "short", max_images=4 if variant == "short" else None)
    return draft, manifest


def apply_heybox_copy(draft: dict[str, Any], platform_copy: dict[str, Any]) -> dict[str, Any]:
    return apply_short_copy(draft, platform_copy)


def apply_short_copy(draft: dict[str, Any], platform_copy: dict[str, Any]) -> dict[str, Any]:
    merged = dict(draft)
    merged["title"] = draft.get("title", "")
    merged["subtitle"] = platform_copy.get("subtitle") or draft.get("subtitle", "")
    merged["body_markdown"] = platform_copy.get("body_markdown") or draft.get("body_markdown", "")
    merged["short_copy"] = platform_copy
    merged["heybox_copy"] = platform_copy
    merged["article_variant"] = "short"
    merged["platform_variant"] = "short"
    return merged


def build_heybox_payload(
    draft: dict[str, Any],
    html_body: str,
    plain_text: str,
    markdown: str,
    manifest: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "format": "heybox_article_export",
        "platform": "heybox",
        "title": format_title_with_prefix(str(draft.get("title") or "")),
        "subtitle": strip_inline_rich_markers(str(draft.get("subtitle") or "")),
        "html": html_body,
        "plain_text": plain_text,
        "markdown": markdown,
        "image_manifest": manifest,
        "creator_url": HEYBOX_CREATOR_URL,
        "open_platform_url": HEYBOX_OPEN_PLATFORM_URL,
        "notes": platform_notes(),
    }


def platform_notes() -> list[str]:
    return [
        "当前未发现小黑盒公开的文章草稿箱创建 API，因此本功能先提供富文本复制、HTML/Markdown 导出和图片 PNG 下载。",
        "如果后续拿到小黑盒官方内容接口、token 和草稿创建文档，可以在此 payload 基础上接入真正的草稿箱同步。",
        "不要使用模拟登录、抓包逆向或绕过平台权限的方式自动写入内容。",
    ]


def render_heybox_content_html(draft: dict[str, Any], manifest: list[dict[str, Any]]) -> str:
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
                image_block = render_heybox_image(manifest_by_slot.get("cover"))
                if image_block:
                    blocks.append(image_block)
                    inserted_cover = True
            return
        blocks.append(render_heybox_paragraph(text))
        paragraph_count += 1
        if extra_images and paragraph_count % 2 == 0:
            image_block = render_heybox_image(extra_images.pop(0))
            if image_block:
                blocks.append(image_block)

    cover_block = render_heybox_image(manifest_by_slot.get("cover"))
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
            blocks.append(render_heybox_heading(heading.group(2).strip()))
            image_block = render_heybox_image(manifest_by_slot.get(f"section-{heading_index}"))
            if image_block:
                blocks.append(image_block)
            continue
        paragraph_lines.append(line)

    flush_paragraph()
    for image in extra_images:
        image_block = render_heybox_image(image)
        if image_block:
            blocks.append(image_block)
    return "\n".join(blocks)


def render_heybox_clipboard_article(draft: dict[str, Any], content_html: str) -> str:
    title = html.escape(format_title_with_prefix(str(draft.get("title") or "")))
    subtitle = html.escape(strip_inline_rich_markers(str(draft.get("subtitle") or "")))
    subtitle_html = f'<p style="margin:0 0 18px;color:#6b7280;font-size:15px;line-height:1.8;">{subtitle}</p>' if subtitle else ""
    return (
        '<article data-source="tech-agent-heybox" '
        'style="max-width:760px;margin:0 auto;color:#1f2933;font-family:-apple-system,BlinkMacSystemFont,'
        '\'Segoe UI\',\'PingFang SC\',\'Microsoft YaHei\',Arial,sans-serif;">'
        f'<h1 style="margin:0 0 12px;font-size:26px;line-height:1.35;font-weight:800;color:#111827;">{title}</h1>'
        f"{subtitle_html}"
        f"{content_html}"
        "</article>"
    )


def render_heybox_heading(text: str) -> str:
    return (
        '<h2 style="margin:30px 0 14px;font-size:21px;line-height:1.45;font-weight:800;color:#111827;">'
        f"{render_inline_rich_text(strip_inline_rich_markers(text))}"
        "</h2>"
    )


def render_heybox_paragraph(text: str) -> str:
    return f'<p style="margin:0 0 16px;font-size:16px;line-height:1.9;color:#24303d;">{render_inline_rich_text(text)}</p>'


def render_heybox_image(image_item: dict[str, Any] | None) -> str:
    if not image_item:
        return ""
    src = str(image_item.get("local_preview_url") or "")
    if not src:
        return ""
    label = html.escape(str(image_item.get("label") or "配图"))
    caption = media_image_caption(image_item)
    caption_html = f'<figcaption style="margin-top:8px;font-size:12px;line-height:1.6;color:#8a94a6;text-align:center;">{html.escape(caption)}</figcaption>' if caption else ""
    return (
        '<figure style="margin:18px 0 24px;text-align:center;">'
        f'<img src="{html.escape(src)}" alt="{label}" '
        'style="width:100%;max-width:720px;height:auto;display:inline-block;object-fit:contain;border-radius:6px;" />'
        f"{caption_html}"
        "</figure>"
    )


def media_image_caption(image_item: dict[str, Any]) -> str:
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


def render_heybox_markdown(draft: dict[str, Any], manifest: list[dict[str, Any]], public_base_url: str) -> str:
    body = clean_body_markdown(str(draft.get("body_markdown") or ""))
    title = strip_inline_rich_markers(str(draft.get("title") or "")).strip()
    subtitle = strip_inline_rich_markers(str(draft.get("subtitle") or "")).strip()
    manifest_by_slot = {str(item.get("slot_id") or ""): item for item in manifest}
    extra_images = extra_manifest_images(manifest, body)
    lines: list[str] = [f"# {title}", ""]
    if subtitle:
        lines.extend([subtitle, ""])
    cover = markdown_image(manifest_by_slot.get("cover"), public_base_url)
    if cover:
        lines.extend([cover, ""])

    heading_index = 0
    paragraph_count = 0
    for raw_line in body.splitlines():
        line = raw_line.strip()
        heading = HEADING_RE.match(line)
        if heading:
            heading_index += 1
            lines.append(raw_line)
            image = markdown_image(manifest_by_slot.get(f"section-{heading_index}"), public_base_url)
            if image:
                lines.extend(["", image, ""])
            continue
        if IMAGE_MARKER_RE.match(line) or is_source_note(line):
            continue
        lines.append(raw_line)
        if line:
            paragraph_count += 1
            if extra_images and paragraph_count % 2 == 0:
                image = markdown_image(extra_images.pop(0), public_base_url)
                if image:
                    lines.extend(["", image, ""])
    for item in extra_images:
        image = markdown_image(item, public_base_url)
        if image:
            lines.extend(["", image, ""])
    return "\n".join(lines).strip() + "\n"


def markdown_image(image_item: dict[str, Any] | None, public_base_url: str) -> str:
    if not image_item:
        return ""
    url = str(image_item.get("download_url") or image_item.get("local_preview_url") or "")
    if not url:
        return ""
    label = strip_inline_rich_markers(str(image_item.get("label") or "配图"))
    image_line = f"![{label}]({absolute_url(url, public_base_url)})"
    caption = media_image_caption(image_item)
    if caption:
        return f"{image_line}\n\n*{strip_inline_rich_markers(caption)}*"
    return image_line


def build_plain_text_article(draft: dict[str, Any]) -> str:
    parts = [format_title_with_prefix(str(draft.get("title") or "")).strip()]
    subtitle = strip_inline_rich_markers(str(draft.get("subtitle") or "")).strip()
    if subtitle:
        parts.append(subtitle)
    body = strip_inline_rich_markers(clean_body_markdown(str(draft.get("body_markdown") or "")))
    if body:
        parts.append(body)
    return "\n\n".join(part for part in parts if part)


def build_export_page(
    draft: dict[str, Any],
    article_html: str,
    manifest: list[dict[str, Any]],
    payload: dict[str, Any],
) -> str:
    title = html.escape(format_title_with_prefix(str(draft.get("title") or "未命名草稿")))
    manifest_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('label') or ''))}</td>"
        f"<td>{html.escape(str(item.get('source_type') or ''))}</td>"
        f"<td>{render_download_link(item)}</td>"
        f"<td>{html.escape(media_image_caption(item))}</td>"
        "</tr>"
        for item in manifest
    )
    payload_preview = html.escape(json.dumps(payload, ensure_ascii=False, indent=2))
    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title} - 小黑盒导出</title>
    <style>
      body {{ margin:0; background:#f5f7fa; color:#111827; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif; }}
      main {{ max-width:920px; margin:0 auto; padding:28px 18px 56px; }}
      .notice,.article,.meta {{ background:#fff; border:1px solid #d9e2ea; border-radius:8px; padding:18px; margin-bottom:14px; }}
      .notice p {{ margin:8px 0 0; color:#5f6b7a; line-height:1.7; }}
      .notice a {{ display:inline-block; margin-top:10px; color:#0f766e; font-weight:700; }}
      table {{ width:100%; border-collapse:collapse; font-size:13px; }}
      td,th {{ padding:8px; border-bottom:1px solid #e6edf1; text-align:left; vertical-align:top; }}
      code,pre {{ white-space:pre-wrap; overflow-wrap:anywhere; }}
      pre {{ background:#eef2f5; border-radius:8px; padding:12px; }}
    </style>
  </head>
  <body>
    <main>
      <section class="notice">
        <strong>小黑盒格式导出</strong>
        <p>当前导出为富文本/Markdown/图片下载包。小黑盒公开入口未提供文章草稿箱创建 API，因此需要复制正文后粘贴到创作者后台。</p>
        <a href="{HEYBOX_CREATOR_URL}" target="_blank" rel="noopener">打开小黑盒创作者后台</a>
      </section>
      <section class="article">{article_html}</section>
      <section class="meta">
        <h2>图片下载清单</h2>
        <table>
          <thead><tr><th>位置</th><th>类型</th><th>PNG 下载</th><th>图注</th></tr></thead>
          <tbody>{manifest_rows}</tbody>
        </table>
      </section>
      <section class="meta">
        <h2>小黑盒导出 JSON</h2>
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


def make_export_id(draft: dict[str, Any], exported_at: datetime) -> str:
    raw = f"{draft.get('draft_id', '')}:{draft.get('title', '')}:{exported_at.isoformat()}"
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:10]
