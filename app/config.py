from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.models import SourceConfig


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT_DIR / "config" / "sources.yml"


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def load_sources(path: Path = DEFAULT_CONFIG_PATH) -> list[SourceConfig]:
    config = load_config(path)
    sources = []
    for source in config.get("sources", []):
        sources.append(SourceConfig(**source))
    return sources


def get_app_settings(path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    config = load_config(path)
    return config.get("app", {})
