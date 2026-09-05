from __future__ import annotations

from typing import Any


CATEGORY_DEFINITIONS: dict[str, dict[str, Any]] = {
    "models": {
        "label": "模型与产品",
        "keywords": [
            "gpt",
            "chatgpt",
            "claude",
            "gemini",
            "deepseek",
            "qwen",
            "glm",
            "kimi",
            "llama",
            "模型",
            "大模型",
            "发布",
            "上线",
            "开源",
            "升级",
        ],
    },
    "agents": {
        "label": "Agent 与应用",
        "keywords": [
            "agent",
            "agents",
            "agentic",
            "智能体",
            "工作流",
            "助手",
            "搜索",
            "编程",
            "办公",
            "应用",
        ],
    },
    "infrastructure": {
        "label": "算力与基础设施",
        "keywords": [
            "nvidia",
            "gpu",
            "blackwell",
            "cuda",
            "芯片",
            "算力",
            "推理",
            "服务器",
            "数据中心",
            "云",
            "infra",
        ],
    },
    "companies": {
        "label": "大厂与商业",
        "keywords": [
            "openai",
            "anthropic",
            "microsoft",
            "google",
            "字节",
            "腾讯",
            "阿里",
            "百度",
            "融资",
            "收购",
            "ceo",
            "估值",
            "合作",
            "商业化",
        ],
    },
    "research": {
        "label": "论文与研究",
        "keywords": [
            "paper",
            "arxiv",
            "icml",
            "cvpr",
            "neurips",
            "论文",
            "研究",
            "benchmark",
            "评测",
            "sota",
        ],
    },
    "robotics": {
        "label": "机器人与硬件",
        "keywords": [
            "robot",
            "robotics",
            "vla",
            "embodied",
            "world model",
            "机器人",
            "具身",
            "硬件",
            "自动驾驶",
        ],
    },
    "policy": {
        "label": "监管与安全",
        "keywords": [
            "policy",
            "regulation",
            "safety",
            "lawsuit",
            "ban",
            "政府",
            "监管",
            "安全",
            "合规",
            "诉讼",
        ],
    },
}

DEFAULT_CATEGORY = "industry"
DEFAULT_CATEGORY_LABEL = "行业动态"


def classify_text(text: str) -> str:
    lowered = text.lower()
    best_key = DEFAULT_CATEGORY
    best_score = 0
    for key, definition in CATEGORY_DEFINITIONS.items():
        score = sum(1 for keyword in definition["keywords"] if keyword.lower() in lowered)
        if score > best_score:
            best_key = key
            best_score = score
    return best_key


def classify_item(item: dict[str, Any]) -> str:
    if item.get("category"):
        return str(item["category"])
    text = " ".join(
        [
            str(item.get("title", "")),
            str(item.get("summary", "")),
            " ".join(str(tag) for tag in item.get("tags", [])),
            str(item.get("source_name", "")),
        ]
    )
    return classify_text(text)


def category_label(category: str | None) -> str:
    if not category:
        return DEFAULT_CATEGORY_LABEL
    return CATEGORY_DEFINITIONS.get(category, {}).get("label", DEFAULT_CATEGORY_LABEL)


def category_options() -> list[dict[str, str]]:
    options = [{"key": DEFAULT_CATEGORY, "label": DEFAULT_CATEGORY_LABEL}]
    options.extend(
        {"key": key, "label": str(value["label"])}
        for key, value in CATEGORY_DEFINITIONS.items()
    )
    return options


def category_counts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for item in items:
        category = classify_item(item)
        counts[category] = counts.get(category, 0) + 1
    return [
        {"key": key, "label": category_label(key), "count": counts.get(key, 0)}
        for key in [DEFAULT_CATEGORY, *CATEGORY_DEFINITIONS.keys()]
        if counts.get(key, 0)
    ]
