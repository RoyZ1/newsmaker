from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.env_loader import load_dotenv


WECHAT_API_BASE = "https://api.weixin.qq.com"
TOKEN_CACHE: dict[str, Any] = {}


@dataclass(slots=True)
class WeChatConfig:
    app_id: str
    app_secret: str
    author: str
    image_max_bytes: int
    image_width: int
    timeout: float


class WeChatAPIError(RuntimeError):
    def __init__(self, message: str, errcode: int | None = None) -> None:
        super().__init__(message)
        self.errcode = errcode


def load_wechat_config() -> WeChatConfig:
    load_dotenv()
    app_id = os.getenv("WECHAT_APP_ID", "").strip()
    app_secret = os.getenv("WECHAT_APP_SECRET", "").strip()
    author = os.getenv("WECHAT_AUTHOR", "").strip()
    image_max_bytes = int(os.getenv("WECHAT_IMAGE_MAX_BYTES", "950000") or 950000)
    image_width = int(os.getenv("WECHAT_IMAGE_WIDTH", "578") or 578)
    timeout = float(os.getenv("WECHAT_TIMEOUT", "60") or 60)
    if not app_id:
        raise RuntimeError("缺少 WECHAT_APP_ID，请在 .env 中配置公众号 AppID。")
    if not app_secret:
        raise RuntimeError("缺少 WECHAT_APP_SECRET，请在 .env 中配置公众号 AppSecret。")
    return WeChatConfig(
        app_id=app_id,
        app_secret=app_secret,
        author=author,
        image_max_bytes=image_max_bytes,
        image_width=image_width,
        timeout=timeout,
    )


def wechat_status() -> dict[str, Any]:
    load_dotenv()
    app_id = os.getenv("WECHAT_APP_ID", "").strip()
    app_secret = os.getenv("WECHAT_APP_SECRET", "").strip()
    return {
        "configured": bool(app_id and app_secret),
        "app_id_set": bool(app_id),
        "app_secret_set": bool(app_secret),
        "app_id_preview": preview_secret(app_id, keep=6),
        "app_secret_length": len(app_secret),
        "author_set": bool(os.getenv("WECHAT_AUTHOR", "").strip()),
        "image_max_bytes": int(os.getenv("WECHAT_IMAGE_MAX_BYTES", "950000") or 950000),
        "image_width": int(os.getenv("WECHAT_IMAGE_WIDTH", "578") or 578),
    }


def test_wechat_access_token() -> dict[str, Any]:
    config = load_wechat_config()
    result = {
        "ok": False,
        "app_id_preview": preview_secret(config.app_id, keep=6),
        "app_secret_length": len(config.app_secret),
        "errcode": None,
        "message": "",
    }
    try:
        with WeChatClient(config) as client:
            token = client.get_access_token(force_refresh=True)
    except WeChatAPIError as exc:
        result["errcode"] = exc.errcode
        result["message"] = str(exc)
        return result
    result["ok"] = True
    result["message"] = f"access_token 获取成功，长度 {len(token)}。"
    return result


