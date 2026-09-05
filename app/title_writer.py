from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import ROOT_DIR
from app.draft_store import ensure_draft_identity, save_draft_dicts, save_draft_version
from app.formatting import clean_body_markdown, strip_inline_rich_markers
from app.llm_client import chat_completion
from app.prompting import load_prompt_json
from app.title_format import format_title_with_prefix, strip_title_prefix
from app.writer import (
    DRAFTS_PATH,
    load_drafts,
    load_humanizer_rules,
    load_title_rules,
    parse_model_json,
    title_has_search_keyword,
    validate_title_quality,
)


TITLE_PROMPT_PATH = ROOT_DIR / "config" / "prompts" / "title_writer.json"


def rewrite_draft_title(draft_index: int) -> dict[str, Any]:
    drafts = load_drafts()
    if draft_index < 0 or draft_index >= len(drafts):
        raise IndexError("没有找到这篇草稿，可能已经被删除。")
    draft = drafts[draft_index]
    ensure_draft_identity(draft, draft_index)
    before_path = save_draft_version(draft, draft_index, "before-title-rewrite")

    result = generate_title_result(draft)
    draft["title"] = strip_title_prefix(result["title"])
    draft["subtitle"] = result["subtitle"]
    draft["title_candidates"] = [strip_title_prefix(title) for title in result["candidates"]]
    draft["updated_at"] = datetime.now(timezone.utc).isoformat()

    saved = save_draft_dicts(drafts, DRAFTS_PATH)
    after_path = save_draft_version(saved[draft_index], draft_index, "title-rewrite")
    return {
        "draft": saved[draft_index],
        "title": result["title"],
        "display_title": format_title_with_prefix(result["title"]),
        "subtitle": result["subtitle"],
        "candidates": result["candidates"],
        "display_candidates": [format_title_with_prefix(title) for title in result["candidates"]],
        "version_before_path": str(before_path),
        "version_saved_path": str(after_path),
    }


def apply_draft_title_choice(draft_index: int, title: str, subtitle: str | None = None) -> dict[str, Any]:
    drafts = load_drafts()
    if draft_index < 0 or draft_index >= len(drafts):
        raise IndexError("没有找到这篇草稿，可能已经被删除。")
    cleaned_title = strip_title_prefix(strip_inline_rich_markers(str(title or "")).strip())
    if not cleaned_title:
        raise ValueError("标题不能为空。")

    draft = drafts[draft_index]
    ensure_draft_identity(draft, draft_index)
    before_path = save_draft_version(draft, draft_index, "before-title-choice")
    draft["title"] = cleaned_title
    if subtitle is not None:
        draft["subtitle"] = strip_inline_rich_markers(str(subtitle or "")).strip()
    candidates = [strip_title_prefix(candidate) for candidate in draft.get("title_candidates", []) or []]
    draft["title_candidates"] = unique_titles([cleaned_title, *candidates])[:8]
    draft["updated_at"] = datetime.now(timezone.utc).isoformat()

    saved = save_draft_dicts(drafts, DRAFTS_PATH)
    after_path = save_draft_version(saved[draft_index], draft_index, "title-choice")
    return {
        "draft": saved[draft_index],
        "title": cleaned_title,
        "display_title": format_title_with_prefix(cleaned_title),
        "subtitle": saved[draft_index].get("subtitle", ""),
        "candidates": saved[draft_index].get("title_candidates", []),
        "display_candidates": [format_title_with_prefix(item) for item in saved[draft_index].get("title_candidates", [])],
        "version_before_path": str(before_path),
        "version_saved_path": str(after_path),
    }


def generate_title_result(draft: dict[str, Any]) -> dict[str, Any]:
    try:
        parsed = parse_model_json(chat_completion(build_title_messages(draft), temperature=0.82))
        result = normalize_title_result(parsed, draft)
        issues = validate_generated_title(result, draft)
        if issues:
            repaired = parse_model_json(chat_completion(build_repair_messages(draft, parsed, issues), temperature=0.56))
            result = normalize_title_result(repaired or parsed, draft)
        if validate_generated_title(result, draft):
            fallback = fallback_title_result(draft)
            fallback["generation_warning"] = "标题仍未通过质量校验，已使用本地兜底标题。"
            return fallback
        return result
    except Exception as exc:  # noqa: BLE001
        fallback = fallback_title_result(draft)
        fallback["generation_warning"] = f"标题大模型生成失败，已使用本地兜底标题：{exc}"
        return fallback


def build_title_messages(draft: dict[str, Any]) -> list[dict[str, str]]:
    prompt = load_title_prompt()
    brief = json.dumps(build_draft_brief(draft), ensure_ascii=False, indent=2)
    user = (
        prompt["user"]
        .replace("{{TITLE_RULES}}", load_title_rules())
        .replace("{{HUMANIZER_RULES}}", load_humanizer_rules())
        .replace("{{DRAFT_BRIEF_JSON}}", brief)
    )
    return [{"role": "system", "content": prompt["system"]}, {"role": "user", "content": user}]


