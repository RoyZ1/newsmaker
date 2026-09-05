from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import ROOT_DIR
from app.database import sync_drafts
from app.formatting import enrich_drafts_layout
from app.image_client import generate_image
from app.image_history import find_latest_generated_slot_image, remember_slot_image
from app.image_audit import audit_candidates, choose_publishable_image
from app.image_candidates import append_generated_candidate, ensure_image_slots, find_slot, refresh_draft_image_candidates, upsert_candidate
from app.llm_client import chat_completion
from app.prompting import load_prompt_json
from app.writer import DRAFTS_PATH, load_drafts


COVER_PROMPT_PATH = ROOT_DIR / "config" / "prompts" / "cover_image_prompt.json"
GENERATED_IMAGE_URL_PREFIX = "/static-data/generated-images"

KNOWN_ENTITIES: dict[str, list[str]] = {
    "OpenAI": ["openai", "chatgpt", "gpt"],
    "Anthropic": ["anthropic", "claude"],
    "Microsoft": ["microsoft", "azure", "github", "微软"],
    "Google DeepMind": ["google", "deepmind", "gemini"],
    "NVIDIA": ["nvidia", "blackwell", "cuda", "gpu", "英伟达"],
    "DeepSeek": ["deepseek"],
    "Alibaba Qwen": ["alibaba", "qwen", "tongyi", "通义", "阿里"],
    "Tencent Hunyuan": ["tencent", "hunyuan", "腾讯", "混元"],
    "ByteDance Doubao": ["bytedance", "doubao", "volcengine", "字节", "豆包"],
    "Baidu ERNIE": ["baidu", "ernie", "文心", "百度"],
    "Zhipu GLM": ["zhipu", "glm", "智谱"],
    "Moonshot Kimi": ["moonshot", "kimi", "月之暗面"],
    "Apple": ["apple", "ipad", "ios", "macos", "苹果"],
    "Meta": ["meta", "llama"],
    "xAI": ["xai", "grok"],
}

PROMPT_TEXT_REPLACEMENTS = {
    "OpenAI": "a green abstract frontier-lab token",
    "ChatGPT": "a green abstract assistant token",
    "GPT": "a green abstract model token",
    "Anthropic": "a purple-orange abstract frontier-lab token",
    "Claude": "a purple-orange abstract model token",
    "Microsoft": "a blue enterprise-cloud token",
    "Azure": "a blue enterprise-cloud token",
    "DeepSeek": "a red-orange cost-efficient model token",
    "Google": "a multicolor search-and-cloud token",
    "DeepMind": "a blue-green research-lab token",
    "Gemini": "a blue-green abstract model token",
    "NVIDIA": "a green compute-infrastructure token",
    "Qwen": "a warm-gold open-model token",
    "Alibaba": "a warm-gold cloud-platform token",
    "Tencent": "a blue social-platform token",
    "ByteDance": "a dark-red content-platform token",
    "Baidu": "a blue search-platform token",
    "Zhipu": "a violet research-lab token",
    "Kimi": "a moonlit silver assistant token",
    "Apple": "a silver device-ecosystem token",
    "Meta": "a blue social-network token",
    "xAI": "a black-and-white challenger token",
    "Grok": "a black-and-white challenger token",
    "cost": "pricing pressure",
    "performance": "capability pressure",
    "label": "glowing surface detail",
    "labels": "glowing surface details",
    "chart": "abstract light structure",
    "charts": "abstract light structures",
    "graph": "abstract light structure",
    "graphs": "abstract light structures",
    "dashboard": "abstract light surface",
    "dashboards": "abstract light surfaces",
    "metric": "signal",
    "metrics": "signals",
    "curve": "light wave",
    "curves": "light waves",
    "binary code": "flowing light particles",
    "code": "light particles",
    "numbers": "abstract particles",
    "digits": "abstract particles",
    "letters": "abstract particles",
    "arrows": "directional light beams",
    "arrow": "directional light beam",
    "consumer icons": "consumer-side abstract tokens",
    "icon": "abstract token",
    "icons": "abstract tokens",
    "smiling face": "smooth circular token",
    "chat bubble": "rounded communication token",
    "data streams": "smooth light streams",
    "data stream": "smooth light stream",
    "8K": "high-resolution",
    "4K": "high-resolution",
    "UI": "abstract light surface",
    "headline": "empty title area",
    "headlines": "empty title areas",
}

