import scripts._bootstrap  # noqa: F401

from app.topics import generate_topics


if __name__ == "__main__":
    topics = generate_topics()
    print(f"选题生成完成，共 {len(topics)} 个。结果已保存到 data/topics.json")
