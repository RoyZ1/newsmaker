from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import ROOT_DIR
from app.database import sync_drafts
from app.formatting import enrich_drafts_layout
from app.llm_client import chat_completion
from app.opinion_materials import load_opinion_items
from app.prompting import load_prompt_json
from app.title_format import strip_title_prefix
from app.topics import load_topics


DRAFTS_PATH = ROOT_DIR / "data" / "drafts.json"
ARTICLE_PROMPT_PATH = ROOT_DIR / "config" / "prompts" / "article_writer.json"
REFERENCE_STYLE_PATH = ROOT_DIR / "config" / "prompts" / "reference_style.md"
HUMANIZER_RULES_PATH = ROOT_DIR / "config" / "prompts" / "humanizer_zh.md"
TITLE_RULES_PATH = ROOT_DIR / "config" / "prompts" / "title_rules.md"


@dataclass(slots=True)
class ArticleDraft:
    topic_id: str
    title: str
    subtitle: str
    body_markdown: str
    source_links: list[dict[str, Any]]
    originality_checklist: list[str]
    image_candidates: list[str] = field(default_factory=list)
    image_plan: list[dict[str, str]] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def generate_article_drafts(max_drafts: int = 3, topics: list[dict[str, Any]] | None = None) -> list[ArticleDraft]:
    source_topics = topics if topics is not None else load_topics()
    if not source_topics:
        raise RuntimeError("No topics found. Run python scripts/collect/run_topics.py first.")
    drafts = [generate_article_draft(topic) for topic in source_topics[:max_drafts]]
    save_drafts(drafts)
    return drafts


def generate_article_draft(topic: dict[str, Any]) -> ArticleDraft:
    messages = build_messages(topic)
    content = chat_completion(messages, temperature=0.72)
    parsed = parse_model_json(content)
    for _ in range(3):
        issues = validate_article_output(parsed, topic)
        if not issues:
            break
        repair_messages = build_repair_messages(messages, content, issues)
        content = chat_completion(repair_messages, temperature=0.52)
        repaired = parse_model_json(content)
        if repaired:
            parsed = repaired
    image_plan = normalize_image_plan(parsed.get("image_plan") if isinstance(parsed.get("image_plan"), list) else [])
    if not image_plan:
        image_plan = default_image_plan(topic)
    return ArticleDraft(
        topic_id=topic.get("id", ""),
        title=parsed.get("title") or topic.get("title", ""),
        subtitle=parsed.get("subtitle") or "",
        body_markdown=parsed.get("body_markdown") or content,
        source_links=topic.get("source_items", []),
        image_candidates=topic.get("publishable_images", []),
        image_plan=image_plan,
        originality_checklist=[
            "正文不显式写“据某来源报道”，来源只放在后台链接区",
            "每个小标题下持续讲清一个问题，不做短碎新闻摘要",
            "没有复制原文标题作为正文段落",
            "没有连续复用来源文章中的长句",
            "媒体/自媒体图片未作为发布图",
        ],
    )


def generate_article_draft_dict(topic: dict[str, Any]) -> dict[str, Any]:
    return generate_article_draft(topic).to_dict()


def build_messages(topic: dict[str, Any]) -> list[dict[str, str]]:
    source_brief = {
        "topic_title": topic.get("title"),
        "angle": topic.get("angle"),
        "themes": topic.get("themes", []),
        "entities": topic.get("entities", []),
        "facts": topic.get("facts", []),
        "avoid_phrases": topic.get("avoid_phrases", []),
        "sources_for_internal_reference_only": topic.get("source_items", []),
        "publishable_images": topic.get("publishable_images", []),
        "public_reaction_materials": select_opinion_materials(topic),
    }
    prompt = load_article_prompt()
    reference_style = load_reference_style()
    humanizer_rules = load_humanizer_rules()
    title_rules = load_title_rules()
    source_json = json.dumps(source_brief, ensure_ascii=False, indent=2)
    user = (
        prompt["user"]
        .replace("{{REFERENCE_STYLE}}", reference_style)
        .replace("{{HUMANIZER_RULES}}", humanizer_rules)
        .replace("{{TITLE_RULES}}", title_rules)
        .replace("{{SOURCE_BRIEF_JSON}}", source_json)
    )
    return [{"role": "system", "content": prompt["system"]}, {"role": "user", "content": user}]


