from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import ROOT_DIR
from app.database import sync_platform_draft
from app.draft_store import ensure_draft_identity
from app.formatting import HEADING_RE, clean_body_markdown, is_source_note, strip_inline_rich_markers
from app.llm_client import chat_completion
from app.prompting import load_prompt_json
from app.writer import load_drafts, load_humanizer_rules, load_title_rules, parse_model_json


HEYBOX_PROMPT_PATH = ROOT_DIR / "config" / "prompts" / "heybox_writer.json"
HEYBOX_DRAFTS_PATH = ROOT_DIR / "data" / "heybox_drafts.json"
MIN_ZH_COUNT = 180
MAX_ZH_COUNT = 800


def load_or_create_heybox_draft(draft_index: int, force: bool = False) -> dict[str, Any]:
    drafts = load_drafts()
    if draft_index < 0 or draft_index >= len(drafts):
        raise IndexError("没有找到这篇草稿，可能已经被删除。")

    source_draft = drafts[draft_index]
    draft_id = ensure_draft_identity(source_draft, draft_index)
    source_hash = stable_source_hash(source_draft)
    cache = load_heybox_cache()
    cached = cache.get(draft_id)
    if not force and valid_cached_draft(cached, source_hash):
        sync_platform_draft(cached)
        return dict(cached)

    result = generate_heybox_draft(source_draft, draft_index, source_hash)
    cache[draft_id] = result
    save_heybox_cache(cache)
    sync_platform_draft(result)
    return result


def regenerate_heybox_draft(draft_index: int) -> dict[str, Any]:
    return load_or_create_heybox_draft(draft_index, force=True)


def generate_heybox_draft(source_draft: dict[str, Any], draft_index: int, source_hash: str) -> dict[str, Any]:
    try:
        parsed = parse_model_json(chat_completion(build_heybox_messages(source_draft), temperature=0.62))
        result = normalize_heybox_output(parsed, source_draft)
        issues = validate_heybox_output(result)
        if issues:
            repair_messages = build_heybox_repair_messages(source_draft, parsed, issues)
            repaired = parse_model_json(chat_completion(repair_messages, temperature=0.46))
            result = normalize_heybox_output(repaired or parsed, source_draft)
    except Exception as exc:  # noqa: BLE001
        result = fallback_heybox_draft(source_draft)
        result["generation_warning"] = f"短文版大模型生成失败，已使用本地压缩兜底：{exc}"

    result["title"] = str(source_draft.get("title") or result.get("title") or "")
    result["draft_index"] = draft_index
    result["draft_id"] = ensure_draft_identity(source_draft, draft_index)
    result["source_hash"] = source_hash
    result["source_title"] = str(source_draft.get("title") or "")
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    result["platform"] = "heybox"
    return result


def build_heybox_messages(source_draft: dict[str, Any]) -> list[dict[str, str]]:
    prompt = load_heybox_prompt()
    source_json = json.dumps(build_source_brief(source_draft), ensure_ascii=False, indent=2)
    user = (
        prompt["user"]
        .replace("{{HUMANIZER_RULES}}", load_humanizer_rules())
        .replace("{{TITLE_RULES}}", load_title_rules())
        .replace("{{SOURCE_BRIEF_JSON}}", source_json)
    )
    return [{"role": "system", "content": prompt["system"]}, {"role": "user", "content": user}]


def build_heybox_repair_messages(source_draft: dict[str, Any], previous: dict[str, Any], issues: list[str]) -> list[dict[str, str]]:
    return [
        *build_heybox_messages(source_draft),
        {"role": "assistant", "content": json.dumps(previous, ensure_ascii=False)},
        {
            "role": "user",
            "content": (
                "这版短文不合格，请只输出修正后的 JSON，不要解释。\n"
                f"需要修正：{'; '.join(issues)}\n"
                "硬性要求：title 原样返回主草稿标题，不要另起标题；正文 250-650 个中文字符，最多 800；"
                "只保留 1 条主线，最多 2 个 ## 小标题；不要写来源、链接、综上、值得注意的是、标志着、赋能。"
            ),
        },
    ]


