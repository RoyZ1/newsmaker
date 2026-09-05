from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.config import ROOT_DIR, load_config
from app.storage import load_items


OFFICIAL_SCREENSHOT_DIR = ROOT_DIR / "data" / "official_screenshots"
OFFICIAL_SCREENSHOT_URL_PREFIX = "/static-data/official-screenshots"

DEFAULT_OFFICIAL_DOMAINS = {
    "openai.com",
    "anthropic.com",
    "deepmind.google",
    "blog.google",
    "googleblog.com",
    "ai.google.dev",
    "nvidia.com",
    "blogs.nvidia.com",
    "huggingface.co",
    "deepseek.com",
    "zhipuai.cn",
    "bigmodel.cn",
    "moonshot.cn",
    "kimi.ai",
    "qwen.ai",
    "qwenlm.github.io",
    "aliyun.com",
    "alibabacloud.com",
    "tongyi.aliyun.com",
    "happyoyster.cn",
    "tencent.com",
    "cloud.tencent.com",
    "volcengine.com",
    "doubao.com",
    "bytedance.com",
    "baidu.com",
    "wenxin.baidu.com",
    "nio.cn",
    "nio.com",
    "microsoft.com",
    "amazon.com",
    "aws.amazon.com",
    "github.blog",
    "vercel.com",
    "pinecone.io",
}

DENIED_MEDIA_DOMAINS = {
    "ithome.com",
    "36kr.com",
    "leiphone.com",
    "infoq.cn",
    "qbitai.com",
    "cnblogs.com",
    "techcrunch.com",
    "theverge.com",
    "wired.com",
    "bloomberg.com",
    "reuters.com",
}

URL_RE = re.compile(r"https?://[^\s<>'\"，。；;）)]+", re.IGNORECASE)
OFFICIAL_HINT_RE = re.compile(
    r"(?:官网|官方网站|官方地址|official\s+(?:site|website))[^。；;\n]{0,40}?(https?://[^\s<>'\"，。；;）)]+)",
    re.IGNORECASE,
)
ASCII_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9.+_-]{2,}")
CHINESE_RE = re.compile(r"[\u4e00-\u9fff]{2,}")


class OfficialScreenshotError(RuntimeError):
    pass


def capture_official_screenshots_for_draft_slot(
    draft: dict[str, Any],
    draft_index: int,
    slot: dict[str, Any],
    count: int = 3,
) -> list[dict[str, Any]]:
    source = select_official_source_for_slot(draft, slot)
    slot_id = str(slot.get("slot_id") or "slot")
    output_paths = screenshot_paths(draft_index, slot_id, str(source["url"]), count=count)
    captured_paths = capture_page_screenshots(str(source["url"]), output_paths)

    created_at = datetime.now(timezone.utc).isoformat()
    records: list[dict[str, Any]] = []
    for index, output_path in enumerate(captured_paths, start=1):
        records.append(
            {
                "type": "official_screenshot",
                "prompt": "",
                "visual_angle": f"官网截图：{source.get('title') or source.get('domain')}（候选 {index}）",
                "entities": source.get("entities", []),
                "safety_notes": [
                    "来自官方页面截图，建议发布前人工确认页面内容、商标使用和截图范围。",
                ],
                "local_path": str(output_path),
                "url": f"{OFFICIAL_SCREENSHOT_URL_PREFIX}/{output_path.name}",
                "generated_at": created_at,
                "slot_id": slot.get("slot_id", ""),
                "slot_label": slot.get("label", ""),
                "source_url": source.get("url", ""),
                "source_name": source.get("source_name", "官方页面"),
                "source_title": source.get("title", ""),
                "source_domain": source.get("domain", ""),
                "manual_selected": index == 1,
            }
        )
    return records


def capture_official_screenshot_for_draft_slot(draft: dict[str, Any], draft_index: int, slot: dict[str, Any]) -> dict[str, Any]:
    records = capture_official_screenshots_for_draft_slot(draft, draft_index, slot, count=1)
    return records[0]


