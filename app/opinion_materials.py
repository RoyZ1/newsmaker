from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from app.config import ROOT_DIR, load_config
from app.douyin_auth import load_douyin_token, token_expired
from app.env_loader import load_dotenv


OPINION_ITEMS_PATH = ROOT_DIR / "data" / "opinion_items.json"
OPINION_CARD_DIR = ROOT_DIR / "data" / "opinion_cards"
OPINION_CARD_URL_PREFIX = "/static-data/opinion-cards"
IMPORTED_OPINION_DIR = ROOT_DIR / "data" / "opinion_imports"
IMPORTED_OPINION_URL_PREFIX = "/static-data/opinion-imports"
MAX_UPLOAD_BYTES = 12 * 1024 * 1024
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
DOUYIN_OPEN_BASE_URL = "https://open.douyin.com"
DOUYIN_COMMENT_LIST_PATH = "/item/comment/list/"
DOUYIN_KEYWORD_VIDEO_SEARCH_PATH = "/video/search/"
DOUYIN_KEYWORD_COMMENT_LIST_PATH = "/video/search/comment/list/"


class OpinionMaterialError(RuntimeError):
    pass


def import_opinion_texts(platform: str, topic: str, texts: list[str], source_url: str = "") -> dict[str, Any]:
    cleaned = [sanitize_comment_text(text) for text in texts if sanitize_comment_text(text)]
    if not cleaned:
        raise OpinionMaterialError("没有可导入的评论文本。")
    items = load_opinion_items()
    created: list[dict[str, Any]] = []
    for text in cleaned[:20]:
        item = build_opinion_item(platform=platform, topic=topic, text=text, source_url=source_url, source_type="manual_text")
        item["card"] = generate_comment_card(item)
        upsert_opinion_item(items, item)
        created.append(item)
    save_opinion_items(items)
    return {"count": len(created), "items": created}


