from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.config import ROOT_DIR
from app.env_loader import load_dotenv


GENERATED_IMAGE_DIR = ROOT_DIR / "data" / "generated_images"


@dataclass(slots=True)
class ImageConfig:
    api_key: str
    model: str
    base_url: str
    timeout: float


def load_image_config() -> ImageConfig:
    load_dotenv()
    api_key = os.getenv("IMAGE_API_KEY", "").strip()
    model = os.getenv("IMAGE_MODEL_ID", "").strip()
    base_url = os.getenv("IMAGE_BASE_URL", "").strip().rstrip("/")
    timeout = float(os.getenv("IMAGE_TIMEOUT", "180") or 180)
    if not api_key:
        raise RuntimeError("Missing IMAGE_API_KEY in .env")
    if not model:
        raise RuntimeError("Missing IMAGE_MODEL_ID in .env")
    if not base_url:
        raise RuntimeError("Missing IMAGE_BASE_URL in .env")
    return ImageConfig(api_key=api_key, model=model, base_url=base_url, timeout=timeout)


def generate_image(prompt: str, output_name: str = "test-cover.png", size: str = "1024x1024") -> Path:
    config = load_image_config()
    if "dashscope.aliyuncs.com" in config.base_url or config.base_url.endswith("/api/v1"):
        return generate_dashscope_image(config, prompt, output_name, size)
    return generate_openai_compatible_image(config, prompt, output_name, size)


def generate_openai_compatible_image(
    config: ImageConfig,
    prompt: str,
    output_name: str,
    size: str,
) -> Path:
    url = f"{config.base_url}/images/generations"
    payload: dict[str, Any] = {
        "model": config.model,
        "prompt": prompt,
        "size": size,
        "n": 1,
    }
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }
    GENERATED_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = GENERATED_IMAGE_DIR / output_name
    with httpx.Client(timeout=config.timeout, follow_redirects=True) as client:
        response = client.post(url, headers=headers, json=payload)
        if response.is_error:
            raise RuntimeError(format_image_api_error("Image API", response))
        data = response.json()
        item = data["data"][0]
        if item.get("b64_json"):
            out_path.write_bytes(base64.b64decode(item["b64_json"]))
            return out_path
        if item.get("url"):
            image_response = client.get(item["url"])
            image_response.raise_for_status()
            out_path.write_bytes(image_response.content)
            return out_path
    raise RuntimeError("Image API response did not contain b64_json or url")


def generate_dashscope_image(
    config: ImageConfig,
    prompt: str,
    output_name: str,
    size: str,
) -> Path:
    base_url = config.base_url.rstrip("/")
    if base_url.endswith("/api/v1"):
        url = f"{base_url}/services/aigc/multimodal-generation/generation"
    else:
        url = f"{base_url}/api/v1/services/aigc/multimodal-generation/generation"
    dashscope_size = normalize_dashscope_size(config.model, size)
    payload: dict[str, Any] = {
        "model": config.model,
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [{"text": prompt}],
                }
            ]
        },
        "parameters": {
            "size": dashscope_size,
            "n": 1,
            "watermark": False,
            "prompt_extend": True,
        },
    }
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }
    GENERATED_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = GENERATED_IMAGE_DIR / output_name
    with httpx.Client(timeout=config.timeout, follow_redirects=True) as client:
        response = client.post(url, headers=headers, json=payload)
        if response.is_error:
            raise RuntimeError(format_dashscope_error(response))
        data = response.json()
        image_url = extract_dashscope_image_url(data)
        if not image_url:
            raise RuntimeError(f"DashScope response did not contain image url: {data}")
        image_response = client.get(image_url)
        image_response.raise_for_status()
        out_path.write_bytes(image_response.content)
        return out_path


def extract_dashscope_image_url(data: dict[str, Any]) -> str | None:
    output = data.get("output") or {}
    results = output.get("results") or []
    if results and isinstance(results[0], dict) and results[0].get("url"):
        return results[0]["url"]
    choices = output.get("choices") or []
    for choice in choices:
        message = choice.get("message") or {}
        for content in message.get("content") or []:
            if isinstance(content, dict) and content.get("image"):
                return content["image"]
    return None


def normalize_dashscope_size(model: str, size: str) -> str:
    requested = (size or "1024x1024").replace("*", "x").lower()
    if model in {"wan2.6-t2i", "wan2.5-t2i-preview"} and requested == "1024x1024":
        return "1280*1280"
    return requested.replace("x", "*")


def format_dashscope_error(response: httpx.Response) -> str:
    return format_image_api_error("DashScope 图片生成失败", response)


def format_image_api_error(label: str, response: httpx.Response) -> str:
    detail = response.text[:800]
    try:
        data = response.json()
    except ValueError:
        data = {}
    if isinstance(data, dict):
        code = data.get("code") or data.get("error_code") or data.get("err_code")
        message = data.get("message") or data.get("description") or data.get("err_msg")
        request_id = data.get("request_id") or data.get("requestId")
        parts = [f"{label} HTTP {response.status_code}"]
        if code:
            parts.append(f"code={code}")
        if message:
            parts.append(str(message))
        if request_id:
            parts.append(f"request_id={request_id}")
        return "：".join(parts)
    return f"{label} HTTP {response.status_code}：{detail}"
