from __future__ import annotations

import hashlib
import html
import json
import re
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus, urljoin, urlparse

if __package__ in {None, ""}:
    import sys

    sys.path.append(str(Path(__file__).resolve().parents[1]))

import feedparser
import httpx
from bs4 import BeautifulSoup

from app.collection_profiles import (
    load_collection_profile,
    normalize_collection_profile,
    save_collection_profile,
    source_ids_for_search_engines,
    source_tags_for_packages,
)
from app.config import DEFAULT_CONFIG_PATH, ROOT_DIR, get_app_settings, load_sources
from app.categories import classify_item, category_label
from app.models import NewsItem, SourceConfig
from app.storage import item_fingerprint, load_items, load_seen_items, save_items, update_seen_items


IMAGE_DIR = ROOT_DIR / "data" / "images"
TEXT_CLEAN_RE = re.compile(r"\s+")
LOCAL_TZ = timezone(timedelta(hours=8))
DATE_RE = re.compile(
    r"(?P<year>20\d{2})(?:[\/\-.]|" + "\u5e74" + r")\s*"
    r"(?P<month>\d{1,2})(?:[\/\-.]|" + "\u6708" + r")\s*"
    r"(?P<day>\d{1,2})"
)
TOP_ENTITIES = {
    "openai",
    "anthropic",
    "google",
    "deepmind",
    "gemini",
    "microsoft",
    "azure",
    "nvidia",
    "meta",
    "apple",
    "amazon",
    "aws",
    "xai",
    "tesla",
    "deepseek",
    "kimi",
    "moonshot",
    "zhipu",
    "glm",
    "qwen",
    "通义",
    "阿里",
    "阿里云",
    "腾讯",
    "混元",
    "字节",
    "豆包",
    "火山引擎",
    "百度",
    "文心",
    "智谱",
    "华为",
    "昇腾",
    "盘古",
    "鸿蒙",
    "harmonyos",
    "ascend",
    "pangu",
    "samsung",
    "qualcomm",
    "arm",
    "amd",
    "intel",
    "阶跃星辰",
    "minimax",
}
MODEL_RELEASE_TERMS = {
    "发布",
    "推出",
    "上线",
    "开源",
    "升级",
    "release",
    "launch",
    "introducing",
    "announce",
    "open-source",
    "open source",
}
HIGH_VALUE_TERMS = {
    "大模型",
    "模型",
    "llm",
    "agent",
    "智能体",
    "多模态",
    "推理",
    "robot",
    "机器人",
    "具身",
    "论文",
    "paper",
    "arxiv",
    "benchmark",
    "sota",
    "融资",
    "投资",
    "收购",
    "合作",
    "芯片",
    "算力",
    "半导体",
    "ai pc",
    "智能汽车",
    "自动驾驶",
    "端侧",
    "边缘计算",
    "cloud",
    "semiconductor",
    "chip",
    "inference",
    "ceo",
    "黄仁勋",
    "马斯克",
    "奥特曼",
}
AGGREGATION_TERMS = {
    "8点1氪",
    "晚报",
    "日报",
    "周报",
    "早报",
    "newsletter",
    "roundup",
}
SOURCE_BASE_SCORES = {
    "openai_news": 35,
    "anthropic_news": 34,
    "deepmind_blog": 34,
    "deepseek_updates": 33,
    "zhipu_news": 31,
    "kimi_blog": 30,
    "huggingface_blog": 24,
    "qbitai_feed": 20,
    "ithome_feed": 18,
    "infoq_feed": 18,
    "techcrunch_ai": 18,
    "google_ai_blog": 28,
    "nvidia_blog": 26,
    "huawei_cn_news": 28,
    "huawei_en_news": 25,
    "aws_ml_blog": 23,
    "microsoft_official_blog": 26,
    "samsung_global_newsroom": 20,
    "arxiv_ai": 24,
    "36kr_feed": 17,
    "leiphone_feed": 18,
    "cnblogs_news": 16,
    "github_ai_recent": 8,
}
SOURCE_LIMITS = {
    "github_ai_recent": 6,
    "github_trending_python": 4,
}


