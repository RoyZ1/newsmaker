from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import scripts._bootstrap  # noqa: F401,E402
from app.collection_profiles import normalize_collection_profile
from app.collector import apply_collection_profile, expand_url_template
from app.config import load_sources


def test_profile_enables_selected_packages_and_search_engines() -> None:
    profile = normalize_collection_profile(
        {
            "keywords": "AI, chip, game",
            "source_packages": ["tech", "game"],
            "search_engines": ["bing"],
        }
    )
    enabled = [source.id for source in apply_collection_profile(load_sources(), profile) if source.enabled]

    assert "openai_news" in enabled
    assert "ign_feed" in enabled
    assert "bing_news_search" in enabled
    assert "baidu_news_search" not in enabled


def test_profile_accepts_keyword_arrays_from_editor() -> None:
    profile = normalize_collection_profile(
        {
            "keywords": ["AI", " ai ", "芯片", "", "  游戏  资讯  "],
            "source_packages": ["tech"],
            "search_engines": ["bing"],
        }
    )

    assert profile["keywords"] == ["AI", "芯片", "游戏 资讯"]


def test_hot_profile_enables_general_news_sources() -> None:
    profile = normalize_collection_profile(
        {
            "preset": "hot",
            "source_packages": ["hot", "media"],
            "search_engines": ["bing"],
        }
    )
    enabled = {source.id for source in apply_collection_profile(load_sources(), profile) if source.enabled}

    assert "bbc_world_feed" in enabled
    assert "npr_news_feed" in enabled
    assert "bing_news_search" in enabled


def test_game_profile_enables_expanded_game_sources() -> None:
    profile = normalize_collection_profile(
        {
            "preset": "game",
            "source_packages": ["game", "media"],
            "search_engines": ["bing"],
        }
    )
    enabled = {source.id for source in apply_collection_profile(load_sources(), profile) if source.enabled}

    assert "ign_feed" in enabled
    assert "polygon_feed" in enabled
    assert "pcgamer_feed" in enabled


def test_search_url_uses_active_keywords() -> None:
    url = expand_url_template(
        "https://example.com/search?q={query_plus}",
        {"active_keywords": ["AI", "chip", "game"], "lookback_hours": 72},
    )

    assert url.endswith("q=AI+chip+game")


def test_keyword_editor_uses_tokens_instead_of_delimited_textarea() -> None:
    html = (ROOT_DIR / "templates" / "index.html").read_text(encoding="utf-8")
    css = (ROOT_DIR / "static" / "style.css").read_text(encoding="utf-8")

    assert 'id="collectKeywordInput"' in html
    assert 'id="collectKeywordAdd"' in html
    assert 'id="collectKeywordList"' in html
    assert 'id="collectKeywords"' not in html
    assert "keywords: collectKeywords" in html
    assert ".keyword-token" in css
    assert ".keyword-remove" in css


def main() -> None:
    tests = [
        test_profile_enables_selected_packages_and_search_engines,
        test_profile_accepts_keyword_arrays_from_editor,
        test_hot_profile_enables_general_news_sources,
        test_game_profile_enables_expanded_game_sources,
        test_search_url_uses_active_keywords,
        test_keyword_editor_uses_tokens_instead_of_delimited_textarea,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
