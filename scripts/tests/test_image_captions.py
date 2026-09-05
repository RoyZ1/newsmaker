from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import scripts._bootstrap  # noqa: F401,E402
from app.formatting import selected_images_by_slot
from app.heybox_export import render_heybox_content_html, render_heybox_markdown
from app.image_candidates import ensure_image_slots, select_slot_image_candidate, update_slot_image_caption
from app.wechat_export import build_image_manifest, render_wechat_content_html
import app.cover_images as cover_images


def css_rule(selector: str) -> str:
    css = (ROOT_DIR / "static" / "style.css").read_text(encoding="utf-8")
    marker = f"{selector} {{"
    start = css.rfind(marker)
    if start < 0:
        return ""
    end = css.find("}", start)
    return css[start:end]


def caption_draft() -> dict:
    image = {
        "type": "manual_upload",
        "url": "/static-data/imported-images/test-caption.png",
        "local_path": "",
        "caption": "",
        "slot_id": "cover",
    }
    candidate = {
        "id": "test-caption",
        "url": image["url"],
        "source_type": "manual_upload",
        "publishable": True,
        "selected": True,
        "caption": "",
    }
    return {
        "title": "图解测试",
        "subtitle": "",
        "body_markdown": "开头正文。\n\n## 第一节\n\n继续正文。",
        "cover_image": dict(image),
        "final_images": [dict(image)],
        "image_candidate_pool": [dict(candidate)],
        "image_slots": [
            {
                "slot_id": "cover",
                "kind": "cover",
                "label": "封面/开头配图",
                "position": 0,
                "selected_image": dict(image),
                "candidate_pool": [dict(candidate)],
            }
        ],
    }


def test_slot_caption_updates_selected_image_and_exports() -> None:
    draft = caption_draft()

    result = update_slot_image_caption(draft, "cover", "  这是一句图解\n用于说明图片  ")

    assert result["caption"] == "这是一句图解 用于说明图片"
    assert draft["image_slots"][0]["selected_image"]["caption"] == "这是一句图解 用于说明图片"
    assert draft["cover_image"]["caption"] == "这是一句图解 用于说明图片"
    assert draft["final_images"][0]["caption"] == "这是一句图解 用于说明图片"
    ensure_image_slots(draft)
    selected_candidate = next(candidate for candidate in draft["image_slots"][0]["candidate_pool"] if candidate["selected"])
    assert selected_candidate["caption"] == "这是一句图解 用于说明图片"

    manifest = build_image_manifest(draft, "http://127.0.0.1:5050")
    wechat_html = render_wechat_content_html(draft, manifest, image_mode="local-preview")
    heybox_html = render_heybox_content_html(draft, manifest)
    heybox_markdown = render_heybox_markdown(draft, manifest, "http://127.0.0.1:5050")

    assert "这是一句图解 用于说明图片" in wechat_html
    assert "这是一句图解 用于说明图片" in heybox_html
    assert "*这是一句图解 用于说明图片*" in heybox_markdown


def test_exported_article_images_are_not_cropped_by_inline_styles() -> None:
    draft = caption_draft()
    manifest = build_image_manifest(draft, "http://127.0.0.1:5050")

    wechat_html = render_wechat_content_html(draft, manifest, image_mode="local-preview")
    heybox_html = render_heybox_content_html(draft, manifest)

    assert "height:auto" in wechat_html
    assert "object-fit:contain" in wechat_html
    assert "height:296px" not in wechat_html
    assert "object-fit:cover" not in wechat_html
    assert "max-height:430px" not in heybox_html
    assert "object-fit:contain" in heybox_html
    assert "object-fit:cover" not in heybox_html


def test_article_preview_css_preserves_full_inserted_images() -> None:
    wechat_rule = css_rule(".wechat-article-image img")
    draft_rule = css_rule(".draft-image")
    selected_candidate_rule = css_rule(".image-candidate-card.is-selected img")

    assert "height: auto" in wechat_rule
    assert "max-height: none" in wechat_rule
    assert "object-fit: contain" in wechat_rule
    assert "height: auto" in draft_rule
    assert "aspect-ratio: auto" in draft_rule
    assert "object-fit: contain" in draft_rule
    assert "height: auto" in selected_candidate_rule
    assert "object-fit: contain" in selected_candidate_rule


def manual_candidate(slot_id: str, url: str) -> dict:
    return {
        "id": url,
        "url": url,
        "local_path": "",
        "source_type": "manual_upload",
        "label": "导入图",
        "publishable": True,
        "selected": False,
        "reasons": [],
        "slot_id": slot_id,
        "caption": "",
    }


