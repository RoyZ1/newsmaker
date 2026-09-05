from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import scripts._bootstrap  # noqa: F401,E402
import app.platform_preview as platform_preview
import app.heybox_writer as heybox_writer
import app.draft_store as draft_store
from app.heybox_export import apply_short_copy
from app.platform_variants import article_variant_choice, set_platform_variant


def test_article_variant_choice_maps_legacy_platform_values() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "platform_choices.json"
        set_platform_variant("draft-1", "article", "heybox", path=path)
        set_platform_variant("draft-2", "article", "wechat", path=path)

        assert article_variant_choice("draft-1", path=path)["variant"] == "short"
        assert article_variant_choice("draft-2", path=path)["variant"] == "long"
        assert article_variant_choice("draft-1", path=path)["label"] == "短文版"
        assert article_variant_choice("draft-missing", path=path)["variant"] == "long"


def test_platform_variant_saves_only_article_scope() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "platform_choices.json"
        try:
            set_platform_variant("draft-1", "heybox", "short", path=path)
        except ValueError as exc:
            assert "文章版本" in str(exc)
        else:
            raise AssertionError("expected non-article platform choice to fail")


def test_short_variant_uses_source_title_not_short_copy_title() -> None:
    source = {
        "title": "主草稿标题",
        "subtitle": "主草稿摘要",
        "body_markdown": "主草稿正文",
    }
    short_copy = {
        "title": "短文副本标题不应使用",
        "subtitle": "短文摘要",
        "body_markdown": "短文正文",
    }

    merged = apply_short_copy(source, short_copy)

    assert merged["title"] == "主草稿标题"
    assert merged["subtitle"] == "短文摘要"
    assert merged["body_markdown"] == "短文正文"
    assert merged["article_variant"] == "short"


def test_generated_short_copy_keeps_source_title_when_model_returns_other_title() -> None:
    source = {
        "draft_id": "short-title",
        "title": "主草稿标题",
        "subtitle": "主草稿摘要",
        "body_markdown": "主草稿正文。",
    }
    original_chat = heybox_writer.chat_completion
    try:
        heybox_writer.chat_completion = lambda messages, temperature=0.62: (
            '{"title":"模型给的短文标题","subtitle":"短文摘要","body_markdown":"短文正文，足够短。"}'
        )
        result = heybox_writer.generate_heybox_draft(source, 0, "hash")
    finally:
        heybox_writer.chat_completion = original_chat

    assert result["title"] == "主草稿标题"
    assert result["source_title"] == "主草稿标题"


def test_variant_preview_exposes_long_and_short_with_same_title() -> None:
    draft = {
        "draft_id": "preview-draft",
        "title": "统一标题",
        "subtitle": "长文摘要",
        "body_markdown": "长文开头。\n\n## 第一节\n\n长文正文。",
        "image_slots": [],
    }
    original_load_drafts = platform_preview.load_drafts
    original_load_short = platform_preview.load_or_create_heybox_draft
    original_choice = platform_preview.article_variant_choice
    try:
        platform_preview.load_drafts = lambda: [dict(draft)]
        platform_preview.load_or_create_heybox_draft = lambda draft_index: {
            "title": "短文标题不应展示",
            "subtitle": "短文摘要",
            "body_markdown": "短文正文。",
        }
        platform_preview.article_variant_choice = lambda draft_id, default="long": {
            "variant": "short",
            "label": "短文版",
        }

        preview = platform_preview.build_draft_variant_previews(0, "http://127.0.0.1:5050")
    finally:
        platform_preview.load_drafts = original_load_drafts
        platform_preview.load_or_create_heybox_draft = original_load_short
        platform_preview.article_variant_choice = original_choice

    assert set(preview["variants"]) == {"long", "short"}
    assert preview["variants"]["long"]["label"] == "长文版"
    assert preview["variants"]["short"]["label"] == "短文版"
    assert preview["variants"]["long"]["title"] == "统一标题"
    assert preview["variants"]["short"]["title"] == "统一标题"
    assert preview["variants"]["long"]["body_markdown"] == draft["body_markdown"]
    assert preview["variants"]["short"]["body_markdown"] == "短文正文。"


def test_short_variant_edit_saves_short_copy_without_overwriting_long_body() -> None:
    draft = {
        "draft_id": "edit-short-draft",
        "title": "原标题",
        "subtitle": "长文摘要",
        "body_markdown": "长文正文",
        "image_slots": [],
    }
    cache: dict[str, dict] = {
        "edit-short-draft": {
            "draft_id": "edit-short-draft",
            "title": "原标题",
            "subtitle": "旧短摘要",
            "body_markdown": "旧短正文",
            "source_hash": "old",
            "platform": "heybox",
        }
    }
    saved_drafts: dict[str, list] = {}
    synced: list[dict] = []

    original_load_drafts = draft_store.load_drafts
    original_save_draft_dicts = draft_store.save_draft_dicts
    original_save_draft_version = draft_store.save_draft_version
    original_load_cache = heybox_writer.load_heybox_cache
    original_save_cache = heybox_writer.save_heybox_cache
    original_sync = draft_store.sync_platform_draft
    try:
        draft_store.load_drafts = lambda: [dict(draft)]

        def fake_save_draft_dicts(payload, path=draft_store.DRAFTS_PATH):
            saved_drafts["payload"] = payload
            return payload

        draft_store.save_draft_dicts = fake_save_draft_dicts
        draft_store.save_draft_version = lambda draft, draft_index, reason: Path(f"{reason}.json")
        heybox_writer.load_heybox_cache = lambda: cache
        def fake_save_heybox_cache(payload):
            saved_cache = dict(payload)
            cache.clear()
            cache.update(saved_cache)

        heybox_writer.save_heybox_cache = fake_save_heybox_cache
        draft_store.sync_platform_draft = lambda payload: synced.append(dict(payload))

        result = draft_store.update_draft_from_payload(
            0,
            {
                "variant": "short",
                "title": "新标题",
                "subtitle": "新短摘要",
                "body_markdown": "新短正文",
            },
        )
    finally:
        draft_store.load_drafts = original_load_drafts
        draft_store.save_draft_dicts = original_save_draft_dicts
        draft_store.save_draft_version = original_save_draft_version
        heybox_writer.load_heybox_cache = original_load_cache
        heybox_writer.save_heybox_cache = original_save_cache
        draft_store.sync_platform_draft = original_sync

    assert result["variant"] == "short"
    assert saved_drafts["payload"][0]["title"] == "新标题"
    assert saved_drafts["payload"][0]["body_markdown"] == "长文正文"
    assert cache["edit-short-draft"]["subtitle"] == "新短摘要"
    assert cache["edit-short-draft"]["body_markdown"] == "新短正文"
    assert cache["edit-short-draft"]["manual_edited"] is True
    assert synced[-1]["body_markdown"] == "新短正文"


def main() -> None:
    tests = [
        test_article_variant_choice_maps_legacy_platform_values,
        test_platform_variant_saves_only_article_scope,
        test_short_variant_uses_source_title_not_short_copy_title,
        test_generated_short_copy_keeps_source_title_when_model_returns_other_title,
        test_variant_preview_exposes_long_and_short_with_same_title,
        test_short_variant_edit_saves_short_copy_without_overwriting_long_body,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
