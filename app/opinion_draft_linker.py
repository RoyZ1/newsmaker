from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from app.database import sync_platform_draft
from app.draft_store import ensure_draft_identity
from app.formatting import HEADING_RE, clean_body_markdown
from app.heybox_writer import load_heybox_cache, save_heybox_cache, stable_source_hash
from app.image_candidates import append_slot_candidate, ensure_image_slots, select_slot_image_candidate
from app.writer import DRAFTS_PATH, load_drafts
from app.cover_images import save_drafts_data


OPINION_SECTION_TITLE = "舆论反馈"


def apply_opinion_screenshot_to_draft(draft_ref: dict[str, Any], opinion_item: dict[str, Any]) -> dict[str, Any]:
    draft_index = int(draft_ref.get("draft_index", -1))
    drafts = load_drafts()
    if draft_index < 0 or draft_index >= len(drafts):
        raise IndexError("没有找到这张截图关联的草稿，请刷新页面后重试。")

    draft = drafts[draft_index]
    draft_id = ensure_draft_identity(draft, draft_index)
    screenshot = opinion_item.get("screenshot") if isinstance(opinion_item.get("screenshot"), dict) else {}
    image_url = str((screenshot or {}).get("url") or "").strip()
    if not image_url:
        raise ValueError("这条舆论素材没有可用截图。")

    comment_text = str(opinion_item.get("text") or "").strip()
    analysis = build_opinion_analysis(comment_text, str(opinion_item.get("topic") or draft.get("title") or ""))
    slot_id = ensure_opinion_section(draft, analysis)
    record = opinion_screenshot_record(opinion_item, slot_id, analysis)

    append_slot_candidate(draft, record, slot_id, selected=False)
    selected = select_slot_image_candidate(draft, slot_id, image_url)
    ensure_image_slots(draft)
    save_drafts_data(drafts)

    short_copy = update_short_copy_with_opinion(draft, draft_index, draft_id, analysis)
    return {
        "draft_index": draft_index,
        "draft_id": draft_id,
        "slot_id": slot_id,
        "analysis": analysis,
        "selected_image": selected,
        "short_copy": short_copy,
        "draft": drafts[draft_index],
    }


def ensure_opinion_section(draft: dict[str, Any], analysis: str) -> str:
    body = clean_body_markdown(str(draft.get("body_markdown") or ""))
    body = remove_existing_opinion_section(body)
    body = "\n\n".join(part for part in [body.strip(), f"## {OPINION_SECTION_TITLE}\n\n{analysis}"] if part).strip()
    draft["body_markdown"] = body
    ensure_image_slots(draft)
    return opinion_slot_id(draft)


def remove_existing_opinion_section(body: str) -> str:
    lines = body.splitlines()
    output: list[str] = []
    skipping = False
    for raw_line in lines:
        line = raw_line.strip()
        heading = HEADING_RE.match(line)
        if heading:
            title = heading.group(2).strip()
            if title == OPINION_SECTION_TITLE:
                skipping = True
                continue
            skipping = False
        if not skipping:
            output.append(raw_line)
    return "\n".join(output).strip()


def opinion_slot_id(draft: dict[str, Any]) -> str:
    position = 0
    for raw_line in str(draft.get("body_markdown") or "").splitlines():
        heading = HEADING_RE.match(raw_line.strip())
        if not heading:
            continue
        position += 1
        if heading.group(2).strip() == OPINION_SECTION_TITLE:
            return f"section-{position}"
    return f"section-{max(position, 1)}"


