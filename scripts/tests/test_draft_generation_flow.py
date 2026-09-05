from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import scripts._bootstrap  # noqa: F401,E402
import app.workflow as workflow


def test_generate_drafts_uses_selected_topic_ids_in_user_order() -> None:
    topics = [
        {"id": "topic-a", "title": "选题 A"},
        {"id": "topic-b", "title": "选题 B"},
        {"id": "topic-c", "title": "选题 C"},
    ]
    captured: dict[str, object] = {}

    original_load_topics = workflow.load_topics
    original_generate_article_drafts = workflow.generate_article_drafts
    original_load_drafts = workflow.load_drafts
    original_draft_already_published = workflow.draft_already_published
    try:
        workflow.load_topics = lambda: topics

        def fake_generate_article_drafts(max_drafts: int = 3, topics: list[dict] | None = None) -> list:
            captured["max_drafts"] = max_drafts
            captured["topic_ids"] = [topic["id"] for topic in topics or []]
            return []

        workflow.generate_article_drafts = fake_generate_article_drafts
        workflow.load_drafts = lambda: [
            {"topic_id": "topic-c", "title": "选题 C 草稿"},
            {"topic_id": "topic-a", "title": "选题 A 草稿"},
        ]
        workflow.draft_already_published = lambda draft: False

        drafts = workflow.generate_drafts_checked(topic_ids=["topic-c", "topic-a", "topic-a"])
    finally:
        workflow.load_topics = original_load_topics
        workflow.generate_article_drafts = original_generate_article_drafts
        workflow.load_drafts = original_load_drafts
        workflow.draft_already_published = original_draft_already_published

    assert captured["max_drafts"] == 2
    assert captured["topic_ids"] == ["topic-c", "topic-a"]
    assert [draft["topic_id"] for draft in drafts] == ["topic-c", "topic-a"]


def test_generate_drafts_does_not_generate_images() -> None:
    topics = [{"id": "topic-a", "title": "选题 A"}]
    image_called = False

    original_load_topics = workflow.load_topics
    original_generate_article_drafts = workflow.generate_article_drafts
    original_generate_article_images = workflow.generate_article_images
    original_load_drafts = workflow.load_drafts
    original_draft_already_published = workflow.draft_already_published
    try:
        workflow.load_topics = lambda: topics
        workflow.generate_article_drafts = lambda max_drafts=3, topics=None: []

        def fake_generate_article_images(force: bool = False) -> list:
            nonlocal image_called
            image_called = True
            return []

        workflow.generate_article_images = fake_generate_article_images
        workflow.load_drafts = lambda: [{"topic_id": "topic-a", "title": "选题 A 草稿"}]
        workflow.draft_already_published = lambda draft: False

        workflow.generate_drafts_checked(topic_ids=["topic-a"])
    finally:
        workflow.load_topics = original_load_topics
        workflow.generate_article_drafts = original_generate_article_drafts
        workflow.generate_article_images = original_generate_article_images
        workflow.load_drafts = original_load_drafts
        workflow.draft_already_published = original_draft_already_published

    assert not image_called


def test_generate_drafts_requires_at_least_one_selected_topic() -> None:
    topics = [{"id": "topic-a", "title": "选题 A"}]

    try:
        workflow.select_topics_for_draft_generation(topics, [])
    except workflow.WorkflowError as exc:
        assert "至少选择一个选题" in exc.message
    else:
        raise AssertionError("empty selected topic list should fail")


def test_regenerate_single_draft_rewrites_text_but_preserves_image_state() -> None:
    cover = {
        "type": "manual_upload",
        "url": "/data/imported_images/manual-cover.png",
        "local_path": "data/imported_images/manual-cover.png",
        "slot_id": "cover",
        "manual_selected": True,
    }
    old_draft = {
        "topic_id": "topic-a",
        "title": "旧标题",
        "subtitle": "旧副标题",
        "body_markdown": "旧正文\n\n## 旧小标题\n\n旧内容",
        "source_links": [],
        "image_candidates": [],
        "cover_image": copy.deepcopy(cover),
        "final_images": [copy.deepcopy(cover)],
        "image_candidate_pool": [copy.deepcopy(cover)],
        "image_slots": [
            {
                "slot_id": "cover",
                "kind": "cover",
                "label": "封面/开头配图",
                "position": 0,
                "selected_image": copy.deepcopy(cover),
                "candidate_pool": [copy.deepcopy(cover)],
            }
        ],
    }
    new_draft = {
        "topic_id": "topic-a",
        "title": "新标题",
        "subtitle": "新副标题",
        "body_markdown": "新正文\n\n## 新小标题\n\n新内容",
        "source_links": [],
        "image_candidates": [],
        "image_plan": [],
        "originality_checklist": [],
    }
    saved: dict[str, object] = {}

    original_load_topics = workflow.load_topics
    original_load_drafts = workflow.load_drafts
    original_generate_article_draft_dict = workflow.generate_article_draft_dict
    original_save_drafts_data = workflow.save_drafts_data
    try:
        workflow.load_topics = lambda: [{"id": "topic-a", "title": "选题 A"}]
        workflow.load_drafts = lambda: [copy.deepcopy(old_draft)]
        workflow.generate_article_draft_dict = lambda topic: copy.deepcopy(new_draft)

        def fake_save_drafts_data(drafts: list[dict]) -> None:
            saved["drafts"] = copy.deepcopy(drafts)

        workflow.save_drafts_data = fake_save_drafts_data
        result = workflow.regenerate_single_draft_checked(0)
    finally:
        workflow.load_topics = original_load_topics
        workflow.load_drafts = original_load_drafts
        workflow.generate_article_draft_dict = original_generate_article_draft_dict
        workflow.save_drafts_data = original_save_drafts_data

    result_draft = result["draft"]
    assert result_draft["title"] == "新标题"
    assert result_draft["body_markdown"].startswith("新正文")
    assert result_draft["cover_image"]["url"] == cover["url"]
    assert result_draft["image_slots"][0]["selected_image"]["url"] == cover["url"]
    assert result["covers"] == []
    assert saved["drafts"][0]["cover_image"]["url"] == cover["url"]


def test_draft_picker_frontend_posts_selected_topic_ids() -> None:
    html = (ROOT_DIR / "templates" / "index.html").read_text(encoding="utf-8")

    assert 'id="draftTopicPicker"' in html
    assert "selectedDraftTopicIds" in html
    assert "topic_ids: selectedDraftTopicIds" in html
    assert "fillInitialDraftTopicIds" in html
    assert 'id="draftTopicFill"' in html
    assert "将生成 ${selectedDraftTopicIds.length} 篇" in html
    assert "editor-variant-note" in html
    assert "setDraftEditorVariant" in html
    assert "variant," in html
    assert "保存${variantLabel(variant)}" in html
    assert "重新生成文本" in html
    assert "重新生成本文" not in html


def main() -> None:
    tests = [
        test_generate_drafts_uses_selected_topic_ids_in_user_order,
        test_generate_drafts_does_not_generate_images,
        test_generate_drafts_requires_at_least_one_selected_topic,
        test_regenerate_single_draft_rewrites_text_but_preserves_image_state,
        test_draft_picker_frontend_posts_selected_topic_ids,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