THEME_HINTS: dict[str, list[str]] = {
    "model launch and benchmark rivalry": ["model", "gpt", "claude", "gemini", "qwen", "glm", "release", "launch", "open source", "开源", "模型"],
    "enterprise AI deployment and cost pressure": ["enterprise", "partner", "deployment", "cost", "pricing", "api", "agent", "企业", "成本"],
    "AI infrastructure and data-center competition": ["gpu", "nvidia", "data center", "inference", "server", "cloud", "芯片", "算力"],
    "robotics and world-model competition": ["robot", "robotics", "world model", "vla", "embodied", "机器人", "具身"],
    "capital market and acquisition game": ["funding", "valuation", "acquisition", "invest", "ipo", "融资", "收购"],
    "AI governance and safety debate": ["safety", "policy", "regulation", "government", "lawsuit", "governance", "监管", "安全"],
}


def generate_cover_images(limit: int | None = None, force: bool = False, prefer_official: bool = True) -> list[dict[str, Any]]:
    drafts = load_drafts()
    if not drafts:
        raise RuntimeError("No drafts found. Run python scripts/write/run_writer.py first.")

    results: list[dict[str, Any]] = []
    for index, draft in enumerate(drafts):
        if limit is not None and len(results) >= limit:
            break
        if not force and draft.get("cover_image", {}).get("local_path"):
            continue
        results.append(ensure_cover_for_draft(draft, index, force=force, prefer_official=prefer_official))

    save_drafts_data(drafts)
    return results


def generate_article_images(force: bool = False, prefer_official: bool = True) -> list[dict[str, Any]]:
    drafts = load_drafts()
    if not drafts:
        raise RuntimeError("No drafts found. Run python scripts/write/run_writer.py first.")

    results: list[dict[str, Any]] = []
    for index, draft in enumerate(drafts):
        results.extend(generate_images_for_draft_index(drafts, index, force=force, prefer_official=prefer_official))

    share_generated_candidates_across_drafts(drafts, results)
    save_drafts_data(drafts)
    return results


def generate_images_for_draft_index(
    drafts: list[dict[str, Any]],
    index: int,
    force: bool = False,
    prefer_official: bool = True,
) -> list[dict[str, Any]]:
    if index < 0 or index >= len(drafts):
        raise IndexError("Draft index out of range.")

    draft = drafts[index]
    results: list[dict[str, Any]] = []
    if force or cover_needs_publishable_image(draft):
        results.append(ensure_cover_for_draft(draft, index, force=force, prefer_official=prefer_official))
    ensure_image_slots(draft)
    return results


def share_generated_candidates_across_drafts(drafts: list[dict[str, Any]], results: list[dict[str, Any]]) -> None:
    shared_candidates = [shared_generated_candidate_from_result(result) for result in results if result.get("source") == "generated"]
    shared_candidates = [candidate for candidate in shared_candidates if candidate]
    if not shared_candidates:
        for draft in drafts:
            ensure_image_slots(draft)
        return

    for draft in drafts:
        pool = draft.get("image_candidate_pool")
        if not isinstance(pool, list):
            pool = []
        for candidate in reversed(shared_candidates):
            upsert_candidate(pool, candidate, prepend=True)
        draft["image_candidate_pool"] = pool
        ensure_image_slots(draft)