class Collector:
    def __init__(self, config_path: Path = DEFAULT_CONFIG_PATH, collection_options: dict[str, Any] | None = None) -> None:
        self.config_path = config_path
        self.settings = get_app_settings(config_path)
        self.collection_profile = normalize_collection_profile(collection_options) if collection_options else load_collection_profile()
        self.settings["active_keywords"] = list(self.collection_profile.get("keywords", []))
        self.sources = apply_collection_profile(load_sources(config_path), self.collection_profile)
        self.errors: list[dict[str, str]] = []
        timeout = self.settings.get("request_timeout_seconds", 20)
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": self.settings.get("user_agent", "AI-News-Agent/0.1"),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )

    def close(self) -> None:
        self.client.close()

    def collect(self) -> list[NewsItem]:
        initialize_seen_from_existing_items()
        items: list[NewsItem] = []
        for source in self.sources:
            if not source.enabled:
                continue
            try:
                if source.type == "rss":
                    source_items = self._collect_rss(source)
                elif source.type == "web":
                    source_items = self._collect_web(source)
                elif source.type == "json":
                    source_items = self._collect_json(source)
                elif source.type == "changelog":
                    source_items = self._collect_changelog(source)
                else:
                    source_items = []
                items.extend(source_items)
            except Exception as exc:  # noqa: BLE001
                message = format_collect_error(exc)
                self.errors.append(
                    {
                        "source_id": source.id,
                        "source_name": source.name,
                        "message": message,
                    }
                )
                print(f"[collector] {source.name} failed: {message}")

        deduped = self._dedupe(items)
        if self.settings.get("skip_seen_items", True):
            deduped = filter_seen_items(deduped)
        for item in deduped:
            item.score, item.raw["score_reasons"] = score_item(item)
            category = classify_item(item.to_dict())
            item.category = category
            item.category_label = category_label(category)
        sorted_items = sorted(
            deduped,
            key=lambda item: (item.score, item.published_at or item.collected_at),
            reverse=True,
        )
        sorted_items = apply_source_limits(
            sorted_items,
            self.settings.get("target_items", 50),
            self.settings.get("source_limits", SOURCE_LIMITS),
            self.settings.get("source_minimums", {}),
        )
        save_items(sorted_items)
        return sorted_items

    def _collect_rss(self, source: SourceConfig) -> list[NewsItem]:
        source_url = expand_url_template(source.url, self.settings, source.id)
        response = self._get(source_url)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        max_items = source.max_items or self.settings.get("max_items_per_source", 8)
        scan_limit = max_items * 5
        items: list[NewsItem] = []

        for entry in feed.entries[:scan_limit]:
            link = entry.get("link", source_url)
            title = clean_text(entry.get("title", "Untitled"))
            summary = clean_html(entry.get("summary") or entry.get("description") or "")
            if not item_matches_keywords(title, summary, source):
                continue
            published_at = parse_feed_date(entry)
            image_urls = self._extract_feed_images(entry, link)

            if not image_urls and should_fetch_article_images(source, self.settings):
                image_urls = self._fetch_article_images(link, source)
            if not image_urls and source.fallback_image:
                image_urls = [source.fallback_image]

            items.append(
                NewsItem(
                    id=self._make_id(source.id, link, title),
                    source_id=source.id,
                    source_name=source.name,
                    title=title,
                    url=link,
                    summary=summary,
                    published_at=published_at,
                    tags=source.tags,
                    images=image_urls,
                    local_images=self._cache_images(image_urls, source.id),
                    raw={"feed_title": feed.feed.get("title", source.name)},
                )
            )
            if len(items) >= max_items:
                break

        return self._finalize_items(items, source)

    def _collect_web(self, source: SourceConfig) -> list[NewsItem]:
        source_url = expand_url_template(source.url, self.settings, source.id)
        response = self._get(source_url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        max_items = source.max_items or self.settings.get("max_items_per_source", 8)
        item_selector = source.item_selector or "a"
        anchors = soup.select(item_selector)
        items: list[NewsItem] = []
        seen_urls: set[str] = set()

        for anchor in anchors:
            href = anchor.get("href")
            if not href:
                continue
            url = urljoin(source_url, href)
            normalized = normalize_url(url)
            if normalized in seen_urls or not is_http_url(url):
                continue
            if normalized == normalize_url(source_url):
                continue
            seen_urls.add(normalized)

            title = clean_text(anchor.get_text(" ", strip=True))
            if not title:
                title = urlparse(url).path.strip("/").split("/")[-1].replace("-", " ")
            if not item_matches_keywords(title, "", source):
                continue

            detail = self._fetch_article_detail(url, source)
            date_text = clean_text(anchor.get_text(" ", strip=True))
            item = NewsItem(
                id=self._make_id(source.id, url, title),
                source_id=source.id,
                source_name=source.name,
                title=detail.get("title") or title,
                url=url,
                summary=detail.get("summary", ""),
                published_at=detail.get("published_at")
                or normalize_datetime(extract_date_text(date_text)),
                tags=source.tags,
                images=detail.get("images", []) or ([source.fallback_image] if source.fallback_image else []),
                local_images=self._cache_images(
                    detail.get("images", []) or ([source.fallback_image] if source.fallback_image else []),
                    source.id,
                ),
            )
            if not item_matches_keywords(item.title, item.summary, source):
                continue
            if not source.require_published_at or item.published_at:
                items.append(item)
            if len(items) >= max_items:
                break

        return self._finalize_items(items, source)

    def _collect_json(self, source: SourceConfig) -> list[NewsItem]:
        source_url = expand_url_template(source.url, self.settings, source.id)
        response = self._get(source_url)
        response.raise_for_status()
        payload = response.json()
        records = get_by_path(payload, source.list_path or "") or []
        if not isinstance(records, list):
            records = []

        max_items = source.max_items or self.settings.get("max_items_per_source", 8)
        collect_limit = json_collect_limit(source, int(max_items))
        items: list[NewsItem] = []
        for record in records[: max_items * 5]:
            title = clean_text(str(get_by_path(record, source.title_path or "title") or ""))
            link = str(get_by_path(record, source.link_path or "url") or source_url)
            summary = clean_text(str(get_by_path(record, source.summary_path or "summary") or ""))
            if not item_matches_keywords(title, summary, source):
                continue
            image = str(get_by_path(record, source.image_path or "image") or "")
            published_at = str(get_by_path(record, source.published_path or "published_at") or "")
            images = [image] if image and is_http_url(image) else []
            if not images and source.fallback_image:
                images = [source.fallback_image]
            if not title:
                title = urlparse(link).path.strip("/").split("/")[-1].replace("-", " ")

            items.append(
                NewsItem(
                    id=self._make_id(source.id, link, title),
                    source_id=source.id,
                    source_name=source.name,
                    title=title,
                    url=link,
                    summary=summary,
                    published_at=normalize_datetime(published_at),
                    tags=source.tags,
                    images=images,
                    local_images=self._cache_images(images, source.id),
                    raw={"json_record": record},
                )
            )
            if len(items) >= collect_limit:
                break
        return self._finalize_items(items, source)

    def _collect_changelog(self, source: SourceConfig) -> list[NewsItem]:
        source_url = expand_url_template(source.url, self.settings, source.id)
        response = self._get(source_url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        max_items = source.max_items or self.settings.get("max_items_per_source", 8)
        headings = soup.select(source.item_selector or "h2, h3")
        items: list[NewsItem] = []

        for heading in headings:
            title = clean_text(heading.get_text(" ", strip=True))
            if not item_matches_keywords(title, "", source):
                continue
            published_at = normalize_datetime(extract_date_text(title))
            if not published_at:
                continue
            body_parts: list[str] = []
            for sibling in heading.find_next_siblings():
                if sibling.name in {"h1", "h2", "h3"}:
                    break
                text = clean_text(sibling.get_text(" ", strip=True))
                if text:
                    body_parts.append(text)
                if len(" ".join(body_parts)) > 500:
                    break

            items.append(
                NewsItem(
                    id=self._make_id(source.id, source.url, title),
                    source_id=source.id,
                    source_name=source.name,
                    title=title,
                    url=source_url,
                    summary=" ".join(body_parts)[:500],
                    published_at=published_at,
                    tags=source.tags,
                    images=[source.fallback_image] if source.fallback_image else [],
                    local_images=self._cache_images(
                        [source.fallback_image] if source.fallback_image else [],
                        source.id,
                    ),
                )
            )
            if len(items) >= max_items:
                break

        return self._finalize_items(items, source)

    def _fetch_article_detail(self, url: str, source: SourceConfig) -> dict[str, Any]:
        try:
            response = self._get(url)
            response.raise_for_status()
        except Exception:
            return {"images": []}

        soup = BeautifulSoup(response.text, "html.parser")
        title = select_text_or_meta(soup, source.title_selector) or select_text_or_meta(
            soup, "meta[property='og:title']"
        )
        summary = select_text_or_meta(soup, source.summary_selector) or select_text_or_meta(
            soup, "meta[name='description']"
        )
        image = select_attr_or_text(soup, source.image_selector, url)
        if not image:
            image = select_attr_or_text(soup, "meta[property='og:image']", url)
        images = [image] if image else []

        if not images:
            images = self._extract_inline_images(soup, url)

        published_at = extract_page_datetime(soup, response.text, source.date_selector)

        return {
            "title": clean_text(title),
            "summary": clean_text(summary),
            "images": images[:3],
            "published_at": normalize_datetime(published_at),
        }

    def _fetch_article_images(self, url: str, source: SourceConfig) -> list[str]:
        return self._fetch_article_detail(url, source).get("images", [])

    def _extract_feed_images(self, entry: Any, base_url: str) -> list[str]:
        images: list[str] = []
        for media in entry.get("media_content", []) or []:
            media_url = media.get("url")
            if media_url:
                images.append(urljoin(base_url, media_url))
        for media in entry.get("media_thumbnail", []) or []:
            media_url = media.get("url")
            if media_url:
                images.append(urljoin(base_url, media_url))
        for enclosure in entry.get("enclosures", []) or []:
            href = enclosure.get("href")
            media_type = enclosure.get("type", "")
            if href and media_type.startswith("image/"):
                images.append(urljoin(base_url, href))
        for field in ("summary", "description"):
            value = entry.get(field)
            if value:
                soup = BeautifulSoup(value, "html.parser")
                images.extend(
                    urljoin(base_url, img.get("src"))
                    for img in soup.select("img[src]")
                    if img.get("src")
                )
        return unique_http_urls(images)[:3]

    def _extract_inline_images(self, soup: BeautifulSoup, base_url: str) -> list[str]:
        images: list[str] = []
        for img in soup.select("main img, article img, img"):
            src = img.get("src") or img.get("data-src") or img.get("data-original")
            if not src:
                continue
            image_url = urljoin(base_url, src)
            if is_probable_content_image(image_url):
                images.append(image_url)
            if len(images) >= 3:
                break
        return unique_http_urls(images)

    def _cache_images(self, image_urls: list[str], source_id: str) -> list[str]:
        if not self.settings.get("cache_images", True):
            return []
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        local_paths: list[str] = []
        for image_url in image_urls[:3]:
            try:
                response = self._get(image_url)
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if not content_type.startswith("image/"):
                    continue
                suffix = extension_from_content_type(content_type, image_url)
                digest = hashlib.sha1(image_url.encode("utf-8")).hexdigest()[:16]
                file_name = f"{source_id}-{digest}{suffix}"
                path = IMAGE_DIR / file_name
                if not path.exists():
                    path.write_bytes(response.content)
                local_paths.append(f"/static-data/images/{file_name}")
            except Exception:
                continue
        return local_paths

    def _get(self, url: str) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                return self.client.get(url)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.8)
                    continue
                raise
        if last_error:
            raise last_error
        raise RuntimeError(f"Failed to fetch {url}")

    def _finalize_items(self, items: list[NewsItem], source: SourceConfig) -> list[NewsItem]:
        finalized = []
        for item in items:
            item.published_at = normalize_datetime(item.published_at)
            if source.require_published_at and not parse_datetime(item.published_at):
                continue
            apply_image_policy(item, source)
            finalized.append(item)
        return self._filter_recent(finalized)

    def _filter_recent(self, items: list[NewsItem]) -> list[NewsItem]:
        source_lookback = None
        if items:
            source_id = items[0].source_id
            source_lookback = self.settings.get("source_lookback_hours", {}).get(source_id)
        lookback_hours = source_lookback or self.settings.get("lookback_hours")
        if not lookback_hours:
            return items
        threshold = datetime.now(timezone.utc) - timedelta(hours=int(lookback_hours))
        filtered = []
        for item in items:
            parsed = parse_datetime(item.published_at)
            if parsed is not None and parsed >= threshold:
                filtered.append(item)
        return filtered

    def _dedupe(self, items: list[NewsItem]) -> list[NewsItem]:
        seen: set[str] = set()
        deduped: list[NewsItem] = []
        for item in items:
            key = normalize_url(item.url) or item.id
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    @staticmethod
    def _make_id(source_id: str, url: str, title: str) -> str:
        digest = hashlib.sha1(f"{source_id}:{url}:{title}".encode("utf-8")).hexdigest()
        return digest[:20]


def collect_once(config_path: Path = DEFAULT_CONFIG_PATH) -> list[NewsItem]:
    collector = Collector(config_path)
    try:
        return collector.collect()
    finally:
        collector.close()


def collect_once_report(config_path: Path = DEFAULT_CONFIG_PATH, collection_options: dict[str, Any] | None = None) -> dict[str, Any]:
    profile = save_collection_profile(collection_options) if collection_options else load_collection_profile()
    collector = Collector(config_path, profile)
    try:
        items = collector.collect()
        return {"items": items, "errors": collector.errors, "collection_profile": profile}
    finally:
        collector.close()


def apply_collection_profile(sources: list[SourceConfig], profile: dict[str, Any]) -> list[SourceConfig]:
    keywords = [str(keyword).strip() for keyword in profile.get("keywords", []) if str(keyword).strip()]
    package_tags = source_tags_for_packages(list(profile.get("source_packages", [])))
    search_source_ids = source_ids_for_search_engines(list(profile.get("search_engines", [])))
    dynamic_sources: list[SourceConfig] = []
    for source in sources:
        source_tags = set(source.tags)
        in_package = bool(source_tags & package_tags)
        is_search_source = "search" in source_tags
        is_selected_search = source.id in search_source_ids
        source.enabled = bool(source.enabled and (is_selected_search if is_search_source else in_package))
        if keywords:
            source.include_keywords = keywords
        if is_search_source:
            source.require_published_at = False
            if source.max_items is None:
                source.max_items = 8
        dynamic_sources.append(source)
    return dynamic_sources


def format_collect_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}: {exc.request.url}"
    if isinstance(exc, httpx.TimeoutException):
        return "请求超时，请检查网络或调大 request_timeout_seconds"
    if isinstance(exc, httpx.NetworkError):
        return "网络连接失败，请检查来源 URL 或本机网络"
    if isinstance(exc, httpx.InvalidURL):
        return "来源 URL 格式错误"
    return str(exc) or exc.__class__.__name__


