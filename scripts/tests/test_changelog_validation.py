from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import scripts._bootstrap  # noqa: F401,E402
from app.changelog import append_changelog_entry, load_changelog, looks_garbled, save_changelog, valid_changelog_entry


def test_valid_chinese_entry_passes() -> None:
    assert valid_changelog_entry(
        {
            "timestamp": "2026-06-22T23:00:00+08:00",
            "category": "功能更新",
            "title": "新增社会热点和游戏资讯源",
            "details": ["补充通用新闻 RSS 和游戏媒体 RSS。"],
        }
    )


def test_question_mark_garble_is_rejected() -> None:
    assert looks_garbled("??????? AI ??????")
    assert not valid_changelog_entry(
        {
            "timestamp": "2026-06-22T23:00:00+08:00",
            "category": "????",
            "title": "??????? AI ??????",
            "details": ["????????????????????"],
        }
    )


def test_append_rejects_garbled_entry() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "changelog.json"
        try:
            append_changelog_entry("???????", ["????????????"], category="????", path=path)
        except ValueError:
            pass
        else:
            raise AssertionError("garbled changelog entry was accepted")


def test_save_filters_existing_garbled_entries() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "changelog.json"
        save_changelog(
            [
                {
                    "timestamp": "2026-06-22T23:00:00+08:00",
                    "category": "功能更新",
                    "title": "正常记录",
                    "details": ["正常内容"],
                },
                {
                    "timestamp": "2026-06-22T22:00:00+08:00",
                    "category": "????",
                    "title": "?????????????",
                    "details": ["????????????"],
                },
            ],
            path=path,
        )

        entries = load_changelog(path)
        assert len(entries) == 1
        assert entries[0]["title"] == "正常记录"


def main() -> None:
    tests = [
        test_valid_chinese_entry_passes,
        test_question_mark_garble_is_rejected,
        test_append_rejects_garbled_entry,
        test_save_filters_existing_garbled_entries,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