def shared_generated_candidate_from_result(result: dict[str, Any]) -> dict[str, Any] | None:
    image_url = str(result.get("image_url") or "").strip()
    if not image_url:
        return None
    return {
        "id": image_url,
        "url": image_url,
        "local_path": result.get("image_path", ""),
        "source_type": "generated_cover" if result.get("slot_id") == "cover" else "generated_section",
        "label": "生成图",
        "publishable": True,
        "selected": False,
        "prompt": result.get("image_prompt", ""),
        "visual_angle": result.get("visual_angle", ""),
        "entities": result.get("entities", []),
        "created_at": result.get("generated_at", datetime.now(timezone.utc).isoformat()),
        "slot_id": "",
    }


def ensure_cover_for_draft(
    draft: dict[str, Any],
    index: int,
    force: bool = False,
    prefer_official: bool = True,
) -> dict[str, Any]:
    audit_existing_draft_images(draft)
    if prefer_official:
        official = choose_publishable_image(draft.get("image_candidates", []), official=True)
        if official:
            record = official_cover_record(official.url, official.local_path, draft)
            draft["cover_image"] = record
            draft["final_images"] = [record]
            upsert_official_cover_plan(draft, record)
            refresh_draft_image_candidates(draft)
            return {
                "draft_index": index,
                "title": draft.get("title", ""),
                "image_prompt": "",
                "image_path": record["local_path"],
                "image_url": record["url"],
                "visual_angle": record["visual_angle"],
                "entities": [],
                "source": "official",
            }
    return generate_cover_for_draft(draft, index)


def generate_cover_for_draft(draft: dict[str, Any], index: int) -> dict[str, Any]:
    return generate_slot_image_for_draft(draft, index, "cover")


def generate_slot_image_for_draft(draft: dict[str, Any], index: int, slot_id: str = "cover") -> dict[str, Any]:
    slots = ensure_image_slots(draft)
    slot = find_slot(slots, slot_id)
    prompt_data = generate_cover_prompt(draft, slot)
    output_name = make_output_name(draft, index, slot_id)
    image_path = generate_image(prompt_data["image_prompt"], output_name=output_name, size="1024x1024")
    image_url = f"{GENERATED_IMAGE_URL_PREFIX}/{image_path.name}"
    image_type = "generated_cover" if slot_id == "cover" else "generated_section"
    record = {
        "type": image_type,
        "prompt": prompt_data["image_prompt"],
        "visual_angle": prompt_data.get("visual_angle", ""),
        "entities": prompt_data.get("entities", []),
        "safety_notes": prompt_data.get("safety_notes", []),
        "local_path": str(image_path),
        "url": image_url,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "slot_id": slot_id,
        "slot_label": slot.get("label", ""),
    }
    if slot_id == "cover":
        draft["cover_image"] = record
    append_generated_candidate(draft, record, slot_id=slot_id)
    remember_slot_image(draft, slot_id, record)
    upsert_generated_cover_plan(draft, record, slot)
    return {
        "draft_index": index,
        "title": draft.get("title", ""),
        "slot_id": slot_id,
        "slot_label": slot.get("label", ""),
        "image_prompt": record["prompt"],
        "image_path": record["local_path"],
        "image_url": record["url"],
        "visual_angle": record["visual_angle"],
        "entities": record["entities"],
        "generated_at": record["generated_at"],
        "source": "generated",
    }


def reuse_historical_slot_image(draft: dict[str, Any], index: int, slot_id: str) -> dict[str, Any] | None:
    record = find_latest_generated_slot_image(draft, slot_id)
    if not record:
        return None
    slots = ensure_image_slots(draft)
    slot = find_slot(slots, slot_id)
    append_generated_candidate(draft, record, slot_id=slot_id)
    if slot_id == "cover":
        draft["cover_image"] = record
    return {
        "draft_index": index,
        "title": draft.get("title", ""),
        "slot_id": slot_id,
        "slot_label": slot.get("label", ""),
        "image_prompt": record.get("prompt", ""),
        "image_path": record.get("local_path", ""),
        "image_url": record.get("url", ""),
        "visual_angle": record.get("visual_angle", ""),
        "entities": record.get("entities", []),
        "source": "history",
    }