def initialize_seen_from_existing_items() -> None:
    if load_seen_items():
        return
    existing = load_items()
    if existing:
        update_seen_items(existing)


def filter_seen_items(items: list[NewsItem]) -> list[NewsItem]:
    seen = load_seen_items()
    filtered = []
    skipped = 0
    for item in items:
        fingerprint = item_fingerprint(item.to_dict())
        if fingerprint in seen:
            skipped += 1
            continue
        filtered.append(item)
    if skipped:
        print(f"[collector] skipped seen items: {skipped}")
    return filtered


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return TEXT_CLEAN_RE.sub(" ", html.unescape(value)).strip()


def clean_html(value: str | None) -> str:
    if not value:
        return ""
    if "<" not in value or ">" not in value:
        return clean_text(value)
    soup = BeautifulSoup(value, "html.parser")
    return clean_text(soup.get_text(" ", strip=True))


def parse_feed_date(entry: Any) -> str | None:
    struct_time = entry.get("published_parsed") or entry.get("updated_parsed")
    if struct_time:
        return datetime(*struct_time[:6], tzinfo=timezone.utc).isoformat()
    raw = entry.get("published") or entry.get("updated")
    return normalize_datetime(raw)


def normalize_datetime(value: str | None) -> str | None:
    if not value:
        return None
    parsed = parse_datetime(value)
    return parsed.isoformat() if parsed else clean_text(value)


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    date_match = DATE_RE.search(text)
    if date_match and "T" not in text:
        try:
            parsed = datetime(
                int(date_match.group("year")),
                int(date_match.group("month")),
                int(date_match.group("day")),
                tzinfo=LOCAL_TZ,
            )
            return parsed.astimezone(timezone.utc)
        except ValueError:
            return None
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError, IndexError):
            return None