def test_manual_slot_upload_does_not_auto_duplicate_into_other_slots() -> None:
    draft = {
        "title": "测试标题",
        "subtitle": "",
        "body_markdown": "开头。\n\n## 标题一\n\n第一段。\n\n## 标题二\n\n第二段。",
        "image_candidate_pool": [],
        "image_slots": [],
    }
    section_one_url = "/static-data/imported-images/section-one.png"
    section_two_url = "/static-data/imported-images/section-two.png"

    ensure_image_slots(draft)
    draft["image_candidate_pool"].insert(0, manual_candidate("section-1", section_one_url))
    select_slot_image_candidate(draft, "section-1", section_one_url)
    ensure_image_slots(draft)

    selected = selected_images_by_slot(draft)
    assert selected["section-1"]["url"] == section_one_url
    assert "cover" not in selected
    assert "section-2" not in selected

    draft["image_candidate_pool"].insert(0, manual_candidate("section-2", section_two_url))
    select_slot_image_candidate(draft, "section-2", section_two_url)

    manifest = build_image_manifest(draft, "http://127.0.0.1:5050")
    wechat_html = render_wechat_content_html(draft, manifest, image_mode="local-preview")
    urls = [item["local_preview_url"] for item in manifest]
    placeholders = "".join(item["wechat_image_placeholder"] for item in manifest)
    assert urls.count("http://127.0.0.1:5050/static-data/imported-images/section-one.png") == 1
    assert urls.count("http://127.0.0.1:5050/static-data/imported-images/section-two.png") == 1
    assert "WECHAT_IMAGE_URL_COVER" not in placeholders
    assert wechat_html.count("section-one.png") == 1
    assert wechat_html.count("section-two.png") == 1


def test_existing_duplicate_manual_slot_selection_is_cleaned() -> None:
    image_url = "/static-data/imported-images/stale-section.png"
    auto_cover = {
        "type": "manual_upload",
        "url": image_url,
        "local_path": "",
        "slot_id": "cover",
    }
    manual_section = {
        **auto_cover,
        "slot_id": "section-1",
        "manual_selected": True,
    }
    draft = {
        "title": "测试标题",
        "subtitle": "",
        "body_markdown": "开头。\n\n## 标题一\n\n第一段。",
        "cover_image": dict(auto_cover),
        "final_images": [dict(auto_cover), dict(manual_section)],
        "image_candidate_pool": [manual_candidate("section-1", image_url)],
        "image_slots": [
            {
                "slot_id": "cover",
                "kind": "cover",
                "label": "封面/开头配图",
                "position": 0,
                "selected_image": dict(auto_cover),
                "candidate_pool": [],
            },
            {
                "slot_id": "section-1",
                "kind": "section",
                "label": "标题一",
                "position": 1,
                "selected_image": dict(manual_section),
                "candidate_pool": [manual_candidate("section-1", image_url)],
            },
        ],
    }

    ensure_image_slots(draft)

    selected = selected_images_by_slot(draft)
    assert "cover" not in selected
    assert selected["section-1"]["url"] == image_url
    assert [image["slot_id"] for image in draft["final_images"]] == ["section-1"]


def test_batch_generation_creates_one_ai_image_per_draft_and_reuses_across_slots() -> None:
    drafts = [
        {
            "title": f"标题 {index + 1}",
            "topic_id": f"topic-{index + 1}",
            "subtitle": "",
            "body_markdown": "开头。\n\n## 小标题一\n\n第一段。\n\n## 小标题二\n\n第二段。",
            "image_candidate_pool": [],
            "image_slots": [],
        }
        for index in range(3)
    ]
    calls: list[str] = []

    original_generate_image = cover_images.generate_image
    original_generate_cover_prompt = cover_images.generate_cover_prompt
    try:
        def fake_generate_image(prompt: str, output_name: str, size: str = "1024x1024") -> Path:
            calls.append(output_name)
            return ROOT_DIR / "data" / "generated_images" / output_name

        def fake_generate_cover_prompt(draft: dict, slot: dict | None = None) -> dict:
            return {
                "image_prompt": f"prompt for {draft['title']}",
                "visual_angle": "新闻配图",
                "entities": [],
                "safety_notes": [],
            }

        cover_images.generate_image = fake_generate_image
        cover_images.generate_cover_prompt = fake_generate_cover_prompt

        results: list[dict] = []
        for index in range(len(drafts)):
            results.extend(cover_images.generate_images_for_draft_index(drafts, index))
        cover_images.share_generated_candidates_across_drafts(drafts, results)
    finally:
        cover_images.generate_image = original_generate_image
        cover_images.generate_cover_prompt = original_generate_cover_prompt

    assert len(calls) == 3
    assert [result["slot_id"] for result in results] == ["cover", "cover", "cover"]
    generated_urls = {result["image_url"] for result in results}
    for draft in drafts:
        ensure_image_slots(draft)
        assert len([candidate for candidate in draft["image_candidate_pool"] if candidate.get("url") in generated_urls]) == 3
        for slot in draft["image_slots"]:
            slot_urls = {candidate.get("url") for candidate in slot.get("candidate_pool", [])}
            assert generated_urls.issubset(slot_urls)


def main() -> None:
    tests = [
        test_slot_caption_updates_selected_image_and_exports,
        test_exported_article_images_are_not_cropped_by_inline_styles,
        test_article_preview_css_preserves_full_inserted_images,
        test_manual_slot_upload_does_not_auto_duplicate_into_other_slots,
        test_existing_duplicate_manual_slot_selection_is_cleaned,
        test_batch_generation_creates_one_ai_image_per_draft_and_reuses_across_slots,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
