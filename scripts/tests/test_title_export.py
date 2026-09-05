from __future__ import annotations

import os
import sys
import json
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import scripts._bootstrap  # noqa: F401,E402
from app.heybox_automation import HEYBOX_TITLE_LIMIT, compact_heybox_title, heybox_title_units
from app.title_format import format_title_with_prefix
import app.title_writer as title_writer
import app.wechat_export as wechat_export
from app.wechat_export import WECHAT_TITLE_LIMIT, build_wechat_api_payload, make_wechat_safe_title


def test_wechat_title_keeps_full_reasonable_chinese_title() -> None:
    os.environ["TITLE_PREFIX"] = "每日快讯"
    raw_title = "华为智驾破120亿公里，小米纽北跑出纪录，AI开始交付了"
    expected = format_title_with_prefix(raw_title)

    safe_title = make_wechat_safe_title(expected)

    assert safe_title == expected
    assert len(safe_title) <= WECHAT_TITLE_LIMIT
    assert len(safe_title.encode("utf-8")) > 30


def test_wechat_payload_uses_safe_title_without_byte_truncation() -> None:
    os.environ["TITLE_PREFIX"] = "每日快讯"
    raw_title = "华为智驾破120亿公里，小米纽北跑出纪录，AI开始交付了"
    draft = {
        "title": raw_title,
        "subtitle": "这是一段摘要",
        "body_markdown": "正文",
    }

    payload = build_wechat_api_payload(draft, "<p>正文</p>", [])
    article = payload["draft_add_payload"]["articles"][0]

    assert article["title"] == format_title_with_prefix(raw_title)
    assert len(article["title"]) <= WECHAT_TITLE_LIMIT


def test_wechat_long_title_compacts_by_characters() -> None:
    os.environ["TITLE_PREFIX"] = "每日快讯"
    raw_title = "这是一个非常长的中文标题，用来模拟微信公众号草稿箱导入时标题不应该只剩下一半，而应该在官方字符限制附近优雅截断并保留前缀和主要信息"

    safe_title = make_wechat_safe_title(format_title_with_prefix(raw_title))

    assert safe_title.startswith("【每日快讯】")
    assert len(safe_title) <= WECHAT_TITLE_LIMIT
    assert safe_title.endswith("…")
    assert "只剩下一半" in safe_title


def test_heybox_title_still_compacts_to_platform_limit() -> None:
    os.environ["TITLE_PREFIX"] = "每日快讯"
    raw_title = "华为智驾破120亿公里，小米纽北跑出纪录，AI开始交付了"

    heybox_title = compact_heybox_title(raw_title)

    assert heybox_title_units(heybox_title) <= HEYBOX_TITLE_LIMIT
    assert heybox_title == raw_title
    assert not heybox_title.endswith("…")
    assert not heybox_title.endswith("...")
    assert "AI" in heybox_title


def test_heybox_title_does_not_add_ellipsis_when_truncated() -> None:
    os.environ["TITLE_PREFIX"] = "每日快讯"
    raw_title = "DeepSeek抢走美国企业，AI价格战比模型竞赛先来了"

    heybox_title = compact_heybox_title(raw_title)

    assert heybox_title_units(heybox_title) <= HEYBOX_TITLE_LIMIT
    assert heybox_title == f"【每日快讯】{raw_title}"
    assert "DeepSeek" in heybox_title
    assert "AI" in heybox_title
    assert not heybox_title.endswith("…")
    assert not heybox_title.endswith("...")


def test_wechat_export_and_clipboard_keep_title(monkey_patch: bool = True) -> None:
    os.environ["TITLE_PREFIX"] = "每日快讯"
    raw_title = "华为智驾破120亿公里，小米纽北跑出纪录，AI开始交付了"
    draft = {
        "draft_id": "test-draft",
        "topic_id": "test-topic",
        "title": raw_title,
        "subtitle": "这是一段摘要",
        "body_markdown": "## 第一部分\n\n正文内容",
        "image_slots": [],
        "selected_images": {},
    }

    original_load_drafts = wechat_export.load_drafts
    original_selected_variant = wechat_export.selected_article_variant
    original_sync = wechat_export.sync_wechat_export
    try:
        wechat_export.load_drafts = lambda: [dict(draft)]
        wechat_export.selected_article_variant = lambda draft_id, default="long": "long"
        wechat_export.sync_wechat_export = lambda export: None

        export = wechat_export.export_draft_for_wechat(0)
        clipboard = wechat_export.build_wechat_clipboard_payload(0)
    finally:
        wechat_export.load_drafts = original_load_drafts
        wechat_export.selected_article_variant = original_selected_variant
        wechat_export.sync_wechat_export = original_sync

    expected = format_title_with_prefix(raw_title)
    payload = json.loads(Path(export["json_path"]).read_text(encoding="utf-8"))
    assert payload["draft_add_payload"]["articles"][0]["title"] == expected
    assert clipboard["title"] == raw_title
    assert clipboard["display_title"] == expected
    assert expected in clipboard["plain_text"]


def test_apply_title_choice_updates_raw_title_and_candidates() -> None:
    os.environ["TITLE_PREFIX"] = "每日快讯"
    drafts = [
        {
            "draft_id": "test-choice-draft",
            "topic_id": "test-topic",
            "title": "旧标题",
            "subtitle": "旧摘要",
            "body_markdown": "正文",
            "title_candidates": ["候选一", "候选二"],
            "image_slots": [],
        }
    ]
    saved_payload: list[dict] = []
    version_reasons: list[str] = []

    original_load_drafts = title_writer.load_drafts
    original_save_draft_dicts = title_writer.save_draft_dicts
    original_save_draft_version = title_writer.save_draft_version
    try:
        title_writer.load_drafts = lambda: drafts

        def fake_save_draft_dicts(payload, path=title_writer.DRAFTS_PATH):
            saved_payload[:] = payload
            return payload

        def fake_save_draft_version(draft, draft_index, reason):
            version_reasons.append(reason)
            return Path(f"{reason}.json")

        title_writer.save_draft_dicts = fake_save_draft_dicts
        title_writer.save_draft_version = fake_save_draft_version

        result = title_writer.apply_draft_title_choice(0, "【每日快讯】人工改的新标题", "新摘要")
    finally:
        title_writer.load_drafts = original_load_drafts
        title_writer.save_draft_dicts = original_save_draft_dicts
        title_writer.save_draft_version = original_save_draft_version

    assert saved_payload[0]["title"] == "人工改的新标题"
    assert saved_payload[0]["subtitle"] == "新摘要"
    assert saved_payload[0]["title_candidates"][:3] == ["人工改的新标题", "候选一", "候选二"]
    assert result["display_title"] == "【每日快讯】人工改的新标题"
    assert result["display_candidates"][0] == "【每日快讯】人工改的新标题"
    assert version_reasons == ["before-title-choice", "title-choice"]


def main() -> None:
    tests = [
        test_wechat_title_keeps_full_reasonable_chinese_title,
        test_wechat_payload_uses_safe_title_without_byte_truncation,
        test_wechat_long_title_compacts_by_characters,
        test_heybox_title_still_compacts_to_platform_limit,
        test_heybox_title_does_not_add_ellipsis_when_truncated,
        test_wechat_export_and_clipboard_keep_title,
        test_apply_title_choice_updates_raw_title_and_candidates,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