def extract_page_datetime(
    soup: BeautifulSoup,
    html_text: str,
    date_selector: str | None = None,
) -> str | None:
    selectors = [
        date_selector,
        "meta[property='article:published_time']",
        "meta[name='article:published_time']",
        "meta[name='pubdate']",
        "meta[name='publishdate']",
        "meta[name='date']",
        "meta[itemprop='datePublished']",
        "time[datetime]",
        "time",
    ]
    for selector in selectors:
        if not selector:
            continue
        value = select_attr_or_text_raw(soup, selector)
        normalized = normalize_datetime(extract_date_text(value) or value)
        if parse_datetime(normalized):
            return normalized

    for script in soup.select("script[type='application/ld+json']"):
        value = extract_json_ld_date(script.string or "")
        if value:
            return value

    return normalize_datetime(extract_date_text(soup.get_text(" ", strip=True) or html_text))


def extract_json_ld_date(raw: str) -> str | None:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None

    stack = payload if isinstance(payload, list) else [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key in ("datePublished", "dateCreated", "dateModified", "uploadDate"):
                value = current.get(key)
                normalized = normalize_datetime(str(value)) if value else None
                if parse_datetime(normalized):
                    return normalized
            for value in current.values():
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)
    return None


def extract_date_text(value: str | None) -> str | None:
    if not value:
        return None
    match = DATE_RE.search(value)
    if not match:
        return None
    return f"{match.group('year')}-{int(match.group('month')):02d}-{int(match.group('day')):02d}"


