from __future__ import annotations

import json
import sqlite3
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.config import ROOT_DIR


DB_PATH = ROOT_DIR / "data" / "agent.db"
SCHEMA_VERSION = 3


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    db_path = db_path or DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS news_items (
                id TEXT PRIMARY KEY,
                source_id TEXT,
                source_name TEXT,
                title TEXT NOT NULL,
                url TEXT,
                summary TEXT,
                published_at TEXT,
                collected_at TEXT,
                score REAL DEFAULT 0,
                category TEXT,
                category_label TEXT,
                image_usage TEXT,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_news_published_at ON news_items(published_at);
            CREATE INDEX IF NOT EXISTS idx_news_score ON news_items(score);
            CREATE INDEX IF NOT EXISTS idx_news_category ON news_items(category);

            CREATE TABLE IF NOT EXISTS topics (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                angle TEXT,
                score REAL DEFAULT 0,
                source_count INTEGER DEFAULT 0,
                created_at TEXT,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_topics_score ON topics(score);
            CREATE INDEX IF NOT EXISTS idx_topics_created_at ON topics(created_at);

            CREATE TABLE IF NOT EXISTS drafts (
                draft_id TEXT PRIMARY KEY,
                topic_id TEXT,
                title TEXT NOT NULL,
                subtitle TEXT,
                body_markdown TEXT,
                created_at TEXT,
                updated_at TEXT,
                payload_json TEXT NOT NULL,
                synced_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_drafts_topic_id ON drafts(topic_id);
            CREATE INDEX IF NOT EXISTS idx_drafts_updated_at ON drafts(updated_at);

            CREATE TABLE IF NOT EXISTS draft_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                draft_id TEXT,
                draft_index INTEGER,
                reason TEXT,
                title TEXT,
                file_path TEXT,
                saved_at TEXT,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_draft_versions_draft_id ON draft_versions(draft_id);
            CREATE INDEX IF NOT EXISTS idx_draft_versions_saved_at ON draft_versions(saved_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_draft_versions_file_path ON draft_versions(file_path);

            CREATE TABLE IF NOT EXISTS publications (
                id TEXT PRIMARY KEY,
                fingerprint TEXT UNIQUE,
                title TEXT NOT NULL,
                subtitle TEXT,
                topic_id TEXT,
                channel TEXT,
                published_at TEXT,
                published_date TEXT,
                archive_path TEXT,
                payload_json TEXT NOT NULL,
                synced_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_publications_date ON publications(published_date);

            CREATE TABLE IF NOT EXISTS wechat_exports (
                id TEXT PRIMARY KEY,
                draft_id TEXT,
                title TEXT,
                exported_at TEXT,
                html_path TEXT,
                json_path TEXT,
                image_dir TEXT,
                image_count INTEGER DEFAULT 0,
                payload_json TEXT NOT NULL,
                synced_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_wechat_exports_draft_id ON wechat_exports(draft_id);
            CREATE INDEX IF NOT EXISTS idx_wechat_exports_exported_at ON wechat_exports(exported_at);

            CREATE TABLE IF NOT EXISTS platform_exports (
                id TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                draft_id TEXT,
                title TEXT,
                exported_at TEXT,
                html_path TEXT,
                markdown_path TEXT,
                json_path TEXT,
                image_dir TEXT,
                image_count INTEGER DEFAULT 0,
                payload_json TEXT NOT NULL,
                synced_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_platform_exports_platform ON platform_exports(platform);
            CREATE INDEX IF NOT EXISTS idx_platform_exports_draft_id ON platform_exports(draft_id);
            CREATE INDEX IF NOT EXISTS idx_platform_exports_exported_at ON platform_exports(exported_at);

            CREATE TABLE IF NOT EXISTS platform_drafts (
                id TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                draft_id TEXT,
                source_hash TEXT,
                title TEXT,
                subtitle TEXT,
                body_markdown TEXT,
                generated_at TEXT,
                payload_json TEXT NOT NULL,
                synced_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_platform_drafts_platform ON platform_drafts(platform);
            CREATE INDEX IF NOT EXISTS idx_platform_drafts_draft_id ON platform_drafts(draft_id);
            CREATE INDEX IF NOT EXISTS idx_platform_drafts_generated_at ON platform_drafts(generated_at);
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )


def sync_news_items(items: Iterable[dict[str, Any]]) -> None:
    init_db()
    now = utc_now()
    rows = []
    for item in items:
        rows.append(
            (
                str(item.get("id") or ""),
                item.get("source_id", ""),
                item.get("source_name", ""),
                item.get("title", ""),
                item.get("url", ""),
                item.get("summary", ""),
                item.get("published_at", ""),
                item.get("collected_at", ""),
                float(item.get("score") or 0),
                item.get("category", ""),
                item.get("category_label", ""),
                item.get("image_usage", ""),
                to_json(item),
                now,
            )
        )
    with connect() as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO news_items(
                id, source_id, source_name, title, url, summary, published_at,
                collected_at, score, category, category_label, image_usage,
                payload_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [row for row in rows if row[0]],
        )


def sync_topics(topics: Iterable[dict[str, Any]]) -> None:
    init_db()
    now = utc_now()
    rows = []
    for topic in topics:
        rows.append(
            (
                str(topic.get("id") or ""),
                topic.get("title", ""),
                topic.get("angle", ""),
                float(topic.get("score") or 0),
                int(topic.get("source_count") or 0),
                topic.get("created_at", ""),
                to_json(topic),
                now,
            )
        )
    with connect() as conn:
        conn.execute("DELETE FROM topics")
        conn.executemany(
            """
            INSERT OR REPLACE INTO topics(
                id, title, angle, score, source_count, created_at, payload_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [row for row in rows if row[0]],
        )


def sync_drafts(drafts: Iterable[dict[str, Any]]) -> None:
    init_db()
    now = utc_now()
    rows = []
    for index, draft in enumerate(drafts):
        draft_id = stable_draft_id(draft, index)
        rows.append(
            (
                draft_id,
                draft.get("topic_id", ""),
                draft.get("title", ""),
                draft.get("subtitle", ""),
                draft.get("body_markdown", ""),
                draft.get("created_at", ""),
                draft.get("updated_at", ""),
                to_json(draft),
                now,
            )
        )
    with connect() as conn:
        conn.execute("DELETE FROM drafts WHERE draft_id LIKE 'draft-index-%'")
        conn.executemany(
            """
            INSERT OR REPLACE INTO drafts(
                draft_id, topic_id, title, subtitle, body_markdown, created_at,
                updated_at, payload_json, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [row for row in rows if row[0]],
        )


def sync_draft_version(version_payload: dict[str, Any], file_path: Path) -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO draft_versions(
                draft_id, draft_index, reason, title, file_path, saved_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_payload.get("draft_id", ""),
                int(version_payload.get("draft_index") or 0),
                version_payload.get("reason", ""),
                version_payload.get("title", ""),
                str(file_path),
                version_payload.get("saved_at", ""),
                to_json(version_payload),
            ),
        )


def sync_publications(records: Iterable[dict[str, Any]]) -> None:
    init_db()
    now = utc_now()
    rows = []
    for record in records:
        rows.append(
            (
                str(record.get("id") or ""),
                record.get("fingerprint", ""),
                record.get("title", ""),
                record.get("subtitle", ""),
                record.get("topic_id", ""),
                record.get("channel", ""),
                record.get("published_at", ""),
                record.get("published_date", ""),
                record.get("archive_path", ""),
                to_json(record),
                now,
            )
        )
    with connect() as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO publications(
                id, fingerprint, title, subtitle, topic_id, channel, published_at,
                published_date, archive_path, payload_json, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [row for row in rows if row[0]],
        )


def sync_wechat_export(export: dict[str, Any]) -> None:
    init_db()
    export_id = stable_export_id(export)
    with connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO wechat_exports(
                id, draft_id, title, exported_at, html_path, json_path, image_dir,
                image_count, payload_json, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                export_id,
                export.get("draft_id", ""),
                export.get("title", ""),
                export.get("exported_at", ""),
                export.get("html_path", ""),
                export.get("json_path", ""),
                export.get("image_dir", ""),
                int(export.get("image_count") or 0),
                to_json(export),
                utc_now(),
            ),
        )


def sync_platform_export(export: dict[str, Any]) -> None:
    init_db()
    export_id = stable_platform_export_id(export)
    with connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO platform_exports(
                id, platform, draft_id, title, exported_at, html_path, markdown_path,
                json_path, image_dir, image_count, payload_json, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                export_id,
                export.get("platform", ""),
                export.get("draft_id", ""),
                export.get("title", ""),
                export.get("exported_at", ""),
                export.get("html_path", ""),
                export.get("markdown_path", ""),
                export.get("json_path", ""),
                export.get("image_dir", ""),
                int(export.get("image_count") or 0),
                to_json(export),
                utc_now(),
            ),
        )


def sync_platform_draft(platform_draft: dict[str, Any]) -> None:
    init_db()
    row_id = stable_platform_draft_id(platform_draft)
    with connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO platform_drafts(
                id, platform, draft_id, source_hash, title, subtitle, body_markdown,
                generated_at, payload_json, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                platform_draft.get("platform", ""),
                platform_draft.get("draft_id", ""),
                platform_draft.get("source_hash", ""),
                platform_draft.get("title", ""),
                platform_draft.get("subtitle", ""),
                platform_draft.get("body_markdown", ""),
                platform_draft.get("generated_at", ""),
                to_json(platform_draft),
                utc_now(),
            ),
        )


def database_summary() -> dict[str, Any]:
    init_db()
    with connect() as conn:
        tables = ["news_items", "topics", "drafts", "draft_versions", "publications", "wechat_exports", "platform_exports", "platform_drafts"]
        counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
        latest = {}
        latest["news_items"] = fetch_latest(conn, "news_items", "updated_at", ["title", "source_name", "published_at", "score"])
        latest["topics"] = fetch_latest(conn, "topics", "updated_at", ["title", "score", "created_at"])
        latest["drafts"] = fetch_latest(conn, "drafts", "synced_at", ["title", "updated_at", "created_at"])
        latest["publications"] = fetch_latest(conn, "publications", "synced_at", ["title", "published_date", "channel"])
        latest["wechat_exports"] = fetch_latest(conn, "wechat_exports", "synced_at", ["title", "exported_at", "html_path"])
        latest["platform_exports"] = fetch_latest(conn, "platform_exports", "synced_at", ["platform", "title", "exported_at", "html_path"])
        latest["platform_drafts"] = fetch_latest(conn, "platform_drafts", "synced_at", ["platform", "title", "generated_at"])
    return {
        "db_path": str(DB_PATH),
        "schema_version": SCHEMA_VERSION,
        "counts": counts,
        "latest": latest,
    }


def backfill_from_json() -> dict[str, Any]:
    init_db()
    data_dir = ROOT_DIR / "data"
    results: dict[str, Any] = {}
    items_path = data_dir / "items.json"
    topics_path = data_dir / "topics.json"
    drafts_path = data_dir / "drafts.json"
    heybox_drafts_path = data_dir / "heybox_drafts.json"
    publications_path = data_dir / "publications.json"

    if items_path.exists():
        items = load_json_list(items_path)
        sync_news_items(items)
        results["news_items"] = len(items)
    else:
        results["news_items"] = 0

    if topics_path.exists():
        topics = load_json_list(topics_path)
        sync_topics(topics)
        results["topics"] = len(topics)
    else:
        results["topics"] = 0

    if drafts_path.exists():
        drafts = load_json_list(drafts_path)
        sync_drafts(drafts)
        results["drafts"] = len(drafts)
    else:
        results["drafts"] = 0

    if publications_path.exists():
        publications = load_json_list(publications_path)
        sync_publications(publications)
        results["publications"] = len(publications)
    else:
        results["publications"] = 0
    if heybox_drafts_path.exists():
        heybox_drafts = load_json_dict(heybox_drafts_path)
        count = 0
        for item in heybox_drafts.values():
            if isinstance(item, dict):
                sync_platform_draft(item)
                count += 1
        results["platform_drafts"] = count
    else:
        results["platform_drafts"] = 0
    results["draft_versions"] = backfill_draft_versions(data_dir / "draft_versions")
    results["wechat_exports"] = backfill_wechat_exports(data_dir / "exports" / "wechat")
    return results


def load_json_list(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, list) else []


def load_json_dict(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def backfill_draft_versions(root: Path) -> int:
    if not root.exists():
        return 0
    count = 0
    for path in root.rglob("*.json"):
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                sync_draft_version(payload, path)
                count += 1
        except (OSError, json.JSONDecodeError):
            continue
    return count


def backfill_wechat_exports(root: Path) -> int:
    if not root.exists():
        return 0
    count = 0
    for html_path in root.rglob("*.html"):
        json_path = html_path.with_suffix(".json")
        payload: dict[str, Any] = {}
        if json_path.exists():
            try:
                with json_path.open("r", encoding="utf-8") as handle:
                    parsed = json.load(handle)
                if isinstance(parsed, dict):
                    payload = parsed
            except (OSError, json.JSONDecodeError):
                payload = {}
        export = {
            "draft_id": "",
            "title": title_from_export_file(html_path),
            "exported_at": exported_at_from_path(html_path),
            "html_path": str(html_path),
            "json_path": str(json_path) if json_path.exists() else "",
            "image_dir": str(html_path.with_name(f"{html_path.stem}-images")) if html_path.with_name(f"{html_path.stem}-images").exists() else "",
            "image_count": len(payload.get("image_manifest") or []) if isinstance(payload.get("image_manifest"), list) else 0,
            "payload": payload,
        }
        sync_wechat_export(export)
        count += 1
    return count


def title_from_export_file(path: Path) -> str:
    stem = path.stem
    match = re.match(r"^\d{6}-(.+)-[0-9a-f]{10,16}$", stem)
    if match:
        return match.group(1)
    return stem


def exported_at_from_path(path: Path) -> str:
    date_key = path.parent.name
    time_match = re.match(r"^(\d{2})(\d{2})(\d{2})-", path.stem)
    if time_match and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_key):
        hour, minute, second = time_match.groups()
        return f"{date_key}T{hour}:{minute}:{second}+08:00"
    return ""


def fetch_latest(conn: sqlite3.Connection, table: str, order_by: str, fields: list[str], limit: int = 5) -> list[dict[str, Any]]:
    columns = ", ".join(fields)
    rows = conn.execute(f"SELECT {columns} FROM {table} ORDER BY {order_by} DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def stable_export_id(export: dict[str, Any]) -> str:
    raw = f"{export.get('draft_id', '')}:{export.get('exported_at', '')}:{export.get('html_path', '')}"
    import hashlib

    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]


def stable_platform_export_id(export: dict[str, Any]) -> str:
    raw = f"{export.get('platform', '')}:{export.get('draft_id', '')}:{export.get('exported_at', '')}:{export.get('html_path', '')}"
    import hashlib

    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]


def stable_platform_draft_id(platform_draft: dict[str, Any]) -> str:
    raw = f"{platform_draft.get('platform', '')}:{platform_draft.get('draft_id', '')}:{platform_draft.get('source_hash', '')}"
    import hashlib

    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]


def stable_draft_id(draft: dict[str, Any], index: int = 0) -> str:
    draft_id = str(draft.get("draft_id") or "").strip()
    if draft_id:
        return draft_id
    raw = f"{index}:{draft.get('topic_id', '')}:{draft.get('title', '')}:{draft.get('created_at', '')}"
    import hashlib

    return f"draft-{hashlib.sha1(raw.encode('utf-8', errors='ignore')).hexdigest()[:16]}"


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