def import_opinion_screenshot(
    platform: str,
    topic: str,
    stream: Any,
    filename: str,
    note: str = "",
    draft_ref: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw = stream.read()
    if not raw:
        raise OpinionMaterialError("上传的评论截图为空。")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise OpinionMaterialError("评论截图太大，请上传 12MB 以内的图片。")
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_IMAGE_EXTENSIONS:
        raise OpinionMaterialError("仅支持 jpg、jpeg、png、webp 评论截图。")

    digest = hashlib.sha1(raw).hexdigest()[:12]
    output_path = IMPORTED_OPINION_DIR / f"opinion-shot-{digest}.png"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(".upload")
    temp_path.write_bytes(raw)
    try:
        with Image.open(temp_path) as image:
            image = ImageOps.exif_transpose(image)
            image.save(output_path, format="PNG", optimize=True)
    except (OSError, UnidentifiedImageError) as exc:
        raise OpinionMaterialError("无法读取这张评论截图，请确认图片没有损坏。") from exc
    finally:
        temp_path.unlink(missing_ok=True)

    item = build_opinion_item(
        platform=platform,
        topic=topic,
        text=clean_screenshot_note(note) or "手动导入评论截图，已提前处理个人信息。",
        source_url="",
        source_type="manual_screenshot",
    )
    item["privacy"] = {
        "anonymized": False,
        "note": "截图由用户在上传前自行处理，系统不再改图或二次匿名化。",
    }
    normalized_draft_ref = normalize_draft_ref(draft_ref)
    if normalized_draft_ref:
        item["draft_ref"] = normalized_draft_ref
    item["screenshot"] = {
        "local_path": str(output_path),
        "url": f"{IMPORTED_OPINION_URL_PREFIX}/{output_path.name}",
        "privacy_note": "系统未修改截图内容；默认认为上传前已处理个人信息，发布前仍建议人工复核。",
    }
    items = load_opinion_items()
    upsert_opinion_item(items, item)
    save_opinion_items(items)
    return {"item": item}


def normalize_draft_ref(draft_ref: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(draft_ref, dict):
        return {}
    result: dict[str, Any] = {}
    draft_index = safe_int_or_none(draft_ref.get("draft_index"))
    if draft_index is not None:
        result["draft_index"] = draft_index
    for key in ("draft_id", "topic_id", "draft_title"):
        value = str(draft_ref.get(key) or "").strip()
        if value:
            result[key] = value[:240]
    return result


def collect_opinions_auto(platform: str, query: str, limit: int = 10) -> dict[str, Any]:
    platform_key = platform.lower()
    if platform_key not in {"douyin", "weibo"}:
        raise OpinionMaterialError("当前自动采集仅预留 douyin/weibo 两类平台配置。")
    config = opinion_platform_config(platform)
    mode = str(config.get("mode") or "proxy").strip().lower()
    if not config.get("enabled") and not (platform_key == "douyin" and mode in {"official", "official_video", "openapi", "openapi_video"}):
        raise OpinionMaterialError(f"{platform} 自动采集未启用。请先在 .env 或 config/sources.yml 配置授权接口。")
    if platform_key == "douyin" and mode in {"official", "official_video", "openapi", "openapi_video"}:
        return collect_douyin_video_comments(query, limit=limit, config=config)
    if platform_key == "douyin" and mode in {"official_keyword", "openapi_keyword", "keyword"}:
        return collect_douyin_keyword_comments(query, limit=limit, config=config)
    return collect_proxy_opinions(platform, query, limit=limit, config=config)


def collect_proxy_opinions(platform: str, query: str, limit: int, config: dict[str, Any]) -> dict[str, Any]:
    endpoint = str(config.get("endpoint") or "").strip()
    token = str(config.get("access_token") or "").strip()
    if not endpoint or not token:
        raise OpinionMaterialError(f"{platform} 自动采集缺少 endpoint 或 access_token。")

    params = {"query": query, "limit": max(1, min(int(limit or 10), 30))}
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = httpx.get(endpoint, params=params, headers=headers, timeout=20)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise OpinionMaterialError(f"{platform} 自动采集请求失败：{exc}") from exc

    payload = response.json()
    comments = normalize_opinion_payload(payload)
    if not comments:
        raise OpinionMaterialError("授权接口没有返回可用评论。")

    items = load_opinion_items()
    created: list[dict[str, Any]] = []
    for comment in comments[: params["limit"]]:
        text = sanitize_comment_text(str(comment.get("text") or ""))
        if not text:
            continue
        item = build_opinion_item(
            platform=platform,
            topic=query,
            text=text,
            source_url=str(comment.get("source_url") or ""),
            source_type="authorized_api",
            published_at=str(comment.get("published_at") or ""),
            like_count=safe_int(comment.get("like_count")),
        )
        item["card"] = generate_comment_card(item)
        upsert_opinion_item(items, item)
        created.append(item)
    save_opinion_items(items)
    return {"count": len(created), "items": created, "mode": "proxy"}


def collect_douyin_video_comments(query: str, limit: int, config: dict[str, Any]) -> dict[str, Any]:
    access_token = str(config.get("open_access_token") or config.get("access_token") or "").strip()
    open_id = str(config.get("open_id") or "").strip()
    if not access_token or not open_id:
        raise OpinionMaterialError("抖音官方视频评论采集缺少 DOUYIN_OPEN_ACCESS_TOKEN 或 DOUYIN_OPEN_ID。")

    item_ids = parse_douyin_item_ids(query)
    if not item_ids:
        item_ids = resolve_douyin_share_item_ids(query)
    if not item_ids:
        raise OpinionMaterialError("抖音官方视频评论模式需要输入视频 item_id，或包含 /video/数字ID 的抖音链接；短链接无法解析时请改填 item_id。")

    comments: list[dict[str, Any]] = []
    max_count = max(1, min(int(limit or 10), 50))
    for item_id in item_ids:
        remaining = max_count - len(comments)
        if remaining <= 0:
            break
        comments.extend(fetch_douyin_item_comments(item_id, open_id, access_token, remaining, config))

    if not comments:
        raise OpinionMaterialError("抖音官方接口没有返回可用评论。请确认视频公开、item.comment 权限已开通。")
    return persist_opinion_comments("douyin", query, comments[:max_count], "douyin_openapi_item_comment", mode="official_video")


def collect_douyin_keyword_comments(query: str, limit: int, config: dict[str, Any]) -> dict[str, Any]:
    client_token = str(config.get("client_token") or "").strip()
    search_token = str(config.get("keyword_search_token") or config.get("open_access_token") or config.get("access_token") or client_token).strip()
    if not search_token or not client_token:
        raise OpinionMaterialError("抖音关键词评论采集缺少搜索 token 或 DOUYIN_CLIENT_TOKEN。该模式需要开通搜索能力权限。")
    videos = fetch_douyin_keyword_videos(query, search_token, max(1, min(int(limit or 10), 10)), config)
    if not videos:
        raise OpinionMaterialError("抖音关键词视频搜索没有返回可用视频。请确认搜索能力权限已开通。")

    comments: list[dict[str, Any]] = []
    max_count = max(1, min(int(limit or 10), 50))
    for video in videos:
        sec_item_id = str(video.get("sec_item_id") or video.get("item_id") or "").strip()
        if not sec_item_id:
            continue
        remaining = max_count - len(comments)
        if remaining <= 0:
            break
        comments.extend(fetch_douyin_keyword_video_comments(sec_item_id, client_token, remaining, config, video))

    if not comments:
        raise OpinionMaterialError("抖音关键词评论接口没有返回可用评论。")
    return persist_opinion_comments("douyin", query, comments[:max_count], "douyin_openapi_keyword_comment", mode="official_keyword")


def opinion_platform_config(platform: str) -> dict[str, Any]:
    load_dotenv()
    key = platform.upper()
    configured = {}
    try:
        configured = (load_config().get("opinion_sources") or {}).get(platform.lower(), {}) or {}
    except Exception:  # noqa: BLE001
        configured = {}
    token_config = douyin_token_config() if platform.lower() == "douyin" else {}
    return {
        "enabled": str(os.getenv(f"{key}_OPINION_ENABLED", configured.get("enabled", ""))).lower() in {"1", "true", "yes", "on"},
        "mode": os.getenv(f"{key}_OPINION_MODE", str(configured.get("mode") or "proxy")),
        "endpoint": os.getenv(f"{key}_OPINION_ENDPOINT", str(configured.get("endpoint") or "")),
        "access_token": os.getenv(f"{key}_OPINION_ACCESS_TOKEN", str(configured.get("access_token") or "")),
        "open_base_url": os.getenv(f"{key}_OPEN_BASE_URL", str(configured.get("open_base_url") or DOUYIN_OPEN_BASE_URL)),
        "open_access_token": os.getenv(f"{key}_OPEN_ACCESS_TOKEN", str(configured.get("open_access_token") or "")),
        "open_id": os.getenv(f"{key}_OPEN_ID", str(configured.get("open_id") or "")),
        "client_token": os.getenv(f"{key}_CLIENT_TOKEN", str(configured.get("client_token") or "")),
        "keyword_search_token": os.getenv(f"{key}_KEYWORD_SEARCH_TOKEN", str(configured.get("keyword_search_token") or "")),
        "comment_list_url": os.getenv(f"{key}_COMMENT_LIST_URL", str(configured.get("comment_list_url") or "")),
        "keyword_video_search_url": os.getenv(f"{key}_KEYWORD_VIDEO_SEARCH_URL", str(configured.get("keyword_video_search_url") or "")),
        "keyword_comment_list_url": os.getenv(f"{key}_KEYWORD_COMMENT_LIST_URL", str(configured.get("keyword_comment_list_url") or "")),
    } | token_config


def douyin_token_config() -> dict[str, Any]:
    token = load_douyin_token()
    if not token.get("access_token") or not token.get("open_id"):
        return {}
    expired = token_expired(token.get("expires_at"))
    return {
        "enabled": bool(token.get("enabled", True)) and not expired,
        "mode": token.get("mode") or "official_video",
        "open_access_token": "" if expired else token.get("access_token") or "",
        "open_id": token.get("open_id") or "",
        "token_expired": expired,
    }


def opinion_config_status(platform: str = "douyin") -> dict[str, Any]:
    platform_key = platform.lower().strip() or "douyin"
    if platform_key not in {"douyin", "weibo"}:
        raise OpinionMaterialError("当前只支持检测 douyin/weibo 舆论配置。")
    config = opinion_platform_config(platform_key)
    mode = str(config.get("mode") or "proxy").strip().lower()
    required_by_mode = {
        "proxy": ("endpoint", "access_token"),
        "official_video": ("open_access_token", "open_id", "comment_list_url"),
        "official": ("open_access_token", "open_id", "comment_list_url"),
        "openapi": ("open_access_token", "open_id", "comment_list_url"),
        "official_keyword": ("client_token", "keyword_video_search_url", "keyword_comment_list_url"),
        "keyword": ("client_token", "keyword_video_search_url", "keyword_comment_list_url"),
    }
    required = required_by_mode.get(mode, required_by_mode["proxy"])
    fields = {
        "enabled": bool(config.get("enabled")),
        "mode": mode,
        "required": {key: bool(str(config.get(key) or "").strip()) for key in required},
        "optional": {
            "open_base_url": bool(str(config.get("open_base_url") or "").strip()),
            "keyword_search_token": bool(str(config.get("keyword_search_token") or "").strip()),
            "open_access_token": bool(str(config.get("open_access_token") or "").strip()),
            "open_id": bool(str(config.get("open_id") or "").strip()),
            "token_expired": bool(config.get("token_expired")),
        },
    }
    oauth_token_expected = platform_key == "douyin" and mode in {"official_video", "official", "openapi"}
    missing = []
    if not fields["enabled"] and not oauth_token_expected:
        missing.append("enabled")
    missing.extend(key for key, ready in fields["required"].items() if not ready)
    if config.get("token_expired"):
        missing.append("token_expired")
    return {
        "platform": platform_key,
        "enabled": fields["enabled"],
        "mode": mode,
        "ready": not missing,
        "missing": missing,
        "fields": fields,
        "hint": opinion_config_hint(platform_key, mode, missing),
    }


def opinion_config_hint(platform: str, mode: str, missing: list[str]) -> str:
    if not missing:
        return f"{platform} {mode} 配置已具备，可以尝试采集。"
    if platform == "douyin" and mode in {"official_video", "official", "openapi"}:
        if "open_access_token" in missing or "open_id" in missing:
            return "douyin official_video 还没有可用授权。请先在抖音授权区生成授权链接，并保存回调 code；也可以手动填写 DOUYIN_OPEN_ACCESS_TOKEN / DOUYIN_OPEN_ID。"
    labels = {
        "enabled": f"{platform.upper()}_OPINION_ENABLED=true",
        "endpoint": f"{platform.upper()}_OPINION_ENDPOINT",
        "access_token": f"{platform.upper()}_OPINION_ACCESS_TOKEN",
        "open_access_token": f"{platform.upper()}_OPEN_ACCESS_TOKEN",
        "open_id": f"{platform.upper()}_OPEN_ID",
        "comment_list_url": f"{platform.upper()}_COMMENT_LIST_URL",
        "client_token": f"{platform.upper()}_CLIENT_TOKEN",
        "keyword_video_search_url": f"{platform.upper()}_KEYWORD_VIDEO_SEARCH_URL",
        "keyword_comment_list_url": f"{platform.upper()}_KEYWORD_COMMENT_LIST_URL",
        "token_expired": "本地抖音 access_token 已过期，请点击刷新Token",
    }
    readable = "、".join(labels.get(item, item) for item in missing)
    return f"{platform} {mode} 还缺少：{readable}。"


def parse_douyin_item_ids(text: str) -> list[str]:
    ids: list[str] = []
    for part in re.split(r"[\s,，;；\n]+", text or ""):
        value = part.strip()
        if not value:
            continue
        match = re.search(r"(?:/video/|modal_id=|item_id=|aweme_id=)(\d{8,})", value)
        item_id = match.group(1) if match else value
        if re.fullmatch(r"\d{8,}", item_id) and item_id not in ids:
            ids.append(item_id)
    return ids


def resolve_douyin_share_item_ids(text: str) -> list[str]:
    urls = re.findall(r"https?://[^\s，,;；]+", text or "")
    ids: list[str] = []
    for url in urls[:3]:
        if "douyin.com" not in url.lower():
            continue
        try:
            response = httpx.get(url, follow_redirects=True, timeout=10)
        except httpx.HTTPError:
            continue
        for candidate in (str(response.url), response.text[:2000]):
            for item_id in parse_douyin_item_ids(candidate):
                if item_id not in ids:
                    ids.append(item_id)
    return ids


def douyin_open_url(config: dict[str, Any], key: str, default_path: str) -> str:
    configured = str(config.get(key) or "").strip()
    if configured:
        return configured
    base_url = str(config.get("open_base_url") or DOUYIN_OPEN_BASE_URL).rstrip("/")
    return f"{base_url}{default_path}"


def fetch_douyin_item_comments(
    item_id: str,
    open_id: str,
    access_token: str,
    limit: int,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    endpoint = douyin_open_url(config, "comment_list_url", DOUYIN_COMMENT_LIST_PATH)
    comments: list[dict[str, Any]] = []
    cursor = 0
    has_more = True
    loops = 0
    while has_more and len(comments) < limit and loops < 6:
        loops += 1
        count = max(1, min(50, limit - len(comments)))
        payload = {"open_id": open_id, "item_id": item_id, "cursor": cursor, "count": count}
        response_payload = request_douyin_openapi(endpoint, access_token, payload, token_header="access-token")
        ensure_douyin_success(response_payload, "抖音视频评论列表")
        batch = extract_comment_list(response_payload)
        comments.extend(attach_douyin_source(comment, item_id=item_id) for comment in batch)
        cursor = safe_int(extract_nested_value(response_payload, ("cursor", "next_cursor")))
        has_more = safe_bool(extract_nested_value(response_payload, ("has_more", "hasMore")))
        if not batch:
            break
    return comments


def fetch_douyin_keyword_videos(query: str, search_token: str, limit: int, config: dict[str, Any]) -> list[dict[str, Any]]:
    endpoint = douyin_open_url(config, "keyword_video_search_url", DOUYIN_KEYWORD_VIDEO_SEARCH_PATH)
    payload = {"keyword": query, "count": max(1, min(limit, 20)), "cursor": 0}
    open_id = str(config.get("open_id") or "").strip()
    if open_id:
        payload["open_id"] = open_id
    response_payload = request_douyin_openapi(endpoint, search_token, payload, token_header="access-token")
    ensure_douyin_success(response_payload, "抖音关键词视频搜索")
    candidates = extract_list_by_keys(response_payload, ("videos", "video_list", "list", "items"))
    return [item for item in candidates if isinstance(item, dict)]


def fetch_douyin_keyword_video_comments(
    sec_item_id: str,
    client_token: str,
    limit: int,
    config: dict[str, Any],
    video: dict[str, Any],
) -> list[dict[str, Any]]:
    endpoint = douyin_open_url(config, "keyword_comment_list_url", DOUYIN_KEYWORD_COMMENT_LIST_PATH)
    payload = {"sec_item_id": sec_item_id, "count": max(1, min(limit, 50)), "cursor": 0}
    response_payload = request_douyin_openapi(endpoint, client_token, payload, token_header="access-token")
    ensure_douyin_success(response_payload, "抖音关键词视频评论")
    comments = extract_comment_list(response_payload)
    source_url = str(video.get("share_url") or video.get("source_url") or "")
    return [attach_douyin_source(comment, item_id=sec_item_id, source_url=source_url) for comment in comments]


def request_douyin_openapi(endpoint: str, token: str, payload: dict[str, Any], token_header: str) -> dict[str, Any]:
    params = dict(payload)
    params.setdefault("access_token", token)
    headers = {
        token_header: token,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    try:
        response = httpx.get(endpoint, params=params, headers=headers, timeout=20)
        if response.status_code in {404, 405}:
            post_payload = dict(payload)
            post_payload.setdefault("access_token", token)
            response = httpx.post(endpoint, json=post_payload, headers=headers, timeout=20)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:400] if exc.response is not None else ""
        raise OpinionMaterialError(f"抖音 OpenAPI 请求失败 HTTP {exc.response.status_code}：{body}") from exc
    except httpx.HTTPError as exc:
        raise OpinionMaterialError(f"抖音 OpenAPI 网络请求失败：{exc}") from exc

    try:
        data = response.json()
    except json.JSONDecodeError as exc:
        raise OpinionMaterialError("抖音 OpenAPI 返回的不是 JSON，请检查接口地址是否正确。") from exc
    if not isinstance(data, dict):
        raise OpinionMaterialError("抖音 OpenAPI 返回结构异常。")
    return data


def ensure_douyin_success(payload: dict[str, Any], action: str) -> None:
    error_code = extract_nested_value(payload, ("error_code", "err_no", "code"))
    if error_code in (None, "", 0, "0"):
        return
    description = extract_nested_value(payload, ("description", "message", "err_msg", "msg")) or "未知错误"
    raise OpinionMaterialError(f"{action}接口错误 {error_code}：{description}")


def extract_comment_list(payload: Any) -> list[dict[str, Any]]:
    candidates = extract_list_by_keys(payload, ("comments", "comment_list", "list", "items", "data"))
    return [normalize_comment_item(item) for item in candidates if isinstance(item, dict)]


def extract_list_by_keys(payload: Any, keys: tuple[str, ...]) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    for value in payload.values():
        nested = extract_list_by_keys(value, keys)
        if nested:
            return nested
    return []


def extract_nested_value(payload: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(payload, dict):
        for key in keys:
            if key in payload:
                return payload.get(key)
        for value in payload.values():
            found = extract_nested_value(value, keys)
            if found not in (None, ""):
                return found
    return None


def attach_douyin_source(comment: dict[str, Any], item_id: str, source_url: str = "") -> dict[str, Any]:
    enriched = dict(comment)
    enriched["source_url"] = source_url or comment.get("source_url") or f"https://www.douyin.com/video/{quote(item_id)}"
    enriched["published_at"] = normalize_comment_time(comment.get("published_at"))
    return enriched


def normalize_comment_time(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        timestamp = float(value)
    elif isinstance(value, str) and re.fullmatch(r"\d{10,13}", value.strip()):
        timestamp = float(value.strip())
    else:
        return str(value)
    if timestamp > 10_000_000_000:
        timestamp = timestamp / 1000
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    except (OSError, ValueError):
        return str(value)


def safe_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def safe_int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def safe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def persist_opinion_comments(
    platform: str,
    topic: str,
    comments: list[dict[str, Any]],
    source_type: str,
    mode: str,
) -> dict[str, Any]:
    items = load_opinion_items()
    created: list[dict[str, Any]] = []
    for comment in comments:
        text = sanitize_comment_text(str(comment.get("text") or ""))
        if not text:
            continue
        item = build_opinion_item(
            platform=platform,
            topic=topic,
            text=text,
            source_url=str(comment.get("source_url") or ""),
            source_type=source_type,
            published_at=str(comment.get("published_at") or ""),
            like_count=safe_int(comment.get("like_count")),
        )
        item["card"] = generate_comment_card(item)
        upsert_opinion_item(items, item)
        created.append(item)
    save_opinion_items(items)
    return {"count": len(created), "items": created, "mode": mode}


def normalize_opinion_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("comments", "data", "items", "list"):
            value = payload.get(key)
            if isinstance(value, list):
                return [normalize_comment_item(item) for item in value if isinstance(item, dict)]
    if isinstance(payload, list):
        return [normalize_comment_item(item) for item in payload if isinstance(item, dict)]
    return []


def normalize_comment_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "text": item.get("text") or item.get("content") or item.get("comment") or item.get("comment_text") or "",
        "source_url": item.get("source_url") or item.get("url") or item.get("share_url") or "",
        "published_at": item.get("published_at") or item.get("create_time") or item.get("create_time_stamp") or "",
        "like_count": item.get("like_count") or item.get("digg_count") or item.get("likes") or item.get("like_num") or 0,
    }


def build_opinion_item(
    platform: str,
    topic: str,
    text: str,
    source_url: str,
    source_type: str,
    published_at: str = "",
    like_count: int = 0,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    raw_id = f"{platform}:{topic}:{text}:{source_url}"
    return {
        "id": hashlib.sha1(raw_id.encode("utf-8", errors="ignore")).hexdigest()[:16],
        "platform": platform.strip() or "unknown",
        "topic": topic.strip(),
        "text": text,
        "source_url": source_url,
        "source_type": source_type,
        "published_at": published_at,
        "like_count": like_count,
        "collected_at": now,
        "privacy": {
            "anonymized": True,
            "note": "仅保留评论文本和平台来源，不保存头像、昵称、用户 ID。",
        },
    }


def generate_comment_card(item: dict[str, Any]) -> dict[str, str]:
    OPINION_CARD_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OPINION_CARD_DIR / f"opinion-{item['id']}.png"
    width, height = 1080, 680
    image = Image.new("RGB", (width, height), "#f6f8fb")
    draw = ImageDraw.Draw(image)
    title_font = load_font(42)
    body_font = load_font(44)
    meta_font = load_font(26)

    draw.rounded_rectangle((54, 52, width - 54, height - 52), radius=28, fill="#ffffff", outline="#d8e0e6", width=2)
    draw.text((92, 92), f"{platform_label(str(item.get('platform') or ''))} 匿名评论", fill="#0f766e", font=title_font)
    draw.text((92, 152), "已匿名处理，仅作舆论观察素材", fill="#8a94a6", font=meta_font)

    wrapped = wrap_text(str(item.get("text") or ""), 22, max_lines=6)
    y = 230
    for line in wrapped:
        draw.text((92, y), line, fill="#182129", font=body_font)
        y += 62

    topic = str(item.get("topic") or "").strip()
    footer = f"话题：{topic[:34]}" if topic else "话题：未标注"
    draw.text((92, height - 112), footer, fill="#63707b", font=meta_font)
    image.save(output_path, format="PNG", optimize=True)
    return {"local_path": str(output_path), "url": f"{OPINION_CARD_URL_PREFIX}/{output_path.name}"}


def sanitize_comment_text(text: str) -> str:
    cleaned = re.sub(r"@\S+", "@匿名用户", text or "")
    cleaned = re.sub(r"\b1[3-9]\d{9}\b", "[手机号已隐藏]", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:240]


def clean_screenshot_note(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    return cleaned[:240]


def wrap_text(text: str, max_chars: int, max_lines: int) -> list[str]:
    chars = list(text)
    lines = ["".join(chars[index : index + max_chars]) for index in range(0, len(chars), max_chars)]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip("，。,. ") + "..."
    return lines or ["（空评论）"]


def platform_label(platform: str) -> str:
    labels = {"douyin": "抖音", "weibo": "微博", "manual": "手动"}
    return labels.get(platform.lower(), platform or "平台")


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def load_opinion_items(path: Path = OPINION_ITEMS_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def save_opinion_items(items: list[dict[str, Any]], path: Path = OPINION_ITEMS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(items, handle, ensure_ascii=False, indent=2)


def upsert_opinion_item(items: list[dict[str, Any]], item: dict[str, Any]) -> None:
    for index, current in enumerate(items):
        if current.get("id") == item.get("id"):
            items[index] = {**current, **item}
            return
    items.insert(0, item)