def select_text_or_meta(soup: BeautifulSoup, selector: str | None) -> str:
    if not selector:
        return ""
    node = soup.select_one(selector)
    if not node:
        return ""
    if node.name == "meta":
        return node.get("content", "")
    return node.get_text(" ", strip=True)


def select_attr_or_text(soup: BeautifulSoup, selector: str | None, base_url: str) -> str:
    if not selector:
        return ""
    node = soup.select_one(selector)
    if not node:
        return ""
    for attr in ("content", "src", "href", "datetime"):
        value = node.get(attr)
        if value:
            return urljoin(base_url, value)
    return node.get_text(" ", strip=True)


def select_attr_or_text_raw(soup: BeautifulSoup, selector: str | None) -> str:
    if not selector:
        return ""
    node = soup.select_one(selector)
    if not node:
        return ""
    for attr in ("content", "datetime", "value", "data-date", "data-time"):
        value = node.get(attr)
        if value:
            return value
    return node.get_text(" ", strip=True)


def select_attr(soup: BeautifulSoup, selector: str | None, attr: str) -> str:
    if not selector:
        return ""
    node = soup.select_one(selector)
    if not node:
        return ""
    return node.get(attr, "")


def get_by_path(payload: Any, path: str) -> Any:
    if path == "":
        return payload
    current = payload
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            current = current[int(part)]
        else:
            return None
    return current