def preview_secret(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return f"{value[:2]}***{value[-keep:]}"


class WeChatClient:
    def __init__(self, config: WeChatConfig | None = None) -> None:
        self.config = config or load_wechat_config()
        self.client = httpx.Client(timeout=self.config.timeout, follow_redirects=True)

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "WeChatClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def get_access_token(self, force_refresh: bool = False) -> str:
        cache_key = self.config.app_id
        cached = TOKEN_CACHE.get(cache_key)
        now = time.time()
        if not force_refresh and cached and cached.get("expires_at", 0) > now + 120:
            return str(cached["access_token"])

        response = self.client.get(
            f"{WECHAT_API_BASE}/cgi-bin/token",
            params={
                "grant_type": "client_credential",
                "appid": self.config.app_id,
                "secret": self.config.app_secret,
            },
        )
        data = parse_wechat_response(response)
        token = data.get("access_token")
        if not token:
            raise WeChatAPIError(f"微信 access_token 返回异常：{data}", data.get("errcode"))
        TOKEN_CACHE[cache_key] = {
            "access_token": token,
            "expires_at": now + int(data.get("expires_in") or 7200),
        }
        return str(token)

    def upload_article_image(self, image_path: Path) -> str:
        token = self.get_access_token()
        with image_path.open("rb") as handle:
            response = self.client.post(
                f"{WECHAT_API_BASE}/cgi-bin/media/uploadimg",
                params={"access_token": token},
                files={"media": (image_path.name, handle, "image/jpeg")},
            )
        data = parse_wechat_response(response, step="上传正文图片")
        url = data.get("url")
        if not url:
            raise WeChatAPIError(f"微信正文图片上传失败：{data}", data.get("errcode"))
        return str(url)

    def upload_permanent_image_material(self, image_path: Path) -> str:
        token = self.get_access_token()
        with image_path.open("rb") as handle:
            response = self.client.post(
                f"{WECHAT_API_BASE}/cgi-bin/material/add_material",
                params={"access_token": token, "type": "image"},
                files={"media": (image_path.name, handle, "image/jpeg")},
            )
        data = parse_wechat_response(response, step="上传永久封面素材")
        media_id = data.get("media_id")
        if not media_id:
            raise WeChatAPIError(f"微信封面素材上传失败：{data}", data.get("errcode"))
        return str(media_id)

    def upload_temporary_image_media(self, image_path: Path) -> str:
        token = self.get_access_token()
        with image_path.open("rb") as handle:
            response = self.client.post(
                f"{WECHAT_API_BASE}/cgi-bin/media/upload",
                params={"access_token": token, "type": "image"},
                files={"media": (image_path.name, handle, "image/jpeg")},
            )
        data = parse_wechat_response(response, step="上传临时封面素材")
        media_id = data.get("media_id")
        if not media_id:
            raise WeChatAPIError(f"微信临时封面素材上传失败：{data}", data.get("errcode"))
        return str(media_id)

    def add_draft(self, article: dict[str, Any]) -> dict[str, Any]:
        token = self.get_access_token()
        body = json.dumps({"articles": [article]}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        response = self.client.post(
            f"{WECHAT_API_BASE}/cgi-bin/draft/add",
            params={"access_token": token},
            content=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        data = parse_wechat_response(response, step="创建公众号草稿")
        if not data.get("media_id"):
            raise WeChatAPIError(f"微信草稿创建失败：{data}", data.get("errcode"))
        return data


def parse_wechat_response(response: httpx.Response, step: str = "调用微信接口") -> dict[str, Any]:
    try:
        response.raise_for_status()
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        raise WeChatAPIError(format_http_error(exc)) from exc
    if isinstance(data, dict) and data.get("errcode") not in {None, 0}:
        raise WeChatAPIError(f"{step}失败：{wechat_error_message(data)}", data.get("errcode"))
    return data if isinstance(data, dict) else {}


def wechat_error_message(data: dict[str, Any]) -> str:
    errcode = data.get("errcode")
    errmsg = data.get("errmsg", "")
    hints = {
        40013: "AppID 无效，请检查 WECHAT_APP_ID。",
        40125: "AppSecret 无效，请检查 WECHAT_APP_SECRET。不要填 EncodingAESKey、小程序 Secret 或旧密钥；如果刚重置过 AppSecret，请同步更新 .env 并重启本地服务。",
        40164: "当前服务器 IP 不在公众号后台 IP 白名单中。",
        45009: "接口调用频率超过限制，请稍后再试。",
    }
    hint = hints.get(errcode, "")
    return f"微信接口错误 {errcode}: {errmsg}" + (f"（{hint}）" if hint else "")


def format_http_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"微信接口 HTTP {exc.response.status_code}: {exc.request.url}"
    if isinstance(exc, httpx.TimeoutException):
        return "微信接口请求超时，请检查网络或调大 WECHAT_TIMEOUT。"
    if isinstance(exc, httpx.NetworkError):
        return "微信接口网络连接失败，请检查网络、代理或 IP 白名单。"
    text = str(exc).strip()
    return text or exc.__class__.__name__
