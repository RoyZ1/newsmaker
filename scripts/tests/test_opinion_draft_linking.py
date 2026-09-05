from __future__ import annotations

import io
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import scripts._bootstrap  # noqa: F401,E402
import app.opinion_materials as opinion_materials
import app.opinion_draft_linker as opinion_draft_linker
import app.writer as writer
from PIL import Image


def png_bytes() -> io.BytesIO:
    buffer = io.BytesIO()
    image = Image.new("RGB", (320, 180), "#ffffff")
    for x in range(0, 80):
        for y in range(0, 180):
            image.putpixel((x, y), (255, 0, 0))
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def test_screenshot_import_saves_draft_ref() -> None:
    with TemporaryDirectory() as tmp:
        items_path = Path(tmp) / "opinion_items.json"
        import_dir = Path(tmp) / "imports"
        original_items_path = opinion_materials.OPINION_ITEMS_PATH
        original_import_dir = opinion_materials.IMPORTED_OPINION_DIR
        try:
            opinion_materials.OPINION_ITEMS_PATH = items_path
            opinion_materials.IMPORTED_OPINION_DIR = import_dir
            result = opinion_materials.import_opinion_screenshot(
                "manual",
                "青年就业",
                png_bytes(),
                "comment.png",
                note="评论认为招聘门槛更高了",
                draft_ref={
                    "draft_index": 1,
                    "draft_id": "draft-abc",
                    "topic_id": "topic-jobs",
                    "draft_title": "AI 岗位变化，年轻人找工作会更难吗？",
                },
            )
            imported_path = result["item"]["screenshot"]["local_path"]
            with Image.open(imported_path) as image:
                assert image.getpixel((20, 20)) == (255, 0, 0)
        finally:
            opinion_materials.OPINION_ITEMS_PATH = original_items_path
            opinion_materials.IMPORTED_OPINION_DIR = original_import_dir

    item = result["item"]
    assert item["draft_ref"]["draft_index"] == 1
    assert item["draft_ref"]["draft_id"] == "draft-abc"
    assert item["draft_ref"]["topic_id"] == "topic-jobs"
    assert item["draft_ref"]["draft_title"].startswith("AI 岗位变化")
    assert item["screenshot"]["url"].startswith("/static-data/opinion-imports/")
    assert item["privacy"]["anonymized"] is False
    assert "系统未修改截图内容" in item["screenshot"]["privacy_note"]


def test_select_opinion_materials_prefers_linked_draft_ref() -> None:
    opinions = [
        {
            "id": "linked",
            "platform": "manual",
            "topic": "截图素材",
            "text": "这条评论没有关键词，但属于当前草稿。",
            "source_type": "manual_screenshot",
            "draft_ref": {"draft_index": 0, "draft_id": "draft-1", "topic_id": "topic-1"},
            "screenshot": {"url": "/static-data/opinion-imports/linked.png"},
        },
        {
            "id": "keyword",
            "platform": "manual",
            "topic": "青年就业",
            "text": "青年就业关键词命中。",
            "source_type": "manual_text",
        },
    ]
    original_load = writer.load_opinion_items
    try:
        writer.load_opinion_items = lambda: opinions
        selected = writer.select_opinion_materials(
            {
                "id": "topic-1",
                "draft_index": 0,
                "draft_id": "draft-1",
                "title": "青年就业观察",
                "angle": "",
                "entities": ["青年就业"],
            }
        )
    finally:
        writer.load_opinion_items = original_load

    assert selected[0]["text"].startswith("这条评论没有关键词")
    assert selected[0]["draft_ref"]["draft_id"] == "draft-1"
    assert selected[0]["card_url"] == "/static-data/opinion-imports/linked.png"
    assert any(item["text"].startswith("青年就业关键词") for item in selected)