def expand_url_template(url: str, settings: dict[str, Any], source_id: str | None = None) -> str:
    today = datetime.now(LOCAL_TZ)
    source_lookback = None
    if source_id:
        source_lookback = settings.get("source_lookback_hours", {}).get(source_id)
    lookback_hours = int(source_lookback or settings.get("lookback_hours") or 72)
    since = today - timedelta(hours=lookback_hours)
    keywords = [str(keyword).strip() for keyword in settings.get("active_keywords", []) if str(keyword).strip()]
    query = " ".join(keywords)
    return url.format(
        yyyy=today.strftime("%Y"),
        yy=today.strftime("%y"),
        mm=today.strftime("%m"),
        m=str(today.month),
        dd=today.strftime("%d"),
        d=str(today.day),
        since_yyyy=since.strftime("%Y"),
        since_mm=since.strftime("%m"),
        since_dd=since.strftime("%d"),
        since_date=since.strftime("%Y-%m-%d"),
        lookback_hours=settings.get("lookback_hours", ""),
        query=quote(query),
        query_plus=quote_plus(query),
    )


def item_matches_keywords(title: str, summary: str, source: SourceConfig) -> bool:
    if source.keyword_scope == "title":
        return text_matches_keywords(title, source)
    return text_matches_keywords(f"{title} {summary}", source)


