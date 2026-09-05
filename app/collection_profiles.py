from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import ROOT_DIR


PROFILE_PATH = ROOT_DIR / "data" / "collection_profile.json"

DEFAULT_KEYWORDS = [
    "AI",
    "人工智能",
    "大模型",
    "模型",
    "Agent",
    "芯片",
    "算力",
    "机器人",
    "智能汽车",
    "自动驾驶",
    "OpenAI",
    "Anthropic",
    "Google",
    "NVIDIA",
    "华为",
    "腾讯",
    "阿里",
    "字节",
]

PROFILE_PRESETS: dict[str, dict[str, Any]] = {
    "tech": {
        "label": "科技资讯",
        "keywords": DEFAULT_KEYWORDS,
        "source_packages": ["tech", "official"],
        "search_engines": ["bing"],
    },
    "game": {
        "label": "游戏资讯",
        "keywords": [
            "游戏",
            "主机",
            "Steam",
            "PlayStation",
            "Xbox",
            "Nintendo",
            "Switch",
            "手游",
            "电竞",
            "Game",
            "gaming",
            "release",
            "trailer",
            "update",
        ],
        "source_packages": ["game", "media"],
        "search_engines": ["bing"],
    },
    "hot": {
        "label": "热点新闻",
        "keywords": [
            "热点",
            "最新",
            "突发",
            "新闻",
            "趋势",
            "社会",
            "民生",
            "政策",
            "国际",
            "breaking",
            "news",
            "world",
            "society",
            "China",
        ],
        "source_packages": ["hot", "media"],
        "search_engines": ["bing"],
    },
}

SOURCE_PACKAGES = [
    {"key": "official", "label": "公司官网/官方博客", "tags": ["official"]},
    {"key": "tech", "label": "科技/AI/芯片来源", "tags": ["ai", "tech", "industry", "developer", "cloud", "paper", "github", "business", "official"]},
    {"key": "game", "label": "游戏资讯来源", "tags": ["game", "gaming"]},
    {"key": "hot", "label": "热点新闻来源", "tags": ["hot", "news", "general"]},
    {"key": "media", "label": "媒体来源", "tags": ["media", "business"]},
]

SEARCH_ENGINES = [
    {"key": "bing", "label": "Bing/Edge 搜索结果", "source_ids": ["bing_news_search"]},
    {"key": "baidu", "label": "百度搜索结果", "source_ids": ["baidu_news_search"]},
]


def default_collection_profile() -> dict[str, Any]:
    preset = PROFILE_PRESETS["tech"]
    return {
        "preset": "tech",
        "keywords": list(preset["keywords"]),
        "source_packages": list(preset["source_packages"]),
        "search_engines": list(preset["search_engines"]),
    }


def load_collection_profile(path: Path = PROFILE_PATH) -> dict[str, Any]:
    if not path.exists():
        return default_collection_profile()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_collection_profile()
    return normalize_collection_profile(payload)


def save_collection_profile(payload: dict[str, Any], path: Path = PROFILE_PATH) -> dict[str, Any]:
    profile = normalize_collection_profile(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    return profile


def normalize_collection_profile(payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = payload or {}
    preset_key = str(payload.get("preset") or "tech")
    preset = PROFILE_PRESETS.get(preset_key, PROFILE_PRESETS["tech"])
    keywords = normalize_words(payload.get("keywords"))
    if not keywords:
        keywords = list(preset["keywords"])
    source_packages = normalize_choice_list(payload.get("source_packages"), {item["key"] for item in SOURCE_PACKAGES})
    if not source_packages:
        source_packages = list(preset["source_packages"])
    search_engines = normalize_choice_list(payload.get("search_engines"), {item["key"] for item in SEARCH_ENGINES})
    if not search_engines:
        search_engines = list(preset["search_engines"])
    return {
        "preset": preset_key if preset_key in PROFILE_PRESETS else "tech",
        "keywords": keywords,
        "source_packages": source_packages,
        "search_engines": search_engines,
    }


def collection_profile_context() -> dict[str, Any]:
    current = load_collection_profile()
    return {
        "current": current,
        "presets": [
            {"key": key, "label": value["label"], "keywords": value["keywords"], "source_packages": value["source_packages"], "search_engines": value["search_engines"]}
            for key, value in PROFILE_PRESETS.items()
        ],
        "source_packages": SOURCE_PACKAGES,
        "search_engines": SEARCH_ENGINES,
    }


def source_tags_for_packages(keys: list[str]) -> set[str]:
    selected = set(keys)
    tags: set[str] = set()
    for package in SOURCE_PACKAGES:
        if package["key"] in selected:
            tags.update(package["tags"])
    return tags


def source_ids_for_search_engines(keys: list[str]) -> set[str]:
    selected = set(keys)
    source_ids: set[str] = set()
    for engine in SEARCH_ENGINES:
        if engine["key"] in selected:
            source_ids.update(engine["source_ids"])
    return source_ids


def normalize_words(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = value.replace("，", ",").replace("、", ",").replace("\n", ",").split(",")
    elif isinstance(value, list):
        raw = [str(item) for item in value]
    else:
        raw = []
    seen: set[str] = set()
    result: list[str] = []
    for item in raw:
        word = " ".join(str(item).strip().split())
        key = word.lower()
        if word and key not in seen:
            seen.add(key)
            result.append(word)
    return result


def normalize_choice_list(value: Any, allowed: set[str]) -> list[str]:
    if isinstance(value, str):
        raw = value.replace("，", ",").replace("、", ",").split(",")
    elif isinstance(value, list):
        raw = [str(item) for item in value]
    else:
        raw = []
    return [item for item in raw if item in allowed]