def build_repair_messages(draft: dict[str, Any], previous: dict[str, Any], issues: list[str]) -> list[dict[str, str]]:
    return [
        *build_title_messages(draft),
        {"role": "assistant", "content": json.dumps(previous, ensure_ascii=False)},
        {
            "role": "user",
            "content": (
                "上一版标题不合格。只输出修正后的 JSON，不要解释。\n"
                f"问题：{'; '.join(issues)}\n"
                "标题必须有明确搜索关键词，必须更像真人判断，不要 AI 总结腔。"
            ),
        },
    ]


def build_draft_brief(draft: dict[str, Any]) -> dict[str, Any]:
    body = clean_body_markdown(str(draft.get("body_markdown") or ""))
    return {
        "current_title": draft.get("title", ""),
        "current_subtitle": draft.get("subtitle", ""),
        "topic_id": draft.get("topic_id", ""),
        "body_markdown_excerpt": trim_text(body, 2600),
        "entities": extract_entities(draft),
        "source_titles": [
            str(item.get("title") or "")
            for item in draft.get("source_links", []) or []
            if isinstance(item, dict) and item.get("title")
        ][:8],
    }


def normalize_title_result(parsed: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
    title = strip_title_prefix(strip_inline_rich_markers(str(parsed.get("title") or "")).strip())
    subtitle = strip_inline_rich_markers(str(parsed.get("subtitle") or "")).strip()
    candidates = parse_candidate_titles(parsed.get("candidates"))
    if not title:
        title = fallback_title_result(draft)["title"]
    if not subtitle:
        subtitle = fallback_title_result(draft)["subtitle"]
    candidates = unique_titles([title, *candidates, *fallback_title_result(draft)["candidates"]])[:6]
    better_title = choose_best_title(candidates, draft)
    if better_title:
        title = better_title
    return {
        "title": strip_title_prefix(clamp_title(title, 32)),
        "subtitle": trim_text(subtitle, 58),
        "candidates": [strip_title_prefix(candidate) for candidate in candidates],
    }


def parse_candidate_titles(candidates_raw: Any) -> list[str]:
    if not isinstance(candidates_raw, list):
        return []
    candidates: list[str] = []
    for item in candidates_raw:
        if isinstance(item, dict):
            value = item.get("title") or item.get("text") or item.get("name") or ""
        else:
            value = item
        cleaned = strip_title_prefix(strip_inline_rich_markers(str(value)).strip())
        if cleaned:
            candidates.append(cleaned)
    return candidates


def choose_best_title(candidates: list[str], draft: dict[str, Any]) -> str:
    valid = [title for title in candidates if not validate_generated_title({"title": title}, draft)]
    if not valid:
        return ""
    def score(title: str) -> tuple[int, int, int, int]:
        zh_count = len(re.findall(r"[\u4e00-\u9fff]", title))
        has_number = 1 if re.search(r"\d|万|亿|秒|公里|%", title) else 0
        human_hook = 1 if re.search(r"不只|不靠|没那么好混|为什么|怎么|吗|？|卖给|盯上|跑到|跑出|换打法|考卷|成绩|硬数字", title) else 0
        mismatch_penalty = 2 if number_entity_mismatch(title) else 0
        length_penalty = abs(zh_count - 22)
        return (has_number + human_hook - mismatch_penalty, -length_penalty, -zh_count, -mismatch_penalty)
    return sorted(valid, key=score, reverse=True)[0]


def validate_generated_title(result: dict[str, Any], draft: dict[str, Any]) -> list[str]:
    title = str(result.get("title") or "")
    issues = validate_title_quality(title, topic_from_draft(draft))
    if len(re.findall(r"[\u4e00-\u9fff]", title)) < 8:
        issues.append("标题太短，信息量不足")
    if not title_has_search_keyword(title, topic_from_draft(draft)):
        issues.append("标题缺少搜索关键词")
    if number_entity_mismatch(title):
        issues.append("标题里的数字和主体可能错配：不要把小米纽北 10分29秒写成华为成绩")
    return issues


def fallback_title_result(draft: dict[str, Any]) -> dict[str, Any]:
    entities = extract_entities(draft)
    keyword = entities[0] if entities else first_keyword_from_text(str(draft.get("title") or draft.get("body_markdown") or "AI"))
    body = clean_body_markdown(str(draft.get("body_markdown") or ""))
    number = headline_number_phrase(body)
    action = "这次不是只拼参数"
    if "自动驾驶" in body or "智驾" in body:
        action = "参数党没那么好混了"
    elif "芯片" in body or "算力" in body:
        action = "算力故事要看交付"
    elif "Agent" in body or "智能体" in body:
        action = "开始碰真实工作流"
    elif "RAG" in body:
        action = "这次不只靠向量"
    if number and keyword not in {"华为", "小米"}:
        title = f"{keyword}{number}，{action}"
    elif "智驾" in entities or "自动驾驶" in entities or "智驾" in body:
        title = "智驾好不好，现在开始看硬数字了"
    else:
        title = f"{keyword}这次不只讲概念"
    title = clamp_title(title, 32)
    subtitle = trim_text(strip_inline_rich_markers(str(draft.get("subtitle") or "先看具体数字，再看它改了谁的成本。")), 58)
    candidates = unique_titles(
        [
            title,
            f"{keyword}为什么突然值得盯紧",
            f"{keyword}不只是在刷存在感",
            f"{keyword}把问题推进到交付侧",
            f"不是热闹，是{keyword}开始验成绩",
        ]
    )
    return {"title": title, "subtitle": subtitle, "candidates": candidates}


def topic_from_draft(draft: dict[str, Any]) -> dict[str, Any]:
    entities = extract_entities(draft)
    return {
        "title": draft.get("title", ""),
        "angle": draft.get("subtitle", ""),
        "entities": entities,
        "themes": extract_entities({"body_markdown": draft.get("body_markdown", "")})[:6],
        "source_items": draft.get("source_links", []),
    }


def extract_entities(draft: dict[str, Any]) -> list[str]:
    text = " ".join(
        [
            str(draft.get("title") or ""),
            str(draft.get("subtitle") or ""),
            str(draft.get("body_markdown") or ""),
        ]
    )
    patterns = (
        r"OpenAI|ChatGPT|GPT-?\d*|Anthropic|Claude|Google|Gemini|DeepMind|Microsoft|微软|Copilot|Azure",
        r"NVIDIA|英伟达|华为|昇腾|盘古|鸿蒙|阿里|通义|Qwen|腾讯|混元|字节|豆包|百度|文心|DeepSeek|智谱|GLM|Kimi|小米|亚马逊|AWS",
        r"Agent|智能体|RAG|世界模型|具身智能|机器人|AI搜索|输入法|算力|芯片|GPU|自动驾驶|智驾|Bedrock|SageMaker",
    )
    found: list[str] = []
    for pattern in patterns:
        found.extend(match.group(0) for match in re.finditer(pattern, text, flags=re.I))
    return unique_titles(found)[:8]


def first_number_phrase(text: str) -> str:
    match = re.search(r"(\d+(?:\.\d+)?\s*(?:亿公里|万公里|公里|秒|分钟|亿|万|%|TOPS|tokens?|Token|参数))", text, flags=re.I)
    return match.group(1).replace(" ", "") if match else ""


def headline_number_phrase(text: str) -> str:
    patterns = (
        r"\d+(?:\.\d+)?\s*亿公里",
        r"\d+\s*分\s*\d+\s*秒(?:\s*\d+)?",
        r"\d+(?:\.\d+)?\s*万公里",
        r"\d+(?:\.\d+)?\s*TOPS",
    )
    matches: list[str] = []
    for pattern in patterns:
        matches.extend(match.group(0).replace(" ", "") for match in re.finditer(pattern, text, flags=re.I))
    return "，".join(matches[:2])


def number_entity_mismatch(title: str) -> bool:
    compact = re.sub(r"\s+", "", title)
    segments = [segment for segment in re.split(r"[，。！？、；：,;:!?]", compact) if segment]
    for segment in segments:
        if "华为" in segment and re.search(r"10分|29秒|纽北", segment):
            return True
        if "小米" in segment and re.search(r"120亿公里", segment):
            return True
    if re.search(r"华为[^，。！？、；：]{0,8}(?:10分|29秒|纽北)", compact):
        return True
    if re.search(r"小米[^，。！？、；：]{0,8}120亿公里", compact):
        return True
    return False


def first_keyword_from_text(text: str) -> str:
    match = re.search(r"[\u4e00-\u9fffA-Za-z0-9]{2,12}", text)
    return match.group(0) if match else "AI"


def unique_titles(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        cleaned = re.sub(r"\s+", "", strip_inline_rich_markers(str(item))).strip("，。！？、；：,.!?;: ")
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def clamp_title(title: str, limit: int) -> str:
    cleaned = re.sub(r"\s+", "", strip_inline_rich_markers(title)).strip("，。！？、；：,.!?;: ")
    if len(cleaned) <= limit:
        return cleaned
    for separator in ["：", ":", "？", "?", "，", ",", "！", "!", "；", ";", "、"]:
        if separator not in cleaned:
            continue
        head = cleaned.split(separator, 1)[0].strip()
        if 8 <= len(head) <= limit:
            return head
    return cleaned[: limit - 1].rstrip("，。！？、；：,.!?;: ") + "…"


def trim_text(text: str, limit: int) -> str:
    cleaned = re.sub(r"\s+", " ", strip_inline_rich_markers(str(text))).strip()
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1].rstrip("，。！？、；：,.!?;: ") + "…"


def load_title_prompt(path: Path = TITLE_PROMPT_PATH) -> dict[str, str]:
    return load_prompt_json(path)