def text_matches_keywords(text: str, source: SourceConfig) -> bool:
    lowered = text.lower()
    include = [keyword for keyword in source.include_keywords if keyword]
    exclude = [keyword for keyword in source.exclude_keywords if keyword]
    if include and not any(keyword_in_text(keyword, lowered) for keyword in include):
        return False
    if exclude and any(keyword_in_text(keyword, lowered) for keyword in exclude):
        return False
    return True


def keyword_in_text(keyword: str, lowered_text: str) -> bool:
    keyword = keyword.strip()
    if not keyword:
        return False
    lowered_keyword = keyword.lower()
    if lowered_keyword.isascii() and lowered_keyword.replace("-", "").isalnum():
        if len(lowered_keyword) <= 5:
            return re.search(
                rf"(?<![a-z0-9]){re.escape(lowered_keyword)}(?![a-z0-9])",
                lowered_text,
            ) is not None
        return lowered_keyword in lowered_text
    return lowered_keyword in lowered_text


def score_item(item: NewsItem) -> tuple[float, list[str]]:
    title_text = item.title.lower()
    summary_text = item.summary[:600].lower()
    text = title_text if "media" in item.tags else f"{title_text} {summary_text}"
    reasons: list[str] = []
    score = float(SOURCE_BASE_SCORES.get(item.source_id, 12))
    reasons.append(f"source:{SOURCE_BASE_SCORES.get(item.source_id, 12)}")

    entity_hits = sorted(entity for entity in TOP_ENTITIES if entity.lower() in text)
    if entity_hits:
        gain = min(24, 5 * len(entity_hits))
        score += gain
        reasons.append(f"top_entity:+{gain}:{','.join(entity_hits[:5])}")

    release_hits = sorted(term for term in MODEL_RELEASE_TERMS if term.lower() in text)
    if release_hits:
        score += 16
        reasons.append(f"release:+16:{','.join(release_hits[:4])}")

    high_value_hits = sorted(term for term in HIGH_VALUE_TERMS if term.lower() in text)
    if high_value_hits:
        gain = min(18, 4 * len(high_value_hits))
        score += gain
        reasons.append(f"topic:+{gain}:{','.join(high_value_hits[:5])}")

    if any(tag in item.tags for tag in ("official",)):
        score += 18
        reasons.append("official:+18")
    if any(tag in item.tags for tag in ("paper", "research")):
        score += 10
        reasons.append("research:+10")
    if any(tag in item.tags for tag in ("china",)):
        score += 4
        reasons.append("china:+4")
    if item.images or item.local_images:
        score += 3
        reasons.append("image:+3")

    age_hours = item_age_hours(item)
    if age_hours is not None:
        if item.source_id.startswith("github"):
            freshness = max(0.0, 8 - age_hours / 24)
        else:
            freshness = max(0.0, 18 - age_hours / 4)
        score += freshness
        reasons.append(f"fresh:+{freshness:.1f}")

    if item.source_id.startswith("github"):
        score *= 0.72
        reasons.append("github_cap_factor:0.72")
    is_aggregation = (
        any(term.lower() in title_text for term in AGGREGATION_TERMS)
        or item.title.count("；") >= 2
        or item.title.count(";") >= 2
    )
    if is_aggregation:
        score *= 0.72
        reasons.append("aggregation_factor:0.72")

    return round(score, 2), reasons