def build_opinion_analysis(comment_text: str, topic: str = "") -> str:
    normalized = normalize_comment_text(comment_text)
    lower = normalized.lower()
    skeptical_terms = ("跑分", "水分", "不续订", "不贵", "买不到", "贵", "门槛", "不值", "质疑", "担心")
    positive_terms = ("有兴趣", "选择", "认可", "期待", "能用", "划算", "续订", "支持")
    skeptical_hits = sum(1 for term in skeptical_terms if term.lower() in lower or term in normalized)
    positive_hits = sum(1 for term in positive_terms if term.lower() in lower or term in normalized)

    if skeptical_hits > positive_hits:
        stance = "这类反馈说明，用户的关注点并不只在模型名字和发布节奏上，而是更在意真实可用性、价格和自己能不能长期用得起。"
    elif positive_hits > skeptical_hits:
        stance = "这类反馈说明，用户并非完全排斥新模型，真正能打动他们的是明确的使用场景、稳定额度和看得见的效率提升。"
    else:
        stance = "这类反馈说明，普通用户正在把技术发布拉回到实际体验里：模型强不强是一回事，能不能稳定、便宜、方便地用上是另一回事。"

    quote = f"比如有用户提到：“{trim_sentence(normalized, 78)}”" if normalized else "从评论区的反馈看，讨论已经从技术参数延伸到真实使用成本"
    return f"{quote}。{stance} 如果这种感受继续扩散，后续舆论很可能不会只看榜单成绩，而会更看重产品是否真正降低了普通人的使用门槛。"


def normalize_comment_text(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    default_texts = {
        "手动导入评论截图，已提前处理个人信息。",
    }
    if text in default_texts:
        return ""
    if looks_garbled(text):
        return ""
    return text


def looks_garbled(text: str) -> bool:
    if not text:
        return False
    question_count = text.count("?")
    if question_count >= 4 and question_count / max(len(text), 1) > 0.18:
        return True
    return "????" in text


def trim_sentence(text: str, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip("，。,. ") + "…"


def opinion_screenshot_record(opinion_item: dict[str, Any], slot_id: str, analysis: str) -> dict[str, Any]:
    screenshot = opinion_item.get("screenshot") if isinstance(opinion_item.get("screenshot"), dict) else {}
    return {
        "type": "opinion_screenshot",
        "prompt": "",
        "visual_angle": "舆论截图",
        "entities": [],
        "safety_notes": ["系统未修改截图内容，默认认为上传前已处理个人信息；发布前仍建议人工复核。"],
        "local_path": str((screenshot or {}).get("local_path") or ""),
        "url": str((screenshot or {}).get("url") or ""),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "slot_id": slot_id,
        "slot_label": OPINION_SECTION_TITLE,
        "source_type": "opinion_screenshot",
        "source_name": str(opinion_item.get("platform") or "manual"),
        "source_title": str(opinion_item.get("topic") or OPINION_SECTION_TITLE),
        "caption": trim_sentence(analysis, 72),
        "manual_selected": True,
        "opinion_item_id": opinion_item.get("id", ""),
    }


def update_short_copy_with_opinion(draft: dict[str, Any], draft_index: int, draft_id: str, analysis: str) -> dict[str, Any]:
    cache = load_heybox_cache()
    source_hash = stable_source_hash(draft)
    existing = cache.get(draft_id) if isinstance(cache.get(draft_id), dict) else {}
    if not existing:
        body = fallback_short_body_from_long(draft)
        subtitle = str(draft.get("subtitle") or "")
    else:
        body = str(existing.get("body_markdown") or fallback_short_body_from_long(draft))
        subtitle = str(existing.get("subtitle") or draft.get("subtitle") or "")

    body = remove_existing_opinion_section(body)
    short_analysis = trim_sentence(analysis, 120)
    body = "\n\n".join(part for part in [body.strip(), f"## {OPINION_SECTION_TITLE}\n\n{short_analysis}"] if part).strip()
    short_copy = {
        **existing,
        "title": str(draft.get("title") or existing.get("title") or ""),
        "subtitle": subtitle,
        "body_markdown": body,
        "draft_index": draft_index,
        "draft_id": draft_id,
        "source_hash": source_hash,
        "source_title": str(draft.get("title") or ""),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "platform": "heybox",
        "opinion_enriched": True,
    }
    cache[draft_id] = short_copy
    save_heybox_cache(cache)
    sync_platform_draft(short_copy)
    return short_copy


def fallback_short_body_from_long(draft: dict[str, Any]) -> str:
    body = clean_body_markdown(str(draft.get("body_markdown") or ""))
    blocks: list[str] = []
    for raw_block in re.split(r"\n{2,}", body):
        block = raw_block.strip()
        if not block:
            continue
        if HEADING_RE.match(block):
            blocks.append(block)
        elif not block.startswith("["):
            blocks.append(trim_sentence(block, 120))
        if len(blocks) >= 6:
            break
    return "\n\n".join(blocks).strip()
