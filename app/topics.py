from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import ROOT_DIR
from app.database import sync_topics
from app.llm_client import chat_completion
from app.prompting import load_prompt_json
from app.storage import load_items


TOPICS_PATH = ROOT_DIR / "data" / "topics.json"
TOPIC_PROMPT_PATH = ROOT_DIR / "config" / "prompts" / "topic_editor.json"
DEFAULT_TOPIC_LIMIT = 11
GITHUB_PROJECT_TOPIC_SLOTS = 1
TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9\-]{2,}|[\u4e00-\u9fff]{2,}")
FORBIDDEN_TOPIC_PHRASES = (
    "释放信号",
    "哪些信号",
    "用户入口",
    "开发者生态",
    "企业采购",
    "企业决策",
    "行业格局",
    "生态格局",
    "正在如何",
    "值得关注",
    "赋能",
    "重塑",
    "深刻改变",
    "标志着",
)
RISKY_UNSUPPORTED_TERMS = (
    "涨价",
    "降价",
    "折扣",
    "会员",
    "订阅",
    "家庭共享",
    "游戏库",
    "库存",
    "封禁",
    "禁令",
    "下架",
    "停服",
    "关服",
    "裁员",
    "收购",
    "融资",
    "上市",
    "破产",
    "罚款",
    "赔偿",
    "泄露",
    "漏洞",
    "隐私",
    "监管",
    "政策",
    "family sharing",
    "layoff",
    "acquisition",
    "funding",
    "ban",
    "leak",
)

ENTITY_ALIASES = {
    "OpenAI": ["openai", "gpt", "chatgpt", "sam altman", "奥特曼"],
    "Anthropic": ["anthropic", "claude"],
    "Google": ["google", "deepmind", "gemini"],
    "Microsoft": ["microsoft", "微软", "azure", "github"],
    "NVIDIA": ["nvidia", "英伟达", "blackwell", "cuda", "黄仁勋"],
    "DeepSeek": ["deepseek"],
    "阿里": ["阿里", "阿里云", "通义", "qwen"],
    "腾讯": ["腾讯", "混元", "buddy ai"],
    "字节": ["字节", "豆包", "火山引擎"],
    "百度": ["百度", "文心"],
    "智谱": ["智谱", "glm", "zhipu"],
    "Kimi": ["kimi", "moonshot", "月之暗面"],
}

THEME_ALIASES = {
    "模型发布": ["发布", "推出", "上线", "升级", "release", "launch", "introducing", "开源"],
    "智能体": ["智能体", "agent", "agents", "agentic"],
    "具身智能与机器人": ["具身", "机器人", "robot", "robotics", "vla"],
    "AI 基础设施": ["gpu", "算力", "芯片", "云", "数据中心", "inference", "推理", "blackwell"],
    "AI 投融资": ["融资", "投资", "收购", "ipo", "估值", "funding", "acquisition"],
    "论文与研究": ["论文", "paper", "arxiv", "icml", "cvpr", "benchmark", "sota"],
    "AI 产品与应用": ["产品", "应用", "搜索", "办公", "coding", "编程", "视频", "音频"],
    "政策与安全": ["安全", "监管", "禁令", "政策", "白宫", "government", "ban"],
}

STOP_TERMS = {
    "https", "http", "com", "www", "html", "the", "and", "for", "with", "from",
    "this", "that", "into", "over", "than", "then", "also", "using", "based",
    "towards", "through", "learning", "training", "data", "models", "model",
    "agents", "agent", "llms", "llm", "without", "under", "between", "while",
    "where", "which", "their", "there", "these", "those", "一个", "我们", "他们",
    "消息", "正式", "近日", "获悉", "氪获悉", "显示", "进行", "提供", "支持",
}


@dataclass(slots=True)
class TopicCandidate:
    id: str
    title: str
    angle: str
    score: float
    themes: list[str]
    entities: list[str]
    source_count: int
    sources: list[str]
    publishable_images: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    structure: list[str] = field(default_factory=list)
    originality_notes: list[str] = field(default_factory=list)
    avoid_phrases: list[str] = field(default_factory=list)
    source_items: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def generate_topics(max_topics: int = DEFAULT_TOPIC_LIMIT, items: list[dict[str, Any]] | None = None, path: Path | None = None) -> list[TopicCandidate]:
    items = items if items is not None else load_items()
    clusters: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        clusters.setdefault(cluster_key(item), []).append(item)

    all_topics = [build_topic(key, cluster_items) for key, cluster_items in clusters.items()]
    all_topics = sorted(all_topics, key=lambda topic: topic.score, reverse=True)
    topics = select_topics_with_github_slot(all_topics, max_topics)
    topics = polish_topics_with_prompt(topics)
    topics = ensure_github_project_topic(topics, all_topics, max_topics)
    save_topics(topics, path)
    return topics