def audit_existing_draft_images(draft: dict[str, Any]) -> None:
    candidates = draft.get("image_candidates", [])
    audits = [result.to_dict() for result in audit_candidates(candidates, official=True)]
    draft["image_audit"] = audits


def official_cover_record(url: str, local_path: str, draft: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "official",
        "prompt": "",
        "visual_angle": "官方来源图片，已通过基础质量审核",
        "entities": [],
        "safety_notes": ["官方来源图片；未检测到明显尺寸、单色或角标风险。"],
        "local_path": local_path,
        "url": url,
        "generated_at": "",
        "slot_id": "cover",
        "slot_label": "封面/开头配图",
        "source_links": draft.get("source_links", [])[:2],
    }


def generate_cover_prompt(draft: dict[str, Any], slot: dict[str, Any] | None = None) -> dict[str, Any]:
    brief = build_draft_brief(draft, slot)
    prompt_template = load_cover_prompt()
    source_json = json.dumps(brief, ensure_ascii=False, indent=2)
    messages = [
        {"role": "system", "content": prompt_template["system"]},
        {"role": "user", "content": prompt_template["user"].replace("{{DRAFT_BRIEF_JSON}}", source_json)},
    ]
    try:
        content = chat_completion(messages, temperature=0.62)
        parsed = parse_model_json(content)
    except Exception as exc:
        parsed = {"safety_notes": [f"LLM prompt generation failed, used local fallback: {exc}"]}

    if not isinstance(parsed, dict) or not parsed.get("image_prompt"):
        parsed = fallback_cover_prompt(brief)
    return normalize_prompt_data(parsed, brief)


def build_draft_brief(draft: dict[str, Any], slot: dict[str, Any] | None = None) -> dict[str, Any]:
    source_links = draft.get("source_links") or []
    source_briefs = [
        {
            "title": item.get("title", ""),
            "source_name": item.get("source_name", ""),
            "published_at": item.get("published_at", ""),
            "url": item.get("url", ""),
        }
        for item in source_links[:8]
        if isinstance(item, dict)
    ]
    text = "\n".join(
        [
            str(draft.get("topic_id", "")),
            str(draft.get("title", "")),
            str(draft.get("subtitle", "")),
            str(draft.get("body_markdown", ""))[:1400],
            json.dumps(source_briefs, ensure_ascii=False),
        ]
    )
    entities = extract_entities(text)
    themes = extract_themes(text)
    image_slot = {
        "slot_id": (slot or {}).get("slot_id", "cover"),
        "kind": (slot or {}).get("kind", "cover"),
        "label": (slot or {}).get("label", "封面/开头配图"),
        "position": (slot or {}).get("position", 0),
        "purpose": "article cover" if not slot or slot.get("kind") == "cover" else "section illustration under this heading",
    }
    return {
        "title": draft.get("title", ""),
        "subtitle": draft.get("subtitle", ""),
        "topic_id": draft.get("topic_id", ""),
        "image_slot": image_slot,
        "entities": entities,
        "themes": themes,
        "source_links": source_briefs,
        "body_excerpt": str(draft.get("body_markdown", ""))[:900],
        "official_image_candidates": draft.get("image_candidates", []),
        "cover_policy": {
            "generated_images_must_not_invent_real_logos": True,
            "official_logo_or_product_images_can_only_come_from_official_assets": True,
        },
    }


def extract_entities(text: str) -> list[str]:
    lowered = text.lower()
    entities = []
    for label, terms in KNOWN_ENTITIES.items():
        if any(term.lower() in lowered for term in terms):
            entities.append(label)
    return entities[:6]


def extract_themes(text: str) -> list[str]:
    lowered = text.lower()
    themes = []
    for label, terms in THEME_HINTS.items():
        if any(term.lower() in lowered for term in terms):
            themes.append(label)
    return themes[:4]