def apply_image_policy(item: NewsItem, source: SourceConfig) -> None:
    has_images = bool(item.images or item.local_images)
    if not has_images:
        item.image_usage = "none"
        return

    if "official" in source.tags:
        item.image_usage = "publishable_candidate"
        item.raw["image_policy"] = "official_source_image_allowed"
        return

    item.raw["preview_images"] = item.images
    item.raw["preview_local_images"] = item.local_images
    item.raw["image_policy"] = "non_official_source_preview_only"
    item.image_usage = "preview_only"
    item.images = []
    item.local_images = []


def should_fetch_article_images(source: SourceConfig, settings: dict[str, Any]) -> bool:
    if source.fetch_article_images is not None:
        return bool(source.fetch_article_images)
    return bool(settings.get("fetch_article_images", False))


def json_collect_limit(source: SourceConfig, max_items: int) -> int:
    if source.id.startswith("github"):
        return max(max_items, max_items * 5)
    return max_items


def item_age_hours(item: NewsItem) -> float | None:
    published = parse_datetime(item.published_at)
    if not published:
        return None
    return max(0.0, (datetime.now(timezone.utc) - published).total_seconds() / 3600)


def apply_source_limits(
    items: list[NewsItem],
    target_items: int,
    source_limits: dict[str, int],
    source_minimums: dict[str, int],
) -> list[NewsItem]:
    counts: dict[str, int] = {}
    selected: list[NewsItem] = []
    selected_ids: set[str] = set()

    for source_id, minimum in source_minimums.items():
        source_candidates = [item for item in items if item.source_id == source_id]
        limit = int(source_limits.get(source_id, minimum))
        for item in source_candidates[: min(int(minimum), limit)]:
            selected.append(item)
            selected_ids.add(item.id)
            counts[item.source_id] = counts.get(item.source_id, 0) + 1

    for item in items:
        if item.id in selected_ids:
            continue
        limit = source_limits.get(item.source_id)
        if limit is not None and counts.get(item.source_id, 0) >= int(limit):
            continue
        selected.append(item)
        selected_ids.add(item.id)
        counts[item.source_id] = counts.get(item.source_id, 0) + 1
        if len(selected) >= int(target_items):
            break
    return sorted(
        selected[: int(target_items)],
        key=lambda item: (item.score, item.published_at or item.collected_at),
        reverse=True,
    )


def is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def normalize_url(value: str) -> str:
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return value
    return parsed._replace(fragment="", query="").geturl().rstrip("/")


def unique_http_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for url in urls:
        if not is_http_url(url):
            continue
        key = normalize_url(url)
        if key in seen:
            continue
        seen.add(key)
        result.append(url)
    return result


def is_probable_content_image(url: str) -> bool:
    lower = url.lower()
    if any(marker in lower for marker in ("logo", "avatar", "icon", "sprite", "tracking")):
        return False
    return any(
        lower.split("?")[0].endswith(ext)
        for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif")
    )


def extension_from_content_type(content_type: str, url: str) -> str:
    if "png" in content_type:
        return ".png"
    if "webp" in content_type:
        return ".webp"
    if "gif" in content_type:
        return ".gif"
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return suffix
    return ".jpg"


if __name__ == "__main__":
    collected = collect_once()
    print(json.dumps([item.to_dict() for item in collected], ensure_ascii=False, indent=2))
