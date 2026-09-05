from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import scripts._bootstrap  # noqa: F401,E402
from app.cover_images import load_cover_prompt
from app.heybox_writer import build_heybox_messages
from app.prompting import normalize_prompt_text
from app.title_writer import load_title_prompt
from app.topics import load_topic_prompt
from app.writer import build_messages, build_repair_messages, load_article_prompt, validate_article_output


def test_prompt_loader_joins_segmented_prompt_text() -> None:
    text = normalize_prompt_text(["第一段", "", "第二段", ["第三段", "第四段"]])

    assert text == "第一段\n\n第二段\n第三段\n第四段"


def test_segmented_prompt_files_load_as_plain_text() -> None:
    prompts = [
        load_article_prompt(),
        load_topic_prompt(),
        load_title_prompt(),
        load_cover_prompt(),
    ]

    for prompt in prompts:
        assert isinstance(prompt["system"], str)
        assert isinstance(prompt["user"], str)
        assert "\n" in prompt["user"]


def test_article_prompt_allows_question_endings_only_for_real_debate() -> None:
    messages = build_messages(
        {
            "id": "topic-1",
            "title": "游戏更新引发玩家争议",
            "angle": "玩家对付费内容和数值改动有正反反馈。",
            "facts": ["版本更新上线", "玩家对付费和数值有分歧"],
            "source_items": [],
        }
    )
    prompt = messages[-1]["content"]

    assert "正反两面" in prompt
    assert "提问式结尾" in prompt
    assert "不要每篇都用" in prompt


def test_article_prompt_targets_shorter_long_form_and_conditional_questions() -> None:
    messages = build_messages(
        {
            "id": "topic-policy-1",
            "title": "医保支付政策调整，普通人看病会变吗？",
            "angle": "从政府政策变化切入，讨论普通人办事和医疗消费的实际影响。",
            "facts": ["政府发布医保支付调整政策", "国内大型平台接入办事服务"],
            "source_items": [],
        }
    )
    prompt = messages[-1]["content"]
    repair_prompt = build_repair_messages(messages, "{}", ["正文太长"])[-1]["content"]

    assert "700-800 个中文字符" in prompt
    assert "650-900 个中文字符" in prompt
    assert "影响普通人" in prompt
    assert "国内大企业" in prompt
    assert "政府政策" in prompt
    assert "不要每篇都问" in prompt
    assert "700-800 个中文字符" in repair_prompt
    assert "政府政策/公共规则" in repair_prompt


def test_article_prompt_prioritizes_youth_life_pressure() -> None:
    messages = build_messages(
        {
            "id": "topic-youth-1",
            "title": "AI 岗位招聘变化，年轻人找工作会更难吗？",
            "angle": "从招聘要求、租房通勤和消费预算切入，讨论 AI 变化对刚入职年轻人的影响。",
            "facts": ["多家公司调整 AI 岗位要求", "青年租房和通勤成本受到关注"],
            "source_items": [],
        }
    )
    prompt = messages[-1]["content"]
    repair_prompt = build_repair_messages(messages, "{}", ["缺少具体影响"])[-1]["content"]

    assert "青年处境" in prompt
    assert "找工作" in prompt
    assert "租房" in prompt
    assert "买车" in prompt
    assert "招聘门槛" in prompt
    assert "具体难处" in prompt
    assert "青年难处只是出发点" in prompt
    assert "企业宣布扩招/新增岗位" in prompt
    assert "岗位类型、门槛、地区、薪资" in prompt
    assert "身份和站位" in prompt
    assert "转场桥" in prompt
    assert "不要突然硬切" in prompt
    assert "段落不要都写成同一种三句结构" in prompt
    assert "找工作、上班、租房、买车" in repair_prompt
    assert "企业动作、平台规则或政策口径" in repair_prompt
    assert "身份站位和真实担心" in repair_prompt
    assert "避免每段都像三句拼接" in repair_prompt


def test_topic_editor_prompt_prioritizes_youth_life_pressure() -> None:
    prompt = load_topic_prompt()["user"]

    assert "青年" in prompt
    assert "找工作" in prompt
    assert "租房" in prompt
    assert "攒钱买车" in prompt
    assert "青年难处只是出发点" in prompt
    assert "企业宣布新增岗位" in prompt
    assert "岗位类型、门槛、地区、薪资" in prompt
    assert "联系/难处/争议怎么写" in prompt


def test_article_validation_accepts_700_to_800_character_body() -> None:
    body = "\n\n".join(
        [
            "开头" * 80,
            "## 第一节",
            "**判断**" + "内容" * 140 + "<red>重点</red>",
            "## 第二节",
            "**变化**" + "影响" * 130,
        ]
    )
    issues = validate_article_output(
        {
            "title": "华为政策调整会影响上班吗？",
            "body_markdown": body,
        },
        {
            "title": "华为政策调整会影响上班吗？",
            "entities": ["华为", "政策"],
        },
    )

    assert not any("正文太短" in issue or "正文太长" in issue for issue in issues)


def test_article_prompt_introduces_people_before_industry_question() -> None:
    messages = build_messages(
        {
            "id": "topic-people-1",
            "title": "57场面试才进OpenAI，AI行业门槛真这么高？",
            "angle": "从一位候选人的求职经历切入，讨论 AI 行业岗位门槛和普通求职者预期。",
            "facts": ["候选人经历 57 场面试后进入 OpenAI", "求职链路漫长引发行业门槛讨论"],
            "source_items": [],
        }
    )
    prompt = messages[-1]["content"]

    assert "人物故事或个人经历" in prompt
    assert "这个人是谁" in prompt
    assert "57 场面试才进 OpenAI" in prompt


def test_heybox_prompt_inherits_question_ending_guardrail() -> None:
    messages = build_heybox_messages(
        {
            "title": "游戏更新引发玩家争议",
            "subtitle": "付费内容和数值改动让反馈分化",
            "body_markdown": "玩家对版本更新有支持和反对两种声音。",
            "source_links": [],
        }
    )
    prompt = messages[-1]["content"]

    assert "提问式结尾" in prompt
    assert "真实的正反舆论" in prompt
    assert "观察指标" in prompt
    assert "不单独改标题" in prompt
    assert "不要另起标题" in prompt


def main() -> None:
    tests = [
        test_prompt_loader_joins_segmented_prompt_text,
        test_segmented_prompt_files_load_as_plain_text,
        test_article_prompt_allows_question_endings_only_for_real_debate,
        test_article_prompt_targets_shorter_long_form_and_conditional_questions,
        test_article_prompt_prioritizes_youth_life_pressure,
        test_topic_editor_prompt_prioritizes_youth_life_pressure,
        test_article_validation_accepts_700_to_800_character_body,
        test_article_prompt_introduces_people_before_industry_question,
        test_heybox_prompt_inherits_question_ending_guardrail,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
