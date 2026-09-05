from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import scripts._bootstrap  # noqa: F401,E402
from app.workflow import append_public_ip_hint, extract_wechat_invalid_ips


def test_extract_wechat_invalid_ip_prefers_wechat_errmsg_ip() -> None:
    message = "微信接口错误 40164: invalid ip 119.2.223.152 ipv6 ::ffff:119.2.223.152, not in whitelist"

    assert extract_wechat_invalid_ips(message) == ["119.2.223.152"]
    hint = append_public_ip_hint(message)
    assert "微信接口实际识别到的出口 IP：119.2.223.152" in hint
    assert "请优先把这个 IP 加到公众号后台 IP 白名单" in hint


def main() -> None:
    tests = [test_extract_wechat_invalid_ip_prefers_wechat_errmsg_ip]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