def test_opinion_screenshot_frontend_requires_draft_selection() -> None:
    html = (ROOT_DIR / "templates" / "index.html").read_text(encoding="utf-8")

    assert 'id="opinionScreenshotDraft"' in html
    assert "请先选择这张截图对应的草稿" in html
    assert 'formData.append("draft_index"' in html
    assert 'data-draft-id="{{ draft.draft_id' in html
    assert "关联草稿：" in html
    assert "导入并生成点评" in html
    assert "舆论图" in html


def test_apply_opinion_screenshot_updates_long_and_short_drafts() -> None:
    draft = {
        "draft_id": "draft-1",
        "topic_id": "topic-openai",
        "title": "OpenAI GPT-5.6 只给可信伙伴，普通人只能看？",
        "subtitle": "测试副标题",
        "body_markdown": "开头正文。\n\n## 门槛变化\n\n这里分析模型发布。",
        "source_links": [],
        "image_candidate_pool": [],
        "image_slots": [],
    }
    saved: dict[str, list] = {}
    cache: dict[str, dict] = {}
    synced: list[dict] = []

    original_load_drafts = opinion_draft_linker.load_drafts
    original_save_drafts = opinion_draft_linker.save_drafts_data
    original_load_cache = opinion_draft_linker.load_heybox_cache
    original_save_cache = opinion_draft_linker.save_heybox_cache
    original_sync = opinion_draft_linker.sync_platform_draft
    try:
        opinion_draft_linker.load_drafts = lambda: [draft]

        def fake_save_drafts(drafts, path=opinion_draft_linker.DRAFTS_PATH):
            saved["drafts"] = drafts

        opinion_draft_linker.save_drafts_data = fake_save_drafts
        opinion_draft_linker.load_heybox_cache = lambda: cache
        opinion_draft_linker.save_heybox_cache = lambda payload: cache.update(payload)
        opinion_draft_linker.sync_platform_draft = lambda payload: synced.append(dict(payload))

        result = opinion_draft_linker.apply_opinion_screenshot_to_draft(
            {"draft_index": 0, "draft_id": "draft-1", "topic_id": "topic-openai"},
            {
                "id": "opinion-1",
                "platform": "manual",
                "topic": draft["title"],
                "text": "这些跑分水分太大，用量不大的话 gpt5.5 并不贵，反倒 gl m5.3 挺贵。",
                "screenshot": {
                    "url": "/static-data/opinion-imports/opinion-shot.png",
                    "local_path": "data/opinion_imports/opinion-shot.png",
                },
            },
        )
    finally:
        opinion_draft_linker.load_drafts = original_load_drafts
        opinion_draft_linker.save_drafts_data = original_save_drafts
        opinion_draft_linker.load_heybox_cache = original_load_cache
        opinion_draft_linker.save_heybox_cache = original_save_cache
        opinion_draft_linker.sync_platform_draft = original_sync

    updated = saved["drafts"][0]
    assert "## 舆论反馈" in updated["body_markdown"]
    assert "跑分" in updated["body_markdown"]
    assert result["slot_id"] == "section-2"
    opinion_slot = next(slot for slot in updated["image_slots"] if slot["slot_id"] == result["slot_id"])
    selected = opinion_slot["selected_image"]
    assert selected["type"] == "opinion_screenshot"
    assert selected["url"] == "/static-data/opinion-imports/opinion-shot.png"
    assert selected["caption"]
    assert cache["draft-1"]["opinion_enriched"] is True
    assert "## 舆论反馈" in cache["draft-1"]["body_markdown"]
    assert synced[-1]["draft_id"] == "draft-1"


def test_garbled_opinion_text_falls_back_to_generic_analysis() -> None:
    analysis = opinion_draft_linker.build_opinion_analysis("???? GPT-5.6 ??????????????", "OpenAI")

    assert "????" not in analysis
    assert "评论区" in analysis


def main() -> None:
    tests = [
        test_screenshot_import_saves_draft_ref,
        test_select_opinion_materials_prefers_linked_draft_ref,
        test_opinion_screenshot_frontend_requires_draft_selection,
        test_apply_opinion_screenshot_updates_long_and_short_drafts,
        test_garbled_opinion_text_falls_back_to_generic_analysis,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