def build_repair_messages(
    original_messages: list[dict[str, str]],
    previous_content: str,
    issues: list[str],
) -> list[dict[str, str]]:
    return [
        *original_messages,
        {"role": "assistant", "content": previous_content},
        {
            "role": "user",
            "content": (
                "这版不合格，请只输出修正后的 JSON，不要解释。\n"
                f"需要修正的问题：{'; '.join(issues)}\n"
                    "硬性要求：全文目标 700-800 个中文字符，允许 650-900 个中文字符；写 2-3 个 ## 小标题；"
                    "最后一个小标题写完后只允许 1-2 段结尾，不能继续发散；"
                "保留一个清楚主线，最多使用 2-4 个事实；"
                "必须包含 2-4 处 **加粗** 和 1-2 处 <red>红色强调</red>；"
                "不要写来源、参考链接、资料来源；"
                "不要使用“此外、值得注意的是、综上、标志着、赋能、释放重要信号”等 AI 味套话；"
                "只有在影响普通人且来自国内大企业或政府政策/公共规则时，才可用贴近生活的反问结尾；"
                "青年难处只是出发点，不是唯一出发点；要把找工作、上班、租房、买车等具体难处和企业动作、平台规则或政策口径放在一起分析；"
                "从人物/政策/国际竞争转到学生或普通人时，要先写身份站位和真实担心，再用一句自然过渡接到具体生活动作；段落长短要有变化，避免每段都像三句拼接；"
                "不要写固定三段式总结，要像一个真实学生围绕一个问题追问。"
            ),
        },
    ]


def validate_article_output(parsed: dict[str, Any], topic: dict[str, Any] | None = None) -> list[str]:
    issues: list[str] = []
    title = str(parsed.get("title") or "").strip()
    body = str(parsed.get("body_markdown") or "")
    zh_count = len(re.findall(r"[\u4e00-\u9fff]", body))
    heading_count = len(re.findall(r"^#{1,4}\s+", body, flags=re.M))
    bold_count = len(re.findall(r"\*\*.+?\*\*", body))
    red_count = len(re.findall(r"<red>.+?</red>|==.+?==", body))

    if not title:
        issues.append("缺少标题")
    title_issues = validate_title_quality(title, topic)
    issues.extend(title_issues)
    if zh_count < 620:
        issues.append(f"正文太短，只有约 {zh_count} 个中文字符")
    if zh_count > 980:
        issues.append(f"正文太长，约 {zh_count} 个中文字符")
    if heading_count < 2 or heading_count > 3:
        issues.append(f"小标题数量应为 2-3 个，现在是 {heading_count} 个")
    if bold_count < 2:
        issues.append("加粗重点不足")
    if bold_count > 6:
        issues.append("加粗太多，像机械强调")
    if red_count < 1:
        issues.append("缺少红色重点")
    if re.search(r"(据.+报道|.+提到|资料显示|来源|相关链接|参考链接|见文末)", body):
        issues.append("正文出现来源提示或链接提示")
    ai_tone_patterns = (
        "此外",
        "值得注意的是",
        "综上",
        "总的来说",
        "标志着",
        "彰显",
        "凸显",
        "赋能",
        "释放重要信号",
        "深刻改变",
        "关键作用",
        "至关重要",
        "全新格局",
        "不断演变",
        "不仅仅是",
        "不仅是",
    )
    hit_patterns = [pattern for pattern in ai_tone_patterns if pattern in body]
    if hit_patterns:
        issues.append(f"正文有明显 AI 味套话：{', '.join(hit_patterns[:5])}")
    return issues


