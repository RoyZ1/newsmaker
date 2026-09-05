from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import ROOT_DIR, load_config
from app.env_loader import load_dotenv


DOUYIN_TOKEN_PATH = ROOT_DIR / "data" / "secrets" / "douyin_oauth.json"
DOUYIN_AUTH_URL = "https://open.douyin.com/platform/oauth/connect/"
DOUYIN_ACCESS_TOKEN_URL = "https://open.douyin.com/oauth/access_token/"
DOUYIN_REFRESH_TOKEN_URL = "https://open.douyin.com/oauth/refresh_token/"
DEFAULT_DOUYIN_SCOPE = "user_info,item.comment"


class DouyinAuthError(RuntimeError):
    pass


def douyin_oauth_config() -> dict[str, str]:
    load_dotenv()
    configured = {}
    try:
        configured = (load_config().get("opinion_sources") or {}).get("douyin", {}) or {}
    except Exception:  # noqa: BLE001
        configured = {}
    return {
        "client_key": os.getenv("DOUYIN_CLIENT_KEY", str(configured.get("client_key") or "")),
        "client_secret": os.getenv("DOUYIN_CLIENT_SECRET", str(configured.get("client_secret") or "")),
        "redirect_uri": os.getenv("DOUYIN_REDIRECT_URI", str(configured.get("redirect_uri") or "")),
        "scope": os.getenv("DOUYIN_OAUTH_SCOPE", str(configured.get("oauth_scope") or DEFAULT_DOUYIN_SCOPE)),
        "optional_scope": os.getenv("DOUYIN_OPTIONAL_SCOPE", str(configured.get("optional_scope") or "")),
        "auth_url": os.getenv("DOUYIN_AUTH_URL", str(configured.get("auth_url") or DOUYIN_AUTH_URL)),
        "access_token_url": os.getenv("DOUYIN_ACCESS_TOKEN_URL", str(configured.get("access_token_url") or DOUYIN_ACCESS_TOKEN_URL)),
        "refresh_token_url": os.getenv("DOUYIN_REFRESH_TOKEN_URL", str(configured.get("refresh_token_url") or DOUYIN_REFRESH_TOKEN_URL)),
    }


def douyin_auth_status() -> dict[str, Any]:
    config = douyin_oauth_config()
    token = load_douyin_token()
    missing = []
    for key in ("client_key", "client_secret", "redirect_uri"):
        if not config.get(key):
            missing.append(f"DOUYIN_{key.upper()}")
    has_token = bool(token.get("access_token") and token.get("open_id"))
    expired = token_expired(token.get("expires_at")) if has_token else True
    return {
        "configured": not missing,
        "missing": missing,
        "scope": config.get("scope"),
        "has_token": has_token,
        "token_expired": expired,
        "open_id_set": bool(token.get("open_id")),
        "expires_at": token.get("expires_at") or "",
        "refresh_expires_at": token.get("refresh_expires_at") or "",
        "token_file": str(DOUYIN_TOKEN_PATH),
    }


def build_douyin_authorize_url(state: str = "") -> dict[str, Any]:
    config = douyin_oauth_config()
    missing = [key for key in ("client_key", "redirect_uri") if not config.get(key)]
    if missing:
        readable = "、".join(f"DOUYIN_{key.upper()}" for key in missing)
        raise DouyinAuthError(f"生成抖音授权链接缺少：{readable}。")
    state_value = state or f"local-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    params = {
        "client_key": config["client_key"],
        "response_type": "code",
        "scope": config["scope"],
        "redirect_uri": config["redirect_uri"],
        "state": state_value,
    }
    if config.get("optional_scope"):
        params["optionalScope"] = config["optional_scope"]
    return {
        "authorize_url": f"{config['auth_url'].rstrip('/')}?{urlencode(params)}",
        "state": state_value,
        "scope": config["scope"],
        "redirect_uri": config["redirect_uri"],
    }


