from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import scripts._bootstrap  # noqa: F401,E402
from app.collector import Collector
from app.models import SourceConfig
from app.topics import generate_topics


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


def github_record(index: int) -> dict:
    return {
        "full_name": f"owner/ai-project-{index}",
        "html_url": f"https://github.com/owner/ai-project-{index}",
        "description": f"AI agent workflow project number {index}",
        "created_at": "2026-06-30T00:00:00Z",
        "owner": {"avatar_url": ""},
    }


def test_github_json_collection_scans_beyond_source_max_items() -> None:
    collector = Collector()
    source = SourceConfig(
        id="github_ai_recent",
        name="GitHub Recent AI Repos",
        type="json",
        enabled=True,
        url="https://api.github.com/search/repositories?q=ai",
        list_path="items",
        title_path="full_name",
        link_path="html_url",
        summary_path="description",
        image_path="owner.avatar_url",
        published_path="created_at",
        tags=["github", "open-source"],
        include_keywords=["AI"],
        max_items=2,
    )
    collector._get = lambda _url: FakeResponse({"items": [github_record(index) for index in range(5)]})  # type: ignore[method-assign]

    items = collector._collect_json(source)

    assert len(items) == 5
    assert items[-1].title == "owner/ai-project-4"


def test_generated_topics_keep_one_github_project_even_when_low_scored() -> None:
    with TemporaryDirectory() as tmp:
        import app.topics as topics_module

        original_chat_completion = topics_module.chat_completion
        topics_module.chat_completion = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("llm unavailable"))
        items = [
            {
                "id": "hot-1",
                "title": "Major AI policy update changes hiring plans across large companies",
                "summary": "Companies say they will add jobs, while candidates say interviews are harder.",
                "source_name": "Tech News",
                "source_id": "tech_news",
                "url": "https://example.com/hot-1",
                "published_at": "2026-06-30T09:00:00+08:00",
                "score": 100,
            },
            {
                "id": "github-1",
                "title": "owner/useful-ai-agent",
                "summary": "An AI agent project that helps turn issue comments into runnable tasks.",
                "source_name": "GitHub Recent AI Repos",
                "source_id": "github_ai_recent",
                "url": "https://github.com/owner/useful-ai-agent",
                "published_at": "2026-06-30T10:00:00+08:00",
                "score": 1,
            },
        ]

        try:
            topics = generate_topics(max_topics=1, items=items, path=Path(tmp) / "topics.json")
        finally:
            topics_module.chat_completion = original_chat_completion

        assert len(topics) == 1
        topic = topics[0]
        assert any("github.com" in str(item.get("url", "")).lower() for item in topic.source_items)
        assert "GitHub" in topic.title
        assert "owner/useful-ai-agent" in topic.title


def test_default_topics_reserve_extra_slot_for_github_project() -> None:
    with TemporaryDirectory() as tmp:
        import app.topics as topics_module

        original_chat_completion = topics_module.chat_completion
        topics_module.chat_completion = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("llm unavailable"))
        subjects = [
            "robotics startup hiring",
            "chip export policy",
            "cloud database pricing",
            "video model launch",
            "smart car insurance",
            "campus job fair",
            "housing subsidy app",
            "mobile browser assistant",
            "office search tool",
            "game platform refund",
            "wearable health device",
            "local government service",
        ]
        items = [
            {
                "id": f"general-{index}",
                "title": f"{subject} changes daily work {index}",
                "summary": f"{subject} brings a separate story about users, jobs, policy, and spending.",
                "source_name": "Tech News",
                "source_id": f"tech_news_{index}",
                "url": f"https://example.com/general-{index}",
                "published_at": "2026-06-30T09:00:00+08:00",
                "score": 100 - index,
            }
            for index, subject in enumerate(subjects)
        ]
        items.append(
            {
                "id": "github-extra",
                "title": "owner/excellent-ai-tool",
                "summary": "A practical AI tool project with docs and active development.",
                "source_name": "GitHub Recent AI Repos",
                "source_id": "github_ai_recent",
                "url": "https://github.com/owner/excellent-ai-tool",
                "published_at": "2026-06-30T10:00:00+08:00",
                "score": 1,
            }
        )

        try:
            topics = generate_topics(items=items, path=Path(tmp) / "topics.json")
        finally:
            topics_module.chat_completion = original_chat_completion

        github_topics = [
            topic
            for topic in topics
            if any("github.com" in str(item.get("url", "")).lower() for item in topic.source_items)
        ]
        assert len(topics) == 11
        assert len(github_topics) == 1
        assert sum(1 for topic in topics if not any("github.com" in str(item.get("url", "")).lower() for item in topic.source_items)) == 10
        assert "owner/excellent-ai-tool" in github_topics[0].title


def main() -> None:
    tests = [
        test_github_json_collection_scans_beyond_source_max_items,
        test_generated_topics_keep_one_github_project_even_when_low_scored,
        test_default_topics_reserve_extra_slot_for_github_project,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
