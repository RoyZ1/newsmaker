import scripts._bootstrap  # noqa: F401

from app.writer import generate_article_drafts


if __name__ == "__main__":
    drafts = generate_article_drafts()
    print(f"文章草稿生成完成，共 {len(drafts)} 篇。结果已保存到 data/drafts.json")