def exchange_douyin_code(code: str) -> dict[str, Any]:
    config = douyin_oauth_config()
    if not code.strip():
        raise DouyinAuthError("缺少抖音授权回调 code。")
    missing = [key for key in ("client_key", "client_secret") if not config.get(key)]
    if missing:
        readable = "、".join(f"DOUYIN_{key.upper()}" for key in missing)
        raise DouyinAuthError(f"用 code 换 token 缺少：{readable}。")

    payload = {
        "client_key": config["client_key"],
        "client_secret": config["client_secret"],
        "code": code.strip(),
        "grant_type": "authorization_code",
    }
    response_payload = post_douyin_form(config["access_token_url"], payload)
    token = normalize_token_payload(response_payload)
    if not token.get("access_token") or not token.get("open_id"):
        raise DouyinAuthError("抖音返回中没有 access_token 或 open_id，请检查 code 是否已使用或权限是否通过。")
    save_douyin_token(token)
    return {"saved": True, "auth_status": douyin_auth_status()}


def refresh_douyin_access_token() -> dict[str, Any]:
    config = douyin_oauth_config()
    token = load_douyin_token()
    refresh_token = str(token.get("refresh_token") or "").strip()
    if not refresh_token:
        raise DouyinAuthError("本地没有 refresh_token，请重新走抖音授权。")
    payload = {
        "client_key": config["client_key"],
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    response_payload = post_douyin_form(config["refresh_token_url"], payload)
    refreshed = normalize_token_payload(response_payload, previous=token)
    save_douyin_token(refreshed)
    return {"saved": True, "auth_status": douyin_auth_status()}


def post_douyin_form(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        response = httpx.post(
            url,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            timeout=20,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:400] if exc.response is not None else ""
        raise DouyinAuthError(f"抖音 OAuth 请求失败 HTTP {exc.response.status_code}：{body}") from exc
    except httpx.HTTPError as exc:
        raise DouyinAuthError(f"抖音 OAuth 网络请求失败：{exc}") from exc
    try:
        data = response.json()
    except json.JSONDecodeError as exc:
        raise DouyinAuthError("抖音 OAuth 返回不是 JSON，请检查接口地址。") from exc
    ensure_oauth_success(data)
    return data


def ensure_oauth_success(payload: dict[str, Any]) -> None:
    error_code = nested_value(payload, ("error_code", "err_no", "code"))
    if error_code in (None, "", 0, "0"):
        return
    description = nested_value(payload, ("description", "message", "err_msg", "msg")) or "未知错误"
    raise DouyinAuthError(f"抖音 OAuth 接口错误 {error_code}：{description}")


def normalize_token_payload(payload: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    previous = previous or {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    now = datetime.now(timezone.utc)
    expires_in = safe_int(data.get("expires_in") or data.get("expiresIn") or previous.get("expires_in"))
    refresh_expires_in = safe_int(data.get("refresh_expires_in") or data.get("refreshExpiresIn") or previous.get("refresh_expires_in"))
    token = {
        "enabled": True,
        "mode": "official_video",
        "access_token": data.get("access_token") or previous.get("access_token") or "",
        "open_id": data.get("open_id") or previous.get("open_id") or "",
        "refresh_token": data.get("refresh_token") or previous.get("refresh_token") or "",
        "scope": data.get("scope") or previous.get("scope") or "",
        "expires_in": expires_in,
        "refresh_expires_in": refresh_expires_in,
        "created_at": previous.get("created_at") or now.isoformat(),
        "updated_at": now.isoformat(),
    }
    if expires_in:
        token["expires_at"] = (now + timedelta(seconds=max(0, expires_in - 300))).isoformat()
    else:
        token["expires_at"] = previous.get("expires_at") or ""
    if refresh_expires_in:
        token["refresh_expires_at"] = (now + timedelta(seconds=max(0, refresh_expires_in - 300))).isoformat()
    else:
        token["refresh_expires_at"] = previous.get("refresh_expires_at") or ""
    return token


def load_douyin_token(path: Path = DOUYIN_TOKEN_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_douyin_token(token: dict[str, Any], path: Path = DOUYIN_TOKEN_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(token, handle, ensure_ascii=False, indent=2)


def token_expired(value: Any) -> bool:
    if not value:
        return True
    try:
        expires_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc)


def nested_value(payload: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(payload, dict):
        for key in keys:
            if key in payload:
                return payload.get(key)
        for value in payload.values():
            found = nested_value(value, keys)
            if found not in (None, ""):
                return found
    return None


def safe_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0