def validate_title_quality(title: str, topic: dict[str, Any] | None = None) -> list[str]:
    issues: list[str] = []
    cleaned = re.sub(r"\s+", "", strip_title_prefix(title))
    generic_phrases = (
        "释放哪些信号",
        "正在如何变化",
        "几点观察",
        "新赛道",
        "新趋势",
        "深度解析",
        "一文看懂",
        "全面解读",
        "行业观察",
        "行业的",
    )
    ai_title_phrases = (
        "AI交付标准变了",
        "交付标准变了",
        "胜负手变了",
        "暗战开始了",
        "生态正在重构",
        "重新定义边界",
        "重新定义",
        "入口变了",
        "边界变了",
        "新资产",
        "新阵地",
        "新阶段",
        "释放重要信号",
        "深刻改变",
        "正在重塑",
        "正在改写",
    )
    if any(phrase in cleaned for phrase in generic_phrases):
        issues.append("标题太模板化，缺少吸引力")
    if any(phrase in cleaned for phrase in ai_title_phrases):
        issues.append("标题有明显 AI 总结腔：要改成具体对象 + 具体动作/冲突/数字")
    if cleaned.endswith(("变了", "来了", "开始了", "重构了", "改写了")) and len(cleaned) <= 18:
        issues.append("标题结尾太空：不要只写“变了/来了/开始了”，要补具体变化")
    zh_count = len(re.findall(r"[\u4e00-\u9fff]", cleaned))
    if zh_count > 28:
        issues.append(f"标题太长，约 {zh_count} 个中文字符")
    hook_patterns = (
        r"[？?]",
        r"不是.+是",
        r"不靠|不卷|不只是|不只|不再|却|反而|突然|偷偷|只有|终于|开始|抢|逼|摸到|跑进|砍掉|翻车|失效|卖给|盯上|跑到|跑出|换打法|没那么好混|考卷|成绩|硬数字|参数党",
        r"\d|万|亿|秒级|第一|SOTA|开源之王",
    )
    if cleaned and not any(re.search(pattern, cleaned, flags=re.I) for pattern in hook_patterns):
        issues.append("标题缺少钩子：需要疑问、冲突、反常识、数字结果或强判断")
    if cleaned and not title_has_search_keyword(cleaned, topic):
        issues.append("标题缺少明显搜索关键词：需要公司名、模型名、产品名、技术名或场景词")
    return issues


def title_has_search_keyword(title: str, topic: dict[str, Any] | None = None) -> bool:
    title = strip_title_prefix(title)
    keyword_patterns = (
        r"OpenAI|ChatGPT|GPT|Anthropic|Claude|Google|Gemini|DeepMind|Microsoft|微软|Copilot|Azure",
        r"NVIDIA|英伟达|华为|昇腾|盘古|鸿蒙|HarmonyOS|阿里|通义|Qwen|腾讯|混元|字节|豆包|百度|文心|DeepSeek|智谱|GLM|Kimi",
        r"Agent|智能体|RAG|世界模型|具身|机器人|AI搜索|输入法|算力|芯片|GPU|半导体|自动驾驶|智驾|AI\s?PC",
        r"CVPR|ICML|NeurIPS|ACL|SOTA|Benchmark|开源|论文|模型|大模型",
        r"Bedrock|SageMaker|AWS|云|API|推理|多模态|语音|视频|搜索",
    )
    if any(re.search(pattern, title, flags=re.I) for pattern in keyword_patterns):
        return True
    if not topic:
        return False

    candidates: list[str] = []
    for key in ("entities", "themes"):
        values = topic.get(key)
        if isinstance(values, list):
            candidates.extend(str(value) for value in values if str(value).strip())
    source_items = topic.get("source_items")
    if isinstance(source_items, list):
        for item in source_items:
            if not isinstance(item, dict):
                continue
            for key in ("title", "source_name"):
                value = str(item.get(key) or "").strip()
                if value:
                    candidates.append(value)
    title_lower = title.lower()
    for keyword in candidates:
        normalized = re.sub(r"\s+", "", keyword)
        if len(normalized) >= 2 and normalized.lower() in title_lower:
            return True
    return False