def save_topics(topics: list[TopicCandidate], path: Path | None = None) -> None:
    save_topic_records([topic.to_dict() for topic in topics], path)


def save_topic_records(topics: list[dict[str, Any]], path: Path | None = None) -> None:
    path = path or TOPICS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [normalize_topic_record(topic) for topic in topics if isinstance(topic, dict) and str(topic.get("title") or "").strip()]
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    if is_default_topics_path(path):
        sync_topics(payload)


def load_topics(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or TOPICS_PATH
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        return []
    return [normalize_topic_record(item) for item in data if isinstance(item, dict) and str(item.get("title") or "").strip()]


def add_manual_topic(payload: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    path = path or TOPICS_PATH
    topic = manual_topic_from_payload(payload)
    topics = load_topics(path)
    topics.insert(0, topic)
    save_topic_records(topics, path)
    return topic


def update_topic(topic_id: str, payload: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    path = path or TOPICS_PATH
    topics = load_topics(path)
    for index, topic in enumerate(topics):
        if str(topic.get("id") or "") != topic_id:
            continue
        updated = merge_topic_payload(topic, payload)
        topics[index] = updated
        save_topic_records(topics, path)
        return updated
    raise KeyError(topic_id)


def delete_topic(topic_id: str, path: Path | None = None) -> bool:
    path = path or TOPICS_PATH
    topics = load_topics(path)
    remaining = [topic for topic in topics if str(topic.get("id") or "") != topic_id]
    if len(remaining) == len(topics):
        return False
    save_topic_records(remaining, path)
    return True


def manual_topic_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    title = str(payload.get("title") or "").strip()
    if not title:
        raise ValueError("选题标题不能为空。")
    now = datetime.now(timezone.utc).isoformat()
    themes = normalize_tags(payload.get("themes")) or ["人工选题"]
    source_items = normalize_source_items(payload)
    topic = {
        "id": stable_topic_id(f"manual-{now}-{title}"),
        "title": title,
        "angle": str(payload.get("angle") or "").strip() or "按人工指定的选题方向生成文章，重点围绕事实、变化和影响展开。",
        "score": float(payload.get("score") or 0),
        "themes": themes,
        "entities": normalize_tags(payload.get("entities")),
        "source_count": len(source_items),
        "sources": unique([str(item.get("source_name") or "") for item in source_items]),
        "publishable_images": normalize_lines(payload.get("publishable_images")),
        "facts": normalize_lines(payload.get("facts")),
        "structure": normalize_lines(payload.get("structure")) or make_manual_structure(themes),
        "originality_notes": default_originality_notes(),
        "avoid_phrases": normalize_lines(payload.get("avoid_phrases")) or [title],
        "source_items": source_items,
        "manual": True,
        "created_at": now,
        "updated_at": now,
    }
    return normalize_topic_record(topic)


def merge_topic_payload(topic: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    updated = dict(topic)
    if "title" in payload:
        title = str(payload.get("title") or "").strip()
        if not title:
            raise ValueError("选题标题不能为空。")
        updated["title"] = title
    for key in ("angle",):
        if key in payload:
            updated[key] = str(payload.get(key) or "").strip()
    for key in ("themes", "entities"):
        if key in payload:
            updated[key] = normalize_tags(payload.get(key))
    for key in ("facts", "structure", "publishable_images", "avoid_phrases"):
        if key in payload:
            updated[key] = normalize_lines(payload.get(key))
    if "source_items" in payload or "source_url" in payload:
        source_items = normalize_source_items(payload)
        updated["source_items"] = source_items
        updated["source_count"] = len(source_items)
        updated["sources"] = unique([str(item.get("source_name") or "") for item in source_items])
    updated["manual_edited"] = True
    updated["updated_at"] = datetime.now(timezone.utc).isoformat()
    return normalize_topic_record(updated)


def normalize_topic_record(topic: dict[str, Any]) -> dict[str, Any]:
    record = dict(topic)
    title = str(record.get("title") or "").strip()
    record["id"] = str(record.get("id") or stable_topic_id(title)).strip()
    record["title"] = title
    record["angle"] = str(record.get("angle") or "").strip()
    record["score"] = safe_float(record.get("score"))
    record["themes"] = normalize_tags(record.get("themes")) or ["综合动态"]
    record["entities"] = normalize_tags(record.get("entities"))
    record["sources"] = normalize_tags(record.get("sources"))
    record["publishable_images"] = normalize_lines(record.get("publishable_images"))
    record["facts"] = normalize_lines(record.get("facts"))
    record["structure"] = normalize_lines(record.get("structure"))
    record["originality_notes"] = normalize_lines(record.get("originality_notes"))
    record["avoid_phrases"] = normalize_lines(record.get("avoid_phrases"))
    source_items = normalize_source_items({"source_items": record.get("source_items")})
    record["source_items"] = source_items
    record["source_count"] = safe_int(record.get("source_count"), len(source_items)) or len(source_items)
    record["created_at"] = str(record.get("created_at") or datetime.now(timezone.utc).isoformat())
    return record


def cluster_key(item: dict[str, Any]) -> str:
    if is_github_project_item(item):
        url = str(item.get("url") or "").rstrip("/")
        title = str(item.get("title") or "").strip().lower()
        repo_key = url.lower() or title
        return f"github_project::{repo_key}"
    text = item_text(item)
    entities = detect_aliases(text, ENTITY_ALIASES)
    themes = detect_aliases(text, THEME_ALIASES)
    if entities and themes:
        return f"{entities[0]}::{themes[0]}"
    if entities:
        return f"{entities[0]}::综合动态"
    if themes:
        return f"行业::{themes[0]}"
    keywords = extract_keywords(text, limit=2)
    return "关键词::" + "-".join(keywords or ["其他"])


def build_topic(key: str, items: list[dict[str, Any]]) -> TopicCandidate:
    ranked = sorted(items, key=lambda item: item.get("score", 0), reverse=True)
    text = " ".join(item_text(item) for item in ranked)
    entities = detect_aliases(text, ENTITY_ALIASES)
    themes = detect_aliases(text, THEME_ALIASES)
    keywords = extract_keywords(text, limit=6)
    sources = sorted({item.get("source_name", "") for item in ranked if item.get("source_name")})
    facts = build_facts(ranked[:8])
    images = []
    for item in ranked:
        if item.get("image_usage") == "publishable_candidate":
            images.extend(item.get("local_images") or item.get("images") or [])

    topic = TopicCandidate(
        id=stable_topic_id(key),
        title=seed_topic_title(entities, themes, keywords, ranked),
        angle=seed_topic_angle(entities, themes, ranked),
        score=round(topic_score(ranked, entities, themes), 2),
        themes=themes or ["综合动态"],
        entities=entities,
        source_count=len(sources),
        sources=sources,
        publishable_images=unique(images)[:3],
        facts=facts,
        structure=make_structure(themes),
        originality_notes=[
            "只使用事实点、时间、主体和影响判断，不复用原文句式。",
            "正文采用新的叙事角度：先讲变化，再讲原因，最后讲影响。",
            "引用来源时用来源名和链接，不整段搬运原文。",
            "媒体/自媒体图片只做内部预览，不作为发布图片。",
        ],
        avoid_phrases=build_avoid_phrases(ranked[:8]),
        source_items=[
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "source_name": item.get("source_name"),
                "published_at": item.get("published_at"),
                "score": item.get("score"),
            }
            for item in ranked[:8]
        ],
    )
    if any(is_github_project_item(item) for item in ranked):
        topic = make_github_project_topic(topic, ranked)
    return topic


def is_github_project_item(item: dict[str, Any]) -> bool:
    source_id = str(item.get("source_id") or "").lower()
    url = str(item.get("url") or "").lower()
    source_name = str(item.get("source_name") or "").lower()
    return source_id.startswith("github") or "github.com/" in url or "github" in source_name


def topic_has_github_project(topic: TopicCandidate) -> bool:
    return any(is_github_project_item(item) for item in topic.source_items)


def github_repo_name(item: dict[str, Any]) -> str:
    title = str(item.get("title") or "").strip()
    if "/" in title:
        return title
    url = str(item.get("url") or "").rstrip("/")
    parts = [part for part in url.split("/") if part]
    if len(parts) >= 2 and "github.com" in url.lower():
        return "/".join(parts[-2:])
    return title or "this GitHub project"


def make_github_project_topic(topic: TopicCandidate, items: list[dict[str, Any]]) -> TopicCandidate:
    project = next((item for item in items if is_github_project_item(item)), items[0] if items else {})
    repo = github_repo_name(project)
    title = clean_topic_title(f"GitHub\u9879\u76ee\u63a8\u8350\uff1a{repo}")
    summary = clean_topic_angle(str(project.get("summary") or first_source_title(topic)))
    angle = (
        f"\u4ee5 {repo} \u4f5c\u4e3a\u672c\u6b21 GitHub \u9879\u76ee\u63a8\u8350\uff1a"
        "\u5148\u8bb2\u5b83\u60f3\u89e3\u51b3\u7684\u5177\u4f53\u95ee\u9898\uff0c"
        "\u518d\u8bb2\u54ea\u7c7b\u4eba\u6700\u503c\u5f97\u8bd5\uff0c"
        "\u6700\u540e\u5224\u65ad\u5b83\u662f\u771f\u80fd\u6539\u8fdb\u5de5\u4f5c\u6d41\uff0c"
        "\u8fd8\u662f\u66f4\u50cf\u4e00\u4e2a\u6f14\u793a\u9879\u76ee\u3002"
    )
    if summary:
        angle = f"{angle}\u9879\u76ee\u63cf\u8ff0\u91cc\u7684\u5173\u952e\u7ebf\u7d22\uff1a{summary[:180]}"
    structure = [
        "\u5f00\u5934\uff1a\u5148\u8bb2\u8fd9\u4e2a\u9879\u76ee\u8981\u89e3\u51b3\u7684\u5177\u4f53\u75db\u70b9\uff0c\u4e0d\u8981\u53ea\u8bf4\u6280\u672f\u5f88\u65b0\u3002",
        "\u89e3\u91ca\u5c42\uff1a\u7528\u767d\u8bdd\u8bb2\u6e05\u5b83\u7684\u7528\u6cd5\u3001\u76ee\u6807\u7528\u6237\u548c\u4e3a\u4ec0\u4e48\u88ab\u5173\u6ce8\u3002",
        "\u68c0\u67e5\u5c42\uff1a\u5199 2-3 \u4e2a\u8bd5\u7528\u524d\u8981\u770b\u7684\u70b9\uff0c\u6bd4\u5982\u6210\u719f\u5ea6\u3001\u6587\u6863\u3001\u66f4\u65b0\u548c\u98ce\u9669\u3002",
        "\u7ed3\u5c3e\uff1a\u7ed9\u51fa\u660e\u786e\u5efa\u8bae\uff0c\u662f\u503c\u5f97\u5173\u6ce8\u3001\u503c\u5f97\u8bd5\u7528\uff0c\u8fd8\u662f\u5148\u89c2\u671b\u3002",
    ]
    return replace_topic(topic, title=title, angle=angle, structure=structure)


def ensure_github_project_topic(
    topics: list[TopicCandidate],
    candidates: list[TopicCandidate],
    max_topics: int,
) -> list[TopicCandidate]:
    if max_topics <= 0 or not candidates:
        return topics[:max_topics]
    if any(topic_has_github_project(topic) for topic in topics):
        return topics[:max_topics]
    github_topic = next((topic for topic in candidates if topic_has_github_project(topic)), None)
    if not github_topic:
        return topics[:max_topics]
    selected = [topic for topic in topics if topic.id != github_topic.id]
    if len(selected) >= max_topics:
        selected = selected[: max_topics - 1]
    selected.append(github_topic)
    return selected[:max_topics]


def select_topics_with_github_slot(
    candidates: list[TopicCandidate],
    max_topics: int,
    github_slots: int = GITHUB_PROJECT_TOPIC_SLOTS,
) -> list[TopicCandidate]:
    if max_topics <= 0:
        return []
    if github_slots <= 0:
        return candidates[:max_topics]

    github_topics = [topic for topic in candidates if topic_has_github_project(topic)]
    if not github_topics:
        return candidates[:max_topics]

    reserved = min(github_slots, max_topics, len(github_topics))
    general_limit = max_topics - reserved
    selected = [topic for topic in candidates if not topic_has_github_project(topic)][:general_limit]
    selected_ids = {topic.id for topic in selected}
    for topic in github_topics[:reserved]:
        if topic.id not in selected_ids:
            selected.append(topic)
            selected_ids.add(topic.id)

    if len(selected) < max_topics:
        for topic in candidates:
            if topic.id in selected_ids:
                continue
            selected.append(topic)
            selected_ids.add(topic.id)
            if len(selected) >= max_topics:
                break
    return selected[:max_topics]


def ensure_github_project_style(topic: TopicCandidate) -> TopicCandidate:
    if not topic_has_github_project(topic):
        return topic
    repo = github_repo_name(topic.source_items[0] if topic.source_items else {})
    combined = f"{topic.title} {topic.angle}".lower()
    if "github" in combined and repo.lower() in combined:
        return topic
    return make_github_project_topic(topic, topic.source_items)


def polish_topics_with_prompt(topics: list[TopicCandidate]) -> list[TopicCandidate]:
    if not topics:
        return topics
    try:
        prompt = load_topic_prompt()
        payload = {"topics": [topic_prompt_payload(topic) for topic in topics]}
        source_json = json.dumps(payload, ensure_ascii=False, indent=2)
        messages = [
            {"role": "system", "content": prompt["system"]},
            {"role": "user", "content": prompt["user"].replace("{{TOPICS_JSON}}", source_json)},
        ]
        content = chat_completion(messages, temperature=0.78)
        polished = parse_topic_editor_output(content)
    except Exception:  # noqa: BLE001
        polished = []
    if not polished:
        return [ensure_github_project_style(fallback_topic_editor(topic)) for topic in topics]

    by_id = {str(item.get("id") or ""): item for item in polished if isinstance(item, dict)}
    result: list[TopicCandidate] = []
    for topic in topics:
        edited = by_id.get(topic.id)
        if edited:
            result.append(apply_topic_editor_result(topic, edited))
        else:
            result.append(fallback_topic_editor(topic))
    return [ensure_github_project_style(topic) for topic in result]


def topic_prompt_payload(topic: TopicCandidate) -> dict[str, Any]:
    return {
        "id": topic.id,
        "current_title": topic.title,
        "current_angle": topic.angle,
        "themes": topic.themes,
        "entities": topic.entities,
        "facts": topic.facts[:6],
        "sources": topic.source_items[:6],
    }


def load_topic_prompt(path: Path = TOPIC_PROMPT_PATH) -> dict[str, str]:
    return load_prompt_json(path)


def parse_topic_editor_output(content: str) -> list[dict[str, Any]]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, dict):
        raw_topics = parsed.get("topics")
    else:
        raw_topics = parsed
    if not isinstance(raw_topics, list):
        return []
    return [item for item in raw_topics if isinstance(item, dict)]


def apply_topic_editor_result(topic: TopicCandidate, edited: dict[str, Any]) -> TopicCandidate:
    title = clean_topic_title(str(edited.get("title") or ""))
    angle = clean_topic_angle(str(edited.get("angle") or ""))
    structure = normalize_lines(edited.get("structure")) or topic.structure
    if not valid_editorial_title(title) or has_unsupported_claims(title, topic):
        title = fallback_topic_title(topic)
    if not valid_editorial_angle(angle) or has_unsupported_claims(angle, topic):
        angle = fallback_topic_angle(topic)
    if any(has_unsupported_claims(step, topic) for step in structure):
        structure = fallback_structure(topic)
    return replace_topic(topic, title=title, angle=angle, structure=structure)


def fallback_topic_editor(topic: TopicCandidate) -> TopicCandidate:
    return replace_topic(topic, title=fallback_topic_title(topic), angle=fallback_topic_angle(topic), structure=fallback_structure(topic))


def replace_topic(topic: TopicCandidate, *, title: str, angle: str, structure: list[str]) -> TopicCandidate:
    return TopicCandidate(
        id=topic.id,
        title=title,
        angle=angle,
        score=topic.score,
        themes=topic.themes,
        entities=topic.entities,
        source_count=topic.source_count,
        sources=topic.sources,
        publishable_images=topic.publishable_images,
        facts=topic.facts,
        structure=structure,
        originality_notes=topic.originality_notes,
        avoid_phrases=topic.avoid_phrases,
        source_items=topic.source_items,
        created_at=topic.created_at,
    )


def clean_topic_title(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip(" \t\r\n\"'“”")


def clean_topic_angle(angle: str) -> str:
    return re.sub(r"\s+", " ", angle).strip()


def valid_editorial_title(title: str) -> bool:
    if not title:
        return False
    zh_count = len(re.findall(r"[\u4e00-\u9fff]", title))
    if zh_count > 32 or len(title) > 72:
        return False
    lowered = title.lower()
    return not any(phrase.lower() in lowered for phrase in FORBIDDEN_TOPIC_PHRASES)


def valid_editorial_angle(angle: str) -> bool:
    if len(angle) < 18:
        return False
    lowered = angle.lower()
    return not any(phrase.lower() in lowered for phrase in FORBIDDEN_TOPIC_PHRASES)


def has_unsupported_claims(text: str, topic: TopicCandidate) -> bool:
    if not text:
        return False
    source_text = topic_source_text(topic).lower()
    lowered = text.lower()
    for term in RISKY_UNSUPPORTED_TERMS:
        term_lower = term.lower()
        if term_lower in lowered and term_lower not in source_text:
            return True
    return False


def topic_source_text(topic: TopicCandidate) -> str:
    source_titles = [str(item.get("title") or "") for item in topic.source_items]
    source_names = [str(item.get("source_name") or "") for item in topic.source_items]
    return " ".join([topic.title, topic.angle, *topic.facts, *source_titles, *source_names])


def fallback_topic_title(topic: TopicCandidate) -> str:
    first_title = first_source_title(topic)
    subject = topic.entities[0] if topic.entities else extract_subject_from_title(first_title)
    hook = choose_life_hook(topic)
    if subject and hook:
        title = f"{subject}{hook}"
    elif first_title:
        title = compress_title(first_title).rstrip(".")
    else:
        title = "这件事为什么会吵起来"
    title = clean_topic_title(title)
    if not valid_editorial_title(title):
        title = remove_forbidden_phrases(title) or "这件事为什么会吵起来"
    return title[:72]


def fallback_topic_angle(topic: TopicCandidate) -> str:
    first_title = first_source_title(topic)
    subject = topic.entities[0] if topic.entities else extract_subject_from_title(first_title)
    subject = subject or "这件事"
    hook = choose_life_focus(topic)
    angle = f"不要写成行业通稿，可先把{subject}和年轻人、普通人能感知到的{hook}连起来，但不要只写青年处境：还要把企业动作、平台规则或政策口径放进来对照，分析生活里的哪一个难处会被放大。"
    return clean_topic_angle(remove_forbidden_phrases(angle))


def fallback_structure(topic: TopicCandidate) -> list[str]:
    focus = choose_life_focus(topic)
    return [
        f"开头：用找工作、上班、租房、买车、通勤或消费里的真实场景切入，说明这件事和{focus}有关。",
        "事实层：只挑 3-4 个关键事实，按时间和动作讲清楚，不堆来源名。",
        "联系层：把人的处境和企业动作、平台规则或政策口径放在一起分析，写清楚矛盾在哪里。",
        "难处层：写清楚年轻人、普通用户、玩家、创作者或从业者为什么会支持或反感。",
        "判断层：给出一个明确但不过度夸张的观点，引导读者讨论这件事会不会让现实压力更明显。",
    ]


def choose_life_hook(topic: TopicCandidate) -> str:
    text = " ".join([topic.title, topic.angle, *topic.facts, *[str(item.get("title") or "") for item in topic.source_items]])
    lowered = text.lower()
    if any(word in lowered for word in ["game", "steam", "xbox", "playstation", "nintendo", "switch", "玩家", "游戏", "手游", "电竞"]):
        return "这次让玩家不淡定了"
    if any(word in lowered for word in ["job", "career", "hire", "hiring", "interview", "resume", "layoff", "salary", "找工作", "就业", "求职", "招聘", "面试", "简历", "应届", "实习", "岗位", "职场", "跳槽", "裁员", "薪资"]):
        return "会不会让年轻人找工作更难"
    if any(word in lowered for word in ["rent", "commute", "housing", "房租", "租房", "通勤", "地铁", "城市", "落户", "社保", "公积金", "居住证"]):
        return "会不会让租房通勤更吃力"
    if any(word in lowered for word in ["car", "vehicle", "automotive", "automaker", "ev ", "汽车", "新能源车", "买车", "车贷", "智驾", "自动驾驶", "油耗", "保费"]):
        return "会不会让年轻人买车更纠结"
    if any(word in lowered for word in ["price", "价格", "涨价", "订阅", "会员", "收费"]):
        return "会不会变贵，才是大家最关心的"
    if any(word in lowered for word in ["服务", "办事", "政务", "医保", "社保", "公积金", "补贴", "资格", "申请"]):
        return "会不会影响年轻人办事和生活成本"
    if any(word in lowered for word in ["安全", "隐私", "监管", "禁令", "政策", "法院"]):
        return "真正影响的是大家敢不敢用"
    if any(word in lowered for word in ["手机", "电脑", "硬件", "芯片", "gpu", "nvidia", "huawei"]):
        return "最后可能体现在手机电脑这些大件消费上"
    if any(word in lowered for word in ["agent", "办公", "搜索", "copilot", "助手", "应用"]):
        return "会先改变一部分人的工作习惯"
    return "为什么值得普通人多看一眼"


def choose_life_focus(topic: TopicCandidate) -> str:
    hook = choose_life_hook(topic)
    if "玩家" in hook:
        return "玩家体验、付费预期和社区情绪"
    if "找工作" in hook:
        return "招聘门槛、岗位要求和薪资预期"
    if "租房通勤" in hook:
        return "房租、通勤和城市生活成本"
    if "买车" in hook:
        return "买车预算、通勤选择和用车成本"
    if "变贵" in hook:
        return "价格、订阅、车贷和日常消费预算"
    if "办事" in hook:
        return "办事流程、补贴资格和生活成本"
    if "敢不敢用" in hook:
        return "隐私、安全和信任"
    if "大件消费" in hook:
        return "手机、电脑这些大件消费和换机预算"
    if "工作习惯" in hook:
        return "搜索、写作、办公这些日常动作和学习成本"
    return "年轻人的工作、消费和生活压力"


def first_source_title(topic: TopicCandidate) -> str:
    for item in topic.source_items:
        title = str(item.get("title") or "").strip()
        if title:
            return title
    return topic.facts[0] if topic.facts else ""


def extract_subject_from_title(title: str) -> str:
    tokens = extract_keywords(title, limit=3)
    if not tokens:
        return ""
    preferred = [token for token in tokens if not token.isascii()]
    return preferred[0] if preferred else tokens[0]


def remove_forbidden_phrases(text: str) -> str:
    cleaned = text
    for phrase in FORBIDDEN_TOPIC_PHRASES:
        cleaned = cleaned.replace(phrase, "")
    return re.sub(r"\s+", " ", cleaned).strip(" ，,。")


def item_text(item: dict[str, Any]) -> str:
    return f"{item.get('title', '')} {item.get('summary', '')}"


def detect_aliases(text: str, aliases: dict[str, list[str]]) -> list[str]:
    lowered = text.lower()
    return [label for label, terms in aliases.items() if any(term.lower() in lowered for term in terms)]


def extract_keywords(text: str, limit: int = 8) -> list[str]:
    counts: dict[str, int] = {}
    for token in TOKEN_RE.findall(text.lower()):
        token = token.strip()
        if token in STOP_TERMS or len(token) < 2:
            continue
        if token.isascii() and len(token) <= 2:
            continue
        if re.fullmatch(r"[a-z]+\d+|\d+[a-z]+", token):
            continue
        counts[token] = counts.get(token, 0) + 1
    return [token for token, _ in sorted(counts.items(), key=lambda pair: pair[1], reverse=True)[:limit]]


def topic_score(items: list[dict[str, Any]], entities: list[str], themes: list[str]) -> float:
    base = sum(float(item.get("score", 0)) for item in items[:5])
    source_bonus = min(15, 3 * len({item.get("source_id") for item in items}))
    entity_bonus = min(12, 3 * len(entities))
    theme_bonus = min(10, 2 * len(themes))
    return base + source_bonus + entity_bonus + theme_bonus


def seed_topic_title(entities: list[str], themes: list[str], keywords: list[str], items: list[dict[str, Any]]) -> str:
    first_title = str(items[0].get("title") or "") if items else ""
    subject = entities[0] if entities else (keywords[0] if keywords else extract_subject_from_title(first_title))
    if not subject:
        return compress_title(first_title) or "这件事为什么会吵起来"
    pseudo_topic = TopicCandidate(
        id="seed",
        title=first_title,
        angle="",
        score=0,
        themes=themes,
        entities=entities,
        source_count=0,
        sources=[],
        facts=[first_title] if first_title else [],
        source_items=[{"title": first_title}],
    )
    return clean_topic_title(f"{subject}{choose_life_hook(pseudo_topic)}")


def seed_topic_angle(entities: list[str], themes: list[str], items: list[dict[str, Any]]) -> str:
    first_title = str(items[0].get("title") or "") if items else ""
    subject = entities[0] if entities else extract_subject_from_title(first_title) or "这件事"
    pseudo_topic = TopicCandidate(
        id="seed",
        title=first_title,
        angle="",
        score=0,
        themes=themes,
        entities=entities,
        source_count=0,
        sources=[],
        facts=[first_title] if first_title else [],
        source_items=[{"title": first_title}],
    )
    focus = choose_life_focus(pseudo_topic)
    return f"从{focus}切入，写清楚{subject}为什么会被讨论、谁会赞成、谁会反感，以及接下来最值得观察的一个变化。"


def build_facts(items: list[dict[str, Any]]) -> list[str]:
    facts = []
    for item in items:
        date = str(item.get("published_at", ""))[:10]
        source = item.get("source_name", "来源")
        title = compress_title(str(item.get("title", "")))
        facts.append(f"{date}，{source} 提到：{title}")
    return facts


def make_structure(themes: list[str]) -> list[str]:
    theme = themes[0] if themes else "这一轮变化"
    return [
        f"开头：用一个判断句点出{theme}的变化，不复述新闻标题。",
        "事实层：列出 3-5 个关键动作，注明来源和时间。",
        "解释层：分析为什么这些动作集中出现。",
        "影响层：落到找工作、上班、租房、买车、消费或办事中的一两个具体难处，并和企业政策或官方表述形成对照。",
        "结尾：给出可验证的观察指标，而不是夸张预测。",
    ]


def build_avoid_phrases(items: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("title", ""))[:80] for item in items if item.get("title")][:8]


def normalize_lines(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = value.replace("；", "\n").splitlines()
    elif isinstance(value, list):
        raw = [str(item) for item in value]
    else:
        raw = []
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = " ".join(str(item).strip().split())
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def normalize_tags(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = value.replace("，", ",").replace("、", ",").replace("\n", ",").split(",")
    elif isinstance(value, list):
        raw = [str(item) for item in value]
    else:
        raw = []
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = " ".join(str(item).strip().split())
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def normalize_source_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("source_items")
    records: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                record = normalize_source_item(item)
                if record:
                    records.append(record)
            elif str(item).strip():
                record = source_item_from_line(str(item))
                if record:
                    records.append(record)
    elif isinstance(raw, str):
        for line in normalize_lines(raw):
            record = source_item_from_line(line)
            if record:
                records.append(record)

    direct = normalize_source_item(
        {
            "title": payload.get("source_title"),
            "url": payload.get("source_url"),
            "source_name": payload.get("source_name"),
            "published_at": payload.get("published_at"),
        }
    )
    if direct:
        records.append(direct)
    return dedupe_source_items(records)


def normalize_source_item(item: dict[str, Any]) -> dict[str, Any]:
    title = str(item.get("title") or "").strip()
    url = str(item.get("url") or "").strip()
    source_name = str(item.get("source_name") or item.get("source") or "").strip()
    published_at = str(item.get("published_at") or "").strip()
    if not any([title, url, source_name]):
        return {}
    return {
        "title": title or url or source_name,
        "url": url,
        "source_name": source_name or "人工来源",
        "published_at": published_at,
        "score": safe_float(item.get("score")),
    }


def source_item_from_line(line: str) -> dict[str, Any]:
    parts = [part.strip() for part in line.split("|") if part.strip()]
    if len(parts) >= 3:
        return normalize_source_item({"source_name": parts[0], "title": parts[1], "url": parts[2]})
    if len(parts) == 2:
        if looks_like_url(parts[0]):
            return normalize_source_item({"url": parts[0], "title": parts[1]})
        return normalize_source_item({"title": parts[0], "url": parts[1]})
    if looks_like_url(line):
        return normalize_source_item({"url": line, "title": line})
    return normalize_source_item({"title": line})


def dedupe_source_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        key = (item.get("url") or item.get("title") or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def looks_like_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def make_manual_structure(themes: list[str]) -> list[str]:
    if themes:
        return make_structure(themes)
    return [
        "开头：直接点出这个选题为什么值得写。",
        "事实层：列出 3-5 个关键事实、时间和主体。",
        "解释层：分析这些事实之间的关系和变化原因。",
        "影响层：写清楚对找工作、上班、租房、买车、消费或办事的具体影响，并和企业政策或官方表述形成对照。",
        "结尾：给出后续可观察的指标。",
    ]


def default_originality_notes() -> list[str]:
    return [
        "只使用事实点、时间、主体和影响判断，不复用原文句式。",
        "正文采用新的叙事角度：先讲变化，再讲原因，最后讲影响。",
        "引用来源时用来源名和链接，不整段搬运原文。",
        "媒体/自媒体图片只做内部预览，不作为发布图片。",
    ]


def is_default_topics_path(path: Path) -> bool:
    try:
        return path.resolve() == TOPICS_PATH.resolve()
    except OSError:
        return path == TOPICS_PATH


def compress_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    return title[:90] + ("..." if len(title) > 90 else "")


def stable_topic_id(key: str) -> str:
    return re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", key).strip("-")[:80]


def unique(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