def fallback_cover_prompt(brief: dict[str, Any]) -> dict[str, Any]:
    entities = brief.get("entities") or ["major AI companies", "frontier model labs"]
    themes = brief.get("themes") or ["AI industry competition"]
    slot = brief.get("image_slot") or {}
    theme_phrase = themes[0]
    section_focus = ""
    if slot.get("kind") == "section":
        section_focus = " Focus the scene on one concrete section-level conflict rather than a broad article cover."
    image_prompt = (
        f"A premium high-tech editorial cover about {theme_phrase}, centered on several competing abstract AI-lab tokens. "
        "Show a strategic game table in a dark glass boardroom, with luminous abstract chess pieces, "
        "neural-network maps, cloud infrastructure lines, and executive silhouettes facing each other "
        "across the table. Use unlabeled color-coded tokens instead of real marks or readable symbols. "
        "The scene should feel like company rivalry and model competition, precise and serious, not generic. "
        f"{section_focus}"
        "Cinematic lighting, crisp reflections, deep graphite background, electric blue and restrained red accents, "
        "business magazine composition, sharp depth of field, clean negative space in the upper third for a WeChat title, "
        "no written marks, no watermarks, no interface panels, no real-person portrait."
    )
    return {
        "image_prompt": image_prompt,
        "visual_angle": f"{theme_phrase} shown as a strategic company rivalry",
        "entities": entities[:4],
        "safety_notes": ["Uses symbolic company tokens instead of invented logos."],
    }


def normalize_prompt_data(parsed: dict[str, Any], brief: dict[str, Any]) -> dict[str, Any]:
    fallback = fallback_cover_prompt(brief)
    prompt = str(parsed.get("image_prompt") or fallback["image_prompt"]).strip()
    if len(prompt.split()) < 45:
        prompt = fallback["image_prompt"]
    prompt = sanitize_image_prompt(prompt)
    prompt = enforce_prompt_guardrails(prompt)
    entities = parsed.get("entities") if isinstance(parsed.get("entities"), list) else brief.get("entities", [])
    safety_notes = parsed.get("safety_notes") if isinstance(parsed.get("safety_notes"), list) else []
    return {
        "image_prompt": prompt,
        "visual_angle": str(parsed.get("visual_angle") or fallback["visual_angle"]),
        "entities": [str(entity) for entity in entities][:6],
        "safety_notes": [str(note) for note in safety_notes][:5],
    }


def enforce_prompt_guardrails(prompt: str) -> str:
    guardrails = (
        " Absolutely no readable words, letters, numbers, captions, signage, interface panels, fake brand marks, real-person portraits, copied screenshots, or watermarks. "
        "Use only unlabeled abstract color-coded symbols when referring to companies."
    )
    lowered = prompt.lower()
    if "readable words" not in lowered or "fake logos" not in lowered:
        prompt += guardrails
    return re.sub(r"\s+", " ", prompt).strip()


def sanitize_image_prompt(prompt: str) -> str:
    sanitized = prompt
    for name, replacement in PROMPT_TEXT_REPLACEMENTS.items():
        sanitized = replace_prompt_term(sanitized, name, replacement)
    sanitized = re.sub(r"\b\d+\s*[A-Za-z]+\b", "large-scale", sanitized)
    sanitized = re.sub(r"\b[A-Za-z]+\s*\d+\b", "frontier-style", sanitized)
    sanitized = re.sub(r"\b\d[\d,\.]*\b", "many", sanitized)
    sanitized = re.sub(r"\b[A-Z]{2,}\b", "an unlabeled abstract token", sanitized)
    sanitized = sanitized.replace("fake logos", "fake brand marks")
    sanitized = sanitized.replace("real logos", "real brand marks")
    sanitized = sanitized.replace("invented logos", "invented brand marks")
    sanitized = sanitized.replace("branded token", "unlabeled abstract token")
    sanitized = sanitized.replace("brand-colored tokens", "unlabeled color-coded tokens")
    sanitized = sanitized.replace("No text", "No written symbols")
    sanitized = sanitized.replace("no text", "no written symbols")
    sanitized = sanitized.replace("pricing pressure labels", "pricing pressure represented by glowing color gradients")
    sanitized = sanitized.replace("capability pressure bars", "capability pressure represented by vertical light beams")
    sanitized = sanitized.replace("No readable text, abstract light structures", "No readable text, charts")
    sanitized = sanitized.replace("no readable text, abstract light structures", "no readable text, charts")
    return sanitized


