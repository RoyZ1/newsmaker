from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import ROOT_DIR


CHANGELOG_PATH = ROOT_DIR / "data" / "changelog.json"


def load_changelog(path: Path = CHANGELOG_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return seed_changelog(path)
    with path.open("r", encoding="utf-8") as handle:
        entries = json.load(handle)
    if not isinstance(entries, list):
        return []
    clean_entries = [entry for entry in entries if valid_changelog_entry(entry)]
    return sorted(clean_entries, key=lambda item: str(item.get("timestamp") or ""), reverse=True)


def append_changelog_entry(
    title: str,
    details: list[str],
    category: str = "功能更新",
    timestamp: str | None = None,
    path: Path = CHANGELOG_PATH,
) -> dict[str, Any]:
    candidate = {
        "category": category,
        "title": title,
        "details": details,
    }
    if not valid_changelog_entry(candidate, require_timestamp=False):
        raise ValueError("更新日志内容疑似乱码或为空，已拒绝写入。")
    entries = load_changelog(path)
    entry = {
        "id": stable_entry_id(timestamp or datetime.now(timezone.utc).isoformat(), title),
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "category": category,
        "title": title,
        "details": details,
    }
    entries = [item for item in entries if item.get("id") != entry["id"]]
    entries.append(entry)
    save_changelog(entries, path)
    return entry


def save_changelog(entries: list[dict[str, Any]], path: Path = CHANGELOG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = [entry for entry in entries if valid_changelog_entry(entry)]
    entries = sorted(entries, key=lambda item: str(item.get("timestamp") or ""), reverse=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(entries, handle, ensure_ascii=False, indent=2)


def valid_changelog_entry(entry: Any, *, require_timestamp: bool = True) -> bool:
    if not isinstance(entry, dict):
        return False
    if require_timestamp and not str(entry.get("timestamp") or "").strip():
        return False
    title = str(entry.get("title") or "").strip()
    category = str(entry.get("category") or "").strip()
    details = entry.get("details")
    if not title or not category or not isinstance(details, list) or not details:
        return False
    text = " ".join([category, title, *[str(item or "") for item in details]])
    return not looks_garbled(text)


def looks_garbled(text: str) -> bool:
    compact = "".join(str(text or "").split())
    if not compact:
        return True
    question_count = compact.count("?")
    if question_count >= 4 and question_count / max(len(compact), 1) > 0.08:
        return True
    if "�" in compact:
        return True
    mojibake_markers = ("Ã", "Â", "â€", "å½", "æ", "ç", "é", "鍔", "鏂", "绋", "€?")
    marker_hits = sum(1 for marker in mojibake_markers if marker in compact)
    return marker_hits >= 2


def seed_changelog(path: Path = CHANGELOG_PATH) -> list[dict[str, Any]]:
    entries = [
        {
            "id": "2026-06-16-collector-server",
            "timestamp": "2026-06-16T15:30:00+08:00",
            "category": "基础能力",
            "title": "搭建新闻采集和本地工作台",
            "details": [
                "新增 Flask 工作台、新闻采集入口和本地静态数据访问。",
                "支持从 RSS、网页、JSON、changelog 等来源采集 AI/科技信息。",
                "增加依赖说明，解决缺少 Flask 时无法启动的问题。",
            ],
        },
        {
            "id": "2026-06-16-sources-scoring",
            "timestamp": "2026-06-16T18:30:00+08:00",
            "category": "采集与排序",
            "title": "扩展信息源、时间戳和热度评分",
            "details": [
                "加入国内 AI 公司与媒体来源，包括 DeepSeek、智谱、Kimi、量子位、IT之家、InfoQ、36Kr、雷峰网等。",
                "要求采集项必须带发布时间，并按日期归档。",
                "增加热度评分：官方来源、顶尖公司、模型发布、论文、融资、时效性都会影响排序。",
            ],
        },
        {
            "id": "2026-06-17-writer-prompts",
            "timestamp": "2026-06-17T11:30:00+08:00",
            "category": "内容创作",
            "title": "接入大模型写作和独立 prompt 文件",
            "details": [
                "接入 .env 中的大模型配置，用于生成公众号文章草稿。",
                "将写作 prompt、参考写法和图片 prompt 从函数中拆出到 config/prompts。",
                "强化原创写作要求：不写来源提示，不搬运摘要，按主线创作。",
            ],
        },
        {
            "id": "2026-06-17-image-generation",
            "timestamp": "2026-06-17T17:00:00+08:00",
            "category": "配图能力",
            "title": "接入图片生成、候选图库和官网截图",
            "details": [
                "支持每篇文章、每个小标题位置单独配图。",
                "区分官方图、媒体预览图、生成图、官网截图和手动导入图。",
                "媒体图默认不直接作为发布图，避免水印和版权风险。",
            ],
        },
        {
            "id": "2026-06-18-wechat-export-sync",
            "timestamp": "2026-06-18T14:30:00+08:00",
            "category": "公众号能力",
            "title": "新增公众号导出和草稿箱同步",
            "details": [
                "支持导出公众号格式 HTML，并生成可下载 PNG 图片。",
                "接入微信公众号草稿箱同步接口，只同步草稿，不自动发布。",
                "修复标题长度超限和副标题误写入标题导致的 45003 问题。",
            ],
        },
        {
            "id": "2026-06-18-database-records",
            "timestamp": "2026-06-18T16:00:00+08:00",
            "category": "存储与记录",
            "title": "加入 SQLite、本地归档和发布备案",
            "details": [
                "将新闻、选题、草稿、发布记录同步到 SQLite。",
                "按日期保存采集结果和已发布内容，减少重复发布风险。",
                "支持编辑草稿并保存，下次加载读取保存后的版本。",
            ],
        },
        {
            "id": "2026-06-18-opinion-materials",
            "timestamp": "2026-06-18T19:30:00+08:00",
            "category": "舆论素材",
            "title": "新增抖音/微博舆论素材接口框架",
            "details": [
                "新增自动采集入口，支持通过授权中转接口接入 douyin/weibo 评论。",
                "新增手动导入评论文本和评论截图，自动生成匿名评论卡片。",
                "截图导入会做基础打码处理，避免直接暴露头像和昵称。",
            ],
        },
        {
            "id": "2026-06-18-industry-sources",
            "timestamp": "2026-06-18T20:30:00+08:00",
            "category": "信息源",
            "title": "扩展产业侧 AI 来源",
            "details": [
                "新增华为中文/英文官方 RSS、AWS Machine Learning Blog、Microsoft Official Blog、Samsung Global Newsroom。",
                "补充华为、昇腾、盘古、鸿蒙、芯片、算力、智能汽车等关键词。",
                "AWS 官方源单独开启正文图片抓取，避免全局抓图拖慢采集。",
            ],
        },
        {
            "id": "2026-06-19-humanizer-title",
            "timestamp": "2026-06-19T10:50:00+08:00",
            "category": "写作优化",
            "title": "接入 Humanizer-zh 和标题搜索规则",
            "details": [
                "解压并分析 Humanizer-zh skill，提炼为 config/prompts/humanizer_zh.md。",
                "新增标题搜索规则，要求标题保留公司、模型、技术或场景关键词。",
                "新增标题校验，拦截只有情绪判断、没有检索对象的标题。",
            ],
        },
        {
            "id": "2026-06-19-single-draft-regenerate",
            "timestamp": "2026-06-19T11:30:00+08:00",
            "category": "草稿流程",
            "title": "支持单篇草稿重新生成",
            "details": [
                "新增 POST /api/drafts/<draft_index>/regenerate。",
                "每篇草稿增加“重新生成本文”按钮，只覆盖当前文章，不影响其它草稿。",
                "单篇重写后只为当前文章补配图。",
            ],
        },
        {
            "id": "2026-06-19-changelog",
            "timestamp": "2026-06-19T12:00:00+08:00",
            "category": "可观测性",
            "title": "新增更新日志页面",
            "details": [
                "新增 data/changelog.json 保存更新记录。",
                "工作台新增“更新日志”分页，按时间线展示功能、接口、修复和 skill 接入记录。",
                "新增 /api/changelog，方便后续查看和扩展。",
            ],
        },
    ]
    save_changelog(entries, path)
    return load_changelog(path)


def stable_entry_id(timestamp: str, title: str) -> str:
    safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in f"{timestamp}-{title}")
    return "-".join(part for part in safe.split("-") if part)[:96]