def select_opinion_materials(topic: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    keywords = opinion_match_keywords(topic)
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in load_opinion_items():
        if opinion_matches_draft_ref(item, topic):
            payload = opinion_item_payload(item)
            selected.append(payload)
            seen.add(str(item.get("id") or payload.get("text") or ""))
            if len(selected) >= limit:
                return selected

    if not keywords:
        return selected

    for item in load_opinion_items():
        item_key = str(item.get("id") or item.get("text") or "")
        if item_key in seen:
            continue
        text = str(item.get("text") or "")
        topic_text = str(item.get("topic") or "")
        haystack = f"{topic_text} {text}".lower()
        if not any(keyword.lower() in haystack for keyword in keywords):
            continue
        selected.append(opinion_item_payload(item))
        seen.add(item_key)
        if len(selected) >= limit:
            break
    return selected


def opinion_matches_draft_ref(item: dict[str, Any], topic: dict[str, Any]) -> bool:
    draft_ref = item.get("draft_ref")
    if not isinstance(draft_ref, dict):
        return False

    item_draft_id = str(draft_ref.get("draft_id") or "").strip()
    topic_draft_id = str(topic.get("draft_id") or "").strip()
    if item_draft_id and topic_draft_id and item_draft_id == topic_draft_id:
        return True

    item_index = safe_int_or_none(draft_ref.get("draft_index"))
    topic_index = safe_int_or_none(topic.get("draft_index"))
    if item_index is not None and topic_index is not None and item_index == topic_index:
        return True

    item_topic_id = str(draft_ref.get("topic_id") or "").strip()
    topic_id = str(topic.get("id") or topic.get("topic_id") or "").strip()
    return bool(item_topic_id and topic_id and item_topic_id == topic_id)


def opinion_item_payload(item: dict[str, Any]) -> dict[str, Any]:
    text = str(item.get("text") or "")
    return {
        "platform": item.get("platform"),
        "topic": item.get("topic"),
        "text": text[:180],
        "published_at": item.get("published_at"),
        "like_count": item.get("like_count", 0),
        "card_url": (item.get("card") or {}).get("url") or (item.get("screenshot") or {}).get("url"),
        "draft_ref": item.get("draft_ref") if isinstance(item.get("draft_ref"), dict) else {},
        "privacy_note": "匿名化后的公众反馈素材，只可用于观点观察，不写昵称和来源。",
    }


def safe_int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def opinion_match_keywords(topic: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for key in ("title", "angle"):
        value = str(topic.get(key) or "").strip()
        if value:
            candidates.extend(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", value))
    for key in ("entities", "themes"):
        values = topic.get(key)
        if isinstance(values, list):
            candidates.extend(str(value).strip() for value in values if str(value).strip())
    keywords: list[str] = []
    for candidate in candidates:
        normalized = re.sub(r"\s+", "", candidate)
        if len(normalized) >= 2 and normalized not in keywords:
            keywords.append(normalized)
    return keywords[:12]


def load_article_prompt(path: Path = ARTICLE_PROMPT_PATH) -> dict[str, str]:
    return load_prompt_json(path)


def load_reference_style(path: Path = REFERENCE_STYLE_PATH) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def load_humanizer_rules(path: Path = HUMANIZER_RULES_PATH) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def load_title_rules(path: Path = TITLE_RULES_PATH) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def default_image_plan(topic: dict[str, Any]) -> list[dict[str, str]]:
    images = topic.get("publishable_images", [])
    if images:
        return [
            {
                "position": "文章开头",
                "type": "official",
                "description": "使用官方来源图片，适合作为封面或首图。",
                "prompt_or_url": images[0],
            }
        ]
    return [
        {
            "position": "文章开头",
            "type": "generated_cover",
            "description": "无官方可发布图片，建议生成原创封面。",
            "prompt_or_url": "科技媒体封面，抽象 AI 工作流界面，干净背景，无品牌 logo，无水印，无真实人物，16:9",
        }
    ]


def normalize_image_plan(plan: list[Any]) -> list[dict[str, str]]:
    normalized = []
    for raw in plan:
        if not isinstance(raw, dict):
            continue
        image_type = str(raw.get("type") or "").strip()
        if image_type in {"publishable", "official_image", "official", "官方图"}:
            image_type = "official"
        elif image_type in {"cover", "generated", "generated_cover", "ai_cover", "原创封面", "原创生成"}:
            image_type = "generated_cover"
        normalized.append(
            {
                "position": str(raw.get("position") or "文章开头"),
                "type": image_type or "generated_cover",
                "description": str(raw.get("description") or ""),
                "prompt_or_url": str(raw.get("prompt_or_url") or raw.get("url") or raw.get("prompt") or ""),
            }
        )
    return normalized


def parse_model_json(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def save_drafts(drafts: list[ArticleDraft], path: Path = DRAFTS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = enrich_drafts_layout([draft.to_dict() for draft in drafts])
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    sync_drafts(payload)


def load_drafts(path: Path = DRAFTS_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return enrich_drafts_layout(json.load(handle))