def build_source_brief(source_draft: dict[str, Any]) -> dict[str, Any]:
    body = clean_body_markdown(str(source_draft.get("body_markdown") or ""))
    return {
        "wechat_title": source_draft.get("title", ""),
        "wechat_subtitle": source_draft.get("subtitle", ""),
        "wechat_body_markdown": trim_text(body, 3600),
        "topic_id": source_draft.get("topic_id", ""),
        "source_items_for_internal_reference_only": simplify_source_items(source_draft.get("source_links", [])),
        "image_slots": [
            {
                "slot_id": slot.get("slot_id"),
                "label": slot.get("label"),
                "has_selected_image": bool((slot.get("selected_image") or {}).get("url")),
            }
            for slot in source_draft.get("image_slots", []) or []
            if isinstance(slot, dict)
        ],
    }


def simplify_source_items(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    simplified: list[dict[str, Any]] = []
    for item in items[:8]:
        if not isinstance(item, dict):
            continue
        simplified.append(
            {
                "title": item.get("title", ""),
                "source_name": item.get("source_name", ""),
                "published_at": item.get("published_at", ""),
                "summary": trim_text(str(item.get("summary") or item.get("content") or ""), 420),
            }
        )
    return simplified


def normalize_heybox_output(parsed: dict[str, Any], source_draft: dict[str, Any]) -> dict[str, Any]:
    title = strip_inline_rich_markers(str(source_draft.get("title") or parsed.get("title") or "")).strip()
    subtitle = strip_inline_rich_markers(str(parsed.get("subtitle") or source_draft.get("subtitle") or "")).strip()
    body = clean_short_body(str(parsed.get("body_markdown") or ""))
    if not body:
        fallback = fallback_heybox_draft(source_draft)
        body = fallback["body_markdown"]
        if not subtitle:
            subtitle = fallback["subtitle"]
    body = ensure_one_bold(body)
    return {
        "title": title,
        "subtitle": trim_sentence(subtitle, 48),
        "body_markdown": body,
    }


def validate_heybox_output(result: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    title = str(result.get("title") or "").strip()
    body = str(result.get("body_markdown") or "")
    zh_count = len(re.findall(r"[\u4e00-\u9fff]", body))
    heading_count = len(re.findall(r"^#{1,4}\s+", body, flags=re.M))
    if not title:
        issues.append("缺少标题")
    if zh_count < MIN_ZH_COUNT:
        issues.append(f"正文太短：约 {zh_count} 个中文字符")
    if zh_count > MAX_ZH_COUNT:
        issues.append(f"正文太长：约 {zh_count} 个中文字符")
    if heading_count > 2:
        issues.append(f"小标题太多：{heading_count} 个")
    banned_patterns = (
        r"来源",
        r"相关链接",
        r"参考链接",
        r"据.{0,12}(?:报道|称|透露|介绍)",
        r"报道称",
        r"资料显示",
        r"综上",
        r"值得注意的是",
        r"标志着",
        r"赋能",
        r"释放重要信号",
    )
    hits = [pattern for pattern in banned_patterns if re.search(pattern, body)]
    if hits:
        issues.append(f"正文有不适合小黑盒的套话或来源提示：{', '.join(hits[:5])}")
    return issues


def fallback_heybox_draft(source_draft: dict[str, Any]) -> dict[str, Any]:
    title = strip_inline_rich_markers(str(source_draft.get("title") or "")).strip()
    subtitle = trim_sentence(strip_inline_rich_markers(str(source_draft.get("subtitle") or "")), 42)
    body = clean_body_markdown(str(source_draft.get("body_markdown") or ""))
    intro_lines: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    current_title = ""
    current_lines: list[str] = []

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or is_source_note(line):
            continue
        heading = HEADING_RE.match(line)
        if heading:
            if current_title or current_lines:
                sections.append((current_title, current_lines))
            current_title = strip_inline_rich_markers(heading.group(2).strip())
            current_lines = []
            continue
        if current_title:
            current_lines.append(line)
        elif len(intro_lines) < 3 and not line.startswith("["):
            intro_lines.append(line)
    if current_title or current_lines:
        sections.append((current_title, current_lines))

    output: list[str] = [compress_paragraph(line, 92) for line in intro_lines[:2]]
    selected_sections = sections[:2] if sections else []
    for section_title, lines in selected_sections:
        if section_title:
            output.extend(["", f"## {trim_sentence(section_title, 22)}"])
        for line in lines[:2]:
            if line.startswith("["):
                continue
            output.append(compress_paragraph(line, 88))
    if not selected_sections:
        paragraphs = [compress_paragraph(line, 88) for line in body.splitlines() if line.strip() and not HEADING_RE.match(line.strip())]
        output.extend(paragraphs[:5])
    body_markdown = "\n\n".join(line for line in output if line).strip()
    body_markdown = ensure_one_bold(clean_short_body(body_markdown))
    return {"title": title, "subtitle": subtitle, "body_markdown": body_markdown}


def clean_short_body(body: str) -> str:
    lines: list[str] = []
    heading_count = 0
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or is_source_note(line):
            continue
        if re.match(r"^```", line):
            continue
        line = re.sub(r"^\s*[-*]\s+", "", line)
        heading = HEADING_RE.match(line)
        if heading:
            heading_count += 1
            if heading_count > 2:
                continue
            lines.append(f"## {trim_sentence(strip_inline_rich_markers(heading.group(2).strip()), 24)}")
            continue
        if line.startswith("#"):
            continue
        if "相关链接" in line or "参考链接" in line:
            continue
        lines.append(line)
    cleaned = "\n\n".join(lines).strip()
    return trim_body_to_limit(cleaned, MAX_ZH_COUNT)


def trim_body_to_limit(body: str, max_zh: int) -> str:
    if len(re.findall(r"[\u4e00-\u9fff]", body)) <= max_zh:
        return body
    blocks = [block.strip() for block in re.split(r"\n{2,}", body) if block.strip()]
    kept: list[str] = []
    count = 0
    for block in blocks:
        block_count = len(re.findall(r"[\u4e00-\u9fff]", block))
        if kept and count + block_count > max_zh:
            break
        kept.append(block)
        count += block_count
    return "\n\n".join(kept).strip() or trim_text(body, max_zh * 2)


def ensure_one_bold(body: str) -> str:
    if "**" in body:
        return body
    blocks = [block for block in re.split(r"(\n{2,})", body) if block]
    for index, block in enumerate(blocks):
        if block.startswith("#") or len(re.findall(r"[\u4e00-\u9fff]", block)) < 8:
            continue
        sentence = re.split(r"([。！？])", block, maxsplit=1)
        if len(sentence) >= 2:
            first = "".join(sentence[:2])
            blocks[index] = block.replace(first, f"**{first}**", 1)
        else:
            blocks[index] = f"**{block}**"
        break
    return "".join(blocks)


def stable_source_hash(draft: dict[str, Any]) -> str:
    relevant = {
        "title": draft.get("title", ""),
        "subtitle": draft.get("subtitle", ""),
        "body_markdown": draft.get("body_markdown", ""),
        "topic_id": draft.get("topic_id", ""),
    }
    raw = json.dumps(relevant, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()


def valid_cached_draft(cached: Any, source_hash: str) -> bool:
    return (
        isinstance(cached, dict)
        and cached.get("source_hash") == source_hash
        and bool(cached.get("title"))
        and bool(cached.get("body_markdown"))
    )


def load_heybox_cache(path: Path = HEYBOX_DRAFTS_PATH) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else {}


def save_heybox_cache(cache: dict[str, dict[str, Any]], path: Path = HEYBOX_DRAFTS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(cache, handle, ensure_ascii=False, indent=2)


def load_heybox_prompt(path: Path = HEYBOX_PROMPT_PATH) -> dict[str, str]:
    return load_prompt_json(path)


def trim_text(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def trim_sentence(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip("，。！？、；：,.!?;: ")
    return text if len(text) <= limit else text[: limit - 1].rstrip("，。！？、；：,.!?;: ") + "…"


def compress_paragraph(text: str, limit: int) -> str:
    text = strip_inline_rich_markers(text)
    text = re.sub(r"\[(?:配图|图片)\d*[^\]]*\]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip("，。！？、；：,.!?;: ") + "。"
