from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import ROOT_DIR


IMPORTED_IMAGE_DIR = ROOT_DIR / "data" / "imported_images"
IMPORTED_IMAGE_URL_PREFIX = "/static-data/imported-images"
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_UPLOAD_BYTES = 12 * 1024 * 1024


def save_imported_image(stream: BinaryIO, filename: str, draft_index: int, slot_id: str) -> dict:
    raw = stream.read()
    if not raw:
        raise ValueError("上传的图片为空。")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("图片太大，请上传 12MB 以内的图片。")

    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError("仅支持 jpg、jpeg、png、webp 图片。")

    digest = hashlib.sha1(raw).hexdigest()[:12]
    safe_slot = "".join(char if char.isalnum() or char in "-_" else "-" for char in slot_id).strip("-") or "slot"
    output_name = f"draft-{draft_index + 1:02d}-{safe_slot}-{digest}.png"
    output_path = IMPORTED_IMAGE_DIR / output_name
    output_path.parent.mkdir(parents=True, exist_ok=True)

    temp_path = output_path.with_suffix(".upload")
    temp_path.write_bytes(raw)
    try:
        with Image.open(temp_path) as image:
            image = ImageOps.exif_transpose(image)
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            image.save(output_path, format="PNG", optimize=True)
    except (OSError, UnidentifiedImageError) as exc:
        output_path.unlink(missing_ok=True)
        raise ValueError("无法读取这张图片，请确认文件没有损坏。") from exc
    finally:
        temp_path.unlink(missing_ok=True)

    return {
        "type": "manual_upload",
        "prompt": "",
        "visual_angle": "人工导入图片",
        "entities": [],
        "safety_notes": ["人工导入图片，请确认版权、水印和公众号发布合规性。"],
        "local_path": str(output_path),
        "url": f"{IMPORTED_IMAGE_URL_PREFIX}/{output_path.name}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "slot_id": slot_id,
        "slot_label": "",
        "manual_selected": True,
    }
