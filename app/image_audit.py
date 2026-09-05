from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageStat

from app.config import ROOT_DIR


DATA_IMAGE_PREFIX = "/static-data/images/"
GENERATED_IMAGE_PREFIX = "/static-data/generated-images/"
WATERMARK_HINT_RE = re.compile(
    r"(watermark|logo|qrcode|qr|weixin|wechat|公众号|微信|36kr|ithome|qbitai|leiphone|infoq|techcrunch)",
    re.I,
)


@dataclass(slots=True)
class ImageAuditResult:
    url: str
    usable: bool
    source: str
    local_path: str = ""
    width: int = 0
    height: int = 0
    dominant_ratio: float = 0.0
    colorfulness: float = 0.0
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def audit_image_candidate(url: str, official: bool = False) -> ImageAuditResult:
    result = ImageAuditResult(url=url, usable=False, source="official" if official else "unknown")
    path = resolve_static_image_path(url)
    if not path:
        result.reasons.append("不是本地可审核图片")
        return result
    result.local_path = str(path)
    if not path.exists():
        result.reasons.append("图片文件不存在")
        return result
    if path.suffix.lower() in {".gif", ".svg"}:
        result.reasons.append("动图或矢量图不适合作为公众号封面")
        return result
    if WATERMARK_HINT_RE.search(path.name) and not official:
        result.reasons.append("文件名疑似来自媒体/自媒体，不能直接发布")
        return result

    try:
        with Image.open(path) as image:
            image = image.convert("RGB")
            result.width, result.height = image.size
            quality_reasons = inspect_image_quality(image)
    except Exception as exc:  # noqa: BLE001
        result.reasons.append(f"图片无法打开：{exc}")
        return result

    result.reasons.extend(quality_reasons)
    if result.width < 640 or result.height < 360:
        result.reasons.append("图片尺寸过小")
    if result.width / max(result.height, 1) < 0.75 or result.width / max(result.height, 1) > 2.6:
        result.reasons.append("图片比例不适合公众号配图")

    result.dominant_ratio = estimate_dominant_color_ratio(path)
    result.colorfulness = estimate_colorfulness(path)
    if result.dominant_ratio >= 0.82 and result.colorfulness < 26:
        result.reasons.append("画面颜色过于单一，疑似无信息量配图")

    result.usable = official and not result.reasons
    if not official and not result.reasons:
        result.reasons.append("非官方来源图片仅可预览，不直接发布")
    return result


def audit_candidates(urls: list[str], official: bool = False) -> list[ImageAuditResult]:
    return [audit_image_candidate(url, official=official) for url in urls if url]


def choose_publishable_image(urls: list[str], official: bool = False) -> ImageAuditResult | None:
    for result in audit_candidates(urls, official=official):
        if result.usable:
            return result
    return None


def resolve_static_image_path(url: str) -> Path | None:
    if url.startswith(DATA_IMAGE_PREFIX):
        return ROOT_DIR / "data" / "images" / url.removeprefix(DATA_IMAGE_PREFIX)
    if url.startswith(GENERATED_IMAGE_PREFIX):
        return ROOT_DIR / "data" / "generated_images" / url.removeprefix(GENERATED_IMAGE_PREFIX)
    return None


def inspect_image_quality(image: Image.Image) -> list[str]:
    reasons = []
    width, height = image.size
    if width <= 0 or height <= 0:
        return ["图片尺寸无效"]

    corner_boxes = [
        (0, 0, width // 4, height // 5),
        (width * 3 // 4, 0, width, height // 5),
        (0, height * 4 // 5, width // 4, height),
        (width * 3 // 4, height * 4 // 5, width, height),
    ]
    for box in corner_boxes:
        crop = image.crop(box)
        stat = ImageStat.Stat(crop)
        if max(stat.stddev) > 62 and min(stat.mean) > 35:
            reasons.append("角落区域纹理复杂，疑似水印/角标")
            break
    return reasons


def estimate_dominant_color_ratio(path: Path) -> float:
    with Image.open(path) as image:
        image = image.convert("RGB").resize((64, 64))
        quantized = image.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
        histogram = quantized.histogram()
        total = sum(histogram)
        return max(histogram) / total if total else 1.0


def estimate_colorfulness(path: Path) -> float:
    with Image.open(path) as image:
        image = image.convert("RGB").resize((96, 96))
        pixels = list(image.getdata())
    rg_values = [r - g for r, g, _ in pixels]
    yb_values = [0.5 * (r + g) - b for r, g, b in pixels]
    rg_mean = sum(rg_values) / len(rg_values)
    yb_mean = sum(yb_values) / len(yb_values)
    rg_std = math.sqrt(sum((value - rg_mean) ** 2 for value in rg_values) / len(rg_values))
    yb_std = math.sqrt(sum((value - yb_mean) ** 2 for value in yb_values) / len(yb_values))
    return math.sqrt(rg_std**2 + yb_std**2) + 0.3 * math.sqrt(rg_mean**2 + yb_mean**2)
