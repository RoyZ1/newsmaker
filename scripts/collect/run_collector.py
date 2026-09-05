import scripts._bootstrap  # noqa: F401

from app.collector import collect_once


if __name__ == "__main__":
    items = collect_once()
    print(f"采集完成，共 {len(items)} 条。结果已保存到 data/items.json")