def replace_prompt_term(text: str, term: str, replacement: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9 ]+", term):
        pattern = rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])"
    else:
        pattern = re.escape(term)
    return re.sub(pattern, replacement, text, flags=re.I)


def upsert_generated_cover_plan(draft: dict[str, Any], record: dict[str, Any], slot: dict[str, Any] | None = None) -> None:
    image_plan = draft.get("image_plan")
    if not isinstance(image_plan, list):
        image_plan = []
    slot_id = (slot or {}).get("slot_id", record.get("slot_id", "cover"))
    position = "article_cover" if slot_id == "cover" else str(slot_id)
    generated_item = {
        "position": position,
        "type": record.get("type", "generated_cover"),
        "description": record.get("visual_angle", "AI-generated editorial cover"),
        "prompt_or_url": record.get("prompt", ""),
        "generated_url": record.get("url", ""),
        "generated_path": record.get("local_path", ""),
        "slot_label": (slot or {}).get("label", record.get("slot_label", "")),
    }
    filtered = [
        item
        for item in image_plan
        if not (isinstance(item, dict) and item.get("position") == position)
    ]
    draft["image_plan"] = [generated_item, *filtered]


def upsert_official_cover_plan(draft: dict[str, Any], record: dict[str, Any]) -> None:
    image_plan = draft.get("image_plan")
    if not isinstance(image_plan, list):
        image_plan = []
    official_item = {
        "position": "article_cover",
        "type": "official",
        "description": record.get("visual_angle", "Official image"),
        "prompt_or_url": record.get("url", ""),
        "generated_url": record.get("url", ""),
        "generated_path": record.get("local_path", ""),
    }
    filtered = [
        item
        for item in image_plan
        if not (isinstance(item, dict) and item.get("position") == "article_cover")
    ]
    draft["image_plan"] = [official_item, *filtered]


def section_slot_needs_generated_image(slot: dict[str, Any], used_urls: set[str]) -> bool:
    selected = slot.get("selected_image")
    if not isinstance(selected, dict) or not selected.get("url"):
        return True
    source_type = str(selected.get("type") or selected.get("source_type") or "")
    url = str(selected.get("url"))
    if source_type == "media_preview":
        return True
    if source_type == "generated_cover":
        return True
    return url in used_urls


def cover_needs_publishable_image(draft: dict[str, Any]) -> bool:
    cover = draft.get("cover_image")
    if not isinstance(cover, dict) or not cover.get("url"):
        return True
    source_type = str(cover.get("type") or cover.get("source_type") or "")
    if source_type == "media_preview":
        return True
    return not bool(cover.get("local_path"))


def make_output_name(draft: dict[str, Any], index: int, slot_id: str = "cover") -> str:
    raw = f"{index}-{slot_id}-{draft.get('topic_id', '')}-{draft.get('title', '')}-{datetime.now(timezone.utc).isoformat()}"
    digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:10]
    safe_slot = re.sub(r"[^a-zA-Z0-9_-]+", "-", slot_id).strip("-") or "cover"
    return f"draft-{index + 1:02d}-{safe_slot}-{digest}.png"


def save_drafts_data(drafts: list[dict[str, Any]], path: Path = DRAFTS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = enrich_drafts_layout(drafts)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    sync_drafts(payload)


def load_cover_prompt(path: Path = COVER_PROMPT_PATH) -> dict[str, str]:
    return load_prompt_json(path)


def parse_model_json(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    match = re.search(r"\{.*\}", text, flags=re.S)
    if match:
        text = match.group(0)
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}
