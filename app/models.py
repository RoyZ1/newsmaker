from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True)
class SourceConfig:
    id: str
    name: str
    type: str
    enabled: bool
    url: str
    tags: list[str] = field(default_factory=list)
    max_items: int | None = None
    item_selector: str | None = None
    title_selector: str | None = None
    summary_selector: str | None = None
    image_selector: str | None = None
    fallback_image: str | None = None
    date_selector: str | None = None
    fetch_article_images: bool | None = None
    require_published_at: bool = True
    include_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    keyword_scope: str = "all"
    list_path: str | None = None
    title_path: str | None = None
    link_path: str | None = None
    summary_path: str | None = None
    image_path: str | None = None
    published_path: str | None = None


@dataclass(slots=True)
class NewsItem:
    id: str
    source_id: str
    source_name: str
    title: str
    url: str
    summary: str = ""
    published_at: str | None = None
    score: float = 0.0
    collected_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    tags: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    local_images: list[str] = field(default_factory=list)
    image_usage: str = "none"
    category: str = ""
    category_label: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
