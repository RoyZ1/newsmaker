from __future__ import annotations

import sys
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import scripts._bootstrap  # noqa: F401,E402
from app.topics import add_manual_topic, delete_topic, generate_topics, load_topics, update_topic


def count_db_topics(db_path: Path) -> int:
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]


def test_manual_topic_can_be_added_and_loaded() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "topics.json"
        topic = add_manual_topic(
            {
                "title": "游戏更新引发玩家争议",
                "angle": "围绕版本变化、玩家反馈和厂商后续动作展开。",
                "themes": "游戏资讯, 玩家反馈",
                "entities": "Steam, 某游戏",
                "facts": "版本更新上线\n玩家集中反馈数值变化",
                "source_items": "游戏媒体 | 更新公告解读 | https://example.com/game",
            },
            path=path,
        )

        loaded = load_topics(path)
        assert len(loaded) == 1
        assert loaded[0]["id"] == topic["id"]
        assert loaded[0]["themes"] == ["游戏资讯", "玩家反馈"]
        assert loaded[0]["source_items"][0]["url"] == "https://example.com/game"


def test_topic_can_be_updated() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "topics.json"
        topic = add_manual_topic({"title": "旧选题", "facts": ["旧事实"]}, path=path)

        updated = update_topic(
            topic["id"],
            {
                "title": "新选题",
                "facts": "新事实一\n新事实二",
                "themes": ["热点新闻"],
            },
            path=path,
        )

        assert updated["title"] == "新选题"
        assert updated["facts"] == ["新事实一", "新事实二"]
        assert load_topics(path)[0]["themes"] == ["热点新闻"]


def test_topic_can_be_deleted() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "topics.json"
        topic = add_manual_topic({"title": "待删除选题"}, path=path)

        assert delete_topic(topic["id"], path=path)
        assert not load_topics(path)
        assert not delete_topic(topic["id"], path=path)


def test_generated_topic_avoids_signal_template_when_llm_unavailable() -> None:
    with TemporaryDirectory() as tmp:
        import app.topics as topics_module

        original_chat_completion = topics_module.chat_completion
        topics_module.chat_completion = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("llm unavailable"))
        items = [
            {
                "id": "game-1",
                "title": "Steam 热门游戏更新后差评激增，玩家质疑付费内容变多",
                "summary": "更新上线后，社区讨论集中在数值改动、付费道具和联机体验。",
                "source_name": "Game Media",
                "source_id": "game_media",
                "url": "https://example.com/game-1",
                "published_at": "2026-06-23T10:00:00+08:00",
                "score": 10,
            },
            {
                "id": "game-2",
                "title": "开发团队回应玩家反馈，称会继续调整游戏经济系统",
                "summary": "官方称会观察玩家数据，并在下一次补丁中调整奖励。",
                "source_name": "Official",
                "source_id": "official",
                "url": "https://example.com/game-2",
                "published_at": "2026-06-23T11:00:00+08:00",
                "score": 8,
            },
        ]

        try:
            topics = generate_topics(max_topics=1, items=items, path=Path(tmp) / "topics.json")
            topic = topics[0]
        finally:
            topics_module.chat_completion = original_chat_completion

        combined = f"{topic.title} {topic.angle}"
        assert "释放信号" not in combined
        assert "用户入口" not in combined
        assert "开发者生态" not in combined
        assert "企业采购" not in combined
        assert "玩家" in combined or "普通人" in combined


def test_generated_topic_connects_youth_pressure_with_company_policy_when_llm_unavailable() -> None:
    with TemporaryDirectory() as tmp:
        import app.topics as topics_module

        original_chat_completion = topics_module.chat_completion
        topics_module.chat_completion = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("llm unavailable"))
        items = [
            {
                "id": "job-1",
                "title": "某科技公司称今年新增上千个 AI 岗位，招聘条件更看重项目经验",
                "summary": "企业表示岗位数量增加，但岗位集中在算法、平台工程和销售解决方案等方向。",
                "source_name": "Company News",
                "source_id": "company_news",
                "url": "https://example.com/job-1",
                "published_at": "2026-06-24T09:00:00+08:00",
                "score": 10,
            },
            {
                "id": "job-2",
                "title": "高校毕业生反馈 AI 岗位面试更难，租房和通勤成本压缩求职选择",
                "summary": "求职者担心新增岗位和自己能投递的岗位并不匹配，薪资预期也更谨慎。",
                "source_name": "Youth Report",
                "source_id": "youth_report",
                "url": "https://example.com/job-2",
                "published_at": "2026-06-24T10:00:00+08:00",
                "score": 9,
            },
        ]

        try:
            topic = generate_topics(max_topics=1, items=items, path=Path(tmp) / "topics.json")[0]
        finally:
            topics_module.chat_completion = original_chat_completion

        combined = f"{topic.title} {topic.angle} {' '.join(topic.structure)}"
        assert "找工作" in combined or "招聘门槛" in combined
        assert "新增岗位" in combined or "企业动作" in combined
        assert "联系层" in combined
        assert "难处" in combined


def test_topic_editor_rejects_unsupported_llm_claims() -> None:
    with TemporaryDirectory() as tmp:
        import app.topics as topics_module

        original_chat_completion = topics_module.chat_completion
        topics_module.chat_completion = lambda *args, **kwargs: """
        {
          "topics": [
            {
              "id": "关键词-steam-玩家",
              "title": "Steam玩家集体破防：你的库存还值钱吗？",
              "angle": "很多普通玩家发现自己的游戏库贬值，家庭共享也被限制。",
              "structure": ["写涨价", "写家庭共享", "写库存贬值"]
            }
          ]
        }
        """
        items = [
            {
                "id": "game-1",
                "title": "Steam 热门游戏更新后差评激增，玩家质疑付费内容变多",
                "summary": "更新上线后，社区讨论集中在数值改动、付费道具和联机体验。",
                "source_name": "Game Media",
                "source_id": "game_media",
                "url": "https://example.com/game-1",
                "published_at": "2026-06-23T10:00:00+08:00",
                "score": 10,
            }
        ]
        try:
            topic = generate_topics(max_topics=1, items=items, path=Path(tmp) / "topics.json")[0]
        finally:
            topics_module.chat_completion = original_chat_completion

        combined = f"{topic.title} {topic.angle} {' '.join(topic.structure)}"
        assert "游戏库" not in combined
        assert "家庭共享" not in combined
        assert "库存" not in combined


def test_default_path_sync_removes_deleted_topics() -> None:
    tmp = ROOT_DIR / "data" / ".test-topic-management"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    import app.database as database
    import app.topics as topics_module

    topic_path = tmp / "topics.json"
    db_path = tmp / "agent.db"
    original_topic_path = topics_module.TOPICS_PATH
    original_db_path = database.DB_PATH
    try:
        topics_module.TOPICS_PATH = topic_path
        database.DB_PATH = db_path
        topic = topics_module.add_manual_topic({"title": "同步测试选题"}, path=topic_path)
        assert count_db_topics(db_path) == 1

        assert topics_module.delete_topic(topic["id"], path=topic_path)
        assert count_db_topics(db_path) == 0
    finally:
        topics_module.TOPICS_PATH = original_topic_path
        database.DB_PATH = original_db_path
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    tests = [
        test_manual_topic_can_be_added_and_loaded,
        test_topic_can_be_updated,
        test_topic_can_be_deleted,
        test_generated_topic_avoids_signal_template_when_llm_unavailable,
        test_generated_topic_connects_youth_pressure_with_company_policy_when_llm_unavailable,
        test_topic_editor_rejects_unsupported_llm_claims,
        test_default_path_sync_removes_deleted_topics,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