def select_official_source_for_slot(draft: dict[str, Any], slot: dict[str, Any]) -> dict[str, Any]:
    target_text = relevant_text_for_slot(draft, slot)
    candidates = source_candidates(draft)
    scored: list[tuple[float, dict[str, Any]]] = []
    for source in candidates:
        if not is_eligible_official_source(source):
            continue
        base_score = source_relevance_score(source, target_text)
        if base_score < 2:
            continue
        score = base_score
        if source.get("official_hint"):
            score += 8
        if source.get("from_source_link"):
            score += 2
        scored.append((score, source))

    if not scored:
        raise OfficialScreenshotError(
            "没有找到可截图的官方来源。当前只允许官方域名或素材中明确标注的官网链接，媒体/自媒体页面不会自动截图。"
        )

    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best = scored[0]
    if best_score < 4:
        raise OfficialScreenshotError(
            "找到了官方页面，但和当前小标题关联度太低，已停止自动截图。可以在素材源里加入更准确的官网链接，或手动导入图片。"
        )
    return best


def source_candidates(draft: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    for link in draft.get("source_links", []) or []:
        if not isinstance(link, dict):
            continue
        add_source_candidate(
            candidates,
            seen,
            {
                "title": str(link.get("title") or ""),
                "url": clean_url(str(link.get("url") or "")),
                "source_name": str(link.get("source_name") or "官方页面"),
                "summary": "",
                "from_source_link": True,
            },
        )

    text_for_item_match = normalize_text(
        " ".join(
            [
                str(draft.get("title") or ""),
                str(draft.get("subtitle") or ""),
                str(draft.get("body_markdown") or ""),
            ]
        )
    )
    for item in load_items():
        if not isinstance(item, dict):
            continue
        item_text = normalize_text(f"{item.get('title', '')} {item.get('summary', '')}")
        related = str(item.get("url") or "") in {str(link.get("url") or "") for link in draft.get("source_links", []) if isinstance(link, dict)}
        if not related and item_overlap_score(item_text, text_for_item_match) < 10:
            continue
        for hinted_url in official_hint_urls(str(item.get("summary") or "")):
            add_source_candidate(
                candidates,
                seen,
                {
                    "title": str(item.get("title") or ""),
                    "url": hinted_url,
                    "source_name": "官网链接",
                    "summary": str(item.get("summary") or ""),
                    "official_hint": True,
                },
            )

    add_entity_fallback_sources(candidates, seen, text_for_item_match)
    for candidate in candidates:
        candidate["domain"] = domain_of(str(candidate.get("url") or ""))
    return candidates


def add_entity_fallback_sources(candidates: list[dict[str, Any]], seen: set[str], text: str) -> None:
    fallbacks = [
        ("HappyOyster", "https://www.happyoyster.cn", "HappyOyster 官网"),
        ("快乐生蚝", "https://www.happyoyster.cn", "HappyOyster 官网"),
        ("OpenAI", "https://openai.com/news/", "OpenAI News"),
        ("Anthropic", "https://www.anthropic.com/news", "Anthropic News"),
        ("DeepSeek", "https://www.deepseek.com", "DeepSeek 官网"),
        ("智谱", "https://www.zhipuai.cn", "智谱 AI 官网"),
        ("Kimi", "https://kimi.moonshot.cn", "Kimi 官网"),
        ("通义", "https://qwen.ai", "通义千问官网"),
        ("Qwen", "https://qwen.ai", "Qwen 官网"),
        ("豆包", "https://www.doubao.com", "豆包官网"),
        ("蔚来", "https://www.nio.cn", "蔚来官网"),
        ("Vercel", "https://vercel.com/blog", "Vercel Blog"),
        ("Pinecone", "https://www.pinecone.io/blog/", "Pinecone Blog"),
        ("NVIDIA", "https://blogs.nvidia.com", "NVIDIA Blog"),
    ]
    for keyword, url, title in fallbacks:
        if keyword.lower() not in text.lower():
            continue
        add_source_candidate(
            candidates,
            seen,
                {
                    "title": title,
                    "url": url,
                    "source_name": "官方页面",
                    "summary": "",
                    "official_hint": True,
                    "entities": [keyword],
                },
            )


def add_source_candidate(candidates: list[dict[str, Any]], seen: set[str], source: dict[str, Any]) -> None:
    url = clean_url(str(source.get("url") or ""))
    if not url or not urlparse(url).scheme.startswith("http"):
        return
    key = normalize_url_key(url)
    if key in seen:
        return
    source["url"] = url
    seen.add(key)
    candidates.append(source)


def is_eligible_official_source(source: dict[str, Any]) -> bool:
    domain = domain_of(str(source.get("url") or ""))
    if not domain or is_denied_domain(domain):
        return False
    if domain_in_allowed_list(domain):
        return True
    return bool(source.get("official_hint"))


def domain_in_allowed_list(domain: str) -> bool:
    configured = set()
    try:
        configured = {str(item).lower().strip() for item in load_config().get("app", {}).get("official_screenshot_domains", [])}
    except Exception:  # noqa: BLE001
        configured = set()
    allowed = {item for item in [*DEFAULT_OFFICIAL_DOMAINS, *configured] if item}
    return any(domain == allowed_domain or domain.endswith(f".{allowed_domain}") for allowed_domain in allowed)


def is_denied_domain(domain: str) -> bool:
    return any(domain == denied or domain.endswith(f".{denied}") for denied in DENIED_MEDIA_DOMAINS)


def official_hint_urls(text: str) -> list[str]:
    urls = [clean_url(match.group(1)) for match in OFFICIAL_HINT_RE.finditer(text or "")]
    if urls:
        return urls
    return []


def source_relevance_score(source: dict[str, Any], target_text: str) -> float:
    title = str(source.get("title") or "")
    summary = str(source.get("summary") or "")
    domain = str(source.get("domain") or domain_of(str(source.get("url") or "")))
    haystack = normalize_text(target_text)
    score = 0.0
    for token in ascii_tokens(f"{title} {domain}"):
        if token.lower() in haystack.lower():
            score += max(2, min(len(token), 10))
    for phrase in chinese_phrases(title):
        if phrase in haystack:
            score += max(2, min(len(phrase), 8))
    for phrase in chinese_phrases(summary)[:24]:
        if phrase in haystack:
            score += 1
    for entity in source.get("entities", []) or []:
        entity_text = str(entity).strip()
        if entity_text and entity_text.lower() in haystack.lower():
            score += max(4, min(len(entity_text), 12))
    return score


def item_overlap_score(item_text: str, target_text: str) -> int:
    score = 0
    for token in ascii_tokens(item_text):
        if token.lower() in target_text.lower():
            score += 4
    for phrase in chinese_phrases(item_text)[:40]:
        if phrase in target_text:
            score += 2
    return score


def relevant_text_for_slot(draft: dict[str, Any], slot: dict[str, Any]) -> str:
    slot_id = str(slot.get("slot_id") or "")
    if slot_id == "cover":
        return "\n".join(
            [
                str(draft.get("title") or ""),
                str(draft.get("subtitle") or ""),
                str(slot.get("label") or ""),
            ]
        )
    section_text = section_body_for_slot(str(draft.get("body_markdown") or ""), slot_id)
    parts = [
        str(draft.get("title") or ""),
        str(draft.get("subtitle") or ""),
        str(slot.get("label") or ""),
        section_text,
    ]
    return "\n".join(parts)


def section_body_for_slot(markdown: str, slot_id: str) -> str:
    if not slot_id.startswith("section-"):
        return markdown[:1200]
    try:
        target_index = int(slot_id.split("-", 1)[1])
    except ValueError:
        return ""
    current = 0
    lines: list[str] = []
    collecting = False
    for line in markdown.splitlines():
        if re.match(r"^#{1,4}\s+", line.strip()):
            current += 1
            collecting = current == target_index
            if current > target_index:
                break
        if collecting:
            lines.append(line)
    return "\n".join(lines)


def capture_page_screenshot(url: str, output_path: Path) -> None:
    capture_page_screenshots(url, [output_path])


def capture_page_screenshots(url: str, output_paths: list[Path]) -> list[Path]:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise OfficialScreenshotError(
            "官网截图需要安装 Playwright。请在当前环境执行：pip install -r requirements.txt，然后执行：python -m playwright install chromium。"
        ) from exc

    for output_path in output_paths:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(
                    viewport={"width": 1280, "height": 720},
                    device_scale_factor=1,
                    locale="zh-CN",
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
                    ),
                )
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)
                close_common_popups(page)
                capture_viewport_series(page, output_paths)
            finally:
                browser.close()
    except PlaywrightTimeoutError as exc:
        raise OfficialScreenshotError("官网页面打开超时，请检查网络，或稍后重试。") from exc
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if "Executable doesn't exist" in message or "playwright install" in message:
            raise OfficialScreenshotError(
                "Playwright 已安装，但 Chromium 浏览器内核还没安装。请执行：python -m playwright install chromium。"
            ) from exc
        raise OfficialScreenshotError(f"官网截图失败：{message}") from exc

    captured = [path for path in output_paths if path.exists() and path.stat().st_size >= 1024]
    if not captured:
        raise OfficialScreenshotError("官网截图文件为空，请稍后重试或手动导入图片。")
    return captured


def capture_viewport_series(page: Any, output_paths: list[Path]) -> None:
    if not output_paths:
        return
    scroll_positions = [0, 520, 1040]
    for index, output_path in enumerate(output_paths):
        scroll_y = scroll_positions[min(index, len(scroll_positions) - 1)]
        try:
            if index == 2:
                scroll_y = content_scroll_position(page)
            page.evaluate("(y) => window.scrollTo(0, y)", scroll_y)
            page.wait_for_timeout(900)
            page.screenshot(path=str(output_path), full_page=False)
        except Exception:  # noqa: BLE001
            continue


def content_scroll_position(page: Any) -> int:
    try:
        value = page.evaluate(
            """
            () => {
              const selectors = ['main article', 'article', 'main', '[role="main"]'];
              for (const selector of selectors) {
                const element = document.querySelector(selector);
                if (!element) continue;
                const rect = element.getBoundingClientRect();
                const top = Math.max(0, Math.floor(rect.top + window.scrollY - 80));
                if (top > 0) return top;
              }
              return Math.min(1040, Math.max(0, document.body.scrollHeight / 3));
            }
            """
        )
        return int(value or 0)
    except Exception:  # noqa: BLE001
        return 1040


def close_common_popups(page: Any) -> None:
    selectors = [
        "button:has-text('Accept')",
        "button:has-text('I agree')",
        "button:has-text('同意')",
        "button:has-text('接受')",
        "button:has-text('知道了')",
        "[aria-label='Close']",
        "[aria-label='关闭']",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0 and locator.is_visible(timeout=500):
                locator.click(timeout=800)
        except Exception:  # noqa: BLE001
            continue


def screenshot_paths(draft_index: int, slot_id: str, url: str, count: int = 3) -> list[Path]:
    return [screenshot_path(draft_index, slot_id, url, index) for index in range(1, max(1, count) + 1)]


def screenshot_path(draft_index: int, slot_id: str, url: str, index: int = 1) -> Path:
    digest = hashlib.sha1(f"{slot_id}:{url}:{index}".encode("utf-8", errors="ignore")).hexdigest()[:12]
    safe_slot = re.sub(r"[^A-Za-z0-9_-]+", "-", slot_id).strip("-") or "slot"
    return OFFICIAL_SCREENSHOT_DIR / f"draft-{draft_index + 1:02d}-{safe_slot}-official-{index}-{digest}.png"


def clean_url(url: str) -> str:
    return (url or "").strip().rstrip(".,;，。；、)")


def normalize_url_key(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().split("@")[-1].split(":")[0].removeprefix("www.")
    except Exception:  # noqa: BLE001
        return ""


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def ascii_tokens(text: str) -> list[str]:
    return list(dict.fromkeys(match.group(0) for match in ASCII_TOKEN_RE.finditer(text or "")))


def chinese_phrases(text: str) -> list[str]:
    phrases: list[str] = []
    for chunk in CHINESE_RE.findall(text or ""):
        if len(chunk) <= 8:
            phrases.append(chunk)
            continue
        for size in (4, 3, 2):
            for index in range(0, len(chunk) - size + 1):
                phrase = chunk[index : index + size]
                if not is_generic_chinese_phrase(phrase):
                    phrases.append(phrase)
    return list(dict.fromkeys(phrases))


def is_generic_chinese_phrase(phrase: str) -> bool:
    generic = {
        "发布",
        "模型",
        "大模型",
        "用户",
        "产品",
        "升级",
        "能力",
        "实现",
        "官方",
        "消息",
        "今日",
        "全新",
        "版本",
        "行业",
    }
    return phrase in generic
